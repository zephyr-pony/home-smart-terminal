import io
import wave

from fastapi.testclient import TestClient

import server
from agent.clarify import Clarifier
from assistant import Assistant
from domain.facts import Facts
from domain.store import AgentStore
from domain.tasks import Tasks
from items import Items
from memory import Memory


class FakeMemory:
    def count(self):
        return 0

    def close(self):
        pass


class FakeItems:
    def count(self):
        return 0

    def close(self):
        pass


class FakeTasks:
    def count(self):
        return 0

    def close(self):
        pass


class FakeTTS:
    async def synthesize_bytes(self, reply):
        return b"audio"


class FailingTTS:
    async def synthesize_bytes(self, reply):
        raise RuntimeError("tts down")


class FakeAssistant:
    def __init__(self):
        self.memory = FakeMemory()
        self.items = FakeItems()
        self.tasks = FakeTasks()
        self.sessions = {}

    def process_text(self, text, client_id):
        return {
            "type": "chat", "reply": text, "memories": [],
            "used_tools": [], "domain": "chat", "action": "chat",
        }


def make_real_assistant():
    items = Items(":memory:")
    store = AgentStore(":memory:")
    return Assistant(
        memory=Memory(":memory:"),
        llm=object(),
        items=items,
        clarifier=Clarifier(store=store, items=items),
        tasks=Tasks(":memory:"),
        facts=Facts(":memory:"),
    )


def wav_bytes(seconds=1):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * int(16000 * seconds))
    return stream.getvalue()


