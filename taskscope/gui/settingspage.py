"""Settings: sections of cards (Appearance, Graphs, General, Window, Data
and tools, Experimental, About). Every change applies at once and is saved;
there is no OK button."""

import os

from .. import APP_NAME, __version__, settings as settings_mod
from . import theme as theme_mod
from .qt import E, QtCore, QtGui, QtWidgets, Qt, version_string
from .widgets import message


class Toggle(QtWidgets.QAbstractButton):
    """An on/off switch with its label beside it."""

    def __init__(self, text=""):
        super().__init__()
        self.setCheckable(True)
        self.setText(text)
        self.setCursor(E(Qt, "CursorShape", "PointingHandCursor"))
        self.setSizePolicy(E(QtWidgets.QSizePolicy, "Policy", "Preferred"),
                           E(QtWidgets.QSizePolicy, "Policy", "Fixed"))

    def sizeHint(self):
        fm = self.fontMetrics()
        w = fm.horizontalAdvance(self.text()) if hasattr(fm, "horizontalAdvance") else fm.width(self.text())
        return QtCore.QSize(44 + 10 + w, max(24, fm.height() + 6))

    def paintEvent(self, _e):
        p = QtGui.QPainter(self)
        p.setRenderHint(E(QtGui.QPainter, "RenderHint", "Antialiasing"))
        c = theme_mod.current
        on = self.isChecked()
        track = QtCore.QRectF(1, (self.height() - 20) / 2, 40, 20)
        p.setPen(QtGui.QPen(QtGui.QColor(c["accent"] if on else c["muted"]), 1))
        p.setBrush(QtGui.QColor(c["accent"]) if on else QtGui.QColor(c["base"]))
        p.drawRoundedRect(track, 10, 10)
        knob = QtCore.QRectF(track.right() - 17 if on else track.left() + 3, track.top() + 3, 14, 14)
        p.setPen(E(Qt, "PenStyle", "NoPen"))
        p.setBrush(QtGui.QColor("#ffffff") if on else QtGui.QColor(c["muted"]))
        p.drawEllipse(knob)
        p.setPen(QtGui.QColor(c["text"] if self.isEnabled() else c["muted"]))
        p.drawText(QtCore.QRectF(track.right() + 10, 0, self.width(), self.height()),
                   E(Qt, "AlignmentFlag", "AlignLeft") | E(Qt, "AlignmentFlag", "AlignVCenter"), self.text())
        p.end()


class ActionButton(QtWidgets.QPushButton):
    """A button that mirrors a window action: its text (e.g. Start/Stop
    recording), whether it is on or available, and its shortcut as a tooltip."""

    def __init__(self, action):
        super().__init__()
        self.action_ = action
        self.setCheckable(action.isCheckable())
        self.clicked.connect(lambda *_: action.trigger())
        action.changed.connect(self._sync)
        if action.isCheckable():
            action.toggled.connect(self._sync)
        self._sync()

    def _sync(self, *_):
        a = self.action_
        self.setText(a.text())
        self.setEnabled(a.isEnabled())
        if a.isCheckable():
            self.setChecked(a.isChecked())
        keys = a.shortcut().toString()
        self.setToolTip(f"Shortcut: {keys}" if keys else "")


def _section(title):
    """A heading and a card; returns (heading widget, card, card layout)."""
    head = QtWidgets.QLabel(title)
    f = head.font()
    f.setBold(True)
    f.setPointSizeF(f.pointSizeF() * 1.1)
    head.setFont(f)
    card = QtWidgets.QFrame()
    card.setObjectName("Card")
    lay = QtWidgets.QGridLayout(card)
    lay.setContentsMargins(16, 12, 16, 12)
    lay.setHorizontalSpacing(16)
    lay.setVerticalSpacing(8)
    lay.setColumnStretch(2, 1)
    return head, card, lay


