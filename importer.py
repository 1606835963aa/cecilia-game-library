"""手动导入：把别处现成的游戏文件夹、压缩包导进游戏库。

- 游戏文件夹：挪进 games、复制一份进去，或者留在原处直接登记；
- 压缩包：解压进 games（只装着安卓版的解到安卓文件夹），原压缩包可以留着，也可以移到回收站；
- apk：和文件夹一样，放进安卓文件夹或留在原处；
- 拖进来的是游戏的 exe：按它所在的文件夹导入，并把它定为主程序；
- 拖进来的文件夹本身不是游戏：往里找游戏和压缩包，一个个列出来。

导入一个一个排队做，复制、解压都在后台线程里跑，界面上随时能看进度。
"""
import itertools
import os
import queue
import re
import shutil
import threading
from pathlib import Path

import auto_extract
import library
from auto_extract import (IMPORTING_DIR, PART_RAR, R_SPLIT, ROOT, SEVEN_SPLIT, SKIP_DIRS, Z_SPLIT, android_root,
                          archive_group, canon, clean_name, find_seven_zip, is_android_file, is_temp_file, load_config,
                          process_group, resolve_dir, save_config, unique)

# 和自动解压（另一个进程）分开用中转文件夹，两边同时解压也不会互相覆盖
IMPORT_WORK_DIR = ROOT / "data" / "import"
auto_extract.WORK_DIR = IMPORT_WORK_DIR

FOLDER_MODES = ("move", "copy", "link")  # 挪进去 / 复制一份 / 留在原处
FIND_DEPTH = 3       # 拖进来的文件夹不是游戏时，往里找几层
MAX_ITEMS = 200
KEEP_JOBS = 60       # 界面上留着的导入记录条数
COPY_CHUNK = 8 * 1024 * 1024
PROGRAM_DIRS = ("web", "data", "logs", ".git", "__pycache__")  # 游戏库程序自己的文件夹，不能导入


def games_root():
    return auto_extract.games_root(load_config())


def inside(path, roots):
    return any(path == r or r in path.parents for r in roots)


# ---------------------------------------------------------------- 识别
def first_volume(path):
    """拖进来的是分卷里的第 2、3 份时，找到第 1 份所在的那组。"""
    name = canon(path.name)
    sfx = re.fullmatch(r"(.+)\.\d+\.sfxv", name, re.I)
    if sfx:
        for sibling in path.parent.iterdir():
            if canon(sibling.name).lower() == sfx.group(1).lower() + ".exe":
                group = archive_group(sibling)
                if group and path in group["parts"]:
                    return group
        return None
    for pattern in (PART_RAR, SEVEN_SPLIT, Z_SPLIT, R_SPLIT):
        m = pattern.match(name)
        if m:
            break
    else:
        return None
    base = m.group("base").lower()
    try:
        siblings = sorted(p for p in path.parent.iterdir() if canon(p.name).lower().startswith(base))
    except OSError:
        return None
    for p in siblings:
        group = archive_group(p)
        if group and path in group["parts"]:
            return group
    return None


def known_names():
    """游戏库里已有游戏的名字（按文件夹名拆出来的，不受后来改名影响）。"""
    games = [g for g in library.list_games() if not g["is_wishlist"]]
    return {library.folder_key(g) for g in games} | {g["name"].lower() for g in games}


def archive_item(group):
    parts = group["parts"]
    size = sum(p.stat().st_size for p in parts if p.exists())
    return {
        "key": group["key"],
        "kind": "archive",
        "path": str(group["trigger"]),
        "name": group.get("name_override") or clean_name(group["trigger"]),
        "parts": len(parts),
        "size": size,
    }


def game_item(path, exe="", roots=()):
    g = library.describe(path, exe)
    if not g:
        return None
    item = {
        "key": str(path).lower(),
        "kind": "apk" if g["platform"] == "安卓" else "folder",
        "path": str(path),
        "name": g["name"],
        "version": g["version"],
        "engine": g["engine"],
        "exe": exe,
        "size": library.folder_size(path),
    }
    if library.find_by_path(path):
        item["blocked"] = "已经在游戏库里"
    elif inside(path, roots):
        item["in_library_dir"] = True  # 已经在 games 或安卓文件夹里，只要登记
    return item


def scan_folder(folder, roots):
    """文件夹本身不是游戏时，往里找游戏和压缩包。"""
    found = [game_item(g["path"], roots=roots) for g in library.find_games(folder, FIND_DEPTH)]
    found = [f for f in found if f]
    taken = [Path(f["path"]) for f in found]
    for dirpath, dirnames, filenames in os.walk(folder):
        here = Path(dirpath)
        if len(here.relative_to(folder).parts) >= FIND_DEPTH or any(t == here or t in here.parents for t in taken):
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith((".", "_"))]
        for fname in sorted(filenames):
            if is_temp_file(fname) or is_android_file(fname):
                continue
            group = archive_group(here / fname)
            if group:
                found.append(archive_item(group))
    return found


