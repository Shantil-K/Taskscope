"""Logged-in users: loginctl when systemd-logind runs, otherwise who/utmp."""

import time

from .probe import run, which

_cache = {"t": -1e9, "v": []}


def sessions(max_age=10.0):
    if time.monotonic() - _cache["t"] < max_age:
        return _cache["v"]
    _cache["v"], _cache["t"] = _sessions(), time.monotonic()
    return _cache["v"]


def _sessions():
    out = []
    if which("loginctl"):
        text = run(["loginctl", "list-sessions", "--no-legend", "--no-pager"])
        for line in text.splitlines():
            p = line.split()
            if len(p) >= 3:
                sid = p[0]
                info = {}
                for kv in run(["loginctl", "show-session", sid, "-p", "Type", "-p", "TTY",
                               "-p", "Remote", "-p", "State", "-p", "Class"]).splitlines():
                    k, _, v = kv.partition("=")
                    info[k] = v
                out.append({"user": p[2], "session": sid, "tty": info.get("TTY", ""),
                            "type": info.get("Type", ""), "state": info.get("State", ""),
                            "remote": info.get("Remote", "") == "yes",
                            "class": info.get("Class", "")})
        if out:
            return out
    if which("who"):
        for line in run(["who"]).splitlines():
            p = line.split()
            if len(p) >= 2:
                out.append({"user": p[0], "session": "", "tty": p[1], "type": "",
                            "state": "active", "remote": "(" in line, "class": "user"})
    return out


def summary(procs):
    """Combine sessions with per-user CPU/memory totals from the process list."""
    by_user = {}
    for p in procs:
        u = by_user.setdefault(p.user, {"user": p.user, "cpu": 0.0, "rss": 0, "procs": 0,
                                        "sessions": 0, "ttys": set()})
        u["cpu"] += p.cpu
        u["rss"] += p.rss
        u["procs"] += 1
    for s in sessions():
        u = by_user.setdefault(s["user"], {"user": s["user"], "cpu": 0.0, "rss": 0,
                                           "procs": 0, "sessions": 0, "ttys": set()})
        u["sessions"] += 1
        if s["tty"]:
            u["ttys"].add(s["tty"])
    res = [dict(u, ttys=", ".join(sorted(u["ttys"]))) for u in by_user.values()]
    return sorted(res, key=lambda u: (-u["sessions"], -u["cpu"]))
