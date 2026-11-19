"""What the Linux package does at every start before the hub (installer spec S7): the shell's launcher (launch.py: the
code becomes a clone of the seed in the user's state folder, a trial left by a reboot goes on), then the state folders,
the hub session rules and settings — once, later it touches nothing. Prints the code folder (the agents-hub command
runs hub-up from it).

The same as deploy/mac/install.sh, but without the system Python: everything needed is in the package folder (python,
node, gh, ffmpeg, chrome-devtools-mcp), the paths to them are taken from it."""
import hashlib
import importlib.util
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # /opt/agents-hub (this file is ROOT/lib/first_run.py)


PACKAGED_CHROME = "11d28074c677b44d67e4e16cc80a452bc9d0ba8b63331ea10dbfa5c019b770b7"   # sha256 of the description an older package wrote for the local browser (its texts were not English)


def is_packaged_chrome(description):
    """Exactly the text that older package wrote: told by the hash, so that a description the owner wrote stays."""
    return isinstance(description, str) and hashlib.sha256(description.encode("utf-8")).hexdigest() == PACKAGED_CHROME


def timezone():
    if os.environ.get("TZ"):
        return os.environ["TZ"].lstrip(":")
    try:
        with open("/etc/timezone") as f:
            return f.read().strip() or "UTC"
    except OSError:
        pass
    link = os.path.realpath("/etc/localtime")
    return link.split("/zoneinfo/", 1)[1] if "/zoneinfo/" in link else "UTC"


