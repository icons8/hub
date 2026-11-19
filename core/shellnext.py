"""The hub's side of the app shell's replacement (app spec §4.4, slice A4). The shell's own tools/package/shellswap.py
checks and swaps; this file is what the running hub does around it:

- at_restart (hub-up, at a full restart, no part running): a new app left "ready" — its watchdog is started from the old
  shell (the record names it) and hub-up exits instead of starting itself again, so that the service starts the app
  by its path: launchd the swapped "Agents Hub.app"; on Windows Setup.exe, which the watchdog runs once hub-up is gone
  (the "stopped" mark keeps the scheduler's repeat away meanwhile). The shell on disk is no longer the one this hub-up
  came from (a .deb installed by the person, a new app dragged into Applications): hub-up exits too, where a service
  starts it again (launchd; systemd — the unit restarts on a failure code).
- A replacement that ended (done | returned | failed) is written up once by the update (modules/update/layer.py).

The record is <state>/layers/shell-update.json (the shell's shellswap.RECORD). This file imports nothing heavy: hub-up
loads it."""
import json
import os
import subprocess
import time

RECORD = "shell-update.json"
CLAIM = "shell-update.claim"   # tools/package/shellswap.CLAIM
HELD = "stopped"     # core/restart.HELD
WAIT = 120           # s for the watchdog to begin (Windows: then hub-up exits for Setup.exe)
WAIT_SWAP = 600      # s for a Mac swap that has begun to be in place


def record_path(state):
    return os.path.join(state, "layers", RECORD)


def _json(path):
    try:
        with open(path, encoding="utf-8") as f:
            got = json.load(f)
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def read(state):
    return _json(record_path(state))


def write(state, rec):
    from core import catalog   # whole or not at all, waiting for a reader on Windows
    catalog.write_private(record_path(state), rec)


def kind(platform):
    platform = str(platform or "")
    return "mac" if platform.startswith("mac-") else "windows" if platform.startswith("windows-") else "linux"


MARK = ".agents-hub-shell.json"   # an environment folder of the one-line install (deploy/oneline/unpack.py)


def folder_env(state):
    """The one-line install's environment folder this hub runs on (<state>/app leads to it, its mark there), or None."""
    link = os.path.join(state, "app")
    if not (os.path.islink(link) or (hasattr(os.path, "isjunction") and os.path.isjunction(link))):
        return None
    real = os.path.realpath(link)
    return real if os.path.isfile(os.path.join(real, MARK)) else None


def python_of(env):
    """The Python of an environment folder."""
    import platforms
    windows = platforms.current.APP_OS == "windows"
    return os.path.join(env, "runtime", "python", "python.exe" if windows else os.path.join("bin", "python3"))


def shell_dir(state):
    """The folder of the shell that started this hub (layers/shell.json, launch.py's), or None."""
    script = _json(os.path.join(state, "layers", "shell.json")).get("launch")
    return os.path.dirname(script) if isinstance(script, str) and script else None


def changed(state, repo):
    """The shell on disk carries another PACKAGED than the one its launcher copied into the running layer at this
    hub-up's start: a newer app was installed under the running hub."""
    shell = shell_dir(state)
    if not shell:
        return False
    disk = _json(os.path.join(shell, "PACKAGED"))
    here = _json(os.path.join(repo, "PACKAGED"))
    keys = ("version", "runtime", "platform")
    return bool(disk) and bool(here) and any(disk.get(k) != here.get(k) for k in keys)


def detached(cmd):
    """The watchdog apart from hub-up and outliving it: on Windows outside every job (WMI) — hub-up's supervisor job
    closes with the supervisor, and the scheduler task's may not be left; elsewhere a session of its own."""
    import platforms
    if not platforms.current.popen_outside(cmd):
        platforms.current.popen_detached(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)


