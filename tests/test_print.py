"""打印（D61）：CUPS 命令、页码解析、附件对话流程、上传接口。

不依赖本机装 CUPS：外部命令用 `_run` 的假实现替换，Windows/Linux CI 都能跑。
"""
import json
import os
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import server
from adapters import printer as printer_mod
from domain import prints as print_store_mod
from adapters.printer import (
    PrintError,
    Printer,
    parse_pages,
    validate_file,
)
from agent.clarify import Clarifier
from agent import guard
from assistant import Assistant, PRINTER_STATUS_TTL_SECONDS
from domain.facts import Facts
from domain.store import AgentStore
from domain.tasks import Tasks
from items import Items
from memory import Memory

AUTH = {"Authorization": "Bearer secret"}


# ---------- 外部命令假实现 ----------

class FakeRun:
    """按命令分派的假的 subprocess.run。"""

    def __init__(self, handler=None):
        self.calls = []
        self.handler = handler or (lambda cmd: (0, "", ""))

    def __call__(self, cmd, timeout=60):
        self.calls.append(list(cmd))
        code, out, err = self.handler(cmd)
        return SimpleNamespace(returncode=code, stdout=out, stderr=err)


def _cups_handler(queues=("xiaoma",), default="xiaoma"):
    def handler(cmd):
        if cmd[0] == "lpstat" and "-a" in cmd:
            text = "".join("%s accepting requests since now\n" % q for q in queues)
            return 0, text, ""
        if cmd[0] == "lpstat" and "-d" in cmd:
            return 0, "system default destination: %s\n" % default, ""
        if cmd[0] == "lpstat" and "-p" in cmd:
            return 0, "printer %s is idle\n" % cmd[-1], ""
        if cmd[0] == "lp":
            return 0, "request id is %s-42 (1 file(s))\n" % default, ""
        return 0, "", ""
    return handler


def _fake_which(name):
    if name in ("lp", "lpstat", "soffice", "libreoffice"):
        return "/usr/bin/" + name
    return None


# ---------- 页码解析 ----------

def test_parse_pages_forms():
    assert parse_pages("1-3,5", 10) == [0, 1, 2, 4]
    assert parse_pages("第1页", 3) == [0]
    assert parse_pages("3-1", 3) == [0, 1, 2]
    assert parse_pages("all", 3) == [0, 1, 2]
    assert parse_pages(None, 3) == [0, 1, 2]
    assert parse_pages("1-5", 3) == [0, 1, 2]
    assert parse_pages([1, 0], 3) == [0, 1]
    assert parse_pages("1-3,5", None) == [0, 1, 2, 4]


def test_parse_pages_rejects_nonsense():
    for spec in ("abc", "0", "1-", "-2", "9", "1.5"):
        with pytest.raises(PrintError):
            parse_pages(spec, 3)


def test_compact_ranges():
    from adapters.printer import _compact_ranges

    assert _compact_ranges([0, 1, 2, 4]) == "1-3,5"
    assert _compact_ranges([2]) == "3"


# ---------- 上传内容校验 ----------

def test_validate_file_checks_magic(tmp_path):
    fake_pdf = tmp_path / "a.pdf"
    fake_pdf.write_text("not a pdf")
    with pytest.raises(PrintError):
        validate_file(str(fake_pdf), "a.pdf")

    txt = tmp_path / "a.txt"
    txt.write_text("hello")
    assert validate_file(str(txt), "a.txt") == ".txt"

    with pytest.raises(PrintError):
        validate_file(str(txt), "a.exe")


def test_validate_image(tmp_path):
    from PIL import Image

    path = tmp_path / "a.png"
    Image.new("RGB", (20, 10), "white").save(path)
    assert validate_file(str(path), "a.png") == ".png"


# ---------- CUPS 命令 ----------

def test_available_checks_queue(monkeypatch):
    monkeypatch.setattr(printer_mod.shutil, "which", _fake_which)
    monkeypatch.setattr(printer_mod, "_run", FakeRun(_cups_handler()))

    assert Printer("xiaoma").available() is True
    assert Printer("not-installed").available() is False


