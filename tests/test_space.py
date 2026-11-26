"""空间隔离的地基（D68）：名字规则、路径组合、两套库互不串。

真正的多成员路由还没做；这里保证「换 space_id 就是换一套文件」这条地基
不会在以后被改坏（比如又改成共用一个库、只加一列 space_id）。
"""
import pytest

from domain.space import (
    DEFAULT_SPACE_ID,
    ensure_space,
    list_spaces,
    normalize_space_id,
    space_dir,
    space_paths,
)
from items import Items
from memory import Memory


def test_normalize_space_id():
    assert normalize_space_id("") == DEFAULT_SPACE_ID
    assert normalize_space_id(None) == DEFAULT_SPACE_ID
    assert normalize_space_id(" Family-A ") == "family-a"

    for bad in ("../etc", "a/b", "带空格 的", "x" * 33, "-lead"):
        with pytest.raises(ValueError):
            normalize_space_id(bad)


def test_default_paths_keep_existing_configuration():
    """默认空间必须还是原来那套路径，升级不能把数据搬走。"""
    paths = space_paths(DEFAULT_SPACE_ID)
    assert paths["space_id"] == "default"
    assert paths["memory"].endswith("memory.db")
    assert paths["print_files"].endswith("print")


def test_other_space_paths_live_under_spaces(tmp_path):
    paths = space_paths("family-b", data_dir=str(tmp_path))
    assert paths["dir"] == str(tmp_path / "spaces" / "family-b")
    assert paths["memory"] == str(tmp_path / "spaces" / "family-b" / "memory.db")
    assert paths["print_files"] == str(tmp_path / "spaces" / "family-b" / "print")


def test_explicit_data_dir_applies_to_default_too(tmp_path):
    paths = space_paths(DEFAULT_SPACE_ID, data_dir=str(tmp_path))
    assert paths["dir"] == str(tmp_path)
    assert paths["tasks"] == str(tmp_path / "tasks.db")


def test_spaces_are_isolated(tmp_path):
    alpha = ensure_space("alpha", data_dir=str(tmp_path))
    beta = ensure_space("beta", data_dir=str(tmp_path))

    memory_a = Memory(alpha["memory"])
    memory_a.store("alpha 家的门禁密码")
    memory_b = Memory(beta["memory"])

    assert memory_a.count() == 1
    assert memory_b.count() == 0
    assert memory_b.search("门禁密码") == []

    items_a = Items(alpha["items"])
    items_a.add("牛奶", "2027-01-01")
    items_b = Items(beta["items"])
    assert items_a.list_all()
    assert items_b.list_all() == []


def test_list_spaces(tmp_path):
    ensure_space("alpha", data_dir=str(tmp_path))
    ensure_space("beta", data_dir=str(tmp_path))
    assert list_spaces(str(tmp_path)) == ["default", "alpha", "beta"]


def test_space_dir_rejects_traversal():
    with pytest.raises(ValueError):
        space_dir("../../root")
