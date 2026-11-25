"""记忆蒸馏（D60）：合并同主题摘要，原文一条都不删。"""
from datetime import datetime

from agent.distill import (
    MemoryDistiller,
    _bigrams,
    distillation_due,
    group_similar,
)
from memory import Memory


class FakeLLM:
    def __init__(self, reply="牛奶下周三过期，后来喝完了"):
        self.reply = reply
        self.calls = []

    def chat(self, messages, temperature=0.3, max_tokens=256):
        self.calls.append(messages)
        if isinstance(self.reply, Exception):
            raise self.reply
        return {"choices": [{"message": {"content": self.reply}}]}


def _seed(memory, entries, day=1):
    """entries: [(原文, 摘要)]，越靠后越新；day 用来把后加的一批排到更晚。"""
    for index, (text, summary) in enumerate(entries):
        memory.store(text, summary)
        with memory._lock:
            memory.conn.execute(
                "UPDATE memories_raw SET timestamp=? WHERE text=?",
                ("2026-11-%02dT10:00:00" % (day + index), text),
            )
            memory.conn.commit()


def _by_text(memory, text):
    return next(row for row in memory.list_all(50) if row["text"] == text)


def test_bigrams_ignore_dates_and_filler():
    grams = _bigrams("明天交电费 11月26日之前")
    assert "电费" in grams
    assert "明天" not in grams      # 停用词
    assert "1月" not in grams       # 日期词不参与聚类
    assert "26" not in grams


def test_group_similar_keeps_unrelated_days_apart():
    rows = [
        {"id": 3, "text": "明天取快递"},
        {"id": 2, "text": "明天交电费"},
        {"id": 1, "text": "冰箱里还有三个鸡蛋"},
    ]
    groups = group_similar(rows)
    # 「明天」不算主题词，三条各自独立
    assert groups == []


def test_group_similar_groups_same_topic():
    rows = [
        {"id": 2, "text": "牛奶喝完了"},
        {"id": 1, "text": "牛奶下周三过期"},
    ]
    groups = group_similar(rows)
    assert len(groups) == 1
    assert [row["id"] for row in groups[0]] == [2, 1]


def test_distill_writes_merged_summary_to_newest_only():
    memory = Memory(":memory:")
    _seed(memory, [
        ("牛奶下周三过期", "牛奶过期"),
        ("牛奶喝完了", "牛奶喝完"),
    ])
    llm = FakeLLM("牛奶下周三过期，后来喝完了")

    result = MemoryDistiller(memory, llm).run()

    assert result["updated"] == 1
    newest = _by_text(memory, "牛奶喝完了")
    older = _by_text(memory, "牛奶下周三过期")
    assert newest["summary"] == "牛奶下周三过期，后来喝完了"
    # 老条目原样保留：摘要、原文都不动
    assert older["summary"] == "牛奶过期"
    assert older["text"] == "牛奶下周三过期"
    # 一条都没删
    assert memory.count() == 2


def test_merged_summary_is_searchable():
    memory = Memory(":memory:")
    _seed(memory, [
        ("牛奶下周三过期", "牛奶过期"),
        ("牛奶喝完了", "牛奶喝完"),
    ])
    MemoryDistiller(memory, FakeLLM("牛奶之前买的，到下周三过期")).run()

    hits = memory.search("之前买")
    assert len(hits) == 1
    assert "牛奶喝完了" in hits[0]


def test_unrelated_group_is_logged_and_skipped():
    memory = Memory(":memory:")
    _seed(memory, [
        ("牛奶下周三过期", "牛奶过期"),
        ("牛奶喝完了", "牛奶喝完"),
    ])
    distiller = MemoryDistiller(memory, FakeLLM("无关"))

    result = distiller.run()

    assert result["updated"] == 0
    assert _by_text(memory, "牛奶喝完了")["summary"] == "牛奶喝完"
    groups = group_similar(memory.list_all(50))
    from agent.distill import _signature
    assert memory.is_distilled(_signature(groups[0]))


def test_llm_failure_keeps_summaries_and_retries_next_time():
    memory = Memory(":memory:")
    _seed(memory, [
        ("牛奶下周三过期", "牛奶过期"),
        ("牛奶喝完了", "牛奶喝完"),
    ])
    llm = FakeLLM(RuntimeError("网络断了"))
    distiller = MemoryDistiller(memory, llm)

    result = distiller.run()

    assert result["updated"] == 0
    assert _by_text(memory, "牛奶喝完了")["summary"] == "牛奶喝完"
    # 失败不记指纹，下一轮还会重试
    groups = group_similar(memory.list_all(50))
    from agent.distill import _signature
    assert not memory.is_distilled(_signature(groups[0]))


def test_same_group_is_not_asked_twice():
    memory = Memory(":memory:")
    _seed(memory, [
        ("牛奶下周三过期", "牛奶过期"),
        ("牛奶喝完了", "牛奶喝完"),
    ])
    llm = FakeLLM()
    distiller = MemoryDistiller(memory, llm)

    distiller.run()
    distiller.run()

    assert len(llm.calls) == 1


def test_new_member_reopens_the_group():
    memory = Memory(":memory:")
    _seed(memory, [
        ("牛奶下周三过期", "牛奶过期"),
        ("牛奶喝完了", "牛奶喝完"),
    ])
    llm = FakeLLM()
    distiller = MemoryDistiller(memory, llm)
    distiller.run()

    _seed(memory, [("又买了一箱牛奶", "新买牛奶")], day=3)
    distiller.run()

    assert len(llm.calls) == 2
    # 最新那条拿到新的合并摘要
    assert _by_text(memory, "又买了一箱牛奶")["summary"].startswith("牛奶")


def test_update_summary_keeps_text_and_time():
    memory = Memory(":memory:")
    _seed(memory, [("牛奶下周三过期", "牛奶过期")])
    before = memory.list_all(5)[0]

    assert memory.update_summary(before["id"], "牛奶 11月26日到期") is True

    after = memory.list_all(5)[0]
    assert after["text"] == before["text"]
    assert after["time"] == before["time"]
    assert after["summary"] == "牛奶 11月26日到期"


def test_update_summary_rejects_empty_or_missing():
    memory = Memory(":memory:")
    _seed(memory, [("牛奶下周三过期", "牛奶过期")])
    memory_id = memory.list_all(5)[0]["id"]

    assert memory.update_summary(memory_id, "  ") is False
    assert memory.update_summary(999999, "随便") is False
    assert _by_text(memory, "牛奶下周三过期")["summary"] == "牛奶过期"


def test_distillation_due_nightly_and_threshold():
    nightly = datetime(2026, 11, 25, 3, 30)
    early = datetime(2026, 11, 25, 2, 0)

    # 从没跑过：到点就跑，没到点但超阈值也跑
    assert distillation_due(nightly, 10) is True
    assert distillation_due(early, 10) is False
    assert distillation_due(early, 600) is True

    # 今天已经跑过：不再跑；昨天跑过：到点再跑
    assert distillation_due(nightly, 10, datetime(2026, 11, 25, 1, 0)) is False
    assert distillation_due(nightly, 10, datetime(2026, 11, 24, 3, 0)) is True

    # 超阈值补跑受间隔约束
    assert distillation_due(
        datetime(2026, 11, 25, 12, 0), 600, datetime(2026, 11, 25, 7, 0),
        interval_hours=6,
    ) is False
    assert distillation_due(
        datetime(2026, 11, 25, 13, 0), 600, datetime(2026, 11, 25, 7, 0),
        interval_hours=6,
    ) is True
