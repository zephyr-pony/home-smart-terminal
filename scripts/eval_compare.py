"""对比两次评测结果（`eval_accuracy.py --json` 的产物）。

为什么单独一个模块：`eval_accuracy.py` 要打真模型、几分钟，且每次结果有抖动；
「这次改工具描述到底有没有把某一类改坏」是纯计算，应该能进 pytest、也该能对着
两次历史结果比对（换模型时尤其有用）。

判据只有一条：**基线通过、这次失败**才算回归。已知问题本来就不计分，
它掉了不拦人，但照样列出来。
"""
CATEGORY_SEP = "："


def category_of(name):
    """用例名是「类目：说什么」；没有冒号的归到「其它」。"""
    name = str(name or "")
    if CATEGORY_SEP in name:
        return name.split(CATEGORY_SEP, 1)[0].strip() or "其它"
    return "其它"


def summarize(cases):
    """计分口径：已知问题（known）永远不算进分母。

    以前 `total` 把 known 也算进去，于是「通过 41/43」和「通过 41/41」两种说法
    同时存在——同一个结果两个分母，看的人先得猜是哪种。
    """
    passed = failed = known_passed = known_failed = 0
    seconds = []
    for item in cases or []:
        ok = bool(item.get("ok"))
        if item.get("known"):
            if ok:
                known_passed += 1
            else:
                known_failed += 1
        elif ok:
            passed += 1
        else:
            failed += 1
        try:
            seconds.append(float(item.get("seconds") or 0))
        except (TypeError, ValueError):
            pass
    return {
        "total": len(cases or []),
        "passed": passed,
        "failed": failed,
        "known_passed": known_passed,
        "known_failed": known_failed,
        "scored": passed + failed,
        "avg_seconds": round(sum(seconds) / len(seconds), 1) if seconds else 0.0,
    }


def load(path):
    """读一份结果文件；格式不对时给人话，不抛 KeyError。"""
    import json

    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        raise ValueError("%s 不是评测结果（缺 cases）" % path)
    return data


def _by_name(data):
    return {
        str(item.get("name")): item
        for item in data.get("cases") or []
        if isinstance(item, dict)
    }


def _ok(item):
    return bool(item and item.get("ok"))


def compare(prev, cur):
    """逐条比。返回回归 / 修好 / 仍失败 / 增删 / 分类变化。"""
    before, after = _by_name(prev), _by_name(cur)
    regressions, fixed, still_failing = [], [], []
    for name, item in after.items():
        old = before.get(name)
        if old is None:
            continue
        was, now = _ok(old), _ok(item)
        if was and not now:
            regressions.append({
                "name": name,
                "known": bool(item.get("known")),
                "problems": list(item.get("problems") or []),
            })
        elif not was and now:
            fixed.append(name)
        elif not now:
            still_failing.append(name)
    return {
        "regressions": regressions,
        "fixed": fixed,
        "still_failing": still_failing,
        "added": [name for name in after if name not in before],
        "dropped": [name for name in before if name not in after],
        "before": summarize(prev.get("cases")),
        "after": summarize(cur.get("cases")),
        "categories": _categories(prev, cur),
    }


def blocking_regressions(comparison):
    """真正该拦人的回归：已知问题掉了不算（它本来就不计分）。"""
    return [item for item in comparison.get("regressions") or []
            if not item.get("known")]


def _categories(prev, cur):
    """按类目算通过率变化；只知道总数掉了多少没用，得知道是哪一类掉了。"""
    rows = {}

    def add(label, data):
        for item in data.get("cases") or []:
            if item.get("known"):
                continue
            row = rows.setdefault(category_of(item.get("name")), {
                "before_passed": 0, "before_total": 0,
                "after_passed": 0, "after_total": 0,
            })
            row[label + "_total"] += 1
            if item.get("ok"):
                row[label + "_passed"] += 1

    add("before", prev)
    add("after", cur)
    return rows


def _model_of(data):
    return str(data.get("model") or "未知模型")


def _when_of(data):
    return str(data.get("started_at") or "").replace("T", " ")[:16]


def format_report(prev, cur, comparison):
    """给人看的几行。只列有变化的东西，别把没动的类目也念一遍。"""
    before, after = comparison["before"], comparison["after"]
    lines = ["对比基线（%s · %s）" % (_when_of(prev) or "?", _model_of(prev))]
    lines.append("  本次（%s · %s）" % (_when_of(cur) or "?", _model_of(cur)))
    lines.append("  计分通过 %d/%d → %d/%d（%+d）" % (
        before["passed"], before["scored"],
        after["passed"], after["scored"],
        after["passed"] - before["passed"],
    ))

    blocking = blocking_regressions(comparison)
    known_breaks = [item for item in comparison["regressions"]
                    if item.get("known")]
    if blocking:
        lines.append("  回归 %d 条：" % len(blocking))
        for item in blocking:
            detail = "；".join(item.get("problems") or [])[:110]
            lines.append("    ❌ %s%s" % (item["name"], ("：" + detail) if detail else ""))
    if known_breaks:
        lines.append("  已知问题掉了 %d 条（不计分）：%s" % (
            len(known_breaks), "、".join(item["name"] for item in known_breaks),
        ))
    if comparison["fixed"]:
        lines.append("  修好 %d 条：%s" % (
            len(comparison["fixed"]), "、".join(comparison["fixed"]),
        ))
    if comparison["still_failing"]:
        lines.append("  仍失败 %d 条：%s" % (
            len(comparison["still_failing"]),
            "、".join(comparison["still_failing"]),
        ))
    if comparison["added"]:
        lines.append("  新增用例 %d 条：%s" % (
            len(comparison["added"]), "、".join(comparison["added"]),
        ))
    if comparison["dropped"]:
        lines.append("  少了用例 %d 条（基线有、这次没跑）：%s" % (
            len(comparison["dropped"]), "、".join(comparison["dropped"]),
        ))

    changed = []
    for category, row in sorted(comparison["categories"].items()):
        before_rate = (row["before_passed"], row["before_total"])
        after_rate = (row["after_passed"], row["after_total"])
        if before_rate != after_rate:
            changed.append("%s %d/%d → %d/%d" % (
                category, before_rate[0], before_rate[1],
                after_rate[0], after_rate[1],
            ))
    if changed:
        lines.append("  按类：")
        for line in changed:
            lines.append("    " + line)

    if not blocking and not comparison["fixed"]:
        lines.append("  没有回归。")
    return lines
