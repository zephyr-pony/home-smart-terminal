"""Agent 工具注册表 - 标准 Tool 定义（OpenAI function calling 协议）

每个工具 = {"schema": OpenAI JSON Schema（给 LLM 看）, "execute": 可调用函数}
模块自治：memory/items 各自导出工具定义，assistant.py 聚合注册。
新增工具只需在此处注册，agent loop 核心循环无需改动。
"""
from datetime import datetime

from agent import wording
from domain.facts import profile_subject
from memory import Memory


def _clamp_int(value, default, low, high):
    """把 LLM 传来的数值参数限制在安全范围内（缺省/非法值回退）。"""
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return max(low, min(value, high))


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


def get_memory_tools(memory: Memory, proposals=None):
    """memory 相关工具（4 件套）。memory 由调用方传入（保持单例）。"""
    def store_memory(text, summary):
        if proposals is not None:
            return proposals.store_memory(text, summary)
        memory.store(text, summary)
        return "已存储"

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
            execute=lambda keywords, n_results=5: _fmt_memories(
                memory.search(keywords, _clamp_int(n_results, 5, 1, 50))
            ),
        ),
        _build(
            name="memory_store",
            description=(
                "存储一条新记忆到家庭记忆库。当用户陈述家庭事实（如'明天交电费''冰箱里还有三个鸡蛋'）时调用。"
                "text 为原文；summary 是精简摘要（15 字内），只能删掉原话里的字，不要改写、不要换算日期。"
            ),
            properties={
                "text": {"type": "string", "description": "用户原话"},
                "summary": {"type": "string", "description": "精简摘要，只删原话的字，不加内容"},
            },
            required=["text", "summary"],
            execute=store_memory,
        ),
        _build(
            name="memory_list",
            description="列出家庭记忆库最近的记忆（按时间倒序）。用户问'看看记忆''记了什么'时调用。",
            properties={
                "n": {"type": "integer", "description": "返回条数，默认 10"},
            },
            required=[],
            execute=lambda n=10: _fmt_memories(
                memory.list_all(_clamp_int(n, 10, 1, 50))
            ),
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
            "兜底用的日期工具。系统提示里已经给出了今天的日期，"
            "常规时间计算直接用它即可，不要为了拿日期专门调用本工具。"
        ),
        properties={},
        required=[],
        execute=lambda: f"今天是 {datetime.now().strftime('%Y年%m月%d日')}",
    )


def get_weather_tool(weather):
    """天气工具：实时 + 未来几天，位置缺省用家庭所在城市。"""
    return _build(
        name="weather_query",
        description=(
            "查天气。scope 决定范围："
            "now=此刻实时（气温、体感、风向风力、湿度、能见度、气压等）；"
            "today=今天一整天（最高最低温、白天夜间天气、紫外线、日出日落）；"
            "forecast=未来几天，不含今天（days 指定天数，默认 3）；"
            "不传 scope 则三段都给。"
            "用户问「现在」用 now，问「今天/穿什么/要不要带伞」用 today，问「明天/这周」用 forecast。"
            "location 不传就查家里所在城市，只有问别的城市时才传城市名。"
        ),
        properties={
            "location": {
                "type": "string",
                "description": "城市名，如'烟台'；不传则查本地",
            },
            "scope": {
                "type": "string",
                "enum": ["now", "today", "forecast", "all"],
                "description": "查询范围：now=此刻 / today=今天 / forecast=未来几天 / all=全部",
            },
            "days": {
                "type": "integer",
                "description": "scope=forecast 时的天数，默认 3",
            },
        },
        required=[],
        execute=lambda location="", scope="all", days=3: weather.query(
            location, scope, days
        ),
    )


# ---------- 格式化辅助 ----------

def _fmt_memories(memories):
    """记忆条目转文本（dict 或 str 都兼容）。"""
    if not memories:
        return "（没有相关记忆）"
    if isinstance(memories[0], dict):
        return "\n".join(f"[{m['time']}] {m['text']}" for m in memories)
    return "\n".join(str(m) for m in memories)


def _add_item(items, name, expiry_date=None, proposals=None):
    if proposals is not None:
        return proposals.add_item(name, expiry_date)
    try:
        item = items.add(name, expiry_date)
    except ValueError as exc:
        return f"无法添加物品：{exc}"
    return f"已添加物品：{item['name']}，到期日 {item['expiry_date'] or '未知'}"


