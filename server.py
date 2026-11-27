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
import logging
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
import traceback
import uuid
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
    DATA_DIR,
    HOME_SCAN_SECONDS,
    MAX_AUDIO_SECONDS,
    MAX_CLIENT_ID_CHARS,
    MAX_TEXT_CHARS,
    MAX_UPLOAD_BYTES,
    MCP_TOKEN,
    MEMORY_DISTILL_CHECK_SECONDS,
    MEMORY_DISTILL_ENABLED,
    MEMORY_DISTILL_HOUR,
    MEMORY_DISTILL_INTERVAL_HOURS,
    MEMORY_DISTILL_MAX_GROUPS,
    MEMORY_DISTILL_SUMMARY_CHARS,
    MEMORY_DISTILL_THRESHOLD,
    PRINTER_MAX_PAGES,
    PRINTER_NAME,
    PRINTER_SERVER,
    PRINT_FILES_DIR,
    PUSH_ENABLED,
    PUSH_MAX_FAILURES,
    PUSH_TTL_SECONDS,
    REMINDER_COOLDOWN_HOURS,
    REMINDER_DAYS,
    REMINDER_INTERVAL_SECONDS,
    SERVER_HOST,
    SERVER_PORT,
    SOFT_REMINDER_LAST_HOUR,
    SOFT_REMINDER_LAST_MINUTE,
    STT_ENABLED,
    VAPID_PRIVATE_KEY,
    VAPID_PUBLIC_KEY,
    VAPID_SUBJECT,
)
from agent import wording
from agent import thinking
from agent.dates import parse_date_text
from agent.distill import MemoryDistiller, distillation_due
from adapters.printer import (
    SUPPORTED_EXTENSIONS, PrintError, kind_of, office_available, pdf_page_count,
    validate_file,
)
from assistant import Assistant
from adapters.home_ws import run_event_stream
from adapters.mcp_endpoint import MCPEndpoint
from domain.push import PushStore
from domain.prints import PrintStore, STATUSES as PRINT_STATUSES
from domain.space import (
    DEFAULT_SPACE_ID, ensure_space, list_spaces, normalize_space_id, space_paths,
)
from push_sender import WebPushSender
from stt import STT
from llm import LLM, build_decision_llm
from memory import Memory
from items import Items
from tts import TTS


# ---------- 启动前的安全检查 ----------

_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")


def _check_exposure(host=None, token=None):
    """监听非本机又没设 API_TOKEN 时拒绝启动。

    默认 127.0.0.1 不会被拦；一旦为了局域网把 SERVER_HOST 改成 0.0.0.0，
    必须同时设 Token——否则记忆、任务、删除接口对同网段所有人开放。
    """
    host = (SERVER_HOST if host is None else host) or ""
    token = API_TOKEN if token is None else token
    if token:
        return
    if host.strip().lower() in _LOOPBACK_HOSTS:
        return
    raise RuntimeError(
        "SERVER_HOST=%s 且 API_TOKEN 为空：这会把家庭记忆和任务接口暴露给"
        "同网段所有人。请在 .env 设置 API_TOKEN，或改回 127.0.0.1。" % host
    )


# ---------- 访问日志脱敏 ----------
# 小智的 MCP 接入点只能用查询参数带 token（上游就是这么连的），uvicorn 的
# 访问日志会原样打印完整 URL。日志被贴到 issue 里就等于泄露 token，落地前抹掉。

_TOKEN_IN_QUERY_RE = re.compile(
    r"((?:[?&;]|^)(?:token|access_token|key)=)[^&\s\"']+", re.I
)


class _TokenRedactingFilter(logging.Filter):
    @staticmethod
    def _clean(value):
        if not isinstance(value, str):
            return value
        return _TOKEN_IN_QUERY_RE.sub(r"\1***", value)

    def filter(self, record):
        if isinstance(record.args, tuple):
            record.args = tuple(self._clean(arg) for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: self._clean(v) for k, v in record.args.items()}
        if isinstance(record.msg, str):
            record.msg = self._clean(record.msg)
        return True


