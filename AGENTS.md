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
| `domain/store.py` | 实体 + 待澄清动作 + 提案（SQLite，`data/agent.db`） |
| `domain/tasks.py` | 通用任务/提醒：账单、日程、自定义（SQLite，`data/tasks.db`） |
| `domain/facts.py` | 结构化事实：户号、地址、品牌、型号（SQLite，`data/facts.db`） |
| `domain/push.py` | Web Push 订阅：endpoint、密钥、失败计数（SQLite，`data/push.db`） |
| `push_sender.py` | Web Push 发送适配器：`pywebpush` 封装，异常统一转结果 |
| `agent/clarify.py` | 澄清引擎：识别歧义、生成问题、合并回答 |
| `agent/propose.py` | 写操作提案/提交：幂等、审计、去重 |
| `agent/confirm.py` | 高风险动作确认：删除/付款等先确认再执行 |
| `agent/verify.py` | 回复事实校验：口播的日期/星期/名字须与工具参数一致 |
| `adapters/mcp_endpoint.py` | MCP 接入点：小智出站连接，暴露 `ask_xiaoma`，读 `_meta` 声纹身份 |
| `infra/xiaozhi-server/` | 小智侧提醒桥：构建时锚点打补丁，订阅设备通道并调 `speak_txt` |
| `agent/entities.py` | 实体管理：新增、改名、别名、列表 |
| `agent/legacy.py` | 旧数据迁移：记忆分类、日期解析、迁移计划 |
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

当前优先 Web 控制台：聊天之外增加统计、列表、系统配置；随后 Android App；RAG 延后。CLI 和现有服务端能力保留。

### 开发顺序

1. 阶段 0：分层重构（`domain/` / `agent/` / `adapters/`），保持现有行为。
2. 阶段 1：`entities` + `pending_actions`，电费/牛奶澄清，两阶段提交。
3. 阶段 2：通用化 `facts` / `tasks` / 提醒。
4. 阶段 3：Web 控制台（统计 / 列表 / 系统配置）。
5. 阶段 4：Android App。
6. 阶段 5：RAG + 本地生活工具（延后）。
7. 阶段 6：ESP32 与语音终端。

### 硬性约束

- LLM 不得直接写正式表；写操作走「提案 → 实体解析 → 澄清 → 提交」。
- 领域层不 import LLM、FastAPI、音频或具体渠道 SDK。
- 澄清只问关键槽位，一轮最多一个问题，优先给候选。
- 删除/付款/通知他人等高风险动作必须确认。
- RAG 只放静态知识；路线、公交、天气、余额走实时工具。
- 工具返回结构化结果，外部工具必须有超时、缓存和降级。
- 多终端阶段引入 `space_id`，为多家庭/多设备预留隔离；当前单家庭版本先集中在默认空间。
- 新增产品决策先写入 `docs/ROADMAP.md` 的「待决策」，不要擅自定。

### 规划中的模块（尚未实现）

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
- 正确率基线在 `scripts/eval_accuracy.py`：32 条固定输入（含摄像头决策两条）、真模型、每个用例独立内存库，
  卡类型/工具/库状态/关键事实而不是措辞。慢且有网络依赖，按需手动跑；
  已知问题的用例标 `known=True`，不计入失败。`case()` 的 `device_camera=True` 走「终端带摄像头」分支。
- 设备联调探针在 `scripts/xiaozhi_probe.py`（合成一句话 → 走小智协议 → 打印识别与回答），搭建步骤见 `docs/xiaozhi-setup.md`。
- 摄像头整条链用 `scripts/fake_camera_device.py --image 图.jpg`：假设备报工具、收调用、把图 POST 到
  `vision_explain`，验证 `tool_calls` 名字、设备驱动和播报。它先试 LAN 地址再退 `127.0.0.1`，
  **不能替代**「另一台设备能不能连 8300/8303」的验证。
