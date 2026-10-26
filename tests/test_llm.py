import pytest

import llm


class FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def make_client(monkeypatch):
    monkeypatch.setattr(llm, "BASE_URL", "http://127.0.0.1/v1")
    monkeypatch.setattr(llm, "API_KEY", "test-key")
    monkeypatch.setattr(llm.time, "sleep", lambda _seconds: None)
    return llm.LLM(model_name="test-model")


def test_missing_config_raises_on_init(monkeypatch):
    monkeypatch.setattr(llm, "BASE_URL", "")
    monkeypatch.setattr(llm, "API_KEY", "")
    with pytest.raises(RuntimeError, match="缺少 LLM 配置"):
        llm.LLM()


def test_chat_with_tools_retries_then_succeeds(monkeypatch):
    client = make_client(monkeypatch)
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise llm.requests.RequestException("boom")
        return FakeResponse({
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        })

    monkeypatch.setattr(llm.requests, "post", fake_post)
    data = client.chat_with_tools([{"role": "user", "content": "hi"}], [])

    assert data["choices"][0]["message"]["content"] == "ok"
    assert len(calls) == 2
    assert calls[0]["json"]["model"] == "test-model"


def test_chat_with_tools_raises_after_retries(monkeypatch):
    client = make_client(monkeypatch)

    def fake_post(*args, **kwargs):
        raise llm.requests.RequestException("boom")

    monkeypatch.setattr(llm.requests, "post", fake_post)
    with pytest.raises(RuntimeError, match="LLM 请求失败"):
        client.chat_with_tools([{"role": "user", "content": "hi"}], [], retries=1)


def test_chat_with_tools_rejects_malformed_response(monkeypatch):
    client = make_client(monkeypatch)
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: FakeResponse({}))

    with pytest.raises(RuntimeError, match="LLM 请求失败"):
        client.chat_with_tools([{"role": "user", "content": "hi"}], [], retries=0)
