"""The app shell's own update (app spec §4.4, slice A4): a release whose layer needs a newer environment (runtime_min
above the shell's runtime) comes with a new app, and "Update" puts it in place of the old one. This file belongs to the
shell, as launch.py next to it: it is frozen at build time, so the watchdog of a replacement is always the PREVIOUS
shell's own code, and nothing of the new one runs until the new one is in place. The stdlib only; it imports nothing
of the hub (launch.py's helpers only).

The record <state>/layers/shell-update.json says where a replacement is (the hub's core/shellnext.py reads it too):

  ready     the hub downloaded the new app (modules/update/layer.py: the sum of releases.json) and `prepare` checked it:
            a Mac — the .dmg mounted, its "Agents Hub.app" copied next to the installed one (".Agents Hub.app.new" in the
            same folder), `codesign --verify --deep --strict`, the same bundle id, the same Team ID as the installed app
            when that one has one, its PACKAGED (platform, environment), the download's quarantine off what was checked;
            Windows — the installed app folder copied to "<app>.old" (the watchdog runs from that copy and it is what
            comes back). Linux has no `prepare`: a .deb is installed by root (pkexec/apt), the hub only downloads it.
  swapping  hub-up, at a full restart with no part running, started `swap` from the old shell (the record's watcher).
            Mac: one atomic swap of the two folders (renamex_np RENAME_SWAP), then the old one becomes "<app>.old" and
            the new one is checked in place again; hub-up exits and launchd starts the app by its path — the new one.
            Windows: hub-up exits (the "stopped" mark keeps the scheduler's repeat from starting it meanwhile), the
            watchdog waits for its processes to be gone and runs Setup.exe /VERYSILENT, then takes the mark off and
            runs the task itself (the postinstall may not have: under Setup install.ps1 was not found through the
            code's junction, winstand); a Setup that refuses or rolls back (rc != 0, the installed app unchanged) —
            the hub is started again on the old app.
  swapped   the new app is in place; the watchdog waits for its core: layers/start.json marked ok (core/appwatch.came_up)
            after the swap, within WINDOW of the watchdog's own clock. Its window starts again after a reboot (launch.py
            starts a watchdog of a record without a live one).
  done      the core came up on the new app. The new hub writes it up and removes "<app>.old".
  returned  it did not: the new app goes to "<app>.bad", the old one back in place and the hub is started (launchd
            kickstart; Windows: the app's processes ended, the folder copied back, the task run). The hub — the old one,
            up again — writes it up from this record (why) and does not offer that app again.
  failed    the replacement did not start (a check, a rename, a Setup that changed nothing): the old app is untouched.

An environment folder (the one-line install, spec §6.2): the hub runs through its pointer <state>/app (a link; a
junction on Windows) to <data>/shells/<runtime>-<flavor>, and the service starts <state>/app/start.py. The new
environment is unpacked and checked next to the old one by the update (modules/update/layer.py); the replacement is the
pointer moved, the same on the three systems and without root (no package manager on Linux):
  ready     `prepare <state> <new environment folder> <runtime>`: the new folder in the same shells folder as the old one,
            its mark, its PACKAGED (the platform, the flavor, the environment asked for), its installer's steps done, its
            Python starts. The watchdog is the old environment's own Python and this file, by their real paths (the
            pointer will lead to the new one).
  swapping  the pointer moved to the new environment (one step: a link replaced; Windows — a junction made again).
  swapped   hub-up exits; launchd and systemd start the service again through the pointer; Windows and a hub without a
            service — the watchdog starts it once the old hub-up is gone (Windows: its "stopped" mark keeps the
            scheduler's repeat away meanwhile).
  done | returned   as above; returned — the pointer back on the old environment, the processes of the new one ended
            (Windows) and the hub's service restarted. The old environment stays on disk until the next replacement.

    python3 shellswap.py prepare <state> <file> <runtime needed>    the hub, after its download (exit 0 = ready)
    python3 shellswap.py swap <state> [--resume]                    the watchdog (hub-up starts it, launch.py resumes it)
"""
import contextlib
import ctypes
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import launch  # noqa: E402 — the shell's own helpers (_read, _write, alive, detached): frozen with this file

