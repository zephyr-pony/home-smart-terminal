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

# ---------- 周期提醒（2026-11-23） ----------

def test_recurring_daily_rolls_after_reminder():
    tasks = Tasks(":memory:")
    tasks.add("吃药", "2026-11-21T08:00", type="custom", recur="daily")

    claimed = tasks.claim_due_reminders(
        days=3, cooldown_hours=24, now=datetime(2026, 11, 21, 8, 0, 5)
    )

    assert [task["title"] for task in claimed] == ["吃药"]
    row = tasks.get(claimed[0]["id"])
    assert row["due_at"] == "2026-11-22T08:00:00"
    assert row["status"] == "pending"
    assert row["reminded_at"] is None  # 冷却不挡住下一次

    # 第二天同一时刻还能再提醒
    again = tasks.claim_due_reminders(
        days=3, cooldown_hours=24, now=datetime(2026, 11, 22, 8, 0, 5)
    )
    assert [task["title"] for task in again] == ["吃药"]


def test_recurring_not_reminded_before_due():
    tasks = Tasks(":memory:")
    tasks.add("吃药", "2026-11-21T20:00", recur="daily")

    assert tasks.claim_due_reminders(
        days=3, cooldown_hours=24, now=datetime(2026, 11, 21, 10, 0)
    ) == []


def test_recurring_skips_missed_occurrences():
    """服务关了一周再开：只补一次，然后滚到下一次，不连放七条。"""
    tasks = Tasks(":memory:")
    tasks.add("吃药", "2026-11-01T08:00", recur="daily")

    claimed = tasks.claim_due_reminders(
        days=3, cooldown_hours=24, now=datetime(2026, 11, 21, 10, 0)
    )

    assert len(claimed) == 1
    assert tasks.get(claimed[0]["id"])["due_at"] == "2026-11-22T08:00:00"


def test_complete_recurring_skips_to_next():
    tasks = Tasks(":memory:")
    task = tasks.add("吃药", "2026-11-21T08:00", recur="daily")

    assert tasks.complete(task["id"], now=datetime(2026, 11, 21, 8, 30)) is True

    row = tasks.get(task["id"])
    assert row["status"] == "pending"
    assert row["due_at"] == "2026-11-22T08:00:00"


def test_complete_one_shot_still_marks_done():
    tasks = Tasks(":memory:")
    task = tasks.add("交电费", "2026-11-21")

    assert tasks.complete(task["id"]) is True
    assert tasks.get(task["id"])["status"] == "done"


def test_monthly_roll_uses_last_day_of_short_month():
    tasks = Tasks(":memory:")
    task = tasks.add("交房租", "2026-01-31T09:00", recur="monthly")

    tasks.complete(task["id"], now=datetime(2026, 1, 31, 10, 0))

    assert tasks.get(task["id"])["due_at"] == "2026-02-28T09:00:00"


def test_recurring_requires_due_time():
    tasks = Tasks(":memory:")
    with pytest.raises(ValueError):
        tasks.add("吃药", None, recur="daily")


def test_mode_defaults_to_timely_and_can_be_soft():
    tasks = Tasks(":memory:")

    timely = tasks.add("吃药", "2026-11-05T08:00")
    soft = tasks.add("取快递", "2026-11-05", mode="soft")

    assert timely["mode"] == "timely"
    assert tasks.get(timely["id"])["mode"] == "timely"
    assert tasks.get(soft["id"])["mode"] == "soft"


def test_mode_can_be_changed_after_the_fact():
    """用户说「这个不用准点提醒」：改一行，别重建任务。"""
    tasks = Tasks(":memory:")
    task = tasks.add("取快递", "2026-11-05")

    assert tasks.update(task["id"], mode="soft") is True
    assert tasks.get(task["id"])["mode"] == "soft"

    with pytest.raises(ValueError):
        tasks.update(task["id"], mode="有时提")


def test_claim_respects_mode():
    tasks = Tasks(":memory:")
    tasks.add("吃药", "2026-11-05T08:00")
    tasks.add("取快递", "2026-11-05", mode="soft")
    now = datetime(2026, 11, 5, 12, 0, 0)

    timely = tasks.claim_due_reminders(days=3, cooldown_hours=24, now=now, mode="timely")
    assert [task["title"] for task in timely] == ["吃药"]
    soft = tasks.claim_due_reminders(days=3, cooldown_hours=24, now=now, mode="soft")
    assert [task["title"] for task in soft] == ["取快递"]


def test_get_soft_due_is_today_and_overdue_only():
    tasks = Tasks(":memory:")
    tasks.add("后天取快递", "2026-11-07", mode="soft")
    tasks.add("今天取快递", "2026-11-05", mode="soft")
    tasks.add("逾期取快递", "2026-11-03", mode="soft")
    tasks.add("吃药", "2026-11-05T08:00")
    # 周期任务不进聊天上下文：「每天八点吃药」本来就该准点响
    tasks.add("每天浇花", "2026-11-05T20:00", recur="daily", mode="soft")
    now = datetime(2026, 11, 5, 12, 0, 0)

    due = tasks.get_soft_due(now=now)

    assert [task["title"] for task in due] == ["逾期取快递", "今天取快递"]
    assert [task["days_left"] for task in due] == [-2, 0]


def test_get_soft_due_skips_done_and_respects_limit():
    tasks = Tasks(":memory:")
    ids = [
        tasks.add("事%d" % n, "2026-11-05", mode="soft")["id"]
        for n in range(5)
    ]
    tasks.complete(ids[0])

    due = tasks.get_soft_due(limit=2, now=datetime(2026, 11, 5, 12, 0, 0))

    assert len(due) == 2
    assert all(task["id"] != ids[0] for task in due)
