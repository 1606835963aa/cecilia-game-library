"""游戏库：扫描游戏文件夹，认出游戏（引擎、主程序、名字、版本），存进数据库。"""
import hashlib
import io
import json
import os
import re
import sqlite3
import struct
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from auto_extract import ROOT, SKIP_DIRS, android_root, is_temp_file, load_config, prune_empty_parents, resolve_dir

DB_PATH = ROOT / "data" / "library.db"
COVER_DIR = ROOT / "data" / "covers"

STATUSES = ("未玩", "在玩", "通关", "弃坑")

# 不是游戏主程序的 exe：汉化验证器、卸载、崩溃上报、运行库安装、更新器等
JUNK_EXE = re.compile(
    r"汉化|验证|破解|补丁|安装|更新|卸载|unins|uninstall|crash|notification_helper|"
    r"dxsetup|dxwebsetup|vc_?redist|setup|install|update|patch|-32\.exe$",
    re.I,
)
# 引擎自带、不需要提醒的辅助程序
ENGINE_HELPERS = re.compile(r"^(unitycrashhandler(32|64)?|notification_helper)\.exe$", re.I)
RUNNABLE_EXT = {".exe", ".bat", ".cmd", ".vbs", ".ps1", ".scr", ".com", ".msi"}

PLATFORM_SUFFIX = re.compile(r"[-_ ](pc|win|win32|win64|windows|market|mac|linux|all|android)$", re.I)
VERSION_RE = re.compile(r"[-_ ]v?(\d+(?:\.\d+)+[a-z]?)(?=$|[-_ ])", re.I)

_db_lock = threading.Lock()

# games 表里刮削和显示用的字段，老数据库启动时自动补上
EXTRA_COLUMNS = {
    "icon": "TEXT DEFAULT ''",            # 主程序或官网的小图标
    "hero": "TEXT DEFAULT ''",            # 游戏页面顶部的横幅大图
    "logo": "TEXT DEFAULT ''",            # 透明底的游戏 Logo
    "screenshots": "TEXT DEFAULT '[]'",   # 截图文件名列表
    "description": "TEXT DEFAULT ''",
    "developer": "TEXT DEFAULT ''",
    "released": "TEXT DEFAULT ''",
    "genres": "TEXT DEFAULT ''",          # 资料网站给的类型标签，用顿号分开
    "meta_source": "TEXT DEFAULT ''",     # 资料来源：steam / vndb，空 = 没匹配上网上的资料
    "meta_id": "TEXT DEFAULT ''",         # 在资料网站上的编号
    "scraped": "INTEGER DEFAULT 0",       # 1 = 已经自动找过资料，不再自动找
    "name_locked": "INTEGER DEFAULT 0",   # 1 = 名字手动改过，刮削不改名
}
# 刮削能写的字段
META_FIELDS = {"name", "cover", "hero", "logo", "screenshots", "description", "developer", "released",
               "genres", "meta_source", "meta_id", "scraped", "icon"}
IMAGE_SIZES = {"cover": (900, 1350), "hero": (2560, 1440), "logo": (1200, 600), "shot": (1600, 900), "icon": (256, 256)}
# Ren'Py runtime/renpy_icon.ico、Godot platform/windows/godot.ico 的各尺寸默认图标。
ENGINE_ICON_HASHES = frozenset({
    "c0033ec048d689fa8d5bef11368ae713debabf6cce7eee1571b37158fbbbfbcc",
    "a1d97903ccbce2179f0f2b9ab7c759995e4b116ad82749183fe44bde36925a2c",
    "d9b5bcac59ea25761572abd2d17584502ac48a5ad11a5dea2482feae57beadfc",
    "a4588207125c073bbb9b11c2f1b9e9c01af5bbc305d3ab2aee9d090898958c90",
    "03aed84a38bc4ba45ea3cb11b99720a8e00183a6b21974a71e5d75aa45ca77a5",
    "aeae826cd91c376b20e26057781375098908a143ca28f5dbf351a1220ae40970",
    "2e3dc4c67d483209cc1b0b3fc30cd1403a2e76c2b1cb8d4ee36bf1c6e1942e56",
    "8f8003c39fa54d88d74677060866e312a89089b31de8c20924bebcd3ee4eb547",
    "0fadc037481d7202841d179f26fe15d77cce1fad4ba697875a6e3ab22b800e20",
    "2807032f439f514405cc2ca624b8c6b6d82af93afc08ba3dd861b052289a4e7e",
})


