"""appscan — find everything on this PC that looks like an installed app, wherever it came from.

Linux software lands in many places: system packages, .deb files, Flatpak, Snap, AppImages,
folders unpacked into /opt or your home folder, install scripts that drop files into /usr/local,
pip/pipx/cargo/npm/go tools, toolchains (rustup, nvm, …), game engines, Wine apps and game
launchers. This module looks in all of them, groups the pieces of each app (program, launcher,
commands, background services, settings and data), and records how each one can be removed.
It never changes anything; removal lives in appremove.py.
"""
import contextlib
import glob
import gzip
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import stat
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import organizer
from .util import DATA_DIR, H, MB, du, human, in_use_paths, inside, read, sh, tilde, which

SCAN_FILE = DATA_DIR / "apps-scan.json"

# Where to look. Module-level so tests can point them at a fake system.
OPT = "/opt"
USR_LOCAL = "/usr/local"
SYSTEM_BIN_DIRS = ["/usr/bin", "/usr/sbin", "/usr/games"]
SYSTEMD_SYSTEM = "/etc/systemd/system"
DPKG_INFO = "/var/lib/dpkg/info"
APT_LOG_DIR = "/var/log/apt"
DPKG_LOG_DIR = "/var/log"
INSTALLER_STATUS = "/var/log/installer/initial-status.gz"
SNAPS = "/var/lib/snapd/snaps"
FLATPAK_SYSTEM = "/var/lib/flatpak"

SOURCES = {
    # key: (group, label)
    "apt": ("System packages", "Package from the Pop!_OS or Ubuntu repositories"),
    "deb": ("Downloaded .deb files", "Installed from a .deb file"),
    "flatpak": ("Flatpak", "Flatpak app"),
    "snap": ("Snap", "Snap package"),
    "appimage": ("AppImages", "AppImage"),
    "opt": ("Manual installs", "Unpacked into /opt"),
    "local": ("Manual installs", "Put in /usr/local by an install script"),
    "unowned": ("Manual installs", "Added to the system outside the package manager"),
    "folder": ("In your home folder", "Downloaded app folder"),
    "binary": ("In your home folder", "Downloaded program"),
    "homebin": ("In your home folder", "Command in ~/.local/bin"),
    "pip": ("Developer tools", "Python package (pip --user)"),
    "pipx": ("Developer tools", "Python app (pipx)"),
    "uv": ("Developer tools", "Python app (uv tool)"),
    "cargo": ("Developer tools", "Rust program (cargo install)"),
    "npm": ("Developer tools", "Node package (npm -g)"),
    "go": ("Developer tools", "Go program (go install)"),
    "toolchain": ("Developer tools", "Developer toolchain"),
    "engine": ("Game engines", "Game engine"),
    "wine": ("Windows apps", "Windows app in Wine"),
    "steam": ("Games", "Steam game"),
    "lutris": ("Games", "Lutris game"),
    "heroic": ("Games", "Heroic game"),
    "service": ("Background services", "Background service"),
    "leftover": ("Leftovers", "Left behind by a removed app"),
    "broken": ("Leftovers", "Broken launcher"),
}
GROUP_ORDER = ["System packages", "Downloaded .deb files", "Flatpak", "Snap", "AppImages", "Manual installs",
               "In your home folder", "Developer tools", "Game engines", "Windows apps", "Games",
               "Background services", "Leftovers"]

# Packages Pop!_OS can't work without. Removing them is blocked outright.
PROTECT = re.compile(
    r"^(pop-|cosmic-|system76|linux-|systemd|udev|libc6|libc-bin|libstdc|gcc-\d+-base|apt|dpkg|sudo|polkit|policykit|"
    r"network-manager|wpasupplicant|xserver|xwayland|gdm|gnome-shell|gnome-session|mesa|libgl|nvidia-|grub|shim|"
    r"efibootmgr|kernelstub|initramfs|plymouth|dbus|bash$|coreutils|util-linux|python3(\.\d+)?(-minimal)?$|"
    r"perl-base|flatpak$|snapd$|firmware|pipewire|wireplumber|pulseaudio|bluez|cups|ubuntu-|ca-certificates|tzdata|"
    r"locales|keyboard-configuration|console-setup|login$|passwd$|adduser|base-files|base-passwd|init$|"
    r"init-system-helpers|debconf|gpgv|e2fsprogs|mount$|fdisk|libpam|xdg-desktop-portal|gsettings|dconf)")

DATA_DENY = {"gtk20", "gtk30", "gtk40", "systemd", "autostart", "applications", "icons", "fonts", "pulse", "dconf",
             "keyrings", "mime", "trash", "recentlyused", "cosmic", "pop", "flatpak", "pip", "pipx", "npm", "node",
             "nodegyp", "cargo", "rustup", "go", "java", "python", "fontconfig", "ibus", "gnome", "kde", "qt", "xdg",
             "menus", "desktop", "user", "local", "share", "cache", "config", "ssh", "gnupg", "pki", "thumbnails",
             "gvfs", "tracker3", "wireplumber", "pipewire", "poputil", "vscode", "nv", "mesashadercache",
             "mozilla", "steam", "var", "wine", "lutris", "heroic", "bottles", "evolution", "gconf", "sound",
             "speechdispatcher", "xfce4", "procps", "bin", "lib", "include", "doc", "man", "android", "gradle"}

# Data that belongs to a well-known app even though its folder name doesn't match.
KNOWN_DATA = {
    "code": ["~/.vscode", "~/.config/Code"], "visualstudiocode": ["~/.vscode", "~/.config/Code"],
    "codium": ["~/.vscode-oss", "~/.config/VSCodium"], "vscodium": ["~/.vscode-oss", "~/.config/VSCodium"],
    "ollama": ["~/.ollama"], "lmstudio": ["~/.lmstudio", "~/.cache/lm-studio"],
    "firefox": ["~/.mozilla/firefox"], "thunderbird": ["~/.thunderbird"],
    "googlechrome": ["~/.config/google-chrome"], "chromium": ["~/.config/chromium"],
    "brave": ["~/.config/BraveSoftware"], "bravebrowser": ["~/.config/BraveSoftware"],
    "unityhub": ["~/.config/UnityHub"], "steam": ["~/.local/share/Steam", "~/.steam"],
    "discord": ["~/.config/discord"], "godot": ["~/.config/godot", "~/.local/share/godot"],
    "blender": ["~/.config/blender"], "obsidian": ["~/.config/obsidian"], "spotify": ["~/.config/spotify"],
    "androidstudio": ["~/.config/Google", "~/.android"], "cursor": ["~/.cursor", "~/.config/Cursor"],
    "zed": ["~/.config/zed", "~/.local/share/zed"], "lutris": ["~/.local/share/lutris", "~/.config/lutris"],
    "heroic": ["~/.config/heroic"], "bottles": ["~/.local/share/bottles"],
}
SENSITIVE_DATA = {"mozilla": "Browser profile: passwords, bookmarks and history",
                  "google-chrome": "Browser profile: passwords, bookmarks and history",
                  "chromium": "Browser profile: passwords, bookmarks and history",
                  "BraveSoftware": "Browser profile: passwords, bookmarks and history",
                  "thunderbird": "Your email accounts and messages", "Steam": "Contains your installed Steam games",
                  ".steam": "Steam's own files"}

# Toolchains that live in your home folder, and the shell-startup lines that load them.
TOOLCHAINS = [
    ("rustup", "~/.rustup", "Rust toolchain (rustup)", ["~/.rustup", "~/.cargo"], [r"\.cargo/env", r"\.cargo/bin"]),
    ("nvm", "~/.nvm", "Node.js versions (nvm)", ["~/.nvm"], [r"NVM_DIR", r"nvm\.sh"]),
    ("pyenv", "~/.pyenv", "Python versions (pyenv)", ["~/.pyenv"], [r"PYENV_ROOT", r"pyenv init", r"\.pyenv/bin"]),
    ("bun", "~/.bun", "Bun", ["~/.bun"], [r"BUN_INSTALL", r"\.bun/bin"]),
    ("deno", "~/.deno", "Deno", ["~/.deno"], [r"DENO_INSTALL", r"\.deno/bin", r"\.deno/env"]),
    ("sdkman", "~/.sdkman", "SDKMAN (Java, Kotlin, Gradle)", ["~/.sdkman"], [r"SDKMAN_DIR", r"sdkman-init\.sh"]),
    ("dotnet", "~/.dotnet", ".NET SDK", ["~/.dotnet"], [r"DOTNET_ROOT", r"\.dotnet/tools", r"\$HOME/\.dotnet"]),
    ("androidsdk", "~/Android/Sdk", "Android SDK", ["~/Android/Sdk"], [r"ANDROID_(HOME|SDK_ROOT)"]),
    ("platformio", "~/.platformio", "PlatformIO", ["~/.platformio"], [r"\.platformio/penv"]),
    ("arduino", "~/.arduino15", "Arduino boards and tools", ["~/.arduino15"], []),
    ("go-local", "~/.local/go", "Go (manual install)", ["~/.local/go"], [r"\.local/go/bin"]),
    ("go-usr", "/usr/local/go", "Go (manual install)", ["/usr/local/go"], [r"/usr/local/go/bin"]),
]
SHELL_FILES = ["~/.bashrc", "~/.profile", "~/.bash_profile", "~/.zshrc", "~/.zprofile"]

# Data from well-known apps that is big enough to matter when the app itself is gone.
ORPHAN_DATA = [("~/.lmstudio", "LM Studio models and data", "lmstudio"),
               ("~/.ollama", "Ollama models", "ollama"),
               ("~/.cache/huggingface", "Hugging Face models (cache)", "huggingface")]

WINE_DEFAULTS = {"common files", "internet explorer", "windows media player", "windows nt", "windows defender",
                 "windows mail", "windows photo viewer", "windows sidebar", "microsoft.net", "windowspowershell",
                 "windows journal", "windows portable devices", "msbuild", "reference assemblies",
                 "microsoft", "wine", "windows"}
STEAM_RUNTIME = re.compile(r"^(Proton|Steam Linux Runtime|Steamworks Common Redistributables|SteamVR)", re.I)

SKIP_WALK = {"node_modules", ".git", "__pycache__", "target", "Library", "Temp", "obj", ".godot", "Intermediate",
             "DerivedDataCache", "Saved", "venv", ".venv", "site-packages", "steamapps", "drive_c", "snap",
             ".poputil-removed", "lost+found", "$RECYCLE.BIN", "System Volume Information"}
ARCH_WORDS = {"linux", "x64", "x86", "x8664", "amd64", "arm64", "aarch64", "appimage", "portable", "setup",
              "stable", "release", "bin", "x11", "64", "32", "gtk", "deb", "tar", "gz", "xz", "zip", "universal",
              "mono", "installer", "glibc", "musl", "lts"}

ICON_EXTS = (".svg", ".png")
SEARCH_PATH = os.pathsep.join(dict.fromkeys(
    os.environ.get("PATH", "").split(os.pathsep) +
    [f"{H}/.local/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/games", "/snap/bin", f"{H}/.cargo/bin",
     f"{H}/go/bin", "/usr/local/sbin", "/usr/sbin"]))


# ───────────────────────── model ─────────────────────────

@dataclass
class Part:
    path: str
    role: str            # program, command, launcher, service, settings, data, shortcut, icon, models
    size: int = 0
    root: bool = False   # needs your password to change
    note: str = ""


