"""The app's update watchdog (installer spec S3, variant A): did the hub come up on the release it just installed?

modules/update copies this file into the state folder before it moves the code (update/trial-run.py) and starts the copy
as its own process: it is the previous release's code and imports nothing of the hub, so a broken new release cannot
break the check. The trial (update/trial.json, written by the update): from/to (commits), version/previous, started,
window (s), port of the page, core_pid (the core that was running at the install), restart_flag and up_file (hub-up's
files).

The clock starts when the previous core is gone (the full restart waits for busy sessions — that is not the new
release's fault): from then the core and the page must answer /api/about from the new release (core_started and
web_started after the install, code.running = the new commit) within `window`. If they do — status "ok". If not — the
code goes back to `from` (git reset, only if nobody moved it), update/rolledback.json for the previous release's update
process to write up, and the hub restarts: the restart file for hub-up (it execs itself on the code now on disk); if
hub-up does not take it within a minute, it is stopped and the OS service brings it up on the previous code.
`--resume` (the app shell's launcher after a reboot, or the update process at its start): the trial is still waiting
and its watchdog is gone — the clock starts now.

The app's code layer (app spec §4.4–4.5, modules/update/layer.py): the trial names layer {from, to, dir, link} — the
layers' folder and the pointer <state>/code; the hub is up when the page answers from the layer `to` (code.layer), and
the way back is the pointer onto `from` (with layers/failed.json naming `to`), not git.

    python3 trial-run.py <state folder> [--resume]
"""
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request

def _direct_local():
    """As core.net (this file imports nothing of the hub): the hub reaches itself directly past any machine proxy."""
    machine = urllib.request.proxy_bypass
    if getattr(machine, "hub_local", False):
        return

    def proxy_bypass(host):
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        return name.strip("[]") in ("localhost", "::1") or name.startswith("127.") or machine(host)
    proxy_bypass.hub_local = True
    urllib.request.proxy_bypass = proxy_bypass


_direct_local()

TICK = 3
SWITCH_TICK = 30   # while a layer's switch waits for the person's restart: a cheap look, no page request
SWITCH_GIVE_UP = 30 * 86400   # a switch still not taken after this: the watchdog leaves, the trial stays open
GIVE_UP = 48 * 3600   # the previous core never went away (no restart): the watchdog leaves, the code is not touched
TAKE_FLAG = 60        # how long hub-up has to pick up the restart file before it is stopped


def path(state, name):
    return os.path.join(state, "update", name)


def load(state):
    try:
        with open(path(state, "trial.json")) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


WINDOWS = os.name == "nt"


def _waiting(call, patience):
    """call(), repeated while a reader holds the file: on Windows a file open in Python (no FILE_SHARE_DELETE) can be
    neither replaced nor removed and the call raises PermissionError. This file imports nothing of the hub, so the
    retry is its own; elsewhere PermissionError is a real refusal and goes up at once."""
    end = time.monotonic() + patience
    pause = 0.01
    while True:
        try:
            return call()
        except PermissionError:
            if not WINDOWS or time.monotonic() >= end:
                raise
            time.sleep(pause)
            pause = min(pause * 2, 0.2)


def replace(src, dst, patience=5):
    return _waiting(lambda: os.replace(src, dst), patience=patience)


def remove(dst, patience=5):
    """Take the file away; one that is gone, or that stays held, is not an error (the caller has nothing to do about it)."""
    try:
        _waiting(lambda: os.unlink(dst), patience=patience)
    except OSError:
        pass


def put(dst, data, patience=5):
    """JSON into a temp file of this process next to dst and then a replace; on failure the temp file is removed, not left
    for the next run, and the error (OSError) goes up."""
    tmp = f"{dst}.{os.getpid()}"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f)
        replace(tmp, dst, patience)
    except BaseException:
        remove(tmp, patience=0)
        raise


def save(state, trial):
    put(path(state, "trial.json"), trial)


def decide(trial, about, core_gone, now):
    """'done' (the trial is over), 'ok' (the hub answers from the new release), 'wait', 'rollback' (the window passed).
    about — the page's /api/about or None (no answer); core_gone — when the previous core was seen gone, or None."""
    if trial.get("status") != "waiting":
        return "done"
    if isinstance(about, dict):
        started = trial.get("started") or 0
        running = str((about.get("code") or {}).get("running") or "")
        layer = trial.get("layer") if isinstance(trial.get("layer"), dict) else None
        same = (str((about.get("code") or {}).get("layer") or "") == str(layer.get("to") or "-")) if layer else (
            len(running) >= 7 and str(trial.get("to") or "").startswith(running))
        if (isinstance(about.get("core_started"), (int, float)) and about["core_started"] >= started
                and isinstance(about.get("web_started"), (int, float)) and about["web_started"] >= started and same):
            return "ok"
    if core_gone is None:
        return "wait"
    return "rollback" if now >= core_gone + (trial.get("window") or 180) else "wait"


