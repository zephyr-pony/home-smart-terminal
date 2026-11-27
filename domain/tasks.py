"""通用任务与提醒 - SQLite tasks 表。

任务类型：bill（账单）/ schedule（日程）/ custom（自定义提醒）。
到期提醒与 Items 类似：领取时落库 reminded_at，按冷却时间避免重复提醒。
"""
import calendar
import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta

from config import TASKS_DB_PATH

TASK_TYPES = ("bill", "schedule", "custom")
# 周期提醒：留空 = 一次性。只支持这三种，够家庭场景用，不做完整 RRULE。
RECUR_TYPES = ("", "daily", "weekly", "monthly")
# 提醒方式（D81）：timely = 到点就响（吃药、出门）；soft = 聊天时顺口带一句
# （牛奶过期、取快递），到晚上还没说才落到准点提醒。软硬由模型按工具描述判，
# 这里只负责存和筛；老数据迁移后默认 timely，行为不变。
TASK_MODES = ("timely", "soft")
DEFAULT_MODE = "timely"
ACTIVE_STATUSES = ("pending", "reminded")

_COLUMNS = (
    "id, type, title, due_at, status, entity_id, payload, "
    "created_at, reminded_at, completed_at, recur, mode"
)


def _parse_due(value):
    if value in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise ValueError("时间必须是 YYYY-MM-DD 或 YYYY-MM-DDTHH:MM")