def _install_log_redaction():
    # 两个都要挂：HTTP 请求走 `uvicorn.access`，但 WebSocket 握手（MCP 接入点就靠它）
    # 是 `uvicorn.error` 打的 `%s - "WebSocket %s" [accepted]`，只挂 access 会漏掉
    # `?token=`，容器日志里就是明文。
    for name in ("uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, _TokenRedactingFilter) for f in logger.filters):
            logger.addFilter(_TokenRedactingFilter())


_install_log_redaction()


# ---------- 全局单例（lifespan 中初始化，避免 import 即加载模型） ----------
stt: STT = None
tts: TTS = None
assistant: Assistant = None

# Web Push：push_store 存订阅，push_sender 负责真正发推（lifespan 里建）
push_store: PushStore = None
push_sender: WebPushSender = None

# 记忆蒸馏（D60）：lifespan 里建，后台循环按深夜/阈值触发
distiller: MemoryDistiller = None

# 待打印文件（D61/D65）：元数据在 prints.db，文件在 PRINT_FILES_DIR（卷里）。
# 重启后记录和文件都还在，「再打一份」不用重传；过期由 `_purge_print_files` 清。
print_store = None

# 空间路由（D71）：默认空间沿用模块里的 `assistant` / `print_store`（启动时建好的那套，
# 测试也靠 monkeypatch 这几个名字），其它空间按需建一套，互不共享库文件。
# 只做「手动路由」：请求带 X-Space-Id 就落到那个空间，不带就是 default。
# 谁该用哪个空间（声纹/设备归属）还没接，别在这里猜。
MAX_SPACES = 16
_space_assistants = {}
_space_lock = threading.Lock()
_shared_llm = None
_shared_decision_llm = None


class Space:
    """一次请求落在哪个空间（D71）。

    默认空间绕回模块级变量：升级前就是那一套，测试也靠 monkeypatch 注入假实现；
    其它空间用 `assistant.print_store` / `print_files_dir`，各空间一套文件。
    """

    def __init__(self, space_id, assistant_obj, paths):
        self.id = space_id
        self.assistant = assistant_obj
        self.dir = paths["dir"]

    @property
    def print_store(self):
        if self.id == DEFAULT_SPACE_ID:
            return print_store
        return getattr(self.assistant, "print_store", None)

    @property
    def print_dir(self):
        if self.id == DEFAULT_SPACE_ID:
            return PRINT_FILES_DIR
        return getattr(self.assistant, "print_files_dir", PRINT_FILES_DIR)


def _build_space_assistant(space_id, paths):
    """新空间的一套 store；测试会替换这个函数，别在别处硬编码构造。"""
    store = PrintStore(path=paths["prints"])
    store.prune()
    return Assistant(
        llm=_shared_llm or LLM(),
        decision_llm=_shared_decision_llm,
        print_store=store,
        space_id=space_id,
    )


def _assistant_for(space_id):
    """返回 (assistant, paths)；新空间第一次访问时创建，之后复用。"""
    if space_id == DEFAULT_SPACE_ID:
        return assistant, space_paths(space_id)
    with _space_lock:
        found = _space_assistants.get(space_id)
        if found is not None:
            return found, space_paths(space_id)
        if len(_space_assistants) >= MAX_SPACES:
            raise HTTPException(
                status_code=409,
                detail="空间数量已达上限（%d），先清理不用的空间" % MAX_SPACES,
            )
        paths = ensure_space(space_id)
        found = _build_space_assistant(space_id, paths)
        _space_assistants[space_id] = found
        print("🗂 空间 %s 已就绪：%s" % (space_id, paths["dir"]), flush=True)
        return found, paths


def current_space(x_space_id: str = Header(default="", alias="X-Space-Id")):
    """按请求头选空间；名字非法直接 400，不静默退回 default（那样会写错库）。"""
    try:
        space_id = normalize_space_id(x_space_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    assistant_obj, paths = _assistant_for(space_id)
    return Space(space_id, assistant_obj, paths)


def _purge_print_files(now=None, store=None):
    """清掉过期文件（默认留 PRINT_KEEP_DAYS 天）。文件没了只留历史记录。"""
    store = print_store if store is None else store
    if store is not None:
        store.prune(now)


def _get_print_file(file_id, client_id="", store=None):
    """取一个待打印文件；不存在/已过期/不属于这个客户端都返回 None。"""
    store = print_store if store is None else store
    if store is None:
        return None
    return store.get(file_id, client_id)


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

# 进程启动时间（lifespan 里写）；/api/status 用它算「跑了多久」
started_at = None

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


def _stats_sync(space=None):
    """统计当前空间；`space` 为空时按默认空间（提醒循环等内部调用）。"""
    assistant_obj = space.assistant if space is not None else assistant
    items_by_status = {
        status: len(assistant_obj.items.list_all(status))
        for status in ("active", "reminded", "expired")
    }
    tasks_by_type = {}
    all_tasks = assistant_obj.tasks.list_all()
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
            "memories": assistant_obj.memory.count(),
            "items": assistant_obj.items.count(),
            "tasks": assistant_obj.tasks.count(),
            "facts": assistant_obj.facts.count(),
            "entities": len(assistant_obj.clarifier.store.list_entities()),
        },
        "items_by_status": items_by_status,
        "tasks_by_type": tasks_by_type,
        "tasks_by_status": {
            status: assistant_obj.tasks.count(status)
            for status in ("pending", "reminded", "done", "cancelled")
        },
        "due_today": due_today,
        "due_items": assistant_obj.items.get_due(days=REMINDER_DAYS),
        "due_tasks": assistant_obj.tasks.get_due(days=REMINDER_DAYS),
        "runtime": _runtime_sync(
            space.dir if space is not None else DATA_DIR,
            space.print_dir if space is not None else PRINT_FILES_DIR,
        ),
    }


# ---------- 网页看板（D82）----------
#
# 看板是「一直开着」的页面，手机/平板每 60 秒问一次。所以：
# - 一次请求拿全（今天要办的、家里、最近、天气），别让页面打四五个接口；
# - 服务端各给一道缓存：看板 30 秒，天气 10 分钟（上游按小时更新，问太勤没意义）；
# - 用户在看板上点了「办了/明天」，前端带 refresh=1 跳过缓存，看到的就是刚落的库。

BOARD_CACHE_SECONDS = 30
WEATHER_CACHE_SECONDS = 600

_weather_cache = {"at": 0.0, "data": None}
_weather_lock = threading.Lock()
_board_cache = {}
_board_lock = threading.Lock()


def _weather_snapshot(force=False):
    """天气快照（结构化）。失败也缓存 60 秒，别让页面每个请求都去打上游。"""
    now = time.time()
    with _weather_lock:
        cached = _weather_cache["data"]
        if cached is not None:
            age = now - _weather_cache["at"]
            if not force and age < (60 if not cached.get("ok") else WEATHER_CACHE_SECONDS):
                return cached
    weather = getattr(assistant, "weather", None)
    if weather is None:
        data = {"ok": False, "error": "天气服务还没配置"}
    else:
        data = weather.snapshot()
    with _weather_lock:
        _weather_cache["data"] = data
        _weather_cache["at"] = time.time()
    return data


def _home_snapshot(assistant_obj):
    home = getattr(assistant_obj, "home", None)
    snapshot = getattr(home, "snapshot", None)
    if not callable(snapshot):
        return {"configured": False, "devices": []}
    try:
        return snapshot()
    except Exception as exc:
        print("⚠️ 设备快照读取失败：%s" % exc)
        return {"configured": False, "devices": [], "error": "设备读数暂时拿不到"}


