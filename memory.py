"""记忆存储模块 - SQLite FTS5 全文检索（中文逐字空格化）

unicode61 分词器不切分中文，会把整串中文当一个 token，
导致子串搜索失效。解决：存储时把每个字符用空格分隔
（"8月5号酸奶过期" -> "8 月 5 号 酸 奶 过 期"），
每个字符成为独立 token，任意子串都可以用短语查询命中。

MVP 阶段零依赖方案，后续可升级为 ChromaDB + embedding 语义检索。
"""
import sqlite3
import os
from datetime import datetime

from config import MEMORY_DB_PATH


def _segment(text):
    """把中文/数字串逐字符空格化，供 FTS5 检索。"""
    return " ".join(ch for ch in str(text) if not ch.isspace())


class Memory:
    def __init__(self, db_path=MEMORY_DB_PATH):
        """初始化记忆库。旧格式数据自动迁移。"""
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path)
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
        self._insert(text, summary, ts)
        self.conn.commit()

    def search(self, query, n_results=5):
        """全文检索相关记忆（中文子串匹配）。

        Args:
            query: 检索关键词（可空格分隔多个词）
            n_results: 返回条数

        Returns:
            list[str]: 记忆列表，格式 "[时间] 原文"
        """
        # 每个关键词 -> 字符短语（"酸奶" -> "酸 奶"，匹配任意位置的"酸奶"）
        keywords = [k for k in query.replace("'", " ").replace('"', " ").split() if k.strip()]
        if not keywords:
            return []

        phrases = [_segment(k) for k in keywords]
        match_expr = " OR ".join(f'"{p}"' for p in phrases)

        cursor = self.conn.execute(
            """
            SELECT r.summary, r.text, r.timestamp
            FROM memories_fts f
            JOIN memories_raw r ON r.id = f.rowid
            WHERE memories_fts MATCH ?
            ORDER BY rank LIMIT ?
            """,
            (match_expr, n_results),
        )

        memories = []
        for _summary, text, ts in cursor.fetchall():
            try:
                dt = datetime.fromisoformat(ts)
                ts_str = dt.strftime("%Y年%m月%d日 %H:%M")
            except Exception:
                ts_str = ts
            memories.append(f"[{ts_str}] {text}")
        return memories

    def count(self):
        """返回记忆总数。"""
        cursor = self.conn.execute("SELECT COUNT(*) FROM memories_raw")
        return cursor.fetchone()[0]

    def close(self):
        self.conn.close()
