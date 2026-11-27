"""Home Assistant 适配器 - 小米设备的唯一入口。

为什么绕道 HA 而不直连米家：

- 米家开放平台（iot.mi.com）只对硬件厂商开放，注册要企业组，做的是「把产品
  接入米家」，不是「让我的程序控制米家设备」。
- 小爱开放平台的方向是「用户对小爱说话 → 你的云服务」，反过来；小爱音箱也
  没有官方 TTS 出口。
- 直连 miIO 要逐台设备 token，新设备是 MIoT-Spec-V2，协议和加密都在变，
  每换一台设备就要重新折腾一次。

让 HA 去吸收几千个型号的差异，这里只跟 HA 的本地 REST 说话：读 `/api/states`，
调 `/api/services/{domain}/{service}`。领域层不认识小米，也不认识 HA。

降级原则：连不上就如实说，绝不假装执行成功（和 `agent/guard` 的假确认拦截
是同一个思路）。
"""
import re
import threading
import time

import requests

from config import (
    HOME_BASE_URL,
    HOME_CONTROL_ALLOW,
    HOME_DEVICES,
    HOME_TIMEOUT_SECONDS,
    HOME_TOKEN,
)

# 语音一句话就能改、且后果不可逆的设备：必须显式进白名单，且要人确认
HIGH_RISK_DOMAINS = (
    "lock", "valve", "water_heater", "alarm_control_panel", "gas",
)
# 可以直接执行的设备类型（灯关了还能开，空调温度调错了再调回来）
SAFE_CONTROL_DOMAINS = (
    "light", "switch", "climate", "cover", "vacuum", "fan", "humidifier",
    "media_player",
)
# 列设备时展示的类型；automation / update / zone 这些内部实体不报给模型
VISIBLE_DOMAINS = (
    "light", "switch", "climate", "cover", "vacuum", "fan", "humidifier",
    "media_player", "sensor", "binary_sensor", "water_heater", "lock",
)

_STATE_LABELS = {
    "on": "开",
    "off": "关",
    "unavailable": "离线",
    "unknown": "未知",
    "idle": "待机",
    "docked": "已回充",
    "cleaning": "清扫中",
    "paused": "暂停",
    "returning": "回充中",
    "heating": "制热",
    "cooling": "制冷",
    "drying": "除湿",
    "fan_only": "送风",
    "auto": "自动",
}

_DEVICE_CLASS_LABELS = {
    "door": "门",
    "window": "窗",
    "moisture": "漏水",
    "smoke": "烟雾",
    "motion": "人体移动",
    "gas": "燃气",
    "tamper": "被移动",
    "battery": "电量",
    "temperature": "温度",
    "humidity": "湿度",
}

# MIoT 把每个属性都变成一个实体：一台空调能刷出十几条「电加热实际开关」
# 「四通阀开关」，全塞给模型只会干扰判断。这里只留「能说人话」的实体。
#
# 注意别用 `_p_\d+_\d+` 这种后缀做过滤：滤芯剩余寿命、水温这些**有用**的量值
# 也带这个后缀，一刀切会把它们连同噪音一起删掉。
_SKIP_NAME_KEYWORDS = (
    "指示灯", "防伪", "演示模式", "状态上报", "是否可用", "重置", "复位",
    "固件", "重启", "自定义服务", "进度条", "截图功能",
    # 设置项，不是「设备」
    "模式", "勿扰", "对接", "设置", "杯量", "出水量",
)
# 累计量和流水数字，用户要的是「还剩多少」，不是「用过多少」
_SENSOR_SKIP_WORDS = (
    "已使用", "已累计", "本次", "统计", "剩余流量", "剩余时间", "制水时长",
    "故障", "管温", "排气", "模块温度", "外机", "运行速度",
)
# 真正会被问到的量值。不带判断地把「有单位的传感器」全放进来，
# 一台空调就能贡献「外机电压 229V」这种没人会问的数字。
_SENSOR_KEEP_WORDS = (
    "滤芯", "寿命", "水质", "TDS", "电量", "PM2.5", "二氧化碳", "湿度", "水温",
)
_SYSTEM_ENTITY_PREFIXES = (
    "sun.", "backup.", "person.", "zone.", "update.", "automation.", "script.",
    "scene.", "persistent_notification.", "conversation.", "todo.", "tts.",
)
# binary_sensor 只有这几类值得念（水浸、烟感、门窗、人体、燃气、被移动）
_USEFUL_BINARY_CLASSES = (
    "moisture", "smoke", "door", "window", "motion", "gas", "tamper",
    "battery", "carbon_monoxide", "problem",
)


