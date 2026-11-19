"""In-memory temp folder for test runs (17.10: the server wrote ~800 GiB to disk per day, most of it test runs of
tools/test.py: journal copies, test stands, Chrome profiles in the temp folder, which is on disk for sessions).

Linux: if /dev/shm is tmpfs and has at least PER_RUN_MB free for each run-lock slot (core/testlock), tools/test.py
gives the chunks TMPDIR in `/dev/shm/hub-tests-<uid>/<pid>/`. The `hub-tests-<uid>` folder must be our own (owned by
us, mode 0700, not a link): /dev/shm is shared by everyone, and a planted folder or link is refused, and the run goes
as before. A run directory holds a lock on the `lock` file while tools/test.py is alive; a directory with no holder is
an orphan of a killed run: its Chrome is stopped and the directory is deleted. macOS and Windows — unchanged (None)."""
import os
import shutil
import stat
import time

from core import lock

ROOT = "/dev/shm"
PER_RUN_MB = 3072          # per one full run, with a margin
LOCK = "lock"
YOUNG = 60                 # s: a directory without a lock file younger than this is still being created, leave it


def _tmpfs(path, mounts="/proc/mounts"):
    try:
        with open(mounts) as f:
            return any(p[1] == path and p[2] == "tmpfs" for p in (line.split() for line in f) if len(p) > 2)
    except OSError:
        return False


def _free_mb(path):
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize / 1048576


def top(root=ROOT):
    return os.path.join(root, f"hub-tests-{os.getuid()}")


def _mine(path):
    """The folder is ours, not a link and closed to others."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and not st.st_mode & 0o077


def _own_dir(path):
    """Own folder: create it 0700 or accept an existing one only if it is _mine."""
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    except OSError:
        return False
    return _mine(path)


def make(slots=1, root=ROOT, mounts="/proc/mounts", free_mb=_free_mb):
    """(run directory, open lock file) or None — no memory, the folder is not ours, or there is no /proc/mounts with
    tmpfs on root (macOS, Windows)."""
    if not _tmpfs(root, mounts):
        return None
    try:
        if free_mb(root) < PER_RUN_MB * max(1, slots):
            return None
    except OSError:
        return None
    home = top(root)
    if not _own_dir(home):
        return None
    sweep(home)
    try:
        # the name is short: Chrome puts SingletonSocket in TMPDIR/hub-core-tests-run-*/com.google.Chrome.*/, and a
        # Unix socket path is at most 107 bytes (with "<pid>-<random>" it came to 109 — Chrome did not start); the
        # directory of a previous process with this pid was already removed by sweep above — nobody holds its lock
        run = os.path.join(home, str(os.getpid()))
        os.mkdir(run, 0o700)
        held = lock.acquire(os.path.join(run, LOCK))
    except OSError:
        return None
    if not held:   # cannot happen: the directory is just ours
        return None
    return run, held


def _stop_chromes(run):
    try:
        from modules.testchrome import litter
        litter.stop_run_chromes(run)
    except Exception:  # noqa: BLE001 — cleanup must not get in the way of the run
        pass


def sweep(home, now=None):
    """Directories of finished runs (nobody holds the lock, or there is no lock file): stop their Chrome and delete
    the directory itself if it is not new — one younger than YOUNG may be just created, still without a lock. Two
    sweepers at once: a directory already removed by the other — skip."""
    now = time.time() if now is None else now
    if not _mine(home):   # update calls sweep without make(): /dev/shm is shared, a planted folder or link is not ours
        return
    try:
        names = os.listdir(home)
    except OSError:
        return
    for name in names:
        run = os.path.join(home, name)
        path = os.path.join(run, LOCK)
        try:
            if not os.path.isdir(run) or os.path.islink(run):
                continue
            if os.path.exists(path):
                f = lock.acquire(path)
                if not f:
                    continue   # the run is alive
                f.close()
            young = now - os.path.getmtime(run) < YOUNG
        except OSError:
            continue
        _stop_chromes(run)   # a just-created one has no Chrome yet — nothing to hit
        if not young:
            shutil.rmtree(run, ignore_errors=True)


def remove(run, held):
    """Own directory after the run: the chunks' Chrome that outlived them (they have already exited) and all contents."""
    _stop_chromes(run)
    held.close()
    shutil.rmtree(run, ignore_errors=True)
