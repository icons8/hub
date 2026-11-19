"""OS adapters, like hosts/ for Claude Code: everything the core and its parts use to talk to the OS (paths, locks,
stopping, processes) lives here, one module per OS family with a single interface: posix.py (Mac, Linux) and
windows.py. The core calls only `current`; a change for one OS leaves the others alone."""
import os


def real_pid(pid):
    """A pid that is safe to signal: an int above 1. Signals to 0, -1 or 1 reach the caller's own group, every process
    of the user or init (killpg(1) is kill(-1)), and a stand-in process from a test (a mock) has pid 1."""
    return type(pid) is int and pid > 1


# Marker of a session's process tree: "<name>@<hub folder>" in claude's environment. Everything the session started
# inherits it, and the core uses it to find those processes after the session is gone for good (POSIX; on Windows a job
# holds the tree). A process the core starts on its own (popen_detached: agents' Chrome) does not get the marker.
TREE_ENV = "HUB_TREE"

if os.name == "nt":
    from platforms import windows as current
else:
    from platforms import posix as current

__all__ = ["current", "real_pid", "TREE_ENV"]