RECORD = "shell-update.json"   # in <state>/layers; core/shellnext.py RECORD
CLAIM = "shell-update.claim"   # in <state>/layers: who moves the app — this watchdog, or hub-up that gave up on it
SHELLS = "shells"              # <state>/layers/shells/<file>: the downloaded apps
BUNDLE_ID = "com.hub-core.app"   # deploy/mac/app.py APP_ID
LABEL = "com.hub-core"
TASK = "Agents Hub"              # the scheduler task (deploy/windows/install.ps1)
HELD = "stopped"                 # core/restart.HELD: the scheduler's repeat leaves a hub with this mark alone
WINDOW = {"mac": 180, "windows": 300}   # s of the watchdog's clock: a fresh Windows takes up to ~50 s to the first line (A0)
WAIT_HUB = 180                   # s for the old hub-up to be gone before Setup.exe runs
TICK = 2
BEAT = 20                        # s between the watchdog's beats (its own thread, for its whole life)
BEAT_STALE = 90                  # s: an older beat — the watchdog's number belongs to another process now
RENAME_SWAP = 0x00000002         # <sys/stdio.h>: renamex_np swaps the two names in one step
MARK = ".agents-hub-shell.json"  # in an environment folder of the one-line install (deploy/oneline/unpack.py)
SEALED = ".agents-hub-sealed"    # its installer's own steps are done (install.py)
POINTER = "app"                  # <state>/app → the environment the hub runs on
FOLDER_WINDOW = 300              # s for the core on the new environment (Windows' first start under Defender is slow)
NO_WINDOW = 0x08000000


class Refused(Exception):
    """The downloaded app is not one the hub puts in place: the text says why."""


def kind(platform):
    platform = str(platform or "")
    return "mac" if platform.startswith("mac-") else "windows" if platform.startswith("windows-") else "linux"


def record_path(state):
    return os.path.join(state, "layers", RECORD)


def read(state):
    return launch._read(record_path(state), dict)


def write(state, rec):
    launch._write(record_path(state), rec)


_LOCK = __import__("threading").Lock()   # the beat's thread and the watchdog's own steps write the same record


def update(state, **changes):
    with _LOCK:
        rec = {**read(state), **changes}
        write(state, rec)
        return rec


