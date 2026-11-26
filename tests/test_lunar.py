"""农历、节气、传统节日（D72）。

固定日期断言，不跟今天比——CI 在别的时区/别的日子跑也必须一样。
cnlunar 没装时整组跳过（它只是可选能力缺口，不是回归）。
"""
import datetime

import pytest

from agent import lunar

pytestmark = pytest.mark.skipif(
    not lunar.available(), reason="没装 cnlunar，农历能力不可用"
)


def test_lunar_day_info():
    info = lunar.day_info(datetime.date(2026, 11, 26))

    assert info["lunar_month"] == 10
    assert info["lunar_day"] == 18
    assert info["month_text"] + info["day_text"] == "十月十八"
    assert info["ganzhi"] + info["zodiac"] == "丙午马"


def test_spring_festival_is_new_year_day():
    info = lunar.day_info(datetime.date(2026, 2, 17))

    assert (info["lunar_month"], info["lunar_day"]) == (1, 1)
    assert "春节" in info["festivals"]


def test_new_years_eve_by_next_day_rule():
    """cnlunar 不标除夕，靠「明天是正月初一」补。"""
    info = lunar.day_info(datetime.date(2026, 2, 16))

    assert info["lunar_month"] == 12
    assert "除夕" in info["festivals"]
    assert "春节" not in info["festivals"]


def test_mid_autumn_festival():
    info = lunar.day_info(datetime.date(2026, 9, 25))

    assert (info["lunar_month"], info["lunar_day"]) == (8, 15)
    assert "中秋节" in info["festivals"]


def test_solar_term_on_the_day():
    assert lunar.day_info(datetime.date(2026, 2, 4))["term"] == "立春"


def test_solar_term_lookup():
    assert lunar.solar_term(2026, "冬至") == datetime.date(2026, 12, 22)
    assert lunar.solar_term(2026, "立春") == datetime.date(2026, 2, 4)


def test_solar_term_rejects_unknown_name():
    with pytest.raises(ValueError):
        lunar.solar_term(2026, "大太阳")


def test_lunar_to_solar():
    assert lunar.to_solar(2026, 8, 15) == datetime.date(2026, 9, 25)
    assert lunar.to_solar(2027, 1, 1) == datetime.date(2027, 2, 6)


def test_lunar_to_solar_handles_leap_month():
    """2025 有闰六月：闰六月初一 = 7/25，六月初一 = 6/25。"""
    assert lunar.to_solar(2025, 6, 1, leap=True) == datetime.date(2025, 7, 25)
    assert lunar.to_solar(2025, 6, 1) == datetime.date(2025, 6, 25)


def test_lunar_to_solar_rejects_day_out_of_month():
    with pytest.raises(ValueError):
        # 2026 的八月是「小月」（29 天）
        lunar.to_solar(2026, 8, 30)


def test_year_range_is_guarded():
    with pytest.raises(ValueError):
        lunar.day_info(datetime.date(2150, 1, 1))


def test_no_zero_month():
    with pytest.raises(ValueError):
        lunar.to_solar(2026, 0, 1)


# ---------- 工具层 ----------

def lunar_tool():
    from tools import get_lunar_tool

    return get_lunar_tool()


def test_tool_defaults_to_today():
    output = lunar_tool()["execute"]()

    assert output.startswith("公历：")
    assert "农历：" in output


def test_tool_formats_spring_festival():
    output = lunar_tool()["execute"](date="2026-02-17")

    assert "2026年2月17日（周二）" in output
    assert "正月初一" in output
    assert "春节" in output


def test_tool_converts_lunar_to_solar():
    output = lunar_tool()["execute"](lunar="8-15")

    assert "八月十五" in output
    assert "2026年9月25日（周五）" in output


def test_tool_lookup_term():
    output = lunar_tool()["execute"](term="立春")

    assert "立春" in output
    assert "2026年2月4日（周三）" in output


def test_tool_explains_lunar_format():
    output = lunar_tool()["execute"](lunar="八月十五")

    assert "月-日" in output


def test_tool_is_registered():
    from memory import Memory
    from tools import get_all_tools

    names = [item["schema"]["function"]["name"]
             for item in get_all_tools(Memory(":memory:"))]

    assert "lunar_calendar" in names
