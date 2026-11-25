"""结构化事实 - SQLite facts 表。

用于户号、地址、品牌、型号、偏好等结构化信息；与自然语言记忆互补。
"""
import os
import re
import sqlite3
import threading
from datetime import datetime

from config import FACTS_DB_PATH
from agent.synonyms import canonicalize, variants

# 查询拆词：空白和常见标点都当分隔符
_SPLIT_RE = re.compile(r"[\s,，。、;；:：/]+")


def _escape_like(text):
    """LIKE 里的 % _ 和反斜杠按字面匹配，别让用户的一个百分号捞出全表。"""
    return (
        str(text).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )


def _like_any(column, term):
    """同义词之间 OR，不同检索词之间 AND（后者由调用方拼）。

    「水费账号」拆成「水费」「账号」后，「账号」这一条要能匹配存成「户号」的行；
    但「水费」仍然必须命中，所以不能把整条查询放松成「命中任一」。
    """
    group = variants(term)
    clause = "(" + " OR ".join(
        column + " LIKE ? ESCAPE '\\'" for _ in group
    ) + ")"
    return clause, ["%" + _escape_like(word) + "%" for word in group]


def _haystack(row):
    """一行事实参与检索的文本：主体 + 属性 + 值。"""
    return "%s%s%s" % (row[1] or "", row[2] or "", row[3] or "")


# 画像统一挂在「用户」前缀下：用户 / 用户/妈妈。
# 热点上下文（D58）就是靠这个前缀把画像和普通事实分开的，别在别处另起格式。
PROFILE_PREFIX = "用户"
_SELF_WORDS = ("", "我", "本人", "自己", "用户", "当前说话人", "说话人")


def profile_subject(person):
    """把「谁」归一成画像 subject：当前说话人 → 用户，别人 → 用户/<名字>。"""
    person = str(person or "").strip()
    if person in _SELF_WORDS:
        return PROFILE_PREFIX
    if person.startswith(PROFILE_PREFIX):
        return person
    return "%s/%s" % (PROFILE_PREFIX, person)


def _query_terms(query):
    """把查询拆成检索词：多词按词拆，长词再拆成二字组。

    为什么拆：`search` 是整串 LIKE，「水费户号」既不是 subject「水费」的子串，
    也不是 predicate「户号」的子串——词序一换就查不到，而第几次说法由模型决定。
    拆成二字组后逐词匹配，只要这些字按顺序出现在「主体+属性+值」里就能命中。
    """
    text = str(query or "").strip()
    if not text:
        return []
    terms = []
    for part in _SPLIT_RE.split(text):
        if not part:
            continue
        if len(part) <= 2:
            terms.append(part)
            continue
        terms.extend(part[i:i + 2] for i in range(len(part) - 1))
    # 去重但保序：重复词只会让 SQL 变长
    seen = set()
    unique = []
    for term in terms:
        if term not in seen:
            seen.add(term)
            unique.append(term)
    return unique


