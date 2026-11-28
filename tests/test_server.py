import io
import json
import logging
import time
import wave
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import server
from agent.clarify import Clarifier
from assistant import Assistant
from domain.facts import Facts
from domain.space import DEFAULT_SPACE_ID
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
                     task_id=None, attachment=None, extra_tools=None,
                     allow_subagent=False):
        self.seen_device_camera = device_camera
        self.seen_extra_tools = extra_tools
        self.seen_attachment = attachment
        self.seen_allow_subagent = allow_subagent
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


def test_status_reports_runtime(monkeypatch, tmp_path):
    """运行观察（D67）：跑多久、核心数据多大、打印目录几个文件、磁盘剩余。"""
    (tmp_path / "memory.db").write_bytes(b"x" * 1024)
    print_dir = tmp_path / "print"
    print_dir.mkdir()
    (print_dir / "a.txt").write_text("hello", encoding="utf-8")
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    monkeypatch.setattr(server, "assistant", FakeAssistant())
    monkeypatch.setattr(server, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(server, "PRINT_FILES_DIR", str(print_dir))
    monkeypatch.setattr(server, "started_at", time.time() - 125)

    client = TestClient(server.app)
    body = client.get(
        "/api/status", headers={"Authorization": "Bearer secret"}
    ).json()
    runtime = body["runtime"]

    assert runtime["uptime_seconds"] >= 120
    assert runtime["core_mb"] >= 0
    assert runtime["print_files"] == 1
    assert runtime["print_mb"] >= 0
    assert runtime["disk_total_gb"] > 0
    assert runtime["disk_free_gb"] >= 0
    assert body["space_id"] == DEFAULT_SPACE_ID


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
    """到期来源：记录 claim 是否被调用（mode 用来区分 timely / soft 两段扫描）。"""

    def __init__(self, fresh=None, mode=None):
        self.fresh = fresh or []
        self.mode = mode
        self.claimed = 0
        self.modes = []

    def get_due(self, days=0):
        return list(self.fresh)

    def claim_due_reminders(self, days=0, cooldown=0, now=None, mode=None):
        self.modes.append(mode)
        if mode == "soft" and self.mode != "soft":
            return []
        if mode == "timely" and self.mode == "soft":
            return []
        self.claimed += 1
        return list(self.fresh)


class FakeDueAssistant:
    def __init__(self, items=None, tasks=None):
        self.items = items or FakeDueStore()
        self.tasks = tasks or FakeDueStore()


# 软提醒的门控要可判定：不写死「现在几点」，测试自己给时刻
MORNING = datetime(2026, 11, 27, 9, 0)
EVENING = datetime(2026, 11, 27, 21, 0)


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

    delivered = asyncio.run(server._scan_once(now=EVENING))
    assert delivered == 1
    assert assistant.items.claimed == 1
    assert sender.sent[0][1]["body"].startswith("提醒：牛奶")


def test_scan_holds_items_until_evening(monkeypatch, tmp_path):
    """物品到期属于软提醒：白天不播报，等晚上兜底才发（D81）。"""
    import asyncio

    item = {"id": 1, "name": "牛奶", "days_left": 0}
    assistant = FakeDueAssistant(items=FakeDueStore([item]))
    sender = FakePushSender()
    store = make_push_env(monkeypatch, tmp_path, sender)
    store.save("https://push.example.com/a", "k", "a")
    monkeypatch.setattr(server, "assistant", assistant)
    monkeypatch.setattr(server, "_subscribers", set())
    monkeypatch.setattr(server, "_device_subscribers", set())

    delivered = asyncio.run(server._scan_once(now=MORNING))
    assert delivered == 0
    assert assistant.items.claimed == 0
    assert sender.sent == []


def test_scan_claims_soft_task_only_in_the_evening(monkeypatch, tmp_path):
    """soft 任务白天不进领取（留给聊天），晚上才走兜底投递。"""
    import asyncio

    task = {"id": 3, "title": "取快递", "days_left": 0, "mode": "soft"}
    assistant = FakeDueAssistant(tasks=FakeDueStore([task], mode="soft"))
    sender = FakePushSender()
    store = make_push_env(monkeypatch, tmp_path, sender)
    store.save("https://push.example.com/a", "k", "a")
    monkeypatch.setattr(server, "assistant", assistant)
    monkeypatch.setattr(server, "_subscribers", set())
    monkeypatch.setattr(server, "_device_subscribers", set())

    assert asyncio.run(server._scan_once(now=MORNING)) == 0
    assert assistant.tasks.claimed == 0

    delivered = asyncio.run(server._scan_once(now=EVENING))
    assert delivered == 1
    assert sender.sent[0][1]["body"].startswith("提醒：取快递")


def test_soft_window_opens_at_configured_time(monkeypatch):
    monkeypatch.setattr(server, "SOFT_REMINDER_LAST_HOUR", 20)
    monkeypatch.setattr(server, "SOFT_REMINDER_LAST_MINUTE", 30)

    assert server._soft_window_open(datetime(2026, 11, 27, 20, 29)) is False
    assert server._soft_window_open(datetime(2026, 11, 27, 20, 30)) is True
    assert server._soft_window_open(datetime(2026, 11, 27, 23, 59)) is True


def test_soft_reminder_facts_say_its_a_make_up_line():
    facts = server._task_reminder_facts(
        {"title": "取快递", "due_at": "2026-11-27T15:00:00", "days_left": 0},
        soft=True,
    )

    assert "软提醒" in facts
    assert server._task_reminder_facts(
        {"title": "取快递", "days_left": 0}, soft=False
    ).count("软提醒") == 0


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
        delivered = await server._scan_once(now=EVENING)
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
                     task_id=None, extra_tools=None):
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
                     task_id=None, extra_tools=None):
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
                     task_id=None, extra_tools=None):
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


