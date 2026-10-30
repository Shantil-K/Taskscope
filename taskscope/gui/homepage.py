"""Home: a one-glance dashboard.

Top row: a SYS box of LED meters (CPU, clock, temperature, GPU), a CPU
overview graph (utilisation, temperature, kernel time) and the busiest
processes. Middle: memory in use. Bottom: system pressure, network, the
system disk and the GPUs.

The page adapts to its size: the process table drops columns (GPU, then PID
and memory) and rows as it narrows or shrinks, headers shorten, and the
bottom row of cards is hidden when there isn't room for it.

Everything is painted directly (no images, no extra widgets per value), so a
refresh costs a few hundred microseconds.
"""

import os
import time
from collections import deque

from .. import fmt
from ..sysinfo.probe import read, read_int
from . import theme
from .qt import E, QtCore, QtGui, QtWidgets, Qt, Signal
from .widgets import tint

P, R = QtCore.QPointF, QtCore.QRectF

CYAN = "#22d3ee"
GREEN = "#22c55e"
RED = "#ef4444"
YELLOW = "#facc15"
BLUE = "#38bdf8"
ORANGE = "#f59e0b"
PURPLE = "#a855f7"
MAGENTA = "#d946ef"
HISTORY = 120

def _col(c, alpha=255):
    q = QtGui.QColor(c)
    q.setAlpha(alpha)
    return q


# ------------------------------------------------------------- building blocks

class TextHeader(QtWidgets.QWidget):
    """A box heading: label on the left, live value on the right (plain text).
    When it gets narrow the label is shortened, the value stays."""

    def __init__(self, text="", value="", color=CYAN, parent=None):
        super().__init__(parent)
        self.text, self.value, self.color = text, value, color
        self.setFixedHeight(26)
        self.setMinimumWidth(1)

    def set(self, text=None, value=None):
        if text is not None:
            self.text = text
        if value is not None:
            self.value = value
        self.update()

    def paintEvent(self, _e):
        p = QtGui.QPainter(self)
        f = QtGui.QFont(self.font())
        f.setPointSizeF(f.pointSizeF() * 1.15)
        f.setBold(True)
        p.setFont(f)
        fm = p.fontMetrics()
        vw = fm.horizontalAdvance(self.value) if hasattr(fm, "horizontalAdvance") else fm.width(self.value)
        mid = E(Qt, "AlignmentFlag", "AlignVCenter")
        p.setPen(QtGui.QColor(theme.current["accent"]))       # follows the theme
        p.drawText(R(0, 0, self.width(), self.height()), E(Qt, "AlignmentFlag", "AlignRight") | mid, self.value)
        p.setPen(QtGui.QColor(theme.current["text"]))
        room = max(0, self.width() - vw - 10)
        p.drawText(R(0, 0, room, self.height()), E(Qt, "AlignmentFlag", "AlignLeft") | mid,
                   fm.elidedText(self.text, E(Qt, "TextElideMode", "ElideRight"), room))
        p.end()


