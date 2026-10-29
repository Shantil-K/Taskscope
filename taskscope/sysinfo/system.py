"""Static machine facts for the System Info page (read once per page open)."""

import os
import platform

from .perf import cpu_model, hostname
from .probe import distro_name, read, read_int, session_type, which, run


def _dmi(name):
    return read(f"/sys/class/dmi/id/{name}").strip()


def _cpu_topology():
    cores = set()
    sockets = set()
    for line in read("/proc/cpuinfo").split("\n\n"):
        phys = core = None
        for l in line.splitlines():
            k, _, v = l.partition(":")
            k = k.strip()
            if k == "physical id":
                phys = v.strip()
            elif k == "core id":
                core = v.strip()
        if phys is not None:
            sockets.add(phys)
            cores.add((phys, core))
    return len(sockets) or 1, len(cores) or (os.cpu_count() or 1)


def _cache_sizes():
    out = {}
    for idx in sorted(os.listdir("/sys/devices/system/cpu/cpu0/cache")) if os.path.isdir("/sys/devices/system/cpu/cpu0/cache") else []:
        d = f"/sys/devices/system/cpu/cpu0/cache/{idx}"
        lvl, typ, size = read(d + "/level").strip(), read(d + "/type").strip(), read(d + "/size").strip()
        if lvl and size:
            key = f"L{lvl}" + ("d" if typ == "Data" else "i" if typ == "Instruction" else "")
            out[key] = size
    return ", ".join(f"{k} {v}" for k, v in out.items())


def info():
    """List of (section, [(label, value), ...])."""
    u = os.uname()
    sockets, cores = _cpu_topology()
    mem_total = 0
    for line in read("/proc/meminfo").splitlines():
        if line.startswith("MemTotal:"):
            mem_total = int(line.split()[1]) * 1024
    from .. import fmt
    gov = read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor").strip()
    maxf = read_int("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")
    virt = run(["systemd-detect-virt"]).strip() if which("systemd-detect-virt") else ""
    gpus = []
    if which("lspci"):
        gpus = [l.split(": ", 1)[-1] for l in run(["lspci"]).splitlines()
                if " VGA " in l or "3D controller" in l or "Display controller" in l]
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "") or os.environ.get("DESKTOP_SESSION", "")
    return [
        ("System", [
            ("Computer name", hostname()),
            ("Operating system", distro_name()),
            ("Kernel", f"{u.sysname} {u.release}"),
            ("Architecture", u.machine),
            ("Desktop", f"{desktop or 'none'} ({session_type()})"),
            ("Virtualization", virt if virt and virt != "none" else "none (bare metal)"),
            ("Python", platform.python_version()),
        ]),
        ("Hardware", [
            ("Manufacturer", _dmi("sys_vendor") or "-"),
            ("Model", (_dmi("product_name") + " " + _dmi("product_version")).strip() or "-"),
            ("Motherboard", (_dmi("board_vendor") + " " + _dmi("board_name")).strip() or "-"),
            ("BIOS", (_dmi("bios_vendor") + " " + _dmi("bios_version") + " " + _dmi("bios_date")).strip() or "-"),
        ]),
        ("Processor", [
            ("Name", cpu_model()),
            ("Sockets / cores / threads", f"{sockets} / {cores} / {os.cpu_count()}"),
            ("Max speed", f"{maxf / 1e6:.2f} GHz" if maxf else "-"),
            ("Cache", _cache_sizes() or "-"),
            ("Governor", gov or "-"),
        ]),
        ("Memory and graphics", [
            ("Installed memory", fmt.size(mem_total)),
            ("Graphics", "; ".join(gpus) or "-"),
        ]),
    ]


# ---------------------------------------------------------------- sections
# Each section function returns a list of rows: (label, value) for a plain
# row, or (label, [rows]) for a group that can be folded. They read /proc and
# /sys only (cheap), so the page can refresh the visible section every tick.
# Things that need a program to run are cached by slow_facts().

import functools
import time

