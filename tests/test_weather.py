"""天气：两个后端（和风 / Open-Meteo）都只给事实，话术留给模型。

「输出里不许出现穿、伞」这两条断言就是 D53 的护栏。
"""
import json

from adapters.weather import (
    Weather,
    _cn_date,
    _dir_name,
    _render_days,
    _render_now,
    _wind_level,
)

QW_HOST = "demo.re.qweatherapi.com"


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


def test_cn_date_formats_for_speech():
    assert _cn_date("2026-11-24") == "11月24日（周二）"
    assert _cn_date("2026/11/24") == "2026/11/24"
    assert _cn_date("") == ""


def test_without_city_asks_for_one_instead_of_config_talk():
    """Open-Meteo 不要 key，缺的只是城市：说人话让用户补，别再提环境变量。"""
    for weather in (Weather(), Weather("", "", "")):
        text = weather.query()
        assert "城市" in text
        assert "WEATHER_CITY" not in text


def test_city_lookup_wins_over_configured_city(monkeypatch):
    """用户说过的城市（facts）优先于 .env 的默认城市（D55）。"""
    urls = []
    monkeypatch.setattr(
        "adapters.weather.requests.get",
        _open_meteo_router(urls, {"杭州市": [
            {"name": "杭州市", "latitude": 30.29, "longitude": 120.16,
             "population": 7510000},
        ]}),
    )

    weather = Weather("", "", "烟台", city_lookup=lambda: "杭州")
    text = weather.query(scope="now")

    assert "杭州市" in text


def test_city_lookup_empty_falls_back_to_configured_city(monkeypatch):
    urls = []
    monkeypatch.setattr(
        "adapters.weather.requests.get",
        _open_meteo_router(urls, {"青岛市": [
            {"name": "青岛市", "latitude": 36.07, "longitude": 120.38,
             "population": 7172451},
        ]}),
    )

    weather = Weather("", "", "青岛", city_lookup=lambda: "")
    text = weather.query(scope="now")

    assert "青岛市" in text


def test_explicit_location_wins_over_city_lookup(monkeypatch):
    urls = []
    monkeypatch.setattr(
        "adapters.weather.requests.get",
        _open_meteo_router(urls, {"北京市": [
            {"name": "北京市", "latitude": 39.9, "longitude": 116.4,
             "population": 18960744},
        ]}),
    )

    text = Weather("", "", "烟台", city_lookup=lambda: "杭州").query(
        "北京", scope="now"
    )

    assert "北京市" in text


def test_snapshot_without_city_is_not_an_error_to_call_api(monkeypatch):
    calls = []

    def fake_get(*args, **kwargs):
        calls.append(args)
        raise AssertionError("没有城市不该发请求")

    monkeypatch.setattr("adapters.weather.requests.get", fake_get)

    snap = Weather().snapshot()

    assert snap["ok"] is False
    assert "城市" in snap["error"]
    assert calls == []


# ---------- 和风 ----------


def _qweather_router(urls):
    def fake_get(url, params=None, timeout=None):
        urls.append(url)
        if "geo/v2/city/lookup" in url:
            return _Resp({"code": "200", "location": [{"id": "101120501", "name": "烟台"}]})
        if url.endswith("/now"):
            return _Resp({"code": "200", "now": {
                "text": "晴", "temp": "1", "feelsLike": "-2", "humidity": "49",
                "windDir": "西南风", "wind360": "225", "windScale": "3",
                "windSpeed": "16", "vis": "28", "pressure": "1018",
                "precip": "0.0", "dew": "21", "cloud": "91",
            }})
        if url.endswith("/24h"):
            # 和风日预报没有降水概率，概率在逐小时里；只取当天的最大值
            return _Resp({"code": "200", "hourly": [
                {"fxTime": "2026-11-24T09:00+08:00", "pop": "10"},
                {"fxTime": "2026-11-24T15:00+08:00", "pop": "40"},
                {"fxTime": "2026-11-25T09:00+08:00", "pop": "90"},
            ]})
        return _Resp({"code": "200", "daily": [
            {"fxDate": "2026-11-24", "textDay": "小雪", "textNight": "晴",
             "tempMax": "3", "tempMin": "0", "windDirDay": "西北风",
             "windScaleDay": "4-5", "humidity": "53", "precip": "0.0",
             "uvIndex": "1", "sunrise": "06:42", "sunset": "16:38",
             "moonPhase": "满月"},
            {"fxDate": "2026-11-25", "textDay": "晴", "tempMax": "13", "tempMin": "3"},
            {"fxDate": "2026-11-26", "textDay": "多云", "tempMax": "9", "tempMin": "1"},
        ]})
    return fake_get