def test_print_file_builds_lp_command(tmp_path, monkeypatch):
    path = tmp_path / "a.txt"
    path.write_text("hello", encoding="utf-8")
    fake = FakeRun(_cups_handler())
    monkeypatch.setattr(printer_mod.shutil, "which", _fake_which)
    monkeypatch.setattr(printer_mod, "_run", fake)

    result = Printer("xiaoma", max_pages=50, max_copies=20).print_file(
        str(path), copies=2, pages="1",
    )

    assert result["ok"] is True
    assert result["job_id"] == "xiaoma-42"
    assert result["copies"] == 2
    lp_call = [call for call in fake.calls if call[0] == "lp"][0]
    assert lp_call[:6] == ["lp", "-d", "xiaoma", "-n", "2", "-t"]
    assert "-o" in lp_call
    assert "page-ranges=1" in lp_call
    assert lp_call[-1] == str(path)


def test_remote_cups_server_uses_host_flag(tmp_path, monkeypatch):
    path = tmp_path / "a.txt"
    path.write_text("hello", encoding="utf-8")
    fake = FakeRun(_cups_handler(queues=("HP1020",), default="HP1020"))
    monkeypatch.setattr(printer_mod.shutil, "which", _fake_which)
    monkeypatch.setattr(printer_mod, "_run", fake)

    printer = Printer("HP1020", server="http://192.168.1.15:631/")
    assert printer.server == "192.168.1.15:631"
    assert printer.available() is True

    result = printer.print_file(str(path), copies=1)
    assert result["printer"] == "HP1020"

    lpstat_calls = [call for call in fake.calls if call[0] == "lpstat"]
    assert lpstat_calls
    assert all(call[1:3] == ["-h", "192.168.1.15:631"] for call in lpstat_calls)

    lp_call = [call for call in fake.calls if call[0] == "lp"][0]
    assert lp_call[:5] == ["lp", "-h", "192.168.1.15:631", "-d", "HP1020"]


def test_pdf_uses_real_page_count(tmp_path, monkeypatch):
    path = tmp_path / "a.pdf"
    path.write_bytes(b"%PDF-1.4 fake")
    fake = FakeRun(_cups_handler())
    monkeypatch.setattr(printer_mod.shutil, "which", _fake_which)
    monkeypatch.setattr(printer_mod, "_run", fake)
    monkeypatch.setattr(printer_mod, "pdf_page_count", lambda p: 5)

    result = Printer("xiaoma").print_file(str(path), pages=[0, 1, 2])

    assert result["pages"] == 3
    lp_call = [call for call in fake.calls if call[0] == "lp"][0]
    assert "page-ranges=1-3" in lp_call


def test_too_many_pages_rejected(tmp_path, monkeypatch):
    path = tmp_path / "a.pdf"
    path.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(printer_mod, "pdf_page_count", lambda p: 5)
    monkeypatch.setattr(printer_mod, "_run", FakeRun(_cups_handler()))

    with pytest.raises(PrintError):
        Printer("xiaoma", max_pages=2).print_file(
            str(path), pages=[0, 1, 2],
        )


def test_lp_failure_is_reported(tmp_path, monkeypatch):
    path = tmp_path / "a.txt"
    path.write_text("hello", encoding="utf-8")
    monkeypatch.setattr(printer_mod.shutil, "which", _fake_which)

    def handler(cmd):
        if cmd[0] == "lpstat":
            return 0, "xiaoma accepting requests\n", ""
        return 1, "", "No such printer"

    monkeypatch.setattr(printer_mod, "_run", FakeRun(handler))

    with pytest.raises(PrintError) as excinfo:
        Printer("xiaoma").print_file(str(path))
    assert "打印失败" in str(excinfo.value)


def test_missing_lp_is_reported(tmp_path, monkeypatch):
    path = tmp_path / "a.txt"
    path.write_text("hello", encoding="utf-8")
    monkeypatch.setattr(printer_mod.shutil, "which", lambda name: None)
    # 真实 _run：在 Windows/Linux 上都会 FileNotFoundError → PrintError
    with pytest.raises(PrintError):
        Printer("xiaoma").print_file(str(path))


# ---------- Office ----------

def _office_handler(created):
    def handler(cmd):
        if cmd[0] == "soffice" or cmd[0].endswith("/soffice"):
            out_dir = cmd[cmd.index("--outdir") + 1]
            source = cmd[-1]
            pdf = os.path.join(out_dir, os.path.splitext(os.path.basename(source))[0] + ".pdf")
            with open(pdf, "wb") as fh:
                fh.write(b"%PDF-1.4 converted")
            created.append(pdf)
            return 0, "", ""
        if cmd[0] == "lpstat":
            return 0, "xiaoma accepting requests\n", ""
        return 0, "request id is xiaoma-9 (1 file(s))\n", ""
    return handler


