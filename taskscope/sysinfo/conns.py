"""Network connections from /proc/net/{tcp,tcp6,udp,udp6} with owning process.

Mapping sockets to processes needs a scan of /proc/*/fd, so this only runs
when the Connections page asks (on open, on Refresh, or every few seconds
while it is visible). Sockets of other users' processes need root to map.
"""

import ipaddress
import os
import pwd
import socket
import struct
import time

from .probe import read_fast

TCP_STATES = {"01": "Established", "02": "SYN sent", "03": "SYN received", "04": "FIN wait 1",
              "05": "FIN wait 2", "06": "Time wait", "07": "Closed", "08": "Close wait",
              "09": "Last ACK", "0A": "Listening", "0B": "Closing"}


_GENERIC = {"sh", "bash", "dash", "env", "python", "python3", "perl", "java", "node", "gjs",
            "flatpak", "snap", "electron", "wine", "mono", "ruby", "xdg-open", "pkexec", "sudo"}
_apps = {"t": -1e9, "map": {}}


def _app_names():
    """Process name -> application name, from installed .desktop files
    (e.g. "code" -> "Visual Studio Code"). Rebuilt at most once a minute."""
    if time.monotonic() - _apps["t"] < 60:
        return _apps["map"]
    from .packages import desktop_apps
    m = {}
    for a in desktop_apps():
        words = [w.strip('"\'') for w in a["exec"].split()]
        while words and (words[0] == "env" or "=" in words[0]):
            words.pop(0)
        if not words:
            continue
        exe = os.path.basename(words[0])
        if exe and exe not in _GENERIC:
            m.setdefault(exe, a["name"])
            m.setdefault(exe[:15], a["name"])      # comm is cut to 15 characters
    _apps["map"], _apps["t"] = m, time.monotonic()
    return m


_services = {}
_NICE = {"domain": "DNS", "bootps": "DHCP", "bootpc": "DHCP", "microsoft-ds": "SMB",
         "netbios-ns": "NetBIOS", "netbios-dgm": "NetBIOS", "netbios-ssn": "NetBIOS",
         "postgresql": "PostgreSQL", "mysql": "MySQL", "mdns": "mDNS", "www": "HTTP",
         "http-alt": "HTTP", "ipp": "IPP", "kerberos": "Kerberos", "openvpn": "OpenVPN"}


def _service(port, proto):
    k = (port, proto)
    if k not in _services:
        try:
            n = socket.getservbyport(port, proto)
            _services[k] = _NICE.get(n, n.upper())
        except OSError:
            _services[k] = ""
    return _services[k]


def _exposure(local_ip, remote_ip, listening):
    """Who can reach this socket: All interfaces / Loopback / Local network / Internet."""
    try:
        loc = ipaddress.ip_address(local_ip)
        rem = ipaddress.ip_address(remote_ip)
    except ValueError:
        return ""
    if listening:
        if loc.is_unspecified:
            return "All interfaces"
        if loc.is_loopback:
            return "Loopback"
        return "Local network" if loc.is_private or loc.is_link_local else "Internet"
    if rem.is_loopback:
        return "Loopback"
    if rem.is_private or rem.is_link_local:
        return "Local network"
    return "Internet"


def _addr(hexaddr, v6):
    ip, port = hexaddr.split(":")
    raw = bytes.fromhex(ip)
    if v6:
        raw = b"".join(struct.pack("<I", struct.unpack(">I", raw[i:i + 4])[0]) for i in range(0, 16, 4))
        text = socket.inet_ntop(socket.AF_INET6, raw)
        if text.startswith("::ffff:"):
            text = text[7:]
        else:
            text = f"[{text}]"
    else:
        text = socket.inet_ntop(socket.AF_INET, struct.pack("<I", int(ip, 16)))
    return f"{text}:{int(port, 16)}"


def _ip_port(addr):
    ip, _, port = addr.rpartition(":")
    return ip.strip("[]"), int(port)


