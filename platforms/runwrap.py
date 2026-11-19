"""The wrapper of one `hub run` (core/hubrun.py), the OS side of it: runs a session's command outside the session and the hub, up to its
deadline, and leaves the outcome in its folder for the core to deliver.

The core copies this file into the run's folder and starts the copy, apart from the hub (core/runjobs.py): an update,
a code layer swap or a removed old layer do not touch a running wrapper. Hence the standard library only and every
import at the top: nothing is read from disk after the start.

The run's folder (<state>/runs/<id>/), in the order of the wrapper's life:
  alive.lock        held for the whole life (flock / msvcrt.locking): "the wrapper lives" without a process number —
                    the lock goes with the process, even a killed one; the command does not inherit it
  spec.json         written by the core; the wrapper takes it by rename to spec.taken.json — after it holds the lock,
                    so a taken run with a free lock means a dead wrapper. The core cancels by renaming it to
                    spec.cancelled.json: one rename wins, a cancelled run never starts. Either keeps no command and no
                    environment once read (forget_command): the core reads only how the run was started
  pid.json          {"pid": wrapper, "pgid": the command's group} right after the start
  out.log           the output (stdout and stderr) through a pipe; over PART bytes it moves to out.1.log
  stop              the core's request to stop ({"by": who}); checked every second
  result.json       {code, reason, by, started_at, ended_at} — only once the command and all it started are gone

The deadline: the wall clock at until_at, or the run's length on a clock that counts the machine's sleep (a wall clock
set back does not stretch it). At the deadline or a stop: SIGTERM to the group, SIGKILL after GRACE seconds (Windows:
the job). Linux under systemd: also everyone left in the run's own cgroup (a descendant in a session of its own)."""
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time

WIN = os.name == "nt"
if WIN:
    import ctypes
    import msvcrt
    from ctypes import wintypes
else:
    try:
        import fcntl
    except ImportError:   # a Python without it (the core is imported to check this): the wrapper itself needs it
        fcntl = None

PART = 64 * 1024 * 1024        # bytes of output in out.log before it moves to out.1.log: at most two parts per run
FREE_MIN = 1024 * 1024 * 1024  # below this free on the disk the output is dropped (the hub's journal lives there too)
GRACE = 10                     # seconds between SIGTERM and SIGKILL of the group
TICK = 1                       # seconds between looks at the command, the stop request and the deadline
DISK_EVERY = 30                # seconds between looks at the free disk space
RESULT_TRIES = 20              # result.json that cannot be written (a full disk): tries, RESULT_PAUSE apart
RESULT_PAUSE = 30
LOCK, SPEC, TAKEN, CANCELLED = "alive.lock", "spec.json", "spec.taken.json", "spec.cancelled.json"
PIDS, LOG, OLD_LOG, STOP, RESULT = "pid.json", "out.log", "out.1.log", "stop", "result.json"
DROPPED = "[hub run: earlier output dropped, the previous {mb} MB are in out.1.log]\n"
NO_SPACE = "[hub run: less than {gb} GB free on the disk — the output is no longer written]\n"


# ---- the lock: the wrapper's life -----------------------------------------------------------------------------------

def hold(folder):
    """Take the life lock -> its open file (keep it open), or None when someone holds it."""
    f = open(os.path.join(folder, LOCK), "a+b")
    try:
        if WIN:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except OSError:
        f.close()
        return None


def alive(folder):
    """Whether a wrapper holds the run's lock (for the core): a free lock is a dead wrapper or one not started yet."""
    if not os.path.exists(os.path.join(folder, LOCK)):
        return False
    f = hold(folder)
    if f is None:
        return True
    try:
        if WIN:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        f.close()
    return False


# ---- clocks ---------------------------------------------------------------------------------------------------------

def awake_clock():
    """Seconds on a clock that keeps counting while the machine sleeps: Linux CLOCK_BOOTTIME, macOS CLOCK_MONOTONIC (it
    counts sleep there, unlike time.monotonic), Windows GetTickCount64."""
    if hasattr(time, "CLOCK_BOOTTIME"):
        return lambda: time.clock_gettime(time.CLOCK_BOOTTIME)
    if sys.platform == "darwin":
        return lambda: time.clock_gettime(time.CLOCK_MONOTONIC)
    if WIN:
        k32 = ctypes.WinDLL("kernel32")
        k32.GetTickCount64.restype = ctypes.c_uint64
        return lambda: k32.GetTickCount64() / 1000
    return time.monotonic


# ---- files ----------------------------------------------------------------------------------------------------------

def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def forget_command(path, spec):
    """A taken or cancelled spec rewritten without the command and its environment: the folder stays days after the end
    (core/runjobs.KEEP_DAYS), a token in the command or a key in the environment with it; the core reads only how the
    run was started (how, unit). A failure (Windows: the core reads the file at that moment) leaves it as it was."""
    if not spec:
        return
    try:
        write_json(path, {k: v for k, v in spec.items() if k not in ("argv", "env")})
    except OSError:
        pass


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


