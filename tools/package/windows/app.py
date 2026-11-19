"""The Windows app shell (installer spec S6): what Setup.exe (Inno Setup, tools/package/windows/agents-hub.iss) and the
Start menu run. It lies in the app folder next to launch.py and, like it, is frozen at build time: an update of the
code cannot break it. Run with the app's own Python; it imports nothing of the hub.

  app.py install [--silent]   after Setup.exe copied the files: the links the build took out (links.json), the seed
                          becomes the code in the state folder (launch.py), then the code's deploy/windows/install.ps1
                          -App: settings, the `hub` command, the scheduler task, the hub started (log: logs/install.log).
                          A hub this user runs from a git clone first: "Move / Stay on the clone / Later" (app spec §8,
                          the release's `hub app`); Move — `hub app move` stops it and saves its files, then the install,
                          then `hub app move --done` or `hub app undo --failed`; --silent — no dialog: nothing changes
  app.py launch           every start of the hub (hub-up.cmd of the scheduler task, before hub-up): the links, the
                          seed into the code if it is not there yet, PACKAGED, a trial after a reboot (launch.py;
                          log: logs/launch.log)
  app.py stop             before Setup.exe replaces the files of an installed app: `hub stop`
  app.py open             the Start menu: the hub page; a hub that is off is turned on (`hub on`), a small window waits
                          for it; no code yet (the install was cut off) — `install` first
  app.py uninstall [--purge]   the uninstaller: `hub uninstall --yes` (the task, PATH, the commands; --purge — the data)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

APP = os.path.dirname(os.path.abspath(__file__))
TASK = "Agents Hub"
PORT = 8787
NO_WINDOW = 0x08000000   # CREATE_NO_WINDOW: the console programs this runs show no window (pythonw has none either)
# msys reads a file with the system attribute and this cookie as a symbolic link (Cygwin's own format, no rights needed)
MSYS_LINK = b"!<symlink>\xff\xfe"
FILE_ATTRIBUTE_SYSTEM = 0x4


def state_dir():
    """Where install.ps1 puts the hub ($env:LOCALAPPDATA\\hub-core) — and nowhere else: HUB_STATE_DIR is not the
    app's, the scheduler task and the commands would lead elsewhere."""
    return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "hub-core")


def program(name, app=APP):
    """A program of the app from the build's map of them (paths.json — the one setup.py reads too)."""
    try:
        with open(os.path.join(app, "paths.json"), encoding="utf-8") as f:
            rel = json.load(f)[name]
    except (OSError, ValueError, KeyError):
        rel = {"python": "runtime/python/python.exe", "git": "runtime/git/cmd/git.exe"}[name]
    return os.path.join(app, *rel.split("/"))


def python(app=APP):
    return program("python", app)


def layer_path(code):
    """The folder a process of Setup.exe's tree can enter. <state>\\code is a junction the user made, and Windows refuses to
    follow such a junction in any process of Setup's tree (cmd, PowerShell, Python alike: "contains an untrusted mount
    point", RedirectionGuard, the Windows stand 07.11): whatever this starts goes by the layer's own path; the pointer is
    only written into the files that outlive the install. A folder (a clone) is its own path. Reading the pointer does
    not follow it."""
    try:
        target = os.readlink(code)
    except OSError:
        return code
    if target.startswith("\\\\?\\"):   # a junction's target as Windows keeps it
        target = target[4:]
    return target if os.path.isabs(target) else os.path.normpath(os.path.join(os.path.dirname(code), target))


def git_env(app=APP, state=None):
    """The environment of what this runs: the app's own git first (launch.py's; a new PC has none), and the app's state
    folder — a HUB_STATE_DIR of the person's (a hub from git) would send `hub stop`, `hub uninstall --purge` there."""
    env = {**os.environ, "PATH": os.path.dirname(program("git", app)) + os.pathsep + os.environ.get("PATH", "")}
    env["HUB_STATE_DIR"] = state or state_dir()
    return env


def msys_link(path, target, system=None):
    """An msys symbolic link without rights: a file of msys's own format with the system attribute."""
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(MSYS_LINK + target.encode("utf-16-le") + b"\x00\x00")
    (system or _system_attribute)(tmp)   # before it takes the name: a file without the attribute is never left there
    os.replace(tmp, path)


