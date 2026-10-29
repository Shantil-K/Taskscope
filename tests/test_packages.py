"""Package databases of other distros, read from small fake copies."""

import os
import plistlib
import tempfile
import unittest

from taskscope.sysinfo import packages as P


def _write(path, text, mode="w"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode) as fh:
        fh.write(text)


class OtherDistros(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_pacman(self):
        P.PACMAN_DB = d = os.path.join(self.root, "pacman")
        _write(f"{d}/gimp-2.10.38-1/desc", "%NAME%\ngimp\n\n%VERSION%\n2.10.38-1\n\n"
               "%SIZE%\n123456789\n\n%PACKAGER%\nArch Dev <a@b.org>\n")
        _write(f"{d}/gimp-2.10.38-1/files", "%FILES%\nusr/\nusr/share/applications/gimp.desktop\n")
        owner, info = P._pacman_index()
        self.assertEqual(owner["/usr/share/applications/gimp.desktop"], "gimp")
        self.assertEqual(info["gimp"], {"version": "2.10.38-1", "publisher": "Arch Dev", "size": 123456789})

    def test_apk(self):
        P.APK_DB = f = os.path.join(self.root, "apk", "installed")
        _write(f, "C:x\nP:firefox\nV:128.0-r0\nI:250000000\nT:Web browser\nm:Alpine <a@b>\n"
               "F:usr/share/applications\nR:firefox.desktop\n\nP:musl\nV:1.2\nI:600000\n")
        owner, info = P._apk_index()
        self.assertEqual(owner["/usr/share/applications/firefox.desktop"], "firefox")
        self.assertEqual(info["firefox"]["size"], 250000000)
        self.assertEqual(info["musl"]["version"], "1.2")

    def test_xbps(self):
        P.XBPS_DB = d = os.path.join(self.root, "xbps")
        _write(f"{d}/pkgdb-0.38.plist", plistlib.dumps(
            {"vlc": {"pkgver": "vlc-3.0.21_1", "installed_size": 9876543}}).decode())
        _write(f"{d}/.vlc-files.plist", plistlib.dumps(
            {"files": [{"file": "/usr/share/applications/vlc.desktop"}]}).decode())
        owner, info = P._xbps_index()
        self.assertEqual(owner["/usr/share/applications/vlc.desktop"], "vlc")
        self.assertEqual(info["vlc"], {"version": "3.0.21_1", "publisher": "", "size": 9876543})

    def test_portage(self):
        P.PORTAGE_DB = d = os.path.join(self.root, "pkg")
        _write(f"{d}/media-gfx/inkscape-1.3.2-r1/CONTENTS",
               "dir /usr/share/applications\nobj /usr/share/applications/inkscape.desktop abc 1700000000\n")
        _write(f"{d}/media-gfx/inkscape-1.3.2-r1/SIZE", "55555\n")
        owner, info = P._portage_index()
        self.assertEqual(owner["/usr/share/applications/inkscape.desktop"], "media-gfx/inkscape")
        self.assertEqual(info["media-gfx/inkscape"]["version"], "1.3.2-r1")
        self.assertEqual(info["media-gfx/inkscape"]["size"], 55555)

    def test_eopkg(self):
        P.EOPKG_DB = d = os.path.join(self.root, "eopkg")
        _write(f"{d}/geany-2.0-12/metadata.xml",
               "<PISI><Source><Packager><Name>Solus Team</Name></Packager></Source><Package>"
               "<Name>geany</Name><InstalledSize>4242</InstalledSize><History>"
               "<Update release=\"12\"><Version>2.0</Version></Update></History></Package></PISI>")
        _write(f"{d}/geany-2.0-12/files.xml",
               "<Files><File><Path>usr/share/applications/geany.desktop</Path></File></Files>")
        owner, info = P._eopkg_index()
        self.assertEqual(owner["/usr/share/applications/geany.desktop"], "geany")
        self.assertEqual(info["geany"], {"version": "2.0", "publisher": "Solus Team", "size": 4242})

    def test_sizes(self):
        self.assertEqual(P._human_size("3.5 MiB"), 3670016)
        self.assertEqual(P._human_size("540,3 MB"), 540300000)
        self.assertIsNone(P._human_size(""))


if __name__ == "__main__":
    unittest.main()
