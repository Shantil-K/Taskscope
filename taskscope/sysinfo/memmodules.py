"""Installed memory modules (type, speed, size, slot, maker) from the
firmware's SMBIOS tables, as systemd-udevd stores them for every user in
/run/udev/data/+dmi:id. Falls back to `udevadm info`. Read once; nothing
here changes while the system runs.
"""

import functools

from .probe import read, run, which

# DDR speed grades as sold (the firmware often reports e.g. 2667 for DDR4-2666)
_GRADES = (1066, 1333, 1600, 1866, 2133, 2400, 2666, 2933, 3200, 3600, 4000, 4266, 4800,
           5200, 5600, 6000, 6400, 7200, 7500, 8000, 8533)
_BLANK = {"", "not specified", "unknown", "none", "0000", "00000000", "000000000000",
          "manufacturer0", "manufacturer1", "undefined", "<out of spec>"}


def _props():
    text = read("/run/udev/data/+dmi:id")
    out = {}
    if text:
        for line in text.splitlines():
            if line.startswith("E:") and "=" in line:
                k, _, v = line[2:].partition("=")
                out[k] = v
    if not out and which("udevadm"):
        for line in run(["udevadm", "info", "-q", "property", "-p", "/sys/devices/virtual/dmi/id"]).splitlines():
            k, _, v = line.partition("=")
            out[k] = v
    return out


def _clean(v):
    v = (v or "").strip()
    return "" if v.lower() in _BLANK or set(v) <= {"0", " "} else v


def grade(mts):
    """2667 -> 2666 (the name the module is sold under)."""
    if not mts:
        return None
    best = min(_GRADES, key=lambda g: abs(g - mts))
    return best if abs(best - mts) <= 3 else mts


@functools.lru_cache(maxsize=1)
def modules():
    """{'slots': n, 'max': bytes or None, 'modules': [ {slot, size, type, form, speed,
    configured, manufacturer, part, rank, voltage} ]} — empty lists when unknown."""
    p = _props()
    n = int(p.get("MEMORY_ARRAY_NUM_DEVICES") or 0)
    mods = []
    for i in range(max(n, 8)):
        pre = f"MEMORY_DEVICE_{i}_"
        if pre + "LOCATOR" not in p and pre + "SIZE" not in p:
            continue
        size = int(p.get(pre + "SIZE") or 0)
        if not size or p.get(pre + "PRESENT") == "0":
            continue
        speed = int(p.get(pre + "SPEED_MTS") or 0) or None
        conf = int(p.get(pre + "CONFIGURED_SPEED_MTS") or 0) or None
        volt = p.get(pre + "CONFIGURED_VOLTAGE")
        mods.append({"slot": _clean(p.get(pre + "LOCATOR")) or f"Slot {i}",
                     "size": size, "type": _clean(p.get(pre + "TYPE")),
                     "form": _clean(p.get(pre + "FORM_FACTOR")),
                     "speed": speed, "configured": conf,
                     "manufacturer": _clean(p.get(pre + "MANUFACTURER")),
                     "part": _clean(p.get(pre + "PART_NUMBER")),
                     "rank": p.get(pre + "RANK"),
                     "voltage": volt if volt and volt not in ("0", "1") else None})
    mx = p.get("MEMORY_ARRAY_MAX_CAPACITY")
    return {"slots": n or len(mods), "max": int(mx) if mx and mx.isdigit() else None, "modules": mods}


def summary():
    """One line such as '16 GB DDR4-2666 SODIMM', or '' when the firmware says nothing."""
    m = modules()["modules"]
    if not m:
        return ""
    from .. import fmt
    types = {x["type"] for x in m if x["type"]}
    forms = {x["form"] for x in m if x["form"]}
    speeds = {grade(x["configured"] or x["speed"]) for x in m if x["configured"] or x["speed"]}
    t = "/".join(sorted(types))
    sp = f"-{min(speeds)}" if speeds and t else (f" {min(speeds)} MT/s" if speeds else "")
    return " ".join(x for x in (gb(sum(x["size"] for x in m)), f"{t}{sp}", "/".join(sorted(forms))) if x)


def gb(n):
    """16 GB rather than 16.0 GB for module sizes (always whole numbers)."""
    from .. import fmt
    g = n / 2 ** 30
    return f"{g:.0f} GB" if g >= 1 and abs(g - round(g)) < 0.01 else fmt.size(n)


def rows():
    """(label, value) facts for the Performance > Memory page."""
    info = modules()
    m = info["modules"]
    if not m:
        return []
    out = []
    speeds = sorted({x["configured"] or x["speed"] for x in m if x["configured"] or x["speed"]})
    if speeds:
        out.append(("Speed", " / ".join(f"{grade(sp)} MT/s" for sp in speeds)))
    out.append(("Slots used", f"{len(m)} of {info['slots']}"))
    forms = sorted({x["form"] for x in m if x["form"]})
    if forms:
        out.append(("Form factor", "/".join(forms)))
    makers = sorted({x["manufacturer"] + (" " + x["part"] if x["part"] else "") for x in m
                     if x["manufacturer"] or x["part"]})
    if makers:
        out.append(("Module", ", ".join(makers)))
    return out
