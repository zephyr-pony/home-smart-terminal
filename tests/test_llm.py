"""LLM 请求体构造（不访问网络）。

重点是 reasoning_effort：网关默认会跑思考模式，语音场景下白等好几秒。
"""
import llm


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def make_llm(monkeypatch, captured):
    monkeypatch.setattr(llm, "BASE_URL", "http://example.com/v1")
    monkeypatch.setattr(llm, "API_KEY", "test-key")

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update({"url": url, "body": json})
        return FakeResponse({
            "choices": [{"message": {"role": "assistant", "content": "好"}}]
        })

    monkeypatch.setattr(llm.requests, "post", fake_post)
    return llm.LLM("test-model")


def test_reasoning_effort_sent(monkeypatch):
    captured = {}
    monkeypatch.setattr(llm, "REASONING_EFFORT", "none")
    client = make_llm(monkeypatch, captured)
    client.chat_with_tools([{"role": "user", "content": "hi"}], tools=[])
    assert captured["body"]["reasoning_effort"] == "none"
    assert captured["url"].endswith("/chat/completions")


def test_reasoning_effort_off_omits_field(monkeypatch):
    captured = {}
    monkeypatch.setattr(llm, "REASONING_EFFORT", "off")
    client = make_llm(monkeypatch, captured)
    client.chat_with_tools([{"role": "user", "content": "hi"}], tools=[])
    assert "reasoning_effort" not in captured["body"]


def test_reasoning_effort_custom_level(monkeypatch):
    captured = {}
    monkeypatch.setattr(llm, "REASONING_EFFORT", "low")
    client = make_llm(monkeypatch, captured)
    client.chat_with_tools([{"role": "user", "content": "hi"}], tools=[])
    assert captured["body"]["reasoning_effort"] == "low"
