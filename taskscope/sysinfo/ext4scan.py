"""Fast disk scan for ext4 (also ext2/ext3), used when running as root.

Instead of asking the kernel about every file (one stat per file), this reads
the file system's own tables straight from the block device: directory
blocks give names and inode numbers, and inode tables give sizes, allocated
blocks and times. Inodes of files in the same folder usually sit next to
each other, so they are read in large chunks.

Reads are buffered through the block device's page cache, which is where the
ext4 driver keeps its metadata, so the view matches the mounted file system
(including changes not yet written to disk). Anything unusual (unsupported
features, encrypted folders, mismatching root folder) raises Unsupported and
the caller falls back to the normal scan.

Read-only: the device is opened O_RDONLY and nothing is ever written.
"""

import os
import struct

MAGIC = 0xEF53
INCOMPAT_FILETYPE = 0x2
INCOMPAT_META_BG = 0x10
INCOMPAT_EXTENTS = 0x40
INCOMPAT_64BIT = 0x80
INCOMPAT_INLINE_DATA = 0x8000
INCOMPAT_DIRDATA = 0x1000
RO_HUGE_FILE = 0x8

FL_ENCRYPT = 0x800
FL_HUGE_FILE = 0x40000
FL_EXTENTS = 0x80000
FL_INLINE_DATA = 0x10000000

S_IFMT, S_IFDIR = 0xF000, 0x4000

# mode, size_lo, mtime, links, blocks_lo, flags, i_block, size_high, blocks_high
INODE = struct.Struct("<H2xI8xI6xHII4x60s8xI4xH")
DIRENT = struct.Struct("<IHBB")
EXT_HDR = struct.Struct("<HHHHI")
EXT_LEAF = struct.Struct("<IHHI")
EXT_IDX = struct.Struct("<IIH2x")


class Unsupported(Exception):
    pass


def device_of(path):
    """/dev node holding *path*, from its st_dev."""
    st = os.stat(path)
    maj, mnr = os.major(st.st_dev), os.minor(st.st_dev)
    try:
        with open(f"/sys/dev/block/{maj}:{mnr}/uevent") as f:
            for line in f:
                if line.startswith("DEVNAME="):
                    return "/dev/" + line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def fs_type(path):
    """File system type of the mount holding *path* (from mountinfo)."""
    st = os.stat(path)
    want = f"{os.major(st.st_dev)}:{os.minor(st.st_dev)}"
    try:
        with open("/proc/self/mountinfo") as f:
            for line in f:
                p = line.split()
                if len(p) > 2 and p[2] == want and " - " in line:
                    return line.split(" - ", 1)[1].split()[0]
    except OSError:
        pass
    return ""


def usable(path):
    """True when the fast scan can be tried for *path*."""
    return os.geteuid() == 0 and fs_type(path) in ("ext4", "ext3", "ext2") and bool(device_of(path))


