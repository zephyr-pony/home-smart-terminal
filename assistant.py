"""核心编排层 - CLI 与服务端共用的处理逻辑

从 main.py 抽离：输入文本 → 内置命令 / agent loop（LLM 决策+工具调用）→ 结构化结果。
不碰音频、不直接播报，由调用方（CLI 的 tts.speak / 服务端的 TTS 合成）决定输出方式。
"""
import json

from memory import Memory
from items import Items
from llm import LLM, _chat_with_tools
from tools import get_all_tools


# 内置命令识别（宽泛匹配，兼容语音变体）
# 查看记忆：对象词（记忆/记录）+ 查看类动词，或直接点名命令
LIST_OBJECTS = ("记忆", "记录")
LIST_VERBS = ("看看", "看一下", "查看", "查查", "查一下", "看下", "有什么", "有哪些", "列一下", "列表")
LIST_PATTERNS = ("记了什么", "记住什么", "我的记忆", "记忆列表", "记录列表")
# 删除记忆：删除类动词（"忘掉酸奶" / "把酸奶忘掉"）
DELETE_WORDS = ("忘掉", "忘记", "删掉", "删除", "去掉", "清除", "移除", "取消")
# 删除目标提取时清理的助词/人称
DELETE_JUNK = ("请", "帮我", "把", "一下", "给我", "我想", "我要", "着", "的", "啊", "呀", "吧", "记忆", "记录", "那条", "这条")


def _is_list_command(text):
    """查看记忆命令：对象词 + 查看动词，或直接点名命令式短语。"""
    if any(obj in text for obj in LIST_OBJECTS) and any(v in text for v in LIST_VERBS):
        return True
    return any(p in text for p in LIST_PATTERNS)


AGENT_SYSTEM_PROMPT = """你是家庭智能终端助手"小马"，帮家里记事情、查事情、管物品。
今天日期：{today}

工具分工：
- 家庭事实（如"明天交电费""冰箱里还有三个鸡蛋"）→ memory_store 记住（summary 用绝对日期）
- 家庭事务查询（"酸奶还有几天过期""冰箱里有什么"）→ 先 item_search 查物品清单，查不到再用 memory_search 查记忆
- 会过期的物品（"酸奶8月5号过期""鸡蛋还剩3个"）→ item_add 记到物品清单（expiry_date 转 YYYY-MM-DD）
- 物品管理（"删掉酸奶""看看有哪些东西"）→ item_delete / item_list
- 记忆管理（"看看记忆""忘掉X"）→ memory_list / memory_delete
- 时间相关问题（还有几天/什么时候/是否过期）→ 调用 get_current_date 拿今天日期，结合物品/记忆日期计算
- 查无结果时老实说"我还没有这方面的记录"
- 回答简洁口语化，像家人聊天，不超过 3 句话
- 只有闲聊（打招呼/无关话题）才直接回答，不需要调用工具"""


