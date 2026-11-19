"""旧数据迁移 CLI：把 memories 里的任务类记录补进 tasks，把 items 补成产品实体。

用法：
    python scripts/migrate_legacy.py            # 默认 dry-run，只打印计划
    python scripts/migrate_legacy.py --apply    # 备份数据库后写入

设计见 agent/legacy.py；不删除旧数据，可重复执行。
"""
import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent.legacy import apply_migration, plan_migration  # noqa: E402
from config import (  # noqa: E402
    AGENT_DB_PATH,
    ITEMS_DB_PATH,
    MEMORY_DB_PATH,
    TASKS_DB_PATH,
)
from domain.store import AgentStore  # noqa: E402
from domain.tasks import Tasks  # noqa: E402


def _resolve(path):
    """配置里的相对路径按仓库根目录解析，避免受启动目录影响。"""
    return path if os.path.isabs(path) else os.path.join(ROOT, path)


def _read_rows(db_path, sql):
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _read_memories(db_path):
    return _read_rows(
        db_path,
        "SELECT id, summary, text, timestamp FROM memories_raw ORDER BY id",
    )


def _read_items(db_path):
    return _read_rows(
        db_path,
        "SELECT id, name, expiry_date, status FROM items ORDER BY id",
    )


def _backup(paths, backup_dir):
    """写入前备份现有数据库，避免迁移出错时无法回退。"""
    os.makedirs(backup_dir, exist_ok=True)
    saved = []
    for path in paths:
        if os.path.exists(path):
            target = os.path.join(backup_dir, os.path.basename(path))
            shutil.copy2(path, target)
            saved.append(target)
    return saved


def _print_plan(plan):
    print("计划：产品实体 %d 个，任务 %d 条，跳过 %d 条"
          % (len(plan["entities"]), len(plan["tasks"]), len(plan["skipped"])))

    for entity in plan["entities"]:
        print("  [实体] %s · %s" % (entity["type"], entity["name"]))
    for task in plan["tasks"]:
        print("  [任务] %s · %s · %s"
              % (task["due_at"], task["type"], task["title"]))
    for skipped in plan["skipped"]:
        print("  [跳过] #%s %s（%s）"
              % (skipped["memory_id"], skipped["text"], skipped["reason"]))


def main(argv=None):
    parser = argparse.ArgumentParser(description="迁移旧 memories / items 到新表")
    parser.add_argument("--apply", action="store_true", help="真正写入（默认 dry-run）")
    parser.add_argument(
        "--include-past",
        action="store_true",
        help="连日期已过的记录一起迁移（默认跳过，避免补发旧提醒）",
    )
    parser.add_argument("--memory-db", default=_resolve(MEMORY_DB_PATH))
    parser.add_argument("--items-db", default=_resolve(ITEMS_DB_PATH))
    parser.add_argument("--agent-db", default=_resolve(AGENT_DB_PATH))
    parser.add_argument("--tasks-db", default=_resolve(TASKS_DB_PATH))
    parser.add_argument("--backup-dir", default=None, help="备份目录，默认 data/backup-<时间>")
    args = parser.parse_args(argv)

    memories = _read_memories(args.memory_db)
    items = _read_items(args.items_db)

    store = AgentStore(args.agent_db)
    tasks = Tasks(args.tasks_db)
    try:
        plan = plan_migration(
            memories,
            items,
            tasks,
            store.list_entities(),
            include_past=args.include_past,
        )
        _print_plan(plan)

        if not plan["entities"] and not plan["tasks"]:
            print("没有需要迁移的数据。")
            return 0

        if not args.apply:
            print("\n这是 dry-run，未写入任何数据；确认无误后加 --apply 执行。")
            return 0

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_dir = args.backup_dir or os.path.join(ROOT, "data", "backup-%s" % stamp)
        saved = _backup(
            [args.memory_db, args.items_db, args.agent_db, args.tasks_db],
            backup_dir,
        )
        print("\n已备份 %d 个数据库到 %s" % (len(saved), backup_dir))

        result = apply_migration(plan, tasks, store)
        print("迁移完成：写入实体 %d 个，任务 %d 条。"
              % (result["entities"], result["tasks"]))
        return 0
    finally:
        store.close()
        tasks.close()


if __name__ == "__main__":
    sys.exit(main())
