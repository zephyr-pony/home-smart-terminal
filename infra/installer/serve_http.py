"""给 BMC/iLO 的虚拟光驱用的极简 HTTP 服务。

为什么不用 `python -m http.server`：BMC 的虚拟光驱按扇区**随机读**，必须先支持
HTTP Range（206 Partial Content）；标准库那个只认整文件 GET，挂上去固件读两下
就报「介质不可用」。

单独用：

    python infra/installer/serve_http.py --root data/installer --port 8899

`ilo_deploy.py` 会在同一个进程里把它跑起来，一般不用手动开。
"""
import argparse
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CHUNK = 262144


def parse_range(header, size):
    """把 Range 头解析成 (start, end, partial)。

    只处理单段 range：BMC 就是这么用的，多段也不值得为它实现。越界直接抛
    ValueError（调用方回 416），别悄悄返回整文件——固件会以为读到了它要的扇区。
    """
    if not header:
        return 0, size - 1, False
    match = re.match(r"bytes=(\d*)-(\d*)", header.strip())
    if not match or (not match.group(1) and not match.group(2)):
        return 0, size - 1, False
    if match.group(1):
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else size - 1
        end = min(end, size - 1)
    else:
        length = int(match.group(2))
        start = max(0, size - length)
        end = size - 1
    if start >= size or start > end:
        raise ValueError("range 越界：%s（文件 %d 字节）" % (header, size))
    return start, end, True


class RangeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    root = "."
    log_path = None

    def log_message(self, fmt, *args):
        pass  # 每个 Range 都记一条会把日志刷爆，自己挑重要的记

    def _file(self):
        name = os.path.basename(self.path.split("?")[0])
        if not name:
            return None
        full = os.path.join(self.root, name)
        return full if os.path.isfile(full) else None

    def _log(self, message):
        if not self.log_path:
            return
        try:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write("%s %s\n" % (time.strftime("%H:%M:%S"), message))
        except OSError:
            pass

    def do_HEAD(self):
        full = self._file()
        if not full:
            self.send_error(404)
            return
        size = os.path.getsize(full)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self):
        full = self._file()
        if not full:
            self.send_error(404)
            return
        size = os.path.getsize(full)
        try:
            start, end, partial = parse_range(self.headers.get("Range"), size)
        except ValueError:
            self.send_response(416)
            self.send_header("Content-Range", "bytes */%d" % size)
            self.end_headers()
            return

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        self._log("GET %s %s" % (os.path.basename(full), self.headers.get("Range") or "full"))
        try:
            with open(full, "rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            self._log("客户端先断开（BMC 读够了就这样）")


def serve(root, port=8899, bind="0.0.0.0", log_path=None):
    """在后台线程里起服务，返回 server 对象（调用方负责 shutdown）。"""
    handler = type("Handler", (RangeHandler,), {
        "root": os.path.abspath(root),
        "log_path": log_path,
    })
    server = ThreadingHTTPServer((bind, port), handler)
    thread = threading.Thread(target=server.serve_forever, name="range-http", daemon=True)
    thread.start()
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description="给 BMC 虚拟光驱用的 Range HTTP 服务")
    parser.add_argument("--root", required=True, help="要共享的目录（ISO 放这里）")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--bind", default="0.0.0.0", help="监听地址；只给本机看用 127.0.0.1")
    parser.add_argument("--log", help="请求日志（默认不记）")
    args = parser.parse_args(argv)
    if not os.path.isdir(args.root):
        print("目录不存在：%s" % args.root, file=sys.stderr)
        return 2
    server = serve(args.root, args.port, args.bind, args.log)
    host, port = server.server_address[:2]
    print("serving %s on http://%s:%d/ （Ctrl+C 停）" % (os.path.abspath(args.root), host, port))
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.shutdown()
        return 0


if __name__ == "__main__":
    sys.exit(main())