@dataclass
class App:
    source: str
    key: str
    name: str
    kind: str = "app"    # app, tool, game, engine, runtime, service, leftover, launcher
    version: str = ""
    desc: str = ""
    icon: str = ""
    location: str = ""
    parts: list = field(default_factory=list)
    size: int = 0
    data_size: int = 0
    last_used: float = 0
    installed: float = 0
    how: str = ""
    by_user: bool = True
    in_library: bool = False
    running: bool = False
    protected: str = ""
    verdict: str = ""
    reason: str = ""
    notes: list = field(default_factory=list)
    method: dict = field(default_factory=dict)
    keys: list = field(default_factory=list)
    claims: list = field(default_factory=list)
    exe: str = ""
    ai: dict = field(default_factory=dict)

    @property
    def id(self):
        return hashlib.sha1(f"{self.source}:{self.key}".encode()).hexdigest()[:12]

    def add(self, path, role, size=None, root=None, note=""):
        path = str(path)
        if any(p.path == path for p in self.parts):
            return
        if size is None:
            size = du(path)[0]
        if root is None:
            root = needs_root(path)
        self.parts.append(Part(path, role, size, root, note))

    def public(self):
        group, label = SOURCES.get(self.source, ("Other", self.source))
        d = {k: v for k, v in asdict(self).items() if k not in ("keys", "claims", "exe", "method")}
        d.update(id=self.id, group=group, source_label=label, has_icon=bool(self.icon),
                 location_t=tilde(self.location) if self.location else "",
                 removable=not self.protected and self.method.get("type") != "external",
                 external=self.method.get("hint", "") if self.method.get("type") == "external" else "")
        d["parts"] = [dict(asdict(p), path_t=tilde(p.path)) for p in self.parts]
        return d


ROOT_AREAS = ("/opt", "/usr", "/etc", "/var", "/snap", "/srv", "/boot")


def needs_root(path):
    """Changing this path needs your password (system folders), as opposed to your home folder or your own drives."""
    path = os.path.normpath(str(path))
    return any(inside(path, r) for r in ROOT_AREAS)


def norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def pretty_name(raw):
    """'blender-4.2.0-linux-x64' -> ('Blender', '4.2.0');  'Obsidian-1.6.7.AppImage' -> ('Obsidian', '1.6.7')."""
    base = re.sub(r"\.(appimage|x86_64|x86|64|run|bin|tar\.\w+|zip)$", "", raw, flags=re.I)
    tokens = [t for t in re.split(r"[-_ ]+", base) if t]
    name, version = [], ""
    for t in tokens:
        tl = norm(t)
        if re.match(r"^v?\d", t):
            version = version or t.lstrip("vV")
            break
        if tl in ARCH_WORDS:
            break
        name.append(t)
    words = name or tokens[:1] or [raw]
    out = " ".join(w if any(c.isupper() for c in w) else w.capitalize() for w in words)
    return out, version


def read_head(path, n=64):
    """Read the start of a file without touching its 'last opened' time where possible."""
    flags = os.O_RDONLY | getattr(os, "O_NOATIME", 0)
    try:
        fd = os.open(path, flags)
    except PermissionError:
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return b""
    except OSError:
        return b""
    try:
        return os.read(fd, n)
    finally:
        os.close(fd)


def is_elf_head(head):
    return head[:4] == b"\x7fELF"


def is_appimage_head(head):
    return head[:4] == b"\x7fELF" and head[8:10] == b"AI" and len(head) > 10 and head[10] in (1, 2)


def atime(path):
    try:
        return os.stat(path).st_atime
    except OSError:
        return 0


def newest_mtime(path, depth=2):
    try:
        newest = os.stat(path).st_mtime
    except OSError:
        return 0
    if depth <= 0 or not os.path.isdir(path):
        return newest
    try:
        for e in os.scandir(path):
            with contextlib.suppress(OSError):
                if e.is_dir(follow_symlinks=False):
                    newest = max(newest, newest_mtime(e.path, depth - 1))
                else:
                    newest = max(newest, e.stat(follow_symlinks=False).st_mtime)
    except OSError:
        pass
    return newest


def expand(p):
    return os.path.expanduser(p) if p.startswith("~") else p


def parse_size(text):
    m = re.match(r"([\d.,]+)\s*([kKMGT]?i?B|bytes?)?", text.replace("\xa0", " ").strip())
    if not m:
        return 0
    num = float(m.group(1).replace(",", "."))
    unit = (m.group(2) or "B").lower()
    base = 1024 if "i" in unit else 1000
    exp = {"b": 0, "byte": 0, "bytes": 0, "kb": 1, "kib": 1, "mb": 2, "mib": 2, "gb": 3, "gib": 3, "tb": 4, "tib": 4}
    return int(num * base ** exp.get(unit, 0))


# ───────────────────────── launchers (.desktop) ─────────────────────────

def desktop_dirs():
    dirs = [f"{H}/.local/share/applications", "/usr/local/share/applications", "/usr/share/applications",
            f"{H}/.local/share/flatpak/exports/share/applications",
            f"{FLATPAK_SYSTEM}/exports/share/applications", "/var/lib/snapd/desktop/applications"]
    for d in os.environ.get("XDG_DATA_DIRS", "").split(":"):
        if d:
            dirs.append(os.path.join(d, "applications"))
    return [d for d in dict.fromkeys(os.path.normpath(x) for x in dirs) if os.path.isdir(d)]


def parse_desktop(path):
    entry, in_main = {}, False
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            if in_main:
                break
            in_main = line == "[Desktop Entry]"
            continue
        if not in_main:
            continue
        k, sep, v = line.partition("=")
        k = k.strip()
        if sep and "[" not in k:
            entry.setdefault(k, v.strip())
    if not entry or entry.get("Type", "Application") != "Application":
        return None
    entry["_path"] = str(path)
    entry["_id"] = os.path.basename(path)[:-8]
    entry["_visible"] = (entry.get("NoDisplay", "false").lower() != "true"
                         and entry.get("Hidden", "false").lower() != "true")
    return entry


FIELD_CODE = re.compile(r"^%[fFuUdDnNickvm%]$")


def exec_target(entry):
    """Work out what a launcher actually starts."""
    raw = entry.get("Exec", "")
    try:
        toks = shlex.split(raw.replace("\\\\", "\\"))
    except ValueError:
        toks = raw.split()
    toks = [t for t in toks if not FIELD_CODE.match(t)]
    env, i = {}, 0
    if toks and os.path.basename(toks[0]) == "env":
        i = 1
    while i < len(toks) and (re.match(r"^[A-Za-z_]\w*=", toks[i]) or (i and toks[i].startswith("-") and toks[0].endswith("env"))):
        m = re.match(r"^([A-Za-z_]\w*)=(.*)$", toks[i])
        if m:
            env[m.group(1)] = m.group(2)
        i += 1
    toks = toks[i:]
    if not toks:
        return None
    prog, base = toks[0], os.path.basename(toks[0])
    if base == "flatpak" and "run" in toks:
        rest = [t for t in toks[toks.index("run") + 1:] if not t.startswith("-") and not t.startswith("@@")]
        return {"kind": "flatpak", "id": rest[0] if rest else ""}
    if base == "snap" and len(toks) > 2 and toks[1] == "run":
        return {"kind": "snap", "name": toks[2].split(".")[0]}
    if prog.startswith("/snap/bin/"):
        return {"kind": "snap", "name": base.split(".")[0]}
    if base.startswith("wine") or "WINEPREFIX" in env:
        return {"kind": "wine", "prefix": expand(env.get("WINEPREFIX", "~/.wine")), "target": " ".join(toks[1:])}
    if base in ("sh", "bash", "python3", "python", "java", "node", "electron", "env") and len(toks) > 1:
        if "-c" in toks[1:2]:
            with contextlib.suppress(ValueError, IndexError):
                inner = shlex.split(toks[2])
                prog = next((t for t in inner if t.startswith(("/", "~"))), inner[0])
        else:
            prog = next((t for t in toks[1:] if t.startswith(("/", "~")) and not t.startswith("-")), prog)
    prog = expand(prog)
    path = prog if prog.startswith("/") else (shutil.which(prog, path=SEARCH_PATH) or "")
    exists = bool(path) and os.path.exists(path)
    if not exists and entry.get("TryExec"):
        te = expand(entry["TryExec"])
        alt = te if te.startswith("/") else shutil.which(te, path=SEARCH_PATH)
        if alt and os.path.exists(alt):
            path, exists = alt, True
    return {"kind": "path", "path": path or prog, "exists": exists,
            "real": os.path.realpath(path) if exists else ""}


def load_launchers():
    out = []
    for d in desktop_dirs():
        for root, _dirs, files in os.walk(d):
            for f in files:
                if f.endswith(".desktop"):
                    e = parse_desktop(os.path.join(root, f))
                    if e:
                        e["_target"] = exec_target(e)
                        out.append(e)
    return out


# ───────────────────────── icons ─────────────────────────

def build_icon_index():
    bases = [f"{H}/.local/share/icons", f"{H}/.icons", "/usr/local/share/icons", "/usr/share/icons",
             f"{FLATPAK_SYSTEM}/exports/share/icons", f"{H}/.local/share/flatpak/exports/share/icons"]
    themes = ["hicolor", "Pop", "Cosmic", "Adwaita", "breeze", "Papirus", "Yaru"]
    rank = {"scalable": 0, "256x256": 1, "512x512": 2, "128x128": 3, "96x96": 4, "64x64": 5, "48x48": 6}
    best = {}

    def offer(name, path, r):
        if name not in best or r < best[name][0]:
            best[name] = (r, path)

    for b in bases:
        for theme in themes:
            for apps_dir in glob.glob(f"{b}/{theme}/*/apps"):
                r = rank.get(os.path.basename(os.path.dirname(apps_dir)), 8)
                with contextlib.suppress(OSError):
                    for e in os.scandir(apps_dir):
                        if e.name.endswith(ICON_EXTS):
                            offer(e.name.rsplit(".", 1)[0], e.path, r)
    for d in ("/usr/share/pixmaps", "/var/lib/snapd/desktop/icons", f"{H}/.local/share/icons"):
        with contextlib.suppress(OSError):
            for e in os.scandir(d):
                if e.is_file() and e.name.endswith(ICON_EXTS):
                    offer(e.name.rsplit(".", 1)[0], e.path, 9)
    return {k: v[1] for k, v in best.items()}


def resolve_icon(name, index):
    if not name:
        return ""
    name = expand(name)
    if name.startswith("/"):
        if name.endswith(ICON_EXTS) and os.path.isfile(name):
            return name
        name = os.path.basename(name).rsplit(".", 1)[0]
    return index.get(name) or index.get(name.lower()) or ""


# ───────────────────────── system packages ─────────────────────────

WATCH = ("/opt/", "/usr/local/", "/etc/systemd/system/", "/usr/share/applications/", "/usr/bin/", "/usr/sbin/",
         "/usr/games/", "/usr/lib/systemd/system/")


