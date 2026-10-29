"""organizer — AI-planned, reversible file organization for PopUtil.

The AI (or built-in rules) only proposes moves. This module decides whether each
move is safe, works out the fix-ups that keep things working after the move
(project lists, launchers, services, shell files, bookmarks, shortcuts, Python
virtualenvs), runs them, and journals everything so a run can be undone.
Nothing is ever deleted.
"""
import json
import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

from . import popclean

HOME = Path.home()
H = str(HOME)
STATE_DIR = HOME / ".local/share/poputil/organize"
RUNS = STATE_DIR / "runs"
MB = 1024 ** 2

LINUX_FS = {"ext4", "ext3", "ext2", "btrfs", "xfs", "f2fs", "zfs", "bcachefs", "jfs", "reiserfs"}

SKIP_DIRS = {"node_modules", ".git", "__pycache__", "target", "Library", "build", ".godot", "Intermediate",
             "DerivedDataCache", "venv", ".venv", "steamapps", "drive_c"}
PARTIAL_EXT = (".part", ".crdownload", ".download", ".tmp", ".partial", ".!qB")


def read(p, default=""):
    try:
        return Path(p).read_text(errors="replace")
    except OSError:
        return default


def tilde(p):
    p = str(p)
    return "~" + p[len(H):] if p == H or p.startswith(H + "/") else p


def expand(p):
    p = str(p).strip()
    if p.startswith("~"):
        p = H + p[1:]
    return os.path.normpath(p)


def inside(child, parent):
    child, parent = os.path.normpath(child), os.path.normpath(parent)
    return child == parent or child.startswith(parent.rstrip("/") + "/")


def has_dot_part(p):
    """True if any path component below home (or a drive mount) is hidden."""
    rel = os.path.relpath(p, H) if inside(p, H) else p
    return any(part.startswith(".") and part not in (".", "..") for part in Path(rel).parts)


# ───────────────────────── inventory ─────────────────────────

def xdg_dirs():
    names = {"DESKTOP": "Desktop", "DOWNLOAD": "Downloads", "DOCUMENTS": "Documents", "PICTURES": "Pictures",
             "VIDEOS": "Videos", "MUSIC": "Music", "TEMPLATES": "Templates", "PUBLICSHARE": "Public"}
    out = {k: os.path.join(H, v) for k, v in names.items()}
    for line in read(HOME / ".config/user-dirs.dirs").splitlines():
        m = re.match(r'XDG_(\w+)_DIR="(.*)"', line.strip())
        if m and m.group(1) in out:
            out[m.group(1)] = os.path.normpath(m.group(2).replace("$HOME", H))
    return out


def drives():
    out, seen = [], set()
    home_dev = os.stat(H).st_dev
    for line in read("/proc/mounts").splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        mnt = parts[1].replace("\\040", " ")
        if not (mnt == "/" or mnt.startswith(("/media/", "/mnt/", "/run/media/"))):
            continue
        if parts[2] in ("tmpfs", "squashfs", "overlay", "proc", "sysfs", "devtmpfs", "autofs", "fuse.portal"):
            continue
        try:
            st, vfs = os.stat(mnt), os.statvfs(mnt)
        except OSError:
            continue
        if st.st_dev in seen:
            continue
        seen.add(st.st_dev)
        total, free = vfs.f_blocks * vfs.f_frsize, vfs.f_bavail * vfs.f_frsize
        out.append({"mount": mnt, "fs": parts[2], "total": total, "free": free,
                    "used_pct": round(100 * (1 - free / total)) if total else 0,
                    "holds_home": st.st_dev == home_dev,
                    "writable": os.access(mnt, os.W_OK) and mnt != "/"})
    return out


def project_kind(names, path):
    if "project.godot" in names:
        return "Godot game"
    if {"Assets", "ProjectSettings"} <= names:
        return "Unity game"
    if any(n.endswith(".uproject") for n in names):
        return "Unreal game"
    if names & {"settings.gradle", "settings.gradle.kts"} or ("gradlew" in names and "app" in names):
        return "Android app"
    if "Cargo.toml" in names:
        return "Rust project"
    if "package.json" in names:
        return "Web/Node project"
    if names & {"pyproject.toml", "setup.py", "requirements.txt"}:
        return "Python project"
    if ".git" in names:
        return "Git repo"
    return None


GAME_KINDS = {"Godot game", "Unity game", "Unreal game"}


