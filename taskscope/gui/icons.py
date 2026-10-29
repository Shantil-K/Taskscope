"""Outline icons drawn at runtime with QPainter (original, no icon files).

Each glyph is a function that draws into a 24x24 box with the given pen.
Icons are rendered once per (name, colour) and cached.
"""

from .qt import E, QtCore, QtGui, Qt

P = QtCore.QPointF
R = QtCore.QRectF


def _summary(p):
    p.drawRoundedRect(R(3, 4, 18, 16), 3, 3)
    p.drawLine(P(3, 9), P(21, 9))
    p.drawLine(P(12, 9), P(12, 20))


def _performance(p):
    p.drawRoundedRect(R(3, 4, 18, 16), 3, 3)
    p.drawPolyline(QtGui.QPolygonF([P(6, 15), P(9, 12), P(12, 14), P(15, 9), P(18, 11)]))


def _processes(p):
    p.drawRoundedRect(R(3, 4, 18, 16), 3, 3)
    for y in (9, 13, 17):
        p.drawLine(P(7, y), P(17, y))


def _info(p):
    p.drawEllipse(R(3, 3, 18, 18))
    p.drawLine(P(12, 11), P(12, 17))
    p.drawPoint(P(12, 7.5))


def _startup(p):
    p.drawPolygon(QtGui.QPolygonF([P(8, 5), P(19, 12), P(8, 19)]))


def _users(p):
    p.drawEllipse(R(5, 5, 6, 6))
    p.drawArc(R(2, 13, 12, 10), 0, 180 * 16)
    p.drawEllipse(R(14, 6, 5, 5))
    p.drawArc(R(12.5, 13, 9, 8), 0, 180 * 16)


def _gear(p, cx, cy, r_out, r_in, teeth, hole):
    import math
    pts = []
    for i in range(teeth * 4):
        a = math.pi * 2 * i / (teeth * 4)
        r = r_out if i % 4 in (1, 2) else r_in
        pts.append(P(cx + r * math.cos(a), cy + r * math.sin(a)))
    p.drawPolygon(QtGui.QPolygonF(pts))
    p.drawEllipse(QtCore.QPointF(cx, cy), hole, hole)


def _services(p):
    # two meshing gears: a large one top-left, a small one bottom-right
    _gear(p, 9.0, 9.0, 6.6, 4.9, 8, 1.9)
    _gear(p, 17.6, 17.6, 4.4, 3.0, 6, 1.2)


def _power(p):
    p.drawPolygon(QtGui.QPolygonF([P(13, 3), P(6, 13), P(11.5, 13), P(10, 21), P(18, 10), P(12.5, 10)]))


def _connections(p):
    p.drawEllipse(R(3, 3, 18, 18))
    p.drawEllipse(R(8, 3, 8, 18))
    p.drawLine(P(3.5, 12), P(20.5, 12))


def _apps(p):
    for x, y in ((4, 4), (13, 4), (4, 13), (13, 13)):
        p.drawRoundedRect(R(x, y, 7, 7), 2, 2)


def _drivers(p):
    p.drawRoundedRect(R(6, 6, 12, 12), 2, 2)
    for i in (9, 12, 15):
        p.drawLine(P(i, 3), P(i, 6))
        p.drawLine(P(i, 18), P(i, 21))
        p.drawLine(P(3, i), P(6, i))
        p.drawLine(P(18, i), P(21, i))


def _disk(p):
    p.drawRoundedRect(R(3, 7, 18, 10), 3, 3)
    p.drawPoint(P(17, 12))
    p.drawLine(P(6, 12), P(11, 12))


def _settings(p):
    p.drawEllipse(R(9, 9, 6, 6))
    import math
    pts = []
    for i in range(16):
        r = 9 if i % 2 == 0 else 6.5
        a = math.pi * 2 * i / 16
        pts.append(P(12 + r * math.cos(a), 12 + r * math.sin(a)))
    p.drawPolygon(QtGui.QPolygonF(pts))


def _refresh(p):
    p.drawArc(R(4, 4, 16, 16), 60 * 16, 290 * 16)
    p.drawPolyline(QtGui.QPolygonF([P(16, 3), P(17, 6.5), P(13.5, 7.5)]))


