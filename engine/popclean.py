#!/usr/bin/env python3
"""popclean — AI-assisted disk cleanup scanner for Pop!_OS.

Scans for things that are usually removable (app caches, build artifacts,
engine caches, old downloads, trash, package caches, logs, big forgotten
files), asks an AI to judge each one, then lets you pick what to remove.
Nothing is touched without your confirmation.

  python3 popclean.py                # scan + AI review (Claude if ANTHROPIC_API_KEY is set)
  python3 popclean.py --ai ollama    # local Ollama model — nothing leaves your machine
  python3 popclean.py --ai none      # rule-based verdicts only
  python3 popclean.py --dry-run      # show what would happen, change nothing
  python3 popclean.py --report out.json

Standard library only. Python 3.8+.
"""
import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path

HOME = Path.home()
NOW = time.time()
DAY = 86400
MB = 1024 ** 2

CLAUDE_MODEL = "claude-sonnet-5-5"
OLLAMA_MODEL = "llama3.1"

# ───────────────────────── helpers ─────────────────────────

TTY = sys.stdout.isatty()


def col(code, s):
    return f"\033[{code}m{s}\033[0m" if TTY else s


def human(n):
    if n <= 0:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


def tilde(p):
    s = str(p)
    h = str(HOME)
    return "~" + s[len(h):] if s == h or s.startswith(h + "/") else s


def du(path):
    """Real disk usage (blocks) and newest mtime under path. Doesn't follow symlinks."""
    try:
        st = os.lstat(path)
    except OSError:
        return 0, 0
    if not stat.S_ISDIR(st.st_mode):
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
                    if s.st_nlink > 1 and not stat.S_ISDIR(s.st_mode):
                        key = (s.st_dev, s.st_ino)
                        if key in seen:
                            continue
                        seen.add(key)
                    total += s.st_blocks * 512
                    newest = max(newest, s.st_mtime)
                    if stat.S_ISDIR(s.st_mode):
                        stack.append(e.path)
        except OSError:
            pass
    return total, newest


