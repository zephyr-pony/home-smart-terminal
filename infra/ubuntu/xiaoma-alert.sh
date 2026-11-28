#!/bin/bash
# 本机出问题时给手机推一条通知（走小马的 Web Push）。
#
# 用途：备份上传失败这类「没人盯着就永远不知道」的事。告警本身不能把主流程搞挂，
# 所以这里任何失败都吞掉：拿不到 token、服务没起来、推送发不出去，都只是没通知。
#
# 用法：xiaoma-alert.sh "标题" "正文"
#   XIAOMA_BASE    服务地址，默认 http://127.0.0.1:8208
#   XIAOMA_ENV     读 API_TOKEN 的 .env，默认 /opt/opencode-my/.env
set -u

TITLE=${1:-小马}
BODY=${2:-}
[ -n "$BODY" ] || exit 0

BASE=${XIAOMA_BASE:-http://127.0.0.1:8208}
ENV_FILE=${XIAOMA_ENV:-/opt/opencode-my/.env}

TOKEN=$(grep -m1 '^API_TOKEN=' "$ENV_FILE" 2>/dev/null | cut -d= -f2-)
[ -n "$TOKEN" ] || exit 0

# 用 python 拼 JSON：错误信息里带引号、反斜杠是常事，手拼一定出错
PAYLOAD=$(python3 - "$TITLE" "$BODY" <<'PY' 2>/dev/null
import json, sys
print(json.dumps({"title": sys.argv[1][:40], "body": sys.argv[2][:500]}, ensure_ascii=False))
PY
)
[ -n "$PAYLOAD" ] || exit 0

curl -s -m 10 -X POST "$BASE/api/push/notify" \
    -H "Authorization: Bearer $TOKEN" \
    -H 'Content-Type: application/json' \
    --data "$PAYLOAD" >/dev/null 2>&1 || true
exit 0
