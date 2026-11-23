"""写工具参数护栏：模型的参数必须能在用户原话里找到依据。

小模型的参数纪律不可靠，提示词也拦不住（实测在系统提示里写「查询不得顺带写入」，
重跑三次照旧）。所以把校验放到工具执行前，属于编排层的确定性兜底：

- 日期：句子里没有日期线索却给了到期日 → 丢掉。4B 模型实测「冰箱里还有三个鸡蛋」
  有约一半概率凭空补一个到期日（补过今天、12-21、12-31），落库后会变成误提醒。
- 标题：任务标题在用户原话里找不到 → 判定为凭空创建，整次调用不执行。
- 摘要：记忆摘要里出现了原话没有的字（尤其是日期）→ 丢掉摘要，改用原话。
  摘要进 FTS 索引，编造的日期会变成「查不到」或「查到错的」。

有日期线索但解析不出来时保留模型给的值：那种情况多半是我们还没覆盖的说法，
贸然丢掉会把正常需求也拦掉。
"""
import re
from datetime import date, datetime

from agent.dates import has_date_hint, parse_date_text, parse_recurrence, resolve_due

# 会落库的写工具；用来判断「用户要写，但没有真的写」
WRITE_TOOLS = (
    "memory_store", "item_add", "task_add", "fact_store",
    "task_update", "task_cancel", "task_complete", "task_postpone",
)
# 会动物理设备的工具：没调却回「已关灯」同样是在骗人，但说法不能套「清单」
DEVICE_TOOLS = ("home_control",)

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


# 摘要比对前去掉标点：模型常补个句号或逗号，那不算「加了内容」
_PUNCT_RE = re.compile(r"[\s，。！？、,.!?：:；;“”\"'（）()【】\[\]·…—～~]+")


def _strip_punct(text):
    return _PUNCT_RE.sub("", str(text or ""))


def _is_subsequence(shorter, longer):
    """shorter 的每个字都能在 longer 里按顺序找到（允许跳过字）。"""
    remaining = iter(longer)
    return all(ch in remaining for ch in shorter)


def summary_grounded(summary, user_text):
    """摘要是否只用了用户原话里已有的字。

    为什么用子序列而不是 `is_grounded`：后者只要有一个二元组重合就算通过，
    「明天买电池」和「8月9日买电池」共享「买电」「电池」，编造的日期照样过关。
    摘要的本职是「把原话剪短」，所以判据是「能不能从原话里按顺序删出来」。

    只删不加也顺带拦住了模型把「三个鸡蛋」写成「3个鸡蛋」这类改写——数字对不上
    就退回原话，检索靠 FTS 的原文兜底，不会丢。
    """
    summary = _strip_punct(summary)
    if not summary:
        return True
    return _is_subsequence(summary, _strip_punct(user_text))


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


def ground_recur(model_value, user_text, today=None, now=None):
    """周期：只认原话里的「每天/每周X/每月X号」，模型自己说有也不算。

    没有具体时间的周期提醒（「每天提醒我吃药」）不落库——几点吃药是用户知道的，
    替人编一个 8 点比多问一句更糟。

    Returns:
        (recur, blocked)：blocked 非空表示这次调用不执行。
    """
    parsed = parse_recurrence(user_text, today, now)
    if not parsed:
        return "", None
    if not parsed.get("time"):
        return "", (
            "已忽略：周期提醒要说明具体时间（比如「每天早上八点」）。"
            "先问用户几点提醒，不要自己定时间。"
        )
    return parsed["recur"], None


