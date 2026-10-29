"""Curses interface: used over SSH, on servers, or when no Qt binding exists.

Keys:  Tab/1-3 switch view   s cycle sort   / filter   k end task
       K kill   r renice      p pause        q quit
"""

import curses
import time

from . import fmt
from .sysinfo import perf, procs, services

SORTS = [("cpu", "CPU"), ("rss", "Memory"), ("pid", "PID"), ("name", "Name")]
VIEWS = ["Processes", "Performance", "Services"]


def _bar(win, y, x, width, frac, label, color):
    width = max(10, width)
    filled = int(width * max(0.0, min(1.0, frac)))
    try:
        win.addstr(y, x, label.ljust(12))
        win.addstr(y, x + 12, "█" * filled, curses.color_pair(color))
        win.addstr(y, x + 12 + filled, "·" * (width - filled))
        win.addstr(y, x + 13 + width, f"{frac * 100:5.1f}%")
    except curses.error:
        pass


class TUI:
    def __init__(self, scr, interval):
        self.scr = scr
        self.interval = interval
        self.ps = procs.ProcessSampler()
        self.pf = perf.PerfSampler()
        self.view = 0
        self.sort = 0
        self.filter = ""
        self.sel = 0
        self.top = 0
        self.paused = False
        self.msg = ""
        self.services = None
        self.rows = []
        self.perf = self.pf.sample()
        self.plist = self.ps.sample(with_io=False)
        self.hist = []

    def sample(self):
        self.perf = self.pf.sample()
        if self.view == 0:
            self.plist = self.ps.sample(with_io=False)
        self.hist = (self.hist + [self.perf["cpu"]["total"]])[-200:]

    def header(self):
        s = self.scr
        h, w = s.getmaxyx()
        c, m = self.perf["cpu"], self.perf["memory"]
        tabs = "  ".join(f"[{v}]" if i == self.view else f" {v} " for i, v in enumerate(VIEWS))
        s.addstr(0, 0, f" Taskscope  {tabs}"[: w - 1], curses.A_BOLD)
        bw = max(10, w - 32)
        _bar(s, 1, 1, bw, c["total"] / 100, "CPU", 1)
        _bar(s, 2, 1, bw, m["percent"] / 100, "Memory", 2)
        sw = m["swap_used"] / m["swap_total"] if m["swap_total"] else 0
        _bar(s, 3, 1, bw, sw, "Swap", 3)
        return 5

    def draw_procs(self, y0):
        s = self.scr
        h, w = s.getmaxyx()
        key = SORTS[self.sort][0]
        rows = self.plist
        if self.filter:
            f = self.filter.lower()
            rows = [p for p in rows if f in p.name.lower() or f in p.cmdline.lower() or f == str(p.pid)]
        rows = sorted(rows, key=lambda p: getattr(p, key), reverse=key in ("cpu", "rss"))
        self.rows = rows
        hdr = f"{'PID':>7} {'USER':<10} {'CPU%':>6} {'MEM':>9} {'S':<1} NAME"
        s.addstr(y0, 0, hdr.ljust(w - 1)[: w - 1], curses.A_REVERSE)
        avail = h - y0 - 2
        self.sel = max(0, min(self.sel, len(rows) - 1))
        if self.sel < self.top:
            self.top = self.sel
        if self.sel >= self.top + avail:
            self.top = self.sel - avail + 1
        for i, p in enumerate(rows[self.top:self.top + avail]):
            line = f"{p.pid:>7} {p.user[:10]:<10} {p.cpu:>6.1f} {fmt.size(p.rss):>9} {p.state[0]} {p.name}  {p.cmdline}"
            attr = curses.A_REVERSE if self.top + i == self.sel else 0
            try:
                s.addstr(y0 + 1 + i, 0, line.ljust(w - 1)[: w - 1], attr)
            except curses.error:
                pass

    def draw_perf(self, y0):
        s = self.scr
        h, w = s.getmaxyx()
        c = self.perf["cpu"]
        y = y0
        s.addstr(y, 0, f"{c['model']}  up {fmt.duration(c['uptime'])}  load {' '.join(f'{x:.2f}' for x in c['load'])}"[: w - 1])
        y += 1
        spark = " ▁▂▃▄▅▆▇█"
        s.addstr(y, 0, "".join(spark[min(8, int(v / 12.5))] for v in self.hist[-(w - 1):]), curses.color_pair(1))
        y += 2
        half = max(10, (w - 40) // 2)
        for i, v in enumerate(c["cores"]):
            col = 0 if i % 2 == 0 else half + 22
            if y + i // 2 < h - 1:
                _bar(s, y + i // 2, col, half, v / 100, f"cpu{i}", 1)
        y += (len(c["cores"]) + 1) // 2 + 1
        for d in self.perf["disks"]:
            if y < h - 1:
                s.addstr(y, 0, f"Disk {d['name']:<8} {d['kind']:<9} busy {d['busy']:5.1f}%  R {fmt.rate(d['read_rate']):>10}  W {fmt.rate(d['write_rate']):>10}"[: w - 1])
                y += 1
        for n in self.perf["net"]:
            if y < h - 1 and n["up"]:
                s.addstr(y, 0, f"Net  {n['name']:<10} {n['kind']:<8} ↓ {fmt.bits(n['rx_rate']):>10}  ↑ {fmt.bits(n['tx_rate']):>10}"[: w - 1])
                y += 1
        for g in self.perf["gpus"]:
            if y < h - 1:
                u = "n/a" if g["util"] is None else f"{g['util']:.0f}%"
                s.addstr(y, 0, f"GPU  {g['name'][:40]:<40} {u:>5}  [{g['backend']}]"[: w - 1])
                y += 1

    def draw_services(self, y0):
        s = self.scr
        h, w = s.getmaxyx()
        if self.services is None:
            mgrs = services.detect()
            self.mgr = mgrs[0] if mgrs else None
            self.services = self.mgr.list() if self.mgr else []
        rows = [r for r in self.services if self.filter.lower() in r["name"].lower()]
        self.rows = rows
        s.addstr(y0, 0, f"{'SERVICE':<40} {'STATE':<10} {'SUB':<10} STARTUP   ({self.mgr.name if self.mgr else 'none'})".ljust(w - 1)[: w - 1], curses.A_REVERSE)
        avail = h - y0 - 2
        self.sel = max(0, min(self.sel, len(rows) - 1))
        if self.sel < self.top:
            self.top = self.sel
        if self.sel >= self.top + avail:
            self.top = self.sel - avail + 1
        for i, r in enumerate(rows[self.top:self.top + avail]):
            attr = curses.A_REVERSE if self.top + i == self.sel else 0
            line = f"{r['name'][:40]:<40} {r['state']:<10} {r['sub'][:10]:<10} {r['enabled']}"
            try:
                s.addstr(y0 + 1 + i, 0, line.ljust(w - 1)[: w - 1], attr)
            except curses.error:
                pass

    def footer(self):
        h, w = self.scr.getmaxyx()
        keys = "q quit  Tab view  s sort  / filter  k end  K kill  r renice  p pause" if self.view == 0 else \
               "q quit  Tab view  / filter  S start  T stop  R restart" if self.view == 2 else "q quit  Tab view  p pause"
        text = (self.msg or f"{keys}   sort: {SORTS[self.sort][1]}" + (f"   filter: {self.filter}" if self.filter else "")
                + ("   [paused]" if self.paused else ""))
        try:
            self.scr.addstr(h - 1, 0, text.ljust(w - 1)[: w - 1], curses.A_REVERSE)
        except curses.error:
            pass

    def prompt(self, label):
        h, w = self.scr.getmaxyx()
        curses.echo()
        curses.curs_set(1)
        self.scr.addstr(h - 1, 0, label.ljust(w - 1)[: w - 1])
        self.scr.move(h - 1, len(label))
        self.scr.timeout(-1)
        try:
            val = self.scr.getstr(h - 1, len(label), 60).decode(errors="replace")
        finally:
            curses.noecho()
            curses.curs_set(0)
        return val

    def selected(self):
        return self.rows[self.sel] if 0 <= self.sel < len(self.rows) else None

    def handle(self, ch):
        self.msg = ""
        if ch in (ord("q"), 27):
            return False
        if ch == 9:
            self.view = (self.view + 1) % len(VIEWS)
            self.sel = self.top = 0
            if self.view == 0:
                self.plist = self.ps.sample(with_io=False)
        elif ch in (ord("1"), ord("2"), ord("3")):
            self.view = ch - ord("1")
            self.sel = self.top = 0
        elif ch == curses.KEY_DOWN:
            self.sel += 1
        elif ch == curses.KEY_UP:
            self.sel = max(0, self.sel - 1)
        elif ch == curses.KEY_NPAGE:
            self.sel += 20
        elif ch == curses.KEY_PPAGE:
            self.sel = max(0, self.sel - 20)
        elif ch == ord("s"):
            self.sort = (self.sort + 1) % len(SORTS)
        elif ch == ord("p"):
            self.paused = not self.paused
        elif ch == ord("/"):
            self.filter = self.prompt("Filter: ").strip()
            self.sel = 0
        elif self.view == 0 and ch in (ord("k"), ord("K")):
            p = self.selected()
            if p:
                fn = procs.terminate if ch == ord("k") else procs.kill
                ok, m = fn(p.pid)
                self.msg = m or (f"Signal sent to {p.name} ({p.pid})" if ok else "Failed")
        elif self.view == 0 and ch == ord("r"):
            p = self.selected()
            if p:
                try:
                    ok, m = procs.renice(p.pid, int(self.prompt(f"Nice for {p.pid} (-20..19): ")))
                    self.msg = m or ("Priority changed" if ok else "Failed")
                except ValueError:
                    self.msg = "Not a number"
        elif self.view == 2 and ch in (ord("S"), ord("T"), ord("R")) and self.mgr:
            r = self.selected()
            if r:
                action = {ord("S"): "start", ord("T"): "stop", ord("R"): "restart"}[ch]
                curses.def_prog_mode()
                curses.endwin()          # sudo/pkexec may prompt in the terminal
                ok, m = self.mgr.act(action, r["name"])
                curses.reset_prog_mode()
                self.msg = m[:200] if m else f"{action}: {'ok' if ok else 'failed'}"
                self.services = None
        return True

    def loop(self):
        curses.curs_set(0)
        curses.use_default_colors()
        for i, c in enumerate((curses.COLOR_BLUE, curses.COLOR_MAGENTA, curses.COLOR_YELLOW), 1):
            curses.init_pair(i, c, -1)
        next_t = 0
        while True:
            now = time.monotonic()
            if now >= next_t and not self.paused:
                self.sample()
                next_t = now + self.interval
            self.scr.erase()
            try:
                y = self.header()
                [self.draw_procs, self.draw_perf, self.draw_services][self.view](y)
            except curses.error:
                pass
            self.footer()
            self.scr.refresh()
            self.scr.timeout(max(50, int((next_t - time.monotonic()) * 1000)) if not self.paused else 500)
            ch = self.scr.getch()
            if ch != -1 and not self.handle(ch):
                return


def run(interval=1.0):
    curses.wrapper(lambda scr: TUI(scr, interval).loop())
    return 0
