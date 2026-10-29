"""Clean up PopUtil copies installed before the Flatpak (version 1 and 2 used install.sh).

Runs once per launch when PopUtil runs as a Flatpak. It removes old launchers, command links and code,
never your data (Removed items, scan results, organize history and settings stay where they are).
Anything it can't remove (for example files a sudo install left owned by root) is reported, with the command to fix it.
"""
import contextlib
import json
import os
import shutil
import time

from .util import DATA_DIR, H, q, sh, which

OLD_CODE_FILES = ["poputil.py", "popclean.py", "organizer.py", "ui.html", "icon.svg", "apps.json", "apps.json.new"]
REPORT = DATA_DIR / "migration.json"


def _is_old_launcher(path):
    try:
        with open(path, errors="replace") as f:
            text = f.read()
    except OSError:
        return False
    return "Name=PopUtil" in text and "poputil.py" in text and "flatpak" not in text


def _points_at_old_code(link):
    tgt = os.readlink(link)
    tgt = tgt if os.path.isabs(tgt) else os.path.join(os.path.dirname(link), tgt)
    tgt = os.path.normpath(tgt)
    return (tgt.startswith(f"{H}/.local/lib/poputil/") or tgt in (str(DATA_DIR / "poputil.py"), str(DATA_DIR / "popclean.py"))
            or not os.path.exists(tgt) and "poputil" in tgt)


def find_old():
    found = []
    apps_dir = f"{H}/.local/share/applications"
    with contextlib.suppress(OSError):
        for e in os.scandir(apps_dir):
            if e.name.endswith(".desktop") and _is_old_launcher(e.path):
                found.append(e.path)
    for name in ("poputil", "popclean"):
        link = f"{H}/.local/bin/{name}"
        if os.path.islink(link) and _points_at_old_code(link):
            found.append(link)
    v2 = f"{H}/.local/lib/poputil"
    if os.path.isfile(f"{v2}/poputil.py") and os.path.isdir(f"{v2}/engine"):
        found.append(v2)
    for f in OLD_CODE_FILES:
        p = DATA_DIR / f
        if p.is_file():
            found.append(str(p))
    return found


def cleanup():
    """Remove old installs. Returns {"removed": [...], "stuck": [...]} and remembers it for the UI."""
    removed, stuck = [], []
    for p in find_old():
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p)
            else:
                os.unlink(p)
            removed.append(p)
        except OSError:
            stuck.append(p)
    if removed and which("update-desktop-database"):
        sh(["update-desktop-database", f"{H}/.local/share/applications"], timeout=20)
    result = {"when": time.time(), "removed": removed, "stuck": stuck,
              "fix": f"sudo rm -rf {' '.join(q(p) for p in stuck)}" if stuck else ""}
    if removed or stuck:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(result, indent=1))
    return result


def last_report():
    try:
        return json.loads(REPORT.read_text())
    except (OSError, ValueError):
        return None
