"""家庭智能终端 - FastAPI 服务端（阶段 2 + 阶段 3 物品提醒）

提供：
- POST /api/voice  语音文件 → 识别+理解+回答（返回 mp3 音频 base64）
- POST /api/chat   文字 → 理解+回答（返回 mp3 音频 base64）
- GET  /api/status 健康检查（含 due_items 到期物品提醒）
- GET  /api/events SSE 事件流（主动推送到期提醒）
- GET  /           静态 Web 客户端（static/index.html）

启动：python server.py  （或 uvicorn server:app --host 0.0.0.0 --port 8000）
"""
import asyncio
import base64
import json
import os
import secrets
import shutil
import tempfile
import threading
import time
import traceback
import wave
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Optional

import config
from fastapi import (
    FastAPI, UploadFile, File, Form, HTTPException, Header, Depends, WebSocket,
)
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config import (
    API_TOKEN,
    HOME_SCAN_SECONDS,
    MAX_AUDIO_SECONDS,
    MAX_CLIENT_ID_CHARS,
    MAX_TEXT_CHARS,
    MAX_UPLOAD_BYTES,
    MCP_TOKEN,
    PUSH_ENABLED,
    PUSH_MAX_FAILURES,
    PUSH_TTL_SECONDS,
    REMINDER_COOLDOWN_HOURS,
    REMINDER_DAYS,
    REMINDER_INTERVAL_SECONDS,
    SERVER_HOST,
    SERVER_PORT,
    VAPID_PRIVATE_KEY,
    VAPID_PUBLIC_KEY,
    VAPID_SUBJECT,
)
from assistant import Assistant
from adapters.home_ws import run_event_stream
from adapters.mcp_endpoint import MCPEndpoint
from domain.push import PushStore
from push_sender import WebPushSender
from stt import STT
from llm import LLM, build_decision_llm
from memory import Memory
from items import Items
from tts import TTS


# ---------- 全局单例（lifespan 中初始化，避免 import 即加载模型） ----------
stt: STT = None
tts: TTS = None
assistant: Assistant = None

# Web Push：push_store 存订阅，push_sender 负责真正发推（lifespan 里建）
push_store: PushStore = None
push_sender: WebPushSender = None

# 到期提醒缓存（提醒循环按 REMINDER_INTERVAL_SECONDS 刷新）
due_items = []
due_tasks = []

# SSE 订阅者队列集合（每个在线客户端一个 asyncio.Queue，提醒循环向其广播）
_subscribers = set()
# 设备通道（小智服务端的提醒桥接器）：单独一组，投递时优先且不合成 mp3。
# 为什么不复用页面订阅：页面要 mp3 自己播，设备走自己的 speak_txt；两条路径的负载不一样。
_device_subscribers = set()

# 因订阅队列满而丢弃的事件次数（丢最旧或补投失败都计入；/api/status 暴露）
dropped_events = 0

# FunASR 非线程安全：同一时间只允许一个识别请求
stt_lock = asyncio.Semaphore(1)

# 临时音频文件保留目录（关闭时清理）
_tmp_dir = None

# 本地 .env（系统配置页只写白名单字段）
ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
_config_lock = threading.Lock()

CONFIG_FIELDS = {
    "TTS_VOICE": ("str", 1, 100),
    "REMINDER_DAYS": ("int", 0, 30),
    "REMINDER_INTERVAL_SECONDS": ("int", 5, 3600),
    "REMINDER_COOLDOWN_HOURS": ("int", 0, 720),
    "SESSION_CONTEXT_TURNS": ("int", 0, 50),
    "SESSION_CONTEXT_MAX_CHARS": ("int", 0, 2000),
    "PROPOSAL_DEDUPE_SECONDS": ("int", 0, 86400),
    "PENDING_ACTION_TTL_SECONDS": ("int", 60, 86400),
    "WAKE_WORD": ("str", 1, 20),
    "WAKE_ACK_TEXT": ("str", 0, 50),
}


async def require_api_token(authorization: str = Header(default="")):
    if not API_TOKEN:
        return
    scheme, _, supplied = authorization.partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(supplied, API_TOKEN):
        raise HTTPException(status_code=401, detail="需要有效的 API Token")


def _coerce_config_value(key, value):
    spec = CONFIG_FIELDS.get(key)
    if spec is None:
        raise ValueError(f"不允许修改 {key}")
    kind, low, high = spec
    if kind == "int":
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{key} 必须是整数")
        if number < low or number > high:
            raise ValueError(f"{key} 必须在 {low}..{high} 之间")
        return number

    text = str(value or "").strip()
    if "\n" in text or len(text) < low or len(text) > high:
        raise ValueError(f"{key} 长度必须在 {low}..{high} 之间")
    return text


def _update_env(updates):
    """把白名单配置写回本地 .env，保留注释和其他行。"""
    with _config_lock:
        lines = []
        if os.path.exists(ENV_PATH):
            with open(ENV_PATH, "r", encoding="utf-8") as handle:
                lines = handle.read().splitlines()

        remaining = dict(updates)
        output = []
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in line:
                key = line.split("=", 1)[0].strip()
                if key in remaining:
                    output.append(f"{key}={remaining.pop(key)}")
                    continue
            output.append(line)

        for key, value in remaining.items():
            output.append(f"{key}={value}")

        text = "\n".join(output).rstrip() + "\n"
        tmp_path = ENV_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_path, ENV_PATH)


