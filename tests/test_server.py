import io
import json
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
        self.seen_device_camera = None

    def process_text(self, text, client_id, on_delta=None, device_camera=False,
                     task_id=None):
        self.seen_device_camera = device_camera
        if on_delta is not None:
            on_delta(text)
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


def test_events_rejects_unknown_role(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "")

    client = TestClient(server.app)
    assert client.get("/api/events?role=robot").status_code == 400


def test_events_role_device_uses_its_own_channel(monkeypatch):
    """设备桥接器订阅 role=device：单独一组，不会被当成网页。

    直接调用端点、不消费流：这是一条无限 SSE，TestClient 消费起来会挂住。
    """
    import asyncio

    monkeypatch.setattr(server, "_subscribers", set())
    monkeypatch.setattr(server, "_device_subscribers", set())

    async def open_device():
        return await server.api_events(role="device", _auth=None)

    response = asyncio.run(open_device())
    try:
        assert response.status_code == 200
        assert len(server._device_subscribers) == 1
        assert server._subscribers == set()
    finally:
        for queue in list(server._device_subscribers):
            server._device_subscribers.discard(queue)


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


def test_task_complete_endpoint_returns_next_for_recurring(monkeypatch):
    """周期任务完成只跳过这次：接口要把下一次带回前端，卡片不能写「已完成」。"""
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    assistant = make_real_assistant()
    task = assistant.tasks.add("吃药", "2026-11-01T08:00", "custom", recur="daily")
    monkeypatch.setattr(server, "assistant", assistant)

    client = TestClient(server.app)
    body = client.post(
        f"/api/tasks/{task['id']}/complete",
        headers={"Authorization": "Bearer secret"},
    ).json()

    assert body["ok"] is True
    assert body["task"]["recur"] == "daily"
    assert body["task"]["status"] == "pending"
    assert assistant.tasks.get(task["id"])["due_at"] != "2026-11-01T08:00:00"


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
    monkeypatch.setattr(server, "_device_subscribers", {object(), object()})
    client = TestClient(server.app)
    body = client.get("/api/status", headers={"Authorization": "Bearer secret"}).json()
    assert body["dropped_events"] == 7
    assert body["device_channels"] == 2
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
    monkeypatch.setattr(server, "_device_subscribers", set())

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
    monkeypatch.setattr(server, "_device_subscribers", set())

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
        monkeypatch.setattr(server, "_device_subscribers", set())
        delivered = await server._scan_once()
        return delivered, queue

    delivered, queue = asyncio.run(run_once())
    assert delivered == 1
    assert sender.sent == []
    assert queue.qsize() == 1
    payload = queue.get_nowait()
    assert "交电费" in payload


def test_scan_prefers_device_over_page_and_push(monkeypatch, tmp_path):
    """设备在线时只投设备：不合成 mp3、网页不弹、手机不推。"""
    import asyncio

    item = {"id": 7, "name": "牛奶", "days_left": 0}
    assistant = FakeDueAssistant(items=FakeDueStore([item]))
    sender = FakePushSender()
    store = make_push_env(monkeypatch, tmp_path, sender)
    store.save("https://push.example.com/a", "k", "a")
    monkeypatch.setattr(server, "assistant", assistant)

    async def boom(_text):
        raise AssertionError("设备通道不需要合成 mp3")

    monkeypatch.setattr(server, "_synthesize", boom)

    async def run_once():
        device_queue = asyncio.Queue(maxsize=5)
        page_queue = asyncio.Queue(maxsize=5)
        monkeypatch.setattr(server, "_device_subscribers", {device_queue})
        monkeypatch.setattr(server, "_subscribers", {page_queue})
        delivered = await server._scan_once()
        return delivered, device_queue, page_queue

    delivered, device_queue, page_queue = asyncio.run(run_once())
    assert delivered == 1
    assert page_queue.empty()
    assert sender.sent == []
    event = json.loads(device_queue.get_nowait())
    assert event["reply"].startswith("提醒：牛奶")
    assert "audio" not in event


def test_has_receivers_counts_device_channel(monkeypatch):
    import asyncio

    monkeypatch.setattr(server, "_device_subscribers", {object()})
    monkeypatch.setattr(server, "_subscribers", set())
    monkeypatch.setattr(server, "push_store", None)

    assert asyncio.run(server._has_receivers()) is True


async def _fake_synthesize(reply):
    return ""


