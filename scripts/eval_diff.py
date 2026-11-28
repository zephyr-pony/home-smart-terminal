"""比两次评测结果，看哪一类掉了。

    python scripts/eval_accuracy.py --json data/eval/before.json
    # 改工具描述 / 换模型
    python scripts/eval_accuracy.py --json data/eval/after.json
    python scripts/eval_diff.py data/eval/before.json data/eval/after.json

只把「基线通过、这次失败」算回归（已知问题本来就不计分）。退出码 1 表示有回归，
可以直接接进脚本，不用人眼看数字。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import eval_compare  # noqa: E402  同目录


def main(argv=None):
    parser = argparse.ArgumentParser(description="对比两次评测结果")
    parser.add_argument("baseline", help="基线 JSON（eval_accuracy.py --json 的产物）")
    parser.add_argument("current", help="这次的 JSON")
    args = parser.parse_args(argv)

    try:
        previous = eval_compare.load(args.baseline)
        current = eval_compare.load(args.current)
    except (OSError, ValueError) as exc:
        print("读不了结果：%s" % exc)
        return 2

    comparison = eval_compare.compare(previous, current)
    for line in eval_compare.format_report(previous, current, comparison):
        print(line)
    return 1 if eval_compare.blocking_regressions(comparison) else 0


if __name__ == "__main__":
    sys.exit(main())
