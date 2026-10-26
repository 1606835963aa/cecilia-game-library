"""通过内置浏览器使用 itch.io 登录状态，账号密码始终在官网页面里填写。"""
import json
import threading
import time
from urllib.parse import urlsplit


HOME = "https://itch.io/"
SETTINGS = "https://itch.io/user/settings"
LOGIN = "https://itch.io/login?return_to=https%3A%2F%2Fitch.io%2Fuser%2Fsettings"

# 只读取登录结果和成人内容选项，不读取密码、表单令牌或 Cookie。
STATUS_SCRIPT = r"""
(async () => {
  if (location.origin !== 'https://itch.io') return {status: 'error'};
  const response = await fetch('/user/settings', {credentials: 'include', cache: 'no-store', signal: AbortSignal.timeout(15000)});
  const url = new URL(response.url);
  if (url.origin !== 'https://itch.io') return {status: 'error'};
  if (url.pathname.startsWith('/login')) return {status: 'signed_out'};
  if (!response.ok) return {status: 'verification'};
  const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
  if (doc.querySelector('form[action="/login"], form.login_form')) return {status: 'signed_out'};
  if (!url.pathname.startsWith('/user/settings') || !doc.querySelector('form')) return {status: 'error'};
  // 官网用固定字段保存这个开关，不能依赖随语言变化的选项描述。
  const nsfw = doc.querySelector('input[type="checkbox"][name="data[nsfw]"]');
  let adult = nsfw ? nsfw.checked : null;
  for (const input of adult === null ? doc.querySelectorAll('input[type="checkbox"]') : []) {
    const label = input.id ? doc.querySelector(`label[for="${CSS.escape(input.id)}"]`) : null;
    const identity = [input.name, input.id].join(' ');
    const description = [identity, input.closest('label')?.textContent, label?.textContent].join(' ');
    if (!/nsfw|adult|mature/i.test(description)) continue;
    const polarity = /nsfw|adult|mature/i.test(identity) && /hide|disable|exclude|block|show|allow|enable|include|display|view/i.test(identity) ? identity : description;
    if (/hide|disable|exclude|block|no_nsfw/i.test(polarity)) adult = !input.checked;
    else if (/show|allow|enable|include|display|view/i.test(polarity)) adult = input.checked;
    if (adult !== null) break;
  }
  return {status: 'signed_in', adult};
})()
"""