class Assistant:
    """家庭智能终端核心编排。"""

    def __init__(self, memory=None, llm=None, items=None):
        self.memory = memory or Memory()
        self.llm = llm or LLM()
        self.items = items or Items()
        # 工具注册表（memory 4 件套 + 日期 + items 4 件套）
        self.tools = get_all_tools(self.memory, self.items)
        self.max_agent_loops = 5
        # 轻量会话上下文：client_id -> [(user_text, reply), ...]（阶段 3 多轮对话的底子）
        self.sessions = {}
        self.max_session_turns = 20

    # ---------- 对外接口 ----------

    def process_text(self, text, client_id=None):
        """处理用户文本输入，返回结构化结果 dict。

        Returns:
            dict: {"type": ..., "reply": str, ...}
            type 取值:
                - "list":   内置命令·查看记忆
                - "delete": 内置命令·删除记忆
                - "delete_prompt": 删除命令缺目标
                - "record": agent 存了记忆
                - "query":  agent 查了记忆并回答
                - "chat":   闲聊
        """
        text = (text or "").strip()
        if not text:
            return {"type": "chat", "reply": "我没听清，能再说一遍吗？"}

        # 1) 内置命令预检（确定性、零延迟，不进 agent）
        cmd_result = self._handle_command(text)
        if cmd_result is not None:
            return cmd_result

        # 2) agent loop：LLM 自主决策调工具 → 基于结果回答
        rtype, answer, extras = self._agent_loop(text)
        reply = {"type": rtype, "reply": answer}
        reply.update(extras)

        self._record_turn(client_id, text, answer)
        return reply

    def process_voice(self, stt, audio_path, client_id=None):
        """识别音频文件并处理。stt 由调用方传入（模型加载归调用方管）。

        Returns:
            dict: process_text 的结果 + {"text": 识别文本}
            识别为空时返回 {"type": "empty", "reply": "", "text": ""}
        """
        text = stt.transcribe(audio_path).strip()
        if not text:
            return {"type": "empty", "reply": "", "text": ""}
        result = self.process_text(text, client_id)
        result["text"] = text
        return result

    # ---------- agent loop ----------

    def _agent_loop(self, user_text):
        """手写 agent loop：LLM 决策 → 执行工具 → 结果回传 → 再决策，最多 N 轮。

        Returns:
            (type, answer, extras): type 为 record/query/chat，extras 为附加字段（如 memories/summary）
        """
        sys_prompt = AGENT_SYSTEM_PROMPT.replace(
            "{today}", __import__("datetime").datetime.now().strftime("%Y年%m月%d日")
        )
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_text},
        ]
        schemas = [t["schema"] for t in self.tools]
        tools_by_name = {t["schema"]["function"]["name"]: t["execute"] for t in self.tools}
        used_tools = set()

        for _turn in range(self.max_agent_loops):
            data = _chat_with_tools(messages, schemas)
            choice = data["choices"][0]
            message = choice["message"]
            finish = choice.get("finish_reason")

            if finish == "tool_calls" and message.get("tool_calls"):
                messages.append(message)  # assistant 的 tool_calls 消息
                for tc in message["tool_calls"]:
                    fn = tc.get("function", {})
                    name = fn.get("name", "")
                    used_tools.add(name)
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    execute = tools_by_name.get(name)
                    if execute is None:
                        result = f"错误：未知工具 {name}"
                    else:
                        try:
                            result = execute(**args)
                        except Exception as e:
                            result = f"工具执行出错: {e}"
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": str(result),
                    })
                continue  # 下一轮：带着工具结果再问

            # 普通回答（finish=stop）
            answer = (message.get("content") or "").strip()
            break
        else:
            answer = "抱歉，我绕晕了，能再说一遍吗？"

        # 类型映射：存了记忆→record，查了记忆→query，否则 chat
        if "memory_store" in used_tools:
            return "record", answer, {}
        if any(t in used_tools for t in ("memory_search", "memory_list")):
            return "query", answer, {}
        return "chat", answer, {}

    # ---------- 会话上下文（阶段 3 多轮对话的底子，暂不注入 prompt） ----------

    def get_history(self, client_id, n=5):
        """返回某客户端最近 n 轮对话 [(user, reply), ...]。"""
        if not client_id:
            return []
        turns = self.sessions.get(client_id, [])
        return turns[-n:]

    def _record_turn(self, client_id, user_text, reply):
        if not client_id:
            return
        turns = self.sessions.setdefault(client_id, [])
        turns.append((user_text, reply))
        if len(turns) > self.max_session_turns:
            del turns[: len(turns) - self.max_session_turns]

    # ---------- 内置命令 ----------

    def _handle_command(self, text):
        """处理内置命令。不是命令时返回 None。"""
        if _is_list_command(text):
            return self._cmd_list()
        for w in DELETE_WORDS:
            if w in text:
                return self._cmd_delete(text, w)
        return None

    def _cmd_list(self):
        mems = self.memory.list_all(10)
        if not mems:
            return {"type": "list", "reply": "记忆库还是空的，跟我说点要记住的事吧。", "memories": []}
        summaries = "，".join(f"{m['summary']}" for m in mems[:5])
        return {
            "type": "list",
            "reply": f"一共有 {len(mems)} 条记忆，最近的是：{summaries}",
            "memories": mems,
        }

    def _cmd_delete(self, text, word):
        target = text.replace(word, "").strip()
        for junk in DELETE_JUNK:
            target = target.replace(junk, "")
        target = target.strip()

        if not target:
            return {"type": "delete_prompt", "reply": "想删掉什么记忆呢？比如对我说，忘掉酸奶。"}

        deleted = self.memory.delete_by_keywords(target)
        if deleted:
            return {
                "type": "delete",
                "reply": f"已删除 {len(deleted)} 条关于{target}的记忆。",
                "deleted": deleted,
                "target": target,
            }
        # 记忆未命中 → 返回 None 降级给 agent loop（用户可能指的是删物品 item_delete）
        return None
