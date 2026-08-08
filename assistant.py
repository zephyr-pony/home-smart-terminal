"""核心编排层 - CLI 与服务端共用的处理逻辑

从 main.py 抽离：输入文本 → 内置命令 / 记忆检索 / LLM 分类 → 结构化结果。
不碰音频、不直接播报，由调用方（CLI 的 tts.speak / 服务端的 TTS 合成）决定输出方式。
"""
from memory import Memory
from llm import LLM


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


class Assistant:
    """家庭智能终端核心编排。"""

    def __init__(self, memory=None, llm=None):
        self.memory = memory or Memory()
        self.llm = llm or LLM()
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
                - "record": 已存记忆
                - "query":  查询回答
                - "chat":   闲聊
        """
        text = (text or "").strip()
        if not text:
            return {"type": "chat", "reply": "我没听清，能再说一遍吗？"}

        # 1) 内置命令（不走 LLM，快速响应）
        cmd_result = self._handle_command(text)
        if cmd_result is not None:
            return cmd_result

        # 2) 先查现有记忆，再让 LLM 带上下文分类
        related = self.memory.search(text)
        result = self.llm.analyze(text, related)
        msg_type = result.get("type", "chat")

        if msg_type == "record":
            summary = result.get("summary", text)
            self.memory.store(text, summary)
            reply = {"type": "record", "reply": "记住了", "summary": summary}
        elif msg_type == "query":
            keywords = result.get("keywords", text)
            memories = self.memory.search(keywords)
            answer = self.llm.answer(text, memories)
            reply = {"type": "query", "reply": answer, "memories": memories}
        else:
            reply = {"type": "chat", "reply": result.get("reply", "好的")}

        self._record_turn(client_id, text, reply["reply"])
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
        return {
            "type": "delete",
            "reply": f"没有找到关于{target}的记忆。",
            "deleted": [],
            "target": target,
        }
