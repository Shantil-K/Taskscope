"""The individual pages shown in the main window's sidebar."""

import os
import re
import shlex
import subprocess
import time

from .. import fmt
from ..sysinfo import conns, datarec, drivers, packages, sensors, services, startup, system, users
from .qt import E, QtCore, QtWidgets, Qt, Signal, exec_
from .diskpage import DiskSpacePage
from .homepage import HomePage
from .settingspage import SettingsPage
from .sysinfopage import SystemInfoPage
from .proctree import ProcessesPage
from .userstree import UsersView
from .widgets import (COLORS, CheckDelegate, Column, FilterBar, Graph, Table, enlarge, fit_columns, message,
                      run_async)


def _vbox(widget, margins=8):
    lay = QtWidgets.QVBoxLayout(widget)
    lay.setContentsMargins(margins, margins, margins, margins)
    lay.setSpacing(6)
    return lay


class Page(QtWidgets.QWidget):
    title = ""
    icon = ""
    live = False            # live pages get refresh() every tick

    def activate(self):
        """Called when the page becomes visible."""

    def refresh(self, snap):
        """Called every tick for live pages."""


# ---------------------------------------------------------- performance --

class OverviewThumb(QtWidgets.QWidget):
    """Thumbnail for Performance > Overview: one tile per kind of device,
    each filled to its current load, in that device's graph colour."""
    KINDS = ("cpu", "memory", "gpu", "disk", "net")

    def __init__(self):
        super().__init__()
        self.setFixedSize(64, 34)
        self.levels = {k: 0.0 for k in self.KINDS}

    def set_levels(self, levels):
        self.levels.update({k: max(0.0, min(1.0, v or 0.0)) for k, v in levels.items()})
        self.update()

    def paintEvent(self, _e):
        from .qt import QtGui
        p = QtGui.QPainter(self)
        p.setRenderHint(E(QtGui.QPainter, "RenderHint", "Antialiasing"))
        p.setPen(E(Qt, "PenStyle", "NoPen"))
        n, gap = len(self.KINDS), 3.0
        w = (self.width() - gap * (n - 1)) / n
        h = self.height() - 2
        for i, k in enumerate(self.KINDS):
            x = i * (w + gap)
            col = QtGui.QColor(COLORS[k])
            dim = QtGui.QColor(col)
            dim.setAlpha(45)
            p.setBrush(dim)
            p.drawRoundedRect(QtCore.QRectF(x, 1, w, h), 2.5, 2.5)
            lvl = max(0.08, self.levels[k])          # always show a sliver of colour
            p.setBrush(col)
            p.drawRoundedRect(QtCore.QRectF(x, 1 + h * (1 - lvl), w, h * lvl), 2.5, 2.5)
        p.end()


class DeviceItem(QtWidgets.QWidget):
    """Sidebar entry on the Performance page: name, value and a sparkline."""

    def __init__(self, title, color):
        super().__init__()
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        self.graph = Graph(color, mini=True)
        lay.addWidget(self.graph)
        col = QtWidgets.QVBoxLayout()
        col.setSpacing(0)
        self.name = QtWidgets.QLabel(title)
        self.name.setObjectName("DeviceName")
        self.value = QtWidgets.QLabel("")
        self.value.setObjectName("DeviceValue")
        col.addWidget(self.name)
        col.addWidget(self.value)
        lay.addLayout(col, 1)


def _elided():
    from .homepage import Elided
    lab = Elided("")
    return lab


class GraphPanel(QtWidgets.QWidget):
    """A graph with a caption on the left and a live value on the right."""

    def __init__(self, caption, graph):
        super().__init__()
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        row = QtWidgets.QHBoxLayout()
        self.caption = QtWidgets.QLabel(caption)
        self.value = QtWidgets.QLabel("")
        self.value.setAlignment(E(Qt, "AlignmentFlag", "AlignRight") | E(Qt, "AlignmentFlag", "AlignVCenter"))
        row.addWidget(self.caption)
        row.addStretch(1)
        row.addWidget(self.value)
        lay.addLayout(row)
        self.graph = graph
        lay.addWidget(graph, 1)


