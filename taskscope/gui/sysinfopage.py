"""System Info page: a list of sections on the left (Hardware, Network,
Software, Diagnostics) and the chosen section as a Property / Value tree on
the right, with the distribution's own logo.

Only the section on screen is read, once a second, and every section is
built from /proc and /sys, so keeping this page open costs next to nothing.
"""

import glob
import os
import time

from ..sysinfo import elevate, packages, probe, services, system
from . import theme
from .qt import E, QtCore, QtGui, QtWidgets, Qt, version_string
from .widgets import enlarge, run_async

HEADER = E(Qt, "ItemDataRole", "UserRole")


# ------------------------------------------------------------------- logo

def _logo_file():
    """The distribution's logo as installed on this system (os-release LOGO,
    then the usual icon names), or None."""
    r = probe.os_release()
    did = r.get("ID", "")
    names = [r.get("LOGO", "")] + [f"{did}-logo", f"distributor-logo-{did}", f"{did}_logo", did]
    names += [f"{x}-logo" for x in r.get("ID_LIKE", "").split()]
    names += ["distributor-logo"]
    svg_ok = b"svg" in [bytes(x) for x in QtGui.QImageReader.supportedImageFormats()]
    exts = ([".svg"] if svg_ok else []) + [".png", ".xpm"]
    roots = [os.path.expanduser("~/.local/share/icons/hicolor"), "/usr/share/icons/hicolor",
             "/usr/local/share/icons/hicolor"]
    sizes = (["scalable"] if svg_ok else []) + ["256x256", "192x192", "128x128", "96x96", "64x64", "48x48"]
    for n in (x for x in names if x):
        icon = QtGui.QIcon.fromTheme(n)
        if not icon.isNull():
            return icon
        for root in roots:
            for sz in sizes:
                for ext in exts:
                    p = f"{root}/{sz}/apps/{n}{ext}"
                    if os.path.exists(p):
                        return p
        for ext in exts:
            p = f"/usr/share/pixmaps/{n}{ext}"
            if os.path.exists(p):
                return p
    return None


_ANSI = {"30": "#555555", "31": "#d64545", "32": "#5fa83f", "33": "#d9a400", "34": "#3b7dd8",
         "35": "#a35bd6", "36": "#2aa8b8", "37": "#bbbbbb"}


def distro_pixmap(size):
    """Logo at *size* px; a coloured letter badge when the distro ships none."""
    src = _logo_file()
    dpr = QtWidgets.QApplication.instance().devicePixelRatio() if QtWidgets.QApplication.instance() else 1
    if isinstance(src, QtGui.QIcon):
        pm = src.pixmap(int(size * dpr), int(size * dpr))
        if not pm.isNull():
            pm.setDevicePixelRatio(dpr)
            return pm
    elif src:
        if src.endswith(".svg"):
            pm = QtGui.QIcon(src).pixmap(int(size * dpr), int(size * dpr))
        else:
            pm = QtGui.QPixmap(src)
            if not pm.isNull():
                pm = pm.scaled(int(size * dpr), int(size * dpr), E(Qt, "AspectRatioMode", "KeepAspectRatio"),
                               E(Qt, "TransformationMode", "SmoothTransformation"))
        if not pm.isNull():
            pm.setDevicePixelRatio(dpr)
            return pm
    # fallback: initial on a circle in the distro's own colour (os-release ANSI_COLOR)
    r = probe.os_release()
    code = (r.get("ANSI_COLOR", "").split(";") or [""])[-1]
    col = QtGui.QColor(_ANSI.get(code[-2:] if len(code) >= 2 else "", theme.current["accent"]))
    pm = QtGui.QPixmap(int(size * dpr), int(size * dpr))
    pm.fill(QtGui.QColor(0, 0, 0, 0))
    pm.setDevicePixelRatio(dpr)
    p = QtGui.QPainter(pm)
    p.setRenderHint(E(QtGui.QPainter, "RenderHint", "Antialiasing"))
    p.setPen(E(Qt, "PenStyle", "NoPen"))
    p.setBrush(col)
    p.drawEllipse(QtCore.QRectF(1, 1, size - 2, size - 2))
    f = QtGui.QFont()
    f.setBold(True)
    f.setPixelSize(int(size * 0.5))
    p.setFont(f)
    p.setPen(QtGui.QColor("#ffffff"))
    p.drawText(QtCore.QRectF(0, 0, size, size), E(Qt, "AlignmentFlag", "AlignCenter"),
               (r.get("NAME") or "Linux")[:1].upper())
    p.end()
    return pm


