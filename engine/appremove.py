"""appremove — remove apps found by appscan safely, keep them restorable, and clean up after them.

Every removal is planned first (nothing changes while planning):
  * system packages are simulated with apt first; anything that would drag Pop!_OS parts along is blocked
  * apps that are open right now are blocked until you close them
  * files are never deleted: they move to a "Removed" holding area (yours in ~/.local/share/poputil/removed,
    system ones in /var/lib/poputil/removed) and can be restored until you empty it
  * launchers, command links, background services and shell-startup lines that belong to the app go with it,
    and the app library, systemd and icon caches are refreshed afterwards
"""
import contextlib
import json
import os
import re
import shutil
import time

from . import appscan
from .jobs import JOBS
from .organizer import move_path
from .util import DATA_DIR, H, inside, q, read, running_inside, sh, tilde, which

REMOVED = DATA_DIR / "removed"
ROOT_REMOVED = "/var/lib/poputil/removed"
RUN_ID = re.compile(r"^\d{8}-\d{6}(-\d+)?$")

USER_PROTECT = {H, f"{H}/.local", f"{H}/.local/share", f"{H}/.local/bin", f"{H}/.local/lib", f"{H}/.config",
                f"{H}/.cache", f"{H}/.var", f"{H}/.var/app", f"{H}/.ssh", f"{H}/.gnupg", f"{H}/Desktop",
                f"{H}/Documents", f"{H}/Downloads", f"{H}/Pictures", f"{H}/Videos", f"{H}/Music", f"{H}/snap",
                f"{H}/.local/share/applications", f"{H}/.local/share/icons", f"{H}/.config/systemd",
                f"{H}/.config/systemd/user", str(DATA_DIR)}
ROOT_PREFIXES = ("/opt/", "/usr/local/", "/etc/systemd/system/", "/usr/bin/", "/usr/sbin/", "/usr/games/",
                 "/usr/share/")
ROOT_PROTECT = {"/opt", "/usr/local", "/usr/local/bin", "/usr/local/sbin", "/usr/local/lib", "/usr/local/share",
                "/usr/local/etc", "/usr/local/include", "/usr/local/games", "/usr/local/src", "/etc/systemd/system",
                "/usr/bin", "/usr/sbin", "/usr/games", "/usr/share", "/usr/share/applications", "/usr/share/icons"}
PACKAGE_TYPES = {"apt", "flatpak", "snap", "pip", "pipx", "uv", "cargo", "npm", "steam"}
DATA_ROLES = {"data", "settings", "cache", "models"}


# ───────────────────────── safety checks ─────────────────────────

def drive_mounts():
    with contextlib.suppress(Exception):
        return appscan.drive_roots()
    return []


def path_problem(path, root):
    """Why a path must not be moved, or '' if it's fine."""
    p = os.path.normpath(path)
    if p in USER_PROTECT or p in ROOT_PROTECT or p == "/":
        return f"{tilde(p)} is a system or personal folder that always stays"
    if inside(p, H):
        if root:
            return "unexpected permissions on a file in your home folder"
        return ""
    if any(inside(p, m) and p != m for m in drive_mounts()):
        return ""
    if not p.startswith(ROOT_PREFIXES):
        return f"{tilde(p)} is outside the places PopUtil is allowed to change"
    return ""


def dpkg_owned(paths):
    paths = [p for p in paths if not inside(p, H)]
    if not paths:
        return set()
    _, out = sh(["dpkg-query", "-S", *paths], timeout=30)
    owned = set()
    for line in out.splitlines():
        if ": " in line:
            owned.add(line.split(": ", 1)[1].strip())
    return {p for p in paths if p in owned}


def unit_state(unit, system):
    scope = [] if system else ["--user"]
    en = sh(["systemctl", *scope, "is-enabled", unit], timeout=10)[1].strip()
    act = sh(["systemctl", *scope, "is-active", unit], timeout=10)[1].strip()
    return en == "enabled", act == "active"


def apt_simulate(pkgs):
    rc, out = sh(["apt-get", "-s", "remove", *pkgs], timeout=60)
    if rc != 0:
        return None, out.strip().splitlines()[-1:] or ["apt couldn't plan this removal"]
    return re.findall(r"^Remv (\S+)", out, re.M), []