def launcher(root):
    """launch.py of the shell, the one the factory put next to PACKAGED."""
    spec = importlib.util.spec_from_file_location("hub_shell_launch", os.path.join(root, "launch.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def config(state, root=ROOT, mcp=None, ffmpeg=None):
    """hub.json of a new hub from a package: the .deb's programs by default; the Mac app names its own (deploy/mac/app.py)."""
    cfg = {
        "hub": {"enabled": True, "name": "hub-core", "cwd": os.path.join(state, "hub-home")},
        "sets": [],
        "base": ["core:hub-base"],
        "hub_tools": ["core:hub-coordinator"],
        "mcp": {}, "connectors": False, "timezone": timezone(), "setting_sources": "project,local",
        "projects": {"sandbox": {"path": os.path.join(state, "sandbox"), "about": "a sandbox: try the hub and sessions"}},
        "browser": {"mcp": mcp or [os.path.join(root, "bin", "chrome-devtools-mcp")]},
        "voice": {"ffmpeg": ffmpeg or os.path.join(root, "bin", "ffmpeg")},
    }
    return cfg


def forget_packaged_chrome(path, code, root=ROOT):
    """An older package carried Chrome for Testing in ROOT/chrome and named it in hub.json; the package no longer has it
    (the hub downloads Chrome at the first start, modules/setup): the stale path goes, and the local browser's text
    no longer says the Chrome is the package's. The rest of hub.json stays; written as the hub writes it (the lock,
    the file's permissions — it may hold tokens)."""
    old = os.path.join(root, "chrome", "chrome")
    try:   # a one-time step at every start: nothing here may keep the hub from starting
        with open(path, encoding="utf-8") as f:
            if os.path.exists(old) or old not in f.read():
                return False
        sys.path.insert(0, code)   # the core of the code folder: the lock and the permissions are its
        from core import catalog
        with catalog.edit_settings(path) as cfg:
            browser = cfg.get("browser") if isinstance(cfg, dict) else None
            if not (isinstance(browser, dict) and browser.get("chrome") == old):
                raise LookupError
            del browser["chrome"]
            local = (cfg.get("browsers") or {}).get("local") if isinstance(cfg.get("browsers"), dict) else None
            if isinstance(local, dict) and is_packaged_chrome(local.get("description")):
                local["description"] = "The hub's Chrome for Testing: its own profile, without your sign-ins"
    except Exception:  # noqa: BLE001 — see above
        return False
    return True


def named(path):
    """The mark names a folder (an empty file is no mark: the readers skip it)."""
    try:
        with open(path, encoding="utf-8") as f:
            return bool(f.read().strip())
    except OSError:
        return False


def setup(home=None, root=ROOT):
    """-> (state folder, hub.json, code folder)."""
    home = home or os.path.expanduser("~")
    state = os.environ.get("HUB_STATE_DIR") or os.path.join(home, ".hub-core")
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")   # where the hub looks (platforms/posix)
    cfg_dir = os.environ.get("HUB_CONFIG_DIR") or os.path.join(xdg, "hub-core")
    launch = launcher(root)
    code = launch.ensure_code(os.path.join(root, "code-seed"), os.path.join(root, "PACKAGED"), state)
    launch.resume_trial(state, sys.executable)
    for d in ("hub-home", "sandbox", "logs"):
        os.makedirs(os.path.join(state, d), exist_ok=True)
    os.makedirs(cfg_dir, exist_ok=True)
    for name in ("CLAUDE.md", "ONBOARDING.md"):   # the hub session rules come from the code: a new release, new rules
        link = os.path.join(state, "hub-home", name)
        want = os.path.join(code, "hub-home", name)
        if os.path.islink(link) and os.readlink(link) == want:
            continue
        if os.path.islink(link):    # an older package linked them into its own folder
            os.unlink(link)
        if not os.path.lexists(link):   # a file the person wrote in place of the link stays as it is
            os.symlink(want, link)
    marker = os.path.join(xdg, "hub-core", "state-dir")
    if not any(named(p) for p in (marker, os.path.join(home, ".config", "hub-core", "state-dir"))):
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w") as f:
            f.write(state + "\n")
    path = os.path.join(cfg_dir, "hub.json")
    if not os.path.exists(path):
        with open(path, "w") as f:
            json.dump(config(state, root), f, ensure_ascii=False, indent=2)
    else:
        forget_packaged_chrome(path, code, root)
    return state, path, code


def choose(texts, run=None, tty=None):
    """The move dialog on Linux: zenity, else kdialog, else the terminal; -> "move" | "stay" | "later", or None — no
    way to ask (a menu entry on a desktop without either: the notification says to run agents-hub in a terminal)."""
    import shutil
    import subprocess
    run = run or subprocess.run
    title, text = texts.get("title") or "Hub by Icons8", texts.get("text") or ""
    move, stay, later = (texts.get(k) or k for k in ("move", "stay", "later"))
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        if shutil.which("zenity"):
            r = run(["zenity", "--question", "--title", title, "--text", text, "--ok-label", move, "--cancel-label",
                     later, "--extra-button", stay, "--no-wrap"], capture_output=True, text=True)
            if r.stdout.strip() == stay:
                return "stay"
            return "move" if r.returncode == 0 else "later"
        if shutil.which("kdialog"):
            r = run(["kdialog", "--title", title, "--yesnocancel", text, "--yes-label", move, "--no-label", stay,
                     "--cancel-label", later], capture_output=True, text=True)
            return {0: "move", 1: "stay"}.get(r.returncode, "later")
    tty = tty if tty is not None else sys.stdin.isatty()
    if not tty:
        return None
    print(f"{text}\n  1 — {move}\n  2 — {stay}\n  3 — {later}", file=sys.stderr)
    try:
        got = input("1/2/3: ").strip()
    except EOFError:
        return "later"
    return {"1": "move", "2": "stay"}.get(got, "later")


MOVING = 10   # --move: the clone's hub is stopped and saved — the start goes on and finishes the move
NO_MOVE = 3   # --moved, --move-failed: no move is half done (the record is not "moving")


def move_step(argv, root=ROOT, home=None, ask=choose, run=None):
    """The .deb's part of the move from a git clone (app spec §8): --move before the first start (0 — nothing to move,
    MOVING — the clone's hub stopped and saved, 3 — the owner chose Later or Stay, or could not be asked, 1 — the move
    refused, nothing changed, 2 — it failed and the clone's service did not start again: `hub app undo`),
    --moved after the page answered, --move-failed when it did not or the start failed (0 — back on the clone, 2 — the
    return did not finish: `hub app undo` in a terminal; 0 too when there was no move)."""
    launch = launcher(root)
    home = home or os.path.expanduser("~")
    state = os.environ.get("HUB_STATE_DIR") or os.path.join(home, ".hub-core")
    seed = os.path.join(root, "code-seed")
    if argv[0] in ("--moved", "--move-failed"):
        try:
            with open(os.path.join(state, "app-move", "record.json"), encoding="utf-8") as f:
                moving = json.load(f).get("status") == "moving"
        except (OSError, ValueError, AttributeError):
            moving = False
        if not moving:
            return NO_MOVE
        code = os.path.join(state, "code")
        code = code if os.path.isfile(os.path.join(code, "bin", "hub")) else seed
        args = ("move", "--done") if argv[0] == "--moved" else ("undo", "--failed")
        r = launch.app_hub(code, state, *args, python=sys.executable, **({"run": run} if run else {}))
        print((r.stdout or "").strip(), file=sys.stderr)
        return 0 if r.returncode == 0 or argv[0] == "--moved" else 2
    kw = {"run": run} if run else {}
    found = launch.git_hub(seed, state, sys.executable, **kw)
    if not found:
        return 0
    choice = ask(found["texts"])
    if choice is None:
        print(f"Hub by Icons8: {found['texts'].get('text')} — run agents-hub in a terminal to answer", file=sys.stderr)
        return 3
    if choice == "stay":
        launch.app_hub(seed, state, "stay", python=sys.executable, **kw)
    if choice != "move":
        return 3
    print(found["texts"].get("waiting") or "", file=sys.stderr)
    r = launch.app_hub(seed, state, "move", found["repo"], "--app", root, python=sys.executable, out=sys.stderr,
                       timeout=launch.MOVE_TIMEOUT, **kw)
    return MOVING if r.returncode == 0 else 2 if r.returncode == 4 else 1   # 4: appmove.NotBack


def main(argv):
    if argv[:1] in (["--move"], ["--moved"], ["--move-failed"]):
        return move_step(argv)
    try:
        state, path, code = setup()
    except Exception as e:  # noqa: BLE001 — a person reads this (often from a menu entry): the cause, no traceback
        print(f"Hub by Icons8: the first start could not be prepared: {e}. Check {os.path.join(ROOT, 'code-seed')} and git.",
              file=sys.stderr)
        return 1
    if "-v" in argv:
        print(f"state: {state}\nsettings: {path}\ncode: {code}", file=sys.stderr)
    print(code)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
