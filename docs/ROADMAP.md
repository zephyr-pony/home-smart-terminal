# 家庭生活 Agent 开发计划

> 状态：规划稿
> 目标：从「家庭记忆 + 物品提醒」演进为「能理解、会追问、可执行」的家庭生活 Agent。
>
> 当前优先级（2026-11-07）：Web 控制台 → Android App → RAG / 本地生活工具（延后）→ ESP32。

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

当前进展（2026-11-19）：已落地 `entities` / `pending_actions` / `proposals` / `tasks` / `facts`、通用澄清规则、实体管理、任务与提醒扩展、提案幂等、删除确认、Web 控制台（聊天 / 统计 / 列表 / 搜索筛选 / 分页 / 任务编辑 / 配置）、旧数据迁移脚本；测试 113 个通过。下一步打磨 Web 语音入口，随后启动 Android App。

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

### 阶段 2.5：Web 控制台（当前优先）

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
- **回归测试**：现有测试全部通过（当前 113 个）。
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
| D12 | `items` 是否退休、改由 `tasks` 接管物品到期 | 暂不退休：两表并行会重复提醒；等 `tasks` 能完整表达物品语义（数量/位置/品牌）后再迁移 |

## 11. 风险

- **过度追问**：用户会烦；只问关键槽位，给候选，允许默认。
- **RAG 时效/合规**：政策类内容会过期，抓取也可能有版权/稳定性问题。
- **工具幻觉**：模型可能编造路线/政策；必须带出处，工具失败要明说。
- **多终端权限**：父母家、孩子设备的数据不能混。
- **成本与延迟**：多轮澄清 + RAG + TTS 会放大延迟和费用，需要预算和缓存。
- **数据迁移**：旧 `memories` / `items` 要平滑迁移，避免丢数据。

## 12. 下一步

阶段 0 + 阶段 1 + 阶段 2 + Web 控制台已经落地（含旧数据迁移脚本）。接下来：

1. Android App：复用同一套 API，先做聊天 + 任务 + 提醒。
2. Web 继续打磨：语音入口、空状态/错误提示、移动端细节。
3. RAG / 本地生活工具延后，等 Web/App 稳定后再接官方来源和高德 API Key。
4. 事实与实体关联：`facts.subject` 升级为实体 ID，支持属性查询。

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
- 已安装 Tailscale 1.104.7；登录与 `tailscale serve` 待账号授权后执行。

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
- 发送用 `asyncio.to_thread` + `gather`，单条 10s 超时；不能阻塞提醒循环。
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
- 测试：`tests/test_push.py` 8 条 + `tests/test_server.py` 推送接口 7 条，共 132 passed。

待做：

- `pushsubscriptionchange`：iOS 静默轮换订阅时自动回传。当前只能靠用户重新开关一次。
- 真机验收：关闭 App 后等一条真实到期提醒（需先造一条明天到期的物品）。