def at_restart(state, repo, say, spawn=detached, clock=time.monotonic, sleep=time.sleep, service=None):
    """hub-up's full restart: -> the exit code when hub-up is to exit (the service starts the new app; the OS adapter's
    APP_SWAP_EXIT), else None (exec as always). Never raises: the hub comes up again either way."""
    import platforms
    try:
        rec = read(state)
        if rec.get("status") == "ready":
            return platforms.current.APP_SWAP_EXIT if _swap(state, rec, say, spawn, clock, sleep) else None
        if changed(state, repo) and platforms.current.APP_SWAP_ON_CHANGE and (
                service if service is not None else _service()):
            say("a newer app is installed: the service starts the hub on it")
            return platforms.current.APP_SWAP_EXIT
    except Exception as e:  # noqa: BLE001 — a record, a disk: the hub comes up on its current app
        say(f"the app's replacement: {e!r}")
    return None


def _service():
    """hub-up runs under a service that starts it again when it exits: the Mac app's launchd agent (it names itself in
    HUB_SERVICE) or systemd (INVOCATION_ID)."""
    return bool(os.environ.get("HUB_SERVICE") or os.environ.get("INVOCATION_ID"))


def _service_of(state):
    """{kind, id} of the service that holds this hub (launchd, systemd, task), or {"kind": "none"}: a container, WSL."""
    import platforms
    if not _service() and platforms.current.APP_OS != "windows":   # started by hand or in the background: a file of
        return {"kind": "none", "id": ""}                            # a service next to it does not start this one
    try:
        got = platforms.current.service_unit(state) or {}
    except Exception:  # noqa: BLE001 — not known: the watchdog then starts the hub itself
        got = {}
    if not got.get("kind") and _service():   # under one whose file is not found: it starts the hub again itself
        return {"kind": "service", "id": ""}
    return {"kind": got.get("kind") or "none", "id": got.get("id") or ""}


def _claim(state):
    try:
        os.close(os.open(os.path.join(state, "layers", CLAIM), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    except FileExistsError:
        return False
    return True


def _unhold(state):
    try:
        os.unlink(os.path.join(state, HELD))
    except FileNotFoundError:
        pass


def _swap(state, rec, say, spawn, clock, sleep):
    """True when hub-up is to exit for the new app."""
    watcher = rec.get("watcher")
    if not (isinstance(watcher, list) and len(watcher) == 2 and all(isinstance(p, str) and os.path.isfile(p)
                                                                      for p in watcher)):
        write(state, {**rec, "status": "failed", "why": "the old shell's watchdog is not there"})
        say("the new app is not put in place: the old shell's watchdog is not there")
        return False
    if rec.get("folder"):   # the one-line install: the watchdog starts the hub itself where no service does
        rec = {**rec, "service": _service_of(state)}
        write(state, rec)
    windows = kind(rec.get("platform")) == "windows"
    if windows:   # the scheduler's repeat must not start the hub while Setup.exe replaces its files
        open(os.path.join(state, HELD), "w").close()
    gone = False
    try:
        try:
            spawn([watcher[0], "-I", "-B", watcher[1], "swap", state])
        except OSError as e:
            write(state, {**rec, "status": "failed", "why": f"the watchdog did not start: {e}"})
            say(f"the new app is not put in place: the watchdog did not start ({e})")
            return False
        say("the app is being replaced: " + ("Setup.exe of the new app runs once hub-up is gone" if windows
                                              else "waiting for the swap"))
        began, late = clock(), False
        while True:
            got = read(state)
            if got.get("status") == "swapped" or (windows and got.get("status") == "swapping"):
                say("hub-up exits for Setup.exe" if windows else "the new app is in place: the service starts it")
                gone = True
                return True
            if got.get("status") in ("failed", "returned"):
                say(f"the new app is not put in place: {got.get('why')}")
                return False
            if not late and clock() >= began + WAIT:
                late = True
                if _claim(state):   # the watchdog never began: it will move nothing now, the hub comes up as it is
                    write(state, {**read(state), "status": "failed", "why": "the watchdog did not begin in time"})
                    say("the watchdog did not answer in time: the hub comes up on its current app")
                    return False
            if late and clock() >= began + WAIT_SWAP:   # it has begun and is slow: the service takes what is at the path
                say("the swap is still going on: hub-up exits, the service starts the app at its path")
                gone = True
                return True
            sleep(0.5)
    finally:
        if windows and not gone:
            _unhold(state)
