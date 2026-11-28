"""run_shell 的危险命令：执行前先问一句（D86）。

为什么不能只靠「模型自己判断」：命令是它写的，它也可能只是照着一段被注入的
网页抄。删文件、关机、把下载的东西直接跑起来，这几类动作做错了收不回来，
所以命中就在执行前停下来复述给用户——和删除记忆、打印出纸走同一个确认入口。

判断用命令文本的静态匹配，只覆盖「一眼能看出来」的破坏性动作。它不是沙箱，
也不假装是：真正的边界在 uid 上（run_shell / run_python 以 work 用户跑，
读不到也写不到正式库），见 adapters/shell_worker.py。
"""
import re

# 每一条都写清楚「为什么停下来」，确认句直接念给用户听
_RULES = (
    # rm -rf / rm -r -f / rm -fr 都算；不带 -r/-f 的 rm 只是删一个文件，不至于
    (re.compile(r"\brm\s+(-[^\s]+\s+)*-[a-z]*[rf]", re.I), "会删文件"),
    (re.compile(r"\bmkfs|\bdd\b[^|;]*\bof=/dev/|\bshred\b", re.I), "会写坏磁盘"),
    (re.compile(r"\b(shutdown|reboot|poweroff|halt)\b", re.I), "会让这台机器关机或重启"),
    (re.compile(r"\bsystemctl\b|\bservice\s+\S+\s+(stop|restart)", re.I),
     "会动系统服务"),
    (re.compile(r"\b(curl|wget)\b[^|;]*\|\s*(sudo\s+)?(ba)?sh\b", re.I),
     "会把网上下载的东西直接跑起来"),
    (re.compile(r"\bsudo\b|\bsu\s+-", re.I), "要提权"),
    (re.compile(r"\b(chmod|chown)\s+-R\b", re.I), "会改整棵目录的权限"),
    (re.compile(r"\b(kill|pkill|killall)\b", re.I), "会杀进程"),
    (re.compile(r"\bsqlite3\b[^|;]*\.db\b|\bDROP\s+TABLE\b|\bDELETE\s+FROM\b"
                r"|\bUPDATE\s+\w+\s+SET\b|\bINSERT\s+INTO\b", re.I),
     "会直接改数据库"),
    (re.compile(r"\bdocker\b", re.I), "会动容器"),
    (re.compile(r">\s*(/etc/|/usr/|/bin/|/data/[^/\s]+\.db)", re.I),
     "会覆盖系统或数据文件"),
)


def risk(command):
    """命中就返回一句原因（中文，给确认句用），没命中返回空串。"""
    text = " ".join(str(command or "").split())
    if not text:
        return ""
    for pattern, reason in _RULES:
        if pattern.search(text):
            return reason
    return ""
