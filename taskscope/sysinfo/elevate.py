"""Privilege elevation with per-distro fallbacks.

Order: already root -> pkexec (polkit) -> kdesu / lxqt-sudo / gksudo ->
sudo with a graphical askpass -> doas / sudo in a terminal. Different distros
ship different subsets of these, so we probe at call time.
"""

import os
import shlex
import subprocess
import sys

from .probe import first_cmd, has_display, is_root, which

_ASKPASS = ("ssh-askpass", "ksshaskpass", "lxqt-openssh-askpass", "x11-ssh-askpass")
_TERMINALS = ("x-terminal-emulator", "gnome-terminal", "konsole", "xfce4-terminal",
              "mate-terminal", "tilix", "alacritty", "kitty", "foot", "xterm")


def method():
    """Name of the elevation method that will be used, or None."""
    if is_root():
        return "root"
    if which("pkexec"):
        return "pkexec"
    for c in ("kdesu", "kdesudo", "lxqt-sudo", "gksudo", "gksu"):
        if which(c):
            return c
    if which("sudo") and (os.environ.get("SUDO_ASKPASS") or first_cmd(*_ASKPASS)):
        return "sudo-askpass"
    if which("doas"):
        return "doas"
    if which("sudo"):
        return "sudo"
    return None


def wrap(args):
    """Return *args* wrapped so they run as root, or None if impossible.

    The returned command may need a GUI password prompt (polkit, askpass).
    Plain sudo/doas get wrapped in a terminal emulator when one exists.
    """
    m = method()
    if m == "root":
        return list(args)
    if m == "pkexec":
        return ["pkexec"] + list(args)
    if m in ("kdesu", "kdesudo"):
        return [m, "-c", shlex.join(args)]
    if m in ("lxqt-sudo", "gksudo", "gksu"):
        return [m] + list(args)
    if m == "sudo-askpass":
        return ["sudo", "-A"] + list(args)
    if m in ("doas", "sudo"):
        if sys.stdin and sys.stdin.isatty():
            return [m] + list(args)
        term = first_cmd(*_TERMINALS)
        if term and has_display():
            inner = f"{m} {shlex.join(args)}; echo; read -p 'Press Enter to close' _"
            if term in ("gnome-terminal",):
                return [term, "--", "sh", "-c", inner]
            return [term, "-e", "sh", "-c", inner]
    return None


def env_for_sudo():
    env = dict(os.environ)
    if "SUDO_ASKPASS" not in env:
        ap = first_cmd(*_ASKPASS)
        if ap:
            env["SUDO_ASKPASS"] = which(ap)
    return env


def run_root(args, timeout=120):
    """Run *args* as root. Returns (ok, message)."""
    cmd = wrap(args)
    if not cmd:
        return False, "No way to gain administrator rights (install polkit or sudo)."
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env=env_for_sudo())
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    msg = (r.stdout + r.stderr).strip()
    if r.returncode in (126, 127) and cmd[0] == "pkexec":
        return False, msg or "Authorization was cancelled."
    return r.returncode == 0, msg


def relaunch_as_root(extra_args=()):
    """Start an elevated copy of this program (GUI) keeping the display env."""
    keep = ["DISPLAY", "XAUTHORITY", "XDG_SESSION_TYPE", "QT_QPA_PLATFORM",
            "DBUS_SESSION_BUS_ADDRESS"]
    if os.environ.get("WAYLAND_DISPLAY"):
        # Wayland clients find the compositor socket through XDG_RUNTIME_DIR.
        # On X11 it isn't needed, and passing the user's own runtime dir to root
        # only makes Qt warn that root doesn't own it.
        keep += ["WAYLAND_DISPLAY", "XDG_RUNTIME_DIR"]
    envs = [f"{k}={os.environ[k]}" for k in keep if k in os.environ]
    if "XAUTHORITY" not in os.environ:
        guess = os.path.expanduser("~/.Xauthority")
        if os.path.exists(guess):
            envs.append(f"XAUTHORITY={guess}")
    envs.append("TASKSCOPE_ELEVATED=1")
    envs.append(f"TASKSCOPE_USER_UID={os.getuid()}")     # apps it opens start as this user again
    # share Home's recent readings with the administrator window (it saves back to the same file)
    hist = os.environ.get("TASKSCOPE_HISTORY") or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "taskscope", "home-history.json.gz")
    envs.append(f"TASKSCOPE_HISTORY={hist}")
    me = [sys.executable, "-m", "taskscope"] + list(extra_args)
    pkg_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    envs.append(f"PYTHONPATH={pkg_root}")
    cmd = wrap(["env"] + envs + me)
    if not cmd:
        return False, "No way to gain administrator rights (install polkit or sudo)."
    try:
        subprocess.Popen(cmd, env=env_for_sudo(), start_new_session=True)
        return True, ""
    except OSError as e:
        return False, str(e)
