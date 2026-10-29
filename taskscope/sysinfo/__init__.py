"""System information backends.

Everything in this package is toolkit-free so it can be shared by the Qt GUI,
the curses TUI and the remote summary server. Each module prefers reading the
kernel's own files (/proc, /sys) and only shells out to optional tools when the
kernel does not expose the data. When a tool is missing, the next alternative
is tried; when none exists the feature reports itself as unavailable instead of
failing.
"""