def protected_pkgs(names):
    if not names:
        return []
    _, out = sh(["dpkg-query", "-W", "-f=${Package}\t${Priority}\t${Essential}\n", *names], timeout=30)
    bad = []
    for line in out.splitlines():
        f = line.split("\t")
        if len(f) == 3 and (f[2] == "yes" or f[1] in ("required", "important") or appscan.PROTECT.match(f[0])):
            bad.append(f[0])
    return bad


def apt_candidate(pkg):
    _, out = sh(["apt-cache", "policy", pkg], timeout=20)
    m = re.search(r"Candidate: (\S+)", out)
    return bool(m and m.group(1) != "(none)")


def flatpak_running():
    if not which("flatpak"):
        return set()
    _, out = sh(["flatpak", "ps", "--columns=application"], timeout=10)
    return {line.strip() for line in out.splitlines() if line.strip()}


# ───────────────────────── planning ─────────────────────────

def _move_parts(app, with_data):
    parts = []
    mtype = app.method.get("type")
    for p in app.parts:
        if not os.path.lexists(p.path):
            continue
        if p.role in DATA_ROLES and not with_data:
            continue
        if mtype in PACKAGE_TYPES and p.role not in DATA_ROLES:
            # the package manager removes its own files; we only take launchers/icons someone added by hand
            if not (inside(p.path, H) and p.role in ("launcher", "icon")) or mtype in ("steam",):
                continue
        if mtype == "steam" and p.role in DATA_ROLES:
            continue  # Steam removes compatdata/shader cache itself
        parts.append(p)
    # remove nested parts (a launcher inside a folder we're already moving)
    parts.sort(key=lambda p: len(p.path))
    kept = []
    for p in parts:
        if not any(inside(p.path, k.path) for k in kept):
            kept.append(p)
    return kept


