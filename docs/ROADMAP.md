# 家庭生活 Agent 开发计划

> 状态：规划稿（2026-11-24 校对）
> 目标：从「家庭记忆 + 物品提醒」演进为「能理解、会追问、可执行」的家庭生活 Agent。
>
> 当前优先级（2026-11-24）：语音设备真机（小智 + 立创 S3）→ 真机验收（提醒 / 拍照 / 唤醒词）→ 家庭传感器事件；Web 控制台与 CLI 已落地并继续维护；Android App 与 RAG 延后。

## 1. 目标与非目标

### 目标

- 记住家庭事实、物品、账单、日程、偏好，并在需要时主动提醒。
- 对模糊信息主动追问，确保记录和执行没有歧义。
- 接入本地生活能力：地图、公交、天气、缴费政策、社区公告。
- 支持 RAG 知识库：政策、说明书、个人资料、本地资讯。
- 同一套能力接入 Web、小程序、App、ESP32 等终端。
- 数据本地优先，可扩展到多用户/多家庭。

### 近期非目标

- 不做通用编程/办公 Agent。
- 不做多 Agent 协作和自动自我训练。
- 不做全屋智能的完整设备控制：已接 Home Assistant，但只做白名单内的查询与低风险控制（§21），不做通用设备平台。
- 不做复杂视觉能力（视频、连续看、人脸识别）；单张拍照问答按 §26 做，算「图片输入扩展点」的落地。

## 2. 现状与差距

### 已有（2026-11-24 校对）

- Python + FastAPI + SQLite，本地运行；CLI 与 Web 共用 `Assistant`。
- 中文语音：FunASR 识别、Edge TTS 合成、CLI 唤醒词「小马」。
- Agent loop + function calling：**25 个工具**（记忆 4、物品 4、实体 3、任务 6、事实 3、
  `family_search`、`home_query` / `home_control`、`camera_look`、日期 1）。
- 实体 / 别名 / 事实 / 任务模型；写操作走「提案 → 澄清 → 确认 → 提交」，`proposals` 带幂等键与审计。
- 结构化提醒：物品到期、账单/日程/自定义任务、周期提醒（每天/每周/每月），冷却与 SSE。
- Web Push（含 `pushsubscriptionchange` 自愈）、设备通道（小智提醒桥），投递顺序设备 → 网页 → Push。
- 提醒可操作：点通知或任务卡会带 `task_id`，下一句「完成了 / 延期」落到那条任务上。
- 小智接入：MCP 端点 `ask_xiaoma`、声纹 `_meta` 分会话、单张拍照问答（模拟设备整链验证）。
- 小米生态：Home Assistant + 官方集成，`home_query` / `home_control`（白名单 + 回读确认）、
  `home_watch` 健康巡检与 WebSocket 事件。
- 评测与回归：457 条 pytest；`scripts/eval_accuracy.py` 41 条真模型用例（当前 41/41）。

### 主要差距

- 没有 RAG，也没有地图/公交/天气等实时工具（阶段 3，延后）。
- 多家庭/多终端未落地：`space_id` 目前只在 `push_subscriptions`，业务表还没有。
- 任务上下文只接了网页：点通知/任务卡会带 `task_id`；设备端（MCP `ask_xiaoma`）还没有「刚提醒过哪条」。
- 设备真机验收未做：唤醒词（D44）、拍照（D49）、反向提醒的真机听感都还等硬件。
- HA 事件只验证了灯，水浸/烟感/门磁还没真正触发过（缺传感器）。
- 事实的 `subject` 还是文本，没和 `entities` 关联（§12 第 4 条）。
- 没有 tracing、成本统计；语义记忆仍是「原文 + 摘要」，没有向量检索。

## 3. 目标架构

```
App / 小程序 / Web / CLI / ESP32
              ↓
          接入网关 Gateway
   （鉴权、会话、流式、推送、渠道适配）
              ↓
          Agent 编排层
   理解 → 实体解析 → 澄清/确认 → 执行 → 记忆
              ↓
 ┌────────────┼────────────┐
 工具层       知识层        记忆层
 地图/天气     RAG 文档      结构化事实
 日程/账单     本地资讯      语义记忆
 家庭设备      政策公告      对话摘要
 └────────────┴────────────┘
              ↓
        数据层
 SQLite（MVP）→ PostgreSQL + 向量索引
```

原则：

- LLM 负责理解和规划，不直接决定事实。
- 数据库负责实体、别名、事实和状态。
- 策略层决定「该不该问」「能不能直接做」。
- 领域层不依赖 LLM、渠道和具体 Agent 框架。
- 渠道适配层只做输入输出转换，不承载业务逻辑。

## 4. 核心机制

### 4.1 两阶段提交

所有写操作统一走：

```
用户输入
  → 抽取结构化提案（LLM）
  → 实体解析（数据库/别名/候选）
  → 澄清策略（缺关键槽位就问）
  → 提交（幂等 + 审计）
```

不允许多数写工具直接落正式表。写工具返回「提案」，由编排层确认后再提交。

`pending_actions` 保存未完成的提案：

- `status`: pending / committed / cancelled / expired
- `payload`: 已抽取的槽位
- `missing_slots`: 还缺什么
- `candidates`: 候选实体
- `expires_at`: 超时后失效，避免无限挂起

### 4.2 实体与槽位

统一实体类型：

- `person`：家庭成员、父母、朋友
- `property`：自己家、父母家、出租房
- `product`：牛奶、酸奶、药品
- `provider`：供电局、水务、燃气、物业
- `place`：烟台大学、超市、医院
- `device`：ESP32、音箱、传感器

每个实体包含：

- `canonical_name`：规范名
- `aliases`：别名，如「咱家」「我爸妈家」
- `attributes`：品牌、地址、户号、位置等
- `owner_id` / `space_id`：归属家庭空间

动作的关键槽位示例：

| 动作 | 关键槽位 | 可选槽位 |
|---|---|---|
| 交电费 | 房子、到期日 | 户号、金额、缴费方式 |
| 记录物品 | 物品规范名、到期日 | 品牌、数量、位置 |
| 创建提醒 | 提醒对象、时间 | 重复规则、渠道 |
| 查询路线 | 起点、终点、交通方式 | 出发时间 |

### 4.3 澄清策略

规则：

- 只对「关键槽位」追问，不要什么都问。
- 一轮最多问一个问题，优先给候选选项。
- 候选唯一且置信度高 → 直接确认/执行。
- 候选多个 → 问「是 A 还是 B？」
- 没有候选 → 问开放式问题，但给出示例。
- 用户说「随便/都行」→ 采用默认值，并明确说明假设。
- 删除、付款、通知他人等高风险动作必须确认。
- 用户切换话题时保留 pending action 一段时间，允许「继续刚才那个」。

示例：

用户：明天要交电费
系统：是咱们家还是爸妈家的电费？
用户：爸妈家
系统：好，记下了：10 月 27 日交爸妈家电费。

用户：牛奶要过期了
系统：是蒙牛那盒还是伊利那盒？
用户：伊利
系统：好，伊利牛奶记下了，到期日是哪天？

需要记录「澄清决策日志」，用于后续评测和调参。

### 4.4 工具契约

每个工具至少包含：

- `name` / `description` / `parameters`
- `kind`: read / write / external
- `requires_confirmation`: 是否需要确认
- `idempotency_key`: 写操作幂等键
- `timeout` / `cache_ttl`
- `provenance`: 数据来源
- 结构化返回：`{ok, data, source, error}`

工具失败要降级，不要把异常直接抛给用户。外部工具（地图、天气）必须有超时和缓存。

> 当前实现是简化版（2026-11-24）：`execute` 统一返回**字符串**（直接进 LLM 上下文），
> 结构化结果放在字符串里，没有 `{ok, data, source, error}` 这种统一包；`kind` /
> `requires_confirmation` 也没有逐工具字段，而是由 `agent/guard.py`、`agent/confirm.py`
> 按工具名集中判断。新增外部工具（地图/天气/RAG）时再按上面的契约补齐超时与来源。

### 4.5 记忆分层

| 层 | 内容 | 存储 |
|---|---|---|
| 结构化事实 | 房子、成员、物品、账单、偏好、别名 | entities / facts / tasks |
| 语义记忆 | 日记、笔记、文档、聊天摘要 | 向量索引 + 元数据 |
| 情节记忆 | 最近对话、做过什么、用户纠正过什么 | conversations / summaries |
| 知识库 | 公告、政策、说明书、本地资讯 | documents / chunks |

短期继续 SQLite：

- FTS5 做关键词和中文子串检索。
- `sqlite-vec` 做向量检索。
- 混合召回后再重排。

多用户/多设备后再考虑 PostgreSQL + `pgvector` 或 Qdrant。

### 4.6 RAG

适用：政策、公告、说明书、学校规定、个人资料、本地资讯。

不适用：实时路况、公交到站、天气、当前余额。这些走实时工具。

管线：

```
来源接入 → 清洗去重 → 分块 → 元数据 → embedding → 混合索引
```

检索：

```
查询改写 → 关键词 + 向量混合召回 → 重排 → 带出处/日期回答
```

要求：

- 每个文档带 `source`、`published_at`、`valid_until`、`region`。
- 回答必须带出处和日期；过期内容不能当事实。
- 查不到就说不知道，或转 web search / 实时工具。
- 中文 embedding 优先评估 BGE-M3、bge-large-zh、Qwen3-Embedding。

语料来源（2026-11-26，D69）：第一批语料是**家庭文档**——发票、保修单、说明书、体检报告、合同。
OCR 与归档交给家庭服务器上独立跑的 **Paperless-ngx**，小马这边只做检索接入；
文档内容**不进 `memories`**（长文本会污染 FTS、把库撑大），也不把整份 PDF 塞进上下文。
具体接入路线见 §10 的 D69。

### 4.7 本地生活工具

- 地图/驾车/公交：高德或百度地图 API，带缓存和限流。
- 天气：高德/和风天气。
- 账单/日程：自己的 tasks + 调度器。
- 本地资讯/政策：RAG + 定时抓取，优先官方来源。
- 家庭设备：MQTT / Home Assistant / Matter，后续接 ESP32。

### 4.8 多终端

统一 API：

```
POST /v1/chat          文字/音频
WS   /v1/stream        流式回复
POST /v1/actions/{id}  确认/取消待澄清动作
GET  /v1/events        SSE/WS 推送
MQTT home/{space}/...  ESP32 控制与提醒
```

终端策略：

- **Web**：保留现有单页，逐步迁移到 `/v1`。
- **小程序**：微信原生，复用 API，推送走订阅消息。
- **App**：Flutter 或 React Native，复用 API，推送走 APNs/FCM。
- **ESP32**：薄客户端。设备端做唤醒词/VAD/录音/播放，ASR/LLM/TTS 在服务端；MQTT 控制；支持 OTA 和离线固定指令。

身份模型：

- `users` / `spaces` / `memberships` / `devices`
- 所有业务数据带 `space_id`
- 权限按空间隔离，父母的账单、孩子的设备不混在一起。

> 当前状态（2026-11-24）：只有 `push_subscriptions` 带 `space_id`（恒为 `default`）；
> `entities` / `facts` / `tasks` / `memories` 都还没有。身份也只有「小智声纹分会话」
> （`client_id = "mcp:<speakerId>"`）这一层，没有 `users` / `memberships`。
> 上面是目标模型，不是现状。

## 5. 数据模型（草案）

> 下面是**目标模型**；当前库是逐步演进出来的，和草案有出入（2026-11-24）：
> `entities` 只有 `id/type/canonical_name/aliases/created_at`（无 `space_id`、无 `attributes`）；
> `facts` 已按草案实现（含 `valid_from/valid_to`）；`tasks` 多了 `recur` 与 `reminded_at` 语义；
> `proposals` / `pending_actions` 没有 `space_id`。改表以 `domain/` 里的建表语句为准。

```sql
entities(
  id TEXT PRIMARY KEY,
  space_id TEXT NOT NULL,
  type TEXT NOT NULL,
  canonical_name TEXT NOT NULL,
  aliases TEXT,
  attributes TEXT,
  created_at TEXT
);

facts(
  id INTEGER PRIMARY KEY,
  subject TEXT NOT NULL,
  predicate TEXT NOT NULL,
  value TEXT NOT NULL,
  source TEXT,
  valid_from TEXT,
  valid_to TEXT,
  created_at TEXT
);

tasks(
  id INTEGER PRIMARY KEY,
  type TEXT NOT NULL,
  title TEXT NOT NULL,
  due_at TEXT,
  status TEXT,
  entity_id INTEGER,
  payload TEXT,
  created_at TEXT,
  reminded_at TEXT,
  completed_at TEXT
);

pending_actions(
  id TEXT PRIMARY KEY,
  space_id TEXT NOT NULL,
  conversation_id TEXT,
  missing_slot TEXT,
  question TEXT,
  candidates TEXT,
  action_type TEXT,
  payload TEXT,
  status TEXT,
  expires_at TEXT
);

proposals(
  id TEXT PRIMARY KEY,
  action_type TEXT,
  payload TEXT,
  status TEXT,
  result TEXT,
  idempotency_key TEXT,
  created_at TEXT
);

documents(
  id TEXT PRIMARY KEY,
  source TEXT,
  title TEXT,
  url TEXT,
  published_at TEXT,
  valid_until TEXT,
  content_hash TEXT,
  metadata TEXT
);

chunks(
  id TEXT PRIMARY KEY,
  document_id TEXT,
  text TEXT,
  embedding BLOB,
  metadata TEXT
);
```

