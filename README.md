# Taskscope

A lightweight, open-source task manager and system monitor for Linux that
works across distributions. It reads the kernel's `/proc` and `/sys` files
directly. For anything the kernel doesn't expose, it uses whatever tools the
distro ships and falls back to an alternative when one is missing.

## Features

| Page / feature | What it does | Backends (tried in order) |
|---|---|---|
| Home | One-glance dashboard: LED meters (CPU, clock, temperature, GPU), CPU graph with temperature and kernel time, busiest processes, memory graph, and pressure / network / disk / GPU cards. Adapts to the window: columns, rows and cards drop out as it gets smaller | same as Performance + `/proc/pressure` |
| Processes | Parent/child tree or flat list with heat-map CPU/memory cells, details panel for the selected process; end/kill/suspend/resume, set priority, open file location, properties | `/proc` (no psutil needed) |
| System Info | Your distribution's logo plus sections for Hardware Overview, Processor, Memory, Graphics, Storage, Power, Firmware, Network, Operating System, Data Sources, Capabilities and Snapshot (live Property/Value trees, copy one or all) | `/proc`, `/sys`, `/sys/class/dmi`, os-release + icon theme for the logo, `lspci` (optional) |
| Power & Freq | Sensor tree grouped by device (processor, GPUs, memory, drives, battery, other chips) with live value plus min/max: clocks, load, temperatures, fans, power (RAPL, battery, GPU), governor and platform profile | `/sys/class/hwmon`, cpufreq, powercap, power_supply; GPU via the Performance backends |
| Connections | TCP/UDP sockets with application, service name (HTTPS, DNS, …), exposure (all interfaces / loopback / local network / internet), PID and user | `/proc/net/*` + fd scan (only while the page is open) |
| Disk Space | Disk usage analyser: scan a drive or folder, then explore it as a folder tree with % of parent, a file-type list, a colour treemap (click to find, hover for details, highlight a type), and a File View that searches names or paths (wildcards too) and finds duplicates; export to CSV, move items to the Trash | GNU `find` on all cores → Python `os.scandir`; hard links counted once. Experimental (Settings, off by default): as administrator on ext2/3/4, read directory blocks and inode tables straight from the device (read-only, ~1.2× faster) |
| Performance | Overview (cards with live graphs for every device; click one for details), then live graphs for CPU (per core), memory, each disk, each network adapter, each GPU (with dedicated memory) | `/proc`, `/sys`; GPUs: NVML → `nvidia-smi` → amdgpu sysfs → i915/xe sysfs |
| Installed Apps | Every desktop app with its logo, size on disk, version, publisher, source (deb, rpm, pacman, apk, xbps, Portage, eopkg, Flatpak, Snap or plain desktop entry), scope and command; summary counts including total space used (shared packages counted once); launch apps (as you, even from the administrator window); uninstall with the package manager that installed it, after showing what will be removed (apt and pacman list every dependent package too) and asking you to confirm; optionally all other packages too | `.desktop` files + your icon theme; package databases read directly (dpkg, rpm, pacman, apk, xbps, Portage, eopkg), nix; Flatpak, Snap; removal via apt / dnf / zypper / pacman / apk / xbps / eopkg / emerge / flatpak / snap |
| Startup apps | Enable/disable autostart entries and systemd user units | XDG autostart (per-user override, never edits `/etc`); unit files |
| Users | Sessions with per-user CPU/memory totals | `loginctl` → `who` |
| Services | List, start, stop, restart, enable, disable | systemd → OpenRC → s6 → runit → SysV `service` |
| Drivers | Every kernel module (loaded, not loaded, built-in, out-of-tree) with summary counts, filters and a details panel (description, dependencies, devices, file path) | `/proc/modules`, `/sys/module`, `modules.dep`, `modules.builtin`, `/sys/bus`; `modinfo` for the selected module (optional) |
| Run as administrator | Elevated copy of the app / privileged actions | pkexec → kdesu / lxqt-sudo / gksudo → sudo + askpass → doas/sudo in a terminal |
| Recordings | Record Taskscope's data (performance + every process, each second) to a compressed file and replay it later in Summary, Performance, Processes and Users, with pause, seek and up to 30× speed | gzip JSON lines in `~/.local/share/taskscope/recordings` (~20 MB/hour) |
| Screensaver (F12) | Live-load animation, optionally sound-reactive; XScreenSaver support | QPainter (no OpenGL needed); audio via `parec` → `pw-record` |
| Remote summary | Read-only live web page, token-protected, localhost by default | Python standard library |
| Terminal mode | Processes / performance / services in curses | used automatically with no display or no Qt |

