"""写工具参数护栏测试：凭空补日期、凭空建任务都要被挡住。"""
import json
from datetime import date, timedelta

from agent.clarify import Clarifier
from agent.guard import guard_false_claim, guard_write_args, is_grounded
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

def test_task_update_title_rejected_when_not_grounded():
    args, blocked = guard_write_args(
        "task_update", {"task_id": 1, "title": "交水费"},
        "把那个提醒改到明天", TODAY,
    )
    assert blocked is not None
    assert "交水费" in blocked


def test_task_update_due_from_sentence():
    args, blocked = guard_write_args(
        "task_update", {"task_id": 1, "due_at": "2026-11-30"},
        "取快递改到明天下午三点", TODAY,
    )
    assert blocked is None
    assert args["due_at"] == "2026-11-22T15:00"


def test_task_update_without_due_keeps_other_fields():
    args, blocked = guard_write_args(
        "task_update", {"task_id": 1, "title": "取快递"},
        "取快递改个名字叫取快递包裹", TODAY,
    )
    assert blocked is None
    assert "due_at" not in args

def test_postpone_target_date_from_phrase():
    """「明天」「三天后」「下周一」是目标日期，不是「在到期日上加几天」。"""
    args, blocked = guard_write_args(
        "task_postpone", {"task_id": 1}, "这个提醒明天再说", TODAY
    )
    assert blocked is None
    assert args["due_at"] == "2026-11-22"

    args, _ = guard_write_args(
        "task_postpone", {"task_id": 1}, "三天后再提醒我", TODAY
    )
    assert args["due_at"] == "2026-11-24"

    # 2026-11-21 是周六，下周一 = 11-23
    args, _ = guard_write_args(
        "task_postpone", {"task_id": 1}, "下周一再提醒我", TODAY
    )
    assert args["due_at"] == "2026-11-23"


def test_postpone_without_phrase_defaults_to_one():
    args, blocked = guard_write_args(
        "task_postpone", {"task_id": 1}, "这个先往后放放", TODAY
    )
    assert blocked is None
    assert args["days"] == 1


def test_postpone_ignores_model_days_when_phrase_present():
    """模型自己算的天数不如原话可靠，有原话时以原话为准。"""
    args, _ = guard_write_args(
        "task_postpone", {"task_id": 1, "days": 30}, "明天再说", TODAY
    )
    assert args["due_at"] == "2026-11-22"
    assert "days" not in args

def test_agent_postpone_uses_phrase_not_model_days():
    """模型传了 days=30，但用户说的是「明天再说」，实际应只顺延一天。"""
    llm = _scripted([
        _tool_call("task_list", {}),
        _tool_call("task_postpone", {"task_id": 1, "days": 30}),
        _stop("好的，明天再说"),
    ])
    assistant = _assistant(llm)
    assistant.tasks.add("交电费", (date.today() + timedelta(days=1)).isoformat(), "bill")

    assistant.process_text("这个提醒明天再说", client_id="c1")

    task = assistant.tasks.list_all()[0]
    # 用户说的是「明天」，就该改成明天，而不是「原到期日再 +1」
    expected = (date.today() + timedelta(days=1)).isoformat()
    assert task["due_at"].startswith(expected)
    assert task["status"] == "pending"

def test_guard_catches_fake_device_claim():
    """没调 home_control 却回「已经把灯关了」：设备根本没动。"""
    reply, replaced = guard_false_claim("把客厅灯关了", [], "已经把客厅灯关了")

    assert replaced is True
    assert "设备没动" in reply


def test_guard_keeps_device_reply_when_tool_ran():
    reply, replaced = guard_false_claim(
        "把客厅灯关了", ["home_control"], "已经把客厅灯关了"
    )

    assert replaced is False
    assert reply == "已经把客厅灯关了"