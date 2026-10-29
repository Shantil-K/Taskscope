"""App icons for the Installed Apps list: the icon each .desktop entry names,
looked up the way the desktop does (your icon theme, then hicolor, then
/usr/share/pixmaps), or a file path. Results are cached; lookups happen only
for rows that are drawn."""

import configparser
import glob
import os

from .qt import QtGui

_cache = {}
_ready = False
_SIZES = ("scalable", "256x256", "128x128", "96x96", "64x64", "48x48", "32x32", "24x24", "22x22", "16x16")


def _desktop_icon_theme():
    """The icon theme the desktop uses (GTK setting, then gsettings)."""
    for path in (os.path.expanduser("~/.config/gtk-3.0/settings.ini"),
                 os.path.expanduser("~/.config/gtk-4.0/settings.ini")):
        cp = configparser.ConfigParser()
        try:
            cp.read(path)
            name = cp.get("Settings", "gtk-icon-theme-name", fallback="").strip()
            if name:
                return name
        except configparser.Error:
            pass
    from ..sysinfo.probe import run, which
    if which("gsettings"):
        for schema in ("org.cinnamon.desktop.interface", "org.gnome.desktop.interface",
                       "org.mate.interface"):
            out = run(["gsettings", "get", schema, "icon-theme"], timeout=3).strip().strip("'")
            if out:
                return out
    return ""


def _setup():
    global _ready
    _ready = True
    paths = list(QtGui.QIcon.themeSearchPaths())
    for p in (os.path.expanduser("~/.local/share/icons"), os.path.expanduser("~/.icons"),
              "/usr/share/icons", "/usr/local/share/icons",
              "/var/lib/flatpak/exports/share/icons",
              os.path.expanduser("~/.local/share/flatpak/exports/share/icons"),
              "/var/lib/snapd/desktop/icons"):
        if os.path.isdir(p) and p not in paths:
            paths.append(p)
    QtGui.QIcon.setThemeSearchPaths(paths)
    if not QtGui.QIcon.themeName() or QtGui.QIcon.themeName() == "hicolor":
        name = _desktop_icon_theme()
        if name:
            QtGui.QIcon.setThemeName(name)
    if hasattr(QtGui.QIcon, "setFallbackThemeName"):
        QtGui.QIcon.setFallbackThemeName("hicolor")


def _search(name):
    """Look in hicolor and pixmaps directly (themes without the icon)."""
    roots = [os.path.expanduser("~/.local/share/icons/hicolor"), "/usr/share/icons/hicolor",
             "/usr/local/share/icons/hicolor", "/var/lib/flatpak/exports/share/icons/hicolor",
             os.path.expanduser("~/.local/share/flatpak/exports/share/icons/hicolor")]
    for root in roots:
        for sz in _SIZES:
            for ext in (".svg", ".png", ".xpm"):
                p = f"{root}/{sz}/apps/{name}{ext}"
                if os.path.exists(p):
                    return p
    for ext in (".svg", ".png", ".xpm", ""):
        p = f"/usr/share/pixmaps/{name}{ext}"
        if os.path.isfile(p):
            return p
    hits = glob.glob(f"/usr/share/icons/*/*/apps/{name}.*")
    return hits[0] if hits else None


def icon(name):
    """QIcon for an Icon= value, or None."""
    if not name:
        return None
    if name in _cache:
        return _cache[name]
    if not _ready:
        _setup()
    ic = None
    if os.path.isabs(name):
        if os.path.exists(name):
            ic = QtGui.QIcon(name)
    else:
        base = name[:-4] if name.endswith((".png", ".svg", ".xpm")) else name
        t = QtGui.QIcon.fromTheme(base)
        if not t.isNull():
            ic = t
        else:
            p = _search(base)
            if p:
                ic = QtGui.QIcon(p)
    if ic is not None and ic.isNull():
        ic = None
    _cache[name] = ic
    return ic
