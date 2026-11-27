# Ubuntu Server 侧部署件（迁移日照着做，现在只写不执行）

这台 Windows 是开发机：Docker Desktop 手动开关，不做开机自启、不配自动登录
（2026-11-27 定）。**开机自启这一套在搬过去之后一次做对**，文件都在这个目录里：

```
infra/ubuntu/
├─ README.md                # 迁移日执行顺序（先读这个）
├─ xiaoma-compose.service   # 开机把 compose 项目拉起来（restart 策略之外的第二道）
├─ xiaoma-backup.service    # 每天备份小马数据卷（加密）
└─ xiaoma-backup.timer      # 触发上面那个 service
```

执行清单在 `docs/server-migration.local.md`（本机笔记，不入库）；备份的口令、
加密格式与恢复演练在 `docs/backup.md`。这里只补两件那边没写细的事：
**开机怎么把这个栈拉起来**，以及**单元文件直接从仓库拷，别手抄**。

---

## 0. 前提

- **系统**：**Ubuntu Server 26.04 LTS**（2026-11-27 定）。安装时选 **Minimized**，
  只开 OpenSSH server。原来清单里写的 Debian 12 已弃用——它现在是 oldstable，
  常规安全支持 2026-07 就结束了。选 Ubuntu 的理由：Immich/Paperless/Tailscale 的教程
  默认它、HPE 支持矩阵里有它、个人 Pro 免费能多几年安全更新和 Livepatch。
- 局域网固定 IP（路由器上做 DHCP 预留），设备和小智都按 IP 连。
- 仓库目录**保持叫 `opencode-my`**，建议 `/opt/opencode-my`。

  不是洁癖：compose 的卷名跟着项目目录名走，现在是 `opencode-my_xiaoma-data`，
  改名就变成 `xiaoma_xiaoma-data`，`scripts/backup_docker.py` 自动找卷、恢复到
  「原来的那个卷」都会对不上。真要改名就先在 compose 里把卷名钉死：

  ```yaml
  volumes:
    xiaoma-data:
      name: opencode-my_xiaoma-data
  ```

  再让 compose 用命名卷，恢复时也仍然用 `--volume opencode-my_xiaoma-data` 显式指定，
  别靠自动识别。

## 1. 装 Docker（官方源）

```bash
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
  sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin python3-cryptography
```

`python3-cryptography` 是给 `scripts/backup_docker.py --encrypt` 用的：备份在宿主机上跑，
不在容器里。

> 如果 `apt update` 报 Docker 源没有对应 codename（404），说明那个版本刚发布、Docker 还没
> 跟上。别改 codename 硬凑，改用官方 convenience script（`curl -fsSL https://get.docker.com | sudo sh`）
> 或等几天再装。

**开机自启的第一道**就是 `docker.service` 本身（apt 装完默认 enabled）：

```bash
sudo systemctl enable --now docker
systemctl is-enabled docker      # 必须是 enabled
```

## 2. 放代码和配置

```bash
sudo mkdir -p /opt && sudo chown "$USER": /opt
git clone <仓库地址> /opt/opencode-my
cd /opt/opencode-my
cp docker-compose.example.yml docker-compose.yml
cp .env.example .env && chmod 600 .env
```

（别用 `sudo git clone`：仓库会变成 root 所有，后面 `git pull`、改 `.env` 都要 sudo。）

`.env` 里上服务器必须确认的几处：

- `API_TOKEN`：**必填**。监听非本机又没有 Token 时 `_check_exposure()` 会直接拒绝启动。
  `SERVER_HOST` 不用在 `.env` 里纠结：compose 已经把容器内固定成 `0.0.0.0`
  （`environment` 优先于 `env_file`），对外靠端口映射 + Token，不靠改监听地址。
- `LLM_PROXY`：现在是 `http://host.docker.internal:10808`，指向 Windows 上的 Clash。
  Ubuntu 上没有这个代理就**清空**（留空仍然先直连，不影响）。
- `HOME_BASE_URL`：HA 还是容器，`http://host.docker.internal:8123` 不用改
  （compose 里已经加了 `host-gateway`）。

## 3. 恢复数据卷

先建镜像（下面演练要用它跑容器查库），再把最近一份备份恢复到临时卷演练：

```bash
docker compose build
docker volume create xiaoma-restore-test
python3 scripts/backup_docker.py --restore backups/xiaoma-….zip.enc \
    --volume xiaoma-restore-test --yes          # 加密包会当场要口令
docker run --rm -v xiaoma-restore-test:/data xiaoma:local python -c \
  "import sqlite3;print(sqlite3.connect('/data/tasks.db').execute('select count(*) from tasks').fetchone())"
```

条数对得上再恢复生产卷（会先停用占用它的容器，所以**在第一次 `up` 之前做**更省事）：

```bash
docker volume create opencode-my_xiaoma-data
python3 scripts/backup_docker.py --restore backups/xiaoma-….zip.enc --volume opencode-my_xiaoma-data --yes
```

## 4. 起栈 + 开机自启

```bash
docker compose up -d --build
curl -s http://127.0.0.1:8208/healthz          # {"ok": true}
```

