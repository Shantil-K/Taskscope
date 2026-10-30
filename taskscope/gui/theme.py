"""Colours, fonts and the application style sheet (dark by default)."""

from .qt import E, QtGui, QtWidgets

DARK = {
    "window": "#1f1f1f", "panel": "#191919", "base": "#1b1b1b", "alt": "#262729",
    "header": "#262626", "border": "#2e2e2e", "text": "#e6e6e6", "muted": "#9a9a9a",
    "accent": "#2f80d8", "accent_text": "#ffffff", "hover": "#2a2d31", "nav_sel": "#2b3a4f",
    "heat": (34, 160, 70), "value": "#ffffff", "ok": "#22c55e", "bad": "#ef4444", "warn": "#f0b429",
}
LIGHT = {
    "window": "#f3f3f3", "panel": "#ebebeb", "base": "#ffffff", "alt": "#f0f3f7",
    "header": "#f0f0f0", "border": "#dcdcdc", "text": "#1b1b1b", "muted": "#666666",
    "accent": "#1e6fd0", "accent_text": "#ffffff", "hover": "#e2e6ea", "nav_sel": "#d6e4f5",
    "heat": (60, 180, 90), "value": "#000000", "ok": "#16a34a", "bad": "#dc2626", "warn": "#b7791f",
}


def _pal(window, panel, base, alt, header, border, text, muted, accent, hover, nav_sel, heat,
         value, ok="#22c55e", bad="#ef4444", warn="#f0b429", accent_text="#ffffff"):
    return {"window": window, "panel": panel, "base": base, "alt": alt, "header": header,
            "border": border, "text": text, "muted": muted, "accent": accent,
            "accent_text": accent_text, "hover": hover, "nav_sel": nav_sel, "heat": heat,
            "value": value, "ok": ok, "bad": bad, "warn": warn}


MIDNIGHT = _pal("#0f172a", "#0b1222", "#0d1526", "#142036", "#16213a", "#22304d", "#e2e8f0",
                "#8a9bb8", "#3b82f6", "#1b2a47", "#1e3a66", (34, 170, 90), "#ffffff")
GRAPHITE = _pal("#2a2a2e", "#232327", "#26262a", "#2f2f34", "#303036", "#3c3c43", "#ececf1",
                "#a0a0ab", "#8b5cf6", "#36363d", "#3d3654", (34, 160, 70), "#ffffff")
FOREST = _pal("#13201a", "#0f1914", "#111c16", "#18261e", "#1a2a21", "#253a2e", "#e3efe7",
              "#8fae9b", "#22a06b", "#1d2f25", "#1f3d2e", (214, 150, 30), "#ffffff")
OCEAN = _pal("#0e1d24", "#0b171d", "#0c1a20", "#12252d", "#142a33", "#1f3a45", "#dff3f7",
             "#86aab4", "#0891b2", "#17323c", "#11404d", (34, 170, 90), "#ffffff")
ROSE = _pal("#24181c", "#1d1316", "#201518", "#2b1d22", "#2d1f24", "#3d2a31", "#f6e8ec",
            "#b9949f", "#e11d48", "#33232a", "#4a2330", (34, 160, 80), "#ffffff")
PAPER = _pal("#f5f0e6", "#ece5d6", "#fbf8f1", "#f1ebdf", "#ebe3d3", "#d8cdb8", "#2b2620",
             "#7a6f60", "#b45309", "#e6dcc8", "#f0dcc0", (217, 119, 6), "#000000",
             ok="#15803d", bad="#b91c1c", warn="#a16207")
CONTRAST = _pal("#000000", "#000000", "#000000", "#141414", "#0a0a0a", "#8a8a8a", "#ffffff",
                "#d8d8d8", "#ffd400", "#262626", "#4a3f00", (0, 190, 90), "#ffffff",
                ok="#00ff6a", bad="#ff4d4d", warn="#ffd400", accent_text="#000000")

