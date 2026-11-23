"""高风险动作确认：删除等操作先请求确认，确认后才执行。"""
from agent import wording
from config import PENDING_ACTION_TTL_SECONDS
from domain.store import normalize_text

YES_WORDS = ("确认", "确定", "是的", "是", "好的", "好", "可以", "删除", "删掉", "执行", "嗯", "行")
NO_WORDS = ("取消", "不要", "不用", "别", "算了", "先不", "停止", "不")

# 弱模型模式下需要先确认的写操作（都是会落库、可能触发提醒的动作）
WRITE_ACTIONS = ("memory_store", "item_add", "task_add", "fact_store")
_WRITE_DOMAINS = {
    "memory_store": "memory",
    "item_add": "items",
    "task_add": "tasks",
    "fact_store": "facts",
}
# 只确认任务时的子集：误提醒的代价最高，物品/记忆写错还能当场查出来
_TASK_ONLY = ("task_add",)

_OFF_VALUES = ("", "0", "off", "false", "no", "none", "关闭")
_ALL_VALUES = ("1", "on", "true", "yes", "all", "全部")


def parse_confirm_actions(value):
    """把 CONFIRM_WRITE_ACTIONS 的配置值解析成需要确认的工具名集合。

    支持 0/off、1/all、tasks，以及逗号分隔的显式列表（如 memory_store,item_add）。
    """
    raw = str(value or "").strip().lower()
    if raw in _OFF_VALUES:
        return frozenset()
    if raw in _ALL_VALUES:
        return frozenset(WRITE_ACTIONS)
    if raw in ("task", "tasks", "任务"):
        return frozenset(_TASK_ONLY)
    wanted = [item.strip() for item in raw.split(",") if item.strip()]
    return frozenset(name for name in wanted if name in WRITE_ACTIONS)


class ConfirmationRequired(Exception):
    """Agent loop 遇到高风险工具时抛出，由 Assistant 转成确认问题。"""

    def __init__(self, reply, pending_action):
        super().__init__(reply)
        self.reply = reply
        self.pending_action = pending_action


class ConfirmationManager:
    def __init__(self, store, memory, items, ttl_seconds=PENDING_ACTION_TTL_SECONDS,
                 tool_runner=None):
        self.store = store
        self.memory = memory
        self.items = items
        self.ttl_seconds = ttl_seconds
        # 写操作确认需要回放工具调用；由 Assistant 注入，避免 agent 层依赖 tools
        self.tool_runner = tool_runner

    def get_pending(self, conversation_id):
        pending = self.store.get_pending(conversation_id)
        if pending and pending.get("missing_slot") == "confirm":
            return pending
        return None

    def request(self, conversation_id, action_type, action_payload,
                question, original_text=""):
        pending_id = self.store.create_pending(
            conversation_id,
            original_text,
            "confirm",
            question,
            [],
            self.ttl_seconds,
            action_type=action_type,
            action_payload=action_payload,
        )
        return {
            "id": pending_id,
            "missing_slot": "confirm",
            "action_type": action_type,
            "question": question,
        }

    def handle(self, pending, text):
        normalized = normalize_text(text)

        if any(word in normalized for word in NO_WORDS):
            self.store.record_proposal(
                pending.get("action_type") or "confirm",
                pending.get("payload") or {},
                "cancelled",
                "用户取消",
            )
            self.store.clear_pending(pending["id"])
            return {"kind": "cancelled", "reply": "好的，已取消。"}

        if any(word in normalized for word in YES_WORDS):
            try:
                result = self._execute(pending)
            except Exception as exc:
                result = {
                    "kind": "done",
                    "type": "chat",
                    "domain": "confirm",
                    "action": "error",
                    "reply": f"操作失败：{exc}",
                }
            self.store.clear_pending(pending["id"])
            return result

        return {"kind": "reask", "reply": pending["question"]}

    def _execute(self, pending):
        action_type = pending.get("action_type")
        payload = pending.get("payload") or {}

        if action_type in WRITE_ACTIONS and self.tool_runner is not None:
            try:
                result = str(self.tool_runner(action_type, payload))
            except Exception as exc:  # 写库失败也要给出可读回复，不向 API 抛错
                return {
                    "kind": "done",
                    "type": "chat",
                    "domain": _WRITE_DOMAINS.get(action_type, "confirm"),
                    "action": "error",
                    "reply": f"操作失败：{exc}",
                }
            self.store.record_proposal(action_type, payload, "committed", result)
            return {
                "kind": "done",
                "type": "record",
                "domain": _WRITE_DOMAINS.get(action_type, "confirm"),
                "action": "create",
                "reply": _written_reply(action_type, payload, result),
            }

        if action_type == "memory_delete":
            keywords = str(payload.get("keywords") or "")
            deleted = self.memory.delete_by_keywords(keywords)
            self.store.record_proposal(
                "memory_delete",
                {"keywords": keywords, "deleted": len(deleted)},
                "committed",
                f"删除 {len(deleted)} 条",
            )
            if deleted:
                return {
                    "kind": "done",
                    "type": "delete",
                    "domain": "memory",
                    "action": "delete",
                    "reply": f"已删除 {len(deleted)} 条关于{keywords}的记忆。",
                    "deleted": deleted,
                }
            return {
                "kind": "done",
                "type": "chat",
                "domain": "memory",
                "action": "delete",
                "reply": "没有找到要删除的记忆。",
            }

        if action_type == "item_delete":
            name = str(payload.get("name") or "")
            resolved = self.items.resolve_name(name) or name
            deleted = self.items.delete_exact(resolved)
            self.store.record_proposal(
                "item_delete",
                {"name": name, "deleted": len(deleted)},
                "committed",
                f"删除 {len(deleted)} 个物品",
            )
            if deleted:
                names = "、".join(item["name"] for item in deleted)
                return {
                    "kind": "done",
                    "type": "chat",
                    "domain": "items",
                    "action": "delete",
                    "reply": f"已删除 {len(deleted)} 个物品：{names}。",
                }
            return {
                "kind": "done",
                "type": "chat",
                "domain": "items",
                "action": "delete",
                "reply": f"没有叫「{name}」的物品，什么都没删。",
            }

        return {
            "kind": "done",
            "type": "chat",
            "domain": "confirm",
            "action": "error",
            "reply": "该操作暂未接入，已取消。",
        }


def _written_reply(action_type, payload, result):
    """确认之后怎么回话。

    工具返回的是给日志和审计看的（里面是 ISO 日期），回话要用参数拼成人话，
    否则 TTS 会把「2026-12-02」念成「二零二六 杠 十二 杠 零二」。
    事实被覆盖时「原值已失效」只在返回里，模板要补上。
    """
    if wording.looks_written(action_type, result):
        reply = "已记好：%s。" % wording.summary(action_type, payload)
        note = wording.replaced_note(result)
        if note:
            reply += note
        return reply
    return str(result)