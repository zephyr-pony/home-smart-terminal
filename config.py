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


def _get_bool(key, default):
    """1/true/yes/on 都算开；其余按关处理。"""
    val = str(_get(key, "1" if default else "0")).strip().lower()
    return val in ("1", "true", "yes", "on")


def _get_clock(key, default):
    """HH:MM → (时, 分)。写错就退回默认值：一个配置项不该拦住启动。"""
    raw = str(_get(key, "") or "").strip()
    try:
        hour, _, minute = raw.partition(":")
        hour, minute = int(hour), int(minute)
        if 0 <= hour < 24 and 0 <= minute < 60:
            return hour, minute
    except ValueError:
        pass
    return default


# ---- LLM（远端 OpenAI 兼容网关）----
LLM_BASE_URL = _get("LLM_BASE_URL", "")
LLM_API_KEY = _get("LLM_API_KEY", "")
LLM_MODEL = _get("LLM_MODEL", "")
# 第一轮（决策/调工具）可换更快的模型；留空表示两轮都用 LLM_MODEL
LLM_MODEL_DECISION = _get("LLM_MODEL_DECISION", "")
# 思考模式：值原样传给网关（commandcode: off 关闭 / low…max 按档位；aisp: none 关闭）
# 留空表示不传该参数；网关不认这个字段时留空，改用 LLM_CHAT_TEMPLATE_KWARGS
LLM_REASONING_EFFORT = _get("LLM_REASONING_EFFORT", "")
# 模型自己调 set_thinking(true) 后用的思考档位；语音场景默认 off，需要时再升。
LLM_THINKING_EFFORT = _get("LLM_THINKING_EFFORT", "medium")
# 记不记录「这轮有没有开思考」（data/thinking.jsonl，调试用，可关）
THINKING_LOG_ENABLED = _get_int("THINKING_LOG_ENABLED", 1)
# 部分后端（llama.cpp / vLLM）用 chat_template_kwargs 关思考，值是 JSON，如
# LLM_CHAT_TEMPLATE_KWARGS={"enable_thinking": false}
LLM_CHAT_TEMPLATE_KWARGS = _get("LLM_CHAT_TEMPLATE_KWARGS", "")
# 可选：访问 LLM 网关走本机代理，如 http://127.0.0.1:10808。
# 留空 = 交给 requests 的默认行为。2026-11-23 实测：同一条请求直连 65 s、
# 走本机代理 4.7 s（NO_PROXY 设了值会让 requests 跳过 Windows 注册表代理）。
LLM_PROXY = _get("LLM_PROXY", "")
# 上网搜索/抓页面走哪个代理；留空 = 直连（默认不借 LLM_PROXY）。
WEB_PROXY = _get("WEB_PROXY", "")

# 看一眼之后，画面描述在会话里保留多久（秒）。只在内存里，不进记忆库：
# 用户拍完接着问「那是什么牌子」要能接上，但三天后还拿旧画面回答就是错的。
VISION_CONTEXT_SECONDS = _get_int("VISION_CONTEXT_SECONDS", 180)

# ---- TTS 语音合成 ----
TTS_VOICE = _get("TTS_VOICE", "zh-CN-XiaoxiaoNeural")

# ---- STT 语音识别 ----
# 关掉后服务端不加载 FunASR（首跑省 900MB 下载），/api/voice 返回 503；文字入口不受影响
STT_ENABLED = _get_bool("STT_ENABLED", True)
STT_MODEL = _get("STT_MODEL", "iic/SenseVoiceSmall")
STT_LANGUAGE = _get("STT_LANGUAGE", "zh")

# ---- 数据目录 ----
# 所有库默认都在 DATA_DIR 下；单独设 *_DB_PATH 的仍以它为优先（迁移/测试用）
DATA_DIR = _get("DATA_DIR", "./data")


def _db_path(filename, key):
    return _get(key, os.path.join(DATA_DIR, filename))


# ---- 记忆存储 ----
MEMORY_DB_PATH = _db_path("memory.db", "MEMORY_DB_PATH")
ITEMS_DB_PATH = _db_path("items.db", "ITEMS_DB_PATH")

