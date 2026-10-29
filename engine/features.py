"""Job-friendly wrappers around the disk cleaner (popclean) and the file organizer."""
import time
from dataclasses import asdict

from . import organizer, popclean
from .util import ai_args, load_conf

# ───────────────────────── disk cleanup ─────────────────────────

CLEAN = {"items": [], "summary": "", "scanned": None}


def clean_scan():
    args = ai_args()
    popclean.NOW = time.time()
    print(f"Scanning your home folder and system caches ({'rules only' if args.ai == 'none' else args.ai})…")
    cands = popclean.scan(args)
    print(f"Found {len(cands)} things that might be clutter.")
    summary = popclean.judge(cands, args)
    cands.sort(key=lambda c: (popclean.ORDER[c.verdict], -c.size))
    for i, c in enumerate(cands, 1):
        c.id = i
    CLEAN.update(items=cands, summary=summary, scanned=time.time())
    print(f"Safe to clear: about {popclean.human(sum(c.size for c in cands if c.verdict == 'safe'))}")


def clean_state():
    items = []
    for c in CLEAN["items"]:
        d = asdict(c)
        d["age_days"] = c.age_days
        d["display"] = popclean.tilde(c.path) if c.action != "command" else c.path
        items.append(d)
    conf = load_conf()
    return {"items": items, "summary": CLEAN["summary"], "scanned": CLEAN["scanned"], "ai": ai_args().ai,
            "has_key": bool(conf.get("anthropic_key"))}


def clean_steps(ids):
    chosen = [c for c in CLEAN["items"] if c.id in set(ids)]
    steps = []
    for c in chosen:
        if c.action == "command":
            steps.append(("root", c.command[5:]) if c.command.startswith("sudo ") else ("user", c.command))
        else:
            steps.append(("py", (lambda c=c: popclean.remove(c, False) is not None)))

    def forget():
        gone = {c.id for c in chosen}
        CLEAN["items"] = [c for c in CLEAN["items"] if c.id not in gone]
    steps.append(("py", forget))
    return chosen, steps


# ───────────────────────── organizer ─────────────────────────

ORG = {"ops": [], "summary": "", "scanned": None, "links": [], "source": "", "drives": []}


def org_scan(instructions):
    def run():
        args = ai_args()
        inv = organizer.inventory()
        source = "rules"
        if args.ai in ("claude", "ollama"):
            print(f"Asking {'Claude' if args.ai == 'claude' else 'Ollama'} for a plan…")
            try:
                summary, raw = organizer.ai_plan(inv, args, instructions)
                source = args.ai
            except Exception as e:  # noqa: BLE001 — fall back to rules on any AI failure
                detail = e.read().decode(errors="replace")[:300] if hasattr(e, "read") else e
                print(f"AI planning didn't work ({detail}); using built-in rules instead.")
                summary, raw = organizer.rules_plan(inv)
        else:
            summary, raw = organizer.rules_plan(inv)
        print("Checking every step for safety…")
        ops = organizer.validate(raw, inv)
        ok = [o for o in ops if not o["blocked"]]
        ORG.update(ops=ops, summary=summary, scanned=time.time(), links=inv["_links"], source=source,
                   drives=inv["drives"])
        print(f"Plan ready: {len(ok)} moves, {len(ops) - len(ok)} skipped for safety.")
    return run


def org_state():
    return {"ops": ORG["ops"], "summary": ORG["summary"], "scanned": ORG["scanned"], "source": ORG["source"],
            "ai": ai_args().ai, "history": organizer.history(),
            "drives": [{k: d[k] for k in ("mount", "free", "total", "used_pct", "holds_home", "writable", "fs")}
                       for d in ORG["drives"]]}


def org_apply_steps(ids):
    chosen = [o for o in ORG["ops"] if o["id"] in set(ids) and not o["blocked"]]

    def run():
        ok = organizer.apply(chosen, ORG["summary"], ORG["links"])
        done = {o["id"] for o in chosen}
        ORG["ops"] = [o for o in ORG["ops"] if o["id"] not in done]
        return ok
    return chosen, [("py", run)]
