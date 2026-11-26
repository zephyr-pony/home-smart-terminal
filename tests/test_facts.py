import pytest

from domain.facts import Facts


def test_store_search_and_list_facts():
    facts = Facts(":memory:")
    facts.store("爸妈家", "电费户号", "123456", source="用户")
    facts.store("牛奶", "品牌", "蒙牛")

    assert facts.count() == 2

    found = facts.search("户号")
    assert found[0]["value"] == "123456"
    assert found[0]["source"] == "用户"

    assert facts.search(subject="牛奶")[0]["predicate"] == "品牌"
    assert len(facts.list_all()) == 2


def test_delete_by_id():
    facts = Facts(":memory:")
    fact = facts.store("爸妈家", "电费户号", "123456")

    deleted = facts.delete_by_id(fact["id"])

    assert deleted["value"] == "123456"
    assert facts.count() == 0
    assert facts.delete_by_id(999) is None


def test_invalid_fact_is_rejected():
    facts = Facts(":memory:")

    with pytest.raises(ValueError):
        facts.store("", "品牌", "蒙牛")


def test_new_value_closes_the_old_one():
    """户号改了：旧记录标失效，检索只返回新值，历史仍查得到。"""
    facts = Facts(":memory:")
    facts.store("爸妈家", "电费户号", "123456")
    result = facts.store("爸妈家", "电费户号", "654321")

    assert result["value"] == "654321"
    assert result["replaced"] == ["123456"]

    active = facts.search(subject="爸妈家")
    assert [row["value"] for row in active] == ["654321"]
    assert facts.count() == 1
    assert facts.count(include_history=True) == 2

    versions = facts.history("爸妈家", "电费户号")
    assert [row["value"] for row in versions] == ["654321", "123456"]
    assert versions[0]["valid_to"] is None
    assert versions[1]["valid_to"] is not None


def test_history_is_hidden_by_default():
    facts = Facts(":memory:")
    facts.store("爸妈家", "电费户号", "123456")
    facts.store("爸妈家", "电费户号", "654321")

    assert len(facts.search("123456")) == 0
    assert len(facts.search("123456", include_history=True)) == 1


def test_same_value_does_not_pile_up_rows():
    facts = Facts(":memory:")
    facts.store("爸妈家", "电费户号", "123456")
    facts.store("爸妈家", "电费户号", "123456")

    assert facts.count(include_history=True) == 1
    assert facts.history("爸妈家", "电费户号")[0]["value"] == "123456"


def test_legacy_duplicates_are_closed_on_startup(tmp_path):
    """旧库只插不关，同一事实会有多条有效记录：启动时只留最新一条。"""
    import sqlite3

    path = str(tmp_path / "facts.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE facts ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT NOT NULL,"
        " predicate TEXT NOT NULL, value TEXT NOT NULL, source TEXT,"
        " valid_from TEXT, valid_to TEXT, created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO facts (subject, predicate, value, created_at) "
        "VALUES ('爸妈家', '电费户号', '123456', '2026-11-21T20:00:00')"
    )
    conn.execute(
        "INSERT INTO facts (subject, predicate, value, created_at) "
        "VALUES ('爸妈家', '电费户号', '654321', '2026-11-22T20:00:00')"
    )
    conn.commit()
    conn.close()

    facts = Facts(path)

    assert [row["value"] for row in facts.search()] == ["654321"]
    assert facts.count() == 1
    assert facts.count(include_history=True) == 2


def test_query_matches_across_subject_and_predicate():
    """「水费户号」跨了 subject 和 predicate，整串 LIKE 查不到，拆词后能查到。"""
    facts = Facts(":memory:")
    facts.store("水费", "户号", "12345678")

    assert [r["value"] for r in facts.search("水费户号")] == ["12345678"]
    assert [r["value"] for r in facts.search("水费 户号")] == ["12345678"]


def test_query_does_not_match_unrelated_rows():
    facts = Facts(":memory:")
    facts.store("水费", "户号", "12345678")
    facts.store("牛奶", "品牌", "蒙牛")

    assert [r["value"] for r in facts.search("牛奶品牌")] == ["蒙牛"]
    assert facts.search("水费品牌") == []


def test_query_treats_wildcards_literally():
    facts = Facts(":memory:")
    facts.store("100%纯牛奶", "品牌", "蒙牛")
    facts.store("水费", "户号", "12345678")

    assert [r["subject"] for r in facts.search("%")] == ["100%纯牛奶"]


# ---------- 同义改写（2026-11-24） ----------

