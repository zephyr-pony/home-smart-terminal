"""Linux 打印队列适配器：通过 CUPS（lp / lpstat）出纸。

为什么走 CUPS：服务器部署是 Linux，Windows 打印队列、GDI、Office COM
在服务器上根本不存在（D61 已按部署方向修正）。CUPS 是 Linux 上的标准打印层：
队列、驱动、过滤器都由它管，我们只负责把文件交给它。

文件格式的分工：
- PDF / 图片 / 纯文本：直接交给 CUPS，由 cups-filters 的过滤器转换；
- Office：先用 LibreOffice（soffice --headless）转成 PDF，再走同一条路。

CUPS 的 `-o page-ranges=` 对 PDF 有效；纯文本的页数要等过滤器排版后才知道，
所以这里对文本只做语法校验，不提前查总页数。
"""
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

# 支持的类型：前端上传、后端校验、适配器分派都用这一份
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"})
TEXT_EXTENSIONS = frozenset({".txt", ".md"})
PDF_EXTENSIONS = frozenset({".pdf"})
OFFICE_EXTENSIONS = frozenset({
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
})
SUPPORTED_EXTENSIONS = (
    IMAGE_EXTENSIONS | TEXT_EXTENSIONS | PDF_EXTENSIONS | OFFICE_EXTENSIONS
)

LP_TIMEOUT_SECONDS = 60
OFFICE_TIMEOUT_SECONDS = 180


class PrintError(RuntimeError):
    """打印失败；消息直接给用户看，不要把命令行和堆栈原样丢出去。"""


def kind_of(name):
    """文件的粗分类，用于上传校验和给模型看的说明。"""
    ext = Path(str(name or "")).suffix.lower()
    if ext in PDF_EXTENSIONS:
        return "pdf"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in TEXT_EXTENSIONS:
        return "text"
    if ext in OFFICE_EXTENSIONS:
        return "office"
    return ""


def validate_file(path, original_name=""):
    """上传落盘后的内容校验：扩展名对不上内容就拒绝。

    只防「把 exe 改名成 pdf」这类误传，不做深度解析；真正的损坏留给打印时报错。
    """
    name = original_name or path
    ext = Path(name).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise PrintError("不支持的文件类型：%s。支持 PDF、图片、TXT/Markdown、Office"
                         % (ext or "无扩展名"))
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
    except OSError as exc:
        raise PrintError("读取文件失败：%s" % exc)
    if not head:
        raise PrintError("文件是空的")
    if ext in PDF_EXTENSIONS and not head.startswith(b"%PDF"):
        raise PrintError("这个文件不是 PDF（扩展名和内容对不上）")
    if ext in (".docx", ".xlsx", ".pptx") and not head.startswith(b"PK"):
        raise PrintError("这个 Office 文件内容不对（扩展名和内容对不上）")
    if ext in (".doc", ".xls", ".ppt") and not head.startswith(b"\xd0\xcf\x11\xe0"):
        raise PrintError("这个 Office 文件内容不对（扩展名和内容对不上）")
    if ext in IMAGE_EXTENSIONS:
        try:
            from PIL import Image
            with Image.open(path) as image:
                image.verify()
        except Exception:
            raise PrintError("图片打不开，可能已损坏")
    return ext