迁移策略：

- 先新增表，不破坏现有 `memories_raw` / `items`。
- 写双读兼容层：新 Agent 读新表，旧 CLI/Web 仍可读旧表。
- 迁移脚本可重复执行，带校验和回滚说明。

## 6. API 契约（草案）

> 实际接口与草案不同：对外是 OpenAI 兼容的 `POST /v1/chat/completions`（小智用）、
> 内部是 `POST /api/chat`、`POST /api/voice`、`GET /api/events` 等；没有 `space_id`、
> 没有 `/v1/actions/{id}`，待澄清动作由 `assistant` 按 `client_id` 自动接续。
> 草案里的 `pending_action` 结构只在 `/api/chat` 的返回里出现（`type=clarify`）。

```json
POST /v1/chat
{
  "space_id": "home-1",
  "device_id": "web-xxx",
  "conversation_id": "conv-1",
  "text": "明天要交电费"
}
```

返回：

```json
{
  "reply": "是咱们家还是爸妈家的电费？",
  "audio": "",
  "pending_action": {
    "id": "act-1",
    "missing_slots": ["property"],
    "candidates": [
      {"id": "prop-self", "name": "自己家"},
      {"id": "prop-parents", "name": "爸妈家"}
    ]
  },
  "used_tools": [],
  "citations": []
}
```

确认：

```json
POST /v1/actions/act-1/confirm
{"value": {"property": "prop-parents"}}
```

## 7. 分阶段计划

当前进展（2026-11-24 校对）：阶段 0–2 的主体与 Web 控制台已落地，并已越过分阶段计划走进设备接入：
实体/事实/任务/提案/澄清/确认、旧数据迁移、Web Push、小智 MCP 接入点、单张拍照问答、
周期提醒、Home Assistant 接入（只读 + 白名单控制 + 事件订阅）。测试 **457** 条；
`scripts/eval_accuracy.py` **41** 条真模型用例（41/41）。下一阶段主线是**设备真机验收**，
不是 Android App（见 §17–§19 的调整）。

分层现状：`domain/` 与 `agent/` 已拆干净；`adapters/` 只落了 `home.py`、`home_ws.py`、
`mcp_endpoint.py`，CLI（`main.py`）、Web/API（`server.py`）仍是入口文件，没有迁进 `adapters/`。

### 阶段 0：基础重构

状态（2026-11-24）：`domain/` 与 `agent/` 已完成；`adapters/` 只落了设备与 MCP 两类，
CLI/Web/API 仍是 `main.py` / `server.py`，**没有全部迁进 `adapters/`**。

目标：不改变现有行为，把代码分层，为后续扩展留接口。

任务：

- 拆出 `domain/`：实体、事实、任务、提醒。
- 拆出 `agent/`：抽取、解析、澄清、执行、编排。
- 拆出 `adapters/`：CLI、Web、API。
- 保留现有 `Assistant` 作为兼容入口。
- 补充现有行为的回归测试。

验收：

- 现有测试全部通过。
- CLI/Web 行为不变。
- 领域层不 import LLM/FastAPI/音频模块。

### 阶段 1：澄清与两阶段提交 MVP

状态：基础版已落地（通用交费规则 + 物品候选 + 实体管理 + CLI + 提案/幂等写入 + 删除确认）。

目标：先解决「电费」「牛奶」两类歧义。

任务：

- 新增 `entities`、`pending_actions` 表。
- 实现抽取 schema、实体解析、澄清策略。
- 写操作改为「提案 → 澄清 → 提交」。
- CLI/Web 支持 pending action 的展示和回答。
- 建立歧义测试集。

验收：

- 「明天要交电费」在多个房子时只问一个问题。
- 回答后只落一条记录，不重复。
- 「牛奶要过期」能区分蒙牛/伊利；只有一盒时直接确认。
- 删除等高风险操作需要确认。

### 阶段 2：通用化记忆与任务

状态：基础版已落地（tasks / facts + 提醒扩展 + 旧数据迁移脚本）。

目标：新增生活领域不需要改核心循环。

任务：

- 实体别名、属性、家庭成员、供应商、产品。
- `facts` / `tasks` 通用化。
- 提醒从「物品到期」扩展到账单、日程、自定义任务。
- 旧 `items` / `memories` 迁移：`scripts/migrate_legacy.py`（items → 产品实体；任务类记忆 → tasks）；不删除旧数据、可重复执行。

验收：

- 新增一个领域（如「物业费」）主要靠配置/数据，不改 Agent 核心。
- 旧数据可迁移，迁移脚本可重复执行。

### 阶段 2.5：Web 控制台（已落地）

状态：基础版 + 搜索/筛选/分页/任务编辑/删除确认已落地。

目标：网页端不只是聊天，还能完成日常查看、管理和配置。

任务：

- 概览：记忆 / 物品 / 任务 / 事实 / 实体数量，到期提醒，任务状态分布。
- 列表：记忆、物品、任务、事实、实体；支持筛选、任务完成/取消。
- 系统配置：展示脱敏配置；可编辑白名单字段，写本地 `.env`，保存后提示重启。
- 聊天保留文字 + 语音，并加入统计/列表快捷入口。
- 移动端适配，所有操作可在手机浏览器完成。

验收：

- 不通过聊天也能查看数据、完成任务。
- 配置页不出现任何密钥；修改后重启生效。
- Web 与 CLI / 服务端共用同一套 API。

### 阶段 3：RAG 与本地生活（延后）

状态（2026-11-26）：**语料来源已定（D69）**——家庭文档先在家庭服务器的 Paperless-ngx 里 OCR 归档，
小马只做检索接入（先直接调 API，文档多了再上本地向量索引）。实时工具（地图/天气）仍未做。

目标：能回答「政策/公告」类问题，也能查实时路线。

任务：

- 文档接入、分块、embedding、混合检索、重排。
- 引用、时效、地区元数据。
- 地图/公交/天气工具，带缓存和限流。
- 区分「知识问答」和「实时查询」。

验收：

- 「到烟台大学开车多久」调用地图工具，不编造。
- 「什么时候交暖气费」命中知识库时带出处和日期。
- 知识库没有时明确说不知道，或转 web search。

### 阶段 4：多渠道网关

状态（2026-11-24）：**部分前置已落地**——OpenAI 兼容 `POST /v1/chat/completions`（小智用）、
`/mcp_endpoint`、设备提醒通道；`users/spaces/memberships` 与业务表 `space_id` 仍未做。

目标：同一套能力接入小程序/App/Web。

任务：

- `/v1` API、WebSocket 流式、统一会话。
- `users/spaces/devices` 身份模型。
- 推送抽象：SSE/WS、APNs/FCM、订阅消息。
- 先做小程序或 Web，再做 App。

验收：

- 同一 `space_id` 下多终端共享记忆和待办。
- 推送能到达对应渠道。
- 渠道适配层不承载业务逻辑。

### 阶段 5：ESP32 与语音终端

状态（2026-11-24）：**进行中**——小智服务端接入、MCP 工具、反向提醒、拍照问答已完成并用
模拟设备验证；立创 S3 已下单，真机刷机与验收待硬件到位（`docs/device-lichuang-s3.md`）。

目标：低成本硬件也能唤醒、对话、被提醒。

任务：

- 设备端唤醒词/VAD，服务端 ASR/LLM/TTS。
- MQTT 控制、音频流、OTA。
- 离线固定指令和断网降级。

验收：

- 说「小马」唤醒，问一句答一句。
- 断网时至少能执行本地固定指令。
- 端到端延迟可接受。

### 阶段 6：Agent 化增强与评测

状态（2026-11-24）：**评测已落地**（41 条真模型用例 + 457 条 pytest）；多步任务、主动建议、
tracing、成本统计仍未做，也还没评估第三方框架。

目标：多步任务、主动建议、可持续迭代。

任务：

- 任务规划、多步执行、失败重试。
- 主动提醒和建议（不只是到期）。
- tracing、评测集、成本统计。
- 评估是否引入 LangGraph/PydanticAI/Hermes。

验收：

- 有稳定的歧义/工具/检索评测指标。
- 关键路径可追踪、可回放。
- 不达标的改动不合并。

## 8. 测试与评测

已落地：

- **歧义/澄清**：`tests/test_clarify.py` + 评测集里的两轮澄清用例（电费、牛奶）。
- **工具与护栏**：`tests/test_guard.py`、`test_confirm.py`、`test_propose.py`、`test_verify.py`。
- **回归**：457 条 pytest；正确率基线 `scripts/eval_accuracy.py` 41 条真模型用例（41/41，
  平均 4.7 s/条），直接跑 `pytest tests -q` 即可，评测另行手动跑。
- **端到端**：`scripts/xiaozhi_probe.py`（小智协议）、`scripts/fake_camera_device.py`（拍照整链）。

还缺：

- **RAG 评测**：召回率、引用正确率、时效判断（阶段 3）。
- **指标**：澄清率、误提交率、槽位准确率、单次成本都还没有统计口径；评测只卡「类型/工具/库状态/关键事实」。
- **tracing / 回放**：没有请求级追踪，只能看日志和 `proposals` 审计。
- **端到端**：小程序/App/ESP32 真机主链路各一条（现在只有模拟设备）。

## 9. 安全、隐私与权限

- 数据默认本地，语音临时文件处理完即删除。
- 多家庭/多成员按 `space_id` 隔离。
- 设备使用独立 token，可随时吊销。
- 删除、付款、通知他人等动作需要确认。
- 工具调用记录审计日志，不记录密钥。
- RAG 来源保留出处，避免把过期/错误信息当事实。
- 云端模型/语音服务可配置开关，明确哪些数据会离开本地。

## 10. 待决策

### 已确认（2026-11-05）

| 编号 | 决策 | 结果 |
|---|---|---|
| D1 | 产品边界 | 家庭垂直优先，保留通用扩展 |
| D2 | 首个终端 | **已被 §17–§19 调整（2026-11-21）**：主路径改为设备端（小智/ESP32），网页收敛为「文字会话 + 统计列表 + 通知落点 + 配置」；CLI 保留 |
| D3 | RAG 来源 | 采用默认：官方公告/物业/学校；**第一批实际语料已定为家庭文档（2026-11-26，见 D69）**，政策/公告类清单仍在阶段 3 前补 |
| D4 | 地图/天气 | 采用默认：高德；阶段 3 前补 API Key |
| D5 | 多家庭 | 先单家庭，数据模型带 `space_id` |
| D6 | 部署 | 本地优先 |

### 可延后

| 编号 | 决策 | 建议 |
|---|---|---|
| D7 | Agent 框架 | 仍自研：评测集已建立（`scripts/eval_accuracy.py`），换框架的收益仍不明显；阶段 3 接 RAG/实时工具后再评估 |
| D8 | 数据库升级 | 继续 SQLite + sqlite-vec，多用户再上 PostgreSQL |
| D9 | 语音链路 | 本地 FunASR/Edge TTS 为主；小智侧自建服务已接管设备语音（§18/§24） |
| D10 | 提醒渠道 | **已扩展**：设备通道 → 网页 SSE → Web Push（2026-11-21 落地）；微信/短信/App 推送仍未做 |
| D11 | 是否引入 Hermes/OpenClaw | 优先作为多渠道前端，通过 MCP 接领域服务 |
| D12 | `items` 是否退休、改由 `tasks` 接管物品到期 | 暂不退休：两表并行会重复提醒；等 `tasks` 能完整表达物品语义（数量/位置/品牌）后再迁移 |

## 11. 风险

- **过度追问**：用户会烦；只问关键槽位，给候选，允许默认。
- **RAG 时效/合规**：政策类内容会过期，抓取也可能有版权/稳定性问题。
- **工具幻觉**：模型可能编造路线/政策；必须带出处，工具失败要明说。
- **多终端权限**：父母家、孩子设备的数据不能混。
- **成本与延迟**：多轮澄清 + RAG + TTS 会放大延迟和费用，需要预算和缓存。
- **数据迁移**：旧 `memories` / `items` 要平滑迁移，避免丢数据。

## 12. 下一步（2026-11-24 重排）

阶段 0–2、Web 控制台、Web Push、小智 MCP 接入、拍照问答、周期提醒、HA 接入都已落地。
按「是否卡住真机」排序的待办：

1. **设备真机**（主线）：立创 S3 到货 → 刷开源固件指局域网 → 跑 `docs/device-lichuang-s3.md`
   验收清单；先验「另一台设备能不能连小智端口」，再验唤醒词（D44）、反向提醒、拍照（D49）。
2. **HA 真实事件**：现在只用灯验过 WebSocket；水浸/烟感/门磁要等传感器到位（D33）。
3. ~~**任务上下文接进会话**~~ **网页侧已完成（2026-11-24）**：点通知/任务卡后
   `/api/chat`、`/api/voice` 带 `task_id`。设备端仍缺「刚提醒过哪条」的会话记忆，
   等反向提醒真机跑通再定要不要在 MCP 侧补。
