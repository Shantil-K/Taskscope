"""Processes page: parent/child tree (or flat list) with heat-map cells and a
details panel for the selected process.

Cost per refresh: when no process started or exited, the model swaps in the
new readings and asks the view to repaint, so Qt only reads the visible cells.
Structural changes use one layoutChanged with persistent indexes remapped by
PID, which keeps selection and expanded branches intact.
"""

import os

from .. import fmt
from ..sysinfo import procs
from . import icons, theme
from .qt import E, QtCore, QtWidgets, Qt, Signal, exec_
from .widgets import enlarge, message

DISPLAY = E(Qt, "ItemDataRole", "DisplayRole")
ALIGN = E(Qt, "ItemDataRole", "TextAlignmentRole")
BG = E(Qt, "ItemDataRole", "BackgroundRole")
FG = E(Qt, "ItemDataRole", "ForegroundRole")
TOOLTIP = E(Qt, "ItemDataRole", "ToolTipRole")
HORIZ = E(Qt, "Orientation", "Horizontal")
RIGHT = E(Qt, "AlignmentFlag", "AlignRight") | E(Qt, "AlignmentFlag", "AlignVCenter")
DENIED = "Access denied"

TOTAL_MEM = 1
for _l in open("/proc/meminfo"):
    if _l.startswith("MemTotal:"):
        TOTAL_MEM = int(_l.split()[1]) * 1024
        break


def confirm_signal(parent, win, fn, p):
    """Ask before End task / Kill when Settings > General says so."""
    if fn not in (procs.terminate, procs.kill) or not win.settings.get("confirm_kill", True):
        return True
    verb = "End" if fn is procs.terminate else "Kill"
    extra = "" if fn is procs.terminate else "\n\nKill stops it at once; unsaved work in it is lost."
    btn = QtWidgets.QMessageBox.question(parent, f"{verb} process", f"{verb} {p.name} (PID {p.pid})?{extra}")
    return btn == E(QtWidgets.QMessageBox, "StandardButton", "Yes")


def _rate(v):
    return DENIED if v is None else fmt.size(v, "/sec")


# (title, attribute, formatter, numeric, width)
COLUMNS = [
    ("Name", "name", None, False, 240),
    ("PID", "pid", str, True, 70),
    ("Status", "state", None, False, 90),
    ("User name", "user", None, False, 100),
    ("CPU", "cpu", lambda v: fmt.cpu(v), True, 64),
    ("Memory", "rss", fmt.size, True, 88),
    ("Disk read", "read_rate", _rate, True, 92),
    ("Disk write", "write_rate", _rate, True, 92),
    ("Threads", "threads", str, True, 62),
    ("Virtual", "vsize", fmt.size, True, 84),
    ("Nice", "nice", str, True, 48),
    ("Command", "cmdline", None, False, 300),
]
CPU_COL, MEM_COL = 4, 5