def inspect_path(raw, roots):
    path = Path(raw)
    try:
        path = path.resolve()
    except OSError:
        pass
    unknown = {"key": str(path).lower(), "kind": "unknown", "path": str(path), "name": path.name or str(path)}
    if path.is_dir():
        if path == ROOT or inside(path, [ROOT / d for d in PROGRAM_DIRS]):
            return [dict(unknown, blocked="这是游戏库自己的文件夹")]
        item = game_item(path, roots=roots)
        if item:
            return [item]
        found = scan_folder(path, roots)
        return found or [dict(unknown, blocked="没认出游戏：里面没有能运行的 exe，也没有压缩包")]
    if not path.is_file():
        return [dict(unknown, blocked="找不到这个文件")]
    low = path.name.lower()
    if low.endswith(".apk"):
        return [game_item(path, roots=roots)]
    if is_android_file(path.name):
        return [dict(unknown, blocked="这种安卓安装包要手动装，游戏库里放不了")]
    if low.endswith(".exe"):
        group = archive_group(path)  # 自解压包也是 exe，但游戏 exe 不会被认成压缩包
        if not group:
            item = game_item(path.parent, exe=path.name, roots=roots)
            return [item or dict(unknown, blocked="没认出游戏")]
    group = archive_group(path) or first_volume(path)
    if group:
        return [archive_item(group)]
    return [dict(unknown, blocked="不是压缩包，也不是游戏文件夹")]


def inspect(paths):
    """看看拖进来、选中的东西都是什么，列出能导入的条目。"""
    roots, _ = library.watch_roots()
    items, seen = [], set()
    names = known_names()
    for raw in paths:
        for item in inspect_path(raw, roots):
            if item["key"] in seen:
                continue
            seen.add(item["key"])
            if not item.get("blocked") and library.parse_name(item["name"])[0].lower() in names:
                item["duplicate"] = True
            items.append(item)
            if len(items) >= MAX_ITEMS:
                return items
    return items


