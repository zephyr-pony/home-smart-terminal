"""评测结果对比（`scripts/eval_compare.py`）：纯计算，不打模型。"""
import importlib.util
import json
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "eval_compare.py"


def _load():
    spec = importlib.util.spec_from_file_location("xiaoma_eval_compare", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


eval_compare = _load()


def _case(name, ok, known=False, problems=(), seconds=1.0):
    return {
        "name": name, "ok": ok, "known": known,
        "problems": list(problems), "seconds": seconds,
    }


def _result(cases, model="m1", started_at="2026-11-28T10:00:00"):
    return {
        "version": 1, "started_at": started_at, "model": model,
        "cases": cases, "summary": eval_compare.summarize(cases),
    }


def test_known_cases_stay_out_of_the_denominator():
    summary = eval_compare.summarize([
        _case("记忆：a", True),
        _case("记忆：b", False),
        _case("记忆：c", False, known=True),
        _case("记忆：d", True, known=True),
    ])
    assert summary["scored"] == 2
    assert (summary["passed"], summary["failed"]) == (1, 1)
    assert (summary["known_passed"], summary["known_failed"]) == (1, 1)
    assert summary["total"] == 4


def test_all_known_suite_has_no_scored_cases():
    summary = eval_compare.summarize([_case("提醒：x", False, known=True)])
    assert summary["scored"] == 0
    assert summary["known_failed"] == 1


def test_category_is_the_prefix_before_the_colon():
    assert eval_compare.category_of("提醒：改到后天保留时刻") == "提醒"
    assert eval_compare.category_of("没有冒号") == "其它"
    assert eval_compare.category_of("") == "其它"


def test_compare_finds_regression_and_fix():
    prev = _result([_case("提醒：a", True), _case("提醒：b", False)])
    cur = _result([_case("提醒：a", False, problems=["工具缺 task_add"]),
                   _case("提醒：b", True)])

    comparison = eval_compare.compare(prev, cur)

    assert [item["name"] for item in comparison["regressions"]] == ["提醒：a"]
    assert comparison["regressions"][0]["problems"] == ["工具缺 task_add"]
    assert comparison["fixed"] == ["提醒：b"]
    assert comparison["still_failing"] == []
    assert eval_compare.blocking_regressions(comparison)


def test_known_regression_does_not_block():
    prev = _result([_case("记忆：a", True, known=True)])
    cur = _result([_case("记忆：a", False, known=True)])

    comparison = eval_compare.compare(prev, cur)

    assert len(comparison["regressions"]) == 1
    assert eval_compare.blocking_regressions(comparison) == []


def test_added_and_dropped_cases_are_reported():
    prev = _result([_case("记忆：老", True)])
    cur = _result([_case("记忆：新", True)])

    comparison = eval_compare.compare(prev, cur)

    assert comparison["added"] == ["记忆：新"]
    assert comparison["dropped"] == ["记忆：老"]
    assert comparison["regressions"] == []


def test_report_shows_the_delta_and_the_category():
    prev = _result([_case("提醒：a", True), _case("记忆：b", True)])
    cur = _result([_case("提醒：a", False, problems=["类型 confirm（应为 record）"]),
                   _case("记忆：b", True)])

    lines = "\n".join(eval_compare.format_report(
        prev, cur, eval_compare.compare(prev, cur)))

    assert "回归 1 条" in lines
    assert "提醒：a" in lines
    assert "类型 confirm" in lines
    assert "提醒 1/1 → 0/1" in lines
    assert "记忆 1/1 → 1/1" not in lines  # 没变的不念
    assert "计分通过 2/2 → 1/2（-1）" in lines


def test_report_says_no_regression_when_nothing_changed():
    data = _result([_case("记忆：a", True)])
    lines = "\n".join(eval_compare.format_report(
        data, data, eval_compare.compare(data, data)))
    assert "没有回归" in lines


def test_load_rejects_a_file_without_cases(tmp_path):
    path = tmp_path / "nope.json"
    path.write_text(json.dumps({"hello": 1}), encoding="utf-8")
    try:
        eval_compare.load(str(path))
    except ValueError as exc:
        assert "cases" in str(exc)
    else:
        raise AssertionError("应该报错")


def test_load_accepts_a_real_result_file(tmp_path):
    path = tmp_path / "ok.json"
    path.write_text(json.dumps(_result([_case("记忆：a", True)])), encoding="utf-8")
    loaded = eval_compare.load(str(path))
    assert loaded["cases"][0]["name"] == "记忆：a"