def test_office_is_converted_with_libreoffice(tmp_path, monkeypatch):
    path = tmp_path / "report.docx"
    path.write_bytes(b"PK\x03\x04 fake")
    created = []
    fake = FakeRun(_office_handler(created))
    monkeypatch.setattr(printer_mod.shutil, "which", _fake_which)
    monkeypatch.setattr(printer_mod, "_run", fake)

    result = Printer("xiaoma").print_file(str(path), copies=1)

    assert result["ok"] is True
    soffice_calls = [call for call in fake.calls if "soffice" in call[0]]
    assert soffice_calls and "--convert-to" in soffice_calls[0]
    lp_call = [call for call in fake.calls if call[0] == "lp"][0]
    assert lp_call[-1].endswith(".pdf")
    assert created and not os.path.exists(os.path.dirname(created[0]))


def test_office_without_libreoffice(tmp_path, monkeypatch):
    path = tmp_path / "report.docx"
    path.write_bytes(b"PK\x03\x04 fake")
    monkeypatch.setattr(
        printer_mod.shutil, "which",
        lambda name: "/usr/bin/lp" if name == "lp" else None,
    )
    with pytest.raises(PrintError) as excinfo:
        Printer("xiaoma").print_file(str(path))
    assert "LibreOffice" in str(excinfo.value)


# ---------- 参数护栏 ----------

def test_guard_print_args_clamps_and_parses(monkeypatch):
    monkeypatch.setattr(guard, "PRINTER_MAX_COPIES", 20)
    monkeypatch.setattr(guard, "PRINTER_MAX_PAGES", 50)

    args, blocked = guard.guard_print_args(
        {"copies": 999, "pages": "1-2", "printer": "别的队列"}
    )
    assert blocked is None
    assert args["copies"] == 20
    assert args["page_indices"] == [0, 1]
    assert args["pages"] == "1-2"
    assert "printer" not in args

    args, blocked = guard.guard_print_args({"copies": "abc"})
    assert blocked is None
    assert args["copies"] == 1
    assert args["page_indices"] is None

    args, blocked = guard.guard_print_args({"pages": "0"})
    assert blocked and "页码" in blocked


# ---------- 附件对话流程 ----------

class ScriptedLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat_with_tools(self, messages, tools, **kwargs):
        self.calls.append({"messages": messages, "tools": tools})
        return self.responses.pop(0)


def _tool_call(name, arguments):
    return {
        "choices": [{
            "message": {
                "content": "",
                "tool_calls": [{
                    "id": "call-1",
                    "function": {"name": name,
                                 "arguments": json.dumps(arguments)},
                }],
            },
            "finish_reason": "tool_calls",
        }]
    }


def _stop(content):
    return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}


class FakePrinter:
    def __init__(self, ready=True):
        self.name = "xiaoma"
        self.ready = ready
        self.calls = []
        self.status_calls = 0

    def available(self):
        return self.ready

    def status(self):
        self.status_calls += 1
        if self.ready:
            return {
                "enabled": True, "configured": True, "available": True,
                "name": self.name, "server": "", "queue": self.name,
                "state": "idle", "jobs": 0, "error": "",
            }
        return {
            "enabled": True, "configured": True, "available": False,
            "name": self.name, "server": "192.168.1.15:631", "queue": "",
            "state": "unknown", "jobs": None, "error": "命令超时：lpstat",
        }

    def print_file(self, path, copies=1, pages=None, printer_name="", doc_name=""):
        self.calls.append({
            "path": path, "copies": copies, "pages": pages,
            "printer_name": printer_name, "doc_name": doc_name,
        })
        return {
            "ok": True, "printer": self.name,
            "pages": len(pages or [0]), "copies": copies,
            "job_id": "xiaoma-1",
        }


def make_assistant(llm, printer=None):
    items = Items(":memory:")
    return Assistant(
        memory=Memory(":memory:"),
        items=items,
        llm=llm,
        clarifier=Clarifier(store=AgentStore(":memory:"), items=items),
        tasks=Tasks(":memory:"),
        facts=Facts(":memory:"),
        printer=printer or FakePrinter(),
    )


def _attachment(tmp_path, pages=1):
    path = tmp_path / "报价单.txt"
    path.write_text("报价单内容", encoding="utf-8")
    return {
        "id": "f1", "path": str(path), "name": "报价单.txt",
        "kind": "text", "size": path.stat().st_size, "pages": pages,
        "client_id": "c1",
    }


