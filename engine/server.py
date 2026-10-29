"""Local HTTP API for the PopUtil window. Listens on 127.0.0.1 only and requires a per-launch token."""
import json
import mimetypes
import os
import re
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import appremove, appscan, catalog, features, migrate, organizer, system
from .jobs import JOBS
from .util import ai_args, load_conf, save_conf

UI = Path(__file__).resolve().parent.parent / "ui"
STATIC = {"/": "index.html", "/index.html": "index.html", "/app.css": "app.css", "/app.js": "app.js",
          "/icon.svg": "icon.svg"}
TOKEN = secrets.token_urlsafe(24)
LAST_PING = {"t": None}

# Apps found by the last scan (kept in memory for planning removals, saved to disk between launches)
APPS = {"when": None, "apps": [], "lock": threading.Lock()}


def _load_scan():
    when, apps = appscan.load()
    APPS.update(when=when, apps=apps)


def apps_state():
    with APPS["lock"]:
        apps = [a.public() for a in APPS["apps"]]
    removed = appremove.runs()
    return {"scanned": APPS["when"], "apps": apps, "groups": appscan.GROUP_ORDER, "removed": removed,
            "held_bytes": sum(r["held_bytes"] for r in removed), "ai": ai_args().ai}


def _scan_job():
    apps = appscan.scan()
    # keep earlier AI advice for apps that are still here
    old = {a.id: a.ai for a in APPS["apps"] if a.ai}
    for a in apps:
        a.ai = old.get(a.id, {})
    with APPS["lock"]:
        APPS.update(when=time.time(), apps=apps)
    appscan.save(apps)


def _remove_job(internal, title):
    def run():
        ok = appremove.execute(internal, title)
        done = {i["app"]["id"] for i in internal}
        with APPS["lock"]:
            APPS["apps"] = [a for a in APPS["apps"] if a.id not in done]
            appscan.save(APPS["apps"])
        return ok
    return run


def _ai_job(ids):
    def run():
        args = ai_args()
        if args.ai == "none":
            print("Add a Claude API key or an Ollama model in Settings to get AI advice.")
            return False
        with APPS["lock"]:
            chosen = [a for a in APPS["apps"] if not ids or a.id in ids][:300]
        appscan.ai_review(chosen, args)
        with APPS["lock"]:
            appscan.save(APPS["apps"])
    return run


def settings_public():
    conf = load_conf()
    return {k: v for k, v in conf.items() if k != "anthropic_key"} | {"has_key": bool(conf.get("anthropic_key")),
                                                                     "cosmic_accent": system.cosmic_accent(),
                                                                     "flatpak": bool(os.environ.get("POPUTIL_FLATPAK"))}


def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


