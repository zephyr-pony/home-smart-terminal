"""订阅 Home Assistant 的事件流，让门铃/门锁这类事件秒级到达。

轮询是 15 秒一轮，等于门铃按下去可能半分钟后才响——对「有人在门口」这种事
基本没有意义。HA 的 WebSocket API 本来就支持订阅 `state_changed`，这里接上。

为什么不用 `subscribe_trigger` 按实体过滤：它要多写一层自动化触发器的配置格式，
而 451 个实体的 state_changed 在局域网里几乎不占带宽，客户端过滤更简单、更少坏点。

职责边界：这里只管「连上、订阅、把事件交出去」，**去重和推送策略在调用方**
（`server.py`），因为「没有人在收就别消耗提醒额度」是服务端的规矩。

断线就退避重连。事件在断线期间会丢——这是刻意的：健康巡检（滤芯、故障）读的是
当前状态，断线不会漏；事件是「发生的瞬间」，补发意义不大。
"""
import asyncio
import json

import aiohttp

# 退避重连的等待秒数，到顶之后一直保持最后一档
_BACKOFF = (1, 2, 5, 10, 30)


def websocket_url(base_url):
    """http://host:8123 → ws://host:8123/api/websocket"""
    url = str(base_url or "").rstrip("/")
    if url.startswith("https://"):
        url = "wss://" + url[len("https://"):]
    elif url.startswith("http://"):
        url = "ws://" + url[len("http://"):]
    return url + "/api/websocket"


def extract_state_change(message, watched):
    """从一条 WS 消息里取出「关注实体」的新状态。

    Returns:
        (entity_id, new_state, attributes) 或 None
    """
    if not isinstance(message, dict) or message.get("type") != "event":
        return None
    event = message.get("event") or {}
    if event.get("event_type") != "state_changed":
        return None
    data = event.get("data") or {}
    entity_id = data.get("entity_id")
    if entity_id not in watched:
        return None
    new_state = data.get("new_state") or {}
    old_state = data.get("old_state") or {}
    state = new_state.get("state")
    if not state or state == old_state.get("state"):
        # 同一秒内重复上报，或属性变了但状态没变
        return None
    return entity_id, state, new_state.get("attributes") or {}


async def _subscribe(session, url, token, watched, on_event):
    """连上并订阅，直到连接断开或出错。"""
    async with session.ws_connect(url, heartbeat=30) as ws:
        hello = await ws.receive_json()
        if hello.get("type") != "auth_required":
            raise RuntimeError("HA 没有要求认证，可能地址写错了")
        await ws.send_json({"type": "auth", "access_token": token})
        auth = await ws.receive_json()
        if auth.get("type") != "auth_ok":
            raise RuntimeError("HA 认证失败：%s" % auth.get("message", ""))

        await ws.send_json({
            "id": 1,
            "type": "subscribe_events",
            "event_type": "state_changed",
        })
        result = await ws.receive_json()
        if not result.get("success", False):
            raise RuntimeError("订阅事件失败：%s" % result.get("error", {}))

        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    payload = json.loads(msg.data)
                except ValueError:
                    continue
                hit = extract_state_change(payload, watched)
                if hit:
                    await on_event(*hit)
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break


async def run_event_stream(client, watched, on_event, log=None):
    """常驻协程：断线重连，把关注的状态变化交给 on_event。

    client 是 `HAClient`（只用它的 base_url / token）。
    on_event(entity_id, new_state, attributes) 是普通协程。
    """
    watched = set(watched or ())
    if not watched:
        return
    url = websocket_url(client.base_url)
    token = client.token
    log = log or (lambda text: print(text, flush=True))

    attempt = 0
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                await _subscribe(session, url, token, watched, on_event)
            attempt = 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            delay = _BACKOFF[min(attempt, len(_BACKOFF) - 1)]
            attempt += 1
            log("⚠️ 事件订阅断开（%s），%d 秒后重连" % (exc, delay))
            await asyncio.sleep(delay)
            continue
        # 正常关闭也重连，但别立刻刷屏
        await asyncio.sleep(_BACKOFF[0])
