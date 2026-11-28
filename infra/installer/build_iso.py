"""把官方 Ubuntu Server ISO 做成「插上就自动装完」的安装盘。

为什么要这么绕：Ubuntu 的自动安装（autoinstall）必须在**内核命令行**上带
`autoinstall ds=nocloud;s=...`，官方 ISO 的 GRUB 菜单里没有这一项，而自动安装
的存在意义恰恰是「没人在机器前守着打字」。所以这里改三处：

1. GRUB 内核行加上 autoinstall 参数；
2. ISO 根目录放 `autoinstall/user-data`、`meta-data`；
3. 重新打包时**保住 El Torito 的 UEFI 入口**。

第 3 条是坑：Ubuntu 的 EFI 镜像不在 ISO9660 目录里，而是拷在 ISO 末尾的附加
分区；拿 pycdlib 这类「只重写文件系统」的工具改，EFI 镜像会丢，机器直接起不来。
所以这里用 xorriso 原样解包再打包，EFI 镜像单独抽出来重新挂上去。

用法（Linux/macOS 直接跑；Windows 上自动借 WSL 里的 xorriso）：

    python infra/installer/build_iso.py \
        --iso ~/Downloads/ubuntu-26.04.1-live-server-amd64.iso \
        --out data/installer/xiaoma.iso \
        --hostname xiaoma --user xiaoma \
        --ssh-key ~/.ssh/id_ed25519.pub

不传 `--password` 会随机生成一个并在结束时打印出来（只打印一次，自己记下来）。
不传 `--ssh-key` 就只能用密码登录，脚本会提醒——**推荐给公钥**，装完直接免密进。

产物本身不含任何密钥文件，只有公钥/密码哈希，可以给人用；但里面的密码哈希和
公钥指向的是你的机器，别把带自己密码的 ISO 传到公开的地方。
"""
import argparse
import os
import re
import secrets
import shutil
import string
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "autoinstall", "user-data.tmpl")

# 内核命令行里必须插在 `---` 前面的部分；`;/cdrom` 是 casper 挂载安装盘的位置。
# **ds 的值必须带引号**：GRUB 脚本里 `;` 是语句分隔符，不引起来会被截成
# `ds=nocloud`，剩下的 `s=/cdrom/...` 变成一个不存在的命令——现象是安装器
# 正常起来，但停在「选语言」，autoinstall 像没生效一样。
AUTOINSTALL_ARGS = 'autoinstall ds="nocloud;s=/cdrom/autoinstall/"'

DEFAULT_PACKAGES = ["vim", "curl", "htop", "git", "ca-certificates", "gnupg",
                    "smartmontools", "lm-sensors", "python3-venv"]
DEFAULT_MIRROR = "https://mirrors.tuna.tsinghua.edu.cn/ubuntu/"


# --------------------------------------------------------------------------
# 纯逻辑（有 pytest，不需要 xorriso、不需要网络）
# --------------------------------------------------------------------------

def patch_grub(text):
    """在内核命令行上插入 autoinstall 参数。

    幂等：已经插过的再跑一次不会变成两份。找不到内核行就报错——宁可在这里
    失败，也别产出一个「看起来像自动安装盘、实际会停在交互界面」的 ISO。
    """
    if AUTOINSTALL_ARGS in text:
        return text
    pattern = re.compile(r"^(\s*linux\s+\S+)(.*)$", re.M)
    match = pattern.search(text)
    if not match:
        raise ValueError("grub.cfg 里找不到 linux 内核行，这个 ISO 不是预期布局")
    head, tail = match.group(1), match.group(2)
    line = head + " " + AUTOINSTALL_ARGS + tail
    return text[:match.start()] + line + text[match.end():]


def parse_efi_interval(as_mkisofs_text):
    """从 `xorriso -report_el_torito as_mkisofs` 的输出里挖出 EFI 附加分区。

    返回 (起始 512 字节块, 块数)，或者 None（EFI 镜像是 ISO 里的普通文件）。
    """
    match = re.search(
        r"-append_partition\s+2\s+\S+\s+--interval:local_fs:(\d+)d-(\d+)d::",
        as_mkisofs_text)
    if not match:
        return None
    start = int(match.group(1))
    end = int(match.group(2))
    return start, end - start + 1


