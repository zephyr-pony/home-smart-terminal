"""天气查询：默认 Open-Meteo（不需要 key），配了和风就用和风（中国局地更准）。

为什么双后端：开源用户 clone 下来不该先注册 API key；而已经有和风 key 的人，
数据质量更好（中国气象局源），不该浪费。

两个后端都只产出「事实清单」，不写建议（D53）；scope=now/today/forecast 分范围取（D56）。
"""
import logging
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 8
FORECAST_DAYS = 3
_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# 和风 v7/weather/now 的全部字段（原样给模型，不裁剪）
_QW_NOW_FIELDS = (
    ("天气", "text", ""),
    ("气温", "temp", "℃"),
    ("体感温度", "feelsLike", "℃"),
    ("风向", "windDir", ""),
    ("风向角度", "wind360", "°"),
    ("风力等级", "windScale", "级"),
    ("风速", "windSpeed", "公里/小时"),
    ("湿度", "humidity", "%"),
    ("降水量", "precip", "毫米"),
    ("气压", "pressure", "百帕"),
    ("能见度", "vis", "公里"),
    ("云量", "cloud", "%"),
    ("露点温度", "dew", "℃"),
)

# 和风 v7/weather/7d 的每日字段
_QW_DAILY_FIELDS = (
    ("白天天气", "textDay", ""),
    ("夜间天气", "textNight", ""),
    ("最高气温", "tempMax", "℃"),
    ("最低气温", "tempMin", "℃"),
    ("白天风向", "windDirDay", ""),
    ("白天风力", "windScaleDay", "级"),
    ("白天风速", "windSpeedDay", "公里/小时"),
    ("夜间风向", "windDirNight", ""),
    ("夜间风力", "windScaleNight", "级"),
    ("夜间风速", "windSpeedNight", "公里/小时"),
    ("湿度", "humidity", "%"),
    ("降水量", "precip", "毫米"),
    ("气压", "pressure", "百帕"),
    ("能见度", "vis", "公里"),
    ("云量", "cloud", "%"),
    ("紫外线指数", "uvIndex", ""),
    ("日出", "sunrise", ""),
    ("日落", "sunset", ""),
    ("月升", "moonrise", ""),
    ("月落", "moonset", ""),
    ("月相", "moonPhase", ""),
)

# WMO 天气码 → 中文（Open-Meteo 用它，和风的 text 已经是中文）
_WMO_ZH = {
    0: "晴", 1: "晴间多云", 2: "多云", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨",
    56: "冻毛毛雨", 57: "强冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "小阵雨", 81: "阵雨", 82: "强阵雨",
    85: "小阵雪", 86: "强阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷阵雨伴冰雹",
}

# 蒲福风级：风速上限（km/h）与等级
_BEAUFORT = (
    (1, 0), (5, 1), (11, 2), (19, 3), (28, 4), (38, 5),
    (49, 6), (61, 7), (74, 8), (88, 9), (102, 10), (117, 11),
)
_WIND_DIRS = ("北风", "东北风", "东风", "东南风", "南风", "西南风", "西风", "西北风")