4. ~~**事实与实体关联**~~ **已完成（2026-11-26，D66）**：`facts.entity_id` 补列，存事实时按主体匹配实体，查事实时把别名对应的实体事实并进来（存「净水器」、问「饮水机」也查得到）。
5. 运维底线：日志轮转、systemd 自启、Linux 上跑一遍 `pytest tests -q`（搬家庭服务器前，§22）。
6. ~~Android App 与 RAG 继续延后~~ **RAG 语料来源已定（2026-11-26，D69）**：家庭文档走
   家庭服务器上的 Paperless-ngx，小马只做检索接入；等 Paperless 跑顺、文档有几百份了
   再接 `doc_search`（先 A 方案：直接调 API)。App 仍延后。

## 13. 网络接入方案（2026-11-20 讨论记录）

### 约束

- 手机浏览器的麦克风只在 HTTPS 或 localhost 下可用，这是必须解决的硬条件。
- 数据本地优先：记忆、账单、事实尽量留在家里。
- 服务是单进程 + 单 token + SQLite，没有速率限制和审计，不能裸奔公网。

### 方案对比

| | Tailscale Serve | CF Tunnel + Access | VPS + frp 回源 | 直接部署 VPS |
|---|---|---|---|---|
| 数据位置 | 家里 | 家里 | 家里 | VPS |
| 公网端口 | 不开 | 不开 | VPS 443 | VPS 443 |
| 自动 HTTPS | ✅ | ✅ | 需 Caddy | 需 Caddy |
| 需要域名 | 否 | 是 | 是 | 是 |
| 访问者装客户端 | 要装 Tailscale | 不用 | 不用 | 不用 |
| 身份层 | 设备身份 | CF Access（邮箱/SSO） | 仅 API_TOKEN | 仅 API_TOKEN |
| 运维成本 | 极低 | 低 | 中（frps + 证书） | 中（更新/备份） |
| 大陆注意 | 连接质量看运营商 | 免费线路延迟偏高 | 域名需备案 | 域名需备案、内存 ≥2GB |

### 结论

1. 先上 **Tailscale Serve**：自动 HTTPS、零端口暴露、无需域名，今天即可让手机语音可用。
2. 只给家里人用且连接稳定 → 停在这里。
3. 要给不装 App 的家人用 → 二选一：**Cloudflare Tunnel + Access**（省心）或 **VPS + frp**（数据留家、全自主）。
4. **暂不把服务直接部署到 VPS**：数据要出门，且 FunASR 模型占 ~890MB 内存。
5. 换方案的成本很低：应用无硬编码地址、无 CORS、前端全相对路径；只需改绑定地址、入口代理和域名，客户端 token 因 localStorage 按源隔离需重填一次。

### 已做的前置准备

- `.env` 配置 `API_TOKEN`（所有 `/api/*` 需 Bearer）。
- `/docs`、`/redoc`、`/openapi.json` 已关闭，避免公网暴露接口结构。
- `.env` 设置 `SERVER_PORT=8200`（本机 8000 被 Windows 保留）。
- 已安装 Tailscale 1.104.7；**Serve 已上线并验证**（2026-11-23）：`https://win-od68q9v0uct.tail8025f9.ts.net`，首页 / `/api/status` / `/sw.js` 均正常，手机已能「添加到主屏」（详见 `docs/pre-hardware.md` §5）。

## 14. PWA 与 iOS 已知问题

### 已接入

- `manifest.json` + 主屏图标 + `sw.js`：可安装到主屏幕，断网时壳仍可打开，`/api/*` 一律走网络。
- 页面导航用「网络优先」策略，改完前端杀进程重开即生效，不需要升缓存版本号。

### iOS 26.1 竖屏 PWA 画不到全屏（系统回归，暂不处理）

现象：iPhone 12 / iOS 26.1，主屏打开后 `innerHeight`、`visualViewport.height`、`100dvh` 全部是 **797**，而 `screen.height` 是 **844**，底部留一条 47pt 的黑色带（47 = 状态栏高度）。

排查结论：

- 不是布局问题：`body`、`#app`、`100dvh` 三者一致为 797，页面内部没有剩余空间可填。
- 不是安装时缓存了旧 meta：删掉图标重装后仍然是 797。
- 不是键盘导致的常见 WebKit 视口缩水：`display: none → flex` 翻转自愈（iOS 17/18 有效）实测 797 → 797，无效。
- 网上同类反馈指向 iOS 26.1 的回归：竖屏 standalone 无法全屏绘制，横屏仍正常。

当前处理：接受该黑带，不再投入时间。诊断面板（连点三下标题）保留 `修复视口` 按钮，等 iOS 修复后可直接验证。若后续要处理，优先级最低的备选是改用「不透明状态栏 + 页面下移」，用顶部黑带换掉底部黑带，观感未必更好。

## 15. 计划：Web Push 到期提醒（下一阶段）

### 目标

App 不在前台、甚至完全关闭时，也能收到「牛奶还有 2 天过期」「明天要交电费」。当前提醒只走 SSE，必须开着页面才收得到——这是移动端体验最大的缺口。

### 可行性前置（已满足）

- iOS 16.4+ 允许「添加到主屏幕」的 PWA 使用 Web Push（必须是从主屏图标打开，Safari 标签页里不行）。
- 已有 HTTPS 入口（Tailscale Serve `https://win-od68q9v0uct.tail8025f9.ts.net`），Web Push 强制要求安全上下文。
- 已有 `sw.js` 的 `push` / `notificationclick` 入口，等后端接上即可。
- 已登录 `win-od68q9v0uct`（Windows），本机可生成 VAPID 密钥。

### 总体设计

```
到期扫描（既有 reminder_loop）
      ├─ SSE 在线 → 现有广播（不变）
      └─ push 订阅 → pywebpush 逐条推送到 APNs / FCM
                        └─ 404/410 → 删除订阅；其它失败 → fail_count++
```

原则：**同一轮提醒只走一条路也够，不做双通道刷屏**。SSE 送达时不再推 push；反之亦然。

### 新增配置（`.env` / `config.py` / `.env.example` / README 四处同步）

| 键 | 说明 |
|---|---|
| `VAPID_PUBLIC_KEY` | 应用服务器公钥（base64url），可下发前端 |
| `VAPID_PRIVATE_KEY` | 私钥，**绝不返回前端**，不写进 `CONFIG_FIELDS` |
| `VAPID_SUBJECT` | `mailto:xxx@example.com`，部分推送服务会校验 |
| `PUSH_ENABLED` | 总开关，默认 `false`；密钥缺失时自动降级为纯 SSE |

密钥生成：`python -m py_vapid --applicationServerKey`（或一次性脚本），生成后写进 `.env`。

### 依赖

`pywebpush`（含 `py-vapid`、`http-ecom` 生态），版本固定写进 `requirements.txt`。

### 存储：`push_subscriptions`

放 `domain/push.py`（新建），沿用现有 `RLock` + `check_same_thread=False` 约定：

| 字段 | 说明 |
|---|---|
| `id` | 自增主键 |
| `endpoint` | 唯一索引，天然幂等（重复订阅只更新时间） |
| `p256dh` / `auth` | 推送加密密钥 |
| `user_agent` | 便于区分设备（iPhone / Android） |
| `space_id` | 预留多家庭隔离，当前恒为 `default` |
| `created_at` / `last_ok_at` / `fail_count` | 运维与清理依据 |

### 接口（全部要求 Bearer Token）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/push/key` | 返回 `{enabled, publicKey}`；未配置时 `enabled=false` |
| POST | `/api/push/subscribe` | 入参 `{endpoint, keys:{p256dh, auth}}`，按 `endpoint` upsert |
| POST | `/api/push/unsubscribe` | 按 `endpoint` 删除，前端关开关时调用 |
| POST | `/api/push/test` | 发一条测试通知，用于排障 |

### 前端（设置页）

- 新增「提醒通知」开关：
  - `Notification.requestPermission()` → 必须用户点击触发，不能自动弹
  - `registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey })`
  - 订阅成功后 `POST /api/push/subscribe`
  - 关闭开关 → `subscription.unsubscribe()` + `POST /api/push/unsubscribe`
- 状态提示区分：未安装到主屏 / iOS 版本过低 / 浏览器不支持 / 通知权限被拒 / 密钥未配置
- 订阅可能被系统静默轮换（`pushsubscriptionchange`）：`sw.js` 里监听该事件并回传新订阅，避免失效后用户无感知

### 提醒循环改造

- `claim_due_reminders` 的触发条件由 `if _subscribers` 改为 `if _subscribers or push.has_subscriptions()`——否则没有在线页面时永远不领取，push 也就发不出去。
- 已领取的提醒：SSE 广播 + push 同时发（用户可能开着页面又在后台）。
- push 正文只发文字（`title` = 「小马提醒」，`body` = 提醒文案），不做 TTS；点通知打开 `/`。
- 发送用 `asyncio.to_thread` + `gather`，单条超时（设计 10s，实际用 15s）；不能阻塞提醒循环。
- 失败处理：
  - `404 / 410` → 立即删除该订阅（设备已卸载或权限被撤）
  - 其它错误 → `fail_count += 1`，连续 5 次标记停用，但保留记录
  - 任一步异常都只记日志，不能打断 SSE

### 测试计划（`tests/test_push.py`）

- `push_subscriptions` upsert：同一 endpoint 重复订阅不产生新行
- `unsubscribe` 幂等
- 注入 `FakePushSender`，验证：无订阅时不 claim；有订阅时推送一次
- 410 订阅被清理，其它错误累加 `fail_count`
- SSE 与 push 同时在线时，同一提醒只推一次（靠 `reminded_at`）
- `/api/push/key` 在 `PUSH_ENABLED=false` 时返回 `{enabled: false}` 且不泄露私钥

### 已知限制（写进 README）

- iOS 必须「添加到主屏幕」并从图标打开才能授权通知；Safari 标签页里不行。
- 通知送达依赖 Apple 推送服务，网络不好时会延迟，不能当闹钟用。
- 卸载 PWA 会静默失效订阅，需要重新打开设置页授权。
- 与 §14 的底部黑带无关，互不影响。

### 待决策

| 编号 | 问题 | 倾向 |
|---|---|---|
| D13 | 通知里是否带「完成 / 稍后提醒」动作按钮 | 先不做，点通知只打开页面；动作按钮要额外的 `notificationclick` 分支和接口 |
| D14 | 多设备时是否按设备静音某类提醒 | 暂不做，先保证一条通道能通 |
| D15 | 是否把 push 订阅也纳入备份/迁移 | 不迁移：订阅与设备绑定，换设备需重新授权 |

### 实施顺序

1. `domain/push.py` + 表结构 + 单测（不涉及网络，先跑通存储）
2. `/api/push/*` 三个接口 + 鉴权 + 单测
3. 生成 VAPID 密钥写入 `.env`，接 `pywebpush`，`/api/push/test` 打通真机
4. 改造 `reminder_loop`，加入 push 分支与失败清理
5. 设置页开关 + `pushsubscriptionchange` 处理
6. README 安装/授权说明，`.env.example` 补配置
7. 真机验收：关掉 App 等一条真提醒

### 实施状态（2026-11-21）

已完成 1–4、6 与 5 的前半：

- `domain/push.py`：`push_subscriptions` 表，endpoint 唯一，upsert、失败计数、停用与复活。
- `push_sender.py`：`WebPushSender` 包装 `pywebpush`，异常统一转 `PushResult`，只暴露 `ok / gone / status_code`。
- `server.py`：`/api/push/key`、`subscribe`、`unsubscribe`、`test`；`/api/status` 与 `/api/config` 增加推送状态（不含私钥）。
- 提醒循环：**有页面在线走 SSE，没有页面在线才走 push**，避免同一提醒既弹窗又弹系统通知。领取条件改为「有 SSE 订阅者或有未停用 push 订阅」。
- 推送 404/410 直接删订阅，其它错误累加 `fail_count`，达 `PUSH_MAX_FAILURES` 停用；单条超时 15s，失败只记日志。
- `.env` 已生成 VAPID 密钥；`scripts/gen_vapid.py` 可重新生成（会让旧订阅失效）。
- 推送前强制 IPv4：本机到 `web.push.apple.com` 的 IPv6 路由是黑洞，`getaddrinfo` 先给 IPv6 会卡满超时才回退，表现就是「点了半天才到」。`push_sender._prefer_ipv4()` 覆盖 urllib3 的地址族选择。
- 设置页新增「提醒通知」卡片：开启 / 关闭 / 发测试通知，并区分未安装到主屏、权限被拒、服务端未配置。
- `pushsubscriptionchange` 自愈：开启通知时把「公钥 + Token」写进 IndexedDB，SW 在订阅被系统轮换时自行重订阅、上报新 endpoint 并清理旧的；页面关着也有效。
- 测试：`tests/test_push.py` 8 条 + `tests/test_server.py` 推送接口 7 条，共 132 passed。

真机验收（2026-11-23 用户实测通过）：

- [x] 手机关掉页面/锁屏后仍收到系统通知
- [x] 点通知打开应用并在聊天里显示提醒原文
- [x] 经 Tailscale Serve（HTTPS）授权成功，Safari 标签页无效、必须从主屏图标打开（符合预期）

## 16. 设计草案：任务通知与语音处理（2026-11-21）

