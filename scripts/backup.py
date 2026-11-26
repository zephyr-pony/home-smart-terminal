"""备份与恢复：把「丢了就回不来」的东西打成一个 zip。

为什么不是直接拷 data/：服务在跑，`*.db` 可能正写到一半，拷出来是坏的。
这里统一走 sqlite 的 backup API（在线热备），恢复时再校验 sha256 与 integrity_check。

为什么带上 .env：VAPID 私钥在里面，丢了所有推送订阅全部作废，手机要重新开一遍通知。

加密（2026-11-26）：备份里迟早会有身份信息（体检报告、账单扫描件），
异地副本一旦离开自家硬盘就必须加密。用 AES-256-GCM，密钥由口令经 scrypt 派生，
输出 `.zip.enc`；明文 zip 加密后立即删除，同一目录里不会同时留两份。

用法：
    python scripts/backup.py                     # 备份到 backups/，保留 7 份
    python scripts/backup.py --encrypt           # 加密备份（口令取 BACKUP_PASSPHRASE）
    python scripts/backup.py --check <zip|enc>   # 校验（加密备份需要口令）
    python scripts/backup.py --restore <zip|enc> --to D:/tmp/drill   # 恢复到指定目录（演练用）
    python scripts/backup.py --restore <zip|enc> --yes               # 恢复回本机（会先自备份）
    python scripts/backup.py --all-data          # 连 data/ 下的目录一起备份（chroma、HA 配置）

默认只备「核心状态」：`.env` + `data/` 顶层的非日志文件（记忆、事实、任务、物品、推送订阅）。
`data/homeassistant` 是 HA 的运行时目录，由 HA 自己的快照负责，默认不掺进来。
"""
import argparse
import contextlib
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(ROOT, "backups")
KEEP = 7
# 备份里不放的东西：日志（天天变、没价值）、备份目录自己
SKIP_SUFFIXES = (".log",)
SKIP_DIRS = ("backups",)
# 打印文件是用户上传/生成的原件（D65），和历史记录一起备；不像 work/home 那样可重建。
# spaces 是其它空间（D68）的一整套库，默认空间之外的数据只在这里，必须备。
ALWAYS_DIRS = ("print", "spaces")

MANIFEST = "manifest.json"

# 加密容器格式（自己定，够用就行）：magic(5) + salt(16) + nonce(12) + AES-GCM 密文。
# 口令不落盘；密钥每次由 scrypt 现算，所以备份文件搬到别的机器也能解。
BACKUP_MAGIC = b"XMBK1"
ENCRYPT_SUFFIX = ".enc"
PASSPHRASE_ENV = "BACKUP_PASSPHRASE"
SCRYPT_N = 2 ** 14  # 约 16MB 内存、50ms 一轮，够挡住离线穷举又不拖慢定时任务


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _snapshot_sqlite(src, dst):
    """用 backup API 做在线热备；不是 sqlite 文件就退回普通拷贝。"""
    try:
        with sqlite3.connect(src) as source, sqlite3.connect(dst) as target:
            source.backup(target)
        return True
    except sqlite3.DatabaseError:
        shutil.copy2(src, dst)
        return False


# ---------- 加密（AES-256-GCM + scrypt 口令派生） ----------

def is_encrypted(path):
    """按文件头判断，不看扩展名（改名/去掉后缀也能认出来）。"""
    try:
        with open(path, "rb") as fh:
            return fh.read(len(BACKUP_MAGIC)) == BACKUP_MAGIC
    except OSError:
        return False


def _derive_key(passphrase, salt):
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    return Scrypt(salt=salt, length=32, n=SCRYPT_N, r=8, p=1).derive(
        str(passphrase).encode("utf-8")
    )


