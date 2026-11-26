"""打印记录：上传/生成的文件和出纸结果（D65）。

为什么落库：文件原来放 `/tmp`，容器一重建就没了，「再打一份」只能让用户重传；
而「打过什么、出没出纸」也查不到。现在文件放 `DATA_DIR/print`（在卷里），
元数据进 `prints.db`，重启/重建容器都还在。

保留策略（两个旋钮）：
- `PRINT_KEEP_DAYS`：文件留几天，过期连文件一起删（默认 7 天，0 表示只留记录不删文件）
- `PRINT_REUSE_HOURS`：多久内算「刚才的文件」，说「再打一份」时能自动接上（默认 24 小时）

历史记录本身一直留着：一条只有名字/时间/份数/结果，几十字节。
"""
import os
import sqlite3
import threading
import time

from config import PRINT_KEEP_DAYS, PRINTS_DB_PATH

# ready 已上传/已生成；printing 已提交；printed 出纸成功；failed 失败；cancelled 用户取消
STATUSES = ("ready", "printing", "printed", "failed", "cancelled")


def _now():
    return time.time()


class PrintStore:
    def __init__(self, path=None, keep_days=None):
        self.path = path or PRINTS_DB_PATH
        self.keep_days = PRINT_KEEP_DAYS if keep_days is None else keep_days
        self._lock = threading.RLock()
        folder = os.path.dirname(self.path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS print_jobs (
                    id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    client_id TEXT DEFAULT '',
                    name TEXT NOT NULL,
                    kind TEXT DEFAULT '',
                    path TEXT NOT NULL,
                    size INTEGER DEFAULT 0,
                    pages INTEGER,
                    copies INTEGER DEFAULT 1,
                    status TEXT DEFAULT 'ready',
                    printer TEXT DEFAULT '',
                    job_id TEXT DEFAULT '',
                    error TEXT DEFAULT '',
                    source TEXT DEFAULT 'upload'
                )
            """)
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_print_jobs_created "
                "ON print_jobs (created_at DESC)"
            )
            self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()

    # ---------- 写入 ----------

    def add(self, file_id, name, path, kind="", size=0, pages=None,
            client_id="", source="upload", status="ready"):
        row = {
            "id": str(file_id),
            "created_at": _now(),
            "client_id": (client_id or "").strip(),
            "name": str(name or os.path.basename(path) or "文件"),
            "kind": kind or "",
            "path": path,
            "size": int(size or 0),
            "pages": pages,
            "copies": 1,
            "status": status,
            "printer": "",
            "job_id": "",
            "error": "",
            "source": source,
        }
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO print_jobs "
                "(id, created_at, client_id, name, kind, path, size, pages, "
                " copies, status, printer, job_id, error, source) "
                "VALUES (:id, :created_at, :client_id, :name, :kind, :path, "
                " :size, :pages, :copies, :status, :printer, :job_id, :error, :source)",
                row,
            )
            self._conn.commit()
        return self.get(row["id"])

    def mark(self, file_id, status, copies=None, printer=None, job_id=None,
             error=""):
        fields = ["status = ?"]
        values = [status]
        if copies is not None:
            fields.append("copies = ?")
            values.append(int(copies))
        if printer is not None:
            fields.append("printer = ?")
            values.append(str(printer))
        if job_id is not None:
            fields.append("job_id = ?")
            values.append(str(job_id))
        if error is not None:
            fields.append("error = ?")
            values.append(str(error)[:300])
        values.append(str(file_id))
        with self._lock:
            self._conn.execute(
                "UPDATE print_jobs SET %s WHERE id = ?" % ", ".join(fields),
                values,
            )
            self._conn.commit()

    # ---------- 查询 ----------

    def get(self, file_id, client_id=""):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM print_jobs WHERE id = ?", (str(file_id or ""),)
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        owner = item.get("client_id") or ""
        if owner and client_id and owner != str(client_id).strip():
            return None
        if not os.path.exists(item.get("path") or ""):
            return None
        return item

    def latest(self, client_id=""):
        """最近一条还能用的文件（文件还在磁盘上）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM print_jobs ORDER BY created_at DESC LIMIT 20"
            ).fetchall()
        for row in rows:
            item = dict(row)
            if not os.path.exists(item.get("path") or ""):
                continue
            if client_id and item.get("client_id") and \
                    item["client_id"] != str(client_id).strip():
                continue
            return item
        return None

    def list(self, q="", status="", limit=20, offset=0):
        where, args = [], []
        if q:
            where.append("name LIKE ?")
            args.append("%" + q + "%")
        if status:
            where.append("status = ?")
            args.append(status)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) FROM print_jobs" + clause, args
            ).fetchone()[0]
            rows = self._conn.execute(
                "SELECT * FROM print_jobs" + clause +
                " ORDER BY created_at DESC LIMIT ? OFFSET ?",
                args + [max(1, min(int(limit or 20), 200)), max(0, int(offset or 0))],
            ).fetchall()
        return [dict(row) for row in rows], total

    def count(self):
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM print_jobs").fetchone()[0]

    # ---------- 清理 ----------

    def prune(self, now=None):
        """删过期文件；`PRINT_KEEP_DAYS<=0` 时保留文件，只清不存在的记录。"""
        now = now or _now()
        cutoff = now - max(0, int(self.keep_days)) * 86400
        removed = 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, path, created_at FROM print_jobs"
            ).fetchall()
            for row in rows:
                path = row["path"] or ""
                exists = os.path.exists(path)
                expired = self.keep_days > 0 and row["created_at"] < cutoff
                if not exists:
                    # 文件没了（手动删/卷迁移），记录留着当历史
                    continue
                if expired:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                    removed += 1
            self._conn.commit()
        return removed
