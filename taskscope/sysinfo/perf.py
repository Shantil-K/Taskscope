"""Performance counters: CPU, memory, disks, network and GPUs.

Everything except NVIDIA GPUs comes from /proc and /sys. GPU support picks a
backend per card: nvidia-smi for NVIDIA, amdgpu sysfs for AMD, i915/xe sysfs
for Intel, and a name-only entry for anything else.
"""

import glob
import os
import re
import socket
import time

from .probe import read, read_fast, read_int, run, which


class _Cached:
    """Re-run an expensive function at most every *ttl* seconds."""

    def __init__(self, fn, ttl):
        self.fn, self.ttl, self.t, self.v = fn, ttl, -1e9, None

    def __call__(self):
        now = time.monotonic()
        if now - self.t >= self.ttl:
            self.v, self.t = self.fn(), now
        return self.v


# ---------------------------------------------------------------- CPU ----

def cpu_model():
    for line in read("/proc/cpuinfo").splitlines():
        k, _, v = line.partition(":")
        if k.strip() in ("model name", "Model", "Hardware", "cpu model"):
            return v.strip()
    return os.uname().machine


def _cpu_times():
    out = []
    for line in read_fast("/proc/stat", 65536).splitlines():
        if line.startswith("cpu"):
            parts = line.split()
            vals = [int(x) for x in parts[1:]]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            # guest time is already counted in user/nice
            total = sum(vals[:8])
            kernel = vals[2] + sum(vals[5:7])          # system + irq + softirq
            out.append((parts[0], total, idle, kernel))
    return out


_FREQ_FILES = None


def _cpu_freq_mhz():
    global _FREQ_FILES
    if _FREQ_FILES is None:
        _FREQ_FILES = glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq")
    vals = [read_int(p) for p in _FREQ_FILES]
    vals = [v for v in vals if v]
    if vals:
        return sum(vals) / len(vals) / 1000.0
    mhz = [float(l.split(":")[1]) for l in read("/proc/cpuinfo").splitlines()
           if l.startswith("cpu MHz")]
    return sum(mhz) / len(mhz) if mhz else None


def _find_cpu_temp_file():
    preferred = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz")
    found = {}
    for hw in glob.glob("/sys/class/hwmon/hwmon*"):
        name = read(hw + "/name").strip()
        if name and os.path.exists(hw + "/temp1_input"):
            found.setdefault(name, hw + "/temp1_input")
    for n in preferred:
        if n in found:
            return found[n]
    for tz in glob.glob("/sys/class/thermal/thermal_zone*"):
        t = read(tz + "/type")
        if "cpu" in t.lower() or "x86_pkg" in t:
            return tz + "/temp"
    return ""


_TEMP_FILE = None


def _cpu_temp():
    """Package temperature in C (sensor located once, then one read per tick)."""
    global _TEMP_FILE
    if _TEMP_FILE is None:
        _TEMP_FILE = _find_cpu_temp_file()
    t = read_int(_TEMP_FILE) if _TEMP_FILE else None
    return t / 1000.0 if t else None


# ---------------------------------------------------------------- disks --

def _whole_disks():
    disks = []
    for path in sorted(glob.glob("/sys/block/*")):
        name = os.path.basename(path)
        if re.match(r"^(loop|ram|zram|dm-|sr|fd|md)", name):
            continue
        if read_int(path + "/size", 0) == 0:
            continue
        disks.append(name)
    return disks


def _disk_kind(name):
    if name.startswith("nvme"):
        return "NVMe SSD"
    if read_int(f"/sys/block/{name}/removable", 0):
        return "Removable"
    rot = read_int(f"/sys/block/{name}/queue/rotational", 1)
    return "HDD" if rot else "SSD"


def _disk_model(name):
    m = read(f"/sys/block/{name}/device/model").strip()
    return m or name


def _mounts_by_disk():
    """Map whole-disk name -> list of mount points of its partitions."""
    res = {}
    for line in read("/proc/mounts").splitlines():
        dev, mnt = line.split()[:2]
        if not dev.startswith("/dev/"):
            continue
        try:
            real = os.path.basename(os.path.realpath(dev))
        except OSError:
            continue
        parent = real
        part_link = f"/sys/class/block/{real}/partition"
        if os.path.exists(part_link):
            parent = os.path.basename(os.path.dirname(os.path.realpath(f"/sys/class/block/{real}")))
        res.setdefault(parent, []).append(mnt.replace("\\040", " "))
    return res


