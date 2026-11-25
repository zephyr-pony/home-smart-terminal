"""热点上下文（D58）：画像/常用事实/最近记忆冻结快照注入 system prompt。"""
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
