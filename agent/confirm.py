"""高风险动作确认：删除等操作先请求确认，确认后才执行。"""
from config import PENDING_ACTION_TTL_SECONDS
from domain.store import normalize_text

YES_WORDS = ("确认", "确定", "是的", "是", "好的", "好", "可以", "删除", "删掉", "执行", "嗯", "行")
NO_WORDS = ("取消", "不要", "不用", "别", "算了", "先不", "停止", "不")


class ConfirmationRequired(Exception):
    """Agent loop 遇到高风险工具时抛出，由 Assistant 转成确认问题。"""

    def __init__(self, reply, pending_action):
        super().__init__(reply)
        self.reply = reply
        self.pending_action = pending_action


class ConfirmationManager:
    def __init__(self, store, memory, items, ttl_seconds=PENDING_ACTION_TTL_SECONDS):
        self.store = store
        self.memory = memory
        self.items = items
        self.ttl_seconds = ttl_seconds

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
            deleted = self.items.delete(name)
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
                "reply": f"没有找到物品：{name}",
            }

        return {
            "kind": "done",
            "type": "chat",
            "domain": "confirm",
            "action": "error",
            "reply": "该操作暂未接入，已取消。",
        }
