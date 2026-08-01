"""集中配置 - 所有可调参数从 .env 加载

密钥等敏感信息只放在本地 .env（已被 .gitignore 排除），
随 GitHub 上传的只有 .env.example 模板。
"""
import os

from dotenv import load_dotenv

load_dotenv()


def _get(key, default):
    """读取环境变量，空值回退默认。"""
    val = os.environ.get(key)
    return val if val not in (None, "") else default


def _get_float(key, default):
    try:
        return float(_get(key, default))
    except (TypeError, ValueError):
        return default


def _get_int(key, default):
    try:
        return int(float(_get(key, default)))
    except (TypeError, ValueError):
        return default


# ---- LLM（远端 OpenAI 兼容网关）----
LLM_BASE_URL = _get("LLM_BASE_URL", "")
LLM_API_KEY = _get("LLM_API_KEY", "")
LLM_MODEL = _get("LLM_MODEL", "")

# ---- TTS 语音合成 ----
TTS_VOICE = _get("TTS_VOICE", "zh-CN-XiaoxiaoNeural")

# ---- STT 语音识别 ----
STT_MODEL = _get("STT_MODEL", "iic/SenseVoiceSmall")
STT_LANGUAGE = _get("STT_LANGUAGE", "zh")

# ---- 记忆存储 ----
MEMORY_DB_PATH = _get("MEMORY_DB_PATH", "./data/memory.db")

# ---- 音频录制 ----
SAMPLE_RATE = _get_int("SAMPLE_RATE", 16000)
CHUNK_SIZE = _get_int("CHUNK_SIZE", 1024)
MAX_RECORD_SECONDS = _get_int("MAX_RECORD_SECONDS", 15)
SILENCE_THRESHOLD = _get_float("SILENCE_THRESHOLD", 0.015)
SILENCE_DURATION = _get_float("SILENCE_DURATION", 1.2)
