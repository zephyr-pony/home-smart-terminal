"""核心编排层 - CLI 与服务端共用的处理逻辑

从 main.py 抽离：输入文本 → 内置命令 / agent loop（LLM 决策+工具调用）→ 结构化结果。
不碰音频、不直接播报，由调用方（CLI 的 tts.speak / 服务端的 TTS 合成）决定输出方式。
"""
import json
import threading
import time
from datetime import datetime

from config import (
    CONFIRM_WRITE_ACTIONS,
    MAX_SESSIONS,
    SESSION_CONTEXT_MAX_CHARS,
    SESSION_CONTEXT_TURNS,
    SESSION_TTL_SECONDS,
)
from agent.clarify import Clarifier
from agent.confirm import (
    WRITE_ACTIONS as CONFIRMABLE_WRITES,
    ConfirmationManager,
    ConfirmationRequired,
    parse_confirm_actions,
)
from agent.entities import EntityService
from agent.guard import correction_note, guard_false_claim, guard_write_args
from agent.home_watch import build_home_watch
from agent.verify import verify_reply
from agent import wording
from agent.propose import ProposalService
from adapters.home import build_home
from domain.facts import Facts
from domain.tasks import Tasks
from memory import Memory
from items import Items
from llm import LLM
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
- 家庭日常事实/闲聊记录（如"今天买了个新鼠标""冰箱里还有三个鸡蛋"）→ memory_store（summary 用绝对日期）
- 账单/日程/提醒（如"明天交电费""下周三开会""提醒我周五取快递"）→ task_add（type=bill/schedule/custom，due_at 转 YYYY-MM-DD）
- 结构化属性（户号、地址、品牌、型号、偏好）→ fact_store；查询用 fact_search
- 任务查询/完成/修改/延期（"我有哪些提醒""电费交了""取快递改到明天""这个提醒不用了""明天再说"）→ task_list / task_complete / task_update / task_cancel / task_postpone
- 家庭事务查询（"酸奶还有几天过期""冰箱里有什么""我有哪些提醒"）→ family_search 一次查完物品/记忆/待办，不要分别调用 item_search / memory_search / task_list
- 会过期的物品（"酸奶8月5号过期""鸡蛋还剩3个"）→ item_add 记到物品清单（expiry_date 转 YYYY-MM-DD）
- 物品管理（"删掉酸奶""看看有哪些东西"）→ item_delete / item_list
- 记忆管理（"看看记忆""忘掉X"）→ memory_list / memory_delete
- 实体管理（"把爸妈家也叫父母家"）→ entity_add / entity_rename / entity_list
- 时间相关问题（还有几天/什么时候/是否过期）→ 直接用上方「今天日期」计算，**不要**调用 get_current_date
- 查无结果时老实说"我还没有这方面的记录"
- 对话历史里可能已有相关上下文，理解"那个""它""还有呢"这类指代时结合历史
- 回答简洁口语化，像家人聊天，不超过 3 句话
- 只有闲聊（打招呼/无关话题）才直接回答，不需要调用工具"""

# 接了设备网关时才追加，避免模型对着不存在的工具瞎调
HOME_PROMPT_LINE = """
- 智能设备状态（"客厅的灯开着吗""空调设的多少度""净水器滤芯还剩多少"）→ home_query，keyword 用设备名或房间名
- home_query 只能查不能操作：用户要求开关设备时，如实说现在只能查、还不能控制"""

# 白名单非空时才有 home_control
HOME_CONTROL_PROMPT_LINE = """
- 开关设备（"把客厅灯关了""空调调到 26 度""扫地机回去充电"）→ home_control，device 用设备名
- 门锁、燃气、热水器这类不允许语音操作，要如实说去米家 App 处理，不要假装能开
- home_control 的返回里已经有「执行后读到的真实状态」，照它说，别自己编"已关闭\""""


