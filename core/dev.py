"""Hub Developer from the start (owner decision 2026-10-02: every hub should have a Hub Developer from the start who
knows how to manage updates): an agent that maintains this hub's code — its session folder is the repo itself. A system
agent (core/system.py, owner decision 2026-10-08): always present, the core brings it back on every start."""
import os

from core import i18n

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ABOUT = "core.dev.about"   # catalog keys: the agent's description is translated where the spec is made


# a hub from a package (core/packaged.py): the same Hub Developer (owner decision 2026-11-09: "the full Hub Developer must
# stay with everyone"), in <state>/own — its plugins and modules, and the core's working copy own/core (core/ownsrc.py),
# since the hub runs from a built release that is not edited
APP_ABOUT = "core.dev.app_about"


def spec(cfg, state_dir=None):
    """(name, session.requested data) — what it should be like (core/system.py brings it back if absent)."""
    from core import packaged
    name = (cfg.get("dev") or {}).get("name") or "hub-dev"
    if packaged.info() is not None and state_dir:
        from core import ownsrc
        cwd = ownsrc.settle(state_dir)   # plugins-dev of an earlier core moves to own here
        return name, {"cwd": cwd, "role": "agent", "about": i18n.t(APP_ABOUT), "icon": "🧑‍💻", "tools": ["core:hub-developer"]}
    return name, {"cwd": REPO, "role": "agent", "about": i18n.t(ABOUT), "icon": "🧑‍💻", "tools": ["core:hub-developer"]}