def dpkg_index():
    owner, bins, desktops, top = {}, {}, {}, set()
    lines = read(f"{DPKG_INFO}/../diversions").splitlines()
    for i in range(0, len(lines) - 2, 3):  # path, diverted-to, package
        for p in lines[i:i + 2]:
            p = "/usr" + p if p.startswith(("/bin/", "/sbin/")) else p
            owner[p] = lines[i + 2]
    for f in glob.glob(f"{DPKG_INFO}/*.list"):
        pkg = os.path.basename(f)[:-5].split(":")[0]
        try:
            fh = open(f, errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                p = line.rstrip("\n")
                if p.startswith(("/bin/", "/sbin/", "/lib/systemd/")):
                    p = "/usr" + p
                if p.startswith(WATCH):
                    owner[p] = pkg
                    if p.startswith(("/usr/bin/", "/usr/games/")) and p.count("/") == 3:
                        bins.setdefault(pkg, []).append(p)
                    elif p.startswith("/usr/share/applications/") and p.endswith(".desktop"):
                        desktops.setdefault(pkg, []).append(p)
                    if p.startswith("/opt/"):
                        top.add("/opt/" + p.split("/")[2])
                elif p.count("/") == 3 and p.startswith(("/usr/share/", "/usr/lib/")):
                    top.add(p)
    return {"owner": owner, "bins": bins, "desktops": desktops, "top": top}


def dpkg_status():
    fmt = "${Package}\t${Version}\t${Installed-Size}\t${Priority}\t${Essential}\t${db:Status-Abbrev}\t${binary:Summary}\n"
    _, out = sh(["dpkg-query", "-W", f"-f={fmt}"], timeout=60)
    pk = {}
    for line in out.splitlines():
        f = line.split("\t")
        if len(f) < 7 or not f[5].startswith("ii"):
            continue
        name = f[0].split(":")[0]
        pk[name] = {"version": f[1], "size": int(f[2] or 0) * 1024, "priority": f[3], "essential": f[4] == "yes",
                    "summary": f[6]}
    return pk


def _log_lines(pattern):
    files = sorted(glob.glob(pattern), key=lambda p: (-int(m.group(1)) if (m := re.search(r"\.(\d+)(\.gz)?$", p)) else 0))
    for f in files:  # oldest first, so later events win
        try:
            opener = gzip.open if f.endswith(".gz") else open
            with opener(f, "rt", errors="replace") as fh:
                yield fh.read()
        except OSError:
            continue


def apt_history():
    """Packages you explicitly asked apt to install: name -> (timestamp, command line)."""
    req = {}
    for text in _log_lines(f"{APT_LOG_DIR}/history.log*"):
        for block in text.split("\n\n"):
            ms = re.search(r"Start-Date: (\d{4}-\d\d-\d\d)\s+(\d\d:\d\d:\d\d)", block)
            mi = re.search(r"^Install: (.*)$", block, re.M)
            if not ms or not mi:
                continue
            cmd = (re.search(r"^Commandline: (.*)$", block, re.M) or [None, ""])[1]
            try:
                ts = time.mktime(time.strptime(f"{ms.group(1)} {ms.group(2)}", "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                ts = 0
            for name, info in re.findall(r"([^\s,():]+)(?::[\w-]+)? \(([^)]*)\)", mi.group(1)):
                if "automatic" not in info:
                    req[name] = (ts, cmd)
    return req


def dpkg_install_times():
    times = {}
    for text in _log_lines(f"{DPKG_LOG_DIR}/dpkg.log*"):
        for m in re.finditer(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) install (\S+?)(?::\S+)? ", text, re.M):
            with contextlib.suppress(ValueError):
                times[m.group(2)] = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
    return times


def installer_packages():
    try:
        with gzip.open(INSTALLER_STATUS, "rt", errors="replace") as fh:
            return set(re.findall(r"^Package: (\S+)", fh.read(), re.M))
    except OSError:
        return None


def local_debs():
    """Packages no repository offers, i.e. installed from a .deb file."""
    if not glob.glob("/var/lib/apt/lists/*_Packages*"):
        return set()  # no repository data, so "local" would mean nothing
    _, out = sh("apt list --installed 2>/dev/null", timeout=60)
    lines = [line for line in out.splitlines() if "[installed" in line]
    local = {line.split("/")[0] for line in lines if ",local]" in line}
    return local if len(local) < max(20, len(lines) // 3) else set()


def apt_origins(pkgs):
    """Which repository each package came from (host name), for packages from third-party repos."""
    if not pkgs:
        return {}
    _, out = sh(["apt-cache", "policy", *sorted(pkgs)], timeout=60)
    origins, cur, in_inst = {}, None, False
    for line in out.splitlines():
        if line and not line.startswith(" ") and line.endswith(":"):
            cur, in_inst = line[:-1].split(":")[0], False
            continue
        if cur is None:
            continue
        if line.startswith(" *** "):
            in_inst = True
            continue
        if in_inst:
            m = re.match(r"^\s+\d+\s+(\S+)", line)
            if m and "://" in m.group(1):
                origins[cur] = re.sub(r"^\w+://([^/]+).*", r"\1", m.group(1))
                in_inst = False
            elif line.startswith("     ") and re.match(r"^\s+\S+ \d+$", line):
                in_inst = False
    official = re.compile(r"(^|\.)(ubuntu\.com|pop-os\.org|system76\.com|launchpadcontent\.net|launchpad\.net)$")
    return {k: v for k, v in origins.items() if not official.search(v)}


def manual_packages():
    _, out = sh(["apt-mark", "showmanual"], timeout=60)
    return {line.strip().split(":")[0] for line in out.splitlines() if line.strip()}


def fmt_date(ts):
    return time.strftime("%b %-d, %Y", time.localtime(ts)) if ts else ""


def scan_apt(ctx):
    print("Reading system packages…")
    pk = dpkg_status()
    idx = ctx["dpkg"]
    hist = apt_history()
    times = dpkg_install_times()
    initial = installer_packages()
    manual = manual_packages()
    local = local_debs()
    by_pkg_launchers = {}
    for e in ctx["launchers"]:
        p = idx["owner"].get(e["_path"])
        if p:
            by_pkg_launchers.setdefault(p, []).append(e)

    def user_installed(name):
        if name in local or name in hist:
            return True
        return initial is not None and name in manual and name not in initial

    def looks_like_library(n):
        return not idx["bins"].get(n) and not any(e["_visible"] for e in by_pkg_launchers.get(n, []))

    chosen = [n for n in pk if any(e["_visible"] for e in by_pkg_launchers.get(n, []))
              or ((n in local or user_installed(n)) and not looks_like_library(n))
              or (n in local and not re.match(r"^(lib|python3-|fonts-|gir1)", n))]
    origins = apt_origins([n for n in chosen if n not in local])
    apps = []
    for name in chosen:
        info = pk[name]
        visible = [e for e in by_pkg_launchers.get(name, []) if e["_visible"]]
        src = "deb" if name in local else "apt"
        app = App(src, name, name, kind="app" if visible else "tool", version=info["version"],
                  desc=info["summary"], size=info["size"], by_user=user_installed(name), in_library=bool(visible))
        if len(visible) == 1:
            app.name = visible[0].get("Name") or name
            app.desc = visible[0].get("Comment") or app.desc
        elif visible:
            app.name = pretty_name(name)[0]
        launcher = visible[0] if visible else None
        if launcher:
            app.icon = resolve_icon(launcher.get("Icon", ""), ctx["icons"])
            tgt = launcher["_target"] or {}
            app.exe = tgt.get("real", "") if tgt.get("kind") == "path" else ""
            for e in visible:
                app.add(e["_path"], "launcher", root=True)
        bins = idx["bins"].get(name, [])
        app.exe = app.exe or (bins[0] if bins else "")
        for b in bins[:6]:
            app.add(b, "command", root=True)
        app.location = os.path.dirname(app.exe) if app.exe and not app.exe.startswith("/usr/bin") else (app.exe or "/usr")
        app.keys = [name, norm(app.name)] + [os.path.basename(b) for b in bins[:3]] + \
                   ([launcher["_id"], launcher.get("StartupWMClass", "")] if launcher else [])
        app.last_used = atime(app.exe) if app.exe else 0
        app.installed = (hist.get(name) or (0,))[0] or times.get(name, 0)
        if name in hist:
            cmd = hist[name][1]
            debfile = next((t for t in cmd.split() if t.endswith(".deb")), "")
            app.how = (f"Installed from {tilde(debfile) if debfile.startswith('/') else debfile} on {fmt_date(hist[name][0])}"
                       if debfile else f"You installed it on {fmt_date(hist[name][0])} with “{cmd.strip()}”")
        elif src == "deb":
            app.how = "Installed from a .deb file" + (f" on {fmt_date(app.installed)}" if app.installed else "")
        elif not app.by_user:
            app.how = "Came with Pop!_OS"
        else:
            app.how = f"Installed with apt{(' on ' + fmt_date(app.installed)) if app.installed else ''}"
        if name in origins:
            app.notes.append(f"Comes from a third-party repository: {origins[name]}")
            app.how += f" from {origins[name]}"
        if info["essential"] or info["priority"] in ("required", "important") or PROTECT.match(name):
            app.protected = "Pop!_OS needs this to work"
        app.method = {"type": "apt", "pkgs": [name]}
        app.claims = [p.path for p in app.parts]
        apps.append(app)
    return apps


# ───────────────────────── Flatpak & Snap ─────────────────────────

def scan_flatpak(ctx):
    if not which("flatpak"):
        return []
    print("Reading Flatpak apps…")
    rc, out = sh(["flatpak", "list", "--app", "--columns=application,name,version,branch,installation,origin,size"], timeout=60)
    if rc != 0:
        return []
    _, desc_out = sh(["flatpak", "list", "--app", "--columns=application,description"], timeout=60)
    descs = dict(line.split("\t", 1) for line in desc_out.splitlines() if "\t" in line)
    _, ps = sh(["flatpak", "ps", "--columns=application"], timeout=20)
    running = {line.strip() for line in ps.splitlines() if line.strip()}
    launchers = {e["_id"]: e for e in ctx["launchers"]}
    apps = []
    for line in out.splitlines():
        f = (line.split("\t") + [""] * 7)[:7]
        app_id, name, version, _branch, inst, origin, size = f
        if not app_id or app_id.startswith("Application"):
            continue
        inst = inst or "system"
        base = f"{H}/.local/share/flatpak" if inst == "user" else FLATPAK_SYSTEM
        app = App("flatpak", app_id, name or app_id.split(".")[-1], version=version, desc=descs.get(app_id, ""),
                  size=parse_size(size), location=f"{base}/app/{app_id}", running=app_id in running)
        app.add(app.location, "program", size=app.size, root=inst != "user")
        e = launchers.get(app_id)
        if e:
            app.in_library = e["_visible"]
            app.icon = resolve_icon(e.get("Icon", app_id), ctx["icons"])
            app.desc = app.desc or e.get("Comment", "")
        else:
            app.icon = resolve_icon(app_id, ctx["icons"])
        data = f"{H}/.var/app/{app_id}"
        if os.path.isdir(data):
            app.add(data, "data", note="Its settings, saves and cache")
            app.last_used = newest_mtime(data, 2)
        with contextlib.suppress(OSError):
            app.installed = os.stat(app.location).st_mtime
        app.how = f"Flatpak from {origin or 'Flathub'} ({'just for you' if inst == 'user' else 'for everyone on this PC'})"
        if app_id == os.environ.get("FLATPAK_ID", "io.github.Jayswansocool48.PopUtil"):
            app.protected = "This is PopUtil itself. Remove it from the COSMIC Store"
        app.method = {"type": "flatpak", "id": app_id, "installation": inst, "origin": origin or "flathub"}
        app.keys = [app_id, norm(app.name)]
        app.claims = [app.location, data]
        apps.append(app)
    installed = {a.key for a in apps}
    # data left behind by Flatpaks that were uninstalled
    var_app = f"{H}/.var/app"
    with contextlib.suppress(OSError):
        for e in os.scandir(var_app):
            if e.is_dir(follow_symlinks=False) and e.name not in installed:
                size = du(e.path)[0]
                if size < 1 * MB:
                    continue
                lo = App("leftover", e.path, e.name, kind="leftover", location=e.path,
                         desc=f"Settings and data from the Flatpak {e.name}, which isn't installed anymore",
                         how="Flatpak apps keep their data in ~/.var/app even after they're removed")
                lo.add(e.path, "data", size=size)
                lo.size = size
                lo.last_used = newest_mtime(e.path, 1)
                lo.method = {"type": "files"}
                apps.append(lo)
    return apps


SNAP_SKIP = re.compile(r"^(core\d*|snapd|bare|gtk-common-themes|gnome-\d+-\d+|kde-frameworks|mesa-|snapd-desktop|"
                       r"snap-store|firmware-updater)")


def scan_snap(ctx):
    if not which("snap"):
        return []
    print("Reading Snap packages…")
    rc, out = sh(["snap", "list", "--unicode=never", "--color=never"], timeout=60)
    if rc != 0:
        return []
    used = in_use_paths()
    launchers = ctx["launchers"]
    apps = []
    for line in out.splitlines()[1:]:
        f = line.split()
        if len(f) < 4:
            continue
        name, version, rev = f[0], f[1], f[2]
        notes = f[5] if len(f) > 5 else ""
        if SNAP_SKIP.match(name) or any(w in notes for w in ("base", "core", "snapd")):
            continue
        snapfile = f"{SNAPS}/{name}_{rev}.snap"
        app = App("snap", name, name, version=version, location=f"/snap/{name}",
                  size=os.path.getsize(snapfile) if os.path.exists(snapfile) else 0)
        app.add(snapfile if os.path.exists(snapfile) else f"/snap/{name}", "program", size=app.size, root=True)
        for e in launchers:
            if e["_path"].startswith("/var/lib/snapd/desktop/applications/") and e["_id"].startswith(f"{name}_"):
                app.in_library = app.in_library or e["_visible"]
                app.name = e.get("Name") or app.name
                app.icon = app.icon or resolve_icon(e.get("Icon", ""), ctx["icons"])
                app.desc = app.desc or e.get("Comment", "")
        data = f"{H}/snap/{name}"
        if os.path.isdir(data):
            app.add(data, "data")
            app.last_used = newest_mtime(data, 2)
        app.running = any(u.startswith(f"/snap/{name}/") for u in used)
        app.how = f"Snap from the Snap Store{(' (' + f[3] + ')') if len(f) > 3 and f[3] != '-' else ''}"
        app.method = {"type": "snap", "name": name}
        app.keys = [name, norm(app.name)]
        app.claims = [f"/snap/{name}", data]
        apps.append(app)
    return apps


# ───────────────────────── manual installs in /opt, /usr/local, /usr/bin ─────────────────────────

def main_exe(folder, hint=""):
    """The program a folder most likely runs: an ELF named like the folder, else the largest one."""
    cands = []
    for sub in ("", "bin"):
        d = os.path.join(folder, sub)
        with contextlib.suppress(OSError):
            for e in os.scandir(d):
                with contextlib.suppress(OSError):
                    if e.is_file(follow_symlinks=False) and e.stat().st_mode & 0o111 and not e.name.endswith(".so"):
                        cands.append(e)
    best, best_score = "", -1
    hint = norm(hint or os.path.basename(folder))
    for e in cands[:60]:
        st = e.stat()
        head = read_head(e.path, 4) if st.st_size > 64 * 1024 else b""
        score = (st.st_size / MB) + (50 if hint and norm(e.name) and (norm(e.name) in hint or hint in norm(e.name)) else 0)
        score += 10 if is_elf_head(head) else (5 if e.name.endswith(".sh") else 0)
        if score > best_score:
            best, best_score = e.path, score
    return best


def unreal_version(folder):
    with contextlib.suppress(OSError, ValueError):
        v = json.loads(read(os.path.join(folder, "Engine/Build/Build.version")))
        return f"{v.get('MajorVersion')}.{v.get('MinorVersion')}.{v.get('PatchVersion')}"
    return ""


def scan_opt(ctx):
    print("Looking in /opt…")
    idx, apps = ctx["dpkg"], []
    with contextlib.suppress(OSError):
        for e in sorted(os.scandir(OPT), key=lambda e: e.name):
            if e.path in idx["top"] or not e.is_dir(follow_symlinks=False) or e.name.startswith("."):
                continue
            if du(e.path)[0] < 64 * 1024:
                continue  # an empty folder some installer left; nothing to show
            if os.path.isfile(os.path.join(e.path, "Engine/Binaries/Linux/UnrealEditor")):
                name, ver, kind, src = "Unreal Engine", unreal_version(e.path), "engine", "engine"
            else:
                (name, ver), kind, src = pretty_name(e.name), "app", "opt"
            app = App(src, e.path, name, kind=kind, version=ver, location=e.path)
            app.add(e.path, "program")
            app.size = app.parts[0].size
            app.exe = main_exe(e.path, e.name)
            app.last_used = atime(app.exe) if app.exe else 0
            with contextlib.suppress(OSError):
                app.installed = e.stat().st_mtime
            app.how = "Unpacked into /opt by hand or by an installer. No package manager tracks it."
            app.method = {"type": "files"}
            app.keys = [e.name, norm(name)]
            app.claims = [e.path]
            apps.append(app)
    return apps


def scan_usr_local(ctx):
    print("Looking in /usr/local and for files added outside the package manager…")
    idx, apps = ctx["dpkg"], []
    dirs = [(f"{USR_LOCAL}/bin", "local"), (f"{USR_LOCAL}/sbin", "local")] + [(d, "unowned") for d in SYSTEM_BIN_DIRS]
    for d, src in dirs:
        with contextlib.suppress(OSError):
            for e in sorted(os.scandir(d), key=lambda e: e.name):
                p = e.path
                if p in idx["owner"] or e.name.startswith("."):
                    continue
                if src == "unowned" and os.path.realpath(p) != p and inside(os.path.realpath(p), d):
                    continue  # links between packaged commands
                if e.is_symlink():
                    tgt = os.path.realpath(p)
                    if tgt.startswith("/etc/alternatives") or tgt in idx["owner"] or not os.path.exists(tgt):
                        if not os.path.exists(tgt) and src == "local":
                            ctx["dangling"].append(p)
                        continue
                    ctx["links"].append((p, tgt))
                    continue
                if not e.is_file() or p in ctx["pip_cmds"]:
                    continue
                if src == "unowned":
                    # files a package's install script created are owned by nobody but belong to the system
                    with contextlib.suppress(OSError):
                        if e.stat().st_mtime < ctx["os_installed"] + 3600:
                            continue
                name, _ = pretty_name(e.name)
                app = App(src, p, e.name, kind="tool", location=p)
                app.add(p, "program", root=True)
                app.size = app.parts[0].size
                app.exe = p
                app.last_used = atime(p)
                with contextlib.suppress(OSError):
                    app.installed = e.stat().st_mtime
                app.how = ("Put in /usr/local by an install script (for example “curl … | sh”)" if src == "local"
                           else f"Copied into {d} without a package. Nothing will update or remove it for you.")
                app.method = {"type": "files"}
                app.keys = [e.name, norm(name)]
                app.claims = [p]
                apps.append(app)
    # Group /usr/local/lib/<x>, /usr/local/share/<x> with a command of the same name later (attach phase)
    for sub in ("lib", "share"):
        with contextlib.suppress(OSError):
            for e in os.scandir(f"{USR_LOCAL}/{sub}"):
                if e.is_dir(follow_symlinks=False) and not e.name.startswith(("python", "pkgconfig", "man", "applications", "icons", "fonts")):
                    ctx["attachable"].append((e.path, norm(e.name), "program", True))
    with contextlib.suppress(OSError):
        for e in os.scandir("/usr/share"):
            if e.is_dir(follow_symlinks=False) and e.path not in idx["top"]:
                ctx["attachable"].append((e.path, norm(e.name), "data", True))
    return apps


def scan_services(ctx):
    print("Looking for background services…")
    idx, apps = ctx["dpkg"], []
    places = [(SYSTEMD_SYSTEM, True), (f"{H}/.config/systemd/user", False)]
    states = {}
    for scope in (["--system"], ["--user"]):
        _, out = sh(["systemctl", *scope, "list-unit-files", "--no-legend", "--no-pager"], timeout=20)
        for line in out.splitlines():
            f = line.split()
            if len(f) >= 2:
                states[(scope[0], f[0])] = f[1]
    for d, system in places:
        with contextlib.suppress(OSError):
            for e in os.scandir(d):
                if not e.name.endswith((".service", ".timer")) or e.is_symlink() or not e.is_file():
                    continue
                if e.path in idx["owner"] or e.name.startswith("poputil-"):
                    continue
                text = read(e.path)
                m = re.search(r"^ExecStart=[-@+!:]*(\S+)", text, re.M)
                exe = expand(m.group(1)) if m else ""
                desc = (re.search(r"^Description=(.*)$", text, re.M) or [None, ""])[1]
                unit = e.name
                state = states.get(("--system" if system else "--user", unit), "")
                ctx["services"].append({"path": e.path, "unit": unit, "exe": exe, "desc": desc,
                                        "system": system, "state": state})
    return apps


# ───────────────────────── home folder & other drives ─────────────────────────

def drive_roots():
    roots = []
    with contextlib.suppress(Exception):
        for d in organizer.drives():
            if d["writable"] and not d["holds_home"] and d["mount"] not in ("/",) \
                    and not d["mount"].startswith(("/mnt/attach", "/boot")):
                roots.append(d["mount"])
    return roots


def classify_dir(path, names):
    """Return what kind of app a folder is, or None."""
    j = os.path.join
    if os.path.isfile(j(path, "Engine/Binaries/Linux/UnrealEditor")):
        return {"type": "engine", "name": "Unreal Engine", "version": unreal_version(path)}
    if "Editor" in names and os.path.isfile(j(path, "Editor/Unity")):
        return {"type": "engine", "name": "Unity editor", "version": os.path.basename(path)}
    if "drive_c" in names and ("system.reg" in names or "user.reg" in names):
        return {"type": "wineprefix"}
    if "bin" in names and "packages" in names and os.path.isfile(j(path, "bin/flutter")):
        return {"type": "toolchain", "name": "Flutter SDK"}
    if "product-info.json" in names and "bin" in names:
        with contextlib.suppress(OSError, ValueError):
            info = json.loads(read(j(path, "product-info.json")))
            return {"type": "folder", "name": info.get("name", ""), "version": info.get("version", "")}
    if "application.ini" in names:
        ini = read(j(path, "application.ini"))
        nm = (re.search(r"^Name=(.*)$", ini, re.M) or [None, ""])[1]
        ver = (re.search(r"^Version=(.*)$", ini, re.M) or [None, ""])[1]
        return {"type": "folder", "name": nm, "version": ver}
    if "chrome-sandbox" in names or ("resources" in names and os.path.isfile(j(path, "resources/app.asar"))):
        return {"type": "folder"}
    base = norm(os.path.basename(path))
    for n in names:
        p = j(path, n)
        with contextlib.suppress(OSError):
            st = os.lstat(p)
            if not stat.S_ISREG(st.st_mode) or not st.st_mode & 0o111 or st.st_size < MB or n.endswith(".so"):
                continue
            head = read_head(p, 16)
            if not is_elf_head(head) or is_appimage_head(head):
                continue
            nn = norm(n)
            if (nn and base and (nn in base or base.startswith(nn[:5]))) or st.st_size > 40 * MB:
                return {"type": "folder"}
    if "bin" in names and "lib" in names:
        exe = main_exe(path)
        if exe and norm(os.path.basename(path))[:4] in norm(os.path.basename(exe)):
            return {"type": "folder"}
    return None


NEVER_APP = {H, f"{H}/.local", f"{H}/.local/share", f"{H}/.local/lib", f"{H}/Desktop", f"{H}/Downloads",
             f"{H}/Documents", f"{H}/Pictures", f"{H}/Videos", f"{H}/Music", f"{H}/Templates", f"{H}/Public"}

GODOT_RE = re.compile(r"^Godot_v([\d.]+)-(\w+?)(_mono)?_(linux|x11)", re.I)


def walk_home(ctx):
    """Find AppImages, unpacked app folders, loose programs, engines and Wine prefixes."""
    print("Looking through your home folder and other drives…")
    roots = [(H, True)] + [(r, False) for r in drive_roots()]
    extra = [f"{H}/.local/opt", f"{H}/opt", f"{H}/Applications", f"{H}/.local/share/JetBrains/Toolbox/apps"]
    extra += glob.glob(f"{H}/.local/*.app")
    found = {"appimage": [], "folder": [], "binary": [], "engine": [], "wineprefix": [], "toolchain": []}
    skip_paths = {f"{H}/.local/bin", f"{H}/go/bin", f"{H}/.cargo", f"{H}/snap", str(DATA_DIR), f"{H}/.var",
                  f"{H}/.local/share/uv", f"{H}/.local/share/pipx", f"{H}/.local/pipx", f"{H}/.local/share/Steam",
                  f"{H}/.local/share/flatpak", f"{H}/.local/share/Trash", f"{H}/.local/share/lutris"}
    visited = 0
    stack = [(r, 0, is_home) for r, is_home in roots] + [(x, 0, True) for x in extra if os.path.isdir(x)]
    seen = set()
    while stack:
        d, depth, _ = stack.pop()
        if d in seen:
            continue
        seen.add(d)
        visited += 1
        if visited % 2000 == 0:
            print(f"   …{visited} folders checked ({tilde(d)})")
        if visited > 400000:
            print("   Stopped walking after 400,000 folders.")
            break
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        names = {e.name for e in entries}
        if d not in NEVER_APP and depth > 0:
            kind = classify_dir(d, names)
            if kind:
                found[kind["type"] if kind["type"] in found else "folder"].append((d, kind))
                continue
            if organizer.project_kind(names, d):
                continue
        for e in entries:
            try:
                if e.is_symlink():
                    continue
                if e.is_dir():
                    if e.name in SKIP_WALK or e.path in skip_paths:
                        continue
                    if e.name.startswith(".") and not (d == H and e.name in (".local",)) and \
                            not (d == f"{H}/.local" and e.name in ("share", "opt")):
                        continue
                    if d == f"{H}/.local/share" and e.name not in ("JetBrains",) and depth > 6:
                        continue
                    if depth < 7:
                        stack.append((e.path, depth + 1, True))
                    continue
                if not e.is_file():
                    continue
                st = e.stat(follow_symlinks=False)
                low = e.name.lower()
                if low.endswith(".appimage") or (st.st_mode & 0o111 and st.st_size >= 2 * MB and not low.endswith(".so")
                                                and "." not in low.rsplit("/", 1)[-1][-6:-4]):
                    if st.st_size < MB:
                        continue
                    head = read_head(e.path, 16)
                    if is_appimage_head(head) or low.endswith(".appimage"):
                        found["appimage"].append((e.path, {}))
                    elif is_elf_head(head) and st.st_mode & 0o111 and d not in (f"{H}/.local/bin",):
                        m = GODOT_RE.match(e.name)
                        if m:
                            found["engine"].append((e.path, {"name": "Godot editor" + (" (.NET)" if m.group(3) else ""),
                                                             "version": f"{m.group(1)}-{m.group(2)}"}))
                        else:
                            found["binary"].append((e.path, {}))
            except OSError:
                continue
    return found


def app_from_found(kind, path, info, ctx):
    is_file = os.path.isfile(path)
    if kind == "appimage":
        name, ver = pretty_name(os.path.basename(path))
        app = App("appimage", path, name, version=ver, location=path)
        app.how = "AppImage: a single file that runs without installing. Deleting the file removes the app."
    elif kind == "engine":
        name = info.get("name") or pretty_name(os.path.basename(path))[0]
        if name == "Unity editor":
            name = f"Unity {info.get('version', '')} editor".replace("  ", " ")
            info = {**info, "version": ""}
        app = App("engine", path, name, kind="engine", version=info.get("version", ""), location=path)
        app.how = "Game engine you downloaded or that a hub installed."
        if "Unity" in name:
            app.notes.append("Unity Hub forgets editors that are removed the next time it starts.")
    elif kind == "toolchain":
        app = App("toolchain", path, info.get("name") or os.path.basename(path), kind="tool", location=path)
        app.how = "Developer toolchain you unpacked into your home folder."
    elif kind == "folder":
        guess, ver = pretty_name(os.path.basename(path))
        app = App("folder", path, info.get("name") or guess, version=info.get("version") or ver, location=path)
        app.how = "A downloaded app folder. Nothing installed it, so nothing will update or remove it."
    else:  # binary
        name, ver = pretty_name(os.path.basename(path))
        app = App("binary", path, name, kind="app", version=ver, location=path)
        app.how = "A program file sitting in a folder. It runs from there and isn't installed."
    app.add(path, "program", root=False)
    app.size = app.parts[0].size
    app.exe = path if is_file else main_exe(path)
    app.last_used = atime(app.exe) if app.exe else 0
    with contextlib.suppress(OSError):
        app.installed = os.stat(path).st_mtime
    app.method = {"type": "files"}
    app.keys = [norm(app.name), norm(app.name.split(" ")[0]), norm(os.path.basename(path).split(".")[0]),
                norm(os.path.basename(app.exe)) if app.exe else ""]
    app.claims = [path]
    return app


# ───────────────────────── developer tools ─────────────────────────

def scan_homebin(ctx):
    apps, d = [], f"{H}/.local/bin"
    with contextlib.suppress(OSError):
        for e in sorted(os.scandir(d), key=lambda e: e.name):
            if e.is_symlink():
                tgt = os.path.realpath(e.path)
                if not os.path.exists(tgt):
                    ctx["dangling"].append(e.path)
                else:
                    ctx["links"].append((e.path, tgt))
                continue
            if not e.is_file():
                continue
            head = read_head(e.path, 128)
            if b"python" in head.split(b"\n")[0] and e.path in ctx["pip_cmds"]:
                continue  # belongs to a pip package
            app = App("homebin", e.path, e.name, kind="tool", location=e.path)
            app.add(e.path, "program")
            app.size = app.parts[0].size
            app.exe = e.path
            app.last_used = atime(e.path)
            with contextlib.suppress(OSError):
                app.installed = e.stat().st_mtime
            app.how = "A command someone put in ~/.local/bin (an install script or you)."
            app.method = {"type": "files"}
            app.keys = [norm(e.name)]
            app.claims = [e.path]
            apps.append(app)
    return apps


def scan_pip(ctx):
    sites = [(sp, False) for sp in glob.glob(f"{H}/.local/lib/python3*/site-packages")]
    sites += [(sp, True) for sp in glob.glob(f"{USR_LOCAL}/lib/python3*/dist-packages")]
    dists = []
    for sp, system in sites:
        bindir = f"{USR_LOCAL}/bin" if system else f"{H}/.local/bin"
        with contextlib.suppress(OSError):
            for e in os.scandir(sp):
                if not e.name.endswith(".dist-info"):
                    continue
                meta = read(os.path.join(e.path, "METADATA"))
                name = (re.search(r"^Name: (.*)$", meta, re.M) or [None, e.name.split("-")[0]])[1].strip()
                files, size = [], 0
                for line in read(os.path.join(e.path, "RECORD")).splitlines():
                    p = os.path.normpath(os.path.join(sp, line.rsplit(",", 2)[0]))
                    with contextlib.suppress(OSError):
                        size += os.lstat(p).st_size
                    if inside(p, bindir):
                        files.append(p)
                        ctx["pip_cmds"].add(p)
                requires = {norm(re.split(r"[ ;<>=!~\[(]", r.strip())[0]) for r in re.findall(r"^Requires-Dist: (.*)$", meta, re.M)}
                dists.append({"name": name, "meta": meta, "files": files, "size": size, "sp": sp, "system": system,
                              "requested": os.path.exists(os.path.join(e.path, "REQUESTED")), "requires": requires,
                              "mtime": e.stat().st_mtime})
    needed = set().union(*(d["requires"] for d in dists)) if dists else set()
    apps = []
    for d in dists:
        if not d["requested"] and not d["files"]:
            continue
        dependency = norm(d["name"]) in needed
        meta = d["meta"]
        app = App("pip", f"{d['sp']}:{d['name'].lower()}", d["name"], kind="tool",
                  version=(re.search(r"^Version: (.*)$", meta, re.M) or [None, ""])[1].strip(), size=d["size"],
                  desc=(re.search(r"^Summary: (.*)$", meta, re.M) or [None, ""])[1].strip(), location=d["sp"],
                  by_user=not dependency)
        for f in d["files"]:
            app.add(f, "command", size=0, root=d["system"])
        app.exe = d["files"][0] if d["files"] else ""
        app.last_used = atime(app.exe) if app.exe else 0
        app.installed = d["mtime"]
        app.how = ("Installed system-wide with “sudo pip install”." if d["system"] else "Installed with “pip install --user”.")
        if dependency:
            app.notes.append("Other Python packages you installed need this one.")
        app.method = {"type": "pip", "name": d["name"], "version": app.version, "root": d["system"]}
        app.keys = [norm(d["name"])]
        app.claims = list(d["files"])
        apps.append(app)
    return apps


def scan_pipx(ctx):
    apps = []
    with contextlib.suppress(OSError):
        for e in os.scandir(f"{H}/.local/share/uv/tools"):
            if not e.is_dir() or e.name.startswith("."):
                continue
            receipt = read(os.path.join(e.path, "uv-receipt.toml"))
            ver = ""
            for dist in glob.glob(os.path.join(e.path, "lib/python3*/site-packages", "*.dist-info")):
                if norm(os.path.basename(dist).split("-")[0]) == norm(e.name):
                    ver = os.path.basename(dist).split("-")[1].removesuffix(".dist-info")
            app = App("uv", e.name, e.name, kind="tool", version=ver, location=e.path)
            app.add(e.path, "program")
            app.size = app.parts[0].size
            with contextlib.suppress(OSError):
                app.installed = e.stat().st_mtime
            app.how = "Installed with “uv tool install”." + (" " if receipt else "")
            app.method = {"type": "uv", "name": e.name}
            app.keys = [norm(e.name)]
            app.claims = [e.path]
            apps.append(app)
    for base in (f"{H}/.local/share/pipx/venvs", f"{H}/.local/pipx/venvs"):
        with contextlib.suppress(OSError):
            for e in os.scandir(base):
                if not e.is_dir():
                    continue
                meta = {}
                with contextlib.suppress(OSError, ValueError):
                    meta = json.loads(read(os.path.join(e.path, "pipx_metadata.json")))
                ver = (meta.get("main_package") or {}).get("package_version", "")
                app = App("pipx", e.name, e.name, kind="tool", version=ver, location=e.path)
                app.add(e.path, "program")
                app.size = app.parts[0].size
                with contextlib.suppress(OSError):
                    app.installed = e.stat().st_mtime
                app.how = "Installed with “pipx install”."
                app.method = {"type": "pipx", "name": e.name}
                app.keys = [norm(e.name)]
                app.claims = [e.path]
                apps.append(app)
    return apps


def scan_cargo(ctx):
    apps = []
    data = {}
    with contextlib.suppress(OSError, ValueError):
        data = json.loads(read(f"{H}/.cargo/.crates2.json")).get("installs", {})
    for spec, info in data.items():
        m = re.match(r"^(\S+) (\S+) ", spec + " ")
        if not m:
            continue
        name, ver = m.groups()
        bins = [f"{H}/.cargo/bin/{b}" for b in info.get("bins", [])]
        app = App("cargo", name, name, kind="tool", version=ver, location=f"{H}/.cargo/bin")
        for b in bins:
            if os.path.exists(b):
                app.add(b, "program")
        app.size = sum(p.size for p in app.parts)
        app.exe = bins[0] if bins else ""
        app.last_used = max((atime(b) for b in bins), default=0)
        app.how = "Installed with “cargo install”."
        app.method = {"type": "cargo", "name": name, "version": ver}
        app.keys = [norm(name)] + [norm(os.path.basename(b)) for b in bins]
        app.claims = bins
        apps.append(app)
    return apps


def scan_npm(ctx):
    if not which("npm"):
        return []
    rc, prefix = sh(["npm", "config", "get", "prefix"], timeout=20)
    prefix = prefix.strip()
    if rc != 0 or not prefix:
        return []
    mods = os.path.join(prefix, "lib/node_modules")
    apps = []
    user_owned = os.access(mods, os.W_OK)
    with contextlib.suppress(OSError):
        for e in os.scandir(mods):
            names = [e.path] if not e.name.startswith("@") else [x.path for x in os.scandir(e.path)]
            for p in names:
                pkg = {}
                with contextlib.suppress(OSError, ValueError):
                    pkg = json.loads(read(os.path.join(p, "package.json")))
                name = pkg.get("name") or os.path.basename(p)
                if name in ("npm", "corepack"):
                    continue
                app = App("npm", name, name, kind="tool", version=pkg.get("version", ""),
                          desc=pkg.get("description", ""), location=p)
                app.add(p, "program", size=du(os.path.realpath(p))[0], root=not user_owned)
                app.size = app.parts[0].size
                bins = pkg.get("bin") or {}
                bins = [bins] if isinstance(bins, str) else list(bins)
                app.exe = os.path.join(prefix, "bin", bins[0] if bins and bins[0] != pkg.get("bin") else name.split("/")[-1])
                app.last_used = atime(os.path.realpath(app.exe)) if os.path.exists(app.exe) else 0
                app.how = f"Installed with “npm install -g” into {tilde(prefix)}."
                app.method = {"type": "npm", "name": name, "root": not user_owned, "version": app.version}
                app.keys = [norm(name.split("/")[-1])]
                app.claims = [p]
                apps.append(app)
    return apps


def scan_go(ctx):
    apps = []
    with contextlib.suppress(OSError):
        for e in os.scandir(f"{H}/go/bin"):
            if e.is_file():
                app = App("go", e.path, e.name, kind="tool", location=e.path)
                app.add(e.path, "program")
                app.size = app.parts[0].size
                app.exe = e.path
                app.last_used = atime(e.path)
                app.how = "Installed with “go install”."
                app.method = {"type": "files"}
                app.keys = [norm(e.name)]
                app.claims = [e.path]
                apps.append(app)
    return apps


def shell_lines(patterns):
    hits = []
    if not patterns:
        return hits
    rx = re.compile("|".join(patterns))
    for f in SHELL_FILES:
        f = expand(f)
        for n, line in enumerate(read(f).splitlines(), 1):
            if rx.search(line) and not line.lstrip().startswith("#"):
                hits.append((f, n, line))
    return hits


def scan_toolchains(ctx):
    apps = []
    for key, marker, name, paths, patterns in TOOLCHAINS:
        if not os.path.exists(expand(marker)):
            continue
        app = App("toolchain", key, name, kind="tool", location=expand(marker))
        for p in paths:
            p = expand(p)
            if os.path.exists(p):
                app.add(p, "program", size=du(os.path.realpath(p))[0])
                if os.path.islink(p):
                    app.add(os.path.realpath(p), "program")
        app.size = sum(p.size for p in app.parts if not os.path.islink(p.path))
        app.last_used = newest_mtime(expand(marker), 1)
        lines = shell_lines(patterns)
        if lines:
            app.notes.append(f"Loaded by {len(lines)} line{'s' if len(lines) > 1 else ''} in your shell startup files; "
                             "PopUtil turns those off too when you remove it.")
        app.how = "Installed with its own installer script."
        app.method = {"type": "files", "shell": patterns}
        app.keys = [key]
        app.claims = [expand(p) for p in paths] + [p.path for p in app.parts]
        apps.append(app)
    # Godot export templates
    with contextlib.suppress(OSError):
        for e in os.scandir(f"{H}/.local/share/godot/export_templates"):
            app = App("engine", e.path, "Godot export templates", kind="engine", version=e.name, location=e.path)
            app.add(e.path, "program")
            app.size = app.parts[0].size
            app.how = "Downloaded by the Godot editor so it can export games."
            app.method = {"type": "files"}
            app.claims = [e.path]
            apps.append(app)
    # Unity editors installed by Unity Hub (default and custom location)
    hub_dirs = {f"{H}/Unity/Hub/Editor"}
    with contextlib.suppress(OSError, ValueError):
        custom = json.loads(read(f"{H}/.config/UnityHub/secondaryInstallPath.json") or '""')
        if custom:
            hub_dirs.add(custom)
    for hd in hub_dirs:
        with contextlib.suppress(OSError):
            for e in os.scandir(hd):
                if os.path.isfile(os.path.join(e.path, "Editor/Unity")):
                    ctx["claimed_engines"].add(e.path)
                    app = App("engine", e.path, f"Unity {e.name} editor", kind="engine", location=e.path)
                    app.add(e.path, "program")
                    app.size = app.parts[0].size
                    app.exe = os.path.join(e.path, "Editor/Unity")
                    app.last_used = atime(app.exe)
                    app.how = "Installed by Unity Hub."
                    app.notes.append("Unity Hub forgets editors that are removed the next time it starts.")
                    app.method = {"type": "files"}
                    app.claims = [e.path]
                    apps.append(app)
    return apps


# ───────────────────────── Windows apps & games ─────────────────────────

def wine_prefixes(found):
    prefixes = {f"{H}/.wine"} | set(glob.glob(f"{H}/.local/share/wineprefixes/*")) | \
        set(glob.glob(f"{H}/.var/app/com.usebottles.bottles/data/bottles/bottles/*")) | \
        set(glob.glob(f"{H}/.local/share/bottles/bottles/*")) | {p for p, _ in found}
    return sorted(p for p in prefixes if os.path.isdir(os.path.join(p, "drive_c")))


def scan_wine(ctx, found):
    apps = []
    launchers = [e for e in ctx["launchers"] if (e["_target"] or {}).get("kind") == "wine"
                 or "/applications/wine/" in e["_path"]]
    for prefix in wine_prefixes(found):
        if any(inside(prefix, c) for c in ctx["game_dirs"]):
            continue
        pre = App("wine", prefix, f"Windows environment ({tilde(prefix)})", kind="runtime", location=prefix)
        pre.add(prefix, "program")
        pre.size = pre.parts[0].size
        pre.last_used = newest_mtime(os.path.join(prefix, "drive_c/users"), 3)
        pre.how = "A Wine prefix: a fake Windows install where Windows programs live."
        pre.notes.append("Removing it removes every Windows app inside it.")
        pre.method = {"type": "files"}
        pre.claims = [prefix]
        apps.append(pre)
        dirs = glob.glob(f"{prefix}/drive_c/Program Files/*") + glob.glob(f"{prefix}/drive_c/Program Files (x86)/*") + \
            glob.glob(f"{prefix}/drive_c/users/*/AppData/Local/Programs/*")
        for d in sorted(dirs):
            if not os.path.isdir(d) or os.path.basename(d).lower() in WINE_DEFAULTS:
                continue
            size = du(d)[0]
            if size < 256 * 1024:
                continue
            name = os.path.basename(d)
            app = App("wine", d, name, location=d)
            app.add(d, "program", size=size)
            app.size = size
            exes = sorted(glob.glob(f"{d}/**/*.exe", recursive=True), key=lambda p: -os.path.getsize(p))[:1]
            app.exe = exes[0] if exes else ""
            app.last_used = atime(app.exe) if app.exe else 0
            key = norm(name)
            for e in launchers:
                if key and (key in norm(e.get("Name", "")) or key in norm(e["_path"])):
                    app.add(e["_path"], "launcher", size=0)
                    app.in_library = app.in_library or e["_visible"]
                    app.icon = app.icon or resolve_icon(e.get("Icon", ""), ctx["icons"])
                    e["_claimed"] = True
            for lnk in glob.glob(f"{prefix}/drive_c/users/*/AppData/Roaming/Microsoft/Windows/Start Menu/Programs/**/*.lnk",
                                 recursive=True) + glob.glob(
                    f"{prefix}/drive_c/ProgramData/Microsoft/Windows/Start Menu/Programs/**/*.lnk", recursive=True):
                if key and key in norm(lnk):
                    app.add(lnk, "shortcut", size=0)
            app.how = f"Windows program installed into the Wine prefix {tilde(prefix)}."
            app.notes.append("Its Windows registry entries stay in the prefix. They're harmless.")
            app.method = {"type": "files"}
            app.keys = [key]
            app.claims = [d]
            apps.append(app)
    return apps


def parse_vdf(text):
    root, stack, key = {}, None, None
    stack = [root]
    for s, br in re.findall(r'"((?:[^"\\]|\\.)*)"|([{}])', text):
        if br == "{":
            d = {}
            stack[-1][key if key is not None else ""] = d
            stack.append(d)
            key = None
        elif br == "}":
            if len(stack) > 1:
                stack.pop()
            key = None
        elif key is None:
            key = s
        else:
            stack[-1][key] = s
            key = None
    return root


def _find_apps_block(d, depth=0):
    if not isinstance(d, dict) or depth > 8:
        return None
    for k, v in d.items():
        if k.lower() == "apps" and isinstance(v, dict) and any(isinstance(x, dict) and "LastPlayed" in x for x in v.values()):
            return v
        found = _find_apps_block(v, depth + 1)
        if found:
            return found
    return None


def steam_roots():
    roots = [f"{H}/.local/share/Steam", f"{H}/.steam/steam", f"{H}/.var/app/com.valvesoftware.Steam/.local/share/Steam",
             f"{H}/snap/steam/common/.local/share/Steam"]
    return list(dict.fromkeys(os.path.realpath(r) for r in roots if os.path.isdir(os.path.join(r, "steamapps"))))


def scan_steam(ctx):
    apps, libraries, last_played = [], [], {}
    for r in steam_roots():
        libraries.append(r)
        vdf = parse_vdf(read(os.path.join(r, "steamapps/libraryfolders.vdf")))
        for v in (vdf.get("libraryfolders") or vdf.get("LibraryFolders") or {}).values():
            if isinstance(v, dict) and v.get("path"):
                libraries.append(v["path"])
        for cfg in glob.glob(os.path.join(r, "userdata/*/config/localconfig.vdf")):
            block = _find_apps_block(parse_vdf(read(cfg))) or {}
            for appid, info in block.items():
                played = info.get("LastPlayed", "") if isinstance(info, dict) else ""
                if isinstance(played, str) and played.isdigit():
                    last_played[appid] = max(last_played.get(appid, 0), int(played))
    seen_ids = set()
    for lib in dict.fromkeys(os.path.realpath(x) for x in libraries):
        sa = os.path.join(lib, "steamapps")
        manifests = glob.glob(os.path.join(sa, "appmanifest_*.acf"))
        ids_here = set()
        for m in manifests:
            st = parse_vdf(read(m)).get("AppState", {})
            appid, name = st.get("appid", ""), st.get("name", "")
            if not appid or appid in seen_ids:
                continue
            seen_ids.add(appid)
            ids_here.add(appid)
            folder = os.path.join(sa, "common", st.get("installdir", ""))
            size = int(st.get("SizeOnDisk", "0") or 0)
            runtime = bool(STEAM_RUNTIME.match(name))
            app = App("steam", appid, name or f"Steam app {appid}", kind="runtime" if runtime else "game",
                      location=folder, size=size)
            app.add(folder, "program", size=size, root=False)
            for extra, role in ((os.path.join(sa, "compatdata", appid), "data"), (os.path.join(sa, "shadercache", appid), "data")):
                if os.path.isdir(extra):
                    app.add(extra, role, note="Proton files and shader cache")
            app.last_used = last_played.get(appid, 0)
            app.installed = int(st.get("LastUpdated", "0") or 0)
            app.how = f"Installed by Steam into {tilde(lib)}."
            if runtime:
                app.verdict, app.reason = "keep", "Steam uses this to run games"
            app.method = {"type": "steam", "appid": appid}
            app.keys = [norm(name)]
            app.claims = [folder]
            ctx["game_dirs"].append(folder)
            apps.append(app)
        # Proton files for games that are gone
        with contextlib.suppress(OSError):
            for e in os.scandir(os.path.join(sa, "compatdata")):
                if e.name.isdigit() and e.name not in ids_here and e.name not in seen_ids and e.name != "0":
                    size = du(e.path)[0]
                    if size < 5 * MB:
                        continue
                    lo = App("leftover", e.path, f"Proton files for Steam app {e.name}", kind="leftover",
                             location=e.path, size=size,
                             desc="Windows-compatibility files for a Steam game that isn't installed anymore. "
                                  "Can include save games for games without cloud saves.")
                    lo.add(e.path, "data", size=size)
                    lo.how = "Steam leaves these behind when you uninstall a game."
                    lo.method = {"type": "files"}
                    apps.append(lo)
    return apps


def scan_lutris(ctx):
    apps = []
    for db in (f"{H}/.local/share/lutris/pga.db", f"{H}/.var/app/net.lutris.Lutris/data/lutris/pga.db"):
        if not os.path.isfile(db):
            continue
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
            rows = con.execute("SELECT id, name, runner, directory, lastplayed, installed_at FROM games WHERE installed=1").fetchall()
            con.close()
        except sqlite3.Error:
            continue
        for gid, name, runner, directory, lastplayed, installed_at in rows:
            app = App("lutris", f"{db}:{gid}", name or f"Lutris game {gid}", kind="game", location=directory or "")
            if directory and os.path.isdir(directory):
                app.add(directory, "program")
                app.size = app.parts[0].size
                ctx["game_dirs"].append(directory)
            app.last_used = lastplayed or 0
            app.installed = installed_at or 0
            app.how = f"Installed by Lutris ({runner or 'unknown runner'})."
            app.method = {"type": "external", "hint": "Remove it in Lutris: right-click the game, then Uninstall."}
            app.keys = [norm(name)]
            app.claims = [directory] if directory else []
            apps.append(app)
    return apps


def scan_heroic(ctx):
    apps = []
    for base in (f"{H}/.config/heroic", f"{H}/.var/app/com.heroicgameslauncher.hgl/config/heroic"):
        files = [(f"{base}/legendaryConfig/legendary/installed.json", "Epic"),
                 (f"{base}/gog_store/installed.json", "GOG"), (f"{base}/nile_config/nile/installed.json", "Amazon")]
        for f, store in files:
            with contextlib.suppress(OSError, ValueError):
                data = json.loads(read(f) or "{}")
                items = data.get("installed", data) if isinstance(data, dict) else data
                items = items.values() if isinstance(items, dict) else items
                for g in items:
                    if not isinstance(g, dict):
                        continue
                    path = g.get("install_path") or g.get("path") or ""
                    title = g.get("title") or g.get("appName") or os.path.basename(path)
                    app = App("heroic", f"{store}:{g.get('app_name') or g.get('appName') or path}", title,
                              kind="game", location=path)
                    if path and os.path.isdir(path):
                        app.add(path, "program")
                        app.size = app.parts[0].size
                        ctx["game_dirs"].append(path)
                    app.how = f"{store} game installed by Heroic."
                    app.method = {"type": "external", "hint": "Remove it in Heroic: open the game, then Uninstall."}
                    app.keys = [norm(title)]
                    app.claims = [path] if path else []
                    apps.append(app)
    return apps


# ───────────────────────── putting it together ─────────────────────────

def data_index():
    idx = {}
    for base in (f"{H}/.config", f"{H}/.local/share", f"{H}/.cache"):
        with contextlib.suppress(OSError):
            for e in os.scandir(base):
                if e.is_dir(follow_symlinks=False):
                    idx.setdefault(norm(e.name), []).append(e.path)
    with contextlib.suppress(OSError):
        for e in os.scandir(H):
            if e.name.startswith(".") and e.is_dir(follow_symlinks=False) and e.name not in (".local", ".config", ".cache"):
                idx.setdefault(norm(e.name[1:]), []).append(e.path)
    return idx


def owner_of(path, apps):
    best, best_len = None, -1
    for a in apps:
        for c in a.claims:
            if c and inside(path, c) and len(c) > best_len:
                best, best_len = a, len(c)
    return best


def attach(apps, ctx):
    print("Grouping files that belong together…")
    by_key = {}
    for a in apps:
        for k in a.keys:
            k = norm(k)
            if len(k) >= 3:
                by_key.setdefault(k, []).append(a)
    # launchers you or an installer added (not owned by a package, Flatpak or Snap)
    for e in ctx["launchers"]:
        p = e["_path"]
        if p in ctx["dpkg"]["owner"] or e.get("_claimed") or "/flatpak/exports/" in p or "/snapd/desktop/" in p:
            continue
        tgt = e["_target"] or {}
        a = None
        if tgt.get("kind") == "path" and tgt.get("exists"):
            a = owner_of(tgt["real"], apps) or owner_of(tgt["path"], apps)
        elif tgt.get("kind") == "flatpak":
            a = next((x for x in apps if x.source == "flatpak" and x.key == tgt["id"]), None)
            if not a:
                ctx["broken"].append((e, f"Opens the Flatpak {tgt['id']}, which isn't installed"))
                continue
        elif tgt.get("kind") == "snap":
            a = next((x for x in apps if x.source == "snap" and x.key == tgt["name"]), None)
            if not a and not os.path.exists(f"/snap/{tgt['name']}"):
                ctx["broken"].append((e, f"Opens the Snap {tgt['name']}, which isn't installed"))
                continue
        elif tgt.get("kind") == "wine":
            if not os.path.isdir(tgt["prefix"]):
                ctx["broken"].append((e, f"Opens a Windows app in {tilde(tgt['prefix'])}, which is gone"))
            continue
        elif tgt.get("kind") == "path" and not tgt.get("exists"):
            ctx["broken"].append((e, f"Opens {tilde(tgt.get('path', '?'))}, which doesn't exist"))
            continue
        if not a:
            continue
        a.add(p, "launcher", size=0)
        if e["_visible"]:
            a.in_library = True
            if a.source in ("appimage", "folder", "binary", "opt", "local", "unowned", "homebin", "engine"):
                a.name = e.get("Name") or a.name
                a.desc = a.desc or e.get("Comment", "")
        a.icon = a.icon or resolve_icon(e.get("Icon", ""), ctx["icons"])
        icon = expand(e.get("Icon", ""))
        if icon.startswith(f"{H}/.local/share/icons") or (a.icon and inside(a.icon, f"{H}/.local/share/icons")
                                                          and norm(os.path.basename(a.icon)).startswith(norm(a.name)[:4])):
            a.add(icon if icon.startswith("/") else a.icon, "icon", size=0)
        for k in (e["_id"], e.get("StartupWMClass", ""), e.get("Name", "")):
            if len(norm(k)) >= 3:
                a.keys.append(k)
                by_key.setdefault(norm(k), []).append(a)
    # command links (/usr/local/bin/x -> /opt/X/x, ~/.local/bin/y -> ~/Apps/Y/y)
    for link, tgt in ctx["links"]:
        a = owner_of(tgt, apps)
        if a:
            a.add(link, "command", size=0)
    # background services
    for s in ctx["services"]:
        a = owner_of(s["exe"], apps) if s["exe"] else None
        if not a:
            unit_key = norm(s["unit"].rsplit(".", 1)[0])
            a = (by_key.get(unit_key) or [None])[0]
        role_note = f"{'Starts at boot' if s['state'] == 'enabled' else 'Not started automatically'}: {s['desc'] or s['unit']}"
        if a:
            a.add(s["path"], "service", size=0, root=s["system"], note=role_note)
            continue
        app = App("service", s["path"], s["desc"] or s["unit"], kind="service", location=s["path"])
        app.add(s["path"], "service", size=0, root=s["system"], note=role_note)
        app.how = ("A background service someone added to the system" if s["system"]
                   else "A background service that runs while you're logged in")
        if s["exe"] and not os.path.exists(s["exe"]):
            app.source, app.kind = "broken", "leftover"
            app.desc = f"Tries to run {tilde(s['exe'])}, which doesn't exist"
        app.method = {"type": "files"}
        app.keys = [s["unit"]]
        apps.append(app)
    # /usr/local/lib/x, /usr/share/x … that match an app found elsewhere
    for path, key, role, root in ctx["attachable"]:
        if len(key) < 3:
            continue
        for a in by_key.get(key, [])[:1]:
            if a.source in ("local", "opt", "unowned", "service", "appimage", "folder"):
                a.add(path, role, root=root)
                a.claims.append(path)
    # settings and data in your home folder
    idx = data_index()
    for a in apps:
        if a.protected or a.kind in ("leftover", "runtime", "service") or a.source in ("steam", "lutris", "heroic", "flatpak", "snap"):
            continue
        keys = {norm(k) for k in a.keys if len(norm(k)) >= 3} - DATA_DENY
        paths = set()
        for k in keys:
            paths.update(idx.get(k, []))
            paths.update(expand(p) for p in KNOWN_DATA.get(k, []) if os.path.exists(expand(p)))
        for p in sorted(paths):
            if any(inside(p, c) for c in a.claims) or owner_of(p, [x for x in apps if x is not a and x.claims]):
                continue
            role = "cache" if inside(p, f"{H}/.cache") else ("settings" if inside(p, f"{H}/.config") else "data")
            note = next((v for k, v in SENSITIVE_DATA.items() if os.path.basename(p) == k), "")
            a.add(p, role, root=False, note=note)
            if role != "cache":
                a.last_used = max(a.last_used, newest_mtime(p, 1)) if not a.exe else a.last_used
    # anything big left by well-known apps that are gone
    for path, label, key in ORPHAN_DATA:
        p = expand(path)
        if os.path.isdir(p) and not any(any(inside(p, x.path) or inside(x.path, p) for x in a.parts) for a in apps):
            size = du(p)[0]
            if size >= 50 * MB:
                lo = App("leftover", p, label, kind="leftover", location=p, size=size,
                         desc=f"Data from {label.split(' ')[0]}, which doesn't seem to be installed anymore")
                lo.add(p, "data", size=size)
                lo.how = "Apps usually leave their downloaded models and data behind when you remove them."
                lo.method = {"type": "files"}
                apps.append(lo)
    # broken launchers
    for e, why in ctx["broken"]:
        p = e["_path"]
        if p in ctx["dpkg"]["owner"]:
            continue
        lo = App("broken", p, e.get("Name") or e["_id"], kind="launcher", location=p, desc=why)
        lo.add(p, "launcher", size=0)
        lo.icon = resolve_icon(e.get("Icon", ""), ctx["icons"])
        lo.in_library = e["_visible"]
        lo.how = "A shortcut in your app library that points at something that was removed."
        lo.method = {"type": "files"}
        apps.append(lo)
    for p in ctx["dangling"]:
        lo = App("broken", p, os.path.basename(p), kind="launcher", location=p,
                 desc=f"A command that points at {tilde(os.readlink(p)) if os.path.islink(p) else 'nothing'}, which is gone")
        lo.add(p, "command", size=0)
        lo.how = "Left behind when the program it pointed to was removed."
        lo.method = {"type": "files"}
        apps.append(lo)
    for a in apps:
        a.data_size = sum(p.size for p in a.parts if p.role in ("data", "settings", "cache", "models"))
        prog = sum(p.size for p in a.parts if p.role in ("program", "command", "launcher", "shortcut", "icon", "service"))
        if a.source not in ("apt", "deb", "flatpak", "snap", "steam", "pip"):
            a.size = max(a.size, prog)
    return apps


def mark_running(apps):
    used = in_use_paths()
    for a in apps:
        if a.running or a.source in ("apt", "deb"):
            continue
        progs = [p.path for p in a.parts if p.role == "program"]
        a.running = any(inside(u, p) for u in used for p in progs)


def rules_verdicts(apps):
    by_name = {}
    for a in apps:
        if a.kind in ("app", "game") and a.source not in ("leftover", "broken"):
            by_name.setdefault(norm(a.name), []).append(a)
    now = time.time()
    for a in apps:
        if a.verdict:
            continue
        if a.protected:
            a.verdict, a.reason = "keep", a.protected
        elif a.source == "broken":
            a.verdict, a.reason = "remove", a.desc or "Points at something that's gone"
        elif a.source == "leftover":
            a.verdict, a.reason = "remove", "Left behind by an app that's gone"
        elif any(x.source != a.source for x in by_name.get(norm(a.name), [])):
            others = sorted({SOURCES[x.source][1] for x in by_name[norm(a.name)] if x.source != a.source})
            a.verdict, a.reason = "review", f"Also installed as: {', '.join(others)}. You probably need only one."
        elif a.last_used and (now - a.last_used) > 180 * 86400 and a.by_user:
            months = int((now - a.last_used) / (30 * 86400))
            a.verdict, a.reason = "review", f"Not opened in about {months} months"


def scan(log=print):
    """Scan everything. Returns a list of App."""
    t0 = time.time()
    ctx = {"links": [], "dangling": [], "services": [], "attachable": [], "broken": [], "pip_cmds": set(),
           "game_dirs": [], "claimed_engines": set()}
    print("Reading launchers in your app library…")
    ctx["launchers"] = load_launchers()
    ctx["icons"] = build_icon_index()
    ctx["dpkg"] = dpkg_index()
    try:
        ctx["os_installed"] = os.stat("/var/log/installer").st_mtime
    except OSError:
        ctx["os_installed"] = 0
    apps, problems = [], []

    def safely(label, fn, *args):
        try:
            return fn(*args)
        except Exception as e:  # noqa: BLE001 — report it, keep scanning everything else
            problems.append(f"{label}: {type(e).__name__}: {e}")
            print(f"   Couldn't read {label} ({type(e).__name__}: {e}). Skipping it.")
            return [] if fn is not walk_home else {k: [] for k in ("appimage", "folder", "binary", "engine", "wineprefix", "toolchain")}

    apps += safely("system packages", scan_apt, ctx)
    apps += safely("Flatpak apps", scan_flatpak, ctx)
    apps += safely("Snap packages", scan_snap, ctx)
    apps += safely("/opt", scan_opt, ctx)
    print("Looking for developer tools…")
    apps += safely("pip packages", scan_pip, ctx)
    apps += safely("/usr/local", scan_usr_local, ctx)
    safely("background services", scan_services, ctx)
    apps += safely("pipx tools", scan_pipx, ctx)
    apps += safely("cargo tools", scan_cargo, ctx)
    apps += safely("npm packages", scan_npm, ctx)
    apps += safely("go programs", scan_go, ctx)
    apps += safely("toolchains and engines", scan_toolchains, ctx)
    apps += safely("~/.local/bin", scan_homebin, ctx)
    print("Looking for games…")
    apps += safely("Steam games", scan_steam, ctx)
    apps += safely("Lutris games", scan_lutris, ctx)
    apps += safely("Heroic games", scan_heroic, ctx)
    found = safely("your home folder", walk_home, ctx)
    claimed = [c for a in apps for c in a.claims if c]
    for kind in ("appimage", "folder", "binary", "engine", "toolchain"):
        for path, info in found[kind]:
            if path in ctx["claimed_engines"] or any(inside(path, c) for c in claimed) \
                    or any(inside(path, g) for g in ctx["game_dirs"]):
                continue
            try:
                apps.append(app_from_found(kind, path, info, ctx))
            except OSError as e:
                problems.append(f"{tilde(path)}: {e}")
    apps += safely("Windows apps", scan_wine, ctx, found["wineprefix"])
    apps = attach(apps, ctx)
    for a in apps:
        if not a.icon and a.exe:
            a.icon = resolve_icon(os.path.basename(a.exe), ctx["icons"])
    mark_running(apps)
    for a in apps:  # timestamps copied out of archives aren't real usage
        if a.last_used and (a.last_used < 946684800 or a.last_used > time.time() + 86400):
            a.last_used = 0
    rules_verdicts(apps)
    apps.sort(key=lambda a: (GROUP_ORDER.index(SOURCES[a.source][0]), -(a.size + a.data_size)))
    print(f"Found {len(apps)} apps and tools in {time.time() - t0:.0f} s.")
    if problems:
        print(f"{len(problems)} place{'s' if len(problems) > 1 else ''} couldn't be read; everything else is complete.")
    scan.problems = problems
    return apps


# ───────────────────────── saving & AI review ─────────────────────────

def save(apps):
    SCAN_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {"when": time.time(), "apps": [dict(asdict(a)) for a in apps]}
    tmp = SCAN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(SCAN_FILE)


def load():
    try:
        data = json.loads(SCAN_FILE.read_text())
    except (OSError, ValueError):
        return None, []
    apps = []
    for d in data.get("apps", []):
        parts = [Part(**p) for p in d.pop("parts", [])]
        with contextlib.suppress(TypeError):
            a = App(**d)
            a.parts = parts
            apps.append(a)
    return data.get("when"), apps


AI_PROMPT = """You help a Pop!_OS user (a game developer) decide which installed apps and tools to remove.
For each item you get its name, how it was installed, where it lives, size, days since last used (null = unknown)
and any notes. For each one reply with:
- "what": what it is, in under 14 plain words (e.g. "Code editor from Microsoft", "Command-line JSON tool")
- "verdict": "keep" (system, drivers, dev tools they rely on, things other apps need), "review" (depends on them),
  or "remove" (clearly unused, duplicate, broken, leftover)
- "reason": under 16 words, specific (mention duplicates, how long unused, what breaks if removed)
Never say "remove" for things marked protected. Be conservative with game engines, SDKs and toolchains the user may need.
Reply with ONLY JSON: {"items": [{"id": "...", "what": "...", "verdict": "...", "reason": "..."}]}"""


def ai_review(apps, args):
    from . import popclean
    items = [{"id": a.id, "name": a.name, "installed_as": SOURCES[a.source][1], "where": tilde(a.location),
              "size": human(a.size + a.data_size), "days_since_used": int((time.time() - a.last_used) / 86400) if a.last_used else None,
              "version": a.version, "description": a.desc[:120], "in_app_library": a.in_library,
              "protected": bool(a.protected), "notes": a.notes[:2] + ([a.reason] if a.reason else [])} for a in apps]
    by_id = {a.id: a for a in apps}
    done = 0
    for i in range(0, len(items), 60):
        chunk = items[i:i + 60]
        print(f"Asking {'Claude' if args.ai == 'claude' else 'Ollama'} about items {i + 1}–{i + len(chunk)}…")
        if args.ai == "claude":
            resp = popclean.http_json("https://api.anthropic.com/v1/messages", {
                "model": args.model or popclean.CLAUDE_MODEL, "max_tokens": 8000, "system": AI_PROMPT,
                "messages": [{"role": "user", "content": json.dumps(chunk)}]},
                {"x-api-key": os.environ.get("ANTHROPIC_API_KEY", ""), "anthropic-version": "2023-06-01"}, timeout=300)
            text = "".join(b.get("text", "") for b in resp.get("content", []))
        else:
            resp = popclean.http_json(f"{args.ollama_host.rstrip('/')}/api/chat", {
                "model": args.model or popclean.OLLAMA_MODEL, "stream": False, "format": "json",
                "messages": [{"role": "system", "content": AI_PROMPT}, {"role": "user", "content": json.dumps(chunk)}]},
                {}, timeout=900)
            text = resp.get("message", {}).get("content", "")
        for it in popclean.extract_json(text).get("items", []):
            a = by_id.get(str(it.get("id")))
            v = str(it.get("verdict", "")).lower()
            if not a or v not in ("keep", "review", "remove"):
                continue
            if a.protected and v == "remove":
                v = "keep"
            a.ai = {"what": str(it.get("what", ""))[:120], "verdict": v, "reason": str(it.get("reason", ""))[:160],
                    "by": args.ai}
            done += 1
    print(f"Got advice on {done} items.")
    return done
