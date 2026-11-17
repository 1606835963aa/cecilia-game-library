"""下载固定版本的冷藏图片工具，核对 SHA-256 后保存到项目 codecs 目录。"""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = {
    "packPNG.exe": (
        "https://github.com/YadeWira/packPNG/releases/download/v2.0l/packPNG_win_x64.exe",
        "838e9c1b35ed34b5b0f0c98a1ba2acb88dc5522bb2bee3821743c39b09ebecf0"),
    "jxl.zip": (
        "https://github.com/libjxl/libjxl/releases/download/v0.12.0/jxl-x64-windows-static.zip",
        "3025d7e308390796d20492322e606bc92decaee7b6bc99d3f7547870ae5db7de"),
}


def main():
    if os.name != "nt":
        raise ValueError("此下载脚本提供 Windows 版工具")
    seven = Path(r"C:\Program Files\7-Zip\7z.exe")
    if not seven.is_file():
        raise ValueError("解包 JPEG 工具需要默认位置的 7-Zip")
    temporary = ROOT / "tmp"
    created = not temporary.exists()
    temporary.mkdir(exist_ok=True)
    destination = ROOT / "codecs"
    try:
        with tempfile.TemporaryDirectory(prefix="cold-codecs-", dir=temporary) as folder:
            work = Path(folder).resolve()
            if not work.is_relative_to(temporary.resolve()):
                raise ValueError("下载目录超出项目 tmp")
            records = []
            for name, (url, expected) in TOOLS.items():
                path = work / name
                request = urllib.request.Request(url, headers={"User-Agent": "Cecilia-Cold-Codecs"})
                with urllib.request.urlopen(request, timeout=90) as source, path.open("xb") as output:
                    shutil.copyfileobj(source, output)
                checksum = hashlib.sha256(path.read_bytes()).hexdigest()
                if checksum != expected:
                    raise ValueError("下载的工具校验不一致，未安装")
                records.append({"name": name, "url": url, "sha256": checksum})
            result = subprocess.run(
                [str(seven), "x", "-y", "-bsp0", "-bso0", "-o" + str(work / "jxl"), str(work / "jxl.zip"),
                 "x64-windows-static/bin/cjxl.exe", "x64-windows-static/bin/djxl.exe"],
                cwd=work, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW, timeout=90)
            if result.returncode:
                raise ValueError("JPEG 编解码工具解包失败")
            sources = {"packPNG.exe": work / "packPNG.exe",
                       "cjxl.exe": work / "jxl/x64-windows-static/bin/cjxl.exe",
                       "djxl.exe": work / "jxl/x64-windows-static/bin/djxl.exe"}
            for path in sources.values():
                if not path.is_file() or path.stat().st_size == 0:
                    raise ValueError("下载的工具不完整")
            destination.mkdir(exist_ok=True)
            for name, path in sources.items():
                # 已校验的文件才替换本地工具，下载失败不影响已有解码能力。
                os.replace(path, destination / name)
            (destination / "sources.json").write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
            print("PNG 和 JPEG 工具已保存到项目 codecs 文件夹。")
    finally:
        if created:
            try:
                temporary.rmdir()
            except OSError:
                pass


if __name__ == "__main__":
    main()
