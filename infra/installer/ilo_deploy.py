"""在 HPE iLO（Redfish）上挂 ISO、一次性从虚拟光驱启动，装完把介质弹掉。

为什么值得单独写：Gen10 Plus 这类机器没有显卡输出也能装系统，前提是 BMC 能挂
虚拟光驱（iLO 需要 Advanced/Essentials 授权）。手点网页要开浏览器、等上传、
再改一次性启动项；这四步 Redfish 都能做，做成脚本以后新机器到手一条命令。

一条命令：

    python infra/installer/ilo_deploy.py \
        --ilo 192.168.2.200 --user Administrator \
        --iso data/installer/xiaoma.iso --scan 192.168.2.0/24

密码走 `ILO_PASSWORD` 环境变量，别写在命令行历史里。

只挂不启：`--no-boot`；只弹介质：`--eject`；装完等新机上线：`--wait-host <IP>`。

**实测注意（iLO 5 2.33 / Gen10 Plus）**：Redfish 设的「一次性从光驱启动」会被 POST
直接清掉而不生效（`BootSourceOverrideTarget` 回到 None，固件仍然从硬盘起）。稳妥做法是
先在 iLO 里把「iLO Virtual USB…CD-ROM」调到**引导顺序第一位**，再冷启动
（`power off hard` → `power on`）；热重启常常跳过虚拟光驱。介质还要是「已连接」
（`ConnectedVia: URI`），只在 CLI 里 `vm cdrom insert` 只写了 URL。
"""
import argparse
import getpass
import ipaddress
import os
import socket
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import serve_http  # noqa: E402  同目录，Range 服务

try:
    import requests
    import urllib3
except ImportError:  # pragma: no cover - 正常安装 requirements-server 就有
    print("需要 requests：pip install requests", file=sys.stderr)
    raise


