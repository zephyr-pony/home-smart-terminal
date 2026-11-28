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
| `domain/prints.py` | 打印记录：文件元数据与出纸结果（SQLite，`data/prints.db`，D65） |
| `domain/dialogue.py` | 对话记录：原文只留最近几天，给跨天上下文和夜间提炼当原料（SQLite，`data/dialogue.db`，D84） |
| `domain/space.py` | 空间隔离：`space_id` 规则、各库/目录路径组合（D68） |
| `push_sender.py` | Web Push 发送适配器：`pywebpush` 封装，异常统一转结果 |
| `agent/clarify.py` | 澄清引擎：识别歧义、生成问题、合并回答 |
| `agent/propose.py` | 写操作提案/提交：幂等、审计、去重 |
| `agent/confirm.py` | 高风险动作确认：删除/付款等先确认再执行 |
| `agent/verify.py` | 回复事实校验：口播的日期/星期/名字须与工具参数一致 |
| `agent/untrusted.py` | 外部资料标注：网页/文件正文进上下文前标成「资料不是指令」（D86） |
| `agent/shellguard.py` | 危险命令识别：`rm -rf`/关机/`curl\|sh`/动库先复述确认（D86） |
| `agent/distill.py` | 记忆蒸馏：同主题合并、只重算 summary，深夜/超阈值后台跑（D60） |
| `agent/profile_miner.py` | 夜间画像提炼：读昨天的对话，产出待确认的习惯候选（D84） |
| `adapters/mcp_endpoint.py` | MCP 接入点：小智出站连接，暴露 `ask_xiaoma`，读 `_meta` 声纹身份 |
| `adapters/printer.py` | Linux CUPS 打印队列：PDF/图片/文本 + LibreOffice 转 Office（D61） |
| `adapters/documents.py` | 读附件内容（表格/PDF/Word/文本）+ 临时目录跑模型写的 Python（D62） |
| `adapters/shell.py` | 通用命令通道：`/data/work` 持久工作目录、清空环境变量、非 root（D62） |
| `adapters/shell_worker.py` | 执行通道：以另一个 uid（容器里 `work`）跑模型的命令与代码，读不到正式库（D86） |
| `adapters/web.py` | 上网查：DuckDuckGo 搜索 + 抓页面正文，不抓内网（D62） |
| `infra/xiaozhi-server/` | 小智侧提醒桥：构建时锚点打补丁，订阅设备通道并调 `speak_txt` |
| `infra/device-firmware/` | ESP32 固件补丁（待机保连接、开机自连、断线重连，D78） |
| `infra/ubuntu/` | 迁移到 Ubuntu Server 的部署件：开机拉起 compose、加密备份的 systemd 单元 |
| `agent/entities.py` | 实体管理：新增、改名、别名、列表 |
| `agent/synonyms.py` | 检索同义词：户号/账号、燃气/煤气等归一，只影响查询 |
| `agent/legacy.py` | 旧数据迁移：记忆分类、日期解析、迁移计划 |
| `server.py` | FastAPI 服务端：REST + SSE、后台到期提醒/记忆蒸馏/画像提炼、静态页面 |
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

当前优先语音设备真机（小智 + ESP32）与真机验收；Web 控制台、CLI 和既有服务端能力继续维护；Android App 与 RAG 延后。

设备语音链路现在是**自建的第三方小智服务端**（`infra/xiaozhi-server`），它是部署件、不是小马的模块依赖：小马本身只提供 `/v1` 和 MCP 接入点，网页/手机/API 不装它也能用。终局是把语音管线（WS 协议、Opus、VAD、打断）做进小马，见 ROADMAP D57。

「要不要结束会话」这类判断交给模型：小智 `exit_commands` 留空、模型调 `handle_exit_intent`；编排层也没有关键词预检（2026-11-27 去掉，见下「Agent 编排」）。

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

### 规划中的模块

- `adapters/`：已有 `home.py` / `home_ws.py`（Home Assistant）、`mcp_endpoint.py`（小智）；
  CLI 入口仍在 `main.py`、Web/API 在 `server.py`，尚未迁进 `adapters/`；MQTT 未做。
- `rag/`：文档接入、分块、embedding、检索。

## 运行

要求 Python 3.9+（不要使用 3.10+ 语法，如 `X | None`、`match`）。

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
pip install -r requirements-server.txt   # 网页/API/提醒/推送
pip install -r requirements-stt.txt      # 语音识别（约 2GB，可不装）
pip install -r requirements-cli.txt      # CLI 录音/播放（可不装）
# 或者一次装全：pip install -r requirements.txt

