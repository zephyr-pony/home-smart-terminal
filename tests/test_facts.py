import pytest

from domain.facts import Facts


def test_store_search_and_list_facts():
    facts = Facts(":memory:")
    facts.store("爸妈家", "电费户号", "123456", source="用户")
    facts.store("牛奶", "品牌", "蒙牛")

    assert facts.count() == 2

    found = facts.search("户号")
    assert found[0]["value"] == "123456"
    assert found[0]["source"] == "用户"

    assert facts.search(subject="牛奶")[0]["predicate"] == "品牌"
    assert len(facts.list_all()) == 2


def test_delete_by_id():
    facts = Facts(":memory:")
    fact = facts.store("爸妈家", "电费户号", "123456")

    deleted = facts.delete_by_id(fact["id"])

    assert deleted["value"] == "123456"
    assert facts.count() == 0
    assert facts.delete_by_id(999) is None


def test_invalid_fact_is_rejected():
    facts = Facts(":memory:")

    with pytest.raises(ValueError):
        facts.store("", "品牌", "蒙牛")
