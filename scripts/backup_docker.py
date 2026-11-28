"""备份跑在 Docker 里的那份数据（命名卷），并做恢复演练。

为什么单独一个脚本：`scripts/backup.py` 扫的是仓库下的 `data/`，而生产部署把数据
放在命名卷里（`opencode-my_xiaoma-data:/data`），那份从来没进过备份 —— 容器一删
记忆、任务、事实全没。这里负责把卷里的库热备出来，再交给 `backup.py` 打包
（复用它的 manifest / sha256 / integrity_check / 保留份数）。

为什么用临时容器而不是 `docker cp`：服务在跑，`docker cp` 拷到的是写到一半的文件。
这里在容器里用 sqlite 的 backup API 逐个库做在线快照，再把结果 tar 出来；
宿主目录不用挂进容器，省掉 Linux 上 uid 对不上的麻烦。

用法：
    python scripts/backup_docker.py                  # 自动找小马的卷，备份到 backups/
    python scripts/backup_docker.py --list           # 看有哪些候选卷
    python scripts/backup_docker.py --volume NAME    # 指定卷
    python scripts/backup_docker.py --check ZIP      # 校验
    python scripts/backup_docker.py --restore ZIP --volume NAME --yes
                                                     # 恢复到卷（会先停用它的容器）

恢复演练建议先建一个空卷：`docker volume create xiaoma-restore-test`，
恢复到它，再 `docker run --rm -v xiaoma-restore-test:/data <镜像> python -c ...`
查条数；确认后再恢复到生产卷。
"""
import argparse
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import backup as backup_mod  # noqa: E402

DEFAULT_IMAGE = "xiaoma:local"
# 容器里以非 root 跑，/tmp 一定可写；不要往容器根目录 mkdir
WORK = "/tmp/xiaoma-backup"

SNAPSHOT_PY = r'''
import os, shutil, sqlite3

src, dst = "/data", "%(work)s/out"
all_data = %(all_data)s
os.makedirs(dst, exist_ok=True)
for name in sorted(os.listdir(src)):
    path = os.path.join(src, name)
    if os.path.isdir(path):
        if name == "backups" or (not all_data and name not in ("print", "spaces")):
            continue
        for base, _dirs, files in os.walk(path):
            for filename in files:
                full = os.path.join(base, filename)
                target = os.path.join(dst, os.path.relpath(full, src))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copy2(full, target)
        continue
    if name.endswith(".log"):
        continue
    target = os.path.join(dst, name)
    if name.endswith(".db"):
        try:
            with sqlite3.connect(path) as source, sqlite3.connect(target) as tgt:
                source.backup(tgt)
            continue
        except sqlite3.DatabaseError:
            pass
    shutil.copy2(path, target)
''' % {"work": WORK, "all_data": "False"}


def _snapshot_py(all_data):
    return SNAPSHOT_PY.replace("all_data = False", "all_data = %r" % bool(all_data))


def _run(cmd, binary=False, timeout=300):
    result = subprocess.run(
        cmd, capture_output=True, timeout=timeout,
    )
    if result.returncode != 0:
        detail = (result.stderr or b"").decode("utf-8", "replace")[-600:]
        raise RuntimeError("命令失败（%s）：%s" % (" ".join(cmd[:3]), detail))
    return result.stdout if binary else result.stdout.decode("utf-8", "replace")


def list_volumes():
    out = _run(["docker", "volume", "ls", "-q"])
    return [line.strip() for line in out.splitlines() if line.strip()]


def pick_volume(name=None, volumes=None):
    """没指定就挑 `*xiaoma*data*`；有多个候选时报错让用户选，别瞎猜。"""
    if name:
        return name
    volumes = list_volumes() if volumes is None else volumes
    candidates = [v for v in volumes if "xiaoma" in v and "data" in v]
    if not candidates:
        raise SystemExit(
            "没找到小马的数据卷。用 --list 看现有卷，或 --volume 指定。"
        )
    if len(candidates) > 1:
        raise SystemExit(
            "有多个候选卷，用 --volume 指定一个：%s" % "、".join(candidates)
        )
    return candidates[0]