> 实施状态（2026-11-24）：**网页侧已接上任务上下文**——点系统通知或页面里的任务卡后，
> 下一轮 `/api/chat`、`/api/voice` 带 `task_id`，`Assistant` 把它写成一条 system 提示
> （「当前任务：#12 交电费，到期 …」），模型直接调 `task_complete` / `task_update` /
> `task_cancel` / `task_postpone`，不必先 `task_list`；任务已不存在/已完成时如实说明，
> 不拿别的任务顶。推送 payload 已带 `task_id` / `task_title` / `kind`，`sw.js` 存进 IndexedDB，
> 点通知后页面显示提醒并在输入框上方挂一条「正在处理：…」的绑定条（可手动取消，10 分钟过期）。
> 已落地 `task_update` / `task_cancel` 与 `agent/dates.py`；网页任务卡有「完成 / 延期 / 取消」，
> 「延期」展开固定候选：明天、三天后、下周一、我自己说（D17 已定，见 §16 待决策）。
> **设备端还没接**：小智走 MCP `ask_xiaoma`，提醒是播报出来的、没有「点击」这个动作，
> 也没有把「刚提醒过哪条任务」写给 MCP 会话，所以客厅里说「完成了」仍要靠 `task_list`。
> D17–D19 还没定；§17 之后网页连续语音已暂缓，语音留在设备端。

### 目标

到期提醒不只是"看一眼"，而是能当场处理：完成、延期、或者用一句话说清楚。交互遵循 §17 的产品原则——**点开就进入一次语音会话**，任务卡只是视觉锚点，不是必经的点击路径。

### 硬约束：iOS 通知上没有按钮

iOS 主屏 PWA 的 Web Push 只渲染标题和正文，`showNotification` 的 `actions` 字段会被忽略（Android Chrome 支持）。所以"通知栏里的完成/延期按钮"在 iPhone 上做不到，只能：

- 通知负责**叫醒**（标题 + 一句话 + 任务 tag）
- 点通知 → 进 App → **直接进入语音会话**（带 `task_id` 上下文），顶部显示任务卡

Android 上可以在通知里额外带 `actions`，同一份 payload 不冲突，前端按平台能力降级。

### 交互流程（语音会话）

1. 到期 → push：正文「电费今天到期，点开可处理」，payload 带 `task_id` 与 `kind: "task"`。
2. 点通知 → 进 App → 自动开始聆听，顶部出现**任务卡**作为上下文锚点：
   - 标题、到期时间
   - 三个按钮：`✅ 完成` / `⏰ 延期` / `🎤 语音回复`（快捷方式，不是必点）
3. `✅ 完成` → 直接标记完成，卡片变成不可点的「已完成」。
4. `⏰ 延期` → 给候选：明天 / 三天后 / 下周一 / 我自己说；选完立刻改 `due_at`，卡片显示新时间。
5. 直接说话（不必点按钮）→ 自动分段上传，服务端带着 `task_id` 进 agent：
   - 「完成了」→ 完成
   - 「延到周五」→ 解析日期并改期
   - 「延期」（没说时间）→ 澄清：「延到什么时候？明天、三天后，还是下周一？」
   - 「不用提醒了」→ 取消任务
6. 多轮澄清复用既有 `pending_actions`（按 `conversation_id`，30 分钟有效）；会话内不再需要重复点击麦克风，说「好了」结束本轮。
7. 任务卡与处理结果进聊天历史（`localStorage`），重开还能看到状态与按钮。

### 后端改动

- `tools.py`：新增 `task_update`（改到期时间）和 `task_cancel`（取消提醒）工具；`task_complete` 已有。
- 日期解析：把 `agent/legacy.py` 里的「明天/后天/下周X」抽成公共函数，工具和澄清规则共用，避免三处各写一套。
- `assistant`：会话可绑定「当前任务」（`task_id`），并把到期时间等信息带进工具上下文，让「完成了」能明确指向某条任务，而不是靠聊天记录猜。
- `/api/chat`、`/api/voice` 增加可选 `task_id` 字段。
- `server._send_push`：任务提醒 payload 增加 `task_id` / `kind`；`sw.js` 存 IndexedDB 时一并保留。
- 处理动作统一写 `proposals` 审计（复用 `record_proposal`），完成后按 `task_id` 复查，已被处理就回「这条已经完成了」。

### 前端改动

- 任务卡组件：原生 JS，复用现有 `card` 样式与气泡布局。
- 语音入口复用现有录音流程，录音结束直接 `POST /api/voice`（带 `task_id`），失败降级为文字输入。
- 多轮时输入区提示「按住继续说」，不强制切回打字。

### 待决策

按 §17 修订后，入口不再是"任务卡 vs 语音"二选一：默认点开即会话。剩下三个细节仍待确认：

| 编号 | 问题 | 倾向 |
|---|---|---|
| D17 | 延期候选 | **已定（2026-11-24 落地）**：明天 / 三天后 / 下周一 + 「我自己说」。卡片点选走 `POST /api/tasks/{id}/postpone` 的 `preset`（服务端用 `agent/dates.py` 算日期），「我自己说」把任务绑到会话里并聚焦输入框 |
| D18 | 完成后任务保留还是删除 | 标记 `done` 保留，列表可查历史；删除仍走单独入口 |
| D19 | 「不用提醒了」是否二次确认 | 不确认：可逆（任务还在，只是取消提醒），但它会静默改变行为，回复里要说清楚 |

## 17. 产品形态原则：语音会话为主（2026-11-21 确认）

项目立项就是为多轮语音服务，网页聊天窗是 MVP 阶段的临时形态，是**补充入口而非主线**。后续所有交互（提醒处理、任务管理、生活工具）都按这个原则设计。

### 原则

1. 主路径是「一次开始 → 多轮对话 → 可打断 → 明确结束」的**会话**，不是一条条消息堆叠的聊天窗口。
2. 网页只做会话的替代入口：点一下开始，会话内不再重复点击；切后台自动结束。
3. 唤醒词与免手操作属于设备端（阶段 6，ESP32 / 智能音箱），网页端不承诺后台监听。
4. 一次会话有清晰的生命周期和结束条件；结束时清理临时上下文与待澄清动作。
5. 视觉是辅助：任务卡、状态提示只为"看得到当前在做什么"，不是必经的点击路径。

### 会话状态机（网页端）

```
idle --点麦克风 / 点通知--> listening --静音 1.2s--> thinking --回复--> speaking
                              ^                                        |
                              +------------- 检测到人声打断 -------------+
listening --静音超时 / 说"结束" / 切后台--> idle
```

- 聆听：同一条 `getUserMedia` 流上持续录音，能量 VAD 自动切句，不需要反复按住
- 打断：`speaking` 阶段检测到人声立即停播，回到 `listening`
- 结束：用户显式说结束、静音超时、页面隐藏 → 结束本轮并清理
- 首句授权：iOS 要求录音在用户手势内启动；之后的多轮监听沿用同一条流

### 分阶段（2026-11-21 调整）

语音主路径转向设备端后，网页端的连续语音会话**暂缓**，避免和 EP32 终端重复投入：

| 阶段 | 内容 | 状态 |
|---|---|---|
| A | 会话状态机 + 连续听 + 自动分段（网页端） | 暂缓，等设备端验证后决定是否还需要 |
| B | 打断（播报中检测到人声即停） | 暂缓，设备端协议内建（`abort`） |
| C | 结束条件与状态提示 | 网页保留简单版（聆听/思考/回答的文字状态） |
| D | 任务上下文 + `task_update` / `task_cancel` | 保留，服务端能力，设备端与网页共用 |
| E | 通知点开进入处理 | 改为「文字任务卡 + 语音留给设备」，见 §19 |
| F | 设备端唤醒词与免手 | **主线**，见 §18 |

### 待决策

| 编号 | 问题 | 倾向 |
|---|---|---|
| D20 | 会话静音多久自动结束 | **挂起**：连续语音会话在网页端暂缓（§17 阶段 A），等设备端验证后再定 |
| D21 | 会话内是否仍写入文字聊天记录 | **挂起**：同上；网页现在只记「按住说一句」的对话 |
| D22 | 网页端是否做"长按锁定一直听" | **挂起**：同上，网页只保留按住说一句 |

## 18. 设备接入：小智（xiaozhi-esp32）方案调研（2026-11-21）

目标：客厅放一台 ESP32 语音终端，唤醒词免手、多轮、可打断；家里这套系统作为"大脑"。

### 公开资料确认的事实

- 固件开源（MIT）：`78/xiaozhi-esp32`。支持 ESP32-S3，N16R8（16MB Flash + 8MB PSRAM）足够跑唤醒词与 Opus 解码。当前要求 ESP-IDF v6.0.1+。
- 唤醒词用 ESP-SR WakeNet，可自定义（"小马"），离线运行。
- 有两种传输：WebSocket（JSON 控制 + 二进制 Opus）与 MQTT + UDP。协议文档齐全（`docs/websocket.md`）。
- 设备默认连官方 `xiaozhi.me`；要连自建服务必须自己烧开源固件并配置 OTA / WebSocket 地址。
- 自建服务端有成熟开源实现 `xinnan-tech/xiaozhi-esp32-server`（Python，MIT）：支持本地 FunASR、EdgeTTS、OpenAI 兼容 LLM、function_call、流式 ASR/TTS、声纹、知识库。
- 它的"**MCP 接入点**"是**你的 MCP 客户端出站连接**到服务端暴露的 `ws://.../mcp_endpoint/mcp/?token=...`，然后把工具注册进去。也就是说家里不需要公网入站端口。
- 设备侧协议里已有：`listen/start|stop|detect`、`abort`（打断 TTS）、`tts/start|sentence_start|stop`、`stt`、`mcp`。客厅需要的"可打断"是协议内建能力。

### 三条路径

| 路径 | 做法 | 优点 | 代价 |
|---|---|---|---|
| A. 官方云 + MCP 接入点 | 设备用官方固件连 `xiaozhi.me`；家里跑 MCP 客户端注册 `ask_xiaoma` 等工具 | 最快，几乎零开发 | 语音与 LLM 走官方云，数据出家门；依赖公网 |
| B. 自建 xiaozhi-server（推荐） | 刷开源固件指向家里服务；ASR/TTS/LLM 配成现有能力（FunASR / EdgeTTS / 现有网关），再用 MCP 接入点或自定义工具把 `ask_xiaoma` 接进去 | 数据不出门；语音链路成熟（VAD/流式/打断现成） | 多一个 Python 进程；本地 FunASR 会重复占内存 |
| C. 自研 WebSocket 端点 | 在现有 FastAPI 里实现 `docs/websocket.md` 协议，直接复用 `assistant.process_text` | 一个进程、一套模型，最干净 | 要自己做 Opus 编解码（16k 上行 / 24k 下行，Windows 需带 libopus）+ 分句与并发 |

### 推荐路线

先做 **B**，但避免两套模型同时加载：

1. 固件刷开源版，指向家里局域网 `ws://<PC-IP>:8000/xiaozhi/v1/`（不需要公网、不需要 Tailscale）。
2. xiaozhi-server 只当"音频网关"：ASR 用**自有 FunASR 的 HTTP 识别接口**，TTS 用**自有 EdgeTTS 接口**（在 `server.py` 加 `/api/asr`、`/api/tts` 两个内部接口），LLM 用现有网关。
3. 只注册一个工具 `ask_xiaoma(text)`：把整句交给 `assistant.process_text`，返回口语化结果。这样澄清、提案、确认、记忆全在家里这套里，xiaozhi-server 的 LLM 只负责决定"这句话要不要交给小马"。
4. 跑通后再评估是否值得转 C（省掉一个进程、降低延迟）。

> 状态（2026-11-24）：B 已上线——小智服务端 + MCP 接入点 + 反向提醒 + 拍照都跑通了
> （模拟设备验证），只剩真机刷固件与听感验收。

### 硬件注意

- N16R8 够用；但**单麦 DIY 板远场效果差**，客厅 3-5 米建议选带麦克风阵列或硬件 AEC 的板子（ESP-BOX-3 / Korvo-2 一类），否则"打断"会被自己的播报声误触发。
- 买成品小智前先确认板型与 Flash/PSRAM 规格（有些便宜版本是 4MB、无 PSRAM），并确认能在 `78/xiaozhi-esp32` 的板型目录里找到对应或容易自定义。
- 服务端可开 `CONFIG_USE_SERVER_AEC` 做回声消除，但要求 Opus 带时间戳（协议 v2），复杂度更高，优先用带硬件 AEC 的板子。

### 延迟预期

现有链路是非流式（整句 FunASR → LLM → 整段 EdgeTTS），端到端大约 3-4 秒；xiaozhi-server 的流式 ASR/TTS 能把首字延迟压到 2 秒内。客厅体验对延迟敏感，上设备前应先把流式化列入计划。

### 待决策

| 编号 | 问题 | 倾向 |
|---|---|---|
| D23 | 首台设备买成品小智还是自己配 ESP32-S3 + 麦克风阵列板 | **已定（2026-11-23）**：自配板——已下单立创·实战派 S3（N16R8、ES7210 四通道 + 硬件 AEC），上游板型 `BOARD_TYPE_LICHUANG_DEV_S3` |
| D24 | xiaozhi-server 与现有服务的关系 | **已定（2026-11-23 落地）**：小智服务端当音频网关，用 MCP 接入点调 `ask_xiaoma`；自研端点（路径 C）留作远期 |
| D25 | 是否接受为低延迟改用流式 ASR/TTS | 仍待真机测：先量端到端延迟，再按 D43「精度优先」决定要不要上流式 |