def get_items_tools(items, proposals=None):
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
            execute=lambda name, expiry_date=None: _add_item(
                items, name, expiry_date, proposals
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
            description=(
                "删除物品。用户说'删掉酸奶''不要酸奶了'时调用。"
                "只删名称完全相同的物品，不做模糊匹配：删「酸奶」不会碰「酸奶机」。"
                "找不到同名物品时返回清单里的相似名称，不要自己改名字重试。"
            ),
            properties={
                "name": {"type": "string", "description": "要删除的物品名称，用清单里的全名"},
            },
            required=["name"],
            execute=lambda name: _delete_item(items, name),
        ),
    ]


def _delete_item(items, name):
    """删物品：精确同名才动，相似项只作为提示。"""
    name = str(name or "").strip()
    deleted = items.delete_exact(name)
    if deleted:
        return "已删除 %d 个物品：%s" % (
            len(deleted), "、".join(d["name"] for d in deleted),
        )
    candidates = [row["name"] for row in items.search(name)][:5]
    if candidates:
        return "没有叫「%s」的物品，什么都没删。清单里有：%s。要删哪个请说完整名称。" % (
            name, "、".join(candidates),
        )
    return "没有找到「%s」这个物品，什么都没删。" % name


def get_entity_tools(entities):
    """实体管理工具：新增、改名、别名、列表。"""
    entity_type_schema = {
        "type": "string",
        "enum": ["property", "person", "product", "provider", "place", "device"],
        "description": "实体类型：property 住所 / person 人 / product 物品 / provider 缴费单位 / place 地点 / device 设备",
    }

    return [
        _build(
            name="entity_add",
            description=(
                "新增家庭实体，或给已有实体加别名。"
                "用户说'把爸妈家也叫父母家''新建一个房子叫老家'时调用。"
            ),
            properties={
                "type": entity_type_schema,
                "canonical_name": {
                    "type": "string",
                    "description": "规范名称，如 '爸妈家'",
                },
                "aliases": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "别名列表，如 ['父母家', '我爸妈家']",
                },
            },
            required=["type", "canonical_name"],
            execute=lambda type, canonical_name, aliases=None: entities.add_entity(
                type, canonical_name, aliases
            ),
        ),
        _build(
            name="entity_rename",
            description=(
                "修改实体规范名。用户说'把爸妈家改名叫父母家'时调用。"
                "旧名字会自动保留为别名。"
            ),
            properties={
                "type": entity_type_schema,
                "old_name": {"type": "string", "description": "旧名称或别名"},
                "new_name": {"type": "string", "description": "新规范名"},
            },
            required=["type", "old_name", "new_name"],
            execute=lambda type, old_name, new_name: entities.rename_entity(
                type, old_name, new_name
            ),
        ),
        _build(
            name="entity_list",
            description=(
                "列出已记录的家庭实体和别名。用户问'看看我记了哪些房子/人/地点'时调用。"
                "不传 type 时列出全部。"
            ),
            properties={
                "type": entity_type_schema,
            },
            required=[],
            execute=lambda type=None: entities.list_entities(type),
        ),
    ]


def _recur_label(task):
    """周期的人话标签：每天 / 每周三 / 每月5号。"""
    return wording.recur_label(task.get("recur"), task.get("due_at"))


def _fmt_tasks(tasks):
    if not tasks:
        return "（暂无任务）"
    lines = []
    for task in tasks:
        due = task.get("due_at") or "未定"
        label = _recur_label(task)
        head = f"- [{task['id']}] {task['title']}：{due}"
        if label:
            head += "，%s" % label
        lines.append(f"{head}（{task['status']}）")
    return "\n".join(lines)


def _fmt_facts(facts):
    if not facts:
        return "（暂无结构化事实）"
    lines = []
    for fact in facts:
        source = f"（来源：{fact['source']}）" if fact.get("source") else ""
        lines.append(
            f"- {fact['subject']} · {fact['predicate']}：{fact['value']}{source}"
        )
    return "\n".join(lines)