def snapshot(volume, image=DEFAULT_IMAGE, all_data=False, timeout=300):
    """把卷快照 tar 出来解到临时目录，返回目录（调用方负责删）。"""
    script = _snapshot_py(all_data)
    shell = "python - <<'PY'\n%s\nPY\ntar -cf - -C %s/out ." % (script, WORK)
    data = _run([
        "docker", "run", "--rm",
        "-v", "%s:/data:ro" % volume,
        image, "sh", "-c", shell,
    ], binary=True, timeout=timeout)
    if not data:
        raise RuntimeError("快照是空的：%s" % volume)
    tmp_dir = tempfile.mkdtemp(prefix="xiaoma-volume-")
    tar_path = os.path.join(tmp_dir, "snapshot.tar")
    with open(tar_path, "wb") as fh:
        fh.write(data)
    dest = os.path.join(tmp_dir, "data")
    os.makedirs(dest, exist_ok=True)
    _safe_extract(tar_path, dest)
    os.remove(tar_path)
    return tmp_dir


def _safe_extract(tar_path, dest):
    """只解普通文件，挡住 ../ 之类的路径穿越；卷里不该有软链。"""
    with tarfile.open(tar_path) as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            name = member.name.lstrip("./")
            if not name or name.startswith("/") or ".." in name.split("/"):
                continue
            target = os.path.join(dest, name)
            folder = os.path.dirname(target)
            if folder:
                os.makedirs(folder, exist_ok=True)
            source = tar.extractfile(member)
            if source is None:
                continue
            with source, open(target, "wb") as fh:
                shutil.copyfileobj(source, fh)


def _safe_unzip(zip_path, dest):
    """同 `_safe_extract`：只解文件，挡住 ../ 和绝对路径。"""
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename.lstrip("./")
            if not name or name.startswith("/") or ".." in name.split("/"):
                continue
            target = os.path.join(dest, name)
            folder = os.path.dirname(target)
            if folder:
                os.makedirs(folder, exist_ok=True)
            with zf.open(info) as source, open(target, "wb") as fh:
                shutil.copyfileobj(source, fh)


def container_using(volume):
    out = _run(["docker", "ps", "-q", "--filter", "volume=%s" % volume])
    return [line.strip() for line in out.splitlines() if line.strip()]


def restore(zip_path, volume, image=DEFAULT_IMAGE, with_env=False, yes=False,
            passphrase=None):
    """把备份写回卷。正在用这个卷的容器要先停，写完再起。"""
    ok, problems = backup_mod.verify_archive(zip_path, passphrase=passphrase)
    if not ok:
        raise SystemExit("备份不可用：\n  " + "\n  ".join(problems))

    running = container_using(volume)
    if running and not yes:
        raise SystemExit(
            "卷 %s 正被容器使用（%s）。确认要覆盖就加 --yes，脚本会先停容器再恢复。"
            % (volume, "、".join(running))
        )

    tmp_dir = tempfile.mkdtemp(prefix="xiaoma-restore-")
    try:
        with backup_mod.open_archive(zip_path, passphrase) as archive:
            _safe_unzip(archive, tmp_dir)
        data_dir = os.path.join(tmp_dir, "data")
        if not os.path.isdir(data_dir):
            raise SystemExit("备份里没有 data/ 目录，不是小马的备份？")

        for name in running:
            _run(["docker", "stop", name])
        try:
            # `--entrypoint sh` 不能省：D86 之后镜像的 entrypoint 会把命令降权成
            # xiaoma/work 两个 uid（执行通道隔离），`--user root` 只决定 entrypoint
            # 自己是谁——正文命令照样是 xiaoma，`chown -R /data` 和读 /data/home
            # 都会失败（真机恢复时踩到：Operation not permitted）
            _run([
                "docker", "run", "--rm", "--user", "root", "--entrypoint", "sh",
                "-v", "%s:/data" % volume,
                "-v", "%s:/in:ro" % data_dir,
                image, "-c",
                "cp -a /in/. /data/ && chown -R 10001:10001 /data",
            ])
        finally:
            for name in running:
                _run(["docker", "start", name])

        if with_env:
            env_src = os.path.join(tmp_dir, ".env")
            if os.path.exists(env_src):
                shutil.copy2(env_src, os.path.join(ROOT, ".env"))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return volume


