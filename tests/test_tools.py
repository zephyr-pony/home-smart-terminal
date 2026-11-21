"""工具层：family_search 合并查询。"""
from domain.tasks import Tasks
from items import Items
from memory import Memory
from tools import get_family_search_tool


def test_family_search_combines_sources():
    memory = Memory(":memory:")
    items = Items(":memory:")
    tasks = Tasks(":memory:")
    items.add("牛奶", "2026-12-01")
    memory.store("上周买了牛奶", "买了牛奶")
    tasks.add("交电费", "2026-11-22", type="bill")

    tool = get_family_search_tool(memory, items, tasks)
    output = tool["execute"](keyword="牛奶")

    assert "【物品】" in output
    assert "牛奶" in output
    assert "【记忆】" in output
    # 关键词不匹配的待办不应该混进来
    assert "交电费" not in output

    all_output = tool["execute"](keyword="")
    assert "【待办】" in all_output
    assert "交电费" in all_output


def test_family_search_empty_result():
    memory = Memory(":memory:")
    items = Items(":memory:")
    tool = get_family_search_tool(memory, items)
    assert tool["execute"](keyword="不存在的") == "（没有找到相关记录）"


def test_family_search_registered_in_all_tools():
    from tools import get_all_tools

    memory = Memory(":memory:")
    items = Items(":memory:")
    names = [t["schema"]["function"]["name"] for t in get_all_tools(memory, items)]
    assert "family_search" in names