def _system_attribute(path):
    if os.name == "nt":
        import ctypes
        if not ctypes.windll.kernel32.SetFileAttributesW(path, FILE_ATTRIBUTE_SYSTEM):
            raise OSError(f"{path}: the system attribute is not set (error {ctypes.GetLastError()})")


def restore_links(app=APP, system=None):
    """links.json of the build: "hard" — {name: the name it is a hard link of} (a copy where the disk has no hard
    links), "msys" — {name: link target, or null for a folder}. Only what is missing is made: a rerun is harmless."""
    try:
        with open(os.path.join(app, "links.json"), encoding="utf-8") as f:
            links = json.load(f)
    except FileNotFoundError:
        return 0
    made = 0
    for name, source in sorted((links.get("hard") or {}).items()):
        path = os.path.join(app, *name.split("/"))
        if os.path.lexists(path):
            continue
        try:
            os.link(os.path.join(app, *source.split("/")), path)
        except OSError:
            shutil.copy2(os.path.join(app, *source.split("/")), path)
        made += 1
    for name, target in sorted((links.get("msys") or {}).items()):
        path = os.path.join(app, *name.split("/"))
        if target is None:
            os.makedirs(path, exist_ok=True)
        elif not os.path.lexists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            msys_link(path, target, system)
            made += 1
    return made


def log_to(state, name):
    os.makedirs(os.path.join(state, "logs"), exist_ok=True)
    return open(os.path.join(state, "logs", name), "a", encoding="utf-8")


def say(log, text):
    log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
    log.flush()


def choose(texts):
    """The move dialog: -> "move" | "stay" | "later" (closed: later). Without tkinter — the system's box, Yes / No /
    Cancel named in its text."""
    try:
        import tkinter
        from tkinter import ttk
        root = tkinter.Tk()
    except Exception:  # noqa: BLE001 — no window toolkit: the system's message box
        return _choose_box(texts)
    got = {"v": "later"}
    root.title(texts.get("title") or TEXT["title"])
    root.resizable(False, False)
    icon = os.path.join(APP, "app.ico")
    if os.path.exists(icon):
        root.iconbitmap(icon)
    frame = ttk.Frame(root, padding=20)
    frame.grid()
    ttk.Label(frame, text=texts.get("text") or "", wraplength=440).grid(sticky="w")
    buttons = ttk.Frame(frame)
    buttons.grid(sticky="e", pady=(16, 0))

    def pick(v):
        got["v"] = v
        root.destroy()
    for col, key in enumerate(("later", "stay", "move")):
        ttk.Button(buttons, text=texts.get(key) or key, command=lambda k=key: pick(k)).grid(row=0, column=col, padx=(8, 0))
    root.attributes("-topmost", True)
    root.mainloop()
    return got["v"]


def _choose_box(texts):
    if os.name != "nt":
        return "later"
    import ctypes
    words = f"{texts.get('text') or ''}\n\nYes — {texts.get('move')}\nNo — {texts.get('stay')}\nCancel — {texts.get('later')}"
    got = ctypes.windll.user32.MessageBoxW(None, words, texts.get("title") or TEXT["title"], 0x3 | 0x20 | 0x40000)
    return {6: "move", 7: "stay"}.get(got, "later")


def script_hub(state):
    """The state folder holds a hub of deploy/windows/install.ps1 that is not this app's: its launcher, no code of the
    app (a clone of the seed, a code layer)."""
    launcher = os.path.join(state, "hub-up.cmd")
    if not os.path.isfile(launcher) or os.path.isdir(os.path.join(state, "code", ".git")) \
            or os.path.isdir(os.path.join(state, "layers")):
        return False
    with open(launcher, "rb") as f:
        return b'app.py" launch' not in f.read(16384)


