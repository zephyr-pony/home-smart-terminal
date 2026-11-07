"""实体管理：新增、改名、别名、候选查看。

实体类型固定为 property / person / product / provider / place / device。
别名冲突必须报错，不能静默合并到错误的实体。
"""
from domain.store import normalize_text

ENTITY_TYPES = {
    "property": "住所",
    "person": "家庭成员",
    "product": "物品",
    "provider": "缴费单位",
    "place": "地点",
    "device": "设备",
}


class EntityService:
    def __init__(self, store):
        self.store = store

    def add_entity(self, type, canonical_name, aliases=None):
        entity_type = str(type or "").strip()
        label = ENTITY_TYPES.get(entity_type)
        if label is None:
            return (
                f"未知实体类型：{entity_type}。可用类型："
                + "、".join(ENTITY_TYPES)
            )

        canonical_name = str(canonical_name or "").strip()
        if not canonical_name:
            return "实体名称不能为空"
        aliases = [str(alias).strip() for alias in (aliases or []) if str(alias).strip()]

        existing = self.store.get_entity(entity_type, canonical_name)
        alias_owners = []
        for alias in aliases:
            owner = self.store.get_entity(entity_type, alias)
            if owner is not None:
                alias_owners.append(owner)

        if existing is None:
            unique_owners = {
                owner["id"]: owner for owner in alias_owners
            }
            if len(unique_owners) > 1:
                names = "、".join(owner["canonical_name"] for owner in unique_owners.values())
                return f"别名冲突：这些别名分别属于 {names}，请分开维护。"
            if unique_owners:
                existing = next(iter(unique_owners.values()))

        if existing is not None:
            for owner in alias_owners:
                if owner["id"] != existing["id"]:
                    return (
                        f"别名冲突：「{owner['canonical_name']}」已经是另一个"
                        f"{label}的名字。"
                    )
            merged = set(existing["aliases"])
            merged.update(aliases)
            merged.add(canonical_name)
            merged.discard(existing["canonical_name"])
            self.store.update_entity(
                existing["id"], existing["canonical_name"], sorted(merged)
            )
            result = self._format_entity(
                existing["canonical_name"], sorted(merged), label
            )
            self._audit(
                "entity_add",
                {"type": entity_type, "canonical_name": canonical_name, "aliases": aliases},
                result,
            )
            return result

        self.store.add_entity(entity_type, canonical_name, aliases)
        result = self._format_entity(canonical_name, aliases, label)
        self._audit(
            "entity_add",
            {"type": entity_type, "canonical_name": canonical_name, "aliases": aliases},
            result,
        )
        return result

    def rename_entity(self, type, old_name, new_name):
        entity_type = str(type or "").strip()
        label = ENTITY_TYPES.get(entity_type)
        if label is None:
            return f"未知实体类型：{entity_type}。"

        old_name = str(old_name or "").strip()
        new_name = str(new_name or "").strip()
        if not old_name or not new_name:
            return "旧名称和新名称都不能为空"

        entity = self.store.get_entity(entity_type, old_name)
        if entity is None:
            return f"没有找到{label}：{old_name}"

        conflict = self.store.get_entity(entity_type, new_name)
        if conflict is not None and conflict["id"] != entity["id"]:
            return f"名称冲突：{new_name} 已经是另一个{label}的名字。"

        aliases = set(entity["aliases"])
        aliases.add(entity["canonical_name"])
        aliases.add(old_name)
        aliases.discard(new_name)

        self.store.update_entity(entity["id"], new_name, sorted(aliases))
        result = self._format_entity(new_name, sorted(aliases), label)
        self._audit(
            "entity_rename",
            {"type": entity_type, "old_name": old_name, "new_name": new_name},
            result,
        )
        return result

    def list_entities(self, type=None):
        entity_type = str(type or "").strip() or None
        if entity_type and entity_type not in ENTITY_TYPES:
            return f"未知实体类型：{entity_type}。"

        if entity_type:
            entities = self.store.list_entities(entity_type)
            if not entities:
                return f"还没有记录{ENTITY_TYPES[entity_type]}。"
            return self._format_group(entity_type, entities)

        all_entities = self.store.list_entities()
        if not all_entities:
            return "还没有记录任何实体。"

        lines = []
        for current_type in ENTITY_TYPES:
            entities = [
                entity for entity in all_entities
                if entity["type"] == current_type
            ]
            if entities:
                lines.append(self._format_group(current_type, entities))
        return "\n".join(lines)

    @staticmethod
    def _format_group(entity_type, entities):
        label = ENTITY_TYPES[entity_type]
        parts = []
        for entity in entities:
            parts.append(EntityService._format_entity(
                entity["canonical_name"], entity["aliases"], label
            ))
        return f"{label}：" + "；".join(parts)

    @staticmethod
    def _format_entity(canonical_name, aliases, label=None):
        if aliases:
            return f"{canonical_name}（别名：{'、'.join(sorted(set(aliases)))}）"
        return canonical_name

    def _audit(self, action_type, payload, result):
        self.store.record_proposal(action_type, payload, "committed", result)
