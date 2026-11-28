"""核心编排层 - CLI 与服务端共用的处理逻辑

从 main.py 抽离：输入文本 → 内置命令 / agent loop（LLM 决策+工具调用）→ 结构化结果。
不碰音频、不直接播报，由调用方（CLI 的 tts.speak / 服务端的 TTS 合成）决定输出方式。
"""
import json
import os
import shutil
import threading
import time
import traceback
import uuid
from datetime import datetime

from config import (
    CONFIRM_WRITE_ACTIONS,
    HOT_CONTEXT_ENABLED,
    HOT_CONTEXT_FACTS,
    HOT_CONTEXT_MAX_CHARS,
    HOT_CONTEXT_MEMORIES,
    LLM_REASONING_EFFORT,
    LLM_THINKING_EFFORT,
    MAX_SESSIONS,
    PRINTER_ALLOWED,
    PRINTER_ENABLED,
    PRINT_REUSE_HOURS,
    PRINTER_MAX_COPIES,
    PRINTER_MAX_PAGES,
    PRINTER_NAME,
    PRINTER_SERVER,
    SESSION_CONTEXT_MAX_CHARS,
    SESSION_CONTEXT_TURNS,
    SESSION_TTL_SECONDS,
    VISION_CONTEXT_SECONDS,
    WEATHER_API_HOST,
    WEATHER_API_KEY,
    WEATHER_CITY,
    WEATHER_LAT,
    WEATHER_LON,
)
from agent.clarify import Clarifier
from agent.confirm import (
    WRITE_ACTIONS as CONFIRMABLE_WRITES,
    ConfirmationManager,
    ConfirmationRequired,
    parse_confirm_actions,
)
from agent.entities import EntityService
from agent.guard import (
    correction_note, guard_false_claim, guard_print_args, guard_write_args,
)
from agent.home_watch import build_home_watch
from agent.verify import verify_reply
from agent import wording
from agent.propose import ProposalService
from agent import thinking
from adapters.home import build_home
from adapters.printer import build_printer, kind_of, pdf_page_count
from adapters import documents
from adapters.weather import Weather
from domain.dialogue import DialogueStore
from domain.facts import Facts, PROFILE_PREFIX, profile_subject
from domain.space import normalize_space_id, space_paths
from domain.store import AgentStore
from domain.tasks import Tasks
from memory import Memory
from items import Items
from llm import LLM
from tools import get_all_tools, get_printer_tool


def _float_or_none(value):
    """配置里的经纬度是字符串；空或非法返回 None，交给 Open-Meteo 走城市名。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# 打印机状态的缓存时长：打印机是另一台机器，断电时 lpstat 要等到超时才返回，
# 概览页轮询不能每次都去碰它（D61 补充）。
PRINTER_STATUS_TTL_SECONDS = 30

# 查看/删除记忆不再走关键词预检（2026-11-27）：以前按「记忆+查看动词」「忘掉/删掉/取消」
# 直接分流，绕过了模型，还会误伤——「取消取快递」先被当成删记忆（靠没命中才退回去）。
# 现在一律进 agent loop：模型调 memory_search / memory_list / memory_delete，
# 删除仍走 ConfirmationManager 的确认。代价是这两类各多一轮 LLM。


AGENT_SYSTEM_PROMPT = """你是家庭智能终端助手"小马"，帮家里记事情、查事情、管物品。
今天日期：{today}

工具描述里写了各自能做什么、什么时候用，按用户的意思自己选。
回答简洁口语化，像家人聊天，别写成公文。
查不到结果就如实说没有记录，不要编；如果用户的话可能被听错（同音字），
再问一句"你要查的是哪件事"，不要顺着可能错的词下结论。
对话历史里可能有相关上下文，理解"那个""它""还有呢"这类指代时结合历史。

打印表格之前先估算版式：按各列宽度加起来，和 A4 可打印宽度比一下（纵向约 17cm、
左右边距各约 2cm 就按 17cm 算；列宽 1 个单位≈0.2cm，中文按 2 个单位估）。
超宽时先找出最宽的那几列（长文本、备注列最常见）收窄；收窄后还超，就把工作表
改成横向（landscape，可打印宽度约 25cm），必要时再按页宽缩放。目的是让整张表
在一页宽里装下，不要默认打成两三页。"""

# 接了设备网关时才追加，避免模型对着不存在的工具瞎调
HOME_PROMPT_LINE = """
- 智能设备状态（"客厅的灯开着吗""空调设的多少度""净水器滤芯还剩多少"）→ home_query，keyword 用设备名或房间名"""

# 只读网关才说这句。能控制时还说「只能查」，模型就会告诉用户「我还不能控制」。
HOME_READONLY_PROMPT_LINE = """
- home_query 只能查不能操作：用户要求开关设备时，如实说现在只能查、还不能控制"""

# 语音终端结束会话：只有调用方把 handle_exit_intent 交给这一轮时才说
EXIT_TOOL_PROMPT_LINE = """
- 这轮接在语音终端上，用户要结束对话（"我睡了""去忙了""先这样""不用了"这类意思）时，
  调用 handle_exit_intent 收尾，say_goodbye 给一句简短告别；只在文字里说再见不会结束会话。
  用户还在问事情、或只是随口提到「再见」时不要调用"""

# 白名单非空时才有 home_control
HOME_CONTROL_PROMPT_LINE = """
- 开关设备（"把客厅灯关了""空调调到 26 度""扫地机回去充电"）→ home_control，device 用设备名
- 门锁、燃气、热水器这类不允许语音操作，要如实说去米家 App 处理，不要假装能开
- home_control 的返回里已经有「执行后读到的真实状态」，照它说，别自己编"已关闭\""""

# 带摄像头的终端才有 camera_look；拍照的边界（明确要看才拍、照片外不猜）写在
# 工具描述里，不在这里重复——描述就是模型看到的那份说明（D75）。

# 看图这一类请求没有工具，只有一张照片
VISION_SYSTEM_PROMPT = """你在看客厅终端刚拍的一张照片，要直接用口语回答家里人的问题。
今天日期：{today}

要求：
- 只回答问的那件事，一到两句话，不要客套，也不要描述拍照过程
- 看不清、拿不准就说「没看清」，不要猜
- 不判断画面里是谁，也不做身份推断
"""