def render_user_data(template, *, hostname, username, password_hash, ssh_keys,
                     packages=None, mirror=DEFAULT_MIRROR, timezone="Asia/Shanghai",
                     locale="en_US.UTF-8"):
    """填模板。占位符没被替换掉就报错——模板改了字段名而代码没跟上时，
    宁可在生成阶段炸，也别让机器装到一半卡在缺字段上。"""
    keys = "\n".join('      - "%s"' % key.strip()
                     for key in (ssh_keys or []) if key.strip())
    if keys:
        ssh_block = "    authorized-keys:\n" + keys
    else:
        ssh_block = "    authorized-keys: []"

    text = template
    text = text.replace("{{HOSTNAME}}", hostname)
    text = text.replace("{{USERNAME}}", username)
    text = text.replace("{{PASSWORD_HASH}}", password_hash)
    text = text.replace("{{SSH_KEYS}}", ssh_block)
    text = text.replace("{{MIRROR}}", mirror)
    text = text.replace("{{TIMEZONE}}", timezone)
    text = text.replace("{{LOCALE}}", locale)
    if packages:
        text = text.replace("{{PACKAGES}}",
                            "\n".join("    - " + name for name in packages))
    else:
        # 一个包都不装就把整段删掉：留个 `packages:` 空值是合法的 YAML，
        # 但 autoinstall 会当成 null 拒绝
        text = text.replace("  packages:\n{{PACKAGES}}\n", "")

    leftover = re.findall(r"\{\{[A-Z_]+\}\}", text)
    if leftover:
        raise ValueError("模板里还有没替换的占位符：%s" % "、".join(sorted(set(leftover))))
    return text


def random_password(length=20):
    alphabet = string.ascii_letters + string.digits + "-_"
    return "".join(secrets.choice(alphabet) for _ in range(length))


# --------------------------------------------------------------------------
# 外部命令：Linux 直接跑，Windows 借 WSL
# --------------------------------------------------------------------------

def to_wsl_path(path):
    """D:\\iso\\a.iso -> /mnt/d/iso/a.iso；已经是 POSIX 路径就原样返回。"""
    path = os.path.abspath(path)
    if re.match(r"^[A-Za-z]:[\\/]", path):
        drive = path[0].lower()
        rest = path[2:].replace("\\", "/")
        return "/mnt/%s%s" % (drive, rest)
    return path.replace("\\", "/")