def plan_app(app, with_data):
    if app.kind == "leftover":
        with_data = True  # the data is the whole item
    pl = {"id": app.id, "name": app.name, "source_label": appscan.SOURCES[app.source][1], "has_icon": bool(app.icon),
          "blocked": "", "warnings": [], "steps": [], "commands": [], "frees": 0, "held": 0, "undo": "",
          "with_data": with_data, "data_size": app.data_size}
    m = app.method
    mtype = m.get("type")
    if app.protected:
        pl["blocked"] = app.protected
        return pl, None
    if mtype == "external":
        pl["blocked"] = m.get("hint", "Remove it with the app that installed it.")
        return pl, None
    # open right now?
    appscan.mark_running([app])
    if mtype == "flatpak":
        app.running = m["id"] in flatpak_running()
    has_service = any(p.role == "service" for p in app.parts)
    progs = [p.path for p in app.parts if p.role == "program" and os.path.lexists(p.path)]
    if (app.running or any(running_inside(p) for p in progs)) and not has_service:
        pl["blocked"] = "It's open right now. Close it first, then try again."
        return pl, None

    actions = []
    parts = _move_parts(app, with_data)
    # paths we'd move: safety + ownership check
    root_paths = [p.path for p in parts if p.root]
    owned = dpkg_owned(root_paths)
    for p in parts:
        why = path_problem(p.path, p.root)
        if not why and p.path in owned:
            why = f"{tilde(p.path)} belongs to a system package"
        if why:
            if p.role in DATA_ROLES or p.role in ("launcher", "icon", "command"):
                pl["warnings"].append(f"Leaving {tilde(p.path)} alone: {why}.")
                continue
            pl["blocked"] = f"Can't remove it safely: {why}."
            return pl, None

    # manager-specific step first
    if mtype == "apt":
        pkgs = m["pkgs"]
        removed, err = apt_simulate(pkgs)
        if removed is None:
            pl["blocked"] = f"apt can't remove it: {err[0] if err else 'unknown error'}"
            return pl, None
        bad = protected_pkgs(removed)
        if bad:
            pl["blocked"] = (f"Removing it would also remove {', '.join(bad[:3])}, which Pop!_OS needs. "
                             "PopUtil won't do that.")
            return pl, None
        extra = [r for r in removed if r not in pkgs]
        if len(extra) > 15:
            pl["blocked"] = f"Removing it would also remove {len(extra)} other packages that depend on it."
            return pl, None
        if extra:
            pl["warnings"].append(f"Also removes {', '.join(extra)}, which depend on it.")
        pl["steps"].append(f"Uninstall the package{'s' if len(removed) > 1 else ''} with apt")
        pl["commands"].append(f"sudo apt-get remove -y {' '.join(removed)}")
        pl["frees"] += app.size
        reinstall = f"apt-get install -y {' '.join(q(x) for x in pkgs)}" if all(apt_candidate(x) for x in pkgs) else ""
        if app.source == "deb" and not reinstall:
            pl["warnings"].append("This came from a .deb file. To get it back later you'll need that file again.")
        actions.append({"t": "pkg", "mgr": "apt", "root": True, "cmd": f"apt-get remove -y {' '.join(q(x) for x in removed)}",
                        "check": removed, "reinstall": reinstall, "reinstall_root": True})
        pl["undo"] = "Restore reinstalls it from the internet." if reinstall else ""
    elif mtype == "flatpak":
        flag = "--user" if m.get("installation") == "user" else "--system"
        cmd = f"flatpak uninstall -y --noninteractive {flag} {q(m['id'])}"
        pl["steps"].append("Uninstall the Flatpak")
        pl["commands"].append(cmd)
        pl["frees"] += app.size
        actions.append({"t": "pkg", "mgr": "flatpak", "root": False, "cmd": cmd, "id": m["id"],
                        "reinstall": f"flatpak install -y --noninteractive {flag} {q(m.get('origin') or 'flathub')} {q(m['id'])}",
                        "reinstall_root": False})
        pl["undo"] = "Restore reinstalls it from Flathub."
    elif mtype == "snap":
        cmd = f"snap remove {q(m['name'])}"
        pl["steps"].append("Uninstall the Snap (Snap keeps a snapshot of its data for 31 days)")
        pl["commands"].append(f"sudo {cmd}")
        pl["frees"] += app.size
        actions.append({"t": "pkg", "mgr": "snap", "root": True, "cmd": cmd,
                        "reinstall": f"snap install {q(m['name'])}", "reinstall_root": True})
        pl["undo"] = "Restore reinstalls it from the Snap Store."
    elif mtype == "pip":
        cmd = f"python3 -m pip uninstall -y --break-system-packages {q(m['name'])}"
        pl["steps"].append("Uninstall it with pip")
        pl["commands"].append(("sudo " if m.get("root") else "") + cmd)
        pl["frees"] += app.size
        ver = f"=={m['version']}" if m.get("version") else ""
        actions.append({"t": "pkg", "mgr": "pip", "root": bool(m.get("root")), "cmd": cmd,
                        "reinstall": f"python3 -m pip install {'' if m.get('root') else '--user '}--break-system-packages {q(m['name'] + ver)}",
                        "reinstall_root": bool(m.get("root"))})
        if app.notes and "need this" in " ".join(app.notes):
            pl["warnings"].append("Other Python packages you installed need this one.")
        pl["undo"] = "Restore reinstalls the same version."
    elif mtype in ("pipx", "uv"):
        tool = "pipx" if mtype == "pipx" else "uv"
        if not which(tool):
            mtype = "files"  # tool itself is gone: fall back to moving the files
        else:
            cmd = f"pipx uninstall {q(m['name'])}" if tool == "pipx" else f"uv tool uninstall {q(m['name'])}"
            pl["steps"].append(f"Uninstall it with {tool}")
            pl["commands"].append(cmd)
            pl["frees"] += app.size
            actions.append({"t": "pkg", "mgr": tool, "root": False, "cmd": cmd,
                            "reinstall": f"pipx install {q(m['name'])}" if tool == "pipx" else f"uv tool install {q(m['name'])}",
                            "reinstall_root": False})
            pl["undo"] = "Restore reinstalls it."
    elif mtype == "cargo":
        cargo = which("cargo") or f"{H}/.cargo/bin/cargo"
        if not os.path.exists(cargo):
            mtype = "files"
        else:
            cmd = f"{q(cargo)} uninstall {q(m['name'])}"
            pl["steps"].append("Uninstall it with cargo")
            pl["commands"].append(f"cargo uninstall {m['name']}")
            pl["frees"] += app.size
            actions.append({"t": "pkg", "mgr": "cargo", "root": False, "cmd": cmd,
                            "reinstall": f"{q(cargo)} install {q(m['name'])} --version {q(m.get('version', ''))}",
                            "reinstall_root": False})
            pl["undo"] = "Restore rebuilds it with cargo."
    elif mtype == "npm":
        cmd = f"npm uninstall -g {q(m['name'])}"
        pl["steps"].append("Uninstall it with npm")
        pl["commands"].append(("sudo " if m.get("root") else "") + cmd)
        pl["frees"] += app.size
        actions.append({"t": "pkg", "mgr": "npm", "root": bool(m.get("root")), "cmd": cmd,
                        "reinstall": f"npm install -g {q(m['name'] + ('@' + m['version'] if m.get('version') else ''))}",
                        "reinstall_root": bool(m.get("root"))})
        pl["undo"] = "Restore reinstalls the same version."
    elif mtype == "steam":
        cmd = f"xdg-open steam://uninstall/{q(m['appid'])}"
        pl["steps"].append("Ask Steam to uninstall it (Steam shows its own confirmation)")
        pl["commands"].append(cmd)
        actions.append({"t": "cmd", "root": False, "cmd": cmd})
        pl["undo"] = "Reinstall it from your Steam library."

    if mtype == "files":
        parts = [p for p in app.parts if os.path.lexists(p.path) and (with_data or p.role not in DATA_ROLES)]
        parts.sort(key=lambda p: len(p.path))
        parts = [p for i, p in enumerate(parts) if not any(inside(p.path, k.path) for k in parts[:i])]
        for p in parts:
            if path_problem(p.path, p.root):
                pl["blocked"] = f"Can't remove it safely: {path_problem(p.path, p.root)}."
                return pl, None
        owned = dpkg_owned([p.path for p in parts if p.root])
        if owned:
            pl["blocked"] = f"{tilde(sorted(owned)[0])} belongs to a system package. Remove that package instead."
            return pl, None
    else:
        parts = [p for p in parts if not path_problem(p.path, p.root) and p.path not in owned]

    # services: stop and turn off before moving their files
    for p in parts:
        if p.role == "service":
            system = p.root
            unit = os.path.basename(p.path)
            enabled, active = unit_state(unit, system)
            scope = "" if system else "--user "
            actions.append({"t": "unit", "root": system, "unit": unit, "enabled": enabled, "active": active,
                            "cmd": f"systemctl {scope}disable --now {q(unit)} 2>/dev/null || true"})
            pl["steps"].append(f"Stop the background service {unit}" + (" and stop it starting at boot" if enabled else ""))
    for p in parts:
        actions.append({"t": "move", "root": p.root, "src": p.path, "bytes": p.size, "role": p.role})
        pl["held"] += p.size
    moved_roles = {p.role for p in parts}
    labels = {"program": "the app's files", "command": "its commands", "launcher": "its app-library launcher",
              "service": "its service files", "icon": "its icons", "shortcut": "its Windows shortcuts",
              "data": "its data", "settings": "its settings", "cache": "its cache"}
    if parts:
        what = [labels[r] for r in ("program", "command", "launcher", "service", "icon", "shortcut", "settings", "data", "cache")
                if r in moved_roles]
        pl["steps"].append("Move " + ", ".join(what) + " to Removed (you can restore them)")
        for p in parts:
            pl["commands"].append(f"{'sudo ' if p.root else ''}mv {tilde(p.path)} → Removed")
    shell = m.get("shell")
    if shell:
        lines = appscan.shell_lines(shell)
        if lines:
            actions.append({"t": "shell", "root": False, "patterns": shell})
            pl["steps"].append(f"Turn off {len(lines)} line{'s' if len(lines) > 1 else ''} in your shell startup files that load it")
            for f, n, line in lines[:6]:
                pl["commands"].append(f"# {tilde(f)}:{n}  {line.strip()[:80]}")
    if not actions:
        pl["blocked"] = "Nothing of it is left on disk."
        return pl, None
    if not pl["undo"]:
        pl["undo"] = "Restore puts everything back where it was."
    if with_data and app.data_size:
        sens = [p.note for p in app.parts if p.role in DATA_ROLES and p.note]
        if sens:
            pl["warnings"].append(sens[0] + ". It goes to Removed too, so you can still restore it.")
    elif app.data_size and not with_data:
        pl["warnings"].append(f"Keeps its settings and data ({appscan.human(app.data_size)}) in case you reinstall it.")
    if any(a["t"] == "move" and a["root"] for a in actions) or any(a.get("root") for a in actions if a["t"] in ("pkg", "unit")):
        pl["needs_password"] = True
    return pl, {"app": {"id": app.id, "name": app.name, "source": app.source, "icon": app.icon, "kind": app.kind},
                "actions": actions}


