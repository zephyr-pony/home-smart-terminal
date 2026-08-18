"""家庭智能终端 - FastAPI 服务端（阶段 2 + 阶段 3 物品提醒）

提供：
- POST /api/voice  语音文件 → 识别+理解+回答（返回 mp3 音频 base64）
- POST /api/chat   文字 → 理解+回答（返回 mp3 音频 base64）
- GET  /api/status 健康检查（含 due_items 到期物品提醒）
- GET  /           静态 Web 客户端（static/index.html）

启动：python server.py  （或 uvicorn server:app --host 0.0.0.0 --port 8000）
"""
import asyncio
import base64
import os
import tempfile
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from config import SERVER_HOST, SERVER_PORT, MAX_AUDIO_SECONDS, MAX_UPLOAD_BYTES
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

# 到期提醒缓存（提醒循环每分钟刷新）
due_items = []

# FunASR 非线程安全：同一时间只允许一个识别请求
stt_lock = asyncio.Semaphore(1)

# 临时音频文件保留目录（关闭时清理）
_tmp_dir = None


async def reminder_loop():
    """后台提醒循环：每分钟扫描物品到期情况，刷新 due_items。"""
    global due_items
    while True:
        try:
            due_items = assistant.items.get_due(days=3)
        except Exception:
            pass  # 扫描失败不影响服务
        await asyncio.sleep(60)


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
    yield
    reminder_task.cancel()
    print("🛑 服务关闭")


app = FastAPI(title="家庭智能终端", lifespan=lifespan)


# ---------- 请求模型 ----------

class ChatRequest(BaseModel):
    text: str
    client_id: str = ""


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
    """合成回复语音，返回 base64 mp3（空回复返回空串）。"""
    if not reply:
        return ""
    data = await tts.synthesize_bytes(reply)
    if not data:
        return ""
    return base64.b64encode(data).decode("ascii")


# ---------- API 路由 ----------

@app.get("/api/status")
async def api_status():
    return {
        "ok": True,
        "stt_ready": stt is not None,
        "memory_count": assistant.memory.count(),
        "item_count": assistant.items.count(),
        "due_items": due_items,  # 到期物品提醒（提醒循环刷新）
        "sessions": len(assistant.sessions),
    }


@app.post("/api/chat")
async def api_chat(req: ChatRequest):
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
    }


@app.post("/api/voice")
async def api_voice(file: UploadFile = File(...), client_id: str = Form("")):
    if not file.filename or not file.filename.lower().endswith((".wav", ".mp3", ".webm", ".m4a", ".ogg")):
        raise HTTPException(status_code=400, detail="仅支持 wav/mp3/webm/m4a/ogg 音频")

    # 读取并校验大小（防止恶意大文件）
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"音频过大，最大 {MAX_UPLOAD_BYTES // 1024 // 1024}MB")

    if len(data) == 0:
        raise HTTPException(status_code=400, detail="音频为空")

    # 落临时文件（STT 接口收文件路径）
    ext = os.path.splitext(file.filename)[1].lower()
    tmp_path = os.path.join(_tmp_dir, f"voice_{id(data)}_{ext}")
    try:
        with open(tmp_path, "wb") as f:
            f.write(data)

        # STT 非线程安全 + 编排阻塞：整体放线程池，Semaphore 保证同一时刻只有一个识别
        async with stt_lock:
            result = await asyncio.to_thread(_handle_voice_sync, tmp_path, client_id)
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"处理失败: {e}")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    return {
        "type": result.get("type", "empty"),
        "reply": result.get("reply", ""),
        "summary": result.get("summary", ""),
        "memories": _slim_memories(result.get("memories", [])),
        "text": result.get("text", ""),
        "audio": await _synthesize(result.get("reply", "")),
    }


# ---------- 静态客户端 ----------

@app.get("/")
async def index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT, log_level="info")
