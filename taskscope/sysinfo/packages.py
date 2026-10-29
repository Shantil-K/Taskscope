"""Installed applications and packages from whichever package managers exist.

Desktop applications are read from XDG .desktop files, which every desktop
distro has. Packages come from every native manager that is present (dpkg,
rpm, pacman, apk, xbps, eopkg, portage, nix) plus Flatpak and Snap.
"""

import glob
import os

from .probe import read, run, which

DESKTOP_DIRS = ["/usr/share/applications", "/usr/local/share/applications",
                os.path.expanduser("~/.local/share/applications"),
                "/var/lib/flatpak/exports/share/applications",
                os.path.expanduser("~/.local/share/flatpak/exports/share/applications"),
                "/var/lib/snapd/desktop/applications"]


def parse_desktop(path):
    entry, section = {}, None
    for line in read(path).splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line
            continue
        if section == "[Desktop Entry]" and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            entry.setdefault(k.strip(), v.strip())
    return entry


def _desktops():
    """Names of the running desktop (XDG_CURRENT_DESKTOP, e.g. X-Cinnamon)."""
    return {d.strip().lower() for d in os.environ.get("XDG_CURRENT_DESKTOP", "").split(":") if d.strip()}


def shown_here(entry, desktops=None):
    """True when the app menu of this desktop shows the entry (OnlyShowIn /
    NotShowIn, as menus do). Without a known desktop, every entry counts."""
    desktops = _desktops() if desktops is None else desktops
    only = {x.strip().lower() for x in entry.get("OnlyShowIn", "").split(";") if x.strip()}
    nots = {x.strip().lower() for x in entry.get("NotShowIn", "").split(";") if x.strip()}
    if not desktops:
        return not only
    if only and not (only & desktops):
        return False
    return not (nots & desktops)


def desktop_apps():
    xdg = os.environ.get("XDG_DATA_DIRS", "")
    dirs = list(DESKTOP_DIRS) + [os.path.join(d, "applications") for d in xdg.split(":") if d]
    seen, apps = set(), []
    for d in dirs:
        for path in glob.glob(os.path.join(d, "*.desktop")):
            fid = os.path.basename(path)
            if fid in seen:
                continue
            seen.add(fid)
            e = parse_desktop(path)
            if (e.get("Type") != "Application" or e.get("NoDisplay") == "true" or e.get("Hidden") == "true"
                    or not shown_here(e)):
                continue
            src = "Flatpak" if "flatpak" in path else "Snap" if "snapd" in path else \
                  "User" if path.startswith(os.path.expanduser("~")) else "System"
            apps.append({"name": e.get("Name", fid), "version": "", "size": None,
                         "description": e.get("Comment", ""), "source": src,
                         "exec": e.get("Exec", ""), "id": fid, "icon": e.get("Icon", "")})
    return sorted(apps, key=lambda a: a["name"].lower())


def _rows(text, sep="\t"):
    for line in text.splitlines():
        if line.strip():
            yield line.split(sep)


def _pkg(name, version="", size=None, desc="", source=""):
    return {"name": name, "version": version, "size": size, "description": desc,
            "source": source, "exec": "", "id": name}


def _dpkg():
    out = run(["dpkg-query", "-W", "-f=${db:Status-Status}\t${Package}\t${Version}\t"
               "${Installed-Size}\t${binary:Summary}\n"], timeout=30)
    res = []
    for p in _rows(out):
        if len(p) >= 5 and p[0] == "installed":
            res.append(_pkg(p[1], p[2], int(p[3]) * 1024 if p[3].isdigit() else None, p[4], "dpkg"))
    return res


def _rpm():
    out = run(["rpm", "-qa", "--qf", "%{NAME}\t%{VERSION}-%{RELEASE}\t%{SIZE}\t%{SUMMARY}\n"], timeout=30)
    return [_pkg(p[0], p[1], int(p[2]) if p[2].isdigit() else None, p[3], "rpm")
            for p in _rows(out) if len(p) >= 4]


