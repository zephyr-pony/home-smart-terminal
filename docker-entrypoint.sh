#!/bin/sh
# 启动顺序（D86）：root 摆权限 → 起执行通道（work 用户）→ 以 xiaoma 跑服务。
#
# 为什么要分两个 uid：run_shell / run_python 是「模型写的代码」，和 App 同 uid 时
# 一句 sqlite3 /data/agent.db 就能绕过提案与确认直接改正式表。这里靠 Linux 权限
# 把那条路堵死：库文件 640 xiaoma:xiaoma，work 用户既读不到也写不到。
set -eu

DATA="${DATA_DIR:-/data}"
WORK="${XIAOMA_WORK_DIR:-$DATA/work}"
WORK_HOME="${XIAOMA_HOME_DIR:-$DATA/home}"
SOCKET="${SHELL_WORKER_SOCKET:-/run/xiaoma/shell.sock}"
SOCKET_DIR="$(dirname "$SOCKET")"

# 库文件 640、目录 750：新库跟着这个 umask 走，别的用户看不见
umask 027

mkdir -p "$DATA" "$WORK" "$WORK_HOME" "$SOCKET_DIR"

# 卷可能是上一个版本留下的：目录 10001:10001，库文件 0644（其他用户可读）。
# 每次启动都摆正一次，幂等；只碰顶层，不去 walk 整个卷（树莓上很慢）。
chown xiaoma:xiaoma "$DATA"
chmod 751 "$DATA"
chown -R work:work "$WORK" "$WORK_HOME"
chmod 1777 "$WORK"
chmod 750 "$WORK_HOME"
chown work:xiaoma "$SOCKET_DIR"
chmod 2770 "$SOCKET_DIR"
chmod 750 /app 2>/dev/null || true
for suffix in "" "-wal" "-shm"; do
    chmod 640 "$DATA"/*.db"$suffix" 2>/dev/null || true
done

# 执行通道：uid 10002，env -i 起（命令里看不到 API_TOKEN / LLM 密钥）。
if command -v setpriv >/dev/null 2>&1; then
    (
        while :; do
            env -i PATH="/usr/local/bin:/usr/bin:/bin" HOME="$WORK_HOME" \
                DATA_DIR="$DATA" XIAOMA_WORK_DIR="$WORK" \
                XIAOMA_HOME_DIR="$WORK_HOME" SHELL_WORKER_SOCKET="$SOCKET" \
                setpriv --reuid=10002 --regid=10002 --clear-groups \
                python3 /opt/xiaoma-worker/shell_worker.py || true
            sleep 2
        done
    ) &
else
    echo "警告：镜像里没有 setpriv，执行通道没起；run_shell 会以 xiaoma 运行（D86 隔离失效）" >&2
fi

exec setpriv --reuid=10001 --regid=10001 --clear-groups "$@"
