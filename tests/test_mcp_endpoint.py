"""MCP 接入点：小智的 tools/call 能不能把 _meta（声纹身份）带到工具里。

协议形状以小智源码为准（core/providers/tools/mcp_endpoint/）：
WebSocket + 裸 JSON-RPC，身份放 params._meta，工具返回读 content[0].text。
"""
import json

import pytest
from fastapi.testclient import TestClient

import server
from adapters.mcp_endpoint import MCPEndpoint, speaker_from_meta


def _endpoint(calls=None):
    calls = calls if calls is not None else []

    def handler(text, speaker):
        calls.append((text, speaker))
        return "收到：%s" % text

    return MCPEndpoint(handler), calls


def test_initialize_reports_tools_capability():
    endpoint, _ = _endpoint()

    response = endpoint.handle_message({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05"},
    })

    assert response["result"]["protocolVersion"] == "2024-11-05"
    assert "tools" in response["result"]["capabilities"]
    assert response["result"]["serverInfo"]["name"] == "xiaoma"


def test_tools_list_exposes_ask_xiaoma():
    endpoint, _ = _endpoint()

    response = endpoint.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})

    tools = response["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["ask_xiaoma"]
    assert tools[0]["inputSchema"]["required"] == ["text"]


def test_notifications_get_no_response():
    endpoint, _ = _endpoint()

    assert endpoint.handle_message({
        "jsonrpc": "2.0", "method": "notifications/initialized", "params": {},
    }) is None


def test_tools_call_passes_speaker_meta_to_handler():
    """这条就是整个声纹方案的前置：_meta 必须原样到达工具。"""
    endpoint, calls = _endpoint()

    response = endpoint.handle_message({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {
            "name": "ask_xiaoma",
            "arguments": {"text": "明天提醒我交电费"},
            "_meta": {"speakerId": "spk-dad", "speakerName": "爸爸"},
        },
    })

    assert calls == [("明天提醒我交电费", {"speaker_id": "spk-dad", "name": "爸爸"})]
    assert endpoint.last_speaker == {"speaker_id": "spk-dad", "name": "爸爸"}
    # 小智读的是 content[0].text
    assert response["result"]["content"][0]["text"] == "收到：明天提醒我交电费"
    assert "isError" not in response["result"]


def test_tools_call_without_meta_is_unknown_speaker():
    endpoint, calls = _endpoint()

    endpoint.handle_message({
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "ask_xiaoma", "arguments": {"text": "关灯"}},
    })

    assert calls == [("关灯", None)]
    assert endpoint.last_speaker is None


def test_speaker_meta_without_name_is_still_an_id():
    assert speaker_from_meta({"speakerId": "spk-1"}) == {
        "speaker_id": "spk-1", "name": "",
    }
    assert speaker_from_meta({}) is None
    assert speaker_from_meta(None) is None


def test_unknown_tool_is_a_jsonrpc_error():
    endpoint, _ = _endpoint()

    response = endpoint.handle_message({
        "jsonrpc": "2.0", "id": 5, "method": "tools/call",
        "params": {"name": "rm_rf", "arguments": {}},
    })

    assert response["error"]["code"] == -32602


def test_empty_text_returns_tool_error_not_exception():
    endpoint, calls = _endpoint()

    response = endpoint.handle_message({
        "jsonrpc": "2.0", "id": 6, "method": "tools/call",
        "params": {"name": "ask_xiaoma", "arguments": {}},
    })

    assert response["result"]["isError"] is True
    assert calls == []


def test_handler_exception_becomes_tool_error():
    def boom(_text, _speaker):
        raise RuntimeError("炸弹")

    endpoint = MCPEndpoint(boom)

    response = endpoint.handle_message({
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {"name": "ask_xiaoma", "arguments": {"text": "你好"}},
    })

    assert response["result"]["isError"] is True


# ---------- 服务端接线 ----------

class FakeAssistant:
    def __init__(self):
        self.calls = []

    def process_text(self, text, client_id=None):
        self.calls.append((text, client_id))
        return {"type": "chat", "reply": "我在"}


def test_websocket_flow_carries_meta(monkeypatch):
    fake = FakeAssistant()
    monkeypatch.setattr(server, "assistant", fake)
    monkeypatch.setattr(server, "MCP_TOKEN", "t0ken")

    client = TestClient(server.app)
    with client.websocket_connect("/mcp_endpoint/mcp/?token=t0ken") as ws:
        ws.send_text(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        }))
        assert ws.receive_json()["result"]["protocolVersion"] == "2024-11-05"

        ws.send_text(json.dumps({
            "jsonrpc": "2.0", "method": "notifications/initialized", "params": {},
        }))
        ws.send_text(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
        assert ws.receive_json()["result"]["tools"][0]["name"] == "ask_xiaoma"

        ws.send_text(json.dumps({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {
                "name": "ask_xiaoma",
                "arguments": {"text": "我是谁"},
                "_meta": {"speakerId": "spk-mom", "speakerName": "妈妈"},
            },
        }))
        assert ws.receive_json()["result"]["content"][0]["text"] == "我在"

    # 声纹只用来分会话：同一个 speakerId 落到同一个 client_id
    assert fake.calls == [("我是谁", "mcp:spk-mom")]


def test_websocket_without_meta_uses_device_session(monkeypatch):
    fake = FakeAssistant()
    monkeypatch.setattr(server, "assistant", fake)
    monkeypatch.setattr(server, "MCP_TOKEN", "t0ken")

    client = TestClient(server.app)
    with client.websocket_connect("/mcp_endpoint/mcp/?token=t0ken") as ws:
        ws.send_text(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "ask_xiaoma", "arguments": {"text": "你好"}},
        }))
        ws.receive_json()

    assert fake.calls == [("你好", "mcp:device")]


def test_websocket_rejects_wrong_token(monkeypatch):
    monkeypatch.setattr(server, "MCP_TOKEN", "t0ken")

    client = TestClient(server.app)
    with pytest.raises(Exception):
        with client.websocket_connect("/mcp_endpoint/mcp/?token=wrong"):
            pass


def test_websocket_is_closed_when_token_not_configured(monkeypatch):
    monkeypatch.setattr(server, "MCP_TOKEN", "")

    client = TestClient(server.app)
    with pytest.raises(Exception):
        with client.websocket_connect("/mcp_endpoint/mcp/?token="):
            pass
