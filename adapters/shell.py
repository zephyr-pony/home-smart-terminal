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
import os
import shutil
import subprocess

from config import DATA_DIR

TIMEOUT_SECONDS = 90
MAX_STDOUT_CHARS = 4000
MAX_STDERR_CHARS = 2000

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
    """跑一条 shell 命令，返回 {returncode, stdout, stderr}。"""
    command = str(command or "").strip()
    if not command:
        raise ShellError("没有要执行的命令")
    shell = _shell()
    if not shell:
        raise ShellError("这台机器没有 bash/sh")
    _prepare_dirs()
    workdir = cwd if cwd and os.path.isdir(cwd) else WORK_DIR
    try:
        timeout = max(5, min(int(timeout or TIMEOUT_SECONDS), 600))
    except (TypeError, ValueError):
        timeout = TIMEOUT_SECONDS

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
