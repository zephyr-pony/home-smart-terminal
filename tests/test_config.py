"""配置路径：一个 DATA_DIR 管所有库，单独覆盖仍然优先。"""
import os

import config


def test_db_path_follows_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.delenv("MEMORY_DB_PATH", raising=False)

    assert config._db_path("memory.db", "MEMORY_DB_PATH") == os.path.join(
        str(tmp_path), "memory.db"
    )


def test_explicit_db_path_still_wins(tmp_path, monkeypatch):
    custom = tmp_path / "custom.db"
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MEMORY_DB_PATH", str(custom))

    assert config._db_path("memory.db", "MEMORY_DB_PATH") == str(custom)