def encrypt_file(src, dst, passphrase):
    """把 zip 加密成 `.zip.enc`；口令为空直接拒绝，不留半个明文包。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not passphrase:
        raise SystemExit(
            "没有口令，不能加密备份：设 %s 或用 --passphrase-file" % PASSPHRASE_ENV
        )
    salt = os.urandom(16)
    nonce = os.urandom(12)
    with open(src, "rb") as fh:
        data = fh.read()
    ciphertext = AESGCM(_derive_key(passphrase, salt)).encrypt(
        nonce, data, BACKUP_MAGIC
    )
    with open(dst, "wb") as fh:
        fh.write(BACKUP_MAGIC + salt + nonce + ciphertext)
    return dst


def decrypt_file(src, dst, passphrase):
    """解密到 dst；口令错和文件被改过都报同一句，别泄露是哪一种。"""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not passphrase:
        raise SystemExit(
            "这是加密备份，需要口令：设 %s 或用 --passphrase-file" % PASSPHRASE_ENV
        )
    with open(src, "rb") as fh:
        blob = fh.read()
    if blob[:len(BACKUP_MAGIC)] != BACKUP_MAGIC:
        raise SystemExit("不是加密的小马备份：%s" % src)
    body = blob[len(BACKUP_MAGIC):]
    salt, nonce, ciphertext = body[:16], body[16:28], body[28:]
    try:
        data = AESGCM(_derive_key(passphrase, salt)).decrypt(
            nonce, ciphertext, BACKUP_MAGIC
        )
    except InvalidTag:
        raise SystemExit("口令不对，或备份文件被改过")
    with open(dst, "wb") as fh:
        fh.write(data)
    return dst


@contextlib.contextmanager
def open_archive(path, passphrase=None):
    """返回一个可以当普通 zip 读的路径：明文直接用，加密的先解到临时文件。

    临时明文只活在这个 with 里，退出即删；不要改成长久落盘。
    """
    if not is_encrypted(path):
        yield path
        return
    tmp_dir = tempfile.mkdtemp(prefix="xiaoma-decrypt-")
    try:
        target = os.path.join(tmp_dir, "backup.zip")
        decrypt_file(path, target, passphrase)
        yield target
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def resolve_passphrase(passphrase=None, passphrase_file=None):
    """口令来源优先级：显式参数 > 文件 > 环境变量。"""
    if passphrase:
        return str(passphrase)
    if passphrase_file:
        with open(passphrase_file, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    return os.environ.get(PASSPHRASE_ENV, "").strip()


def collect_sources(root=ROOT, all_data=False, data_dir=None):
    """返回 [(zip 内名字, 绝对路径)]。

    `data_dir` 用来备份别处的数据（Docker 卷快照解出来的副本）；
    默认仍是仓库下的 `data/`。
    """
    items = []
    env_path = os.path.join(root, ".env")
    if os.path.exists(env_path):
        items.append((".env", env_path))
    data_dir = data_dir or os.path.join(root, "data")
    if not os.path.isdir(data_dir):
        return items
    for name in sorted(os.listdir(data_dir)):
        path = os.path.join(data_dir, name)
        if os.path.isdir(path):
            if name in SKIP_DIRS or (not all_data and name not in ALWAYS_DIRS):
                continue
            for base, _dirs, files in os.walk(path):
                for filename in files:
                    full = os.path.join(base, filename)
                    rel = os.path.relpath(full, root).replace(os.sep, "/")
                    items.append((rel, full))
            continue
        if name.endswith(SKIP_SUFFIXES):
            continue
        items.append(("data/" + name, path))
    return items


def create_backup(root=ROOT, out_dir=DEFAULT_OUT, keep=KEEP, all_data=False, now=None,
                  data_dir=None, note=None, passphrase=None):
    """打一份备份，返回路径（加密时是 `.zip.enc`）。

    内容先落到临时目录，再整体写入，避免半份文件；加密时写完立刻删明文 zip。
    """
    now = now or datetime.now()
    os.makedirs(out_dir, exist_ok=True)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    zip_path = os.path.join(out_dir, "xiaoma-%s.zip" % stamp)

    sources = collect_sources(root, all_data=all_data, data_dir=data_dir)
    if not sources:
        raise SystemExit("没有可备份的文件：%s" % root)

    entries = []
    tmp_dir = tempfile.mkdtemp(prefix="xiaoma-backup-")
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for arcname, full in sources:
                staged = os.path.join(tmp_dir, os.path.basename(arcname))
                if arcname.endswith(".db"):
                    _snapshot_sqlite(full, staged)
                else:
                    shutil.copy2(full, staged)
                zf.write(staged, arcname)
                entries.append({
                    "name": arcname,
                    "size": os.path.getsize(staged),
                    "sha256": _sha256(staged),
                })
            manifest = {
                "created_at": now.isoformat(timespec="seconds"),
                "host": socket.gethostname(),
                "root": root,
                "all_data": bool(all_data),
                "note": note or "",
                "items": entries,
            }
            zf.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if passphrase:
        enc_path = zip_path + ENCRYPT_SUFFIX
        encrypt_file(zip_path, enc_path, passphrase)
        os.remove(zip_path)  # 同一目录不留明文副本
        return enc_path
    return zip_path


def verify_archive(zip_path, passphrase=None):
    """返回 (ok, problems)。校验 sha256 与每个 .db 的 integrity_check。

    加密备份会先解到临时目录再验；口令错在这里就报出来，不会去覆盖数据。
    """
    problems = []
    if not os.path.exists(zip_path):
        return False, ["文件不存在：%s" % zip_path]
    if is_encrypted(zip_path):
        try:
            with open_archive(zip_path, passphrase) as archive:
                return verify_archive(archive)
        except SystemExit as exc:
            return False, [str(exc)]
    tmp_dir = tempfile.mkdtemp(prefix="xiaoma-verify-")
    try:
        with zipfile.ZipFile(zip_path) as zf:
            bad = zf.testzip()
            if bad:
                problems.append("压缩包内文件损坏：%s" % bad)
            try:
                manifest = json.loads(zf.read(MANIFEST).decode("utf-8"))
            except (KeyError, ValueError):
                return False, ["缺少或损坏 manifest.json"]
            for item in manifest.get("items", []):
                name = item.get("name")
                try:
                    zf.extract(name, tmp_dir)
                except KeyError:
                    problems.append("清单里有、包里没有：%s" % name)
                    continue
                path = os.path.join(tmp_dir, name)
                if _sha256(path) != item.get("sha256"):
                    problems.append("sha256 不符：%s" % name)
                if name.endswith(".db"):
                    try:
                        with sqlite3.connect(path) as conn:
                            result = conn.execute("PRAGMA integrity_check").fetchone()
                        if not result or result[0] != "ok":
                            problems.append("integrity_check 不通过：%s" % name)
                    except sqlite3.DatabaseError as exc:
                        problems.append("打不开数据库 %s：%s" % (name, exc))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return not problems, problems


def restore_archive(zip_path, target_root=ROOT, force=False, passphrase=None):
    """把备份解回目标目录；不覆盖正在运行的服务数据，除非 force。"""
    ok, problems = verify_archive(zip_path, passphrase=passphrase)
    if not ok:
        raise SystemExit("备份不可用：\n  " + "\n  ".join(problems))

    if os.path.abspath(target_root) == os.path.abspath(ROOT):
        if not force:
            raise SystemExit(
                "拒绝直接覆盖本机数据：先停掉 server.py，确认后加 --yes。"
                "（演练用 --to <空目录> 更安全）"
            )
        safety = create_backup(out_dir=os.path.join(ROOT, "backups", "pre-restore"))
        print("已先备份当前状态：%s" % safety)

    os.makedirs(target_root, exist_ok=True)
    with open_archive(zip_path, passphrase) as archive:
        with zipfile.ZipFile(archive) as zf:
            for name in zf.namelist():
                if name == MANIFEST:
                    continue
                zf.extract(name, target_root)
    return target_root


def _backup_names(out_dir):
    if not os.path.isdir(out_dir):
        return []
    return sorted(
        name for name in os.listdir(out_dir)
        if name.startswith("xiaoma-")
        and (name.endswith(".zip") or name.endswith(".zip" + ENCRYPT_SUFFIX))
    )


def list_backups(out_dir=DEFAULT_OUT):
    return _backup_names(out_dir)


def prune(out_dir=DEFAULT_OUT, keep=KEEP):
    """只留最近 keep 份；按文件名里的时间戳排序，不依赖文件修改时间。"""
    names = _backup_names(out_dir)
    removed = []
    for name in names[:-keep] if keep > 0 else names:
        os.remove(os.path.join(out_dir, name))
        removed.append(name)
    return removed


def main(argv=None):
    parser = argparse.ArgumentParser(description="小马数据备份 / 校验 / 恢复")
    parser.add_argument("--out", default=DEFAULT_OUT, help="备份目录（默认 backups/）")
    parser.add_argument("--keep", type=int, default=KEEP, help="保留份数（默认 7）")
    parser.add_argument("--all-data", action="store_true", help="连 data/ 下的目录一起备份")
    parser.add_argument("--data-dir", metavar="DIR",
                        help="备份这个目录而不是仓库 data/（Docker 卷快照用）")
    parser.add_argument("--list", action="store_true", help="列出已有备份")
    parser.add_argument("--check", metavar="ZIP", help="校验一份备份")
    parser.add_argument("--restore", metavar="ZIP", help="从备份恢复")
    parser.add_argument("--to", metavar="DIR", help="恢复到指定目录（演练用，不动本机数据）")
    parser.add_argument("--yes", action="store_true", help="确认覆盖本机数据")
    parser.add_argument("--encrypt", action="store_true",
                        help="加密备份（口令取 %s 或 --passphrase-file）" % PASSPHRASE_ENV)
    parser.add_argument("--passphrase-file", metavar="FILE",
                        help="从文件读口令（建议 chmod 600；别写进命令行历史）")
    args = parser.parse_args(argv)

    passphrase = resolve_passphrase(passphrase_file=args.passphrase_file)

    if args.list:
        names = list_backups(args.out)
        if not names:
            print("还没有备份（目录：%s）" % args.out)
        for name in names:
            path = os.path.join(args.out, name)
            print("%-28s %6.1f MB" % (name, os.path.getsize(path) / 1024 / 1024))
        return 0

    if args.check:
        ok, problems = verify_archive(args.check, passphrase=passphrase)
        if ok:
            print("✅ 备份可用：%s" % args.check)
            return 0
        print("❌ 备份有问题：")
        for problem in problems:
            print("  - " + problem)
        return 1

    if args.restore:
        target = args.to or ROOT
        if args.to:
            os.makedirs(args.to, exist_ok=True)
        path = restore_archive(args.restore, target, force=args.yes,
                               passphrase=passphrase)
        print("✅ 已恢复到：%s" % path)
        if target != ROOT:
            print("演练完成：这是独立副本，本机数据没动。")
        else:
            print("记得重启 server.py 才会读回这些数据。")
        return 0

    if args.encrypt and not passphrase:
        raise SystemExit(
            "要做加密备份，先设 %s 或给 --passphrase-file" % PASSPHRASE_ENV
        )
    zip_path = create_backup(
        out_dir=args.out, keep=args.keep, all_data=args.all_data,
        data_dir=args.data_dir,
        passphrase=passphrase if args.encrypt else None,
    )
    ok, problems = verify_archive(zip_path, passphrase=passphrase)
    removed = prune(args.out, args.keep)
    print("✅ 备份：%s（%.1f MB）%s" % (
        zip_path, os.path.getsize(zip_path) / 1024 / 1024,
        "（已加密）" if is_encrypted(zip_path) else "",
    ))
    print("   校验：%s" % ("通过" if ok else "失败 " + "；".join(problems)))
    if removed:
        print("   清理旧备份：%s" % "、".join(removed))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