def test_qweather_uses_geo_v2_lookup(monkeypatch):
    urls = []
    monkeypatch.setattr("adapters.weather.requests.get", _qweather_router(urls))

    text = Weather(QW_HOST, "key", "烟台").query()
    assert "烟台" in text
    assert any("geo/v2/city/lookup" in u for u in urls)


def test_qweather_scope_now_only_hits_now_endpoint(monkeypatch):
    urls = []
    monkeypatch.setattr("adapters.weather.requests.get", _qweather_router(urls))

    text = Weather(QW_HOST, "key", "烟台").query(scope="now")
    assert "实时天气" in text
    assert "风力等级：3级" in text
    assert not any(u.endswith("/7d") for u in urls)


def test_qweather_scope_today_skips_now_and_other_days(monkeypatch):
    urls = []
    monkeypatch.setattr("adapters.weather.requests.get", _qweather_router(urls))

    text = Weather(QW_HOST, "key", "烟台").query(scope="today")
    assert "11月24日" in text
    assert "11月25日" not in text
    assert not any(u.endswith("/now") for u in urls)


def test_qweather_scope_forecast_skips_today(monkeypatch):
    monkeypatch.setattr("adapters.weather.requests.get", _qweather_router([]))

    text = Weather(QW_HOST, "key", "烟台").query(scope="forecast", days=2)
    assert "11月24日" not in text
    assert "11月25日" in text
    assert "11月26日" in text


def test_qweather_output_has_no_advice():
    """D53：工具不许写穿衣/带伞。"""
    facts = [("天气", "小雪"), ("气温", "0℃")]
    text = _render_now("烟台", facts)
    assert "穿" not in text and "伞" not in text


# ---------- Open-Meteo ----------


def _open_meteo_router(urls, geocode_results=None):
    geocode_results = geocode_results or {}

    def fake_get(url, params=None, timeout=None):
        urls.append(url)
        if "geocoding-api" in url:
            name = (params or {}).get("name", "")
            return _Resp({"results": geocode_results.get(name, [])})
        return _Resp({
            "current": {
                "time": "2026-11-25T10:30",
                "temperature_2m": 8.6,
                "apparent_temperature": 2.3,
                "relative_humidity_2m": 29,
                "wind_speed_10m": 21.5,
                "wind_direction_10m": 210,
                "weather_code": 0,
                "precipitation": 0.0,
                "pressure_msl": 1019.9,
                "cloud_cover": 6,
                "visibility": 31200.0,
            },
            "daily": {
                "time": ["2026-11-25", "2026-11-26", "2026-11-27"],
                "weather_code": [0, 2, 61],
                "temperature_2m_max": [13.1, 17.2, 12.9],
                "temperature_2m_min": [-1.6, 7.3, 5.1],
                "wind_speed_10m_max": [24.8, 27.5, 23.6],
                "wind_direction_10m_dominant": [209, 203, 281],
                "precipitation_sum": [0.0, 0.0, 3.2],
                "uv_index_max": [3.05, 3.0, 2.45],
                "sunrise": ["2026-11-25T06:43", "2026-11-26T06:44", "2026-11-27T06:45"],
                "sunset": ["2026-11-25T16:38", "2026-11-26T16:37", "2026-11-27T16:37"],
            },
        })
    return fake_get


def test_open_meteo_geocoding_merges_city_and_shi():
    """「烟台」会命中辽宁小村，要和「烟台市」合并后取人口最大的。"""
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append((params or {}).get("name"))
        if (params or {}).get("name") == "烟台":
            return _Resp({"results": [
                {"name": "烟台", "latitude": 40.6, "longitude": 122.6, "population": None},
            ]})
        return _Resp({"results": [
            {"name": "烟台市", "latitude": 37.476, "longitude": 121.441,
             "population": 7102100},
        ]})

    import adapters.weather as mod
    original = mod.requests.get
    mod.requests.get = fake_get
    try:
        info = Weather("", "", "烟台")._om_geocode("烟台")
    finally:
        mod.requests.get = original

    assert info["name"] == "烟台市"
    assert abs(info["latitude"] - 37.476) < 0.01
    assert "烟台" in calls and "烟台市" in calls