def plan(apps, ids, with_data_ids):
    by_id = {a.id: a for a in apps}
    plans, internal = [], []
    for i in ids:
        a = by_id.get(i)
        if not a:
            continue
        pl, inner = plan_app(a, i in with_data_ids)
        plans.append(pl)
        if inner:
            internal.append(inner)
    return plans, internal


# ───────────────────────── running a removal ─────────────────────────

def _new_run_id():
    rid = time.strftime("%Y%m%d-%H%M%S")
    n = 1
    while (REMOVED / f"{rid}.json").exists():
        n += 1
        rid = f"{time.strftime('%Y%m%d-%H%M%S')}-{n}"
    return rid


def _mount_of(path):
    path = os.path.realpath(os.path.dirname(path) if not os.path.isdir(path) else path)
    dev = os.stat(path).st_dev
    while path != "/":
        parent = os.path.dirname(path)
        if os.stat(parent).st_dev != dev:
            return path
        path = parent
    return "/"


def _user_hold_dir(src, rid):
    """Somewhere on the same drive as src, so moving is instant."""
    base = REMOVED / rid
    with contextlib.suppress(OSError):
        REMOVED.mkdir(parents=True, exist_ok=True)
        if os.lstat(src).st_dev != os.stat(REMOVED).st_dev:
            mount = _mount_of(src)
            if os.access(mount, os.W_OK):
                return os.path.join(mount, ".poputil-removed", rid)
    return str(base)