def _board_sync(space, force=False):
    """看板数据：今天要办的 + 家里 + 最近 + 天气。"""
    now = datetime.now()
    if not force:
        with _board_lock:
            cached = _board_cache.get(space.id)
        if cached and (time.time() - cached["at"]) < BOARD_CACHE_SECONDS:
            return cached["data"]

    assistant_obj = space.assistant
    due_tasks = assistant_obj.tasks.get_due(days=0)
    due_items = assistant_obj.items.get_due(days=0)

    # 今天已经办掉的：看板折叠成一行「已办 N 件」，不然办完就消失像没发生过
    done_today = []
    try:
        stamp = now.strftime("%Y-%m-%d")
        for row in assistant_obj.tasks.list_all("done"):
            if str(row.get("completed_at") or "")[:10] == stamp:
                done_today.append({"id": row.get("id"), "title": row.get("title") or ""})
    except Exception as exc:
        print("⚠️ 今天已办任务读取失败：%s" % exc)

    recent_memories = []
    try:
        for row in assistant_obj.memory.list_all(4):
            recent_memories.append({
                "id": row.get("id"),
                "text": row.get("summary") or row.get("text") or "",
                "time": row.get("time") or "",
            })
    except Exception as exc:
        print("⚠️ 最近记忆读取失败：%s" % exc)

    recent_prints = []
    store = space.print_store
    if store is not None:
        try:
            rows, _total = store.list("", "", 3, 0)
            for row in rows:
                recent_prints.append({
                    "id": row.get("id"),
                    "name": row.get("name") or "文件",
                    "status": row.get("status") or "",
                    "copies": row.get("copies") or 1,
                    "created_at": row.get("created_at"),
                })
        except Exception as exc:
            print("⚠️ 最近打印读取失败：%s" % exc)

    data = {
        "ok": True,
        "space_id": space.id,
        "at": now.isoformat(timespec="seconds"),
        "today": {
            "tasks": [
                {
                    "id": task.get("id"),
                    "title": task.get("title") or "",
                    "type": task.get("type") or "custom",
                    "mode": task.get("mode") or "timely",
                    "recur": task.get("recur") or "",
                    "due_at": task.get("due_at") or "",
                    "days_left": task.get("days_left"),
                }
                for task in due_tasks
            ],
            "items": [
                {
                    "id": item.get("id"),
                    "name": item.get("name") or "",
                    "expiry_date": item.get("expiry_date") or "",
                    "days_left": item.get("days_left"),
                }
                for item in due_items
            ],
            "done": done_today[:5],
        },
        "home": _home_snapshot(assistant_obj),
        "printer": assistant_obj.printer_status() if hasattr(assistant_obj, "printer_status") else {},
        "recent": {"memories": recent_memories, "prints": recent_prints},
        "weather": _weather_snapshot(),
    }
    with _board_lock:
        _board_cache[space.id] = {"at": time.time(), "data": data}
    return data


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
    recur = task.get("recur") or ""
    if recur:
        # 周期任务不用「到期」：它天天到点，说清楚是哪一个周期就行
        label = wording.recur_label(recur, task.get("due_at"))
        return "提醒：%s（%s）" % (title, label) if label else "提醒：%s" % title
    left = task.get("days_left")
    if left is None:
        return f"提醒：{title}"
    if left < 0:
        return f"提醒：{title} 已经过期 {-left} 天了"
    if left == 0:
        return f"提醒：{title} 今天到期"
    return f"提醒：{title} 还有 {left} 天到期"


def _item_reminder_facts(item, soft=False):
    """给模型的提醒事实（只给事实，话由它组织）：物品名、到期日、还剩几天、现在几点。"""
    lines = ["这是物品快到期提醒。", "物品：%s" % (item.get("name") or "物品")]
    expiry = str(item.get("expiry_date") or "")
    if expiry:
        lines.append("到期日：%s" % expiry)
    left = item.get("days_left")
    if left is None:
        pass
    elif left < 0:
        lines.append("已经过期 %d 天" % -left)
    elif left == 0:
        lines.append("今天就到期")
    else:
        lines.append("还有 %d 天到期" % left)
    if soft:
        lines.append("这是白天没机会说的软提醒，现在补一句。")
    lines.append("现在：%s" % datetime.now().strftime("%Y年%m月%d日 %H:%M"))
    return "\n".join(lines)


