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


def patch_intent_exit():
    """退出命令按「句尾」匹配。

    上游用的是 `if text == cmd`，但人说话几乎总带前缀（「好的再见」「不用再见」），
    去掉标点后整句相等反而永远匹配不上，会话会一直挂着等无语音超时。
    """
    _replace_once(
        "core/handle/intentHandler.py",
        "    for cmd in cmd_exit:\n        if text == cmd:\n",
        "    for cmd in cmd_exit:\n"
        "        # 小马：整句相等或句尾命中都算，避免「好的再见」这种说法漏掉\n"
        "        if text == cmd or text.endswith(cmd):\n",
        "退出命令句尾匹配",
    )


def patch_weather_tool_desc():
    """去掉「本地天气已在上下文中，绝对不要调用」这句。

    上游靠 prompt_manager 预取天气塞进 system prompt（模板要写 {{weather_info}}），
    我们用的是自定义 prompt，没有这个预取；留着这句模型既不调工具、也没有数据，
    只会回「我查不了天气」。
    """
    _replace_once(
        "plugins_func/functions/get_weather.py",
        "重要：本地未来7天天气已在上下文中提供，用户未指明其他城市时绝对不要调用此工具。",
        "用户问天气时调用此工具；未指明城市时查本地（家庭所在城市）。",
        "天气工具描述去掉禁调提示",
    )


if __name__ == "__main__":
    patch_websocket_server()
    patch_app()
    patch_intent_exit()
    patch_weather_tool_desc()
    print("小智提醒桥补丁完成")
    sys.exit(0)
