"""写操作提案与提交：统一入口、幂等、审计。

工具层不再直接调 memory.store / items.add，而是交给 ProposalService。
低风险创建动作在提案校验通过后立即提交；删除/付款等高风险动作后续再加确认。
"""
import hashlib
import json
from datetime import datetime, timedelta

from config import PROPOSAL_DEDUPE_SECONDS


class ProposalService:
    def __init__(self, store, memory, items, tasks=None, facts=None):
        self.store = store
        self.memory = memory
        self.items = items
        self.tasks = tasks
        self.facts = facts

    def store_memory(self, text, summary=None):
        text = str(text or "").strip()
        summary = str(summary or "").strip() or None
        if not text:
            return "无法记录：记忆原文不能为空"

        payload = {"text": text, "summary": summary}
        return self._commit(
            "memory_store",
            payload,
            lambda: self.memory.store(text, summary),
            "已存储",
        )

    def add_item(self, name, expiry_date=None):
        name = str(name or "").strip()
        if not name:
            return "无法添加物品：物品名称不能为空"

        # 同名物品合并：不然一周里说两次「牛奶下周三过期」就会变成两条、提醒两次
        existing = None
        find_active = getattr(self.items, "find_active_by_name", None)
        if find_active is not None:
            existing = find_active(name)
        if existing is not None:
            return self._merge_item(existing, expiry_date)

        payload = {"name": name, "expiry_date": expiry_date or None}
        key = self._key("item_add", payload)
        duplicate = self._recent_duplicate(key)
        if duplicate and duplicate["status"] in ("committed", "duplicate"):
            self._record("item_add", payload, "duplicate", "重复提案，未写入", key)
            return "已存在相同物品，未重复添加"

        try:
            item = self.items.add(name, expiry_date)
        except ValueError as exc:
            self._record("item_add", payload, "rejected", str(exc), key)
            return f"无法添加物品：{exc}"

        payload["expiry_date"] = item["expiry_date"]
        message = (
            f"已添加物品：{item['name']}，"
            f"到期日 {item['expiry_date'] or '未知'}"
        )
        self._record("item_add", payload, "committed", message, key)
        return message

    def _merge_item(self, existing, expiry_date):
        """同名物品已存在：只在有新日期时更新，避免清单里出现重复条目。"""
        name = existing.get("name", "")
        old_expiry = existing.get("expiry_date")
        if expiry_date in (None, ""):
            return "已存在物品：%s，到期日 %s，未重复添加" % (name, old_expiry or "未知")

        try:
            item = self.items.update_expiry(existing["id"], expiry_date)
        except ValueError as exc:
            self._record(
                "item_add",
                {"name": name, "expiry_date": expiry_date},
                "rejected",
                str(exc),
            )
            return f"无法更新物品：{exc}"
        if item is None:
            return f"没有找到物品：{name}"

        if old_expiry == item["expiry_date"]:
            return "已存在物品：%s，到期日 %s，未重复添加" % (name, item["expiry_date"])
        message = "已更新物品：%s，到期日 %s → %s" % (
            name, old_expiry or "未知", item["expiry_date"] or "未知",
        )
        self._record(
            "item_add",
            {"name": name, "expiry_date": item["expiry_date"]},
            "merged",
            message,
        )
        return message

    def add_task(self, title, due_at=None, type="custom"):
        if self.tasks is None:
            return "任务模块未启用"
        title = str(title or "").strip()
        if not title:
            return "无法创建任务：任务标题不能为空"

        payload = {"title": title, "due_at": due_at or None, "type": type}
        key = self._key("task_add", payload)
        duplicate = self._recent_duplicate(key)
        if duplicate and duplicate["status"] in ("committed", "duplicate"):
            self._record("task_add", payload, "duplicate", "重复提案，未写入", key)
            return "已存在相同任务，未重复创建"

        try:
            task = self.tasks.add(title, due_at, type)
        except ValueError as exc:
            self._record("task_add", payload, "rejected", str(exc), key)
            return f"无法创建任务：{exc}"

        message = (
            f"已创建任务 #{task['id']}：{task['title']}，"
            f"时间 {task['due_at'] or '未定'}"
        )
        self._record("task_add", payload, "committed", message, key)
        return message

    def store_fact(self, subject, predicate, value, source=None):
        if self.facts is None:
            return "事实模块未启用"
        payload = {
            "subject": str(subject or "").strip(),
            "predicate": str(predicate or "").strip(),
            "value": str(value or "").strip(),
            "source": source,
        }

        key = self._key("fact_store", payload)
        duplicate = self._recent_duplicate(key)
        if duplicate and duplicate["status"] in ("committed", "duplicate"):
            self._record("fact_store", payload, "duplicate", "重复提案，未写入", key)
            return "已存在相同事实，未重复保存"

        try:
            fact = self.facts.store(
                payload["subject"],
                payload["predicate"],
                payload["value"],
                source,
            )
        except ValueError as exc:
            self._record("fact_store", payload, "rejected", str(exc), key)
            return f"无法保存事实：{exc}"

        message = (
            f"已保存：{fact['subject']} · {fact['predicate']} = {fact['value']}"
        )
        replaced = fact.get("replaced") or []
        if replaced:
            message += "（原值 %s 已失效）" % "、".join(replaced)
        self._record("fact_store", payload, "committed", message, key)
        return message

    def _commit(self, action_type, payload, write_fn, success_message):
        key = self._key(action_type, payload)
        duplicate = self._recent_duplicate(key)
        if duplicate and duplicate["status"] in ("committed", "duplicate"):
            self._record(action_type, payload, "duplicate", "重复提案，未写入", key)
            return "已存在相同记忆，未重复写入"

        try:
            write_fn()
        except Exception as exc:
            self._record(action_type, payload, "rejected", str(exc), key)
            return f"写入失败：{exc}"

        self._record(action_type, payload, "committed", success_message, key)
        return success_message

    def _recent_duplicate(self, key):
        since = (
            datetime.now() - timedelta(seconds=PROPOSAL_DEDUPE_SECONDS)
        ).isoformat()
        return self.store.find_recent_proposal(key, since)

    def _record(self, action_type, payload, status, result, key=None):
        self.store.record_proposal(
            action_type, payload, status, result, idempotency_key=key
        )

    @staticmethod
    def _key(action_type, payload):
        raw = action_type + ":" + json.dumps(
            payload, ensure_ascii=False, sort_keys=True
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
