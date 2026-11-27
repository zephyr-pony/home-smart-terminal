# 反向提醒：服务端 → 设备（方案 A）

> 决策见 `docs/ROADMAP.md` D42；背景是 MCP 只能设备→小马，反向不通。

## 目标与边界

让客厅设备**主动说话**（到期提醒、故障、事件），复用小智已经调好的 VAD / 流式 / 打断。

- 不改固件，不重写语音链路
- 只 fork `xiaozhi-server`，在它「说话」的既有入口上开一个口子
- 同一提醒只走一条通道：**设备在线 → 网页在线 → Web Push**
- 不做：设备定向选择（多台同时响）、送达确认（ack）、离线补投

## 进度（2026-11-23）

- [x] **小马侧设备通道**：`/api/events?role=device`、优先级 设备 → 网页 → push、
      设备事件不带 mp3、`/api/status` 暴露 `device_channels`。真实服务验过 1 → 0
- [x] **小智侧补丁**：`infra/xiaozhi-server/`（桥接模块 + 构建时锚点替换 + Dockerfile）。
      已构建镜像并跑在 `D:\xiaozhi-server`，日志出现 `🔔 小马提醒桥启动`
- [x] **端到端验收**：用一个保持连接的假设备（协议上与数字人页等价）验过：
      插入今天到期的物品 → `device_channels=1` → 收到
      `tts sentence_start 提醒 / 联调提醒 今天到期 / stop` + 76 个音频包；
      断开后 `device_channels` 回到 0
- 测试：`tests/test_server.py`（优先级/回落/通道）与 `tests/test_reminder_bridge.py`（排队/过期/多设备）

## 数据流

```
提醒到期
  └─ 小马提醒循环（server.py `_scan_once`）
       ├─ 有小智设备订阅？ ──▶ SSE `/api/events?role=device` ──▶ 小智补丁
       │                                                     ├─ 选一台空闲连接
       │                                                     └─ speak_txt(conn, 文本)
       │                                                          └─ tts/start + Opus + tts/stop ──▶ 设备
       ├─ 有网页订阅？ ──▶ 现有 SSE（带 mp3）
       └─ 都没有 ──▶ Web Push
```

## 小马侧改动清单

| 文件 | 改动 |
|---|---|
| `server.py` | `_device_subscribers` 集合；`/api/events` 增加 `role=device` 参数，加入设备集合 |
| `server.py` | `_notify` 按「设备 → 网页 → push」顺序投递；设备事件**不合成 mp3**（设备自己 TTS） |
| `server.py` | `_has_receivers` 把设备订阅算作在线，否则提醒循环不会领取 |
| `server.py` | `/api/status` 增加 `device_channels` 数量，便于排障 |
| `tests/test_server.py` | 设备在线时只投设备；设备断开后回落到网页 / push；`role=device` 的事件没有 `audio` 字段 |

接口契约（复用现有 SSE）：

```
GET /api/events?role=device
Authorization: Bearer <API_TOKEN>

data: {"type":"reminder","reply":"提醒：牛奶今天到期","tag":"item-7", ...}
```

`reply` 就是要念的整句；设备端不需要 `item/task` 结构，但保留原字段不影响。

## 小智服务端改动清单（fork）

新增文件：

| 文件 | 内容 |
|---|---|
| `core/reminder_bridge.py` | `DeviceRegistry`（device_id → conn）+ `ReminderBridge`（订阅 SSE、选空闲设备、调 `speak_txt`、忙时排队） |

修改文件（三处钩子）：

| 文件 | 位置 | 改动 |
|---|---|---|
| `core/websocket_server.py` | `_handle_connection` 建好 `handler` 后 | `self.devices[device_id] = handler`，`finally` 里移除 |
| `app.py` | 启动 `WebSocketServer` 后 | `asyncio.create_task(ReminderBridge(config, server.devices).run())` |
| `plugins_func/register.py` 或 `intentHandler` | 不需要改；`speak_txt` 直接 import | — |

仓库里已经落成 `infra/xiaozhi-server/`：桥接模块、**构建时锚点替换**（不存整份改过的上游文件）、
Dockerfile 与 compose 示例。锚点对不上会直接构建失败，升级后不会静默少功能；装法见该目录 README。

调用前要按现有轮次初始化（照 `intentHandler` 的做法）：

```python
conn.sentence_id = uuid.uuid4().hex
conn.client_abort = False
speak_txt(conn, text)     # 走它自己的 TTS 队列，不用碰 Opus
```

空闲判定与排队：

- 忙：`conn.client_is_speaking` 为真，或 `conn.tts.tts_text_queue` 非空 → 入内存队列（上限 20 条）
- 每 2 秒重试一次；排队超过 10 分钟丢弃并记录日志
- 没有任何设备连接时**断开 SSE**，让小马自动回落到网页 / push（不要让小马以为设备在线）