class Weather:
    """天气查询。provider 由配置决定：和风的 host+key 优先，否则 Open-Meteo。"""

    def __init__(self, api_host="", api_key="", city="", lat=None, lon=None):
        self.api_host = (api_host or "").strip()
        self.api_key = (api_key or "").strip()
        self.city = (city or "").strip()
        self.lat = lat
        self.lon = lon
        self.provider = self._pick_provider()

    def _pick_provider(self):
        if self.api_host and self.api_key:
            return "qweather"
        if self.city or (self.lat is not None and self.lon is not None):
            return "open-meteo"
        return None

    @property
    def configured(self):
        return self.provider is not None

    def query(self, location="", scope="all", days=FORECAST_DAYS):
        """scope: now=此刻 / today=今天 / forecast=未来几天 / all=三段都给。"""
        if not self.configured:
            return "天气服务还没配置：填 WEATHER_CITY 就能用 Open-Meteo，或填和风的 Host/Key"
        scope = (scope or "all").strip().lower()
        if scope not in ("now", "today", "forecast", "all"):
            scope = "all"
        try:
            want = int(days)
        except (TypeError, ValueError):
            want = FORECAST_DAYS
        want = max(1, want)

        if self.provider == "qweather":
            return self._query_qweather(location, scope, want)
        return self._query_open_meteo(location, scope, want)

    # ---------- 看板用的结构化快照 ----------

    def snapshot(self, location="", days=3):
        """结构化天气（网页看板用）：数值和字段名，不是给模型念的文本。

        为什么不复用 query()：那边拼的是「气温：7℃」这种句子，前端要拆字符串
        才能排版，还容易跟着措辞改而坏掉。两边都从同一份原始响应取，不多发请求。
        """
        if not self.configured:
            return {"ok": False, "error": "天气服务还没配置"}
        try:
            want = int(days)
        except (TypeError, ValueError):
            want = FORECAST_DAYS
        want = max(1, min(want, FORECAST_DAYS))
        try:
            if self.provider == "qweather":
                return self._snapshot_qweather(location, want)
            return self._snapshot_open_meteo(location, want)
        except Exception as exc:
            logger.warning("天气快照失败: %s", exc)
            return {"ok": False, "error": "天气服务暂时连不上"}

    def _snapshot_open_meteo(self, location, days):
        city = (location or self.city).strip()
        lat, lon, city_name = self._om_coords(city)
        if lat is None:
            return {"ok": False, "error": "没找到 %s 这个地方" % (city or "位置")}

        data = self._om_fetch(lat, lon)
        current = data.get("current") or {}
        daily = data.get("daily") or {}
        dates = daily.get("time") or []

        out_days = []
        for index, date in enumerate(dates[:days + 1]):
            def pick(key):
                values = daily.get(key) or []
                return values[index] if index < len(values) else None

            code = pick("weather_code")
            out_days.append({
                "date": date,
                "sky": _WMO_ZH.get(code, "未知") if code is not None else "",
                "high": pick("temperature_2m_max"),
                "low": pick("temperature_2m_min"),
                "pop": pick("precipitation_probability_max"),
                "precip_mm": pick("precipitation_sum"),
                "wind_dir": _dir_name(pick("wind_direction_10m_dominant")),
                "wind_level": _wind_level(pick("wind_speed_10m_max")),
                "sunrise": _time_part(pick("sunrise")),
                "sunset": _time_part(pick("sunset")),
            })

        code = current.get("weather_code")
        return {
            "ok": True,
            "provider": "open-meteo",
            "city": city_name,
            "now": {
                "temp": current.get("temperature_2m"),
                "feels": current.get("apparent_temperature"),
                "sky": _WMO_ZH.get(code, "未知") if code is not None else "",
                "humidity": current.get("relative_humidity_2m"),
                "wind_dir": _dir_name(current.get("wind_direction_10m")),
                "wind_level": _wind_level(current.get("wind_speed_10m")),
                "updated_at": current.get("time"),
            },
            "days": out_days,
        }

    def _snapshot_qweather(self, location, days):
        city = (location or self.city).strip()
        info = self._qw_city(city)
        if not info:
            return {"ok": False, "error": "没找到 %s 这个地方" % (city or "位置")}

        location_id = info.get("id")
        city_name = info.get("name") or city
        now = (self._qw_get("v7/weather/now", location_id) or {}).get("now") or {}
        daily = (self._qw_get("v7/weather/7d", location_id) or {}).get("daily") or []

        # 和风的日预报没有降水概率（只有降水量），概率在逐小时里；
        # 取今天各小时的最大值当「今天降水概率」，取不到就不给，不编。
        today_pop = None
        today = daily[0].get("fxDate") if daily else None
        if today:
            try:
                hourly = (self._qw_get("v7/weather/24h", location_id) or {}).get("hourly") or []
                pops = []
                for hour in hourly:
                    if str(hour.get("fxTime") or "")[:10] != today:
                        continue
                    value = _num(hour.get("pop"))
                    if value is not None:
                        pops.append(value)
                today_pop = max(pops) if pops else None
            except Exception as exc:
                logger.warning("和风逐小时降水概率失败: %s", exc)

        out_days = []
        for index, item in enumerate(daily[:days + 1]):
            out_days.append({
                "date": item.get("fxDate"),
                "sky": item.get("textDay") or "",
                "sky_night": item.get("textNight") or "",
                "high": _num(item.get("tempMax")),
                "low": _num(item.get("tempMin")),
                "pop": today_pop if index == 0 else None,
                "precip_mm": _num(item.get("precip")),
                "wind_dir": item.get("windDirDay") or "",
                "wind_level": item.get("windScaleDay") or "",
                "sunrise": item.get("sunrise") or "",
                "sunset": item.get("sunset") or "",
            })

        return {
            "ok": True,
            "provider": "qweather",
            "city": city_name,
            "now": {
                "temp": _num(now.get("temp")),
                "feels": _num(now.get("feelsLike")),
                "sky": now.get("text") or "",
                "humidity": _num(now.get("humidity")),
                "wind_dir": now.get("windDir") or "",
                "wind_level": ("%s级" % now.get("windScale")
                               if now.get("windScale") not in (None, "") else ""),
                "updated_at": now.get("obsTime") or "",
            },
            "days": out_days,
        }

    # ---------- 和风 ----------

    def _query_qweather(self, location, scope, days):
        city = (location or self.city).strip()
        if not city:
            return "我还不知道你家在哪个城市，说个城市名我就查"
        try:
            city_info = self._qw_city(city)
        except Exception as exc:
            logger.warning("和风城市解析失败: %s", exc)
            return "天气服务暂时连不上，等会儿再问我"
        if not city_info:
            return "没找到 %s 这个地方，换个说法试试" % city

        location_id = city_info.get("id")
        city_name = city_info.get("name") or city

        if scope == "now":
            try:
                data = self._qw_get("v7/weather/now", location_id)
            except Exception as exc:
                logger.warning("和风实时数据失败: %s", exc)
                return "天气服务暂时连不上，等会儿再问我"
            return _render_now(city_name, _qw_now_facts((data or {}).get("now") or {}))

        try:
            daily = self._qw_get("v7/weather/7d", location_id)
        except Exception as exc:
            logger.warning("和风预报数据失败: %s", exc)
            return "天气服务暂时连不上，等会儿再问我"
        items = (daily or {}).get("daily") or []
        if not items:
            return "天气服务暂时查不到 %s 的预报" % city_name

        if scope == "today":
            return _render_days(city_name, "今天", [_qw_day_facts(items[0])])
        picked = items[1:1 + days]
        if not picked:
            return _render_days(city_name, "今天", [_qw_day_facts(items[0])])
        if scope == "forecast":
            return _render_days(city_name, "未来 %d 天" % len(picked),
                                [_qw_day_facts(x) for x in picked])

        blocks = []
        try:
            now = self._qw_get("v7/weather/now", location_id)
            if now:
                blocks.append(_render_now(city_name, _qw_now_facts(now.get("now") or {})))
        except Exception as exc:
            logger.warning("和风实时数据失败: %s", exc)
        blocks.append(_render_days(city_name, "今天", [_qw_day_facts(items[0])]))
        blocks.append(_render_days(city_name, "未来 %d 天" % len(picked),
                                   [_qw_day_facts(x) for x in picked]))
        return "\n\n".join(blocks)

    def _qw_get(self, path, location_id):
        url = "https://%s/%s" % (self.api_host, path.lstrip("/"))
        resp = requests.get(
            url, params={"key": self.api_key, "location": location_id},
            timeout=TIMEOUT_SECONDS,
        )
        if resp.status_code != 200:
            raise RuntimeError("HTTP %s" % resp.status_code)
        data = resp.json()
        if str(data.get("code")) != "200":
            raise RuntimeError("code=%s" % data.get("code"))
        return data

    def _qw_city(self, city):
        # 和风 2024 年起把地理位置接口挪到 /geo/v2/，旧的 /v2/city/lookup 会 404
        data = self._qw_get("geo/v2/city/lookup", city)
        locations = data.get("location") or []
        return locations[0] if locations else None

    # ---------- Open-Meteo ----------

    def _query_open_meteo(self, location, scope, days):
        city = (location or self.city).strip()
        lat, lon, city_name = self._om_coords(city)
        if lat is None:
            return "没找到 %s 这个地方，换个说法试试" % city if city else (
                "我还不知道你家在哪个城市，说个城市名我就查"
            )

        try:
            data = self._om_fetch(lat, lon)
        except Exception as exc:
            logger.warning("Open-Meteo 请求失败: %s", exc)
            return "天气服务暂时连不上，等会儿再问我"

        current = data.get("current") or {}
        daily = data.get("daily") or {}
        dates = daily.get("time") or []

        if scope == "now":
            return _render_now(city_name, _om_now_facts(current))

        if scope == "today":
            if not dates:
                return "天气服务暂时查不到 %s 的预报" % city_name
            return _render_days(city_name, "今天", [_om_day_facts(daily, 0)])

        start = 1
        end = min(1 + days, len(dates))
        if end <= start:
            if dates:
                return _render_days(city_name, "今天", [_om_day_facts(daily, 0)])
            return "天气服务暂时查不到 %s 的预报" % city_name
        picked = [_om_day_facts(daily, i) for i in range(start, end)]
        if scope == "forecast":
            return _render_days(city_name, "未来 %d 天" % len(picked), picked)

        blocks = [_render_now(city_name, _om_now_facts(current))]
        if dates:
            blocks.append(_render_days(city_name, "今天", [_om_day_facts(daily, 0)]))
        blocks.append(_render_days(city_name, "未来 %d 天" % len(picked), picked))
        return "\n\n".join(blocks)

    def _om_coords(self, city):
        """城市名 → 经纬度。配了经纬度就直接用，不走 geocoding。"""
        if not city and self.lat is not None and self.lon is not None:
            return float(self.lat), float(self.lon), self.city or "本地"
        if city and city == self.city and self.lat is not None and self.lon is not None:
            return float(self.lat), float(self.lon), self.city

        name = city or self.city
        if not name:
            return None, None, ""
        info = self._om_geocode(name)
        if not info:
            return None, None, name
        return info["latitude"], info["longitude"], info.get("name") or name

    def _om_geocode(self, name):
        """Open-Meteo 的地理编码：中文地名要同时搜「词」和「词市」，按人口取最大。

        实测「烟台」只命中辽宁小村，必须搜「烟台市」；而「北京市」又搜不到，
        得搜「北京」。两次合并、取人口最多的那个最稳。
        """
        query = name.strip()
        candidates = []
        seen = set()
        for candidate in (query, query + "市"):
            if candidate in seen:
                continue
            seen.add(candidate)
            try:
                resp = requests.get(
                    "https://geocoding-api.open-meteo.com/v1/search",
                    params={"name": candidate, "count": 5, "language": "zh", "format": "json"},
                    timeout=TIMEOUT_SECONDS,
                )
                if resp.status_code != 200:
                    continue
                for item in (resp.json() or {}).get("results") or []:
                    candidates.append(item)
            except Exception as exc:
                logger.debug("geocoding %s 失败: %s", candidate, exc)
        if not candidates:
            return None
        with_pop = [c for c in candidates if c.get("population")]
        pool = with_pop or candidates
        return max(pool, key=lambda c: c.get("population") or 0)

    def _om_fetch(self, lat, lon):
        params = {
            "latitude": lat,
            "longitude": lon,
            "current": ",".join((
                "temperature_2m", "apparent_temperature", "relative_humidity_2m",
                "wind_speed_10m", "wind_direction_10m", "weather_code",
                "precipitation", "pressure_msl", "cloud_cover", "visibility",
            )),
            "daily": ",".join((
                "weather_code", "temperature_2m_max", "temperature_2m_min",
                "wind_speed_10m_max", "wind_direction_10m_dominant",
                "precipitation_sum", "precipitation_probability_max",
                "uv_index_max", "sunrise", "sunset",
            )),
            "timezone": "Asia/Shanghai",
            "forecast_days": max(FORECAST_DAYS + 1, 7),
            "language": "zh",
        }
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params=params, timeout=TIMEOUT_SECONDS,
        )
        if resp.status_code != 200:
            raise RuntimeError("HTTP %s" % resp.status_code)
        return resp.json()


