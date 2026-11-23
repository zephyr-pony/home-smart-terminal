"""在构建镜像时给小智服务端打两个钩子。

为什么不在仓库里存改过的整份 `websocket_server.py` / `app.py`：那两个文件随上游
频繁变动，整份拷贝等于把上游历史也一起冻结了。这里只做字符串级替换，锚点找不到
就**构建失败**——升级后要么两分钟修好，要么立刻发现，不会静默少一块功能。
"""
import io
import os
import sys

# 在镜像里以 /opt/xiaozhi-esp32-server 为工作目录运行（见 Dockerfile）
ROOT = os.getcwd()
if not os.path.exists(os.path.join(ROOT, "app.py")):
    raise SystemExit("请在 xiaozhi-server 根目录运行：当前 %s 下没有 app.py" % ROOT)


def _replace_once(path, old, new, label):
    full = os.path.join(ROOT, path)
    with io.open(full, encoding="utf-8") as fh:
        text = fh.read()
    if new in text:
        print("已打过补丁，跳过：%s" % label)
        return
    count = text.count(old)
    if count != 1:
        raise SystemExit(
            "补丁锚点在 %s 出现 %d 次（期望 1）：%s\n"
            "上游改了这个位置，请更新 infra/xiaozhi-server/apply_patch.py"
            % (path, count, label)
        )
    with io.open(full, "w", encoding="utf-8", newline="") as fh:
        fh.write(text.replace(old, new, 1))
    print("已打补丁：%s" % label)


def patch_websocket_server():
    _replace_once(
        "core/websocket_server.py",
        "from core.connection import ConnectionHandler\n",
        "from core.connection import ConnectionHandler\n"
        "from core.reminder_bridge import registry as reminder_registry\n",
        "导入注册表",
    )
    _replace_once(
        "core/websocket_server.py",
        "        try:\n            await handler.handle_connection(websocket)\n",
        "        reminder_registry.add(handler)\n"
        "        try:\n            await handler.handle_connection(websocket)\n",
        "建连时注册设备",
    )
    _replace_once(
        "core/websocket_server.py",
        "        finally:\n            # 强制关闭连接（如果还没有关闭的话）\n",
        "        finally:\n"
        "            reminder_registry.discard(handler)\n"
        "            # 强制关闭连接（如果还没有关闭的话）\n",
        "断连时移除设备",
    )


def patch_app():
    _replace_once(
        "app.py",
        "from core.websocket_server import WebSocketServer\n",
        "from core.websocket_server import WebSocketServer\n"
        "from core.reminder_bridge import start_from_env as start_reminder_bridge\n",
        "导入提醒桥",
    )
    _replace_once(
        "app.py",
        "    ws_task = asyncio.create_task(ws_server.start())\n",
        "    ws_task = asyncio.create_task(ws_server.start())\n"
        "    # 小马提醒桥：环境变量没配就什么都不做\n"
        "    start_reminder_bridge()\n",
        "启动提醒桥",
    )


if __name__ == "__main__":
    patch_websocket_server()
    patch_app()
    print("小智提醒桥补丁完成")
    sys.exit(0)