cp .env.example .env   # 填入 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL
```

```bash
python scripts/doctor.py   # 启动前自检：配置 / 数据目录 / 端口 / 可选模块
python server.py       # Web 服务端，http://127.0.0.1:8000
start.bat              # Windows 一键启动服务端
start.bat cli          # Windows 启动 CLI
python main.py         # CLI 模式
start.bat wake         # Windows 启动唤醒词常驻监听
python main.py wake    # 常驻监听唤醒词「小马」
```

首次启动会下载 FunASR 模型（约 900MB），服务端启动时同步加载，属正常现象。
只想文字调试时设 `STT_ENABLED=0`：不加载模型，`/api/voice` 返回 503，其它入口不受影响。

Docker：`Dockerfile` 默认 `INSTALL_STT=0`（只装 `requirements-server.txt`），
`--build-arg INSTALL_STT=1` 才装 CPU 版 torch 与 FunASR；容器内 `SERVER_HOST=0.0.0.0`，
所以 `API_TOKEN` 必填，否则 `_check_exposure()` 拒绝启动。数据在 `/data`（`DATA_DIR`）。

## 测试

```bash
pip install -r requirements-dev.txt
pytest tests -q
```

- 测试不需要 `.env`：`LLM` 的配置在实例化时校验，测试里用 `FakeLLM` / monkeypatch 注入。
- 不要把时区写死（比如 `+08:00`）：CI 在 UTC 跑，涉及时刻的断言用「本机时区」倒推（见 `test_home_watch`）。
- 手工诊断脚本在 `scripts/record_diagnostic.py`，不会被 pytest 收集。
- 正确率基线在 `scripts/eval_accuracy.py`：43 条固定输入（含摄像头决策 2 条、周期提醒 4 条、软硬提醒 2 条、同义检索 2 条、提醒绑定 3 条）、真模型、每个用例独立内存库，
  卡类型/工具/库状态/关键事实而不是措辞。慢且有网络依赖，按需手动跑；
  已知问题的用例标 `known=True`，不计入失败。**单次结果有 LLM 抖动**（2026-11-27 连跑四次：
  40/41、40/41、40/41、41/41，每次挂的是不同用例）；判断回归看同一类用例是否连续下滑，
  别按单次数字下结论，也别为了让某条变绿去反复调工具描述——那是拟合噪声。`case()` 的 `device_camera=True` 走「终端带摄像头」分支；
  `bound_task="latest"`（或具体 id）模拟「点开提醒」，配合 `not_tools` 断言没有多调 `task_list`。
  热点上下文（D59）已把事实/记忆塞进提示词，答案正确、只是没调工具的用例可以用
  `context_ok=True` 放行（回复内容和库状态照验，通过行会标「上下文作答」）。
  别把日期写死；「下周三」用脚本里的 `NEXT_WEEK_WED`（2026-11-26 修过一次：
  周四~周日会多算一周）。
- 生产数据在 Docker 命名卷里，备份用 `scripts/backup_docker.py`（容器里 sqlite 热备 →
  宿主机打包）；`scripts/backup.py --data-dir` 是它复用的底层，单独跑只备仓库 `data/`。
  恢复要求 `--yes`（会先停用该卷的容器），演练用临时卷，见 `docs/backup.md`。
  **离开本机的那一份必须加密**：`--encrypt`，口令只走 `BACKUP_PASSPHRASE` 或
  `--passphrase-file`（别写进命令行历史），产物是 `.zip.enc`，明文 zip 加密后立即删除。
  备份不能只留在同一块盘上——单盘部署时盘坏了原数据和备份一起没。
- 设备联调探针在 `scripts/xiaozhi_probe.py`（合成一句话 → 走小智协议 → 打印识别与回答），搭建步骤见 `docs/xiaozhi-setup.md`。
- 摄像头整条链用 `scripts/fake_camera_device.py --image 图.jpg`：假设备报工具、收调用、把图 POST 到
  `vision_explain`，验证 `tool_calls` 名字、设备驱动和播报。它先试 LAN 地址再退 `127.0.0.1`，
  **不能替代**「另一台设备能不能连小智端口（当前 8400/8403）」的验证。
- 旧数据迁移：`python scripts/migrate_legacy.py`（默认 dry-run，`--apply` 才写入并自动备份）。迁移逻辑在 `agent/legacy.py`，测试在 `tests/test_legacy.py`。
- 测试里用 `Memory(":memory:")` / `Items(":memory:")` / `AgentStore(":memory:")`，不要碰 `data/`。
- 澄清测试在 `tests/test_clarify.py`；写提案在 `tests/test_propose.py`；确认流程在 `tests/test_confirm.py`；实体管理在 `tests/test_entities.py`；任务/事实在 `tests/test_tasks.py` / `tests/test_facts.py`；对话记录在 `tests/test_dialogue.py`，夜间提炼在 `tests/test_profile_miner.py`。
- `tests/conftest.py` 默认把 `domain.dialogue.DIALOGUE_ENABLED` 关掉（否则每个 `Assistant()` 都会建 `data/dialogue.db`）；要测记录行为就显式传 `DialogueStore(":memory:", enabled=True)`。
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
  - `delete`：额外带 `deleted`（删除确认后真的删掉的那几条）
  - `device_call`：额外带 `device_call`（`{"name", "arguments"}`），`reply` 为空——这一轮交给设备执行，`/v1` 据此回 OpenAI `tool_calls`
  - 修改 `_agent_loop` 时必须同步维护这些字段，CLI 和 API 都依赖它们。
- **没有内置命令预检**（2026-11-27 去掉）：以前「看看我的记忆」「忘掉酸奶」按关键词直接分流、不进模型，还会误伤（「取消取快递」先被当成删记忆）。现在一律进 agent loop，由模型选 `memory_search` / `memory_list` / `memory_delete`；删除仍走确认。代价是这两类每轮多一次 LLM（语音约 +2 秒）。
- `process_text(..., task_id=)` / `process_voice(..., task_id=)`：用户点开到期提醒时带上那条任务，
  `_agent_loop` 只在这一轮注入一条 system 提示（`_task_context_note`），让「完成了 / 延期」
  直接落到那条任务，省掉 `task_list` 一轮；任务不存在或已完成时如实说明，不改别的任务。
  **只注入上下文，不代执行**——写操作仍走工具、护栏和确认；提示不进会话历史。
  `/api/chat` 的 `task_id`、`/api/voice` 的表单字段、Web Push 的 `task_id`/`task_title`/`kind`
  都走这条链；MCP（`ask_xiaoma`）目前没有任务上下文。

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

- `memory_store` / `item_add` / `task_add` / `fact_store` / `profile_set` 不直接写库，统一走 `ProposalService`。
- `profile_set` 是「模型推断的偏好」，**无条件**先确认再落库，不跟 `CONFIRM_WRITE_ACTIONS` 走；
  用户自己说出的事实仍走 `fact_store` 直接写。画像主体统一挂 `用户` / `用户/<名字>` 前缀
  （`domain/facts.py` 的 `profile_subject`），热点上下文靠它识别画像（D59）。
- 画像候选只有两条来路（D84）：用户当面说的（`profile_set` 当场确认，D59 不变）和
  **夜里提炼**的（`agent/profile_miner.py`）。提炼读最近 24 小时的对话，产物只是
  `proposals`（`action_type=profile_set`、`status=proposed`）；网页「概览 → 待确认的习惯」
  点「是，记住」才落 `用户/...`，点「不是」标 `cancelled` 并且不再提。**证据必须是用户原话的
  子串**，对不上整条丢掉——模型编证据比编结论容易得多。一次性的请求、对助手的要求、
  户号地址这类事实，都不算偏好。
- 提案记录在 `proposals` 表，带 `idempotency_key`；`PROPOSAL_DEDUPE_SECONDS` 内相同内容自动去重。
- 新增写工具也应走 `ProposalService`，为后续确认、审计和回滚留位置。
- 同名物品在 `ProposalService.add_item` 里合并：带新日期就更新到期日（清 `reminded_at`、状态退回 `active`），不带日期就保留原值，避免清单出现两条同名物品、提醒两次。
- `Facts.store` 同一「主体 + 属性」再次保存会把旧行标 `valid_to`、新行记 `valid_from`，返回值带 `replaced`；检索默认只返回当前有效的，看历史用 `include_history=True` / `history()`。不要改回直接覆盖（丢历史）或只插新行（模型会拿到过期值）。
- `Facts.search` 的 `query` 会拆成二字组、逐词 AND 匹配「主体+属性+值」拼接文本（D46）：整串连续子串的要求太脆，「水费户号」跨 subject/predicate 就查不到。拆词不放松到「命中任一」，否则「自己家电费户号」会把水费那条也捞出来。
- 同义说法在**检索侧**归一（`agent/synonyms.py`：户号/账号、燃气/煤气/天然气、宽带/网费……）：查询串和库里的文本都 `canonicalize` 之后再比，所以存「户号」、问「账号」也能命中，两个方向都通。只收真正可互换的词，不做单字替换，也不收「水费/水表」这类相关但不同的说法——放宽一个词不会放松同一查询里的其它词（「电费账号」不会捞出「水费户号」），宁可查不到也不查错。写入内容不受影响，用户原话照存。
- 事实挂实体（D66）：`facts.entity_id` 由 `Assistant` 注入的 `AgentStore.find_any_entity` 在存的时候按主体最长匹配填；`Facts.search` 先把查询里命中的实体名下的事实并进来，再用去掉实体名之后剩的词筛。主体文字不改成规范名（用户原话照存），画像 `用户/...` 不挂实体。新增「东西的别名」只需加实体别名，不要往 `agent/synonyms.py` 里加（那是通用说法，不是家里的东西）。
- 记忆也认实体（D83）：`Memory.set_entity_lookup` 同样由 `Assistant` 注入 `find_any_entity`，但**不建关联表、不在写入时挂实体**——查询里认出实体就把它的规范名和别名再捞一轮（存「净水器」、问「饮水机」也查得到），老记忆零迁移立刻受益。补召回排在原文命中**之后**（关键词是模型挑的，通常更准；硬置顶会让「家里」这类宽别名把无关记忆顶到前面），只加在 `search` / `search_details`；删除（`delete_by_keywords` / `find_matching`）**不展开别名**，「忘掉饮水机」不该顺手删掉写成「净水器」的那些。实体解析在记忆库的锁外做，别把两把锁套起来。
- 家里城市存 facts（D55）：用户说「我家在杭州」走 `fact_store` 记成 `家 / 所在城市`，`Assistant._home_city()` 注入给 `Weather`，天气、看板共用一份；`.env` 的 `WEATHER_CITY` 只作兜底。城市是运行时才说的，天气工具不能等「配置了城市」才注册；没城市时如实回「还不知道城市」，由模型问用户，别猜一个城市去查。读取时只认主体是「家」这一类（家/我家/我的家/家里/自家/家庭）、属性是城市（所在城市/城市/城市名）的当前事实，别把「爸妈家」当成自己家，也别把「位置」这类可能是地址的事实当城市。
- 删除类动作（`memory_delete` / `item_delete`）不直接执行，先经 `ConfirmationManager` 请求确认。
- 写工具参数在 agent loop 里先过 `agent/guard.py`：日期一律以 `agent/dates.py` 从用户原话解析的结果为准，句子里没有日期线索就把日期留空；`task_add` 的标题在原话里找不到依据时整次调用不执行。
- 所有「念给用户听」的写操作文案统一走 `agent/wording.py`：日期说成「11月25日（周三）」或「明天」，确认句引用用户原话，执行后只补模型没说到的日期。不要在工具或编排里自己拼句子——三处各写一遍，日期格式就会三处不一致，而语音里念成「二零二六 杠 十一 杠 二五」用户根本听不出对错。
- 记忆摘要只能用原话里已有的字（`agent/guard.py` 的 `summary_grounded`，子序列判据）：模型会把「明天」算成「8月9日」，而摘要进 FTS 索引，编造的日期会让这条记忆查不到或查到错的。校验不过就**用原话当摘要**，不拒收记忆本身——记忆是用户的真实陈述，摘要是可降级的。
- `task_postpone` 的日期也由护栏换算：用户说「明天」「下周一」是**目标日期**（绝对改期），只有说不出具体日期才退化成「原到期日 + N 天」。
- 改期只给日期时保留原时刻（`domain/tasks.py` 的 `_keep_clock`）：用户说「取快递改到后天」，指的是还是下午三点那个提醒；原话里带了时间才覆盖。
- 回复发出前过 `agent/verify.py`：模型口播的日期、星期、**时刻**、引号里的名字必须和工具参数一致，不一致就整句换成 `wording.written_sentence` 拼的模板句——不修补、不拼接，避免「一句对一句错」。这一轮发生过写入时最终回答不走流式（推出去的片段收不回来）。时刻只在真的写了时间时才核（只写日期时，「一点」这类词会被误判）。
- 回复里的**长数字**（6 位以上，户号/电话/卡号）必须有出处：本轮 system prompt（含热点上下文）、会话历史、用户原话、工具返回之一。没有就整句换成 `wording.UNVERIFIED_NUMBER_REPLY`——号码念错比不给答案糟得多。门槛定 6 位是为了放过年份、页码、时刻、金额。
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
- **工具描述也是代码（D75）**：只写真实能力和边界，不写「只有…才可用」「没有网络」这类没核实的话，
  也不许提已经删掉的工具（`file_process` 就这样让模型空调过）。删工具或改行为时，
  全仓搜一遍描述；`tests/test_tools.py` 有一条守卫拦已删工具名。
- 参数名与 JSON Schema 保持一致；`assistant` 会把 `memory_store` 的 `summary`、`memory_search` 的关键词提取进返回值，改动工具名/参数时注意同步。
- `camera_look` 是摄像头入口的例外：照片在设备上，`execute` 只在没有摄像头的入口（CLI / 网页）被调用，回一句「看不到」。`/v1` 只在请求 `tools` 里出现 `self.camera.take_photo` 时才把它的 schema 给模型；模型一调用就转成 `device_call` 收尾，不执行、不落库、不写会话（写进去下一句会被带着继续拍）。
- 小智会把设备工具名的点换成下划线（`self.camera.take_photo` → `self_camera_take_photo`）再给 LLM；`/v1` 回 `tool_calls` 时**必须用请求里那个名字**，用带点的原始名它查不到工具，拍照会静默失败。判断有没有摄像头也要按归一化后的名字比。
- **服务端插件透传（结束会话）**：`/v1` 把请求 `tools` 里的 `handle_exit_intent` 原样交给模型（`Assistant.process_text(extra_tools=...)`），模型一调用就回 `tool_calls`，由**小智服务端自己执行**——它执行时置 `close_after_chat`，告别语播完就关连接，设备重连回待机。没有这条，说「再见」之后服务端不结束会话，设备就停在「聆听中」（2026-11-28 真机踩到）。透传工具不在本机执行，别在 `tools.py` 里再实现一个同名的。
- `/v1` 里带 `image_url` 的请求走 `Assistant.describe_image`，不进 agent、不进记忆；图片只在内存里用一次，日志不打印 base64。描述本身在 `vision_context` 里留 `VISION_CONTEXT_SECONDS`（默认 180 秒）供下一句追问，过期即删、不进记忆库。
- 打印（D61）：`printer_print` 只要有打印机就把 schema 给模型（D62 起不要求本轮先有附件，
  否则「run_python 生成文件再打印」走不通）；不经过 `execute`，确认后由
  `ConfirmationManager.print_runner` 调 `adapters/printer.py`。
  文件经 `POST /api/print/files` 落到 `PRINT_FILES_DIR`（默认 `DATA_DIR/print`，容器里在卷上），
  元数据进 `prints.db`（`domain/prints.py`，D65）；上传时登记的 `client_id` 只约束按 file_id 直接取。
  重启/重建容器后「刚才的文件再打一份」仍然认得（`PRINT_REUSE_HOURS`，默认 24 小时），
  默认留 `PRINT_KEEP_DAYS` 天；**打印成功不删文件**（卡纸重打很常见）。
  每次出纸/失败/取消都要 `PrintStore.mark(...)`，`GET /api/prints` 和概览页「最近打印」
  靠它；概览的「再打一份」就是带着原 `file_id` 再走一次确认，不要新开直接出纸的接口。
  这份「最近文件」**各端共用**（D64）：单家庭阶段不分设备，上多用户再按 `space_id` 拆。
  服务器是 Linux，打印走 CUPS（`lp` / `lpstat`），Office 用 LibreOffice 转 PDF；
  不要写任何 Windows 专用代码（打印队列、GDI、Office COM 都不在支持范围）。
  **纯文本里有非 ASCII（中文）必须先转 PDF**（D77）：CUPS 的文本过滤器只认单字节字体，
  中文 txt 直打是一页乱码；`text_needs_pdf()` 判断，纯 ASCII 仍直打。没装 LibreOffice
  就明说「会乱码、请另存为 PDF」，别打废纸。
- 打印机状态（`GET /api/printer`）给概览页用：在线/队列/积压/原因，30 秒缓存
  （`Assistant.printer_status`），打印机断电时 `lpstat` 会等到超时，轮询不能每次去碰它。
  `Printer.status()` 不抛异常，原因放 `error`；上传接口的 503 也带上这个原因，
  别只说「没有可用的打印机」。网页是「先看能不能打，再传文件」，不是传完才报错。

### 通用能力，不按需求加工具（D62）

- 用户的每个新想法都不该变成一个新参数或新工具（「缩到一页」「转格式」「两列相乘」……）。
  先问缺的是哪种**通用能力**：看见文件、查环境里有什么、跑代码/命令、上网查。
- 对应通道：`file_read`（读内容）、`find_tools`（查已装命令和包）、`run_python`
  （临时目录跑代码，产出新文件后自动成为待打印文件）、`run_shell`（bash，`/data/work`
  持久、`pip install --user` 装到 `/data/home`）、`web_search` / `web_fetch`。
- 这些工具的描述只写**能力与边界**（能干什么、装不装得下、有没有网络），不写操作步骤；
  具体用哪个库、走哪条命令，由模型自己查、自己定。
- 执行类工具的安全边界必须保持：子进程清空环境变量（密钥不进命令）、有超时、
  以另一个 uid 运行（D86，读不到正式库）、`web_fetch` 不抓内网地址。它们**不是**
  沙箱，别在文档里说成沙箱。
- 新增能力优先做成「通道 + 清单」，而不是「功能开关」；确实需要固定契约的
  （打印、写库）才做专用工具。
- **时间/算术/农历（D72）**：`time_now` 给时刻和日期推算（系统提示只有日期，模型
  看不到表、跨月跨年会算错）；`calc` 是 `ast` 白名单求值（不是 `eval`，也拦了
  `9**9**9` 这种），折扣/分摊/百分比别让模型口算；`lunar_calendar` 走 `cnlunar`
  做公历↔农历、节气、传统节日（1901–2099），**没装就不注册这个工具**（
  `get_all_tools` 里判断 `lunar_mod.available()`），否则模型会去调一个算不了的。
- **设备电量（D73）**：立创 S3 没有电量采样（板级没重写 `GetBatteryLevel`），
  `/v1` 也只透传拍照工具。问「还有多少电」只能如实说设备没报，**不要猜百分比**；
  真要做得先焊分压 + 改板级代码，再谈小马这侧的透传。
- **不知道就上网查（D74）**：`web_search` / `web_fetch` 所有入口都注册、没有开关。
  时效性事实（放假、政策、新闻、价格、赛事、产品参数）先搜再答，**不许直接回
  「查不到」**——这句写在工具描述里，不要加进系统提示（那是换个地方写路由，D56）。
  改描述前后差别很大：改之前模型答「我查不到」，改之后会搜、会 `web_fetch` 看原文。

### 执行通道：模型跑代码碰不到正式库（D86）

- `run_shell` / `run_python` 跑在**另一个 uid**（容器里 `work` 10002），App 仍是
  `xiaoma` 10001。不是为了「更安全一点」，而是硬约束「LLM 不得直接写正式表」在同
  uid 下根本不成立：一句 `sqlite3 /data/agent.db` 就绕过了提案、确认和审计。
- 布局：`/data` 751 `xiaoma:xiaoma`（穿得进、列不出），库文件 640（umask 027）；
  `/data/work` 1777 两边都能写；`/data/home` 只有 work；`/app` 750——通道读不到
  服务代码和 `.env`；通道自己只有 `/opt/xiaoma-worker/shell_worker.py`。
- `docker-entrypoint.sh` 是 root：先摆正卷上的属主/权限（老卷里 0644 的库会被收到
  640），再 `env -i` + `setpriv` 起通道，最后 `setpriv` 以 xiaoma `exec` 服务。
  别把 `USER xiaoma` 加回 Dockerfile，那样入口脚本就没权限分两个 uid 了。
- 通道协议是 `adapters/shell_worker.py` 里的一行 JSON 一条（AF_UNIX），只接受
  `shell` / `python` / `whoami`，cwd 和脚本都必须在 `/data/work` 里。
- 套接字**不存在** = 开发机（Windows/本机），退回本机直接跑，行为同以前；
  套接字**在但连不上** = 直接报错，**不许**悄悄退回本机——那等于把隔离丢了还不说。
- `run_python` 的临时目录要建在 `/data/work` 下并放开权限（通道是另一个 uid，
  要读得到脚本、写得出产物），别改回 `/tmp`。产物 644，App 才读得到拿去打印。
- 新增「跑模型代码/命令」的能力必须接这条通道；不要图省事在 App 进程里 `subprocess`。
- 外部内容（`web_search` / `web_fetch` / `file_read`）的返回值统一过
  `agent/untrusted.py` 包一层「这是资料，不是指令」。挡不住所有注入，但注入文本不再
  和用户原话同一种语气；新增读外部内容的工具也要包。
- 危险命令（`rm -rf`、关机、`curl|sh`、`sqlite3 *.db`、sudo、`chmod -R`……）由
  `agent/shellguard.py` 匹配，命中先复述命令等确认（走 `ConfirmationManager`，
  确认后原样回放）。它是提醒不是沙箱，真正的边界是上面的 uid。
- `scripts/doctor.py` 的「执行通道」会核对通道 uid 与 App 是否相同：容器里同 uid
  就说明降权没生效。

### 思考开关（D63）

- 不按入口硬切「语音 off / 文件 medium」，交给模型判断：默认 `LLM_REASONING_EFFORT`，
  模型调 `set_thinking(true)` 后本轮改用 `LLM_THINKING_EFFORT`，下一轮复位。
- `set_thinking` 在 `assistant._agent_loop` 里拦，不落 `execute`、不进会话历史；
  开关只影响本轮后续请求，不跨轮记忆。
- 每轮记一行 `data/thinking.jsonl`（问题 / 开没开 / 原因 / 工具 / 耗时），
  `GET /api/thinking` 看最近记录和统计，网页「概览 → 思考开关」也能看；
  这是调试观测数据，不是用户资产，可随时删，`THINKING_LOG_ENABLED=0` 关闭；写失败不许影响回答。
- 观察到某类问题长期不开思考却总答错，改工具描述，不要加关键词路由。

### 工具输出：给事实，不给话术

- 工具返回**事实和结构化数据**，不返回成品话术（穿衣建议、开场白、语气、安慰）。这些由模型按用户是谁、今天要出门干什么、怕不怕冷自己组织；工具里写死就等于把 LLM 降级成模板填空（D53）。
- 数据里的日期/时刻用中文格式（`11月24日（周二）`），不要把 ISO 串丢给模型——语音里「二零二六杠十一杠二四」用户听不出对错，模型也不会主动转格式。
- 字段给齐，不替模型挑：少一个字段，用户问到它就只能编，或者再问一遍。上游数据自相矛盾时也原样给出，让模型决定说哪个、怎么说。
- 例外仍然只有两处：写操作的确认回显、回复校验失败时的兜底（`agent/wording.py`）。那是「说了就得对」，不是替模型说话。
- 判断标准：把这条输出交给两个人，他们的回答应该不一样（一个怕冷、一个不怕），否则就是定死了。

### 让助手像个人（2026-11-27）

用户原话：「我可以把你当作人一样去沟通，而不是给你发送硬指令，是因为你足够聪明，而你的
聪明可能不止是大模型带来的，也有 agent 端的优化。」这是本项目最重要的一条取向：
**同一个模型，编排不同，可以是「语音版命令行」，也可以是家里人。**

落到代码上的四条：

- **该模型开口的地方别替它写话**。工具给事实和边界，不给成品话术（见上一节）；模板只在
  「说了就得对」的地方（写操作回显、校验失败兜底）。发现自己正在拼一句完整回复时，
  先问一句：这句能不能交给模型？
- **时机也是智能**。什么时候说，和说什么一样重要：软提醒顺着聊天带出来，不到点弹（D81）；
  提醒的软/硬也由模型按工具描述判，不写死类型表。
- **不把交互写成硬指令**。没有「只有…才能…」的规则墙，没有关键词路由（D56）；默认值都能
  被用户一句话推翻——实体别名、提醒软硬、称呼都是如此。
- **「活」不等于放任**。编造日期、说错事实、危险动作必须由代码拦（`guard` / `verify` /
  `confirm`）。这些兜底不是限制模型，而是**让模型敢自由说话**：说错了有人接住，
  才敢不照着稿子念。

反过来说：想加一条「必须这样说」的规则时，先分清它是「说得不对」还是「说得不够好」。
前者写进代码，后者交给模型。

原则有优先级：**先「说得对」，再「说得自然」**。两者冲突时选对的——生硬但正确，好过
顺耳但错误；`guard` / `verify` / `confirm` 做的就是这件事。

「自然」的另一半是**认人**。同一句中性的话（「冰箱里还有鸡蛋」算物品还是备忘），不同的人
答案不同，这类歧义靠画像（D58/D59 的 `用户/...` 偏好层）往用户本人的习惯上靠；而确定性的
问题（户号、日期、谁是谁、交没交）没有歧义，一律以事实和工具结果为准，**不许拿画像去猜**。
画像记的是习惯与偏好，不是替用户做事实判断——把这条搞混，就会从「像个人」变成「自作主张」。

### 约束放在代码里，不放在提示词里

- **执行前给模型的规则**（system prompt 里的场景路由表、「日期转 YYYY-MM-DD」、「不超过三句」）能删就删：工具 schema 写清能力，模型自己判断。路由表本质是关键词路由，写进提示词也只是换了个地方的 `if`。
- **需要保留的约束一律做成执行期检查**，不依赖模型自觉：
  - 参数校正/拦截：`agent/guard.py`（工具执行前，拿用户原话校正）
  - 高风险确认：`agent/confirm.py`（执行前拦下等确认）
  - 回复校验：`agent/verify.py`（发出前比对工具参数）
  - 检索收紧：工具内部实现（D46）
- 判断标准：模型违反了会导致「说的是错的」，就必须有代码兜底；如果只是「说得不够好」，交给模型。
- 提示词里只留身份/语气，以及确有必要的一点路由歧义；剩下的靠工具描述和实测。
- 删掉提示词里的规则后要跑 `scripts/eval_accuracy.py` 验证准确率没有下降，掉了就修工具描述，不要把路由表加回来（D56）。

### 存储与并发

- **空间隔离（D68）**：一个空间一套库文件，不往每张表加 `space_id` 列。
  `domain/space.py` 管 `space_id` 规则、路径组合和 `ensure_space`；
  默认空间仍是 `DATA_DIR` 下的现有库（升级不搬家），其它空间在
  `DATA_DIR/spaces/<id>/`。`Assistant(space_id=...)` 按空间建默认 store。
  新增库/目录时必须走 `space_paths(...)`，不要自己拼 `DATA_DIR` 子路径。
- **请求级路由（D71）**：`X-Space-Id` 头选空间，不带就是 default。新空间第一次访问时
  在 `server._assistant_for` 里按需建一套（`MAX_SPACES=16`），名字非法直接 400、
  不静默退回 default。默认空间必须继续用模块级 `assistant`/`print_store`/`PRINT_FILES_DIR`
  （测试和单家庭部署都靠 monkeypatch 这几个名字）；给新空间加 store 时改
  `Space` 的属性，不要在各 handler 里各写一遍。新增数据接口一律
  `space: Space = Depends(current_space)`，别直接读模块级 `assistant`。
  `/v1`（小智）和后台提醒/蒸馏仍只跑默认空间；`space_id` 不参与鉴权。
- `Memory` / `Items` / `Tasks` / `Facts` / `AgentStore` 的连接是 `check_same_thread=False`，所有数据库操作必须持有 `RLock`。
- 服务端用 `asyncio.to_thread` 跑同步编排；不要在 async handler 里直接做重 IO。
- 到期状态同步、筛选、标记必须在同一把锁内完成（见 `Items.claim_due_reminders`），避免并发窗口。
- `memories` 是 `memories_raw` + `memories_fts` 双表；检索依赖“逐字空格化”，换分词方案需要数据迁移。
- FTS 索引的是 summary，原文里独有的词会漏；`Memory._search_rows` 在未命中时用 LIKE 兜底扫 `summary` 和 `text`，通配符已转义。别把兜底删掉，否则「记过但查不到」会重现。
- **热点上下文（D58）写库后必须作废**：`_invalidate_hot_contexts()` 在写工具真正落库后
  和确认流程执行后各清一次。快照冻结是为了保住前缀缓存，但它要活到 24 小时 TTL：
  同一天里改过的户号、删掉的记忆会继续被当成事实念出来，而六位以上数字的出处校验
  只认「提示词里有没有」，拦不住。新增写路径（后台任务、脚本、蒸馏）也要在这里清，
  不要让快照自己过期。
- **多轮上下文只在进程内存**：`client_id` → 最近 20 轮，注入最近 `SESSION_CONTEXT_TURNS`
  条（本机 8，默认 5）、单条截 `SESSION_CONTEXT_MAX_CHARS` 字；重启/重建容器就断，
  换浏览器/换设备是新对话。网页 `localStorage` 里的聊天记录只用于显示，**不发给模型**
  ——别以为它会自动接上。按 `client_id` 分的还有待澄清动作和热点快照，设备要传稳定 ID。
- 对话记录（D84）另存一份到 `dialogue.db`（`domain/dialogue.py`，`space_paths["dialogue"]`）：
  只记 user / assistant 两侧文本，保留 `DIALOGUE_KEEP_DAYS`（默认 7 天），启动时和每天夜里
  各清一次。**只写在 `Assistant.process_text` 一处**——网页、CLI、`/v1`、MCP 都走这里，
  各入口各写一遍必然漏掉小智那条。它**不进 system prompt**（上下文仍用最近几轮 + 热点快照），
  也不转成 `memories`；`DIALOGUE_ENABLED=0` 时连库都不建。网页「设置 → 对话记录」能看到
  存了什么、一键清空（`GET/DELETE /api/dialogue`），清空只删原文，画像和记忆是提炼后的结论。

### 运维与运行观察（D67）

- 容器日志统一走 stdout，靠 compose 的 `logging`（json-file，10MB × 3 份）轮转；
  不要再给应用加文件日志（写失败、占盘、和 Docker 抢着滚）。改 compose 时这段别丢。
- `/api/status` 与 `/api/stats` 的 `runtime` 只数 `DATA_DIR` 顶层库、打印目录和思考日志，
  不要去 walk `work/`、`home/`（pip 包和模型几百 MB，走一遍很慢）。概览页「运行状态」用它。
- 数据增长看三处：`core_mb`（库 + 思考日志）、`print_mb`（打印原件，按 `PRINT_KEEP_DAYS` 清）、
  `disk_free_gb`；备份在宿主机 `backups/`，容器里看不到。

### 提醒与 SSE

- 提醒循环同时扫描 `items`（物品到期）和 `tasks`（账单/日程/自定义）。
- **软提醒**（D81）：`tasks.mode` 分 `timely`（准点，默认）/ `soft`（等话口），软硬由模型在
  `task_add` / `task_update` 里判，不写死类型表；物品到期一律算 soft（它只有日期）。
  白天 soft 不进 `claim_due_reminders`，而是由 `Assistant._soft_reminder_note()` 把今天到期/
  已过期的几条（最多 4 条）注入这一轮上下文，聊到相关话题时模型顺口提一句；
  到 `SOFT_REMINDER_LAST_CALL`（默认 20:30）还没机会说，才按原通道投递一次（晚间兜底）。
  soft 的兜底文案用 `SOFT_REMINDER_PROMPT`（像忽然想起来），失败或没说对名字仍回落模板。
  注入只在这一轮、不进会话历史；物品过期超过 7 天不再提（`SOFT_NOTE_ITEM_OVERDUE_DAYS`）。
- **设备事件**（Home Assistant，`HOME_WATCH_EVENTS`）走 WS 秒级推送 + 15 秒轮询兜底，
  去重靠 `home_alerts` 的 `last_value`。门锁事件用 `describe_lock_event()` 把 MIoT 数字码
  翻成「妈妈用指纹开了门」「门从里面打开了」；`HOME_LOCK_USERS=501=爸爸,2001=爸爸`
  把操作ID换成名字（**ID 是凭据编号，人脸/指纹各一个**），没配就念「501 号用户」。
  **门锁不走 10 分钟事件冷却**（`HOME_LOCK_COOLDOWN_SECONDS=0`），否则第二个人进门不报；
  自动上锁（每次关门都来一条）静默，只记状态不播报。
  门锁的 `exception-occurred` 事件由 `describe_lock_exception()` 翻译（撬锁、频繁失败、
  钥匙未取出…），其中 23/24（门未关/门虚掩）**不在这里报**——交给门状态传感器，避免
  同一件事报两遍。门状态传感器（`options` 里有「门虚掩」的 sensor）在值变成
  「门未关/门虚掩」时提醒，只按状态变化去重、不设时间冷却；它要列进 `HOME_DEVICES` 才读得到。
- **提醒文案由模型组织**（2026-11-27）：`Assistant.reminder_sentence(facts, fallback, must_include)`
  拿事实说一句人话；失败、超长、**没说对名字**就回落 `wording` 的模板。事实由 `_item_reminder_facts`
  / `_task_reminder_facts` 拼（只给名字、到期、逾期天数、周期、现在几点）。模板是兜底不是主路径。
- 投递顺序固定为 **设备 → 网页 → Web Push**，同一提醒只走一条：设备在线时页面不弹、手机不推。
- 设备通道是同一 SSE 端点的 `?role=device`（小智提醒桥用）；设备事件**不带 mp3**，设备用自己的 TTS 念。
- 桥的两条硬约束（D76，真机踩出来的）：①**说话前必须发 `{"type":"tts","state":"start"}`**
  ——固件只在 `kDeviceStateSpeaking` 时才解码收到的 Opus 包，不发 start 就只显示文字、没声音；
  ②**设备全断要立刻退订**——桥挂着 SSE 而设备已经没了，小马会把提醒投进空通道并标成已提醒，
  24 小时内既不重发也不回落 push。桥里 1 秒看门狗负责这件事。
- 只有存在「在线设备、在线 SSE 订阅者或未停用的 push 订阅」时才 `claim_due_reminders`，避免没送达却消耗提醒额度。
- 提醒状态落库（`reminded_at`），冷却由 `REMINDER_COOLDOWN_HOURS` 控制。
- 周期任务（`recur=daily/weekly/monthly`）**到点才提醒**，领取后滚到下一次并清 `reminded_at`（否则 24h 冷却会挡住第二天）；完成是跳过本次，取消才停整个系列；周期必须有具体时间，缺时间就追问，不替用户定。
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

### 设备待机屏（D85）

- 待机时设备显示时间/日期/农历/天气/今天最急的两件事，数据由固件每 5 分钟从 `GET /api/display` 拉（请求头 `X-Display-Token`）。这是**只读**接口，用单独的 `DISPLAY_TOKEN`；别把 `API_TOKEN` 编进固件，设备丢了最多泄露「今天有什么事」。
- 服务端只返回天气 + 农历 + 最急的两条 + 件数，不要把网页看板整包塞给设备（屏幕放不下，也少泄露）。农历在服务端算（`agent/lunar.py`），固件只显示字符串；算不出来就给空，别显示猜的日子。
- 固件是覆盖层：对话/提醒/配网/错误时照旧显示表情与状态；只有待机且没有错误时才盖上看板。没配 URL 时固件行为与上游一致。
- **屏幕只有一个时间**：看板显示时 `SetStatus` 不再往状态栏写（上游空闲时每秒把 `HH:MM` 写进状态栏，会和看板时间一起出现），文字存起来退出看板时补上。显隐切换后要 `lv_obj_invalidate(lv_screen_active())` 整屏重绘一次——真机上只改 HIDDEN 标志会留着上一帧的「待命」两个字。
- 看板配色沿用网页控制台的暖米（正文 `#332a24` / 次要 `#8a7a6c` / 分割线 `#ecdfd0` / 逾期 `#b4472f`），但**底色要压深**：网页那档 `#f5e7d5` 在这块 2 寸 IPS 上（75% 背光）看着就是白的，设备用 `#e7d2ab`。别照抄网页的底色值。**整机一起换**：在 `LcdDisplay::SetTheme` 里覆盖主题色，而不是只给待机那屏上色——资产刷新会再调 `SetTheme`，写在构造函数里会被盖回黑白，用户看到的就是「主题没生效」。中文只有板上那颗 20px 字，别指望多档字号。
- 天气两行：第一行天气+气温+体感（`feels`），第二行风向风级（`wind`）+ 今日高低温；缺字段就少哪段，不补 `--`。缺省值见 `/api/display` 的 `weather`。
- 失败不编：天气 `ok=false` 不显示天气，任务读不到就不显示那一行；OTA 校时前显示 `--:--`。补丁在 `infra/device-firmware/dashboard.patch`，应用顺序在 `keepalive.patch` 之后。

