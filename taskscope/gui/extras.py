"""Window extras switched on in Settings: a live icon in the notification
area, a small always-on-top mini monitor, and starting Taskscope at login.
"""

import os
import shutil
import sys

from .. import APP_NAME, fmt
from . import icons, theme
from .qt import E, QtCore, QtGui, QtWidgets, Qt, exec_
from .widgets import COLORS, Graph


# ------------------------------------------------------- notification area

class TrayIcon(QtCore.QObject):
    """Notification-area icon drawn from live data: two bars, CPU and memory.
    Click to show or hide the window; right-click for a menu."""

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.icon = QtWidgets.QSystemTrayIcon(icons.app_icon(), win)
        m = QtWidgets.QMenu()
        m.addAction("Show Taskscope", self.show_window)
        self.mini_action = m.addAction("Mini monitor", self._toggle_mini)
        self.mini_action.setCheckable(True)
        m.addSeparator()
        m.addAction("Quit", self._quit)
        self._menu = m
        self.icon.setContextMenu(m)
        self.icon.activated.connect(self._activated)
        self.icon.show()
        self._last = None

    @staticmethod
    def available():
        return QtWidgets.QSystemTrayIcon.isSystemTrayAvailable()

    def _activated(self, reason):
        if reason == E(QtWidgets.QSystemTrayIcon, "ActivationReason", "Trigger"):
            if self.win.isVisible() and not self.win.isMinimized():
                self.win.hide()
            else:
                self.show_window()

    def show_window(self):
        self.win.showNormal()
        self.win.raise_()
        self.win.activateWindow()

    def _toggle_mini(self):
        self.win.settings["mini_monitor"] = not self.win.settings.get("mini_monitor")
        self.win.apply_settings()

    def _quit(self):
        self.win.quit_for_real()

    def update(self, perf):
        cpu, mem = perf["cpu"]["total"], perf["memory"]["percent"]
        key = (round(cpu), round(mem))
        self.mini_action.setChecked(bool(self.win.settings.get("mini_monitor")))
        if key != self._last:                       # redraw only when it would look different
            self._last = key
            self.icon.setIcon(_meter_icon(cpu, mem))
        self.icon.setToolTip(f"{APP_NAME}\nCPU {cpu:.0f}%   Memory {mem:.0f}%")

    def close(self):
        self.icon.hide()
        self.icon.deleteLater()


def _meter_icon(cpu, mem):
    ic = QtGui.QIcon()
    for size in (16, 22, 24, 32, 48):
        pm = QtGui.QPixmap(size, size)
        pm.fill(QtGui.QColor(0, 0, 0, 0))
        p = QtGui.QPainter(pm)
        p.setRenderHint(E(QtGui.QPainter, "RenderHint", "Antialiasing"))
        p.setPen(E(Qt, "PenStyle", "NoPen"))
        p.setBrush(QtGui.QColor(20, 24, 32, 230))
        p.drawRoundedRect(QtCore.QRectF(0, 0, size, size), size * 0.18, size * 0.18)
        w = size * 0.32
        gap = size * 0.12
        x0 = (size - 2 * w - gap) / 2
        for i, (v, col) in enumerate(((cpu, COLORS["cpu"]), (mem, COLORS["memory"]))):
            x = x0 + i * (w + gap)
            top, bottom = size * 0.12, size * 0.88
            h = (bottom - top) * max(0.06, min(1.0, v / 100))
            dim = QtGui.QColor(col)
            dim.setAlpha(60)
            p.setBrush(dim)
            p.drawRoundedRect(QtCore.QRectF(x, top, w, bottom - top), 1.5, 1.5)
            p.setBrush(QtGui.QColor(col))
            p.drawRoundedRect(QtCore.QRectF(x, bottom - h, w, h), 1.5, 1.5)
        p.end()
        ic.addPixmap(pm)
    return ic


# --------------------------------------------------------------- mini monitor

