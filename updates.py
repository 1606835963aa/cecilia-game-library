"""从本项目 GitHub 正式发布中检查、下载并安排更新。"""
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

import certifi
import psutil

from app_version import VERSION
from update_helper import MAX_PACKAGE, PACKAGE, SHA256, active_request, read_json, version_tuple, write_json

REPOSITORY = "1606835963aa/cecilia-game-library"
RELEASES_URL = f"https://github.com/{REPOSITORY}/releases"
LATEST_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
CHECK_INTERVAL = 6 * 60 * 60
IDLE_SECONDS = 60
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


class GitHubRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, url):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "https" or parsed.username or parsed.port not in (None, 443) or
                parsed.hostname not in ("github.com", "api.github.com", "release-assets.githubusercontent.com",
                                        "objects.githubusercontent.com")):
            raise ValueError("更新下载地址不正确")
        return super().redirect_request(request, fp, code, message, headers, url)


def open_url(url):
    request = urllib.request.Request(url, headers={"User-Agent": f"CeciliaGameLibrary/{VERSION}",
                                                  "Accept": "application/vnd.github+json"})
    opener = urllib.request.build_opener(GitHubRedirect(), urllib.request.HTTPSHandler(context=SSL_CONTEXT))
    return opener.open(request, timeout=30)


def release_info(data):
    if data.get("draft") or data.get("prerelease"):
        raise ValueError("这个发布还不是正式版本")
    version = str(data.get("tag_name", "")).removeprefix("v")
    version_tuple(version)
    if version_tuple(version) <= version_tuple(VERSION):
        return None
    assets = [asset for asset in data.get("assets", []) if asset.get("name") == PACKAGE]
    if len(assets) != 1:
        raise ValueError("这个版本还没有上传完整的更新包")
    asset = assets[0]
    sha256 = str(asset.get("digest", "")).removeprefix("sha256:")
    url = str(asset.get("browser_download_url", ""))
    expected = f"https://github.com/{REPOSITORY}/releases/download/{data['tag_name']}/{PACKAGE}"
    size = asset.get("size")
    if not SHA256.fullmatch(sha256) or type(size) is not int or not 0 < size <= MAX_PACKAGE or url != expected:
        raise ValueError("这个版本缺少可校验的更新包，请到发布页查看")
    return {"version": version, "sha256": sha256, "size": size, "url": url}


def extractor_processes(root):
    target = os.path.normcase(str(Path(root) / "自动解压.exe"))
    found = []
    for process in psutil.process_iter(["pid", "exe", "cmdline"]):
        if os.path.normcase(process.info.get("exe") or "") != target:
            continue
        args = (process.info.get("cmdline") or [])[1:]
        if "--once" in args or "--file" in args:
            raise ValueError("正在解压游戏，完成后再更新")
        found.append({"pid": process.pid, "args": args})
    return found