class LedBar(QtWidgets.QWidget):
    """Segmented LED meter. Vertical (fills from the bottom) or horizontal;
    'ramp' colours segments green → yellow → red along the bar."""

    def __init__(self, color, vertical=True, ramp=False, parent=None):
        super().__init__(parent)
        self.color, self.vertical, self.ramp = QtGui.QColor(color), vertical, ramp
        self.frac = 0.0
        if vertical:
            self.setMinimumWidth(16)
        else:
            self.setFixedHeight(12)

    def set(self, frac):
        frac = max(0.0, min(1.0, frac or 0.0))
        if abs(frac - self.frac) > 0.001:
            self.frac = frac
            self.update()

    def _seg_color(self, i, n):
        if not self.ramp:
            return tint(self.color)
        t = i / max(1, n - 1)
        return tint(GREEN if t < 0.55 else YELLOW if t < 0.8 else RED)

    def paintEvent(self, _e):
        p = QtGui.QPainter(self)
        p.setRenderHint(E(QtGui.QPainter, "RenderHint", "Antialiasing"))
        p.setPen(E(Qt, "PenStyle", "NoPen"))
        w, h = self.width(), self.height()
        if self.vertical:
            seg, gap = 6.0, 2.5
            n = max(1, int((h + gap) // (seg + gap)))
            lit = round(self.frac * n)
            for i in range(n):
                y = h - (i + 1) * (seg + gap) + gap
                c = self._seg_color(i, n)
                p.setBrush(c if i < lit else _col(c.name(), 38))
                p.drawRoundedRect(R(1, y, w - 2, seg), 1.5, 1.5)
        else:
            seg, gap = 7.0, 3.0
            n = max(1, int((w + gap) // (seg + gap)))
            lit = round(self.frac * n)
            for i in range(n):
                c = self._seg_color(i, n)
                p.setBrush(c if i < lit else _col(c.name(), 38))
                p.drawRoundedRect(R(i * (seg + gap), 1, seg, h - 2), 1.5, 1.5)
        p.end()


class Elided(QtWidgets.QLabel):
    """A label that shortens its text with … instead of growing the layout."""

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._full = text
        self.setMinimumWidth(10)
        self.setSizePolicy(E(QtWidgets.QSizePolicy, "Policy", "Ignored"),
                           E(QtWidgets.QSizePolicy, "Policy", "Preferred"))
        self.setText(text)

    def setText(self, text):
        self._full = text
        super().setText(self.fontMetrics().elidedText(text, E(Qt, "TextElideMode", "ElideRight"),
                                                      max(10, self.width())))
        self.setToolTip(text)

    def resizeEvent(self, e):
        super().setText(self.fontMetrics().elidedText(self._full, E(Qt, "TextElideMode", "ElideRight"),
                                                      max(10, self.width())))
        super().resizeEvent(e)


def _card():
    f = QtWidgets.QFrame()
    f.setObjectName("Card")
    return f


# ------------------------------------------------------------------- SYS box

class SysBox(QtWidgets.QFrame):
    METERS = (("CPU", GREEN), ("Clock", RED), ("Temp", YELLOW), ("GPU", BLUE))

    def __init__(self):
        super().__init__()
        self.setObjectName("Card")
        self.setFixedWidth(200)
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(6)
        self.head = TextHeader("SYS", "")
        v.addWidget(self.head)
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(4)
        self.bars, self.vals = [], []
        for i, (name, color) in enumerate(self.METERS):
            cap = QtWidgets.QLabel(name)
            cap.setAlignment(E(Qt, "AlignmentFlag", "AlignCenter"))
            cap.setStyleSheet(f"color: {color}; font-size: 9pt;")
            bar = LedBar(color)
            val = QtWidgets.QLabel("")
            val.setAlignment(E(Qt, "AlignmentFlag", "AlignCenter"))
            val.setStyleSheet(f"color: {color}; font-weight: 600; font-size: 9pt;")
            grid.addWidget(cap, 0, i)
            grid.addWidget(bar, 1, i)
            grid.addWidget(val, 2, i)
            self.bars.append(bar)
            self.vals.append(val)
        grid.setRowStretch(1, 1)
        v.addLayout(grid, 1)
        self.max_mhz = (read_int("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq") or 0) / 1000 or None

    def update_from(self, perf):
        c = perf["cpu"]
        mhz = c.get("freq_mhz") or 0
        self.head.set(value=f"{mhz / 1000:.1f} GHz" if mhz else "")
        gpus = perf.get("gpus") or []
        g = next((x for x in gpus if x.get("mem_total")), gpus[0] if gpus else None)
        gu = (g or {}).get("util")
        temp = c.get("temp")
        vals = [(c["total"] / 100, f"{c['total']:.0f}%"),
                (mhz / self.max_mhz if self.max_mhz and mhz else 0, f"{mhz / 1000:.1f}G" if mhz else "–"),
                ((temp or 0) / 100, fmt.temp_short(temp) if temp else "–"),
                ((gu or 0) / 100, f"{gu:.0f}%" if gu is not None else "–")]
        for bar, lab, (frac, text) in zip(self.bars, self.vals, vals):
            bar.set(frac)
            lab.setText(text)


# --------------------------------------------------------------- CPU overview

class _Scrub:
    """Hover and click handling shared by the CPU and memory graphs: hovering
    shows a guide line and the values at that moment; clicking opens the
    history view at that moment (HomePage.set_history); double-click returns
    to live data."""
    owner = None            # the HomePage
    _hover_x = None

    def _rect(self):
        raise NotImplementedError

    def _series(self):
        raise NotImplementedError

    def _x_to_serial(self, x):
        s, r = self._series(), self._rect()
        n, mx = len(s), s.maxlen
        if n < 2 or self.owner is None:
            return None
        i = round((x - r.left()) / max(1.0, r.width()) * (mx - 1)) - (mx - n)
        if i < 0:
            return None                      # left of where the graph starts: no data there
        i = min(n - 1, i)
        return self.owner.serial - (n - 1 - i)

    def _serial_to_x(self, serial):
        s, r = self._series(), self._rect()
        n, mx = len(s), s.maxlen
        if self.owner is None or n < 2:
            return None
        i = n - 1 - (self.owner.serial - serial)
        if i < 0:
            return None
        return r.left() + r.width() * (i + mx - n) / (mx - 1)

    def _draw_marks(self, p):
        r = self._rect()
        if self.owner is not None and self.owner.hist is not None:
            x = self._serial_to_x(self.owner.hist)
            if x is not None:
                p.setPen(QtGui.QPen(QtGui.QColor("#ffffff"), 1, E(Qt, "PenStyle", "DashLine")))
                p.drawLine(P(x, r.top()), P(x, r.bottom()))
        if self._hover_x is not None and r.left() <= self._hover_x <= r.right():
            p.setPen(QtGui.QPen(_col("#ffffff", 90), 1))
            p.drawLine(P(self._hover_x, r.top()), P(self._hover_x, r.bottom()))

    def mouseMoveEvent(self, e):
        pos = e.position() if hasattr(e, "position") else QtCore.QPointF(e.pos())
        self._hover_x = pos.x()
        serial = self._x_to_serial(pos.x())
        f = self.owner.frame(serial) if (self.owner is not None and serial is not None) else None
        gp = e.globalPosition().toPoint() if hasattr(e, "globalPosition") else e.globalPos()
        if f is not None and f.get("gap"):
            QtWidgets.QToolTip.showText(gp, f"No data for {fmt.duration(f['gap'])}: Taskscope wasn't "
                                            "running or the computer was asleep", self)
            self.update()
            return
        if f is not None and (self.owner.hist is not None or e.buttons() & E(Qt, "MouseButton", "LeftButton")):
            # once in history view, the marker follows the mouse over the graph (no need to drag);
            # moving off the graph leaves it where it was
            self.owner.set_history(serial)
        if f is None and self.owner is not None and self._rect().contains(pos):
            QtWidgets.QToolTip.showText(gp, "No data here yet", self)
        if f is not None:
            c, m = f["perf"]["cpu"], f["perf"]["memory"]
            when = self.owner.ago(f)
            txt = (f"{when}\nCPU {c['total']:.1f}%  ·  kernel {c.get('kernel', 0):.1f}%"
                   + (f"  ·  {fmt.temp(c['temp'])}" if c.get("temp") else "")
                   + f"\nMemory {fmt.size(m['used'])} ({m['percent']:.0f}%)"
                   + ("" if self.owner.hist is not None else "\nClick for history"))
            QtWidgets.QToolTip.showText(gp, txt, self)
        self.update()

    def leaveEvent(self, e):
        self._hover_x = None
        self.update()
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        if e.button() == E(Qt, "MouseButton", "LeftButton") and self.owner is not None:
            pos = e.position() if hasattr(e, "position") else QtCore.QPointF(e.pos())
            serial = self._x_to_serial(pos.x())
            f = self.owner.frame(serial) if serial is not None else None
            if f is not None and not f.get("gap"):
                self.owner.set_history(serial)

    def mouseDoubleClickEvent(self, e):
        if self.owner is not None:
            self.owner.set_history(None)


class CpuPlot(_Scrub, QtWidgets.QWidget):
    """Utilisation and kernel time (left axis, %) and temperature (right axis, °C)."""

    def __init__(self, util, kern, temp):
        super().__init__()
        self.util, self.kern, self.temp = util, kern, temp
        self.setMinimumHeight(80)
        self.setMouseTracking(True)
        self.setCursor(E(Qt, "CursorShape", "CrossCursor"))

    def _small_font(self):
        f = QtGui.QFont(self.font())
        f.setPointSizeF(8)
        return f

    def _margins(self):
        """Room for the axis labels, measured in the font they are drawn in."""
        fm = QtGui.QFontMetrics(self._small_font())
        adv = fm.horizontalAdvance if hasattr(fm, "horizontalAdvance") else fm.width
        return (adv("100%") + 8, adv("110°") + 8)

    def _rect(self):
        left, right = self._margins()
        top = 8                                    # half a label above the 100% line
        return R(left, top, max(10, self.width() - left - right), max(10, self.height() - 18 - top))

    def _series(self):
        return self.util

    def paintEvent(self, _e):
        p = QtGui.QPainter(self)
        p.setRenderHint(E(QtGui.QPainter, "RenderHint", "Antialiasing"))
        fm = p.fontMetrics()
        p.setFont(self._small_font())
        left, right = self._margins()
        r = self._rect()
        p.fillRect(r, QtGui.QColor("#0c1b14"))
        p.setPen(QtGui.QPen(_col(GREEN, 40), 1))
        for i in range(1, 10):
            y = r.top() + r.height() * i / 10
            p.drawLine(P(r.left(), y), P(r.right(), y))
        cols = max(4, int(r.width() // 22))
        for i in range(1, cols):
            x = r.left() + r.width() * i / cols
            p.drawLine(P(x, r.top()), P(x, r.bottom()))
        p.setPen(QtGui.QPen(_col(GREEN, 120), 1))
        p.drawRect(r)
        for frac, txt, rtxt in ((0, "100%", "110°"), (0.5, "50%", "55°"), (1, "0%", "0°")):
            y = r.top() + r.height() * frac
            p.setPen(QtGui.QColor(GREEN))
            p.drawText(R(0, y - 7, left - 4, 14), E(Qt, "AlignmentFlag", "AlignRight") | E(Qt, "AlignmentFlag", "AlignVCenter"), txt)
            p.setPen(QtGui.QColor(ORANGE))
            p.drawText(R(r.right() + 4, y - 7, right, 14), E(Qt, "AlignmentFlag", "AlignLeft") | E(Qt, "AlignmentFlag", "AlignVCenter"), rtxt)
        _shade_gaps(p, r, self.util)
        for series, color, scale in ((self.temp, ORANGE, 110.0), (self.kern, PURPLE, 100.0), (self.util, GREEN, 100.0)):
            n, mx = len(series), series.maxlen
            p.setPen(QtGui.QPen(tint(color), 1.4))
            for start, vals in _runs(series):           # a gap (None) breaks the line
                pts = [P(r.left() + r.width() * (start + i + mx - n) / (mx - 1),
                         r.bottom() - r.height() * min(v, scale) / scale) for i, v in enumerate(vals)]
                if len(pts) > 1:
                    p.drawPolyline(QtGui.QPolygonF(pts))
                else:
                    p.drawPoint(pts[0])
        self._draw_marks(p)
        p.setPen(QtGui.QColor(theme.current["muted"]))
        p.drawText(R(r.left(), r.bottom() + 2, r.width(), 16), E(Qt, "AlignmentFlag", "AlignLeft"), "CPU utilization")
        p.end()


class CpuBox(QtWidgets.QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("Card")
        self.util, self.kern, self.temp = (deque(maxlen=HISTORY) for _ in range(3))
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(12, 10, 12, 8)
        v.setSpacing(4)
        self.head = TextHeader("CPU", "")
        v.addWidget(self.head)
        legend_w = QtWidgets.QWidget()
        legend_w.setMinimumWidth(1)                 # narrow box: the legend is clipped, not the box
        legend = QtWidgets.QHBoxLayout(legend_w)
        legend.setContentsMargins(0, 0, 0, 0)
        legend.setSpacing(14)
        from . import icons
        self.legend = []
        for txt, col, glyph in (("Utilization", GREEN, "pulse"), ("Temperature", ORANGE, "thermo"),
                                ("Kernel", PURPLE, "chip")):
            item = QtWidgets.QHBoxLayout()
            item.setSpacing(4)
            ic = QtWidgets.QLabel()
            ic.setPixmap(icons.icon(glyph, tint(col).name()).pixmap(14, 14))
            l = QtWidgets.QLabel(txt)
            l.setStyleSheet(f"color: {tint(col).name()}; font-size: 9pt;")
            l.setMinimumWidth(0)
            item.addWidget(ic)
            item.addWidget(l)
            legend.addLayout(item)
            self.legend.append((ic, l, col, glyph))
        legend.addStretch(1)
        v.addWidget(legend_w)
        self.plot = CpuPlot(self.util, self.kern, self.temp)
        v.addWidget(self.plot, 1)
        foot_w = QtWidgets.QWidget()
        foot_w.setMinimumWidth(1)
        foot = QtWidgets.QHBoxLayout(foot_w)
        foot.setContentsMargins(0, 0, 0, 0)
        self.f_left = Elided("")
        self.f_right = QtWidgets.QLabel("")
        for l in (self.f_left, self.f_right):
            l.setProperty("muted", True)
            l.setStyleSheet("font-size: 9pt;")
        foot.addWidget(self.f_left, 1)
        foot.addWidget(self.f_right)
        v.addWidget(foot_w)

    def push(self, perf):
        if perf is None:                              # a gap: no readings for a while
            for q in (self.util, self.kern, self.temp):
                q.append(None)
            return
        c = perf["cpu"]
        self.util.append(c["total"])
        self.kern.append(c.get("kernel", 0.0))
        self.temp.append(c.get("temp") or 0.0)

    def update_from(self, perf, ago=None):
        from . import icons
        for ic, l, col, glyph in self.legend:       # follow the Colors popup (saturation, greyscale…)
            want = tint(col).name()
            if l.property("tint") != want:
                l.setProperty("tint", want)
                ic.setPixmap(icons.icon(glyph, want).pixmap(14, 14))
                l.setStyleSheet(f"color: {want}; font-size: 9pt;")
        c = perf["cpu"]
        self.head.set(text="CPU" if ago is None else f"CPU history · {ago}", value=f"{c['total']:.2f}%")
        self.f_left.setText(f"{len(c['cores'])} logical processors")
        mhz = c.get("freq_mhz") or 0
        self.f_right.setText(f"Speed {mhz / 1000:.2f} GHz" if mhz >= 1000 else f"Speed {mhz:.0f} MHz")
        self.plot.update()


# ------------------------------------------------------------- top processes

class TopProcs(QtWidgets.QWidget):
    """The busiest processes. As the box narrows, the GPU column goes first,
    then PID and memory; as it shrinks, fewer rows are shown."""
    picked = Signal(int)
    ROW = 19

    def __init__(self):
        super().__init__()
        self.rows = []
        self.setMinimumSize(120, 60)
        self.setMouseTracking(True)
        self._hits = []

    def set_rows(self, rows):
        self.rows = rows
        self.update()

    def capacity(self):
        return max(0, (self.height() - 22) // self.ROW)

    def _columns(self):
        w = self.width()
        cols = []
        if w >= 260:
            cols.append(("PID", 58, "pid", False))
        cols.append(("Name", None, "name", False))
        cols.append(("CPU", 46, "cpu", True))
        if w >= 330:
            cols.append(("GPU", 38, "gpu", True))
        if w >= 230:
            cols.append(("Memory", 70, "mem", True))
        return cols

    def paintEvent(self, _e):
        p = QtGui.QPainter(self)
        f = QtGui.QFont(self.font())
        f.setPointSizeF(8.5)
        p.setFont(f)
        if not self.rows and getattr(self, "empty_note", ""):
            p.setPen(QtGui.QColor(theme.current["muted"]))
            p.drawText(R(8, 8, self.width() - 16, self.height() - 16),
                       int(E(Qt, "AlignmentFlag", "AlignCenter")) | int(E(Qt, "TextFlag", "TextWordWrap")),
                       self.empty_note)
            p.end()
            return
        cols = self._columns()
        fixed = sum(c[1] for c in cols if c[1])
        name_w = max(40, self.width() - fixed - 8)
        xs, x = [], 4
        for title, w, key, right in cols:
            w = w or name_w
            xs.append((x, w, key, right, title))
            x += w
        text = QtGui.QColor(theme.current["text"])
        p.setPen(text)
        for x, w, key, right, title in xs:
            p.drawText(R(x + 4, 0, w - 8, 20), E(Qt, "AlignmentFlag", "AlignLeft") | E(Qt, "AlignmentFlag", "AlignVCenter"), title)
        p.setPen(QtGui.QPen(QtGui.QColor(theme.current["border"]), 1))
        p.drawLine(P(0, 21), P(self.width(), 21))
        self._hits = []
        y = 23
        fm = p.fontMetrics()
        for i, r in enumerate(self.rows[:self.capacity()]):
            heat = min(1.0, r["cpu_v"] / 5.0)        # 5 % of the whole CPU = full colour
            if r["cpu_v"] >= 0.05 and getattr(self, "highlight", True):
                p.fillRect(R(0, y, self.width(), self.ROW - 1), _col(GREEN, int(40 + 150 * heat)))
            elif i % 2:
                p.fillRect(R(0, y, self.width(), self.ROW - 1), QtGui.QColor(theme.current["alt"]))
            p.setPen(text)
            for x, w, key, right, _t in xs:
                v = r.get(key, "")
                s = fm.elidedText(str(v), E(Qt, "TextElideMode", "ElideRight"), int(w - 8))
                al = (E(Qt, "AlignmentFlag", "AlignLeft") if not right else E(Qt, "AlignmentFlag", "AlignLeft")) | \
                    E(Qt, "AlignmentFlag", "AlignVCenter")
                p.drawText(R(x + 4, y, w - 8, self.ROW), al, s)
            self._hits.append((R(0, y, self.width(), self.ROW), r["pid_n"]))
            y += self.ROW
        p.end()

    def mouseDoubleClickEvent(self, e):
        pos = e.position() if hasattr(e, "position") else QtCore.QPointF(e.pos())
        for rect, pid in self._hits:
            if rect.contains(pos):
                self.picked.emit(pid)
                return


class ProcBox(QtWidgets.QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("Card")
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(12, 10, 12, 8)
        v.setSpacing(6)
        self.head = TextHeader("TOP CPU", "")
        v.addWidget(self.head)
        self.table = TopProcs()
        self.table.setToolTip("Double-click a process to open it on the Processes page")
        v.addWidget(self.table, 1)

    def update_from(self, rows, history=False):
        """rows: (pid, name, cpu, rss) busiest first, as stored by HomePage."""
        n = self.table.capacity()
        self.table.highlight = not history          # live data gets the green heat
        self.table.empty_note = ("Processes weren't recorded at this moment. They are read every second "
                                 "while Home, Processes or Users is on screen, and every 5 seconds while "
                                 "Taskscope is minimised. Try a moment nearby." if history else "")
        self.table.set_rows([{"pid": str(pid), "pid_n": pid, "name": name, "cpu": fmt.cpu(cpu),
                              "cpu_v": cpu, "gpu": "—", "mem": fmt.size(rss)}
                             for pid, name, cpu, rss in rows[:max(n, 1)]])
        self.head.set(text=f"TOP {n} CPU" + (" · then" if history else ""))


# ---------------------------------------------------------------- memory

class MemPlot(_Scrub, QtWidgets.QWidget):
    """Memory in use, zoomed to the range it actually moves in."""

    def __init__(self, series):
        super().__init__()
        self.series = series
        self.setMinimumHeight(40)
        self.setMouseTracking(True)
        self.setCursor(E(Qt, "CursorShape", "CrossCursor"))

    def _rect(self):
        return R(0, 0, self.width() - 1, max(10, self.height() - 18))

    def _series(self):
        return self.series

    def paintEvent(self, _e):
        p = QtGui.QPainter(self)
        p.setRenderHint(E(QtGui.QPainter, "RenderHint", "Antialiasing"))
        small = QtGui.QFont(self.font())
        small.setPointSizeF(8)
        p.setFont(small)
        r = R(0, 0, self.width() - 1, max(10, self.height() - 18))
        p.fillRect(r, QtGui.QColor("#160d24"))
        p.setPen(QtGui.QPen(_col(PURPLE, 45), 1))
        for i in range(1, 4):
            y = r.top() + r.height() * i / 4
            p.drawLine(P(r.left(), y), P(r.right(), y))
        cols = max(4, int(r.width() // 22))
        for i in range(1, cols):
            x = r.left() + r.width() * i / cols
            p.drawLine(P(x, r.top()), P(x, r.bottom()))
        p.setPen(QtGui.QPen(_col(PURPLE, 150), 1))
        p.drawRect(r)
        s = self.series
        known = [v for v in s if v is not None]
        if len(s) >= 2 and known:
            _shade_gaps(p, r, s)
            lo, hi = min(known), max(known)
            pad = max((hi - lo) * 0.15, 64 << 20)
            lo, hi = max(0, lo - pad), hi + pad
            p.setPen(QtGui.QPen(tint(MAGENTA), 1.4))
            for start, vals in _runs(s):                 # a gap (None) breaks the line
                pts = [P(r.left() + r.width() * (start + i + s.maxlen - len(s)) / (s.maxlen - 1),
                         r.bottom() - r.height() * (v - lo) / (hi - lo)) for i, v in enumerate(vals)]
                if len(pts) > 1:
                    p.drawPolyline(QtGui.QPolygonF(pts))
                else:
                    p.drawPoint(pts[0])
            p.setPen(QtGui.QColor(MAGENTA))
            p.drawText(R(r.left() + 5, r.top() + 2, 120, 14), E(Qt, "AlignmentFlag", "AlignLeft"), fmt.size(hi))
            p.drawText(R(r.left() + 5, r.bottom() - 16, 120, 14), E(Qt, "AlignmentFlag", "AlignLeft"), fmt.size(lo))
        self._draw_marks(p)
        p.setPen(QtGui.QColor(theme.current["muted"]))
        p.drawText(R(2, r.bottom() + 2, r.width(), 16), E(Qt, "AlignmentFlag", "AlignLeft"), "Physical memory in use")
        p.end()


class MemBox(QtWidgets.QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("Card")
        self.used = deque(maxlen=HISTORY * 2)
        h = QtWidgets.QHBoxLayout(self)
        h.setContentsMargins(14, 10, 14, 10)
        h.setSpacing(14)
        left = QtWidgets.QVBoxLayout()
        self.bar = LedBar(MAGENTA)
        self.bar.setFixedWidth(22)
        self.pct = QtWidgets.QLabel("")
        self.pct.setStyleSheet(f"color: {MAGENTA}; font-weight: 600; font-size: 9pt;")
        left.addWidget(self.bar, 1)
        left.addWidget(self.pct)
        h.addLayout(left)
        right = QtWidgets.QVBoxLayout()
        right.setSpacing(4)
        top = QtWidgets.QHBoxLayout()
        t = QtWidgets.QLabel("Memory Utilization")
        f = t.font()
        f.setBold(True)
        t.setFont(f)
        self.total = QtWidgets.QLabel("")
        self.total.setStyleSheet(f"color: {MAGENTA}; font-size: 11pt;")
        top.addWidget(t)
        top.addStretch(1)
        top.addWidget(self.total)
        right.addLayout(top)
        self.plot = MemPlot(self.used)
        right.addWidget(self.plot, 1)
        self.foot = QtWidgets.QHBoxLayout()
        self.f_avail, self.f_cache, self.f_swap = (QtWidgets.QLabel("") for _ in range(3))
        for l in (self.f_avail, self.f_cache, self.f_swap):
            l.setProperty("muted", True)
            l.setStyleSheet("font-size: 9pt;")
        self.foot.addWidget(self.f_avail)
        self.foot.addStretch(1)
        self.foot.addWidget(self.f_cache)
        self.foot.addStretch(1)
        self.foot.addWidget(self.f_swap)
        right.addLayout(self.foot)
        h.addLayout(right, 1)

    def push(self, perf):
        self.used.append(None if perf is None else perf["memory"]["used"])

    def update_from(self, perf):
        m = perf["memory"]
        self.plot.update()
        self.bar.set(m["percent"] / 100)
        self.pct.setText(f"{m['percent']:.0f}%")
        self.total.setText(f"{fmt.size(m['used'])} / {fmt.size(m['total'])}")
        self.f_avail.setText(f"Available {fmt.size(m['available'])}")
        self.f_cache.setText(f"Cached {fmt.size(m['cached'])}")
        self.f_swap.setText(f"Swap {fmt.size(m['swap_used'])}")
        compact = self.height() < 150                 # small window: keep just the graph
        for l in (self.f_avail, self.f_cache, self.f_swap):
            l.setVisible(not compact)
        self.plot.update()


# ----------------------------------------------------------- bottom cards

class StatCard(QtWidgets.QFrame):
    def __init__(self, title, color, ramp=False):
        super().__init__()
        self.setObjectName("Card")
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(14, 10, 14, 10)
        v.setSpacing(4)
        self.title = Elided(title)
        v.addWidget(self.title)
        row = QtWidgets.QHBoxLayout()
        self.line = Elided("")
        self.side = QtWidgets.QLabel("")
        row.addWidget(self.line, 1)
        row.addWidget(self.side)
        v.addLayout(row)
        bottom = QtWidgets.QHBoxLayout()
        bottom.setSpacing(10)
        self.bar = LedBar(color, vertical=False, ramp=ramp)
        self.value = QtWidgets.QLabel("")
        self.value.setStyleSheet(f"color: {color}; font-weight: 600; font-size: 9pt;")
        self.value.setMinimumWidth(60)
        self.value.setAlignment(E(Qt, "AlignmentFlag", "AlignRight") | E(Qt, "AlignmentFlag", "AlignVCenter"))
        bottom.addWidget(self.bar, 1)
        bottom.addWidget(self.value)
        v.addLayout(bottom)
        self.setFixedHeight(104)


def _psi(kind):
    line = read(f"/proc/pressure/{kind}").splitlines()
    if not line:
        return None
    vals = dict(kv.split("=") for kv in line[0].split()[1:] if "=" in kv)
    try:
        return float(vals.get("avg10", 0))
    except ValueError:
        return None


def _runs(series):
    """(start index, values) for each unbroken stretch of a series with gaps (None)."""
    out, start, cur = [], None, []
    for i, v in enumerate(series):
        if v is None:
            if cur:
                out.append((start, cur))
            start, cur = None, []
        else:
            if start is None:
                start = i
            cur.append(v)
    if cur:
        out.append((start, cur))
    return out


def _shade_gaps(p, r, series):
    """Shade the stretches where there is no data."""
    n, mx = len(series), series.maxlen
    i = 0
    vals = list(series)
    while i < n:
        if vals[i] is None:
            j = i
            while j < n and vals[j] is None:
                j += 1
            x0 = r.left() + r.width() * (i - 0.5 + mx - n) / (mx - 1)
            x1 = r.left() + r.width() * (j - 0.5 + mx - n) / (mx - 1)
            band = R(max(r.left(), x0), r.top(), max(2.0, min(r.right(), x1) - max(r.left(), x0)), r.height())
            p.fillRect(band, QtGui.QColor(255, 255, 255, 18))
            p.save()
            p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 60), 1, E(Qt, "PenStyle", "DotLine")))
            p.drawLine(P(band.left(), r.top()), P(band.left(), r.bottom()))
            p.drawLine(P(band.right(), r.top()), P(band.right(), r.bottom()))
            p.restore()
            i = j
        else:
            i += 1


def history_path():
    """Where Home keeps its recent readings between runs. The administrator
    window is told the user's own path (TASKSCOPE_HISTORY), so both share it."""
    p = os.environ.get("TASKSCOPE_HISTORY")
    if p:
        return p
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "taskscope", "home-history.json.gz")


def _slim(perf):
    """The parts of a reading Home needs, so the saved history stays small."""
    c = perf["cpu"]
    return {"cpu": {k: c.get(k) for k in ("total", "kernel", "temp", "freq_mhz", "cores", "load",
                                          "processes", "threads")},
            "memory": perf["memory"],
            "gpus": [{k: g.get(k) for k in ("util", "temp", "mem_total", "power")} for g in perf.get("gpus") or []]}


# ------------------------------------------------------------------ page

class HomePage(QtWidgets.QWidget):
    title, icon, live = "Home", "home", True

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.net_peak = deque(maxlen=60)
        self.frames = deque(maxlen=HISTORY * 2)   # what each update showed, for the history view
        self.serial = 0                            # number of the newest frame
        self.hist = None                           # frame shown in history view, or None for live
        self._saved = time.monotonic()
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(10)
        self.top = QtWidgets.QHBoxLayout()
        self.top.setSpacing(10)
        self.sys = SysBox()
        self.cpu = CpuBox()
        self.procs = ProcBox()
        self.procs.table.picked.connect(self._open_process)
        self.top.addWidget(self.sys)
        self.top.addWidget(self.cpu, 1)
        self.top.addWidget(self.procs)
        lay.addLayout(self.top, 3)
        self.mem = MemBox()
        lay.addWidget(self.mem, 2)
        self.cpu.plot.owner = self
        self.mem.plot.owner = self
        self._load_history()                       # carry on from the last Taskscope window
        self.bottom = QtWidgets.QWidget()
        bl = QtWidgets.QHBoxLayout(self.bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(10)
        self.c_pressure = StatCard("System Pressure", GREEN, ramp=True)
        self.c_net = StatCard("Network", BLUE)
        self.c_disk = StatCard("Disk", GREEN)
        self.c_gpu = StatCard("GPUs", BLUE)
        for c in (self.c_pressure, self.c_net, self.c_disk, self.c_gpu):
            bl.addWidget(c, 1)
        lay.addWidget(self.bottom)

    def activate(self):
        self.refresh(self.win.snapshot, record=False)

    # ---- history view ----------------------------------------------------------
    def interval(self):
        return max(0.1, self.win.settings.get("interval_ms", 1000) / 1000)

    def frame(self, serial):
        i = len(self.frames) - 1 - (self.serial - serial)
        return self.frames[i] if serial is not None and 0 <= i < len(self.frames) else None

    def ago(self, f):
        """'now' / '0:00:12 ago' for a frame, from the time it was taken."""
        sec = time.time() - f.get("t", time.time())
        return "now" if sec < 0.5 else f"{fmt.duration(sec)} ago"

    # ---- keeping the history across restarts -------------------------------------
    def save_history(self):
        """Write the recent readings so the next Taskscope (or the administrator
        window) carries on from here."""
        import gzip
        import json
        self._saved = time.monotonic()
        path = history_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            text = json.dumps({"v": 1, "frames": [x for x in self.frames if not x.get("gap")]},
                              separators=(",", ":"))
            with open(tmp, "wb") as f:              # one compress call: ~4x faster than streaming
                f.write(gzip.compress(text.encode("utf-8"), 6))
            os.replace(tmp, path)
            if os.geteuid() == 0:                    # administrator window: keep the file the user's
                st = os.stat(os.path.dirname(path))
                if st.st_uid != 0:
                    os.chown(path, st.st_uid, st.st_gid)
        except (OSError, TypeError, ValueError):
            pass

    def _load_history(self):
        import gzip
        import json
        try:
            with gzip.open(history_path(), "rt", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError, EOFError):
            return
        span = self.frames.maxlen * self.interval()
        now = time.time()
        for fr in data.get("frames") or []:
            t = fr.get("t")
            perf = fr.get("perf")
            if not t or not perf or now - t > span or t > now + 5:
                continue                              # too old for the graphs, a gap marker, or a clock jump
            self._gap_until(t)                        # time missing between saved readings
            fr["top"] = [tuple(x) for x in fr.get("top") or []]
            self.frames.append(fr)
            self.cpu.push(perf)
            self.mem.push(perf)
            self.serial += 1
        self._gap_until(now)                          # and between the last saved reading and now

    def _gap_until(self, t):
        """If nothing was recorded for a while before time *t*, add a gap: a
        few empty points that break the graph lines (at most 10, so a long gap
        does not push the earlier history off the graphs)."""
        last = next((f.get("t") or f.get("until") for f in reversed(self.frames)
                     if f.get("t") or f.get("until")), None)
        if last is None:
            return
        missing = t - last
        step = self.interval()
        if missing <= step * 2.5:
            return
        points = int(min(10, max(2, missing / step - 1)))
        for _ in range(points):
            self.frames.append({"gap": missing, "t": None, "until": t, "perf": None, "top": []})
            self.cpu.push(None)
            self.mem.push(None)
            self.serial += 1

    def save_state(self, settings):               # called when the window closes
        self.save_history()

    def set_history(self, serial):
        """Show the moment *serial* (click on a graph), or live data (None)."""
        was = self.hist is not None
        f = self.frame(serial) if serial is not None else None
        if f is not None and f.get("gap"):
            return                                 # nothing to show inside a gap
        self.hist = serial if f is not None else None
        app = QtWidgets.QApplication.instance()
        if self.hist is not None and not was:
            app.installEventFilter(self)           # a click anywhere else returns to live
        elif self.hist is None and was:
            app.removeEventFilter(self)
        self._show()
        self.cpu.plot.update()
        self.mem.plot.update()

    def in_history(self):
        return self.hist is not None

    def eventFilter(self, obj, ev):
        if self.hist is not None and ev.type() == E(QtCore.QEvent, "Type", "MouseButtonPress"):
            w = obj if isinstance(obj, QtWidgets.QWidget) else None
            on_graph = False
            while w is not None:
                if w is self.cpu.plot or w is self.mem.plot:
                    on_graph = True
                    break
                w = w.parentWidget()
            if not on_graph and isinstance(obj, QtWidgets.QWidget):
                QtCore.QTimer.singleShot(0, lambda: self.set_history(None))
        return False

    def hideEvent(self, e):
        self.set_history(None)                     # leaving Home: back to live
        super().hideEvent(e)

    # ---- sizing: what to show at which size ---------------------------------------
    def resizeEvent(self, e):
        super().resizeEvent(e)
        w, h = self.width(), self.height()
        self.bottom.setVisible(h >= 640)          # short window: drop the bottom cards
        self.procs.setVisible(w >= 520)           # very narrow: drop the process list
        self.sys.setVisible(w >= 420)
        # the process list takes about 40 % of what the SYS box leaves
        avail = w - (210 if self.sys.isVisible() else 0)
        self.procs.setFixedWidth(int(max(160, min(430, avail * 0.40))))
        self.mem.setMinimumHeight(110 if h < 560 else 150)

    # ---- data ----------------------------------------------------------------------
    def refresh(self, snap, record=True):
        perf = (snap or {}).get("perf")
        if not perf:
            return
        if record:
            self._gap_until(time.time())
            self.cpu.push(perf)
            self.mem.push(perf)
            # only a freshly read process list belongs to this moment
            procs = (snap.get("procs") or []) if snap.get("procs_fresh", True) else []
            top = sorted(procs, key=lambda p: (p.cpu, p.rss), reverse=True)[:40]
            self.frames.append({"perf": _slim(perf), "top": [(p.pid, p.name, p.cpu, p.rss) for p in top],
                                "t": time.time()})
            self.serial += 1
            if time.monotonic() - self._saved > 30:       # at most half a minute lost on a crash
                self.save_history()
            if self.hist is not None and self.frame(self.hist) is None:
                self.hist = None                   # the moment scrolled off the graphs: back to live
        if not self.isVisible():
            return                                 # history kept, nothing drawn
        self._show()
        if self.bottom.isVisible():
            self._cards(perf)

    def _show(self):
        """Draw the live moment, or the one picked on a graph."""
        f = self.frame(self.hist) if self.hist is not None else (self.frames[-1] if self.frames else None)
        if f is None or f.get("gap"):
            return
        ago = self.ago(f) if self.hist is not None else None
        self.sys.update_from(f["perf"])
        self.cpu.update_from(f["perf"], ago)
        self.mem.update_from(f["perf"])
        if self.procs.isVisible():
            self.procs.update_from(f["top"], history=self.hist is not None)

    def _cards(self, perf):
        c = perf["cpu"]
        ncpu = max(1, len(c["cores"]))
        demand = (c["load"][0] / ncpu * 100) if c.get("load") else 0.0
        mem_stall = _psi("memory") or 0.0
        cpu_stall = _psi("cpu")
        level = max(demand, mem_stall * 5, cpu_stall or 0)
        state = "LOW" if level < 30 else "MODERATE" if level < 70 else "HIGH"
        p = self.c_pressure
        p.line.setText(f"{state} · Load demand {demand:.1f}% · Memory stalls {mem_stall:.1f}%")
        stall = cpu_stall if cpu_stall is not None else c["total"]
        p.bar.set(stall / 100)
        p.value.setText(f"{stall:.1f}%")
        p.setToolTip("Load demand: 1-minute load average per CPU. Stalls: share of time tasks "
                     "waited for CPU or memory (Linux pressure stall information).")

        nets = [n for n in perf["net"] if n["up"] and n["kind"] != "Virtual"]
        rx = sum(n["rx_rate"] for n in nets)
        tx = sum(n["tx_rate"] for n in nets)
        self.net_peak.append(rx + tx)
        scale = max(max(self.net_peak), 64 * 1024)
        n = self.c_net
        n.line.setText(f"R {fmt.rate(rx)} · S {fmt.rate(tx)}")
        n.side.setText(f"Scale {fmt.rate(scale)}")
        n.bar.set((rx + tx) / scale)
        n.value.setText(fmt.rate(rx + tx))

        disks = perf["disks"]
        d = next((x for x in disks if "/" in (x.get("mounts") or [])), disks[0] if disks else None)
        k = self.c_disk
        if d:
            k.title.setText(d["name"] + (f"  ·  {d['model']}" if d.get("model") else ""))
            k.line.setText(f"R {fmt.rate(d['read_rate'])} · W {fmt.rate(d['write_rate'])}")
            k.side.setText(f"{d.get('kind', '')}")
            k.side.setStyleSheet(f"color: {GREEN};")
            k.bar.set(d["busy"] / 100)
            k.value.setText(f"{d['busy']:.1f}%")
        gpus = perf.get("gpus") or []
        g = self.c_gpu
        power = sum(x.get("power") or 0 for x in gpus)
        temps = [x["temp"] for x in gpus if x.get("temp")]
        utils = [x["util"] for x in gpus if x.get("util") is not None]
        g.line.setText((f"GPU Power: {power:.1f} W" if power else "GPU Power: n/a")
                       + (f" · {len(gpus)} GPUs" if len(gpus) > 1 else ""))
        if temps:
            g.bar.set(max(temps) / 100)
            g.value.setText(fmt.temp(max(temps)))
        else:
            g.bar.set((max(utils) if utils else 0) / 100)
            g.value.setText(f"{max(utils):.0f}%" if utils else "–")

    def _open_process(self, pid):
        from . import pages
        self.win.open_page(pages.ProcessesPage)
        pg = self.win.page_of(pages.ProcessesPage)
        if pg is not None:
            ix = pg.model.index_of(pid)
            if ix.isValid():
                pg.view.setCurrentIndex(ix)
                pg.view.scrollTo(ix)
