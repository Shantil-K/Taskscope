"""Service managers: systemd, OpenRC, runit, s6 and SysV init, auto-detected."""

import glob
import os
import re

from . import elevate
from .probe import read, run, which


class ServiceManager:
    name = "none"
    actions = ()

    def available(self):
        return False

    def list(self):
        return []

    def act(self, action, unit):
        return False, "Not supported"


class Systemd(ServiceManager):
    name = "systemd"
    actions = ("start", "stop", "restart", "enable", "disable")

    def __init__(self, user=False):
        self.user = user
        if user:
            self.name = "systemd (user)"

    def available(self):
        return os.path.isdir("/run/systemd/system") and bool(which("systemctl"))

    def _base(self):
        return ["systemctl", "--user"] if self.user else ["systemctl"]

    def _unit_dirs(self):
        if self.user:
            home = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
            return [os.path.join(home, "systemd/user"), "/etc/systemd/user",
                    f"/run/user/{os.getuid()}/systemd/user", "/usr/local/lib/systemd/user",
                    "/usr/lib/systemd/user", "/lib/systemd/user"]
        return ["/etc/systemd/system", "/run/systemd/system", "/usr/local/lib/systemd/system",
                "/usr/lib/systemd/system", "/lib/systemd/system"]

    def _unit_file_states(self):
        """Startup state from the unit files themselves. `systemctl
        list-unit-files` does the same but can take seconds; this takes ms."""
        files, wanted = {}, set()
        for d in self._unit_dirs():
            for path in glob.glob(d + "/*.service"):
                n = os.path.basename(path)
                if "@" not in n:
                    files.setdefault(n, path)       # earlier dirs override later ones
            for path in glob.glob(d + "/*.wants/*.service") + glob.glob(d + "/*.requires/*.service"):
                wanted.add(os.path.basename(path))
        states = {}
        for n, path in files.items():
            if os.path.islink(path) and os.path.realpath(path) == "/dev/null":
                states[n] = "masked"
            elif n in wanted:
                states[n] = "enabled"
            elif "[Install]" in read(path):
                states[n] = "disabled"
            else:
                states[n] = "static"
        return states

    def _main_pids(self):
        """unit -> first PID in its cgroup (cgroup v2 / unified layout)."""
        root = "/sys/fs/cgroup"
        base = (f"{root}/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service"
                if self.user else f"{root}/system.slice")
        pids = {}
        for dirpath, dirnames, _ in os.walk(base):
            if dirpath.count("/") - base.count("/") > 2:
                dirnames[:] = []
                continue
            n = os.path.basename(dirpath)
            if n.endswith(".service"):
                first = read(dirpath + "/cgroup.procs").split("\n", 1)[0].strip()
                if first:
                    pids[n] = first
        return pids

    def list(self):
        units = {}
        out = run(self._base() + ["list-units", "--type=service", "--all",
                                  "--no-legend", "--plain", "--no-pager"])
        for line in out.splitlines():
            p = line.split(None, 4)
            if len(p) >= 4 and p[0].endswith(".service"):
                units[p[0]] = {"name": p[0], "state": p[2], "sub": p[3],
                               "description": p[4] if len(p) > 4 else "",
                               "enabled": "", "pid": ""}
        for n, state in self._unit_file_states().items():
            u = units.setdefault(n, {"name": n, "state": "inactive", "sub": "dead",
                                     "description": "", "pid": ""})
            u["enabled"] = state
        for n, pid in self._main_pids().items():
            if n in units and units[n]["state"] == "active":
                units[n]["pid"] = pid
        return sorted(units.values(), key=lambda u: u["name"])

    def act(self, action, unit):
        if self.user:
            from .probe import run_status
            code, msg = run_status(self._base() + [action, unit])
            return code == 0, msg
        return elevate.run_root(["systemctl", action, unit])


