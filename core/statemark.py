"""The mark of the default state folder: hub-core/state-dir in the settings folder, written by the installers (the bare
`hub` command, hubd and the helpers lead to the folder it names). Its own file: a process of the previous release
imports the readers from the new tree on disk lazily, while its `platforms` is the old one in memory and has no new names."""
import os

import platforms


def pointers(home=None):
    """The files that may hold the mark, the first with a folder in it wins: in the OS settings folder (XDG_CONFIG_HOME on
    Linux, where the hub also reads hub.json), then in ~/.config (installs that ignored XDG; the same file on a Mac).
    The OS folder is this process's: for the home folder of someone else (home=) only its ~/.config."""
    mine = os.path.expanduser("~")
    home = home or mine
    folders = ([platforms.current.config_dir()] if os.path.realpath(home) == os.path.realpath(mine) else []) \
        + [os.path.join(home, ".config", "hub-core")]
    folders = [os.path.normpath(f) for f in folders]
    return [os.path.join(f, "state-dir") for i, f in enumerate(folders) if f not in folders[:i]]


def default_state(home=None):
    """The state folder a mark names, or None."""
    for path in pointers(home):
        try:
            with open(path, encoding="utf-8") as f:
                got = f.read().strip()
        except OSError:
            continue
        if got:
            return got
    return None