class Run:
    def __init__(self, title):
        self.id = _new_run_id()
        REMOVED.mkdir(parents=True, exist_ok=True)
        self.dir = REMOVED / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.n = 0
        self.data = {"id": self.id, "when": time.time(), "title": title, "state": "held", "apps": []}
        self.save()

    def save(self):
        tmp = REMOVED / f"{self.id}.json.tmp"
        tmp.write_text(json.dumps(self.data, indent=1))
        tmp.replace(REMOVED / f"{self.id}.json")

    def slot(self, src, hold_dir):
        self.n += 1
        return os.path.join(hold_dir, f"{self.n:03d}__{os.path.basename(src.rstrip('/'))}")


def _comment_out(run, patterns, rec):
    rx = re.compile("|".join(patterns))
    for f in appscan.SHELL_FILES:
        f = os.path.expanduser(f)
        text = read(f)
        if not text:
            continue
        lines = text.split("\n")
        changed = False
        for i, line in enumerate(lines):
            if rx.search(line) and not line.lstrip().startswith("#"):
                lines[i] = f"# disabled by PopUtil: {line}"
                changed = True
        if changed:
            backup = run.dir / f"shell-{os.path.basename(f)}.bak"
            shutil.copy2(f, backup)
            with open(f, "w") as fh:
                fh.write("\n".join(lines))
            rec["actions"].append({"t": "edit", "path": f, "backup": str(backup)})
            print(f"   turned off its lines in {tilde(f)}")


