"""农历、节气与传统节日（D72）。

为什么用 cnlunar：农历要有 1900–2099 的年月表，自己抄表抄错一位就整天错；
cnlunar 是纯 Python（约 50 KB）、离线、MIT，只依赖标准库，Python 3.9 可用。
没装（或以后换成别家）时整个能力降级，不影响其它工具——import 时兜住。

这里只做「换算与事实」，不生成话术：今天初几、哪天立春、某个农历日子是公历几号。
"""
import datetime
import re

try:
    import cnlunar
except ImportError:  # 只影响农历工具，别让整个服务起不来
    cnlunar = None

# cnlunar 的表覆盖 1900–2099；边界年份容易踩到表尾，收窄一点
MIN_YEAR = 1901
MAX_YEAR = 2099

# 二十四节气（cnlunar 的 thisYearSolarTermsDic 就这 24 个键）
SOLAR_TERMS = (
    "小寒", "大寒", "立春", "雨水", "惊蛰", "春分", "清明", "谷雨",
    "立夏", "小满", "芒种", "夏至", "小暑", "大暑", "立秋", "处暑",
    "白露", "秋分", "寒露", "霜降", "立冬", "小雪", "大雪", "冬至",
)

# 只留家里真会问的节日。cnlunar 同一天会顺带给出「弥勒佛圣诞」「酆都大帝诞」
# 这类，念出来是噪音，白名单过滤掉。
FESTIVALS = frozenset((
    "春节", "元宵节", "龙抬头", "上巳节", "寒食节", "清明节", "端午节",
    "七夕", "七夕节", "中元节", "中秋节", "重阳节", "腊八节", "小年", "除夕",
    "元旦", "妇女节", "劳动节", "儿童节", "教师节", "国庆节",
    "母亲节", "父亲节",
))

_SPLIT_RE = re.compile(r"[\s\-—+、；;]+")


def available():
    """农历能力是否可用（没装 cnlunar 就 False，调用方据此决定要不要给模型）。"""
    return cnlunar is not None


def _raw(when):
    """统一的入口：把 date/datetime 变成 cnlunar.Lunar，越界和异常转成 ValueError。"""
    if cnlunar is None:
        raise ValueError("没有安装 cnlunar，农历不可用")
    if isinstance(when, datetime.datetime):
        moment = when
    else:
        moment = datetime.datetime(when.year, when.month, when.day)
    if not MIN_YEAR <= moment.year <= MAX_YEAR:
        raise ValueError("农历只支持 %d–%d 年" % (MIN_YEAR, MAX_YEAR))
    try:
        return cnlunar.Lunar(moment)
    except Exception as exc:  # 表尾/非法日期都从这里出，别把异常抛给模型
        raise ValueError("农历算不出来：%s" % exc)


def _names(value):
    """cnlunar 把多个节日用空格或 '-' 连在一起（'七夕-魁星诞'），拆开再过滤。"""
    return [part for part in _SPLIT_RE.split(str(value or "")) if part]


def day_info(when):
    """公历某天的农历信息。返回 dict，不知道的字段给空，别让调用方猜。"""
    lunar = _raw(when)
    month_text = re.sub(r"[大小]$", "", lunar.lunarMonthCn or "")

    festivals = []
    for value in (lunar.get_legalHolidays(), lunar.get_otherHolidays(),
                  lunar.get_otherLunarHolidays()):
        for name in _names(value):
            if name in FESTIVALS and name not in festivals:
                festivals.append(name)

    # cnlunar 不标除夕：第二天是正月初一，今天就是除夕
    if lunar.lunarMonth == 12:
        tomorrow = _raw(datetime.date(when.year, when.month, when.day)
                        + datetime.timedelta(days=1))
        if tomorrow.lunarMonth == 1 and tomorrow.lunarDay == 1:
            if "除夕" not in festivals:
                festivals.append("除夕")

    term = str(getattr(lunar, "todaySolarTerms", "") or "").strip()
    if term in ("", "无"):
        term = ""

    return {
        "lunar_year": lunar.lunarYear,
        "lunar_month": lunar.lunarMonth,
        "lunar_day": lunar.lunarDay,
        "leap": bool(lunar.isLunarLeapMonth),
        "month_text": month_text,
        "day_text": lunar.lunarDayCn or "",
        "ganzhi": lunar.year8Char or "",   # 丙午
        "zodiac": lunar.chineseYearZodiac or "",  # 马
        "term": term,
        "festivals": festivals,
    }


def new_year(lunar_year):
    """春节（正月初一）的公历日期。春节总落在 1/21–2/21，扫这个窗口就够。"""
    start = datetime.date(int(lunar_year), 1, 21)
    for offset in range(0, 33):
        when = start + datetime.timedelta(days=offset)
        info = _raw(when)
        if info.lunarYear == int(lunar_year) and info.lunarMonth == 1 \
                and info.lunarDay == 1:
            return when
    raise ValueError("找不到 %d 年的春节" % int(lunar_year))


def to_solar(lunar_year, month, day, leap=False):
    """农历 → 公历。

    一年 12 个月 + 可能的闰月，每月 29/30 天，所以按「月份起点 +29 天，
    不是初一就再 +1」走，最多十几轮就到位，比逐日扫一年快得多。
    """
    month = int(month)
    day = int(day)
    if not 1 <= month <= 12:
        raise ValueError("农历月份要在 1–12 之间")
    if not 1 <= day <= 30:
        raise ValueError("农历日期要在 1–30 之间")

    cursor = new_year(lunar_year)
    for _ in range(15):  # 12 个月 + 1 个闰月 + 余量
        info = _raw(cursor)
        if info.lunarMonth == month and bool(info.isLunarLeapMonth) == bool(leap):
            target = cursor + datetime.timedelta(days=day - 1)
            check = _raw(target)
            if check.lunarMonth != month or check.lunarDay != day:
                raise ValueError("%d 月没有第 %d 天" % (month, day))
            return target
        cursor = cursor + datetime.timedelta(days=29)
        if _raw(cursor).lunarDay != 1:
            cursor = cursor + datetime.timedelta(days=1)
    raise ValueError("找不到农历 %d 年 %d 月" % (int(lunar_year), month))


def solar_term(solar_year, name):
    """某公历年里某个节气的日期（如 2026 年立春 → 2 月 4 日）。"""
    name = str(name or "").strip()
    if name not in SOLAR_TERMS:
        raise ValueError("节气名要是这 24 个之一：%s" % "、".join(SOLAR_TERMS))
    table = _raw(datetime.date(int(solar_year), 6, 1)).thisYearSolarTermsDic
    if name not in table:
        raise ValueError("%d 年的节气表里没有 %s" % (int(solar_year), name))
    month, day = table[name]
    return datetime.date(int(solar_year), month, day)
