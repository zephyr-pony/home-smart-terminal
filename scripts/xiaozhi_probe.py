"""小智协议端到端探针（无需硬件）。

用途：在没有 ESP32 的情况下验证
    语音 → xiaozhi-server(VAD/ASR) → 本机 /v1/chat/completions → TTS

做法：用 Edge TTS 合成一句"用户说的话"，用 ffmpeg 转成 16k Opus，
按协议（v1，裸 Opus 包）通过 WebSocket 推给 xiaozhi-server，再打印
服务端返回的 STT 文本与 TTS 句子。

前置：
    - xiaozhi-server 已启动（默认 ws://127.0.0.1:8300/xiaozhi/v1/）
    - 本机 server.py 已启动（/api/tts 与 /v1/chat/completions 可用）
    - 系统已安装 ffmpeg（winget install Gyan.FFmpeg）

用法：
    python scripts/xiaozhi_probe.py "明天提醒我交电费"
    python scripts/xiaozhi_probe.py --ws ws://192.168.2.40:8300/xiaozhi/v1/ "..."
"""
import argparse
import asyncio
import json
import os
import struct
import subprocess
import sys
import tempfile
import time

import requests
import websockets

DEFAULT_WS = "ws://127.0.0.1:8300/xiaozhi/v1/"
DEFAULT_API = "http://127.0.0.1:8200"
FRAME_MS = 60
SAMPLE_RATE = 16000


def iter_ogg_packets(data):
    """从 Ogg 流里拆出每个 Opus 包（按 lacing 规则）。"""
    packets = []
    current = bytearray()
    pos = 0
    while True:
        start = data.find(b"OggS", pos)
        if start < 0:
            break
        if start + 27 > len(data):
            break
        segments = data[start + 26]
        table_end = start + 27 + segments
        if table_end > len(data):
            break
        table = data[start + 27:table_end]
        body_pos = table_end
        for size in table:
            current.extend(data[body_pos:body_pos + size])
            body_pos += size
            if size < 255:
                if current:
                    packets.append(bytes(current))
                current = bytearray()
        pos = body_pos
    return packets


def synth_user_audio(text, api_base, token):
    """用本机 TTS 合成"用户的话"，再转成 16k Opus（不经扬声器）。"""
    response = requests.post(
        api_base.rstrip("/") + "/api/tts",
        json={"text": text},
        headers={"Authorization": "Bearer " + token} if token else {},
        timeout=60,
    )
    response.raise_for_status()

    tmp = tempfile.mkdtemp(prefix="xz_probe_")
    mp3_path = os.path.join(tmp, "in.mp3")
    ogg_path = os.path.join(tmp, "in.ogg")
    with open(mp3_path, "wb") as handle:
        handle.write(response.content)

    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", mp3_path,
            "-ar", str(SAMPLE_RATE), "-ac", "1",
            "-c:a", "libopus", "-b:a", "24k",
            "-frame_duration", str(FRAME_MS),
            "-f", "ogg", ogg_path,
        ],
        check=True,
    )
    with open(ogg_path, "rb") as handle:
        packets = iter_ogg_packets(handle.read())
    # 前两个包是 OpusHead / OpusTags
    return [p for p in packets if not p.startswith(b"OpusHead")
            and not p.startswith(b"OpusTags")]


async def run_probe(ws_url, text, api_base, token, timeout=60):
    packets = synth_user_audio(text, api_base, token)
    print("上行音频包: %d 个（%.1f 秒）" % (
        len(packets), len(packets) * FRAME_MS / 1000.0))

    stt_text = ""
    replies = []
    audio_bytes = 0

    # xiaozhi-server 靠握手头识别设备，缺 device-id 会直接断开
    headers = {
        "device-id": "aa:bb:cc:dd:ee:ff",
        "client-id": "probe-client-0001",
        "protocol-version": "1",
    }
    if token:
        headers["authorization"] = "Bearer " + token
    try:
        ws_cm = websockets.connect(
            ws_url, max_size=None, additional_headers=headers
        )
    except TypeError:  # websockets < 14
        ws_cm = websockets.connect(ws_url, max_size=None, extra_headers=headers)

    async with ws_cm as ws:
        await ws.send(json.dumps({
            "type": "hello",
            "version": 1,
            "transport": "websocket",
            "audio_params": {
                "format": "opus",
                "sample_rate": SAMPLE_RATE,
                "channels": 1,
                "frame_duration": FRAME_MS,
            },
        }))
        hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        session_id = hello.get("session_id", "")
        print("会话建立: %s（下行 %s Hz）" % (
            session_id,
            (hello.get("audio_params") or {}).get("sample_rate", "?")))

        await ws.send(json.dumps({
            "session_id": session_id,
            "type": "listen",
            "state": "start",
            "mode": "manual",
        }))
        for packet in packets:
            await ws.send(packet)
            await asyncio.sleep(FRAME_MS / 1000.0)
        await ws.send(json.dumps({
            "session_id": session_id,
            "type": "listen",
            "state": "stop",
            "mode": "manual",
        }))

        marks = {}
        t_start = time.monotonic()
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                message = await asyncio.wait_for(ws.recv(), timeout=25)
            except asyncio.TimeoutError:
                break
            if isinstance(message, bytes):
                audio_bytes += len(message)
                continue
            event = json.loads(message)
            kind = event.get("type")
            if kind == "stt":
                stt_text = event.get("text", "")
                marks["stt"] = (time.monotonic() - t_start) * 1000
                print("识别: %s（%.0f ms）" % (stt_text, marks["stt"]))
            elif kind == "tts":
                state = event.get("state")
                if state == "sentence_start":
                    if "first_tts" not in marks:
                        marks["first_tts"] = (time.monotonic() - t_start) * 1000
                    replies.append(event.get("text", ""))
                    print("回答: %s" % event.get("text", ""))
                elif state == "stop":
                    marks["done"] = (time.monotonic() - t_start) * 1000
                    break
            elif kind == "llm":
                print("表情: %s" % event.get("emotion", ""))

    print("下行音频: %d 字节" % audio_bytes)
    if marks:
        print("耗时: 识别 %.0f ms | 首句 %.0f ms | 完成 %.0f ms" % (
            marks.get("stt", 0), marks.get("first_tts", 0), marks.get("done", 0)))
    ok = bool(stt_text and replies)
    print("结果: %s" % ("通过" if ok else "未通过（没有识别或没有回答）"))
    return ok


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("text", nargs="?", default="明天提醒我交电费")
    parser.add_argument("--ws", default=DEFAULT_WS)
    parser.add_argument("--api", default=DEFAULT_API)
    parser.add_argument("--timeout", type=int, default=90)
    args = parser.parse_args()

    token = os.environ.get("API_TOKEN", "")
    if not token:
        print("提示：没设置 API_TOKEN，本机 /api/tts 会返回 401", file=sys.stderr)

    ok = asyncio.run(run_probe(args.ws, args.text, args.api, token, args.timeout))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