def test_printer_availability_refreshes(tmp_path):
    """打印机晚开机：不用重启小马，下一次请求自己就能打。"""
    printer = FakePrinter(ready=False)
    llm = ScriptedLLM([_stop("好")])
    assistant = make_assistant(llm, printer)
    assert assistant.printer_ready is False

    printer.ready = True
    assert assistant.refresh_printer(force=True) is True
    names = [t["schema"]["function"]["name"] for t in assistant.tools]
    assert "printer_print" in names

    printer.ready = False
    assert assistant.refresh_printer(force=True) is False


def test_print_tool_follows_printer_availability(tmp_path):
    """printer_print 跟着打印机可用性走，不再要求「这一轮先有附件」——
    模型可能先 run_python 生成文件再打印（D62）。"""
    llm = ScriptedLLM([_stop("好")])
    assistant = make_assistant(llm, FakePrinter(ready=False))
    assistant.process_text("你好", "c1")
    names = [tool["function"]["name"] for tool in llm.calls[-1]["tools"]]
    assert "printer_print" not in names

    llm2 = ScriptedLLM([_stop("好")])
    assistant2 = make_assistant(llm2)
    assistant2.process_text("你好", "c1")
    names2 = [tool["function"]["name"] for tool in llm2.calls[-1]["tools"]]
    assert "printer_print" in names2

    llm3 = ScriptedLLM([_stop("好")])
    assistant3 = make_assistant(llm3)
    assistant3.process_text("你好", "c1", attachment=_attachment(tmp_path))
    names3 = [tool["function"]["name"] for tool in llm3.calls[-1]["tools"]]
    assert "printer_print" in names3


def test_print_requires_confirmation_then_prints(tmp_path):
    attachment = _attachment(tmp_path)
    llm = ScriptedLLM([_tool_call("printer_print", {"copies": 2, "pages": "1"})])
    printer = FakePrinter()
    assistant = make_assistant(llm, printer)

    result = assistant.process_text("打印两份", "c1", attachment=attachment)

    assert result["type"] == "confirm"
    assert "报价单.txt" in result["reply"]
    assert "2 份" in result["reply"]
    assert printer.calls == []
    assert os.path.exists(attachment["path"])

    done = assistant.process_text("确认", "c1")

    assert done["action"] == "print"
    assert "已提交到打印队列" in done["reply"]
    assert len(printer.calls) == 1
    assert printer.calls[0]["copies"] == 2
    assert printer.calls[0]["pages"] == [0]
    # 打完不删：下一句「再打一份」还要靠它（TTL 到期由服务端清）
    assert os.path.exists(attachment["path"])


def test_run_python_output_becomes_printable_file(tmp_path):
    """模型先写代码生成文件，再打印它：不用为每种需求加工具（D62）。"""
    code = (
        "import os\n"
        "open(os.environ['XIAOMA_OUT'], 'w', encoding='utf-8').write('10 x 20 = 200')\n"
    )
    llm = ScriptedLLM([
        _tool_call("run_python", {"code": code, "output_name": "汇总.txt"}),
        _tool_call("printer_print", {"copies": 1}),
    ])
    printer = FakePrinter()
    assistant = make_assistant(llm, printer)

    asked = assistant.process_text("把两列相乘，再打印出来", "c1")

    assert asked["type"] == "confirm"
    assert "汇总.txt" in asked["reply"]
    assert printer.calls == []

    done = assistant.process_text("确认", "c1")
    assert done["action"] == "print"
    assert len(printer.calls) == 1
    printed = printer.calls[0]
    assert printed["doc_name"] == "汇总.txt"
    assert printed["path"].endswith(".txt")
    # 产物留着：确认后未打印，或想再打一份都还能用
    assert os.path.exists(printed["path"])


def test_file_tools_visibility(tmp_path):
    """file_read 只在有附件时露出；run_python 随时可用，打印没文件会被拦。"""
    llm = ScriptedLLM([_stop("好")])
    assistant = make_assistant(llm)
    assistant.process_text("你好", "c1")
    names = [tool["function"]["name"] for tool in llm.calls[-1]["tools"]]
    assert "file_read" not in names
    assert "run_python" in names and "find_tools" in names
    assert "printer_print" in names

    llm2 = ScriptedLLM([_stop("好")])
    assistant2 = make_assistant(llm2)
    assistant2.process_text("你好", "c1", attachment=_attachment(tmp_path))
    names2 = [tool["function"]["name"] for tool in llm2.calls[-1]["tools"]]
    assert "file_read" in names2


