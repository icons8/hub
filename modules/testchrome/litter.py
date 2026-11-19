"""Chrome and test litter in the system temp folder. Chrome puts a com.google.Chrome.* directory with a SingletonSocket and
empty BlobRegistryFiles* into the system temp folder, bypassing TMPDIR, and does not remove them if it was killed (06.10: ~4700 and
~870 directories, 800 a day, from page tests, `hub shot`, `hub video`, mermaid diagrams for Telegram).

  own(profile)   Chrome with this profile is closed: remove its com.google.Chrome.* directory (the
                 SingletonSocket in the profile points at it)
  sweep(base)    abandoned for over an hour: a com.google.Chrome.* directory with a dead SingletonSocket (connect refuses
                 or there is no file) made when no live Chrome started;
                 empty network request directories (url_fetcher), the request finished long ago; empty BlobRegistryFiles*,
                 except those created in the first START_GAP seconds of a still-live Chrome's life (its directory, Chrome creates it at
                 start): do not touch a live Chrome of your own (yours, an agent's)
  sweep_runs(base)  test run directories (tests/__init__.py) older than an hour with no live process
  stop_run_chromes(base)  kill the main Chrome whose profile is in a test run with no live process (or already
                 removed): a run killed by a timeout left it living for days (09.10); likewise the stub
                 tests/fakes/fake_chrome.py (it runs under python, lived outside sweep for 4 days, 23.10)
  stop_proof_chromes(base)  the same for the Chrome of `hub shot`, `hub video` and scripts on modules/proof/cdp.Page (profile
                 hub-proof-*, with the owner's pid in it): a script that crashed or was killed before close() left it living (12.10)
Only what is surely ours: a directory of this account, not a link, with the exact mkdtemp name; a process of this account
whose command has not changed by the time of the signal (03.11: the test cleaners hit someone else's live Chrome and folder).
"""
import glob
import os
import re
import shutil
import socket
import stat
import subprocess
import time

AGE = 3600
START_GAP = 120
CHROME = {"Google Chrome", "Google Chrome for Testing", "Google Chrome Beta", "Google Chrome Dev", "Google Chrome Canary",
          "Chromium", "chrome", "chromium", "chrome-headless-shell",
          "headless_shell",   # old Playwright layout (chrome.SHELL_PATTERNS)
          "google-chrome", "google-chrome-stable", "google-chrome-beta", "google-chrome-unstable", "chromium-browser"}   # Linux: a launcher script with `exec -a "$0"`


def own(profile):
    """The com.google.Chrome.* directory of a closed Chrome with the given profile: remove it."""
    try:
        sock = os.readlink(os.path.join(profile, "SingletonSocket"))
    except OSError:
        return
    d = os.path.dirname(sock)
    if os.path.basename(d).startswith("com.google.Chrome."):
        shutil.rmtree(d, ignore_errors=True)


