# 设备固件补丁：让提醒随时能到（D78）

上游 `78/xiaozhi-esp32` 是「人先说话、设备才连」的模型：会话一结束、连接就关，
服务端再想主动说话也没门路。反向提醒（D25）在这个前提下只能等用户先喊一声，
那就不叫提醒了。

这个补丁改三处 `main/application.cc`（以及 `application.h` 的声明）：

1. **开机就把连接建起来，然后停在待机**——重启之后不用等人先喊一声。
2. **收到 `{"type":"session","state":"idle"}` 回待机、但不关连接**——待机时麦克风关、
   WebSocket 留着，服务端随时能把语音推下来（`application.cc` 里待机只开本地唤醒词检测，
   不关通道；`tts start` 会强制进 `kDeviceStateSpeaking`，所以待机照样能念）。
3. **断了自动重连**——「说再见」、服务器重启、WiFi 抖一下都会把连接关掉；加一个 5 秒
   重连定时器（失败翻倍退避到 60 秒），只有待机状态才重连，说话/聆听/配网时不去抢。
   没有这一条，说一句「再见」就得等人再喊一声。

配套的服务端改动在 `infra/xiaozhi-server/`：`close_connection_no_voice_time` 调到很大
（不再挂断）+ 新增 `idle_after_voice_time`（安静这么久就下发待机指令）。

## 怎么打

```bash
git clone https://github.com/78/xiaozhi-esp32.git
cd xiaozhi-esp32
git checkout 92d400d          # 补丁就是在这个提交上生成的
git apply /path/to/keepalive.patch
# 板型、AEC、OTA、唤醒词的 menuconfig 见 docs/device-lichuang-s3.md
idf.py build
idf.py -p COM<x> flash
```

刷之前记得先整片备份：`esptool read-flash 0x0 0x1000000 backup.bin`
（2026-11-26 的备份在 `D:\esp\flash-backup\lichuang-s3-2026-11-26.bin`）。

## 现在的行为

- 上电/重启 → 自动建连 → 待机（屏幕表情，麦克风关）
- 说「你好小智」→ 正常对话；回答完 15 秒没声音 → 回待机，连接还在
- 任何时候有提醒 → 直接念（不用先唤醒）
- 说「再见」/ 按按键 / 服务器重启 / WiFi 抖动 → 5 秒后自己接回来，仍是待机
- 只有断电才不会自己回来（重新上电即恢复）