def _safe_config():
    return {
        "llm": {
            "model": config.LLM_MODEL,
            "configured": bool(config.LLM_BASE_URL and config.LLM_API_KEY),
        },
        "api_token_enabled": bool(config.API_TOKEN),
        "push": {
            "enabled": bool(config.PUSH_ENABLED and config.VAPID_PRIVATE_KEY),
            "subscriptions": push_store.count_active() if push_store else 0,
        },
        "values": {
            key: getattr(config, key) for key in CONFIG_FIELDS
        },
        "server": {
            "host": config.SERVER_HOST,
            "port": config.SERVER_PORT,
        },
        "db_paths": {
            "memory": config.MEMORY_DB_PATH,
            "items": config.ITEMS_DB_PATH,
            "agent": config.AGENT_DB_PATH,
            "tasks": config.TASKS_DB_PATH,
            "facts": config.FACTS_DB_PATH,
        },
    }


def _page(rows, limit, offset):
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    return rows[offset:offset + limit], len(rows), limit, offset


def _require_confirm(confirm):
    if not confirm:
        raise HTTPException(status_code=400, detail="需要确认删除")


def _stats_sync():
    items_by_status = {
        status: len(assistant.items.list_all(status))
        for status in ("active", "reminded", "expired")
    }
    tasks_by_type = {}
    all_tasks = assistant.tasks.list_all()
    for task in all_tasks:
        task_type = task.get("type") or "custom"
        tasks_by_type[task_type] = tasks_by_type.get(task_type, 0) + 1
    today = datetime.now().strftime("%Y-%m-%d")
    due_today = sum(
        1 for task in all_tasks
        if task.get("status") in ("pending", "reminded")
        and str(task.get("due_at") or "").startswith(today)
    )
    return {
        "counts": {
            "memories": assistant.memory.count(),
            "items": assistant.items.count(),
            "tasks": assistant.tasks.count(),
            "facts": assistant.facts.count(),
            "entities": len(assistant.clarifier.store.list_entities()),
        },
        "items_by_status": items_by_status,
        "tasks_by_type": tasks_by_type,
        "tasks_by_status": {
            status: assistant.tasks.count(status)
            for status in ("pending", "reminded", "done", "cancelled")
        },
        "due_today": due_today,
        "due_items": assistant.items.get_due(days=REMINDER_DAYS),
        "due_tasks": assistant.tasks.get_due(days=REMINDER_DAYS),
    }


def _audio_duration(path, ext):
    if ext != ".wav":
        raise ValueError("当前服务端只支持 WAV 音频的时长校验")
    try:
        with wave.open(path, "rb") as wav:
            frames = wav.getnframes()
            rate = wav.getframerate()
            if not rate:
                raise ValueError("音频采样率无效")
            return frames / float(rate)
    except (wave.Error, OSError) as exc:
        raise ValueError("音频格式无效") from exc


def _reminder_text(item):
    """把到期物品转成一句口语化提醒。"""
    name = item.get("name", "物品")
    left = item.get("days_left")
    if left is None:
        return f"提醒：{name} 快到保质期了"
    if left < 0:
        return f"提醒：{name} 已经过期 {-left} 天了"
    if left == 0:
        return f"提醒：{name} 今天到期"
    return f"提醒：{name} 还有 {left} 天过期"


def _task_reminder_text(task):
    """把到期任务转成一句口语化提醒。"""
    title = task.get("title", "任务")
    left = task.get("days_left")
    if left is None:
        return f"提醒：{title}"
    if left < 0:
        return f"提醒：{title} 已经过期 {-left} 天了"
    if left == 0:
        return f"提醒：{title} 今天到期"
    return f"提醒：{title} 还有 {left} 天到期"


async def _broadcast(event, subscribers=None):
    """把事件推给所有 SSE 订阅者；队列满时丢最旧的，保证最新提醒送达。

    丢事件不再静默：计数累加并通过 /api/status 的 dropped_events 暴露。
    subscribers 为空时默认推给网页订阅者。
    """
    global dropped_events
    payload = json.dumps(event, ensure_ascii=False)
    targets = _subscribers if subscribers is None else subscribers
    for queue in list(targets):
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            # 队列满 = 该客户端消费过慢（或已断连但尚未清理）
            dropped_events += 1
            try:
                queue.get_nowait()
                queue.put_nowait(payload)
                print(f"⚠️ SSE 队列满，丢弃最旧事件（累计 {dropped_events} 次）")
            except Exception:
                print(f"⚠️ SSE 队列满且补投失败，事件未送达（累计 {dropped_events} 次）")
                traceback.print_exc()


async def _send_push(text, event):
    """把一条提醒推给所有有效订阅；失败按 404/410 或失败次数清理。

    返回 (sent, removed, failed)，failed 只用于 /api/push/test 的排障提示。
    """
    if push_sender is None or push_store is None or not push_sender.available:
        return 0, 0, 0

    subscriptions = await asyncio.to_thread(push_store.list_active)
    if not subscriptions:
        return 0, 0, 0

    payload = {
        "title": "小马提醒",
        "body": text,
        "url": "/",
        "tag": event.get("tag") or "xiaoma-reminder",
    }

    async def deliver(subscription):
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(push_sender.send, subscription, payload),
                timeout=15,
            )
        except asyncio.TimeoutError:
            print("⚠️ 推送超时：%s" % subscription.get("endpoint", "")[:60])
            return None

    results = await asyncio.gather(
        *[deliver(sub) for sub in subscriptions], return_exceptions=True
    )

    sent, removed, failed = 0, 0, 0
    for subscription, result in zip(subscriptions, results):
        endpoint = subscription.get("endpoint")
        if isinstance(result, Exception):
            print("⚠️ 推送异常：%s" % result)
            failed += 1
            await asyncio.to_thread(push_store.mark_failure, endpoint, PUSH_MAX_FAILURES)
        elif result is None:
            failed += 1
            await asyncio.to_thread(push_store.mark_failure, endpoint, PUSH_MAX_FAILURES)
        elif result.ok:
            sent += 1
            await asyncio.to_thread(push_store.mark_success, endpoint)
        elif result.gone:
            # 设备已卸载或权限被撤，订阅作废，直接删掉
            removed += 1
            await asyncio.to_thread(push_store.remove, endpoint)
        else:
            failed += 1
            print("⚠️ 推送失败（%s）：%s" % (result.status_code, result.error))
            await asyncio.to_thread(push_store.mark_failure, endpoint, PUSH_MAX_FAILURES)
    return sent, removed, failed