def move_first(app, state, log, silent=False, ask=None, run=subprocess.run, wait=None):
    """A hub this user runs from a git clone (the seed's `hub app find`): the dialog, and on "Move" the release's
    `hub app move`. -> None — no such hub, go on as a new install; "moving" — the clone's hub is stopped and saved, go on
    and finish the move; an exit code — stop here (0: Later or Stay, nothing changed)."""
    import launch
    seed = os.path.join(app, "code-seed")
    found = launch.git_hub(seed, state, python(app), run=run)
    if not found:
        return None
    if silent:
        say(log, f"a hub from git ({found['repo']}): a silent install moves nothing")
        return 0
    choice = (ask or choose)(found["texts"])
    say(log, f"a hub from git ({found['repo']}): {choice}")
    if choice == "stay":
        launch.app_hub(seed, state, "stay", python=python(app), run=run)
    if choice != "move":
        return 0
    result = {}

    def go():
        result["r"] = launch.app_hub(seed, state, "move", found["repo"], "--app", app, python=python(app), run=run,
                                     out=log, timeout=launch.MOVE_TIMEOUT)
    import threading
    thread = threading.Thread(target=go, daemon=True)
    thread.start()
    shown = TEXT["wait"]
    TEXT["wait"] = found["texts"].get("waiting") or shown   # the small window says what it waits for
    try:
        (wait or window)(lambda: not thread.is_alive(), os.path.join(state, "logs", "install.log"), late=launch.MOVE_TIMEOUT)
    finally:
        TEXT["wait"] = shown
    thread.join()
    if result["r"].returncode:
        say(log, f"hub app move: exit code {result['r'].returncode}")
        if result["r"].returncode == 4:   # appmove.NotBack: failed, and the clone's service did not start again
            say(log, "the move failed, and the return to the clone did not finish: run hub app undo in a terminal")
            return 7
        return 5
    return "moving"


def finish_move(code, state, app, log, up, run=subprocess.run, late=None):
    """After the install of a move: the page answered — done; it did not — everything back on the clone."""
    import launch
    end = time.time() + (LATE if late is None else late)
    while time.time() < end and not up():
        time.sleep(1)
    if up():
        done = launch.app_hub(code, state, "move", "--done", python=python(app), run=run, out=log).returncode == 0
        say(log, "moved into the app" if done else "moved into the app, but the record is not marked done: hub app move --done")
        return 0
    return undo_failed(code, state, app, log, run)


def undo_failed(code, state, app, log, run=subprocess.run):
    """The move did not come up: everything back on the clone (`hub app undo --failed` of the code there is, else the
    seed's). -> 4, or 7 when the return did not finish (the log says so: `hub app undo` in a terminal)."""
    import launch
    back = code if code and os.path.isfile(os.path.join(code, "bin", "hub")) else os.path.join(app, "code-seed")
    if launch.app_hub(back, state, "undo", "--failed", python=python(app), run=run, out=log).returncode == 0:
        say(log, "the move did not come up: back on the clone")
        return 4
    say(log, "the move did not come up, and the return to the clone did not finish: run hub app undo in a terminal")
    return 7