def main(argv=None):
    parser = argparse.ArgumentParser(description="备份 / 恢复 Docker 里的小马数据卷")
    parser.add_argument("--volume", help="数据卷名（默认自动找 *xiaoma*data*）")
    parser.add_argument("--image", default=DEFAULT_IMAGE, help="用来跑快照的镜像")
    parser.add_argument("--out", default=backup_mod.DEFAULT_OUT, help="备份目录")
    parser.add_argument("--keep", type=int, default=backup_mod.KEEP, help="保留份数")
    parser.add_argument("--all-data", action="store_true", help="连子目录一起备份")
    parser.add_argument("--list", action="store_true", help="列出候选卷")
    parser.add_argument("--check", metavar="ZIP", help="校验一份备份")
    parser.add_argument("--restore", metavar="ZIP", help="恢复到卷")
    parser.add_argument("--with-env", action="store_true",
                        help="恢复时一并覆盖仓库 .env（换机器时慎用）")
    parser.add_argument("--yes", action="store_true", help="确认覆盖正在使用的卷")
    parser.add_argument("--encrypt", action="store_true",
                        help="加密备份（口令取 %s 或 --passphrase-file）"
                             % backup_mod.PASSPHRASE_ENV)
    parser.add_argument("--passphrase-file", metavar="FILE",
                        help="从文件读口令（建议 chmod 600）")
    args = parser.parse_args(argv)

    passphrase = backup_mod.resolve_passphrase(
        passphrase_file=args.passphrase_file
    )

    if args.list:
        for name in list_volumes():
            mark = "  <- 小马" if "xiaoma" in name and "data" in name else ""
            print(name + mark)
        return 0

    if args.check:
        ok, problems = backup_mod.verify_archive(args.check, passphrase=passphrase)
        if ok:
            print("✅ 备份可用：%s" % args.check)
            return 0
        print("❌ 备份有问题：")
        for problem in problems:
            print("  - " + problem)
        return 1

    volume = pick_volume(args.volume)

    if args.restore:
        restore(args.restore, volume, image=args.image,
                with_env=args.with_env, yes=args.yes, passphrase=passphrase)
        print("✅ 已恢复到卷：%s" % volume)
        print("   容器已经按原状态停/起；没在跑的记得手动 up。")
        return 0

    if args.encrypt and not passphrase:
        raise SystemExit(
            "要做加密备份，先设 %s 或给 --passphrase-file"
            % backup_mod.PASSPHRASE_ENV
        )
    tmp_dir = snapshot(volume, image=args.image, all_data=args.all_data)
    try:
        zip_path = backup_mod.create_backup(
            out_dir=args.out, all_data=args.all_data,
            data_dir=os.path.join(tmp_dir, "data"),
            note="docker:%s" % volume,
            passphrase=passphrase if args.encrypt else None,
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    ok, problems = backup_mod.verify_archive(zip_path, passphrase=passphrase)
    removed = backup_mod.prune(args.out, args.keep)
    print("✅ 备份：%s（%.1f MB，卷 %s）%s" % (
        zip_path, os.path.getsize(zip_path) / 1024 / 1024, volume,
        "（已加密）" if backup_mod.is_encrypted(zip_path) else "",
    ))
    print("   校验：%s" % ("通过" if ok else "失败 " + "；".join(problems)))
    if removed:
        print("   清理旧备份：%s" % "、".join(removed))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