def sensitive_reason(path):
    """Things that break when moved by hand; these may only be relocated (moved + linked back) or left alone."""
    p = path.replace("\\", "/")
    names = set()
    try:
        names = set(os.listdir(path)) if os.path.isdir(path) else set()
    except OSError:
        pass
    if "steamapps" in names or "libraryfolder.vdf" in names or "/steamapps" in p:
        return "Steam library: move it with Steam > Settings > Storage instead"
    if "drive_c" in names and "system.reg" in names:
        return "Wine/Proton prefix: move it from Lutris, Bottles or Heroic instead"
    if "/Unity/Hub/Editor" in p or p.endswith("/Unity/Hub"):
        return "Unity editors: change the location in Unity Hub > Preferences"
    if re.search(r"/Android/Sdk($|/)", p):
        return "Android SDK: Android Studio points at this path"
    if path.endswith(".AppImage") and is_in_use(path):
        return "AppImage is running"
    return None


def in_use_paths():
    """cwd and executable of every process we can see."""
    used = set()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        for link in ("cwd", "exe"):
            try:
                used.add(os.readlink(f"/proc/{pid}/{link}"))
            except OSError:
                pass
    return used


_USED = {"t": 0, "v": set()}


def is_in_use(path):
    if time.time() - _USED["t"] > 5:
        _USED.update(t=time.time(), v=in_use_paths())
    return any(inside(u, path) for u in _USED["v"])


def type_summary(path, cap=3000):
    counts, n = {}, 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            ext = os.path.splitext(f)[1].lower() or "(none)"
            counts[ext] = counts.get(ext, 0) + 1
            n += 1
            if n >= cap:
                break
        if n >= cap:
            break
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:6]
    return {"files": n if n < cap else f"{cap}+", "types": dict(top)}


def find_projects(roots, max_depth=5, progress=None):
    projects, links = [], []
    for root, base_depth in roots:
        stack = [(root, 0)]
        while stack:
            d, depth = stack.pop()
            try:
                entries = list(os.scandir(d))
            except OSError:
                continue
            names = {e.name for e in entries}
            kind = project_kind(names, d) if d != root else None
            if kind:
                projects.append({"path": d, "kind": kind, "git": ".git" in names})
                continue
            for e in entries:
                try:
                    if e.is_symlink():
                        tgt = os.path.normpath(os.path.join(d, os.readlink(e.path)))
                        links.append((e.path, tgt))
                        continue
                    if e.is_dir() and not e.name.startswith(".") and e.name not in SKIP_DIRS and depth < max_depth:
                        stack.append((e.path, depth + 1))
                except OSError:
                    continue
            if progress and len(stack) % 200 == 0:
                progress(d)
    # shortcuts that live in hidden places but point at your files
    for extra in (HOME / ".local/bin", HOME / "bin"):
        if extra.is_dir():
            for e in os.scandir(extra):
                if e.is_symlink():
                    links.append((e.path, os.path.normpath(os.path.join(str(extra), os.readlink(e.path)))))
    return projects, links


def inventory(log=print):
    xdg = xdg_dirs()
    drv = drives()
    log("Mapping your home folder and drives…")
    roots = [(H, 0)] + [(d["mount"], 0) for d in drv if d["writable"] and not d["holds_home"]]
    projects, links = find_projects(roots)
    proj_paths = {p["path"]: p for p in projects}
    log(f"Found {len(projects)} projects.")

    standard = {v for k, v in xdg.items()}
    looks = [H, xdg["DESKTOP"], xdg["DOWNLOAD"], xdg["DOCUMENTS"], xdg["PICTURES"], xdg["VIDEOS"], xdg["MUSIC"]]
    items, seen = [], set()
    now = time.time()
    for folder in dict.fromkeys(looks):
        if not os.path.isdir(folder):
            continue
        try:
            entries = sorted(os.scandir(folder), key=lambda e: e.name.lower())
        except OSError:
            continue
        for e in entries:
            if e.name.startswith(".") or e.path in standard or e.path in seen:
                continue
            seen.add(e.path)
            try:
                if e.is_symlink():
                    continue
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            is_dir = stat.S_ISDIR(st.st_mode)
            size, newest = popclean.du(e.path) if is_dir else (st.st_blocks * 512, st.st_mtime)
            item = {"path": tilde(e.path), "in": tilde(folder), "kind": "folder" if is_dir else "file",
                    "size": popclean.human(size), "bytes": size,
                    "modified_days": round((now - newest) / 86400, 1)}
            if is_dir:
                if e.path in proj_paths:
                    item["project"] = proj_paths[e.path]["kind"]
                else:
                    item.update(type_summary(e.path))
                    inner = [p["kind"] for p in projects if inside(p["path"], e.path)]
                    if inner:
                        item["contains_projects"] = len(inner)
            else:
                item["ext"] = os.path.splitext(e.name)[1].lower()
            if is_in_use(e.path):
                item["in_use"] = True
            if sensitive_reason(e.path):
                item["note"] = sensitive_reason(e.path)
            items.append(item)
            if len(items) >= 500:
                break
    log(f"Looked at {len(items)} loose items.")

    tree = []
    for e in sorted(os.scandir(H), key=lambda e: e.name.lower()):
        if e.name.startswith(".") or not e.is_dir(follow_symlinks=False):
            continue
        subs = []
        try:
            subs = sorted(x.name for x in os.scandir(e.path) if x.is_dir(follow_symlinks=False) and not x.name.startswith("."))
        except OSError:
            pass
        tree.append({"folder": tilde(e.path), "subfolders": subs[:30] + (["…"] if len(subs) > 30 else [])})

    return {
        "home": H, "standard_folders": {k.lower(): tilde(v) for k, v in xdg.items()},
        "drives": [{**d, "total": popclean.human(d["total"]), "free": popclean.human(d["free"]),
                    "free_bytes": d["free"]} for d in drv],
        "tree": tree[:120], "items": items,
        "projects": [{"path": tilde(p["path"]), "kind": p["kind"], "git": p["git"]} for p in projects][:300],
        "_links": links,
    }


