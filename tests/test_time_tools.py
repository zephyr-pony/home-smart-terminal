"""时间与算术工具（D72）：现在几点、日期推算、精确算术。

这两件事模型自己做会错（时刻它根本看不到；跨月日期差、带小数的乘除常算错），
所以做成确定计算，测的是「答案对不对」，不是措辞。
"""
from datetime import datetime

from memory import Memory
from tools import get_all_tools, get_calc_tool, get_time_tool

NOW = datetime(2026, 11, 26, 19, 50)  # 周四


def time_tool():
    return get_time_tool(now_provider=lambda: NOW)


def test_now_includes_date_weekday_and_clock():
    output = time_tool()["execute"]()

    assert "2026年11月26日（周四）" in output
    assert "19:50" in output


def test_offset_days_forward():
    output = time_tool()["execute"](offset_days=3)

    assert "3 天后" in output
    assert "2026年11月29日（周日）" in output


def test_offset_days_backward():
    output = time_tool()["execute"](offset_days=-7)

    assert "7 天前" in output
    assert "2026年11月19日（周四）" in output


def test_offset_days_zero_is_today():
    output = time_tool()["execute"](offset_days=0)

    assert "今天就是" in output
    assert "2026年11月26日（周四）" in output


def test_target_date_in_the_future():
    output = time_tool()["execute"](target_date="2026-12-25")

    assert "2026年12月25日（周五）" in output
    assert "还有 29 天" in output


def test_target_date_in_the_past():
    output = time_tool()["execute"](target_date="2026-11-20")

    assert "已经过去 6 天" in output


def test_target_date_today():
    output = time_tool()["execute"](target_date="2026-11-26")

    assert "就是今天" in output


def test_bad_target_date_explains_format():
    output = time_tool()["execute"](target_date="12月25日")

    assert output == "target_date 要写成 YYYY-MM-DD（比如 2026-12-25）"


def calc(expression):
    return get_calc_tool()["execute"](expression=expression)


def test_calc_keeps_cents():
    assert calc("268*0.85") == "268*0.85 = 227.8"


def test_calc_integer_result_has_no_trailing_point():
    assert calc("(120+80)/2") == "(120+80)/2 = 100"


def test_calc_accepts_fullwidth_and_thousands_separator():
    assert calc("1,000×3") == "1000*3 = 3000"


def test_calc_rejects_division_by_zero():
    assert "0" in calc("1/0")
    assert "=" not in calc("1/0")


def test_calc_refuses_code_execution():
    """ast 求值器只认数字，别把工具变成任意代码执行入口。"""
    output = calc("__import__('os').system('echo hi')")

    assert "算不了" in output
    assert "=" not in output


def test_calc_caps_power():
    assert "太大" in calc("9**9**9")


def test_time_and_calc_are_registered_without_old_date_tool():
    tools = get_all_tools(Memory(":memory:"))
    names = [item["schema"]["function"]["name"] for item in tools]

    assert "time_now" in names
    assert "calc" in names
    assert "get_current_date" not in names
