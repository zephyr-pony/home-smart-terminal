"""把工具参数说成人话。

为什么单独成模块：确认句、执行回显、列表摘要说的是同一个东西。各写一遍的结果是
日期格式三处不一致——语音里念成「二零二六 杠 十一 杠 二五」，用户根本听不出对错，
而确认的意义恰恰是让他当场发现参数错了。

三条规则：
- 日期一律说「11月25日（周三）」；今天/明天/后天用相对词
- 有时间才说时间，且说「早上8点」不说「08:00」
- 确认时引用用户原话：人记得自己说过什么，比记得参数格式更可靠
"""
from datetime import date, datetime
import re

_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# 回复里出现没有出处的长数字（户号、电话）时的替换句。宁可这一轮不给答案，
# 也不能把一个编出来的号码念给用户——他照着念、照着记，错的就是错的。
UNVERIFIED_NUMBER_REPLY = "这个号码我这边没核对上，先不念了——你再确认一下要查的是哪一项？"

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


def recur_label(recur, due_at=None):
    """每天 / 每周三 / 每月5号。一次性任务返回空串。

    周期任务不说「明天」——说明天反而误导，用户关心的是「每天都在这个点」。
    """
    recur = str(recur or "").strip()
    if recur == "daily":
        return "每天"
    if recur not in ("weekly", "monthly"):
        return ""
    try:
        moment = datetime.fromisoformat(str(due_at))
    except (TypeError, ValueError):
        return "每周" if recur == "weekly" else "每月"
    if recur == "weekly":
        return "每周%s" % "一二三四五六日"[moment.weekday()]
    return "每月%d号" % moment.day


def speak_clock_from_iso(value):
    """从 YYYY-MM-DDTHH:MM 里取时刻并说人话；没有时间返回空串。"""
    text = str(value or "")
    if "T" not in text:
        return ""
    raw = text.split("T", 1)[1]
    head, _, rest = raw.partition(":")
    minute = rest.split(":", 1)[0] if rest else "0"
    return speak_clock(head, minute)


def summary(tool_name, args, today=None):
    """一条写入的参数摘要，不带动词。

    `today` 只是为测试固定「今天」；不传就用真实日期。相对说法（明天/后天）
    必须和调用方认定的是同一天，否则跨过午夜时同一句话在两处会算出不同日期。
    """
    args = args or {}
    if tool_name == "item_add":
        name = str(args.get("name") or "").strip() or "这个物品"
        expiry = str(args.get("expiry_date") or "").strip()
        return "%s，%s到期" % (name, speak_date(expiry, today)) if expiry else "%s，没有到期日" % name
    if tool_name == "task_add":
        title = str(args.get("title") or "").strip() or "这件事"
        due = str(args.get("due_at") or "").strip()
        label = recur_label(args.get("recur"), due)
        if label:
            return "%s，%s%s" % (title, label, speak_clock_from_iso(due))
        return "%s，%s" % (title, speak_datetime(due, today)) if due else "%s，没定时间" % title
    if tool_name in ("task_update", "task_postpone"):
        title = str(args.get("title") or "").strip() or "这个提醒"
        due = str(args.get("due_at") or "").strip()
        return "%s，改到%s" % (title, speak_datetime(due, today)) if due else "%s，已改期" % title
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


def _profile_who(subject):
    """「用户/妈妈」→「妈妈」，「用户」→「你」。"""
    text = str(subject or "").strip()
    if text in ("", "用户"):
        return "你"
    for prefix in ("用户/", "用户"):
        if text.startswith(prefix):
            name = text[len(prefix):].strip("/ ")
            if name:
                return name
    return text


def profile_question(args, user_text=None):
    """画像确认：念的是「我猜的」，不是「你说的」。

    措辞必须让用户能立刻否认；套用写入确认的「要记成」会听起来像既定事实，
    用户更容易顺着「嗯」下去，确认就白做了（D59）。
    """
    args = args or {}
    who = _profile_who(args.get("subject"))
    aspect = str(args.get("predicate") or "偏好").strip()
    value = str(args.get("value") or "").strip()
    return "我猜%s的%s是「%s」，要记下来吗？回复「确认」或「取消」。" % (
        who, aspect, value,
    )


def profile_reply(args):
    """确认之后按参数回显，不让模型复述——画像内容必须和落库的一字不差。"""
    args = args or {}
    who = _profile_who(args.get("subject"))
    aspect = str(args.get("predicate") or "偏好").strip()
    value = str(args.get("value") or "").strip()
    return "好，我记住了：%s的%s是「%s」。" % (who, aspect, value)