class ProcessModel(QtCore.QAbstractItemModel):
    refreshed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.procs = {}          # pid -> Proc
        self.children = {0: []}  # pid -> [child pids]; 0 is the invisible root
        self.parent_of = {}      # pid -> parent pid (0 for top level)
        self.row_of = {}         # pid -> row among its siblings
        self.tree = True
        self.filter = ""
        self.only_uid = None
        self.sort_col, self.sort_desc = CPU_COL, True

    # ---- Qt model interface ------------------------------------------
    def index(self, row, column, parent=QtCore.QModelIndex()):
        ppid = int(parent.internalId()) if parent.isValid() else 0
        kids = self.children.get(ppid, ())
        if 0 <= row < len(kids):
            return self.createIndex(row, column, kids[row])
        return QtCore.QModelIndex()

    def parent(self, index=None):
        if index is None or not index.isValid():
            return QtCore.QModelIndex()
        pp = self.parent_of.get(int(index.internalId()), 0)
        if pp == 0:
            return QtCore.QModelIndex()
        return self.createIndex(self.row_of.get(pp, 0), 0, pp)

    def rowCount(self, parent=QtCore.QModelIndex()):
        if parent.isValid():
            if parent.column() != 0:
                return 0
            return len(self.children.get(int(parent.internalId()), ()))
        return len(self.children.get(0, ()))

    def hasChildren(self, parent=QtCore.QModelIndex()):
        return self.rowCount(parent) > 0

    def columnCount(self, parent=QtCore.QModelIndex()):
        return len(COLUMNS)

    def headerData(self, section, orientation, role=DISPLAY):
        if orientation == HORIZ and role == DISPLAY:
            return COLUMNS[section][0]
        if orientation == HORIZ and role == ALIGN and COLUMNS[section][3]:
            return RIGHT
        return None

    def data(self, index, role=DISPLAY):
        p = self.procs.get(int(index.internalId()))
        if p is None:
            return None
        col = index.column()
        title, attr, f, numeric, _ = COLUMNS[col]
        if role == DISPLAY:
            v = getattr(p, attr)
            if attr == "cmdline" and not v:
                return f"[{p.name}]"
            if f:
                return f(v)
            return "" if v is None else str(v)
        if role == ALIGN:
            return RIGHT if numeric else None
        if role == BG:
            if col == CPU_COL:
                return theme.heat(p.cpu / 20.0) if p.cpu >= 0.05 else None
            if col == MEM_COL:
                frac = p.rss / TOTAL_MEM
                return theme.heat(frac * 8) if frac >= 0.01 else None
            return None
        if role == FG:
            if (attr in ("read_rate", "write_rate") and getattr(p, attr) is None) or \
               (attr == "cmdline" and not p.cmdline):
                from .qt import QtGui
                return QtGui.QColor(theme.current["muted"])
            return None
        if role == TOOLTIP:
            if attr == "cmdline" and len(p.cmdline) > 50:
                return p.cmdline
            if attr in ("read_rate", "write_rate") and getattr(p, attr) is None:
                return (f"Owned by {p.user}. Linux only shows a process's disk counters to its "
                        "owner or an administrator. Use File → Run as administrator to see them.")
        return None

    def sort(self, column, order=E(Qt, "SortOrder", "AscendingOrder")):
        self.sort_col = column
        self.sort_desc = order == E(Qt, "SortOrder", "DescendingOrder")
        self._rebuild(force=True)

    # ---- updates -----------------------------------------------------
    def set_procs(self, plist):
        self.procs = {p.pid: p for p in plist}
        self._rebuild()

    def set_mode(self, tree=None, flt=None, only_uid=False):
        if tree is not None:
            self.tree = tree
        if flt is not None:
            self.filter = flt.strip().lower()
        if only_uid is not False:
            self.only_uid = only_uid
        self._rebuild(force=True)

    def _visible(self):
        ps = self.procs
        if self.only_uid is not None:
            ps = {k: p for k, p in ps.items() if p.uid == self.only_uid}
        if self.filter:
            f = self.filter
            ps = {k: p for k, p in ps.items()
                  if f in p.name.lower() or f in p.user.lower() or f == str(k) or f in p.cmdline.lower()}
        return ps

    def _sort_key(self):
        attr = COLUMNS[self.sort_col][1]
        if COLUMNS[self.sort_col][3]:
            return lambda pid: (lambda v: -1.0 if v is None else v)(getattr(self.procs[pid], attr))
        return lambda pid: str(getattr(self.procs[pid], attr) or "").lower()

    def _rebuild(self, force=False):
        vis = self._visible()
        tree = self.tree and not self.filter and self.only_uid is None
        children = {0: []}
        parent_of = {}
        for pid, p in vis.items():
            pp = p.ppid if tree and p.ppid in vis else 0
            parent_of[pid] = pp
            children.setdefault(pp, []).append(pid)
        key = self._sort_key()
        for kids in children.values():
            kids.sort(key=key, reverse=self.sort_desc)
        if not force and children == self.children:
            self.refreshed.emit()   # same rows, new numbers: just repaint
            return
        self.layoutAboutToBeChanged.emit()
        old = self.persistentIndexList()
        row_of = {}
        for kids in children.values():
            for i, pid in enumerate(kids):
                row_of[pid] = i
        new = []
        for idx in old:
            pid = int(idx.internalId())
            r = row_of.get(pid)
            new.append(self.createIndex(r, idx.column(), pid) if r is not None else QtCore.QModelIndex())
        self.children, self.parent_of, self.row_of = children, parent_of, row_of
        self.changePersistentIndexList(old, new)
        self.layoutChanged.emit()

    def index_of(self, pid):
        row = self.row_of.get(pid)
        return self.createIndex(row, 0, pid) if row is not None else QtCore.QModelIndex()

    def pid_at(self, index):
        return int(index.internalId()) if index.isValid() else None


