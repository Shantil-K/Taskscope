"""The Colors popup behind the sidebar's Theme button: theme, accent colour,
how graphs are coloured, saturation, glow and row density. Every change
shows at once; click outside the popup to close it."""

from . import theme
from .qt import E, QtCore, QtGui, QtWidgets, Qt

ACCENTS = ["#2f80d8", "#7c3aed", "#db2777", "#dc2626", "#ea580c", "#ca8a04", "#16a34a", "#0891b2"]


class Swatch(QtWidgets.QAbstractButton):
    def __init__(self, color, tip):
        super().__init__()
        self.color = color
        self.setCheckable(True)
        self.setFixedSize(24, 24)
        self.setToolTip(tip)
        self.setCursor(E(Qt, "CursorShape", "PointingHandCursor"))

    def paintEvent(self, _e):
        p = QtGui.QPainter(self)
        p.setRenderHint(E(QtGui.QPainter, "RenderHint", "Antialiasing"))
        r = QtCore.QRectF(3, 3, 18, 18)
        if self.color:
            p.setPen(E(Qt, "PenStyle", "NoPen"))
            p.setBrush(QtGui.QColor(self.color))
            p.drawEllipse(r)
        else:                                    # "theme default": half and half
            p.setPen(QtGui.QPen(QtGui.QColor(theme.current["muted"]), 1.2))
            p.setBrush(E(Qt, "BrushStyle", "NoBrush"))
            p.drawEllipse(r)
            p.drawLine(QtCore.QPointF(6, 18), QtCore.QPointF(18, 6))
        if self.isChecked():
            p.setPen(QtGui.QPen(QtGui.QColor(theme.current["text"]), 2))
            p.setBrush(E(Qt, "BrushStyle", "NoBrush"))
            p.drawEllipse(QtCore.QRectF(1, 1, 22, 22))
        p.end()


class ThemePopup(QtWidgets.QFrame):
    def __init__(self, win):
        super().__init__(win, E(Qt, "WindowType", "Popup"))
        self.win = win
        self.setObjectName("ThemePopup")
        self.setAttribute(E(Qt, "WidgetAttribute", "WA_DeleteOnClose"))
        self._paint_bg()
        lay = QtWidgets.QGridLayout(self)
        lay.setContentsMargins(16, 12, 16, 14)
        lay.setHorizontalSpacing(14)
        lay.setVerticalSpacing(10)
        s = win.settings
        head = QtWidgets.QLabel("Colors")
        f = head.font()
        f.setBold(True)
        f.setPointSizeF(f.pointSizeF() * 1.1)
        head.setFont(f)
        lay.addWidget(head, 0, 0, 1, 2)
        r = 1

        def row(label, widget):
            nonlocal r
            lay.addWidget(QtWidgets.QLabel(label), r, 0)
            lay.addWidget(widget, r, 1)
            r += 1

        self.themes = QtWidgets.QComboBox()
        for k in theme.CHOICES:
            self.themes.addItem(theme.label(k), k)
        self.themes.setCurrentIndex(max(0, self.themes.findData(s.get("theme"))))
        self.themes.currentIndexChanged.connect(lambda *_: self._set("theme", self.themes.currentData()))
        row("App theme", self.themes)

        # accent colour: theme default, presets, custom
        sw = QtWidgets.QHBoxLayout()
        sw.setSpacing(2)
        self.group = QtWidgets.QButtonGroup(self)
        self.group.setExclusive(True)
        cur = s.get("accent", "") or ""
        for col in [""] + ACCENTS:
            b = Swatch(col, "Theme's own accent" if not col else col)
            b.setChecked(col.lower() == cur.lower())
            b.clicked.connect(lambda _=False, c=col: self._set("accent", c))
            self.group.addButton(b)
            sw.addWidget(b)
        custom = QtWidgets.QToolButton()
        custom.setText("…")
        custom.setToolTip("Pick any colour")
        custom.clicked.connect(self._custom)
        sw.addWidget(custom)
        sw.addStretch(1)
        holder = QtWidgets.QWidget()
        holder.setLayout(sw)
        row("Accent", holder)

        self.display = QtWidgets.QComboBox()
        for text, key in (("Colour (each device its own)", "color"), ("Accent colour only", "accent"),
                          ("Greyscale", "mono")):
            self.display.addItem(text, key)
        self.display.setCurrentIndex(max(0, self.display.findData(s.get("color_display", "color"))))
        self.display.currentIndexChanged.connect(lambda *_: self._set("color_display", self.display.currentData()))
        row("Graphs", self.display)

        self.sat = self._slider(lay, r, "Saturation", "color_saturation", 11)
        r += 2
        self.glow = self._slider(lay, r, "Glow", "color_glow", 0)
        r += 2

        self.density = QtWidgets.QComboBox()
        for text, key in (("Compact", "compact"), ("Normal", "normal"), ("Comfortable", "comfortable")):
            self.density.addItem(text, key)
        self.density.setCurrentIndex(max(0, self.density.findData(s.get("density", "normal"))))
        self.density.currentIndexChanged.connect(lambda *_: self._set("density", self.density.currentData()))
        row("Row density", self.density)

        more = QtWidgets.QPushButton("More settings…")
        more.setFlat(True)
        more.setCursor(E(Qt, "CursorShape", "PointingHandCursor"))
        more.clicked.connect(self._more)
        reset = QtWidgets.QPushButton("Reset colours")
        reset.clicked.connect(self._reset)
        foot = QtWidgets.QHBoxLayout()
        foot.addWidget(reset)
        foot.addStretch(1)
        foot.addWidget(more)
        lay.addLayout(foot, r, 0, 1, 2)
        self.setMinimumWidth(380)

    def _paint_bg(self):
        c = theme.current
        self.setStyleSheet(f"#ThemePopup {{ background: {c['panel']}; border: 1px solid {c['border']}; "
                           f"border-radius: 8px; }}")

    def _slider(self, lay, r, label, key, default):
        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel(label))
        top.addStretch(1)
        val = QtWidgets.QLabel()
        top.addWidget(val)
        sl = QtWidgets.QSlider(E(Qt, "Orientation", "Horizontal"))
        sl.setRange(0, 11)
        v = int(self.win.settings.get(key, default))
        sl.setValue(v)
        val.setText(f"{v} / 11")

        def changed(x):
            val.setText(f"{x} / 11")
            self._set(key, x)
        sl.valueChanged.connect(changed)
        lay.addLayout(top, r, 0, 1, 2)
        lay.addWidget(sl, r + 1, 0, 1, 2)
        return sl

    def _set(self, key, value):
        self.win.settings[key] = value
        self.win.apply_settings()
        self._paint_bg()
        for p in self.win.pages:
            if hasattr(p, "sync_theme"):
                p.sync_theme()

    def _custom(self):
        start = QtGui.QColor(self.win.settings.get("accent") or theme.current["accent"])
        self.hide()                              # the colour dialog would close a popup anyway
        col = QtWidgets.QColorDialog.getColor(start, self.win, "Accent colour")
        if col.isValid():
            self._set("accent", col.name())
        self.close()

    def _reset(self):
        for k, v in (("accent", ""), ("color_display", "color"), ("color_saturation", 11),
                     ("color_glow", 0), ("density", "normal")):
            self.win.settings[k] = v
        self.win.apply_settings()
        self.close()

    def _more(self):
        self.close()
        from . import pages
        self.win.open_page(pages.SettingsPage)
