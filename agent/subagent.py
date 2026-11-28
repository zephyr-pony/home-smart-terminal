"""只读子 agent（D89）：把「要多步查证」的活丢出去，主会话只等结论。

三条边界，都是「没有用户在场」推出来的：

1. **只读**。写操作在小马里全靠「人当场点头」（提案 → 确认 → 提交），
   子任务里没人可问，所以它一个写工具都不给，连打印、控制设备也不给。
2. **不带历史**。子任务只拿到任务描述，拿不到这家人的聊天记录——它可能去读
   公开网页，把家里的话带进那种上下文没有好处。
3. **有上限**。最多 `MAX_STEPS` 轮、`DEADLINE_SECONDS` 秒，超了就把已经查到的
   交回去。语音场景等不起，所以这个工具只给网页和 CLI（见 assistant）。
"""
import json
import time
from datetime import datetime

# 白名单而不是黑名单：以后新增的写工具默认不会漏进子任务（fail-closed）。
READONLY_TOOLS = frozenset({
    # 时间 / 计算 / 农历
    "time_now", "calc", "lunar_calendar",
    # 上网
    "web_search", "web_fetch", "find_tools",
    # 家里的事（全是查询）
    "memory_search", "memory_list",
    "item_search", "item_list",
    "fact_search", "fact_list",
    "task_list", "entity_list",
    "family_search", "home_query", "weather_query",
    # 文件与代码：run_python 走降权通道，碰不到正式库（D86）
    "file_read", "run_python",
})

MAX_STEPS = 8
DEADLINE_SECONDS = 90
MAX_TOKENS = 1024
MAX_ANSWER_CHARS = 1500

SYSTEM_PROMPT = """你是小马的子任务助手。只做一件事：把交给你的问题查清楚，然后把结论交回去。

规则：
- 只用给你的工具。查不到就说查不到，不要用训练时的印象代替工具结果。
- 你不写任何东西：不记手机号、不加任务、不改事实、不打印、不控制设备。
- 结论用两三句话，带上关键数字和出处（哪个网页、哪条记忆、哪次计算）。
- 今天是 {today}。这次的任务类型是 {purpose}。
"""


def _schemas(tools):
    """只挑只读工具的 schema。tools 是 Assistant 的工具表（dict 列表）。"""
    picked = []
    for tool in tools or []:
        schema = tool.get("schema") or {}
        name = (schema.get("function") or {}).get("name")
        if name in READONLY_TOOLS:
            picked.append(tool)
    return picked


def run(task, llm, tools, purpose="research"):
    """跑一个只读子任务，返回一段结论（字符串，直接进主会话上下文）。"""
    task = str(task or "").strip()
    if not task:
        return "错误：子任务要做什么没写清楚。"
    picked = _schemas(tools)
    if not picked:
        return "错误：现在没有可用的只读工具，子任务做不了。"
    by_name = {
        t["schema"]["function"]["name"]: t["execute"] for t in picked
    }
    schemas = [t["schema"] for t in picked]
    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT.format(
                today=datetime.now().strftime("%Y-%m-%d"),
                purpose="查资料" if purpose != "compute" else "跑代码/算东西",
            ),
        },
        {"role": "user", "content": task},
    ]
    deadline = time.monotonic() + DEADLINE_SECONDS
    used = []
    for _step in range(MAX_STEPS):
        if time.monotonic() >= deadline:
            return "子任务超时（%d 秒没查完）。已经用过的工具：%s。" % (
                DEADLINE_SECONDS, "、".join(sorted(set(used))) or "无",
            )
        try:
            data = llm.chat_with_tools(messages, schemas, max_tokens=MAX_TOKENS)
        except Exception as exc:  # 子任务失败不该把主会话打挂
            return "子任务失败（模型调用出错）：%s" % exc
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            return "子任务失败（模型返回格式不对）。"
        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            messages.append(message)
            for call in tool_calls:
                fn = call.get("function") or {}
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                execute = by_name.get(name)
                if execute is None:
                    result = "错误：子任务不能调用 %s，只能用只读工具。" % (name or "这个工具")
                else:
                    try:
                        result = execute(**(args or {}))
                    except Exception as exc:
                        result = "工具执行出错：%s" % exc
                    used.append(name)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": str(result),
                })
            continue
        answer = (message.get("content") or "").strip()
        if "<invoke" in answer or "<tool_call" in answer:
            answer = ""
        if not answer:
            return "子任务没有给出结论。"
        if len(answer) > MAX_ANSWER_CHARS:
            answer = answer[:MAX_ANSWER_CHARS].rstrip() + "…"
        if used:
            answer += "\n（子任务用了：%s）" % "、".join(sorted(set(used)))
        return answer
    return "子任务跑了 %d 步还没结论。把已经查到的先告诉用户，或者换个更具体的问法。" % MAX_STEPS
