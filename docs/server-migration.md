# 搬到家庭服务器：执行清单

目标机：ThinkPad X13 Gen1 / Ryzen 5 PRO 4650U / 16 GB / 477 GB，装 Ubuntu Server 24.04 LTS。
背景与容量评估见 `docs/ROADMAP.md` §22；**迁移时机**见文末。

原则：开发继续留在现在的 Windows 机器，服务器只做部署；迁移只做一次，不来回搬。

---

## 阶段 0：动手前（Windows 这台，半天）

- [ ] `powercfg /batteryreport` 看电池健康，低于 60% 就别把它当 UPS
- [ ] CrystalDiskInfo 看 SSD 健康（通电时间、重映射扇区）
- [ ] 记下无线网卡型号（Realtek 在 Linux 上要额外驱动；能有线就有线）
- [ ] 确认内存是板载还是可换（16 GB 够现在，跑 7B 不够）
- [ ] **BitLocker 解绑、退出微软账号**，否则盘拆下来读不出
- [ ] 做一份完整备份（见阶段 3 的备份脚本），并验证能恢复
- [ ] 路由器上给小笔记本**预留 DHCP 地址**（别在 netplan 里写死，换网段会失联）

## 阶段 1：装系统（1–2 小时）

- [ ] Ubuntu Server 24.04，最小安装，开 OpenSSH
- [ ] 时区 `Asia/Shanghai`，`timedatectl` 确认
- [ ] 用户加入 `sudo` 与 `docker` 组
- [ ] `ufw`：只放行 22、8200、8123（和以后的 443），**默认拒绝入站**
- [ ] 关合盖休眠：`/etc/systemd/logind.conf` 里
      `HandleLidSwitchExternalPower=ignore`、`HandleLidSwitch=ignore`
- [ ] 关 WiFi 省电（用有线时直接禁用无线）
- [ ] 确认 `homeassistant.local` 解析：`/etc/hosts` 加 `127.0.0.1 homeassistant.local`
      （小米官方集成把 OAuth 回调写死成这个域名，不加会 503）

## 阶段 2：依赖与服务（半天）

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip ffmpeg sqlite3
# CPU 版 torch，别让 pip 拉 CUDA
python3 -m venv .venv
.venv/bin/pip install --index-url https://download.pytorch.org/whl/cpu torch==2.8.0
.venv/bin/pip install -r requirements.txt
```

> 服务端不需要 `pyaudio` / `pygame`（那是 CLI 与浏览器的事），
> 但 `requirements.txt` 里带着；如果 portaudio 装不上，可以临时注释这两项。

- [ ] `git clone` 仓库到 `/opt/xiaoma`（或 `~/xiaoma`），`git checkout` 到发布 tag/commit
- [ ] 拷 `data/`（五个 SQLite + `data/homeassistant/`）与 `.env`
- [ ] `.env` 里把 `SERVER_HOST=0.0.0.0`、`SERVER_PORT=8200`
- [ ] `.env` 里保留原 `API_TOKEN`、`MCP_TOKEN`、`VAPID_*`、`HOME_TOKEN`、LLM key
      —— VAPID 私钥丢了推送订阅全废
- [ ] systemd：`/etc/systemd/system/xiaoma.service`

```ini
[Unit]
Description=Xiaoma family agent
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=xiaoma
WorkingDirectory=/opt/xiaoma
EnvironmentFile=/opt/xiaoma/.env
ExecStart=/opt/xiaoma/.venv/bin/python server.py
Restart=always
RestartSec=5
# SQLite 写坏的风险低（笔记本有电池），但仍建议优雅退出
KillSignal=SIGINT

[Install]
WantedBy=multi-user.target
```

- [ ] HA：`infra/homeassistant/docker-compose.yml` 改成 Linux 的形态

```yaml
    # Linux 上改用 host 网络：mDNS 发现和米家本地协议都更顺
    network_mode: host
    # ports 段删掉
