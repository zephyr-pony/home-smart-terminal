"""假设备：不用 ESP32 也能把「摄像头」整条链跑一遍。

扮演一台带摄像头的终端连到 xiaozhi-server：
1. hello 时声明支持 MCP
2. 按 MCP 协议应答 initialize / tools/list，自报一个 self.camera.take_photo
3. 用「文字」当用户输入（不走麦克风），说一句「看看桌上有什么」
4. 收到工具调用就把测试图 POST 到 server.vision_explain（和固件一模一样）
5. 把视觉端点返回的 JSON 原样当工具结果交回去，打印服务端念出来的话

验的是：小马 /v1 回的 tool_calls 名字对不对、小智能不能驱动设备、视觉结果能不能被播报。

用法：
    python scripts/fake_camera_device.py --image C:/tmp/table.jpg
    python scripts/fake_camera_device.py --text "看看桌上有什么" --image C:/tmp/table.jpg
"""
import argparse
import asyncio
import json
import os
import sys
import time
from urllib.parse import urlsplit

import requests
import websockets

DEFAULT_WS = "ws://127.0.0.1:8400/xiaozhi/v1/"
DEVICE_ID = "aa:bb:cc:dd:ee:01"
CLIENT_ID = "fake-camera-01"
DEFAULT_TEXT = "看看桌上有什么"

TOOL = {
    "name": "self.camera.take_photo",
    "description": (
        "Always remember you have a camera. If the user asks you to see something, "
        "use this tool to take a photo and then explain it."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "想确认什么"},
        },
        "required": ["question"],
    },
}


class FakeCamera:
    def __init__(self, ws_url, text, image_path, timeout):
        self.ws_url = ws_url
        self.text = text
        self.image = open(image_path, "rb").read()
        self.timeout = timeout
        self.vision = {}
        self.stt = ""
        self.spoken = []
        self.called = False
        self.result_text = ""
        self.said = False

    async def reply(self, ws, msg_id, result):
        await ws.send(json.dumps({
            "type": "mcp",
            "payload": {"jsonrpc": "2.0", "id": msg_id, "result": result},
        }))

    def explain(self, question):
        """和固件一样：把图 POST 到服务端下发的视觉地址，原样返回它的 JSON。

        探针和服务器在同一台机器上，Docker Desktop 发布的端口有时只对 loopback
        生效（从本机访问自己的 LAN IP 会超时，别的设备却是通的），所以按顺序试。
        """
        url = self.vision.get("url") or ""
        token = self.vision.get("token") or ""
        if not url:
            return json.dumps({"success": False, "message": "服务端没下发 vision 地址"})

        candidates = [url]
        host = urlsplit(url).hostname
        if host and host not in ("127.0.0.1", "localhost"):
            candidates.append(url.replace("//" + host, "//127.0.0.1", 1))

        last_error = ""
        for candidate in candidates:
            started = time.time()
            try:
                resp = requests.post(
                    candidate,
                    headers={"Authorization": "Bearer " + token, "Device-Id": DEVICE_ID},
                    files=[
                        ("question", (None, question or "看看画面里有什么")),
                        ("file", ("photo.jpg", self.image, "image/jpeg")),
                    ],
                    # 连接超时给短一点：本机访问自己的 LAN IP 会一直挂到系统超时（~21 s），
                    # 挂久了会撞上小智 30 s 的工具调用超时
                    timeout=(3, 60),
                )
                print("   [设备] POST %s %.1fs -> %s" % (
                    candidate, time.time() - started, resp.text[:120]))
                return resp.text
            except requests.RequestException as exc:
                last_error = str(exc)[:120]
                print("   [设备] %s 不通: %s" % (candidate, last_error))
        return json.dumps({"success": False, "message": last_error}, ensure_ascii=False)

    async def on_mcp(self, ws, payload):
        method = payload.get("method")
        msg_id = payload.get("id")
        params = payload.get("params") or {}

        if method == "initialize":
            caps = params.get("capabilities") or {}
            self.vision = caps.get("vision") or {}
            print("   [MCP] initialize，视觉地址: %s" % (self.vision.get("url") or "无"))
            await self.reply(ws, msg_id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-camera", "version": "0.1"},
            })
        elif method == "tools/list":
            print("   [MCP] tools/list -> 报一个 take_photo")
            await self.reply(ws, msg_id, {"tools": [TOOL]})
            if not self.said:
                self.said = True
                asyncio.create_task(self.say(ws))
        elif method == "tools/call":
            name = params.get("name") or ""
            args = params.get("arguments") or {}
            print("   [MCP] tools/call %s %s" % (name, args))
            if name == "self.camera.take_photo":
                self.called = True
                self.result_text = self.explain(args.get("question"))
                await self.reply(ws, msg_id, {
                    "content": [{"type": "text", "text": self.result_text}]
                })
            else:
                await self.reply(ws, msg_id, {
                    "content": [{"type": "text", "text": "这个假设备没有实现该工具"}]
                })

    async def say(self, ws):
        await asyncio.sleep(0.8)  # 等 tools/list 落库
        print("   [用户] %s" % self.text)
        await ws.send(json.dumps({
            "type": "listen",
            "state": "detect",
            "text": self.text,
        }))

    async def run(self):
        headers = {
            "device-id": DEVICE_ID,
            "client-id": CLIENT_ID,
            "protocol-version": "1",
        }
        try:
            ws_cm = websockets.connect(self.ws_url, max_size=None, additional_headers=headers)
        except TypeError:  # websockets < 14
            ws_cm = websockets.connect(self.ws_url, max_size=None, extra_headers=headers)

        async with ws_cm as ws:
            await ws.send(json.dumps({
                "type": "hello",
                "version": 1,
                "transport": "websocket",
                "features": {"mcp": True},
                "audio_params": {
                    "format": "opus",
                    "sample_rate": 16000,
                    "channels": 1,
                    "frame_duration": 60,
                },
            }))
            deadline = asyncio.get_event_loop().time() + self.timeout
            while asyncio.get_event_loop().time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=15)
                except asyncio.TimeoutError:
                    break
                if isinstance(raw, bytes):
                    continue
                msg = json.loads(raw)
                kind = msg.get("type")
                if kind == "mcp":
                    await self.on_mcp(ws, msg.get("payload") or {})
                elif kind == "stt":
                    self.stt = msg.get("text", "")
                    print("   [识别] %s" % self.stt)
                elif kind == "tts":
                    state = msg.get("state")
                    if state == "sentence_start":
                        self.spoken.append(msg.get("text", ""))
                        print("   [播报] %s" % msg.get("text", ""))
                    elif state == "stop":
                        break
                elif kind == "llm":
                    print("   [表情] %s" % msg.get("emotion", ""))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ws", default=DEFAULT_WS)
    parser.add_argument("--text", default=DEFAULT_TEXT)
    parser.add_argument("--image", required=True,
                        help="一张测试图（JPEG/PNG），模拟摄像头拍到的东西")
    parser.add_argument("--timeout", type=int, default=90)
    args = parser.parse_args()

    if not os.path.exists(args.image):
        raise SystemExit("找不到图片：%s" % args.image)

    device = FakeCamera(args.ws, args.text, args.image, args.timeout)
    asyncio.run(device.run())

    print()
    print("照片工具被调用: %s" % ("是" if device.called else "否"))
    print("工具结果: %s" % (device.result_text[:160] or "无"))
    print("播报: %s" % (" / ".join(device.spoken) or "无"))
    ok = device.called and bool(device.spoken)
    print("结果: %s" % ("通过" if ok else "未通过"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
