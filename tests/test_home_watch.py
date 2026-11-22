from datetime import datetime, timedelta

from agent.home_watch import HomeWatcher, parse_event_ids
from domain.store import AgentStore


class FakeClient:
    def __init__(self, rows):
        self.rows = rows
        self.configured = True

    def states(self, max_age=3.0):
        return self.rows

    def state(self, entity_id):
        for row in self.rows:
            if row["entity_id"] == entity_id:
                return row
        return None


class FakeHome:
    """够 HomeWatcher 用的最小设备视图：白名单已经在 _devices 里做过。"""

    def __init__(self, rows):
        self.client = FakeClient(rows)
        self._rows = rows

    def _devices(self):
        return self._rows


FILTER_LOW = {
    "entity_id": "sensor.composite",
    "state": "0",
    "attributes": {"friendly_name": "复合滤芯", "unit_of_measurement": "%"},
}
FILTER_OK = {
    "entity_id": "sensor.ro",
    "state": "58",
    "attributes": {"friendly_name": "RO滤芯", "unit_of_measurement": "%"},
}
FAULT = {
    "entity_id": "sensor.fault",
    "state": "3",
    "attributes": {"friendly_name": "净水器 故障"},
}


def make(rows, **kwargs):
    store = AgentStore(":memory:")
    watcher = HomeWatcher(FakeHome(rows), store, **kwargs)
    return watcher, store


def test_low_filter_alerts():
    watcher, _ = make([FILTER_LOW, FILTER_OK], filter_percent=10)

    alerts = watcher.check()

    assert len(alerts) == 1
    assert alerts[0]["kind"] == "filter"
    assert "复合滤芯" in alerts[0]["text"]
    assert "0%" in alerts[0]["text"]


def test_filter_above_threshold_stays_quiet():
    watcher, _ = make([FILTER_OK], filter_percent=10)
    assert watcher.check() == []


def test_filter_does_not_repeat_within_cooldown():
    watcher, _ = make([FILTER_LOW], filter_percent=10, cooldown_hours=24)
    now = datetime(2026, 11, 22, 9, 0, 0)

    assert len(watcher.check(now=now)) == 1
    assert watcher.check(now=now + timedelta(hours=1)) == []
    assert len(watcher.check(now=now + timedelta(hours=25))) == 1


def test_fault_alert_only_when_code_changes():
    watcher, _ = make([FAULT])

    alerts = watcher.check()
    assert len(alerts) == 1 and "故障" in alerts[0]["text"]
    # 同一个错误码不反复喊
    assert watcher.check() == []


def test_zero_fault_code_is_not_an_alert():
    row = dict(FAULT, state="0")
    watcher, _ = make([row])
    assert watcher.check() == []


def test_first_event_is_recorded_not_announced():
    """第一次看到只记下来：不然一上电就把历史事件当新闻推一遍。"""
    event = {
        "entity_id": "event.motion",
        "state": "2026-11-22T12:04:16.754+00:00",
        "attributes": {"friendly_name": "识别到画面变动"},
    }
    watcher, store = make(
        [event], filter_percent=None, event_ids=("event.motion",),
    )

    assert watcher.check() == []
    assert store.get_home_alert("event", "event.motion")["last_value"].startswith("2026-11-22")


def test_new_event_after_first_is_announced():
    event = {
        "entity_id": "event.motion",
        "state": "2026-11-22T12:04:16.754+00:00",
        "attributes": {"friendly_name": "识别到画面变动"},
    }
    watcher, store = make(
        [event], filter_percent=None, event_ids=("event.motion",),
    )
    watcher.check()  # 首次记录

    store.touch_home_alert("event", "event.motion", "2026-11-22T12:00:00.000+00:00")
    alerts = watcher.check()

    assert len(alerts) == 1
    assert alerts[0]["kind"] == "event"
    assert "画面变动" in alerts[0]["text"]


def test_unknown_event_state_is_ignored():
    event = {
        "entity_id": "event.motion",
        "state": "unknown",
        "attributes": {"friendly_name": "识别到画面变动"},
    }
    watcher, _ = make(
        [event], filter_percent=None, event_ids=("event.motion",),
    )
    assert watcher.check() == []


def test_events_disabled_by_default():
    event = {
        "entity_id": "event.motion",
        "state": "2026-11-22T12:04:16.754+00:00",
        "attributes": {"friendly_name": "识别到画面变动"},
    }
    watcher, _ = make([event], filter_percent=None)
    assert watcher.check() == []


def test_dry_run_does_not_consume_cooldown():
    watcher, store = make([FILTER_LOW], filter_percent=10)

    assert len(watcher.check(apply=False)) == 1
    assert store.get_home_alert("filter", "sensor.composite") is None
    assert len(watcher.check()) == 1


def test_parse_event_ids():
    assert parse_event_ids("event.a, event.b ,") == ("event.a", "event.b")
    assert parse_event_ids("") == ()


def test_fresh_first_event_is_announced(monkeypatch):
    """门铃第一次响就该提醒，不能当成「历史事件」吞掉。"""
    from datetime import datetime as _dt

    now = _dt(2026, 11, 22, 21, 0, 0)
    event = {
        "entity_id": "event.doorbell",
        "state": "2026-11-22T20:59:50+08:00",   # 10 秒前
        "attributes": {"friendly_name": "门铃响了"},
    }
    watcher, _ = make(
        [event], filter_percent=None, event_ids=("event.doorbell",),
    )

    alerts = watcher.check(now=now)

    assert len(alerts) == 1
    assert "门铃响了" in alerts[0]["text"]