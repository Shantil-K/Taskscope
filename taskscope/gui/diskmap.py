"""Treemap of a disk scan: every folder is a box split into its subfolders
and files, each sized by allocated space and coloured by file type.

Layout uses the squarified algorithm (boxes as close to square as possible).
Recursion stops once boxes get smaller than a few pixels, so drawing cost
depends on the window size, not on how many files were scanned. The picture
is rendered once into a pixmap and only redrawn on resize or new data.
"""

from . import theme
from .qt import E, QtCore, QtGui, QtWidgets, Qt, Signal

R = QtCore.QRectF

# Distinct colours for the biggest file types; everything else is grey.
PALETTE = ["#e8b100", "#e05a00", "#8f3fd6", "#2ea043", "#d6336c", "#1f8fff", "#7c5cff",
           "#e5484d", "#a3b100", "#0fb5ae", "#c2410c", "#b84fd6", "#4f9d2d", "#ff7ab6",
           "#3b82f6", "#d97706"]
OTHER = "#707070"
MIN_SIDE = 3.0          # boxes smaller than this are not split further
LABEL_H = 14


def squarify(weights, x, y, w, h):
    """weights: areas (sorted largest first) that add up to w*h.
    Returns one (x, y, w, h) per weight."""
    out = []
    i, n = 0, len(weights)
    while i < n:
        side = min(w, h)
        if side <= 0:
            out.extend((x, y, 0, 0) for _ in range(n - i))
            break
        s = weights[i]
        hi = lo = s
        side2 = side * side
        worst = max(side2 * hi / (s * s), (s * s) / (side2 * lo)) if s > 0 else 1e18
        j = i + 1
        while j < n:
            v = weights[j]
            s2 = s + v
            hi2, lo2 = max(hi, v), min(lo, v)
            w2 = max(side2 * hi2 / (s2 * s2), (s2 * s2) / (side2 * lo2)) if lo2 > 0 else 1e18
            if w2 > worst:
                break
            s, hi, lo, worst = s2, hi2, lo2, w2
            j += 1
        if w >= h:                       # a column on the left
            cw = s / h if h else 0
            yy = y
            for k in range(i, j):
                hh = weights[k] / cw if cw else 0
                out.append((x, yy, cw, hh))
                yy += hh
            x += cw
            w -= cw
        else:                            # a row along the top
            rh = s / w if w else 0
            xx = x
            for k in range(i, j):
                ww = weights[k] / rh if rh else 0
                out.append((xx, y, ww, rh))
                xx += ww
            y += rh
            h -= rh
        i = j
    return out