# ---------- 中间表示 → 文本 ----------


def _format_value(value, unit=""):
    if value in (None, ""):
        return None
    return "%s%s" % (value, unit)


def _num(value):
    """和风把数字都发成字符串；快照给前端要数值，能转就转，转不了给 None。"""
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number == int(number) else number


def _render_now(city_name, facts):
    lines = ["%s 实时天气" % city_name]
    for label, value in facts:
        if value not in (None, ""):
            lines.append("%s：%s" % (label, value))
    return "\n".join(lines)


def _render_days(city_name, title, days):
    lines = ["%s %s" % (city_name, title)]
    for day in days:
        lines.append("%s：" % _cn_date(day.get("date")))
        for label, value in day.get("fields") or []:
            if value not in (None, ""):
                lines.append("  %s：%s" % (label, value))
    return "\n".join(lines)


def _cn_date(fx_date):
    """2026-11-24 → 11月24日（周一）。语音里 ISO 串用户听不出对错。"""
    if not fx_date:
        return ""
    try:
        dt = datetime.strptime(str(fx_date)[:10], "%Y-%m-%d")
    except ValueError:
        return str(fx_date)
    return "%d月%d日（%s）" % (dt.month, dt.day, _WEEKDAYS[dt.weekday()])


# ---------- 和风 → 中间表示 ----------


