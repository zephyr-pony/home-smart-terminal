# 搬到家庭服务器（HPE Gen10 Plus）：执行清单

目标机：**HPE ProLiant MicroServer Gen10 Plus**——Xeon E-2224（4C/4T）、48GB DDR4 ECC、
1TB SSD（系统）、4 × 1GbE（Intel i350-AM4）、4 个 3.5" 盘位、1 个 PCIe 3.0 x16 半高槽、
180W 外置电源、**无核显**；12TB HDD 待装（`/srv`）。

系统：**Debian 12 + Docker Compose**（不为了这点负载上 Proxmox；4C/4T 开虚拟机反而浪费）。
原则：开发继续留在 Windows 这台，服务器只做部署；迁移只做一次。

> 存储与服务规划另记在本机笔记 `docs/home-server.local.md`（已 gitignore，不提交）。

---

## 阶段 0：动手前（新机器到位后，1 小时）

- [ ] 开机进 iLO（如有 Enablement Kit）：设静态 IP、改默认密码、升级固件
      （iLO 是可选项，没有就用显示器 + USB 键盘装系统）
- [ ] BIOS：确认 ECC 已识别（`Memory` 页应显示 48GB ECC）、VT-d 开着、启动顺序 U 盘优先
- [ ] 装盘：SSD 放 1 号位；12TB 先不插（等系统装完再挂，免得分区选错）
- [ ] 记下 4 个网口的 MAC/编号，规划用途：

| 网口 | 用途 |
|---|---|
| 1 | 家庭 LAN（主用，给 `192.168.1.x` 预留 DHCP 地址） |
| 2 | 预留：软路由 WAN 或 2.5G 网卡（若加卡） |
| 3 | IoT 段（摄像头/插座，以后和 HA 隔离） |
| 4 | 管理/调试（插笔记本直连） |

- [ ] **做一份完整备份并验证能恢复**（当前 Windows 上的数据）：
      `python scripts/backup_docker.py`（命名卷），确认 `--check` 通过；明文包别留在公共盘

## 阶段 1：装系统（1 小时）

- [ ] Debian 12 netinst，最小安装，只勾 SSH server
- [ ] 时区 `Asia/Shanghai`：`timedatectl set-timezone Asia/Shanghai`
- [ ] 主机名小写、DSN 干净：`hostnamectl set-hostname xiaoma`
- [ ] 用户加入 `sudo`；`apt install -y sudo vim curl htop smartmontools`
- [ ] 静态地址：**在路由器上做 DHCP 预留**，不在 `/etc/network/interfaces` 写死
      （换网段时不会失联）
- [ ] 时区/时间同步：`timedatectl` 显示 `System clock synchronized: yes`
- [ ] `ufw` 默认拒绝入站，只放行 22；8208/8123 只对本机或 Tailscale
      （服务监听 `127.0.0.1`，靠 Tailscale Serve 反代，不需要开端口）
- [ ] **没有电池/UPS**：断电是硬关机。能配就配一台小 UPS（USB 通信，`apcupsd`/`nut` 自动关机），
      配不了就把「备份必须离开本机」当硬要求（见阶段 7）

## 阶段 2：Docker 与数据恢复（半天）

- [ ] 装 Docker（官方源）：

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"   # 重新登录生效
```

- [ ] `git clone` 仓库到 `/opt/xiaoma`，`git checkout` 到发布 commit
- [ ] 放 `.env`（从备份里取）：`API_TOKEN`、`MCP_TOKEN`、`VAPID_*`、`HOME_TOKEN`、LLM key
      —— **VAPID 私钥丢了推送订阅全废**；`SERVER_HOST=127.0.0.1`（保持反代模型）
- [ ] `LLM_PROXY` 按新环境改或留空：直连网关正常就别配，配错会绕远
- [ ] 建卷并恢复：

```bash
docker volume create xiaoma-data
docker compose build
python3 scripts/backup_docker.py --restore backups/xiaoma-<时间>.zip.enc \
    --volume xiaoma-data --yes      # 加密包会要口令（BACKUP_PASSPHRASE）