def _pacman():
    res, cur = [], {}
    for line in run(["pacman", "-Qi"], timeout=30).splitlines() + [""]:
        if not line.strip():
            if cur.get("Name"):
                res.append(_pkg(cur["Name"], cur.get("Version", ""), None,
                                cur.get("Description", ""), "pacman"))
            cur = {}
            continue
        k, _, v = line.partition(":")
        if k and not k.startswith(" "):
            cur[k.strip()] = v.strip()
    return res


def _apk():
    res = []
    for line in run(["apk", "info", "-v"], timeout=30).splitlines():
        name, _, ver = line.rpartition("-")
        name, _, rel = name.rpartition("-")
        if name:
            res.append(_pkg(name, f"{rel}-{ver}", None, "", "apk"))
    return res


def _xbps():
    res = []
    for line in run(["xbps-query", "-l"], timeout=30).splitlines():
        p = line.split(None, 2)
        if len(p) >= 2:
            name, _, ver = p[1].rpartition("-")
            res.append(_pkg(name, ver, None, p[2] if len(p) > 2 else "", "xbps"))
    return res


def _eopkg():
    res = []
    for line in run(["eopkg", "li", "-l"], timeout=30).splitlines():
        if line.startswith("Name"):
            parts = dict(x.split(":", 1) for x in line.split(",") if ":" in x)
            res.append(_pkg(parts.get("Name", "").strip(), parts.get(" version", "").strip(),
                            None, "", "eopkg"))
    return res


def _portage():
    res = []
    for line in run(["qlist", "-Iv"], timeout=30).splitlines():
        res.append(_pkg(line.strip(), "", None, "", "portage"))
    return res


def _nix():
    return [_pkg(l.strip(), "", None, "", "nix") for l in run(["nix-env", "-q"], timeout=30).splitlines()]


def _flatpak():
    out = run(["flatpak", "list", "--app", "--columns=name,application,version,size"], timeout=30)
    return [_pkg(p[0], p[2] if len(p) > 2 else "", None, p[1], "Flatpak")
            for p in _rows(out) if p and p[0]]


def _snap():
    res = []
    for line in run(["snap", "list"], timeout=30).splitlines()[1:]:
        p = line.split()
        if len(p) >= 2:
            res.append(_pkg(p[0], p[1], None, "", "Snap"))
    return res


BACKENDS = [
    ("dpkg", "dpkg-query", _dpkg),
    ("rpm", "rpm", _rpm),
    ("pacman", "pacman", _pacman),
    ("apk", "apk", _apk),
    ("xbps", "xbps-query", _xbps),
    ("eopkg", "eopkg", _eopkg),
    ("portage", "qlist", _portage),
    ("nix", "nix-env", _nix),
    ("Flatpak", "flatpak", _flatpak),
    ("Snap", "snap", _snap),
]


def available_sources():
    """Names of package sources usable on this machine ('Applications' first)."""
    srcs = ["Applications"]
    for name, cmd, _ in BACKENDS:
        if which(cmd):
            # rpm exists on some Debian systems without an rpm database
            if name == "rpm" and not os.path.isdir("/var/lib/rpm") and not os.path.isdir("/usr/lib/sysimage/rpm"):
                continue
            srcs.append(name)
    return srcs


def list_source(source):
    if source == "Applications":
        return desktop_apps()
    for name, _, fn in BACKENDS:
        if name == source:
            return fn()
    return []


# ------------------------------------------------------------------ inventory
# One list of installed applications: each .desktop entry joined with the
# package that installed it (version, publisher, source, scope). Optionally
# also the packages that have no desktop entry.

SOURCE_LABELS = {"dpkg": "deb (dpkg)", "rpm": "rpm", "pacman": "pacman", "apk": "apk (Alpine)",
                 "xbps": "xbps (Void)", "portage": "Portage (Gentoo)", "eopkg": "eopkg (Solus)",
                 "nix": "Nix", "Flatpak": "Flatpak", "Snap": "Snap"}


def _who(s):
    """'Kubuntu Developers <x@y>' -> 'Kubuntu Developers'."""
    import re
    s = re.sub(r"\S+@\S+", "", s.split("<")[0])
    return " ".join(s.split())