def guard_write_args(name, args, user_text, today=None):
    """在工具执行前清洗参数。

    Returns:
        (cleaned_args, blocked_message)：blocked_message 不为 None 时调用方不应执行工具，
        直接把它当作工具结果回给模型，让模型自己纠正。
    """
    cleaned = dict(args or {})
    text = str(user_text or "")

    if name == "memory_store":
        summary = str(cleaned.get("summary") or "").strip()
        if summary and not summary_grounded(summary, text):
            # 记忆本身是用户的真实陈述，不能因为摘要编了东西就不记；
            # 但摘要会进检索索引，错的摘要比长摘要更糟，所以退回原话。
            cleaned["summary"] = text.strip()
        return cleaned, None

    if name == "task_add":
        title = str(cleaned.get("title") or "").strip()
        if title and not is_grounded(title, text):
            return cleaned, (
                "已忽略：用户这句话里没有提到「%s」，不要凭空创建任务。" % title
            )
        recur, blocked = ground_recur(cleaned.get("recur"), text, today)
        if blocked:
            return cleaned, blocked
        cleaned["recur"] = recur
        cleaned["due_at"] = ground_due(cleaned.get("due_at"), text, today)
        return cleaned, None

    if name == "item_add":
        cleaned["expiry_date"] = ground_expiry(cleaned.get("expiry_date"), text, today)
        return cleaned, None

    if name == "task_update":
        title = cleaned.get("title")
        if title is not None:
            title = str(title).strip()
            if title and not is_grounded(title, text):
                return cleaned, (
                    "已忽略：用户这句话里没有提到「%s」，不要凭空改名。" % title
                )
        # 只改时间时才校验日期；没传 due_at 表示不改
        if cleaned.get("due_at") is not None:
            cleaned["due_at"] = ground_due(cleaned.get("due_at"), text, today)
        return cleaned, None

    if name == "task_postpone":
        # 有具体说法就按绝对日期改（「明天」「下周一」是目标日，不是「在到期日上加几天」）
        today_date = today or date.today()
        if isinstance(today_date, datetime):
            today_date = today_date.date()
        parsed = parse_date_text(text, today_date)
        if parsed:
            due = parsed["date"]
            if parsed.get("time"):
                due = "%sT%s" % (due, parsed["time"])
            cleaned["due_at"] = due
            cleaned.pop("days", None)
        elif cleaned.get("due_at"):
            cleaned.pop("due_at", None)
        elif cleaned.get("days") is None:
            cleaned["days"] = 1
        return cleaned, None

    return cleaned, None


def correction_note(name, before, after):
    """告诉模型参数被改成了什么，免得它照着旧值口播。

    护栏只保证落库数据正确；4B 模型常常用自己编的参数说话（实测库里存了 11-25，
    嘴上仍说「11月26日」）。把校正结果放进工具返回里给它一次机会改正。
    """
    before = before or {}
    after = after or {}
    if name == "memory_store":
        old = str(before.get("summary") or "").strip()
        new = str(after.get("summary") or "").strip()
        if old == new:
            return ""
        return "（摘要里出现了用户原话没有的内容，已改用原话记录；回答时不要提这些内容）"
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


# 疑问句：模型在回答而不是在记录，不参与「假确认」判定
_QUESTION_RE = re.compile(
    r"吗|呢|吧|多少|哪些|几号|几天|几时|什么时候|有没有|是不是|是什么|在哪|怎么|啥时候"
)
# 模型「已经做完」的口头表述
_CLAIM_RE = re.compile(
    r"已记|记好了|记下了|记下来|已经记|已添加|已保存|已设置|已经设置"
    r"|已加|已存|记好啦|搞定|已改|改好了|已更新|已取消|已经取消|已延期|延后了"
)
# 设备类「已经做完」的说法（措辞不同，替换语也不能说「清单」）
_DEVICE_CLAIM_RE = re.compile(
    r"已经.{0,10}(关|开|调|启动|停止)|已(关|开|调|设|启动|停止)"
)


def guard_false_claim(user_text, used_tools, reply):
    """小模型没调写工具却宣称「已经记好了」。

    实测 Spark-X2.6：同一句「冰箱里还有三个鸡蛋」约有三分之一概率直接回
    「已经记在清单里了」，实际一条都没写。用户看不出区别，回头查才发现丢了。
    这里只做保守判断：不是疑问句、本轮没有任何写工具执行、回复又声称已完成。

    Returns:
        (reply, replaced)：replaced 为 True 表示原回复不可信，已换成诚实的说法。
    """
    text = str(user_text or "").strip()
    reply = str(reply or "")
    if len(text) < 4 or not reply:
        return reply, False
    if _QUESTION_RE.search(text):
        return reply, False
    if any(name in WRITE_TOOLS for name in (used_tools or [])):
        return reply, False
    if any(name in DEVICE_TOOLS for name in (used_tools or [])):
        # 指令真发下去了，读回结果已经在工具返回里，不用改
        return reply, False
    if _DEVICE_CLAIM_RE.search(reply) and not _CLAIM_RE.search(reply):
        return "抱歉，我刚才没真的下发指令，设备没动。能再说一遍吗？", True
    if not _CLAIM_RE.search(reply):
        return reply, False
    return "抱歉，我这次没写进清单，刚才那句不算。能再说一遍吗？", True
