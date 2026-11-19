"""The downloaded "Agents Hub.app" (installer spec S5): what its executable (deploy/mac/launcher.c) runs.

The app is built by tools/package/macapp.py: Contents/Resources/hub is the platform folder of tools/package/build.py
(runtime/, code-seed/, PACKAGED, launch.py, paths.json) plus a frozen copy of this file — an update of the code cannot
break it. The launcher starts it with the app's own Python, `-I -B` (nothing is written into the signed app):

  open   Finder opened the app (no arguments): a person wants the hub. (The launcher has already refused a copy on the
         disk image or in App Translocation and taken the download's quarantine off the app's files — before any of
         them runs.) A hub this Mac runs from a git clone: the dialog "Move / Stay on the clone / Later" (app spec
         §8, the release's own `hub app`: its words in the owner's language) — Move: the release's `hub app move`
         stops the clone's hub and saves what the install rewrites, then the install below, then `hub app move
         --done` once the page answers, or `hub app undo --failed`. Another hub installed by a script is left alone
         (spec §13 scenario 6). Otherwise: the code is
         cloned from the seed (launch.py), deploy/mac/install.sh of the code registers the launchd agent in app mode
         (HUB_APP), and the hub's page opens in the default browser. Opening it again while the hub runs only opens
         the page.
  serve  launchd starts the hub (the agent's ProgramArguments are [<app>/Contents/MacOS/Agents Hub, --serve]): the code
         from the seed if it is not there yet, a trial left by an update resumed (launch.py), the app's programs first
         in PATH, then hub-up of the code in place of this process.

<state>/app is a link to Contents/Resources/hub, renewed at every start: hub.json and the hub's python3 point through
it, so moving the app and opening it again repoints everything at once. Dialogs are AppleScript (osascript): the
launcher is not a GUI program. The stdlib only."""
import fcntl
import json
import os
import plistlib
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from xml.parsers.expat import ExpatError

HERE = os.path.dirname(os.path.abspath(__file__))                 # <app>/Contents/Resources/hub
APP = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))     # <app>
sys.path.insert(0, HERE)   # -I leaves the script's folder out of sys.path
import launch  # noqa: E402

LABEL = "com.hub-core"
EXE = os.path.join("Contents", "MacOS", "Agents Hub")
BINS = ("bin", "runtime/node/bin", "runtime/gh/bin", "runtime/ffmpeg/bin")   # first in PATH (bin: the git wrapper)
# any sign of a hub's data: wider than core/uninstall.marked on purpose — here a false «hub» only keeps the app away
HUB_MARKS = ("journal.jsonl", "state.json", os.path.join("hub-home", "CLAUDE.md"))
PAGE_WAIT = 120   # seconds the first start may take before the page answers (a cold Mac, the first clone)

# the dialogs come before the hub knows its owner's language: English (the page then speaks the owner's language)
TEXT = {
    "foreign": "A hub is already installed on this Mac (from git or by a script). The app is for a new install and "
               "leaves the installed hub alone. Its page: {url}",
    "leftover": "Settings and data of a hub installed from git or by a script are left on this Mac ({cfg}, {state}). "
                "The app leaves them alone. To install it afresh, rename or delete these folders and open Agents Hub "
                "again.",
    "leftover_data": "Data of a hub installed from git or by a script is left in {state}. The app leaves it alone. To "
                     "install it afresh, rename or delete this folder and open Agents Hub again.",
    "busy": "Port {port} is taken by another program, and the hub needs it. Quit it and open Agents Hub again.",
    "failed": "The hub did not start. What happened is in the log {log}",
    "slow": "The hub is taking longer than usual to start. Its page is {url} — try again in a minute. Log: {log}",
    "show_log": "Show the log",
    "ok": "OK",
}


def t(key, **kw):
    return TEXT[key].format(**kw)


def dialog(text, buttons=("ok",)):
    """A dialog with the text and the buttons (keys of TEXT); -> the key of the button pressed, or None."""
    names = [t(b) for b in buttons]
    script = ["on run argv", "tell me to activate",
              "set r to display dialog (item 1 of argv) with title \"Agents Hub\" buttons (rest of argv) "
              "default button (count of (rest of argv)) with icon caution", "return button returned of r", "end run"]
    cmd = ["osascript"] + [x for line in script for x in ("-e", line)] + [text] + names
    try:
        got = subprocess.run(cmd, capture_output=True, text=True, timeout=3600).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return next((b for b, n in zip(buttons, names) if n == got), None)


