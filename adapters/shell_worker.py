"""把模型的命令交给另一个 uid 执行（D86）。

App 以 xiaoma 跑，执行通道以 work 跑：work 能穿过 /data、能写 /data/work，
但库文件是 640 xiaoma:xiaoma，它读不到也改不了。这条边界是 Linux 权限，
不是提示词，也不是「模型答应不写库」。

为什么不直接用 subprocess：同 uid 下，一段 run_python 就能 sqlite3 打开
/data/agent.db 改正式表，把「提案 → 确认 → 提交」整套绕过；提示词拦不住，
静态检查也拦不全（任意 Python）。所以要么换 uid，要么那条硬约束就只是口号。

协议：一行 JSON 请求，一行 JSON 响应，AF_UNIX 套接字：

    {"kind": "shell",  "command": "ls", "cwd": "/data/work", "timeout": 90}
    {"kind": "python", "script": "…/run.py", "cwd": "…", "timeout": 30,
     "cpu_limit": 30, "env": {"XIAOMA_OUT": "…"}}
    {"kind": "whoami"}

这个文件要能独立运行（构建时单独拷到 /opt，见 Dockerfile），所以不 import 项目模块。
"""
import json
import os
import signal
import socket
import subprocess
import sys

DEFAULT_SOCKET = "/run/xiaoma/shell.sock"
DEFAULT_WORK_DIR = "/data/work"
DEFAULT_HOME_DIR = "/data/home"
DEFAULT_TIMEOUT = 90
MAX_TIMEOUT = 600
MAX_REQUEST_BYTES = 512 * 1024
MAX_STDOUT_CHARS = 4000
MAX_STDERR_CHARS = 2000


def _work_dir():
    return os.environ.get("XIAOMA_WORK_DIR", DEFAULT_WORK_DIR)


def _home_dir():
    return os.environ.get("XIAOMA_HOME_DIR", DEFAULT_HOME_DIR)


def _inside_work(cwd):
    """只允许在 work 目录里跑；越界一律拒绝，不解释、不兜底。"""
    root = os.path.realpath(_work_dir())
    path = os.path.realpath(cwd or root)
    return path == root or path.startswith(root + os.sep)


def _shell_env():
    """命令能看到的全部环境。密钥不进这里，见 Dockerfile 的 env -i。"""
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": _home_dir(),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INPUT": "1",
        "TERM": "dumb",
    }
    return env


def _limits(cpu_limit):
    """子进程的资源上限。Windows 开发机没有 resource，跳过。"""
    try:
        import resource
    except ImportError:
        return None

    def apply():
        for name, value in (
            ("RLIMIT_CPU", int(cpu_limit or DEFAULT_TIMEOUT)),
            ("RLIMIT_AS", 2 * 1024 * 1024 * 1024),
        ):
            limit = getattr(resource, name, None)
            if limit is None:
                continue
            try:
                resource.setrlimit(limit, (value, value))
            except (ValueError, OSError):
                pass

    return apply


def _run(argv, cwd, env, timeout, cpu_limit=None, shell=False):
    """跑起来，超时就整组杀掉；输出截断后再回去。"""
    kwargs = {
        "cwd": cwd,
        "env": env,
        "shell": shell,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if os.name == "posix":
        # Windows 开发机上没有进程组和 rlimit，这两样只在容器里生效
        kwargs["start_new_session"] = True
        kwargs["preexec_fn"] = _limits(cpu_limit)
    proc = subprocess.Popen(argv, **kwargs)
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, AttributeError):
            proc.kill()
        out, err = proc.communicate()
    return {
        "returncode": proc.returncode if not timed_out else -9,
        "stdout": (out or b"").decode("utf-8", errors="replace")[:MAX_STDOUT_CHARS],
        "stderr": (err or b"").decode("utf-8", errors="replace")[:MAX_STDERR_CHARS],
        "timed_out": timed_out,
    }


def _handle_shell(req):
    command = str(req.get("command") or "").strip()
    if not command:
        return {"ok": False, "error": "没有要执行的命令"}
    cwd = str(req.get("cwd") or "")
    if not _inside_work(cwd):
        return {"ok": False, "error": "只能在 %s 里执行" % _work_dir()}
    shell = "/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh"
    timeout = _timeout(req)
    result = _run(
        [shell, "-lc", command], os.path.realpath(cwd or _work_dir()),
        _shell_env(), timeout, timeout,
    )
    result["ok"] = True
    return result


def _handle_python(req):
    script = str(req.get("script") or "")
    if not script or not os.path.isfile(script):
        return {"ok": False, "error": "脚本不存在"}
    if not _inside_work(script):
        return {"ok": False, "error": "脚本只能在 %s 里" % _work_dir()}
    cwd = str(req.get("cwd") or os.path.dirname(script))
    if not _inside_work(cwd):
        return {"ok": False, "error": "只能在 %s 里执行" % _work_dir()}
    env = dict(_shell_env())
    for key, value in (req.get("env") or {}).items():
        if isinstance(key, str) and isinstance(value, str):
            env[key] = value
    timeout = _timeout(req)
    result = _run(
        [sys.executable, script], os.path.realpath(cwd), env, timeout,
        req.get("cpu_limit") or timeout,
    )
    result["ok"] = True
    return result


def _timeout(req):
    try:
        return max(1, min(int(req.get("timeout") or DEFAULT_TIMEOUT), MAX_TIMEOUT))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT


def handle_request(req):
    kind = str(req.get("kind") or "")
    if kind == "whoami":
        try:
            import pwd
            name = pwd.getpwuid(os.geteuid()).pw_name
        except (ImportError, KeyError, AttributeError):
            name = str(os.geteuid())
        return {"ok": True, "uid": os.geteuid(), "user": name}
    if kind == "shell":
        return _handle_shell(req)
    if kind == "python":
        return _handle_python(req)
    return {"ok": False, "error": "不认识的请求：%s" % (kind or "空")}


def handle_conn(conn):
    stream = conn.makefile("rb")
    try:
        line = stream.readline(MAX_REQUEST_BYTES)
        if not line:
            return
        try:
            req = json.loads(line.decode("utf-8", errors="replace"))
        except ValueError:
            reply = {"ok": False, "error": "请求不是 JSON"}
        else:
            if not isinstance(req, dict):
                reply = {"ok": False, "error": "请求不是对象"}
            else:
                try:
                    reply = handle_request(req)
                except Exception as exc:  # 执行通道不能因为一条命令就整个退出
                    reply = {"ok": False, "error": "执行失败：%s" % exc}
        conn.sendall((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
    finally:
        stream.close()


def serve(socket_path=None):
    path = socket_path or os.environ.get("SHELL_WORKER_SOCKET", DEFAULT_SOCKET)
    # 产物要让 App 读得到（打印、再传给下一轮），所以通道自己的 umask 用 022：
    # 入口脚本那边是 027（库文件 640），这里不能跟着走。
    os.umask(0o022)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    try:
        os.unlink(path)
    except OSError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    try:
        os.chmod(path, 0o660)
    except OSError:
        pass
    server.listen(16)
    print("shell worker：%s（uid=%d）" % (path, os.geteuid()), flush=True)
    while True:
        conn, _ = server.accept()
        try:
            handle_conn(conn)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # 一条坏请求不能拖垮通道
            print("shell worker 错误：%s" % exc, file=sys.stderr, flush=True)
        finally:
            conn.close()


if __name__ == "__main__":
    serve()