配置（小智 `config.yaml`）：

```yaml
reminders:
  xiaoma_base_url: http://<小马 IP>:8200
  api_token: <小马 API_TOKEN>
  device_id: <可选：只推给这台设备>
```

## 验收（没有 ESP32 也能做）

小智的数字人测试页本身就是一个设备连接（带 `device-id` 的 WebSocket），可以直接收 `tts`。

1. 起动小马与 xiaozhi-server，打开数字人测试页
2. 注册表应有 1 台设备；`/api/status` 的 `device_channels` 为 1
3. 造一条今天到期的物品（`POST /api/chat`：`牛奶今天过期`）
4. 等一个提醒周期（默认 60s）：设备念出「提醒：牛奶今天到期」
5. 同时检查：网页没收到这条、手机没收到 push、`reminded_at` 已写
6. 关掉测试页，再造一条：应回落到网页 SSE（页面在线）或 push

## 已知风险

| 风险 | 处理 |
|---|---|
| `speak_txt` 与 `sentence_id` / `tts` 状态耦合，属内部 API | 只调一个入口；升级后先跑上面的验收 |
| 上游 `_handle_connection` 结构变化 | 钩子只有 3 行，冲突可手工解决；必要时把能力做成通用接口提 PR |
| 提醒在设备上播放失败但已领取 | 现阶段接受（与 push 失败一致），日志留痕；要做可靠送达再加 ack |
| 多台设备同时在线 | 现阶段全部播报；定向播放等有多房间需求再做 |
| 设备正在对话时来提醒 | 排队，不打断；打断策略属于产品决策 |
| 小智 INFO 日志会打印设备握手的 `authorization` 头（含 token） | 日志只在本机、别外发；迁移时可调低日志级别或做脱敏 |
| 小智会把配置里的 `/mcp/` 改写成 `/call/` | 小马两条路径都接了：`/mcp_endpoint/mcp/` 与 `/mcp_endpoint/call/` |

## 软提醒：顺口说，而不是到点播（D81，2026-11-27 实现）

现在的提醒是「到点 → 一句话 → 念一遍」，只是**那句话由模型组织**（拿事实说人话，
失败或没说对名字才回落模板，2026-11-27 起），不管人在说什么。用户要的是另一种：
**牛奶快过期这种事，应该在聊天里自然带出来**（「对了，冰箱里的牛奶应该要过期了，
你喝了没」），然后由人当场定「喝了」还是「再放两天」。喂到模型嘴里的是事实，
话由它组织——和 D53 一致。

### 前提：得先分「软 / 硬」

不是所有提醒都适合等话口。吃药、出门、开会前几分钟，晚说就等于没说。

- `tasks` 加一列 `mode`：`timely`（准点，现有行为）/ `soft`（等话口）。
- **软硬由模型判**（2026-11-27 定）：`task_add` / `task_update` 的 `mode` 参数，工具描述里
  写清分界（吃药、出门、开会前几分钟 → timely；牛奶过期、取快递 → soft），不写死类型表。
  用户一句话就能改口（「这个不用准点提醒」）。老数据迁移后默认 `timely`（行为不变）。
- 周期任务默认 `timely`——每天八点吃药这类本来就要准点。

### 主路径：把当天挂着的软提醒塞进对话上下文

同 D58 的手法，**不是新工具**（多一个工具就多一轮 LLM，语音链路等不起）：

1. 每轮开始，查「今天到期 + 已过期未完成」的软提醒（最多 3–5 条），拼成一条
   system 消息，**放在会话历史之后、用户这句话之前**——别塞进 system prompt 开头，
   否则每轮变化会让前缀缓存全失效（D58 踩过）。
2. 内容只有事实：`#14 交电费，11月26日到期，逾期 1 天`，日期用中文格式；带上 id，
   用户说「完成了 / 明天再说」时不用再 `task_list`。
3. 跟一句语气要求，不给路由：**「聊到相关话题时顺口带一句，问一句要不要办掉或往后放；
   没聊到就别硬提」**。重复靠会话历史自然收敛——上一轮已经提过，模型看得到。
4. 用户回答走现有工具：`task_complete` / `task_postpone` / `task_cancel`，
   参数由 `agent/guard.py` 按原话校正（「明天」是目标日期，不是 +1 天）。

物品（`items`）的到期也走同一条注入口，不另开一套。

### 兜底：晚上还没说，就落到准点一次

「今天没机会说」必须有人记账，否则软提醒会静默消失。

- 加配置 `SOFT_REMINDER_LAST_CALL`（默认 20:30）：在此之前，`mode=soft` 的任务
  **不进** `claim_due_reminders`；到点之后才按原逻辑领取、走设备 → 网页 → push。
- 还没有「到底说没说」的可靠判据。**v1 接受可能重复一次**（白天提过、晚上又提一句），
  只有一条，不至于吵；真要收，再按「今天这条有没有在回复里出现过」做标题匹配，
  那是锦上添花，先不做。