def _dpkg_index():
    """(desktop path -> package, package -> info) from dpkg's own files."""
    owner = {}
    for lst in glob.glob("/var/lib/dpkg/info/*.list"):
        pkg = os.path.basename(lst)[:-5].split(":")[0]
        for line in read(lst).splitlines():
            if line.endswith(".desktop") and "/applications/" in line:
                owner[line] = pkg
    info = {}
    out = run(["dpkg-query", "-W", "-f=${db:Status-Status}\t${Package}\t${Version}\t"
               "${Maintainer}\t${Installed-Size}\n"], timeout=30)
    for p in _rows(out):
        if len(p) >= 5 and p[0] == "installed":
            info[p[1]] = {"version": p[2], "publisher": _who(p[3]),
                          "size": int(p[4]) * 1024 if p[4].isdigit() else None}
    return owner, info


def _rpm_index(paths):
    owner, info = {}, {}
    if not paths:
        return owner, info
    out = run(["rpm", "-qf", "--qf", "%{NAME}\t%{VERSION}-%{RELEASE}\t%{VENDOR}\t%{SIZE}\n"]
              + list(paths), timeout=30)
    for path, line in zip(paths, out.splitlines()):
        p = line.split("\t")
        if len(p) >= 4 and " " not in p[0]:
            owner[path] = p[0]
            info[p[0]] = {"version": p[1], "publisher": "" if p[2] == "(none)" else p[2],
                          "size": int(p[3]) if p[3].isdigit() else None}
    return owner, info


# Package databases, read directly (no tool needed, and much faster than asking
# the tool once per file). Paths are module-level so tests can point them elsewhere.
PACMAN_DB = "/var/lib/pacman/local"
APK_DB = "/lib/apk/db/installed"
XBPS_DB = "/var/db/xbps"
PORTAGE_DB = "/var/db/pkg"
EOPKG_DB = "/var/lib/eopkg/package"


def _is_app_file(rel):
    rel = "/" + rel.lstrip("/")
    return rel.endswith(".desktop") and "/applications/" in rel, rel


def _pacman_index(paths=None):
    """Arch, Manjaro, EndeavourOS...: local/<name-ver>/desc and files."""
    owner, info = {}, {}
    for d in glob.glob(os.path.join(PACMAN_DB, "*", "")):
        fields, key = {}, None
        for line in read(d + "desc").splitlines():
            if line.startswith("%") and line.endswith("%"):
                key = line.strip("%")
            elif line and key and key not in fields:
                fields[key] = line
        name = fields.get("NAME")
        if not name:
            continue
        size = fields.get("SIZE", "")
        info[name] = {"version": fields.get("VERSION", ""), "publisher": _who(fields.get("PACKAGER", "")),
                      "size": int(size) if size.isdigit() else None}
        for line in read(d + "files").splitlines():
            ok, rel = _is_app_file(line)
            if ok:
                owner[rel] = name
    if not info and which("pacman"):                 # unusual db location: ask pacman
        for line in run(["pacman", "-Qo"] + list(paths or []), timeout=30).splitlines():
            path, sep, rest = line.partition(" is owned by ")
            if sep:
                owner[path] = rest.split()[0]
                info.setdefault(rest.split()[0], {"version": rest.split()[1] if len(rest.split()) > 1 else "",
                                                  "publisher": "", "size": None})
    return owner, info


def _apk_index():
    """Alpine, postmarketOS: one text database, a block per package."""
    owner, info = {}, {}
    for block in read(APK_DB).split("\n\n"):
        name, cur_dir, f = None, "", {}
        for line in block.splitlines():
            k, _, v = line.partition(":")
            if k == "P":
                name = v
            elif k == "F":
                cur_dir = v
            elif k == "R" and name:
                ok, rel = _is_app_file(f"{cur_dir}/{v}")
                if ok:
                    owner[rel] = name
            elif k in ("V", "I", "m", "T") and k not in f:
                f[k] = v
        if name:
            info[name] = {"version": f.get("V", ""), "publisher": _who(f.get("m", "")),
                          "size": int(f["I"]) if f.get("I", "").isdigit() else None}
    return owner, info


