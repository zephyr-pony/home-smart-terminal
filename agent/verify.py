"""回复发出前的事实校验：模型嘴上说的日期必须和工具写进去的一致。

为什么要有这一层：护栏和 `correction_note` 只保证**库里**正确，用户听到的却是模型
那一句。实测 4B 模型在同一个回复里能一边写对一边念错——参数是 12 月 2 日，嘴里说
「11 月 26 日」；或者说成「12 月 2 日（周四）」（那天是周三）。语音场景没有回看的机会，
用户按听到的记，错的就是错的。

做法：把回复里出现的日期说法全部解析出来，逐个和工具参数里的日期比。不一致就整句
换成参数拼的模板句——不修补、不拼接，避免「一句对一句错」的混合输出。

只有「写了带日期的东西」才校验：没有事实可对时，模型闲聊里的日期不该被拦。
"""
import re
from datetime import date

from agent import wording
from agent.dates import parse_date_text
from agent.guard import is_grounded

# 具体日子：「2026-12-02」「12月2日」「12月2号」
_ABS_DATE_RE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}月\d{1,2}[日号]")
# 相对天：「明天」「后天」这类，解析成具体日子再比
_REL_DAY_RE = re.compile(r"大后天|后天|明天|今天|昨天|前天")
# 周几：「周三」「星期三」「下周三」
_WEEKDAY_RE = re.compile(r"(下下|下|上|本|这)?(?:周|星期)([一二三四五六日天])")
# 模型给名字加引号时，引号里的东西要有出处
_QUOTED_RE = re.compile(r"「([^」]{1,20})」|“([^”]{1,20})”")


def _as_date(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def written_days(executed):
    """这次真的写进去的日期（可能多条写入，也可能没有）。"""
    days = []
    for _name, args in executed or []:
        for key in ("expiry_date", "due_at"):
            day = _as_date((args or {}).get(key))
            if day:
                days.append(day)
    return days


def _span_day(span, today):
    """一句话里的日期说法 → date；解析不了返回 None（不判，宁可放过）。"""
    parsed = parse_date_text(span, today)
    if not parsed:
        return None
    return _as_date(parsed.get("date"))


def _names_ok(text, executed, user_text):
    """引号里冒出来的名字必须在用户原话或工具参数里出现过。"""
    sources = [str(user_text or "")]
    for _name, args in executed or []:
        sources.extend(str(value) for value in (args or {}).values())
    source_text = " ".join(sources)
    for match in _QUOTED_RE.finditer(text):
        quoted = (match.group(1) or match.group(2) or "").strip()
        if quoted and not is_grounded(quoted, source_text):
            return False
    return True


def verify_reply(reply, executed, user_text=None, today=None):
    """核对回复里的日期、星期、名字。

    Returns:
        (text, replaced)：不一致时返回参数拼的模板句，且 replaced 为 True。
    """
    text = str(reply or "").strip()
    days = written_days(executed)
    if not days or not text:
        return reply, False

    today = today or date.today()
    # 今天也放行：模型常补一句「今天是 11 月 23 日」，那是真的
    allowed = set(days) | {today}
    allowed_weekdays = {day.weekday() for day in allowed}

    bad = False
    for pattern in (_ABS_DATE_RE, _REL_DAY_RE):
        for match in pattern.finditer(text):
            day = _span_day(match.group(0), today)
            if day is not None and day not in allowed:
                bad = True
    for match in _WEEKDAY_RE.finditer(text):
        prefix = match.group(1)
        if prefix:
            # 「下周三」是确定的某一天，整句都对才行
            day = _span_day(match.group(0), today)
            if day is not None and day not in allowed:
                bad = True
        else:
            # 只说「周三」时只核对星期几：口语里本来就常省「这周」
            day = _span_day(match.group(0), today)
            if day is not None and day.weekday() not in allowed_weekdays:
                bad = True

    if not _names_ok(text, executed, user_text):
        bad = True

    if not bad:
        return reply, False
    template = wording.written_sentence(executed)
    if not template:
        return reply, False
    return template, True