## 19. 执行顺序（2026-11-21 定）

语音主路径定为设备端（§18 路径 B）后，网页端的定位随之收敛：**文字会话 + 统计列表 + 通知落点 + 配置**，不再投入连续语音会话。

### 先做设备端准备（不需要硬件）

理由：它是客厅终端的必经环节，纯服务端、可离线验证，而且它定义了"会话接口"，CLI / 网页 / 设备 / 未来 App 都能复用。网页语音会话若先做，设备上线后大概率作废。

1. **语音网关接口**：`POST /api/asr`（WAV → 文本）、`POST /api/tts`（文本 → MP3/Opus 源），带内部 Token；先做成可被 xiaozhi-server 当自定义 ASR/TTS 调用的形态
2. **ask_xiaoma 工具**：把整句交给 `assistant.process_text`，返回口语化结果；先以 HTTP 接口 + 简单 CLI 客户端验证
3. **MCP 接入点（2026-11-23 完成）**：方向和小智源码一致——**小智服务端是 MCP 客户端**，出站连 `ws://.../mcp_endpoint/mcp/?token=...`；我们这侧由 `adapters/mcp_endpoint.py` 暴露 `ask_xiaoma`，`scripts/mcp_probe.py` 按它的报文形状验证。`params._meta` 的声纹身份已实测透传，见 `docs/xiaozhi-mcp.md`（原计划里"写一个 mcp_xiaoma 客户端"的方向是反的）
4. **联调**：先用小智的 digital-human 测试页（不需要设备）跑一轮，量端到端延迟

### 再做网页端打磨

5. 控制台：统计口径、列表筛选/排序、搜索体验、分页与空状态
6. 通知落点：任务卡（文字版）+ 完成/延期按钮，语音入口标注"设备端支持"（2026-11-22 完成前半：页面在线时任务提醒渲染成卡片，带完成/延期/取消；Web Push 仍是纯文字，因为推送正文不发交互）
7. 任务上下文与工具：`task_update` / `task_cancel`、日期解析抽公共函数（2026-11-22 完成：`agent/dates.py` + 两个任务工具，取消电费提醒不再被澄清拦截）

### 最后

8. 设备到位：刷固件、指局域网、真机验收（唤醒词、打断、延迟）
9. 延迟超标则流式化（ASR/TTS）
10. 随身端（iOS 原生优先于 Android）

### 待决策

| 编号 | 问题 | 倾向 |
|---|---|---|
| D26 | 网页端是否彻底不做连续语音会话 | 是；网页只保留"按住说一句"的兜底入口 |
| D27 | `ask_xiaoma` 的粒度 | 单一入口（整句交给家里系统），不做细粒度工具，避免两套 agent 逻辑打架 |

## 20. 模型选型：在线 aisp 为主，本地小模型做兜底（2026-11-22 定）

> 2026-11-23 起线上默认换到 commandcode `deepseek/deepseek-v4.1-flash` 试用（D47），
> aisp 配置保留可回退；本节表格是当时用 aisp 测的，数字只作历史基线。

给客厅设备挑大脑时，实测了本地 4B 与在线 aisp 的差距，结论是**默认仍用 aisp**，本地模型只在断网和隐私场景顶上。

这一轮落地的护栏、任务闭环和控制台改动，实施细节见 [changes-2026-11.md](changes-2026-11.md)。

### 实测（同一组 9-11 条用例，2026-11-21/22）

| 模型 | 闲聊 | 查询 | 11 条总计 | 主要问题 |
|---|---|---|---|---|
| aisp `DeepSeek-V4.1-Flash` | 2.1-2.6 s | 4.3-6.2 s | 53.0 s | 网关抖动大；偶有措辞串味 |
| 本地 `Spark-X2.6-4B` Q4 | **0.7 s** | **3.1 s** | **27.8 s** | 约一半概率凭空补到期日；多轮指代失效；偶发假确认 |
| 本地 `Spark-X2.5-4B` Q4 | 6-14 s | 3.3 s | 43.2 s | 同上，且更啰嗦 |
| 本地 `Qwen3.5-4B`（越狱版） | 3-7.5 s | 2.3-4.0 s | 54.8 s | 把纯查询当写操作，凭空建任务 |

Spark 需要 llama.cpp `b10828+`（本机已放到 `D:\llama-b12606`，与 `D:\llama-vulkan` 并存）。

### 决策

- **默认 aisp**：写操作的可靠性优先，误提醒是异步暴露的，用户当场发现不了。
- **本地模型**用于两类场景：断网兜底；涉及户号、地址、健康等不便外发的查询。
- 本地模型一律开弱模型模式（`CONFIRM_WRITE_ACTIONS=1` 或 `tasks`）。

### 已落地的防御（对所有模型生效，不随模型强弱变化）

| 机制 | 模块 | 作用 |
|---|---|---|
| 相对日期解析 | `agent/dates.py` | 「下周三」「周末」「3天后」交给代码算，不依赖模型推理 |
| 写参数护栏 | `agent/guard.py` | 日期以原话为准，无日期线索留空；任务标题无依据则整次拒绝 |
| 参数校正提示 | `agent/guard.py` | 把校正结果附在工具返回里，减少模型口播旧值 |
| 写操作确认 | `agent/confirm.py` | `CONFIRM_WRITE_ACTIONS` 控制，复述参数后再落库 |
| 假确认拦截 | `agent/guard.py` | 没调工具却回「已记好」时换成诚实说法 |

设计取舍：前三条无条件生效，是为了换模型时行为不变；代价是偶尔会用代码的判断覆盖模型（已补 12 个线索词避免误抹）。

### 待决策

| 编号 | 问题 | 倾向 |
|---|---|---|
| D28 | `CONFIRM_WRITE_ACTIONS` 默认值 | 保持 `0`；只有部署本地模型时改成 `1` 或 `tasks` |
| D29 | 是否给设备端做 aisp→本地自动降级 | 待定；降级后必须同时切弱模型模式，否则写操作会裸奔 |
| D30 | 是否引入工具专精模型（xLAM-2-3B 一类）只负责参数抽取 | 暂不做；架构从一次调用变两次，收益不确定 |

## 21. 接入小米生态（2026-11-22 调研）

### 结论先说

三条能走的路里只有一条现实：**Home Assistant 当设备网关，小马只跟 HA 的本地 REST / WebSocket 说话**，不碰小米云、也不碰 miIO 协议。

另外两条为什么不行：

- **米家开放平台**（iot.mi.com）是给硬件厂商的，注册要企业组，做的是「把我的产品接入米家」，不是「让我的程序控制米家设备」。方向不对。
- **小爱开放平台**是「用户对小爱说话 → 你的云服务」，方向相反；小爱音箱本身**没有官方 TTS 出口**，想让它播报小马的提醒只能逆向，且型号依赖。

### 三条路对比

| 路径 | 覆盖 | 本地控制 | 维护成本 | 结论 |
|---|---|---|---|---|
| HA + 小米官方集成（`XiaoMi/ha_xiaomi_home`） | 大部分 WiFi / Zigbee（需网关），不支持蓝牙、红外、虚拟设备 | 需小米中枢网关（固件 3.3.0_0023+，仅中国大陆）；否则走云 | 低，官方维护 | **主路径** |
| HA + Miot Auto（`al-one/hass-xiaomi-miot`） | 同上，型号适配更全 | 不需中枢网关，自动取 token 走本地 miIO；状态靠 30s 轮询 | 中，社区维护 | 无中枢网关时补位 |
| 直连 `python-miio` | 只有老 WiFi 设备，新设备是 MIoT-Spec-V2 | 是，延迟最低 | 高，逐台 token，换设备就失效 | 只对 1-2 台高频设备做优化 |
| 米家开放平台 / 小爱技能 | — | — | 企业认证 + 公网服务 + 审核 | 不做 |

许可证注意：官方集成及其云端接口版权归小米，**授权仅限在 Home Assistant 内非商业使用**，禁止用来开发其他应用/Web 服务。小马调的是 HA 自己的本地 API，属于「在 HA 内使用」，边界清楚；但不要绕过 HA 直接抄它的云端接口。

### 小马这边怎么接

原则：**领域层不认识小米**，`domain/` 不 import 任何设备 SDK；HA 只是又一个外部服务，和 LLM / TTS 同级。

新增 `adapters/home.py`：

- `HAClient`：REST `GET /api/states`、`POST /api/services/{domain}/{service}`、WebSocket `subscribe_events`
- 所有异常统一转成结构化结果；HA 连不上就明确回「设备网关连不上」，绝不假装执行成功（和假确认拦截同源）

工具（**search-first**，别把上百个实体塞进系统提示词）：

| 工具 | 作用 |
|---|---|
| `home_query` | 按名称/房间找设备，一次返回候选 + `entity_id` + 当前状态 |
| `home_control` | 执行动作：白名单 + 服务映射 + **回读确认**（白名单为空时不注册） |
| `home_scene` | 执行米家场景（未做） |

查和读合成一个工具，是因为语音链路里每次多一个工具调用就多跑一轮 LLM（约 2 秒），
拆成 `home_search` + `home_status` 会让「空调开着吗」变成四秒。

> 名字最后定的是 `home_query`（不是 `home_search` / `home_status`）；上面的表已经是落地口径。

风险分级：

| 级别 | 例子 | 处理 |
|---|---|---|
| 只读 | 温湿度、滤芯、开关状态 | 直接执行 |
| 低风险 | 灯、空调、窗帘、扫地机 | 直接执行 + **回读状态** |
| 高风险 | 门锁、燃气阀、热水器、大功率插座、任何「开启」类 | 强制确认 + 白名单 |

三条硬规则：

1. **回读确认**：调用后读回 `states` 再回话。HA 返回 200 不代表设备真的动了，和 `correction_note` 是同一个思路。
2. **默认只读 + 控制白名单**：`.env` 加 `HOME_CONTROL_ALLOW`，没列出来的设备只查不控。
3. **绝不假装**：超时、不可达、设备离线，都要如实说。

### 分阶段

1. **装 HA + 集成，只做「读」**：不需要买硬件，用现有设备把 `home_query` 跑通（已完成 2026-11-22）。
2. **低风险控制**：加回读、白名单，接进现有确认体系。
3. **事件入口**：HA WebSocket 订阅水浸、烟感、门磁、人体 → 走 `server._notify` 现有 SSE / Web Push 通道。**这一环价值最高**：米家本来就在往 HA 推事件，小马拿到的是一条现成的传感器网络，而且是只读、零风险。
4. **语音侧**：固定指令表直连（「关客厅灯」不进 LLM，1 秒内响应），复杂说法才进 agent loop。
5. 真想省延迟，再对 1-2 台高频设备做 `python-miio` 直连。

### 已落地（2026-11-22）

- `infra/homeassistant/`：HA 容器（`ghcr.io/home-assistant/home-assistant:stable`，
  配置在已忽略的 `data/homeassistant/`）+ 小米官方集成 v0.5.0，装法与常见问题见该目录
  README。Windows 上跑 Docker Desktop，WSL2 已切 `networkingMode=mirrored`。
- `adapters/home.py`：`HAClient`（读 states、调服务、3 秒快照缓存）+ `HomeDevices`
  （按名字找设备、把状态说成中文）+ 风险常量与 `HOME_CONTROL_ALLOW` 白名单判断。
- `tools.py`：`home_query`，只在配置了 `HOME_TOKEN` 时注册；`home_control` 额外要求
  `HOME_CONTROL_ALLOW` 非空（**默认空 = 全部只读，工具根本不注册**）。
- `agent/guard.py`：`DEVICE_TOOLS` + `_DEVICE_CLAIM_RE`，没调 `home_control` 却回
  「已经把灯关了」时换成「设备没动」，措辞与「没写进清单」分开。
- `scripts/ha_check.py`：接设备前先自检令牌，并打印设备名 / `entity_id` 清单。
- 测试 214 → 248。

### 接入实测（2026-11-22，米家 8 台设备）

HA 里 **451 个实体、237 个「设备」**——MIoT 把每个属性都变成一个实体，一台空调
贡献 60 多个 switch（扫风、提示音、水印、智能场景开关……）。关键词过滤救不了，
最后改成**白名单 `HOME_DEVICES`**，可写成 `entity_id=别名`：

```
HOME_DEVICES=light.xxx=客厅灯, sensor.yyy=RO滤芯
```

效果：17 个设备，名字是「客厅灯」「RO滤芯」这种能直接念的。实测三句：

| 用户 | 工具 | 回答 |
|---|---|---|
| 净水器的 RO 滤芯还剩多少 | `home_query` | 还剩 58%，还能用一阵子 |
| 客厅空调开着吗 | `home_query` | 关着的，设定 22.5℃，室温 19.4℃ |
| 把客厅灯关了（白名单为空） | 无 | 只能查状态，还关不了，得你手动关 |

### 接入时踩到的三个坑

1. **小米官方集成把 OAuth 回调写死在 `homeassistant.local:8123`**（`const.py:142`），
   表单下拉框只有这一个值。本机解析不到这个名字时授权必失败（表现为 503）。
   解法：hosts 加 `127.0.0.1 homeassistant.local`。