# ---- Agent 状态（实体 / 待澄清动作）----
AGENT_DB_PATH = _db_path("agent.db", "AGENT_DB_PATH")
PENDING_ACTION_TTL_SECONDS = _get_int("PENDING_ACTION_TTL_SECONDS", 30 * 60)
PROPOSAL_DEDUPE_SECONDS = _get_int("PROPOSAL_DEDUPE_SECONDS", 5 * 60)
# 弱模型模式：写操作是否先复述参数等确认
#   0/off     关闭（默认，大模型直接用）
#   1/all     四个写工具全部确认
#   tasks     只确认任务（误提醒代价最高），物品/记忆/事实直接写
CONFIRM_WRITE_ACTIONS = _get("CONFIRM_WRITE_ACTIONS", "0")

# ---- 通用任务与结构化事实 ----
TASKS_DB_PATH = _db_path("tasks.db", "TASKS_DB_PATH")
FACTS_DB_PATH = _db_path("facts.db", "FACTS_DB_PATH")

# ---- 对话记录与画像提炼（D84）----
# 只留最近几天：给「昨天说到哪」和夜间画像提炼当原料，过期自动删。
# 原文是用户说过的话，属于敏感数据——保留期要短，设置页能看、能清、能关。
DIALOGUE_DB_PATH = _db_path("dialogue.db", "DIALOGUE_DB_PATH")
DIALOGUE_ENABLED = _get_int("DIALOGUE_ENABLED", 1)
DIALOGUE_KEEP_DAYS = _get_int("DIALOGUE_KEEP_DAYS", 7)
# 夜间从昨天的对话里找稳定偏好，只写「候选」等用户点头（D59 的确认制不变）
PROFILE_MINING_ENABLED = _get_int("PROFILE_MINING_ENABLED", 1)

# ---- 音频录制 ----
SAMPLE_RATE = _get_int("SAMPLE_RATE", 16000)
CHUNK_SIZE = _get_int("CHUNK_SIZE", 1024)
MAX_RECORD_SECONDS = _get_int("MAX_RECORD_SECONDS", 15)
SILENCE_THRESHOLD = _get_float("SILENCE_THRESHOLD", 0.015)
SILENCE_DURATION = _get_float("SILENCE_DURATION", 1.2)

# ---- Web 服务端 ----
SERVER_HOST = _get("SERVER_HOST", "127.0.0.1")
SERVER_PORT = _get_int("SERVER_PORT", 8000)
API_TOKEN = _get("API_TOKEN", "")
# MCP 接入点（小智设备端出站连接）。空 = 不启用，避免默认开一个能写库的入口
MCP_TOKEN = _get("MCP_TOKEN", "")
# 设备待机屏只读接口（时间/天气/今天待办）。空 = 不启用；单独一个令牌，
# 别把完整 API_TOKEN 编进固件——设备丢了也只泄露「今天有什么事」。
DISPLAY_TOKEN = _get("DISPLAY_TOKEN", "")
MAX_TEXT_CHARS = _get_int("MAX_TEXT_CHARS", 2000)
MAX_CLIENT_ID_CHARS = _get_int("MAX_CLIENT_ID_CHARS", 128)
MAX_UPLOAD_BYTES = _get_int("MAX_UPLOAD_BYTES", 10 * 1024 * 1024)
MAX_AUDIO_SECONDS = _get_int("MAX_AUDIO_SECONDS", 30)
SESSION_TTL_SECONDS = _get_int("SESSION_TTL_SECONDS", 24 * 60 * 60)
MAX_SESSIONS = _get_int("MAX_SESSIONS", 1000)

# ---- 唤醒词（CLI 常驻监听）----
WAKE_WORD = _get("WAKE_WORD", "小马")
WAKE_WORD_MAX_SECONDS = _get_int("WAKE_WORD_MAX_SECONDS", 8)
WAKE_ACK_TEXT = _get("WAKE_ACK_TEXT", "我在")

# ---- 多轮对话上下文（注入 agent 的历史轮次）----
SESSION_CONTEXT_TURNS = _get_int("SESSION_CONTEXT_TURNS", 5)
SESSION_CONTEXT_MAX_CHARS = _get_int("SESSION_CONTEXT_MAX_CHARS", 300)