def _xbps_index():
    """Void: pkgdb plist (versions, sizes) and a files plist per package."""
    import plistlib
    owner, info = {}, {}
    dbs = sorted(glob.glob(os.path.join(XBPS_DB, "pkgdb-*.plist")))
    try:
        with open(dbs[-1], "rb") as fh:
            db = plistlib.load(fh)
    except (IndexError, OSError, ValueError, plistlib.InvalidFileException):
        db = {}
    for name, d in db.items():
        if isinstance(d, dict):
            ver = d.get("pkgver", "")
            info[name] = {"version": ver[len(name) + 1:] if ver.startswith(name + "-") else ver,
                          "publisher": "", "size": d.get("installed_size")}
    for fl in glob.glob(os.path.join(XBPS_DB, ".*-files.plist")):
        text = read(fl)
        if "/applications/" not in text:             # skip parsing the many that have no apps
            continue
        name = os.path.basename(fl)[1:-len("-files.plist")]
        try:
            files = plistlib.loads(text.encode())
        except (ValueError, plistlib.InvalidFileException):
            continue
        for kind in ("files", "links"):
            for item in files.get(kind, []):
                ok, rel = _is_app_file(item.get("file", ""))
                if ok:
                    owner[rel] = name
    return owner, info


def _portage_index():
    """Gentoo: /var/db/pkg/<category>/<name-version>/{CONTENTS,SIZE}."""
    import re
    owner, info = {}, {}
    for d in glob.glob(os.path.join(PORTAGE_DB, "*", "*", "")):
        cat, pf = d.rstrip("/").split("/")[-2:]
        m = re.match(r"(.+?)-(\d[^-]*(?:-r\d+)?)$", pf)
        name, ver = (f"{cat}/{m.group(1)}", m.group(2)) if m else (f"{cat}/{pf}", "")
        size = read(d + "SIZE").strip()
        info[name] = {"version": ver, "publisher": "", "size": int(size) if size.isdigit() else None}
        for line in read(d + "CONTENTS").splitlines():
            parts = line.split()
            if len(parts) > 1 and parts[0] in ("obj", "sym"):
                ok, rel = _is_app_file(parts[1])
                if ok:
                    owner[rel] = name
    return owner, info


def _eopkg_index():
    """Solus: package/<name-ver>/{metadata.xml,files.xml}."""
    import re
    owner, info = {}, {}
    for d in glob.glob(os.path.join(EOPKG_DB, "*", "")):
        meta = read(d + "metadata.xml")
        m = re.search(r"<Package>.*?<Name>([^<]+)</Name>", meta, re.S)
        if not m:
            continue
        name = m.group(1).strip()
        size = re.search(r"<InstalledSize>(\d+)</InstalledSize>", meta)
        ver = re.search(r"<Update[^>]*>.*?<Version>([^<]+)</Version>", meta, re.S)
        who = re.search(r"<Packager>.*?<Name>([^<]+)</Name>", meta, re.S)
        info[name] = {"version": ver.group(1).strip() if ver else "",
                      "publisher": who.group(1).strip() if who else "",
                      "size": int(size.group(1)) if size else None}
        for path in re.findall(r"<Path>([^<]+)</Path>", read(d + "files.xml")):
            ok, rel = _is_app_file(path)
            if ok:
                owner[rel] = name
    return owner, info


def _human_size(text):
    """'1.2 GB' / '540,3 MB' / '12 kB' -> bytes."""
    import re
    m = re.match(r"\s*([\d.,]+)\s*([kKMGT]?i?)B", text or "")
    if not m:
        return None
    n = float(m.group(1).replace(",", "."))
    base = 1024 if "i" in m.group(0) else 1000
    return int(n * base ** " KMGT".index(m.group(2)[:1].upper() or " "))


def _flatpak_index():
    info = {}
    if which("flatpak"):
        out = run(["flatpak", "list", "--app", "--columns=application,version,origin,installation,size"],
                  timeout=30)
        for p in _rows(out):
            if p and p[0]:
                info[p[0]] = {"version": p[1] if len(p) > 1 else "",
                              "publisher": p[2] if len(p) > 2 else "",
                              "scope": "User" if len(p) > 3 and p[3] == "user" else "System",
                              "size": _human_size(p[4]) if len(p) > 4 else None}
    return info


