"""备份与恢复：把「丢了就回不来」的东西打成一个 zip。

为什么不是直接拷 data/：服务在跑，`*.db` 可能正写到一半，拷出来是坏的。
这里统一走 sqlite 的 backup API（在线热备），恢复时再校验 sha256 与 integrity_check。

为什么带上 .env：VAPID 私钥在里面，丢了所有推送订阅全部作废，手机要重新开一遍通知。

用法：
    python scripts/backup.py                     # 备份到 backups/，保留 7 份
    python scripts/backup.py --list              # 看现有备份
    python scripts/backup.py --check <zip>       # 校验一份备份（sha256 + integrity_check）
    python scripts/backup.py --restore <zip> --to D:/tmp/drill   # 恢复到指定目录（演练用）
    python scripts/backup.py --restore <zip> --yes               # 恢复回本机（会先自备份）
    python scripts/backup.py --all-data          # 连 data/ 下的目录一起备份（chroma、HA 配置）

默认只备「核心状态」：`.env` + `data/` 顶层的非日志文件（记忆、事实、任务、物品、推送订阅）。
`data/homeassistant` 是 HA 的运行时目录，由 HA 自己的快照负责，默认不掺进来。
"""
import argparse
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

MANIFEST = "manifest.json"


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


def collect_sources(root=ROOT, all_data=False):
    """返回 [(zip 内名字, 绝对路径)]。"""
    items = []
    env_path = os.path.join(root, ".env")
    if os.path.exists(env_path):
        items.append((".env", env_path))
    data_dir = os.path.join(root, "data")
    if not os.path.isdir(data_dir):
        return items
    for name in sorted(os.listdir(data_dir)):
        path = os.path.join(data_dir, name)
        if os.path.isdir(path):
            if not all_data or name in SKIP_DIRS:
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


def create_backup(root=ROOT, out_dir=DEFAULT_OUT, keep=KEEP, all_data=False, now=None):
    """打一份备份，返回 zip 路径。内容先落到临时目录，再整体写入，避免半份文件。"""
    now = now or datetime.now()
    os.makedirs(out_dir, exist_ok=True)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    zip_path = os.path.join(out_dir, "xiaoma-%s.zip" % stamp)

    sources = collect_sources(root, all_data=all_data)
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
                "items": entries,
            }
            zf.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return zip_path


def verify_archive(zip_path):
    """返回 (ok, problems)。校验 sha256 与每个 .db 的 integrity_check。"""
    problems = []
    if not os.path.exists(zip_path):
        return False, ["文件不存在：%s" % zip_path]
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


def restore_archive(zip_path, target_root=ROOT, force=False):
    """把备份解回目标目录；不覆盖正在运行的服务数据，除非 force。"""
    ok, problems = verify_archive(zip_path)
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
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if name == MANIFEST:
                continue
            zf.extract(name, target_root)
    return target_root


def list_backups(out_dir=DEFAULT_OUT):
    if not os.path.isdir(out_dir):
        return []
    names = [n for n in os.listdir(out_dir) if n.startswith("xiaoma-") and n.endswith(".zip")]
    return sorted(names)


def prune(out_dir=DEFAULT_OUT, keep=KEEP):
    """只留最近 keep 份；按文件名里的时间戳排序，不依赖文件修改时间。"""
    names = list_backups(out_dir)
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
    parser.add_argument("--list", action="store_true", help="列出已有备份")
    parser.add_argument("--check", metavar="ZIP", help="校验一份备份")
    parser.add_argument("--restore", metavar="ZIP", help="从备份恢复")
    parser.add_argument("--to", metavar="DIR", help="恢复到指定目录（演练用，不动本机数据）")
    parser.add_argument("--yes", action="store_true", help="确认覆盖本机数据")
    args = parser.parse_args(argv)

    if args.list:
        names = list_backups(args.out)
        if not names:
            print("还没有备份（目录：%s）" % args.out)
        for name in names:
            path = os.path.join(args.out, name)
            print("%-28s %6.1f MB" % (name, os.path.getsize(path) / 1024 / 1024))
        return 0

    if args.check:
        ok, problems = verify_archive(args.check)
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
        path = restore_archive(args.restore, target, force=args.yes)
        print("✅ 已恢复到：%s" % path)
        if target != ROOT:
            print("演练完成：这是独立副本，本机数据没动。")
        else:
            print("记得重启 server.py 才会读回这些数据。")
        return 0

    zip_path = create_backup(out_dir=args.out, keep=args.keep, all_data=args.all_data)
    ok, problems = verify_archive(zip_path)
    removed = prune(args.out, args.keep)
    print("✅ 备份：%s（%.1f MB）" % (zip_path, os.path.getsize(zip_path) / 1024 / 1024))
    print("   校验：%s" % ("通过" if ok else "失败 " + "；".join(problems)))
    if removed:
        print("   清理旧备份：%s" % "、".join(removed))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
