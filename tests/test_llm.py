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

    def fake_post(url, json=None, headers=None, timeout=None, **kwargs):
        captured.update({"url": url, "body": json,
                         "proxies": kwargs.get("proxies")})
        return FakeResponse({
            "choices": [{"message": {"role": "assistant", "content": "好"}}]
        })

    monkeypatch.setattr(llm.requests, "post", fake_post)
    return llm.LLM("test-model")


def test_reasoning_effort_off_sent(monkeypatch):
    """commandcode 用 off 关思考：必须发出去，不能当成「不传」。"""
    captured = {}
    monkeypatch.setattr(llm, "REASONING_EFFORT", "off")
    client = make_llm(monkeypatch, captured)
    client.chat_with_tools([{"role": "user", "content": "hi"}], tools=[])
    assert captured["body"]["reasoning_effort"] == "off"
    assert captured["url"].endswith("/chat/completions")


def test_reasoning_effort_none_passthrough(monkeypatch):
    """aisp 的枚举是 none：原样透传，不做归一化。"""
    captured = {}
    monkeypatch.setattr(llm, "REASONING_EFFORT", "none")
    client = make_llm(monkeypatch, captured)
    client.chat_with_tools([{"role": "user", "content": "hi"}], tools=[])
    assert captured["body"]["reasoning_effort"] == "none"


def test_reasoning_effort_empty_omits_field(monkeypatch):
    captured = {}
    monkeypatch.setattr(llm, "REASONING_EFFORT", "")
    client = make_llm(monkeypatch, captured)
    client.chat_with_tools([{"role": "user", "content": "hi"}], tools=[])
    assert "reasoning_effort" not in captured["body"]


def test_reasoning_effort_custom_level(monkeypatch):
    captured = {}
    monkeypatch.setattr(llm, "REASONING_EFFORT", "low")
    client = make_llm(monkeypatch, captured)
    client.chat_with_tools([{"role": "user", "content": "hi"}], tools=[])
    assert captured["body"]["reasoning_effort"] == "low"


def test_model_override(monkeypatch):
    """决策轮可以换模型：请求体里的 model 跟着变，其他参数不变。"""
    captured = {}
    client = make_llm(monkeypatch, captured)
    client.chat_with_tools([{"role": "user", "content": "hi"}], tools=[],
                           model="fast/model")
    assert captured["body"]["model"] == "fast/model"


def test_build_decision_llm_none_when_unset(monkeypatch):
    monkeypatch.setattr(llm, "LLM_MODEL_DECISION", "")
    assert llm.build_decision_llm() is None


def test_build_decision_llm_none_when_same_model(monkeypatch):
    monkeypatch.setattr(llm, "LLM_MODEL_DECISION", "same/model")
    monkeypatch.setattr(llm, "MODEL_NAME", "same/model")
    assert llm.build_decision_llm() is None


def test_build_decision_llm_uses_decision_model(monkeypatch):
    monkeypatch.setattr(llm, "BASE_URL", "http://example.com/v1")
    monkeypatch.setattr(llm, "API_KEY", "k")
    monkeypatch.setattr(llm, "LLM_MODEL_DECISION", "fast/model")
    monkeypatch.setattr(llm, "MODEL_NAME", "slow/model")
    client = llm.build_decision_llm()
    assert client.model_name == "fast/model"


def test_chat_template_kwargs_sent(monkeypatch):
    """llama.cpp / vLLM 用模板参数关思考：这种网关不认 reasoning_effort，留空。"""
    captured = {}
    monkeypatch.setattr(llm, "REASONING_EFFORT", "")
    monkeypatch.setattr(llm, "CHAT_TEMPLATE_KWARGS", {"enable_thinking": False})
    client = make_llm(monkeypatch, captured)
    client.chat_with_tools([{"role": "user", "content": "hi"}], tools=[])
    assert captured["body"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert "reasoning_effort" not in captured["body"]


def test_http_error_carries_gateway_body(monkeypatch):
    """换网关时最需要看到的是服务端说哪个字段不对，不能只报 400。"""
    captured = {}
    monkeypatch.setattr(llm, "BASE_URL", "http://example.com/v1")
    monkeypatch.setattr(llm, "API_KEY", "k")
    monkeypatch.setattr(llm, "REASONING_EFFORT", "off")

    class Resp:
        status_code = 400
        text = '{"error":{"message":"Model \\"X\\" is not supported"}}'

    def fake_post(url, json=None, headers=None, timeout=None, **kwargs):
        err = llm.requests.HTTPError("400 Client Error")
        err.response = Resp()
        raise err

    monkeypatch.setattr(llm.requests, "post", fake_post)
    client = llm.LLM("test-model")
    try:
        client.chat_with_tools([{"role": "user", "content": "hi"}], tools=[], retries=0)
    except RuntimeError as exc:
        assert "HTTP 400" in str(exc)
        assert "not supported" in str(exc)
    else:
        raise AssertionError("应当抛 RuntimeError")


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

    def fake_post(url, json=None, headers=None, timeout=None, stream=False, **kwargs):
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


def test_proxy_passed_when_configured(monkeypatch):
    """直连 65 s、走本机代理 4.7 s：配了 LLM_PROXY 就必须把 proxies 传给 requests。"""
    captured = {}
    monkeypatch.setattr(llm, "REASONING_EFFORT", "")
    monkeypatch.setattr(llm, "LLM_PROXY", "http://127.0.0.1:10808")
    client = make_llm(monkeypatch, captured)
    client.chat_with_tools([{"role": "user", "content": "hi"}], tools=[])
    assert captured["proxies"] == {
        "http": "http://127.0.0.1:10808",
        "https": "http://127.0.0.1:10808",
    }


def test_proxy_untouched_when_unset(monkeypatch):
    """没配就让 requests 走默认（系统代理/直连），不要强塞一个空 dict。"""
    captured = {}
    monkeypatch.setattr(llm, "REASONING_EFFORT", "")
    monkeypatch.setattr(llm, "LLM_PROXY", "")
    client = make_llm(monkeypatch, captured)
    client.chat_with_tools([{"role": "user", "content": "hi"}], tools=[])
    assert captured["proxies"] is None