async def _notify(text, event):
    """按「设备 → 网页 → Web Push」投递，同一提醒只走一条通道。

    设备在线优先：客厅是最自然的落点，而且设备自己会念，不需要我们再合成 mp3。
    """
    print("🔔 提醒：%s" % text, flush=True)
    if _device_subscribers:
        await _broadcast(dict(event, reply=text), _device_subscribers)
        return
    if _subscribers:
        await _broadcast(dict(event, reply=text, audio=await _synthesize(text)))
        return
    await _send_push(text, event)


async def _scan_once():
    """跑一轮到期扫描：刷新缓存，必要时领取并投递。

    单独抽出来是为了可测试——提醒循环只负责节奏和异常兜底。
    """
    global due_items, due_tasks
    due_items = await asyncio.to_thread(assistant.items.get_due, days=REMINDER_DAYS)
    due_tasks = await asyncio.to_thread(assistant.tasks.get_due, days=REMINDER_DAYS)
    push_count = 0
    if push_store is not None:
        push_count = await asyncio.to_thread(push_store.count_active)
    if not (_device_subscribers or _subscribers or push_count):
        return 0

    fresh_items = await asyncio.to_thread(
        assistant.items.claim_due_reminders,
        REMINDER_DAYS,
        REMINDER_COOLDOWN_HOURS,
    )
    fresh_tasks = await asyncio.to_thread(
        assistant.tasks.claim_due_reminders,
        REMINDER_DAYS,
        REMINDER_COOLDOWN_HOURS,
    )
    delivered = 0
    for item in fresh_items:
        await _notify(_reminder_text(item), {
            "type": "reminder",
            "item": item,
            "tag": "item-%s" % item.get("id", ""),
        })
        delivered += 1
    for task in fresh_tasks:
        await _notify(_task_reminder_text(task), {
            "type": "reminder",
            "task": task,
            "tag": "task-%s" % task.get("id", ""),
        })
        delivered += 1
    return delivered


async def _has_receivers():
    """有人在收吗：在线设备、在线页面或未停用的 push 订阅。没有就别消耗提醒额度。"""
    if _device_subscribers:
        return True
    if _subscribers:
        return True
    if push_store is None:
        return False
    count = await asyncio.to_thread(push_store.count_active)
    return bool(count)


async def _scan_home():
    """设备巡检：滤芯余量、故障码、盯住的事件。

    和到期提醒共用「有人在收才发」的规矩：没有在线页面也没有 push 订阅时，
    提醒没法送到人，就不消耗去重额度。
    """
    watcher = getattr(assistant, "home_watch", None)
    if watcher is None:
        return 0
    if not await _has_receivers():
        return 0
    alerts = await asyncio.to_thread(watcher.check)
    for alert in alerts:
        await _notify(alert["text"], {
            "type": "reminder",
            "device": {"kind": alert["kind"], "entity_id": alert["entity_id"]},
            "tag": alert["tag"],
        })
    return len(alerts)


async def home_loop():
    """设备巡检单独跑：门铃/有人停留这类事件等 60 秒就没意义了。

    60 秒的循环留给物品和任务（天级的事情），设备看 HOME_SCAN_SECONDS。
    """
    while True:
        try:
            await _scan_home()
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(HOME_SCAN_SECONDS)


async def _on_home_event(entity_id, new_state, attributes):
    """WS 推来一条关注的事件：去重、冷却、投递都走和轮询同一套判定。

    没有人在收（无在线页面也无 push 订阅）时直接返回，不写去重状态——
    否则额度被消耗掉，而人根本没收到。丢了也不要紧：轮询那边还有 3 分钟
    新鲜度窗口兜着。
    """
    watcher = getattr(assistant, "home_watch", None)
    if watcher is None:
        return
    if not await _has_receivers():
        return
    alert = await asyncio.to_thread(
        watcher.annotate_event,
        entity_id,
        new_state,
        attributes.get("friendly_name") or entity_id,
    )
    if not alert:
        return
    await _notify(alert["text"], {
        "type": "reminder",
        "device": {"kind": alert["kind"], "entity_id": alert["entity_id"]},
        "tag": alert["tag"],
    })


