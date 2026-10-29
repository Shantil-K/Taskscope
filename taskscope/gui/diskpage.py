"""Disk Space page: pick a drive or folder, scan it, then explore where the
space went in a folder tree, a file-type list, a treemap and a searchable
list of the largest (or duplicated) files.

Nothing is read until Scan is pressed. The scan runs in a worker thread and
the window stays responsive; results stay in memory until the next scan.
"""

import os
import subprocess
import threading
import time

from .. import fmt
from ..sysinfo import diskscan, diskspace
from . import theme
from .diskmap import Treemap, ext_colors
from .qt import E, QtCore, QtGui, QtWidgets, Qt, exec_
from .widgets import Column, Table, fit_columns, message, run_async

DISPLAY = E(Qt, "ItemDataRole", "DisplayRole")
ALIGN = E(Qt, "ItemDataRole", "TextAlignmentRole")
DECOR = E(Qt, "ItemDataRole", "DecorationRole")
TOOLTIP = E(Qt, "ItemDataRole", "ToolTipRole")
HORIZ = E(Qt, "Orientation", "Horizontal")
RIGHT = E(Qt, "AlignmentFlag", "AlignRight") | E(Qt, "AlignmentFlag", "AlignVCenter")
LEFT = E(Qt, "AlignmentFlag", "AlignLeft") | E(Qt, "AlignmentFlag", "AlignVCenter")
FILE_BASE = 1 << 30      # tree ids: folders are 1..n, files FILE_BASE + index


def _when(t):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else ""


# ------------------------------------------------------------------ folder tree

class DirModel(QtCore.QAbstractItemModel):
    """Folders and files of a scan as a tree, children sorted by a column."""
    COLS = ["Folder", "% of Parent", "Size", "Allocated", "Items", "Files", "Folders", "Modified"]
    NUMERIC = {1, 2, 3, 4, 5, 6}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.res = None
        self.kids = {}          # id -> [child ids] (built when first needed)
        self.row_of = {}
        self.sort_col, self.sort_desc = 3, True
        style = QtWidgets.QApplication.style()
        from .icons import folder_icon
        self.dir_icon = folder_icon()
        self.file_icon = style.standardIcon(E(QtWidgets.QStyle, "StandardPixmap", "SP_FileIcon"))

    def set_result(self, res):
        self.beginResetModel()
        self.res = res
        self.kids, self.row_of = {}, {0: 0, 1: 0}
        self.endResetModel()

    # ---- helpers ------------------------------------------------------------
    def _vals(self, iid):
        """(name, size, alloc, items, files, folders, mtime, parent alloc)."""
        r = self.res
        if iid >= FILE_BASE:
            f = iid - FILE_BASE
            return (r.name(f), r.fsize[f], r.falloc[f], None, None, None, r.fmtime[f],
                    r.dalloc[r.fparent[f]])
        d = iid - 1
        pa = r.dalloc[r.dparent[d]] if d else r.dalloc[0]
        return (r.root if d == 0 else r.dname[d], r.dsize[d], r.dalloc[d],
                r.dfiles[d] + r.dfolders[d], r.dfiles[d], r.dfolders[d], r.dmtime[d], pa)

    def _key(self):
        c = self.sort_col
        if c == 0:
            return lambda i: self._vals(i)[0].lower()
        pos = {1: 2, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 7: 6}[c]
        return lambda i: (lambda v: -1 if v is None else v)(self._vals(i)[pos])

    def children(self, iid):
        lst = self.kids.get(iid)
        if lst is None:
            if iid == 0:
                lst = [1] if self.res else []
            elif iid >= FILE_BASE:
                lst = []
            else:
                d = iid - 1
                dirs = [s + 1 for s in self.res.subfolders(d)]
                files = [FILE_BASE + f for f in self.res.files_in(d)]
                key = self._key()
                dirs.sort(key=key, reverse=self.sort_desc)
                files.sort(key=key, reverse=self.sort_desc)
                lst = dirs + files            # folders first, like a file manager
            self.kids[iid] = lst
            for i, c in enumerate(lst):
                self.row_of[c] = i
        return lst

    def parent_id(self, iid):
        if iid == 1:
            return 0
        if iid >= FILE_BASE:
            return self.res.fparent[iid - FILE_BASE] + 1
        return self.res.dparent[iid - 1] + 1

    def index_of(self, iid, col=0):
        if iid == 0:
            return QtCore.QModelIndex()
        self.children(self.parent_id(iid))
        return self.createIndex(self.row_of.get(iid, 0), col, iid)

    def resort(self):
        """Re-sort every list already built, keeping selection and expanded folders."""
        self.layoutAboutToBeChanged.emit()
        old = self.persistentIndexList()
        key = self._key()
        for iid, lst in self.kids.items():
            if iid in (0,) or not lst:
                continue
            dirs = sorted((c for c in lst if c < FILE_BASE), key=key, reverse=self.sort_desc)
            files = sorted((c for c in lst if c >= FILE_BASE), key=key, reverse=self.sort_desc)
            self.kids[iid] = dirs + files
            for i, c in enumerate(self.kids[iid]):
                self.row_of[c] = i
        new = [self.createIndex(self.row_of.get(int(i.internalId()), 0), i.column(), i.internalId())
               for i in old]
        self.changePersistentIndexList(old, new)
        self.layoutChanged.emit()

    # ---- Qt interface ---------------------------------------------------------
    def index(self, row, column, parent=QtCore.QModelIndex()):
        pid = int(parent.internalId()) if parent.isValid() else 0
        lst = self.children(pid)
        if 0 <= row < len(lst):
            return self.createIndex(row, column, lst[row])
        return QtCore.QModelIndex()

    def parent(self, index=None):
        if index is None or not index.isValid():
            return QtCore.QModelIndex()
        iid = int(index.internalId())
        if iid == 1:
            return QtCore.QModelIndex()
        pid = self.parent_id(iid)
        return self.createIndex(self.row_of.get(pid, 0), 0, pid)

    def rowCount(self, parent=QtCore.QModelIndex()):
        if self.res is None:
            return 0
        if not parent.isValid():
            return 1
        if parent.column() != 0:
            return 0
        iid = int(parent.internalId())
        if iid >= FILE_BASE:
            return 0
        lst = self.kids.get(iid)
        if lst is not None:
            return len(lst)
        d = iid - 1
        return len(self.res.subfolders(d)) + self.res.file_count(d)

    def hasChildren(self, parent=QtCore.QModelIndex()):
        return self.rowCount(parent) > 0

    def columnCount(self, parent=QtCore.QModelIndex()):
        return len(self.COLS)

    def headerData(self, section, orientation, role=DISPLAY):
        if orientation == HORIZ and role == DISPLAY:
            return self.COLS[section]
        if orientation == HORIZ and role == ALIGN:
            return LEFT
        return None

    def data(self, index, role=DISPLAY):
        if not index.isValid() or self.res is None:
            return None
        iid, col = int(index.internalId()), index.column()
        if role == DISPLAY:
            name, size, alloc, items, files, folders, mtime, pa = self._vals(iid)
            if col == 0:
                return name
            if col == 1:
                return f"{alloc * 100.0 / pa:.1f} %" if pa else ""
            if col == 2:
                return fmt.size(size)
            if col == 3:
                return fmt.size(alloc)
            if col in (4, 5, 6):
                v = (items, files, folders)[col - 4]
                return "" if v is None else f"{v:,}"
            if col == 7:
                return time.strftime("%Y-%m-%d", time.localtime(mtime)) if mtime else ""
        if role == ALIGN and col in self.NUMERIC:
            return RIGHT
        if role == DECOR and col == 0:
            return self.file_icon if iid >= FILE_BASE else self.dir_icon
        if role == E(Qt, "ItemDataRole", "UserRole") and col == 1:
            v = self._vals(iid)
            return v[2] / v[7] if v[7] else 0.0
        return None

    def sort(self, column, order=E(Qt, "SortOrder", "AscendingOrder")):
        self.sort_col = column
        self.sort_desc = order == E(Qt, "SortOrder", "DescendingOrder")
        if self.res is not None:
            self.resort()