def _menu(p):
    for y in (7, 12, 17):
        p.drawLine(P(4, y), P(20, y))


def _view(p):
    p.drawRoundedRect(R(3, 5, 18, 14), 2, 2)
    p.drawLine(P(8, 5), P(8, 19))


def _search(p):
    p.drawEllipse(R(4, 4, 11, 11))
    p.drawLine(P(14, 14), P(20, 20))


def _palette(p):
    p.drawEllipse(R(3, 3, 18, 18))
    for x, y in ((8, 9), (12, 7), (16, 9), (15, 14)):
        p.drawEllipse(R(x - 1, y - 1, 2, 2))


def _shield(p):
    path = QtGui.QPainterPath()
    path.moveTo(12, 3)
    path.lineTo(19, 6)
    path.cubicTo(19, 13, 16, 18, 12, 21)
    path.cubicTo(8, 18, 5, 13, 5, 6)
    path.closeSubpath()
    p.drawPath(path)


def _record(p):
    p.drawEllipse(R(5, 5, 14, 14))


def _stop(p):
    p.drawRect(R(6, 6, 12, 12))


def _pc(p):
    p.drawRoundedRect(R(3, 4, 18, 12), 2, 2)
    p.drawLine(P(12, 16), P(12, 20))
    p.drawLine(P(8, 20), P(16, 20))


def _clock(p):
    p.drawEllipse(R(4, 4, 16, 16))
    p.drawPolyline(QtGui.QPolygonF([P(12, 7.5), P(12, 12), P(15, 14)]))


def _thermo(p):
    p.drawEllipse(R(8.5, 14, 7, 7))
    p.drawLine(P(10.5, 14.6), P(10.5, 5))
    p.drawLine(P(13.5, 14.6), P(13.5, 5))
    p.drawArc(R(10.5, 3.5, 3, 3), 0, 180 * 16)
    p.drawLine(P(12, 17.5), P(12, 10))


def _util(p):
    for x, h in ((5, 6), (9.5, 10), (14, 14), (18.5, 8)):
        p.drawLine(P(x, 20), P(x, 20 - h))
    p.drawLine(P(3, 20.5), P(21, 20.5))


def _gpu(p):
    p.drawRoundedRect(R(3, 6, 18, 11), 2, 2)
    p.drawEllipse(R(12, 8.5, 6, 6))
    p.drawLine(P(6, 17), P(6, 20))
    p.drawLine(P(6, 9.5), P(9, 9.5))
    p.drawLine(P(6, 13), P(9, 13))


def _memory(p):
    p.drawRoundedRect(R(3, 7, 18, 9), 1.5, 1.5)
    for x in (7, 11, 15):
        p.drawRect(R(x - 1, 9.5, 2.5, 4))
    for x in (5, 8, 11, 14, 17, 19.5):
        p.drawLine(P(x, 16), P(x, 18))


def _battery(p):
    p.drawRoundedRect(R(3, 7, 16, 10), 2, 2)
    p.drawLine(P(21, 10.5), P(21, 13.5))
    p.drawLine(P(6.5, 10), P(6.5, 14))
    p.drawLine(P(9.5, 10), P(9.5, 14))


def _fan(p):
    p.drawEllipse(R(10.5, 10.5, 3, 3))
    for a in (0, 120, 240):
        p.save()
        p.translate(12, 12)
        p.rotate(a)
        path = QtGui.QPainterPath()
        path.moveTo(0, -1.5)
        path.cubicTo(-4, -4, -2, -9, 2, -9)
        path.cubicTo(4, -6, 3, -3, 1.2, -1)
        p.drawPath(path)
        p.restore()


def _plug(p):
    p.drawLine(P(9, 3), P(9, 8))
    p.drawLine(P(15, 3), P(15, 8))
    p.drawRoundedRect(R(6, 8, 12, 6), 2, 2)
    p.drawPolyline(QtGui.QPolygonF([P(12, 14), P(12, 18), P(15, 21)]))


def _home(p):
    p.drawPolyline(QtGui.QPolygonF([P(3.5, 11.5), P(12, 4), P(20.5, 11.5)]))
    p.drawPolyline(QtGui.QPolygonF([P(6, 9.5), P(6, 20), P(18, 20), P(18, 9.5)]))
    p.drawRect(R(10, 14, 4, 6))