class ItchAccount:
    def __init__(self, host, on_ready, log):
        self.host = host
        self.on_ready = on_ready
        self.log = log
        self.lock = threading.RLock()
        self.window_lock = threading.Lock()
        self.window = None
        self.visible = False
        self.stopping = False
        self.clearing = False
        self.closing_window = None
        self.generation = 0
        self.checked_at = 0
        self.state = {"status": "signed_out", "adult": None, "checking": False, "message": "未登录"}

    def snapshot(self):
        with self.lock:
            return dict(self.state, available=self.host.window is not None, window_open=self.visible,
                        clearing=self.clearing)

    def get_window(self):
        if not self.host.ready.wait(20) or self.stopping:
            raise ValueError("游戏库窗口还没准备好，请稍后再试")
        with self.window_lock:
            if self.stopping:
                raise ValueError("游戏库正在退出")
            if self.clearing:
                raise ValueError("正在清除 itch.io 登录，请稍后再试")
            if self.window is None:
                import webview

                window = webview.create_window("itch.io 登录", HOME, width=940, height=760,
                                              min_size=(640, 480), hidden=True, text_select=True)
                self.window = window
                window.events.loaded += lambda: self.on_loaded(window)
                window.events.closing += lambda: self.on_closing(window)
                window.events.closed += lambda: self.on_closed(window)
            return self.window

    def wait_window(self):
        window = self.get_window()
        if not window.events.loaded.wait(25):
            raise ValueError("itch.io 页面还没打开，请在设置里打开登录窗口重试")
        return window

    def evaluate_async(self, window, script):
        completed = threading.Event()
        values = []

        def finish(value):
            values.append(value)
            completed.set()

        window.evaluate_js(script, callback=finish)
        if not completed.wait(20):
            raise ValueError("itch.io 响应太慢，请稍后重试")
        return values[0]

    def open(self, settings=False):
        window = self.get_window()
        self.visible = True
        window.show()
        window.restore()
        window.load_url(SETTINGS if settings else LOGIN)
        return self.snapshot()

    def on_loaded(self, window):
        if window is not self.window or self.clearing or self.stopping:
            return
        current = window.get_current_url() or ""
        parsed = urlsplit(current)
        if parsed.scheme != "https" or parsed.hostname != "itch.io":
            return
        with self.lock:
            # 页面跳转会中断旧页面的异步检查，让新页面立即重新确认登录。
            self.generation += 1
            self.state["checking"] = False
        if parsed.path.startswith("/login"):
            verification = window.evaluate_js("!!window._cf_chl_opt || /just a moment|verify|verification/i.test(document.title)")
            with self.lock:
                was_signed_in = self.state["status"] == "signed_in"
                if verification:
                    self.state.update(status="verification", adult=None, message="请在官网窗口完成验证，再登录账号")
                else:
                    self.state.update(status="expired" if was_signed_in else "signed_out", adult=None,
                                      message="登录已过期，请重新登录" if was_signed_in else "请在官网窗口登录")
            return
        self.check_async(retry=parsed.path.startswith("/user/settings"))

    def on_closing(self, window):
        if self.stopping or window is self.closing_window:
            return
        self.visible = False
        window.hide()
        # 窗口藏起来后仍能完成官网检查，后台搜索也继续使用同一个浏览器会话。
        self.check_async()
        return False

    def on_closed(self, window):
        with self.window_lock:
            if self.window is window:
                self.window = None
                self.visible = False

    def check_async(self, retry=False):
        with self.lock:
            if self.state["checking"] or self.clearing or self.stopping:
                return self.snapshot()
            self.state["checking"] = True
            generation = self.generation
        threading.Thread(target=self.check, args=(generation, retry), daemon=True).start()
        return self.snapshot()

    def check(self, generation, retry=False):
        try:
            window = self.wait_window()
            result = self.evaluate_async(window, STATUS_SCRIPT)
            if not isinstance(result, dict):
                raise ValueError("没有读到 itch.io 登录结果，请打开官网窗口重试")
            with self.lock:
                if generation != self.generation:
                    return
                previous = dict(self.state)
                status, adult = result.get("status"), result.get("adult")
                if status == "signed_in":
                    message = {True: "已登录，成人内容已开启", False: "已登录，成人内容未开启"}.get(adult, "已登录，请在官网确认成人内容设置")
                    self.state.update(status=status, adult=adult, message=message)
                elif status == "signed_out":
                    expired = previous["status"] in ("signed_in", "expired")
                    self.state.update(status="expired" if expired else status, adult=None,
                                      message="登录已过期，请重新登录" if expired else "未登录")
                else:
                    self.state.update(status="verification" if status == "verification" else "error",
                                      message="请打开官网窗口完成验证，再检查登录状态")
                self.checked_at = time.monotonic()
                ready = self.state["status"] == "signed_in" and adult is not False
                changed = previous["status"] != "signed_in" or (adult is True and previous["adult"] is not True)
            if ready and (changed or retry):
                try:
                    self.on_ready()
                except Exception:
                    self.log("itch.io 已登录，暂时没能重新查找未匹配的游戏资料")
        except Exception:
            with self.lock:
                if generation == self.generation:
                    self.state.update(status="error", message="暂时没能检查登录，请打开官网窗口重试")
        finally:
            with self.lock:
                if generation == self.generation:
                    self.state["checking"] = False

    def search_page(self, url):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "itch.io" or parsed.path != "/search":
            raise ValueError("不是 itch.io 搜索地址")
        window = self.wait_window()
        if time.monotonic() - self.checked_at > 300:
            self.check_async()
        # 搜索留在 itch.io 自己的页面里，登录信息不会交给本机页面或其他资料网站。
        script = """(async () => {
          if (location.origin !== 'https://itch.io') return {status: 0};
          const response = await fetch(%s, {credentials: 'include', cache: 'no-store', signal: AbortSignal.timeout(15000)});
          if (new URL(response.url).origin !== 'https://itch.io') return {status: 0};
          return {status: response.status, body: await response.text()};
        })()""" % json.dumps(url)
        result = self.evaluate_async(window, script)
        if not isinstance(result, dict) or result.get("status") != 200:
            raise OSError("itch.io 搜索暂时不可用，请打开登录窗口检查")
        return result["body"]

    def clear_browser_cookies(self):
        completed = threading.Event()
        errors = []

        def start():
            from System import Action
            from System.Collections.Generic import List
            from System.Threading.Tasks import Task, TaskScheduler
            from Microsoft.Web.WebView2.Core import CoreWebView2Cookie

            manager = self.host.window.native.browser.webview.CoreWebView2.CookieManager

            def finish(task):
                try:
                    for cookie in task.Result:
                        if str(cookie.Domain).lstrip(".").lower() == "itch.io":
                            manager.DeleteCookie(cookie)
                except Exception as error:
                    errors.append(error)
                finally:
                    completed.set()

            manager.GetCookiesAsync("").ContinueWith(Action[Task[List[CoreWebView2Cookie]]](finish),
                                                    TaskScheduler.FromCurrentSynchronizationContext())

        self.host.on_ui_thread(start)
        if not completed.wait(15) or errors:
            raise ValueError("itch.io 登录没能清除，请稍后再试")

    def logout(self):
        with self.lock:
            if self.clearing:
                raise ValueError("正在清除登录，请稍后再试")
            self.clearing = True
            self.generation += 1
            self.state.update(status="signed_out", adult=None, checking=False, message="未登录")
        try:
            window = self.window
            if window and urlsplit(window.get_current_url() or "").hostname == "itch.io":
                try:
                    window.evaluate_js("localStorage.clear(); sessionStorage.clear();")
                except Exception:
                    pass
            self.close_window()
            self.clear_browser_cookies()
            self.checked_at = 0
        except Exception:
            with self.lock:
                self.state.update(status="error", message="登录没能完全清除，请稍后重试")
            raise ValueError("登录没能完全清除，请稍后重试") from None
        finally:
            self.clearing = False
        return self.snapshot()

    def close_window(self):
        window = self.window
        if window:
            self.closing_window = window
            window.destroy()
            with self.window_lock:
                if self.window is window:
                    self.window = None
            self.visible = False

    def shutdown(self):
        self.stopping = True
        self.close_window()
