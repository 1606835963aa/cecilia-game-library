#!/usr/bin/env python3
"""自动解压工具：监视下载文件夹，自动解开压缩包并整理好。

能处理：
- zip / rar / 7z，带密码（密码候选写在 config.json）
- 文件名带“删”后缀（如 xxx.rar删、xxx.apk删）
- 分卷：xxx.7z.001、xxx.part1.rar、xxx.z01、xxx.r00
- 自解压分卷：xxx.exe + xxx.001.sfxv + xxx.002.sfxv（自动拼起来解）
- 压缩包套压缩包（多层，密码会从文件名和说明文本里找线索）
- 中文密码的 zip（国产压缩软件按 GBK 存密码，7-Zip 会报密码错误，这里自己解密）
- 下载未完成的临时文件（.downloading 等）不会被碰
- 安卓版（apk）按设置处理：解压后放进单独的安卓文件夹 / 不解压原样挪过去 / 直接删除

用法：
    python auto_extract.py              监视模式（一直运行）
    python auto_extract.py --once       扫描一次就退出
    python auto_extract.py --file a.rar 只处理指定文件
    python auto_extract.py --dir D:\下载 临时换监视目录
"""
import argparse
import hashlib
import hmac
import json
import msvcrt
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import zipfile
import zlib
from contextlib import contextmanager
from datetime import datetime
from functools import lru_cache
from pathlib import Path, PurePosixPath
from uuid import uuid4

# 打包成 exe 时，设置、数据和游戏放在 exe 旁边；页面这类程序自带的文件在 PyInstaller 的 _internal 里
FROZEN = getattr(sys, "frozen", False)
ROOT = Path(sys.executable if FROZEN else __file__).resolve().parent
APP_DIR = Path(getattr(sys, "_MEIPASS", ROOT))
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "data" / "extract_state.json"
LOG_DIR = ROOT / "logs"
WORK_DIR = ROOT / "data"  # 解压时的中转文件夹都放在这里
SCAN_LOCK = ROOT / "data" / "extract.lock"
EVENT_PREFIX = "__EXTRACT_EVENT__"

