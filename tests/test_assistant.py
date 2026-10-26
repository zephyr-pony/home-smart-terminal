import json

from assistant import Assistant
from items import Items
from memory import Memory


class FakeLLM:
    """默认返回一句闲聊；记录收到的 messages 供断言。"""

    def __init__(self, reply="好的"):
        self.reply = reply
        self.calls = []

    def chat_with_tools(self, messages, tools, **kwargs):
        self.calls.append(messages)
        return _stop_reply(self.reply)


class ScriptedLLM:
    """按预设顺序返回响应，用于模拟工具调用回合。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat_with_tools(self, messages, tools, **kwargs):
        self.calls.append(messages)
        return self.responses.pop(0)


class FailingLLM:
    def __init__(self, error=None):
        self.error = error or RuntimeError("boom")

    def chat_with_tools(self, messages, tools, **kwargs):
        raise self.error


def make_assistant(llm=None):
    return Assistant(
        memory=Memory(":memory:"),
        items=Items(":memory:"),
        llm=llm or FakeLLM(),
    )


def _tool_call_reply(name, arguments, call_id="call-1"):
    return {
        "choices": [{
            "message": {
                "content": "",
                "tool_calls": [{
                    "id": call_id,
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }],
            },
            "finish_reason": "tool_calls",
        }]
    }


def _stop_reply(content):
    return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}


def test_tool_metadata():
    assert Assistant._tool_metadata(["item_search", "get_current_date"]) == ("items", "search")
    assert Assistant._tool_metadata(["memory_store"]) == ("memory", "store")
    assert Assistant._tool_metadata([]) == ("chat", "chat")


def test_session_ttl_and_limit():
    assistant = make_assistant()
    assistant.session_ttl_seconds = 0
    assistant._record_turn("old", "hello", "hi")
    assert assistant.get_history("old") == []

    assistant.session_ttl_seconds = 3600
    assistant.max_sessions = 1
    assistant._record_turn("first", "a", "b")
    assistant._record_turn("second", "c", "d")
    assert assistant.get_history("first") == []
    assert assistant.get_history("second") == [("c", "d")]
    assistant._record_turn("", "ignored", "ignored")
    assert "" not in assistant.sessions


def test_empty_result_has_structured_metadata():
    result = make_assistant().process_text("")
    assert result["type"] == "chat"
    assert result["domain"] == "chat"
    assert result["action"] == "chat"
    assert result["used_tools"] == []


def test_agent_loop_injects_session_history():
    """多轮上下文：同 client_id 的历史轮次要进 agent 的 messages。"""
    assistant = make_assistant()
    assistant._record_turn("c1", "我买了酸奶", "已记录")

    assistant.process_text("那牛奶呢", client_id="c1")

    messages = assistant.llm.calls[-1]
    pairs = [(m["role"], m["content"]) for m in messages]
    assert ("user", "我买了酸奶") in pairs
    assert ("assistant", "已记录") in pairs
    # 本轮问题必须排在最后
    assert messages[-1] == {"role": "user", "content": "那牛奶呢"}
    # 系统提示在最前
    assert messages[0]["role"] == "system"


def test_agent_loop_without_client_id_has_no_history():
    """无 client_id 时退化为单轮：messages 只有 system + 本轮。"""
    assistant = make_assistant()
    assistant._record_turn("c1", "我买了酸奶", "已记录")

    assistant.process_text("你好")

    messages = assistant.llm.calls[-1]
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1] == {"role": "user", "content": "你好"}


def test_context_messages_clips_long_history():
    assistant = make_assistant()
    assistant.session_context_max_chars = 4
    assistant._record_turn("c1", "很长的用户输入文本", "很长的助手回复内容")
    messages = assistant._context_messages("c1")
    assert messages[0]["content"] == "很长的用…"
    assert messages[1]["content"] == "很长的助…"


def test_agent_loop_returns_record_summary():
    assistant = make_assistant(ScriptedLLM([
        _tool_call_reply(
            "memory_store",
            {"text": "明天交电费", "summary": "8月9日交电费"},
        ),
        _stop_reply("记住了"),
    ]))
    result = assistant.process_text("明天交电费")

    assert result["type"] == "record"
    assert result["reply"] == "记住了"
    assert result["summary"] == "8月9日交电费"


def test_agent_loop_returns_search_memories():
    assistant = make_assistant(ScriptedLLM([
        _tool_call_reply("memory_search", {"keywords": "酸奶 过期"}),
        _stop_reply("酸奶8月5号过期"),
    ]))
    assistant.memory.store("酸奶8月5号过期", "酸奶8月5号过期")

    result = assistant.process_text("酸奶什么时候过期")

    assert result["type"] == "query"
    assert result["memories"]
    assert "酸奶" in result["memories"][0]


def test_agent_loop_degrades_when_llm_fails():
    result = make_assistant(FailingLLM()).process_text("你好")

    assert result["type"] == "chat"
    assert result["domain"] == "chat"
    assert result["action"] == "chat"
    assert result["used_tools"] == []
    assert "抱歉" in result["reply"]


def test_agent_loop_reports_unknown_tool_without_crashing():
    assistant = make_assistant(ScriptedLLM([
        _tool_call_reply("no_such_tool", {}),
        _stop_reply("好的"),
    ]))
    result = assistant.process_text("你好")

    assert result["reply"] == "好的"
    tool_messages = [
        m["content"] for m in assistant.llm.calls[-1] if m.get("role") == "tool"
    ]
    assert tool_messages == ["错误：未知工具 no_such_tool"]
