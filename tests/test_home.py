from adapters.home import (
    HomeDevices,
    HomeUnavailable,
    build_home,
    control_allowed,
    parse_control_allow,
    parse_devices,
)

LIGHT = {
    "entity_id": "light.keting_deng",
    "state": "on",
    "attributes": {"friendly_name": "客厅灯", "brightness": 153},
}
CLIMATE = {
    "entity_id": "climate.wo_shi_kong_diao",
    "state": "cool",
    "attributes": {
        "friendly_name": "卧室空调",
        "temperature": 26,
        "current_temperature": 28.5,
    },
}
FILTER = {
    "entity_id": "sensor.jingshuiqi_filter",
    "state": "12",
    "attributes": {"friendly_name": "净水器滤芯", "unit_of_measurement": "%"},
}
TRACKER = {
    "entity_id": "device_tracker.phone",
    "state": "home",
    "attributes": {"friendly_name": "手机"},
}
OFFLINE = {
    "entity_id": "light.yangtai_deng",
    "state": "unavailable",
    "attributes": {"friendly_name": "阳台灯"},
}


class FakeClient:
    def __init__(self, rows=None, configured=True, error=None, applies=True):
        self.rows = rows if rows is not None else []
        self.configured = configured
        self.error = error
        # applies=False 模拟「指令发出去了设备没动」
        self.applies = applies
        self.service_calls = []

    def states(self, max_age=3.0):
        if self.error:
            raise self.error
        return self.rows

    def state(self, entity_id):
        for row in self.rows:
            if row["entity_id"] == entity_id:
                return row
        return None

    def call_service(self, domain, service, entity_id=None, data=None):
        data = dict(data or {})
        self.service_calls.append((domain, service, entity_id, data))
        if not self.applies:
            return True
        row = self.state(entity_id)
        if row is None:
            return True
        attrs = row.setdefault("attributes", {})
        if service == "turn_on":
            row["state"] = "on"
            if "brightness_pct" in data:
                attrs["brightness"] = round(data["brightness_pct"] / 100 * 255)
        elif service == "turn_off":
            row["state"] = "off"
        elif service == "set_temperature":
            attrs["temperature"] = data.get("temperature")
        elif service == "open_cover":
            row["state"] = "open"
        elif service == "close_cover":
            row["state"] = "closed"
        elif service == "start":
            row["state"] = "cleaning"
        elif service == "return_to_base":
            row["state"] = "docked"
        return True


def _devices(rows=None, **kwargs):
    return HomeDevices(client=FakeClient(rows, **kwargs))


def test_query_matches_chinese_name_and_describes_state():
    home = _devices([LIGHT, CLIMATE, FILTER, TRACKER])

    out = home.query("客厅灯")

    assert "客厅灯" in out
    assert "light.keting_deng" in out
    assert "开" in out
    assert "60%" in out
    # 空调没被匹配进来
    assert "卧室空调" not in out


def test_query_reports_temperature_and_filter_percentage():
    home = _devices([LIGHT, CLIMATE, FILTER])

    climate = home.query("空调")
    assert "设定 26" in climate
    assert "室温 28.5" in climate

    filt = home.query("滤芯")
    assert "12%" in filt


def test_query_without_keyword_lists_devices():
    home = _devices([LIGHT, CLIMATE])

    out = home.query()

    assert "客厅灯" in out
    assert "卧室空调" in out


def test_query_miss_lists_available_names():
    """没找到要让模型知道有哪些设备，别直接说「没有」就完了。"""
    home = _devices([LIGHT, CLIMATE])

    out = home.query("扫地机")

    assert "没有找到「扫地机」" in out
    assert "客厅灯" in out
    assert "卧室空调" in out


def test_internal_entities_are_not_exposed():
    """HA 的内部实体（device_tracker/automation 之类）别念给用户。"""
    home = _devices([LIGHT, TRACKER])

    out = home.query()

    assert "客厅灯" in out
    assert "手机" not in out


def test_offline_device_says_offline_not_on_or_off():
    home = _devices([OFFLINE])

    out = home.query()

    assert "离线" in out


def test_unconfigured_gateway_says_so():
    home = _devices([LIGHT], configured=False)
    assert "还没配置" in home.query("客厅灯")


def test_unreachable_gateway_returns_sentence_not_exception():
    """HA 挂了只能如实说，工具里不能抛异常（会把服务端打成 500）。"""
    home = _devices(error=HomeUnavailable("设备网关连不上（超时）"))

    out = home.query("客厅灯")

    assert "连不上" in out


def test_empty_device_list_explains_what_to_do():
    home = _devices([])
    assert "设备清单是空的" in home.query()


def test_build_home_returns_none_without_token(monkeypatch):
    monkeypatch.setattr("adapters.home.HOME_TOKEN", "")
    assert build_home() is None


def test_build_home_with_token(monkeypatch):
    monkeypatch.setattr("adapters.home.HOME_TOKEN", "dummy-token")
    home = build_home()
    assert home is not None and home.configured


