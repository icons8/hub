"""The "Chrome for tests" module (modelled on VOICE_CMD): which browser the page's e2e tests launch.
Regular Chrome, even with `--headless=new`, shows in the dock for a second on macOS, and the update module runs the tests
on every update, so the dock blinks. chrome-headless-shell is not visible in the dock.

The setting works like voice (`voice.cmd`): the path to the browser in `testchrome.path` in hub.json or in the HUB_TEST_CHROME variable
(it wins). Nothing set: chrome-headless-shell if it is already in the usual places (shell()), otherwise Chrome for Testing
(testing_build()). On macOS regular Google Chrome never runs the tests, even headless (10.10: a dock icon,
Dock/Raycast registration being replaced): with neither of the two, browser tests are skipped with a hint how to install
(INSTALL). Linux and Windows have no dock, so there the last choice is the system Chrome.
The hub installs the browser for the system and writes the path when the owner asks (the hub-developer skill);
the module itself downloads nothing. The same browser takes `hub shot`/`hub video` and draws mermaid diagrams. The path is passed to tests
by the hub update (modules/update) and tests/__init__.py (a run from a session: the tests do not see the machine's hub.json).
"""
import glob
import json
import os
import re
import shlex
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from modules.browser import browsers  # noqa: E402
from core import runtime  # noqa: E402

ENV = "HUB_TEST_CHROME"
# where chrome-headless-shell lies after installation: the settings folder of this hub (shell_patterns() puts it first),
# the fixed folder of earlier installs, the puppeteer cache
# (`npx @puppeteer/browsers install chrome-headless-shell`), the playwright cache; the version in the path changes, take the newest
SHELL_PATTERNS = [
    "~/.config/hub-core/chrome-headless-shell/**/chrome-headless-shell",
    "~/.config/hub-core/chrome-headless-shell/**/chrome-headless-shell.exe",
    "~/.cache/puppeteer/chrome-headless-shell/*/*/chrome-headless-shell",
    "~/.cache/puppeteer/chrome-headless-shell/*/*/chrome-headless-shell.exe",
    "~/Library/Caches/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell",   # newer Playwright versions
    "~/.cache/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell",
    "~/Library/Caches/ms-playwright/chromium_headless_shell-*/*/headless_shell",
    "~/.cache/ms-playwright/chromium_headless_shell-*/*/headless_shell",
]
# a dock icon from regular Chrome is only a macOS trouble: on Linux and Windows without a special build the tests take the system Chrome
# rather than being skipped silently (10.10: 110 page tests vanished in a container)
MAC = sys.platform == "darwin"
# Chrome for Testing is a separate app for automation (not the user's Google Chrome): the puppeteer cache
TESTING_PATTERNS = [
    "~/.cache/puppeteer/chrome/*/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
    "~/.cache/puppeteer/chrome/*/chrome-mac-x64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
    "~/.cache/puppeteer/chrome/*/chrome-linux64/chrome",
    "~/.cache/puppeteer/chrome/*/chrome-win64/chrome.exe",
]
# the Chrome of the tests (e2e of the page and of "Files", the README demo) gets no real sound devices: no output, and a
# fake microphone and camera. 2026-11-04: after a full run the owner's default output (a USB DAC) went silent until its
# cable was replugged; the voice tests open a real AudioContext and getUserMedia, and their Chrome is stopped by
# TerminateProcess. Not for the hub's own Chrome (mermaid.args: hub shot/video, the voice reply): it needs real devices
NO_AUDIO = ("--mute-audio", "--disable-audio-output", "--use-fake-device-for-media-stream")
# `npx @puppeteer/browsers install` puts the build under <path>/chrome-headless-shell/<platform-version>/…
SHELL_DIR = "chrome-headless-shell"
INSTALL = ("needs chrome-headless-shell or Chrome for Testing: `npx @puppeteer/browsers install "
           "chrome-headless-shell@stable`, or a path in HUB_TEST_CHROME / testchrome.path (tests do not start the ordinary Chrome)")


def install_command():
    """The command that puts chrome-headless-shell into the settings folder of THIS hub (a second hub on the machine has its own
    folder, HUB_CONFIG_DIR), where shell() looks first."""
    from core import catalog
    return f"npx @puppeteer/browsers install chrome-headless-shell@stable --path {shlex.quote(catalog.config_dir())}"


CLOSE_WAIT = 5   # seconds for the test's Chrome to exit by itself after Browser.close


def close(profile, timeout=2):
    """Ask the Chrome of this profile to close (CDP Browser.close at the endpoint it wrote into DevToolsActivePort), as
    a user closes it. True: the request went out; False: no endpoint or no connection."""
    from modules.proof import cdp
    try:
        with open(os.path.join(profile, "DevToolsActivePort"), encoding="utf-8") as f:
            port, path = f.read().split()[:2]
        ws = cdp.Socket(f"ws://127.0.0.1:{port}{path}", timeout)
    except (OSError, ValueError, cdp.Error):
        return False
    try:
        ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
        ws.recv()   # its answer, or the connection closed on the way out
    except (OSError, cdp.Error):
        pass
    finally:
        ws.close()
    return True


