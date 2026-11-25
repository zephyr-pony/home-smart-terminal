"""画像（D59）：模型推断只提议，用户点头才落库，全程留审计。"""
import json

from agent.clarify import Clarifier
from assistant import Assistant
from domain.facts import Facts, profile_subject
from domain.store import AgentStore
from domain.tasks import Tasks
from items import Items
from memory import Memory


class ScriptedLLM:
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


def make_assistant(llm, confirm_write_actions=frozenset()):
    items = Items(":memory:")
    assistant = Assistant(
        memory=Memory(":memory:"),
        items=items,
        llm=llm,
        clarifier=Clarifier(store=AgentStore(":memory:"), items=items),
        tasks=Tasks(":memory:"),
        facts=Facts(":memory:"),
    )
    assistant.confirm_write_actions = confirm_write_actions
    return assistant


def _profile_call(**overrides):
    args = {
        "subject": "用户",
        "predicate": "口味",
        "value": "偏清淡",
        "evidence": "连着几次说想吃清淡点",
    }
    args.update(overrides)
    return _tool_call("profile_set", args)


def _proposals(assistant, action_type="profile_set"):
    return [
        row for row in assistant.proposals.store.list_proposals(50)
        if row["action_type"] == action_type
    ]


def test_profile_subject_normalizes_to_prefix():
    assert profile_subject("") == "用户"
    assert profile_subject("我") == "用户"
    assert profile_subject("妈妈") == "用户/妈妈"
    assert profile_subject("用户/妈妈") == "用户/妈妈"


def test_model_proposal_writes_nothing_but_records_audit():
    llm = ScriptedLLM([_profile_call()])
    assistant = make_assistant(llm)

    result = assistant.process_text("你是不是觉得我口味偏淡", client_id="c1")

    assert result["type"] == "confirm"
    assert "我猜" in result["reply"]
    assert "偏清淡" in result["reply"]
    # 还没点头，一条都不许落库
    assert assistant.facts.search(subject="用户") == []

    rows = _proposals(assistant)
    assert [row["status"] for row in rows] == ["proposed"]


def test_confirmation_writes_profile_and_audits_committed():
    llm = ScriptedLLM([_profile_call()])
    assistant = make_assistant(llm)
    assistant.process_text("你是不是觉得我口味偏淡", client_id="c1")

    result = assistant.process_text("确认", client_id="c1")

    facts = assistant.facts.search(subject="用户")
    assert len(facts) == 1
    assert facts[0]["predicate"] == "口味"
    assert facts[0]["value"] == "偏清淡"
    assert "模型推断" in (facts[0]["source"] or "")
    assert result["type"] == "record"
    assert result["domain"] == "profile"
    assert "偏清淡" in result["reply"]

    statuses = [row["status"] for row in _proposals(assistant)]
    assert "proposed" in statuses
    assert "committed" in statuses


def test_cancel_writes_nothing_and_audits_cancelled():
    llm = ScriptedLLM([_profile_call()])
    assistant = make_assistant(llm)
    assistant.process_text("你是不是觉得我口味偏淡", client_id="c1")

    result = assistant.process_text("取消", client_id="c1")

    assert assistant.facts.search(subject="用户") == []
    assert result["action"] == "cancel"
    statuses = [row["status"] for row in _proposals(assistant)]
    assert "committed" not in statuses
    assert "cancelled" in statuses


def test_profile_always_confirms_even_when_weak_mode_off():
    """画像不是弱模型开关：CONFIRM_WRITE_ACTIONS=0 也必须问。"""
    llm = ScriptedLLM([_profile_call()])
    assistant = make_assistant(llm, confirm_write_actions=False)

    result = assistant.process_text("你是不是觉得我口味偏淡", client_id="c1")

    assert result["type"] == "confirm"
    assert assistant.facts.search(subject="用户") == []


def test_other_person_goes_under_user_prefix():
    llm = ScriptedLLM([_profile_call(subject="妈妈", predicate="作息", value="早睡")])
    assistant = make_assistant(llm)
    assistant.process_text("我妈好像睡得挺早", client_id="c1")
    assistant.process_text("确认", client_id="c1")

    facts = assistant.facts.search(subject="用户/妈妈")
    assert len(facts) == 1
    assert facts[0]["value"] == "早睡"


def test_missing_value_asks_instead_of_proposing():
    llm = ScriptedLLM([_tool_call("profile_set", {"subject": "用户", "predicate": "口味"})])
    assistant = make_assistant(llm)

    result = assistant.process_text("你猜我口味", client_id="c1")

    assert result["type"] == "chat"
    assert result["reply"].startswith("要记的是")
    assert assistant.confirmations.get_pending("c1") is None
    assert _proposals(assistant) == []


def test_confirmed_profile_lands_in_hot_context():
    llm = ScriptedLLM([_profile_call()])
    assistant = make_assistant(llm)
    assistant.process_text("你是不是觉得我口味偏淡", client_id="c1")
    assistant.process_text("确认", client_id="c1")

    # 新会话（快照重建）才注入；同一会话按 D58 保持冻结
    text = assistant._hot_context("c2")
    assert "用户画像" in text
    assert "偏清淡" in text


def test_duplicate_profile_is_not_written_twice():
    llm = ScriptedLLM([_profile_call(), _profile_call()])
    assistant = make_assistant(llm)
    assistant.process_text("你是不是觉得我口味偏淡", client_id="c1")
    assistant.process_text("确认", client_id="c1")

    assistant.process_text("你是不是觉得我口味偏淡", client_id="c1")
    assistant.process_text("确认", client_id="c1")

    assert len(assistant.facts.search(subject="用户")) == 1


def test_profile_tool_registered():
    assistant = make_assistant(ScriptedLLM([]))
    names = [t["schema"]["function"]["name"] for t in assistant.tools]
    assert "profile_set" in names
    assert "fact_store" in names