def _task_reminder_facts(task, soft=False):
    """任务版的事实：标题、到期时间、逾期/剩余天数、周期、现在几点。"""
    lines = ["这是到点提醒。" if not task.get("recur") else "这是周期提醒。"]
    lines.append("事项：%s" % (task.get("title") or "任务"))
    due = str(task.get("due_at") or "")
    if due:
        lines.append("到期：%s" % due)
    recur = task.get("recur") or ""
    if recur:
        label = wording.recur_label(recur, due)
        if label:
            lines.append("周期：%s" % label)
    left = task.get("days_left")
    if left is not None:
        if left < 0:
            lines.append("已经过期 %d 天" % -left)
        elif left == 0:
            lines.append("今天到期")
        else:
            lines.append("还有 %d 天到期" % left)
    if soft:
        lines.append("这是白天没机会说的软提醒，现在补一句。")
    lines.append("现在：%s" % datetime.now().strftime("%Y年%m月%d日 %H:%M"))
    return "\n".join(lines)


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

    task = event.get("task") or {}
    item = event.get("item") or {}
    payload = {
        "title": "小马提醒",
        "body": text,
        "url": "/",
        "tag": event.get("tag") or "xiaoma-reminder",
        # 点通知时要能定位到具体任务（网页端据此把这一轮绑定到那条提醒）
        "kind": "task" if task else ("item" if item else ""),
        "task_id": task.get("id") or 0,
        "task_title": task.get("title") or "",
        "item_id": item.get("id") or 0,
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


def _soft_window_open(now=None):
    """软提醒的晚间兜底到点了吗（SOFT_REMINDER_LAST_CALL）。

    白天 soft 的只走「聊天里顺口提」，到这个点还没机会说，才按准点提醒发一次，
    免得事情悄悄过期。物品到期也算 soft：它只有一个日期，没有「到点」可言。
    """
    now = now or datetime.now()
    cutoff = now.replace(
        hour=SOFT_REMINDER_LAST_HOUR,
        minute=SOFT_REMINDER_LAST_MINUTE,
        second=0,
        microsecond=0,
    )
    return now >= cutoff


async def _scan_once(now=None):
    """跑一轮到期扫描：刷新缓存，必要时领取并投递。

    单独抽出来是为了可测试——提醒循环只负责节奏和异常兜底。
    """
    global due_items, due_tasks
    now = now or datetime.now()
    soft_open = _soft_window_open(now)
    due_items = await asyncio.to_thread(assistant.items.get_due, days=REMINDER_DAYS)
    due_tasks = await asyncio.to_thread(assistant.tasks.get_due, days=REMINDER_DAYS)
    push_count = 0
    if push_store is not None:
        push_count = await asyncio.to_thread(push_store.count_active)
    if not (_device_subscribers or _subscribers or push_count):
        return 0

    # 软提醒等晚间兜底：白天不领取，就不会在饭桌上突然播报，
    # 也不会把 reminded_at 提前写掉（否则晚上反而提不了）。
    fresh_items = []
    if soft_open:
        fresh_items = await asyncio.to_thread(
            assistant.items.claim_due_reminders,
            REMINDER_DAYS,
            REMINDER_COOLDOWN_HOURS,
            now,
        )
    fresh_tasks = await asyncio.to_thread(
        assistant.tasks.claim_due_reminders,
        REMINDER_DAYS,
        REMINDER_COOLDOWN_HOURS,
        now,
        "timely",
    )
    if soft_open:
        fresh_soft = await asyncio.to_thread(
            assistant.tasks.claim_due_reminders,
            REMINDER_DAYS,
            REMINDER_COOLDOWN_HOURS,
            now,
            "soft",
        )
        fresh_tasks = list(fresh_tasks) + list(fresh_soft)
    delivered = 0
    compose = getattr(assistant, "reminder_sentence", None)
    for item in fresh_items:
        fallback = _reminder_text(item)
        text = fallback
        if compose is not None:
            text = await asyncio.to_thread(
                compose, _item_reminder_facts(item, soft=True), fallback,
                item.get("name"), soft=True,
            )
        await _notify(text, {
            "type": "reminder",
            "item": item,
            "tag": "item-%s" % item.get("id", ""),
        })
        delivered += 1
    for task in fresh_tasks:
        soft = task.get("mode") == "soft"
        fallback = _task_reminder_text(task)
        text = fallback
        if compose is not None:
            text = await asyncio.to_thread(
                compose, _task_reminder_facts(task, soft=soft), fallback,
                task.get("title"), soft=soft,
            )
        await _notify(text, {
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
        attributes,
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


# ---------- 记忆蒸馏（D60） ----------

_DISTILL_STATE = os.path.join(DATA_DIR, "distill_state.json")


def _load_last_distill(path=None):
    try:
        with open(path or _DISTILL_STATE, encoding="utf-8") as fh:
            return datetime.fromisoformat(json.load(fh)["last_run"])
    except Exception:
        return None


def _save_last_distill(when, path=None):
    try:
        target = path or _DISTILL_STATE
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            json.dump({"last_run": when.isoformat()}, fh)
    except Exception:
        traceback.print_exc()


async def distill_loop():
    """每天深夜蒸馏一次，记忆超过阈值时补跑（D60）。

    状态存 DATA_DIR 下的小 JSON：重启不该把当天已经跑过的再跑一遍；
    失败也别频繁重试——等下一个检查周期，别把模型额度打满。
    """
    while True:
        try:
            if distiller is not None:
                count = await asyncio.to_thread(assistant.memory.count)
                if distillation_due(
                    datetime.now(), count, _load_last_distill(),
                    hour=MEMORY_DISTILL_HOUR,
                    threshold=MEMORY_DISTILL_THRESHOLD,
                    interval_hours=MEMORY_DISTILL_INTERVAL_HOURS,
                ):
                    result = await asyncio.to_thread(distiller.run)
                    _save_last_distill(datetime.now())
                    print("🧠 记忆蒸馏：%d 组，更新 %d 条，跳过 %d 组" % (
                        result["groups"], result["updated"], result["skipped"],
                    ), flush=True)
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(MEMORY_DISTILL_CHECK_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global stt, tts, assistant, _tmp_dir, push_store, push_sender, distiller, print_store
    global _shared_llm, _shared_decision_llm
    global started_at, PRINT_FILES_DIR
    _check_exposure()
    started_at = time.time()
    print("🔧 正在初始化系统（首次会加载 STT 模型，约需几十秒）...")
    _tmp_dir = tempfile.mkdtemp(prefix="home_terminal_")
    # 空间（D68）：单家庭先只有 default，路径与升级前一致；接多成员时按人换 space_id
    space = ensure_space(DEFAULT_SPACE_ID)
    PRINT_FILES_DIR = space["print_files"]
    print_store = PrintStore(path=space["prints"])
    _purge_print_files()
    tts = TTS()
    _shared_decision_llm = build_decision_llm()
    _shared_llm = LLM()
    assistant = Assistant(llm=_shared_llm, decision_llm=_shared_decision_llm,
                          print_store=print_store, space_id=DEFAULT_SPACE_ID)
    print("🧠 决策轮模型：%s" % (
        _shared_decision_llm.model_name if _shared_decision_llm else "同回答轮"
    ))
    # 记忆蒸馏（D60）：只重算摘要，不删原文；跑在后台，失败不影响对话
    distiller = None
    if MEMORY_DISTILL_ENABLED:
        distiller = MemoryDistiller(
            assistant.memory,
            assistant.llm,
            max_groups=MEMORY_DISTILL_MAX_GROUPS,
            summary_max_chars=MEMORY_DISTILL_SUMMARY_CHARS,
        )
    if STT_ENABLED:
        stt = STT()  # 预热 FunASR
    else:
        print("🔇 语音识别已关闭（STT_ENABLED=0），只提供文字入口", flush=True)
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
    distill_task = None
    if distiller is not None:
        distill_task = asyncio.create_task(distill_loop())
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
        if distill_task is not None:
            distill_task.cancel()
            try:
                await distill_task
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
        if print_store is not None:
            print_store.close()
        if push_store is not None:
            push_store.close()
        if _tmp_dir:
            shutil.rmtree(_tmp_dir, ignore_errors=True)
        _subscribers.clear()
        stt = None
        tts = None
        assistant = None
        print_store = None
        push_store = None
        push_sender = None
        distiller = None
        print_store = None
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
    # 从到期提醒点进来时带上的任务；只影响这一轮，不落库
    task_id: Optional[int] = Field(default=None, ge=0)
    # 网页里先上传的待打印文件（D61）；只在这一轮生效
    file_id: Optional[str] = Field(default=None, max_length=64)


class ConfigUpdate(BaseModel):
    values: dict


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    due_at: Optional[str] = None
    type: Optional[str] = None


class TaskPostpone(BaseModel):
    days: int = 1
    due_at: Optional[str] = None
    # 任务卡上的固定候选；交给 agent/dates.py 算，别在客户端拼日期
    preset: Optional[str] = None


# 延期候选：前端只传 preset，日期口径和语音里的「明天/三天后/下周一」共用一套解析
POSTPONE_PRESETS = {
    "tomorrow": "明天",
    "in_3_days": "三天后",
    "next_monday": "下周一",
}


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
    # 设备工具清单（function_call 模式下 xiaozhi 会把设备端 MCP 工具一起发过来）。
    # 现在只用它判断「这台终端有没有摄像头」，不把这些工具交给我们的模型。
    tools: list = Field(default_factory=list)
    stream: bool = False


# ---------- 核心处理 ----------

def _slim_memories(memories):
    """记忆列表精简成前端展示字段。"""
    if not memories:
        return []
    if isinstance(memories[0], dict):
        return [{"text": m["text"], "time": m["time"]} for m in memories]
    return [{"text": str(m), "time": ""} for m in memories]


def _handle_voice_sync(audio_path: str, client_id: str, task_id=None,
                       assistant_obj=None) -> dict:
    """同步处理语音文件（在 to_thread 里跑：STT 识别 + 编排）。"""
    assistant_obj = assistant_obj or assistant
    result = assistant_obj.process_voice(stt, audio_path, client_id, task_id=task_id)
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


async def _chat_completion_stream(text, completion_id, created, model,
                                  device_camera=False, camera_tool=None):
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
            result = assistant.process_text(
                text, "xiaozhi", on_delta=on_delta, device_camera=device_camera
            )
            loop.call_soon_threadsafe(queue.put_nowait, ("done", result))
        except Exception as exc:
            traceback.print_exc()
            loop.call_soon_threadsafe(queue.put_nowait, ("error", str(exc)))

    worker = asyncio.create_task(asyncio.to_thread(work))

    emitted = ""
    tool_call_sent = False
    try:
        yield _sse_chunk(completion_id, created, model, {"role": "assistant"})
        while True:
            kind, payload = await queue.get()
            if kind == "delta":
                emitted += payload
                yield _sse_chunk(completion_id, created, model, {"content": payload})
                continue

            if kind == "done":
                result = payload or {}
                device_call = result.get("device_call")
                if device_call:
                    # 要看东西：交给 xiaozhi 去驱动设备拍照。照片结果由视觉端点
                    # 直接播报，不再回到这里生成第二段话。
                    arguments = json.dumps(
                        device_call.get("arguments") or {}, ensure_ascii=False
                    )
                    yield _sse_chunk(completion_id, created, model, {
                        "tool_calls": [{
                            "index": 0,
                            "id": "call_" + secrets.token_hex(8),
                            "type": "function",
                            "function": {
                                "name": camera_tool or device_call.get("name")
                                        or "self.camera.take_photo",
                                "arguments": arguments,
                            },
                        }],
                    })
                    yield _sse_chunk(completion_id, created, model, {}, "tool_calls")
                    tool_call_sent = True
                    break

                full = (result.get("reply") or "").strip()
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

        if not tool_call_sent:
            yield _sse_chunk(completion_id, created, model, {}, "stop")
        yield "data: [DONE]\n\n"
    finally:
        await worker


async def _chat_completion_reply(reply, completion_id, created, model):
    """把一次性生成的回复也按 SSE 发出去（调用方要的是流式协议）。"""
    yield _sse_chunk(completion_id, created, model, {"role": "assistant"})
    if reply:
        yield _sse_chunk(completion_id, created, model, {"content": reply})
    yield _sse_chunk(completion_id, created, model, {}, "stop")
    yield "data: [DONE]\n\n"


def _user_text_and_images(message):
    """一条 user 消息 → (文本, 图片 url 列表)。

    content 可能是字符串，也可能是多段 parts：语音终端传文本，摄像头传 image_url。
    """
    content = (message or {}).get("content")
    if isinstance(content, str):
        return content.strip(), []
    texts = []
    images = []
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if kind in (None, "text"):
                text = part.get("text")
                if isinstance(text, str):
                    texts.append(text)
            elif kind == "image_url":
                image = part.get("image_url")
                url = image.get("url") if isinstance(image, dict) else image
                if isinstance(url, str) and url:
                    images.append(url)
    return "".join(texts).strip(), images


def _last_user_message(messages):
    for message in reversed(messages or []):
        if (message or {}).get("role") == "user":
            return message
    return {}


def _last_user_text(messages):
    """OpenAI messages → 最后一条用户文本。"""
    return _user_text_and_images(_last_user_message(messages))[0]


def _normalize_tool_name(name):
    """工具名归一。

    小智把设备工具名里的点换成下划线再给 LLM（`self.camera.take_photo` →
    `self_camera_take_photo`），调用时要用它给的那个名字，不能用原始名。
    """
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


_CAMERA_TOOL_KEY = _normalize_tool_name("self.camera.take_photo")


def _camera_tool_name(tools):
    """在设备工具清单里找拍照工具，返回请求里**原样**的名字。

    返回 None 表示这台终端没有摄像头。名字可能带点也可能带下划线，两种都认。
    """
    for tool in tools or []:
        function = (tool or {}).get("function") or {}
        name = function.get("name") or ""
        if name and _normalize_tool_name(name) == _CAMERA_TOOL_KEY:
            return name
    return None


def _has_camera_tool(tools):
    """设备工具清单里有没有拍照（有才把 camera_look 给模型看）。"""
    return _camera_tool_name(tools) is not None


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


@app.get("/healthz")
async def healthz():
    """容器/反代探活：不鉴权、不带数据，只回答“进程起没起来”。

    别往这里加统计或配置——这个地址通常会对整条反代链路开放。
    """
    if assistant is None:
        raise HTTPException(status_code=503, detail="尚未就绪")
    return {"ok": True}


def _runtime_sync(data_dir=None, print_dir=None):
    """运行观察（D67）：跑多久、数据占多少、磁盘还剩多少。

    只数空间目录顶层的库、打印目录和思考日志，不去 walk `work/`、`home/`
    —— 那里可能有几百 MB 的 pip 包和模型，走一遍很慢且对判断没帮助。
    """
    data_dir = DATA_DIR if data_dir is None else data_dir
    print_dir = PRINT_FILES_DIR if print_dir is None else print_dir
    core_bytes = 0
    for name in ("memory.db", "items.db", "tasks.db", "facts.db",
                 "agent.db", "push.db", "prints.db", "thinking.jsonl",
                 "distill_state.json"):
        path = os.path.join(data_dir, name)
        try:
            core_bytes += os.path.getsize(path)
        except OSError:
            pass
    print_files = 0
    print_bytes = 0
    try:
        for entry in os.scandir(print_dir):
            if entry.is_file(follow_symlinks=False):
                print_files += 1
                print_bytes += entry.stat().st_size
    except OSError:
        pass
    try:
        usage = shutil.disk_usage(data_dir)
        total, free = usage.total, usage.free
    except OSError:
        total = free = 0
    return {
        "uptime_seconds": int(time.time() - started_at) if started_at else 0,
        "core_mb": round(core_bytes / 1048576, 1),
        "print_files": print_files,
        "print_mb": round(print_bytes / 1048576, 1),
        "disk_total_gb": round(total / 1073741824, 1),
        "disk_free_gb": round(free / 1073741824, 1),
    }


@app.get("/api/status")
async def api_status(space: Space = Depends(current_space),
                     _auth=Depends(require_api_token)):
    memory_count, item_count, task_count = await asyncio.to_thread(
        lambda: (
            space.assistant.memory.count(),
            space.assistant.items.count(),
            space.assistant.tasks.count(),
        )
    )
    push_count = await asyncio.to_thread(push_store.count_active) if push_store else 0
    runtime = await asyncio.to_thread(
        _runtime_sync, space.dir, space.print_dir
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
        "push_enabled": bool(push_sender is not None and push_sender.available),
        "push_subscriptions": push_count,
        "device_channels": len(_device_subscribers),  # 小智提醒桥接器在线数
        "sessions": len(space.assistant.sessions),
        "space_id": space.id,
        "runtime": runtime,  # 跑多久 / 数据多大 / 磁盘还剩多少（D67）
    }


@app.get("/api/spaces")
async def api_spaces(_auth=Depends(require_api_token)):
    """现有空间（D71）。只列名字和默认标记，不读别的空间的库。

    计数请带上 `X-Space-Id` 请求头去调 `/api/status`——这里刻意不做汇总，
    否则以后空间多了会把每个库都打开一遍。
    """
    ids = await asyncio.to_thread(list_spaces)
    return {
        "ok": True,
        "spaces": [
            {"id": space_id, "default": space_id == DEFAULT_SPACE_ID}
            for space_id in ids
        ],
        "max_spaces": MAX_SPACES,
    }


@app.get("/api/stats")
async def api_stats(space: Space = Depends(current_space),
                    _auth=Depends(require_api_token)):
    data = await asyncio.to_thread(_stats_sync, space)
    data["ok"] = True
    data["space_id"] = space.id
    return data


@app.get("/api/board")
async def api_board(refresh: int = 0, space: Space = Depends(current_space),
                    _auth=Depends(require_api_token)):
    """网页看板的一次性数据源（D82）。

    `refresh=1` 跳过 30 秒缓存：看板上点了「办了/明天」之后要立刻看到变化。
    """
    return await asyncio.to_thread(_board_sync, space, bool(refresh))


@app.get("/api/weather")
async def api_weather(refresh: int = 0, _auth=Depends(require_api_token)):
    """结构化天气（看板顶栏 + 三天预报弹层）。失败时 `ok=false`，前端整块隐藏。"""
    return await asyncio.to_thread(_weather_snapshot, bool(refresh))


@app.get("/api/thinking")
async def api_thinking(limit: int = 50, _auth=Depends(require_api_token)):
    """观察「模型自己决定的思考开关」（D63）：最近 N 轮 + 粗统计。

    只读调试数据，不是用户资产；`data/thinking.jsonl` 删了也不影响任何功能。
    """
    rows = await asyncio.to_thread(thinking.tail, limit)
    stats = await asyncio.to_thread(thinking.summary, max(limit, 200))
    return {
        "ok": True,
        "enabled": bool(thinking.ENABLED),
        "items": rows,
        "summary": stats,
    }


@app.get("/api/prints")
async def api_prints(q: str = "", status: str = "", limit: int = 20,
                     offset: int = 0, space: Space = Depends(current_space),
                     _auth=Depends(require_api_token)):
    """打印历史（D65）：名字、时间、份数、结果。不返回容器内路径。

    `status` 取 ready / printing / printed / failed / cancelled。
    返回里的 `id` 就是上传时的 file_id，可以带回去再打一份。
    """
    store = space.print_store
    if store is None:
        raise HTTPException(status_code=503, detail="服务尚未就绪")
    if status and status not in PRINT_STATUSES:
        raise HTTPException(status_code=400, detail="status 不认识")
    rows, total = await asyncio.to_thread(
        store.list, q, status, limit, offset
    )
    keep = ("id", "created_at", "name", "kind", "size", "pages",
            "copies", "status", "printer", "job_id", "error", "source")
    return {
        "ok": True,
        "total": total,
        "items": [{key: row.get(key) for key in keep} for row in rows],
    }


@app.get("/api/printer")
async def api_printer(refresh: int = 0, _auth=Depends(require_api_token)):
    """打印机状态：网页先看能不能打，别等传完文件才报错。

    `refresh=1` 绕过 30 秒缓存（概览页的重查按钮）；打印机断电时
    `lpstat` 可能等到超时，所以状态本身带缓存，别让轮询把请求线程占住。
    """
    if assistant is None:
        raise HTTPException(status_code=503, detail="服务尚未就绪")
    detail = await asyncio.to_thread(assistant.printer_status, bool(refresh))
    payload = {"ok": True}
    payload.update(detail)
    return payload


@app.get("/api/memories")
async def api_memories(q: str = "", sort: str = "newest", limit: int = 20,
                       offset: int = 0, space: Space = Depends(current_space),
                       _auth=Depends(require_api_token)):
    if sort not in ("newest", "oldest"):
        raise HTTPException(status_code=400, detail="sort 必须是 newest/oldest")
    if q:
        rows = await asyncio.to_thread(
            space.assistant.memory.search_details, q, 500
        )
    else:
        rows = await asyncio.to_thread(space.assistant.memory.list_all, 500)
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
                    offset: int = 0, space: Space = Depends(current_space),
                    _auth=Depends(require_api_token)):
    if status and status not in ("active", "reminded", "expired"):
        raise HTTPException(status_code=400, detail="status 必须是 active/reminded/expired")
    rows = await asyncio.to_thread(space.assistant.items.list_all, status)
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
                    space: Space = Depends(current_space),
                    _auth=Depends(require_api_token)):
    allowed = ("pending", "reminded", "done", "cancelled")
    if status and status not in allowed:
        raise HTTPException(status_code=400, detail="status 无效")
    if sort not in ("due", "created", "title"):
        raise HTTPException(status_code=400, detail="sort 必须是 due/created/title")
    rows = await asyncio.to_thread(space.assistant.tasks.list_all, status)
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
                    space: Space = Depends(current_space),
                    _auth=Depends(require_api_token)):
    rows = await asyncio.to_thread(
        space.assistant.facts.search, q or None, None, None, 500
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
                       offset: int = 0, space: Space = Depends(current_space),
                       _auth=Depends(require_api_token)):
    rows = await asyncio.to_thread(
        space.assistant.clarifier.store.list_entities, type
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
                            space: Space = Depends(current_space),
                            _auth=Depends(require_api_token)):
    _require_confirm(confirm)
    ok = await asyncio.to_thread(space.assistant.memory.delete_by_id, memory_id)
    if not ok:
        raise HTTPException(status_code=404, detail="记忆不存在")
    return {"ok": True}


@app.delete("/api/items/{item_id}")
async def api_delete_item(item_id: int, confirm: bool = False,
                          space: Space = Depends(current_space),
                          _auth=Depends(require_api_token)):
    _require_confirm(confirm)
    deleted = await asyncio.to_thread(
        space.assistant.items.delete_by_id, item_id
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="物品不存在")
    return {"ok": True, "deleted": deleted}


@app.delete("/api/facts/{fact_id}")
async def api_delete_fact(fact_id: int, confirm: bool = False,
                          space: Space = Depends(current_space),
                          _auth=Depends(require_api_token)):
    _require_confirm(confirm)
    deleted = await asyncio.to_thread(
        space.assistant.facts.delete_by_id, fact_id
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="事实不存在")
    return {"ok": True, "deleted": deleted}


@app.delete("/api/tasks/{task_id}")
async def api_delete_task(task_id: int, confirm: bool = False,
                          space: Space = Depends(current_space),
                          _auth=Depends(require_api_token)):
    _require_confirm(confirm)
    deleted = await asyncio.to_thread(
        space.assistant.tasks.delete_by_id, task_id
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"ok": True, "deleted": deleted}


@app.patch("/api/tasks/{task_id}")
async def api_update_task(task_id: int, req: TaskUpdate,
                          space: Space = Depends(current_space),
                          _auth=Depends(require_api_token)):
    if req.title is None and req.due_at is None and req.type is None:
        raise HTTPException(status_code=400, detail="没有可更新的字段")
    try:
        ok = await asyncio.to_thread(
            space.assistant.tasks.update,
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
async def api_task_complete(task_id: int,
                            space: Space = Depends(current_space),
                            _auth=Depends(require_api_token)):
    ok = await asyncio.to_thread(space.assistant.tasks.complete, task_id)
    if not ok:
        raise HTTPException(status_code=404, detail="任务不存在或已完成")
    # 周期任务「完成」只是跳过这一次：把下一次的时间带回前端，卡片别写「已完成」
    task = await asyncio.to_thread(space.assistant.tasks.get, task_id)
    return {"ok": True, "task": task}


@app.post("/api/tasks/{task_id}/cancel")
async def api_task_cancel(task_id: int,
                          space: Space = Depends(current_space),
                          _auth=Depends(require_api_token)):
    ok = await asyncio.to_thread(space.assistant.tasks.cancel, task_id)
    if not ok:
        raise HTTPException(status_code=404, detail="任务不存在或已结束")
    return {"ok": True}


@app.post("/api/tasks/{task_id}/postpone")
async def api_task_postpone(task_id: int, req: TaskPostpone,
                            space: Space = Depends(current_space),
                            _auth=Depends(require_api_token)):
    due_at = (req.due_at or "").strip()
    if req.preset:
        text = POSTPONE_PRESETS.get(req.preset)
        if not text:
            raise HTTPException(status_code=400, detail="preset 无效")
        parsed = parse_date_text(text)
        if not parsed:
            raise HTTPException(status_code=400, detail="无法计算延期日期")
        due_at = parsed["date"]
    try:
        if due_at:
            task = await asyncio.to_thread(
                space.assistant.tasks.postpone, task_id, due_at=due_at,
            )
        else:
            task = await asyncio.to_thread(
                space.assistant.tasks.postpone, task_id, req.days,
            )
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
async def api_ask(req: AskRequest, space: Space = Depends(current_space),
                  _auth=Depends(require_api_token)):
    """整句交给 Agent（设备端 ask_xiaoma 工具的后端）。

    client_id 决定多轮上下文与待澄清动作的归属，设备应传稳定的设备 ID。
    """
    if space.assistant is None:
        raise HTTPException(status_code=503, detail="服务尚未就绪")
    client_id = req.client_id or "xiaozhi"
    try:
        result = await asyncio.to_thread(
            space.assistant.process_text, req.text, client_id
        )
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

    message = _last_user_message(req.messages)
    text, images = _user_text_and_images(message)

    created = int(time.time())
    completion_id = "chatcmpl-" + secrets.token_hex(8)
    model = req.model or "xiaoma"

    # 看图：摄像头拍完把图传到小智的 vision 端点，那边再转到这里。
    # 照片只在这一句里用一次，不落盘、不进记忆，也不走 agent（不是一次对话）。
    if images:
        reply = await asyncio.to_thread(
            assistant.describe_image,
            text or "看看画面里有什么",
            images,
            client_id="xiaozhi",
        )
        if req.stream:
            return StreamingResponse(
                _chat_completion_reply(reply, completion_id, created, model),
                media_type="text/event-stream",
            )
        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": reply},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": len(text), "completion_tokens": len(reply),
                      "total_tokens": len(text) + len(reply)},
        }

    if not text:
        raise HTTPException(status_code=400, detail="messages 里没有用户文本")

    # 有摄像头才让模型用 camera_look；没摄像头就别让它说「我看到了」。
    # 名字要用请求里给的那个（小智会把点换成下划线），否则它认不出这次调用。
    camera_tool = _camera_tool_name(req.tools)
    device_camera = camera_tool is not None

    if req.stream:
        # 先把 head 发出去，再在后台线程跑 agent loop，按 delta 实时下发，
        # 让语音端第一句就能开始合成。
        return StreamingResponse(
            _chat_completion_stream(
                text, completion_id, created, model, device_camera, camera_tool
            ),
            media_type="text/event-stream",
        )

    try:
        result = await asyncio.to_thread(
            assistant.process_text, text, "xiaozhi", device_camera=device_camera
        )
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"处理失败: {exc}")

    # 要看东西：不生成回复，回一个 tool_calls 让 xiaozhi 驱动设备拍照
    device_call = result.get("device_call")
    if device_call:
        arguments = json.dumps(device_call.get("arguments") or {}, ensure_ascii=False)
        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_" + secrets.token_hex(8),
                        "type": "function",
                        "function": {
                            "name": camera_tool or device_call.get("name")
                                    or "self.camera.take_photo",
                            "arguments": arguments,
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": len(text), "completion_tokens": 0,
                      "total_tokens": len(text)},
        }

    reply = result.get("reply", "") or ""

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
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
async def api_chat(req: ChatRequest, space: Space = Depends(current_space),
                   _auth=Depends(require_api_token)):
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="text 不能为空")

    attachment = None
    if req.file_id:
        attachment = _get_print_file(
            req.file_id, req.client_id, store=space.print_store
        )
        if attachment is None:
            raise HTTPException(status_code=400, detail="文件已过期，请重新上传")

    try:
        # 编排是同步阻塞（memory 检索 + LLM 调用），丢线程池避免卡事件循环
        result = await asyncio.to_thread(
            space.assistant.process_text, req.text, req.client_id,
            task_id=req.task_id, attachment=attachment,
        )
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