def _dead(path):
    """A socket nobody listens on (or not a socket at all, or gone). A timeout, EAGAIN and the rest are not proof: a live
    Chrome that has not accepted for a while (hung, stopped) has a full queue, and Linux answers EAGAIN (03.11: its
    directory was deleted, the same profile then could not start)."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(1)
    try:
        s.connect(path)
        return False
    except (ConnectionRefusedError, FileNotFoundError):
        return True
    except OSError:
        return False
    finally:
        s.close()


def _ours(path):
    """A directory of this account, not a link (lstat; on Windows lstat calls a junction a directory)."""
    st = os.lstat(path)
    junction = getattr(st, "st_reparse_tag", 0) == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", -1)
    return stat.S_ISDIR(st.st_mode) and not junction and (not hasattr(os, "getuid") or st.st_uid == os.getuid())


def _mark(run):
    """The pid in the run's mark; None if the mark is empty (cut short: killed while writing, disk full). No mark, an
    unreadable one or not a pid: OSError / ValueError — not known to be ours."""
    with open(os.path.join(run, "pid"), "rb") as f:
        text = f.read().strip()
    if not text:
        return None
    pid = int(text)
    if not 1 < pid < 2 ** 31:   # os.kill takes a C int: a bigger one is OverflowError, not a process
        raise ValueError(text)
    return pid


def _made_at_start(mtime, starts):
    """A live Chrome started when this directory was made (it creates it at start), or live Chromes are unknown."""
    return starts is None or any(0 <= mtime - s <= START_GAP for s in starts)


def _elapsed(text):
    """etime from ps: [[dd-]hh:]mm:ss -> seconds."""
    days, _, rest = text.rpartition("-")
    parts = [int(x) for x in rest.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return int(days or 0) * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]


def _ps():
    return subprocess.run(["ps", "-axo", "etime=,command="], capture_output=True, text=True, timeout=10).stdout


def chrome_starts(now=None, ps=_ps):
    """When the live Chromes were started (main processes: program name is Chrome, no --type=); if unknown, None."""
    now = time.time() if now is None else now
    try:
        out = ps()
    except (OSError, subprocess.SubprocessError):
        return None
    starts = []
    for line in out.splitlines():
        etime, _, cmd = line.strip().partition(" ")
        exe = cmd.split(" --", 1)[0].strip()
        if "--type=" in cmd or os.path.basename(exe) not in CHROME:
            continue
        try:
            starts.append(now - _elapsed(etime))
        except ValueError:
            continue
    return starts


def sweep(base, now=None, starts=None):
    now = time.time() if now is None else now
    if starts is None:
        starts = chrome_starts(now)
    for d in glob.glob(os.path.join(base, "com.google.Chrome.*")) + glob.glob(os.path.join(base, "BlobRegistryFiles*")):
        try:
            mtime = os.path.getmtime(d)
            if now - mtime < AGE or not _ours(d):
                continue
            sock = os.path.join(d, "SingletonSocket")
            if os.path.lexists(sock):
                # macOS refuses a connect to a full queue just like one to a dead socket: refused is not enough, the
                # directory of a Chrome that is still alive (made at its start) stays
                if not _made_at_start(mtime, starts) and _dead(sock):   # a live one's socket is not even probed
                    shutil.rmtree(d, ignore_errors=True)
            elif os.listdir(d):
                continue
            elif "url_fetcher" in os.path.basename(d):
                os.rmdir(d)
            elif not _made_at_start(mtime, starts):
                os.rmdir(d)
        except OSError:
            continue


def _alive(pid):
    import platforms   # not os.kill(pid, 0): on Windows that sends Ctrl+C to the whole console (08.10: it brought the hub down during the self-check)
    if not platforms.current.alive(pid):
        return False
    if os.name == "nt":
        return True
    # a zombie is dead (signal 0 finds it): right after the self-check timeout the workers of the killed run are not yet
    # reaped, and its Chrome stayed alive (PR review)
    try:
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(int(pid))], capture_output=True, text=True,
                               timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return True
    return not state.startswith("Z")


def _ps_pids():
    return subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=10).stdout


def _ps_one(pid):
    """(uid, command) of the process now, None if it is gone: a second look right before the signal."""
    try:
        out = subprocess.run(["ps", "-o", "uid=,command=", "-p", str(int(pid))], capture_output=True, text=True,
                             timeout=10).stdout.strip()
        uid, _, cmd = out.partition(" ")
        return int(uid), cmd.strip()
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _term(pid):
    import signal
    import platforms
    if platforms.real_pid(pid):
        os.kill(pid, signal.SIGTERM)


# the exact mkdtemp name (tests/__init__.py, modules/proof/cdp.py): prefix and 8 characters of tempfile's alphabet
MKDTEMP = r"[a-z0-9_]{8}"
RUN_NAME = re.compile(r"hub-core-tests-run-" + MKDTEMP + r"\Z")
RUN_PROFILE = re.compile(r"--user-data-dir=(\S*[/\\]hub-core-tests-run-" + MKDTEMP + r")(?=[/\\\s]|\Z)")
PROOF_PROFILE = re.compile(r"--user-data-dir=(\S*[/\\]hub-proof-" + MKDTEMP + r")(?=[/\\\s]|\Z)")


def _run_over(run, now):
    """The run is gone or finished: the directory is removed, the process from the pid mark is dead, or the mark is cut
    short and the run is older than AGE. With no mark or an unreadable one it is not known to be ours: not over."""
    try:
        if not _ours(run):   # a link, another account's folder
            return False
        pid = _mark(run)
    except FileNotFoundError:
        return not os.path.lexists(run)   # the directory is removed: over; there without a mark: not known to be ours
    except (OSError, ValueError):
        return False   # the mark is unreadable or not a pid: nothing is known — not over
    if pid is not None:
        return not _alive(pid)
    try:   # the mark cut short: ours, over once old
        return now - os.path.getmtime(run) >= AGE
    except OSError:
        return True


def stop_run_chromes(base, now=None, ps=_ps_pids, kill=_term):
    _stop_chromes(base, RUN_PROFILE, now, ps, kill)


def stop_proof_chromes(base, now=None, ps=_ps_pids, kill=_term):
    _stop_chromes(base, PROOF_PROFILE, now, ps, kill)


def _is_fake(exe):
    """The tests/fakes/fake_chrome.py stub: launched under an interpreter, so the program name is python, not Chrome."""
    return os.path.basename(exe.rpartition(" ")[2]) == "fake_chrome.py"


def _stop_chromes(base, profile, now, ps, kill):
    """The main Chrome whose profile (by pattern: a run directory or the profile itself) lies in base and whose owner has finished."""
    now = time.time() if now is None else now
    home = os.path.realpath(base)
    me = os.getuid() if hasattr(os, "getuid") else None
    try:
        out = ps()
    except (OSError, subprocess.SubprocessError):
        return
    for line in out.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        exe = cmd.split(" --", 1)[0].strip()
        m = profile.search(cmd)
        if not m or "--type=" in cmd or not (os.path.basename(exe) in CHROME or _is_fake(exe)):
            continue
        run = m[1]
        if os.path.realpath(os.path.dirname(run)) != home or not _run_over(run, now):
            continue
        # the list is all accounts' and the checks above took several ps calls: the same process of this account still
        if _ps_one(pid) != (me, cmd.strip()):
            continue
        try:
            kill(int(pid))
        except (OSError, ValueError):
            continue


def sweep_runs(base, now=None):
    import platforms
    now = time.time() if now is None else now
    for d in glob.glob(os.path.join(base, "hub-core-tests-run-*")):
        try:
            if not RUN_NAME.match(os.path.basename(d)) or not _ours(d) or now - os.path.getmtime(d) < AGE:
                continue
            try:   # our run writes its pid mark right after mkdtemp: without a readable mark it is not known to be ours
                pid = _mark(d)
            except (OSError, ValueError):
                continue
            if pid is not None and _alive(pid):   # None: the mark cut short — ours, and old
                continue
            platforms.current.rmtree(d)   # not shutil: it leaves read-only files (.git/objects) forever
        except OSError:
            continue
