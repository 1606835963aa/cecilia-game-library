"""游戏资料刮削：从游戏文件和资料网站找游戏的真名、封面、横幅大图、Logo、简介和截图。

来源：
- 游戏文件（不联网）：Ren'Py / Godot / RPG Maker / Unity 游戏里存的游戏名和主菜单背景、Logo
- Steam：商店公开接口，有竖版封面、横幅大图、Logo、中文简介、截图
- VNDB（视觉小说数据库）：公开接口，很多 Ren'Py 游戏在上面，有封面、简介、标签、截图
- DLsite：日本同人游戏商店，按作品编号（RJ 开头）查最准；用的是它自己网页在用的接口和作品网页
- itch.io：独立游戏商店，用的是搜索网页和每个游戏页面的 data.json
- SubscribeStar：订阅平台作者主页的公开介绍、头像和横幅
网页资料来源改版后，解析规则可能要跟着改。

只读游戏文件里的数据，不会运行游戏里的任何东西。
"""
import html
import io
import json
import pickle
import queue
import re
import ssl
import struct
import threading
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError

import library
import wishlist
from auto_extract import load_config

try:
    # 用 certifi 自带的证书列表：Windows 证书库里有过期的旧根证书，Python 有时会因此连不上 VNDB
    import certifi

    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) gamegame-library/1.0"  # itch.io 会拦不以 Mozilla 开头的请求
DOWNLOAD_LIMIT = 30 * 1024 * 1024
MAX_SCREENSHOTS = 8
SMALL_HERO_WIDTH = 1100

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")
# 文件名里带这些词的图片，可能是主菜单背景、宣传图
HERO_WORDS = re.compile(r"main_?menu|title|cover|key_?art|poster|splash|menu_?bg|background|wallpaper", re.I)
LOGO_WORDS = re.compile(r"logo", re.I)
# 界面零件、社交平台图标、引擎自带图片，不要
SKIP_WORDS = re.compile(
    r"overlay|button|/bar/|frame|textbox|namebox|phone/|slider|scroll|icon|cursor|choice|notify|nvl|"
    r"patreon|discord|steam|itch|twitter|subscribestar|kofi|godot|renpy|addons/|gut",
    re.I,
)


