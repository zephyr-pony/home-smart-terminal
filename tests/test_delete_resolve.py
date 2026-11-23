"""删除前把目标坐实：精确优先，相似项只作为提示，绝不静默删别的。"""
from agent.clarify import Clarifier
from assistant import Assistant
from domain.facts import Facts
from domain.store import AgentStore
from domain.tasks import Tasks
from items import Items
from memory import Memory


class ScriptedLLM:
    def __init__(self, responses):
        self.responses = list(responses)

    def chat_with_tools(self, messages, tools, **kwargs):
        return self.responses.pop(0)


def _tool_call(name, arguments, call_id="call-1"):
    return {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": _dump(arguments)},
                }],
            },
            "finish_reason": "tool_calls",
        }]
    }


def _stop(text="好的"):
    return {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}


def _dump(value):
    import json
    return json.dumps(value, ensure_ascii=False)


def make_assistant(responses, items=None):
    store = AgentStore(":memory:")
    items = items or Items(":memory:")
    return Assistant(
        memory=Memory(":memory:"),
        items=items,
        llm=ScriptedLLM(responses),
        clarifier=Clarifier(store=store, items=items),
        tasks=Tasks(":memory:"),
        facts=Facts(":memory:"),
    )


def test_exact_name_wins_over_similar():
    """清单里有「酸奶」和「酸奶机」，删「酸奶」只能动前者。"""
    items = Items(":memory:")
    items.add("酸奶")
    items.add("酸奶机")
    assistant = make_assistant([_tool_call("item_delete", {"name": "酸奶"})], items)

    result = assistant.process_text("删掉酸奶", client_id="c1")

    assert result["type"] == "confirm"
    assert "酸奶机" not in result["reply"]

    assistant.process_text("确认", client_id="c1")
    assert [row["name"] for row in items.list_all()] == ["酸奶机"]


def test_single_similar_candidate_is_named_not_assumed():
    """只有「酸奶机」时，确认句必须说出它的全名，让人有机会发现认错了。"""
    items = Items(":memory:")
    items.add("酸奶机")
    assistant = make_assistant([_tool_call("item_delete", {"name": "酸奶"})], items)

    result = assistant.process_text("删掉酸奶", client_id="c1")

    assert result["type"] == "confirm"
    assert "酸奶机" in result["reply"]

    # 不确认就不会删
    assert items.count() == 1


def test_multiple_candidates_are_listed_and_nothing_is_deleted():
    items = Items(":memory:")
    items.add("酸奶大瓶")
    items.add("酸奶小瓶")
    assistant = make_assistant([_tool_call("item_delete", {"name": "酸奶"})], items)

    result = assistant.process_text("删掉酸奶", client_id="c1")

    assert result["type"] == "chat"
    assert "酸奶大瓶" in result["reply"] and "酸奶小瓶" in result["reply"]
    assert items.count() == 2


def test_missing_item_says_not_found_without_offering_lookalikes():
    items = Items(":memory:")
    items.add("酸奶机")
    assistant = make_assistant([_tool_call("item_delete", {"name": "牛奶"})], items)

    result = assistant.process_text("删掉牛奶", client_id="c1")

    assert result["type"] == "chat"
    assert "没有找到" in result["reply"]
    assert "酸奶机" not in result["reply"]
    assert items.count() == 1


def test_memory_delete_without_hits_is_not_confirmed():
    assistant = make_assistant([_tool_call("memory_delete", {"keywords": "酸奶"})])

    result = assistant.process_text("忘掉酸奶", client_id="c1")

    assert result["type"] == "chat"
    assert "没有找到" in result["reply"]
