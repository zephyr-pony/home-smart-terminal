"""通用能力（D62）：上网查、抓页面、跑命令装环境。

外部请求全部 mock；shell 测试在没有 bash/sh 的机器上跳过。
"""
import os
import shutil

import pytest

from adapters import shell as shell_mod
from adapters import web


# ---------- shell ----------

def _has_shell():
    return bool(shutil.which("bash") or shutil.which("sh"))


@pytest.fixture
def shell_dirs(tmp_path, monkeypatch):
    work = tmp_path / "work"
    home = tmp_path / "home"
    work.mkdir()
    home.mkdir()
    monkeypatch.setattr(shell_mod, "WORK_DIR", str(work))
    monkeypatch.setattr(shell_mod, "HOME_DIR", str(home))
    return work, home


@pytest.mark.skipif(not _has_shell(), reason="没有 bash/sh")
def test_run_shell_returns_output_and_exit_code(shell_dirs):
    result = shell_mod.run("echo hello")
    assert result["returncode"] == 0
    assert "hello" in result["stdout"]

    failed = shell_mod.run("exit 3")
    assert failed["returncode"] == 3
    text = shell_mod.format_result(failed)
    assert "退出码：3" in text


@pytest.mark.skipif(not _has_shell(), reason="没有 bash/sh")
def test_run_shell_does_not_leak_secrets(shell_dirs, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "super-secret")
    monkeypatch.setenv("LLM_API_KEY", "sk-secret")

    result = shell_mod.run("env")

    assert "super-secret" not in result["stdout"]
    assert "sk-secret" not in result["stdout"]


@pytest.mark.skipif(os.name != "posix", reason="路径语义只在 Linux 上成立")
def test_run_shell_uses_persistent_home(shell_dirs):
    work, home = shell_dirs
    result = shell_mod.run("pwd && echo $HOME")
    assert str(work) in result["stdout"]
    assert str(home) in result["stdout"]


@pytest.mark.skipif(not _has_shell(), reason="没有 bash/sh")
def test_run_shell_empty_command_is_error(shell_dirs):
    with pytest.raises(shell_mod.ShellError):
        shell_mod.run("   ")


# ---------- web ----------

class FakeResponse:
    def __init__(self, text="", status_code=200, headers=None):
        self.text = text
        self.status_code = status_code
        self.headers = headers or {"Content-Type": "text/html; charset=utf-8"}
        self.encoding = "utf-8"

    def iter_content(self, size):
        yield self.text.encode("utf-8")

    def close(self):
        pass


def test_search_parses_results(monkeypatch):
    html = (
        '<a rel="nofollow" class="result__a" href="https://github.com/a/b">'
        'excel-mcp-server</a>'
        '<a class="result__snippet">一个 MCP 服务器</a>'
        '<a rel="nofollow" class="result__a" href="https://example.com/x">'
        '另一个结果</a>'
        '<a class="result__snippet">摘要二</a>'
    )
    monkeypatch.setattr(
        web.requests, "get",
        lambda *a, **kw: FakeResponse(html),
    )

    results = web.search("excel mcp", n=2)

    assert results[0]["title"] == "excel-mcp-server"
    assert results[0]["url"].startswith("https://github.com/")
    assert "MCP" in results[0]["snippet"]
    text = web.format_results(results)
    assert "1. excel-mcp-server" in text


def test_search_failure_raises(monkeypatch):
    monkeypatch.setattr(
        web.requests, "get",
        lambda *a, **kw: FakeResponse("", status_code=502),
    )
    with pytest.raises(web.WebError):
        web.search("anything")


def test_fetch_strips_tags(monkeypatch):
    html = "<html><body><h1>标题</h1><script>x=1</script><p>正文段落</p></body></html>"
    monkeypatch.setattr(
        web.requests, "get",
        lambda *a, **kw: FakeResponse(html),
    )

    text = web.fetch("https://example.com/doc")

    assert "标题" in text and "正文段落" in text
    assert "<h1>" not in text and "x=1" not in text


def test_fetch_rejects_non_http_and_private_hosts():
    with pytest.raises(web.WebError):
        web.fetch("file:///etc/passwd")
    with pytest.raises(web.WebError):
        web.fetch("http://127.0.0.1:8208/healthz")
    with pytest.raises(web.WebError):
        web.fetch("http://192.168.1.1/admin")


class RawResponse:
    """按原始字节返回，用来测编码：requests 在没声明 charset 时默认 ISO-8859-1。"""

    def __init__(self, raw, headers=None, encoding="ISO-8859-1"):
        self._raw = raw
        self.status_code = 200
        self.headers = headers or {"Content-Type": "text/html"}
        self.encoding = encoding

    def iter_content(self, size):
        for start in range(0, len(self._raw), size):
            yield self._raw[start:start + size]

    def close(self):
        pass


def test_fetch_keeps_utf8_when_charset_missing(monkeypatch):
    """gov.cn 这类：内容是 UTF-8 但响应头没写 charset，别按 latin-1 解成乱码。"""
    html = "<html><body><p>国务院办公厅关于2027年部分节假日安排的通知</p></body></html>"
    monkeypatch.setattr(
        web.requests, "get",
        lambda *a, **kw: RawResponse(html.encode("utf-8")),
    )

    text = web.fetch("https://example.com/utf8")

    assert "国务院办公厅" in text
    assert "å" not in text


def test_fetch_decodes_gbk_page(monkeypatch):
    """老站 GBK/GB2312：声明了 charset 就按它解。"""
    html = "<html><body><p>春节放假调休安排</p></body></html>"
    monkeypatch.setattr(
        web.requests, "get",
        lambda *a, **kw: RawResponse(
            html.encode("gbk"),
            headers={"Content-Type": "text/html; charset=gb2312"},
        ),
    )

    text = web.fetch("https://example.com/gbk")

    assert "春节放假调休安排" in text


def test_fetch_falls_back_to_gb18030(monkeypatch):
    """GBK 内容又没声明 charset：UTF-8 解不开时退回 gb18030。"""
    html = "<html><body><p>劳动节放假五天</p></body></html>"
    monkeypatch.setattr(
        web.requests, "get",
        lambda *a, **kw: RawResponse(html.encode("gbk")),
    )

    text = web.fetch("https://example.com/gbk-nc")

    assert "劳动节放假五天" in text


def test_web_tools_are_registered_without_any_switch():
    """上网能力全入口都在，没有权限门槛（D74）：描述变了也不能把工具弄丢。"""
    from memory import Memory
    from tools import get_all_tools

    names = {item["schema"]["function"]["name"]
             for item in get_all_tools(Memory(":memory:"))}

    assert {"web_search", "web_fetch"} <= names


def test_search_description_tells_model_to_look_things_up():
    """D74 的落点：描述里要说清「不确定/会变的事实先搜」，不是只说找库和格式。"""
    from tools import get_capability_tools

    tool = {item["schema"]["function"]["name"]: item
            for item in get_capability_tools()}["web_search"]
    description = tool["schema"]["function"]["description"]

    assert "放假" in description
    assert "查不到" in description