class Log:
    """The command's output from the pipe into out.log: at most two parts of PART bytes, nothing written while the disk
    has less than FREE_MIN free (the pipe is still read: a command must not stand on a full pipe)."""

    def __init__(self, folder, part=PART, free_min=FREE_MIN, clock=time.monotonic):
        self.folder, self.part, self.free_min, self.clock = folder, part, free_min, clock
        self.path = os.path.join(folder, LOG)
        self.f = open(self.path, "ab")
        self.size, self.checked, self.full = self.f.tell(), None, False

    def write(self, chunk):
        now = self.clock()
        if self.checked is None or now - self.checked >= DISK_EVERY:
            self.checked = now
            try:
                low = shutil.disk_usage(self.folder).free < self.free_min
            except OSError:
                low = False
            if low and not self.full:
                self._raw(NO_SPACE.format(gb=self.free_min // (1024 ** 3) or 1).encode())
            self.full = low
        if self.full:
            return
        if self.size + len(chunk) > self.part:
            try:
                self.f.close()
                os.replace(self.path, os.path.join(self.folder, OLD_LOG))
                self.f = open(self.path, "wb")
                self.size = 0
                self._raw(DROPPED.format(mb=self.part // (1024 * 1024) or 1).encode())
            except (OSError, ValueError):   # a full disk, a reader holding the file (Windows): keep the pipe read
                try:   # the part cannot move aside: it starts again in place, the cap still holds
                    self.f = open(self.path, "wb")
                    self.size = 0
                    self._raw(DROPPED.replace(", the previous {mb} MB are in out.1.log", "").encode())
                except OSError:
                    self.full = True   # nowhere to write: the output is dropped until the next look at the disk
                    return
        self._raw(chunk)

    def _raw(self, data):
        try:
            self.f.write(data)
            self.f.flush()
            self.size += len(data)
        except (OSError, ValueError):
            pass   # a full disk, or closed under a late writer (a detached descendant): the output is lost, the run goes on

    def close(self):
        try:
            self.f.close()
        except OSError:
            pass


def pump(stream, log):
    """Copy the pipe into the log until every holder of its write end is gone."""
    fd = stream.fileno()
    while True:
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        log.write(chunk)


# ---- the group: POSIX process group (+ the systemd cgroup), Windows job --------------------------------------------

if WIN:
    class _Basic(ctypes.Structure):   # JOBOBJECT_BASIC_LIMIT_INFORMATION
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _Extended(ctypes.Structure):   # JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        _fields_ = [("BasicLimitInformation", _Basic), ("IoInfo", ctypes.c_uint64 * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    class _Pids(ctypes.Structure):   # JOBOBJECT_BASIC_PROCESS_ID_LIST with room for 1024 processes
        _fields_ = [("Assigned", wintypes.DWORD), ("Listed", wintypes.DWORD), ("Ids", ctypes.c_size_t * 1024)]


class Group:
    """What the command started, as one thing to look at and to stop."""

    def __init__(self):
        self.job = None
        if WIN:   # the wrapper's own job, KILL_ON_JOB_CLOSE: the wrapper killed — the job closes, the command dies
            k32 = self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            k32.OpenProcess.restype = wintypes.HANDLE
            job = k32.CreateJobObjectW(None, None)
            info = _Extended()
            info.BasicLimitInformation.LimitFlags = 0x2000   # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if job and k32.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info)) \
                    and k32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(k32.GetCurrentProcess())):
                self.job = job
        self.cgroup = None if WIN else own_cgroup()

    def spawn(self, argv, cwd, env, umask):
        kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "cwd": cwd,
              "env": env}
        if WIN:
            kw["creationflags"] = 0x08000000   # CREATE_NO_WINDOW
        else:
            kw["start_new_session"] = True
            if isinstance(umask, int) and umask >= 0:
                kw["umask"] = umask
        self.proc = subprocess.Popen(argv, **kw)
        return self.proc

    def others(self):
        """Live processes of the group other than the wrapper itself (POSIX: the process group, and under systemd the
        rest of the run's cgroup)."""
        if WIN:
            if not self.job:
                return [self.proc.pid] if self.proc.poll() is None else []
            pids = _Pids()
            if not self.k32.QueryInformationJobObject(wintypes.HANDLE(self.job), 3, ctypes.byref(pids),
                                                      ctypes.sizeof(pids), None):
                return [self.proc.pid] if self.proc.poll() is None else []
            return [int(p) for p in pids.Ids[:pids.Listed] if int(p) != os.getpid()]
        out = []
        try:
            os.killpg(self.proc.pid, 0)
            out.append(-self.proc.pid)   # the group as a whole
        except ProcessLookupError:
            pass
        except PermissionError:
            out.append(-self.proc.pid)
        for pid in cgroup_procs(self.cgroup):
            if pid != os.getpid():
                out.append(pid)
        return out

    def signal(self, hard):
        if WIN:
            for pid in self.others():
                h = self.k32.OpenProcess(0x0001, False, pid)   # PROCESS_TERMINATE
                if h:
                    self.k32.TerminateProcess(wintypes.HANDLE(h), 1)
                    self.k32.CloseHandle(wintypes.HANDLE(h))
            return
        sig = signal.SIGKILL if hard else signal.SIGTERM
        for pid in self.others():
            try:
                if pid < 0:
                    os.killpg(-pid, sig)
                else:
                    os.kill(pid, sig)
            except OSError:
                pass

    def gone(self):
        self.proc.poll()
        return not self.others()


def own_cgroup():
    """The run's own cgroup when it runs as a systemd unit of its own (hub-run-…): its cgroup.procs, else None."""
    try:
        with open("/proc/self/cgroup") as f:
            line = next((x for x in f.read().splitlines() if x.startswith("0::")), "")
    except OSError:
        return None
    path = line[3:]
    if "/hub-run-" not in path:
        return None
    procs = os.path.join("/sys/fs/cgroup", path.lstrip("/"), "cgroup.procs")
    return procs if os.path.exists(procs) else None


def cgroup_procs(procs):
    if not procs:
        return []
    try:
        with open(procs) as f:
            return [int(x) for x in f.read().split()]
    except (OSError, ValueError):
        return []


# ---- the run ------------------------------------------------------------------------------------------------------

def due(wall, awake, until, began, length):
    """The deadline came: the wall clock reached until_at, or the run's length passed on the awake clock (a wall clock
    set back does not stretch the run)."""
    return wall >= until or awake - began >= length


class Run:
    def __init__(self, folder, wall=time.time, awake=None, sleep=time.sleep):
        self.folder, self.wall, self.sleep = folder, wall, sleep
        self.awake = awake or awake_clock()
        self.asked = None   # a stop by a signal to the wrapper: who
        self.grace = GRACE

    def path(self, name):
        return os.path.join(self.folder, name)

    def main(self):
        lock = hold(self.folder)
        if lock is None:
            return 3   # another wrapper holds this run
        try:
            os.rename(self.path(SPEC), self.path(TAKEN))
        except OSError:
            return 0   # cancelled by the core (spec.cancelled.json) or already taken: nothing to run
        spec = read_json(self.path(TAKEN)) or {}
        forget_command(self.path(TAKEN), spec)   # read once, here: nobody runs it again
        started, began = self.wall(), self.awake()
        until = spec.get("until_at") if isinstance(spec.get("until_at"), (int, float)) else started
        length = max(0, until - started)
        if isinstance(spec.get("grace"), (int, float)) and spec["grace"] >= 0:
            self.grace = spec["grace"]
        group = Group()
        log = Log(self.folder)
        try:
            proc = group.spawn(spec["argv"], spec.get("cwd") or None, spec.get("env") or None, spec.get("umask"))
        except (OSError, KeyError, TypeError, ValueError) as e:
            log._raw(f"[hub run: the command did not start: {e}]\n".encode())
            log.close()
            self.result({"code": None, "reason": "not_started", "error": str(e)[:300], "started_at": started})
            return 0
        write_json(self.path(PIDS), {"pid": os.getpid(), "pgid": proc.pid})
        reader = threading.Thread(target=pump, args=(proc.stdout, log), daemon=True)
        reader.start()
        if not WIN:
            for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                signal.signal(s, lambda *_: setattr(self, "asked", "signal"))
        reason, by = "exit", None
        while proc.poll() is None:
            stop = read_json(self.path(STOP)) if os.path.exists(self.path(STOP)) else None
            if stop is not None or self.asked:
                reason, by = "stopped", (stop or {}).get("by") or self.asked
                break
            if due(self.wall(), self.awake(), until, began, length):
                reason = "timeout"
                break
            self.sleep(TICK)
        code = proc.poll()
        self.finish(group)   # the rest of the group too: a run is the command and everything it started
        code = proc.returncode if code is None else code
        reader.join(5)
        log.close()
        forget_command(self.path(TAKEN), spec)   # again: the first rewrite may have failed (Windows: the core read it)
        self.result({"code": code, "reason": reason, **({"by": by} if by else {}), "started_at": started})
        return 0

    def finish(self, group):
        """Stop what is left of the group and wait until it is gone: SIGTERM, SIGKILL after GRACE."""
        if group.gone():
            return
        group.signal(hard=False)
        end = self.awake() + self.grace
        while self.awake() < end:
            if group.gone():
                return
            self.sleep(0.2)
        group.signal(hard=True)
        for _ in range(50):
            if group.gone():
                return
            self.sleep(0.2)

    def result(self, data):
        data = {**data, "ended_at": self.wall()}
        for _ in range(RESULT_TRIES):
            try:
                write_json(self.path(RESULT), data)
                return True
            except OSError:
                self.sleep(RESULT_PAUSE)   # a full disk: the lock stays held, the core waits
        return False


if __name__ == "__main__":
    sys.exit(Run(sys.argv[1]).main())
