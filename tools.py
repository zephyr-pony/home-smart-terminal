"""Agent 工具注册表 - 标准 Tool 定义（OpenAI function calling 协议）

每个工具 = {"schema": OpenAI JSON Schema（给 LLM 看）, "execute": 可调用函数}
模块自治：memory/items 各自导出工具定义，assistant.py 聚合注册。
新增工具只需在此处注册，agent loop 核心循环无需改动。
"""
from datetime import datetime

from memory import Memory


def _build(name, description, properties, required, execute):
    """构造一个标准工具条目。"""
    return {
        "schema": {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        },
        "execute": execute,
    }


def get_memory_tools(memory: Memory):
    """memory 相关工具（4 件套）。memory 由调用方传入（保持单例）。"""
    return [
        _build(
            name="memory_search",
            description=(
                "搜索家庭记忆库，返回相关记忆片段（每条带 [存储日期] 前缀）。"
                "用于回答用户关于家庭琐事的问题，如'酸奶还有几天过期''上次换鼠标电池是什么时候'。"
                "keywords 用空格分隔多个关键词，如'酸奶 过期'。"
            ),
            properties={
                "keywords": {
                    "type": "string",
                    "description": "搜索关键词，空格分隔，如 '酸奶 过期'",
                },
                "n_results": {
                    "type": "integer",
                    "description": "返回条数，默认 5",
                },
            },
            required=["keywords"],
            execute=lambda keywords, n_results=5: _fmt_memories(memory.search(keywords, n_results)),
        ),
        _build(
            name="memory_store",
            description=(
                "存储一条新记忆到家庭记忆库。当用户陈述家庭事实（如'明天交电费''冰箱里还有三个鸡蛋'）时调用。"
                "text 为原文，summary 为精简摘要（15 字内，相对时间转绝对日期，如'8月8日'）。"
            ),
            properties={
                "text": {"type": "string", "description": "用户原话"},
                "summary": {"type": "string", "description": "精简摘要，含绝对日期"},
            },
            required=["text", "summary"],
            execute=lambda text, summary: memory.store(text, summary) or "已存储",
        ),
        _build(
            name="memory_list",
            description="列出家庭记忆库最近的记忆（按时间倒序）。用户问'看看记忆''记了什么'时调用。",
            properties={
                "n": {"type": "integer", "description": "返回条数，默认 10"},
            },
            required=[],
            execute=lambda n=10: _fmt_memories(memory.list_all(n)),
        ),
        _build(
            name="memory_delete",
            description="按关键词删除记忆库中匹配的记忆。用户说'忘掉X''删掉X'时调用。返回被删除的记录。",
            properties={
                "keywords": {"type": "string", "description": "要删除的记忆关键词，如 '酸奶'"},
            },
            required=["keywords"],
            execute=lambda keywords: (
                lambda deleted: f"已删除 {len(deleted)} 条关于 {keywords} 的记忆" if deleted
                else f"没有找到关于 {keywords} 的记忆"
            )(memory.delete_by_keywords(keywords)),
        ),
    ]


def get_current_date_tool():
    """获取当前日期工具（时间推理必需）。"""
    return _build(
        name="get_current_date",
        description=(
            "获取今天的日期（如 2026年8月8日）。当用户问'还有几天''什么时候''过期'等时间相关问题时，"
            "必须先调用本工具拿到今天日期，再结合记忆/物品日期计算。"
        ),
        properties={},
        required=[],
        execute=lambda: f"今天是 {datetime.now().strftime('%Y年%m月%d日')}",
    )


# ---------- 格式化辅助 ----------

def _fmt_memories(memories):
    """记忆条目转文本（dict 或 str 都兼容）。"""
    if not memories:
        return "（没有相关记忆）"
    if isinstance(memories[0], dict):
        return "\n".join(f"[{m['time']}] {m['text']}" for m in memories)
    return "\n".join(str(m) for m in memories)


def get_items_tools(items):
    """物品管理工具（4 件套，阶段 B 加入）。items 由调用方传入（保持单例）。"""
    def _fmt_items(item_list):
        if not item_list:
            return "（暂无物品记录）"
        lines = []
        for it in item_list:
            exp = it.get("expiry_date") or "未知"
            left = it.get("days_left")
            if left is not None:
                exp = f"{exp}（{'已过期' + str(-left) + '天' if left < 0 else '还剩' + str(left) + '天'}）"
            lines.append(f"- {it['name']}：到期 {exp}")
        return "\n".join(lines)

    return [
        _build(
            name="item_add",
            description=(
                "添加一个会过期的物品（食品/药品等）到物品清单。"
                "用户说'酸奶8月5号过期''鸡蛋下周三到期'这类话时调用。"
                "expiry_date 必须转成 YYYY-MM-DD 格式（如 2026-08-05）。"
            ),
            properties={
                "name": {"type": "string", "description": "物品名称，如 '酸奶'"},
                "expiry_date": {"type": "string", "description": "到期日期 YYYY-MM-DD，如 2026-08-05"},
            },
            required=["name"],
            execute=lambda name, expiry_date=None: (
                f"已添加物品：{name}，到期日 {expiry_date or '未知'}"
                if (items.add(name, expiry_date) and True) else ""
            ),
        ),
        _build(
            name="item_search",
            description="按名称查找物品清单。用户问'酸奶还有几天过期''冰箱里有什么东西'时调用。",
            properties={
                "keyword": {"type": "string", "description": "物品名称关键词，如 '酸奶'"},
            },
            required=["keyword"],
            execute=lambda keyword: _fmt_items(items.search(keyword)),
        ),
        _build(
            name="item_list",
            description="列出全部物品。用户问'我记了哪些东西''物品清单'时调用。",
            properties={},
            required=[],
            execute=lambda: _fmt_items(items.list_all()),
        ),
        _build(
            name="item_delete",
            description="删除物品。用户说'删掉酸奶''不要酸奶了'时调用。返回被删除的物品。",
            properties={
                "name": {"type": "string", "description": "要删除的物品名称"},
            },
            required=["name"],
            execute=lambda name: (
                lambda deleted: f"已删除 {len(deleted)} 个物品：{', '.join(d['name'] for d in deleted)}"
                if deleted else f"没有找到物品：{name}"
            )(items.delete(name)),
        ),
    ]


def get_all_tools(memory: Memory, items=None):
    """聚合全部工具（memory 4 件套 + 日期 + 可选 items 4 件套）。"""
    tools = get_memory_tools(memory) + [get_current_date_tool()]
    if items is not None:
        tools += get_items_tools(items)
    return tools