"""Web Push 订阅存储 - SQLite push_subscriptions 表。

只负责存取，不碰推送网络；真正发推在 push_sender.py。
订阅与设备绑定，endpoint 唯一，重复订阅按 endpoint 覆盖密钥。
连续推送失败到阈值后标记 disabled，但保留记录，方便排障。
"""
import os
import sqlite3
import threading
from datetime import datetime

from config import PUSH_DB_PATH

DEFAULT_SPACE = "default"


def _now():
    return datetime.now().isoformat()


class PushStore:
    def __init__(self, db_path=PUSH_DB_PATH):
        if db_path:
            db_dir = os.path.dirname(db_path)
            if db_dir:
                os.makedirs(db_dir, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(db_path or ":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._create_schema()
        self.conn.commit()

    def _create_schema(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                endpoint TEXT NOT NULL UNIQUE,
                p256dh TEXT NOT NULL,
                auth TEXT NOT NULL,
                user_agent TEXT DEFAULT '',
                space_id TEXT DEFAULT 'default',
                created_at TEXT,
                last_ok_at TEXT,
                fail_count INTEGER DEFAULT 0,
                disabled INTEGER DEFAULT 0
            )
        """)
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_push_active "
            "ON push_subscriptions(space_id, disabled)"
        )

    @staticmethod
    def _valid_endpoint(endpoint):
        text = str(endpoint or "").strip()
        if not text.startswith("https://"):
            raise ValueError("endpoint 必须是 https 地址")
        if len(text) > 2000:
            raise ValueError("endpoint 过长")
        return text

    def save(self, endpoint, p256dh, auth, user_agent="", space_id=DEFAULT_SPACE):
        """按 endpoint upsert：重复订阅只刷新密钥，不产生新行。"""
        endpoint = self._valid_endpoint(endpoint)
        p256dh = str(p256dh or "").strip()
        auth = str(auth or "").strip()
        if not p256dh or not auth:
            raise ValueError("缺少 p256dh / auth")
        if len(p256dh) > 500 or len(auth) > 500:
            raise ValueError("订阅密钥过长")

        with self._lock:
            self.conn.execute(
                "INSERT INTO push_subscriptions "
                "(endpoint, p256dh, auth, user_agent, space_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(endpoint) DO UPDATE SET "
                "p256dh=excluded.p256dh, auth=excluded.auth, "
                "user_agent=excluded.user_agent, space_id=excluded.space_id, "
                "fail_count=0, disabled=0",
                (
                    endpoint,
                    p256dh,
                    auth,
                    str(user_agent or "")[:300],
                    space_id or DEFAULT_SPACE,
                    _now(),
                ),
            )
            self.conn.commit()
            row = self.conn.execute(
                "SELECT * FROM push_subscriptions WHERE endpoint = ?", (endpoint,)
            ).fetchone()
        return dict(row) if row else None

    def remove(self, endpoint):
        endpoint = str(endpoint or "").strip()
        if not endpoint:
            return False
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,)
            )
            self.conn.commit()
            return cur.rowcount > 0

    def get(self, endpoint):
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM push_subscriptions WHERE endpoint = ?",
                (str(endpoint or "").strip(),),
            ).fetchone()
        return dict(row) if row else None

    def list_active(self, space_id=None):
        sql = "SELECT * FROM push_subscriptions WHERE disabled = 0"
        args = []
        if space_id:
            sql += " AND space_id = ?"
            args.append(space_id)
        with self._lock:
            rows = self.conn.execute(sql + " ORDER BY id", args).fetchall()
        return [dict(row) for row in rows]

    def count_active(self, space_id=None):
        sql = "SELECT COUNT(*) FROM push_subscriptions WHERE disabled = 0"
        args = []
        if space_id:
            sql += " AND space_id = ?"
            args.append(space_id)
        with self._lock:
            return int(self.conn.execute(sql, args).fetchone()[0])

    def mark_success(self, endpoint):
        with self._lock:
            self.conn.execute(
                "UPDATE push_subscriptions SET last_ok_at = ?, fail_count = 0, "
                "disabled = 0 WHERE endpoint = ?",
                (_now(), str(endpoint or "").strip()),
            )
            self.conn.commit()

    def mark_failure(self, endpoint, max_failures=5):
        """累加失败次数；到阈值就禁用。返回最新的 fail_count。"""
        endpoint = str(endpoint or "").strip()
        with self._lock:
            self.conn.execute(
                "UPDATE push_subscriptions SET fail_count = fail_count + 1 "
                "WHERE endpoint = ?",
                (endpoint,),
            )
            row = self.conn.execute(
                "SELECT fail_count FROM push_subscriptions WHERE endpoint = ?",
                (endpoint,),
            ).fetchone()
            fails = int(row[0]) if row else 0
            if fails >= max(1, int(max_failures)):
                self.conn.execute(
                    "UPDATE push_subscriptions SET disabled = 1 WHERE endpoint = ?",
                    (endpoint,),
                )
            self.conn.commit()
        return fails

    def close(self):
        with self._lock:
            self.conn.close()
