# 家庭智能终端

以**记忆**为核心的家庭 AI 助手。通过语音记录家庭琐事（"酸奶 8 月 5 号过期"、"明天交电费"），之后随时语音提问（"酸奶还有几天过期？"），系统自动检索并回答。

## 功能特性

- 🎤 **全程语音交互**：麦克风说话 → 识别 → 理解 → 语音回答
- 🧠 **记忆为核心**：陈述事实自动记录，疑问句自动检索回答
- 🔍 **中文全文检索**：任意子串匹配，零依赖（SQLite FTS5 中文空格化方案）
- 🤖 **智能意图识别**：区分"记录/查询/闲聊"，支持纠正句（"不是今年，是今天"）
- ⚙️ **配置集中管理**：所有参数在 `.env` 中，密钥不入库

## 工作流程

```
说话 → FunASR 语音识别 → LLM 意图分类（带记忆上下文）
        ↓                        ↓
    记录事实                  查询已有记忆
    （存 SQLite）          （检索 → LLM 回答 → Edge TTS 播报）
```

## 环境要求

- Windows / Linux
- Python 3.9+
- 麦克风 + 音箱

## 快速开始

```bash
# 1. 克隆并进入目录
git clone <你的仓库地址>
cd 家庭智能终端

# 2. 创建虚拟环境并安装依赖
python -m venv .venv
.venv\Scripts\activate        # Windows
source .venv/bin/activate     # Linux/macOS
pip install -r requirements.txt

# 3. 配置 LLM 服务（OpenAI 兼容接口，如 llama.cpp / LM Studio / OneAPI 网关）
copy .env.example .env        # Windows
# cp .env.example .env        # Linux/macOS
# 编辑 .env，填入 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL

# 4. 运行

**Web 服务端（手机/平板访问，推荐）**
```bash
start.bat            # Windows 一键启动
# 或 python server.py
```
浏览器打开 `http://<电脑IP>:8000`（本机测试用 `http://127.0.0.1:8000`），
支持按住说话（麦克风）或文字聊天。首次启动加载 STT 模型约需几十秒。

> 局域网访问需放行防火墙：以管理员运行 PowerShell 执行
> `New-NetFirewallRule -DisplayName "HomeSmartTerminal" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow`

**命令行模式（本机语音交互）**
```bash
start.bat cli        # 或 python main.py
```

> 首次运行会自动下载 FunASR 语音识别模型（约 900MB），需要一些时间。

## 服务端 API

| 接口 | 方法 | 说明 |
|---|---|---|
| `/api/status` | GET | 状态、记忆/物品数量和到期缓存（配置 API_TOKEN 后需 Bearer Token） |
| `/api/chat` | POST | 文字交互 `{text, client_id}` → 回复 + mp3 音频 base64 |
| `/api/voice` | POST | WAV 语音交互（multipart: file + client_id） |

配置 `API_TOKEN` 后，三个 API 都需要请求头 `Authorization: Bearer <API_TOKEN>`。当前服务端限制文本 2000 字符、音频 10MB/30 秒；Web 页面会保存并自动发送 Token。