def home():
    return os.path.expanduser("~")


def state_dir():
    return os.path.join(home(), ".hub-core")


def plist_path():
    return os.path.join(home(), "Library", "LaunchAgents", LABEL + ".plist")


def page_port():
    try:
        return int(os.environ.get("HUB_WEB_PORT") or 8787)
    except ValueError:
        return 8787


def log(state, line):
    os.makedirs(os.path.join(state, "logs"), exist_ok=True)
    with open(os.path.join(state, "logs", "setup.log"), "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} app: {line}\n")


def agent():
    """ProgramArguments of the registered launchd agent of the hub, [] when there is none, None when unreadable."""
    try:
        with open(plist_path(), "rb") as f:
            args = plistlib.load(f).get("ProgramArguments")
    except FileNotFoundError:
        return []
    except (OSError, ValueError, plistlib.InvalidFileException, ExpatError, AttributeError):
        return None
    return args if isinstance(args, list) else None


def ours(args):
    """The agent runs an app's shell (this app or a copy of it elsewhere), not a hub from git."""
    return isinstance(args, list) and len(args) == 2 and args[1] == "--serve" and isinstance(args[0], str) \
        and args[0].endswith("/" + EXE)


def answers(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


def config_dir():
    return os.path.join(home(), ".config", "hub-core")   # install.sh's, whatever the environment says


def page_url(port):
    """The page to open: with the sign-in by a key on (core/pagekey.py — <config>/page-key, read the same way: this file
    is a frozen copy and imports nothing of the hub) the link with the key, so this browser signs in; otherwise the page
    itself. macOS does not show another account the arguments of this account's processes (`open`, the browser). A hub
    with the page's password (core/pagepass.py — <config>/page-password) asks for it on the page: its key is the one
    a password keeps for a rollback, never opened."""
    if os.path.exists(os.path.join(config_dir(), "page-password")):
        return f"http://127.0.0.1:{port}/"
    try:
        with open(os.path.join(config_dir(), "page-key"), "rb") as f:
            key = f.read(200).decode("ascii").strip()
    except (OSError, ValueError):
        key = ""
    ok = len(key) == 64 and all(c in "0123456789abcdef" for c in key)
    return f"http://127.0.0.1:{port}/" + (f"?key={key}" if ok else "")


def apps_settings(path, state):
    """hub.json the app wrote: app.py config names the browser's MCP and ffmpeg through <state>/app."""
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        named = list((cfg.get("browser") or {}).get("mcp") or []) + [(cfg.get("voice") or {}).get("ffmpeg")]
    except (OSError, ValueError, AttributeError, TypeError):
        return False
    app = os.path.join(state, "app") + "/"
    return any(isinstance(x, str) and x.startswith(app) for x in named)


def foreign(state, port):
    """Why this Mac already has a hub the app must not take over (None — it has none, or it is the app's own)."""
    args = agent()
    if args and not ours(args) or args is None:
        return "foreign"
    own = os.path.isdir(os.path.join(state, "code", ".git")) or os.path.exists(os.path.join(state, "app.json")) \
        or os.path.islink(os.path.join(state, "app"))   # what only the app leaves in the state folder
    path = os.path.join(config_dir(), "hub.json")
    if os.path.exists(path) and not own and not apps_settings(path, state):
        return "leftover"   # a hub installed another way, its service gone: its settings name paths the app has not
    if not own and any(os.path.lexists(os.path.join(state, m)) for m in HUB_MARKS):
        return "leftover_data"   # its data without its settings: the core would run over someone else's journal
    if not args and answers(port):
        return "busy"
    return None


def choose(texts):
    """The move dialog: -> "move" | "stay" | "later" (closed, or no answer: later — nothing changes)."""
    names = [texts.get(k) or k for k in ("later", "stay", "move")]
    script = ["on run argv", "tell me to activate",
              "set r to display dialog (item 1 of argv) with title (item 2 of argv) buttons (items 3 thru 5 of argv) "
              "default button 3 cancel button 1 with icon note", "return button returned of r", "end run"]
    cmd = ["osascript"] + [x for line in script for x in ("-e", line)] + [texts.get("text") or "",
                                                                          texts.get("title") or "Agents Hub"] + names
    try:
        got = subprocess.run(cmd, capture_output=True, text=True, timeout=3600).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "later"
    return {names[1]: "stay", names[2]: "move"}.get(got, "later")


def notify(text):
    subprocess.run(["osascript", "-e", "on run argv", "-e", 'display notification (item 1 of argv) with title "Agents Hub"',
                    "-e", "end run", text], capture_output=True, timeout=30)


def move(state, found, port):
    """"Move": the clone's hub stopped and its files saved (the release's `hub app move`), the app installed as for a
    new hub, then done — or everything back on the clone. -> exit code."""
    seed, python = os.path.join(HERE, "code-seed"), os.path.join(HERE, "runtime", "python", "bin", "python3")
    texts = found["texts"]
    path = os.path.join(state, "logs", "setup.log")
    log(state, f"moving the hub from {found['repo']}")
    notify(texts.get("waiting") or "")
    with open(path, "a", encoding="utf-8") as out:
        r = launch.app_hub(seed, state, "move", found["repo"], "--app", HERE, python=python, out=out,
                           timeout=launch.MOVE_TIMEOUT)
    if r.returncode:
        log(state, f"not moved: hub app move gave {r.returncode}")
        words = f"{texts.get('undo_failed') or ''} {path}" if r.returncode == 4 else t("failed", log=path)   # NotBack
        if dialog(words, ("show_log", "ok")) == "show_log":
            reveal(path)
        return 6
    try:
        code = launch.ensure_code(seed, os.path.join(HERE, "PACKAGED"), state)
        link(state)
        ok = install(code, state) and wait_page(port)
    except (OSError, RuntimeError, subprocess.SubprocessError) as e:
        log(state, f"failed: {e}")
        code, ok = None, False
    with open(path, "a", encoding="utf-8") as out:
        if ok:
            if launch.app_hub(code, state, "move", "--done", python=python, out=out).returncode:
                log(state, "moved, but the record is not marked done: hub app move --done")   # the hub runs on the app
            else:
                log(state, "moved")
            return 0
        back = code if code and os.path.isfile(os.path.join(code, "bin", "hub")) else seed
        undone = launch.app_hub(back, state, "undo", "--failed", python=python, out=out).returncode == 0
    log(state, "the move did not come up: " + ("back on the clone" if undone else "the return to the clone did not finish"))
    show = os.path.join(state, "logs", "hub-up.log" if undone else "setup.log")
    words = texts.get("failed" if undone else "undo_failed") or t("failed", log="")
    choice = dialog(f"{words} {show}", ("show_log", "ok"))
    if choice == "show_log":
        reveal(show)
    return 4


def link(state):
    """<state>/app → this app's Contents/Resources/hub."""
    path = os.path.join(state, "app")
    if os.path.islink(path) and os.readlink(path) == HERE:
        return path
    tmp = path + ".part"
    if os.path.lexists(tmp):
        os.unlink(tmp)
    os.symlink(HERE, tmp)
    os.replace(tmp, path)
    return path


def stamp():
    """What makes an installed agent this app's: the app's path and its PACKAGED (version, environment)."""
    with open(os.path.join(HERE, "PACKAGED")) as f:
        return {"app": APP, "packaged": json.load(f)}


def running():
    r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], capture_output=True, timeout=30)
    return r.returncode == 0


