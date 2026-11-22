"""设备主动提醒：把「滤芯快没了」「净水器报故障」「摄像头有人出现」变成通知。

设备状态在 HA 里是一手数据，但小马的价值在于**先开口**——没人会每天打开米家
看滤芯百分比，等发现时已经喝了两周没换滤芯的水。

两件事：

1. **健康巡检**：周期性读白名单里的量值（滤芯余量、故障码），跌破阈值就提醒，
   按冷却时间 + 上次的值去重，避免每 60 秒喊一次。
2. **事件巡检**：HA 的 `event` 实体状态就是「上次触发时间」，比对上一次看到的
   就知道有没有新事件。

为什么轮询而不是 WebSocket：现有的提醒循环本来就是 60 秒一轮，多一条长连接要多
处理鉴权、重连、断线补发；摄像头这类事件晚半分钟没关系。等水浸/烟感接进来、
要求秒级时再换。

刻意没做的事：摄像头事件默认**关**。移动侦测的触发频率以小时计，默认开就是把
客厅变成刷屏机器，得让用户自己决定要盯哪一路。
"""
import re
import threading
from datetime import datetime

from config import (
    HOME_ALERT_COOLDOWN_HOURS,
    HOME_EVENT_COOLDOWN_SECONDS,
    HOME_FILTER_ALERT_PERCENT,
    HOME_WATCH_EVENTS,
)

# 事件实体给的时间戳形如 2026-11-22T12:04:16.754+00:00
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
# 事件时间戳在这个窗口内，才认为是「刚发生的」。超出的只记下来不播报，
# 否则一开机就把历史事件当新闻推一遍。
EVENT_FRESH_SECONDS = 180


def _event_time(raw):
    """把 HA 事件实体的状态（ISO 时间）解析成本地 naive 时间。"""
    try:
        stamp = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone().replace(tzinfo=None)
    return stamp


def _is_fresh(raw, now, seconds=EVENT_FRESH_SECONDS):
    stamp = _event_time(raw)
    if stamp is None:
        return False
    delta = (now - stamp).total_seconds()
    # 允许一点时钟偏差，但未来的时间戳不当真
    return -60 <= delta <= seconds


def parse_event_ids(value):
    """HOME_WATCH_EVENTS：逗号分隔的 event 实体 id，留着空 = 不盯任何事件。"""
    return tuple(
        part.strip()
        for part in str(value or "").replace("，", ",").split(",")
        if part.strip()
    )


def _is_filter_sensor(row):
    """滤芯余量：名字带「滤芯」、单位是百分比的 sensor。"""
    attrs = row.get("attributes") or {}
    name = str(attrs.get("friendly_name") or "")
    return "滤芯" in name and attrs.get("unit_of_measurement") == "%"


def _is_fault_sensor(row):
    attrs = row.get("attributes") or {}
    return "故障" in str(attrs.get("friendly_name") or "")


