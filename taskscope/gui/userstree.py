"""Users page: one row per user, with that user's running processes listed
underneath. CPU and Memory cells are heat-coloured like the Processes page.

Uses the same cheap refresh as the process tree: when no process started or
exited, the view is only repainted; otherwise one layoutChanged with the
persistent indexes remapped keeps selection and expanded users intact.
"""

from .. import fmt
from ..sysinfo import procs, users
from . import theme
from .proctree import TOTAL_MEM, HeatDelegate
from .qt import E, QtCore, QtWidgets, Qt, Signal, exec_
from .widgets import enlarge, message

DISPLAY = E(Qt, "ItemDataRole", "DisplayRole")
ALIGN = E(Qt, "ItemDataRole", "TextAlignmentRole")
BG = E(Qt, "ItemDataRole", "BackgroundRole")
TOOLTIP = E(Qt, "ItemDataRole", "ToolTipRole")
HORIZ = E(Qt, "Orientation", "Horizontal")
RIGHT = E(Qt, "AlignmentFlag", "AlignRight") | E(Qt, "AlignmentFlag", "AlignVCenter")
LEFT = E(Qt, "AlignmentFlag", "AlignLeft") | E(Qt, "AlignmentFlag", "AlignVCenter")

# (title, user-row key, numeric, width)
COLUMNS = [("User", "user", False, 240), ("Sessions", "sessions", True, 80),
           ("Terminals", "ttys", False, 120), ("Processes", "procs", True, 90),
           ("CPU", "cpu", True, 80), ("Memory", "rss", True, 100), ("PID", "pid", True, 80)]
CPU_COL, MEM_COL, PID_COL = 4, 5, 6
USER_BASE = 1 << 30   # internal ids at or above this are users; below are PIDs


class UsersModel(QtCore.QAbstractItemModel):
    refreshed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.users = {}       # id -> summary row
        self.procs = {}       # pid -> Proc
        self.uid_of = {}      # user name -> stable id
        self.children = {0: []}
        self.parent_of = {}
        self.row_of = {}
        self.sort_col, self.sort_desc = 1, True

    # ---- Qt model interface ------------------------------------------
    def index(self, row, column, parent=QtCore.QModelIndex()):
        pid = int(parent.internalId()) if parent.isValid() else 0
        kids = self.children.get(pid, ())
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
        if orientation == HORIZ and role == ALIGN:
            return LEFT
        return None

    def data(self, index, role=DISPLAY):
        iid = int(index.internalId())
        col = index.column()
        if iid >= USER_BASE:
            u = self.users.get(iid)
            if u is None:
                return None
            cpu, rss = u["cpu"], u["rss"]
            if role == DISPLAY:
                k = COLUMNS[col][1]
                if k == "cpu":
                    return fmt.cpu(cpu)
                if k == "rss":
                    return fmt.size(rss)
                return str(u.get(k, ""))
        else:
            p = self.procs.get(iid)
            if p is None:
                return None
            cpu, rss = p.cpu, p.rss
            if role == DISPLAY:
                if col == 0:
                    return p.name
                if col == CPU_COL:
                    return fmt.cpu(cpu)
                if col == MEM_COL:
                    return fmt.size(rss)
                if col == PID_COL:
                    return str(p.pid)
                return ""
            if role == TOOLTIP and col == 0:
                return f"PID {p.pid}\n{p.cmdline or '[' + p.name + ']'}"
        if role == ALIGN:
            return RIGHT if COLUMNS[col][2] else None
        if role == BG:
            if col == CPU_COL:
                return theme.heat(cpu / 20.0) if cpu >= 0.05 else None
            if col == MEM_COL:
                frac = rss / TOTAL_MEM
                return theme.heat(frac * 8) if frac >= 0.01 else None
        return None

    def sort(self, column, order=E(Qt, "SortOrder", "AscendingOrder")):
        self.sort_col = column
        self.sort_desc = order == E(Qt, "SortOrder", "DescendingOrder")
        self._rebuild(force=True)

    # ---- updates -----------------------------------------------------
    def set_procs(self, plist):
        self.procs = {p.pid: p for p in plist}
        rows = users.summary(plist)
        self.users = {}
        for r in rows:
            iid = self.uid_of.setdefault(r["user"], USER_BASE + len(self.uid_of))
            self.users[iid] = r
        self._rebuild()

    def _keys(self):
        c, k = self.sort_col, COLUMNS[self.sort_col][1]
        if COLUMNS[c][2]:
            ukey = lambda i: self.users[i].get(k, 0)
        else:
            ukey = lambda i: str(self.users[i][k]).lower()
        if c == 0:
            pkey = lambda pid: self.procs[pid].name.lower()
        elif c == MEM_COL:
            pkey = lambda pid: self.procs[pid].rss
        elif c == PID_COL:
            pkey = lambda pid: pid
        else:
            pkey = lambda pid: self.procs[pid].cpu
        return ukey, pkey

    def _rebuild(self, force=False):
        ids = {u["user"]: i for i, u in self.users.items()}
        children = {0: list(self.users)}
        parent_of = {i: 0 for i in self.users}
        for pid, p in self.procs.items():
            uid = ids.get(p.user)
            if uid is not None:
                children.setdefault(uid, []).append(pid)
                parent_of[pid] = uid
        ukey, pkey = self._keys()
        children[0].sort(key=ukey, reverse=self.sort_desc)
        for i, kids in children.items():
            if i:
                # names read best A→Z; numbers read best biggest first
                kids.sort(key=pkey, reverse=self.sort_desc if self.sort_col in (0, PID_COL) else True)
        if not force and children == self.children:
            self.refreshed.emit()
            return
        self.layoutAboutToBeChanged.emit()
        old = self.persistentIndexList()
        row_of = {}
        for kids in children.values():
            for n, iid in enumerate(kids):
                row_of[iid] = n
        new = []
        for idx in old:
            iid = int(idx.internalId())
            r = row_of.get(iid)
            new.append(self.createIndex(r, idx.column(), iid) if r is not None else QtCore.QModelIndex())
        self.children, self.parent_of, self.row_of = children, parent_of, row_of
        self.changePersistentIndexList(old, new)
        self.layoutChanged.emit()

    def proc_at(self, index):
        if not index.isValid():
            return None
        return self.procs.get(int(index.internalId()))


