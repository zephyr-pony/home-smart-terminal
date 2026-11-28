# 备份与恢复

> 目标不是「有备份」，而是**恢复过一次**。下面这套 2026-11-23 在这台机器上真跑过演练
> （见末尾「演练记录」）。

## 备什么、为什么

| 内容 | 为什么不能丢 |
|---|---|
| `data/memory.db` | 家里人说过的话，丢了没法重建 |
| `data/facts.db` | 户号、地址、型号；重建要靠人回忆 |
| `data/tasks.db` | 提醒和账单 |
| `data/items.db` | 物品与到期日 |
| `data/agent.db` | 实体、别名、审计、待澄清动作 |
| `data/dialogue.db` | 最近几天的对话原文（默认 7 天）。画像和记忆是提炼后的结论，已另存在 `facts.db` / `memory.db`；这份只影响「接着聊」，含原话，离机备份务必加密 |
| `data/push.db` | 推送订阅（丢了手机要重新开一遍通知） |
| `.env` | **VAPID 私钥**在这里；丢了所有推送订阅全部作废 |

默认**不备**：`*.log`（噪声）、`data/homeassistant`（HA 运行时目录，用 HA 自己的快照）、
`data/chroma`（RAG 向量，可重建）。要连目录一起备就加 `--all-data`。

## 用法

```bash
# 每天跑一次：备份 + 自动校验 + 只留最近 7 份
python scripts/backup.py

# 看现有备份 / 校验某一份（sha256 + 每个库的 integrity_check）
python scripts/backup.py --list
python scripts/backup.py --check backups/xiaoma-20261123-155308.zip

# 恢复演练：解到空目录，不动本机数据
python scripts/backup.py --restore backups/xiaoma-20261123-155308.zip --to D:/tmp/drill

# 加密备份（口令取 BACKUP_PASSPHRASE 或 --passphrase-file）
python scripts/backup.py --encrypt

# 真正恢复（会先自动把当前状态另存一份到 backups/pre-restore/）
python scripts/backup.py --restore backups/xiaoma-<时间>.zip --yes
```

落地位置：`backups/xiaoma-<YYYYMMDD-HHMMSS>.zip`（已进 `.gitignore`，因为里面有 `.env`）；
加密时是 `.zip.enc`，同一目录不会同时留明文。

## 数据在 Docker 命名卷里时（当前部署）

`scripts/backup.py` 扫的是仓库下的 `data/`，而容器把数据挂在命名卷
（`opencode-my_xiaoma-data:/data`）——直接跑它等于**没备到**。用这个：

```bash
# 自动找 *xiaoma*data* 卷，快照 → 打包 → 校验 → 只留 7 份
python scripts/backup_docker.py

# 看不到候选卷就列出来 / 手动指定
python scripts/backup_docker.py --list
python scripts/backup_docker.py --volume opencode-my_xiaoma-data

# 恢复演练：先建一个空卷，恢复到它，再查条数
docker volume create xiaoma-restore-test
python scripts/backup_docker.py --restore backups/xiaoma-2026....zip --volume xiaoma-restore-test
docker run --rm -v xiaoma-restore-test:/data:ro xiaoma:local \
  python -c "import sqlite3;print(sqlite3.connect('/data/memory.db').execute('select count(*) from memories_raw').fetchone())"
docker volume rm xiaoma-restore-test

# 真正恢复：正在用该卷的容器会被先停再起，所以要 --yes
python scripts/backup_docker.py --restore backups/xiaoma-2026....zip --yes
```

它不 `docker cp`：服务在跑，`cp` 拿到的是写到一半的库。做法是起一个临时容器，
只读挂载卷，用 sqlite `backup` API 逐个库做在线快照，再把结果 `tar` 到 stdout，
由宿主机解出来交给 `backup.py` 打包（manifest / sha256 / integrity_check / 保留份数
都复用）。宿主目录不用挂进容器，Linux 上也不会有 uid 对不上的问题。
`.env` 仍在宿主机上打包；恢复时默认不动它（换机器时加 `--with-env` 才覆盖）。

## 为什么不是「拷 data/ 目录」

