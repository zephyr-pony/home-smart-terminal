"""弱模型模式：写操作先复述参数、等确认后再落库。"""
import json
from datetime import date, timedelta

from agent.clarify import Clarifier
from agent.confirm import WRITE_ACTIONS, parse_confirm_actions
from assistant import Assistant
from domain.facts import Facts
from domain.store import AgentStore
from domain.tasks import Tasks
from items import Items
from memory import Memory


class ScriptedLLM:
    """按预设顺序返回响应，用于模拟工具调用回合。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat_with_tools(self, messages, tools, **kwargs):
        self.calls.append(messages)
        return self.responses.pop(0)


def _tool_call(name, arguments):
    return {
        "choices": [{
            "message": {
                "content": "",
                "tool_calls": [{
                    "id": "call-1",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }],
            },
            "finish_reason": "tool_calls",
        }]
    }


def _stop(content):
    return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}


def make_assistant(llm, confirm_write_actions=True):
    items = Items(":memory:")
    assistant = Assistant(
        memory=Memory(":memory:"),
        items=items,
        llm=llm,
        clarifier=Clarifier(store=AgentStore(":memory:"), items=items),
        tasks=Tasks(":memory:"),
        facts=Facts(":memory:"),
    )
    assistant.confirm_write_actions = parse_confirm_actions(confirm_write_actions)
    return assistant


def test_item_add_asks_before_writing():
    llm = ScriptedLLM([
        _tool_call("item_add", {"name": "鸡蛋", "expiry_date": "2026-12-31"}),
        _stop("好的"),
    ])
    assistant = make_assistant(llm)

    result = assistant.process_text("冰箱里还有三个鸡蛋", client_id="c1")

    assert result["type"] == "confirm"
    # 复述里要说清楚「没有到期日」，编出来的 12-31 已被护栏清掉
    assert "鸡蛋" in result["reply"]
    assert "没有到期日" in result["reply"]
    assert assistant.items.list_all() == []


def test_confirm_writes_and_keeps_guarded_args():
    llm = ScriptedLLM([
        _tool_call("item_add", {"name": "牛奶", "expiry_date": "2026-11-26"}),
        _stop("好的"),
    ])
    assistant = make_assistant(llm)
    assistant.process_text("牛奶下周三过期", client_id="c1")

    result = assistant.process_text("确认", client_id="c1")

    rows = assistant.items.list_all()
    assert len(rows) == 1
    assert rows[0]["expiry_date"] == "2026-11-25"
    assert result["type"] == "record"
    assert "11-25" in result["reply"]


def test_cancel_writes_nothing():
    llm = ScriptedLLM([
        _tool_call("item_add", {"name": "鸡蛋"}),
        _stop("好的"),
    ])
    assistant = make_assistant(llm)
    assistant.process_text("冰箱里还有三个鸡蛋", client_id="c1")

    result = assistant.process_text("取消", client_id="c1")

    assert assistant.items.list_all() == []
    assert result["action"] == "cancel"


def test_task_add_confirmation_shows_time():
    llm = ScriptedLLM([
        _tool_call("task_add", {"title": "取快递", "due_at": "2026-11-22"}),
        _stop("好的"),
    ])
    assistant = make_assistant(llm)
    result = assistant.process_text("明天下午三点提醒我取快递", client_id="c1")
    tomorrow = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")
    assert result["type"] == "confirm"
    assert "取快递" in result["reply"]
    assert "%s 15:00" % tomorrow in result["reply"]

    assistant.process_text("确认", client_id="c1")
    tasks = assistant.tasks.list_all()
    assert len(tasks) == 1
    assert tasks[0]["due_at"].startswith("%sT15" % tomorrow)


def test_switch_off_writes_directly():
    llm = ScriptedLLM([
        _tool_call("item_add", {"name": "鸡蛋"}),
        _stop("记好了"),
    ])
    assistant = make_assistant(llm, confirm_write_actions=False)
    result = assistant.process_text("冰箱里还有三个鸡蛋", client_id="c1")
    assert result["used_tools"] == ["item_add"]
    assert len(assistant.items.list_all()) == 1


def test_false_claim_replaced_when_no_tool_ran():
    """没调工具却回「已经记好了」，要改成诚实说法。"""
    llm = ScriptedLLM([_stop("记好啦，鸡蛋已经记到物品清单里了。")])
    assistant = make_assistant(llm)
    result = assistant.process_text("冰箱里还有三个鸡蛋", client_id="c1")
    assert assistant.items.list_all() == []
    assert "没写进清单" in result["reply"]


def test_false_claim_not_triggered_by_question():
    llm = ScriptedLLM([_stop("你已设置的提醒有：交电费。")])
    assistant = make_assistant(llm)
    result = assistant.process_text("我有哪些提醒", client_id="c1")
    assert "没写进清单" not in result["reply"]


def test_parse_confirm_actions_values():
    assert parse_confirm_actions("0") == frozenset()
    assert parse_confirm_actions("off") == frozenset()
    assert parse_confirm_actions("1") == frozenset(WRITE_ACTIONS)
    assert parse_confirm_actions("all") == frozenset(WRITE_ACTIONS)
    assert parse_confirm_actions("tasks") == frozenset({"task_add"})
    assert parse_confirm_actions("task_add,item_add") == frozenset({"task_add", "item_add"})
    # 不认识的工具名直接忽略，避免配错就全开
    assert parse_confirm_actions("unknown") == frozenset()


def test_tasks_only_confirms_tasks_not_items():
    llm = ScriptedLLM([
        _tool_call("item_add", {"name": "鸡蛋"}),
        _stop("记好了"),
    ])
    assistant = make_assistant(llm, confirm_write_actions="tasks")
    result = assistant.process_text("冰箱里还有三个鸡蛋", client_id="c1")
    # 只确认任务：物品直接写
    assert result["type"] != "confirm"
    assert len(assistant.items.list_all()) == 1

    llm2 = ScriptedLLM([
        _tool_call("task_add", {"title": "取快递", "due_at": "2026-11-23"}),
        _stop("好的"),
    ])
    assistant2 = make_assistant(llm2, confirm_write_actions="tasks")
    result2 = assistant2.process_text("明天提醒我取快递", client_id="c1")
    assert result2["type"] == "confirm"
    assert assistant2.tasks.list_all() == []
