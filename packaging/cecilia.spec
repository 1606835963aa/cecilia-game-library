# PyInstaller 打包配置：游戏库和自动解压两个 exe 共用一个 _internal 文件夹。
# 用 packaging\build.py 调用，不直接运行。
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
ICON = str(ROOT / "web" / "icon.ico")
# pywebview 的 Qt 界面和顺带进来的 numpy 用不到，不排除的话包会大一倍
EXCLUDES = ["tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "qtpy", "numpy", "win32ui", "pythonwin"]

library = Analysis(
    [str(ROOT / "game_manager.py")],
    pathex=[str(ROOT)],
    datas=[(str(ROOT / "web"), "web")],
    hiddenimports=collect_submodules("uvicorn"),  # uvicorn 按名字动态加载协议和事件循环，静态分析找不到
    excludes=EXCLUDES,
)
extract = Analysis(
    [str(ROOT / "auto_extract.py")],
    pathex=[str(ROOT)],
    excludes=EXCLUDES,
)

library_exe = EXE(
    PYZ(library.pure),
    library.scripts,
    exclude_binaries=True,
    name="塞西莉亚的游戏库",
    icon=ICON,
    console=False,
    upx=False,  # 压缩过的 exe 更容易被杀毒软件误报
)
extract_exe = EXE(
    PYZ(extract.pure),
    extract.scripts,
    exclude_binaries=True,
    name="自动解压",
    icon=ICON,
    console=True,  # 常驻监视时要显示运行记录；游戏库刷新时隐藏窗口读它的输出
    upx=False,
)

COLLECT(
    library_exe, library.binaries, library.datas,
    extract_exe, extract.binaries, extract.datas,
    name="塞西莉亚的游戏库",
    upx=False,
)