def test_api_requires_token(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    client = TestClient(server.app)
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_chat_accepts_valid_token_and_returns_metadata(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    monkeypatch.setattr(server, "assistant", FakeAssistant())
    monkeypatch.setattr(server, "tts", FakeTTS())
    client = TestClient(server.app)
    response = client.post(
        "/api/chat",
        headers={"Authorization": "Bearer secret"},
        json={"text": "你好"},
    )
    assert response.status_code == 200
    assert response.json()["domain"] == "chat"
    assert response.json()["action"] == "chat"


def test_voice_rejects_overlong_wav_before_stt(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    monkeypatch.setattr(server, "MAX_AUDIO_SECONDS", 1)
    monkeypatch.setattr(server, "_tmp_dir", "")
    client = TestClient(server.app)
    response = client.post(
        "/api/voice",
        headers={"Authorization": "Bearer secret"},
        files={"file": ("voice.wav", wav_bytes(2), "audio/wav")},
    )
    assert response.status_code in (400, 413, 503)


def test_chat_survives_tts_failure(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    monkeypatch.setattr(server, "assistant", FakeAssistant())
    monkeypatch.setattr(server, "tts", FailingTTS())
    client = TestClient(server.app)
    response = client.post(
        "/api/chat",
        headers={"Authorization": "Bearer secret"},
        json={"text": "你好"},
    )
    assert response.status_code == 200
    assert response.json()["reply"] == "你好"
    assert response.json()["audio"] == ""


def test_reminder_text_formatting():
    assert "已经过期 3 天" in server._reminder_text({"name": "酸奶", "days_left": -3})
    assert "今天到期" in server._reminder_text({"name": "酸奶", "days_left": 0})
    assert "还有 2 天过期" in server._reminder_text({"name": "酸奶", "days_left": 2})


def test_task_reminder_text_formatting():
    assert "已经过期 2 天" in server._task_reminder_text({"title": "交电费", "days_left": -2})
    assert "今天到期" in server._task_reminder_text({"title": "交电费", "days_left": 0})
    assert "还有 1 天到期" in server._task_reminder_text({"title": "交电费", "days_left": 1})


def test_events_requires_token(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    client = TestClient(server.app)
    assert client.get("/api/events").status_code == 401


def test_api_docs_are_disabled():
    """/docs、/redoc、/openapi.json 不对外暴露（公网入口防 schema 泄露）。"""
    client = TestClient(server.app)
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_pwa_assets_are_served():
    client = TestClient(server.app)
    manifest = client.get("/manifest.json")
    assert manifest.status_code == 200
    data = manifest.json()
    assert data["name"] and data["start_url"] == "/"
    assert any(icon["sizes"] == "192x192" for icon in data["icons"])

    # sw.js 必须挂在根路径，作用域才是 /
    sw = client.get("/sw.js")
    assert sw.status_code == 200
    assert sw.headers["service-worker-allowed"] == "/"

    assert client.get("/static/icons/icon-192.png").status_code == 200


def test_broadcast_delivers_to_subscriber():
    import asyncio

    async def run():
        queue = asyncio.Queue(maxsize=10)
        server._subscribers.add(queue)
        try:
            await server._broadcast({"type": "reminder", "reply": "提醒：酸奶还有 2 天过期"})
            return queue.get_nowait()
        finally:
            server._subscribers.discard(queue)

    payload = asyncio.run(run())
    assert "reminder" in payload
    assert "酸奶" in payload


def test_broadcast_drops_oldest_and_counts_when_queue_full(capsys):
    """队列满时丢最旧保最新，并累加 dropped_events（不再静默丢事件）。"""
    import asyncio

    async def run():
        queue = asyncio.Queue(maxsize=1)
        server._subscribers.add(queue)
        try:
            await server._broadcast({"type": "reminder", "reply": "第一条"})
            before = server.dropped_events
            await server._broadcast({"type": "reminder", "reply": "第二条"})
            return server.dropped_events - before, queue.get_nowait()
        finally:
            server._subscribers.discard(queue)

    dropped, payload = asyncio.run(run())
    assert dropped == 1
    assert "第二条" in payload
    assert "SSE 队列满" in capsys.readouterr().out


def test_stats_and_lists(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    assistant = make_real_assistant()
    assistant.memory.store("酸奶8月5号过期", "酸奶过期")
    assistant.items.add("牛奶", "2026-11-10")
    assistant.tasks.add("交电费", "2026-11-08", "bill")
    assistant.facts.store("爸妈家", "电费户号", "123456")
    assistant.clarifier.store.add_entity("property", "自己家")
    monkeypatch.setattr(server, "assistant", assistant)

    client = TestClient(server.app)
    headers = {"Authorization": "Bearer secret"}

    stats = client.get("/api/stats", headers=headers).json()
    assert stats["counts"] == {
        "memories": 1, "items": 1, "tasks": 1, "facts": 1, "entities": 1,
    }
    assert stats["tasks_by_status"]["pending"] == 1

    assert client.get("/api/memories", headers=headers).json()["memories"][0]["text"]
    assert client.get("/api/items", headers=headers).json()["items"][0]["name"] == "牛奶"
    assert client.get("/api/tasks", headers=headers).json()["tasks"][0]["title"] == "交电费"
    assert client.get("/api/facts", headers=headers).json()["facts"][0]["value"] == "123456"
    assert client.get("/api/entities", headers=headers).json()["entities"][0]["canonical_name"] == "自己家"


def test_task_complete_endpoint(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    assistant = make_real_assistant()
    task = assistant.tasks.add("交电费", "2026-11-08", "bill")
    monkeypatch.setattr(server, "assistant", assistant)

    client = TestClient(server.app)
    response = client.post(
        f"/api/tasks/{task['id']}/complete",
        headers={"Authorization": "Bearer secret"},
    )

    assert response.status_code == 200
    assert assistant.tasks.list_all()[0]["status"] == "done"


def test_list_search_and_pagination(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    assistant = make_real_assistant()
    for index in range(25):
        assistant.tasks.add(f"任务{index}", "2026-11-10", "custom")
    assistant.items.add("牛奶", "2026-11-10")
    assistant.items.add("面包", "2026-11-10")
    monkeypatch.setattr(server, "assistant", assistant)

    client = TestClient(server.app)
    headers = {"Authorization": "Bearer secret"}

    first = client.get("/api/tasks?limit=10&offset=0", headers=headers).json()
    assert first["total"] == 25
    assert len(first["tasks"]) == 10

    last = client.get("/api/tasks?limit=10&offset=20", headers=headers).json()
    assert len(last["tasks"]) == 5

    filtered = client.get("/api/tasks?q=任务1", headers=headers).json()
    assert filtered["total"] > 0
    assert all("任务1" in task["title"] for task in filtered["tasks"])

    items = client.get("/api/items?q=牛奶", headers=headers).json()
    assert items["total"] == 1
    assert items["items"][0]["name"] == "牛奶"


def test_delete_requires_confirmation(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    assistant = make_real_assistant()
    assistant.memory.store("酸奶8月5号过期", "酸奶过期")
    memory_id = assistant.memory.list_all()[0]["id"]
    monkeypatch.setattr(server, "assistant", assistant)

    client = TestClient(server.app)
    headers = {"Authorization": "Bearer secret"}

    assert client.delete(
        f"/api/memories/{memory_id}", headers=headers
    ).status_code == 400

    assert client.delete(
        f"/api/memories/{memory_id}?confirm=true", headers=headers
    ).status_code == 200
    assert assistant.memory.count() == 0


def test_task_update_endpoint(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    assistant = make_real_assistant()
    task = assistant.tasks.add("交电费", "2026-11-08", "bill")
    monkeypatch.setattr(server, "assistant", assistant)

    client = TestClient(server.app)
    headers = {"Authorization": "Bearer secret"}

    response = client.patch(
        f"/api/tasks/{task['id']}",
        headers=headers,
        json={"title": "交水费", "due_at": "2026-11-12", "type": "custom"},
    )
    assert response.status_code == 200
    updated = assistant.tasks.list_all()[0]
    assert updated["title"] == "交水费"
    assert updated["type"] == "custom"

    invalid = client.patch(
        f"/api/tasks/{task['id']}",
        headers=headers,
        json={"due_at": "2026-02-30"},
    )
    assert invalid.status_code == 400


def test_config_endpoint_hides_secrets(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    monkeypatch.setattr(server.config, "API_TOKEN", "secret")
    monkeypatch.setattr(server.config, "LLM_API_KEY", "sk-test-secret")
    monkeypatch.setattr(server.config, "LLM_BASE_URL", "https://example.com/v1")

    client = TestClient(server.app)
    response = client.get(
        "/api/config", headers={"Authorization": "Bearer secret"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["llm"]["configured"] is True
    assert body["api_token_enabled"] is True
    assert "sk-test-secret" not in response.text
    assert "example.com" not in response.text
    assert "TTS_VOICE" in body["values"]


def test_config_update_writes_env(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# comment\nTTS_VOICE=old\nLLM_API_KEY=secret\n", encoding="utf-8"
    )
    monkeypatch.setattr(server, "ENV_PATH", str(env_path))

    client = TestClient(server.app)
    response = client.patch(
        "/api/config",
        headers={"Authorization": "Bearer secret"},
        json={"values": {"TTS_VOICE": "zh-CN-YunxiNeural", "REMINDER_DAYS": 5}},
    )

    assert response.status_code == 200
    assert response.json()["restart_required"] is True
    text = env_path.read_text(encoding="utf-8")
    assert "# comment" in text
    assert "TTS_VOICE=zh-CN-YunxiNeural" in text
    assert "REMINDER_DAYS=5" in text
    assert "LLM_API_KEY=secret" in text


def test_config_update_rejects_unknown_field(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    client = TestClient(server.app)

    response = client.patch(
        "/api/config",
        headers={"Authorization": "Bearer secret"},
        json={"values": {"LLM_API_KEY": "hack"}},
    )

    assert response.status_code == 400


def test_status_exposes_drop_counter(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    monkeypatch.setattr(server, "assistant", FakeAssistant())
    monkeypatch.setattr(server, "dropped_events", 7)
    monkeypatch.setattr(server, "due_items", [])
    monkeypatch.setattr(server, "due_tasks", [])
    client = TestClient(server.app)
    body = client.get("/api/status", headers={"Authorization": "Bearer secret"}).json()
    assert body["dropped_events"] == 7
    assert body["due_items"] == []
    assert body["due_tasks"] == []
    assert body["task_count"] == 0


# ---------- Web Push ----------

class FakePushSender:
    def __init__(self, ok=True, gone=False, status_code=201):
        self.available = True
        self.sent = []
        self._ok = ok
        self._gone = gone
        self._status = status_code

    def send(self, subscription, payload):
        from push_sender import PushResult

        self.sent.append((subscription.get("endpoint"), payload))
        return PushResult(self._ok, self._status, gone=self._gone)


def make_push_env(monkeypatch, tmp_path, sender=None):
    from domain.push import PushStore

    monkeypatch.setattr(server, "API_TOKEN", "secret")
    store = PushStore(str(tmp_path / "push.db"))
    monkeypatch.setattr(server, "push_store", store)
    monkeypatch.setattr(server, "push_sender", sender)
    return store


def test_push_key_disabled_without_config(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    monkeypatch.setattr(server, "PUSH_ENABLED", False)
    monkeypatch.setattr(server, "VAPID_PUBLIC_KEY", "")
    client = TestClient(server.app)
    body = client.get(
        "/api/push/key", headers={"Authorization": "Bearer secret"}
    ).json()
    assert body["enabled"] is False
    assert body["publicKey"] == ""


def test_push_key_requires_token(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    client = TestClient(server.app)
    assert client.get("/api/push/key").status_code == 401


def test_push_subscribe_and_unsubscribe(monkeypatch, tmp_path):
    sender = FakePushSender()
    store = make_push_env(monkeypatch, tmp_path, sender)
    monkeypatch.setattr(server, "PUSH_ENABLED", True)
    monkeypatch.setattr(server, "VAPID_PUBLIC_KEY", "pub-key")
    client = TestClient(server.app)
    headers = {"Authorization": "Bearer secret"}

    payload = {
        "endpoint": "https://push.example.com/abc",
        "keys": {"p256dh": "k1", "auth": "a1"},
        "user_agent": "iPhone",
    }
    first = client.post("/api/push/subscribe", headers=headers, json=payload)
    assert first.status_code == 200
    assert first.json()["subscriptions"] == 1

    # 同一 endpoint 重复订阅不增加
    again = client.post("/api/push/subscribe", headers=headers, json=payload)
    assert again.json()["subscriptions"] == 1

    removed = client.post(
        "/api/push/unsubscribe",
        headers=headers,
        json={"endpoint": "https://push.example.com/abc"},
    )
    assert removed.json()["removed"] is True
    assert store.count_active() == 0


def test_push_subscribe_rejects_insecure_endpoint(monkeypatch, tmp_path):
    make_push_env(monkeypatch, tmp_path, FakePushSender())
    client = TestClient(server.app)
    response = client.post(
        "/api/push/subscribe",
        headers={"Authorization": "Bearer secret"},
        json={
            "endpoint": "http://insecure.example.com/abc",
            "keys": {"p256dh": "k", "auth": "a"},
        },
    )
    assert response.status_code == 400


def test_push_test_endpoint_disabled(monkeypatch, tmp_path):
    make_push_env(monkeypatch, tmp_path, None)
    client = TestClient(server.app)
    response = client.post(
        "/api/push/test", headers={"Authorization": "Bearer secret"}
    )
    assert response.status_code == 503


def test_push_test_endpoint_sends(monkeypatch, tmp_path):
    sender = FakePushSender()
    store = make_push_env(monkeypatch, tmp_path, sender)
    store.save("https://push.example.com/a", "k", "a")
    client = TestClient(server.app)
    body = client.post(
        "/api/push/test", headers={"Authorization": "Bearer secret"}
    ).json()
    assert body["ok"] is True
    assert body["sent"] == 1
    assert sender.sent[0][0] == "https://push.example.com/a"
    assert "推送测试" in sender.sent[0][1]["body"]


def test_push_removes_gone_subscription(monkeypatch, tmp_path):
    """410 的订阅在推送后被删除。"""
    import asyncio

    sender = FakePushSender(ok=False, gone=True, status_code=410)
    store = make_push_env(monkeypatch, tmp_path, sender)
    store.save("https://push.example.com/gone", "k", "a")

    sent, removed, failed = asyncio.run(
        server._send_push("提醒", {"type": "reminder", "tag": "t"})
    )
    assert (sent, removed, failed) == (0, 1, 0)
    assert store.count_active() == 0


def test_push_failure_counting(monkeypatch, tmp_path):
    import asyncio

    sender = FakePushSender(ok=False, gone=False, status_code=500)
    store = make_push_env(monkeypatch, tmp_path, sender)
    store.save("https://push.example.com/a", "k", "a")
    monkeypatch.setattr(server, "PUSH_MAX_FAILURES", 2)

    asyncio.run(server._send_push("提醒", {"type": "reminder"}))
    assert store.count_active() == 1
    asyncio.run(server._send_push("提醒", {"type": "reminder"}))
    assert store.count_active() == 0


class FakeDueStore:
    """到期来源：记录 claim 是否被调用。"""

    def __init__(self, fresh=None):
        self.fresh = fresh or []
        self.claimed = 0

    def get_due(self, days=0):
        return list(self.fresh)

    def claim_due_reminders(self, days=0, cooldown=0):
        self.claimed += 1
        return list(self.fresh)


class FakeDueAssistant:
    def __init__(self, items=None, tasks=None):
        self.items = items or FakeDueStore()
        self.tasks = tasks or FakeDueStore()


def test_scan_skips_claim_without_any_channel(monkeypatch, tmp_path):
    import asyncio

    assistant = FakeDueAssistant()
    monkeypatch.setattr(server, "assistant", assistant)
    make_push_env(monkeypatch, tmp_path, FakePushSender())
    monkeypatch.setattr(server, "_subscribers", set())

    delivered = asyncio.run(server._scan_once())
    assert delivered == 0
    assert assistant.items.claimed == 0
    assert assistant.tasks.claimed == 0


def test_scan_pushes_when_only_push_subscribers(monkeypatch, tmp_path):
    import asyncio

    item = {"id": 1, "name": "牛奶", "days_left": 2}
    assistant = FakeDueAssistant(items=FakeDueStore([item]))
    sender = FakePushSender()
    store = make_push_env(monkeypatch, tmp_path, sender)
    store.save("https://push.example.com/a", "k", "a")
    monkeypatch.setattr(server, "assistant", assistant)
    monkeypatch.setattr(server, "_subscribers", set())

    delivered = asyncio.run(server._scan_once())
    assert delivered == 1
    assert assistant.items.claimed == 1
    assert sender.sent[0][1]["body"].startswith("提醒：牛奶")


def test_scan_prefers_sse_over_push(monkeypatch, tmp_path):
    """有页面在线时只走 SSE，不再发系统通知。"""
    import asyncio

    task = {"id": 2, "title": "交电费", "days_left": 0}
    assistant = FakeDueAssistant(tasks=FakeDueStore([task]))
    sender = FakePushSender()
    store = make_push_env(monkeypatch, tmp_path, sender)
    store.save("https://push.example.com/a", "k", "a")
    monkeypatch.setattr(server, "assistant", assistant)
    monkeypatch.setattr(server, "_synthesize", _fake_synthesize)

    async def run_once():
        queue = asyncio.Queue(maxsize=5)   # 必须在当前事件循环里建
        monkeypatch.setattr(server, "_subscribers", {queue})
        delivered = await server._scan_once()
        return delivered, queue

    delivered, queue = asyncio.run(run_once())
    assert delivered == 1
    assert sender.sent == []
    assert queue.qsize() == 1
    payload = queue.get_nowait()
    assert "交电费" in payload


async def _fake_synthesize(reply):
    return ""


def test_config_endpoint_does_not_expose_vapid_private_key(monkeypatch, tmp_path):
    make_push_env(monkeypatch, tmp_path, FakePushSender())
    monkeypatch.setattr(server.config, "VAPID_PRIVATE_KEY", "super-secret-pem")
    monkeypatch.setattr(server.config, "PUSH_ENABLED", True)

    client = TestClient(server.app)
    response = client.get(
        "/api/config", headers={"Authorization": "Bearer secret"}
    )
    assert response.status_code == 200
    assert "super-secret-pem" not in response.text
    assert "VAPID_PRIVATE_KEY" not in response.text
    assert response.json()["push"]["enabled"] is True
