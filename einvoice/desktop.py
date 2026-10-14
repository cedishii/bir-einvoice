"""Desktop mode: the app in its own window, no browser tabs and no console.

    .venv\\Scripts\\pythonw.exe -m einvoice.desktop     (what the desktop shortcut runs)

Starts the same local server as run.bat, opens it in a Microsoft Edge (or
Chrome) "app" window, and stops the server once every app window is closed.
If the app is already running (from run.bat or another window), it just
opens another window onto it. Both views share the same data.

Edge hands the window over to another process, so the server can't simply
wait for the browser process. Instead each page reports in (see Presence):
a ping every few seconds, and a "bye" when the page goes away. Navigating
between pages sends a bye followed at once by a ping from the next page, so
a short grace period tells a closed window from a page change.
"""
from __future__ import annotations

import ctypes
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from pathlib import Path

from flask import Flask, request
from werkzeug.serving import make_server

from .config import load_config
from .web.app import create_app

APP_ID = "Micronet.eInvoice"   # Windows AppUserModelID, also set on the shortcut
APP_TITLE = "Micronet e-Invoice"
ICON_PATH = Path(__file__).resolve().parent.parent / "assets" / "app.ico"
HOST = "127.0.0.1"
PREFERRED_PORT = 5000
WINDOW_SIZE = "1280,900"

BROWSERS = [
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
]


PING_SECONDS = 10       # how often a page reports in (also in base.html)
BYE_GRACE_SECONDS = 6   # a page change re-pings well within this
# A minimised window's timers may be slowed to once a minute by the browser,
# so a window only counts as gone after this long without a ping.
STALE_SECONDS = 150
FIRST_WINDOW_SECONDS = 120


class Presence:
    """Tracks open app windows; calls `on_empty` when the last one closes."""

    def __init__(self, on_empty, clock=time.monotonic):
        self.on_empty = on_empty
        self.clock = clock
        self.lock = threading.Lock()
        self.last_seen: dict[str, float] = {}
        self.bye_at: dict[str, float] = {}
        self.started = clock()
        self.seen_any = False
        self.stopped = False

    def ping(self, window_id: str) -> None:
        with self.lock:
            self.last_seen[window_id] = self.clock()
            self.bye_at.pop(window_id, None)
            self.seen_any = True

    def bye(self, window_id: str) -> None:
        with self.lock:
            self.bye_at[window_id] = self.clock()

    def check(self) -> None:
        now = self.clock()
        with self.lock:
            for wid, t in list(self.bye_at.items()):
                if now - t > BYE_GRACE_SECONDS:
                    self.last_seen.pop(wid, None)
                    del self.bye_at[wid]
            for wid, t in list(self.last_seen.items()):
                if now - t > STALE_SECONDS:
                    del self.last_seen[wid]
            empty = not self.last_seen and (
                self.seen_any or now - self.started > FIRST_WINDOW_SECONDS)
            if empty and not self.stopped:
                self.stopped = True
            else:
                return
        self.on_empty()

    def watch(self, interval: float = 2.0) -> None:
        while not self.stopped:
            time.sleep(interval)
            self.check()

    def install(self, app: Flask) -> None:
        app.config["DESKTOP"] = True

        @app.post("/_window/ping")
        def window_ping():
            self.ping(request.form.get("id", "") or "?")
            return "", 204

        @app.post("/_window/bye")
        def window_bye():
            self.bye(request.form.get("id", "") or "?")
            return "", 204


def find_browser() -> str | None:
    for candidate in BROWSERS:
        path = os.path.expandvars(candidate)
        if os.path.isfile(path):
            return path
    return None


def already_running(port: int) -> bool:
    """True if this app (not something else) is answering on the port."""
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/logo.png", timeout=1) as r:
            return r.status == 200 and r.headers.get_content_type() == "image/png"
    except OSError:
        return False


def port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex((HOST, port)) != 0


def free_port() -> int:
    with socket.socket() as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def open_window(url: str, profile_dir: Path) -> subprocess.Popen | None:
    browser = find_browser()
    if browser is None:
        webbrowser.open(url)
        return None
    profile_dir.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen([
        browser, f"--app={url}", f"--user-data-dir={profile_dir}",
        f"--window-size={WINDOW_SIZE}", "--no-first-run", "--no-default-browser-check",
        # Browser extensions (e.g. writing assistants) have no business reading
        # invoice data, and they open their own welcome pages in a new profile.
        "--disable-extensions",
    ])


def message_box(text: str, title: str = "Micronet e-Invoice") -> None:
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)
    except Exception:
        print(text, file=sys.stderr)


def set_app_id() -> None:
    """Group this process's windows under our own taskbar entry, not Python's."""
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass


def start_server(cfg, presence: "Presence | None" = None):
    port = PREFERRED_PORT if port_free(PREFERRED_PORT) else free_port()
    app = create_app(cfg)
    if presence is not None:
        presence.install(app)
    server = make_server(HOST, port, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


def _set_icon(window) -> None:
    """Use the company logo as the window icon (instead of Python's)."""
    try:
        import clr  # pythonnet, installed with pywebview
        clr.AddReference("System.Drawing")
        from System import Action
        from System.Drawing import Icon
        form = window.native
        icon = Icon(str(ICON_PATH))
        # WinForms objects must be changed on the window's own thread.
        form.Invoke(Action(lambda: setattr(form, "Icon", icon)))
    except Exception:
        pass


class WindowApi:
    """Called from the page (window.pywebview.api) in the native window."""

    def open_window(self, url: str, title: str = "") -> None:
        """Links that would open a new tab (the invoice PDF) get their own
        window, so the main window keeps its page."""
        import webview
        if not url.startswith(f"http://{HOST}:"):
            return  # only this app's own pages
        w = webview.create_window(title or APP_TITLE, url, width=900, height=1000)
        w.events.shown += lambda: _set_icon(w)


def native_window(url: str) -> None:
    """Show `url` in a native window; returns when the window is closed."""
    import webview

    webview.settings["ALLOW_DOWNLOADS"] = True                   # Download PDF / JSON
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False
    window = webview.create_window(APP_TITLE, url, width=1320, height=880, min_size=(900, 600),
                                   js_api=WindowApi())
    window.events.shown += lambda: _set_icon(window)
    webview.start(gui="edgechromium", private_mode=False,
                  storage_path=str(load_config().data_dir / "webview"))


def main() -> None:
    set_app_id()
    cfg = load_config()

    if already_running(PREFERRED_PORT):
        url = f"http://{HOST}:{PREFERRED_PORT}/"
        try:
            native_window(url)
        except Exception:
            open_window(url, cfg.data_dir / "window-profile")
        return

    try:
        import webview  # noqa: F401
        native = True
    except Exception:
        native = False

    if native:
        server, port = start_server(cfg)
        try:
            native_window(f"http://{HOST}:{port}/")
            return
        except Exception:
            # WebView2 unavailable: fall back to the browser app window below.
            server.shutdown()

    stopping = threading.Event()
    presence = Presence(on_empty=stopping.set)
    server, port = start_server(cfg, presence)
    threading.Thread(target=presence.watch, daemon=True).start()
    open_window(f"http://{HOST}:{port}/", cfg.data_dir / "window-profile")
    stopping.wait()
    server.shutdown()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log = Path(__file__).resolve().parent.parent / "data" / "desktop-error.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(traceback.format_exc(), encoding="utf-8")
        message_box(f"The app could not start.\n\nDetails were saved to:\n{log}")
