"""技能包（D89）：一份 SKILL.md + 可选脚本，只教流程，不新增权限。

为什么不是「插件」：注册新工具就意味着脚本要 import 进主进程执行，等于绕开
D86 的双 uid 隔离（模型随手一句 `sqlite3 /data/agent.db` 就能碰到正式库）。
所以技能只做一件事——把「这类活该怎么做」讲清楚，执行仍然走现有的
`run_python` / `run_shell`（降权通道）和 `web_search` / `web_fetch`。

目录约定：

    skills/<技能名>/
      SKILL.md       可带 front matter（name / description），正文是步骤和边界
      scripts/*.py   可选，模型用 run_python 去读、去改、去跑

技能也不能写「必须这样说」的话术，更不能覆盖 guard / verify / confirm——
那三条是小马的边界，不是实现细节。
"""
import os
import re

SKILL_FILE = "SKILL.md"
MAX_BODY_CHARS = 6000
MAX_SCRIPTS = 20
# 技能名要能出现在工具参数里：字母数字下划线点横线，中文也算 \w
_NAME_RE = re.compile(r"^[\w\-.]{1,40}$", re.UNICODE)

_LIST_INTRO = "可用技能（这里只有名字和一句话；真要按它做，再 skill_load 拿完整步骤）："


class Skill(object):
    """一个装好的技能：名字、一句话、正文、目录里的脚本。"""

    def __init__(self, name, description, body, path, scripts):
        self.name = name
        self.description = description
        self.body = body
        self.path = path
        self.scripts = scripts or []

    def load_text(self):
        """给模型看的完整内容：正文 + 脚本清单（不含脚本源码，省上下文）。"""
        body = self.body.strip()
        if len(body) > MAX_BODY_CHARS:
            body = body[:MAX_BODY_CHARS].rstrip() + "\n…（正文太长，后面省略）"
        text = "技能：%s\n\n%s" % (self.name, body)
        if self.scripts:
            text += "\n\n技能目录：%s\n目录里的脚本（用 run_python 读它，或者照着改）：" % self.path
            for name, lines in self.scripts:
                text += "\n- %s（%d 行）" % (name, lines)
        return text


def _split_front_matter(text):
    """拆 `---` 包住的简单 key: value 头部，不引 yaml 依赖。

    没有闭合的 `---` 就当没有头部，别把正文第一段吃掉。
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta = {}
    for index in range(1, len(lines)):
        line = lines[index]
        if line.strip() == "---":
            return meta, "\n".join(lines[index + 1:])
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip().lower()] = value.strip()
    return {}, text


def _first_line(body):
    for line in body.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:60]
    return ""


def _collect_scripts(directory):
    """技能目录里有哪些脚本（只要文件名和行数，源码等模型真要时再读）。"""
    scripts = []
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
        for name in sorted(files):
            if not name.endswith((".py", ".sh", ".js")):
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, directory).replace(os.sep, "/")
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    count = sum(1 for _ in fh)
            except OSError:
                continue
            scripts.append((rel, count))
            if len(scripts) >= MAX_SCRIPTS:
                return scripts
    return scripts


def parse_skill(directory):
    """读一个技能目录；没有 SKILL.md 或名字不合法就返回 None。"""
    path = os.path.join(directory, SKILL_FILE)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except OSError:
        return None
    meta, body = _split_front_matter(raw)
    name = (meta.get("name") or os.path.basename(os.path.abspath(directory))).strip()
    if not _NAME_RE.match(name):
        return None
    description = (meta.get("description") or _first_line(body)).strip()[:80]
    return Skill(name, description, body, os.path.abspath(directory),
                 _collect_scripts(directory))


def discover(directories):
    """扫多个目录，返回 {名字: Skill}。

    后面的目录覆盖前面的：内置技能在仓库里，用户装的放空间目录，同名时以用户为准。
    """
    found = {}
    for directory in directories:
        if not directory or not os.path.isdir(directory):
            continue
        for entry in sorted(os.listdir(directory)):
            path = os.path.join(directory, entry)
            if not os.path.isdir(path) or entry.startswith("."):
                continue
            skill = parse_skill(path)
            if skill is not None:
                found[skill.name] = skill
    return found


def format_list(skills):
    """清单：只有名字和一句话。空的时候如实说没有。"""
    if not skills:
        return "现在没有装任何技能。"
    lines = [_LIST_INTRO]
    for name in sorted(skills):
        lines.append("- %s：%s" % (name, skills[name].description or "（没有描述）"))
    return "\n".join(lines)


def load(skills, name):
    """按名字取技能正文；名字不对就把现有的列出来，别让模型瞎猜。"""
    skill = (skills or {}).get(name)
    if skill is None:
        names = "、".join(sorted(skills or {})) or "（一个也没有）"
        return "没有「%s」这个技能。现有技能：%s" % (name, names)
    return skill.load_text()
