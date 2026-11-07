from datetime import datetime, timedelta

import pytest

from domain.tasks import Tasks


def test_add_and_list_tasks():
    tasks = Tasks(":memory:")
    tasks.add("交电费", "2026-11-10", type="bill")
    tasks.add("开会", "2026-11-09T09:00", type="schedule")

    listed = tasks.list_all()

    assert [task["title"] for task in listed] == ["开会", "交电费"]
    assert listed[0]["type"] == "schedule"
    assert tasks.count() == 2


def test_get_due_is_sorted_and_skips_far_tasks():
    tasks = Tasks(":memory:")
    tasks.add("后天", "2026-11-07")
    tasks.add("已过期", "2026-11-01")
    tasks.add("远期", "2026-12-01")
    now = datetime(2026, 11, 5, 12, 0, 0)

    due = tasks.get_due(days=3, now=now)

    assert [task["title"] for task in due] == ["已过期", "后天"]
    assert due[0]["days_left"] < 0
    assert due[1]["days_left"] == 2


def test_claim_due_reminders_cooldown():
    tasks = Tasks(":memory:")
    tasks.add("交电费", "2026-11-06")
    now = datetime(2026, 11, 5, 12, 0, 0)

    first = tasks.claim_due_reminders(days=3, cooldown_hours=24, now=now)
    assert [task["title"] for task in first] == ["交电费"]

    assert tasks.claim_due_reminders(days=3, cooldown_hours=24, now=now) == []

    later = now + timedelta(hours=25)
    assert len(tasks.claim_due_reminders(days=3, cooldown_hours=24, now=later)) == 1


def test_claim_due_reminders_once_when_cooldown_zero():
    tasks = Tasks(":memory:")
    tasks.add("交电费", "2026-11-06")
    now = datetime(2026, 11, 5, 12, 0, 0)

    assert len(tasks.claim_due_reminders(days=3, cooldown_hours=0, now=now)) == 1
    assert tasks.claim_due_reminders(days=3, cooldown_hours=0, now=now) == []


def test_complete_task_removes_it_from_due():
    tasks = Tasks(":memory:")
    task = tasks.add("交电费", "2026-11-06")

    assert tasks.complete(task["id"])
    assert tasks.list_all()[0]["status"] == "done"
    assert tasks.get_due(days=3, now=datetime(2026, 11, 5, 12)) == []


def test_invalid_due_date_is_rejected():
    tasks = Tasks(":memory:")

    with pytest.raises(ValueError):
        tasks.add("交电费", "2026-02-30")