def run(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return r.stdout + r.stderr
    except (OSError, subprocess.TimeoutExpired):
        return ""


def status(msg):
    if TTY:
        sys.stderr.write("\r\033[K" + col("2", msg[:110]))
        sys.stderr.flush()


def clear_status():
    if TTY:
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()


# ───────────────────────── candidates ─────────────────────────

@dataclass
class Cand:
    category: str
    path: str               # real path, or a description for command items
    size: int = 0
    mtime: float = 0        # last change (for projects: approx. last project activity)
    kind: str = "regenerable"   # regenerable | user | system
    action: str = "delete"      # delete | trash | empty-trash | command
    command: str = ""
    note: str = ""
    verdict: str = ""
    reason: str = ""
    id: int = 0

    @property
    def age_days(self):
        return int((NOW - self.mtime) / DAY) if self.mtime else None


CACHE_NOTES = {
    "thumbnails": "File-manager thumbnails; regenerated on demand",
    "mesa_shader_cache": "GPU shader cache; games may stutter briefly while it rebuilds",
    "mesa_shader_cache_db": "GPU shader cache; games may stutter briefly while it rebuilds",
    "nvidia": "NVIDIA shader cache; rebuilt automatically",
    "pip": "pip download cache",
    "mozilla": "Firefox web cache (not bookmarks or logins)",
    "google-chrome": "Chrome web cache (not profile data)",
    "chromium": "Chromium web cache",
    "JetBrains": "JetBrains IDE caches/indexes; IDE reindexes",
    "yarn": "Yarn package cache",
    "go-build": "Go build cache",
    "electron": "Electron download cache",
    "typescript": "TypeScript cache",
    "pop-shop": "Pop!_Shop cache",
    "spotify": "Spotify streaming cache",
    "Unity": "Unity editor cache (package/asset downloads)",
    "godot": "Godot editor cache",
}

TOOL_CACHES = [
    ("~/.npm/_cacache", "npm cache", "Packages re-download as needed"),
    ("~/.cargo/registry/cache", "Cargo crate downloads", "Re-downloaded on next build"),
    ("~/.cargo/registry/src", "Cargo extracted sources", "Re-extracted on next build"),
    ("~/.gradle/caches", "Gradle cache", "Re-downloaded; next Android/Gradle build is slow"),
    ("~/.gradle/wrapper/dists", "Gradle wrapper distributions", "Re-downloaded by ./gradlew"),
    ("~/.m2/repository", "Maven repository", "Re-downloaded on next build"),
    ("~/.nuget/packages", "NuGet packages", "Re-downloaded by dotnet restore"),
    ("~/.android/cache", "Android SDK cache", "Re-downloaded as needed"),
    ("~/.local/share/Steam/steamapps/shadercache", "Steam shader pre-cache", "Rebuilt per game; first launches may stutter"),
    ("~/.local/share/godot/export_templates", "Godot export templates", "Needed to export builds; re-downloadable from the editor"),
]

# Build artifacts: (dir name, test(siblings, path) -> bool, label, note)
def _has(*names):
    return lambda s, p: any(n in s for n in names)


ARTIFACT_RULES = [
    ("node_modules", _has("package.json"), "Node.js dependencies", "Reinstall with npm/yarn/pnpm install"),
    ("target", _has("Cargo.toml"), "Rust build output", "Rebuilt by cargo build"),
    ("Library", lambda s, p: {"Assets", "ProjectSettings"} <= s, "Unity Library cache",
     "Unity regenerates it; next open triggers a full (slow) reimport"),
    ("Temp", lambda s, p: {"Assets", "ProjectSettings"} <= s, "Unity Temp folder", "Only used while the editor is open"),
    ("obj", lambda s, p: {"Assets", "ProjectSettings"} <= s, "Unity obj folder", "C# intermediate build files"),
    (".godot", _has("project.godot"), "Godot import cache", "Regenerated when the project is opened"),
    ("Intermediate", lambda s, p: any(x.endswith(".uproject") for x in s), "Unreal Intermediate", "Rebuilt on next compile"),
    ("DerivedDataCache", lambda s, p: any(x.endswith(".uproject") for x in s), "Unreal DerivedDataCache",
     "Rebuilt; shaders recompile on next open"),
    ("build", _has("build.gradle", "build.gradle.kts"), "Gradle/Android build output", "Rebuilt by gradle; old APKs inside are lost"),
    (".gradle", _has("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"),
     "Gradle project cache", "Rebuilt by gradle"),
    (".venv", lambda s, p: os.path.exists(os.path.join(p, "pyvenv.cfg")), "Python virtualenv",
     "Recreate with python -m venv + pip install"),
    ("venv", lambda s, p: os.path.exists(os.path.join(p, "pyvenv.cfg")), "Python virtualenv",
     "Recreate with python -m venv + pip install"),
]
ARTIFACT_NAMES = {r[0] for r in ARTIFACT_RULES}

INSTALLER_EXT = (".deb", ".appimage", ".iso", ".img", ".zip", ".tar.gz", ".tgz", ".tar.xz", ".7z", ".rar", ".exe", ".msi", ".apk", ".run")


def project_activity(parent):
    """Newest mtime of a project's top-level entries, ignoring artifact dirs."""
    newest = 0
    try:
        for e in os.scandir(parent):
            if e.name in ARTIFACT_NAMES:
                continue
            try:
                newest = max(newest, e.stat(follow_symlinks=False).st_mtime)
            except OSError:
                pass
    except OSError:
        pass
    return newest


def scan_home_caches(min_size):
    out = []
    base = HOME / ".cache"
    if base.is_dir():
        for e in os.scandir(base):
            if e.is_symlink():
                continue
            status(f"Sizing {tilde(e.path)}")
            size, mt = du(e.path)
            if size >= min_size:
                out.append(Cand("App cache", e.path, size, mt, note=CACHE_NOTES.get(e.name, "Application cache")))
    # Flatpak per-app caches
    var_app = HOME / ".var" / "app"
    if var_app.is_dir():
        for app in os.scandir(var_app):
            cdir = os.path.join(app.path, "cache")
            if os.path.isdir(cdir) and not os.path.islink(cdir):
                size, mt = du(cdir)
                if size >= min_size:
                    out.append(Cand("Flatpak app cache", cdir, size, mt, note=f"Cache for {app.name}"))
    for p, label, note in TOOL_CACHES:
        full = os.path.expanduser(p)
        if os.path.isdir(full) and not os.path.islink(full):
            status(f"Sizing {p}")
            size, mt = du(full)
            if size >= min_size:
                kind = "user" if "export_templates" in p else "regenerable"
                out.append(Cand(label, full, size, mt, kind=kind, note=note))
    gomod = HOME / "go" / "pkg" / "mod"
    if gomod.is_dir() and shutil.which("go"):
        size, mt = du(str(gomod))
        if size >= min_size:
            out.append(Cand("Go module cache", str(gomod), size, mt, action="command",
                            command="go clean -modcache", note="Read-only files; use go's own cleaner"))
    return out


def scan_trash():
    t = HOME / ".local" / "share" / "Trash"
    if not t.is_dir():
        return []
    size, mt = du(str(t / "files"))
    if size < MB:
        return []
    return [Cand("Trash", str(t), size, mt, kind="user", action="empty-trash",
                 note="Already-deleted files still taking space")]


def scan_downloads(min_size, old_days):
    out = []
    d = HOME / "Downloads"
    if not d.is_dir():
        return out
    for e in os.scandir(d):
        if e.is_symlink():
            continue
        size, mt = du(e.path)
        if size < min_size or (NOW - mt) < old_days * DAY:
            continue
        inst = e.name.lower().endswith(INSTALLER_EXT)
        out.append(Cand("Old download", e.path, size, mt, kind="user", action="trash",
                        note="Installer/archive, probably already used" if inst else "Untouched download"))
    return out


def walk_home(min_artifact, min_large):
    artifacts, large = [], []
    prune = {str(HOME / ".cache"), str(HOME / ".local/share/Trash"), str(HOME / "Downloads"),
             str(HOME / ".var/app"), str(HOME / "go/pkg/mod")}
    prune |= {os.path.expanduser(p) for p, _, _ in TOOL_CACHES}
    stack, n = [str(HOME)], 0
    root_dev = os.lstat(str(HOME)).st_dev
    while stack:
        d = stack.pop()
        n += 1
        if n % 300 == 0:
            status(f"Scanning {tilde(d)}")
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        names = {e.name for e in entries}
        for e in entries:
            try:
                if e.is_symlink():
                    continue
                if e.is_dir():
                    p = e.path
                    if p in prune or e.name == ".git" or (e.name == "common" and d.endswith("steamapps")):
                        continue
                    if e.stat(follow_symlinks=False).st_dev != root_dev:
                        continue  # don't wander onto other drives/mounts
                    hit = next((r for r in ARTIFACT_RULES if r[0] == e.name and r[1](names, p)), None)
                    if hit:
                        artifacts.append((p, hit))
                        continue
                    if e.name == "node_modules":
                        continue
                    stack.append(p)
                elif e.is_file():
                    st = e.stat(follow_symlinks=False)
                    if st.st_blocks * 512 >= min_large:
                        large.append((e.path, st.st_blocks * 512, st.st_mtime))
            except OSError:
                continue
    out = []
    for p, (_, _, label, note) in artifacts:
        status(f"Sizing {tilde(p)}")
        size, _ = du(p)
        if size >= min_artifact:
            proj = os.path.dirname(p)
            out.append(Cand(label, p, size, project_activity(proj),
                            note=f"{note}. Project: {tilde(proj)}"))
    for p, size, mt in large:
        out.append(Cand("Large file", p, size, mt, kind="user", action="trash",
                        note="Big file; could be a VM image, video, export, or archive"))
    return out


def _parse_size(num, unit):
    mult = {"": 1, "B": 1, "K": 1024, "M": MB, "G": 1024 ** 3, "T": 1024 ** 4}
    return int(float(num) * mult.get(unit.upper()[:1] if unit else "", 1))


def scan_system():
    out = []
    size, mt = du("/var/cache/apt/archives")
    if size >= 10 * MB:
        out.append(Cand("APT package cache", "/var/cache/apt/archives", size, mt, kind="system",
                        action="command", command="sudo apt clean",
                        note="Downloaded .deb files for packages already installed"))
    if shutil.which("apt-get"):
        txt = run(["apt-get", "-s", "autoremove"])
        pkgs = [l.split()[1] for l in txt.splitlines() if l.startswith("Remv ")]
        if pkgs:
            more = f" (+{len(pkgs) - 12} more)" if len(pkgs) > 12 else ""
            out.append(Cand("Unused packages", f"{len(pkgs)} packages: " + ", ".join(pkgs[:12]) + more,
                            kind="system", action="command", command="sudo apt autoremove --purge",
                            note="Dependencies nothing installed needs anymore (may include old kernels)"))
    if shutil.which("journalctl"):
        m = re.search(r"take up ([\d.]+)\s*([KMGT]?)", run(["journalctl", "--disk-usage"]))
        if m:
            size = _parse_size(*m.groups())
            if size >= 200 * MB:
                out.append(Cand("System logs (journal)", "/var/log/journal", size, kind="system",
                                action="command", command="sudo journalctl --vacuum-time=2weeks",
                                note="Keeps the last 2 weeks of logs"))
    if shutil.which("flatpak"):
        out.append(Cand("Unused Flatpak runtimes", "Runtimes no installed Flatpak app uses", kind="system",
                        action="command", command="flatpak uninstall --unused",
                        note="Flatpak lists what it will remove and asks before doing it"))
    if shutil.which("docker"):
        txt = run(["docker", "system", "df"])
        if "RECLAIMABLE" in txt.upper():
            out.append(Cand("Docker leftovers", "Stopped containers, dangling images, build cache", kind="system",
                            action="command", command="docker system prune",
                            note="Docker shows what it removes and asks first"))
    size, mt = du("/var/lib/systemd/coredump")
    if size >= 50 * MB:
        out.append(Cand("Crash dumps", "/var/lib/systemd/coredump", size, mt, kind="system", action="command",
                        command="sudo rm -rf /var/lib/systemd/coredump/*", note="Old crash dumps"))
    return out


def scan(args):
    cands = []
    status("Scanning caches…")
    cands += scan_home_caches(args.min_size * MB)
    cands += scan_trash()
    cands += scan_downloads(args.min_size * MB, args.old_days)
    status("Walking home folder…")
    cands += walk_home(args.min_size * MB, args.large * MB)
    if not args.no_system:
        status("Checking system…")
        cands += scan_system()
    clear_status()
    seen, uniq = set(), []
    for c in cands:
        if c.path not in seen:
            seen.add(c.path)
            uniq.append(c)
    uniq.sort(key=lambda c: -c.size)
    for i, c in enumerate(uniq, 1):
        c.id = i
    return uniq


# ───────────────────────── verdicts ─────────────────────────

def heuristic(c):
    age = c.age_days
    if c.category == "Large file":
        return "review", "Personal file — only you know if it's needed"
    if c.category == "Old download":
        if c.path.lower().endswith(INSTALLER_EXT):
            return "safe", f"Installer/archive untouched for {age} days"
        return "review", f"Untouched for {age} days"
    if c.category == "Trash":
        return "safe", "Already in the trash"
    if c.category == "Unused packages":
        return "review", "Usually fine; check the list for anything you want"
    if c.kind == "system":
        return "safe", "Standard system cleanup"
    if c.category == "Godot export templates":
        return "review", "Needed to export; re-download takes a while"
    if c.category == "Python virtualenv" or c.category.startswith("Unity Library") or "Unreal" in c.category:
        if age is not None and age > 60:
            return "safe", f"Project idle {age} days; regenerable"
        return "review", "Active project; regenerating is slow"
    if c.mtime and age is not None and "Project:" in c.note:
        return ("safe", f"Project idle {age} days") if age > 30 else ("review", "Active project; rebuilds needed")
    return "safe", "Regenerable cache"


SYSTEM_PROMPT = """You help a Pop!_OS user (a game developer: Unity/Godot/Unreal, Android builds, web) decide what to delete to free disk space.
You get a JSON list of cleanup candidates from a scanner. For each one give a verdict:
- "safe": regenerable or clearly disposable; removing it loses nothing the user can't easily get back
- "review": probably removable, but it depends on the user (personal files, recent projects, installers they might reuse, slow-to-rebuild caches for active projects)
- "keep": likely important or risky to remove
Rules: never mark personal files (documents, media, project sources, game saves, VM disks, keys, backups) "safe". Weigh size against rebuild cost and how recently the thing was used ("days_since_change"; for build artifacts it's the project's last activity). Name concrete consequences, e.g. "Unity will reimport for ~10 min".
Reply with ONLY JSON, no prose: {"summary": "<2-3 sentences on the biggest wins and anything to be careful about>", "items": [{"id": <int>, "verdict": "safe|review|keep", "reason": "<under 20 words>"}]}"""


def ai_payload(cands):
    return [{"id": c.id, "category": c.category, "path": tilde(c.path), "size": human(c.size),
             "days_since_change": c.age_days, "kind": c.kind, "note": c.note} for c in cands]


def http_json(url, body, headers, timeout=180):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"content-type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def ask_claude(items, model):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY isn't set")
    resp = http_json("https://api.anthropic.com/v1/messages", {
        "model": model, "max_tokens": 8000, "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": json.dumps(items)}],
    }, {"x-api-key": key, "anthropic-version": "2023-06-01"})
    return "".join(b.get("text", "") for b in resp.get("content", []))