2. **别用 `_p_\d+_\d+` 后缀过滤实体**：滤芯剩余寿命、水温这些有用量值也带这个后缀，
   一刀切会连有用的一起删掉。
3. **中文名里的空格**：米家给的是「客厅 灯」，用户说「客厅灯」，匹配必须先去掉空格再比。

已知问题：摄像头在官方集成里**没有 camera 实体**（只有指示灯和一堆配置开关）；
三个吸顶灯长期 `unavailable`（墙上开关断电时就是这样，此时控制会被如实拒绝）。

### 控制实测（同一批设备，白名单 7 个）

| 用户 | 结果 |
|---|---|
| 把晾衣架灯打开 | `home_control` → 回读确认「已打开，亮度 100%」 |
| 把晾衣架灯关了 | 回读确认「已关了」，`ha_check` 复查状态确为「关」 |
| 把客厅灯关了（设备离线） | 「现在离线，指令发不出去，可能得检查下开关或网络」 |

回读是真做了：`call_service` 之后轮询 `states` 2.5 秒，到期望值才说「已完成」。

### 主动提醒（滤芯 / 故障 / 事件）

`agent/home_watch.py` + `agent.db` 的 `home_alerts` 表（按 `kind + entity_id` 去重），
设备巡检走**独立 15 秒循环**（`HOME_SCAN_SECONDS`），物品/任务仍走 60 秒。

| 规则 | 触发条件 | 去重 |
|---|---|---|
| 滤芯余量 | `< HOME_FILTER_ALERT_PERCENT`（默认 10%） | 24 小时内不重复 |
| 故障码 | `故障` 传感器 ≠ 0 | 同一错误码只报一次 |
| 事件 | 盯住的 `event` 实体状态变成新时间戳 | 同路 10 分钟内不重复 |

两个刻意的取舍：

- **事件时间戳要新鲜**（`EVENT_FRESH_SECONDS=180`）。第一次看到旧时间戳只记下来不播报，
  否则一开机就把历史事件当新闻推一遍；但如果是「刚发生的」，第一次也照报——
  不然门铃第一次响会被吞掉。
- **摄像头事件默认不盯**。移动侦测按小时触发，默认开会把客厅变成刷屏机器。
  更值钱的是门锁那几路：门铃响、门锁开关、猫眼低电量。

实测：复合滤芯 0%（真实数据）触发过一次提醒，`home_alerts` 记录
`2026-11-22T20:26:38`，Push 已送达。

### 事件改成 WebSocket 订阅（2026-11-22）

轮询是 15 秒一轮，门铃按下去可能半分钟后才响，等于没用。`adapters/home_ws.py`
接上 HA 的 WebSocket：认证 → `subscribe_events(state_changed)` → 客户端按
`HOME_WATCH_EVENTS` 过滤 → 交给 `server._on_home_event` 走同一套去重（`home_alerts`
是唯一裁判，先到的记下 `last_value`，后到的跳过）。

实测（真实设备）：调 `light/turn_on` 后 **0.46 秒**收到 `state_changed`。
订阅 `state_changed` 全量而不是 `subscribe_trigger`：451 个实体在局域网里不占带宽，
少一层触发器配置格式，少一个坏点。

三条实现约束：

- 断线退避重连（1/2/5/10/30 秒）。断线期间的事件**会丢**——健康巡检读的是当前状态，
  不受影响；事件是「发生的瞬间」，补发没意义。
- 没有人在收（无在线页面也无 push 订阅）时不写去重状态，否则额度被消耗而人没收到；
  轮询那边还有 3 分钟新鲜度窗口兜着。
- 轮询**保留**作兜底（15 秒一轮），两条通道靠 `last_value` 去重，不会推两遍。
- `aiohttp` 从此是直接依赖，已在 `requirements.txt` 钉到 3.13.5。

已知限制：Docker Desktop 的端口发布在局域网不可达，手机浏览器要访问 HA 需要一次
管理员权限的 `netsh portproxy`（见 infra README）。

### 待决策

| 编号 | 问题 | 倾向 |
|---|---|---|
| D31 | 接入方式 | **已定：HA + 小米官方集成**（2026-11-22 装好） |
| D32 | 第一阶段能力范围 | **已定：只读 → 事件 → 低风险控制 → 高风险单独一批**（用户选定的顺序） |
| D33 | 米家事件是否做主动提醒 | 做，但家里暂时没有水浸/烟感/门磁，先只能靠净水器、摄像头；买传感器后再展开 |
| D34 | 小爱音箱当播报出口 | **已定：放弃**；播报走 ESP32 / 网页 / 手机 |
| D35 | 设备与 `entities` 的关系 | 待定；先靠 HA 的 friendly_name 匹配，别名需求出现再接 `entities(type=device)` |
| D36 | 高风险设备清单与白名单默认值 | 默认空 = 全部只读（已实现）；实测按 7 台低风险设备配了白名单，门锁/燃气/热水器等高风险清单仍待列 |
| D37 | 设备状态是否允许过小米云 | **需要拍板**：官方集成默认云控，状态与控制都经小米云；要本地必须上中枢网关（仅国行） |

## 22. 部署形态：家庭服务器（2026-11-22 记录，暂不动手）

目标机器：**ThinkPad X13 Gen1** / Ryzen 5 PRO 4650U（6C12T Zen2）/ 16 GB / 477 GB SSD，
计划装 Linux 后作为家庭服务器。

**结论：现在不搬。** 等小米接入、推送、ESP32 真机都调通、可以上线了再迁。理由很实际：
迁移会改变 LAN IP 和访问源，ESP32 固件里的服务端地址、浏览器的推送订阅都要跟着改一遍；
在还在改代码的阶段搬，等于把这些改动做两次。

### 够不够

| 负载 | 预计占用 |
|---|---|
| Ubuntu Server + Docker | ~1 GB |
| HA 容器 | 0.4–0.8 GB |
| 小马服务（FastAPI + FunASR） | 1.5–2.5 GB，CPU 突发 |
| 5 个 SQLite | < 50 MB |
| 本地 4B 兜底（可选） | ~3 GB |
| 合计 | 4–6 GB / 16 GB |

- 性能预期：比现在的 Ryzen 7 255 慢约 40–50%。FunASR 从「很快」变成每句 1–3 秒；
  本地 Spark 4B 兜底从 0.7 秒掉到 2 秒上下。走 aisp 时不受影响。
- 真实收益：空载 7–10 W（一年电费约 40 元，桌面机 40 W 约 200 元），
  而且**电池就是现成的 UPS**，短暂停电不会把 SQLite 写坏。

### 搬之前必须先解决的三个坑

1. **合盖休眠**：改 `logind.conf`（`HandleLidSwitchExternalPower=ignore`），关 WiFi 省电，
   机器平放、留进风。
2. **用网线**：HA 的 mDNS 发现、米家本地 miIO、ESP32 的 WebSocket 都对 WiFi 省电和漫游敏感。
3. **HTTPS**：Web Push 与 Service Worker 只在安全上下文里工作。现在能推是因为访问 `localhost`；
   手机打开 `http://<LAN IP>:8200` 会同时失去推送和「添加到主屏」。
   要么 Caddy + 本地 CA（手机装一次根证书），要么 Tailscale / Cloudflare Tunnel。
   这条不解决，「提醒送到手机」是断的。

### 迁移清单

1. `data/` 整个目录 + `.env`（VAPID 私钥、API_TOKEN 丢了找不回）
2. 推送订阅全部失效（订阅和 localStorage 都绑在「源」上），每台设备重新开启通知
3. ESP32 固件里的服务端地址改成新 IP
4. compose 改回 `network_mode: host`（Windows 上的桥接只是为了绕开 Docker Desktop）
5. `start.bat` → systemd unit，`Restart=always`

### 建议形态

Ubuntu Server 24.04 LTS + Docker + compose，HA 用容器（和现在一致，备份就是拷目录）。
开发继续留在现在这台，服务器只做部署。

### 重装前先做（都会随系统一起没）

1. `powercfg /batteryreport` 看电池健康（低于 60% 就别指望它当 UPS）
2. CrystalDiskInfo 看 SSD 健康
3. 确认内存是板载还是可换（16 GB 现在够，跑 7B 就不够）
4. 记下无线网卡型号（Realtek 卡在 Linux 上要折腾驱动）
5. **BitLocker 解绑 + 微软账号退出**，否则旧盘重装后读不出来

## 23. 身份：声纹与多角色（2026-11-22 调研）

### 结论

- **声纹不用自己训**：小智已经做完，而且会主动把身份塞进 MCP 调用。
- MCP 接入点已完成（2026-11-23，§19 第 3 步），`_meta` 的 `speakerId/speakerName` 已实测透传。
- 声纹只能当**软身份**（称呼、上下文、默认值），**不能当安全凭据**。

### 小智是怎么做的

| 环节 | 实现 |
|---|---|
| 声纹服务 | 独立部署 `xinnan-tech/voiceprint-api`（3D-Speaker，134★，Apache-2.0），端口 8005，**依赖 MySQL** |
| 识别 | 与 ASR 并行，拿同一段音频调 `/voiceprint/identify`，返回 `speaker_id` + `score`，阈值默认 **0.4** |
| 传递 | 命中后写进 MCP `tools/call` 的 `params._meta`：`{speakerId, speakerName}`；`mcp_forward_speaker` 默认开启 |
| 未命中 | 展示名「未知说话人」，且 `_meta` 里不放 name |
| 注册 | 管理台录一段音频；配置项是 `speaker_id,名称,描述`（描述进小智自己的提示词） |

关键代码：`core/providers/tools/mcp_endpoint/mcp_speaker.py`、`core/utils/voiceprint_provider.py`。

### 三条路的取舍

| 路径 | 覆盖 | 成本 | 结论 |
|---|---|---|---|
| 吃小智的 `_meta` | 只有小智设备 | 零模型成本，但要先做 MCP 接入点 | **先做这个** |
| 自建 `adapters/speaker.py` | 手机 / 网页 / 小智都能用 | 一个 campplus 模型（~30 MB）+ 注册与阈值工程 | 手机端进来时再做 |
| 直接部署 voiceprint-api | 小智设备 | 现成，但强依赖 MySQL，和「SQLite 唯一库」冲突 | 不选 |

自建那条的技术底子是现成的：ModelScope 的 `iic/speech_campplus_sv_zh-cn_16k-common`，CPU 上抽 embedding 约百毫秒级，SQLite 存 blob + 余弦相似度即可，不引入新框架（FunASR/ModelScope 已是依赖）。

### 认人之后得到什么（按价值排）

1. **称呼与语气**：叫得出名字，对小孩用短句。零风险，立刻有感。
2. **上下文隔离**：每个人的记忆、任务、物品各归各的。
3. **权限**：小孩（Supervised）的写入默认要大人确认——接 family-os 那套思路。
4. **隐私分区**：某些事实只对本人可见。

2、3、4 都要动数据模型（`persons` + `owner` 字段），比声纹本身大得多。

### 四条硬规矩

1. **身份是不可信输入**：`speakerId` 必须在 `persons` 里查得到才认，`speakerName` 只用于显示。
2. **认不出来就退回默认身份**，绝不猜。
3. **声纹不能当安全凭据**：录音回放即可冒充，门锁/燃气永远不用声纹授权。
4. **认错人的代价要可控**：写操作照样走提案/确认，身份只影响上下文和默认值，不能绕过护栏。

### 已知风险

- 短句（「关灯」）音素太少，声纹准确率明显低于整句；儿童声线会随年龄漂移。默认阈值 0.4 偏松。
- 小智的 `_meta` 是我们的 MCP 框架能不能透传到工具函数，需要实测（部分框架会丢掉 `_meta`）。
  **2026-11-23 已实测**：小智用裸 JSON-RPC，`_meta` 就在 `params` 里，我们的接入点原样收到；
  见 `docs/xiaozhi-mcp.md`。
- 小智 speakers 配置里的「描述」不会随 `_meta` 过来，要用得在小马侧另存人物档案。

### 待决策

| 编号 | 问题 | 倾向 |
|---|---|---|
| D38 | 短期是否只吃小智 `_meta` | **已定（2026-11-23）**：是；MCP 接入点已上线，声纹只用于 `client_id` 分会话 |
| D39 | 长期自建 campplus 还是用 voiceprint-api | 自建；不为了声纹引入 MySQL |
| D40 | 身份怎么挂到数据模型 | `persons`（复用 `entities(type=person)`）+ `owner` 字段 |
| D41 | 小孩/客人身份的默认权限 | 写入默认要成人确认，只读自由 |

## 24. 反向提醒：服务端 → 设备（2026-11-23 定）

主动提醒必须由**持有设备连接的那一侧**发：小智设备协议里 `tts/start` + Opus 是服务端下发的，
而我们的小马只是小智的 MCP 工具，插不进那条连接。三条路比过之后选 **A：给小智服务端打小补丁**，
B（设备直连小马、自己重写语音主循环）留作远期。取舍见 D42。

- 固件保持官方版，不改、不编译
- 只 fork `xiaozhi-server`，改动集中在「连接注册表 + 启动订阅任务 + 调用 `speak_txt`」
- 提醒优先级改为：**设备在线 → 网页在线 → Web Push**，同一提醒只走一条通道
- 详细改动清单与验收步骤见 [device-reminders.md](device-reminders.md)

