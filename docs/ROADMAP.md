# 家庭生活 Agent 开发计划

> 状态：规划稿
> 目标：从「家庭记忆 + 物品提醒」演进为「能理解、会追问、可执行」的家庭生活 Agent。

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
- 不做全屋智能的完整设备控制，先预留 MQTT/Home Assistant 接口。
- 不做复杂视觉能力，先保留图片输入扩展点。

## 2. 现状与差距

### 已有

- Python + FastAPI + SQLite，本地运行。
- 中文语音：FunASR 识别、Edge TTS 合成、CLI 唤醒词「小马」。
- Agent loop + function calling：记忆、物品、日期共 9 个工具。
- 结构化物品与到期提醒：到期状态、提醒冷却、SSE 主动推送。
- Web 单页客户端、CLI、pytest 测试。

### 主要差距

- 记忆是「原文 + 摘要」，没有实体、别名、事实、任务模型。
- LLM 直接写库，没有「提案 → 解析 → 澄清 → 提交」流程。
- 没有澄清状态机，遇到「电费」「牛奶」这类歧义只能猜。
- 没有 RAG，也没有地图/公交/天气等实时工具。
- 会话只在内存里，单进程，没有多用户/多设备。
- 没有评测集、tracing、工具审计。

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

## 5. 数据模型（草案）

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
  id TEXT PRIMARY KEY,
  space_id TEXT NOT NULL,
  subject_id TEXT,
  predicate TEXT,
  value TEXT,
  valid_from TEXT,
  valid_to TEXT,
  confidence REAL,
  source TEXT,
  created_at TEXT
);

tasks(
  id TEXT PRIMARY KEY,
  space_id TEXT NOT NULL,
  type TEXT,
  title TEXT,
  entity_id TEXT,
  due_at TEXT,
  status TEXT,
  payload TEXT,
  created_at TEXT
);

pending_actions(
  id TEXT PRIMARY KEY,
  space_id TEXT NOT NULL,
  conversation_id TEXT,
  action_type TEXT,
  payload TEXT,
  missing_slots TEXT,
  candidates TEXT,
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

当前进展（2026-11-05）：已落地 `entities` / `pending_actions` / `proposals`、电费/牛奶澄清、`ProposalService` 幂等写入、CLI 关联；测试 59 个通过。下一步把澄清规则和实体管理做通用，再进入 RAG。

### 阶段 0：基础重构

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

状态：基础版已落地（电费/牛奶 + CLI + 提案/幂等写入）。

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

目标：新增生活领域不需要改核心循环。

任务：

- 实体别名、属性、家庭成员、供应商、产品。
- `facts` / `tasks` 通用化。
- 提醒从「物品到期」扩展到账单、日程、自定义任务。
- 旧 `items` / `memories` 迁移。

验收：

- 新增一个领域（如「物业费」）主要靠配置/数据，不改 Agent 核心。
- 旧数据可迁移，迁移脚本可重复执行。

### 阶段 3：RAG 与本地生活

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

- **歧义测试集**：电费、牛奶、父母家、同名物品、指代（那个/它）。
- **工具测试**：超时、限流、错误降级、幂等。
- **RAG 评测**：召回率、引用正确率、时效判断。
- **回归测试**：现有测试全部通过（当前 59 个）。
- **端到端场景**：Web/小程序/ESP32 各一条主链路。
- **指标**：澄清率、误提交率、槽位准确率、平均延迟、单次成本。

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
| D2 | 首个终端 | 当前 CLI 优先；Web 保留；小程序/App/ESP32 延后 |
| D3 | RAG 来源 | 采用默认：官方公告/物业/学校；阶段 3 前补具体清单 |
| D4 | 地图/天气 | 采用默认：高德；阶段 3 前补 API Key |
| D5 | 多家庭 | 先单家庭，数据模型带 `space_id` |
| D6 | 部署 | 本地优先 |

### 可延后

| 编号 | 决策 | 建议 |
|---|---|---|
| D7 | Agent 框架 | 阶段 0–2 自研，阶段 3 后评估 LangGraph/PydanticAI/Hermes |
| D8 | 数据库升级 | 继续 SQLite + sqlite-vec，多用户再上 PostgreSQL |
| D9 | 语音链路 | 本地 FunASR/Edge TTS 为主，云端作为可选降级 |
| D10 | 提醒渠道 | 先站内 + Web，后续接微信/短信/App 推送 |
| D11 | 是否引入 Hermes/OpenClaw | 优先作为多渠道前端，通过 MCP 接领域服务 |

## 11. 风险

- **过度追问**：用户会烦；只问关键槽位，给候选，允许默认。
- **RAG 时效/合规**：政策类内容会过期，抓取也可能有版权/稳定性问题。
- **工具幻觉**：模型可能编造路线/政策；必须带出处，工具失败要明说。
- **多终端权限**：父母家、孩子设备的数据不能混。
- **成本与延迟**：多轮澄清 + RAG + TTS 会放大延迟和费用，需要预算和缓存。
- **数据迁移**：旧 `memories` / `items` 要平滑迁移，避免丢数据。

## 12. 下一步

阶段 0 + 阶段 1 的基础版已经落地，写操作也已统一走 `ProposalService`。接下来：

1. 通用化澄清规则：水费/燃气费/物业费/暖气费、实体别名、候选管理。
2. 高风险动作确认：删除/付款/通知他人走确认流程，并记录审计。
3. 扩充歧义测试集：指代、同名物品、父母家、多个账单。
4. 再进入阶段 2（`facts` / `tasks` 通用化），然后才是 RAG 和多终端。
