"""MCP 接入点探针：模拟小智服务端，验 `_meta` 能不能带到工具里。

小智那侧是 MCP 客户端（见 xiaozhi-esp32-server 的
`core/providers/tools/mcp_endpoint/mcp_endpoint_handler.py`），
出站 WebSocket + 裸 JSON-RPC，id 固定用 1(initialize)/2(tools/list)，
`tools/call` 从 3 开始；身份放 `params._meta`。这里按同样的形状发一遍。

用法：
    python scripts/mcp_probe.py --token <MCP_TOKEN>
    python scripts/mcp_probe.py --token <...> --text "明天提醒我交电费" \
        --speaker-id spk-dad --speaker-name 爸爸

服务端日志里应该出现：
    🔊 MCP 声纹身份：speakerId=spk-dad name=爸爸
"""
import argparse
import asyncio
import json
import sys

import aiohttp

DEFAULT_URL = "http://127.0.0.1:8200/mcp_endpoint/mcp/"


def ws_url(base):
    if base.startswith("ws://") or base.startswith("wss://"):
        return base
    return base.replace("http://", "ws://").replace("https://", "wss://")


async def rpc(ws, payload, expect_id):
    await ws.send_str(json.dumps(payload, ensure_ascii=False))
    while True:
        raw = await ws.receive(timeout=60)
        if raw.type != aiohttp.WSMsgType.TEXT:
            raise RuntimeError("连接异常：%s" % raw.type)
        message = json.loads(raw.data)
        if message.get("id") == expect_id:
            return message


async def run(args):
    base = ws_url(args.url)
    if not base.endswith("/"):
        base += "/"
    url = "%s?token=%s" % (base, args.token)
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(url) as ws:
            init = await rpc(ws, {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"roots": {"listChanged": True}, "sampling": {}},
                    "clientInfo": {"name": "XiaozhiMCPEndpointClient", "version": "1.0.0"},
                },
            }, 1)
            print("① initialize → %s" % json.dumps(init.get("result"), ensure_ascii=False))

            await ws.send_str(json.dumps({
                "jsonrpc": "2.0", "method": "notifications/initialized", "params": {},
            }))

            listing = await rpc(ws, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, 2)
            names = [tool.get("name") for tool in listing["result"]["tools"]]
            print("② tools/list → %s" % ", ".join(names))

            params = {"name": "ask_xiaoma", "arguments": {"text": args.text}}
            if args.speaker_id or args.speaker_name:
                meta = {}
                if args.speaker_id:
                    meta["speakerId"] = args.speaker_id
                if args.speaker_name:
                    meta["speakerName"] = args.speaker_name
                params["_meta"] = meta

            call = await rpc(ws, {
                "jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": params,
            }, 3)
            result = call.get("result") or {}
            text = ""
            content = result.get("content") or []
            if content and isinstance(content[0], dict):
                text = content[0].get("text") or ""
            print("③ tools/call → %s" % text)
            if result.get("isError"):
                print("   （工具返回错误）")
                return 1
            print()
            print("如果服务端日志里出现「🔊 MCP 声纹身份」，说明 _meta 透传成功。")
            return 0


def main():
    parser = argparse.ArgumentParser(description="小智 MCP 接入点探针")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--token", required=True)
    parser.add_argument("--text", default="你好")
    parser.add_argument("--speaker-id", default="")
    parser.add_argument("--speaker-name", default="")
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
