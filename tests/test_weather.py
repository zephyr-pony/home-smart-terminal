"""天气工具：只返回事实，话术（穿衣/带伞）留给模型。

这几个断言就是「不给模型写死结论」的护栏：工具输出里出现"穿""伞"就该失败。
"""
import pytest

from adapters.weather import Weather, _cn_date, _format_days, _format_now


def test_cn_date_formats_for_speech():
    assert _cn_date("2026-11-24") == "11月24日（周二）"
    # 解析不了就原样返回，不要静默丢掉日期
    assert _cn_date("2026/11/24") == "2026/11/24"
    assert _cn_date("") == ""


def test_not_configured_says_so():
    text = Weather("", "", "烟台").query()
    assert "配置" in text


def test_without_city_asks_for_one():
    text = Weather("host", "key", "").query()
    assert "城市" in text


def test_format_now_has_every_now_field():
    """实时这一段只讲此刻，字段给齐（模型要念风力，代码不能替它判断"不重要"）。"""
    now = {
        "text": "晴", "temp": "1", "feelsLike": "-2", "humidity": "49",
        "windDir": "西南风", "wind360": "225", "windScale": "3",
        "windSpeed": "16", "vis": "28", "pressure": "1018",
        "precip": "0.0", "dew": "21", "cloud": "91",
        "obsTime": "2026-11-24T22:05+08:00",
    }
    text = _format_now("烟台", now)

    for expected in (
        "风力等级：3级", "风向角度：225°", "风速：16公里/小时",
        "露点温度：21℃", "云量：91%", "降水量：0.0毫米", "气压：1018百帕",
        "能见度：28公里", "观测时间：2026-11-24T22:05+08:00",
    ):
        assert expected in text, expected

    # 实时段不该混进"今天/未来"的概念
    assert "未来" not in text
    assert "最高气温" not in text


def test_format_days_has_every_day_field():
    daily = [
        {
            "fxDate": "2026-11-24", "textDay": "小雪", "textNight": "晴",
            "tempMax": "3", "tempMin": "0", "windDirDay": "西北风",
            "windScaleDay": "4-5", "windSpeedDay": "34",
            "windDirNight": "西北风", "windScaleNight": "1-3",
            "windSpeedNight": "16", "humidity": "53", "precip": "0.0",
            "pressure": "1017", "vis": "24", "cloud": "0", "uvIndex": "1",
            "sunrise": "06:42", "sunset": "16:38",
            "moonrise": "15:58", "moonset": "06:10", "moonPhase": "满月",
        }
    ]
    text = _format_days("烟台", daily, "今天")

    for expected in (
        "11月24日（周二）", "最高气温：3℃", "最低气温：0℃",
        "白天风力：4-5级", "夜间风力：1-3级", "紫外线指数：1",
        "日出：06:42", "日落：16:38", "月相：满月",
    ):
        assert expected in text, expected
    # 这一节不该带实时观测值
    assert "观测时间" not in text

    # 不写死建议（这是模型该干的活）
    assert "伞" not in text
    assert "穿" not in text
    assert "注意" not in text


def test_scope_now_only_calls_now_endpoint(monkeypatch):
    """scope=now 只请求实时接口，不拉预报。"""
    urls = []

    class _Resp:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    def fake_get(url, params=None, timeout=None):
        urls.append(url)
        if "geo/v2/city/lookup" in url:
            return _Resp({"code": "200", "location": [{"id": "101120501", "name": "烟台"}]})
        if url.endswith("/now"):
            return _Resp({"code": "200", "now": {"text": "晴", "temp": "1"}})
        return _Resp({"code": "200", "daily": [{"fxDate": "2026-11-24", "textDay": "晴"}]})

    monkeypatch.setattr("adapters.weather.requests.get", fake_get)

    text = Weather("demo.re.qweatherapi.com", "key", "烟台").query(scope="now")
    assert "实时天气" in text
    assert not any(u.endswith("/7d") for u in urls)
    assert not any(u.endswith("/3d") for u in urls)


def test_scope_today_skips_now(monkeypatch):
    urls = []

    class _Resp:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    def fake_get(url, params=None, timeout=None):
        urls.append(url)
        if "geo/v2/city/lookup" in url:
            return _Resp({"code": "200", "location": [{"id": "101120501", "name": "烟台"}]})
        if url.endswith("/now"):
            return _Resp({"code": "200", "now": {"text": "晴", "temp": "1"}})
        return _Resp({"code": "200", "daily": [
            {"fxDate": "2026-11-24", "textDay": "小雪", "tempMax": "3", "tempMin": "0"},
            {"fxDate": "2026-11-25", "textDay": "晴", "tempMax": "13", "tempMin": "3"},
        ]})

    monkeypatch.setattr("adapters.weather.requests.get", fake_get)

    text = Weather("demo.re.qweatherapi.com", "key", "烟台").query(scope="today")
    assert "11月24日" in text
    assert "11月25日" not in text
    assert not any(u.endswith("/now") for u in urls)


def test_scope_forecast_skips_today(monkeypatch):
    class _Resp:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    def fake_get(url, params=None, timeout=None):
        if "geo/v2/city/lookup" in url:
            return _Resp({"code": "200", "location": [{"id": "101120501", "name": "烟台"}]})
        return _Resp({"code": "200", "daily": [
            {"fxDate": "2026-11-24", "textDay": "小雪"},
            {"fxDate": "2026-11-25", "textDay": "晴"},
            {"fxDate": "2026-11-26", "textDay": "多云"},
        ]})

    monkeypatch.setattr("adapters.weather.requests.get", fake_get)

    text = Weather("demo.re.qweatherapi.com", "key", "烟台").query(scope="forecast", days=2)
    assert "11月24日" not in text
    assert "11月25日" in text
    assert "11月26日" in text


def test_query_uses_geo_lookup_path(monkeypatch):
    """和风把地理位置接口挪到了 /geo/v2/，用旧路径会 404。"""
    urls = []

    class _Resp:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    def fake_get(url, params=None, timeout=None):
        urls.append(url)
        if "geo/v2/city/lookup" in url:
            return _Resp({"code": "200", "location": [{"id": "101120501", "name": "烟台"}]})
        if url.endswith("/now"):
            return _Resp({"code": "200", "now": {"text": "晴", "temp": "1"}})
        return _Resp({"code": "200", "daily": [
            {"fxDate": "2026-11-24", "textDay": "小雪", "tempMax": "3", "tempMin": "0"},
            {"fxDate": "2026-11-25", "textDay": "晴", "tempMax": "13", "tempMin": "3"},
        ]})

    monkeypatch.setattr("adapters.weather.requests.get", fake_get)

    text = Weather("demo.re.qweatherapi.com", "key", "烟台").query()
    assert "烟台" in text
    assert "实时天气" in text
    assert "今天" in text
    assert any("geo/v2/city/lookup" in u for u in urls)


def test_upstream_failure_degrades_to_sentence(monkeypatch):
    """上游 500 时给一句人话，不要把异常抛到 agent loop 里。"""

    class _Resp:
        status_code = 500

        def json(self):
            return {}

    monkeypatch.setattr(
        "adapters.weather.requests.get", lambda *a, **k: _Resp()
    )
    text = Weather("demo.re.qweatherapi.com", "key", "烟台").query()
    assert "天气服务" in text
    assert "Traceback" not in text
