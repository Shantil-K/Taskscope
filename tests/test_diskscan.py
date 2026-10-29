import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import shutil  # noqa: E402
import subprocess  # noqa: E402

from taskscope.sysinfo import diskscan, ext4scan  # noqa: E402


def _tree(root):
    """a/one.txt (1000 B), a/b/two.so.6 (5000 B), a/b/link (hard link to two),
    c/one.txt (1000 B, duplicate name+size), top.ISO (3000 B)."""
    os.makedirs(os.path.join(root, "a", "b"))
    os.makedirs(os.path.join(root, "c"))
    for path, n in (("a/one.txt", 1000), ("a/b/two.so.6", 5000), ("c/one.txt", 1000),
                    ("top.ISO", 3000)):
        with open(os.path.join(root, path), "wb") as f:
            f.write(b"x" * n)
    os.link(os.path.join(root, "a/b/two.so.6"), os.path.join(root, "a/b/link"))


class DiskScanTest(unittest.TestCase):
    def check(self, res):
        self.assertEqual(res.nfiles, 5)
        self.assertEqual(len(res.dname), 4)                 # root, a, b, c
        self.assertEqual(res.dfiles[0], 5)
        self.assertEqual(res.dfolders[0], 3)
        self.assertEqual(res.dsize[0], 1000 + 5000 + 5000 + 1000 + 3000)
        # the hard link's blocks are counted once
        allocs = sorted(res.falloc[f] for f in range(res.nfiles))
        self.assertEqual(allocs[0], 0)
        # trailing version numbers are not an extension
        self.assertIn(".so", res.exts)
        self.assertNotIn(".6", res.exts)
        self.assertIn(".iso", res.exts)
        names = sorted(r["name"] for r in res.search("one"))
        self.assertEqual(names, ["one.txt", "one.txt"])
        self.assertEqual([r["name"] for r in res.search("*.iso")], ["top.ISO"])
        dup = res.search("", dups="name_size", dups_only=True)
        self.assertEqual(sorted(r["name"] for r in dup), ["one.txt", "one.txt"])
        self.assertTrue(all(r["dup_count"] == 1 for r in dup))
        paths = {res.file_path(f) for f in range(res.nfiles)}
        self.assertIn(os.path.join(res.root, "a", "b", "two.so.6"), paths)

    def test_find_and_python_agree(self):
        with tempfile.TemporaryDirectory() as d:
            _tree(d)
            a = diskscan.scan(d)
            self.check(a)
            old = diskscan._gnu_find
            diskscan._gnu_find = lambda: None
            try:
                b = diskscan.scan(d)
            finally:
                diskscan._gnu_find = old
            self.assertEqual(b.backend, "python")
            self.check(b)
            self.assertEqual(a.dsize[0], b.dsize[0])

    def test_parallel_matches_single(self):
        with tempfile.TemporaryDirectory() as d:
            _tree(d)
            for i in range(6):                       # a few more subtrees to spread out
                sub = os.path.join(d, f"t{i}", "x", "y")
                os.makedirs(sub)
                for j in range(i + 1):
                    with open(os.path.join(sub, f"f{j}.dat"), "wb") as f:
                        f.write(b"y" * (100 * (j + 1)))
            old = diskscan.SPLIT_DEPTH
            diskscan.SPLIT_DEPTH = 1                 # make every top folder a worker task
            try:
                par = diskscan.scan(d, workers=4)
            finally:
                diskscan.SPLIT_DEPTH = old
            self.assertEqual(par.backend, "find" if diskscan._gnu_find() else "python")  # no fallback
            one = diskscan.scan(d, workers=1)
            for r in (par, one):
                self.assertEqual(r.nfiles, 5 + 21)
            self.assertEqual(par.dsize[0], one.dsize[0])
            self.assertEqual(par.dalloc[0], one.dalloc[0])
            self.assertEqual(par.dfolders[0], one.dfolders[0])
            self.assertEqual(sorted(par.file_path(f) for f in range(par.nfiles)),
                             sorted(one.file_path(f) for f in range(one.nfiles)))
            for x in range(len(par.dname)):
                self.assertTrue(all(par.fparent[f] == x for f in par.files_in(x)))
            self.assertEqual(sum(par.file_count(x) for x in range(len(par.dname))), par.nfiles)
            y = par.dname.index("y")
            self.assertEqual(par.dfiles[par.dparent[y]], par.dfiles[y])

    def test_remove_and_export(self):
        with tempfile.TemporaryDirectory() as d:
            _tree(d)
            res = diskscan.scan(d)
            before = res.dsize[0]
            iso = next(f for f in range(res.nfiles) if res.name(f) == "top.ISO")
            res.remove_file(iso)
            self.assertEqual(res.dsize[0], before - 3000)
            self.assertEqual(res.dfiles[0], 4)
            c = res.dname.index("c")
            res.remove_dir(c)
            self.assertEqual(res.dsize[0], before - 3000 - 1000)
            self.assertEqual(res.dfolders[0], 2)
            out = os.path.join(d, "out.csv")
            res.export_csv(out)
            lines = open(out).read().splitlines()
            self.assertEqual(lines[0], "Path,Size,Allocated,Modified,Files,Folders")
            self.assertTrue(any("two.so.6" in line for line in lines))



