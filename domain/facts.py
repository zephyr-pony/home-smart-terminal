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


def _matches_terms(row, terms):
    """一条事实（dict）是否包含全部检索词：主体 + 属性 + 值，同义词先归一。"""
    text = canonicalize("%s%s%s" % (
        row.get("subject") or "", row.get("predicate") or "", row.get("value") or "",
    ))
    return all(term in text for term in terms)


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
        # 实体解析由调用方注入（Assistant 用 AgentStore 的 find_any_entity）：
        # 领域层不 import agent，也不去打开另一个库。
        self._entity_lookup = None
        self._create_schema()
        self._close_legacy_duplicates()
        self.conn.commit()

    def set_entity_lookup(self, lookup):
        """lookup(text) -> {"id", "canonical_name", "aliases"...} 或 None。"""
        self._entity_lookup = lookup

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
        # D66：事实挂到实体上，查「饮水机」也能找到存在「净水器」名下的事实。
        # 老库直接补列，不重建表；没匹配到实体的事实 entity_id 留空，文本检索照旧。
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(facts)")}
        if "entity_id" not in columns:
            self.conn.execute("ALTER TABLE facts ADD COLUMN entity_id INTEGER")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_facts_subject "
            "ON facts(subject, predicate)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_facts_entity "
            "ON facts(entity_id, predicate)"
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
              valid_from=None, valid_to=None, entity_id=None):
        """存一条事实；同一「主体 + 属性」的旧值会被关闭而不是覆盖。

        为什么要留历史：户号、地址、型号都会变，直接覆盖之后「以前是多少」
        就查不到了；而旧版本实际连覆盖都没做，只是不断插入新行，检索时新旧
        一起返回，模型可能拿到过期值。现在旧行标 valid_to，新行 valid_from。

        D66：能匹配到实体时把 `entity_id` 一起存下来（例如「饮水机」是
        「净水器」的别名），查别名时不必依赖文本是否完全一致；主体文字
        仍按用户原话保存，展示不变。
        """
        subject = str(subject or "").strip()
        predicate = str(predicate or "").strip()
        value = str(value or "").strip()
        if not subject or not predicate or not value:
            raise ValueError("subject / predicate / value 都不能为空")

        if entity_id is None and self._entity_lookup is not None \
                and not subject.startswith(PROFILE_PREFIX):
            entity = self._entity_lookup(subject)
            if entity:
                entity_id = entity.get("id")

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
                "(subject, predicate, value, source, valid_from, valid_to, "
                " created_at, entity_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (subject, predicate, value, source, start, valid_to, ts, entity_id),
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
            "entity_id": entity_id,
            "replaced": replaced,
        }

    def _one(self, fact_id, replaced):
        row = self.conn.execute(
            "SELECT id, subject, predicate, value, source, valid_from, "
            "       valid_to, created_at, entity_id FROM facts WHERE id=?",
            (fact_id,),
        ).fetchone()
        result = self._row_to_dict(row)
        result["replaced"] = replaced
        return result

    def search(self, query=None, subject=None, predicate=None, n_results=10,
               include_history=False, entity_id=None):
        """查事实。默认只返回「当前有效」的，历史要显式要。

        D66：查询里出现某个实体（规范名或别名）时，把这个实体名下的事实
        也并进来；别名不再是两套文本。「净水器滤芯型号」= 净水器的事实里
        再按「滤芯型号」筛。
        """
        found = self._search_text(
            query, subject, predicate, n_results, include_history, entity_id
        )
        entity = self._match_entity(query)
        if entity is None:
            return found
        extra = self._entity_facts(
            entity, query, include_history, subject, predicate
        )
        merged = {row["id"]: row for row in found}
        for row in extra:
            merged.setdefault(row["id"], row)
        rows = sorted(merged.values(), key=lambda row: row["id"], reverse=True)
        return rows[:n_results]

    def _search_text(self, query, subject, predicate, n_results,
                     include_history, entity_id=None):
        clauses = []
        params = []
        if not include_history:
            clauses.append("valid_to IS NULL")
        if entity_id is not None:
            clauses.append("entity_id = ?")
            params.append(int(entity_id))
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
                "       valid_to, created_at, entity_id FROM facts"
                + where
                + " ORDER BY id DESC",
                params,
            ).fetchall()

        # 同义词要把库里存的文本也归一之后才比，否则「存账号、问户号」对不上。
        # 家庭事实表只有几十行，一次取回来在内存里过滤，比在 SQL 里拼 REPLACE 清楚。
        items = [self._row_to_dict(row) for row in rows]
        if terms:
            items = [row for row in items if _matches_terms(row, terms)]
        return items[:n_results]

    def _match_entity(self, query):
        if not query or self._entity_lookup is None:
            return None
        return self._entity_lookup(str(query))

    def _entity_facts(self, entity, query, include_history, subject, predicate):
        """实体名下的当前事实；查询里剩下的词（如「滤芯型号」）再筛一遍。"""
        names = [entity.get("canonical_name") or ""] + list(entity.get("aliases") or [])
        names = [name for name in names if name]
        if not names:
            return []
        clauses = []
        params = []
        if not include_history:
            clauses.append("valid_to IS NULL")
        match = ["entity_id = ?"] + ["subject LIKE ? ESCAPE '\\'" for _ in names]
        params.append(int(entity["id"]))
        params.extend("%" + _escape_like(name) + "%" for name in names)
        clauses.append("(" + " OR ".join(match) + ")")
        if subject:
            clause, values = _like_any("subject", subject)
            clauses.append(clause)
            params.extend(values)
        if predicate:
            clause, values = _like_any("predicate", predicate)
            clauses.append(clause)
            params.extend(values)
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, subject, predicate, value, source, valid_from, "
                "       valid_to, created_at, entity_id FROM facts"
                + " WHERE " + " AND ".join(clauses)
                + " ORDER BY id DESC",
                params,
            ).fetchall()
        items = [self._row_to_dict(row) for row in rows]
        rest = query or ""
        for name in sorted(names, key=len, reverse=True):
            if name and name in rest:
                rest = rest.replace(name, "", 1)
                break
        terms = _query_terms(canonicalize(rest)) if rest.strip() else []
        if terms:
            items = [row for row in items if _matches_terms(row, terms)]
        return items

    def list_all(self, n_results=10):
        return self.search(n_results=n_results)

    def history(self, subject, predicate, n_results=50):
        """某条事实的全部版本（含已失效的），新的在前。"""
        subject = str(subject or "").strip()
        predicate = str(predicate or "").strip()
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, subject, predicate, value, source, valid_from, "
                "       valid_to, created_at, entity_id FROM facts "
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
            "entity_id": row[8] if len(row) > 8 else None,
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
