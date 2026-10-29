#!/bin/sh
# Taskscope installer - works on Debian/Ubuntu/Mint, Fedora/RHEL, openSUSE,
# Arch/Manjaro, Alpine, Void, Gentoo, Solus and anything else with Python 3.8+.
#
#   ./install.sh                 install for the current user (~/.local)
#   ./install.sh --system        install for everyone (/usr/local, needs root)
#   ./install.sh --no-deps       don't touch the package manager
#   ./install.sh --prefix DIR    install somewhere else
#
# Dependencies are tried in order of preference; when a distro doesn't have a
# package, the next alternative is used. Only Python 3 is strictly required:
# without any Qt binding Taskscope runs as a terminal (curses) app.
set -eu

PREFIX="$HOME/.local"
DEPS=1
while [ $# -gt 0 ]; do
    case "$1" in
        --system) PREFIX=/usr/local ;;
        --prefix) PREFIX="$2"; shift ;;
        --no-deps) DEPS=0 ;;
        -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

HERE=$(cd "$(dirname "$0")" && pwd)
say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1; then SUDO=sudo
    elif command -v doas >/dev/null 2>&1; then SUDO=doas
    fi
fi

# --------------------------------------------------------------- detection
PM=""
for c in apt-get dnf yum zypper pacman apk xbps-install emerge eopkg; do
    if command -v "$c" >/dev/null 2>&1; then PM=$c; break; fi
done

# Each manager: how to ask whether a package exists, and how to install it.
pkg_exists() {
    case "$PM" in
        apt-get) apt-cache show "$1" >/dev/null 2>&1 ;;
        dnf|yum) $PM -q info "$1" >/dev/null 2>&1 ;;
        zypper) zypper -q info "$1" 2>/dev/null | grep -q "^Name" ;;
        pacman) pacman -Si "$1" >/dev/null 2>&1 ;;
        apk) apk search -e "$1" 2>/dev/null | grep -q . ;;
        xbps-install) xbps-query -R "$1" >/dev/null 2>&1 ;;
        emerge) command -v equery >/dev/null 2>&1 && equery -q list -po "$1" >/dev/null 2>&1 || return 0 ;;
        eopkg) eopkg info "$1" 2>/dev/null | grep -q "Name" ;;
        *) return 1 ;;
    esac
}

pkg_install() {
    case "$PM" in
        apt-get) $SUDO apt-get install -y "$@" ;;
        dnf|yum) $SUDO $PM install -y "$@" ;;
        zypper) $SUDO zypper --non-interactive install "$@" ;;
        pacman) $SUDO pacman -S --needed --noconfirm "$@" ;;
        apk) $SUDO apk add "$@" ;;
        xbps-install) $SUDO xbps-install -y "$@" ;;
        emerge) $SUDO emerge --noreplace "$@" ;;
        eopkg) $SUDO eopkg install -y "$@" ;;
    esac
}

# Install the first candidate that exists. Usage: first_of "what" pkg1 pkg2 ...
first_of() {
    what=$1; shift
    for p in "$@"; do
        if pkg_exists "$p"; then
            say "Installing $what: $p"
            if pkg_install "$p"; then return 0; fi
            warn "$p failed to install, trying the next option"
        fi
    done
    warn "no package for $what found in this distro's repositories"
    return 1
}

python_has_qt() {
    for m in PySide6 PyQt6 PyQt5 PySide2; do
        if python3 -c "import $m.QtWidgets" >/dev/null 2>&1; then echo "$m"; return 0; fi
    done
    return 1
}