服务常驻在跑，`*.db` 随时可能写到一半；直接拷 zip 出来的库会**偶发损坏**，而且平时看不出来，
等到真要恢复才发现。脚本对每个库走 sqlite 的 `backup` API（在线热备），写完再逐个
`PRAGMA integrity_check`。

## 定时（Windows）

**当前决定：先不注册计划任务，按需手动跑**（`python scripts/backup.py`）。
理由是现在数据量小、改动不频繁，每天自动打包却没人看，反而容易把「有备份」当成「验证过」。

要加的时候（系统级改动，先确认）：

```powershell
schtasks /Create /TN "xiaoma-backup" /SC DAILY /ST 03:00 /RL HIGHEST `
  /TR "\"D:\opencode-my\.venv\Scripts\python.exe\" \"D:\opencode-my\scripts\backup.py\""
```

迁移到 Linux 后换成 cron：

```cron
0 3 * * * cd /opt/xiaoma && .venv/bin/python scripts/backup.py >> data/backup.log 2>&1
```

## 加密（2026-11-26）

备份里有 `.env`（VAPID 私钥）；接上文档/照片后还会有体检报告、账单扫描件这类身份信息。
**离开自家硬盘的那一份必须加密**，本机目录里也建议默认加密。

```bash
# 口令走环境变量（交互式终端），或用 --passphrase-file 指向一个 chmod 600 的文件
export BACKUP_PASSPHRASE='一串足够长的口令'
python scripts/backup_docker.py --encrypt        # 产出 xiaoma-…zip.enc，明文 zip 立刻删除
python scripts/backup_docker.py --check backups/xiaoma-….zip.enc
python scripts/backup_docker.py --restore backups/xiaoma-….zip.enc --volume … --yes
```

- 格式：`magic(5) + salt(16) + nonce(12) + AES-256-GCM 密文`，密钥由口令经 scrypt 派生，
  换机器也能解，不依赖本机任何密钥文件。
- **口令丢了备份就打不开**，没有任何后门。把口令记在密码库（Vaultwarden）里，别和备份放同一个目录。
- CLI 上 `--encrypt` 没给口令会直接报错退出，不会悄悄产出明文包。
- `--check` / `--restore` 认文件头：`.zip` 和 `.zip.enc` 都能直接传，加密的才要口令。
- 加密靠 `cryptography`（已经是 `pywebpush` 的依赖，这里显式钉版本）。

## 定时（Linux，迁移后）

Windows 这台继续按需手动跑（上面那节）。上了家庭服务器就交给 systemd timer，
别用 cron 拼长命令——日志进 journal，失败也看得见。

两个单元文件在仓库里，**直接拷，别手抄**（`infra/ubuntu/`，与开机自启的
`xiaoma-compose.service` 放在一起）：

- `infra/ubuntu/xiaoma-backup.service` —— 跑 `scripts/backup_docker.py --encrypt --keep 14`
- `infra/ubuntu/xiaoma-backup.timer` —— 每天 03:30，`Persistent=true` 补跑错过的

`/etc/xiaoma/backup.env`（`chmod 600`，只 root 可读）：

```ini
BACKUP_PASSPHRASE=一串足够长的口令
```

```bash
sudo cp infra/ubuntu/xiaoma-backup.service infra/ubuntu/xiaoma-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now xiaoma-backup.timer
systemctl list-timers xiaoma-backup.timer      # 看下次触发
journalctl -u xiaoma-backup.service -n 50      # 看最近一次结果
```

`--keep 14` 保留两周；内存/磁盘都吃得下，出问题时能往回多看几天。

异地那一份由另一个 user timer 负责（`xiaoma-backup-upload.timer`），见下节。

## 异地那一份（OneDrive，2026-11-28 起）

`backup.py` / `backup_docker.py` 仍然**不碰网络**：制包和上传分成两步，上传只认
`.zip.enc`，明文包永不出机器。服务器上已经装好 rclone（用户级，不需要 sudo）：

| 东西 | 位置 |
|---|---|
| rclone | `~/.local/bin/rclone` |
| 授权配置 | `~/.config/rclone/rclone.conf`（`600`） |
| 上传脚本 | `~/.local/bin/xiaoma-backup-upload.sh`（模板在 `infra/ubuntu/`） |
| 定时器 | `~/.config/systemd/user/xiaoma-backup-upload.timer` |
| 日志 | `~/.local/state/xiaoma-backup-upload.log` |
| 云端目录 | `onedrive:xiaoma-backup/` |

- 每天 **04:10**（03:32 出包之后）跑一次，`RandomizedDelaySec=15m` 错开整点。
  user timer 靠 `loginctl enable-linger "$USER"`，没人登录也会触发。
- 手动跑：`systemctl --user start xiaoma-backup-upload.service`；看结果：
  `systemctl --user show xiaoma-backup-upload.service -p Result` 和上面那个日志。
- **只 `copy` 不 `sync`**：云端是只增的归档，本地 `--keep 14` 删掉的旧包在云上还在。
  要清旧包自己来：`rclone delete onedrive:xiaoma-backup --min-age 180d`。
- 恢复时先把包拉回来：`~/.local/bin/rclone copy onedrive:xiaoma-backup ./backups`，再走上面的
  `--restore` 流程。
- 授权是 OAuth，token 就在 `rclone.conf` 里（`600`）。换机器/重装要重走一次
  `rclone authorize onedrive`：服务器没有浏览器，用 SSH 把 53682 端口转出来
  （`ssh -L 53682:127.0.0.1:53682 …`），浏览器授权完 token 直接写回服务器。
- 口令仍只在 `/etc/xiaoma/backup.env`，**不要**和 OneDrive 放一起：云端只有加密包，
  口令丢了等于没备份。
- **上传失败会往手机推一条**：脚本先看 rclone 退出码，非 0 就调 `xiaoma-alert.sh`
  （`POST /api/push/notify`，走小马自己的 Web Push）。token 过期、OneDrive 改了密码、
  网络抽风都会触发，不用天天翻 journal。告警本身失败不影响退出码——systemd 还是能
  看到这次失败。要临时关掉：`XIAOMA_ALERT_DISABLE=1`。
- 单盘部署（Gen10 只有一块 SSD）尤其要注意：**备份不能只留在同一台机器上**，
  否则盘坏了两份一起没。

## 演练记录（2026-11-23）

线上 → 备份 → 解到独立目录 → 逐项比对，**全部一致**：

| 项 | 线上 | 演练副本 |
|---|---|---|
| 记忆 | 3 | 3 |
| 物品 | 0 | 0 |
| 任务 | 5 | 5 |
| 事实 | 3 | 3 |
| 实体 | 1 | 1 |
| `.env` sha256 | — | 一致 |

顺带验证过：服务在跑、数据库连接开着的时候备份，内容依然完整（`tests/test_backup.py`
里也有这条用例）。

## 演练记录（2026-11-26，Docker 命名卷）

容器在跑、服务正在写的时候：`backup_docker.py` 快照 → 打包（18.4 KB）→ 校验通过 →
恢复到临时卷 `xiaoma-restore-test`，逐项比对**全部一致**：

| 项 | 生产卷 | 演练卷 |
|---|---|---|
| 记忆 | 3 | 3 |
| 物品 | 0 | 0 |
| 任务 | 8 | 8 |
| 事实 | 6 | 6 |
| 实体 | 1 | 1 |
| 提案（审计） | 59 | 59 |
| 待澄清动作 | 3 | 3 |
| 推送订阅 | 1 | 1 |

也验证了「卷正被容器使用、没加 `--yes` 会拒绝恢复」，以及演练卷用完 `docker volume rm`
清掉，生产卷没动。

## 已知边界

- **`.env` 在备份里是明文**：zip 当密钥文件对待，别上传、别发群
- 恢复后要**重启 server.py** 才会读回数据（SQLite 连接在进程里）
- `--restore` 到本机要求加 `--yes`，且会先自备份一份，防手滑
- 备份不含小智服务端（`D:\xiaozhi-server`）与 HA 数据；那两块各自有配置/快照