class BarDelegate(QtWidgets.QStyledItemDelegate):
    """Percent column: a bar behind the number."""

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        frac = index.data(E(Qt, "ItemDataRole", "UserRole")) or 0.0
        r = option.rect.adjusted(2, 3, -2, -3)
        col = QtGui.QColor(theme.current["accent"])
        col.setAlpha(110)
        painter.save()
        painter.fillRect(QtCore.QRectF(r.x(), r.y(), r.width() * min(1.0, frac), r.height()), col)
        painter.restore()


class SwatchDelegate(QtWidgets.QStyledItemDelegate):
    """Colour square for the file-type list."""

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        view = self.parent()
        row = view.model_.row_at(index.row())
        if row is None:
            return
        r = option.rect
        side = min(r.height() - 8, 14)
        painter.fillRect(QtCore.QRect(r.center().x() - side // 2, r.center().y() - side // 2,
                                      side, side), row["color"])


class EmptyHint(QtWidgets.QWidget):
    """Placeholder for an empty list: faint 'ghost' rows laid out in the
    list's own columns, with a short note in the middle. Shown until there
    is data."""

    def __init__(self, view, title, text, style="table"):
        super().__init__(view.viewport())
        self.view, self.title, self.text, self.style_ = view, title, text, style
        self.setAttribute(E(Qt, "WidgetAttribute", "WA_TransparentForMouseEvents"))
        view.viewport().installEventFilter(self)
        hdr = self._header()
        hdr.sectionResized.connect(lambda *_: self.update())
        hdr.sectionMoved.connect(lambda *_: self.update())
        self._place()

    def _header(self):
        return self.view.header() if isinstance(self.view, QtWidgets.QTreeView) else self.view.horizontalHeader()

    def _place(self):
        self.setGeometry(self.view.viewport().rect())

    def eventFilter(self, obj, ev):
        if ev.type() == E(QtCore.QEvent, "Type", "Resize"):
            self._place()
        return False

    def paintEvent(self, _e):
        p = QtGui.QPainter(self)
        p.setRenderHint(E(QtGui.QPainter, "RenderHint", "Antialiasing"))
        hdr = self._header()
        cols = [(hdr.sectionViewportPosition(hdr.logicalIndex(v)), hdr.sectionSize(hdr.logicalIndex(v)), v)
                for v in range(hdr.count()) if not hdr.isSectionHidden(hdr.logicalIndex(v))]
        ink = QtGui.QColor(theme.current["text"])
        alt = QtGui.QColor(theme.current["alt"])
        rh = 24
        top = 30                                        # first line holds the note
        p.setPen(E(Qt, "PenStyle", "NoPen"))
        for r in range((self.height() - top) // rh + 1):
            y = top + r * rh
            if r % 2:
                p.fillRect(QtCore.QRectF(0, y, self.width(), rh), alt)
            fade = max(0.25, 1.0 - r / 14)                  # rows fade out further down
            for x, w, vis in cols:
                if w < 14:
                    continue
                seed = (r * 37 + vis * 101) % 97
                c = QtGui.QColor(ink)
                c.setAlpha(int((26 + seed % 12) * fade))
                p.setBrush(c)
                inset = 8
                if vis == 0 and self.style_ == "tree":
                    depth = 0 if r == 0 else 1 + (r * 7 % 3 == 0)
                    inset = 8 + depth * 16
                    p.drawRoundedRect(QtCore.QRectF(x + inset, y + 6, 12, 12), 2, 2)   # folder icon
                    inset += 18
                if vis == 0 and self.style_ == "types":
                    col = QtGui.QColor(("#e8b100", "#e05a00", "#8f3fd6", "#2ea043", "#d6336c", "#1f8fff")[r % 6])
                    col.setAlpha(int(70 * fade))
                    p.setBrush(col)
                    p.drawRoundedRect(QtCore.QRectF(x + 6, y + 6, 12, 12), 2, 2)
                    continue
                frac = 0.35 + (seed % 50) / 100.0
                bw = max(10, (w - inset - 8) * frac)
                p.drawRoundedRect(QtCore.QRectF(x + inset, y + 9, bw, 7), 3.5, 3.5)
        # the note: one line above the ghost rows, nothing drawn over them
        f = QtGui.QFont(self.font())
        f.setBold(True)
        fm_b, fm = QtGui.QFontMetrics(f), QtGui.QFontMetrics(self.font())
        adv = lambda m, t: m.horizontalAdvance(t) if hasattr(m, "horizontalAdvance") else m.width(t)  # noqa: E731
        mid = E(Qt, "AlignmentFlag", "AlignLeft") | E(Qt, "AlignmentFlag", "AlignVCenter")
        p.setFont(f)
        p.setPen(QtGui.QColor(theme.current["text"]))
        p.drawText(QtCore.QRectF(10, 0, self.width() - 20, top), mid, self.title)
        x = 10 + adv(fm_b, self.title) + 10
        p.setFont(self.font())
        p.setPen(QtGui.QColor(theme.current["muted"]))
        p.drawText(QtCore.QRectF(x, 0, max(0, self.width() - x - 10), top), mid,
                   fm.elidedText(self.text, E(Qt, "TextElideMode", "ElideRight"), int(max(0, self.width() - x - 10))))
        p.end()


# ------------------------------------------------------------------ the page

class DiskSpacePage(QtWidgets.QWidget):
    title, icon, live = "Disk Space", "disk", False

    DUPS = [("Don't look for duplicates", None), ("Locate by name and size", "name_size"),
            ("Locate by name, size and date", "name_size_date")]

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.res = None
        self.colors = {}
        self._thread = None
        self._cancel = None
        self._seen = 0
        self._result = None
        self._mime = None
        self._types = {}

        self.toolbar = QtWidgets.QWidget()
        tl = QtWidgets.QHBoxLayout(self.toolbar)
        tl.setContentsMargins(0, 0, 0, 0)
        self.export_btn = QtWidgets.QPushButton("Export CSV…")
        self.export_btn.setToolTip("Save every scanned folder and file to a CSV file")
        self.export_btn.setEnabled(False)
        self.export_btn.clicked.connect(self.export)
        tl.addWidget(self.export_btn)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 4)
        lay.setSpacing(6)
        lay.addLayout(self._top())

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self._tree_tab(), "Tree View")
        self.tabs.addTab(self._file_tab(), "File View")
        lay.addWidget(self.tabs, 1)
        # Before a scan the views keep their layout and show what will appear
        # where (an outline of the map, hints in the empty lists).
        self._hints = [
            EmptyHint(self.tree, "Not scanned yet",
                      "Press Scan to list every folder and file here, largest first.", "tree"),
            EmptyHint(self.ext_table, "File types", "Which kinds of files use the most space.", "types"),
            EmptyHint(self.files, "Largest files and duplicates",
                      "After a scan, search all files by name or path, or find duplicate copies."),
        ]

        self.status = QtWidgets.QLabel("")
        self.status.setProperty("muted", True)
        lay.addWidget(self.status)

        self._poll = QtCore.QTimer(self)
        self._poll.timeout.connect(self._check_scan)
        self._search_timer = QtCore.QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.timeout.connect(self.run_search)
        self._fill_targets()

    # ---- layout -------------------------------------------------------------
    def _top(self):
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)
        left = QtWidgets.QFrame()
        left.setObjectName("Card")
        g = QtWidgets.QGridLayout(left)
        g.setContentsMargins(12, 8, 12, 8)
        g.setVerticalSpacing(6)
        g.setColumnStretch(1, 1)
        g.addWidget(QtWidgets.QLabel("Select:"), 0, 0)
        self.target = QtWidgets.QComboBox()
        self.target.setMinimumWidth(160)
        self.target.activated.connect(self._target_changed)
        g.addWidget(self.target, 0, 1)
        self.scan_btn = QtWidgets.QPushButton("▶  Scan")
        self.scan_btn.setObjectName("Primary")       # the main action: solid accent colour
        self.scan_btn.setDefault(True)
        self.scan_btn.setMinimumWidth(80)
        self.scan_btn.setCursor(E(Qt, "CursorShape", "PointingHandCursor"))
        self.scan_btn.clicked.connect(self.toggle_scan)
        g.addWidget(self.scan_btn, 0, 2)
        self.scan_msg = QtWidgets.QLabel("Not scanned yet")
        self.scan_msg.setProperty("muted", True)
        g.addWidget(self.scan_msg, 1, 0, 1, 3)
        self.bar = QtWidgets.QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        g.addWidget(self.bar, 2, 0, 1, 3)
        row.addWidget(left, 3)

        right = QtWidgets.QFrame()
        right.setObjectName("Card")
        sg = QtWidgets.QGridLayout(right)
        sg.setContentsMargins(14, 8, 14, 8)
        sg.setHorizontalSpacing(14)
        sg.setVerticalSpacing(3)
        self.sum = {}
        for i, (k, t) in enumerate((("sel", "Selection"), ("fs", "File system"),
                                    ("total", "Total space"), ("used", "Space used"),
                                    ("free", "Space free"), ("reserved", "Reserved"))):
            cap = QtWidgets.QLabel(t)
            cap.setObjectName("StatCaption")
            val = QtWidgets.QLabel("")
            val.setTextInteractionFlags(E(Qt, "TextInteractionFlag", "TextSelectableByMouse"))
            sg.addWidget(cap, i % 3, (i // 3) * 2)
            sg.addWidget(val, i % 3, (i // 3) * 2 + 1)
            self.sum[k] = val
        sg.setColumnStretch(1, 1)
        sg.setColumnStretch(3, 1)
        row.addWidget(right, 2)
        return row

    def scan_path(self, path):
        i = self.target.findData(path)
        if i < 0:
            self.target.insertItem(0, path, path)
            i = 0
        self.target.setCurrentIndex(i)
        self._summary()
        if self._thread is None:
            self.toggle_scan()

    def _scan_button(self, scanning):
        self.scan_btn.setText("■  Stop" if scanning else "▶  Scan")
        self.scan_btn.setProperty("danger", scanning)
        self.scan_btn.style().unpolish(self.scan_btn)     # pick up the red style
        self.scan_btn.style().polish(self.scan_btn)

    def _show_views(self, on):
        """on: there is scan data (or a live preview) to show."""
        for h in self._hints:
            h.setVisible(not on)

    def _tree_tab(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(0, 6, 0, 0)
        vsplit = QtWidgets.QSplitter(E(Qt, "Orientation", "Vertical"))
        hsplit = QtWidgets.QSplitter(E(Qt, "Orientation", "Horizontal"))

        self.model = DirModel(self)
        self.tree = QtWidgets.QTreeView()
        self.tree.setModel(self.model)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setAllColumnsShowFocus(True)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(3, E(Qt, "SortOrder", "DescendingOrder"))
        self.tree.setItemDelegateForColumn(1, BarDelegate(self.tree))
        self.tree.setContextMenuPolicy(E(Qt, "ContextMenuPolicy", "CustomContextMenu"))
        self.tree.customContextMenuRequested.connect(
            lambda pos: self._menu_for_index(self.tree.indexAt(pos), self.tree.viewport().mapToGlobal(pos)))
        self.tree.doubleClicked.connect(lambda idx: self._open(*self._item(idx)))
        hdr = self.tree.header()
        hdr.setHighlightSections(False)
        for c, wd in enumerate((300, 84, 78, 82, 74, 70, 64, 92)):
            self.tree.setColumnWidth(c, wd)
        # Folder column takes the spare width but never shrinks below 260 px
        # (names stay readable; a narrow window scrolls instead).
        fit_columns(self.tree, (0,), 260)
        self.tree.selectionModel().currentChanged.connect(lambda cur, _: self._tree_selected(cur))
        hsplit.addWidget(self.tree)

        self.ext_table = Table([Column("", "swatch", lambda v: "", width=24),
                                Column("Extension", "ext", width=74),
                                Column("Percent", "pct", lambda v: f"{v:.1f} %", numeric=True, width=60),
                                Column("Size", "size", fmt.size, numeric=True, width=72),
                                Column("Allocated", "alloc", fmt.size, numeric=True, width=76),
                                Column("Files", "files", lambda v: f"{v:,}", numeric=True, width=62)],
                               lambda r: r["idx"])
        self.ext_table.setItemDelegateForColumn(0, SwatchDelegate(self.ext_table))
        self.ext_table.sortByColumn(4, E(Qt, "SortOrder", "DescendingOrder"))
        self.ext_table.selectionModel().selectionChanged.connect(lambda *_: self._ext_selected())
        self.ext_table.setToolTip("Select a file type to highlight it in the map; click again to clear")
        self.ext_table.clicked.connect(self._ext_clicked)
        hsplit.addWidget(self.ext_table)
        hsplit.setStretchFactor(0, 3)
        hsplit.setStretchFactor(1, 2)
        hsplit.setSizes([800, 380])
        fit_columns(self.ext_table, (1,), 60)

        box = QtWidgets.QWidget()
        bl = QtWidgets.QVBoxLayout(box)
        bl.setContentsMargins(0, 4, 0, 0)
        bl.setSpacing(2)
        self.map_title = QtWidgets.QLabel("")
        self.map_title.setAlignment(E(Qt, "AlignmentFlag", "AlignCenter"))
        self.map_title.setProperty("muted", True)
        bl.addWidget(self.map_title)
        self.map = Treemap()
        self.map.clicked.connect(self._map_clicked)
        self.map.activated.connect(self._open)
        self.map.context.connect(lambda kind, idx, gp: self._menu(kind, idx, gp))
        bl.addWidget(self.map, 1)

        vsplit.addWidget(hsplit)
        vsplit.addWidget(box)
        vsplit.setStretchFactor(0, 1)
        vsplit.setStretchFactor(1, 1)
        vsplit.setSizes([300, 300])
        v.addWidget(vsplit)
        return w

    def _file_tab(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(0, 6, 0, 0)
        v.setSpacing(6)
        opts = QtWidgets.QHBoxLayout()
        opts.setSpacing(12)
        self.m_name = QtWidgets.QRadioButton("Match file name only")
        self.m_path = QtWidgets.QRadioButton("Match entire path")
        self.m_name.setChecked(True)
        self.with_dirs = QtWidgets.QCheckBox("Folders")
        self.with_dirs.setToolTip("Also list folders")
        self.limit = QtWidgets.QComboBox()
        for n in (100, 1000, 10000, 100000):
            self.limit.addItem(f"{n:,}", n)
        self.limit.setCurrentIndex(1)
        self.dups = QtWidgets.QComboBox()
        for t, _ in self.DUPS:
            self.dups.addItem(t)
        self.dups_only = QtWidgets.QCheckBox("Duplicates only")
        for wd in (self.m_name, self.m_path, self.with_dirs):
            opts.addWidget(wd)
        opts.addWidget(QtWidgets.QLabel("Max files:"))
        opts.addWidget(self.limit)
        opts.addStretch(1)
        v.addLayout(opts)
        opts2 = QtWidgets.QHBoxLayout()            # second row, so narrow windows still fit
        opts2.setSpacing(12)
        opts2.addWidget(QtWidgets.QLabel("Duplicate files:"))
        opts2.addWidget(self.dups)
        opts2.addWidget(self.dups_only)
        opts2.addStretch(1)
        v.addLayout(opts2)
        srow = QtWidgets.QHBoxLayout()
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Search files — plain text, or wildcards like *.iso or report-??.pdf")
        self.search.setClearButtonEnabled(True)
        srow.addWidget(self.search, 1)
        v.addLayout(srow)
        for sig in (self.search.textChanged, self.m_name.toggled, self.with_dirs.toggled,
                    self.limit.currentIndexChanged, self.dups.currentIndexChanged,
                    self.dups_only.toggled):
            sig.connect(lambda *_: self._search_timer.start(350))

        self.files = Table([Column("File name", "name", width=240), Column("Path", "path", width=380),
                            Column("% of total", "pct", lambda v: f"{v:.1f} %", numeric=True, width=80),
                            Column("Size", "size", fmt.size, numeric=True, width=84),
                            Column("Allocated", "alloc", fmt.size, numeric=True, width=84),
                            Column("Modified", "mtime", _when, width=130),
                            Column("Dup count", "dup_count", lambda v: f"{v}" if v else "", numeric=True, width=80),
                            Column("Dup size", "dup_size", lambda v: fmt.size(v) if v else "", numeric=True, width=84)],
                           lambda r: (r["kind"], r["idx"]))
        self.files.sortByColumn(4, E(Qt, "SortOrder", "DescendingOrder"))
        fit_columns(self.files, (1,), 160)
        self.files.doubleClicked.connect(lambda *_: self._open_row())
        self.files.customContextMenuRequested.connect(self._files_menu)
        self.files.selectionModel().selectionChanged.connect(lambda *_: self._file_selected())
        v.addWidget(self.files, 1)
        self.file_msg = QtWidgets.QLabel("")
        self.file_msg.setProperty("muted", True)
        v.addWidget(self.file_msg)
        return w

    # ---- target and summary -------------------------------------------------------
    def _fill_targets(self):
        self.target.clear()
        home = os.path.expanduser("~")
        mounts = diskspace.list_mounts()
        root_first = os.geteuid() == 0
        if not root_first:
            self.target.addItem(f"{home}  (home folder)", home)
        for m in mounts:
            self.target.addItem(f"{m['mount']}  ({m['device']}, {fmt.size(m['total'])})", m["mount"])
        if root_first:
            self.target.addItem(f"{home}  (home folder)", home)
        self.target.addItem("Choose a folder…", None)
        self.target.setCurrentIndex(0)
        self._summary()

    def _target_changed(self, i):
        if self.target.itemData(i) is None:
            d = QtWidgets.QFileDialog.getExistingDirectory(self, "Folder to scan", os.path.expanduser("~"))
            if d:
                self.target.insertItem(i, d, d)
                self.target.setCurrentIndex(i)
            else:
                self.target.setCurrentIndex(0)
        self._summary()

    def _path(self):
        return self.target.currentData() or os.path.expanduser("~")

    def _summary(self):
        path = self._path()
        try:
            st = os.statvfs(path)
        except OSError:
            return
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        reserved = (st.f_bfree - st.f_bavail) * st.f_frsize
        used = total - st.f_bfree * st.f_frsize
        pct = lambda v: f"  ({v * 100.0 / total:.1f}%)" if total else ""   # noqa: E731
        mount = next((m for m in sorted(diskspace.list_mounts(), key=lambda m: -len(m["mount"]))
                      if path == m["mount"] or path.startswith(m["mount"].rstrip("/") + "/")), None)
        self.sum["sel"].setText(path)
        if self.model.res is None and self._thread is None:
            self.map_title.setText(f"{path}  —  not scanned yet. After a scan, every folder and file "
                                   "appears here as a box sized by the space it uses.")
        self.sum["fs"].setText(f"{mount['device']} ({mount['fs']})" if mount else "")
        self.sum["total"].setText(fmt.size(total))
        self.sum["used"].setText(fmt.size(used) + pct(used))
        self.sum["free"].setText(fmt.size(free) + pct(free))
        self.sum["reserved"].setText(fmt.size(reserved))

    def activate(self):
        self._summary()


    def refresh(self, snap):
        pass

    # ---- scanning ----------------------------------------------------------------
    def toggle_scan(self):
        if self._thread is not None:
            self._cancel.set()
            self.scan_btn.setEnabled(False)
            return
        path = self._path()
        # Free the previous results before building new ones.
        self.res = None
        self._show_views(False)
        self.model.set_result(None)
        self.map.set_result(None, {})
        self.ext_table.set_rows([])
        self.files.set_rows([])
        self.export_btn.setEnabled(False)
        self._seen = 0
        self._result = None
        self._partial = None
        self._preview = None
        self._cancel = threading.Event()
        # The direct ext4 read is an experimental option (Settings).
        method = "auto" if self.win.settings.get("exp_ext4_scan") else "normal"
        is_mount = any(m["mount"] == path for m in diskspace.list_mounts())
        est = diskscan.estimate_entries(path) if is_mount else 0
        self.bar.setRange(0, est if est else 0)     # 0..0 = busy animation
        self._scan_button(scanning=True)
        self.scan_msg.setText(f"Scanning {path}…")

        def work():
            try:
                self._result = diskscan.scan(path, progress=self._set_seen, cancel=self._cancel,
                                             method=method, partial=self._set_partial)
            except diskscan.Cancelled:
                self._result = "cancelled"
            except Exception as e:     # report, don't lose it
                self._result = e
        self._thread = threading.Thread(target=work, daemon=True)
        self._thread.start()
        self._poll.start(150)

    def _set_seen(self, n):
        self._seen = n

    def _set_partial(self, preview, totals, exts):
        self._partial = (preview, totals, exts)     # picked up by _check_scan

    def _show_partial(self):
        """Show the live preview: top folders with sizes that keep growing."""
        part, self._partial = self._partial, None
        if part is None:
            return
        p, (ds, da, df, dd), (names, stats) = part
        p.dsize, p.dalloc, p.dfiles, p.dfolders = ds, da, df, dd
        p.exts, p.ext_stats = names, stats
        colors = ext_colors(p)
        if self._preview is not p:
            self._preview = p
            self._show_views(True)
            self.model.set_result(p)
            self.tree.expand(self.model.index_of(1))
            self.map.set_result(p, colors)
        else:
            self.model.resort()
            self.map.colors = colors
            self.map.invalidate()
        self.colors = colors
        self.map_title.setText(f"{p.root}  —  {fmt.size(p.total_alloc)} so far (scanning…)")
        self._fill_exts(p)

    def _check_scan(self):
        if self._thread is not None and self._thread.is_alive():
            if self.bar.maximum():
                self.bar.setValue(min(self._seen, self.bar.maximum()))
            self.scan_msg.setText(f"Scanning {self._path()}… {self._seen:,} items")
            self._show_partial()
            return
        self._partial = None
        self._preview = None
        self._poll.stop()
        self._thread = None
        self._scan_button(scanning=False)
        self.scan_btn.setEnabled(True)
        self.bar.setRange(0, 1)
        r = self._result
        self._result = None
        if r == "cancelled" or r is None:
            self.bar.setValue(0)
            self.scan_msg.setText("Scan stopped")
            if self.model.res is not None:     # a live preview is still on screen
                self.map_title.setText(f"{self.model.res.root}  —  stopped; showing what was "
                                       "found so far (sizes incomplete)")
            return
        if isinstance(r, Exception):
            self.bar.setValue(0)
            self.scan_msg.setText(f"Scan failed: {r}")
            return
        self.bar.setValue(1)
        self.show_result(r)

    def show_result(self, res):
        self.res = res
        self._show_views(True)
        self.colors = ext_colors(res)
        note = ""
        if res.denied:
            note = (f" · {res.denied:,} folders could not be read"
                    + ("" if os.geteuid() == 0 else " (run as administrator to include them)"))
        how = " · read directly from ext4" if res.backend.startswith("ext4") else ""
        self.scan_msg.setText(f"Scan complete in {res.elapsed:.1f} s · {res.nfiles:,} files, "
                              f"{len(res.dname) - 1:,} folders{how}{note}")
        if res.backend.startswith("ext4"):
            tip = ("Read the file system's own tables straight from the disk "
                   "(fast scan, available when running as administrator on ext4)")
        elif res.backend.startswith("find"):
            tip = "Scanned with GNU find on all CPU cores" + res.backend[4:]
        else:
            tip = "Scanned with Python" + res.backend[6:]
        self.scan_msg.setToolTip(tip)
        self.model.set_result(res)
        self.tree.expand(self.model.index_of(1))
        self.tree.setCurrentIndex(self.model.index_of(1))
        self.map.set_result(res, self.colors)
        self.map_title.setText(f"{res.root}  —  {fmt.size(res.total_alloc)}")
        self._fill_exts()
        self.export_btn.setEnabled(True)
        self._summary()
        self.run_search()

    def _type_name(self, ext):
        if ext == diskscan.NO_EXT:
            return "No extension"
        t = self._types.get(ext)
        if t is None:
            if self._mime is None:
                self._mime = QtCore.QMimeDatabase()
            mt = self._mime.mimeTypeForFile("x" + ext, E(QtCore.QMimeDatabase, "MatchMode",
                                                         "MatchExtension"))
            t = mt.comment() if mt.isValid() and not mt.isDefault() else f"{ext[1:].upper()} file"
            self._types[ext] = t
        return t

    def _fill_exts(self, res=None):
        res = res or self.res
        total = res.total_alloc or 1
        grey = QtGui.QColor("#707070")
        rows = []
        for i, (size, alloc, n) in enumerate(res.ext_stats):
            if n <= 0:
                continue
            rows.append({"idx": i, "swatch": "", "ext": res.exts[i], "type": "",
                         "pct": alloc * 100.0 / total, "size": size, "alloc": alloc, "files": n,
                         "color": self.colors.get(i, grey)})
        rows.sort(key=lambda r: r["alloc"], reverse=True)
        for r in rows[:300]:          # type name as a hover hint, for the ones you'll see
            r["_tips"] = {"ext": self._type_name(r["ext"])}
        self.ext_table.set_rows(rows)

    # ---- selection sync -------------------------------------------------------------
    def _item(self, index):
        if not index.isValid():
            return None, None
        iid = int(index.internalId())
        return ("file", iid - FILE_BASE) if iid >= FILE_BASE else ("dir", iid - 1)

    def _tree_selected(self, cur):
        kind, idx = self._item(cur)
        if kind is None:
            return
        self.map.select(kind, idx)
        self._status(kind, idx)

    def _status(self, kind, idx):
        res = self.model.res
        if res is None or kind is None:
            self.status.setText("")
            return
        if kind == "dir":
            self.status.setText(f"{res.dir_path(idx)}   ·   {res.dfiles[idx]:,} files   ·   "
                                f"Size {fmt.size(res.dsize[idx])}   ·   Allocated {fmt.size(res.dalloc[idx])}")
        else:
            self.status.setText(f"{res.file_path(idx)}   ·   Size {fmt.size(res.fsize[idx])}   ·   "
                                f"Allocated {fmt.size(res.falloc[idx])}")

    def _map_clicked(self, kind, idx):
        self.reveal(kind, idx)

    def reveal(self, kind, idx):
        """Expand the tree down to an item and select it."""
        res = self.model.res
        if res is None:
            return
        d = idx if kind == "dir" else res.fparent[idx]
        for a in res.ancestors(d)[:-1] if kind == "dir" else res.ancestors(d):
            self.tree.expand(self.model.index_of(a + 1))
        iid = idx + 1 if kind == "dir" else FILE_BASE + idx
        ix = self.model.index_of(iid)
        self.tree.setCurrentIndex(ix)
        self.tree.scrollTo(ix)

    def _ext_clicked(self, index):
        r = self.ext_table.model_.row_at(index.row())
        if r is not None and self.map.highlight == r["idx"] and getattr(self, "_ext_same", False):
            self.ext_table.clearSelection()
            self.map.set_highlight(None)
        self._ext_same = True

    def _ext_selected(self):
        self._ext_same = False
        r = self.ext_table.selected()
        self.map.set_highlight(r["idx"] if r else None)

    def _file_selected(self):
        r = self.files.selected()
        if r:
            self._status(r["kind"], r["idx"])

    # ---- file view ---------------------------------------------------------------------
    def run_search(self):
        if self.res is None:
            self.file_msg.setText("The file list is ready when the scan finishes."
                                  if self._thread is not None else "Scan a drive or folder first.")
            return
        res = self.res
        args = (self.search.text(), self.m_path.isChecked(), self.with_dirs.isChecked(),
                self.limit.currentData(), self.DUPS[self.dups.currentIndex()][1],
                self.dups_only.isChecked())
        self.file_msg.setText("Searching…")

        def done(rows):
            if res is not self.res:
                return
            if isinstance(rows, Exception):
                self.file_msg.setText(f"Search failed: {rows}")
                return
            self.files.set_rows(rows)
            if args[4] and args[5]:
                self.files.sortByColumn(7, E(Qt, "SortOrder", "DescendingOrder"))
            n = sum(1 for r in rows if r["kind"] == "file")
            self.file_msg.setText(f"{n:,} files   ·   Total size {fmt.size(sum(r['size'] for r in rows if r['kind'] == 'file'))}"
                                  f"   ·   Allocated {fmt.size(sum(r['alloc'] for r in rows if r['kind'] == 'file'))}")
        run_async(lambda: res.search(*args), done)

    def _open_row(self):
        r = self.files.selected()
        if r:
            self._open(r["kind"], r["idx"])

    def _files_menu(self, pos):
        r = self.files.selected()
        if r:
            self._menu(r["kind"], r["idx"], self.files.viewport().mapToGlobal(pos))

    # ---- actions -----------------------------------------------------------------------
    def _path_of(self, kind, idx):
        return self.res.dir_path(idx) if kind == "dir" else self.res.file_path(idx)

    def _open(self, kind, idx):
        if self.res is None or kind is None:
            return
        from .. import launcher
        launcher.open_path(self._path_of(kind, idx))

    def _open_folder(self, kind, idx):
        p = self._path_of(kind, idx)
        if kind == "file":
            p = os.path.dirname(p)
        from .. import launcher
        launcher.open_path(p)

    def _menu_for_index(self, index, gpos):
        kind, idx = self._item(index)
        if kind:
            self._menu(kind, idx, gpos)

    def _menu(self, kind, idx, gpos):
        if self.res is None:
            return
        m = QtWidgets.QMenu(self)
        m.addAction("Open", lambda: self._open(kind, idx))
        m.addAction("Open containing folder" if kind == "file" else "Open folder",
                    lambda: self._open_folder(kind, idx))
        m.addAction("Copy path", lambda: QtWidgets.QApplication.clipboard().setText(self._path_of(kind, idx)))
        if kind == "dir":
            m.addAction("Show in tree", lambda: (self.tabs.setCurrentIndex(0), self.reveal(kind, idx)))
        else:
            m.addAction("Show in tree", lambda: (self.tabs.setCurrentIndex(0), self.reveal(kind, idx)))
        m.addSeparator()
        a = m.addAction("Move to Trash…", lambda: self.trash(kind, idx))
        a.setEnabled(not (kind == "dir" and idx == 0))
        exec_(m, gpos)

    def trash(self, kind, idx):
        res = self.res
        path = self._path_of(kind, idx)
        size = res.dalloc[idx] if kind == "dir" else res.falloc[idx]
        what = "folder and everything in it" if kind == "dir" else "file"
        btn = QtWidgets.QMessageBox.question(
            self, "Move to Trash", f"Move this {what} to the Trash?\n\n{path}\n\n{fmt.size(size)}")
        if btn != E(QtWidgets.QMessageBox, "StandardButton", "Yes"):
            return
        ok = False
        try:
            ok = bool(QtCore.QFile.moveToTrash(path)) if hasattr(QtCore.QFile, "moveToTrash") else False
        except Exception:
            ok = False
        if not ok:
            try:
                ok = subprocess.run(["gio", "trash", path], capture_output=True, timeout=60).returncode == 0
            except (OSError, subprocess.SubprocessError):
                ok = False
        if not ok:
            message(self, False, f"Could not move {path} to the Trash.\n"
                                 "You may not have permission to delete it.")
            return
        if kind == "dir":
            res.remove_dir(idx)
        else:
            res.remove_file(idx)
        self.model.resort()
        self.map.invalidate()
        self._fill_exts()
        self._summary()
        self.run_search()
        self.win.flash(f"Moved to Trash: {os.path.basename(path)}")

    def export(self):
        if self.res is None:
            return
        default = os.path.join(os.path.expanduser("~"), time.strftime("disk-usage-%Y%m%d-%H%M%S.csv"))
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Export CSV", default, "CSV files (*.csv)")
        if not path:
            return
        self.export_btn.setEnabled(False)
        res = self.res

        def done(r):
            self.export_btn.setEnabled(self.res is not None)
            if isinstance(r, Exception):
                message(self, False, f"Export failed: {r}")
            else:
                self.win.flash(f"Saved {path}")
        run_async(lambda: res.export_csv(path), done)