def _qw_now_facts(now):
    return [(label, _format_value(now.get(key), unit)) for label, key, unit in _QW_NOW_FIELDS]


def _qw_day_facts(item):
    return {
        "date": item.get("fxDate"),
        "fields": [(label, _format_value(item.get(key), unit))
                   for label, key, unit in _QW_DAILY_FIELDS],
    }


# ---------- Open-Meteo → 中间表示 ----------


def _om_now_facts(current):
    code = current.get("weather_code")
    wind = current.get("wind_speed_10m")
    return [
        ("天气", _WMO_ZH.get(code, "未知") if code is not None else None),
        ("气温", _format_value(current.get("temperature_2m"), "℃")),
        ("体感温度", _format_value(current.get("apparent_temperature"), "℃")),
        ("湿度", _format_value(current.get("relative_humidity_2m"), "%")),
        ("风向", _dir_name(current.get("wind_direction_10m"))),
        ("风力等级", _wind_level(wind)),
        ("风速", _format_value(wind, "公里/小时")),
        ("降水量", _format_value(current.get("precipitation"), "毫米")),
        ("气压", _format_value(current.get("pressure_msl"), "百帕")),
        ("能见度", _format_value(_km(current.get("visibility")), "公里")),
        ("云量", _format_value(current.get("cloud_cover"), "%")),
        ("观测时间", current.get("time")),
    ]


