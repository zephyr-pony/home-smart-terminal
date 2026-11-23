"""MCP 接入点：把小马作为 MCP 服务端暴露给 xiaozhi-server。

小智那边是 **MCP 客户端**（`core/providers/tools/mcp_endpoint/`）：出站连到
`ws://<小马>/mcp_endpoint/mcp/?token=...`，用裸 JSON-RPC 2.0 走 initialize →
tools/list → tools/call。它的 `tools/call` 里会带：

    params._meta = {"speakerId": "...", "speakerName": "..."}

声纹身份就藏在这个字段里（未命中时整个 `_meta` 不出现）。身份是**不可信输入**：
这里只把它取出来交给调用方，是否采信由业务层决定，绝不参与鉴权。

不用官方 MCP SDK：小智的传输是自带 WebSocket + 裸 JSON-RPC，SDK 反而要垫一层；
这里只实现它实际用到的几个方法，行为以它的源码为准。
"""
import asyncio
import json
import traceback

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "xiaoma", "version": "0.1.0"}

_TOOL_DEFINITIONS = [
    {
        "name": "ask_xiaoma",
        "description": (
            "把用户的话交给小马（家庭助理）：记事情、查清单、设提醒、"
            "查设备状态。text 用用户原话，不要改写。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "用户原话"},
            },
            "required": ["text"],
        },
    },
]


def speaker_from_meta(meta):
    """从 params._meta 取声纹身份；没有就是 None。

    字段名跟小智的 `mcp_speaker.py` 对齐，改名要两边一起改。
    """
    if not isinstance(meta, dict):
        return None
    speaker_id = str(meta.get("speakerId") or "").strip()
    name = str(meta.get("speakerName") or "").strip()
    if not speaker_id and not name:
        return None
    return {"speaker_id": speaker_id, "name": name}


class MCPEndpoint:
    """MCP 接入点：一条工具 ask_xiaoma，一个同步回调。

    handler(text, speaker) 返回要念给用户的文本；speaker 为 None 表示没认出来。
    """

    def __init__(self, handler):
        self.handler = handler
        # 最近一次 tools/call 带来的身份，探针和测试用它核对 _meta 有没有到
        self.last_speaker = None

    # ---------- JSON-RPC ----------

    def handle_message(self, payload):
        """处理一条 JSON-RPC 消息。通知返回 None（不回包）。"""
        if not isinstance(payload, dict):
            return _error(None, -32600, "Invalid Request")

        method = payload.get("method")
        msg_id = payload.get("id")
        params = payload.get("params") or {}

        # 通知（notifications/initialized 等）没有 id，按协议不回
        if method and str(method).startswith("notifications/"):
            return None
        if method == "initialize":
            return _result(msg_id, {
                "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            })
        if method == "ping":
            return _result(msg_id, {})
        if method == "tools/list":
            return _result(msg_id, {"tools": _TOOL_DEFINITIONS})
        if method == "tools/call":
            return self._call_tool(msg_id, params)
        return _error(msg_id, -32601, "Method not found: %s" % method)

    def _call_tool(self, msg_id, params):
        if not isinstance(params, dict):
            return _error(msg_id, -32602, "Invalid params")
        name = str(params.get("name") or "")
        if name != "ask_xiaoma":
            return _error(msg_id, -32602, "Unknown tool: %s" % name)

        arguments = params.get("arguments") or {}
        text = str(arguments.get("text") or "").strip()
        if not text:
            return _result(msg_id, _text_result("要说什么？", is_error=True))

        speaker = speaker_from_meta(params.get("_meta"))
        self.last_speaker = speaker
        if speaker:
            print(
                "🔊 MCP 声纹身份：speakerId=%s name=%s"
                % (speaker.get("speaker_id") or "-", speaker.get("name") or "-")
            )
        try:
            reply = self.handler(text, speaker)
        except Exception:
            traceback.print_exc()
            return _result(msg_id, _text_result("小马这边出错了，稍后再试。", is_error=True))
        return _result(msg_id, _text_result(str(reply or "")))

    # ---------- WebSocket ----------

    async def handle_ws(self, websocket):
        """FastAPI WebSocket 会话（鉴权由调用方在 accept 前做）。"""
        await websocket.accept()
        while True:
            try:
                raw = await websocket.receive_text()
            except Exception:
                break
            try:
                payload = json.loads(raw)
            except ValueError:
                await websocket.send_text(json.dumps(
                    _error(None, -32700, "Parse error"), ensure_ascii=False
                ))
                continue
            # 工具执行是同步的（Assistant 会跑 LLM），丢到线程里别堵事件循环
            response = await asyncio.to_thread(self.handle_message, payload)
            if response is not None:
                await websocket.send_text(json.dumps(response, ensure_ascii=False))


def _result(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _text_result(text, is_error=False):
    """小智读的是 content[0].text（见 mcp_endpoint_handler.py）。"""
    payload = {"content": [{"type": "text", "text": text}]}
    if is_error:
        payload["isError"] = True
    return payload