def execute(internal, title):
    """Runs inside a job. Removes every planned app, journaling each change so it can be restored."""
    run = Run(title)
    print(f"Holding area for this removal: {tilde(run.dir)}")
    any_failed = False
    root_dir = f"{ROOT_REMOVED}/{run.id}"
    recs = []
    root_cmds, root_moves, root_pkgs, root_units = [], [], [], []
    touched = {"user_units": False, "system_units": False, "user_launchers": False, "system_launchers": False,
               "user_icons": False}

    # 1) work out every step, then do all password-needing ones in one prompt
    for item in internal:
        rec = {"app": item["app"], "actions": [], "ok": True}
        recs.append(rec)
        for a in item["actions"]:
            if a["t"] == "unit" and a["root"]:
                root_cmds.append(a["cmd"])
                root_units.append((rec, a))
            elif a["t"] == "pkg" and a["root"]:
                root_cmds.append(a["cmd"])
                root_pkgs.append((rec, a))
            elif a["t"] == "move" and a["root"]:
                dst = run.slot(a["src"], root_dir)
                root_cmds.append(f"install -d -m 755 {q(root_dir)} && mv -T -- {q(a['src'])} {q(dst)}")
                root_moves.append((rec, a, dst))
    if root_cmds:
        print("Doing the steps that need your password…")
        rc = JOBS._run_root(root_cmds)
        if rc != 0:
            print("Some steps that needed your password didn't run. Checking what did…")
    for rec, a in root_units:
        rec["actions"].append({"t": "unit", "root": True, "unit": a["unit"], "enabled": a["enabled"], "active": a["active"]})
        touched["system_units"] = True
    for rec, a in root_pkgs:
        ok = _pkg_gone(a)
        rec["actions"].append({"t": "pkg", "mgr": a["mgr"], "ok": ok, "reinstall": a["reinstall"],
                               "reinstall_root": a["reinstall_root"]})
        rec["ok"] &= ok
    for rec, a, dst in root_moves:
        if os.path.lexists(dst) and not os.path.lexists(a["src"]):
            rec["actions"].append({"t": "move", "root": True, "src": a["src"], "dst": dst, "bytes": a["bytes"]})
            touched["system_launchers"] |= a["role"] == "launcher"
            touched["system_units"] |= a["role"] == "service"
        else:
            rec["ok"] = False
            print(f"   couldn't move {tilde(a['src'])}")
    run.data["apps"] = recs
    run.save()

    # 2) everything that runs as you, app by app
    for item, rec in zip(internal, recs):
        name = item["app"]["name"]
        print(f"Removing {name}…")
        for a in item["actions"]:
            try:
                if a["t"] == "unit" and not a["root"]:
                    JOBS.emit(f"$ {a['cmd']}", "cmd")
                    JOBS._stream(["/bin/bash", "-c", a["cmd"]])
                    rec["actions"].append({"t": "unit", "root": False, "unit": a["unit"], "enabled": a["enabled"],
                                           "active": a["active"]})
                    touched["user_units"] = True
                elif a["t"] == "pkg" and not a["root"]:
                    JOBS.emit(f"$ {a['cmd']}", "cmd")
                    JOBS._stream(["/bin/bash", "-c", a["cmd"]])
                    ok = _pkg_gone(a)
                    rec["actions"].append({"t": "pkg", "mgr": a["mgr"], "ok": ok, "reinstall": a["reinstall"],
                                           "reinstall_root": a["reinstall_root"]})
                    if not ok:
                        rec["ok"] = False
                        print(f"   {a['mgr']} didn't remove it")
                elif a["t"] == "cmd":
                    JOBS.emit(f"$ {a['cmd']}", "cmd")
                    JOBS._stream(["/bin/bash", "-c", a["cmd"]])
                    rec["actions"].append({"t": "cmd", "cmd": a["cmd"]})
                elif a["t"] == "move" and not a["root"]:
                    if not os.path.lexists(a["src"]):
                        continue
                    if running_inside(a["src"]) and a["role"] == "program":
                        raise OSError("it's open right now")
                    hold = _user_hold_dir(a["src"], run.id)
                    os.makedirs(hold, exist_ok=True)
                    dst = run.slot(a["src"], hold)
                    move_path(a["src"], dst)
                    rec["actions"].append({"t": "move", "root": False, "src": a["src"], "dst": dst, "bytes": a["bytes"]})
                    touched["user_launchers"] |= a["role"] == "launcher"
                    touched["user_icons"] |= a["role"] == "icon"
                    touched["user_units"] |= a["role"] == "service"
                    print(f"   moved {tilde(a['src'])}")
                elif a["t"] == "shell":
                    _comment_out(run, a["patterns"], rec)
            except Exception as e:  # noqa: BLE001 — record and keep going with the rest
                rec["ok"] = False
                print(f"   couldn't finish one step for {name}: {e}")
            run.save()
        any_failed |= not rec["ok"]

    # 3) refresh what the system remembers
    post = []
    if touched["user_units"]:
        post.append("systemctl --user daemon-reload")
    if touched["user_launchers"] and which("update-desktop-database"):
        post.append(f"update-desktop-database {q(H + '/.local/share/applications')}")
    if touched["user_icons"] and which("gtk-update-icon-cache") and os.path.exists(f"{H}/.local/share/icons/hicolor/index.theme"):
        post.append(f"gtk-update-icon-cache -q -t {q(H + '/.local/share/icons/hicolor')}")
    for c in post:
        JOBS.emit(f"$ {c}", "cmd")
        JOBS._stream(["/bin/bash", "-c", c + " 2>/dev/null || true"])
    root_post = []
    if touched["system_units"]:
        root_post.append("systemctl daemon-reload")
    if touched["system_launchers"]:
        root_post.append("update-desktop-database /usr/share/applications 2>/dev/null || true")
    if root_post:
        JOBS._run_root(root_post)
    run.data["held_bytes"] = sum(a.get("bytes", 0) for r in recs for a in r["actions"] if a["t"] == "move")
    run.save()
    done = sum(1 for r in recs if r["ok"])
    print(f"Removed {done} of {len(recs)}. Anything moved is in Removed until you empty it.")
    return not any_failed


