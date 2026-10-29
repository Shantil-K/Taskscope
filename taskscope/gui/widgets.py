"""Reusable widgets: history graphs, an incrementally updated table model,
and a tiny helper to run slow calls off the GUI thread."""

from collections import deque
from concurrent.futures import ThreadPoolExecutor

from .qt import E, QtCore, QtGui, QtWidgets, Qt

_pool = ThreadPoolExecutor(max_workers=4)

COLORS = {"cpu": "#3b82f6", "memory": "#a855f7", "disk": "#22c55e",
          "net": "#f59e0b", "gpu": "#ef4444"}

DISPLAY = E(Qt, "ItemDataRole", "DisplayRole")
USER = E(Qt, "ItemDataRole", "UserRole")
ALIGN = E(Qt, "ItemDataRole", "TextAlignmentRole")
TOOLTIP = E(Qt, "ItemDataRole", "ToolTipRole")
DECORATION = E(Qt, "ItemDataRole", "DecorationRole")
FG = E(Qt, "ItemDataRole", "ForegroundRole")
HORIZ = E(Qt, "Orientation", "Horizontal")
RIGHT = E(Qt, "AlignmentFlag", "AlignRight") | E(Qt, "AlignmentFlag", "AlignVCenter")


def run_async(fn, callback, *args):
    """Run fn(*args) in a worker thread and deliver the result on the GUI thread."""
    fut = _pool.submit(fn, *args)

    def poll():
        if fut.done():
            try:
                res = fut.result()
            except Exception as e:  # report errors instead of losing them
                res = e
            callback(res)
        else:
            QtCore.QTimer.singleShot(60, poll)
    QtCore.QTimer.singleShot(30, poll)


# Graph preferences, set from Settings > Graphs.
GRAPH = {"keyed": True,      # each device in its own colour (else the theme accent)
         "dashed": True,     # second series (e.g. network send) dashed
         "smooth": False,    # curved lines instead of straight segments
         "points": 60,       # samples of history kept per graph
         "display": "color", # color | accent | mono (Theme popup)
         "saturation": 11,   # 0..11
         "glow": 0}          # 0..11


def tint(color):
    """A device colour as the Theme popup wants it shown: full colour, the
    accent colour, or grey, with the chosen saturation."""
    from . import theme
    c = QtGui.QColor(color)
    if GRAPH["display"] == "accent" or not GRAPH["keyed"]:
        c = QtGui.QColor(theme.current["accent"])
    if GRAPH["display"] == "mono":
        v = c.valueF()
        return QtGui.QColor.fromHsvF(0, 0, max(0.55, v), c.alphaF())
    sat = max(0, min(11, GRAPH["saturation"])) / 11
    if sat < 1:
        h, s_, v, a = c.getHsvF()
        c = QtGui.QColor.fromHsvF(max(0.0, h), s_ * sat, v, a)
    return c


