"""把工具参数说成人话。

为什么单独成模块：确认句、执行回显、列表摘要说的是同一个东西。各写一遍的结果是
日期格式三处不一致——语音里念成「二零二六 杠 十一 杠 二五」，用户根本听不出对错，
而确认的意义恰恰是让他当场发现参数错了。

三条规则：
- 日期一律说「11月25日（周三）」；今天/明天/后天用相对词
- 有时间才说时间，且说「早上8点」不说「08:00」
- 确认时引用用户原话：人记得自己说过什么，比记得参数格式更可靠
"""
from datetime import date

_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# 引用原话的上限：超过这个长度就不引用了。整段话复述一遍要念十几秒
# （语音确认最怕这个），而且长句通常是多个意图混在一起，引用了反而更好听错。
_QUOTE_MAX_CHARS = 20


def _as_date(value):
    """ISO 日期/日期时间 → date；解析不了返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def weekday_cn(day):
    return _WEEKDAYS[day.weekday()]


def speak_date(value, today=None):
    """2026-11-25 → 11月25日（周三）；今天/明天/后天用相对词。"""
    day = _as_date(value)
    if day is None:
        return str(value or "").strip()
    today = today or date.today()
    delta = (day - today).days
    if delta == 0:
        return "今天"
    if delta == 1:
        return "明天"
    if delta == 2:
        return "后天"
    if delta == -1:
        return "昨天"
    return "%d月%d日（%s）" % (day.month, day.day, weekday_cn(day))


def speak_clock(hour, minute=0):
    """8,0 → 早上8点；14,30 → 下午2点30分。"""
    try:
        hour = int(hour)
        minute = int(minute or 0)
    except (TypeError, ValueError):
        return ""
    tail = "%d点" % hour
    if minute:
        tail += "%02d分" % minute
    if hour < 6:
        return "凌晨" + tail
    if hour < 12:
        return "早上" + tail
    if hour == 12:
        return "中午12点" + ("%02d分" % minute if minute else "")
    if hour < 18:
        return "下午%d点%s" % (hour - 12, ("%02d分" % minute) if minute else "")
    return "晚上%d点%s" % (hour - 12, ("%02d分" % minute) if minute else "")


def speak_datetime(value, today=None):
    """2026-11-24T08:00:00 → 明天早上8点。"""
    text = str(value or "").strip()
    day_part = speak_date(text, today)
    if "T" not in text:
        return day_part
    raw = text.split("T", 1)[1]
    head, _, rest = raw.partition(":")
    minute = rest.split(":", 1)[0] if rest else "0"
    clock = speak_clock(head, minute)
    return (day_part + clock) if clock else day_part


def summary(tool_name, args):
    """一条写入的参数摘要，不带动词。"""
    args = args or {}
    if tool_name == "item_add":
        name = str(args.get("name") or "").strip() or "这个物品"
        expiry = str(args.get("expiry_date") or "").strip()
        return "%s，%s到期" % (name, speak_date(expiry)) if expiry else "%s，没有到期日" % name
    if tool_name == "task_add":
        title = str(args.get("title") or "").strip() or "这件事"
        due = str(args.get("due_at") or "").strip()
        return "%s，%s" % (title, speak_datetime(due)) if due else "%s，没定时间" % title
    if tool_name == "fact_store":
        subject = str(args.get("subject") or "").strip()
        predicate = str(args.get("predicate") or "").strip()
        value = str(args.get("value") or "").strip()
        head = "".join(p for p in (subject, predicate) if p) or "这条事实"
        return "%s = %s" % (head, value) if value else head
    text = str(args.get("text") or "").strip()
    return text or "这条记忆"


def _quote(user_text):
    """引用用户原话；太长或太短就不引。"""
    text = str(user_text or "").strip().strip("。！？!?")
    if len(text) < 4:
        return ""
    if len(text) > _QUOTE_MAX_CHARS:
        text = text[:_QUOTE_MAX_CHARS] + "…"
    return "你说的「%s」，" % text


def confirmation_question(tool_name, args, user_text=None):
    """写入前复述一遍：原话 + 说人话的参数 + 确认方式。"""
    return "%s要记成：%s。回复「确认」或「取消」。" % (
        _quote(user_text), summary(tool_name, args),
    )


def delete_question(tool_name, args):
    """删除类确认。删除不可逆，参数必须念清楚。"""
    args = args or {}
    if tool_name == "memory_delete":
        target = str(args.get("keywords") or "").strip() or "这条"
        count = args.get("count")
        if count and count > 1:
            return "关于「%s」找到 %d 条记忆，要全部删掉吗？回复「确认」或「取消」。" % (
                target, count,
            )
        return "确认删除关于「%s」的记忆吗？回复「确认」或「取消」。" % target
    target = str(args.get("name") or "").strip() or "这个物品"
    return "确认删除物品「%s」吗？回复「确认」或「取消」。" % target


def echo_prefix(executed, reply=""):
    """执行完的极短回显，接在模型回复前面。

    模型嘴上说的日期可能是旧值（实测 4B 会），这行由参数拼出来，先把它放前面，
    用户至少先听到一遍正确的。真正的兜底是回复校验。
    """
    parts = []
    for tool_name, args in executed or []:
        if tool_name not in _WRITE_TOOLS:
            continue
        day = _day_token(tool_name, args)
        if not day:
            # 没有日期的事情，模型随手说一遍就够了，回显只会变成复读
            continue
        if day in str(reply or ""):
            continue
        parts.append(summary(tool_name, args))
    if not parts:
        return ""
    return "；".join(parts) + "。"


_WRITE_TOOLS = ("memory_store", "item_add", "task_add", "fact_store")
# 出现这些词说明没写进去（被护栏拒绝、重复、找不到目标）
_NOT_WRITTEN = (
    "无法", "没有找到", "错误", "已忽略", "未重复", "不存在", "失败",
)


def _day_token(tool_name, args):
    """这条写入口语里会出现的「哪天」，用来判断回复有没有已经说到。"""
    args = args or {}
    if tool_name == "task_add":
        return speak_datetime(args.get("due_at"))
    if tool_name == "item_add":
        expiry = str(args.get("expiry_date") or "").strip()
        return speak_date(expiry) if expiry else ""
    return ""


def looks_written(tool_name, result):
    """工具返回看起来是「真的写进去了」。

    回显宁可少一次，也不能在失败时冒充成功——那正好是这套护栏要防的事。
    """
    if tool_name not in _WRITE_TOOLS:
        return False
    text = str(result or "")
    if not text:
        return False
    if any(word in text for word in _NOT_WRITTEN):
        return False
    return text.startswith("已") or "已更新" in text
