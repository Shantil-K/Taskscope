"""JSON settings stored under $XDG_CONFIG_HOME/taskscope/settings.json."""

import json
import os

CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "taskscope")
PATH = os.path.join(CONFIG_DIR, "settings.json")

DEFAULTS = {
    "interval_ms": 1000,
    "theme": "dark",            # a key of gui.theme.THEMES, or "system"
    "process_view": "tree",     # tree | flat
    "nav_collapsed": False,
    "proc_header": None,        # process view column widths/order/sort
    "always_on_top": False,
    "last_page": 0,
    "remote_enabled": False,
    "remote_host": "127.0.0.1",
    "remote_port": 8765,
    "remote_token": "",
    "screensaver_audio": False,
    "exp_ext4_scan": False,     # experimental: read ext4 directly when scanning disk space
    # appearance
    "font_family": "",          # "" = pick a good installed font
    "font_size": 10,
    "temp_unit": "C",           # C | F
    "mini_monitor": False,      # small always-on-top monitor window
    "mini_opacity": 90,         # percent
    "mini_hover_opaque": True,  # fully opaque while the mouse is over it
    "mini_pos": None,
    # colours (Theme popup)
    "accent": "",               # "" = the theme's own accent colour
    "color_display": "color",   # color | accent | mono
    "color_saturation": 11,     # 0..11, 11 = full colour
    "color_glow": 0,            # 0..11 glow around graph lines
    "density": "normal",        # compact | normal | comfortable
    # graphs
    "graph_keyed": True,
    "graph_dashed": True,
    "graph_smooth": False,
    "graph_points": 60,
    # general
    "start_page": "last",       # "last" or a page title
    "show_self": True,          # list Taskscope itself among the processes
    "cpu_per_core": False,
    "cpu_graph": "cores",       # Performance > CPU: "cores" (one graph each) or "overall"      # process CPU %: 100 % = one full core
    "confirm_kill": True,       # ask before End task / Kill
    # window
    "tray_icon": False,         # live icon in the notification area
    "close_to_tray": False,     # closing the window keeps Taskscope in the notification area
    "autostart": False,
    "fit_pages": False,         # True: the window grows to fit each page (no page scrolling)         # start when you log in
    "data_dir": "",             # where data recordings are saved ("" = default)
    "geometry": None,
}


def load():
    data = dict(DEFAULTS)
    try:
        with open(PATH) as f:
            stored = json.load(f)
        if isinstance(stored, dict):
            data.update({k: v for k, v in stored.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return data


def save(data):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, PATH)


def reset():
    try:
        os.remove(PATH)
        return True
    except FileNotFoundError:
        return False
