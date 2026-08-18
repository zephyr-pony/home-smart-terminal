"""物品管理模块 - SQLite items 表（结构化：名称 + 到期日期）

与 memory（自然语言记忆）互补：物品到期信息存这里，精确匹配优先。
线程安全：check_same_thread=False + RLock（仿 memory.py）。
"""
import sqlite3
import os
import threading
from datetime import datetime, date

from config import ITEMS_DB_PATH


class Items:
    def __init__(self, db_path=ITEMS_DB_PATH):
        """初始化物品库。"""
        if db_path:
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(db_path or ":memory:", check_same_thread=False)
        self._create_schema()
        self.conn.commit()

    def _create_schema(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                expiry_date TEXT,          -- YYYY-MM-DD，可为空（不明）
                status TEXT DEFAULT 'active',  -- active / reminded / expired
                created_at TEXT,
                reminded_at TEXT
            )
        """)

    # ---------- 写操作 ----------

    def add(self, name, expiry_date=None):
        """添加一个物品。expiry_date: 'YYYY-MM-DD' 或 None。

        Returns:
            dict: 新物品记录
        """
        ts = datetime.now().isoformat()
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO items (name, expiry_date, status, created_at) VALUES (?, ?, 'active', ?)",
                (name, expiry_date, ts),
            )
            self.conn.commit()
            rid = cur.lastrowid
        return {"id": rid, "name": name, "expiry_date": expiry_date, "status": "active"}

    def delete(self, name):
        """按名称删除物品（模糊匹配）。返回被删记录列表。"""
        with self._lock:
            hits = self.conn.execute(
                "SELECT id, name, expiry_date, status FROM items WHERE name LIKE ?",
                (f"%{name}%",),
            ).fetchall()
            if not hits:
                return []
            ids = [h[0] for h in hits]
            placeholders = ",".join("?" * len(ids))
            self.conn.execute(f"DELETE FROM items WHERE id IN ({placeholders})", ids)
            self.conn.commit()
        return [
            {"id": h[0], "name": h[1], "expiry_date": h[2], "status": h[3]} for h in hits
        ]

    # ---------- 读操作 ----------

    def list_all(self, status=None):
        """列出物品。status: active/reminded/expired/None=全部。"""
        with self._lock:
            if status:
                rows = self.conn.execute(
                    "SELECT id, name, expiry_date, status, created_at FROM items WHERE status=? ORDER BY id DESC",
                    (status,),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT id, name, expiry_date, status, created_at FROM items ORDER BY id DESC"
                ).fetchall()
        return [
            {"id": r[0], "name": r[1], "expiry_date": r[2], "status": r[3], "created_at": r[4]}
            for r in rows
        ]

    def search(self, keyword):
        """按名称模糊查找物品。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, name, expiry_date, status, created_at FROM items WHERE name LIKE ?",
                (f"%{keyword}%",),
            ).fetchall()
        return [
            {"id": r[0], "name": r[1], "expiry_date": r[2], "status": r[3], "created_at": r[4]}
            for r in rows
        ]

    def get_due(self, days=3):
        """获取即将到期（含已过期）的物品。

        Args:
            days: 未来几天内算"即将到期"

        Returns:
            list[dict]: 每条含 days_left 字段（负数=已过期）
        """
        today = date.today()
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, name, expiry_date, status FROM items WHERE expiry_date IS NOT NULL"
            ).fetchall()
        result = []
        for rid, name, exp, status in rows:
            try:
                exp_date = date.fromisoformat(exp)
            except ValueError:
                continue
            days_left = (exp_date - today).days
            if days_left <= days:
                result.append({
                    "id": rid, "name": name, "expiry_date": exp,
                    "status": status, "days_left": days_left,
                })
        result.sort(key=lambda x: x["days_left"])
        return result

    def count(self):
        """物品总数。"""
        with self._lock:
            return self.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]

    def close(self):
        with self._lock:
            self.conn.close()