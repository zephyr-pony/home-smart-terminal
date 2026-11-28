import json

from agent import subagent


def _tool_call(name, arguments, call_id="call-1"):
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


def _stop(text):
    return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}


class ScriptedLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.tools_seen = []

    def chat_with_tools(self, messages, tools, **kwargs):
        self.tools_seen.append([t["function"]["name"] for t in tools])
        return self.responses.pop(0)


class FailingLLM:
    def chat_with_tools(self, messages, tools, **kwargs):
        raise RuntimeError("boom")


def _tools(executed):
    def make(name):
        def execute(**kwargs):
            executed.append((name, kwargs))
            return "结果：%s" % name
        return {
            "schema": {"type": "function", "function": {"name": name, "parameters": {}}},
            "execute": execute,
        }

    return [make("web_search"), make("run_python"), make("memory_store"), make("printer_print")]


def test_runs_readonly_tool_then_answers():
    executed = []
    llm = ScriptedLLM([
        _tool_call("web_search", {"query": "明天放假吗"}),
        _stop("明天放假，来源：gov.cn，2026-11-28。"),
    ])
    answer = subagent.run("明天放假吗", llm, _tools(executed))
    assert executed == [("web_search", {"query": "明天放假吗"})]
    assert "明天放假" in answer
    assert "web_search" in answer  # 结论后面附上用了哪些工具


def test_write_tool_is_rejected_and_not_executed():
    executed = []
    llm = ScriptedLLM([
        _tool_call("memory_store", {"text": "记住这个", "summary": "记"}),
        _stop("查完了。"),
    ])
    answer = subagent.run("记一下", llm, _tools(executed))
    assert executed == []          # 写工具一次都没被执行
    assert "查完了" in answer


def test_schema_only_contains_readonly_tools():
    executed = []
    llm = ScriptedLLM([_stop("好")])
    subagent.run("随便问问", llm, _tools(executed))
    names = llm.tools_seen[0]
    assert "web_search" in names and "run_python" in names
    assert "memory_store" not in names and "printer_print" not in names


def test_empty_task_returns_error():
    executed = []
    assert "没写清楚" in subagent.run("", ScriptedLLM([]), _tools(executed))
    assert executed == []


def test_no_readonly_tools_returns_error():
    tools = [{
        "schema": {"type": "function", "function": {"name": "memory_store", "parameters": {}}},
        "execute": lambda **kwargs: "x",
    }]
    text = subagent.run("查一下", ScriptedLLM([]), tools)
    assert "没有可用的只读工具" in text


def test_last_step_forces_an_answer():
    """最后一步不给工具，模型必须用已查到的内容作答，而不是再查一轮。"""
    executed = []
    calls = [_tool_call("web_search", {"query": "q%d" % i})
             for i in range(subagent.MAX_STEPS - 1)]
    calls.append(_stop("查到几条，都不确定具体日期。"))
    llm = ScriptedLLM(calls)
    text = subagent.run("一直查", llm, _tools(executed))
    assert "都不确定" in text
    # 最后一次调用时工具已经被收走
    assert llm.tools_seen[-1] == []
    # 前面每一步都真的执行了（参数不同，不会被去重挡住）
    assert len(executed) == subagent.MAX_STEPS - 1


def test_tool_calls_when_tools_are_gone_is_not_executed():
    """模型在最后一步还硬要调工具：不执行，如实说没结论。"""
    executed = []
    calls = [_tool_call("web_search", {"query": "q%d" % i})
             for i in range(subagent.MAX_STEPS)]
    text = subagent.run("一直查", ScriptedLLM(calls), _tools(executed))
    assert "没有给出结论" in text
    assert len(executed) == subagent.MAX_STEPS - 1


def test_same_call_twice_is_not_executed_twice():
    """同一组参数重复查不会换答案，只会烧掉步数——第二次起直接告诉模型别重复。"""
    executed = []
    llm = ScriptedLLM([
        _tool_call("web_search", {"query": "一样的"}),
        _tool_call("web_search", {"query": "一样的"}),
        _stop("查完了。"),
    ])
    text = subagent.run("查一下", llm, _tools(executed))
    assert len(executed) == 1
    assert "查完了" in text


def test_llm_failure_does_not_raise():
    executed = []
    text = subagent.run("查一下", FailingLLM(), _tools(executed))
    assert "子任务失败" in text


def test_no_conclusion_returns_message():
    executed = []
    text = subagent.run("查一下", ScriptedLLM([_stop("")]), _tools(executed))
    assert "没有给出结论" in text


def test_pseudo_tool_call_text_is_retried_once():
    """模型把 tool_call 写成正文（`< invoke`、`</｜tool_calls>`）时，催一次再要结论。"""
    executed = []
    llm = ScriptedLLM([
        _stop('<tool_calls>\n< invoke name="web_search">'),
        _stop("查到了：两家都开到 18:00。"),
    ])
    text = subagent.run("查营业时间", llm, _tools(executed))
    assert "18:00" in text
    assert "<" not in text.split("（子任务用了")[0]


def test_pseudo_tool_call_text_twice_gives_up():
    executed = []
    llm = ScriptedLLM([
        _stop('<invoke name="web_search">'),
        _stop('</｜tool_calls>'),
    ])
    text = subagent.run("查一下", llm, _tools(executed))
    assert "写成了正文" in text

