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
