"""看一眼 XIAOMA_IN 的结构：sheet、列名、行数、每列的类型和空值。

表格整理的起点。改这份脚本比从零写快，也不用猜列名。
"""
import os

import pandas as pd

src = os.environ.get("XIAOMA_IN", "")
out = os.environ.get("XIAOMA_OUT", "")
if not src:
    raise SystemExit("没有输入文件：这一轮要先上传表格")

sheets = pd.read_excel(src, sheet_name=None) if src.endswith((".xlsx", ".xls")) \
    else {"csv": pd.read_csv(src)}

for sheet, frame in sheets.items():
    print("== %s：%d 行 x %d 列" % (sheet, len(frame), len(frame.columns)))
    for column in frame.columns:
        series = frame[column]
        blanks = int(series.isna().sum())
        kinds = series.dropna().map(lambda v: type(v).__name__).unique().tolist()
        sample = series.dropna().head(2).tolist()
        print("  %-16s %-14s 空 %-4d 样例 %s" % (column, ",".join(kinds) or "-", blanks, sample))

# 真正要处理时，把清洗后的结果写到 out：
#   frame.to_excel(out, index=False)
# 这里只探测，不产出文件。
print("\n输出路径：%s" % (out or "（这一轮不产出文件）"))
