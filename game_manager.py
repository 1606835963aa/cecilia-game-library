"""游戏库主程序：开一个只有本机能访问的小网站，再用独立的软件窗口把它显示出来。

窗口用的是 Windows 自带的 WebView2 网页引擎（pywebview 负责建窗口），
没有地址栏、右键菜单和浏览器账号，用起来就是一个普通软件。

用法：
    pythonw game_manager.py          打开游戏库窗口（不带黑窗口，日志写到 logs\\game_manager.log）
    python game_manager.py --browser 用默认浏览器打开，方便调试
"""
import argparse
import ctypes
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

# 打包成 exe 时日志放在 exe 旁边（和 auto_extract.ROOT 同一个位置，这里要赶在加载其他模块前用到）
LOG_FILE = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent / "logs" / "game_manager.log"

# pythonw 没有控制台，把输出（包括报错）都写进日志文件。
# 要赶在 pywebview 加载之前做，它发现没有控制台会把输出扔掉。
if sys.stdout is None:
    LOG_FILE.parent.mkdir(exist_ok=True)
    _mode = "w" if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1024 * 1024 else "a"
    sys.stdout = sys.stderr = open(LOG_FILE, _mode, encoding="utf-8", buffering=1)


def fatal(msg):
    """用 pythonw 运行时没有黑窗口，出错只能弹个对话框告诉用户。"""
    print(msg, file=sys.stderr)
    ctypes.windll.user32.MessageBoxW(None, msg, "塞西莉亚的游戏库打不开", 0x10)
    sys.exit(1)


try:
    import psutil
    import uvicorn
    import webview
    from webview.dom import DOMEventHandler
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
    from fastapi.staticfiles import StaticFiles
except ImportError as e:
    fatal(f"缺少 Python 模块：{e.name}\n\n在命令行运行下面这条命令装上：\npip install fastapi uvicorn psutil pywin32 Pillow pywebview")

import importer
import library
import scraper
import wishlist
import updates
from app_version import VERSION
from update_helper import active_request
from itch_account import ItchAccount
from window_frame import CustomFrame
from auto_extract import (ANDROID_MODES, APP_DIR, EVENT_PREFIX, FROZEN, ROOT, android_root, download_roots,
                          find_seven_zip, games_root, load_config, save_config, scan_lock, validate_layout)

WEB_DIR = APP_DIR / "web"
ICON = WEB_DIR / "icon.ico"
WINDOW_STATE = ROOT / "data" / "window.json"
WEBVIEW_DATA = ROOT / "data" / "webview"
PORT = int(os.environ.get("GAME_LIBRARY_PORT") or 8765)  # 开发时换个端口，就能和正在用的游戏库同时开
APP_ID = "game-library"
TOKEN = secrets.token_hex(16)
AUTO_SCAN_SECONDS = 60
maintenance_lock = threading.Lock()
maintenance = {"updating": False, "writes": 0, "scans": 0}
exit_requested = threading.Event()
# 皮肤：页面配色在 web 里，这里管窗口标题栏和页面出来之前的底色，都和页面顶部导航条同色
THEMES = {
    "steam": {"label": "经典深色", "dark": True, "bar": "#171a21", "text": "#dcdedf", "bg": "#1a1f26"},
    "paper": {"label": "浪子的真情", "dark": False, "bar": "#f7f5ec", "text": "#26473b", "bg": "#f4f1e6"},
}


def current_theme():
    theme = load_config().get("theme")
    return theme if theme in THEMES else "steam"


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- 启动与计时
class PlayTracker:
    """记录正在玩的游戏。

    只要有进程的程序文件在游戏文件夹里，就算还在玩。很多游戏的 exe 只是个启动器，
    真正的游戏是它再开的另一个进程，所以不能只盯着最开始那个进程。
    """

    CHECK_SECONDS = 5
    START_GRACE = 30  # 刚启动时游戏进程可能还没出来，最多等这么久

    def __init__(self):
        self.lock = threading.Lock()
        self.running = {}  # game_id -> 记录

    def launch(self, game):
        folder = library.to_abs(game["path"]).resolve()
        exe = folder / game["exe"]
        if not game["exe"] or not exe.is_file():
            raise HTTPException(400, "找不到主程序，请在详情里重新选一个")
        with self.lock:
            if game["id"] in self.running:
                raise HTTPException(409, "这个游戏已经在运行了")
            if library.ensure_normal_integrity(folder, game["exe"]):
                log(f"{game['name']} 的文件带低完整性标签（会让游戏以低权限运行而闪退），已恢复成普通权限")
            proc = subprocess.Popen(
                [str(exe)],
                cwd=str(exe.parent),
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )
            self.running[game["id"]] = {
                "name": game["name"],
                "proc": proc,
                "prefix": os.path.normcase(str(folder)) + os.sep,
                "session": library.start_session(game["id"]),
                "started": time.time(),
                "last_seen": time.time(),
                "seen": False,
            }
        log(f"启动：{game['name']}")

    def seconds(self):
        with self.lock:
            return {gid: int(r["last_seen"] - r["started"]) for gid, r in self.running.items()}

    def active(self):
        with self.lock:
            return bool(self.running)

    def _game_exes(self):
        paths = []
        for p in psutil.process_iter(["exe"]):
            exe = p.info.get("exe")
            if exe:
                paths.append(os.path.normcase(exe))
        return paths

    def loop(self):
        while True:
            time.sleep(self.CHECK_SECONDS)
            if not self.active():
                continue
            try:
                exes = self._game_exes()
            except Exception as e:
                log(f"读取进程列表失败：{e}")
                continue
            now = time.time()
            finished = []
            with self.lock:
                for gid, r in self.running.items():
                    alive = r["proc"].poll() is None or any(e.startswith(r["prefix"]) for e in exes)
                    if alive:
                        r["last_seen"], r["seen"] = now, True
                    elif r["seen"] or now - r["started"] > self.START_GRACE:
                        finished.append(gid)
                    library.update_session(r["session"], int(r["last_seen"] - r["started"]))
                for gid in finished:
                    r = self.running.pop(gid)
                    secs = int(r["last_seen"] - r["started"])
                    library.update_session(r["session"], secs, finished=True)
                    log(f"结束：{r['name']}，这次玩了 {secs // 60} 分钟")