def ask_ollama(items, model, host):
    resp = http_json(f"{host.rstrip('/')}/api/chat", {
        "model": model, "stream": False, "format": "json",
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": json.dumps(items)}],
    }, {}, timeout=600)
    return resp.get("message", {}).get("content", "")


def extract_json(text):
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b < a:
        raise ValueError("no JSON in AI reply")
    return json.loads(text[a:b + 1])


def judge(cands, args):
    summaries = []
    if args.ai != "none" and cands:
        batch = 80
        for i in range(0, len(cands), batch):
            chunk = cands[i:i + batch]
            status(f"Asking {args.ai} to review {len(cands)} items…" if len(cands) <= batch
                   else f"Asking {args.ai} (batch {i // batch + 1})…")
            try:
                items = ai_payload(chunk)
                text = ask_claude(items, args.model or CLAUDE_MODEL) if args.ai == "claude" \
                    else ask_ollama(items, args.model or OLLAMA_MODEL, args.ollama_host)
                data = extract_json(text)
                by_id = {c.id: c for c in chunk}
                for it in data.get("items", []):
                    c = by_id.get(it.get("id"))
                    v = str(it.get("verdict", "")).lower()
                    if c and v in ("safe", "review", "keep"):
                        c.verdict, c.reason = v, str(it.get("reason", ""))[:160]
                if data.get("summary"):
                    summaries.append(data["summary"])
            except (urllib.error.URLError, urllib.error.HTTPError, ValueError, RuntimeError, OSError) as e:
                clear_status()
                detail = e.read().decode(errors="replace")[:300] if isinstance(e, urllib.error.HTTPError) else e
                print(col("33", f"AI review failed ({detail}); using built-in rules for these items."))
        clear_status()
    for c in cands:
        if not c.verdict:
            c.verdict, c.reason = heuristic(c)
        # Hard guardrail: personal files are never auto-"safe"
        if c.category == "Large file" and c.verdict == "safe":
            c.verdict = "review"
    return " ".join(summaries)