def print_question(args, user_text=None):
    """打印确认：念清文件、份数、页码和打印机，纸和墨是花掉就回不来的。"""
    args = args or {}
    name = str(args.get("name") or "这份文件").strip()
    copies = args.get("copies") or 1
    pages = str(args.get("pages") or "").strip()
    page_text = ("第 %s 页" % pages) if pages else "全部页"
    printer = str(args.get("printer") or "").strip() or "默认打印机"
    return "要打印《%s》%d 份，%s，用 %s 吗？回复「确认」或「取消」。" % (
        name, int(copies), page_text, printer,
    )


def print_reply(args, result):
    """打印回执：内容来自参数和 CUPS 的返回，不让模型自己说。

    纯文本由 CUPS 排版，提交时还不知道页数；这时只报份数，不编「共 N 页」。
    """
    args = args or {}
    result = result or {}
    name = str(args.get("name") or result.get("name") or "文件").strip()
    copies = result.get("copies") or args.get("copies") or 1
    pages = result.get("pages")
    printer = str(result.get("printer") or args.get("printer") or "打印队列").strip()
    head = "已提交到打印队列（%s）：《%s》%d 份" % (printer, name, int(copies))
    if pages:
        head += "，共 %d 页" % int(pages)
    return head + "。"


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


_WRITE_TOOLS = (
    "memory_store", "item_add", "task_add", "fact_store",
    "task_update", "task_postpone",
)
# 出现这些词说明没写进去（被护栏拒绝、重复、找不到目标）
_NOT_WRITTEN = (
    "无法", "没有找到", "错误", "已忽略", "未重复", "不存在", "失败",
)
# 各工具「写成了」的说法不一样：物品是「已添加」，任务改期是「已改到」
_WRITTEN_MARKERS = ("已更新", "已改到", "已延期", "已改期")


def _day_token(tool_name, args):
    """这条写入口语里会出现的「哪天」，用来判断回复有没有已经说到。"""
    args = args or {}
    expiry = str(args.get("expiry_date") or "").strip()
    if expiry:
        return speak_date(expiry)
    due = str(args.get("due_at") or "").strip()
    if due:
        return speak_datetime(due)
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
    return text.startswith("已") or any(mark in text for mark in _WRITTEN_MARKERS)


def written_sentence(executed, today=None):
    """写成功之后的模板句：内容全部来自参数，不经模型。

    回复校验不通过时整句换成它——宁可少说几句，也不能把错日期念出去。
    `today` 由调用方传入（校验用哪个「今天」，这里就用哪个）。
    """
    parts = [
        summary(tool_name, args, today)
        for tool_name, args in executed or []
        if tool_name in _WRITE_TOOLS
    ]
    if not parts:
        return ""
    return "已记好：%s。" % "；".join(parts)


def has_template(tool_name):
    """这个工具写成功之后，能不能直接由参数拼出回话。

    目前只在回复校验失败时用（`verify_reply` 换成模板句）；不用于跳过
    第二轮 LLM——写入之后仍交给模型组织，宁慢一点，不让模板代替理解。
    """
    return tool_name in _WRITE_TOOLS


# 查询类工具：结果由用户嘴里那个词决定，也最容易被听错（「记」→「寄」）。
# 它们返回空时不能顺着错词下结论，要反问一句让用户确认。
QUERY_TOOLS = (
    "memory_search", "item_search", "fact_search", "family_search", "home_query",
)
_EMPTY_MARKERS = (
    "没有找到", "没有相关记忆", "暂无物品记录", "暂无结构化事实",
    "没有找到相关记录",
)

# 空结果时的回话：不猜用户想说什么，只说明没查到并请他确认，给纠正的机会。
# 用固定句而不是让模型自由发挥——模型会顺着听错的词往下问（「寄给谁？」）。
EMPTY_QUERY_REPLY = "我这边没查到相关记录。你要查的是哪件事？"


def is_empty_query(tool_name, result):
    """这次查询是不是「按关键词查了、什么都没有」。"""
    if tool_name not in QUERY_TOOLS:
        return False
    text = str(result or "")
    return any(marker in text for marker in _EMPTY_MARKERS)


# 事实被覆盖时，工具返回里有「（原值 X 已失效）」。确认后的模板回话要带上，
# 否则用户以为旧值还有效。
_REPLACED_NOTE_RE = re.compile(r"（原值\s*(.+?)\s*已失效）")


def replaced_note(result):
    """从工具返回里取出「原值已失效」提示；没有就返回空串。"""
    match = _REPLACED_NOTE_RE.search(str(result or ""))
    if not match:
        return ""
    return "原值 %s 已失效。" % match.group(1).strip()