# ---- 智能家居（Home Assistant 当设备网关）----
# HA 里装小米官方集成（Xiaomi Home），小马只跟 HA 的本地 REST 说话，
# 不直接碰小米云，也不碰 miIO 协议。
HOME_BASE_URL = _get("HOME_BASE_URL", "http://127.0.0.1:8123")
# 长期访问令牌（HA 个人资料 → 安全 → 长期访问令牌）。留空则不注册设备工具。
HOME_TOKEN = _get("HOME_TOKEN", "")
HOME_TIMEOUT_SECONDS = _get_float("HOME_TIMEOUT_SECONDS", 5)
# 小马认识哪些设备：逗号分隔，可写成 `entity_id=别名`。
# MIoT 会把每个属性都变成一个实体（一台空调 60 多个），不做白名单根本没法读。
# 留空则回退到自动过滤，效果很差，仅用于快速看一眼。
HOME_DEVICES = _get("HOME_DEVICES", "")
# 允许语音直接控制的设备：逗号分隔 entity_id，或 `light.` 这样的域前缀。
# 留空 = 全部只读；门锁/燃气/热水器必须显式列出且每次确认。
HOME_CONTROL_ALLOW = _get("HOME_CONTROL_ALLOW", "")
# 设备主动提醒：滤芯余量跌破这个百分比就提醒；同一设备多久内不重复提醒
HOME_FILTER_ALERT_PERCENT = _get_float("HOME_FILTER_ALERT_PERCENT", 10)
HOME_ALERT_COOLDOWN_HOURS = _get_int("HOME_ALERT_COOLDOWN_HOURS", 24)
# 盯哪些 event 实体（逗号分隔），如摄像头「有人出现了」。留空 = 不盯，
# 因为移动侦测的触发频率按小时算，默认开会把客厅变成刷屏机器。
HOME_WATCH_EVENTS = _get("HOME_WATCH_EVENTS", "")
HOME_EVENT_COOLDOWN_SECONDS = _get_int("HOME_EVENT_COOLDOWN_SECONDS", 600)
# 门锁事件里的「操作ID」是锁上报的凭据编号，换成名字才好念：
# `501=爸爸,2001=爸爸,2004=妈妈`。人脸和指纹各一个号，同一人可能要写两遍。
# 不配就念「501 号用户」——听见编号再补，比先去哪里查编号现实。
HOME_LOCK_USERS = _get("HOME_LOCK_USERS", "")
# 门锁事件的冷却（秒）：默认 0 = 每条都报。10 分钟里进来两个人，
# 第二个不能被普通事件的冷却吞掉；重复上报靠时间戳去重。
HOME_LOCK_COOLDOWN_SECONDS = _get_int("HOME_LOCK_COOLDOWN_SECONDS", 0)
# 设备巡检间隔：门铃/有人停留这类事件等 60 秒就没意义了，单独跑短周期
HOME_SCAN_SECONDS = _get_int("HOME_SCAN_SECONDS", 15)

# ---- 天气 ----
# 默认走 Open-Meteo（不需要 key）：填 WEATHER_CITY 即可；配了和风的
# Host+Key 则优先用和风（中国局地数据更准）。两者都没配就不注册天气工具。
WEATHER_API_HOST = _get("WEATHER_API_HOST", "")
WEATHER_API_KEY = _get("WEATHER_API_KEY", "")
WEATHER_CITY = _get("WEATHER_CITY", "")
# Open-Meteo 的城市名匹配偶尔会撞上同名小地方（「烟台」命中辽宁小村），
# 填了经纬度就跳过 geocoding，最稳。
WEATHER_LAT = _get("WEATHER_LAT", "")
WEATHER_LON = _get("WEATHER_LON", "")

# ---- 热点上下文（D58）----
# 会话开始时把画像、常用事实、最近记忆注入 system prompt，会话内不变（冻结快照）。
# 目的是省掉「查一次记忆」那一轮 LLM（语音场景约 2-5 秒），所以必须小：
# 预算超了就按 画像 > 常用事实 > 最近记忆 的优先级截断。
HOT_CONTEXT_ENABLED = _get_bool("HOT_CONTEXT_ENABLED", True)
HOT_CONTEXT_MAX_CHARS = _get_int("HOT_CONTEXT_MAX_CHARS", 900)
HOT_CONTEXT_FACTS = _get_int("HOT_CONTEXT_FACTS", 6)
HOT_CONTEXT_MEMORIES = _get_int("HOT_CONTEXT_MEMORIES", 5)