def _om_day_facts(daily, index):
    def pick(key):
        values = daily.get(key) or []
        return values[index] if index < len(values) else None

    code = pick("weather_code")
    wind = pick("wind_speed_10m_max")
    return {
        "date": pick("time"),
        "fields": [
            ("天气", _WMO_ZH.get(code, "未知") if code is not None else None),
            ("最高气温", _format_value(pick("temperature_2m_max"), "℃")),
            ("最低气温", _format_value(pick("temperature_2m_min"), "℃")),
            ("风向", _dir_name(pick("wind_direction_10m_dominant"))),
            ("风力等级", _wind_level(wind)),
            ("最大风速", _format_value(wind, "公里/小时")),
            ("降水量", _format_value(pick("precipitation_sum"), "毫米")),
            ("紫外线指数", _format_value(pick("uv_index_max"))),
            ("日出", _time_part(pick("sunrise"))),
            ("日落", _time_part(pick("sunset"))),
        ],
    }


def _dir_name(degrees):
    """风向角度 → 八方位中文。"""
    if degrees is None:
        return None
    try:
        value = float(degrees) % 360
    except (TypeError, ValueError):
        return None
    return _WIND_DIRS[int((value + 22.5) // 45) % 8]


def _wind_level(kmh):
    """km/h → 蒲福风级。"""
    if kmh is None:
        return None
    try:
        value = float(kmh)
    except (TypeError, ValueError):
        return None
    for limit, level in _BEAUFORT:
        if value < limit:
            return "%d级" % level
    return "12级"


def _km(meters):
    if meters is None:
        return None
    try:
        return round(float(meters) / 1000.0, 1)
    except (TypeError, ValueError):
        return None


def _time_part(value):
    """2026-11-25T06:43 → 06:43。"""
    if not value:
        return None
    text = str(value)
    return text.split("T", 1)[1] if "T" in text else text
