#!/bin/bash
# 在服务器上部署小马：拉最新代码 → 重建镜像 → 重建容器 → 健康检查（失败自动回滚）。
#
# 为什么要有它：以前从 Windows 用 git bundle + scp 一步步来，漏过步骤——
# 镜像重建了、容器没换（`up -d` 认为没变就不 recreate），跑的还是旧代码。
# 这里一条命令，步骤顺序固定，失败自己退回去。
#
# 用法（服务器上，root）：
#   sudo /opt/opencode-my/scripts/deploy_server.sh          # 有更新就部署
#   sudo /opt/opencode-my/scripts/deploy_server.sh --check  # 只看有没有更新，不动
#   sudo ... --build                                        # 只改了文档也重建镜像
#   sudo ... --force                                        # 工作区有改动也硬来（先 stash）
#
# 环境变量：DEPLOY_PROXY（默认 http://127.0.0.1:10808）、REMOTE_URL、BRANCH、
#           HEALTH_TIMEOUT（默认 90 秒）、DEPLOY_LOG。
#
# 只部署小马（默认空间的那套 compose）。小智和 HA 是各自独立的项目
# （/opt/xiaozhi-server、data/homeassistant），改它们不在这里做。

set -uo pipefail

REPO=${REPO:-/opt/opencode-my}
BRANCH=${BRANCH:-master}
REMOTE_URL=${REMOTE_URL:-https://github.com/zephyr-pony/home-smart-terminal.git}
PROXY=${DEPLOY_PROXY:-http://127.0.0.1:10808}
HEALTH_URL=${HEALTH_URL:-http://127.0.0.1:8208/healthz}
HEALTH_TIMEOUT=${HEALTH_TIMEOUT:-90}
SERVICE=${SERVICE:-xiaoma}
LOG=${DEPLOY_LOG:-/var/log/xiaoma-deploy.log}

MODE=deploy
FORCE=0
FORCE_BUILD=0
for arg in "$@"; do
    case "$arg" in
        --check) MODE=check ;;
        --build) FORCE_BUILD=1 ;;
        --force) FORCE=1 ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "不认识的参数：$arg（--check / --build / --force）" >&2; exit 2 ;;
    esac
done

# 日志：root 跑就写 /var/log，否则写仓库根目录（log 里不含密钥）
log() {
    printf '%s %s\n' "$(date '+%F %T')" "$*"
    printf '%s %s\n' "$(date '+%F %T')" "$*" >>"$LOG" 2>/dev/null || true
}
die() { log "❌ $*"; exit 1; }

[ -d "$REPO/.git" ] || die "不是 git 仓库：$REPO"
cd "$REPO" || die "进不去 $REPO"

# 同一个仓库同时只允许一个部署在跑
exec 9>"$REPO/.deploy.lock" || die "建不了锁文件"
flock -n 9 || die "已经有一个部署在跑（.deploy.lock 被占用）"

# git 走 xray：服务器直连 GitHub 不通，代理可用就用，不可用就退回直连
git_net=()
if curl -s -x "$PROXY" -o /dev/null --max-time 5 https://github.com/ 2>/dev/null; then
    git_net=(-c "http.proxy=$PROXY")
fi

log "=== 拉取 $REMOTE_URL（$BRANCH）重试 ${#git_net[@]} 个代理设置 ==="
git "${git_net[@]}" fetch --prune "$REMOTE_URL" "$BRANCH" >/dev/null 2>&1 \
    || die "拉取失败：检查网络或 $PROXY"
NEW=$(git rev-parse FETCH_HEAD) || die "拿不到 FETCH_HEAD"
OLD=$(git rev-parse HEAD)

if [ "$OLD" = "$NEW" ]; then
    log "✅ 已是最新：$(git log -1 --format='%h %s')"
    exit 0
fi

log "--- 待部署 ---"
git log --oneline --no-decorate "$OLD..$NEW" | sed 's/^/    /' | tee -a "$LOG" >/dev/null
git log --oneline --no-decorate "$OLD..$NEW" | sed 's/^/  /'

[ "$MODE" = "check" ] && { log "（--check：只看不部署）"; exit 0; }

# 工作区必须干净：有手改就停下，别让 ff 合并把改动顶掉
if [ -n "$(git status --porcelain)" ]; then
    if [ "$FORCE" = "1" ]; then
        log "⚠️ 工作区有改动，--force：先 stash（可用 git stash list 取回）"
        git stash push -u -m "deploy-$(date +%Y%m%d-%H%M%S)" >/dev/null || die "stash 失败"
    else
        git status --short | sed 's/^/  /'
        die "工作区有未提交的改动，先处理掉（或用 --force 让脚本 stash）"
    fi
fi

log "=== 合并代码 $OLD -> $NEW ==="
git merge --ff-only "$NEW" >/dev/null 2>&1 || die "不是快进合并，手动处理后再来"

# 要不要重建：只有文档/测试变了就不用（镜像里虽然有，但不影响运行）
needs_build=1
if [ "$FORCE_BUILD" = "0" ]; then
    changed=$(git diff --name-only "$OLD..$NEW")
    if [ -n "$changed" ] && ! printf '%s\n' "$changed" \
            | grep -vqE '^(docs/|tests/|\.github/)|\.md$'; then
        needs_build=0
    fi
fi

if [ "$needs_build" = "0" ]; then
    log "=== 只有文档/测试变了，不重建、不重启 ==="
    log "✅ 代码已更新到 $(git log -1 --format='%h %s')"
    exit 0
fi

# 记下当前镜像，失败时用它退回去（重建只要 30 秒，退镜像只要几秒）
PREV_IMAGE=$(docker image inspect "$SERVICE:local" --format '{{.Id}}' 2>/dev/null || true)
[ -n "$PREV_IMAGE" ] && docker tag "$PREV_IMAGE" "$SERVICE:rollback" >/dev/null 2>&1 || true

health() {
    local deadline=$((SECONDS + HEALTH_TIMEOUT))
    while [ $SECONDS -lt $deadline ]; do
        if curl -s -o /dev/null --max-time 3 "$HEALTH_URL"; then
            return 0
        fi
        sleep 2
    done
    return 1
}

rollback() {
    log "❌ $1 —— 回滚到 $OLD"
    git reset --hard "$OLD" >/dev/null 2>&1
    if [ -n "$PREV_IMAGE" ]; then
        docker tag "$SERVICE:rollback" "$SERVICE:local" >/dev/null 2>&1 || true
    fi
    docker compose up -d --force-recreate --no-deps "$SERVICE" >/dev/null 2>&1 || true
    if health; then
        log "↩️ 已回滚，服务恢复（健康检查通过）"
    else
        log "🔥 回滚后仍然不健康，需要人工介入"
    fi
    exit 1
}

if [ "$needs_build" = "1" ]; then
    log "=== 重建镜像 ==="
    if ! docker compose build "$SERVICE" 2>&1 | tail -5 | sed 's/^/    /'; then
        rollback "镜像构建失败"
    fi
fi

log "=== 重建容器（$SERVICE）==="
docker compose up -d --force-recreate --no-deps "$SERVICE" 2>&1 | tail -3 | sed 's/^/    /' \
    || rollback "容器起不来"

if health; then
    log "✅ 部署完成：$(git log -1 --format='%h %s')，健康检查通过"
else
    rollback "健康检查 ${HEALTH_TIMEOUT}s 内没通过"
fi
