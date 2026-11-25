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