def test_control_allowlist_defaults_to_empty():
    """默认只读：白名单为空时什么都不让动。"""
    assert parse_control_allow("") == ()
    assert control_allowed("light.keting_deng", ()) is False


def test_control_allowlist_accepts_entity_and_domain_prefix():
    allow = parse_control_allow("light.keting_deng, switch. ,")
    assert control_allowed("light.keting_deng", allow) is True
    assert control_allowed("light.yangtai_deng", allow) is False
    assert control_allowed("switch.chazuo", allow) is True
    assert control_allowed("lock.damen", allow) is False


# ---------- 控制 ----------

def _allow(monkeypatch, value):
    monkeypatch.setattr("adapters.home.HOME_CONTROL_ALLOW", value)


def _fresh_light():
    return {
        "entity_id": "light.keting_deng",
        "state": "on",
        "attributes": {"friendly_name": "客厅灯", "brightness": 255},
    }


def test_control_turns_off_and_reads_back(monkeypatch):
    _allow(monkeypatch, "light.keting_deng")
    client = FakeClient([_fresh_light()])
    home = HomeDevices(client=client)

    out = home.control("客厅灯", "off", timeout=0)

    assert "已完成" in out
    assert "关" in out
    assert client.service_calls == [("light", "turn_off", "light.keting_deng", {})]


def test_control_sets_temperature(monkeypatch):
    _allow(monkeypatch, "climate.")
    row = {
        "entity_id": "climate.wo_shi",
        "state": "cool",
        "attributes": {"friendly_name": "卧室空调", "temperature": 28},
    }
    client = FakeClient([row])
    home = HomeDevices(client=client)

    out = home.control("卧室空调", "set_temperature", 26, timeout=0)

    assert "已完成" in out
    assert "设定 26" in out
    assert client.service_calls[0][1] == "set_temperature"
    assert client.service_calls[0][3] == {"temperature": 26.0}


def test_control_refused_without_allowlist(monkeypatch):
    """默认只读：白名单没列的设备一句都不许动。"""
    _allow(monkeypatch, "")
    client = FakeClient([_fresh_light()])
    home = HomeDevices(client=client)

    out = home.control("客厅灯", "off", timeout=0)

    assert "没开语音控制权限" in out
    assert client.service_calls == []


def test_control_refuses_high_risk_even_when_allowlisted(monkeypatch):
    _allow(monkeypatch, "lock.")
    lock = {
        "entity_id": "lock.damen",
        "state": "locked",
        "attributes": {"friendly_name": "大门锁"},
    }
    client = FakeClient([lock])
    home = HomeDevices(client=client)

    out = home.control("大门锁", "on", timeout=0)

    assert "高风险" in out
    assert client.service_calls == []


def test_control_asks_which_device_when_ambiguous(monkeypatch):
    _allow(monkeypatch, "light.")
    rows = [
        {"entity_id": "light.keting", "state": "on",
         "attributes": {"friendly_name": "客厅灯"}},
        {"entity_id": "light.canting", "state": "on",
         "attributes": {"friendly_name": "餐厅灯"}},
    ]
    client = FakeClient(rows)
    home = HomeDevices(client=client)

    out = home.control("灯", "off", timeout=0)

    assert "找到多个设备" in out
    assert "客厅灯" in out and "餐厅灯" in out
    assert client.service_calls == []


def test_control_reports_offline_instead_of_pretending(monkeypatch):
    _allow(monkeypatch, "light.")
    row = {"entity_id": "light.yangtai", "state": "unavailable",
           "attributes": {"friendly_name": "阳台灯"}}
    client = FakeClient([row])
    home = HomeDevices(client=client)

    out = home.control("阳台灯", "on", timeout=0)

    assert "离线" in out
    assert client.service_calls == []


def test_control_admits_when_state_did_not_change(monkeypatch):
    """HA 返回 200 但设备没动：不能回「已完成」。"""
    _allow(monkeypatch, "light.")
    client = FakeClient([_fresh_light()], applies=False)
    home = HomeDevices(client=client)

    out = home.control("客厅灯", "off", timeout=0)

    assert "状态没变" in out
    assert "已完成" not in out


def test_control_rejects_unknown_action(monkeypatch):
    _allow(monkeypatch, "light.")
    client = FakeClient([_fresh_light()])
    home = HomeDevices(client=client)

    out = home.control("客厅灯", "跳个舞", timeout=0)

    assert "不支持这个操作" in out
    assert client.service_calls == []


def test_control_enabled_follows_allowlist(monkeypatch):
    _allow(monkeypatch, "")
    assert HomeDevices(client=FakeClient([])).control_enabled is False

    _allow(monkeypatch, "light.")
    assert HomeDevices(client=FakeClient([])).control_enabled is True


# ---------- 真实米家实体的过滤 ----------