# ---- 记忆蒸馏（D60）----
# 合并同主题记忆，只重算 summary、不删原文。深夜跑一次，超阈值再补跑。
MEMORY_DISTILL_ENABLED = _get_bool("MEMORY_DISTILL_ENABLED", True)
MEMORY_DISTILL_HOUR = _get_int("MEMORY_DISTILL_HOUR", 3)
MEMORY_DISTILL_THRESHOLD = _get_int("MEMORY_DISTILL_THRESHOLD", 500)
MEMORY_DISTILL_INTERVAL_HOURS = _get_int("MEMORY_DISTILL_INTERVAL_HOURS", 6)
MEMORY_DISTILL_MAX_GROUPS = _get_int("MEMORY_DISTILL_MAX_GROUPS", 10)
MEMORY_DISTILL_SUMMARY_CHARS = _get_int("MEMORY_DISTILL_SUMMARY_CHARS", 60)
MEMORY_DISTILL_CHECK_SECONDS = _get_int("MEMORY_DISTILL_CHECK_SECONDS", 900)

# ---- 打印（D61）----
# 部署在 Linux，走 CUPS（lp / lpstat）；Windows 打印队列不在支持范围。
PRINTER_ENABLED = _get_bool("PRINTER_ENABLED", True)
# CUPS 队列名（`lpstat -a` 第一列）；空 = 系统默认队列
PRINTER_NAME = _get("PRINTER_NAME", "")
# 远程 CUPS 服务器（host:port，如 192.168.1.15:631）；空 = 本机 CUPS。
# 容器部署时填家里共享打印机那台 CUPS，容器里不用自己跑 cupsd。
PRINTER_SERVER = _get("PRINTER_SERVER", "")
# 允许使用的队列（逗号分隔）；空 = 只允许 PRINTER_NAME / 系统默认
PRINTER_ALLOWED = _get("PRINTER_ALLOWED", "")
PRINTER_MAX_PAGES = _get_int("PRINTER_MAX_PAGES", 50)
PRINTER_MAX_COPIES = _get_int("PRINTER_MAX_COPIES", 20)
# 打印记录（D65）：文件留几天（在 DATA_DIR/print 里，连历史一起进 prints.db）
PRINTS_DB_PATH = _db_path("prints.db", "PRINTS_DB_PATH")
PRINT_KEEP_DAYS = _get_int("PRINT_KEEP_DAYS", 7)
# 打印文件放哪（要在卷里，容器重建不丢；/tmp 会被清掉）
PRINT_FILES_DIR = _get("PRINT_FILES_DIR", os.path.join(DATA_DIR, "print"))
# 多久内算「刚才的文件」；说「再打一份」时自动接上（重启后也算）
PRINT_REUSE_HOURS = _get_int("PRINT_REUSE_HOURS", 24)

# ---- 到期提醒 ----
REMINDER_DAYS = _get_int("REMINDER_DAYS", 3)                    # 未来几天内算"即将到期"
REMINDER_INTERVAL_SECONDS = _get_int("REMINDER_INTERVAL_SECONDS", 60)  # 后台扫描间隔
REMINDER_COOLDOWN_HOURS = _get_int("REMINDER_COOLDOWN_HOURS", 24)      # 同一物品再次提醒的间隔（0=只提醒一次）
# 软提醒的兜底时刻（D81）：mode=soft 的任务和物品到期，白天只在聊天里顺口提；
# 到这个点还没机会说，才按准点提醒走一次（设备 → 网页 → push）。
SOFT_REMINDER_LAST_CALL = _get("SOFT_REMINDER_LAST_CALL", "20:30")
SOFT_REMINDER_LAST_HOUR, SOFT_REMINDER_LAST_MINUTE = _get_clock(
    "SOFT_REMINDER_LAST_CALL", (20, 30)
)

# ---- Web Push（页面关闭时的到期提醒）----
# 私钥用 base64(PEM) 单行存放，.env 里不能有多行值；用 scripts/gen_vapid.py 生成。
PUSH_ENABLED = _get_bool("PUSH_ENABLED", False)
PUSH_DB_PATH = _db_path("push.db", "PUSH_DB_PATH")
VAPID_PUBLIC_KEY = _get("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = _get("VAPID_PRIVATE_KEY", "")
VAPID_SUBJECT = _get("VAPID_SUBJECT", "mailto:noreply@example.com")
PUSH_TTL_SECONDS = _get_int("PUSH_TTL_SECONDS", 12 * 60 * 60)   # 推送在推送服务里的存活时间
PUSH_MAX_FAILURES = _get_int("PUSH_MAX_FAILURES", 5)            # 连续失败几次后标记停用
