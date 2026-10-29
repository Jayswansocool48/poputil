"""Machine info, live readings, power profiles, tweaks and maintenance tasks."""
import contextlib
import os
import re
import shutil
import time
from pathlib import Path

from .util import HOME, MB, read, sh, which

# ───────────────────────── info ─────────────────────────

_cpu_prev = None


def cpu_percent():
    global _cpu_prev
    try:
        vals = list(map(int, read("/proc/stat").splitlines()[0].split()[1:]))
    except (ValueError, IndexError):
        return 0.0
    idle, total = vals[3] + vals[4], sum(vals)
    prev, _cpu_prev = _cpu_prev, (idle, total)
    if not prev or total == prev[1]:
        return 0.0
    return round(100 * (1 - (idle - prev[0]) / (total - prev[1])), 1)


def meminfo():
    m = {}
    for line in read("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        with contextlib.suppress(ValueError, IndexError):
            m[k] = int(v.split()[0]) * 1024
    return m


def cpu_temp():
    base = Path("/sys/class/hwmon")
    best = None
    for hw in base.glob("hwmon*") if base.exists() else []:
        if read(hw / "name").strip() not in ("coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz"):
            continue
        for lab in sorted(hw.glob("temp*_input")):
            label = read(str(lab).replace("_input", "_label")).strip()
            try:
                t = int(read(lab)) / 1000
            except ValueError:
                continue
            if label in ("Package id 0", "Tctl", "Tdie"):
                return round(t)
            best = t if best is None else best
    return round(best) if best is not None else None


_gpu = {"t": 0.0, "v": None}


def gpu_live():
    if time.time() - _gpu["t"] < 3:
        return _gpu["v"]
    v = None
    if which("nvidia-smi"):
        rc, out = sh(["nvidia-smi", "--query-gpu=utilization.gpu,temperature.gpu,memory.used,memory.total",
                      "--format=csv,noheader,nounits"], timeout=5)
        if rc == 0 and out.strip():
            with contextlib.suppress(ValueError):
                u, t, mu, mt = [float(x) for x in out.splitlines()[0].split(",")]
                v = {"util": u, "temp": t, "mem_used": mu * MB, "mem_total": mt * MB}
    if v is None:
        for f in Path("/sys/class/drm").glob("card*/device/gpu_busy_percent"):
            with contextlib.suppress(ValueError):
                v = {"util": float(read(f).strip())}
                break
    _gpu.update(t=time.time(), v=v)
    return v


def disk(path):
    try:
        u = shutil.disk_usage(path)
        return {"path": path, "total": u.total, "used": u.used, "free": u.free}
    except OSError:
        return None


def disks():
    ds = [d for d in (disk("/"), disk(str(HOME))) if d]
    if len(ds) == 2 and os.stat("/").st_dev == os.stat(str(HOME)).st_dev:
        ds = ds[:1]
    return ds


def package_counts():
    rc, out = sh(["dpkg-query", "-W", "-f=${db:Status-Abbrev}\n"], timeout=30)
    apt = sum(1 for line in out.splitlines() if line.startswith("ii"))
    flat = 0
    if which("flatpak"):
        _, out = sh(["flatpak", "list", "--app", "--columns=application"], timeout=30)
        flat = len([line for line in out.splitlines() if line.strip()])
    return {"apt": apt, "flatpak": flat}


def sysinfo():
    osr = dict(re.findall(r'^(\w+)="?([^"\n]*)"?', read("/etc/os-release"), re.M))
    cpu = next((line.split(":", 1)[1].strip() for line in read("/proc/cpuinfo").splitlines()
                if line.startswith("model name")), "")
    _, lspci = sh(["lspci"])
    gpus = [re.sub(r"^\S+\s+[^:]+:\s*", "", line) for line in lspci.splitlines()
            if re.search(r"VGA|3D controller|Display controller", line)]
    nv = sh(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"], timeout=5)[1] if which("nvidia-smi") else ""
    return {
        "host": os.uname().nodename, "os": osr.get("PRETTY_NAME", "Linux"), "kernel": os.uname().release,
        "desktop": os.environ.get("XDG_CURRENT_DESKTOP", "") or "COSMIC", "cpu": cpu, "cores": os.cpu_count(),
        "gpus": gpus, "nvidia_driver": nv.strip(), "ram": meminfo().get("MemTotal", 0), "disks": disks(),
        "packages": package_counts(), "user": os.environ.get("USER", ""),
    }


def live():
    m = meminfo()
    up = float((read("/proc/uptime", "0").split() or ["0"])[0])
    return {"cpu": cpu_percent(), "temp": cpu_temp(),
            "mem_used": m.get("MemTotal", 0) - m.get("MemAvailable", 0), "mem_total": m.get("MemTotal", 0),
            "swap_used": m.get("SwapTotal", 0) - m.get("SwapFree", 0), "swap_total": m.get("SwapTotal", 0),
            "load": os.getloadavg()[0], "uptime": up, "gpu": gpu_live(), "disks": disks()}


# ───────────────────────── COSMIC theme ─────────────────────────

def cosmic_accent():
    """The accent color picked in COSMIC Settings, as #rrggbb, or None if it can't be read."""
    base = HOME / ".config/cosmic"
    dark = read(base / "com.system76.CosmicTheme.Mode/v1/is_dark").strip() != "false"
    for theme in (("Dark", "Light") if dark else ("Light", "Dark")):
        for d in (f"com.system76.CosmicTheme.{theme}/v1/accent", f"com.system76.CosmicTheme.{theme}.Builder/v1/accent"):
            text = read(base / d)
            m = re.search(r"red:\s*([\d.]+)\s*,\s*green:\s*([\d.]+)\s*,\s*blue:\s*([\d.]+)", text)
            if m:
                with contextlib.suppress(ValueError):
                    r, g, b = (max(0, min(255, round(float(x) * 255))) for x in m.groups())
                    return f"#{r:02x}{g:02x}{b:02x}"
    return None


# ───────────────────────── power ─────────────────────────

def power_state():
    if which("system76-power"):
        _, out = sh(["system76-power", "profile"])
        m = re.search(r"Power Profile:\s*(\w+)", out)
        if m:
            return {"tool": "system76-power", "current": m.group(1).lower(),
                    "options": ["battery", "balanced", "performance"]}
    if which("powerprofilesctl"):
        rc, out = sh(["powerprofilesctl", "get"])
        if rc == 0:
            return {"tool": "powerprofilesctl", "current": out.strip(),
                    "options": ["power-saver", "balanced", "performance"]}
    return None


def power_steps(value):
    p = power_state()
    if not p or value not in p["options"]:
        return None
    cmd = f"system76-power profile {value}" if p["tool"] == "system76-power" else f"powerprofilesctl set {value}"
    return [("user", cmd)]


# ───────────────────────── tweaks ─────────────────────────

def sysctl_val(key):
    try:
        return int(read(f"/proc/sys/{key.replace('.', '/')}").split()[0])
    except (ValueError, IndexError):
        return None


def unit_state(unit, user=False):
    _, out = sh(["systemctl"] + (["--user"] if user else []) + ["is-enabled", unit])
    out = out.strip()
    return out if out and "No such" not in out and "not-found" not in out else None


def _sysctl_tweak(tid, name, desc, key, value):
    path = f"/etc/sysctl.d/80-poputil-{tid}.conf"
    return dict(id=tid, name=name, desc=desc, root=True, group="Performance",
                check=lambda: (sysctl_val(key) or 0) >= value,
                on=[("root", f"printf '{key} = {value}\\n' > {path}"), ("root", "sysctl --system >/dev/null")],
                off=[("root", f"rm -f {path}"), ("root", "sysctl --system >/dev/null")],
                note="Fully reverts after a restart.")


FLATPAK_SVC = HOME / ".config/systemd/user/poputil-flatpak-update.service"
FLATPAK_TMR = HOME / ".config/systemd/user/poputil-flatpak-update.timer"


def _write_flatpak_timer():
    FLATPAK_SVC.parent.mkdir(parents=True, exist_ok=True)
    FLATPAK_SVC.write_text("[Unit]\nDescription=Update Flatpak apps (PopUtil)\n\n[Service]\nType=oneshot\n"
                           "ExecStart=/usr/bin/flatpak update -y --noninteractive\n")
    FLATPAK_TMR.write_text("[Unit]\nDescription=Daily Flatpak updates (PopUtil)\n\n[Timer]\nOnCalendar=daily\n"
                           "RandomizedDelaySec=1h\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n")
    print("Wrote the daily update timer.")


def _remove_flatpak_timer():
    for f in (FLATPAK_SVC, FLATPAK_TMR):
        with contextlib.suppress(FileNotFoundError):
            f.unlink()
    print("Removed the daily update timer.")


def _unit_tweak_check(unit, want_enabled):
    def check():
        st = unit_state(unit)
        return None if st is None else ((st == "enabled") == want_enabled)
    return check


TWEAKS = [
    dict(id="flathub", group="Apps", name="Flathub app store",
         desc="Lets PopUtil and the COSMIC Store install apps from Flathub.", root=True,
         check=lambda: None if not which("flatpak") else "flathub" in sh(["flatpak", "remotes", "--columns=name"])[1].split(),
         on=[("root", "flatpak remote-add --if-not-exists --system flathub https://dl.flathub.org/repo/flathub.flatpakrepo")],
         off=None),
    dict(id="flatpak-auto", group="Apps", name="Update Flatpaks daily",
         desc="Updates Flatpak apps once a day in the background.", root=False,
         check=lambda: None if not which("flatpak") else FLATPAK_TMR.exists(),
         on=[("py", _write_flatpak_timer),
             ("user", "systemctl --user daemon-reload && systemctl --user enable --now poputil-flatpak-update.timer")],
         off=[("user", "systemctl --user disable --now poputil-flatpak-update.timer || true"),
              ("py", _remove_flatpak_timer), ("user", "systemctl --user daemon-reload")]),
    _sysctl_tweak("gaming", "Game memory limit",
                  "Raises vm.max_map_count to the SteamOS value so heavy Proton games and big engine projects don't crash.",
                  "vm.max_map_count", 2147483642),
    _sysctl_tweak("watchers", "More file watchers",
                  "Stops VS Code, Godot and npm from running out of file watchers on big projects.",
                  "fs.inotify.max_user_watches", 524288),
    dict(id="waitonline", group="Startup", name="Skip waiting for the network",
         desc="Boots faster by not waiting for Wi-Fi. Turn it off if a network drive fails to mount.", root=True,
         check=_unit_tweak_check("NetworkManager-wait-online.service", False),
         on=[("root", "systemctl disable NetworkManager-wait-online.service")],
         off=[("root", "systemctl enable NetworkManager-wait-online.service")]),
    dict(id="shutdown", group="Startup", name="Faster shutdown",
         desc="Waits 10 seconds instead of 90 for stuck services when powering off.", root=True,
         check=lambda: os.path.exists("/etc/systemd/system.conf.d/80-poputil.conf"),
         on=[("root", "mkdir -p /etc/systemd/system.conf.d"),
             ("root", "printf '[Manager]\\nDefaultTimeoutStopSec=10s\\n' > /etc/systemd/system.conf.d/80-poputil.conf"),
             ("root", "systemctl daemon-reexec")],
         off=[("root", "rm -f /etc/systemd/system.conf.d/80-poputil.conf"), ("root", "systemctl daemon-reexec")]),
    dict(id="firewall", group="Security", name="Firewall",
         desc="Blocks incoming connections. Dev servers you open to tablets and phones on your network will need a rule.",
         root=True, check=lambda: "ENABLED=yes" in read("/etc/ufw/ufw.conf"),
         on=[("root", "command -v ufw >/dev/null || apt-get install -y ufw"),
             ("root", "ufw default deny incoming && ufw default allow outgoing && ufw --force enable")],
         off=[("root", "ufw disable")]),
    dict(id="trim", group="Performance", name="Weekly SSD trim", desc="Keeps SSDs fast over time.", root=True,
         check=_unit_tweak_check("fstrim.timer", True),
         on=[("root", "systemctl enable --now fstrim.timer")],
         off=[("root", "systemctl disable --now fstrim.timer")]),
]
TWEAK_BY_ID = {t["id"]: t for t in TWEAKS}


def tweaks_state():
    out = []
    for t in TWEAKS:
        try:
            on = t["check"]()
        except Exception:  # noqa: BLE001 — a broken check just shows as unavailable
            on = None
        out.append({"id": t["id"], "group": t["group"], "name": t["name"], "desc": t["desc"], "root": t["root"],
                    "on": on, "can_off": t["off"] is not None, "note": t.get("note", "")})
    return {"tweaks": out, "power": power_state()}


# ───────────────────────── maintenance ─────────────────────────

TASKS = [
    dict(id="update-all", group="Updates", name="Update everything",
         desc="System packages, Flatpak apps, then a firmware check.",
         steps=[("root", "apt-get update && apt-get full-upgrade -y"),
                ("user", "command -v flatpak >/dev/null && flatpak update -y --noninteractive || true"),
                ("user", "command -v fwupdmgr >/dev/null && (fwupdmgr get-updates --no-unreported-check || true) || true")]),
    dict(id="update-flatpak", group="Updates", name="Update Flatpak apps",
         desc="Doesn't need your password.", steps=[("user", "flatpak update -y --noninteractive")]),
    dict(id="cleanup", group="Clean up", name="Quick system cleanup",
         desc="Removes unused packages, the package download cache, old logs and unused Flatpak runtimes.",
         steps=[("root", "apt-get autoremove --purge -y && apt-get clean && journalctl --vacuum-time=2weeks"),
                ("user", "command -v flatpak >/dev/null && flatpak uninstall --unused -y --noninteractive || true")]),
    dict(id="thumbs", group="Clean up", name="Clear thumbnail cache",
         desc="Fixes stale or wrong thumbnails in Files.",
         steps=[("user", "rm -rf ~/.cache/thumbnails/* && echo Cleared.")]),
    dict(id="launchers", group="Clean up", name="Refresh the app library",
         desc="Rebuilds the launcher and icon caches so removed apps disappear and new ones show up.",
         steps=[("user", "update-desktop-database ~/.local/share/applications 2>/dev/null; "
                         "gtk-update-icon-cache -f -t ~/.local/share/icons/hicolor 2>/dev/null; echo Refreshed.")]),
    dict(id="fix-apt", group="Fix things", name="Repair broken packages",
         desc="Finishes interrupted installs and fixes missing dependencies.",
         steps=[("root", "dpkg --configure -a && apt-get install -f -y")]),
    dict(id="fix-flatpak", group="Fix things", name="Repair Flatpak",
         desc="Checks and repairs installed Flatpak apps and runtimes.",
         steps=[("root", "flatpak repair --system"), ("user", "flatpak repair --user")]),
    dict(id="fix-audio", group="Fix things", name="Restart audio",
         desc="For crackling, missing devices or no sound after sleep.",
         steps=[("user", "systemctl --user restart pipewire pipewire-pulse wireplumber && echo Audio restarted.")]),
    dict(id="fix-net", group="Fix things", name="Restart networking",
         desc="Reconnects Wi-Fi and Ethernet.", steps=[("root", "systemctl restart NetworkManager")]),
    dict(id="fix-fonts", group="Fix things", name="Rebuild font cache",
         desc="For newly installed fonts that don't show up.", steps=[("user", "fc-cache -f && echo Fonts refreshed.")]),
    dict(id="fix-boot", group="Fix things", name="Rebuild boot image",
         desc="Regenerates the initramfs, for example after driver trouble.",
         steps=[("root", "update-initramfs -u")]),
]
TASK_BY_ID = {t["id"]: t for t in TASKS}


def tasks_state():
    return {"tasks": [{k: t[k] for k in ("id", "group", "name", "desc")} |
                      {"root": any(s[0] == "root" for s in t["steps"])} for t in TASKS]}
