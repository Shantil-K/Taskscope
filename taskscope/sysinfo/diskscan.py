"""Disk usage analyser: scans one folder tree (staying on its file system)
and keeps every folder and file in compact column arrays, so a million files
cost roughly 100 MB instead of several hundred.

Scanning uses GNU find (C speed, about 5x faster than Python) and falls back
to os.scandir where find has no -printf (BusyBox, BSD). Nothing runs until the
user presses Scan, and the results are dropped when a new scan starts.

Hard links are counted once, like the kernel does for used space.
"""

import bisect
import csv
import fnmatch
import heapq
import os
import subprocess
import sys
import threading
import time
from array import array

from .probe import which

NO_EXT = "(none)"


class Cancelled(Exception):
    pass


class ScanResult:
    """Folders and files as parallel arrays.

    Folder 0 is the scanned root. A folder's parent always has a smaller
    index, so totals can be summed bottom-up in one reverse pass.
    """

    def __init__(self, root):
        self.root = root.rstrip("/") or "/"
        # folders
        self.dname, self.dparent = [], array("l")
        self.dsize, self.dalloc = array("q"), array("q")
        self.dfiles, self.dfolders = array("l"), array("l")
        self.dmtime = array("q")
        # files. Names live in one byte buffer ("name/name/...", '/' can't
        # appear in a name), which is far smaller than a list of str objects.
        self.fblob, self.foff = bytearray(), array("q", [0])
        self.fparent = array("i")
        self.fsize, self.falloc = array("q"), array("q")
        self.fmtime, self.fext = array("q"), array("i")
        # extensions: index -> name, and per-extension [size, allocated, count]
        self.exts, self.ext_index, self.ext_stats = [], {}, []
        self.denied = 0
        self.elapsed = 0.0
        self.backend = ""
        self._kids = None        # folder -> subfolders
        self._fstart = None      # folder -> where its files start in _forder
        self._fcount = None      # folder -> how many files it holds
        self._forder = None
        self._dpath = None

    # ---- building ---------------------------------------------------------
    def _ext(self, name):
        """name: bytes. Returns the extension index. Trailing version numbers
        are skipped, so libfoo.so.6.1 counts as .so."""
        dot = name.rfind(b".")
        while dot > 0 and name[dot + 1:].isdigit():
            name = name[:dot]
            dot = name.rfind(b".")
        if 0 < dot and len(name) - dot <= 12 and b" " not in name[dot:]:
            e = name[dot:].lower()
        else:
            e = b""
        i = self.ext_index.get(e)
        if i is None:
            i = self.ext_index[e] = len(self.exts)
            self.exts.append(os.fsdecode(e) if e else NO_EXT)
            self.ext_stats.append([0, 0, 0])
        return i

    def _add_dir(self, name, parent, mtime, alloc):
        self.dname.append(name)
        self.dparent.append(parent)
        self.dsize.append(0)
        self.dalloc.append(alloc)        # a directory's own blocks count as used space
        self.dfiles.append(0)
        self.dfolders.append(0)
        self.dmtime.append(mtime)
        return len(self.dname) - 1

    def _add_file(self, name, parent, size, alloc, mtime):
        """name: bytes."""
        e = self._ext(name)
        self.fblob += name
        self.fblob += b"/"
        self.foff.append(len(self.fblob))
        self.fparent.append(parent)
        self.fsize.append(size)
        self.falloc.append(alloc)
        self.fmtime.append(mtime)
        self.fext.append(e)
        st = self.ext_stats[e]
        st[0] += size
        st[1] += alloc
        st[2] += 1

    def _set_names(self, names):
        """Append many names at once (much faster than one by one)."""
        from itertools import accumulate
        base = len(self.fblob)
        if names:
            self.fblob += b"/".join(names)
            self.fblob += b"/"
            self.foff.extend(accumulate((len(x) + 1 for x in names), initial=base))
            self.foff.pop(len(self.foff) - len(names) - 1)   # 'initial' repeats the old end
        return base

    def _file_sums(self):
        """Add each file to its own folder's totals (no propagation yet)."""
        ds, da, df = self.dsize, self.dalloc, self.dfiles
        fs, fa = self.fsize, self.falloc
        for f, p in enumerate(self.fparent):
            ds[p] += fs[f]
            da[p] += fa[f]
            df[p] += 1

    def _group_files(self, first=0):
        """Files grouped by folder with a counting sort: _forder holds file
        indices, folder d's files are _forder[_fstart[d]:_fstart[d] + _fcount[d]].
        Only files from index *first* on are grouped (and appended)."""
        n = len(self.dname)
        if self._fstart is None:
            self._fstart, self._fcount, self._forder = array("l", bytes(8 * n)), array("l", bytes(8 * n)), array("l")
        count = {}
        fp = self.fparent
        for f in range(first, self.nfiles):
            count[fp[f]] = count.get(fp[f], 0) + 1
        base = len(self._forder)
        pos = {}
        for d in sorted(count):
            self._fstart[d] = base
            self._fcount[d] = count[d]
            pos[d] = base
            base += count[d]
        self._forder.extend(array("l", bytes(8 * (base - len(self._forder)))))
        for f in range(first, self.nfiles):
            d = fp[f]
            self._forder[pos[d]] = f
            pos[d] += 1

    def _finish_dirs(self):
        ds, da, df, dd, dp = self.dsize, self.dalloc, self.dfiles, self.dfolders, self.dparent
        for d in range(len(self.dname) - 1, 0, -1):
            p = dp[d]
            ds[p] += ds[d]
            da[p] += da[d]
            df[p] += df[d]
            dd[p] += dd[d] + 1
        kids = [[] for _ in range(len(self.dname))]
        for d in range(1, len(self.dname)):
            kids[dp[d]].append(d)
        self._kids = kids

    def _finish(self):
        self._file_sums()
        self._group_files()
        self._finish_dirs()

    # ---- queries -----------------------------------------------------------
    @property
    def nfiles(self):
        return len(self.fparent)

    def name(self, f):
        return self.fblob[self.foff[f]:self.foff[f + 1] - 1].decode("utf-8", "surrogateescape")

    def _raw_name(self, f):
        return bytes(self.fblob[self.foff[f]:self.foff[f + 1] - 1])

    def subfolders(self, d):
        return self._kids[d]

    def files_in(self, d):
        s = self._fstart[d]
        return self._forder[s:s + self._fcount[d]]

    def file_count(self, d):
        return self._fcount[d]

    def dir_path(self, d):
        if self._dpath is None:
            paths = [self.root]
            for i in range(1, len(self.dname)):
                pp = paths[self.dparent[i]]
                paths.append(("" if pp == "/" else pp) + "/" + self.dname[i])
            self._dpath = paths
        return self._dpath[d]

    def file_path(self, f):
        pp = self.dir_path(self.fparent[f])
        return ("" if pp == "/" else pp) + "/" + self.name(f)

    def ancestors(self, d):
        chain = []
        while d > 0:
            chain.append(d)
            d = self.dparent[d]
        chain.append(0)
        return chain[::-1]

    @property
    def total_alloc(self):
        return self.dalloc[0] if self.dalloc else 0

    def remove_file(self, f):
        """Forget a deleted file: zero it and take it off every folder total."""
        s, a = self.fsize[f], self.falloc[f]
        d = self.fparent[f]
        st = self.ext_stats[self.fext[f]]
        st[0] -= s
        st[1] -= a
        st[2] -= 1
        while True:
            self.dsize[d] -= s
            self.dalloc[d] -= a
            self.dfiles[d] -= 1
            if d == 0:
                break
            d = self.dparent[d]
        self.fsize[f] = self.falloc[f] = 0

    def remove_dir(self, d):
        s, a, nf, nd = self.dsize[d], self.dalloc[d], self.dfiles[d], self.dfolders[d] + 1
        stack = [d]
        while stack:                        # zero the contents too
            x = stack.pop()
            for f in self.files_in(x):
                if self.falloc[f] or self.fsize[f]:
                    st = self.ext_stats[self.fext[f]]
                    st[0] -= self.fsize[f]
                    st[1] -= self.falloc[f]
                    st[2] -= 1
                    self.fsize[f] = self.falloc[f] = 0
            self.dsize[x] = self.dalloc[x] = self.dfiles[x] = self.dfolders[x] = 0
            stack.extend(self._kids[x])
        p = self.dparent[d] if d else -1
        while p >= 0:
            self.dsize[p] -= s
            self.dalloc[p] -= a
            self.dfiles[p] -= nf
            self.dfolders[p] -= nd
            p = self.dparent[p] if p else -1

    # ---- file view ----------------------------------------------------------
    def _name_matches(self, text):
        """Files whose name contains *text*, found by scanning the name buffer
        directly (no per-file string objects)."""
        needle = os.fsencode(text)
        if not needle or b"/" in needle:
            return []
        hay = bytes(self.fblob).lower()
        off, out, i = self.foff, [], hay.find(needle)
        while i >= 0:
            f = bisect.bisect_right(off, i) - 1
            out.append(f)
            i = hay.find(needle, off[f + 1])          # next name
        return out

    def search(self, text="", whole_path=False, folders=False, limit=1000, dups=None,
               dups_only=False):
        """Largest matching files (and folders), by allocated size.

        text: substring, or a wildcard pattern when it contains * ? or [.
        dups: None, "name_size" or "name_size_date" to count duplicate copies.
        """
        text = text.strip().lower()
        wild = any(c in text for c in "*?[")
        n = self.nfiles
        if not text:
            cand = range(n)
        elif whole_path:
            test = (lambda s: fnmatch.fnmatchcase(s, text)) if wild else (lambda s: text in s)
            cand = [f for f in range(n) if test(self.file_path(f).lower())]
        elif wild:
            cand = [f for f in range(n) if fnmatch.fnmatchcase(self.name(f).lower(), text)]
        else:
            cand = self._name_matches(text)
        alloc, size = self.falloc, self.fsize
        counts, key = {}, None
        rank = alloc.__getitem__
        if dups:
            raw, mtime = self._raw_name, self.fmtime
            if dups == "name_size":
                key = lambda f: (raw(f), size[f])               # noqa: E731
            else:
                key = lambda f: (raw(f), size[f], mtime[f])     # noqa: E731
            for f in range(n):
                if size[f]:
                    k = key(f)
                    counts[k] = counts.get(k, 0) + 1
            if dups_only:
                cand = [f for f in cand if size[f] and counts.get(key(f), 0) > 1]
                rank = lambda f: ((counts[key(f)] - 1) * size[f], raw(f))   # noqa: E731
        top = heapq.nlargest(limit, cand, key=rank)
        total = self.total_alloc or 1
        rows = []
        for f in top:
            c = counts.get(key(f), 1) - 1 if key and size[f] else 0
            rows.append({"kind": "file", "idx": f, "name": self.name(f),
                         "path": self.file_path(f), "size": size[f],
                         "alloc": alloc[f], "pct": alloc[f] * 100.0 / total,
                         "mtime": self.fmtime[f], "dup_count": c, "dup_size": c * size[f]})
        if folders and not dups_only:
            dn = self.dname
            if not text:
                dc = range(len(dn))
            else:
                test = (lambda s: fnmatch.fnmatchcase(s, text)) if wild else (lambda s: text in s)
                get = (lambda d: self.dir_path(d)) if whole_path else dn.__getitem__
                dc = [d for d in range(1, len(dn)) if test(get(d).lower())]
            for d in heapq.nlargest(limit, dc, key=self.dalloc.__getitem__):
                rows.append({"kind": "dir", "idx": d, "name": dn[d] + ("" if d == 0 else "/"),
                             "path": self.dir_path(d), "size": self.dsize[d],
                             "alloc": self.dalloc[d], "pct": self.dalloc[d] * 100.0 / total,
                             "mtime": self.dmtime[d], "dup_count": 0, "dup_size": 0})
            rows.sort(key=lambda r: r["alloc"], reverse=True)
            rows = rows[:limit]
        return rows

    def export_csv(self, path):
        """Every folder followed by its files, depth first."""
        with open(path, "w", newline="", encoding="utf-8", errors="surrogateescape") as fh:
            w = csv.writer(fh)
            w.writerow(["Path", "Size", "Allocated", "Modified", "Files", "Folders"])
            stamp = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))   # noqa: E731
            stack = [0]
            while stack:
                d = stack.pop()
                w.writerow([self.dir_path(d) + ("" if d == 0 and self.root == "/" else "/"),
                            self.dsize[d], self.dalloc[d], stamp(self.dmtime[d]),
                            self.dfiles[d], self.dfolders[d]])
                for f in self.files_in(d):
                    w.writerow([self.file_path(f), self.fsize[f], self.falloc[f],
                                stamp(self.fmtime[f]), "", ""])
                stack.extend(sorted(self._kids[d], key=self.dalloc.__getitem__))


