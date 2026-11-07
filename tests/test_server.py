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