def _is_device(row):
    """这条实体值不值得说给用户听。"""
    entity_id = str(row.get("entity_id", ""))
    domain = entity_id.split(".")[0]
    attrs = row.get("attributes") or {}
    if domain not in VISIBLE_DOMAINS:
        return False
    if attrs.get("hidden"):
        return False
    if entity_id.startswith(_SYSTEM_ENTITY_PREFIXES):
        return False
    name = str(attrs.get("friendly_name") or "")
    if any(word in name for word in _SKIP_NAME_KEYWORDS):
        return False
    if domain == "binary_sensor":
        return attrs.get("device_class") in _USEFUL_BINARY_CLASSES
    if domain == "sensor":
        if any(word in name for word in _SENSOR_SKIP_WORDS):
            return False
        return any(word in name for word in _SENSOR_KEEP_WORDS)
    return True


class HomeUnavailable(Exception):
    """HA 没配置或连不上。调用方把它转成一句人话，别抛给 API。"""


def _round(value, digits=0):
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return value


def _describe(state, with_id=True):
    """把一条 HA state 说成一句中文，语音能直接念。"""
    attrs = state.get("attributes") or {}
    name = attrs.get("friendly_name") or state.get("entity_id", "")
    entity_id = state.get("entity_id", "")
    domain = str(entity_id).split(".")[0]
    raw = str(state.get("state", ""))
    value = _STATE_LABELS.get(raw, raw)

    if domain == "light":
        if raw == "on" and attrs.get("brightness") is not None:
            value = "开，亮度 %d%%" % _round(attrs["brightness"] / 255 * 100)
    elif domain == "climate":
        parts = [value]
        if attrs.get("temperature") is not None:
            parts.append("设定 %s℃" % _round(attrs["temperature"], 1))
        if attrs.get("current_temperature") is not None:
            parts.append("室温 %s℃" % _round(attrs["current_temperature"], 1))
        value = "，".join(parts)
    elif domain == "vacuum":
        parts = [value]
        if attrs.get("battery_level") is not None:
            parts.append("电量 %s%%" % _round(attrs["battery_level"]))
        value = "，".join(parts)
    elif domain == "sensor":
        unit = attrs.get("unit_of_measurement") or ""
        if unit:
            value = "%s%s" % (raw, unit)
        else:
            # 滤芯这类枚举值（如 "remaining_time"）没有单位，补上设备类别
            label = _DEVICE_CLASS_LABELS.get(attrs.get("device_class") or "")
            value = "%s（%s）" % (value, label) if label else value
    elif domain == "binary_sensor":
        label = _DEVICE_CLASS_LABELS.get(attrs.get("device_class") or "")
        if label:
            value = "%s：%s" % (label, "是" if raw == "on" else "否")

    return "- %s（%s）：%s" % (name, entity_id, value) if with_id else "%s：%s" % (name, value)


class HAClient:
    """HA 的 REST 薄封装。只做三件事：读状态、调服务、等状态变化。"""

    def __init__(self, base_url=None, token=None, timeout=None, session=None):
        self.base_url = str(base_url or HOME_BASE_URL).rstrip("/")
        self.token = token if token is not None else HOME_TOKEN
        self.timeout = timeout or HOME_TIMEOUT_SECONDS
        self._session = session or requests.Session()
        self._lock = threading.RLock()
        self._snapshot = (0.0, [])

    @property
    def configured(self):
        return bool(self.base_url and self.token)

    def _headers(self):
        return {
            "Authorization": "Bearer %s" % self.token,
            "Content-Type": "application/json",
        }

    def states(self, max_age=3.0):
        """设备状态快照。几秒内复用，避免一个回答连打好几次 HA。"""
        with self._lock:
            cached_at, rows = self._snapshot
            if rows and time.time() - cached_at < max_age:
                return rows
        if not self.configured:
            raise HomeUnavailable("还没接设备网关")
        try:
            resp = self._session.get(
                self.base_url + "/api/states",
                headers=self._headers(),
                timeout=self.timeout,
            )
            resp.raise_for_status()
            rows = resp.json()
        except Exception as exc:
            raise HomeUnavailable("设备网关连不上（%s）" % _error_text(exc))
        with self._lock:
            self._snapshot = (time.time(), rows)
        return rows

    def state(self, entity_id):
        """读一只设备的当前状态，未找到返回 None。"""
        for row in self.states(max_age=0):
            if row.get("entity_id") == entity_id:
                return row
        return None

    def call_service(self, domain, service, entity_id=None, data=None):
        """调用一个 HA 服务（即「执行动作」），返回是否成功。"""
        if not self.configured:
            raise HomeUnavailable("还没接设备网关")
        payload = dict(data or {})
        if entity_id:
            payload["entity_id"] = entity_id
        try:
            resp = self._session.post(
                "%s/api/services/%s/%s" % (self.base_url, domain, service),
                headers=self._headers(),
                json=payload,
                timeout=self.timeout,
            )
            resp.raise_for_status()
        except Exception as exc:
            raise HomeUnavailable("设备没有响应（%s）" % _error_text(exc))
        # 状态变了才算真的执行成功，200 只代表消息发出去了
        with self._lock:
            self._snapshot = (0.0, [])
        return True


