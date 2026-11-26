"""Docker 卷备份的纯逻辑（不真的调 docker）。

真链路验收在部署机上跑 `scripts/backup_docker.py`；这里只保证：
卷名不瞎猜、快照脚本规则对、解包不放路径穿越、备份里能看到 data/。
"""
import io
import importlib.util
import os
import sqlite3
import tarfile
import zipfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "backup_docker", os.path.join(ROOT, "scripts", "backup_docker.py")
)
bd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bd)
backup_mod = bd.backup_mod


def test_pick_volume_auto_when_single():
    volumes = ["abc", "opencode-my_xiaoma-data", "xiaoma_other"]
    assert bd.pick_volume(volumes=volumes) == "opencode-my_xiaoma-data"


def test_pick_volume_refuses_to_guess():
    with pytest.raises(SystemExit):
        bd.pick_volume(volumes=["a", "b"])
    with pytest.raises(SystemExit):
        bd.pick_volume(volumes=["a_xiaoma-data", "b_xiaoma-data"])


def test_snapshot_script_only_takes_books_and_skips_logs():
    script = bd._snapshot_py(False)
    assert 'all_data = False' in script
    assert '.db' in script and 'source.backup(tgt)' in script
    assert '.log' in script
    # 非 all_data 时，除了 print/、spaces/ 不碰其它目录
    assert 'if name == "backups" or (not all_data and name not in ("print", "spaces"))' in script
    assert 'all_data = True' in bd._snapshot_py(True)


def test_safe_extract_blocks_path_traversal(tmp_path):
    src = tmp_path / "evil.tar"
    with tarfile.open(src, "w") as tar:
        for name in ("ok.txt", "../escape.txt", "/abs.txt"):
            data = b"x"
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tar.addfile(link)

    dest = tmp_path / "out"
    dest.mkdir()
    bd._safe_extract(str(src), str(dest))

    assert (dest / "ok.txt").exists()
    assert not (tmp_path / "escape.txt").exists()
    assert not (dest / "link").exists()


def test_safe_unzip_blocks_path_traversal(tmp_path):
    src = tmp_path / "evil.zip"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("ok.txt", "x")
        zf.writestr("../escape.txt", "x")
        zf.writestr("nested/a.db", "x")

    dest = tmp_path / "out"
    dest.mkdir()
    bd._safe_unzip(str(src), str(dest))

    assert (dest / "ok.txt").exists()
    assert (dest / "nested" / "a.db").exists()
    assert not (tmp_path / "escape.txt").exists()


def test_backup_from_data_dir_packs_data_prefix(tmp_path):
    """卷快照解出来的目录交给 backup.py，包内路径仍是 data/xxx，恢复逻辑不变。"""
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".env").write_text("API_TOKEN=x\n", encoding="utf-8")
    data_dir = tmp_path / "volume"
    data_dir.mkdir()
    with sqlite3.connect(str(data_dir / "memory.db")) as conn:
        conn.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, text TEXT)")
        conn.execute("INSERT INTO memories (text) VALUES ('牛奶')")

    zip_path = backup_mod.create_backup(
        root=str(root), out_dir=str(tmp_path / "out"),
        data_dir=str(data_dir), note="docker:test",
    )

    ok, problems = backup_mod.verify_archive(zip_path)
    assert ok, problems
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    assert "data/memory.db" in names
    assert ".env" in names


def test_restore_to_busy_volume_needs_yes(tmp_path, monkeypatch):
    monkeypatch.setattr(
        bd.backup_mod, "verify_archive",
        lambda path, passphrase=None: (True, []),
    )
    monkeypatch.setattr(bd, "container_using", lambda volume: ["xiaoma"])

    with pytest.raises(SystemExit):
        bd.restore("whatever.zip", "opencode-my_xiaoma-data", yes=False)