def parse_pages(spec, total):
    """「1-3,5」→ [0,1,2,4]；空/all 返回全部；越界报错。

    `total=None` 表示还不知道页数（纯文本要 CUPS 排版后才知道），只做语法解析；
    确认后回放时 `spec` 也可能是 guard 解析好的 0 基序号列表。
    """
    if isinstance(spec, (list, tuple)):
        picked = []
        for item in spec:
            try:
                picked.append(int(item))
            except (TypeError, ValueError):
                raise PrintError("页码范围看不懂：%s" % (spec,))
        if total is not None and total > 0:
            picked = [index for index in picked if 0 <= index < total]
        if not picked:
            raise PrintError("页码超出范围：共 %d 页" % (total or 0))
        return sorted(set(picked))

    if spec is None:
        return list(range(total)) if total and total > 0 else []
    text = str(spec).strip().lower()
    if not text or text in ("all", "全部", "所有"):
        return list(range(total)) if total and total > 0 else []
    text = (text.replace("，", ",").replace("、", ",").replace("；", ",")
            .replace(";", ",").replace("－", "-").replace("—", "-")
            .replace("–", "-").replace("第", "").replace("页", ""))
    picked = []
    for chunk in (part.strip() for part in text.split(",")):
        if not chunk:
            continue
        match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", chunk)
        if match:
            start, end = int(match.group(1)), int(match.group(2))
            if start < 1 or end < 1:
                raise PrintError("页码从 1 开始：%s" % spec)
            if start > end:
                start, end = end, start
            picked.extend(range(start - 1, end))
            continue
        if re.fullmatch(r"\d+", chunk):
            number = int(chunk)
            if number < 1:
                raise PrintError("页码从 1 开始：%s" % spec)
            picked.append(number - 1)
            continue
        raise PrintError("页码范围看不懂：%s（例：1-3,5）" % spec)
    if total is not None and total > 0:
        picked = [index for index in picked if index < total]
        if not picked:
            raise PrintError("页码超出范围：共 %d 页" % total)
    return sorted(set(picked))


def _compact_ranges(indices):
    """[0,1,2,4] → '1-3,5'，给 CUPS 的 page-ranges 用。"""
    ranges = []
    for index in sorted(set(indices)):
        if ranges and index == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], index)
        else:
            ranges.append((index, index))
    parts = []
    for start, end in ranges:
        if start == end:
            parts.append(str(start + 1))
        else:
            parts.append("%d-%d" % (start + 1, end + 1))
    return ",".join(parts)


def _run(cmd, timeout=LP_TIMEOUT_SECONDS):
    """跑一条外部命令；命令不存在/超时都换成给人看的错误。

    强制 C locale：`lpstat` 的默认队列/队列列表都是本地化文本，
    中文环境下按 "system default destination:" 解析会直接失效。
    """
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=env,
        )
    except FileNotFoundError:
        raise PrintError(
            "服务器上没有 %s，打印需要先安装并配置 CUPS" % cmd[0]
        )
    except subprocess.TimeoutExpired:
        raise PrintError("命令超时：%s" % " ".join(cmd[:2]))


def pdf_page_count(path):
    """数一下 PDF 页数；数不出来就返回 None（让调用方跳过越界检查）。"""
    try:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(path)
        try:
            return len(pdf)
        finally:
            pdf.close()
    except Exception:
        return None


def office_converter():
    """找到 LibreOffice 的可执行文件；没有返回 None。"""
    return shutil.which("soffice") or shutil.which("libreoffice")


def office_available():
    return office_converter() is not None


def _office_to_pdf(path, timeout=OFFICE_TIMEOUT_SECONDS):
    """用 LibreOffice 把 Office 文档转成 PDF；返回 (pdf 路径, 临时目录)。

    服务器不装 Office（那是 Windows 的东西），LibreOffice 是 Linux 上唯一的
    稳妥选择。转换失败就直接告诉用户「先另存为 PDF」。
    """
    soffice = office_converter()
    if not soffice:
        raise PrintError(
            "服务器没装 LibreOffice，Office 文档先在电脑上另存为 PDF 再发"
        )

    out_dir = tempfile.mkdtemp(prefix="xiaoma_print_")
    profile = Path(out_dir) / "lo-profile"
    cmd = [
        soffice,
        "--headless",
        "--norestore",
        "--convert-to", "pdf",
        "--outdir", out_dir,
        "-env:UserInstallation=%s" % profile.as_uri(),
        os.path.abspath(path),
    ]
    result = _run(cmd, timeout=timeout)
    if result.returncode != 0:
        shutil.rmtree(out_dir, ignore_errors=True)
        message = (result.stderr or result.stdout or "").strip()
        raise PrintError("Office 文档转 PDF 失败：%s" % (message[:200] or "soffice 返回 %d" % result.returncode))

    pdf_path = Path(out_dir) / (Path(path).stem + ".pdf")
    if not pdf_path.exists():
        found = sorted(Path(out_dir).glob("*.pdf"))
        pdf_path = found[0] if found else None
    if not pdf_path or not pdf_path.exists() or pdf_path.stat().st_size == 0:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise PrintError("Office 文档转换后没有生成 PDF，换一份文件试试")
    return str(pdf_path), out_dir