class UpdateManager:
    def __init__(self, root, frozen, config, prepare, close, release, log):
        self.root = Path(root)
        self.supported = bool(frozen and os.name == "nt" and (self.root / "更新助手.exe").is_file())
        self.config, self.prepare, self.close, self.release, self.log = config, prepare, close, release, log
        self.lock = threading.Lock()
        self.operation = threading.Lock()
        self.stop = threading.Event()
        self.last_activity = time.monotonic()
        self.editing_until = 0
        self.next_check = time.monotonic() + 15
        self.info = None
        self.job = None
        self.requested = False
        self.browser = False
        self.state = {"phase": "idle" if self.supported else "source", "current_version": VERSION,
                      "latest_version": "", "message": "等待检查更新" if self.supported else "源码运行，请通过 Git 更新代码",
                      "checked_at": "", "downloaded": 0, "total": 0}
        try:
            result = read_json(self.root / "data/software-update.json")
            if result.get("phase") == "error" or result.get("version") == VERSION:
                self.state.update(phase=result["phase"], message=result["message"])
                if result.get("phase") == "error":
                    self.next_check = time.monotonic() + CHECK_INTERVAL
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def enabled(self):
        return bool(self.config().get("auto_update", True))

    def snapshot(self):
        with self.lock:
            state = dict(self.state)
        return {**state, "supported": self.supported, "auto_update": self.enabled(), "release_url": RELEASES_URL}

    def update(self, **values):
        with self.lock:
            self.state.update(values)

    def activity(self, editing=False):
        self.last_activity = time.monotonic()
        self.editing_until = self.last_activity + 15 if editing else 0

    def start(self, browser=False):
        self.browser = browser
        self.cleanup()
        if self.supported:
            threading.Thread(target=self.loop, daemon=True, name="software-updates").start()

    def cleanup(self):
        folder = self.root / "tmp/updates"
        if not folder.is_dir() or not folder.resolve().is_relative_to(self.root.resolve()) or active_request(self.root):
            return
        for job in folder.iterdir():
            if (not job.is_dir() or job.is_symlink() or not re.fullmatch(r"[0-9a-f]{32}", job.name) or
                    job == self.job or ((job / "journal.json").exists() and not (job / "finished.json").exists())):
                continue
            # 助手退出、释放自己的文件后再清理；失败日志另留在 logs。
            if (job / "finished.json").exists() or time.time() - job.stat().st_mtime > 86400:
                try:
                    finished = job / "finished.json"
                    if finished.is_file() and psutil.pid_exists(int(read_json(finished).get("helper_pid", 0))):
                        continue
                    error = job / "error.txt"
                    if error.is_file():
                        logs = self.root / "logs"
                        logs.mkdir(exist_ok=True)
                        shutil.copy2(error, logs / "update.log")
                    shutil.rmtree(job)
                except (OSError, ValueError, TypeError):
                    pass
        marker = folder / "active.json"
        if marker.exists() and not active_request(self.root):
            marker.unlink(missing_ok=True)
        try:
            folder.rmdir()
            folder.parent.rmdir()
        except OSError:
            pass

    def check(self):
        if self.state["phase"] in ("ready", "installing"):
            return self.snapshot()
        if not self.operation.acquire(blocking=False):
            return self.snapshot()
        self.update(phase="checking", message="正在检查更新……")
        threading.Thread(target=self._check, daemon=True).start()
        return self.snapshot()

    def _check(self):
        try:
            with open_url(LATEST_API) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("发布信息过大")
            info = release_info(json.loads(raw))
            self.info = info
            self.update(checked_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        latest_version=info["version"] if info else "")
            if info is None:
                self.update(phase="current", message="当前无需更新")
            elif not self.supported:
                self.update(phase="available", message=f"发现 {info['version']}，请到发布页下载绿色版")
            else:
                self.update(phase="available", message=f"发现新版本 {info['version']}")
                if self.enabled():
                    self._download()
        except urllib.error.HTTPError as error:
            self.fail("暂时没有正式发布版本" if error.code == 404 else "检查更新失败，请稍后再试")
        except Exception as error:
            self.fail(f"检查更新失败：{error}")
        finally:
            self.next_check = time.monotonic() + CHECK_INTERVAL
            self.operation.release()

    def install(self):
        if not self.supported:
            raise ValueError("源码运行不能替换程序，请到发布页下载绿色版")
        if self.state["phase"] == "ready":
            self.requested = True
        elif self.state["phase"] == "available" and self.info and self.operation.acquire(blocking=False):
            self.requested = True
            threading.Thread(target=self._manual_download, daemon=True).start()
        elif self.state["phase"] not in ("downloading", "installing"):
            raise ValueError("请先检查更新")
        return self.snapshot()

    def _manual_download(self):
        try:
            self._download()
        except Exception as error:
            self.fail(f"更新下载失败：{error}")
        finally:
            self.operation.release()

    def _download(self):
        info = dict(self.info)
        folder = self.root / "tmp/updates"
        folder.mkdir(parents=True, exist_ok=True)
        if not folder.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("更新临时目录必须位于游戏库文件夹内")
        job = folder / uuid.uuid4().hex
        job.mkdir()
        self.job = job
        self.update(phase="downloading", message=f"正在下载 {info['version']}……", downloaded=0, total=info["size"])
        sha256, downloaded = hashlib.sha256(), 0
        try:
            if shutil.disk_usage(job).free < info["size"] * 3 + 128 * 1024 * 1024:
                raise ValueError("空间不足，请腾出一些空间再更新")
            with open_url(info["url"]) as source, (job / (PACKAGE + ".part")).open("xb") as output:
                while chunk := source.read(1024 * 1024):
                    if self.stop.is_set() or (not self.requested and not self.enabled()):
                        raise InterruptedError("自动更新已关闭")
                    downloaded += len(chunk)
                    if downloaded > info["size"]:
                        raise ValueError("下载大小和发布记录不一致")
                    output.write(chunk)
                    sha256.update(chunk)
                    self.update(downloaded=downloaded)
            if downloaded != info["size"] or sha256.hexdigest() != info["sha256"]:
                raise ValueError("更新包校验没通过，请重新下载")
            os.replace(job / (PACKAGE + ".part"), job / PACKAGE)
            write_json(job / "release.json", info)
            self.update(phase="ready", message="新版已下载，空闲后自动更新" if self.enabled() or self.requested else "新版已下载")
        except Exception:
            shutil.rmtree(job)
            self.job = None
            raise

    def fail(self, message):
        self.log(message)
        self.update(phase="error", message=message)
        self.requested = False
        if not active_request(self.root):
            self.job = None

    def loop(self):
        while not self.stop.wait(1):
            try:
                phase = self.state["phase"]
                if phase == "ready" and (self.requested or self.enabled()):
                    if not self.requested and (time.monotonic() - self.last_activity < IDLE_SECONDS or
                                               time.monotonic() < self.editing_until):
                        self.update(message="新版已下载，停止操作一分钟后自动更新")
                        continue
                    if self.operation.acquire(blocking=False):
                        try:
                            self.handoff()
                        finally:
                            self.operation.release()
                elif self.enabled() and time.monotonic() >= self.next_check and phase not in ("checking", "downloading", "installing", "ready"):
                    self.check()
                self.cleanup()
            except Exception as error:
                self.release()
                self.fail(f"更新没能开始：{error}")
                self.next_check = time.monotonic() + CHECK_INTERVAL

    def handoff(self):
        reason = self.prepare()
        if reason:
            self.update(message="新版已下载，" + reason)
            return
        marker = self.root / "tmp/updates/active.json"
        process = None
        try:
            extractors = extractor_processes(self.root)
            write_json(marker, {"job": self.job.name, "expires": time.time() + 360})
            plan = {**self.info, "root": str(self.root), "token": uuid.uuid4().hex,
                    "wait_pids": [os.getpid(), *[p["pid"] for p in extractors]],
                    "extractors": extractors, "browser": self.browser}
            write_json(self.job / "plan.json", plan)
            helper = self.job / "helper.exe"
            shutil.copy2(self.root / "更新助手.exe", helper)
            runtime = self.job / "runtime"
            runtime.mkdir()
            environment = os.environ.copy()
            for name in ("TEMP", "TMP"):
                environment["CECILIA_PREVIOUS_" + name] = environment.get(name, "")
                environment[name] = str(runtime)
            environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
            process = subprocess.Popen([str(helper), "--apply", str(self.job / "plan.json")],
                                       cwd=self.root, env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
            self.update(phase="installing", message="正在准备更新……")
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if (self.job / "helper-ready.json").is_file():
                    write_json(self.job / "parent-exiting.json", {"exiting": True})
                    self.update(message="正在更新，即将重新打开……")
                    self.close()
                    self.stop.set()
                    return
                if process.poll() is not None:
                    raise ValueError("更新包无法使用，原版本已保留")
                time.sleep(0.1)
            raise ValueError("更新助手启动超时")
        except Exception:
            # 助手未就绪时主程序继续运行，不能让它稍后再替换文件。
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
            marker.unlink(missing_ok=True)
            self.release()
            raise


def acknowledge_boot(root, plan_path):
    path = Path(plan_path).resolve()
    request = active_request(root)
    if not request or path != Path(root).resolve() / "tmp/updates" / request["job"] / "plan.json":
        raise ValueError("更新启动信息不正确")
    plan = read_json(path)
    if plan["version"] != VERSION:
        raise ValueError("启动的版本与更新版本不一致")
    write_json(path.parent / "booted.json", {"token": plan["token"], "version": VERSION})
