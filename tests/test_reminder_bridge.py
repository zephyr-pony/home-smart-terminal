"""提醒桥的排队与选设备逻辑（不依赖 xiaozhi 运行时）。"""
import asyncio
import importlib.util
from pathlib import Path

BRIDGE_PATH = Path(__file__).resolve().parents[1] / "infra" / "xiaozhi-server" / "reminder_bridge.py"


def _load():
    spec = importlib.util.spec_from_file_location("xiaozhi_reminder_bridge", BRIDGE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bridge_module = _load()


class FakeConn:
    def __init__(self, speaking=False, websocket=True, tts=True):
        self.websocket = object() if websocket else None
        self.tts = object() if tts else None
        self.client_is_speaking = speaking
        self.said = []


def make_bridge(conns=(), clock=None):
    devices = bridge_module.DeviceRegistry()
    for conn in conns:
        devices.add(conn)
    said = []

    def speaker(conn, text):
        conn.said.append(text)
        said.append(text)

    bridge = bridge_module.ReminderBridge(
        "http://127.0.0.1:8200", "token",
        speaker=speaker, devices=devices,
        now=(lambda: clock[0]) if clock else None,
    )
    return bridge, devices, said


def test_registry_tracks_only_usable_devices():
    conn = FakeConn()
    speaking = FakeConn(speaking=True)
    dead = FakeConn(websocket=False)
    no_tts = FakeConn(tts=False)

    _, devices, _ = make_bridge([conn, speaking, dead, no_tts])

    assert len(devices) == 4
    assert list(devices.idle()) == [conn]

    devices.discard(conn)
    assert len(devices) == 3
    assert list(devices.idle()) == []


def test_delivers_to_idle_device_then_waits_for_it():
    """一趟只发一条给同一台设备；不等它说完不会塞第二条。"""
    conn = FakeConn()
    bridge, _, said = make_bridge([conn])

    bridge.enqueue("提醒：牛奶今天到期")
    bridge.enqueue("提醒：电费明天到期")

    assert asyncio.run(bridge.deliver_pending()) == 1
    assert said == ["提醒：牛奶今天到期"]
    assert bridge.pending_count() == 1

    # 冷却期内再跑一次：还是发不出去
    assert asyncio.run(bridge.deliver_pending()) == 0


def test_second_device_picks_up_the_queue():
    first = FakeConn()
    second = FakeConn()
    bridge, _, said = make_bridge([first, second])

    bridge.enqueue("第一条")
    bridge.enqueue("第二条")

    assert asyncio.run(bridge.deliver_pending()) == 2
    assert said == ["第一条", "第二条"]


def test_expired_reminders_are_dropped():
    clock = [1000.0]
    conn = FakeConn()
    bridge, _, said = make_bridge([conn], clock=clock)

    bridge.enqueue("提醒：早就过期了")
    clock[0] += bridge_module.QUEUE_TTL_SECONDS + 1

    assert asyncio.run(bridge.deliver_pending()) == 0
    assert said == []
    assert bridge.pending_count() == 0


def test_queue_is_capped():
    bridge, _, _ = make_bridge([FakeConn()])

    for index in range(bridge_module.MAX_QUEUE + 5):
        bridge.enqueue("第 %d 条" % index)

    assert bridge.pending_count() == bridge_module.MAX_QUEUE


def test_speaker_failure_does_not_block_the_queue():
    conn = FakeConn()

    def boom(_conn, _text):
        raise RuntimeError("TTS 挂了")

    devices = bridge_module.DeviceRegistry()
    devices.add(conn)
    bridge = bridge_module.ReminderBridge(
        "http://x", "t", speaker=boom, devices=devices,
    )
    bridge.enqueue("提醒：第一条")
    bridge.enqueue("提醒：第二条")

    assert asyncio.run(bridge.deliver_pending()) == 0
    # 两条都失败并丢弃，不会卡在队里反复重试
    assert bridge.pending_count() == 0


def test_start_from_env_is_noop_when_unconfigured(monkeypatch):
    monkeypatch.delenv("XIAOMA_BASE_URL", raising=False)
    monkeypatch.delenv("XIAOMA_API_TOKEN", raising=False)

    assert bridge_module.start_from_env() is None


def test_async_speaker_is_awaited():
    """真机口播要先 await 发 tts start，异步的 speaker 不能被当成同步丢掉。"""
    devices = bridge_module.DeviceRegistry()
    devices.add(FakeConn())
    said = []

    async def speaker(_conn, text):
        await asyncio.sleep(0)
        said.append(text)

    bridge = bridge_module.ReminderBridge("http://x", "t", speaker=speaker, devices=devices)
    bridge.enqueue("提醒：牛奶今天到期")

    assert asyncio.run(bridge.deliver_pending()) == 1
    assert said == ["提醒：牛奶今天到期"]


def test_watchdog_closes_sse_when_devices_go_away(monkeypatch):
    """设备全断要主动退订，否则小马会把提醒投进没人收的通道（D76）。"""
    monkeypatch.setattr(bridge_module, "DEVICE_CHECK_SECONDS", 0.01)
    devices = bridge_module.DeviceRegistry()
    conn = FakeConn()
    devices.add(conn)
    bridge = bridge_module.ReminderBridge(
        "http://x", "t", speaker=lambda c, t: None, devices=devices,
    )

    class FakeResponse:
        closed = False

        def close(self):
            self.closed = True

    response = FakeResponse()

    async def scenario():
        task = asyncio.create_task(bridge._close_when_no_devices(response))
        await asyncio.sleep(0.05)
        assert response.closed is False
        devices.discard(conn)
        await asyncio.sleep(0.05)
        assert response.closed is True
        assert bridge._idle_closed is True
        task.cancel()

    asyncio.run(scenario())
