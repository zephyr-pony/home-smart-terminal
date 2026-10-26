import sqlite3
import time

from memory import Memory


def test_store_and_search_chinese_substring():
    memory = Memory(":memory:")
    memory.store("酸奶8月5号过期", "酸奶8月5号过期")

    assert any("酸奶" in item for item in memory.search("酸奶"))
    assert any("过期" in item for item in memory.search("过期"))
    assert any("酸奶" in item for item in memory.search("酸奶 过期"))


def test_search_indexes_summary_not_raw_text():
    memory = Memory(":memory:")
    memory.store("冰箱里那瓶酸酸的奶", "酸奶过期")

    assert memory.search("酸酸的奶") == []
    found = memory.search("酸奶")
    assert found and "酸酸的奶" in found[0]


def test_list_all_and_delete_by_keywords():
    memory = Memory(":memory:")
    memory.store("第一条", "第一条")
    time.sleep(0.002)
    memory.store("第二条", "第二条")

    listed = memory.list_all()
    assert [item["summary"] for item in listed] == ["第二条", "第一条"]

    deleted = memory.delete_by_keywords("第一条")
    assert [item["summary"] for item in deleted] == ["第一条"]
    assert memory.count() == 1
    assert memory.search("第一条") == []


def test_legacy_single_table_migration(tmp_path):
    db_path = tmp_path / "memory.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE VIRTUAL TABLE memories USING fts5(summary, text, timestamp)"
    )
    conn.execute(
        "INSERT INTO memories (summary, text, timestamp) VALUES (?, ?, ?)",
        ("旧记忆", "旧记忆原文", "2026-01-01T10:00:00"),
    )
    conn.commit()
    conn.close()

    memory = Memory(str(db_path))

    assert memory.count() == 1
    assert [item["summary"] for item in memory.list_all()] == ["旧记忆"]
    assert any("旧记忆" in item for item in memory.search("旧记忆"))