def get_task_tools(tasks, proposals=None):
    """任务与提醒工具：账单/日程/自定义提醒（支持每天/每周/每月）。"""
    def add_task(title, due_at=None, type="custom", recur=""):
        if proposals is not None:
            return proposals.add_task(title, due_at, type, recur=recur)
        try:
            task = tasks.add(title, due_at, type, recur=recur)
        except ValueError as exc:
            return f"无法创建任务：{exc}"
        label = _recur_label(task)
        when = task["due_at"] or "未定"
        return (
            f"已创建任务 #{task['id']}：{task['title']}，"
            f"时间 {when}" + (f"（{label}）" if label else "")
        )

    def complete_task(task_id):
        task_id = _clamp_int(task_id, 0, 0, 10 ** 9)
        if not task_id:
            return "任务 ID 无效"
        if tasks.complete(task_id):
            task = tasks.get(task_id)
            if task and task.get("recur"):
                return "任务 #%d 这次记下了，下次 %s（%s）" % (
                    task_id, task.get("due_at") or "未定", _recur_label(task),
                )
            return f"任务 #{task_id} 已完成"
        return f"没有找到未完成的任务 #{task_id}"

    def update_task(task_id, title=None, due_at=None, type=None):
        task_id = _clamp_int(task_id, 0, 0, 10 ** 9)
        if not task_id:
            return "任务 ID 无效"
        if title is None and due_at is None and type is None:
            return "没有要修改的内容"
        try:
            changed = tasks.update(task_id, title=title, due_at=due_at, type=type)
        except ValueError as exc:
            return f"无法修改任务：{exc}"
        if not changed:
            return f"没有找到任务 #{task_id}"
        parts = []
        if title is not None:
            parts.append(f"标题「{title}」")
        if due_at is not None:
            parts.append(f"时间 {due_at or '清空'}")
        if type is not None:
            parts.append(f"类型 {type}")
        return f"任务 #{task_id} 已更新：{'，'.join(parts)}"

    def cancel_task(task_id):
        task_id = _clamp_int(task_id, 0, 0, 10 ** 9)
        if not task_id:
            return "任务 ID 无效"
        if tasks.cancel(task_id):
            return f"任务 #{task_id} 已取消"
        return f"没有找到可取消的任务 #{task_id}"

    def postpone_task(task_id, days=None, due_at=None):
        task_id = _clamp_int(task_id, 0, 0, 10 ** 9)
        if not task_id:
            return "任务 ID 无效"
        try:
            if due_at:
                task = tasks.postpone(task_id, due_at=due_at)
            else:
                days = _clamp_int(1 if days is None else days, 1, 1, 180)
                task = tasks.postpone(task_id, days=days)
        except ValueError as exc:
            return f"无法延期：{exc}"
        if task is None:
            return f"没有找到可延期的任务 #{task_id}"
        return "任务 #%d 已改到 %s" % (task_id, task.get("due_at") or "未定")

    return [
        _build(
            name="task_add",
            description=(
                "创建账单、日程或自定义提醒。用户说'明天交电费''下周三开会'"
                "'提醒我周五取快递'时调用。due_at 转成 YYYY-MM-DD 或 "
                "YYYY-MM-DDTHH:MM。周期提醒（'每天八点吃药''每周三下午三点开会'"
                "'每月1号交房租'）把 recur 设为 daily/weekly/monthly。"
            ),
            properties={
                "type": {
                    "type": "string",
                    "enum": ["bill", "schedule", "custom"],
                    "description": "bill 账单 / schedule 日程 / custom 自定义提醒",
                },
                "title": {"type": "string", "description": "任务标题，如 '交电费'"},
                "due_at": {
                    "type": "string",
                    "description": "下一次提醒时间，YYYY-MM-DD 或 YYYY-MM-DDTHH:MM",
                },
                "recur": {
                    "type": "string",
                    "enum": ["daily", "weekly", "monthly"],
                    "description": "周期：每天/每周/每月；一次性任务不要传",
                },
            },
            required=["title"],
            execute=add_task,
        ),
        _build(
            name="task_list",
            description="列出任务。用户问'我有哪些提醒''还有什么没做'时调用。",
            properties={
                "status": {
                    "type": "string",
                    "enum": ["pending", "reminded", "done", "cancelled"],
                    "description": "按状态筛选，不传则全部",
                },
            },
            required=[],
            execute=lambda status=None: _fmt_tasks(tasks.list_all(status)),
        ),
        _build(
            name="task_complete",
            description="把任务标记为完成。用户说'电费交了''会议开完了'时调用。",
            properties={
                "task_id": {"type": "integer", "description": "任务 ID"},
            },
            required=["task_id"],
            execute=complete_task,
        ),
        _build(
            name="task_update",
            description=(
                "修改已存在的任务。用户说'取快递改到明天''那个提醒换个名字'"
                "'把电费改成账单'时调用。先用 task_list 拿到 task_id。"
                "due_at 转成 YYYY-MM-DD 或 YYYY-MM-DDTHH:MM。"
            ),
            properties={
                "task_id": {"type": "integer", "description": "任务 ID"},
                "title": {"type": "string", "description": "新标题，不改则省略"},
                "due_at": {
                    "type": "string",
                    "description": "新时间，YYYY-MM-DD 或 YYYY-MM-DDTHH:MM；只改时间才传",
                },
                "type": {
                    "type": "string",
                    "enum": ["bill", "schedule", "custom"],
                    "description": "新类型，不改则省略",
                },
            },
            required=["task_id"],
            execute=update_task,
        ),
        _build(
            name="task_cancel",
            description="取消任务（不删除记录）。用户说'这个提醒不用了''取消下周的会议'时调用。",
            properties={
                "task_id": {"type": "integer", "description": "任务 ID"},
            },
            required=["task_id"],
            execute=cancel_task,
        ),
        _build(
            name="task_postpone",
            description=(
                "把任务往后顺延。用户说'这个提醒明天再说''三天后再提醒我'"
                "'下周一再提醒'时调用，先用 task_list 拿到 task_id。"
                "days 可以省略：系统会按用户原话换算成天数。"
            ),
            properties={
                "task_id": {"type": "integer", "description": "任务 ID"},
                "due_at": {
                    "type": "string",
                    "description": "改到哪一天，YYYY-MM-DD（可省略，系统按原话换算）",
                },
                "days": {
                    "type": "integer",
                    "description": "没有具体日期时顺延几天（可省略，默认 1）",
                },
            },
            required=["task_id"],
            execute=postpone_task,
        ),
    ]


