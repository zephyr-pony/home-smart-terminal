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
    HOME_LOCK_COOLDOWN_SECONDS,
    HOME_LOCK_USERS,
    HOME_WATCH_EVENTS,
)

# 事件实体给的时间戳形如 2026-11-22T12:04:16.754+00:00
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
# event_type 只有带中文才拿来念：HA 给门铃是「门铃响了」，给别的集成可能是
# `completed` 这种英文枚举，念出来比实体名还费解。
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
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


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# 门锁事件（MIoT `lock-event`）把「谁、怎么开的」编成几个数字放在事件属性里。
# 表格来自 loock.lock.t3pul 的 MIoT spec（小米智能门锁 2 Pro），小米/鹿客这套锁
# 基本共用；不翻译的话，一条开门事件念出来只是「亚拉锁 门锁 锁事件」。
LOCK_METHODS = {
    1: "手机", 2: "指纹", 3: "密码", 4: "NFC", 5: "人脸", 6: "指静脉",
    7: "掌纹", 8: "机械钥匙", 9: "一次性密码", 10: "周期性密码", 11: "HomeKit",
    12: "胁迫", 13: "双重验证", 14: "转盘", 15: "手动", 16: "自动",
}
# 15 手动 / 16 自动不是「验证方式」，句子结构不一样，单独处理
LOCK_VERIFY_METHODS = tuple(m for m in LOCK_METHODS if m not in (15, 16))
LOCK_ACTIONS = {
    1: "上锁", 2: "开锁", 3: "上提加固", 4: "开启反锁", 5: "关闭反锁",
    6: "开启童锁", 7: "关闭童锁", 8: "开启离家模式", 9: "关闭离家模式",
}
# `exception-occurred` 事件里的异常码：这些比「谁开了门」更该说，尤其撬锁和频繁失败。
LOCK_EXCEPTIONS = {
    1: "多种方式连续开锁失败，有人在试",
    2: "密码连续输错",
    3: "指纹连续识别失败",
    4: "NFC 连续识别失败",
    5: "面容连续识别失败",
    6: "掌纹连续识别失败",
    7: "指静脉连续识别失败",
    8: "机械钥匙连续使用失败",
    9: "门锁被撬了，注意安全",
    10: "门一直没上锁",
    11: "开启离家模式后，有人从里面开了门",
    12: "门锁被重置了",
    13: "钥匙孔里有异物",
    14: "钥匙还插在门上",
    15: "指纹传感器异常",
    16: "机械故障",
    17: "锁体传感器故障",
    18: "电池温度过高",
    19: "门锁电量低",
    20: "门锁电量快没了",
    21: "猫眼电量低",
    22: "猫眼电量快没了",
    25: "有人在撬门，注意安全",
    26: "摄像头频繁录像",
}
# 23 门未关 / 24 门虚掩交给门状态传感器（它是持续状态，不会一闪而过），
# 从这里播报会和传感器重复，同一件事报两遍。
_DOOR_CODES = (23, 24)


def parse_lock_users(value):
    """HOME_LOCK_USERS：`1=爸爸,2=妈妈` → {1: '爸爸', 2: '妈妈'}。

    操作ID 是锁上报的凭据编号（实测人脸是 2001/2004 这种四位数、指纹是 501），
    同一人的人脸和指纹可能是两个号——所以这里按「凭据」映射，不是按家庭成员。
    只有家里人才知道 501 是谁，所以名字必须可配；没配就念编号，也比「有人」强。
    """
    users = {}
    for part in str(value or "").replace("，", ",").split(","):
        key, sep, name = part.partition("=")
        if not sep:
            continue
        uid = _to_int(key.strip())
        name = name.strip()
        if uid is not None and name:
            users[uid] = name
    return users


def describe_lock_event(attrs, users=None):
    """把一条门锁事件翻译成人话。

    Returns:
        None：不是锁事件（看不懂），调用方回落到实体名；
        ""：是锁事件，但不用播报（比如每次关门都跟着的自动上锁）；
        其它：要念的整句。

    只给事实：谁（操作ID）、用什么（操作方式）、做了什么（锁动作）、门内还是门外。
    拼句子是因为这条文本会被直接念出来，不是给模型看的素材。
    """
    attrs = attrs or {}
    action = _to_int(attrs.get("锁动作"))
    if action is None:
        return None
    method = _to_int(attrs.get("操作方式"))
    position = _to_int(attrs.get("操作位置"))
    uid = _to_int(attrs.get("操作ID")) or 0
    who = (users or {}).get(uid) or ("%d 号用户" % uid if uid else "")

    if action == 2:  # 开锁
        if method == 12:
            # 胁迫开锁：被逼着开门时输的密码，是这盏锁唯一的安全信号，必须说清
            return "%s用胁迫方式开了门，注意安全" % (who or "有人")
        if position == 1:
            return ("%s从门里面开了门" % who) if who else "门从里面打开了"
        if method == 5:
            # 真实数据里 3D 面容就是这个码；「刷脸」比「用人脸」更像人话
            return "%s刷脸开了门" % (who or "有人")
        if method in LOCK_VERIFY_METHODS:
            return "%s用%s开了门" % (who or "有人", LOCK_METHODS[method])
        if method == 15:
            if who:
                return "%s手动开了门" % who
            # 位置为 3 时锁自己也不知道门内门外，别替他断言
            return "有人在门外手动开了门" if position == 2 else "门被手动打开了"
        if method == 16:
            return "门自动开了"
        return ("%s开了门" % who) if who else "门开了"

    if action == 1:  # 上锁
        if method == 16:
            # 每次关门都跟着一条自动上锁，念出来纯属噪音；看懂了但不播报
            return ""
        if who and method in LOCK_VERIFY_METHODS:
            return "%s用%s锁了门" % (who, LOCK_METHODS[method])
        return ("%s锁了门" % who) if who else "门锁上了"

    tail = {
        3: "门上提加固了",
        4: "开启了反锁",
        5: "解除了反锁",
        6: "开启了童锁",
        7: "关闭了童锁",
        8: "开启了离家模式",
        9: "关闭了离家模式",
    }.get(action)
    return tail


