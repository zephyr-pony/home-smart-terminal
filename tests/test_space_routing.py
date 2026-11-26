"""空间手动路由（D71）：X-Space-Id 换一套库，非法名字报错，默认空间不受影响。

只测「请求落到哪套数据」，不测多成员权限——现在还没有认人，谁带了这个头就是谁。
"""
import pytest
from fastapi.testclient import TestClient

import server
from agent.clarify import Clarifier
from domain.facts import Facts
from domain.space import DEFAULT_SPACE_ID
from domain.store import AgentStore
from domain.tasks import Tasks
from items import Items
from memory import Memory

AUTH = {"Authorization": "Bearer secret"}


class SpaceAssistant:
    """只实现路由会碰到的部分；库用内存，不碰 DATA_DIR。"""

    def __init__(self, space_id):
        self.space_id = space_id
        self.memory = Memory(":memory:")
        self.items = Items(":memory:")
        self.tasks = Tasks(":memory:")
        self.facts = Facts(":memory:")
        self.clarifier = Clarifier(store=AgentStore(":memory:"), items=self.items)
        self.sessions = {}
        self.print_store = None
        self.print_files_dir = ""
        self.printer_ready = True
        self.calls = []

    def process_text(self, text, client_id=None, **kwargs):
        self.calls.append((text, client_id))
        return {
            "type": "chat", "reply": "来自 " + self.space_id,
            "memories": [], "used_tools": [], "domain": "chat", "action": "chat",
        }

    def printer_status(self, force=False):
        return {"available": True, "state": "idle", "error": "",
                "configured": True, "queue": "xiaoma", "jobs": 0}


async def _fake_synthesize(reply):
    return ""


@pytest.fixture
def routed(monkeypatch, tmp_path):
    """把默认空间换成假实现，新空间也走假构造，数据目录指到 tmp。"""
    import domain.space as space_mod
    monkeypatch.setattr(space_mod, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    monkeypatch.setattr(server, "_synthesize", _fake_synthesize)
    monkeypatch.setattr(server, "_space_assistants", {})
    monkeypatch.setattr(server, "MAX_SPACES", 16)

    default = SpaceAssistant(DEFAULT_SPACE_ID)
    monkeypatch.setattr(server, "assistant", default)

    made = {}

    def build(space_id, paths):
        made[space_id] = SpaceAssistant(space_id)
        made[space_id].print_files_dir = paths["print_files"]
        return made[space_id]

    monkeypatch.setattr(server, "_build_space_assistant", build)
    return TestClient(server.app), default, made, tmp_path


def test_header_routes_chat_to_that_space(routed):
    client, default, made, _tmp = routed

    response = client.post(
        "/api/chat", headers={**AUTH, "X-Space-Id": "family-a"},
        json={"text": "你好", "client_id": "c1"},
    )

    assert response.status_code == 200
    assert response.json()["reply"] == "来自 family-a"
    assert made["family-a"].calls == [("你好", "c1")]
    assert default.calls == []  # 默认空间完全没被碰


def test_list_endpoints_read_the_space_store(routed):
    client, default, made, _tmp = routed
    default.memory.store("默认空间的记忆")
    # 第一次访问触发创建，之后往它的库里写一条
    client.get("/api/memories", headers={**AUTH, "X-Space-Id": "family-b"})
    made["family-b"].memory.store("另一个空间的记忆")

    plain = client.get("/api/memories", headers=AUTH).json()
    other = client.get(
        "/api/memories", headers={**AUTH, "X-Space-Id": "family-b"}
    ).json()

    assert [row["text"] for row in plain["memories"]] == ["默认空间的记忆"]
    assert [row["text"] for row in other["memories"]] == ["另一个空间的记忆"]


def test_status_reports_the_requested_space(routed):
    client, _default, _made, _tmp = routed

    body = client.get(
        "/api/status", headers={**AUTH, "X-Space-Id": "family-c"}
    ).json()

    assert body["space_id"] == "family-c"
    assert body["stt_ready"] is False


def test_new_space_directory_is_created_under_spaces(routed):
    client, _default, _made, tmp_path = routed

    client.get("/api/memories", headers={**AUTH, "X-Space-Id": "family-d"})

    assert (tmp_path / "spaces" / "family-d").is_dir()
    assert (tmp_path / "spaces" / "family-d" / "print").is_dir()


def test_invalid_space_id_is_rejected_without_creating(routed):
    client, _default, made, _tmp = routed

    response = client.get(
        "/api/memories", headers={**AUTH, "X-Space-Id": "../etc"}
    )

    assert response.status_code == 400
    assert made == {}


def test_space_count_is_capped(routed, monkeypatch):
    client, _default, _made, _tmp = routed
    monkeypatch.setattr(server, "MAX_SPACES", 0)

    response = client.get(
        "/api/memories", headers={**AUTH, "X-Space-Id": "family-e"}
    )

    assert response.status_code == 409
    assert "上限" in response.json()["detail"]


def test_spaces_endpoint_lists_default(routed):
    client, _default, _made, _tmp = routed
    client.get("/api/memories", headers={**AUTH, "X-Space-Id": "family-f"})

    body = client.get("/api/spaces", headers=AUTH).json()

    ids = [item["id"] for item in body["spaces"]]
    assert ids[0] == DEFAULT_SPACE_ID
    assert "family-f" in ids
    assert body["max_spaces"] == 16


def test_space_print_assets(routed):
    client, default, made, _tmp = routed
    client.get("/api/memories", headers={**AUTH, "X-Space-Id": "family-g"})

    other = made["family-g"]
    other.print_store = object()

    space = server.Space("family-g", other, {"dir": "x"})
    assert space.print_store is other.print_store
    assert space.print_dir == other.print_files_dir

    # 默认空间仍回到模块级变量，测试和单家庭部署都靠这个
    default_space = server.Space(DEFAULT_SPACE_ID, default, {"dir": "x"})
    assert default_space.print_store is server.print_store
    assert default_space.print_dir is server.PRINT_FILES_DIR
