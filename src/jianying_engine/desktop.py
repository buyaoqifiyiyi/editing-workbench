"""Windows EdgeChromium shell for the existing loopback workbench, with owned lifecycle."""
import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
import ctypes
import http.client
import json
import logging
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit


def window_dimensions(work_area, dpi=96):
    scale = max(1.0, dpi / 96)
    width = min(1280, max(1, int((work_area[2] - work_area[0]) / scale * 0.9)))
    height = min(720, max(1, int((work_area[3] - work_area[1]) / scale * 0.9)))
    return width, height


def desktop_dimensions():
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except AttributeError:
        pass
    area = wintypes.RECT()
    if not user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(area), 0):
        raise OSError('无法读取屏幕工作区，未创建可能超出屏幕的窗口。')
    dpi = user32.GetDpiForSystem() if hasattr(user32, 'GetDpiForSystem') else 96
    return window_dimensions((area.left, area.top, area.right, area.bottom), dpi)


def show_error(message):
    logging.error('Desktop workbench: %s', message)
    if os.name == 'nt':
        ctypes.windll.user32.MessageBoxW(None, str(message), '剪辑工作台', 0x10)


def activate_existing(info):
    if info.get('mode', 'browser') != 'desktop':
        raise RuntimeError('已有浏览器模式工作台正在运行。请先正常退出该服务，再启动桌面应用；未接管或停止现有服务。')
    port, token = info.get('port'), info.get('activate_token')
    if type(port) is not int or not 0 < port <= 65535 or not isinstance(token, str) or not token:
        raise RuntimeError('已有桌面实例的状态记录不完整，请核对服务状态；未停止任何进程。')
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
    try:
        connection.request('POST', '/api/activate', body=b'{}',
                           headers={'X-Workbench-Token': token, 'Content-Type': 'application/json'})
        response = connection.getresponse()
        response.read(4096)
        if response.status != 200:
            raise RuntimeError('已有桌面实例未能响应唤醒，请检查服务日志；未停止任何进程。')
    finally:
        connection.close()


def navigation_allowed(uri, local_url):
    try:
        target, local = urlsplit(uri), urlsplit(local_url)
        return (target.scheme == 'http' and target.hostname == '127.0.0.1'
                and target.port == local.port and not target.username and not target.password)
    except ValueError:
        return False