# key -> (menu label, palette); "system" follows the desktop's light/dark choice.
# Heat cells (CPU/Memory) use a colour that differs from each theme's selection tint.
THEMES = {"dark": ("Dark", DARK), "light": ("Light", LIGHT), "midnight": ("Midnight blue", MIDNIGHT),
          "graphite": ("Graphite", GRAPHITE), "forest": ("Forest", FOREST),
          "ocean": ("Ocean", OCEAN), "rose": ("Rose", ROSE), "paper": ("Paper", PAPER),
          "contrast": ("High contrast", CONTRAST)}
CHOICES = list(THEMES) + ["system"]


def label(key):
    return "Follow system" if key == "system" else THEMES.get(key, ("Dark",))[0]

current = dict(DARK)
_fusion_set = False

FONT_CHOICES = ("Segoe UI Variable Text", "Segoe UI", "Selawik", "Inter", "Noto Sans",
                "Cantarell", "Ubuntu", "DejaVu Sans")


def font_families():
    """Installed font families (Qt 6 static call; Qt 5 needs an instance)."""
    try:
        return list(QtGui.QFontDatabase.families())
    except TypeError:
        return list(QtGui.QFontDatabase().families())


def pick_font():
    """First installed font from FONT_CHOICES (nothing is bundled)."""
    families = set(font_families())
    for f in FONT_CHOICES:
        if f in families:
            return f
    return None