def test_open_meteo_maps_fields_and_units(monkeypatch):
    urls = []
    monkeypatch.setattr(
        "adapters.weather.requests.get",
        _open_meteo_router(urls, {"青岛市": [
            {"name": "青岛市", "latitude": 36.07, "longitude": 120.38, "population": 7172451},
        ]}),
    )

    text = Weather("", "", "青岛").query(scope="now")
    assert "天气：晴" in text            # WMO 0 → 晴
    assert "风力等级：4级" in text        # 21.5 km/h → 4 级
    assert "风向：西南风" in text         # 210° 落在西南风区间
    assert "能见度：31.2公里" in text     # 31200 m → km
    assert "穿" not in text and "伞" not in text


def test_open_meteo_prefers_configured_coordinates(monkeypatch):
    urls = []
    monkeypatch.setattr("adapters.weather.requests.get", _open_meteo_router(urls))

    Weather("", "", "烟台", 37.476, 121.441).query(scope="today")
    # 配了经纬度就不该再调 geocoding
    assert not any("geocoding-api" in u for u in urls)


def test_open_meteo_today_and_forecast_are_separate(monkeypatch):
    monkeypatch.setattr("adapters.weather.requests.get", _open_meteo_router([]))

    today = Weather("", "", "", 37.476, 121.441).query(scope="today")
    assert "11月25日" in today
    assert "11月26日" not in today

    forecast = Weather("", "", "", 37.476, 121.441).query(scope="forecast", days=2)
    assert "11月25日" not in forecast
    assert "11月26日" in forecast
    assert "11月27日" in forecast


def test_upstream_failure_degrades_to_sentence(monkeypatch):
    monkeypatch.setattr(
        "adapters.weather.requests.get",
        lambda *a, **k: _Resp({}, status=500),
    )
    for weather in (Weather(QW_HOST, "key", "烟台"), Weather("", "", "", 37.4, 121.4)):
        text = weather.query()
        assert "天气服务" in text
        assert "Traceback" not in text


# ---------- 单位换算 ----------


def test_wind_level_and_direction_helpers():
    assert _wind_level(0.5) == "0级"
    assert _wind_level(21.5) == "4级"
    assert _wind_level(120) == "12级"
    assert _wind_level(None) is None
    assert _dir_name(0) == "北风"
    assert _dir_name(180) == "南风"
    assert _dir_name(210) == "西南风"
    assert _dir_name(315) == "西北风"
    assert _dir_name(None) is None


def test_render_days_keeps_all_fields():
    day = {"date": "2026-11-25", "fields": [("最高气温", "13℃"), ("紫外线指数", "3")]}
    text = _render_days("烟台", "今天", [day])
    assert "11月25日（周三）" in text
    assert "最高气温：13℃" in text
    assert "紫外线指数：3" in text


# ---------- 看板快照（结构化，不是给模型念的句子）----------


def test_snapshot_open_meteo_is_structured(monkeypatch):
    monkeypatch.setattr("adapters.weather.requests.get", _open_meteo_router([]))

    snap = Weather("", "", "", 37.476, 121.441).snapshot(days=2)

    assert snap["ok"] is True
    assert snap["provider"] == "open-meteo"
    assert snap["now"]["temp"] == 8.6
    assert snap["now"]["sky"] == "晴"
    assert snap["now"]["wind_level"] == "4级"
    assert [day["date"] for day in snap["days"]] == [
        "2026-11-25", "2026-11-26", "2026-11-27",
    ]
    assert snap["days"][0]["high"] == 13.1
    # 假数据里没有降水概率：宁可不给，也不要编一个 0%
    assert snap["days"][0]["pop"] is None
    assert "穿" not in json.dumps(snap, ensure_ascii=False)


def test_snapshot_qweather_converts_strings_to_numbers(monkeypatch):
    monkeypatch.setattr("adapters.weather.requests.get", _qweather_router([]))

    snap = Weather(QW_HOST, "key", "烟台").snapshot(days=2)

    assert snap["ok"] is True
    assert snap["provider"] == "qweather"
    assert snap["now"]["temp"] == 1
    assert snap["now"]["wind_level"] == "3级"
    assert snap["days"][0]["high"] == 3
    assert snap["days"][1]["low"] == 3
    # 降水概率取当天逐小时的最大值，明天的 90% 不算进来
    assert snap["days"][0]["pop"] == 40


def test_snapshot_failure_returns_ok_false(monkeypatch):
    monkeypatch.setattr(
        "adapters.weather.requests.get", lambda *a, **k: _Resp({}, status=500),
    )

    snap = Weather(QW_HOST, "key", "烟台").snapshot()

    assert snap["ok"] is False
    assert snap["error"]
    # 没配置时也只给一句话，不抛异常
    assert Weather().snapshot()["ok"] is False