class SettingsPage(QtWidgets.QWidget):
    title, icon, live = "Settings", "settings", False

    def __init__(self, win):
        super().__init__()
        self.win = win
        self._loading = True
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(E(QtWidgets.QFrame, "Shape", "NoFrame"))
        inner = QtWidgets.QWidget()
        scroll.setWidget(inner)
        outer.addWidget(scroll)
        col = QtWidgets.QVBoxLayout(inner)
        col.setContentsMargins(16, 12, 16, 16)
        col.setSpacing(8)
        self.col = col
        self.w = {}

        # ---- Actions: what used to be the File, View and Help menus
        lay = self._add("Actions")
        for title, acts in win.command_groups:
            r = lay.rowCount()
            lay.addWidget(QtWidgets.QLabel(title), r, 0)
            row = QtWidgets.QHBoxLayout()
            row.setSpacing(6)
            for a in acts:
                row.addWidget(ActionButton(a))
            row.addStretch(1)
            lay.addLayout(row, r, 1, 1, 2)

        # ---- Appearance
        lay = self._add("Appearance")
        self.combo(lay, "theme", "Theme", [(theme_mod.label(k), k) for k in theme_mod.CHOICES])
        fonts = [("Automatic (best installed)", "")] + [(f, f) for f in sorted(set(theme_mod.font_families()))
                                                      if not f.startswith(".")]
        self.combo(lay, "font_family", "Application font", fonts)
        self.spin(lay, "font_size", "Font size", 8, 16, " pt")
        self.combo(lay, "temp_unit", "Temperature", [("Celsius (°C)", "C"), ("Fahrenheit (°F)", "F")])
        self.toggle(lay, "mini_monitor", "Mini monitor",
                    "A small always-on-top window with CPU, memory, network and GPU graphs. "
                    "Drag it anywhere; double-click it to open Taskscope; right-click to hide it.")
        self.slider(lay, "mini_opacity", "Mini monitor opacity", 20, 100)
        self.toggle(lay, "mini_hover_opaque", "Fully opaque while pointing at the mini monitor")

        # ---- Graphs
        lay = self._add("Graphs")
        self.toggle(lay, "graph_keyed", "Colour-keyed graphs",
                    "Each kind of device keeps its own colour (CPU blue, memory purple, …). "
                    "Off: every graph uses the theme's accent colour.")
        self.toggle(lay, "graph_dashed", "Dashed line for the second series (e.g. network send)")
        self.toggle(lay, "graph_smooth", "Smooth graph curves")
        self.combo(lay, "graph_points", "History shown", [("1 minute", 60), ("2 minutes", 120),
                                                           ("5 minutes", 300), ("10 minutes", 600)],
                   note="How far back the graphs reach (at the normal update speed). Longer "
                        "histories use a little more memory.")

        # ---- General
        lay = self._add("General")
        self.combo(lay, "interval_ms", "Update speed", [("Fast: twice a second", 500),
                                                         ("Normal: every second", 1000),
                                                         ("Slow: every 2 seconds", 2000),
                                                         ("Very slow: every 5 seconds", 5000)])
        pages = [("Last page used", "last")] + [(p.title, p.title) for p in self.win.pages
                                                 if p.title != "Settings"]
        self.combo(lay, "start_page", "Start on", pages)
        self.toggle(lay, "show_self", "Show Taskscope in the process list")
        self.toggle(lay, "cpu_per_core", "Per-core CPU figures for processes",
                    "Show process CPU in one-core units: one busy core is 100 %, so a program using "
                    f"all {os.cpu_count()} logical processors shows {os.cpu_count() * 100} %. "
                    "Off: share of the whole CPU (never above 100 %).")
        self.toggle(lay, "confirm_kill", "Ask before End task and Kill")

        # ---- Window
        lay = self._add("Window")
        self.toggle(lay, "always_on_top", "Always on top",
                    "Keeps the window above other windows. Some desktops decide this themselves "
                    "and may ignore the request.")
        from .extras import TrayIcon
        tray_ok = TrayIcon.available()
        t = self.toggle(lay, "tray_icon", "Live icon in the notification area",
                        "Shows CPU and memory as two small bars next to the clock. Click it to show "
                        "or hide Taskscope." + ("" if tray_ok else
                                               " This desktop has no notification area, so it is not available."))
        t.setEnabled(tray_ok)
        self.toggle(lay, "close_to_tray", "Closing the window keeps Taskscope in the notification area",
                    "Quit from the icon's menu (or Exit Taskscope in Actions above, Ctrl+Q) to end it. "
                    "Needs the live icon.")
        self.toggle(lay, "autostart", "Start Taskscope when you log in")
        self.toggle(lay, "fit_pages", "Free window sizing (fit the window to each page)",
                    "Off (default): the window keeps one minimum size, large enough for "
                    "Performance > Overview, so it never changes size when you switch pages; a page "
                    "that needs more room scrolls. On: the window can be made as small as the page on "
                    "screen allows, and grows when you open a page that needs more room.")

        # ---- Data and tools
        lay = self._add("Data and tools")
        self.folder(lay, "data_dir", "Recordings folder", "Default: ~/.local/share/taskscope/recordings")
        self.toggle(lay, "screensaver_audio", "Screensaver reacts to sound (needs parec or pw-record)")
        self.toggle(lay, "remote_enabled", "Remote summary page",
                    "A read-only web page with live figures, protected by a token.")
        self.combo(lay, "remote_host", "Remote page listens on", [("This computer only (127.0.0.1)", "127.0.0.1"),
                                                                  ("Whole network (0.0.0.0)", "0.0.0.0")])
        self.spin(lay, "remote_port", "Remote page port", 1024, 65535, "")
        r = lay.rowCount()
        lab = QtWidgets.QLabel("Address")
        self.rurl = QtWidgets.QLineEdit()
        self.rurl.setReadOnly(True)
        newtok = QtWidgets.QPushButton("New access token")
        newtok.clicked.connect(self.new_token)
        lay.addWidget(lab, r, 0)
        lay.addWidget(self.rurl, r, 1, 1, 2)
        lay.addWidget(newtok, r + 1, 1)

        # ---- Experimental
        lay = self._add("Experimental")
        self.toggle(lay, "exp_ext4_scan", "Fast ext4 disk scan",
                    "Disk Space reads ext2/3/4 file systems straight from the drive instead of checking "
                    "every file. Only used when running as administrator; read-only, and it falls back "
                    "to the normal scan if anything looks unusual. About 1.2× faster on a 1.2 million-file "
                    "home folder.")

        # ---- About
        lay = self._add("About")
        about = QtWidgets.QLabel(
            f"<b>{APP_NAME} {__version__}</b><br>A lightweight task manager for Linux.<br>"
            f"Free software under the GNU GPL v3 or later.<br>{version_string()}")
        about.setTextInteractionFlags(E(Qt, "TextInteractionFlag", "TextSelectableByMouse"))
        lay.addWidget(about, 0, 0, 1, 3)
        info = QtWidgets.QLabel(win.backend_summary())
        info.setWordWrap(True)
        info.setProperty("muted", True)
        info.setTextInteractionFlags(E(Qt, "TextInteractionFlag", "TextSelectableByMouse"))
        lay.addWidget(info, 1, 0, 1, 3)
        row = QtWidgets.QHBoxLayout()
        b1 = QtWidgets.QPushButton("Open settings folder")
        b1.clicked.connect(self.open_folder)
        b2 = QtWidgets.QPushButton("Reset all settings…")
        b2.clicked.connect(self.reset)
        row.addWidget(b1)
        row.addWidget(b2)
        row.addStretch(1)
        lay.addLayout(row, 2, 0, 1, 3)
        col.addStretch(1)
        self._loading = False
        self.update_url()

    # ---- building rows ----------------------------------------------------------------
    def _add(self, title):
        head, card, lay = _section(title)
        self.col.addSpacing(6)
        self.col.addWidget(head)
        self.col.addWidget(card)
        return lay

    def _note(self, lay, text):
        n = QtWidgets.QLabel(text)
        n.setWordWrap(True)
        n.setProperty("muted", True)
        lay.addWidget(n, lay.rowCount(), 0, 1, 3)

    def combo(self, lay, key, label, items, note=""):
        cb = QtWidgets.QComboBox()
        for text, data in items:
            cb.addItem(text, data)
        i = cb.findData(self.win.settings.get(key))
        cb.setCurrentIndex(max(0, i))
        cb.setMinimumWidth(240)
        cb.currentIndexChanged.connect(lambda *_: self._set(key, cb.currentData()))
        r = lay.rowCount()
        lay.addWidget(QtWidgets.QLabel(label), r, 0)
        lay.addWidget(cb, r, 1)
        if note:
            self._note(lay, note)
        self.w[key] = cb
        return cb

    def spin(self, lay, key, label, lo, hi, suffix):
        sp = QtWidgets.QSpinBox()
        sp.setRange(lo, hi)
        sp.setSuffix(suffix)
        sp.setValue(int(self.win.settings.get(key) or lo))
        sp.setMinimumWidth(120)
        sp.valueChanged.connect(lambda v: self._set(key, v))
        r = lay.rowCount()
        lay.addWidget(QtWidgets.QLabel(label), r, 0)
        lay.addWidget(sp, r, 1, E(Qt, "AlignmentFlag", "AlignLeft"))
        self.w[key] = sp
        return sp

    def slider(self, lay, key, label, lo, hi):
        box = QtWidgets.QHBoxLayout()
        sl = QtWidgets.QSlider(E(Qt, "Orientation", "Horizontal"))
        sl.setRange(lo, hi)
        sl.setValue(int(self.win.settings.get(key) or hi))
        sl.setMinimumWidth(200)
        val = QtWidgets.QLabel(f"{sl.value()}%")
        sl.valueChanged.connect(lambda v: (val.setText(f"{v}%"), self._set(key, v)))
        box.addWidget(sl)
        box.addWidget(val)
        r = lay.rowCount()
        lay.addWidget(QtWidgets.QLabel(label), r, 0)
        lay.addLayout(box, r, 1)
        self.w[key] = sl
        return sl

    def toggle(self, lay, key, label, note=""):
        t = Toggle(label)
        t.setChecked(bool(self.win.settings.get(key)))
        t.toggled.connect(lambda on: self._set(key, on))
        lay.addWidget(t, lay.rowCount(), 0, 1, 3)
        if note:
            self._note(lay, note)
        self.w[key] = t
        return t

    def folder(self, lay, key, label, placeholder):
        ed = QtWidgets.QLineEdit(self.win.settings.get(key) or "")
        ed.setPlaceholderText(placeholder)
        ed.editingFinished.connect(lambda: self._set(key, ed.text().strip()))
        btn = QtWidgets.QPushButton("Browse…")

        def browse():
            d = QtWidgets.QFileDialog.getExistingDirectory(self, label, ed.text() or os.path.expanduser("~"))
            if d:
                ed.setText(d)
                self._set(key, d)
        btn.clicked.connect(browse)
        r = lay.rowCount()
        lay.addWidget(QtWidgets.QLabel(label), r, 0)
        lay.addWidget(ed, r, 1, 1, 1)
        lay.addWidget(btn, r, 2, E(Qt, "AlignmentFlag", "AlignLeft"))
        self.w[key] = ed

    # ---- applying -----------------------------------------------------------------------
    def _set(self, key, value):
        if self._loading:
            return
        self.win.settings[key] = value
        if key == "close_to_tray" and value and not self.win.settings.get("tray_icon"):
            self.win.settings["tray_icon"] = True        # needs the icon to come back from
            self.w["tray_icon"].setChecked(True)
        self.win.apply_settings()
        self.update_url()

    def activate(self):
        """Show values changed elsewhere (menus, the Theme button, the mini monitor)."""
        self._loading = True
        s = self.win.settings
        for key, wdg in self.w.items():
            v = s.get(key)
            if isinstance(wdg, Toggle):
                wdg.setChecked(bool(v))
            elif isinstance(wdg, QtWidgets.QComboBox):
                wdg.setCurrentIndex(max(0, wdg.findData(v)))
            elif isinstance(wdg, (QtWidgets.QSpinBox, QtWidgets.QSlider)) and v is not None:
                wdg.setValue(int(v))
            elif isinstance(wdg, QtWidgets.QLineEdit):
                wdg.setText(v or "")
        self._loading = False
        self.update_url()

    def refresh(self, snap):
        pass

    def sync_theme(self):
        self.activate()

    def new_token(self):
        self.win.settings["remote_token"] = ""
        self.win.apply_settings()
        self.update_url()

    def update_url(self):
        self.rurl.setText(self.win.remote.url if self.win.remote else "Off")

    def open_folder(self):
        os.makedirs(settings_mod.CONFIG_DIR, exist_ok=True)
        from .. import launcher
        launcher.open_path(settings_mod.CONFIG_DIR)

    def reset(self):
        btn = QtWidgets.QMessageBox.question(
            self, "Reset all settings",
            "Put every setting back to its default? Window size, column layouts and the theme are reset too.")
        if btn != E(QtWidgets.QMessageBox, "StandardButton", "Yes"):
            return
        keep = {k: self.win.settings.get(k) for k in ("geometry",)}
        self.win.settings.clear()
        self.win.settings.update(settings_mod.DEFAULTS)
        self.win.settings.update(keep)
        self.win.apply_settings()
        self.activate()
        message(self, True, "All settings are back to their defaults.")
