#!/bin/sh
# Pre-compile the Python code (faster start) and refresh the app menu and icons.
python3 -m compileall -q /usr/lib/taskscope/taskscope >/dev/null 2>&1 || true
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q /usr/share/applications || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -t /usr/share/icons/hicolor || true
exit 0