### Web 控制台

- 页面保持单文件原生 JS，无构建步骤。
- **默认页是看板**（`#view-board`，D82）：「今天 / 家里 / 最近」三张卡 + 顶栏天气。
  看板只放「要你动手的」和「现在的状态」，不放图表（概览页负责），也不做固定仪表盘——
  卡片内容跟着当天的事变。聊天是浮层（`#chat-sheet`），已不是 tab；新增入口时别再把它塞回 tab。
- **颜色一律用 CSS 变量**（`--bg` / `--surface` / `--accent` / `--accent-weak`…），
  四套主题挂在 `body[data-theme]` 上（暖米 / 陶土 / 鼠尾草 / 暖深）。写死颜色会在切主题时露馅。
  主题与背景只存本机 `localStorage`（`hx_theme` / `hx_bg` / `hx_auto_night`），不上服务端、不进备份。
- 看板数据走 `GET /api/board`（30 秒缓存）；**动作之后必须带 `refresh=1` 重取**，
  否则会看到操作前的旧数据。天气失败时整块隐藏，不显示 `--°`、不编温度。
- **「家里」卡行数封顶**：门锁、室内、打印机，新设备类别并进已有行，不新开行。
  门锁那行末尾按需补两句：灯全关/全离线时室内行补「灯全关着」；猫眼电量
  （别名固定 `猫眼电量`）**低于 20%** 时在门锁行补「猫眼 11%」——它是另一块电池，
  比门锁本体掉得快，正常时不占字。门状态只在事件时上报，`unknown` 时如实显示「未知」，
  不要编「已上锁」。
