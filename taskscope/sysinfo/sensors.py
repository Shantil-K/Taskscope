"""Sensor tree for the Power & Freq page: clocks, temperatures, power,
utilisation, memory, drives, batteries and fans, grouped by device.

Everything is discovered once from sysfs; each sample is a handful of small
file reads (only while the page is open). CPU load, memory and GPU readings
reuse the Performance sampler's snapshot, so nothing is measured twice.

A node is a dict:
    id, parent, label, icon, unit, fn (reads the value, or None for a group),
    primary (id of the child whose value a group shows), tip, expanded
"""

import glob
import os
import re

from .perf import cpu_model
from .probe import read, read_fast

UNITS = ("GHz", "MHz", "C", "W", "%", "B", "RPM", "V", "text")

_CPU_CHIPS = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "cpu-thermal", "soc_thermal",
              "x86_pkg_temp", "via_cputemp")
_DRIVE_CHIPS = ("nvme", "drivetemp")
_SKIP_CHIPS = ("AC", "ADP1", "ACAD")            # mains adapters have no sensors worth showing
_FRIENDLY = {
    "acpitz": "ACPI thermal zone", "iwlwifi": "Wi-Fi adapter", "pch_": "Chipset (PCH)",
    "thinkpad": "ThinkPad embedded controller", "dell_smm": "Dell embedded controller",
    "asus": "ASUS embedded controller", "hp": "HP embedded controller",
    "nct": "Motherboard", "it87": "Motherboard", "it86": "Motherboard", "f71": "Motherboard",
    "w83": "Motherboard", "spd5118": "Memory module", "jc42": "Memory module",
    "amdgpu": "AMD GPU", "radeon": "AMD GPU", "nouveau": "NVIDIA GPU", "i915": "Intel graphics",
    "xe": "Intel graphics", "mt79": "Wi-Fi adapter", "ath1": "Wi-Fi adapter",
    "r8169": "Ethernet adapter", "igc": "Ethernet adapter", "BAT": "Battery",
}
_RAPL_NAMES = {"package-0": "CPU package", "package-1": "CPU package 2", "core": "CPU cores",
               "uncore": "Integrated graphics", "dram": "Memory (DRAM)", "psys": "Platform (whole system)"}


def _num(path, scale=1.0):
    s = read_fast(path, 64).strip()
    if not s:
        return None
    try:
        return int(s) / scale
    except ValueError:
        return None


def _text(path):
    return lambda perf: read_fast(path, 256).strip() or None


def _friendly(chip):
    for k, v in _FRIENDLY.items():
        if chip.startswith(k):
            return v
    return chip


def _clean_cpu(name):
    name = re.sub(r"\((R|TM|tm|r)\)", "", name)
    name = re.sub(r"\s+CPU\s*@.*$", "", name)
    name = re.sub(r"\s+\d+-Core Processor$", "", name)
    return re.sub(r"\s+", " ", name).strip()


def _clean_gpu(name):
    vendor = ""
    for long, short in (("NVIDIA", "NVIDIA"), ("Intel", "Intel"), ("Advanced Micro Devices", "AMD"),
                        ("AMD", "AMD")):
        if name.startswith(long):
            vendor = short
            break
    m = re.search(r"\[([^\]]+)\]\s*$", name)
    if m and vendor:
        model = m.group(1)
        return model if model.startswith(vendor) else f"{vendor} {model}"
    return name


