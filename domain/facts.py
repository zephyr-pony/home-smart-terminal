"""结构化事实 - SQLite facts 表。

用于户号、地址、品牌、型号、偏好等结构化信息；与自然语言记忆互补。
"""
import os
import sqlite3
import threading
from datetime import datetime

from config import FACTS_DB_PATH


class Facts:
    def __init__(self, db_path=FACTS_DB_PATH):
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
            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                value TEXT NOT NULL,
                source TEXT,
                valid_from TEXT,
                valid_to TEXT,
                created_at TEXT
            )
        """)
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_facts_subject "
            "ON facts(subject, predicate)"
        )

    def store(self, subject, predicate, value, source=None,
              valid_from=None, valid_to=None):
        subject = str(subject or "").strip()
        predicate = str(predicate or "").strip()
        value = str(value or "").strip()
        if not subject or not predicate or not value:
            raise ValueError("subject / predicate / value 都不能为空")

        ts = datetime.now().isoformat()
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO facts "
                "(subject, predicate, value, source, valid_from, valid_to, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (subject, predicate, value, source, valid_from, valid_to, ts),
            )
            self.conn.commit()
            fact_id = cur.lastrowid

        return {
            "id": fact_id,
            "subject": subject,
            "predicate": predicate,
            "value": value,
            "source": source,
        }

    def search(self, query=None, subject=None, predicate=None, n_results=10):
        clauses = []
        params = []
        if subject:
            clauses.append("subject LIKE ?")
            params.append(f"%{subject}%")
        if predicate:
            clauses.append("predicate LIKE ?")
            params.append(f"%{predicate}%")
        if query:
            clauses.append("(subject LIKE ? OR predicate LIKE ? OR value LIKE ?)")
            params.extend([f"%{query}%"] * 3)

        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, subject, predicate, value, source, valid_from, "
                "       valid_to, created_at FROM facts"
                + where
                + " ORDER BY id DESC LIMIT ?",
                params + [n_results],
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def list_all(self, n_results=10):
        return self.search(n_results=n_results)

    @staticmethod
    def _row_to_dict(row):
        return {
            "id": row[0],
            "subject": row[1],
            "predicate": row[2],
            "value": row[3],
            "source": row[4],
            "valid_from": row[5],
            "valid_to": row[6],
            "created_at": row[7],
        }

    def delete_by_id(self, fact_id):
        with self._lock:
            row = self.conn.execute(
                "SELECT id, subject, predicate, value FROM facts WHERE id=?",
                (fact_id,),
            ).fetchone()
            if not row:
                return None
            self.conn.execute("DELETE FROM facts WHERE id=?", (fact_id,))
            self.conn.commit()
        return {
            "id": row[0],
            "subject": row[1],
            "predicate": row[2],
            "value": row[3],
        }

    def count(self):
        with self._lock:
            return self.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]

    def close(self):
        with self._lock:
            self.conn.close()
