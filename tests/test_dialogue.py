"""对话记录（D84）：只留最近几天，给跨天上下文和夜间画像提炼当原料。"""
import os
from datetime import datetime, timedelta

from agent.clarify import Clarifier
from assistant import Assistant
from domain.dialogue import DialogueStore
from domain.facts import Facts
from domain.store import AgentStore
from domain.tasks import Tasks
from items import Items
from memory import Memory


def test_records_turns_and_recent_order():
    store = DialogueStore(":memory:", keep_days=7, enabled=True)
    assert store.add("web:1", "user", "明天要交电费")
    assert store.add("web:1", "assistant", "记下了，明天早上提醒你")

    rows = store.recent(10)
    assert [row["role"] for row in rows] == ["assistant", "user"]
    assert rows[1]["text"] == "明天要交电费"
    assert store.count() == 2


def test_skips_empty_text_and_unknown_roles():
    store = DialogueStore(":memory:", enabled=True)
    assert store.add("web:1", "system", "系统提示不该进对话记录") is False
    assert store.add("web:1", "user", "   ") is False
    assert store.count() == 0


def test_purge_drops_turns_past_keep_days():
    store = DialogueStore(":memory:", keep_days=7, enabled=True)
    old = (datetime.now() - timedelta(days=9)).isoformat()
    store.add("web:1", "user", "九天前的一句", at=old)
    store.add("web:1", "user", "今天的一句")

    assert store.purge() == 1
    assert [row["text"] for row in store.recent(5)] == ["今天的一句"]


def test_clear_removes_everything():
    store = DialogueStore(":memory:", enabled=True)
    store.add("web:1", "user", "一")
    store.add("web:1", "user", "二")

    assert store.clear() == 2
    assert store.count() == 0


def test_disabled_store_creates_no_file(tmp_path):
    """关掉时连库文件都不该建：用户关的是「保存」，不是「藏起来」。"""
    path = str(tmp_path / "dialogue.db")
    store = DialogueStore(path, enabled=False)

    assert store.enabled is False
    assert store.add("web:1", "user", "你好") is False
    assert store.count() == 0
    assert store.clear() == 0
    assert os.path.exists(path) is False


class FakeLLM:
    def chat_with_tools(self, messages, tools, **kwargs):
        return {"choices": [{"message": {"content": "好的"}, "finish_reason": "stop"}]}


def test_assistant_records_both_sides_of_a_turn():
    """记录只写在编排层：网页、CLI、/v1、MCP 最后都走 process_text。"""
    dialogue = DialogueStore(":memory:", enabled=True)
    assistant = Assistant(
        memory=Memory(":memory:"),
        llm=FakeLLM(),
        items=Items(":memory:"),
        clarifier=Clarifier(store=AgentStore(":memory:"), items=Items(":memory:")),
        tasks=Tasks(":memory:"),
        facts=Facts(":memory:"),
        dialogue=dialogue,
    )

    assistant.process_text("今天好冷", "web:1")

    rows = dialogue.recent(10)
    assert [row["role"] for row in rows] == ["assistant", "user"]
    assert rows[1]["text"] == "今天好冷"
    assert rows[0]["text"] == "好的"
    assert rows[0]["client_id"] == "web:1"