def get_fact_tools(facts, proposals=None):
    """结构化事实工具：户号、地址、品牌、型号、偏好等。"""
    def store_fact(subject, predicate, value, source=None):
        if proposals is not None:
            return proposals.store_fact(subject, predicate, value, source)
        try:
            fact = facts.store(subject, predicate, value, source)
        except ValueError as exc:
            return f"无法保存事实：{exc}"
        message = f"已保存：{fact['subject']} · {fact['predicate']} = {fact['value']}"
        replaced = fact.get("replaced") or []
        if replaced:
            message += "（原值 %s 已失效）" % "、".join(replaced)
        return message

    def search_facts(query=None, subject=None, predicate=None):
        return _fmt_facts(facts.search(query, subject, predicate))

    return [
        _build(
            name="fact_store",
            description=(
                "保存结构化事实，如户号、地址、品牌、型号、偏好。"
                "用户说'爸妈家电费户号是123''牛奶是蒙牛的'时调用。"
                "只记用户自己说出来的；你从对话里推断的偏好用 profile_set。"
                "同一主体+属性再次保存会关闭旧值、保留历史。"
            ),
            properties={
                "subject": {"type": "string", "description": "主体，如 '爸妈家'；某个人的偏好用 '用户' 或 '用户/妈妈'"},
                "predicate": {"type": "string", "description": "属性，如 '电费户号'"},
                "value": {"type": "string", "description": "值，如 '123456'"},
                "source": {"type": "string", "description": "来源，可选"},
            },
            required=["subject", "predicate", "value"],
            execute=store_fact,
        ),
        _build(
            name="fact_search",
            description="查询结构化事实。用户问户号、地址、品牌、型号时调用。",
            properties={
                "query": {"type": "string", "description": "关键词"},
                "subject": {"type": "string", "description": "主体筛选，可选"},
                "predicate": {"type": "string", "description": "属性筛选，可选"},
            },
            required=[],
            execute=search_facts,
        ),
        _build(
            name="fact_list",
            description="列出最近保存的结构化事实。",
            properties={},
            required=[],
            execute=lambda: _fmt_facts(facts.list_all()),
        ),
    ]


