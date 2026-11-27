"""对话记录（D84）：只留最近几天，给跨天上下文和夜间画像提炼当原料。

为什么单独一个库：
- 对话不是用户资产——保留期一到就删，和 memories（长期记忆）混在一起会让
  「蒸馏只动摘要」和「热查询」两件事都变复杂；
- 单独一个文件也方便「一键清空」和整库删除。

为什么不留全量历史：语音助手在家里一直开着，一天几十句、一年几 MB 不算多，
但原文里有外人说的话、健康、钱。画像需要的是「提炼出来的判断」，不是原话；
原话只需要活到「昨夜提炼」和「昨天说到哪」够用，默认 7 天。
"""
import os
import sqlite3
import threading
from datetime import datetime, timedelta

from config import DIALOGUE_DB_PATH, DIALOGUE_ENABLED, DIALOGUE_KEEP_DAYS

# 只收这两类；工具调用、系统提示、设备事件不进这里
ROLES = ("user", "assistant")


class DialogueStore:
    def __init__(self, path=None, keep_days=None, enabled=None):
        self.path = path or DIALOGUE_DB_PATH
        self.keep_days = DIALOGUE_KEEP_DAYS if keep_days is None else int(keep_days)
        self.enabled = bool(DIALOGUE_ENABLED if enabled is None else enabled)
        self._lock = threading.RLock()
        self.conn = None
        if not self.enabled:
            return
        folder = os.path.dirname(self.path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        with self._lock:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_id TEXT DEFAULT '',
                    role TEXT NOT NULL,
                    text TEXT NOT NULL,
                    at TEXT NOT NULL
                )
            """)
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_turns_at ON turns (at)"
            )
        # 启动时先清一次：服务停了一周再开，不该把过期原文留着
        self.purge()

    def add(self, client_id, role, text, at=None):
        """记一句；返回是否真的写入（关掉、空文本、坏角色都返回 False）。"""
        if self.conn is None:
            return False
        role = str(role or "").strip()
        text = str(text or "").strip()
        if role not in ROLES or not text:
            return False
        at = at or datetime.now().isoformat()
        with self._lock:
            self.conn.execute(
                "INSERT INTO turns (client_id, role, text, at) VALUES (?, ?, ?, ?)",
                (str(client_id or "")[:128], role, text[:2000], at),
            )
            self.conn.commit()
        return True

    def since(self, since_iso, limit=500):
        """取某个时间点之后的对话，按时间正序（给提炼读整段上下文）。"""
        if self.conn is None:
            return []
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, client_id, role, text, at FROM turns "
                "WHERE at >= ? ORDER BY id ASC LIMIT ?",
                (since_iso, int(limit)),
            ).fetchall()
        return [self._row(row) for row in rows]

    def recent(self, limit=50, client_id=None):
        """最近几条，按时间倒序（网页上「存了什么」用）。"""
        if self.conn is None:
            return []
        sql = "SELECT id, client_id, role, text, at FROM turns"
        params = []
        if client_id:
            sql += " WHERE client_id=?"
            params.append(client_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._row(row) for row in rows]

    def count(self):
        if self.conn is None:
            return 0
        with self._lock:
            row = self.conn.execute("SELECT COUNT(*) FROM turns").fetchone()
        return row[0] if row else 0

    def purge(self, now=None):
        """删掉超过保留期的原文，返回删了几条。"""
        if self.conn is None or self.keep_days is None:
            return 0
        if self.keep_days <= 0:
            # 0 表示不留：整库清掉，但仍允许本轮记录（设置页能立刻看到效果）
            cutoff = (now or datetime.now()).isoformat()
        else:
            cutoff = (
                (now or datetime.now()) - timedelta(days=self.keep_days)
            ).isoformat()
        with self._lock:
            cur = self.conn.execute("DELETE FROM turns WHERE at < ?", (cutoff,))
            self.conn.commit()
            return cur.rowcount

    def clear(self):
        """一键清空（设置页「清空对话记录」）。"""
        if self.conn is None:
            return 0
        with self._lock:
            cur = self.conn.execute("DELETE FROM turns")
            self.conn.commit()
            return cur.rowcount

    @staticmethod
    def _row(row):
        return {
            "id": row[0],
            "client_id": row[1],
            "role": row[2],
            "text": row[3],
            "at": row[4],
        }

    def close(self):
        if self.conn is None:
            return
        with self._lock:
            self.conn.close()
            self.conn = None
