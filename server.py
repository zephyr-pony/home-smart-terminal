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
import traceback
import wave
from contextlib import asynccontextmanager

import config
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Header, Depends
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from config import (
    API_TOKEN,
    MAX_AUDIO_SECONDS,
    MAX_CLIENT_ID_CHARS,
    MAX_TEXT_CHARS,
    MAX_UPLOAD_BYTES,
    REMINDER_COOLDOWN_HOURS,
    REMINDER_DAYS,
    REMINDER_INTERVAL_SECONDS,
    SERVER_HOST,
    SERVER_PORT,
)
from assistant import Assistant
from stt import STT
from llm import LLM
from memory import Memory
from items import Items
from tts import TTS


# ---------- 全局单例（lifespan 中初始化，避免 import 即加载模型） ----------
stt: STT = None
tts: TTS = None
assistant: Assistant = None

# 到期提醒缓存（提醒循环按 REMINDER_INTERVAL_SECONDS 刷新）
due_items = []
due_tasks = []

# SSE 订阅者队列集合（每个在线客户端一个 asyncio.Queue，提醒循环向其广播）
_subscribers = set()

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


def _stats_sync():
    return {
        "counts": {
            "memories": assistant.memory.count(),
            "items": assistant.items.count(),
            "tasks": assistant.tasks.count(),
            "facts": assistant.facts.count(),
            "entities": len(assistant.clarifier.store.list_entities()),
        },
        "tasks_by_status": {
            status: assistant.tasks.count(status)
            for status in ("pending", "reminded", "done", "cancelled")
        },
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


async def _broadcast(event):
    """把事件推给所有 SSE 订阅者；队列满时丢最旧的，保证最新提醒送达。

    丢事件不再静默：计数累加并通过 /api/status 的 dropped_events 暴露。
    """
    global dropped_events
    payload = json.dumps(event, ensure_ascii=False)
    for queue in list(_subscribers):
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


async def reminder_loop():
    """后台提醒循环：扫描到期物品，落库去重后向在线客户端主动推送提醒。

    只有存在在线订阅者时才"领取"提醒——否则没通知到人，不应消耗去重额度。
    """
    global due_items, due_tasks
    while True:
        try:
            due_items = await asyncio.to_thread(
                assistant.items.get_due, days=REMINDER_DAYS
            )
            due_tasks = await asyncio.to_thread(
                assistant.tasks.get_due, days=REMINDER_DAYS
            )
            if _subscribers:
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
                for item in fresh_items:
                    text = _reminder_text(item)
                    await _broadcast({
                        "type": "reminder",
                        "item": item,
                        "reply": text,
                        "audio": await _synthesize(text),
                    })
                for task in fresh_tasks:
                    text = _task_reminder_text(task)
                    await _broadcast({
                        "type": "reminder",
                        "task": task,
                        "reply": text,
                        "audio": await _synthesize(text),
                    })
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(REMINDER_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global stt, tts, assistant, _tmp_dir
    print("🔧 正在初始化系统（首次会加载 STT 模型，约需几十秒）...")
    _tmp_dir = tempfile.mkdtemp(prefix="home_terminal_")
    tts = TTS()
    assistant = Assistant(memory=Memory(), llm=LLM(), items=Items())
    stt = STT()  # 预热 FunASR
    # 启动到期提醒后台任务
    reminder_task = asyncio.create_task(reminder_loop())
    print(f"✅ 系统就绪。记忆库 {assistant.memory.count()} 条 | 物品 {assistant.items.count()} 件 | 网页: http://{SERVER_HOST}:{SERVER_PORT}")
    try:
        yield
    finally:
        reminder_task.cancel()
        try:
            await reminder_task
        except asyncio.CancelledError:
            pass
        if assistant is not None:
            assistant.close()
        if _tmp_dir:
            shutil.rmtree(_tmp_dir, ignore_errors=True)
        _subscribers.clear()
        stt = None
        tts = None
        assistant = None
        _tmp_dir = None
        print("🛑 服务关闭")


app = FastAPI(title="家庭智能终端", lifespan=lifespan)


# ---------- 请求模型 ----------

class ChatRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    client_id: str = Field(default="", max_length=MAX_CLIENT_ID_CHARS)


class ConfigUpdate(BaseModel):
    values: dict


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

@app.get("/api/status")
async def api_status(_auth=Depends(require_api_token)):
    memory_count, item_count, task_count = await asyncio.to_thread(
        lambda: (
            assistant.memory.count(),
            assistant.items.count(),
            assistant.tasks.count(),
        )
    )
    return {
        "ok": True,
        "stt_ready": stt is not None,
        "memory_count": memory_count,
        "item_count": item_count,
        "task_count": task_count,
        "due_items": due_items,  # 到期物品提醒（提醒循环刷新）
        "due_tasks": due_tasks,  # 到期任务提醒（提醒循环刷新）
        "dropped_events": dropped_events,  # SSE 队列满导致丢弃的事件数
        "sessions": len(assistant.sessions),
    }


@app.get("/api/stats")
async def api_stats(_auth=Depends(require_api_token)):
    data = await asyncio.to_thread(_stats_sync)
    data["ok"] = True
    return data


@app.get("/api/memories")
async def api_memories(limit: int = 50, _auth=Depends(require_api_token)):
    limit = max(1, min(int(limit), 200))
    memories = await asyncio.to_thread(assistant.memory.list_all, limit)
    return {"memories": memories}


@app.get("/api/items")
async def api_items(status: str = None, _auth=Depends(require_api_token)):
    if status and status not in ("active", "reminded", "expired"):
        raise HTTPException(status_code=400, detail="status 必须是 active/reminded/expired")
    items = await asyncio.to_thread(assistant.items.list_all, status)
    return {"items": items}


@app.get("/api/tasks")
async def api_tasks(status: str = None, _auth=Depends(require_api_token)):
    allowed = ("pending", "reminded", "done", "cancelled")
    if status and status not in allowed:
        raise HTTPException(status_code=400, detail="status 无效")
    tasks = await asyncio.to_thread(assistant.tasks.list_all, status)
    return {"tasks": tasks}


@app.get("/api/facts")
async def api_facts(q: str = "", limit: int = 50, _auth=Depends(require_api_token)):
    limit = max(1, min(int(limit), 200))
    facts = await asyncio.to_thread(
        assistant.facts.search, q or None, None, None, limit
    )
    return {"facts": facts}


@app.get("/api/entities")
async def api_entities(type: str = None, _auth=Depends(require_api_token)):
    entities = await asyncio.to_thread(
        assistant.clarifier.store.list_entities, type
    )
    return {"entities": entities}


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


@app.get("/api/events")
async def api_events(_auth=Depends(require_api_token)):
    """SSE 事件流：主动推送到期提醒。

    前端用 fetch 流式读取（EventSource 无法携带 Authorization 头）。
    每个连接一个队列，提醒循环通过 _broadcast 投递。
    """
    queue = asyncio.Queue(maxsize=50)
    _subscribers.add(queue)

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
            _subscribers.discard(queue)

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

@app.get("/")
async def index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT, log_level="info")