class DetailPane(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        lay = _vbox(self, 4)
        head = QtWidgets.QHBoxLayout()
        self.title = QtWidgets.QLabel()
        f = self.title.font()
        f.setPointSizeF(f.pointSizeF() * 1.6)
        self.title.setFont(f)
        from .homepage import Elided
        self.subtitle = Elided("")              # long names are shortened, never widen the page
        self.subtitle.setObjectName("PerfSub")
        self.subtitle.setAlignment(E(Qt, "AlignmentFlag", "AlignRight") | E(Qt, "AlignmentFlag", "AlignVCenter"))
        head.addWidget(self.title)
        # CPU only: one combined graph or one per logical processor
        self.cpu_mode = QtWidgets.QWidget()
        ml = QtWidgets.QHBoxLayout(self.cpu_mode)
        ml.setContentsMargins(12, 0, 0, 0)
        ml.setSpacing(4)
        self.cpu_btns = QtWidgets.QButtonGroup(self)
        self.cpu_btns.setExclusive(True)
        for i, (text, tip) in enumerate((("Overall", "One graph for the whole CPU"),
                                         ("Per core", "One graph per logical processor"))):
            b = QtWidgets.QPushButton(text)
            b.setCheckable(True)
            b.setProperty("seg", True)
            b.setToolTip(tip)
            self.cpu_btns.addButton(b, i)
            ml.addWidget(b)
        self.cpu_mode.hide()
        # the device name takes the free space (shortened with … only if it must be)
        head.addWidget(self.subtitle, 1)
        head.addWidget(self.cpu_mode)
        lay.addLayout(head)
        self.graphs_box = QtWidgets.QGridLayout()
        self.graphs_box.setSpacing(4)
        lay.addLayout(self.graphs_box, 1)
        self.stats = QtWidgets.QGridLayout()
        self.stats.setHorizontalSpacing(28)
        lay.addLayout(self.stats)
        # smaller label/value list under the main figures (GPU details)
        self.details = QtWidgets.QGridLayout()
        self.details.setHorizontalSpacing(14)
        self.details.setVerticalSpacing(2)
        lay.addLayout(self.details)
        self._labels = {}
        self._dlabels = {}

    def set_graphs(self, graphs, cols=1, stretches=None):
        while self.graphs_box.count():
            w = self.graphs_box.takeAt(0).widget()
            if w:
                w.setParent(None)
        for r in range(self.graphs_box.rowCount()):
            self.graphs_box.setRowStretch(r, 0)
        for i, g in enumerate(graphs):
            self.graphs_box.addWidget(g, i // cols, i % cols)
            g.show()
        for r, st in enumerate(stretches or []):
            self.graphs_box.setRowStretch(r, st)

    def set_details(self, items):
        """items: list of (label, value); two label/value pairs per row."""
        keys = [k for k, _ in items]
        if keys != list(self._dlabels):
            while self.details.count():
                w = self.details.takeAt(0).widget()
                if w:
                    w.setParent(None)
            self._dlabels = {}
            for i, k in enumerate(keys):
                cap = QtWidgets.QLabel(k + ":")
                cap.setObjectName("StatCaption")
                val = _elided()
                val.setTextInteractionFlags(E(Qt, "TextInteractionFlag", "TextSelectableByMouse"))
                r, c = i // 2, (i % 2) * 3
                self.details.addWidget(cap, r, c)
                self.details.addWidget(val, r, c + 1)
                self._dlabels[k] = val
            # the value columns take the spare width (shortened values stay readable)
            self.details.setColumnStretch(1, 1)
            self.details.setColumnStretch(4, 1)
            self.details.setColumnMinimumWidth(2, 24)
        for k, v in items:
            self._dlabels[k].setText(str(v))

    def set_stats(self, items):
        """items: list of (label, value); laid out four to a row."""
        keys = [k for k, _ in items]
        if keys != list(self._labels):
            while self.stats.count():
                w = self.stats.takeAt(0).widget()
                if w:
                    w.setParent(None)
            self._labels = {}
            for i, k in enumerate(keys):
                cap = QtWidgets.QLabel(k.upper())
                cap.setObjectName("StatCaption")
                cap.setMinimumWidth(1)
                val = _elided()
                val.setObjectName("StatValue")
                val.setTextInteractionFlags(E(Qt, "TextInteractionFlag", "TextSelectableByMouse"))
                r, c = (i // 4) * 2, i % 4          # four figures per row
                self.stats.addWidget(cap, r, c)
                self.stats.addWidget(val, r + 1, c)
                self.stats.setRowMinimumHeight(r + 1, 30)
                self._labels[k] = val
            for c in range(4):
                self.stats.setColumnStretch(c, 1)
        for k, v in items:
            self._labels[k].setText(str(v))


class PerformancePage(Page):
    title, icon, live = "Performance", "performance", True

    def __init__(self, win):
        super().__init__()
        self.win = win
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        self.list = QtWidgets.QListWidget()
        self.list.setObjectName("DeviceList")
        self.list.setFixedWidth(240)
        self.list.currentRowChanged.connect(self._select)
        lay.addWidget(self.list)
        # Right side: the Overview (all devices as cards, formerly the Summary
        # page) or one device in detail.
        self.right = QtWidgets.QStackedWidget()
        self.detail = DetailPane()
        # If the window is smaller than a view needs, scroll instead of
        # making the window grow.
        self.detail_scroll = QtWidgets.QScrollArea()
        self.detail_scroll.setWidgetResizable(True)
        self.detail_scroll.setFrameShape(E(QtWidgets.QFrame, "Shape", "NoFrame"))
        self.detail_scroll.setWidget(self.detail)
        self.overview = SummaryPage(win)
        self.overview.on_pick = self._pick_card
        self.right.addWidget(self.detail_scroll)
        self.right.addWidget(self.overview)
        lay.addWidget(self.right, 1)
        self.devices = {}       # key -> dict(item, widget, graphs, kind)
        self.order = []
        self.current = None
        self.per_core = win.settings.get("cpu_graph", "cores") == "cores"
        self.detail.cpu_btns.button(1 if self.per_core else 0).setChecked(True)
        self.detail.cpu_btns.idClicked.connect(self._cpu_mode) if hasattr(self.detail.cpu_btns, "idClicked") \
            else self.detail.cpu_btns.buttonClicked[int].connect(self._cpu_mode)
        ov = DeviceItem("Overview", COLORS["cpu"])
        ov.value.setText("All devices at a glance")
        thumb = OverviewThumb()
        ov.layout().replaceWidget(ov.graph, thumb)
        ov.graph.setParent(None)
        ov.graph = thumb
        it = QtWidgets.QListWidgetItem()
        it.setSizeHint(ov.sizeHint())
        self.list.addItem(it)
        self.list.setItemWidget(it, ov)
        self.devices["overview"] = {"item": it, "widget": ov, "graph": None, "kind": "overview",
                                    "title": "Overview", "cores": []}
        self.order.append("overview")
        self.list.setCurrentRow(0)

    def _device(self, key, title, kind):
        if key in self.devices:
            return self.devices[key]
        color = COLORS[kind]
        w = DeviceItem(title, color)
        it = QtWidgets.QListWidgetItem()
        it.setSizeHint(w.sizeHint())
        self.list.addItem(it)
        self.list.setItemWidget(it, w)
        big = Graph(color, maximum=100.0 if kind in ("cpu", "memory", "gpu") else None)
        d = {"item": it, "widget": w, "graph": big, "kind": kind, "title": title, "cores": []}
        self.devices[key] = d
        self.order.append(key)
        if self.list.count() == 1:
            self.list.setCurrentRow(0)
        return d

    def _cpu_mode(self, i):
        self.per_core = i == 1
        self.win.settings["cpu_graph"] = "cores" if self.per_core else "overall"
        if self.current == "cpu":
            self._select(self.order.index("cpu"))

    def _pick_card(self, key):
        if key == "net":
            # The card adds up the connected adapters, so open the one carrying
            # the traffic (connected first, then the busiest), not just the first.
            perf = (self.win.snapshot or {}).get("perf") or {}
            nets = [n for n in perf.get("net") or [] if "net:" + n["name"] in self.order]
            nets.sort(key=lambda n: (n["up"] and n["kind"] != "Virtual", n["up"],
                                     n["rx_rate"] + n["tx_rate"], n["rx_total"] + n["tx_total"]),
                      reverse=True)
            if nets:
                key = "net:" + nets[0]["name"]
        if key in self.order:
            self.list.setCurrentRow(self.order.index(key))

    def _select(self, row):
        if 0 <= row < len(self.order):
            self.current = self.order[row]
            d = self.devices[self.current]
            if self.current == "overview":
                self.right.setCurrentWidget(self.overview)
                self.overview.refresh(self.win.snapshot)
                return
            self.right.setCurrentWidget(self.detail_scroll)
            self.detail.title.setText(d["title"])
            self.detail.cpu_mode.setVisible(d["kind"] == "cpu")
            if d["kind"] == "cpu" and self.per_core and d["cores"]:
                n = len(d["cores"])
                cols = 4 if n > 8 else 2 if n > 1 else 1
                self.detail.set_graphs(d["cores"], cols)
            elif d.get("panels"):
                # GPU: utilisation on top, dedicated memory below (when it has its own)
                self.detail.set_graphs(d["panels"], 1, [3, 1][:len(d["panels"])])
            else:
                self.detail.set_graphs([d["graph"]])
            if d["kind"] != "gpu":
                self.detail.set_details([])
            self._stats = None
            self.refresh(self.win.snapshot)

    def refresh(self, snap):
        perf = snap.get("perf")
        if not perf:
            return
        self.overview.refresh(snap)              # keeps the cards' graphs going
        ov = self.devices["overview"]["widget"]
        disks, nets, gpus = perf["disks"], perf["net"], perf["gpus"]
        rate = sum(n["rx_rate"] + n["tx_rate"] for n in nets if n["up"] and n["kind"] != "Virtual")
        ov.graph.set_levels({
            "cpu": perf["cpu"]["total"] / 100, "memory": perf["memory"]["percent"] / 100,
            "gpu": max([g["util"] or 0 for g in gpus] or [0]) / 100,
            "disk": max([d["busy"] for d in disks] or [0]) / 100,
            "net": min(1.0, rate / (1 << 20)),        # 1 MB/s fills the tile
        })
        stats = {}
        c = perf["cpu"]
        d = self._device("cpu", "CPU", "cpu")
        d["graph"].label = "% Utilization, all logical processors"
        d["graph"].push(c["total"])
        if not d["cores"]:
            for i in range(len(c["cores"])):
                g = Graph(COLORS["cpu"])
                g.setMinimumHeight(50)
                g.label = f"CPU {i}"
                d["cores"].append(g)
            if self.current == "cpu":
                self._select(self.order.index("cpu"))
        for g, v in zip(d["cores"], c["cores"]):
            g.push(v)
        d["widget"].graph.push(c["total"])
        freq = f"{c['freq_mhz'] / 1000:.2f} GHz" if c["freq_mhz"] else ""
        d["widget"].value.setText(f"{c['total']:.0f}%  {freq}")
        stats["cpu"] = (c["model"], [("Utilization", fmt.pct(c["total"])), ("Speed", freq or "n/a"),
                                     ("Processes", c["processes"]), ("Threads", c["threads"]),
                                     ("Logical processors", len(c["cores"])),
                                     ("Load average", " ".join(f"{x:.2f}" for x in c["load"])),
                                     ("Temperature", fmt.temp(c["temp"]) if c["temp"] else "n/a"),
                                     ("Up time", fmt.duration(c["uptime"]))])
        m = perf["memory"]
        d = self._device("mem", "Memory", "memory")
        d["graph"].push(m["percent"])
        d["widget"].graph.push(m["percent"])
        d["widget"].value.setText(f"{fmt.size(m['used'])}/{fmt.size(m['total'])} ({m['percent']:.0f}%)")
        from ..sysinfo import memmodules
        msum = memmodules.summary()
        stats["mem"] = ((f"{msum}  ·  {fmt.size(m['total'])} usable" if msum else fmt.size(m["total"])),
                        [("In use", fmt.size(m["used"])), ("Available", fmt.size(m["available"]))]
                        + memmodules.rows() + [
                                               ("Cached", fmt.size(m["cached"])),
                                               ("Buffers", fmt.size(m["buffers"])),
                                               ("Shared", fmt.size(m["shared"])),
                                               ("Swap used", fmt.pair(m['swap_used'], m['swap_total']))])
        for i, k in enumerate(perf["disks"]):
            d = self._device("disk:" + k["name"], f"Disk {i} ({k['name']})", "disk")
            d["graph"].maximum = 100.0
            d["graph"].push(k["busy"])
            d["widget"].graph.push(k["busy"])
            d["widget"].value.setText(f"{k['kind']}  {k['busy']:.0f}%")
            stats["disk:" + k["name"]] = (k["model"], [
                ("Active time", fmt.pct(k["busy"])), ("Read speed", fmt.rate(k["read_rate"])),
                ("Write speed", fmt.rate(k["write_rate"])), ("Capacity", fmt.size(k["size"])),
                ("Type", k["kind"]), ("Mounted at", ", ".join(k["mounts"][:3]) or "-")])
        for n in perf["net"]:
            if n["kind"] == "Virtual" and not n["up"]:
                continue
            d = self._device("net:" + n["name"], f"{n['kind']} ({n['name']})", "net")
            d["graph"].push(n["rx_rate"], n["tx_rate"])
            d["widget"].graph.maximum = None
            d["widget"].graph.push(n["rx_rate"] + n["tx_rate"])
            d["widget"].value.setText(f"↓ {fmt.bits(n['rx_rate'])}  ↑ {fmt.bits(n['tx_rate'])}"
                                      if n["up"] else "Not connected")
            stats["net:" + n["name"]] = (n["name"], [
                ("Receive", fmt.bits(n["rx_rate"])), ("Send", fmt.bits(n["tx_rate"])),
                ("Link speed", f"{n['speed_mbps']} Mbps" if n["speed_mbps"] else "n/a"),
                ("State", "Up" if n["up"] else "Down"),
                ("Received", fmt.size(n["rx_total"])), ("Sent", fmt.size(n["tx_total"])),
                ("Address", ", ".join(n["addrs"][:2]) or "-"), ("MAC", n["mac"] or "-")])
        gpu_details = {}
        for i, g in enumerate(perf["gpus"]):
            key = "gpu:" + g["id"]
            d = self._device(key, f"GPU {i}", "gpu")
            u = g["util"] or 0.0
            if not d.get("panels"):
                cap = "% Utilization" + (" (estimated from clock speed)" if g["backend"] == "intel" else "")
                d["util_panel"] = GraphPanel(cap, d["graph"])
                d["panels"] = [d["util_panel"]]
            if g.get("mem_total") and len(d["panels"]) == 1:
                mg = Graph(COLORS["gpu"], maximum=float(g["mem_total"]))
                mg.setMinimumHeight(70)
                d["mem_graph"] = mg
                d["mem_panel"] = GraphPanel("Dedicated GPU memory in use", mg)
                d["panels"].append(d["mem_panel"])
                if self.current == key:
                    self._select(self.order.index(key))
            d["graph"].push(u)
            d["widget"].graph.push(u)
            d["util_panel"].value.setText(f"{u:.0f}%" if g["util"] is not None else "n/a")
            if d.get("mem_graph") is not None:
                d["mem_graph"].push(g.get("mem_used") or 0)
                d["mem_panel"].value.setText(fmt.pair(g['mem_used'] or 0, g['mem_total']))
            d["widget"].value.setText((f"{u:.0f}%" if g["util"] is not None else "n/a") +
                                      (f"  {fmt.temp(g['temp'])}" if g.get("temp") else ""))
            items = [("Utilization", fmt.pct(g["util"]) if g["util"] is not None else "n/a")]
            if g.get("mem_total"):
                items.append(("Dedicated memory", fmt.pair(g['mem_used'], g['mem_total'])))
            if g.get("clock"):
                items.append(("Clock", f"{g['clock']:.0f} MHz"))
            if g.get("temp"):
                items.append(("Temperature", fmt.temp(g["temp"])))
            if g.get("power"):
                items.append(("Power draw", f"{g['power']:.1f} W"))
            stats[key] = (g["name"], items)
            det = []
            if g.get("max_clock"):
                det.append(("Max clock", f"{g['max_clock']} MHz"))
            if g.get("mem_clock") is not None:
                det.append(("Memory clock", f"{g['mem_clock']} MHz"))
            if g.get("max_mem_clock"):
                det.append(("Max memory clock", f"{g['max_mem_clock']} MHz"))
            if g.get("power_limit"):
                det.append(("Power limit", f"{g['power_limit']:.0f} W"))
            if g.get("encoder") is not None:
                det.append(("Video encode", fmt.pct(g["encoder"])))
            if g.get("decoder") is not None:
                det.append(("Video decode", fmt.pct(g["decoder"])))
            if g.get("fan") is not None:
                det.append(("Fan", f"{g['fan']}%"))
            if g.get("pcie_gen"):
                link = f"PCIe {g['pcie_gen']}.0 x{g.get('pcie_width') or '?'}"
                if g.get("max_pcie_gen"):
                    link += f" (max {g['max_pcie_gen']}.0 x{g.get('max_pcie_width') or '?'})"
                det.append(("PCI Express", link))
            if g.get("pci"):
                det.append(("Physical location", g["pci"]))
            det.append(("Driver", g["driver"] + (f" {g['driver_version']}" if g.get("driver_version") else "")
                        if g.get("driver") else "-"))
            det.append(("Data source", {"nvml": "NVIDIA NVML", "nvidia-smi": "nvidia-smi",
                                        "amdgpu": "amdgpu (sysfs)", "intel": "i915/xe (sysfs)"}
                        .get(g["backend"], g["backend"])))
            gpu_details[key] = det
        if self.current in stats:
            sub, items = stats[self.current]
            self.detail.subtitle.setText(sub)
            self.detail.set_stats(items)
            if self.current in gpu_details:
                self.detail.set_details(gpu_details[self.current])


# ------------------------------------------------------- lazy list pages --

def stat_cards(items):
    """A row of compact summary boxes. items: [(key, title)].
    Returns (layout, {key: value label})."""
    row = QtWidgets.QHBoxLayout()
    row.setContentsMargins(8, 6, 8, 6)
    row.setSpacing(8)
    labels = {}
    for key, title in items:
        f = QtWidgets.QFrame()
        f.setObjectName("Card")
        f.setFixedHeight(58)
        fl = QtWidgets.QVBoxLayout(f)
        fl.setContentsMargins(12, 6, 12, 6)
        fl.setSpacing(0)
        t = QtWidgets.QLabel(title)
        t.setObjectName("StatCaption")
        v = QtWidgets.QLabel("–")
        v.setObjectName("StatValue")
        fl.addWidget(t)
        fl.addWidget(v)
        labels[key] = v
        row.addWidget(f, 1)
    return row, labels


class ListPage(Page):
    """A filterable table that loads in the background when first shown."""
    columns = []

    def __init__(self, win):
        super().__init__()
        self.win = win
        lay = _vbox(self, 0)
        self.bar = FilterBar("Filter")
        self.toolbar = self.bar
        self.table = Table(self.columns, self.key)
        self.bar.edit.textChanged.connect(self.table.set_filter)
        lay.addWidget(self.table)
        self.status = QtWidgets.QLabel("")
        self.status.setProperty("muted", True)
        self.status.setContentsMargins(12, 4, 12, 4)
        lay.addWidget(self.status)
        self.loaded = False
        self.loading = False
        self.setup()          # the header's Refresh button calls reload()

    def setup(self):
        pass

    def key(self, row):
        return row["name"]

    def load(self):
        return []

    def activate(self):
        if not self.loaded:
            self.reload()

    def reload(self):
        if self.loading:
            self._again = True              # e.g. an option changed mid-load: load again after
            return
        self._again = False
        self.loading = True
        if not self.loaded:                 # later refreshes keep the old text: no flicker
            self.status.setText("Loading…")

        def done(rows):
            self.loading = False
            if getattr(self, "_again", False):     # this result is already out of date
                self.reload()
                return
            if isinstance(rows, Exception):
                self.status.setText(f"Error: {rows}")
                return
            self.loaded = True
            self.table.set_rows(rows)
            text = self.describe(rows)
            if text != self.status.text():
                self.status.setText(text)
        run_async(self.load, done)

    def describe(self, rows):
        return f"{len(rows)} items"


class AppsPage(ListPage):
    title, icon = "Installed Apps", "apps"
    columns = [Column("Application", "name", width=270, icon=lambda r: AppsPage.app_icon(r)),
               Column("Size", "size", lambda v: fmt.size(v) if v else "", numeric=True, width=90),
               Column("Version", "version", width=160),
               Column("Publisher", "publisher", width=230),
               Column("Source", "source", width=110),
               Column("Scope", "scope", width=80), Column("Command", "exec")]

    def setup(self):
        self.bar.edit.setPlaceholderText("Filter applications")
        self.launch_btn = self.bar.button("Launch", self.launch, "Start the selected application")
        self.launch_btn.setEnabled(False)
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._sync_launch())
        self.table.model_.modelReset.connect(self._sync_launch)
        self.bar.button("Uninstall…", self.uninstall, "Remove the selected app with the package manager that installed it")
        self.table.doubleClicked.connect(lambda *_: self.launch())
        self.table.customContextMenuRequested.connect(self.menu)
        lay = self.layout()
        cards, self.counts = stat_cards([("apps", "Applications"), ("user", "User installed"),
                                         ("system", "System installed"), ("space", "Space used")])
        enlarge(self.table, 10.5, 30)                      # room for the app icons
        self.table.setIconSize(QtCore.QSize(22, 22))
        lay.insertLayout(0, cards)
        # Footer: status on the left, packages switch on the right
        lay.removeWidget(self.status)
        foot = QtWidgets.QHBoxLayout()
        foot.setContentsMargins(0, 0, 12, 0)
        foot.addWidget(self.status, 1)
        self.with_pkgs = QtWidgets.QCheckBox("Show packages without desktop entries")
        self.with_pkgs.toggled.connect(lambda *_: self.reload())
        foot.addWidget(self.with_pkgs)
        lay.addLayout(foot)

    def key(self, row):
        return (row["kind"], row["source"], row["id"])

    def load(self):
        return packages.inventory(self.with_pkgs.isChecked())

    def describe(self, rows):
        apps = [r for r in rows if r["kind"] == "app"]
        user = sum(1 for r in apps if r["scope"] == "User")
        self.counts["apps"].setText(str(len(apps)))
        self.counts["user"].setText(str(user))
        self.counts["system"].setText(str(len(apps) - user))
        # several apps can come from one package (e.g. all Cinnamon settings panels):
        # count each package once, and say so on the shared ones
        by_pkg = {}
        for r in rows:
            if r.get("size"):
                by_pkg.setdefault(r.get("package") or r.get("appid") or r["id"], []).append(r)
        total = sum(v[0]["size"] for v in by_pkg.values())
        for pkg, group in by_pkg.items():
            if len(group) > 1:
                for r in group:
                    tips = r.setdefault("_tips", {})
                    tips["size"] = (f"Package {pkg}: {fmt.size(r['size'])}, shared by {len(group)} apps "
                                    f"({', '.join(x['name'] for x in group[:6])}{'…' if len(group) > 6 else ''})")
        unknown = sum(1 for r in rows if not r.get("size"))
        self.counts["space"].setText(fmt.size(total) if by_pkg else "–")
        self.counts["space"].setToolTip(f"{len(by_pkg)} packages, each counted once"
                                        + (f"; {unknown} items have no known size" if unknown else ""))
        for r in rows:
            if r["kind"] == "app" and r.get("description"):
                r["_tips"] = {"name": r["description"]}
            if r["kind"] == "package":
                r["_muted"] = ("exec",)
        extra = len(rows) - len(apps)
        text = "Installed application inventory"
        if extra:
            text += f" · {len(apps)} applications and {extra} other packages"
        return text

    _placeholder = None

    @classmethod
    def app_icon(cls, row):
        from . import appicons, icons, theme as th
        ic = appicons.icon(row.get("icon")) if row.get("kind") == "app" else None
        if ic is None:
            if cls._placeholder is None:
                cls._placeholder = icons.icon("apps", th.current["muted"])
            ic = cls._placeholder
        return ic

    def _sync_launch(self):
        r = self.table.selected()
        ok = bool(r and r.get("exec"))
        self.launch_btn.setEnabled(ok)
        if hasattr(self.launch_btn, "_tb_action"):   # its copy in the » overflow menu
            self.launch_btn._tb_action.setEnabled(ok)

    def launch(self):
        r = self.table.selected()
        if not r or not r.get("exec"):
            return
        from .. import launcher
        ok, msg = launcher.launch_desktop(r)
        message(self, ok, msg)

    def menu(self, pos):
        r = self.table.selected()
        if not r:
            return
        m = QtWidgets.QMenu(self)
        if r.get("exec"):                         # packages without a desktop entry can't be launched
            m.addAction("Launch", self.launch)
            m.addAction("Copy command", lambda: QtWidgets.QApplication.clipboard().setText(r["exec"]))
        m.addAction("Copy name", lambda: QtWidgets.QApplication.clipboard().setText(r["name"]))
        m.addSeparator()
        m.addAction("Uninstall…", self.uninstall)
        exec_(m, self.table.viewport().mapToGlobal(pos))

    # ---- uninstalling ------------------------------------------------------------
    def uninstall(self):
        r = self.table.selected()
        if not r:
            return
        from ..sysinfo import uninstall as un
        self.status.setText(f"Checking what removing {r['name']} involves…")

        def planned(p):
            if isinstance(p, Exception):
                self.status.setText("")
                message(self, False, str(p))
                return
            self.status.setText(self.describe(self.table.model_.all))
            if not p["ok"]:
                message(self, False, p["note"], "Can't uninstall")
                return
            box = QtWidgets.QMessageBox(self)
            box.setIcon(E(QtWidgets.QMessageBox, "Icon", "Warning" if p["warn"] else "Question"))
            box.setWindowTitle("Uninstall")
            box.setText(f"<b>Uninstall {r['name']}?</b>")
            info = [f"Removed with: {p['how']}" + ("  (asks for your password)" if p["root"] and os.geteuid() else "")]
            if p["warn"]:
                info.append(p["warn"])
            if p["note"]:
                info.append(p["note"])
            box.setInformativeText("\n\n".join(info))
            if p["removes"]:
                box.setDetailedText("Will be removed:\n" + "\n".join(p["removes"]))
            yes = box.addButton("Uninstall", E(QtWidgets.QMessageBox, "ButtonRole", "DestructiveRole"))
            box.addButton(E(QtWidgets.QMessageBox, "StandardButton", "Cancel"))
            exec_(box)
            if box.clickedButton() is not yes:
                return
            self.status.setText(f"Uninstalling {r['name']}…")

            def done(res):
                ok, msg = res if isinstance(res, tuple) else (False, str(res))
                if ok and un.gone(r):
                    # take it (and anything removed with it) out of the list straight away
                    removed = set(p["removes"])
                    keep = [x for x in self.table.model_.all
                            if x is not r and not (x.get("package") and x.get("package") in removed)]
                    self.table.set_rows(keep)
                    self.status.setText(self.describe(keep))
                    self.win.flash(f"Uninstalled {r['name']}", 5000)
                else:
                    self.status.setText(self.describe(self.table.model_.all))
                    message(self, False, (msg or "").strip()[-1500:] or "The package manager did not remove it.",
                            "Uninstall failed")
            run_async(un.run, done, p)
        run_async(un.plan, planned, r)


class StartupPage(ListPage):
    title, icon = "Startup apps", "startup"
    columns = [Column("Enabled", "enabled", lambda v: "", width=78),
               Column("Name", "name", width=330),
               Column("Type", "source", width=165),
               Column("Command", "command")]

    def setup(self):
        enlarge(self.table)
        self.bar.edit.setPlaceholderText("Filter startup apps")
        self.table.setItemDelegateForColumn(0, CheckDelegate(self.table, self.set_entry))
        self.table.sortByColumn(1, E(Qt, "SortOrder", "AscendingOrder"))
        self.table.doubleClicked.connect(lambda idx: idx.column() != 0 and self.toggle())
        self.table.customContextMenuRequested.connect(self.menu)
        self.bar.button("Enable / Disable", self.toggle, "Turn the selected entry on or off")
        self.bar.button("Run new task", lambda: run_command_dialog(self))

    def key(self, row):
        return (row["kind"], row["id"])

    def load(self):
        rows = startup.list_entries()
        for r in rows:
            if r["kind"] == "Autostart":
                r["source"] = r["scope"]          # "User" or "System"
            else:
                r["source"] = "systemd user service"
                r["command"] = r["command"] or f"systemctl --user start {r['id']}"
        return rows

    def describe(self, rows):
        on = sum(1 for r in rows if r["enabled"])
        return f"{len(rows)} startup entries · {on} enabled · changes apply to your account only"

    def set_entry(self, row, on):
        ok, msg = startup.set_enabled(row, on)
        if not ok:
            message(self, ok, msg)
        self.reload()

    def toggle(self):
        r = self.table.selected()
        if r:
            self.set_entry(r, not r["enabled"])

    def menu(self, pos):
        r = self.table.selected()
        if not r:
            return
        m = QtWidgets.QMenu(self)
        m.addAction("Disable" if r["enabled"] else "Enable", self.toggle)
        m.addAction("Copy command",
                    lambda: QtWidgets.QApplication.clipboard().setText(r["command"]))
        if r.get("path"):
            m.addAction("Open entry file", lambda: launcher_open(r["path"]))
        exec_(m, self.table.viewport().mapToGlobal(pos))


class UsersPage(Page):
    title, icon, live = "Users", "users", True

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.tick = 0
        lay = _vbox(self, 0)
        self.table = UsersView()
        self.table.win = win
        lay.addWidget(self.table)

    def refresh(self, snap):
        # session list shells out, so refresh it every 5th tick only
        self.tick += 1
        if self.isVisible() or self.tick % 5 == 1:
            self.table.set_procs(snap["procs"])


class ServicesPage(ListPage):
    title, icon = "Services", "services"
    columns = [Column("Name", "name", width=260), Column("State", "state", width=90),
               Column("Sub-state", "sub", width=90), Column("Startup", "enabled", width=90),
               Column("PID", "pid", width=70), Column("Description", "description")]

    def setup(self):
        enlarge(self.table)
        self.managers = services.detect()
        self.mgr = self.bar.add(QtWidgets.QComboBox())
        self.mgr.addItems([m.name for m in self.managers] or ["No service manager found"])
        self.mgr.currentIndexChanged.connect(lambda *_: (self.table.set_rows([]), self._buttons(), self.reload()))
        self.btns = {}
        for a in ("start", "stop", "restart", "enable", "disable"):
            self.btns[a] = self.bar.button(a.capitalize(), lambda _=False, a=a: self.act(a))
        self._buttons()
        self.table.customContextMenuRequested.connect(self.menu)

    def _m(self):
        i = self.mgr.currentIndex()
        return self.managers[i] if 0 <= i < len(self.managers) else None

    def _buttons(self):
        m = self._m()
        for a, b in self.btns.items():
            (getattr(b, "_tb_action", None) or b).setVisible(bool(m) and a in m.actions)

    def load(self):
        m = self._m()
        return m.list() if m else []

    def describe(self, rows):
        m = self._m()
        running = sum(1 for r in rows if r["state"] in ("active", "started", "run", "running", "up"))
        return f"{len(rows)} services, {running} running · manager: {m.name if m else 'none'}"

    def act(self, action):
        r, m = self.table.selected(), self._m()
        if not r or not m:
            return
        self.status.setText(f"{action} {r['name']}…")

        def done(res):
            ok, msg = res if isinstance(res, tuple) else (False, str(res))
            message(self, ok, msg)
            self.reload()
        run_async(m.act, done, action, r["name"])

    def menu(self, pos):
        m = self._m()
        if not m or not self.table.selected():
            return
        menu = QtWidgets.QMenu(self)
        for a in m.actions:
            menu.addAction(a.capitalize(), lambda a=a: self.act(a))
        exec_(menu, self.table.viewport().mapToGlobal(pos))


class DriversPage(ListPage):
    title, icon = "Drivers", "drivers"
    columns = [Column("Module", "name", width=200), Column("State", "state", width=86),
               Column("Size", "size", lambda v: fmt.size(v) if v else "", numeric=True, width=80),
               Column("Used by", "used_by", width=160),
               Column("Version", "version", width=90),
               Column("Source", "source", width=90)]
    FILTERS = [("Loaded", lambda r: r["state"] == "Loaded"),
               ("Not loaded", lambda r: r["state"] == "Not loaded"),
               ("Built-in", lambda r: r["state"] == "Built-in"),
               ("Out-of-tree", lambda r: r["source"] == "Out-of-tree"),
               ("All", None)]
    DETAILS = [("Name", "name"), ("Description", "description"), ("State", "state"),
               ("Source", "source"), ("Version", "version"), ("Source version", "srcversion"),
               ("Kernel memory", "size"), ("Used by", "used_by"), ("Depends on", "depends"),
               ("Devices", "devices"), ("Author", "author"), ("License", "license"),
               ("File path", "path")]

    def setup(self):
        self.bar.edit.setPlaceholderText("Filter drivers")
        self.rows = []
        self._keep = None
        self.cat = self.FILTERS[0][1]        # open on "Loaded"
        lay = self.layout()
        cards, self.counts = stat_cards([("all", "Drivers"), ("loaded", "Loaded"),
                                         ("oot", "Out-of-tree"), ("mem", "Kernel memory")])
        lay.insertLayout(0, cards)
        seg = QtWidgets.QHBoxLayout()
        seg.setContentsMargins(8, 0, 8, 2)
        seg.setSpacing(4)
        group = QtWidgets.QButtonGroup(self)
        group.setExclusive(True)
        for i, (name, fn) in enumerate(self.FILTERS):
            b = QtWidgets.QPushButton(name)
            b.setCheckable(True)
            b.setProperty("seg", True)
            b.setChecked(fn is self.cat)
            b.clicked.connect(lambda _=False, fn=fn: self._category(fn))
            group.addButton(b)
            seg.addWidget(b)
        seg.addStretch(1)
        lay.insertLayout(1, seg)
        self.caption = QtWidgets.QLabel(f"Kernel modules for Linux {drivers.kernel_release()}")
        self.caption.setProperty("muted", True)
        self.caption.setContentsMargins(10, 2, 8, 2)
        lay.insertWidget(2, self.caption)
        # Table and details side by side
        lay.removeWidget(self.table)
        split = QtWidgets.QSplitter(E(Qt, "Orientation", "Horizontal"))
        split.addWidget(self.table)
        split.addWidget(self._details())
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        split.setSizes([900, 300])
        # Fit the table to its width: Module and Used by share the spare room,
        # the rest keep their size, so there is no sideways scrolling.
        self.table.horizontalHeader().setMinimumSectionSize(50)
        fit_columns(self.table, (0, 3), 120)
        split.setChildrenCollapsible(False)
        lay.insertWidget(3, split, 1)
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._show())

    def _details(self):
        box = QtWidgets.QFrame()
        box.setObjectName("DetailPane")
        box.setMinimumWidth(240)
        outer = QtWidgets.QVBoxLayout(box)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QtWidgets.QScrollArea()        # only scrolls on very small windows
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(E(QtWidgets.QFrame, "Shape", "NoFrame"))
        inner = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(inner)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(4)
        sel = E(Qt, "TextInteractionFlag", "TextSelectableByMouse")
        self.dhead = QtWidgets.QLabel("Module details")
        f = self.dhead.font()
        f.setBold(True)
        f.setPointSizeF(f.pointSizeF() * 1.15)
        self.dhead.setFont(f)
        self.dhead.setTextInteractionFlags(sel)
        self.ddesc = QtWidgets.QLabel("Select a module to see its details.")
        self.ddesc.setObjectName("DetailCaption")
        self.ddesc.setWordWrap(True)
        v.addWidget(self.dhead)
        v.addWidget(self.ddesc)
        v.addSpacing(6)
        # One line per field: label on the left, value on the right.
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(5)
        grid.setColumnStretch(1, 1)
        self.dvals = {}
        for i, (title, key) in enumerate(self.DETAILS):
            if key in ("name", "description"):
                continue
            cap = QtWidgets.QLabel(title)
            cap.setObjectName("DetailCaption")
            cap.setAlignment(E(Qt, "AlignmentFlag", "AlignLeft") | E(Qt, "AlignmentFlag", "AlignTop"))
            val = QtWidgets.QLabel()
            val.setWordWrap(True)
            val.setTextInteractionFlags(sel)
            grid.addWidget(cap, i, 0)
            grid.addWidget(val, i, 1)
            cap.hide()
            val.hide()
            self.dvals[key] = (cap, val)
        v.addLayout(grid)
        v.addStretch(1)
        scroll.setWidget(inner)
        outer.addWidget(scroll)
        return box

    def _show(self):
        r = self.table.selected()
        if not r:
            self.dhead.setText("Module details")
            self.ddesc.setText("Select a module to see its details.")
            for cap, val in self.dvals.values():
                cap.hide()
                val.hide()
            return
        if "author" not in r:            # modinfo only for the module you look at
            info = drivers.modinfo(r["name"]) if r["state"] != "Built-in" else {}
            r["author"] = info.get("author", "")
            r["license"] = info.get("license", "")
            for k in ("description", "version", "srcversion"):
                if not r.get(k) and info.get(k):
                    r[k] = info[k]
        self.dhead.setText(r["name"])
        self.ddesc.setText(r.get("description") or "No description")
        for key, (cap, val) in self.dvals.items():
            v = r.get(key)
            if key == "size":
                v = fmt.size(v) if v else ""
            elif key == "author" and v:
                v = re.sub(r"\s*<?[^\s<>]+@[^\s<>]+>?", "", v)   # drop e-mail addresses
            elif key == "srcversion" and v:
                v = "\u200b".join(v[i:i + 8] for i in range(0, len(v), 8))   # wraps if narrow
            elif key == "path" and v:
                v = v.replace(f"/lib/modules/{drivers.kernel_release()}/", "…/")
                v = v.replace("/", "/\u200b")              # let long paths wrap after each folder
            if key == "path" and r.get("path", "").startswith("/"):
                val.setToolTip(r["path"])
            val.setText(str(v) if v else "")
            cap.setVisible(bool(v))
            val.setVisible(bool(v))

    def _category(self, fn):
        self.cat = fn
        cur = self.table.selected() or self._keep
        self._keep = None
        self.table.set_rows([r for r in self.rows if fn(r)] if fn else self.rows)
        if cur is not None:              # keep the same module selected if it is still listed
            for i, r in enumerate(self.table.model_.rows):
                if r["name"] == cur["name"]:
                    self.table.selectRow(i)
                    break
        self._select_first()

    def reload(self):
        self._keep = self.table.selected()     # survive the list being replaced
        super().reload()

    def _select_first(self):
        """Keep a module selected so the details panel is never empty."""
        if self.table.selected() is None and self.table.model_.rowCount() > 0:
            self.table.selectRow(0)
            self.table.scrollToTop()

    def key(self, row):
        return row["name"]

    def load(self):
        return drivers.inventory()

    def describe(self, rows):
        self.rows = rows
        loaded = [r for r in rows if r["state"] == "Loaded"]
        self.counts["all"].setText(f"{len(rows):,}")
        self.counts["loaded"].setText(str(len(loaded)))
        self.counts["oot"].setText(str(sum(1 for r in rows if r["source"] == "Out-of-tree")))
        self.counts["mem"].setText(fmt.size(sum(r["size"] or 0 for r in loaded)))
        self._category(self.cat)
        return ""


# -------------------------------------------------------------- settings --

def launcher_open(path):
    from .. import launcher
    launcher.open_path(path)


def run_command_dialog(parent):
    text, ok = QtWidgets.QInputDialog.getText(parent, "Run new task", "Command to run:")
    if ok and text.strip():
        try:
            subprocess.Popen(shlex.split(text), start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, ValueError) as e:
            message(parent, False, str(e))


# --------------------------------------------------------------- summary --

class Card(QtWidgets.QFrame):
    def __init__(self, title, color, maximum=100.0):
        super().__init__()
        self.setObjectName("Card")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 12)
        t = QtWidgets.QLabel(title)
        t.setObjectName("CardTitle")
        self.value = QtWidgets.QLabel("-")
        self.value.setObjectName("CardValue")
        from .homepage import Elided
        self.sub = Elided("")                # long text is shortened, never widens the card
        self.sub.setProperty("muted", True)
        t.setMinimumWidth(1)
        self.value.setMinimumWidth(1)
        self.graph = Graph(color, maximum=maximum)
        self.graph.setMinimumHeight(80)
        # the smallest size where title, value, detail line and graph all read well;
        # the window's minimum size is based on six of these (Performance > Overview)
        self.setMinimumSize(230, 190)
        for w in (t, self.value, self.sub):
            lay.addWidget(w)
        lay.addWidget(self.graph, 1)
        self.on_click = None

    def mousePressEvent(self, e):
        if self.on_click and e.button() == E(Qt, "MouseButton", "LeftButton"):
            self.on_click()
        super().mousePressEvent(e)