## 配置说明（.env）

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `LLM_BASE_URL` | （必填） | LLM 服务地址，如 `http://127.0.0.1:8081/v1` |
| `LLM_API_KEY` | （必填） | API 密钥 |
| `LLM_MODEL` | （可选） | 模型名 |
| `TTS_VOICE` | `zh-CN-XiaoxiaoNeural` | 语音角色（女声）；男声用 `zh-CN-YunxiNeural` |
| `STT_MODEL` | `iic/SenseVoiceSmall` | 语音识别模型 |
| `STT_LANGUAGE` | `zh` | 识别语言 |
| `MEMORY_DB_PATH` | `./data/memory.db` | 记忆库位置 |
| `ITEMS_DB_PATH` | `./data/items.db` | 物品库位置 |
| `SAMPLE_RATE` | `16000` | 录音采样率 |
| `CHUNK_SIZE` | `1024` | 音频块大小 |
| `MAX_RECORD_SECONDS` | `15` | CLI 最长录音时长（秒） |
| `SILENCE_THRESHOLD` | `0.015` | 静音能量阈值 |
| `SILENCE_DURATION` | `1.2` | 停顿多久视为说完（秒） |
| `SERVER_HOST` | `127.0.0.1` | 服务监听地址；局域网使用可改为 `0.0.0.0` |
| `SERVER_PORT` | `8000` | 服务端口 |
| `API_TOKEN` | 空 | API Bearer Token；局域网使用建议必填 |
| `MAX_TEXT_CHARS` | `2000` | 单次文本上限 |
| `MAX_CLIENT_ID_CHARS` | `128` | 会话 ID 上限 |
| `MAX_UPLOAD_BYTES` | `10485760` | WAV 上传上限 |
| `MAX_AUDIO_SECONDS` | `30` | WAV 音频时长上限 |
| `SESSION_TTL_SECONDS` | `86400` | 会话空闲过期时间 |
| `MAX_SESSIONS` | `1000` | 最大内存会话数 |

## 使用方式

```
按 Enter      开始语音录音（说完停顿自动结束）
直接输入文字   以文本方式交互
输入 quit     退出
```

### 示例对话

| 用户说 | 系统行为 |
|---|---|
| 我今天给鼠标换了电池 | 记录：鼠标电池今天换的 |
| 酸奶 8 月 5 号过期，放冰箱了 | 记录：酸奶过期时间、位置 |
| 我的酸奶还有几天过期？ | 检索记忆 → 回答"还有 4 天" |
| 我什么时候换的鼠标电池？ | 检索记忆 → 回答 |
| 你好 | 闲聊回复 |

## 目录结构

```
├── main.py          # CLI 入口
├── assistant.py     # 核心编排和 Agent loop
├── tools.py         # OpenAI function calling 工具注册
├── server.py        # FastAPI 服务端
├── config.py        # 集中配置（从 .env 加载）
├── audio_io.py      # PyAudio 录音与 pygame 播放
├── stt.py           # FunASR SenseVoice 语音识别
├── llm.py           # OpenAI 兼容 LLM 交互
├── memory.py        # SQLite FTS5 中文记忆检索
├── items.py         # SQLite 物品管理（名称/到期日）
├── tts.py           # Edge TTS 语音合成
├── static/index.html # Web 客户端
├── test_record.py   # 手工录音/播放诊断工具
├── tests/           # pytest 自动化测试
├── requirements.txt
├── requirements-dev.txt
├── .env.example     # 配置模板（复制为 .env）
└── data/            # 数据库（已 gitignore，不入库）
```

## 技术栈

- **语音识别**：[FunASR](https://github.com/modelscope/FunASR) SenseVoice（中文识别率优于 Whisper，CPU 可实时）
- **语音合成**：Edge TTS（微软神经网络语音，免费）
- **大模型**：Qwen3.5-9B（llama.cpp / 任意 OpenAI 兼容服务）
- **记忆存储**：SQLite FTS5（中文逐字空格化实现任意子串检索，零依赖）

## 隐私说明

- 记忆数据仅保存在本地 `data/memory.db`，不会上传
- API 密钥仅存于本地 `.env`（已被 `.gitignore` 排除）
- 语音音频为临时文件，处理完即删除

## 路线图

- [x] 阶段 1：本地语音 MVP
- [x] 阶段 2：FastAPI 服务端 + Web 客户端
- [x] 阶段 3 基础：Agent、物品管理、到期扫描和页面状态展示
- [x] 基础稳固：API Token、输入边界、资源清理、会话 TTL、自动化测试
- [ ] 阶段 3 完善：WebSocket/SSE 主动推送、提醒确认、数量/位置/消耗管理
- [ ] 阶段 4：菜谱推荐
- [ ] 阶段 5：唤醒词 + 蓝牙音箱联动
- [ ] 阶段 6：视觉能力（摄像头识别）

## License

MIT
