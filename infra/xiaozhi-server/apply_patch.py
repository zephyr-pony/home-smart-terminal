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


def patch_idle_session():
    """安静一会儿先让设备回待机，而不是一直开着麦克风。

    上游只有「无语音超时就挂断」这一条路；我们要的是常连：设备进待机后
    麦克风关掉、WebSocket 留着，提醒随时能推下来（D78）。待机由固件处理，
    这里只负责在真的安静下来时发一句 `{"type":"session","state":"idle"}`。
    """
    _replace_once(
        "core/handle/receiveAudioHandle.py",
        "    if have_voice:\n"
        "        conn.last_activity_time = time.time() * 1000\n"
        "        return\n",
        "    if have_voice:\n"
        "        conn.last_activity_time = time.time() * 1000\n"
        "        conn.idle_sent = False\n"
        "        return\n",
        "有声音时清待机标记",
    )
    _replace_once(
        "core/handle/receiveAudioHandle.py",
        "        no_voice_time = time.time() * 1000 - conn.last_activity_time\n",
        "        no_voice_time = time.time() * 1000 - conn.last_activity_time\n"
        "        # 小马：安静一会儿先让设备回待机（连接不关），别一直开着麦克风\n"
        "        idle_after = int(conn.config.get(\"idle_after_voice_time\", 15) or 0)\n"
        "        if (\n"
        "            idle_after > 0\n"
        "            and no_voice_time > 1000 * idle_after\n"
        "            and not getattr(conn, \"idle_sent\", False)\n"
        "        ):\n"
        "            conn.idle_sent = True\n"
        "            try:\n"
        "                await conn.websocket.send(\n"
        "                    json.dumps({\"type\": \"session\", \"state\": \"idle\"})\n"
        "                )\n"
        "            except Exception:\n"
        "                pass\n",
        "无语音时下发待机指令",
    )


if __name__ == "__main__":
    patch_websocket_server()
    patch_app()
    # 退出命令不再打补丁：exit_commands 留空，结束会话交给模型的 handle_exit_intent
    # （2026-11-27）。句尾匹配那版会把「不用再见」也当成退出。
    patch_weather_tool_desc()
    patch_idle_session()
    print("小智提醒桥补丁完成")
    sys.exit(0)
