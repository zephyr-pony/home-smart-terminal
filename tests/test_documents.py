"""文件读写与通用执行器（D62）。

不依赖 LibreOffice / 网络：表格用 openpyxl 现场造，代码在临时目录里跑。
"""
import os

import pytest

from adapters import documents
from adapters.documents import DocumentError


def _make_xlsx(path):
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.title = "报价"
    sheet.append(["项目", "数量", "单价"])
    sheet.append(["螺丝", 10, 2.5])
    sheet.append(["螺母", 20, 1.5])
    book.save(path)


def test_read_xlsx_shows_sheets_columns_and_rows(tmp_path):
    path = tmp_path / "报价.xlsx"
    _make_xlsx(path)

    text = documents.read_document(str(path), "报价.xlsx")

    assert "【工作表】报价" in text
    assert "A=项目" in text and "B=数量" in text
    assert "螺丝" in text and "10" in text


def test_read_text_file(tmp_path):
    path = tmp_path / "note.md"
    path.write_text("# 标题\n内容一行", encoding="utf-8")

    text = documents.read_document(str(path), "note.md")
    assert "标题" in text and "内容一行" in text


def test_read_missing_file_raises(tmp_path):
    with pytest.raises(DocumentError):
        documents.read_document(str(tmp_path / "nope.txt"), "nope.txt")


def test_run_code_computes_and_writes_output(tmp_path):
    source = tmp_path / "data.txt"
    source.write_text("1 2 3", encoding="utf-8")
    code = (
        "import os\n"
        "numbers = [int(n) for n in open(os.environ['XIAOMA_IN']).read().split()]\n"
        "open(os.environ['XIAOMA_OUT'], 'w').write('sum=%d' % sum(numbers))\n"
    )

    result = documents.run_code(code, str(source), out_name="结果.txt")

    assert result["path"].endswith(".txt")
    with open(result["path"], encoding="utf-8") as fh:
        assert fh.read() == "sum=6"
    # 工作目录里是输入副本，原始文件不动
    assert os.path.exists(result["workdir"])
    assert source.read_text(encoding="utf-8") == "1 2 3"


def test_run_code_without_input_file(tmp_path):
    code = (
        "import os\n"
        "open(os.environ['XIAOMA_OUT'], 'w').write('generated')\n"
    )
    result = documents.run_code(code, None, out_name="out.txt")
    with open(result["path"], encoding="utf-8") as fh:
        assert fh.read() == "generated"


def test_run_code_accepts_output_with_custom_name(tmp_path):
    """模型没写 XIAOMA_OUT、自己起了名字：工作目录里的新文件也算产物。"""
    code = (
        "open('结果.xlsx', 'wb').write(b'PK\\x03\\x04 fake')\n"
    )
    result = documents.run_code(code, None, out_name="out.xlsx")
    assert result["path"].endswith("结果.xlsx")


def test_run_code_accepts_in_place_edit(tmp_path):
    """模型直接改输入文件并保存回原路径，也算产出。"""
    source = tmp_path / "data.txt"
    source.write_text("old", encoding="utf-8")
    code = (
        "import os, time\n"
        "time.sleep(0.05)\n"
        "open(os.environ['XIAOMA_IN'], 'w').write('new')\n"
    )
    result = documents.run_code(code, str(source), out_name="data.txt")
    with open(result["path"], encoding="utf-8") as fh:
        assert fh.read() == "new"


def test_run_code_failure_reports_stderr(tmp_path):
    code = "raise RuntimeError('boom')\n"
    with pytest.raises(DocumentError) as excinfo:
        documents.run_code(code, None, out_name="out.txt")
    assert "boom" in str(excinfo.value)


def test_run_code_directory_output_is_not_a_file(tmp_path):
    """把输出路径当目录用：不算产物，返回 path=None，打印目标不换。"""
    code = (
        "import os\n"
        "os.makedirs(os.environ['XIAOMA_OUT'], exist_ok=True)\n"
        "open(os.path.join(os.environ['XIAOMA_OUT'], 'a.txt'), 'w').write('x')\n"
    )
    result = documents.run_code(code, None, out_name="out.xlsx")
    assert result["path"] is None


def test_run_code_without_output_keeps_going(tmp_path):
    """只 print 不产出文件的探测代码是合法的：返回 path=None。"""
    result = documents.run_code("print('hello')", None, out_name="out.txt")
    assert result["path"] is None
    assert "hello" in result["stdout"]


def test_inventory_finds_python_packages():
    text = documents.inventory("openpyxl")
    assert "openpyxl" in text.lower()


def test_inventory_without_query_lists_tools():
    text = documents.inventory()
    assert "命令行工具" in text and "Python 包" in text