_slow = {}


def slow_facts():
    """Facts that cost a subprocess: computed once, in a worker thread."""
    if _slow:
        return _slow
    virt = run(["systemd-detect-virt"]).strip() if which("systemd-detect-virt") else ""
    _slow["virt"] = "No" if not virt or virt == "none" else f"Yes ({virt})"
    names = {}
    if which("lspci"):
        for line in run(["lspci", "-D"]).splitlines():
            if " VGA " in line or "3D controller" in line or "Display controller" in line:
                addr, _, rest = line.partition(" ")
                desc = rest.split(": ", 1)[-1]
                names[addr.lower()] = desc
    _slow["gpu_names"] = names
    try:
        from . import services
        mgrs = services.detect()
        rows = mgrs[0].list() if mgrs else []
        running = sum(1 for r in rows if r.get("state") in ("active", "running", "started", "up"))
        failed = sum(1 for r in rows if r.get("state") == "failed")
        _slow["services"] = f"{len(rows)} ({running} running, {failed} failed)" if rows else "-"
    except Exception:
        _slow["services"] = "-"
    try:
        from . import startup
        _slow["startup"] = str(len(startup.list_entries()))
    except Exception:
        _slow["startup"] = "-"
    return _slow


def _meminfo():
    out = {}
    for line in read("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        p = v.split()
        if p and p[0].isdigit():
            out[k] = int(p[0]) * (1024 if len(p) > 1 else 1)
    return out


def _cpu_flags():
    for line in read("/proc/cpuinfo").splitlines():
        if line.startswith("flags"):
            return set(line.split(":", 1)[1].split())
    return set()


def _caches():
    out = {}
    base = "/sys/devices/system/cpu/cpu0/cache"
    if os.path.isdir(base):
        for idx in sorted(os.listdir(base)):
            d = f"{base}/{idx}"
            lvl, typ, size = read(d + "/level").strip(), read(d + "/type").strip(), read(d + "/size").strip()
            if not lvl or not size:
                continue
            k = int(size.rstrip("K")) * 1024 if size.endswith("K") else \
                int(size.rstrip("M")) * 1024 * 1024 if size.endswith("M") else 0
            # per-core caches are counted once per core, like most tools report them
            shared = read(d + "/shared_cpu_list").strip()
            out.setdefault(lvl, []).append((k, typ, shared))
    return out


def _cache_total(entries, cores):
    total = 0
    seen = set()
    for size, typ, shared in entries:
        total += size
    # L1/L2 are per core: multiply by the number of physical cores
    return total


def _fmt():
    from .. import fmt
    return fmt


def _gpu_name(g):
    from .sensors import _clean_gpu
    return _clean_gpu(g.get("name") or "Graphics card")


def overview(perf):
    f = _fmt()
    u = os.uname()
    sockets, cores = _cpu_topology()
    mi = _meminfo()
    gpus = (perf or {}).get("gpus") or []
    main_gpu = next((g for g in gpus if g.get("mem_total")), gpus[0] if gpus else None)
    up = float(read("/proc/uptime", "0").split()[0] or 0)
    product = " ".join(x for x in (_dmi("sys_vendor"), _dmi("product_name")) if x)
    return [("Computer", product or "-"),
            ("Processor", cpu_model()),
            ("Logical processors", os.cpu_count()),
            ("Physical cores", cores),
            ("Sockets", sockets),
            ("Memory", f.size(mi.get("MemTotal", 0))),
            ("Graphics", _gpu_name(main_gpu) if main_gpu else "-"),
            ("Distribution", distro_name()),
            ("Kernel", u.release),
            ("Virtual machine", slow_facts().get("virt", "…") if _slow else "…"),
            ("Time since boot", f.duration(up))]


def processor():
    f = _fmt()
    sockets, cores = _cpu_topology()
    cf = "/sys/devices/system/cpu/cpu0/cpufreq/"
    base = read_int(cf + "base_frequency")
    maxf = read_int(cf + "cpuinfo_max_freq")
    cur = read_int(cf + "scaling_cur_freq")
    flags = _cpu_flags()
    rows = [("Model", cpu_model()), ("Architecture", os.uname().machine),
            ("Logical processors", os.cpu_count()), ("Physical cores", cores), ("Sockets", sockets)]
    if base:
        rows.append(("Base frequency", f"{base / 1e6:.2f} GHz"))
    if maxf:
        rows.append(("Maximum frequency", f"{maxf / 1e6:.2f} GHz"))
    if cur:
        rows.append(("Current frequency", f"{cur / 1000:.0f} MHz"))
    caches = _caches()
    for lvl in sorted(caches):
        per = sum(s for s, _t, _sh in caches[lvl])
        n = cores if lvl in ("1", "2") else sockets
        rows.append((f"L{lvl} cache", f.size(per * n)))
    for key, label in (("scaling_driver", "Frequency driver"), ("scaling_governor", "Frequency governor"),
                       ("energy_performance_preference", "Power preference")):
        v = read(cf + key).strip()
        if v:
            rows.append((label, v))
    if os.path.exists("/sys/devices/system/cpu/intel_pstate/no_turbo"):
        rows.append(("Turbo boost", "Off" if read("/sys/devices/system/cpu/intel_pstate/no_turbo").strip() == "1"
                     else "On"))
    virt = "vmx" in flags or "svm" in flags
    rows.append(("Virtualization", "Hardware virtualization supported" if virt else "Not available"))
    mc = ""
    for line in read("/proc/cpuinfo").splitlines():
        if line.startswith("microcode"):
            mc = line.split(":", 1)[1].strip()
            break
    if mc:
        rows.append(("Microcode", mc))
    rows.append(("Virtual machine", slow_facts().get("virt", "…") if _slow else "…"))
    return rows


def memory():
    from . import memmodules
    f = _fmt()
    mi = _meminfo()
    total, avail = mi.get("MemTotal", 0), mi.get("MemAvailable", 0)
    info = memmodules.modules()
    mods = []
    if info["modules"]:
        mods.append(("Installed", memmodules.summary()))
        mods.append(("Slots used", f"{len(info['modules'])} of {info['slots']}"))
        if info["max"]:
            mods.append(("Maximum supported", memmodules.gb(info["max"])))
    rows = mods + [("Usable memory", f.size(total)), ("In use", f.size(total - avail)),
            ("Available", f.size(avail)),
            ("Cached", f.size(mi.get("Cached", 0) + mi.get("SReclaimable", 0))),
            ("Buffers", f.size(mi.get("Buffers", 0))), ("Shared", f.size(mi.get("Shmem", 0))),
            ("Kernel slab", f.size(mi.get("Slab", 0))),
            ("Swap in use", f.size(mi.get("SwapTotal", 0) - mi.get("SwapFree", 0))),
            ("Swap total", f.size(mi.get("SwapTotal", 0))),
            ("Committed", f.size(mi.get("Committed_AS", 0))), ("Commit limit", f.size(mi.get("CommitLimit", 0)))]
    psi = read("/proc/pressure/memory").splitlines()
    if psi:
        some = dict(kv.split("=") for kv in psi[0].split()[1:] if "=" in kv)
        rows.append(("Stall (PSI some, 10s)", f"{float(some.get('avg10', 0)):.1f}%"))
    vm = {}
    for line in read("/proc/vmstat").splitlines():
        k, _, v = line.partition(" ")
        if k in ("pswpin", "pswpout", "pgmajfault"):
            vm[k] = int(v)
    for x in info["modules"]:
        sp = x["configured"] or x["speed"]
        mrow = [("Size", memmodules.gb(x["size"])), ("Type", x["type"] or "-"),
                ("Speed", f"{memmodules.grade(sp)} MT/s" + (f" (reported {sp})" if sp and memmodules.grade(sp) != sp else "")
                 if sp else "-"),
                ("Form factor", x["form"] or "-")]
        if x["rank"]:
            mrow.append(("Ranks", x["rank"]))
        mrow.append(("Manufacturer", x["manufacturer"] or "not reported by the firmware"))
        if x["part"]:
            mrow.append(("Part number", x["part"]))
        if x["voltage"]:
            mrow.append(("Voltage", f"{int(x['voltage']) / 1000:.2f} V" if x["voltage"].isdigit() else x["voltage"]))
        rows.append((f"Module in {x['slot']}", mrow))
    rows += [("Pages swapped in", f"{vm.get('pswpin', 0):,}"), ("Pages swapped out", f"{vm.get('pswpout', 0):,}"),
             ("Major page faults", f"{vm.get('pgmajfault', 0):,}")]
    return rows


def _pcie(dev):
    sp, w = read(dev + "/current_link_speed").strip(), read(dev + "/current_link_width").strip()
    msp, mw = read(dev + "/max_link_speed").strip(), read(dev + "/max_link_width").strip()
    if not sp or sp.startswith("Unknown") or w in ("", "0"):
        return ""            # integrated GPUs report no real PCIe link
    s = f"{sp} x{w}"
    if msp and (msp != sp or mw != w):
        s += f" (max {msp} x{mw})"
    return s


def graphics(perf):
    f = _fmt()
    out = []
    names = (slow_facts() if _slow else {}).get("gpu_names", {})
    for g in (perf or {}).get("gpus") or []:
        rows = []
        pci = g.get("pci") or ""
        dev = None
        if g["id"].startswith("/sys/class/drm/"):
            dev = os.path.realpath(g["id"] + "/device")
            pci = pci or os.path.basename(dev)
        elif pci:
            dev = f"/sys/bus/pci/devices/{pci}" if os.path.exists(f"/sys/bus/pci/devices/{pci}") else \
                f"/sys/bus/pci/devices/0000:{pci}"
        desc = names.get(pci.lower()) or g.get("name")
        if desc and desc != _gpu_name(g):
            rows.append(("Description", desc))
        rows.append(("Driver", (g.get("driver") or "-") + (f" {g['driver_version']}" if g.get("driver_version") else "")))
        if pci:
            rows.append(("Bus", pci))
        if dev and os.path.isdir(dev):
            link = _pcie(dev)
            if link:
                rows.append(("Link", link))
        if g.get("mem_total"):
            rows.append(("Dedicated memory", f.size(g["mem_total"])))
            rows.append(("Memory in use", f.size(g.get("mem_used") or 0)))
        for key, label, unit in (("clock", "Clock", "MHz"), ("max_clock", "Maximum clock", "MHz"),
                                 ("mem_clock", "Memory clock", "MHz"), ("max_mem_clock", "Maximum memory clock", "MHz")):
            if g.get(key):
                rows.append((label, f"{g[key]:.0f} {unit}"))
        if g.get("power"):
            rows.append(("Power draw", f"{g['power']:.2f} W"))
        if g.get("power_limit"):
            rows.append(("Power limit", f"{g['power_limit']:.0f} W"))
        if g.get("temp"):
            rows.append(("Temperature", _fmt().temp(g["temp"])))
        if g.get("util") is not None:
            rows.append(("Utilization", f"{g['util']:.1f}%" + (" (estimated)" if g["backend"] == "intel" else "")))
        for key, label in (("encoder", "Video encode"), ("decoder", "Video decode")):
            if g.get(key) is not None:
                rows.append((label, f"{g[key]:.1f}%"))
        if g.get("fan") is not None:
            rows.append(("Fan", f"{g['fan']}%"))
        out.append((_gpu_name(g), rows))
    return out or [("Graphics", "No graphics devices found")]


def storage(perf):
    f = _fmt()
    root_dev = ""
    try:
        st = os.stat("/")
        root_dev = os.path.basename(os.path.realpath(f"/sys/dev/block/{os.major(st.st_dev)}:{os.minor(st.st_dev)}"))
    except OSError:
        pass
    out = []
    for d in (perf or {}).get("disks") or []:
        name = d["name"]
        q = f"/sys/block/{name}/queue/"
        rot = read(q + "rotational").strip() == "1"
        sched = read(q + "scheduler").strip()
        if "[" in sched:
            sched = sched[sched.index("[") + 1:sched.index("]")]
        parts = read(f"/proc/diskstats")
        rd = wr = ios = ticks = 0
        for line in parts.splitlines():
            p = line.split()
            if len(p) > 13 and p[2] == name:
                rd, wr = int(p[5]) * 512, int(p[9]) * 512
                ios, ticks = int(p[3]) + int(p[7]), int(p[6]) + int(p[10])
        system = root_dev.startswith(name)
        rows = [("Description", d.get("model") or "-"), ("Type", d.get("kind") or ("HDD" if rot else "SSD")),
                ("Capacity", f.size(d.get("size") or 0)), ("Rotational", "Yes" if rot else "No"),
                ("System disk", "Yes" if system else "No"),
                ("Logical block size", f"{read(q + 'logical_block_size').strip() or '?'} B"),
                ("Scheduler", sched or "-"), ("Read since boot", f.size(rd)), ("Written since boot", f.size(wr)),
                ("Average response time", f"{ticks / ios:.2f} ms" if ios else "-"),
                ("Active time", f"{d.get('busy', 0):.1f}%"), ("Read rate", f.rate(d.get("read_rate", 0))),
                ("Write rate", f.rate(d.get("write_rate", 0))),
                ("Mounted at", ", ".join(d.get("mounts") or []) or "-")]
        out.append((name, rows))
    return out or [("Storage", "No disks found")]


def power():
    rows = []
    sup = "/sys/class/power_supply"
    try:
        names = sorted(os.listdir(sup))
    except OSError:
        names = []
    mains = [n for n in names if read(f"{sup}/{n}/type").strip() == "Mains"]
    if mains:
        on = any(read(f"{sup}/{n}/online").strip() == "1" for n in mains)
        rows.append(("Power source", "AC power" if on else "Battery"))
    for n in names:
        b = f"{sup}/{n}"
        if read(b + "/type").strip() != "Battery" or read(b + "/scope").strip() == "Device":
            continue
        brow = []
        cap = read(b + "/capacity").strip()
        if cap:
            brow.append(("Charge", f"{cap}%"))
        st = read(b + "/status").strip()
        if st:
            brow.append(("Status", st))
        full = read_int(b + "/energy_full") or read_int(b + "/charge_full")
        design = read_int(b + "/energy_full_design") or read_int(b + "/charge_full_design")
        if full and design:
            brow.append(("Health (capacity left)", f"{full * 100 / design:.0f}%"))
        cyc = read(b + "/cycle_count").strip()
        if cyc:
            brow.append(("Cycle count", cyc))
        lvl = read(b + "/capacity_level").strip()
        if lvl:
            brow.append(("Condition", lvl))
        for key, label in (("technology", "Technology"), ("manufacturer", "Manufacturer"),
                           ("model_name", "Model")):
            v = read(b + "/" + key).strip()
            if v:
                brow.append((label, v))
        lo, hi = read(b + "/charge_control_start_threshold").strip(), read(b + "/charge_control_end_threshold").strip()
        if hi:
            brow.append(("Charge limit", f"{lo or '?'}% – {hi}%"))
        rows.append((f"Battery {n}", brow))
    prof = read("/sys/firmware/acpi/platform_profile").strip()
    if prof:
        rows.append(("Platform profile", prof))
    return rows or [("Power", "No power information exposed")]


def network(perf):
    f = _fmt()
    out = []
    for n in (perf or {}).get("net") or []:
        name = n["name"]
        mtu = read(f"/sys/class/net/{name}/mtu").strip()
        rows = [("Type", n.get("kind") or "-"), ("State", "up" if n.get("up") else "down"),
                ("Hardware address", n.get("mac") or "-"), ("MTU", mtu or "-"),
                ("Link speed", f"{n['speed_mbps']} Mbps" if n.get("speed_mbps") else "-"),
                ("Addresses", ", ".join(n.get("addrs") or []) or "-"),
                ("Total received", f.size(n.get("rx_total") or 0)), ("Total sent", f.size(n.get("tx_total") or 0)),
                ("Receive rate", f.bits(n.get("rx_rate") or 0)), ("Send rate", f.bits(n.get("tx_rate") or 0))]
        drv = os.path.realpath(f"/sys/class/net/{name}/device/driver") if os.path.exists(f"/sys/class/net/{name}/device/driver") else ""
        if drv:
            rows.insert(1, ("Driver", os.path.basename(drv)))
        out.append((name, rows))
    return out or [("Network", "No network adapters found")]


def operating_system(nprocs=None, nthreads=None):
    from .probe import os_release
    f = _fmt()
    r = os_release()
    u = os.uname()
    up = float(read("/proc/uptime", "0").split()[0] or 0)
    la = read("/proc/loadavg").split()
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "") or os.environ.get("DESKTOP_SESSION", "")
    init = os.path.basename(os.path.realpath("/sbin/init")) if os.path.exists("/sbin/init") else "-"
    slow = slow_facts() if _slow else {}
    rows = [("Distribution", distro_name())]
    if r.get("VERSION"):
        rows.append(("Version", r["VERSION"]))
    if r.get("VERSION_CODENAME"):
        rows.append(("Codename", r["VERSION_CODENAME"]))
    if r.get("ID_LIKE"):
        rows.append(("Based on", r["ID_LIKE"]))
    rows += [("Kernel release", u.release), ("Architecture", u.machine), ("Computer name", hostname()),
             ("Desktop", f"{desktop or 'none'} ({session_type()})"), ("Init system", init),
             ("Time since boot", f.duration(up)),
             ("Booted at", time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() - up)))]
    if nprocs is not None:
        rows.append(("Processes", f"{nprocs:,}"))
    if nthreads is not None:
        rows.append(("Threads", f"{nthreads:,}"))
    if len(la) >= 4:
        rows += [("Load average (1 min)", la[0]), ("Load average (5 min)", la[1]),
                 ("Load average (15 min)", la[2])]
        run_, total = la[3].split("/")
        rows += [("Runnable tasks", run_), ("Total tasks", total)]
    try:
        from .users import sessions
        rows.append(("User sessions", len(sessions())))
    except Exception:
        pass
    rows += [("Services", slow.get("services", "…")), ("Startup items", slow.get("startup", "…")),
             ("Python", platform.python_version())]
    if r.get("HOME_URL"):
        rows.append(("Website", r["HOME_URL"]))
    return rows


def firmware():
    """Computer, board and BIOS (DMI)."""
    rows = []
    for label, keys in (("Manufacturer", ("sys_vendor",)), ("Model", ("product_name", "product_version")),
                        ("Motherboard", ("board_vendor", "board_name")),
                        ("BIOS", ("bios_vendor", "bios_version", "bios_date"))):
        v = " ".join(x for x in (_dmi(k) for k in keys) if x and x.lower() not in ("default string", "to be filled by o.e.m."))
        rows.append((label, v or "-"))
    boot = "UEFI" if os.path.isdir("/sys/firmware/efi") else "Legacy BIOS"
    rows.append(("Boot mode", boot))
    sb = ""
    for p in os.listdir("/sys/firmware/efi/efivars") if os.path.isdir("/sys/firmware/efi/efivars") else []:
        if p.startswith("SecureBoot-"):
            data = open(f"/sys/firmware/efi/efivars/{p}", "rb").read() if os.access(f"/sys/firmware/efi/efivars/{p}", os.R_OK) else b""
            sb = "On" if data[-1:] == b"\x01" else "Off" if data else ""
    if sb:
        rows.append(("Secure Boot", sb))
    return rows