def test_print_without_file_is_blocked(tmp_path):
    llm = ScriptedLLM([_tool_call("printer_print", {"copies": 1})])
    printer = FakePrinter()
    assistant = make_assistant(llm, printer)

    result = assistant.process_text("打印", "c1")

    assert printer.calls == []
    assert "文件" in result["reply"]


def test_recent_file_is_reused_for_next_message(tmp_path):
    """网页的 📎 只跟一条消息走；下一句「再打一份」要能接上刚才的文件。"""
    attachment = _attachment(tmp_path)
    llm = ScriptedLLM([_stop("好的"), _tool_call("printer_print", {"copies": 1})])
    printer = FakePrinter()
    assistant = make_assistant(llm, printer)

    assistant.process_text("这个文件先别打", "c1", attachment=attachment)
    result = assistant.process_text("刚才的文件再重新打一份", "c1")

    assert result["type"] == "confirm"
    assert "报价单.txt" in result["reply"]
    # 提示里要说明是「刚才的」文件，别让模型以为这一轮又上传了
    notes = [
        m["content"] for m in llm.calls[-1]["messages"]
        if m.get("role") == "system" and "文件" in m.get("content", "")
    ]
    assert any("刚才" in note and "报价单.txt" in note for note in notes)
    names = [tool["function"]["name"] for tool in llm.calls[-1]["tools"]]
    assert "file_read" in names


def test_reprint_after_print_success(tmp_path):
    """打完不删文件：卡纸了、想再要一份，回一句「再打一份」就行。"""
    attachment = _attachment(tmp_path)
    llm = ScriptedLLM([_tool_call("printer_print", {"copies": 1})])
    printer = FakePrinter()
    assistant = make_assistant(llm, printer)

    assistant.process_text("打印", "c1", attachment=attachment)
    assistant.process_text("确认", "c1")
    assert len(printer.calls) == 1
    assert os.path.exists(attachment["path"])

    llm.responses.append(_tool_call("printer_print", {"copies": 1}))
    asked = assistant.process_text("再打一份", "c1")
    assert asked["type"] == "confirm"
    assistant.process_text("确认", "c1")
    assert len(printer.calls) == 2


def test_recent_file_shared_across_clients(tmp_path):
    """D64：单家庭部署先不分设备——手机上传的文件，电脑上也能「再打一份」。"""
    attachment = _attachment(tmp_path)
    llm = ScriptedLLM([
        _stop("好"),
        _tool_call("printer_print", {"copies": 1}),
    ])
    printer = FakePrinter()
    assistant = make_assistant(llm, printer)

    assistant.process_text("先放着", "phone-a", attachment=attachment)
    result = assistant.process_text("打印刚才那个", "phone-b")

    names = [tool["function"]["name"] for tool in llm.calls[-1]["tools"]]
    assert "file_read" in names
    assert result["type"] == "confirm"
    assert "报价单.txt" in result["reply"]


def test_recent_file_expires(tmp_path):
    attachment = _attachment(tmp_path)
    llm = ScriptedLLM([_stop("好"), _stop("好")])
    assistant = make_assistant(llm)

    assistant.process_text("先放着", "c1", attachment=attachment)
    assistant._recent_file["at"] -= 10 ** 6
    assistant.process_text("刚才的文件呢", "c1")

    names = [tool["function"]["name"] for tool in llm.calls[-1]["tools"]]
    assert "file_read" not in names


def _assistant_with_store(llm, store, printer=None):
    items = Items(":memory:")
    return Assistant(
        memory=Memory(":memory:"), items=items, llm=llm,
        clarifier=Clarifier(store=AgentStore(":memory:"), items=items),
        tasks=Tasks(":memory:"), facts=Facts(":memory:"),
        printer=printer or FakePrinter(), print_store=store,
    )


def test_recent_file_survives_restart(tmp_path):
    """重启后内存是空的，靠 prints.db 接上「刚才的文件」。"""
    path = tmp_path / "重启前.txt"
    path.write_text("x", encoding="utf-8")
    store = print_store_mod.PrintStore(str(tmp_path / "prints.db"))
    store.add("f1", "重启前.txt", str(path), kind="text", size=1)

    llm = ScriptedLLM([_tool_call("printer_print", {"copies": 1})])
    assistant = _assistant_with_store(llm, store)
    result = assistant.process_text("刚才的文件再打一份", "web")

    assert result["type"] == "confirm"
    assert "重启前.txt" in result["reply"]
    names = [tool["function"]["name"] for tool in llm.calls[-1]["tools"]]
    assert "file_read" in names


