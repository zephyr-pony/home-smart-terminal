"""固定评测集：先量正确率，再谈延迟和优化。

为什么单独成脚本：`pytest` 用假 LLM，只能证明接线正确；这个脚本打真模型、走完整
agent loop，回答的是「用户那句话，小马到底做对没有」。慢（一句 3–8 秒）且有网络
依赖，所以不进 pytest，需要时手动跑。

  用法：python scripts/eval_accuracy.py
        python scripts/eval_accuracy.py --only 记忆   只跑名字里含「记忆」的用例

判据只卡「做对没有」，不卡措辞：类型、调用到的工具、库里的状态、以及回复里必须
出现/不能出现的关键事实（日期、名字、号码）。措辞好坏靠人看最后一列。

每次用例都用全新的内存库，互不影响。
"""
import argparse
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.clarify import Clarifier
from assistant import Assistant
from domain.facts import Facts
from domain.store import AgentStore
from domain.tasks import Tasks
from items import Items
from llm import LLM
from memory import Memory

TODAY = date.today()
TOMORROW = TODAY + timedelta(days=1)
DAY_AFTER = TODAY + timedelta(days=2)
NEXT_WEEK_WED = TODAY + timedelta(days=7 + (2 - TODAY.weekday()) % 7)


CASES = []


def case(name, text, setup=None, tools=None, any_tools=None, type=None,
         has=(), lacks=(), check=None, known=False):
    CASES.append({
        "name": name, "text": text, "setup": setup, "tools": set(tools or ()),
        "any_tools": set(any_tools or ()), "type": type,
        "has": has, "lacks": lacks, "check": check, "known": known,
    })


def build():
    store = AgentStore(":memory:")
    items = Items(":memory:")
    return Assistant(
        memory=Memory(":memory:"), llm=LLM(), items=items,
        clarifier=Clarifier(store=store, items=items),
        tasks=Tasks(":memory:"), facts=Facts(":memory:"),
    )


def _count(attr):
    def check(assistant, _result):
        actual = getattr(assistant, attr).count()
        return actual == 1, "%s 里 %d 条（应为 1）" % (attr, actual)
    return check


def _task_due(expected):
    def check(assistant, _result):
        rows = assistant.tasks.list_all() or []
        if not rows:
            return False, "任务表是空的"
        due = str(rows[0].get("due_at") or "")
        return due == expected, "due_at=%s（应为 %s）" % (due, expected)
    return check


def _item_expiry(expected):
    def check(assistant, _result):
        rows = assistant.items.list_all() or []
        if not rows:
            return False, "物品表是空的"
        expiry = str(rows[0].get("expiry_date") or "")
        return expiry == expected, "expiry=%s（应为 %s）" % (expiry, expected)
    return check


def _fact_value():
    def check(assistant, _result):
        rows = assistant.facts.list_all() or []
        if not rows:
            return False, "事实表是空的"
        return True, "value=%s" % rows[0].get("value")
    return check


def _task_status(expected):
    def check(assistant, _result):
        rows = assistant.tasks.list_all() or []
        if not rows:
            return False, "任务表是空的"
        status = str(rows[0].get("status") or "")
        return status == expected, "status=%s（应为 %s）" % (status, expected)
    return check


def seed_memory(assistant):
    assistant.memory.store("牛奶快喝完了", "牛奶快喝完了")


def seed_item(assistant):
    assistant.items.add("牛奶", TOMORROW.isoformat())


def seed_task(assistant):
    assistant.tasks.add("交电费", (TODAY + timedelta(days=1)).isoformat() + "T15:00:00")


def seed_task_dated(assistant):
    assistant.tasks.add("取快递", TOMORROW.isoformat() + "T15:00:00")


def seed_fact(assistant):
    assistant.clarifier.store.add_entity("property", "自己家")
    # 按真实写入路径落库的键：澄清会把「自己家」并进句子，模型存成
    # subject=自己家 / predicate=水费户号
    assistant.facts.store("自己家", "水费户号", "12345678")


def seed_fact_bare_subject(assistant):
    """反例：主体没带家庭名、属性和事情分开存，和查询时的说法对不上。"""
    assistant.clarifier.store.add_entity("property", "自己家")
    assistant.facts.store("水费", "户号", "12345678")


def seed_memory_egg(assistant):
    assistant.memory.store("冰箱里有三个鸡蛋", "冰箱里有三个鸡蛋")


# ---------- 闲聊 ----------
case("闲聊：打招呼", "你好小马", type="chat", lacks=("已记好", "已删除"))
case("闲聊：报时间", "现在几点了", type="chat", lacks=("已记好",))

# ---------- 记忆 ----------
case("记忆：写入并落库", "记一下，冰箱里还有三个鸡蛋",
     tools=("memory_store",), type="record", check=_count("memory"))
case("记忆：查询命中", "我记了哪些东西",
     setup=seed_memory, any_tools=("family_search", "memory_search", "memory_list"),
     has=("牛奶",))
case("记忆：摘要不编日期", "记一下，备用钥匙在鞋柜第二层",
     tools=("memory_store",), type="record",
     check=lambda a, r: (
         "月" not in str((a.memory.list_all() or [{}])[0].get("summary") or ""),
         "摘要=%s" % (a.memory.list_all() or [{}])[0].get("summary"),
     ))
case("记忆：删除要先确认", "把鸡蛋那条记忆删掉", setup=seed_memory_egg,
     type="confirm")