docker compose up -d
```

- [ ] `docker compose ps` 里 `xiaoma` 为 healthy；`docker logs xiaoma` 没有 traceback
- [ ] 核对数量：`/api/stats` 的 memories / items / tasks / facts / entities 与旧机器一致
- [ ] **在 Linux 上跑一遍测试**（路径/大小写差异只有这里能发现）：

```bash
sudo apt install -y python3-venv
python3 -m venv .venv && .venv/bin/pip install -r requirements-ci.txt
.venv/bin/python -m pytest tests -q
```

## 阶段 3：12TB 机械盘（装完系统再插）

- [ ] 插盘、`lsblk` 确认设备名（`/dev/sdb` 之类，别按想当然）
- [ ] 分区 + 文件系统（单盘，无冗余）：

```bash
sudo parted /dev/sdb --script mklabel gpt
sudo parted /dev/sdb --script mkpart primary ext4 0% 100%
sudo mkfs.ext4 -m 1 -L data12t /dev/sdb1
sudo mkdir -p /srv && sudo blkid /dev/sdb1     # 取 UUID
```

- [ ] `/etc/fstab` 用 **UUID** 挂到 `/srv`，加 `noatime`；`sudo mount -a` 验证
- [ ] 目录：`/srv/photos`（Immich）、`/srv/docs`（Paperless）、`/srv/backup`
- [ ] 权限：Docker 容器以 uid 10001 运行，`chown -R 10001:10001 /srv/docs /srv/photos`
      （装对应服务时按镜像文档再核一遍）
- [ ] SMART：`sudo smartctl -a /dev/sdb`，`systemctl enable --now smartd`，
      每月一次 `smartctl -t short /dev/sdb`，看 `/var/log/syslog`
- [ ] 文件系统选型：ext4 就够；要快照防手滑用 btrfs（`mount -o compress=zstd,noatime`）；
      用 ZFS 单盘没有冗余，但 48GB 内存做 ARC 很舒服——**显式设 `zfs_arc_max=8G~16G`**，
      别让 ARC 把容器要的内存占满

> **不要用 HPE S100i 软 RAID**：Linux 下不认。单盘就是单盘，冗余靠第二份备份。

## 阶段 4：打印（CUPS 客户端）

- [ ] 确认镜像里有 `lp`/`lpstat`；打印机在另一台机器上（`PRINTER_SERVER=192.168.1.15:631`）：

```bash
docker exec xiaoma lpstat -h 192.168.1.15:631 -a     # 应列出队列
curl -s http://127.0.0.1:8208/api/printer -H "Authorization: Bearer $API_TOKEN"
```

- [ ] 打印机开机后 `/api/printer` 30 秒内变绿（`available=true`），概览页能点「重查」
- [ ] 真打一张：网页上传 → 聊天说「打印」→ 确认 → 出纸（**只能在这台机器上验一次**）
- [ ] Office 文档要 LibreOffice：`docker exec xiaoma soffice --version`（镜像里已装就跳）

## 阶段 5：网络与地址

| 依赖 | 旧值 | 新值要改哪里 |
|---|---|---|
| 浏览器访问 | `http://127.0.0.1:8208` | 手机/平板用 `https://<新 tailnet 主机名>` |
| Web Push 订阅 | 绑定旧「源」 | **全部失效，每台设备重新开启通知** |
| PWA 安装 | 绑定旧「源」 | 重新「添加到主屏」 |
| localStorage 聊天记录 | 绑定旧「源」 | 不会跟过去（可接受） |
| 小智 MCP 地址 | `ws://<旧 IP>:8208/mcp_endpoint/mcp/?token=…` | xiaozhi-server 配置 |
| 摄像头视觉地址 | `http://<旧 IP>:<小智端口>/mcp/vision/explain` | `.config.yaml`，**设备要能访问到** |
| 设备 OTA/WS 地址 | 旧 IP | 小智 OTA 返回的地址 |

