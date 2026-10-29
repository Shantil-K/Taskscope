"""Start a .desktop application, or open a file or folder, using whatever
launcher the desktop offers.

When Taskscope runs as administrator, apps and file managers are started as
the user who asked for administrator rights, inside that user's own desktop
session. Many desktop apps fail or refuse to start as root (no session bus,
wrong settings), and none of them should get root rights just because
Taskscope has them.
"""

import os
import pwd
import shlex
import subprocess
import sys

from .sysinfo.probe import which


def launch_desktop(entry):
    """entry: dict with 'id' (desktop file name) and 'exec'."""
    fid = entry.get("id", "")
    try:
        argv = [a for a in shlex.split(entry.get("exec", "")) if not (len(a) == 2 and a[0] == "%")]
    except ValueError as e:
        return False, str(e)
    # Apps that ask for a password themselves (Exec=pkexec ...): pkexec refuses to
    # run once the program that started it has exited, and gtk-launch & co. exit at
    # once. So start these directly: Taskscope stays alive as their parent.
    if argv and os.path.basename(argv[0]) == "pkexec":
        return spawn(argv)
    # Preferred: let the desktop environment launch it (handles Terminal=, env, etc.)
    if fid.endswith(".desktop"):
        if which("gtk-launch"):
            return spawn(["gtk-launch", fid[:-8]])
        if which("kioclient"):
            return spawn(["kioclient", "exec", fid])
        if which("dex"):
            return spawn(["dex", fid])
    # Fallback: run the Exec line ourselves (field codes like %f/%u dropped above).
    if not argv:
        return False, "This entry has no command."
    return spawn(argv)


def open_path(path):
    """Open a file or folder in the user's file manager / default app."""
    return spawn(["xdg-open", path])


def original_uid():
    """The desktop user behind an administrator Taskscope, or None when
    Taskscope runs as a normal user (or root logged in directly)."""
    if os.geteuid() != 0:
        return None
    for key in ("TASKSCOPE_USER_UID", "PKEXEC_UID", "SUDO_UID"):
        v = os.environ.get(key, "")
        if v.isdigit() and int(v) != 0:
            return int(v)
    return None


_env_cache = {}


def session_env(uid):
    """The environment of *uid*'s desktop session, copied from one of their
    own processes (so the session bus, display and desktop settings match)."""
    if uid in _env_cache:
        return _env_cache[uid]
    display = os.environ.get("DISPLAY")
    found = None
    try:
        pids = sorted(int(p) for p in os.listdir("/proc") if p.isdigit())
    except OSError:
        pids = []
    for pid in pids:
        try:
            if os.stat(f"/proc/{pid}").st_uid != uid:
                continue
            with open(f"/proc/{pid}/environ", "rb") as f:
                raw = f.read()
        except OSError:
            continue
        env = dict(x.split("=", 1) for x in raw.decode("utf-8", "replace").split("\0") if "=" in x)
        if "DBUS_SESSION_BUS_ADDRESS" not in env or not ("DISPLAY" in env or "WAYLAND_DISPLAY" in env):
            continue
        # prefer a process started by the desktop itself (it carries the desktop's
        # name and data folders) on the same display as Taskscope
        score = ((display is None or env.get("DISPLAY") == display) * 4
                 + ("XDG_CURRENT_DESKTOP" in env) * 2 + ("XDG_DATA_DIRS" in env))
        if found is None or score > found[0]:
            found = (score, env)
            if score == 7:
                break
    found = found[1] if found else None
    if found is None:                              # no session process readable: build one
        pw = pwd.getpwuid(uid)
        run = f"/run/user/{uid}"
        found = {"HOME": pw.pw_dir, "USER": pw.pw_name, "LOGNAME": pw.pw_name,
                 "SHELL": pw.pw_shell or "/bin/sh",
                 "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                 "XDG_RUNTIME_DIR": run, "DBUS_SESSION_BUS_ADDRESS": f"unix:path={run}/bus"}
        for k in ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP"):
            if k in os.environ:
                found[k] = os.environ[k]
    _env_cache[uid] = found
    return found


def spawn(argv):
    """Start *argv* detached; as the desktop user when Taskscope is administrator."""
    kw = {"start_new_session": True, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    uid = original_uid()
    if uid is not None:
        try:
            pw = pwd.getpwuid(uid)
        except KeyError:
            return False, f"Unknown user id {uid}."
        groups = os.getgrouplist(pw.pw_name, pw.pw_gid)
        kw.update(env=session_env(uid), cwd=pw.pw_dir if os.path.isdir(pw.pw_dir) else "/")
        if sys.version_info >= (3, 9):
            kw.update(user=uid, group=pw.pw_gid, extra_groups=groups)
        else:                                      # Python 3.8: drop rights by hand
            def demote():
                os.setgroups(groups)
                os.setgid(pw.pw_gid)
                os.setuid(uid)
            kw["preexec_fn"] = demote
    try:
        subprocess.Popen(argv, **kw)
        return True, ""
    except (OSError, ValueError) as e:
        return False, str(e)


_spawn = spawn                                    # older name