# ───────────────────────── planning ─────────────────────────

PROMPT = """You organize a Pop!_OS user's files. They are a game developer (Godot, Unity, Android apps, web apps).
You get a JSON inventory of their home folder and drives. Propose a tidy, conventional layout using the fewest moves that make a real difference.

Default layout (if the user already has a folder for something, use theirs instead):
- Projects in ~/Projects: games in ~/Projects/Games, other code in ~/Projects/Apps. Keep each project folder whole.
- Screenshots in ~/Pictures/Screenshots, screen recordings in ~/Videos/Recordings.
- Installers and archives in ~/Software/Installers, APKs in ~/Software/APKs.
- Loose game assets in ~/Projects/Assets/Art, /Audio, /3D or /Fonts.
- Documents grouped by purpose inside ~/Documents.

Rules:
- "src" must be an exact path from the inventory "items" or "projects". Move whole items. Never move something into itself.
- "dst_dir" is the folder the item goes INTO; the item keeps its own name. Moving ~/Documents/Games to ~/Projects means dst_dir "~/Projects" (result: ~/Projects/Games).
- To fold a folder's contents into an existing folder (e.g. ~/Documents/Music into ~/Music), use "merge": true with dst_dir set to that existing folder.
- Never invent category folders from project types; keep project folder names as they are.
- Never use hidden (dot) paths. Never move the standard folders themselves, except with "relocate".
- Skip items with in_use, a "note", or modified_days under 1.
- Leave things that are already sensibly placed. Don't reorganize inside existing projects.
- "relocate" moves an item to another drive and leaves a link at the old path so nothing breaks. Use it only when the drive holding home is over 80% full or the user asks, only to drives with writable=true and enough free space, and only for big media or project folders.
- Never relocate projects or code to drives whose fs is vfat, exfat, ntfs or fuseblk: they can't store Linux links or permissions. Those drives are fine for videos, music and pictures.
- The user's instructions, if any, take priority over these defaults.

Reply with ONLY JSON:
{"summary": "2-3 plain sentences on what changes and why",
 "ops": [{"op": "move", "src": "~/…", "dst_dir": "~/…", "reason": "under 15 words"},
         {"op": "move", "src": "~/Documents/Music", "dst_dir": "~/Music", "merge": true, "reason": "under 15 words"},
         {"op": "relocate", "src": "~/…", "dst_dir": "/media/…/…", "reason": "under 15 words"}]}"""


def ai_plan(inv, args, instructions=""):
    payload = {k: v for k, v in inv.items() if not k.startswith("_")}
    for it in payload["items"]:
        it.pop("bytes", None)
    user = json.dumps(payload)
    if instructions.strip():
        user = f"User's instructions: {instructions.strip()}\n\nInventory:\n{user}"
    if args.ai == "claude":
        resp = popclean.http_json("https://api.anthropic.com/v1/messages", {
            "model": args.model or popclean.CLAUDE_MODEL, "max_tokens": 16000, "system": PROMPT,
            "messages": [{"role": "user", "content": user}]},
            {"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"}, timeout=300)
        text = "".join(b.get("text", "") for b in resp.get("content", []))
    else:
        resp = popclean.http_json(f"{args.ollama_host.rstrip('/')}/api/chat", {
            "model": args.model or popclean.OLLAMA_MODEL, "stream": False, "format": "json",
            "messages": [{"role": "system", "content": PROMPT}, {"role": "user", "content": user}]}, {}, timeout=900)
        text = resp.get("message", {}).get("content", "")
    data = popclean.extract_json(text)
    return data.get("summary", ""), data.get("ops", [])