class Assistant:
    """家庭智能终端核心编排。"""

    def __init__(self, memory=None, llm=None, items=None, clarifier=None,
                 tasks=None, facts=None, home=None, decision_llm=None,
                 printer=None, print_store=None, space_id="default",
                 dialogue=None):
        self.space_id = normalize_space_id(space_id)
        self.space = space_paths(self.space_id)
        self.memory = memory or Memory(self.space["memory"])
        self.llm = llm or LLM()
        # 打印记录（D65）：重启后还能接上「刚才的文件」、能查历史
        self.print_store = print_store
        # 每个请求一个线程，思考开关的临时状态放线程本地，避免多客户端串台
        self._turn = threading.local()
        # 最近处理过的文件（D61 补充 / D64）：手机上传一次以后，「再打一份」
        # 「改成横向」这类话说的时候没有新附件，靠它把文件接上，TTL 内有效。
        # 当前单家庭部署**各端共用一份**；上多用户时再按 space_id / 人分开。
        self._recent_file = None
        # 第一轮（决策/调工具）可以换更快的模型；没配置就和回答轮同一个
        self.decision_llm = decision_llm
        self.items = items or Items(self.space["items"])
        self.tasks = tasks or Tasks(self.space["tasks"])
        self.facts = facts or Facts(self.space["facts"])
        # 对话记录（D84）：只留最近几天，给跨天上下文和夜间画像提炼当原料。
        # 关掉时 store 里 conn 为 None，add 直接返回 False。
        self.dialogue = dialogue if dialogue is not None else DialogueStore(
            self.space["dialogue"]
        )
        # 设备网关（Home Assistant）：没配 HOME_TOKEN 就是 None，工具不注册
        self.home = home if home is not None else build_home()
        # 歧义澄清（实体 + 待澄清动作），写操作前先过一遍
        self.clarifier = clarifier or Clarifier(
            store=AgentStore(self.space["agent"]), items=self.items
        )
        # 打印文件目录跟着空间走（D68）；没传 print_store 的测试/CLI 落临时目录
        self.print_files_dir = self.space["print_files"]
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
        # 事实挂实体（D66）：存的「饮水机」和「净水器」是同一个东西；
        # 领域层不认识 agent，解析函数从这里注入。
        self.facts.set_entity_lookup(self.clarifier.store.find_any_entity)
        # 记忆也一样（D83）：问「饮水机滤芯」要能捞到写着「净水器」的那条
        self.memory.set_entity_lookup(self.clarifier.store.find_any_entity)
        # 天气：默认 Open-Meteo（零 key），配了和风 key 就用和风。
        # 家里城市优先取 facts 的「家 / 所在城市」（D55），用户说一次就到处共用；
        # WEATHER_CITY 只作兜底，保留给没在对话里设过城市的部署。
        self.weather = Weather(
            WEATHER_API_HOST, WEATHER_API_KEY, WEATHER_CITY,
            _float_or_none(WEATHER_LAT), _float_or_none(WEATHER_LON),
            city_lookup=self._home_city,
        )
        # 打印（D61）：Linux CUPS 队列；没装 lp/lpstat 时 available=False，
        # 工具不注册，上传入口也会如实说不可用。
        self.printer = printer if printer is not None else build_printer(
            name=PRINTER_NAME,
            allowed=[item for item in str(PRINTER_ALLOWED or "").split(",") if item.strip()],
            max_pages=PRINTER_MAX_PAGES,
            max_copies=PRINTER_MAX_COPIES,
            server=PRINTER_SERVER,
            enabled=PRINTER_ENABLED,
        )
        self.printer_ready = self.printer.available()
        self._printer_checked_at = time.time()
        # 概览页要看的完整状态（可不可用 / 队列 / 积压 / 原因）；30 秒缓存
        self._printer_detail = None
        self._printer_detail_at = 0.0
        self._printer_lock = threading.Lock()
        self._printer_checking = False
        self._printer_tool = get_printer_tool()
        # 工具注册表（memory + 日期 + items + 实体）
        self.tools = get_all_tools(
            self.memory,
            self.items,
            proposals=self.proposals,
            entities=self.entities,
            tasks=self.tasks,
            facts=self.facts,
            home=self.home,
            weather=self.weather,
        )
        if self.printer_ready:
            self.tools.append(self._printer_tool)
        self._tools_by_name = {
            t["schema"]["function"]["name"]: t["execute"] for t in self.tools
        }
        # 确认通过后回放原来的工具调用
        self.confirmations.tool_runner = self._run_tool_by_name
        # 打印确认后由这里真正出纸（适配器走 CUPS / 远程 CUPS）
        self.confirmations.print_runner = self._print_file
        self.max_agent_loops = 5
        # 轻量会话上下文：client_id -> [(user_text, reply), ...]（阶段 3 多轮对话的底子）
        self.sessions = {}
        self.max_session_turns = 20
        self.session_ttl_seconds = SESSION_TTL_SECONDS
        self.max_sessions = MAX_SESSIONS
        # 注入 agent 的历史轮数（0=关闭多轮上下文）与单条截断长度
        self.session_context_turns = SESSION_CONTEXT_TURNS
        self.session_context_max_chars = SESSION_CONTEXT_MAX_CHARS
        # 热点上下文冻结快照（D58）：client_id -> {"text", "at"}，过期即重建
        self.hot_contexts = {}
        self.hot_context_enabled = HOT_CONTEXT_ENABLED
        self.hot_context_max_chars = HOT_CONTEXT_MAX_CHARS
        self.hot_context_facts = HOT_CONTEXT_FACTS
        self.hot_context_memories = HOT_CONTEXT_MEMORIES
        self._session_lock = threading.RLock()
        # 最近一次「看到」的画面描述：client_id -> {"text", "at"}。
        # 只在内存里、有 TTL，不进记忆库——拍照是看一眼，不是一条事实。
        self.vision_context = {}
        self.vision_context_seconds = VISION_CONTEXT_SECONDS

    # ---------- 对外接口 ----------

    def process_text(self, text, client_id=None, on_delta=None, device_camera=False,
                     task_id=None, attachment=None, extra_tools=None):
        """处理用户文本，并记一条「这轮有没有开思考」（D63）。

        记录只是一次本地追加，不联网、不调模型；写失败也不影响回答。
        extra_tools 是调用方（`/v1`）转来的外部工具 schema，模型一调用就原样交回
        调用方执行——小智服务端自己的插件（如 `handle_exit_intent`）走这条。
        """
        started = time.time()
        try:
            result = self._process_text_inner(
                text, client_id, on_delta=on_delta, device_camera=device_camera,
                task_id=task_id, attachment=attachment, extra_tools=extra_tools,
            )
        finally:
            state = getattr(self._turn, "state", None)
            self._turn.state = None
        if state is not None:
            thinking.record_turn(
                text, client_id, state.get("thinking"), state.get("why"),
                state.get("tools"), time.time() - started, result,
            )
        self._record_dialogue(text, client_id, result)
        return result

    def _record_dialogue(self, text, client_id, result):
        """记下这一轮的原话和回复（D84）。写失败不影响回答。

        只在编排层记一次：网页、CLI、`/v1`、MCP 最后都走 process_text，
        各入口各写一遍必然漏掉某个（小智那条链路最容易漏）。
        """
        store = getattr(self, "dialogue", None)
        if store is None or not getattr(store, "enabled", False):
            return
        text = (text or "").strip()
        if not text:
            return
        try:
            store.add(client_id or "cli", "user", text)
            reply = (result or {}).get("reply") or ""
            if reply:
                store.add(client_id or "cli", "assistant", reply)
        except Exception as exc:  # 记录是旁路，不能拖垮回答
            print("⚠️ 对话记录写入失败：%s" % exc)

    def _process_text_inner(self, text, client_id=None, on_delta=None, device_camera=False,
                            task_id=None, attachment=None, extra_tools=None):
        """处理用户文本输入，返回结构化结果 dict。

        Args:
            device_camera: 这个入口背后的终端带摄像头（只有 /v1 的语音终端会传 True）。
                带的时候才把 camera_look 给模型看，避免网页/CLI 上模型说「我看到了」。
            attachment: 这一轮网页上传的文件（打印用），只在内存里；不回写会话历史。
            task_id: 用户点开的到期任务（通知/任务卡带过来的）。只作为这一轮的工具
                上下文，让「完成了 / 延期」指向那一条；不落库、不进历史。

        Returns:
            dict: {"type": ..., "reply": str, ...}
            type 取值:
                - "delete": 删除确认后真的删了（带 `deleted`）
                - "record": agent 存了记忆
                - "query":  agent 查了记忆并回答
                - "chat":   闲聊
                - "clarify": 需要用户补充信息
                - "confirm": 等待用户确认高风险操作
                - "device_call": 需要设备端执行（现在只有拍照），device_call 里带工具名和参数
        """
        text = (text or "").strip()
        if not text:
            return self._result("chat", "我没听清，能再说一遍吗？", "chat", "chat")

        conversation_id = (client_id or "cli").strip()
        # 打印机是另一台机器：启动时没开、后来开了也要能打，别要求重启小马
        self.refresh_printer()
        # 3.5) 没带新附件时，接上最近处理过的文件：「刚才的文件再打一份」
        # 在网页上不会重新上传，不兜住就只能让用户重传一遍。
        attachment = self._reuse_recent_file(attachment)

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
                payload = pending_confirm.get("payload") or {}
                file_id = payload.get("file_id") if isinstance(payload, dict) else ""
                if pending_confirm.get("action_type") == "print_file" and file_id \
                        and self.print_store is not None:
                    self.print_store.mark(str(file_id), "cancelled")
                self._record_turn(client_id, text, reply)
                return self._result("chat", reply, "confirm", "cancel")
            if kind == "done":
                reply = outcome.get("reply", "")
                # 确认之后落库的同样是写操作（画像、删除、弱模型模式下的写工具）：
                # 不在这里作废快照，用户确认完再问，模型还会念旧值
                self._invalidate_hot_contexts()
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

        # 2) agent loop：LLM 自主决策调工具 → 基于结果回答（带该客户端的最近对话上下文）
        try:
            rtype, answer, extras = self._agent_loop(
                text, client_id, on_delta, device_camera=device_camera,
                task_id=task_id, attachment=attachment, extra_tools=extra_tools,
            )
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

        # 交给设备拍照的那一轮没有回话，记进上下文只会让下一句被「看东西」带偏
        if rtype != "device_call":
            self._record_turn(client_id, text, answer)
        return reply

    def process_voice(self, stt, audio_path, client_id=None, task_id=None):
        """识别音频文件并处理。stt 由调用方传入（模型加载归调用方管）。

        Returns:
            dict: process_text 的结果 + {"text": 识别文本}
            识别为空时返回 {"type": "empty", "reply": "", "text": ""}
        """
        text = stt.transcribe(audio_path).strip()
        if not text:
            return {"type": "empty", "reply": "", "text": ""}
        result = self.process_text(text, client_id, task_id=task_id)
        result["text"] = text
        return result

    def describe_image(self, question, images, client_id="xiaozhi"):
        """看一张照片并直接回答（语音终端拍完就念这句）。

        照片只在内存里过一遍：不落盘、不记日志、不进对话历史。视觉结果也不写入
        记忆——「冰箱里有牛奶」三天后不一定还成立，写进 FTS 只会让后来的查询
        拿到过期的画面。但描述会在会话里留一小会儿（`VISION_CONTEXT_SECONDS`），
        让「那是什么牌子」这种追问能接上，不用再拍一张。
        """
        question = (question or "").strip() or "看看画面里有什么"
        parts = [{"type": "text", "text": question}]
        for url in (images or [])[:2]:  # 最多两张，防止有人一次塞几十张
            if url:
                parts.append({"type": "image_url", "image_url": {"url": url}})
        messages = [
            {
                "role": "system",
                "content": VISION_SYSTEM_PROMPT.replace(
                    "{today}", datetime.now().strftime("%Y年%m月%d日")
                ),
            },
            {"role": "user", "content": parts},
        ]
        try:
            data = self.llm.chat(messages, temperature=0.3, max_tokens=220)
            reply = (data["choices"][0]["message"].get("content") or "").strip()
        except Exception as exc:
            print(f"⚠️ 看图失败：{exc}")
            reply = ""
        # 看不清就说看不清：语音里编一句「看到了」比承认失败糟糕得多；
        # 失败也不留「刚才看到」，免得下一句拿空话当依据。
        if reply:
            self._remember_observation(client_id, reply)
            return reply
        return "我这边没看清，你再说一遍？"

    def _remember_observation(self, client_id, text):
        if not text or self.vision_context_seconds <= 0:
            return
        with self._session_lock:
            self.vision_context[(client_id or "cli").strip()] = {
                "text": text.strip(),
                "at": time.time(),
            }

    def _recent_observation(self, client_id):
        """取最近一次画面描述；过期就当没看过（重启就没了，不进库）。"""
        key = (client_id or "cli").strip()
        with self._session_lock:
            item = self.vision_context.get(key)
            if not item:
                return None
            if time.time() - item["at"] > self.vision_context_seconds:
                self.vision_context.pop(key, None)
                return None
            return item["text"]

    # ---------- 提醒措辞 ----------

    REMINDER_PROMPT = (
        "你在给家里人发一条到点提醒。只根据给出的事实说一句话，口语、简短（不超过 30 字），"
        "像家里人顺口提醒一句；可以带一句「要不要现在办 / 晚点再说」。"
        "**原样说出事实里的那个名字**（标题或物品名），不要改写它；"
        "不要编造事实里没有的日期、数字和后果，不要加称呼和客套。"
    )
    # 软提醒的晚间兜底（D81）：白天没机会说，这会儿补一句，别说得像准点播报
    SOFT_REMINDER_PROMPT = (
        "你在给家里人补一句白天没机会说的提醒。只根据给出的事实说一句话，口语、简短"
        "（不超过 30 字），像忽然想起来顺口提一句，不要说得像整点播报。"
        "**原样说出事实里的那个名字**（标题或物品名），不要改写它；"
        "不要编造事实里没有的日期、数字和后果，不要加称呼和客套。"
    )

    def reminder_sentence(self, facts, fallback, must_include=None, soft=False):
        """把提醒事实交给模型说一句人话；失败、超长或没说对名字就退回模板。

        为什么留模板兜底：提醒的第一要求是「送得到、说得对」，措辞是第二位。
        模型超时、断网或者开始瞎编时，宁可念一句生硬的模板，也不能让这条提醒没声音。

        soft=True 是软提醒的晚间兜底：同一件事，语气要像「忽然想起来」，
        不是到点播报（D81）。
        """
        facts = (facts or "").strip()
        if not facts:
            return fallback
        messages = [
            {
                "role": "system",
                "content": self.SOFT_REMINDER_PROMPT if soft else self.REMINDER_PROMPT,
            },
            {"role": "user", "content": facts},
        ]
        try:
            data = self.llm.chat(messages, temperature=0.4, max_tokens=60)
            text = (data["choices"][0]["message"].get("content") or "").strip()
        except Exception as exc:
            print("⚠️ 提醒措辞生成失败：%s" % exc)
            return fallback
        if not text or len(text) > 60:
            return fallback
        # 名字必须原样出现：模型把「交电费」改写成「交费」还能忍，但换成
        # 「交水费」就是另一件事了——这种宁可念模板。
        if must_include and must_include not in text:
            return fallback
        return text

    # ---------- 热点上下文（D58） ----------

    def _hot_context(self, client_id):
        """会话开始时的冻结快照：画像 + 常用事实 + 最近记忆。

        为什么冻结而不是每轮重建：system prompt 一旦每轮变化，前缀缓存全部失效，
        token 成本翻倍；而且同一通对话里记忆本来就不该漂移（Hermes 的冻结快照同理）。
        预算很小（默认 900 字），所以只能装热点，长尾仍然走工具检索。
        """
        if not self.hot_context_enabled:
            return ""
        key = (client_id or "cli").strip()
        now = time.time()
        with self._session_lock:
            item = self.hot_contexts.get(key)
            if item and now - item["at"] <= self.session_ttl_seconds:
                return item["text"]
            self.hot_contexts.pop(key, None)

        text = self._build_hot_context()
        with self._session_lock:
            self.hot_contexts[key] = {"text": text, "at": now}
            if len(self.hot_contexts) > self.max_sessions:
                oldest = min(
                    self.hot_contexts, key=lambda k: self.hot_contexts[k]["at"]
                )
                self.hot_contexts.pop(oldest, None)
        return text

    def _invalidate_hot_contexts(self):
        """写库之后作废冻结的热点快照，下一轮重建。

        快照是为了保住前缀缓存（D58），但它本来要活到 TTL 到期（默认 24 小时）：
        上午把电费户号改了，下午再问，模型仍会从「已知信息」里念出旧号码——旧号码
        就在系统提示里，六位以上数字的出处校验也只认「提示词里有没有」，拦不住。
        写操作之后清一次，代价只是下一轮重建快照（本地查两个库，毫秒级）。

        不分客户端清：单家庭就几个终端，写的是同一套库，别的终端手里也是同一份旧值。
        """
        with self._session_lock:
            self.hot_contexts.clear()

    def invalidate_hot_context(self):
        """外部写库之后调（网页采纳画像、脚本、后台任务），下一轮重建快照。"""
        self._invalidate_hot_contexts()

    def _build_hot_context(self):
        """按 画像 > 常用事实 > 最近记忆 的优先级拼，超出预算就停。"""
        budget = max(0, self.hot_context_max_chars)
        if budget <= 0:
            return ""

        profile_lines = []
        fact_lines = []
        try:
            facts = self.facts.search(n_results=60) if self.facts is not None else []
        except Exception:
            facts = []
        for fact in facts:
            subject = str(fact.get("subject") or "").strip()
            predicate = str(fact.get("predicate") or "").strip()
            value = str(fact.get("value") or "").strip()
            if not subject or not value:
                continue
            line = "- %s %s：%s" % (subject, predicate, value) if predicate else (
                "- %s：%s" % (subject, value)
            )
            if subject == PROFILE_PREFIX or subject.startswith(PROFILE_PREFIX + "/") or "偏好" in predicate:
                profile_lines.append(line)
            elif len(fact_lines) < self.hot_context_facts:
                fact_lines.append(line)

        mem_lines = []
        try:
            memories = (
                self.memory.list_all(self.hot_context_memories)
                if self.memory is not None else []
            )
        except Exception:
            memories = []
        for mem in memories:
            summary = str(mem.get("summary") or mem.get("text") or "").strip()
            if summary:
                mem_lines.append("- [%s] %s" % (mem.get("time", ""), summary))

        blocks = []
        if profile_lines:
            blocks.append(("用户画像", profile_lines))
        if fact_lines:
            blocks.append(("常用事实", fact_lines))
        if mem_lines:
            blocks.append(("最近记忆", mem_lines))
        if not blocks:
            return ""

        text = "【已知信息】"
        for title, lines in blocks:
            chunk = "\n%s：\n%s" % (title, "\n".join(lines))
            if len(text) + len(chunk) <= budget:
                text += chunk
                continue
            # 整块放不下：先加标题，再按行塞，塞不下就停（后面的块优先级更低）
            header = "\n%s：" % title
            if len(text) + len(header) <= budget:
                text += header
            for line in lines:
                if len(text) + len(line) + 2 > budget:
                    break
                text += "\n" + line
            break
        return text if len(text) > len("【已知信息】") else ""

    def _home_city(self):
        """家里在哪个城市：facts 的「家 / 所在城市」优先（D55）。

        用户说一次「我家在杭州」就该让天气、看板共用，而不是再去改 .env。
        只认主体是「家」这类、属性是城市/位置的当前有效事实，避免把
        「爸妈家」或别的地址当成自己家。
        """
        try:
            rows = self.facts.search(query="城市", n_results=20)
        except Exception:
            return ""
        for row in rows:
            subject = str(row.get("subject") or "").strip()
            predicate = str(row.get("predicate") or "").strip()
            value = str(row.get("value") or "").strip()
            if not value:
                continue
            if subject in ("家", "我家", "我的家", "家里", "自家", "自己家", "家庭") and \
                    predicate in ("所在城市", "所在的城市", "城市", "城市名"):
                return value
        return ""

    def _task_context_note(self, task_id):
        """把「点开的那条提醒」说成一条 system 提示；没有有效 id 就返回空串。

        为什么不让模型自己 task_list 找：语音链路每次多一个工具调用就多跑一轮
        LLM（约 2 秒），而点通知时目标本来就确定。这里只提供上下文，不替模型
        执行——写操作仍走工具、护栏和确认。
        """
        if not task_id:
            return ""
        try:
            tid = int(task_id)
        except (TypeError, ValueError):
            return ""
        if tid <= 0:
            return ""
        try:
            task = self.tasks.get(tid)
        except Exception:
            task = None
        if not task:
            # 旧通知点进来时任务可能已经被处理/删除：如实说，不要改别的任务
            return (
                "用户是从一条提醒点进来的，但任务 #%d 已经不存在了。"
                "如果他说完成、延期或取消「这条」，先如实告诉他这条提醒已经不在，"
                "不要改别的任务。" % tid
            )

        title = task.get("title") or "这条提醒"
        due = task.get("due_at") or ""
        status = task.get("status") or ""
        lines = [
            "用户是从提醒点进来的，当前任务：#%d「%s」%s，状态 %s。" % (
                tid, title, ("，到期 " + due) if due else "", status or "未知",
            ),
            "他说「完成了」「延期」「不用提醒了」时默认指这条；调用 task_complete / "
            "task_update / task_cancel / task_postpone 直接用 task_id=%d，"
            "不要先 task_list，也不要挑别的任务。" % tid,
        ]
        if status == "done":
            lines.append("这条已经完成了：他说完成时如实说明，不要当成新任务。")
        elif status == "cancelled":
            lines.append("这条已经取消了：他说完成或延期时如实说明，不要恢复它。")
        return "\n".join(lines)

    # ---------- 软提醒注入（D81） ----------

    # 每轮最多塞几条：多了就变成汇报清单，聊天里没人这么说话
    SOFT_NOTE_LIMIT = 4
    # 物品过期超过这些天就不再主动提：留下来的旧账不该每轮都在耳边念
    SOFT_NOTE_ITEM_OVERDUE_DAYS = 7

    @staticmethod
    def _soft_when(due):
        """(日期说法, 有没有具体时刻)。

        数据库里的日期型 due_at 也带 T00:00:00（`Tasks.add` 统一存 datetime），
        所以「零点」只能当成没有时刻——不然会说成「昨天凌晨0点」。
        """
        text = str(due or "").strip()
        if not text:
            return "", False
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return text, False
        if moment.hour or moment.minute:
            return wording.speak_datetime(text), True
        return "%d月%d日（%s）" % (
            moment.date().month, moment.date().day, wording.weekday_cn(moment.date()),
        ), False

    def _soft_line(self, kind, name, due, left, overdue_word):
        """一条软提醒：`任务 #14：交电费，11月26日（周四）到期，逾期 1 天`。"""
        when, has_clock = self._soft_when(due)
        if has_clock:
            return "%s：%s，%s" % (kind, name, when)
        if left == 0:
            return "%s：%s，今天到期" % (kind, name)
        return "%s：%s，%s到期，%s %d 天" % (kind, name, when, overdue_word, -left)

    def _soft_reminder_note(self):
        """今天挂着的软提醒，作为这一轮的一条 system 提示（D81）。

        为什么塞上下文而不是加工具：语音链路每多一轮 LLM 就多约 2 秒，而这类事
        本来就不需要「查」——模型这一轮看不看得到，才决定它顺不顺口提一句。
        只给事实和编号，怎么说不写死：聊到相关话题时提，没聊到就不提；白天没
        机会说还有晚间兜底（SOFT_REMINDER_LAST_CALL），不会静默消失。
        """
        entries = []
        get_soft = getattr(self.tasks, "get_soft_due", None)
        if callable(get_soft):
            try:
                tasks = get_soft(limit=self.SOFT_NOTE_LIMIT)
            except Exception as exc:
                print("⚠️ 软提醒任务读取失败：%s" % exc)
                tasks = []
            for task in tasks:
                left = task.get("days_left")
                left = 0 if left is None else int(left)
                entries.append({
                    "sort": (left, 0),
                    "line": self._soft_line(
                        "任务 #%s" % task.get("id"),
                        task.get("title") or "提醒",
                        task.get("due_at") or "",
                        left,
                        "逾期",
                    ),
                })

        get_items = getattr(self.items, "get_due", None)
        if callable(get_items):
            try:
                items = get_items(days=0)
            except Exception as exc:
                print("⚠️ 软提醒物品读取失败：%s" % exc)
                items = []
            for item in items:
                left = item.get("days_left")
                left = 0 if left is None else int(left)
                if left < -self.SOFT_NOTE_ITEM_OVERDUE_DAYS:
                    continue
                entries.append({
                    "sort": (left, 1),
                    "line": self._soft_line(
                        "物品",
                        item.get("name") or "东西",
                        item.get("expiry_date") or "",
                        left,
                        "已经过期",
                    ),
                })

        if not entries:
            return ""
        entries.sort(key=lambda entry: entry["sort"])
        lines = [
            "【今天挂着的软提醒】下面是今天到期或已经过期的几件事。"
            "聊到相关话题时顺口提一句，问一句要不要办掉或者往后放；"
            "没聊到就别硬提，也不要一次全报一遍。",
            "用户对这些事说办了 / 明天再说 / 不用了，就用上面的编号直接调 "
            "task_complete / task_postpone / task_cancel（物品用 item_delete 删掉）。",
        ]
        lines.extend(entry["line"] for entry in entries[:self.SOFT_NOTE_LIMIT])
        return "\n".join(lines)

    @staticmethod
    def _attachment_note(attachment):
        """这一轮待打印文件的说明：只给元数据，不把文件内容塞进上下文。"""
        if not attachment:
            return ""
        name = str(attachment.get("name") or "文件")
        kind = {
            "pdf": "PDF", "image": "图片", "text": "文本", "office": "Office 文档",
        }.get(str(attachment.get("kind") or ""), "文件")
        parts = [kind]
        pages = attachment.get("pages")
        if pages:
            parts.append("%d 页" % int(pages))
        size = attachment.get("size")
        if size:
            parts.append("%.1f MB" % (float(size) / 1024 / 1024))
        if attachment.get("recent"):
            # 沿用上一轮的文件：这句话可能跟文件无关，别硬把文件拉进来
            head = (
                "用户刚才上传/处理过文件：《%s》（%s），这一轮没有重新上传，"
                "但你要用随时可以（比如「再打一份」「改成横向」）。"
                "如果这句话和文件无关，就当没有这个文件，正常回答。"
                % (name, "，".join(parts))
            )
        else:
            head = "用户这一轮在网页里上传了文件：《%s》（%s）。" % (
                name, "，".join(parts)
            )
        return (
            head
            + "要知道里面写了什么就调 file_read；要改内容/排版/转格式，"
            "先 find_tools 看这台机器有什么，再用 run_python 生成新文件，"
            "然后打印那个新文件。要打印就调用 printer_print，份数和页码按用户说的填。"
            "没读过就不要编造文件里写了什么。"
        )

    def _remember_file(self, attachment):
        """记住最近处理过的文件（含 run_python 产出的那份）。

        只留一份、不按客户端分（D64）：当前是单家庭部署，手机传完想在电脑上
        「再打一份」是正常需求；上多用户/多家庭时再按 `space_id` 分开。
        """
        if not attachment or not attachment.get("path"):
            return
        entry = dict(attachment)
        entry["at"] = time.time()
        self._recent_file = entry

    def _reuse_recent_file(self, attachment):
        """没带新附件时接上最近那个文件；过期或已删就当没有。

        「再打一份」「刚才那个改横向」是常事，网页的 📎 只跟着一条消息走。
        内存里没有（比如刚重启）就查打印记录（D65），`PRINT_REUSE_HOURS` 内都算。
        命中的文件带 `recent` 标记，提示里会说明「上一轮的」，避免回复硬扯到文件。
        """
        if attachment:
            return attachment
        entry = self._recent_file
        if entry and self._fresh_file(entry):
            return self._as_recent(entry)
        self._recent_file = None
        if self.print_store is not None:
            row = self.print_store.latest()
            if row and self._fresh_file(row):
                return self._as_recent(row)
        return None

    @staticmethod
    def _fresh_file(entry):
        path = entry.get("path")
        at = entry.get("at") or entry.get("created_at") or 0
        return bool(
            path and os.path.exists(path)
            and time.time() - float(at) <= max(1, PRINT_REUSE_HOURS) * 3600
        )

    @staticmethod
    def _as_recent(entry):
        reused = dict(entry)
        reused.pop("at", None)
        reused.pop("created_at", None)
        reused["recent"] = True
        return reused

    def refresh_printer(self, force=False):
        """打印机可用性每 30 秒重查一次。

        打印机是另一台机器，开机时间和小马无关：启动时没开、过一会儿开了，
        不该让人重启小马；反过来拔了线也别一直假装能打。
        """
        now = time.time()
        if not force and now - self._printer_checked_at < 30:
            return self.printer_ready
        self._printer_checked_at = now
        ready = bool(self.printer and self.printer.available())
        if ready and self._printer_tool not in self.tools:
            self.tools.append(self._printer_tool)
            self._tools_by_name = {
                t["schema"]["function"]["name"]: t["execute"] for t in self.tools
            }
        self.printer_ready = ready
        return ready

    def printer_status(self, force=False):
        """打印机状态（概览页/接口用）：能不能打、队列名、积压、原因。

        为什么不能每次都同步查：打印机断电时 `lpstat` 会一直等到超时（实测 10 秒），
        概览页每 30 秒问一次，不能把页面卡在那儿。所以：
        - 有缓存就立刻返回（30 秒内算新鲜；过期时顺手起一个后台检查，先给旧值）；
        - 从来没查过（刚启动）才同步查一次，否则第一个请求拿不到任何东西。
        返回里的 `stale=True` 表示「这是旧值，后台正在更新」，网页据此再拉一次。
        """
        now = time.time()
        if force:
            return self._check_printer()
        if (self._printer_detail is not None
                and now - self._printer_detail_at < PRINTER_STATUS_TTL_SECONDS):
            return dict(self._printer_detail)

        if self._printer_detail is None:
            # 启动后第一次：不能同步等 CUPS（断电时要 10 秒），先给「检测中」，
            # 后台查完网页再来取一次
            self._start_printer_check()
            return {
                "enabled": bool(getattr(self.printer, "enabled", False)),
                "configured": bool(
                    getattr(self.printer, "name", "") or
                    getattr(self.printer, "server", "")
                ),
                "available": False,
                "name": getattr(self.printer, "name", "") or "",
                "server": getattr(self.printer, "server", "") or "",
                "queue": "",
                "state": "checking",
                "jobs": None,
                "error": "",
                "stale": True,
            }

        self._start_printer_check()
        detail = dict(self._printer_detail)
        detail["stale"] = True
        return detail

    def _check_printer(self):
        """真正去问 CUPS，结果写回缓存；可能阻塞几秒，别在轮询里直接调。"""
        if self.printer is None:
            detail = {
                "enabled": False, "configured": False, "available": False,
                "name": "", "server": "", "queue": "", "state": "unknown",
                "jobs": None, "error": "没有配置打印机",
            }
        else:
            detail = self.printer.status()
        detail["stale"] = False

        self._printer_detail = detail
        self._printer_detail_at = time.time()
        # 状态和「模型能不能看到 printer_print」用同一个结论，不要各查各的
        self._printer_checked_at = self._printer_detail_at
        self.printer_ready = bool(detail.get("available"))
        if self.printer_ready and self._printer_tool not in self.tools:
            self.tools.append(self._printer_tool)
            self._tools_by_name = {
                t["schema"]["function"]["name"]: t["execute"] for t in self.tools
            }
        return dict(detail)

    def _start_printer_check(self):
        """后台查一次；已经在查就不重复起线程。"""
        with self._printer_lock:
            if self._printer_checking:
                return
            self._printer_checking = True

        def run():
            try:
                self._check_printer()
            except Exception:  # 后台线程不能把异常抛给没人接的地方
                traceback.print_exc()
            finally:
                with self._printer_lock:
                    self._printer_checking = False

        threading.Thread(target=run, name="printer-check", daemon=True).start()

    # ---------- agent loop ----------

    def _system_prompt(self, device_camera=False):
        """身份 + 这一轮的能力提示（设备网关）；热点上下文由调用方另加。

        为什么按 control_enabled 二选一：只读那句「只能查不能操作」以前是无条件
        加的，控制开了以后它就变成假话，模型会回「我还不能控制」。

        device_camera 不再影响提示：拍照的边界写在 camera_look 的描述里，而那个
        schema 只在有摄像头时才给模型（D75）——同一套规则不在两处维护。
        """
        prompt = AGENT_SYSTEM_PROMPT.replace(
            "{today}", datetime.now().strftime("%Y年%m月%d日")
        )
        if self.home is not None:
            prompt += HOME_PROMPT_LINE
            if getattr(self.home, "control_enabled", False):
                prompt += HOME_CONTROL_PROMPT_LINE
            else:
                prompt += HOME_READONLY_PROMPT_LINE
        return prompt

    def _agent_loop(self, user_text, client_id=None, on_delta=None, device_camera=False,
                    task_id=None, attachment=None, extra_tools=None):
        """手写 agent loop：LLM 决策 → 执行工具 → 结果回传 → 再决策，最多 N 轮。

        client_id 用于取该客户端的最近对话，作为多轮上下文注入 messages。
        task_id 是用户点开的到期任务：只在这一轮注入一条 system 提示，
        让「完成了 / 延期」指向那一条，不写进会话历史。
        attachment 是网页上传的待打印文件：只在有文件时把 printer_print 给模型看。
        另外每轮会注入「今天挂着的软提醒」（D81）：到期的几件事，聊到相关话题时
        顺口提一句，晚上还没说由提醒循环兜底。

        Returns:
            (type, answer, extras): type 为 record/query/chat，extras 为附加字段（如 memories/summary）；
            device_camera 时还可能返回 device_call（这一轮交给设备端执行，不落库也不生成回复）。
        """
        sys_prompt = self._system_prompt(device_camera)
        # 调用方转来的外部工具（小智服务端插件）：schema 照发，模型一调用就交回去执行
        extra_tools = [t for t in (extra_tools or []) if isinstance(t, dict)]
        passthrough_names = set()
        for item in extra_tools:
            fn = item.get("function") or {}
            if fn.get("name"):
                passthrough_names.add(fn["name"])
        if "handle_exit_intent" in passthrough_names:
            # 模型不会自己想到「结束会话」是一个要调的工具，点一句；只在调用方给了这个工具时加
            sys_prompt += "\n\n" + EXIT_TOOL_PROMPT_LINE
        # 热点上下文（D58）：画像/常用事实/最近记忆，冻结快照，省掉查记忆那一轮
        hot = self._hot_context(client_id)
        if hot:
            sys_prompt += "\n\n" + hot
        messages = [{"role": "system", "content": sys_prompt}]
        history = self._context_messages(client_id)
        messages.extend(history)
        # 刚拍过照：把画面描述带进这一轮，追问「那是什么牌子」不用再拍一张。
        # 只保留 VISION_CONTEXT_SECONDS 秒，过期和重启都没了（不进记忆库）。
        observation = self._recent_observation(client_id)
        if observation:
            messages.append({
                "role": "system",
                "content": (
                    "刚才摄像头看到：%s\n"
                    "用户接着问这个画面里的东西时按这句话回答；画面外、看不清的"
                    "就说看不到，不要猜。" % observation
                ),
            })
        # 用户从提醒点进来：把那条任务告诉模型，省掉 task_list 这一步，
        # 也避免「完成了」被它安到别的任务上。只在这一轮有效。
        task_note = self._task_context_note(task_id)
        if task_note:
            messages.append({"role": "system", "content": task_note})
        # 软提醒（D81）：今天到期的几件事，聊到相关话题时顺口带一句。
        # 放在历史之后、用户这句话之前——每轮会变，塞进 system prompt 开头会
        # 让前缀缓存全部失效（D58 踩过）。
        soft_note = self._soft_reminder_note()
        if soft_note:
            messages.append({"role": "system", "content": soft_note})
        # 网页上传了待打印文件：只在这一轮告诉模型「有文件可打」，
        # 文件名/页数够了；正文不给模型看，避免把文件内容带进上下文。
        attachment_note = self._attachment_note(attachment)
        if attachment_note:
            messages.append({"role": "system", "content": attachment_note})
        messages.append({"role": "user", "content": user_text})
        # 没有摄像头的入口不给这个 schema：模型看不到就不会说「我看到了」；
        # file_read 只在有附件时露出来；run_python 任何时候都能生成文件，
        # 所以 printer_print 只要有打印机就给——没文件时调用会被拦下并说明原因。
        show_printer = self.printer_ready
        file_tools = {"file_read", "run_python"}
        schemas = [
            t["schema"] for t in self.tools
            if (device_camera or t["schema"]["function"]["name"] != "camera_look")
            and (t["schema"]["function"]["name"] != "printer_print" or show_printer)
            and (
                t["schema"]["function"]["name"] not in file_tools
                or t["schema"]["function"]["name"] == "run_python"
                or attachment
            )
        ]
        own_names = {s["function"]["name"] for s in schemas}
        schemas += [t for t in extra_tools if t["function"]["name"] not in own_names]
        tools_by_name = self._tools_by_name
        used_tools = []
        used_tool_names = set()
        executed_writes = []
        # 这一轮工具吐回来的原文（含参数）：回复里的长数字要能在这里找到出处，
        # 否则就是模型自己编的（户号、电话这种念错最要命）。
        executed_results = []
        # 用户原始上传的文件：run_python 每次都以它为输入，产物只影响「接下来打印哪个」，
        # 否则一次「探测环境」的产物会把输入顶掉，后面再也读不到原表
        origin_attachment = attachment
        if attachment:
            self._remember_file(attachment)
        # 这一轮的思考决策，结束时由 process_text 记一条（D63）
        self._turn.state = {"thinking": False, "why": "", "tools": used_tools}
        query_total = 0
        query_empty = 0
        blocked_reply = None
        summary = None
        search_keywords = None
        search_n = 5
        device_call = None

        max_loops = self.max_agent_loops
        if attachment:
            # 带文件的活天然多步：读内容 → 查工具 → 改文件 → 确认打印，
            # 5 轮不够用（2026-11-25 实测在「绕晕了」前刚好卡住）
            max_loops = max(max_loops, 10)
        # 模型要给 run_python 写整段代码，512 会在代码中间截断，
        # 截断后的半截代码会被当成回复念给用户（2026-11-25 实测）
        turn_tokens = 2048 if attachment else 512
        # 思考档位：默认按配置（语音场景是 off）；模型调 set_thinking(true) 后
        # 本轮改成 LLM_THINKING_EFFORT，下一轮用户说话时自然复位（D63）
        effort = None
        for _turn in range(max_loops):
            # 第一轮只决定「调不调工具、调哪个」，第二轮才组织要说的话；
            # 配了 decision_llm 就分开用，没配则两轮同一个模型。
            client = self.decision_llm if (_turn == 0 and self.decision_llm is not None) else self.llm
            try:
                # 已经写过东西时不流式：最终回答要过事实校验，逐字推出去的片段
                # 收不回来，校验失败就只能让用户听到错日期。
                can_stream = on_delta is not None and not executed_writes
                if can_stream and hasattr(client, "chat_with_tools_stream"):
                    data = client.chat_with_tools_stream(
                        messages, schemas, on_delta=on_delta,
                        max_tokens=turn_tokens, reasoning_effort=effort,
                    )
                else:
                    data = client.chat_with_tools(
                        messages, schemas, max_tokens=turn_tokens,
                        reasoning_effort=effort,
                    )
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
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id", ""),
                            "content": blocked,
                        })
                        continue
                    if name in passthrough_names:
                        # 小智服务端自己的插件（如 handle_exit_intent）：我们没有实现，
                        # 原样交回调用方去执行——它执行完会关会话，设备回待机。
                        device_call = {"name": name, "arguments": args or {}}
                        break
                    if name not in used_tool_names:
                        used_tools.append(name)
                        used_tool_names.add(name)
                    # 摄像头：画面在设备端，本机没有。把这次调用原样交给调用方
                    # （/v1 转成 tool_calls 让 xiaozhi 去拍），这一轮不再生成回复。
                    if name == "camera_look" and device_camera:
                        device_call = {
                            "name": "self.camera.take_photo",
                            "arguments": {
                                "question": (args.get("question") or user_text).strip(),
                            },
                        }
                        break
                    # 模型自己决定要不要开思考（D63）：默认关，本轮内生效
                    if name == "set_thinking":
                        wanted = (args or {}).get("enabled")
                        if isinstance(wanted, str):
                            wanted = wanted.strip().lower() in (
                                "1", "true", "yes", "是", "开",
                            )
                        if wanted:
                            effort = LLM_THINKING_EFFORT or "medium"
                            turn_tokens = max(turn_tokens, 2048)
                            result = "已开启深度思考，本轮后续步骤更稳，会稍慢。"
                        else:
                            effort = LLM_REASONING_EFFORT or None
                            result = "已关闭深度思考。"
                        state = getattr(self._turn, "state", None)
                        if state is not None:
                            state["thinking"] = bool(wanted)
                            state["why"] = str((args or {}).get("why") or "")[:120]
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id", ""),
                            "content": result,
                        })
                        print("🛠 set_thinking -> %s（reasoning=%s）" % (
                            "on" if wanted else "off", effort,
                        ))
                        continue
                    # 文件：先看内容、再用代码处理；处理后的新文件接替这一轮的附件，
                    # 后面调 printer_print 打的就是它（D62）。
                    if name in ("file_read", "run_python"):
                        try:
                            if name == "file_read" and not attachment:
                                result = "错误：这一轮没有附带文件。"
                            elif name == "file_read":
                                result = documents.read_document(
                                    attachment.get("path"), attachment.get("name")
                                )
                            else:
                                print("🐍 run_python 代码：%s" % str(
                                    (args or {}).get("code") or ""
                                ).replace("\n", " ")[:240])
                                result, attachment = self._process_file(
                                    origin_attachment, args or {}
                                )
                                self._remember_file(attachment)
                        except documents.DocumentError as exc:
                            result = "错误：%s" % exc
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id", ""),
                            "content": str(result),
                        })
                        print("🛠 %s -> %s" % (
                            name, str(result).replace("\n", " ")[:160]
                        ))
                        continue
                    # 打印（D61）：文件只在内存/临时目录里，确认后由适配器出纸。
                    # 无条件确认——纸和墨花掉就回不来，和画像一样不跟弱模型开关走。
                    if name == "printer_print":
                        if not self.printer_ready:
                            blocked_reply = "打印机现在连不上，等它恢复了再打。"
                            break
                        if not attachment:
                            blocked_reply = (
                                "我这边还没有文件，你点聊天框左边的 📎 传一个，"
                                "再说要打印就行。"
                            )
                            break
                        args, blocked = guard_print_args(args)
                        if blocked:
                            blocked_reply = blocked
                            break
                        payload = dict(args)
                        payload["path"] = attachment.get("path")
                        payload["name"] = attachment.get("name")
                        payload["file_id"] = attachment.get("id") or ""
                        payload["printer"] = self.printer.name
                        question = wording.print_question(payload, user_text)
                        conversation_id = (client_id or "cli").strip()
                        pending = self.confirmations.request(
                            conversation_id, "print_file", payload, question, user_text
                        )
                        raise ConfirmationRequired(question, pending)
                    # 画像（D59）：模型推断的偏好永远先问一句，
                    # 不跟 CONFIRM_WRITE_ACTIONS 走——用户的事后否认不能靠配置开关。
                    if name == "profile_set":
                        args, blocked = self._profile_args(args)
                        if blocked:
                            blocked_reply = blocked
                            break
                        question = wording.profile_question(args, user_text)
                        conversation_id = (client_id or "cli").strip()
                        # 先记一条 proposed：用户答不答应都能在审计里看到
                        self.proposals.record_profile_proposal(args, question)
                        pending = self.confirmations.request(
                            conversation_id, name, args, question, user_text
                        )
                        raise ConfirmationRequired(question, pending)
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
                        executed_results.append(str(result))
                        if name in wording.QUERY_TOOLS:
                            query_total += 1
                            if wording.is_empty_query(name, result):
                                query_empty += 1
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": str(result),
                    })
                    print("🛠 %s -> %s" % (
                        name, str(result).replace("\n", " ")[:160]
                    ))
                if device_call is not None:
                    break
                if blocked_reply is not None:
                    answer = blocked_reply
                    break
                # 关键词查询全空：没什么可总结的，模型只会顺着可能听错的词发挥
                # （「寄给谁？」）。直接用固定问句，也省掉一轮生成——语音里模型
                # 的话一出口就收不回来，事后再替换是没用的。
                if (query_total and query_empty == query_total
                        and used_tool_names <= set(wording.QUERY_TOOLS)):
                    answer = wording.EMPTY_QUERY_REPLY
                    break
                continue  # 下一轮：带着工具结果再问

            # 普通回答（finish=stop）
            answer = (message.get("content") or "").strip()
            # 有些模型会把工具调用写成 <invoke ...> 文本（多发生在输出被截断时）；
            # 这种半截文本不能当回复念给用户
            if "<invoke" in answer or "<tool_call" in answer:
                answer = "这一步我没走通，能再发一次吗？"
            break
        else:
            answer = "抱歉，我绕晕了，能再说一遍吗？"

        # 写进去之后必须让热点快照失效：否则同一天里改过的户号/删掉的记忆，
        # 下一轮还会从「已知信息」里被当成事实念出来（见 _invalidate_hot_contexts）
        if executed_writes:
            self._invalidate_hot_contexts()

        # 要看东西：把工具调用交给调用方去设备端执行。这里不生成回复（设备会把
        # 画面描述直接念出来），也不写库——照片是一次性的，不进记忆。
        if device_call is not None:
            is_camera = device_call.get("name") == "self.camera.take_photo"
            return "device_call", "", {
                "used_tools": used_tools,
                "domain": "vision" if is_camera else "chat",
                "action": "look" if is_camera else "passthrough",
                "device_call": device_call,
            }

        # 模型嘴上说的日期可能是旧值（实测 4B 会）。先过一遍事实校验：日期、星期、
        # 名字和工具参数对不上就整句换成参数拼的模板句；对得上时再补一句回显
        # （模型已经说到正确日期就不重复念）。
        answer, replaced = verify_reply(
            answer, executed_writes, user_text,
            evidence=[sys_prompt, soft_note, user_text]
            + [str(m.get("content") or "") for m in history]
            + executed_results,
        )
        if not replaced:
            prefix = wording.echo_prefix(executed_writes, answer)
            if prefix and answer:
                answer = prefix + answer

        # 类型映射：写入→record，查询→query，否则 chat
        if any(t in used_tools for t in (
            "memory_store", "item_add", "task_add", "fact_store"
        )):
            rtype = "record"
        elif any(t in used_tools for t in (
            "task_complete", "task_update", "task_cancel", "task_postpone"
        )):
            rtype = "chat"
        elif any(t in used_tools for t in (
            "memory_search", "memory_list", "item_search", "item_list",
            "task_list", "fact_search", "fact_list",
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

    @staticmethod
    def _profile_args(args):
        """画像主体归一成「用户」/「用户/<名字>」，和热点上下文用同一套前缀。

        缺方面或内容时不硬凑一句话去问「我猜你的的...」，直接告诉模型没听清。
        """
        args = dict(args or {})
        args["subject"] = profile_subject(args.get("subject"))
        for key in ("predicate", "value", "evidence"):
            args[key] = str(args.get(key) or "").strip()
        if not args["predicate"] or not args["value"]:
            return args, "要记的是哪方面、什么内容？我没太听明白。"
        return args, None

    def _run_tool_by_name(self, name, args):
        execute = self._tools_by_name.get(name)
        if execute is None:
            return f"错误：未知工具 {name}"
        return execute(**(args or {}))

    def _process_file(self, attachment, args):
        """跑一段模型写的 Python，返回 (给模型的说明, 新附件)。

        没有附件也可以跑：从零生成一个表格/PDF 再打印。
        新文件放在附件同目录（没有附件就放临时目录），打印成功时由
        `_print_file` 删掉；用户取消就跟着临时目录清。
        """
        import tempfile

        from adapters.printer import SUPPORTED_EXTENSIONS

        attachment = attachment or {}
        requested = str(args.get("output_name") or "").strip()
        fallback_ext = os.path.splitext(requested)[1].lower() or os.path.splitext(
            attachment.get("name") or ""
        )[1].lower() or ".txt"
        outcome = documents.run_code(
            args.get("code"), attachment.get("path"), out_name=requested
        )
        if not outcome.get("path"):
            # 只是查环境/试代码：不改打印目标，把输出原样带回去
            note = "代码执行完成，没有生成文件，打印目标保持不变。"
            if outcome.get("stdout"):
                note += "\n代码输出：%s" % outcome["stdout"]
            return note, attachment
        if not os.path.isfile(outcome["path"]):
            raise documents.DocumentError("产物不是一个文件，没法打印")
        # 以实际产物的扩展名为准：模型常常写 .xlsx 却传了个别的名字
        actual_ext = os.path.splitext(outcome["path"])[1].lower() or fallback_ext
        if actual_ext not in SUPPORTED_EXTENSIONS:
            shutil.rmtree(outcome.get("workdir") or "", ignore_errors=True)
            raise documents.DocumentError(
                "产物是 %s，打印认不了；打印只支持 %s。" %
                (actual_ext or "未知格式", "、".join(sorted(SUPPORTED_EXTENSIONS)))
            )
        source_dir = os.path.dirname(attachment.get("path") or "")
        if not source_dir or not os.path.isdir(source_dir):
            # 有打印记录（生产）就放持久目录；没有（CLI/测试）仍用临时目录，
            # 别让单测往仓库 data/ 里写文件。
            if self.print_store is not None:
                source_dir = self.print_files_dir
                os.makedirs(source_dir, exist_ok=True)
            else:
                source_dir = tempfile.mkdtemp(prefix="xiaoma_out_")
        stem = os.path.splitext(attachment.get("name") or "file")[0]
        if requested:
            final_name = os.path.splitext(requested)[0] + actual_ext
        else:
            final_name = stem + "_处理" + actual_ext
        final_path = os.path.join(source_dir, uuid.uuid4().hex + actual_ext)
        try:
            shutil.move(outcome["path"], final_path)
        finally:
            shutil.rmtree(outcome.get("workdir") or "", ignore_errors=True)

        updated = {
            "path": final_path,
            "name": final_name,
            "kind": kind_of(final_name),
            "size": os.path.getsize(final_path),
            "pages": pdf_page_count(final_path) if kind_of(final_name) == "pdf" else None,
        }
        if self.print_store is not None:
            file_id = uuid.uuid4().hex
            self.print_store.add(
                file_id, final_name, final_path,
                kind=updated["kind"], size=updated["size"],
                pages=updated["pages"], source="generated",
            )
            updated["id"] = file_id
        note = "已生成《%s》（%d 字节），这一步要打印的就是它。" % (
            final_name, updated["size"],
        )
        if outcome.get("stdout"):
            note += "\n代码输出：%s" % outcome["stdout"]
        return note, updated

    def _print_file(self, payload):
        """确认之后真正打印，并把结果记进打印记录（D65）。

        打印成功后**不删**文件：纸卡了、想再要一份，用户下一句「再打一份」很常见，
        而重新上传对手机不友好。文件按 `PRINT_KEEP_DAYS` 到期清理，重启后还在。
        """
        payload = payload or {}
        file_id = payload.get("file_id") or ""
        copies = payload.get("copies") or 1
        printer = payload.get("printer") or ""
        if file_id and self.print_store is not None:
            self.print_store.mark(
                file_id, "printing", copies=copies,
                printer=self.printer.name or printer,
            )
        result = self.printer.print_file(
            payload.get("path"),
            copies=copies,
            pages=payload.get("page_indices"),
            printer_name=printer,
            doc_name=payload.get("name") or "",
        )
        if file_id and self.print_store is not None:
            if result.get("ok"):
                self.print_store.mark(
                    file_id, "printed", copies=copies,
                    printer=result.get("printer") or self.printer.name or printer,
                    job_id=result.get("job_id") or "",
                    error="",
                )
            else:
                self.print_store.mark(
                    file_id, "failed", error=str(result.get("error") or "")[:300],
                )
        return result

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
            "profile_set": ("profile", "store"),
            "printer_print": ("printer", "print"),
            "time_now": ("date", "query"),
        }
        business = [mappings[name] for name in used_tools if name in mappings and name != "time_now"]
        if not business:
            return ("date", "query") if "time_now" in used_tools else ("chat", "chat")
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