# --------------------------------------------------------------- page

class SystemInfoPage(QtWidgets.QWidget):
    title, icon, live = "System Info", "info", True

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.sections = [
            ("Hardware", [("Hardware Overview", self._overview), ("Processor", lambda s: system.processor()),
                          ("Memory", lambda s: system.memory()), ("Graphics", self._graphics),
                          ("Storage", self._storage), ("Power", lambda s: system.power()),
                          ("Firmware", lambda s: system.firmware())]),
            ("Network", [("Network", self._network)]),
            ("Software", [("Operating System", self._os)]),
            ("Diagnostics", [("Data Sources", self._sources), ("Capabilities", self._capabilities),
                             ("Snapshot", self._snapshot)]),
        ]
        self.funcs = {name: fn for _c, items in self.sections for name, fn in items}
        self.current = None
        self._groups = []
        self._static_cache = {}

        self.toolbar = QtWidgets.QWidget()
        tl = QtWidgets.QHBoxLayout(self.toolbar)
        tl.setContentsMargins(0, 0, 0, 0)
        b = QtWidgets.QPushButton("Copy section")
        b.clicked.connect(lambda: self.copy(False))
        tl.addWidget(b)
        b = QtWidgets.QPushButton("Copy all")
        b.setToolTip("Copy every section as text, e.g. to paste into a bug report")
        b.clicked.connect(lambda: self.copy(True))
        tl.addWidget(b)

        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(10)
        # ---- left column: distro card + section list
        left = QtWidgets.QVBoxLayout()
        left.setSpacing(8)
        card = QtWidgets.QFrame()
        card.setObjectName("Card")
        cl = QtWidgets.QHBoxLayout(card)
        cl.setContentsMargins(10, 10, 10, 10)
        self.logo_small = QtWidgets.QLabel()
        cl.addWidget(self.logo_small)
        ct = QtWidgets.QVBoxLayout()
        ct.setSpacing(0)
        self.distro = QtWidgets.QLabel(probe.distro_name())
        f = self.distro.font()
        f.setBold(True)
        self.distro.setFont(f)
        self.distro.setWordWrap(True)
        self.kernel = QtWidgets.QLabel(f"Linux {os.uname().release}")
        self.kernel.setProperty("muted", True)
        self.kernel.setWordWrap(True)
        ct.addWidget(self.distro)
        ct.addWidget(self.kernel)
        cl.addLayout(ct, 1)
        left.addWidget(card)
        self.nav = QtWidgets.QListWidget()
        self.nav.setObjectName("SectionList")
        bold = QtGui.QFont(self.nav.font())
        bold.setBold(True)
        for cat, items in self.sections:
            h = QtWidgets.QListWidgetItem(cat)
            h.setFont(bold)
            h.setFlags(E(Qt, "ItemFlag", "NoItemFlags"))
            h.setData(HEADER, True)
            self.nav.addItem(h)
            for name, _fn in items:
                it = QtWidgets.QListWidgetItem("   " + name)
                it.setData(HEADER, False)
                it.setData(E(Qt, "ItemDataRole", "UserRole") + 1, name)
                self.nav.addItem(it)
        self.nav.setStyleSheet("QListWidget#SectionList::item { padding: 5px 6px; }")
        self.nav.setSpacing(0)
        self.nav.currentItemChanged.connect(self._pick)
        left.addWidget(self.nav, 1)
        holder = QtWidgets.QWidget()
        holder.setLayout(left)
        holder.setFixedWidth(250)
        lay.addWidget(holder)

        # ---- right column: title, optional logo banner, the tree
        right = QtWidgets.QVBoxLayout()
        right.setSpacing(6)
        self.head = QtWidgets.QLabel()
        f = self.head.font()
        f.setPointSizeF(f.pointSizeF() * 1.6)
        f.setBold(True)
        self.head.setFont(f)
        right.addWidget(self.head)
        self.banner = QtWidgets.QFrame()
        self.banner.setObjectName("Card")
        bl = QtWidgets.QHBoxLayout(self.banner)
        bl.setContentsMargins(16, 12, 16, 12)
        bl.setSpacing(16)
        self.logo_big = QtWidgets.QLabel()
        bl.addWidget(self.logo_big)
        bt = QtWidgets.QVBoxLayout()
        bt.setSpacing(2)
        self.b_name = QtWidgets.QLabel(probe.distro_name())
        f = self.b_name.font()
        f.setPointSizeF(f.pointSizeF() * 1.5)
        f.setBold(True)
        self.b_name.setFont(f)
        self.b_sub = QtWidgets.QLabel()
        self.b_sub.setProperty("muted", True)
        self.b_sub.setWordWrap(True)
        bt.addStretch(1)
        bt.addWidget(self.b_name)
        bt.addWidget(self.b_sub)
        bt.addStretch(1)
        bl.addLayout(bt, 1)
        right.addWidget(self.banner)
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["Property", "Value"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setIndentation(18)
        self.tree.header().setHighlightSections(False)
        self.tree.setColumnWidth(0, 280)
        self.tree.setContextMenuPolicy(E(Qt, "ContextMenuPolicy", "CustomContextMenu"))
        self.tree.customContextMenuRequested.connect(self._menu)
        enlarge(self.tree, 10.5, 24)
        right.addWidget(self.tree, 1)
        lay.addLayout(right, 1)

        self._set_logos()
        r = probe.os_release()
        bits = [r.get("VERSION") or "", "based on " + r["ID_LIKE"] if r.get("ID_LIKE") else "",
                f"Linux {os.uname().release}", os.uname().machine]
        self.b_sub.setText("  ·  ".join(b for b in bits if b))
        # open on the overview
        for i in range(self.nav.count()):
            if not self.nav.item(i).data(HEADER):
                self.nav.setCurrentRow(i)
                break

    def _set_logos(self):
        self.logo_small.setPixmap(distro_pixmap(40))
        self.logo_big.setPixmap(distro_pixmap(84))

    # ---- sections -------------------------------------------------------------------
    def _pick(self, cur, _prev=None):
        if cur is None or cur.data(HEADER):
            return
        self.current = cur.data(E(Qt, "ItemDataRole", "UserRole") + 1)
        self.head.setText(self.current)
        self.banner.setVisible(self.current in ("Hardware Overview", "Operating System"))
        self._groups = []
        self.tree.clear()
        self.refresh(self.win.snapshot)

    def activate(self):
        if not system._slow:                     # services/lspci/virt: once, in the background
            run_async(system.slow_facts, lambda _r: self.refresh(self.win.snapshot))
        self.refresh(self.win.snapshot)

    def refresh(self, snap):
        if not self.isVisible() or self.current is None:
            return
        try:
            rows = self.funcs[self.current](snap)
        except Exception as e:                   # never take the window down
            rows = [("Error", str(e))]
        self._fill(rows)

    def _fill(self, rows):
        """Update the tree in place when its shape is unchanged (no flicker,
        folded groups stay folded); rebuild it otherwise."""
        rows = [r if len(r) == 3 else (r[0], r[1], None) for r in rows]   # (label, value|[rows], group value)
        shape = [(k, [c[0] for c in v] if isinstance(v, list) else None) for k, v, _g in rows]
        if shape != self._groups:
            expanded = {self.tree.topLevelItem(i).text(0): self.tree.topLevelItem(i).isExpanded()
                        for i in range(self.tree.topLevelItemCount())}
            self.tree.clear()
            bold = QtGui.QFont(self.tree.font())
            bold.setBold(True)
            first_group = True
            for k, v, gv in rows:
                it = QtWidgets.QTreeWidgetItem([str(k), (str(gv) if gv is not None else "")
                                                if isinstance(v, list) else str(v)])
                if isinstance(v, list):
                    if gv is None:                 # a heading (a device, a battery…)
                        it.setFont(0, bold)
                    for ck, cv in v:
                        QtWidgets.QTreeWidgetItem(it, [str(ck), str(cv)])
                self.tree.addTopLevelItem(it)
                if isinstance(v, list):
                    default = (first_group or len(rows) <= 3) if gv is None else False
                    it.setExpanded(expanded.get(str(k), default))
                    if gv is None:
                        first_group = False
            self._groups = shape
            self.tree.resizeColumnToContents(0)
            self.tree.setColumnWidth(0, min(460, max(260, self.tree.columnWidth(0) + 24)))
            return
        for i, (k, v, gv) in enumerate(rows):
            it = self.tree.topLevelItem(i)
            if isinstance(v, list):
                if gv is not None and it.text(1) != str(gv):
                    it.setText(1, str(gv))
                for j, (_ck, cv) in enumerate(v):
                    c = it.child(j)
                    if c.text(1) != str(cv):
                        c.setText(1, str(cv))
            elif it.text(1) != str(v):
                it.setText(1, str(v))

    # live sections use the snapshot the window already sampled
    def _perf(self, snap):
        return (snap or {}).get("perf") or {}

    def _overview(self, snap):
        return system.overview(self._perf(snap))

    def _graphics(self, snap):
        return system.graphics(self._perf(snap))

    def _storage(self, snap):
        return system.storage(self._perf(snap))

    def _network(self, snap):
        return system.network(self._perf(snap))

    def _os(self, snap):
        c = self._perf(snap).get("cpu") or {}
        return system.operating_system(c.get("processes"), c.get("threads"))

    def _sources(self, snap):
        """Where every number in Taskscope comes from on this machine."""
        if "sources" not in self._static_cache:
            perf = self._perf(snap)
            gpus = [(system._gpu_name(g), {"nvml": "NVIDIA NVML (driver library, in-process)",
                                           "nvidia-smi": "nvidia-smi (every 2 s)",
                                           "amdgpu": "amdgpu sysfs", "intel": "i915/xe sysfs (clock ratio)"}
                     .get(g["backend"], g["backend"])) for g in perf.get("gpus") or []]
            mgrs = services.detect()
            hw = glob.glob("/sys/class/hwmon/hwmon*")
            self._static_cache["sources"] = [
                ("Processes", [("Source", "/proc (procfs)"), ("Per-process disk I/O",
                               "/proc/<pid>/io (own processes" + (")" if os.geteuid() else ", all as administrator)"))]),
                ("CPU and memory", [("CPU load", "/proc/stat"), ("Frequency", "cpufreq sysfs"),
                                    ("Memory", "/proc/meminfo"), ("Temperature", "hwmon / thermal zones")]),
                ("Disks", [("Activity", "/proc/diskstats"), ("Models", "/sys/block"), ("Free space", "statvfs")]),
                ("Network", [("Traffic", "/sys/class/net statistics"), ("Connections", "/proc/net/tcp, udp")]),
                ("Graphics", gpus or [("GPUs", "none found")]),
                ("Sensors", [("Chips", f"{len(hw)} hwmon devices"), ("Power", "RAPL (powercap), battery sysfs")]),
                ("Services", [("Manager", ", ".join(m.name for m in mgrs) or "none found")]),
                ("Installed apps", [("Sources", ", ".join(packages.available_sources()))]),
                ("Administrator actions", [("Method", elevate.method() or "none available")]),
                ("Interface", [("Toolkit", version_string())]),
            ]
        return self._static_cache["sources"]

    def _capabilities(self, snap):
        root = os.geteuid() == 0
        admin = "Supported" if root else "Limited: needs administrator"
        rapl = glob.glob("/sys/class/powercap/intel-rapl:*/energy_uj")
        fans = glob.glob("/sys/class/hwmon/hwmon*/fan*_input")
        temps = glob.glob("/sys/class/hwmon/hwmon*/temp*_input")
        bats = [p for p in glob.glob("/sys/class/power_supply/*") if system.read(p + "/type").strip() == "Battery"]
        perf = self._perf(snap)
        gpu_state = "Not available"
        if perf.get("gpus"):
            full = any(g["backend"] in ("nvml", "amdgpu", "nvidia-smi") for g in perf["gpus"])
            gpu_state = "Supported" if full else "Limited: load estimated from clock speed"
        mgrs = services.detect()

        def row(name, state, why=""):
            # status on the row itself; the reason folds out underneath
            return (name, [("Details", why)], state) if why else (name, state)
        rows = [
            row("End / kill your own processes", "Supported"),
            row("End / kill other users' processes", admin, "" if root else
                "Linux only lets the owner or an administrator signal a process"),
            row("Per-process disk read/write", admin if not root else "Supported", "" if root else
                "Other users' I/O counters are private (/proc/<pid>/io)"),
            row("Process names for every connection", admin, "" if root else
                "Mapping other users' sockets to processes needs administrator rights"),
            row("CPU power draw (RAPL)", "Not available" if not rapl else
                ("Supported" if all(os.access(p, os.R_OK) for p in rapl) else "Limited: needs administrator")),
            row("GPU telemetry", gpu_state),
            row("Temperatures", "Supported" if temps else "Not available", f"{len(temps)} sensors"),
            row("Fan speeds", "Supported" if fans else "Not available",
                "" if fans else "No fan sensors exposed by this machine's drivers"),
            row("Battery", "Supported" if bats else "Not available"),
            row("Start / stop services", "Supported" if mgrs else "Not available",
                ", ".join(m.name for m in mgrs) + ("" if root else "; asks for your password")),
            row("Run as administrator", "Supported" if (root or elevate.method()) else "Not available",
                "Already administrator" if root else (elevate.method() or "")),
            row("Disk space scan", "Supported", "All CPU cores (GNU find)" if system.which("find") else "Python"),
            row("Fast ext4 disk scan", "Supported" if root else "Limited: needs administrator",
                "Experimental, turn it on in Settings"),
            row("Data recording and replay", "Supported"),
            row("Remote summary page", "Supported", "Python standard library web server"),
        ]
        return rows

    def _snapshot(self, snap):
        st = self.win.sample_stats
        perf = self._perf(snap)
        c = perf.get("cpu") or {}
        devices = 2 + len(perf.get("disks") or []) + len(perf.get("net") or []) + len(perf.get("gpus") or [])
        sensors = len(glob.glob("/sys/class/hwmon/hwmon*/*_input"))
        rows = [("Sampled at", time.strftime("%d %b %Y %H:%M:%S", time.localtime(st["at"])) if st.get("at") else "-"),
                ("Requested interval", f"{self.win.settings['interval_ms']:.1f} ms"),
                ("Actual interval", f"{st['interval_ms']:.1f} ms" if st.get("interval_ms") else "-"),
                ("Performance sample took", f"{st['perf_ms']:.1f} ms" if st.get("perf_ms") is not None else "-"),
                ("Process sample took", f"{st['procs_ms']:.1f} ms" if st.get("procs_ms") is not None
                 else "not sampled (no page needs it)"),
                ("Updates since start", f"{st.get('ticks', 0):,}"),
                ("Processes", f"{c.get('processes', 0):,}"), ("Threads", f"{c.get('threads', 0):,}"),
                ("Performance devices", devices), ("Hardware sensors", sensors),
                ("Mode", "Replaying a recording" if self.win.player is not None else "Live")]
        return rows

    # ---- copy -----------------------------------------------------------------------
    def _rows_text(self, rows, indent=""):
        out = []
        for r in rows:
            k, v = r[0], r[1]
            if isinstance(v, list):
                out.append(f"{indent}{k}" + (f": {r[2]}" if len(r) == 3 and r[2] is not None else ""))
                out += self._rows_text(v, indent + "    ")
            else:
                out.append(f"{indent}{k}: {v}")
        return out

    def copy(self, everything):
        names = [n for _c, items in self.sections for n, _f in items] if everything else [self.current]
        lines = []
        for n in names:
            try:
                rows = self.funcs[n](self.win.snapshot)
            except Exception as e:
                rows = [("Error", str(e))]
            lines += [f"[{n}]"] + self._rows_text(rows) + [""]
        QtWidgets.QApplication.clipboard().setText("\n".join(lines))
        self.win.flash("Copied " + ("all sections" if everything else self.current), 3000)

    def _menu(self, pos):
        it = self.tree.itemAt(pos)
        if it is None:
            return
        m = QtWidgets.QMenu(self)
        m.addAction("Copy value", lambda: QtWidgets.QApplication.clipboard().setText(it.text(1)))
        m.addAction("Copy row", lambda: QtWidgets.QApplication.clipboard().setText(f"{it.text(0)}: {it.text(1)}"))
        from .qt import exec_
        exec_(m, self.tree.viewport().mapToGlobal(pos))