def qss(c):
    return f"""
QMainWindow, QDialog {{ background: {c['window']}; }}
QWidget {{ color: {c['text']}; }}
QToolTip {{ background: {c['header']}; color: {c['text']}; border: 1px solid {c['border']}; padding: 4px; }}
#Nav {{ background: {c['window']}; border: none; }}
#Nav QListWidget {{ background: transparent; border: none; outline: 0; }}
#Nav QListWidget::item {{ padding: 6px 8px; margin: 1px 6px; border-radius: 5px; }}
#Nav QListWidget::item:hover {{ background: {c['hover']}; }}
#Nav QListWidget::item:selected {{ background: {c['nav_sel']}; color: {c['text']}; }}
#NavSection {{ color: {c['accent']}; font-size: 8pt; font-weight: 600; padding: 6px 18px 2px 18px; }}
#PageTitle {{ font-size: 17pt; font-weight: 600; }}
#Header {{ background: {c['window']}; }}
#Footer {{ background: {c['window']}; border-top: 1px solid {c['border']}; }}
#Footer QLabel {{ color: {c['text']}; }}
#Muted, QLabel[muted="true"] {{ color: {c['muted']}; }}
#Details {{ background: {c['window']}; border-top: 1px solid {c['border']}; }}
#Card {{ background: {c['base']}; border: 1px solid {c['border']}; border-radius: 8px; }}
#CardTitle {{ color: {c['muted']}; }}
#CardValue {{ font-size: 18pt; font-weight: 600; }}
#DeviceList::item {{ border-radius: 6px; margin: 1px 4px; }}
#DeviceList::item:hover {{ background: {c['hover']}; }}
#DeviceList::item:selected, #DeviceList::item:selected:active, #DeviceList::item:selected:!active {{
    background: {c['nav_sel']}; color: {c['text']}; }}
#StatCaption {{ color: {c['muted']}; font-size: 9pt; }}
#StatValue {{ color: {c['value']}; font-size: 14pt; font-weight: 600; }}
#DeviceName {{ font-weight: 600; }}
#DeviceValue {{ color: {c['muted']}; }}
#PerfSub {{ color: {c['muted']}; }}
QPushButton, QToolButton {{ background: {c['header']}; border: 1px solid {c['border']};
    border-radius: 5px; padding: 5px 12px; }}
QPushButton:hover, QToolButton:hover {{ background: {c['hover']}; }}
QPushButton:pressed, QToolButton:pressed {{ background: {c['nav_sel']}; }}
QPushButton:disabled, QToolButton:disabled {{ color: {c['muted']}; border-color: {c['border']}; background: {c['panel']}; }}
QPushButton#Primary {{ background: {c['accent']}; color: {c['accent_text']}; border: 1px solid {c['accent']};
    border-radius: 5px; padding: 5px 12px; font-weight: 600; }}
QPushButton#Primary:hover {{ border: 1px solid {c['text']}; }}
QPushButton#Primary[danger="true"] {{ background: {c['bad']}; color: #ffffff; }}
QPushButton#Primary:disabled {{ background: {c['border']}; color: {c['muted']}; }}
QPushButton[seg="true"] {{ padding: 4px 10px; border-radius: 4px; }}
QPushButton[seg="true"]:checked {{ background: {c['nav_sel']}; border-color: {c['accent']}; }}
#DetailCaption {{ color: {c['muted']}; font-size: 9pt; }}
#DetailPane {{ background: {c['base']}; border: 1px solid {c['border']}; border-radius: 8px; }}
QToolButton#Flat {{ background: transparent; border: none; padding: 4px; }}
QToolButton#Flat:hover {{ background: {c['hover']}; }}
QToolButton::menu-indicator {{ subcontrol-position: right bottom; }}
QLineEdit, QComboBox, QSpinBox {{ background: {c['base']}; border: 1px solid {c['border']};
    border-radius: 5px; padding: 5px 8px; selection-background-color: {c['nav_sel']};
    selection-color: {c['text']}; }}
QLineEdit:focus {{ border-bottom: 2px solid {c['accent']}; }}
QComboBox QAbstractItemView {{ background: {c['base']}; border: 1px solid {c['border']};
    selection-background-color: {c['nav_sel']}; selection-color: {c['text']}; }}
QTreeView, QTableView, QListWidget {{ background: {c['base']}; alternate-background-color: {c['alt']};
    border: none; selection-background-color: {c['nav_sel']}; selection-color: {c['text']};
    outline: 0; }}
QTreeView::item, QTableView::item {{ padding: 0 4px; border: none; }}
QTreeView::item:alternate, QTableView::item:alternate {{ background: {c['alt']}; }}
QTreeView::branch:alternate {{ background: {c['alt']}; }}
QTreeView::item:hover {{ background: {c['hover']}; }}
QTreeView[big="true"]::item {{ padding: {c['_pad']}px 4px; }}
QTreeView[big="true"] QHeaderView::section, QTableView[big="true"] QHeaderView::section {{ padding: 9px 6px; font-size: 11pt; }}
QTreeView::item:selected, QTableView::item:selected, QListWidget::item:selected,
QTreeView::item:selected:!active, QTableView::item:selected:!active, QListWidget::item:selected:!active,
QTreeView::branch:selected {{ background: {c['nav_sel']}; color: {c['text']}; }}
QHeaderView {{ background: {c['header']}; border: none; }}
QHeaderView::section {{ background: {c['header']}; color: {c['text']}; border: none;
    border-right: 1px solid {c['border']}; padding: 7px 6px; }}
QHeaderView::section:hover {{ background: {c['hover']}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {c['border']}; border-radius: 4px; min-height: 30px; margin: 2px; }}
QScrollBar::handle:vertical:hover {{ background: {c['muted']}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {c['border']}; border-radius: 4px; min-width: 30px; margin: 2px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
QMenu {{ background: {c['header']}; border: 1px solid {c['border']}; padding: 4px; }}
QMenu::item {{ padding: 6px 24px 6px 12px; border-radius: 4px; }}
QMenu::item:selected {{ background: {c['nav_sel']}; }}
QMenu::separator {{ height: 1px; background: {c['border']}; margin: 4px 8px; }}
QMenuBar {{ background: {c['window']}; }}
QMenuBar::item {{ padding: 4px 8px; background: transparent; }}
QMenuBar::item:selected {{ background: {c['hover']}; border-radius: 4px; }}
QGroupBox {{ border: 1px solid {c['border']}; border-radius: 8px; margin-top: 14px; padding: 10px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {c['muted']}; }}
QTabWidget::pane {{ border: none; border-top: 1px solid {c['border']}; }}
QTabBar::tab {{ background: transparent; color: {c['muted']}; padding: 6px 14px; border: none;
    border-bottom: 2px solid transparent; }}
QTabBar::tab:selected {{ color: {c['text']}; border-bottom: 2px solid {c['accent']}; }}
QTabBar::tab:hover {{ color: {c['text']}; }}
QProgressBar {{ background: {c['header']}; border: none; border-radius: 4px; height: 8px; text-align: center; }}
QProgressBar::chunk {{ background: {c['accent']}; border-radius: 4px; }}
"""