def _pulse(p):
    p.drawPolyline(QtGui.QPolygonF([P(2, 13), P(7, 13), P(9.5, 7), P(13, 18), P(15.5, 10), P(17, 13), P(22, 13)]))


GLYPHS = {"summary": _summary, "performance": _performance, "processes": _processes,
          "info": _info, "startup": _startup, "users": _users, "services": _services,
          "power": _power, "connections": _connections, "apps": _apps, "drivers": _drivers,
          "disk": _disk, "settings": _settings, "refresh": _refresh, "menu": _menu,
          "view": _view, "search": _search, "palette": _palette, "shield": _shield, "record": _record, "stop": _stop,
          "pc": _pc, "chip": _drivers, "clock": _clock, "thermo": _thermo, "util": _util,
          "gpu": _gpu, "memory": _memory, "drive": _disk, "battery": _battery, "fan": _fan,
          "plug": _plug, "gear": _settings, "home": _home, "pulse": _pulse}

_cache = {}


def icon(name, color="#d0d0d0", fill=None):
    key = (name, color, fill)
    if key in _cache:
        return _cache[key]
    ic = QtGui.QIcon()
    for size in (16, 20, 24, 32, 48):
        pm = QtGui.QPixmap(size, size)
        pm.fill(QtGui.QColor(0, 0, 0, 0))
        p = QtGui.QPainter(pm)
        p.setRenderHint(E(QtGui.QPainter, "RenderHint", "Antialiasing"))
        p.scale(size / 24.0, size / 24.0)
        pen = QtGui.QPen(QtGui.QColor(color), 1.6)
        pen.setCapStyle(E(Qt, "PenCapStyle", "RoundCap"))
        pen.setJoinStyle(E(Qt, "PenJoinStyle", "RoundJoin"))
        p.setPen(pen)
        p.setBrush(QtGui.QColor(fill) if fill else E(Qt, "BrushStyle", "NoBrush"))
        GLYPHS[name](p)
        p.end()
        ic.addPixmap(pm)
    _cache[key] = ic
    return ic


# The logo: the Overview thumbnail's bars (device colours, fixed levels) under a
# line graph like the Performance page's, with a dot on its peak
LOGO_BARS = (("#3b82f6", 0.50), ("#a855f7", 0.75), ("#ef4444", 0.38), ("#22c55e", 0.90), ("#f59e0b", 0.60))
LOGO_LINE = ((22, 50), (40, 40), (56, 46), (72, 22), (88, 34), (106, 28))
LOGO_PEAK = (72, 22)
LOGO_NEON = "#39ff14"