# ---------------------------------------------------------------- network

def _net_ifaces():
    out = []
    for line in read_fast("/proc/net/dev", 65536).splitlines()[2:]:
        name, _, data = line.partition(":")
        name = name.strip()
        if name == "lo":
            continue
        v = data.split()
        out.append((name, int(v[0]), int(v[8])))
    return out


def _iface_kind(name):
    if os.path.isdir(f"/sys/class/net/{name}/wireless") or name.startswith("wl"):
        return "Wi-Fi"
    if not os.path.exists(f"/sys/class/net/{name}/device"):
        return "Virtual"
    return "Ethernet"


def _iface_addrs():
    """iface -> list of addresses. Uses `ip`, then `ifconfig`, then nothing."""
    res = {}
    if which("ip"):
        for line in run(["ip", "-o", "addr", "show"]).splitlines():
            p = line.split()
            if len(p) >= 4:
                res.setdefault(p[1], []).append(p[3].split("/")[0])
    elif which("ifconfig"):
        cur = None
        for line in run(["ifconfig", "-a"]).splitlines():
            if line and not line[0].isspace():
                cur = line.split(":")[0].split()[0]
            m = re.search(r"inet6? (?:addr:)?([0-9a-fA-F:.]+)", line)
            if cur and m:
                res.setdefault(cur, []).append(m.group(1))
    return res


# ---------------------------------------------------------------- GPUs ---

_PCI_VENDORS = {"0x10de": "NVIDIA", "0x1002": "AMD", "0x8086": "Intel"}


def _lspci_names():
    names = {}
    if which("lspci"):
        for line in run(["lspci", "-D", "-mm"]).splitlines():
            m = re.match(r'(\S+) "([^"]*)" "([^"]*)" "([^"]*)"', line)
            if m and ("VGA" in m.group(2) or "3D" in m.group(2) or "Display" in m.group(2)):
                names[m.group(1)] = f"{m.group(3)} {m.group(4)}"
    return names


