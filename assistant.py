"""核心编排层 - CLI 与服务端共用的处理逻辑

从 main.py 抽离：输入文本 → 内置命令 / agent loop（LLM 决策+工具调用）→ 结构化结果。
不碰音频、不直接播报，由调用方（CLI 的 tts.speak / 服务端的 TTS 合成）决定输出方式。
"""
import json
import threading
import time

from config import (
    MAX_SESSIONS,
    SESSION_CONTEXT_MAX_CHARS,
    SESSION_CONTEXT_TURNS,
    SESSION_TTL_SECONDS,
)
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
- 对话历史里可能已有相关上下文，理解"那个""它""还有呢"这类指代时结合历史
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
        self.session_ttl_seconds = SESSION_TTL_SECONDS
        self.max_sessions = MAX_SESSIONS
        # 注入 agent 的历史轮数（0=关闭多轮上下文）与单条截断长度
        self.session_context_turns = SESSION_CONTEXT_TURNS
        self.session_context_max_chars = SESSION_CONTEXT_MAX_CHARS
        self._session_lock = threading.RLock()

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
            return self._result("chat", "我没听清，能再说一遍吗？", "chat", "chat")

        # 1) 内置命令预检（确定性、零延迟，不进 agent）
        cmd_result = self._handle_command(text)
        if cmd_result is not None:
            return cmd_result

        # 2) agent loop：LLM 自主决策调工具 → 基于结果回答（带该客户端的最近对话上下文）
        rtype, answer, extras = self._agent_loop(text, client_id)
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

    def _agent_loop(self, user_text, client_id=None):
        """手写 agent loop：LLM 决策 → 执行工具 → 结果回传 → 再决策，最多 N 轮。

        client_id 用于取该客户端的最近对话，作为多轮上下文注入 messages。

        Returns:
            (type, answer, extras): type 为 record/query/chat，extras 为附加字段（如 memories/summary）
        """
        sys_prompt = AGENT_SYSTEM_PROMPT.replace(
            "{today}", __import__("datetime").datetime.now().strftime("%Y年%m月%d日")
        )
        messages = [{"role": "system", "content": sys_prompt}]
        messages.extend(self._context_messages(client_id))
        messages.append({"role": "user", "content": user_text})
        schemas = [t["schema"] for t in self.tools]
        tools_by_name = {t["schema"]["function"]["name"]: t["execute"] for t in self.tools}
        used_tools = []
        used_tool_names = set()

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
                    if name not in used_tool_names:
                        used_tools.append(name)
                        used_tool_names.add(name)
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
            rtype = "record"
        elif any(t in used_tools for t in ("memory_search", "memory_list")):
            rtype = "query"
        else:
            rtype = "chat"
        domain, action = self._tool_metadata(used_tools)
        return rtype, answer, {
            "used_tools": used_tools,
            "domain": domain,
            "action": action,
        }

    @staticmethod
    def _tool_metadata(used_tools):
        mappings = {
            "memory_store": ("memory", "store"),
            "memory_search": ("memory", "search"),
            "memory_list": ("memory", "list"),
            "memory_delete": ("memory", "delete"),
            "item_add": ("items", "add"),
            "item_search": ("items", "search"),
            "item_list": ("items", "list"),
            "item_delete": ("items", "delete"),
            "get_current_date": ("date", "query"),
        }
        business = [mappings[name] for name in used_tools if name in mappings and name != "get_current_date"]
        if not business:
            return ("date", "query") if "get_current_date" in used_tools else ("chat", "chat")
        domains = {domain for domain, _action in business}
        domain = next(iter(domains)) if len(domains) == 1 else "mixed"
        return domain, business[0][1]

    def _result(self, rtype, reply, domain, action, **extra):
        result = {
            "type": rtype,
            "reply": reply,
            "used_tools": [],
            "domain": domain,
            "action": action,
        }
        result.update(extra)
        return result

    # ---------- 会话上下文（多轮对话：注入 agent messages） ----------

    def _context_messages(self, client_id, n=None):
        """取该客户端最近若干轮对话，拼成 role 消息（供 agent 多轮上下文）。

        只回放 user/assistant 纯文本，单条超长截断，避免上下文膨胀。
        client_id 为空或 session_context_turns<=0 时返回空列表（退化为单轮）。
        """
        if not client_id or self.session_context_turns <= 0:
            return []
        n = self.session_context_turns if n is None else n
        messages = []
        for user_text, reply in self.get_history(client_id, n):
            if user_text:
                messages.append({"role": "user", "content": self._clip(user_text)})
            if reply:
                messages.append({"role": "assistant", "content": self._clip(reply)})
        return messages

    def _clip(self, text):
        """截断过长的历史消息。limit<=0 表示不截断。"""
        limit = self.session_context_max_chars
        text = str(text)
        if limit and len(text) > limit:
            return text[:limit] + "…"
        return text

    def _purge_sessions(self, now=None):
        now = now or time.time()
        expired = [
            client_id for client_id, session in self.sessions.items()
            if now - session["last_access"] >= self.session_ttl_seconds
        ]
        for client_id in expired:
            del self.sessions[client_id]

    def get_history(self, client_id, n=5):
        """返回某客户端最近 n 轮对话 [(user, reply), ...]。"""
        client_id = (client_id or "").strip()
        if not client_id or len(client_id) > 128:
            return []
        with self._session_lock:
            now = time.time()
            self._purge_sessions(now)
            session = self.sessions.get(client_id)
            if not session:
                return []
            session["last_access"] = now
            return session["turns"][-n:]

    def _record_turn(self, client_id, user_text, reply):
        client_id = (client_id or "").strip()
        if not client_id or len(client_id) > 128:
            return
        with self._session_lock:
            now = time.time()
            self._purge_sessions(now)
            if client_id not in self.sessions and len(self.sessions) >= self.max_sessions:
                oldest = min(self.sessions, key=lambda key: self.sessions[key]["last_access"])
                del self.sessions[oldest]
            session = self.sessions.setdefault(client_id, {"last_access": now, "turns": []})
            session["last_access"] = now
            session["turns"].append((user_text, reply))
            if len(session["turns"]) > self.max_session_turns:
                del session["turns"][: len(session["turns"]) - self.max_session_turns]

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
            return self._result("list", "记忆库还是空的，跟我说点要记住的事吧。", "memory", "list", memories=[])
        summaries = "，".join(f"{m['summary']}" for m in mems[:5])
        return self._result(
            "list", f"一共有 {len(mems)} 条记忆，最近的是：{summaries}",
            "memory", "list", memories=mems,
        )

    def _cmd_delete(self, text, word):
        target = text.replace(word, "").strip()
        for junk in DELETE_JUNK:
            target = target.replace(junk, "")
        target = target.strip()

        if not target:
            return self._result("delete_prompt", "想删掉什么记忆呢？比如对我说，忘掉酸奶。", "memory", "delete")

        deleted = self.memory.delete_by_keywords(target)
        if deleted:
            return self._result(
                "delete", f"已删除 {len(deleted)} 条关于{target}的记忆。",
                "memory", "delete", deleted=deleted, target=target,
            )
        # 记忆未命中 → 返回 None 降级给 agent loop（用户可能指的是删物品 item_delete）
        return None