async def reminder_loop():
    """后台提醒循环：扫描到期物品与任务，领取后推给在线页面或 push 订阅。

    只有存在「在线页面或 push 订阅」时才领取——否则没通知到人，
    不应消耗 reminded_at 的去重额度。
    """
    while True:
        try:
            await _scan_once()
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(REMINDER_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global stt, tts, assistant, _tmp_dir, push_store, push_sender
    print("🔧 正在初始化系统（首次会加载 STT 模型，约需几十秒）...")
    _tmp_dir = tempfile.mkdtemp(prefix="home_terminal_")
    tts = TTS()
    decision_llm = build_decision_llm()
    assistant = Assistant(memory=Memory(), llm=LLM(), items=Items(),
                          decision_llm=decision_llm)
    print("🧠 决策轮模型：%s" % (decision_llm.model_name if decision_llm else "同回答轮"))
    stt = STT()  # 预热 FunASR
    # Web Push：没有密钥就整体降级为纯 SSE，不影响其他功能
    push_store = PushStore()
    push_sender = None
    if PUSH_ENABLED and VAPID_PRIVATE_KEY:
        try:
            push_sender = WebPushSender(
                VAPID_PRIVATE_KEY, VAPID_SUBJECT, ttl=PUSH_TTL_SECONDS
            )
            print(f"📬 Web Push 已启用，订阅 {push_store.count_active()} 台设备")
        except Exception:
            traceback.print_exc()
            print("⚠️ Web Push 初始化失败，降级为仅 SSE 提醒")
    else:
        print("📭 Web Push 未启用（PUSH_ENABLED/VAPID 密钥缺失）")
    # 启动到期提醒后台任务
    reminder_task = asyncio.create_task(reminder_loop())
    home_task = None
    ws_task = None
    watcher = getattr(assistant, "home_watch", None)
    if watcher is not None:
        home_task = asyncio.create_task(home_loop())
        print(f"🏠 设备巡检已启用（每 {HOME_SCAN_SECONDS} 秒）", flush=True)
        if watcher.event_ids:
            ws_task = asyncio.create_task(
                run_event_stream(assistant.home.client, watcher.event_ids, _on_home_event)
            )
            print(f"📡 事件订阅已启用（{len(watcher.event_ids)} 路，秒级推送）", flush=True)
    print(f"✅ 系统就绪。记忆库 {assistant.memory.count()} 条 | 物品 {assistant.items.count()} 件 | 网页: http://{SERVER_HOST}:{SERVER_PORT}")
    try:
        yield
    finally:
        reminder_task.cancel()
        try:
            await reminder_task
        except asyncio.CancelledError:
            pass
        if home_task is not None:
            home_task.cancel()
            try:
                await home_task
            except asyncio.CancelledError:
                pass
        if ws_task is not None:
            ws_task.cancel()
            try:
                await ws_task
            except asyncio.CancelledError:
                pass
        if assistant is not None:
            assistant.close()
        if push_store is not None:
            push_store.close()
        if _tmp_dir:
            shutil.rmtree(_tmp_dir, ignore_errors=True)
        _subscribers.clear()
        stt = None
        tts = None
        assistant = None
        push_store = None
        push_sender = None
        _tmp_dir = None
        print("🛑 服务关闭")


app = FastAPI(
    title="家庭智能终端",
    lifespan=lifespan,
    # 公网/隧道入口下不暴露接口文档，避免 schema 泄露
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


def _mcp_ask(text, speaker):
    """MCP 接入点唯一入口：设备上所有对话都走这里。

    声纹只用来分开会话上下文，不作凭据：认不出来就用设备默认会话，
    写操作照样走提案/确认，身份不能绕过护栏。
    """
    if assistant is None:
        return "小马还没准备好，稍后再试。"
    speaker_id = (speaker or {}).get("speaker_id") or ""
    client_id = "mcp:%s" % speaker_id if speaker_id else "mcp:device"
    result = assistant.process_text(text, client_id)
    return result.get("reply") or ""


mcp_endpoint = MCPEndpoint(_mcp_ask)


# ---------- 请求模型 ----------

class ChatRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    client_id: str = Field(default="", max_length=MAX_CLIENT_ID_CHARS)


class ConfigUpdate(BaseModel):
    values: dict


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    due_at: Optional[str] = None
    type: Optional[str] = None


class TaskPostpone(BaseModel):
    days: int = 1


class PushKeys(BaseModel):
    p256dh: str = Field(min_length=1, max_length=500)
    auth: str = Field(min_length=1, max_length=500)


class PushSubscribeRequest(BaseModel):
    endpoint: str = Field(min_length=10, max_length=2000)
    keys: PushKeys
    user_agent: str = Field(default="", max_length=300)


class PushEndpointRequest(BaseModel):
    endpoint: str = Field(min_length=10, max_length=2000)


class AskRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    client_id: str = Field(default="", max_length=MAX_CLIENT_ID_CHARS)
    want_audio: bool = False


class TtsRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)


class ChatCompletionRequest(BaseModel):
    """只取需要的字段：xiaozhi-server 会带一堆 OpenAI 参数，多余的忽略。"""

    model: str = ""
    messages: list = Field(default_factory=list)
    stream: bool = False


# ---------- 核心处理 ----------

def _slim_memories(memories):
    """记忆列表精简成前端展示字段。"""
    if not memories:
        return []
    if isinstance(memories[0], dict):
        return [{"text": m["text"], "time": m["time"]} for m in memories]
    return [{"text": str(m), "time": ""} for m in memories]


def _handle_voice_sync(audio_path: str, client_id: str) -> dict:
    """同步处理语音文件（在 to_thread 里跑：STT 识别 + 编排）。"""
    result = assistant.process_voice(stt, audio_path, client_id)
    return result


def _sse_chunk(completion_id, created, model, delta, finish_reason=None):
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


async def _chat_completion_stream(text, completion_id, created, model):
    """把 agent loop 的流式输出转成 OpenAI SSE。

    agent 在 to_thread 里跑（同步阻塞），通过 queue 把 delta 交回事件循环；
    已经发出去的片段会在结束时对齐，避免重复。
    """
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue()

    def on_delta(piece):
        loop.call_soon_threadsafe(queue.put_nowait, ("delta", piece))

    def work():
        try:
            result = assistant.process_text(text, "xiaozhi", on_delta=on_delta)
            loop.call_soon_threadsafe(queue.put_nowait, ("done", result))
        except Exception as exc:
            traceback.print_exc()
            loop.call_soon_threadsafe(queue.put_nowait, ("error", str(exc)))

    worker = asyncio.create_task(asyncio.to_thread(work))

    emitted = ""
    try:
        yield _sse_chunk(completion_id, created, model, {"role": "assistant"})
        while True:
            kind, payload = await queue.get()
            if kind == "delta":
                emitted += payload
                yield _sse_chunk(completion_id, created, model, {"content": payload})
                continue

            if kind == "done":
                full = (payload.get("reply") or "").strip()
                if not emitted:
                    rest = full
                elif full.startswith(emitted):
                    rest = full[len(emitted):]
                else:
                    rest = ""   # 已经播出去的内容不重复
                if rest:
                    yield _sse_chunk(completion_id, created, model, {"content": rest})
                break

            # 出错也按正常收尾，避免语音端一直等
            if not emitted:
                yield _sse_chunk(
                    completion_id, created, model,
                    {"content": "抱歉，我这边出错了，请再说一次。"},
                )
            break

        yield _sse_chunk(completion_id, created, model, {}, "stop")
        yield "data: [DONE]\n\n"
    finally:
        await worker


def _last_user_text(messages):
    """OpenAI messages → 最后一条用户文本。

    content 可能是字符串，也可能是多段 parts（语音终端偶尔这么传），都拼起来。
    """
    for message in reversed(messages or []):
        if (message or {}).get("role") != "user":
            continue
        content = (message or {}).get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") in (None, "text"):
                    text = part.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            return "".join(parts).strip()
    return ""


async def _synthesize(reply: str) -> str:
    """合成回复语音，返回 base64 mp3（空回复返回空串）。

    TTS 失败只丢语音，不影响文字回复。
    """
    if not reply:
        return ""
    try:
        data = await tts.synthesize_bytes(reply)
    except Exception:
        traceback.print_exc()
        return ""
    if not data:
        return ""
    return base64.b64encode(data).decode("ascii")


async def _save_upload(file, ext):
    if _tmp_dir is None:
        raise HTTPException(status_code=503, detail="服务尚未就绪")
    handle = tempfile.NamedTemporaryFile(dir=_tmp_dir, suffix=ext, delete=False)
    tmp_path = handle.name
    total = 0
    try:
        while True:
            chunk = await file.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail=f"音频过大，最大 {MAX_UPLOAD_BYTES // 1024 // 1024}MB")
            await asyncio.to_thread(handle.write, chunk)
        handle.close()
        if total == 0:
            raise HTTPException(status_code=400, detail="音频为空")
        try:
            duration = _audio_duration(tmp_path, ext)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if duration > MAX_AUDIO_SECONDS:
            raise HTTPException(status_code=413, detail=f"音频过长，最大 {MAX_AUDIO_SECONDS} 秒")
        return tmp_path
    except Exception:
        try:
            handle.close()
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        raise


