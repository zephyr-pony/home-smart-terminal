"""写工具参数护栏测试：凭空补日期、凭空建任务都要被挡住。"""
import json
from datetime import date

from agent.clarify import Clarifier
from agent.guard import guard_write_args, is_grounded
from assistant import Assistant
from domain.facts import Facts
from domain.store import AgentStore
from domain.tasks import Tasks
from items import Items
from memory import Memory

TODAY = date(2026, 11, 21)


def test_item_expiry_dropped_without_date_phrase():
    """「冰箱里还有三个鸡蛋」不该产生到期日（实测模型会编出今天/12-21/12-31）。"""
    args, blocked = guard_write_args(
        "item_add", {"name": "鸡蛋", "expiry_date": "2026-11-21"},
        "冰箱里还有三个鸡蛋", TODAY,
    )
    assert blocked is None
    assert args["name"] == "鸡蛋"
    assert args["expiry_date"] is None


def test_item_expiry_replaced_by_parsed_date():
    args, blocked = guard_write_args(
        "item_add", {"name": "牛奶", "expiry_date": "2026-11-26"},
        "牛奶下周三过期", TODAY,
    )
    assert blocked is None
    # 模型算成了周四，以句子里解析出的日期为准
    assert args["expiry_date"] == "2026-11-25"


def test_item_expiry_kept_when_phrase_unparsed():
    """有日期线索但解析不了（如「月底」）时不贸然丢掉。"""
    args, _ = guard_write_args(
        "item_add", {"name": "牛奶", "expiry_date": "2026-11-30"},
        "牛奶月底过期", TODAY,
    )
    assert args["expiry_date"] == "2026-11-30"


def test_task_rejected_when_title_not_grounded():
    """「我家水费户号是多少」被模型当成 task_add 时要整次拒绝。"""
    args, blocked = guard_write_args(
        "task_add", {"title": "交电费", "due_at": "2026-11-22"},
        "我家水费户号是多少", TODAY,
    )
    assert blocked is not None
    assert "交电费" in blocked


def test_task_due_from_sentence():
    args, blocked = guard_write_args(
        "task_add", {"title": "交电费", "due_at": "2026-11-22"},
        "明天下午三点提醒我交电费", TODAY,
    )
    assert blocked is None
    assert args["due_at"] == "2026-11-22T15:00"


def test_task_due_dropped_without_date_phrase():
    args, blocked = guard_write_args(
        "task_add", {"title": "取快递", "due_at": "2026-11-22"},
        "记得取快递", TODAY,
    )
    assert blocked is None
    assert args["due_at"] is None


def test_is_grounded_basics():
    assert is_grounded("交电费", "明天下午三点提醒我交电费")
    assert is_grounded("牛奶", "冰箱里的牛奶快过期了")
    assert not is_grounded("交电费", "我家水费户号是多少")
    assert is_grounded("", "随便说点什么")


def _scripted(replies):
    class ScriptedLLM:
        def __init__(self, responses):
            self.responses = list(responses)
            self.calls = []

        def chat_with_tools(self, messages, tools, **kwargs):
            self.calls.append(messages)
            return self.responses.pop(0)

    return ScriptedLLM(replies)


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


def _assistant(llm):
    items = Items(":memory:")
    return Assistant(
        memory=Memory(":memory:"),
        items=items,
        llm=llm,
        clarifier=Clarifier(store=AgentStore(":memory:"), items=items),
        tasks=Tasks(":memory:"),
        facts=Facts(":memory:"),
    )


def test_agent_loop_drops_fabricated_expiry():
    llm = _scripted([
        _tool_call("item_add", {"name": "鸡蛋", "expiry_date": "2026-12-31"}),
        _stop("记好了"),
    ])
    assistant = _assistant(llm)
    assistant.process_text("冰箱里还有三个鸡蛋")
    rows = assistant.items.list_all()
    assert len(rows) == 1
    assert rows[0]["expiry_date"] is None


def test_agent_loop_blocks_fabricated_task():
    llm = _scripted([
        _tool_call("task_add", {"title": "交电费", "due_at": "2026-11-22"}),
        _stop("我没记过你家的水费户号"),
    ])
    assistant = _assistant(llm)
    result = assistant.process_text("我家水费户号是多少")
    assert assistant.tasks.list_all() == []
    assert "task_add" not in result["used_tools"]