def install(app=APP, state=None, run=subprocess.run, silent=False, ask=None, up=None, wait=None):
    """-> exit code. Each step into logs/install.log; a failure names the step there."""
    state = state or state_dir()
    with log_to(state, "install.log") as log:
        say(log, f"installing the app from {app}")
        step, moving, code = "links of the build", None, None
        try:   # whatever breaks is written down: Setup.exe and the Start menu send the person to this log
            say(log, f"links of the build: {restore_links(app)} restored")
            step = "the hub from git"
            sys.path.insert(0, app)
            import launch
            os.environ.update(git_env(app, state))   # launch.py's git; install.ps1's and its children's state
            moving = move_first(app, state, log, silent=silent, ask=ask, run=run, wait=wait)
            if moving is not None and moving != "moving":
                return moving
            if moving is None and script_hub(state):   # Setup.exe let it through for the move: not a clone after all
                say(log, "a hub of the install script that does not run from a git clone of the core: nothing changed")
                return 6
            step = "the hub's code"
            code = launch.ensure_code(os.path.join(app, "code-seed"), os.path.join(app, "PACKAGED"), state)
            say(log, f"the hub's code: {code}")
            # Setup.exe let no process of the app live (PrepareToInstall): an instance of the task still "Running" is an
            # orphaned cmd.exe, and its IgnoreNew would swallow the start install.ps1 asks for (app spec, probe A0)
            step = "the task's orphan"
            run(["schtasks", "/End", "/TN", TASK], stdin=subprocess.DEVNULL, capture_output=True,
                creationflags=NO_WINDOW if os.name == "nt" else 0, timeout=60)
            step = "install.ps1"
            here = layer_path(code)   # Setup's tree cannot enter the pointer; install.ps1 writes the pointer (-Code) down
            ps1 = os.path.join(here, "deploy", "windows", "install.ps1")
            r = run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", ps1,
                     "-App", app, *(["-Code", code] if here != code else []), "-Yes"], cwd=here, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    env={**os.environ, "HUB_INSTALL_OUTPUT": "utf-8"},   # its words into this UTF-8 log as they are
                    creationflags=NO_WINDOW if os.name == "nt" else 0, timeout=900)
        except Exception as e:  # noqa: BLE001 — see above
            say(log, f"{step}: failed: {e!r}")
            if moving == "moving":   # the clone's hub is stopped: back on it
                return undo_failed(code, state, app, log, run)
            return 3
        say(log, f"install.ps1: exit code {r.returncode}")
        if moving == "moving":
            return finish_move(code, state, app, log, up or page_up, run=run)
        return 0 if r.returncode == 0 else 4


def launch(app=APP, state=None):
    """-> exit code. hub-up starts after it whatever it says: the code is usually there already."""
    state = state or state_dir()
    with log_to(state, "launch.log") as log:
        try:
            made = restore_links(app)
            if made:
                say(log, f"links of the build: {made} restored")
            sys.path.insert(0, app)
            import launch as shell
            os.environ.update(git_env(app, state))
            shell.ensure_code(os.path.join(app, "code-seed"), os.path.join(app, "PACKAGED"), state)
            if shell.resume_trial(state, python(app)):
                say(log, "the update watchdog started again (a reboot in the middle of a check)")
        except Exception as e:  # noqa: BLE001 — written down; the hub starts anyway
            say(log, f"start: {e!r}")
            return 1
    return 0


def hub(state, *args, run=subprocess.run, timeout=300, app=APP):
    """`hub <args>` of the installed code with the app's Python; None — there is no code (not installed yet)."""
    script = os.path.join(layer_path(os.path.join(state, "code")), "bin", "hub")
    if not os.path.isfile(script):
        return None
    try:   # the uninstaller may delete the state folder: the command does not run inside it
        return run([python(app), script, *args], cwd=tempfile.gettempdir(), stdin=subprocess.DEVNULL,
                   capture_output=True, text=True, encoding="utf-8", errors="replace", env=git_env(app, state),
                   creationflags=NO_WINDOW if os.name == "nt" else 0, timeout=timeout).returncode
    except (OSError, subprocess.SubprocessError):   # under pythonw nobody would see it: a failed command
        return 1


def page_up(port=PORT):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # the machine's proxy is not for 127.0.0.1
    try:
        with opener.open(f"http://127.0.0.1:{port}/", timeout=1):
            return True
    except urllib.error.HTTPError:
        return True   # it answered: the page decides what to show
    except OSError:
        return False


# the window comes before the hub knows its owner's language: English (the page then speaks the owner's language)
TEXT = {"title": "Hub by Icons8", "wait": "Starting the hub…", "late": "The hub does not answer yet. Wait a little longer "
        "or send the log file:", "failed": "The hub was not set up. What happened is in the file:", "log": "Open the log",
        "close": "Close"}
LATE = 120   # seconds before the window says the hub does not answer


