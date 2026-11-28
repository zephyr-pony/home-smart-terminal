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

## 待机看板补丁（D85）

`dashboard.patch` 把待机屏从「一个机器人图标」改成一块暖米色小看板：

```
 09:58                       11月28日 周六
                              农历十月十四
                                还有 1 件
 ────────────────────────────────────────
            阴 6℃ · 体感 1°
         西北风 6级 · 6° / 1°

 取快递                          逾期 3 天
 交电费                          今天 09:00
```

它**在 keepalive 补丁之后**应用，顺序别反：

```bash
git checkout 92d400d
git apply /path/to/keepalive.patch
git apply /path/to/dashboard.patch
```

然后 `idf.py menuconfig` → `Xiaoma Dashboard (D85)`：

- `XIAOMA_DASHBOARD_URL`：`http://<PC-IP>:8208/api/display`，留空则整个功能关闭
- `XIAOMA_DASHBOARD_TOKEN`：小马 `.env` 里的 `DISPLAY_TOKEN`（不是 `API_TOKEN`）

时间想用 46px：`Component config → LVGL → Font usage → Montserrat 46` 打开；不开也能编，
时间退回 20px 正文字体。补丁只改标准 LCD 界面（本板 `CONFIG_USE_EMOTE_MESSAGE_STYLE`
没开），emote 表情模式不在范围内。配色写死在 `lcd_display.cc` 的 `kStandby*` 常量里，
跟网页控制台那套暖米一致；聊天/提醒界面仍用上游主题。

**做自定义界面时别写死旧字体**：LVGL 样式里存的是 `lv_font_t*` 裸指针，开机后资产分区里的
20px 字体会替换主题字体，旧字体（内置 14px）随即释放。自定义 label 如果显式设了旧
`text_font`，下一次布局就会 dereference 悬空指针，表现为开机 `Guru Meditation
(InstrFetchProhibited)`，backtrace 落在 `lv_font_get_glyph_width`。让 label 继承屏幕字体，
或在 `SetTheme()` 里把字体重绑到新主题。

**HTTP 状态码要先收响应头**：`esp_http_client_open()` 之后直接 `esp_http_client_get_status_code()`
拿到的是 0（不是网络错误）；先 `esp_http_client_fetch_headers()`，再读状态码和 body。

## 现在的行为

- 上电/重启 → 自动建连 → 待机（屏幕表情，麦克风关）
- 说「你好小智」→ 正常对话；回答完 15 秒没声音 → 回待机，连接还在
- 任何时候有提醒 → 直接念（不用先唤醒）
- 说「再见」/ 按按键 / 服务器重启 / WiFi 抖动 → 5 秒后自己接回来，仍是待机
- 只有断电才不会自己回来（重新上电即恢复）
