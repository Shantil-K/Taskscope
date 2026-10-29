"""Add the Taskscope screensaver to ~/.xscreensaver's program list."""

import os
import shutil

from .probe import first_cmd

LINE = '  "Taskscope" taskscope --screensaver -root \\n\\'


def register(open_settings=True):
    path = os.path.expanduser("~/.xscreensaver")
    try:
        with open(path) as f:
            text = f.read()
    except FileNotFoundError:
        text = ""
    if "taskscope --screensaver" in text:
        msg = "Already registered."
    elif not text:
        with open(path, "w") as f:
            f.write("programs:\t\\\n" + LINE + "\n\n")
        msg = f"Created {path}."
    else:
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("programs:"):
                if line.rstrip().endswith("\\"):
                    lines.insert(i + 1, LINE)
                else:
                    # single-line value: turn it into a continued list
                    lines[i] = "programs:\t\\"
                    lines.insert(i + 1, LINE)
                break
        else:
            lines += ["programs:\t\\", LINE, ""]
        shutil.copy2(path, path + ".bak")
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
        msg = f"Registered (previous file kept as {path}.bak)."
    if open_settings:
        tool = first_cmd("xscreensaver-settings", "xscreensaver-demo")
        if tool:
            from .. import launcher
            launcher.spawn([tool])
    return msg
