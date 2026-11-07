# AGENTS.md

给在此仓库工作的 AI 编码代理（以及人类开发者）的说明。目标是快速理解结构、遵守既有约定、避免踩坑。

## 项目概览

家庭智能终端「小马」：语音/文字输入 → FunASR 识别 → LLM 通过 function calling 自主调用工具（记忆 / 物品 / 日期）→ 文字 + Edge TTS 回答。SQLite 本地存储，数据不上传。

当前正在从「记忆 + 提醒」演进为家庭生活 Agent，完整计划见 [docs/ROADMAP.md](docs/ROADMAP.md)。

## 模块职责

| 文件 | 职责 |
|---|---|
| `main.py` | CLI 入口：录音 → STT → `Assistant` → 打印 + TTS 播报；含唤醒词常驻模式 |
| `wake_word.py` | 唤醒词解析：从 STT 文本提取「小马」后的指令 |
| `domain/store.py` | 实体 + 待澄清动作（SQLite，`data/agent.db`） |
| `agent/clarify.py` | 澄清引擎：识别歧义、生成问题、合并回答 |
| `agent/propose.py` | 写操作提案/提交：幂等、审计、去重 |
| `agent/confirm.py` | 高风险动作确认：删除/付款等先确认再执行 |
| `agent/entities.py` | 实体管理：新增、改名、别名、列表 |
| `server.py` | FastAPI 服务端：REST + SSE、后台到期提醒、静态页面 |
| `assistant.py` | 核心编排 + 手写 Agent loop；CLI 与服务端共用 |
| `tools.py` | OpenAI function calling 工具注册表（schema + execute） |
| `llm.py` | OpenAI 兼容 `/chat/completions` 客户端（带 tools） |
| `memory.py` | 记忆库：SQLite FTS5 + 中文逐字空格化检索 |
| `items.py` | 物品库：SQLite（名称 / 到期日 / 提醒状态） |
| `config.py` | 集中配置，全部从 `.env` 加载 |
| `stt.py` | FunASR SenseVoice 识别（模型懒加载） |
| `tts.py` | Edge TTS 合成（CLI 播放 / 服务端返回 mp3 字节） |
| `audio_io.py` | PyAudio 录音、pygame 播放 |
| `static/index.html` | 单文件 Web 客户端，无构建步骤 |
| `tests/` | pytest 自动化测试 |

## 演进路线与架构约束

当前处于阶段 1：`entities` / `pending_actions`、电费/牛奶澄清、CLI 关联已落地；优先把家庭场景的澄清和两阶段提交做通用，再扩展 RAG 和多终端。

### 开发顺序

1. 阶段 0：分层重构（`domain/` / `agent/` / `adapters/`），保持现有行为。
2. 阶段 1：`entities` + `pending_actions`，电费/牛奶澄清，两阶段提交。
3. 阶段 2：通用化 `facts` / `tasks` / 提醒。
4. 阶段 3：RAG + 地图/公交/天气。
5. 阶段 4：统一网关 + 小程序/App。
6. 阶段 5：ESP32 与语音终端。

### 硬性约束

- LLM 不得直接写正式表；写操作走「提案 → 实体解析 → 澄清 → 提交」。
- 领域层不 import LLM、FastAPI、音频或具体渠道 SDK。
- 澄清只问关键槽位，一轮最多一个问题，优先给候选。
- 删除/付款/通知他人等高风险动作必须确认。
- RAG 只放静态知识；路线、公交、天气、余额走实时工具。
- 工具返回结构化结果，外部工具必须有超时、缓存和降级。
- 业务数据带 `space_id`，为多家庭/多设备预留隔离。
- 新增产品决策先写入 `docs/ROADMAP.md` 的「待决策」，不要擅自定。

### 规划中的模块（尚未实现）

- `domain/`：实体、事实、任务、提醒。
- `agent/`：抽取、实体解析、澄清策略、执行编排。
- `adapters/`：CLI、Web、API、MQTT/设备。
- `rag/`：文档接入、分块、embedding、检索。

## 运行