class OpenRC(ServiceManager):
    name = "OpenRC"
    actions = ("start", "stop", "restart", "enable", "disable")

    def available(self):
        return bool(which("rc-service") and which("rc-status"))

    def list(self):
        status = {}
        section = ""
        for line in run(["rc-status", "--all", "--nocolor"]).splitlines():
            if line.startswith("Runlevel:"):
                section = line.split(":", 1)[1].strip()
                continue
            m = re.match(r"\s*(\S+)\s+\[\s*(\w+)\s*\]", line)
            if m:
                s = status.setdefault(m.group(1), {"name": m.group(1), "state": m.group(2),
                                                   "sub": "", "description": "",
                                                   "enabled": "", "pid": ""})
                if section and section not in ("hotplugged", "manual", "needed/wanted"):
                    s["enabled"] = section
        for path in glob.glob("/etc/init.d/*"):
            n = os.path.basename(path)
            status.setdefault(n, {"name": n, "state": "stopped", "sub": "", "description": "",
                                  "enabled": "", "pid": ""})
        return sorted(status.values(), key=lambda u: u["name"])

    def act(self, action, unit):
        if action == "enable":
            return elevate.run_root(["rc-update", "add", unit, "default"])
        if action == "disable":
            return elevate.run_root(["rc-update", "del", unit, "default"])
        return elevate.run_root(["rc-service", unit, action])


class Runit(ServiceManager):
    name = "runit"
    actions = ("start", "stop", "restart")
    dirs = ("/var/service", "/service", "/etc/service", "/run/runit/service")

    def available(self):
        return bool(which("sv")) and any(os.path.isdir(d) for d in self.dirs)

    def _dir(self):
        return next(d for d in self.dirs if os.path.isdir(d))

    def list(self):
        out = []
        d = self._dir()
        for path in sorted(glob.glob(d + "/*")):
            n = os.path.basename(path)
            st = run(["sv", "status", path]).strip()
            state = st.split(":", 1)[0] if st else "unknown"
            m = re.search(r"\(pid (\d+)\)", st)
            out.append({"name": n, "state": state, "sub": st, "description": "",
                        "enabled": "enabled", "pid": m.group(1) if m else ""})
        return out

    def act(self, action, unit):
        return elevate.run_root(["sv", {"start": "up", "stop": "down"}.get(action, action),
                                 os.path.join(self._dir(), unit)])


class S6(ServiceManager):
    name = "s6"
    actions = ("start", "stop", "restart")

    def available(self):
        return bool(which("s6-rc")) and os.path.isdir("/run/s6-rc")

    def list(self):
        active = set(run(["s6-rc", "-a", "list"]).split())
        return [{"name": n, "state": "up" if n in active else "down", "sub": "",
                 "description": "", "enabled": "", "pid": ""}
                for n in sorted(set(run(["s6-rc", "-d", "list"]).split()) | active)]

    def act(self, action, unit):
        if action == "restart":
            ok, msg = elevate.run_root(["s6-rc", "-d", "change", unit])
            if not ok:
                return ok, msg
            action = "start"
        flag = "-u" if action == "start" else "-d"
        return elevate.run_root(["s6-rc", flag, "change", unit])


class SysV(ServiceManager):
    name = "SysV init"
    actions = ("start", "stop", "restart")

    def available(self):
        return os.path.isdir("/etc/init.d") and bool(which("service"))

    def list(self):
        out = []
        for line in run(["service", "--status-all"]).splitlines():
            m = re.match(r"\s*\[\s*([+\-?])\s*\]\s+(\S+)", line)
            if m:
                out.append({"name": m.group(2),
                            "state": {"+": "running", "-": "stopped"}.get(m.group(1), "unknown"),
                            "sub": "", "description": "", "enabled": "", "pid": ""})
        return out

    def act(self, action, unit):
        return elevate.run_root(["service", unit, action])


def detect():
    """Return the system service manager plus the user one if present."""
    managers = []
    for cls in (Systemd, OpenRC, S6, Runit, SysV):
        m = cls()
        if m.available():
            managers.append(m)
            break
    if managers and isinstance(managers[0], Systemd):
        u = Systemd(user=True)
        if run(["systemctl", "--user", "is-system-running"]).strip():
            managers.append(u)
    return managers
