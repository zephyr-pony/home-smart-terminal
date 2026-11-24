import json

from agent import wording
from agent.clarify import Clarifier
from assistant import Assistant
from domain.facts import Facts
from domain.store import AgentStore
from domain.tasks import Tasks
from items import Items
from memory import Memory


class FakeLLM:
    """默认返回一句闲聊；记录收到的 messages 供断言。"""

    def __init__(self, reply="好的"):
        self.reply = reply
        self.calls = []

    def chat_with_tools(self, messages, tools, **kwargs):
        self.calls.append(list(messages))
        return _stop_reply(self.reply)


class ScriptedLLM:
    """按预设顺序返回响应，用于模拟工具调用回合。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.tools_seen = []

    def chat_with_tools(self, messages, tools, **kwargs):
        self.calls.append(list(messages))
        self.tools_seen.append([t["function"]["name"] for t in tools])
        return self.responses.pop(0)


class FailingLLM:
    def __init__(self, error=None):
        self.error = error or RuntimeError("boom")

    def chat_with_tools(self, messages, tools, **kwargs):
        raise self.error


def make_assistant(llm=None):
    items = Items(":memory:")
    clarifier = Clarifier(store=AgentStore(":memory:"), items=items)
    return Assistant(
        memory=Memory(":memory:"),
        items=items,
        llm=llm or FakeLLM(),
        clarifier=clarifier,
        tasks=Tasks(":memory:"),
        facts=Facts(":memory:"),
    )


def _tool_call_reply(name, arguments, call_id="call-1"):
    return {
        "choices": [{
            "message": {
                "content": "",
                "tool_calls": [{
                    "id": call_id,
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }],
            },
            "finish_reason": "tool_calls",
        }]
    }


def _stop_reply(content):
    return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}


def test_decision_llm_used_only_on_first_turn():
    """第一轮换模型：决策轮用它，工具结果回来那轮用回答模型。"""
    decision = ScriptedLLM([_tool_call_reply("task_list", {})])
    answer = FakeLLM("现在没有待办提醒。")
    assistant = make_assistant(llm=answer)
    assistant.decision_llm = decision
    assistant.process_text("我有哪些提醒", "test-decision-llm")
    assert len(decision.calls) == 1
    assert len(answer.calls) == 1
    # 回答轮看得到工具结果，决策轮看不到
    assert not any(m.get("role") == "tool" for m in decision.calls[0])
    assert any(m.get("role") == "tool" for m in answer.calls[0])


def test_no_decision_llm_keeps_single_model():
    answer = FakeLLM("好")
    assistant = make_assistant(llm=answer)
    assert assistant.decision_llm is None
    assistant.process_text("你好", "test-single-model")
    assert len(answer.calls) == 1


def test_tool_metadata():
    assert Assistant._tool_metadata(["item_search", "get_current_date"]) == ("items", "search")
    assert Assistant._tool_metadata(["memory_store"]) == ("memory", "store")
    assert Assistant._tool_metadata([]) == ("chat", "chat")


def test_session_ttl_and_limit():
    assistant = make_assistant()
    assistant.session_ttl_seconds = 0
    assistant._record_turn("old", "hello", "hi")
    assert assistant.get_history("old") == []

    assistant.session_ttl_seconds = 3600
    assistant.max_sessions = 1
    assistant._record_turn("first", "a", "b")
    assistant._record_turn("second", "c", "d")
    assert assistant.get_history("first") == []
    assert assistant.get_history("second") == [("c", "d")]
    assistant._record_turn("", "ignored", "ignored")
    assert "" not in assistant.sessions


def test_empty_result_has_structured_metadata():
    result = make_assistant().process_text("")
    assert result["type"] == "chat"
    assert result["domain"] == "chat"
    assert result["action"] == "chat"
    assert result["used_tools"] == []


def test_agent_loop_injects_session_history():
    """多轮上下文：同 client_id 的历史轮次要进 agent 的 messages。"""
    assistant = make_assistant()
    assistant._record_turn("c1", "我买了酸奶", "已记录")

    assistant.process_text("那牛奶呢", client_id="c1")

    messages = assistant.llm.calls[-1]
    pairs = [(m["role"], m["content"]) for m in messages]
    assert ("user", "我买了酸奶") in pairs
    assert ("assistant", "已记录") in pairs
    # 本轮问题必须排在最后
    assert messages[-1] == {"role": "user", "content": "那牛奶呢"}
    # 系统提示在最前
    assert messages[0]["role"] == "system"


def test_agent_loop_without_client_id_has_no_history():
    """无 client_id 时退化为单轮：messages 只有 system + 本轮。"""
    assistant = make_assistant()
    assistant._record_turn("c1", "我买了酸奶", "已记录")

    assistant.process_text("你好")

    messages = assistant.llm.calls[-1]
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1] == {"role": "user", "content": "你好"}


def test_context_messages_clips_long_history():
    assistant = make_assistant()
    assistant.session_context_max_chars = 4
    assistant._record_turn("c1", "很长的用户输入文本", "很长的助手回复内容")
    messages = assistant._context_messages("c1")
    assert messages[0]["content"] == "很长的用…"
    assert messages[1]["content"] == "很长的助…"


def test_agent_loop_returns_record_summary():
    assistant = make_assistant(ScriptedLLM([
        _tool_call_reply(
            "memory_store",
            {"text": "明天买电池", "summary": "明天买电池"},
        ),
        _stop_reply("记住了"),
    ]))
    result = assistant.process_text("明天买电池")

    assert result["type"] == "record"
    assert result["reply"] == "记住了"
    assert result["summary"] == "明天买电池"


def test_agent_loop_write_uses_second_round():
    """写操作之后仍交给模型组织回话：宁慢一点，不让模板代替理解。"""
    llm = ScriptedLLM([
        _tool_call_reply("item_add", {"name": "牛奶", "expiry_date": "2026-12-02"}),
        _stop_reply("记好了，牛奶 12 月 2 日过期"),
    ])
    assistant = make_assistant(llm)

    result = assistant.process_text("12月2日牛奶过期")

    assert len(llm.calls) == 2
    # 回复里没提到日期，回显会先补一句正确的
    assert result["reply"].endswith("记好了，牛奶 12 月 2 日过期")
    assert "12月2日（周三）" in result["reply"]
    assert assistant.items.count() == 1


def test_agent_loop_query_still_uses_a_second_round():
    """读操作没有模板句，仍要模型组织回答。"""
    llm = ScriptedLLM([
        _tool_call_reply("item_list", {}),
        _stop_reply("清单里没有东西"),
    ])
    assistant = make_assistant(llm)

    result = assistant.process_text("我记了哪些东西")

    assert len(llm.calls) == 2
    assert result["reply"] == "清单里没有东西"


def test_agent_loop_mixed_write_and_query_uses_second_round():
    llm = ScriptedLLM([
        {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "c1", "type": "function",
                            "function": {
                                "name": "item_add",
                                "arguments": json.dumps(
                                    {"name": "牛奶", "expiry_date": "2026-12-02"},
                                    ensure_ascii=False,
                                ),
                            },
                        },
                        {
                            "id": "c2", "type": "function",
                            "function": {"name": "item_list", "arguments": "{}"},
                        },
                    ],
                },
                "finish_reason": "tool_calls",
            }]
        },
        _stop_reply("记好了，清单里现在有牛奶"),
    ])
    assistant = make_assistant(llm)

    result = assistant.process_text("12月2日牛奶过期，顺便看看清单")

    assert len(llm.calls) == 2
    # 回复里没提到日期，回显会先补一句正确的
    assert result["reply"].endswith("记好了，清单里现在有牛奶")
    assert "12月2日（周三）" in result["reply"]


def test_agent_loop_failed_write_still_asks_the_model():
    """没写进去时不能让模板冒充成功。"""
    llm = ScriptedLLM([
        _tool_call_reply("item_add", {"name": "", "expiry_date": "2026-12-02"}),
        _stop_reply("这次没记上，物品名是空的"),
    ])
    assistant = make_assistant(llm)

    result = assistant.process_text("12月2日牛奶过期")

    assert len(llm.calls) == 2
    assert result["reply"] == "这次没记上，物品名是空的"
    assert assistant.items.count() == 0


def test_invented_summary_is_replaced_by_user_words():
    """模型把「明天」算成「8月9日」：记忆照记，摘要退回原话。"""
    assistant = make_assistant(ScriptedLLM([
        _tool_call_reply(
            "memory_store",
            {"text": "明天买电池", "summary": "8月9日买电池"},
        ),
        _stop_reply("记住了"),
    ]))
    result = assistant.process_text("明天买电池")

    rows = assistant.memory.list_all()
    assert len(rows) == 1
    assert rows[0]["summary"] == "明天买电池"
    assert "8月9日" not in rows[0]["summary"]
    assert result["summary"] == "明天买电池"
    assert "8月9日" not in result["reply"]


def test_agent_loop_returns_task_record():
    assistant = make_assistant(ScriptedLLM([
        _tool_call_reply(
            "task_add",
            {"type": "custom", "title": "买电池", "due_at": "2026-11-10"},
        ),
        _stop_reply("记下了"),
    ]))

    result = assistant.process_text("11月10日提醒我买电池")

    assert result["type"] == "record"
    assert result["domain"] == "tasks"
    assert result["summary"] == "买电池"
    assert assistant.tasks.count() == 1


def test_agent_loop_item_add_is_record():
    """物品写入也要是 record：网页与统计按 type 分卡片，缺了会被当成闲聊。"""
    assistant = make_assistant(ScriptedLLM([
        _tool_call_reply("item_add", {"name": "牛奶", "expiry_date": "2026-11-25"}),
        _stop_reply("记好了"),
    ]))

    result = assistant.process_text("牛奶11月25日过期")

    assert result["type"] == "record"
    assert result["domain"] == "items"


def test_agent_loop_returns_task_complete():
    assistant = make_assistant(ScriptedLLM([
        _tool_call_reply("task_list", {}),
        _tool_call_reply("task_complete", {"task_id": 1}, call_id="call-2"),
        _stop_reply("完成了"),
    ]))
    assistant.tasks.add("交电费", "2026-11-08")

    result = assistant.process_text("电费交了")

    assert result["type"] == "chat"
    assert result["domain"] == "tasks"
    assert result["action"] == "complete"
    assert assistant.tasks.list_all()[0]["status"] == "done"


def test_agent_loop_returns_task_list_query():
    assistant = make_assistant(ScriptedLLM([
        _tool_call_reply("task_list", {}),
        _stop_reply("你有一个任务"),
    ]))

    result = assistant.process_text("我有哪些提醒")

    assert result["type"] == "query"
    assert result["domain"] == "tasks"
    assert result["action"] == "list"


def test_assistant_asks_property_before_bill():
    assistant = make_assistant()
    assistant.clarifier.store.add_entity("property", "自己家")
    assistant.clarifier.store.add_entity("property", "爸妈家")

    result = assistant.process_text("明天要交电费", client_id="c1")

    assert result["type"] == "clarify"
    assert "自己家" in result["reply"]
    assert "爸妈家" in result["reply"]
    assert result["pending_action"]["missing_slot"] == "property"

    result2 = assistant.process_text("爸妈家", client_id="c1")

    assert result2["type"] == "chat"
    assert assistant.llm.calls[-1][-1] == {
        "role": "user",
        "content": "爸妈家，明天要交电费",
    }


def test_agent_loop_returns_search_memories():
    assistant = make_assistant(ScriptedLLM([
        _tool_call_reply("memory_search", {"keywords": "酸奶 过期"}),
        _stop_reply("酸奶8月5号过期"),
    ]))
    assistant.memory.store("酸奶8月5号过期", "酸奶8月5号过期")

    result = assistant.process_text("酸奶什么时候过期")

    assert result["type"] == "query"
    assert result["memories"]
    assert "酸奶" in result["memories"][0]


def test_agent_loop_degrades_when_llm_fails():
    result = make_assistant(FailingLLM()).process_text("你好")

    assert result["type"] == "chat"
    assert result["domain"] == "chat"
    assert result["action"] == "chat"
    assert result["used_tools"] == []
    assert "抱歉" in result["reply"]


def test_assistant_delete_requires_confirmation():
    assistant = make_assistant()
    assistant.memory.store("酸奶8月5号过期", "酸奶过期")

    result = assistant.process_text("忘掉酸奶", client_id="c1")

    assert result["type"] == "confirm"
    assert "确认删除" in result["reply"]
    assert assistant.memory.count() == 1

    result2 = assistant.process_text("确认", client_id="c1")

    assert result2["type"] == "delete"
    assert len(result2["deleted"]) == 1
    assert assistant.memory.count() == 0


def test_assistant_item_delete_requires_confirmation():
    assistant = make_assistant(ScriptedLLM([
        _tool_call_reply("item_delete", {"name": "牛奶"}),
        _stop_reply("好的"),
    ]))
    assistant.items.add("蒙牛牛奶")

    result = assistant.process_text("删掉牛奶", client_id="c1")

    assert result["type"] == "confirm"
    assert assistant.items.count() == 1

    result2 = assistant.process_text("确认", client_id="c1")

    assert result2["type"] == "chat"
    assert assistant.items.count() == 0


def test_agent_loop_reports_unknown_tool_without_crashing():
    assistant = make_assistant(ScriptedLLM([
        _tool_call_reply("no_such_tool", {}),
        _stop_reply("好的"),
    ]))
    result = assistant.process_text("你好")

    assert result["reply"] == "好的"
    tool_messages = [
        m["content"] for m in assistant.llm.calls[-1] if m.get("role") == "tool"
    ]
    assert tool_messages == ["错误：未知工具 no_such_tool"]


def test_fact_update_tool_result_carries_the_invalidated_value():
    """事实被覆盖时，「原值已失效」要进模型上下文，否则它会以为旧值还有效。"""
    assistant = make_assistant(ScriptedLLM([
        _tool_call_reply(
            "fact_store",
            {"subject": "牛奶", "predicate": "品牌", "value": "伊利"},
        ),
        _stop_reply("牛奶现在是伊利，原来的蒙牛已经失效了"),
    ]))
    assistant.facts.store("牛奶", "品牌", "蒙牛")

    result = assistant.process_text("牛奶现在是伊利的")

    tool_messages = [
        m["content"] for m in assistant.llm.calls[-1] if m.get("role") == "tool"
    ]
    assert any("原值 蒙牛 已失效" in content for content in tool_messages)
    assert "伊利" in result["reply"]


def test_empty_query_question_is_appended():
    """按错词查空了：换成固定问句，而不是顺着错词下结论。"""
    llm = ScriptedLLM([
        _tool_call_reply("family_search", {"query": "寄东西"}),
        _stop_reply("没查到寄东西的信息"),
    ])
    assistant = make_assistant(llm)
    assistant._tools_by_name["family_search"] = (
        lambda **kwargs: "（没有找到相关记录）"
    )

    result = assistant.process_text("我寄了哪些东西")

    assert result["reply"] == wording.EMPTY_QUERY_REPLY
    assert len(llm.calls) == 1  # 空结果不再花一轮生成


def test_empty_query_replaces_a_wrong_followup_question():
    """模型自己追问「寄给谁」也算顺着错词走：一样换掉。"""
    llm = ScriptedLLM([
        _tool_call_reply("memory_search", {"keywords": "寄东西"}),
        _stop_reply("没查到。你寄的是什么，寄给谁？"),
    ])
    assistant = make_assistant(llm)
    assistant._tools_by_name["memory_search"] = lambda **kwargs: "（没有相关记忆）"

    result = assistant.process_text("我寄了哪些东西")

    assert result["reply"] == wording.EMPTY_QUERY_REPLY
    assert len(llm.calls) == 1  # 空结果不再花一轮生成


def test_empty_list_does_not_trigger_the_question():
    """清单类（task_list）本来就没有关键词，不该被当成听错。"""
    llm = ScriptedLLM([
        _tool_call_reply("task_list", {}),
        _stop_reply("你现在没有待办"),
    ])
    assistant = make_assistant(llm)

    result = assistant.process_text("我有哪些提醒")

    assert result["reply"] == "你现在没有待办"


# ---------- 摄像头（2026-11-23） ----------

def test_camera_tool_hidden_without_device():
    """网页/CLI 没有摄像头：schema 里不给这个工具，模型就没机会说「看到了」。"""
    llm = ScriptedLLM([_stop_reply("好的")])
    assistant = make_assistant(llm)

    assistant.process_text("你好")

    assert "camera_look" not in llm.tools_seen[0]


def test_camera_tool_offered_on_device():
    llm = ScriptedLLM([_stop_reply("好的")])
    assistant = make_assistant(llm)

    assistant.process_text("你好", device_camera=True)

    assert "camera_look" in llm.tools_seen[0]


def test_camera_look_becomes_device_call():
    """带摄像头时，「看看」不执行也不写库，而是把拍照交给调用方。"""
    llm = ScriptedLLM([_tool_call_reply("camera_look", {"question": "桌上有什么"})])
    assistant = make_assistant(llm)

    result = assistant.process_text("看看桌上有什么", "xiaozhi", device_camera=True)

    assert result["type"] == "device_call"
    assert result["reply"] == ""
    assert result["device_call"] == {
        "name": "self.camera.take_photo",
        "arguments": {"question": "桌上有什么"},
    }
    assert assistant.memory.count() == 0
    assert len(llm.calls) == 1  # 不再花一轮组织回话，照片结果由设备直接播报


def test_camera_look_falls_back_to_user_words():
    """模型没给 question 时用原话，别让设备拍完却问了个空问题。"""
    llm = ScriptedLLM([_tool_call_reply("camera_look", {})])
    assistant = make_assistant(llm)

    result = assistant.process_text("看看冰箱里还有牛奶吗", "xiaozhi", device_camera=True)

    assert result["device_call"]["arguments"]["question"] == "看看冰箱里还有牛奶吗"


def test_camera_look_without_device_says_it_cannot():
    llm = ScriptedLLM([
        _tool_call_reply("camera_look", {"question": "桌上有什么"}),
        _stop_reply("这个入口看不到画面。"),
    ])
    assistant = make_assistant(llm)

    result = assistant.process_text("看看桌上有什么")

    assert result["type"] == "chat"
    assert "camera_look" in result["used_tools"]
    assert result["reply"] == "这个入口看不到画面。"


class VisionLLM:
    def __init__(self, reply="左边是个红色的方块。", error=None):
        self.reply = reply
        self.error = error
        self.messages = None

    def chat(self, messages, **kwargs):
        self.messages = messages
        if self.error:
            raise self.error
        return _stop_reply(self.reply)


def test_describe_image_passes_image_to_model():
    llm = VisionLLM("桌上有一盒牛奶。")
    assistant = make_assistant(llm)

    reply = assistant.describe_image("桌上有什么", ["data:image/jpeg;base64,AAAA"])

    assert reply == "桌上有一盒牛奶。"
    parts = llm.messages[1]["content"]
    assert parts[0] == {"type": "text", "text": "桌上有什么"}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"] == "data:image/jpeg;base64,AAAA"
    assert assistant.memory.count() == 0  # 画面不进记忆


def test_describe_image_keeps_two_images_at_most():
    llm = VisionLLM()
    assistant = make_assistant(llm)

    assistant.describe_image("看看", ["data:image/jpeg;base64,A"] * 5)

    assert len(llm.messages[1]["content"]) == 3  # 文本 + 两张图


def test_describe_image_degrades_on_error():
    """看图失败要老实说没看清，不能编一句「看到了」。"""
    assistant = make_assistant(VisionLLM(error=RuntimeError("gateway down")))

    reply = assistant.describe_image("桌上有什么", ["data:image/jpeg;base64,AAAA"])

    assert "没看清" in reply


class ObservationLLM:
    """同时扮演看图（chat）和 agent（chat_with_tools）。"""

    def __init__(self, vision_reply="桌上有一盒牛奶。", reply="是伊利的。", error=None):
        self.vision_reply = vision_reply
        self.reply = reply
        self.error = error
        self.calls = []
        self.vision_calls = 0

    def chat(self, messages, **kwargs):
        self.vision_calls += 1
        if self.error:
            raise self.error
        return _stop_reply(self.vision_reply)

    def chat_with_tools(self, messages, tools, **kwargs):
        self.calls.append(list(messages))
        return _stop_reply(self.reply)


def test_vision_description_is_used_in_follow_up():
    """拍完接着问，要把刚才看到的带进这一轮，而不是再拍一张。"""
    llm = ObservationLLM()
    assistant = make_assistant(llm)

    assistant.describe_image("桌上有什么", ["data:image/jpeg;base64,AA"])
    assistant.process_text("那是什么牌子的", "xiaozhi", device_camera=True)

    messages = llm.calls[-1]
    assert any("桌上有一盒牛奶" in str(m.get("content") or "")
               for m in messages if m.get("role") == "system")
    assert llm.vision_calls == 1  # 没有重新拍


def test_vision_description_expires():
    llm = ObservationLLM()
    assistant = make_assistant(llm)
    assistant.vision_context_seconds = 0

    assistant.describe_image("桌上有什么", ["data:image/jpeg;base64,AA"])
    assistant.process_text("那是什么牌子的", "xiaozhi", device_camera=True)

    messages = llm.calls[-1]
    assert not any("桌上有一盒牛奶" in str(m.get("content") or "") for m in messages)


def test_failed_vision_leaves_no_observation():
    llm = ObservationLLM(error=RuntimeError("boom"))
    assistant = make_assistant(llm)

    assert "没看清" in assistant.describe_image("桌上有什么", ["data:image/jpeg;base64,AA"])
    assistant.process_text("那是什么牌子的", "xiaozhi", device_camera=True)

    messages = llm.calls[-1]
    assert not any("刚才摄像头看到" in str(m.get("content") or "") for m in messages)


def test_vision_observation_is_per_client():
    """一个客户端的画面不该被另一个客户端拿来回答。"""
    llm = ObservationLLM()
    assistant = make_assistant(llm)

    assistant.describe_image("桌上有什么", ["data:image/jpeg;base64,AA"], client_id="xiaozhi")
    assistant.process_text("那是什么牌子的", "cli")

    messages = llm.calls[-1]
    assert not any("刚才摄像头看到" in str(m.get("content") or "") for m in messages)


# ---------- 任务上下文（点提醒进来） ----------

def test_bound_task_is_told_to_the_model():
    """点开提醒后，「完成了」要指向那条任务，不用先 task_list。"""
    llm = FakeLLM("好，这条完成了")
    assistant = make_assistant(llm)
    task = assistant.tasks.add("交电费", due_at="2026-11-24T09:00:00", type="bill")

    assistant.process_text("完成了", "web", task_id=task["id"])

    notes = [
        str(m.get("content") or "")
        for m in llm.calls[0] if m.get("role") == "system"
    ]
    joined = "\n".join(notes)
    assert "当前任务：#%d" % task["id"] in joined
    assert "task_id=%d" % task["id"] in joined
    assert "不要先 task_list" in joined


def test_bound_task_lets_the_model_complete_without_listing():
    llm = ScriptedLLM([
        _tool_call_reply("task_complete", {"task_id": 1}),
        _stop_reply("交电费这条完成了"),
    ])
    assistant = make_assistant(llm)
    task = assistant.tasks.add("交电费", due_at="2026-11-24T09:00:00", type="bill")

    result = assistant.process_text("完成了", "web", task_id=task["id"])

    assert result["used_tools"] == ["task_complete"]
    assert assistant.tasks.get(task["id"])["status"] == "done"


def test_missing_bound_task_is_flagged_not_guessed():
    """旧通知点进来，任务已经没了：要说清楚，不拿别的任务顶。"""
    llm = FakeLLM("这条提醒已经不在了")
    assistant = make_assistant(llm)

    assistant.process_text("完成了", "web", task_id=9999)

    notes = [
        str(m.get("content") or "")
        for m in llm.calls[0] if m.get("role") == "system"
    ]
    joined = "\n".join(notes)
    assert "9999" in joined
    assert "已经不存在" in joined


def test_task_context_only_applies_to_that_turn():
    """绑定只在这一轮注入，不能留在历史里带偏下一句。"""
    llm = FakeLLM("好的")
    assistant = make_assistant(llm)
    task = assistant.tasks.add("交电费", due_at="2026-11-24T09:00:00", type="bill")

    assistant.process_text("完成了", "web", task_id=task["id"])
    assistant.process_text("今天天气怎么样", "web")

    notes = [
        str(m.get("content") or "")
        for m in llm.calls[-1] if m.get("role") == "system"
    ]
    assert not any("task_id=%d" % task["id"] in text for text in notes)