要求 Python 3.9+（不要使用 3.10+ 语法，如 `X | None`、`match`）。

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # 填入 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL
```

```bash
python server.py       # Web 服务端，http://127.0.0.1:8000
start.bat              # Windows 一键启动服务端
start.bat cli          # Windows 启动 CLI
python main.py         # CLI 模式
start.bat wake         # Windows 启动唤醒词常驻监听
python main.py wake    # 常驻监听唤醒词「小马」
```

首次启动会下载 FunASR 模型（约 900MB），服务端启动时同步加载，属正常现象。

## 测试

```bash
pip install -r requirements-dev.txt
pytest tests -q
```

- 测试不需要 `.env`：`LLM` 的配置在实例化时校验，测试里用 `FakeLLM` / monkeypatch 注入。
- 手工诊断脚本在 `scripts/record_diagnostic.py`，不会被 pytest 收集。
- 测试里用 `Memory(":memory:")` / `Items(":memory:")` / `AgentStore(":memory:")`，不要碰 `data/`。
- 澄清测试在 `tests/test_clarify.py`；写提案在 `tests/test_propose.py`；确认流程在 `tests/test_confirm.py`；实体管理在 `tests/test_entities.py`。
- 服务端测试通过 `monkeypatch.setattr(server, "assistant", FakeAssistant())` 注入假实现；`assistant` 测试通过注入带 `chat_with_tools` 方法的 FakeLLM 模拟 LLM。

## 必须保持的约定

### Agent 编排

- `Assistant` 是唯一业务编排层，CLI / 服务端不要重复实现业务逻辑。
- Agent loop 最多 `max_agent_loops`（5）轮：LLM → 执行工具 → 工具结果回传 → 再决策。
- `self.llm.chat_with_tools(...)` 对网络错误和畸形响应自动重试；重试耗尽后 agent loop 降级为文字回复，不向 API 抛 500。
- `process_text` 返回的 dict 是对外契约：
  - 通用：`type`、`reply`、`used_tools`、`domain`、`action`
  - `record`：额外带 `summary`
  - `query`：额外带 `memories`（`"[时间] 原文"` 字符串列表）
  - `list` / `delete`：额外带 `memories` / `deleted`
  - 修改 `_agent_loop` 时必须同步维护这些字段，CLI 和 API 都依赖它们。

### 澄清

- 写操作前先过 `Clarifier`；缺关键槽位时返回 `type=clarify`，不要直接写库。
- `pending_action` 按 `conversation_id` 保存，默认 30 分钟过期；一个会话同时只保留一个。
- 一轮只问一个问题，回答后合并成完整文本再进 agent loop。
- CLI 固定传 `client_id="cli"`，否则待澄清动作无法关联。

### 实体

- 实体类型固定为 `property` / `person` / `product` / `provider` / `place` / `device`。
- 新增、改名、别名走 `EntityService`；别名冲突必须报错，不能静默合并。
- 澄清规则只依赖实体和别名，不要把家庭结构硬编码进规则。
- 实体变更写入 `proposals` 审计。

### 写操作

- `memory_store` / `item_add` 不直接写库，统一走 `ProposalService`。
- 提案记录在 `proposals` 表，带 `idempotency_key`；`PROPOSAL_DEDUPE_SECONDS` 内相同内容自动去重。
- 新增写工具也应走 `ProposalService`，为后续确认、审计和回滚留位置。
- 删除类动作（`memory_delete` / `item_delete`）不直接执行，先经 `ConfirmationManager` 请求确认。
- 待确认动作存在 `pending_actions`（`missing_slot=confirm`），用户回复确认/取消后才执行。
- 付款、通知他人等未来高风险动作也走同一入口。

### 工具注册

- 新工具在 `tools.py` 用 `_build(...)` 注册，返回 `{"schema", "execute"}`；`assistant` 会自动聚合，核心循环不用改。
- `execute(**kwargs)` 必须返回**字符串**（直接进 LLM 上下文）。
- 参数名与 JSON Schema 保持一致；`assistant` 会把 `memory_store` 的 `summary`、`memory_search` 的关键词提取进返回值，改动工具名/参数时注意同步。

### 存储与并发

- `Memory` / `Items` 的连接是 `check_same_thread=False`，所有数据库操作必须持有 `RLock`。
- 服务端用 `asyncio.to_thread` 跑同步编排；不要在 async handler 里直接做重 IO。
- 到期状态同步、筛选、标记必须在同一把锁内完成（见 `Items.claim_due_reminders`），避免并发窗口。
- `memories` 是 `memories_raw` + `memories_fts` 双表；检索依赖“逐字空格化”，换分词方案需要数据迁移。

### 提醒与 SSE

- 只有存在在线 SSE 订阅者时才 `claim_due_reminders`，避免没送达却消耗提醒额度。
- 提醒状态落库（`reminded_at`），冷却由 `REMINDER_COOLDOWN_HOURS` 控制。
- SSE 事件格式：`data: {json}\n\n`，心跳为注释行；队列满时丢最旧并累加 `dropped_events`，不要静默丢。
- 前端用 `fetch` 流式读取而非 `EventSource`（需要携带 `Authorization`）。

### 前端音频

- 录音统一转成 16kHz / 单声道 / 16-bit PCM WAV 再上传。
- `srcRate` 必须在 `cleanupAudio()` 之前读取（`audioCtx` 关闭后为 null）。
- 页面是单文件原生 JS，无打包流程；不要引入前端框架/构建工具。

### 唤醒词（CLI）

- 只有 CLI 常驻模式：`python main.py wake`；Web 端仍是按住说话。
- 被动监听用 `record_audio(verbose=False)`，避免每次检测都刷屏。
- 命中唤醒词后，同句带指令就直接执行；只喊唤醒词则播报 `WAKE_ACK_TEXT` 再录一句。
- STT 只在检测到语音段后运行；唤醒词匹配是纯文本处理（`wake_word.extract_wake_command`）。

### 配置与隐私

- 所有可调参数走 `config.py` + `.env`；新增配置项要同步 `.env.example` 和 README 配置表。
- 不提交 `data/`、`.env`、音频文件；日志里不要输出密钥。
- 新增依赖需固定版本（见 `requirements.txt`）。

## 代码风格

- 注释 / docstring 用中文，解释“为什么”而非“做了什么”。
- 保持模块单一职责，避免为一次性需求造抽象。
- 错误处理集中在边界（API 入口、外部服务调用），内部逻辑信任调用方。
- 外部服务（LLM / TTS）失败要降级：能回文字就不要整体 500。

## 常见坑

- 缺少 `.env` 时 `LLM()` 会在启动阶段抛 `RuntimeError`；import 模块本身不受影响，pytest 收集也不会失败。
- `stt.py` 内部才 `import funasr`，导入模块本身不会加载模型。
- `TTS` 的私有事件循环刻意不关闭（Windows Proactor 下 close 会产生告警）。
- agent loop 通过 `self.llm.chat_with_tools(...)` 调用 LLM，测试注入一个带同名方法的假实现即可。
- `_synthesize` 已兜底：TTS 失败只丢语音，文字回复照常返回。
- 唤醒词常驻模式会持续占用麦克风，不要和其他录音模式同时运行。
- `README.md`、`.env.example`、`config.py` 三处配置说明要同步更新。

## 提交前检查

```bash
pytest tests -q
python -m py_compile *.py tests/*.py
git diff --check
```

只在被明确要求时创建 commit。