class DesktopHost:
    """Main-thread GUI and a single owned asyncio server thread."""
    def __init__(self, service, sock, url, state_dir):
        self.service, self.sock, self.url = service, sock, url
        self.state_dir = Path(state_dir)
        self.server = self.loop = self.thread = self.window = None
        self._serve_task = None
        self._closed = False
        self.error = None
        self._guard_restore = None
        self._webview = None

    def start_server(self, app, timeout=10):
        import uvicorn
        self.server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=self.sock.getsockname()[1],
                                    access_log=False, log_config=None, timeout_graceful_shutdown=3))
        async def serve():
            self.loop = asyncio.get_running_loop()
            self._serve_task = asyncio.current_task()
            await self.server.serve(sockets=[self.sock])
        def worker():
            try:
                asyncio.run(serve())
            except BaseException as exc:
                if not isinstance(exc, asyncio.CancelledError):
                    self.error = f'本机服务启动或运行失败：{type(exc).__name__}'
                    logging.exception('Desktop server failed')
        self.thread = threading.Thread(target=worker, name='jianying-workbench-server', daemon=True)
        self.thread.start()
        deadline = time.monotonic() + timeout
        while not self.server.started:
            if not self.thread.is_alive() or self.error:
                raise RuntimeError(self.error or '本机服务未能启动。')
            if time.monotonic() >= deadline:
                raise TimeoutError('等待本机服务启动超时。')
            time.sleep(0.02)

    def _call(self, coroutine, timeout=10):
        if self.loop is None or not self.loop.is_running():
            coroutine.close()
            raise RuntimeError('桌面后台服务已停止。')
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return future.result(timeout)
        except FutureTimeout:
            future.cancel()
            raise TimeoutError('等待桌面后台操作超时。')

    def activate(self):
        if self.window is None or self._closed:
            raise RuntimeError('桌面窗口尚未就绪或正在关闭。')
        self.window.restore()
        self.window.show()
        if self.window.native is not None:
            from System import Action
            self.window.native.BeginInvoke(Action(lambda: self.window.native.Activate()))

    def pick(self, kind):
        if self.window is None or self._closed:
            raise ValueError('桌面窗口尚未就绪。')
        if kind == 'folder':
            result = self.window.create_file_dialog(self._webview.FileDialog.FOLDER)
        elif kind == 'files':
            result = self.window.create_file_dialog(self._webview.FileDialog.OPEN, allow_multiple=True,
                                                    file_types=('媒体文件 (*.mp4;*.mov;*.mkv;*.avi;*.webm;*.wav;*.mp3;*.m4a;*.flac;*.png;*.jpg;*.jpeg;*.webp)', '所有文件 (*.*)'))
        else:
            raise ValueError('未知的选择类型。')
        return list(result) if result else []

    def request_close(self):
        if self._closed:
            return True
        async def busy():
            from .workbench import BUSY
            active = self.service.active()
            return bool(active and active.get('status') in BUSY)
        try:
            is_busy = self._call(busy(), 2)
        except Exception:
            active = self.service.active()
            is_busy = bool(active and active.get('status') in {'starting', 'running', 'stopping', 'awaiting_approval'})
        if is_busy and not self.window.create_confirmation_dialog('退出剪辑工作台', '当前任务仍在执行或等待审批。退出将中断任务并保存会话记录。确定退出吗？'):
            return False
        try:
            self.shutdown()
        except Exception as exc:
            self.error = str(exc)
            show_error(self.error)
        return True

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self.loop and self.loop.is_running():
                try:
                    self._call(self.service.close(), 10)
                except Exception:
                    logging.exception('Bounded desktop service close failed')
                    # Only this Workbench's owned app-server tree; never search by process name.
                    bridge = self.service.bridge
                    tree = getattr(bridge, '_tree', None)
                    if tree is not None:
                        tree.close()
                    self.service.save()
            else:
                self.service.save()
        finally:
            if self.server is not None:
                self.server.should_exit = True
            if self.thread and self.thread is not threading.current_thread():
                self.thread.join(5)
                if self.thread.is_alive() and self.loop and self.loop.is_running():
                    self.server.force_exit = True
                    if self._serve_task:
                        self.loop.call_soon_threadsafe(self._serve_task.cancel)
                    self.thread.join(3)
            self.sock.close()
        if self.thread and self.thread.is_alive():
            raise RuntimeError('后台线程未及时退出；自有连接已关闭，请检查server.log。')

    def _install_guards(self, renderer):
        if renderer != 'edgechromium':
            self.error = '桌面模式要求 Microsoft Edge WebView2，未使用其它浏览器内核降级。'
            return False
        from webview.platforms.edgechromium import EdgeChrome
        original_navigation, original_popup = EdgeChrome.on_navigation_start, EdgeChrome.on_new_window_request
        def navigation(browser, sender, args):
            if not navigation_allowed(str(args.Uri), self.url):
                args.Cancel = True
                return
            original_navigation(browser, sender, args)
        def popup(browser, sender, args):
            args.Handled = True
        EdgeChrome.on_navigation_start, EdgeChrome.on_new_window_request = navigation, popup
        self._guard_restore = (EdgeChrome, original_navigation, original_popup)

    def _shown(self):
        try:
            from System import Action
            from System.Drawing import Icon
            icon = Path(__file__).with_name('web') / 'app.ico'
            if icon.is_file():
                self.window.native.Invoke(Action(lambda: setattr(self.window.native, 'Icon', Icon(str(icon)))))
        except Exception:
            logging.exception('Could not set desktop window icon')

    def run(self, webview_module=None):
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError('桌面窗口必须从主线程启动。')
        if os.name != 'nt':
            raise RuntimeError('当前桌面模式仅支持 Windows + Edge WebView2。')
        if webview_module is None:
            import webview as webview_module
        self._webview = webview_module
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('JianyingEngine.Workbench')
        webview_module.settings['ALLOW_FILE_URLS'] = False
        webview_module.settings['OPEN_EXTERNAL_LINKS_IN_BROWSER'] = False
        webview_module.settings['ALLOW_DOWNLOADS'] = False
        width, height = desktop_dimensions()
        self.window = webview_module.create_window('剪辑工作台', self.url, width=width, height=height,
                                                  min_size=(min(800, width), min(600, height)), js_api=None, confirm_close=False,
                                                  background_color='#f3f3f3')
        self.window.events.initialized += self._install_guards
        self.window.events.shown += self._shown
        self.window.events.closing += self.request_close
        try:
            webview_module.start(gui='edgechromium', debug=False, private_mode=True,
                                 storage_path=str(self.state_dir / 'webview'))
            if self.error:
                raise RuntimeError(self.error)
        finally:
            if self._guard_restore:
                renderer, navigation, popup = self._guard_restore
                renderer.on_navigation_start, renderer.on_new_window_request = navigation, popup
            self.shutdown()