class HeatDelegate(QtWidgets.QStyledItemDelegate):
    """Paints the heat-map fill itself: style sheets that style items make Qt
    ignore BackgroundRole, so the model's colour is drawn here first."""

    def paint(self, painter, option, index):
        col = index.data(BG)
        if col is None:
            return super().paint(painter, option, index)
        # Heat cell: paint the row background (the selection tint on the
        # selected row), the heat on top so it stays visible, then the text.
        from .qt import QtGui
        r = option.rect
        sel = option.state & E(QtWidgets.QStyle, "StateFlag", "State_Selected")
        alt = option.features & E(QtWidgets.QStyleOptionViewItem, "ViewItemFeature", "Alternate")
        bg = "nav_sel" if sel else ("alt" if alt else "base")
        painter.fillRect(r, QtGui.QColor(theme.current[bg]))
        painter.fillRect(r.adjusted(1, 1, -1, -1), col)
        painter.setPen(QtGui.QColor(theme.current["text"]))
        painter.setFont(option.font)
        text = option.fontMetrics.elidedText(str(index.data(DISPLAY) or ""),
                                             E(Qt, "TextElideMode", "ElideRight"), r.width() - 8)
        painter.drawText(r.adjusted(4, 0, -4, 0), RIGHT, text)


class DetailsPanel(QtWidgets.QFrame):
    FIELDS = [["PID", "Parent", "User", "Threads"],
              ["CPU time", "Kernel time", "User time", "Memory"],
              ["Peak memory", "Virtual", "Shared", "Swap"],
              ["File descriptors", "Nice", "Cgroup", "Image path"]]

    def __init__(self):
        super().__init__()
        self.setObjectName("Details")
        grid = QtWidgets.QGridLayout(self)
        grid.setContentsMargins(16, 10, 16, 10)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(2)
        self.vals = {}
        for c, col in enumerate(self.FIELDS):
            for r, name in enumerate(col):
                lab = QtWidgets.QLabel(name + ":")
                lab.setProperty("muted", True)
                val = QtWidgets.QLabel("-")
                val.setTextInteractionFlags(E(Qt, "TextInteractionFlag", "TextSelectableByMouse"))
                val.setMinimumWidth(80)
                grid.addWidget(lab, r, c * 2)
                grid.addWidget(val, r, c * 2 + 1)
                self.vals[name] = val
        for c in range(4):
            grid.setColumnStretch(c * 2 + 1, 1)

    def show_proc(self, p, live=True):
        if p is None:
            for v in self.vals.values():
                v.setText("-")
            return
        # During replay only the recorded numbers exist; never read the live
        # process that happens to have the same PID now.
        x = procs.panel_info(p.pid) if live else {"fds": None, "cgroup": ""}
        missing = DENIED if live else "not recorded"

        def sz(v):
            return fmt.size(v) if v is not None else missing
        vals = {"PID": p.pid, "Parent": p.ppid, "User": p.user, "Threads": p.threads,
                "CPU time": fmt.duration(p.utime + p.stime), "Kernel time": fmt.duration(p.stime),
                "User time": fmt.duration(p.utime), "Memory": fmt.size(p.rss),
                "Peak memory": sz(x.get("VmHWM")), "Virtual": fmt.size(p.vsize),
                "Shared": sz(x.get("RssShmem")), "Swap": sz(x.get("VmSwap")),
                "File descriptors": x["fds"] if x["fds"] is not None else missing,
                "Nice": p.nice, "Cgroup": x["cgroup"] or "-", "Image path": p.exe or missing}
        for k, v in vals.items():
            lab = self.vals[k]
            text = str(v)
            lab.setToolTip(text if len(text) > 40 else "")
            lab.setText(text if len(text) <= 48 else "…" + text[-47:])