# ---------------------------------------------------------------- 网络
def http_get(url, payload=None, headers=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, "Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as resp:
        body = resp.read(DOWNLOAD_LIMIT + 1)
    if len(body) > DOWNLOAD_LIMIT:
        raise ValueError("文件太大")
    return body


def get_json(url, payload=None, headers=None):
    return json.loads(http_get(url, payload, headers))


def get_text(url, headers=None):
    return http_get(url, headers=headers).decode("utf-8", "replace")


def full_url(url):
    return "https:" + url if url and url.startswith("//") else (url or "")


def inner_div(page, marker):
    """网页里包含 marker 的那个 <div>（连同里面嵌套的 div）整段取出来。"""
    i = page.find(marker)
    start = page.rfind("<div", 0, i) if i >= 0 else -1
    if start < 0:
        return ""
    depth = 0
    for m in re.finditer(r"<div\b|</div>", page[start:]):
        depth += 1 if m.group() == "<div" else -1
        if depth == 0:
            return page[start:start + m.end()]
    return page[start:]


def html_text(fragment):
    """网页片段转成纯文字，保留分段。"""
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</h\d>", "\n", fragment, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    lines = [re.sub(r"[ \t\u3000]+", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def has_transparency(img):
    return img.mode in ("RGBA", "LA", "P") and img.convert("RGBA").getextrema()[3][0] < 255


def open_image(data):
    from PIL import Image

    img = Image.open(io.BytesIO(data))
    img.load()
    return img


def download_image(url):
    try:
        return open_image(http_get(url)) if url else None
    except Exception:
        return None


def normalize(text):
    """比较名字用：只留字母、数字和中日文，忽略大小写、空格和标点。"""
    text = re.sub(r"【[^】]*】", "", (text or "").lower().replace("’", "'"))  # DLsite 标题前的【中文版】之类
    return re.sub(r"[^0-9a-z぀-ヿ一-鿿]+", "", text)


VERSION_NUMBER = r"(\d+(?:\.\d+){1,4}(?:[ -]?(?:alpha|beta|rc|a|b)\d*)?)"


def version_fields(title="", description="", page=""):
    """只认明确标注的版本和当前下载文件名，更新日期、历史版本列表不能当当前版本。"""
    def unique(values):
        values = list(dict.fromkeys(v.strip() for v in values))
        return values[0] if len(values) == 1 else ""

    candidates = (
        (re.findall(r"(?:latest|current|最新|当前)\s*(?:version|版本|バージョン)?\s*[:：=]?\s*v?" + VERSION_NUMBER, description, re.I), "官网说明"),
        (re.findall(r"(?:\bv(?:er(?:sion)?\.?)?|版本|バージョン)\s*[:：=]?\s*" + VERSION_NUMBER, title, re.I), "作品标题"),
        (re.findall(r"[\[【(（]\s*v?" + VERSION_NUMBER + r"\s*[\]】)）]", title, re.I), "作品标题"),
        (re.findall(r'["\']softwareVersion["\']\s*:\s*["\']v?' + VERSION_NUMBER, page, re.I), "官网版本字段"),
        (re.findall(r"^\s*(?:version|版本|バージョン)\s*[:：=]?\s*v?" + VERSION_NUMBER, description, re.I | re.M), "官网说明"),
    )
    for values, evidence in candidates:
        value = unique(values)
        if value:
            return {"online_version": value, "version_evidence": evidence}
    names = re.findall(r'<[^>]+class=["\'][^"\']*\bupload_name\b[^"\']*["\'][^>]*>(.*?)</(?:div|span)>', page, re.S | re.I)
    versions = []
    for name in names:
        filename = re.match(r"(.+?\.(?:zip|7z|rar|exe|apk|tar(?:\.gz|\.bz2)?))(?=\s|$)", html_text(name), re.I)
        if not filename:
            continue
        for value in re.findall(r"(?:^|[\s_\-])v?" + VERSION_NUMBER + r"(?=[\s_.\-]|$)", filename.group(1), re.I):
            if not re.fullmatch(r"20\d{2}\.\d{1,2}\.\d{1,2}", value):
                versions.append(value)
    value = unique(versions)
    return {"online_version": value, "version_evidence": "官网下载文件名" if value else ""}


# ---------------------------------------------------------------- 游戏文件
class Candidate:
    """游戏文件里的一张图片。read(n) 读前 n 个字节，不传 n 读全部。"""

    def __init__(self, label, nbytes, read):
        self.label, self.nbytes, self.read = label, nbytes, read
        self.size = None

    def probe(self):
        """只读文件头拿到宽高，不解码整张图。"""
        if self.size is None:
            try:
                from PIL import Image

                self.size = Image.open(io.BytesIO(self.read(256 * 1024))).size
            except Exception:
                self.size = (0, 0)
        return self.size

    def image(self):
        try:
            return open_image(self.read())
        except Exception:
            return None


def file_candidate(path, label):
    def read(n=None):
        with open(path, "rb") as f:
            return f.read(n) if n else f.read()

    return Candidate(label, path.stat().st_size, read)


class _BasicTypesOnly(pickle.Unpickler):
    """Ren'Py 资源包的目录是 pickle 格式。pickle 能夹带代码，这里只放行基本数据类型。"""

    def find_class(self, module, name):
        if (module, name) == ("_codecs", "encode"):  # 老版本 pickle 存 bytes 的方式，无害
            import _codecs

            return _codecs.encode
        raise pickle.UnpicklingError(f"不允许的类型：{module}.{name}")


def read_rpa(path):
    """读 Ren'Py 的 .rpa 资源包目录，返回 {文件名: Candidate}。"""
    import zlib

    with open(path, "rb") as f:
        parts = f.readline(200).split()
        if not parts or parts[0] not in (b"RPA-3.0", b"RPA-2.0"):
            return {}
        offset = int(parts[1], 16)
        key = int(parts[2], 16) if parts[0] == b"RPA-3.0" else 0
        f.seek(offset)
        index = _BasicTypesOnly(io.BytesIO(zlib.decompress(f.read())), encoding="latin-1").load()

    found = {}
    for name, chunks in index.items():
        if not isinstance(name, str) or not name.lower().endswith(IMAGE_EXT) or not chunks:
            continue
        chunk = chunks[0]
        start, length = chunk[0] ^ key, chunk[1] ^ key
        prefix = chunk[2] if len(chunk) > 2 else b""
        if isinstance(prefix, str):
            prefix = prefix.encode("latin-1")

        def read(n=None, start=start, length=length, prefix=prefix):
            want = length if n is None else min(n, length)
            with open(path, "rb") as f:
                f.seek(start)
                return (prefix + f.read(want - len(prefix)))[:want]

        found[name] = Candidate(name, length, read)
    return found


def renpy_info(folder):
    game_dir = folder / "game"
    images = {}
    for rpa in sorted(game_dir.glob("*.rpa")):
        try:
            images.update(read_rpa(rpa))
        except Exception:
            continue
    for path in game_dir.rglob("*"):
        if path.suffix.lower() in IMAGE_EXT and "cache" not in path.parts:
            label = path.relative_to(game_dir).as_posix()
            images[label] = file_candidate(path, label)
    return None, list(images.values())


GODOT_IMPORTED = re.compile(r"(?:\.godot/imported|\.import)/(.+?)-[0-9a-f]{32}\.(?:ctex|stex)$")


def find_pck(folder, exe):
    """Godot 的资源包：和 exe 同名的 .pck，或者直接打包在 exe 末尾。返回 (文件, 资源包开头位置)。"""
    exe_path = folder / exe
    pck = exe_path.with_suffix(".pck")
    if pck.is_file():
        return pck, 0
    try:
        with open(exe_path, "rb") as f:
            f.seek(-12, 2)
            size, magic = struct.unpack("<Q4s", f.read(12))
            if magic == b"GDPC":
                return exe_path, f.tell() - 12 - size
    except OSError:
        pass
    return None, 0


def read_pck(path, start):
    """读 Godot 资源包目录，返回 [(资源路径, 文件里的位置, 大小)]。"""
    with open(path, "rb") as f:
        f.seek(start)
        magic, fmt = struct.unpack("<4sI", f.read(8))
        if magic != b"GDPC":
            return []
        f.read(12)  # 引擎版本号
        flags, base = struct.unpack("<IQ", f.read(12)) if fmt >= 2 else (0, 0)
        if flags & 1:  # 目录加密了
            return []
        if fmt >= 3:
            (dir_offset,) = struct.unpack("<Q", f.read(8))
            f.seek(start + dir_offset)
        else:
            f.read(64)
        (count,) = struct.unpack("<I", f.read(4))
        entries = []
        for _ in range(min(count, 500000)):
            (n,) = struct.unpack("<I", f.read(4))
            name = f.read(n).rstrip(b"\0").decode("utf-8", "replace").removeprefix("res://")
            offset, size = struct.unpack("<QQ", f.read(16))
            f.read(16)  # md5
            entry_flags = struct.unpack("<I", f.read(4))[0] if fmt >= 2 else 0
            if not entry_flags & 1:  # 1 = 这个文件加密了
                entries.append((name, offset, size))

        # 各版本记录位置的方式不一样，用 project.binary 的文件头（ECFG）试出真正的起点
        bases = dict.fromkeys((start + base, start, 0))
        probe = next((e for e in entries if e[0] == "project.binary"), None)
        shift = start + base
        if probe:
            for b in bases:
                f.seek(b + probe[1])
                if f.read(4) == b"ECFG":
                    shift = b
                    break
    return [(name, shift + offset, size) for name, offset, size in entries]


def godot_project_name(data):
    """从 project.binary（Godot 的项目设置）里读 application/config/name。"""
    if data[:4] != b"ECFG":
        return None
    (count,) = struct.unpack_from("<I", data, 4)
    pos = 8
    for _ in range(count):
        (klen,) = struct.unpack_from("<I", data, pos)
        key = data[pos + 4:pos + 4 + klen]
        pos += 4 + klen
        (vlen,) = struct.unpack_from("<I", data, pos)
        value = data[pos + 4:pos + 4 + vlen]
        pos += 4 + vlen
        if key == b"application/config/name":
            if struct.unpack_from("<I", value)[0] & 0xFF != 4:  # 4 = 字符串
                return None
            (slen,) = struct.unpack_from("<I", value, 4)
            return value[8:8 + slen].decode("utf-8", "replace").strip() or None
    return None


def godot_texture(path, offset, size, label):
    """Godot 导入后的图片（.ctex / .stex）里面包着一张完整的 png 或 webp。"""

    def read(n=None):
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read(size if n is None else min(size, n + 256))
        for sig in (b"RIFF", b"\x89PNG"):
            i = data.find(sig, 0, 256)
            if i >= 0:
                return data[i:]
        return b""

    cand = Candidate(label, size, read)
    with open(path, "rb") as f:
        f.seek(offset)
        head = f.read(16)
    if head[:4] == b"GST2":
        cand.size = struct.unpack_from("<II", head, 8)
    elif head[:4] == b"GDST":
        cand.size = (struct.unpack_from("<H", head, 4)[0], struct.unpack_from("<H", head, 8)[0])
    return cand


def godot_info(folder, exe):
    pck, start = find_pck(folder, exe)
    if not pck:
        return None, []
    entries = read_pck(pck, start)
    name, images = None, []
    with open(pck, "rb") as f:
        for res, offset, size in entries:
            if res == "project.binary":
                f.seek(offset)
                name = godot_project_name(f.read(size))
            m = GODOT_IMPORTED.search(res)
            if m and m.group(1).lower().endswith(IMAGE_EXT) and (HERO_WORDS.search(m.group(1)) or LOGO_WORDS.search(m.group(1))):
                images.append(godot_texture(pck, offset, size, m.group(1)))
    return name, images


def rpgmaker_mv_info(folder):
    base = folder / "www" if (folder / "www").is_dir() else folder
    try:
        system = json.loads((base / "data" / "System.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None, []
    key = bytes.fromhex(system.get("encryptionKey") or "") if system.get("hasEncryptedImages") else b""
    images = []
    for path in sorted((base / "img" / "titles1").glob("*")):
        suffix = path.suffix.lower()
        if suffix not in (".png", ".rpgmvp", ".png_"):
            continue

        def read(n=None, path=path, encrypted=suffix != ".png"):
            data = path.read_bytes()
            if encrypted:  # 加密图片：去掉 16 字节的假文件头，前 16 字节和密钥异或
                if not key:
                    return b""
                data = data[16:]
                data = bytes(b ^ key[i % len(key)] for i, b in enumerate(data[:16])) + data[16:]
            return data

        images.append(Candidate(f"titles1/{path.stem}", path.stat().st_size, read))
    return (system.get("gameTitle") or "").strip() or None, images


def ini_title(folder):
    """RPG Maker VX/XP 的 Game.ini 里有游戏名。"""
    raw = (folder / "Game.ini").read_bytes() if (folder / "Game.ini").is_file() else b""
    for enc in ("utf-8", "cp932", "gbk"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        return None
    m = re.search(r"^Title=(.+)$", text, re.M)
    return m.group(1).strip() if m else None


def unity_info(folder):
    """开发者和游戏名一起读取，站内搜索漏掉作品时还能核对官方页面。"""
    for info in folder.glob("*_Data/app.info"):
        lines = info.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) >= 2 and lines[1].strip():
            return lines[1].strip(), lines[0].strip()
    return None, ""


def pick_hero(images):
    """挑一张当横幅大图：横版、够大、细节够多（纯色背景压缩后很小），文件名像主菜单背景的优先。"""
    best, best_score = None, None
    for c in images:
        if SKIP_WORDS.search(c.label) or not HERO_WORDS.search(c.label):
            continue
        w, h = c.probe()
        if w < 1200 or not 1.3 <= w / max(h, 1) <= 2.6 or c.nbytes / (w * h) < 0.08:
            continue
        score = c.nbytes
        if best_score is None or score > best_score:
            best, best_score = c, score
    return best.image() if best else None


def pick_logo(images):
    """挑一张透明底的 Logo，大的优先。"""
    for c in sorted((c for c in images if LOGO_WORDS.search(c.label) and not SKIP_WORDS.search(c.label)),
                    key=lambda c: c.nbytes, reverse=True)[:4]:
        w, h = c.probe()
        if w < 300:
            continue
        img = c.image()
        if img is not None and has_transparency(img):
            return img
    return None


def local_info(game):
    """从游戏文件里找真名、横幅大图和 Logo。"""
    result = {"name": None, "developer": "", "hero": None, "logo": None}
    folder = library.to_abs(game["path"])
    if game["platform"] != "PC" or not folder.is_dir():
        return result
    engine = game["engine"]
    try:
        if engine == "Ren'Py":
            name, images = renpy_info(folder)
        elif engine == "Godot":
            name, images = godot_info(folder, game["exe"])
        elif engine == "RPG Maker MV/MZ":
            name, images = rpgmaker_mv_info(folder)
        elif engine == "RPG Maker VX/XP":
            name, images = ini_title(folder), []
        elif engine == "Unity":
            name, result["developer"] = unity_info(folder)
            images = []
        else:
            return result
    except Exception:
        return result
    result["name"] = name
    if engine.startswith("RPG Maker MV"):
        result["hero"] = max((c.image() for c in images), key=lambda i: i.width if i else 0, default=None)
    else:
        result["hero"] = pick_hero(images)
        result["logo"] = pick_logo(images)
    return result


# ---------------------------------------------------------------- Steam
STEAM_ASSETS = "https://shared.akamai.steamstatic.com/store_item_assets/"


def steam_search(term):
    url = "https://store.steampowered.com/api/storesearch/?" + urllib.parse.urlencode({"term": term, "l": "english", "cc": "US"})
    return [
        {"source": "steam", "id": str(i["id"]), "title": i["name"], "thumb": i.get("tiny_image", ""), "year": "", "developer": ""}
        for i in get_json(url).get("items", [])[:8]
    ]


def steam_assets(appid):
    """Steam 游戏库用的图（竖版封面、横幅、Logo）地址带随机串，要从这个接口查。"""
    query = {"ids": [{"appid": int(appid)}], "context": {"language": "schinese", "country_code": "US"},
             "data_request": {"include_assets": True}}
    url = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/?input_json=" + urllib.parse.quote(json.dumps(query))
    items = get_json(url).get("response", {}).get("store_items") or [{}]
    assets = items[0].get("assets") or {}
    fmt = assets.get("asset_url_format", "")

    def url_of(*keys):
        for k in keys:
            if assets.get(k) and fmt:
                return STEAM_ASSETS + fmt.replace("${FILENAME}", assets[k])
        return ""

    return url_of


def steam_details(appid):
    data = get_json(f"https://store.steampowered.com/api/appdetails?appids={appid}&l=schinese&cc=US")[appid]
    if not data.get("success"):
        raise LookupError("Steam 上没有这个游戏的资料")
    d = data["data"]
    asset = steam_assets(appid)
    release = d.get("release_date") or {}
    return {
        "source": "steam", "id": appid, "title": d.get("name", ""),
        **version_fields(d.get("name", ""), html_text(d.get("detailed_description") or "")),
        "description": html.unescape(re.sub(r"<[^>]+>", "", d.get("short_description") or "")).strip(),
        "developer": "、".join(d.get("developers") or []),
        "released": release.get("date", ""),
        "genres": [g["description"] for g in d.get("genres") or []],
        "cover": asset("library_capsule_2x", "library_capsule"),
        "hero": asset("library_hero_2x", "library_hero") or d.get("background_raw", "") or d.get("header_image", ""),
        "logo": asset("library_logo_2x", "library_logo"),
        "screenshots": [s["path_full"] for s in (d.get("screenshots") or [])[:MAX_SCREENSHOTS]],
    }


# ---------------------------------------------------------------- VNDB
VNDB_API = "https://api.vndb.org/kana/vn"


def vndb_search(term):
    payload = {"filters": ["search", "=", term], "fields": "title, alttitle, released, image.thumbnail, developers.name", "results": 8}
    return [
        {"source": "vndb", "id": v["id"], "title": v["title"], "alt": v.get("alttitle") or "",
         "thumb": (v.get("image") or {}).get("thumbnail", ""), "year": (v.get("released") or "")[:4],
         "developer": "、".join(d["name"] for d in v.get("developers") or [])}
        for v in get_json(VNDB_API, payload).get("results", [])
    ]


def clean_bbcode(text):
    text = re.sub(r"\[spoiler\].*?\[/spoiler\]", "", text or "", flags=re.S | re.I)
    text = re.sub(r"\[url=[^\]]*\](.*?)\[/url\]", r"\1", text, flags=re.S | re.I)
    return re.sub(r"\[/?\w+[^\]]*\]", "", text).strip()


def vndb_details(vid):
    fields = ("title, released, description, image.url, image.dims, developers.name, "
              "tags.name, tags.rating, tags.spoiler, screenshots.url, screenshots.dims")
    results = get_json(VNDB_API, {"filters": ["id", "=", vid], "fields": fields}).get("results")
    if not results:
        raise LookupError("VNDB 上没有这个游戏的资料")
    v = results[0]
    image = v.get("image") or {}
    w, h = image.get("dims") or (0, 0)
    shots = [s for s in v.get("screenshots") or [] if s.get("url")]
    landscape_shot = next((s["url"] for s in shots if s["dims"][0] > s["dims"][1]), "")
    tags = sorted((t for t in v.get("tags") or [] if not t.get("spoiler")), key=lambda t: -t.get("rating", 0))
    portrait = h > w * 1.1
    return {
        "source": "vndb", "id": vid, "title": v.get("title", ""),
        **version_fields(v.get("title", ""), clean_bbcode(v.get("description"))),
        "description": clean_bbcode(v.get("description")),
        "developer": "、".join(d["name"] for d in v.get("developers") or []),
        "released": v.get("released") or "",
        "genres": [t["name"] for t in tags[:8]],
        # VNDB 的封面有竖有横：竖的当封面，横的当横幅大图
        "cover": image.get("url", "") if portrait else "",
        "hero": landscape_shot if portrait else image.get("url", "") or landscape_shot,
        "logo": "",
        "screenshots": [s["url"] for s in shots[:MAX_SCREENSHOTS]],
    }


# ---------------------------------------------------------------- DLsite
DLSITE = "https://www.dlsite.com"
DLSITE_HEADERS = {"Cookie": "adultchecked=1; locale=zh_CN"}  # 跳过成人确认页，资料尽量用中文
DLSITE_CODE = re.compile(r"(?<![A-Za-z0-9])((?:RJ|VJ|BJ|RE)\d{6,8})(?![0-9])", re.I)


def dlsite_title(name):
    return re.sub(r"^\s*【[^】]*】\s*", "", name or "")  # 去掉标题前的【中文版】之类语言标签


def dlsite_info(codes):
    """按作品编号批量查基本信息（作品名、主图、发售日期、所在分站）。"""
    url = f"{DLSITE}/maniax/product/info/ajax?" + urllib.parse.urlencode({"product_id": ",".join(codes)})
    data = get_json(url, headers=DLSITE_HEADERS)
    return data if isinstance(data, dict) else {}


def dlsite_search(term):
    params = {"term": term, "site": "adult-jp", "time": int(datetime.now().timestamp() * 1000), "touch": 0}
    url = f"{DLSITE}/suggest-api/global-search?" + urllib.parse.urlencode(params)
    works = (get_json(url, headers=DLSITE_HEADERS).get("work") or [])[:8]
    info = dlsite_info([w["workno"] for w in works]) if works else {}
    results = []
    for w in works:
        i = info.get(w["workno"]) or {}
        results.append({"source": "dlsite", "id": w["workno"], "title": dlsite_title(i.get("work_name") or w["work_name"]),
                        "alt": w["work_name"], "thumb": full_url(i.get("work_image")),
                        "year": (i.get("regist_date") or "")[:4], "developer": w.get("maker_name", "")})
    return results


def dlsite_details(code):
    code = code.upper()
    info = dlsite_info([code]).get(code)
    if not info:
        raise LookupError("DLsite 上没有这个作品")
    page = get_text(f"{DLSITE}/{info.get('site_id') or 'maniax'}/work/=/product_id/{code}.html", headers=DLSITE_HEADERS)
    og_title = re.search(r'<meta property="og:title" content="([^"]*)"', page)
    maker = re.search(r"\[([^\]]+)\] \| DLsite", html.unescape(og_title.group(1))) if og_title else None
    genre_block = re.search(r'<div class="main_genre">(.*?)</div>', page, re.S)
    shots = re.findall(r'(//img\.dlsite\.jp/modpub/images2/work/[^"\'\s]+?_img_smp\d+\.(?:jpg|png|webp))', page)
    return {
        "source": "dlsite", "id": code, "title": dlsite_title(info.get("work_name")),
        **version_fields(dlsite_title(info.get("work_name")), html_text(inner_div(page, 'itemprop="description"')), page),
        "description": html_text(inner_div(page, 'itemprop="description"')),
        "developer": maker.group(1) if maker else "",
        "released": (info.get("regist_date") or "")[:10],
        "genres": [html.unescape(g) for g in re.findall(r">([^<>]+)</a>", genre_block.group(1))][:8] if genre_block else [],
        # DLsite 的主图是横版宣传图，当横幅大图用
        "cover": "", "hero": full_url(info.get("work_image")), "logo": "",
        "screenshots": [full_url(u) for u in dict.fromkeys(shots)][:MAX_SCREENSHOTS],
    }


def find_dlsite_code(game):
    """资源站的文件夹名、说明 txt 里常带 DLsite 作品编号（RJ 开头），有编号就能精确查到。"""
    path = library.to_abs(game["path"])
    texts = [p.name for p in (path, *path.parents)][:4]
    folder = path if path.is_dir() else path.parent
    for f in folder.glob("*.txt"):
        try:
            raw = f.read_bytes() if f.stat().st_size < 50_000 else b""
        except OSError:
            continue
        for enc in ("utf-8", "gbk"):
            try:
                texts.append(raw.decode(enc))
                break
            except UnicodeDecodeError:
                continue
    for text in texts:
        m = DLSITE_CODE.search(text)
        if m:
            return m.group(1).upper()
    return None


# ---------------------------------------------------------------- itch.io
ITCH_GAME = re.compile(r"https?://([\w-]+)\.itch\.io/([\w-]+)", re.I)
itch_browser = None


def itch_url(text):
    m = ITCH_GAME.search(text or "")
    return f"https://{m.group(1).lower()}.itch.io/{m.group(2).lower()}" if m else None


def itch_date(text):
    """itch.io 写的是 “11 October 2026 @ 20:32 UTC”，统一成 2026-10-11。"""
    try:
        return datetime.strptime(text.strip(), "%d %B %Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def itch_search(term):
    url = "https://itch.io/search?" + urllib.parse.urlencode({"q": term})
    page = itch_browser.search_page(url) if itch_browser else get_text(url)
    results = []
    for cell in re.split(r'class="game_cell[ "]', page)[1:9]:
        tag = re.search(r'<a\b([^>]*class="title game_link"[^>]*)>([^<]*)</a>', cell)
        href = re.search(r'href="([^"]+)"', tag.group(1)) if tag else None
        url = itch_url(href.group(1)) if href else None
        if not url:
            continue
        thumb = re.search(r'data-lazy_src="([^"]+)"', cell)
        author = re.search(r'class="game_author"><a[^>]*>([^<]+)<', cell)
        results.append({"source": "itch", "id": url, "title": html.unescape(tag.group(2)).strip(),
                        "thumb": thumb.group(1) if thumb else "", "year": "",
                        "developer": html.unescape(author.group(1)) if author else ""})
    return results


def itch_icon(page, url):
    """取作品网页声明的小图标，兼容链接属性的不同顺序。"""
    tag = re.search(r'<link\b[^>]*\brel=["\'](?:shortcut\s+)?icon["\'][^>]*>', page, re.I)
    href = re.search(r'\bhref=["\']([^"\']+)', tag.group(0), re.I) if tag else None
    return urllib.parse.urljoin(url, html.unescape(href.group(1))) if href else ""


def itch_details(url):
    url = itch_url(url)
    if not url:
        raise LookupError("不是 itch.io 的游戏页面地址")
    data = get_json(url + "/data.json")
    page = get_text(url)
    key_art = (re.search(r'<meta content="([^"]+)" property="og:image"', page)
               or re.search(r'property="og:image" content="([^"]+)"', page))
    header = re.search(r'id="header"[^>]*>\s*<img[^>]+src="([^"]+)"', page)
    shots = re.findall(r'<a[^>]+data-image_lightbox[^>]+href="([^"]+)"', page) or data.get("screenshots") or []
    rows = {k.strip(): v for k, v in re.findall(r"<tr><td>([^<]+)</td><td>(.*?)</td></tr>", page)}
    published = re.search(r'title="([^"@]+)', rows.get("Published") or rows.get("Release date") or "")
    genres = [g.strip() for k in ("Genre", "Tags") for g in html_text(rows.get(k, "")).split(",") if g.strip()]
    return {
        "source": "itch", "id": url, "title": data.get("title", ""),
        **version_fields(data.get("title", ""), html_text(inner_div(page, 'class="formatted_description')), page),
        "description": html_text(inner_div(page, 'class="formatted_description')),
        "developer": "、".join(a["name"] for a in data.get("authors") or []),
        "released": itch_date(published.group(1)) if published else "",
        "genres": list(dict.fromkeys(genres))[:8],
        # itch.io 的封面是横版宣传图，当横幅大图用；页面顶部的标题图如果是透明底就当 Logo
        "cover": "", "hero": key_art.group(1) if key_art else data.get("cover_image", ""),
        "logo": header.group(1) if header else "",
        "icon": itch_icon(page, url),
        "screenshots": shots[:MAX_SCREENSHOTS],
    }


def itch_from_developer(term, developer):
    """只接受游戏名和开发者都对得上的候选，不能把同名作品自动套进来。"""
    slug = re.sub(r"[^a-z0-9]+", "-", term.lower()).strip("-")
    author = re.sub(r"[^a-z0-9-]", "", developer.lower())
    if (not slug or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", author)
            or normalize(developer) in ("defaultcompany", "companyname", "unknown")):
        return []
    try:
        meta = itch_details(f"https://{author}.itch.io/{slug}")
    except HTTPError as error:
        if error.code in (404, 410):
            return []
        raise
    except LookupError:
        return []
    if normalize(meta["title"]) != normalize(term) or normalize(meta["developer"]) != normalize(developer):
        return []
    return [search_result(meta)]


# ---------------------------------------------------------------- 订阅平台公开主页
class PageMetadata(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.meta = {}
        self.images = []
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta":
            key = attrs.get("property") or attrs.get("name")
            if key:
                self.meta[key.lower()] = attrs.get("content", "")
        elif tag == "img":
            self.images.append(attrs)


def subscribestar_url(text):
    try:
        parsed = urllib.parse.urlsplit(str(text).strip())
        host = (parsed.hostname or "").lower().removeprefix("www.")
        slug = parsed.path.strip("/")
        reserved = {"posts", "search", "creators", "login", "sign_up", "subscriptions", "features", "pricing", "about"}
        if (parsed.scheme not in ("https", "http") or parsed.username or parsed.password
                or parsed.port not in (None, 80, 443)
                or host not in ("subscribestar.adult", "subscribestar.art", "subscribestar.com")
                or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", slug, re.I) or slug.lower() in reserved):
            return None
        host = "subscribestar.adult" if host == "subscribestar.art" else host
        return f"https://{host}/{slug.lower()}"
    except ValueError:
        return None


def subscribestar_details(url):
    url = subscribestar_url(url)
    if not url:
        raise LookupError("请粘贴订阅平台的作者主页地址")
    try:
        page = get_text(url)
    except HTTPError as error:
        if error.code in (404, 410):
            raise LookupError("没有找到这个作者主页") from None
        raise
    parsed = PageMetadata(page)
    raw_title = parsed.meta.get("og:title", "")
    title = re.sub(r"\s+on\s+subscribestar\.(?:adult|art|com)\s*$", "", raw_title, flags=re.I).strip()
    if not title or normalize(title) in ("subscribestar", "subscribestaradult", "subscribestarart"):
        raise LookupError("没有读到这个作者主页的资料")
    description_tag = next((m for m in re.finditer(r'<div\b[^>]*\bclass=["\']([^"\']+)["\'][^>]*>', page, re.I)
                            if "profile_main_info-description" in m.group(1).split()), None)
    description = html_text(inner_div(page, description_tag.group(0)[4:])) if description_tag else ""
    if not description:
        raise LookupError("作者主页暂时没有公开介绍")

    def image_url(attrs):
        value = attrs.get("src", "") if attrs else ""
        if not value:
            return ""
        value = urllib.parse.urljoin(url, value)
        return value if urllib.parse.urlsplit(value).scheme in ("https", "http") else ""

    cover = next((a for a in parsed.images if "profile_main_info-cover" in a.get("class", "").split()), None)
    avatar = next((a for a in parsed.images if a.get("data-type") == "avatar" and normalize(a.get("alt")) == normalize(title)), None)
    return {
        "source": "subscribestar", "id": url, "title": title, "description": description,
        "developer": title, "released": "", "genres": [],
        **version_fields(title, description),
        "cover": image_url(avatar), "hero": image_url(cover), "logo": "", "icon": image_url(avatar),
        "screenshots": [],
    }


def subscribestar_search(term):
    """先核对同名作者主页，用户名不一致的主页可直接粘贴链接匹配。"""
    slug = re.sub(r"[^a-z0-9]+", "-", term.lower()).strip("-")
    if not slug or len(slug) > 100:
        return []
    try:
        meta = subscribestar_details(f"https://subscribestar.adult/{slug}")
    except HTTPError as error:
        if error.code in (404, 410):
            return []
        raise
    except LookupError:
        return []
    return [search_result(meta)] if normalize(meta["title"]) == normalize(term) else []


# ---------------------------------------------------------------- 搜索与匹配
SEARCHERS = {"steam": steam_search, "vndb": vndb_search, "dlsite": dlsite_search, "itch": itch_search, "subscribestar": subscribestar_search}
DETAILS = {"steam": steam_details, "vndb": vndb_details, "dlsite": dlsite_details, "itch": itch_details, "subscribestar": subscribestar_details}
SOURCE_LABELS = {"steam": "Steam", "vndb": "VNDB", "dlsite": "DLsite", "itch": "itch.io", "subscribestar": "SubscribeStar"}
# 自动匹配时几家都对上名字，用排在前面的（Steam 的图最适合游戏库）
SOURCE_ORDER = ("steam", "vndb", "dlsite", "itch", "subscribestar")
# 粘贴进来的游戏页面地址（DLsite 直接写作品编号也行）
LINKS = (
    ("steam", re.compile(r"(?:store\.steampowered\.com|steamcommunity\.com)/app/(\d+)", re.I)),
    ("vndb", re.compile(r"vndb\.org/(v\d+)", re.I)),
    ("dlsite", DLSITE_CODE),
    ("itch", ITCH_GAME),
)


def parse_link(text):
    """认出游戏页面地址是哪个网站的哪个游戏，返回 (来源, 编号)。"""
    subscription = subscribestar_url(text)
    if subscription:
        return "subscribestar", subscription
    for source, pattern in LINKS:
        m = pattern.search(text)
        if m:
            if source == "itch":
                return source, itch_url(text)
            return source, m.group(1).upper() if source == "dlsite" else m.group(1).lower()
    return None


def search_result(meta):
    return {"source": meta["source"], "id": meta["id"], "title": meta["title"], "alt": "",
            "thumb": meta.get("cover") or meta.get("hero", ""), "year": meta.get("released", "")[:4],
            "developer": meta.get("developer", "")}


def lookup_link(source, item_id):
    """粘贴链接时，直接查这一个游戏，做成和搜索结果一样的格式。"""
    try:
        meta = DETAILS[source](item_id)
    except LookupError:
        return [], []
    except Exception:
        return [], [source]
    return [search_result(meta)], []


def search(term, developer=""):
    """同时搜所有来源，返回 (结果列表, 出错的来源)。粘贴的是游戏页面地址就直接查那一个。"""
    link = parse_link(term)
    if link:
        return lookup_link(*link)
    results, errors = [], []
    with ThreadPoolExecutor(len(SEARCHERS)) as pool:
        futures = {src: pool.submit(fn, term) for src, fn in SEARCHERS.items()}
        for src, fut in futures.items():
            try:
                results.extend(fut.result())
            except Exception:
                errors.append(src)
    if developer and not exact_match(term, results):
        try:
            results.extend(itch_from_developer(term, developer))
        except Exception:
            if "itch" not in errors:
                errors.append("itch")
    return results, errors


def local_developer(game):
    if game.get("platform") != "PC":
        game = library.find_twin(game) or game
    if game.get("engine") == "Unity":
        try:
            return unity_info(library.to_abs(game["path"]))[1]
        except OSError:
            pass
    return game.get("developer") or ""


def exact_match(term, results):
    """名字完全对得上的才算（Steam 优先）；对不上就不自动套用，免得套错。"""
    key = normalize(term)
    for src in SOURCE_ORDER:
        for r in results:
            if r["source"] == src and key and key in (normalize(r["title"]), normalize(r.get("alt"))):
                return r
    return None


def can_update_site_icon(game):
    """官网图标只补空缺、失效缓存和已确认的引擎默认图标。"""
    name = game["icon"] or ""
    return "-custom" not in name and (
        not name or name == f"{game['id']}-site-icon.png"
        or not (library.COVER_DIR / name).is_file() or library.is_engine_icon(name)
    )


def needs_site_icon(game):
    """只给已匹配的作品补图，已有官网图标不重复下载。"""
    name = game["icon"] or ""
    return (
        not game["missing"] and game["meta_source"] == "itch" and bool(itch_url(game["meta_id"]))
        and (name != f"{game['id']}-site-icon.png" or not (library.COVER_DIR / name).is_file())
        and can_update_site_icon(game)
    )


def refresh_site_icon(game_id):
    """沿用已选定的作品地址，只补小图标，不重新搜索或改动其他资料。"""
    game = library.get_game(game_id)
    if not game or not needs_site_icon(game):
        return ""
    url = itch_url(game["meta_id"])
    image = download_image(itch_icon(get_text(url), url))
    if image is None:
        return "官网暂时没有可用的小图标"
    with library._db_lock, library.connect() as conn:
        current = conn.execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone()
        # 下载期间可能手动换了资料，不能把旧作品的图标写回去。
        if (not current or current["missing"] or current["meta_source"] != game["meta_source"]
                or current["meta_id"] != game["meta_id"] or current["icon"] != game["icon"]
                or not can_update_site_icon(current)):
            return ""
        name = library.save_image(image, f"{game_id}-site-icon.png", "icon")
        conn.execute("UPDATE games SET icon=? WHERE id=?", (name, game_id))
    return "已补上官网小图标"


def apply(game_id, meta, local):
    """把找到的资料和图片存下来。手动换过的封面、横幅大图和手动改过的名字不动。"""
    meta = meta or {}
    game = library.get_game(game_id)
    if not game:
        return {"game_id": game_id, "cancelled": True}
    urls = {k: meta.get(k, "") for k in ("cover", "hero", "logo", "icon")}
    shot_urls = meta.get("screenshots") or []
    with ThreadPoolExecutor(6) as pool:
        images = dict(zip(urls, pool.map(download_image, urls.values())))
        shots = [img for img in pool.map(download_image, shot_urls) if img is not None]
    current = library.get_game(game_id)
    if not current or (current["scrape_status"] == "cleared" and game["scrape_status"] != "cleared"):
        return {"game_id": game_id, "cancelled": True}
    # 下载期间用户可能换图或改名，保存时要以当下的手动设置为准。
    game = current
    cover = images["cover"]
    if cover is not None and cover.width > cover.height:  # 横版的“封面”放在封面墙上会被裁坏，改当背景
        images["hero"] = images["hero"] or cover
        images["cover"] = None
    if images["logo"] is not None and not has_transparency(images["logo"]):  # 没有透明底的不像 Logo
        images["logo"] = None
    hero, local_hero = images["hero"], local["hero"]
    # 网上的图只有几百像素宽（itch.io 常见），而游戏文件里有更大的主菜单图时，用游戏文件里的
    if hero is None or (local_hero is not None and hero.width < SMALL_HERO_WIDTH and local_hero.width > hero.width):
        images["hero"] = local_hero
    images["logo"] = images["logo"] or local["logo"]

    keep_shots = bool(shot_urls and not shots and game["meta_source"] == meta.get("source")
                      and str(game["meta_id"]) == str(meta.get("id")) and game["screenshots"] not in ("", "[]"))
    if not keep_shots:
        for f in library.COVER_DIR.glob(f"{game_id}-shot*.jpg"):
            f.unlink(missing_ok=True)
    fields = {}
    for kind, ext in (("cover", "jpg"), ("hero", "jpg"), ("logo", "png")):
        if (images[kind] is None and urls[kind] and game["meta_source"] == meta.get("source")
                and str(game["meta_id"]) == str(meta.get("id")) and game[kind]):
            continue
        name = f"{game_id}-{kind}.{ext}"
        if images[kind] is not None:
            library.save_image(images[kind], name, kind)
        else:
            (library.COVER_DIR / name).unlink(missing_ok=True)
            name = ""
        if "-custom" not in (game[kind] or ""):
            fields[kind] = name
    # 没有图标或只有引擎默认图标时用官网图标，游戏自己的图标仍然保留。
    site_icon = f"{game_id}-site-icon.png"
    current = library.get_game(game_id) or game
    current_icon = current["icon"] or ""
    if can_update_site_icon(current):
        if images["icon"] is not None:
            fields["icon"] = library.save_image(images["icon"], site_icon, "icon")
        elif (current_icon == site_icon and not urls["icon"]
              and (meta.get("source"), meta.get("id")) != (game["meta_source"], game["meta_id"])):
            (library.COVER_DIR / site_icon).unlink(missing_ok=True)
            native_icon = f"{game_id}-icon.png"
            fields["icon"] = native_icon if (library.COVER_DIR / native_icon).is_file() else ""
    if not keep_shots:
        fields["screenshots"] = [library.save_image(img, f"{game_id}-shot{i}.jpg", "shot") for i, img in enumerate(shots)]
    fields.update(
        description=meta.get("description", ""), developer=meta.get("developer") or local.get("developer", ""),
        released=meta.get("released", ""), genres="、".join(meta.get("genres") or []),
        meta_source=meta.get("source", ""), meta_id=meta.get("id", ""), scraped=1,
        scrape_status="matched" if meta.get("source") else "local",
    )
    new_name = meta.get("title") or local["name"]
    if new_name and not game["name_locked"]:
        fields["name"] = new_name
    library.set_meta(game_id, fields)
    if meta.get("source"):
        wishlist.record_version(game_id, meta.get("online_version"), meta.get("version_evidence", ""))
    fields["game_id"] = game_id
    return fields


def local_for(game):
    """游戏文件里的资料。安卓版的 apk 读不了，借同一个游戏 PC 版的。返回 (资料, PC 版)。"""
    if game["is_wishlist"]:
        return {"name": game["name"], "hero": None, "logo": None}, None
    twin = library.find_twin(game) if game["platform"] != "PC" else None
    return local_info(twin or game), twin


def auto_scrape(game_id, online=True):
    """自动找资料：先看游戏文件，再用真名和文件夹名去网上搜，名字完全对上才套用。

    返回 (说明, 是否连上了网)；未匹配或联网失败时保留补查状态，已有资料按原官网地址更新。
    """
    game = library.get_game(game_id)
    if not game or game["missing"]:
        return "游戏不在了", True
    if game["is_wishlist"]:
        if not online:
            return "愿望单资料已保存在本机", True
        meta = DETAILS[game["meta_source"]](game["meta_id"])
        apply(game_id, meta, local_for(game)[0])
        return "愿望单资料和版本已更新", True
    local, twin = local_for(game)
    names = [twin and twin["name"], local["name"], game["name"]]
    meta, reachable = None, True
    source = game["meta_source"]
    if online and source in DETAILS and game["meta_id"]:
        try:
            meta = DETAILS[source](game["meta_id"])
        except Exception:
            library.set_meta(game_id, {"scraped": 0, "scrape_status": "error"})
            return "官网暂时没连上，已保留原来的资料", False
    elif not online and source:
        return "已保留原来的官网资料", True
    code = find_dlsite_code(twin or game) if online and not meta else None
    if code and not meta:
        try:
            meta = dlsite_details(code)
        except LookupError:
            pass
        except Exception:
            reachable = False
    if online and not meta:
        terms = list(dict.fromkeys(t for t in names if t and normalize(t)))
        for term in terms:
            results, errors = search(term, developer=local.get("developer") or game.get("developer") or "")
            reachable = reachable and not errors
            hit = exact_match(term, results)
            if hit:
                meta = DETAILS[hit["source"]](hit["id"])
                break
    if twin and not meta and not twin["name_locked"]:
        local["name"] = local["name"] or twin["name"]
    if source and not meta:
        return "暂时没找到官网资料，已保留原来的内容", False
    current = library.get_game(game_id)
    if not current or (current["scrape_status"] == "cleared" and game["scrape_status"] != "cleared"):
        return "已停止更新，保留手动清除的结果", True
    applied = apply(game_id, meta, local)
    if applied.get("cancelled"):
        return "已停止更新，保留手动清除的结果", True
    if online and not meta:
        library.set_meta(game_id, {"scraped": 0, "scrape_status": "unmatched" if reachable else "error"})
    found = "、".join(k for k, v in (("名字", local["name"]), ("背景图", local["hero"]), ("Logo", local["logo"])) if v)
    if meta:
        return f"在 {SOURCE_LABELS[meta['source']]} 上找到「{meta['title']}」", reachable
    return (f"网上没找到，从游戏文件里取了{found}" if found else "网上和游戏文件里都没找到资料"), reachable


def apply_match(game_id, source, item_id):
    """手动选中一个搜索结果后，套用它的资料。"""
    if source not in DETAILS:
        raise ValueError("不认识的资料来源")
    game = library.get_game(game_id)
    return apply(game_id, DETAILS[source](str(item_id)), local_for(game)[0])


def clear(game_id):
    """去掉网上找来的资料，只保留游戏文件里取到的图。"""
    game = library.get_game(game_id)
    if game["is_wishlist"]:
        raise ValueError("愿望单需要保留官网来源，如不再关注可移除这条记录")
    apply(game_id, None, local_for(game)[0])
    library.set_meta(game_id, {"scrape_status": "cleared"})


def add_wishlist(source, item_id, baseline=""):
    if source not in DETAILS:
        raise ValueError("请选择一个搜索结果")
    meta = DETAILS[source](str(item_id))
    if not str(meta.get("title") or "").strip():
        raise LookupError("官网没有返回游戏名，请检查作品链接")
    game_id, added = wishlist.create(meta, baseline)
    if added:
        try:
            apply(game_id, meta, local_for(library.get_game(game_id))[0])
            wishlist.set_initial_version(game_id, meta.get("online_version"), baseline, meta.get("version_evidence", ""))
        except Exception:
            library.remove_game(game_id)
            raise
    return {"game_id": game_id, "added": added}


def check_wishlist_version(game_id):
    game = library.get_game(game_id)
    if not game or not game["wishlisted_at"] or game["meta_source"] not in DETAILS:
        raise ValueError("这条记录没有可检查的官网来源")
    try:
        meta = DETAILS[game["meta_source"]](game["meta_id"])
        version = meta.get("online_version", "")
        wishlist.record_version(game_id, version, meta.get("version_evidence", ""))
        return "已检查，官网版本：" + version if version else "已检查，官网没有明确标注版本号"
    except Exception:
        wishlist.record_version(game_id, "", status="error")
        raise


# ---------------------------------------------------------------- 后台队列
class Worker:
    """新游戏入库后排队，在后台一个个找资料，不卡界面。"""

    def __init__(self, log):
        self.log = log
        self.queue = queue.PriorityQueue()  # PC 版先处理，安卓版好借用 PC 版的结果
        self.lock = threading.Lock()
        self.pending = set()

    def enqueue(self, ids, icons_only=False, versions_only=False):
        with self.lock:
            for gid in ids:
                if gid not in self.pending:
                    self.pending.add(gid)
                    game = library.get_game(gid) or {}
                    self.queue.put((game.get("platform") != "PC", gid, "version" if versions_only else "icon" if icons_only else "auto"))

    def enqueue_versions(self):
        if load_config().get("auto_scrape", True):
            self.enqueue([g["id"] for g in library.list_games() if g["wishlisted_at"] and not g["missing"]], versions_only=True)

    def enqueue_icons(self):
        if load_config().get("auto_scrape", True):
            self.enqueue([g["id"] for g in library.list_games() if needs_site_icon(g)], icons_only=True)

    def enqueue_unmatched(self):
        if load_config().get("auto_scrape", True):
            self.enqueue(library.unmatched_games())

    def busy(self):
        with self.lock:
            return set(self.pending)

    def loop(self):
        while True:
            _, gid, task = self.queue.get()
            try:
                online = bool(load_config().get("auto_scrape", True))
                game = library.get_game(gid)
                if not game or game["scrape_status"] == "cleared":
                    continue
                if task == "version":
                    msg = check_wishlist_version(gid) if online else ""
                elif task == "icon":
                    msg = refresh_site_icon(gid) if online else ""
                else:
                    msg, _ = auto_scrape(gid, online=online)
                if game and msg:
                    self.log(f"找资料：{game['name']} → {msg}")
            except Exception as e:
                self.log(f"找资料出错（游戏 {gid}）：{e}")
            finally:
                with self.lock:
                    self.pending.discard(gid)