def _keep_clock(new_due, old_value, raw_value):
    """只给了日期时保留原来的时刻。

    用户说「取快递改到后天」，指的是还是下午三点那个提醒，不是改成零点。
    只有原话里带了时间（raw_value 含 T）才覆盖时刻。
    """
    if "T" in str(raw_value or ""):
        return new_due
    try:
        old = datetime.fromisoformat(str(old_value))
    except (TypeError, ValueError):
        return new_due
    if old.hour or old.minute:
        return new_due.replace(hour=old.hour, minute=old.minute)
    return new_due


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
                completed_at TEXT,
                recur TEXT DEFAULT '',
                mode TEXT DEFAULT 'timely'
            )
        """)
        # 老库补列：2026-11-23 之前建的 tasks 只有一次性任务
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(tasks)")}
        if "recur" not in columns:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN recur TEXT DEFAULT ''")
        # 软提醒（D81）：老数据一律 timely，升级后提醒时间不变
        if "mode" not in columns:
            self.conn.execute(
                "ALTER TABLE tasks ADD COLUMN mode TEXT DEFAULT '%s'" % DEFAULT_MODE
            )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_due "
            "ON tasks(status, due_at)"
        )

    def add(self, title, due_at=None, type="custom", entity_id=None, payload=None,
            recur="", mode=DEFAULT_MODE):
        title = str(title or "").strip()
        if not title:
            raise ValueError("任务标题不能为空")
        task_type = str(type or "custom").strip()
        if task_type not in TASK_TYPES:
            raise ValueError(f"任务类型必须是 {'/'.join(TASK_TYPES)}")
        recur = str(recur or "").strip()
        if recur not in RECUR_TYPES:
            raise ValueError("周期只能是 daily/weekly/monthly 或留空")
        if recur and not _parse_due(due_at):
            raise ValueError("周期任务要有下一次的时间")
        mode = str(mode or DEFAULT_MODE).strip()
        if mode not in TASK_MODES:
            raise ValueError("提醒方式只能是 timely/soft 或留空")

        due = _parse_due(due_at)
        ts = datetime.now().isoformat()
        payload_json = json.dumps(payload or {}, ensure_ascii=False)

        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO tasks "
                "(type, title, due_at, status, entity_id, payload, created_at, recur, mode) "
                "VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?)",
                (
                    task_type,
                    title,
                    due.isoformat() if due else None,
                    entity_id,
                    payload_json,
                    ts,
                    recur,
                    mode,
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
            "recur": recur,
            "mode": mode,
        }

    def list_all(self, status=None):
        with self._lock:
            if status:
                rows = self.conn.execute(
                    "SELECT " + _COLUMNS + " FROM tasks WHERE status=? "
                    "ORDER BY (due_at IS NULL), due_at, id",
                    (status,),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT " + _COLUMNS + " FROM tasks "
                    "ORDER BY (due_at IS NULL), due_at, id"
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
            "recur": row[10] or "",
            "mode": row[11] or DEFAULT_MODE,
        }

    def get(self, task_id):
        with self._lock:
            row = self.conn.execute(
                "SELECT " + _COLUMNS + " FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
        return self._row_to_dict(row) if row else None

    @staticmethod
    def _advance(due, recur):
        """下一次的时间：daily/weekly 直接加；monthly 遇到小月用当月最后一天。"""
        if recur == "daily":
            return due + timedelta(days=1)
        if recur == "weekly":
            return due + timedelta(days=7)
        if recur == "monthly":
            total = due.year * 12 + due.month - 1 + 1
            year, month = total // 12, total % 12 + 1
            day = min(due.day, calendar.monthrange(year, month)[1])
            return due.replace(year=year, month=month, day=day)
        return due

    def _next_after(self, due_at, recur, now):
        """把时间推到 now 之后的第一跳；错过多次也只补一次提醒。"""
        try:
            due = datetime.fromisoformat(str(due_at))
        except (TypeError, ValueError):
            return None
        if not recur:
            return None
        guard = 0
        while due <= now and guard < 1000:
            due = self._advance(due, recur)
            guard += 1
        return due

    def complete(self, task_id, now=None):
        """完成：一次性任务标记 done；周期任务只跳过这一次，滚到下一次。"""
        now = now or datetime.now()
        with self._lock:
            row = self.conn.execute(
                "SELECT " + _COLUMNS + " FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if not row:
                return False
            task = self._row_to_dict(row)
            if task["status"] == "done":
                return False
            if task["recur"]:
                nxt = self._next_after(task["due_at"], task["recur"], now)
                if nxt is None:
                    return False
                self.conn.execute(
                    "UPDATE tasks SET due_at=?, reminded_at=NULL, status='pending' "
                    "WHERE id=?",
                    (nxt.isoformat(), task_id),
                )
            else:
                self.conn.execute(
                    "UPDATE tasks SET status='done', completed_at=? "
                    "WHERE id=? AND status <> 'done'",
                    (now.isoformat(), task_id),
                )
            self.conn.commit()
            return True

    def cancel(self, task_id):
        with self._lock:
            cur = self.conn.execute(
                "UPDATE tasks SET status='cancelled' "
                "WHERE id=? AND status NOT IN ('done', 'cancelled')",
                (task_id,),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def postpone(self, task_id, days=1, due_at=None, now=None):
        """把任务顺延或改到指定时间，返回更新后的任务。

        两种语义要分开：
        - 给了 due_at 就按绝对时间改（用户说「明天再说」「下周一」）
        - 没给才按「当前到期日 + days」顺延（用户说「再往后推三天」）

        两种情况都要清 reminded_at 并把状态退回 pending，否则会被冷却窗口挡住。
        """
        new_due = None
        if due_at:
            new_due = _parse_due(due_at)
            if new_due is None:
                raise ValueError("时间格式无效")
        else:
            if days is None:
                days = 1
            try:
                days = int(days)
            except (TypeError, ValueError):
                raise ValueError("顺延天数必须是整数")
            if days <= 0:
                raise ValueError("顺延天数必须是正整数")

        now = now or datetime.now()
        with self._lock:
            row = self.conn.execute(
                "SELECT " + _COLUMNS + " FROM tasks WHERE id=?",
                (task_id,),
            ).fetchone()
            if not row:
                return None
            if row[4] in ("done", "cancelled"):
                return None
            if new_due is None:
                try:
                    base = datetime.fromisoformat(row[3]) if row[3] else now
                except (TypeError, ValueError):
                    base = now
                new_due = base + timedelta(days=days)
            elif due_at:
                # 「改到后天」没说几点：保留原来的时刻
                new_due = _keep_clock(new_due, row[3], due_at)
            self.conn.execute(
                "UPDATE tasks SET due_at=?, reminded_at=NULL, status='pending' "
                "WHERE id=?",
                (new_due.isoformat(), task_id),
            )
            self.conn.commit()
            updated = self.conn.execute(
                "SELECT " + _COLUMNS + " FROM tasks WHERE id=?",
                (task_id,),
            ).fetchone()
        return self._row_to_dict(updated)

    def update(self, task_id, title=None, due_at=None, type=None, mode=None):
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

        if mode is not None:
            mode = str(mode).strip()
            if mode not in TASK_MODES:
                raise ValueError("提醒方式只能是 timely/soft")
            fields.append("mode=?")
            params.append(mode)

        with self._lock:
            if due_at is not None:
                due = _parse_due(due_at)
                if due is not None:
                    row = self.conn.execute(
                        "SELECT due_at FROM tasks WHERE id=?", (task_id,)
                    ).fetchone()
                    if row:
                        # 只给了日期：保留原来的时刻，别把下午三点改成零点
                        due = _keep_clock(due, row[0], due_at)
                fields.append("due_at=?")
                params.append(due.isoformat() if due else None)

            if not fields:
                return False

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
                "SELECT " + _COLUMNS + " FROM tasks "
                "WHERE due_at IS NOT NULL AND status IN (?, ?)",
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

    def get_soft_due(self, limit=5, now=None):
        """今天到期或已经过期的软提醒（注入对话用，D81）。

        只给「今天该提」的：未来的排进上下文没有意义（模型会提前念叨），
        周期任务也不进来——「每天八点吃药」是准点提醒，不该变成聊天里的话题。
        排序按逾期程度，最该处理的排前面。
        """
        now = now or datetime.now()
        today = now.date()
        with self._lock:
            rows = self.conn.execute(
                "SELECT " + _COLUMNS + " FROM tasks "
                "WHERE due_at IS NOT NULL AND status IN (?, ?) "
                "AND mode=? AND (recur IS NULL OR recur='')",
                ACTIVE_STATUSES + ("soft",),
            ).fetchall()

        result = []
        for row in rows:
            task = self._row_to_dict(row)
            try:
                due = datetime.fromisoformat(task["due_at"])
            except (TypeError, ValueError):
                continue
            days_left = (due.date() - today).days
            if days_left > 0:
                continue
            task["days_left"] = days_left
            result.append(task)
        result.sort(key=lambda task: (task["days_left"], task["due_at"], task["id"]))
        return result[:limit] if limit and limit > 0 else result

    def claim_due_reminders(self, days=3, cooldown_hours=24, now=None, mode=None):
        """领取需要提醒的任务。

        mode 为 None 时不筛（老行为）；服务端按两段调用：timely 一直跑，
        soft 要等 SOFT_REMINDER_LAST_CALL 之后才领（D81 的晚间兜底）。
        """
        now = now or datetime.now()
        today = now.date()

        if cooldown_hours and cooldown_hours > 0:
            cutoff = (now - timedelta(hours=cooldown_hours)).isoformat()
            where = "(reminded_at IS NULL OR reminded_at <= ?)"
            params = (cutoff,)
        else:
            where = "reminded_at IS NULL"
            params = ()

        if mode:
            where += " AND mode=?"
            params = params + (mode,)

        with self._lock:
            rows = self.conn.execute(
                "SELECT " + _COLUMNS + " FROM tasks WHERE due_at IS NOT NULL "
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
                if task["recur"]:
                    # 周期提醒到点才响，不像一次性任务那样提前几天提醒：
                    # 「每天八点吃药」提前一天说没有意义。
                    if due <= now:
                        task["days_left"] = (due.date() - today).days
                        claimed.append(task)
                    continue
                days_left = (due.date() - today).days
                if days_left <= days:
                    task["days_left"] = days_left
                    claimed.append(task)

            one_shot = [task for task in claimed if not task["recur"]]
            if one_shot:
                ids = [task["id"] for task in one_shot]
                placeholders = ",".join("?" * len(ids))
                self.conn.execute(
                    "UPDATE tasks SET reminded_at=?, "
                    "status=CASE WHEN status='pending' THEN 'reminded' ELSE status END "
                    f"WHERE id IN ({placeholders})",
                    [now.isoformat()] + ids,
                )
            # 周期任务提醒完就滚到下一次，并清掉冷却标记——否则下次会被 24h 冷却挡住
            for task in claimed:
                if not task["recur"]:
                    continue
                nxt = self._next_after(task["due_at"], task["recur"], now)
                if nxt is None:
                    continue
                self.conn.execute(
                    "UPDATE tasks SET due_at=?, reminded_at=NULL, status='pending' "
                    "WHERE id=?",
                    (nxt.isoformat(), task["id"]),
                )
            if claimed:
                self.conn.commit()

        claimed.sort(key=lambda task: (task["days_left"], task["due_at"], task["id"]))
        return claimed

    def close(self):
        with self._lock:
            self.conn.close()