DEFAULT_CONFIG = {
    "watch_dirs": ["games"],
    "download_dirs": ["downloads"],
    "games_dir": "games",
    "passwords": [],
    "seven_zip": "",
    "delete_archive": True,
    "poll_seconds": 10,
    "stable_seconds": 8,
    "scan_depth": 6,
    "nested_depth": 3,
    "auto_scrape": True,
    "android": "keep",
    "android_dir": "android",
    "theme": "steam",
}
# 安卓版的处理方式
ANDROID_MODES = {
    "keep": "解压，放进安卓文件夹",
    "archive": "不解压，压缩包原样挪进安卓文件夹",
    "delete": "直接删除",
}
ANDROID_EXT = (".apk", ".xapk", ".apks", ".obb")
# 判断压缩包是不是安卓版时，不算这些说明、广告类的小文件
DOC_EXT = (".txt", ".url", ".htm", ".html", ".md", ".ini", ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp")

SEVEN_ZIP_CANDIDATES = [
    str(ROOT / "7-Zip" / "7z.exe"),  # 绿色版自带的 7-Zip
    r"C:\Program Files\7-Zip\7z.exe",
    r"C:\Program Files (x86)\7-Zip\7z.exe",
]

TEMP_MARKERS = (".baiduyun.", ".downloading", ".tmp")
IMPORTING_DIR = "_正在导入"  # 游戏库手动导入时在 games 里用的中转文件夹，扫描和监视都跳过它
PUBLISHING_DIR = "_正在整理"
GAME_INFO = ".game-library.json"
SKIP_DIRS = {".git", "data", "logs", "tmp", IMPORTING_DIR, PUBLISHING_DIR, "$RECYCLE.BIN", "System Volume Information"}

ARCHIVE_MAGIC = (
    (b"PK\x03\x04", "zip"),
    (b"PK\x05\x06", "zip"),
    (b"PK\x07\x08", "zip"),
    (b"Rar!\x1a\x07", "rar"),
    (b"7z\xbc\xaf\x27\x1c", "7z"),
)
# 网盘里常把压缩包改成这些后缀躲审核（比如 .rar 改成 .pdf），只对这些后缀看文件头
SNIFF_SUFFIXES = {"", ".txt", ".pdf", ".jpg", ".png", ".mp4", ".iso", ".img", ".bin"}

PART_RAR = re.compile(r"^(?P<base>.+)\.part(?P<n>\d+)\.rar$", re.I)
SEVEN_SPLIT = re.compile(r"^(?P<base>.+)\.(?:zip|rar|7z)\.(?P<n>\d{3})$", re.I)
Z_SPLIT = re.compile(r"^(?P<base>.+)\.z\d{2}$", re.I)
R_SPLIT = re.compile(r"^(?P<base>.+)\.r\d{2}$", re.I)
PASSWORD_HINT = re.compile(
    r"(?:解压密码|安装密码|二级解压密码|2级解压密码|解压码|密码|password|pwd)\s*[:：=]?\s*([^\s，。；、,;:：()（）\[\]【】\"'“”]+)", re.I
)


# ---------------------------------------------------------------- 基础工具
def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        LOG_DIR.mkdir(exist_ok=True)
        with open(LOG_DIR / "extract.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            if isinstance(user, dict):
                cfg.update(user)
                if "games_dir" not in user:
                    cfg["games_dir"] = (user.get("watch_dirs") or ["games"])[0]
        except (OSError, json.JSONDecodeError) as e:
            log(f"配置文件读取失败，使用默认设置：{e}")
    return cfg


def save_config(updates):
    """只改动给出的设置项，其余（比如密码）原样保留。"""
    user = {}
    if CONFIG_PATH.exists():
        user = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    user.update(updates)
    CONFIG_PATH.write_text(json.dumps(user, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def resolve_dir(value):
    p = Path(value)
    return (p if p.is_absolute() else ROOT / p).resolve()


def android_root(cfg):
    return resolve_dir(cfg.get("android_dir") or "android")


def games_root(cfg):
    return resolve_dir(cfg.get("games_dir") or "games")


def download_roots(cfg):
    return list(dict.fromkeys(resolve_dir(p) for p in cfg.get("download_dirs") or ["downloads"]))


def validate_layout(cfg):
    """下载目录和成品目录不能互相包含，否则又会扫到解压结果。"""
    for source in download_roots(cfg):
        for target in (games_root(cfg), android_root(cfg)):
            if source == target or source in target.parents or target in source.parents:
                raise ValueError("下载文件夹和游戏、安卓文件夹要分开，不能是同一个文件夹或互相包含")


def game_label(folder):
    """编号目录另存原名，让游戏库和资料匹配仍然使用游戏名。"""
    try:
        info = Path(folder) / GAME_INFO
        if info.stat().st_size <= 16_384:
            name = json.loads(info.read_text(encoding="utf-8")).get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()[:300]
    except (OSError, ValueError, AttributeError):
        pass
    return Path(folder).name


def find_seven_zip(cfg):
    configured = str(cfg.get("seven_zip") or "").strip()
    if configured and Path(configured).exists():
        return configured
    for cand in SEVEN_ZIP_CANDIDATES:
        if Path(cand).exists():
            return cand
    return shutil.which("7z")


def run_7z(seven, args, timeout=None):
    proc = subprocess.run(
        [seven] + args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW,  # 游戏库没有黑窗口，不加的话每次调用 7-Zip 都会闪一个
    )
    raw = proc.stdout or b""
    for enc in ("utf-8", "gbk"):
        try:
            return proc.returncode, raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return proc.returncode, raw.decode("utf-8", "replace")


def canon(name):
    """去掉站里用来防和谐的“删”后缀，只用来识别，不改真实文件名。"""
    name = name.rstrip()
    while name.endswith("删"):
        name = name[:-1].rstrip(" .")
    return name


def is_temp_file(name):
    low = name.lower()
    return any(marker in low for marker in TEMP_MARKERS)


def human_size(num):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.1f}{unit}" if unit != "B" else f"{int(num)}B"
        num /= 1024


def clean_name(path):
    name = canon(path.name)
    suffixes = (".part1", ".001", ".z01", ".r00", ".zip", ".rar", ".7z", ".exe")
    changed = True
    while changed:
        changed = False
        low = name.lower()
        for suf in suffixes:
            if low.endswith(suf) and len(name) > len(suf):
                name = name[: -len(suf)]
                changed = True
                break
    name = re.sub(r'[<>:"/\\|?*]', "_", name).strip(" .")
    return name or "未命名"


# ---------------------------------------------------------------- 压缩包识别
def _sibling_parts(parent, predicate):
    try:
        names = os.listdir(parent)
    except OSError:
        return []
    return [parent / n for n in sorted(names) if predicate(parent / n)]


def archive_group(path):
    """判断 path 是不是压缩包（或分卷第一份），返回 dict 或 None。"""
    name = canon(path.name)
    low = name.lower()
    parent = path.parent

    m = PART_RAR.match(name)
    if m:
        if int(m.group("n")) != 1:
            return None
        base = m.group("base").lower()
        parts = _sibling_parts(parent, lambda p: bool(PART_RAR.match(canon(p.name)))
                              and PART_RAR.match(canon(p.name)).group("base").lower() == base)
        return {"trigger": path, "parts": parts, "key": (parent / base).as_posix().lower()}

    m = SEVEN_SPLIT.match(name)
    if m:
        if m.group("n") != "001":
            return None
        base = name[:-4]
        prefix = base.lower() + "."
        parts = _sibling_parts(parent, lambda p: canon(p.name).lower().startswith(prefix)
                              and canon(p.name)[len(prefix):].isdigit())
        return {"trigger": path, "parts": parts or [path], "key": (parent / base).as_posix().lower()}

    if Z_SPLIT.match(name) or R_SPLIT.match(name):
        return None

    if low.endswith((".zip", ".rar", ".7z")):
        parts = [path]
        if low.endswith(".zip"):
            parts += _sibling_parts(parent, lambda p: bool(re.fullmatch(re.escape(name[:-4]) + r"\.z\d{2}", canon(p.name), re.I)))
        if low.endswith(".rar"):
            parts += _sibling_parts(parent, lambda p: bool(re.fullmatch(re.escape(name[:-4]) + r"\.r\d{2}", canon(p.name), re.I)))
        return {"trigger": path, "parts": sorted(set(parts)), "key": str(parent / name[:-4]).lower()}

    if low.endswith(".exe"):
        volumes = _sibling_parts(parent, lambda p: bool(re.fullmatch(re.escape(name[:-4]) + r"\.\d+\.sfxv", canon(p.name), re.I)))
        if volumes:
            volumes.sort(key=lambda p: int(re.search(r"\.(\d+)\.sfxv$", canon(p.name), re.I).group(1)))
            return {"trigger": path, "parts": [path, *volumes], "key": str(path).lower(), "sfx": True}
        if executable_archive(path):
            return {"trigger": path, "parts": [path], "key": str(path).lower()}

    if Path(name).suffix.lower() in SNIFF_SUFFIXES or re.search(r"\.(?:zip|rar|7z)\d+$", low):
        kind = sniff_archive(path)
        if kind:
            return {
                "trigger": path,
                "parts": [path],
                "key": str(path).lower(),
                "name_override": path.stem or path.name,
            }
    return None


def sniff_archive(path):
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return None
    for magic, kind in ARCHIVE_MAGIC:
        if head.startswith(magic):
            return kind
    return None


def executable_archive(path):
    if sniff_archive(path):
        return True
    try:
        stat = path.stat()
        return _executable_archive(str(path), stat.st_size, stat.st_mtime_ns, find_seven_zip(load_config()))
    except OSError:
        return False


@lru_cache(maxsize=512)
def _executable_archive(path, size, modified, seven):
    """先看 PE 末尾附加的数据，再让 7-Zip 确认；普通主程序的资源段不能当压缩包。"""
    if not seven:
        return False
    try:
        with open(path, "rb") as file:
            head = file.read(64)
            if len(head) != 64 or not head.startswith(b"MZ"):
                return False
            offset = struct.unpack_from("<I", head, 60)[0]
            if offset > size - 24:
                return False
            file.seek(offset)
            header = file.read(24)
            if not header.startswith(b"PE\0\0"):
                return False
            count = struct.unpack_from("<H", header, 6)[0]
            optional_size = struct.unpack_from("<H", header, 20)[0]
            if not 0 < count <= 96:
                return False
            file.seek(offset + 24 + optional_size)
            sections = file.read(count * 40)
            if len(sections) != count * 40:
                return False
            end = max(sum(struct.unpack_from("<II", sections, i * 40 + 16)) for i in range(count))
            if end <= 0 or end >= size:
                return False
            file.seek(end)
            overlay = file.read(1024 * 1024)
            if not any(magic in overlay for magic, _ in ARCHIVE_MAGIC):
                return False
        _, listing = run_7z(seven, ["l", "-slt", "-sccUTF-8", "-p-", path], timeout=20)
        kind = re.search(r"^Type = (.+)$", listing, re.M)
        if kind:
            return kind.group(1).strip().lower() in ("7z", "zip", "rar", "rar5")
        # 文件头也加密时，7-Zip 在填对密码前只报告加密包，不输出格式字段。
        return "encrypted archive" in listing.lower()
    except (OSError, ValueError, struct.error, subprocess.TimeoutExpired):
        return False


@contextmanager
def readable_group(group):
    """分卷自解压包只拼接成数据文件，整个流程都由 7-Zip 读取。"""
    if not group.get("sfx"):
        yield group
        return
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    combined = WORK_DIR / f"_sfx_{uuid4().hex}.exe"
    try:
        with combined.open("wb") as output:
            for part in group["parts"]:
                with part.open("rb") as source:
                    shutil.copyfileobj(source, output, 8 * 1024 * 1024)
        yield dict(group, trigger=combined, parts=[combined], sfx=False)
    finally:
        combined.unlink(missing_ok=True)


def group_signature(group):
    sig = []
    for p in group["parts"]:
        try:
            sig.append((p.as_posix(), p.stat().st_size))
        except OSError:
            sig.append((p.as_posix(), -1))
    return tuple(sig)


def archive_complete(group):
    """7z 头记录了末尾位置，缺分卷时不能把读取失败误报成密码错误。"""
    trigger = group["trigger"]
    if sniff_archive(trigger) != "7z" and not canon(trigger.name).lower().endswith(".exe"):
        return True
    try:
        with trigger.open("rb") as source:
            head = source.read(1024 * 1024)
        offset = head.find(b"7z\xbc\xaf\x27\x1c")
        header = head[offset:offset + 32] if offset >= 0 else b""
        if len(header) != 32 or zlib.crc32(header[12:32]) & 0xffffffff != struct.unpack_from("<I", header, 8)[0]:
            return True
        next_offset, next_size = struct.unpack_from("<QQ", header, 12)
        required = offset + 32 + next_offset + next_size
        return sum(p.stat().st_size for p in group["parts"]) >= required
    except (OSError, struct.error):
        return False


def group_ready(group, stable_seconds, seen):
    key = group["key"]
    try:
        sig = group_signature(group)
    except OSError:
        return False
    record = seen.get(key)
    if record is None or record[0] != sig:
        seen[key] = (sig, time.time())
        return False
    return time.time() - record[1] >= stable_seconds


# ---------------------------------------------------------------- 密码
def parse_listing(text):
    entries = []
    current = {}
    for line in text.splitlines():
        if line.startswith("Path = "):
            if current:
                entries.append(current)
            current = {"path": line[7:]}
        elif line.startswith("Size = "):
            try:
                current["size"] = int(line[7:].strip())
            except ValueError:
                current["size"] = 0
        elif line.startswith("Attributes = "):
            current["attr"] = line[13:].strip()
    if current:
        entries.append(current)
    files = []
    for entry in entries:
        if not entry.get("path") or "attr" not in entry:
            continue
        if entry["attr"].upper().startswith("D"):
            continue
        files.append(entry)
    files.sort(key=lambda e: e.get("size") or 0)
    return files


def try_list(seven, archive, password):
    args = ["l", "-slt", "-sccUTF-8"]
    if password:
        args.append(f"-p{password}")
    rc, out = run_7z(seven, args + [str(archive)])
    return rc == 0, out


def verify_password_stream(seven, archive, password, probe_file):
    """把文件内容流式读一点点就够判断密码对不对，避免为了验密码把几个 G 解出来。"""
    args = ["x", "-so", "-bso0", "-bsp0", "-sccUTF-8"]
    if password:
        args.append(f"-p{password}")
    args += [str(archive), probe_file]
    try:
        proc = subprocess.Popen(
            [seven] + args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except OSError:
        return False
    try:
        return bool(proc.stdout.read(64 * 1024))
    except OSError:
        return False
    finally:
        for closer in (proc.kill, lambda: proc.wait(timeout=10)):
            try:
                closer()
            except (OSError, subprocess.TimeoutExpired):
                pass


def verify_password(seven, archive, password, probe_file, tmpdir):
    args = ["x", "-y", "-bso0", "-bsp0", "-sccUTF-8", f"-o{tmpdir}"]
    if password:
        args.append(f"-p{password}")
    args += [str(archive), probe_file]
    try:
        rc, _ = run_7z(seven, args)
    except subprocess.TimeoutExpired:
        return False
    if rc not in (0, 1):
        return False
    return any(p.is_file() for p in Path(tmpdir).rglob("*"))


def choose_password(seven, archive, candidates):
    ok, out = try_list(seven, archive, None)
    if ok:
        files = parse_listing(out)
        if not files:
            return None, out, "空压缩包"
        probe_entry = next((e for e in files if (e.get("size") or 0) > 0), files[0])
        probe = probe_entry["path"]
        big = (probe_entry.get("size") or 0) > 16 * 1024 * 1024
        tmpdir = WORK_DIR / "_pwcheck"

        def check(password):
            if big:
                return verify_password_stream(seven, archive, password, probe)
            try:
                shutil.rmtree(tmpdir, ignore_errors=True)
                tmpdir.mkdir(parents=True, exist_ok=True)
                return verify_password(seven, archive, password, probe, str(tmpdir))
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        if check(None):
            return None, out, "无密码"
        for pwd in candidates:
            if check(pwd):
                return pwd, out, "密码正确"
        return None, out, "密码不正确"

    for pwd in candidates:
        ok2, out2 = try_list(seven, archive, pwd)
        if ok2:
            return pwd, out2, "密码正确"
    return None, out, "需要密码但候选密码都不对"


# ---------------------------------------------------------------- 中文密码的 zip
# 国产压缩软件给 zip 加密时，中文密码常按 GBK 字节存；7-Zip 一律按 UTF-8 算，就会报密码错误。
# 这里自己按 GBK/UTF-8 两种编码去试，并用 zip 自带的 AES 或 ZipCrypto 解开。
ZIP_AES_STRENGTH = {1: 16, 2: 24, 3: 32}
PWD_ENCODINGS = (("utf-8", "UTF-8"), ("gbk", "GBK"), ("utf-16-le", "UTF-16"))


def looks_like_zip(path):
    if canon(path.name).lower().endswith(".zip"):
        return True
    try:
        with open(path, "rb") as f:
            head = f.read(4)
    except OSError:
        return False
    return head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06")


def _read_local_aes_header(fp, info):
    fp.seek(info.header_offset)
    header = fp.read(30)
    if len(header) < 30:
        raise ValueError("zip 头部不完整")
    fields = struct.unpack("<IHHHHHIIIHH", header)
    csize, nlen, elen = fields[7], fields[9], fields[10]
    fp.read(nlen)
    extra = fp.read(elen)
    aes = None
    while len(extra) >= 4:
        hid, hsize = struct.unpack("<HH", extra[:4])
        body = extra[4:4 + hsize]
        if hid == 0x9901 and len(body) >= 7:
            aes = {"strength": body[4], "actual": struct.unpack("<H", body[5:7])[0]}
        extra = extra[4 + hsize:]
    if not aes or aes["strength"] not in ZIP_AES_STRENGTH:
        raise ValueError("不是 AES 加密的 zip 条目")
    return aes, csize, fp.tell()


def _derive_zip_keys(password, salt, key_len):
    dk = hashlib.pbkdf2_hmac("sha1", password, salt, 1000, 2 * key_len + 2)
    return dk[:key_len], dk[key_len:2 * key_len], dk[-2:]


def _aes_password_ok(zip_path, info, pwd_bytes):
    try:
        with open(zip_path, "rb") as fp:
            aes, _csize, data_off = _read_local_aes_header(fp, info)
            salt_len = {1: 8, 2: 12, 3: 16}[aes["strength"]]
            key_len = ZIP_AES_STRENGTH[aes["strength"]]
            fp.seek(data_off)
            salt = fp.read(salt_len)
            verifier = fp.read(2)
        _enc, _auth, check = _derive_zip_keys(pwd_bytes, salt, key_len)
        return check == verifier
    except (OSError, ValueError, struct.error):
        return False


def _zipcrypto_password_ok(zf, info, pwd_bytes):
    try:
        with zf.open(info, pwd=pwd_bytes) as f:
            f.read(16)
        return True
    except (RuntimeError, zipfile.BadZipFile, OSError, ValueError, NotImplementedError):
        return False


def zip_find_password(zip_path, candidates):
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError):
        return None
    with zf:
        entries = [i for i in zf.infolist() if not i.is_dir() and (i.flag_bits & 0x1)]
        if not entries:
            return None
        aes_entry = next((i for i in entries if i.compress_type == 99), None)
        crypto_entry = next((i for i in entries if i.compress_type != 99), None)
        for pwd in candidates:
            for enc, label in PWD_ENCODINGS:
                try:
                    raw = pwd.encode(enc)
                except (LookupError, UnicodeEncodeError):
                    continue
                if aes_entry is not None and _aes_password_ok(zip_path, aes_entry, raw):
                    return raw, label
                if crypto_entry is not None and _zipcrypto_password_ok(zf, crypto_entry, raw):
                    return raw, label
    return None


def _xor_bytes(data, keystream):
    return (int.from_bytes(data, "big") ^ int.from_bytes(keystream, "big")).to_bytes(len(data), "big")


def _decrypt_aes_entry(zip_path, info, pwd_bytes, target):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    with open(zip_path, "rb") as fp:
        aes, csize, data_off = _read_local_aes_header(fp, info)
        salt_len = {1: 8, 2: 12, 3: 16}[aes["strength"]]
        key_len = ZIP_AES_STRENGTH[aes["strength"]]
        fp.seek(data_off)
        salt = fp.read(salt_len)
        verifier = fp.read(2)
        enc_key, auth_key, check = _derive_zip_keys(pwd_bytes, salt, key_len)
        if check != verifier:
            raise ValueError("密码不对")
        cipher_len = csize - salt_len - 2 - 10
        if cipher_len < 0:
            raise ValueError("zip 数据长度异常")
        cipher = Cipher(algorithms.AES(enc_key), modes.ECB()).encryptor()
        digest = hmac.new(auth_key, digestmod=hashlib.sha1)
        inflater = zlib.decompressobj(-15) if aes["actual"] == 8 else None
        counter = 1
        remaining = cipher_len
        with open(target, "wb") as out:
            while remaining > 0:
                want = min(16 * 1024 * 1024, remaining)
                chunk = fp.read(want)
                if not chunk:
                    raise ValueError("zip 数据被截断")
                blocks = (len(chunk) + 15) // 16
                counters = b"".join((counter + i).to_bytes(16, "little") for i in range(blocks))
                keystream = cipher.update(counters)
                counter += blocks
                remaining -= len(chunk)
                digest.update(chunk)
                plain = _xor_bytes(chunk, keystream[:len(chunk)])
                if inflater is not None:
                    plain = inflater.decompress(plain)
                if plain:
                    out.write(plain)
            if inflater is not None:
                tail = inflater.flush()
                if tail:
                    out.write(tail)
            stored_mac = fp.read(10)
    if stored_mac and digest.digest()[:10] != stored_mac:
        log(f"  警告：{info.filename} 校验值不一致，文件可能不完整")


def extract_zip_with_python(zip_path, pwd_bytes, staging):
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            parts = [p for p in PurePosixPath(info.filename.replace("\\", "/")).parts
                     if p not in ("", "/", "..")]
            if not parts:
                continue
            target = staging.joinpath(*parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            encrypted = bool(info.flag_bits & 0x1)
            if info.compress_type == 99 and encrypted:
                _decrypt_aes_entry(zip_path, info, pwd_bytes, target)
            elif encrypted:
                with zf.open(info, pwd=pwd_bytes) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, 8 * 1024 * 1024)
            else:
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, 8 * 1024 * 1024)


def collect_hints(directory, extra=None, recursive=True):
    """从文件名、文件夹名和小文本里找密码线索（如“2级解压密码GAME158”）。

    recursive=False 时只看这一层，不往子文件夹里找（压缩包在桌面、下载这类大文件夹里时用）。
    """
    hints = []
    seen = set()

    def add(value):
        value = str(value or "").strip().strip("：:=。，,;；")
        if not value or len(value) > 256:
            return
        if value in seen:
            return
        seen.add(value)
        hints.append(value)

    for value in extra or []:
        add(value)

    try:
        walker = os.walk(directory)
        for dirpath, _dirnames, filenames in walker:
            base = Path(dirpath)
            for name in [base.name] + filenames:
                for m in PASSWORD_HINT.finditer(name):
                    add(m.group(1))
            for name in filenames:
                low = name.lower()
                if not low.endswith((".txt", ".url", ".ini", ".md")):
                    continue
                fp = base / name
                try:
                    if fp.stat().st_size > 200_000:
                        continue
                    data = fp.read_bytes()
                except OSError:
                    continue
                for enc in ("utf-8", "gbk"):
                    try:
                        text = data.decode(enc)
                        break
                    except UnicodeDecodeError:
                        text = None
                if text is None:
                    continue
                for m in PASSWORD_HINT.finditer(text):
                    add(m.group(1))
            if not recursive:
                break
    except OSError:
        pass
    return hints


# ---------------------------------------------------------------- 解压
def extract_archive(seven, group, password, staging):
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    args = ["x", "-y", "-bso0", "-bsp0", "-sccUTF-8", f"-o{staging}"]
    if password:
        args.append(f"-p{password}")
    args.append(str(group["trigger"]))
    rc, out = run_7z(seven, args)
    return rc, out


def extract_to_staging(seven, group, candidates, staging):
    trigger = group["trigger"]
    pwd, _out, note = choose_password(seven, trigger, candidates)
    if pwd is not None or note in ("无密码", "空压缩包"):
        if note == "密码正确":
            log("  密码已匹配")
        rc, output = extract_archive(seven, group, pwd, staging)
        if rc in (0, 1):
            return True, note
        tail = "\n".join(output.strip().splitlines()[-3:])
        return False, f"解压失败（退出码 {rc}）：{tail}"
    if looks_like_zip(trigger):
        found = zip_find_password(trigger, candidates)
        if found:
            pwd_bytes, label = found
            log(f"  密码已匹配（zip 密码按 {label} 编码）")
            try:
                extract_zip_with_python(trigger, pwd_bytes, staging)
                return True, "密码正确"
            except Exception as e:  # noqa: BLE001 解密失败就当没解开
                return False, f"zip 解密失败：{e}"
    return False, note


def unique(base):
    if not base.exists():
        return base
    i = 2
    while True:
        cand = base.with_name(f"{base.name} ({i})")
        if not cand.exists():
            return cand
        i += 1


def organize(staging, dest, name, at_watch_root):
    items = list(staging.iterdir())
    if not items:
        shutil.rmtree(staging, ignore_errors=True)
        return dest
    if len(items) == 1:
        item = items[0]
        target = unique(dest / item.name)
        shutil.move(str(item), str(target))
        shutil.rmtree(staging, ignore_errors=True)
        return target if target.is_dir() else dest
    if at_watch_root:
        target = unique(dest / name)
        target.mkdir(parents=True, exist_ok=True)
        for item in items:
            shutil.move(str(item), str(unique(target / item.name)))
        shutil.rmtree(staging, ignore_errors=True)
        return target
    for item in items:
        shutil.move(str(item), str(unique(dest / item.name)))
    shutil.rmtree(staging, ignore_errors=True)
    return dest


def publish_game(staging, dest, title=None):
    """去掉单层包装目录，再完整放进编号目录；游戏内部的资源路径不改。"""
    payload = staging
    title = title or game_label(staging)
    while True:
        items = list(payload.iterdir())
        directories = [p for p in items if p.is_dir()]
        documents = [p for p in items if p.is_file()]
        if (len(directories) != 1 or any(p.suffix.lower() not in (".txt", ".md", ".url", ".html", ".htm", ".pdf") for p in documents)):
            break
        child = directories[0]
        if child.name.lower() not in ("pc", "windows", "win", "win64", "win32", "game", "games", "dist"):
            title = child.name
        for document in documents:
            shutil.move(str(document), str(unique(child / document.name)))
        payload = child
    name = PASSWORD_HINT.sub("", str(title)).strip(" ._-") or "Game"
    (payload / GAME_INFO).write_text(json.dumps({"name": name[:300]}, ensure_ascii=False) + "\n", encoding="utf-8")
    dest.mkdir(parents=True, exist_ok=True)
    incoming = dest / PUBLISHING_DIR / uuid4().hex
    incoming.parent.mkdir(parents=True, exist_ok=True)
    try:
        # 先搬到同一磁盘的隐藏中转目录，最终改名时扫描看不到半份游戏。
        shutil.move(str(payload), str(incoming))
        numbers = [int(p.name[5:]) for p in dest.iterdir() if re.fullmatch(r"Game_\d+", p.name)]
        number = max(numbers, default=0) + 1
        while True:
            target = dest / f"Game_{number:06d}"
            try:
                if target.exists():
                    number += 1
                    continue
                incoming.rename(target)
                return target
            except OSError:
                if not target.exists():
                    raise
                number += 1
    finally:
        if incoming.exists():
            shutil.rmtree(incoming)
        try:
            incoming.parent.rmdir()
        except OSError:
            pass


def delete_group_files(group):
    removed = 0
    for part in group["parts"]:
        try:
            part.unlink()
            removed += 1
        except OSError:
            pass
    return removed


# ---------------------------------------------------------------- 嵌套压缩包
def is_application_dir(directory, filenames, dirnames):
    """已能运行的程序目录不再拆内部资源包、模组和存档。"""
    names = {name.lower() for name in filenames}
    exes = [Path(directory) / name for name in filenames if name.lower().endswith(".exe")]
    dirs = {name.lower() for name in dirnames}
    # 下载包也可能附带推广程序和 PC 子目录，只有运行资源的特征才算游戏目录。
    resources = bool(dirs & {"game", "data", "www", "renpy", "engine", "content", "resources", "assets", "gamedata", "res", "lib"})
    resources = resources or any(path.stem.lower() + "_data" in dirs for path in exes)
    resources = resources or any(name.endswith((".dll", ".pck", ".rgss3a", ".rgss2a", ".rgssad")) for name in names)
    return bool(exes and resources and any(not executable_archive(path) for path in exes))


def iter_archives_in(directory):
    for dirpath, dirnames, filenames in os.walk(directory):
        if is_application_dir(dirpath, filenames, dirnames):
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith("_")]
        for fname in filenames:
            if is_temp_file(fname):
                continue
            path = Path(dirpath) / fname
            if clean_name(path).lower() in ("存档", "save", "saves", "savegame", "savegames"):
                continue
            group = archive_group(path)
            if group:
                yield group


def process_nested(seven, directory, passwords, cfg, depth, watch_roots, progress=None, handled=None, android_archives=None):
    handled = handled if handled is not None else set()
    android_archives = android_archives if android_archives is not None else []
    groups = [g for g in iter_archives_in(directory) if g["key"] not in handled]
    if depth <= 0:
        return [f"{g['trigger'].name}：已达到套娃层数限制" for g in groups]
    failures = []
    for group in groups:
        handled.add(group["key"])
        trigger = group["trigger"]
        if not trigger.exists():
            continue
        parent = trigger.parent
        log(f"发现嵌套压缩包：{trigger.name}")
        if not archive_complete(group):
            message = "分卷还没下齐，或压缩包不完整"
            failures.append(f"{trigger.name}：{message}")
            if progress:
                progress({"phase": "result", "file": trigger.name, "path": str(trigger), "ok": False, "message": message})
            continue
        if progress:
            progress({"phase": "extracting", "file": trigger.name, "message": "正在解压里面的压缩包"})
        hints = collect_hints(directory, passwords)
        staging = WORK_DIR / f"_nested_{uuid4().hex}"
        with readable_group(group) as readable:
            android_only = archive_is_android(seven, readable, hints)
            if android_only and cfg.get("android") in ("archive", "delete"):
                android_archives.append(group)
                continue
            ok, note = extract_to_staging(seven, readable, hints, staging)
        if not ok:
            shutil.rmtree(staging, ignore_errors=True)
            log(f"  跳过嵌套包：{note} —— {trigger.name}")
            failures.append(f"{trigger.name}：{note}")
            if progress:
                progress({"phase": "result", "file": trigger.name, "path": str(trigger), "ok": False, "message": note})
            continue
        target = organize(staging, parent, clean_name(trigger), parent == directory)
        log(f"  嵌套包已解压：{trigger.name}")
        # 内层包是外层包里的副本，成品中只留解压后的内容。
        delete_group_files(group)
        if progress:
            progress({"phase": "result", "file": trigger.name, "path": str(trigger), "ok": True, "message": "已解压"})
        failures.extend(process_nested(seven, target if target.is_dir() else parent, hints, cfg, depth - 1,
                                       watch_roots, progress, handled, android_archives))
    return failures


def rename_stripped(directory):
    """把解压出来的 xxx.apk删 这类文件改名成 xxx.apk。"""
    for dirpath, dirnames, filenames in os.walk(directory, topdown=False):
        for name in filenames + dirnames:
            if not name.endswith("删"):
                continue
            src = Path(dirpath) / name
            dst = src.with_name(canon(name))
            if src.exists() and not dst.exists():
                try:
                    src.rename(dst)
                except OSError:
                    pass


# ---------------------------------------------------------------- 安卓版
def is_android_file(name):
    return canon(name).lower().endswith(ANDROID_EXT)


def archive_is_android(seven, group, passwords):
    """压缩包里除了说明、广告类小文件，装的全是安卓安装包。"""
    for pwd in [None, *passwords]:
        ok, out = try_list(seven, group["trigger"], pwd)
        if not ok:
            continue
        names = [canon(PurePosixPath(e["path"].replace("\\", "/")).name).lower() for e in parse_listing(out)]
        payload = [n for n in names if not n.endswith(DOC_EXT)]
        return bool(payload) and all(n.endswith(ANDROID_EXT) for n in payload)
    return False


def android_path(path, watch_roots, cfg):
    """在安卓文件夹里对应的位置，保留原来在监视目录里的相对路径：games/xx/AZ/a.apk → android/xx/AZ/a.apk。"""
    for root in watch_roots:
        try:
            return android_root(cfg) / path.relative_to(root)
        except ValueError:
            continue
    return android_root(cfg) / path.name


def move_to_android(path, watch_roots, cfg, strip=True):
    target = android_path(path, watch_roots, cfg)
    if strip:
        target = target.with_name(canon(target.name))
    target = unique(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(target))
    return target


def handle_android_file(path, watch_roots, cfg):
    """单个安卓安装包（apk）按设置挪走或删掉。"""
    if cfg.get("android", "keep") == "delete":
        path.unlink()
        log(f"  安卓版按设置删掉了：{path.name}")
    else:
        log(f"  安卓版挪进了安卓文件夹：{move_to_android(path, watch_roots, cfg)}")
    prune_empty_parents(path.parent, watch_roots)


def divert_android_archive(group, cfg, watch_roots):
    """只装着安卓版的压缩包：设置成不解压或删除时在这里处理掉，返回 True 表示不用再解压了。"""
    mode = cfg.get("android", "keep")
    if mode not in ("archive", "delete"):
        return False
    if mode == "delete":
        removed = delete_group_files(group)
        log(f"  这是安卓版，按设置删掉了（{removed} 个文件）")
    else:
        for part in group["parts"]:
            moved = move_to_android(part, watch_roots, cfg, strip=False)
        log(f"  这是安卓版，按设置原样挪进了安卓文件夹：{moved.parent}")
    prune_empty_parents(group["trigger"].parent, watch_roots)
    return True


def sweep_android(directory, watch_roots, cfg):
    """解压出来的安卓安装包按设置挪走或删掉。"""
    if directory == android_root(cfg) or android_root(cfg) in directory.parents:
        return
    for dirpath, _dirnames, filenames in os.walk(directory):
        for name in filenames:
            if is_android_file(name):
                try:
                    handle_android_file(Path(dirpath) / name, watch_roots, cfg)
                except OSError as e:
                    log(f"  安卓版处理失败：{name}：{e}")


def output_dir(parent, android_only, watch_roots, cfg):
    """PC 版统一进入游戏目录，安卓版沿用安卓目录。"""
    if not android_only:
        return games_root(cfg)
    target = android_path(parent, watch_roots, cfg)
    target.mkdir(parents=True, exist_ok=True)
    return target


def prune_empty_parents(folder, watch_roots):
    """往上删掉空文件夹，到监视目录为止。"""
    folder = Path(folder)
    while folder not in watch_roots and any(r in folder.parents for r in watch_roots):
        try:
            if any(folder.iterdir()):
                return
            folder.rmdir()
        except OSError:
            return
        folder = folder.parent


def remove_empty_dirs(directory):
    for dirpath, _dirnames, _filenames in os.walk(directory, topdown=False):
        p = Path(dirpath)
        try:
            if p != directory and not any(p.iterdir()):
                p.rmdir()
        except OSError:
            pass


# ---------------------------------------------------------------- 主流程
def process_group(seven, group, cfg, passwords, watch_roots, into=None, progress=None):
    """解压一组压缩包并整理好，返回 (成功没有, 说明)。

    into：解压进这个文件夹（游戏库手动导入用）。这时只装着安卓版的压缩包解到安卓文件夹，
    原压缩包不动，删不删由导入那边决定。
    """
    trigger = group["trigger"]
    parent = trigger.parent
    log(f"发现压缩包：{trigger.name}（{human_size(trigger.stat().st_size)}）")
    if not archive_complete(group):
        return False, "分卷还没下齐，或压缩包仍在下载"
    if into is None:
        validate_layout(cfg)
    hints = collect_hints(parent, passwords, recursive=False)
    for ancestor in parent.parents:
        if not any(ancestor == root or root in ancestor.parents for root in watch_roots):
            break
        hints = collect_hints(ancestor, hints, recursive=False)
    staging = WORK_DIR / f"_staging_{uuid4().hex}"
    try:
        with readable_group(group) as readable:
            android_only = archive_is_android(seven, readable, hints)
            if android_only and into is None and divert_android_archive(group, cfg, watch_roots):
                return True, "安卓版"
            ok, note = extract_to_staging(seven, readable, hints, staging)
        if not ok:
            log(f"  跳过：{note} —— {trigger.name}")
            return False, note
        all_pwds = collect_hints(staging, hints)
        android_archives = []
        failures = process_nested(seven, staging, all_pwds, cfg, int(cfg.get("nested_depth", 3)),
                                  [staging], progress, android_archives=android_archives)
        if failures:
            # 全部解开才交给游戏库，内层失败时保留下载区的原包供补密码后重试。
            return False, "里面的压缩包没解开，原包已保留：" + "；".join(failures[:3])
        rename_stripped(staging)
        for inner in android_archives:
            if cfg.get("android") == "delete":
                delete_group_files(inner)
            else:
                for part in inner["parts"]:
                    move_to_android(part, [staging], cfg, strip=False)
        sweep_android(staging, [staging], cfg)
        remove_empty_dirs(staging)
        title = group.get("name_override") or clean_name(trigger)
        if android_only:
            out_dir = output_dir(parent, True, watch_roots, cfg)
            dest = organize(staging, out_dir, title, True)
        elif any(staging.iterdir()):
            dest = publish_game(staging, into if into is not None else games_root(cfg), title)
        else:
            dest = android_root(cfg)
        log(f"  已解压到：{dest}")
        if into is None and cfg.get("delete_archive", True):
            removed = delete_group_files(group)
            log(f"  已删除压缩包 {removed} 个")
        return True, note
    finally:
        shutil.rmtree(staging, ignore_errors=True)


@contextmanager
def scan_lock(wait=False, on_wait=None):
    """刷新和独立监视共用一把文件锁，避免同时整理、删除同一个压缩包。"""
    SCAN_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(SCAN_LOCK, "a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        announced = False
        while True:
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if not wait:
                    yield False
                    return
                if not announced and on_wait:
                    on_wait()
                    announced = True
                time.sleep(0.25)
        try:
            yield True
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def scan_once(cfg, seven, state, seen, cooldown, force=False):
    with scan_lock() as acquired:
        if not acquired:
            log("已有解压任务在运行，本轮稍后再试")
            return
        state.update(load_state())
        return _scan_once(cfg, seven, state, seen, cooldown, force)


def _scan_once(cfg, seven, state, seen, cooldown, force=False, progress=None):
    report = {"processed": 0, "succeeded": 0, "failed": 0, "waiting": 0}

    def result(path, ok, message):
        report["processed"] += 1
        report["succeeded" if ok else "failed"] += 1
        if progress:
            progress({"phase": "result", "file": path.name, "ok": ok, "message": message})

    def nested_progress(event):
        if event["phase"] == "result":
            if event["ok"]:
                state[event["path"]] = True
            result(Path(event["file"]), event["ok"], event["message"])
        elif progress:
            progress(event)

    validate_layout(cfg)
    watch_roots = download_roots(cfg)
    found = 0
    stable = float(cfg.get("stable_seconds", 8))
    android_dir = android_root(cfg)
    for root in watch_roots:
        if not root.exists():
            log(f"监视目录不存在：{root}")
            continue
        max_depth = int(cfg.get("scan_depth", 6))
        for dirpath, dirnames, filenames in os.walk(root):
            rel = len(Path(dirpath).relative_to(root).parts)
            if rel >= max_depth:
                dirnames[:] = []
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith("_")
                           and (Path(dirpath) / d).resolve() != android_dir]
            for fname in filenames:
                if is_temp_file(fname):
                    continue
                path = Path(dirpath) / fname
                if is_android_file(fname):
                    # 直接下载下来的 apk，下载完就按设置挪走或删掉
                    if force or group_ready({"key": str(path).lower(), "parts": [path]}, stable, seen):
                        try:
                            handle_android_file(path, watch_roots, cfg)
                            result(path, True, "安卓版已按设置处理")
                        except OSError as e:
                            log(f"  安卓版处理失败：{fname}：{e}")
                            result(path, False, str(e))
                    else:
                        report["waiting"] += 1
                    continue
                group = archive_group(path)
                if not group:
                    continue
                if str(path) in state:
                    continue
                if not archive_complete(group):
                    report["waiting"] += 1
                    continue
                if time.time() - cooldown.get(group["key"], 0) < 120:
                    continue
                if not force and not group_ready(group, stable, seen):
                    report["waiting"] += 1
                    continue
                found += 1
                if progress:
                    progress({"phase": "extracting", "file": path.name, "message": "正在解压"})
                try:
                    ok, note = process_group(seven, group, cfg, cfg.get("passwords") or [], watch_roots,
                                             progress=nested_progress)
                except Exception as e:  # noqa: BLE001 单个文件出错不影响其它
                    log(f"  处理异常：{path.name}：{e}")
                    ok, note = False, str(e)
                result(path, ok, "已解压" if ok else note)
                if ok:
                    state[str(path)] = True
                else:
                    cooldown[group["key"]] = time.time()
    if found == 0:
        log("扫描完成，没有发现可解压的压缩包")
    else:
        log(f"扫描完成，处理了 {found} 个压缩包")
    save_state({k: v for k, v in state.items() if Path(k).exists()})
    return report


def scan_for_library(seven):
    """游戏库刷新：先确认文件大小稳定，再解压并向页面报告结果。"""
    def progress(event):
        print(EVENT_PREFIX + json.dumps(event, ensure_ascii=False), flush=True)

    def waiting():
        progress({"phase": "waiting", "message": "正在等自动解压完成"})

    with scan_lock(wait=True, on_wait=waiting):
        cfg = load_config()
        progress({"phase": "checking", "message": "正在检查下载文件夹"})
        state, seen, cooldown = load_state(), {}, {}
        report = _scan_once(cfg, seven, state, seen, cooldown, progress=progress)
        if report["waiting"]:
            progress({"phase": "waiting", "message": "正在确认压缩包已下载完成"})
            time.sleep(max(float(cfg.get("stable_seconds", 8)), 0))
            report = _scan_once(load_config(), seven, state, seen, cooldown, progress=progress)
        progress({"phase": "finished", **report})


def load_state():
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state):
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def watch_loop(cfg, seven, watch_dirs=None):
    state = load_state()
    seen = {}
    cooldown = {}
    log("开始监视，按 Ctrl+C 退出")
    validate_layout(cfg)
    games_root(cfg).mkdir(parents=True, exist_ok=True)
    for p in download_roots(cfg):
        p.mkdir(parents=True, exist_ok=True)
        log(f"监视目录：{p}")
    log(f"游戏目录：{games_root(cfg)}")
    log(f"安卓版：{ANDROID_MODES.get(cfg.get('android'), cfg.get('android'))}（安卓文件夹：{android_root(cfg)}）")
    while True:
        scan_once(cfg, seven, state, seen, cooldown)
        time.sleep(float(cfg.get("poll_seconds", 10)))
        # 每轮重新读设置，在游戏库「设置」里改了马上生效
        old_passwords = set(cfg.get("passwords") or [])
        cfg = load_config()
        # 密码都不对的压缩包要冷却两分钟再试；加了新密码就不用等，下一轮马上重试
        if set(cfg.get("passwords") or []) - old_passwords and cooldown:
            log("加了新密码，马上重试之前没解开的压缩包")
            cooldown.clear()
        if watch_dirs:
            cfg["download_dirs"] = watch_dirs


