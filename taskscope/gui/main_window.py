"""Main window: sidebar navigation, live sampling loop, menus and status bar.

Low-overhead rules:
* One QTimer drives everything; nothing polls on its own.
* While the window is hidden or minimised, sampling stops entirely (unless the
  remote summary server is on, which needs fresh readings).
* The process list (the most expensive reading) is only sampled while a page
  that shows it is open, or the remote server is on.
* Lazy pages (Apps, Services, Drivers, Startup) load once, in a worker thread,
  when first opened.
"""

import os
import time

from .. import APP_NAME, __version__, fmt, settings as settings_mod
from ..sysinfo import datarec, elevate, packages, perf, probe, procs, remote, services
from . import icons, pages, theme
from .qt import E, QAction, QShortcut, QtCore, QtGui, QtNetwork, QtWidgets, Qt, exec_, version_string
from .widgets import USER, message, run_async


class Badge(QtWidgets.QFrame):
    """Pill-shaped status badge: icon, text and an optional info icon, each a
    separate widget so they all centre on the same line."""

    def __init__(self, on_click):
        super().__init__()
        self.on_click = on_click
        self.setObjectName("Badge")
        self.setCursor(E(Qt, "CursorShape", "PointingHandCursor"))
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(10, 3, 10, 3)
        lay.setSpacing(6)
        center = E(Qt, "AlignmentFlag", "AlignVCenter")
        self.icon = QtWidgets.QLabel()
        self.text = QtWidgets.QLabel()
        self.info = QtWidgets.QLabel()
        for w in (self.icon, self.text, self.info):
            w.setAlignment(center | E(Qt, "AlignmentFlag", "AlignHCenter"))
            lay.addWidget(w, 0, center)

    def set_state(self, color_key, text, filled, info, tip):
        col = theme.current[color_key]
        self.setStyleSheet(
            f"#Badge {{ border: 1px solid {col}; border-radius: 11px; background: transparent; }}"
            f"#Badge:hover {{ background: {theme.current['hover']}; }}"
            f"#Badge QLabel {{ color: {col}; font-weight: 600; background: transparent; }}")
        self.icon.setPixmap(icons.icon("shield", col, col if filled else None).pixmap(15, 15))
        self.text.setText(text)
        self.info.setVisible(info)
        if info:
            self.info.setPixmap(icons.icon("info", col).pixmap(15, 15))
        self.setToolTip(tip)

    def mousePressEvent(self, e):
        if e.button() == E(Qt, "MouseButton", "LeftButton"):
            self.on_click()


class _CurrentStack(QtWidgets.QStackedWidget):
    """A page stack whose minimum is the same whichever page is shown (the
    largest any page needs), so switching pages never resizes the window.
    Its preferred size follows the page on screen."""

    def __init__(self):
        super().__init__()
        self.currentChanged.connect(lambda *_: self.updateGeometry())

    def add_page(self, page):
        """Add a page inside a scroll area: a page that needs more room than
        the window has scrolls, instead of enlarging the window."""
        sa = QtWidgets.QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(E(QtWidgets.QFrame, "Shape", "NoFrame"))
        sa.setStyleSheet("QScrollArea { background: transparent; }")
        sa.viewport().setAutoFillBackground(False)
        sa.setWidget(page)
        sa.page = page
        self.addWidget(sa)

    def currentWidget(self):
        w = super().currentWidget()
        return getattr(w, "page", w)

    def sizeHint(self):
        w = self.currentWidget()
        return w.sizeHint() if w is not None else super().sizeHint()

    min_page = None                          # page whose minimum sets the window's minimum
    fit_pages = False                        # Settings: grow the window to fit each page instead

    def minimumSizeHint(self):
        if self.count() and hasattr(super().currentWidget(), "page") and self.fit_pages:
            m = self.currentWidget().minimumSizeHint()      # old behaviour: what this page needs
            return QtCore.QSize(max(200, m.width()), max(150, m.height()))
        if self.count() and hasattr(super().currentWidget(), "page"):
            # page area: room for Performance > Overview with every card in full;
            # other pages scroll if they need more
            m = self.min_page.minimumSizeHint() if self.min_page is not None else QtCore.QSize(0, 0)
            return QtCore.QSize(max(200, m.width()), max(150, m.height()))
        w = self.currentWidget()             # header toolbars: what the current one needs
        return w.minimumSizeHint() if w is not None else super().minimumSizeHint()


def _overflow_bar(tb):
    """Put a page's toolbar controls in a QToolBar, which tucks the ones that
    don't fit into a » menu instead of widening the window."""
    bar = QtWidgets.QToolBar()
    bar.setMovable(False)
    bar.setFloatable(False)
    bar.setObjectName("PageToolBar")
    bar.setStyleSheet("#PageToolBar { background: transparent; border: none; spacing: 6px; padding: 0; }")
    lay = tb.layout() if isinstance(tb, QtWidgets.QWidget) else None
    widgets = []
    if lay is not None and not isinstance(tb, (QtWidgets.QAbstractButton, QtWidgets.QLineEdit)):
        for i in range(lay.count()):
            w = lay.itemAt(i).widget()
            if w is not None:
                widgets.append(w)
    if not widgets:
        widgets = [tb]
    for w in widgets:
        hidden = w.isHidden() and not w.isWindow() and w.testAttribute(E(Qt, "WidgetAttribute", "WA_WState_ExplicitShowHide"))
        act = bar.addWidget(w)
        w._tb_action = act              # show/hide toolbar controls through their action
        if hidden:
            act.setVisible(False)
    bar.setIconSize(QtCore.QSize(16, 16))
    bar.setContentsMargins(0, 0, 0, 0)
    bar.layout().setContentsMargins(0, 0, 0, 0)
    bar.setFixedHeight(36)                  # same on every page: the header never changes height
    return bar