def ask_about(port):
    """The page's /api/about from this machine, without the owner's proxy; None if it does not answer."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{int(port)}/api/about", timeout=5) as r:
            data = json.loads(r.read() or b"{}")
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def alive(pid):
    """The process lives. Windows: tasklist (os.kill(pid, 0) there is Ctrl+C to the whole console), read as bytes — its
    numbers carry the console code page's no-break space, and the hub's parts run in UTF-8 mode (winstand: as text the
    watchdog died right after an install)."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if WINDOWS:
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], capture_output=True,
                                 timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            return True   # cannot tell: do not start the clock on a guess
        return f'"{pid}"'.encode() in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def switch_waits(trial):
    """A layer's switch to the trial's release still waits for the restart (layers/next.json names it): a background
    install the person has not restarted for yet."""
    layer = trial.get("layer") if isinstance(trial.get("layer"), dict) else None
    if not layer or not isinstance(layer.get("dir"), str):
        return False
    try:
        with open(os.path.join(layer["dir"], "next.json"), encoding="utf-8") as f:
            return json.load(f).get("name") == layer.get("to")
    except (OSError, ValueError, AttributeError):
        return False


def switch_dropped(trial):
    """A layer's switch that is neither waiting nor taken: next.json gone, the pointer not on the new layer."""
    layer = trial.get("layer") if isinstance(trial.get("layer"), dict) else None
    if not layer or not isinstance(layer.get("link"), str) or switch_waits(trial):
        return False
    return pointed(layer["link"]) != layer.get("to")


def up_core(trial):
    """The core hub-up.json names now, or None (no file, unreadable)."""
    try:
        with open(trial.get("up_file") or "") as f:
            core = json.load(f).get("core")
    except (OSError, ValueError, AttributeError):
        return None
    return core if isinstance(core, int) and not isinstance(core, bool) else None


def still_old_core(trial):
    """The core of the install is alive and hub-up still names it (a pid reused after a reboot is not in hub-up.json)."""
    core = up_core(trial)
    return core is not None and core == trial.get("core_pid") and alive(core)


def restarted(trial, about, first):
    """With no core of the install known (core_pid None — hub-up.json had none at the install): the restart has
    happened once a core started after the install answers the page, or hub-up names another core than when the
    watchdog (or the taken switch) began (first). Without this the clock started at once (review 02.11)."""
    if isinstance(about, dict) and isinstance(about.get("core_started"), (int, float)) \
            and about["core_started"] >= (trial.get("started") or 0):
        return True
    return up_core(trial) != first


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=120,
                          stdin=subprocess.DEVNULL)


try:   # Windows: the layer's pointer is a junction (as core/appcode.point; this file imports nothing of the hub)
    import _winapi
except ImportError:
    _winapi = None


def pointed(link):
    """The layer name the pointer leads to, or None (no pointer)."""
    try:
        return os.path.basename(os.readlink(link).rstrip("/\\"))
    except (OSError, ValueError):
        return None


def point(link, parent, name):
    if _winapi is not None:
        if os.path.lexists(link):
            os.rmdir(link)
        _winapi.CreateJunction(os.path.join(parent, name), link)
        return
    tmp = f"{link}.{os.getpid()}.part"
    if os.path.lexists(tmp):
        os.unlink(tmp)
    os.symlink(os.path.relpath(os.path.join(parent, name), os.path.dirname(link)), tmp)
    os.replace(tmp, link)


