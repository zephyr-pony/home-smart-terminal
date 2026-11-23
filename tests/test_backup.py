"""备份脚本：内容范围、热备一致性、校验、保留策略、恢复演练。

这台机器上真跑过一次恢复演练（见 docs/backup.md），这里保证行为不被改坏。
"""
import importlib.util
import json
import os
import sqlite3
import zipfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "backup_script", os.path.join(ROOT, "scripts", "backup.py")
)
backup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backup)


@pytest.fixture()
def project(tmp_path):
    """一个最小的项目目录：.env + 两个库 + 会被排除的日志/子目录。"""
    root = tmp_path / "proj"
    (root / "data").mkdir(parents=True)
    (root / ".env").write_text("API_TOKEN=test-token\n", encoding="utf-8")
    conn = sqlite3.connect(str(root / "data" / "memory.db"))
    conn.execute("CREATE TABLE memories (text TEXT)")
    conn.execute("INSERT INTO memories VALUES ('牛奶快喝完了')")
    conn.commit()
    conn.close()
    (root / "data" / "server.log").write_text("noise\n", encoding="utf-8")
    (root / "data" / "chroma").mkdir()
    (root / "data" / "chroma" / "index.bin").write_bytes(b"vector")
    return str(root)


def test_backup_contains_core_state_only(project):
    zip_path = backup.create_backup(root=project, out_dir=os.path.join(project, "backups"))
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
    assert ".env" in names
    assert "data/memory.db" in names
    assert "data/server.log" not in names
    assert "data/chroma/index.bin" not in names
    assert backup.MANIFEST in names


def test_all_data_includes_subdirs(project):
    zip_path = backup.create_backup(
        root=project, out_dir=os.path.join(project, "backups"), all_data=True
    )
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
    assert "data/chroma/index.bin" in names


def test_snapshot_works_while_db_connection_open(project):
    """服务在跑的时候备份：连接开着也要拿到完整数据，不能拷到写了一半的页。"""
    live = sqlite3.connect(os.path.join(project, "data", "memory.db"))
    live.execute("INSERT INTO memories VALUES ('备用钥匙在鞋柜第二层')")
    live.commit()  # 不关闭连接，模拟 server.py 常驻

    zip_path = backup.create_backup(root=project, out_dir=os.path.join(project, "backups"))
    live.close()

    target = os.path.join(project, "restore")
    backup.restore_archive(zip_path, target)
    restored = sqlite3.connect(os.path.join(target, "data", "memory.db"))
    rows = [row[0] for row in restored.execute("SELECT text FROM memories")]
    restored.close()
    assert "牛奶快喝完了" in rows
    assert "备用钥匙在鞋柜第二层" in rows


def test_verify_rejects_tampered_manifest(project):
    zip_path = backup.create_backup(root=project, out_dir=os.path.join(project, "backups"))
    # 改掉压缩包里的 .env，清单里的 sha256 就对不上了
    tampered = zip_path + ".bad"
    with zipfile.ZipFile(zip_path) as src, zipfile.ZipFile(tampered, "w") as dst:
        for name in src.namelist():
            data = src.read(name)
            if name == ".env":
                data = b"API_TOKEN=evil\n"
            dst.writestr(name, data)
    ok, problems = backup.verify_archive(tampered)
    assert not ok
    assert any("sha256" in problem for problem in problems)


def test_verify_accepts_fresh_backup(project):
    zip_path = backup.create_backup(root=project, out_dir=os.path.join(project, "backups"))
    ok, problems = backup.verify_archive(zip_path)
    assert ok, problems


def test_prune_keeps_newest(project):
    out = os.path.join(project, "backups")
    os.makedirs(out)
    for stamp in ("20260101-030000", "20260102-030000", "20260103-030000"):
        with zipfile.ZipFile(os.path.join(out, "xiaoma-%s.zip" % stamp), "w") as zf:
            zf.writestr("x", "1")
    removed = backup.prune(out, keep=2)
    assert removed == ["xiaoma-20260101-030000.zip"]
    assert len(backup.list_backups(out)) == 2


def test_restore_to_dir_does_not_touch_source(project):
    zip_path = backup.create_backup(root=project, out_dir=os.path.join(project, "backups"))
    (os.path.join(project, "data", "memory.db"))
    before = open(os.path.join(project, ".env"), encoding="utf-8").read()
    target = os.path.join(project, "drill")
    backup.restore_archive(zip_path, target)
    assert os.path.exists(os.path.join(target, ".env"))
    assert open(os.path.join(project, ".env"), encoding="utf-8").read() == before


def test_restore_refuses_without_yes(project):
    """防手滑：不加 --yes 不许往本机数据目录解包。"""
    zip_path = backup.create_backup(root=project, out_dir=os.path.join(project, "backups"))
    with pytest.raises(SystemExit):
        backup.restore_archive(zip_path, backup.ROOT)


def test_manifest_lists_items_with_hashes(project):
    zip_path = backup.create_backup(root=project, out_dir=os.path.join(project, "backups"))
    with zipfile.ZipFile(zip_path) as zf:
        manifest = json.loads(zf.read(backup.MANIFEST).decode("utf-8"))
    names = {item["name"] for item in manifest["items"]}
    assert ".env" in names and "data/memory.db" in names
    assert all(len(item["sha256"]) == 64 for item in manifest["items"])
