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

| 环节 | 耗时 |
|---|---|
| ASR（SenseVoiceSmall，本地） | 0.2 - 1.2 s |
| 带工具的一轮 LLM 决策 | 2.0 - 3.5 s |
| 最终回答（流式，首段） | 1.9 s（无工具）/ 6.2 s（工具轮之后） |
| 首句 TTS（端到端） | 7.0 - 8.3 s |

已做的优化：

- `LLM_REASONING_EFFORT=none` 关闭思考模式：单轮 5.1s → 1.9s
- `/v1/chat/completions` 支持流式，首段内容一产生就下发，首句约提前 1 秒

试过但**不可行**的做法：在工具轮之后省略 `tools` 让模型直接作答——历史消息里含
`tool_calls` 时模型会返回空内容（实测 reply 为空），已回退。因此每轮都带工具，
2 秒级开销暂时无法避免。

下一步若要继续压：把「查询类」改成先检索、单轮生成（不再让模型决定调工具），
或者换更快的网关。