def roll_layer(state, trial, why):
    """The app's code layer back to `from`: 'rolled-back', or 'moved' (the pointer is on neither layer of the trial).
    As roll_tree: the write-up (update/rolledback.json) first, the layer marked as not coming up (layers/failed.json:
    neither the update nor a shell of the same release offers it again), the switch left for a restart taken back,
    then the pointer."""
    layer = trial["layer"]
    link, parent, old, new = layer.get("link"), layer.get("dir"), layer.get("from"), layer.get("to")
    if not (isinstance(link, str) and isinstance(parent, str) and isinstance(old, str) and isinstance(new, str)):
        trial.update(status="failed", error="the trial does not name its layers")
        save(state, trial)
        return "failed"
    now = pointed(link)
    if now not in (old, new):
        trial["status"] = "moved"
        save(state, trial)
        return "moved"
    done = {k: trial.get(k) for k in ("from", "to", "version", "previous")}
    done.update(why=why, ts=round(time.time()), layer_from=old, layer_to=new)
    put(path(state, "rolledback.json"), done)
    failed_path = os.path.join(parent, "failed.json")
    try:
        with open(failed_path) as f:
            failed = json.load(f)
    except (OSError, ValueError):
        failed = {}
    failed = failed if isinstance(failed, dict) else {}
    failed[new] = {"version": trial.get("version") or "", "at": round(time.time()), "why": why}
    put(failed_path, failed)
    nxt = os.path.join(parent, "next.json")
    try:
        with open(nxt) as f:
            left = json.load(f)
    except (OSError, ValueError):
        left = None
    if isinstance(left, dict) and left.get("name") == new:
        remove(nxt)
    if now == new:
        try:
            point(link, parent, old)
        except OSError as e:
            remove(path(state, "rolledback.json"))   # nothing was rolled back: no write-up
            trial.update(status="failed", error=repr(e)[-300:])
            save(state, trial)
            return "failed"
    trial["status"] = "rolled-back"
    save(state, trial)
    return "rolled-back"


def roll_tree(repo, state, trial, why):
    """Code back to the previous release: 'rolled-back', or 'moved' (the code is not on the trial's release any more —
    someone else changed it; do not touch). The write-up (installed, history, the owner's line) is the previous
    release's update process: update/rolledback.json. It is written BEFORE the reset: a write that does not get through
    raises OSError and leaves the code and the trial as they were (run() tries again on the next pass), and a reset that
    was done always has its write-up; a pass that finds the code already on `from` with this trial's write-up in place
    only closes the trial. A reset that did not happen leaves the write-up behind only if it cannot be removed: the
    previous release's update process drops a write-up whose code is not on `from`."""
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    ours = path(state, "rolledback.json")
    try:
        with open(ours) as f:
            noted = json.load(f).get("to")
    except (OSError, ValueError, AttributeError):
        noted = None
    # an earlier pass reset it, the trial is not closed (a note of another trial's is not this one's)
    reset = head == trial.get("from") and noted is not None and noted == trial.get("to")
    if not reset and head != trial.get("to"):
        trial["status"] = "moved"
        save(state, trial)
        return "moved"
    if not reset:
        done = {k: trial.get(k) for k in ("from", "to", "version", "previous")}
        done.update(why=why, ts=round(time.time()))
        put(ours, done)
        # edits of the code into its stash first (`git stash list`); if git cannot, the rollback still goes: the hub must
        # come back up
        sha = git(repo, "-c", "user.name=hub", "-c", "user.email=hub@localhost", "stash", "create").stdout.strip()
        if sha:
            git(repo, "stash", "store", "-m", f"hub update: before the rollback {head[:7]}", sha)
        r = git(repo, "reset", "-q", "--hard", str(trial.get("from")))
        if r.returncode:
            remove(ours)   # nothing was rolled back: no write-up
            trial.update(status="failed", error=r.stderr.strip()[-300:])
            save(state, trial)
            return "failed"
    trial["status"] = "rolled-back"
    save(state, trial)
    return "rolled-back"


def started(pid, k32=None):
    """When the process started, or None (no such process, not readable) — as hub-up writes it to hub-up.json
    ("started", platforms.current.process_start; a copy: this file imports nothing of the hub). Linux: clock ticks since
    boot (/proc/<pid>/stat, field 22); macOS: `ps -o lstart=` in the C locale; Windows: the creation FILETIME."""
    if type(pid) is not int or pid <= (4 if os.name == "nt" else 1):
        return None
    if os.name == "nt":
        k32 = k32 or _kernel32()
        handle = k32.OpenProcess(_QUERY, False, pid)
        if not handle:
            return None
        try:
            return _created(k32, handle)
        finally:
            k32.CloseHandle(handle)
    if os.path.isdir("/proc/self"):
        try:
            with open(f"/proc/{pid}/stat") as f:
                return int(f.read().rsplit(")", 1)[1].split()[19])
        except (OSError, ValueError, IndexError):
            return None
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=30,
                             env={**os.environ, "LC_ALL": "C"}).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out or None


