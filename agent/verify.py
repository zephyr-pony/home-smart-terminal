"""回复发出前的事实校验：模型嘴上说的日期、时刻必须和工具写进去的一致。

为什么要有这一层：护栏和 `correction_note` 只保证**库里**正确，用户听到的却是模型
那一句。实测 4B 模型在同一个回复里能一边写对一边念错——参数是 12 月 2 日，嘴里说
「11 月 26 日」；或者说成「12 月 2 日（周四）」（那天是周三）；再或者日期全对、
时间念成「晚上 8 点」（参数是 15:00）。语音场景没有回看的机会，用户按听到的记，
错的就是错的。

做法：把回复里出现的日期、时刻说法全部解析出来，逐个和工具参数里的值比。不一致就
整句换成参数拼的模板句——不修补、不拼接，避免「一句对一句错」的混合输出。

只有「写了带日期/时刻的东西」才校验：没有事实可对时，模型闲聊里的时间不该被拦。
"""
import re
from datetime import date

from agent import wording
from agent.dates import parse_date_text, parse_time_text
from agent.guard import is_grounded

# 具体日子：「2026-12-02」「12月2日」「12月2号」
_ABS_DATE_RE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}月\d{1,2}[日号]")
# 相对天：「明天」「后天」这类，解析成具体日子再比
_REL_DAY_RE = re.compile(r"大后天|后天|明天|今天|昨天|前天")
# 周几：「周三」「星期三」「下周三」
_WEEKDAY_RE = re.compile(r"(下下|下|上|本|这)?(?:周|星期)([一二三四五六日天])")
# 模型给名字加引号时，引号里的东西要有出处
_QUOTED_RE = re.compile(r"「([^」]{1,20})」|“([^”]{1,20})”")
# 口播里的时刻：「下午3点」「三点半」「15:00」。校验时间必须有这一层，
# 否则参数是 15:00、嘴里说 20:00，日期全对也不会被拦下。
_TIME_RE = re.compile(
    r"(?:上午|下午|晚上|傍晚|中午|凌晨|早上|夜里)?\s*(?:"
    r"[0-9]{1,2}\s*[:：]\s*[0-9]{2}"
    r"|[0-9零〇一二两三四五六七八九十]{1,3}\s*[点時时]\s*"
    r"(?:半|[0-9零〇一二两三四五六七八九十]{1,3}\s*分)?"
    r")"
)
# 工具参数里的时刻：2026-11-24T15:00:00
_ISO_CLOCK_RE = re.compile(r"[T ]\s*(\d{1,2}):(\d{2})")
# 钟点前的时段词：有它才把「一点」当时间
_PERIOD_RE = re.compile(r"上午|下午|晚上|傍晚|中午|凌晨|早上|夜里")


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


def written_times(executed):
    """这次真的写进去的时刻（`HH:MM`）；只写了日期时为空。"""
    times = []
    for _name, args in executed or []:
        for key in ("due_at", "remind_at"):
            raw = str((args or {}).get(key) or "")
            for match in _ISO_CLOCK_RE.finditer(raw):
                hour, minute = int(match.group(1)), int(match.group(2))
                if 0 <= hour < 24 and 0 <= minute < 60:
                    times.append("%02d:%02d" % (hour, minute))
    return times


def _span_clock(span):
    """回复里的一句话时刻 → `HH:MM`；解析不了返回 None（不判，宁可放过）。"""
    text = str(span or "").strip()
    colon = re.search(r"(\d{1,2})\s*[:：]\s*(\d{2})", text)
    if colon:
        hour, minute = int(colon.group(1)), int(colon.group(2))
        if not (0 <= hour < 24 and 0 <= minute < 60):
            return None
        return "%02d:%02d" % (hour, minute)
    return parse_time_text(text)


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
    """核对回复里的日期、星期、时刻、名字。

    Returns:
        (text, replaced)：不一致时返回参数拼的模板句，且 replaced 为 True。
    """
    text = str(reply or "").strip()
    days = written_days(executed)
    times = written_times(executed)
    if not text or not (days or times):
        return reply, False

    today = today or date.today()
    bad = False

    if days:
        # 今天也放行：模型常补一句「今天是 11 月 23 日」，那是真的
        allowed = set(days) | {today}
        allowed_weekdays = {day.weekday() for day in allowed}

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

    # 时刻只在真的写了时间时核对。只写了日期时不核对：中文「一点」「差点」
    # 这类说法会被误判成 1 点，宁可放过。
    if times:
        allowed_times = set(times)
        for match in _TIME_RE.finditer(text):
            span = match.group(0)
            # 「一点」在中文里常是「快一点/一点事」，没有时段词时不当时间
            if span.strip() in ("一点", "1点") and not _PERIOD_RE.search(span):
                continue
            clock = _span_clock(span)
            if clock is not None and clock not in allowed_times:
                bad = True

    if not _names_ok(text, executed, user_text):
        bad = True

    if not bad:
        return reply, False
    template = wording.written_sentence(executed, today)
    if not template:
        return reply, False
    return template, True
