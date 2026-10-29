"""Qt binding shim: PySide6 -> PyQt6 -> PyQt5 -> PySide2.

Distros package different Qt bindings (or only one of them), so the GUI is
written against this module. Set TASKSCOPE_QT=pyqt5 (etc.) to force one.
Enum access always goes through E() so Qt5's flat enums and Qt6's scoped
enums both work.
"""

import os

_order = ["PySide6", "PyQt6", "PyQt5", "PySide2"]
_forced = os.environ.get("TASKSCOPE_QT", "").lower()
if _forced:
    _order = [b for b in _order if b.lower() == _forced] + [b for b in _order if b.lower() != _forced]

API = None
_errors = []
for _b in _order:
    try:
        if _b == "PySide6":
            from PySide6 import QtCore, QtGui, QtWidgets, QtNetwork  # noqa: F401
            Signal = QtCore.Signal
        elif _b == "PyQt6":
            from PyQt6 import QtCore, QtGui, QtWidgets, QtNetwork  # noqa: F401
            Signal = QtCore.pyqtSignal
        elif _b == "PyQt5":
            from PyQt5 import QtCore, QtGui, QtWidgets, QtNetwork  # noqa: F401
            Signal = QtCore.pyqtSignal
        else:
            from PySide2 import QtCore, QtGui, QtWidgets, QtNetwork  # noqa: F401
            Signal = QtCore.Signal
        API = _b
        break
    except ImportError as e:
        _errors.append(f"{_b}: {e}")

if API is None:
    raise ImportError("No Qt binding found (tried " + "; ".join(_errors) + ")")

QT6 = API in ("PySide6", "PyQt6")
Qt = QtCore.Qt
QAction = getattr(QtGui, "QAction", None) or QtWidgets.QAction
QShortcut = getattr(QtGui, "QShortcut", None) or QtWidgets.QShortcut


def E(owner, scope, name):
    """Enum member that works with both scoped (Qt6) and flat (Qt5) enums."""
    sc = getattr(owner, scope, None)
    if sc is not None and hasattr(sc, name):
        return getattr(sc, name)
    return getattr(owner, name)


def exec_(obj, *args):
    fn = getattr(obj, "exec", None) or getattr(obj, "exec_")
    return fn(*args)


def version_string():
    return f"{API} / Qt {QtCore.qVersion()}"
