"""The real claude for a call that sets its own isolation (the critic, the watchdog line, a module's model call), bypassing
the session wrapper (hosts/nested/claude, marker HUB_NESTED_FLAGS): the Windows wrapper needs HUB_NESTED_* which such a
call's environment lacks (16.10: "is not a command"). A file of its own, stdlib only: a process of the previous release
keeps its core modules in memory and imports this one fresh from the new tree (platforms only for the fallback name:
claude_installed is in every release since 1.7)."""
import os
import shutil


def _wrapper(path):
    try:
        with open(path, "rb") as f:
            return b"HUB_NESTED_FLAGS" in f.read(4096)
    except OSError:
        return False


def find():
    """The one the core gave the session (HUB_REAL_CLAUDE: a call from a session runs that session's claude), else the
    release's own copy (core/ownclaude: a call outside a session takes what the core takes), else PATH past the
    wrapper's folders, else where `claude install` puts it — never a bare "claude", which PATH would turn into the
    wrapper."""
    given = os.environ.get("HUB_REAL_CLAUDE") or ""
    if os.path.isabs(given) and os.path.isfile(given) and not _wrapper(given):
        return given
    try:
        from core import ownclaude
        own = None if os.environ.get("HUB_CLAUDE") else ownclaude.mine()   # the owner's own claude: as before
    except Exception:  # noqa: BLE001 — a tree without the file (a process of an older release): as before
        own = None
    if own:
        ownclaude.switches()
        return own
    path = os.pathsep.join(d for d in os.environ.get("PATH", "").split(os.pathsep)
                           if d and not _wrapper(os.path.join(d, "claude")))
    found = shutil.which("claude", path=path)
    if found:
        return found
    import platforms   # the OS's own name of the command (claude.exe on Windows)
    return platforms.current.claude_installed(os.path.expanduser("~"))
