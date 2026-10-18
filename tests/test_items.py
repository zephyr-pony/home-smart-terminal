from datetime import date, datetime, timedelta

import pytest

from items import Items


def test_items_validate_dates_and_sync_expired_status():
    items = Items(":memory:")
    items.add("酸奶", "2020-01-01")
    assert items.list_all()[0]["status"] == "expired"

    with pytest.raises(ValueError):
        items.add("牛奶", "2026-02-30")

    with pytest.raises(ValueError):
        items.add("", None)


def test_items_due_results_are_sorted_and_include_days_left():
    items = Items(":memory:")
    today = date.today()
    items.add("后到期", (today + timedelta(days=2)).isoformat())
    items.add("已过期", "2020-01-01")

    due = items.get_due(days=3)
    assert [item["name"] for item in due] == ["已过期", "后到期"]
    assert due[0]["days_left"] < 0
    assert due[1]["days_left"] == 2


def test_items_search_and_delete_are_stable():
    items = Items(":memory:")
    items.add("酸奶大瓶")
    items.add("酸奶小瓶")
    assert [item["name"] for item in items.search("酸奶")] == ["酸奶小瓶", "酸奶大瓶"]
    deleted = items.delete("酸奶")
    assert [item["name"] for item in deleted] == ["酸奶大瓶", "酸奶小瓶"]
    assert items.search("酸奶") == []


def test_claim_due_reminders_dedup_within_cooldown():
    items = Items(":memory:")
    items.add("酸奶", "2026-01-02")
    now = datetime(2026, 1, 1, 12, 0, 0)

    first = items.claim_due_reminders(days=3, cooldown_hours=24, now=now)
    assert [i["name"] for i in first] == ["酸奶"]
    assert first[0]["days_left"] == 1
    # 冷却期内不重复提醒
    assert items.claim_due_reminders(days=3, cooldown_hours=24, now=now) == []


def test_claim_due_reminders_again_after_cooldown():
    items = Items(":memory:")
    items.add("酸奶", "2026-01-02")
    now = datetime(2026, 1, 1, 12, 0, 0)

    assert len(items.claim_due_reminders(days=3, cooldown_hours=24, now=now)) == 1
    later = now + timedelta(hours=25)
    assert len(items.claim_due_reminders(days=3, cooldown_hours=24, now=later)) == 1


def test_claim_due_reminders_once_when_cooldown_zero():
    items = Items(":memory:")
    items.add("酸奶", "2026-01-02")
    now = datetime(2026, 1, 1, 12, 0, 0)

    assert len(items.claim_due_reminders(days=3, cooldown_hours=0, now=now)) == 1
    assert items.claim_due_reminders(days=3, cooldown_hours=0, now=now) == []


def test_claim_due_reminders_skips_far_off_items():
    items = Items(":memory:")
    items.add("远期的", "2026-01-20")
    now = datetime(2026, 1, 1, 12, 0, 0)
    assert items.claim_due_reminders(days=3, cooldown_hours=24, now=now) == []


def test_claim_marks_status_reminded():
    items = Items(":memory:")
    today = date.today()
    items.add("酸奶", (today + timedelta(days=2)).isoformat())
    items.claim_due_reminders(days=3, cooldown_hours=24)
    assert items.list_all()[0]["status"] == "reminded"
