"""Live-load screensaver drawn with QPainter (no OpenGL requirement).

Rings stream outward from the centre; each ring is split into four arcs whose
brightness and thickness follow CPU, memory, GPU and disk load. Optional audio
reactivity reads the default output's monitor through parec or pw-record.

Can run full screen, or inside a window handed over by XScreenSaver
(-root / XSCREENSAVER_WINDOW, or -window-id ID).
"""

import math
import os
import random
import struct
import subprocess
import sys
import threading

from ..sysinfo import perf, probe
from .qt import E, QtCore, QtGui, QtWidgets, Qt
from .widgets import COLORS

METRICS = ("cpu", "memory", "gpu", "disk")


class AudioLevel:
    """Background reader of the system's output level, 0..1."""

    def __init__(self):
        self.level = 0.0
        self.proc = None
        cmd = None
        if probe.which("parec"):
            mon = "@DEFAULT_MONITOR@"
            cmd = ["parec", "-d", mon, "--raw", "--format=s16le", "--channels=1", "--rate=8000"]
        elif probe.which("pw-record"):
            cmd = ["pw-record", "--target", "0", "--format", "s16", "--channels", "1",
                   "--rate", "8000", "-"]
        if not cmd:
            return
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            return
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        n = 400  # 50 ms of audio
        while self.proc and self.proc.poll() is None:
            buf = self.proc.stdout.read(n * 2)
            if not buf:
                break
            vals = struct.unpack(f"<{len(buf) // 2}h", buf[: len(buf) // 2 * 2])
            rms = math.sqrt(sum(v * v for v in vals) / max(1, len(vals))) / 32768.0
            self.level = self.level * 0.6 + min(1.0, rms * 4) * 0.4

    def stop(self):
        if self.proc:
            self.proc.kill()
            self.proc = None


class Screensaver(QtWidgets.QWidget):
    def __init__(self, source=None, audio=False, on_close=None, embedded=False):
        """source: callable returning the latest perf snapshot, or None to
        sample on our own (standalone mode)."""
        super().__init__()
        self.setWindowTitle("Taskscope screensaver")
        self.setAttribute(E(Qt, "WidgetAttribute", "WA_DeleteOnClose"))
        self.setAttribute(E(Qt, "WidgetAttribute", "WA_OpaquePaintEvent"))
        self.setCursor(E(Qt, "CursorShape", "BlankCursor"))
        self.setMouseTracking(True)
        self.on_close = on_close
        self.embedded = embedded
        self._own = None
        if source is None:
            self._own = perf.PerfSampler()
            self._own.sample()
            self._snap = self._own.sample()
            source = lambda: self._snap  # noqa: E731
            t = QtCore.QTimer(self)
            t.timeout.connect(self._sample_own)
            t.start(1000)
        self.source = source
        self.load = {m: 0.0 for m in METRICS}
        self.target = dict(self.load)
        self.rings = [random.random() for _ in range(26)]
        self.phase = 0.0
        self.audio = AudioLevel() if audio else None
        self.mouse_start = None
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._frame)
        self.timer.start(33)

    def _sample_own(self):
        self._snap = self._own.sample()

    def _read_targets(self):
        s = self.source() or {}
        if not s:
            return
        g = [x["util"] for x in s.get("gpus", []) if x.get("util") is not None]
        d = [x["busy"] for x in s.get("disks", [])]
        self.target = {"cpu": s["cpu"]["total"] / 100, "memory": s["memory"]["percent"] / 100,
                       "gpu": (max(g) if g else 0) / 100, "disk": (max(d) if d else 0) / 100}

    def _frame(self):
        self._read_targets()
        for m in METRICS:
            self.load[m] += (self.target[m] - self.load[m]) * 0.08
        a = self.audio.level if self.audio else 0.0
        speed = 0.004 + 0.012 * self.load["cpu"] + 0.02 * a
        self.rings = [(r + speed * (0.4 + r)) % 1.0 for r in self.rings]
        self.phase += 0.003 + 0.01 * self.load["gpu"]
        self.update()

    def paintEvent(self, _e):
        p = QtGui.QPainter(self)
        p.setRenderHint(E(QtGui.QPainter, "RenderHint", "Antialiasing"))
        p.fillRect(self.rect(), QtGui.QColor(4, 6, 12))
        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2
        maxr = math.hypot(cx, cy)
        a = self.audio.level if self.audio else 0.0
        for r in sorted(self.rings):
            rad = (r ** 2.2) * maxr
            if rad < 2:
                continue
            for i, m in enumerate(METRICS):
                col = QtGui.QColor(COLORS[m])
                v = self.load[m]
                col.setAlphaF(max(0.05, min(1.0, (0.15 + 0.85 * v) * min(1.0, r * 2.5))))
                pen = QtGui.QPen(col, 1 + 7 * v * r + 4 * a * r)
                pen.setCapStyle(E(Qt, "PenCapStyle", "RoundCap"))
                p.setPen(pen)
                start = (i * 90 + self.phase * 57.3 + 6) * 16
                p.drawArc(QtCore.QRectF(cx - rad, cy - rad * 0.62, rad * 2, rad * 1.24),
                          int(start), int(78 * 16))
        p.setPen(QtGui.QColor(200, 210, 230, 160))
        f = p.font()
        f.setPointSize(11)
        p.setFont(f)
        txt = "   ".join(f"{m.upper()} {self.load[m] * 100:3.0f}%" for m in METRICS)
        p.drawText(self.rect().adjusted(0, 0, 0, -24),
                   E(Qt, "AlignmentFlag", "AlignHCenter") | E(Qt, "AlignmentFlag", "AlignBottom"), txt)
        p.end()

    # Dismiss on any input when we own the screen.
    def _dismiss(self):
        if not self.embedded:
            self.close()

    def keyPressEvent(self, _e):
        self._dismiss()

    def mousePressEvent(self, _e):
        self._dismiss()

    def mouseMoveEvent(self, e):
        pos = e.position() if hasattr(e, "position") else e.pos()
        if self.mouse_start is None:
            self.mouse_start = pos
        elif abs(pos.x() - self.mouse_start.x()) + abs(pos.y() - self.mouse_start.y()) > 12:
            self._dismiss()

    def closeEvent(self, e):
        self.timer.stop()
        if self.audio:
            self.audio.stop()
        if self.on_close:
            self.on_close()
        super().closeEvent(e)


def run_standalone(argv):
    """Entry for `taskscope --screensaver [-root | -window-id ID] [--audio]`."""
    win_id = None
    if "--configure" in argv:
        print("Taskscope screensaver: run `taskscope --register-screensaver` to add it to\n"
              "XScreenSaver, or press F12 in Taskscope. Idle time and locking are your\n"
              "desktop's own settings.")
        return 0
    if "-root" in argv:
        env = os.environ.get("XSCREENSAVER_WINDOW")
        if not env:
            print("-root needs a screensaver manager (XSCREENSAVER_WINDOW is not set)", file=sys.stderr)
            return 2
        win_id = int(env, 0)
    if "-window-id" in argv:
        try:
            win_id = int(argv[argv.index("-window-id") + 1], 0)
        except (IndexError, ValueError):
            print("-window-id needs a window id", file=sys.stderr)
            return 2
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    saver = Screensaver(audio="--audio" in argv, embedded=win_id is not None)
    if win_id is not None:
        foreign = QtGui.QWindow.fromWinId(win_id)
        if foreign is None:
            return 2
        saver.winId()
        saver.windowHandle().setParent(foreign)
        saver.setGeometry(foreign.geometry().adjusted(0, 0, 0, 0))
        saver.move(0, 0)
        saver.show()
    else:
        saver.showFullScreen()
    from .qt import exec_
    return exec_(app)
