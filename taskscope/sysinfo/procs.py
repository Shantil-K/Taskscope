"""Process list read directly from /proc (no psutil required)."""

import os
import pwd
import signal
import time

from . import elevate
from .probe import read, read_fast

CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")
NCPU = os.cpu_count() or 1

STATES = {"R": "Running", "S": "Sleeping", "D": "Disk sleep", "Z": "Zombie",
          "T": "Stopped", "t": "Traced", "I": "Idle", "X": "Dead", "P": "Parked"}

_user_cache = {}


def username(uid):
    if uid not in _user_cache:
        try:
            _user_cache[uid] = pwd.getpwuid(uid).pw_name
        except KeyError:
            _user_cache[uid] = str(uid)
    return _user_cache[uid]


class Proc:
    __slots__ = ("pid", "ppid", "name", "user", "uid", "state", "cpu", "rss",
                 "threads", "nice", "cmdline", "start", "io_read", "io_write",
                 "read_rate", "write_rate", "exe", "vsize", "utime", "stime")

    def as_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}


def _parse_stat(text):
    # The comm field may contain spaces and parentheses; split around the last ')'.
    l, r = text.find("("), text.rfind(")")
    comm = text[l + 1:r]
    rest = text[r + 2:].split()
    return comm, rest


class _StatFiles:
    """/proc/<pid>/stat kept open between samples and re-read with pread():
    about half the cost of opening each file every second. A file belongs to
    the process it was opened for (once that exits, reads fail and the file is
    closed), so a reused PID can never be confused with the old one. The number
    kept open stays well under the process's file limit."""

    def __init__(self):
        try:
            import resource
            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            want = hard if hard != resource.RLIM_INFINITY else 65536
            if soft != resource.RLIM_INFINITY and soft < min(want, 16384):
                resource.setrlimit(resource.RLIMIT_NOFILE, (min(want, 16384), hard))
                soft = min(want, 16384)
            limit = soft if soft != resource.RLIM_INFINITY else 16384
        except (ImportError, ValueError, OSError):
            limit = 1024
        self.budget = max(0, min(limit // 2, limit - 512))
        self.fds = {}

    def read(self, pid):
        fd = self.fds.get(pid)
        if fd is not None:
            try:
                data = os.pread(fd, 4096, 0)
                if data:
                    return data.decode("utf-8", "replace")
            except OSError:
                pass
            os.close(fd)                     # the process is gone (or the PID was reused)
            del self.fds[pid]
            return read_fast(f"/proc/{pid}/stat") if os.path.isdir(f"/proc/{pid}") else ""
        if len(self.fds) >= self.budget:
            return read_fast(f"/proc/{pid}/stat")
        try:
            fd = os.open(f"/proc/{pid}/stat", os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        except OSError:
            return ""
        try:
            data = os.pread(fd, 4096, 0)
        except OSError:
            os.close(fd)
            return ""
        self.fds[pid] = fd
        return data.decode("utf-8", "replace")

    def prune(self, live):
        for pid in [p for p in self.fds if p not in live]:
            os.close(self.fds.pop(pid))


class ProcessSampler:
    """Keeps the previous sample so CPU and I/O rates can be computed."""

    def __init__(self):
        self._prev = {}       # pid -> (cpu ticks, read bytes, write bytes)
        self._prev_time = None
        self._uid = os.geteuid()  # /proc/<pid>/io is only readable for our own processes
        self._static = {}     # (pid, starttime) -> (name, cmdline, exe, uid, comm)
        self._boot = self._boot_time()
        self._stat = _StatFiles()

    @staticmethod
    def _boot_time():
        for line in read("/proc/stat").splitlines():
            if line.startswith("btime"):
                return int(line.split()[1])
        return time.time()

    def sample(self, with_io=True):
        """One pass over /proc. Per tick this reads only /proc/<pid>/stat (and
        /proc/<pid>/io when *with_io*); name, command line, executable and
        owner are read once per process and cached by (pid, start time)."""
        now = time.monotonic()
        dt = (now - self._prev_time) if self._prev_time else None
        procs, cur, static = [], {}, {}
        try:
            pids = [int(p) for p in os.listdir("/proc") if p.isdigit()]
        except OSError:
            pids = []
        self._stat.prune(set(pids))
        for pid in pids:
            base = f"/proc/{pid}"
            st = self._stat.read(pid)
            if not st:
                continue
            try:
                comm, f = _parse_stat(st)
                starttime = int(f[19])
                p = Proc()
                p.pid = pid
                p.state = STATES.get(f[0], f[0])
                p.ppid = int(f[1])
                ticks = int(f[11]) + int(f[12])
                p.nice = int(f[16])
                p.threads = int(f[17])
                p.start = self._boot + starttime / CLK_TCK
                p.rss = int(f[21]) * PAGE
                p.vsize = int(f[20])
                p.utime = int(f[11]) / CLK_TCK
                p.stime = int(f[12]) / CLK_TCK
            except (IndexError, ValueError):
                continue
            key = (pid, starttime)
            info = self._static.get(key)
            if info is None or comm != info[4]:  # comm changes on exec()
                info = self._read_static(base, comm)
                if info is None:
                    continue
            static[key] = info
            p.name, p.cmdline, p.exe, p.uid, _ = info
            p.user = username(p.uid)
            rb = wb = None
            if with_io and (p.uid == self._uid or self._uid == 0):
                io = read_fast(base + "/io")
                if io:
                    for line in io.splitlines():
                        if line.startswith("read_bytes:"):
                            rb = int(line.split()[1])
                        elif line.startswith("write_bytes:"):
                            wb = int(line.split()[1])
            p.io_read, p.io_write = rb, wb
            prev = self._prev.get(pid)
            if prev and dt:
                p.cpu = max(0.0, (ticks - prev[0]) / CLK_TCK / dt * 100.0 / NCPU)
                p.read_rate = (rb - prev[1]) / dt if rb is not None and prev[1] is not None else None
                p.write_rate = (wb - prev[2]) / dt if wb is not None and prev[2] is not None else None
            else:
                p.cpu, p.read_rate, p.write_rate = 0.0, None, None
            cur[pid] = (ticks, rb, wb)
            procs.append(p)
        self._prev, self._prev_time, self._static = cur, now, static
        return procs

    def sample_top(self, n=40):
        """The *n* busiest processes (by CPU, then memory), at a fraction of the
        cost of sample(): every process gets only its CPU ticks and memory
        read; names and the rest are filled in for the top *n* alone. Used for
        Home's history while the full list is not on screen. Shares the CPU
        baseline with sample(), so the two can alternate freely."""
        import heapq
        now = time.monotonic()
        dt = (now - self._prev_time) if self._prev_time else None
        cur, static, rows = {}, {}, []
        prev_all, cache = self._prev, self._static
        try:
            pids = [int(p) for p in os.listdir("/proc") if p.isdigit()]
        except OSError:
            pids = []
        self._stat.prune(set(pids))
        for pid in pids:
            st = self._stat.read(pid)
            if not st:
                continue
            f = st[st.rfind(")") + 2:].split()
            try:
                ticks = int(f[11]) + int(f[12])
                key = (pid, int(f[19]))
                rss = int(f[21]) * PAGE
            except (IndexError, ValueError):
                continue
            info = cache.get(key)
            if info is not None:
                static[key] = info
            prev = prev_all.get(pid)
            cpu = max(0.0, (ticks - prev[0]) / CLK_TCK / dt * 100.0 / NCPU) if prev and dt else 0.0
            cur[pid] = (ticks, None, None)       # no I/O read here: the next full read starts afresh
            rows.append((cpu, rss, pid, key, st))
        top = []
        for cpu, rss, pid, key, st in heapq.nlargest(n, rows, key=lambda r: (r[0], r[1])):
            comm, f = _parse_stat(st)
            info = static.get(key)
            if info is None or comm != info[4]:
                info = self._read_static(f"/proc/{pid}", comm)
                if info is None:
                    continue
                static[key] = info
            try:
                p = Proc()
                p.pid, p.ppid, p.state = pid, int(f[1]), STATES.get(f[0], f[0])
                p.nice, p.threads = int(f[16]), int(f[17])
                p.start = self._boot + int(f[19]) / CLK_TCK
                p.rss, p.vsize = rss, int(f[20])
                p.utime, p.stime = int(f[11]) / CLK_TCK, int(f[12]) / CLK_TCK
            except (IndexError, ValueError):
                continue
            p.name, p.cmdline, p.exe, p.uid, _ = info
            p.user = username(p.uid)
            p.cpu = cpu
            p.io_read = p.io_write = p.read_rate = p.write_rate = None
            top.append(p)
        self._prev, self._prev_time, self._static = cur, now, static
        return top

    @staticmethod
    def _read_static(base, comm):
        try:
            uid = os.stat(base).st_uid
        except OSError:
            return None
        raw = read_fast(base + "/cmdline", 65536)
        cmdline = raw.replace("\0", " ").strip()
        # Prefer the full executable name over the 15-char comm when possible.
        name = comm
        if raw:
            first = raw.split("\0", 1)[0]
            if "\0" not in raw.rstrip("\0"):
                # Programs like Chromium rewrite argv as one space-joined string.
                first = first.split(" ", 1)[0]
            first = os.path.basename(first)
            if first.startswith(comm[:15]) and len(first) > len(comm):
                name = first
        try:
            exe = os.readlink(base + "/exe")
        except OSError:
            exe = ""
        return (name, cmdline, exe, uid, comm)


# ---- actions -------------------------------------------------------------

def _signal(pid, sig):
    try:
        os.kill(pid, sig)
        return True, ""
    except PermissionError:
        num = int(sig)
        return elevate.run_root(["kill", f"-{num}", str(pid)])
    except ProcessLookupError:
        return False, "The process has already exited."


def terminate(pid):
    return _signal(pid, signal.SIGTERM)


def kill(pid):
    return _signal(pid, signal.SIGKILL)


def suspend(pid):
    return _signal(pid, signal.SIGSTOP)


def resume(pid):
    return _signal(pid, signal.SIGCONT)


def renice(pid, value):
    try:
        os.setpriority(os.PRIO_PROCESS, pid, value)
        return True, ""
    except PermissionError:
        return elevate.run_root(["renice", "-n", str(value), "-p", str(pid)])
    except ProcessLookupError:
        return False, "The process has already exited."


def panel_info(pid):
    """Cheap extra facts for the selected process only (one /proc/<pid>/status
    read plus an fd count)."""
    base = f"/proc/{pid}"
    info = {}
    for line in read_fast(base + "/status", 16384).splitlines():
        k, _, v = line.partition(":")
        if k in ("VmHWM", "RssShmem", "VmSwap"):
            p = v.split()
            info[k] = int(p[0]) * 1024 if p else None
    try:
        info["fds"] = len(os.listdir(base + "/fd"))
    except OSError:
        info["fds"] = None
    cg = read_fast(base + "/cgroup").strip().splitlines()
    info["cgroup"] = cg[-1].split(":", 2)[-1] if cg else ""
    return info


def details(pid):
    """Extra, slower information for a properties dialog."""
    base = f"/proc/{pid}"
    out = {"PID": pid}
    status = read(base + "/status")
    for line in status.splitlines():
        k, _, v = line.partition(":")
        if k in ("Name", "State", "PPid", "Uid", "Threads", "VmPeak", "VmSize",
                 "VmRSS", "VmSwap", "voluntary_ctxt_switches",
                 "nonvoluntary_ctxt_switches", "Cpus_allowed_list"):
            out[k] = v.strip()
    out["Command line"] = read(base + "/cmdline").replace("\0", " ").strip()
    for key, link in (("Executable", "exe"), ("Working dir", "cwd")):
        try:
            out[key] = os.readlink(f"{base}/{link}")
        except OSError:
            out[key] = "(not readable)"
    try:
        out["Open files"] = len(os.listdir(base + "/fd"))
    except OSError:
        out["Open files"] = "(not readable)"
    cg = read(base + "/cgroup").strip().splitlines()
    if cg:
        out["Cgroup"] = cg[-1].split(":", 2)[-1]
    return out
