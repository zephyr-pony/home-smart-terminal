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

def _task_tool(tasks, name):
    from tools import get_task_tools

    return [t for t in get_task_tools(tasks) if t["schema"]["function"]["name"] == name][0]


def test_task_update_changes_fields():
    tasks = Tasks(":memory:")
    task = tasks.add("取快递", "2026-11-23", "custom")
    tool = _task_tool(tasks, "task_update")

    output = tool["execute"](task_id=task["id"], title="取快递（顺路买牛奶）",
                             due_at="2026-11-24T15:00", type="schedule")

    updated = tasks.list_all()[0]
    assert updated["title"] == "取快递（顺路买牛奶）"
    assert updated["due_at"].startswith("2026-11-24T15")
    assert updated["type"] == "schedule"
    assert "已更新" in output


def test_task_update_without_fields():
    tasks = Tasks(":memory:")
    task = tasks.add("取快递", None, "custom")
    tool = _task_tool(tasks, "task_update")
    assert tool["execute"](task_id=task["id"]) == "没有要修改的内容"


def test_task_update_missing_task():
    tasks = Tasks(":memory:")
    tool = _task_tool(tasks, "task_update")
    assert "没有找到" in tool["execute"](task_id=999, title="改名")


def test_task_cancel_marks_cancelled():
    tasks = Tasks(":memory:")
    task = tasks.add("下周开会", "2026-11-25", "schedule")
    tool = _task_tool(tasks, "task_cancel")

    assert "已取消" in tool["execute"](task_id=task["id"])
    assert tasks.list_all()[0]["status"] == "cancelled"
    assert "没有找到" in tool["execute"](task_id=task["id"])


def test_task_tools_registered():
    from tools import get_task_tools

    names = [t["schema"]["function"]["name"] for t in get_task_tools(Tasks(":memory:"))]
    assert "task_update" in names
    assert "task_cancel" in names