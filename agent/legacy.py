"""旧数据迁移：把早期只写在 memories / items 里的结构化信息补进新表。

背景：entities / tasks / facts 是后加的分层。在此之前，"明天要交电费" 这类
事情只以自然语言记忆存在，新的 Agent 查不到；物品也只躺在 items 表里，
产品实体层是空的。这里做一次确定性的补录，**不删除任何旧数据**。

刻意不做的事：

- 不把 items 复制成 tasks。物品提醒仍由 items 表负责，复制会产生重复提醒；
  物品是否退休、改由 tasks 接管，属于产品决策，留给 ROADMAP 待决策。
- 不猜测自由文本语义。只有同时命中"动作线索 + 可解析日期"的记忆才迁移，
  其余原样留在 memories —— 它们本来就是语义记忆层的正常内容。

迁移可重复执行：实体按 (type, canonical_name) 去重，任务在 payload 里记录
``legacy_memory_id``，已迁移的记忆不会再次落库。
"""
import re
from datetime import date, datetime, timedelta

_CN_WEEKDAY = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}

# 顺序重要：长词在前，避免"大后天"被"后天"抢先匹配
_RELATIVE_DAYS = (
    ("大后天", 3),
    ("后天", 2),
    ("明天", 1),
    ("明日", 1),
    ("今天", 0),
    ("今日", 0),
)

_WEEK_RE = re.compile(r"(下下周|下个星期|下周|这周|本周|星期|礼拜|周)([一二三四五六日天])")
_ISO_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_YMD_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})[日号]")
_MD_RE = re.compile(r"(\d{1,2})月(\d{1,2})[日号]")

_BILL_RE = re.compile(
    r"(交|缴|付)[^，。]{0,4}(费|账单|房租)"
    r"|(水费|电费|燃气费|暖气费|物业费|宽带费|话费)"
)
_SCHEDULE_RE = re.compile(
    r"(开会|会议|面试|复诊|看病|体检|上课|出差|婚礼|聚会|饭局|接机|送机)"
)
_CUSTOM_RE = re.compile(
    r"(提醒|记得|别忘|不要忘|要买|该买|买点|取快递|拿快递|续费|报名|预约)"
)


def _safe_date(year, month, day):
    try:
        return date(int(year), int(month), int(day))
    except (TypeError, ValueError):
        return None


def parse_due(text, recorded_at):
    """从文本里解析到期日，相对日期以记录时刻为基准。

    Args:
        text: 记忆原文或摘要
        recorded_at: 记忆写入时间（datetime 或 date）

    Returns:
        date 或 None（解析不出就返回 None，绝不猜）。
    """
    if not text:
        return None

    base = recorded_at.date() if isinstance(recorded_at, datetime) else recorded_at
    if not isinstance(base, date):
        return None

    match = _ISO_RE.search(text)
    if match:
        return _safe_date(match.group(1), match.group(2), match.group(3))

    match = _YMD_RE.search(text)
    if match:
        return _safe_date(match.group(1), match.group(2), match.group(3))

    match = _MD_RE.search(text)
    if match:
        # 早期记忆不带年份：默认按记录年份理解；明显早于记录时间半年以上，
        # 才认为是"下一年的那个月日"。
        parsed = _safe_date(base.year, match.group(1), match.group(2))
        if parsed and (base - parsed).days > 180:
            parsed = _safe_date(base.year + 1, match.group(1), match.group(2))
        return parsed

    match = _WEEK_RE.search(text)
    if match:
        prefix, weekday_word = match.group(1), match.group(2)
        weekday = _CN_WEEKDAY[weekday_word]
        if prefix == "下下周":
            monday = base + timedelta(days=7 - base.weekday()) + timedelta(days=7)
            return monday + timedelta(days=weekday - 1)
        if prefix in ("下周", "下个星期"):
            monday = base + timedelta(days=7 - base.weekday())
            return monday + timedelta(days=weekday - 1)
        # "周三 / 这周三"：取不早于记录日的最近一次
        return base + timedelta(days=(weekday - 1 - base.weekday()) % 7)

    for word, offset in _RELATIVE_DAYS:
        if word in text:
            return base + timedelta(days=offset)

    return None