```

- [ ] `docker compose up -d`，`docker compose ps` 里 HA 为 healthy
- [ ] 日志：不要再用 `RedirectStandardOutput` 到文件；用 journald
      `journalctl -u xiaoma -f`。若坚持写文件，配 `logrotate`（每日、保留 7 份）
- [ ] 首次启动会下 FunASR 模型（约 900 MB，在 `~/.cache/modelscope`），等它完成

### xiaozhi-server 也要一起搬

它现在在仓库之外（`D:\xiaozhi-server`，Docker），迁移时容易漏：

- [ ] 拷 `data/.config.yaml`（里面有 `API_TOKEN`、LLM `base_url`、`mcp_endpoint`）
- [ ] `models/SenseVoiceSmall/model.pt` 可以直接从旧机器拷，别重新下
- [ ] Linux 上没有 `host.docker.internal`。两种改法：
      compose 里加 `extra_hosts: ["host.docker.internal:host-gateway"]`，
      或者干脆 `network_mode: host`，把 LLM 地址写成 `http://127.0.0.1:8200/v1`
- [ ] 端口：Windows 上为了绕开保留端口段改成了 8400/8403（该段会随重启变化，见
      `docs/xiaozhi-setup.md`）；Linux 上可以改回 8000/8003，
      也可以保留，**改完要同步设备 OTA 里的 WebSocket 地址**
- [ ] `mcp_endpoint` 改成 `ws://127.0.0.1:8200/mcp_endpoint/mcp/?token=<MCP_TOKEN>`
      （同一台机器时不用走局域网 IP）
- [ ] `mcp_forward_speaker: true` 保持开启，否则声纹身份不会带过来

## 阶段 3：数据与配置迁移

- [ ] 停旧服务，做一致性快照（运行中直接 cp SQLite 可能拷到半写状态）：

```bash
for db in data/*.db; do sqlite3 "$db" ".backup '/backup/$(basename "$db")'"; done
cp .env /backup/
tar czf /backup/homeassistant-$(date +%F).tgz -C data homeassistant
```

- [ ] 新机器上恢复 `data/` 与 `.env`
- [ ] `.env` 的 `LLM_PROXY` 是 Windows 本机代理地址（`127.0.0.1:10808`），Linux 上按新环境
      改或留空——直连网关正常就不要配，配错会绕远
- [ ] 核对数量：`/api/stats` 的 memories / items / tasks / facts / entities
      与旧机器一致（facts 的 `valid_to` 迁移会在启动时自动做）
- [ ] `pytest tests -q` 在新机器上跑一遍（Linux 与 Windows 的路径差异只有这里能发现）

## 阶段 4：网络与地址（最容易漏的一节）

| 依赖 | 旧值 | 新值要改哪里 |
|---|---|---|
| 浏览器访问 | `http://127.0.0.1:8200` | 手机/平板用 `https://<新地址>` |
| Web Push 订阅 | 绑定旧「源」 | **全部失效，每台设备重新开启通知** |
| PWA 安装 | 绑定旧「源」 | 重新「添加到主屏」 |
| localStorage 聊天记录 | 绑定旧「源」 | 不会跟过去（可接受；要保留就导出） |
| 小智 MCP 地址 | `ws://<旧 IP>:8200/mcp_endpoint/mcp/?token=…` | xiaozhi-server 配置 |
| 小智服务端自身 | `D:\xiaozhi-server`（8000/8003 或 8400/8403） | 新机器 compose + `.config.yaml` |
| 摄像头视觉地址 | `server.vision_explain=http://<旧 IP>:8403/mcp/vision/explain` | `.config.yaml`（必须是**设备能访问到**的新 LAN IP） |
| 设备固件 OTA/WS 地址 | 旧 IP | 小智 OTA 返回的地址（本机部署时用新 IP） |
| HA 回调 | `homeassistant.local` | 新机器的 `/etc/hosts` |
| SSE / API 调用方 | 旧 IP | `docs/xiaozhi-mcp.md` 里的地址 |

- [ ] 先在同一局域网用 `curl http://<IP>:8200/api/status -H "Authorization: Bearer …"` 验证
- [ ] 再上 HTTPS（见下），因为推送和 PWA 只在安全上下文可用

## 阶段 5：HTTPS（2026-11-23 已落地：Tailscale Serve）

三条路，按「家里人能不能接受装证书/装 VPN」选：