class GpuReader:
    def __init__(self):
        self.cards = []   # dicts with id, name, backend, path, pci
        names = _lspci_names()
        from . import nvml
        self.nvml = nvml.init()
        self.nvml_by_pci = {}
        if self.nvml:
            for h in self.nvml.handles:
                self.nvml_by_pci[self.nvml.pci(h)] = h
        self.nvidia = bool(self.nvml or which("nvidia-smi"))
        self._smi = _Cached(self._nvidia_smi, 2.0)  # spawn at most every 2 s
        seen_pci = set()
        for card in sorted(glob.glob("/sys/class/drm/card[0-9]")):
            dev = os.path.realpath(card + "/device")
            pci = os.path.basename(dev)
            if pci in seen_pci:
                continue
            seen_pci.add(pci)
            vendor = read(card + "/device/vendor").strip()
            driver = os.path.basename(os.path.realpath(card + "/device/driver")) if os.path.exists(card + "/device/driver") else ""
            vname = _PCI_VENDORS.get(vendor, "GPU")
            name = names.get(pci, f"{vname} graphics ({driver or 'unknown driver'})")
            if driver == "nvidia" and self.nvml and pci in self.nvml_by_pci:
                backend = "nvml"
            elif driver == "nvidia" and self.nvidia:
                backend = "nvidia-smi"
            elif driver == "amdgpu":
                backend = "amdgpu"
            elif driver in ("i915", "xe"):
                backend = "intel"
            else:
                backend = "none"
            self.cards.append({"id": card, "name": name, "backend": backend,
                               "path": card + "/device", "pci": pci, "driver": driver})
        known = {c["pci"] for c in self.cards}
        if self.nvml:
            for pci, h in self.nvml_by_pci.items():
                if pci not in known:   # headless / no DRM node
                    self.cards.append({"id": f"nvidia:{pci}", "name": self.nvml.name(h),
                                       "backend": "nvml", "path": "", "pci": pci, "driver": "nvidia"})
        elif self.nvidia and not any(c["backend"] == "nvidia-smi" for c in self.cards):
            for i, line in enumerate(run(["nvidia-smi", "--query-gpu=name,pci.bus_id",
                                          "--format=csv,noheader"]).splitlines()):
                self.cards.append({"id": f"nvidia{i}", "name": line.split(",")[0].strip(),
                                   "backend": "nvidia-smi", "path": "", "pci": "",
                                   "driver": "nvidia"})

    def _nvidia_smi(self):
        out = {}
        txt = run(["nvidia-smi", "--query-gpu=pci.bus_id,utilization.gpu,memory.used,"
                   "memory.total,temperature.gpu,power.draw,name",
                   "--format=csv,noheader,nounits"], timeout=3)
        for i, line in enumerate(txt.splitlines()):
            p = [x.strip() for x in line.split(",")]
            if len(p) < 7:
                continue

            def num(x):
                try:
                    return float(x)
                except ValueError:
                    return None
            bus = p[0].lower()
            # nvidia-smi prints 00000000:01:00.0; sysfs uses 0000:01:00.0
            bus = bus[4:] if len(bus) > 12 else bus
            out[bus] = out[i] = {"util": num(p[1]), "mem_used": (num(p[2]) or 0) * 2**20,
                                 "mem_total": (num(p[3]) or 0) * 2**20, "temp": num(p[4]),
                                 "power": num(p[5])}
        return out

    def sample(self):
        res = []
        nv = self._smi() if any(c["backend"] == "nvidia-smi" for c in self.cards) else {}
        nv_i = 0
        for c in self.cards:
            d = {"id": c["id"], "name": c["name"], "backend": c["backend"], "driver": c["driver"],
                 "pci": c.get("pci", ""),
                 "util": None, "mem_used": None, "mem_total": None, "temp": None, "power": None}
            if c["backend"] == "nvml":
                d.update(self.nvml.sample(self.nvml_by_pci[c["pci"]]))
            elif c["backend"] == "nvidia-smi":
                d.update(nv.get(c["pci"].lower(), nv.get(nv_i, {})))
                nv_i += 1
            elif c["backend"] == "amdgpu":
                p = c["path"]
                d["util"] = read_int(p + "/gpu_busy_percent")
                d["mem_used"] = read_int(p + "/mem_info_vram_used")
                d["mem_total"] = read_int(p + "/mem_info_vram_total")
                for hw in glob.glob(p + "/hwmon/hwmon*"):
                    t = read_int(hw + "/temp1_input")
                    if t:
                        d["temp"] = t / 1000.0
                    pw = read_int(hw + "/power1_average") or read_int(hw + "/power1_input")
                    if pw:
                        d["power"] = pw / 1e6
            elif c["backend"] == "intel":
                card = c["id"]
                cur = read_int(card + "/gt_act_freq_mhz") or read_int(card + "/gt_cur_freq_mhz")
                mx = read_int(card + "/gt_max_freq_mhz") or read_int(card + "/gt_RP0_freq_mhz")
                if cur is not None and mx:
                    # Clock ratio is a coarse but root-free load estimate.
                    d["util"] = min(100.0, cur * 100.0 / mx)
                    d["clock"] = cur
            res.append(d)
        return res


# ---------------------------------------------------------------- sampler

