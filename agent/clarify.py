"""澄清引擎：识别生活场景里的歧义，先问一句再提交。

当前 MVP 覆盖两类：
- 交费类：电费 / 水费 / 燃气费 / 物业费 / 暖气费，缺「哪个家」时追问。
- 物品到期：物品名有多个候选时追问具体是哪一件。

只做确定性判断和问题生成，真正的记录仍交给 agent loop。
"""
from config import PENDING_ACTION_TTL_SECONDS
from domain.store import AgentStore, normalize_text

BILL_TRIGGERS = ("电费", "水费", "燃气费", "煤气费", "物业费", "暖气费")
BILL_COMPLETION_WORDS = ("交了", "付了", "缴了", "完成", "搞定", "已经交", "已交")
EXPIRY_TRIGGERS = ("过期", "保质期", "到期")
CANCEL_WORDS = ("算了", "不用了", "取消", "先不记", "不记了", "没事")

# 规则注册表：新增一类歧义 = 加一条规则，不用改主流程
RULES = (
    {
        "name": "bill_property",
        "triggers": BILL_TRIGGERS,
        "missing_slot": "property",
        "entity_type": "property",
        "question": "是哪里的{trigger}？",
        "empty_hint": "比如自己家、爸妈家。",
    },
    {
        "name": "item_expiry",
        "triggers": EXPIRY_TRIGGERS,
        "missing_slot": "product",
        "candidates": "items",
        "question": "是哪一件？",
    },
)


class Clarifier:
    def __init__(self, store=None, items=None, ttl_seconds=PENDING_ACTION_TTL_SECONDS):
        self.store = store or AgentStore()
        self.items = items
        self.ttl_seconds = ttl_seconds

    def close(self):
        self.store.close()

    def check(self, text, conversation_id):
        """返回 None（无需澄清）或一个澄清结果 dict。

        dict 形态：
        - {"kind": "clarify", "reply": 问题, "pending_action": {...}}
        - {"kind": "resolved", "text": 补全后的文本}
        - {"kind": "cancelled", "reply": 回复}
        """
        pending = self.store.get_pending(conversation_id)
        if pending:
            # 待确认动作由 ConfirmationManager 处理，澄清引擎不要误接
            if pending.get("missing_slot") == "confirm":
                return None
            return self._resolve(pending, text)
        return self._detect(text, conversation_id)

    # ---------- 识别 ----------

    def _detect(self, text, conversation_id):
        normalized = normalize_text(text)
        if not normalized:
            return None

        for rule in RULES:
            trigger = next(
                (word for word in rule["triggers"] if word in normalized),
                None,
            )
            if not trigger:
                continue
            if rule.get("entity_type"):
                return self._check_entity_rule(
                    rule, trigger, text, conversation_id
                )
            if rule.get("candidates") == "items":
                return self._check_item_rule(rule, text, conversation_id)
        return None

    def _check_entity_rule(self, rule, trigger, text, conversation_id):
        entity_type = rule["entity_type"]

        # 完成态不是新建动作，交给 agent 去完成任务
        if rule["name"] == "bill_property":
            normalized = normalize_text(text)
            if any(word in normalized for word in BILL_COMPLETION_WORDS):
                return None

        # 用户已经明确说了实体，不再追问
        if self.store.find_entity(entity_type, text):
            return None

        entities = self.store.list_entities(entity_type)
        question_base = rule["question"].format(trigger=trigger)

        if not entities:
            hint = rule.get("empty_hint", "")
            question = question_base + hint
            return self._ask(
                conversation_id, text, rule["missing_slot"], question, []
            )

        if len(entities) == 1:
            # 只有一个已知候选时采用它，并明确写进上下文
            canonical = entities[0]["canonical_name"]
            return {"kind": "resolved", "text": f"{canonical}，{text}"}

        candidates = [
            {
                "id": entity["id"],
                "label": entity["canonical_name"],
                "canonical": entity["canonical_name"],
            }
            for entity in entities
        ]
        labels = [candidate["label"] for candidate in candidates]
        question = question_base + "还是".join(labels) + "？"
        return self._ask(
            conversation_id, text, rule["missing_slot"], question, candidates
        )

    def _check_item_rule(self, rule, text, conversation_id):
        matches = self._match_items(text)
        if len(matches) <= 1:
            return None

        candidates = [
            {"id": item["id"], "label": item["name"], "canonical": item["name"]}
            for item in matches
        ]
        labels = [candidate["label"] for candidate in candidates]
        question = rule["question"] + "还是".join(labels) + "？"
        return self._ask(
            conversation_id, text, rule["missing_slot"], question, candidates
        )

    def _match_items(self, text):
        """找出文本里可能指代的物品；多个最高分候选视为歧义。"""
        if self.items is None:
            return []

        normalized = normalize_text(text)
        if not normalized:
            return []

        scored = []
        for item in self.items.list_all():
            if item.get("status") == "expired":
                continue
            score = self._match_score(item.get("name", ""), normalized)
            if score:
                scored.append((score, item))

        if not scored:
            return []

        top = max(score for score, _item in scored)
        return [item for score, item in scored if score == top]

    @staticmethod
    def _match_score(name, text):
        normalized = normalize_text(name)
        max_length = min(len(normalized), 6)
        for length in range(max_length, 1, -1):
            for start in range(len(normalized) - length + 1):
                if normalized[start:start + length] in text:
                    return length
        return 0

    # ---------- 追问与合并 ----------

    def _ask(self, conversation_id, text, missing_slot, question, candidates):
        pending_id = self.store.create_pending(
            conversation_id,
            text,
            missing_slot,
            question,
            candidates,
            self.ttl_seconds,
        )
        return {
            "kind": "clarify",
            "reply": question,
            "pending_action": {
                "id": pending_id,
                "missing_slot": missing_slot,
                "candidates": candidates,
            },
        }

    def _resolve(self, pending, answer):
        normalized = normalize_text(answer)
        if any(word in normalized for word in CANCEL_WORDS):
            self.store.clear_pending(pending["id"])
            return {"kind": "cancelled", "reply": "好的，先不记了。"}

        chosen = None
        best_length = 0
        for candidate in pending["candidates"]:
            for name in (candidate.get("label", ""), candidate.get("canonical", "")):
                normalized_name = normalize_text(name)
                if not normalized_name:
                    continue
                score = self._common_substring_length(normalized, normalized_name)
                if score >= 2 and score > best_length:
                    chosen = candidate
                    best_length = score

        canonical = (chosen or {}).get("canonical") or normalize_text(answer)
        if not canonical:
            self.store.clear_pending(pending["id"])
            return {"kind": "cancelled", "reply": "没听清，先不记了。"}

        if pending["missing_slot"] == "property":
            self.store.add_entity("property", canonical, aliases=[answer])
        elif pending["missing_slot"] == "product":
            self.store.add_entity("product", canonical, aliases=[answer])

        self.store.clear_pending(pending["id"])
        return {
            "kind": "resolved",
            "text": f"{canonical}，{pending['original_text']}",
        }

    @staticmethod
    def _common_substring_length(left, right):
        """最长公共子串长度，用于容忍「蒙牛的」「伊利那盒」这类口语回答。"""
        if not left or not right:
            return 0
        best = 0
        for i in range(len(left)):
            for j in range(len(right)):
                length = 0
                while (
                    i + length < len(left)
                    and j + length < len(right)
                    and left[i + length] == right[j + length]
                ):
                    length += 1
                best = max(best, length)
        return best
