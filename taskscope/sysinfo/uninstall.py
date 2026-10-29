"""Uninstall an app from the Installed Apps list with whichever package
manager installed it. Every removal is planned first (a dry run where the
manager has one) so the user sees exactly what goes before confirming.

plan(row)  -> {"ok", "how", "removes": [...], "note", "warn", "cmd", "root"}
run(plan)  -> (ok, message)
gone(row)  -> True once the app is really uninstalled
"""

import os
import re

from . import elevate
from .probe import run as sh, which


def _apt_simulate(pkg):
    """Packages apt would remove with *pkg* (dry run, needs no root)."""
    out = sh(["apt-get", "-s", "remove", pkg], timeout=60)
    removes = re.findall(r"^Remv (\S+)", out, re.M)
    essential = "essential" in out.lower() and "WARNING" in out
    return removes, essential, out


def plan(row):
    src, pkg, mgr = row.get("source", ""), row.get("package"), row.get("manager")
    name = row.get("name", "")
    if src == "Flatpak" and row.get("appid"):
        scope = "--user" if row.get("scope") == "User" else "--system"
        return {"ok": True, "how": "Flatpak", "removes": [row["appid"]], "root": False,
                "cmd": ["flatpak", "uninstall", "-y", "--noninteractive", scope, row["appid"]],
                "note": "Your data for it in ~/.var/app is kept.", "warn": ""}
    if src == "Snap" and row.get("appid"):
        return {"ok": True, "how": "Snap", "removes": [row["appid"]], "root": True,
                "cmd": ["snap", "remove", row["appid"]], "note": "", "warn": ""}
    if pkg and mgr == "dpkg":
        if which("apt-get"):
            removes, essential, out = _apt_simulate(pkg)
            if not removes:
                reason = next((l for l in out.splitlines() if l.startswith("E:")), "apt would not remove it.")
                return {"ok": False, "how": "apt", "removes": [], "note": reason, "warn": "", "cmd": [], "root": True}
            warn = ""
            if essential:
                return {"ok": False, "how": "apt", "removes": removes, "root": True, "cmd": [],
                        "note": "apt marks this as an essential part of the system; Taskscope will not remove it.",
                        "warn": ""}
            if len(removes) > 1:
                n = len(removes) - 1
                warn = (f"Removing {pkg} also removes {n} other package{'s' if n > 1 else ''} "
                        f"that depend{'' if n > 1 else 's'} on it.")
            meta = [x for x in removes if "meta" in x or x.endswith(("-desktop", "-core", "-standard"))]
            if meta:
                warn += (" Careful: this includes " + ", ".join(meta) + ", which keep core parts of your "
                         "system installed. Removing them can let other system packages be removed later "
                         "(for example by \"apt autoremove\").")
            return {"ok": True, "how": "apt", "removes": removes, "root": True, "warn": warn,
                    "cmd": ["apt-get", "remove", "-y", pkg],
                    "note": "Settings in your home folder are kept."}
        return {"ok": True, "how": "dpkg", "removes": [pkg], "root": True, "warn": "",
                "cmd": ["dpkg", "-r", pkg], "note": ""}
    if pkg and mgr == "rpm":
        for tool, cmd in (("dnf", ["dnf", "remove", "-y", pkg]), ("zypper", ["zypper", "-n", "rm", pkg]),
                          ("yum", ["yum", "remove", "-y", pkg]), ("rpm", ["rpm", "-e", pkg])):
            if which(tool):
                return {"ok": True, "how": tool, "removes": [pkg], "root": True, "cmd": cmd,
                        "warn": "Packages that depend on it may be removed too." if tool != "rpm" else "",
                        "note": ""}
    if pkg and mgr == "pacman":
        out = sh(["pacman", "-Rp", "--print-format", "%n", pkg], timeout=60)
        removes = [l.strip() for l in out.splitlines() if l.strip()] or [pkg]
        return {"ok": True, "how": "pacman", "removes": removes, "root": True,
                "cmd": ["pacman", "-R", "--noconfirm", pkg], "warn": "", "note": ""}
    if pkg and mgr == "portage" and which("emerge"):
        return {"ok": True, "how": "emerge", "removes": [pkg], "root": True,
                "cmd": ["emerge", "--unmerge", "--quiet", pkg], "warn": "",
                "note": "Run \"emerge --depclean\" afterwards to remove what only it needed."}
    if pkg:
        for tool, cmd, root in (("apk", ["apk", "del", pkg], True), ("xbps-remove", ["xbps-remove", "-y", pkg], True),
                                ("eopkg", ["eopkg", "rm", "-y", pkg], True), ("nix-env", ["nix-env", "-e", pkg], False)):
            if which(tool):
                return {"ok": True, "how": tool.split("-")[0], "removes": [pkg], "root": root, "cmd": cmd,
                        "warn": "", "note": ""}
    # No package: an app someone added by hand
    path = row.get("path") or ""
    home = os.path.expanduser("~")
    if path.startswith(home) and path.endswith(".desktop"):
        return {"ok": True, "how": "menu entry", "removes": [os.path.basename(path)], "root": False,
                "cmd": ["__delete__", path], "warn": "",
                "note": ("No package manager installed this; it is a menu entry in your home folder. "
                         "Removing it takes it out of the app menu and this list. If it is a web app or a "
                         "program in its own folder, its files are not deleted.")}
    return {"ok": False, "how": "", "removes": [], "root": False, "cmd": [], "warn": "",
            "note": (f"{name} was not installed by a package manager Taskscope knows (it is a "
                     f"menu entry in {os.path.dirname(path) or 'a system folder'}). Remove it the way "
                     "it was installed, for example with its own uninstaller.")}


def run(p):
    cmd = p["cmd"]
    if not cmd:
        return False, p.get("note") or "Nothing to do."
    if cmd[0] == "__delete__":
        try:
            os.remove(cmd[1])
            return True, ""
        except OSError as e:
            return False, str(e)
    if p["root"] and os.geteuid() != 0:
        return elevate.run_root(cmd, timeout=900)
    out = sh(cmd, timeout=900)
    return True, out


def gone(row):
    """Check with the package manager that the app is really gone."""
    src, pkg, mgr = row.get("source", ""), row.get("package"), row.get("manager")
    if src == "Flatpak" and row.get("appid"):
        return row["appid"] not in sh(["flatpak", "list", "--app", "--columns=application"])
    if src == "Snap" and row.get("appid"):
        return not any(l.split()[:1] == [row["appid"]] for l in sh(["snap", "list"]).splitlines()[1:])
    if pkg and mgr == "dpkg":
        st = sh(["dpkg-query", "-W", "-f=${db:Status-Status}", pkg]).strip()
        return st != "installed"
    if pkg and mgr == "rpm":
        return not sh(["rpm", "-q", pkg]).strip() or "not installed" in sh(["rpm", "-q", pkg])
    if pkg and mgr == "pacman":
        return not sh(["pacman", "-Q", pkg]).strip()
    if pkg and mgr == "apk":
        return not sh(["apk", "info", "-e", pkg]).strip()
    if pkg and mgr == "xbps":
        return not sh(["xbps-query", pkg]).strip()
    path = row.get("path")
    return bool(path) and not os.path.exists(path)
