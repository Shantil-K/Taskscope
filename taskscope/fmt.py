"""Formatting helpers shared by the GUI and the TUI."""


def size(n, suffix=""):
    if n is None:
        return ""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}{suffix}" if unit == "B" else f"{n:.1f} {unit}{suffix}"
        n /= 1024


def pair(used, total):
    """'0.38 / 4.0 GB': both numbers in the total's unit, so it stays short."""
    if total is None:
        return size(used)
    used, total = float(used or 0), float(total)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(total) < 1024 or unit == "TB":
            if unit == "B":
                return f"{used:.0f} / {total:.0f} B"
            d = 2 if used < 1 else 1
            return f"{used:.{d}f} / {total:.1f} {unit}"
        used /= 1024
        total /= 1024


def rate(n):
    return "" if n is None else size(n, "/s")


def bits(n):
    """Bytes/s -> human bits/s, as network adapters are usually rated."""
    if n is None:
        return ""
    b = n * 8
    for unit in ("bps", "Kbps", "Mbps", "Gbps"):
        if b < 1000 or unit == "Gbps":
            return f"{b:.0f} {unit}" if unit == "bps" else f"{b:.1f} {unit}"
        b /= 1000


# Display preferences, set from Settings (Taskscope > Settings > Appearance / General).
TEMP_UNIT = "C"      # "C" or "F"
CPU_SCALE = 1.0      # 1 = share of the whole CPU; number of cores = "100% = one full core"


def temp(c, decimals=0):
    """Temperature in the chosen unit."""
    if c is None:
        return ""
    if TEMP_UNIT == "F":
        return f"{c * 9 / 5 + 32:.{decimals}f} °F"
    return f"{c:.{decimals}f} °C"


def temp_short(c):
    """Compact form for meters, e.g. 87°."""
    if c is None:
        return ""
    return f"{c * 9 / 5 + 32:.0f}°F" if TEMP_UNIT == "F" else f"{c:.0f}°"


def cpu(v):
    """A process's CPU use, as % of the whole CPU or of one core (setting)."""
    return "" if v is None else f"{v * CPU_SCALE:.1f}%"


def pct(v):
    return "" if v is None else f"{v:.0f}%"


def duration(sec):
    sec = int(sec)
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m, s = divmod(sec, 60)
    return f"{d}:{h:02}:{m:02}:{s:02}" if d else f"{h}:{m:02}:{s:02}"