class Ext4:
    CHUNK = 1 << 16             # inode table read size (64 KiB)

    def __init__(self, dev):
        self.fd = os.open(dev, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        try:
            self._superblock()
        except BaseException:
            os.close(self.fd)
            raise
        self._chunks = {}

    def close(self):
        os.close(self.fd)

    def read(self, off, n):
        data = os.pread(self.fd, n, off)
        if len(data) < n:
            raise Unsupported("short read from the device")
        return data

    def _superblock(self):
        sb = self.read(1024, 1024)
        u32 = lambda o: struct.unpack_from("<I", sb, o)[0]      # noqa: E731
        u16 = lambda o: struct.unpack_from("<H", sb, o)[0]      # noqa: E731
        if u16(56) != MAGIC:
            raise Unsupported("not an ext2/3/4 file system")
        self.bs = 1024 << u32(24)
        self.first_data_block = u32(20)
        self.ipg = u32(40)
        self.inodes = u32(0)
        rev = u32(76)
        self.isz = u16(88) if rev >= 1 else 128
        self.incompat = u32(96)
        self.ro_compat = u32(100)
        if self.incompat & (INCOMPAT_META_BG | INCOMPAT_INLINE_DATA | INCOMPAT_DIRDATA):
            raise Unsupported("file system features this reader does not handle")
        self.filetype = bool(self.incompat & INCOMPAT_FILETYPE)
        self.huge = bool(self.ro_compat & RO_HUGE_FILE)
        is64 = bool(self.incompat & INCOMPAT_64BIT)
        dsz = u16(254) if is64 else 32
        if is64 and dsz < 64:
            dsz = 64
        ngroups = (self.inodes + self.ipg - 1) // self.ipg
        gdt = self.read((self.first_data_block + 1) * self.bs, ngroups * dsz)
        tables = []
        for g in range(ngroups):
            o = g * dsz
            lo = struct.unpack_from("<I", gdt, o + 8)[0]
            hi = struct.unpack_from("<I", gdt, o + 0x28)[0] if dsz >= 64 else 0
            tables.append(((hi << 32) | lo) * self.bs)
        self.tables = tables

    # ---- inodes ---------------------------------------------------------------
    def inode(self, ino):
        """(mode, size, alloc, mtime, links, flags, i_block)."""
        g, i = divmod(ino - 1, self.ipg)
        off = self.tables[g] + i * self.isz
        c, within = divmod(off, self.CHUNK)
        buf = self._chunks.get(c)
        if buf is None:
            if len(self._chunks) > 2048:          # ~128 MiB at most, then start over
                self._chunks.clear()
            buf = self._chunks[c] = os.pread(self.fd, self.CHUNK, c * self.CHUNK)
        if within + 118 > len(buf):              # inode crosses the chunk edge
            buf = self.read(off, 128)
            within = 0
        mode, size_lo, mtime, links, blo, flags, iblock, size_hi, bhi = INODE.unpack_from(buf, within)
        blocks = blo | ((bhi << 32) if self.huge else 0)
        alloc = blocks * self.bs if flags & FL_HUGE_FILE else blocks << 9
        return mode, size_lo | (size_hi << 32), alloc, mtime, links, flags, iblock

    # ---- file data blocks (for directories) ----------------------------------------
    def blocks(self, flags, iblock, size):
        """Physical block numbers holding the first *size* bytes of a file."""
        n = (size + self.bs - 1) // self.bs
        if flags & FL_INLINE_DATA:
            raise Unsupported("inline-data folder")
        if flags & FL_EXTENTS:
            out = []
            self._extents(iblock, out)
            return out[:n]
        return self._blockmap(iblock, n)

    def _extents(self, node, out):
        magic, entries, _mx, depth, _gen = EXT_HDR.unpack_from(node, 0)
        if magic != 0xF30A:
            raise Unsupported("bad extent header")
        for k in range(entries):
            o = 12 + 12 * k
            if depth == 0:
                _lblk, ln, hi, lo = EXT_LEAF.unpack_from(node, o)
                if ln > 32768:                     # unwritten extent: reads as zeros
                    continue
                start = (hi << 32) | lo
                out.extend(range(start, start + ln))
            else:
                _lblk, lo, hi = EXT_IDX.unpack_from(node, o)
                self._extents(self.read(((hi << 32) | lo) * self.bs, self.bs), out)

    def _blockmap(self, iblock, n):
        ptrs = struct.unpack_from("<15I", iblock, 0)
        out = [b for b in ptrs[:12]]
        per = self.bs // 4

        def walk(blk, level):
            if len(out) >= n:
                return
            if blk == 0:
                out.extend([0] * min(per ** level, n - len(out)))
                return
            arr = struct.unpack_from(f"<{per}I", self.read(blk * self.bs, self.bs), 0)
            for b in arr:
                if len(out) >= n:
                    return
                if level == 1:
                    out.append(b)
                else:
                    walk(b, level - 1)
        for level, blk in ((1, ptrs[12]), (2, ptrs[13]), (3, ptrs[14])):
            walk(blk, level)
        return out[:n]

    # ---- directories ------------------------------------------------------------------
    def entries(self, flags, iblock, size):
        """(inode, name bytes) of a folder, without "." and ".."."""
        bs = self.bs
        out = []
        add = out.append
        blks = self.blocks(flags, iblock, size)
        # read runs of consecutive blocks in one go
        i = 0
        while i < len(blks):
            j = i
            while j + 1 < len(blks) and blks[j + 1] == blks[j] + 1 and j - i < 255:
                j += 1
            if blks[i] == 0:                       # a hole
                i = j + 1
                continue
            data = self.read(blks[i] * bs, (j - i + 1) * bs)
            for base in range(0, len(data), bs):
                off, end = base, base + bs
                while off + 8 <= end:
                    ino, rec, nlen, _ft = DIRENT.unpack_from(data, off)
                    if rec < 8:
                        if rec == 0 and bs >= 65536:
                            rec = bs
                        else:
                            break                  # damaged block: skip the rest
                    if ino and nlen:
                        name = data[off + 8:off + 8 + nlen]
                        if name != b"." and name != b"..":
                            add((ino, name))
                    off += rec
            i = j + 1
        return out


def scan_into(res, root, progress=None, cancel=None, hard=None):
    """Fill ScanResult *res* (empty) for folder *root* of a mounted file
    system, in this process. Raises Unsupported when the fast path can't be used."""
    dev = device_of(root)
    check_root(dev, root)
    fs = Ext4(dev)
    try:
        _walk(fs, res, os.stat(root).st_ino, progress, cancel, hard)
    finally:
        fs.close()


def check_root(dev, root):
    """Raise Unsupported unless *root* on *dev* looks the same raw as through the kernel."""
    if not dev:
        raise Unsupported("no block device")
    fs = Ext4(dev)
    try:
        for _try in range(3):              # the folder may change while we look
            st = os.stat(root)
            mode, size, _a, mtime, _l, _f, _b = fs.inode(st.st_ino)
            if (mode & S_IFMT) == S_IFDIR and mtime == int(st.st_mtime) and size == st.st_size:
                return
        raise Unsupported("root folder does not match the device")
    finally:
        fs.close()


def scan_image(res, image, root_ino=2, progress=None, cancel=None):
    """Scan an unmounted ext4 image file (used by the tests)."""
    fs = Ext4(image)
    try:
        _walk(fs, res, root_ino, progress, cancel, None)
    finally:
        fs.close()


def scan_batch(res, dev, paths, progress=None, hard=None):
    """Worker side of the parallel scan: several folders of one mounted file
    system. Folder 0 is a stand-in parent; each start folder is recorded in
    res.batch_roots as (path, index), like the find-based batches."""
    items = []
    for path in paths:
        try:
            st = os.lstat(path)
        except OSError:
            continue
        items.append((path, st.st_ino))
    scan_inodes(res, dev, items, progress, hard)


def scan_inodes(res, dev, items, progress=None, hard=None):
    """items: (path, inode) of folders to scan. A folder that is gone (its
    inode is no longer a folder) is skipped, as a normal scan would."""
    fs = Ext4(dev)
    try:
        res._add_dir("", -1, 0, 0)
        res.batch_roots = []
        starts = []
        for path, ino in items:
            mode, size, alloc, mtime, _l, flags, iblock = fs.inode(ino)
            if (mode & S_IFMT) != S_IFDIR:
                continue
            idx = res._add_dir(os.path.basename(path), 0, mtime, alloc)
            res.batch_roots.append((path, idx))
            starts.append((idx, flags, iblock, size))
        _walk(fs, res, None, progress, None, hard, starts=starts)
    finally:
        fs.close()


def _walk(fs, res, root_ino, progress, cancel, hard, starts=None):
    from .diskscan import Cancelled, NO_EXT
    if starts is None:
        rmode, rsize, ralloc, rmtime, _l, rflags, rblock = fs.inode(root_ino)
        if (rmode & S_IFMT) != S_IFDIR:
            raise Unsupported("root is not a folder")
        res._add_dir(res.root, -1, rmtime, ralloc)
        starts = [(0, rflags, rblock, rsize)]
    names = []
    names_add = names.append
    fparent_add, fsize_add = res.fparent.append, res.fsize.append
    falloc_add, fmtime_add, fext_add = res.falloc.append, res.fmtime.append, res.fext.append
    dname, dparent, dsize, dalloc = res.dname, res.dparent, res.dsize, res.dalloc
    dfiles, dfolders, dmtime = res.dfiles, res.dfolders, res.dmtime
    ext_index, exts, ext_stats = res.ext_index, res.exts, res.ext_stats
    seen = set()
    inode = fs.inode
    fsdecode = os.fsdecode
    # depth-first like find, so folders are numbered parent-first
    stack = list(reversed(starts))
    n = total = 0
    while stack:
        d, flags, iblock, size = stack.pop()
        if flags & FL_ENCRYPT:
            raise Unsupported("encrypted folder")
        kids = []
        for ino, name in fs.entries(flags, iblock, size):
            mode, fsize, alloc, mtime, links, iflags, iblk = inode(ino)
            if (mode & S_IFMT) == S_IFDIR:
                idx = len(dname)
                dname.append(fsdecode(name))
                dparent.append(d)
                dsize.append(0)
                dalloc.append(alloc)
                dfiles.append(0)
                dfolders.append(0)
                dmtime.append(mtime)
                kids.append((idx, iflags, iblk, fsize))
                continue
            if links > 1:
                if ino in seen:            # another name for a file we counted
                    alloc = 0
                else:
                    seen.add(ino)
                    if hard is not None:
                        hard.append((len(names), ino))
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
            est[0] += fsize
            est[1] += alloc
            est[2] += 1
            names_add(name)
            fparent_add(d)
            fsize_add(fsize)
            falloc_add(alloc)
            fmtime_add(mtime)
            fext_add(e)
            n += 1
        stack.extend(reversed(kids))
        n += len(kids)
        if n > 20000:
            total += n
            n = 0
            if progress:
                progress(total)
        if cancel is not None and cancel.is_set():
            raise Cancelled()
    res._set_names(names)