class SensorTree:
    def __init__(self, perf=None):
        self.nodes = []
        self._energy = {}       # RAPL: path -> (energy µJ, monotonic time)
        self._add("pc", None, "This PC", "pc", expanded=True)
        self._cpu(perf)
        self._gpus(perf)
        self._memory(perf)
        self._drives()
        self._batteries()
        self._others()
        # Groups show their first child's value unless told otherwise.
        kids = {}
        for n in self.nodes:
            kids.setdefault(n["parent"], []).append(n)
        for n in self.nodes:
            if n["fn"] is None and n["primary"] is None and n["id"] != "pc":
                c = kids.get(n["id"])
                while c:
                    if c[0]["fn"] is not None:
                        n["primary"] = c[0]["id"]
                        break
                    c = kids.get(c[0]["id"])
        # Drop empty groups (a chip with no readable sensors).
        has_kids = set(kids)
        self.nodes = [n for n in self.nodes if n["fn"] is not None or n["id"] in has_kids]
        # A group already shows its primary reading, so a child row with the
        # same value would be repeated. Hide it and name it in the group's tooltip.
        by_id = {n["id"]: n for n in self.nodes}
        for n in self.nodes:
            c = by_id.get(n["primary"]) if n["primary"] else None
            if c is not None and c["parent"] == n["id"] and c["fn"] is not None:
                c["hidden"] = True
                n["tip"] = (n["tip"] + "\n" if n["tip"] else "") + "Value shown: " + c["label"]

    def _add(self, nid, parent, label, icon, unit=None, fn=None, primary=None, tip="",
             expanded=False):
        self.nodes.append({"id": nid, "parent": parent, "label": label, "icon": icon,
                           "unit": unit, "fn": fn, "primary": primary, "tip": tip,
                           "expanded": expanded, "hidden": False})

    # ---- processor -----------------------------------------------------
    def _cpu(self, perf):
        ncpu = os.cpu_count() or 1
        # The processor row shows the average clock, so it still says something
        # useful when the tree is collapsed (the Clocks row repeats it).
        self._add("cpu", "pc", _clean_cpu(cpu_model()), "chip", expanded=True,
                  primary="cpu.clk.avg", tip=cpu_model())
        # Clocks
        self._add("cpu.clk", "cpu", "Clocks", "clock", primary="cpu.clk.avg")
        dirs = sorted(glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq"),
                      key=lambda p: int(re.search(r"/cpu(\d+)/", p).group(1)))
        if dirs:
            files = [d + "/scaling_cur_freq" for d in dirs]

            def avg(perf, files=files):
                vals = [v for v in (_num(f, 1e6) for f in files) if v]
                return sum(vals) / len(vals) if vals else None
            self._add("cpu.clk.avg", "cpu.clk", "Average", "clock", "GHz", avg,
                      tip="Mean current clock of all CPUs")
            for f in files:
                i = re.search(r"/cpu(\d+)/", f).group(1)
                self._add(f"cpu.clk.{i}", "cpu.clk", f"CPU {i}", "clock", "GHz",
                          lambda perf, f=f: _num(f, 1e6))
        else:
            # No cpufreq driver (VMs, some ARM boards): /proc/cpuinfo has MHz lines.
            def mhz(perf):
                vals = [float(l.split(":")[1]) for l in read_fast("/proc/cpuinfo", 1 << 20).splitlines()
                        if l.startswith("cpu MHz")]
                return sum(vals) / len(vals) / 1000 if vals else None
            self._add("cpu.clk.avg", "cpu.clk", "Average", "clock", "GHz", mhz)
        # Utilisation (from the performance sampler)
        self._add("cpu.util", "cpu", "Utilization", "util", primary="cpu.util.total")
        self._add("cpu.util.total", "cpu.util", "Total", "util", "%",
                  lambda perf: perf["cpu"]["total"] if perf else None)
        for i in range(ncpu):
            self._add(f"cpu.util.{i}", "cpu.util", f"CPU {i}", "util", "%",
                      lambda perf, i=i: perf["cpu"]["cores"][i] if perf else None)
        # Temperatures
        self._add("cpu.temp", "cpu", "Temperatures", "thermo")
        ids = []
        for hw in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
            chip = read(hw + "/name").strip()
            if chip not in _CPU_CHIPS:
                continue
            for t in sorted(glob.glob(hw + "/temp*_input"),
                            key=lambda p: int(re.search(r"temp(\d+)_", p).group(1))):
                lab = read(t.replace("_input", "_label")).strip() or "CPU"
                lab = lab.replace("Package id ", "Package ")
                nid = f"cpu.temp.{os.path.basename(hw)}.{os.path.basename(t)}"
                self._add(nid, "cpu.temp", lab, "thermo", "C", lambda perf, t=t: _num(t, 1000))
                ids.append((not lab.startswith(("Package", "Tctl", "Tdie")), nid))
        if ids:
            self._node("cpu.temp")["primary"] = min(ids)[1] if any(not k for k, _ in ids) else ids[0][1]
        # Power (RAPL). Recent kernels make the counters root-only.
        rapl = sorted(p for p in glob.glob("/sys/class/powercap/intel-rapl:*")
                      if os.path.exists(p + "/energy_uj"))
        if rapl:
            self._add("cpu.pwr", "cpu", "Powers", "power")
            if all(os.access(p + "/energy_uj", os.R_OK) for p in rapl):
                for p in rapl:
                    name = read(p + "/name").strip()
                    self._add("cpu.pwr." + os.path.basename(p), "cpu.pwr",
                              _RAPL_NAMES.get(name, name), "power", "W",
                              lambda perf, p=p: self._rapl(p))
            else:
                self._add("cpu.pwr.denied", "cpu.pwr", "Power readings", "power", "text",
                          lambda perf: "Requires administrator",
                          tip="The kernel only lets administrators read CPU energy counters "
                              "(RAPL). Use File → Run as administrator to see them.")
        # Policy
        cf = "/sys/devices/system/cpu/cpu0/cpufreq/"
        self._add("cpu.pol", "cpu", "Power policy", "gear")
        for f, lab in (("scaling_governor", "Governor"),
                       ("energy_performance_preference", "Energy preference"),
                       ("scaling_driver", "Frequency driver")):
            if os.path.exists(cf + f):
                self._add("cpu.pol." + f, "cpu.pol", lab, "gear", "text", _text(cf + f))
        if os.path.exists("/sys/devices/system/cpu/intel_pstate/no_turbo"):
            self._add("cpu.pol.turbo", "cpu.pol", "Turbo boost", "gear", "text",
                      lambda perf: {"0": "On", "1": "Off"}.get(
                          read_fast("/sys/devices/system/cpu/intel_pstate/no_turbo", 8).strip()))
        elif os.path.exists("/sys/devices/system/cpu/cpufreq/boost"):
            self._add("cpu.pol.turbo", "cpu.pol", "Turbo boost", "gear", "text",
                      lambda perf: {"1": "On", "0": "Off"}.get(
                          read_fast("/sys/devices/system/cpu/cpufreq/boost", 8).strip()))
        if os.path.exists("/sys/firmware/acpi/platform_profile"):
            self._add("cpu.pol.profile", "cpu.pol", "Platform profile", "gear", "text",
                      _text("/sys/firmware/acpi/platform_profile"))

    def _node(self, nid):
        return next(n for n in self.nodes if n["id"] == nid)

    def _rapl(self, path):
        import time
        e = _num(path + "/energy_uj")
        now = time.monotonic()
        prev = self._energy.get(path)
        self._energy[path] = (e, now)
        if e is None or prev is None or prev[0] is None or now <= prev[1]:
            return None
        d = e - prev[0]
        if d < 0:                                    # counter wrapped
            d += _num(path + "/max_energy_range_uj") or 0
        return d / 1e6 / (now - prev[1])

    # ---- graphics ------------------------------------------------------
    def _gpus(self, perf):
        for i, g in enumerate((perf or {}).get("gpus", [])):
            nid = f"gpu{i}"
            self._add(nid, "pc", _clean_gpu(g["name"] or "Graphics card"), "gpu", expanded=True,
                      tip=g["name"])

            def val(key, i=i):
                def f(perf):
                    gs = perf and perf.get("gpus") or []
                    return gs[i].get(key) if i < len(gs) else None
                return f
            amd = glob.glob("/sys/class/drm/card*/device/hwmon/hwmon*") if g["driver"] == "amdgpu" else []
            if g.get("clock") is not None:
                self._add(nid + ".clk", nid, "Clock", "clock", "GHz",
                          lambda perf, f=val("clock"): (lambda v: v / 1000 if v else v)(f(perf)))
            for hw in amd[:1]:
                if os.path.exists(hw + "/freq1_input"):
                    self._add(nid + ".clk", nid, "Clock", "clock", "GHz",
                              lambda perf, p=hw + "/freq1_input": _num(p, 1e9))
            if g.get("util") is not None:
                self._add(nid + ".util", nid, "Utilization", "util", "%", val("util"))
            if g.get("temp") is not None:
                self._add(nid + ".temp", nid, "Temperature", "thermo", "C", val("temp"))
            if g.get("power") is not None:
                self._add(nid + ".pwr", nid, "Power", "power", "W", val("power"))
            if g.get("mem_used") is not None:
                self._add(nid + ".mem", nid, "Memory used", "memory", "B", val("mem_used"))
            for hw in amd[:1]:
                for fan in sorted(glob.glob(hw + "/fan*_input")):
                    self._add(nid + ".fan", nid, "Fan", "fan", "RPM", lambda perf, p=fan: _num(p))

    # ---- memory --------------------------------------------------------
    def _memory(self, perf):
        self._add("mem", "pc", "System Memory", "memory", primary="mem.used")
        self._add("mem.used", "mem", "Used", "memory", "B", lambda perf: perf["memory"]["used"] if perf else None)
        self._add("mem.avail", "mem", "Available", "memory", "B",
                  lambda perf: perf["memory"]["available"] if perf else None)
        self._add("mem.load", "mem", "Load", "util", "%", lambda perf: perf["memory"]["percent"] if perf else None)
        if perf and perf["memory"]["swap_total"]:
            self._add("mem.swap", "mem", "Swap used", "memory", "B",
                      lambda perf: perf["memory"]["swap_used"] if perf else None)

    # ---- drives ----------------------------------------------------------
    def _drives(self):
        n = 0
        for hw in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
            chip = read(hw + "/name").strip()
            if chip not in _DRIVE_CHIPS:
                continue
            model = read(hw + "/device/model").strip()
            kind = "NVMe SSD" if chip == "nvme" else "Drive"
            nid = f"drive{n}"
            n += 1
            self._add(nid, "pc", f"{kind}: {model}" if model else kind, "drive", expanded=True)
            for t in sorted(glob.glob(hw + "/temp*_input"),
                            key=lambda p: int(re.search(r"temp(\d+)_", p).group(1))):
                lab = read(t.replace("_input", "_label")).strip()
                lab = "Drive temperature" if lab in ("", "Composite") else lab
                self._add(f"{nid}.{os.path.basename(t)}", nid, lab, "thermo", "C",
                          lambda perf, t=t: _num(t, 1000))

    # ---- batteries -------------------------------------------------------
    def _batteries(self):
        sup = sorted(glob.glob("/sys/class/power_supply/*"))
        mains = [p for p in sup if read(p + "/type").strip() == "Mains"]
        for p in sup:
            if read(p + "/type").strip() != "Battery" or read(p + "/scope").strip() == "Device":
                continue
            name = os.path.basename(p)
            nid = "bat." + name
            self._add(nid, "pc", f"Battery {name}", "battery", expanded=True)
            if os.path.exists(p + "/capacity"):
                self._add(nid + ".cap", nid, "Charge level", "battery", "%",
                          lambda perf, p=p: _num(p + "/capacity"))

            def power(perf, p=p):
                w = _num(p + "/power_now", 1e6)
                if w is None:
                    a, v = _num(p + "/current_now", 1e6), _num(p + "/voltage_now", 1e6)
                    w = a * v if a is not None and v else None
                return w
            if os.path.exists(p + "/power_now") or os.path.exists(p + "/current_now"):
                self._add(nid + ".pwr", nid, "Power draw", "power", "W", power,
                          tip="Charge or discharge rate, as reported by the battery")
            if os.path.exists(p + "/voltage_now"):
                self._add(nid + ".volt", nid, "Voltage", "power", "V",
                          lambda perf, p=p: _num(p + "/voltage_now", 1e6))

            def health(perf, p=p):
                full = _num(p + "/energy_full") or _num(p + "/charge_full")
                design = _num(p + "/energy_full_design") or _num(p + "/charge_full_design")
                return full * 100.0 / design if full and design else None
            if health(None) is not None:
                self._add(nid + ".health", nid, "Health (capacity left)", "battery", "%", health)
            self._add(nid + ".status", nid, "Status", "battery", "text", _text(p + "/status"))
            if mains:
                self._add(nid + ".src", nid, "Power source", "plug", "text",
                          lambda perf, m=mains: "AC adapter" if any(
                              read_fast(x + "/online", 8).strip() == "1" for x in m) else "Battery")

    # ---- everything else hwmon knows about -------------------------------
    def _others(self):
        n = 0
        for hw in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
            chip = read(hw + "/name").strip()
            if (chip in _CPU_CHIPS or chip in _DRIVE_CHIPS or chip in _SKIP_CHIPS
                    or chip.startswith("BAT") or chip == "amdgpu"):
                continue
            nid = f"hw{n}"
            n += 1
            label = _friendly(chip)
            self._add(nid, "pc", label, "chip", tip=f"hwmon chip: {chip}")
            for t in sorted(glob.glob(hw + "/temp*_input")):
                lab = read(t.replace("_input", "_label")).strip() or "Temperature"
                self._add(f"{nid}.{os.path.basename(t)}", nid, lab, "thermo", "C",
                          lambda perf, t=t: _num(t, 1000))
            for f in sorted(glob.glob(hw + "/fan*_input")):
                lab = read(f.replace("_input", "_label")).strip() or \
                    "Fan " + re.search(r"fan(\d+)", f).group(1)
                self._add(f"{nid}.{os.path.basename(f)}", nid, lab, "fan", "RPM",
                          lambda perf, f=f: _num(f))
            for f in sorted(glob.glob(hw + "/power*_input")) + sorted(glob.glob(hw + "/power*_average")):
                lab = read(re.sub(r"_(input|average)$", "_label", f)).strip() or "Power"
                self._add(f"{nid}.{os.path.basename(f)}", nid, lab, "power", "W",
                          lambda perf, f=f: _num(f, 1e6))

    # ---- sampling --------------------------------------------------------
    def sample(self, perf):
        out = {}
        for n in self.nodes:
            if n["fn"] is not None:
                try:
                    out[n["id"]] = n["fn"](perf)
                except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError):
                    out[n["id"]] = None
        return out