# ---------- 物品 ----------
case("物品：写入带到期日", "牛奶下周三过期", tools=("item_add",),
     check=_item_expiry(NEXT_WEEK_WED.isoformat()),
     has=("%d月%d日" % (NEXT_WEEK_WED.month, NEXT_WEEK_WED.day),))
case("物品：清单", "我记了哪些东西", setup=seed_item,
     any_tools=("family_search", "item_list", "item_search"), has=("牛奶",))
case("物品：不带到期日不瞎猜", "冰箱里放了一盒酸奶",
     any_tools=("item_add", "memory_store"), type=None,
     check=lambda a, r: (
         all(not str(row.get("expiry_date") or "").strip()
             for row in (a.items.list_all() or [])),
         "物品表里有编出来的到期日",
     ),
     lacks=("过期了",))

# ---------- 任务 / 提醒 ----------
case("提醒：明天下午三点", "明天下午三点提醒我取快递",
     tools=("task_add",), type="record",
     check=_task_due(TOMORROW.isoformat() + "T15:00:00"),
     has=("下午3点",))
case("提醒：清单", "我有哪些提醒", setup=seed_task,
     tools=("task_list",), has=("交电费",))
case("提醒：说完成", "电费交了", setup=seed_task,
     tools=("task_complete",), check=_task_status("done"))
case("提醒：改到后天保留时刻", "取快递改到后天", setup=seed_task_dated,
     any_tools=("task_postpone", "task_update"),
     check=_task_due(DAY_AFTER.isoformat() + "T15:00:00"))
case("提醒：补一句延后", "取快递晚一天", setup=seed_task_dated,
     tools=("task_postpone",), has=("取快递",))

# ---------- 事实 ----------
case("事实：存户号", "水费户号是12345678",
     setup=lambda a: a.clarifier.store.add_entity("property", "自己家"),
     tools=("fact_store",), type="record", has=("12345678",),
     check=_fact_value())
case("事实：改品牌说清旧值", "牛奶现在是伊利的",
     setup=lambda a: a.facts.store("牛奶", "品牌", "蒙牛"),
     tools=("fact_store",), type="record", has=("伊利",),
     check=lambda a, r: (r.get("type") == "record", "type=%s" % r.get("type")))
case("事实：查品牌", "牛奶是什么牌子的",
     setup=lambda a: a.facts.store("牛奶", "品牌", "蒙牛"),
     any_tools=("fact_search", "fact_list"), has=("蒙牛",))
# 已知问题：事实的键和查询都是自由文本，`Facts.search` 用整串 LIKE 匹配，
# 「水费户号」既不是 subject「水费」也不是 predicate「户号」的连续子串，查不到。
# 正常写入路径（带实体澄清）键能对齐；这条盯的是键一旦对不上就没有兜底（D46）。
case("事实：查户号（键对不上，已知问题）", "水费户号是多少",
     setup=seed_fact_bare_subject,
     any_tools=("fact_search", "fact_list"), has=("12345678",), known=True)

# ---------- 家居（只读） ----------
case("家居：查询开关状态", "客厅灯开着吗", tools=("home_query",),
     lacks=("已打开", "已关闭"))

# ---------- 识别错字（ASR 同音，文字层同样暴露） ----------
case("同音：寄/记", "我寄了哪些东西",
     any_tools=("family_search", "memory_search", "memory_list"),
     has=("你要查的是哪件事",), lacks=("已记好",))


def run(only=None):
    passed = 0
    failed = []
    known_failed = []
    for item in CASES:
        if only and only not in item["name"]:
            continue
        assistant = build()
        try:
            if item["setup"]:
                item["setup"](assistant)
            result = assistant.process_text(item["text"], "eval")
            reply = str(result.get("reply") or "")
            used = set(result.get("used_tools") or [])
            problems = []
            if item["type"] and result.get("type") != item["type"]:
                problems.append("类型 %s（应为 %s）" % (result.get("type"), item["type"]))
            if item["tools"] and not item["tools"] <= used:
                problems.append("工具 %s（缺 %s）" % (
                    ",".join(sorted(used)) or "无",
                    ",".join(sorted(item["tools"] - used)),
                ))
            if item["any_tools"] and not (item["any_tools"] & used):
                problems.append("工具 %s（应为 %s 之一）" % (
                    ",".join(sorted(used)) or "无",
                    "/".join(sorted(item["any_tools"])),
                ))
            for needle in item["has"]:
                if needle not in reply:
                    problems.append("回复缺「%s」" % needle)
            for needle in item["lacks"]:
                if needle in reply:
                    problems.append("回复不该有「%s」" % needle)
            if item["check"]:
                ok, detail = item["check"](assistant, result)
                if not ok:
                    problems.append(detail)
        finally:
            assistant.close()

        line = "%-22s %s" % (item["name"], reply[:56])
        if not problems:
            passed += 1
            print("✅ " + line)
        elif item["known"]:
            known_failed.append((item["name"], problems))
            print("⚠️  " + line)
            print("     已知问题：" + "；".join(problems))
        else:
            failed.append((item["name"], problems, reply))
            print("❌ " + line)
            print("     " + "；".join(problems))

    total = passed + len(failed) + len(known_failed)
    print("\n通过 %d/%d" % (passed, total))
    if known_failed:
        print("已知问题 %d 条（不计入失败）：%s" % (
            len(known_failed), "、".join(name for name, _ in known_failed),
        ))
    return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default=None, help="只跑名字里含这个词的用例")
    args = parser.parse_args()
    sys.exit(run(args.only))
