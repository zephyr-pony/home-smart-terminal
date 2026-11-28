#!/usr/bin/env bash
# 只把加密备份包传到 OneDrive；只增不删，云端的旧包一直留着。
# 失败时给手机推一条通知（走小马的 Web Push），否则没人会知道。
set -euo pipefail

RCLONE="$HOME/.local/bin/rclone"
CONFIG="$HOME/.config/rclone/rclone.conf"
SRC="${SRC:-/opt/opencode-my/backups}"
DEST="${DEST:-onedrive:xiaoma-backup}"
ALERT="${ALERT:-$HOME/.local/bin/xiaoma-alert.sh}"
LOG="$HOME/.local/state/xiaoma-backup-upload.log"

mkdir -p "$(dirname "$LOG")"
# 日志超过 1MB 轮转一份，别让它无限长
if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG")" -gt 1048576 ]; then
  mv -f "$LOG" "$LOG.1"
fi

# 只认 .zip.enc：明文 zip 里有 .env，绝不让它出机器
status=0
"$RCLONE" copy "$SRC" "$DEST" \
  --config "$CONFIG" \
  --include '*.zip.enc' \
  --transfers 1 \
  --checkers 2 \
  --retries 3 \
  --low-level-retries 5 \
  --contimeout 30s \
  --timeout 5m \
  --stats-one-line \
  --log-level INFO \
  --log-file "$LOG" || status=$?

if [ "$status" -ne 0 ]; then
  echo "rclone 退出码 $status，上传失败" >&2
  # 告警失败不影响退出码：主流程的失败原因要如实带出去
  if [ -z "${XIAOMA_ALERT_DISABLE:-}" ] && [ -x "$ALERT" ]; then
    "$ALERT" "备份上传失败" "rclone 退出码 $status，看 journalctl --user -u xiaoma-backup-upload" || true
  fi
  exit "$status"
fi