def _inode_owners():
    """socket inode -> pid for every process we may look into, plus
    uid -> [pids] for all processes (used when the fd scan is denied)."""
    owners, by_uid = {}, {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            uid = os.stat(f"/proc/{pid}").st_uid
        except OSError:
            continue
        by_uid.setdefault(uid, []).append(int(pid))
        fd_dir = f"/proc/{pid}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                link = os.readlink(f"{fd_dir}/{fd}")
            except OSError:
                continue
            if link.startswith("socket:["):
                owners[link[8:-1]] = int(pid)
    return owners, by_uid


def _comm(pid):
    return read_fast(f"/proc/{pid}/comm").strip()


_users = {}
_main = {}


def _ppid(pid):
    st = read_fast(f"/proc/{pid}/stat", 512).rpartition(")")[2].split()
    return int(st[1]) if len(st) > 1 else 0


def _main_pid(uid, by_uid):
    """The one process of a service account that was started by another user
    (its daemon); the rest are its children. None when there isn't exactly one.
    Not used for root, which runs many unrelated services."""
    pids = by_uid.get(uid, ())
    key = (uid, tuple(pids))
    if key not in _main:
        mine = set(pids)
        tops = [p for p in pids if _ppid(p) not in mine]
        if len(_main) > 64:
            _main.clear()
        _main[key] = tops[0] if len(tops) == 1 else None
    return _main[key]



def _user(uid):
    if uid not in _users:
        try:
            _users[uid] = pwd.getpwuid(uid).pw_name
        except KeyError:
            _users[uid] = str(uid)
    return _users[uid]


def list_connections():
    """Rows with pid/process when known. "how" says how the owner was found:
    "fd" (exact), "only" (the socket's user runs just one process, so it must
    be that one), "main" (the user's main daemon process; the socket may belong
    to one of its children), "denied" (needs administrator) or "none" (closing socket
    that no process holds any more)."""
    owners, by_uid = _inode_owners()
    names = _app_names()
    out = []
    for proto in ("tcp", "tcp6", "udp", "udp6"):
        text = read_fast(f"/proc/net/{proto}", 1 << 22)
        for line in text.splitlines()[1:]:
            p = line.split()
            if len(p) < 10:
                continue
            v6 = proto.endswith("6")
            state = TCP_STATES.get(p[3], p[3]) if proto.startswith("tcp") else \
                ("Listening" if p[2].endswith(":0000") else "Connected")
            uid, inode = int(p[7]), p[9]
            pid = owners.get(inode)
            how = "fd"
            if pid is None:
                if inode == "0":
                    how = "none"
                elif len(by_uid.get(uid, ())) == 1:
                    pid, how = by_uid[uid][0], "only"
                elif uid != 0 and _main_pid(uid, by_uid) is not None:
                    pid, how = _main_pid(uid, by_uid), "main"
                else:
                    how = "denied"
            process = _comm(pid) if pid else ""
            if how == "denied":
                process = "Needs administrator"
            elif how == "none":
                process = "None (closing)"
            local, remote = _addr(p[1], v6), _addr(p[2], v6)
            lip, lport = _ip_port(local)
            rip, rport = _ip_port(remote)
            listening = state == "Listening"
            base = proto[:3]
            if listening:
                svc = _service(lport, base)
                service = f"{lport} · {svc}" if svc else str(lport)
            else:
                svc = _service(rport, base) or (_service(lport, base) if lport < 1024 else "")
                service = f"{remote} · {svc}" if svc else remote
            app = names.get(process, process) if how in ("fd", "only", "main") else process
            out.append({"key": (proto, p[1], p[2], inode),
                        "proto": base.upper() + ("6" if v6 else "4"),
                        "local": local, "remote": remote, "service": service,
                        "exposure": _exposure(lip, rip, listening),
                        "state": state, "pid": pid, "process": process, "app": app,
                        "how": how, "user": _user(uid)})
    return out