def command(pid):
    """The process's command line (POSIX), or "" (no such process, no access)."""
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(int(pid))], capture_output=True, timeout=60,
                             stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.decode("utf-8", "replace").strip()


_QUERY, _TERMINATE = 0x1000, 0x0001   # PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_TERMINATE


def _kernel32():
    import ctypes
    from ctypes import wintypes
    k32, H = ctypes.WinDLL("kernel32", use_last_error=True), wintypes.HANDLE
    k32.OpenProcess.argtypes, k32.OpenProcess.restype = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD), H
    k32.GetProcessTimes.argtypes = (H, *(ctypes.POINTER(wintypes.FILETIME),) * 4)
    k32.GetProcessTimes.restype = wintypes.BOOL
    k32.TerminateProcess.argtypes, k32.TerminateProcess.restype = (H, wintypes.UINT), wintypes.BOOL
    k32.CloseHandle.argtypes, k32.CloseHandle.restype = (H,), wintypes.BOOL
    return k32


def _created(k32, handle):
    import ctypes
    from ctypes import wintypes
    times = [wintypes.FILETIME() for _ in range(4)]
    if not k32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
        return None
    return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime


def is_hub_up(pid, born):
    """The process under this number is the hub-up that wrote hub-up.json: its start time and, on POSIX (the time is to
    the second on macOS), hub-up in its command line; on Windows the time is to 100 ns (stop_nt)."""
    return born is not None and started(pid) == born and (os.name == "nt" or "hub-up" in command(pid))


def stop_nt(pid, born, k32=None):
    """Windows: stop hub-up through one handle whose creation time (to 100 ns) is the one hub-up wrote: while it is open
    the number cannot go to another process, so the time names the process (no command line: PowerShell takes seconds
    and fails on a machine with slow services). The process alone, no walk by parent numbers (the walk of 2026-11-03):
    the supervisor's job holds the rest of the hub (platforms/windows.run_hub_up waits for the core, closes the job)."""
    k32 = k32 or _kernel32()
    handle = k32.OpenProcess(_QUERY | _TERMINATE, False, pid)
    if not handle:
        return False
    try:
        return _created(k32, handle) == born and bool(k32.TerminateProcess(handle, 1))
    finally:
        k32.CloseHandle(handle)


def start_hub(trial, run=subprocess.run):
    """After a rollback no hub-up runs (the new one crashed at once): on Windows the scheduler task does not bring it
    up again — the watchdog runs the hub's task itself (the task the update named in the trial: "task"); a launchd or
    systemd service restarts hub-up by itself. -> whether the task was run."""
    task = trial.get("task")
    if not WINDOWS or not isinstance(task, str) or not task:
        return False
    try:
        return run(["schtasks", "/Run", "/TN", task], capture_output=True, timeout=60,
                   stdin=subprocess.DEVNULL).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def restart_hub(trial, sleep=time.sleep):
    """hub-up execs itself on the code now on disk when it reads the restart file; a hub-up that does not read it
    (hung, or crashing in a loop under the service) is stopped and the service brings it up again. Only the hub-up that
    wrote hub-up.json: the file outlives a crash, and by now its number may be another program's (the audit of
    2026-11-03) — the process must have the start time hub-up wrote ("started") and, on POSIX, hub-up in its command
    line; a file without the time (an earlier hub-up) cannot be checked, so nothing is stopped.
    -> taken | no-hub-up | not-hub-up | stopped | killed"""
    flag = trial.get("restart_flag")
    if flag:
        try:
            put(flag, {"parts": [], "why": "update watchdog"})   # not flag + ".tmp": that name is the core's own
        except OSError:
            flag = None   # a request that cannot be written is a hub-up that does not take it: stop it below
    if flag:
        for _ in range(TAKE_FLAG):
            if not os.path.exists(flag):
                return "taken"
            sleep(1)
    try:
        with open(trial.get("up_file") or "") as f:
            up = json.load(f)
        pid, born = up.get("pid"), up.get("started")
    except (OSError, ValueError, AttributeError):
        return "no-hub-up"
    if not alive(pid):
        return "no-hub-up"
    if born is None or pid == os.getpid():
        return "not-hub-up"
    if WINDOWS:
        return "killed" if stop_nt(pid, born) else "not-hub-up"
    if not is_hub_up(pid, born):
        return "not-hub-up"
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return "stopped"
    except OSError:
        return "not-hub-up"
    for _ in range(30):
        if not alive(pid):
            return "stopped"
        sleep(1)
    if not is_hub_up(pid, born):   # gone while we waited (the number may be someone else's), or no longer checkable
        return "stopped" if not alive(pid) else "not-hub-up"
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        return "stopped"
    return "killed"