MIOT_HEATER = {
    "entity_id": "binary_sensor.xiaomi_cn_747166298_rr0r00_real_heater_switch_p_12_4",
    "state": "off",
    "attributes": {"friendly_name": "空调 * 机器状态 电加热实际开关"},
}
INDICATOR = {
    "entity_id": "light.xiaomi_cn_747166298_rr0r00_s_6_indicator_light",
    "state": "on",
    "attributes": {"friendly_name": "空调 指示灯"},
}
SUN = {
    "entity_id": "sensor.sun_next_dawn",
    "state": "2026-11-22T22:12:13+00:00",
    "attributes": {"friendly_name": "Sun 下个清晨"},
}
LEAK = {
    "entity_id": "binary_sensor.leak_1",
    "state": "off",
    "attributes": {"friendly_name": "厨房水浸", "device_class": "moisture"},
}
SPACED_LIGHT = {
    "entity_id": "light.yeelink_cn_282498628_ceiling18_s_2_light",
    "state": "on",
    "attributes": {"friendly_name": "客厅 灯"},
}


def test_snapshot_keeps_devices_beyond_fourteen(monkeypatch):
    """白名单是有顺序的，门锁/湿度常写在最后：上限太小会把「家里」卡的门锁截掉。

    2026-11-27 真机就是这条：18 个设备只给了 14 个，门锁和湿度正好在最后。
    """
    rows = [
        {"entity_id": "sensor.d%d" % index, "state": str(index),
         "attributes": {"friendly_name": "设备%d" % index}}
        for index in range(18)
    ]
    monkeypatch.setattr(
        "adapters.home.HOME_DEVICES",
        ", ".join("sensor.d%d=设备%d" % (index, index) for index in range(18)),
    )

    snap = HomeDevices(client=FakeClient(rows)).snapshot()

    names = [row["name"] for row in snap["devices"]]
    assert len(names) == 18
    assert names[-1] == "设备17"


def test_miot_property_entities_are_filtered_out():
    """空调一台就有十几条「电加热实际开关」这类属性实体，不能塞给模型。"""
    home = _devices([MIOT_HEATER, SPACED_LIGHT])

    out = home.query()

    assert "电加热" not in out
    assert "客厅 灯" in out


def test_indicator_lights_are_filtered_out():
    """指示灯不是给用户关的灯，留着会污染「关灯」的匹配。"""
    home = _devices([INDICATOR, SPACED_LIGHT])

    out = home.query()

    assert "指示灯" not in out
    assert "客厅 灯" in out


def test_system_entities_are_filtered_out():
    home = _devices([SUN, LEAK])
    out = home.query()

    assert "Sun" not in out
    assert "厨房水浸" in out


def test_sensor_without_unit_is_not_a_device():
    row = {
        "entity_id": "sensor.some_state",
        "state": "idle",
        "attributes": {"friendly_name": "某状态"},
    }
    home = _devices([row, SPACED_LIGHT])
    assert "某状态" not in home.query()


def test_chinese_name_matches_across_spaces():
    """小米给的名字带空格（客厅 灯），用户说的是「客厅灯」，必须能对上。"""
    home = _devices([SPACED_LIGHT])

    out = home.query("客厅灯")

    assert "客厅 灯" in out
    assert "没有找到" not in out

# ---------- 白名单与别名 ----------

def test_parse_devices_with_alias():
    assert parse_devices(" light.a=客厅灯 , sensor.b = RO滤芯 ") == (
        ("light.a", "客厅灯"),
        ("sensor.b", "RO滤芯"),
    )
    assert parse_devices("") == ()


def test_pinned_devices_replace_auto_filter(monkeypatch):
    """配了白名单就只认白名单，MIoT 的属性汤进不来。"""
    monkeypatch.setattr(
        "adapters.home.HOME_DEVICES",
        "light.keting=客厅灯, light.other=次卧灯",
    )
    client = FakeClient([
        {"entity_id": "light.keting", "state": "on",
         "attributes": {"friendly_name": "客厅  灯"}},
        {"entity_id": "light.other", "state": "off",
         "attributes": {"friendly_name": "次卧  灯"}},
        {"entity_id": "switch.junk", "state": "on",
         "attributes": {"friendly_name": "空调 风机控制 左上风区上下扫风"}},
    ])
    home = HomeDevices(client=client)

    out = home.query()

    assert "客厅灯" in out and "次卧灯" in out
    assert "扫风" not in out


def test_pinned_alias_is_what_gets_spoken(monkeypatch):
    monkeypatch.setattr(
        "adapters.home.HOME_DEVICES",
        "sensor.lx=RO滤芯",
    )
    client = FakeClient([{
        "entity_id": "sensor.lx",
        "state": "58",
        "attributes": {
            "friendly_name": "米家净水器1000G Plus  RO滤芯 滤芯剩余寿命",
            "unit_of_measurement": "%",
        },
    }])
    home = HomeDevices(client=client)

    out = home.query("RO滤芯")

    assert "RO滤芯" in out
    assert "58%" in out
    assert "1000G" not in out


def test_pinned_ignores_unknown_entities(monkeypatch):
    monkeypatch.setattr("adapters.home.HOME_DEVICES", "light.gone=客厅灯")
    home = HomeDevices(client=FakeClient([]))

    assert "HOME_DEVICES" in home.query("客厅灯")