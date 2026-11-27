import sqlite3
import time

from memory import Memory


def test_store_and_search_chinese_substring():
    memory = Memory(":memory:")
    memory.store("酸奶8月5号过期", "酸奶8月5号过期")

    assert any("酸奶" in item for item in memory.search("酸奶"))
    assert any("过期" in item for item in memory.search("过期"))
    assert any("酸奶" in item for item in memory.search("酸奶 过期"))


def test_search_falls_back_to_raw_text():
    """摘要里没有、原文里有的词也要能查到（FTS 查不到时用 LIKE 兜底）。"""
    memory = Memory(":memory:")
    memory.store("冰箱里那瓶酸酸的奶", "酸奶过期")

    found = memory.search("酸酸的奶")
    assert found and "酸酸的奶" in found[0]

    # 摘要命中时仍然正常
    assert memory.search("酸奶")
    # 确实没有的东西仍然返回空
    assert memory.search("纯牛奶") == []


def test_like_fallback_escapes_wildcards():
    """兜底用 LIKE 时必须转义 % 和 _，否则搜一个百分号会把全部记忆捞出来。"""
    memory = Memory(":memory:")
    memory.store("物业费涨了 5%", "物业费上涨")
    memory.store("冰箱里还有三个鸡蛋", "冰箱三个鸡蛋")

    # 搜 % 只应命中真正含 % 的那条，而不是把全部记忆捞出来
    found = memory.search("%")
    assert len(found) == 1
    assert "5%" in found[0]
    assert memory.search("_") == []


def test_search_details_and_delete_by_id():
    memory = Memory(":memory:")
    memory.store("酸奶8月5号过期", "酸奶过期")

    details = memory.search_details("酸奶")
    assert details[0]["text"] == "酸奶8月5号过期"

    assert memory.delete_by_id(details[0]["id"])
    assert memory.count() == 0
    assert memory.delete_by_id(999) is False


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


# ---------- D83：检索时按实体补召回（别名跨词） ----------

def _memory_with_entities():
    from domain.store import AgentStore

    store = AgentStore(":memory:")
    store.add_entity("product", "净水器", aliases=["饮水机", "净水机"])
    memory = Memory(":memory:")
    memory.set_entity_lookup(store.find_any_entity)
    return memory, store


def test_alias_query_finds_memory():
    """库里写的是「净水器」，用户问「饮水机」也要捞得到。"""
    memory, store = _memory_with_entities()
    memory.store("净水器滤芯上个月刚换", "净水器滤芯刚换")

    found = memory.search("饮水机")
    assert len(found) == 1 and "净水器滤芯" in found[0]
    assert memory.search_details("饮水机")[0]["text"] == "净水器滤芯上个月刚换"

    # 没注入实体解析时行为不变：别名查不到（原来就是这个结果）
    plain = Memory(":memory:")
    plain.store("净水器滤芯上个月刚换", "净水器滤芯刚换")
    assert plain.search("饮水机") == []


def test_entity_expansion_does_not_widen_other_queries():
    """补召回只跟着查询里认出的实体走，别把无关记忆一起捞出来。"""
    memory, _store = _memory_with_entities()
    memory.store("牛奶放在冰箱第二层", "牛奶位置")
    memory.store("酸奶今天到期", "酸奶到期")

    found = memory.search("牛奶")
    assert found and all("酸奶" not in item for item in found)
    # 库里没有的东西仍然返回空（空结果要能触发「没查到」的答复）
    assert memory.search("面包") == []


def test_entity_rows_fill_after_text_hits():
    """原文命中的排在前面，实体补的跟在后面，不重复。"""
    memory, _store = _memory_with_entities()
    memory.store("净水器滤芯上个月刚换", "净水器滤芯刚换")
    memory.store("净水器是去年双十一买的", "净水器购买时间")

    found = memory.search("饮水机 滤芯")
    assert len(found) == 2
    assert "滤芯" in found[0]              # 关键词命中的那条在前
    assert any("双十一" in item for item in found[1:])
    assert len(set(found)) == 2


def test_delete_by_alias_stays_literal():
    """删除不走实体展开：说「忘掉饮水机」不该删掉写成「净水器」的记忆。"""
    memory, _store = _memory_with_entities()
    memory.store("净水器滤芯上个月刚换", "净水器滤芯刚换")

    assert memory.delete_by_keywords("饮水机") == []
    assert memory.count() == 1
    assert memory.search("净水器")


def test_assistant_wires_memory_entity_lookup():
    """Assistant 要把实体解析注入记忆库，否则别名在记忆这条路上永远对不上。"""
    from agent.clarify import Clarifier
    from assistant import Assistant
    from domain.store import AgentStore
    from domain.tasks import Tasks
    from items import Items

    class DummyLLM:
        def chat_with_tools(self, *args, **kwargs):
            raise AssertionError("这轮不该调用模型")

    store = AgentStore(":memory:")
    items = Items(":memory:")
    assistant = Assistant(
        memory=Memory(":memory:"), llm=DummyLLM(), items=items,
        clarifier=Clarifier(store=store, items=items),
        tasks=Tasks(":memory:"),
    )
    assistant.entities.add_entity("product", "净水器", aliases=["饮水机"])
    assistant.memory.store("净水器滤芯上个月刚换", "净水器滤芯刚换")

    assert assistant.memory.search("饮水机")