class SummaryPage(Page):
    title, icon, live = "Summary", "summary", True

    def __init__(self, win):
        super().__init__()
        self.win = win
        outer = _vbox(self, 12)
        self.grid = QtWidgets.QGridLayout()
        self.grid.setSpacing(12)
        for c in range(3):                   # every card the same width…
            self.grid.setColumnStretch(c, 1)
        outer.addLayout(self.grid, 1)
        self.facts = QtWidgets.QLabel()
        self.facts.setProperty("muted", True)
        outer.addWidget(self.facts)
        self.cards = {}

    def _card(self, key, title, color, maximum=100.0):
        if key not in self.cards:
            c = Card(title, color, maximum)
            if getattr(self, "on_pick", None):
                c.on_click = lambda k=key: self.on_pick(k)
                c.setCursor(E(Qt, "CursorShape", "PointingHandCursor"))
                c.setToolTip("Click for details")
            n = len(self.cards)
            self.grid.addWidget(c, n // 3, n % 3)
            self.grid.setRowStretch(n // 3, 1)    # …and the same height
            self.cards[key] = c
        return self.cards[key]

    def refresh(self, snap):
        perf = snap.get("perf")
        if not perf:
            return
        c, m = perf["cpu"], perf["memory"]
        k = self._card("cpu", "CPU", COLORS["cpu"])
        k.value.setText(f"{c['total']:.0f}%")
        freq = f"{c['freq_mhz'] / 1000:.2f} GHz" if c["freq_mhz"] else ""
        temp = f"  ·  {fmt.temp(c['temp'])}" if c["temp"] else ""
        k.sub.setText(f"{freq}{temp}  ·  {len(c['cores'])} threads")
        k.graph.push(c["total"])
        k = self._card("mem", "Memory", COLORS["memory"])
        k.value.setText(f"{m['percent']:.0f}%")
        k.sub.setText(f"{fmt.size(m['used'])} of {fmt.size(m['total'])}")
        k.graph.push(m["percent"])
        from ..sysinfo.sensors import _clean_gpu
        for i, g in enumerate(perf["gpus"]):
            k = self._card("gpu:" + g["id"], f"GPU {i}", COLORS["gpu"])
            k.value.setText(fmt.pct(g["util"]) if g["util"] is not None else "n/a")
            k.sub.setText(_clean_gpu(g["name"])[:48])
            k.graph.push(g["util"] or 0)
        for d in perf["disks"]:
            k = self._card("disk:" + d["name"], f"Disk ({d['name']})", COLORS["disk"])
            k.value.setText(f"{d['busy']:.0f}%")
            k.sub.setText(f"R {fmt.rate(d['read_rate'])}  ·  W {fmt.rate(d['write_rate'])}")
            k.graph.push(d["busy"])
        up = [n for n in perf["net"] if n["up"] and n["kind"] != "Virtual"]
        rx = sum(n["rx_rate"] for n in up)
        tx = sum(n["tx_rate"] for n in up)
        k = self._card("net", "Network", COLORS["net"], maximum=None)
        k.value.setText(fmt.bits(rx + tx))
        k.sub.setText(f"↓ {fmt.bits(rx)}  ·  ↑ {fmt.bits(tx)}")
        k.graph.push(rx, tx)
        self.facts.setText(f"{c['processes']} processes  ·  {c['threads']} threads  ·  "
                           f"load {' '.join(f'{x:.2f}' for x in c['load'])}  ·  "
                           f"up {fmt.duration(c['uptime'])}")


# ------------------------------------------------------------ system info --

# --------------------------------------------------------- power & freq --

class PowerPage(Page):
    """Sensor tree: every clock, temperature, power and load reading the
    system exposes, grouped by device, with the lowest and highest value seen
    since the page was opened (or since Reset min/max)."""
    title, icon, live = "Power & Freq", "power", True

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.tree = None
        self.items = {}
        self.lo, self.hi = {}, {}
        lay = _vbox(self, 0)
        self.toolbar = QtWidgets.QWidget()
        tl = QtWidgets.QHBoxLayout(self.toolbar)
        tl.setContentsMargins(0, 0, 0, 0)
        reset = QtWidgets.QPushButton("Reset min/max")
        reset.setToolTip("Start recording the lowest and highest values again")
        reset.clicked.connect(self.reset)
        tl.addWidget(reset)
        self.view = QtWidgets.QTreeWidget()
        self.view.setColumnCount(4)
        self.view.setHeaderLabels(["Sensor", "Value", "Min", "Max"])
        enlarge(self.view)
        self.view.setUniformRowHeights(True)
        self.view.setAlternatingRowColors(True)
        self.view.setAllColumnsShowFocus(True)
        self.view.setIndentation(20)
        hdr = self.view.header()
        hdr.setHighlightSections(False)
        for c, w in ((0, 400), (1, 190), (2, 130), (3, 130)):
            self.view.setColumnWidth(c, w)
        fit_columns(self.view, (0,), 200)
        self.view.setContextMenuPolicy(E(Qt, "ContextMenuPolicy", "CustomContextMenu"))
        self.view.customContextMenuRequested.connect(self.menu)
        lay.addWidget(self.view)

    def _build(self, perf):
        from . import icons, theme
        from .qt import QtGui
        self.tree = sensors.SensorTree(perf)
        self.units = {n["id"]: n["unit"] for n in self.tree.nodes}
        bold = QtGui.QFont(self.view.font())
        bold.setBold(True)
        col = theme.current["muted"]
        self.shown = [n for n in self.tree.nodes if not n["hidden"]]
        for n in self.shown:
            parent = self.items.get(n["parent"], self.view.invisibleRootItem())
            it = QtWidgets.QTreeWidgetItem(parent, [n["label"], "", "", ""])
            it.setIcon(0, icons.icon(n["icon"], col))
            if n["tip"]:
                it.setToolTip(0, n["tip"])
                it.setToolTip(1, n["tip"])
            if n["fn"] is None:          # group rows are bold
                it.setFont(0, bold)
            self.items[n["id"]] = it
        for n in self.shown:
            self.items[n["id"]].setExpanded(n["expanded"])

    @staticmethod
    def _fmt(v, unit):
        if v is None:
            return ""
        if unit == "text":
            return str(v)
        if unit == "GHz":
            return f"{v:.2f} GHz"
        if unit == "C":
            return fmt.temp(v, 1)
        if unit == "W":
            return f"{v:.1f} W"
        if unit == "%":
            return f"{v:.1f}%"
        if unit == "B":
            return fmt.size(v)
        if unit == "RPM":
            return f"{v:.0f} RPM"
        if unit == "V":
            return f"{v:.2f} V"
        return str(v)

    def refresh(self, snap):
        if not self.isVisible():
            return
        perf = snap.get("perf")
        if self.tree is None:
            if perf is None:
                return
            self._build(perf)
        vals = self.tree.sample(perf)
        for k, v in vals.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                if k not in self.lo or v < self.lo[k]:
                    self.lo[k] = v
                if k not in self.hi or v > self.hi[k]:
                    self.hi[k] = v
        from .qt import QtGui
        from . import theme
        muted = QtGui.QColor(theme.current["muted"])
        for n in self.shown:
            src = n["id"] if n["fn"] is not None else n["primary"]
            if not src:
                continue
            unit = self.units[src]
            it = self.items[n["id"]]
            v = vals.get(src)
            texts = (self._fmt(v, unit),
                     "" if unit == "text" else self._fmt(self.lo.get(src), unit),
                     "" if unit == "text" else self._fmt(self.hi.get(src), unit))
            for c, t in enumerate(texts, 1):
                if it.text(c) != t:
                    it.setText(c, t)
            if v == "Requires administrator":
                it.setForeground(1, muted)

    def reset(self):
        self.lo.clear()
        self.hi.clear()
        self.refresh(self.win.snapshot)

    def menu(self, pos):
        it = self.view.itemAt(pos)
        m = QtWidgets.QMenu(self)
        if it is not None:
            m.addAction("Copy", lambda: QtWidgets.QApplication.clipboard().setText(
                "\t".join(it.text(c) for c in range(4)).rstrip()))
            m.addSeparator()
        m.addAction("Expand all", self.view.expandAll)
        m.addAction("Collapse all", self.view.collapseAll)
        m.addAction("Reset min/max", self.reset)
        exec_(m, self.view.viewport().mapToGlobal(pos))


# ------------------------------------------------------------ connections --

class ConnectionsPage(ListPage):
    title, icon = "Connections", "connections"
    columns = [Column("Application", "app", width=170), Column("Protocol", "proto", width=72),
               Column("State", "state", width=90), Column("Local endpoint", "local", width=170),
               Column("Remote / service", "service", width=220),
               Column("Exposure", "exposure", width=110), Column("PID", "pid", numeric=True, width=70),
               Column("User", "user", width=100)]

    TIPS = {
        "denied": "This socket belongs to another user's process. Linux only shows which "
                  "process owns a socket to its owner or an administrator. Use File → Run as "
                  "administrator to see it.",
        "only": "Found from the socket's owner: this user runs only this one process.",
        "main": "Found from the socket's owner: this is the main process of that service "
                "account. The socket may be held by one of its child processes.",
        "none": "The connection is closing and no process holds it any more.",
    }
    EXPOSURE = {
        "All interfaces": "Listening on every network interface: other devices on your "
                          "networks can connect unless a firewall blocks them.",
        "Loopback": "Only programs on this computer can reach it.",
        "Local network": "Talks to (or listens on) a private network address such as your "
                         "home LAN or a virtual machine network.",
        "Internet": "Connected to a public address outside your local network.",
    }

    def setup(self):
        # Fit the table to the window: Application, Local endpoint and
        # Remote / service share the spare room, so there is no sideways scrolling.
        self.table.horizontalHeader().setMinimumSectionSize(50)
        fit_columns(self.table, (0, 3, 4), 110)
        self.hide_listen = self.bar.add(QtWidgets.QCheckBox("Hide listening"))
        self.hide_listen.toggled.connect(lambda *_: self.reload())
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(lambda: self.isVisible() and self.reload())
        self.timer.start(5000)     # only does work while the page is visible

    def key(self, row):
        return row["key"]

    def load(self):
        rows = conns.list_connections()
        if self.hide_listen.isChecked():
            rows = [r for r in rows if r["state"] != "Listening"]
        for r in rows:
            tip = self.TIPS.get(r["how"])
            tips = {"exposure": self.EXPOSURE.get(r["exposure"], "")}
            if r["app"] != r["process"]:
                tips["app"] = f"Process: {r['process']}"
            if tip:
                tips["app"] = tips["pid"] = tip
            r["_tips"] = tips
            if r["how"] in ("denied", "none"):
                r["_muted"] = ("app",)
        return rows

    def describe(self, rows):
        unknown = sum(1 for r in rows if r["how"] == "denied")
        note = (f" · {unknown} belong to other users' processes; run as administrator to see "
                "which process") if unknown else ""
        return f"{len(rows)} sockets{note}"


# ------------------------------------------------------------- disk space --

# ------------------------------------------------------------- recordings --

def _when(t):
    """'Today, 14:05' / 'Yesterday, 09:12' / 'Mon 27 Oct, 18:40'."""
    lt = time.localtime(t)
    today = time.localtime()
    day = (time.mktime(today[:3] + (0, 0, 0) + today[6:]) - time.mktime(lt[:3] + (0, 0, 0) + lt[6:])) / 86400
    hm = time.strftime("%H:%M", lt)
    if round(day) == 0:
        return f"Today, {hm}"
    if round(day) == 1:
        return f"Yesterday, {hm}"
    return time.strftime("%a %d %b %Y, %H:%M", lt)


def _length(sec):
    sec = int(round(sec or 0))
    if sec < 60:
        return f"{sec} second{'s' if sec != 1 else ''}"
    if sec < 3600:
        return f"{sec // 60} min {sec % 60:02d} s"
    return f"{sec // 3600} h {sec % 3600 // 60:02d} min"


class _RecRow(QtWidgets.QWidget):
    """One recording in the list: name, length and size, and its buttons."""

    def __init__(self, page, rec):
        super().__init__()
        self.rec = rec
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 8, 6)
        from . import icons, theme as th
        ic = QtWidgets.QLabel()
        ic.setPixmap(icons.icon("clock", th.current["muted"]).pixmap(22, 22))
        lay.addWidget(ic)
        col = QtWidgets.QVBoxLayout()
        col.setSpacing(0)
        title = QtWidgets.QLabel(rec["title"] or _when(rec["start"]))
        f = title.font()
        f.setBold(True)
        title.setFont(f)
        if rec.get("recording"):
            bits = ["<span style='color:#e5383b'>● recording now</span>"]
        else:
            bits = [_length(rec["duration"]), fmt.size(rec["size"])]
            if rec["title"]:
                bits.insert(0, _when(rec["start"]))
            if rec.get("host"):
                bits.append(rec["host"])
        sub = QtWidgets.QLabel("  ·  ".join(bits))
        sub.setProperty("muted", True)
        col.addWidget(title)
        col.addWidget(sub)
        lay.addLayout(col, 1)
        if not rec.get("recording"):           # nothing to replay until it is finished
            play = QtWidgets.QPushButton("▶  Replay")
            play.clicked.connect(lambda: page.replay(rec))
            lay.addWidget(play)
        more = QtWidgets.QToolButton()
        more.setText("⋯")
        more.setPopupMode(E(QtWidgets.QToolButton, "ToolButtonPopupMode", "InstantPopup"))
        m = QtWidgets.QMenu(more)
        m.addAction("Rename…", lambda: page.rename(rec))
        m.addAction("Show in folder", page.open_folder)
        m.addSeparator()
        d = m.addAction("Delete…", lambda: page.delete(rec))
        d.setEnabled(not rec.get("recording"))
        more.setMenu(m)
        lay.addWidget(more)


class RecTimeline(QtWidgets.QWidget):
    """The whole recording at a glance: CPU (filled) and memory (line).
    Click anywhere to start the replay at that moment."""
    picked = Signal(float)

    def __init__(self):
        super().__init__()
        self.data, self.peak, self.n = [], None, 0
        self.setMinimumHeight(110)
        self.setCursor(E(Qt, "CursorShape", "PointingHandCursor"))
        self.setToolTip("CPU (blue) and memory (purple) over the whole recording. Click to replay from there.")
        self._hover = None
        self.setMouseTracking(True)

    def set_data(self, data, peak, n):
        self.data, self.peak, self.n = data, peak, n
        self.update()

    def paintEvent(self, _e):
        from .qt import QtGui
        from . import theme
        from .widgets import tint
        p = QtGui.QPainter(self)
        p.setRenderHint(E(QtGui.QPainter, "RenderHint", "Antialiasing"))
        r = QtCore.QRectF(0, 0, self.width() - 1, self.height() - 18)
        p.fillRect(r, QtGui.QColor(theme.current["base"]))
        grid = QtGui.QColor(theme.current["border"])
        p.setPen(QtGui.QPen(grid, 1))
        for i in range(1, 4):
            y = r.top() + r.height() * i / 4
            p.drawLine(QtCore.QPointF(r.left(), y), QtCore.QPointF(r.right(), y))
        p.drawRect(r)
        d = self.data
        if len(d) >= 2:
            def pts(col):
                return [QtCore.QPointF(r.left() + r.width() * i / (len(d) - 1),
                                       r.bottom() - r.height() * min(100.0, x[col]) / 100.0) for i, x in enumerate(d)]
            cpu = tint(COLORS["cpu"])
            fill = QtGui.QColor(cpu)
            fill.setAlpha(60)
            c_pts = pts(1)
            p.setPen(E(Qt, "PenStyle", "NoPen"))
            p.setBrush(fill)
            p.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(r.left(), r.bottom())] + c_pts +
                                          [QtCore.QPointF(r.right(), r.bottom())]))
            p.setBrush(E(Qt, "BrushStyle", "NoBrush"))
            p.setPen(QtGui.QPen(cpu, 1.4))
            p.drawPolyline(QtGui.QPolygonF(c_pts))
            p.setPen(QtGui.QPen(tint(COLORS["memory"]), 1.4))
            p.drawPolyline(QtGui.QPolygonF(pts(2)))
            if self.peak is not None and self.n > 1:     # mark the busiest moment
                x = r.left() + r.width() * self.peak / (self.n - 1)
                p.setPen(QtGui.QPen(QtGui.QColor(theme.current["warn"]), 1, E(Qt, "PenStyle", "DashLine")))
                p.drawLine(QtCore.QPointF(x, r.top()), QtCore.QPointF(x, r.bottom()))
            if self._hover is not None:
                p.setPen(QtGui.QPen(QtGui.QColor(theme.current["text"]), 1))
                p.drawLine(QtCore.QPointF(self._hover, r.top()), QtCore.QPointF(self._hover, r.bottom()))
            t0, t1 = d[0][0], d[-1][0]
            p.setPen(QtGui.QColor(theme.current["muted"]))
            small = QtGui.QFont(self.font())
            small.setPointSizeF(8)
            p.setFont(small)
            p.drawText(QtCore.QRectF(0, r.bottom() + 2, 200, 16), E(Qt, "AlignmentFlag", "AlignLeft"),
                       time.strftime("%H:%M:%S", time.localtime(t0)))
            p.drawText(QtCore.QRectF(r.right() - 200, r.bottom() + 2, 200, 16), E(Qt, "AlignmentFlag", "AlignRight"),
                       time.strftime("%H:%M:%S", time.localtime(t1)))
            p.drawText(QtCore.QRectF(0, r.bottom() + 2, r.width(), 16), E(Qt, "AlignmentFlag", "AlignHCenter"),
                       "— CPU   — Memory   ┆ busiest moment")
        p.end()

    def mouseMoveEvent(self, e):
        pos = e.position() if hasattr(e, "position") else QtCore.QPointF(e.pos())
        self._hover = pos.x()
        if len(self.data) >= 2:
            i = int(round(pos.x() / max(1, self.width() - 1) * (len(self.data) - 1)))
            x = self.data[max(0, min(len(self.data) - 1, i))]
            gp = e.globalPosition().toPoint() if hasattr(e, "globalPosition") else e.globalPos()
            QtWidgets.QToolTip.showText(gp, f"{time.strftime('%H:%M:%S', time.localtime(x[0]))}\n"
                                            f"CPU {x[1]:.0f}%  ·  Memory {x[2]:.0f}%  ·  Disk {x[3]:.0f}%  ·  "
                                            f"Network {fmt.rate(x[4])}", self)
        self.update()

    def leaveEvent(self, e):
        self._hover = None
        self.update()
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        if self.data and e.button() == E(Qt, "MouseButton", "LeftButton"):
            pos = e.position() if hasattr(e, "position") else QtCore.QPointF(e.pos())
            self.picked.emit(max(0.0, min(1.0, pos.x() / max(1, self.width() - 1))))


