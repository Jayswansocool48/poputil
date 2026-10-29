"""End-to-end test of finding, removing, restoring and deleting apps on the fake PC from tests/fakepc.sh.

Run (as root on a throwaway machine):  HOME=<scratch>/home PATH=<scratch>/bin:$PATH python3 -m unittest tests.test_removal
"""
import hashlib
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import appremove, appscan  # noqa: E402
from engine.jobs import JOBS  # noqa: E402

H = os.path.expanduser("~")
WATCH = [f"{H}/Applications", f"{H}/.local/share/applications", f"{H}/.config", f"{H}/.bashrc", f"{H}/.nvm",
         f"{H}/Downloads", f"{H}/.wine", f"{H}/.var", f"{H}/.local/bin", "/opt/FakeEditor-2.1-linux-x64",
         "/usr/local/bin/fakeeditor", "/usr/share/applications/fakeeditor.desktop", "/usr/local/bin/fakeollama",
         "/usr/local/lib/fakeollama", "/etc/systemd/system/fakeollama.service"]


def snapshot():
    out = {}
    for root in WATCH:
        if os.path.islink(root) or os.path.isfile(root):
            out[root] = _digest(root)
            continue
        for d, dirs, files in os.walk(root):
            for f in dirs + files:
                p = os.path.join(d, f)
                out[p] = _digest(p)
    return out


def _digest(p):
    if os.path.islink(p):
        return "link:" + os.readlink(p)
    if os.path.isdir(p):
        return "dir"
    with open(p, "rb") as fh:
        return hashlib.md5(fh.read()).hexdigest()


def wait_jobs():
    while JOBS.busy():
        time.sleep(0.05)


class RemovalRoundTrip(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.apps = appscan.scan()
        cls.by_name = {a.name: a for a in cls.apps}

    def pick(self, *names):
        return [self.by_name[n] for n in names]

    def test_1_plans(self):
        plans, internal = appremove.plan(self.apps, [a.id for a in self.pick("Fake Editor", "fakeollama", "Tool Studio")], set())
        by = {p["name"]: p for p in plans}
        self.assertFalse(by["Fake Editor"]["blocked"])
        self.assertTrue(by["Fake Editor"].get("needs_password"))
        self.assertTrue(any("service" in s for s in by["fakeollama"]["steps"]))
        self.assertIn("Keeps its settings", " ".join(by["Tool Studio"]["warnings"]))
        # a protected system package is never removable
        prot = next(a for a in self.apps if a.protected)
        p, inner = appremove.plan_app(prot, False)
        self.assertTrue(p["blocked"])
        self.assertIsNone(inner)

    def test_2_remove_restore_remove_delete(self):
        names = ("Fake Editor", "fakeollama", "Tool Studio", "Blender", "Node.js versions (nvm)", "Notepad++",
                 "com.old.Removed", "Old Game", "dead")
        chosen = self.pick(*names)
        before = snapshot()
        with_data = {self.by_name["Tool Studio"].id, self.by_name["Blender"].id}
        plans, internal = appremove.plan(self.apps, [a.id for a in chosen], with_data)
        blocked = {p["name"]: p["blocked"] for p in plans if p["blocked"]}
        self.assertEqual(blocked, {})
        JOBS.submit("test remove", [("py", lambda: appremove.execute(internal, "test"))])
        wait_jobs()
        gone = ["/opt/FakeEditor-2.1-linux-x64", "/usr/local/bin/fakeeditor", "/usr/share/applications/fakeeditor.desktop",
                "/usr/local/bin/fakeollama", "/etc/systemd/system/fakeollama.service", "/usr/local/lib/fakeollama",
                f"{H}/Applications/Tool-1.6.7-x86_64.AppImage", f"{H}/.config/Tool Studio",
                f"{H}/.local/share/applications/appimagekit_tool.desktop", f"{H}/.config/systemd/user/syncer.service",
                f"{H}/Downloads/blender-4.2.0-linux-x64", f"{H}/.config/blender", f"{H}/.nvm",
                f"{H}/.wine/drive_c/Program Files/Notepad++", f"{H}/.var/app/com.old.Removed",
                f"{H}/.local/share/applications/oldgame.desktop", f"{H}/.local/bin/dead"]
        for p in gone:
            self.assertFalse(os.path.lexists(p), f"still there: {p}")
        self.assertTrue(os.path.exists(f"{H}/.config/godot"), "unrelated data must stay")
        with open(f"{H}/.bashrc") as fh:
            self.assertIn("# disabled by PopUtil", fh.read())
        runs = appremove.runs()
        self.assertEqual(runs[0]["state"], "held")
        self.assertTrue(all(a["ok"] for a in runs[0]["apps"]), runs[0]["apps"])

        JOBS.submit("test restore", [("py", lambda: appremove.restore(runs[0]["id"]))])
        wait_jobs()
        after = snapshot()
        self.assertEqual(before, after, "restore must put back every file byte for byte")
        self.assertEqual(appremove.runs()[0]["state"], "restored")

        plans, internal = appremove.plan(self.apps, [self.by_name["Blender"].id], set())
        JOBS.submit("test remove 2", [("py", lambda: appremove.execute(internal, "again"))])
        wait_jobs()
        rid = appremove.runs()[0]["id"]
        held = appremove.runs()[0]["held_bytes"]
        self.assertGreater(held, 0)
        JOBS.submit("test delete", [("py", lambda: appremove.delete_forever(rid))])
        wait_jobs()
        self.assertEqual(appremove.runs()[0]["state"], "deleted")
        self.assertFalse(os.path.exists(appremove.REMOVED / rid))
        self.assertTrue(os.path.exists(f"{H}/.config/blender"), "data kept when not asked to remove it")

    def test_0_scan_survives_broken_sources(self):
        names = [a.name for a in self.apps]
        self.assertIn("My Game", names, "a never-played Steam game must not break the scan")
        original = appscan.scan_snap
        appscan.scan_snap = lambda ctx: 1 / 0
        try:
            apps = appscan.scan()
        finally:
            appscan.scan_snap = original
        self.assertTrue(any(a.name == "Fake Editor" for a in apps))
        self.assertTrue(any("Snap" in p for p in appscan.scan.problems))

    def test_3_refuses_dangerous_paths(self):
        for p in (H, f"{H}/.config", "/usr/bin", "/opt", "/etc/passwd", "/"):
            self.assertTrue(appremove.path_problem(p, not p.startswith(H)), p)
        self.assertEqual(appremove.path_problem("/opt/SomeApp", True), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