class Graph(QtWidgets.QWidget):
    """Scrolling history graph with one or two series."""

    def __init__(self, color="#3b82f6", maximum=100.0, points=None, mini=False, parent=None):
        super().__init__(parent)
        points = points or GRAPH["points"]
        self.base_color = QtGui.QColor(color)
        self.color = QtGui.QColor(color)
        self.maximum = maximum          # None -> auto scale
        self.series = [deque([0.0] * points, maxlen=points)]
        self.dashed_second = True
        self.mini = mini
        self.label = ""
        self._bg = None          # cached background + grid, rebuilt on resize/theme change
        self._bg_key = None
        if mini:
            self.setFixedSize(64, 34)
        else:
            self.setMinimumHeight(110)

    def set_points(self, n):
        """Change how much history is kept, keeping what is there."""
        if n == self.series[0].maxlen:
            return
        self.series = [deque([0.0] * max(0, n - len(s)) + list(s)[-n:], maxlen=n) for s in self.series]
        self.update()

    def reset(self):
        """Forget the history (used when replay jumps to another moment)."""
        for q in self.series:
            q.extend([0.0] * q.maxlen)
        self.update()

    def set_series_count(self, n):
        pts = self.series[0].maxlen
        while len(self.series) < n:
            self.series.append(deque([0.0] * pts, maxlen=pts))

    def push(self, *values):
        self.set_series_count(len(values))
        for s, v in zip(self.series, values):
            s.append(float(v or 0.0))
        self.update()

    def _scale(self):
        if self.maximum:
            return self.maximum
        peak = max(max(s) for s in self.series) or 1.0
        # round up to a 1/2/5 step so the axis doesn't jitter
        step = 1.0
        while step * 10 < peak:
            step *= 10
        for m in (1, 2, 5, 10):
            if step * m >= peak:
                return step * m
        return peak

    def _background(self, r):
        base = self.palette().color(E(QtGui.QPalette, "ColorRole", "Base"))
        key = (r.width(), r.height(), base.rgba(), self.color.rgba(), self.devicePixelRatioF())
        if self._bg_key == key:
            return self._bg
        dpr = self.devicePixelRatioF()
        pm = QtGui.QPixmap(int(self.width() * dpr), int(self.height() * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(base)
        p = QtGui.QPainter(pm)
        grid = QtGui.QColor(self.color)
        grid.setAlpha(40)
        p.setPen(QtGui.QPen(grid, 1))
        if not self.mini:
            for i in range(1, 10):
                x = r.left() + r.width() * i / 10
                y = r.top() + r.height() * i / 10
                p.drawLine(QtCore.QPointF(x, r.top()), QtCore.QPointF(x, r.bottom()))
                p.drawLine(QtCore.QPointF(r.left(), y), QtCore.QPointF(r.right(), y))
        p.drawRect(r)
        p.end()
        self._bg, self._bg_key = pm, key
        return pm

    def paintEvent(self, _e):
        from . import theme
        want = tint(self.base_color)
        if want != self.color:
            self.color = want
            self._bg_key = None
        p = QtGui.QPainter(self)
        r = self.rect().adjusted(0, 0, -1, -1)
        p.drawPixmap(0, 0, self._background(r))
        p.setRenderHint(E(QtGui.QPainter, "RenderHint", "Antialiasing"))
        scale = self._scale()
        for idx, s in enumerate(self.series):
            n = len(s)
            if n < 2:
                continue
            pts = [QtCore.QPointF(r.left() + r.width() * i / (n - 1),
                                  r.bottom() - r.height() * min(v, scale) / scale)
                   for i, v in enumerate(s)]
            pen = QtGui.QPen(self.color, 1.4 if self.mini else 1.6)
            if idx == 1 and self.dashed_second and GRAPH["dashed"]:
                pen.setStyle(E(Qt, "PenStyle", "DashLine"))
            line = _path(pts, GRAPH["smooth"])
            if GRAPH["glow"] and not self.mini:
                # soft glow: wide, faint copies of the line underneath
                for k in (3, 2, 1):
                    gc = QtGui.QColor(self.color)
                    gc.setAlpha(min(120, 10 + GRAPH["glow"] * 5))
                    gp = QtGui.QPen(gc, 1.6 + GRAPH["glow"] * 0.55 * k)
                    gp.setCapStyle(E(Qt, "PenCapStyle", "RoundCap"))
                    gp.setJoinStyle(E(Qt, "PenJoinStyle", "RoundJoin"))
                    p.setBrush(E(Qt, "BrushStyle", "NoBrush"))
                    p.setPen(gp)
                    p.drawPath(line)
            if idx == 0:
                area = QtGui.QPainterPath(line)
                area.lineTo(QtCore.QPointF(r.right(), r.bottom()))
                area.lineTo(QtCore.QPointF(r.left(), r.bottom()))
                area.closeSubpath()
                fill = QtGui.QColor(self.color)
                fill.setAlpha(45)
                p.setPen(E(Qt, "PenStyle", "NoPen"))
                p.setBrush(fill)
                p.drawPath(area)
            p.setBrush(E(Qt, "BrushStyle", "NoBrush"))
            p.setPen(pen)
            p.drawPath(line)
        if self.label and not self.mini:
            p.setPen(self.palette().color(E(QtGui.QPalette, "ColorRole", "Text")))
            p.drawText(r.adjusted(6, 4, -6, -4), E(Qt, "AlignmentFlag", "AlignRight"), self.label)
        p.end()


def _path(pts, smooth):
    """Polyline, or a curve through the points (midpoint quadratic smoothing)."""
    path = QtGui.QPainterPath(pts[0])
    if not smooth or len(pts) < 3:
        for q in pts[1:]:
            path.lineTo(q)
        return path
    for i in range(1, len(pts) - 1):
        mid = (pts[i] + pts[i + 1]) / 2
        path.quadTo(pts[i], mid)
    path.lineTo(pts[-1])
    return path


class Column:
    def __init__(self, title, key, fmt=None, numeric=False, width=None, icon=None):
        self.title, self.key, self.fmt, self.numeric, self.width = title, key, fmt, numeric, width
        self.icon = icon          # optional: row -> QIcon shown before the text


class TableModel(QtCore.QAbstractTableModel):
    """Sorting and filtering happen here in plain Python (sub-millisecond for
    thousands of rows) instead of in a QSortFilterProxyModel, which re-sorts
    and re-filters everything on every dataChanged. A refresh whose row order
    is unchanged costs one dataChanged; Qt then asks only for visible cells."""

    def __init__(self, columns, key_fn, parent=None):
        super().__init__(parent)
        self.columns = columns
        self.key_fn = key_fn
        self.all = []
        self.rows = []
        self.keys = []
        self.sort_col, self.sort_desc = -1, False
        self.filter = ""

    def _get(self, row, key):
        return row.get(key) if isinstance(row, dict) else getattr(row, key, None)

    def rowCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QtCore.QModelIndex()):
        return len(self.columns)

    def headerData(self, section, orientation, role=DISPLAY):
        if orientation == HORIZ and role == DISPLAY:
            return self.columns[section].title
        return None

    def _text(self, row, col):
        v = self._get(row, col.key)
        if col.fmt:
            return col.fmt(v)
        if isinstance(v, bool):
            return "Yes" if v else "No"
        return "" if v is None else str(v)

    def data(self, index, role=DISPLAY):
        if role == DISPLAY:
            return self._text(self.rows[index.row()], self.columns[index.column()])
        col = self.columns[index.column()]
        if role == ALIGN:
            return RIGHT if col.numeric else None
        row = self.rows[index.row()]
        if role == DECORATION and col.icon is not None:
            return col.icon(row)
        if role == TOOLTIP:
            tip = row.get("_tips", {}).get(col.key) if isinstance(row, dict) else None
            if tip:
                return tip
            s = self._text(row, col)
            return s if len(s) > 40 else None
        if role == FG and isinstance(row, dict) and col.key in row.get("_muted", ()):
            from . import theme
            return QtGui.QColor(theme.current["muted"])
        return None

    def row_at(self, i):
        return self.rows[i] if 0 <= i < len(self.rows) else None

    # -- view computation --------------------------------------------------
    def _view(self):
        rows = self.all
        if self.filter:
            f = self.filter.lower()
            rows = [r for r in rows if any(f in self._text(r, c).lower() for c in self.columns)]
        if 0 <= self.sort_col < len(self.columns):
            c = self.columns[self.sort_col]
            if c.numeric:
                kf = lambda r: (lambda v: -1e18 if v is None else float(v))(self._get(r, c.key))  # noqa: E731
            else:
                kf = lambda r: (lambda v: "" if v is None else str(v).lower())(self._get(r, c.key))  # noqa: E731
            rows = sorted(rows, key=kf, reverse=self.sort_desc)
        return rows

    def _apply(self, rows):
        keys = [self.key_fn(r) for r in rows]
        if keys == self.keys:
            self.rows = rows
            if rows:
                self.dataChanged.emit(self.index(0, 0), self.index(len(rows) - 1, len(self.columns) - 1))
            return
        # Order or membership changed: move persistent indexes (selection,
        # current item) to wherever their row went, then announce the layout.
        self.layoutAboutToBeChanged.emit()
        old = self.persistentIndexList()
        pos = {k: i for i, k in enumerate(keys)}
        targets = []
        for idx in old:
            k = self.keys[idx.row()] if idx.row() < len(self.keys) else None
            i = pos.get(k)
            targets.append(self.index(i, idx.column()) if i is not None else QtCore.QModelIndex())
        self.rows, self.keys = rows, keys
        self.changePersistentIndexList(old, targets)
        self.layoutChanged.emit()

    def update(self, new_rows):
        self.all = list(new_rows)
        self._apply(self._view())

    def set_filter(self, text):
        self.filter = text.strip()
        self._apply(self._view())

    def sort(self, column, order=E(Qt, "SortOrder", "AscendingOrder")):
        self.sort_col = column
        self.sort_desc = order == E(Qt, "SortOrder", "DescendingOrder")
        self._apply(self._view())


class Table(QtWidgets.QTableView):
    """Sortable, filterable view over a TableModel."""

    def __init__(self, columns, key_fn, parent=None):
        super().__init__(parent)
        self.model_ = TableModel(columns, key_fn, self)
        self.setModel(self.model_)
        self.setSortingEnabled(True)
        self.sortByColumn(0, E(Qt, "SortOrder", "AscendingOrder"))
        self.setSelectionBehavior(E(QtWidgets.QAbstractItemView, "SelectionBehavior", "SelectRows"))
        self.setSelectionMode(E(QtWidgets.QAbstractItemView, "SelectionMode", "SingleSelection"))
        self.setAlternatingRowColors(True)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.verticalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(24)
        self.horizontalHeader().setStretchLastSection(True)
        self.horizontalHeader().setSectionsMovable(True)
        self.horizontalHeader().setHighlightSections(False)
        self.horizontalHeader().setDefaultAlignment(
            E(Qt, "AlignmentFlag", "AlignLeft") | E(Qt, "AlignmentFlag", "AlignVCenter"))
        self.setContextMenuPolicy(E(Qt, "ContextMenuPolicy", "CustomContextMenu"))
        for i, c in enumerate(columns):
            if c.width:
                self.setColumnWidth(i, c.width)

    def set_rows(self, rows):
        self.model_.update(rows)

    def set_filter(self, text):
        self.model_.set_filter(text)

    def selected(self):
        idx = self.selectionModel().selectedRows()
        return self.model_.row_at(idx[0].row()) if idx else None


class FilterBar(QtWidgets.QWidget):
    """A search box followed by any number of buttons."""

    def __init__(self, placeholder="Search", parent=None):
        super().__init__(parent)
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.edit = QtWidgets.QLineEdit()
        self.edit.setPlaceholderText(placeholder)
        self.edit.setClearButtonEnabled(True)
        self.edit.setMinimumWidth(140)
        self.edit.setMaximumWidth(260)
        lay.addWidget(self.edit)
        self.layout_ = lay

    def add(self, widget):
        self.layout_.addWidget(widget)
        return widget

    def button(self, text, slot, tip=""):
        b = QtWidgets.QPushButton(text)
        b.clicked.connect(slot)
        if tip:
            b.setToolTip(tip)
        return self.add(b)


class CheckDelegate(QtWidgets.QStyledItemDelegate):
    """A clickable rounded checkbox drawn in the theme's accent colour.
    on_toggle(row, new_value) is called when the user clicks it."""

    def __init__(self, view, on_toggle):
        super().__init__(view)
        self.view = view
        self.on_toggle = on_toggle

    def _box(self, rect):
        side = 16
        c = QtCore.QRectF(rect).center()
        return QtCore.QRectF(round(c.x() - side / 2), round(c.y() - side / 2), side, side)

    def paint(self, painter, option, index):
        super().paint(painter, option, index)          # background / selection only
        from . import theme
        row = self.view.model_.row_at(index.row())
        on = bool(row and row.get(self.view.model_.columns[index.column()].key))
        box = self._box(option.rect)
        painter.save()
        painter.setRenderHint(E(QtGui.QPainter, "RenderHint", "Antialiasing"))
        if on:
            painter.setPen(E(Qt, "PenStyle", "NoPen"))
            painter.setBrush(QtGui.QColor(theme.current["accent"]))
            painter.drawRoundedRect(box, 3.5, 3.5)
            pen = QtGui.QPen(QtGui.QColor("#ffffff"), 2)
            pen.setCapStyle(E(Qt, "PenCapStyle", "RoundCap"))
            pen.setJoinStyle(E(Qt, "PenJoinStyle", "RoundJoin"))
            painter.setPen(pen)
            x, y = box.left(), box.top()
            painter.drawPolyline(QtGui.QPolygonF([QtCore.QPointF(x + 4, y + 8.5),
                                                  QtCore.QPointF(x + 7, y + 11.5),
                                                  QtCore.QPointF(x + 12.5, y + 5)]))
        else:
            painter.setPen(QtGui.QPen(QtGui.QColor(theme.current["muted"]), 1.4))
            painter.setBrush(E(Qt, "BrushStyle", "NoBrush"))
            painter.drawRoundedRect(box.adjusted(0.7, 0.7, -0.7, -0.7), 3.5, 3.5)
        painter.restore()

    def editorEvent(self, event, model, option, index):
        release = E(QtCore.QEvent, "Type", "MouseButtonRelease")
        if event.type() == release and event.button() == E(Qt, "MouseButton", "LeftButton"):
            pos = event.position() if hasattr(event, "position") else event.pos()
            if self._box(option.rect).adjusted(-6, -6, 6, 6).contains(QtCore.QPointF(pos)):
                row = self.view.model_.row_at(index.row())
                if row is not None:
                    self.on_toggle(row, not row.get(self.view.model_.columns[index.column()].key))
                return True
        return False


class _ColumnFitter(QtCore.QObject):
    """Every column can be resized by dragging the line between headers,
    while the "flex" columns soak up the spare width whenever the view (or a
    fixed column) changes size, so the table still fills the window."""

    def __init__(self, view, flex, minimum):
        super().__init__(view)
        self.view, self.flex, self.minimum = view, list(flex), minimum
        self.hdr = view.horizontalHeader() if isinstance(view, QtWidgets.QTableView) else view.header()
        self.hdr.setStretchLastSection(False)
        interactive = E(QtWidgets.QHeaderView, "ResizeMode", "Interactive")
        for i in range(self.hdr.count()):
            self.hdr.setSectionResizeMode(i, interactive)
        self.weights = {c: max(1, self.hdr.sectionSize(c)) for c in self.flex}
        self._busy = False
        self._user_w = None        # viewport width when the user last dragged a flex column
        view.viewport().installEventFilter(self)
        self.hdr.sectionResized.connect(self._resized)

    def eventFilter(self, obj, ev):
        if ev.type() == E(QtCore.QEvent, "Type", "Resize"):
            # A scroll bar appearing changes the width a little; only re-fit
            # after a manual drag when the window itself was resized.
            w = self.view.viewport().width()
            if self._user_w is None or abs(w - self._user_w) > 30:
                self._user_w = None
                self.fit()
        return False

    def _resized(self, idx, _old, new):
        if self._busy:
            return
        if idx in self.flex:
            self.weights[idx] = max(1, new)       # remember the user's choice
            self._user_w = self.view.viewport().width()
        else:
            self.fit()

    def fit(self):
        hdr = self.hdr
        fixed = sum(hdr.sectionSize(c) for c in range(hdr.count())
                    if c not in self.flex and not hdr.isSectionHidden(c))
        avail = self.view.viewport().width() - fixed
        total = sum(self.weights.values()) or 1
        self._busy = True
        try:
            left = avail
            for n, c in enumerate(self.flex):
                w = left if n == len(self.flex) - 1 else int(avail * self.weights[c] / total)
                w = max(self.minimum, w)
                hdr.resizeSection(c, w)
                left -= w
        finally:
            self._busy = False


def fit_columns(view, flex, minimum=60):
    """Make all columns of *view* user-resizable; *flex* columns fill the rest."""
    view._fitter = _ColumnFitter(view, flex, minimum)
    return view._fitter


def enlarge(view, point_size=11.0, row_height=26):
    """Roomier rows and text for the main list pages (Processes, Startup,
    Users, Services)."""
    f = view.font()
    f.setPointSizeF(point_size)
    view.setFont(f)
    if isinstance(view, QtWidgets.QTableView):
        view.horizontalHeader().setFont(f)
        view.verticalHeader().setDefaultSectionSize(row_height)
        for i in range(view.model().columnCount()):
            view.setColumnWidth(i, int(view.columnWidth(i) * 1.12))
    else:
        view.header().setFont(f)
    view.setProperty("big", True)
    view.setIconSize(QtCore.QSize(18, 18))


def message(parent, ok, msg, title="Taskscope"):
    if ok and not msg:
        return
    box = QtWidgets.QMessageBox.information if ok else QtWidgets.QMessageBox.warning
    box(parent, title, msg or ("Done." if ok else "The operation failed."))