class ProcessesPage(QtWidgets.QWidget):
    title, icon, live = "Processes", "processes", True

    def __init__(self, win):
        super().__init__()
        self.win = win
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # toolbar lives in the main window's header
        self.toolbar = QtWidgets.QWidget()
        tl = QtWidgets.QHBoxLayout(self.toolbar)
        tl.setContentsMargins(0, 0, 0, 0)
        self.view_btn = QtWidgets.QToolButton()
        self.view_btn.setText("Process View")
        self.view_btn.setIcon(icons.icon("view", theme.current["text"]))
        self.view_btn.setToolButtonStyle(E(Qt, "ToolButtonStyle", "ToolButtonTextBesideIcon"))
        self.view_btn.setPopupMode(E(QtWidgets.QToolButton, "ToolButtonPopupMode", "InstantPopup"))
        m = QtWidgets.QMenu(self.view_btn)
        self.a_tree = m.addAction("Tree (parent → children)", lambda: self.set_view(True))
        self.a_flat = m.addAction("Flat list", lambda: self.set_view(False))
        m.addSeparator()
        self.a_mine = m.addAction("Only my processes", self.toggle_mine)
        for a in (self.a_tree, self.a_flat, self.a_mine):
            a.setCheckable(True)
        m.addSeparator()
        m.addAction("Expand all", lambda: self.view.expandAll())
        m.addAction("Collapse all", lambda: self.view.collapseAll())
        self.view_btn.setMenu(m)
        tl.addWidget(self.view_btn)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Filter by name, user, or PID")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(260)
        self.search.textChanged.connect(lambda t: self.model.set_mode(flt=t))
        tl.addWidget(self.search)
        end = QtWidgets.QPushButton("End task")
        end.clicked.connect(self.end_task)
        tl.addWidget(end)

        self.model = ProcessModel(self)
        self.view = QtWidgets.QTreeView()
        self.view.setModel(self.model)
        enlarge(self.view)
        self.view.setUniformRowHeights(True)      # big speed-up for large trees
        self.view.setSortingEnabled(True)
        self.view.setAlternatingRowColors(True)
        self.view.setAllColumnsShowFocus(True)
        self.view.setIndentation(16)
        self.view.setContextMenuPolicy(E(Qt, "ContextMenuPolicy", "CustomContextMenu"))
        self.view.customContextMenuRequested.connect(self.menu)
        self.view.doubleClicked.connect(lambda *_: self.properties())
        self.model.refreshed.connect(self.view.viewport().update)
        self.heat = HeatDelegate(self.view)
        self.view.setItemDelegateForColumn(CPU_COL, self.heat)
        self.view.setItemDelegateForColumn(MEM_COL, self.heat)
        hdr = self.view.header()
        hdr.setSectionsMovable(True)
        hdr.setStretchLastSection(True)
        for i, c in enumerate(COLUMNS):
            self.view.setColumnWidth(i, int(c[4] * 1.12))   # room for the larger font
        self.view.sortByColumn(CPU_COL, E(Qt, "SortOrder", "DescendingOrder"))
        saved = win.settings.get("proc_header")
        if saved:
            try:
                hdr.restoreState(QtCore.QByteArray.fromHex(saved.encode()))
                self.view.sortByColumn(hdr.sortIndicatorSection(), hdr.sortIndicatorOrder())
            except (TypeError, ValueError):
                pass
        from .qt import QShortcut, QtGui
        sc = QShortcut(QtGui.QKeySequence("Delete"), self.view)
        sc.setContext(E(Qt, "ShortcutContext", "WidgetShortcut"))
        sc.activated.connect(self.end_task)
        self.view.selectionModel().currentRowChanged.connect(lambda *_: self._update_details())
        lay.addWidget(self.view, 1)
        self.details = DetailsPanel()
        lay.addWidget(self.details)
        self._expanded_once = False
        self._selected_once = False
        self.set_view(win.settings.get("process_view", "tree") == "tree", save=False)

    def save_state(self, settings):
        settings["proc_header"] = bytes(self.view.header().saveState().toHex()).decode()

    # ---- data --------------------------------------------------------
    def activate(self):
        pass

    def refresh(self, snap):
        self.model.set_procs(snap["procs"])
        if not self._expanded_once and self.model.tree and self.model.procs:
            self._expanded_once = True
            # Open the first level (init's children) but keep kernel threads folded.
            for row, pid in enumerate(self.model.children.get(0, [])):
                if pid != 2:
                    self.view.expand(self.model.index(row, 0))
        if not self._selected_once and self.model.procs:
            # Start with PID 1 (init / systemd) selected, so the details panel isn't empty.
            self._selected_once = True
            ix = self.model.index_of(1)
            if ix.isValid() and not self.view.currentIndex().isValid():
                self.view.setCurrentIndex(ix)
                self.view.scrollTo(ix)
        self._update_details()

    def selected(self):
        pid = self.model.pid_at(self.view.currentIndex())
        return self.model.procs.get(pid) if pid else None

    def _update_details(self):
        self.details.show_proc(self.selected(), live=self.win.player is None)

    def _live(self):
        """Actions act on running processes: not while a recording is replayed."""
        if getattr(self.win, "player", None) is not None:
            message(self, False, "This is a recording being replayed. Go back to live data "
                                 "(Back to live) to act on running processes.")
            return False
        return True

    def set_view(self, tree, save=True):
        self.a_tree.setChecked(tree)
        self.a_flat.setChecked(not tree)
        self.view.setRootIsDecorated(tree)
        self.model.set_mode(tree=tree)
        if save:
            self.win.settings["process_view"] = "tree" if tree else "flat"
        if tree:
            self._expanded_once = False

    def toggle_mine(self):
        self.model.set_mode(only_uid=os.getuid() if self.a_mine.isChecked() else None)

    # ---- actions -----------------------------------------------------
    def _act(self, fn, *args):
        if not self._live():
            return
        p = self.selected()
        if p and confirm_signal(self, self.win, fn, p):
            ok, msg = fn(p.pid, *args)
            message(self, ok, msg)

    def end_task(self):
        self._act(procs.terminate)

    def properties(self):
        if not self._live():
            return
        p = self.selected()
        if not p:
            return
        info = procs.details(p.pid)
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle(f"{p.name} ({p.pid})")
        form = QtWidgets.QFormLayout(dlg)
        for k, v in info.items():
            lab = QtWidgets.QLabel(str(v))
            lab.setTextInteractionFlags(E(Qt, "TextInteractionFlag", "TextSelectableByMouse"))
            lab.setWordWrap(True)
            form.addRow(k + ":", lab)
        bb = QtWidgets.QDialogButtonBox(E(QtWidgets.QDialogButtonBox, "StandardButton", "Close"))
        bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        dlg.resize(560, 420)
        exec_(dlg)

    def set_priority(self):
        if not self._live():
            return
        p = self.selected()
        if not p:
            return
        v, ok = QtWidgets.QInputDialog.getInt(self, "Set priority",
                                              f"Nice value for {p.name} (-20 highest, 19 lowest):",
                                              p.nice, -20, 19)
        if ok:
            self._act(procs.renice, v)

    def open_location(self):
        if not self._live():
            return
        p = self.selected()
        if p and p.exe:
            from .. import launcher
            launcher.open_path(os.path.dirname(p.exe))

    def menu(self, pos):
        p = self.selected()
        if not p:
            return
        m = QtWidgets.QMenu(self)
        if getattr(self.win, "player", None) is not None:      # replaying: copy only
            m.addAction("Copy command line",
                        lambda: QtWidgets.QApplication.clipboard().setText(p.cmdline))
            exec_(m, self.view.viewport().mapToGlobal(pos))
            return
        m.addAction("End task", self.end_task)
        m.addAction("Kill (force)", lambda: self._act(procs.kill))
        m.addSeparator()
        m.addAction("Suspend", lambda: self._act(procs.suspend))
        m.addAction("Resume", lambda: self._act(procs.resume))
        m.addAction("Set priority…", self.set_priority)
        m.addSeparator()
        a = m.addAction("Open file location", self.open_location)
        a.setEnabled(bool(p.exe))
        m.addAction("Copy command line",
                    lambda: QtWidgets.QApplication.clipboard().setText(p.cmdline))
        m.addAction("Properties", self.properties)
        exec_(m, self.view.viewport().mapToGlobal(pos))
