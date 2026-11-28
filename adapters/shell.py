"""在服务器上跑命令、装环境（D62）。

为什么不是「给每个需求写一个工具」：用户的目标是无限的，工具写不完。
这里给一条通用的命令行通道，配一个会持久保存的工作目录：
    /data/work   下载的脚本、编译出来的东西
    /data/home   pip install --user / npm 的安装位置

安全边界（写清楚，别假装是沙箱）：
- 不继承服务进程的环境变量：API Token、LLM 密钥不会出现在命令里；
- 有超时（默认 90 秒），输出截断后再给模型；
- 以容器内的普通用户（xiaoma）运行，不是 root，装不了系统包；
  需要系统包时由人改 Dockerfile 重建镜像，这也是刻意的。
"""
import json
import os
import shutil
import socket
import subprocess

from config import DATA_DIR, SHELL_WORKER_SOCKET

TIMEOUT_SECONDS = 90
MAX_STDOUT_CHARS = 4000
MAX_STDERR_CHARS = 2000
# 等执行通道的宽限：命令自己有时限，这里只多等它把输出送回来
WORKER_GRACE_SECONDS = 15

WORK_DIR = os.path.join(DATA_DIR, "work")
HOME_DIR = os.path.join(DATA_DIR, "home")


class ShellError(RuntimeError):
    """命令没跑起来；消息直接给用户看。"""


def _prepare_dirs():
    os.makedirs(WORK_DIR, exist_ok=True)
    os.makedirs(HOME_DIR, exist_ok=True)


def _shell():
    return shutil.which("bash") or shutil.which("sh")


def _env():
    """只给命令留下它活命需要的变量。"""
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": HOME_DIR,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INPUT": "1",
        "TERM": "dumb",
    }
    # Windows 上 Python 初始化和代理设置需要这几个；Linux 上不存在也无害
    for key in ("SystemRoot", "SYSTEMROOT", "TEMP", "TMP"):
        if key in os.environ:
            env[key] = os.environ[key]
    # 如果部署里显式配了 HTTP_PROXY，让 pip/npm 也跟着走（默认没有，直连）
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
                "http_proxy", "https_proxy", "no_proxy"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def run(command, timeout=TIMEOUT_SECONDS, cwd=""):
    """跑一条 shell 命令，返回 {returncode, stdout, stderr}。

    容器里有执行通道（另一个 uid）就走它；开发机上没有套接字，退回本机。
    通道在但不通时直接报错，**不**悄悄退回本机——那等于把隔离丢了还不告诉人。
    """
    command = str(command or "").strip()
    if not command:
        raise ShellError("没有要执行的命令")
    _prepare_dirs()
    workdir = cwd if cwd and os.path.isdir(cwd) else WORK_DIR
    try:
        timeout = max(5, min(int(timeout or TIMEOUT_SECONDS), 600))
    except (TypeError, ValueError):
        timeout = TIMEOUT_SECONDS

    if worker_available():
        result = _worker_call(
            {"kind": "shell", "command": command,
             "cwd": workdir, "timeout": timeout},
            timeout + WORKER_GRACE_SECONDS,
        )
        if result.get("timed_out"):
            raise ShellError("命令超过 %d 秒还没结束，先拆小一点" % timeout)
        return result

    shell = _shell()
    if not shell:
        raise ShellError("这台机器没有 bash/sh")
    try:
        result = subprocess.run(
            [shell, "-lc", command],
            cwd=workdir, env=_env(), timeout=timeout,
            capture_output=True,
        )
    except subprocess.TimeoutExpired:
        raise ShellError("命令超过 %d 秒还没结束，先拆小一点" % timeout)
    except OSError as exc:
        raise ShellError("命令起不来：%s" % exc)

    return {
        "returncode": result.returncode,
        "stdout": (result.stdout or b"").decode("utf-8", errors="replace"),
        "stderr": (result.stderr or b"").decode("utf-8", errors="replace"),
    }


def run_python_script(script_path, cwd="", env=None, timeout=30, cpu_limit=None):
    """用执行通道跑一个脚本文件（run_python 用）。没有通道就返回 None。

    调用方拿走 None 时自己在本机跑（开发机/Windows），逻辑和以前一样。
    """
    if not worker_available():
        return None
    result = _worker_call(
        {
            "kind": "python",
            "script": script_path,
            "cwd": cwd or WORK_DIR,
            "timeout": timeout,
            "cpu_limit": cpu_limit or timeout,
            "env": {str(k): str(v) for k, v in (env or {}).items()},
        },
        timeout + WORKER_GRACE_SECONDS,
    )
    return result


def worker_available():
    """通道在不在。套接字路径不存在 = 没启用（开发机），不是故障。"""
    if not hasattr(socket, "AF_UNIX"):
        return False
    path = _worker_socket()
    return bool(path) and os.path.exists(path)


def worker_identity():
    """通道以哪个 uid 跑；问不到返回 None。doctor.py 用它核对是否真的降权了。"""
    if not worker_available():
        return None
    try:
        reply = _worker_call({"kind": "whoami"}, 10)
    except ShellError:
        return None
    return reply if reply.get("ok") else None


def _worker_socket():
    return os.environ.get("SHELL_WORKER_SOCKET", SHELL_WORKER_SOCKET)


def _worker_call(payload, wait_seconds):
    path = _worker_socket()
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(5)
    try:
        try:
            conn.connect(path)
        except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
            raise ShellError("执行通道没起来（%s），先别跑命令" % exc)
        conn.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        conn.settimeout(max(1, wait_seconds))
        buffer = b""
        while not buffer.endswith(b"\n"):
            chunk = conn.recv(65536)
            if not chunk:
                break
            buffer += chunk
            if len(buffer) > 1024 * 1024:
                raise ShellError("执行通道回的包太大")
    except socket.timeout:
        raise ShellError("执行通道一直没有回话，稍后再试")
    except OSError as exc:
        raise ShellError("执行通道出错：%s" % exc)
    finally:
        conn.close()

    try:
        reply = json.loads(buffer.decode("utf-8", errors="replace"))
    except ValueError:
        raise ShellError("执行通道回的不是 JSON")
    if not isinstance(reply, dict):
        raise ShellError("执行通道回的内容看不懂")
    if not reply.get("ok"):
        raise ShellError(str(reply.get("error") or "命令没跑起来"))
    return reply


def format_result(result):
    """给模型看的结果：退出码 + 输出，明确截断。"""
    stdout = result.get("stdout") or ""
    stderr = result.get("stderr") or ""
    parts = ["退出码：%s" % result.get("returncode")]
    if stdout.strip():
        parts.append("输出：\n%s" % stdout[:MAX_STDOUT_CHARS])
        if len(stdout) > MAX_STDOUT_CHARS:
            parts.append("…（输出已截断）")
    else:
        parts.append("输出：（空）")
    if stderr.strip():
        parts.append("错误输出：\n%s" % stderr[:MAX_STDERR_CHARS])
        if len(stderr) > MAX_STDERR_CHARS:
            parts.append("…（错误输出已截断）")
    return "\n".join(parts)
