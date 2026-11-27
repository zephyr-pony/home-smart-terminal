from datetime import datetime, timedelta

from agent.home_watch import (
    HomeWatcher, describe_lock_event, describe_lock_exception, parse_event_ids,
    parse_lock_users,
)
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


def test_parse_lock_users():
    assert parse_lock_users("1=爸爸, 2=妈妈") == {1: "爸爸", 2: "妈妈"}
    assert parse_lock_users("3=奶奶，4=爷爷") == {3: "奶奶", 4: "爷爷"}
    assert parse_lock_users("") == {}
    assert parse_lock_users("爸爸") == {}


def test_describe_lock_event_fingerprint_unlock():
    attrs = {"锁动作": 2, "操作方式": 2, "操作位置": 2, "操作ID": 2}

    assert describe_lock_event(attrs, {2: "妈妈"}) == "妈妈用指纹开了门"


def test_describe_lock_event_unknown_user_keeps_the_id():
    attrs = {"锁动作": 2, "操作方式": 2, "操作位置": 2, "操作ID": 3}

    assert describe_lock_event(attrs) == "3 号用户用指纹开了门"


def test_describe_lock_event_face_unlock():
    """实测：3D 面容上报 操作方式=5、操作ID=2004（四位数）。"""
    attrs = {"锁动作": 2, "操作方式": 5, "操作位置": 2, "操作ID": 2004}

    assert describe_lock_event(attrs, {2004: "妈妈"}) == "妈妈刷脸开了门"
    assert describe_lock_event(attrs) == "2004 号用户刷脸开了门"


def test_describe_lock_event_indoor_unlock():
    """门内开锁：实测是 操作位置=1、操作方式=16，得说成「从里面开了门」。"""
    attrs = {"锁动作": 2, "操作方式": 16, "操作位置": 1, "操作ID": 0}

    assert describe_lock_event(attrs) == "门从里面打开了"


def test_describe_lock_event_auto_lock_is_silent():
    """每次关门都跟着一条自动上锁，念出来纯属噪音：看懂了但不播报。"""
    attrs = {"锁动作": 1, "操作方式": 16, "操作位置": 3, "操作ID": 0}

    assert describe_lock_event(attrs) == ""


def test_describe_lock_event_manual_without_position_does_not_guess():
    attrs = {"锁动作": 2, "操作方式": 15, "操作位置": 3, "操作ID": 0}

    assert describe_lock_event(attrs) == "门被手动打开了"


def test_describe_lock_event_duress_is_called_out():
    attrs = {"锁动作": 2, "操作方式": 12, "操作位置": 2, "操作ID": 1}

    text = describe_lock_event(attrs, {1: "爸爸"})

    assert text == "爸爸用胁迫方式开了门，注意安全"


def test_describe_lock_event_other_actions():
    assert describe_lock_event(
        {"锁动作": 4, "操作方式": 15, "操作位置": 1, "操作ID": 0}
    ) == "开启了反锁"
    assert describe_lock_event({"锁动作": 8}) == "开启了离家模式"


def test_describe_lock_event_without_lock_attrs_returns_none():
    assert describe_lock_event({"friendly_name": "门铃响了"}) is None


def test_doorbell_uses_event_type_not_the_long_entity_name():
    """「亚拉锁 可视门铃 门铃响了」念出来像报型号；事件本身是「门铃响了」。"""
    now = datetime(2026, 11, 27, 12, 0, 0)
    stamp = (now - timedelta(seconds=10)).astimezone().isoformat()
    event = {
        "entity_id": "event.doorbell",
        "state": stamp,
        "attributes": {
            "friendly_name": "亚拉锁 可视门铃 门铃响了",
            "event_type": "门铃响了",
        },
    }
    watcher, _ = make(
        [event], filter_percent=None, event_ids=("event.doorbell",),
    )

    alerts = watcher.check(now=now)

    assert alerts[0]["text"] == "门铃响了"


def test_english_event_type_falls_back_to_entity_name():
    """英文枚举（completed/failed）不能直接念。"""
    now = datetime(2026, 11, 27, 12, 0, 0)
    stamp = (now - timedelta(seconds=10)).astimezone().isoformat()
    event = {
        "entity_id": "event.backup",
        "state": stamp,
        "attributes": {"friendly_name": "备份 失败了", "event_type": "failed"},
    }
    watcher, _ = make(
        [event], filter_percent=None, event_ids=("event.backup",),
    )

    alerts = watcher.check(now=now)

    assert alerts[0]["text"] == "备份 失败了"


def test_describe_lock_exception_maps_codes():
    assert describe_lock_exception({"异常情况": 9}) == "门锁被撬了，注意安全"
    assert describe_lock_exception({"异常情况": 25}) == "有人在撬门，注意安全"
    assert describe_lock_exception({"异常情况": 20}) == "门锁电量快没了"
    # 23/24 由门状态传感器负责，异常事件这边静默，避免同一件事报两遍
    assert describe_lock_exception({"异常情况": 23}) == ""
    assert describe_lock_exception({"异常情况": 24}) == ""
    assert describe_lock_exception({"异常情况": 99}) is None
    assert describe_lock_exception({}) is None


