"""夜间画像提炼（D84）：产物只进 proposals，且证据必须是用户原话。"""
import json
from datetime import datetime, timedelta

from agent.profile_miner import ProfileMiner
from domain.dialogue import DialogueStore
from domain.facts import Facts
from domain.store import AgentStore


class FakeLLM:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append(list(messages))
        content = self.payload
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        return {"choices": [{"message": {"content": content}}]}


def _turns(store, lines, days=0):
    at = (datetime.now() - timedelta(days=days)).isoformat()
    for text in lines:
        store.add("web:1", "user", text, at=at)


def make_miner(payload, lines=None):
    dialogue = DialogueStore(":memory:", enabled=True)
    _turns(dialogue, lines or [
        "今天好冷啊，手脚都是凉的",
        "把空调调到 24 度吧",
        "我不吃辣，别放辣椒",
    ])
    store = AgentStore(":memory:")
    facts = Facts(":memory:")
    proposals = SimpleProposals(store, facts)
    return ProfileMiner(dialogue, proposals, FakeLLM(payload)), store, facts


class SimpleProposals:
    """只实现提炼用到的两样：记候选、查已写好的画像。"""

    def __init__(self, store, facts):
        self.store = store
        self.facts = facts

    def record_profile_proposal(self, payload, note=""):
        self.store.record_proposal("profile_set", payload, "proposed", note)


def test_records_candidate_with_evidence():
    miner, store, _facts = make_miner([
        {"predicate": "怕冷", "value": "冬天室温要 24 度以上",
         "evidence": "今天好冷啊，手脚都是凉的"},
    ])

    result = miner.run()

    assert result["candidates"] == 1
    rows = store.list_proposals(10, "proposed", "profile_set")
    assert rows[0]["payload"]["predicate"] == "怕冷"
    assert rows[0]["payload"]["subject"] == "用户"
    assert rows[0]["status"] == "proposed"


def test_drops_candidate_whose_evidence_is_not_in_transcript():
    """模型最容易编的就是证据：对不上原话就整条丢掉（宁可少记）。"""
    miner, store, _facts = make_miner([
        {"predicate": "怕冷", "value": "冬天室温要 24 度以上",
         "evidence": "你们家冬天从来不关暖气"},
        {"predicate": "口味", "value": "不吃辣", "evidence": "我不吃辣"},
    ])

    result = miner.run()

    assert result["candidates"] == 1
    assert result["skipped"] == 1
    payloads = [row["payload"] for row in store.list_proposals(10)]
    assert [item["predicate"] for item in payloads] == ["口味"]


def test_drops_candidate_without_evidence():
    """没写证据不能算「对得上」：模型偷懒时整条丢掉，别让防编造空转。"""
    miner, store, _facts = make_miner([
        {"predicate": "怕冷", "value": "冬天室温要 24 度以上", "evidence": ""},
        {"predicate": "口味", "value": "不吃辣", "evidence": "我不吃辣"},
    ])

    result = miner.run()

    assert result["candidates"] == 1
    assert result["skipped"] == 1
    payloads = [row["payload"] for row in store.list_proposals(10)]
    assert [item["predicate"] for item in payloads] == ["口味"]


def test_skips_when_too_few_turns():
    dialogue = DialogueStore(":memory:", enabled=True)
    _turns(dialogue, ["今天好冷"])
    llm = FakeLLM([])
    miner = ProfileMiner(dialogue, SimpleProposals(AgentStore(":memory:"), Facts(":memory:")), llm)

    result = miner.run()

    assert result["turns"] == 1
    assert result["candidates"] == 0
    assert llm.calls == []          # 不值得为两句话花一次模型调用


def test_skips_already_known_fact():
    miner, store, facts = make_miner([
        {"predicate": "口味", "value": "不吃辣", "evidence": "我不吃辣"},
    ])
    facts.store("用户", "口味", "不吃辣")

    result = miner.run()

    assert result["candidates"] == 0
    assert store.list_proposals(10) == []


def test_skips_duplicate_candidate():
    miner, store, _facts = make_miner([
        {"predicate": "口味", "value": "不吃辣", "evidence": "我不吃辣"},
    ])
    miner.run()
    # 第二天又提炼出同一条：不再重复提，免得天天问
    again = miner.run()

    assert again["candidates"] == 0
    assert len(store.list_proposals(10)) == 1


def test_tolerates_model_chatter_and_code_fence():
    payload = '好的，我先看一下：\n```json\n[{"predicate": "口味", "value": "不吃辣", "evidence": "我不吃辣"}]\n```'
    miner, store, _facts = make_miner(payload)

    assert miner.run()["candidates"] == 1
    assert store.list_proposals(10)[0]["payload"]["predicate"] == "口味"


def test_bad_json_does_not_raise():
    miner, store, _facts = make_miner("我觉得他怕冷")

    assert miner.run()["candidates"] == 0
    assert store.list_proposals(10) == []