- 旧数据迁移：`python scripts/migrate_legacy.py`（默认 dry-run，`--apply` 才写入并自动备份）。迁移逻辑在 `agent/legacy.py`，测试在 `tests/test_legacy.py`。
- 测试里用 `Memory(":memory:")` / `Items(":memory:")` / `AgentStore(":memory:")`，不要碰 `data/`。
- 澄清测试在 `tests/test_clarify.py`；写提案在 `tests/test_propose.py`；确认流程在 `tests/test_confirm.py`；实体管理在 `tests/test_entities.py`；任务/事实在 `tests/test_tasks.py` / `tests/test_facts.py`。
- 服务端测试通过 `monkeypatch.setattr(server, "assistant", FakeAssistant())` 注入假实现；`assistant` 测试通过注入带 `chat_with_tools` 方法的 FakeLLM 模拟 LLM。
- Web API 的统计/列表/搜索/分页/删除/编辑/配置测试在 `tests/test_server.py`；配置写入测试用 `monkeypatch.setattr(server, "ENV_PATH", ...)` 指向临时文件。

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
  - `device_call`：额外带 `device_call`（`{"name", "arguments"}`），`reply` 为空——这一轮交给设备执行，`/v1` 据此回 OpenAI `tool_calls`
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

- `memory_store` / `item_add` / `task_add` / `fact_store` 不直接写库，统一走 `ProposalService`。
- 提案记录在 `proposals` 表，带 `idempotency_key`；`PROPOSAL_DEDUPE_SECONDS` 内相同内容自动去重。
- 新增写工具也应走 `ProposalService`，为后续确认、审计和回滚留位置。
- 同名物品在 `ProposalService.add_item` 里合并：带新日期就更新到期日（清 `reminded_at`、状态退回 `active`），不带日期就保留原值，避免清单出现两条同名物品、提醒两次。
- `Facts.store` 同一「主体 + 属性」再次保存会把旧行标 `valid_to`、新行记 `valid_from`，返回值带 `replaced`；检索默认只返回当前有效的，看历史用 `include_history=True` / `history()`。不要改回直接覆盖（丢历史）或只插新行（模型会拿到过期值）。
- `Facts.search` 的 `query` 会拆成二字组、逐词 AND 匹配「主体+属性+值」拼接文本（D46）：整串连续子串的要求太脆，「水费户号」跨 subject/predicate 就查不到。拆词不放松到「命中任一」，否则「自己家电费户号」会把水费那条也捞出来；不做同义改写（「账号」对「户号」仍查不到）。
- 删除类动作（`memory_delete` / `item_delete`）不直接执行，先经 `ConfirmationManager` 请求确认。
- 写工具参数在 agent loop 里先过 `agent/guard.py`：日期一律以 `agent/dates.py` 从用户原话解析的结果为准，句子里没有日期线索就把日期留空；`task_add` 的标题在原话里找不到依据时整次调用不执行。
- 所有「念给用户听」的写操作文案统一走 `agent/wording.py`：日期说成「11月25日（周三）」或「明天」，确认句引用用户原话，执行后只补模型没说到的日期。不要在工具或编排里自己拼句子——三处各写一遍，日期格式就会三处不一致，而语音里念成「二零二六 杠 十一 杠 二五」用户根本听不出对错。
- 记忆摘要只能用原话里已有的字（`agent/guard.py` 的 `summary_grounded`，子序列判据）：模型会把「明天」算成「8月9日」，而摘要进 FTS 索引，编造的日期会让这条记忆查不到或查到错的。校验不过就**用原话当摘要**，不拒收记忆本身——记忆是用户的真实陈述，摘要是可降级的。
- `task_postpone` 的日期也由护栏换算：用户说「明天」「下周一」是**目标日期**（绝对改期），只有说不出具体日期才退化成「原到期日 + N 天」。
- 改期只给日期时保留原时刻（`domain/tasks.py` 的 `_keep_clock`）：用户说「取快递改到后天」，指的是还是下午三点那个提醒；原话里带了时间才覆盖。
- 回复发出前过 `agent/verify.py`：模型口播的日期、星期、**时刻**、引号里的名字必须和工具参数一致，不一致就整句换成 `wording.written_sentence` 拼的模板句——不修补、不拼接，避免「一句对一句错」。这一轮发生过写入时最终回答不走流式（推出去的片段收不回来）。时刻只在真的写了时间时才核（只写日期时，「一点」这类词会被误判）。
- **写操作之后仍由模型组织回话**（`4554eae` 曾跳过第二轮，`D45` 已撤回）。参数里的模板只用于两处：回复校验失败时的兜底、确认模式执行后的回显。不用它代替模型总结，宁可多等 2 秒。
- 关键词查询（`memory_search` / `item_search` / `fact_search` / `family_search` / `home_query`）全空时，**在答复轮之前**直接回 `wording.EMPTY_QUERY_REPLY`（「我这边没查到相关记录。你要查的是哪件事？」），不再花一轮生成。ASR 同音会把「记」听成「寄」，让模型顺着错词答（还会追问「寄给谁」），等于告诉用户「它听不懂我」。语音是流式播报，模型的话一出口就收不回来，所以必须在生成前短接、不能事后替换。清单类（`task_list` / `item_list`）不做这个替换。
- 延迟优化只能改「怎么说」，不能改「说什么」：不为了快跳过校验、不用关键词路由代替理解、不用模板掩盖失败（D43，2026-11-23 定）。事实被覆盖时确认回显要补 `wording.replaced_note(result)` 的「原值已失效」。
- 护栏只负责落库正确，模型仍可能口播旧值；`correction_note` 会把校正结果附在工具返回里让它改口。
- 本地小模型开 `CONFIRM_WRITE_ACTIONS=1`（弱模型模式）：四个写工具执行前先复述参数等用户确认，确认后才真正落库；同时拦下「没调工具却回已记好」的假确认。代价是每次写多一句话。
- 待确认动作存在 `pending_actions`（`missing_slot=confirm`），用户回复确认/取消后才执行。
- 付款、通知他人等未来高风险动作也走同一入口。