class Facts:
    def __init__(self, db_path=FACTS_DB_PATH):
        if db_path:
            db_dir = os.path.dirname(db_path)
            if db_dir:
                os.makedirs(db_dir, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(db_path or ":memory:", check_same_thread=False)
        self._create_schema()
        self._close_legacy_duplicates()
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

    def _close_legacy_duplicates(self):
        """旧版本只插入不关闭，同一事实会堆成好几条「当前有效」。

        启动时把它们关掉、只留最新一条，否则检索会把新旧值一起返回。
        幂等：没有重复就不写库。
        """
        rows = self.conn.execute(
            "SELECT id, subject, predicate, created_at FROM facts "
            "WHERE valid_to IS NULL ORDER BY id ASC"
        ).fetchall()
        groups = {}
        for fact_id, subject, predicate, created_at in rows:
            groups.setdefault((subject, predicate), []).append((fact_id, created_at))
        for items in groups.values():
            if len(items) < 2:
                continue
            cutoff = items[-1][1] or datetime.now().isoformat()
            older = [fact_id for fact_id, _ in items[:-1]]
            placeholders = ",".join("?" * len(older))
            self.conn.execute(
                "UPDATE facts SET valid_to=? WHERE id IN (%s)" % placeholders,
                [cutoff] + older,
            )

    def store(self, subject, predicate, value, source=None,
              valid_from=None, valid_to=None):
        """存一条事实；同一「主体 + 属性」的旧值会被关闭而不是覆盖。

        为什么要留历史：户号、地址、型号都会变，直接覆盖之后「以前是多少」
        就查不到了；而旧版本实际连覆盖都没做，只是不断插入新行，检索时新旧
        一起返回，模型可能拿到过期值。现在旧行标 valid_to，新行 valid_from。
        """
        subject = str(subject or "").strip()
        predicate = str(predicate or "").strip()
        value = str(value or "").strip()
        if not subject or not predicate or not value:
            raise ValueError("subject / predicate / value 都不能为空")

        ts = datetime.now().isoformat()
        start = valid_from or ts
        with self._lock:
            active = self.conn.execute(
                "SELECT id, value FROM facts WHERE subject=? AND predicate=? "
                "AND valid_to IS NULL ORDER BY id DESC",
                (subject, predicate),
            ).fetchall()

            if active and active[0][1] == value:
                # 值没变：沿用现有记录，不制造历史噪音（重复说同一件事很常见）
                return self._one(active[0][0], replaced=[])

            replaced = [row[1] for row in active if row[1] != value]
            if active:
                ids = [row[0] for row in active]
                placeholders = ",".join("?" * len(ids))
                self.conn.execute(
                    "UPDATE facts SET valid_to=? WHERE id IN (%s)" % placeholders,
                    [ts] + ids,
                )
            cur = self.conn.execute(
                "INSERT INTO facts "
                "(subject, predicate, value, source, valid_from, valid_to, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (subject, predicate, value, source, start, valid_to, ts),
            )
            self.conn.commit()
            fact_id = cur.lastrowid

        return {
            "id": fact_id,
            "subject": subject,
            "predicate": predicate,
            "value": value,
            "source": source,
            "valid_from": start,
            "valid_to": valid_to,
            "replaced": replaced,
        }

    def _one(self, fact_id, replaced):
        row = self.conn.execute(
            "SELECT id, subject, predicate, value, source, valid_from, "
            "       valid_to, created_at FROM facts WHERE id=?",
            (fact_id,),
        ).fetchone()
        result = self._row_to_dict(row)
        result["replaced"] = replaced
        return result

    def search(self, query=None, subject=None, predicate=None, n_results=10,
               include_history=False):
        """查事实。默认只返回「当前有效」的，历史要显式要。"""
        clauses = []
        params = []
        if not include_history:
            clauses.append("valid_to IS NULL")
        if subject:
            clause, values = _like_any("subject", subject)
            clauses.append(clause)
            params.extend(values)
        if predicate:
            clause, values = _like_any("predicate", predicate)
            clauses.append(clause)
            params.extend(values)
        if query:
            # 先把整句里的同义说法归一，再拆词：「水费账号」→「水费户号」，
            # 拆出来的二字组才和库里的「水费户号」对得上。
            terms = _query_terms(canonicalize(query))
            if not terms:
                return []
        else:
            terms = []

        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, subject, predicate, value, source, valid_from, "
                "       valid_to, created_at FROM facts"
                + where
                + " ORDER BY id DESC",
                params,
            ).fetchall()

        # 同义词要把库里存的文本也归一之后才比，否则「存账号、问户号」对不上。
        # 家庭事实表只有几十行，一次取回来在内存里过滤，比在 SQL 里拼 REPLACE 清楚。
        if terms:
            rows = [
                row for row in rows
                if all(term in canonicalize(_haystack(row)) for term in terms)
            ]
        return [self._row_to_dict(row) for row in rows[:n_results]]

    def list_all(self, n_results=10):
        return self.search(n_results=n_results)

    def history(self, subject, predicate, n_results=50):
        """某条事实的全部版本（含已失效的），新的在前。"""
        subject = str(subject or "").strip()
        predicate = str(predicate or "").strip()
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, subject, predicate, value, source, valid_from, "
                "       valid_to, created_at FROM facts "
                "WHERE subject=? AND predicate=? ORDER BY id DESC LIMIT ?",
                (subject, predicate, n_results),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

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

    def count(self, include_history=False):
        with self._lock:
            where = "" if include_history else " WHERE valid_to IS NULL"
            return self.conn.execute(
                "SELECT COUNT(*) FROM facts" + where
            ).fetchone()[0]

    def close(self):
        with self._lock:
            self.conn.close()
