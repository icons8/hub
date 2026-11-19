"""Parts the hub fetches on first use (installer spec §4, §12: Chrome for Testing is not in the package — «Setup»
downloads it): where they live in the state folder. `<state>/runtime/chrome/<version>/` holds the unpacked build and
`<state>/runtime/chrome.json` {"version", "path"} names its program; the agents' browser (modules/browser
find_chrome) takes it when the machine has no Chrome of its own. A package may keep one for the whole machine
(PACKAGED "shared", shared()): it comes first.

The stdlib only: it is read by the browser launcher and by git's environment in every hub process."""
import json
import os

CHROME_FILE = "chrome.json"


def state_dir():
    """This hub's state folder as the core finds it (core/daemon.default_state_dir), without importing the daemon."""
    if os.environ.get("HUB_STATE_DIR"):
        return os.environ["HUB_STATE_DIR"]
    try:
        import platforms
        from core import statemark
        return statemark.default_state() or platforms.current.state_dir()
    except Exception:  # noqa: BLE001 — no state folder known
        return ""


def folder(state):
    return os.path.join(state, "runtime")


def _record(base):
    """The program that base/chrome.json names ({"version", "path"}; relative to base), or None."""
    try:
        with open(os.path.join(base, CHROME_FILE), encoding="utf-8") as f:
            got = json.load(f)
    except (OSError, ValueError):
        return None
    path = got.get("path") if isinstance(got, dict) else None
    if not isinstance(path, str) or not path:
        return None
    path = os.path.join(base, path) if not os.path.isabs(path) else path
    return path if os.path.isfile(path) else None


def shared():
    """The Chrome a package keeps for the whole machine (PACKAGED "shared": its folder), or None. On Linux it is root's:
    Ubuntu 23.10+ gives the sandbox only to a program with an AppArmor profile, and a profile may name only a path
    that users cannot write (modules/setup/chrome.install_shared). A record that leads out of the folder is not followed."""
    try:
        from core import packaged
        base = (packaged.info() or {}).get("shared")
    except Exception:  # noqa: BLE001 — no package: nothing shared
        return None
    if not isinstance(base, str) or not os.path.isabs(base):
        return None
    path = _record(base)
    if path is None:
        return None
    real = os.path.realpath(base)
    return path if os.path.commonpath([real, os.path.realpath(path)]) == real else None


def chrome(state=None):
    """The program of the Chrome the package keeps (shared), else the one «Setup» downloaded into the state folder, or
    None (not downloaded, or its files are gone)."""
    got = shared()
    if got:
        return got
    state = state if state is not None else state_dir()
    return _record(folder(state)) if state else None