def get_profile_tools(facts, proposals=None):
    """用户画像：只装「模型推断出的偏好」，必须先问用户（D59）。

    为什么不和 fact_store 合并：fact_store 记的是用户自己说出来的事实
    （「我妈不吃辣」），猜错也是用户自己说的；这条是模型从对话里推测的
    （「用户可能喜欢简短回答」），猜错会一直污染后续对话，所以必须让用户
    点头。确认由 assistant 无条件拦下，不依赖 CONFIRM_WRITE_ACTIONS。
    """

    def set_profile(subject, predicate, value, evidence=""):
        subject = profile_subject(subject)
        predicate = str(predicate or "").strip()
        value = str(value or "").strip()
        evidence = str(evidence or "").strip()
        if not predicate or not value:
            return "无法保存画像：方面和内容都不能为空"
        if proposals is not None:
            return proposals.store_profile(subject, predicate, value, evidence)
        source = "模型推断"
        if evidence:
            source += "：%s" % evidence[:80]
        try:
            fact = facts.store(subject, predicate, value, source)
        except ValueError as exc:
            return f"无法保存画像：{exc}"
        return "已保存画像：%s · %s = %s" % (
            fact["subject"], fact["predicate"], fact["value"],
        )

    return [
        _build(
            name="profile_set",
            description=(
                "记录你从对话里推断出来的用户偏好、习惯、口味、称呼或沟通风格。"
                "只在用户没有直接说明、而是你推测出来时用；"
                "用户明确说出的事实（如「我妈不吃辣」）用 fact_store。"
                "这个工具保存前会自动向用户确认，调用后不用再自己问一遍。"
            ),
            properties={
                "subject": {
                    "type": "string",
                    "description": "这是谁的画像：当前说话人填「用户」，别人填「用户/妈妈」这样的名字",
                },
                "predicate": {
                    "type": "string",
                    "description": "哪个方面，如 口味 / 作息 / 称呼 / 沟通风格 / 偏好",
                },
                "value": {
                    "type": "string",
                    "description": "你推断出的内容，一句话说清",
                },
                "evidence": {
                    "type": "string",
                    "description": "依据：用户哪句话让你这么判断，引用原话片段",
                },
            },
            required=["subject", "predicate", "value"],
            execute=set_profile,
        ),
    ]


def get_family_search_tool(memory, items, tasks=None):
    """一次查完物品 / 记忆 / 任务。

    语音链路里每一次工具调用都要多跑一轮 LLM（约 2 秒），
    「先查物品、查不到再查记忆」会把一轮查询变成三轮。
    """

    def format_items(rows):
        if not rows:
            return ""
        lines = []
        for it in rows:
            expiry = it.get("expiry_date") or "未知"
            left = it.get("days_left")
            if left is not None:
                expiry = "%s（%s）" % (
                    expiry,
                    ("已过期%d天" % -left) if left < 0 else ("还剩%d天" % left),
                )
            lines.append("- %s：到期 %s" % (it.get("name", ""), expiry))
        return "\n".join(lines)

    def run(keyword=""):
        keyword = str(keyword or "").strip()
        sections = []

        item_rows = items.search(keyword) if keyword else items.list_all()
        if item_rows:
            sections.append("【物品】\n" + format_items(item_rows))

        memories = memory.search(keyword, 5) if keyword else memory.list_all(5)
        if memories:
            sections.append("【记忆】\n" + "\n".join("- " + str(m) for m in memories))

        if tasks is not None:
            task_rows = tasks.list_all("pending")
            if keyword:
                task_rows = [
                    t for t in task_rows
                    if keyword in str(t.get("title", ""))
                ]
            if task_rows:
                sections.append("【待办】\n" + "\n".join(
                    "- %s（%s）" % (t.get("title", ""), t.get("due_at") or "无日期")
                    for t in task_rows
                ))

        return "\n".join(sections) if sections else "（没有找到相关记录）"

    return _build(
        name="family_search",
        description=(
            "一次查完物品清单、家庭记忆和待办任务。"
            "用户问'X还有几天过期''家里有什么''记过什么''我有哪些提醒'时用这个，"
            "不要分别调用 item_search / memory_search / task_list。"
            "keyword 留空则列出全部。"
        ),
        properties={
            "keyword": {
                "type": "string",
                "description": "要查的关键词，如 '牛奶'；留空列出全部",
            },
        },
        required=[],
        execute=run,
    )