# ───────────────────────── UI ─────────────────────────

VCOL = {"safe": "32", "review": "33", "keep": "31"}
ORDER = {"safe": 0, "review": 1, "keep": 2}


def show(cands, summary):
    cands.sort(key=lambda c: (ORDER[c.verdict], -c.size))
    for i, c in enumerate(cands, 1):
        c.id = i
    print()
    print(col("1", f"{'#':>3}  {'VERDICT':<7} {'SIZE':>9}  {'AGE':>6}  WHAT"))
    last = None
    for c in cands:
        if c.verdict != last:
            tot = sum(x.size for x in cands if x.verdict == c.verdict)
            print(col(VCOL[c.verdict] + ";1", f"── {c.verdict.upper()} ({human(tot)}) " + "─" * 40))
            last = c.verdict
        age = f"{c.age_days}d" if c.age_days is not None else "—"
        where = tilde(c.path) if c.action != "command" else c.path
        print(f"{c.id:>3}  {col(VCOL[c.verdict], f'{c.verdict:<7}')} {human(c.size):>9}  {age:>6}  "
              f"{col('1', c.category)}  {col('2', where)}")
        extra = f"  → runs: {c.command}" if c.command else ""
        print(f"{'':>30}{c.reason}{col('36', extra)}")
    total_safe = sum(c.size for c in cands if c.verdict == "safe")
    print()
    if summary:
        print(col("1", "AI summary: ") + summary)
    print(col("1", f"Safe to reclaim now: ~{human(total_safe)}") + col("2", "  (command items show '?' until they run)"))


