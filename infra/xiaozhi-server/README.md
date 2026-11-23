# 小智服务端的提醒桥（方案 A）

让客厅设备**主动说话**。完整方案与小马侧见 [`docs/device-reminders.md`](../../docs/device-reminders.md)；
这里只放需要落到 xiaozhi-server 那台机器上的文件。

```
infra/xiaozhi-server/
├─ reminder_bridge.py           # 复制进镜像：订阅小马 SSE → speak_txt
├─ apply_patch.py               # 构建时打两个钩子，锚点不符就构建失败
├─ Dockerfile                   # 基于官方镜像 + 补丁
└─ docker-compose.example.yml   # 带环境变量的启动方式
```

## 为什么不是「改两份文件然后提交」

`websocket_server.py` 和 `app.py` 随上游频繁变动，整份拷贝会把上游历史一起冻结。
这里只做**锚点替换**：位置长得一样就改，长得不一样就让构建失败——升级后要么两分钟修好，
要么立刻知道，不会静默少一块功能。

## 装法（Windows，现在的联调机）

1. 把本目录全部复制进 `D:\xiaozhi-server\`（和 `data/`、`models/` 同级）
2. 在 `D:\xiaozhi-server\.env` 里写（**不要入库**）：

```
XIAOMA_API_TOKEN=<小马的 API_TOKEN>
```

3. 用示例 compose 覆盖原来的：

```powershell
cd D:\xiaozhi-server
copy docker-compose.example.yml docker-compose.yml
docker compose up -d --build
```

4. 看日志：

```powershell
docker logs -f xiaozhi-esp32-server
```

设备连上后，应该看到：

```
🔔 小马提醒桥启动：http://host.docker.internal:8200
🔔 提醒桥已连接：http://host.docker.internal:8200
```

## 验收

1. 小智数字人测试页连上（它就是一个设备连接）
2. 小马 `/api/status` 的 `device_channels` 变成 1
3. 造一条今天到期的提醒（等一个提醒周期，默认 60s）
4. 测试页念出「提醒：…」；此时网页不弹、手机不推
5. 关掉测试页：`device_channels` 回到 0，提醒桥日志断开，
   下一条提醒回落到网页 SSE 或 Web Push

2026-11-23 实测（用一个保持连接的假设备，协议上与数字人页等价）：

```
初始 device_channels: 0
设备会话已建立: cc433e0b
提醒桥在线 device_channels: 1
已插入今天到期的物品: 联调提醒-可删
tts: sentence_start 提醒
tts: sentence_start 联调提醒-可删 今天到期
tts: stop
收到 tts 消息 3 条、音频包 76 个
清理后（连接关闭）device_channels: 0
```

## 注意

- 小智的 INFO 日志会打印设备握手的 `authorization` 头（含 token）。日志只留在本机，别外发；
  迁移时可以考虑调低日志级别或做脱敏。
- 小智会把 `mcp_endpoint` 里的 `/mcp/` 改写成 `/call/`，小马两条路径都接了，不用管。

## 排障

| 现象 | 检查 |
|---|---|
| `device_channels` 一直是 0 | 小智那边有没有设备连；桥的日志有没有「已连接」 |
| 日志说未配置 | `XIAOMA_BASE_URL` / `XIAOMA_API_TOKEN` 没进容器 |
| 401 | token 和小马 `.env` 的 `API_TOKEN` 不一致 |
| 设备没声音 | 设备是否在说话中（会排队）；`speak_txt` 是否被上游改了签名 |

## 升级

```powershell
docker compose build --no-cache
docker compose up -d
```

构建时 `apply_patch.py` 会重新核对锚点。失败信息会写清楚是哪个文件、哪个位置，
照提示改 `apply_patch.py` 里的锚点即可，不要改成整份拷贝。

## Linux 上

如果 xiaozhi-server 和小马在同一台机器：

```yaml
    network_mode: host
    # 删掉 ports 和 extra_hosts
    environment:
      - XIAOMA_BASE_URL=http://127.0.0.1:8200
```

详见 `docs/server-migration.md`。
