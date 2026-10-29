#!/usr/bin/env python3
"""PopUtil's window inside the Flatpak.

PopUtil manages your actual system (apt, /opt, services, other Flatpaks), so its engine has to run outside
the sandbox. This shell copies the engine that ships in the Flatpak to ~/.local/share/poputil/runtime/<version>,
starts it on the host with `flatpak-spawn --host`, and shows its interface in a WebKit window. The host process
stops automatically when the window closes (--watch-bus).
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.request

APP_ID = os.environ.get("FLATPAK_ID", "io.github.Jayswansocool48.PopUtil")
BUNDLED = os.environ.get("POPUTIL_BUNDLED", "/app/share/poputil")
HOME = os.path.expanduser("~")
RUNTIME = os.path.join(HOME, ".local/share/poputil/runtime")


def version():
    try:
        with open(os.path.join(BUNDLED, "VERSION")) as f:
            return f.read().strip()
    except OSError:
        return "dev"


def stage():
    """Copy the bundled engine where the host can run it. Returns its folder. Safe to call every launch."""
    ver = version()
    dst = os.path.join(RUNTIME, ver)
    if not os.path.exists(os.path.join(dst, ".complete")) or ver == "dev":
        tmp = f"{dst}.tmp-{os.getpid()}"
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(BUNDLED, tmp, ignore=shutil.ignore_patterns("__pycache__"))
        with open(os.path.join(tmp, ".complete"), "w") as f:
            f.write(ver)
        shutil.rmtree(dst, ignore_errors=True)
        os.replace(tmp, dst)
    # keep only the version in use
    for old in os.listdir(RUNTIME):
        if old != ver:
            shutil.rmtree(os.path.join(RUNTIME, old), ignore_errors=True)
    return dst


def host_command(engine_dir):
    return ["flatpak-spawn", "--host", "--watch-bus", "--env=POPUTIL_FLATPAK=1", "--env=PYTHONDONTWRITEBYTECODE=1",
            "python3", os.path.join(engine_dir, "poputil.py"), "--no-window"]


def start_engine(engine_dir, timeout=30):
    """Start the engine on the host. Returns (process, url) or raises RuntimeError with a readable reason."""
    try:
        proc = subprocess.Popen(host_command(engine_dir), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except OSError as e:
        raise RuntimeError(f"Couldn't start PopUtil's engine: {e}") from e
    result = {}

    def read_url():
        line = proc.stdout.readline()
        result["url"] = line.strip()

    t = threading.Thread(target=read_url, daemon=True)
    t.start()
    t.join(timeout)
    url = result.get("url", "")
    if not url.startswith("http://127.0.0.1:"):
        err = ""
        if proc.poll() is not None:
            err = (proc.stderr.read() or "").strip().splitlines()[-1:] or [""]
            err = err[0]
        proc.kill()
        if "python3" in err and ("not found" in err or "No such file" in err):
            raise RuntimeError("PopUtil needs Python 3 on your system. Install it with: sudo apt install python3")
        raise RuntimeError(f"PopUtil's engine didn't start. {err}".strip())
    # keep draining output so the engine never blocks on a full pipe
    threading.Thread(target=lambda: [None for _ in proc.stdout], daemon=True).start()
    threading.Thread(target=lambda: [sys.stderr.write(x) for x in proc.stderr], daemon=True).start()
    return proc, url


def engine_busy(url):
    token = url.split("#t=", 1)[-1]
    base = url.split("/#", 1)[0]
    try:
        req = urllib.request.Request(f"{base}/api/log?after=999999999", headers={"X-Token": token})
        with urllib.request.urlopen(req, timeout=2) as r:
            jobs = json.loads(r.read())["jobs"]
        return bool(jobs["current"] or jobs["queued"])
    except (OSError, ValueError, KeyError):
        return False


def error_page(message):
    import html
    return f"""<!doctype html><meta charset="utf-8"><body style="margin:0;display:grid;place-items:center;height:100vh;
      background:#14171c;color:#e6e9ef;font:15px/1.5 system-ui,sans-serif"><div style="max-width:440px;text-align:center">
      <h1 style="font-weight:500;font-size:22px">PopUtil couldn't start</h1><p style="color:#9097a6">{html.escape(message)}</p>
      </div></body>"""


def main():
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("WebKit", "6.0")
    from gi.repository import Gio, GLib, Gtk, WebKit

    state = {"proc": None, "url": None, "closing": False}

    def on_activate(app):
        win = Gtk.ApplicationWindow(application=app, title="PopUtil")
        win.set_default_size(1280, 840)
        view = WebKit.WebView()
        win.set_child(view)
        try:
            state["proc"], state["url"] = start_engine(stage())
            view.load_uri(state["url"])
        except (RuntimeError, OSError) as e:
            view.load_html(error_page(str(e)), "about:blank")

        def on_close(_w):
            if state["closing"] or not state["url"] or not engine_busy(state["url"]):
                stop()
                return False
            dialog = Gtk.AlertDialog(message="PopUtil is still working. Close it anyway?",
                                     detail="Closing now can leave a job half done.", buttons=["Keep it open", "Close"],
                                     cancel_button=0, default_button=0)

            def answered(d, res):
                try:
                    if d.choose_finish(res) == 1:
                        state["closing"] = True
                        win.close()
                except GLib.Error:
                    pass
            dialog.choose(win, None, answered)
            return True

        win.connect("close-request", on_close)
        win.present()

    def stop():
        p = state["proc"]
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()

    app = Gtk.Application(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
    app.connect("activate", on_activate)
    app.connect("shutdown", lambda *_: stop())
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
