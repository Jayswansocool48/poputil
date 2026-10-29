"""The "Get apps" catalog: one-click installs from apps.json."""
import json
import os
from pathlib import Path

from .util import q, sh, which

CATALOG = Path(os.environ.get("POPUTIL_CATALOG", Path(__file__).resolve().parent.parent / "apps.json"))


def load():
    data = json.loads(CATALOG.read_text())
    return data.get("presets", {}), data["apps"]


def installed_sets():
    debs, flats = set(), set()
    _, out = sh(["dpkg-query", "-W", "-f=${Package}\t${db:Status-Abbrev}\n"], timeout=30)
    for line in out.splitlines():
        name, _, st = line.partition("\t")
        if st.startswith("ii"):
            debs.add(name.split(":")[0])
    if which("flatpak"):
        _, out = sh(["flatpak", "list", "--app", "--columns=application"], timeout=30)
        flats = {line.strip() for line in out.splitlines() if line.strip()}
    return debs, flats


def state():
    presets, apps = load()
    debs, flats = installed_sets()
    out = []
    for a in apps:
        inst = all(p in debs for p in a["apt"]) if a.get("apt") else a.get("flatpak") in flats
        out.append({**a, "source": "apt" if a.get("apt") else "flatpak", "installed": inst})
    return {"presets": presets, "apps": out, "flatpak": bool(which("flatpak"))}


def steps(ids, remove=False):
    _, apps = load()
    chosen = [a for a in apps if a["id"] in set(ids)]
    debs = [p for a in chosen if a.get("apt") for p in a["apt"]]
    flats = [a["flatpak"] for a in chosen if a.get("flatpak")]
    out = []
    if debs:
        pk = " ".join(q(p) for p in debs)
        out.append(("root", f"apt-get remove -y {pk}" if remove else f"apt-get update -q && apt-get install -y {pk}"))
    if flats:
        fl = " ".join(q(f) for f in flats)
        out.append(("user", f"flatpak uninstall -y --noninteractive {fl}" if remove
                    else f"flatpak install -y --noninteractive flathub {fl}"))
    return chosen, out