@app.post("/api/print/files")
async def api_print_upload(file: UploadFile = File(...), client_id: str = Form(""),
                           space: Space = Depends(current_space),
                           _auth=Depends(require_api_token)):
    """接收待打印文件（D61/D65）：落持久目录，返回 file_id，随下一条聊天消息使用。"""
    store = space.print_store
    if space.assistant is None or store is None:
        raise HTTPException(status_code=503, detail="服务尚未就绪")
    # 打印机是物理设备，不分空间：状态统一用默认那份（只查一次 CUPS）
    detail = await asyncio.to_thread(assistant.printer_status)
    available = detail.get("available")
    if not available and detail.get("state") == "checking":
        # 后台正在检测（打印机断电要等超时）：别急着拒绝，用启动时的结论兜一下
        available = bool(getattr(assistant, "printer_ready", False))
    if not available:
        reason = detail.get("error") or (
            "正在检测打印机，稍后再试"
            if detail.get("state") == "checking" else "Linux 服务器需要 CUPS"
        )
        raise HTTPException(status_code=503, detail="打印机不可用：%s" % reason)
    if len(client_id) > MAX_CLIENT_ID_CHARS:
        raise HTTPException(status_code=422, detail="client_id 过长")

    original = os.path.basename(file.filename or "").strip()
    ext = os.path.splitext(original)[1].lower()
    if not original or ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail="不支持的文件类型；支持 PDF、图片、TXT/Markdown、Office",
        )

    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="文件是空的")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="文件太大，最多 10MB")

    file_id = uuid.uuid4().hex
    folder = space.print_dir
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, file_id + ext)
    with open(path, "wb") as fh:
        fh.write(data)
    try:
        validate_file(path, original)
    except PrintError as exc:
        try:
            os.remove(path)
        except OSError:
            pass
        raise HTTPException(status_code=415, detail=str(exc))

    kind = kind_of(original)
    if kind == "office" and not office_available():
        try:
            os.remove(path)
        except OSError:
            pass
        raise HTTPException(
            status_code=415,
            detail="服务器没装 LibreOffice，Office 文档先另存为 PDF 再发",
        )
    pages = pdf_page_count(path) if kind == "pdf" else None
    _purge_print_files(store=store)
    store.add(
        file_id, original, path, kind=kind, size=len(data),
        pages=pages, client_id=(client_id or "").strip(), source="upload",
    )
    return {
        "file_id": file_id,
        "name": original,
        "kind": kind,
        "size": len(data),
        "pages": pages,
    }


@app.post("/api/voice")
async def api_voice(file: UploadFile = File(...), client_id: str = Form(""),
                    task_id: Optional[int] = Form(None),
                    space: Space = Depends(current_space),
                    _auth=Depends(require_api_token)):
    if stt is None:
        raise HTTPException(
            status_code=503, detail="语音识别未启用（STT_ENABLED=0）"
        )
    if len(client_id) > MAX_CLIENT_ID_CHARS:
        raise HTTPException(status_code=422, detail="client_id 过长")
    if task_id is not None and task_id < 0:
        raise HTTPException(status_code=422, detail="task_id 无效")
    if not file.filename or not file.filename.lower().endswith(".wav"):
        raise HTTPException(status_code=400, detail="仅支持 WAV 音频")

    ext = ".wav"
    tmp_path = None
    try:
        tmp_path = await _save_upload(file, ext)
        async with stt_lock:
            result = await asyncio.to_thread(
                _handle_voice_sync, tmp_path, client_id, task_id,
                space.assistant,
            )
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

    _check_exposure()
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT, log_level="info")