# ---------- API 路由 ----------

@app.websocket("/mcp_endpoint/mcp/")
@app.websocket("/mcp_endpoint/call/")
async def mcp_endpoint_ws(websocket: WebSocket):
    """小智 MCP 接入点（出站 WebSocket + 裸 JSON-RPC）。

    两个路径指向同一个处理器：小智服务端会把配置里的 `/mcp/` 自动改写成
    `/call/`（它给自己接入点的约定），我们不改它的代码，加一个等价路由即可。

    没配 MCP_TOKEN 就整体关闭：这是一个能写库的入口，不能默认对外。
    """
    if not MCP_TOKEN:
        await websocket.close(code=1008)
        return
    token = websocket.query_params.get("token", "")
    if not secrets.compare_digest(token, MCP_TOKEN):
        await websocket.close(code=1008)
        return
    await mcp_endpoint.handle_ws(websocket)


@app.get("/api/status")
async def api_status(_auth=Depends(require_api_token)):
    memory_count, item_count, task_count = await asyncio.to_thread(
        lambda: (
            assistant.memory.count(),
            assistant.items.count(),
            assistant.tasks.count(),
        )
    )
    push_count = await asyncio.to_thread(push_store.count_active) if push_store else 0
    return {
        "ok": True,
        "stt_ready": stt is not None,
        "memory_count": memory_count,
        "item_count": item_count,
        "task_count": task_count,
        "due_items": due_items,  # 到期物品提醒（提醒循环刷新）
        "due_tasks": due_tasks,  # 到期任务提醒（提醒循环刷新）
        "dropped_events": dropped_events,  # SSE 队列满导致丢弃的事件数
        "push_enabled": bool(push_sender is not None and push_sender.available),
        "push_subscriptions": push_count,
        "device_channels": len(_device_subscribers),  # 小智提醒桥接器在线数
        "sessions": len(assistant.sessions),
    }


@app.get("/api/stats")
async def api_stats(_auth=Depends(require_api_token)):
    data = await asyncio.to_thread(_stats_sync)
    data["ok"] = True
    return data