def test_query_accepts_synonyms():
    """存「户号」、问「账号」也要能查到（D46 遗留）。"""
    facts = Facts(":memory:")
    facts.store("水费", "户号", "12345678")
    facts.store("燃气", "户号", "87654321")

    assert [r["value"] for r in facts.search("水费账号")] == ["12345678"]
    assert [r["value"] for r in facts.search("煤气账号")] == ["87654321"]


def test_synonyms_do_not_break_precision():
    """只放宽那一个词：「电费」仍然必须命中，不能把水费一起捞出来。"""
    facts = Facts(":memory:")
    facts.store("水费", "户号", "11112222")
    facts.store("电费", "户号", "33334444")

    assert [r["value"] for r in facts.search("电费账号")] == ["33334444"]
    assert facts.search("燃气账号") == []


def test_synonyms_apply_to_filters():
    """subject / predicate 过滤也要跟着同义词走。"""
    facts = Facts(":memory:")
    facts.store("燃气", "户号", "87654321")

    assert [r["value"] for r in facts.search(subject="煤气")] == ["87654321"]
    assert [r["value"] for r in facts.search(predicate="账号")] == ["87654321"]


# ---------- D66：事实挂实体 ----------

def _facts_with_entities():
    from domain.store import AgentStore

    store = AgentStore(":memory:")
    store.add_entity("device", "净水器", aliases=["饮水机", "净水机"])
    facts = Facts(":memory:")
    facts.set_entity_lookup(store.find_any_entity)
    return facts, store


def test_fact_links_entity_by_alias():
    facts, store = _facts_with_entities()
    store.add_entity("device", "打印机", aliases=["惠普打印机"])

    facts.store("饮水机", "滤芯型号", "PP-100")
    facts.store("惠普打印机", "墨盒", "CF280A")

    rows = facts.search("饮水机")
    assert rows[0]["entity_id"] == store.find_any_entity("饮水机")["id"]
    assert facts.search("惠普打印机")[0]["predicate"] == "墨盒"


def test_alias_query_finds_canonical_fact():
    """用户习惯说别名：存的是「净水器」，问「饮水机」也要查得到。"""
    facts, _store = _facts_with_entities()
    facts.store("净水器", "滤芯型号", "PP-100")
    facts.store("净水器", "滤芯更换周期", "6 个月")

    values = [row["value"] for row in facts.search("饮水机滤芯型号")]
    assert values == ["PP-100"]          # 型号命中，不把「更换周期」也带出来
    assert len(facts.search("饮水机")) == 2   # 只问实体：给它全部


def test_entity_filter_query():
    facts, store = _facts_with_entities()
    entity = store.find_any_entity("净水器")
    facts.store("净水器", "滤芯型号", "PP-100")
    facts.store("牛奶", "品牌", "蒙牛")

    rows = facts.search(entity_id=entity["id"])
    assert [row["value"] for row in rows] == ["PP-100"]


def test_profile_subject_never_links_entity():
    """画像挂在「用户」前缀下，不能被同名实体吃掉。"""
    facts, _store = _facts_with_entities()
    facts.store("用户/妈妈", "口味", "不吃辣")
    assert facts.list_all()[0]["entity_id"] is None


def test_assistant_wires_entity_lookup():
    """Assistant 要把实体解析注入 Facts，否则别名永远对不上。"""
    from agent.clarify import Clarifier
    from assistant import Assistant
    from domain.store import AgentStore
    from domain.tasks import Tasks
    from items import Items
    from memory import Memory

    class DummyLLM:
        def chat_with_tools(self, *args, **kwargs):
            raise AssertionError("这轮不该调用模型")

    store = AgentStore(":memory:")
    facts = Facts(":memory:")
    items = Items(":memory:")
    assistant = Assistant(
        memory=Memory(":memory:"), llm=DummyLLM(), items=items,
        clarifier=Clarifier(store=store, items=items),
        tasks=Tasks(":memory:"), facts=facts,
    )
    assistant.entities.add_entity("device", "净水器", aliases=["饮水机"])
    facts.store("饮水机", "滤芯型号", "PP-100")

    assert [row["value"] for row in facts.search("净水器")] == ["PP-100"]


def test_old_database_gets_entity_column(tmp_path):
    """老库没有 entity_id 列：打开时补列，不丢数据。"""
    import sqlite3

    path = str(tmp_path / "facts.db")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE facts (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "subject TEXT NOT NULL, predicate TEXT NOT NULL, value TEXT NOT NULL, "
            "source TEXT, valid_from TEXT, valid_to TEXT, created_at TEXT)"
        )
        conn.execute(
            "INSERT INTO facts (subject, predicate, value) VALUES ('牛奶','品牌','蒙牛')"
        )

    facts = Facts(path)
    row = facts.list_all()[0]
    assert row["value"] == "蒙牛"
    assert row["entity_id"] is None