def logo_pixmap(size, dpr=1.0):
    """The Taskscope logo drawn at exactly *size* pixels (times the screen's
    scale), never scaled from another size. Bar edges and the tile are snapped
    to whole pixels, so the logo stays sharp even at 16-32 px. Same design as
    data/taskscope.svg."""
    px = max(1, int(round(size * dpr)))
    pm = QtGui.QPixmap(px, px)
    pm.fill(QtGui.QColor(0, 0, 0, 0))
    k = px / 128.0

    def snap(v):
        return round(v * k)

    def rect(x, y, w, h):                       # 128-grid rect -> whole device pixels
        x0, y0, x1, y1 = snap(x), snap(y), snap(x + w), snap(y + h)
        return R(x0, y0, max(1, x1 - x0), max(1, y1 - y0))

    p = QtGui.QPainter(pm)
    p.setRenderHint(E(QtGui.QPainter, "RenderHint", "Antialiasing"))
    p.setPen(E(Qt, "PenStyle", "NoPen"))
    g = QtGui.QLinearGradient(0, 8 * k, 0, 120 * k)
    g.setColorAt(0, QtGui.QColor("#14532d"))               # deep green tile
    g.setColorAt(1, QtGui.QColor("#052e16"))
    p.setBrush(QtGui.QBrush(g))
    p.drawRoundedRect(rect(8, 8, 112, 112), 26 * k, 26 * k)
    x0, x1, y0, y1, gap = 24, 104, 40, 102, 5
    n = len(LOGO_BARS)
    w = (x1 - x0 - gap * (n - 1)) / n
    # whole-pixel bar width and gap, the same for every bar, centred on the tile
    wpx = max(1, round(w * k))
    gpx = max(1, round(gap * k))
    left = round((x0 + x1) / 2 * k - (n * wpx + (n - 1) * gpx) / 2)
    top, bottom = snap(y0), snap(y1)
    rad = max(0.0, 3.5 * k)
    for i, (color, level) in enumerate(LOGO_BARS):
        x = left + i * (wpx + gpx)
        track = QtGui.QColor(color)
        track.setAlphaF(0.14)
        p.setBrush(track)
        p.drawRoundedRect(R(x, top, wpx, bottom - top), rad, rad)
        p.setBrush(QtGui.QColor(color))
        bar_top = bottom - max(1, round((bottom - top) * level))
        p.drawRoundedRect(R(x, bar_top, wpx, bottom - bar_top), rad, rad)
    line = QtGui.QPolygonF([P(x * k, y * k) for x, y in LOGO_LINE])
    p.setBrush(E(Qt, "BrushStyle", "NoBrush"))
    for width, col in ((11, "#052e16"), (6, LOGO_NEON)):   # a dark edge keeps the neon line crisp
        pen = QtGui.QPen(QtGui.QColor(col), max(1.0, width * k))
        pen.setCapStyle(E(Qt, "PenCapStyle", "RoundCap"))
        pen.setJoinStyle(E(Qt, "PenJoinStyle", "RoundJoin"))
        p.setPen(pen)
        p.drawPolyline(line)
    p.setPen(E(Qt, "PenStyle", "NoPen"))
    p.setBrush(QtGui.QColor(LOGO_NEON))
    p.drawEllipse(P(LOGO_PEAK[0] * k, LOGO_PEAK[1] * k), 6.5 * k, 6.5 * k)
    p.setBrush(QtGui.QColor("#ffffff"))
    p.drawEllipse(P(LOGO_PEAK[0] * k, LOGO_PEAK[1] * k), 2.8 * k, 2.8 * k)
    p.end()
    pm.setDevicePixelRatio(dpr)
    return pm


def app_icon():
    """The Taskscope logo as an icon, with each common size drawn natively."""
    if "_app" in _cache:
        return _cache["_app"]
    ic = QtGui.QIcon()
    for size in (16, 20, 22, 24, 28, 32, 40, 48, 56, 64, 96, 128, 256):
        ic.addPixmap(logo_pixmap(size))
    _cache["_app"] = ic
    return ic


def folder_icon():
    """Yellow file-manager style folder (drawn here, no image files)."""
    if "_folder" in _cache:
        return _cache["_folder"]
    ic = QtGui.QIcon()
    for size in (16, 20, 24, 32, 48):
        pm = QtGui.QPixmap(size, size)
        pm.fill(QtGui.QColor(0, 0, 0, 0))
        p = QtGui.QPainter(pm)
        p.setRenderHint(E(QtGui.QPainter, "RenderHint", "Antialiasing"))
        p.scale(size / 24.0, size / 24.0)
        p.setPen(E(Qt, "PenStyle", "NoPen"))
        # back panel with the tab
        back = QtGui.QPainterPath()
        back.moveTo(2, 6.5)
        back.quadTo(2, 4.5, 4, 4.5)
        back.lineTo(9, 4.5)
        back.lineTo(11, 6.5)
        back.lineTo(20, 6.5)
        back.quadTo(22, 6.5, 22, 8.5)
        back.lineTo(22, 18)
        back.lineTo(2, 18)
        back.closeSubpath()
        p.setBrush(QtGui.QColor("#d99a1e"))
        p.drawPath(back)
        # front panel
        g = QtGui.QLinearGradient(0, 8.5, 0, 20)
        g.setColorAt(0.0, QtGui.QColor("#ffd769"))
        g.setColorAt(1.0, QtGui.QColor("#f5b829"))
        p.setBrush(QtGui.QBrush(g))
        p.drawRoundedRect(R(2, 8.5, 20, 11.5), 1.6, 1.6)
        p.end()
        ic.addPixmap(pm)
        ic.addPixmap(pm, E(QtGui.QIcon, "Mode", "Selected"))   # don't tint on selected rows
    _cache["_folder"] = ic
    return ic
