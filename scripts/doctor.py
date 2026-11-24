"""启动前自检：配置齐不齐、目录能不能写、端口被谁占着。

为什么单独成脚本：这些错误现在都要等 `python server.py` 走到一半才暴露，
有的是十秒后、有的是下载完 900MB 模型之后。doctor 不加载模型、不发网络请求，
几秒钟把能在启动前发现的问题一次说完。

    python scripts/doctor.py
    python scripts/doctor.py --json

退出码：0 全部通过（可以有提醒）；1 有错误。
"""
import argparse
import importlib.util
import json
import os
import socket
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

OK, WARN, ERROR = "ok", "warn", "error"
_LOOPBACK = ("127.0.0.1", "localhost", "::1", "[::1]")


class Report(object):
    def __init__(self):
        self.items = []

    def add(self, level, title, detail=""):
        self.items.append({"level": level, "title": title, "detail": detail})

    def ok(self, title, detail=""):
        self.add(OK, title, detail)

    def warn(self, title, detail=""):
        self.add(WARN, title, detail)

    def error(self, title, detail=""):
        self.add(ERROR, title, detail)

    def count(self, level):
        return sum(1 for item in self.items if item["level"] == level)

    @property
    def failed(self):
        return self.count(ERROR) > 0


def check_llm(report, base_url, api_key, model):
    """LLM 是唯一必填项；缺一个就起不来。"""
    missing = [
        name for name, value in (
            ("LLM_BASE_URL", base_url),
            ("LLM_API_KEY", api_key),
            ("LLM_MODEL", model),
        ) if not str(value or "").strip()
    ]
    if missing:
        report.error("LLM 配置不完整", "缺少 " + " / ".join(missing))
    else:
        report.ok("LLM 配置", "%s · %s" % (base_url, model))


def check_exposure(report, host, token):
    """监听非本机又没 Token 时，server 会拒绝启动；这里提前说清楚。"""
    if str(token or "").strip():
        report.ok("接口鉴权", "API_TOKEN 已设置")
        return
    if str(host or "").strip().lower() in _LOOPBACK:
        report.ok("接口鉴权", "只监听本机，未设 Token 也可以")
        return
    report.error(
        "接口会暴露",
        "SERVER_HOST=%s 且 API_TOKEN 为空；改成 127.0.0.1 或设置 Token" % host,
    )


def check_data_dir(report, data_dir, db_paths=()):
    """目录建不出来或写不进去，启动后每写一条都会报错。"""
    try:
        os.makedirs(data_dir, exist_ok=True)
        probe = os.path.join(data_dir, ".doctor-write-test")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        report.ok("数据目录", data_dir)
    except OSError as exc:
        report.error("数据目录不可写", "%s（%s）" % (data_dir, exc))

    for path in db_paths:
        parent = os.path.dirname(os.path.abspath(path)) or "."
        if not os.path.isdir(parent):
            report.warn("数据库目录不存在", "%s → %s" % (path, parent))
        elif not os.access(parent, os.W_OK):
            report.error("数据库目录不可写", parent)


def check_port(report, host, port):
    """端口被占多半是服务已经在跑；连不上才需要继续看。"""
    probe = host if str(host or "").strip().lower() not in _LOOPBACK else "127.0.0.1"
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        in_use = sock.connect_ex((probe, int(port))) == 0
    except (OSError, ValueError):
        in_use = False
    finally:
        sock.close()
    if in_use:
        report.warn("端口已被占用", "%s:%s 上已经有服务在监听（可能正是小马）" % (probe, port))
    else:
        report.ok("端口可用", "%s:%s" % (probe, port))


