import json
import os

from agent import skills
from assistant import Assistant
from agent.clarify import Clarifier
from domain.facts import Facts
from domain.store import AgentStore
from domain.tasks import Tasks
from items import Items
from memory import Memory


class FakeLLM:
    def __init__(self, reply="好的"):
        self.reply = reply
        self.calls = []

    def chat_with_tools(self, messages, tools, **kwargs):
        self.calls.append(list(messages))
        return {"choices": [{"message": {"content": self.reply}, "finish_reason": "stop"}]}


def _write_skill(directory, name, body="步骤：先看，再做。", meta=True, scripts=()):
    target = os.path.join(directory, name)
    os.makedirs(target, exist_ok=True)
    header = "---\nname: %s\ndescription: 一句话说明\n---\n" % name if meta else ""
    with open(os.path.join(target, "SKILL.md"), "w", encoding="utf-8") as fh:
        fh.write(header + body)
    for script, text in scripts:
        full = os.path.join(target, script)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(text)
    return target


def test_parse_skill_reads_front_matter(tmp_path):
    _write_skill(str(tmp_path), "表格整理", body="第一步：看结构。")
    skill = skills.parse_skill(os.path.join(str(tmp_path), "表格整理"))
    assert skill.name == "表格整理"
    assert skill.description == "一句话说明"
    assert "第一步" in skill.body


def test_parse_skill_without_front_matter_uses_dir_and_first_line(tmp_path):
    _write_skill(str(tmp_path), "demo", body="# 快递查询\n\n查单号。", meta=False)
    skill = skills.parse_skill(os.path.join(str(tmp_path), "demo"))
    assert skill.name == "demo"
    assert skill.description.startswith("快递查询")


def test_parse_skill_ignores_unclosed_front_matter(tmp_path):
    target = os.path.join(str(tmp_path), "broken")
    os.makedirs(target)
    with open(os.path.join(target, "SKILL.md"), "w", encoding="utf-8") as fh:
        fh.write("---\nname: broken\n正文还在")
    skill = skills.parse_skill(target)
    # 没有闭合的 --- 就当没有头部：名字回退成目录名，正文不能被吃掉
    assert skill.name == "broken"
    assert "正文还在" in skill.body


def test_parse_skill_missing_file(tmp_path):
    os.makedirs(os.path.join(str(tmp_path), "空目录"))
    assert skills.parse_skill(os.path.join(str(tmp_path), "空目录")) is None


def test_parse_skill_rejects_illegal_name(tmp_path):
    _write_skill(str(tmp_path), "坏名字", meta=False)
    path = os.path.join(str(tmp_path), "坏名字", "SKILL.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("---\nname: ../etc/passwd\n---\n正文")
    assert skills.parse_skill(os.path.join(str(tmp_path), "坏名字")) is None


def test_discover_later_dir_overrides(tmp_path):
    builtin = tmp_path / "builtin"
    user = tmp_path / "user"
    builtin.mkdir()
    user.mkdir()
    _write_skill(str(builtin), "demo", body="内置版本")
    _write_skill(str(user), "demo", body="用户版本")
    found = skills.discover([str(builtin), str(user)])
    assert found["demo"].body.strip() == "用户版本"


def test_format_list_and_load(tmp_path):
    _write_skill(str(tmp_path), "demo", body="正文内容")
    found = skills.discover([str(tmp_path)])
    text = skills.format_list(found)
    assert "demo" in text and "一句话说明" in text
    assert "正文内容" in skills.load(found, "demo")


def test_format_list_empty():
    assert "没有装任何技能" in skills.format_list({})


def test_load_unknown_lists_existing(tmp_path):
    _write_skill(str(tmp_path), "demo")
    found = skills.discover([str(tmp_path)])
    text = skills.load(found, "不存在")
    assert "没有「不存在」这个技能" in text
    assert "demo" in text


def test_load_text_lists_scripts(tmp_path):
    _write_skill(str(tmp_path), "demo", scripts=[("scripts/a.py", "print(1)\n")])
    found = skills.discover([str(tmp_path)])
    text = skills.load(found, "demo")
    assert "scripts/a.py" in text
    assert "1 行" in text


def make_assistant(llm=None):
    items = Items(":memory:")
    return Assistant(
        memory=Memory(":memory:"),
        items=items,
        llm=llm or FakeLLM(),
        clarifier=Clarifier(store=AgentStore(":memory:"), items=items),
        tasks=Tasks(":memory:"),
        facts=Facts(":memory:"),
    )


def test_assistant_registers_skill_tools_from_repo():
    assistant = make_assistant()
    names = {t["schema"]["function"]["name"] for t in assistant.tools}
    assert "skill_list" in names and "skill_load" in names
    assert "spawn_agent" in names


def test_skill_list_reports_repo_skill():
    assistant = make_assistant()
    tool = {t["schema"]["function"]["name"]: t["execute"] for t in assistant.tools}
    text = tool["skill_list"]()
    assert "表格整理" in text


def test_spawn_agent_hidden_unless_allowed():
    llm = FakeLLM("好的")
    assistant = make_assistant(llm=llm)

    class Recorder:
        def __init__(self, reply):
            self.reply = reply
            self.seen = []

        def chat_with_tools(self, messages, tools, **kwargs):
            self.seen.append([t["function"]["name"] for t in tools])
            return {"choices": [{"message": {"content": self.reply}, "finish_reason": "stop"}]}

    recorder = Recorder("好的")
    assistant.llm = recorder
    # 语音入口（默认）：不给
    assistant.process_text("你好", "voice-like")
    assert "spawn_agent" not in recorder.seen[-1]
    # 网页文字 / CLI：给
    assistant.process_text("你好", "web-like", allow_subagent=True)
    assert "spawn_agent" in recorder.seen[-1]
