"""Agent 领域存储：实体与待澄清动作。

与 memory / items 分开，避免把通用领域状态塞进具体业务表。
线程安全：check_same_thread=False + RLock。
"""
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta

from config import AGENT_DB_PATH, PENDING_ACTION_TTL_SECONDS

_JUNK_RE = re.compile(r"[\s，。！？、,.!?~～…]+")


def normalize_text(text):
    """去掉空白和标点，用于实体/候选匹配。"""
    return _JUNK_RE.sub("", str(text or ""))


class AgentStore:
    def __init__(self, db_path=AGENT_DB_PATH):
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
            CREATE TABLE IF NOT EXISTS entities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                canonical_name TEXT NOT NULL,
                aliases TEXT DEFAULT '[]',
                created_at TEXT
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                original_text TEXT NOT NULL,
                missing_slot TEXT NOT NULL,
                question TEXT NOT NULL,
                candidates TEXT DEFAULT '[]',
                created_at TEXT,
                expires_at TEXT
            )
        """)
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pending_conversation "
            "ON pending_actions(conversation_id)"
        )

    # ---------- 实体 ----------

    def add_entity(self, type, canonical_name, aliases=None):
        """新增或更新实体，返回 id。"""
        canonical_name = str(canonical_name or "").strip()
        if not canonical_name:
            return None
        aliases = [str(a).strip() for a in (aliases or []) if str(a).strip()]

        with self._lock:
            row = self.conn.execute(
                "SELECT id, aliases FROM entities WHERE type=? AND canonical_name=?",
                (type, canonical_name),
            ).fetchone()
            if row:
                existing = set(json.loads(row[1] or "[]"))
                existing.update(aliases)
                self.conn.execute(
                    "UPDATE entities SET aliases=? WHERE id=?",
                    (json.dumps(sorted(existing), ensure_ascii=False), row[0]),
                )
                self.conn.commit()
                return row[0]

            cur = self.conn.execute(
                "INSERT INTO entities (type, canonical_name, aliases, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    type,
                    canonical_name,
                    json.dumps(aliases, ensure_ascii=False),
                    datetime.now().isoformat(),
                ),
            )
            self.conn.commit()
            return cur.lastrowid

    def list_entities(self, type):
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, type, canonical_name, aliases FROM entities "
                "WHERE type=? ORDER BY id",
                (type,),
            ).fetchall()
        return [
            {
                "id": row[0],
                "type": row[1],
                "canonical_name": row[2],
                "aliases": json.loads(row[3] or "[]"),
            }
            for row in rows
        ]

    def find_entity(self, type, text):
        """按规范名/别名在文本中找实体，取最长匹配。"""
        normalized = normalize_text(text)
        if not normalized:
            return None

        best = None
        best_length = 0
        for entity in self.list_entities(type):
            for name in [entity["canonical_name"]] + entity["aliases"]:
                candidate = normalize_text(name)
                if candidate and candidate in normalized and len(candidate) > best_length:
                    best = entity
                    best_length = len(candidate)
        return best

    # ---------- 待澄清动作 ----------

    def create_pending(self, conversation_id, original_text, missing_slot,
                       question, candidates=None, ttl_seconds=None):
        ttl = PENDING_ACTION_TTL_SECONDS if ttl_seconds is None else ttl_seconds
        now = datetime.now()
        expires_at = (now + timedelta(seconds=ttl)).isoformat()
        payload = json.dumps(candidates or [], ensure_ascii=False)

        with self._lock:
            # 一个会话同时只保留一个待澄清动作
            self.conn.execute(
                "DELETE FROM pending_actions WHERE conversation_id=?",
                (conversation_id,),
            )
            cur = self.conn.execute(
                "INSERT INTO pending_actions "
                "(conversation_id, original_text, missing_slot, question, "
                " candidates, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    conversation_id,
                    original_text,
                    missing_slot,
                    question,
                    payload,
                    now.isoformat(),
                    expires_at,
                ),
            )
            self.conn.commit()
            return cur.lastrowid

    def get_pending(self, conversation_id):
        with self._lock:
            row = self.conn.execute(
                "SELECT id, conversation_id, original_text, missing_slot, "
                "       question, candidates, created_at, expires_at "
                "FROM pending_actions WHERE conversation_id=? "
                "ORDER BY id DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
            if not row:
                return None
            if row[7] and row[7] <= datetime.now().isoformat():
                self.conn.execute("DELETE FROM pending_actions WHERE id=?", (row[0],))
                self.conn.commit()
                return None

        return {
            "id": row[0],
            "conversation_id": row[1],
            "original_text": row[2],
            "missing_slot": row[3],
            "question": row[4],
            "candidates": json.loads(row[5] or "[]"),
            "created_at": row[6],
            "expires_at": row[7],
        }

    def clear_pending(self, pending_id):
        with self._lock:
            self.conn.execute(
                "DELETE FROM pending_actions WHERE id=?", (pending_id,)
            )
            self.conn.commit()

    def close(self):
        with self._lock:
            self.conn.close()