EXIT_TOOL = {
    "type": "function",
    "function": {
        "name": "handle_exit_intent",
        "description": "当用户想结束对话或需要退出系统时调用",
        "parameters": {
            "type": "object",
            "properties": {"say_goodbye": {"type": "string"}},
            "required": ["say_goodbye"],
        },
    },
}


class ExitAssistant(FakeAssistant):
    """模拟模型决定结束会话：这一轮把 handle_exit_intent 交给服务端执行。"""

    def __init__(self):
        super().__init__()
        self.seen_extra_tools = None

    def process_text(self, text, client_id, on_delta=None, device_camera=False,
                     task_id=None, extra_tools=None):
        self.seen_extra_tools = extra_tools
        return {
            "type": "device_call", "reply": "",
            "device_call": {
                "name": "handle_exit_intent",
                "arguments": {"say_goodbye": "好的，再见"},
            },
        }


def test_chat_completions_passes_exit_tool_and_returns_its_call(monkeypatch):
    """「再见」要能结束会话：schema 转给模型，模型一调就原样回给服务端执行。"""
    assistant = ExitAssistant()
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    monkeypatch.setattr(server, "assistant", assistant)
    client = TestClient(server.app)

    body = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer secret"},
        json={
            "messages": [{"role": "user", "content": "再见"}],
            "tools": [EXIT_TOOL],
        },
    ).json()

    assert [t["function"]["name"] for t in assistant.seen_extra_tools] == [
        "handle_exit_intent"
    ]
    # 上游那句描述太笼统，转发时补了「去忙了」这类边界
    assert "去忙了" in assistant.seen_extra_tools[0]["function"]["description"]
    call = body["choices"][0]["message"]["tool_calls"][0]
    assert call["function"]["name"] == "handle_exit_intent"
    assert json.loads(call["function"]["arguments"]) == {"say_goodbye": "好的，再见"}


def test_chat_completions_stream_returns_exit_tool_call(monkeypatch):
    assistant = ExitAssistant()
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    monkeypatch.setattr(server, "assistant", assistant)
    client = TestClient(server.app)

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer secret"},
        json={
            "messages": [{"role": "user", "content": "再见"}],
            "stream": True,
            "tools": [EXIT_TOOL],
        },
    )

    assert "handle_exit_intent" in response.text
    assert '"finish_reason": "tool_calls"' in response.text
    assert response.text.rstrip().endswith("[DONE]")


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


def test_chat_allows_subagent_voice_does_not(monkeypatch):
    """D89：只有网页文字和 CLI 能开子任务，语音入口不给。"""
    fake = FakeAssistant()
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    monkeypatch.setattr(server, "assistant", fake)
    client = TestClient(server.app)

    response = client.post(
        "/api/chat",
        headers={"Authorization": "Bearer secret"},
        json={"text": "帮我查三家店的营业时间"},
    )

    assert response.status_code == 200
    assert fake.seen_allow_subagent is True


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


# ---------- 开源工程的启动底线 ----------

