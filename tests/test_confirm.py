from agent.confirm import ConfirmationManager
from domain.store import AgentStore
from items import Items
from memory import Memory


def make_manager():
    store = AgentStore(":memory:")
    memory = Memory(":memory:")
    items = Items(":memory:")
    return ConfirmationManager(store, memory, items), memory, items


def test_memory_delete_requires_confirmation():
    manager, memory, _items = make_manager()
    memory.store("酸奶8月5号过期", "酸奶过期")
    manager.request("c1", "memory_delete", {"keywords": "酸奶"}, "确认删除？")

    result = manager.handle(manager.get_pending("c1"), "确认")

    assert result["kind"] == "done"
    assert result["type"] == "delete"
    assert memory.count() == 0
    assert manager.get_pending("c1") is None


def test_memory_delete_cancel_keeps_data():
    manager, memory, _items = make_manager()
    memory.store("酸奶8月5号过期", "酸奶过期")
    manager.request("c1", "memory_delete", {"keywords": "酸奶"}, "确认删除？")

    result = manager.handle(manager.get_pending("c1"), "取消")

    assert result["kind"] == "cancelled"
    assert memory.count() == 1
    assert manager.get_pending("c1") is None


def test_item_delete_requires_confirmation():
    manager, _memory, items = make_manager()
    items.add("蒙牛牛奶")
    manager.request("c1", "item_delete", {"name": "牛奶"}, "确认删除？")

    result = manager.handle(manager.get_pending("c1"), "好的")

    assert result["kind"] == "done"
    assert result["type"] == "chat"
    assert items.count() == 0


def test_unclear_reply_reasks():
    manager, _memory, _items = make_manager()
    manager.request("c1", "memory_delete", {"keywords": "酸奶"}, "确认删除？")

    result = manager.handle(manager.get_pending("c1"), "我想想")

    assert result["kind"] == "reask"
    assert manager.get_pending("c1") is not None
