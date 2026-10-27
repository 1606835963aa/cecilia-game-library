"""打包绿色版：解压后双击 exe 就能用，不用装 Python。

    python packaging\\build.py

需要先装 PyInstaller（pip install pyinstaller）和 7-Zip。
结果在 dist\\塞西莉亚的游戏库（文件夹）和 dist\\CeciliaGameLibrary-portable.zip。
"""
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app_version import VERSION
from update_helper import MANIFEST, OWNED, digest, write_json
NAME = "塞西莉亚的游戏库"
DIST = ROOT / "dist"
APP = DIST / NAME
ZIP = DIST / "CeciliaGameLibrary-portable.zip"  # GitHub 发布页会把中文文件名改成「-.zip」，zip 名用英文
WORK = ROOT / "tmp" / "pyinstaller"
SEVEN_ZIP = Path(r"C:\Program Files\7-Zip")
# 7-Zip 命令行解压只需要这两个文件；按 7-Zip 的许可证要求带上许可证原文
SEVEN_ZIP_FILES = ("7z.exe", "7z.dll", "License.txt")


def main():
    occupied = [name for name in ("config.json", "data", "logs", "android") if (APP / name).exists()]
    occupied += [name for name in ("games", "downloads") if (APP / name).is_dir() and any((APP / name).iterdir())]
    if occupied:
        sys.exit("打包目录里有使用数据（" + "、".join(occupied) + "），请先将它移到其他位置保存")
    missing = [f for f in SEVEN_ZIP_FILES if not (SEVEN_ZIP / f).is_file()]
    if missing:
        sys.exit(f"没找到 7-Zip 的 {'、'.join(missing)}，先把 7-Zip 装在默认位置 {SEVEN_ZIP}")

    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                    "--distpath", str(DIST), "--workpath", str(WORK), str(ROOT / "packaging" / "cecilia.spec")],
                   check=True)

    (APP / "7-Zip").mkdir(exist_ok=True)
    for f in SEVEN_ZIP_FILES:
        shutil.copy2(SEVEN_ZIP / f, APP / "7-Zip" / f)
    shutil.copy2(ROOT / "LICENSE", APP / "LICENSE")
    shutil.move(str(DIST / "更新助手.exe"), str(APP / "更新助手.exe"))
    files = {}
    for name in OWNED:
        if name == MANIFEST:
            continue
        item = APP / name
        for path in sorted(item.rglob("*")) if item.is_dir() else [item]:
            if path.is_file():
                files[path.relative_to(APP).as_posix()] = {"size": path.stat().st_size, "sha256": digest(path)}
    write_json(APP / MANIFEST, {"schema": 1, "version": VERSION, "files": files})
    # 解压出来就能看到这两个文件夹，第一次打开程序前就能把网盘的下载位置设到 downloads
    for folder in ("downloads", "games"):
        (APP / folder).mkdir(exist_ok=True)

    ZIP.unlink(missing_ok=True)
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(APP.rglob("*")):
            if path.relative_to(APP).parts[0] in OWNED or path.relative_to(APP).as_posix() in ("downloads", "games"):
                zf.write(path, path.relative_to(DIST))
    (DIST / (ZIP.name + ".sha256")).write_text(f"{digest(ZIP)}  {ZIP.name}\n", encoding="ascii")
    shutil.rmtree(WORK, ignore_errors=True)
    print(f"\n{VERSION} 打包完成：{ZIP}（{ZIP.stat().st_size / 1024 / 1024:.0f} MB）")


if __name__ == "__main__":
    main()
