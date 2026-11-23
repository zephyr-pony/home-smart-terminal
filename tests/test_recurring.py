"""周期提醒：措辞与工具层（解析/滚动在 test_dates / test_tasks）。"""
from agent import wording
from domain.tasks import Tasks
from tools import get_task_tools


def test_recurring_summary_reads_naturally():
    """周期提醒说「每天8点」，不说「明天8点」——明天反而误导。"""
    assert wording.summary("task_add", {
        "title": "吃药", "due_at": "2026-11-22T08:00", "recur": "daily",
    }) == "吃药，每天早上8点"
    assert wording.summary("task_add", {
        "title": "开会", "due_at": "2026-11-25T15:00", "recur": "weekly",
    }) == "开会，每周三下午3点"
    assert wording.recur_label("monthly", "2026-12-05T09:00") == "每月5号"
    assert wording.recur_label("", "2026-11-25T15:00") == ""


def test_task_list_marks_recurrence():
    tasks = Tasks(":memory:")
    task = tasks.add("吃药", "2026-11-22T08:00", recur="daily")
    tool = [item for item in get_task_tools(tasks)
            if item["schema"]["function"]["name"] == "task_list"][0]

    listing = tool["execute"]()

    assert "吃药" in listing
    assert "每天" in listing
    assert str(task["id"]) in listing


def test_task_complete_recurring_says_next_time():
    tasks = Tasks(":memory:")
    task = tasks.add("吃药", "2026-11-22T08:00", recur="daily")
    tool = [item for item in get_task_tools(tasks)
            if item["schema"]["function"]["name"] == "task_complete"][0]

    message = tool["execute"](task_id=task["id"])

    assert "下次" in message
    assert "每天" in message
