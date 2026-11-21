"""中文日期时间短语解析。

小模型算相对日期并不可靠：实测「牛奶下周三过期」，三个 4B 模型里有两个算成了
周四；「冰箱里还有三个鸡蛋」还会被凭空补上一个到期日。这类换算是确定性的，
交给代码比交给模型稳，所以单独成模块。

只覆盖家庭场景的常见说法，不追求通用自然语言日期库。
"""
import re
from datetime import date, datetime, timedelta

_WEEKDAY_INDEX = {
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
}
_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_NUM = "0-9零〇一二两三四五六七八九十"

# 长词在前，避免「大后天」被「后天」先匹配
_REL_DAYS = (
    ("大后天", 3),
    ("后天", 2),
    ("明天", 1),
    ("明日", 1),
    ("今天", 0),
    ("今日", 0),
    ("昨天", -1),
    ("前天", -2),
)

_TIME_RE = re.compile(
    r"(上午|下午|晚上|傍晚|中午|凌晨|早上)?\s*([%s]{1,3})\s*[点時时]\s*(半|[%s]{1,3}\s*分)?"
    % (_NUM, _NUM)
)
_ABS_YM_D_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?")
_ABS_M_D_RE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
_WEEK_RE = re.compile(r"(下下|下|这|本|上)?\s*个?\s*(?:周|星期|礼拜)\s*([一二三四五六日天])")
_DAYS_LATER_RE = re.compile(r"([%s]{1,3})\s*天\s*(?:以后|之后|后)" % _NUM)
_WEEKS_LATER_RE = re.compile(r"([%s]{1,3})\s*(?:个)?\s*(?:周|星期|礼拜)\s*(?:以后|之后|后)" % _NUM)
_NEXT_MONTH_DAY_RE = re.compile(r"下\s*个?\s*月\s*([%s]{1,3})\s*[日号]" % _NUM)

_DATE_HINT_RE = re.compile(
    r"\d{4}\s*年|\d{1,2}\s*月\s*\d{1,2}\s*[日号]|周[一二三四五六日天]|星期[一二三四五六日天]"
    r"|礼拜[一二三四五六日天]|今天|今日|明天|明日|后天|昨天|前天"
    r"|[%s]{1,3}\s*天\s*(?:以后|之后|后)|下\s*个?\s*月|月[初中底]" % _NUM
)


def _cn_to_int(text):
    """把「三」「十二」「二十三」「3」这类写法转成整数；无法识别返回 None。"""
    text = str(text or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if "十" in text:
        left, _, right = text.partition("十")
        if left and left not in _CN_DIGITS:
            return None
        if right and right not in _CN_DIGITS:
            return None
        tens = _CN_DIGITS.get(left, 1) if left else 1
        ones = _CN_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    total = 0
    for ch in text:
        if ch not in _CN_DIGITS:
            return None
        total = total * 10 + _CN_DIGITS[ch]
    return total


def _parse_absolute(text, today):
    match = _ABS_YM_D_RE.search(text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    match = _ABS_M_D_RE.search(text)
    if match:
        month, day = int(match.group(1)), int(match.group(2))
        for year in (today.year, today.year + 1):
            try:
                candidate = date(year, month, day)
            except ValueError:
                continue
            # 只说了「11月25日」而今年这天已过去很久时，按明年算
            if candidate >= today - timedelta(days=90):
                return candidate
        return None
    return None


def _parse_week(text, today):
    match = _WEEK_RE.search(text)
    if not match:
        return None
    prefix, weekday_char = match.group(1), match.group(2)
    monday = today - timedelta(days=today.weekday())
    index = _WEEKDAY_INDEX[weekday_char]
    if prefix == "下下":
        return monday + timedelta(days=14 + index)
    if prefix == "下":
        return monday + timedelta(days=7 + index)
    if prefix == "上":
        return monday - timedelta(days=7 - index)
    # 「这周三」「本周三」按本周算；不带前缀时取最近的那个（含今天）
    target = monday + timedelta(days=index)
    if prefix in ("这", "本"):
        return target
    if target < today:
        target += timedelta(days=7)
    return target


def _parse_relative_days(text, today):
    for phrase, offset in _REL_DAYS:
        if phrase in text:
            return today + timedelta(days=offset)

    match = _DAYS_LATER_RE.search(text)
    if match:
        days = _cn_to_int(match.group(1))
        if days is not None:
            return today + timedelta(days=days)

    match = _WEEKS_LATER_RE.search(text)
    if match:
        weeks = _cn_to_int(match.group(1))
        if weeks is not None:
            return today + timedelta(days=weeks * 7)

    match = _NEXT_MONTH_DAY_RE.search(text)
    if match:
        day = _cn_to_int(match.group(1))
        if day:
            year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
            for candidate_day in range(day, 0, -1):
                try:
                    return date(year, month, candidate_day)
                except ValueError:
                    continue
    return None


def _parse_time(text):
    """返回 'HH:MM'；没有时间说法时返回 None。"""
    match = _TIME_RE.search(text)
    if not match:
        return None
    period = match.group(1) or ""
    hour = _cn_to_int(match.group(2))
    if hour is None or not 0 <= hour <= 24:
        return None
    minute_text = (match.group(3) or "").strip()
    if not minute_text:
        minute = 0
    elif minute_text == "半":
        minute = 30
    else:
        minute = _cn_to_int(minute_text.replace("分", "").strip()) or 0
    if not 0 <= minute < 60:
        minute = 0
    if period in ("下午", "晚上", "傍晚", "中午") and hour < 12:
        hour += 12
    if hour >= 24:
        hour -= 24
    return "%02d:%02d" % (hour, minute)


def parse_date_text(text, today=None):
    """解析一句话里的日期（可选时间）。

    Returns:
        dict: {"date": "YYYY-MM-DD", "time": "HH:MM" 或 None, "phrase": 原文片段}
        解析不到日期时返回 None。
    """
    text = str(text or "")
    if not text.strip():
        return None
    today = today or date.today()
    if isinstance(today, datetime):
        today = today.date()

    target = _parse_absolute(text, today)
    if target is None:
        target = _parse_relative_days(text, today)
    if target is None:
        target = _parse_week(text, today)
    if target is None:
        return None
    return {
        "date": target.strftime("%Y-%m-%d"),
        "time": _parse_time(text),
    }


def has_date_hint(text):
    """句子里是否出现了日期线索（用于「模型给了日期但解析不出来」时兜底）。"""
    return bool(_DATE_HINT_RE.search(str(text or "")))


def parse_time_text(text):
    """只取时间，用于「下午三点提醒我」这类当天口头约定。"""
    return _parse_time(str(text or ""))


def resolve_due(text, today=None, allow_time_only=False):
    """把一句话换算成任务/物品可用的日期字符串。

    Returns:
        "YYYY-MM-DD"、"YYYY-MM-DDTHH:MM"，或 None（句子里没有日期）。
    """
    parsed = parse_date_text(text, today)
    if parsed:
        if parsed["time"]:
            return "%sT%s" % (parsed["date"], parsed["time"])
        return parsed["date"]
    if allow_time_only:
        # 「下午三点提醒我交电费」只给了时间，按今天算
        clock = parse_time_text(text)
        if clock:
            today = today or date.today()
            if isinstance(today, datetime):
                today = today.date()
            return "%sT%s" % (today.strftime("%Y-%m-%d"), clock)
    return None
