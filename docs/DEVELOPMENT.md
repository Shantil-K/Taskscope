# Developing Taskscope

[← Back to the README](../README.md)

## Run it from the source folder

```sh
git clone https://github.com/Shantil-K/Taskscope.git
cd Taskscope
python3 -m taskscope
```

You need Python 3.8 or newer and one Qt binding (PySide6, PyQt6, PyQt5 or
PySide2). Without Qt, Taskscope opens in the terminal instead. There are no
other Python dependencies: everything else is read from `/proc` and `/sys`.

## Install from source

```sh
./install.sh            # for you, into ~/.local (installs dependencies with your package manager)
./install.sh --system   # for everyone, into /usr/local
./install.sh --no-deps  # only copy the files
./uninstall.sh          # remove it again (add --purge to delete settings too)
```

The installer knows apt, dnf/yum, zypper, pacman, apk, xbps, emerge and eopkg.
For each dependency it tries every package name it knows until one installs.
If no Qt binding can be installed from the distro, it tries pip, and failing
that, Taskscope runs in the terminal.

## Command line

```
taskscope                       window (or terminal mode with no display / no Qt)
taskscope --tui                 terminal mode
taskscope --check               show what was detected on this machine
taskscope --remote [--host H] [--port N]   summary web page, no window
taskscope --screensaver [-root | -window-id ID] [--audio]
taskscope --register-screensaver
taskscope --reset-settings
taskscope --self-test
```

## Keyboard shortcuts

| Keys | Does |
|---|---|
| `Ctrl+1` … `Ctrl+9` | Switch pages |
| `Ctrl+F` / `Esc` | Filter the list / clear the filter |
| `F5` | Refresh |
| `Ctrl+P` | Pause updates |
| `Ctrl+N` | Run a command |
| `Ctrl+Shift+R` | Start / stop recording data |
| `Ctrl+O` | Replay a recording |
| `F12` | Screensaver |
| `Ctrl+Q` | Quit |

Starting Taskscope again brings the window that's already open to the front.

## Tests

```sh
python3 -m unittest discover -s tests                 # unit tests (no display needed)
QT_QPA_PLATFORM=offscreen python3 -m taskscope --self-test   # every page, offscreen
TASKSCOPE_QT=pyqt5 python3 -m taskscope --self-test   # try another Qt binding
```

## Project layout

| Path | What's there |
|---|---|
| `taskscope/sysinfo/` | Reading the system: processes, performance, packages, services, drivers… (no Qt) |
| `taskscope/gui/` | The window and every page (Qt) |
| `taskscope/tui.py` | Terminal mode |
| `data/` | Desktop entry, AppStream metadata, icon, man page |
| `packaging/` | nFPM config and install scripts for the release packages |
| `tests/` | Unit tests |

## Making a release

Packages are built by GitHub Actions (`.github/workflows/release.yml`, using
[nFPM](https://nfpm.goreleaser.com) with `packaging/nfpm.yaml`).

1. Set the new version in `taskscope/__init__.py` and commit.
2. Push a matching tag:

   ```sh
   git push && git tag v0.2.0 && git push origin v0.2.0
   ```

The workflow checks the tag matches the version, runs the tests, builds the
.deb, .rpm, Arch and Alpine packages and a source tarball, installs the .deb to
make sure it runs, and publishes everything as a GitHub Release with checksums.

To try a build without releasing, run **Release packages** by hand from the
Actions tab: the packages are kept as a downloadable artifact.
