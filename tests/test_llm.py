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


class FakeStreamResponse:
    def __init__(self, lines):
        self._lines = lines
        self.status_code = 200

    def raise_for_status(self):
        return None

    def iter_lines(self, decode_unicode=False):
        return [line.encode("utf-8") for line in self._lines]


def patch_stream(monkeypatch, lines, captured):
    monkeypatch.setattr(llm, "BASE_URL", "http://example.com/v1")
    monkeypatch.setattr(llm, "API_KEY", "k")

    def fake_post(url, json=None, headers=None, timeout=None, stream=False):
        captured["body"] = json
        return FakeStreamResponse(lines)

    monkeypatch.setattr(llm.requests, "post", fake_post)
    return llm.LLM("test-model")


def test_stream_accumulates_content_and_calls_back(monkeypatch):
    captured = {}
    pieces = []
    client = patch_stream(monkeypatch, [
        'data: {"choices":[{"delta":{"content":"你好"}}]}',
        'data: {"choices":[{"delta":{"content":"呀"}}]}',
        "data: [DONE]",
    ], captured)

    data = client.chat_with_tools_stream(
        [{"role": "user", "content": "hi"}], tools=[], on_delta=pieces.append
    )
    assert pieces == ["你好", "呀"]
    assert data["choices"][0]["message"]["content"] == "你好呀"
    assert data["choices"][0]["finish_reason"] == "stop"
    assert captured["body"]["stream"] is True


def test_stream_assembles_tool_calls(monkeypatch):
    captured = {}
    client = patch_stream(monkeypatch, [
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1",'
        '"function":{"name":"memory_search","arguments":"{\\"key"}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
        '"function":{"arguments":"words\\":\\"牛奶\\"}"}}]}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    ], captured)

    data = client.chat_with_tools_stream(
        [{"role": "user", "content": "hi"}], tools=[], on_delta=lambda _p: None
    )
    message = data["choices"][0]["message"]
    assert data["choices"][0]["finish_reason"] == "tool_calls"
    assert message["tool_calls"][0]["function"]["name"] == "memory_search"
    assert message["tool_calls"][0]["function"]["arguments"] == '{"keywords":"牛奶"}'
