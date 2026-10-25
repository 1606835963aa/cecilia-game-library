"""自绘标题栏：去掉 Windows 自带的标题栏，让页面顶部导航条顶到窗口最上面。

窗口边框还是系统的，所以拖边缩放、贴边分屏、阴影、最小化动画都照旧。
拖动窗口交给 WebView2：页面里标了 app-region: drag 的地方（顶部导航条空白处）就当标题栏用，
能拖、双击最大化、右键出系统菜单。最小化、最大化、关闭三个按钮由页面自己画。
"""
import ctypes
import time
from ctypes import wintypes

user32 = ctypes.windll.user32
comctl32 = ctypes.windll.comctl32

WM_SIZE = 0x0005
WM_WINDOWPOSCHANGING = 0x0046
WM_NCCALCSIZE = 0x0083
WM_NCHITTEST = 0x0084
WM_SYSCOMMAND = 0x0112
HTCLIENT, HTTOP, HTTOPLEFT, HTTOPRIGHT = 1, 12, 13, 14
SC_MINIMIZE, SC_MAXIMIZE, SC_RESTORE, SC_CLOSE = 0xF020, 0xF030, 0xF120, 0xF060
SM_CXSIZEFRAME, SM_CYSIZEFRAME, SM_CXPADDEDBORDER = 32, 33, 92
SWP_NOSIZE = 0x0001
SWP_FRAMECHANGED = 0x0020 | 0x0001 | 0x0002 | 0x0004 | 0x0010  # 加上不挪位置、不改大小、不改层级、不抢焦点
SIZE_RESTORED, SIZE_MAXIMIZED = 0, 2

LRESULT = ctypes.c_ssize_t
SUBCLASSPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                  ctypes.c_size_t, ctypes.c_size_t)
comctl32.DefSubclassProc.restype = LRESULT
comctl32.DefSubclassProc.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
comctl32.SetWindowSubclass.argtypes = (wintypes.HWND, SUBCLASSPROC, ctypes.c_size_t, ctypes.c_size_t)
user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))


class NCCALCSIZE_PARAMS(ctypes.Structure):
    _fields_ = [("rgrc", wintypes.RECT * 3), ("lppos", ctypes.c_void_p)]


class WINDOWPOS(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("after", wintypes.HWND), ("x", ctypes.c_int), ("y", ctypes.c_int),
                ("cx", ctypes.c_int), ("cy", ctypes.c_int), ("flags", wintypes.UINT)]


class CustomFrame:
    def __init__(self, hwnd):
        self.hwnd = hwnd
        self.last_size_state = SIZE_RESTORED
        self.pin_size = None
        self.pin_until = 0
        self._proc = SUBCLASSPROC(self._wndproc)  # 留着引用，不然会被回收，窗口一收到消息就崩

    def install(self):
        """要在窗口所在的线程里调用。"""
        comctl32.SetWindowSubclass(self.hwnd, self._proc, 1, 0)
        user32.SetWindowPos(self.hwnd, None, 0, 0, 0, 0, SWP_FRAMECHANGED)
        self.last_size_state = SIZE_MAXIMIZED if self.maximized() else SIZE_RESTORED

    def frame_size(self):
        """系统的可拖动边框有多厚（随缩放比例变）。"""
        dpi = user32.GetDpiForWindow(self.hwnd) or 96
        pad = user32.GetSystemMetricsForDpi(SM_CXPADDEDBORDER, dpi)
        return (user32.GetSystemMetricsForDpi(SM_CXSIZEFRAME, dpi) + pad,
                user32.GetSystemMetricsForDpi(SM_CYSIZEFRAME, dpi) + pad)

    def maximized(self):
        return bool(user32.IsZoomed(self.hwnd))

    def command(self, action):
        """页面上的三个按钮：min / max / close。发系统命令，和点系统按钮效果一样。"""
        code = {"min": SC_MINIMIZE, "close": SC_CLOSE,
                "max": SC_RESTORE if self.maximized() else SC_MAXIMIZE}[action]
        user32.PostMessageW(self.hwnd, WM_SYSCOMMAND, code, 0)

    def _wndproc(self, hwnd, msg, wparam, lparam, _id, _data):
        if msg == WM_SIZE:
            # WinForms 从最大化、最小化还原后，会再按“还有系统标题栏”改一次窗口大小，每还原一次就高出一截标题栏；
            # 记下系统刚还原好的大小（贴边分屏的半屏大小也算），一秒内的那次改大小钉回这个大小
            if wparam == SIZE_RESTORED and self.last_size_state != SIZE_RESTORED:
                rect = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                self.pin_size = (rect.right - rect.left, rect.bottom - rect.top)
                self.pin_until = time.monotonic() + 1
            self.last_size_state = wparam
        if msg == WM_WINDOWPOSCHANGING and self.pin_until:
            pos = ctypes.cast(lparam, ctypes.POINTER(WINDOWPOS)).contents
            if time.monotonic() > self.pin_until:
                self.pin_until = 0
            elif not pos.flags & SWP_NOSIZE and (pos.cx, pos.cy) != self.pin_size:
                pos.cx, pos.cy = self.pin_size
                self.pin_until = 0
        if msg == WM_NCCALCSIZE and wparam:
            # 左右下的边框照系统算，上面的标题栏不要；
            # 最大化时窗口会超出屏幕一圈边框，顶上要让出这一圈，不然导航条会被切掉一截
            params = ctypes.cast(lparam, ctypes.POINTER(NCCALCSIZE_PARAMS)).contents
            top = params.rgrc[0].top
            comctl32.DefSubclassProc(hwnd, msg, wparam, lparam)
            params.rgrc[0].top = top + (self.frame_size()[1] if self.maximized() else 0)
            return 0
        if msg == WM_NCHITTEST:
            hit = comctl32.DefSubclassProc(hwnd, msg, wparam, lparam)
            if hit != HTCLIENT or self.maximized():
                return hit
            # 标题栏没了，最上面一条边框也没了：顶边几个像素当成上边框，照样能往上拉
            x, y = ctypes.c_short(lparam & 0xFFFF).value, ctypes.c_short((lparam >> 16) & 0xFFFF).value
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            fx, fy = self.frame_size()
            if y < rect.top + fy:
                if x < rect.left + fx * 2:
                    return HTTOPLEFT
                if x >= rect.right - fx * 2:
                    return HTTOPRIGHT
                return HTTOP
            return hit
        return comctl32.DefSubclassProc(hwnd, msg, wparam, lparam)