class Toolbox(object):
    """包一层「命令在哪跑」：本机有 xorriso 就用本机，Windows 上退到 WSL。"""

    def __init__(self, distro=None, verbose=False):
        self.verbose = verbose
        self.wsl = None
        self.prefix = []
        if shutil.which("xorriso"):
            return
        if sys.platform.startswith("win"):
            if not shutil.which("wsl"):
                raise RuntimeError("找不到 xorriso：Windows 上要么装 WSL，要么手动跑 "
                                   "`wsl -u root apt install xorriso`")
            self.wsl = distro or self._default_distro()
            if not self.wsl:
                raise RuntimeError("WSL 里没有可用的发行版，先 `wsl --install -d Ubuntu`")
            self.prefix = ["wsl", "-d", self.wsl, "-u", "root", "--"]
            probe = self._raw(["bash", "-lc", "command -v xorriso"])
            if probe.strip() == "":
                raise RuntimeError("WSL(%s) 里没装 xorriso：`wsl -d %s -u root -- "
                                   "apt install -y xorriso`" % (self.wsl, self.wsl))
            return
        raise RuntimeError("找不到 xorriso，先装上：apt install xorriso")

    @staticmethod
    def _default_distro():
        try:
            out = subprocess.run(["wsl", "-l", "-q"], capture_output=True, text=True,
                                 timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        for line in out.replace("\x00", "").splitlines():
            name = line.strip()
            if name:
                return name
        return None

    def path(self, path):
        return to_wsl_path(path) if self.wsl else path

    def _raw(self, args, check=True, capture=True):
        if self.verbose:
            print("+ " + " ".join(args))
        result = subprocess.run(args, capture_output=capture, text=True,
                                encoding="utf-8", errors="replace")
        if check and result.returncode != 0:
            raise RuntimeError("命令失败（%d）：%s\n%s" % (
                result.returncode, " ".join(args), (result.stderr or "")[-1500:]))
        return result.stdout or ""

    def run(self, args, check=True, capture=True):
        return self._raw(self.prefix + [self.path(a) if self._is_path(a) else a
                                        for a in args], check=check, capture=capture)

    @staticmethod
    def _is_path(arg):
        return bool(re.match(r"^[A-Za-z]:[\\/]", arg)) or (
            arg.startswith("/") and not arg.startswith("/dev/"))


def hash_password(toolbox, password):
    """用 openssl 生成 SHA-512 crypt；云初始化吃的就是这个格式。"""
    salt = "".join(secrets.choice(string.ascii_lowercase) for _ in range(8))
    out = toolbox.run(["openssl", "passwd", "-6", "-salt", salt, password])
    return out.strip()


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def build(iso, out, hostname, username, password_hash, ssh_keys, packages,
          mirror, workdir=None, keep_workdir=False, toolbox=None, verbose=False):
    """生成 ISO。返回产物路径。调用方负责给出密码哈希和公钥。"""
    toolbox = toolbox or Toolbox(verbose=verbose)
    iso, out = os.path.abspath(iso), os.path.abspath(out)
    if not os.path.isfile(iso):
        raise ValueError("找不到 ISO：%s" % iso)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)

    workdir = workdir or tempfile.mkdtemp(prefix="xiaoma-iso-",
                                          dir=os.path.dirname(out) or ".")
    extracted = os.path.join(workdir, "root")
    if os.path.isdir(extracted):
        shutil.rmtree(extracted)
    os.makedirs(extracted)

    print("1/5 解包……")
    toolbox.run(["xorriso", "-osirrox", "on", "-indev", iso, "-extract", "/", extracted])

    print("2/5 改 GRUB……")
    grub = os.path.join(extracted, "boot", "grub", "grub.cfg")
    if not os.path.isfile(grub):
        raise ValueError("ISO 里没有 boot/grub/grub.cfg，确认是 Ubuntu Server 的 ISO")
    text = open(grub, encoding="utf-8").read()
    open(grub, "w", encoding="utf-8", newline="\n").write(patch_grub(text))

    print("3/5 写 autoinstall……")
    template = open(TEMPLATE, encoding="utf-8").read()
    user_data = render_user_data(template, hostname=hostname, username=username,
                                 password_hash=password_hash, ssh_keys=ssh_keys,
                                 packages=packages, mirror=mirror)
    autoinstall_dir = os.path.join(extracted, "autoinstall")
    os.makedirs(autoinstall_dir, exist_ok=True)
    open(os.path.join(autoinstall_dir, "user-data"), "w",
         encoding="utf-8", newline="\n").write(user_data)
    open(os.path.join(autoinstall_dir, "meta-data"), "w",
         encoding="utf-8", newline="\n").write(
        "instance-id: %s-installer\nlocal-hostname: %s\n" % (hostname, hostname))

    print("4/5 抽出 El Torito 的 EFI 镜像……")
    report = toolbox.run(["xorriso", "-indev", iso, "-report_el_torito", "as_mkisofs"])
    efi_arg = None
    efi_file = os.path.join(extracted, "boot", "grub", "efi.img")
    if os.path.isfile(efi_file):
        efi_arg = "boot/grub/efi.img"
    else:
        interval = parse_efi_interval(report)
        if not interval:
            raise ValueError("既没有 boot/grub/efi.img，也没在 El Torito 报告里找到"
                             "EFI 附加分区——这个 ISO 的启动结构不支持重打包")
        start, count = interval
        print("   EFI 附加分区：块 %d，%d 块" % (start, count))
        toolbox.run(["dd", "if=" + iso, "of=" + os.path.join(extracted, "efi.img"),
                     "bs=512", "skip=%d" % start, "count=%d" % count, "status=none"])
        efi_arg = "efi.img"

    print("5/5 重新打包……")
    volume_id = _volume_id(toolbox, iso)
    args = ["xorriso", "-as", "mkisofs", "-r", "-V", volume_id, "-o", out,
            "-c", "boot.catalog",
            "-b", "boot/grub/i386-pc/eltorito.img", "-no-emul-boot",
            "-boot-load-size", "4", "-boot-info-table", "--grub2-boot-info",
            "-eltorito-alt-boot", "-e", efi_arg, "-no-emul-boot",
            extracted]
    toolbox.run(args)

    if not keep_workdir:
        shutil.rmtree(workdir, ignore_errors=True)
    return out


