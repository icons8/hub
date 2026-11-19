"""Whether the code of a function is in this tree: the older name of core/embed.py's slots, kept for every place that asks
it (and for a process started on the previous release that reads the new files until its restart). The open flavor is
the core without that code — the built-in login of accounts (its gateway and conduit), the reply voice and the stats
report; the core then works without them: sessions on this machine's Claude Code login, a reply stays text, no report
goes anywhere — a third-party module with the capability may take the function over (core/providers.py). The code is
the core's own or an embed's (core/embed.py); either way it is "built-in" for the core.

Every place that would import that code asks present() first. The answer is the tree's: a process sees the tree it was
started from (a layer is replaced whole, the hub restarts after). The stdlib only."""
from core import embed

REPO = embed.CORE
# a function -> the file whose presence means its code is in the tree (core/embed.SLOTS)
FILES = {name: spec["file"] for name, spec in embed.SLOTS.items()}
NEEDS = embed.NEEDS
# the parts of hub-up that run only that code (bin/hub-up, core/restart.PARTS)
PARTS = {part: slot for part, (slot, _) in embed.PARTS.items()}


def present(name, repo=None):
    """The code of function name (one of FILES) is in the tree repo (this core's by default), with what it needs (NEEDS).
    Asked of the disk every time. Tests of the hub command name functions to treat as absent in HUB_TEST_CUT
    (comma-separated): the command runs in its own process."""
    return embed.present(name, repo)


def parts(names, repo=None):
    """names without the parts whose code is not in the tree."""
    return [n for n in names if n not in PARTS or present(PARTS[n], repo)]
