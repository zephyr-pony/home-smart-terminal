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