def classify_memory(text, summary, recorded_at):
    """判断一条记忆是否应该迁移成任务。

    Returns:
        (task, reason)：task 为 None 时 reason 说明跳过原因；
        task 形如 {"type", "title", "due_at"}。
    """
    text = str(text or "").strip()
    if not text:
        return None, "内容为空"

    due = parse_due(text, recorded_at)
    if due is None:
        return None, "没有可解析的日期"

    if _BILL_RE.search(text):
        task_type = "bill"
    elif _SCHEDULE_RE.search(text):
        task_type = "schedule"
    elif _CUSTOM_RE.search(text):
        task_type = "custom"
    else:
        return None, "不是任务类内容"

    title = str(summary or "").strip() or text
    title = title.strip("。！？!?，, ")

    return {"type": task_type, "title": title, "due_at": due.isoformat()}, ""


def _parse_timestamp(value):
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def plan_migration(memory_rows, item_rows, tasks, entities, today=None,
                   include_past=False):
    """计算迁移计划，不写库。

    Args:
        memory_rows: [(id, summary, text, timestamp)]，来自 memories_raw
        item_rows: [(id, name, expiry_date, status)]，来自 items
        tasks: Tasks 实例（用于识别已迁移的记忆）
        entities: list[dict]，当前实体快照
        today: 判断"日期已过"的基准日（便于测试）
        include_past: 是否迁移日期已过的记录。默认跳过，避免迁移后立刻
            补发几个月前的提醒；旧记录仍完整保留在 memories。

    Returns:
        {"entities": [...], "tasks": [...], "skipped": [...]}
    """
    today = today or date.today()
    existing_entities = {
        (entity["type"], entity["canonical_name"]) for entity in entities
    }

    migrated_memory_ids = set()
    for task in tasks.list_all():
        payload = task.get("payload") or {}
        memory_id = payload.get("legacy_memory_id")
        if memory_id is not None:
            migrated_memory_ids.add(memory_id)

    plan = {"entities": [], "tasks": [], "skipped": []}

    # items -> 产品实体：补齐实体层，便于后续候选/别名匹配；不动提醒归属
    for _id, name, _expiry, _status in item_rows:
        name = str(name or "").strip()
        if not name:
            continue
        if ("product", name) in existing_entities:
            continue
        existing_entities.add(("product", name))
        plan["entities"].append({"type": "product", "name": name})

    # 只有"任务类 + 有日期"的记忆才迁移，其余保留在 memories
    for memory_id, summary, text, timestamp in memory_rows:
        if memory_id in migrated_memory_ids:
            plan["skipped"].append(
                {"memory_id": memory_id, "text": text, "reason": "已迁移过"}
            )
            continue

        recorded_at = _parse_timestamp(timestamp)
        if recorded_at is None:
            plan["skipped"].append(
                {"memory_id": memory_id, "text": text, "reason": "记录时间无法解析"}
            )
            continue

        task, reason = classify_memory(text, summary, recorded_at)
        if task is None:
            plan["skipped"].append(
                {"memory_id": memory_id, "text": text, "reason": reason}
            )
            continue

        if not include_past and date.fromisoformat(task["due_at"]) < today:
            plan["skipped"].append(
                {
                    "memory_id": memory_id,
                    "text": text,
                    "reason": "日期已过（保留在 memories，可用 --include-past 强制迁移）",
                }
            )
            continue

        plan["tasks"].append(
            {
                "memory_id": memory_id,
                "type": task["type"],
                "title": task["title"],
                "due_at": task["due_at"],
                "payload": {
                    "legacy_memory_id": memory_id,
                    "legacy_source": "memory",
                    "legacy_text": text,
                },
            }
        )

    return plan


def apply_migration(plan, tasks, store):
    """执行迁移计划，返回写入条数。"""
    for entity in plan["entities"]:
        store.add_entity(entity["type"], entity["name"])

    for task in plan["tasks"]:
        tasks.add(
            task["title"],
            task["due_at"],
            task["type"],
            payload=task["payload"],
        )

    return {"entities": len(plan["entities"]), "tasks": len(plan["tasks"])}
