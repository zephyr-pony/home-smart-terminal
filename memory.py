"""记忆存储模块 - SQLite FTS5 全文检索（中文逐字空格化）

unicode61 分词器不切分中文，会把整串中文当一个 token，
导致子串搜索失效。解决：存储时把每个字符用空格分隔
（"8月5号酸奶过期" -> "8 月 5 号 酸 奶 过 期"），
每个字符成为独立 token，任意子串都可以用短语查询命中。

MVP 阶段零依赖方案，后续可升级为 ChromaDB + embedding 语义检索。
"""
import sqlite3
import os
import threading
from datetime import datetime

from config import MEMORY_DB_PATH


def _segment(text):
    """把中文/数字串逐字符空格化，供 FTS5 检索。"""
    return " ".join(ch for ch in str(text) if not ch.isspace())


def _escape_like(text):
    """转义 LIKE 的通配符，避免用户输入 % 或 _ 时匹配到全部。"""
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


class Memory:
    def __init__(self, db_path=MEMORY_DB_PATH):
        """初始化记忆库。旧格式数据自动迁移。

        线程安全：服务端用 asyncio.to_thread 跑编排，会跨线程访问，
        所以连接放开线程检查，并用 RLock 串行化所有数据库操作。
        """
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._migrate_legacy()
        self._create_schema()
        self.conn.commit()

    def _create_schema(self):
        """FTS5 索引表 + 原文表（原文用于展示，不参与分词）。"""
        self.conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                searchable,
                timestamp,
                tokenize='unicode61'
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS memories_raw (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                summary TEXT,
                text TEXT,
                timestamp TEXT
            )
        """)
        # 蒸馏记录（D60）：同一组成员再跑一次没有意义，用指纹跳过；
        # 模型判定「无关」也记一条，免得每天重复问同一个问题。
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_distill_log (
                signature TEXT PRIMARY KEY,
                memory_id INTEGER,
                summary TEXT,
                updated_at TEXT
            )
        """)

    def _migrate_legacy(self):
        """迁移旧版单表格式（memories FTS 表）到新格式。"""
        has_old = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memories'"
        ).fetchone()
        if not has_old:
            return

        rows = self.conn.execute("SELECT summary, text, timestamp FROM memories").fetchall()
        self.conn.execute("DROP TABLE memories")
        self._create_schema()
        self.conn.commit()
        for summary, text, ts in rows:
            self._insert(summary, text, ts)
        self.conn.commit()

    def _insert(self, summary, text, timestamp):
        """写入一条记忆（内部方法，id 对齐）。"""
        doc = summary if summary else text
        cur = self.conn.execute(
            "INSERT INTO memories_raw (summary, text, timestamp) VALUES (?, ?, ?)",
            (summary, text, timestamp),
        )
        rowid = cur.lastrowid
        self.conn.execute(
            "INSERT INTO memories_fts (rowid, searchable, timestamp) VALUES (?, ?, ?)",
            (rowid, _segment(doc), timestamp),
        )

    def store(self, text, summary=None, metadata=None):
        """存储一条记忆。

        Args:
            text: 原文
            summary: 摘要（用于检索），不传则用原文
            metadata: 额外元数据（暂不使用，保持接口兼容）
        """
        ts = datetime.now().isoformat()
        with self._lock:
            self._insert(summary, text, ts)
            self.conn.commit()

    def _search_rows(self, query, n_results=5):
        """全文检索，返回原始行 (id, summary, text, timestamp)。

        每个关键词 -> 字符短语（"酸奶" -> "酸 奶"，匹配任意位置的"酸奶"）。
        FTS 只索引了 summary（摘要），原文里有、摘要里没有的词会漏，
        所以查不到时再用 LIKE 扫一遍原文补召回。
        """
        keywords = [k for k in query.replace("'", " ").replace('"', " ").split() if k.strip()]
        if not keywords:
            return []

        phrases = [_segment(k) for k in keywords]
        match_expr = " OR ".join(f'"{p}"' for p in phrases)

        rows = self.conn.execute(
            """
            SELECT r.id, r.summary, r.text, r.timestamp
            FROM memories_fts f
            JOIN memories_raw r ON r.id = f.rowid
            WHERE memories_fts MATCH ?
            ORDER BY rank LIMIT ?
            """,
            (match_expr, n_results),
        ).fetchall()
        if rows:
            return rows
        return self._search_like_rows(keywords, n_results)

    def _search_like_rows(self, keywords, n_results):
        """兜底召回：对摘要和原文做子串匹配（FTS 未命中时调用）。"""
        clauses = []
        params = []
        for keyword in keywords:
            clauses.append(
                "(COALESCE(summary, '') LIKE ? ESCAPE '\\' "
                "OR COALESCE(text, '') LIKE ? ESCAPE '\\')"
            )
            pattern = "%" + _escape_like(keyword) + "%"
            params.extend([pattern, pattern])
        params.append(n_results)
        return self.conn.execute(
            "SELECT id, summary, text, timestamp FROM memories_raw "
            "WHERE " + " OR ".join(clauses) +
            " ORDER BY timestamp DESC LIMIT ?",
            params,
        ).fetchall()

    @staticmethod
    def _format_time(ts):
        try:
            dt = datetime.fromisoformat(ts)
            return dt.strftime("%Y年%m月%d日 %H:%M")
        except Exception:
            return ts

    def search(self, query, n_results=5):
        """全文检索相关记忆（中文子串匹配）。

        Args:
            query: 检索关键词（可空格分隔多个词）
            n_results: 返回条数

        Returns:
            list[str]: 记忆列表，格式 "[时间] 原文"
        """
        with self._lock:
            rows = self._search_rows(query, n_results)
        return [
            f"[{self._format_time(ts)}] {text}"
            for _rid, _summary, text, ts in rows
        ]

    def list_all(self, n_results=10):
        """列出最近 n_results 条记忆（按时间倒序）。

        Returns:
            list[dict]: [{"id", "summary", "text", "time"}]，time 为格式化时间串
        """
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, summary, text, timestamp FROM memories_raw "
                # Windows 的 datetime.now() 精度只有十几毫秒，同一轮连续写入会
                # 撞上同一个 timestamp；用 id 兜底，保证「最新」是确定的。
                "ORDER BY timestamp DESC, id DESC LIMIT ?",
                (n_results,),
            ).fetchall()
        return [
            {"id": rid, "summary": summary, "text": text, "time": self._format_time(ts)}
            for rid, summary, text, ts in rows
        ]

    def find_matching(self, keywords, limit=50):
        """只查不删：删除前先看会命中什么，避免「没命中却报告删了」或删错。"""
        with self._lock:
            rows = self._search_rows(keywords, n_results=limit)
        return [
            {"id": row[0], "summary": row[1], "text": row[2], "timestamp": row[3]}
            for row in rows
        ]

    def delete_by_keywords(self, query):
        """按关键词删除匹配的记忆。

        Args:
            query: 检索关键词

        Returns:
            list[dict]: 被删除的记忆（与 list_all 格式一致），空列表表示无匹配
        """
        with self._lock:
            hits = self._search_rows(query, n_results=50)
            if not hits:
                return []

            ids = [rid for rid, _s, _t, _ts in hits]
            placeholders = ",".join("?" * len(ids))
            self.conn.execute(
                f"DELETE FROM memories_raw WHERE id IN ({placeholders})", ids
            )
            self.conn.execute(
                f"DELETE FROM memories_fts WHERE rowid IN ({placeholders})", ids
            )
            self.conn.commit()

        return [
            {"id": rid, "summary": summary, "text": text, "time": self._format_time(ts)}
            for rid, summary, text, ts in hits
        ]

    def search_details(self, query, n_results=50):
        """全文检索并返回结构化结果（供 Web 列表使用）。"""
        with self._lock:
            rows = self._search_rows(query, n_results)
        return [
            {
                "id": rid,
                "summary": summary,
                "text": text,
                "time": self._format_time(ts),
            }
            for rid, summary, text, ts in rows
        ]

    def delete_by_id(self, memory_id):
        """按 id 删除一条记忆，返回是否命中。"""
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM memories_raw WHERE id=?", (memory_id,)
            )
            self.conn.execute(
                "DELETE FROM memories_fts WHERE rowid=?", (memory_id,)
            )
            self.conn.commit()
            return cur.rowcount > 0

    def count(self):
        """返回记忆总数。"""
        with self._lock:
            cursor = self.conn.execute("SELECT COUNT(*) FROM memories_raw")
            return cursor.fetchone()[0]

    # ---------- 蒸馏（D60） ----------

    def update_summary(self, memory_id, summary):
        """只换摘要：原文和时间一个字都不动，FTS 索引同步重建。

        原文是用户真实说过的话，也是摘要查不到时的兜底，所以这里绝不 UPDATE text。
        """
        summary = str(summary or "").strip()
        if not summary:
            return False
        with self._lock:
            row = self.conn.execute(
                "SELECT timestamp FROM memories_raw WHERE id=?", (memory_id,)
            ).fetchone()
            if row is None:
                return False
            self.conn.execute(
                "UPDATE memories_raw SET summary=? WHERE id=?", (summary, memory_id)
            )
            self.conn.execute(
                "DELETE FROM memories_fts WHERE rowid=?", (memory_id,)
            )
            self.conn.execute(
                "INSERT INTO memories_fts (rowid, searchable, timestamp) VALUES (?, ?, ?)",
                (memory_id, _segment(summary), row[0]),
            )
            self.conn.commit()
            return True

    def is_distilled(self, signature):
        """这一组是否已经蒸馏过（或已判定为无关）。"""
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM memory_distill_log WHERE signature=?", (signature,)
            ).fetchone()
            return row is not None

    def record_distill(self, signature, memory_id, summary=""):
        """记下蒸馏结果；summary 为空表示模型判定「不是同一主题」。"""
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO memory_distill_log "
                "(signature, memory_id, summary, updated_at) VALUES (?, ?, ?, ?)",
                (signature, memory_id, summary or "", datetime.now().isoformat()),
            )
            self.conn.commit()

    def close(self):
        with self._lock:
            self.conn.close()
