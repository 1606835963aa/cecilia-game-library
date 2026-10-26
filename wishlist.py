"""愿望单的加入时版本、官网版本记录和本地游戏关联。"""
import json
import re
import shutil
from uuid import uuid4

import library


def comparison(baseline, current):
    if not baseline or not current:
        return "unknown"

    def parts(text):
        match = re.fullmatch(r"[vV]?(\d+(?:\.\d+)*)(?:[ -]?(alpha|beta|rc|a|b)(\d*))?", text.strip(), re.I)
        if not match:
            return None
        numbers = [int(n) for n in match.group(1).split(".")]
        while numbers and numbers[-1] == 0:
            numbers.pop()
        stage = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "rc": 2, None: 3}[match.group(2).lower() if match.group(2) else None]
        return numbers, stage, int(match.group(3) or 0)

    left, right = parts(baseline), parts(current)
    if left is not None and right is not None:
        width = max(len(left[0]), len(right[0]))
        left = (left[0] + [0] * (width - len(left[0])), *left[1:])
        right = (right[0] + [0] * (width - len(right[0])), *right[1:])
        return "newer" if right > left else "older" if right < left else "same"
    return "same" if baseline.strip().lower().lstrip("v") == current.strip().lower().lstrip("v") else "changed"


def create(meta, baseline=""):
    source, item_id = meta["source"], str(meta["id"])
    with library._db_lock, library.connect() as conn:
        existing = conn.execute("SELECT id FROM games WHERE meta_source=? AND meta_id=? ORDER BY is_wishlist,id LIMIT 1", (source, item_id)).fetchone()
        if existing:
            return existing["id"], False
        now = library.now_str()
        cursor = conn.execute(
            "INSERT INTO games(path,name,is_wishlist,wishlisted_at,wish_version,meta_source,meta_id,scraped,added_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("_wishlist/" + uuid4().hex, meta["title"], 1, now, str(baseline).strip()[:80], source, item_id, 1, now),
        )
        return cursor.lastrowid, True


def record_version(game_id, version, evidence="", status=None):
    version = str(version or "").strip()[:80]
    with library._db_lock, library.connect() as conn:
        game = conn.execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone()
        if not game or not game["wishlisted_at"]:
            return
        now = library.now_str()
        history = json.loads(game["version_history"] or "[]")
        if version and (not history or history[-1]["version"] != version):
            history.append({"version": version, "checked_at": now, "evidence": evidence})
        # 加入时没有标注的版本保持空白，不能用后来发现的版本补写历史。
        conn.execute(
            "UPDATE games SET latest_version=?,version_checked_at=?,version_status=?,version_evidence=?,version_history=? WHERE id=?",
            (version or game["latest_version"], now, status or ("found" if version else "unknown"), evidence[:300], json.dumps(history, ensure_ascii=False), game_id),
        )


def set_initial_version(game_id, detected, manual="", evidence=""):
    baseline = str(manual or detected or "").strip()[:80]
    with library._db_lock, library.connect() as conn:
        conn.execute("UPDATE games SET wish_version=? WHERE id=?", (baseline, game_id))
    record_version(game_id, detected or manual, evidence if detected else "手动填写" if manual else "", None if detected else "manual" if manual else "unknown")


def bind(wish_id, local_id):
    if wish_id == local_id:
        return wish_id
    with library._db_lock, library.connect() as conn:
        wish = conn.execute("SELECT * FROM games WHERE id=?", (wish_id,)).fetchone()
        local = conn.execute("SELECT * FROM games WHERE id=?", (local_id,)).fetchone()
        if not wish or not wish["is_wishlist"] or not local or local["is_wishlist"]:
            raise ValueError("请选择愿望单记录和已经下载的游戏")
        if local["missing"] or not library.to_abs(local["path"]).exists():
            raise ValueError("这个游戏的本地文件已经不在了")
        if conn.execute("SELECT 1 FROM sessions WHERE game_id IN (?,?) AND ended_at IS NULL", (wish_id, local_id)).fetchone():
            raise ValueError("游戏正在运行，退出后再关联")
        fields = {key: local[key] for key in ("path", "version", "platform", "engine", "exe", "exe_locked", "size_bytes", "suspicious", "ad_files", "status")}
        fields.update(is_wishlist=0, missing=0)

        def adopt_image(name):
            if not name or not name.startswith(f"{local_id}-"):
                return name
            target = f"{wish_id}-" + name.split("-", 1)[1]
            path = library.COVER_DIR / name
            if path.is_file():
                shutil.copy2(path, library.COVER_DIR / target)
                return target
            return ""

        for kind in ("cover", "hero", "logo", "icon"):
            fields[kind] = wish[kind] if "-custom" in (wish[kind] or "") else adopt_image(local[kind]) or wish[kind]
        local_shots = json.loads(local["screenshots"] or "[]")
        if local_shots:
            fields["screenshots"] = json.dumps([adopt_image(name) for name in local_shots], ensure_ascii=False)
        fields["notes"] = "\n\n".join(dict.fromkeys(text for text in (wish["notes"], local["notes"]) if text))
        fields["tags"] = wish["tags"] or local["tags"]
        fields["rating"] = wish["rating"] or local["rating"]
        conn.execute("UPDATE sessions SET game_id=? WHERE game_id=?", (wish_id, local_id))
        conn.execute("DELETE FROM games WHERE id=?", (local_id,))
        sets = ",".join(f"{key}=?" for key in fields)
        conn.execute(f"UPDATE games SET {sets} WHERE id=?", (*fields.values(), wish_id))
    for path in library.COVER_DIR.glob(f"{local_id}-*"):
        path.unlink(missing_ok=True)
    return wish_id


def decorate(games):
    for game in games:
        game["version_comparison"] = comparison(game["wish_version"], game["latest_version"])
    return games
