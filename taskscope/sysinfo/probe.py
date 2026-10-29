"""Helpers for detecting what this distro provides and running tools safely."""

import functools
import os
import shutil
import subprocess


@functools.lru_cache(maxsize=None)
def which(cmd):
    return shutil.which(cmd)


def first_cmd(*cmds):
    """Return the first command in *cmds* that exists on PATH, or None."""
    for c in cmds:
        if which(c):
            return c
    return None


def run(args, timeout=10, input=None, env=None):
    """Run a command and return its stdout ('' on any failure)."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           input=input, env=env)
        return r.stdout if r.returncode == 0 else (r.stdout or "")
    except (OSError, subprocess.SubprocessError):
        return ""


def run_status(args, timeout=30):
    """Run a command and return (returncode, combined output)."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 127, str(e)


def read(path, default=""):
    try:
        with open(path, "r", errors="replace") as f:
            return f.read()
    except OSError:
        return default


def read_fast(path, size=8192):
    """Raw os.read of a small kernel file; ~5x cheaper than open().read()."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return ""
    try:
        return os.read(fd, size).decode("utf-8", "replace")
    except OSError:
        return ""
    finally:
        os.close(fd)


def read_int(path, default=None):
    try:
        return int(read(path).strip())
    except ValueError:
        return default


@functools.lru_cache(maxsize=1)
def os_release():
    info = {}
    for path in ("/etc/os-release", "/usr/lib/os-release"):
        text = read(path)
        if text:
            for line in text.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    info[k.strip()] = v.strip().strip('"')
            break
    return info


def distro_name():
    r = os_release()
    return r.get("PRETTY_NAME") or r.get("NAME") or "Linux"


def session_type():
    t = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if t:
        return t
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return "tty"


def has_display():
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def is_root():
    return os.geteuid() == 0
