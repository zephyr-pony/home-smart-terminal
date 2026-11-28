from datetime import date, timedelta

from agent import wording


def _day(offset):
    return (date.today() + timedelta(days=offset)).isoformat()


# ---------- 日期口语化 ----------

def test_speak_date_uses_relative_words_for_near_days():
    assert wording.speak_date(_day(0)) == "今天"
    assert wording.speak_date(_day(1)) == "明天"
    assert wording.speak_date(_day(2)) == "后天"


def test_speak_date_gives_month_day_and_weekday():
    spoken = wording.speak_date("2026-12-02")
    assert spoken.startswith("12月2日")
    assert "周三" in spoken


def test_speak_datetime_appends_clock():
    """语音里要念「明天下午3点」，不能是「2026-11-24T15:00」。"""
    assert wording.speak_datetime(_day(1) + "T15:00:00") == "明天下午3点"
    assert wording.speak_datetime("2026-12-02T09:30:00") == "12月2日（周三）早上9点30分"


def test_speak_datetime_without_time_is_just_a_date():
    assert wording.speak_datetime(_day(1)) == "明天"


def test_speak_date_returns_input_when_unparsable():
    assert wording.speak_date("下周三") == "下周三"
    assert wording.speak_date("") == ""


# ---------- 参数摘要 ----------

def test_summary_item_with_and_without_expiry():
    assert wording.summary("item_add", {"name": "牛奶", "expiry_date": _day(2)}) == "牛奶，后天到期"
    assert wording.summary("item_add", {"name": "鸡蛋"}) == "鸡蛋，没有到期日"


def test_summary_task_with_and_without_time():
    assert wording.summary(
        "task_add", {"title": "取快递", "due_at": _day(1) + "T15:00:00"},
    ) == "取快递，明天下午3点"
    assert wording.summary("task_add", {"title": "交电费"}) == "交电费，没定时间"


def test_summary_fact_store():
    out = wording.summary(
        "fact_store", {"subject": "水费", "predicate": "户号", "value": "12345678"},
    )
    assert out == "水费户号 = 12345678"


# ---------- 确认句 ----------

def test_confirmation_quotes_what_user_said():
    """引用原话：人记得自己说了什么，比记得参数格式可靠。"""
    out = wording.confirmation_question(
        "item_add", {"name": "牛奶", "expiry_date": "2026-12-02"}, "牛奶下周三过期",
    )
    assert "你说的「牛奶下周三过期」" in out
    assert "牛奶，12月2日（周三）到期" in out
    assert "确认" in out and "取消" in out
    # 不能把 ISO 日期念给用户
    assert "2026-12-02" not in out


def test_confirmation_without_user_text_still_works():
    out = wording.confirmation_question("memory_store", {"text": "冰箱里还有三个鸡蛋"})
    assert out.startswith("要记成：")
    assert "冰箱里还有三个鸡蛋" in out


def test_confirmation_truncates_too_long_quote():
    """原话太长就截断，不是整句丢掉：截断的引用仍然比没有强。"""
    long_text = "我今天下班路上顺手买了牛奶还买了鸡蛋和面包" * 3
    out = wording.confirmation_question("item_add", {"name": "牛奶"}, long_text)
    assert "你说的" in out
    assert "…" in out
    assert len(out) < len(long_text)


# ---------- 回显 ----------

def test_echo_prefix_fires_when_reply_misses_the_date():
    out = wording.echo_prefix(
        [("item_add", {"name": "牛奶", "expiry_date": _day(2)})],
        "好的，记下了",
    )
    assert out == "牛奶，后天到期。"


def test_echo_prefix_is_quiet_when_reply_already_says_it():
    out = wording.echo_prefix(
        [("item_add", {"name": "牛奶", "expiry_date": _day(2)})],
        "好的，牛奶后天到期",
    )
    assert out == ""


def test_echo_prefix_skips_writes_without_dates():
    """没有日期的事情，模型说一遍就够了，回显只会变成复读。"""
    out = wording.echo_prefix([("memory_store", {"text": "明天买电池"})], "记住了")
    assert out == ""


def test_echo_prefix_joins_multiple_writes():
    out = wording.echo_prefix(
        [
            ("item_add", {"name": "牛奶", "expiry_date": _day(2)}),
            ("task_add", {"title": "取快递", "due_at": _day(1) + "T15:00:00"}),
        ],
        "记好了",
    )
    assert "牛奶，后天到期" in out
    assert "取快递，明天下午3点" in out


# ---------- 是否真的写进去了 ----------

def test_looks_written_reads_tool_messages():
    assert wording.looks_written("memory_store", "已存储")
    assert wording.looks_written("item_add", "已添加物品：牛奶，到期日 2026-12-02")
    assert not wording.looks_written("item_add", "无法添加物品：物品名称不能为空")
    assert not wording.looks_written("memory_store", "已存在相同记忆，未重复写入")
    assert not wording.looks_written("task_list", "已存储")  # 不是写工具


def test_replaced_note_is_carried_into_the_template():
    """事实被覆盖时，模板必须说「原值已失效」，不能只报新值。"""
    message = "已保存：牛奶 · 品牌 = 伊利（原值 蒙牛 已失效）"
    assert wording.replaced_note(message) == "原值 蒙牛 已失效。"
    assert wording.replaced_note("已保存：牛奶 · 品牌 = 伊利") == ""


def test_empty_query_is_recognized_for_search_tools():
    assert wording.is_empty_query("family_search", "（没有找到相关记录）")
    assert wording.is_empty_query("memory_search", "没有找到关于 寄东西 的记忆")
    assert not wording.is_empty_query("memory_search", "[2026-11-23] 牛奶快喝完了")
    # 清单类没有关键词，不在此列
    assert not wording.is_empty_query("task_list", "（暂无任务）")


def test_looks_like_tool_call_text_catches_broken_markup():
    """deepseek 会把工具调用写成正文，而且格式很随意（多空格、全角竖线）。"""
    assert wording.looks_like_tool_call_text('<invoke name="web_search">')
    assert wording.looks_like_tool_call_text('< invoke name="web_search">')
    assert wording.looks_like_tool_call_text("</｜tool_calls>")
    assert wording.looks_like_tool_call_text("<tool_calls>")
    assert not wording.looks_like_tool_call_text("明天上午九点开会。")
    assert not wording.looks_like_tool_call_text("")