def local_ip_towards(host):
    """问操作系统：去这个地址用哪块网卡的 IP（不发包）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((host, 443))
        return sock.getsockname()[0]
    finally:
        sock.close()


class Ilo(object):
    def __init__(self, host, user, password, timeout=30):
        urllib3.disable_warnings()
        self.base = "https://%s" % host
        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = False  # iLO 出厂是自签证书，局域网内可以接受
        self.session.headers.update({"Connection": "close"})
        reply = self.session.post(self.base + "/redfish/v1/SessionService/Sessions",
                                  json={"UserName": user, "Password": password},
                                  timeout=timeout)
        if reply.status_code not in (200, 201):
            raise RuntimeError("iLO 登录失败（%d）：检查用户名密码" % reply.status_code)
        self.session.headers["X-Auth-Token"] = reply.headers.get("X-Auth-Token", "")

    def get(self, path):
        reply = self.session.get(self.base + path, timeout=self.timeout)
        reply.raise_for_status()
        return reply.json()

    def post(self, path, payload=None):
        return self.session.post(self.base + path, json=payload or {}, timeout=self.timeout)

    def patch(self, path, payload):
        return self.session.patch(self.base + path, json=payload, timeout=self.timeout)

    def cd_device(self):
        """挑带 CD 能力的虚拟媒体（有的机器是 Floppy/USB 在前）。"""
        media = self.get("/redfish/v1/Managers/1/VirtualMedia/")
        for member in media.get("Members", []):
            info = self.get(member["@odata.id"])
            types = info.get("MediaTypes") or []
            if any("CD" in str(item) or "DVD" in str(item) for item in types):
                return member["@odata.id"], info
        raise RuntimeError("这台 BMC 没有 CD 虚拟媒体")

    def insert(self, device, url):
        reply = self.post(device.rstrip("/") + "/Actions/VirtualMedia.InsertMedia",
                          {"Image": url, "Inserted": True, "WriteProtected": True})
        if reply.status_code not in (200, 201, 204):
            raise RuntimeError("挂载失败（%d）：%s" % (reply.status_code, reply.text[:400]))
        info = self.get(device)
        if not info.get("Inserted"):
            raise RuntimeError("BMC 报告没挂上，检查 URL 是否可达：%s" % url)
        return info

    def eject(self, device):
        reply = self.post(device.rstrip("/") + "/Actions/VirtualMedia.EjectMedia", {})
        return reply.status_code in (200, 204)

    def boot_once_from_cd(self):
        reply = self.patch("/redfish/v1/Systems/1/",
                           {"Boot": {"BootSourceOverrideTarget": "Cd",
                                     "BootSourceOverrideEnabled": "Once"}})
        if reply.status_code not in (200, 204):
            raise RuntimeError("设置一次性启动失败（%d）：%s" % (reply.status_code, reply.text[:300]))

    def clear_boot_override(self):
        self.patch("/redfish/v1/Systems/1/",
                   {"Boot": {"BootSourceOverrideEnabled": "Disabled"}})

    def restart(self):
        reply = self.post("/redfish/v1/Systems/1/Actions/ComputerSystem.Reset/",
                          {"ResetType": "ForceRestart"})
        if reply.status_code not in (200, 202, 204):
            raise RuntimeError("重启失败（%d）：%s" % (reply.status_code, reply.text[:300]))

    def power_state(self):
        return self.get("/redfish/v1/Systems/1/").get("PowerState")


def port_open(host, port=22, timeout=1.5):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        return sock.connect_ex((host, port)) == 0
    finally:
        sock.close()


def scan(cidr, port=22, workers=64):
    """扫一段网段里开着某个端口的机器（没有 DNS 时找新装好的机器用）。"""
    from concurrent.futures import ThreadPoolExecutor

    hosts = [str(ip) for ip in ipaddress.ip_network(cidr, strict=False).hosts()]
    found = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for host, ok in zip(hosts, pool.map(lambda h: port_open(h, port), hosts)):
            if ok:
                found.append(host)
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(description="用 iLO 虚拟光驱装一台机器")
    parser.add_argument("--ilo", required=True, help="iLO 地址")
    parser.add_argument("--user", default="Administrator")
    parser.add_argument("--password", help="默认读 ILO_PASSWORD，都没有就交互问")
    parser.add_argument("--iso", help="要挂的 ISO（--eject 时不用）")
    parser.add_argument("--http-port", type=int, default=8899)
    parser.add_argument("--http-host", help="iLO 访问本机用的地址（默认自动判断）")
    parser.add_argument("--no-boot", action="store_true", help="只挂载，不重启")
    parser.add_argument("--eject", action="store_true", help="只弹介质并清掉一次性启动")
    parser.add_argument("--wait-host", help="等这台机器 22 端口起来（装完自动弹介质）")
    parser.add_argument("--scan", help="扫一段网段里开着 22 的机器，如 192.168.2.0/24")
    parser.add_argument("--wait-timeout", type=int, default=1800)
    args = parser.parse_args(argv)

    password = args.password or os.environ.get("ILO_PASSWORD")
    if not password and not args.scan:
        password = getpass.getpass("iLO 密码：")

    if args.scan:  # 只扫描，不需要登录
        found = scan(args.scan)
        print("22 端口开着的机器（%d 台）：%s" % (len(found), "、".join(found) or "无"))
        return 0

    ilo = Ilo(args.ilo, args.user, password)
    device, info = ilo.cd_device()
    print("虚拟光驱：%s（当前已插入：%s）" % (device, info.get("Inserted")))

    if args.eject:
        print("弹出介质：%s" % ("成功" if ilo.eject(device) else "失败"))
        ilo.clear_boot_override()
        return 0

    if not args.iso:
        parser.error("挂载要 --iso")
    iso = os.path.abspath(args.iso)
    if not os.path.isfile(iso):
        parser.error("找不到 ISO：%s" % iso)

    http_host = args.http_host or local_ip_towards(args.ilo)
    server = serve_http.serve(os.path.dirname(iso), args.http_port,
                              log_path=os.path.join(os.path.dirname(iso), "serve.log"))
    url = "http://%s:%d/%s" % (http_host, args.http_port, os.path.basename(iso))
    print("HTTP: %s" % url)
    try:
        ilo.insert(device, url)
        print("已挂载，BMC 正在拉取（大 ISO 要等一会儿才有响应）")
        if args.no_boot:
            print("没有重启（--no-boot）。手动开机后会从虚拟光驱启动一次。")
            return 0
        ilo.boot_once_from_cd()
        ilo.restart()
        print("已设置「一次性从光驱启动」并重启，进安装器约 1–3 分钟")
        if not args.wait_host:
            print("装完自己弹介质：加 --eject，或看 iLO 网页。")
            return 0
        deadline = time.time() + args.wait_timeout
        print("等 %s 的 22 端口起来……" % args.wait_host)
        while time.time() < deadline:
            if port_open(args.wait_host):
                break
            time.sleep(10)
        else:
            print("超时没等到，介质仍挂着；确认后手动 --eject")
            return 1
        print("机器起来了，弹介质")
        ilo.eject(device)
        ilo.clear_boot_override()
        return 0
    finally:
        server.shutdown()


if __name__ == "__main__":
    sys.exit(main())
