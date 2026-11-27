"""pytest 公共夹具。

测试不能依赖本机 .env：这台机器接了设备网关（HOME_TOKEN 非空、HOME_DEVICES 有值）时，
Assistant() 会自动注册设备工具、查询也走白名单，断言就会随环境变化。统一在这里关掉。
"""
import pytest


@pytest.fixture(autouse=True)
def _no_home_gateway(monkeypatch):
    monkeypatch.setattr("adapters.home.HOME_TOKEN", "")
    monkeypatch.setattr("adapters.home.HOME_DEVICES", "")
    monkeypatch.setattr("adapters.home.HOME_CONTROL_ALLOW", "")
    monkeypatch.setattr("agent.home_watch.HOME_WATCH_EVENTS", "")
    monkeypatch.setattr("agent.home_watch.HOME_FILTER_ALERT_PERCENT", 10.0)
    # MCP 接入点是能写库的入口，测试里必须默认关掉
    monkeypatch.setattr("server.MCP_TOKEN", "")
    # 思考记录只在真实运行时写，测试不落盘
    monkeypatch.setattr("agent.thinking.ENABLED", False)
    # 对话记录（D84）同理：默认不建库、不记；要测记录行为的用例自己传
    # DialogueStore(":memory:", enabled=True)
    monkeypatch.setattr("domain.dialogue.DIALOGUE_ENABLED", 0)