def _error_text(exc):
    """把 requests 的异常压成一句人话，别把整段堆栈念给用户。"""
    if isinstance(exc, requests.Timeout):
        return "超时"
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 401:
        return "令牌无效，去 HA 重新生成长期访问令牌"
    if status:
        return "HTTP %s" % status
    return exc.__class__.__name__


_ACTION_ALIASES = {
    "on": "on", "开": "on", "打开": "on", "开启": "on", "turn_on": "on",
    "off": "off", "关": "off", "关上": "off", "关闭": "off", "turn_off": "off",
    "set_temperature": "set_temperature", "温度": "set_temperature",
    "调温": "set_temperature", "调温度": "set_temperature",
    "set_brightness": "set_brightness", "亮度": "set_brightness",
    "调亮度": "set_brightness",
    "open": "open", "拉起": "open",
    "close": "close", "合上": "close",
    "start": "start", "开始": "start", "清扫": "start",
    "stop": "stop", "暂停": "stop",
    "dock": "dock", "回充": "dock", "回去充电": "dock",
}


def normalize_action(action):
    """把模型给的动作用词归一；不认识返回 None。"""
    return _ACTION_ALIASES.get(str(action or "").strip().lower())


def _service_for(domain, action, value):
    """动作 → HA 的 service 调用。不认识的组合返回 (None, {})。

    不同设备类型用同一个「动作词」，由这里翻译成各自的 service：
    灯是 turn_on/off，窗帘是 open/close，扫地机是 start/return_to_base。
    """
    if domain in ("light", "switch", "fan", "humidifier", "media_player"):
        if action in ("on", "set_brightness"):
            if action == "set_brightness" or (value is not None and domain == "light"):
                if value is None:
                    return None, {}
                return "turn_on", {"brightness_pct": max(1, min(100, int(round(float(value)))))}
            return "turn_on", {}
        if action == "off":
            return "turn_off", {}
    elif domain == "climate":
        if action == "on":
            return "turn_on", {}
        if action == "off":
            return "turn_off", {}
        if action == "set_temperature" and value is not None:
            return "set_temperature", {"temperature": float(value)}
    elif domain == "cover":
        if action in ("open", "on"):
            return "open_cover", {}
        if action in ("close", "off"):
            return "close_cover", {}
        if action == "stop":
            return "stop_cover", {}
    elif domain == "vacuum":
        if action in ("start", "on"):
            return "start", {}
        if action in ("dock", "off"):
            return "return_to_base", {}
        if action == "stop":
            return "stop", {}
    return None, {}


def _state_matches(domain, action, value, state):
    """回读判断：设备真的到了期望状态才算成功。"""
    if not state:
        return False
    raw = str(state.get("state", ""))
    attrs = state.get("attributes") or {}
    if action in ("on", "open", "start"):
        return raw in ("on", "open", "cleaning")
    if action in ("off", "close", "dock", "stop"):
        return raw in ("off", "closed", "docked", "idle", "paused", "returning")
    if action == "set_temperature" and value is not None:
        temp = attrs.get("temperature")
        try:
            return abs(float(temp) - float(value)) < 0.6
        except (TypeError, ValueError):
            return False
    if action == "set_brightness" and value is not None:
        bright = attrs.get("brightness")
        try:
            return abs(bright / 255 * 100 - float(value)) <= 5
        except (TypeError, ValueError):
            return False
    return True


