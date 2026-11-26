from agent.clarify import Clarifier
from domain.store import AgentStore
from items import Items


def make_clarifier(items=None):
    return Clarifier(store=AgentStore(":memory:"), items=items or Items(":memory:"))


def test_bill_asks_when_multiple_properties():
    clarifier = make_clarifier()
    clarifier.store.add_entity("property", "自己家")
    clarifier.store.add_entity("property", "爸妈家")

    result = clarifier.check("明天要交电费", "c1")

    assert result["kind"] == "clarify"
    assert "自己家" in result["reply"]
    assert "爸妈家" in result["reply"]
    assert result["pending_action"]["missing_slot"] == "property"


def test_bill_uses_single_known_property():
    clarifier = make_clarifier()
    clarifier.store.add_entity("property", "自己家")

    assert clarifier.check("明天要交电费", "c1") == {
        "kind": "resolved",
        "text": "自己家，明天要交电费",
    }


def test_bill_resolves_and_clears_pending():
    clarifier = make_clarifier()
    clarifier.store.add_entity("property", "自己家")
    clarifier.store.add_entity("property", "爸妈家")

    clarifier.check("明天要交电费", "c1")
    result = clarifier.check("爸妈家", "c1")

    assert result == {
        "kind": "resolved",
        "text": "爸妈家，明天要交电费",
    }
    assert clarifier.store.get_pending("c1") is None


def test_bill_no_question_when_property_explicit():
    clarifier = make_clarifier()
    clarifier.store.add_entity("property", "爸妈家")

    assert clarifier.check("爸妈家明天要交电费", "c1") is None


def test_other_bill_types_use_the_same_property_rule():
    clarifier = make_clarifier()
    clarifier.store.add_entity("property", "自己家")
    clarifier.store.add_entity("property", "爸妈家")

    result = clarifier.check("下个月要交暖气费", "c1")

    assert result["kind"] == "clarify"
    assert "暖气费" in result["reply"]


def test_property_alias_skips_clarification():
    clarifier = make_clarifier()
    clarifier.store.add_entity("property", "自己家", aliases=["咱家"])

    assert clarifier.check("咱家明天要交电费", "c1") is None


def test_bill_completion_skips_clarification():
    clarifier = make_clarifier()
    clarifier.store.add_entity("property", "自己家")
    clarifier.store.add_entity("property", "爸妈家")

    assert clarifier.check("电费交了", "c1") is None
    assert clarifier.check("电费已经交了", "c1") is None


def test_bill_learns_new_property_after_answer():
    clarifier = make_clarifier()

    first = clarifier.check("明天要交电费", "c1")
    assert first["kind"] == "clarify"

    second = clarifier.check("自己家", "c1")
    assert second == {"kind": "resolved", "text": "自己家，明天要交电费"}
    assert [e["canonical_name"] for e in clarifier.store.list_entities("property")] == ["自己家"]


def test_cancel_pending():
    clarifier = make_clarifier()
    clarifier.store.add_entity("property", "自己家")
    clarifier.store.add_entity("property", "爸妈家")
    clarifier.check("明天要交电费", "c1")

    result = clarifier.check("算了", "c1")

    assert result == {"kind": "cancelled", "reply": "好的，先不记了。"}
    assert clarifier.store.get_pending("c1") is None


def test_expiry_asks_when_multiple_items():
    items = Items(":memory:")
    items.add("蒙牛牛奶")
    items.add("伊利牛奶")
    clarifier = make_clarifier(items)

    result = clarifier.check("牛奶要过期了", "c1")

    assert result["kind"] == "clarify"
    assert "蒙牛牛奶" in result["reply"]
    assert "伊利牛奶" in result["reply"]

    resolved = clarifier.check("伊利那盒", "c1")
    assert resolved["kind"] == "resolved"
    assert resolved["text"].startswith("伊利牛奶，")


def test_expiry_accepts_colloquial_brand_answer():
    items = Items(":memory:")
    items.add("蒙牛牛奶")
    items.add("伊利牛奶")
    clarifier = make_clarifier(items)
    clarifier.check("牛奶要过期了", "c1")

    resolved = clarifier.check("蒙牛的", "c1")

    assert resolved["kind"] == "resolved"
    assert resolved["text"].startswith("蒙牛牛奶，")


def test_expiry_no_question_when_one_item():
    items = Items(":memory:")
    items.add("蒙牛牛奶")
    clarifier = make_clarifier(items)

    assert clarifier.check("牛奶要过期了", "c1") is None


def test_expiry_no_question_when_item_is_explicit():
    items = Items(":memory:")
    items.add("蒙牛牛奶")
    items.add("伊利牛奶")
    clarifier = make_clarifier(items)

    assert clarifier.check("蒙牛牛奶要过期了", "c1") is None

def test_cancel_bill_reminder_not_clarified():
    """「交电费的提醒不用了」是取消已有任务，不该追问是哪个家。"""
    clarifier = Clarifier(store=AgentStore(":memory:"), items=Items(":memory:"))
    assert clarifier.check("交电费的提醒不用了", "c1") is None
    assert clarifier.check("把电费提醒取消掉", "c1") is None
    # 新建账单仍然要追问
    assert clarifier.check("明天交电费", "c1") is not None


def test_recurring_without_time_asks_clock():
    """「每天提醒我吃药」缺时刻：先问几点，别让模型自己编一个。"""
    clarifier = make_clarifier()
    result = clarifier.check("每天提醒我吃药", "c1")

    assert result["kind"] == "clarify"
    assert "几点" in result["reply"]
    assert "每天" in result["reply"]
    assert result["pending_action"]["missing_slot"] == "time"

    resolved = clarifier.check("早上八点", "c1")
    assert resolved["kind"] == "resolved"
    assert resolved["text"] == "每天提醒我吃药，早上八点"


def test_recurring_weekday_keeps_the_day_in_question():
    clarifier = make_clarifier()
    result = clarifier.check("每周三提醒我开会", "c1")
    assert result["kind"] == "clarify"
    assert "每周三" in result["reply"]


def test_recurring_with_time_is_not_clarified():
    clarifier = make_clarifier()
    assert clarifier.check("每天早上八点提醒我吃药", "c1") is None
    assert clarifier.check("每周三下午三点提醒我开会", "c1") is None
    assert clarifier.check("每天提醒我喝水，8:30", "c1") is None


def test_time_rule_ignores_non_reminders():
    """「每天省点钱」不是安排提醒，不能弹「几点提醒你」。"""
    clarifier = make_clarifier()
    assert clarifier.check("每天省点钱", "c1") is None
    assert clarifier.check("每天都很忙", "c1") is None