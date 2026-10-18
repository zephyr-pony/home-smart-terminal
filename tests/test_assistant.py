from assistant import Assistant
from items import Items
from memory import Memory


class FakeLLM:
    pass


def make_assistant():
    return Assistant(memory=Memory(":memory:"), items=Items(":memory:"), llm=FakeLLM())


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


def test_agent_loop_injects_session_history(monkeypatch):
    """多轮上下文：同 client_id 的历史轮次要进 agent 的 messages。"""
    assistant = make_assistant()
    assistant._record_turn("c1", "我买了酸奶", "已记录")

    captured = {}

    def fake_chat(messages, tools, **kwargs):
        captured["messages"] = messages
        return {"choices": [{"message": {"content": "好的"}, "finish_reason": "stop"}]}

    monkeypatch.setattr("assistant._chat_with_tools", fake_chat)
    assistant.process_text("那牛奶呢", client_id="c1")

    pairs = [(m["role"], m["content"]) for m in captured["messages"]]
    assert ("user", "我买了酸奶") in pairs
    assert ("assistant", "已记录") in pairs
    # 本轮问题必须排在最后
    assert captured["messages"][-1] == {"role": "user", "content": "那牛奶呢"}
    # 系统提示在最前
    assert captured["messages"][0]["role"] == "system"


def test_agent_loop_without_client_id_has_no_history(monkeypatch):
    """无 client_id 时退化为单轮：messages 只有 system + 本轮。"""
    assistant = make_assistant()
    assistant._record_turn("c1", "我买了酸奶", "已记录")

    captured = {}

    def fake_chat(messages, tools, **kwargs):
        captured["messages"] = messages
        return {"choices": [{"message": {"content": "好的"}, "finish_reason": "stop"}]}

    monkeypatch.setattr("assistant._chat_with_tools", fake_chat)
    assistant.process_text("你好")

    assert len(captured["messages"]) == 2
    assert captured["messages"][0]["role"] == "system"
    assert captured["messages"][1] == {"role": "user", "content": "你好"}


def test_context_messages_clips_long_history():
    assistant = make_assistant()
    assistant.session_context_max_chars = 4
    assistant._record_turn("c1", "很长的用户输入文本", "很长的助手回复内容")
    messages = assistant._context_messages("c1")
    assert messages[0]["content"] == "很长的用…"
    assert messages[1]["content"] == "很长的助…"
