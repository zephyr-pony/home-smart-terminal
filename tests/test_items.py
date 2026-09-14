from datetime import date, timedelta

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