def parse_selection(s, cands):
    picked = {}
    for part in re.split(r"[,\s]+", s.strip().lower()):
        if not part:
            continue
        if part in ("safe", "review"):
            for c in cands:
                if c.verdict == part:
                    picked[c.id] = c
        elif re.fullmatch(r"\d+-\d+", part):
            a, b = map(int, part.split("-"))
            for c in cands:
                if a <= c.id <= b:
                    picked[c.id] = c
        elif part.isdigit():
            for c in cands:
                if c.id == int(part):
                    picked[c.id] = c
        else:
            raise ValueError(part)
    return sorted(picked.values(), key=lambda c: c.id)


PROTECTED = {str(HOME / p) for p in ("", ".ssh", ".gnupg", ".config", ".local", ".local/share", "Documents",
                                     "Pictures", "Music", "Videos", "Desktop", "Downloads", "Projects")}


def guard(path):
    ap = os.path.abspath(path)
    if ap in PROTECTED or ap.rstrip("/") == str(HOME):
        raise PermissionError(f"refusing to touch protected path {tilde(ap)}")
    if not ap.startswith(str(HOME) + "/"):
        raise PermissionError(f"refusing to touch path outside home: {ap}")
    return ap


def _force_writable(func, p, _exc):
    try:
        os.chmod(os.path.dirname(p), 0o755)
        os.chmod(p, 0o755)
        func(p)
    except OSError:
        pass


