"""Startup applications: XDG autostart entries and systemd user units."""

import glob
import os

from .packages import parse_desktop
from .probe import which

USER_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "autostart")


def _system_dirs():
    dirs = os.environ.get("XDG_CONFIG_DIRS", "/etc/xdg").split(":")
    return [os.path.join(d, "autostart") for d in dirs if d]


def _is_enabled(e):
    return not (e.get("Hidden", "").lower() == "true"
                or e.get("X-GNOME-Autostart-enabled", "").lower() == "false")


def _shown_here(e):
    desk = set(os.environ.get("XDG_CURRENT_DESKTOP", "").split(":")) - {""}
    only = set(filter(None, e.get("OnlyShowIn", "").split(";")))
    nots = set(filter(None, e.get("NotShowIn", "").split(";")))
    if only and desk and not (only & desk):
        return False
    if nots & desk:
        return False
    return True


def list_entries():
    entries = {}
    for d in _system_dirs():
        for path in glob.glob(os.path.join(d, "*.desktop")):
            fid = os.path.basename(path)
            if fid not in entries:
                entries[fid] = (path, "System")
    for path in glob.glob(os.path.join(USER_DIR, "*.desktop")):
        entries[os.path.basename(path)] = (path, "User")
    out = []
    for fid, (path, scope) in entries.items():
        e = parse_desktop(path)
        if not _shown_here(e):
            continue
        out.append({"id": fid, "name": e.get("Name", fid), "command": e.get("Exec", ""),
                    "description": e.get("Comment", ""), "enabled": _is_enabled(e),
                    "kind": "Autostart", "scope": scope, "path": path})
    if which("systemctl") and os.path.isdir("/run/systemd/system"):
        from .services import Systemd
        for name, state in Systemd(user=True)._unit_file_states().items():
            if state in ("enabled", "disabled"):
                out.append({"id": name, "name": name, "command": "", "description": "",
                            "enabled": state == "enabled", "kind": "systemd --user",
                            "scope": "User", "path": ""})
    return sorted(out, key=lambda x: x["name"].lower())


def set_enabled(entry, enabled):
    """Enable/disable by writing a per-user override; never touches /etc."""
    if entry["kind"] == "systemd --user":
        from .probe import run_status
        code, msg = run_status(["systemctl", "--user", "enable" if enabled else "disable", entry["id"]])
        return code == 0, msg
    os.makedirs(USER_DIR, exist_ok=True)
    target = os.path.join(USER_DIR, entry["id"])
    try:
        with open(entry["path"], errors="replace") as f:
            lines = f.read().splitlines()
    except OSError as e:
        return False, str(e)
    out, in_main, done = [], False, False
    for line in lines:
        s = line.strip()
        if s.startswith("["):
            if in_main and not done:
                out.append(f"Hidden={'false' if enabled else 'true'}")
                done = True
            in_main = s == "[Desktop Entry]"
        elif in_main and (s.startswith("Hidden=") or s.startswith("X-GNOME-Autostart-enabled=")):
            continue
        out.append(line)
    if not done:
        out.append(f"Hidden={'false' if enabled else 'true'}")
    try:
        with open(target, "w") as f:
            f.write("\n".join(out) + "\n")
    except OSError as e:
        return False, str(e)
    return True, ""
