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
- `LLM_PROXY`：留空就是直连（先直连、连不上才用它）。以前填的是 Windows 上
  v2rayN 的 `10808`；服务器本机跑了自己的 xray 之后（见第 9 节），要兜底可以填
  `http://host.docker.internal:10808`，不填也正常。
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

注意这是**三个 compose 项目，各有一份 unit**（都在同目录，装法一样）：

```bash
sudo cp infra/ubuntu/{xiaoma,xiaozhi,homeassistant}-compose.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable xiaoma-compose.service xiaozhi-compose.service homeassistant-compose.service
```

| unit | 项目 | 工作目录 |
|---|---|---|
| `xiaoma-compose.service` | 小马 | `/opt/opencode-my` |
| `xiaozhi-compose.service` | 小智服务端（`infra/xiaozhi-server/`） | `/opt/xiaozhi-server` |
| `homeassistant-compose.service` | HA（`infra/homeassistant/`） | `/opt/opencode-my/infra/homeassistant` |

只靠容器的 `restart` 策略也能跟着 `docker.service` 回来，但有人 `docker compose down`
过一次就永远不回来了——这三个 oneshot 就是补这个洞。

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

## 9. 服务器本机跑 xray（代理不再依赖 Windows）

以前 `LLM_PROXY` / `WEB_PROXY` 指向开发机上 v2rayN 的 `192.168.2.41:10808`，那台
一关机，服务器就拉不动 Docker Hub、查不了被墙的站。现在服务器自己跑一份：

```bash
# 1. 取二进制（GitHub 直连能下；不行就先借开发机的代理）
curl -fL -o /tmp/xray.zip https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip
sudo install -d /opt/xray
# 2. 解包到 /opt/xray（xray / geoip.dat / geosite.dat 三个文件）
# 3. 配置：直接把开发机 v2rayN 的 binConfigs/config.json 拷过来
sudo install -m 644 config.json /opt/xray/config.json
# 4. 起服务（unit 见下）
sudo systemctl enable --now xray
```

`xray.service` 的要点：`User=xray`（`useradd -r -s /usr/sbin/nologin xray`，别用
`nobody`，systemd 会告警）、`ExecStart=/opt/xray/xray run -config /opt/xray/config.json`、
`Restart=on-failure`。配置里的入站是 `mixed` `0.0.0.0:10808`，和 v2rayN 一致。

**`geosite.dat` / `geoip.dat` 必须用 v2rayN 自带的那两个（`bin/` 目录下）**，
不能用 Xray 官方 release 里的：官方包只有 `geosite:cn`、`geosite:google` 这些，
没有 `geosite:gfw` / `geosite:greatfire`，xray 启动会直接报
`illegal domain rule: geosite:gfw` 然后退出。

系统级改成走本机：

```bash
# /etc/environment（http_proxy / https_proxy / no_proxy，内网段放 no_proxy）
# /etc/apt/apt.conf.d/95proxy
# /etc/systemd/system/docker.service.d/http-proxy.conf  → HTTP(S)_PROXY=http://127.0.0.1:10808
sudo systemctl daemon-reload && sudo systemctl restart docker
```

**Docker 只 `reload` 不生效**：Go 把 `HTTP_PROXY` 缓存了，必须先重启
`docker.service` 才会用新值（容器有 restart 策略，会自己回来）。

小马自己的上网工具（`web_search` / `web_fetch`）走的是 `config.WEB_PROXY`，
**填在 `.env` 里，别写进 compose 的 `environment:`**——那里会盖掉 `env_file`，
而且部署脚本一改 compose 就没了：

```bash
WEB_PROXY=http://host.docker.internal:10808   # 容器 → 宿主机 xray
```

验收（三条都要过）：

```bash
curl -sI -m 10 https://registry-1.docker.io/v2/ | head -n 1   # 401 = 通了
docker pull hello-world
docker exec -i xiaoma python -c "from adapters.web import fetch; print(fetch('https://duckduckgo.com', 40))"
```

注意：`/opt/xray/config.json` 只是 v2rayN 配置的**静态快照**，换节点/换订阅要重新拷；
它监听 `0.0.0.0:10808`（和 v2rayN 一样，局域网里谁都能当代理用），只想本机用就把
`inbounds[].listen` 改成 `127.0.0.1`，此时容器改走 `172.17.0.1` 之类的桥地址或
`host-gateway`。

## 10. 在服务器上一条命令部署

以前从 Windows 用 `git bundle` + `scp` 一步步来，漏过步骤：镜像重建了、容器没换
（`up -d` 认为镜像没变就不 recreate），跑的还是旧代码。现在把部署放在服务器上：

```bash
sudo /opt/opencode-my/scripts/deploy_server.sh          # 拉 GitHub master → 重建 → 健康检查
sudo /opt/opencode-my/scripts/deploy_server.sh --check  # 只看有哪些新提交
sudo /opt/opencode-my/scripts/deploy_server.sh --build  # 只改了文档也强制重建
```

装好的机器上还有个短名字（软链到同一份脚本，改了脚本不用重新安装）：

```bash
sudo xiaoma-deploy          # 平时用这个
```

它做的事，顺序固定：

1. `git fetch` 最新的 `master`（走本机 xray：服务器直连 GitHub 不通，代理不可用才退回直连）
2. 打印 `旧..新` 的提交列表；带 `--check` 就停在这里
3. 工作区必须干净（有改动就停下；`--force` 会先 `git stash`）
4. 快进合并；只改了 `docs/`、`tests/`、`*.md` 就不重建、不重启，直接结束
5. `docker tag` 当前镜像当回滚点 → 重建 → `up -d --force-recreate`
6. 轮询 `http://127.0.0.1:8208/healthz`（默认 90 秒）；**失败自动回滚**：
   代码 `git reset --hard` 回旧提交、镜像换回旧 tag、容器重建、再验一次
7. 每一步写 `/var/log/xiaoma-deploy.log`

实测（Gen10 Plus，四个容器在跑）：**有代码更新的一次部署 17 秒**（镜像层已缓存），
无更新时 2 秒（只 fetch 比对，不动容器）。`flock` 锁在 `/run/lock/xiaoma-deploy.lock`，
同一时间只有一个部署在跑；**别把锁放回仓库里**——它会出现在 `git status` 里，把
「工作区必须干净」这条自己判成脏（踩过）。

仓库是公开的，所以拉取不需要密钥。它只部署小马这一套 compose；小智
（`/opt/xiaozhi-server`）和 HA（`data/homeassistant`）是各自独立的项目，不在这里做。