class Treemap(QtWidgets.QWidget):
    clicked = Signal(str, int)          # kind ("dir" / "file"), index
    activated = Signal(str, int)        # double click
    context = Signal(str, int, object)  # right click: kind, index, global position

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumHeight(120)
        self.res = None
        self.colors = {}                # extension index -> QColor
        self.highlight = None           # extension index to stand out, or None
        self.selected = None            # (kind, idx)
        self.hover = None               # (kind, idx) under the mouse
        self.rects = {}                 # (kind, idx) -> QRectF
        self.kids = {}                  # folder -> [(QRectF, kind, idx)] for hit testing
        self.pix = None
        self._sorted = {}
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.rebuild)

    # ---- data -------------------------------------------------------------
    def set_result(self, res, colors):
        self.res = res
        self.colors = colors
        self.selected = None
        self.hover = None
        self.highlight = None
        self._sorted = {}
        self.rebuild()

    def invalidate(self):
        """Call after the scan data changed (e.g. a file was deleted)."""
        self._sorted = {}
        self.rebuild()

    def set_highlight(self, ext):
        if ext != self.highlight:
            self.highlight = ext
            self.rebuild()

    def select(self, kind, idx):
        self.selected = (kind, idx) if kind else None
        self.update()

    def _children(self, d):
        """[(allocated, kind, idx)] of folder d, largest first (cached)."""
        c = self._sorted.get(d)
        if c is None:
            res = self.res
            c = [(res.dalloc[s], "dir", s) for s in res.subfolders(d) if res.dalloc[s] > 0]
            fa = res.falloc
            c += [(fa[f], "file", f) for f in res.files_in(d) if fa[f] > 0]
            c.sort(key=lambda t: t[0], reverse=True)
            self._sorted[d] = c
        return c

    # ---- drawing ----------------------------------------------------------
    def resizeEvent(self, e):
        self._timer.start(120)
        super().resizeEvent(e)

    def rebuild(self):
        self.rects, self.kids = {}, {}
        if not self.isVisible() and self.pix is not None:
            self.pix = None             # redraw when shown
        dpr = self.devicePixelRatioF() if hasattr(self, "devicePixelRatioF") else 1.0
        size = self.size()
        if size.width() < 10 or size.height() < 10:
            return
        pix = QtGui.QPixmap(int(size.width() * dpr), int(size.height() * dpr))
        pix.setDevicePixelRatio(dpr)
        pix.fill(QtGui.QColor(theme.current["panel"]))
        if self.res is None:
            p = QtGui.QPainter(pix)
            self._ghost(p, R(0, 0, size.width(), size.height()))
            p.end()
        if self.res is not None and self.res.total_alloc > 0:
            p = QtGui.QPainter(pix)
            f = QtGui.QFont(self.font())
            f.setPointSizeF(8.0)
            p.setFont(f)
            self._fm = QtGui.QFontMetrics(f)
            dark = QtGui.QColor(theme.current["base"]).lightness() < 128
            self._dim = QtGui.QColor("#3a3a3a" if dark else "#c8c8c8")
            self._other = QtGui.QColor(OTHER)
            self._box_bg = QtGui.QColor("#202020" if dark else "#d8d8d8")
            self._pending = QtGui.QColor("#4f5b6e" if dark else "#9fb0c8")
            self._text = QtGui.QColor(theme.current["text"])
            self._folder(p, 0, R(0, 0, size.width(), size.height()))
            p.end()
        self.pix = pix
        self.update()

    def _ghost(self, p, rect):
        """A faint outline of a treemap (fixed made-up sizes), shown before a scan."""
        base = QtGui.QColor(theme.current["text"])
        p.setPen(E(Qt, "PenStyle", "NoPen"))
        top = [34, 20, 14, 10, 8, 6, 4, 3, 1]
        inner = [30, 18, 12, 9, 7, 6, 5, 4, 3, 2, 2, 1, 1]
        for i, (x, y, w, h) in enumerate(squarify([v * rect.width() * rect.height() / sum(top) for v in top],
                                                  rect.x(), rect.y(), rect.width(), rect.height())):
            box = R(x, y, w, h).adjusted(1, 1, -1, -1)
            c = QtGui.QColor(base)
            c.setAlpha(14)
            p.setBrush(c)
            p.drawRect(box)
            if w < 30 or h < 30:
                continue
            sub = box.adjusted(2, 14, -2, -2)
            k = inner[i % 3:]
            for j, (sx, sy, sw, sh) in enumerate(squarify([v * sub.width() * sub.height() / sum(k) for v in k],
                                                          sub.x(), sub.y(), sub.width(), sub.height())):
                c = QtGui.QColor(base)
                c.setAlpha(18 + (j * 7 + i * 5) % 22)
                p.setBrush(c)
                p.drawRect(R(sx, sy, sw, sh).adjusted(0.5, 0.5, -0.5, -0.5))
            lab = QtGui.QColor(base)
            lab.setAlpha(40)
            p.setBrush(lab)
            p.drawRoundedRect(R(box.x() + 4, box.y() + 4, min(90, box.width() * 0.5), 6), 3, 3)
        p.setBrush(E(Qt, "BrushStyle", "NoBrush"))

    def _color(self, ext):
        if self.highlight is not None and ext != self.highlight:
            return self._dim
        return self.colors.get(ext, self._other)

    def _folder(self, p, d, rect):
        res = self.res
        self.rects[("dir", d)] = rect
        p.fillRect(rect, self._box_bg)
        inner = rect.adjusted(1, 1, -1, -1)
        if rect.width() > 70 and rect.height() > 34:
            name = res.root if d == 0 else res.dname[d] + "/"
            label = f"{name} ({_size(res.dalloc[d])})"
            p.setPen(self._text)
            p.drawText(rect.adjusted(3, 0, -2, 0).toRect(), E(Qt, "AlignmentFlag", "AlignLeft"),
                       self._fm.elidedText(label, E(Qt, "TextElideMode", "ElideMiddle"),
                                           int(rect.width() - 6)))
            inner = rect.adjusted(1, LABEL_H, -1, -1)
        if inner.width() < MIN_SIDE or inner.height() < MIN_SIDE:
            return
        kids = self._children(d)
        total = sum(k[0] for k in kids)
        if not total:
            if getattr(res, "preview", False) and res.dalloc[d] > 0:
                # live preview: this folder's contents are known only as a total so far
                self._file(p, inner, self._pending)
            return
        scale = inner.width() * inner.height() / total
        # Boxes under ~4 px² can't be seen; lump them together at the end.
        weights, items, rest = [], [], 0.0
        for a, kind, idx in kids:
            area = a * scale
            if area < 4.0:
                rest += area
            else:
                weights.append(area)
                items.append((kind, idx))
        if rest > 0:
            weights.append(rest)
            items.append(("rest", -1))
        boxes = squarify(weights, inner.x(), inner.y(), inner.width(), inner.height())
        hit = []
        for (x, y, w, h), (kind, idx) in zip(boxes, items):
            r = R(x, y, w, h)
            if kind == "dir":
                hit.append((r, kind, idx))
                self._folder(p, idx, r)
            elif kind == "file":
                hit.append((r, kind, idx))
                self.rects[("file", idx)] = r
                self._file(p, r, self._color(res.fext[idx]))
            else:
                p.fillRect(r.adjusted(0.5, 0.5, -0.5, -0.5), self._dim)
        self.kids[d] = hit

    def _file(self, p, r, col):
        rr = r.adjusted(0.5, 0.5, -0.5, -0.5)
        if rr.width() >= 6 and rr.height() >= 6:
            # Cushion shading: lighter top-left, darker bottom-right.
            g = QtGui.QLinearGradient(rr.topLeft(), rr.bottomRight())
            g.setColorAt(0.0, col.lighter(135))
            g.setColorAt(1.0, col.darker(160))
            p.fillRect(rr, QtGui.QBrush(g))
        else:
            p.fillRect(rr, col)

    def paintEvent(self, e):
        p = QtGui.QPainter(self)
        if self.pix is None:
            self.rebuild()
        if self.pix is not None:
            p.drawPixmap(0, 0, self.pix)
        if self.res is not None and self.hover in self.rects and self.hover != self.selected:
            # Hovered box: brighten it and outline it.
            r = self.rects[self.hover]
            p.fillRect(r, QtGui.QColor(255, 255, 255, 60 if self.hover[0] == "file" else 28))
            p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 200), 1))
            p.setBrush(E(Qt, "BrushStyle", "NoBrush"))
            p.drawRect(r.adjusted(0.5, 0.5, -0.5, -0.5))
        if self.res is not None and self.selected in self.rects:
            pen = QtGui.QPen(QtGui.QColor("#ffffff"), 2)
            p.setPen(pen)
            p.setBrush(E(Qt, "BrushStyle", "NoBrush"))
            p.drawRect(self.rects[self.selected].adjusted(1, 1, -1, -1))
        p.end()

    # ---- mouse ------------------------------------------------------------
    def item_at(self, pos):
        if self.res is None:
            return None
        pt = QtCore.QPointF(pos)
        found, d = None, 0
        while True:
            nxt = None
            for r, kind, idx in self.kids.get(d, ()):
                if r.contains(pt):
                    nxt = (kind, idx)
                    break
            if nxt is None:
                break
            found = nxt
            if nxt[0] != "dir":
                break
            d = nxt[1]
        if found is None and ("dir", 0) in self.rects:
            found = ("dir", 0)
        return found

    def mouseMoveEvent(self, e):
        pos = e.position().toPoint() if hasattr(e, "position") else e.pos()
        it = self.item_at(pos)
        if it != self.hover:
            old = self.rects.get(self.hover)
            self.hover = it
            new = self.rects.get(it)
            # repaint only the two boxes that changed
            for r in (old, new):
                if r is not None:
                    self.update(r.toAlignedRect().adjusted(-2, -2, 2, 2))
        if it:
            res = self.res
            if it[0] == "file":
                txt = f"{res.file_path(it[1])}\n{_size(res.falloc[it[1]])}"
            else:
                txt = f"{res.dir_path(it[1])}\n{_size(res.dalloc[it[1]])} · {res.dfiles[it[1]]:,} files"
            gp = e.globalPosition().toPoint() if hasattr(e, "globalPosition") else e.globalPos()
            QtWidgets.QToolTip.showText(gp, txt, self)

    def leaveEvent(self, e):
        if self.hover is not None:
            r = self.rects.get(self.hover)
            self.hover = None
            if r is not None:
                self.update(r.toAlignedRect().adjusted(-2, -2, 2, 2))
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        pos = e.position().toPoint() if hasattr(e, "position") else e.pos()
        it = self.item_at(pos)
        if not it:
            return
        if e.button() == E(Qt, "MouseButton", "LeftButton"):
            self.select(*it)
            self.clicked.emit(*it)
        elif e.button() == E(Qt, "MouseButton", "RightButton"):
            self.select(*it)
            self.clicked.emit(*it)
            gp = e.globalPosition().toPoint() if hasattr(e, "globalPosition") else e.globalPos()
            self.context.emit(it[0], it[1], gp)

    def mouseDoubleClickEvent(self, e):
        pos = e.position().toPoint() if hasattr(e, "position") else e.pos()
        it = self.item_at(pos)
        if it:
            self.activated.emit(*it)


def _size(n):
    from .. import fmt
    return fmt.size(n)


def ext_colors(res):
    """Extension index -> colour: palette for the biggest types, grey after."""
    order = sorted(range(len(res.exts)), key=lambda e: res.ext_stats[e][1], reverse=True)
    return {e: QtGui.QColor(PALETTE[i]) for i, e in enumerate(order[:len(PALETTE)])}