- **兜底那句也交给模型组织**（2026-11-27 定）：领取时拿任务事实（标题、到期/逾期天数、
  现在几点）让它说一句人话（「对了，冰箱那盒酸奶今天到期，还喝吗」），不是模板句。
  这是一次**不带工具**的小调用；超时或失败就回落 `agent/wording.py` 的模板——
  兜底的第一要求是「一定能发出去」，措辞是第二位。成本：每条软提醒每天最多多一次调用。

### 三个坑

- **设备待机时麦克风是关的**（D78）：用户不开口就没有「对话中」，软提醒只能等下一次
  聊天。这是取舍，不是本方案能解决的。
- **注入 ≠ 一定会说**：模型判断「这会儿不合适」就不提，这是特性；但不能因此丢掉，
  所以有晚间兜底。
- **别用关键词触发**（提到「牛奶」才注入）：那是路由表，D56 明确不要。

### 实施清单（2026-11-27 已完成）

1. `domain/tasks.py`：`mode` 列（沿用 `ALTER TABLE ... ADD COLUMN` 的老套路）、
   `get_soft_due()`
2. `tools.py`：`task_add` / `task_update` 增加 `mode`，描述写软硬分界
3. `assistant.py`：`_soft_reminder_note()` + 注入点（仿 `_task_context_note`）
4. `server.py`：`claim_due_reminders` 拆两段（timely 一直跑；soft 到点后才跑）；
   soft 的文案复用 `reminder_sentence(..., soft=True)`——无工具的小调用，失败回落 `wording`
5. `config.py` / `.env.example` / README 配置表：`SOFT_REMINDER_LAST_CALL`
6. 测试：mode 过滤、注入内容与预算、晚间门控、没聊到仍会兜底

### 实现时定下的细节（2026-11-27）

- **物品到期也算 soft**：`items` 没有 mode 列，也不该有——物品只有一个日期，没有
  「到点」可言。`_scan_once` 里物品和 soft 任务同一道门：`SOFT_REMINDER_LAST_CALL` 之前
  一律不领取。代价是物品提醒从「当天任意时刻」挪到 20:30 之后，但牛奶、酸奶这种事
  本来就不该在上午十点突然播报。
- **注入上限 4 条、物品过期超过 7 天不再提**：`SOFT_NOTE_LIMIT` /
  `SOFT_NOTE_ITEM_OVERDUE_DAYS`。留下来的旧账（没人删的过期牛奶）每轮都在耳边念，
  比不提更烦；任务不设这个上限——欠了十天的账单还要提。
- **注入位置**在会话历史之后、用户这句话之前（`_agent_loop`），并且不进会话历史、
  不进 `_hot_context`：每轮会变，塞进 system prompt 开头会让前缀缓存全部失效（D58）。
- **兜底语气单独一套**：`Assistant.SOFT_REMINDER_PROMPT`（「像忽然想起来顺口提一句」），
  和准点提醒的 prompt 分开；失败、超长、没说对名字仍回落 `wording` 模板。
- **改口走 `task_update` 的 mode**：用户说「这个不用准点提醒」，模型改一行即可；
  `wording.summary` 对 soft 的回显会补「聊天时顺口提」，免得用户以为会响铃。

### 离线预演（2026-11-27，不等设备）

临时空间 `softtest`（`X-Space-Id`，不碰默认数据）造两条 soft 任务（今天到期 / 逾期 2 天）、
一条 timely 任务（今晚 20:00）、一盒昨天过期的牛奶，用真模型走 `/api/ask` 五轮：

- 问温度（不相关）→ 不提软提醒；
- 问快递 → 「今天到期，还没办，要现在去取还是往后挪一天？」不调工具；
- 问牛奶 → 调 `item_search`，答对过期日，主动问要不要删；
- 「快递我拿回来了」→ `task_complete`，顺带提另一件；
- 「今天还有什么要弄的吗」→ 只报剩下的。

`牛奶我扔了` 走确认后才删；`_soft_window_open()` 20:30 前后分界正确。
timely 的「吃药」全程没被塞进上下文。

一条摩擦：用户对账单类软提醒回「物业费明天再说」时，会先被 `Clarifier` 问「是哪里的
物业费」。软提醒关闭动作本来带着 task id，这一问是多余的（要改得动关键词规则，先不动）。
细节见 `docs/changes-2026-11.md` 四十四。

## 以后要不要走 B

只有当「少一个进程 / 完全掌控延迟 / 不要上游依赖」变成硬需求时，才考虑设备直连小马。
那时要做的是**服务端**语音主循环（VAD、ASR 流式、TTS 流控、打断、OTA），不是写固件——
现在的改动清单仍然有用：`speak_txt` 那段就是最小可行的「让设备说话」语义。