tracker = PlayTracker()
worker = scraper.Worker(log)
imports = importer.Importer(log, worker.enqueue)


class RefreshRunner:
    """解压放在独立进程，既不阻塞页面，也不占用手动导入的中转文件夹。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"id": 0, "running": False, "phase": "idle", "message": "", "results": []}
        self.starting_games = set()

    def snapshot(self):
        with self.lock:
            return self.snapshot_unlocked()

    def busy(self):
        with self.lock:
            return self.state["running"]

    def update(self, **fields):
        with self.lock:
            self.state.update(fields)

    def start(self):
        with self.lock:
            if self.state["running"]:
                return self.snapshot_unlocked()
            if not find_seven_zip(load_config()):
                raise ValueError("没找到 7-Zip，装上后才能解压")
            self.starting_games = {(g["id"], g["path"]) for g in library.list_games() if not g["is_wishlist"]}
            self.state = {
                "id": self.state["id"] + 1, "running": True, "phase": "checking",
                "message": "正在检查下载文件夹", "file": "", "results": [],
                "processed": 0, "succeeded": 0, "failed": 0, "waiting": 0,
            }
            state = self.snapshot_unlocked()
        threading.Thread(target=self.run, daemon=True).start()
        return state

    def snapshot_unlocked(self):
        return dict(self.state, results=list(self.state["results"]))

    def run(self):
        try:
            # 游戏库窗口没有标准输出，改用控制台程序跑解压并隐藏黑窗口，才能读取进度。
            # 打包成 exe 时用旁边的「自动解压.exe」，源码运行时用同一套 Python 的 python.exe。
            if FROZEN:
                command = [str(ROOT / "自动解压.exe"), "--once", "--events"]
            else:
                python = Path(sys.executable)
                if python.name.lower() == "pythonw.exe":
                    python = python.with_name("python.exe")
                command = [str(python), "-u", "-X", "utf8", str(ROOT / "auto_extract.py"), "--once", "--events"]
            report = None
            with subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, encoding="utf-8", errors="replace",
                                  creationflags=subprocess.CREATE_NO_WINDOW) as proc:
                for line in proc.stdout:
                    if not line.startswith(EVENT_PREFIX):
                        continue
                    event = json.loads(line[len(EVENT_PREFIX):])
                    phase = event.get("phase")
                    if phase == "result":
                        with self.lock:
                            self.state["results"].append({k: event[k] for k in ("file", "ok", "message")})
                            self.state["results"] = self.state["results"][-100:]
                    elif phase == "finished":
                        report = {k: event[k] for k in ("processed", "succeeded", "failed", "waiting")}
                    else:
                        self.update(phase=phase, message=event.get("message", ""), file=event.get("file", ""))
                returncode = proc.wait()
            if returncode or report is None:
                raise ValueError("解压程序没能完成，请查看 logs 文件夹里的 extract.log")
            self.update(phase="scanning", message="正在更新游戏列表", file="", **report)
            result = scan_and_queue(full=True)
            # 自动扫描也会在解压期间入库，按刷新前后的记录比较，才能把抢先入库的也算上。
            current_games = [g for g in library.list_games() if not g["is_wishlist"] and not g["missing"]]
            added = [g["name"] for g in current_games if (g["id"], g["path"]) not in self.starting_games]
            parts = []
            if report["succeeded"]:
                parts.append(f"已处理 {report['succeeded']} 项")
            if report["failed"]:
                parts.append(f"{report['failed']} 个没解开")
            if report["waiting"]:
                parts.append(f"{report['waiting']} 个还在下载，下载完再刷新")
            parts.append(f"新入库 {len(added)} 个游戏" if added else f"没有新游戏（共 {len(current_games)} 个）")
            message = "，".join(parts)
            self.update(running=False, phase="done", message=message, added=added, total=len(current_games))
            log(f"刷新完成：{message}")
        except Exception as e:
            log(f"刷新失败：{e}")
            self.update(running=False, phase="error", message=str(e), file="")


refresh = RefreshRunner()


# ---------------------------------------------------------------- 网页接口
app = FastAPI()
app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")


@app.middleware("http")
async def guard(request: Request, call_next):
    # 只接受本机地址访问；接口还要带上页面里的口令，防止别的网页偷偷调用
    host = request.headers.get("host", "").rsplit(":", 1)[0]
    if host not in ("127.0.0.1", "localhost"):
        return PlainTextResponse("forbidden", status_code=403)
    if request.url.path.startswith("/api/") and request.headers.get("x-token") != TOKEN:
        return PlainTextResponse("forbidden", status_code=403)
    writing = request.url.path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS")
    with maintenance_lock:
        if writing and maintenance["updating"]:
            return JSONResponse({"error": "正在更新软件，请稍等片刻"}, status_code=503)
        if writing:
            maintenance["writes"] += 1
    try:
        response = await call_next(request)
    finally:
        if writing:
            with maintenance_lock:
                maintenance["writes"] -= 1
    # 页面文件每次都先问一下有没有更新，不然窗口会一直用缓存里的旧版页面
    if request.url.path == "/" or request.url.path.startswith("/web/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/", response_class=HTMLResponse)
def index():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace("__TOKEN__", TOKEN).replace("__THEME__", current_theme())
    # 页面文件的地址带上最后修改时间，文件一改地址就变，窗口不会拿缓存里的旧版
    version = max(int(f.stat().st_mtime) for f in WEB_DIR.iterdir() if f.is_file())
    html = html.replace("__VERSION__", str(version))
    # 软件窗口里标题栏是页面自己画的；用浏览器打开时没有这些按钮
    return html.replace("__FRAME__", "custom" if shell.window else "")


@app.get("/healthz")
def healthz():
    return {"app": APP_ID, "version": VERSION}


@app.post("/show")
def show(request: Request):
    # 第二次双击启动时，新进程调这个接口把已经开着的窗口叫到前台
    if request.headers.get("x-app") != APP_ID:
        raise HTTPException(403)
    shell.bring_to_front()
    return {"ok": True}


@app.post("/api/window/{action}")
def api_window(action: str):
    if action not in ("min", "max", "close"):
        raise HTTPException(404)
    if not shell.frame:
        raise HTTPException(400, "在浏览器里打开时用不了")
    shell.frame.command(action)
    return {"ok": True}


@app.get("/covers/{name}")
def cover(name: str):
    path = library.COVER_DIR / name
    if Path(name).name != name or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, headers={"Cache-Control": "no-cache"})


def require_game(game_id, local=False):
    game = library.get_game(game_id)
    if not game:
        raise HTTPException(404, "游戏不存在")
    if local and game["is_wishlist"]:
        raise HTTPException(400, "这款游戏还没下载，请先关联本地游戏")
    return game


@app.get("/api/games")
def api_games():
    games = wishlist.decorate(library.list_games(tracker.seconds()))
    busy = worker.busy()
    for g in games:
        g["scraping"] = g["id"] in busy
    return {"games": games, "statuses": library.STATUSES}


def scan_and_queue(full=False):
    with maintenance_lock:
        if maintenance["updating"]:
            return {"added": [], "added_ids": [], "total": len(library.list_games())}
        maintenance["scans"] += 1
    try:
        result = library.scan(full=full)
        if result["added"]:
            log(f"新入库：{'、'.join(result['added'])}")
            worker.enqueue(result["added_ids"])
        if full:
            worker.enqueue_unmatched()
            worker.enqueue_versions()
            worker.enqueue_icons()
        return result
    finally:
        with maintenance_lock:
            maintenance["scans"] -= 1


@app.post("/api/scan")
async def api_scan(request: Request):
    body = await request.json() if await request.body() else {}
    return scan_and_queue(full=bool(body.get("full")))


@app.get("/api/refresh")
def api_refresh_status():
    return refresh.snapshot()


@app.post("/api/refresh")
def api_refresh():
    try:
        return refresh.start()
    except (OSError, ValueError) as e:
        raise HTTPException(400, str(e))


@app.get("/api/search")
def api_search(q: str, game_id: int | None = None):
    if not q.strip():
        return {"results": [], "errors": []}
    developer = scraper.local_developer(require_game(game_id)) if game_id is not None else ""
    results, errors = scraper.search(q.strip(), developer=developer)
    return {"results": results, "errors": errors, "itch_account": shell.itch.snapshot()}


@app.post("/api/games/{game_id}/match")
async def api_match(game_id: int, request: Request):
    require_game(game_id)
    body = await request.json()
    try:
        fields = await run_blocking(scraper.apply_match, game_id, body.get("source"), body.get("id"))
    except (ValueError, LookupError) as e:
        raise HTTPException(400, str(e))
    except OSError:
        raise HTTPException(502, "连不上资料网站，稍后再试")
    return {"ok": True, "name": fields.get("name"), "game_id": fields["game_id"]}


@app.post("/api/wishlist")
async def api_add_wishlist(request: Request):
    body = await request.json()
    baseline = str(body.get("version") or "").strip()
    if len(baseline) > 80:
        raise HTTPException(400, "版本号请控制在 80 个字以内")
    try:
        return await run_blocking(scraper.add_wishlist, body.get("source"), body.get("id"), baseline)
    except (ValueError, LookupError) as e:
        raise HTTPException(400, str(e))
    except OSError:
        raise HTTPException(502, "连不上资料网站，稍后再试")


@app.post("/api/wishlist/{game_id}/check")
async def api_check_wishlist(game_id: int):
    require_game(game_id)
    try:
        message = await run_blocking(scraper.check_wishlist_version, game_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except OSError:
        raise HTTPException(502, "连不上资料网站，已保留上次的版本记录")
    return {"message": message}


@app.post("/api/wishlist/{game_id}/attach")
async def api_attach_wishlist(game_id: int, request: Request):
    require_game(game_id)
    try:
        local_id = int((await request.json()).get("local_id"))
        if local_id in tracker.seconds():
            raise HTTPException(409, "游戏正在运行，退出后再关联")
        return {"game_id": await run_blocking(wishlist.bind, game_id, local_id)}
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e) or "请选择已下载的游戏")


@app.patch("/api/wishlist/{game_id}/version")
async def api_record_wishlist_version(game_id: int, request: Request):
    game = require_game(game_id)
    if not game["wishlisted_at"]:
        raise HTTPException(400, "这款游戏没有愿望单版本记录")
    version = str((await request.json()).get("version") or "").strip()
    if not version or len(version) > 80:
        raise HTTPException(400, "请填写 80 个字以内的版本号")
    wishlist.record_version(game_id, version, "手动填写", "manual")
    return {"ok": True}


@app.post("/api/games/{game_id}/rescrape")
async def api_rescrape(game_id: int):
    game = require_game(game_id)
    msg, reachable = await run_blocking(scraper.auto_scrape, game_id)
    actual_id = game_id if library.get_game(game_id) else library.find_by_path(library.to_abs(game["path"]))
    return {"message": msg if reachable else msg + "（有资料网站没连上）", "game_id": actual_id}


@app.post("/api/games/{game_id}/clear-meta")
async def api_clear_meta(game_id: int):
    require_game(game_id)
    try:
        await run_blocking(scraper.clear, game_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


async def run_blocking(fn, *args):
    """联网下载比较慢，放到线程里跑，不卡住其他请求。"""
    import asyncio

    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


@app.patch("/api/games/{game_id}")
async def api_update(game_id: int, request: Request):
    require_game(game_id)
    try:
        library.update_game(game_id, await request.json())
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/games/{game_id}/launch")
def api_launch(game_id: int):
    game = require_game(game_id, local=True)
    if game["missing"]:
        raise HTTPException(400, "游戏文件夹已经不在了")
    tracker.launch(game)
    return {"ok": True}


@app.post("/api/games/{game_id}/open-folder")
def api_open_folder(game_id: int):
    path = library.to_abs(require_game(game_id, local=True)["path"])
    if not path.exists():
        raise HTTPException(400, "游戏文件夹已经不在了")
    if path.is_file():
        subprocess.Popen(["explorer", "/select,", str(path)])
    else:
        os.startfile(path)
    return {"ok": True}


@app.get("/api/games/{game_id}/exes")
def api_exes(game_id: int):
    game = require_game(game_id, local=True)
    folder = library.to_abs(game["path"])
    return {"exes": library.list_exes(folder) if folder.is_dir() else [], "current": game["exe"]}


@app.get("/api/games/{game_id}/sessions")
def api_sessions(game_id: int):
    require_game(game_id)
    return {"sessions": library.recent_sessions(game_id)}


@app.post("/api/games/{game_id}/exe")
async def api_set_exe(game_id: int, request: Request):
    require_game(game_id, local=True)
    try:
        library.set_exe(game_id, (await request.json()).get("exe", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


IMAGE_KINDS = ("cover", "hero")  # 竖版封面、横幅大图


@app.post("/api/games/{game_id}/image/{kind}")
async def api_image(game_id: int, kind: str, request: Request):
    require_game(game_id)
    if kind not in IMAGE_KINDS:
        raise HTTPException(404)
    raw = await request.body()
    if not raw or len(raw) > 30 * 1024 * 1024:
        raise HTTPException(400, "图片为空或超过 30MB")
    try:
        library.save_custom_image(game_id, raw, kind)
    except Exception:
        raise HTTPException(400, "这个文件不是能识别的图片")
    return {"ok": True}


@app.delete("/api/games/{game_id}/image/{kind}")
def api_reset_image(game_id: int, kind: str):
    require_game(game_id)
    if kind not in IMAGE_KINDS:
        raise HTTPException(404)
    library.reset_image(game_id, kind)
    return {"ok": True}


@app.delete("/api/games/{game_id}")
def api_remove(game_id: int):
    require_game(game_id)
    library.remove_game(game_id)
    return {"ok": True}


@app.delete("/api/games/{game_id}/files")
def api_delete_files(game_id: int):
    game = require_game(game_id, local=True)
    if game_id in tracker.seconds():
        raise HTTPException(409, "游戏正在运行，先退出游戏再删")
    try:
        library.delete_game_files(game_id)
    except (ValueError, OSError) as e:
        raise HTTPException(400, str(e))
    log(f"删除游戏：{game['name']}（已移到回收站）")
    return {"ok": True}


# ---------------------------------------------------------------- 手动导入
ARCHIVE_TYPES = ("压缩包 (*.zip;*.rar;*.7z;*.001;*.exe)", "所有文件 (*.*)")


@app.post("/api/import/pick")
async def api_import_pick(request: Request):
    # 用系统自带的选择窗口挑压缩包或文件夹
    if not shell.window:
        raise HTTPException(400, "在浏览器里打开时用不了，请在游戏库窗口里导入")
    folder = (await request.json()).get("what") == "folder"
    if folder:
        picked = shell.window.create_file_dialog(webview.FileDialog.FOLDER)
    else:
        picked = shell.window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True, file_types=ARCHIVE_TYPES)
    return {"paths": list(picked or [])}


@app.post("/api/import/inspect")
async def api_import_inspect(request: Request):
    paths = [str(p) for p in (await request.json()).get("paths") or [] if p]
    return {"items": await run_blocking(importer.inspect, paths)}


@app.post("/api/import/start")
async def api_import_start(request: Request):
    body = await request.json()
    try:
        ids = imports.start(body.get("items") or [], body.get("mode", "move"), bool(body.get("delete_archive")))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ids": ids}


@app.post("/api/import/{job_id}/retry")
async def api_import_retry(job_id: int, request: Request):
    try:
        ids = imports.retry(job_id, (await request.json()).get("password", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ids": ids}


@app.get("/api/import/jobs")
def api_import_jobs():
    return {"jobs": imports.jobs()}


@app.post("/api/import/clear")
def api_import_clear():
    imports.clear()
    return {"ok": True}


# ---------------------------------------------------------------- 设置
@app.get("/api/settings")
def api_settings():
    cfg = load_config()
    return {
        "android": cfg.get("android", "keep"),
        "android_modes": ANDROID_MODES,
        "android_dir": str(android_root(cfg)),
        "auto_scrape": bool(cfg.get("auto_scrape", True)),
        "auto_update": bool(cfg.get("auto_update", True)),
        "software_update": software_updates.snapshot(),
        "delete_archive": bool(cfg.get("delete_archive", True)),
        "download_dirs": [str(p) for p in download_roots(cfg)],
        "games_dir": str(games_root(cfg)),
        "native_window": shell.window is not None,
        "theme": current_theme(),
        "themes": {k: v["label"] for k, v in THEMES.items()},
        "passwords": [str(p) for p in cfg.get("passwords") or []],
        "itch_account": shell.itch.snapshot(),
    }


@app.patch("/api/settings")
async def api_save_settings(request: Request):
    body = await request.json()
    updates = {k: bool(body[k]) for k in ("auto_scrape", "delete_archive", "auto_update") if k in body}
    if "android" in body:
        if body["android"] not in ANDROID_MODES:
            raise HTTPException(400, "不认识的安卓版处理方式")
        updates["android"] = body["android"]
    if "theme" in body:
        if body["theme"] not in THEMES:
            raise HTTPException(400, "不认识的皮肤")
        updates["theme"] = body["theme"]
    directories = {}
    if "download_dirs" in body:
        values = body["download_dirs"]
        if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v.strip() for v in values):
            raise HTTPException(400, "请选择下载文件夹")
        directories["download_dirs"] = values
    if "games_dir" in body:
        if not isinstance(body["games_dir"], str) or not body["games_dir"].strip():
            raise HTTPException(400, "请选择游戏文件夹")
        directories["games_dir"] = body["games_dir"]
    try:
        if directories:
            save_directories(dict(updates, **directories))
        else:
            save_config(updates)
    except (OSError, ValueError) as e:
        raise HTTPException(400, f"设置没存上：{e}")
    if "theme" in updates:
        shell.paint_title_bar()
    return api_settings()


def save_directories(updates):
    if refresh.busy() or imports.busy():
        raise HTTPException(409, "正在解压或导入，完成后再改文件夹")
    with scan_lock() as acquired:
        if not acquired:
            raise HTTPException(409, "自动解压正在运行，完成后再改文件夹")
        cfg = dict(load_config(), **updates)
        validate_layout(cfg)
        for folder in [games_root(cfg), *download_roots(cfg)]:
            folder.mkdir(parents=True, exist_ok=True)
        save_config(dict(updates, games_dir=str(games_root(cfg)), download_dirs=[str(p) for p in download_roots(cfg)]))


@app.post("/api/directories/pick")
async def api_pick_directory(request: Request):
    if not shell.window:
        raise HTTPException(400, "请在游戏库软件窗口里更改文件夹")
    kind = (await request.json()).get("kind")
    if kind not in ("downloads", "games"):
        raise HTTPException(400, "不认识的文件夹")
    cfg = load_config()
    current = download_roots(cfg)[0] if kind == "downloads" else games_root(cfg)
    picked = await run_blocking(lambda: shell.window.create_file_dialog(webview.FileDialog.FOLDER, directory=str(current)))
    if not picked:
        return {"cancelled": True}
    try:
        save_directories({"download_dirs": [picked[0]]} if kind == "downloads" else {"games_dir": picked[0]})
    except (OSError, ValueError) as e:
        raise HTTPException(400, f"文件夹没改成：{e}")
    return api_settings()


@app.post("/api/directories/{kind}/open")
def api_open_directory(kind: str):
    cfg = load_config()
    if kind == "downloads":
        folder = download_roots(cfg)[0]
    elif kind == "games":
        folder = games_root(cfg)
    else:
        raise HTTPException(400, "不认识的文件夹")
    folder.mkdir(parents=True, exist_ok=True)
    os.startfile(folder)
    return {"ok": True}


@app.get("/api/itch/account")
def api_itch_account():
    return shell.itch.snapshot()


def require_native_window():
    if not shell.window:
        raise HTTPException(400, "请在游戏库软件窗口里登录 itch.io")


@app.post("/api/itch/login")
async def api_itch_login(request: Request):
    require_native_window()
    body = await request.json() if await request.body() else {}
    try:
        return await run_blocking(shell.itch.open, bool(body.get("settings")))
    except (OSError, ValueError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/itch/check")
def api_itch_check():
    require_native_window()
    return shell.itch.check_async(retry=True)


@app.delete("/api/itch/login")
def api_itch_logout():
    require_native_window()
    try:
        return shell.itch.logout()
    except (OSError, ValueError) as e:
        raise HTTPException(400, str(e))


# 解压密码：每次都在当下的设置文件上加减一个，不整份覆盖，免得冲掉导入时刚记下的密码
@app.post("/api/passwords")
async def api_add_password(request: Request):
    pwd = str((await request.json()).get("password", "")).strip()
    if not pwd:
        raise HTTPException(400, "密码是空的")
    saved = [str(p) for p in load_config().get("passwords") or []]
    if pwd in saved:
        raise HTTPException(400, "这个密码已经在列表里了")
    try:
        save_config({"passwords": saved + [pwd]})
    except (OSError, ValueError) as e:
        raise HTTPException(400, f"密码没存上：{e}")
    return api_settings()


@app.post("/api/passwords/remove")
async def api_remove_password(request: Request):
    pwd = str((await request.json()).get("password", ""))
    saved = [str(p) for p in load_config().get("passwords") or []]
    try:
        save_config({"passwords": [p for p in saved if p != pwd]})
    except (OSError, ValueError) as e:
        raise HTTPException(400, f"密码没删掉：{e}")
    return api_settings()


@app.post("/api/open-android-dir")
def api_open_android_dir():
    folder = android_root(load_config())
    folder.mkdir(parents=True, exist_ok=True)
    os.startfile(folder)
    return {"ok": True}


@app.exception_handler(HTTPException)
async def http_error(request, exc):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


# ---------------------------------------------------------------- 后台扫描
@app.get("/api/updates")
def api_updates():
    return software_updates.snapshot()


@app.post("/api/updates/check")
def api_update_check():
    return software_updates.check()


@app.post("/api/updates/install")
def api_update_install():
    try:
        return software_updates.install()
    except ValueError as error:
        raise HTTPException(400, str(error))


@app.post("/api/updates/activity")
async def api_update_activity(request: Request):
    body = await request.json()
    software_updates.activity(editing=bool(body.get("editing")))
    return {"ok": True}


def auto_scan_loop():
    while True:
        time.sleep(AUTO_SCAN_SECONDS)
        try:
            scan_and_queue()
        except Exception as e:
            log(f"自动扫描出错：{e}")


# ---------------------------------------------------------------- 窗口
def already_running():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/healthz", timeout=2) as resp:
            return json.loads(resp.read()).get("app") == APP_ID
    except Exception:
        return False


def pick_port():
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", PORT))
            return PORT
        except OSError:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


def colorref(hex_color):
    """#rrggbb 转成 Windows 用的颜色值（顺序是蓝绿红）。"""
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return r | g << 8 | b << 16