def get_home_tools(home):
    """智能设备查询（Home Assistant / 米家）。

    一次调用就同时给「有没有这台设备」和「它现在什么状态」——语音链路里每次
    多一个工具调用就多跑一轮 LLM（约 2 秒），拆成 search + status 会翻倍。
    """

    def query(keyword=""):
        return home.query(keyword)

    tools = [
        _build(
            name="home_query",
            description=(
                "查家里的智能设备状态（灯、空调、净水器滤芯、摄像头……）。"
                "用户问'客厅的灯开着吗''空调设的多少度''净水器滤芯还剩多少'时用。"
                "keyword 用设备名或房间名，如'客厅灯''净水器'；留空列出全部设备。"
                "这个工具只能查，不能操作设备。"
            ),
            properties={
                "keyword": {
                    "type": "string",
                    "description": "设备名或房间名，如 '客厅灯'、'净水器'；留空列出全部",
                },
            },
            required=[],
            execute=query,
        ),
    ]

    if getattr(home, "control_enabled", False):
        def control(device, action, value=None):
            return home.control(device, action, value)

        tools.append(_build(
            name="home_control",
            description=(
                "操作家里的智能设备（开/关灯、开关空调、调温度、开关窗帘、扫地机清扫或回充）。"
                "device 用设备名或 entity_id，如'客厅灯'。"
                "action 取值：on（开）/ off（关）/ set_temperature（调温度，配 value=摄氏度）/ "
                "set_brightness（调亮度，配 value=百分比）/ open / close / start / stop / dock。"
                "门锁、燃气、热水器这类**不支持**，要如实告诉用户去米家 App 操作。"
                "同名设备有多个时工具会返回候选，需要反问用户要哪一个。"
            ),
            properties={
                "device": {"type": "string", "description": "设备名或 entity_id，如 '客厅灯'"},
                "action": {
                    "type": "string",
                    "description": "on / off / set_temperature / set_brightness / open / close / start / stop / dock",
                },
                "value": {
                    "type": "number",
                    "description": "set_temperature 是摄氏度，set_brightness 是百分比；其它动作可省略",
                },
            },
            required=["device", "action"],
            execute=control,
        ))

    return tools


def get_camera_tool():
    """看一眼前方：拍照这一动作在设备端，服务端只负责说「要看」。

    真正的照片走小智那条链：我们把调用转成 tool_calls，设备拍完直接播报画面描述。
    这里留的 execute 只服务于没有引摄像头的入口（CLI / 网页），说明看不了就行，
    免得模型编一段「我看到……」出来。
    """
    def look(question=""):
        return "这个入口没有摄像头，看不到画面。"

    return _build(
        name="camera_look",
        description=(
            "看一眼前方：让客厅终端拍一张照片，再看画面回答。"
            "用户说'看看''这是什么''桌上有什么''冰箱里还有牛奶吗'这类需要看的时候用它。"
            "只有用户明确要看某个东西时才调用，打招呼、闲聊、问时间都不要拍照。"
            "决定要看就直接调用，不要先说话——话一出口就播出去了，收不回来。"
            "参数 question 写清楚要在画面里确认什么；画面描述会直接念给用户听。"
        ),
        properties={
            "question": {
                "type": "string",
                "description": "要在画面里确认什么，例如 '桌上有什么'、'这个是不是牛奶'",
            },
        },
        required=["question"],
        execute=look,
    )