def stop(proc, wait=10, profile=None):
    """Stop the test's Chrome and wait for the root. With its profile: first asked to close (close()), its audio and GPU
    processes end their streams themselves (05.11: the owner's USB DAC went silent again on a day of page tests); killed
    only if still alive after CLOSE_WAIT. Killed: with all its processes (Windows has no killpg, so the tree; 09.10:
    tests left browsers as orphans, a full run gave 20 GB). Closed by itself: what it left still alive in its group is
    swept all the same (POSIX: the group outlives the collected root; Windows finds the tree only from a live root)."""
    import platforms
    if profile and proc.returncode is None and close(profile):
        try:
            proc.wait(CLOSE_WAIT)
        except subprocess.TimeoutExpired:
            pass
    try:
        platforms.current.kill_detached(proc)
    except (ProcessLookupError, PermissionError):
        pass
    proc.wait(wait)


def settings():
    """The machine's hub.json; if unreadable, {}."""
    try:
        from core import catalog
        from core.daemon import default_state_dir
        return catalog.load_config(default_state_dir())
    except Exception:  # noqa: BLE001 — without settings, the system Chrome rather than a failed snapshot or diagram
        return {}


def configured(cfg=None):
    """The path from HUB_TEST_CHROME or testchrome.path (settings not passed: from hub.json: snapshots, videos, mermaid diagrams and
    tests started from a session, not only the update, 08.10: the dock blinked from them); no such file means as if not set."""
    if cfg is None and not os.environ.get(ENV):
        cfg = settings()
    path = (os.environ.get(ENV) or ((cfg or {}).get("testchrome") or {}).get("path") or "").strip()
    path = os.path.expanduser(path)
    return path if path and os.path.exists(path) else None


def system():
    """System Chrome/Chromium; if none, None."""
    try:
        return browsers.find_chrome({})
    except browsers.Problem:
        return None


def _version(path):
    """Numbers from the path ("mac_arm-154.0.8037.57", "chromium_headless_shell-1187"), so that a newer one sorts later, not alphabetically."""
    return [int(n) for n in re.findall(r"\d+", path)]


def _newest(patterns):
    """The places are checked in list order, within a place the newest version (versions of different vendors cannot be
    compared with each other)."""
    for pattern in patterns:
        found = [p for p in glob.glob(os.path.expanduser(pattern), recursive=True)
                 if os.path.isfile(p) and os.access(p, os.X_OK)]
        if found:
            return os.path.normpath(max(found, key=_version))   # Windows: "~/.cache/..." gave a mix of "home/.cache\..."
    return None


def shell_patterns():
    """SHELL_PATTERNS with the settings folder of this hub (catalog.config_dir: HUB_CONFIG_DIR, XDG, %APPDATA%) first; the
    fixed `~/.config/hub-core` of earlier installs stays in the list."""
    try:
        from core import catalog
        base = glob.escape(os.path.join(catalog.config_dir(), SHELL_DIR))
    except Exception:  # noqa: BLE001 — no settings folder known: the fixed places
        return SHELL_PATTERNS
    return [f"{base}/**/chrome-headless-shell", f"{base}/**/chrome-headless-shell.exe", *SHELL_PATTERNS]


def shell():
    """An already installed chrome-headless-shell, with no window and no dock icon; if none, None."""
    return _newest(shell_patterns())


def testing_build():
    """Installed Chrome for Testing; if none, None."""
    return _newest(TESTING_PATTERNS)


# Ubuntu 23.10+: AppArmor does not give unprivileged namespaces to programs without their own profile, so the sandbox of
# Chrome from Playwright, puppeteer, Chrome for Testing too ("No usable sandbox"); the system Chrome has a profile
USERNS_SWITCH = "/proc/sys/kernel/apparmor_restrict_unprivileged_userns"


def sandbox_blocked():
    """Linux forbids the Chrome sandbox without an AppArmor profile."""
    try:
        with open(USERNS_SWITCH) as f:
            return f.read().strip() == "1"
    except OSError:
        return False


def downloaded():
    """The Chrome for Testing the hub itself downloaded (core/runtime: «Setup», or a package's); if none, None."""
    try:
        return runtime.chrome()
    except Exception:  # noqa: BLE001 — a missing or strange state folder is "none"
        return None


def find(cfg=None):
    """What to launch the page with in tests: the set path, otherwise chrome-headless-shell, otherwise Chrome for Testing, otherwise (not on
    macOS) the system Chrome, otherwise None. On macOS the user's system Google Chrome, never. Where AppArmor does not allow
    the sandbox to special builds (13.10: a session installed Playwright and tests, diagrams and snapshots went red), the system one comes first.
    The Chrome the hub downloaded itself goes before the system one, on every OS (macOS: it is its own build, not the owner's Chrome)."""
    first = system() if not MAC and sandbox_blocked() else None
    return (configured(cfg) or first or shell() or testing_build() or downloaded() or (None if MAC else system()))