# ---------------------------------------------------------------- 数据库
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS games (
                id INTEGER PRIMARY KEY,
                path TEXT UNIQUE NOT NULL,      -- 游戏文件夹（安卓游戏是 apk 文件），项目内的用相对路径
                name TEXT NOT NULL,
                version TEXT DEFAULT '',
                platform TEXT DEFAULT 'PC',     -- PC / 安卓
                engine TEXT DEFAULT '',
                exe TEXT DEFAULT '',            -- 主程序，相对游戏文件夹
                exe_locked INTEGER DEFAULT 0,   -- 1 = 手动指定过主程序，扫描时不再改
                status TEXT DEFAULT '未玩',
                rating INTEGER DEFAULT 0,
                tags TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                cover TEXT DEFAULT '',          -- 竖版封面，data/covers 里的文件名；名字带 -custom 的是手动换的
                size_bytes INTEGER DEFAULT 0,
                suspicious TEXT DEFAULT '[]',   -- 主程序以外的可执行文件
                ad_files INTEGER DEFAULT 0,     -- 广告链接（.url）个数
                missing INTEGER DEFAULT 0,      -- 1 = 文件夹已经不在了
                added_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY,
                game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                seconds INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_game ON sessions(game_id);
            """
        )
        have = {row["name"] for row in conn.execute("PRAGMA table_info(games)")}
        for column, definition in EXTRA_COLUMNS.items():
            if column not in have:
                conn.execute(f"ALTER TABLE games ADD COLUMN {column} {definition}")
        # 图标单独放在 icon 里，cover 只放真正的封面
        conn.execute("UPDATE games SET icon=cover, cover='' WHERE cover LIKE '%-icon.png'")
        # 上次异常退出时没收尾的游玩记录，按已记下的时长补上结束时间
        for row in conn.execute("SELECT id, started_at, seconds FROM sessions WHERE ended_at IS NULL"):
            start = datetime.fromisoformat(row["started_at"])
            end = datetime.fromtimestamp(start.timestamp() + (row["seconds"] or 0))
            conn.execute("UPDATE sessions SET ended_at=? WHERE id=?", (now_str(end), row["id"]))


def now_str(dt=None):
    return (dt or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def to_rel(path):
    path = Path(path).resolve()
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def to_abs(stored):
    p = Path(stored)
    return p if p.is_absolute() else ROOT / p


# ---------------------------------------------------------------- 识别
def split_camel(text):
    return re.sub(r"(?<=[a-z'])(?=[A-Z])", " ", text)


def parse_name(raw):
    """从文件夹名拆出游戏名和版本号：SummerMemories-1.5.2-pc → Summer Memories / 1.5.2"""
    text = raw.strip()
    while PLATFORM_SUFFIX.search(text):
        text = PLATFORM_SUFFIX.sub("", text)
    version = ""
    matches = list(VERSION_RE.finditer(text))
    if matches:
        m = matches[-1]
        version = m.group(1)
        text = text[: m.start()]
    name = split_camel(text.replace("_", " ")).strip(" -._")
    return (name or raw), version


def _files_and_dirs(folder):
    files, dirs = {}, {}
    try:
        for entry in os.scandir(folder):
            if entry.is_file():
                files[entry.name.lower()] = entry.name
            elif entry.is_dir():
                dirs[entry.name.lower()] = entry.name
    except OSError:
        pass
    return files, dirs


def detect_engine(folder, files, dirs):
    exe_stems = {Path(n).stem.lower(): files[n] for n in files if n.endswith(".exe")}
    if "renpy" in dirs and "game" in dirs:
        preferred = [exe_stems[s] for s in exe_stems if f"{s}.py" in files]
        return "Ren'Py", preferred
    pck = [Path(n).stem for n in files if n.endswith(".pck")]
    if pck:
        return "Godot", [exe_stems[s] for s in pck if s in exe_stems]
    if "unityplayer.dll" in files:
        return "Unity", [exe_stems[s] for s in exe_stems if f"{s}_data" in dirs]
    if (folder / "www" / "js" / "rpg_core.js").exists() or (folder / "js" / "rmmz_core.js").exists():
        return "RPG Maker MV/MZ", [exe_stems[s] for s in ("game",) if s in exe_stems]
    if any(n.endswith((".rgss3a", ".rgss2a", ".rgssad")) for n in files) or ("game.ini" in files and "data" in dirs):
        return "RPG Maker VX/XP", [exe_stems[s] for s in ("game",) if s in exe_stems]
    if "engine" in dirs:
        return "Unreal", []
    if "nw.dll" in files and "package.json" in files:
        return "HTML (NW.js)", []
    return "", []


def pick_exe(files, preferred):
    for name in preferred:
        if not JUNK_EXE.search(name):
            return name
    candidates = [files[n] for n in files if n.endswith(".exe") and not JUNK_EXE.search(n) and not ENGINE_HELPERS.match(n)]
    return candidates[0] if len(candidates) == 1 else (sorted(candidates, key=len)[0] if candidates else "")


def detect_game(folder):
    """文件夹是一个游戏就返回识别结果，否则返回 None。"""
    files, dirs = _files_and_dirs(folder)
    if not any(n.endswith(".exe") for n in files):
        return None
    engine, preferred = detect_engine(folder, files, dirs)
    exe = pick_exe(files, preferred)
    if not exe:
        return None
    # 认不出引擎时，要求有 dll 或子文件夹，避免把只装着一个汉化工具的文件夹当成游戏
    if not engine and not dirs and not any(n.endswith(".dll") for n in files):
        return None
    name, version = parse_name(folder.name)
    return {
        "path": folder,
        "name": name,
        "version": version,
        "platform": "PC",
        "engine": engine,
        "exe": exe,
        **scan_extras(folder, files, exe),
    }


def scan_extras(folder, files, exe):
    suspicious = sorted(
        files[n]
        for n in files
        if Path(n).suffix in RUNNABLE_EXT and files[n] != exe and not ENGINE_HELPERS.match(n)
    )
    ads = sum(1 for n in files if n.endswith(".url"))
    return {"suspicious": suspicious, "ad_files": ads}


def apk_entry(path):
    name, version = parse_name(path.stem)
    return {"path": path, "name": name, "version": version, "platform": "安卓",
            "engine": "", "exe": "", "suspicious": [], "ad_files": 0}


def find_games(root, max_depth):
    """从监视目录往下找游戏；认出一个游戏后不再往它里面找。"""
    def walk(folder, depth):
        try:
            entries = sorted(os.scandir(folder), key=lambda e: e.name.lower())
        except OSError:
            return
        for entry in entries:
            if is_temp_file(entry.name):
                continue
            if entry.is_file() and entry.name.lower().endswith(".apk"):
                yield apk_entry(Path(entry.path))
        for entry in entries:
            if not entry.is_dir() or entry.name in SKIP_DIRS or entry.name.startswith("."):
                continue
            sub = Path(entry.path)
            game = detect_game(sub)
            if game:
                yield game
            elif depth < max_depth:
                yield from walk(sub, depth + 1)

    yield from walk(root, 1)


def folder_size(path):
    if path.is_file():
        return path.stat().st_size
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for f in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    return total


def list_exes(folder, limit=200):
    """游戏文件夹里所有 exe（往下 3 层），给“手动指定主程序”用。"""
    found = []
    base_depth = len(folder.parts)
    for dirpath, dirnames, filenames in os.walk(folder):
        if len(Path(dirpath).parts) - base_depth >= 3:
            dirnames[:] = []
        for f in filenames:
            if f.lower().endswith(".exe"):
                found.append(str((Path(dirpath) / f).relative_to(folder)))
                if len(found) >= limit:
                    return found
    return sorted(found, key=lambda p: (p.count(os.sep), p.lower()))


# ---------------------------------------------------------------- 低完整性标签
LOW_INTEGRITY_SID = "S-1-16-4096"


def has_low_label(path):
    """文件是否带“低完整性”标签。

    Windows 启动程序时，权限取“当前用户”和“exe 文件标签”里较低的那个。
    带这个标签的 exe 会以低权限运行，写不了 AppData，游戏会报错或闪退。
    在沙盒工具（比如 Codex）的工作文件夹里解压的文件会带上它，移动文件夹时标签也跟着走。
    """
    try:
        import win32security

        sd = win32security.GetNamedSecurityInfo(
            str(path), win32security.SE_FILE_OBJECT, win32security.LABEL_SECURITY_INFORMATION
        )
        sacl = sd.GetSecurityDescriptorSacl()
        if not sacl:
            return False
        return any(
            win32security.ConvertSidToStringSid(sacl.GetAce(i)[-1]) == LOW_INTEGRITY_SID
            for i in range(sacl.GetAceCount())
        )
    except Exception:
        return False


def clear_low_label(folder):
    """把整个游戏文件夹恢复成普通权限：先改回继承上级文件夹的权限，再把级别设为正常（中）。"""
    for args in (["/reset"], ["/setintegritylevel", "M"]):
        subprocess.run(
            ["icacls", str(folder), *args, "/T", "/C", "/Q"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    return not any(has_low_label(p) for p in (folder, *Path(folder).glob("*.exe")))


def ensure_normal_integrity(folder, exe):
    """主程序带低完整性标签就修好整个游戏文件夹；返回 True 表示这次修过。"""
    if exe and has_low_label(Path(folder) / exe):
        clear_low_label(folder)
        return True
    return False


# ---------------------------------------------------------------- 封面
def is_engine_icon(name):
    """只认像素一致的引擎默认图标，不能仅凭引擎名称替换游戏自己的图标。"""
    from PIL import Image

    try:
        with Image.open(COVER_DIR / name) as image:
            if image.width != image.height or image.width not in (16, 32, 48, 64, 128, 256):
                return False
            # 透明区域的隐藏颜色可能不同，合成到同一底色后再比对。
            background = Image.new("RGBA", image.size, "white")
            pixels = Image.alpha_composite(background, image.convert("RGBA")).convert("RGB")
            return hashlib.sha256(pixels.tobytes()).hexdigest() in ENGINE_ICON_HASHES
    except (OSError, ValueError, Image.DecompressionBombError):
        return False


def extract_icon(exe_path, out_path):
    """从 exe 里取出最大的那个图标存成 png。只把 exe 当数据读，不会运行它。"""
    try:
        import win32api
        import win32con
        from PIL import Image
    except ImportError:
        return False
    try:
        handle = win32api.LoadLibraryEx(str(exe_path), 0, win32con.LOAD_LIBRARY_AS_DATAFILE)
    except Exception:
        return False
    try:
        groups = win32api.EnumResourceNames(handle, win32con.RT_GROUP_ICON)
        if not groups:
            return False
        group = win32api.LoadResource(handle, win32con.RT_GROUP_ICON, groups[0])
        count = struct.unpack_from("<H", group, 4)[0]
        entries = []
        for i in range(count):
            w, h, colors, _, planes, bits, size, icon_id = struct.unpack_from("<BBBBHHIH", group, 6 + i * 14)
            entries.append(((w or 256), bits, w, h, colors, planes, icon_id))
        side, bits, w, h, colors, planes, icon_id = max(entries)
        data = win32api.LoadResource(handle, win32con.RT_ICON, icon_id)
        ico = struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", w, h, colors, 0, planes, bits, len(data), 22) + data
        img = Image.open(io.BytesIO(ico))
        img.load()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.convert("RGBA").save(out_path, "PNG")
        return True
    except Exception:
        return False
    finally:
        win32api.FreeLibrary(handle)


def refresh_icon(conn, game_id, folder, exe):
    """按当前主程序重新取图标。"""
    name = f"{game_id}-icon.png"
    if exe and extract_icon(folder / exe, COVER_DIR / name):
        conn.execute("UPDATE games SET icon=? WHERE id=?", (name, game_id))


def save_image(img, name, kind):
    """把图片缩到合适大小存进 data/covers；Logo 要保留透明底，存 png，其余存 jpg。"""
    COVER_DIR.mkdir(parents=True, exist_ok=True)
    img.thumbnail(IMAGE_SIZES[kind])
    if name.endswith(".png"):
        img.convert("RGBA").save(COVER_DIR / name, "PNG", optimize=True)
    else:
        img.convert("RGB").save(COVER_DIR / name, "JPEG", quality=86)
    return name


def save_custom_image(game_id, raw, kind):
    """手动换的封面（cover）或横幅大图（hero）。"""
    from PIL import Image

    img = Image.open(io.BytesIO(raw))
    img.load()
    name = save_image(img, f"{game_id}-{kind}-custom.jpg", kind)
    with _db_lock, connect() as conn:
        conn.execute(f"UPDATE games SET {kind}=? WHERE id=?", (name, game_id))
    return name


def reset_image(game_id, kind):
    """去掉手动换的图，换回刮削到的那张（没有就空着）。"""
    scraped = f"{game_id}-{kind}.jpg"
    with _db_lock, connect() as conn:
        conn.execute(f"UPDATE games SET {kind}=? WHERE id=?",
                     (scraped if (COVER_DIR / scraped).exists() else "", game_id))
    (COVER_DIR / f"{game_id}-{kind}-custom.jpg").unlink(missing_ok=True)


# ---------------------------------------------------------------- 扫描入库
def watch_roots():
    """要扫的文件夹：监视目录（games）加上安卓文件夹。"""
    cfg = load_config()
    roots = [resolve_dir(w) for w in cfg.get("watch_dirs") or ["games"]]
    android = android_root(cfg)
    if not any(r == android or r in android.parents for r in roots):
        roots.append(android)
    return roots, int(cfg.get("scan_depth", 6))


def scan(full=False):
    """扫一遍监视目录：新游戏入库，已有的更新信息，不见了的标记出来。

    full=True 时重新统计所有游戏的占用空间（慢一些）。
    """
    roots, depth = watch_roots()
    found = []
    for root in roots:
        if root.is_dir():
            found.extend(find_games(root, depth))

    added, added_ids = [], []
    with _db_lock, connect() as conn:
        existing = {row["path"]: row for row in conn.execute("SELECT * FROM games")}
        # 文件夹（或 apk）被挪了位置：按名字认回原来那条记录，资料和游玩时长都留着
        gone = {}
        for rel, row in existing.items():
            if not to_abs(rel).exists():
                gone.setdefault((Path(rel).name.lower(), row["platform"]), row)
        seen = set()
        for g in found:
            rel = to_rel(g["path"])
            seen.add(rel)
            row = existing.get(rel)
            extras = (json.dumps(g["suspicious"], ensure_ascii=False), g["ad_files"])
            if row is None:
                moved = gone.pop((Path(rel).name.lower(), g["platform"]), None)
                if moved:
                    conn.execute("UPDATE games SET path=?, missing=0 WHERE id=?", (rel, moved["id"]))
                    seen.add(moved["path"])
                    continue
                added.append(g["name"])
                added_ids.append(insert_game(conn, g))
                continue
            updates = {"missing": 0, "suspicious": extras[0], "ad_files": extras[1]}
            if not row["exe_locked"] and row["exe"] != g["exe"]:
                updates.update(exe=g["exe"], engine=g["engine"])
            if full:
                updates["size_bytes"] = folder_size(g["path"])
            sets = ", ".join(f"{k}=?" for k in updates)
            conn.execute(f"UPDATE games SET {sets} WHERE id=?", (*updates.values(), row["id"]))
            if "exe" in updates or (full and not row["icon"]):
                refresh_icon(conn, row["id"], g["path"], updates.get("exe", row["exe"]))

        missing = 0
        for rel, row in existing.items():
            if rel not in seen and not to_abs(rel).exists():
                if not row["missing"]:
                    conn.execute("UPDATE games SET missing=1 WHERE id=?", (row["id"],))
                missing += 1
    return {"added": added, "added_ids": added_ids, "missing": missing, "total": len(found)}


def insert_game(conn, g, exe_locked=False):
    if g["platform"] == "PC":
        ensure_normal_integrity(g["path"], g["exe"])
    cur = conn.execute(
        "INSERT INTO games (path, name, version, platform, engine, exe, exe_locked, suspicious, ad_files, size_bytes, added_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (to_rel(g["path"]), g["name"], g["version"], g["platform"], g["engine"], g["exe"], int(exe_locked),
         json.dumps(g["suspicious"], ensure_ascii=False), g["ad_files"], folder_size(g["path"]), now_str()),
    )
    refresh_icon(conn, cur.lastrowid, g["path"], g["exe"])
    return cur.lastrowid


def describe(path, exe=""):
    """认一个游戏文件夹（或 apk）。exe 给了就用它当主程序，文件夹认不出引擎也照样当游戏。"""
    path = Path(path)
    if path.is_file():
        return apk_entry(path) if path.suffix.lower() == ".apk" else None
    if not exe:
        return detect_game(path)
    files, dirs = _files_and_dirs(path)
    name, version = parse_name(path.name)
    return {"path": path, "name": name, "version": version, "platform": "PC",
            "engine": detect_engine(path, files, dirs)[0], "exe": exe, **scan_extras(path, files, exe)}


def add_game(path, exe=""):
    """把一个游戏文件夹（或 apk）直接登记进游戏库，返回 (编号, 是不是新加的)。

    不在 games 和安卓文件夹里的也能登记，扫描时不会把它当成不见了。
    """
    path = Path(path).resolve()
    g = describe(path, exe)
    if not g:
        raise ValueError("没认出游戏：文件夹里找不到能运行的 exe")
    with _db_lock, connect() as conn:
        row = conn.execute("SELECT id FROM games WHERE path=?", (to_rel(path),)).fetchone()
        if row:
            conn.execute("UPDATE games SET missing=0 WHERE id=?", (row["id"],))
            if exe:
                conn.execute("UPDATE games SET exe=?, exe_locked=1 WHERE id=?", (exe, row["id"]))
            return row["id"], False
        return insert_game(conn, g, exe_locked=bool(exe)), True


def find_by_path(path):
    """这个文件夹（或 apk）已经在游戏库里的话，返回那条记录的编号。"""
    with connect() as conn:
        row = conn.execute("SELECT id FROM games WHERE path=?", (to_rel(path),)).fetchone()
    return row["id"] if row else None


def games_under(path):
    """在这个文件夹里（或就是它）的游戏。"""
    path = Path(path).resolve()
    with connect() as conn:
        rows = conn.execute("SELECT id, name, path FROM games").fetchall()
    return [{"id": r["id"], "name": r["name"]} for r in rows
            if (p := to_abs(r["path"]).resolve()) == path or path in p.parents]


# ---------------------------------------------------------------- 查询与修改
def list_games(running=None):
    running = running or {}
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT g.*,
                   COALESCE(SUM(s.seconds), 0) AS play_seconds,
                   COUNT(s.id) AS play_count,
                   MAX(s.started_at) AS last_played
            FROM games g LEFT JOIN sessions s ON s.game_id = g.id
            GROUP BY g.id
            """
        ).fetchall()
    games = []
    for row in rows:
        g = dict(row)
        g["suspicious"] = json.loads(g["suspicious"] or "[]")
        g["screenshots"] = json.loads(g["screenshots"] or "[]")
        g["running_seconds"] = running.get(g["id"])
        games.append(g)
    return games