**进度（2026-11-23）**：小马侧设备通道已完成（真实验过 `device_channels` 1 → 0）；
小智侧补丁已构建并运行，假设备端到端收到 tts 消息与 76 个音频包。

### 待决策

| 编号 | 问题 | 倾向 |
|---|---|---|
| D42 | 反向提醒走哪条路 | **已定 A**（2026-11-23）：fork 小智服务端加一个订阅模块；固件不动。B 只在将来要自建整套语音链路时再考虑 |
## 25. 硬件到位前的准备（2026-11-23 排）

清单见 [pre-hardware.md](pre-hardware.md)。要点：

- **反向提醒（A）**：设备只是一问一答，还是要能主动说话
- **延迟**：11-21 实测 aisp 首句 8.3 s、整轮 14 s；官方路由 2.8 / 11.2 s。当前不做分流，
  所以必须在买硬件前复测并拍板验收线（D43）
- **唤醒词**：ESP-SR 内置是「你好小智」；上游已支持 MultiNet 自定义「小马」（D44），不必等定制
- **硬件选型**：D23 已下单立创·实战派 S3（N16R8、ES7210 四通道 + 硬件 AEC、200W 摄像头），
  上游板型 `BOARD_TYPE_LICHUANG_DEV_S3`，见 `docs/device-lichuang-s3.md`
- **HTTPS**：手机推送与 PWA 只在安全上下文可用
- HA 常开与门铃真实触发、评测集、备份与日志轮转

### 待决策

| 编号 | 问题 | 倾向 |
|---|---|---|
| D43 | 语音延迟目标，以及是否为此改变模型路由 | **2026-11-23 修正：精度优先。** 只做不改变「说什么/写什么」的优化；不做关键词路由代替理解、不跳过校验、不为速度换模型。延迟验收线按真机体感定，不追 3 s 的硬指标 |
| D44 | 唤醒词用内置还是定制「小马」 | 待定；2026-11-23 核实上游支持 `USE_CUSTOM_WAKE_WORD`（MultiNet，拼音），「小马」可自编译、不必等定制。选项：A 内置「你好小智」/ B MultiNet「小马」/ C 定制 WakeNet。B 需真机实测远场命中率 |
| D45 | 写操作成功后是否跳过第二轮 LLM | **2026-11-23 已撤回短路（`4554eae` → 本轮回退）。** 用户判断：不准确或有歧义的不入库；既然入库的都是准确的，回话就该由模型完整组织，不能为 2 秒省掉理解。写入首句回到 7–8 s 可以接受 |
| D46 | 结构化事实的检索口径 | **2026-11-24 已补齐同义说法**：`Facts.search` 先拆二字组、逐词 AND 匹配「主体+属性+值」拼接文本；查询和库里的文本都先经 `agent/synonyms.py` 归一（户号/账号、燃气/煤气/天然气等），所以「存户号、问账号」也能命中。同义替换只放宽**组内那一个词**，别的词仍是 AND（「电费账号」不会捞出「水费户号」）；不做单字替换，不收「水费/水表」这类相关词——宁可查不到，也不查错 |
| D47 | LLM 网关长期用 aisp 还是 commandcode | **试用中（2026-11-23）**：commandcode `deepseek/deepseek-v4.1-flash`，评测集 **30/30**（平均 5.8 s/条）；闲聊首字 2.0 s、写入 5.5 s、查询 7.0 s（aisp 为 3.4 / 6.4 / 6.4）。精度不掉、查询略慢。`.env` 已切，aisp 注释保留可回退；正式拍板等真机听感。**晚复跑 30/30、平均 7.1 s**：首轮 65 s/条是机器没走代理（`NO_PROXY` 让 requests 跳过系统代理），配 `LLM_PROXY` 后回到 2–3 s，详见 `changes-2026-11.md` §31 |

## 26. 摄像头：单张拍照问答（2026-11-23 已实现，待真机验证）

范围先说清楚：只做「用户问 → 拍一张 → 说出来」。不做视频、不做连续看、不做人脸/身份识别。
这是 §1「保留图片输入扩展点」的落地，不是开「复杂视觉能力」。

### 可行性已核实（2026-11-23）

| 环节 | 结论 |
|---|---|
| 板级 | 上游 `lichuang_dev_board.cc` 有 `InitializeCamera()` / `GetCamera()`；`mcp_server.cc` 在 `board.GetCamera()` 非空时注册 `self.camera.take_photo` |
| 上游链路 | 设备拍 QVGA(320×240) → JPEG → multipart POST 到服务端下发的 vision 地址 → 文本 → **`Action.RESPONSE` 直接播报，不经过二次 LLM** |
| 模型 | 实测 `deepseek/deepseek-v4.1-flash`（`reasoning_effort=off`）能读图：320×200 测试图答出「左边红方块、右边蓝圆圈」，2.15 s / 2.19 s |
| 断点 | ① 小智 `Intent: nointent` 不把设备工具交给 LLM；② `server.vision_explain` 为空；③ 我们 `/v1` 只取文本、不看 `tools` |

### 决策（2026-11-23）

- **D48 已定**：做单张拍照问答，照片内容进我们的大脑（能带记忆/事实上下文），允许图片经
  commandcode 网关识别（和文字一样出门，不存原图）。
- 代价：比纯文字多 3–5 秒（拍照约 1 s + 视觉 2–4 s）。上游把视觉结果直接播报，所以只多这一次，
  不会「两轮变三轮」。

### 要改的四处

1. `D:\xiaozhi-server\data\.config.yaml`：`Intent: function_call`、`server.vision_explain:
   http://192.168.0.138:8303/mcp/vision/explain`（Docker 里 8003 → 宿主机 8303）、`VLLM` 指向
   `http://host.docker.internal:8200/v1`（模型名如 `xiaoma-vision`）。
   切 `function_call` 对现有聊天无影响：上游在没有 `tool_calls` 时照常把 `content` 送 TTS。
2. `tools.py`：注册 `camera_look(question)`，由模型自己判断什么时候要看（不做关键词路由，D43）。
3. `assistant.py`：该工具不落库，返回「设备调用指令」，agent loop 立即收尾并带出指令。
   注意第一轮是流式的：模型若先吐了话再决定拍照，话已经播出，收不回来——工具描述要写成
   「要拍照时直接调，不要先说话」。
4. `server.py`：`/v1` 有指令时回 SSE `tool_calls`（`finish_reason=tool_calls`）；请求里出现
   `image_url` 时走视觉分支（同一模型、沿用 `reasoning_effort`），不再走 `_last_user_text`。

### 落地（2026-11-23）

四处都改完了，**没等硬件**：用 curl 模拟小智的请求先把两端验通。

| 位置 | 实际改动 |
|---|---|
| `.config.yaml` | 三处配置（原文件备份为 `.config.yaml.bak-20261123-camera`）；YAML 已解析校验 |
| `tools.py` | `camera_look(question)`；`execute` 只在没有摄像头的入口被调用，直接说「看不到」 |
| `assistant.py` | `process_text` / `_agent_loop` 加 `device_camera`；**没有摄像头就不把 schema 给模型**；模型调 `camera_look` 时直接转成 `device_call` 收尾（不执行、不落库、不记会话）；新增 `describe_image`（最多两张图、失败降级）；拍完把描述留在内存 180 秒，供下一句追问用 |
| `server.py` | `ChatCompletionRequest.tools`；`_user_text_and_images` / `_has_camera_tool`；`/v1` 看图分支；`device_call` → SSE `tool_calls` 且**不再补一个 stop** |

模拟验证（真模型、本机 /v1）：

| 场景 | 结果 |
|---|---|
| 带 `self.camera.take_photo` 工具问「看看桌上有什么」 | 3.4 s 回 `tool_calls`，参数 `{"question": "桌上有什么东西"}`，`finish_reason=tool_calls` |
| 带同样的工具说「你好呀」 | 5.2 s 纯文字回答，**没有触发拍照** |
| 直接 POST 一张图（`image_url`） | 2.1–3.0 s 答出「左边一个红色方块，右边一个蓝色圆形」 |
| **经小智视觉端点**（容器里的 VLLM → 我们 `/v1`） | 3.0 s，回 `{"success":true,"action":"RESPONSE","response":"一个红色的正方形和一个蓝色的圆形。"}` |
| **假设备整条链**（`scripts/fake_camera_device.py`） | 通过：报工具 → `% self_camera_take_photo` → 设备 POST 图 → 播报「桌上有一个红色的正方形和一个蓝色的圆形」 |

小智侧配置确认（容器重启后）：

- 启动日志 `初始化组件: intent成功 function_call`、`get_vision_url` 返回我们的
  `http://192.168.0.138:8303/mcp/vision/explain`
- 上游启动那行「视觉分析接口是 http://172.18.0.2:8003/…」是**硬编码打印容器 IP**，
  不代表实际下发地址，别被它误导（实际用的是 `get_vision_url`）
- 容器里没有 `server.auth_key`，每次启动随机生成；设备发图用的是 MCP 初始化时下发的 token

**假设备抓到的一个真问题**：小智给 LLM 的工具名是下划线版（`self_camera_take_photo`），
不是原始的点号版。最早按 `self.camera.take_photo` 精确匹配，真机上会「有摄像头也不认」；
就算认了，回调用点号名字小智也查不到工具。现在判断和回调都按请求里的名字走（归一化后比较），
测试补了三条。**这条只有跑假设备才会暴露**——单测和 curl 模拟都用的点号，全绿。

测试 381 → 396（assistant 8 条、server 7 条）：schema 有无、`device_call` 形状、
question 回退原话、无摄像头时的降级、看图不被记忆、失败降级、SSE 顺序。

**踩到的坑**：第一版把「拍照」那一轮的空回复也写进了会话上下文，结果下一句「你好呀」被带偏，
模型继续去拍照。修法是 `device_call` 这一轮不 `_record_turn`。

### 拍完之后：别让追问变成重拍（2026-11-23）

设备把画面描述播出来就结束了，我们这边并不知道刚才看到什么。用户顺口问「那是什么牌子的」，
模型只能重拍一张，或者干脆瞎答。

做法：`describe_image` 成功后把描述记进 `assistant.vision_context[client_id]`，
`_agent_loop` 组 messages 时插一条 system——「刚才摄像头看到：…，接着问这个画面就按它答；
画面外、看不清就说看不到」。TTL 由 `VISION_CONTEXT_SECONDS` 控制（默认 180 秒）。

- **不进记忆库**（D50）：画面是一次性的，写进 FTS 会让三天后的查询拿到过期画面
- 按 client_id 隔离，重启即失，过期即删
- 看图失败不留描述，免得下一句拿一句空话当依据
- **没有**把记忆/事实塞进看图提示：那会把「记忆里有牛奶」读成「画面里有牛奶」，
  属于编造。要跨来源对照就让用户明确问（以后可以做「查记录 + 看一眼」两个工具的组合）

### 硬规矩

- 原图不落盘，内存里用完即弃；日志不打印 base64。
- 不做人脸识别/身份推断：画面里有人也不描述是谁（声纹只作软身份，同 D38–D41）。
- 每次拍照设备屏幕会显示 `% self.camera.take_photo`（上游自带），不要关掉。
- 视觉失败要降级成「我这边没看清，你再说一遍」，不能假装看到了。

### 验收（无硬件可先做一半）

- [x] 模拟小智发一个带 `tools` 的流式请求 → 我们回 `tool_calls`，参数里有 question（2026-11-23）
- [x] 直接 POST 一张 JPEG 到 `/v1`（`image_url`）→ 回一句正确的中文描述（2026-11-23）
- [ ] 真机：说「看看桌上有什么」→ 屏幕出现 `% 拍照` → 3–5 秒内播报，内容对得上
- [ ] 真机：遮住镜头/拔掉 WiFi → 说「看看」要给出失败提示，不能编内容

### 待决策

| 编号 | 问题 | 倾向 |
|---|---|---|
| D48 | 是否做「单张拍照问答」，照片是否可经在线网关识别 | **已定（2026-11-23）**：做；不存原图、允许经 commandcode 网关识别，与文字同样出门 |
| D49 | 拍照的触发口径 | 待定：A 模型自主判断（「冰箱里有什么」也会拍）/ B 只在用户明确说「看看/拍照/这是什么」时拍 / C 做成开关。A 最自然、B 最保守；倾向 A + 屏幕提示，先真机看误拍多不多 |
| D50 | 照片内容要不要写进记忆 | **2026-11-23 已定：不进记忆库。** 描述只在内存里留 `VISION_CONTEXT_SECONDS`（默认 180 秒），够接一句追问，过期/重启就没了。写进 FTS 会让「冰箱里有牛奶」三天后还查得到 |

## 27. 周期提醒：每天 / 每周 / 每月（2026-11-23）

家庭里真正高频的提醒几乎都是周期性的：吃药、倒垃圾、接孩子、交房租。之前 `tasks`
只有一次性，说「每天八点吃药」只能建出明天一条，后天就没了。

### 范围

