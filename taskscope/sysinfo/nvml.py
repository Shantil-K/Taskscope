"""Minimal NVIDIA NVML binding via ctypes.

Reading the NVIDIA driver's library in-process costs microseconds per sample,
whereas spawning nvidia-smi every tick costs tens of milliseconds of CPU.
Returns None from init() when libnvidia-ml is absent, so callers can fall back.
"""

import ctypes


class _Util(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class _Mem(ctypes.Structure):
    _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong),
                ("used", ctypes.c_ulonglong)]


class _Pci(ctypes.Structure):
    _fields_ = [("busIdLegacy", ctypes.c_char * 16), ("domain", ctypes.c_uint),
                ("bus", ctypes.c_uint), ("device", ctypes.c_uint),
                ("pciDeviceId", ctypes.c_uint), ("pciSubSystemId", ctypes.c_uint),
                ("busId", ctypes.c_char * 32)]


class NVML:
    def __init__(self, lib):
        self.lib = lib
        self.handles = []
        n = ctypes.c_uint()
        if lib.nvmlDeviceGetCount_v2(ctypes.byref(n)) != 0:
            return
        for i in range(n.value):
            h = ctypes.c_void_p()
            if lib.nvmlDeviceGetHandleByIndex_v2(i, ctypes.byref(h)) == 0:
                self.handles.append(h)

    def name(self, h):
        buf = ctypes.create_string_buffer(96)
        self.lib.nvmlDeviceGetName(h, buf, 96)
        return buf.value.decode(errors="replace")

    def pci(self, h):
        p = _Pci()
        if self.lib.nvmlDeviceGetPciInfo_v3(h, ctypes.byref(p)) == 0:
            bid = p.busId.decode().lower()
            return bid[4:] if len(bid) > 12 else bid
        return ""

    def sample(self, h):
        d = {"util": None, "mem_used": None, "mem_total": None, "temp": None, "power": None}
        u = _Util()
        if self.lib.nvmlDeviceGetUtilizationRates(h, ctypes.byref(u)) == 0:
            d["util"] = float(u.gpu)
        m = _Mem()
        if self.lib.nvmlDeviceGetMemoryInfo(h, ctypes.byref(m)) == 0:
            d["mem_used"], d["mem_total"] = m.used, m.total
        t = ctypes.c_uint()
        if self.lib.nvmlDeviceGetTemperature(h, 0, ctypes.byref(t)) == 0:
            d["temp"] = float(t.value)
        # Power is a slow driver call (~5 ms): refresh it every 2 s.
        d["power"] = self._every(h, "power", 2.0, self._power)
        clk = ctypes.c_uint()
        if self.lib.nvmlDeviceGetClockInfo(h, 0, ctypes.byref(clk)) == 0:   # 0 = graphics clock
            d["clock"] = float(clk.value)
        # Details only shown on the Performance page: refresh every 5 s
        # (each costs about a millisecond in the driver).
        d.update(self._every(h, "extra", 5.0, self._extra))
        d.update(self.static(h))
        return d

    def _every(self, h, name, ttl, fn):
        import time
        cache = self.__dict__.setdefault("_ttl", {})
        key = (h.value, name)
        hit = cache.get(key)
        now = time.monotonic()
        if hit is None or now - hit[0] >= ttl:
            hit = cache[key] = (now, fn(h))
        return hit[1]

    def _power(self, h):
        pw = ctypes.c_uint()
        return pw.value / 1000.0 if self.lib.nvmlDeviceGetPowerUsage(h, ctypes.byref(pw)) == 0 else None

    def _extra(self, h):
        d = {"mem_clock": self._uint("nvmlDeviceGetClockInfo", h, 2),          # 2 = memory clock
             "fan": self._uint("nvmlDeviceGetFanSpeed", h),
             "pcie_gen": self._uint("nvmlDeviceGetCurrPcieLinkGeneration", h),
             "pcie_width": self._uint("nvmlDeviceGetCurrPcieLinkWidth", h)}
        for key, fn in (("encoder", "nvmlDeviceGetEncoderUtilization"),
                        ("decoder", "nvmlDeviceGetDecoderUtilization")):
            u, period = ctypes.c_uint(), ctypes.c_uint()
            f = getattr(self.lib, fn, None)
            d[key] = float(u.value) if f and f(h, ctypes.byref(u), ctypes.byref(period)) == 0 else None
        return d

    def _uint(self, fn, h, *args):
        f = getattr(self.lib, fn, None)
        v = ctypes.c_uint()
        try:
            return v.value if f and f(h, *args, ctypes.byref(v)) == 0 else None
        except (OSError, ctypes.ArgumentError):
            return None

    def static(self, h):
        """Values that never change while running; read once per GPU."""
        key = h.value
        cache = self.__dict__.setdefault("_static", {})
        if key not in cache:
            st = {"max_clock": self._uint("nvmlDeviceGetMaxClockInfo", h, 0),
                  "max_mem_clock": self._uint("nvmlDeviceGetMaxClockInfo", h, 2),
                  "max_pcie_gen": self._uint("nvmlDeviceGetMaxPcieLinkGeneration", h),
                  "max_pcie_width": self._uint("nvmlDeviceGetMaxPcieLinkWidth", h)}
            lim = (self._uint("nvmlDeviceGetEnforcedPowerLimit", h)
                   or self._uint("nvmlDeviceGetPowerManagementLimit", h))
            st["power_limit"] = lim / 1000.0 if lim else None
            buf = ctypes.create_string_buffer(96)
            f = getattr(self.lib, "nvmlSystemGetDriverVersion", None)
            st["driver_version"] = buf.value.decode() if f and f(buf, 96) == 0 else None
            cache[key] = st
        return cache[key]


def init():
    for name in ("libnvidia-ml.so.1", "libnvidia-ml.so"):
        try:
            lib = ctypes.CDLL(name)
        except OSError:
            continue
        try:
            init_fn = getattr(lib, "nvmlInit_v2", None) or lib.nvmlInit
            if init_fn() != 0:
                return None
            return NVML(lib)
        except (AttributeError, OSError):
            return None
    return None