@app.get("/api/memories")
async def api_memories(q: str = "", sort: str = "newest", limit: int = 20,
                       offset: int = 0, _auth=Depends(require_api_token)):
    if sort not in ("newest", "oldest"):
        raise HTTPException(status_code=400, detail="sort 必须是 newest/oldest")
    if q:
        rows = await asyncio.to_thread(
            assistant.memory.search_details, q, 500
        )
    else:
        rows = await asyncio.to_thread(assistant.memory.list_all, 500)
    if sort == "oldest":
        rows = list(reversed(rows))
    page, total, limit, offset = _page(rows, limit, offset)
    return {
        "memories": page,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@app.get("/api/items")
async def api_items(q: str = "", status: str = None, limit: int = 20,
                    offset: int = 0, _auth=Depends(require_api_token)):
    if status and status not in ("active", "reminded", "expired"):
        raise HTTPException(status_code=400, detail="status 必须是 active/reminded/expired")
    rows = await asyncio.to_thread(assistant.items.list_all, status)
    if q:
        keyword = q.strip().lower()
        rows = [
            item for item in rows
            if keyword in str(item.get("name", "")).lower()
        ]
    page, total, limit, offset = _page(rows, limit, offset)
    return {
        "items": page,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@app.get("/api/tasks")
async def api_tasks(q: str = "", status: str = None, sort: str = "due",
                    limit: int = 20, offset: int = 0,
                    _auth=Depends(require_api_token)):
    allowed = ("pending", "reminded", "done", "cancelled")
    if status and status not in allowed:
        raise HTTPException(status_code=400, detail="status 无效")
    if sort not in ("due", "created", "title"):
        raise HTTPException(status_code=400, detail="sort 必须是 due/created/title")
    rows = await asyncio.to_thread(assistant.tasks.list_all, status)
    if q:
        keyword = q.strip().lower()
        rows = [
            task for task in rows
            if keyword in str(task.get("title", "")).lower()
        ]
    if sort == "created":
        rows = sorted(rows, key=lambda t: str(t.get("created_at") or ""), reverse=True)
    elif sort == "title":
        rows = sorted(rows, key=lambda t: str(t.get("title") or ""))
    page, total, limit, offset = _page(rows, limit, offset)
    return {
        "tasks": page,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@app.get("/api/facts")
async def api_facts(q: str = "", limit: int = 20, offset: int = 0,
                    _auth=Depends(require_api_token)):
    rows = await asyncio.to_thread(
        assistant.facts.search, q or None, None, None, 500
    )
    page, total, limit, offset = _page(rows, limit, offset)
    return {
        "facts": page,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@app.get("/api/entities")
async def api_entities(q: str = "", type: str = None, limit: int = 20,
                       offset: int = 0, _auth=Depends(require_api_token)):
    rows = await asyncio.to_thread(
        assistant.clarifier.store.list_entities, type
    )
    if q:
        keyword = q.strip().lower()
        rows = [
            entity for entity in rows
            if keyword in str(entity.get("canonical_name", "")).lower()
            or any(
                keyword in str(alias).lower()
                for alias in entity.get("aliases", [])
            )
        ]
    page, total, limit, offset = _page(rows, limit, offset)
    return {
        "entities": page,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@app.delete("/api/memories/{memory_id}")
async def api_delete_memory(memory_id: int, confirm: bool = False,
                            _auth=Depends(require_api_token)):
    _require_confirm(confirm)
    ok = await asyncio.to_thread(assistant.memory.delete_by_id, memory_id)
    if not ok:
        raise HTTPException(status_code=404, detail="记忆不存在")
    return {"ok": True}


@app.delete("/api/items/{item_id}")
async def api_delete_item(item_id: int, confirm: bool = False,
                          _auth=Depends(require_api_token)):
    _require_confirm(confirm)
    deleted = await asyncio.to_thread(assistant.items.delete_by_id, item_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="物品不存在")
    return {"ok": True, "deleted": deleted}


@app.delete("/api/facts/{fact_id}")
async def api_delete_fact(fact_id: int, confirm: bool = False,
                          _auth=Depends(require_api_token)):
    _require_confirm(confirm)
    deleted = await asyncio.to_thread(assistant.facts.delete_by_id, fact_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="事实不存在")
    return {"ok": True, "deleted": deleted}


@app.delete("/api/tasks/{task_id}")
async def api_delete_task(task_id: int, confirm: bool = False,
                          _auth=Depends(require_api_token)):
    _require_confirm(confirm)
    deleted = await asyncio.to_thread(assistant.tasks.delete_by_id, task_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"ok": True, "deleted": deleted}


@app.patch("/api/tasks/{task_id}")
async def api_update_task(task_id: int, req: TaskUpdate,
                          _auth=Depends(require_api_token)):
    if req.title is None and req.due_at is None and req.type is None:
        raise HTTPException(status_code=400, detail="没有可更新的字段")
    try:
        ok = await asyncio.to_thread(
            assistant.tasks.update,
            task_id,
            req.title,
            req.due_at,
            req.type,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"ok": True}


@app.post("/api/tasks/{task_id}/complete")
async def api_task_complete(task_id: int, _auth=Depends(require_api_token)):
    ok = await asyncio.to_thread(assistant.tasks.complete, task_id)
    if not ok:
        raise HTTPException(status_code=404, detail="任务不存在或已完成")
    return {"ok": True}


@app.post("/api/tasks/{task_id}/cancel")
async def api_task_cancel(task_id: int, _auth=Depends(require_api_token)):
    ok = await asyncio.to_thread(assistant.tasks.cancel, task_id)
    if not ok:
        raise HTTPException(status_code=404, detail="任务不存在或已结束")
    return {"ok": True}


@app.post("/api/tasks/{task_id}/postpone")
async def api_task_postpone(task_id: int, req: TaskPostpone,
                            _auth=Depends(require_api_token)):
    try:
        task = await asyncio.to_thread(assistant.tasks.postpone, task_id, req.days)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在或已结束")
    return {"ok": True, "task": task}


@app.get("/api/config")
async def api_config(_auth=Depends(require_api_token)):
    return {"ok": True, **_safe_config()}


@app.patch("/api/config")
async def api_config_update(req: ConfigUpdate, _auth=Depends(require_api_token)):
    updates = {}
    for key, value in req.values.items():
        try:
            updates[key] = _coerce_config_value(key, value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    if not updates:
        raise HTTPException(status_code=400, detail="没有可更新的配置")

    await asyncio.to_thread(_update_env, updates)
    return {
        "ok": True,
        "restart_required": True,
        "updated": sorted(updates),
    }


@app.get("/api/push/key")
async def api_push_key(_auth=Depends(require_api_token)):
    """下发前端订阅用的公钥；私钥永不出现在响应里。"""
    enabled = bool(
        PUSH_ENABLED and VAPID_PUBLIC_KEY and push_sender is not None
        and push_sender.available
    )
    return {
        "ok": True,
        "enabled": enabled,
        "publicKey": VAPID_PUBLIC_KEY if enabled else "",
    }


@app.post("/api/push/subscribe")
async def api_push_subscribe(req: PushSubscribeRequest, _auth=Depends(require_api_token)):
    if push_store is None:
        raise HTTPException(status_code=503, detail="推送存储未初始化")
    if not req.endpoint.startswith("https://"):
        raise HTTPException(status_code=400, detail="endpoint 必须是 https 地址")
    try:
        await asyncio.to_thread(
            push_store.save,
            req.endpoint,
            req.keys.p256dh,
            req.keys.auth,
            req.user_agent,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    total = await asyncio.to_thread(push_store.count_active)
    return {"ok": True, "subscriptions": total}


@app.post("/api/push/unsubscribe")
async def api_push_unsubscribe(req: PushEndpointRequest, _auth=Depends(require_api_token)):
    if push_store is None:
        raise HTTPException(status_code=503, detail="推送存储未初始化")
    removed = await asyncio.to_thread(push_store.remove, req.endpoint)
    total = await asyncio.to_thread(push_store.count_active)
    return {"ok": True, "removed": removed, "subscriptions": total}


@app.post("/api/push/test")
async def api_push_test(_auth=Depends(require_api_token)):
    if push_sender is None or not push_sender.available:
        raise HTTPException(status_code=503, detail="服务端未配置推送密钥")
    sent, removed, failed = await _send_push("推送测试：这是一条来自小马的提醒", {
        "type": "reminder",
        "tag": "push-test",
    })
    total = await asyncio.to_thread(push_store.count_active)
    if sent:
        detail = "已发送 %d 台" % sent
    elif failed:
        detail = "有 %d 台推送失败：服务端连不上推送服务或密钥不对" % failed
    elif removed:
        detail = "订阅已失效并清理，请重新开启通知"
    elif not total:
        detail = "没有可用的订阅，请先开启通知"
    else:
        detail = "没有可用的订阅"
    return {
        "ok": sent > 0,
        "sent": sent,
        "removed": removed,
        "failed": failed,
        "subscriptions": total,
        "detail": detail,
    }


@app.post("/api/asr")
async def api_asr(file: UploadFile = File(...), _auth=Depends(require_api_token)):
    """语音转文字（设备网关用）：只识别，不进 agent。"""
    if stt is None:
        raise HTTPException(status_code=503, detail="语音识别未就绪")
    if not file.filename or not file.filename.lower().endswith(".wav"):
        raise HTTPException(status_code=400, detail="仅支持 WAV 音频")

    tmp_path = None
    try:
        tmp_path = await _save_upload(file, ".wav")
        async with stt_lock:
            text = await asyncio.to_thread(stt.transcribe, tmp_path)
    except HTTPException:
        raise
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"识别失败: {exc}")
    finally:
        await file.close()
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)

    return {"ok": True, "text": str(text or "").strip()}


@app.post("/api/tts")
async def api_tts(req: TtsRequest, _auth=Depends(require_api_token)):
    """文字转语音（设备网关用）：返回 mp3 字节，交给调用方转 Opus。

    Edge TTS 偶发 NoAudioReceived（对端直接断流），重试一次再报错；
    设备端等不起长尾失败，宁可多一次请求。
    """
    if tts is None:
        raise HTTPException(status_code=503, detail="语音合成未就绪")
    data = b""
    for _attempt in range(2):
        try:
            data = await tts.synthesize_bytes(req.text)
        except Exception:
            traceback.print_exc()
            data = b""
        if data:
            break
    if not data:
        raise HTTPException(status_code=502, detail="语音合成失败")
    return Response(content=data, media_type="audio/mpeg")


@app.post("/api/ask")
async def api_ask(req: AskRequest, _auth=Depends(require_api_token)):
    """整句交给 Agent（设备端 ask_xiaoma 工具的后端）。

    client_id 决定多轮上下文与待澄清动作的归属，设备应传稳定的设备 ID。
    """
    if assistant is None:
        raise HTTPException(status_code=503, detail="服务尚未就绪")
    client_id = req.client_id or "xiaozhi"
    try:
        result = await asyncio.to_thread(assistant.process_text, req.text, client_id)
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"处理失败: {exc}")

    payload = {
        "ok": True,
        "type": result.get("type", "chat"),
        "reply": result.get("reply", ""),
        "summary": result.get("summary", ""),
        "used_tools": result.get("used_tools", []),
        "domain": result.get("domain", "chat"),
        "action": result.get("action", "chat"),
        "pending_action": result.get("pending_action"),
    }
    if req.want_audio:
        payload["audio"] = await _synthesize(result.get("reply", ""))
    return payload


@app.post("/v1/chat/completions")
async def api_chat_completions(req: ChatCompletionRequest,
                               _auth=Depends(require_api_token)):
    """OpenAI 兼容入口：让 xiaozhi-server 把整轮对话直接交给家里的 Agent。

    语音终端那边只做 VAD/ASR/TTS，理解和工具调用全在这里，避免两套 agent。
    """
    if assistant is None:
        raise HTTPException(status_code=503, detail="服务尚未就绪")

    text = _last_user_text(req.messages)
    if not text:
        raise HTTPException(status_code=400, detail="messages 里没有用户文本")

    created = int(time.time())
    completion_id = "chatcmpl-" + secrets.token_hex(8)

    if req.stream:
        # 先把 head 发出去，再在后台线程跑 agent loop，按 delta 实时下发，
        # 让语音端第一句就能开始合成。
        return StreamingResponse(
            _chat_completion_stream(text, completion_id, created, req.model or "xiaoma"),
            media_type="text/event-stream",
        )

    try:
        result = await asyncio.to_thread(assistant.process_text, text, "xiaozhi")
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"处理失败: {exc}")

    reply = result.get("reply", "") or ""

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": req.model or "xiaoma",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": reply},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": len(text), "completion_tokens": len(reply),
                  "total_tokens": len(text) + len(reply)},
    }


