"""和风天气：实时 + 未来几天预报。

为什么单独放一层：外部 HTTP 服务，超时、鉴权失败、上游返回码变化都在这里
收敛成「一段能直接念的文本」，工具层和编排层不需要知道 HTTP 细节。
和风的 api_host 是账号专属域名，必须和 api_key 配对；用公共域名会 403。
"""
import logging
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 6
FORECAST_DAYS = 3
_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# 和风 v7/weather/now 的全部字段。原样给模型，不挑选：念哪些、怎么组织是模型的事，
# 少给一个字段，模型就只能编或者被问第二遍。
_NOW_FIELDS = (
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

# 和风 v7/weather/3d 的每日字段
_DAILY_FIELDS = (
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


class Weather:
    """和风天气查询（只读，没有写操作）。"""

    def __init__(self, api_host="", api_key="", default_city=""):
        self.api_host = (api_host or "").strip()
        self.api_key = (api_key or "").strip()
        self.default_city = (default_city or "").strip()

    @property
    def configured(self):
        return bool(self.api_host and self.api_key)

    def query(self, location="", scope="all", days=FORECAST_DAYS):
        """按 scope 取对应范围的数据。

        scope:
          now      只查此刻实时（现在几度、风大不大）
          today    只查今天一整天（今天穿什么、要不要带伞）
          forecast 只查未来几天，不含今天（明天、这周）
          all      两个都给，三段分开标注

        为什么分开：实时、今天、未来是三个概念。一次全灌的话，问「现在怎么样」
        会带着未来三天，问「今天天气」会混进 22:05 的实时值，模型得自己分辨。
        """
        if not self.configured:
            return "天气服务还没配置，先把和风的 API Host 和 Key 填进 .env"
        city = (location or self.default_city).strip()
        if not city:
            return "我还不知道你家在哪个城市，说个城市名我就查"

        try:
            city_info = self._city_lookup(city)
        except Exception as exc:
            logger.warning("天气城市解析失败: %s", exc)
            return "天气服务暂时连不上，等会儿再问我"

        if not city_info:
            return "没找到 %s 这个地方，换个说法试试" % city

        location_id = city_info.get("id")
        city_name = city_info.get("name") or city
        scope = (scope or "all").strip().lower()
        if scope not in ("now", "today", "forecast", "all"):
            scope = "all"

        if scope == "now":
            try:
                now = self._weather("now", location_id)
            except Exception as exc:
                logger.warning("天气实时数据获取失败: %s", exc)
                return "天气服务暂时连不上，等会儿再问我"
            if not now:
                return "天气服务暂时查不到 %s 的实时数据" % city_name
            return _format_now(city_name, now.get("now") or {})

        try:
            daily = self._weather("7d", location_id)
        except Exception as exc:
            logger.warning("天气预报数据获取失败: %s", exc)
            return "天气服务暂时连不上，等会儿再问我"
        items = (daily or {}).get("daily") or []
        if not items:
            return "天气服务暂时查不到 %s 的预报" % city_name

        if scope == "today":
            return _format_days(city_name, items[:1], "今天")

        try:
            want = int(days)
        except (TypeError, ValueError):
            want = FORECAST_DAYS
        want = max(1, want)
        # 和风的 daily[0] 就是今天，forecast 要跳过它
        picked = items[1:1 + want]
        if not picked:
            return _format_days(city_name, items[:1], "今天")
        if scope == "forecast":
            return _format_days(city_name, picked, "未来 %d 天" % len(picked))

        # all：三段分开标注，不做合并
        blocks = []
        try:
            now = self._weather("now", location_id)
        except Exception as exc:
            logger.warning("天气实时数据获取失败: %s", exc)
            now = None
        if now:
            blocks.append(_format_now(city_name, now.get("now") or {}))
        blocks.append(_format_days(city_name, items[:1], "今天"))
        blocks.append(_format_days(city_name, picked, "未来 %d 天" % len(picked)))
        return "\n\n".join(blocks)

    # ---------- 内部 ----------

    def _get(self, path, params):
        url = "https://%s/%s" % (self.api_host, path.lstrip("/"))
        query = dict(params or {})
        query["key"] = self.api_key
        resp = requests.get(url, params=query, timeout=TIMEOUT_SECONDS)
        if resp.status_code != 200:
            raise RuntimeError("HTTP %s" % resp.status_code)
        data = resp.json()
        if str(data.get("code")) != "200":
            raise RuntimeError("code=%s" % data.get("code"))
        return data

    def _city_lookup(self, city):
        # 和风 2024 年起把地理位置接口挪到 /geo/v2/，旧的 /v2/city/lookup 会 404
        data = self._get("geo/v2/city/lookup", {"location": city, "number": 1})
        locations = data.get("location") or []
        return locations[0] if locations else None

    def _weather(self, endpoint, location_id):
        return self._get("v7/weather/%s" % endpoint, {"location": location_id})


def _format_now(city_name, now):
    """实时这一段：只讲此刻，不带今天/未来。"""
    lines = ["%s 实时天气" % city_name]
    for label, key, unit in _NOW_FIELDS:
        value = now.get(key)
        if value not in (None, ""):
            lines.append("%s：%s%s" % (label, value, unit))
    obs_time = now.get("obsTime")
    if obs_time:
        lines.append("观测时间：%s" % obs_time)
    return "\n".join(lines)


def _format_days(city_name, items, title):
    """按天这一段：字段全给，不裁剪（D53）。"""
    lines = ["%s %s" % (city_name, title)]
    for item in items:
        lines.append("%s：" % _cn_date(item.get("fxDate")))
        for label, key, unit in _DAILY_FIELDS:
            value = item.get(key)
            if value not in (None, ""):
                lines.append("  %s：%s%s" % (label, value, unit))
    return "\n".join(lines)


def _cn_date(fx_date):
    """2026-11-24 → 11月24日（周一）。

    为什么不用原始 ISO：语音里「二零二六杠十一杠二四」用户根本听不出对错，
    而模型也不会主动替我们转格式。
    """
    if not fx_date:
        return ""
    try:
        dt = datetime.strptime(str(fx_date), "%Y-%m-%d")
    except ValueError:
        return str(fx_date)
    return "%d月%d日（%s）" % (dt.month, dt.day, _WEEKDAYS[dt.weekday()])
