"""记忆蒸馏（D60）：合并同主题条目，只重算 summary，不动原文。

为什么不动原文：原文是用户真实说过的话，也是 FTS 查不到摘要时的兜底；
删掉就再也找不回来。蒸馏只做索引优化——把一组同主题记忆压成一句摘要
写进最新那条，检索更容易命中，老条目照常可查、可删。

为什么用 LLM 而不是规则合并：中文没有空格，规则只能数二元组重合，
「牛奶下周三过期」和「牛奶喝完了」共享的只有「牛奶」，拼出来的摘要
必然漏掉一半信息。让模型读原话再写一句，才不会把「下周三」丢了。
"""
import logging
import re
from datetime import datetime

logger = logging.getLogger(__name__)

# 只靠这些词聚在一起的多半不是同一主题（「明天交电费」和「明天取快递」）
_STOP_BIGRAMS = frozenset("""
今天 明天 后天 昨天 前天 早上 中午 晚上 上午 下午 现在 刚才
还有 我的 我们 你的 你们 他们 她们 一个 这个 那个 什么 怎么
已经 可以 没有 就是 不是 然后 因为 所以 但是 如果 时候 一下
一点 记得 帮我 给我 我要 我想 需要 应该 可能 大概 左右 之后
""".split())

# 日期类二元组（「1月」「6日」）会把一整个月的账单连成一组，不参与聚类
_DATE_CHARS = frozenset("年月日号点分秒周")
_STRIP_RE = re.compile(r"[\s\W_]+", re.UNICODE)
_PREFIX_RE = re.compile(r"^(合并摘要|摘要)[：:]\s*")


def _meaningful(gram):
    """这个二元组里有没有真正的词（不是纯数字/日期/符号）。"""
    for ch in gram:
        if "\u4e00" <= ch <= "\u9fff":
            if ch not in _DATE_CHARS:
                return True
        elif ch.isalpha():
            return True
    return False


def _bigrams(text):
    """去掉标点后的二元组集合，停用词和日期词不参与。"""
    clean = _STRIP_RE.sub("", str(text or ""))
    grams = set()
    for index in range(len(clean) - 1):
        gram = clean[index:index + 2]
        if gram in _STOP_BIGRAMS or not _meaningful(gram):
            continue
        grams.add(gram)
    return grams


def group_similar(rows, min_group=2, max_group=5):
    """星形聚类：以最新的那条为锚点，把后面共享关键词的拉进同一组。

    为什么不做传递闭包：共享一个词就能连成链，几百条记忆会串成一组，
    LLM 最后拿到一堆互不相关的东西。锚点法每组最多 max_group 条。
    `rows` 必须按时间倒序（`Memory.list_all` 就是这样）。
    """
    grams = [_bigrams(row.get("text") or "") for row in rows]
    used = [False] * len(rows)
    groups = []
    for anchor in range(len(rows)):
        if used[anchor] or not grams[anchor]:
            continue
        members = [anchor]
        for other in range(anchor + 1, len(rows)):
            if used[other] or not (grams[anchor] & grams[other]):
                continue
            members.append(other)
            if len(members) >= max_group:
                break
        if len(members) >= min_group:
            for index in members:
                used[index] = True
            groups.append([rows[index] for index in members])
    return groups


def _signature(group):
    """一组的指纹：成员 id 排序后拼起来。成员变了才重新蒸馏。"""
    return ",".join(str(row["id"]) for row in sorted(group, key=lambda r: r["id"]))


def distillation_due(now, count, last_run=None, hour=3, threshold=500,
                     interval_hours=6):
    """该跑蒸馏了吗。

    - 深夜窗口：到了 hour 点、当天还没跑过 → 跑（每天一次）。
    - 阈值补跑：记忆达到 threshold 条，且离上次至少 interval_hours → 跑，
      不等到半夜，避免超阈值后大批记忆挤在索引里过一整天。
    """
    now = now or datetime.now()
    if last_run is None:
        # 从没跑过：到点就跑；没到点但已经超阈值也跑，别等一整天
        return now.hour >= hour or count >= threshold
    elapsed = (now - last_run).total_seconds()
    if count >= threshold and elapsed >= interval_hours * 3600:
        return True
    return now.hour >= hour and last_run.date() < now.date()


class MemoryDistiller:
    """把同主题记忆合并成一句摘要，写到该组最新的一条上。"""

    def __init__(self, memory, llm, min_group=2, max_group=5, max_groups=10,
                 scan_limit=1000, summary_max_chars=60):
        self.memory = memory
        self.llm = llm
        self.min_group = max(2, int(min_group))
        self.max_group = max(self.min_group, int(max_group))
        self.max_groups = max(1, int(max_groups))
        self.scan_limit = max(50, int(scan_limit))
        self.summary_max_chars = max(20, int(summary_max_chars))

    def run(self):
        """跑一轮蒸馏，返回 {"groups", "updated", "skipped"}。

        单组失败只记日志、不中断整轮：半夜无人值守，一次网络抖动不该让
        后面几十组都白等一天。
        """
        rows = self.memory.list_all(self.scan_limit)
        groups = group_similar(rows, self.min_group, self.max_group)
        updated = 0
        skipped = 0
        for group in groups:
            if updated >= self.max_groups:
                break
            signature = _signature(group)
            if self.memory.is_distilled(signature):
                skipped += 1
                continue
            try:
                summary = self._merge(group)
            except Exception as exc:
                logger.warning("记忆蒸馏失败（%d 条）：%s", len(group), exc)
                continue
            if not summary:
                # 模型判定不是同一主题：记下来，免得每天重复问一遍
                self.memory.record_distill(signature, group[0]["id"], "")
                skipped += 1
                continue
            if self.memory.update_summary(group[0]["id"], summary):
                self.memory.record_distill(signature, group[0]["id"], summary)
                updated += 1
        return {"groups": len(groups), "updated": updated, "skipped": skipped}

    def _merge(self, group):
        lines = "\n".join(
            "%d. %s" % (index + 1, row.get("text") or "")
            for index, row in enumerate(group)
        )
        prompt = (
            "下面几条是同一主题的家庭记忆：\n%s\n\n"
            "合并成一句不超过%d字的摘要，保留时间、地点、物品、数字等关键信息。"
            "只输出摘要本身，不要评论、不要分点。"
            "如果它们其实不是同一主题，只回复：无关"
        ) % (lines, self.summary_max_chars)
        data = self.llm.chat(
            [
                {"role": "system", "content": "你把同一主题的记忆合并成一句摘要，不加内容。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=120,
        )
        text = (data["choices"][0]["message"].get("content") or "").strip()
        text = _PREFIX_RE.sub("", text).strip("「」『』\"'“”‘’。 \n")
        if not text:
            return ""
        if "无关" in text[:10] or "不是同一主题" in text:
            return ""
        # 明显跑题（模型写了一大段）就不要，宁可这一组不蒸馏
        if len(text) > self.summary_max_chars * 2:
            return ""
        return text
