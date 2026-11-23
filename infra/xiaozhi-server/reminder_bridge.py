"""小智服务端侧的提醒桥：把 xiaoma 的 SSE 提醒念给设备听。

为什么在小智这边做：主动说话需要设备连接（`conn.websocket`），而它只在
xiaozhi-server 进程里；小马那边只能通过 MCP 回结果，推不动设备。

数据流：
    xiaoma `/api/events?role=device`（SSE）
        → 本模块选一台空闲连接
        → `intentHandler.speak_txt(conn, text)`（小智自己的 TTS 队列）
        → 设备播报

约定：
- 没有设备连接时**不订阅**，让小马自动回落到网页 / Web Push
- 设备正在说话就排队，不打断；排队超过 10 分钟丢弃
- 这里只做桥接，不改小智的语音链路
"""
import asyncio
import json
import os
import time
import uuid

# 排队上限与存活时间：提醒过期得比“迟到”更有害
MAX_QUEUE = 20
QUEUE_TTL_SECONDS = 600
BUSY_RETRY_SECONDS = 2
RECONNECT_MAX_SECONDS = 30


class DeviceRegistry:
    """当前在线的小智设备。

    用 `id(conn)` 做键：`conn.device_id` 在 `handle_connection` 里才赋值，
    注册发生时还拿不到；真正需要标识设备时读 conn 自己。
    """

    def __init__(self):
        self._devices = {}

    def add(self, conn):
        if conn is not None:
            self._devices[id(conn)] = conn

    def discard(self, conn):
        self._devices.pop(id(conn), None)

    def __len__(self):
        return len(self._devices)

    def all(self):
        return list(self._devices.values())

    def idle(self):
        """能马上说话的设备：连还在、不在说话、TTS 已初始化。"""
        for conn in self.all():
            if getattr(conn, "websocket", None) is None:
                continue
            if getattr(conn, "tts", None) is None:
                continue
            if getattr(conn, "client_is_speaking", False):
                continue
            yield conn


registry = DeviceRegistry()


def speak_via_xiaozhi(conn, text):
    """走小智现成的“让这台设备念一句”。

    照 `intentHandler` 的做法先开一个新句子：`speak_txt` 依赖 `sentence_id`
    和 TTS 状态，不初始化会把话塞进上一轮。
    """
    from core.handle.intentHandler import speak_txt

    conn.sentence_id = uuid.uuid4().hex
    conn.client_abort = False
    speak_txt(conn, text)


class ReminderBridge:
    def __init__(self, base_url, token, speaker=None, devices=None, now=None):
        self.base_url = str(base_url or "").rstrip("/")
        self.token = str(token or "")
        self._speaker = speaker or speak_via_xiaozhi
        self._devices = devices or registry
        self._now = now or time.time
        self._pending = []
        # 同一台设备刚念完，给它一点时间真正进入 speaking 状态
        self._busy_until = {}

    # ---------- 队列 ----------

    def enqueue(self, text):
        text = str(text or "").strip()
        if not text:
            return
        self._pending.append({"text": text, "at": self._now()})
        if len(self._pending) > MAX_QUEUE:
            dropped = self._pending.pop(0)
            print("⚠️ 提醒队列已满，丢弃最早一条：%s" % dropped["text"][:30])

    def pending_count(self):
        return len(self._pending)

    def _pick(self, now):
        for conn in self._devices.idle():
            if self._busy_until.get(id(conn), 0) <= now:
                return conn
        return None

    def deliver_pending(self):
        """把排队中的提醒尽量发出去，返回本次发了多少条。"""
        now = self._now()
        self._pending = [
            item for item in self._pending
            if now - item["at"] <= QUEUE_TTL_SECONDS
        ]
        delivered = 0
        for item in list(self._pending):
            conn = self._pick(now)
            if conn is None:
                break
            try:
                self._speaker(conn, item["text"])
            except Exception as exc:  # 单条失败不拖垮整队
                print("⚠️ 提醒下发失败，丢弃：%s（%s）" % (item["text"][:30], exc))
                self._pending.remove(item)
                continue
            self._pending.remove(item)
            self._busy_until[id(conn)] = now + _speak_seconds(item["text"])
            delivered += 1
        return delivered

    # ---------- SSE ----------

    async def run(self):
        """常驻循环：有设备才订阅；没设备就等，让小马回落。"""
        backoff = 1
        while True:
            if len(self._devices) == 0:
                await asyncio.sleep(BUSY_RETRY_SECONDS)
                continue
            try:
                await self._subscribe_once()
                backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print("⚠️ 提醒桥断开：%s，%s 秒后重连" % (exc, backoff))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX_SECONDS)

    async def _subscribe_once(self):
        import aiohttp

        headers = {}
        if self.token:
            headers["Authorization"] = "Bearer %s" % self.token
        timeout = aiohttp.ClientTimeout(total=None, sock_read=None)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            url = self.base_url + "/api/events?role=device"
            async with session.get(url, headers=headers) as response:
                if response.status != 200:
                    raise RuntimeError("SSE 返回 %s" % response.status)
                print("🔔 提醒桥已连接：%s" % self.base_url)
                ticker = asyncio.create_task(self._tick())
                try:
                    async for raw in response.content:
                        if len(self._devices) == 0:
                            # 设备都断了：立刻退订，让小马把提醒改投别处
                            return
                        line = raw.decode("utf-8", "ignore").strip()
                        if not line.startswith("data: "):
                            continue
                        try:
                            event = json.loads(line[6:])
                        except ValueError:
                            continue
                        text = str(event.get("reply") or "").strip()
                        if text:
                            self.enqueue(text)
                            self.deliver_pending()
                finally:
                    ticker.cancel()

    async def _tick(self):
        """设备忙时排队的那部分，每 2 秒试一次。"""
        while True:
            await asyncio.sleep(BUSY_RETRY_SECONDS)
            if self._pending:
                self.deliver_pending()


def _speak_seconds(text):
    """粗估这段话要念多久，用来给设备留出进入 speaking 的时间。"""
    return min(30.0, 1.0 + 0.25 * len(str(text or "")))


def start_from_env():
    """从环境变量启动；没配就什么都不做（小马会按无设备处理）。"""
    base_url = os.environ.get("XIAOMA_BASE_URL", "").strip()
    token = os.environ.get("XIAOMA_API_TOKEN", "").strip()
    if not base_url or not token:
        print("ℹ️ 未配置 XIAOMA_BASE_URL / XIAOMA_API_TOKEN，提醒桥不启动")
        return None
    bridge = ReminderBridge(base_url, token)
    asyncio.create_task(bridge.run())
    print("🔔 小马提醒桥启动：%s" % base_url)
    return bridge
