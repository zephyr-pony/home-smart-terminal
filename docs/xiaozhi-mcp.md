# 小智 MCP 接入点：方向、协议与 `_meta` 透传

本文记录「小马作为 MCP 服务端」的实际实现依据。结论先写：

- **小智服务端是 MCP 客户端，我们是被连的一方。** 小智出站 `WebSocket` 连到
  `ws://<小马>/mcp_endpoint/mcp/?token=...`，不需要公网入站。
- 传输是**裸 JSON-RPC 2.0**，不是 Streamable HTTP；没有用官方 MCP SDK，按它的
  源码实现它实际用到的四个方法即可。
- 声纹身份放在 `tools/call` 的 `params._meta`：`{"speakerId": "...", "speakerName": "..."}`，
  未命中时整个 `_meta` 不出现。**已实测能够到达我们的工具函数**。

## 协议形状（以小智源码为准）

来自 `xinnan-tech/xiaozhi-esp32-server`：

| 文件 | 作用 |
|---|---|
| `core/providers/tools/mcp_endpoint/mcp_endpoint_handler.py` | 连接、握手、`tools/list`、`tools/call` |
| `core/providers/tools/mcp_endpoint/mcp_speaker.py` | 把声纹身份拼进 `params._meta` |

握手细节：

- `initialize` 的 id 固定为 `1`，`protocolVersion` 是 `2024-11-05`
- `tools/list` 的 id 固定为 `2`
- `tools/call` 从 id `3` 开始递增（不能用 1/2，会撞）
- 工具返回值只读 `result.content[0].text`；`result.isError` 为 `true` 时抛错
- `notifications/initialized` 是通知，没有 id，不回包

`tools/call` 的实际报文：

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "tools/call",
  "params": {
    "name": "ask_xiaoma",
    "arguments": { "text": "明天提醒我交电费" },
    "_meta": { "speakerId": "spk-dad", "speakerName": "爸爸" }
  }
}
```

## 小马这一侧

`adapters/mcp_endpoint.py`：

- 一个工具 `ask_xiaoma(text)`，交给 `assistant.process_text`
- `speaker_from_meta(params["_meta"])` 取出身份；同时记 `last_speaker` 便于探针/测试核对
- 身份只用来**分会话上下文**（`client_id = "mcp:<speakerId>"`，没有就 `mcp:device`），
  不参与鉴权，也绕不过写操作护栏
- 没有 `MCP_TOKEN` 时整个接入点关闭（这是一个能写库的入口，不能默认对外）

`server.py` 的路由：

```
@app.websocket("/mcp_endpoint/mcp/")
```

鉴权用查询参数 `?token=`（小智的连接 URL 就带 token，WebSocket 也不好加 Bearer 头）。

## 配置

`.env`：

```
MCP_TOKEN=<随机串>
```

服务端必须能被小智所在机器访问，所以通常还要：

```
SERVER_HOST=0.0.0.0
API_TOKEN=<另一个随机串>
```

小智服务端（`xiaozhi-server`）的 `data/.config.yaml` 或管理台里把 MCP 接入点地址填成：

```
ws://<小马所在机器 IP>:8200/mcp_endpoint/mcp/?token=<MCP_TOKEN>
```

## 探针

不需要 ESP32，也不需要小智服务端：探针按小智的报文形状发一遍。

```bash
python scripts/mcp_probe.py --token <MCP_TOKEN> \
    --text "你好" --speaker-id spk-dad --speaker-name 爸爸
```

服务端日志应出现：

```
🔊 MCP 声纹身份：speakerId=spk-dad name=爸爸
```

2026-11-23 实测（真实服务 + 真模型）：

```
① initialize → {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {...}}
② tools/list → ask_xiaoma
③ tools/call → 你好呀！我是小马，家里的事都可以交给我记～
服务端日志：🔊 MCP 声纹身份：speakerId=spk-dad name=爸爸
```

不带 `_meta` 时日志没有这一行，会话落在 `mcp:device`；token 错误时 WebSocket 握手 403。

## 剩下的

- `speakerId → persons` 的归属还没做（D40）：现在只拿它分会话，不认「这是谁」。
  在 `persons` 建好之前，`speakerName` 只用于日志，不进提示词。
- 小智的 speakers 配置里那段「描述」不会随 `_meta` 过来，人物档案要在小马侧另存。
- 真机（ESP32）与 xiaozhi-server 的端到端延迟还没量；MCP 这一层只加一次进程内调用。
