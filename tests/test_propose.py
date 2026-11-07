from agent.clarify import Clarifier
from agent.propose import ProposalService
from assistant import Assistant
from domain.store import AgentStore
from items import Items
from memory import Memory


def make_service():
    store = AgentStore(":memory:")
    return ProposalService(store, Memory(":memory:"), Items(":memory:")), store


def test_memory_store_dedupes_within_window():
    service, store = make_service()

    assert service.store_memory("明天交电费", "11月6日交电费") == "已存储"
    assert service.store_memory("明天交电费", "11月6日交电费") == "已存在相同记忆，未重复写入"

    assert service.memory.count() == 1
    statuses = [proposal["status"] for proposal in store.list_proposals()]
    assert statuses == ["duplicate", "committed"]


def test_memory_store_rejects_empty_text():
    service, _store = make_service()

    assert service.store_memory("", "x") == "无法记录：记忆原文不能为空"
    assert service.memory.count() == 0


def test_item_add_dedupes_and_allows_different_expiry():
    service, _store = make_service()

    assert service.add_item("牛奶", "2026-11-10").startswith("已添加物品")
    assert service.add_item("牛奶", "2026-11-10") == "已存在相同物品，未重复添加"
    assert service.items.count() == 1

    assert service.add_item("牛奶", "2026-11-12").startswith("已添加物品")
    assert service.items.count() == 2


def test_item_add_invalid_date_is_rejected():
    service, store = make_service()

    result = service.add_item("牛奶", "2026-02-30")

    assert "无法添加物品" in result
    assert service.items.count() == 0
    assert store.list_proposals()[0]["status"] == "rejected"


def test_assistant_memory_tool_uses_proposal_service():
    items = Items(":memory:")
    store = AgentStore(":memory:")
    assistant = Assistant(
        memory=Memory(":memory:"),
        items=items,
        llm=object(),
        clarifier=Clarifier(store=store, items=items),
    )
    tool = next(
        tool for tool in assistant.tools
        if tool["schema"]["function"]["name"] == "memory_store"
    )

    assert tool["execute"]("明天交电费", "11月6日交电费") == "已存储"
    assert tool["execute"]("明天交电费", "11月6日交电费") == "已存在相同记忆，未重复写入"
    assert assistant.memory.count() == 1