@app.get("/api/events")
async def api_events(role: str = "browser", _auth=Depends(require_api_token)):
    """SSE 事件流：主动推送到期提醒。

    前端用 fetch 流式读取（EventSource 无法携带 Authorization 头）。
    每个连接一个队列，提醒循环通过 _broadcast 投递。

    role=device 是给小智服务端的提醒桥接器用的：投递优先级最高，
    事件里不带 mp3（设备用自己的 TTS 念）。其余情况都按网页处理。
    """
    if role not in ("browser", "device"):
        raise HTTPException(status_code=400, detail="role 只能是 browser 或 device")

    queue = asyncio.Queue(maxsize=50)
    targets = _device_subscribers if role == "device" else _subscribers
    targets.add(queue)

    async def event_stream():
        try:
            yield ": connected\n\n"
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=25)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"  # 心跳，防代理/浏览器断流
                    continue
                yield f"data: {payload}\n\n"
        finally:
            targets.discard(queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/chat")
async def api_chat(req: ChatRequest, _auth=Depends(require_api_token)):
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="text 不能为空")

    try:
        # 编排是同步阻塞（memory 检索 + LLM 调用），丢线程池避免卡事件循环
        result = await asyncio.to_thread(assistant.process_text, req.text, req.client_id)
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"处理失败: {e}")

    return {
        "type": result.get("type", "chat"),
        "reply": result.get("reply", ""),
        "summary": result.get("summary", ""),
        "memories": _slim_memories(result.get("memories", [])),
        "text": req.text,
        "audio": await _synthesize(result.get("reply", "")),
        "used_tools": result.get("used_tools", []),
        "domain": result.get("domain", "chat"),
        "action": result.get("action", "chat"),
        "pending_action": result.get("pending_action"),
    }