def _volume_id(toolbox, iso):
    """沿用原盘的卷标：casper 靠它找安装介质，改了会找不到 .disk/info。"""
    out = toolbox.run(["xorriso", "-indev", iso, "-pvd_info"], check=False)
    match = re.search(r"Volume Id\s*:\s*(.+)", out)
    return match.group(1).strip() if match else "Ubuntu-Server"


def read_public_key(path):
    with open(path, encoding="utf-8") as fh:
        key = fh.read().strip()
    if not key.startswith(("ssh-ed25519", "ssh-rsa", "ecdsa-sha2-", "sk-ssh-")):
        raise ValueError("%s 看起来不是公钥（以 ssh-ed25519/ssh-rsa 开头）" % path)
    return key


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="生成小马的一键安装 ISO（Ubuntu Server + autoinstall）")
    parser.add_argument("--iso", required=True, help="官方 Ubuntu Server ISO")
    parser.add_argument("--out", required=True, help="输出的 ISO 路径")
    parser.add_argument("--hostname", default="xiaoma")
    parser.add_argument("--user", default="xiaoma")
    parser.add_argument("--password", help="登录密码（不传就随机生成并打印一次）")
    parser.add_argument("--password-hash", help="直接用现成的 SHA-512 crypt 哈希")
    parser.add_argument("--ssh-key", action="append", default=[],
                        help="公钥文件，可多次给；强烈建议给，装完直接免密")
    parser.add_argument("--mirror", default=DEFAULT_MIRROR, help="apt 镜像")
    parser.add_argument("--package", action="append", dest="packages",
                        help="额外装的包，可多次给（默认装 %s）" % "、".join(DEFAULT_PACKAGES))
    parser.add_argument("--no-packages", action="store_true", help="不预装任何额外包")
    parser.add_argument("--workdir", help="解包的临时目录（默认在输出旁边）")
    parser.add_argument("--keep-workdir", action="store_true")
    parser.add_argument("--wsl-distro", help="Windows 上用哪个 WSL 发行版")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    toolbox = Toolbox(distro=args.wsl_distro, verbose=args.verbose)
    keys = [read_public_key(path) for path in args.ssh_key]
    if not keys:
        print("⚠ 没给 --ssh-key：装完只能用密码 SSH，建议加一个公钥", file=sys.stderr)

    if args.password_hash:
        password_hash, password = args.password_hash, None
    elif args.password:
        password, password_hash = args.password, hash_password(toolbox, args.password)
    else:
        password = random_password()
        password_hash = hash_password(toolbox, password)

    packages = [] if args.no_packages else (args.packages or DEFAULT_PACKAGES)
    out = build(args.iso, args.out, args.hostname, args.user, password_hash, keys,
                packages, args.mirror, workdir=args.workdir,
                keep_workdir=args.keep_workdir, toolbox=toolbox, verbose=args.verbose)

    print("\n装好了：%s（%.1f GB）" % (out, os.path.getsize(out) / 1e9))
    print("主机名 %s，用户 %s" % (args.hostname, args.user))
    if password:
        print("初始密码：%s   （只显示这一次）" % password)
    print("下一步：把它挂到 BMC/iLO 的虚拟光驱上启动；HPE 机器可以直接用 "
          "`python infra/installer/ilo_deploy.py --ilo <地址> --iso %s`" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