**第二道自启**：`restart: unless-stopped` 只能保证「容器跟着 docker.service 起来」；
如果有人 `docker compose down` 过、或者容器被删了，重启后就没有了。装一个 oneshot：

```bash
sudo cp infra/ubuntu/xiaoma-compose.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable xiaoma-compose.service
```

内容见同目录文件。它只做 `docker compose up -d`，镜像和端口都在 compose 里。

注意它**只管 `opencode-my` 这一个 compose 项目**：HA（`infra/homeassistant`）和小智
（原 `D:\xiaozhi-server`）是另外两个项目，靠各自的 `restart` 策略跟着 `docker.service`
起来。只有在你也会手动 `docker compose down` 它们时，才需要照抄一份 unit。

**验收方式**（迁移阶段 6 的重启演练）：

```bash
sudo reboot
# 等 60 秒，不登录、不开任何会话
docker ps --format '{{.Names}} {{.Status}}'
curl -s http://127.0.0.1:8208/healthz
```

三个容器（xiaoma / xiaozhi-esp32-server / homeassistant）应该都在跑，且
`xiaoma status` 里的 `device_channels` 在设备上电后变 1。

## 5. 备份定时

```bash
sudo mkdir -p /etc/xiaoma
sudo install -m 600 /dev/null /etc/xiaoma/backup.env
sudo tee /etc/xiaoma/backup.env >/dev/null <<'EOF'
BACKUP_PASSPHRASE=换成你自己的长口令
EOF

sudo cp infra/ubuntu/xiaoma-backup.service infra/ubuntu/xiaoma-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now xiaoma-backup.timer
```

立刻跑一次确认能出包（不等 03:30）：

```bash
sudo systemctl start xiaoma-backup.service
journalctl -u xiaoma-backup.service -n 30
ls -lh backups/
systemctl list-timers xiaoma-backup.timer
```

口令只在这一个文件里（`600`，root 可读），**不要**写进 `.env`、不要进仓库、
不要和 `backups/` 放同一个目录。丢了备份就打不开。

## 6. 端口与暴露面

| 端口 | 谁 | 必须能从哪访问 |
|---|---|---|
| 8400 | 小智 WebSocket | 局域网（设备装不了 Tailscale，这条绕不开） |
| 8403 | 小智 HTTP（OTA + 视觉回调） | 局域网（同上） |
| 8208 | 小马 REST/SSE | 手机、网页 |
| 8123 | Home Assistant | 小马容器 + 浏览器 |

两种开法，按要不要让外网进门选一种：

- **局域网直连（简单）**：`ufw` 放行，访问控制靠 `API_TOKEN`。

```bash
sudo ufw allow 22/tcp
sudo ufw allow 8400,8403/tcp
sudo ufw allow 8208,8123/tcp
sudo ufw enable
```

- **网页/手机只走 Tailscale（更紧）**：8208/8123 只绑本机，用 `tailscale serve` 反代；
  8400/8403 仍然要对局域网开，否则设备连不上。

```yaml
ports:
  - "127.0.0.1:${SERVER_PORT:-8208}:8000"
```

> 绑哪个地址由 compose 的 `ports:` 决定，**不是** `.env` 里的 `SERVER_HOST`：
> 容器内必须监听 `0.0.0.0`，改它只会让容器自己都不可达。

Windows 上的 `netsh interface portproxy` 在 Linux **不需要**：容器直接发布在
`0.0.0.0`，设备用「局域网 IP:8400」就能连。那段 portproxy 是 Docker Desktop 的
虚拟机网络逼出来的补丁，别一起搬过来。

## 7. 小智服务端（单独一套，不在本仓库）

`infra/xiaozhi-server/` 里只有补丁、桥和 compose 模板，真正的 `D:\xiaozhi-server`
（`data/`、`models/` 两个目录，加起来几个 G）要**整个目录拷过去**，别只拷代码。

拷完必须确认 `data/.config.yaml` 里有这两行（D78，重装最容易漏）：

```yaml
close_connection_no_voice_time: 86400
idle_after_voice_time: 15
```

少了第一行，聊完就挂断、提醒只能在会话期间到达；少了第二行，设备会一直开着麦克风。

## 8. 与 Windows 的差异（搬完会踩的）

- **CUPS 不在容器里**：打印走局域网 CUPS（`192.168.1.15:631`），Ubuntu 上
  只要容器里有 `lp` 客户端（`INSTALL_PRINT=1` 已装）就能用，不需要在本机装 CUPS 服务。
- **`host.docker.internal`** 在 Linux 上靠 compose 的 `extra_hosts: host-gateway`，
  两个 compose 都要有，删了就连不上宿主上的服务。
- **时区**：`TZ=Asia/Shanghai` 已在 compose 里给；`timedatectl` 也要设对，
  否则夜间蒸馏、提醒冷却的时间点都会偏。
- **`data/` 目录**：HA 的配置在 `data/homeassistant`（compose 用相对路径挂载），
  跟小马的数据库不是一回事，备份脚本默认也不含它（`--all-data` 才带）。
