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

LOG_FILE = Path(__file__).resolve().parent / "logs" / "game_manager.log"

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
from window_frame import CustomFrame
from auto_extract import ANDROID_MODES, ROOT, android_root, load_config, save_config

WEB_DIR = ROOT / "web"
ICON = WEB_DIR / "icon.ico"
WINDOW_STATE = ROOT / "data" / "window.json"
WEBVIEW_DATA = ROOT / "data" / "webview"
PORT = 8765
APP_ID = "game-library"
TOKEN = secrets.token_hex(16)
AUTO_SCAN_SECONDS = 60
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
    return await call_next(request)


@app.get("/", response_class=HTMLResponse)
def index():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace("__TOKEN__", TOKEN).replace("__THEME__", current_theme())
    # 软件窗口里标题栏是页面自己画的；用浏览器打开时没有这些按钮
    return html.replace("__FRAME__", "custom" if shell.window else "")


@app.get("/healthz")
def healthz():
    return {"app": APP_ID}


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


def require_game(game_id):
    game = library.get_game(game_id)
    if not game:
        raise HTTPException(404, "游戏不存在")
    return game


@app.get("/api/games")
def api_games():
    games = library.list_games(tracker.seconds())
    busy = worker.busy()
    for g in games:
        g["scraping"] = g["id"] in busy
    return {"games": games, "statuses": library.STATUSES}


def scan_and_queue(full=False):
    result = library.scan(full=full)
    if result["added"]:
        log(f"新入库：{'、'.join(result['added'])}")
        worker.enqueue(result["added_ids"])
    return result


@app.post("/api/scan")
async def api_scan(request: Request):
    body = await request.json() if await request.body() else {}
    return scan_and_queue(full=bool(body.get("full")))


@app.get("/api/search")
def api_search(q: str):
    if not q.strip():
        return {"results": [], "errors": []}
    results, errors = scraper.search(q.strip())
    return {"results": results, "errors": errors}


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
    return {"ok": True, "name": fields.get("name")}


@app.post("/api/games/{game_id}/rescrape")
async def api_rescrape(game_id: int):
    require_game(game_id)
    msg, reachable = await run_blocking(scraper.auto_scrape, game_id)
    return {"message": msg if reachable else msg + "（有资料网站没连上）"}


@app.post("/api/games/{game_id}/clear-meta")
async def api_clear_meta(game_id: int):
    require_game(game_id)
    await run_blocking(scraper.clear, game_id)
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
    game = require_game(game_id)
    if game["missing"]:
        raise HTTPException(400, "游戏文件夹已经不在了")
    tracker.launch(game)
    return {"ok": True}


@app.post("/api/games/{game_id}/open-folder")
def api_open_folder(game_id: int):
    path = library.to_abs(require_game(game_id)["path"])
    if not path.exists():
        raise HTTPException(400, "游戏文件夹已经不在了")
    if path.is_file():
        subprocess.Popen(["explorer", "/select,", str(path)])
    else:
        os.startfile(path)
    return {"ok": True}


@app.get("/api/games/{game_id}/exes")
def api_exes(game_id: int):
    game = require_game(game_id)
    folder = library.to_abs(game["path"])
    return {"exes": library.list_exes(folder) if folder.is_dir() else [], "current": game["exe"]}


@app.get("/api/games/{game_id}/sessions")
def api_sessions(game_id: int):
    require_game(game_id)
    return {"sessions": library.recent_sessions(game_id)}


@app.post("/api/games/{game_id}/exe")
async def api_set_exe(game_id: int, request: Request):
    require_game(game_id)
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
    game = require_game(game_id)
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
        "delete_archive": bool(cfg.get("delete_archive", True)),
        "theme": current_theme(),
        "themes": {k: v["label"] for k, v in THEMES.items()},
    }


@app.patch("/api/settings")
async def api_save_settings(request: Request):
    body = await request.json()
    updates = {k: bool(body[k]) for k in ("auto_scrape", "delete_archive") if k in body}
    if "android" in body:
        if body["android"] not in ANDROID_MODES:
            raise HTTPException(400, "不认识的安卓版处理方式")
        updates["android"] = body["android"]
    if "theme" in body:
        if body["theme"] not in THEMES:
            raise HTTPException(400, "不认识的皮肤")
        updates["theme"] = body["theme"]
    try:
        save_config(updates)
    except (OSError, ValueError) as e:
        raise HTTPException(400, f"设置没存上：{e}")
    if "theme" in updates:
        shell.paint_title_bar()
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
            settings = self.window.native.browser.webview.CoreWebView2.Settings
            if not settings.IsNonClientRegionSupportEnabled:
                settings.IsNonClientRegionSupportEnabled = True
                switched.append(True)

        self.on_ui_thread(enable)
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
        if tracker.active() or imports.busy():
            self.hidden = True
            self.window.hide()
            if tracker.active():
                log("窗口藏起来了，游戏还在运行，玩完会自动记下时长再退出")
            else:
                log("窗口藏起来了，还在导入游戏，导入完再退出")
            return False
        self.save_state()

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
            if self.hidden and not tracker.active() and not imports.busy():
                self.window.destroy()
                return


shell = Shell()


def show_existing():
    """游戏库已经开着时，让它把窗口叫到前台。"""
    ctypes.windll.user32.AllowSetForegroundWindow(-1)  # 允许那个进程把自己的窗口放到最前面
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/show", method="POST", headers={"X-App": APP_ID})
    urllib.request.urlopen(req, timeout=5).close()


def main():
    parser = argparse.ArgumentParser(description="本地游戏库")
    parser.add_argument("--browser", action="store_true", help="用默认浏览器打开，不开独立窗口")
    args = parser.parse_args()

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

    library.init_db()
    log("正在扫描游戏文件夹……")
    result = library.scan()
    log(f"游戏库里共 {result['total']} 个游戏" + (f"，新入库 {len(result['added'])} 个" if result["added"] else ""))

    port = pick_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    threading.Thread(target=tracker.loop, daemon=True).start()
    threading.Thread(target=auto_scan_loop, daemon=True).start()
    threading.Thread(target=worker.loop, daemon=True).start()
    imports.cleanup()
    threading.Thread(target=imports.loop, daemon=True).start()
    worker.enqueue(library.unprocessed_games())
    while not server.started:
        time.sleep(0.1)

    url = f"http://127.0.0.1:{port}/"
    log(f"游戏库已打开：{url}")
    if args.browser:
        webbrowser.open(url)
        log("在浏览器里打开了游戏库。关掉这个黑窗口就会退出")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    else:
        # 让任务栏用游戏库自己的图标，而不是 Python 的图标
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("gamegame.library")
        shell.run(url)
    server.should_exit = True
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
