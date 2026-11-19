"""A session's own folder: where it works if no folder was named (owner decision 2026-10-08: it starts not in a shared
sandbox but in a folder with its own ID). A folder shared by several sessions mixed their files, Claude Code settings and conversations."""
import os


def path(state_dir, name):
    """<state folder>/workspaces/<name> — created on first access."""
    d = os.path.join(state_dir, "workspaces", name)
    os.makedirs(d, exist_ok=True)
    return d
