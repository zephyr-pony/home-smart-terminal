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


def test_update_and_delete_task():
    tasks = Tasks(":memory:")
    task = tasks.add("交电费", "2026-11-08", "bill")

    assert tasks.update(task["id"], title="交水费", due_at="2026-11-12", type="custom")
    updated = tasks.list_all()[0]
    assert updated["title"] == "交水费"
    assert updated["due_at"].startswith("2026-11-12")
    assert updated["type"] == "custom"

    assert tasks.update(task["id"], due_at="")
    assert tasks.list_all()[0]["due_at"] is None

    deleted = tasks.delete_by_id(task["id"])
    assert deleted["title"] == "交水费"
    assert tasks.count() == 0


def test_update_rejects_invalid_fields():
    tasks = Tasks(":memory:")
    task = tasks.add("交电费", "2026-11-08", "bill")

    with pytest.raises(ValueError):
        tasks.update(task["id"], title="")
    with pytest.raises(ValueError):
        tasks.update(task["id"], type="unknown")
    with pytest.raises(ValueError):
        tasks.update(task["id"], due_at="2026-02-30")


def test_invalid_due_date_is_rejected():
    tasks = Tasks(":memory:")

    with pytest.raises(ValueError):
        tasks.add("交电费", "2026-02-30")


def test_postpone_resets_reminder_state():
    """延期后必须能重新提醒：reminded_at 清掉、状态退回 pending。"""
    tasks = Tasks(":memory:")
    task = tasks.add("交电费", "2026-11-10", "bill")
    claimed = tasks.claim_due_reminders(
        days=3, cooldown_hours=0, now=datetime(2026, 11, 9, 12, 0)
    )
    assert len(claimed) == 1
    assert tasks.list_all()[0]["status"] == "reminded"

    updated = tasks.postpone(task["id"], 1, now=datetime(2026, 11, 9, 12, 0))

    assert updated["status"] == "pending"
    assert updated["reminded_at"] is None
    assert updated["due_at"].startswith("2026-11-11")
    # 冷却窗口内也能再提醒（reminded_at 已清空）
    again = tasks.claim_due_reminders(
        days=3, cooldown_hours=24, now=datetime(2026, 11, 9, 12, 5)
    )
    assert [t["id"] for t in again] == [task["id"]]


def test_postpone_rejects_finished_task():
    tasks = Tasks(":memory:")
    task = tasks.add("交电费", "2026-11-10", "bill")
    tasks.complete(task["id"])
    assert tasks.postpone(task["id"], 1) is None


def test_postpone_to_a_date_keeps_the_clock():
    """「取快递改到后天」没说几点：还是下午三点那个提醒，不是零点。"""
    tasks = Tasks(":memory:")
    task = tasks.add("取快递", "2026-11-24T15:00")

    updated = tasks.postpone(task["id"], due_at="2026-11-25")

    assert updated["due_at"] == "2026-11-25T15:00:00"


def test_postpone_with_explicit_time_overrides_the_clock():
    tasks = Tasks(":memory:")
    task = tasks.add("取快递", "2026-11-24T15:00")

    updated = tasks.postpone(task["id"], due_at="2026-11-25T09:30")

    assert updated["due_at"] == "2026-11-25T09:30:00"


def test_update_to_a_date_keeps_the_clock():
    tasks = Tasks(":memory:")
    task = tasks.add("取快递", "2026-11-24T15:00")

    tasks.update(task["id"], due_at="2026-11-26")

    assert tasks.list_all()[0]["due_at"] == "2026-11-26T15:00:00"