def parse_devices(value):
    """解析 HOME_DEVICES：`entity_id` 或 `entity_id=别名`，逗号分隔。

    别名很重要：米家给的名字是「米家净水器1000G Plus  RO滤芯 滤芯剩余寿命」，
    念出来像在背参数表，起了别名才像跟家里人说话。
    """
    entries = []
    for part in str(value or "").replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            entity_id, alias = part.split("=", 1)
            entries.append((entity_id.strip(), alias.strip()))
        else:
            entries.append((part, ""))
    return tuple(entries)


def parse_control_allow(value):
    """解析 HOME_CONTROL_ALLOW：逗号分隔的 entity_id 或 `light.` 这样的域前缀。"""
    items = []
    for part in str(value or "").replace("，", ",").split(","):
        part = part.strip()
        if part:
            items.append(part)
    return tuple(items)


def control_allowed(entity_id, allow=None):
    """这只设备能不能被语音直接控制。

    默认只读：白名单为空时什么都不让动，先把「读」跑顺了再说。
    """
    entity_id = str(entity_id or "")
    if not entity_id:
        return False
    allow = parse_control_allow(HOME_CONTROL_ALLOW) if allow is None else allow
    for rule in allow:
        if rule == entity_id:
            return True
        if rule.endswith(".") and entity_id.startswith(rule):
            return True
    return False


