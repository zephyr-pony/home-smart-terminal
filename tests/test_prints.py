"""打印记录（D65）：重启后还能「再打一份」，历史能查。

文件放持久目录、元数据进 prints.db；这里验证存取、重启、清理和只读 API。
"""
import time

from fastapi.testclient import TestClient

import server
from domain.prints import PrintStore


def _store(tmp_path):
    return PrintStore(str(tmp_path / "prints.db"), keep_days=7)


def _file(tmp_path, name="报价单.txt", content=b"x"):
    path = tmp_path / ("f-" + name)
    path.write_bytes(content)
    return str(path)


def test_add_get_and_owner_check(tmp_path):
    store = _store(tmp_path)
    path = _file(tmp_path)
    store.add("f1", "报价单.txt", path, kind="text", size=1, client_id="phone")

    row = store.get("f1")
    assert row["name"] == "报价单.txt"
    assert row["status"] == "ready"
    # 别人的 id 加自己的 client_id 取不到
    assert store.get("f1", "other") is None
    assert store.get("f1", "phone")["path"] == path


def test_survives_reopen(tmp_path):
    """重启就是 close 再 open：记录和文件都还在。"""
    path = _file(tmp_path)
    store = _store(tmp_path)
    store.add("f1", "表.xlsx", path, kind="office", pages=None, source="generated")
    store.close()

    again = _store(tmp_path)
    row = again.get("f1")
    assert row is not None
    assert row["source"] == "generated"
    assert again.latest()["id"] == "f1"


def test_latest_skips_missing_files(tmp_path):
    path = _file(tmp_path)
    store = _store(tmp_path)
    store.add("old", "旧.pdf", path, kind="pdf")
    missing = str(tmp_path / "gone.pdf")
    store.add("new", "新.pdf", missing, kind="pdf")

    assert store.latest()["id"] == "old"


def test_mark_records_result(tmp_path):
    store = _store(tmp_path)
    store.add("f1", "a.txt", _file(tmp_path), kind="text")
    store.mark("f1", "printed", copies=2, printer="HP1020",
               job_id="HP1020-11", error="")
    row = store.get("f1")
    assert row["status"] == "printed"
    assert row["copies"] == 2
    assert row["job_id"] == "HP1020-11"


def test_prune_removes_old_files_but_keeps_history(tmp_path):
    path = _file(tmp_path)
    store = _store(tmp_path)
    store.add("f1", "a.txt", path, kind="text")
    with store._lock:
        store._conn.execute("UPDATE print_jobs SET created_at = ? WHERE id = 'f1'",
                            (time.time() - 8 * 86400,))
        store._conn.commit()

    assert store.prune() == 1
    assert not __import__("os").path.exists(path)
    # 历史还在，只是文件没了，取不到内容
    items, total = store.list()
    assert total == 1
    assert store.get("f1") is None


def test_list_filters_and_paging(tmp_path):
    store = _store(tmp_path)
    for i in range(3):
        store.add("f%d" % i, "报表%d.txt" % i, _file(tmp_path, "%d.txt" % i))
    store.mark("f0", "printed", job_id="J0")

    rows, total = store.list(q="报表1")
    assert total == 1 and rows[0]["id"] == "f1"
    rows, total = store.list(status="printed")
    assert total == 1 and rows[0]["id"] == "f0"
    rows, total = store.list(limit=2, offset=2)
    assert total == 3 and len(rows) == 1


def test_api_prints_lists_history(monkeypatch, tmp_path):
    store = _store(tmp_path)
    store.add("f1", "报价单.txt", _file(tmp_path), kind="text", size=12,
              client_id="phone")
    store.mark("f1", "printed", copies=1, printer="HP1020", job_id="HP1020-12")
    monkeypatch.setattr(server, "print_store", store)
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    client = TestClient(server.app)

    resp = client.get("/api/prints", headers={"Authorization": "Bearer secret"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    item = data["items"][0]
    assert item["id"] == "f1"
    assert item["name"] == "报价单.txt"
    assert item["status"] == "printed"
    assert item["job_id"] == "HP1020-12"
    assert "path" not in item  # 不把容器内路径给前端

    assert client.get("/api/prints").status_code == 401
    bad = client.get("/api/prints?status=nope",
                     headers={"Authorization": "Bearer secret"})
    assert bad.status_code == 400


def test_overview_page_has_prints_panel(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "")
    client = TestClient(server.app)
    html = client.get("/").text
    assert 'id="prints-list"' in html
    assert "最近打印" in html
    assert 'id="runtime-info"' in html
    assert "运行状态" in html
