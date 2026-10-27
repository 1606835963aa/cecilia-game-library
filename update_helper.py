"""独立更新助手：主程序退出后替换程序文件，启动失败时还原。"""
import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import zipfile
from pathlib import Path, PurePosixPath
from contextlib import closing

NAME = "塞西莉亚的游戏库"
PACKAGE = "CeciliaGameLibrary-portable.zip"
MANIFEST = "update-manifest.json"
OWNED = (NAME + ".exe", "自动解压.exe", "更新助手.exe", "_internal", "7-Zip", "LICENSE", MANIFEST)
REQUIRED = {NAME + ".exe", "自动解压.exe", "更新助手.exe", "_internal/web/index.html",
            "_internal/web/app.js", "7-Zip/7z.exe", "7-Zip/7z.dll", "7-Zip/License.txt", "LICENSE"}
MAX_PACKAGE = 1024 * 1024 * 1024
MAX_EXPANDED = 2 * MAX_PACKAGE
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r"v?\d{1,6}(?:\.\d{1,6}){1,3}", value):
        raise ValueError("版本号格式不正确")
    parts = tuple(int(n) for n in value.removeprefix("v").split("."))
    return parts + (0,) * (4 - len(parts))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def active_request(root):
    try:
        request = read_json(Path(root) / "tmp/updates/active.json")
        return request if time.time() < float(request["expires"]) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def safe_relative(name):
    parts = PurePosixPath(name).parts
    if (not parts or "\\" in name or name.startswith("/") or any(
            p in (".", "..") or p.endswith((".", " ")) or ":" in p or
            re.search(r'[<>"|?*\x00-\x1f]', p) or
            re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", p) for p in parts)):
        raise ValueError("更新包里有不安全的文件路径")
    if parts[0] not in OWNED or (parts[0] not in ("_internal", "7-Zip") and len(parts) != 1):
        raise ValueError("更新包包含程序目录以外的文件")
    return "/".join(parts)


def stage_package(archive, destination, expected_version, expected_hash):
    """只提取清单列出的程序文件，游戏、设置和数据库不接受来自更新包的内容。"""
    archive, destination = Path(archive), Path(destination)
    if not SHA256.fullmatch(expected_hash) or digest(archive) != expected_hash:
        raise ValueError("更新包校验没通过，请重新下载")
    with zipfile.ZipFile(archive) as zf:
        if len(zf.infolist()) > 50000:
            raise ValueError("更新包文件过多")
        entries, seen = {}, set()
        expanded = 0
        for info in zf.infolist():
            name = info.filename
            if not name.startswith(NAME + "/"):
                raise ValueError("更新包的文件夹名称不正确")
            relative = name[len(NAME) + 1:].rstrip("/")
            if info.is_dir() and relative in ("", "downloads", "games"):
                continue
            relative = safe_relative(relative)
            key = relative.casefold()
            if key in seen or stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1:
                raise ValueError("更新包里有重复、链接或加密文件")
            seen.add(key)
            if not info.is_dir():
                entries[relative] = info
                expanded += info.file_size
        if expanded > MAX_EXPANDED or MANIFEST not in entries or entries[MANIFEST].file_size > 8 * 1024 * 1024:
            raise ValueError("更新包大小或清单不正确")
        manifest = json.loads(zf.read(entries[MANIFEST]))
        if manifest.get("schema") != 1 or version_tuple(manifest.get("version")) != version_tuple(expected_version):
            raise ValueError("更新包版本和发布版本不一致")
        files = manifest.get("files")
        if not isinstance(files, dict) or not REQUIRED.issubset(files) or set(entries) != set(files) | {MANIFEST}:
            raise ValueError("更新包缺少必要文件，或包含未列入清单的文件")
        for name, record in files.items():
            if safe_relative(name) != name or name == MANIFEST or not isinstance(record, dict):
                raise ValueError("更新文件清单不正确")
            if (not SHA256.fullmatch(str(record.get("sha256", ""))) or
                    type(record.get("size")) is not int or record["size"] != entries[name].file_size):
                raise ValueError("更新文件大小或校验值不正确")
        if shutil.disk_usage(destination.parent).free < expanded + 64 * 1024 * 1024:
            raise ValueError("空间不足，请腾出一些空间再更新")
        destination.mkdir()
        try:
            for name, info in entries.items():
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                if name != MANIFEST and digest(target) != files[name]["sha256"]:
                    raise ValueError("更新文件校验没通过，请重新下载")
        except Exception:
            shutil.rmtree(destination)
            raise
    return manifest


def remove_owned(path, root):
    path, root = Path(path), Path(root).resolve()
    if path.parent.resolve() != root or path.name not in OWNED or path.resolve().parent != root:
        raise ValueError("不能清理程序目录以外的文件")
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def restore(root, job, journal):
    root, job = Path(root), Path(job)
    for step in reversed(journal["steps"]):
        name = step["name"]
        backup, staged, target = job / "backup" / name, job / "staged" / name, root / name
        if backup.exists():
            remove_owned(target, root)
            os.replace(backup, target)
        elif not step["existed"] and not staged.exists():
            remove_owned(target, root)
    database = root / "data/library.db"
    if journal.get("database_saved"):
        for suffix in ("-wal", "-shm", "-journal"):
            database.with_name(database.name + suffix).unlink(missing_ok=True)
        if journal["database_existed"]:
            shutil.copy2(job / "library.db", database)
        else:
            database.unlink(missing_ok=True)


def replace_program(root, job):
    root, job = Path(root), Path(job)
    (job / "backup").mkdir()
    database = root / "data/library.db"
    journal = {"steps": [], "database_existed": database.exists(), "database_saved": False}
    if database.exists():
        with closing(sqlite3.connect(database)) as source, closing(sqlite3.connect(job / "library.db")) as output:
            source.backup(output)
    journal["database_saved"] = True
    write_json(job / "journal.json", journal)
    for name in OWNED:
        target = root / name
        if target.exists() and (target.resolve().parent != root.resolve() or
                              getattr(target.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError("程序文件夹里有链接目录，请使用普通文件夹更新")
        step = {"name": name, "existed": target.exists()}
        journal["steps"].append(step)
        write_json(job / "journal.json", journal)
        if step["existed"]:
            os.replace(target, job / "backup" / name)
        os.replace(job / "staged" / name, target)
    return journal


def process_handle(pid):
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(0x100000, False, int(pid))
    if not handle and ctypes.get_last_error() != 87:
        raise OSError("无法等待正在运行的程序退出")
    return kernel, handle


def restart_environment():
    environment = os.environ.copy()
    environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    for name in ("TEMP", "TMP"):
        previous = environment.pop("CECILIA_PREVIOUS_" + name, None)
        if previous is not None:
            if previous:
                environment[name] = previous
            else:
                environment.pop(name, None)
    return environment


def restart(root, plan, updated=False):
    command = [str(root / (NAME + ".exe"))]
    if plan.get("browser"):
        command.append("--browser")
    if updated:
        command += ["--update-job", str(plan["plan_path"])]
    return subprocess.Popen(command, cwd=root, env=restart_environment(), creationflags=subprocess.CREATE_NO_WINDOW)


def apply_update(plan_path):
    plan_path = Path(plan_path).resolve()
    plan = read_json(plan_path)
    root = Path(plan["root"]).resolve()
    job = plan_path.parent
    if (plan_path.name != "plan.json" or not job.is_relative_to(root) or job.parent != (root / "tmp/updates").resolve() or
            not re.fullmatch(r"[0-9a-f]{32}", job.name)):
        raise ValueError("更新任务的位置不正确")
    active = active_request(root)
    if not active or active.get("job") != job.name:
        raise ValueError("更新任务已经过期，请重新检查更新")
    handles, journal, candidate, restored = [], None, None, True
    request_path = root / "tmp/updates/active.json"
    result_path = root / "data/software-update.json"
    try:
        for pid in plan["wait_pids"]:
            handles.append(process_handle(pid))
        stage_package(job / PACKAGE, job / "staged", plan["version"], plan["sha256"])
        write_json(job / "helper-ready.json", {"ready": True})
        deadline = time.monotonic() + 120
        for kernel, handle in handles:
            while handle and kernel.WaitForSingleObject(handle, 200) != 0:
                pending = active_request(root)
                if not pending or pending.get("job") != job.name:
                    raise ValueError("更新任务已取消")
                if time.monotonic() >= deadline:
                    raise TimeoutError("程序还没退出，这次更新已取消")
        pending = active_request(root)
        if not pending or pending.get("job") != job.name or not (job / "parent-exiting.json").is_file():
            raise ValueError("更新任务已取消")
        journal = replace_program(root, job)
        plan["plan_path"] = str(plan_path)
        candidate = restart(root, plan, updated=True)
        deadline = time.monotonic() + 90
        confirmed_at = None
        while time.monotonic() < deadline:
            if candidate.poll() is not None:
                raise RuntimeError("新版没能正常启动")
            if (job / "booted.json").is_file():
                booted = read_json(job / "booted.json")
                if booted.get("token") == plan["token"] and booted.get("version") == plan["version"]:
                    if confirmed_at is None:
                        confirmed_at = time.monotonic()
                    if time.monotonic() - confirmed_at < 2:
                        time.sleep(0.2)
                        continue
                    write_json(result_path, {"phase": "done", "version": plan["version"],
                                             "message": f"已更新到 {plan['version']}"})
                    request_path.unlink(missing_ok=True)
                    write_json(job / "finished.json", {"ok": True, "helper_pid": os.getpid()})
                    return
            time.sleep(0.2)
        raise TimeoutError("新版启动超时")
    except Exception as error:
        if candidate is not None and candidate.poll() is None:
            candidate.terminate()
            candidate.wait(timeout=15)
        try:
            if journal is None and (job / "journal.json").is_file():
                journal = read_json(job / "journal.json")
            if journal is not None:
                restore(root, job, journal)
        except Exception as restore_error:
            restored = False
            error = f"{error}；还原受阻：{restore_error}，原程序备份在 {job / 'backup'}"
        message = f"更新没完成，{'已保留原版本' if restored else '请保留备份文件夹'}：{error}"
        write_json(result_path, {"phase": "error", "version": plan["version"], "message": message})
        request_path.unlink(missing_ok=True)
        if restored:
            write_json(job / "finished.json", {"ok": False, "helper_pid": os.getpid()})
        # 主程序确认助手就绪后才退出；准备阶段出错时不能再开一个窗口。
        if restored and (job / "parent-exiting.json").exists():
            restart(root, plan)
        raise
    finally:
        for kernel, handle in handles:
            if handle:
                kernel.CloseHandle(handle)
        if restored and not active_request(root):
            for extractor in plan.get("extractors", []):
                kernel, handle = process_handle(extractor["pid"])
                try:
                    if not handle or kernel.WaitForSingleObject(handle, 0) == 0:
                        subprocess.Popen([str(root / "自动解压.exe"), *extractor["args"]], cwd=root,
                                         env=restart_environment(), creationflags=subprocess.CREATE_NEW_CONSOLE)
                finally:
                    if handle:
                        kernel.CloseHandle(handle)


def main():
    parser = argparse.ArgumentParser(description="游戏库更新助手")
    parser.add_argument("--apply", required=True, type=Path)
    args = parser.parse_args()
    try:
        apply_update(args.apply)
    except Exception as error:
        log = args.apply.parent / "error.txt"
        log.write_text(str(error), encoding="utf-8")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