class HomeDevices:
    """面向小马的设备视图：能搜、能念状态，控制能力留到下一步。"""

    def __init__(self, client=None, limit=8):
        self.client = client or HAClient()
        self.limit = limit

    @property
    def configured(self):
        return self.client.configured

    @property
    def control_enabled(self):
        """白名单为空就是只读：控制工具干脆不注册，模型也没得可调。"""
        return bool(parse_control_allow(HOME_CONTROL_ALLOW))

    def _devices(self):
        """小马认识的设备。白名单优先；没配就退化成自动过滤（噪音很多）。"""
        rows = self.client.states()
        pinned = parse_devices(HOME_DEVICES)
        if not pinned:
            return [row for row in rows if _is_device(row)]

        by_id = {}
        for row in rows:
            by_id[row.get("entity_id")] = row
        picked = []
        for entity_id, alias in pinned:
            row = by_id.get(entity_id)
            if row is None:
                continue
            if alias:
                # 只改副本，别污染 HAClient 的缓存
                row = dict(row)
                row["attributes"] = dict(row.get("attributes") or {}, friendly_name=alias)
            picked.append(row)
        return picked

    def _match(self, devices, keyword):
        """按名字匹配。中文里「客厅灯」和「客厅 灯」要能对上，所以空格不参与比较。"""
        tokens = [t for t in str(keyword or "").replace("，", " ").split() if t]
        if not tokens:
            return devices
        squashed = "".join(tokens).lower()
        hits = []
        for row in devices:
            attrs = row.get("attributes") or {}
            haystack = "%s %s" % (
                attrs.get("friendly_name") or "", row.get("entity_id", ""),
            )
            haystack = haystack.lower()
            if squashed and squashed in haystack.replace(" ", ""):
                hits.append(row)
                continue
            if all(token.lower() in haystack for token in tokens):
                hits.append(row)
        return hits

    def snapshot(self, limit=14):
        """结构化设备清单（看板用）：名字、状态、单位，不念句子。

        看板要自己排版，不能解析 query() 的中文句子；这里只把白名单设备摊平。
        读不到就给空表 + 一句原因，页面据此决定显示「未配置」还是报错。
        """
        if not self.configured:
            return {"configured": False, "devices": []}
        try:
            devices = self._devices()
        except HomeUnavailable as exc:
            return {"configured": True, "error": str(exc), "devices": []}
        except Exception as exc:  # 网络抖动之类，看板不该整块挂掉
            print("⚠️ 设备快照失败：%s" % exc)
            return {"configured": True, "error": "设备读数暂时拿不到", "devices": []}

        rows = []
        for row in devices[:limit]:
            attrs = row.get("attributes") or {}
            entity_id = row.get("entity_id") or ""
            rows.append({
                "entity_id": entity_id,
                "name": attrs.get("friendly_name") or entity_id,
                "state": row.get("state", ""),
                "unit": attrs.get("unit_of_measurement") or "",
                "domain": entity_id.split(".")[0],
            })
        return {"configured": True, "devices": rows}

    def query(self, keyword="", limit=None):
        """按名字找设备并带上状态。一次调用拿到「有没有」+「什么样」。"""
        if not self.configured:
            return "设备网关还没配置，先在 .env 里填 HOME_TOKEN"
        limit = limit or self.limit
        try:
            devices = self._devices()
        except HomeUnavailable as exc:
            return str(exc)
        if not devices:
            if parse_devices(HOME_DEVICES):
                return "HOME_DEVICES 里的设备在 Home Assistant 里都找不到，检查 entity_id 是否写错"
            return "设备清单是空的，先在米家 App 里确认设备已同步到 Home Assistant"

        hits = self._match(devices, keyword)
        if hits:
            lines = [_describe(row) for row in hits[:limit]]
            more = len(hits) - len(lines)
            if more > 0:
                lines.append("（还有 %d 个匹配的设备没列出来）" % more)
            return "\n".join(lines)

        if not keyword:
            return "\n".join(_describe(row) for row in devices[:limit])

        names = [
            "%s" % ((row.get("attributes") or {}).get("friendly_name") or row["entity_id"])
            for row in devices[:12]
        ]
        return "没有找到「%s」。现在有：%s" % (keyword, "、".join(names))

    # ---------- 控制 ----------

    def _resolve(self, devices, device):
        """按名字定位一台设备。返回 (唯一命中, 候选列表)。

        命中多台时**不猜**：返回候选让模型反问用户（澄清只问一个问题）。
        """
        keyword = str(device or "").strip()
        if not keyword:
            return None, []
        for row in devices:
            if row.get("entity_id") == keyword:
                return row, []
        hits = self._match(devices, keyword)
        if len(hits) == 1:
            return hits[0], []
        return None, hits

    def _wait_state(self, entity_id, action, value, timeout):
        """回读：HA 返回 200 只代表消息发出去了，设备动没动要再看一眼。"""
        timeout = 2.5 if timeout is None else timeout
        deadline = time.time() + timeout
        state = self.client.state(entity_id)
        while not _state_matches(entity_id.split(".")[0], action, value, state):
            if time.time() >= deadline:
                return state, False
            time.sleep(0.3)
            try:
                state = self.client.state(entity_id)
            except HomeUnavailable:
                return None, False
        return state, True

    def control(self, device, action, value=None, timeout=None):
        """执行一个动作，然后回读确认。返回一句给人听的话。"""
        if not self.configured:
            return "设备网关还没配置，先在 .env 里填 HOME_TOKEN"
        raw_action = action
        action = normalize_action(raw_action)
        if action is None:
            return "不支持这个操作：%s" % raw_action
        try:
            devices = self._devices()
        except HomeUnavailable as exc:
            return str(exc)

        target, candidates = self._resolve(devices, device)
        if target is None:
            if candidates:
                names = [
                    (row.get("attributes") or {}).get("friendly_name") or row["entity_id"]
                    for row in candidates[:5]
                ]
                return "找到多个设备：%s，你要操作哪一个？" % "、".join(names)
            return "没有找到「%s」，可以先用 home_query 看看有哪些设备" % device

        entity_id = target.get("entity_id", "")
        domain = entity_id.split(".")[0]
        name = (target.get("attributes") or {}).get("friendly_name") or entity_id

        if domain in HIGH_RISK_DOMAINS:
            return "「%s」属于高风险设备，语音不直接操作，请在米家 App 里处理" % name
        if not control_allowed(entity_id):
            return "「%s」没开语音控制权限（.env 的 HOME_CONTROL_ALLOW 里没列它）" % name
        if str(target.get("state")) == "unavailable":
            return "「%s」现在离线，指令发不出去" % name

        service, data = _service_for(domain, action, value)
        if service is None:
            return "「%s」不支持这个操作" % name

        try:
            self.client.call_service(domain, service, entity_id, data)
        except HomeUnavailable as exc:
            return str(exc)

        state, matched = self._wait_state(entity_id, action, value, timeout)
        if state is None:
            return "指令发出去了，但读不到「%s」的状态" % name
        if not matched:
            return "指令发出去了，但「%s」状态没变（可能离线或不支持），现在是：%s" % (
                name, _describe(state, with_id=False).split("：", 1)[-1],
            )
        return "已完成：%s" % _describe(state, with_id=False)


def build_home():
    """按 .env 决定要不要接设备网关；没配 token 就返回 None（工具不注册）。"""
    client = HAClient()
    if not client.configured:
        return None
    return HomeDevices(client=client)