def describe_lock_exception(attrs):
    """把 `exception-occurred` 的异常码翻译成人话。

    Returns: None（不是异常事件）/ ""（交给门状态传感器，不重复播报）/ 整句。
    """
    code = _to_int((attrs or {}).get("异常情况"))
    if code is None:
        return None
    if code in _DOOR_CODES:
        return ""
    return LOCK_EXCEPTIONS.get(code)


def _is_door_state_sensor(row):
    """门状态传感器：用 HA 给的 options 认，别靠实体名猜。"""
    options = (row.get("attributes") or {}).get("options") or []
    return any("门虚掩" in str(option) for option in options)


def _door_alert_text(raw):
    """门没关好时念什么；正常状态（已上锁/已开锁/unknown）返回空串。"""
    text = str(raw or "")
    if "门虚掩" in text:
        return "门虚掩着，可能没关好"
    if "门未关" in text:
        return "门没关，去看一下"
    return ""


def _event_text(attrs, name):
    """非门锁事件念什么：优先 HA 的 event_type（「门铃响了」），否则实体名。

    实体名往往带厂商和分组前缀（「亚拉锁 可视门铃 门铃响了」），念出来像报设备
    型号；event_type 才是这件事本身。
    """
    label = str((attrs or {}).get("event_type") or "").strip()
    if label and _CJK_RE.search(label):
        return label
    return name


class HomeWatcher:
    def __init__(self, devices, store, filter_percent=None, cooldown_hours=None,
                 event_ids=None, event_cooldown_seconds=None, lock_users=None,
                 lock_cooldown_seconds=None):
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
        self.lock_users = (
            parse_lock_users(HOME_LOCK_USERS) if lock_users is None else dict(lock_users)
        )
        # 门锁事件默认不冷却：10 分钟里进来两个人，第二个也得报
        self.lock_cooldown_seconds = (
            HOME_LOCK_COOLDOWN_SECONDS
            if lock_cooldown_seconds is None
            else lock_cooldown_seconds
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
            elif _is_door_state_sensor(row):
                # 门状态不是事件：它是持续状态（已上锁/已开锁/门未关/门虚掩），
                # 所以只在值变化时判断，不设时间冷却——门关好了又虚掩，第二次也得报。
                seen = self.store.get_home_alert("door", entity_id)
                changed = not seen or seen.get("last_value") != raw
                if not changed:
                    continue
                if apply:
                    self.store.record_home_alert(
                        "door", entity_id, raw, now.isoformat(),
                    )
                text = _door_alert_text(raw)
                if text:
                    out.append({
                        "kind": "door",
                        "entity_id": entity_id,
                        "tag": "door-%s" % entity_id.split(".")[-1],
                        "text": text,
                    })
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
                entity_id, raw, attrs.get("friendly_name") or entity_id,
                attrs=attrs, now=now, apply=apply,
            )
            if alert:
                out.append(alert)
        return out

    def annotate_event(self, entity_id, raw, name, attrs=None, now=None, apply=True):
        """一条事件该不该播报。WS 推送和轮询共用这一套判定，避免推两遍。

        两种通道都会调到这里，所以去重状态（`home_alerts`）是唯一裁判：
        先到的记下 last_value，后到的看到值一样就跳过。

        门锁事件带上「谁、用什么、做了什么」，其余事件还是念实体名。
        """
        now = now or datetime.now()
        if not _ISO_RE.match(str(raw or "")):
            return None
        text = describe_lock_event(attrs, self.lock_users)
        if text is None:
            text = describe_lock_exception(attrs)
        if text == "":
            # 看懂了但不用播报（关门后的自动上锁）：记下来，下次别当新事件
            if apply:
                self.store.record_home_alert("event", entity_id, str(raw), now.isoformat())
            return None
        # 门锁的每一条都是「谁进出了」，不能被 10 分钟的事件冷却吞掉第二个人；
        # 摄像头那种高频事件才需要冷却。
        cooldown = (
            self.lock_cooldown_seconds if text is not None
            else self.event_cooldown_seconds
        )
        with self._event_lock:
            seen = self.store.get_home_alert("event", entity_id)
            if seen and seen.get("last_value") == str(raw):
                return None
            if not seen and not _is_fresh(raw, now):
                # 第一次看到的是旧事件：只记下来，别当新闻推
                if apply:
                    self.store.touch_home_alert("event", entity_id, raw)
                return None
            if not self._cooled(seen, now, cooldown):
                if apply:
                    self.store.touch_home_alert("event", entity_id, raw)
                return None
            if apply:
                self.store.record_home_alert("event", entity_id, str(raw), now.isoformat())
        return {
            "kind": "event",
            "entity_id": entity_id,
            "tag": "event-%s" % entity_id.split(".")[-1],
            "text": text if text is not None else _event_text(attrs, name),
        }


def build_home_watch(home, store):
    """只有接了设备网关才建巡检器。"""
    if home is None:
        return None
    return HomeWatcher(home, store)