# ------------------------------------------------------------------- scanning

_find_ok = None


def _gnu_find():
    """'find' when GNU find (with -printf) is installed, else None. Checked once."""
    global _find_ok
    if _find_ok is None:
        _find_ok = _check_find() or ""
    return _find_ok or None


def _check_find():
    if not which("find"):
        return None
    try:
        r = subprocess.run(["find", "/dev/null", "-maxdepth", "0", "-printf", "%y"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return "find" if r.returncode == 0 and r.stdout == "c" else None


def estimate_entries(path):
    """Used inodes on the file system: a progress estimate for whole-disk scans."""
    try:
        st = os.statvfs(path)
        return max(0, st.f_files - st.f_ffree)
    except OSError:
        return 0


def scan(root, progress=None, cancel=None, workers=None, method="auto", partial=None):
    """Scan *root*. progress(entries_seen) is called now and then from this
    (worker) thread; set the threading.Event *cancel* to stop early.

    method: "auto" reads ext2/3/4 directly from the device when running as
    root (see ext4scan) and otherwise walks the tree; "ext4" forces the
    direct read, "normal" forces the walk.

    The walk splits the tree into batches that several processes scan at
    once (one per CPU core). workers=1 scans in this process.

    partial(preview, totals, exts): called now and then during a parallel
    scan with a preview ScanResult of the top folder levels; totals is
    (dsize, dalloc, dfiles, dfolders) and exts is (names, stats), both
    growing as batches finish. Called from the scanning thread."""
    t0 = time.monotonic()
    cancel = cancel or threading.Event()
    note = ""
    if method in ("auto", "ext4"):
        from . import ext4scan
        if method == "ext4" or ext4scan.usable(root):
            res = ScanResult(root)
            res.backend = "ext4 direct"
            nw = workers or min(os.cpu_count() or 1, 16)
            try:
                if nw > 1:
                    dev = ext4scan.device_of(root)
                    ext4scan.check_root(dev, root)
                    _scan_parallel(res, progress, cancel, nw, ext4_dev=dev, partial=partial)
                else:
                    ext4scan.scan_into(res, root, progress, cancel)
                    res._finish()
                res.elapsed = time.monotonic() - t0
                return res
            except Cancelled:
                raise
            except Exception as e:          # anything odd: use the normal scan
                if method == "ext4":
                    raise
                note = f" (direct ext4 read not used: {e})"
    t0 = time.monotonic()
    res = ScanResult(root)
    res.backend = ("find" if _gnu_find() else "python") + note
    workers = workers or min(os.cpu_count() or 1, 16)
    if workers > 1:
        try:
            _scan_parallel(res, progress, cancel, workers, partial=partial)
        except Cancelled:
            raise
        except (OSError, EOFError, ValueError) as e:
            # A worker could not start or died: do it the simple way instead.
            if cancel.is_set():
                raise Cancelled()
            res = ScanResult(root)
            res.backend = ("find" if _gnu_find() else "python") + " (single process: " + str(e) + ")"
            (_scan_find if _gnu_find() else _scan_python)(res, progress, cancel)
            res._finish()
    else:
        (_scan_find if res.backend == "find" else _scan_python)(res, progress, cancel)
        res._finish()
    res.elapsed = time.monotonic() - t0
    return res


# ---- parallel scan ------------------------------------------------------------
# The first few levels are listed by the main process; every folder at the
# split depth becomes a task. Worker processes take tasks one at a time, so
# fast workers pick up more. When all are scanned, the main process hands out
# the final numbering and each worker renumbers its own subtrees in parallel;
# the main process then only appends ready-made arrays (C speed).

SPLIT_DEPTH = 3


def _subtree(paths, conn, dev=None):
    """Scan a batch of folders: with one find run, or straight from the ext4
    device when *dev* is given."""
    res = ScanResult("")
    hard = []
    last = [0]

    def bump(n):
        conn.send(("progress", n - last[0]))
        last[0] = n
    if dev:
        from . import ext4scan
        ext4scan.scan_batch(res, dev, paths, bump, hard)
    else:
        (_scan_find if _gnu_find() else _scan_python)(res, bump, threading.Event(), hard, starts=paths)
    own = {i: (res.dsize[i], res.dalloc[i], res.dfiles[i]) for _p, i in res.batch_roots}
    res._file_sums()
    res._group_files()
    # whole-subtree totals of each start folder, for the live preview
    ts, ta = array("q", res.dsize), array("q", res.dalloc)
    tf, td = array("l", res.dfiles), array("l", res.dfolders)
    dp = res.dparent
    for d in range(len(res.dname) - 1, 0, -1):
        p = dp[d]
        ts[p] += ts[d]
        ta[p] += ta[d]
        tf[p] += tf[d]
        td[p] += td[d] + 1
    roots = []
    for path, i in res.batch_roots:
        o = own[i]
        roots.append((path, i, (res.dsize[i] - o[0], res.dalloc[i] - o[1], res.dfiles[i] - o[2]),
                      res._fstart[i], res._fcount[i],
                      (ts[i] - o[0], ta[i] - o[1], tf[i] - o[2], td[i])))
    return res, hard, roots


def _worker_main(rfd, wfd):
    """Entry point of a worker process (started by _Workers)."""
    from multiprocessing.connection import Connection
    inbox, out = Connection(rfd, writable=False), Connection(wfd, readable=False)
    try:
        _worker(inbox, out)
    except (EOFError, BrokenPipeError, KeyboardInterrupt):
        pass


def _worker(inbox, conn):
    held = {}
    while True:
        msg = inbox.recv()
        if msg[0] == "scan":
            bid, paths, dev = msg[1], msg[2], msg[3]
            try:
                res, hard, roots = _subtree(paths, conn, dev)
            except Exception as e:                  # report and carry on
                conn.send(("error", bid, str(e)))
                continue
            held[bid] = (res, roots)
            conn.send(("scanned", bid, len(res.dname) - 1 - len(roots), res.nfiles, len(res.fblob),
                       res.exts, res.ext_stats, hard, res.denied, roots))
        elif msg[0] == "final":
            for bid, (doff, fbase, shift, emap, rootmap) in msg[1].items():
                res, _roots = held.pop(bid)
                n = len(res.dname)
                gidx = [-1] * n
                keep = []
                nxt = doff
                for k in range(1, n):
                    g = rootmap.get(k)
                    if g is not None:
                        gidx[k] = g
                    else:
                        gidx[k] = nxt
                        nxt += 1
                        keep.append(k)
                dn, dp, dm = res.dname, res.dparent, res.dmtime
                ds, da, df = res.dsize, res.dalloc, res.dfiles
                fs, fc = res._fstart, res._fcount
                conn.send(("part", bid,
                           [dn[k] for k in keep], array("l", (gidx[dp[k]] for k in keep)),
                           array("q", (dm[k] for k in keep)), array("q", (ds[k] for k in keep)),
                           array("q", (da[k] for k in keep)), array("l", (df[k] for k in keep)),
                           bytes(res.fblob), array("q", (x + shift for x in res.foff[1:])),
                           array("i", map(gidx.__getitem__, res.fparent)), res.fsize, res.falloc,
                           res.fmtime, array("i", map(emap.__getitem__, res.fext)),
                           array("l", (fbase + f for f in res._forder)),
                           array("l", (fbase + fs[k] for k in keep)),
                           array("l", (fc[k] for k in keep))))
                del res
            conn.send(("end",))
            return
        else:
            return


class _Preview:
    """Live picture of a running parallel scan: the folders listed up front
    (the top levels) with totals that grow as each batch of deeper folders
    is finished by a worker."""

    def __init__(self, res, callback):
        self.cb = callback
        p = ScanResult(res.root)
        p.backend = res.backend
        p.dname, p.dparent, p.dmtime = list(res.dname), array("l", res.dparent), array("q", res.dmtime)
        p.fblob, p.foff = bytearray(res.fblob), array("q", res.foff)
        p.fparent, p.fsize, p.falloc = array("i", res.fparent), array("q", res.fsize), array("q", res.falloc)
        p.fmtime, p.fext = array("q", res.fmtime), array("i", res.fext)
        p.exts, p.ext_index = list(res.exts), dict(res.ext_index)
        p.ext_stats = [list(x) for x in res.ext_stats]
        p.dsize, p.dalloc = array("q", res.dsize), array("q", res.dalloc)
        p.dfiles, p.dfolders = array("l", res.dfiles), array("l", res.dfolders)
        p._group_files()
        p._kids = [[] for _ in p.dname]
        for d in range(1, len(p.dname)):
            p._kids[p.dparent[d]].append(d)
        p.preview = True
        self.p = p
        self.base = (array("q", res.dsize), array("q", res.dalloc), array("l", res.dfiles))
        # file types are counted here, never on the preview object the GUI is showing
        self.exts, self.index = list(res.exts), dict(res.ext_index)
        self.stats = [list(x) for x in res.ext_stats]
        self.sub = {}                  # folder -> (size, alloc, files, folders) below it
        self.last = 0.0

    def add(self, msg, tasks):
        exts, ext_stats, roots = msg[5], msg[6], msg[9]
        for i, e in enumerate(exts):
            key = e.encode("utf-8", "surrogateescape") if e != NO_EXT else b""
            j = self.index.get(key)
            if j is None:
                j = self.index[key] = len(self.exts)
                self.exts.append(e)
                self.stats.append([0, 0, 0])
            for k in range(3):
                self.stats[j][k] += ext_stats[i][k]
        for path, _i, _rf, _fs, _fc, sub in roots:
            self.sub[tasks[path]] = sub

    def emit(self, force=False):
        now = time.monotonic()
        if not force and now - self.last < 0.5:
            return
        self.last = now
        ds, da, df = (array(a.typecode, a) for a in self.base)
        dd = array("l", bytes(8 * len(ds)))
        for g, (s_, a_, f_, d_) in self.sub.items():
            ds[g] += s_
            da[g] += a_
            df[g] += f_
            dd[g] += d_
        dp = self.p.dparent
        for d in range(len(ds) - 1, 0, -1):
            q = dp[d]
            ds[q] += ds[d]
            da[q] += da[d]
            df[q] += df[d]
            dd[q] += dd[d] + 1
        self.cb(self.p, (ds, da, df, dd), (list(self.exts), [list(x) for x in self.stats]))


def _grow(arr, n):
    if len(arr) < n:
        arr.extend(array(arr.typecode, bytes(arr.itemsize * (n - len(arr)))))


class _Workers:
    """Plain child processes that import only this module (no
    multiprocessing start-up tricks, nothing of the GUI), talking over pipes."""

    def __init__(self, n):
        from multiprocessing.connection import Connection
        pkg_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        code = (f"import sys; sys.path.insert(0, {pkg_root!r}); "
                "from taskscope.sysinfo.diskscan import _worker_main; "
                "_worker_main(int(sys.argv[1]), int(sys.argv[2]))")
        self.procs, self.send, self.recv = [], [], []
        try:
            for _ in range(n):
                r1, w1 = os.pipe()          # main -> worker
                r2, w2 = os.pipe()          # worker -> main
                pr = subprocess.Popen([sys.executable, "-c", code, str(r1), str(w2)],
                                      pass_fds=(r1, w2), stdin=subprocess.DEVNULL,
                                      start_new_session=True)
                os.close(r1)
                os.close(w2)
                self.procs.append(pr)
                self.send.append(Connection(w1, readable=False))
                self.recv.append(Connection(r2, writable=False))
        except BaseException:
            self.close(kill=True)
            raise

    def close(self, kill=False):
        for c in self.send + self.recv:
            try:
                c.close()
            except OSError:
                pass
        for pr in self.procs:
            if kill:
                try:
                    os.killpg(pr.pid, 9)       # the worker and its find
                except OSError:
                    pass
            try:
                pr.wait(2)
            except subprocess.TimeoutExpired:
                pr.kill()


def _scan_parallel(res, progress, cancel, workers, ext4_dev=None, partial=None):
    from multiprocessing.connection import wait
    root = res.root
    try:
        st = os.lstat(root)
    except OSError as e:
        raise OSError(f"Cannot read {root}: {e.strerror}")
    root_dev = st.st_dev
    res._add_dir(root, -1, int(st.st_mtime), st.st_blocks * 512)
    seen = {}                        # inode -> file index, for multi-linked files
    listed = 0

    frontier = [(root, 0)]
    for _depth in range(SPLIT_DEPTH):
        if cancel.is_set():
            raise Cancelled()
        nxt = []
        for path, d in frontier:
            try:
                it = os.scandir(path)
            except OSError:
                res.denied += 1
                continue
            with it:
                for e in it:
                    try:
                        s = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    listed += 1
                    if e.is_dir(follow_symlinks=False):
                        if s.st_dev != root_dev:     # another file system mounted here
                            continue
                        nxt.append((e.path, res._add_dir(e.name, d, int(s.st_mtime), s.st_blocks * 512)))
                    else:
                        alloc = s.st_blocks * 512
                        if s.st_nlink > 1:
                            if s.st_ino in seen:
                                alloc = 0
                            else:
                                seen[s.st_ino] = res.nfiles
                        res._add_file(os.fsencode(e.name), d, s.st_size, alloc, int(s.st_mtime))
        frontier = nxt
    tasks = dict(frontier)                    # path -> folder index
    res._file_sums()
    res._group_files()
    if progress:
        progress(listed)
    live = _Preview(res, partial) if partial and tasks else None
    if live:
        live.emit(force=True)
    if not tasks:
        res._finish_dirs()
        return

    pool = _Workers(min(workers, len(tasks)))
    out_of = dict(zip(pool.recv, pool.send))  # reply pipe -> that worker's request pipe
    seen_entries = 0
    ok = False
    try:
        # Phase 1: scan. Biggest-looking folders first (more links = more
        # subfolders); batches shrink towards the end so cores finish together.
        queue = sorted(tasks, key=lambda p: -_nlink(p))
        nw = len(pool.recv)
        batches = {}

        def next_batch():
            size = max(1, min(64, len(queue) // (nw * 3)))
            paths = queue[:size]
            del queue[:size]
            bid = len(batches)
            batches[bid] = paths
            return bid, paths, ext4_dev

        busy = set()
        for c in pool.recv:
            if queue:
                out_of[c].send(("scan",) + next_batch())
                busy.add(c)
        scanned, order = {}, []
        while busy:
            if cancel.is_set():
                raise Cancelled()
            for c in wait(list(busy), timeout=0.15):
                msg = c.recv()
                if msg[0] == "progress":
                    seen_entries += msg[1]
                    continue
                if msg[0] == "scanned":
                    scanned[msg[1]] = (c, msg)
                    order.append(msg[1])
                    if live:
                        live.add(msg, tasks)
                else:                         # a batch that failed
                    if ext4_dev:
                        raise OSError(f"direct read failed: {msg[2]}")
                    res.denied += len(batches.get(msg[1], ()))
                if queue:
                    out_of[c].send(("scan",) + next_batch())
                else:
                    busy.discard(c)
            if progress:
                progress(listed + seen_entries)
            if live:
                live.emit()
        if live:
            live.emit(force=True)

        # Numbering: the folders and files of each batch follow each other.
        doff, fbase, shift = len(res.dname), res.nfiles, len(res.fblob)
        table = {c: {} for c in pool.recv}
        layout = []
        for bid in order:
            c, (_t, _b, nd, nf, blen, exts, ext_stats, hard, denied, roots) = scanned[bid]
            emap = []
            for i, e in enumerate(exts):
                key = e.encode("utf-8", "surrogateescape") if e != NO_EXT else b""
                j = res.ext_index.get(key)
                if j is None:
                    j = res.ext_index[key] = len(res.exts)
                    res.exts.append(e)
                    res.ext_stats.append([0, 0, 0])
                stt = res.ext_stats[j]
                for k in range(3):
                    stt[k] += ext_stats[i][k]
                emap.append(j)
            rootmap = {i: tasks[path] for path, i, *_ in roots}
            table[c][bid] = (doff, fbase, shift, emap, rootmap)
            layout.append((bid, fbase, hard, roots))
            res.denied += denied
            doff += nd
            fbase += nf
            shift += blen

        # Phase 2: workers renumber in parallel; append in numbering order.
        for c in pool.recv:
            out_of[c].send(("final", table[c]))
        parts, nxt_i, open_c = {}, 0, set(pool.recv)
        while True:
            while nxt_i < len(layout) and layout[nxt_i][0] in parts:
                _append(res, parts.pop(layout[nxt_i][0]), layout[nxt_i], tasks, seen)
                nxt_i += 1
            if not open_c or nxt_i >= len(layout):
                break
            if cancel.is_set():
                raise Cancelled()
            for c in wait(list(open_c), timeout=0.15):
                msg = c.recv()
                if msg[0] == "part":
                    parts[msg[1]] = msg
                else:
                    open_c.discard(c)
        if nxt_i < len(layout):
            raise EOFError("a scanner process stopped early")
        ok = True
    finally:
        pool.close(kill=not ok)
    res._finish_dirs()


def _nlink(path):
    try:
        return os.lstat(path).st_nlink
    except OSError:
        return 0


def _append(res, msg, info, tasks, seen):
    (_t, _b, dname, dparent, dmtime, dsize, dalloc, dfiles, fblob, foff, fparent, fsize, falloc,
     fmtime, fext, forder, fstart, fcount) = msg
    _bid, fbase, hard, roots = info
    res.dname.extend(dname)
    res.dparent.extend(dparent)
    res.dmtime.extend(dmtime)
    res.dsize.extend(dsize)
    res.dalloc.extend(dalloc)
    res.dfiles.extend(dfiles)
    res.dfolders.extend(array("l", bytes(8 * len(dname))))
    res.fblob += fblob
    res.foff.extend(foff)
    res.fparent.extend(fparent)
    res.fsize.extend(fsize)
    res.falloc.extend(falloc)
    res.fmtime.extend(fmtime)
    res.fext.extend(fext)
    res._forder.extend(forder)
    res._fstart.extend(fstart)
    res._fcount.extend(fcount)
    for path, _i, (s_, a_, n_), fs0, fc0, _sub in roots:
        g = tasks[path]
        res.dsize[g] += s_
        res.dalloc[g] += a_
        res.dfiles[g] += n_
        if fc0:
            res._fstart[g] = fbase + fs0
            res._fcount[g] = fc0
    # hard links already counted in another batch
    for lf, ino in hard:
        f = fbase + lf
        if ino in seen:
            a = res.falloc[f]
            res.falloc[f] = 0
            res.dalloc[res.fparent[f]] -= a
            res.ext_stats[res.fext[f]][1] -= a
        else:
            seen[ino] = f


def _scan_find(res, progress, cancel, hard=None, starts=None):
    """Read the tree from GNU find. find prints in depth-first order with
    each entry's depth, so the parent folder is simply the last folder seen
    one level up: no path strings, no lookups.

    starts: scan several folders in one find run (worker batches). Folder 0
    is then a stand-in parent and each start folder sits at depth 1; their
    indices are recorded in res.batch_roots as (path, index)."""
    fmt = "%y%d %s %b %T@ %n %i %f\\0"
    if starts:
        res._add_dir("", -1, 0, 0)
        cmd = ["find"] + list(starts) + ["-xdev", "-printf", fmt]
        off = 1
        expect = list(starts)
        res.batch_roots = []
    else:
        try:
            st = os.lstat(res.root)
        except OSError:
            st = None
        res._add_dir(res.root, -1, int(st.st_mtime) if st else 0, st.st_blocks * 512 if st else 0)
        # %y type+%d depth, %s size, %b 512-byte blocks, %T@ mtime, %n links, %i inode, %f name
        cmd = ["find", res.root, "-xdev", "-mindepth", "1", "-printf", fmt]
        off = 0
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=1 << 20)
    errs = []
    t = threading.Thread(target=lambda: errs.append(p.stderr.read()), daemon=True)
    t.start()

    names = []                       # joined into the name buffer at the end
    stack = [0]                      # folder index at each depth
    seen_inodes = set()
    ext_index, exts, ext_stats = res.ext_index, res.exts, res.ext_stats
    names_add = names.append
    fparent_add, fsize_add = res.fparent.append, res.fsize.append
    falloc_add, fmtime_add, fext_add = res.falloc.append, res.fmtime.append, res.fext.append
    dname, dparent, dsize, dalloc = res.dname, res.dparent, res.dsize, res.dalloc
    dfiles, dfolders, dmtime = res.dfiles, res.dfolders, res.dmtime
    fsdecode = os.fsdecode
    rest = b""
    n = 0
    try:
        while True:
            chunk = p.stdout.read(1 << 20)
            if not chunk:
                break
            if cancel.is_set():
                raise Cancelled()
            recs = (rest + chunk).split(b"\0")
            rest = recs.pop()
            for rec in recs:
                parts = rec.split(b" ", 6)
                if len(parts) < 7:
                    continue
                yd, sz, blk, m, nl, ino, name = parts
                depth = int(yd[1:]) + off
                if depth > len(stack):              # parent was not listed: skip
                    continue
                parent = stack[depth - 1]
                dot = m.find(b".")
                mtime = int(m[:dot] if dot >= 0 else m)
                alloc = int(blk) << 9
                if yd[0] == 100:                        # "d": a folder
                    idx = len(dname)
                    dname.append(fsdecode(name))
                    dparent.append(parent)
                    dsize.append(0)
                    dalloc.append(alloc)
                    dfiles.append(0)
                    dfolders.append(0)
                    dmtime.append(mtime)
                    del stack[depth:]
                    stack.append(idx)
                    if off and depth == 1:            # a start folder of the batch
                        _match_root(res, expect, fsdecode(name), idx)
                    continue
                if nl != b"1":
                    if ino in seen_inodes:              # another name for a file we counted
                        alloc = 0
                    else:
                        seen_inodes.add(ino)
                        if hard is not None:
                            hard.append((len(names), int(ino)))
                size = int(sz)
                # extension (trailing version numbers skipped: libfoo.so.6 -> .so)
                key = b""
                dot = name.rfind(b".")
                if dot > 0:
                    key = name[dot:]
                    while key[1:].isdigit():
                        cut = name.rfind(b".", 0, dot)
                        if cut <= 0:
                            key = b""
                            break
                        key, dot = name[cut:dot], cut
                    if len(key) > 12 or b" " in key:
                        key = b""
                    else:
                        key = key.lower()
                e = ext_index.get(key)
                if e is None:
                    e = ext_index[key] = len(exts)
                    exts.append(fsdecode(key) if key else NO_EXT)
                    ext_stats.append([0, 0, 0])
                est = ext_stats[e]
                est[0] += size
                est[1] += alloc
                est[2] += 1
                names_add(name)
                fparent_add(parent)
                fsize_add(size)
                falloc_add(alloc)
                fmtime_add(mtime)
                fext_add(e)
            n += len(recs)
            if progress:
                progress(n)
    finally:
        if cancel.is_set():
            p.kill()
        p.stdout.close()
        p.wait()
        t.join(2)
        p.stderr.close()
    res._set_names(names)
    res.denied = sum(1 for line in (errs[0] if errs else b"").splitlines()
                     if b"Permission denied" in line)


def _match_root(res, expect, name, idx):
    for i, path in enumerate(expect):
        if os.path.basename(path.rstrip("/")) == name:
            res.batch_roots.append((path, idx))
            del expect[i]
            return


def _scan_python(res, progress, cancel, hard=None, starts=None):
    if starts:
        res._add_dir("", -1, 0, 0)
        res.batch_roots = []
        stack = []
        for path in starts:
            try:
                st = os.lstat(path)
            except OSError:
                continue
            i = res._add_dir(os.path.basename(path), 0, int(st.st_mtime), st.st_blocks * 512)
            res.batch_roots.append((path, i))
            stack.append((path, i, st.st_dev))
        stack.reverse()
    else:
        try:
            st = os.lstat(res.root)
        except OSError:
            st = None
        res._add_dir(res.root, -1, int(st.st_mtime) if st else 0, st.st_blocks * 512 if st else 0)
        stack = [(res.root, 0, st.st_dev if st else None)]
    seen_inodes = set()
    n = 0
    while stack:
        if cancel.is_set():
            raise Cancelled()
        path, d, dev = stack.pop()
        try:
            it = os.scandir(path)
        except OSError:
            res.denied += 1
            continue
        with it:
            for e in it:
                try:
                    s = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                n += 1
                if e.is_dir(follow_symlinks=False):
                    if s.st_dev != dev:           # another file system mounted here
                        continue
                    sub = res._add_dir(e.name, d, int(s.st_mtime), s.st_blocks * 512)
                    stack.append((e.path, sub, dev))
                else:
                    alloc = s.st_blocks * 512
                    if s.st_nlink > 1:
                        if s.st_ino in seen_inodes:
                            alloc = 0
                        else:
                            seen_inodes.add(s.st_ino)
                            if hard is not None:
                                hard.append((res.nfiles, s.st_ino))
                    res._add_file(os.fsencode(e.name), d, s.st_size, alloc, int(s.st_mtime))
        if progress and n % 2000 < 50:
            progress(n)
