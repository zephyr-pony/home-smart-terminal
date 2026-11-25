"""「这轮有没有开思考」的记录（D63）。

为什么单独记一份：思考开关交给模型判断之后，效果好不好只能靠观察——
哪类问题它开了、哪类没开、开了之后是不是真的更稳。先落成 JSONL，用
`GET /api/thinking` 或直接看 `data/thinking.jsonl` 都行，不急着建表。

不写库的理由：这是调试观测数据，不是用户资产；按大小滚动，删了也不心疼。
"""
import json
import os
import threading
import time

from config import DATA_DIR, THINKING_LOG_ENABLED

ENABLED = bool(THINKING_LOG_ENABLED)
MAX_BYTES = 5 * 1024 * 1024
LOG_PATH = os.path.join(DATA_DIR, "thinking.jsonl")
_lock = threading.Lock()


def record_turn(text, client_id, thinking_on, why, tools, seconds, result):
    """记一轮：问的什么、开没开思考、为什么、用了哪些工具、耗时。"""
    if not ENABLED:
        return
    entry = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "client": str(client_id or "")[:40],
        "text": str(text or "").strip()[:120],
        "thinking": bool(thinking_on),
        "why": str(why or "")[:120],
        "tools": list(tools or [])[:10],
        "seconds": round(float(seconds or 0), 1),
        "type": (result or {}).get("type", ""),
        "action": (result or {}).get("action", ""),
    }
    line = json.dumps(entry, ensure_ascii=False)
    try:
        with _lock:
            folder = os.path.dirname(LOG_PATH)
            if folder:
                os.makedirs(folder, exist_ok=True)
            if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > MAX_BYTES:
                os.replace(LOG_PATH, LOG_PATH + ".1")
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except OSError:
        # 观测数据写不进去不该影响正常回答
        pass


def tail(limit=50):
    """最近 N 条，新的在后。文件不存在返回空列表。"""
    try:
        limit = max(1, min(int(limit or 50), 500))
    except (TypeError, ValueError):
        limit = 50
    try:
        with _lock:
            with open(LOG_PATH, "r", encoding="utf-8") as fh:
                lines = fh.readlines()[-limit:]
    except OSError:
        return []
    items = []
    for line in lines:
        try:
            items.append(json.loads(line))
        except ValueError:
            continue
    return items


def summary(limit=200):
    """粗统计：开了多少、没开多少，各自平均耗时。"""
    items = tail(limit)
    on = [item for item in items if item.get("thinking")]
    off = [item for item in items if not item.get("thinking")]
    def avg(rows):
        if not rows:
            return 0
        return round(sum(float(row.get("seconds") or 0) for row in rows) / len(rows), 1)
    return {
        "total": len(items),
        "thinking": len(on),
        "no_thinking": len(off),
        "avg_seconds_thinking": avg(on),
        "avg_seconds_no_thinking": avg(off),
    }
