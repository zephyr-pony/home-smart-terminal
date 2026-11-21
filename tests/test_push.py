import pytest

from domain.push import PushStore
from push_sender import PushResult, WebPushSender


def make_store():
    return PushStore(":memory:")


def test_save_is_idempotent_by_endpoint():
    store = make_store()
    store.save("https://push.example.com/a", "key1", "auth1", "iPhone")
    store.save("https://push.example.com/a", "key2", "auth2", "iPhone 15")

    rows = store.list_active()
    assert len(rows) == 1
    assert rows[0]["p256dh"] == "key2"
    assert store.count_active() == 1


def test_save_requires_https_and_keys():
    store = make_store()
    with pytest.raises(ValueError):
        store.save("http://insecure.example.com/a", "k", "a")
    with pytest.raises(ValueError):
        store.save("https://push.example.com/a", "", "a")


def test_remove_is_idempotent():
    store = make_store()
    store.save("https://push.example.com/a", "k", "a")
    assert store.remove("https://push.example.com/a") is True
    assert store.remove("https://push.example.com/a") is False
    assert store.count_active() == 0


def test_failure_disables_after_threshold():
    store = make_store()
    endpoint = "https://push.example.com/a"
    store.save(endpoint, "k", "a")

    for _ in range(2):
        store.mark_failure(endpoint, max_failures=3)
    assert store.count_active() == 1

    assert store.mark_failure(endpoint, max_failures=3) == 3
    assert store.count_active() == 0
    assert store.get(endpoint) is not None  # 保留记录便于排障

    # 重新订阅可以复活
    store.save(endpoint, "k", "a")
    assert store.count_active() == 1


def test_mark_success_resets_failures():
    store = make_store()
    endpoint = "https://push.example.com/a"
    store.save(endpoint, "k", "a")
    store.mark_failure(endpoint, max_failures=5)
    store.mark_success(endpoint)

    row = store.get(endpoint)
    assert row["fail_count"] == 0
    assert row["last_ok_at"]


def test_sender_without_key_is_unavailable():
    sender = WebPushSender("", "mailto:a@b.com")
    assert sender.available is False
    result = sender.send({"endpoint": "https://x", "keys": {}}, {})
    assert result.ok is False


class FakeResponse(object):
    status_code = 201


def test_sender_returns_result_on_success(monkeypatch):
    import pywebpush

    sender = WebPushSender.__new__(WebPushSender)
    sender._subject = "mailto:a@b.com"
    sender._ttl = 60
    sender._timeout = 1
    sender._vapid = object()

    monkeypatch.setattr(pywebpush, "webpush", lambda **kwargs: FakeResponse())
    result = sender.send(
        {"endpoint": "https://push.example.com/a", "keys": {"p256dh": "k", "auth": "a"}},
        {"title": "t"},
    )
    assert result.ok is True
    assert result.status_code == 201


def test_sender_marks_gone_on_410(monkeypatch):
    import pywebpush

    class GoneError(pywebpush.WebPushException):
        def __init__(self):
            self.message = "subscription gone"
            self.response = type("R", (), {"status_code": 410})()

    sender = WebPushSender.__new__(WebPushSender)
    sender._subject = "mailto:a@b.com"
    sender._ttl = 60
    sender._timeout = 1
    sender._vapid = object()

    def boom(**kwargs):
        raise GoneError()

    monkeypatch.setattr(pywebpush, "webpush", boom)
    result = sender.send(
        {"endpoint": "https://push.example.com/a", "keys": {"p256dh": "k", "auth": "a"}},
        {"title": "t"},
    )
    assert result.ok is False
    assert result.gone is True
    assert result.status_code == 410