class RecordingsPage(Page):
    """Record Taskscope's live data and replay it later."""
    title, icon, live = "Recordings", "clock", False

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.recs = []
        self._summaries = {}
        self.toolbar = QtWidgets.QWidget()
        tl = QtWidgets.QHBoxLayout(self.toolbar)
        tl.setContentsMargins(0, 0, 0, 0)
        b = QtWidgets.QPushButton("Open a recording file…")
        b.clicked.connect(lambda: self.win.open_recording_dialog())
        tl.addWidget(b)
        b = QtWidgets.QPushButton("Open folder")
        b.clicked.connect(self.open_folder)
        tl.addWidget(b)

        lay = _vbox(self, 8)
        lay.setSpacing(10)
        # ---- the big record control
        hero = QtWidgets.QFrame()
        hero.setObjectName("Card")
        hl = QtWidgets.QHBoxLayout(hero)
        hl.setContentsMargins(18, 14, 18, 14)
        hl.setSpacing(16)
        self.big = QtWidgets.QPushButton()
        self.big.setFixedSize(64, 64)
        self.big.setIconSize(QtCore.QSize(28, 28))
        self.big.clicked.connect(self._big_clicked)
        hl.addWidget(self.big)
        txt = QtWidgets.QVBoxLayout()
        txt.setSpacing(2)
        self.state = QtWidgets.QLabel()
        f = self.state.font()
        f.setPointSizeF(f.pointSizeF() * 1.35)
        f.setBold(True)
        self.state.setFont(f)
        self.hint = QtWidgets.QLabel()
        self.hint.setWordWrap(True)
        self.hint.setProperty("muted", True)
        txt.addWidget(self.state)
        txt.addWidget(self.hint)
        hl.addLayout(txt, 1)
        lay.addWidget(hero)

        # ---- list on the left, the selected recording on the right
        cap = QtWidgets.QLabel("Your recordings")
        f = cap.font()
        f.setBold(True)
        cap.setFont(f)
        lay.addWidget(cap)
        body = QtWidgets.QSplitter(E(Qt, "Orientation", "Horizontal"))
        self.stack = QtWidgets.QStackedWidget()
        self.list = QtWidgets.QListWidget()
        self.list.setObjectName("RecList")
        self.list.currentRowChanged.connect(lambda *_: self._show_selected())
        self.list.itemDoubleClicked.connect(lambda it: self.replay(self._rec_of(it)))
        self.empty = QtWidgets.QLabel("No recordings yet.\n\nPress the red button above (or Ctrl+Shift+R) "
                                      "to start. Taskscope will save what it sees every second, and "
                                      "you can replay it here afterwards.")
        self.empty.setAlignment(E(Qt, "AlignmentFlag", "AlignCenter"))
        self.empty.setWordWrap(True)
        self.empty.setProperty("muted", True)
        self.stack.addWidget(self.list)
        self.stack.addWidget(self.empty)
        body.addWidget(self.stack)
        body.addWidget(self._detail_pane())
        body.setStretchFactor(0, 2)
        body.setStretchFactor(1, 3)
        body.setSizes([440, 800])
        lay.addWidget(body, 1)
        self.footer = QtWidgets.QLabel()
        self.footer.setProperty("muted", True)
        lay.addWidget(self.footer)

        self._tick = QtCore.QTimer(self)
        self._tick.timeout.connect(self._live_update)
        self._tick.start(1000)
        self.update_state(reload=False)

    # ---- the right-hand summary --------------------------------------------
    STATS = ["Length", "Samples", "Average CPU", "Busiest moment", "Average memory", "Peak memory",
             "Disk activity", "Network", "Peak GPU", "Temperatures", "Processes", "File"]

    def _detail_pane(self):
        outer = self.detail_box = QtWidgets.QFrame()
        outer.setObjectName("DetailPane")
        ol = QtWidgets.QVBoxLayout(outer)
        ol.setContentsMargins(0, 0, 0, 0)
        scroll = QtWidgets.QScrollArea()             # long summaries scroll inside the panel
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(E(QtWidgets.QFrame, "Shape", "NoFrame"))
        box = QtWidgets.QWidget()
        scroll.setWidget(box)
        scroll.setHorizontalScrollBarPolicy(E(Qt, "ScrollBarPolicy", "ScrollBarAlwaysOff"))
        ol.addWidget(scroll)
        v = QtWidgets.QVBoxLayout(box)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(6)
        self.d_title = QtWidgets.QLabel("Select a recording")
        f = self.d_title.font()
        f.setBold(True)
        f.setPointSizeF(f.pointSizeF() * 1.15)
        self.d_title.setFont(f)
        self.d_title.setWordWrap(True)
        self.d_sub = QtWidgets.QLabel("")
        self.d_sub.setProperty("muted", True)
        self.d_sub.setWordWrap(True)
        v.addWidget(self.d_title)
        v.addWidget(self.d_sub)
        # timeline: click to start the replay at that moment
        self.d_chart = RecTimeline()
        self.d_chart.picked.connect(self._replay_at)
        v.addWidget(self.d_chart)
        self.d_grid = QtWidgets.QGridLayout()
        self.d_grid.setHorizontalSpacing(14)
        self.d_grid.setVerticalSpacing(4)
        self.d_vals = {}
        for i, k in enumerate(self.STATS):
            cap = QtWidgets.QLabel(k)
            cap.setObjectName("StatCaption")
            val = QtWidgets.QLabel("")
            val.setWordWrap(True)
            val.setTextInteractionFlags(E(Qt, "TextInteractionFlag", "TextSelectableByMouse"))
            self.d_grid.addWidget(cap, i, 0, E(Qt, "AlignmentFlag", "AlignTop"))
            self.d_grid.addWidget(val, i, 1)
            self.d_vals[k] = (cap, val)
        self.d_grid.setColumnStretch(1, 1)
        v.addLayout(self.d_grid)
        tops = QtWidgets.QHBoxLayout()
        tops.setSpacing(18)
        self.d_top_cap = QtWidgets.QLabel("Used the most CPU")
        self.d_top_cap.setObjectName("StatCaption")
        self.d_top = QtWidgets.QLabel("")
        self.d_top.setTextFormat(E(Qt, "TextFormat", "RichText"))
        self.d_mem_cap = QtWidgets.QLabel("Used the most memory")
        self.d_mem_cap.setObjectName("StatCaption")
        self.d_mem = QtWidgets.QLabel("")
        self.d_mem.setTextFormat(E(Qt, "TextFormat", "RichText"))
        for cap, lab in ((self.d_top_cap, self.d_top), (self.d_mem_cap, self.d_mem)):
            col = QtWidgets.QVBoxLayout()
            col.setSpacing(2)
            col.addWidget(cap)
            col.addWidget(lab)
            col.setAlignment(E(Qt, "AlignmentFlag", "AlignTop"))
            tops.addLayout(col)
        tops.addStretch(1)
        v.addSpacing(4)
        v.addLayout(tops)
        v.addStretch(1)
        row = QtWidgets.QHBoxLayout()
        self.d_play = QtWidgets.QPushButton("▶  Replay from the start")
        self.d_play.clicked.connect(lambda: self.replay(self._selected()))
        self.d_peak = QtWidgets.QPushButton("Jump to the busiest moment")
        self.d_peak.clicked.connect(self._replay_peak)
        self.d_export = QtWidgets.QPushButton("Copy summary")
        self.d_export.clicked.connect(self._copy_summary)
        row.addWidget(self.d_play)
        row.addWidget(self.d_peak)
        row.addWidget(self.d_export)
        row.addStretch(1)
        v.addLayout(row)
        self._detail_visible(False)
        return outer

    def _detail_visible(self, on):
        for cap, val in self.d_vals.values():
            cap.setVisible(on)
            val.setVisible(on)
        for w in (self.d_chart, self.d_top_cap, self.d_top, self.d_mem_cap, self.d_mem,
                  self.d_play, self.d_peak, self.d_export):
            w.setVisible(on)

    def _rec_of(self, item):
        i = self.list.row(item)
        return self.recs[i] if 0 <= i < len(self.recs) else None

    def _selected(self):
        i = self.list.currentRow()
        return self.recs[i] if 0 <= i < len(self.recs) else None

    def _show_selected(self):
        r = self._selected()
        if r is None:
            self.d_title.setText("Select a recording")
            self.d_sub.setText("Its summary appears here.")
            self._detail_visible(False)
            return
        self.d_title.setText(r["title"] or _when(r["start"]))
        self.d_sub.setText(time.strftime("%A %d %B %Y, %H:%M:%S", time.localtime(r["start"])) +
                           (f" on {r['host']}" if r.get("host") else ""))
        self._detail_visible(True)
        self.d_vals["Length"][1].setText(f"{_length(r['duration'])}  ({fmt.size(r['size'])})")
        busy = bool(r.get("recording"))
        self.d_play.setEnabled(not busy)
        self.d_peak.setEnabled(False)
        if busy:
            self._detail_visible(False)
            self.d_sub.setText(self.d_sub.text() + "\n\nStill recording. Stop it to see what it "
                               "captured and to replay it.")
            return
        key = (r["path"], r["size"])
        if key in self._summaries:
            self._fill_summary(r, self._summaries[key])
            return
        for k in self.STATS[1:]:
            self.d_vals[k][1].setText("…")
        self.d_chart.set_data([], None, 0)
        self.d_top.setText("Reading the recording…")
        self.d_mem.setText("")

        def done(res):
            if isinstance(res, Exception):
                self.d_top.setText(f"Could not read it: {res}")
                return
            self._summaries[key] = res
            if self._selected() is r:
                self._fill_summary(r, res)
        run_async(datarec.summarize, done, r["path"])

    def _fill_summary(self, r, s):
        v = {k: lab for k, (_c, lab) in self.d_vals.items()}
        at = lambda t: time.strftime("%H:%M:%S", time.localtime(t)) if t else "-"   # noqa: E731
        into = lambda t: f" ({fmt.duration(t - s['start'])} in)" if t and s["start"] else ""  # noqa: E731
        v["Samples"].setText(f"{s['samples']:,}" + (f", one every {s['interval']:.1f} s" if s["interval"] else ""))
        v["Average CPU"].setText(fmt.pct(s["cpu_avg"]) if s["cpu_avg"] is not None else "-")
        v["Busiest moment"].setText(f"CPU {s['cpu_max']:.0f}% at {at(s['cpu_max_time'])}{into(s['cpu_max_time'])}"
                                    if s["cpu_max"] is not None else "-")
        v["Average memory"].setText(fmt.pct(s["mem_avg"]) if s["mem_avg"] is not None else "-")
        v["Peak memory"].setText(f"{s['mem_max']:.0f}%" + (f" of {fmt.size(s['mem_total'])}" if s["mem_total"] else "")
                                 + f" at {at(s['mem_max_time'])}" if s["mem_max"] is not None else "-")
        v["Disk activity"].setText(f"{s['disk_avg']:.0f}% average, peak {s['disk_max']:.0f}% at {at(s['disk_max_time'])}"
                                   if s["disk_avg"] is not None else "-")
        v["Network"].setText(f"↓ {fmt.size(s['rx_total'])} received, ↑ {fmt.size(s['tx_total'])} sent; "
                             f"peak {fmt.rate(s['net_peak'])} at {at(s['net_peak_time'])}"
                             if s["net_peak"] is not None else "-")
        v["Peak GPU"].setText(fmt.pct(s["gpu_max"]) if s["gpu_max"] is not None else "-")
        temps = []
        if s["cpu_temp_max"]:
            temps.append(f"CPU up to {fmt.temp(s['cpu_temp_max'])}")
        if s["gpu_temp_max"]:
            temps.append(f"GPU up to {fmt.temp(s['gpu_temp_max'])}")
        v["Temperatures"].setText(", ".join(temps) or "-")
        v["Processes"].setText(f"{s['procs_lo']:,} – {s['procs_hi']:,} running" if s["procs_lo"] else "-")
        v["File"].setText(r["path"])
        rows = "".join(f"<tr><td>{name}</td><td align='right'>&nbsp;&nbsp;{fmt.cpu(cpu)} avg</td></tr>"
                       for name, cpu, _mem in s["top"])
        self.d_top.setText(f"<table>{rows}</table>" if rows else "-")
        rows = "".join(f"<tr><td>{name}</td><td align='right'>&nbsp;&nbsp;{fmt.size(mem)}</td></tr>"
                       for name, mem in s["top_mem"])
        self.d_mem.setText(f"<table>{rows}</table>" if rows else "-")
        self.d_chart.set_data(s["timeline"], s["cpu_max_index"] if s["samples"] else None, s["samples"])
        self.d_peak.setEnabled(s["samples"] > 1)

    def _copy_summary(self):
        r = self._selected()
        if not r:
            return
        lines = [self.d_title.text(), self.d_sub.text(), ""]
        lines += [f"{k}: {lab.text()}" for k, (_c, lab) in self.d_vals.items()]
        QtWidgets.QApplication.clipboard().setText("\n".join(lines))
        self.win.flash("Recording summary copied", 3000)

    def _replay_at(self, frac):
        r = self._selected()
        s = self._summaries.get((r["path"], r["size"])) if r else None
        if s and s["samples"]:
            self.win.open_recording(r["path"], start=int(frac * (s["samples"] - 1)))

    def _replay_peak(self):
        r = self._selected()
        s = self._summaries.get((r["path"], r["size"])) if r else None
        if s:
            # start a little before the peak so you see it build up
            self.win.open_recording(r["path"], start=max(0, s["cpu_max_index"] - 5))

    # ---- state -------------------------------------------------------------------
    def _big_clicked(self):
        if self.win.player is not None:
            self.win.stop_replay()
        else:
            self.win.toggle_data_recording()

    def update_state(self, reload=True):
        from . import icons, theme as th
        w = self.win
        red = "#e5383b"
        if w.datarec.active:
            self.big.setIcon(icons.icon("stop", "#ffffff", "#ffffff"))
            self.big.setStyleSheet(f"QPushButton {{ background: {red}; border: none; border-radius: 32px; }}")
            self.big.setToolTip("Stop recording (Ctrl+Shift+R)")
            self.state.setText("Recording…")
            self._live_update()
        elif w.player is not None:
            self.big.setIcon(icons.icon("stop", th.current["text"], th.current["text"]))
            self.big.setStyleSheet(f"QPushButton {{ background: {th.current['header']}; "
                                   f"border: 1px solid {th.current['border']}; border-radius: 32px; }}")
            self.big.setToolTip("Back to live data")
            self.state.setText(f"Replaying {os.path.basename(w.player.path)}")
            self.hint.setText("Summary, Performance, Processes and Users show the recording. Use the bar "
                              "at the bottom to pause, jump around or change the speed. Press the button "
                              "to go back to live data.")
        else:
            self.big.setIcon(icons.icon("record", "#ffffff", "#ffffff"))
            self.big.setStyleSheet(f"QPushButton {{ background: {red}; border: none; border-radius: 32px; }}"
                                   f"QPushButton:hover {{ background: #ff4d50; }}")
            self.big.setToolTip("Start recording (Ctrl+Shift+R)")
            self.state.setText("Not recording")
            self.hint.setText("Press to record what Taskscope sees every second: CPU, memory, disks, "
                              "network, GPUs and every process. Replay it later to see what happened, "
                              "for example what slowed the computer down while you were away. "
                              "Uses about 20 MB per hour; recording continues while Taskscope is minimised.")
        if reload:
            self.reload()

    def _live_update(self):
        w = self.win
        if not w.datarec.active or not self.isVisible():
            return
        try:
            size = os.path.getsize(w.datarec.path)
        except OSError:
            size = 0
        self.hint.setText(f"{fmt.duration(time.time() - w.datarec.started)} so far  ·  {w.datarec.frames:,} "
                          f"samples  ·  {fmt.size(size)}  ·  press the button (or Ctrl+Shift+R) to stop")

    def activate(self):
        self.update_state(reload=False)
        self.reload()

    def refresh(self, snap):
        pass

    def folder(self):
        return self.win.settings.get("data_dir") or datarec.default_dir()

    def reload(self):
        keep = self._selected()
        keep = keep["path"] if keep else None
        recs = datarec.list_recordings(self.folder())
        active = self.win.datarec.path if self.win.datarec.active else None
        for r in recs:
            r["recording"] = r["path"] == active
        self.recs = recs
        self.list.clear()
        for r in recs:
            it = QtWidgets.QListWidgetItem()
            row = _RecRow(self, r)
            it.setSizeHint(row.sizeHint())
            self.list.addItem(it)
            self.list.setItemWidget(it, row)
        self.stack.setCurrentWidget(self.list if recs else self.empty)
        self.detail_box.setVisible(bool(recs))
        idx = next((i for i, r in enumerate(recs) if r["path"] == keep), 0 if recs else -1)
        self.list.setCurrentRow(idx)
        self._show_selected()
        total = sum(r["size"] for r in recs)
        self.footer.setText(f"{len(recs)} recording{'s' if len(recs) != 1 else ''}  ·  {fmt.size(total)}  ·  "
                            f"saved in {self.folder()}")

    # ---- actions -------------------------------------------------------------------
    def replay(self, r):
        if r and not r.get("recording"):
            self.win.open_recording(r["path"])

    def rename(self, r):
        text, ok = QtWidgets.QInputDialog.getText(self, "Rename recording", "Name:",
                                                  text=r["title"] or _when(r["start"]))
        if ok:
            try:
                datarec.set_title(r["path"], text)
            except OSError as e:
                message(self, False, str(e))
            self.reload()

    def delete(self, r):
        if r.get("recording"):
            return
        btn = QtWidgets.QMessageBox.question(
            self, "Delete recording",
            f"Delete “{r['title'] or _when(r['start'])}” ({_length(r['duration'])}, {fmt.size(r['size'])})?")
        if btn != E(QtWidgets.QMessageBox, "StandardButton", "Yes"):
            return
        for path in (r["path"], r["path"] + ".json"):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except OSError as e:
                message(self, False, str(e))
                return
        self.reload()

    def open_folder(self):
        os.makedirs(self.folder(), exist_ok=True)
        launcher_open(self.folder())


# Sidebar layout: (section heading, pages). Settings is opened from the footer.
NAV = [("", [HomePage, PerformancePage, ProcessesPage, SystemInfoPage, StartupPage,
             UsersPage, ServicesPage]),
       ("TOOLS", [PowerPage, ConnectionsPage, AppsPage, DriversPage, DiskSpacePage, RecordingsPage])]
ALL_PAGES = [p for _, ps in NAV for p in ps] + [SettingsPage]
