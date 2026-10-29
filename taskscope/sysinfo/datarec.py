"""Record Taskscope's live data to a file and play it back later.

A recording is a gzip-compressed JSON-lines file (*.tsrec):

    {"type": "header", "v": 1, "host": ..., "start": <unix time>, "interval_ms": ...}
    {"t": <unix time>, "perf": {...}, "p": [[pid, ppid, cpu, rss, ...], ...], "new": {pid: [...]}}
    ...

"perf" is the Performance sampler's snapshot as is. Processes are stored
compactly: the numbers that change every second go in "p", while the name,
user, command line and so on are written only when a process first appears
("new"). About 5-10 KB per second after compression.

A small sidecar file (<name>.tsrec.json) keeps the summary (start, end,
samples) so the list of recordings can be shown without reading every file.
"""

import gzip
import json
import os
import socket
import time

from .procs import Proc

SUFFIX = ".tsrec"
# per-second process fields, in order
DYN = ("pid", "ppid", "cpu", "rss", "threads", "nice", "state", "read_rate", "write_rate",
       "vsize", "utime", "stime")
# fields written once per process
STATIC = ("name", "user", "uid", "cmdline", "start", "exe")


def default_dir():
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "taskscope", "recordings")


def _round(o):
    """Round floats to 2 decimals (smaller files, nothing visible lost)."""
    if isinstance(o, float):
        return round(o, 2)
    if isinstance(o, dict):
        return {k: _round(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_round(v) for v in o]
    return o


class Recorder:
    def __init__(self):
        self.fh = None
        self.path = None
        self.started = 0.0
        self.frames = 0
        self._known = {}           # pid -> the name/user/command line last written for it
        self._last_index = 0.0

    @property
    def active(self):
        return self.fh is not None

    def start(self, folder=None, interval_ms=1000):
        folder = folder or default_dir()
        os.makedirs(folder, exist_ok=True)
        self.started = time.time()
        name = time.strftime("taskscope-%Y%m%d-%H%M%S", time.localtime(self.started)) + SUFFIX
        self.path = os.path.join(folder, name)
        self.fh = gzip.open(self.path, "wt", encoding="utf-8", compresslevel=6)
        self.frames = 0
        self._known = {}
        self.interval_ms = interval_ms
        self.host = socket.gethostname()
        self._write({"type": "header", "v": 1, "host": self.host, "start": self.started,
                     "interval_ms": interval_ms})
        self._index()
        return self.path

    def _write(self, obj):
        self.fh.write(json.dumps(obj, separators=(",", ":")))
        self.fh.write("\n")

    def add(self, snapshot):
        """Store one tick: {"perf": ..., "procs": [Proc, ...]}."""
        if not self.fh or not snapshot.get("perf"):
            return
        rows, new = [], {}
        for p in snapshot.get("procs") or ():
            static = [getattr(p, k) for k in STATIC]
            if self._known.get(p.pid) != static:      # new process, or it changed its name (exec)
                self._known[p.pid] = static
                new[p.pid] = static
            rows.append([round(v, 2) if isinstance(v, float) else v
                         for v in (getattr(p, k) for k in DYN)])
        frame = {"t": round(time.time(), 3), "perf": _round(snapshot["perf"]), "p": rows}
        if new:
            frame["new"] = new
        self._write(frame)
        self.frames += 1
        if time.monotonic() - self._last_index > 30:     # summary survives a crash
            self.fh.flush()
            self._index()

    def _index(self, end=None):
        self._last_index = time.monotonic()
        info = {"start": self.started, "end": end or time.time(), "frames": self.frames,
                "host": self.host, "interval_ms": self.interval_ms}
        try:                                    # keep a name given while recording
            with open(self.path + ".json") as f:
                old = json.load(f)
            if old.get("title"):
                info["title"] = old["title"]
        except (OSError, ValueError):
            pass
        try:
            with open(self.path + ".json", "w") as f:
                json.dump(info, f)
        except OSError:
            pass

    def stop(self):
        if not self.fh:
            return None
        self.fh.close()
        self.fh = None
        self._index(time.time())
        return self.path


class Player:
    """A loaded recording. frame(i) returns a snapshot like the live one."""

    def __init__(self, path):
        self.path = path
        self.header = {}
        self.frames = []
        self._static = []           # per frame: pid -> static fields valid at that point
        static = {}
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except ValueError:
                    break                      # a recording cut off mid-line: keep what we have
                if obj.get("type") == "header":
                    self.header = obj
                    continue
                if "new" in obj:
                    static = dict(static)
                    for pid, vals in obj["new"].items():
                        static[int(pid)] = vals
                self.frames.append(obj)
                self._static.append(static)
        if not self.frames:
            raise ValueError("the recording holds no samples")
        self.interval_ms = int(self.header.get("interval_ms") or 1000)

    def __len__(self):
        return len(self.frames)

    def time_at(self, i):
        return self.frames[i]["t"]

    def frame(self, i):
        f = self.frames[i]
        static = self._static[i]
        plist = []
        for row in f["p"]:
            p = Proc()
            for k, v in zip(DYN, row):
                setattr(p, k, v)
            s = static.get(p.pid)
            for k, v in zip(STATIC, s or ("?", "?", -1, "", 0, "")):
                setattr(p, k, v)
            p.io_read = p.io_write = None
            plist.append(p)
        return {"perf": f["perf"], "procs": plist, "time": f["t"]}


def list_recordings(folder=None):
    folder = folder or default_dir()
    out = []
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    for n in names:
        if not n.endswith(SUFFIX):
            continue
        path = os.path.join(folder, n)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        info = {}
        try:
            with open(path + ".json") as f:
                info = json.load(f)
        except (OSError, ValueError):
            pass
        start = info.get("start") or os.path.getmtime(path)
        end = info.get("end") or start
        out.append({"path": path, "name": n, "start": start, "duration": max(0, end - start),
                    "frames": info.get("frames"), "host": info.get("host", ""), "size": size,
                    "title": info.get("title", "")})
    return sorted(out, key=lambda r: r["start"], reverse=True)


# ---- summaries and names (for the Recordings page) -------------------------

def set_title(path, title):
    """Give a recording a friendly name (kept in its summary file)."""
    info = {}
    try:
        with open(path + ".json") as f:
            info = json.load(f)
    except (OSError, ValueError):
        pass
    info["title"] = title.strip()
    with open(path + ".json", "w") as f:
        json.dump(info, f)


def summarize(path, points=400):
    """What happened in a recording, read in one pass: averages and peaks for
    CPU, memory, disk, network, GPU and temperatures, the processes that used
    the most CPU and memory, and a downsampled timeline for a chart."""
    n = 0
    t0 = t1 = None
    cpu_sum = mem_sum = disk_sum = 0.0
    cpu_max, cpu_max_t, cpu_max_i = -1.0, None, 0
    mem_max, mem_max_t, mem_total = -1.0, None, 0
    disk_max, disk_max_t = -1.0, None
    net_peak, net_peak_t = -1.0, None
    rx_total = tx_total = 0.0
    gpu_max = gtemp_max = ctemp_max = None
    procs_lo = procs_hi = None
    names, per_name = {}, {}
    timeline = []                       # (t, cpu %, memory %, disk %, net B/s)
    last_t = None
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except ValueError:
                break
            if obj.get("type") == "header":
                continue
            t = obj.get("t")
            t0 = t if t0 is None else t0
            t1 = t
            dt = (t - last_t) if last_t is not None and t is not None else 0
            last_t = t
            perf = obj.get("perf") or {}
            c = perf.get("cpu") or {}
            m = perf.get("memory") or {}
            cpu = c.get("total") or 0.0
            mem = m.get("percent") or 0.0
            cpu_sum += cpu
            mem_sum += mem
            if cpu > cpu_max:
                cpu_max, cpu_max_t, cpu_max_i = cpu, t, n
            if mem > mem_max:
                mem_max, mem_max_t, mem_total = mem, t, m.get("total", 0)
            busy = max([d.get("busy") or 0 for d in perf.get("disks") or []] or [0])
            disk_sum += busy
            if busy > disk_max:
                disk_max, disk_max_t = busy, t
            nets = [x for x in perf.get("net") or [] if x.get("up") and x.get("kind") != "Virtual"]
            rx = sum(x.get("rx_rate") or 0 for x in nets)
            tx = sum(x.get("tx_rate") or 0 for x in nets)
            rx_total += rx * dt
            tx_total += tx * dt
            if rx + tx > net_peak:
                net_peak, net_peak_t = rx + tx, t
            for g in perf.get("gpus") or ():
                if g.get("util") is not None:
                    gpu_max = max(gpu_max or 0.0, g["util"])
                if g.get("temp"):
                    gtemp_max = max(gtemp_max or 0.0, g["temp"])
            if c.get("temp"):
                ctemp_max = max(ctemp_max or 0.0, c["temp"])
            np_ = c.get("processes")
            if np_:
                procs_lo = np_ if procs_lo is None else min(procs_lo, np_)
                procs_hi = np_ if procs_hi is None else max(procs_hi, np_)
            timeline.append((t, cpu, mem, busy, rx + tx))
            for pid, vals in (obj.get("new") or {}).items():
                names[int(pid)] = vals[0]
            for row in obj.get("p") or ():
                acc = per_name.setdefault(names.get(row[0], "?"), [0.0, 0])
                acc[0] += row[2] or 0.0                  # CPU, summed over samples
                acc[1] = max(acc[1], row[3] or 0)        # largest memory seen
            n += 1
    k = max(n, 1)
    if len(timeline) > points:                           # keep peaks when thinning
        step = len(timeline) / points
        thin = []
        for i in range(points):
            chunk = timeline[int(i * step):int((i + 1) * step)] or [timeline[-1]]
            thin.append((chunk[0][0], max(x[1] for x in chunk), max(x[2] for x in chunk),
                         max(x[3] for x in chunk), max(x[4] for x in chunk)))
        timeline = thin
    top_cpu = sorted(per_name.items(), key=lambda kv: kv[1][0], reverse=True)[:5]
    top_mem = sorted(per_name.items(), key=lambda kv: kv[1][1], reverse=True)[:5]
    return {"samples": n, "start": t0, "end": t1,
            "cpu_avg": cpu_sum / k if n else None, "cpu_max": cpu_max if n else None,
            "cpu_max_time": cpu_max_t, "cpu_max_index": cpu_max_i,
            "mem_avg": mem_sum / k if n else None,
            "mem_max": mem_max if mem_max >= 0 else None, "mem_max_time": mem_max_t,
            "mem_total": mem_total, "disk_avg": disk_sum / k if n else None,
            "disk_max": disk_max if n else None, "disk_max_time": disk_max_t,
            "rx_total": rx_total, "tx_total": tx_total,
            "net_peak": net_peak if n else None, "net_peak_time": net_peak_t,
            "gpu_max": gpu_max, "gpu_temp_max": gtemp_max, "cpu_temp_max": ctemp_max,
            "procs_lo": procs_lo, "procs_hi": procs_hi,
            "interval": (t1 - t0) / (n - 1) if n > 1 and t0 is not None else None,
            "top": [(name, acc[0] / k, acc[1]) for name, acc in top_cpu],
            "top_mem": [(name, acc[1]) for name, acc in top_mem],
            "timeline": timeline}