@app.post("/api/voice")
async def api_voice(file: UploadFile = File(...), client_id: str = Form(""), _auth=Depends(require_api_token)):
    if len(client_id) > MAX_CLIENT_ID_CHARS:
        raise HTTPException(status_code=422, detail="client_id 过长")
    if not file.filename or not file.filename.lower().endswith(".wav"):
        raise HTTPException(status_code=400, detail="仅支持 WAV 音频")

    ext = ".wav"
    tmp_path = None
    try:
        tmp_path = await _save_upload(file, ext)
        async with stt_lock:
            result = await asyncio.to_thread(_handle_voice_sync, tmp_path, client_id)
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"处理失败: {e}")
    finally:
        await file.close()
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)

    return {
        "type": result.get("type", "empty"),
        "reply": result.get("reply", ""),
        "summary": result.get("summary", ""),
        "memories": _slim_memories(result.get("memories", [])),
        "text": result.get("text", ""),
        "audio": await _synthesize(result.get("reply", "")),
        "used_tools": result.get("used_tools", []),
        "domain": result.get("domain", "chat"),
        "action": result.get("action", "chat"),
        "pending_action": result.get("pending_action"),
    }


# ---------- 静态客户端 ----------

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/manifest.json")
async def manifest():
    """PWA 清单：安卓 Chrome / iOS Safari 靠它识别「可安装」。"""
    return FileResponse(
        os.path.join(STATIC_DIR, "manifest.json"),
        media_type="application/manifest+json",
    )


@app.get("/sw.js")
async def service_worker():
    """Service Worker 必须挂在根路径，作用域才是整个站点。"""
    path = os.path.join(STATIC_DIR, "sw.js")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Service worker 未生成")
    response = FileResponse(path, media_type="application/javascript")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Service-Worker-Allowed"] = "/"
    return response


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT, log_level="info")
