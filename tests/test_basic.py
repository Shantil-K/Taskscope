"""Run with: python3 -m unittest discover -s tests"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from taskscope import fmt  # noqa: E402
from taskscope.sysinfo import procs, startup, xscreensaver  # noqa: E402


class Parsing(unittest.TestCase):
    def test_stat_with_parens_in_name(self):
        comm, f = procs._parse_stat("42 (a (weird) name) S 1 42 42 0 -1 0 0 0 0 0 5 3 0 0 20 0 2 0 100 0 10")
        self.assertEqual(comm, "a (weird) name")
        self.assertEqual(f[0], "S")
        self.assertEqual(f[1], "1")

    def test_sampler_sees_itself(self):
        s = procs.ProcessSampler()
        s.sample()
        mine = [p for p in s.sample() if p.pid == os.getpid()]
        self.assertEqual(len(mine), 1)
        self.assertTrue(mine[0].rss > 0)

    def test_fmt(self):
        self.assertEqual(fmt.size(512), "512 B")
        self.assertEqual(fmt.size(1536), "1.5 KB")
        self.assertEqual(fmt.bits(125000), "1.0 Mbps")
        self.assertEqual(fmt.duration(90061), "1:01:01:01")


class UserFiles(unittest.TestCase):
    def test_autostart_override_is_per_user(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "app.desktop")
            with open(src, "w") as f:
                f.write("[Desktop Entry]\nName=App\nExec=app\nX-GNOME-Autostart-enabled=true\n")
            old = startup.USER_DIR
            startup.USER_DIR = os.path.join(d, "user")
            try:
                ok, _ = startup.set_enabled({"id": "app.desktop", "path": src, "kind": "Autostart"}, False)
                self.assertTrue(ok)
                text = open(os.path.join(d, "user", "app.desktop")).read()
                self.assertIn("Hidden=true", text)
                self.assertNotIn("X-GNOME-Autostart-enabled", text)
                self.assertIn("X-GNOME", open(src).read())   # original untouched
            finally:
                startup.USER_DIR = old

    def test_xscreensaver_register_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.environ.get("HOME")
            os.environ["HOME"] = d
            try:
                with open(os.path.join(d, ".xscreensaver"), "w") as f:
                    f.write("timeout: 0:10:00\nprograms:\t\\\n  maze -root \\n\\\n\n")
                xscreensaver.register(open_settings=False)
                xscreensaver.register(open_settings=False)
                text = open(os.path.join(d, ".xscreensaver")).read()
                self.assertEqual(text.count("taskscope --screensaver"), 1)
                self.assertIn("maze -root", text)
                self.assertTrue(os.path.exists(os.path.join(d, ".xscreensaver.bak")))
            finally:
                os.environ["HOME"] = old


if __name__ == "__main__":
    unittest.main()
