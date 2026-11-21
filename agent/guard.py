"""写工具参数护栏：模型的参数必须能在用户原话里找到依据。

小模型的参数纪律不可靠，提示词也拦不住（实测在系统提示里写「查询不得顺带写入」，
重跑三次照旧）。所以把校验放到工具执行前，属于编排层的确定性兜底：

- 日期：句子里没有日期线索却给了到期日 → 丢掉。4B 模型实测「冰箱里还有三个鸡蛋」
  有约一半概率凭空补一个到期日（补过今天、12-21、12-31），落库后会变成误提醒。
- 标题：任务标题在用户原话里找不到 → 判定为凭空创建，整次调用不执行。

有日期线索但解析不出来时保留模型给的值：那种情况多半是我们还没覆盖的说法，
贸然丢掉会把正常需求也拦掉。
"""
import re

from agent.dates import has_date_hint, resolve_due

# 中英文混排时，中文按二元组、英文数字按整词比较
_TOKEN_SPLIT_RE = re.compile(r"[^\u4e00-\u9fa50-9a-zA-Z]+")


def _tokens(text):
    grams = set()
    for chunk in _TOKEN_SPLIT_RE.split(str(text or "")):
        if not chunk:
            continue
        if chunk.isascii():
            grams.add(chunk.lower())
            continue
        if len(chunk) == 1:
            grams.add(chunk)
            continue
        for index in range(len(chunk) - 1):
            grams.add(chunk[index:index + 2])
    return grams


def is_grounded(value, user_text):
    """value 是否在用户原话里有依据（至少一个二元组/整词重合）。"""
    value = str(value or "").strip()
    if not value:
        return True
    return bool(_tokens(value) & _tokens(user_text))


def ground_due(model_value, user_text, today=None):
    """任务的到期时间：能从句子里算出来就以句子里为准。"""
    resolved = resolve_due(user_text, today, allow_time_only=True)
    if resolved:
        return resolved
    if has_date_hint(user_text):
        return model_value or None
    return None


def ground_expiry(model_value, user_text, today=None):
    """物品到期日：句子里没有日期就一定留空。"""
    resolved = resolve_due(user_text, today, allow_time_only=False)
    if resolved:
        # 物品只要日期部分
        return resolved.split("T")[0]
    if has_date_hint(user_text):
        return model_value or None
    return None


def guard_write_args(name, args, user_text, today=None):
    """在工具执行前清洗参数。

    Returns:
        (cleaned_args, blocked_message)：blocked_message 不为 None 时调用方不应执行工具，
        直接把它当作工具结果回给模型，让模型自己纠正。
    """
    cleaned = dict(args or {})
    text = str(user_text or "")

    if name == "task_add":
        title = str(cleaned.get("title") or "").strip()
        if title and not is_grounded(title, text):
            return cleaned, (
                "已忽略：用户这句话里没有提到「%s」，不要凭空创建任务。" % title
            )
        cleaned["due_at"] = ground_due(cleaned.get("due_at"), text, today)
        return cleaned, None

    if name == "item_add":
        cleaned["expiry_date"] = ground_expiry(cleaned.get("expiry_date"), text, today)
        return cleaned, None

    return cleaned, None


def correction_note(name, before, after):
    """告诉模型参数被改成了什么，免得它照着旧值口播。

    护栏只保证落库数据正确；4B 模型常常用自己编的参数说话（实测库里存了 11-25，
    嘴上仍说「11月26日」）。把校正结果放进工具返回里给它一次机会改正。
    """
    before = before or {}
    after = after or {}
    if name == "item_add":
        old = str(before.get("expiry_date") or "").strip()
        new = str(after.get("expiry_date") or "").strip()
        if old == new:
            return ""
        if new:
            return "（到期日已按用户原话校正为 %s，回答时以此为准）" % new
        return "（用户没有提到到期日，已留空；回答时不要编造日期）"
    if name == "task_add":
        old = str(before.get("due_at") or "").strip()
        new = str(after.get("due_at") or "").strip()
        if old == new:
            return ""
        if new:
            return "（提醒时间已按用户原话校正为 %s，回答时以此为准）" % new
        return "（用户没有提到时间，已留空；回答时不要编造时间）"
    return ""