REC_TIP = ("Record Taskscope's data (performance and processes) to replay it later "
           "(Ctrl+Shift+R). Recordings are listed on the Recordings page.")


def server_name():
    return f"taskscope-{os.getuid()}" + ("-root" if os.geteuid() == 0 else "")


class _NavDelegate(QtWidgets.QStyledItemDelegate):
    """Sidebar rows. Collapsed, each row is just its icon: the row's highlight
    is drawn as usual and the icon goes in the exact middle (Qt would keep it
    at the left, where the text used to start)."""
    collapsed = False

    def paint(self, painter, option, index):
        if not self.collapsed:
            return super().paint(painter, option, index)
        opt = QtWidgets.QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        icon = index.data(E(Qt, "ItemDataRole", "DecorationRole"))
        opt.icon = QtGui.QIcon()
        opt.features &= ~E(QtWidgets.QStyleOptionViewItem, "ViewItemFeature", "HasDecoration")
        opt.text = ""
        view = self.parent()
        style = view.style() if view is not None else QtWidgets.QApplication.style()
        style.drawControl(E(QtWidgets.QStyle, "ControlElement", "CE_ItemViewItem"), opt, painter, view)
        if isinstance(icon, QtGui.QIcon) and not icon.isNull():
            size = view.iconSize() if view is not None else QtCore.QSize(18, 18)
            r = QtCore.QRect(QtCore.QPoint(0, 0), size)
            r.moveCenter(option.rect.center())
            icon.paint(painter, r)

    def sizeHint(self, option, index):
        hint = super().sizeHint(option, index)
        if self.collapsed and self.parent() is not None:
            hint.setWidth(self.parent().viewport().width())     # never wider than the bar
        return hint


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        self.proc_sampler = procs.ProcessSampler()
        self.perf_sampler = perf.PerfSampler()
        self.snapshot = {"procs": [], "perf": None}
        self.datarec = datarec.Recorder()            # data recording (footer button)
        self.player = None                           # a recording being replayed
        self.sample_stats = {}                       # timings of the last update (System Info)
        self.replay_pos = 0
        self.replay_playing = False
        self.replay_speed = 1.0
        self.remote = None
        self.saver = None
        self.tray = None                             # notification-area icon (Settings)
        self.mini = None                             # mini monitor window (Settings)
        self._quitting = False

        title = APP_NAME + (" (Administrator)" if probe.is_root() else "")
        self.setWindowTitle(title)
        self.setWindowIcon(icons.app_icon())
        theme.apply(settings["theme"], settings.get("font_family", ""), settings.get("font_size", 10),
                    settings.get("accent", ""), settings.get("density", "normal"))

        central = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        body = QtWidgets.QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        outer.addLayout(body, 1)
        self.setCentralWidget(central)

        # ---- sidebar -------------------------------------------------
        self.side = QtWidgets.QFrame()
        self.side.setObjectName("Nav")
        side = QtWidgets.QVBoxLayout(self.side)
        side.setContentsMargins(0, 8, 0, 8)
        side.setSpacing(0)
        self.nav = QtWidgets.QListWidget()
        self.nav.setIconSize(QtCore.QSize(18, 18))
        self.nav.setFrameShape(E(QtWidgets.QFrame, "Shape", "NoFrame"))
        self.nav.setHorizontalScrollBarPolicy(E(Qt, "ScrollBarPolicy", "ScrollBarAlwaysOff"))
        self.nav.setItemDelegate(_NavDelegate(self.nav))
        side.addWidget(self.nav, 1)
        # the logo and the name: two labels, centred on one line
        self.logo = QtWidgets.QWidget()
        logo_row = QtWidgets.QHBoxLayout(self.logo)
        logo_row.setContentsMargins(18, 8, 8, 8)
        logo_row.setSpacing(10)
        logo_row.addStretch(0)                            # grows when collapsed, to centre the mark
        self.logo_mark = QtWidgets.QLabel()
        self.logo_mark.setFixedSize(28, 28)
        self.logo_name = QtWidgets.QLabel("Taskscope")
        nf = self.logo_name.font()
        nf.setPointSizeF(15)
        nf.setWeight(E(QtGui.QFont, "Weight", "DemiBold"))
        self.logo_name.setFont(nf)
        vc = E(Qt, "AlignmentFlag", "AlignVCenter")
        logo_row.addWidget(self.logo_mark, 0, vc)
        logo_row.addWidget(self.logo_name, 0, vc)
        logo_row.addStretch(1)
        self._logo_row = logo_row
        side.addWidget(self.logo)
        foot = QtWidgets.QHBoxLayout()
        foot.setContentsMargins(10, 4, 10, 0)
        foot.addStretch(0)                          # grows when collapsed, to centre the buttons
        self._foot = foot
        self.settings_btn = self._flat_button("settings", "Settings", self.toggle_settings)
        self.theme_btn = self._flat_button("palette", "Theme", self.cycle_theme)
        foot.addWidget(self.settings_btn)
        foot.addWidget(self.theme_btn)
        foot.addStretch(1)
        side.addLayout(foot)
        body.addWidget(self.side)

        # ---- right side: header + pages ------------------------------
        right = QtWidgets.QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        header = QtWidgets.QWidget()
        header.setObjectName("Header")
        hl = QtWidgets.QHBoxLayout(header)
        hl.setContentsMargins(12, 10, 12, 10)
        self.menu_btn = self._flat_button("menu", "", self.toggle_nav)
        self.menu_btn.setToolTip("Collapse or expand the sidebar")
        hl.addWidget(self.menu_btn)
        self.page_title = QtWidgets.QLabel()
        self.page_title.setObjectName("PageTitle")
        hl.addWidget(self.page_title)
        hl.addStretch(1)
        self.tools = _CurrentStack()
        self.tools.setFixedHeight(36)            # the header is the same height on every page
        hl.addWidget(self.tools)
        self.refresh_btn = QtWidgets.QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh_all)
        hl.addWidget(self.refresh_btn)
        right.addWidget(header)
        self.stack = _CurrentStack()
        right.addWidget(self.stack, 1)
        body.addLayout(right, 1)

        # ---- pages ---------------------------------------------------
        self.pages = []
        self.nav_items = []
        for section, classes in pages.NAV:
            if section:
                it = QtWidgets.QListWidgetItem(section)
                it.setFlags(E(Qt, "ItemFlag", "NoItemFlags"))
                f = it.font()
                f.setPointSizeF(f.pointSizeF() * 0.8)
                f.setBold(True)
                it.setFont(f)
                it.setData(USER, -1)
                self.nav.addItem(it)
                self._section_item = it
            for cls in classes:
                self._add_page(cls, nav=True)
        self._menus()                              # before Settings, which shows them as buttons
        self._add_page(pages.SettingsPage, nav=False)
        self.stack.min_page = next((p for p in self.pages if isinstance(p, pages.PerformancePage)), None)
        if self.stack.min_page is not None:
            # Build Performance > Overview's cards now (one per device), so the
            # window's minimum size is right from the start, not after the first update.
            self.snapshot = {"procs": [], "perf": self.perf_sampler.sample()}
            self.stack.min_page.refresh(self.snapshot)
        self.nav.currentItemChanged.connect(self._nav_changed)

        # ---- footer --------------------------------------------------
        footer = QtWidgets.QFrame()
        footer.setObjectName("Footer")
        fl = QtWidgets.QHBoxLayout(footer)
        fl.setContentsMargins(14, 6, 14, 6)
        self.priv = Badge(self.explain_privileges)
        fl.addWidget(self.priv, 0, E(Qt, "AlignmentFlag", "AlignVCenter"))
        fl.addSpacing(8)
        self.dot = QtWidgets.QLabel("●")
        fl.addWidget(self.dot)
        self.status_cpu = QtWidgets.QLabel()
        fl.addWidget(self.status_cpu, 1)
        self.status_msg = QtWidgets.QLabel()
        self.status_msg.setProperty("muted", True)
        fl.addWidget(self.status_msg)
        self.status_remote = QtWidgets.QLabel()
        fl.addWidget(self.status_remote)
        self._flash_timer = QtCore.QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(lambda: self.status_msg.setText(""))
        self.rec_btn = self._flat_button("record", "", self.toggle_data_recording, fill="#e5383b", color="#e5383b")
        self.rec_btn.setToolTip(REC_TIP)
        fl.addWidget(self.rec_btn)
        self.status_rec = QtWidgets.QLabel("00:00:00")
        self.status_rec.setFont(QtGui.QFontDatabase.systemFont(E(QtGui.QFontDatabase, "SystemFont", "FixedFont")))
        self.status_rec.setToolTip(REC_TIP)
        fl.addWidget(self.status_rec)
        outer.addWidget(self._replay_bar())
        outer.addWidget(footer)
        self._update_dot(True)

        self._shortcuts()
        self.set_nav_collapsed(settings.get("nav_collapsed", False))

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.apply_settings(initial=True)
        geo = settings.get("geometry")
        if geo:
            try:
                self.restoreGeometry(QtCore.QByteArray.fromHex(geo.encode()))
            except (TypeError, ValueError):
                self.resize(1000, 680)
        else:
            self.resize(1000, 680)
        start = settings.get("start_page", "last")
        titles = [p.title for p in self.pages]
        if start != "last" and start in titles:
            self.open_index(titles.index(start))
        else:
            self.open_index(min(settings.get("last_page", 0), len(self.pages) - 1))
        self.tick()

    # ---------------------------------------------------------- layout --
    def _flat_button(self, icon_name, text, slot, fill=None, color=None):
        b = QtWidgets.QToolButton()
        b.setObjectName("Flat")
        b.setIcon(icons.icon(icon_name, color or theme.current["text"], fill))
        b.setIconSize(QtCore.QSize(18, 18))
        if text:
            b.setText(text)
            b.setToolButtonStyle(E(Qt, "ToolButtonStyle", "ToolButtonTextBesideIcon"))
        b.clicked.connect(lambda *_: slot())
        b._icon_name = icon_name
        return b

    def _add_page(self, cls, nav):
        page = cls(self)
        idx = len(self.pages)
        self.pages.append(page)
        self.stack.add_page(page)
        holder = QtWidgets.QWidget()      # keeps each page's toolbar right-aligned
        hl = QtWidgets.QHBoxLayout(holder)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.addStretch(1)
        tb = getattr(page, "toolbar", None)
        if tb is not None:
            hl.addWidget(_overflow_bar(tb))
        self.tools.addWidget(holder)
        if nav:
            it = QtWidgets.QListWidgetItem(icons.icon(cls.icon, theme.current["text"]), cls.title)
            it.setData(USER, idx)
            it.setSizeHint(QtCore.QSize(150, 36))
            self.nav.addItem(it)
            self.nav_items.append(it)

    def _paint_chrome(self):
        """Re-tint icons and the logo after a theme change."""
        col = theme.current["text"]
        for it in self.nav_items:
            it.setIcon(icons.icon(self.pages[it.data(USER)].icon, col))
        for b in (self.settings_btn, self.theme_btn, self.menu_btn):
            b.setIcon(icons.icon(b._icon_name, col))
        dpr = self.devicePixelRatioF() if hasattr(self, "devicePixelRatioF") else 1.0
        self.logo_mark.setPixmap(icons.logo_pixmap(28, dpr))   # drawn at the exact size: sharp
        # the name: light grey on dark themes, the theme's own text colour on light ones
        dark = QtGui.QColor(theme.current["window"]).lightness() < 128
        self.logo_name.setStyleSheet(f"color: {'#e5e7eb' if dark else theme.current['text']};")
        self._update_dot(True)
        self._paint_privilege_badge()

    def _paint_privilege_badge(self):
        admin = probe.is_root()
        self.priv.set_state(
            "ok" if admin else "warn",
            "Administrator" if admin else "Standard user",
            filled=admin, info=not admin,
            tip=("Running as root: every process's readings are visible." if admin else
                 "Running without administrator rights. Some readings for other users' "
                 "processes are hidden. Click for details."))

    def explain_privileges(self):
        import pwd
        if probe.is_root():
            QtWidgets.QMessageBox.information(
                self, "Running as administrator",
                "Taskscope is running as root, so every process's disk activity, open "
                "files, executable path and network sockets are visible, and actions on "
                "any process or service need no further password.\n\n"
                "Close this window when you no longer need full access.")
            return
        user = pwd.getpwuid(os.getuid()).pw_name
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Running as a standard user")
        box.setIcon(E(QtWidgets.QMessageBox, "Icon", "Information"))
        box.setText(f"<b>Taskscope is running as <i>{user}</i>, without administrator rights.</b>")
        box.setInformativeText(
            "Linux only lets a process's owner or an administrator read some of its "
            "details. For processes that belong to other accounts (root, mysql, system "
            "services):<br><br>"
            "• <b>Disk read / Disk write</b> show <i>Access denied</i> — the kernel keeps "
            "these counters in /proc/&lt;pid&gt;/io, readable only by the owner or root.<br>"
            "• <b>File descriptors</b> and <b>Image path</b> in the details panel are hidden.<br>"
            "• <b>Connections</b> can't tell which program owns their network sockets.<br>"
            "• <b>Ending, suspending or re-prioritising</b> them, and starting or stopping "
            "system services, asks for your password each time.<br><br>"
            "Everything else — CPU, memory, threads, performance graphs and all of your own "
            "processes — is fully visible.<br><br>"
            "<b>Run as administrator</b> opens a second window with full access after "
            "asking for your password once.")
        run = box.addButton("Run as administrator", E(QtWidgets.QMessageBox, "ButtonRole", "AcceptRole"))
        box.addButton("Close", E(QtWidgets.QMessageBox, "ButtonRole", "RejectRole"))
        if elevate.method() is None:
            run.setEnabled(False)
            run.setToolTip("No elevation tool found (install polkit or sudo).")
        exec_(box)
        if box.clickedButton() is run:
            self.run_as_admin()

    def set_nav_collapsed(self, collapsed):
        self.settings["nav_collapsed"] = collapsed
        self.side.setFixedWidth(56 if collapsed else 220)
        for it in self.nav_items:
            title = self.pages[it.data(USER)].title
            it.setText("" if collapsed else title)
            it.setToolTip(title if collapsed else "")
        if hasattr(self, "_section_item"):
            self._section_item.setText("" if collapsed else "TOOLS")
        # collapsed: icons, logo and buttons centred in the narrow bar
        self.nav.itemDelegate().collapsed = collapsed
        self.nav.doItemsLayout()                    # rows take the new width, not the old text's
        self.logo_name.setVisible(not collapsed)
        self._logo_row.setContentsMargins(*((0, 8, 0, 8) if collapsed else (18, 8, 8, 8)))
        self._logo_row.setStretch(0, 1 if collapsed else 0)
        self._foot.setStretch(0, 1 if collapsed else 0)
        self._foot.setContentsMargins(*((0, 4, 0, 0) if collapsed else (10, 4, 10, 0)))
        for b, t in ((self.settings_btn, "Settings"), (self.theme_btn, "Theme")):
            b.setText("" if collapsed else t)
            b.setToolButtonStyle(E(Qt, "ToolButtonStyle", "ToolButtonIconOnly" if collapsed else "ToolButtonTextBesideIcon"))
            b.setToolTip(t)
        self._paint_chrome()

    def toggle_nav(self):
        self.set_nav_collapsed(not self.settings.get("nav_collapsed", False))

    def cycle_theme(self):
        """Theme button: the Colors popup (theme, accent, display, saturation, glow, density)."""
        from .themepopup import ThemePopup
        pop = ThemePopup(self)
        btn = self.theme_btn
        pos = btn.mapToGlobal(QtCore.QPoint(0, 0))
        pop.adjustSize()
        pop.move(pos.x(), max(0, pos.y() - pop.height() - 6))
        pop.show()

    def _theme_menu(self):
        """(old) a menu of all themes, the current one ticked."""
        m = QtWidgets.QMenu(self)
        cur = self.settings.get("theme", "dark")
        for key in theme.CHOICES:
            if key == "system":
                m.addSeparator()
            a = m.addAction(theme.label(key), lambda k=key: self.set_theme(k))
            a.setCheckable(True)
            a.setChecked(key == cur)
        btn = self.theme_btn
        exec_(m, btn.mapToGlobal(QtCore.QPoint(0, -m.sizeHint().height())))

    def set_theme(self, key):
        self.settings["theme"] = key
        self.apply_settings()
        for p in self.pages:
            if isinstance(p, pages.SettingsPage):
                p.sync_theme()
        self.flash(f"Theme: {theme.label(key)}")

    def _filter_edit(self):
        return self.tools.currentWidget().findChild(QtWidgets.QLineEdit)

    def focus_filter(self):
        e = self._filter_edit()
        if e:
            e.setFocus()
            e.selectAll()

    def clear_filter(self):
        page = self.stack.currentWidget()
        if getattr(page, "in_history", lambda: False)():
            page.set_history(None)                 # Home: leave the history view
            return
        e = self._filter_edit()
        if e and e.text():
            e.clear()

    def flash(self, text, ms=3000):
        self.status_msg.setText(text)
        self._flash_timer.start(ms)

    def _update_dot(self, ok):
        self.dot.setStyleSheet(f"color: {theme.current['ok'] if ok else theme.current['bad']};")

    def open_index(self, idx):
        for it in self.nav_items:
            if it.data(USER) == idx:
                self.nav.setCurrentItem(it)
                return
        self._show_page(idx)

    def open_page(self, cls):
        for i, p in enumerate(self.pages):
            if isinstance(p, cls):
                self.nav.clearSelection()
                self.nav.setCurrentItem(None)
                self._show_page(i)
                return

    def toggle_settings(self):
        """Settings button: open Settings, or close it again and go back to
        the page that was open before."""
        if isinstance(self.stack.currentWidget(), pages.SettingsPage):
            self.open_index(getattr(self, "_before_settings", 0))
        else:
            self._before_settings = self.stack.currentIndex()
            self.open_page(pages.SettingsPage)

    def _nav_changed(self, cur, _prev=None):
        if cur is not None and cur.data(USER) is not None and cur.data(USER) >= 0:
            self._show_page(cur.data(USER))

    # ------------------------------------------------------------ menus --
    def _menus(self):
        """The window's commands. There is no menu bar (it took a row of space):
        the commands are buttons in Settings > Actions, and their keyboard
        shortcuts work anywhere in the window."""
        self.command_groups = []

        def group(title, *acts):
            self.command_groups.append((title, [a for a in acts if a is not None]))
        admin = None
        if not probe.is_root():
            admin = self._act(self, "Run as administrator", self.run_as_admin)
            admin.setEnabled(elevate.method() is not None)
        group("Tasks", self._act(self, "Run new task…", lambda: pages.run_command_dialog(self), "Ctrl+N"), admin)
        self.datarec_action = self._act(self, "Start recording data", self.toggle_data_recording, "Ctrl+Shift+R")
        group("Recording", self.datarec_action,
              self._act(self, "Replay a recording…", self.open_recording_dialog, "Ctrl+O"))
        group("Screensaver", self._act(self, "Start screensaver", self.screensaver, "F12"),
              self._act(self, "Register with XScreenSaver", self.register_saver))
        self.pause_action = self._act(self, "Pause updates", self.toggle_pause, "Ctrl+P")
        self.pause_action.setCheckable(True)
        group("Updates", self._act(self, "Refresh now", self.refresh_all, "F5"), self.pause_action)
        group("Help", self._act(self, "What was detected on this system", self.show_backends),
              self._act(self, "About Taskscope", self.about))
        group("Quit", self._act(self, "Exit Taskscope", self.close, "Ctrl+Q"))
        self.top_action = QAction("Always on top", self)    # the switch is in Settings > Window
        self.top_action.setCheckable(True)
        self.top_action.triggered.connect(lambda *_: self.toggle_top())

    def _shortcuts(self):
        QShortcut(QtGui.QKeySequence("Ctrl+F"), self, activated=self.focus_filter)
        QShortcut(QtGui.QKeySequence("Escape"), self, activated=self.clear_filter)
        for i, it in enumerate(self.nav_items[:9]):
            QShortcut(QtGui.QKeySequence(f"Ctrl+{i + 1}"), self,
                      activated=lambda it=it: self.nav.setCurrentItem(it))

    def _act(self, owner, text, slot, shortcut=None):
        a = QAction(text, self)
        a.triggered.connect(lambda *_: slot())
        if shortcut:
            a.setShortcut(QtGui.QKeySequence(shortcut))
            a.setShortcutContext(E(Qt, "ShortcutContext", "WindowShortcut"))
        owner.addAction(a)                     # added to the window, so the shortcut works anywhere
        return a

    # ------------------------------------------------------------ loop ----
    def _need_procs(self):
        page = self.stack.currentWidget()
        return (isinstance(page, (pages.ProcessesPage, pages.UsersPage, pages.HomePage))
                or self.remote is not None
                or self.datarec.active)

    def tick(self):
        if self.player is not None:
            self._replay_tick()
            return
        if self.pause_action.isChecked():
            return
        visible = self.isVisible() and not self.isMinimized()
        # Keep sampling while minimised or hidden, so graphs and Home's history have
        # no holes; only drawing the pages is skipped while nobody can see them.
        t0 = time.perf_counter()
        perf_snap = self.perf_sampler.sample(light=not visible and self.remote is None
                                             and not self.datarec.active)
        t1 = time.perf_counter()
        self._ticks = getattr(self, "_ticks", 0) + 1
        # The full process list is read when a page shows it (or for recording / the
        # remote page). Otherwise only the busiest processes are read, cheaply, so
        # Home's history has its top processes for every moment, minimised or not.
        full = (self._need_procs() and visible) or self.datarec.active or self.remote is not None
        procs_list = self.proc_sampler.sample(with_io=visible or self.datarec.active) if full \
            else self.proc_sampler.sample_top(40)
        sampled_procs = True
        t2 = time.perf_counter()
        now = time.time()
        last = self.sample_stats.get("at")
        self.sample_stats = {"at": now, "perf_ms": (t1 - t0) * 1000,
                             "procs_ms": (t2 - t1) * 1000 if sampled_procs else None,
                             "interval_ms": (now - last) * 1000 if last else None,
                             "ticks": self.sample_stats.get("ticks", 0) + 1}
        if not self.settings.get("show_self", True):        # hide Taskscope itself
            me = os.getpid()
            procs_list = [p for p in procs_list if p.pid != me]
        self.snapshot = {"procs": procs_list, "perf": perf_snap, "procs_fresh": sampled_procs,
                         "procs_top_only": not full}
        if self.tray is not None:
            self.tray.update(perf_snap)
        if self.mini is not None and self.mini.isVisible():
            self.mini.update_from(perf_snap)
        if self.datarec.active:
            try:
                self.datarec.add(self.snapshot)
            except OSError as e:            # disk full etc.: stop cleanly and say why
                self.toggle_data_recording()
                message(self, False, f"Recording stopped: {e}")
        page = self.stack.currentWidget()
        for p in self.pages:
            # Performance and Home keep their history even while another page is
            # shown or the window is minimised (they skip drawing when hidden).
            if isinstance(p, (pages.PerformancePage, pages.HomePage)) or (visible and p.live and p is page):
                p.refresh(self.snapshot)
        if visible:
            c, m = perf_snap["cpu"], perf_snap["memory"]
            ip = next((a for n in perf_snap["net"] if n["up"] and n["kind"] != "Virtual"
                       for a in n["addrs"] if "." in a), None)
            parts = [f"{c['processes']} processes", f"CPU {c['total']:.0f}%",
                     f"Memory {m['percent']:.0f}%"]
            if ip:
                parts.append(f"IP {ip}")
            parts.append(f"Up {fmt.duration(c['uptime'])}")
            self.status_cpu.setText("  •  ".join(parts))
            if self.datarec.active:
                el = int(time.time() - self.datarec.started)
                self.status_rec.setText(fmt.duration(el).rjust(8, "0"))

    def _show_page(self, idx):
        self.stack.setCurrentIndex(idx)
        self.tools.setCurrentIndex(idx)
        page = self.pages[idx]
        self.page_title.setText(page.title)
        self.settings["last_page"] = idx
        page.activate()
        if page.live:
            if self._need_procs() and (not self.snapshot["procs"] or self.snapshot.get("procs_top_only")):
                self.snapshot["procs"] = self.proc_sampler.sample()
            page.refresh(self.snapshot)

    def refresh_all(self):
        page = self.stack.currentWidget()
        if isinstance(page, (pages.ListPage, pages.RecordingsPage)):
            page.reload()
        else:
            self.tick()

    def changeEvent(self, e):
        # Resume immediately when un-minimised so the view is never stale.
        if e.type() == E(QtCore.QEvent, "Type", "WindowStateChange") and not self.isMinimized():
            QtCore.QTimer.singleShot(0, self.tick)
        super().changeEvent(e)

    # -------------------------------------------------------- settings ----
    def apply_settings(self, initial=False):
        s = self.settings
        self.timer.start(int(s["interval_ms"]))
        theme.apply(s["theme"], s.get("font_family", ""), s.get("font_size", 10),
                    s.get("accent", ""), s.get("density", "normal"))
        self._paint_chrome()
        self.top_action.setChecked(s["always_on_top"])
        flag = E(Qt, "WindowType", "WindowStaysOnTopHint")
        want = bool(s["always_on_top"])
        if bool(self.windowFlags() & flag) != want:
            self.setWindowFlag(flag, want)
            if not initial:
                self.show()
        self._apply_remote()
        self._apply_extras(initial)
        if not initial:
            settings_mod.save(s)

    def _apply_extras(self, initial=False):
        """Display preferences, graphs, notification-area icon, mini monitor, autostart."""
        from . import extras, widgets
        s = self.settings
        fmt.TEMP_UNIT = "F" if s.get("temp_unit") == "F" else "C"
        if self.stack.fit_pages != bool(s.get("fit_pages")):
            self.stack.fit_pages = bool(s.get("fit_pages"))
            self.stack.updateGeometry()
        fmt.CPU_SCALE = float(os.cpu_count() or 1) if s.get("cpu_per_core") else 1.0
        widgets.GRAPH.update(keyed=s.get("graph_keyed", True), dashed=s.get("graph_dashed", True),
                             smooth=s.get("graph_smooth", False), points=int(s.get("graph_points", 60)),
                             display=s.get("color_display", "color"),
                             saturation=int(s.get("color_saturation", 11)), glow=int(s.get("color_glow", 0)))
        # row density for the tables (the tree views follow the style sheet padding)
        rh = {"compact": 22, "comfortable": 32}.get(s.get("density", "normal"), 26)
        for t in self.findChildren(QtWidgets.QTableView):
            t.verticalHeader().setDefaultSectionSize(rh if t.property("big") else rh - 2)
        for w in self.findChildren(QtWidgets.QWidget):     # repaint custom-drawn meters
            if type(w).__name__ in ("LedBar", "CpuPlot", "MemPlot", "TextHeader"):
                w.update()
        for g in self.findChildren(widgets.Graph):
            if not g.mini:
                g.set_points(widgets.GRAPH["points"])
            g.update()
        # notification-area icon
        want_tray = bool(s.get("tray_icon")) and extras.TrayIcon.available()
        if want_tray and self.tray is None:
            self.tray = extras.TrayIcon(self)
        elif not want_tray and self.tray is not None:
            self.tray.close()
            self.tray = None
        # mini monitor
        if s.get("mini_monitor"):
            if self.mini is None:
                self.mini = extras.MiniMonitor(self)
            self.mini._apply_opacity(False)
            self.mini.show()
        elif self.mini is not None:
            self.mini.close()
            self.mini.deleteLater()
            self.mini = None
        # start at login: keep the autostart file in step with the setting
        if not initial and bool(s.get("autostart")) != os.path.exists(extras.autostart_path()):
            err = extras.set_autostart(bool(s.get("autostart")))
            if err:
                message(self, False, f"Could not change start at login: {err}")
        for p in self.pages:
            if p.isVisible() and hasattr(p, "refresh") and getattr(p, "live", False):
                p.refresh(self.snapshot)

    def quit_for_real(self):
        self._quitting = True
        self.close()

    def _apply_remote(self):
        s = self.settings
        want = s["remote_enabled"]
        cfg = (s["remote_host"], s["remote_port"], s["remote_token"])
        if self.remote and (not want or cfg != (self.remote.host, self.remote.port, self.remote.token)):
            self.remote.stop()
            self.remote = None
        if want and not self.remote:
            srv = remote.RemoteServer(self._remote_snapshot, s["remote_host"], s["remote_port"],
                                      s["remote_token"] or None)
            try:
                srv.start()
            except OSError as e:
                message(self, False, f"Remote summary could not start: {e}")
                s["remote_enabled"] = False
                return
            s["remote_token"] = srv.token
            self.remote = srv
        self.status_remote.setText("Remote: on" if self.remote else "")

    def _remote_snapshot(self):
        snap = self.snapshot           # replaced atomically by tick()
        if not snap["perf"]:
            return {}
        return remote.make_snapshot(snap["perf"], snap["procs"])

    def toggle_top(self):
        self.settings["always_on_top"] = self.top_action.isChecked()
        self.apply_settings()

    def toggle_pause(self):
        self.flash("Updates paused" if self.pause_action.isChecked() else "Updates resumed")

    # --------------------------------------------------------- actions ----
    def run_as_admin(self):
        for p in self.pages:                       # hand Home's recent readings to the new window
            if hasattr(p, "save_history"):
                p.save_history()
        ok, msg = elevate.relaunch_as_root()
        if not ok:
            message(self, False, msg)

    # ---- data recording and replay --------------------------------------
    def toggle_data_recording(self):
        if self.datarec.active:
            path = self.datarec.stop()
            self.datarec_action.setText("Start recording data")
            self.rec_btn.setIcon(icons.icon("record", "#e5383b", "#e5383b"))
            self.rec_btn.setToolTip(REC_TIP)
            self.status_rec.setText("00:00:00")
            self.flash(f"Recording saved: {os.path.basename(path)} (replay it from Recordings)", 8000)
        else:
            if self.player is not None:
                message(self, False, "Go back to live data before starting a new recording.")
                return
            try:
                path = self.datarec.start(self.settings.get("data_dir") or None,
                                          int(self.settings["interval_ms"]))
            except OSError as e:
                message(self, False, f"Could not start recording: {e}")
                return
            self.datarec_action.setText("Stop recording data")
            self.rec_btn.setIcon(icons.icon("stop", "#e5383b", "#e5383b"))
            self.rec_btn.setToolTip("Stop recording (Ctrl+Shift+R)")
            self.flash(f"Recording data to {os.path.basename(path)}", 5000)
        self._recordings_changed()

    def _recordings_changed(self):
        for p in self.pages:
            if isinstance(p, pages.RecordingsPage):
                p.update_state()

    def open_recording_dialog(self):
        folder = self.settings.get("data_dir") or datarec.default_dir()
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Replay a recording", folder, f"Taskscope recordings (*{datarec.SUFFIX})")
        if path:
            self.open_recording(path)

    def open_recording(self, path, start=0):
        if self.datarec.active:
            message(self, False, "Stop the current recording before replaying one.")
            return
        self.flash("Loading recording…", 30000)

        def done(player):
            if isinstance(player, Exception):
                self.flash("")
                message(self, False, f"Could not open the recording: {player}")
                return
            self._start_replay(player, start)
        run_async(datarec.Player, done, path)

    def _replay_bar(self):
        bar = QtWidgets.QFrame()
        bar.setObjectName("ReplayBar")
        bar.setStyleSheet(f"#ReplayBar {{ background: {theme.current['nav_sel']}; }}")
        lay = QtWidgets.QHBoxLayout(bar)
        lay.setContentsMargins(14, 4, 14, 4)
        tag = QtWidgets.QLabel("<b>REPLAY</b>")
        lay.addWidget(tag)
        self.rp_play = QtWidgets.QPushButton("Pause")
        self.rp_play.setFixedWidth(80)
        self.rp_play.clicked.connect(self.replay_toggle)
        lay.addWidget(self.rp_play)
        self.rp_slider = QtWidgets.QSlider(E(Qt, "Orientation", "Horizontal"))
        self.rp_slider.valueChanged.connect(self._replay_slider)
        lay.addWidget(self.rp_slider, 1)
        self.rp_time = QtWidgets.QLabel("")
        self.rp_time.setMinimumWidth(260)
        lay.addWidget(self.rp_time)
        self.rp_speed = QtWidgets.QComboBox()
        for sp in (0.5, 1, 2, 5, 10, 30):
            self.rp_speed.addItem(f"{sp:g}×", sp)
        self.rp_speed.setCurrentIndex(1)
        self.rp_speed.setToolTip("Playback speed")
        self.rp_speed.currentIndexChanged.connect(self._replay_speed)
        lay.addWidget(self.rp_speed)
        back = QtWidgets.QPushButton("Back to live")
        back.clicked.connect(self.stop_replay)
        lay.addWidget(back)
        bar.hide()
        self.replay_bar = bar
        return bar

    def _reset_graphs(self):
        from .widgets import Graph
        for p in self.pages:
            for g in p.findChildren(Graph):
                g.reset()

    def _start_replay(self, player, start=0):
        self.player = player
        self.replay_playing = True
        self.rp_play.setText("Pause")
        self.rp_slider.blockSignals(True)
        self.rp_slider.setRange(0, len(player) - 1)
        self.rp_slider.setValue(0)
        self.rp_slider.blockSignals(False)
        self.replay_bar.setStyleSheet(f"#ReplayBar {{ background: {theme.current['nav_sel']}; }}")
        self.replay_bar.show()
        self._reset_graphs()
        self.timer.start(max(30, int(player.interval_ms / self.replay_speed)))
        if start:
            self._replay_seek(min(start, len(player) - 1))
        else:
            self._replay_show(0)
        self.flash(f"Replaying {os.path.basename(player.path)}. Processes, Users, Summary and "
                   "Performance show the recording.", 8000)
        self._recordings_changed()

    def stop_replay(self):
        if self.player is None:
            return
        self.player = None
        self.replay_bar.hide()
        self._reset_graphs()
        self.timer.start(int(self.settings["interval_ms"]))
        self.snapshot = {"procs": [], "perf": None}
        self.flash("Back to live data", 3000)
        self._recordings_changed()
        self.tick()

    def replay_toggle(self):
        if self.player is None:
            return
        if not self.replay_playing and self.replay_pos >= len(self.player) - 1:
            self._replay_seek(0)                  # at the end: start over
        self.replay_playing = not self.replay_playing
        self.rp_play.setText("Pause" if self.replay_playing else "Play")

    def _replay_speed(self, *_):
        self.replay_speed = float(self.rp_speed.currentData())
        if self.player is not None:
            self.timer.start(max(30, int(self.player.interval_ms / self.replay_speed)))

    def _replay_slider(self, v):
        if self.player is not None and v != self.replay_pos:
            self._replay_seek(v)

    def _replay_tick(self):
        if not self.replay_playing:
            return
        if self.replay_pos >= len(self.player) - 1:
            self.replay_playing = False
            self.rp_play.setText("Play")
            return
        self.replay_pos += 1
        self._replay_show(self.replay_pos)

    def _replay_seek(self, i):
        """Jump to sample i, rebuilding the graphs' last minute of history."""
        self.replay_pos = i
        self._reset_graphs()
        graph_pages = [p for p in self.pages if isinstance(p, (pages.PerformancePage, pages.SummaryPage, pages.HomePage))]
        for j in range(max(0, i - 60), i):
            snap = {"perf": self.player.frames[j]["perf"], "procs": []}
            for p in graph_pages:
                p.refresh(snap)
        self._replay_show(i)

    def _replay_show(self, i):
        snap = self.player.frame(i)
        self.snapshot = snap
        page = self.stack.currentWidget()
        for p in self.pages:
            if p.live and (p is page or isinstance(p, (pages.PerformancePage, pages.HomePage))):
                p.refresh(snap)
        c, m = snap["perf"]["cpu"], snap["perf"]["memory"]
        self.status_cpu.setText(f"{c['processes']} processes  •  CPU {c['total']:.0f}%  •  "
                                f"Memory {m['percent']:.0f}%  •  recorded")
        t0 = self.player.time_at(0)
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(snap["time"]))
        self.rp_time.setText(f"{when}   {fmt.duration(snap['time'] - t0)} / "
                             f"{fmt.duration(self.player.time_at(len(self.player) - 1) - t0)}")
        self.rp_slider.blockSignals(True)
        self.rp_slider.setValue(i)
        self.rp_slider.blockSignals(False)

    def screensaver(self):
        from .screensaver import Screensaver
        if self.saver:
            return
        self.saver = Screensaver(lambda: self.snapshot["perf"], audio=self.settings["screensaver_audio"],
                                 on_close=self._saver_closed)
        self.saver.showFullScreen()

    def _saver_closed(self):
        self.saver = None

    def register_saver(self):
        from ..sysinfo import xscreensaver
        try:
            message(self, True, xscreensaver.register())
        except OSError as e:
            message(self, False, str(e))

    def backend_summary(self):
        mgrs = services.detect()
        gpus = ", ".join(f"{c['name']} [{c['backend']}]" for c in self.perf_sampler.gpu.cards) or "none"
        return "\n".join([
            f"Distribution: {probe.distro_name()}",
            f"Session: {probe.session_type()}   Toolkit: {version_string()}",
            f"Service manager: {', '.join(m.name for m in mgrs) or 'none found'}",
            f"Package sources: {', '.join(packages.available_sources())}",
            f"GPUs: {gpus}",
            f"Elevation: {elevate.method() or 'unavailable'}",
        ])

    def show_backends(self):
        QtWidgets.QMessageBox.information(self, "Detected on this system", self.backend_summary())

    def about(self):
        QtWidgets.QMessageBox.about(
            self, f"About {APP_NAME}",
            f"<b>{APP_NAME} {__version__}</b><br>A lightweight task manager for Linux.<br><br>"
            f"Free software under the GNU GPL v3 or later.<br>{version_string()}")

    def closeEvent(self, e):
        if (self.tray is not None and self.settings.get("close_to_tray") and not self._quitting):
            # keep running in the notification area; Quit from its menu ends Taskscope
            self.settings["geometry"] = bytes(self.saveGeometry().toHex()).decode()
            self.hide()
            e.ignore()
            self.tray.icon.showMessage(APP_NAME, "Still running here. Right-click the icon to quit.",
                                       icons.app_icon(), 3000)
            return
        if self.mini is not None:
            self.mini.close()
        if self.tray is not None:
            self.tray.close()
        if self.datarec.active:
            self.datarec.stop()
        if self.remote:
            self.remote.stop()
        self.settings["geometry"] = bytes(self.saveGeometry().toHex()).decode()
        for p in self.pages:
            if hasattr(p, "save_state"):
                p.save_state(self.settings)
        try:
            settings_mod.save(self.settings)
        except OSError:
            pass
        super().closeEvent(e)

    def summon(self):
        self.setWindowState(self.windowState() & ~E(Qt, "WindowState", "WindowMinimized"))
        self.show()
        self.raise_()
        self.activateWindow()


# ------------------------------------------------------- single instance --

def try_forward_to_running():
    """If another instance runs, ask it to raise its window and return True."""
    sock = QtNetwork.QLocalSocket()
    sock.connectToServer(server_name())
    if sock.waitForConnected(300):
        sock.write(b"raise\n")
        sock.waitForBytesWritten(300)
        sock.disconnectFromServer()
        return True
    return False


def listen_for_summon(win):
    srv = QtNetwork.QLocalServer(win)
    QtNetwork.QLocalServer.removeServer(server_name())   # stale socket after a crash
    srv.listen(server_name())

    def on_conn():
        c = srv.nextPendingConnection()
        if c:
            c.readyRead.connect(lambda: (c.readAll(), win.summon()))
    srv.newConnection.connect(on_conn)
    return srv
