from datetime import date, datetime

from agent.legacy import apply_migration, classify_memory, parse_due, plan_migration
from domain.store import AgentStore
from domain.tasks import Tasks

# 2026-08-08 是周六
BASE = datetime(2026, 8, 8, 17, 30)


def test_parse_due_relative_days():
    assert parse_due("明天要交电费", BASE).isoformat() == "2026-08-09"
    assert parse_due("后天开会", BASE).isoformat() == "2026-08-10"
    assert parse_due("大后天体检", BASE).isoformat() == "2026-08-11"
    assert parse_due("今天买了牛奶", BASE).isoformat() == "2026-08-08"


def test_parse_due_weekdays_and_absolute_dates():
    assert parse_due("下周三开会", BASE).isoformat() == "2026-08-12"
    assert parse_due("周三开会", BASE).isoformat() == "2026-08-12"
    assert parse_due("周五取快递", BASE).isoformat() == "2026-08-14"
    assert parse_due("8月20号体检", BASE).isoformat() == "2026-08-20"
    assert parse_due("2026-09-01 面试", BASE).isoformat() == "2026-09-01"
    assert parse_due("3月5号交物业费", datetime(2026, 11, 19, 9, 0)).isoformat() == "2027-03-05"


def test_parse_due_returns_none_without_date():
    assert parse_due("冰箱里还有三个鸡蛋", BASE) is None
    assert parse_due("", BASE) is None


def test_classify_memory_types():
    bill, _ = classify_memory("明天要交电费", "明天交电费", BASE)
    assert bill == {"type": "bill", "title": "明天交电费", "due_at": "2026-08-09"}

    schedule, _ = classify_memory("下周三开会", "", BASE)
    assert schedule["type"] == "schedule"

    custom, _ = classify_memory("提醒我周五取快递", "", BASE)
    assert custom["type"] == "custom"
    assert custom["due_at"] == "2026-08-14"


def test_classify_memory_skips_non_task_content():
    task, reason = classify_memory("冰箱里还有三个鸡蛋", "", BASE)
    assert task is None
    assert "日期" in reason

    task, reason = classify_memory("今天换了鼠标电池", "", BASE)
    assert task is None
    assert "不是任务类" in reason


def test_plan_migration_does_not_write(tmp_path):
    store = AgentStore(str(tmp_path / "agent.db"))
    tasks = Tasks(str(tmp_path / "tasks.db"))
    try:
        plan = plan_migration(
            [(16, "明天交电费", "明天要交电费", "2026-08-08T17:11:45")],
            [(1, "酸奶", "2026-08-05", "expired")],
            tasks,
            store.list_entities(),
            today=date(2026, 8, 8),
        )
        assert len(plan["tasks"]) == 1
        assert plan["entities"] == [{"type": "product", "name": "酸奶"}]
        assert tasks.count() == 0
        assert store.list_entities() == []
    finally:
        store.close()
        tasks.close()


def test_apply_migration_is_idempotent(tmp_path):
    store = AgentStore(str(tmp_path / "agent.db"))
    tasks = Tasks(str(tmp_path / "tasks.db"))
    memory_rows = [
        (16, "明天交电费", "明天要交电费", "2026-08-08T17:11:45"),
        (24, "冰箱里还有三个鸡蛋", "冰箱里还有三个鸡蛋", "2026-08-18T04:50:04"),
    ]
    item_rows = [(1, "酸奶", "2026-08-05", "expired")]
    try:
        plan = plan_migration(
            memory_rows,
            item_rows,
            tasks,
            store.list_entities(),
            today=date(2026, 8, 8),
        )
        assert any(item["memory_id"] == 24 for item in plan["skipped"])

        result = apply_migration(plan, tasks, store)
        assert result == {"entities": 1, "tasks": 1}

        task = tasks.list_all()[0]
        assert task["type"] == "bill"
        assert task["due_at"].startswith("2026-08-09")
        assert task["payload"]["legacy_memory_id"] == 16

        # 再跑一次：不新增实体、不新增任务
        plan_again = plan_migration(
            memory_rows,
            item_rows,
            tasks,
            store.list_entities(),
            today=date(2026, 8, 8),
        )
        assert plan_again["entities"] == []
        assert plan_again["tasks"] == []
        apply_migration(plan_again, tasks, store)

        assert tasks.count() == 1
        assert [entity["canonical_name"] for entity in store.list_entities()] == ["酸奶"]

        reasons = [item["reason"] for item in plan_again["skipped"]]
        assert "已迁移过" in reasons
    finally:
        store.close()
        tasks.close()


def test_plan_skips_past_due_unless_forced(tmp_path):
    store = AgentStore(str(tmp_path / "agent.db"))
    tasks = Tasks(str(tmp_path / "tasks.db"))
    rows = [(16, "明天交电费", "明天要交电费", "2026-08-08T17:11:45")]
    try:
        plan = plan_migration(
            rows, [], tasks, store.list_entities(), today=date(2026, 11, 19)
        )
        assert plan["tasks"] == []
        assert "日期已过" in plan["skipped"][0]["reason"]

        forced = plan_migration(
            rows,
            [],
            tasks,
            store.list_entities(),
            today=date(2026, 11, 19),
            include_past=True,
        )
        assert len(forced["tasks"]) == 1
    finally:
        store.close()
        tasks.close()