def remove(c, dry):
    tag = col("2", "[dry-run] ") if dry else ""
    if c.action == "command":
        print(f"{tag}#{c.id} {c.category}: {col('36', c.command)}")
        if dry:
            return 0
        if input("   Run it? [y/N] ").strip().lower() == "y":
            subprocess.run(c.command, shell=True)
            return c.size
        print("   skipped")
        return 0
    p = guard(c.path)
    if c.action == "empty-trash":
        print(f"{tag}Emptying trash ({human(c.size)})")
        if not dry:
            if shutil.which("gio"):  # also empties trash on other drives
                subprocess.run(["gio", "trash", "--empty"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # Make sure the home trash is really empty, whatever gio did
            for sub in ("files", "info", "expunged"):
                d = os.path.join(p, sub)
                for e in os.scandir(d) if os.path.isdir(d) else []:
                    shutil.rmtree(e.path, onerror=_force_writable) if e.is_dir(follow_symlinks=False) else os.unlink(e.path)
        return c.size
    if c.action == "trash":
        print(f"{tag}Moving to trash: {tilde(p)} ({human(c.size)})")
        if not dry:
            subprocess.run(["gio", "trash", p], check=True)
        return 0  # space returns only when trash is emptied
    print(f"{tag}Deleting: {tilde(p)} ({human(c.size)})")
    if not dry:
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p, onerror=_force_writable)
        else:
            os.unlink(p)
    return c.size


def interactive(cands, args):
    print()
    print("Pick what to remove: numbers (3,5,8-12), " + col("32", "safe") + " = every SAFE item, "
          + col("33", "review") + " = every REVIEW item, combine freely (safe 14 17). Enter to quit.")
    while True:
        try:
            s = input(col("1", "> ")).strip()
        except EOFError:
            return
        if not s or s.lower() in ("q", "quit", "exit"):
            return
        try:
            sel = parse_selection(s, cands)
        except ValueError as e:
            print(f"Didn't understand '{e}'.")
            continue
        if not sel:
            print("Nothing matched.")
            continue
        keep = [c for c in sel if c.verdict == "keep"]
        total = sum(c.size for c in sel)
        print(f"\n{len(sel)} items, ~{human(total)}:")
        for c in sel:
            how = {"delete": "delete", "trash": "→ trash", "empty-trash": "empty", "command": "run cmd"}[c.action]
            print(f"  #{c.id:<3} {how:<8} {human(c.size):>9}  {tilde(c.path) if c.action != 'command' else c.command}")
        if keep:
            print(col("31", f"  ⚠ {len(keep)} of these are marked KEEP."))
        if any(c.action == "trash" for c in sel):
            print(col("2", "  Personal files go to the Trash, so you can restore them; empty the trash afterwards to free that space."))
        prompt = "Simulate? [yes/N] " if args.dry_run else "Type 'yes' to proceed: "
        if input(prompt).strip().lower() != "yes":
            print("Cancelled.")
            continue
        freed = 0
        for c in sel:
            try:
                freed += remove(c, args.dry_run)
            except (OSError, subprocess.CalledProcessError) as e:
                print(col("31", f"  #{c.id} failed: {e}"))
        verb = "Would free" if args.dry_run else "Freed"
        print(col("32;1", f"\n{verb} ~{human(freed)} (plus anything the commands removed)."))
        if not args.dry_run:
            removed = {c.id for c in sel}
            cands[:] = [c for c in cands if c.id not in removed]
        return


# ───────────────────────── main ─────────────────────────

def main():
    ap = argparse.ArgumentParser(description="AI-assisted disk cleanup scanner for Pop!_OS")
    ap.add_argument("--ai", choices=["claude", "ollama", "none"],
                    default="claude" if os.environ.get("ANTHROPIC_API_KEY") else "none",
                    help="who reviews the findings (default: claude if ANTHROPIC_API_KEY is set, else none)")
    ap.add_argument("--model", help=f"model name (default {CLAUDE_MODEL} / {OLLAMA_MODEL})")
    ap.add_argument("--ollama-host", default="http://localhost:11434")
    ap.add_argument("--min-size", type=int, default=50, help="ignore items smaller than this many MB (default 50)")
    ap.add_argument("--large", type=int, default=1024, help="flag single files bigger than this many MB (default 1024)")
    ap.add_argument("--old-days", type=int, default=60, help="downloads untouched this long count as old (default 60)")
    ap.add_argument("--no-system", action="store_true", help="skip apt/journal/flatpak/docker checks")
    ap.add_argument("--dry-run", action="store_true", help="show what would be removed, change nothing")
    ap.add_argument("--report", metavar="FILE", help="also save findings as JSON")
    args = ap.parse_args()

    if os.geteuid() == 0:
        print("Run this as your normal user, not with sudo — it asks for sudo only for system cleanups.")
        sys.exit(1)

    print(col("1", "popclean") + col("2", f" — scanning {tilde(HOME)} and system caches…"))
    t0 = time.time()
    cands = scan(args)
    print(col("2", f"Found {len(cands)} candidates in {time.time() - t0:.0f}s."))
    if not cands:
        print("Nothing worth cleaning above the size threshold.")
        return
    if args.ai == "none":
        print(col("2", "AI review off (set ANTHROPIC_API_KEY or use --ai ollama); using built-in rules."))
    elif args.ai == "claude":
        print(col("2", "Sending paths, sizes and ages (no file contents) to Claude for review…"))
    summary = judge(cands, args)
    show(cands, summary)
    if args.report:
        with open(args.report, "w") as f:
            json.dump({"summary": summary, "items": [asdict(c) for c in cands]}, f, indent=2)
        print(col("2", f"Report saved to {args.report}"))
    interactive(cands, args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nBye.")