def test_healthz_needs_no_token(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    monkeypatch.setattr(server, "assistant", FakeAssistant())
    client = TestClient(server.app)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_healthz_reports_not_ready(monkeypatch):
    monkeypatch.setattr(server, "assistant", None)
    client = TestClient(server.app)

    assert client.get("/healthz").status_code == 503


def test_voice_reports_disabled_stt(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    monkeypatch.setattr(server, "stt", None)
    client = TestClient(server.app)

    response = client.post(
        "/api/voice",
        headers={"Authorization": "Bearer secret"},
        files={"file": ("v.wav", wav_bytes(1), "audio/wav")},
    )

    assert response.status_code == 503
    assert "STT_ENABLED" in response.json()["detail"]


def test_exposure_check_blocks_lan_without_token():
    with pytest.raises(RuntimeError, match="API_TOKEN"):
        server._check_exposure(host="0.0.0.0", token="")
    with pytest.raises(RuntimeError, match="API_TOKEN"):
        server._check_exposure(host="192.168.0.10", token="")
    with pytest.raises(RuntimeError, match="API_TOKEN"):
        server._check_exposure(host="", token="")

    server._check_exposure(host="127.0.0.1", token="")
    server._check_exposure(host="localhost", token="")
    server._check_exposure(host="0.0.0.0", token="secret")


def test_access_log_redacts_query_token():
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1234", "GET", "/mcp_endpoint/mcp/?token=secret-123&x=1", "1.1", 200),
        None,
    )

    server._TokenRedactingFilter().filter(record)

    rendered = record.getMessage()
    assert "secret-123" not in rendered
    assert "token=***" in rendered


def test_access_log_redaction_keeps_substrings():
    """`monkey=` 不是 token，别把正常参数也吃掉。"""
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1234", "GET", "/x?monkey=1", "1.1", 200),
        None,
    )

    server._TokenRedactingFilter().filter(record)

    assert "monkey=1" in record.getMessage()


def test_websocket_handshake_log_redaction():
    """WS 握手走 uvicorn.error（MCP 的 token 就在这条 URL 里），同样要打码。"""
    record = logging.LogRecord(
        "uvicorn.error", logging.INFO, __file__, 1,
        '%s - "WebSocket %s" [accepted]',
        ("172.20.0.1:48814", "/mcp_endpoint/mcp/?token=secret-456"),
        None,
    )

    server._TokenRedactingFilter().filter(record)

    rendered = record.getMessage()
    assert "secret-456" not in rendered
    assert "token=***" in rendered


def test_redaction_filter_is_installed_on_both_loggers():
    """只挂 uvicorn.access 会漏掉 WS 握手那行（真实踩过）。"""
    server._install_log_redaction()

    for name in ("uvicorn.access", "uvicorn.error"):
        filters = logging.getLogger(name).filters
        assert any(isinstance(f, server._TokenRedactingFilter) for f in filters), name


# ---------- 网页看板（D82） ----------


class _FakeHome:
    def __init__(self, devices):
        self._devices = devices

    def snapshot(self):
        return {"configured": True, "devices": self._devices}


class _BoardAssistant:
    """只够看板用的一小撮实现：真库 + 假设备/打印机。"""

    def __init__(self):
        self.tasks = Tasks(":memory:")
        self.items = Items(":memory:")
        self.memory = Memory(":memory:")
        self.home = _FakeHome([
            {"entity_id": "sensor.door_state", "name": "门锁状态",
             "state": "已上锁", "unit": "", "domain": "sensor"},
        ])

    def printer_status(self):
        return {"configured": True, "state": "idle"}


def _board_space(assistant_obj):
    return SimpleNamespace(id="default", assistant=assistant_obj, dir=".", print_store=None)



def test_board_collects_today_only(monkeypatch):
    monkeypatch.setattr(server, "_board_cache", {})
    monkeypatch.setattr(server, "_weather_cache", {"at": 0.0, "data": None})
    assistant_obj = _BoardAssistant()
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    assistant_obj.tasks.add("交物业费", due_at=today.isoformat(), mode="soft")
    assistant_obj.tasks.add("下周体检", due_at=(today + timedelta(days=7)).isoformat())
    assistant_obj.items.add("牛奶", expiry_date=(today - timedelta(days=1)).strftime("%Y-%m-%d"))
    assistant_obj.memory.store("酸奶 8月5号过期")

    board = server._board_sync(_board_space(assistant_obj))

    assert board["ok"] is True
    titles = [task["title"] for task in board["today"]["tasks"]]
    assert "交物业费" in titles
    assert "下周体检" not in titles          # 未来的不该占「今天」
    assert board["today"]["tasks"][0]["mode"] == "soft"
    assert board["today"]["items"][0]["name"] == "牛奶"
    assert board["home"]["devices"][0]["name"] == "门锁状态"
    assert board["printer"]["state"] == "idle"
    assert board["recent"]["memories"][0]["text"]