# ---------------------------------------------------------------- 执行
class Importer:
    """导入任务排队一个个做。界面轮询 jobs() 看进度。"""

    def __init__(self, log, on_added):
        self.log = log
        self.on_added = on_added  # 新入库的游戏编号列表，交给后台去找资料
        self.lock = threading.Lock()
        self.queue = queue.Queue()
        self.records = []
        self.ids = itertools.count(1)

    def cleanup(self):
        """上次没做完的导入留下的中转文件夹（原文件都还在原处，可以直接清掉）。"""
        shutil.rmtree(games_root() / IMPORTING_DIR, ignore_errors=True)
        shutil.rmtree(IMPORT_WORK_DIR, ignore_errors=True)

    def start(self, items, mode="move", delete_archive=False):
        if mode not in FOLDER_MODES:
            raise ValueError("不认识的导入方式")
        jobs = []
        with self.lock:
            for item in items:
                job = {
                    "id": next(self.ids),
                    "kind": item.get("kind"),
                    "path": str(item.get("path") or ""),
                    "name": item.get("name") or Path(str(item.get("path"))).name,
                    "exe": item.get("exe") or "",
                    "password": (item.get("password") or "").strip(),
                    "mode": "link" if item.get("in_library_dir") else mode,
                    "delete_archive": bool(delete_archive),
                    "state": "waiting",
                    "message": "排队中",
                    "progress": None,
                }
                if job["kind"] not in ("folder", "apk", "archive"):
                    continue
                jobs.append(job)
                self.records.append(job)
            finished = [j for j in self.records if j["state"] in ("done", "failed")]
            for j in finished[:-KEEP_JOBS]:
                self.records.remove(j)
        for job in jobs:
            self.queue.put(job)
        return [j["id"] for j in jobs]

    def jobs(self):
        with self.lock:
            return [{k: v for k, v in j.items() if k != "password"} for j in self.records]

    def busy(self):
        with self.lock:
            return any(j["state"] in ("waiting", "running") for j in self.records)

    def clear(self):
        """界面上关掉导入窗口时，把做完的记录清掉。"""
        with self.lock:
            self.records = [j for j in self.records if j["state"] in ("waiting", "running")]

    def update(self, job, **fields):
        with self.lock:
            job.update(fields)

    def loop(self):
        while True:
            job = self.queue.get()
            self.update(job, state="running", message="准备中")
            try:
                if job["kind"] == "archive":
                    self.import_archive(job)
                else:
                    self.import_game(job)
            except Exception as e:  # noqa: BLE001 一个出错不影响后面的
                self.log(f"导入失败：{job['name']}：{e}")
                self.update(job, state="failed", message=str(e) or "导入失败", progress=None,
                            need_password=getattr(e, "need_password", False))

    # ---------- 游戏文件夹、apk
    def import_game(self, job):
        src = Path(job["path"])
        if not src.exists():
            raise ValueError("找不到了，可能已经被挪走")
        target = src
        if job["mode"] != "link":
            dest_root = android_root(load_config()) if job["kind"] == "apk" else games_root()
            dest_root.mkdir(parents=True, exist_ok=True)
            target = unique(dest_root / src.name)
            self.transfer(job, src, target, dest_root)
        self.update(job, message="登记中", progress=None)
        gid, new = library.add_game(target, job["exe"])
        if new:
            self.on_added([gid])
        folder = "安卓文件夹" if job["kind"] == "apk" else " games 文件夹"
        done = {"move": f"已挪进{folder}", "copy": f"已复制进{folder}", "link": "已登记，文件留在原处"}[job["mode"]]
        done += job.get("warning", "")
        self.log(f"导入：{job['name']}（{done}）")
        self.update(job, state="done", message=done if new else "游戏库里本来就有", game_id=gid)

    def transfer(self, job, src, target, dest_root):
        same_drive = os.path.splitdrive(str(src))[0].lower() == os.path.splitdrive(str(dest_root))[0].lower()
        if job["mode"] == "move" and same_drive:
            self.update(job, message="正在挪过去")
            try:
                os.rename(src, target)
            except OSError as e:
                raise ValueError(f"挪不过去，可能有文件正被别的程序占用：{e.strerror or e}")
            return
        # 先复制到中转文件夹，全部复制完再改名放好，复制到一半时扫描不会把它当成游戏
        staging = dest_root / IMPORTING_DIR / str(job["id"])
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        total = max(library.folder_size(src), 1)
        copied = 0

        def copy(s, d):
            # 分块复制，大文件复制到一半也能看到进度
            nonlocal copied
            with open(s, "rb") as fi, open(d, "wb") as fo:
                while chunk := fi.read(COPY_CHUNK):
                    fo.write(chunk)
                    copied += len(chunk)
                    self.update(job, progress=min(copied / total, 1))
            shutil.copystat(s, d)

        self.update(job, message="正在复制", progress=0)
        try:
            temp = staging / src.name
            if src.is_dir():
                shutil.copytree(src, temp, copy_function=copy)
            else:
                copy(src, temp)
            os.rename(temp, target)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            prune(staging.parent)
        if job["mode"] == "move":
            self.update(job, message="删除原来的文件", progress=None)
            try:
                if src.is_dir():
                    shutil.rmtree(src)
                else:
                    src.unlink()
            except OSError:
                self.update(job, warning="（原来的文件没删干净，可能有文件正被占用，请自己删一下）")

    # ---------- 压缩包
    def import_archive(self, job):
        trigger = Path(job["path"])
        group = archive_group(trigger)
        if not group:
            raise ValueError("找不到这个压缩包了")
        cfg = load_config()
        seven = find_seven_zip(cfg)
        if not seven:
            raise ValueError("没找到 7-Zip，装上 7-Zip 才能解压")
        saved = cfg.get("passwords") or []
        passwords = ([job["password"]] if job["password"] else []) + saved
        root = games_root()
        staging = root / IMPORTING_DIR / str(job["id"])
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        self.update(job, message="正在解压")
        try:
            ok, note = process_group(seven, group, cfg, passwords, [staging], into=staging)
            if not ok:
                need = "密码" in note
                raise ImportFailed(note, need_password=need)
            placed = [str(auto_extract.publish_game(p, root)) for p in list(staging.iterdir())]
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            prune(staging.parent)
        if job["password"] and job["password"] not in saved:
            save_config({"passwords": saved + [job["password"]]})

        self.update(job, message="登记中")
        result = library.scan()
        if result["added_ids"]:
            self.on_added(result["added_ids"])
        # 后台每分钟的自动扫描可能抢先一步入库，所以按解压出来的位置找，不只看这次扫描新加的
        found = [g for p in placed for g in library.games_under(p)]
        if found:
            message = "已入库：" + "、".join(g["name"] for g in found)
        elif not placed:
            message = "解压好了，放进了安卓文件夹"
        else:
            message = f"解压好了，但没认出游戏，文件在 {placed[0]}"
        if job["delete_archive"]:
            try:
                for part in group["parts"]:
                    library.send_to_recycle_bin(part)
                message += "，压缩包已移到回收站"
            except (ValueError, OSError) as e:
                message += f"（压缩包没删掉：{e}）"
        self.log(f"导入：{job['name']}（{message}）")
        self.update(job, state="done", message=message, game_id=found[0]["id"] if len(found) == 1 else None)

    def retry(self, job_id, password):
        with self.lock:
            old = next((j for j in self.records if j["id"] == job_id), None)
        if not old or old["state"] != "failed":
            raise ValueError("这条导入不能重试")
        item = dict(old, password=password)
        with self.lock:
            self.records.remove(old)
        return self.start([item], old["mode"], old["delete_archive"])


class ImportFailed(Exception):
    def __init__(self, message, need_password=False):
        super().__init__(message)
        self.need_password = need_password


def prune(folder):
    """中转文件夹空了就删掉。"""
    try:
        folder.rmdir()
    except OSError:
        pass