def _pkg_gone(a):
    mgr = a["mgr"]
    if mgr == "apt":
        _, out = sh(["dpkg-query", "-W", "-f=${Package} ${db:Status-Abbrev}\n", *a["check"]], timeout=30)
        return not any(line.split()[1:2] == ["ii"] for line in out.splitlines() if line.strip())
    if mgr == "flatpak":
        _, out = sh(["flatpak", "list", "--app", "--columns=application"], timeout=30)
        return a["id"] not in out.split()
    return True


# ───────────────────────── removed items: list, restore, delete ─────────────────────────

def runs():
    out = []
    if not REMOVED.is_dir():
        return out
    loaded = []
    for f in REMOVED.glob("*.json"):
        with contextlib.suppress(OSError, ValueError):
            loaded.append(json.loads(f.read_text()))
    loaded.sort(key=lambda d: (d.get("when", 0), d.get("id", "")), reverse=True)
    for d in loaded[:40]:
        with contextlib.suppress(KeyError, TypeError):
            out.append({"id": d["id"], "when": d["when"], "state": d.get("state", "held"),
                        "held_bytes": d.get("held_bytes", 0) if d.get("state", "held") == "held" else 0,
                        "apps": [{"name": r["app"]["name"], "id": r["app"]["id"], "ok": r.get("ok", True),
                                  "reinstall": any(a["t"] == "pkg" and a.get("reinstall") for a in r["actions"])}
                                 for r in d.get("apps", [])]})
    return out


def _load_run(rid):
    if not RUN_ID.match(rid or ""):
        raise ValueError("unknown removal")
    return json.loads((REMOVED / f"{rid}.json").read_text())


def _save_run(d):
    (REMOVED / f"{d['id']}.json").write_text(json.dumps(d, indent=1))