- 聊天记录存本机 `localStorage`（`hst_chat_history`，最多 120 条），启动时还原；报错提示和欢迎语不入库，语音播放按钮不持久化。要做多设备同步再考虑落到服务端表。
- 聊天、统计、列表、配置共用同一套 API；不要在前端重复业务逻辑。
- 列表/统计走只读 API；任务完成/取消走 tasks，删除仍走确认。
- 系统配置只允许白名单字段写 `.env`，密钥绝不返回前端；保存后提示重启生效。
- `server.py` 的 `CONFIG_FIELDS` 与 `static/index.html` 的 `CONFIG_FIELDS` 要保持同步。
- 列表接口支持 `q` / `status` / `limit` / `offset`，返回 `total`；前端每页 20 条。
- 删除接口必须带 `confirm=true`；前端还要弹二次确认，不能静默删除。
- 任务编辑走 `PATCH /api/tasks/{id}`，只允许改标题、时间、类型。
- 任务卡延期走 `POST /api/tasks/{id}/postpone`：`days` / `due_at` / `preset`
  （`tomorrow` / `in_3_days` / `next_monday`）。**日期只在服务端算**（`agent/dates.py`），
  前端不拼日期；`Tasks.postpone(due_at=...)` 保留原时刻，别改成零点。
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
- 数据库路径统一由 `DATA_DIR` 决定，新增库用 `config._db_path("<name>.db", "<NAME>_DB_PATH")`，
  不要直接拼 `"./data/..."`：单独覆盖优先，但默认必须跟着 `DATA_DIR` 走。
  `scripts/backup.py` 仍只扫仓库下的 `data/`，`DATA_DIR` 指到别处时靠 `doctor.py` 提醒。
