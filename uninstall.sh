#!/bin/sh
# Remove Taskscope. Use --system for a /usr/local install, or --prefix DIR.
# Settings in ~/.config/taskscope are kept unless --purge is given.
set -eu
PREFIX="$HOME/.local"
PURGE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --system) PREFIX=/usr/local ;;
        --prefix) PREFIX="$2"; shift ;;
        --purge) PURGE=1 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done
SUDO=""
[ -w "$PREFIX" ] || SUDO=$(command -v sudo || command -v doas || true)
$SUDO rm -rf "$PREFIX/lib/taskscope" "$PREFIX/share/doc/taskscope"
$SUDO rm -f "$PREFIX/bin/taskscope" \
    "$PREFIX/share/applications/io.github.taskscope.Taskscope.desktop" \
    "$PREFIX/share/icons/hicolor/scalable/apps/taskscope.svg" \
    "$PREFIX/share/man/man1/taskscope.1" \
    "$PREFIX/share/metainfo/io.github.taskscope.Taskscope.metainfo.xml"
[ "$PURGE" -eq 1 ] && rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/taskscope"
echo "Taskscope removed from $PREFIX."