### 工具注册

- 新工具在 `tools.py` 用 `_build(...)` 注册，返回 `{"schema", "execute"}`；`assistant` 会自动聚合，核心循环不用改。
- `execute(**kwargs)` 必须返回**字符串**（直接进 LLM 上下文）。
- 参数名与 JSON Schema 保持一致；`assistant` 会把 `memory_store` 的 `summary`、`memory_search` 的关键词提取进返回值，改动工具名/参数时注意同步。
- `camera_look` 是唯一的例外：照片在设备上，`execute` 只在没有摄像头的入口（CLI / 网页）被调用，回一句「看不到」。`/v1` 只在请求 `tools` 里出现 `self.camera.take_photo` 时才把它的 schema 给模型；模型一调用就转成 `device_call` 收尾，不执行、不落库、不写会话（写进去下一句会被带着继续拍）。
- 小智会把设备工具名的点换成下划线（`self.camera.take_photo` → `self_camera_take_photo`）再给 LLM；`/v1` 回 `tool_calls` 时**必须用请求里那个名字**，用带点的原始名它查不到工具，拍照会静默失败。判断有没有摄像头也要按归一化后的名字比。
- `/v1` 里带 `image_url` 的请求走 `Assistant.describe_image`，不进 agent、不进记忆；图片只在内存里用一次，日志不打印 base64。描述本身在 `vision_context` 里留 `VISION_CONTEXT_SECONDS`（默认 180 秒）供下一句追问，过期即删、不进记忆库。

### 存储与并发

- `Memory` / `Items` / `Tasks` / `Facts` / `AgentStore` 的连接是 `check_same_thread=False`，所有数据库操作必须持有 `RLock`。
- 服务端用 `asyncio.to_thread` 跑同步编排；不要在 async handler 里直接做重 IO。
- 到期状态同步、筛选、标记必须在同一把锁内完成（见 `Items.claim_due_reminders`），避免并发窗口。
- `memories` 是 `memories_raw` + `memories_fts` 双表；检索依赖“逐字空格化”，换分词方案需要数据迁移。
- FTS 索引的是 summary，原文里独有的词会漏；`Memory._search_rows` 在未命中时用 LIKE 兜底扫 `summary` 和 `text`，通配符已转义。别把兜底删掉，否则「记过但查不到」会重现。

### 提醒与 SSE

