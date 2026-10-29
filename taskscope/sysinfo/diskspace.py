"""Mounted file systems and their free space (statvfs, no `df` needed)."""

import os

from .probe import read

SKIP_FS = {"proc", "sysfs", "devtmpfs", "devpts", "cgroup", "cgroup2", "securityfs", "pstore",
           "bpf", "tracefs", "debugfs", "mqueue", "hugetlbfs", "configfs", "fusectl",
           "autofs", "binfmt_misc", "efivarfs", "rpc_pipefs", "nsfs", "ramfs", "squashfs",
           "overlay", "fuse.portal", "fuse.gvfsd-fuse"}


def list_mounts(include_tmpfs=False):
    out, seen = [], set()
    for line in read("/proc/mounts").splitlines():
        p = line.split()
        if len(p) < 4:
            continue
        dev, mnt, fs = p[0], p[1].replace("\\040", " "), p[2]
        if fs in SKIP_FS or (fs == "tmpfs" and not include_tmpfs):
            continue
        if mnt.startswith("/snap/") or (dev, fs) in seen and fs != "tmpfs":
            continue
        try:
            st = os.statvfs(mnt)
        except OSError:
            continue
        total = st.f_blocks * st.f_frsize
        if total == 0:
            continue
        free = st.f_bavail * st.f_frsize
        used = total - st.f_bfree * st.f_frsize
        seen.add((dev, fs))
        out.append({"mount": mnt, "device": dev, "fs": fs, "total": total, "used": used,
                    "free": free, "percent": used * 100.0 / total if total else 0.0,
                    "readonly": "ro" in p[3].split(",")})
    return sorted(out, key=lambda m: m["mount"])