def _snap_index():
    info = {}
    if which("snap"):
        lines = run(["snap", "list"], timeout=30).splitlines()
        for line in lines[1:]:
            p = line.split()
            if len(p) >= 5:
                snapfile = f"/var/lib/snapd/snaps/{p[0]}_{p[2]}.snap"     # the app's compressed image
                try:
                    size = os.path.getsize(snapfile)
                except OSError:
                    size = None
                info[p[0]] = {"version": p[1], "publisher": p[4].rstrip("✓*✪"), "size": size}
    return info


def inventory(with_packages=False):
    """Rows: name, version, publisher, source, scope, exec, id, kind."""
    xdg = os.environ.get("XDG_DATA_DIRS", "")
    dirs = list(DESKTOP_DIRS) + [os.path.join(d, "applications") for d in xdg.split(":") if d]
    home = os.path.expanduser("~")
    entries, seen = [], set()
    for d in dirs:
        for path in sorted(glob.glob(os.path.join(d, "*.desktop"))):
            fid = os.path.basename(path)
            if fid in seen:
                continue
            seen.add(fid)
            e = parse_desktop(path)
            if (e.get("Type") != "Application" or e.get("NoDisplay") == "true" or e.get("Hidden") == "true"
                    or not shown_here(e)):
                continue
            entries.append((path, fid, e))

    native, owner, info = None, {}, {}
    sys_paths = [p for p, _, _ in entries if not p.startswith(home) and "flatpak" not in p
                 and "snapd" not in p]
    if which("dpkg-query") and os.path.isdir("/var/lib/dpkg/info"):
        native = "dpkg"
        owner, info = _dpkg_index()
    elif which("rpm") and (os.path.isdir("/var/lib/rpm") or os.path.isdir("/usr/lib/sysimage/rpm")):
        native = "rpm"
        owner, info = _rpm_index(sys_paths)
    elif os.path.isdir(PACMAN_DB) or which("pacman"):
        native = "pacman"
        owner, info = _pacman_index(sys_paths)
    elif os.path.isfile(APK_DB):
        native = "apk"
        owner, info = _apk_index()
    elif os.path.isdir(XBPS_DB):
        native = "xbps"
        owner, info = _xbps_index()
    elif os.path.isdir(PORTAGE_DB):
        native = "portage"
        owner, info = _portage_index()
    elif os.path.isdir(EOPKG_DB):
        native = "eopkg"
        owner, info = _eopkg_index()
    flat = _flatpak_index() if any("flatpak" in p for p, _, _ in entries) else {}
    snaps = _snap_index() if any("snapd" in p for p, _, _ in entries) else {}

    rows, owning, seen_rows = [], set(), set()
    for path, fid, e in entries:
        row = {"name": e.get("Name", fid), "version": "", "publisher": "", "size": None,
               "source": "Desktop entry", "scope": "User" if path.startswith(home) else "System",
               "exec": e.get("Exec", ""), "id": fid, "kind": "app", "path": path, "icon": e.get("Icon", ""),
               "description": e.get("Comment", "")}
        pkg = owner.get(path) or owner.get(os.path.realpath(path))
        if pkg and pkg in info:
            row.update(info[pkg], source=SOURCE_LABELS[native], package=pkg, manager=native)
            owning.add(pkg)
        elif "flatpak" in path:
            fi = flat.get(fid[:-8])
            row["source"] = "Flatpak"
            row["appid"] = fid[:-8]
            if fi:
                row.update(fi)
        elif "snapd" in path:
            si = snaps.get(fid.split("_")[0])
            row["source"] = "Snap"
            row["appid"] = fid.split("_")[0]
            if si:
                row.update(si)
        key = (row["name"].lower(), row["exec"].split(" %")[0], row.get("package") or path)
        if key in seen_rows:                      # the same app listed twice
            continue
        seen_rows.add(key)
        rows.append(row)

    if with_packages and native:
        if not info:
            info = {p["name"]: {"version": p["version"], "publisher": "", "size": p["size"]}
                    for p in list_source(native)}
        for pkg, i in info.items():
            if pkg not in owning:
                rows.append({"name": pkg, "version": i["version"], "publisher": i["publisher"],
                             "size": i["size"], "source": SOURCE_LABELS[native], "scope": "System",
                             "exec": "", "id": pkg, "kind": "package", "description": "",
                             "package": pkg, "manager": native})
    return rows