def test_print_result_is_recorded(tmp_path):
    """出纸成功后历史里能看到 printed 和 CUPS 任务号。"""
    attachment = _attachment(tmp_path)
    store = print_store_mod.PrintStore(str(tmp_path / "prints.db"))
    store.add("f1", attachment["name"], attachment["path"], kind="text",
              size=attachment["size"])
    llm = ScriptedLLM([_tool_call("printer_print", {"copies": 1})])
    assistant = _assistant_with_store(llm, store)

    assistant.process_text("打印", "c1", attachment=attachment)
    assistant.process_text("确认", "c1")

    row = store.get("f1")
    assert row["status"] == "printed"
    assert row["job_id"] == "xiaoma-1"


def test_cancel_marks_record_cancelled(tmp_path):
    attachment = _attachment(tmp_path)
    store = print_store_mod.PrintStore(str(tmp_path / "prints.db"))
    store.add("f1", attachment["name"], attachment["path"], kind="text")
    llm = ScriptedLLM([_tool_call("printer_print", {"copies": 1})])
    assistant = _assistant_with_store(llm, store)

    assistant.process_text("打印", "c1", attachment=attachment)
    assistant.process_text("取消", "c1")

    assert store.get("f1")["status"] == "cancelled"


def test_failed_print_is_recorded(tmp_path):
    attachment = _attachment(tmp_path)
    store = print_store_mod.PrintStore(str(tmp_path / "prints.db"))
    store.add("f1", attachment["name"], attachment["path"], kind="text")

    class BrokenPrinter(FakePrinter):
        def print_file(self, path, **kwargs):
            return {"ok": False, "error": "纸没了"}

    llm = ScriptedLLM([_tool_call("printer_print", {"copies": 1})])
    assistant = _assistant_with_store(llm, store, BrokenPrinter())

    assistant.process_text("打印", "c1", attachment=attachment)
    assistant.process_text("确认", "c1")

    row = store.get("f1")
    assert row["status"] == "failed"
    assert "纸没了" in row["error"]


def test_set_thinking_raises_effort_for_rest_of_turn(tmp_path):
    """模型自己要求「深度思考」后，同一轮后续调用改用 thinking 档位（D63）。"""
    class EffortLLM:
        def __init__(self, responses):
            self.responses = list(responses)
            self.efforts = []

        def chat_with_tools(self, messages, tools, **kwargs):
            self.efforts.append(kwargs.get("reasoning_effort"))
            return self.responses.pop(0)

    llm = EffortLLM([
        _tool_call("set_thinking", {"enabled": True, "why": "要算表格"}),
        _stop("好"),
    ])
    assistant = make_assistant(llm)
    assistant.process_text("算一下这个月的开支", "c1")

    assert llm.efforts[0] is None          # 第一轮用配置默认（off）
    assert llm.efforts[1] == "medium"      # 打开后本轮后续用 thinking 档


def test_cancel_print_keeps_file_and_prints_nothing(tmp_path):
    attachment = _attachment(tmp_path)
    llm = ScriptedLLM([_tool_call("printer_print", {"copies": 1})])
    printer = FakePrinter()
    assistant = make_assistant(llm, printer)

    assistant.process_text("打印", "c1", attachment=attachment)
    result = assistant.process_text("取消", "c1")

    assert result["action"] == "cancel"
    assert printer.calls == []
    assert os.path.exists(attachment["path"])


def test_bad_page_spec_asks_instead_of_printing(tmp_path):
    attachment = _attachment(tmp_path)
    llm = ScriptedLLM([_tool_call("printer_print", {"pages": "0"})])
    printer = FakePrinter()
    assistant = make_assistant(llm, printer)

    result = assistant.process_text("打印第0页", "c1", attachment=attachment)

    assert result["type"] == "chat"
    assert "页码" in result["reply"]
    assert printer.calls == []
    assert assistant.confirmations.get_pending("c1") is None


def test_printer_unavailable_hides_tool():
    llm = ScriptedLLM([_stop("好")])
    assistant = make_assistant(llm, FakePrinter(ready=False))
    assistant.process_text("你好", "c1")

    names = [tool["function"]["name"]
             for tool in llm.calls[-1]["tools"]]
    assert "printer_print" not in names


# ---------- 上传接口 ----------