- [ ] 局域网内先验收（HTTP）：`curl http://<新 IP>:8208/api/status -H "Authorization: Bearer …"`
- [ ] 装 Tailscale，`tailscale up`，再 `tailscale serve --bg 8208`
      —— 主机名变了就等于换源：**每台手机重新订阅推送、重新添加到主屏**
- [ ] 如果加了 2.5G 网卡：只有 x16 半高槽，装了网卡就不能装显卡；板载 4 口仍可给 IoT/管理用

### xiaozhi-server 一起搬（它不在本仓库）

- [ ] 拷 `data/.config.yaml`（`API_TOKEN`、LLM `base_url`、`mcp_endpoint`）
- [ ] `models/SenseVoiceSmall/model.pt` 直接拷，别重新下（约 900MB）
- [ ] Linux 上没有 `host.docker.internal`：compose 加
      `extra_hosts: ["host.docker.internal:host-gateway"]`，或 `network_mode: host`
- [ ] 端口：Windows 上为了绕保留端口段用过 8400/8403；Linux 上可改回 8000/8003，
      **改完同步设备 OTA 里的 WebSocket 地址**
- [ ] `mcp_endpoint` 指 `ws://127.0.0.1:8208/mcp_endpoint/mcp/?token=<MCP_TOKEN>`
- [ ] `mcp_forward_speaker: true` 保持开启，否则声纹身份带不过来
- [ ] 两套 ASR 别同时开：设备语音走小智，小马保持 `STT_ENABLED=0`

## 阶段 6：功能验收（逐项打勾）

- [ ] `/api/status`：`stt_ready` 符合配置、计数正确、`runtime` 有值
- [ ] `/api/printer`：`available=true`、`queue=HP1020`、`jobs` 正常
- [ ] 网页聊天：记一条、查一条、删一条（走确认）
- [ ] 提醒：造一条今天到期 → 在线时 SSE 弹卡片并播报
- [ ] 推送：手机锁屏能收到（**仅 HTTPS 下有效**）
- [ ] HA：`python scripts/ha_check.py`；灯开关回读一致
- [ ] MCP：`python scripts/mcp_probe.py --token … --speaker-id test`
- [ ] 空间：`curl -H "X-Space-Id: family-a" …/api/status` 返回 `space_id=family-a`，
      `GET /api/spaces` 能列出来（D71）
- [ ] 重启演练：`sudo reboot` → 容器自动起来（`restart: unless-stopped`）
- [ ] 断电演练（如果没 UPS）：直接拔电，恢复后数据库 `PRAGMA integrity_check` 正常

## 阶段 7：备份与回滚

- [ ] 加密备份 + systemd timer（步骤在 `docs/backup.md`，`--keep 14`）：
      `systemctl list-timers xiaoma-backup.timer` 能看到下一次
- [ ] **异地一份**：每月把 `backups/*.zip.enc` 拷到移动盘/网盘（口令别放一起）
- [ ] 备份恢复演练：恢复到临时卷，查条数与生产一致（`docs/backup.md` 有完整命令）
- [ ] 回滚：旧 Windows 服务先别拆；路由器 DHCP 预留改回去，`start.bat` 起来，
      数据用最近一份备份覆盖

## 阶段 8：观察与维护

- [ ] 温度/功耗：`sensors`（需 `lm-sensors`）；iLO 有的话直接看实时功耗
      —— 预期待机 23–27W，日常 30–40W
- [ ] 磁盘：`df -h /srv`、`smartctl -H /dev/sdb`；概览页「运行状态」看 `disk_free_gb`
- [ ] 日志：`docker logs --since 1h xiaoma`；容器日志已由 compose 轮转（10MB × 3）
- [ ] 每月：跑一次 `scripts/backup_docker.py --check`，抽一份恢复到临时卷
- [ ] 每季度：`apt update && apt upgrade`，`docker compose pull && docker compose up -d`

## 迁移时机

设备真机（唤醒词、反向提醒、拍照）和打印机验收都要在这台机器上做一遍。
建议顺序：系统 + 恢复数据 → 打印机 → Tailscale/推送 → 小智与设备 → 12TB 挂载 → 备份定时。
每一步都留下可回滚的状态（旧机器别提前拆）。