GUI toolkit: **PySide6 → PyQt6 → PyQt5 → PySide2**, whichever the distro has.
To force one, set `TASKSCOPE_QT=pyqt5` (or another binding name).

## Install

```sh
./install.sh            # current user, into ~/.local (dependencies via your package manager)
./install.sh --system   # everyone, into /usr/local
./install.sh --no-deps  # just copy files
taskscope --check       # show what was detected on this machine
```

Supported package managers: apt, dnf/yum, zypper, pacman, apk, xbps, emerge
and eopkg. For each dependency the installer tries every known package name
until one works. If no Qt binding can be installed, Taskscope runs in the
terminal. Only Python 3.8+ is required.

Remove with `./uninstall.sh` (add `--purge` to delete settings too).

You can also run it straight from the source folder: `python3 -m taskscope`.

## Command line

```
taskscope                       GUI (or terminal mode when there is no display / Qt)
taskscope --tui                 terminal mode
taskscope --check               detected backends
taskscope --remote [--host H] [--port N]   summary web page without a window
taskscope --screensaver [-root | -window-id ID] [--audio]
taskscope --register-screensaver
taskscope --reset-settings
taskscope --self-test
```

Keyboard: `Ctrl+1…9` switch pages, `F5` refresh, `F12` screensaver,
`Ctrl+N` run a command, `Ctrl+Shift+R` start/stop recording data, `Ctrl+O`
replay a recording, `Ctrl+P` pause.
Launching Taskscope again raises the window that is already open.

## Look

A dark (or light) Windows 11-style layout: icon sidebar that collapses to
icons only, large page titles with the page's controls in the header, a
details panel under the process tree, and a status footer with a record
button. Icons and the logo are drawn by Taskscope itself, and fonts come from
your system (Segoe UI or Selawik if installed, otherwise Inter, Noto Sans,
Cantarell, Ubuntu or DejaVu Sans). Themes: Dark, Light, Midnight blue, Graphite,
Forest, Ocean, Rose, Paper, High contrast, or Follow system; pick one from
the Theme button or in Settings. The Theme button's Colors popup also sets
the accent colour (presets or any colour), how graphs are coloured (per device,
accent only, or greyscale), saturation, glow on graph lines, and row density.

## Low overhead by design

* One timer drives everything. While the window is minimised or hidden it
  keeps sampling (so graphs and Home's history have no holes) but draws nothing.
* The process list, the most expensive reading, is only sampled while a page
  that shows it is open.
* Per tick, each process costs one read of `/proc/<pid>/stat` (and `io` for
  your own processes). Name, command line, executable and owner are cached.
* No external program runs on a timer. NVIDIA GPUs are read through NVML
  in-process. Addresses, mounts and login sessions are cached for 10 s.
* Tables sort and filter in Python and only repaint the visible rows. Graph
  backgrounds are cached as images.
* The disk analyser only reads the disk when you press Scan. It splits the
  tree into batches that several worker processes scan at once (one `find`
  run per batch), so all CPU cores share the work. Results are
  kept in compact arrays (about 130 bytes per file) and dropped on the
  next scan.
* Slow lists (packages, services, drivers) load once, in a worker thread,
  when the page is first opened. systemd unit states come from the unit
  files on disk, not `systemctl list-unit-files`.

## Licensing and origin

Taskscope is free software under the **GNU GPL v3 or later** (see `LICENSE`).

It is an independent, clean-room project. It was written from scratch,
using only the public descriptions of what a task manager does. It contains
no code, binaries, icons, fonts, artwork, names or trademarks from any
proprietary task manager, and it was not made by decompiling or
disassembling one. In particular it is **not affiliated with Task Manager
TMOG or Plummers' Software LLC**. Their licence forbids redistributing their
software, presenting it as your own, or bypassing its PRO access controls,
and Taskscope does none of those things. It doesn't use or interact with
their program, licence keys or servers.

Runtime dependencies keep their own licences: Qt bindings (PyQt: GPL v3 /
commercial; PySide: LGPL v3 / commercial). These are compatible with
Taskscope's GPL v3. Optional external tools (nvidia-smi, lspci, etc.) run as
separate programs and are not distributed with Taskscope.