class UploadAssistant:
    printer_ready = True

    def __init__(self):
        self.attachments = []

    def printer_status(self, force=False):
        if self.printer_ready:
            return {"available": True, "configured": True, "queue": "xiaoma",
                    "state": "idle", "jobs": 0, "error": ""}
        return {"available": False, "configured": True, "queue": "",
                "state": "unknown", "jobs": None,
                "error": "连不上 CUPS（192.168.1.15:631）"}

    def process_text(self, text, client_id=None, task_id=None, attachment=None):
        self.attachments.append(attachment)
        return {"type": "chat", "reply": "好"}


@pytest.fixture
def upload_server(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "API_TOKEN", "secret")
    monkeypatch.setattr(server, "_tmp_dir", str(tmp_path))
    monkeypatch.setattr(server, "PRINT_FILES_DIR", str(tmp_path / "print"))
    monkeypatch.setattr(
        server, "print_store",
        print_store_mod.PrintStore(str(tmp_path / "prints.db")),
    )
    assistant = UploadAssistant()
    monkeypatch.setattr(server, "assistant", assistant)

    async def fake_synth(text):
        return ""

    monkeypatch.setattr(server, "_synthesize", fake_synth)
    return TestClient(server.app), assistant, tmp_path


def test_upload_rejects_unknown_type(upload_server):
    client, _assistant, _tmp = upload_server
    response = client.post(
        "/api/print/files", headers=AUTH,
        files={"file": ("evil.exe", b"MZ", "application/octet-stream")},
    )
    assert response.status_code == 415


def test_upload_without_printer_is_503(upload_server, monkeypatch):
    client, assistant, _tmp = upload_server
    assistant.printer_ready = False
    response = client.post(
        "/api/print/files", headers=AUTH,
        files={"file": ("a.txt", b"hi", "text/plain")},
    )
    assert response.status_code == 503
    assert "CUPS" in response.json()["detail"]


def test_office_upload_needs_libreoffice(upload_server, monkeypatch):
    client, _assistant, _tmp = upload_server
    monkeypatch.setattr(server, "office_available", lambda: False)
    response = client.post(
        "/api/print/files", headers=AUTH,
        files={"file": ("a.docx", b"PK\x03\x04 fake", "application/octet-stream")},
    )
    assert response.status_code == 415
    assert "LibreOffice" in response.json()["detail"]


def test_upload_rejects_oversize(upload_server, monkeypatch):
    client, _assistant, _tmp = upload_server
    monkeypatch.setattr(server, "MAX_UPLOAD_BYTES", 4)
    response = client.post(
        "/api/print/files", headers=AUTH,
        files={"file": ("a.txt", b"1234567890", "text/plain")},
    )
    assert response.status_code == 413


