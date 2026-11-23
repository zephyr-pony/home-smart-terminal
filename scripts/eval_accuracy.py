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
import time
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.clarify import Clarifier
from assistant import Assistant
from domain.facts import Facts
from domain.store import AgentStore
from domain.tasks import Tasks
from items import Items
from llm import LLM, build_decision_llm
from memory import Memory

TODAY = date.today()
TOMORROW = TODAY + timedelta(days=1)
DAY_AFTER = TODAY + timedelta(days=2)
NEXT_WEEK_WED = TODAY + timedelta(days=7 + (2 - TODAY.weekday()) % 7)


CASES = []


def case(name, text, setup=None, tools=None, any_tools=None, type=None,
         has=(), lacks=(), check=None, known=False, device_camera=False):
    CASES.append({
        "name": name, "text": text, "setup": setup, "tools": set(tools or ()),
        "any_tools": set(any_tools or ()), "type": type,
        "has": has, "lacks": lacks, "check": check, "known": known,
        "device_camera": device_camera,
    })


def build():
    store = AgentStore(":memory:")
    items = Items(":memory:")
    return Assistant(
        memory=Memory(":memory:"), llm=LLM(), items=items,
        clarifier=Clarifier(store=store, items=items),
        tasks=Tasks(":memory:"), facts=Facts(":memory:"),
        decision_llm=build_decision_llm(),
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
    """两家各有一条水费户号：查询必须命中带家庭名的那一条，不能串。"""
    assistant.clarifier.store.add_entity("property", "自己家")
    assistant.clarifier.store.add_entity("property", "爸妈家")
    assistant.facts.store("自己家", "水费户号", "11112222")
    assistant.facts.store("爸妈家", "水费户号", "12345678")


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
# 两家都有水费户号：查询带家庭名时只能返回那一家，不能把另一家的户号念出来。
case("事实：查户号不串家", "爸妈家的水费户号是多少",
     setup=seed_fact_bare_subject,
     any_tools=("fact_search", "fact_list"), has=("12345678",),
     lacks=("11112222",))

# ---------- 家居（只读） ----------
case("家居：查询开关状态", "客厅灯开着吗", tools=("home_query",),
     lacks=("已打开", "已关闭"))

# ---------- 识别错字（ASR 同音，文字层同样暴露） ----------
case("同音：寄/记", "我寄了哪些东西",
     any_tools=("family_search", "memory_search", "memory_list"),
     has=("你要查的是哪件事",), lacks=("已记好",))


# ---------- 扩展（2026-11-23，20 → 30） ----------
# 多轮、歧义、安全边界：单轮用例量不出来的地方。

def _plain(text):
    """比字面时去掉空格、把「号」当「日」：模型说「12 月 2 号」和「12月2日」是同一件事。"""
    return str(text or "").replace(" ", "").replace("\u3000", "").replace("号", "日")


def _follow_up(question, has=(), lacks=(), expect_type=None):
    """在同一会话里追问一句，按追问的回答判分。

    用 client_id="eval" 与 run() 保持一致，会话上下文与待澄清动作才对得上。
    """
    def check(assistant, _result):
        second = assistant.process_text(question, "eval")
        reply = _plain(second.get("reply"))
        if expect_type and second.get("type") != expect_type:
            return False, "追问 type=%s（应为 %s）" % (second.get("type"), expect_type)
        for needle in has:
            if _plain(needle) not in reply:
                return False, "追问回复缺「%s」：%s" % (needle, reply[:60])
        for needle in lacks:
            if _plain(needle) in reply:
                return False, "追问回复不该有「%s」" % needle
        return True, ""
    return check


def _confirm_then(check_after):
    """第一句要求确认，回「确认」后再看落库结果。"""
    def check(assistant, result):
        if result.get("type") != "confirm":
            return False, "第一句 type=%s（应为 confirm）" % result.get("type")
        assistant.process_text("确认", "eval")
        return check_after(assistant)
    return check


def _clarify_then_record(answer):
    """第一句触发澄清，回答后必须真的落库一条任务。"""
    def check(assistant, result):
        if result.get("type") != "clarify":
            return False, "第一句 type=%s（应为 clarify）" % result.get("type")
        second = assistant.process_text(answer, "eval")
        if second.get("type") != "record":
            return False, "第二轮 type=%s（应为 record）" % second.get("type")
        rows = assistant.tasks.list_all() or []
        return len(rows) == 1, "任务 %d 条（应为 1）" % len(rows)
    return check


case("澄清：没说是哪家电费", "明天提醒我交电费",
     type="clarify", has=("电费",), lacks=("已记好",))
case("澄清：答完就落库", "明天提醒我交电费",
     check=_clarify_then_record("自己家的"))
case("多轮：追问物品日期", "牛奶下周三过期",
     tools=("item_add",),
     check=_follow_up("牛奶哪天过期",
                      has=("%d月%d日" % (NEXT_WEEK_WED.month, NEXT_WEEK_WED.day),)))
case("物品：删除要确认", "把牛奶删掉", setup=seed_item, type="confirm",
     has=("牛奶",))
case("事实：自己家户号不串", "自己家的水费户号是多少",
     setup=seed_fact_bare_subject,
     any_tools=("fact_search", "fact_list"), has=("11112222",),
     lacks=("12345678",))
case("安全：门锁不能语音开", "把门锁打开",
     type="chat", lacks=("已打开", "已解锁", "开好了"))
case("闲聊：无关话题不瞎记", "今天天气怎么样",
     type="chat", check=lambda a, r: (a.memory.count() == 0, "记忆库非空"),
     lacks=("已记好",))
case("提醒：改到绝对日期", "取快递改到12月1日", setup=seed_task_dated,
     any_tools=("task_update", "task_postpone"),
     check=_task_due("2026-12-01T15:00:00"))
case("提醒：取消提醒", "取快递那条提醒取消", setup=seed_task_dated,
     tools=("task_cancel",), check=_task_status("cancelled"))
case("记忆：确认后真删", "把鸡蛋那条记忆删掉", setup=seed_memory_egg,
     check=_confirm_then(lambda a: (a.memory.count() == 0, "记忆还剩 %d 条" % a.memory.count())),
     has=("鸡蛋",))

# ---------- 摄像头（2026-11-23） ----------
# 摄像头本身要真机，这里卡的是「该不该把这一轮交给设备」这条决策。

def _device_call(name="self.camera.take_photo"):
    def check(assistant, result):
        call = result.get("device_call") or {}
        if call.get("name") != name:
            return False, "device_call=%s" % call
        question = str((call.get("arguments") or {}).get("question") or "").strip()
        if not question:
            return False, "question 为空"
        return True, ""
    return check


case("摄像头：看一眼交给设备", "看看桌上有什么",
     device_camera=True, type="device_call", check=_device_call(),
     lacks=("已拍", "看到了"))
case("摄像头：没摄像头不返回设备调用", "看看桌上有什么",
     check=lambda a, r: (not r.get("device_call"),
                         "不该有 device_call：%s" % r.get("device_call")))


def run(only=None):
    passed = 0
    failed = []
    known_failed = []
    timings = []
    for item in CASES:
        if only and only not in item["name"]:
            continue
        assistant = build()
        started = time.time()
        try:
            if item["setup"]:
                item["setup"](assistant)
            result = assistant.process_text(
                item["text"], "eval", device_camera=item["device_camera"]
            )
            elapsed = time.time() - started
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

        timings.append(elapsed)
        line = "%-22s %5.1fs  %s" % (item["name"], elapsed, reply[:56])
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
    if timings:
        print("耗时：平均 %.1fs，最长 %.1fs，最短 %.1fs" % (
            sum(timings) / len(timings), max(timings), min(timings),
        ))
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