class Assistant:
    """家庭智能终端核心编排。"""

    def __init__(self, memory=None, llm=None, items=None, clarifier=None,
                 tasks=None, facts=None, home=None):
        self.memory = memory or Memory()
        self.llm = llm or LLM()
        self.items = items or Items()
        self.tasks = tasks or Tasks()
        self.facts = facts or Facts()
        # 设备网关（Home Assistant）：没配 HOME_TOKEN 就是 None，工具不注册
        self.home = home if home is not None else build_home()
        # 歧义澄清（实体 + 待澄清动作），写操作前先过一遍
        self.clarifier = clarifier or Clarifier(items=self.items)
        # 写操作统一走「提案 → 提交」，带幂等和审计
        self.proposals = ProposalService(
            self.clarifier.store,
            self.memory,
            self.items,
            tasks=self.tasks,
            facts=self.facts,
        )
        # 删除等高风险动作先确认
        self.confirmations = ConfirmationManager(
            self.clarifier.store, self.memory, self.items
        )
        # 设备主动提醒：滤芯余量、故障码、盯住的事件（去重状态存在 agent.db）
        self.home_watch = build_home_watch(self.home, self.clarifier.store)
        # 弱模型模式：写操作也要先复述参数再执行（小模型会记错日期、凭空建任务）
        self.confirm_write_actions = parse_confirm_actions(CONFIRM_WRITE_ACTIONS)
        # 实体管理：新增/改名/别名/列表
        self.entities = EntityService(self.clarifier.store)
        # 工具注册表（memory + 日期 + items + 实体）
        self.tools = get_all_tools(
            self.memory,
            self.items,
            proposals=self.proposals,
            entities=self.entities,
            tasks=self.tasks,
            facts=self.facts,
            home=self.home,
        )
        self._tools_by_name = {
            t["schema"]["function"]["name"]: t["execute"] for t in self.tools
        }
        # 确认通过后回放原来的工具调用
        self.confirmations.tool_runner = self._run_tool_by_name
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

    def process_text(self, text, client_id=None, on_delta=None):
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
                - "clarify": 需要用户补充信息
                - "confirm": 等待用户确认高风险操作
        """
        text = (text or "").strip()
        if not text:
            return self._result("chat", "我没听清，能再说一遍吗？", "chat", "chat")

        conversation_id = (client_id or "cli").strip()

        # 0) 待确认动作优先：确认/取消/重新提问
        pending_confirm = self.confirmations.get_pending(conversation_id)
        if pending_confirm is not None:
            outcome = self.confirmations.handle(pending_confirm, text)
            kind = outcome.get("kind")
            if kind == "reask":
                return self._result(
                    "confirm", outcome.get("reply", ""), "confirm", "ask",
                    pending_action={
                        "id": pending_confirm["id"],
                        "missing_slot": "confirm",
                        "action_type": pending_confirm.get("action_type"),
                        "question": pending_confirm.get("question", ""),
                    },
                )
            if kind == "cancelled":
                reply = outcome.get("reply", "")
                self._record_turn(client_id, text, reply)
                return self._result("chat", reply, "confirm", "cancel")
            if kind == "done":
                reply = outcome.get("reply", "")
                extra = {}
                if "deleted" in outcome:
                    extra["deleted"] = outcome["deleted"]
                self._record_turn(client_id, text, reply)
                return self._result(
                    outcome.get("type", "chat"),
                    reply,
                    outcome.get("domain", "confirm"),
                    outcome.get("action", "delete"),
                    **extra,
                )

        # 1) 歧义澄清：缺关键槽位时先问一句，不直接写库
        clarification = self.clarifier.check(text, conversation_id)
        if clarification is not None:
            kind = clarification.get("kind")
            if kind == "clarify":
                reply = clarification.get("reply", "")
                self._record_turn(client_id, text, reply)
                return self._result(
                    "clarify", reply, "clarify", "ask",
                    pending_action=clarification.get("pending_action"),
                )
            if kind == "cancelled":
                reply = clarification.get("reply", "")
                self._record_turn(client_id, text, reply)
                return self._result("chat", reply, "clarify", "cancel")
            if kind == "resolved":
                text = clarification.get("text", text)

        # 2) 内置命令预检（确定性、零延迟，不进 agent）
        cmd_result = self._handle_command(text, conversation_id)
        if cmd_result is not None:
            self._record_turn(client_id, text, cmd_result.get("reply", ""))
            return cmd_result

        # 3) agent loop：LLM 自主决策调工具 → 基于结果回答（带该客户端的最近对话上下文）
        try:
            rtype, answer, extras = self._agent_loop(text, client_id, on_delta)
        except ConfirmationRequired as exc:
            self._record_turn(client_id, text, exc.reply)
            return self._result(
                "confirm", exc.reply, "confirm", "ask",
                pending_action=exc.pending_action,
            )
        # 小模型常见的「假确认」：明明没调写工具，却回「已经记好了」
        if self.confirm_write_actions:
            answer, replaced = guard_false_claim(
                text, extras.get("used_tools") or [], answer
            )
            if replaced:
                rtype = "chat"
                extras.pop("summary", None)
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

    def _agent_loop(self, user_text, client_id=None, on_delta=None):
        """手写 agent loop：LLM 决策 → 执行工具 → 结果回传 → 再决策，最多 N 轮。

        client_id 用于取该客户端的最近对话，作为多轮上下文注入 messages。

        Returns:
            (type, answer, extras): type 为 record/query/chat，extras 为附加字段（如 memories/summary）
        """
        sys_prompt = AGENT_SYSTEM_PROMPT.replace(
            "{today}", datetime.now().strftime("%Y年%m月%d日")
        )
        if self.home is not None:
            sys_prompt += HOME_PROMPT_LINE
            if getattr(self.home, "control_enabled", False):
                sys_prompt += HOME_CONTROL_PROMPT_LINE
        messages = [{"role": "system", "content": sys_prompt}]
        messages.extend(self._context_messages(client_id))
        messages.append({"role": "user", "content": user_text})
        schemas = [t["schema"] for t in self.tools]
        tools_by_name = self._tools_by_name
        used_tools = []
        used_tool_names = set()
        executed_writes = []
        blocked_reply = None
        summary = None
        search_keywords = None
        search_n = 5

        for _turn in range(self.max_agent_loops):
            try:
                # 已经写过东西时不流式：最终回答要过事实校验，逐字推出去的片段
                # 收不回来，校验失败就只能让用户听到错日期。
                can_stream = on_delta is not None and not executed_writes
                if can_stream and hasattr(self.llm, "chat_with_tools_stream"):
                    data = self.llm.chat_with_tools_stream(
                        messages, schemas, on_delta=on_delta
                    )
                else:
                    data = self.llm.chat_with_tools(messages, schemas)
            except Exception as exc:
                print(f"⚠️ LLM 调用失败：{exc}")
                return "chat", "抱歉，我现在连不上大脑，请稍后再试。", {
                    "used_tools": used_tools,
                    "domain": "chat",
                    "action": "chat",
                }
            choice = data["choices"][0]
            message = choice["message"]
            finish = choice.get("finish_reason")

            if finish == "tool_calls" and message.get("tool_calls"):
                messages.append(message)  # assistant 的 tool_calls 消息
                turn_writes = 0
                turn_other = False
                turn_blocked = False
                turn_notes = []
                for tc in message["tool_calls"]:
                    fn = tc.get("function", {})
                    name = fn.get("name", "")
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    # 写操作先过参数护栏：小模型会凭空补日期、把查询当成写操作
                    args_raw = args
                    args, blocked = guard_write_args(name, args, user_text)
                    if blocked:
                        turn_blocked = True
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id", ""),
                            "content": blocked,
                        })
                        continue
                    if not wording.has_template(name):
                        turn_other = True
                    if name not in used_tool_names:
                        used_tools.append(name)
                        used_tool_names.add(name)
                    if self.confirm_write_actions and name in CONFIRMABLE_WRITES \
                            and name in self.confirm_write_actions:
                        question = self._write_confirmation_question(name, args, user_text)
                        conversation_id = (client_id or "cli").strip()
                        pending = self.confirmations.request(
                            conversation_id, name, args, question, user_text
                        )
                        raise ConfirmationRequired(question, pending)
                    if name in ("memory_delete", "item_delete"):
                        resolved, blocked = self._resolve_delete(name, args)
                        if blocked:
                            # 删除目标不唯一或不存在：这句就是最终回复。
                            # 继续让模型转述，它很可能说成「已经删了」。
                            blocked_reply = blocked
                            break
                        args = resolved
                        question = self._confirmation_question(name, args)
                        conversation_id = (client_id or "cli").strip()
                        pending = self.confirmations.request(
                            conversation_id, name, args, question, user_text
                        )
                        raise ConfirmationRequired(question, pending)
                    if name == "memory_store":
                        summary = (args.get("summary") or "").strip() or summary
                    elif name == "task_add":
                        summary = (args.get("title") or "").strip() or summary
                    elif name == "fact_store":
                        subject = (args.get("subject") or "").strip()
                        predicate = (args.get("predicate") or "").strip()
                        summary = f"{subject} {predicate}".strip() or summary
                    elif name == "memory_search" and args.get("keywords"):
                        search_keywords = args["keywords"]
                        try:
                            search_n = int(args.get("n_results", 5))
                        except (TypeError, ValueError):
                            search_n = 5
                        search_n = max(1, min(search_n, 50))
                    execute = tools_by_name.get(name)
                    if execute is None:
                        result = f"错误：未知工具 {name}"
                    else:
                        try:
                            result = execute(**args)
                        except Exception as e:
                            result = f"工具执行出错: {e}"
                        note = correction_note(name, args_raw, args)
                        if note:
                            result = "%s\n%s" % (result, note)
                        if wording.looks_written(name, result):
                            executed_writes.append((name, dict(args)))
                            turn_writes += 1
                            note = wording.replaced_note(result)
                            if note:
                                turn_notes.append(note)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": str(result),
                    })
                if blocked_reply is not None:
                    answer = blocked_reply
                    break
                # 整轮都是成功的写操作：参数里已经有要说的那句话，不再问模型。
                # 这是语音首句延迟最大的一块（决策之后还要再等一次生成）。
                if (turn_writes and turn_writes == len(message["tool_calls"])
                        and not turn_other and not turn_blocked):
                    answer = wording.written_sentence(executed_writes)
                    if turn_notes:
                        answer += "".join(turn_notes)
                    break
                continue  # 下一轮：带着工具结果再问

            # 普通回答（finish=stop）
            answer = (message.get("content") or "").strip()
            break
        else:
            answer = "抱歉，我绕晕了，能再说一遍吗？"

        # 模型嘴上说的日期可能是旧值（实测 4B 会）。先过一遍事实校验：日期、星期、
        # 名字和工具参数对不上就整句换成参数拼的模板句；对得上时再补一句回显
        # （模型已经说到正确日期就不重复念）。
        answer, replaced = verify_reply(answer, executed_writes, user_text)
        if not replaced:
            prefix = wording.echo_prefix(executed_writes, answer)
            if prefix and answer:
                answer = prefix + answer

        # 类型映射：写入→record，查询→query，否则 chat
        if any(t in used_tools for t in ("memory_store", "task_add", "fact_store")):
            rtype = "record"
        elif any(t in used_tools for t in (
            "task_complete", "task_update", "task_cancel", "task_postpone"
        )):
            rtype = "chat"
        elif any(t in used_tools for t in (
            "memory_search", "memory_list", "task_list", "fact_search", "fact_list"
        )):
            rtype = "query"
        else:
            rtype = "chat"
        domain, action = self._tool_metadata(used_tools)
        extras = {
            "used_tools": used_tools,
            "domain": domain,
            "action": action,
        }
        if rtype == "record" and summary:
            extras["summary"] = summary
        elif rtype == "query" and search_keywords:
            extras["memories"] = self.memory.search(search_keywords, search_n)
        return rtype, answer, extras

    def _resolve_delete(self, tool_name, args):
        """删除前先把目标坐实。

        删除不可逆，宁可不做也不能删错：
        - 精确同名 → 直接用
        - 只命中一条相似项 → 用它的全名（用户说「牛奶」，清单里是「蒙牛牛奶」）
        - 多条或没有 → 不删，把情况交给模型去反问

        Returns:
            (resolved_args, blocked)：blocked 非空表示这次不删。
        """
        args = dict(args or {})
        if tool_name == "item_delete":
            name = str(args.get("name") or "").strip()
            if not name:
                return args, "要删哪个物品？名字不能为空。"
            exact = self.items.find_exact(name)
            if exact:
                return args, None
            hits = self.items.search(name)
            if len(hits) == 1:
                args["name"] = hits[0]["name"]
                return args, None
            if len(hits) > 1:
                names = "、".join(row["name"] for row in hits[:5])
                return args, f"清单里有好几个相近的：{names}。要删哪一个？请说完整名称。"
            return args, f"没有找到「{name}」这个物品，什么都没删。"

        keywords = str(args.get("keywords") or "").strip()
        if not keywords:
            return args, "要删哪条记忆？关键词不能为空。"
        hits = self.memory.find_matching(keywords)
        if not hits:
            return args, f"没有找到关于「{keywords}」的记忆，什么都没删。"
        args["count"] = len(hits)
        return args, None

    @staticmethod
    def _confirmation_question(tool_name, args):
        return wording.delete_question(tool_name, args)
    @staticmethod
    def _write_confirmation_question(tool_name, args, user_text=None):
        """复述一遍将要写进去的内容，让用户当场发现参数错了。

        句子由 `agent/wording.py` 拼，不让模型措辞：确认环节本身失真，确认就白做了。
        """
        return wording.confirmation_question(tool_name, args, user_text)

    def _run_tool_by_name(self, name, args):
        execute = self._tools_by_name.get(name)
        if execute is None:
            return f"错误：未知工具 {name}"
        return execute(**(args or {}))

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
            "task_add": ("tasks", "add"),
            "task_list": ("tasks", "list"),
            "task_complete": ("tasks", "complete"),
            "task_update": ("tasks", "update"),
            "task_cancel": ("tasks", "cancel"),
            "task_postpone": ("tasks", "postpone"),
            "fact_store": ("facts", "store"),
            "fact_search": ("facts", "search"),
            "fact_list": ("facts", "list"),
            "get_current_date": ("date", "query"),
        }
        business = [mappings[name] for name in used_tools if name in mappings and name != "get_current_date"]
        if not business:
            return ("date", "query") if "get_current_date" in used_tools else ("chat", "chat")
        domains = {domain for domain, _action in business}
        domain = next(iter(domains)) if len(domains) == 1 else "mixed"
        if "task_complete" in used_tools:
            return domain, "complete"
        if "task_update" in used_tools:
            return domain, "update"
        if "task_cancel" in used_tools:
            return domain, "cancel"
        if "task_postpone" in used_tools:
            return domain, "postpone"
        return domain, business[0][1]

    def close(self):
        self.memory.close()
        self.items.close()
        self.tasks.close()
        self.facts.close()
        self.clarifier.close()

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

    def _handle_command(self, text, conversation_id):
        """处理内置命令。不是命令时返回 None。"""
        if _is_list_command(text):
            return self._cmd_list()
        for w in DELETE_WORDS:
            if w in text:
                return self._cmd_delete(text, w, conversation_id)
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

    def _cmd_delete(self, text, word, conversation_id):
        target = text.replace(word, "").strip()
        for junk in DELETE_JUNK:
            target = target.replace(junk, "")
        target = target.strip()

        if not target:
            return self._result("delete_prompt", "想删掉什么记忆呢？比如对我说，忘掉酸奶。", "memory", "delete")

        # 记忆未命中 → 返回 None 降级给 agent loop（用户可能指的是删物品 item_delete）
        if not self.memory.search(target, 1):
            return None

        question = f"确认删除关于「{target}」的记忆吗？回复「确认」或「取消」。"
        pending = self.confirmations.request(
            conversation_id,
            "memory_delete",
            {"keywords": target},
            question,
            text,
        )
        return self._result(
            "confirm", question, "memory", "delete",
            pending_action=pending, target=target,
        )