def window(up, log, failed=False, late=LATE):
    """A small window while the hub comes up (the browser cannot wait for it: Chromium does not let a page from a file
    ask 127.0.0.1): "Starting the hub…" until up() — True; after `late` seconds, or at once when failed, the log file
    to send — False when closed. No window possible (tkinter missing) — waits without one."""
    try:
        import tkinter
        from tkinter import ttk
        root = tkinter.Tk()
    except Exception:  # noqa: BLE001 — no window: wait quietly
        end = time.time() + late
        while time.time() < end and not failed:
            if up():
                return True
            time.sleep(1)
        return False
    root.title(TEXT["title"])
    root.resizable(False, False)
    icon = os.path.join(APP, "app.ico")
    if os.path.exists(icon):
        root.iconbitmap(icon)
    frame = ttk.Frame(root, padding=20)
    frame.grid()
    label = ttk.Label(frame, text=TEXT["wait"], wraplength=380)
    label.grid(sticky="w")
    bar = ttk.Progressbar(frame, mode="indeterminate", length=380)
    bar.grid(pady=(12, 0))
    bar.start(15)
    began, result = time.time(), {"up": False}

    def tell():
        bar.grid_remove()
        label.configure(text=TEXT["failed" if failed else "late"])
        ttk.Label(frame, text=log, wraplength=380).grid(sticky="w", pady=(8, 0))
        buttons = ttk.Frame(frame)
        buttons.grid(sticky="e", pady=(12, 0))
        ttk.Button(buttons, text=TEXT["log"], command=lambda: os.startfile(log) if os.path.exists(log) else None).grid(
            row=0, column=0, padx=(0, 8))
        ttk.Button(buttons, text=TEXT["close"], command=root.destroy).grid(row=0, column=1)

    def poll():
        if up():
            result["up"] = True
            root.destroy()
            return
        if time.time() - began > late and bar.winfo_ismapped():
            tell()
        root.after(1000, poll)

    if failed:
        tell()
    else:
        root.after(200, poll)
    root.attributes("-topmost", True)
    root.mainloop()
    return result["up"]


def page_url(port=PORT):
    """The page to open: with the sign-in by a key on (core/pagekey.py — %APPDATA%\\hub-core\\page-key) the link with
    the key, so this browser signs in; otherwise the page itself. A hub with the page's password (core/pagepass.py —
    page-password next to it) asks for it on the page: its key is the one a password keeps for a rollback, never opened."""
    folder = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "hub-core")
    if os.path.exists(os.path.join(folder, "page-password")):
        return f"http://127.0.0.1:{port}/"
    try:
        with open(os.path.join(folder, "page-key"), "rb") as f:
            key = f.read(200).decode("ascii").strip()
    except (OSError, ValueError):
        key = ""
    ok = len(key) == 64 and all(c in "0123456789abcdef" for c in key)
    return f"http://127.0.0.1:{port}/" + (f"?key={key}" if ok else "")


def open_hub(app=APP, state=None, up=page_up, browser=None, run=subprocess.run, wait=window):
    """The Start menu: the hub page, once the hub answers (turned on if it was off)."""
    state = state or state_dir()
    browser = browser or os.startfile
    url = page_url()
    if up():
        browser(url)
        return 0
    if hub(state, "on", run=run, app=app) is None:   # the install was cut off before the code: do it now
        code = install(app, state, run=run)
        if code:
            wait(up, os.path.join(state, "logs", "install.log"), failed=True)
            return code
    if wait(up, os.path.join(state, "logs", "hub-up.log")):
        browser(url)
        return 0
    return 1


def uninstall(purge=False, app=APP, state=None, run=subprocess.run):
    state = state or state_dir()
    code = hub(state, "uninstall", "--yes", *(["--purge"] if purge else []), run=run, app=app)
    if code is None:   # no code: at least the task goes (install.ps1 may have registered it)
        run(["schtasks", "/Delete", "/TN", TASK, "/F"], capture_output=True,
            creationflags=NO_WINDOW if os.name == "nt" else 0, timeout=60)
        return 0
    return code


def main(argv):
    cmd = argv[0] if argv else ""
    if cmd == "install" and argv[1:] in ([], ["--silent"]):
        return install(silent=argv[1:] == ["--silent"])
    if cmd == "launch":
        return launch()
    if cmd == "stop":
        hub(state_dir(), "stop")
        return 0
    if cmd == "open":
        return open_hub()
    if cmd == "uninstall" and argv[1:] in ([], ["--purge"]):
        return uninstall(purge=argv[1:] == ["--purge"])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