def test_exception_event_is_announced():
    now = datetime(2026, 11, 27, 12, 0, 0)
    stamp = (now - timedelta(seconds=10)).astimezone().isoformat()
    event = {
        "entity_id": "event.lock_exception",
        "state": stamp,
        "attributes": {
            "friendly_name": "亚拉锁 门锁 发生异常",
            "event_type": "发生异常",
            "异常情况": 9,
        },
    }
    watcher, _ = make(
        [event], filter_percent=None, event_ids=("event.lock_exception",),
    )

    alerts = watcher.check(now=now)

    assert len(alerts) == 1
    assert alerts[0]["text"] == "门锁被撬了，注意安全"


DOOR_STATE = {
    "entity_id": "sensor.door_state",
    "state": "已上锁",
    "attributes": {
        "friendly_name": "门锁状态",
        "options": ["已上锁", "已开锁", "门未关（门超时未关）", "门虚掩"],
    },
}


def test_door_state_ajar_alerts_on_change_only():
    door = dict(DOOR_STATE, state="门虚掩")
    watcher, _ = make([door], filter_percent=None)

    first = watcher.check()
    assert [alert["text"] for alert in first] == ["门虚掩着，可能没关好"]
    # 状态没变就不重复念
    assert watcher.check() == []

    # 关好之后再虚掩：第二次也要报，不能因为「上次也虚掩过」被吞掉
    door["state"] = "已上锁"
    assert watcher.check() == []
    door["state"] = "门未关（门超时未关）"
    assert [alert["text"] for alert in watcher.check()] == ["门没关，去看一下"]


def test_door_state_locked_is_quiet():
    watcher, _ = make([dict(DOOR_STATE, state="已上锁")], filter_percent=None)

    assert watcher.check() == []


def test_door_state_unknown_does_not_alert():
    watcher, _ = make([dict(DOOR_STATE, state="unknown")], filter_percent=None)

    assert watcher.check() == []


def test_lock_event_alert_uses_the_decoded_sentence():
    """回归：门锁事件以前只念实体名「亚拉锁 门锁 锁事件」。"""
    now = datetime(2026, 11, 27, 12, 0, 0)
    stamp = (now - timedelta(seconds=10)).astimezone().isoformat()
    event = {
        "entity_id": "event.lock",
        "state": stamp,
        "attributes": {
            "friendly_name": "亚拉锁 门锁 锁事件",
            "锁动作": 2, "操作方式": 2, "操作位置": 2, "操作ID": 2,
        },
    }
    watcher, _ = make(
        [event], filter_percent=None, event_ids=("event.lock",),
        lock_users={2: "妈妈"},
    )

    alerts = watcher.check(now=now)

    assert len(alerts) == 1
    assert alerts[0]["text"] == "妈妈用指纹开了门"


def test_auto_lock_is_silent_but_recorded():
    now = datetime(2026, 11, 27, 12, 0, 0)
    stamp = (now - timedelta(seconds=5)).astimezone().isoformat()
    watcher, store = make(
        [], filter_percent=None, event_ids=("event.lock",),
    )

    alert = watcher.annotate_event(
        "event.lock", stamp, "亚拉锁 门锁 锁事件",
        {"锁动作": 1, "操作方式": 16, "操作位置": 3, "操作ID": 0},
        now=now,
    )

    assert alert is None
    assert store.get_home_alert("event", "event.lock")["last_value"] == stamp


def test_second_person_within_event_cooldown_is_still_announced():
    """门锁不走 10 分钟冷却：先回来的那个人不能把后一个盖掉。"""
    now = datetime(2026, 11, 27, 12, 0, 0)
    watcher, _ = make(
        [], filter_percent=None, event_ids=("event.lock",),
        lock_users={1: "爸爸", 2: "妈妈"}, event_cooldown_seconds=600,
    )

    first = watcher.annotate_event(
        "event.lock", (now - timedelta(seconds=40)).astimezone().isoformat(),
        "亚拉锁 门锁 锁事件",
        {"锁动作": 2, "操作方式": 2, "操作位置": 2, "操作ID": 1},
        now=now,
    )
    second = watcher.annotate_event(
        "event.lock", (now - timedelta(seconds=10)).astimezone().isoformat(),
        "亚拉锁 门锁 锁事件",
        {"锁动作": 2, "操作方式": 3, "操作位置": 2, "操作ID": 2},
        now=now,
    )

    assert first["text"] == "爸爸用指纹开了门"
    assert second["text"] == "妈妈用密码开了门"


def test_fresh_first_event_is_announced(monkeypatch):
    """门铃第一次响就该提醒，不能当成「历史事件」吞掉。"""
    from datetime import datetime as _dt, timedelta

    now = _dt(2026, 11, 22, 21, 0, 0)
    # HA 发的是带时区的时间；按本机时区倒推 10 秒，测试才不依赖 CI 跑在哪个时区。
    # 写死 +08:00 的话，UTC 的 runner 会把它当成 8 小时前的旧事件。
    stamp = (now - timedelta(seconds=10)).astimezone().isoformat()
    event = {
        "entity_id": "event.doorbell",
        "state": stamp,   # 10 秒前
        "attributes": {"friendly_name": "门铃响了"},
    }
    watcher, _ = make(
        [event], filter_percent=None, event_ids=("event.doorbell",),
    )

    alerts = watcher.check(now=now)

    assert len(alerts) == 1
    assert "门铃响了" in alerts[0]["text"]