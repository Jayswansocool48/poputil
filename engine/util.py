"""Shared helpers: paths, settings, shell calls, formatting."""
import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
import time
import types
from pathlib import Path

HOME = Path.home()
H = str(HOME)
CONF_DIR = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "poputil"
CONF_FILE = CONF_DIR / "config.json"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", HOME / ".local/share")) / "poputil"
MB = 1024 ** 2
GB = 1024 ** 3
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\r")

CLAUDE_MODEL = "claude-sonnet-5-5"
OLLAMA_MODEL = "llama3.1"


def which(name):
    return shutil.which(name)


def sh(cmd, timeout=30, env=None):
    """Run a command quietly. Returns (returncode, stdout). Never raises."""
    try:
        r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True,
                           timeout=timeout, errors="replace", env=env)
        return r.returncode, r.stdout
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""


def read(path, default=""):
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return default


def q(path):
    """Shell-quote a path for a command we build ourselves."""
    return shlex.quote(str(path))


def human(n):
    if not n or n <= 0:
        return "0 B" if n == 0 else "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


def tilde(p):
    p = str(p)
    return "~" + p[len(H):] if p == H or p.startswith(H + "/") else p


def inside(child, parent):
    child, parent = os.path.normpath(str(child)), os.path.normpath(str(parent))
    return child == parent or child.startswith(parent.rstrip("/") + "/")


def du(path, follow=False):
    """Disk usage (blocks) and newest mtime under path; doesn't follow symlinks."""
    try:
        st = os.stat(path) if follow else os.lstat(path)
    except OSError:
        return 0, 0
    if not os.path.isdir(path) or (os.path.islink(path) and not follow):
        return st.st_blocks * 512, st.st_mtime
    total, newest, seen = 0, st.st_mtime, set()
    stack = [path]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        s = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if s.st_nlink > 1 and not e.is_dir(follow_symlinks=False):
                        key = (s.st_dev, s.st_ino)
                        if key in seen:
                            continue
                        seen.add(key)
                    total += s.st_blocks * 512
                    newest = max(newest, s.st_mtime)
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
        except OSError:
            pass
    return total, newest


def days_ago(ts):
    return None if not ts else max(0, int((time.time() - ts) / 86400))


# ───────────────────────── settings ─────────────────────────

def load_conf():
    try:
        return json.loads(CONF_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_conf(conf):
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONF_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(conf, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(CONF_FILE)


def ai_args():
    """Which AI reviews things, from settings (Claude key, Ollama, or rules only)."""
    conf = load_conf()
    key = conf.get("anthropic_key") or os.environ.get("ANTHROPIC_API_KEY")
    ai = conf.get("ai") or ("claude" if key else "none")
    if key:
        os.environ["ANTHROPIC_API_KEY"] = key
    if ai == "claude" and not key:
        ai = "none"
    return types.SimpleNamespace(
        ai=ai, model=conf.get("model") or None,
        ollama_host=conf.get("ollama_host") or "http://localhost:11434",
        min_size=int(conf.get("min_size") or 50), large=int(conf.get("large") or 1024),
        old_days=int(conf.get("old_days") or 60), no_system=False)


# ───────────────────────── processes ─────────────────────────

_USED = {"t": 0.0, "v": set()}


def in_use_paths():
    """Executables, working dirs and memory-mapped files of every process we can see (cached 5 s)."""
    if time.time() - _USED["t"] < 5:
        return _USED["v"]
    used = set()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        for link in ("exe", "cwd"):
            with contextlib.suppress(OSError):
                used.add(os.readlink(f"/proc/{pid}/{link}").removesuffix(" (deleted)"))
    _USED.update(t=time.time(), v=used)
    return used


def running_inside(path):
    path = str(path)
    return any(inside(u, path) for u in in_use_paths())


def is_elf(path):
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False