def current(state):
    """The agent is this app's, of this version, and launchd has it: nothing to install."""
    try:
        with open(os.path.join(state, "app.json")) as f:
            done = json.load(f)
    except (OSError, ValueError):
        return False
    args = agent()
    return done == stamp() and ours(args) and os.path.realpath(args[0]) == os.path.realpath(os.path.join(APP, EXE)) \
        and running()


def install(code, state):
    """deploy/mac/install.sh of the code in app mode; True when it registered the agent."""
    os.makedirs(os.path.join(state, "logs"), exist_ok=True)
    with open(os.path.join(state, "logs", "setup.log"), "a", encoding="utf-8") as out:
        r = subprocess.run(["/bin/sh", os.path.join(code, "deploy", "mac", "install.sh")], cwd=code,
                           env={**os.environ, "HUB_APP": APP}, stdin=subprocess.DEVNULL, stdout=out,
                           stderr=subprocess.STDOUT, timeout=600)
    if r.returncode:
        return False
    with open(os.path.join(state, "app.json.part"), "w") as f:
        json.dump(stamp(), f)
    os.replace(os.path.join(state, "app.json.part"), os.path.join(state, "app.json"))
    return True


def wait_page(port, seconds=PAGE_WAIT, clock=time.monotonic, sleep=time.sleep):
    """The hub's page answers (any HTTP answer from it). Straight to 127.0.0.1: the Mac's proxy settings would send
    the request to a proxy, whose own error looks like an answer."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    end = clock() + seconds
    while True:
        try:
            with opener.open(f"http://127.0.0.1:{port}/api/hello", timeout=5):
                return True
        except urllib.error.HTTPError:
            return True    # the page's server answers
        except OSError:
            pass
        if clock() >= end:
            return False
        sleep(1)


def reveal(path):
    subprocess.run(["open", "-R", path], capture_output=True, timeout=30)


def open_app():
    os.environ.update(serve_env(os.environ))   # Finder gives /usr/bin first: its git is a stub asking for Xcode tools
    port = page_port()
    url = f"http://127.0.0.1:{port}/"
    state = state_dir()
    os.makedirs(state, exist_ok=True)
    with open(os.path.join(state, "app.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)   # a second double click waits for the first, then only opens the page
        why = foreign(state, port)
        found = launch.git_hub(os.path.join(HERE, "code-seed"), state,
                               os.path.join(HERE, "runtime", "python", "bin", "python3")) if why else None
        if found:   # a hub from a git clone: the owner chooses (app spec §8)
            choice = choose(found["texts"])
            log(state, f"a hub from git ({found['repo']}): {choice}")
            if choice == "stay":
                launch.app_hub(os.path.join(HERE, "code-seed"), state, "stay",
                               python=os.path.join(HERE, "runtime", "python", "bin", "python3"))
            if choice != "move":
                return 3
            code = move(state, found, port)
            if code == 0:
                subprocess.run(["open", page_url(port)], capture_output=True, timeout=30)
            return code
        if why:
            log(state, f"not installing: {why}")
            dialog(t(why, url=url, port=port, cfg=config_dir(), state=state))
            return 3
        if not current(state):
            log(state, f"installing {APP}")
            try:
                code = launch.ensure_code(os.path.join(HERE, "code-seed"), os.path.join(HERE, "PACKAGED"), state)
                link(state)
                ok = install(code, state)
            except (OSError, RuntimeError, subprocess.SubprocessError) as e:
                log(state, f"failed: {e}")
                ok = False
            if not ok:
                path = os.path.join(state, "logs", "setup.log")
                if dialog(t("failed", log=path), ("show_log", "ok")) == "show_log":
                    reveal(path)
                return 4
    if not wait_page(port):
        path = os.path.join(state, "logs", "hub-up.log")
        if dialog(t("slow", url=url, log=path), ("show_log", "ok")) == "show_log":
            reveal(path)
        return 5
    subprocess.run(["open", page_url(port)], capture_output=True, timeout=30)
    return 0


def serve_env(env):
    """PATH with the app's programs first (once, however often it is applied)."""
    path = [os.path.join(HERE, b) for b in BINS if os.path.isdir(os.path.join(HERE, b))]
    rest = [p for p in (env.get("PATH") or "/usr/bin:/bin:/usr/sbin:/sbin").split(os.pathsep) if p and p not in path]
    return {"PATH": os.pathsep.join(path + rest)}


def serve(execve=os.execve):
    os.environ.update(serve_env(os.environ))   # launch.py's git is the app's
    state = os.environ.get("HUB_STATE_DIR") or state_dir()
    try:
        code = launch.ensure_code(os.path.join(HERE, "code-seed"), os.path.join(HERE, "PACKAGED"), state)
        link(state)
    except (OSError, RuntimeError, subprocess.SubprocessError) as e:
        print(f"Agents Hub: the hub's code is not ready: {e}", file=sys.stderr, flush=True)
        return 1   # launchd starts the agent again (KeepAlive)
    python = os.path.join(HERE, "runtime", "python", "bin", "python3")
    launch.resume_trial(state, python)
    os.chdir(code)
    execve(python, [python, os.path.join(code, "bin", "hub-up")], dict(os.environ))
    return 0


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "open"
    if action == "serve":
        sys.exit(serve())
    sys.exit(open_app())
