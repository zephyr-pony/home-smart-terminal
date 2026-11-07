"""通用任务与提醒 - SQLite tasks 表。

任务类型：bill（账单）/ schedule（日程）/ custom（自定义提醒）。
到期提醒与 Items 类似：领取时落库 reminded_at，按冷却时间避免重复提醒。
"""
import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta

from config import TASKS_DB_PATH

TASK_TYPES = ("bill", "schedule", "custom")
ACTIVE_STATUSES = ("pending", "reminded")


def _parse_due(value):
    if value in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise ValueError("时间必须是 YYYY-MM-DD 或 YYYY-MM-DDTHH:MM")


class Tasks:
    def __init__(self, db_path=TASKS_DB_PATH):
        if db_path:
            db_dir = os.path.dirname(db_path)
            if db_dir:
                os.makedirs(db_dir, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(db_path or ":memory:", check_same_thread=False)
        self._create_schema()
        self.conn.commit()

    def _create_schema(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                title TEXT NOT NULL,
                due_at TEXT,
                status TEXT DEFAULT 'pending',
                entity_id INTEGER,
                payload TEXT DEFAULT '{}',
                created_at TEXT,
                reminded_at TEXT,
                completed_at TEXT
            )
        """)
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_due "
            "ON tasks(status, due_at)"
        )

    def add(self, title, due_at=None, type="custom", entity_id=None, payload=None):
        title = str(title or "").strip()
        if not title:
            raise ValueError("任务标题不能为空")
        task_type = str(type or "custom").strip()
        if task_type not in TASK_TYPES:
            raise ValueError(f"任务类型必须是 {'/'.join(TASK_TYPES)}")

        due = _parse_due(due_at)
        ts = datetime.now().isoformat()
        payload_json = json.dumps(payload or {}, ensure_ascii=False)

        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO tasks "
                "(type, title, due_at, status, entity_id, payload, created_at) "
                "VALUES (?, ?, ?, 'pending', ?, ?, ?)",
                (
                    task_type,
                    title,
                    due.isoformat() if due else None,
                    entity_id,
                    payload_json,
                    ts,
                ),
            )
            self.conn.commit()
            task_id = cur.lastrowid

        return {
            "id": task_id,
            "type": task_type,
            "title": title,
            "due_at": due.isoformat() if due else None,
            "status": "pending",
        }

    def list_all(self, status=None):
        with self._lock:
            if status:
                rows = self.conn.execute(
                    "SELECT id, type, title, due_at, status, entity_id, payload, "
                    "       created_at, reminded_at, completed_at "
                    "FROM tasks WHERE status=? "
                    "ORDER BY (due_at IS NULL), due_at, id",
                    (status,),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT id, type, title, due_at, status, entity_id, payload, "
                    "       created_at, reminded_at, completed_at "
                    "FROM tasks ORDER BY (due_at IS NULL), due_at, id"
                ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    @staticmethod
    def _row_to_dict(row):
        return {
            "id": row[0],
            "type": row[1],
            "title": row[2],
            "due_at": row[3],
            "status": row[4],
            "entity_id": row[5],
            "payload": json.loads(row[6] or "{}"),
            "created_at": row[7],
            "reminded_at": row[8],
            "completed_at": row[9],
        }

    def complete(self, task_id):
        with self._lock:
            cur = self.conn.execute(
                "UPDATE tasks SET status='done', completed_at=? "
                "WHERE id=? AND status <> 'done'",
                (datetime.now().isoformat(), task_id),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def cancel(self, task_id):
        with self._lock:
            cur = self.conn.execute(
                "UPDATE tasks SET status='cancelled' "
                "WHERE id=? AND status NOT IN ('done', 'cancelled')",
                (task_id,),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def update(self, task_id, title=None, due_at=None, type=None):
        """更新任务字段；due_at 传空字符串表示清空时间。"""
        fields = []
        params = []

        if title is not None:
            title = str(title).strip()
            if not title:
                raise ValueError("任务标题不能为空")
            fields.append("title=?")
            params.append(title)

        if type is not None:
            task_type = str(type).strip()
            if task_type not in TASK_TYPES:
                raise ValueError(f"任务类型必须是 {'/'.join(TASK_TYPES)}")
            fields.append("type=?")
            params.append(task_type)

        if due_at is not None:
            due = _parse_due(due_at)
            fields.append("due_at=?")
            params.append(due.isoformat() if due else None)

        if not fields:
            return False

        with self._lock:
            cur = self.conn.execute(
                "UPDATE tasks SET " + ", ".join(fields) + " WHERE id=?",
                params + [task_id],
            )
            self.conn.commit()
            return cur.rowcount > 0

    def delete_by_id(self, task_id):
        with self._lock:
            row = self.conn.execute(
                "SELECT id, type, title, due_at, status FROM tasks WHERE id=?",
                (task_id,),
            ).fetchone()
            if not row:
                return None
            self.conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            self.conn.commit()
        return {
            "id": row[0],
            "type": row[1],
            "title": row[2],
            "due_at": row[3],
            "status": row[4],
        }

    def count(self, status=None):
        with self._lock:
            if status:
                row = self.conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE status=?", (status,)
                ).fetchone()
            else:
                row = self.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()
        return row[0]

    def get_due(self, days=3, now=None):
        now = now or datetime.now()
        today = now.date()
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, type, title, due_at, status, entity_id, payload, "
                "       created_at, reminded_at, completed_at "
                "FROM tasks WHERE due_at IS NOT NULL AND status IN (?, ?)",
                ACTIVE_STATUSES,
            ).fetchall()

        result = []
        for row in rows:
            task = self._row_to_dict(row)
            try:
                due = datetime.fromisoformat(task["due_at"])
            except (TypeError, ValueError):
                continue
            days_left = (due.date() - today).days
            if days_left <= days:
                task["days_left"] = days_left
                result.append(task)
        result.sort(key=lambda task: (task["days_left"], task["due_at"], task["id"]))
        return result

    def claim_due_reminders(self, days=3, cooldown_hours=24, now=None):
        now = now or datetime.now()
        today = now.date()

        if cooldown_hours and cooldown_hours > 0:
            cutoff = (now - timedelta(hours=cooldown_hours)).isoformat()
            where = "(reminded_at IS NULL OR reminded_at <= ?)"
            params = (cutoff,)
        else:
            where = "reminded_at IS NULL"
            params = ()

        with self._lock:
            rows = self.conn.execute(
                "SELECT id, type, title, due_at, status, entity_id, payload, "
                "       created_at, reminded_at, completed_at "
                "FROM tasks WHERE due_at IS NOT NULL "
                "AND status IN (?, ?) AND " + where,
                ACTIVE_STATUSES + params,
            ).fetchall()

            claimed = []
            for row in rows:
                task = self._row_to_dict(row)
                try:
                    due = datetime.fromisoformat(task["due_at"])
                except (TypeError, ValueError):
                    continue
                days_left = (due.date() - today).days
                if days_left <= days:
                    task["days_left"] = days_left
                    claimed.append(task)

            if claimed:
                ids = [task["id"] for task in claimed]
                placeholders = ",".join("?" * len(ids))
                self.conn.execute(
                    "UPDATE tasks SET reminded_at=?, "
                    "status=CASE WHEN status='pending' THEN 'reminded' ELSE status END "
                    f"WHERE id IN ({placeholders})",
                    [now.isoformat()] + ids,
                )
                self.conn.commit()

        claimed.sort(key=lambda task: (task["days_left"], task["due_at"], task["id"]))
        return claimed

    def close(self):
        with self._lock:
            self.conn.close()
