import io
import wave

from fastapi.testclient import TestClient

import server


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


class FakeTTS:
    async def synthesize_bytes(self, reply):
        return b"audio"


class FakeAssistant:
    def __init__(self):
        self.memory = FakeMemory()
        self.items = FakeItems()
        self.sessions = {}

    def process_text(self, text, client_id):
        return {
            "type": "chat", "reply": text, "memories": [],
            "used_tools": [], "domain": "chat", "action": "chat",
        }


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