def apply(theme, font_family="", font_size=10.0, accent="", density="normal"):
    """theme: a key of THEMES, or 'system'. font_family "" picks a good
    installed font automatically."""
    global current
    app = QtWidgets.QApplication.instance()
    # Each of these restyles every widget in the window (seconds once all the pages
    # exist), so only call the ones whose value actually changed.
    # (once a style sheet is set, app.style() is a nameless wrapper, so remember it here)
    global _fusion_set
    if not _fusion_set:
        app.setStyle("Fusion")
        _fusion_set = True
    if theme == "system":
        pref = _desktop_prefers_dark()
        if pref is None:
            bg = app.style().standardPalette().color(E(QtGui.QPalette, "ColorRole", "Window"))
            pref = bg.lightness() < 128
        theme = "dark" if pref else "light"
    c = dict(THEMES.get(theme, THEMES["dark"])[1])
    if accent and QtGui.QColor(accent).isValid():       # Theme popup: own accent colour
        c["accent"] = accent
        sel = QtGui.QColor(accent)
        base = QtGui.QColor(c["base"])
        # selection tint: the accent mixed into the background
        mix = lambda a, b, t: int(a + (b - a) * t)        # noqa: E731
        c["nav_sel"] = QtGui.QColor(mix(base.red(), sel.red(), 0.3), mix(base.green(), sel.green(), 0.3),
                                    mix(base.blue(), sel.blue(), 0.3)).name()
    c["_pad"] = {"compact": 0, "comfortable": 5}.get(density, 2)
    current = dict(c)
    pal = QtGui.QPalette()
    roles = {"Window": c["window"], "WindowText": c["text"], "Base": c["base"],
             "AlternateBase": c["alt"], "Text": c["text"], "Button": c["header"],
             "ButtonText": c["text"], "Highlight": c["nav_sel"], "HighlightedText": c["text"],
             "ToolTipBase": c["header"], "ToolTipText": c["text"], "PlaceholderText": c["muted"],
             "Mid": c["border"], "Midlight": c["border"], "Dark": c["panel"]}
    for role, col in roles.items():
        try:
            pal.setColor(E(QtGui.QPalette, "ColorRole", role), QtGui.QColor(col))
        except AttributeError:
            pass
    if pal != app.palette():
        app.setPalette(pal)
    fam = font_family or pick_font()
    f = QtGui.QFont(fam) if fam else QtGui.QFont(app.font())
    f.setPointSizeF(float(font_size or 10))
    if f != app.font():
        app.setFont(f)
    sheet = qss(c) + _branch_qss(c) + _indicator_qss(c)
    if sheet != app.styleSheet():
        app.setStyleSheet(sheet)


