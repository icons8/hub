"""Lock for a full test run: by default one per machine — shared by tools/test.py and the update self-check
(modules/update) (08.10, 09.10: two full runs at once, each with its own headless Chrome, ran out of memory). Sessions
have different temp folders, so the file is in the home folder.

Optional, only for your own machine: `test-lock.json` in the settings folder — {"slots": N, "min_free_mb": M,
"max_load": L} allows up to N runs at once. The first slot is taken as always; the next one only while free memory is
above M and the load (one-minute load average) is below L, no more often than once per SETTLE seconds: a run that has
just started has not taken its memory yet. No file or a broken one — one lock, as before.

Waiters line up in a fair queue (16.10: polling every 10 s, and one who came an hour earlier lost the race to newcomers):
a ticket is a file `<number>-<pid>-<random>` in `<lock>.queue/`, the number is in order of arrival; a ticket holds its
own lock while the owner waits. Only the earliest live ticket tries to take a slot; the ticket of a dead one (no such
process, or nobody holds the lock — the pid was taken by another process) is removed by whoever sees it. The pid is in
the name, not in the file: on Windows a locked file cannot be read by others."""
import json
import os
import secrets
import shutil
import subprocess
import time

import platforms
from core import lock

PATH = os.path.join(os.path.expanduser("~"), ".cache", "agents-hub", "full-test-run.lock")
SETTINGS = "test-lock.json"
SETTLE = 60
_sleep = time.sleep   # own reference: a test replaces it, not time.sleep of the whole process (other threads sleep there too)
_UNSET = object()


class Busy(RuntimeError):
    """Did not wait for the lock: someone else's full run is going."""


def settings(config_dir=None):
    """Slot settings or None (off). A broken or incomplete file is also off: the safe side."""
    if config_dir is None:
        from core import catalog
        config_dir = catalog.config_dir()
    try:
        with open(os.path.join(config_dir, SETTINGS)) as f:
            d = json.load(f)
        got = {"slots": int(d["slots"]), "min_free_mb": float(d["min_free_mb"]), "max_load": float(d["max_load"])}
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return got if got["slots"] >= 2 else None


def free_mb():
    """How much memory can be taken, MB: MemAvailable on Linux, free+inactive+speculative on macOS; unknown — None."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        pass
    if shutil.which("vm_stat"):
        try:
            out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=10).stdout
            size = int(out.split("page size of")[1].split()[0])
            pages = {}
            for line in out.splitlines():
                name, _, num = line.partition(":")
                num = num.strip().rstrip(".")
                if num.isdigit():
                    pages[name.strip()] = int(num)
            return sum(pages.get(k, 0) for k in ("Pages free", "Pages inactive", "Pages speculative")) * size / 1048576
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            pass
    return None


def load_now():
    try:
        return os.getloadavg()[0]
    except (AttributeError, OSError):   # Windows: the load is unknown
        return None


def probe():
    return free_mb(), load_now()


def _slots(path, cfg):
    return [path] + [f"{path}.{i}" for i in range(1, cfg["slots"])] if cfg else [path]


def _extra(path, cfg, probe, settle):
    """The next slot beyond the first — or None: no resources, settle has not passed, or all are taken."""
    gate = lock.acquire(path + ".gate")   # decided one at a time: two with the same numbers must not take a slot each
    if not gate:
        return None
    try:
        stamp = path + ".grant"
        try:
            if time.time() - os.stat(stamp).st_mtime < settle:
                return None
        except OSError:
            pass
        mb, busy = probe()
        if mb is None or busy is None or mb <= cfg["min_free_mb"] or busy >= cfg["max_load"]:
            return None
        for p in _slots(path, cfg)[1:]:
            f = lock.acquire(p)
            if f:
                with open(stamp, "a"):
                    os.utime(stamp)
                return f
        return None
    finally:
        gate.close()


def free(path=None, config=_UNSET):
    """Whether the lock is free now (take and release at once): a retry of a deferred install does nothing extra while
    it is busy. With several slots — at least one is free."""
    cfg = _config(path, config)
    path = path or PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    for p in _slots(path, cfg):
        f = lock.acquire(p)
        if f:
            f.close()
            return True
    return False


def _config(path, config):
    if config is not _UNSET:
        return config
    return settings() if path is None else None   # own path — one lock: tests and calls do not read the machine's settings


def _dead(path, name):
    """Ticket of one who left: no such process, or nobody holds the ticket's lock."""
    try:
        if not platforms.current.alive(int(name.split("-")[1])):
            return True
    except (IndexError, ValueError):
        return True
    f = lock.acquire(path)
    if f:
        f.close()
        return True
    return False


def _remove(path):
    try:
        os.remove(path)
    except OSError:   # Windows: the file is still open by someone — the next one will remove it
        pass


def _ahead(queue, mine):
    """How many live tickets are before mine; remove the dead ones."""
    n = 0
    for name in sorted(os.listdir(queue)):
        if name >= mine:
            break
        p = os.path.join(queue, name)
        if _dead(p, name):
            _remove(p)
        else:
            n += 1
    return n


def _enter(queue):
    """Own ticket: an open file with a lock, and its name. The number is greater than all standing ones (the coarse
    Windows clock gives equal stamps), issued under the queue lock."""
    with lock.exclusive(queue + ".lock"):
        last = max((int(n.split("-")[0]) for n in os.listdir(queue) if n.split("-")[0].isdigit()), default=0)
        name = f"{max(time.time_ns(), last + 1):020d}-{os.getpid()}-{secrets.token_hex(4)}"
        f = open(os.path.join(queue, name), "a+")
        platforms.current.try_lock(f)
        return f, name


def wait_turn(path=None, patience=None, every=10, sleep=None, waiting=None, config=_UNSET, probe=None, settle=None):
    """The open lock file — hold it until the end of the run. patience — how many seconds to wait (None — as long as
    needed), not waited out — Busy; waiting(n) — when we have to wait and when n changes, the number of runs in the
    queue ahead of us."""
    cfg = _config(path, config)
    path, sleep = path or PATH, sleep or _sleep
    probe = probe or globals()["probe"]
    settle = SETTLE if settle is None else settle
    queue = path + ".queue"
    os.makedirs(queue, exist_ok=True)
    ticket, mine = _enter(queue)
    try:
        waited, said = 0, None
        while True:
            with lock.exclusive(queue + ".lock"):
                ahead = _ahead(queue, mine)
            if not ahead:
                f = lock.acquire(path)
                if not f and cfg:
                    f = _extra(path, cfg, probe, settle)
                if f:
                    return f
            if patience is not None and waited >= patience:
                raise Busy("another full test run is going on this machine — the run lock is taken")
            if waiting and ahead != said:
                waiting(ahead)
                said = ahead
            sleep(every)
            waited += every
    finally:
        ticket.close()   # Windows: an open file cannot be deleted
        _remove(os.path.join(queue, mine))