def claim(state, who):
    """The one who moves the app: the file made exclusively — True for the first caller only (hub-up's late "the hub
    comes up as it is" and the watchdog's "I swap now" cannot both win)."""
    try:
        fd = os.open(os.path.join(state, "layers", CLAIM), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as f:
        f.write(who)
    return True


def beats(state, stop, every=BEAT, now=time.time):
    """The watchdog's beat into its record until stop: resume() tells a live watchdog by it, not by its number."""
    while not stop.wait(every):
        with contextlib.suppress(OSError):
            update(state, beat=now())


def say(state, text):
    with contextlib.suppress(OSError):
        os.makedirs(os.path.join(state, "logs"), exist_ok=True)
        with open(os.path.join(state, "logs", "shell-update.log"), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")


def shell_of(state):
    """(the shell folder that runs the hub now, its Python): layers/shell.json, written by launch.py at every start."""
    got = launch._read(os.path.join(state, "layers", "shell.json"), dict)
    script, python = got.get("launch"), got.get("python")
    if not (isinstance(script, str) and isinstance(python, str) and os.path.isfile(script)):
        raise Refused("layers/shell.json does not name a shell (the hub was not started by an app)")
    return os.path.dirname(os.path.abspath(script)), python


def packaged(folder):
    got = launch._read(os.path.join(folder, "PACKAGED"), dict)
    if got.get("format") != "app":
        raise Refused(f"{folder}: no PACKAGED of an app")
    return got


def runtime_of(mark):
    n = mark.get("runtime", 0)
    return n if isinstance(n, int) and not isinstance(n, bool) else 0


def cmd(run, args, timeout=600):
    """A program's run: (exit code, its output) — never raises (a missing program is code 127)."""
    try:
        r = run(args, capture_output=True, text=True, errors="replace", stdin=subprocess.DEVNULL, timeout=timeout,
                **({"creationflags": NO_WINDOW} if os.name == "nt" else {}))
    except (OSError, subprocess.SubprocessError) as e:
        return 127, str(e)
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()


# --- the Mac ------------------------------------------------------------------------------------------------------

def mac_app(shell):
    """<app> of the shell folder <app>/Contents/Resources/hub."""
    app = os.path.dirname(os.path.dirname(os.path.dirname(shell)))
    if not app.endswith(".app"):
        raise Refused(f"{shell}: not inside an .app")
    return app


def team_of(app, run):
    """The signature's Team ID, or None (ad-hoc, unsigned)."""
    code, out = cmd(run, ["codesign", "-dv", "--verbose=2", app], 60)
    for line in out.splitlines():
        if line.startswith("TeamIdentifier=") and line.split("=", 1)[1] not in ("", "not set"):
            return line.split("=", 1)[1].strip()
    return None


def verify_mac(new, need, platform, team, run):
    """The new app as the installed one may be replaced by: sealed, the same bundle, the same team, enough environment."""
    code, out = cmd(run, ["codesign", "--verify", "--deep", "--strict", new])
    if code:
        raise Refused(f"codesign --verify: {out[-300:] or code}")
    try:
        with open(os.path.join(new, "Contents", "Info.plist"), "rb") as f:
            ident = plistlib.load(f).get("CFBundleIdentifier")
    except (OSError, ValueError, plistlib.InvalidFileException):
        ident = None
    if ident != BUNDLE_ID:
        raise Refused(f"the bundle is {ident!r}, not {BUNDLE_ID}")
    if team and team_of(new, run) != team:
        raise Refused(f"the new app is not signed by the team {team}")
    mark = packaged(os.path.join(new, "Contents", "Resources", "hub"))
    if mark.get("platform") != platform or runtime_of(mark) < need:
        raise Refused(f"the new app is {mark.get('platform')} with environment {runtime_of(mark)}, "
                      f"the hub needs {platform} with {need}")
    return mark


def prepare_mac(state, shell, file, need, platform, run):
    app = mac_app(shell)
    new = os.path.join(os.path.dirname(app), "." + os.path.basename(app) + ".new")
    if os.path.lexists(new):
        shutil.rmtree(new)
    mount = tempfile.mkdtemp(prefix="hub-app-")
    code, out = cmd(run, ["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-mountpoint", mount, file])
    if code:
        os.rmdir(mount)
        raise Refused(f"hdiutil attach: {out[-300:] or code}")
    try:
        inside = os.path.join(mount, os.path.basename(app))
        if not os.path.isdir(inside):
            raise Refused(f"the disk image has no {os.path.basename(app)}")
        code, out = cmd(run, ["ditto", inside, new])
        if code:
            raise Refused(f"ditto: {out[-300:] or code}")
    finally:
        cmd(run, ["hdiutil", "detach", mount, "-force"], 120)
        with contextlib.suppress(OSError):
            os.rmdir(mount)
    team = team_of(app, run)
    try:
        mark = verify_mac(new, need, platform, team, run)
    except Refused:
        shutil.rmtree(new, ignore_errors=True)
        raise
    # only what the hub downloaded itself and checked: the image of a browser download would carry it (A0: SIGKILL)
    cmd(run, ["xattr", "-dr", "com.apple.quarantine", new], 120)
    return {"app": app, "new": new, "old": app + ".old", "mark": mark, "team": team}


def swap_names(a, b):
    """a and b change places in one step (macOS renamex_np); False — not possible here (then two renames)."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        fn = libc.renamex_np
    except (OSError, AttributeError, TypeError):   # not macOS (Windows has no CDLL(None))
        return False
    fn.argtypes, fn.restype = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint], ctypes.c_int
    if fn(os.fsencode(a), os.fsencode(b), RENAME_SWAP) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), a)
    return True


def swap_mac(state, rec, run):
    app, new, old = rec["app"], rec["new"], rec["old"]
    if os.path.lexists(old):
        shutil.rmtree(old)
    if swap_names(new, app):   # app is the new one now, new holds the old one
        os.replace(new, old)
    else:
        os.replace(app, old)
        os.replace(new, app)
    try:
        verify_mac(app, rec["runtime"], rec["platform"], rec.get("team"), run)   # A0: in place, before it runs
    except Refused:
        os.replace(app, new)
        os.replace(old, app)
        raise
    # the watchdog's own files are in the old app now: a resumed watchdog runs them from there
    rec["watcher"] = [p.replace(app, old, 1) if p.startswith(app + os.sep) else p for p in rec["watcher"]]


def give_back_mac(state, rec, run):
    app, old = rec["app"], rec["old"]
    if not os.path.isdir(old):
        raise Refused(f"no {old} to put back")
    bad = app + ".bad"
    if os.path.lexists(bad):
        shutil.rmtree(bad, ignore_errors=True)
    if os.path.lexists(app):
        if not swap_names(old, app):
            os.replace(app, bad)
            os.replace(old, app)
        else:
            os.replace(old, bad)
    else:
        os.replace(old, app)
    kick = os.environ.get("HUB_SHELL_KICK")   # a stand's own supervisor (the Mac bridge has no launchd agents)
    args = json.loads(kick) if kick else ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LABEL}"]
    code, out = cmd(run, args, 120)
    say(state, f"kickstart: {code} {out[-200:]}")


# --- Windows ------------------------------------------------------------------------------------------------------

def copy_tree(src, dst):
    if os.path.lexists(dst):
        shutil.rmtree(dst, onexc=launch._writable_again)
    shutil.copytree(src, dst, symlinks=True)


# Windows' own word on a file's signature: "<status>" and "<signer's subject>" on two lines (the path in an environment
# variable: no quoting of a path into the command)
SIGNATURE_PS = ("[Console]::OutputEncoding = New-Object Text.UTF8Encoding $false; "
                "$s = Get-AuthenticodeSignature -LiteralPath $env:HUB_SIGNED_FILE; "
                "Write-Output ([string]$s.Status); Write-Output ([string]$s.SignerCertificate.Subject)")


def subject_org(subject):
    """O= of a certificate subject as Windows writes it ("CN=X, O=\"Y, Z\", L=…")."""
    for m in re.finditer(r'(?:^|,)\s*([A-Za-z.0-9]+)=("(?:[^"]|"")*"|[^,]*)', subject or ""):
        if m.group(1) == "O":
            value = m.group(2).strip()
            return value[1:-1].replace('""', '"') if value.startswith('"') else value
    return None


def verify_windows(file, publisher, run):
    """The new Setup.exe is validly signed by the publisher the installed app was built for (its PACKAGED)."""
    try:
        r = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", SIGNATURE_PS], capture_output=True,
                text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=300,
                env={**os.environ, "HUB_SIGNED_FILE": file}, **({"creationflags": NO_WINDOW} if os.name == "nt" else {}))
        lines = [x.strip().lstrip("\ufeff") for x in (r.stdout or "").splitlines()]
    except (OSError, subprocess.SubprocessError) as e:
        raise Refused(f"the signature of {os.path.basename(file)} is not checked: {e}") from None
    status, subject = (lines + ["", ""])[:2]
    if r.returncode or status != "Valid":
        raise Refused(f"{os.path.basename(file)}: the signature is {status or 'not read'} "
                      f"({(r.stderr or '').strip()[-200:] or r.returncode}), not Valid")
    if subject_org(subject) != publisher:
        raise Refused(f"{os.path.basename(file)} is signed by {subject!r}, not {publisher!r}")
    return subject


def prepare_windows(state, shell, file, need, platform, run, mark=None):
    with open(file, "rb") as f:
        if f.read(2) != b"MZ":
            raise Refused(f"{os.path.basename(file)} is not a program")
    publisher = (mark if mark is not None else packaged(shell)).get("publisher")
    if isinstance(publisher, str) and publisher:   # a signed app: its replacement is signed by the same publisher
        verify_windows(file, publisher, run)
    old = shell + ".old"
    copy_tree(shell, old)   # the watchdog's Python and what comes back if the new app does not come up
    return {"app": shell, "old": old, "setup": file,
            "watcher": [os.path.join(old, "runtime", "python", "python.exe"), os.path.join(old, "shellswap.py")]}


def schtasks(run, *args, task=None):
    return cmd(run, ["schtasks", *args, "/TN", task or TASK], 120)


def start_hub(state, run, end=True, task=None):
    """The hub's task started afresh: its stop mark off, an orphaned instance ended first (A0: a cmd.exe left Running
    makes IgnoreNew swallow /Run) — unless end is False (a hub the task already runs stays). task: a second hub's own."""
    with contextlib.suppress(FileNotFoundError):
        os.unlink(os.path.join(state, HELD))
    if end:
        schtasks(run, "/End", task=task)
    code, out = schtasks(run, "/Run", task=task)
    say(state, f"schtasks /Run: {code} {out[-200:]}")


def hub_alive(state, alive):
    """hub-up or the core of hub-up.json is alive (a hub the task runs now)."""
    up = launch._read(os.path.join(state, "hub-up.json"), dict)
    return any(isinstance(p, int) and not isinstance(p, bool) and alive(p) for p in (up.get("pid"), up.get("core")))


def hub_gone(state, alive, clock, sleep, wait=WAIT_HUB):
    up = launch._read(os.path.join(state, "hub-up.json"), dict)
    pids = [p for p in (up.get("pid"), up.get("core")) if isinstance(p, int) and not isinstance(p, bool)]
    end = clock() + wait
    while any(alive(p) for p in pids):
        if clock() >= end:
            return False
        sleep(1)
    return True


def stop_app_processes(app, run):
    """Every process running a program of the app folder (the new app that did not come up), ended by its number."""
    script = ("$a = $args[0].TrimEnd('\\') + '\\'; Get-CimInstance Win32_Process | "
              "Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($a, 'OrdinalIgnoreCase') } | "
              "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    return cmd(run, ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script, app], 120)


def swap_windows(state, rec, run, alive, clock, sleep):
    if not hub_gone(state, alive, clock, sleep):
        raise Refused("the hub did not stop for the new app")
    log = os.path.join(state, "logs", f"setup-{rec.get('runtime')}.log")
    code, out = cmd(run, [rec["setup"], "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/LOG={log}"], 1800)
    say(state, f"Setup.exe: {code} {out[-200:]}")
    if installed(rec):
        # the new app is there. Its postinstall starts the task as a rule; the watchdog starts it too — winstand: a
        # process of Setup's tree may not follow the code's junction, and then install.ps1 neither ran nor took the
        # "stopped" mark off. A hub the postinstall started stays (IgnoreNew drops this start); without one, an
        # orphaned instance is ended first
        start_hub(state, run, end=not hub_alive(state, alive))
        return
    raise Refused(f"Setup.exe did not install the new app (exit code {code}; its log: {log})")


def give_back_windows(state, rec, run, sleep):
    app, old = rec["app"], rec["old"]
    if not os.path.isfile(os.path.join(old, "PACKAGED")):
        raise Refused(f"no {old} to put back")
    with open(os.path.join(state, HELD), "w"):
        pass   # the repeat of the task must not start the broken app meanwhile
    try:
        schtasks(run, "/End")
        bad = app + ".bad"
        for _ in range(30):   # a process that just ended may hold its files a moment longer
            stop_app_processes(app, run)
            try:
                if os.path.lexists(bad):
                    shutil.rmtree(bad, onexc=launch._writable_again)
                os.replace(app, bad)
                break
            except OSError:
                sleep(1)
        else:
            raise Refused(f"{app} is still held by a process")
        shutil.copytree(old, app, symlinks=True)
    finally:   # back or not, the hub is started: held, nobody would ever start it again
        start_hub(state, run)


# --- an environment folder (the one-line install) -----------------------------------------------------------------

def pointer_of(state):
    return os.path.join(state, POINTER)


def folder_shell(shell):
    """The environment folder a shell path leads to when it is one of the one-line install (its mark), else None."""
    real = os.path.realpath(shell)
    return real if os.path.isfile(os.path.join(real, MARK)) else None


def python_in(env):
    return os.path.join(env, "runtime", "python", "python.exe" if os.name == "nt" else os.path.join("bin", "python3"))


def same_path(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def check_folder(new, old, need, mark, run):
    """The new environment as the pointer may lead to it: another folder next to the old one, finished by the install,
    of the same platform and flavor, with the environment asked for, its Python starting. -> its PACKAGED."""
    if same_path(new, old) or not same_path(os.path.dirname(new), os.path.dirname(old)):
        raise Refused(f"{new} is not another environment next to {old}")
    if not (os.path.isfile(os.path.join(new, MARK)) and os.path.isfile(os.path.join(new, SEALED))):
        raise Refused(f"{new} is not an environment the install finished")
    got = packaged(new)
    if got.get("platform") != mark.get("platform") or got.get("flavor") != mark.get("flavor") or runtime_of(got) < need:
        raise Refused(f"the new environment is {got.get('platform')} {got.get('flavor')} with environment "
                      f"{runtime_of(got)}, the hub needs {mark.get('platform')} {mark.get('flavor')} with {need}")
    code, out = cmd(run, [python_in(new), "-I", "-B", "-c", "import ssl, sqlite3"], 120)
    if code:
        raise Refused(f"the new environment's Python does not start: {out[-300:] or code}")
    return got


def prepare_folder(state, old, new, need, mark, run):
    link = pointer_of(state)
    if not (launch.appcode().is_pointer(link) and same_path(link, old)):
        raise Refused(f"{link} does not lead to the environment the hub runs on ({old})")
    new = os.path.realpath(new)
    check_folder(new, old, need, mark, run)
    # the watchdog by real paths: the pointer will lead to the new environment while it runs
    return {"folder": True, "app": link, "old": old, "new": new,
            "watcher": [os.path.realpath(python_in(old)), os.path.join(old, "shellswap.py")]}


def point_to(state, env):
    """<state>/app → env (one step on macOS and Linux; on Windows the junction is made again). A pointer that does not
    lead there after it is put back where it led (a hub must never be left with a pointer to nowhere)."""
    link = pointer_of(state)
    was = os.path.realpath(link) if os.path.lexists(link) else None
    try:
        launch.appcode().point(link, os.path.dirname(env), os.path.basename(env))
        ok = same_path(link, env)
    except OSError as e:   # Windows: the junction removed and not made again
        ok, why = False, e
    else:
        why = None
    if not ok:
        if was and os.path.isdir(was):
            with contextlib.suppress(OSError):
                launch.appcode().point(link, os.path.dirname(was), os.path.basename(was))
        raise Refused(f"the pointer does not lead to {env}" + (f": {why}" if why else ""))


def start_plain(state):
    """A hub with no service: its start through the pointer, apart from the watchdog."""
    app = pointer_of(state)
    launch.detached([python_in(app), "-I", "-B", os.path.join(app, "start.py")])
    say(state, "started the hub through the pointer (no service)")


def stop_plain(state, alive, clock, sleep):
    """A hub with no service: its hub-up told to stop, and waited for."""
    import signal
    up = launch._read(os.path.join(state, "hub-up.json"), dict).get("pid")
    if isinstance(up, int) and not isinstance(up, bool) and up > 0 and alive(up) and is_hub_up(up):
        with contextlib.suppress(OSError):
            os.kill(up, signal.SIGTERM)
    return hub_gone(state, alive, clock, sleep)


def is_hub_up(pid):
    """The number is hub-up's still (its command line), not another program's after a restart of the machine."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            line = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        line = cmd(subprocess.run, ["ps", "-o", "command=", "-p", str(pid)], 30)[1]
    return "hub-up" in line


def start_after_swap(state, rec, run, alive, clock, sleep):
    """After the pointer moved and hub-up exited: launchd and systemd start the service again by themselves (hub-up's
    exit code); a scheduler task (its "stopped" mark kept the repeat away) and a hub with no service — the watchdog does."""
    svc = rec.get("service") or {}
    if svc.get("kind") in ("launchd", "systemd", "service"):
        return
    if not hub_gone(state, alive, clock, sleep):
        say(state, "the old hub-up has not gone: the hub is started anyway")
    if svc.get("kind") == "task" or os.name == "nt":
        start_hub(state, run, end=not hub_alive(state, alive), task=svc.get("id"))
    else:
        start_plain(state)


def give_back_folder(state, rec, run, alive, clock, sleep):
    """The pointer back on the old environment and the hub started again on it."""
    old, new = rec["old"], rec["new"]
    if not os.path.isfile(os.path.join(old, MARK)):
        raise Refused(f"no {old} to put back")
    svc = rec.get("service") or {}
    if os.name == "nt":
        with open(os.path.join(state, HELD), "w"):
            pass   # the repeat of the task must not start the new environment meanwhile
        try:
            schtasks(run, "/End", task=svc.get("id"))
            for path in (pointer_of(state), new):   # a process names the folder it was started by: the pointer, or not
                stop_app_processes(path, run)
            point_to(state, old)
        finally:
            start_hub(state, run, task=svc.get("id"))
        return
    point_to(state, old)
    service = svc.get("kind")
    if service == "service":   # one whose file was not found: the system's own
        service = "launchd" if sys.platform == "darwin" else "systemd"
    if service == "launchd":
        kick = os.environ.get("HUB_SHELL_KICK")   # a stand's own supervisor (the Mac bridge has no launchd agents)
        args = json.loads(kick) if kick else ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{svc.get('id') or LABEL}"]
    elif service == "systemd":
        args = ["systemctl", "--user", "restart", svc.get("id") or "agents-hub.service"]
    else:
        stop_plain(state, alive, clock, sleep)
        start_plain(state)
        return
    code, out = cmd(run, args, 120)
    say(state, f"{' '.join(args[:3])}: {code} {out[-200:]}")


# --- the steps ----------------------------------------------------------------------------------------------------

def prepare(state, file, need, run=subprocess.run, now=time.time):
    """The downloaded new app, checked and laid out for the swap (see the record's "ready"). -> the record."""
    shell, python = shell_of(state)
    mark = packaged(shell)
    platform, os_kind = mark.get("platform"), kind(mark.get("platform"))
    rec = {"status": "ready", "platform": platform, "runtime": need, "was_runtime": runtime_of(mark),
           "file": file, "at": now(), "watcher": [python, os.path.join(shell, "shellswap.py")]}
    env = folder_shell(shell)
    if env:   # the one-line install: file is the new environment's folder, unpacked and checked next to this one
        rec.update(prepare_folder(state, env, file, need, mark, run))
        rec.update(window=FOLDER_WINDOW)
    elif os_kind == "mac":
        got = prepare_mac(state, shell, file, need, platform, run)
        rec.update(app=got["app"], new=got["new"], old=got["old"], team=got["team"])
    elif os_kind == "windows":
        rec.update(prepare_windows(state, shell, file, need, platform, run, mark))
    else:
        raise Refused("on Linux the new app is installed by the package manager (root)")
    if not env:
        rec.update(window=WINDOW[os_kind])
    with contextlib.suppress(FileNotFoundError):   # a new replacement: nobody has moved anything for it yet
        os.unlink(os.path.join(state, "layers", CLAIM))
    write(state, {**read(state), **rec, "status": "ready"})
    say(state, f"ready: environment {rec['was_runtime']} -> {need} ({os.path.basename(file)})")
    return rec


def installed(rec):
    """The app at its path is the new one (its PACKAGED carries the environment the record asked for; an environment
    folder — the pointer leads to the new one)."""
    if rec.get("folder"):
        return same_path(rec["app"], rec["new"])
    hub = os.path.join(rec["app"], "Contents", "Resources", "hub") if kind(rec.get("platform")) == "mac" else rec["app"]
    return runtime_of(launch._read(os.path.join(hub, "PACKAGED"), dict)) >= rec["runtime"]


def started_ok(state, since):
    start = launch._read(os.path.join(state, "layers", "start.json"), dict)
    at = start.get("at")
    return bool(start.get("ok")) and isinstance(at, (int, float)) and at >= since - 1


def watch(state, rec, clock, sleep, now):
    """The core on the new app within the window -> True."""
    window = rec.get("window") or 180
    with contextlib.suppress(ValueError, TypeError):
        window = float(os.environ.get("HUB_SHELL_WINDOW") or window)   # a stand's short window
    end = clock() + window
    while clock() < end:
        if started_ok(state, rec.get("since") or 0):
            return True
        sleep(TICK)
    return started_ok(state, rec.get("since") or 0)


def swap(state, resume=False, run=subprocess.run, clock=time.monotonic, sleep=time.sleep, now=time.time,
         alive=launch.alive):
    """The watchdog's whole life (the record's swapping → swapped → done | returned | failed). -> the final status."""
    rec = read(state)
    os_kind = kind(rec.get("platform"))
    if rec.get("status") not in (("swapping", "swapped") if resume else ("ready",)) or (
            os_kind == "linux" and not rec.get("folder")):
        return rec.get("status") or "none"
    if rec["status"] == "ready" and not claim(state, "watchdog"):   # hub-up gave up waiting for this watchdog
        return read(state).get("status") or "none"
    rec = update(state, pid=os.getpid(), beat=now())
    stop = __import__("threading").Event()
    __import__("threading").Thread(target=beats, args=(state, stop), kwargs={"now": now}, daemon=True).start()
    try:
        return _swap(state, rec, resume, os_kind, run, clock, sleep, now, alive)
    finally:
        stop.set()


def _swap(state, rec, resume, os_kind, run, clock, sleep, now, alive):
    try:
        if rec["status"] == "ready":
            # since: the core's start must come after it (Windows: Setup's own postinstall starts the hub before it returns)
            rec = update(state, status="swapping", since=now())
            if rec.get("folder"):
                point_to(state, rec["new"])
            elif os_kind == "mac":
                swap_mac(state, rec, run)
            else:
                swap_windows(state, rec, run, alive, clock, sleep)
            rec = update(state, status="swapped", watcher=rec["watcher"])
            say(state, f"swapped: the new app is in place, waiting for its core ({rec.get('window')} s)")
            started = rec.get("folder")
        elif rec["status"] == "swapping":   # a reboot in the middle: what is at the app's path says how far it got
            started = False
            if not installed(rec):
                if os_kind == "windows" and not rec.get("folder"):   # a Setup cut off may have left half of the new files: the copy comes back
                    return give_back(state, rec, run, sleep, now, "cut")
                raise Refused("cut off before the new app was in place")
            if os_kind == "mac" and not rec.get("folder") and os.path.isdir(rec["new"]):   # swapped, not yet aside
                os.replace(rec["new"], rec["old"])
                rec["watcher"] = [p.replace(rec["app"], rec["old"], 1) for p in rec["watcher"]]
            rec = update(state, status="swapped", watcher=rec["watcher"])
        else:
            started = False
    except (Refused, OSError, KeyError) as e:
        say(state, f"failed: {e}")
        update(state, status="failed", why=str(e)[:500], at_end=now())
        if os_kind == "windows":
            start_hub(state, run, task=(rec.get("service") or {}).get("id"))   # the old app is untouched: up again
        return "failed"
    if started:   # the pointer leads to the new one now: a hub that does not start there is the window's to tell
        try:
            start_after_swap(state, rec, run, alive, clock, sleep)
        except Exception as e:  # noqa: BLE001 — whatever it is, the window tells and the way back follows
            say(state, f"the hub did not start on the new environment: {e!r}")
    if watch(state, rec, clock, sleep, now):
        update(state, status="done", at_end=now())
        say(state, "done: the core came up on the new app")
        return "done"
    say(state, "the new app did not bring the core up: the old one goes back")
    return give_back(state, rec, run, sleep, now, "timeout", alive, clock)


def give_back(state, rec, run, sleep, now, why, alive=launch.alive, clock=time.monotonic):
    try:
        if rec.get("folder"):
            give_back_folder(state, rec, run, alive, clock, sleep)
        elif kind(rec.get("platform")) == "mac":
            give_back_mac(state, rec, run)
        else:
            give_back_windows(state, rec, run, sleep)
    except (Refused, OSError) as e:
        say(state, f"the old app is not back: {e}")
        update(state, status="returned", why=why, back=False, error=str(e)[:500], at_end=now())
        return "returned"
    update(state, status="returned", why=why, back=True, at_end=now())
    return "returned"


def resume(state, run=launch.detached, now=time.time):
    """launch.py at a start: a replacement whose watchdog is gone (a reboot in the middle) gets it again, its clock from
    now. True when one was started."""
    rec = read(state)
    if rec.get("status") not in ("swapping", "swapped") or (kind(rec.get("platform")) == "linux" and not rec.get("folder")):
        return False
    beat = rec.get("beat")
    fresh = isinstance(beat, (int, float)) and abs(now() - beat) < BEAT_STALE
    if launch.alive(rec.get("pid")) and fresh:
        return False
    watcher = rec.get("watcher")
    if not (isinstance(watcher, list) and len(watcher) == 2 and all(isinstance(p, str) and os.path.isfile(p)
                                                                      for p in watcher)):
        return False
    run([watcher[0], "-I", "-B", watcher[1], "swap", state, "--resume"])
    return True


if __name__ == "__main__":
    args = sys.argv[1:]
    try:
        if args[:1] == ["prepare"] and len(args) == 4:
            prepare(args[1], args[2], int(args[3]))
            sys.exit(0)
        if args[:1] == ["swap"] and len(args) in (2, 3):
            print(swap(args[1], resume=args[2:] == ["--resume"]))
            sys.exit(0)
    except Refused as e:
        print(f"Hub by Icons8: the new app is not taken: {e}", file=sys.stderr)
        sys.exit(3)
    print(__doc__, file=sys.stderr)
    sys.exit(2)