class UsersView(QtWidgets.QTreeView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.model_ = UsersModel(self)
        self.setModel(self.model_)
        enlarge(self)
        self.setUniformRowHeights(True)
        self.setSortingEnabled(True)
        self.setAlternatingRowColors(True)
        self.setAllColumnsShowFocus(True)
        self.setIndentation(16)
        hdr = self.header()
        hdr.setStretchLastSection(False)   # a full-width heat bar is hard to read
        hdr.setSectionsMovable(True)
        hdr.setHighlightSections(False)
        for i, c in enumerate(COLUMNS):
            self.setColumnWidth(i, int(c[3] * 1.12))
        self.heat = HeatDelegate(self)
        self.setItemDelegateForColumn(CPU_COL, self.heat)
        self.setItemDelegateForColumn(MEM_COL, self.heat)
        self.model_.refreshed.connect(self.viewport().update)
        self.sortByColumn(1, E(Qt, "SortOrder", "DescendingOrder"))
        self.setContextMenuPolicy(E(Qt, "ContextMenuPolicy", "CustomContextMenu"))
        self.customContextMenuRequested.connect(self.menu)
        self._opened = False

    def set_procs(self, plist):
        self.model_.set_procs(plist)
        if not self._opened and self.model_.users:
            # open the first user (the one with sessions, i.e. you)
            self._opened = True
            self.expand(self.model_.index(0, 0))

    def _act(self, fn):
        if getattr(self, "win", None) is not None and self.win.player is not None:
            message(self, False, "This is a recording being replayed. Go back to live data "
                                 "(Back to live) to act on running processes.")
            return
        p = self.model_.proc_at(self.currentIndex())
        from .proctree import confirm_signal
        if p and confirm_signal(self, self.win, fn, p):
            ok, msg = fn(p.pid)
            message(self, ok, msg)

    def menu(self, pos):
        p = self.model_.proc_at(self.currentIndex())
        m = QtWidgets.QMenu(self)
        if p:
            m.addAction("End task", lambda: self._act(procs.terminate))
            m.addAction("Kill (force)", lambda: self._act(procs.kill))
            m.addSeparator()
            m.addAction("Copy command line",
                        lambda: QtWidgets.QApplication.clipboard().setText(p.cmdline))
            m.addSeparator()
        m.addAction("Expand all", self.expandAll)
        m.addAction("Collapse all", self.collapseAll)
        exec_(m, self.viewport().mapToGlobal(pos))