def _has_module(name):
    """只查模块在不在，不 import：torch/funasr 一 import 就是几秒钟。"""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def check_optional(report, push_enabled, vapid_private, home_token, home_devices,
                   mcp_token, stt_enabled):
    """可选模块只看“开关打开了但没配完”这种自相矛盾。"""
    if push_enabled and not str(vapid_private or "").strip():
        report.error("Web Push 开着但没密钥", "设 VAPID_PRIVATE_KEY 或把 PUSH_ENABLED 置 0")
    elif push_enabled:
        report.ok("Web Push", "已启用")
    else:
        report.ok("Web Push", "未启用（需要时再开）")

    if home_devices and not str(home_token or "").strip():
        report.error("设备白名单填了但没有 Token", "HOME_TOKEN 为空时不会注册设备工具")
    elif str(home_token or "").strip():
        report.ok("Home Assistant", "已配置")
    else:
        report.ok("Home Assistant", "未启用")

    report.ok("MCP 接入点", "已启用" if str(mcp_token or "").strip() else "未启用")

    # Docker 镜像默认不带语音识别；.env 里留着 STT_ENABLED=1 就会在启动时崩，
    # 与其等容器反复重启，不如在这里说清楚是缺依赖。
    if not stt_enabled:
        report.ok("语音识别", "已关闭（仅文字）")
    elif _has_module("funasr"):
        report.ok("语音识别", "已启用")
    else:
        report.error(
            "语音识别开着但没装 funasr",
            "pip install -r requirements-stt.txt，或把 STT_ENABLED 设成 0",
        )


def check_backup_scope(report, data_dir, root=ROOT):
    """备份脚本扫的是仓库下的 data/；DATA_DIR 指到别处时提醒一句。"""
    default = os.path.join(root, "data")
    try:
        same = os.path.abspath(data_dir) == os.path.abspath(default)
    except OSError:
        same = False
    if not same:
        report.warn(
            "备份范围",
            "DATA_DIR=%s 不在仓库 data/ 下，scripts/backup.py 不会自动带上它" % data_dir,
        )
    else:
        report.ok("备份范围", "data/ 会被 scripts/backup.py 备份")


def format_text(report):
    icons = {OK: "✅", WARN: "⚠️ ", ERROR: "❌"}
    lines = [icons[item["level"]] + " " + item["title"] +
             (("：" + item["detail"]) if item["detail"] else "")
             for item in report.items]
    lines.append("")
    if report.failed:
        lines.append("结果：%d 个错误、%d 个提醒，先修错误再启动。"
                     % (report.count(ERROR), report.count(WARN)))
    else:
        lines.append("结果：通过（%d 个提醒）" % report.count(WARN))
    return "\n".join(lines)


def run(config=None):
    if config is None:
        import config as config_module
        config = config_module
    report = Report()

    if sys.version_info < (3, 9):
        report.error("Python 版本过低", "需要 3.9+，当前 %s" % sys.version.split()[0])
    else:
        report.ok("Python", sys.version.split()[0])

    env_path = os.path.join(ROOT, ".env")
    if os.path.exists(env_path):
        report.ok(".env", env_path)
    else:
        report.warn(".env", "不存在；只靠环境变量也可以，但重启/迁移容易丢")

    check_llm(report, config.LLM_BASE_URL, config.LLM_API_KEY, config.LLM_MODEL)
    check_exposure(report, config.SERVER_HOST, config.API_TOKEN)
    check_data_dir(
        report,
        config.DATA_DIR,
        [
            config.MEMORY_DB_PATH,
            config.ITEMS_DB_PATH,
            config.AGENT_DB_PATH,
            config.TASKS_DB_PATH,
            config.FACTS_DB_PATH,
            config.PUSH_DB_PATH,
        ],
    )
    check_port(report, config.SERVER_HOST, config.SERVER_PORT)
    check_optional(
        report,
        push_enabled=bool(getattr(config, "PUSH_ENABLED", False)),
        vapid_private=getattr(config, "VAPID_PRIVATE_KEY", ""),
        home_token=getattr(config, "HOME_TOKEN", ""),
        home_devices=getattr(config, "HOME_DEVICES", ""),
        mcp_token=getattr(config, "MCP_TOKEN", ""),
        stt_enabled=bool(getattr(config, "STT_ENABLED", True)),
    )
    check_backup_scope(report, config.DATA_DIR)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="小马启动前自检")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    report = run()
    if args.json:
        print(json.dumps(
            {"ok": not report.failed, "items": report.items},
            ensure_ascii=False, indent=2,
        ))
    else:
        print(format_text(report))
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