class MiniMonitor(QtWidgets.QWidget):
    """A small floating window with CPU, memory, network and GPU graphs.
    Drag it anywhere; right-click for options; double-click opens Taskscope."""
    ROWS = (("cpu", "CPU", "cpu"), ("mem", "Memory", "memory"), ("net", "Network", "net"),
            ("gpu", "GPU", "gpu"))

    def __init__(self, win):
        flags = (E(Qt, "WindowType", "Tool") | E(Qt, "WindowType", "FramelessWindowHint")
                 | E(Qt, "WindowType", "WindowStaysOnTopHint"))
        super().__init__(None, flags)
        self.win = win
        self.setWindowTitle(f"{APP_NAME} mini monitor")
        self.setObjectName("MiniMonitor")
        self.setAttribute(E(Qt, "WidgetAttribute", "WA_StyledBackground"))
        self.setStyleSheet(f"#MiniMonitor {{ background: {theme.current['panel']}; "
                           f"border: 1px solid {theme.current['border']}; border-radius: 8px; }}")
        lay = QtWidgets.QGridLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setHorizontalSpacing(8)
        lay.setVerticalSpacing(4)
        self.vals, self.graphs = {}, {}
        for r, (key, title, col) in enumerate(self.ROWS):
            t = QtWidgets.QLabel(title)
            t.setStyleSheet(f"color: {COLORS[col]}; font-size: 9pt;")
            v = QtWidgets.QLabel("")
            v.setStyleSheet("font-size: 9pt; font-weight: 600;")
            v.setAlignment(E(Qt, "AlignmentFlag", "AlignRight") | E(Qt, "AlignmentFlag", "AlignVCenter"))
            g = Graph(COLORS[col], maximum=None if key == "net" else 100.0)
            g.setMinimumHeight(0)
            g.setFixedSize(110, 26)
            lay.addWidget(t, r, 0)
            lay.addWidget(v, r, 1)
            lay.addWidget(g, r, 2)
            self.vals[key], self.graphs[key] = v, g
        self.setFixedSize(self.sizeHint())
        self._drag = None
        self._apply_opacity(hover=False)
        pos = win.settings.get("mini_pos")
        if pos:
            self.move(*pos)
        else:
            geo = QtWidgets.QApplication.primaryScreen().availableGeometry()
            self.move(geo.right() - self.width() - 24, geo.top() + 60)

    def update_from(self, perf):
        c, m = perf["cpu"], perf["memory"]
        nets = [n for n in perf["net"] if n["up"] and n["kind"] != "Virtual"]
        rate = sum(n["rx_rate"] + n["tx_rate"] for n in nets)
        gpus = perf.get("gpus") or []
        gu = max([g["util"] or 0 for g in gpus] or [0])
        for key, val, text in (("cpu", c["total"], f"{c['total']:.0f}%"),
                               ("mem", m["percent"], f"{m['percent']:.0f}%"),
                               ("net", rate, fmt.rate(rate)),
                               ("gpu", gu, f"{gu:.0f}%" if gpus else "–")):
            self.graphs[key].push(val)
            self.vals[key].setText(text)

    # ---- opacity, dragging, menu ----------------------------------------------------
    def _apply_opacity(self, hover):
        s = self.win.settings
        op = 1.0 if (hover and s.get("mini_hover_opaque", True)) else max(0.2, s.get("mini_opacity", 90) / 100)
        self.setWindowOpacity(op)

    def enterEvent(self, e):
        self._apply_opacity(True)
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._apply_opacity(False)
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        if e.button() == E(Qt, "MouseButton", "LeftButton"):
            gp = e.globalPosition().toPoint() if hasattr(e, "globalPosition") else e.globalPos()
            self._drag = gp - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            gp = e.globalPosition().toPoint() if hasattr(e, "globalPosition") else e.globalPos()
            self.move(gp - self._drag)

    def mouseReleaseEvent(self, e):
        if self._drag is not None:
            self._drag = None
            self.win.settings["mini_pos"] = [self.x(), self.y()]

    def mouseDoubleClickEvent(self, e):
        self.win.showNormal()
        self.win.raise_()
        self.win.activateWindow()

    def contextMenuEvent(self, e):
        m = QtWidgets.QMenu(self)
        m.addAction("Open Taskscope", lambda: self.mouseDoubleClickEvent(None))
        m.addAction("Hide mini monitor", self._hide)
        exec_(m, e.globalPos())

    def _hide(self):
        self.win.settings["mini_monitor"] = False
        self.win.apply_settings()


# ------------------------------------------------------------- start at login

def autostart_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "autostart", "taskscope.desktop")


def set_autostart(on):
    """Create or remove the XDG autostart entry. Returns an error text or ''."""
    path = autostart_path()
    try:
        if not on:
            if os.path.exists(path):
                os.remove(path)
            return ""
        launcher = shutil.which("taskscope")
        if launcher:
            cmd = launcher
        else:                                    # running from the source folder
            pkg_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            cmd = f'env PYTHONPATH="{pkg_root}" {sys.executable} -m taskscope'
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("[Desktop Entry]\nType=Application\nName=Taskscope\n"
                    "Comment=Task manager and system monitor\n"
                    f"Exec={cmd}\nIcon=taskscope\nTerminal=false\n"
                    "X-GNOME-Autostart-enabled=true\n")
        return ""
    except OSError as e:
        return str(e)