class Printer:
    """一个 CUPS 打印队列。name 为空时用系统默认队列。

    `server` 是远程 CUPS 服务器（如 `192.168.1.15:631`）；留空则用本机
    CUPS。容器部署时一般填家里那台共享打印机的 CUPS 地址，容器里不需要
    自己跑 cupsd。
    """

    def __init__(self, name="", allowed=(), max_pages=50, max_copies=20,
                 enabled=True, server="", lp="lp", lpstat="lpstat"):
        self.name = str(name or "").strip()
        self.allowed = tuple(item.strip() for item in (allowed or ()) if item.strip())
        self.max_pages = max(1, int(max_pages))
        self.max_copies = max(1, int(max_copies))
        self.enabled = bool(enabled)
        self.server = self._clean_server(server)
        self.lp = lp
        self.lpstat = lpstat

    @staticmethod
    def _clean_server(server):
        """`http://host:631/` 这种写法也认；`lp -h` 要的是 `host:631`。"""
        text = str(server or "").strip()
        for prefix in ("http://", "https://", "ipp://", "ipps://"):
            if text.lower().startswith(prefix):
                text = text[len(prefix):]
                break
        return text.rstrip("/")

    def _host_args(self):
        return ["-h", self.server] if self.server else []

    def available(self):
        """当前部署能不能打印（有 lp 命令 + 队列存在）。"""
        if not self.enabled:
            return False
        if not shutil.which(self.lp):
            return False
        try:
            self._resolve_queue()
            return True
        except Exception:
            return False

    def info(self):
        """给诊断/日志用；拿不到就返回 None。"""
        try:
            queue = self._resolve_queue()
            result = _run(
                [self.lpstat] + self._host_args() + ["-p", queue], timeout=10,
            )
            return {"name": queue, "status": (result.stdout or "").strip()}
        except Exception:
            return None

    def status(self):
        """给网页/接口看的状态：能不能打、队列在不在、有没有积压。

        刻意不抛异常：打印机断电是常态，概览页每 30 秒问一次，
        每次都冒堆栈没有意义；原因放 `error` 里给人看。
        `jobs` 是队列里未完成的任务数（None 表示查不到）。
        """
        detail = {
            "enabled": bool(self.enabled),
            "configured": bool(self.name or self.server),
            "available": False,
            "name": self.name,
            "server": self.server,
            "queue": "",
            "state": "unknown",
            "jobs": None,
            "error": "",
        }
        if not self.enabled:
            detail["error"] = "打印功能已关闭（PRINTER_ENABLED=0）"
            return detail

        missing = [cmd for cmd in (self.lp, self.lpstat) if not shutil.which(cmd)]
        if missing:
            detail["error"] = "服务器上没有 %s，打印需要先装 CUPS" % missing[0]
            return detail

        try:
            queue = self._resolve_queue()
        except PrintError as exc:
            detail["error"] = str(exc)
            return detail
        detail["queue"] = queue
        detail["available"] = True

        try:
            result = _run(
                [self.lpstat] + self._host_args() + ["-p", queue], timeout=10,
            )
            text = (result.stdout or "").strip()
            if "disabled" in text:
                detail["state"] = "disabled"
                detail["error"] = "队列被停用了，在 CUPS 里重新启用一下"
            elif "now printing" in text:
                detail["state"] = "printing"
            elif "is idle" in text:
                detail["state"] = "idle"
            elif result.returncode != 0:
                detail["error"] = (
                    result.stderr or text or "lpstat 返回 %d" % result.returncode
                ).strip()[:200]
        except PrintError as exc:
            detail["available"] = False
            detail["error"] = str(exc)
            return detail

        try:
            jobs = _run(
                [self.lpstat] + self._host_args() + ["-o", queue], timeout=10,
            )
            detail["jobs"] = len([
                line for line in (jobs.stdout or "").splitlines() if line.strip()
            ])
        except PrintError:
            pass
        return detail

    def list_printers(self):
        try:
            result = _run(
                [self.lpstat] + self._host_args() + ["-a"], timeout=10,
            )
            return [
                line.split(None, 1)[0]
                for line in (result.stdout or "").splitlines()
                if line.strip()
            ]
        except Exception:
            return []

    def _default_queue(self):
        result = _run(
            [self.lpstat] + self._host_args() + ["-d"], timeout=10,
        )
        text = (result.stdout or "").strip()
        marker = "system default destination:"
        if marker in text:
            return text.split(marker, 1)[1].strip()
        raise PrintError("没有配置打印机，也没有系统默认队列")

    def _resolve_queue(self):
        name = self.name or self._default_queue()
        result = _run([self.lpstat] + self._host_args() + ["-a"], timeout=10)
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "").strip()
            raise PrintError("查不到 CUPS 队列：%s" % (message or "lpstat 失败"))
        queues = {
            line.split(None, 1)[0]
            for line in (result.stdout or "").splitlines() if line.strip()
        }
        if name not in queues:
            raise PrintError(
                "CUPS 里没有队列「%s」。现有：%s" % (name, "、".join(sorted(queues)) or "无")
            )
        if self.allowed and name not in self.allowed:
            raise PrintError("只允许使用：%s" % "、".join(self.allowed))
        return name

    def print_file(self, path, copies=1, pages=None, printer_name="",
                   doc_name=""):
        """提交一个打印任务，返回 {"ok", "printer", "pages", "copies", "job_id"}。

        pages 可以是 guard 解析好的 0 基序号列表，也可以是「1-3,5」字符串；
        None 表示全部页。
        """
        if not self.enabled:
            raise PrintError("打印功能已关闭（PRINTER_ENABLED=0）")
        if not path or not os.path.exists(path):
            raise PrintError("文件不存在或已过期，请重新上传")

        ext = validate_file(path, doc_name or path)
        try:
            count = int(copies)
        except (TypeError, ValueError):
            count = 1
        count = max(1, min(count, self.max_copies))

        temp_dir = None
        print_path = path
        total = None
        try:
            if ext in OFFICE_EXTENSIONS:
                print_path, temp_dir = _office_to_pdf(path)
                total = pdf_page_count(print_path)
            elif ext in PDF_EXTENSIONS:
                total = pdf_page_count(print_path)
            elif ext in IMAGE_EXTENSIONS:
                total = 1
            # 纯文本 total=None：CUPS 排版后才知道页数

            spec = self._page_spec(pages, total)
            queue = self._resolve_queue()
            job_id = self._submit(
                queue, print_path, count, spec,
                doc_name or os.path.basename(path),
            )
            selected = len(parse_pages(pages, total)) if total else None
            return {
                "ok": True,
                "printer": queue,
                "pages": selected,
                "copies": count,
                "total_pages": total,
                "job_id": job_id,
                "kind": kind_of(doc_name or path),
            }
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)

    def _page_spec(self, pages, total):
        """把页码整理成 CUPS 的 page-ranges 字符串；None 表示全部。"""
        if pages is None:
            return None
        if isinstance(pages, (list, tuple)):
            if not pages:
                return None
            indices = parse_pages(pages, total)
        else:
            text = str(pages).strip()
            if not text or text.lower() in ("all", "全部", "所有"):
                return None
            indices = parse_pages(text, total)
        spec = _compact_ranges(indices)
        if total and len(indices) > self.max_pages:
            raise PrintError("一次最多打印 %d 页" % self.max_pages)
        return spec

    def _submit(self, queue, path, copies, page_spec, doc_name):
        cmd = [self.lp] + self._host_args() + [
            "-d", queue,
            "-n", str(copies),
            "-t", str(doc_name or "xiaoma"),
        ]
        if page_spec:
            cmd += ["-o", "page-ranges=%s" % page_spec]
        cmd.append(path)

        result = _run(cmd)
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "").strip()
            raise PrintError(
                "打印失败：%s" % (message[:200] or "lp 返回 %d" % result.returncode)
            )
        output = (result.stdout or "").strip()
        job_id = ""
        if "request id is" in output:
            job_id = output.split("request id is", 1)[1].strip().split()[0]
        return job_id


def build_printer(name="", allowed=(), max_pages=50, max_copies=20,
                  enabled=True, server=""):
    return Printer(
        name=name, allowed=allowed, max_pages=max_pages,
        max_copies=max_copies, enabled=enabled, server=server,
    )
