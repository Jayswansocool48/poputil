"""Unit tests for the scanner's parsers. Safe to run anywhere: python3 -m unittest tests.test_parsing"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import appscan  # noqa: E402


class Names(unittest.TestCase):
    def test_pretty_name(self):
        cases = {
            "blender-4.2.0-linux-x64": ("Blender", "4.2.0"),
            "Obsidian-1.6.7.AppImage": ("Obsidian", "1.6.7"),
            "cursor-0.42.3x86_64.AppImage": ("Cursor", "0.42.3x86"),
            "LM-Studio-0.3.5-2-x64.AppImage": ("LM Studio", "0.3.5"),
            "node22": ("Node22", ""),
            "google-cloud-sdk": ("Google Cloud Sdk", ""),
            "UnityHub.AppImage": ("UnityHub", ""),
        }
        for raw, want in cases.items():
            self.assertEqual(appscan.pretty_name(raw), want, raw)

    def test_parse_size(self):
        self.assertEqual(appscan.parse_size("312.4 MB"), 312_400_000)
        self.assertEqual(appscan.parse_size("1.5\xa0GB"), 1_500_000_000)
        self.assertEqual(appscan.parse_size("2 MiB"), 2 * 1024 ** 2)
        self.assertEqual(appscan.parse_size("nonsense"), 0)


class Launchers(unittest.TestCase):
    def target(self, exec_line, **extra):
        return appscan.exec_target({"Exec": exec_line, **extra})

    def test_flatpak(self):
        t = self.target("/usr/bin/flatpak run --branch=stable --arch=x86_64 --command=spotify --file-forwarding com.spotify.Client @@u %U @@")
        self.assertEqual(t, {"kind": "flatpak", "id": "com.spotify.Client"})

    def test_snap(self):
        self.assertEqual(self.target("/snap/bin/code --force-user-env %F"), {"kind": "snap", "name": "code"})
        self.assertEqual(self.target("snap run firefox %u")["name"], "firefox")

    def test_wine(self):
        t = self.target('env WINEPREFIX="/home/u/.wine" wine C:\\\\\\\\users\\\\\\\\Public\\\\\\\\x.lnk')
        self.assertEqual(t["kind"], "wine")
        self.assertEqual(t["prefix"], "/home/u/.wine")

    def test_paths(self):
        t = self.target('"/opt/My App/app" --no-sandbox %U')
        self.assertEqual((t["kind"], t["path"], t["exists"]), ("path", "/opt/My App/app", False))
        t = self.target("sh")
        self.assertTrue(t["exists"])
        t = self.target("env GDK_BACKEND=x11 /bin/sh -c 'echo hi'")
        self.assertTrue(t["path"].endswith("/echo"), t)  # the program the launcher really starts

    def test_desktop_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".desktop", delete=False) as f:
            f.write("[Desktop Entry]\nType=Application\nName=Thing\nName[de]=Ding\nExec=thing\nNoDisplay=true\n"
                    "[Desktop Action new]\nName=New window\nExec=thing --new\n")
        e = appscan.parse_desktop(f.name)
        os.unlink(f.name)
        self.assertEqual(e["Name"], "Thing")
        self.assertEqual(e["Exec"], "thing")
        self.assertFalse(e["_visible"])


class Steam(unittest.TestCase):
    def test_vdf(self):
        text = '"AppState"\n{\n\t"appid"\t\t"123"\n\t"name"\t\t"My \\"Game\\""\n\t"UserConfig"\n\t{\n\t\t"lang"\t\t"en"\n\t}\n}\n'
        d = appscan.parse_vdf(text)
        self.assertEqual(d["AppState"]["appid"], "123")
        self.assertEqual(d["AppState"]["UserConfig"]["lang"], "en")


class Safety(unittest.TestCase):
    def test_needs_root(self):
        self.assertTrue(appscan.needs_root("/opt/x"))
        self.assertTrue(appscan.needs_root("/usr/local/bin/x"))
        self.assertFalse(appscan.needs_root(os.path.expanduser("~/Apps/x")))
        self.assertFalse(appscan.needs_root("/media/me/drive/x"))

    def test_appimage_magic(self):
        self.assertTrue(appscan.is_appimage_head(b"\x7fELF\x02\x01\x01\x00AI\x02\x00"))
        self.assertFalse(appscan.is_appimage_head(b"\x7fELF\x02\x01\x01\x00\x00\x00\x00"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
