#!/usr/bin/env python3
"""PopUtil — a personal control panel for Pop!_OS.

Runs a small local server (127.0.0.1 only, random port, secret token) and opens the interface
in a native WebKitGTK window, or a Chromium-style app window if WebKitGTK isn't installed.
Anything that needs root is batched into one pkexec password prompt per job.

  poputil               open the app
  poputil --no-window   just print the URL
  poputil --browser     use an app/browser window instead of the native one
"""
import argparse
import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from engine import server  # noqa: E402
from engine.jobs import JOBS  # noqa: E402
from engine.util import CONF_DIR, sh, which  # noqa: E402

VERSION = (HERE / "VERSION").read_text().strip() if (HERE / "VERSION").exists() else "dev"


def native_window(url):
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        for v in ("4.1", "4.0"):
            try:
                gi.require_version("WebKit2", v)
                break
            except ValueError:
                continue
        else:
            return False
        from gi.repository import Gtk, WebKit2
    except (ImportError, ValueError):
        return False
    win = Gtk.Window(title="PopUtil")
    win.set_default_size(1280, 840)
    win.set_wmclass("poputil", "PopUtil")
    with contextlib.suppress(Exception):
        win.set_icon_from_file(str(HERE / "ui/icon.svg"))
    view = WebKit2.WebView()
    settings = view.get_settings()
    settings.set_enable_developer_extras(bool(os.environ.get("POPUTIL_DEBUG")))
    view.load_uri(url)
    win.add(view)

    def on_close(*_):
        if JOBS.busy():
            dlg = Gtk.MessageDialog(transient_for=win, modal=True, message_type=Gtk.MessageType.QUESTION,
                                    buttons=Gtk.ButtonsType.YES_NO,
                                    text="PopUtil is still working. Close it anyway?")
            dlg.format_secondary_text("Closing now can leave a job half done.")
            answer = dlg.run()
            dlg.destroy()
            if answer != Gtk.ResponseType.YES:
                return True
        Gtk.main_quit()
        return False

    win.connect("delete-event", on_close)
    win.show_all()
    Gtk.main()
    return True


def app_window(url):
    profile = CONF_DIR / "window"
    for b in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "brave-browser", "microsoft-edge"):
        if which(b):
            subprocess.Popen([b, f"--app={url}", "--window-size=1280,840", f"--user-data-dir={profile}",
                              "--class=PopUtil", "--no-first-run"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    for fp in ("com.google.Chrome", "org.chromium.Chromium", "com.brave.Browser"):
        if which("flatpak") and sh(["flatpak", "info", fp])[0] == 0:
            subprocess.Popen(["flatpak", "run", fp, f"--app={url}", "--window-size=1280,840"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    return subprocess.call(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0


def main():
    ap = argparse.ArgumentParser(description="PopUtil — Pop!_OS control panel")
    ap.add_argument("--no-window", action="store_true", help="don't open a window, just print the URL")
    ap.add_argument("--browser", action="store_true", help="use an app/browser window instead of the native one")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--version", action="version", version=f"PopUtil {VERSION}")
    args = ap.parse_args()
    if os.geteuid() == 0:
        sys.exit("Run PopUtil as your normal user. It asks for your password only when a step needs it.")

    _srv, url = server.start(args.port)
    if args.no_window:
        print(url, flush=True)
    elif not args.browser and native_window(url):
        return
    elif not app_window(url):
        print(f"Open this in a browser: {url}")

    # Browser windows can't tell us they closed: exit once the page stops checking in and nothing is running.
    try:
        while True:
            time.sleep(5)
            t = server.LAST_PING["t"]
            if not args.no_window and t and time.time() - t > 30 and not JOBS.busy():
                break
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