| 方案 | 做法 | 代价 |
|---|---|---|
| Caddy + 本地 CA | 本地 DNS 名（如 `xiaoma.lan`）+ Caddy `tls internal`；每台手机装一次根证书 | 一次性的证书安装；名字要能被局域网解析 |
| **Tailscale Serve（本机现状）** | 手机装 Tailscale，访问 `https://win-od68q9v0uct.tail8025f9.ts.net` | 依赖 VPN；推送本身仍由浏览器推送服务送达 |
| Cloudflare Tunnel | 公网域名 + Access 策略 | 数据出入口，隐私上要多想一步 |

现状（2026-11-23 实测）：

```
tailscale serve status
https://win-od68q9v0uct.tail8025f9.ts.net (tailnet only)
|-- / proxy http://127.0.0.1:8200
```

- 只对 tailnet 可见，**没有开 Funnel**，公网访问不到
- 经这条 HTTPS 验过：`/api/status` 200、首页 200（含 manifest 与 SW 注册）、`/sw.js`
  `Content-Type=application/javascript` + `no-cache`、`/api/events` 立刻收到 `: connected`
  （Tailscale 不缓冲 SSE 流）
- 服务端仍只监听 `127.0.0.1`：Serve 从本机反代，不需要把 8200 暴露到局域网

手机侧要手动做一次（换源就等于换订阅）——**2026-11-23 已由用户实测全部通过**：

- [x] 手机装 Tailscale，登录同一账号，确认与主机在同一 tailnet
- [x] 用 `https://win-od68q9v0uct.tail8025f9.ts.net` 打开小马 →「添加到主屏」
- [x] 从主屏图标进入 → 设置 →「开启通知」（订阅是按源存的，旧的 `http://` 订阅作废）
- [x] 锁屏造一条到期提醒，确认能收到；点通知能打开并显示原文

注意：

- 通知由浏览器推送服务送达，**不需要 VPN 常开也能收到**；但点开应用时要能连上 tailnet，
  否则页面打不开（这是 VPN 方案的固有代价）
- 主机名变了（重命名/换机）等于换源：推送订阅与 PWA 安装都要重来一遍

- [x] 反代只把 `/`、`/api/*`、`/mcp_endpoint/*` 暴露出去
- [ ] 不要把 8123（HA）暴露到公网；HA 自己也要设密码
- [x] 换域名/换协议后，再让每台手机重新订阅推送

## 阶段 6：功能验收（逐项打勾）

- [ ] `/api/status`：`stt_ready=true`，计数正确
- [ ] 网页聊天：记一条、查一条、删一条（走确认）
- [ ] 提醒：造一条今天到期 → 网页在线时 SSE 弹卡片并播报
- [ ] 推送：手机锁屏能收到（**仅 HTTPS 下有效**）
- [ ] HA：`python scripts/ha_check.py`；空调状态、灯开关回读一致
- [ ] 事件订阅：日志里不再出现 `事件订阅断开`
- [ ] MCP：`python scripts/mcp_probe.py --token … --speaker-id test`
- [ ] 故障演练：拔网线 30 秒，恢复后事件订阅与首页都自动重连
- [ ] 重启演练：`reboot` 后 `systemctl status xiaoma` 自动起来

## 阶段 7：备份与回滚

- [x] 备份脚本：`scripts/backup.py`（sqlite 热备 + sha256 + integrity_check，保留 7 份），
      用法与演练记录见 `docs/backup.md`
- [ ] 每日定时跑一次（Windows `schtasks` / Linux `cron`，命令在 `docs/backup.md`）
- [x] 每月：从备份恢复到临时目录并打开一次——2026-11-23 已演练，逐项计数一致
- [ ] 异地存一份（手动拷到手机/网盘；zip 里有 `.env`，别放公共网盘）
- [ ] 回滚：旧机器保持可启动；把路由器 DHCP 预留改回、旧服务 `start.bat` 起来；
      数据用最近备份覆盖

## 迁移时机

等这几件不再变动再搬：

1. 反向提醒（方案 A，`docs/device-reminders.md`）落地
2. HTTPS 方案选定（否则手机推送搬过去也是断的）
3. ESP32 的地址/唤醒词等固件侧参数定下来

现在搬，上面这些东西都要改两遍。