# ---------- 设备网关接口（ASR / TTS / ask）----------

class FakeSTT:
    def __init__(self, text="你好小马"):
        self.text = text
        self.calls = 0

    def transcribe(self, path):
        self.calls += 1
        return self.text


class FakeTTSBytes:
    async def synthesize_bytes(self, text):
        return ("mp3:" + text).encode("utf-8")


class _NullLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def test_asr_endpoint_returns_text(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    fake = FakeSTT("把牛奶记一下")
    monkeypatch.setattr(server, "stt", fake)
    monkeypatch.setattr(server, "stt_lock", _NullLock())
    monkeypatch.setattr(server, "_tmp_dir", str(tmp_path))

    client = TestClient(server.app)
    response = client.post(
        "/api/asr",
        headers={"Authorization": "Bearer secret"},
        files={"file": ("a.wav", wav_bytes(1), "audio/wav")},
    )
    assert response.status_code == 200
    assert response.json()["text"] == "把牛奶记一下"
    assert fake.calls == 1


def test_asr_endpoint_rejects_non_wav(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    monkeypatch.setattr(server, "stt", FakeSTT())
    monkeypatch.setattr(server, "_tmp_dir", str(tmp_path))
    client = TestClient(server.app)
    response = client.post(
        "/api/asr",
        headers={"Authorization": "Bearer secret"},
        files={"file": ("a.mp3", b"xx", "audio/mpeg")},
    )
    assert response.status_code == 400


def test_asr_endpoint_requires_token(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    client = TestClient(server.app)
    response = client.post(
        "/api/asr", files={"file": ("a.wav", wav_bytes(1), "audio/wav")}
    )
    assert response.status_code == 401


def test_tts_endpoint_returns_audio(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    monkeypatch.setattr(server, "tts", FakeTTSBytes())
    client = TestClient(server.app)
    response = client.post(
        "/api/tts",
        headers={"Authorization": "Bearer secret"},
        json={"text": "好的"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/mpeg")
    assert response.content == "mp3:好的".encode("utf-8")


def test_tts_endpoint_handles_failure(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    monkeypatch.setattr(server, "tts", FailingTTS())
    client = TestClient(server.app)
    response = client.post(
        "/api/tts",
        headers={"Authorization": "Bearer secret"},
        json={"text": "好的"},
    )
    assert response.status_code == 502


def test_ask_endpoint_returns_reply(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    monkeypatch.setattr(server, "assistant", FakeAssistant())
    monkeypatch.setattr(server, "tts", FakeTTSBytes())
    client = TestClient(server.app)
    body = client.post(
        "/api/ask",
        headers={"Authorization": "Bearer secret"},
        json={"text": "今天有什么安排"},
    ).json()
    assert body["ok"] is True
    assert body["reply"] == "今天有什么安排"
    assert "audio" not in body


def test_ask_endpoint_can_return_audio(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    monkeypatch.setattr(server, "assistant", FakeAssistant())
    monkeypatch.setattr(server, "tts", FakeTTSBytes())
    client = TestClient(server.app)
    body = client.post(
        "/api/ask",
        headers={"Authorization": "Bearer secret"},
        json={"text": "你好", "want_audio": True},
    ).json()
    import base64

    assert base64.b64decode(body["audio"]) == "mp3:你好".encode("utf-8")


def test_ask_endpoint_uses_default_client_id(monkeypatch):
    seen = {}

    class RecordingAssistant(FakeAssistant):
        def process_text(self, text, client_id, **kwargs):
            seen["client_id"] = client_id
            seen["task_id"] = kwargs.get("task_id")
            return super().process_text(text, client_id, **kwargs)

    monkeypatch.setattr(server, "API_TOKEN", "secret")
    monkeypatch.setattr(server, "assistant", RecordingAssistant())
    client = TestClient(server.app)
    client.post(
        "/api/ask",
        headers={"Authorization": "Bearer secret"},
        json={"text": "你好"},
    )
    assert seen["client_id"] == "xiaozhi"


# ---------- OpenAI 兼容入口（xiaozhi-server 用）----------

def test_chat_completions_non_stream(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    monkeypatch.setattr(server, "assistant", FakeAssistant())
    client = TestClient(server.app)
    body = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer secret"},
        json={
            "model": "xiaoma",
            "messages": [
                {"role": "system", "content": "你是小马"},
                {"role": "user", "content": "今天有什么事"},
            ],
        },
    ).json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "今天有什么事"
    assert body["choices"][0]["finish_reason"] == "stop"


def test_chat_completions_stream(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    monkeypatch.setattr(server, "assistant", FakeAssistant())
    client = TestClient(server.app)
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer secret"},
        json={"messages": [{"role": "user", "content": "你好"}], "stream": True},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "[DONE]" in response.text
    assert "你好" in response.text
    assert '"finish_reason": "stop"' in response.text


def test_chat_completions_rejects_missing_user_text(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    monkeypatch.setattr(server, "assistant", FakeAssistant())
    client = TestClient(server.app)
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer secret"},
        json={"messages": [{"role": "system", "content": "hi"}]},
    )
    assert response.status_code == 400


def test_last_user_text_handles_parts():
    messages = [
        {"role": "user", "content": "旧的"},
        {"role": "assistant", "content": "回答"},
        {"role": "user", "content": [{"type": "text", "text": "新的"},
                                     {"type": "text", "text": "问题"}]},
    ]
    assert server._last_user_text(messages) == "新的问题"


class StreamingAssistant(FakeAssistant):
    """模拟 agent 分两段吐出内容。"""

    def process_text(self, text, client_id, on_delta=None, device_camera=False,
                     task_id=None):
        if on_delta is not None:
            on_delta("第一句。")
            on_delta("第二句。")
        return {
            "type": "chat", "reply": "第一句。第二句。", "memories": [],
            "used_tools": [], "domain": "chat", "action": "chat",
        }


def test_chat_completions_stream_emits_multiple_chunks(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    monkeypatch.setattr(server, "assistant", StreamingAssistant())
    client = TestClient(server.app)
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer secret"},
        json={"messages": [{"role": "user", "content": "说说"}], "stream": True},
    )
    assert response.status_code == 200
    assert response.text.count("data: ") >= 4
    assert "第一句。" in response.text
    assert "第二句。" in response.text
    assert response.text.rstrip().endswith("[DONE]")


def test_chat_completions_stream_survives_agent_error(monkeypatch):
    class BrokenAssistant(FakeAssistant):
        def process_text(self, text, client_id, on_delta=None, device_camera=False,
                     task_id=None):
            raise RuntimeError("boom")

    monkeypatch.setattr(server, "API_TOKEN", "secret")
    monkeypatch.setattr(server, "assistant", BrokenAssistant())
    client = TestClient(server.app)
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer secret"},
        json={"messages": [{"role": "user", "content": "说说"}], "stream": True},
    )
    assert response.status_code == 200
    assert "出错" in response.text
    assert "[DONE]" in response.text


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


def test_task_postpone_endpoint(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    assistant = make_real_assistant()
    task = assistant.tasks.add("交电费", "2026-11-10", "bill")
    monkeypatch.setattr(server, "assistant", assistant)

    client = TestClient(server.app)
    response = client.post(
        f"/api/tasks/{task['id']}/postpone",
        json={"days": 2},
        headers={"Authorization": "Bearer secret"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["task"]["due_at"].startswith("2026-11-12")
    assert body["task"]["status"] == "pending"


def test_task_postpone_rejects_bad_days(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    assistant = make_real_assistant()
    task = assistant.tasks.add("交电费", "2026-11-10", "bill")
    monkeypatch.setattr(server, "assistant", assistant)

    client = TestClient(server.app)
    response = client.post(
        f"/api/tasks/{task['id']}/postpone",
        json={"days": 0},
        headers={"Authorization": "Bearer secret"},
    )
    assert response.status_code == 400


def test_task_postpone_missing_task(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    assistant = make_real_assistant()
    monkeypatch.setattr(server, "assistant", assistant)

    client = TestClient(server.app)
    response = client.post(
        "/api/tasks/9999/postpone",
        json={"days": 1},
        headers={"Authorization": "Bearer secret"},
    )
    assert response.status_code == 404


def test_task_postpone_presets_keep_the_clock(monkeypatch):
    """任务卡上的「明天 / 三天后」：日期由服务端算，时刻保留。"""
    from datetime import date, timedelta

    from agent.dates import parse_date_text

    monkeypatch.setattr(server, "API_TOKEN", "secret")
    assistant = make_real_assistant()
    today = date.today()
    task = assistant.tasks.add("取快递", today.isoformat() + "T15:00:00")
    monkeypatch.setattr(server, "assistant", assistant)
    client = TestClient(server.app)
    headers = {"Authorization": "Bearer secret"}

    expected = {
        "tomorrow": (today + timedelta(days=1)).isoformat(),
        "in_3_days": (today + timedelta(days=3)).isoformat(),
        "next_monday": parse_date_text("下周一", today)["date"],
    }
    for preset, day in expected.items():
        response = client.post(
            f"/api/tasks/{task['id']}/postpone",
            json={"preset": preset},
            headers=headers,
        )
        assert response.status_code == 200
        assert response.json()["task"]["due_at"] == day + "T15:00:00"


def test_task_postpone_rejects_unknown_preset(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    assistant = make_real_assistant()
    task = assistant.tasks.add("取快递", "2026-11-25T15:00:00")
    monkeypatch.setattr(server, "assistant", assistant)

    client = TestClient(server.app)
    response = client.post(
        f"/api/tasks/{task['id']}/postpone",
        json={"preset": "next_year"},
        headers={"Authorization": "Bearer secret"},
    )
    assert response.status_code == 400


def test_task_postpone_accepts_absolute_date(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    assistant = make_real_assistant()
    task = assistant.tasks.add("取快递", "2026-11-25T15:00:00")
    monkeypatch.setattr(server, "assistant", assistant)

    client = TestClient(server.app)
    response = client.post(
        f"/api/tasks/{task['id']}/postpone",
        json={"due_at": "2026-12-01"},
        headers={"Authorization": "Bearer secret"},
    )
    assert response.status_code == 200
    assert response.json()["task"]["due_at"] == "2026-12-01T15:00:00"

def test_stats_includes_breakdowns(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    assistant = make_real_assistant()
    assistant.items.add("牛奶", "2026-11-10")
    assistant.items.add("面包", "2027-12-01")
    assistant.tasks.add("交电费", "2026-11-10", "bill")
    assistant.tasks.add("开会", "2026-11-11", "schedule")
    monkeypatch.setattr(server, "assistant", assistant)

    client = TestClient(server.app)
    response = client.get("/api/stats", headers={"Authorization": "Bearer secret"})

    assert response.status_code == 200
    body = response.json()
    assert body["items_by_status"]["expired"] == 1
    assert body["items_by_status"]["active"] == 1
    assert body["tasks_by_type"] == {"bill": 1, "schedule": 1}
    assert "due_today" in body


def test_tasks_sort_and_validation(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    assistant = make_real_assistant()
    assistant.tasks.add("B 任务", "2026-11-30", "custom")
    assistant.tasks.add("A 任务", "2026-11-10", "custom")
    monkeypatch.setattr(server, "assistant", assistant)

    client = TestClient(server.app)
    headers = {"Authorization": "Bearer secret"}

    by_title = client.get("/api/tasks?sort=title", headers=headers).json()
    assert [t["title"] for t in by_title["tasks"]] == ["A 任务", "B 任务"]

    by_due = client.get("/api/tasks?sort=due", headers=headers).json()
    assert [t["title"] for t in by_due["tasks"]] == ["A 任务", "B 任务"]

    bad = client.get("/api/tasks?sort=unknown", headers=headers)
    assert bad.status_code == 400


def test_memories_sort_oldest(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    assistant = make_real_assistant()
    assistant.memory.store("第一条", "一")
    assistant.memory.store("第二条", "二")
    monkeypatch.setattr(server, "assistant", assistant)

    client = TestClient(server.app)
    headers = {"Authorization": "Bearer secret"}

    newest = client.get("/api/memories?sort=newest", headers=headers).json()
    oldest = client.get("/api/memories?sort=oldest", headers=headers).json()
    assert newest["memories"][0]["text"] == oldest["memories"][-1]["text"]
    assert newest["memories"][0]["text"] != oldest["memories"][0]["text"]

    bad = client.get("/api/memories?sort=unknown", headers=headers)
    assert bad.status_code == 400

# ---------- 摄像头：tool_calls 与看图（2026-11-23） ----------

CAMERA_TOOL = {
    "type": "function",
    "function": {
        "name": "self.camera.take_photo",
        "description": "拍一张照片并回答关于画面里的问题",
        "parameters": {
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
        },
    },
}


class CameraAssistant(FakeAssistant):
    """模拟带摄像头时 agent 的决定：这一轮交给设备拍，不生成回复。"""

    def process_text(self, text, client_id, on_delta=None, device_camera=False,
                     task_id=None):
        self.seen_device_camera = device_camera
        return {
            "type": "device_call", "reply": "",
            "used_tools": ["camera_look"], "domain": "vision", "action": "look",
            "device_call": {
                "name": "self.camera.take_photo",
                "arguments": {"question": "桌上有什么"},
            },
        }


def _camera_request(client, stream=False, tools=None):
    return client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer secret"},
        json={
            "model": "xiaoma",
            "messages": [{"role": "user", "content": "看看桌上有什么"}],
            "stream": stream,
            "tools": tools or [CAMERA_TOOL],
        },
    )


def test_chat_completions_stream_returns_camera_tool_call(monkeypatch):
    assistant = CameraAssistant()
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    monkeypatch.setattr(server, "assistant", assistant)
    client = TestClient(server.app)

    response = _camera_request(client, stream=True)

    assert response.status_code == 200
    assert "self.camera.take_photo" in response.text
    assert '"finish_reason": "tool_calls"' in response.text
    assert '"finish_reason": "stop"' not in response.text
    assert "桌上有什么" in response.text
    assert response.text.rstrip().endswith("[DONE]")
    assert assistant.seen_device_camera is True


def test_chat_completions_non_stream_returns_camera_tool_call(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    monkeypatch.setattr(server, "assistant", CameraAssistant())
    client = TestClient(server.app)

    body = _camera_request(client).json()

    choice = body["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] is None
    call = choice["message"]["tool_calls"][0]
    assert call["type"] == "function"
    assert call["function"]["name"] == "self.camera.take_photo"
    assert json.loads(call["function"]["arguments"]) == {"question": "桌上有什么"}


def test_chat_completions_passes_camera_capability(monkeypatch):
    """有没有摄像头由请求里的设备工具决定，不能让网页入口也以为能看。"""
    assistant = FakeAssistant()
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    monkeypatch.setattr(server, "assistant", assistant)
    client = TestClient(server.app)

    client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer secret"},
        json={"messages": [{"role": "user", "content": "你好"}]},
    )
    assert assistant.seen_device_camera is False

    _camera_request(client)
    assert assistant.seen_device_camera is True


class VisionAssistant(FakeAssistant):
    def __init__(self):
        super().__init__()
        self.seen = None

    def describe_image(self, question, images, client_id="xiaozhi"):
        self.seen = (question, images, client_id)
        return "桌上有一盒牛奶。"

    def process_text(self, *args, **kwargs):
        raise AssertionError("看图不该走进对话")


def _vision_request(client, stream=False):
    return client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer secret"},
        json={
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "桌上有什么"},
                    {"type": "image_url",
                     "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
                ],
            }],
            "stream": stream,
        },
    )


def test_chat_completions_routes_images_to_vision(monkeypatch):
    assistant = VisionAssistant()
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    monkeypatch.setattr(server, "assistant", assistant)
    client = TestClient(server.app)

    body = _vision_request(client).json()

    assert body["choices"][0]["message"]["content"] == "桌上有一盒牛奶。"
    assert assistant.seen[0] == "桌上有什么"
    assert assistant.seen[1] == ["data:image/jpeg;base64,AAAA"]


def test_chat_completions_streams_vision_reply(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    monkeypatch.setattr(server, "assistant", VisionAssistant())
    client = TestClient(server.app)

    response = _vision_request(client, stream=True)

    assert "桌上有一盒牛奶。" in response.text
    assert '"finish_reason": "stop"' in response.text
    assert response.text.rstrip().endswith("[DONE]")


def test_user_message_splits_text_and_images():
    text, images = server._user_text_and_images({
        "role": "user",
        "content": [
            {"type": "text", "text": "这个是不是"},
            {"type": "text", "text": "牛奶"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA"}},
        ],
    })
    assert text == "这个是不是牛奶"
    assert images == ["data:image/jpeg;base64,AA"]


def test_has_camera_tool_ignores_other_tools():
    assert server._has_camera_tool([CAMERA_TOOL]) is True
    assert server._has_camera_tool([]) is False
    assert server._has_camera_tool([
        {"type": "function", "function": {"name": "self.get_device_status"}},
    ]) is False


SANITIZED_CAMERA_TOOL = {
    "type": "function",
    "function": {
        "name": "self_camera_take_photo",  # 小智把点换成下划线后再给 LLM
        "description": "拍一张照片并回答关于画面里的问题",
        "parameters": {
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
        },
    },
}


def test_camera_tool_name_accepts_sanitized_name():
    """真机上小智发来的是 self_camera_take_photo，不能只认带点的写法。"""
    assert server._camera_tool_name([SANITIZED_CAMERA_TOOL]) == "self_camera_take_photo"
    assert server._has_camera_tool([SANITIZED_CAMERA_TOOL]) is True


def test_tool_call_uses_name_from_request(monkeypatch):
    """回调用请求里的名字，否则小智查不到这个工具，拍照直接失败。"""
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    monkeypatch.setattr(server, "assistant", CameraAssistant())
    client = TestClient(server.app)

    body = _camera_request(client, tools=[SANITIZED_CAMERA_TOOL]).json()

    call = body["choices"][0]["message"]["tool_calls"][0]
    assert call["function"]["name"] == "self_camera_take_photo"


def test_stream_tool_call_uses_name_from_request(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    monkeypatch.setattr(server, "assistant", CameraAssistant())
    client = TestClient(server.app)

    response = _camera_request(client, stream=True, tools=[SANITIZED_CAMERA_TOOL])

    assert "self_camera_take_photo" in response.text
    assert "self.camera.take_photo" not in response.text


# ---------- 任务上下文：从提醒点进来 ----------

def test_chat_passes_bound_task(monkeypatch):
    seen = {}

    class Recording(FakeAssistant):
        def process_text(self, text, client_id, **kwargs):
            seen["task_id"] = kwargs.get("task_id")
            return super().process_text(text, client_id, **kwargs)

    monkeypatch.setattr(server, "API_TOKEN", "secret")
    monkeypatch.setattr(server, "assistant", Recording())
    client = TestClient(server.app)

    response = client.post(
        "/api/chat",
        headers={"Authorization": "Bearer secret"},
        json={"text": "完成了", "task_id": 7},
    )

    assert response.status_code == 200
    assert seen["task_id"] == 7


def test_voice_passes_bound_task(monkeypatch, tmp_path):
    seen = {}

    class StubStt:
        def transcribe(self, path):
            return "完成了"

    class VoiceAssistant(FakeAssistant):
        def process_voice(self, stt, audio_path, client_id=None, task_id=None):
            seen["task_id"] = task_id
            return {
                "type": "chat", "reply": "好", "text": "完成了", "memories": [],
                "used_tools": [], "domain": "chat", "action": "chat",
            }

    monkeypatch.setattr(server, "API_TOKEN", "secret")
    monkeypatch.setattr(server, "_tmp_dir", str(tmp_path))
    monkeypatch.setattr(server, "stt", StubStt())
    monkeypatch.setattr(server, "assistant", VoiceAssistant())
    monkeypatch.setattr(server, "tts", FakeTTS())
    client = TestClient(server.app)

    response = client.post(
        "/api/voice",
        headers={"Authorization": "Bearer secret"},
        data={"client_id": "web", "task_id": "7"},
        files={"file": ("v.wav", wav_bytes(1), "audio/wav")},
    )

    assert response.status_code == 200
    assert seen["task_id"] == 7


def test_push_payload_carries_task_id(monkeypatch, tmp_path):
    import asyncio

    sender = FakePushSender()
    store = make_push_env(monkeypatch, tmp_path, sender)
    store.save("https://push.example.com/a", "k", "a")

    asyncio.run(server._send_push("提醒：交电费", {
        "type": "reminder",
        "task": {"id": 12, "title": "交电费"},
        "tag": "task-12",
    }))

    payload = sender.sent[0][1]
    assert payload["task_id"] == 12
    assert payload["task_title"] == "交电费"
    assert payload["kind"] == "task"


def test_push_payload_without_task_has_zero_id(monkeypatch, tmp_path):
    import asyncio

    sender = FakePushSender()
    store = make_push_env(monkeypatch, tmp_path, sender)
    store.save("https://push.example.com/a", "k", "a")

    asyncio.run(server._send_push("提醒：牛奶还有 2 天过期", {
        "type": "reminder", "item": {"id": 3, "name": "牛奶"},
    }))

    payload = sender.sent[0][1]
    assert payload["task_id"] == 0
    assert payload["kind"] == "item"