def restore(rid):
    d = _load_run(rid)
    if d.get("state") != "held":
        print("This removal was already restored or deleted.")
        return True
    ok = True
    root_cmds, root_checks, reinstall_root, post_user = [], [], [], set()
    for rec in reversed(d["apps"]):
        print(f"Restoring {rec['app']['name']}…")
        for a in reversed(rec["actions"]):
            t = a["t"]
            try:
                if t == "move" and a["root"]:
                    root_cmds.append(f"[ -e {q(a['src'])} ] || {{ mkdir -p {q(os.path.dirname(a['src']))} && mv -T -- {q(a['dst'])} {q(a['src'])}; }}")
                    root_checks.append(a)
                elif t == "move":
                    if os.path.lexists(a["src"]):
                        raise OSError(f"something new is already at {tilde(a['src'])}")
                    os.makedirs(os.path.dirname(a["src"]), exist_ok=True)
                    move_path(a["dst"], a["src"])
                    print(f"   put back {tilde(a['src'])}")
                    if "/applications/" in a["src"]:
                        post_user.add("launchers")
                elif t == "edit":
                    shutil.copy2(a["backup"], a["path"])
                    print(f"   restored {tilde(a['path'])}")
                elif t == "unit" and a["root"]:
                    if a.get("enabled"):
                        root_cmds.append(f"systemctl daemon-reload && systemctl enable {'--now ' if a.get('active') else ''}{q(a['unit'])} || true")
                elif t == "unit":
                    post_user.add("units")
                    if a.get("enabled"):
                        post_user.add(f"enable:{a['unit']}:{int(bool(a.get('active')))}")
                elif t == "pkg" and a.get("reinstall") and a.get("ok", True):
                    if a.get("reinstall_root"):
                        reinstall_root.append(a["reinstall"])
                    else:
                        JOBS.emit(f"$ {a['reinstall']}", "cmd")
                        if JOBS._stream(["/bin/bash", "-c", a["reinstall"]]) != 0:
                            ok = False
            except Exception as e:  # noqa: BLE001
                ok = False
                print(f"   couldn't restore one part: {e}")
    if root_cmds or reinstall_root:
        cmds = [c for c in root_cmds if not c.startswith("systemctl")] + reinstall_root + \
               [c for c in root_cmds if c.startswith("systemctl")]
        if JOBS._run_root(cmds) != 0:
            ok = False
    for a in root_checks:
        if not os.path.lexists(a["src"]):
            ok = False
            print(f"   couldn't put back {tilde(a['src'])}")
    if "units" in post_user:
        JOBS._stream(["/bin/bash", "-c", "systemctl --user daemon-reload"])
        for item in post_user:
            if item.startswith("enable:"):
                _, unit, active = item.split(":")
                JOBS._stream(["/bin/bash", "-c", f"systemctl --user enable {'--now ' if active == '1' else ''}{q(unit)}"])
    if "launchers" in post_user and which("update-desktop-database"):
        JOBS._stream(["/bin/bash", "-c", f"update-desktop-database {q(H + '/.local/share/applications')} 2>/dev/null || true"])
    d["state"] = "restored" if ok else "held"
    _save_run(d)
    _cleanup_dirs(d)
    print("Everything is back." if ok else "Restored what could be restored; see the notes above.")
    return ok


def _cleanup_dirs(d):
    """Remove now-empty holding folders."""
    for rec in d["apps"]:
        for a in rec["actions"]:
            if a["t"] == "move" and not a["root"]:
                with contextlib.suppress(OSError):
                    os.rmdir(os.path.dirname(a["dst"]))


def delete_forever(rid):
    d = _load_run(rid)
    if d.get("state") != "held":
        print("Nothing left to delete for this removal.")
        return True
    root_dirs, ok = set(), True
    for rec in d["apps"]:
        for a in rec["actions"]:
            if a["t"] != "move":
                continue
            hold = os.path.dirname(a["dst"])
            if not hold.endswith(d["id"]):
                continue  # never delete anything outside this run's holding folders
            if a["root"]:
                root_dirs.add(hold)
                continue
            try:
                if os.path.isdir(a["dst"]) and not os.path.islink(a["dst"]):
                    shutil.rmtree(a["dst"], onerror=_writable)
                elif os.path.lexists(a["dst"]):
                    os.unlink(a["dst"])
            except OSError as e:
                ok = False
                print(f"   couldn't delete {tilde(a['dst'])}: {e}")
    for hold in sorted(root_dirs):
        if not hold.startswith(ROOT_REMOVED + "/") or not RUN_ID.match(os.path.basename(hold)):
            continue
        if JOBS._run_root([f"rm -rf --one-file-system -- {q(hold)}"]) != 0:
            ok = False
    if ok:
        shutil.rmtree(REMOVED / d["id"], ignore_errors=True)
        _cleanup_dirs(d)
        d["state"] = "deleted"
        _save_run(d)
        print(f"Deleted for good. Freed about {appscan.human(d.get('held_bytes', 0))}.")
    return ok


def _writable(func, p, _exc):
    with contextlib.suppress(OSError):
        os.chmod(os.path.dirname(p), 0o755)
        os.chmod(p, 0o755)
        func(p)