if [ "$DEPS" -eq 1 ]; then
    if [ -z "$PM" ]; then
        warn "unknown package manager; skipping dependency installation"
    else
        say "Package manager: $PM"
        [ "$PM" = apt-get ] && { $SUDO apt-get update -qq || true; }
        command -v python3 >/dev/null 2>&1 || first_of "Python 3" python3 python dev-lang/python || true
        if ! python_has_qt >/dev/null; then
            case "$PM" in
                apt-get) set -- python3-pyqt6 python3-pyside6.qtwidgets python3-pyqt5 python3-pyside2.qtwidgets ;;
                dnf|yum) set -- python3-pyqt6 python3-pyside6 python3-qt5 python3-pyside2 ;;
                zypper) set -- python3-PyQt6 python3-pyside6 python3-qt5 python3-PyQt5 ;;
                pacman) set -- python-pyqt6 pyside6 python-pyqt5 pyside2 ;;
                apk) set -- py3-pyqt6 py3-pyside6 py3-qt5 ;;
                xbps-install) set -- python3-PyQt6 python3-pyside6 python3-PyQt5 ;;
                emerge) set -- dev-python/pyqt6 dev-python/pyside dev-python/pyqt5 ;;
                eopkg) set -- python3-qt6 python3-qt5 ;;
            esac
            first_of "a Qt binding for the GUI" "$@" || true
        fi
        # Optional helpers. Each has a fallback inside Taskscope, so failures are fine.
        case "$PM" in
            emerge) OPT="sys-apps/pciutils sys-apps/kmod sys-apps/iproute2 media-video/ffmpeg" ;;
            dnf|yum) OPT="pciutils kmod iproute ffmpeg|ffmpeg-free" ;;
            *) OPT="pciutils kmod iproute2|iproute ffmpeg" ;;
        esac
        for group in $OPT; do
            # shellcheck disable=SC2086
            first_of "optional ${group%%|*}" $(echo "$group" | tr '|' ' ') || true
        done
    fi
fi

command -v python3 >/dev/null 2>&1 || { echo "Python 3 is required." >&2; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' || { echo "Python 3.8 or newer is required." >&2; exit 1; }

QT=$(python_has_qt || true)
if [ -z "$QT" ] && [ "$DEPS" -eq 1 ]; then
    say "No distro Qt package worked; trying pip (user install)"
    python3 -m pip install --user PyQt6 >/dev/null 2>&1 \
        || python3 -m pip install --user PyQt5 >/dev/null 2>&1 \
        || warn "pip could not install a Qt binding either"
    QT=$(python_has_qt || true)
fi

# -------------------------------------------------------------- files ----
if [ ! -w "$(dirname "$PREFIX")" ] && [ ! -w "$PREFIX" ] && [ -n "$SUDO" ]; then
    INST="$SUDO"
else
    INST=""
fi
LIB="$PREFIX/lib/taskscope"
say "Installing to $PREFIX"
$INST mkdir -p "$LIB" "$PREFIX/bin" "$PREFIX/share/applications" \
    "$PREFIX/share/icons/hicolor/scalable/apps" "$PREFIX/share/man/man1" \
    "$PREFIX/share/metainfo" "$PREFIX/share/doc/taskscope"
$INST rm -rf "$LIB/taskscope"
$INST cp -r "$HERE/taskscope" "$LIB/"
$INST find "$LIB" -name __pycache__ -prune -exec rm -rf {} +
$INST sh -c "cat > '$PREFIX/bin/taskscope'" <<EOF
#!/bin/sh
PYTHONPATH="$LIB\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m taskscope "\$@"
EOF
$INST chmod 755 "$PREFIX/bin/taskscope"
sed "s|^Exec=taskscope|Exec=$PREFIX/bin/taskscope|" "$HERE/data/io.github.taskscope.Taskscope.desktop" \
    | $INST sh -c "cat > '$PREFIX/share/applications/io.github.taskscope.Taskscope.desktop'"
$INST cp "$HERE/data/taskscope.svg" "$PREFIX/share/icons/hicolor/scalable/apps/taskscope.svg"
$INST cp "$HERE/data/taskscope.1" "$PREFIX/share/man/man1/taskscope.1"
$INST cp "$HERE/data/io.github.taskscope.Taskscope.metainfo.xml" "$PREFIX/share/metainfo/"
$INST cp "$HERE/LICENSE" "$HERE/README.md" "$PREFIX/share/doc/taskscope/"
python3 -m compileall -q "$LIB/taskscope" >/dev/null 2>&1 || true
command -v update-desktop-database >/dev/null 2>&1 && $INST update-desktop-database -q "$PREFIX/share/applications" 2>/dev/null || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && $INST gtk-update-icon-cache -q -t "$PREFIX/share/icons/hicolor" 2>/dev/null || true

say "Done."
if [ -n "$QT" ]; then echo "    GUI toolkit: $QT"; else echo "    No Qt binding: Taskscope will run in terminal mode."; fi
case ":$PATH:" in *":$PREFIX/bin:"*) ;; *) echo "    Add $PREFIX/bin to your PATH to run 'taskscope' from a shell." ;; esac
echo "    Run 'taskscope --check' to see which backends were detected."
