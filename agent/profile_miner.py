"""夜间画像提炼（D84）：从昨天的对话里找「稳定偏好」，只写候选、等用户点头。

为什么不放在对话里让模型顺手记：语音链路里它正忙着答问题、还要压延迟，
`profile_set` 几乎不会被主动调用——画像层就这么一直是空的。而且当场判断容易
把一次性的要求（「这次帮我记成明天」）当成偏好；回头看一眼整天的话，
「昨天也说过」这种判据才成立。

为什么产物只是「候选」：D59 定过，模型推断的偏好只提议、不直接写。这里沿用：
候选进 `proposals`（status=proposed），网页上点「采纳」才落 `用户/...`。

防编造：模型给的 evidence 必须是某条用户原话里的子串，对不上就整条丢掉——
和记忆摘要 `summary_grounded` 是同一个思路（宁可少记，不许编）。
"""
import json
import logging
import re
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

MAX_CANDIDATES = 3
MAX_PREDICATE_CHARS = 12
MAX_VALUE_CHARS = 60
MAX_EVIDENCE_CHARS = 40
MIN_USER_TURNS = 3
MAX_TRANSCRIPT_CHARS = 6000

_STRIP_RE = re.compile(r"[\s\W_]+", re.UNICODE)


def _flat(text):
    """去掉标点和空白，做「证据是不是原话」的比对。"""
    return _STRIP_RE.sub("", str(text or ""))


def _clip(text, limit):
    text = str(text or "").strip()
    return text[:limit]


class ProfileMiner:
    def __init__(self, dialogue, proposals, llm, subject="用户",
                 max_candidates=MAX_CANDIDATES):
        self.dialogue = dialogue
        self.proposals = proposals
        self.llm = llm
        self.subject = subject
        self.max_candidates = max_candidates

    def run(self, now=None, hours=24):
        """读最近一段对话，产出候选画像。

        Args:
            now: 注入当前时间（测试用）
            hours: 回看多久（默认 24 小时）

        Returns:
            dict: {"turns": 用户说了几句, "candidates": 记下几条候选,
                   "skipped": 重复/没证据丢掉几条}
        """
        now = now or datetime.now()
        since = (now - timedelta(hours=hours)).isoformat()
        turns = self.dialogue.since(since, limit=400)
        users = [t for t in turns if t.get("role") == "user"]
        if len(users) < MIN_USER_TURNS:
            # 一天没几句话就别花一次模型调用，也没法判断「重复」
            return {"turns": len(users), "candidates": 0, "skipped": 0}

        try:
            candidates = self._extract(turns)
        except Exception as exc:
            logger.warning("画像提炼失败：%s", exc)
            return {"turns": len(users), "candidates": 0, "skipped": 0}

        texts = [_flat(turn.get("text")) for turn in users]
        recorded = 0
        skipped = 0
        for item in candidates:
            payload = self._clean(item, texts)
            if payload is None:
                skipped += 1
                continue
            if self._already_known(payload):
                skipped += 1
                continue
            key = "%s|%s|%s" % (
                payload["subject"], payload["predicate"], payload["value"],
            )
            if self._already_proposed(key):
                skipped += 1
                continue
            self.proposals.record_profile_proposal(payload, note="来自对话提炼")
            recorded += 1
            if recorded >= self.max_candidates:
                break
        return {"turns": len(users), "candidates": recorded, "skipped": skipped}

    def _extract(self, turns):
        """一次模型调用：把对话变成候选 JSON。"""
        lines = []
        budget = MAX_TRANSCRIPT_CHARS
        for turn in turns:
            who = "用户" if turn.get("role") == "user" else "小马"
            line = "%s：%s" % (who, _clip(turn.get("text"), 120))
            if budget - len(line) <= 0:
                break
            budget -= len(line)
            lines.append(line)
        prompt = (
            "下面是这个家庭最近一天的对话（用户 = 家里的人，小马 = 助手）。\n\n"
            "%s\n\n"
            "找出「关于用户本人」的稳定偏好或习惯，供以后聊天时参考。规则：\n"
            "1. 用户明确说过的偏好可以直接算，例如「我不吃辣」「我怕冷」「叫我老王」。\n"
            "2. 从行为里推断的习惯必须出现两次以上，例如连着两天问天气、每天都问吃药。\n"
            "3. 不要提取事实信息（户号、地址、电话、生日、谁是谁）、不要提取一次性请求"
            "（「这次帮我记成明天」「把灯关了」）、不要提取对助手的要求。\n"
            "4. 每条都要给 evidence：用户原话里的一段，照抄，不超过%d字。\n"
            "5. 最多%d条；没有就输出 []。\n\n"
            "只输出 JSON 数组，不要解释。格式：\n"
            '[{"predicate": "怕冷", "value": "冬天要把室温调到 24 度以上", '
            '"evidence": "今天好冷啊，帮我调到 24 度"}]\n'
            "predicate 是短属性名（不超过%d字），value 是一句结论（不超过%d字）。"
        ) % (
            "\n".join(lines), MAX_EVIDENCE_CHARS, self.max_candidates,
            MAX_PREDICATE_CHARS, MAX_VALUE_CHARS,
        )
        data = self.llm.chat(
            [
                {"role": "system", "content": "你从家庭对话里提炼用户偏好，只输出 JSON。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=400,
        )
        content = data["choices"][0]["message"].get("content") or ""
        return _parse_json_list(content)

    def _clean(self, item, texts):
        """校验一条候选：字段长度、证据必须能在原话里找到。"""
        if not isinstance(item, dict):
            return None
        predicate = _clip(item.get("predicate"), MAX_PREDICATE_CHARS)
        value = _clip(item.get("value"), MAX_VALUE_CHARS)
        evidence = _clip(item.get("evidence"), MAX_EVIDENCE_CHARS)
        if not predicate or not value:
            return None
        # 编造的证据一律丢掉：模型很容易把「经常」写成一句用户没说过的话
        flat_evidence = _flat(evidence)
        if flat_evidence and not any(flat_evidence in text for text in texts):
            return None
        return {
            "subject": self.subject,
            "predicate": predicate,
            "value": value,
            "evidence": evidence,
        }

    def _already_known(self, payload):
        """已经写成画像的就不再提（facts 里同主体同属性同值）。"""
        facts = getattr(self.proposals, "facts", None)
        if facts is None:
            return False
        try:
            rows = facts.search(
                subject=payload["subject"], predicate=payload["predicate"],
            )
        except Exception:
            return False
        return any(row.get("value") == payload["value"] for row in rows)

    def _already_proposed(self, key):
        """等确认的、已采纳的都不再重复提；被忽略过的也认账，不纠缠。"""
        try:
            rows = self.proposals.store.list_proposals(
                200, action_type="profile_set",
            )
        except Exception:
            return False
        for row in rows:
            if row.get("status") not in ("proposed", "committed", "cancelled"):
                continue
            payload = row.get("payload") or {}
            other = "%s|%s|%s" % (
                payload.get("subject"), payload.get("predicate"),
                payload.get("value"),
            )
            if other == key:
                return True
        return False


def _parse_json_list(content):
    """容错解析：模型偶尔会带 ```json 围栏或前置说明。"""
    text = str(content or "").strip()
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return data[:MAX_CANDIDATES * 2]