- 提醒循环同时扫描 `items`（物品到期）和 `tasks`（账单/日程/自定义）。
- 投递顺序固定为 **设备 → 网页 → Web Push**，同一提醒只走一条：设备在线时页面不弹、手机不推。
- 设备通道是同一 SSE 端点的 `?role=device`（小智提醒桥用）；设备事件**不带 mp3**，设备用自己的 TTS 念。
- 只有存在「在线设备、在线 SSE 订阅者或未停用的 push 订阅」时才 `claim_due_reminders`，避免没送达却消耗提醒额度。
- 提醒状态落库（`reminded_at`），冷却由 `REMINDER_COOLDOWN_HOURS` 控制。
- SSE 事件格式：`data: {json}\n\n`，心跳为注释行；队列满时丢最旧并累加 `dropped_events`，不要静默丢。
- 前端用 `fetch` 流式读取而非 `EventSource`（需要携带 `Authorization`）。

### Web Push

- 有页面在线走 SSE，没有页面在线才发 push：同一提醒不走两条通道，避免弹两次。
- `push_sender.py` 是唯一 import `pywebpush` 的地方；其它模块只处理 `PushResult`。
- 404/410 视为订阅作废，立即删除；其它失败累加 `fail_count`，到 `PUSH_MAX_FAILURES` 停用但保留记录。
- `VAPID_PRIVATE_KEY` 只在 `.env`，不进 `CONFIG_FIELDS`、不进 `/api/config` 响应。
- 推送正文只发文字，不合成语音；点通知打开应用并**在聊天里显示这条提醒原文**（不是只停主页）：
  SW 发通知时把正文写进 IndexedDB `last`，点击时若页面已开着就 `postMessage`，否则用 `/?reminder=1` 打开，页面读 IndexedDB 渲染并清掉查询参数。
- `pushsubscriptionchange` 由 SW 自己处理：需要公钥和 Token，页面在开启通知时写进 IndexedDB（库名 `xiaoma-push`），关闭通知时删除。改存储结构要同时改 `static/index.html` 和 `static/sw.js` 两处。

### MCP 接入点（小智）

- 方向别搞反：**小智服务端是 MCP 客户端**，出站连我们。路由 `/mcp_endpoint/mcp/`，裸 JSON-RPC 2.0（`initialize` / `tools/list` / `tools/call`），不要改成官方 MCP SDK。
- 只暴露一个工具 `ask_xiaoma`；`params._meta` 里的 `speakerId/speakerName` 是**不可信输入**，只用来分会话上下文（`client_id = "mcp:<speakerId>"`），不参与鉴权、不能绕过写操作护栏。
- `MCP_TOKEN` 为空时接入点整体关闭；令牌走查询参数，因为小智的连接 URL 就这么带。协议形状以 `docs/xiaozhi-mcp.md` 为准，改了要同步 `scripts/mcp_probe.py`。
- 小智会把配置里的 `/mcp/` 自动改写成 `/call/`；`server.py` 把 `/mcp_endpoint/mcp/` 与 `/mcp_endpoint/call/` 都指向同一个处理器，别删别名。

### Web 控制台

- 页面保持单文件原生 JS，无构建步骤。
- 聊天记录存本机 `localStorage`（`hst_chat_history`，最多 120 条），启动时还原；报错提示和欢迎语不入库，语音播放按钮不持久化。要做多设备同步再考虑落到服务端表。
- 聊天、统计、列表、配置共用同一套 API；不要在前端重复业务逻辑。
- 列表/统计走只读 API；任务完成/取消走 tasks，删除仍走确认。
- 系统配置只允许白名单字段写 `.env`，密钥绝不返回前端；保存后提示重启生效。
- `server.py` 的 `CONFIG_FIELDS` 与 `static/index.html` 的 `CONFIG_FIELDS` 要保持同步。
- 列表接口支持 `q` / `status` / `limit` / `offset`，返回 `total`；前端每页 20 条。
- 删除接口必须带 `confirm=true`；前端还要弹二次确认，不能静默删除。
- 任务编辑走 `PATCH /api/tasks/{id}`，只允许改标题、时间、类型。
- 新增列表接口时保持只读；需要写操作就复用现有任务/确认逻辑。
- 移动端优先，所有操作要能在手机浏览器完成。

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
- LLM 请求莫名变慢（65 s 级）先看 `LLM_PROXY`：设了 `NO_PROXY` 环境变量的机器上，requests 会跳过 Windows 注册表里的系统代理，变成裸连远端；本机走代理可能只要几秒。
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
