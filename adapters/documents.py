"""读和处理用户上传的文件（D62）。

为什么要有这一层：
- 打印之前，模型需要先**看见**文件里有什么（哪个工作表、哪些列、多少行），
  否则「把两列相乘」「调列宽」这种要求它只能回「做不到」。
- 看见之后还要能改：给一个通用 Python 通道（openpyxl 等），产出新文件再打印。
  不按「缩放」「合并」一个个加参数——那是把用户的每个想法都写死在代码里。

安全边界：
- 代码在独立子进程里跑，工作目录是临时目录，只放一份输入文件副本；
- 环境变量清空（只剩 PATH/HOME/语言），不把 API Token、LLM 密钥带进去；
- 30 秒超时、1GB 地址空间、CPU 时间上限（非 Windows 生效）；
- 仍然不是真正的沙箱（容器里没有权限建网络命名空间），所以在文档里写清楚：
  这条通道的信任级别等同于「用户让模型在自己家里跑脚本」。
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

MAX_TEXT_CHARS = 6000
MAX_ROWS = 40
CODE_TIMEOUT_SECONDS = 30
CODE_MEMORY_BYTES = 1024 * 1024 * 1024


class DocumentError(RuntimeError):
    """读文件/跑代码失败；消息直接给用户看。"""


def _ext(name):
    return Path(str(name or "")).suffix.lower()


def _truncate(text, limit=MAX_TEXT_CHARS):
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…（已截断，共 %d 字）" % len(text)


# ---------- 读内容 ----------

def _read_table(path, max_rows=MAX_ROWS):
    """表格：返回工作表、尺寸、列字母和前几行，格式给模型看。"""
    source = path
    temp_dir = None
    if _ext(path) == ".xls":
        # openpyxl 只认 xlsx；老格式先用 LibreOffice 转一份
        from adapters.printer import office_converter, _run

        soffice = office_converter()
        if not soffice:
            raise DocumentError("服务器没装 LibreOffice，读不了 .xls，请另存为 .xlsx")
        temp_dir = tempfile.mkdtemp(prefix="xiaoma_read_")
        result = _run(
            [soffice, "--headless", "--norestore", "--convert-to", "xlsx",
             "--outdir", temp_dir, os.path.abspath(path)],
            timeout=120,
        )
        if result.returncode != 0:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise DocumentError("这个 .xls 转 xlsx 失败，换一份文件试试")
        source = os.path.join(temp_dir, Path(path).stem + ".xlsx")

    try:
        from openpyxl import load_workbook
        from openpyxl.utils import get_column_letter
    except ImportError:
        raise DocumentError("服务器没装 openpyxl，读不了表格；先另存为 PDF 或 CSV")

    try:
        book = load_workbook(source, data_only=True, read_only=True)
    except Exception as exc:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
        raise DocumentError("这个表格打不开：%s" % str(exc)[:120])

    parts = []
    try:
        for sheet in book.worksheets:
            rows = list(sheet.iter_rows(max_row=max_rows, values_only=True))
            width = max((len(row) for row in rows), default=0)
            parts.append(
                "【工作表】%s（%d 行 × %d 列，读到前 %d 行）"
                % (sheet.title, sheet.max_row, sheet.max_column,
                   min(max_rows, sheet.max_row or 0))
            )
            if width:
                parts.append("列：" + "、".join(
                    "%s=%s" % (get_column_letter(i + 1), rows[0][i] if rows and i < len(rows[0]) else "")
                    for i in range(width)
                ))
            for row in rows:
                cells = ["" if cell is None else str(cell) for cell in row]
                parts.append("\t".join(cells))
    finally:
        book.close()
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
    return _truncate("\n".join(parts))


def _read_pdf(path):
    tool = shutil.which("pdftotext")
    if not tool:
        return "（服务器没装 pdftotext，读不到 PDF 正文；只能按页打印）"
    try:
        result = subprocess.run(
            [tool, "-layout", "-l", "5", os.path.abspath(path), "-"],
            capture_output=True, timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "（PDF 正文读取失败：%s）" % exc
    text = (result.stdout or b"").decode("utf-8", errors="replace").strip()
    if not text:
        return "（这个 PDF 没有可提取的文字，可能是扫描件）"
    return _truncate(text)


def _read_docx(path):
    try:
        with zipfile.ZipFile(path) as zf:
            xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
    except Exception as exc:
        raise DocumentError("这个 Word 文件打不开：%s" % str(exc)[:120])
    xml = re.sub(r"</w:p>", "\n", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return _truncate(text) or "（Word 里没有可提取的文字）"


def read_document(path, name=""):
    """把附件内容读成模型能理解的文本。"""
    if not path or not os.path.exists(path):
        raise DocumentError("文件不存在或已过期，请重新上传")
    name = name or path
    ext = _ext(name)
    if ext in (".xlsx", ".xls"):
        return _read_table(path)
    if ext == ".pdf":
        return _read_pdf(path)
    if ext == ".docx":
        return _read_docx(path)
    if ext in (".txt", ".md"):
        with open(path, encoding="utf-8", errors="replace") as fh:
            return _truncate(fh.read())
    if ext in (".doc", ".ppt", ".pptx"):
        return "（这种格式读不出结构；要改内容请先传 .docx / .xlsx / PDF）"
    return "（这是图片，读不到里面的文字）"


# ---------- 工具清单 ----------

_INVENTORY = None
# 只列「真在这台机器上、能干活」的，别把 PATH 里几百个程序全倒给模型
_INTERESTING_BINS = (
    "soffice", "libreoffice", "pdftotext", "pdfinfo", "pdftoppm", "gs", "qpdf",
    "pandoc", "convert", "magick", "ffmpeg", "zip", "unzip", "tar", "node",
    "npx", "python3", "pip", "lp", "lpstat", "cupsfilter", "curl", "wget",
)


def inventory(query=""):
    """这台机器上能用的工具和 Python 包，给模型自己挑（D62）。

    为什么不内置「转格式」「改列宽」这类工具：每个需求都加一个工具，
    工具的数目会跟着用户的想象无限长；模型真正需要的是一张能查的清单。
    """
    global _INVENTORY
    if _INVENTORY is None:
        bins = {}
        for name in _INTERESTING_BINS:
            path = shutil.which(name)
            if path:
                bins[name] = path
        packages = []
        try:
            import importlib.metadata as metadata

            packages = sorted({
                dist.metadata["Name"]
                for dist in metadata.distributions()
                if dist.metadata.get("Name")
            })
        except Exception:
            packages = []
        _INVENTORY = (bins, packages)

    bins, packages = _INVENTORY
    terms = [term for term in str(query or "").lower().split() if term]

    def hit(name):
        if not terms:
            return True
        return any(term in name.lower() for term in terms)

    found_bins = sorted(name for name in bins if hit(name))
    found_pkgs = [name for name in packages if hit(name)][:30]
    lines = []
    lines.append("命令行工具：%s" % (", ".join(found_bins) if found_bins else "（没有匹配的）"))
    lines.append("Python 包：%s" % (", ".join(found_pkgs) if found_pkgs else "（没有匹配的）"))
    if terms and not found_pkgs:
        # 关键词对不上不代表没装：把实际装了的列出来，模型才不会以为环境是空的
        lines.append("已装的包（前 40 个）：%s" % ", ".join(packages[:40]))
    if not terms:
        lines.append(
            "全部 Python 包共 %d 个；查具体能力请带关键词，如 find_tools(\"pdf\")。"
            % len(packages)
        )
    return "\n".join(lines)


# ---------- 跑代码 ----------

def _limits():
    """子进程资源上限；Windows 上没有 resource，跳过。"""
    try:
        import resource
    except ImportError:
        return
    try:
        resource.setrlimit(resource.RLIMIT_AS, (CODE_MEMORY_BYTES, CODE_MEMORY_BYTES))
        resource.setrlimit(resource.RLIMIT_CPU, (CODE_TIMEOUT_SECONDS, CODE_TIMEOUT_SECONDS))
    except (ValueError, OSError):
        pass


def run_code(code, src_path=None, out_name=""):
    """在临时目录里跑一段 Python，产出一个新文件。

    约定：输入文件路径在环境变量 XIAOMA_IN（没有附件时为空字符串），输出文件
    路径在 XIAOMA_OUT；代码把结果写到 XIAOMA_OUT 即可。
    """
    code = str(code or "").strip()
    if not code:
        raise DocumentError("没有要执行的代码")
    if src_path and not os.path.exists(src_path):
        raise DocumentError("文件不存在或已过期，请重新上传")

    # 容器里执行通道是另一个 uid（work）：临时目录要放在它的工作目录下，
    # 并且放开权限，否则它进不来也写不进产出文件（D86）。
    from adapters import shell as shell_mod
    use_worker = shell_mod.worker_available()
    if use_worker:
        os.makedirs(shell_mod.WORK_DIR, exist_ok=True)
        workdir = tempfile.mkdtemp(prefix="xiaoma_file_", dir=shell_mod.WORK_DIR)
        os.chmod(workdir, 0o777)
    else:
        workdir = tempfile.mkdtemp(prefix="xiaoma_file_")
    # 和 run_shell 用同一个家目录：pip install --user 装的东西两边都能用，
    # 而且落在 /data 卷里，重建容器不用重装。
    if use_worker:
        home_dir = shell_mod.HOME_DIR
    else:
        home_dir = os.path.join(
            os.environ.get("DATA_DIR", "/data"), "home"
        )
        try:
            os.makedirs(home_dir, exist_ok=True)
            if not os.access(home_dir, os.W_OK):
                raise OSError
        except OSError:
            home_dir = workdir
    input_path = ""
    if src_path:
        input_path = os.path.join(workdir, "input" + _ext(src_path))
        shutil.copyfile(src_path, input_path)
    out_ext = _ext(out_name) or _ext(src_path) or ".txt"
    output_path = os.path.join(workdir, "output" + out_ext)
    script_path = os.path.join(workdir, "run.py")
    with open(script_path, "w", encoding="utf-8") as fh:
        fh.write(code)
    if use_worker:
        # 执行通道是另一个 uid，脚本和输入得让它读得到（它们只是这一轮的临时文件）
        for path in (script_path, input_path):
            if not path:
                continue
            try:
                os.chmod(path, 0o644)
            except OSError:
                pass

    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": home_dir,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "MPLBACKEND": "Agg",
        "XIAOMA_IN": input_path,
        # 用户原始上传的文件：产物再传给下一轮，输入也不该跟着变
        "XIAOMA_SOURCE": input_path,
        "XIAOMA_OUT": output_path,
    }
    # Windows 上 Python 初始化随机数要读系统目录；只补这几个系统变量，
    # 密钥（API_TOKEN 等）一律不带进去。
    for key in ("SystemRoot", "SYSTEMROOT", "TEMP", "TMP"):
        if key in os.environ:
            env[key] = os.environ[key]
    try:
        before = set(os.listdir(workdir))
        if use_worker:
            result = shell_mod.run_python_script(
                script_path, cwd=workdir, env=env,
                timeout=CODE_TIMEOUT_SECONDS, cpu_limit=CODE_TIMEOUT_SECONDS,
            )
        else:
            kwargs = {}
            if os.name == "posix":
                kwargs["preexec_fn"] = _limits
            proc = subprocess.run(
                [sys.executable, script_path],
                cwd=workdir, env=env, timeout=CODE_TIMEOUT_SECONDS,
                capture_output=True, **kwargs
            )
            result = {
                "returncode": proc.returncode,
                "stdout": (proc.stdout or b"").decode("utf-8", errors="replace"),
                "stderr": (proc.stderr or b"").decode("utf-8", errors="replace"),
            }
    except shell_mod.ShellError as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        raise DocumentError("代码没跑起来：%s" % exc)
    except subprocess.TimeoutExpired:
        shutil.rmtree(workdir, ignore_errors=True)
        raise DocumentError("这段代码跑了超过 %d 秒，先改简单点" % CODE_TIMEOUT_SECONDS)

    if result.get("timed_out"):
        shutil.rmtree(workdir, ignore_errors=True)
        raise DocumentError("这段代码跑了超过 %d 秒，先改简单点" % CODE_TIMEOUT_SECONDS)

    stdout = str(result.get("stdout") or "").strip()
    stderr = str(result.get("stderr") or "").strip()
    if result.get("returncode") != 0:
        shutil.rmtree(workdir, ignore_errors=True)
        detail = (stderr or stdout)[-600:]
        raise DocumentError("代码执行失败：%s" % (
            detail or "退出码 %s" % result.get("returncode")
        ))
    if not os.path.isfile(output_path) or os.path.getsize(output_path) == 0:
        # 模型常见几种写法：改输入文件本身、自己起了个名字、甚至把输出路径
        # 当成目录用。只认「工作目录里新出现的普通文件」，别为了名字较劲。
        produced = [
            os.path.join(workdir, name) for name in os.listdir(workdir)
            if name not in before and name != "run.py"
            and os.path.isfile(os.path.join(workdir, name))
            and os.path.getsize(os.path.join(workdir, name)) > 0
        ]
        if not produced and input_path and os.path.exists(input_path):
            # 直接改输入文件也算产出（保存回原路径）
            if os.path.getmtime(input_path) > os.path.getmtime(script_path):
                produced = [input_path]
        if produced:
            output_path = max(produced, key=os.path.getmtime)
        else:
            # 只查环境的代码（print 一下）是合法的：不产出文件就不换打印目标
            shutil.rmtree(workdir, ignore_errors=True)
            return {
                "path": None,
                "workdir": "",
                "stdout": _truncate(stdout, 1500),
            }
    return {
        "path": output_path,
        "workdir": workdir,
        "stdout": _truncate(stdout, 1500),
    }