MKFS = shutil.which("mkfs.ext4") or ("/usr/sbin/mkfs.ext4" if os.path.exists("/usr/sbin/mkfs.ext4") else None)


@unittest.skipUnless(MKFS, "mkfs.ext4 not installed")
class Ext4DirectTest(unittest.TestCase):
    """The direct ext4 reader must see exactly what a normal scan sees."""

    def _image(self, d, *opts):
        src = os.path.join(d, "src")
        img = os.path.join(d, "fs.img")
        with open(img, "wb") as f:
            f.truncate(64 << 20)
        subprocess.run([MKFS, "-q", "-F", *opts, "-d", src, img], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return src, img

    def _same(self, a, b):
        def rows(r):
            files = sorted((os.path.relpath(r.file_path(f), r.root), r.fsize[f], r.fmtime[f])
                           for f in range(r.nfiles))
            dirs = sorted(os.path.relpath(r.dir_path(x), r.root) for x in range(1, len(r.dname)))
            return files, [x for x in dirs if not x.startswith("lost+found")]
        self.assertEqual(rows(a), rows(b))

    def test_images_match_normal_scan(self):
        for opts in ((), ("-b", "1024"), ("-t", "ext3"), ("-t", "ext2", "-O", "^filetype")):
            with self.subTest(opts=opts), tempfile.TemporaryDirectory() as d:
                os.makedirs(os.path.join(d, "src"))
                _tree(os.path.join(d, "src"))
                with open(os.path.join(d, "src", "many"), "w"):
                    pass
                os.makedirs(os.path.join(d, "src", "dir"))
                for i in range(400):                     # a multi-block folder
                    with open(os.path.join(d, "src", "dir", f"file-{i:04d}-" + "x" * 60), "wb") as f:
                        f.write(b"q" * i)
                src, img = self._image(d, *opts)
                ref = diskscan.scan(src, workers=1)
                fast = diskscan.ScanResult(src)
                ext4scan.scan_image(fast, img)
                fast._finish()
                self._same(fast, ref)
                self.assertEqual(fast.dsize[0], ref.dsize[0])

    def test_batch_worker_matches(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "src"))
            _tree(os.path.join(d, "src"))
            src, img = self._image(d)
            whole = diskscan.ScanResult("x")
            ext4scan.scan_image(whole, img)
            whole._finish()
            fs = ext4scan.Ext4(img)
            try:
                top = {name.decode(): ino for ino, name in fs.entries(*fs.inode(2)[5:7], fs.inode(2)[1])}
            finally:
                fs.close()
            part = diskscan.ScanResult("")
            ext4scan.scan_inodes(part, img, [("a", top["a"]), ("c", top["c"])])
            part._finish()
            self.assertEqual([p for p, _ in part.batch_roots], ["a", "c"])
            names = sorted(part.name(f) for f in range(part.nfiles))
            want = sorted(whole.name(f) for f in range(whole.nfiles)
                          if whole.file_path(f).split("/")[1] in ("a", "c"))
            self.assertEqual(names, want)


if __name__ == "__main__":
    unittest.main()