class Shell:
    """游戏库的软件窗口。

    关窗口时如果还有游戏在玩、或者还在导入，只把窗口藏起来，后台继续计时、导入，都结束后再真正退出；
    藏起来的这段时间再双击启动，会把窗口重新显示出来。
    """

    def __init__(self):
        self.window = None
        self.hwnd = None
        self.frame = None
        self.hidden = False
        self.maximized = False
        self.state = {}
        self.ready = threading.Event()
        self.itch = ItchAccount(self, self.on_itch_ready, log)

    def on_itch_ready(self):
        with maintenance_lock:
            if not maintenance["updating"] and load_config().get("auto_scrape", True):
                worker.enqueue(library.unmatched_games())

    def run(self, url):
        try:
            self.state = json.loads(WINDOW_STATE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.state = {}
        self.maximized = bool(self.state.get("maximized"))
        self.window = webview.create_window(
            "塞西莉亚的游戏库", url,
            width=self.state.get("width", 1320), height=self.state.get("height", 880),
            min_size=(960, 620), maximized=self.maximized, background_color=THEMES[current_theme()]["bg"],
        )
        events = self.window.events
        events.shown += self.on_shown
        events.loaded += self.on_loaded
        events.closing += self.on_closing
        events.maximized += lambda: self.set_maximized(True)
        events.restored += lambda: self.set_maximized(False)
        threading.Thread(target=self.exit_when_idle, daemon=True).start()
        # 关掉无痕模式，页面记住的东西（比如左边分组收起没有）下次打开还在
        webview.start(private_mode=False, storage_path=str(WEBVIEW_DATA), icon=str(ICON))

    def on_shown(self):
        self.hwnd = self.window.native.Handle.ToInt32()
        self.paint_title_bar()
        self.frame = CustomFrame(self.hwnd)
        self.on_ui_thread(self.frame.install)

    def on_ui_thread(self, fn):
        from System import Action

        self.window.native.Invoke(Action(fn))

    def set_maximized(self, value):
        self.maximized = value
        self.window.evaluate_js(f"window.setMaximized && setMaximized({json.dumps(value)})")

    def paint_title_bar(self):
        # 系统标题栏藏起来了，这里管的是窗口边框、右键系统菜单的深浅色，跟着皮肤走
        # （Windows 11 才支持改颜色，Windows 10 会忽略）
        if not self.hwnd:
            return
        theme = THEMES[current_theme()]
        for attr, value in ((20, int(theme["dark"])), (35, colorref(theme["bar"])), (36, colorref(theme["text"]))):
            ctypes.windll.dwmapi.DwmSetWindowAttribute(self.hwnd, attr, ctypes.byref(ctypes.c_int(value)), 4)

    def on_loaded(self):
        # 拖动窗口用的是 WebView2 的“页面标题栏”功能，要先打开；打开后刷新一次页面才生效
        # WebView2 的对象只能在窗口线程里碰，在别的线程里读写会把两边卡死
        switched = []

        def enable():
            core = self.window.native.browser.webview.CoreWebView2
            settings = core.Settings
            # 登录留在官网，账号密码也不交给浏览器的自动保存功能。
            if hasattr(core.Profile, "IsPasswordAutosaveEnabled"):
                core.Profile.IsPasswordAutosaveEnabled = False
            if hasattr(core.Profile, "IsGeneralAutofillEnabled"):
                core.Profile.IsGeneralAutofillEnabled = False
            if not settings.IsNonClientRegionSupportEnabled:
                settings.IsNonClientRegionSupportEnabled = True
                switched.append(True)

        self.on_ui_thread(enable)
        self.ready.set()
        if switched:
            self.window.evaluate_js("location.reload()")
            return
        self.set_maximized(self.frame.maximized() if self.frame else self.maximized)
        # 页面每次加载完都要重新挂一次；只有在 Python 这边才拿得到拖进来的文件的完整路径
        self.window.dom.document.events.drop += DOMEventHandler(self.on_drop, prevent_default=True)

    def on_drop(self, event):
        files = (event.get("dataTransfer") or {}).get("files") or []
        paths = [f["pywebviewFullPath"] for f in files if f.get("pywebviewFullPath")]
        if paths:
            self.window.evaluate_js(f"window.importDropped({json.dumps(paths)})")

    def on_closing(self):
        if tracker.active() or imports.busy() or refresh.busy():
            self.hidden = True
            if self.itch.window:
                self.itch.visible = False
                self.itch.window.hide()
            self.window.hide()
            if tracker.active():
                log("窗口藏起来了，游戏还在运行，玩完会自动记下时长再退出")
            elif imports.busy():
                log("窗口藏起来了，还在导入游戏，导入完再退出")
            else:
                log("窗口藏起来了，还在解压和刷新，完成后再退出")
            return False
        self.save_state()
        self.itch.shutdown()

    def save_state(self):
        state = dict(self.state, maximized=self.maximized)
        if not self.maximized:
            state["width"], state["height"] = self.window.width, self.window.height
        try:
            WINDOW_STATE.write_text(json.dumps(state), encoding="utf-8")
        except OSError as e:
            log(f"窗口大小没存上：{e}")

    def bring_to_front(self):
        if not self.window:
            return
        self.hidden = False
        self.window.show()
        if self.hwnd:
            user32 = ctypes.windll.user32
            if user32.IsIconic(self.hwnd):
                user32.ShowWindow(self.hwnd, 9)  # 从最小化还原
            user32.SetForegroundWindow(self.hwnd)

    def exit_when_idle(self):
        while True:
            time.sleep(PlayTracker.CHECK_SECONDS)
            if self.hidden and not tracker.active() and not imports.busy() and not refresh.busy():
                self.itch.shutdown()
                self.window.destroy()
                return


shell = Shell()


def prepare_software_update():
    with maintenance_lock:
        if maintenance["writes"] or maintenance["scans"]:
            return "等当前操作完成后更新"
        if tracker.active():
            return "等游戏结束后更新"
        if imports.busy() or refresh.busy():
            return "等导入和解压完成后更新"
        if worker.busy() or shell.itch.visible or shell.itch.snapshot().get("checking"):
            return "等资料获取和官网窗口关闭后更新"
        with scan_lock() as acquired:
            if not acquired:
                return "等解压完成后更新"
        maintenance["updating"] = True
    return ""


def release_software_update():
    with maintenance_lock:
        maintenance["updating"] = False


def close_for_update():
    if shell.window:
        shell.window.destroy()
    exit_requested.set()


software_updates = updates.UpdateManager(ROOT, FROZEN, load_config, prepare_software_update,
                                         close_for_update, release_software_update, log)


def start_background(browser=False, update_job=None):
    if update_job:
        if not browser and not shell.ready.wait(60):
            return
        updates.acknowledge_boot(ROOT, update_job)
        while active_request(ROOT):
            time.sleep(0.2)
        software_updates.update(phase="done", message=f"已更新到 {VERSION}")
        release_software_update()
        scan_and_queue()
    threading.Thread(target=tracker.loop, daemon=True).start()
    threading.Thread(target=auto_scan_loop, daemon=True).start()
    threading.Thread(target=worker.loop, daemon=True).start()
    imports.cleanup()
    threading.Thread(target=imports.loop, daemon=True).start()
    worker.enqueue(library.unprocessed_games())
    worker.enqueue_icons()
    software_updates.start(browser)


def show_existing():
    """游戏库已经开着时，让它把窗口叫到前台。"""
    ctypes.windll.user32.AllowSetForegroundWindow(-1)  # 允许那个进程把自己的窗口放到最前面
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/show", method="POST", headers={"X-App": APP_ID})
    urllib.request.urlopen(req, timeout=5).close()


def main():
    parser = argparse.ArgumentParser(description="本地游戏库")
    parser.add_argument("--browser", action="store_true", help="用默认浏览器打开，不开独立窗口")
    parser.add_argument("--update-job", help=argparse.SUPPRESS)
    args = parser.parse_args()

    pending = active_request(ROOT)
    if pending:
        expected = ROOT / "tmp/updates" / pending["job"] / "plan.json"
        if not args.update_job or Path(args.update_job).resolve() != expected.resolve():
            log("游戏库正在更新，更新后会自动打开")
            return
        maintenance["updating"] = True
    elif args.update_job:
        log("更新任务已结束，请正常打开游戏库")
        return

    if already_running():
        log("游戏库已经开着了，把窗口叫到前台")
        if args.browser:
            webbrowser.open(f"http://127.0.0.1:{PORT}/")
            return
        try:
            show_existing()
        except OSError as e:
            log(f"没能叫出已经开着的窗口：{e}")
        return

    cfg = load_config()
    for folder in [games_root(cfg), *download_roots(cfg)]:
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log(f"暂时无法创建文件夹 {folder}：{e}")
    library.init_db()
    if not args.browser:
        scraper.itch_browser = shell.itch
    log("正在扫描游戏文件夹……")
    result = {"total": len(library.list_games()), "added": []} if args.update_job else library.scan()
    log(f"游戏库里共 {result['total']} 个游戏" + (f"，新入库 {len(result['added'])} 个" if result["added"] else ""))

    port = pick_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.1)

    threading.Thread(target=start_background, args=(args.browser, args.update_job), daemon=True).start()

    url = f"http://127.0.0.1:{port}/"
    log(f"游戏库已打开：{url}")
    if args.browser:
        webbrowser.open(url)
        log("在浏览器里打开了游戏库。关掉这个黑窗口就会退出")
        try:
            while not exit_requested.wait(1):
                pass
        except KeyboardInterrupt:
            pass
    else:
        # 让任务栏用游戏库自己的图标，而不是 Python 的图标
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("gamegame.library")
        shell.run(url)
    server.should_exit = True
    software_updates.stop.set()
    log("已退出")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        import traceback
        traceback.print_exc()
        where = f"\n\n详细信息在：{LOG_FILE}" if getattr(sys.stdout, "name", "") == str(LOG_FILE) else ""
        fatal(f"游戏库运行出错：{e}{where}")