- 三种周期：`daily` / `weekly` / `monthly`；不做完整 RRULE，也不做「工作日」「隔周」
- 周期**必须带具体时间**（每天几点 / 每周几几点 / 每月几号几点）；没说就追问，不替用户定
- **到点才提醒**（不提前）；服务停过再开只补一次，然后滚到下一次
- 完成 = 跳过这一次，周期继续；取消 = 整个停掉；改期 = 改下一次，锚点跟着走
- 每月 31 号遇到小月按当月最后一天

### 为什么这么做

- 提前提醒对周期任务没意义（「每天八点吃药」提前一天说是噪音），所以周期任务不走
  一次性的「N 天内到期」窗口，而是 `due <= now`
- 滚动后清 `reminded_at`：否则 `REMINDER_COOLDOWN_HOURS=24` 会把第二天的提醒挡住
- 完成周期任务不标 `done`：标了就从列表消失，用户会以为提醒没了
- 「每天提醒我吃药」不替用户编一个八点：几点吃药是用户知道的，多问一句比编一个时间便宜

### 落地

| 位置 | 改动 |
|---|---|
| `domain/tasks.py` | 新增 `recur` 列（旧库 `ALTER` 补列）；`claim_due_reminders` 到点领取并滚动；`complete` 周期任务跳过本次；`_advance` 处理月末 |
| `agent/dates.py` | `parse_recurrence`：识别每天/每周X/每月X号并算出「下一次」；当天时刻已过就顺延一个周期 |
| `agent/guard.py` | `ground_recur`：只认原话里的周期；没时间就拦下，让模型追问 |
| `tools.py` / `wording.py` | schema 加 `recur`；列表标「每天/每周三/每月5号」；确认句说周期，不说「明天」 |
| `server.py` / `static/index.html` | 提醒文案用周期标签；任务列表显示周期 |

### 验收

- 评测 4 条：「每天八点吃药」「每周三下午三点开会」「每月1号交房租」各落库正确；
  「每天提醒我吃药」追问几点且不落库
- 单测：解析（含月末）、滚动、错过多次只补一次、完成跳过、冷却不挡下一次、措辞

### 待决策

| 编号 | 问题 | 倾向 |
|---|---|---|
| D51 | 周期任务的「完成」语义 | **已定（2026-11-23）**：跳过本次、周期继续；取消才停整个系列 |
| D52 | 自定义 `DATA_DIR` 时备份范围怎么算 | 暂不跟随：`scripts/backup.py` 仍只扫仓库 `data/`，由 `doctor.py` 明确提醒；等真有这种部署再改 |
| D53 | 工具该返回事实还是成品话术 | **已定（2026-11-24）**：只给事实和结构化数据，穿衣/带伞这类建议由模型按用户情况组织；工具里写死等于把 LLM 降级成模板填空。例外仍只有写操作回显和回复校验兜底 |
| D54 | 天气由谁提供 | **已定（2026-11-24）**：小马的 `weather_query` 工具。小智的 `get_weather` 插件在 `/v1` 链路里根本不生效——`/v1` 只把小马自己的工具给模型，`req.tools` 只用来判断有没有摄像头 |
| D55 | 家庭位置（城市）存在哪 | 待定：当前在 `.env` 的 `WEATHER_CITY`；倾向于存进 `facts`（`家/所在城市`），天气、穿衣、出门提醒共用一份 |
| D56 | 约束写在提示词还是代码里 | **已定（2026-11-25）**：提示词只留身份/语气，场景路由表和格式要求尽量删——工具 schema 写清能力，模型自己判断；会「说错」的约束才做执行期检查（guard/confirm/verify）。删完跑 `eval_accuracy.py` 验证，掉了修工具描述，不加回路由表 |
| D57 | 开源形态：设备语音管线归谁 | **已定（2026-11-25）**：近期走 A/C——小马主体以网页/手机/API 独立可跑为准，ESP32 设备作为「高级玩法」，靠 `infra/xiaozhi-server` 的 compose + 补丁 + 文档复现；终局走 B，把小智那套语音管线（WS 协议、Opus、VAD、打断）做进小马，去掉 10 GB 镜像和第三方补丁。触发条件：出现真实外部用户，或小智上游改动频繁到补丁维护成本失控 |
| D58 | 记忆分几层、各存哪 | **已定（2026-11-25）**：人格只读（代码常量，模型不能改）；画像按人分（`用户/<说话人>/偏好`，约 500 字/人）；记忆长尾（现有 `memory` + `facts`）；**热点层注入 system prompt**——画像 + 常用事实（地址/户号）+ 最近 2-3 条记忆，冻结快照（会话内写入下次生效，保前缀缓存）。依据：本地 FTS 查询 1-5 ms，慢的是多一轮 LLM（2-5 s），热点常驻能省掉这一轮 |
| D59 | 谁可以写记忆/画像 | **已定（2026-11-25）**：用户陈述即时写；**模型推断出的偏好只提议、不直接写**，用户点头才落；画像写入前必须问一句。全部走 `proposals` 审计，网页端可看/改/删。依据：OpenClaw 允许 agent 改 SOUL.md、Hermes 有 `write_approval` 开关，两者都承认「模型会存错假设」。**已落地（2026-11-25）**：`profile_set` 无条件确认、主体挂 `用户`/`用户/<名字>` 前缀，审计留 `proposed`/`committed`/`cancelled` 三态 |
| D60 | 记忆怎么蒸馏 | **已定（2026-11-25）**：**不删原文，只重算 summary**（原文留 FTS 兜底，摘要进索引）；每天深夜跑一次（或超过 500 条），用本地 LLM 合并同主题条目。不抄 Hermes 的「写满报错让模型当场合并」——语音场景里模型没机会重试。**已落地（2026-11-25）**：`agent/distill.py` 星形聚类 + LLM 合并，摘要写到最新一条；`memory_distill_log` 按成员指纹防重复；服务端 3 点后跑、超 500 条按 6 小时补跑 |
| D61 | 打印：走哪条链路、入口在哪 | **已定（2026-11-25，按部署方向修正）**：服务器是 **Linux**，打印走 **CUPS**（`lp` / `lpstat`），不写任何 Windows 专用代码（Windows 队列、GDI、Office COM 全部移除）。入口仍是**网页聊天附件**（上传 → 说「打印两份」→ 确认）；支持 **PDF / 图片 / 纯文本与 Markdown / Office**。PDF、图片、文本直接交给 CUPS 过滤器；Office 用服务器上的 **LibreOffice**（`soffice --headless`）转 PDF。`PRINTER_SERVER` 填远程 CUPS（`192.168.1.15:631`）、`PRINTER_NAME` 填队列（`HP1020`），用 `lp -h` 提交，容器不用跑 cupsd；Docker 镜像带 `INSTALL_PRINT=1`。约束：文件走临时目录、按 TTL 清理（默认 30 分钟，「再打一份」不用重传）；每次确认；份数/页数上限；只允许配置队列。Windows 开发机上打印显示不可用，验收在 Linux/Docker 部署做。**2026-11-25 验收通过**：Docker（Linux）容器内 `lpstat -h` 看到 `HP1020`，上传单页 PDF → 确认 → CUPS 任务完成并真实出纸，临时文件已删 |
| D62 | 文件处理/新需求怎么加能力 | **已定（2026-11-25）**：不为单个需求加工具或参数，只补通用通道——`file_read`（看内容）、`find_tools`（查已装命令与 Python 包）、`run_python`（临时目录跑代码，产出文件接替打印附件）、`run_shell`（bash；`/data/work` 持久、`pip install --user` 到 `/data/home`、非 root）、`web_search` / `web_fetch`（DuckDuckGo + 抓正文，代理复用 `LLM_PROXY`，不抓内网）。转格式、算列、调列宽、找现成工具都由模型现场组合。安全边界：清空子进程环境变量、超时、非 root；**不是沙箱**，信任级别等于「让模型在家里跑脚本」 |
| D63 | 思考模式什么时候开 | **已定（2026-11-25）**：不按入口硬切，交给模型自己判断。每轮默认 `LLM_REASONING_EFFORT`（语音场景 `off`）；模型觉得要多步推理、写代码、算表格、比较方案，就调 `set_thinking(true)`，本轮后续请求改用 `LLM_THINKING_EFFORT`（默认 `medium`，输出上限同时提到 2048），下一轮复位。开关在 `assistant._agent_loop` 里拦，不落 `execute`、不进会话历史。每轮记一行到 `data/thinking.jsonl`（问题 / 开没开 / 原因 / 工具 / 耗时），`GET /api/thinking` 查看与统计，`THINKING_LOG_ENABLED=0` 可关——先观察，不预设结论 |
| D64 | 刚上传的文件要不要按设备隔离 | **已定（2026-11-25）**：**先不隔离，各端共用一份**。当前是单家庭部署，手机传完想在电脑上「再打一份」是正常需求，按 `client_id` 分开只会逼着重传。`Assistant._recent_file` 只留最近一份，任何入口都能接上（时限后来由 D65 定为 `PRINT_REUSE_HOURS`，默认 24 小时）。上多用户/多家庭成员时再按 `space_id`（或人）分开，届时隔离层级跟会话上下文一致 |
| D65 | 打印记录要不要落库 | **已定（2026-11-26）**：**要**。文件从 `/tmp` 挪到 `DATA_DIR/print`（Docker 卷上），元数据进 `prints.db`（`domain/prints.py`）：重启/重建容器后「刚才的文件再打一份」仍然认得，`GET /api/prints` 查得到打过什么、成没成功、CUPS 任务号。文件留 `PRINT_KEEP_DAYS`（默认 7 天），「刚才」的判定窗口是 `PRINT_REUSE_HOURS`（默认 24 小时）；历史记录不删。出纸/失败/取消都写状态；概览页「最近打印」的「再打一份」带原 `file_id` 重走确认，**不新开直接出纸的接口** |
| D66 | facts 怎么关联实体 | **已定（2026-11-26）**：`facts` 加 `entity_id`（老库启动时 `ALTER TABLE` 补列，数据不动），存的仍是用户原话主体，**不改写文本**。`Assistant` 把 `AgentStore.find_any_entity` 注入 `Facts`：存事实时按主体里的规范名/别名最长匹配挂上实体；查事实时先按文本检索，再把「查询里命中的实体」名下的事实并进来（别名等价），剩下没被实体名吃掉的词（如「滤芯型号」）继续筛。画像 `用户/...` 永不挂实体。好处：存「净水器」问「饮水机」也查得到，同一个东西不用记两套说法 |
| D67 | 运维：日志和运行观察 | **已定（2026-11-26）**：容器日志靠 compose 的 `logging`（json-file，10MB × 3 份）轮转，应用本身不写文件日志——进程退出前的最后一段日志留在 stdout 更有用。`/api/status` 和 `/api/stats` 增加 `runtime`：运行时长、核心数据大小、打印目录文件数与体积、磁盘总量/剩余；概览页「运行状态」直接显示。只数顶层库和打印目录，不 walk `work/`、`home/`（几百 MB 的包，走一遍没意义） |
| D68 | 空间隔离怎么落地 | **已定（2026-11-26）**：**一个空间一套库文件（目录级隔离），不往每张表加 `space_id` 列**。理由：家里数据量小；现有约一百条 SQL 逐条加 `WHERE space_id=?` 漏一处就串数据且看不出来；备份/导出/删空间是整目录操作；默认空间继续用现有 `DATA_DIR` 路径，升级无感。`domain/space.py` 负责 `space_id` 规则（小写字母/数字/`_`/`-`，≤32，挡路径穿越）、路径组合、`ensure_space`、`list_spaces`；其它空间落在 `DATA_DIR/spaces/<id>/`。`Assistant` 按 `space_id` 建默认 store；请求级路由（谁用哪个空间）等接多成员再做。`space_id` 不参与鉴权 |
| D69 | RAG 第一批语料从哪来、怎么接 | **已定方向（2026-11-26）**：**家庭文档先交给家庭服务器上独立跑的 Paperless-ngx**（发票/保修单/说明书/体检报告/合同），OCR 用 `chi_sim+eng`、输出 PDF/A；小马这边**只做检索接入**，不改服务器上的部署。分两步：**A（先做）**直接调 `GET /api/documents/?query=…`，把标题+日期+页码+片段交给模型；**B（文档上千份、关键词开始漏召回再上）**按页切 300–500 字，本地 embedding（`bge-small-zh-v1.5` / `bge-m3`，CPU）+ `sqlite-vec`，BM25 与向量混合召回。约定：只做一个能力型工具 `doc_search`；命中 PDF 接现有 `printer_print`，不新开打印通道；**整份 PDF 不进上下文、文档内容不进 `memories`**；接口只在内网/Tailscale 调，Token 走环境变量 |
| D70 | 备份要不要加密、怎么定时 | **已定（2026-11-26）**：本机备份可以是明文 zip（能离线校验、恢复不依赖口令），**一旦离开本机就必须加密**——`--encrypt` 用 AES-256-GCM，密钥由口令经 scrypt 派生，产物 `.zip.enc`，明文 zip 加密后立刻删除；口令只走 `BACKUP_PASSPHRASE` 或 `--passphrase-file`（`chmod 600`），不进命令行历史、不随备份存放。Linux 上用 **systemd timer**（`--keep 14`）而不是 cron 长命令，日志进 journal；单盘部署时备份必须另放一台机器或移动盘（备份与原数据同盘等于没有） |
