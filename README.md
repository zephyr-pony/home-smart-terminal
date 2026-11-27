# 家庭智能终端

[![tests](https://github.com/zephyr-pony/home-smart-terminal/actions/workflows/tests.yml/badge.svg)](https://github.com/zephyr-pony/home-smart-terminal/actions/workflows/tests.yml)

以**记忆**为核心的家庭 AI 助手：记得家里的事，也会提醒、看家、打印、上网查。通过语音记录家庭琐事（"酸奶 8 月 5 号过期"、"明天交电费"），之后随时语音提问（"酸奶还有几天过期？"），系统自动检索并回答。

## 功能特性

- 🎤 **全程语音交互**：麦克风说话 → 识别 → 理解 → 语音回答
- 🗣️ **语音唤醒**：CLI 常驻监听，说「小马」唤醒；支持「小马，明天要交电费」一次说完
- 🧠 **记忆为核心**：陈述事实自动记录，疑问句自动检索回答
- 🧩 **主动澄清**：电费/水费缺「哪个家」、物品有多个候选时，先追问再记录，避免歧义
- 🧾 **写操作可审计**：记忆/物品创建走提案服务，短时间重复内容自动去重
- 🛡️ **高风险确认**：删除记忆/物品先问「确认还是取消」，确认后才执行
- 🧱 **实体管理**：新增/改名/别名，如把「爸妈家」也叫「父母家」
- 🔍 **中文全文检索**：任意子串匹配，零依赖（SQLite FTS5 中文空格化方案）
- 🧮 **时间、算术与农历**：问「现在几点」「三天后是哪天」走 `time_now`，折扣/分摊/百分比走 `calc`，农历/节气/传统节日走 `lunar_calendar`（公历↔农历双向），都不让模型口算
- 🤖 **Agent 驱动**：LLM 通过 function calling 自主选择工具（记忆 / 物品 / 日期），不是硬编码规则
- 💬 **多轮对话**：同一客户端最近几轮上下文注入 agent，支持"那个、它、还有呢"这类追问
- 🗓️ **物品与到期提醒**：记录物品到期日，服务端主动推送（设备 / 网页 SSE / Web Push）即将到期 / 已过期提醒并语音播报
- ✅ **通用任务提醒**：账单、日程、自定义提醒统一进 tasks，和物品到期一起推送；说「电费交了」可标记完成
- 🔁 **周期提醒**：说「每天八点吃药」「每周三下午三点开会」「每月1号交房租」，到点才提醒；完成只跳过这一次，取消才停整个系列
- 🗣️ **软提醒**（D81）：该准点的事照常响（吃药、出门、开会），牛奶过期 / 取快递这类则等你聊天时顺口提一句，由你当场定「办了 / 往后放」；到 20:30 还没机会说，才兜底播报一次。提醒那句话由模型拿事实组织，不是模板句
- 🏠 **设备主动提醒**（Home Assistant）：滤芯余量、故障码巡检，门铃/门锁事件秒级推送；门锁会说清「爸爸用指纹开了门」「妈妈刷脸开了门」「门从里面打开了」，还会报撬锁、频繁开锁失败、门未关 / 门虚掩
- 🔌 **客厅语音终端**（小智 + ESP32）：唤醒、对话、反向提醒、拍照问答；MCP 接入点与 OpenAI 兼容 `/v1` 两种接法，终端待机也不断线。要不要结束会话（「再见」「先这样」）由模型判断，不靠关键词
- 🖥️ **Web 控制台**：聊天之外提供概览统计、数据列表、搜索/筛选/分页、任务编辑和系统配置，移动端可用
- 🖨️ **打印**（D61）：网页/手机传文件 → 说要求 → 确认 → 远程 CUPS 出纸；Office 转 PDF，纯文本含中文自动转 PDF（D77）
- 🌐 **通用能力**（D62/D74）：读附件、跑 Python、执行命令、上网搜索和抓页面，都由模型现场组合；不为「缩到一页」「转格式」这类需求单独加工具
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

> 当前优先：语音设备（小智 + ESP32）已上真机，正在做迁移到 Ubuntu Server 的部署件与运维；Web 控制台与 CLI 已落地并继续维护；Android App 与 RAG 延后。
> 配置页只允许白名单字段写入本地 `.env`，密钥不会返回前端；保存后重启服务端生效。

### 设计取向

同一个模型，编排不同，可能是「语音版命令行」，也可能是家里人。几条写进代码的取向：

- **先「说得对」，再「说得自然」**。冲突时选对的——生硬但正确，好过顺耳但错误。
- **该模型开口的地方别替它写话**。工具给事实和边界，不给成品话术；模板只留在「说了就得对」的两处（写操作回显、校验失败兜底）。
- **时机也是智能**。什么时候说和说什么一样重要（软提醒顺口提，不到点弹）。
- **「活」不等于放任**。编造日期、说错号码、危险动作由代码拦（`guard` / `verify` / `confirm`）——这些兜底是为了让模型敢自由说话。
- **不把交互写成硬指令**。没有关键词路由、没有「只有…才能…」的规则墙；默认值都能被一句话推翻。
- **歧义靠画像，事实不靠猜**。中性、有歧义的问题按用户习惯（画像层）靠；户号、日期、谁是谁这种确定性问题一律以事实和工具结果为准。

展开见 [AGENTS.md](AGENTS.md) 的「让助手像个人」和 [ROADMAP §1](docs/ROADMAP.md)。

## 工作流程

```
输入（语音 / 文字）
   ↓  （语音先经 FunASR 识别）
Agent：LLM 按 function calling 自主决策调哪个工具
   ├─ memory_* / fact_* / entity_*   → 记 / 查家庭事实、结构化信息与别名
   ├─ item_* / task_*                → 物品到期、账单 / 日程 / 提醒（软 / 硬）
   ├─ time_now / calc / lunar_calendar → 时刻与日期推算、算术、农历
   ├─ file_read / run_python / run_shell / web_search / web_fetch → 通用能力
   ├─ printer_print                  → 上传的文件出纸（确认后）
   ├─ home_* / camera_look           → 家庭设备查询控制、设备拍照（终端带摄像头时）
   ↓
回答（文字 + Edge TTS 语音；设备在线时直接推到客厅音响）
```

## 环境要求

- Windows（CLI / 开发）或 Linux（服务端、打印）
- Python 3.9+
- 麦克风 + 音箱（语音链路需要；只聊天可以不要）
- 网络（Edge TTS 需联网；LLM 可用本机服务）
- Docker（可选，长期部署推荐：Linux 镜像，打印走 CUPS）

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
> 换机或换域名等于换源：每台设备要重新开启通知、重新「添加到主屏」。

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
- 打印：镜像默认装 CUPS 客户端和 LibreOffice（`INSTALL_PRINT=1`，镜像会大几百 MB）。
  不需要打印就 `--build-arg INSTALL_PRINT=0`；打印队列在 `.env` 里配：
  `PRINTER_SERVER=192.168.1.15:631`、`PRINTER_NAME=HP1020`。容器只向远程 CUPS
  发 IPP 任务，不用自己跑 cupsd；宿主机/容器能访问到那台 CUPS 即可。
- 数据放在命名卷 `xiaoma-data`（容器内 `/data`，对应 `DATA_DIR`）。备份要用
  `python scripts/backup_docker.py`（自动找卷、容器里走 sqlite 热备、打包校验），
  别直接拷卷文件；换机器/搬家也用它导出的 zip。细节见 `docs/backup.md`。
- 容器日志默认 JSON 文件会一直长；`docker-compose.example.yml` 里已配
  `logging`（10MB × 3 份轮转）。自己改过 compose 的别忘了抄这一段，否则跑几个月能把盘吃满。
- 语音模型缓存在 `xiaoma-cache` 卷（`/home/xiaoma/.cache`），重建容器不用重下 900MB 模型。
- 长期部署（Ubuntu Server + 开机自启 + 定时加密备份）见 [`infra/ubuntu/README.md`](infra/ubuntu/README.md)。
- 拉不到 `docker.io` 时（国内网络常见）先给 Docker 配镜像加速器或改用可访问的镜像源拉
  `python:3.12-slim` 并打回同名标签，Dockerfile 本身不用改。系统包/ pip 也连不上时，
  构建加镜像源参数：`APT_MIRROR=mirrors.aliyun.com`、`PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple`
  （写进 compose 的 `build.args` 或 `--build-arg`）。
- **Windows + Docker Desktop（WSL2）**：端口发布到局域网要在 **Hyper-V 防火墙**放行，
  普通 Windows 防火墙规则不管用。`networkingMode=mirrored` 时尤其如此：
  ```powershell
  # 管理员 PowerShell
  New-NetFirewallHyperVRule -DisplayName "Xiaoma-8208" -Direction Inbound -Action Allow -Protocol TCP -LocalPorts 8208
  ```
  加完从**手机或另一台电脑**访问 `http://<这台机器 IP>:8208` 验证；
  宿主机自己访问自己的局域网 IP 可能超时（WSL 镜像模式的 hairpin），这不算失败。

## 服务端 API

| 接口 | 方法 | 说明 |
|---|---|---|
| `/healthz` | GET | 探活，**不需要鉴权**，只回 `{"ok": true}`（容器/反代用） |
| `/api/status` | GET | 状态、记忆/物品/任务数量和当前到期提醒（`due_items` / `due_tasks`）；`runtime` 里有运行时长、数据大小、磁盘剩余 |
| `/api/spaces` | GET | 已存在的空间（`X-Space-Id` 请求头选空间，不带 = 默认空间） |
| `/api/stats` | GET | 概览统计：数量、任务状态、到期物品/任务 |
| `/api/thinking` | GET | 「这轮有没有开思考」的观察记录（最近 20 轮 + 统计），`limit` 可调 |
| `/api/prints` | GET | 打印历史：名字、时间、份数、结果、CUPS 任务号；`q` / `status` / `limit` / `offset` |
| `/api/printer` | GET | 打印机状态：在线 / 队列 / 积压 / 原因（30 秒缓存，断电不会挂住页面） |
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
| `/api/chat` | POST | 文字交互 `{text, client_id}` → 回复 + mp3 音频 base64；可带 `file_id`（打之前传的文件）、`task_id`（点开的那条提醒） |
| `/api/print/files` | POST | 上传待打印文件（multipart: file + client_id），返回 `file_id`；落 `DATA_DIR/print`，由 Linux CUPS 出纸 |
| `/api/voice` | POST | WAV 语音交互（multipart: file + client_id） |
| `/api/asr` | POST | 设备网关：WAV → 文本（只识别，不进 Agent） |
| `/api/tts` | POST | 设备网关：`{text}` → mp3 字节 |
| `/api/ask` | POST | 设备网关：`{text, client_id, want_audio}` → Agent 回复（ask_xiaoma 的后端） |
| `/v1/chat/completions` | POST | 小智 / ESP32 的 OpenAI 兼容入口：带工具时回 `tool_calls`（拍照），带图时走视觉问答 |
| `/api/events` | GET | SSE 事件流，主动推送提醒；`?role=device` 给小智提醒桥（设备自己念，不带 mp3） |

配置 `API_TOKEN` 后，除 `/healthz` 外所有接口都需要请求头 `Authorization: Bearer <API_TOKEN>`。当前服务端限制文本 2000 字符、音频 10MB/30 秒；Web 页面会保存并自动发送 Token。

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

- 服务端后台任务按 `REMINDER_INTERVAL_SECONDS`（默认 60s）扫描物品和任务，未来 `REMINDER_DAYS`（默认 3）天内到期或已过期的会**主动推送**。投递顺序固定为 **设备 → 网页 → Web Push**，同一条提醒只走一条：客厅设备在线就让它自己念，页面在线就弹 SSE 并播报，都没有才发系统通知。
- **软提醒**（D81）：`mode=soft` 的任务（牛奶过期、取快递这类当天提一句就行的事，软硬由模型判断）和物品到期，白天不在固定时间播报，只在聊天里顺口带一句，由你当场定「办了 / 往后放」；到 `SOFT_REMINDER_LAST_CALL`（默认 20:30）还没机会说，才按准点提醒发一次。吃药、出门、开会这类错过就没用的仍走准点提醒（`mode=timely`，默认）。
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
| `LLM_REASONING_EFFORT` | `off` | 默认思考档位，值原样传给网关：commandcode 用 `off` 关，aisp 用 `none` 关；留空表示不传该参数 |
| `LLM_THINKING_EFFORT` | `medium` | 模型自己调 `set_thinking(true)` 后本轮用的档位（默认关，够用才开） |
| `THINKING_LOG_ENABLED` | `1` | 记录「这轮有没有开思考」到 `data/thinking.jsonl`，`GET /api/thinking` 可看 |
| `LLM_PROXY` | 空 | 访问网关的备用代理（如 `http://127.0.0.1:10808`）。**先直连，连不上才走它**；直连失败后 5 分钟内优先代理，之后自动再试直连 |
| `WEB_PROXY` | 空 | 搜索/抓页面用哪个代理；留空直连（不借用 `LLM_PROXY`） |
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
| `SOFT_REMINDER_LAST_CALL` | `20:30` | 软提醒兜底时刻（HH:MM）：到点还没在聊天里提过就准点提醒一次 |
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
| `HOME_WATCH_EVENTS` | 空 | 盯哪些 `event` 实体（摄像头移动侦测、门铃、门锁锁事件/异常等），空 = 不盯 |
| `HOME_EVENT_COOLDOWN_SECONDS` | `600` | 同一路事件多久内不重复提醒 |
| `HOME_LOCK_USERS` | 空 | 门锁凭据ID→名字，如 `501=爸爸,2001=爸爸,2004=妈妈`；人脸/指纹各一个号 |
| `HOME_LOCK_COOLDOWN_SECONDS` | `0` | 门锁事件冷却（秒）；0 = 每条都报，10 分钟内进两个人不会漏 |
| `HOME_SCAN_SECONDS` | `15` | 设备巡检间隔（秒）；物品/任务仍是 60 秒 |
| `WEATHER_CITY` | 空 | 家庭所在城市；只填这一项就能用 Open-Meteo（不需要 key） |
| `WEATHER_LAT` / `WEATHER_LON` | 空 | 可选，填了跳过城市名匹配，避免撞同名小地方 |
| `WEATHER_API_HOST` | 空 | 可选，和风专属 API Host；与 Key 都填才启用和风 |
| `WEATHER_API_KEY` | 空 | 可选，和风 API KEY（不是 JWT） |
| `HOT_CONTEXT_ENABLED` | `1` | 会话开始时把画像/常用事实/最近记忆注入 system prompt（冻结快照） |
| `HOT_CONTEXT_MAX_CHARS` | `900` | 热点上下文总字符预算，超了按 画像 > 常用事实 > 最近记忆 截断 |
| `HOT_CONTEXT_FACTS` | `6` | 常用事实最多注入几条 |
| `HOT_CONTEXT_MEMORIES` | `5` | 最近记忆最多注入几条 |
| `MEMORY_DISTILL_ENABLED` | `1` | 记忆蒸馏：合并同主题记忆，只重算摘要、不删原文 |
| `MEMORY_DISTILL_HOUR` | `3` | 每天几点跑蒸馏（本机时间） |
| `MEMORY_DISTILL_THRESHOLD` | `500` | 记忆达到多少条时不等半夜、直接补跑 |
| `MEMORY_DISTILL_INTERVAL_HOURS` | `6` | 超阈值补跑的最小间隔（小时） |
| `MEMORY_DISTILL_MAX_GROUPS` | `10` | 每轮最多合并几组（一组一次模型调用） |
| `MEMORY_DISTILL_SUMMARY_CHARS` | `60` | 合并后的摘要最多多少字 |
| `MEMORY_DISTILL_CHECK_SECONDS` | `900` | 后台检查是否该蒸馏的间隔（秒） |
| `PRINTER_ENABLED` | `1` | 网页上传文件，用 Linux CUPS 队列出纸（服务器部署） |
| `PRINTER_SERVER` | 空 | 远程 CUPS 服务器 `host:port`（如 `192.168.1.15:631`）；空 = 本机 CUPS |
| `PRINTER_NAME` | 空 | CUPS 队列名（`lpstat -h <server> -a` 第一列）；空 = 默认队列 |
| `PRINTER_ALLOWED` | 空 | 允许使用的队列（逗号分隔）；空 = 只允许默认那个 |
| `PRINTER_MAX_PAGES` | `50` | 一次最多打印多少页 |
| `PRINTER_MAX_COPIES` | `20` | 一次最多打印多少份 |
| `PRINT_KEEP_DAYS` | `7` | 打印文件保留几天（`DATA_DIR/print`），过期连文件一起清；历史记录一直在 |
| `PRINT_REUSE_HOURS` | `24` | 多久内算「刚才的文件」；「再打一份」不用重传，重启后也算 |
| `PRINT_FILES_DIR` | `<DATA_DIR>/print` | 打印原件目录（要在卷上，容器重建不丢） |
| `PRINTS_DB_PATH` | `<DATA_DIR>/prints.db` | 打印记录库（文件 id、结果、CUPS 任务号） |

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
        思考开关：最近 20 轮开没开、原因、工具、耗时（GET /api/thinking）
        最近打印：打印机在线/队列状态，结果、时间、份数，点「再打一份」回到确认
                  （GET /api/printer、GET /api/prints）
        运行状态：已运行多久、数据多大、打印文件数量、磁盘剩余
数据    记忆 / 物品 / 任务 / 事实 / 实体列表
        支持搜索、状态筛选、分页
        任务可完成 / 取消 / 编辑；删除需二次确认
设置    语音角色、提醒天数/间隔/冷却、上下文轮数等
        写本地 .env，重启服务端后生效，密钥不返回前端
```

支持安装成 PWA：手机浏览器打开后「添加到主屏幕」，全屏运行、有独立图标；
断网时至少能打开界面（数据接口仍需要服务端在线）。Service Worker 挂在 `/sw.js`，
Web Push 也走同一个入口（见上一节）。

### 打印文件（Linux + CUPS）

1. 服务器先配好 CUPS 队列：`lpstat -a` 看队列名，填进 `PRINTER_NAME`
2. 点聊天输入框左边的 📎，选一个文件（PDF / 图片 / TXT、Markdown / Office）
3. 说「打印」或「打印两份，1到2页」
4. 小马复述文件、份数、页码和队列，回复「确认」才提交打印

- 需要 CUPS 客户端（`lp` / `lpstat`）；Office 文档还需要 LibreOffice（`soffice`）
  - Debian/Ubuntu：`sudo apt install cups-client cups-filters libreoffice-core fonts-noto-cjk`
- 打印机可以挂在别的机器上：`PRINTER_SERVER=192.168.1.15:631`、`PRINTER_NAME=HP1020`，
  容器只发 IPP 任务，不用自己跑 cupsd
- PDF / 图片 / 纯文本直接交给 CUPS 过滤器转换；Office 先由 LibreOffice 转 PDF
- 文件存 `DATA_DIR/print`（Docker 里在卷上），默认留 `PRINT_KEEP_DAYS` 天；「刚才的文件再打一份」
  在 `PRINT_REUSE_HOURS` 内不用重传，**重启/重建容器也认得**（记录在 `prints.db`）
- 概览页有「最近打印」：打过什么、成没成功、CUPS 任务号，点「再打一份」直接回到确认那一步
- 语音终端只能确认，不能传文件；文件必须从网页或 API 上传
- 不支持 Windows 打印队列：服务器部署是 Linux（D61）

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
| 看看我的记忆 / 忘掉酸奶 | 模型自己选 `memory_list` / `memory_delete`；删除仍要回「确认」 |
| 牛奶今天过期 | 记入物品；白天不弹提醒，聊到相关话题时顺口问一句，20:30 还没说才兜底播报 |
| 每天八点提醒我吃药 | 创建周期任务，到点准点提醒（`mode=timely`） |
| （门锁被打开时） | 设备/网页/手机会主动说「爸爸用指纹开了门」「妈妈刷脸开了门」，不用问 |
| 你好 | 闲聊回复 |

## 目录结构

```
├── main.py          # CLI 入口（含唤醒词常驻模式）
├── wake_word.py     # 唤醒词解析
├── assistant.py     # 核心编排和 Agent loop（软提醒注入、回复校验、提醒措辞）
├── tools.py         # OpenAI function calling 工具注册
├── server.py        # FastAPI 服务端：REST / SSE / MCP 接入点 / 提醒循环
├── config.py        # 集中配置（从 .env 加载）
├── audio_io.py      # PyAudio 录音与 pygame 播放
├── stt.py           # FunASR SenseVoice 语音识别
├── llm.py           # OpenAI 兼容 LLM 交互
├── memory.py        # SQLite FTS5 中文记忆检索
├── items.py         # SQLite 物品管理（名称/到期日）
├── tts.py           # Edge TTS 语音合成
├── agent/           # 澄清 / 提案 / 确认 / 护栏 / 回复校验 / 任务提示 / 设备巡检
├── domain/          # 领域层：实体 / 待澄清 / 提案 / 任务 / 事实 / 空间 / 打印记录
├── adapters/        # Home Assistant、打印机（CUPS）、文档、命令、上网、小智 MCP
├── static/index.html # Web 客户端（单文件，无构建）
├── infra/           # 部署件：小智提醒桥补丁、ESP32 固件补丁、Ubuntu 部署
├── docs/            # ROADMAP、changes、备份/小智/提醒等文档
├── scripts/
│   ├── backup.py             # 数据备份 / 校验 / 恢复（见 docs/backup.md）
│   ├── backup_docker.py      # 命名卷备份 / 恢复（容器里走 sqlite 热备）
│   ├── doctor.py             # 启动前自检（配置 / 目录 / 端口 / 可选模块）
│   ├── eval_accuracy.py      # 43 条固定评测集（真模型，不进 pytest）
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
                     # 其它空间的数据在 data/spaces/<space_id>/（D68）
```

## 技术栈

- **语音识别**：[FunASR](https://github.com/modelscope/FunASR) SenseVoice（中文识别率优于 Whisper，CPU 可实时）
- **语音合成**：Edge TTS（微软神经网络语音，免费）
- **大模型**：任意 OpenAI 兼容服务（llama.cpp / LM Studio / OneAPI 网关 / 云端 API），需支持 function calling
- **记忆存储**：SQLite FTS5（中文逐字空格化实现任意子串检索，零依赖）
- **家庭设备**：Home Assistant（小米官方集成）+ 本地 REST / WebSocket；语音终端走自建小智服务端
- **打印**：Linux CUPS（`lp` / `lpstat`）+ LibreOffice（Office 转 PDF）
- **部署**：Docker Compose；手机经 Tailscale + PWA 访问

## 隐私说明

- 记忆、物品、任务、事实、打印记录只存在本地 `DATA_DIR`（`memory.db` / `items.db` / `tasks.db` / `facts.db` / `agent.db` / `push.db` / `prints.db`），不上传、不入库
- 打印原件留在 `DATA_DIR/print`，按 `PRINT_KEEP_DAYS` 清理；`data/thinking.jsonl` 只是调试观测（可关）
- 对话文本会发给你在 `.env` 里配置的 LLM 服务（本地服务则不出内网）——这是它回答问题的前提；API 密钥只存在本地 `.env`
- 语音音频是临时文件，处理完即删；摄像头图片只在内存里用一次，不落盘、不做人脸识别
- Home Assistant 的令牌、Web Push 私钥都只放 `.env`，接口不返回前端

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
- [x] Linux / Docker 部署：容器内跑服务端，手机经 Tailscale + PWA 访问
- [x] 打印（D61）：网页/手机上传 → 说要求 → 确认 → 远程 CUPS 出纸；Office 转 PDF，表格自己算列宽、超宽转横向、压缩到一页
- [x] 通用文件能力（D62）：`file_read` / `find_tools` / `run_python` / `run_shell` / `web_search` / `web_fetch`，读表、改表、装环境、上网查都由模型现场组合，不为单个需求加工具
- [x] 思考开关交给模型（D63）：默认快，模型判断需要多步推理时自己开；每轮记录开没开（`GET /api/thinking`）
- [x] 最近文件各端共用 30 分钟（D64）：「刚才的文件再打一份」「改成横向」不用重新上传
- [x] 打印记录落库（D65）：文件留在卷上、历史进 `prints.db`，重启/重建容器后「再打一份」照用，概览页能查打印历史
- [x] 事实与实体关联（D66）：事实挂 `entity_id`，存「净水器」、问「饮水机」也查得到；别名只需加实体别名
- [x] 运维：容器日志轮转（10MB × 3）+ 概览「运行状态」（运行时长 / 数据大小 / 磁盘剩余）
- [x] 备份可加密（AES-256-GCM，`--encrypt`）+ Linux systemd timer 定时（`docs/backup.md`）
- [x] 概览「打印机状态」：在线/队列/积压/原因，上传前就能看到（`GET /api/printer`）
- [x] 空间隔离地基（D68）+ 手动路由（D71）：一个空间一套库（默认空间路径不变，其它在 `data/spaces/<id>/`）；请求带 `X-Space-Id` 就落到那个空间，`GET /api/spaces` 可列
- [x] 设备保持连接（D78）：待机不收麦克风、WebSocket 常驻，提醒不用先喊一声；固件开机自连 + 断线重连
- [x] 软提醒（D81）：`mode=timely/soft` 由模型判，白天软提醒只在聊天里顺口提，`SOFT_REMINDER_LAST_CALL`（默认 20:30）兜底；提醒文案由模型组织
- [x] 门锁事件解码：谁、用什么方式、门内门外、撬锁/频繁失败/门未关/门虚掩都能说清（`HOME_LOCK_USERS` 把凭据编号映射成名字）
- [x] 迁移预演：`infra/ubuntu/`（compose oneshot + 加密备份 systemd 单元），目标机 HPE Gen10 Plus / Ubuntu Server 26.04

下一步：

- [ ] 迁移到 Ubuntu Server 真机（Gen10 Plus）：卷名、端口暴露、UPS/iLO、加密备份异地
- [ ] 软提醒真机验收：聊到相关话题时是否顺口提、白天是否不再播报、20:30 是否兜底
- [ ] 门锁异常 / 门未关真机验证（异常事件 6 天未触发过，目前只有单测）
- [ ] 传感器事件（水浸 / 烟感 / 门磁）真机验证
- [ ] Android App / 小程序
- [ ] 记忆分层 + RAG + 本地生活工具（延后）
- [ ] 多空间认人：声纹/设备归属 → `space_id`（手动路由已就绪，见 D71），再做多家庭成员权限
- [ ] 更远：菜谱推荐、蓝牙音箱联动、图像连续理解（单张已支持）

## License

MIT，见 [LICENSE](LICENSE)。参与开发见 [CONTRIBUTING.md](CONTRIBUTING.md)。
