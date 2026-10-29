"""Kernel modules (drivers): loaded, available on disk and built in, plus the
devices bound to them."""

import glob
import os

from .probe import read, run, which


def _bound_devices():
    """module name -> list of device ids that use it."""
    res = {}
    for drv in glob.glob("/sys/bus/*/drivers/*"):
        mod = os.path.join(drv, "module")
        if not os.path.exists(mod):
            continue
        name = os.path.basename(os.path.realpath(mod))
        bus = drv.split("/")[3]
        try:
            entries = os.listdir(drv)
        except OSError:
            continue
        for dev in entries:
            if dev != "module" and os.path.islink(os.path.join(drv, dev)):
                res.setdefault(name, []).append(f"{bus}:{dev}")
    return res


# ------------------------------------------------------------------ inventory
# Every module this kernel can use: loaded (/proc/modules), available on disk
# (modules.dep) and built into the kernel image (modules.builtin).

def _modname(path):
    base = os.path.basename(path)
    for ext in (".ko.zst", ".ko.xz", ".ko.gz", ".ko"):
        if base.endswith(ext):
            base = base[:-len(ext)]
            break
    return base.replace("-", "_")


def _out_of_tree_path(rel):
    return rel.startswith(("updates/", "extra/")) or "/dkms/" in rel


def _builtin_info(moddir):
    """name -> {key: value} from modules.builtin.modinfo (NUL separated)."""
    info = {}
    try:
        with open(os.path.join(moddir, "modules.builtin.modinfo"), "rb") as f:
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return info
    for item in data.split("\0"):
        mod, dot, kv = item.partition(".")
        k, eq, v = kv.partition("=")
        if dot and eq and k in ("description", "version", "license", "author"):
            info.setdefault(mod, {}).setdefault(k, v)
    return info


def kernel_release():
    return os.uname().release


def inventory():
    moddir = f"/lib/modules/{kernel_release()}"
    rows = {}

    def row(name):
        return rows.setdefault(name, {
            "name": name, "state": "Not loaded", "size": None, "used_by": "", "version": "",
            "srcversion": "", "source": "In-tree", "path": "", "depends": "", "devices": "",
            "description": ""})

    for line in read(os.path.join(moddir, "modules.dep")).splitlines():
        rel, _, deps = line.partition(":")
        if not rel:
            continue
        r = row(_modname(rel))
        r["path"] = os.path.join(moddir, rel)
        r["depends"] = ", ".join(_modname(d) for d in deps.split())
        if _out_of_tree_path(rel):
            r["source"] = "Out-of-tree"
    binfo = _builtin_info(moddir)
    for line in read(os.path.join(moddir, "modules.builtin")).splitlines():
        if line.strip():
            r = row(_modname(line.strip()))
            r["state"] = "Built-in"
            r["path"] = "Built into the kernel image"
            bi = binfo.get(r["name"], {})
            r["description"] = bi.get("description", "")
            r["version"] = bi.get("version", "")
    devices = _bound_devices()
    for line in read("/proc/modules").splitlines():
        p = line.split()
        if len(p) < 5:
            continue
        r = row(p[0])
        r["state"] = "Loaded"
        r["size"] = int(p[1])
        r["used_by"] = ", ".join(x for x in p[3].strip(",").split(",") if x and x != "-")
        r["version"] = read(f"/sys/module/{p[0]}/version").strip()
        r["srcversion"] = read(f"/sys/module/{p[0]}/srcversion").strip()
        if "O" in read(f"/sys/module/{p[0]}/taint"):
            r["source"] = "Out-of-tree"
    for name, devs in devices.items():
        r = row(name)
        r["devices"] = ", ".join(devs)
        if r["state"] == "Not loaded":       # bound but not a module: built in
            r["state"] = "Built-in"
    return sorted(rows.values(), key=lambda m: m["name"])


def modinfo(name):
    """Extra fields for one module (on selection only): description, author, license."""
    out = {}
    if which("modinfo"):
        for line in run(["modinfo", name], timeout=3).splitlines():
            k, _, v = line.partition(":")
            k = k.strip()
            if k in ("description", "author", "license", "version", "srcversion") and k not in out:
                out[k] = v.strip()
    return out