- 不提交 `data/`、`.env`、音频文件；日志里不要输出密钥。
- 依赖按用途分层：`requirements-server.txt`（服务端，必装）/ `requirements-stt.txt`（语音）/
  `requirements-cli.txt`（CLI 音频）；`requirements.txt` 只是三者组合，`requirements-ci.txt`
  引用 server 并加 pytest。新增依赖要放对文件并固定版本，别只加进 `requirements.txt`。
- 监听非本机（`SERVER_HOST` 不是 127.0.0.1/localhost）且 `API_TOKEN` 为空时，`_check_exposure()` 会拒绝启动；不要为了“方便”去掉这道检查。
- MCP token 只能走查询参数（上游小智就是这么连的），`uvicorn.access` 上的 `_TokenRedactingFilter` 负责把 `token=` 打码；不要移除这个过滤器，也不要把完整 URL 打进自己的日志。
- `/healthz` 不鉴权、只回 `{"ok": true}`；不要往里加统计、配置或任何用户数据。

## 代码风格

- 注释 / docstring 用中文，解释“为什么”而非“做了什么”。
- 保持模块单一职责，避免为一次性需求造抽象。
- 错误处理集中在边界（API 入口、外部服务调用），内部逻辑信任调用方。
- 外部服务（LLM / TTS）失败要降级：能回文字就不要整体 500。

## 常见坑

- 缺少 `.env` 时 `LLM()` 会在启动阶段抛 `RuntimeError`；import 模块本身不受影响，pytest 收集也不会失败。
- LLM 请求莫名变慢（65 s 级）先看 `LLM_PROXY`：设了 `NO_PROXY` 环境变量的机器上，requests 会跳过 Windows 注册表里的系统代理，变成裸连远端；本机走代理可能只要几秒。现在的策略是**先直连、连不上才走 `LLM_PROXY`**（并记住 5 分钟），配了代理也不会拖慢正常直连。
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