def get_game(game_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone()
    return dict(row) if row else None


EDITABLE = {"name", "version", "status", "rating", "tags", "notes"}


def update_game(game_id, fields):
    fields = {k: v for k, v in fields.items() if k in EDITABLE}
    if "status" in fields and fields["status"] not in STATUSES:
        raise ValueError("状态不对")
    if "rating" in fields:
        fields["rating"] = max(0, min(5, int(fields["rating"])))
    if "name" in fields and not str(fields["name"]).strip():
        raise ValueError("名字不能为空")
    if not fields:
        return
    if "name" in fields:
        fields["name_locked"] = 1
    sets = ", ".join(f"{k}=?" for k in fields)
    with _db_lock, connect() as conn:
        conn.execute(f"UPDATE games SET {sets} WHERE id=?", (*fields.values(), game_id))


def set_exe(game_id, exe_rel):
    game = get_game(game_id)
    folder = to_abs(game["path"]).resolve()
    target = (folder / exe_rel).resolve()
    if folder not in target.parents or target.suffix.lower() != ".exe" or not target.is_file():
        raise ValueError("只能选这个游戏文件夹里的 exe")
    rel = str(target.relative_to(folder))
    with _db_lock, connect() as conn:
        conn.execute("UPDATE games SET exe=?, exe_locked=1 WHERE id=?", (rel, game_id))
        refresh_icon(conn, game_id, folder, rel)


def set_meta(game_id, fields):
    """刮削结果写进数据库。"""
    fields = {k: v for k, v in fields.items() if k in META_FIELDS}
    if "screenshots" in fields:
        fields["screenshots"] = json.dumps(fields["screenshots"], ensure_ascii=False)
    sets = ", ".join(f"{k}=?" for k in fields)
    with _db_lock, connect() as conn:
        conn.execute(f"UPDATE games SET {sets} WHERE id=?", (*fields.values(), game_id))


def unprocessed_games():
    """还没自动找过资料的游戏。"""
    with connect() as conn:
        return [row["id"] for row in conn.execute("SELECT id FROM games WHERE scraped=0 AND missing=0")]


def folder_key(game):
    """文件夹名（apk 是文件名）拆出来的游戏名，不受后来改名影响。"""
    path = Path(game["path"])
    return parse_name(path.stem if game["platform"] == "安卓" else path.name)[0].lower()


def find_twin(game):
    """同一个游戏的 PC 版：资源站常把 PC 版和安卓版放在一起，文件夹名里的游戏名一样。"""
    key = folder_key(game)
    with connect() as conn:
        rows = conn.execute("SELECT * FROM games WHERE platform='PC' AND id<>? AND missing=0", (game["id"],)).fetchall()
    return next((dict(r) for r in rows if folder_key(r) == key), None)


def send_to_recycle_bin(path):
    """移到回收站。没有回收站的盘（比如网络盘）不删，免得直接永久删除。"""
    import win32file
    from win32com.shell import shell, shellcon

    if win32file.GetDriveType(path.anchor) != win32file.DRIVE_FIXED:
        raise ValueError("这个盘没有回收站，为了防止直接永久删除，请自己到文件夹里删")
    # 太大放不进回收站时，WANTNUKEWARNING 会让 Windows 先弹窗问，不会悄悄永久删除
    flags = shellcon.FOF_ALLOWUNDO | shellcon.FOF_NOCONFIRMATION | shellcon.FOF_WANTNUKEWARNING | shellcon.FOF_SILENT
    result, aborted = shell.SHFileOperation((0, shellcon.FO_DELETE, str(path), None, flags, None, None))
    if aborted:
        raise ValueError("删除已取消")
    if result != 0 or path.exists():
        raise OSError(f"没能移到回收站（错误码 {result}），可能有文件正被别的程序占用")


def delete_game_files(game_id):
    """把游戏文件移到回收站，再从游戏库移除。"""
    game = get_game(game_id)
    path = to_abs(game["path"]).resolve()
    roots, _ = watch_roots()
    if not any(r in path.parents for r in roots):
        raise ValueError("只能删除 games 或安卓文件夹里的游戏")
    if path.exists():
        send_to_recycle_bin(path)
    remove_game(game_id)
    prune_empty_parents(path.parent, roots)


def remove_game(game_id):
    """只从游戏库里移除记录，不动硬盘上的游戏文件。"""
    with _db_lock, connect() as conn:
        conn.execute("DELETE FROM sessions WHERE game_id=?", (game_id,))
        conn.execute("DELETE FROM games WHERE id=?", (game_id,))
    for f in COVER_DIR.glob(f"{game_id}-*"):
        f.unlink(missing_ok=True)


# ---------------------------------------------------------------- 游玩记录
def recent_sessions(game_id, limit=20):
    with connect() as conn:
        rows = conn.execute(
            "SELECT started_at, ended_at, seconds FROM sessions WHERE game_id=? ORDER BY started_at DESC LIMIT ?",
            (game_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def start_session(game_id):
    with _db_lock, connect() as conn:
        cur = conn.execute("INSERT INTO sessions (game_id, started_at) VALUES (?, ?)", (game_id, now_str()))
        return cur.lastrowid


def update_session(session_id, seconds, finished=False):
    with _db_lock, connect() as conn:
        if finished:
            conn.execute("UPDATE sessions SET seconds=?, ended_at=? WHERE id=?", (seconds, now_str(), session_id))
        else:
            conn.execute("UPDATE sessions SET seconds=? WHERE id=?", (seconds, session_id))


if __name__ == "__main__":
    init_db()
    result = scan(full=True)
    print(f"认出 {result['total']} 个游戏，新入库 {len(result['added'])} 个：{result['added']}")
    for g in list_games():
        print(f"  [{g['platform']}] {g['name']} {g['version']}  引擎={g['engine'] or '?'}  主程序={g['exe'] or '-'}"
              f"  可疑={g['suspicious']}  广告={g['ad_files']}  封面={g['cover'] or '-'}")
