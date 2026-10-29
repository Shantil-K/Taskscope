"""Command-line entry point.

taskscope                   start the GUI (or the TUI when there is no display / no Qt)
taskscope --tui             force the terminal interface
taskscope --check           print what was detected on this system and exit
taskscope --remote          serve the read-only summary page without a GUI
taskscope --screensaver …   run the screensaver (-root, -window-id ID, --audio)
taskscope --register-screensaver [--no-open]
taskscope --reset-settings  delete the settings file and exit
taskscope --self-test       exercise every backend and page, exit 0 on success
"""

import argparse
import os as _os
# some app icons have odd colour profiles or huge SVG sizes; Qt's warnings about them are noise
_os.environ.setdefault("QT_LOGGING_RULES", "qt.gui.imageio.warning=false;qt.svg.draw.warning=false")
import os
import sys
import time

from . import APP_ID, APP_NAME, __version__, settings


def _check():
    from .sysinfo import elevate, packages, perf, probe, services
    pf = perf.PerfSampler()
    try:
        from .gui import qt
        toolkit = qt.version_string()
    except ImportError:
        toolkit = "none (terminal interface only)"
    rows = [
        ("Distribution", probe.distro_name()),
        ("Session", probe.session_type()),
        ("GUI toolkit", toolkit),
        ("Service manager", ", ".join(m.name for m in services.detect()) or "none"),
        ("Package sources", ", ".join(packages.available_sources())),
        ("GPUs", "; ".join(f"{c['name']} [{c['backend']}]" for c in pf.gpu.cards) or "none"),
        ("Elevation", elevate.method() or "unavailable"),
        ("lspci / modinfo / ip", " ".join(f"{c}:{'yes' if probe.which(c) else 'no'}"
                                          for c in ("lspci", "modinfo", "ip"))),
    ]
    for k, v in rows:
        print(f"{k:<22}{v}")
    return 0


def _self_test(gui):
    from .sysinfo import drivers, packages, perf, procs, services, startup, users
    failures = 0

    def check(name, fn):
        nonlocal failures
        t = time.perf_counter()
        try:
            res = fn()
            n = len(res) if hasattr(res, "__len__") else ""
            print(f"  ok   {name:<22} {n!s:>6}  {1000 * (time.perf_counter() - t):7.1f} ms")
        except Exception as e:  # noqa: BLE001 - report everything
            failures += 1
            print(f"  FAIL {name:<22} {e!r}")

    ps, pf = procs.ProcessSampler(), perf.PerfSampler()
    ps.sample()
    pf.sample()
    time.sleep(0.25)
    plist = ps.sample()
    check("processes", ps.sample)
    check("performance", pf.sample)
    check("users", lambda: users.summary(plist))
    for m in services.detect():
        check(f"services ({m.name})", m.list)
    for src in packages.available_sources():
        check(f"apps ({src})", lambda src=src: packages.list_source(src))
    check("startup", startup.list_entries)
    check("drivers", drivers.inventory)
    if gui:
        try:
            from .gui.qt import QtWidgets
            from .gui.main_window import MainWindow
            app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([APP_NAME])
            win = MainWindow(dict(settings.DEFAULTS))
            for i in range(len(win.pages)):
                win.open_index(i)
                app.processEvents()
                win.tick()
            t0 = time.time()
            while time.time() - t0 < 3:
                app.processEvents()
                time.sleep(0.05)
            win.close()
            print("  ok   gui pages")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"  FAIL gui: {e!r}")
    print("self-test", "passed" if not failures else f"FAILED ({failures})")
    return 0 if not failures else 1


def _remote_only(host, port):
    from .sysinfo import perf, procs, remote
    ps, pf = procs.ProcessSampler(), perf.PerfSampler()
    pf.sample()                      # prime the counters so the first reading has a delta
    ps.sample(with_io=False)
    state = {"t": time.monotonic(), "snap": None}

    def snapshot():
        # Sample on demand, at most once a second, so an idle server costs nothing.
        if state["snap"] is None or time.monotonic() - state["t"] >= 1.0:
            state["snap"] = remote.make_snapshot(pf.sample(), ps.sample(with_io=False))
            state["t"] = time.monotonic()
        return state["snap"]
    srv = remote.RemoteServer(snapshot, host, port)
    print(f"Serving read-only summary at {srv.start()}  (Ctrl+C to stop)", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.stop()
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "--screensaver":
        from .gui.screensaver import run_standalone
        return run_standalone(argv[1:])

    ap = argparse.ArgumentParser(prog="taskscope", description=f"{APP_NAME} - lightweight Linux task manager")
    ap.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    ap.add_argument("--tui", action="store_true", help="use the terminal interface")
    ap.add_argument("--interval", type=float, default=1.0, help="TUI refresh interval in seconds")
    ap.add_argument("--check", action="store_true", help="show detected backends and exit")
    ap.add_argument("--remote", action="store_true", help="serve the summary page without a GUI")
    ap.add_argument("--host", default="127.0.0.1", help="address for --remote (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8765, help="port for --remote")
    ap.add_argument("--register-screensaver", action="store_true")
    ap.add_argument("--no-open", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--reset-settings", action="store_true", help="delete the settings file and exit")
    ap.add_argument("--self-test", action="store_true", help="exercise every backend and exit")
    a = ap.parse_args(argv)

    if a.reset_settings:
        print("Settings deleted." if settings.reset() else "There were no settings to delete.")
        return 0
    if a.check:
        return _check()
    if a.register_screensaver:
        from .sysinfo import xscreensaver
        print(xscreensaver.register(open_settings=not a.no_open))
        return 0
    if a.remote:
        return _remote_only(a.host, a.port)

    from .sysinfo import probe
    want_gui = not a.tui and probe.has_display()
    qt_ok = False
    if want_gui or a.self_test:
        try:
            from .gui import qt  # noqa: F401
            qt_ok = True
        except ImportError as e:
            if want_gui:
                print(f"{e}\nFalling back to the terminal interface. "
                      "Install PyQt6, PySide6 or PyQt5 for the GUI.", file=sys.stderr)
    if a.self_test:
        return _self_test(gui=qt_ok and probe.has_display())
    if not (want_gui and qt_ok):
        from . import tui
        return tui.run(a.interval)

    from .gui.main_window import MainWindow, listen_for_summon, try_forward_to_running
    from .gui.qt import QtGui, QtWidgets, exec_
    QtGui.QGuiApplication.setDesktopFileName(APP_ID)
    app = QtWidgets.QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    if try_forward_to_running():
        return 0
    win = MainWindow(settings.load())
    listen_for_summon(win)
    _quit_on_signals(app, win)
    win.show()
    return exec_(app)


def _quit_on_signals(app, win):
    """Logging out, `kill` or Ctrl+C close the window properly, so Home's
    history and the settings are saved instead of lost."""
    import signal

    def quit_(*_):
        win._quitting = True                     # past "close to tray"
        win.close()
        app.quit()
    for name in ("SIGTERM", "SIGHUP", "SIGINT"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), quit_)
    app.aboutToQuit.connect(lambda: [p.save_history() for p in win.pages if hasattr(p, "save_history")])


if __name__ == "__main__":
    sys.exit(main())