def test_upload_then_chat_passes_attachment(upload_server):
    client, assistant, tmp_path = upload_server
    response = client.post(
        "/api/print/files", headers=AUTH,
        files={"file": ("报价单.txt", "打印内容".encode("utf-8"), "text/plain")},
        data={"client_id": "c1"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["name"] == "报价单.txt"
    assert payload["kind"] == "text"
    assert (tmp_path / "print" / (payload["file_id"] + ".txt")).exists()

    chat = client.post(
        "/api/chat", headers=AUTH,
        json={"text": "打印", "client_id": "c1", "file_id": payload["file_id"]},
    )
    assert chat.status_code == 200
    attachment = assistant.attachments[-1]
    assert attachment["name"] == "报价单.txt"
    assert attachment["path"].endswith(".txt")


def test_file_belongs_to_uploader(upload_server):
    client, _assistant, _tmp = upload_server
    response = client.post(
        "/api/print/files", headers=AUTH,
        files={"file": ("报价单.txt", b"hi", "text/plain")},
        data={"client_id": "c1"},
    )
    file_id = response.json()["file_id"]

    other = client.post(
        "/api/chat", headers=AUTH,
        json={"text": "打印", "client_id": "c2", "file_id": file_id},
    )
    assert other.status_code == 400


# ---------- 打印机状态（概览页先看能不能打） ----------

def _status_handler(jobs=0, disabled=False, queues=("xiaoma",)):
    def handler(cmd):
        if cmd[0] == "lpstat" and "-a" in cmd:
            text = "".join("%s accepting requests since now\n" % q for q in queues)
            return 0, text, ""
        if cmd[0] == "lpstat" and "-d" in cmd:
            return 0, "system default destination: %s\n" % queues[0], ""
        if cmd[0] == "lpstat" and "-p" in cmd:
            if disabled:
                return 0, "printer %s disabled since now\n" % queues[0], ""
            return 0, "printer %s is idle.  enabled since now\n" % queues[0], ""
        if cmd[0] == "lpstat" and "-o" in cmd:
            text = "".join("%s-%d user 1024 now\n" % (queues[0], i)
                           for i in range(jobs))
            return 0, text, ""
        return 0, "", ""
    return handler


def test_status_reports_idle_queue(monkeypatch):
    monkeypatch.setattr(printer_mod.shutil, "which", _fake_which)
    monkeypatch.setattr(printer_mod, "_run", FakeRun(_status_handler(jobs=2)))

    detail = Printer(name="xiaoma").status()

    assert detail["available"] is True
    assert detail["queue"] == "xiaoma"
    assert detail["state"] == "idle"
    assert detail["jobs"] == 2
    assert detail["error"] == ""


def test_status_reports_disabled_queue(monkeypatch):
    monkeypatch.setattr(printer_mod.shutil, "which", _fake_which)
    monkeypatch.setattr(printer_mod, "_run", FakeRun(_status_handler(disabled=True)))

    detail = Printer(name="xiaoma").status()

    assert detail["available"] is True
    assert detail["state"] == "disabled"
    assert "停用" in detail["error"]


def test_status_without_cups_says_so(monkeypatch):
    monkeypatch.setattr(printer_mod.shutil, "which", lambda name: None)

    detail = Printer(name="xiaoma").status()

    assert detail["available"] is False
    assert "CUPS" in detail["error"]


def test_status_survives_unreachable_server(monkeypatch):
    """打印机断电时 lpstat 会超时；状态查询不能因此抛异常。"""
    monkeypatch.setattr(printer_mod.shutil, "which", _fake_which)

    def boom(cmd, timeout=60):
        raise PrintError("命令超时：lpstat")

    monkeypatch.setattr(printer_mod, "_run", boom)

    detail = Printer(name="xiaoma", server="192.168.1.15:631").status()

    assert detail["available"] is False
    assert "超时" in detail["error"]
    assert detail["server"] == "192.168.1.15:631"


def test_assistant_caches_printer_status():
    printer = FakePrinter()
    assistant = make_assistant(SimpleNamespace(), printer=printer)

    first = assistant.printer_status(force=True)  # 网页「重查」走同步那条
    second = assistant.printer_status()

    assert first["available"] is True
    assert second["available"] is True
    assert printer.status_calls == 1  # 30 秒内不重复问 CUPS
    assistant.printer_status(force=True)
    assert printer.status_calls == 2


def test_first_printer_status_does_not_block():
    """启动后第一次也不能同步等 CUPS：先回「检测中」，后台查。"""
    printer = FakePrinter()
    assistant = make_assistant(SimpleNamespace(), printer=printer)

    detail = assistant.printer_status()

    assert detail["state"] == "checking"
    assert detail["stale"] is True

    deadline = time.time() + 2
    while printer.status_calls < 1 and time.time() < deadline:
        time.sleep(0.01)
    deadline = time.time() + 2
    while assistant.printer_status()["state"] == "checking" and time.time() < deadline:
        time.sleep(0.01)
    assert assistant.printer_status()["available"] is True


def test_stale_printer_status_returns_old_value_then_refreshes():
    """过期后先给旧值（不让页面等 lpstat 超时），后台再更新。"""
    printer = FakePrinter()
    assistant = make_assistant(SimpleNamespace(), printer=printer)
    assistant.printer_status(force=True)  # 写入缓存

    assistant._printer_detail_at -= PRINTER_STATUS_TTL_SECONDS + 1
    stale = assistant.printer_status()

    assert stale["stale"] is True
    assert stale["available"] is True  # 旧值照给

    deadline = time.time() + 2
    while printer.status_calls < 2 and time.time() < deadline:
        time.sleep(0.01)

    assert printer.status_calls == 2
    assert assistant.printer_status()["stale"] is False


def test_printer_endpoint_reports_offline(upload_server):
    client, assistant, _tmp = upload_server

    online = client.get("/api/printer", headers=AUTH).json()
    assert online["ok"] is True
    assert online["available"] is True
    assert online["queue"] == "xiaoma"

    assistant.printer_ready = False
    offline = client.get("/api/printer", headers=AUTH).json()
    assert offline["available"] is False
    assert "CUPS" in offline["error"]
