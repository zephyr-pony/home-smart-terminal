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


def test_max_steps_exhausted():
    executed = []
    calls = [_tool_call("web_search", {"query": "q"}) for _ in range(subagent.MAX_STEPS)]
    llm = ScriptedLLM(calls)
    text = subagent.run("一直查", llm, _tools(executed))
    assert "还没结论" in text


def test_llm_failure_does_not_raise():
    executed = []
    text = subagent.run("查一下", FailingLLM(), _tools(executed))
    assert "子任务失败" in text


def test_no_conclusion_returns_message():
    executed = []
    text = subagent.run("查一下", ScriptedLLM([_stop("")]), _tools(executed))
    assert "没有给出结论" in text