EXT_RULES = [
    ((".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".heic", ".svg"), "~/Pictures"),
    ((".mp4", ".mkv", ".webm", ".mov", ".avi"), "~/Videos"),
    ((".mp3", ".flac", ".m4a", ".opus"), "~/Music"),
    ((".wav", ".ogg"), "~/Projects/Assets/Audio"),
    ((".blend", ".fbx", ".obj", ".glb", ".gltf", ".dae", ".stl"), "~/Projects/Assets/3D"),
    ((".ase", ".aseprite", ".kra", ".psd", ".xcf", ".pxo", ".tmx", ".ldtk"), "~/Projects/Assets/Art"),
    ((".ttf", ".otf", ".woff", ".woff2"), "~/Projects/Assets/Fonts"),
    ((".apk", ".aab"), "~/Software/APKs"),
    ((".deb", ".appimage", ".iso", ".img", ".run", ".exe", ".msi", ".zip", ".7z", ".rar", ".tar", ".gz", ".xz", ".tgz", ".bz2"), "~/Software/Installers"),
    ((".pdf", ".doc", ".docx", ".odt", ".rtf", ".txt", ".md", ".xlsx", ".xls", ".ods", ".csv", ".pptx", ".odp", ".epub"), "~/Documents"),
]


def rules_plan(inv):
    std = {k: v for k, v in inv["standard_folders"].items()}
    ops = []
    for it in inv["items"]:
        if it.get("in_use") or it.get("note") or it["modified_days"] < 1:
            continue
        src, parent = it["path"], it["in"]
        if it.get("project"):
            if inside(expand(src), expand("~/Projects")):
                continue
            dst = "~/Projects/Games" if it["project"] in GAME_KINDS else "~/Projects/Apps"
            ops.append({"op": "move", "src": src, "dst_dir": dst, "reason": f"{it['project']} in with your other projects"})
            continue
        if it["kind"] != "file" or parent in (std.get("documents"), std.get("music")):
            continue
        name = os.path.basename(src).lower()
        dst = None
        if name.startswith(("screenshot", "screen shot")) and it["ext"] in (".png", ".jpg", ".jpeg", ".webp"):
            dst = "~/Pictures/Screenshots"
        elif name.startswith(("screencast", "recording", "screen recording")) and it["ext"] in (".mp4", ".webm", ".mkv"):
            dst = "~/Videos/Recordings"
        else:
            for exts, d in EXT_RULES:
                if it["ext"] in exts:
                    dst = d
                    break
        if dst and parent in (std.get("pictures"), std.get("videos")) and dst in ("~/Pictures", "~/Videos"):
            continue  # already where it belongs
        if dst:
            ops.append({"op": "move", "src": src, "dst_dir": dst, "reason": f"{it['ext'] or 'file'} files live in {dst}"})
    return "Rule-based plan: loose files sorted by type and projects gathered into ~/Projects.", ops


# ───────────────────────── fix-ups ─────────────────────────

REF_FILES = [
    (".config/godot/projects.cfg", "Godot project list"),
    (".var/app/org.godotengine.Godot/config/godot/projects.cfg", "Godot project list"),
    (".config/UnityHub/projects-v1.json", "Unity Hub project list"),
    (".config/gtk-3.0/bookmarks", "file manager bookmarks"),
    (".bashrc", "shell startup file"), (".bash_aliases", "shell startup file"),
    (".profile", "shell startup file"), (".zshrc", "shell startup file"),
]
REF_GLOBS = [
    (".local/share/applications", "*.desktop", "app launcher"),
    (".config/autostart", "*.desktop", "autostart entry"),
    (".config/systemd/user", "*.service", "background service"),
    (".config/systemd/user", "*.timer", "background service"),
]


def ref_files():
    out = []
    for rel, label in REF_FILES:
        p = HOME / rel
        if p.is_file():
            out.append((str(p), label))
    for d, pat, label in REF_GLOBS:
        for p in sorted((HOME / d).glob(pat)) if (HOME / d).is_dir() else []:
            if p.is_file() and not p.is_symlink():
                out.append((str(p), label))
    return out


def _pat(old):
    return re.compile(re.escape(old) + r"(?=$|[/\"'\]\s:;,)=])", re.M)


def refers_to(text, old):
    return bool(_pat(old).search(text) or _pat(quote(old, safe="/")).search(text))


def rewrite(text, old, new):
    text = _pat(old).sub(lambda m: new, text)
    return _pat(quote(old, safe="/")).sub(lambda m: quote(new, safe="/"), text)


def venvs_in(path, depth=3):
    found = []
    if not os.path.isdir(path):
        return found
    base = path.rstrip("/").count("/")
    for root, dirs, files in os.walk(path):
        if "pyvenv.cfg" in files:
            found.append(root)
            dirs[:] = []
            continue
        if root.count("/") - base >= depth:
            dirs[:] = []
        dirs[:] = [d for d in dirs if d not in ("node_modules", ".git", "Library", "target")]
    return found


def plan_fixups(src, links, refs=None, relocate=False):
    """What has to be updated after moving src, as human-readable strings."""
    out = []
    for f, label in refs if refs is not None else ref_files():
        if refers_to(read(f), src):
            out.append(f"{label} ({os.path.basename(f)})")
    n = sum(1 for link, tgt in links if inside(tgt, src) and not inside(link, src))
    if n:
        out.append(f"{n} shortcut link{'s' if n > 1 else ''}")
    v = venvs_in(src) if not relocate else []
    if v:
        out.append(f"Python virtualenv paths ({len(v)})")
    return out


# ───────────────────────── validation ─────────────────────────

def has_links(path, cap=200000):
    n = 0
    for root, dirs, files in os.walk(path):
        for x in dirs + files:
            n += 1
            if os.path.islink(os.path.join(root, x)):
                return True
        if n > cap:
            return True  # too big to be sure; treat as unsafe for a non-Linux drive
    return False


def project_inside(path, inv):
    if not os.path.isdir(path):
        return False
    try:
        if project_kind(set(os.listdir(path)), path):
            return True
    except OSError:
        pass
    return any(inside(expand(p["path"]), path) for p in inv["projects"])


def allowed_roots(inv):
    return [H] + [d["mount"] for d in inv["drives"] if d["writable"] and not d["holds_home"]]


def unique_dest(dst):
    if not os.path.lexists(dst):
        return dst
    stem, ext = (dst, "") if os.path.isdir(dst) else os.path.splitext(dst)
    i = 2
    while os.path.lexists(f"{stem} ({i}){ext}"):
        i += 1
    return f"{stem} ({i}){ext}"


def validate(raw_ops, inv):
    roots = allowed_roots(inv)
    std = {expand(v) for v in inv["standard_folders"].values()} | {H}
    sizes = {expand(i["path"]): i.get("bytes", 0) for i in inv["items"]}
    drv = {d["mount"]: d for d in inv["drives"]}
    refs = ref_files()
    out, used_srcs, planned = [], set(), set()
    for raw in raw_ops:
        if not isinstance(raw, dict):
            continue
        op = raw.get("op", "move")
        src, dst_dir = expand(raw.get("src", "")), expand(raw.get("dst_dir", ""))
        o = {"op": op if op in ("move", "relocate") else "move", "src": src, "dst_dir": dst_dir,
             "reason": str(raw.get("reason", ""))[:140], "warnings": [], "blocked": None, "fixups": []}
        name = os.path.basename(src.rstrip("/"))
        o["dst"] = os.path.join(dst_dir, name)
        o["merge"] = False
        if raw.get("src") and raw.get("dst_dir"):
            same_name = os.path.basename(dst_dir.rstrip("/")) == name
            if raw.get("merge") or (same_name and os.path.isdir(dst_dir) and os.path.isdir(src)):
                # fold the folder's contents into an existing folder instead of nesting it
                o["merge"], o["dst"] = True, dst_dir
            elif same_name:
                # the plan gave the full target path; don't create Name/Name
                o["dst"], o["dst_dir"] = dst_dir, os.path.dirname(dst_dir)
                dst_dir = o["dst_dir"]

        def block(msg):
            if not o["blocked"]:
                o["blocked"] = msg

        if not raw.get("src") or not raw.get("dst_dir"):
            block("Plan step is missing a path")
        elif has_dot_part(src) or has_dot_part(dst_dir):
            block("Hidden app data isn't moved")
        elif not os.path.lexists(src):
            block("Doesn't exist anymore")
        elif src in used_srcs:
            block("Already moved by another step")
        elif not any(inside(src, r) for r in roots) or not any(inside(dst_dir, r) for r in roots):
            block("Outside your home folder and drives")
        elif src in std and o["op"] != "relocate":
            block("Standard folders stay put")
        elif has_dot_part(src) or has_dot_part(dst_dir):
            block("Hidden app data isn't moved")
        elif inside(dst_dir, src):
            block("Can't move a folder into itself")
        elif (o["merge"] and src == o["dst"]) or (not o["merge"] and os.path.dirname(src) == dst_dir):
            block("Already there")
        elif o["merge"] and not os.path.isdir(src):
            block("Only folders can be merged")
        elif o["merge"] and o["op"] == "relocate":
            block("Can't merge onto another drive")
        elif os.path.islink(src):
            block("Shortcut links aren't moved")
        elif name.lower().endswith(PARTIAL_EXT):
            block("Still downloading")
        if not o["blocked"]:
            why = sensitive_reason(src)
            if why and o["op"] == "move":
                block(why)
            if is_in_use(src):
                block("In use by a running program")
            if any(inside(src, p) or inside(p, src) for p in planned if p != src):
                block("Overlaps with another step")
        if not o["blocked"] and o["op"] == "relocate":
            mount = next((m for m in sorted(drv, key=len, reverse=True) if inside(dst_dir, m)), None)
            if not mount or not drv[mount]["writable"]:
                block("Target drive isn't writable")
            elif os.stat(src).st_dev == os.stat(mount).st_dev:
                block("Target is on the same drive")
            elif drv[mount]["fs"] not in LINUX_FS and (project_inside(src, inv) or has_links(src)):
                block(f"{mount} is {drv[mount]['fs']}, which can't store Linux links and permissions; projects and code need an ext4 or btrfs drive")
            else:
                if drv[mount]["fs"] not in LINUX_FS:
                    o["warnings"].append(f"{drv[mount]['fs']} drive: Linux file permissions aren't kept")
                if mount.startswith(("/media/", "/run/media/")):
                    o["warnings"].append("Removable drive: the link stops working while it's unplugged")
                need = sizes.get(src) or popclean.du(src)[0]
                if need > drv[mount]["free_bytes"] * 0.95:
                    block(f"Not enough space on {mount}")
        if not o["blocked"]:
            try:
                age = (time.time() - popclean.du(src)[1]) / 86400 if os.path.isdir(src) else (time.time() - os.lstat(src).st_mtime) / 86400
                if age < 1:
                    o["warnings"].append("Changed in the last day")
            except OSError:
                pass
            if o["merge"]:
                clashes = [c for c in os.listdir(src) if os.path.lexists(os.path.join(o["dst"], c))]
                if clashes:
                    o["warnings"].append(f"{len(clashes)} name clash{'es' if len(clashes) > 1 else ''}; those get a (2) suffix")
            elif os.path.lexists(o["dst"]):
                o["dst"] = unique_dest(o["dst"])
                o["warnings"].append(f"Name taken; will be saved as {os.path.basename(o['dst'])}")
            if o["op"] == "relocate":
                o["fixups"] = []
            else:
                o["fixups"] = plan_fixups(src, inv["_links"], refs)
            if any(f.startswith("Godot") for f in o["fixups"]) and any("godot" in u.lower() for u in _USED["v"]):
                o["warnings"].append("Close Godot first so it doesn't overwrite its project list")
            used_srcs.add(src)
            planned.add(src)
        out.append(o)
    for i, o in enumerate(out, 1):
        o["id"] = i
        o["src_t"], o["dst_t"] = tilde(o["src"]), tilde(o["dst"])
    return out


# ───────────────────────── apply & undo ─────────────────────────

def tree_stats(p):
    if os.path.isfile(p) or os.path.islink(p):
        return 1, os.lstat(p).st_size
    n = size = 0
    for root, dirs, files in os.walk(p):
        for f in files:
            try:
                size += os.lstat(os.path.join(root, f)).st_size
                n += 1
            except OSError:
                pass
    return n, size


def _copy_lenient(s, d):
    """copy2, but don't fail when the target drive can't store permissions/timestamps (FAT, exFAT, NTFS)."""
    try:
        return shutil.copy2(s, d)
    except PermissionError:
        return shutil.copyfile(s, d)


def move_path(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.lstat(src).st_dev == os.stat(os.path.dirname(dst)).st_dev:
        os.rename(src, dst)
        return
    # Different drive: copy, verify, then remove the original
    before = tree_stats(src)
    try:
        if os.path.isdir(src) and not os.path.islink(src):
            shutil.copytree(src, dst, symlinks=True, copy_function=_copy_lenient)
        else:
            shutil.copy2(src, dst, follow_symlinks=False)
    except (OSError, shutil.Error) as e:
        if os.path.lexists(dst):  # never leave a half copy behind
            shutil.rmtree(dst, ignore_errors=True) if os.path.isdir(dst) and not os.path.islink(dst) else os.unlink(dst)
        if isinstance(e, shutil.Error) and e.args and isinstance(e.args[0], list):
            errs = e.args[0]
            raise OSError(f"{len(errs)} item(s) couldn't be copied to that drive (first: {tilde(errs[0][0])}: "
                          f"{str(errs[0][2]).split(':')[0]}); the partial copy was removed and the original is untouched")
        raise OSError(f"copy failed ({e}); the partial copy was removed and the original is untouched")
    after = tree_stats(dst)
    if before != after:
        shutil.rmtree(dst, ignore_errors=True) if os.path.isdir(dst) else os.unlink(dst)
        raise OSError(f"copy check failed ({before} vs {after}); original left untouched")
    shutil.rmtree(src) if os.path.isdir(src) and not os.path.islink(src) else os.unlink(src)


class Journal:
    def __init__(self, summary):
        self.id = time.strftime("%Y%m%d-%H%M%S")
        self.dir = RUNS / self.id
        (self.dir / "backups").mkdir(parents=True, exist_ok=True)
        self.data = {"id": self.id, "when": time.time(), "summary": summary, "ops": [], "undone": False}
        self.nb = 0
        self.save()

    def save(self):
        (RUNS / f"{self.id}.json").write_text(json.dumps(self.data, indent=1))

    def start(self, desc):
        self.data["ops"].append({"desc": desc, "actions": [], "ok": False})
        self.save()

    def act(self, **a):
        self.data["ops"][-1]["actions"].append(a)
        self.save()

    def backup(self, path):
        self.nb += 1
        b = self.dir / "backups" / f"{self.nb}-{os.path.basename(path)}"
        shutil.copy2(path, b)
        return str(b)


def _fix_refs(j, old, new, refs_changed):
    for f, label in ref_files():
        text = read(f)
        if refers_to(text, old):
            b = j.backup(f)
            Path(f).write_text(rewrite(text, old, new))
            j.act(t="edit", path=f, backup=b)
            refs_changed.add(label)
            print(f"   updated {label}: {tilde(f)}")


def _fix_links(j, links, old, new):
    for link, tgt in links:
        if inside(tgt, old) and not inside(link, old) and os.path.islink(link):
            new_tgt = new + tgt[len(old):]
            old_raw = os.readlink(link)
            os.unlink(link)
            os.symlink(new_tgt, link)
            j.act(t="relink", link=link, old=old_raw, new=new_tgt)
            print(f"   repointed shortcut {tilde(link)}")


def _fix_venvs(j, new, old):
    for v in venvs_in(new):
        for f in list(Path(v, "bin").glob("*")) + [Path(v, "pyvenv.cfg")]:
            try:
                if f.is_symlink() or not f.is_file() or f.stat().st_size > 2 * MB:
                    continue
                text = f.read_text()
            except (OSError, UnicodeDecodeError):
                continue
            if refers_to(text, old):
                b = j.backup(str(f))
                f.write_text(rewrite(text, old, new))
                j.act(t="edit", path=str(f), backup=b)
        print(f"   repaired virtualenv {tilde(v)}")


def post_commands(labels):
    cmds = []
    if "background service" in labels:
        cmds.append(["systemctl", "--user", "daemon-reload"])
    if "app launcher" in labels or "autostart entry" in labels:
        cmds.append(["update-desktop-database", str(HOME / ".local/share/applications")])
    for c in cmds:
        if not shutil.which(c[0]):
            continue
        print(f"   $ {' '.join(c)}")
        try:
            subprocess.run(c, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"   (couldn't run {c[0]}: {e})")


def apply(ops, summary, links):
    j = Journal(summary)
    refs_changed, ok_all = set(), True
    for o in ops:
        if o.get("blocked"):
            continue
        src, dst = o["src"], o["dst"]
        if o.get("merge"):
            print(f"Merging: contents of {tilde(src)} into {tilde(dst)}")
        else:
            print(f"{'Moving to drive' if o['op'] == 'relocate' else 'Moving'}: {tilde(src)} becomes {tilde(dst)}")
        j.start(f"{tilde(src)} to {tilde(dst)}")
        try:
            if not os.path.lexists(src):
                raise OSError("it's gone")
            if is_in_use(src):
                raise OSError("a running program is using it")
            if os.path.lexists(dst) and not o.get("merge"):
                dst = unique_dest(dst)
            # record folders we create so undo can remove them
            missing, d = [], os.path.dirname(dst)
            while not os.path.exists(d):
                missing.append(d)
                d = os.path.dirname(d)
            for d in reversed(missing):
                os.mkdir(d)
                j.act(t="mkdir", path=d)
            if o.get("merge"):
                if not os.path.isdir(dst):
                    os.mkdir(dst)
                    j.act(t="mkdir", path=dst)
                for child in sorted(os.listdir(src)):
                    c_src = os.path.join(src, child)
                    c_dst = unique_dest(os.path.join(dst, child))
                    move_path(c_src, c_dst)
                    j.act(t="move", src=c_src, dst=c_dst)
                    if os.path.basename(c_dst) != child:
                        print(f"   {child} saved as {os.path.basename(c_dst)} (name clash)")
                        _fix_links(j, links, c_src, c_dst)
                        _fix_refs(j, c_src, c_dst, refs_changed)
                    _fix_venvs(j, c_dst, c_src)
                os.rmdir(src)
                j.act(t="rmdir", path=src)
                _fix_links(j, links, src, dst)
                _fix_refs(j, src, dst, refs_changed)
                j.data["ops"][-1]["ok"] = True
                j.save()
                continue
            move_path(src, dst)
            j.act(t="move", src=src, dst=dst)
            if o["op"] == "relocate":
                os.symlink(dst, src)
                j.act(t="symlink", path=src, target=dst)
                print("   left a link at the old location")
            else:  # the link keeps old paths working for relocations; plain moves need fix-ups
                _fix_links(j, links, src, dst)
                _fix_refs(j, src, dst, refs_changed)
                _fix_venvs(j, dst, src)
            j.data["ops"][-1]["ok"] = True
            j.save()
        except Exception as e:  # noqa: BLE001 — report and continue with the next move
            ok_all = False
            print(f"   skipped: {e}")
    post_commands(refs_changed)
    j.data["refs"] = sorted(refs_changed)
    j.save()
    done = sum(1 for o in j.data["ops"] if o["ok"])
    print(f"Moved {done} of {len(j.data['ops'])}. Undo is available in the history.")
    return ok_all


def history():
    out = []
    for f in sorted(RUNS.glob("*.json"), reverse=True)[:25] if RUNS.is_dir() else []:
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        out.append({"id": d["id"], "when": d["when"], "summary": d.get("summary", ""),
                    "moved": sum(1 for o in d["ops"] if o["ok"]), "undone": d.get("undone", False),
                    "items": [o["desc"] for o in d["ops"] if o["ok"]][:50]})
    return out


def undo(run_id):
    f = RUNS / f"{run_id}.json"
    d = json.loads(f.read_text())
    if d.get("undone"):
        print("Already undone.")
        return True
    ok = True
    labels = set(d.get("refs", []))
    for op in reversed(d["ops"]):
        for a in reversed(op["actions"]):
            try:
                t = a["t"]
                if t == "edit":
                    shutil.copy2(a["backup"], a["path"])
                elif t == "relink":
                    if os.path.islink(a["link"]):
                        os.unlink(a["link"])
                    os.symlink(a["old"], a["link"])
                elif t == "symlink":
                    if os.path.islink(a["path"]):
                        os.unlink(a["path"])
                elif t == "move":
                    if os.path.lexists(a["src"]):
                        raise OSError(f"something new is at {tilde(a['src'])}")
                    move_path(a["dst"], a["src"])
                    print(f"Put back {tilde(a['src'])}")
                elif t == "rmdir":
                    os.makedirs(a["path"], exist_ok=True)
                elif t == "mkdir":
                    try:
                        os.rmdir(a["path"])
                    except OSError:
                        pass  # not empty anymore: keep it
            except Exception as e:  # noqa: BLE001
                ok = False
                print(f"   couldn't undo one step ({a['t']}): {e}")
    post_commands(labels)
    d["undone"] = True
    f.write_text(json.dumps(d, indent=1))
    print("Undo finished." if ok else "Undo finished with some steps skipped (see above).")
    return ok
