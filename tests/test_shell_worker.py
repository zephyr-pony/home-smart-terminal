"""执行通道（D86）：协议、超时、越界目录、降权后的身份。

容器里通道跑在 work 用户下；测试里就是当前用户，验的是协议和边界判断，
uid 是否真的分开由 docker-entrypoint.sh 保证（doctor 会再核一次）。
"""
import os
import socket
import sys
import threading
import time

import pytest

from adapters import shell_worker

pytestmark = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="需要 AF_UNIX 套接字"
)


@pytest.fixture
def worker(tmp_path, monkeypatch):
    work = tmp_path / "work"
    home = tmp_path / "home"
    work.mkdir()
    home.mkdir()
    path = str(tmp_path / "shell.sock")
    monkeypatch.setenv("XIAOMA_WORK_DIR", str(work))
    monkeypatch.setenv("XIAOMA_HOME_DIR", str(home))
    monkeypatch.setenv("SHELL_WORKER_SOCKET", path)
    thread = threading.Thread(target=shell_worker.serve, args=(path,), daemon=True)
    thread.start()
    for _ in range(100):
        if os.path.exists(path):
            break
        time.sleep(0.02)
    else:
        raise AssertionError("执行通道没起来")
    return work


def _call(payload, wait=20):
    from adapters import shell

    return shell._worker_call(payload, wait)


def test_whoami_reports_the_channel_user(worker):
    reply = _call({"kind": "whoami"})
    assert reply["ok"] is True
    assert reply["uid"] == os.getuid()


@pytest.mark.skipif(os.name != "posix", reason="容器里才有 bash")
def test_shell_runs_in_work_dir(worker):
    reply = _call({"kind": "shell", "command": "pwd", "cwd": str(worker), "timeout": 10})
    assert reply["stdout"].strip() == os.path.realpath(str(worker))


@pytest.mark.skipif(os.name != "posix", reason="容器里才有 bash")
def test_shell_exit_code_and_stderr(worker):
    reply = _call({"kind": "shell", "command": "echo oops >&2; exit 3",
                   "cwd": str(worker), "timeout": 10})
    assert reply["returncode"] == 3
    assert "oops" in reply["stderr"]


@pytest.mark.skipif(os.name != "posix", reason="容器里才有 bash")
def test_shell_timeout_kills_the_process(worker):
    reply = _call({"kind": "shell", "command": "sleep 30",
                   "cwd": str(worker), "timeout": 1}, wait=15)
    assert reply["timed_out"] is True


def test_shell_cannot_escape_work_dir(worker, tmp_path):
    from adapters import shell

    with pytest.raises(shell.ShellError) as err:
        _call({"kind": "shell", "command": "ls", "cwd": str(tmp_path),
               "timeout": 5})
    assert "只能在" in str(err.value)


def test_python_script_runs_and_prints(worker):
    script = worker / "hello.py"
    script.write_text("print('hi from work')", encoding="utf-8")
    reply = _call({"kind": "python", "script": str(script), "cwd": str(worker),
                   "timeout": 20, "cpu_limit": 20})
    assert reply["returncode"] == 0
    assert "hi from work" in reply["stdout"]


def test_python_script_outside_work_is_refused(worker, tmp_path):
    from adapters import shell

    script = tmp_path / "outside.py"
    script.write_text("print(1)", encoding="utf-8")
    with pytest.raises(shell.ShellError) as err:
        _call({"kind": "python", "script": str(script), "cwd": str(worker),
               "timeout": 5})
    assert "只能在" in str(err.value)


def test_output_is_truncated(worker):
    script = worker / "spam.py"
    script.write_text("print('x' * 20000)", encoding="utf-8")
    reply = _call({"kind": "python", "script": str(script), "cwd": str(worker),
                   "timeout": 30, "cpu_limit": 30})
    assert len(reply["stdout"]) <= shell_worker.MAX_STDOUT_CHARS


def test_unknown_kind_is_rejected(worker):
    from adapters import shell

    with pytest.raises(shell.ShellError) as err:
        _call({"kind": "rm-rf"}, wait=5)
    assert "不认识的请求" in str(err.value)


def test_client_without_channel_returns_none(monkeypatch, tmp_path):
    """没有套接字 = 开发机，run_python 自己在本机跑。"""
    from adapters import shell

    monkeypatch.setenv("SHELL_WORKER_SOCKET", str(tmp_path / "nope.sock"))
    assert shell.worker_available() is False
    assert shell.run_python_script(__file__) is None