def _indicator_qss(c):
    """Check boxes and radio buttons with a visible outline when unticked
    (the default ones nearly vanish on the dark theme)."""
    import os
    d = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "taskscope")
    tick = os.path.join(d, "tick-ffffff.png")
    try:
        os.makedirs(d, exist_ok=True)
        if not os.path.exists(tick):
            from .qt import QtCore, Qt
            img = QtGui.QImage(32, 32, E(QtGui.QImage, "Format", "Format_ARGB32_Premultiplied"))
            img.fill(QtGui.QColor(0, 0, 0, 0))
            p = QtGui.QPainter(img)
            p.setRenderHint(E(QtGui.QPainter, "RenderHint", "Antialiasing"))
            pen = QtGui.QPen(QtGui.QColor("#ffffff"), 4)
            pen.setCapStyle(E(Qt, "PenCapStyle", "RoundCap"))
            pen.setJoinStyle(E(Qt, "PenJoinStyle", "RoundJoin"))
            p.setPen(pen)
            p.drawPolyline(QtGui.QPolygonF([QtCore.QPointF(8, 17), QtCore.QPointF(14, 23),
                                            QtCore.QPointF(25, 10)]))
            p.end()
            img.save(tick, "PNG")
    except OSError:
        tick = ""
    img = f'image: url("{tick}");' if tick else ""
    return f"""
QCheckBox::indicator, QRadioButton::indicator {{ width: 14px; height: 14px; border: 1px solid {c['muted']};
    background: {c['base']}; }}
QCheckBox::indicator {{ border-radius: 3px; }}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {c['accent']}; }}
QCheckBox::indicator:checked {{ background: {c['accent']}; border-color: {c['accent']}; {img} }}
QRadioButton::indicator:checked {{ border: 1px solid {c['accent']};
    background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,
        stop:0 #ffffff, stop:0.38 #ffffff, stop:0.48 {c['accent']}, stop:1 {c['accent']}); }}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{ border-color: {c['border']}; }}
"""


def _branch_qss(c):
    """Expand/collapse arrows for tree views. Giving ::branch a background (for
    the striped rows) stops Qt drawing its own arrows, so draw them here once
    per theme and point the style sheet at the cached images."""
    import os
    from .qt import QtCore, Qt
    d = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "taskscope")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return ""
    paths = {}
    for name, pts in (("closed", ((6.5, 4.5), (10.5, 8), (6.5, 11.5))),
                      ("open", ((4.5, 6.5), (8, 10.5), (11.5, 6.5)))):
        path = os.path.join(d, f"branch-{name}-{c['muted'].lstrip('#')}.png")
        if not os.path.exists(path):
            img = QtGui.QImage(16, 16, E(QtGui.QImage, "Format", "Format_ARGB32_Premultiplied"))
            img.fill(QtGui.QColor(0, 0, 0, 0))
            p = QtGui.QPainter(img)
            p.setRenderHint(E(QtGui.QPainter, "RenderHint", "Antialiasing"))
            pen = QtGui.QPen(QtGui.QColor(c["muted"]), 1.6)
            pen.setCapStyle(E(Qt, "PenCapStyle", "RoundCap"))
            pen.setJoinStyle(E(Qt, "PenJoinStyle", "RoundJoin"))
            p.setPen(pen)
            p.drawPolyline(QtGui.QPolygonF([QtCore.QPointF(x, y) for x, y in pts]))
            p.end()
            if not img.save(path, "PNG"):
                return ""
        paths[name] = path.replace("\\", "/")
    return f"""
QTreeView::branch:has-children:closed {{ image: url("{paths['closed']}"); border-image: none; }}
QTreeView::branch:has-children:open {{ image: url("{paths['open']}"); border-image: none; }}
"""


def _desktop_prefers_dark():
    """True/False from the GTK/freedesktop setting when it can be read cheaply."""
    import os
    for path in (os.path.expanduser("~/.config/gtk-3.0/settings.ini"),
                 os.path.expanduser("~/.config/gtk-4.0/settings.ini")):
        try:
            with open(path) as f:
                for line in f:
                    if "gtk-application-prefer-dark-theme" in line:
                        return line.split("=")[1].strip() in ("1", "true")
                    if line.startswith("gtk-theme-name") and "dark" in line.lower():
                        return True
        except OSError:
            pass
    return None


def heat(frac):
    """Background colour for a heat-map cell; None when the value is ~0."""
    if frac is None or frac <= 0.0005:
        return None
    r, g, b = current["heat"]
    a = int(70 + 185 * min(1.0, frac))
    return QtGui.QColor(r, g, b, a)
