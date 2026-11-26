from datetime import date, timedelta

from agent.clarify import Clarifier
from agent.propose import ProposalService
from assistant import Assistant
from domain.facts import Facts
from domain.store import AgentStore
from domain.tasks import Tasks
from items import Items
from memory import Memory

# 别写死日期：物品合并只对「还没过期」的记录生效，写死的月末日期过几天就失效
SOON = (date.today() + timedelta(days=5)).isoformat()
LATER = (date.today() + timedelta(days=8)).isoformat()


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


def test_task_add_dedupes_within_window():
    store = AgentStore(":memory:")
    service = ProposalService(
        store, Memory(":memory:"), Items(":memory:"), Tasks(":memory:"), Facts(":memory:")
    )

    assert service.add_task("交电费", "2026-11-10", "bill").startswith("已创建任务")
    assert service.add_task("交电费", "2026-11-10", "bill") == "已存在相同任务，未重复创建"
    assert service.tasks.count() == 1


def test_fact_store_dedupes_within_window():
    store = AgentStore(":memory:")
    service = ProposalService(
        store, Memory(":memory:"), Items(":memory:"), Tasks(":memory:"), Facts(":memory:")
    )

    assert service.store_fact("爸妈家", "电费户号", "123456").startswith("已保存")
    assert service.store_fact("爸妈家", "电费户号", "123456") == "已存在相同事实，未重复保存"
    assert service.facts.count() == 1


def test_assistant_memory_tool_uses_proposal_service():
    items = Items(":memory:")
    store = AgentStore(":memory:")
    assistant = Assistant(
        memory=Memory(":memory:"),
        items=items,
        llm=object(),
        clarifier=Clarifier(store=store, items=items),
        tasks=Tasks(":memory:"),
        facts=Facts(":memory:"),
    )
    tool = next(
        tool for tool in assistant.tools
        if tool["schema"]["function"]["name"] == "memory_store"
    )

    assert tool["execute"]("明天交电费", "11月6日交电费") == "已存储"
    assert tool["execute"]("明天交电费", "11月6日交电费") == "已存在相同记忆，未重复写入"
    assert assistant.memory.count() == 1


def test_item_add_merges_same_name_with_new_expiry():
    """同名物品再报一次日期应该更新，而不是多出一条。"""
    service, _ = make_service()
    service.add_item("牛奶", SOON)
    message = service.add_item("牛奶", LATER)

    rows = service.items.list_all()
    assert len(rows) == 1
    assert rows[0]["expiry_date"] == LATER
    assert "已更新" in message


def test_item_add_same_name_without_date_keeps_old():
    service, _ = make_service()
    service.add_item("牛奶", SOON)
    message = service.add_item("牛奶")

    rows = service.items.list_all()
    assert len(rows) == 1
    assert rows[0]["expiry_date"] == SOON
    assert "未重复添加" in message


def test_item_add_same_name_same_date_is_noop():
    service, _ = make_service()
    service.add_item("牛奶", SOON)
    message = service.add_item("牛奶", SOON)

    assert len(service.items.list_all()) == 1
    assert "未重复添加" in message


def test_item_add_different_names_stay_separate():
    service, _ = make_service()
    service.add_item("牛奶", "2026-11-25")
    service.add_item("酸奶", "2026-11-25")

    assert len(service.items.list_all()) == 2


def test_item_add_after_expired_creates_new_row():
    """已经过期的旧记录不该被新买的同名物品改掉，保留历史。"""
    service, _ = make_service()
    service.items.add("牛奶", "2026-01-01")   # 早已过期
    service.add_item("牛奶", "2026-12-01")

    rows = service.items.list_all()
    assert len(rows) == 2
    active = [r for r in rows if r["status"] != "expired"]
    assert len(active) == 1
    assert active[0]["expiry_date"] == "2026-12-01"


def test_item_merge_records_audit_status():
    service, store = make_service()
    service.add_item("牛奶", SOON)
    service.add_item("牛奶", LATER)

    statuses = [p.get("status") for p in store.list_proposals()]
    assert "merged" in statuses