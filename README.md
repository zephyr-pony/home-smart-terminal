# 家庭智能终端

[![tests](https://github.com/zephyr-pony/home-smart-terminal/actions/workflows/tests.yml/badge.svg)](https://github.com/zephyr-pony/home-smart-terminal/actions/workflows/tests.yml)

以**记忆**为核心的家庭 AI 助手。通过语音记录家庭琐事（"酸奶 8 月 5 号过期"、"明天交电费"），之后随时语音提问（"酸奶还有几天过期？"），系统自动检索并回答。

## 功能特性

- 🎤 **全程语音交互**：麦克风说话 → 识别 → 理解 → 语音回答
- 🗣️ **语音唤醒**：CLI 常驻监听，说「小马」唤醒；支持「小马，明天要交电费」一次说完
- 🧠 **记忆为核心**：陈述事实自动记录，疑问句自动检索回答
- 🧩 **主动澄清**：电费/水费缺「哪个家」、物品有多个候选时，先追问再记录，避免歧义
- 🧾 **写操作可审计**：记忆/物品创建走提案服务，短时间重复内容自动去重
- 🛡️ **高风险确认**：删除记忆/物品先问「确认还是取消」，确认后才执行
- 🧱 **实体管理**：新增/改名/别名，如把「爸妈家」也叫「父母家」
- 🔍 **中文全文检索**：任意子串匹配，零依赖（SQLite FTS5 中文空格化方案）
- 🤖 **Agent 驱动**：LLM 通过 function calling 自主选择工具（记忆 / 物品 / 日期），不是硬编码规则
- 💬 **多轮对话**：同一客户端最近几轮上下文注入 agent，支持"那个、它、还有呢"这类追问
- 🗓️ **物品与到期提醒**：记录物品到期日，服务端主动推送（SSE）即将到期 / 已过期提醒并语音播报
- ✅ **通用任务提醒**：账单、日程、自定义提醒统一进 tasks，和物品到期一起推送；说「电费交了」可标记完成
- 🔁 **周期提醒**：说「每天八点吃药」「每周三下午三点开会」「每月1号交房租」，到点才提醒；完成只跳过这一次，取消才停整个系列
- 🖥️ **Web 控制台**：聊天之外提供概览统计、数据列表、搜索/筛选/分页、任务编辑和系统配置，移动端可用
- 📷 **摄像头看一眼**（带摄像头的语音终端）：说「看看桌上有什么」，设备拍一张图由模型回答；图片只在内存里用一次，不落盘、不做人脸识别，详见 [ROADMAP §26](docs/ROADMAP.md)
- ⚙️ **配置集中管理**：所有参数在 `.env` 中，密钥不入库

## 演进方向：家庭生活 Agent

当前系统正在从「记忆 + 提醒」向「家庭生活 Agent」演进：能理解家庭实体、主动追问消歧、接入本地生活服务和 RAG 知识库，并逐步支持小程序、App、ESP32 等终端。

完整计划、数据模型、分阶段任务和待决策事项见 **[docs/ROADMAP.md](docs/ROADMAP.md)**。

近期重点：

1. 实体消歧与「提案 → 澄清 → 提交」两阶段流程
2. 记忆分层：结构化事实 / 语义记忆 / 情节记忆
3. RAG + 地图、公交、天气等本地生活工具
4. 统一网关与多渠道接入

> 当前优先：语音设备真机（小智 + ESP32）与真机验收；Web 控制台与 CLI 已落地并继续维护；Android App 与 RAG 延后。
> 配置页只允许白名单字段写入本地 `.env`，密钥不会返回前端；保存后重启服务端生效。

## 工作流程

```
输入（语音 / 文字）
   ↓  （语音先经 FunASR 识别）
Agent：LLM 按 function calling 自主决策调哪个工具
   ├─ memory_* / fact_*             → 记 / 查家庭事实与结构化信息
   ├─ item_*                        → 物品与到期
   ├─ task_*                        → 账单 / 日程 / 提醒
   └─ get_current_date              → 时间推理（还有几天 / 是否过期）
   ↓
回答（文字 + Edge TTS 语音）
```

## 环境要求

- Windows / Linux
- Python 3.9+
- 麦克风 + 音箱
- 网络（Edge TTS 需联网；LLM 视部署方式而定，可用本地服务）

## 快速开始

```bash
# 1. 克隆并进入目录
git clone <你的仓库地址>
cd 家庭智能终端

# 2. 创建虚拟环境，按需要装依赖（三份可以叠加，也可以一次装全）
python -m venv .venv
.venv\Scripts\activate        # Windows
source .venv/bin/activate     # Linux/macOS

pip install -r requirements-server.txt   # 网页 / API / 提醒 / 推送（必装）
pip install -r requirements-stt.txt      # 语音识别：约 2GB，首跑再下 900MB 模型
pip install -r requirements-cli.txt      # CLI 录音与播放；Linux 先装 portaudio19-dev
# 一次装全：pip install -r requirements.txt
#
# 只想用文字聊天：只装 server，并在 .env 设 STT_ENABLED=0。

# 3. 配置 LLM 服务（OpenAI 兼容接口，如 llama.cpp / LM Studio / OneAPI 网关）
#    只有这三项是必填的，其余都有默认值
cat > .env <<'EOF'
LLM_BASE_URL=https://你的网关/v1
LLM_API_KEY=sk-...
LLM_MODEL=你的模型名
EOF
# Windows 用记事本 / VS Code 新建 .env，写同样三行即可
#
# 想调端口、语音、Web Push、智能家居，再复制完整模板：
#   copy .env.example .env      # Windows
#   cp .env.example .env        # Linux/macOS

# 4. 启动前自检：不加载模型、不发网络请求，几秒钟把配置/目录/端口问题一次说完
python scripts/doctor.py
```

接下来按需要选一种运行方式：

**Web 服务端（手机/平板访问，推荐）**
```bash
start.bat            # Windows 一键启动
# 或 python server.py
```
浏览器打开 `http://<电脑IP>:8000`（本机测试用 `http://127.0.0.1:8000`），
支持按住说话（麦克风）或文字聊天。首次启动加载 STT 模型约需几十秒。

> 局域网访问需放行防火墙：以管理员运行 PowerShell 执行
> `New-NetFirewallRule -DisplayName "HomeSmartTerminal" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow`

> **手机想用推送和「添加到主屏」就必须走 HTTPS**（两者只在安全上下文可用，`http://<局域网IP>` 不行）。
> 推荐 Tailscale Serve：`tailscale serve --bg http://127.0.0.1:8000` 之后，手机装 Tailscale 加入同一
> tailnet，用 `https://<主机名>.<tailnet>.ts.net` 打开即可，证书由 Tailscale 自动签发。
> 详见 `docs/server-migration.md` 阶段 5。

> 若 `8000` 端口被占用或被系统保留（Windows 上可用 `netsh interface ipv4 show excludedportrange protocol=tcp` 查看），改 `.env` 的 `SERVER_PORT` 即可。

> **安全**：`SERVER_HOST` 不是 `127.0.0.1` 又没设 `API_TOKEN` 时，服务端会拒绝启动。
> 如果改用 `uvicorn server:app --host 0.0.0.0` 启动，应用看不到命令行里的地址，请自己确保已设 token。

**命令行模式（本机语音交互）**
```bash
start.bat cli        # 或 python main.py
```

**唤醒词常驻模式（说「小马」唤醒）**
```bash
start.bat wake       # 或 python main.py wake
```

> 唤醒词目前只做在 CLI（本机麦克风常驻监听）；Web 端仍为按住说话。
> 唤醒后可以直接说指令，也可以只喊「小马」等回应后再说。说「退出」或按 Ctrl+C 结束。

> 首次运行会自动下载 FunASR 语音识别模型（约 900MB），需要一些时间。
> 只想用文字聊天/调网页：在 `.env` 设 `STT_ENABLED=0`，跳过模型下载，`/api/voice` 返回 503。

**Docker（可选，适合长期跑在 NAS / 小主机上）**

```bash
cp docker-compose.example.yml docker-compose.yml
cp .env.example .env          # 至少填 LLM 三项；容器里必须设 API_TOKEN
docker compose up -d --build
```

- 镜像默认**不带语音识别**（compose 里把 `STT_ENABLED` 固定为 `0`）：网页文字、API、
  到期提醒都能用，只有 `/api/voice` 返回 503。需要语音识别：`docker compose build --build-arg INSTALL_STT=1`
  （先装 CPU 版 torch，镜像会大 2GB 以上），再把 compose 里的 `STT_ENABLED` 改成 `"1"`；
  只改 `.env` 不改这里不会生效，`doctor.py` 也会在缺依赖时报错。
- 容器里 `SERVER_HOST` 固定 `0.0.0.0`，**必须设 `API_TOKEN`**，否则 `_check_exposure()`
  会直接拒绝启动——这是刻意的，别为了省事关掉。
- 数据放在命名卷 `xiaoma-data`（容器内 `/data`，对应 `DATA_DIR`）。换机器/搬家优先用
  `python scripts/backup.py` 导出，命名卷直接拷文件容易漏掉正在写的库。
- 拉不到 `docker.io` 时（国内网络常见）先给 Docker 配镜像加速器或改用可访问的镜像源拉
  `python:3.12-slim` 并打回同名标签，Dockerfile 本身不用改。

## 服务端 API

| 接口 | 方法 | 说明 |
|---|---|---|
| `/healthz` | GET | 探活，**不需要鉴权**，只回 `{"ok": true}`（容器/反代用） |
| `/api/status` | GET | 状态、记忆/物品/任务数量和当前到期提醒（`due_items` / `due_tasks`） |
| `/api/stats` | GET | 概览统计：数量、任务状态、到期物品/任务 |
| `/api/memories` | GET | 记忆列表，支持 `q` / `limit` / `offset` |
| `/api/items` | GET | 物品列表，支持 `q` / `status` / `limit` / `offset` |
| `/api/tasks` | GET | 任务列表，支持 `q` / `status` / `limit` / `offset` |
| `/api/facts` | GET | 结构化事实列表 / 搜索，支持 `q` / `limit` / `offset` |
| `/api/entities` | GET | 实体与别名列表，支持 `q` / `type` / `limit` / `offset` |
| `/api/tasks/{id}/complete` | POST | 完成任务 |
| `/api/tasks/{id}/cancel` | POST | 取消任务 |
| `/api/tasks/{id}/postpone` | POST | 延期：`days` / `due_at` / `preset`（`tomorrow` / `in_3_days` / `next_monday`） |
| `/api/tasks/{id}` | PATCH | 编辑任务标题 / 时间 / 类型 |
| `/api/{memories\|items\|facts\|tasks}/{id}?confirm=true` | DELETE | 删除记录，必须显式确认 |
| `/api/config` | GET / PATCH | 读取脱敏配置 / 更新白名单字段 |
| `/api/push/key` | GET | 推送公钥与启用状态（私钥永不下发） |
| `/api/push/subscribe` | POST | 保存设备推送订阅 `{endpoint, keys}` |
| `/api/push/unsubscribe` | POST | 删除设备推送订阅 |
| `/api/push/test` | POST | 给已订阅设备发一条测试通知 |
| `/api/chat` | POST | 文字交互 `{text, client_id}` → 回复 + mp3 音频 base64 |
| `/api/voice` | POST | WAV 语音交互（multipart: file + client_id） |
| `/api/asr` | POST | 设备网关：WAV → 文本（只识别，不进 Agent） |
| `/api/tts` | POST | 设备网关：`{text}` → mp3 字节 |
| `/api/ask` | POST | 设备网关：`{text, client_id, want_audio}` → Agent 回复（ask_xiaoma 的后端） |
| `/api/events` | GET | SSE 事件流，主动推送到期提醒（`{"type":"reminder", ...}`） |

配置 `API_TOKEN` 后，所有 API 都需要请求头 `Authorization: Bearer <API_TOKEN>`。当前服务端限制文本 2000 字符、音频 10MB/30 秒；Web 页面会保存并自动发送 Token。

> `/api/events` 返回 `text/event-stream`；因浏览器 `EventSource` 无法携带请求头，Web 客户端改用 `fetch` 流式读取以便统一鉴权。

### 设备网关接口（小智 / ESP32）

给语音终端用的三个内部接口，都要 `Authorization: Bearer <API_TOKEN>`：

```bash
# 1. 识别（WAV → 文本）
curl -X POST http://127.0.0.1:8200/api/asr \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@utterance.wav;type=audio/wav"

# 2. 合成（文本 → mp3 字节）
curl -X POST http://127.0.0.1:8200/api/tts \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text":"好的，已记下"}' --output reply.mp3

# 3. 把整句交给 Agent（ask_xiaoma 的后端）
curl -X POST http://127.0.0.1:8200/api/ask \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text":"明天提醒我交电费","client_id":"xiaozhi-001"}'
```

- `/api/ask` 的 `client_id` 决定多轮上下文与待澄清动作归属，设备应传稳定的设备 ID
- `want_audio: true` 会让 `/api/ask` 额外返回 base64 mp3
- `/api/asr` 只识别，不触发 Agent；`/api/tts` 失败会自动重试一次

#### MCP 接入点（小智出站连接）

小智服务端作为 **MCP 客户端**，出站连到小马，把小马注册成一个工具 `ask_xiaoma`：

```
ws://<小马 IP>:8200/mcp_endpoint/mcp/?token=<MCP_TOKEN>
```

- `MCP_TOKEN` 留空则接入点关闭；非空时用查询参数鉴权
- 传输是裸 JSON-RPC 2.0（`initialize` / `tools/list` / `tools/call`），不用官方 MCP SDK
- 小智的声纹识别结果在 `tools/call` 的 `params._meta` 里（`speakerId` / `speakerName`），
  小马只拿它**分会话上下文**，不作安全凭据
- 自带探针（按小智报文形状发一遍）：

```bash
python scripts/mcp_probe.py --token <MCP_TOKEN> \
    --text "你好" --speaker-id spk-dad --speaker-name 爸爸
```

细节、报文形状与实测记录见 [`docs/xiaozhi-mcp.md`](docs/xiaozhi-mcp.md)。

#### 摄像头（拍照问答）

设备带摄像头时（如立创·实战派 S3），小智会把 `self.camera.take_photo` 作为工具交给 LLM。
小马这边要做的只有两步：

1. 说「看看桌上有什么」→ `/v1/chat/completions` 回一个 `tool_calls`（`finish_reason=tool_calls`），
   参数是 `{"question": "桌上有什么"}`；
2. 设备拍完把图 POST 到小智的视觉端点，那边转给小马的 `/v1`（`image_url`），小马用同一个模型
   看图回答，设备直接播报。

要点：

- 只有请求里的 `tools` 含 `self.camera.take_photo` 时，模型才看得见 `camera_look`
  （网页 / CLI 没有摄像头，不会说「我看到了」）
- 小智会把工具名里的点换成下划线（`self_camera_take_photo`）；小马回 `tool_calls` 时用它给的
  那个名字，否则小智查不到工具
- 图片不进记忆、不落盘、日志不打印 base64；画面里有人也不做身份推断
- 小智侧需要 `Intent: function_call`、`server.vision_explain`、`VLLM` 三项配置，
  见 [docs/ROADMAP.md §26](docs/ROADMAP.md)
- **没有硬件也能验整条链**：`python scripts/fake_camera_device.py --image 一张测试图.jpg` 会当一台带
  摄像头的设备连上去，报工具、收调用、把图 POST 到视觉端点，最后打印服务端念出来的话。
  脚本先按服务端下发的 LAN 地址试，不通再退到 `127.0.0.1`（Docker Desktop 的转发在本机上只对
  loopback 生效），所以它不能替代「另一台设备能不能连」的验证

## 到期提醒

- 服务端后台任务按 `REMINDER_INTERVAL_SECONDS`（默认 60s）扫描物品和任务，未来 `REMINDER_DAYS`（默认 3）天内到期或已过期的会**主动推送**：有网页在线时走 SSE，弹出提醒并语音播报；没有网页在线时走 Web Push，发系统通知。
- 同一物品在 `REMINDER_COOLDOWN_HOURS`（默认 24h）内不重复提醒；设为 `0` 表示只提醒一次。
- 提醒状态落库（`reminded_at`），重启服务也不会重复打扰。
- 点开任务提醒（网页卡片或系统通知）后，输入框上方会出现「正在处理：…」：接下来一句
  「完成了」「延到周五」直接落到那条任务，不用再说一遍是哪条；10 分钟内有效，可手动取消。
- 任务卡上可以直接「完成 / 延期 / 取消」；「延期」展开明天、三天后、下周一，或选「我自己说」
  再用一句话指定日期。延期保留原来的时刻（下午三点的提醒还是下午三点）。
- 网页状态栏同时常驻显示当前到期物品（`/api/status` 的 `due_items`）。

### 手机系统通知（Web Push）

页面关掉也能收到提醒，需要一次性配置：

1. 用 `python scripts/gen_vapid.py` 生成密钥，把输出的四行贴到 `.env`。
2. 重启服务端，日志出现 `📬 Web Push 已启用`。
3. 手机上从主屏图标打开小马 → 设置 → 「开启通知」→ 允许通知。
4. 点「发测试通知」确认能收到。

iPhone 的限制：必须在 iOS 16.4+，且只能从「添加到主屏幕」的图标打开时才能授权；Safari 标签页里不支持。通知送达依赖 Apple 推送服务，网络差时会延迟。系统静默轮换订阅会自动重新上报；但删除图标或清除网站数据会让订阅失效，需要重新开启一次。

点通知会打开小马，并把这条提醒的原文显示在聊天里（带蓝色边框标记），不是只回到空白主页。


## 配置说明（.env）

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `LLM_BASE_URL` | （必填） | LLM 服务地址，如 `http://127.0.0.1:8081/v1` |
| `LLM_API_KEY` | （必填） | API 密钥 |
| `LLM_MODEL` | （必填） | 模型名 |
| `LLM_MODEL_DECISION` | 空 | 第一轮（挑工具）单独用更快的模型；留空 = 两轮都用 `LLM_MODEL` |
| `LLM_REASONING_EFFORT` | `off` | 思考模式，值原样传给网关：commandcode 用 `off` 关，aisp 用 `none` 关；留空表示不传该参数 |
| `LLM_PROXY` | 空 | 访问网关走本机代理（如 `http://127.0.0.1:10808`）。直连很慢时配它；留空交给 requests 默认行为 |
| `VISION_CONTEXT_SECONDS` | `180` | 拍照后「刚才看到什么」在会话里保留多久；只在内存，不进记忆库 |
| `LLM_CHAT_TEMPLATE_KWARGS` | 空 | 本地 llama.cpp/vLLM 关思考用，JSON，如 `{"enable_thinking": false}` |
| `TTS_VOICE` | `zh-CN-XiaoxiaoNeural` | 语音角色（女声）；男声用 `zh-CN-YunxiNeural` |
| `STT_ENABLED` | `1` | 设 `0` 不加载 FunASR（首跑省 900MB），仅保留文字入口，`/api/voice` 返回 503 |
| `STT_MODEL` | `iic/SenseVoiceSmall` | 语音识别模型 |
| `STT_LANGUAGE` | `zh` | 识别语言 |
| `DATA_DIR` | `./data` | 所有数据库的默认目录；单独设下面 `*_DB_PATH` 的仍以它为优先 |
| `MEMORY_DB_PATH` | `<DATA_DIR>/memory.db` | 记忆库位置（一般不用改） |
| `ITEMS_DB_PATH` | `<DATA_DIR>/items.db` | 物品库位置 |
| `AGENT_DB_PATH` | `<DATA_DIR>/agent.db` | 实体 / 待澄清动作 / 提案库位置 |
| `TASKS_DB_PATH` | `<DATA_DIR>/tasks.db` | 任务 / 提醒库位置 |
| `FACTS_DB_PATH` | `<DATA_DIR>/facts.db` | 结构化事实库位置 |
| `PENDING_ACTION_TTL_SECONDS` | `1800` | 待澄清动作过期时间（秒） |
| `PROPOSAL_DEDUPE_SECONDS` | `300` | 相同写提案的去重窗口（秒） |
| `CONFIRM_WRITE_ACTIONS` | `0` | 弱模型模式：`0` 关闭 / `1` 所有写操作先确认 / `tasks` 只确认任务。开启后同时拦下「没调工具却说已记好」 |
| `SAMPLE_RATE` | `16000` | 录音采样率 |
| `CHUNK_SIZE` | `1024` | 音频块大小 |
| `MAX_RECORD_SECONDS` | `15` | CLI 最长录音时长（秒） |
| `SILENCE_THRESHOLD` | `0.015` | 静音能量阈值 |
| `SILENCE_DURATION` | `1.2` | 停顿多久视为说完（秒） |
| `WAKE_WORD` | `小马` | CLI 常驻监听的唤醒词 |
| `WAKE_WORD_MAX_SECONDS` | `8` | 唤醒句最长录音时长（秒） |
| `WAKE_ACK_TEXT` | `我在` | 只喊唤醒词时的应答，留空则不播报 |
| `SERVER_HOST` | `127.0.0.1` | 服务监听地址；局域网使用可改为 `0.0.0.0` |
| `SERVER_PORT` | `8000` | 服务端口 |
| `API_TOKEN` | 空 | API Bearer Token；局域网使用建议必填 |
| `MCP_TOKEN` | 空 | 小智 MCP 接入点令牌（`?token=`）；留空则接入点关闭 |
| `MAX_TEXT_CHARS` | `2000` | 单次文本上限 |
| `MAX_CLIENT_ID_CHARS` | `128` | 会话 ID 上限 |
| `MAX_UPLOAD_BYTES` | `10485760` | WAV 上传上限 |
| `MAX_AUDIO_SECONDS` | `30` | WAV 音频时长上限 |
| `SESSION_TTL_SECONDS` | `86400` | 会话空闲过期时间（秒） |
| `MAX_SESSIONS` | `1000` | 最大内存会话数 |
| `SESSION_CONTEXT_TURNS` | `5` | 注入 agent 的历史轮数（0 = 关闭多轮上下文） |
| `SESSION_CONTEXT_MAX_CHARS` | `300` | 单条历史注入的最大字符数 |
| `REMINDER_DAYS` | `3` | 未来几天内算"即将到期" |
| `REMINDER_INTERVAL_SECONDS` | `60` | 后台到期扫描间隔（秒） |
| `REMINDER_COOLDOWN_HOURS` | `24` | 同一物品再次提醒的冷却（小时，0 = 只提醒一次） |
| `PUSH_ENABLED` | `0` | 是否启用 Web Push 系统通知（页面关闭时也能收到） |
| `PUSH_DB_PATH` | `<DATA_DIR>/push.db` | 推送订阅库位置 |
| `VAPID_PUBLIC_KEY` | 空 | 推送公钥（`scripts/gen_vapid.py` 生成） |
| `VAPID_PRIVATE_KEY` | 空 | 推送私钥，base64(PEM) 单行；绝不返回前端 |
| `VAPID_SUBJECT` | `mailto:noreply@example.com` | VAPID 联系地址 |
| `PUSH_TTL_SECONDS` | `43200` | 推送在推送服务里的存活时间（秒） |
| `PUSH_MAX_FAILURES` | `5` | 连续推送失败几次后停用该订阅 |
| `HOME_BASE_URL` | `http://127.0.0.1:8123` | Home Assistant 地址（小米设备的网关） |
| `HOME_TOKEN` | 空 | HA 长期访问令牌；留空则不注册设备工具 |
| `HOME_TIMEOUT_SECONDS` | `5` | 调 HA 的超时（秒） |
| `HOME_DEVICES` | 空 | 小马认识的设备，`entity_id=别名` 逗号分隔 |
| `HOME_CONTROL_ALLOW` | 空 | 允许语音控制的设备（entity_id 或 `light.` 前缀）；空 = 全部只读 |
| `HOME_FILTER_ALERT_PERCENT` | `10` | 滤芯余量低于此百分比就主动提醒 |
| `HOME_ALERT_COOLDOWN_HOURS` | `24` | 同一设备多久内不重复提醒 |
| `HOME_WATCH_EVENTS` | 空 | 盯哪些 `event` 实体（摄像头移动侦测等），空 = 不盯 |
| `HOME_EVENT_COOLDOWN_SECONDS` | `600` | 同一路事件多久内不重复提醒 |
| `HOME_SCAN_SECONDS` | `15` | 设备巡检间隔（秒）；物品/任务仍是 60 秒 |

## 使用方式

```
按 Enter      开始语音录音（说完停顿自动结束）
直接输入文字   以文本方式交互
输入 quit     退出
```

### Web 控制台

```
聊天    文字 / 语音对话，保留 SSE 提醒
概览    记忆 / 物品 / 任务 / 事实 / 实体数量，到期提醒
数据    记忆 / 物品 / 任务 / 事实 / 实体列表
        支持搜索、状态筛选、分页
        任务可完成 / 取消 / 编辑；删除需二次确认
设置    语音角色、提醒天数/间隔/冷却、上下文轮数等
        写本地 .env，重启服务端后生效，密钥不返回前端
```

支持安装成 PWA：手机浏览器打开后「添加到主屏幕」，全屏运行、有独立图标；
断网时至少能打开界面（数据接口仍需要服务端在线）。Service Worker 挂在 `/sw.js`，
后续 Web Push 提醒复用同一套入口。

### 唤醒词模式

```
说「小马，明天要交电费」   唤醒并直接执行后半句
说「小马」                 回应「我在」，再说指令
说「退出」 / Ctrl+C        结束
```

### 示例对话

| 用户说 | 系统行为 |
|---|---|
| 我今天给鼠标换了电池 | 记为记忆：相对时间自动换算成绝对日期（如"8月X日"） |
| 明天要交电费 | 创建账单任务，到期前主动提醒 |
| 电费交了 | 找到对应任务并标记完成 |
| 我有哪些提醒？ | 列出待办任务 |
| 酸奶 8 月 5 号过期 | 记入物品清单：酸奶，到期日 2026-08-05 |
| 我的酸奶还有几天过期？ | 查物品 → 结合今天日期回答"还有 4 天"（或已过期 X 天） |
| 冰箱里有什么东西？ | 列出物品清单及到期情况 |
| 我什么时候换的鼠标电池？ | 检索记忆 → 回答（带正确的记忆日期） |
| 看看记忆 / 忘掉酸奶 | 内置命令：列出最近记忆 / 删除相关记忆 |
| 你好 | 闲聊回复 |

## 目录结构

```
├── main.py          # CLI 入口（含唤醒词常驻模式）
├── wake_word.py     # 唤醒词解析
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
├── scripts/
│   ├── backup.py             # 数据备份 / 校验 / 恢复（见 docs/backup.md）
│   ├── doctor.py             # 启动前自检（配置 / 目录 / 端口 / 可选模块）
│   ├── eval_accuracy.py      # 41 条固定评测集（真模型，不进 pytest）
│   ├── fake_camera_device.py # 假设备：无硬件验摄像头整条链
│   └── record_diagnostic.py  # 手工录音/播放诊断（不参与 pytest）
├── tests/           # pytest 自动化测试
├── requirements-server.txt  # 网页 / API / 提醒 / 推送
├── requirements-stt.txt     # 语音识别（torch + FunASR）
├── requirements-cli.txt     # CLI 录音与播放（pyaudio / pygame）
├── requirements.txt         # 上面三份的组合
├── requirements-dev.txt
├── requirements-ci.txt      # CI 用：server + pytest，不含 torch/funasr/pyaudio
├── Dockerfile               # 服务端镜像，默认不含语音识别
├── docker-compose.example.yml
├── .env.example     # 配置模板（复制为 .env）
├── LICENSE          # MIT
├── CONTRIBUTING.md  # 开发说明
└── data/            # 数据库（已 gitignore，不入库；可用 DATA_DIR 改位置）
```

## 技术栈

- **语音识别**：[FunASR](https://github.com/modelscope/FunASR) SenseVoice（中文识别率优于 Whisper，CPU 可实时）
- **语音合成**：Edge TTS（微软神经网络语音，免费）
- **大模型**：任意 OpenAI 兼容服务（llama.cpp / LM Studio / OneAPI 网关 / 云端 API），需支持 function calling
- **记忆存储**：SQLite FTS5（中文逐字空格化实现任意子串检索，零依赖）
- **物品存储**：SQLite（名称 + 到期日，线程安全）

## 隐私说明

- 记忆与物品数据仅保存在本地 `DATA_DIR`（默认 `data/`，含 `memory.db` / `items.db` / `tasks.db` / `facts.db` / `agent.db` / `push.db`），不会上传
- API 密钥仅存于本地 `.env`（已被 `.gitignore` 排除）
- 语音音频为临时文件，处理完即删除

## 路线图

已完成：

- [x] 阶段 1：本地语音 MVP
- [x] 阶段 2：FastAPI 服务端 + Web 客户端
- [x] 阶段 3：Agent 化（function calling）+ 物品管理 + 到期提醒
- [x] 增强：多轮对话上下文、SSE 主动推送、API Token、输入边界、自动化测试与评测集
- [x] 唤醒词「小马」（CLI 常驻监听）
- [x] 实体消歧与两阶段提交、删除确认、实体管理、通用任务/结构化事实
- [x] Web 控制台（聊天 / 统计 / 列表 / 搜索筛选 / 分页 / 任务编辑 / 删除确认 / 配置）
- [x] 周期提醒（每天 / 每周 / 每月）、Web Push、设备反向提醒
- [x] 小智设备接入（MCP `ask_xiaoma`）、单张拍照问答
- [x] 家庭设备：Home Assistant 只读 + 白名单控制 + 事件订阅
- [x] 提醒点开处理（任务绑定、完成 / 延期 / 取消，详见 [docs/ROADMAP.md](docs/ROADMAP.md) §16）

下一步：

- [ ] 设备真机验收（唤醒词、反向提醒、拍照、延迟）
- [ ] 传感器事件（水浸 / 烟感 / 门磁）真机验证
- [ ] facts 与 entities 关联（结构化事实按实体查）
- [ ] Android App / 小程序
- [ ] 记忆分层 + RAG + 本地生活工具（延后）
- [ ] 多空间 `space_id`（多家庭/多成员隔离）
- [ ] 更远：菜谱推荐、蓝牙音箱联动、图像连续理解（单张已支持）

## License

MIT，见 [LICENSE](LICENSE)。参与开发见 [CONTRIBUTING.md](CONTRIBUTING.md)。