def get_printer_tool():
    """打印这一轮用户上传的文件（有打印机时把 schema 给模型）。

    真正的出纸要等用户确认，由 assistant 拦截后走 ConfirmationManager；
    这里的 execute 只是兜底，防止模型在没有文件时调用。
    """
    return _build(
        name="printer_print",
        description=(
            "打印这一轮用户上传的文件（PDF / 图片 / 文本 / Office）。"
            "只有用户附带了文件时才可用；打印前系统会自动向用户确认，调用后不用再问。"
            "不知道文件里是什么就先调 file_read；要改内容或排版就先调 file_process，"
            "它产出的新文件才是这一步要打印的文件。不要编造文件里写了什么。"
        ),
        properties={
            "copies": {
                "type": "integer",
                "description": "打印几份，默认 1",
            },
            "pages": {
                "type": "string",
                "description": "页码范围，如 '1-3,5'；不传打印全部页",
            },
        },
        required=[],
        execute=lambda **kwargs: "这一轮没有附带文件，无法打印。",
    )


def get_file_tools():
    """读取/处理这一轮上传的文件，外加一份「这台机器有什么工具」的清单（D62）。

    为什么不内置「转格式」「改列宽」「两列相乘」这类工具：每个需求都加一个，
    工具的数量会跟着用户的想象无限长。这里只给三样通用的东西：
    - file_read：把文件内容变成模型能读的文本；
    - find_tools：查这台机器装了哪些命令行工具和 Python 包；
    - run_python：一段 Python，模型自己 import / 调 soffice / 算东西，
      产出新文件后自动成为这一步的打印文件。
    具体用什么库、走哪条命令，由模型自己看着办。
    """
    from adapters import documents

    return [
        _build(
            name="file_read",
            description=(
                "读取这一轮用户上传文件的内容。表格返回工作表名、行列数和前 40 行"
                "（带列字母，如 A=项目、B=一月），PDF 返回正文，Word 返回文字。"
                "用户问文件里有什么、要按列运算或改版式之前，先调用它。"
            ),
            properties={},
            required=[],
            execute=lambda **kwargs: "这一轮没有附带文件。",
        ),
        _build(
            name="find_tools",
            description=(
                "查这台服务器上装了哪些命令行工具和 Python 包（如 soffice、pdftotext、"
                "gs、openpyxl）。做格式转换、拆合 PDF、算数、画图之前先查一下，"
                "按查到的能力自己选，不要凭印象假设某个工具存在。"
            ),
            properties={
                "query": {
                    "type": "string",
                    "description": "关键词，如 'pdf'、'office'、'image'；不传列全部",
                },
            },
            required=[],
            execute=lambda **kwargs: documents.inventory(kwargs.get("query")),
        ),
        _build(
            name="run_python",
            description=(
                "跑一段 Python 处理附件（或从零生成一个文件），产出新文件用于打印。"
                "输入路径在环境变量 XIAOMA_IN（没有附件时为空字符串），把结果存到 "
                "环境变量 XIAOMA_OUT 指向的**文件路径**（它不是一个目录，直接 "
                "wb.save(os.environ['XIAOMA_OUT']) 就行）；运行完这个文件自动成为"
                "这一步的打印文件。XIAOMA_IN 始终是用户上传的原件，不会因为上一步"
                "的产物而改变。"
                "可以用容器里已装的库和命令行（先 find_tools 查），也可以用 subprocess "
                "调 soffice 转格式。没有网络。"
                "用户只是问内容时用 file_read 就够，不必跑代码。"
            ),
            properties={
                "code": {
                    "type": "string",
                    "description": "要执行的 Python 代码；读写 XIAOMA_IN / XIAOMA_OUT",
                },
                "output_name": {
                    "type": "string",
                    "description": (
                        "输出文件名（含扩展名，如 报价单_改.xlsx、结果.pdf）；"
                        "不传沿用输入文件的扩展名"
                    ),
                },
            },
            required=["code"],
            execute=lambda **kwargs: "这一轮没有附带文件。",
        ),
    ]