def main():
    parser = argparse.ArgumentParser(description="自动解压下载文件夹里的压缩包")
    parser.add_argument("--once", action="store_true", help="只扫描一次就退出")
    parser.add_argument("--file", help="只处理指定的压缩包")
    parser.add_argument("--dir", nargs="*", help="临时指定要监视的目录")
    parser.add_argument("--events", action="store_true", help="向游戏库报告解压进度")
    args = parser.parse_args()
    if args.events:
        # 游戏库按 UTF-8 读进度；打包成 exe 后没有 python -X utf8 可用，在这里指定
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    cfg = load_config()
    if args.dir:
        cfg["download_dirs"] = args.dir
    seven = find_seven_zip(cfg)
    if not seven:
        log("找不到 7z.exe，请安装 7-Zip，或在 config.json 的 seven_zip 里填路径")
        sys.exit(1)

    if args.file:
        path = Path(args.file)
        if not path.is_absolute():
            path = (ROOT / path).resolve()
        group = archive_group(path)
        if not group:
            log(f"不是可识别的压缩包（或不是分卷第一份）：{path}")
            sys.exit(1)
        with scan_lock() as acquired:
            if not acquired:
                log("已有解压任务在运行，请等它完成后再试")
                sys.exit(1)
            ok, _ = process_group(seven, group, cfg, cfg.get("passwords") or [], [path.parent.resolve()])
        sys.exit(0 if ok else 1)

    if args.once:
        if args.events:
            scan_for_library(seven)
        else:
            scan_once(cfg, seven, load_state(), {}, {}, force=True)
        return
    try:
        watch_loop(cfg, seven, args.dir)
    except KeyboardInterrupt:
        log("已退出")


if __name__ == "__main__":
    main()
