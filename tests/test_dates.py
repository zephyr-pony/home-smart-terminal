"""中文日期短语解析测试（固定「今天」= 2026-11-21 星期六）。"""
from datetime import date, datetime

from agent.dates import (
    has_date_hint, parse_date_text, parse_recurrence, parse_time_text, resolve_due,
)

TODAY = date(2026, 11, 21)


def _date(text, today=TODAY):
    parsed = parse_date_text(text, today)
    return parsed["date"] if parsed else None


def test_relative_days():
    assert _date("明天交电费") == "2026-11-22"
    assert _date("后天") == "2026-11-23"
    assert _date("大后天取快递") == "2026-11-24"
    assert _date("今天买的") == "2026-11-21"
    assert _date("昨天") == "2026-11-20"


def test_week_phrases():
    # 2026-11-21 是星期六，本周一 = 11-16
    assert _date("下周三过期") == "2026-11-25"
    assert _date("下下周一") == "2026-11-30"
    # 不带前缀 = 最近的那个周三（已过的本周三顺延到下周）
    assert _date("周三开会") == "2026-11-25"
    # 显式本周就是本周
    assert _date("这周三") == "2026-11-18"
    assert _date("周日") == "2026-11-22"


def test_days_and_weeks_later():
    assert _date("3天后") == "2026-11-24"
    assert _date("三天以后提醒我") == "2026-11-24"
    assert _date("两周后") == "2026-12-05"
    assert _date("下个月5号") == "2026-12-05"


def test_absolute_dates():
    assert _date("11月25日") == "2026-11-25"
    assert _date("2026年12月1日") == "2026-12-01"
    assert _date("11月25号过期") == "2026-11-25"


def test_time_parsing():
    parsed = parse_date_text("明天下午三点提醒我交电费", TODAY)
    assert parsed["date"] == "2026-11-22"
    assert parsed["time"] == "15:00"

    # 只有时间没有日期时，parse_date_text 返回 None，时间要用 parse_time_text
    assert parse_date_text("晚上八点半开会", TODAY) is None
    assert parse_time_text("晚上八点半开会") == "20:30"
    assert parse_time_text("中午12点") == "12:00"
    assert parse_time_text("上午九点") == "09:00"


def test_resolve_due_formats():
    assert resolve_due("明天下午三点提醒我交电费", TODAY) == "2026-11-22T15:00"
    assert resolve_due("明天交电费", TODAY) == "2026-11-22"
    # 只给时间：按今天算
    assert resolve_due("下午三点提醒我交电费", TODAY, allow_time_only=True) == "2026-11-21T15:00"
    assert resolve_due("下午三点提醒我交电费", TODAY) is None
    assert resolve_due("冰箱里还有三个鸡蛋", TODAY) is None


def test_no_date_phrases():
    for text in ("冰箱里还有三个鸡蛋", "户号是123456", "我有哪些提醒", "帮我讲个笑话"):
        assert parse_date_text(text, TODAY) is None
        assert has_date_hint(text) is False


def test_date_hint_detects_unparsed_phrases():
    assert has_date_hint("牛奶下周三过期") is True
    assert has_date_hint("月底前交掉") is True


def test_vague_phrases_are_not_wiped_by_guard():
    """「年底」「过几天」解析不了，但必须被认成日期线索，别把模型给的日期抹掉。"""
    from agent.guard import ground_expiry

    for text in ("牛奶年底过期", "牛奶过几天过期", "牛奶下个月过期", "牛奶最近过期"):
        assert has_date_hint(text) is True
        assert ground_expiry("2026-12-31", text, TODAY) == "2026-12-31"
    # 真没有任何日期线索时仍然留空
    assert ground_expiry("2026-12-31", "冰箱里还有三个鸡蛋", TODAY) is None


def test_weekend_phrase():
    # 2026-11-21 是周六，「周末」就是当天；2026-11-22 是周日，顺延到下周六
    assert _date("周末大扫除") == "2026-11-21"
    assert _date("周末大扫除", date(2026, 11, 22)) == "2026-11-28"


# ---------- 周期提醒（2026-11-23） ----------

NOW = datetime(2026, 11, 21, 10, 0, 0)  # 周六上午十点


def test_recurrence_daily_uses_next_occurrence():
    # 早上八点已经过了，下一次是明天，不是立刻响
    assert parse_recurrence("每天早上八点提醒我吃药", TODAY, NOW) == {
        "recur": "daily", "date": "2026-11-22", "time": "08:00",
    }
    # 晚上八点还没到，就从今天开始
    assert parse_recurrence("每天晚上八点提醒我吃药", TODAY, NOW)["date"] == "2026-11-21"


def test_recurrence_weekly():
    parsed = parse_recurrence("每周三下午三点开会", TODAY, NOW)
    assert parsed["recur"] == "weekly"
    assert parsed["date"] == "2026-11-25"  # 周六说「每周三」→ 下周三
    assert parsed["time"] == "15:00"


def test_recurrence_monthly_and_clamp():
    parsed = parse_recurrence("每月5号早上九点交房租", TODAY, NOW)
    assert parsed["recur"] == "monthly"
    assert parsed["date"] == "2026-12-05"  # 本月 5 号已过
    # 每月 31 号：11 月只有 30 天，按当月最后一天
    parsed = parse_recurrence("每月31号早上九点", TODAY, NOW)
    assert parsed["date"] == "2026-11-30"


def test_recurrence_without_time_keeps_none():
    parsed = parse_recurrence("每天提醒我吃药", TODAY, NOW)
    assert parsed["recur"] == "daily"
    assert parsed["time"] is None


def test_resolve_due_uses_recurrence_anchor():
    assert resolve_due("每天早上八点提醒我吃药", TODAY, allow_time_only=True,
                       now=NOW) == "2026-11-22T08:00"
    assert has_date_hint("每天提醒我吃药") is True
