"""口播句事实校验：日期、星期、名字必须和工具写进去的一致。"""
import json
from datetime import date

from agent import wording
from agent.clarify import Clarifier
from agent.verify import verify_reply, written_days
from assistant import Assistant
from domain.facts import Facts
from domain.store import AgentStore
from domain.tasks import Tasks
from items import Items
from memory import Memory

TODAY = date(2026, 11, 23)  # 周一


def test_written_days_reads_expiry_and_due():
    executed = [
        ("item_add", {"name": "牛奶", "expiry_date": "2026-12-02"}),
        ("task_add", {"title": "取快递", "due_at": "2026-11-24T15:00:00"}),
        ("memory_store", {"text": "冰箱里有鸡蛋"}),
    ]

    assert written_days(executed) == [date(2026, 12, 2), date(2026, 11, 24)]


def test_correct_date_is_kept():
    executed = [("item_add", {"name": "牛奶", "expiry_date": "2026-12-02"})]

    text, replaced = verify_reply("好，12月2日（周三）到期。", executed, today=TODAY)

    assert replaced is False
    assert text == "好，12月2日（周三）到期。"


def test_wrong_date_is_replaced_by_template():
    executed = [("item_add", {"name": "牛奶", "expiry_date": "2026-12-02"})]

    text, replaced = verify_reply("好，11月26日到期。", executed, today=TODAY)

    assert replaced is True
    assert text == "已记好：牛奶，12月2日（周三）到期。"
    assert "11月26日" not in text


def test_right_date_with_wrong_weekday_is_replaced():
    executed = [("item_add", {"name": "牛奶", "expiry_date": "2026-12-02"})]

    text, replaced = verify_reply("12月2日（周四）到期", executed, today=TODAY)

    assert replaced is True
    assert "周四" not in text


def test_bare_weekday_only_checks_the_day_of_week():
    """口语里常省「这周」：说「周三」不该因为落在 11-25 而被判错。"""
    executed = [("item_add", {"name": "牛奶", "expiry_date": "2026-12-02"})]

    _text, replaced = verify_reply("周三就过期了", executed, today=TODAY)

    assert replaced is False


def test_qualified_weekday_must_match_the_date():
    executed = [("item_add", {"name": "牛奶", "expiry_date": "2026-12-02"})]

    _text, replaced = verify_reply("下周三过期", executed, today=TODAY)
    assert replaced is False

    _text, replaced = verify_reply("这周三过期", executed, today=TODAY)
    assert replaced is True


def test_relative_day_is_resolved_and_compared():
    executed = [("task_add", {"title": "取快递", "due_at": "2026-11-24T15:00:00"})]

    _text, replaced = verify_reply("明天下午3点提醒你", executed, today=TODAY)
    assert replaced is False

    text, replaced = verify_reply("后天下午3点提醒你", executed, today=TODAY)
    assert replaced is True
    assert text == "已记好：取快递，明天下午3点。"


def test_saying_today_is_allowed_as_context():
    """模型常补一句「今天是11月23日」，那是真的，不该拦。"""
    executed = [("item_add", {"name": "牛奶", "expiry_date": "2026-12-02"})]

    _text, replaced = verify_reply(
        "今天记下了，牛奶12月2日到期。", executed, today=TODAY,
    )

    assert replaced is False


def test_quoted_invented_name_is_replaced():
    executed = [("item_add", {"name": "牛奶", "expiry_date": "2026-12-02"})]

    text, replaced = verify_reply(
        "「酸奶」记好了，12月2日到期。", executed, "牛奶下周三过期", today=TODAY,
    )

    assert replaced is True
    assert "酸奶" not in text


def test_quoting_the_user_is_not_replaced():
    executed = [("item_add", {"name": "牛奶", "expiry_date": "2026-12-02"})]

    _text, replaced = verify_reply(
        "你说的「牛奶下周三过期」记下了，12月2日到期。",
        executed, "牛奶下周三过期", today=TODAY,
    )

    assert replaced is False


def test_no_dated_write_means_nothing_to_verify():
    executed = [("memory_store", {"text": "冰箱里还有三个鸡蛋"})]

    text, replaced = verify_reply("好的，记下了", executed, today=TODAY)

    assert replaced is False
    assert text == "好的，记下了"


def test_multiple_writes_accept_any_of_their_dates():
    executed = [
        ("item_add", {"name": "牛奶", "expiry_date": "2026-12-02"}),
        ("task_add", {"title": "取快递", "due_at": "2026-11-24T15:00:00"}),
    ]

    _text, replaced = verify_reply(
        "牛奶12月2日到期，快递明天下午3点提醒。", executed, today=TODAY,
    )

    assert replaced is False


# ---------- 与 agent loop 的接线 ----------

class ScriptedLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def _next(self):
        return self.responses.pop(0)

    def chat_with_tools(self, messages, tools, **kwargs):
        self.calls.append("plain")
        return self._next()

    def chat_with_tools_stream(self, messages, tools, on_delta=None):
        self.calls.append("stream")
        return self._next()


def _tool_call(name, arguments):
    return {
        "choices": [{
            "message": {
                "content": "",
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
                }],
            },
            "finish_reason": "tool_calls",
        }]
    }


def _stop(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}


def make_assistant(llm):
    store = AgentStore(":memory:")
    items = Items(":memory:")
    return Assistant(
        memory=Memory(":memory:"), llm=llm, items=items,
        clarifier=Clarifier(store=store, items=items),
        tasks=Tasks(":memory:"), facts=Facts(":memory:"),
    )


def test_agent_loop_replaces_misquoted_date():
    # 模型把「下周三」算成 11-25（实际 12-02）：护栏会改参数，模型嘴上仍念旧值
    llm = ScriptedLLM([
        _tool_call("item_add", {"name": "牛奶", "expiry_date": "2026-11-25"}),
        _stop("记好了，牛奶11月25日过期，记得喝。"),
    ])
    assistant = make_assistant(llm)

    result = assistant.process_text("牛奶下周三过期")

    assert "11月25日" not in result["reply"]
    assert wording.speak_date("2026-12-02") in result["reply"]


def test_agent_loop_does_not_stream_the_turn_after_a_write():
    """写完之后那句要过校验，不能提前逐字推给设备。"""
    llm = ScriptedLLM([
        _tool_call("item_add", {"name": "牛奶", "expiry_date": "2026-12-02"}),
        _stop("记好了"),
    ])
    assistant = make_assistant(llm)

    assistant.process_text("牛奶下周三过期", on_delta=lambda piece: None)

    # 第一轮（只吐 tool_calls）流式无妨；写完之后的最终回答必须走非流式
    assert llm.calls == ["stream", "plain"]


def test_written_sentence_covers_postpone():
    assert wording.written_sentence([
        ("task_postpone", {"title": "取快递", "due_at": "2026-11-24"}),
    ]) == "已记好：取快递，改到明天。"


def test_looks_written_accepts_postpone_and_update_results():
    assert wording.looks_written("task_postpone", "任务 #1 已改到 2026-11-24")
    assert wording.looks_written("task_update", "任务 #1 已更新：时间 2026-11-24")
    assert not wording.looks_written("task_postpone", "没有找到可延期的任务 #1")
