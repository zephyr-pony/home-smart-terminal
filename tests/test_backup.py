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


def test_print_files_are_backed_up_by_default(project):
    """打印文件是用户原件（D65），不像 work/home 那样可重建，默认要备。"""
    folder = os.path.join(project, "data", "print")
    os.makedirs(folder)
    with open(os.path.join(folder, "quote.txt"), "w", encoding="utf-8") as fh:
        fh.write("报价单")

    zip_path = backup.create_backup(root=project, out_dir=os.path.join(project, "backups"))
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
    assert "data/print/quote.txt" in names
    assert "data/chroma/index.bin" not in names


def test_backup_from_an_external_data_dir_keeps_archive_paths(tmp_path, project):
    """Docker 卷快照解在仓库外的临时目录时，压缩包里的路径必须还是 data/…。

    以前这里相对仓库根算 relpath，跨盘直接抛 ValueError，同盘则写成
    `../temp/xiaoma-volume-…/data/print/x`——压缩包「成功」了，但校验失败、
    恢复出来位置也不对。
    """
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "memory.db").write_bytes(b"snapshot")
    (snapshot / "print").mkdir()
    (snapshot / "print" / "a.txt").write_text("原件的副本", encoding="utf-8")

    zip_path = backup.create_backup(
        root=project, out_dir=os.path.join(project, "backups"),
        data_dir=str(snapshot),
    )
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
    assert "data/memory.db" in names
    assert "data/print/a.txt" in names
    assert not [name for name in names if name.startswith("..")]


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


# ---------- 加密备份（AES-256-GCM） ----------

def test_encrypted_backup_leaves_no_plaintext(project):
    out = os.path.join(project, "backups")
    path = backup.create_backup(root=project, out_dir=out, passphrase="正确口令")

    assert path.endswith(".zip.enc")
    assert backup.is_encrypted(path)
    # 同一目录不能同时留明文 zip：加密完就删
    assert [n for n in os.listdir(out) if n.endswith(".zip")] == []
    with open(path, "rb") as fh:
        assert fh.read(5) == backup.BACKUP_MAGIC
    # 密文里不该出现原文
    with open(path, "rb") as fh:
        assert "牛奶快喝完了".encode("utf-8") not in fh.read()


def test_encrypted_backup_verifies_and_restores(project):
    out = os.path.join(project, "backups")
    path = backup.create_backup(root=project, out_dir=out, passphrase="正确口令")

    ok, problems = backup.verify_archive(path, passphrase="正确口令")
    assert ok, problems

    target = os.path.join(project, "drill")
    backup.restore_archive(path, target, passphrase="正确口令")
    assert os.path.exists(os.path.join(target, ".env"))
    assert os.path.exists(os.path.join(target, "data", "memory.db"))


def test_wrong_passphrase_is_reported(project):
    out = os.path.join(project, "backups")
    path = backup.create_backup(root=project, out_dir=out, passphrase="正确口令")

    ok, problems = backup.verify_archive(path, passphrase="错误口令")
    assert ok is False
    assert any("口令" in item for item in problems)

    with pytest.raises(SystemExit):
        backup.restore_archive(path, os.path.join(project, "drill"),
                               passphrase="错误口令")


def test_empty_passphrase_means_plain_backup(project):
    """空口令不是错误，只是「不加密」；拒绝发生在 CLI 的 --encrypt 上。"""
    out = os.path.join(project, "backups")
    path = backup.create_backup(root=project, out_dir=out, passphrase="")
    assert path.endswith(".zip")
    assert backup.is_encrypted(path) is False


def test_passphrase_from_env_and_file(project, tmp_path, monkeypatch):
    key_file = tmp_path / "backup.key"
    key_file.write_text("文件口令\n", encoding="utf-8")
    monkeypatch.delenv(backup.PASSPHRASE_ENV, raising=False)

    assert backup.resolve_passphrase(passphrase_file=str(key_file)) == "文件口令"

    monkeypatch.setenv(backup.PASSPHRASE_ENV, "环境口令")
    assert backup.resolve_passphrase() == "环境口令"
    # 显式参数优先
    assert backup.resolve_passphrase("显式口令") == "显式口令"


def test_cli_encrypt_requires_passphrase(tmp_path, monkeypatch):
    """CLI 上 --encrypt 没给口令就退出，不悄悄产出明文包。"""
    monkeypatch.delenv(backup.PASSPHRASE_ENV, raising=False)
    with pytest.raises(SystemExit):
        backup.main(["--encrypt", "--out", str(tmp_path)])


def test_prune_counts_encrypted_archives(project):
    out = os.path.join(project, "backups")
    os.makedirs(out, exist_ok=True)
    for stamp in ("20260101-030000", "20260102-030000", "20260103-030000"):
        with open(os.path.join(out, "xiaoma-%s.zip.enc" % stamp), "wb") as fh:
            fh.write(backup.BACKUP_MAGIC + b"x" * 40)

    removed = backup.prune(out, keep=2)

    assert removed == ["xiaoma-20260101-030000.zip.enc"]
    assert len(backup.list_backups(out)) == 2
