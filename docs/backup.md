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

# 真正恢复（会先自动把当前状态另存一份到 backups/pre-restore/）
python scripts/backup.py --restore backups/xiaoma-<时间>.zip --yes
```

落地位置：`backups/xiaoma-<YYYYMMDD-HHMMSS>.zip`（已进 `.gitignore`，因为里面有 `.env`）。

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

## 异地那一份

脚本**不碰网络**（备份里有密钥，自动上传等于扩大暴露面）。目前是手动：

- 每月一次把最新的 `backups/xiaoma-*.zip` 拷到手机或网盘（zip 里有 `.env`，别放公共网盘）
- 或者等迁移到家庭服务器后，插一块移动硬盘每周 rsync 一次

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

## 已知边界

- **`.env` 在备份里是明文**：zip 当密钥文件对待，别上传、别发群
- 恢复后要**重启 server.py** 才会读回数据（SQLite 连接在进程里）
- `--restore` 到本机要求加 `--yes`，且会先自备份一份，防手滑
- 备份不含小智服务端（`D:\xiaozhi-server`）与 HA 数据；那两块各自有配置/快照