class PerfSampler:
    """Collects one snapshot per call; rates are computed between calls."""

    def __init__(self):
        self.model = cpu_model()
        self.ncpu = os.cpu_count() or 1
        self.gpu = GpuReader()
        self._cpu_prev = None
        self._disk_prev = {}
        self._net_prev = {}
        self._t = None
        self._addrs = _Cached(_iface_addrs, 10.0)
        self._mounts = _Cached(_mounts_by_disk, 10.0)
        self._disk_list = _Cached(_whole_disks, 10.0)
        self._static = {}   # per-device facts that never change

    def sample(self, light=False):
        """light: the window is hidden, so the GPUs (a slow driver call) are read
        every 5th time and the last reading is repeated in between."""
        now = time.monotonic()
        dt = (now - self._t) if self._t else None
        self._t = now
        self._n = getattr(self, "_n", 0) + 1
        if not light or self._n % 5 == 0 or getattr(self, "_gpus", None) is None:
            self._gpus = self.gpu.sample()
        return {
            "time": time.time(),
            "cpu": self._cpu(),
            "memory": self._memory(),
            "disks": self._disks(dt),
            "net": self._net(dt),
            "gpus": self._gpus,
        }

    def _cpu(self):
        times = _cpu_times()
        usage = []
        kernel = 0.0
        if self._cpu_prev and len(self._cpu_prev) == len(times):
            for k, ((n, t, i, kt), (_, pt, pi, pk)) in enumerate(zip(times, self._cpu_prev)):
                dt_, di = t - pt, i - pi
                usage.append(max(0.0, min(100.0, (dt_ - di) * 100.0 / dt_)) if dt_ > 0 else 0.0)
                if k == 0 and dt_ > 0:
                    kernel = max(0.0, min(100.0, (kt - pk) * 100.0 / dt_))
        else:
            usage = [0.0] * len(times)
        self._cpu_prev = times
        nproc = sum(1 for d in os.listdir("/proc") if d.isdigit())
        nthr = 0
        la = read_fast("/proc/loadavg", 65536).split()
        if len(la) >= 4:
            nthr = int(la[3].split("/")[1])
        return {
            "model": self.model, "total": usage[0] if usage else 0.0, "kernel": kernel,
            "cores": usage[1:], "freq_mhz": _cpu_freq_mhz(), "temp": _cpu_temp(),
            "processes": nproc, "threads": nthr,
            "load": [float(x) for x in la[:3]] if la else [],
            "uptime": float(read("/proc/uptime", "0").split()[0]),
        }

    @staticmethod
    def _memory():
        mi = {}
        for line in read_fast("/proc/meminfo", 65536).splitlines():
            k, _, v = line.partition(":")
            p = v.split()
            if p:
                mi[k] = int(p[0]) * 1024
        total = mi.get("MemTotal", 0)
        avail = mi.get("MemAvailable", mi.get("MemFree", 0) + mi.get("Cached", 0))
        return {
            "total": total, "available": avail, "used": total - avail,
            "cached": mi.get("Cached", 0) + mi.get("SReclaimable", 0),
            "buffers": mi.get("Buffers", 0), "shared": mi.get("Shmem", 0),
            "swap_total": mi.get("SwapTotal", 0),
            "swap_used": mi.get("SwapTotal", 0) - mi.get("SwapFree", 0),
            "percent": (total - avail) * 100.0 / total if total else 0.0,
        }

    def _disks(self, dt):
        stats = {}
        for line in read_fast("/proc/diskstats", 65536).splitlines():
            p = line.split()
            if len(p) >= 14:
                # sectors read, sectors written, ms doing I/O
                stats[p[2]] = (int(p[5]) * 512, int(p[9]) * 512, int(p[12]))
        mounts = self._mounts()
        out = []
        for name in self._disk_list():
            s = stats.get(name)
            if not s:
                continue
            prev = self._disk_prev.get(name)
            rr = wr = busy = 0.0
            if prev and dt:
                rr = (s[0] - prev[0]) / dt
                wr = (s[1] - prev[1]) / dt
                busy = min(100.0, (s[2] - prev[2]) / (dt * 10.0))
            self._disk_prev[name] = s
            st = self._static.get("disk:" + name)
            if st is None:
                st = self._static["disk:" + name] = {
                    "model": _disk_model(name), "kind": _disk_kind(name),
                    "size": read_int(f"/sys/block/{name}/size", 0) * 512}
            out.append({"name": name, **st,
                        "read_rate": rr, "write_rate": wr, "busy": busy,
                        "mounts": mounts.get(name, [])})
        return out

    def _net(self, dt):
        addrs = self._addrs()
        out = []
        for name, rx, tx in _net_ifaces():
            prev = self._net_prev.get(name)
            rxr = txr = 0.0
            if prev and dt:
                rxr, txr = max(0, rx - prev[0]) / dt, max(0, tx - prev[1]) / dt
            self._net_prev[name] = (rx, tx)
            st = self._static.get("net:" + name)
            if st is None:
                st = self._static["net:" + name] = {
                    "kind": _iface_kind(name),
                    "mac": read(f"/sys/class/net/{name}/address").strip()}
            up = read(f"/sys/class/net/{name}/operstate").strip() == "up"
            speed = read_int(f"/sys/class/net/{name}/speed") if up else None
            out.append({"name": name, "kind": st["kind"], "mac": st["mac"],
                        "up": up,
                        "rx_rate": rxr, "tx_rate": txr, "rx_total": rx, "tx_total": tx,
                        "speed_mbps": speed if speed and speed > 0 else None,
                        "addrs": addrs.get(name, [])})
        return out


def hostname():
    return socket.gethostname()