def run(state, resume=False, now=time.time, sleep=time.sleep, about=ask_about):
    trial = load(state)
    if not trial or trial.get("status") != "waiting":
        return "done"
    trial["pid"] = os.getpid()
    save(state, trial)
    # resumed (a reboot, the update process restarted alone): the clock starts now, unless the previous core still is
    # the hub's core — the full restart has not happened yet (it waits for busy sessions)
    # a layer's switch that still waits for the person's restart (a background install): no clock before it is taken
    core_gone = now() if resume and not still_old_core(trial) and not switch_waits(trial) else None
    first = up_core(trial)
    began = waited = now()
    failures = 0   # passes in a row whose rollback raised OSError
    while True:
        trial = load(state)
        if not trial or trial.get("pid") != os.getpid():
            return "done"   # the trial was replaced (a newer update) or another watchdog took it
        # resumed after a reboot: hub-up.json may still name the core of the install until hub-up rewrites it, and its
        # pid may belong to another process now — the clock starts once hub-up names another core
        core = up_core(trial)   # before the look at the switch: a restart between the two must not become "first"
        if switch_waits(trial):
            # installed in the background, the person has not restarted yet: a core that restarts on the running
            # release is no reason to count (review: it took back a release that never ran); the clock and the give-up
            # start once hub-up has taken the switch. A reboot meanwhile: the launcher resumes the watchdog.
            if now() - waited > SWITCH_GIVE_UP:   # a switch nobody takes (an older hub-up): not a watchdog for ever
                # the trial stays open: the restart that takes the switch at last starts the update process, which
                # resumes the watchdog (audit: a "gave-up" here let a release switch in later with no trial)
                trial["pid"] = None
                save(state, trial)
                return "left"
            first, began = core, now()
            sleep(SWITCH_TICK)
            continue
        if core_gone is None and switch_dropped(trial):
            # the switch went without being taken (a return, a switch hub-up refused): nothing of the new release ran
            trial["status"] = "dropped"
            save(state, trial)
            return "dropped"
        page = about(trial.get("port") or 8787)
        if core_gone is None and (resume and not still_old_core(trial) or (
                restarted(trial, page, first) if trial.get("core_pid") is None else not alive(trial.get("core_pid")))):
            core_gone = now()
        got = decide(trial, page, core_gone, now())
        if got == "done":
            return "done"
        if got == "ok":
            trial["status"] = "ok"
            save(state, trial)
            return "ok"
        if got == "rollback":
            try:
                rolled = roll_layer(state, trial, "timeout") if isinstance(trial.get("layer"), dict) else roll_tree(
                    trial.get("repo"), state, trial, "timeout")
            except OSError as e:   # a write that never got through: what it left is whole, the next pass starts over
                failures += 1
                if failures == 1:
                    print(f"trial: rollback failed, trying again: {e!r}", flush=True)
                if failures >= TAKE_FLAG:   # not for ever: a repo folder that is gone or no git does not pass by itself
                    print(f"trial: giving up the rollback: {e!r}", flush=True)
                    # roll_tree marks the trial before its closing save: the reset was done and only the save keeps
                    # failing — the code is on the previous release, so the hub is still asked to come up on it
                    done = trial.get("status") == "rolled-back"
                    if not done:
                        trial.update(status="failed", error=repr(e)[-300:])
                    try:
                        save(state, trial)
                    except OSError:
                        pass   # the trial stays "waiting": the next start of the hub gives it a watchdog again
                    if done:
                        restart_hub(trial, sleep=sleep)
                    return "rolled-back" if done else "failed"
                sleep(TICK)
                continue
            if rolled == "rolled-back":
                got = restart_hub(trial, sleep=sleep)
                if got == "not-hub-up":
                    print("trial: the pid in hub-up.json is not the hub-up that wrote it, or the file has no start "
                          "time: not stopped", flush=True)
                elif got == "no-hub-up":
                    start_hub(trial)
                return "rolled-back"
            return "done"
        if core_gone is None and now() - began > GIVE_UP:
            trial["status"] = "gave-up"
            save(state, trial)
            return "gave-up"
        sleep(TICK)


if __name__ == "__main__":
    print(f"trial: {run(sys.argv[1], resume='--resume' in sys.argv[2:])}", flush=True)
