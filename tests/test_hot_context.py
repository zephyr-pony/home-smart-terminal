"""热点上下文（D58）：画像/常用事实/最近记忆冻结快照注入 system prompt。"""
import json

from agent.clarify import Clarifier
from assistant import Assistant
from domain.facts import Facts
from domain.store import AgentStore
from domain.tasks import Tasks
from items import Items
from memory import Memory


class RecordingLLM:
    def __init__(self):
        self.messages = None

    def chat_with_tools(self, messages, tools=None, **kwargs):
        self.messages = messages
        return {
            "choices": [{
                "message": {"content": "好", "tool_calls": []},
                "finish_reason": "stop",
            }]
        }


class ScriptedLLM:
    """按顺序返回预设响应，用来走一遍「模型调写工具」的真实链路。"""

    def __init__(self, responses):
        self.responses = list(responses)

    def chat_with_tools(self, messages, tools=None, **kwargs):
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


def _stop(content="好"):
    return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}


def make_assistant(llm=None):
    store = AgentStore(":memory:")
    items = Items(":memory:")
    return Assistant(
        memory=Memory(":memory:"), llm=llm or RecordingLLM(), items=items,
        clarifier=Clarifier(store=store, items=items),
        tasks=Tasks(":memory:"), facts=Facts(":memory:"),
    )


def test_injects_profile_facts_and_memories():
    assistant = make_assistant()
    assistant.facts.store("用户/老王", "偏好", "回答简短")
    assistant.facts.store("电费", "户号", "123456")
    assistant.memory.store("冰箱里还有三个鸡蛋", "冰箱 三个鸡蛋")

    text = assistant._hot_context("cli")

    assert "用户画像" in text
    assert "回答简短" in text
    assert "常用事实" in text
    assert "123456" in text
    assert "最近记忆" in text
    assert "三个鸡蛋" in text


def test_frozen_within_session_but_new_session_sees_updates():
    assistant = make_assistant()
    assistant.memory.store("第一条记忆", "第一条")

    first = assistant._hot_context("cli")
    assistant.memory.store("第二条记忆", "第二条")
    second = assistant._hot_context("cli")

    # 同一会话里冻结：新写入不改变快照
    assert second == first
    assert "第二条" not in second
    # 新会话（不同 client_id）能看到新记忆
    assert "第二条" in assistant._hot_context("other")


def test_respects_char_budget():
    assistant = make_assistant()
    assistant.hot_context_max_chars = 80
    for index in range(20):
        assistant.facts.store("设备%d" % index, "型号", "型号X%d" % index)

    text = assistant._hot_context("cli")
    assert len(text) <= 80
    # 至少塞进去一部分，不是直接空
    assert "常用事实" in text


def test_can_be_disabled():
    assistant = make_assistant()
    assistant.hot_context_enabled = False
    assert assistant._hot_context("cli") == ""


def test_empty_store_injects_nothing():
    assistant = make_assistant()
    assert assistant._hot_context("cli") == ""


def test_hot_context_lands_in_system_prompt():
    llm = RecordingLLM()
    assistant = make_assistant(llm)
    assistant.facts.store("电费", "户号", "123456")

    assistant.process_text("你好", "cli")

    assert llm.messages is not None
    system = llm.messages[0]
    assert system["role"] == "system"
    assert "123456" in system["content"]
    assert "【已知信息】" in system["content"]


def test_hot_context_is_not_injected_when_disabled():
    llm = RecordingLLM()
    assistant = make_assistant(llm)
    assistant.hot_context_enabled = False
    assistant.facts.store("电费", "户号", "123456")

    assistant.process_text("你好", "cli")

    assert "123456" not in llm.messages[0]["content"]


def test_write_invalidates_frozen_snapshot():
    """改完事实立刻再问，不能还念旧值。

    旧值本来就在系统提示里，六位以上数字的出处校验会认为它「有出处」而放行，
    所以只能靠写库之后作废快照（`_invalidate_hot_contexts`）。
    """
    llm = ScriptedLLM([
        _tool_call("fact_store", {
            "subject": "冰箱", "predicate": "品牌", "value": "海尔",
        }),
        _stop("好的，换成海尔了。"),
    ])
    assistant = make_assistant(llm)
    assistant.facts.store("冰箱", "品牌", "西门子")
    assert "西门子" in assistant._hot_context("cli")

    assistant.process_text("冰箱品牌换成海尔", "cli")

    text = assistant._hot_context("cli")
    assert "海尔" in text
    assert "西门子" not in text


def test_confirmation_invalidates_frozen_snapshot():
    """确认类写入（画像）也要让快照失效，不能等 TTL 到期。"""
    llm = ScriptedLLM([
        _tool_call("profile_set", {
            "subject": "用户", "predicate": "口味", "value": "偏清淡",
            "evidence": "连着几次说想吃清淡点",
        }),
        _stop("是这么觉得。"),
    ])
    assistant = make_assistant(llm)
    assistant.process_text("你是不是觉得我口味偏淡", "cli")
    assert "偏清淡" not in assistant._hot_context("cli")

    assistant.process_text("确认", "cli")

    assert "偏清淡" in assistant._hot_context("cli")