def _to_number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class HomeWatcher:
    def __init__(self, devices, store, filter_percent=None, cooldown_hours=None,
                 event_ids=None, event_cooldown_seconds=None):
        self.devices = devices
        self.store = store
        self.filter_percent = (
            HOME_FILTER_ALERT_PERCENT if filter_percent is None else filter_percent
        )
        self.cooldown_hours = (
            HOME_ALERT_COOLDOWN_HOURS if cooldown_hours is None else cooldown_hours
        )
        self.event_ids = (
            parse_event_ids(HOME_WATCH_EVENTS) if event_ids is None else event_ids
        )
        self.event_cooldown_seconds = (
            HOME_EVENT_COOLDOWN_SECONDS
            if event_cooldown_seconds is None
            else event_cooldown_seconds
        )
        # WS 推送在事件循环里，轮询在线程里，两边都会读写去重状态
        self._event_lock = threading.Lock()

    @property
    def enabled(self):
        return bool(self.event_ids) or self.filter_percent is not None

    def _cooled(self, state, now, seconds):
        """距上次提醒够久了没。"""
        if not state or not state.get("last_alert_at"):
            return True
        try:
            last = datetime.fromisoformat(state["last_alert_at"])
        except (TypeError, ValueError):
            return True
        return (now - last).total_seconds() >= seconds

    def check(self, now=None, apply=True):
        """跑一轮巡检，返回要发出去的提醒（已按冷却过滤）。

        apply=False 只预览不写去重状态，给脚本和排障用。
        """
        now = now or datetime.now()
        alerts = []
        alerts.extend(self._check_devices(now, apply))
        alerts.extend(self._check_events(now, apply))
        return alerts

    def _check_devices(self, now, apply=True):
        out = []
        try:
            rows = self.devices._devices()
        except Exception:
            # 网关连不上就静默跳过，别把提醒循环打断
            return out

        cooldown = self.cooldown_hours * 3600
        for row in rows:
            entity_id = row.get("entity_id", "")
            attrs = row.get("attributes") or {}
            name = attrs.get("friendly_name") or entity_id
            raw = str(row.get("state", ""))

            if _is_filter_sensor(row) and self.filter_percent is not None:
                value = _to_number(raw)
                if value is None or value > self.filter_percent:
                    continue
                seen = self.store.get_home_alert("filter", entity_id)
                if not self._cooled(seen, now, cooldown):
                    continue
                out.append({
                    "kind": "filter",
                    "entity_id": entity_id,
                    "tag": "filter-%s" % entity_id.split(".")[-1],
                    "text": "%s只剩 %g%% 了，该换滤芯了" % (name, value),
                })
                if apply:
                    self.store.record_home_alert(
                        "filter", entity_id, raw, now.isoformat(),
                    )
            elif _is_fault_sensor(row):
                value = _to_number(raw)
                if value in (None, 0):
                    continue
                seen = self.store.get_home_alert("fault", entity_id)
                if seen and seen.get("last_value") == raw:
                    continue
                out.append({
                    "kind": "fault",
                    "entity_id": entity_id,
                    "tag": "fault-%s" % entity_id.split(".")[-1],
                    "text": "%s报故障了（代码 %s），看一眼米家 App" % (name, raw),
                })
                if apply:
                    self.store.record_home_alert(
                        "fault", entity_id, raw, now.isoformat(),
                    )
        return out

    def _check_events(self, now, apply=True):
        out = []
        if not self.event_ids:
            return out
        try:
            rows = {row.get("entity_id"): row for row in self.devices.client.states()}
        except Exception:
            return out

        for entity_id in self.event_ids:
            row = rows.get(entity_id)
            if not row:
                continue
            raw = str(row.get("state", ""))
            if not _ISO_RE.match(raw):
                # unknown / unavailable：没有新事件
                continue
            attrs = row.get("attributes") or {}
            alert = self.annotate_event(
                entity_id, raw, attrs.get("friendly_name") or entity_id, now, apply,
            )
            if alert:
                out.append(alert)
        return out

    def annotate_event(self, entity_id, raw, name, now=None, apply=True):
        """一条事件该不该播报。WS 推送和轮询共用这一套判定，避免推两遍。

        两种通道都会调到这里，所以去重状态（`home_alerts`）是唯一裁判：
        先到的记下 last_value，后到的看到值一样就跳过。
        """
        now = now or datetime.now()
        if not _ISO_RE.match(str(raw or "")):
            return None
        with self._event_lock:
            seen = self.store.get_home_alert("event", entity_id)
            if seen and seen.get("last_value") == str(raw):
                return None
            if not seen and not _is_fresh(raw, now):
                # 第一次看到的是旧事件：只记下来，别当新闻推
                if apply:
                    self.store.touch_home_alert("event", entity_id, raw)
                return None
            if not self._cooled(seen, now, self.event_cooldown_seconds):
                if apply:
                    self.store.touch_home_alert("event", entity_id, raw)
                return None
            if apply:
                self.store.record_home_alert("event", entity_id, str(raw), now.isoformat())
        return {
            "kind": "event",
            "entity_id": entity_id,
            "tag": "event-%s" % entity_id.split(".")[-1],
            "text": "%s" % name,
        }


def build_home_watch(home, store):
    """只有接了设备网关才建巡检器。"""
    if home is None:
        return None
    return HomeWatcher(home, store)