class Handler(BaseHTTPRequestHandler):
    server_version = "PopUtil"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", cache=False):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self):
        return (self.headers.get("Host") or "").split(":")[0] in ("127.0.0.1", "localhost")

    def _auth(self):
        return self._host_ok() and secrets.compare_digest(self.headers.get("X-Token", ""), TOKEN)

    # ── GET ──
    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden"})
        u = urlparse(self.path)
        if u.path in STATIC:
            f = UI / STATIC[u.path]
            ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            return self._send(200, f.read_bytes(), f"{ctype}; charset=utf-8" if ctype.startswith("text") or ctype.endswith("javascript") else ctype)
        if re.fullmatch(r"/fonts/[\w.-]+\.woff2", u.path):
            f = UI / u.path.lstrip("/")
            if f.is_file():
                return self._send(200, f.read_bytes(), "font/woff2", cache=True)
            return self._send(404, {"error": "not found"})
        q = parse_qs(u.query)
        if u.path == "/api/appicon":
            # icons are served by app id only, never by path, so this can't read arbitrary files
            if q.get("t", [""])[0] != TOKEN:
                return self._send(401, {"error": "bad token"})
            aid = q.get("id", [""])[0]
            with APPS["lock"]:
                icon = next((a.icon for a in APPS["apps"] if a.id == aid), "")
            if icon and icon.endswith((".png", ".svg")) and Path(icon).is_file() and Path(icon).stat().st_size < 4_000_000:
                return self._send(200, Path(icon).read_bytes(), "image/svg+xml" if icon.endswith(".svg") else "image/png", cache=True)
            return self._send(404, {"error": "no icon"})
        if not self._auth():
            return self._send(401, {"error": "bad token"})
        routes = {
            "/api/sysinfo": lambda: system.sysinfo() | {"migration": migrate.last_report()}, "/api/live": system.live, "/api/tweaks": system.tweaks_state,
            "/api/tasks": system.tasks_state, "/api/catalog": catalog.state, "/api/clean": features.clean_state,
            "/api/organize": features.org_state, "/api/apps": apps_state, "/api/settings": settings_public,
        }
        if u.path == "/api/log":
            LAST_PING["t"] = time.time()
            after = int(q.get("after", ["0"])[0] or 0)
            return self._send(200, {"lines": JOBS.since(after), "jobs": JOBS.status()})
        fn = routes.get(u.path)
        if not fn:
            return self._send(404, {"error": "not found"})
        try:
            return self._send(200, fn())
        except Exception as e:  # noqa: BLE001 — report instead of dropping the connection
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})

    # ── POST ──
    def do_POST(self):
        if not self._auth():
            return self._send(401, {"error": "bad token"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(min(length, 1_000_000)) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad request"})
        try:
            res = self.post(urlparse(self.path).path, body)
        except (KeyError, ValueError) as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})
        return self._send(400 if "error" in res else 200, res)

    def post(self, path, b):
        ids = [str(i) for i in b.get("ids", [])][:500]
        if path == "/api/apps/scan":
            return {"job": JOBS.submit("Scanning your PC for apps", [("py", _scan_job)], "apps", "Scan finished")}
        if path == "/api/apps/plan":
            with APPS["lock"]:
                apps = list(APPS["apps"])
            plans, _ = appremove.plan(apps, ids, set(map(str, b.get("with_data", []))))
            return {"plans": plans}
        if path == "/api/apps/remove":
            with APPS["lock"]:
                apps = list(APPS["apps"])
            plans, internal = appremove.plan(apps, ids, set(map(str, b.get("with_data", []))))
            if not internal:
                return {"error": "Nothing in that selection can be removed safely."}
            names = [i["app"]["name"] for i in internal]
            title = f"Removing {names[0]}" if len(names) == 1 else f"Removing {_plural(len(names), 'app')}"
            done = f"Removed {names[0]}" if len(names) == 1 else f"Removed {_plural(len(names), 'app')}"
            return {"job": JOBS.submit(title, [("py", _remove_job(internal, title))], "apps", done),
                    "skipped": [p["name"] for p in plans if p["blocked"]]}
        if path == "/api/apps/restore":
            rid = str(b.get("id", ""))
            return {"job": JOBS.submit("Restoring removed apps", [("py", lambda: appremove.restore(rid))], "removed",
                                       "Restored")}
        if path == "/api/apps/purge":
            rids = [r["id"] for r in appremove.runs() if r["state"] == "held"] if b.get("all") else [str(b.get("id", ""))]
            steps = [("py", (lambda r=r: appremove.delete_forever(r))) for r in rids]
            if not steps:
                return {"error": "Removed is already empty."}
            return {"job": JOBS.submit("Emptying Removed", steps, "removed", "Removed emptied")}
        if path == "/api/apps/reveal":
            with APPS["lock"]:
                app = next((a for a in APPS["apps"] if a.id == str(b.get("id"))), None)
            if not app or not app.location:
                return {"error": "PopUtil doesn't know where that one lives."}
            target = app.location if os.path.isdir(app.location) else os.path.dirname(app.location)
            if not os.path.isdir(target):
                return {"error": f"{target} doesn't exist anymore."}
            subprocess.Popen(["xdg-open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            return {"ok": True}
        if path == "/api/apps/ai":
            return {"job": JOBS.submit("Asking AI about your apps", [("py", _ai_job(set(ids)))], "apps", "AI advice ready")}
        if path in ("/api/catalog/install", "/api/catalog/remove"):
            remove = path.endswith("remove")
            chosen, steps = catalog.steps(ids, remove)
            if not steps:
                return {"error": "Nothing selected."}
            names = ", ".join(a["name"] for a in chosen[:3]) + (f" and {len(chosen) - 3} more" if len(chosen) > 3 else "")
            verb = ("Uninstalling", "Uninstalled") if remove else ("Installing", "Installed")
            return {"job": JOBS.submit(f"{verb[0]} {names}", steps, "catalog", f"{verb[1]} {names}")}
        if path == "/api/tweak":
            t = system.TWEAK_BY_ID[b["id"]]
            steps = t["on"] if b.get("on") else t["off"]
            if not steps:
                return {"error": "This one can't be turned off here."}
            word = "on" if b.get("on") else "off"
            return {"job": JOBS.submit(f"Turning {word} {t['name']}", steps, "tweaks", f"{t['name']} is {word}")}
        if path == "/api/power":
            steps = system.power_steps(b.get("value"))
            if not steps:
                return {"error": "Power profiles aren't available."}
            return {"job": JOBS.submit(f"Switching to {b['value']}", steps, "tweaks", "Power profile changed")}
        if path == "/api/task":
            t = system.TASK_BY_ID[b["id"]]
            return {"job": JOBS.submit(t["name"], t["steps"], "tasks", f"{t['name']}: done")}
        if path == "/api/clean/scan":
            return {"job": JOBS.submit("Looking for disk clutter", [("py", features.clean_scan)], "clean", "Clutter scan finished")}
        if path == "/api/clean/apply":
            chosen, steps = features.clean_steps([int(i) for i in ids if i.isdigit()])
            if not chosen:
                return {"error": "Nothing selected."}
            return {"job": JOBS.submit(f"Clearing {_plural(len(chosen), 'item')}", steps, "clean",
                                       f"Cleared {_plural(len(chosen), 'item')}")}
        if path == "/api/organize/scan":
            return {"job": JOBS.submit("Planning how to organize your files",
                                       [("py", features.org_scan(str(b.get("instructions", ""))[:2000]))], "organize", "Plan ready")}
        if path == "/api/organize/apply":
            chosen, steps = features.org_apply_steps([int(i) for i in ids if i.isdigit()])
            if not chosen:
                return {"error": "Nothing selected."}
            return {"job": JOBS.submit(f"Organizing {_plural(len(chosen), 'item')}", steps, "organize",
                                       f"Organized {_plural(len(chosen), 'item')}")}
        if path == "/api/organize/undo":
            rid = str(b.get("id", ""))
            if not re.fullmatch(r"\d{8}-\d{6}", rid):
                return {"error": "Unknown run."}
            return {"job": JOBS.submit("Undoing an organize run", [("py", lambda: organizer.undo(rid))], "organize", "Undone")}
        if path == "/api/settings":
            conf = load_conf()
            for k in ("ai", "model", "ollama_host", "min_size", "large", "old_days", "theme", "match_cosmic"):
                if k in b:
                    conf[k] = b[k]
            if b.get("anthropic_key") is not None:
                if b["anthropic_key"]:
                    conf["anthropic_key"] = str(b["anthropic_key"]).strip()
                else:
                    conf.pop("anthropic_key", None)
            save_conf(conf)
            return {"ok": True}
        raise KeyError(f"unknown action {path}")


def start(port=0):
    if os.environ.get("POPUTIL_FLATPAK"):
        threading.Thread(target=migrate.cleanup, daemon=True, name="migrate").start()
    _load_scan()
    system.cpu_percent()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True, name="http").start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/#t={TOKEN}"
