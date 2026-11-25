"""思考开关的观测记录（D63）。

模型自己决定要不要开思考之后，效果只能靠观察：哪类问题开了、开了是不是更稳。
这里验证记录本身（写、读、统计、可关闭）和 `/api/thinking` 只读接口。
"""
import json
import os

from fastapi.testclient import TestClient

import server
from agent import thinking


def _use_temp_log(monkeypatch, tmp_path):
    monkeypatch.setattr(thinking, "LOG_PATH", str(tmp_path / "thinking.jsonl"))
    monkeypatch.setattr(thinking, "ENABLED", True)


def test_record_and_tail(monkeypatch, tmp_path):
    _use_temp_log(monkeypatch, tmp_path)
    thinking.record_turn(
        "把数量和单价相乘再打印", "web", True, "要算表格写代码",
        ["file_read", "run_python"], 12.34, {"type": "chat", "action": "print"},
    )
    thinking.record_turn(
        "今天几号", "web", False, "", [], 1.2, {"type": "chat", "action": "chat"},
    )

    items = thinking.tail(10)
    assert len(items) == 2
    assert items[0]["thinking"] is True
    assert items[0]["why"] == "要算表格写代码"
    assert items[0]["tools"] == ["file_read", "run_python"]
    assert items[1]["thinking"] is False
    assert items[1]["type"] == "chat"


def test_summary_splits_thinking_and_not(monkeypatch, tmp_path):
    _use_temp_log(monkeypatch, tmp_path)
    thinking.record_turn("算表", "c", True, "", [], 10.0, {"type": "chat"})
    thinking.record_turn("算表2", "c", True, "", [], 20.0, {"type": "chat"})
    thinking.record_turn("你好", "c", False, "", [], 2.0, {"type": "chat"})

    stats = thinking.summary(10)
    assert stats == {
        "total": 3,
        "thinking": 2,
        "no_thinking": 1,
        "avg_seconds_thinking": 15.0,
        "avg_seconds_no_thinking": 2.0,
    }


def test_disabled_writes_nothing(monkeypatch, tmp_path):
    path = tmp_path / "thinking.jsonl"
    monkeypatch.setattr(thinking, "LOG_PATH", str(path))
    monkeypatch.setattr(thinking, "ENABLED", False)
    thinking.record_turn("你好", "c", False, "", [], 1.0, {"type": "chat"})
    assert not path.exists()
    assert thinking.tail(5) == []


def test_tail_survives_corrupt_line(monkeypatch, tmp_path):
    path = tmp_path / "thinking.jsonl"
    monkeypatch.setattr(thinking, "LOG_PATH", str(path))
    monkeypatch.setattr(thinking, "ENABLED", True)
    path.write_text('{"text": "旧"}\nnot-json\n', encoding="utf-8")
    items = thinking.tail(10)
    assert [item["text"] for item in items] == ["旧"]


def test_api_thinking_reads_only(monkeypatch, tmp_path):
    _use_temp_log(monkeypatch, tmp_path)
    thinking.record_turn(
        "帮我查快递", "web", False, "单条查询不用思考", ["task_list"], 1.5,
        {"type": "query", "action": "search"},
    )
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    client = TestClient(server.app)

    resp = client.get(
        "/api/thinking?limit=5", headers={"Authorization": "Bearer secret"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["enabled"] is True
    assert data["items"][0]["text"] == "帮我查快递"
    assert data["summary"]["total"] == 1
    assert data["summary"]["no_thinking"] == 1


def test_api_thinking_requires_token(monkeypatch, tmp_path):
    _use_temp_log(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    client = TestClient(server.app)
    assert client.get("/api/thinking").status_code == 401
