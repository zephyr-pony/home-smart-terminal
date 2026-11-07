from agent.clarify import Clarifier
from agent.entities import EntityService
from assistant import Assistant
from domain.store import AgentStore
from items import Items
from memory import Memory


def make_service():
    store = AgentStore(":memory:")
    return EntityService(store), store


def test_add_entity_and_alias():
    service, store = make_service()

    result = service.add_entity("property", "自己家", ["咱家"])

    assert "自己家" in result
    assert "咱家" in result
    entity = store.get_entity("property", "咱家")
    assert entity["canonical_name"] == "自己家"
    assert "咱家" in entity["aliases"]


def test_add_alias_to_existing_entity():
    service, store = make_service()
    service.add_entity("property", "自己家")

    service.add_entity("property", "自己家", ["我家"])

    entity = store.get_entity("property", "自己家")
    assert "我家" in entity["aliases"]


def test_alias_conflict_between_entities():
    service, _store = make_service()
    service.add_entity("property", "自己家")
    service.add_entity("property", "爸妈家")

    result = service.add_entity("property", "自己家", ["爸妈家"])

    assert "冲突" in result


def test_rename_entity_keeps_old_name_as_alias():
    service, store = make_service()
    service.add_entity("property", "爸妈家")

    result = service.rename_entity("property", "爸妈家", "父母家")

    assert "父母家" in result
    entity = store.get_entity("property", "父母家")
    assert entity["canonical_name"] == "父母家"
    assert "爸妈家" in entity["aliases"]


def test_rename_conflict():
    service, _store = make_service()
    service.add_entity("property", "自己家")
    service.add_entity("property", "爸妈家")

    result = service.rename_entity("property", "自己家", "爸妈家")

    assert "冲突" in result


def test_list_entities():
    service, _store = make_service()
    service.add_entity("property", "自己家", ["咱家"])
    service.add_entity("person", "妈妈")

    text = service.list_entities()

    assert "住所" in text
    assert "自己家" in text
    assert "咱家" in text
    assert "家庭成员" in text
    assert "妈妈" in text


def test_list_entities_by_type():
    service, _store = make_service()
    service.add_entity("property", "自己家")
    service.add_entity("person", "妈妈")

    text = service.list_entities("property")

    assert "自己家" in text
    assert "妈妈" not in text


def test_renamed_entity_used_by_clarifier():
    service, store = make_service()
    service.add_entity("property", "自己家")
    service.rename_entity("property", "自己家", "我家")
    service.add_entity("property", "爸妈家")
    clarifier = Clarifier(store=store, items=Items(":memory:"))

    result = clarifier.check("明天要交电费", "c1")

    assert result["kind"] == "clarify"
    assert "我家" in result["reply"]
    assert "自己家" not in result["reply"]


def test_alias_skips_clarification():
    service, store = make_service()
    service.add_entity("property", "自己家", ["咱家"])
    clarifier = Clarifier(store=store, items=Items(":memory:"))

    assert clarifier.check("咱家明天要交电费", "c1") is None


def test_assistant_registers_entity_tools():
    items = Items(":memory:")
    store = AgentStore(":memory:")
    assistant = Assistant(
        memory=Memory(":memory:"),
        items=items,
        llm=object(),
        clarifier=Clarifier(store=store, items=items),
    )
    tool = next(
        tool for tool in assistant.tools
        if tool["schema"]["function"]["name"] == "entity_add"
    )

    result = tool["execute"]("property", "自己家", ["咱家"])

    assert "咱家" in result
    assert store.get_entity("property", "咱家")["canonical_name"] == "自己家"