def get_capability_tools():
    """上网、装环境、跑命令：通用能力，不针对某个需求（D62）。

    用户的原话是「就像现在我跟你一样」——查资料、找工具、装环境、跑起来。
    这里只给通道，具体用哪个库、走哪条命令，由模型自己判断。
    """
    from adapters import shell as shell_mod
    from adapters import web

    def do_search(**kwargs):
        try:
            results = web.search(kwargs.get("query"), kwargs.get("n") or 5)
        except web.WebError as exc:
            return "错误：%s" % exc
        return web.format_results(results)

    def do_fetch(**kwargs):
        try:
            return web.fetch(kwargs.get("url"))
        except web.WebError as exc:
            return "错误：%s" % exc

    def do_shell(**kwargs):
        try:
            result = shell_mod.run(kwargs.get("command"), kwargs.get("timeout"))
        except shell_mod.ShellError as exc:
            return "错误：%s" % exc
        return shell_mod.format_result(result)

    return [
        _build(
            name="web_search",
            description=(
                "搜网页（必应优先，DuckDuckGo 兜底），拿到标题、链接和摘要。"
                "遇到不认识的格式、要找现成的库或工具时用；支持 site: 限定站点，"
                "如 site:github.com、site:pypi.org、site:modelscope.cn、site:docs.python.org。"
                "搜到的地址可以用 web_fetch 打开看细节。"
            ),
            properties={
                "query": {"type": "string", "description": "搜索词"},
                "n": {"type": "integer", "description": "返回几条，默认 5，最多 10"},
            },
            required=["query"],
            execute=do_search,
        ),
        _build(
            name="web_fetch",
            description=(
                "打开一个网页，返回去掉标签的正文（最多约 6000 字）。"
                "用于看文档、README、报错页；只支持 http/https，不抓内网地址。"
            ),
            properties={
                "url": {"type": "string", "description": "http/https 地址"},
            },
            required=["url"],
            execute=do_fetch,
        ),
        _build(
            name="run_shell",
            description=(
                "在服务器（Linux 容器）里执行 shell 命令，用来装环境和跑现成的工具。"
                "工作目录 /data/work 会保留；pip install --user 装到 /data/home，重启还在。"
                "当前用户不是 root，装不了 apt 系统包；缺系统包时如实告诉用户。"
                "默认 90 秒超时；下载、编译这种慢活拆小一点或显式传 timeout。"
            ),
            properties={
                "command": {"type": "string", "description": "要执行的命令"},
                "timeout": {
                    "type": "integer",
                    "description": "超时秒数，默认 90，最大 600",
                },
            },
            required=["command"],
            execute=do_shell,
        ),
    ]


def get_thinking_tool():
    """让模型自己决定要不要开思考（D63）。默认关，省下语音场景的几秒。

    真正切换在 assistant 的 agent loop 里；这个 execute 只是兜底。
    """
    return _build(
        name="set_thinking",
        description=(
            "打开/关闭深度思考。问题需要多步推理、写代码、算表格、"
            "比较方案或拿不准时，先调用 set_thinking(true) 再动手；"
            "带附件要改文件、要打印、算法不止一步时，第一步就该开。"
            "闲聊、查一条记录、时间天气这类不必开，直接回答。"
            "开启后本轮后续步骤会慢几秒。"
        ),
        properties={
            "enabled": {
                "type": "boolean",
                "description": "true 打开，false 关闭",
            },
            "why": {
                "type": "string",
                "description": "为什么需要/不需要，一句话",
            },
        },
        required=["enabled"],
        execute=lambda **kwargs: "思考开关由编排层处理。",
    )


def get_all_tools(memory: Memory, items=None, proposals=None, entities=None,
                  tasks=None, facts=None, home=None, weather=None):
    """聚合全部工具（memory + 日期 + items + 实体 + 任务 + 事实 + 设备 + 天气）。"""
    tools = get_memory_tools(memory, proposals) + [get_current_date_tool()]
    if items is not None:
        tools += get_items_tools(items, proposals)
        tools.append(get_family_search_tool(memory, items, tasks))
    if entities is not None:
        tools += get_entity_tools(entities)
    if tasks is not None:
        tools += get_task_tools(tasks, proposals)
    if facts is not None:
        tools += get_fact_tools(facts, proposals)
        tools += get_profile_tools(facts, proposals)
    if home is not None:
        tools += get_home_tools(home)
    if weather is not None and getattr(weather, "configured", False):
        tools.append(get_weather_tool(weather))
    # 摄像头只在带摄像头的终端上可用；assistant 会按入口决定给不给模型看这个 schema
    tools.append(get_camera_tool())
    # 文件读写同样只在带附件的那一轮露出来
    tools += get_file_tools()
    tools += get_capability_tools()
    tools.append(get_thinking_tool())
    return tools