def test_board_cache_can_be_bypassed_after_an_action(monkeypatch):
    monkeypatch.setattr(server, "_board_cache", {})
    monkeypatch.setattr(server, "_weather_cache", {"at": 0.0, "data": None})
    assistant_obj = _BoardAssistant()
    space = _board_space(assistant_obj)
    first = server._board_sync(space)

    assistant_obj.tasks.add("新任务", due_at=datetime.now().isoformat())
    assert server._board_sync(space) is first      # 30 秒内直接吃缓存

    refreshed = server._board_sync(space, force=True)
    assert refreshed is not first
    assert any(task["title"] == "新任务" for task in refreshed["today"]["tasks"])


def test_board_endpoint_returns_payload(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "")
    monkeypatch.setattr(server, "_board_cache", {})
    monkeypatch.setattr(server, "assistant", _BoardAssistant())
    client = TestClient(server.app)

    response = client.get("/api/board?refresh=1")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and "today" in body


def test_weather_cache_refreshes_when_city_changes(monkeypatch):
    """D55：城市是运行时才说的，换城市不能等 10 分钟缓存过期。"""
    class _Weather:
        def __init__(self):
            self.city = "烟台"
            self.calls = 0

        def city_for(self, location=""):
            return self.city

        def snapshot(self):
            self.calls += 1
            return {"ok": True, "city": self.city}

    fake = _Weather()

    class _Assistant:
        weather = fake

    monkeypatch.setattr(server, "assistant", _Assistant())
    monkeypatch.setattr(server, "_weather_cache", {"at": 0.0, "data": None})

    assert server._weather_snapshot()["city"] == "烟台"
    assert server._weather_snapshot()["city"] == "烟台"
    assert fake.calls == 1

    fake.city = "杭州"
    assert server._weather_snapshot()["city"] == "杭州"
    assert fake.calls == 2


def test_display_endpoint_requires_its_own_token(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "")
    monkeypatch.setattr(server, "DISPLAY_TOKEN", "dsecret")
    client = TestClient(server.app)

    assert client.get("/api/display").status_code == 403
    assert client.get(
        "/api/display", headers={"X-Display-Token": "wrong"}
    ).status_code == 403


def test_display_endpoint_disabled_without_token(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "")
    monkeypatch.setattr(server, "DISPLAY_TOKEN", "")
    client = TestClient(server.app)

    assert client.get(
        "/api/display", headers={"X-Display-Token": "x"}
    ).status_code == 503


def test_display_payload_has_weather_and_most_urgent(monkeypatch):
    """待机屏只拿几行字：天气 + 最急的一件事 + 还有几件。"""
    class FakeTasks:
        def get_due(self, days=0):
            return [
                {"title": "取快递", "days_left": -3, "due_at": "2026-11-25T15:00:00"},
                {"title": "交电费", "days_left": 0, "due_at": "2026-11-28T09:00:00"},
            ]

    class FakeItems:
        def get_due(self, days=0):
            return [{"name": "牛奶", "days_left": 0, "expiry_date": "2026-11-28"}]

    class FakeAssistant:
        tasks = FakeTasks()
        items = FakeItems()

    monkeypatch.setattr(server, "API_TOKEN", "")
    monkeypatch.setattr(server, "assistant", FakeAssistant())
    monkeypatch.setattr(server, "DISPLAY_TOKEN", "dsecret")
    monkeypatch.setattr(server, "_weather_snapshot", lambda force=False: {
        "ok": True,
        "city": "烟台",
        "now": {"sky": "晴", "temp": 6, "feels": 4,
                "wind_dir": "东南风", "wind_level": "3级"},
        "days": [{"sky": "多云", "high": 9, "low": -1}],
    })
    client = TestClient(server.app)

    body = client.get(
        "/api/display", headers={"X-Display-Token": "dsecret"}
    ).json()

    assert body["weather"] == {
        "ok": True, "city": "烟台", "sky": "晴", "temp": 6,
        "feels": 4, "wind": "东南风 3级", "high": 9, "low": -1,
    }
    assert body["today"]["count"] == 3
    # 屏幕放得下两条；第三条靠 count 交代
    assert body["today"]["items"] == [
        {"kind": "task", "title": "取快递", "when": "逾期 3 天"},
        {"kind": "task", "title": "交电费", "when": "今天 09:00"},
    ]
    assert body["lunar"].startswith("农历")


