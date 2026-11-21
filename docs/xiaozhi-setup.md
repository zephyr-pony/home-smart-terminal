# 小智（xiaozhi-esp32-server）本地联调

设备还没到之前，先用这条链路把「语音 → 识别 → 小马 → 播报」跑通。
联调环境搭在这台 Windows 上，仓库之外，不参与 git。

## 目录

```
D:\xiaozhi-server\
  ├─ docker-compose.yml
  ├─ data\.config.yaml        # 我们的配置（LLM 指向 127.0.0.1:8200）
  └─ models\SenseVoiceSmall\model.pt
```

`model.pt` 直接从 FunASR 的 modelscope 缓存复制，不用重新下载：

```powershell
copy C:\Users\<你>\.cache\modelscope\hub\iic\SenseVoiceSmall\model.pt D:\xiaozhi-server\models\SenseVoiceSmall\model.pt
```

## 端口

官方 compose 用 `8000`（WebSocket）和 `8003`（OTA/视觉）。这台机器上 8000/8100 落在
Windows 保留端口段，所以映射改成 **8300 → 8000**、**8303 → 8003**。

```yaml
ports:
  - "8300:8000"
  - "8303:8003"
```

## 配置（data/.config.yaml）

```yaml
server:
  ip: 0.0.0.0
  port: 8000
  http_port: 8003
  websocket: ws://<局域网IP>:8300/xiaozhi/v1/   # 设备回来后用这个地址
  timezone_offset: +8

prompt: |
  你叫小马，是这个家的生活助手。说话简短、口语化，像家里人聊天。

selected_module:
  VAD: SileroVAD
  ASR: FunASR
  LLM: ChatGLMLLM     # 名字不重要，关键是下面的 base_url 指向小马
  TTS: EdgeTTS
  Memory: nomem
  Intent: nointent    # 工具调用在小马内部完成，这里不要再跑一遍意图

ASR:
  FunASR:
    language: zh

LLM:
  ChatGLMLLM:
    type: openai
    base_url: http://host.docker.internal:8200/v1
    model_name: xiaoma
    api_key: <API_TOKEN>
```

要点：

- `host.docker.internal` 是容器访问宿主机的入口；`/v1/chat/completions` 由 `server.py` 提供
- `Intent: nointent`，否则会在小马之外再跑一次意图识别，白花一次模型调用
- `ASR.language: zh`，避免短句被 SenseVoice 误判成韩文/日文

## 启动

```powershell
cd D:\xiaozhi-server
docker compose up -d
docker logs -f xiaozhi-esp32-server
```

看到 `server listening on 0.0.0.0:8000` 和 `初始化组件: asr成功 FunASR` 即就绪。

## 端到端探针

`scripts/xiaozhi_probe.py` 用 Edge TTS 合成一句"用户说的话"，转成 16k Opus 按协议推给
xiaozhi-server，再打印识别结果与回答：

```powershell
$env:API_TOKEN = "<你的 token>"
python scripts/xiaozhi_probe.py "明天提醒我交电费"
```

正常输出：

```
上行音频包: 38 个（2.3 秒）
会话建立: 7a7a6c1a-...（下行 16000 Hz）
识别: 明天提醒我交电费
回答: 是哪里的电费？
回答: 比如自己家、爸妈家
耗时: 识别 203 ms | 首句 ... ms | 完成 ... ms
结果: 通过
```

### 两个坑

1. **握手必须带 `device-id`**（以及 `client-id`），否则 xiaozhi-server 直接断开且日志里
   只有一句 `connection open`，很难猜。
2. 官方固件连的是 `xiaozhi.me`，设备回来后要刷开源固件并把 OTA/WebSocket 指向
   `ws://<局域网IP>:8300/xiaozhi/v1/`。

## 实测延迟（2026-11-21）

| 环节 | aisp `DeepSeek-V4.1-Flash` | 官方 `deepseek-flash` |
|---|---|---|
| ASR（SenseVoiceSmall，本地） | 0.2 - 0.3 s | 0.2 - 0.3 s |
| 带工具的一轮决策 | 2.0 - 3.5 s | **0.6 - 1.0 s** |
| 首句 TTS（闲聊） | 8.3 s | **2.8 s** |
| 首句 TTS（带工具查询） | 8.3 s | **5.5 s** |
| 整轮完成（带工具） | 14.0 s | **11.2 s** |

已做的优化，按收益排序：

1. 换官方 `deepseek-flash`（aisp 是转售路由，同样模型慢 2-3 倍）
2. `LLM_REASONING_EFFORT=none` 关闭思考模式（aisp 上单轮 5.1s → 1.9s）
3. `/v1/chat/completions` 流式下发，首段内容一产生就推给语音端

试过但**不可行**：工具轮之后省略 `tools` 让模型直接作答——历史消息里含
`tool_calls` 时模型返回空内容，已回退。

剩下的大头是两轮 LLM（决策 + 组织回答）与 Edge TTS 逐句合成。若还要压，
只能把查询类改成「先检索、单轮生成」。

### 首字延迟的三个变量（2026-11-21 实测，aisp）

| 变量 | 首字（TTFT） | 端到端首句 |
|---|---|---|
| 思考模式 开 | 最多 14 s，波动大 | 14.0 s（查询）/ 5.6 s（闲聊） |
| 思考模式 关（`reasoning_effort=none`） | 1.4 - 3.6 s | 8.6 s / 4.5 s |
| 上下文 819 字（0 轮历史） | 1.7 s | — |
| 上下文 1.7k - 6.3k 字（10 - 60 轮） | 2.6 - 3.6 s | — |

结论：

- **思考模式影响最大**，关掉能省 2-5 秒，且波动明显变小
- **上下文长度有影响但有限**：从 0 轮加到 60 轮，TTFT 只多 1-1.5 秒；把
  `SESSION_CONTEXT_TURNS` 从 5 降到 3 大概省 0.3-0.6 秒，不是主要矛盾
- 官方 `deepseek-flash` 的 TTFT（0.6-1.0 s）比 aisp 转售（1.5-3.6 s）稳定更快，
  同一条查询端到端 5.5 s vs 8.6 s

### 本地模型（2026-11-21 实测）

这台机器上 llama.cpp（`127.0.0.1:8081`）跑 `Qwen3.5-9B-abliterated Q4_K_M`，
硬件是 AMD 780M 核显（无独显），所以基本是 CPU 推理：

| 配置 | 查询端到端 | 闲聊 |
|---|---|---|
| 默认（思考开） | 35 s+ / 首轮 34.9 s | 8.4 s |
| 关思考（`chat_template_kwargs.enable_thinking=false`） | 10.3 s（工具 8.0 + 回答 2.3） | 7.5 s |

吞吐约 9 字/秒，TTFT 4.8-8 s。回答质量和工具调用都正常（会用 `family_search`），
但**不适合语音**：比 aisp 还慢一档。

`llm.py` 已支持 `LLM_CHAT_TEMPLATE_KWARGS`（JSON），llama.cpp / vLLM 可用它关思考：

```
LLM_BASE_URL=http://127.0.0.1:8081/v1
LLM_API_KEY=none
LLM_MODEL=Qwen3.5-9B-abliterated-Q4_K_M_2.gguf
LLM_REASONING_EFFORT=off
LLM_CHAT_TEMPLATE_KWARGS={"enable_thinking": false}
```