def test_display_lunar_line_degrades_when_unavailable(monkeypatch):
    """没装 cnlunar 就不显示农历那一行，不要编一个日子。"""
    from agent import lunar as lunar_mod

    monkeypatch.setattr(lunar_mod, "available", lambda: False)
    assert server._lunar_text(datetime.now()) == ""

    monkeypatch.setattr(lunar_mod, "available", lambda: True)
    text = server._lunar_text(datetime.now())
    assert text.startswith("农历") and "月" in text


def test_weather_endpoint_reports_unconfigured(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "")
    monkeypatch.setattr(server, "_weather_cache", {"at": 0.0, "data": None})

    class _NoWeather:
        weather = None

    monkeypatch.setattr(server, "assistant", _NoWeather())
    client = TestClient(server.app)

    body = client.get("/api/weather").json()

    assert body["ok"] is False
    assert body["error"]

# ---------- D84：对话记录与画像候选 ----------

def test_dialogue_endpoints_show_and_clear(monkeypatch):
    from domain.dialogue import DialogueStore

    monkeypatch.setattr(server, "API_TOKEN", "")
    assistant = make_real_assistant()
    assistant.dialogue = DialogueStore(":memory:", enabled=True)
    monkeypatch.setattr(server, "assistant", assistant)
    client = TestClient(server.app)

    assert client.get("/api/dialogue").json()["count"] == 0
    assistant.dialogue.add("web:1", "user", "我不吃辣")

    body = client.get("/api/dialogue").json()
    assert body["enabled"] is True
    assert body["count"] == 1
    assert body["turns"][0]["text"] == "我不吃辣"

    assert client.delete("/api/dialogue").json()["deleted"] == 1
    assert client.get("/api/dialogue").json()["count"] == 0


def test_profile_suggestion_accept_writes_profile(monkeypatch):
    """网页上点「采纳」就是 D59 要的那声点头：此时才落 用户/... 画像。"""
    monkeypatch.setattr(server, "API_TOKEN", "")
    assistant = make_real_assistant()
    monkeypatch.setattr(server, "assistant", assistant)
    client = TestClient(server.app)
    assistant.proposals.store.record_proposal(
        "profile_set",
        {"subject": "用户", "predicate": "口味", "value": "不吃辣",
         "evidence": "我不吃辣"},
        "proposed", "来自对话提炼",
    )

    items = client.get("/api/profile/suggestions").json()["suggestions"]
    assert len(items) == 1
    proposal_id = items[0]["id"]

    accepted = client.post(
        "/api/profile/suggestions/%d" % proposal_id, json={"action": "accept"}
    )
    assert accepted.status_code == 200
    assert accepted.json()["fact"]["value"] == "不吃辣"

    values = [row["value"] for row in assistant.facts.search(subject="用户")]
    assert values == ["不吃辣"]
    # 提议变成已采纳，不会重复出现；再点一次是 409
    assert client.get("/api/profile/suggestions").json()["suggestions"] == []
    again = client.post(
        "/api/profile/suggestions/%d" % proposal_id, json={"action": "accept"}
    )
    assert again.status_code == 409


def test_profile_suggestion_dismiss_keeps_facts(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "")
    assistant = make_real_assistant()
    monkeypatch.setattr(server, "assistant", assistant)
    client = TestClient(server.app)
    assistant.proposals.store.record_proposal(
        "profile_set",
        {"subject": "用户", "predicate": "口味", "value": "爱吃辣"},
        "proposed", "来自对话提炼",
    )
    proposal_id = client.get("/api/profile/suggestions").json()["suggestions"][0]["id"]

    dismissed = client.post(
        "/api/profile/suggestions/%d" % proposal_id, json={"action": "dismiss"}
    )

    assert dismissed.status_code == 200
    assert assistant.facts.search(subject="用户") == []
    row = assistant.proposals.store.get_proposal(proposal_id)
    assert row["status"] == "cancelled"
