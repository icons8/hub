"""Windows adapter. The interface is the same as platforms/posix.py.

There are no signals here as on POSIX: `terminate()` is TerminateProcess, no handler is called; CTRL_BREAK reaches only
processes in the same console. So a soft stop goes through the stop pipe: hub-up closed it (or died), and hubd stops
by itself. There are no process groups or killpg either: a job (Job Object) holds the process tree, and everything in
it is stopped at once.
"""
import contextlib
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

STOP = "HUB_UP_STOP"     # hub-up sets it for the core: the reading end of the stop pipe (handle)
INNER = "HUB_UP_INNER"   # this hub-up is under the supervisor (run_hub_up)
RESTART = 75             # hub-up asks the supervisor to start it again (new code)
BREAKAWAY_FROM_JOB = 0x01000000   # CREATE_BREAKAWAY_FROM_JOB: the child is outside the parent's job (needs BREAKAWAY_OK)
_DETACHED = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
CREATE_NO_WINDOW = 0x08000000


# another (not administrator) account does not read the command lines of this account's processes
ARGS_SEEN_BY_ALL = False
KILLS_BY_IMAGE = True   # taskkill /IM, wmic: the hook (hooks/deny_hint.py) refuses killing by image name
# the core does not fast-forward a lead's folder here (core/daemon.forward_folders): a file held open by another
# process (an editor, a server) can stop a checkout halfway — after a probe on winstand
FORWARDS_FOLDERS = False
EOF_KEYS = "cli.platform.eof_windows"   # the catalog key of the end of input in a console (stdin of a module command, core/modcli): key names


def config_dir():
    """The hub's settings directory, where the OS recommends keeping it: %APPDATA%."""
    return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "hub-core")


def state_dir():
    """The default state directory: %LOCALAPPDATA%."""
    return os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "hub-core")


def try_lock(f):
    """Lock an open file without waiting: if it is taken, OSError."""
    import msvcrt
    f.seek(0)
    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)


def open_log(path):
    """A log descriptor for append only (FILE_APPEND_DATA), with shared access: every write goes to the end, including
    for children that got the handle without the CRT's append flag (with O_APPEND from a plain os.open the inner hub-up
    wrote from the start of the file over the log), and the file does not stop others from opening it."""
    import msvcrt
    k32 = _kernel32()
    h = k32.CreateFileW(path, _FILE_APPEND_DATA | _SYNCHRONIZE, 7, None, _OPEN_ALWAYS, 0x80, None)  # 7 is READ|WRITE|DELETE
    if h in (None, ctypes.c_void_p(-1).value):   # INVALID_HANDLE_VALUE
        raise ctypes.WinError(ctypes.get_last_error())
    return msvcrt.open_osfhandle(h, os.O_WRONLY | os.O_APPEND)


@contextlib.contextmanager
def exclusive(path, timeout=None):
    """Lock a file for the duration of the `with` block; if another process holds it, wait — at most timeout seconds,
    then TimeoutError (None: as long as it takes). msvcrt.locking without waiting, in a loop with a pause (LK_LOCK gives
    up after 10 attempts with OSError), and an explicit unlock: when the file is closed, the OS does not release the
    lock at once."""
    import msvcrt
    end = None if timeout is None else time.monotonic() + timeout
    with open(path, "a+") as f:
        while True:
            f.seek(0)  # the lock region starts at the file's current position
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if end is not None and time.monotonic() >= end:
                    raise TimeoutError(f"{path} is held by another process") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


def hook_command(python, hook):
    """The core's hook for Claude Code settings, exec form (`args`, Claude Code 2.1.139+), without a shell. As a string
    it would be run by Git Bash, which eats `\\` in an unquoted path (`C:Users...python.exe: command not found`): no events
    at all, and the session stays "starting" forever (2026-10-05). -X utf8: Claude Code writes UTF-8 to the hook, while the
    session's PYTHONUTF8 is the owner's (owner_env); in cp1251 a Cyrillic letter does not decode, and the event was lost (review)."""
    return {"type": "command", "command": python, "args": ["-X", "utf8", hook]}


def hook_shell(line):
    """argv running a hook's command line from settings: in PowerShell, as Codex runs a hook here (the session's shell;
    hosts/codex_rules._hook_line) — a repo's hook on file edits went this way before hooks/edit_hooks stood in front.
    PowerShell -Command ends with 1 after a failed last statement, a native command's code lost: a guard's exit 2 (a
    refusal) would be a mere error and the patch would go through (winstand, 2026-11-05). As /bin/sh -c: a line whose
    last statement succeeded — 0; failed — that native command's code (a cmdlet's failure — 1)."""
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            line + "\n" + HOOK_EXIT]


# after a hook's line: its last statement's exit code ($? right after it; $LASTEXITCODE — the last native command's)
HOOK_EXIT = "if (-not $?) { if ($LASTEXITCODE) { exit $LASTEXITCODE }; exit 1 }"


# the reason for refusing pkill/killall/pgrep (hosts/common.DENY) and killing by image name (hooks/deny_hint.py): Git
# Bash's `ps` sees only MSYS processes, and Git Bash spoils `tasklist /v` (`/v` becomes a path), so PowerShell. `\$_` so
# that bash does not substitute its own; inside, single quotes (Windows escapes double ones in an argument unreliably); without lines with
# Get-CimInstance: the query itself and its bash; fl: the whole command line, a table truncates it
KILL_HINT = ("Killing by name or pattern (taskkill /IM, Stop-Process -Name, pkill, killall) is closed to sessions by the "
             "hub: a name hits other people's processes — that is how the hub dies (`taskkill /IM python.exe` killed it "
             "whole: it is python too). Kill by the PID of your own process: remember the PID at start (`$!` in Bash) or "
             "find it: "
             "`powershell -NoProfile -Command \"Get-CimInstance Win32_Process | ? { \\$_.CommandLine -like '*<what>*' "
             "-and \\$_.CommandLine -notlike '*Get-CimInstance*' } | fl ProcessId,CommandLine\"` — and stop only your "
             "own process: `powershell -NoProfile -Command \"Stop-Process -Id <pid>\"`.")


def popen_detached(cmd, env=None, **kw):
    """A process apart from the caller, outliving it. DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP do not take it out of
    hub-up's job (run_hub_up): without CREATE_BREAKAWAY_FROM_JOB Chrome would die with the hub. An outer job without the
    right to break away refuses, then without the flag, as before."""
    try:
        return subprocess.Popen(cmd, creationflags=_DETACHED | BREAKAWAY_FROM_JOB, env=env, **kw)
    except PermissionError:
        return subprocess.Popen(cmd, creationflags=_DETACHED, env=env, **kw)



def popen_outside(cmd):
    """A process outside every job of the caller (hub-up's supervisor job closes with it; the scheduler task's job may
    not be left, probe A0): created by WMI (Win32_Process.Create), as the same user, without a window. -> True when
    WMI started it; False — it did not (the caller falls back to popen_detached). The new process has the user's own
    environment and folder, not the caller's: what it needs goes on its command line."""
    # a failed call is non-terminating and leaves $r empty ([int]$null is 0): any error, or no answer, is not a start
    script = ("$ErrorActionPreference = 'Stop'; $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
              "-Arguments @{CommandLine = $env:HUB_OUTSIDE_CMD}; if ($null -eq $r -or $null -eq $r.ProcessId) "
              "{ exit 9 }; exit [int]$r.ReturnValue")
    try:
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                           env={**os.environ, "HUB_OUTSIDE_CMD": subprocess.list2cmdline(cmd)}, capture_output=True,
                           stdin=subprocess.DEVNULL, timeout=60, creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0

# the app's replacement (core/shellnext.py): hub-up's exit code when the service is to start the new app, and whether a
# newer app found on disk under the running hub is taken by exiting — Setup.exe of the new app starts the scheduler task itself; an exit code the task counts as a failure would have it restarted meanwhile
APP_SWAP_EXIT = 0
APP_SWAP_ON_CHANGE = False


def bind_alone(sock):
    """Before bind: the port only if nobody listens on it. SO_REUSEADDR (http.server sets it) here lets a second socket
    take a port another one listens on — two servers on one port (27.10: the built-in gateway sat next to the module's);
    SO_EXCLUSIVEADDRUSE refuses, while a port with only closed connections (TIME_WAIT) still binds (checked on Windows 11)."""
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)


def apart():
    """subprocess arguments: the child in its own hidden console (CREATE_NO_WINDOW). Ctrl+C reaches all processes of a
    console: a test that sent it took down the hub too (2026-10-08: the update self-check under the hub's conhost, a
    minute later KeyboardInterrupt in hub-up). The stdout/stderr pipes work as usual."""
    return {"creationflags": CREATE_NO_WINDOW}


def no_terminal():
    """subprocess arguments so that the child does not ask from the terminal: here as is. There is no /dev/tty; a hidden console
    (apart) would hide the ssh question, and git would hang until the timeout instead of printing it to the owner in their console."""
    return {}


def join_command(argv):
    """argv -> a line for cmd and PowerShell on Windows (the bus prints its install command)."""
    return subprocess.list2cmdline(argv)


def split_command(line):
    """A command line from settings (voice.cmd) -> argv. shlex in POSIX mode eats the "\\" of paths (C:\\x\\asr.cmd ->
    C:xasr.cmd): here "\\" is just a character, quotes only group (2026-10-08)."""
    import shlex
    return [a[1:-1] if len(a) > 1 and a[0] == a[-1] == '"' else a for a in shlex.split(line, posix=False)]


def kill_detached(proc, hard=False):
    """Stop a popen_detached process together with its descendants (a browser is four processes; 2026-10-09: tests left them
    as orphans). The tree is searched from a live root, so it goes first, then the root itself. Always hard: TerminateProcess.
    A child already collected (returncode set): nothing — the snapshot no longer lists it, its tree is not found from it."""
    if proc.returncode is None:
        _kill_tree(proc.pid)
        proc.kill()


def stop_detached(pid, born):
    """Stop a popen_detached process another process started, by its number and the start time written with it,
    together with its descendants (a sign-in's codex.exe holds the files of its folder). No start time — left alone.
    -> True when stopped."""
    if born is None or type(pid) is not int:
        return False
    return _kill_tree(pid, born=born)


def on_core_stop(stop, soon):
    """hubd: stop means soon() (a flag; the core writes the snapshot between ticks): the stop pipe closed (hub-up stopped
    the core or died) or Ctrl+C / Ctrl+Break in the console, which all processes of the console receive. The snapshot
    cannot be written from the pipe's thread: it would go in the middle of a tick that edits the same state."""
    for name in ("SIGINT", "SIGBREAK"):
        signal.signal(getattr(signal, name), lambda *_: soon())
    _stop_on_parent(soon)


def on_hub_up_stop(stop):
    """hub-up: stop means stop(signum, frame) on SIGTERM, Ctrl+C and Ctrl+Break."""
    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        signal.signal(getattr(signal, name), stop)


def run_hub_up(main, script, argv, say, core_of):
    """hub-up under a thin supervisor: it holds the job (KILL_ON_JOB_CLOSE) containing the whole hub, and starts hub-up
    again on code RESTART (exec here is a new process). If the supervisor is killed ("End task", taskkill /F), the job
    closes and stops the whole tree: nobody is left holding ports and hubd.lock. BREAKAWAY_OK: the browser part's Chrome
    leaves the job and outlives the hub (popen_detached). The price: children of MSYS processes (Git Bash) whose nearest
    job is this one leave it by themselves; sessions have their own job without this flag (session_tree).
    If hub-up exited not for a restart (crashed, killed by pid), the core is already stopping on the closed pipe: the
    supervisor waits for it (core_of(hub-up pid) is the core's pid) up to stop_grace and only then exits and closes the
    job (review: the job stopped the core before the "stop" snapshot)."""
    if os.environ.get(INNER):
        return main(argv)
    for stream in (sys.stdout, sys.stderr):  # the shared log with hub-up is UTF-8 too (Python writes to a console this way anyway)
        if stream is not None and not stream.isatty():
            stream.reconfigure(encoding="utf-8")
    job = Job(kill_on_close=True, breakaway_ok=True)
    if not job.add_current():
        say(f"Windows job not created (error {job.error}): a killed supervisor will leave the hub's parts alive")
    for name in ("SIGINT", "SIGBREAK"):  # everyone gets Ctrl+C in the console: hub-up handles the stop
        signal.signal(getattr(signal, name), signal.SIG_IGN)
    # the hub works in Russian: open() and the parts' pipes are UTF-8, not the ANSI code page; the original value is for owner_env
    env = {**os.environ, INNER: "1", "PYTHONUTF8": "1", UTF8_WAS: os.environ.get("PYTHONUTF8", "")}
    # a venv's python.exe is a launcher, the real Python is its child: the pid of hub-up and the parts would be the
    # launcher's pid, and the core would not be found (review). The hub needs only the standard library: Python itself, bypassing the venv
    python = getattr(sys, "_base_executable", None) or sys.executable
    while True:
        inner = subprocess.Popen([python, script] + argv, env=env)
        code = inner.wait()
        if code == RESTART:
            say("hub-up restarts on the new code")
            env = {**env, "HUB_UP_RESTARTED": "1"}   # core/restart.RESTARTED: `hub stop` during a restart is fresh
            continue
        core = core_of(inner.pid)
        if core and alive(core):
            grace = stop_grace(True)
            say(f"hub-up exited with code {code}: waiting for the core (pid {core}) up to {grace} s — it writes the snapshot")
            end = time.time() + grace
            while alive(core) and time.time() < end:
                time.sleep(0.2)
        return _exit_code(code)


def supervisor():
    """The supervisor's pid (run_hub_up) for the inner hub-up, otherwise None: if it is killed, the job stops the whole hub."""
    return os.getppid() if os.environ.get(INNER) else None


def restart_gap():
    """How long `hub stop` waits for hub-up to write about itself after a full restart: the new hub-up is a new process,
    and until it writes to hub-up.json the pid is the old one's."""
    return 3


def restart_hub_up(script, argv):
    """hub-up again on the new code: exec here is a new process, so the supervisor starts it (run_hub_up)."""
    sys.exit(RESTART)


def ext_command(cmd):
    """The command of a plugin process (run in its manifest). `sh run.sh`: when the hub was started with the machine
    PATH (the Task Scheduler), it carries only Git's cmd folder, and sh is not found (WinError 2) - then Git's own
    sh.exe. `python3 main.py` (or `python`): when PATH has no such Python or only the Microsoft Store alias (its stub
    exits 9009 and the plugin never runs), the hub's own Python, as for modules (core/modrun.command)."""
    if cmd and cmd[0] == "sh" and not shutil.which("sh"):
        return [_git_sh() or "sh", *cmd[1:]]
    if cmd and cmd[0] in ("python3", "python") and _no_python(cmd[0]):
        return [sys.executable, *(["-X", "utf8"] if sys.flags.utf8_mode else []), *cmd[1:]]
    return cmd


def _no_python(word):
    """No real Python by this name in PATH: none at all, or only an app alias in WindowsApps (the Store stub)."""
    path = shutil.which(word)
    return not path or "windowsapps" in re.split(r"[\\/]", path.lower())


def _git_sh():
    """sh.exe of Git for Windows next to the git found in PATH (Git/cmd/git.exe -> Git/usr/bin/sh.exe), else None."""
    git = shutil.which("git")
    if not git:
        return None
    root = os.path.dirname(os.path.dirname(os.path.realpath(git)))
    for rel in (("usr", "bin", "sh.exe"), ("bin", "sh.exe")):
        path = os.path.join(root, *rel)
        if os.path.isfile(path):
            return path
    return None


def _git_bash():
    """bash.exe of Git for Windows: the one CLAUDE_CODE_GIT_BASH_PATH names (the hub package sets it), else the one next to
    the git found in PATH (Git/cmd/git.exe, or Git/mingw64/bin/git.exe from a Git Bash PATH -> Git/bin/bash.exe: it puts
    Git's tools in PATH), else None. Not `bash` from PATH: with the PATH of the Task Scheduler that is the WSL launcher
    from System32."""
    named = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH")
    if named and os.path.isfile(named):
        return named
    git = shutil.which("git")
    if not git:
        return None
    up = os.path.dirname(os.path.dirname(os.path.realpath(git)))
    for root in (up, os.path.dirname(up)):
        path = os.path.join(root, "bin", "bash.exe")
        if os.path.isfile(path):
            return path
    return None


_JOB_SHELL_TOLD = set()   # why a bash job stayed in cmd.exe: one line per reason per process (a job every minute)


def job_shell(command, env, state_dir, shell):
    """A schedule command job or pre-check (a shell string) -> (what to start, shell=, env). As before through cmd.exe;
    with hub.json `schedule.shell` "bash" through Git Bash: the sessions write these lines in Bash (a pipe, `$(...)`,
    single quotes), and cmd reads a pipe outside double quotes as its own. With the `hub` of the nested folder first in
    PATH: `bin/hub` has a python3 shebang, not found here. No Git Bash, or the folder cannot be written: cmd.exe and a
    line in the log (once)."""
    if shell != "bash":
        return command, True, env
    bash = _git_bash()
    why = None if bash else ("Git Bash is not found (CLAUDE_CODE_GIT_BASH_PATH, Git/bin/bash.exe next to git)")
    if bash:
        shim = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hosts", "nested", "claude")
        try:
            folder, extra = nested(shim, state_dir)
        except OSError as e:
            why = f"the folder of `hub` cannot be written ({e})"
    if why:
        if why not in _JOB_SHELL_TOLD:
            _JOB_SHELL_TOLD.add(why)
            print(f"schedule: schedule.shell is bash, but {why} - the jobs run in cmd.exe", file=sys.stderr, flush=True)
        return command, True, env
    return [bash, "-c", command], False, {**env, **extra, "PATH": folder + os.pathsep + env.get("PATH", "")}


def job_quote(args, shell):
    """A schedule command from a list of arguments (the core's own pre-checks), for the shell job_shell runs it in. Git
    Bash: single quotes (in double ones bash eats `\\$`, a backtick and a `\\` before the closing quote); cmd.exe: in
    double quotes, as before."""
    if shell == "bash" and _git_bash():
        import shlex
        return shlex.join(args)
    return " ".join(f'"{a}"' for a in args)


# Priority classes (creationflags). The Task Scheduler starts the hub task at priority 7 (below normal), and every child
# inherits it: under a full CPU (a heavy program rendering at normal priority) the core, the page and Telegram got no
# processor for minutes (no journal event at all, the page and the bot silent). The hub's own parts run at
# normal priority; sessions and plugin processes (their heavy commands: whisper, Chrome, ffmpeg) stay below normal, so
# the owner's work in other programs still comes first.
NORMAL_PRIORITY = 0x00000020
BELOW_NORMAL_PRIORITY = 0x00004000
_PRIORITY_CLASSES = 0x00000020 | 0x00000040 | 0x00000080 | 0x00000100 | 0x00004000 | 0x00008000


def background():
    """subprocess arguments for heavy work beside the hub (sessions, plugin processes): below normal priority."""
    return {"creationflags": BELOW_NORMAL_PRIORITY}


def below_hub(cmd, cwd=None, platform=sys.platform):
    """The command of heavy work beside the hub, unchanged: its priority is given at the start (background())."""
    return list(cmd)


def give_way():
    """Nothing: a background job of the hub gets below normal priority at the start (background())."""


def spawn_part(cmd, core, tree=False, **kw):
    """A hub part under hub-up -> (process, stop handle). For the core, the stop pipe: closed (stop_part) or hub-up
    died, and hubd writes the snapshot and exits; without it TerminateProcess would stop the core without a snapshot.
    tree (plugin processes): the part and all it starts are in a job (session_tree). `sh run.sh` does not replace
    itself with `exec` under Git Bash: python is a child of sh, TerminateProcess of sh left it running, and after every
    hub restart one more copy of the plugin posted each message (2026-11-05)."""
    if not kw.get("creationflags", 0) & _PRIORITY_CLASSES:   # a hub part: normal priority, whatever hub-up runs at
        kw["creationflags"] = kw.get("creationflags", 0) | NORMAL_PRIORITY
    kw["env"] = {k: v for k, v in (kw.get("env") or os.environ).items() if k != INNER}
    if not core:
        p = subprocess.Popen(cmd, **kw)
        return p, session_tree(p) if tree else None
    r, w = os.pipe()  # both ends are non-inheritable (PEP 446); the child gets only the reading one
    import msvcrt
    handle = msvcrt.get_osfhandle(r)
    os.set_handle_inheritable(handle, True)
    kw["env"][STOP] = str(handle)
    try:
        # close_fds=False passes only inheritable handles: this one and the standard streams
        return subprocess.Popen(cmd, close_fds=False, **kw), w
    except BaseException:
        os.close(w)
        raise
    finally:
        os.close(r)


def stop_part(proc, handle):
    """Ask a part to stop: for the core, close the pipe (it leaves by itself, with a snapshot), for a plugin its job
    (the whole tree), for the others TerminateProcess."""
    if isinstance(handle, Job):
        handle.terminate()
        handle.close()
    elif handle is not None:
        os.close(handle)
    elif proc.poll() is None:
        proc.terminate()


def stop_grace(core):
    """How long to wait for a part after stop_part. The core writes the snapshot after a tick, and a tick can be long (git:
    up to 30 s per call), so it gets longer (review)."""
    return 60 if core else 15


def session_tree(popen):
    """A session's process tree: claude and everything it starts (background commands) are in a job; there are no process
    groups or killpg here. Without BREAKAWAY_OK: with it MSYS (Git Bash, through which claude runs Bash) itself sets
    CREATE_BREAKAWAY_FROM_JOB on children, and `sleep 300 &` and dev servers left the job as orphans (review). That is
    also why Chrome, which the session's MCP launcher started itself, lives while the session lives; the browser part's
    Chrome is outside sessions. KILL_ON_JOB_CLOSE: hubd holds the job, and if it dies without stopping the sessions, they
    stop too instead of living as orphans. A window: children born before add are outside the job (subprocess can't do CREATE_SUSPENDED)."""
    job = Job(kill_on_close=True)
    if job.add(popen.pid):
        return job
    job.close()
    return None


def terminate_session(popen, tree):
    """Stop the session's process together with its tree."""
    tree.terminate() if tree else popen.terminate()


def oom_first(pid, score):
    """There is no victim order under memory pressure (Linux's oom_score_adj) here: unchanged."""


def keep_on_oom(cgroup="/proc/self/cgroup", home=None, run=subprocess.run):
    """There are no systemd services here."""
    return False


def end_tree(tree):
    """claude is gone: its background commands go with it, not as orphans (2026-10-05: `sleep 300` lived after the hub)."""
    if tree:
        tree.terminate()
        tree.close()


def end_tagged(match, grace=3.0):
    """Session processes by the tree marker (POSIX): here the session's job took them when claude left (end_tree)."""
    return 0


def reap(pid, session_id):
    """A session process from a previous life of hubd: if it is that one (the sessionId is in the command line), stop it. True means stopped.
    There is no ps here, and Git Bash's ps does not know -o: the command line comes through Win32_Process; os.kill is
    TerminateProcess of one root, while the tree is searched while the root is alive."""
    if not alive(pid):  # PowerShell takes a second per call: only for the living
        return False
    born = process_start(pid)   # before the command: the number may go to another process while PowerShell reads it
    cmd = command_line(pid)
    if born is None or session_id not in cmd or "stream-json" not in cmd:
        return False
    return _kill_tree(pid, born=born)


def reap_tagged(pid, match):
    """A session process found by its tree marker (POSIX): here the session's job (KILL_ON_JOB_CLOSE) stopped it
    together with the hubd that held the job."""
    return False


def replace(src, dst):
    """Atomically replace dst with the file src. While dst is open by a reader (open() in Python has no FILE_SHARE_DELETE),
    Windows refuses with PermissionError: `hub` reads state.json every 0.3 s, the page and the clock read the journal.
    Retry until the reader closes the file (review: hubd crashed on save_state, and on stop, without a snapshot)."""
    end = time.time() + 5
    pause = 0.01
    while True:
        try:
            return os.replace(src, dst)
        except PermissionError:
            if time.time() >= end:
                raise
            time.sleep(pause)
            pause = min(pause * 2, 0.2)


def sync(fd, platform=sys.platform):
    """Flush an open file to the disk itself: fsync here is FlushFileBuffers, through the drive's cache."""
    os.fsync(fd)


def sync_dir(path):
    """A rename is reliable without this: NTFS journals metadata, and a folder can't be opened for flushing."""


_opened = set()   # the folders this process has already looked at in makedirs_private


def makedirs_private(path):
    """A folder of the hub that others must not read. No mode here: the user profile folders are closed by their own
    ACL, which the new folder inherits. A mode of 0o700 (Python 3.12.4+) gives the folder an ACL of SYSTEM,
    Administrators and OWNER RIGHTS alone, and a process of the scheduled task owns it as the Administrators group - the
    user's own terminal, where Administrators is "deny only", could neither read nor delete it (06.11: `hub uninstall
    --purge` left update/). A folder that existed before may be one of those: the process that made it (the caller is
    the hub's own, so it may) opens it to the user once, in the background: the inherited ACL back, OWNER RIGHTS out."""
    existed = os.path.isdir(path)
    os.makedirs(path, exist_ok=True)
    key = os.path.normcase(os.path.abspath(path))
    if key not in _opened:
        _opened.add(key)
        if existed:
            threading.Thread(target=_reopen, args=(path,), name="acl", daemon=True).start()


def _reopen(path):
    try:
        r = run(["icacls", path, "/inheritance:e", "/remove", "*S-1-3-4", "/T", "/C", "/Q"], 120,
                capture_output=True, stdin=subprocess.DEVNULL)
        if r.returncode:
            print(f"the folder {path} stays closed to the user's terminal: icacls exit {r.returncode}", flush=True)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"the folder {path} stays closed to the user's terminal: {e}", flush=True)


def link_dir(src, link):
    """link is a link to the folder src. A symbolic link needs a privilege (WinError 1314 without developer mode), so then a
    junction: no privileges, for folders on the same computer it is the same thing. Not through `cmd /c mklink /J`: cmd cuts
    the path at & and does not escape ^ and % (review, 2026-10-09)."""
    try:
        os.symlink(src, link, target_is_directory=True)
    except OSError as e:
        import _winapi
        try:
            _winapi.CreateJunction(os.path.abspath(src), os.path.abspath(link))
        except OSError as j:
            from core import i18n
            raise OSError(i18n.t("cli.platform.link_failed", symlink=e, junction=j, link=link, src=src)) from j


def run(args, timeout, **kw):
    """subprocess.run with a timeout that holds. `git` from Git/cmd is a launcher: on a timeout subprocess.run stopped
    only it and waited for the pipes the real git holds, until its end (review: a hung git held the core's tick longer
    than hub-up waits for the core). On a timeout or an interrupt we stop the whole tree; the tree is in a job with
    KILL_ON_JOB_CLOSE: if the caller is killed without cleanup (TerminateProcess of a part on a hub restart), it stops
    too (2026-10-12: the update self-check's tools/test.py lived on 80 more minutes after hub-update and ate the CPU of
    the next check). The job stops on a normal exit too: a background process left by the command (git fsmonitor--daemon
    and the like) does not outlive the call, while on POSIX such a background process lives. A window: children born
    before add are outside the job, as in session_tree."""
    if kw.pop("capture_output", False):
        kw["stdout"] = kw["stderr"] = subprocess.PIPE
    data = kw.pop("input", None)
    if data is not None:   # as subprocess.run and posix.run: without a pipe communicate silently drops the input (26.10)
        if kw.get("stdin") is not None:
            raise ValueError("stdin and input arguments may not both be used.")
        kw["stdin"] = subprocess.PIPE
    with subprocess.Popen(args, **kw) as p:
        tree = session_tree(p)
        try:
            out, err = p.communicate(data, timeout=timeout)
        except BaseException as e:
            # an interrupt (Ctrl+C, exit by a signal) stops the tree too, as posix.run: otherwise the update self-check
            # released the machine lock, and tools/test.py with Chrome went on next to the next run (review).
            # The number will not go to someone else: Popen holds the process handle
            tree.terminate() if tree else _kill_tree(p.pid)   # the job stops orphans too, whose parent has already exited
            p.kill()
            if isinstance(e, subprocess.TimeoutExpired):
                p.communicate()
            raise
        finally:
            if tree:
                tree.close()   # whatever left the process into the background and is alive stops with it
    return subprocess.CompletedProcess(args, p.returncode, out, err)


HOOK_EXEC_FORM = (2, 1, 139)   # Claude Code with the exec form of hooks (`args`): hook_command


def claude_problem(version):
    """What this Claude Code version lacks for the hub on this OS: a text for the owner, or None. The core's hook here is
    in exec form: an old Claude Code does not know it, hooks do not fire, and the session stays "starting" forever (review)."""
    try:
        got = tuple(int(x) for x in str(version).split(".")[:3])
    except ValueError:
        return None
    if got >= HOOK_EXEC_FORM:
        return None
    need = ".".join(map(str, HOOK_EXEC_FORM))
    from core import i18n   # the text goes into the journal (session.info problem) and the owner sees it later
    return i18n.tr(i18n.owner(), "cli.platform.claude_too_old", version=version, need=need)


UTF8_WAS = "HUB_UP_PYTHONUTF8_WAS"   # the owner's PYTHONUTF8 before the supervisor ("" means there was none)


def python():
    """Python for the hub's own processes that hub-up does not start (the MCP server in a session): in UTF-8 by itself
    (-X utf8), since PYTHONUTF8 from the hub's environment does not reach the owner's commands (owner_env)."""
    return [sys.executable, "-X", "utf8"]


def utf8_mode(argv):
    """The hub's own Python process (`hub` from a session or a schedule job) in UTF-8: output is UTF-8 (claude reads it),
    the hub's files are UTF-8. If it is not, restart itself with -X utf8 (PYTHONUTF8 in the environment would be inherited
    by the owner's scripts). argv is the script path and arguments; interpreter flags (-u, -O...) stay, as in posix."""
    if sys.flags.utf8_mode:
        return
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # the child gets Ctrl+C in the console too: when it exits, so do we
    flags = sys.orig_argv[1:len(sys.orig_argv) - len(sys.argv)]
    sys.exit(_exit_code(subprocess.call([sys.executable, *flags, "-X", "utf8"] + argv)))


def read_stdin():
    """The whole stdin of a `hub ... -` command, as text. Bytes: UTF-8, and if it is not, the output of a console program in
    the OEM encoding (cmd, tasklist) (review: in UTF-8 mode it was read with surrogates, and writing to the spool failed).
    Line ends as text mode reads them: a pipe from PowerShell or a text-mode writer brings "\\r\\n" (release stands
    11.11: every line of the message ended in a stray "\\r")."""
    raw = sys.stdin.buffer.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("oem", "replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def output_text(raw):
    """A child program's output (bytes) as text: UTF-8, and if it is not, a message from cmd.exe or a system utility in the
    console's OEM encoding (2026-10-15: hub critic failed with UnicodeDecodeError, byte 0xad, a Cyrillic letter in cp866)."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("oem", "replace")


def umask(mask):
    """There is no umask here: the user profile folders are closed by ACL -> None (core/perms.py)."""
    return None


def file_modes():
    """Whether mode bits keep a file closed here: no, the folder's ACL does that (core/perms.py)."""
    return False


def owner_env(env):
    """The environment for the owner's commands (sessions, schedule jobs) from the hub's environment: PYTHONUTF8 as the
    owner had it, not the supervisor's (review: their cp1251 scripts broke only inside the hub)."""
    env = dict(env)
    was = env.pop(UTF8_WAS, None)
    if was is not None:
        if was:
            env["PYTHONUTF8"] = was
        else:
            env.pop("PYTHONUTF8", None)
    return env


def nested(shim, state_dir):
    """The `claude` wrapper for a session (hosts/nested/claude) -> (a folder first in PATH, the session's environment
    variables). A `python3` shebang is no support here: most often it is a Microsoft Store stub, not Python. A folder of its
    own in the state directory: `claude` for Git Bash (Bash claude) and `claude.cmd` for PowerShell and cmd; both start the
    wrapper with the same Python as the hub. Paths are in environment variables, not in files: cmd reads .cmd in the
    console code page and expands %...% in it (review: a folder with a Cyrillic name or a "%" in it, and the wrapper
    did not start). The HUB_NESTED_FLAGS marker lets the wrapper look for the real claude past this folder. `hub` and
    `hub.cmd` are there too: the `bin/hub` shebang is the same python3, and `hub` from a session's Bash exited with code
    49 (2026-10-06, test bench: a hub session could neither write nor look at the status)."""
    d = os.path.join(state_dir, "nested")
    os.makedirs(d, exist_ok=True)
    files = {"claude": ('#!/bin/sh\n# hosts/nested/claude (HUB_NESTED_FLAGS)\n'
                        'exec "$HUB_NESTED_PYTHON" "$HUB_NESTED_SHIM" "$@"\n'),
             "claude.cmd": ('@rem hosts/nested/claude (HUB_NESTED_FLAGS)\r\n'
                            '@"%HUB_NESTED_PYTHON%" "%HUB_NESTED_SHIM%" %*\r\n'),
             "hub": '#!/bin/sh\nexec "$HUB_NESTED_PYTHON" "$HUB_NESTED_HUB" "$@"\n',
             "hub.cmd": '@"%HUB_NESTED_PYTHON%" "%HUB_NESTED_HUB%" %*\r\n'}
    for name, text in files.items():
        path = os.path.join(d, name)
        try:
            with open(path, newline="") as f:
                if f.read() == text:
                    continue
        except OSError:
            pass
        with open(path + ".tmp", "w", newline="") as f:
            f.write(text)
        replace(path + ".tmp", path)
    hub = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")
    return d, {"HUB_NESTED_PYTHON": sys.executable, "HUB_NESTED_SHIM": os.path.abspath(shim), "HUB_NESTED_HUB": hub}


def exec_instead(argv):
    """This process becomes the command argv (the hosts/nested/claude wrapper becomes the real claude). exec here is a new
    process, and this one would exit at once: the caller would get code 0 before the answer. As a child: its exit code is ours."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # the child gets Ctrl+C in the console too: when it exits, so do we
    code = subprocess.call(argv)
    sys.exit(_exit_code(code))


def spec_loader():
    """The command that starts a program from a private spec file {"env", "umask"} and removes the file (the tmux host
    is POSIX-only; exec here would be a new process, so the program is a child and its exit code is ours)."""
    return [sys.executable, "-I", "-c", "import json, os, subprocess, sys\n"
            "path = sys.argv[1]\n"
            "with open(path, encoding='utf-8') as f:\n    spec = json.load(f)\n"
            "os.unlink(path)\n"
            "sys.exit(subprocess.call(sys.argv[2:], env={**os.environ, **spec['env']}))\n"]


def page_path(path):
    """A path as the page asked for it (file:///D:/x is "/D:/x") as a machine path: "D:/x" (realpath of "/D:/x" would give
    a path with two drives)."""
    return path[1:] if re.match(r"/[A-Za-z]:[\\/]", path) else path


def native_path(path):
    """Whether this is a path here: on Windows both a path with a drive (C:) and one from "/" are paths."""
    return True


def long_path(path):
    """A path that Windows file calls accept even when longer than 260 characters: \\\\?\\ before the full path."""
    path = os.path.abspath(path)
    if path.startswith("\\\\?\\"):
        return path
    return "\\\\?\\UNC\\" + path[2:] if path.startswith("\\\\") else "\\\\?\\" + path


def rmdir(path):
    """Remove an empty folder, also with a path longer than 260 characters and with the read-only attribute (rmdir
    refuses on it with WinError 5): clear it and try once more."""
    import stat
    path = long_path(path)
    try:
        os.rmdir(path)
    except PermissionError:
        os.chmod(path, stat.S_IWRITE)
        os.rmdir(path)


def cut_junctions(path):
    """Cut every junction inside the folder without going into it: on Windows `git worktree remove` (2.33) and os.walk
    walk through a junction and erase what it leads to (2026-11-09: a copy's node_modules was a junction to the main
    checkout's). The junction itself goes (rmdir touches no target), the folders around it stay. A symlink stays: git does
    not walk through one, and one it tracks (mode 120000) is part of a clean copy. A mount point is not cut (a volume is not
    ours to unmount) and neither is the folder itself being a junction: OSError. -> how many junctions were cut."""
    if os.path.islink(path) or os.path.isjunction(path):
        raise OSError(f"{path} is itself a link")
    cut = 0
    with os.scandir(long_path(path)) as it:
        entries = list(it)
    for e in entries:
        plain = os.path.join(path, e.name)
        if e.is_junction():
            # by the plain path: ntpath.ismount says False for anything with the \\?\ prefix but a drive root
            if os.path.ismount(plain):
                raise OSError(f"{plain} is a mount point")
            os.rmdir(e.path)   # not rmdir() of ours: its chmod fallback would follow the link
            cut += 1
        elif e.is_dir(follow_symlinks=False):
            cut += cut_junctions(plain)
    return cut


def rmtree(path):
    """Delete a folder entirely: both paths longer than 260 characters (2026-10-09: executors' copies with node_modules) and
    "read-only" files (git makes .git/objects such). -> whether the folder remained."""
    import shutil
    import stat

    def again(func, p, _):
        if func not in (os.unlink, os.remove, os.rmdir):   # retrying os.scandir would leave an open handle on the directory
            return
        try:
            # a link itself, never what it leads to (someone else's file); Python 3.12 has no such chmod here: skipped
            os.chmod(p, stat.S_IWRITE, **({"follow_symlinks": False} if os.path.islink(p) or os.path.isjunction(p)
                                          else {}))
            func(p)
        except (OSError, NotImplementedError):
            pass
    shutil.rmtree(long_path(path), onexc=again)
    return os.path.exists(long_path(path))


def has_exec_bit(path):
    """No execute bit: here the extension decides what is a program (.exe, .cmd, .ps1...)."""
    return False


def alive(pid):
    """Whether the process is alive: the exit code STILL_ACTIVE. `os.kill(pid, 0)` here is not a check but Ctrl+C
    (GenerateConsoleCtrlEvent)."""
    k32 = _kernel32()
    handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    code = wintypes.DWORD()
    try:
        return bool(k32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == _STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def process_start(pid):
    """When the process started, or None (no such process, not readable): with the pid it names one process, since a
    number written to a file may go to another one. Here the creation time (FILETIME as a number)."""
    if type(pid) is not int or pid <= 4:   # Idle and System
        return None
    k32 = _kernel32()
    handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        return _created(k32, handle)
    finally:
        k32.CloseHandle(handle)


def service():
    """hub-up is started by an OS service -> the command that stops it; otherwise None. The hub has no services on Windows yet."""
    return None


def old_service(repo, home=None, cgroup=None):
    """The earlier service layout was Linux only (a systemd service per part): none here."""
    return None


def old_units(repo, state, home=None):
    return []


TASK = "Agents Hub"   # the scheduler task that deploy/windows/install.ps1 sets up
APP_OS = "windows"   # the app's name of this OS (core/appmove.py)
CASE_BLIND = True   # NTFS ignores case: .ENV is .env (hooks/repo_rules.py)


def console_encoding():
    """The console code page (OEM) the .cmd files of deploy/windows/setup.py are written in: cp866 on Russian Windows."""
    return f"cp{_kernel32().GetOEMCP()}"


def system_language():
    """The language tag the user's Windows speaks ('ru-RU'), for the hub's first greeting before its owner has written;
    '' when it does not say."""
    try:
        name = ctypes.WinDLL("kernel32").GetUserDefaultLocaleName
        name.argtypes, name.restype = [wintypes.LPWSTR, ctypes.c_int], ctypes.c_int
        buf = ctypes.create_unicode_buffer(85)
        return buf.value if name(buf, 85) else ""
    except (OSError, AttributeError):
        return ""


def service_unit(state_dir, home=None, platform=sys.platform):
    """The hub at Windows sign-in is held by a scheduler task (not a service): `hub on` runs it; `hub off` is
    `hub stop` (the task does not start the hub again). No task, or the task runs another hub (its action is not this
    state folder's launcher): None — `hub uninstall` of a second hub took the first one's task (tests research 03.11).
    An instance (core/instance.py) has its own task, "Agents Hub (<name>)", named by its anchor."""
    task = _task_of(state_dir)
    try:
        r = subprocess.run(["schtasks", "/Query", "/TN", task, "/XML"], capture_output=True, timeout=20,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0 or not _task_runs(r.stdout or b"", state_dir):
        return None
    return {"kind": "task", "id": task, "path": None, "port": None, "files_port": None}


def _task_of(state_dir):
    """The scheduler task of the hub in this state folder: the anchor's (an instance), else TASK. Read here, not
    through core/instance.py: a process of the previous release loads this file anew with its own core in memory."""
    import json
    try:
        with open(os.path.join(state_dir, "instance.json"), encoding="utf-8") as f:
            svc = json.load(f).get("service") or {}
    except (OSError, ValueError, AttributeError):
        return TASK
    got = svc.get("id") if isinstance(svc, dict) and svc.get("kind") == "task" else None
    return got if isinstance(got, str) and got.startswith(TASK + " (") else TASK


def _task_runs(xml, state_dir):
    """The task's XML (schtasks /Query /XML, bytes) runs <state_dir>\\hub-up.cmd, as deploy/windows/install.ps1 sets it
    up: `--headless cmd.exe /c "<launcher>"`. Paths in any letter case, the path whole (not the start of a longer one).
    schtasks writes the XML in the console's OEM code page, not in the UTF-16 it declares (winstand 03.11: Cyrillic came
    in cp866), a letter that page lacks as «?» or its look-alike (best fit): the path is also compared as that page turns
    it; the bytes are read both as UTF-8 and as OEM (Cyrillic in cp866 may happen to be valid UTF-8)."""
    import html
    import ntpath
    path = ntpath.join(ntpath.normpath(state_dir), "hub-up.cmd")
    texts, forms = {output_text(xml)}, {path}
    if xml[:2] in (b"\xff\xfe", b"\xfe\xff"):
        texts.add(xml.decode("utf-16", "replace"))
    try:
        texts.add(xml.decode("oem", "replace"))
        forms.add(path.encode("oem", "replace").decode("oem"))
    except LookupError:   # no OEM code page outside Windows
        pass
    args = [ntpath.normcase(html.unescape(a)) for t in texts for a in re.findall(r"<Arguments>(.*?)</Arguments>", t, re.S)]
    return any(re.search(r'(?:^|[\s"])' + re.escape(ntpath.normcase(f)) + r'(?=[\s"]|$)', a) for f in forms for a in args)


def service_commands(unit, uid=None):
    return {"on": ["schtasks", "/Run", "/TN", unit["id"]], "off": None, "status": None}


def lingers():
    """No systemd here: the logon task brings the hub up, and `hub off` holds it with the stop mark (restart.HELD)."""
    return False


def reveal_command(path, platform=sys.platform):
    """Show a file in its folder: Explorer with the file selected. A string, not a list: Explorer needs /select,"path"
    (quotes around the path alone), while a subprocess list quotes the whole argument "/select,path", and with a space in
    the path Explorer opens "Documents"."""
    return f'explorer /select,"{os.path.normpath(path)}"'


def file_manager(env=None, platform=sys.platform, which=shutil.which, run=subprocess.run):
    """What the hub machine shows a folder in (the id the page turns into the button's name): always Explorer. The
    arguments are the same as on POSIX (the adapters keep one interface); nothing is asked here."""
    return "explorer"


# ---- internal ------------------------------------------------------------------------------------------------------
def _exit_code(code):
    """A child's exit code for sys.exit: an NTSTATUS code (Ctrl+C is 0xC000013A) is not accepted by sys.exit: 1."""
    return code if 0 <= code <= 255 else 1


def _stop_on_parent(handler):
    """A child: the stop pipe closed (the parent stopped the part or died itself) -> handler(). No pipe (not started from
    hub-up): nothing. Not stdin (2026-10-05): the child's children inherit it, and while a read from the pipe hangs in
    another thread, git with the same stdin waited forever at start: hubd got stuck on the first `git rev-parse`."""
    import msvcrt
    value = os.environ.pop(STOP, None)  # pop: sessions and their commands must not inherit it
    if not value:
        return
    os.set_handle_inheritable(int(value), False)
    fd = msvcrt.open_osfhandle(int(value), os.O_RDONLY)

    def watch():
        try:
            while os.read(fd, 1024):
                pass
        except OSError:
            pass
        handler()
    threading.Thread(target=watch, daemon=True, name="stop-on-parent").start()


def command_line(pid):
    """A process's command line, or "" (no such process / no access): Win32_Process through PowerShell."""
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
           f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=30, stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.decode("oem", "replace").strip()  # system utilities write to the pipe in the OEM encoding, not UTF-8


def _kill_tree(pid, born=None):
    """Stop a live process together with its descendants (the root must be alive: the tree is searched from it). By a
    process snapshot (Toolhelp32) and TerminateProcess: taskkill /T asks the system through services, and on a machine
    where they are slow every call took 10 s (2026-10-19: tasklist and taskkill took 10.4 s each even for a nonexistent
    pid; the stop tests were red, the update self-check over the limit). Each member is stopped through a handle whose
    start time is the one the tree was built with: a number freed after the snapshot and taken by a foreign process is
    left alone. born: the root's start time as the caller saw it (reap, before reading the command line), None — the
    caller holds the root (its Popen). The snapshot failed: the root alone (taskkill /T walks parent numbers without
    start times — the walk of 2026-11-03). Numbers up to 4 (Idle, System) are never ours."""
    if int(pid) <= 4:
        return False
    times = {}
    tree = _tree_of(int(pid), times)
    if tree is None:
        tree = [int(pid)]
    if not tree or born is not None and times.get(tree[0], born) != born:
        return False
    if born is not None:
        times[tree[0]] = born
    k32 = _kernel32()
    done = False
    for p in tree:   # the root first: it will spawn no one more
        known = times.get(p)
        h = k32.OpenProcess(_PROCESS_TERMINATE | (_PROCESS_QUERY_LIMITED_INFORMATION if known is not None else 0),
                            False, p)
        if h:
            try:
                if known is None or _created(k32, h) == known:
                    done |= bool(k32.TerminateProcess(h, 1)) and p == tree[0]
            finally:
                k32.CloseHandle(h)
    return done


def _created(k32, handle):
    """The process's start time (FILETIME as a number) by its handle, or None."""
    times = [wintypes.FILETIME() for _ in range(4)]
    if not k32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
        return None
    return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime


def _tree_of(pid, times=None):
    """[pid, its descendants...] by a process snapshot; [] means no such process; None means the snapshot failed. A
    descendant is one whose parent is in the tree and which, by known start times, started no earlier than the parent:
    the number of a dead parent could have gone to a foreign process. The caller and its ancestors by the same snapshot
    (_chain) are never in the tree; if pid is one of them, []. times: a dict that gets the start times read
    ({pid: time or None})."""
    k32 = _kernel32()
    snap = k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snap or snap == _INVALID_HANDLE:
        return None
    parent = {}
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            parent[entry.th32ProcessID] = entry.th32ParentProcessID
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    if pid not in parent:
        return []
    born = {} if times is None else times

    def started(p):
        if p not in born:
            born[p] = None
            h = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, p)
            if h:
                born[p] = _created(k32, h)
                k32.CloseHandle(h)
        return born[p]
    mine = _chain(os.getpid(), parent, started)
    return [] if pid in mine else _descend(pid, parent, started, mine)


def _chain(me, parent, started):
    """The caller and its ancestors from a snapshot {pid: parent pid}: up to a dead number, a loop, or a "parent" that
    started later than a known time below it. A dead ancestor's number may have gone to a newer process — the hub's own
    Chrome or session too (review of PR #421: taken as an ancestor, it was never stopped and leaked); wininit's parent is
    such a number. A link with an unreadable time (a launcher that just exited, a system process) does not end the chain:
    the ancestors above it stay spared."""
    mine, p, floor = {me}, me, started(me)
    while parent.get(p) in parent and parent[p] not in mine:
        p = parent[p]
        a = started(p)
        if a is not None and floor is not None and a > floor:
            break
        if a is not None:
            floor = a if floor is None else min(floor, a)
        mine.add(p)
    return mine


def _descend(pid, parent, started, spare=()):
    """[pid, its descendants...] from a snapshot {pid: parent pid} and started(pid) -> start time or None. An unknown
    time is not a descendant (2026-11-03): wininit and csrss point at a long-dead parent pid, a normal user cannot read
    their time, and a hub process that got that pid took them, services and every svchost into its tree; stopping it
    killed the whole user session. Our own processes have readable times; one that exits between the snapshot and the
    time check leaves its children to a job if the caller has one (sessions, run when the job was created);
    kill_detached, tty_stop and reap have none. spare: pids never taken (the caller and its ancestors)."""
    tree, i = [pid], 0
    while i < len(tree):
        a = started(tree[i])
        for child, par in parent.items():
            if a is not None and par == tree[i] and child not in tree and child != par and child not in spare:
                b = started(child)
                if b is not None and b >= a:
                    tree.append(child)
        i += 1
    return tree


class Job:
    """A job: processes in it and all their descendants are stopped by one call (terminate), and with kill_on_close also
    when the last handle of the job is closed (the owner died, even forcibly)."""

    def __init__(self, kill_on_close=False, breakaway_ok=False):
        self.handle, self.error = None, None  # error is a Windows error code if the job was not created
        k32 = _kernel32()
        handle = k32.CreateJobObjectW(None, None)
        if not handle:
            self.error = ctypes.get_last_error()
            return
        flags = (_KILL_ON_JOB_CLOSE if kill_on_close else 0) | (_BREAKAWAY_OK if breakaway_ok else 0)
        if flags:
            info = _ExtendedLimits()
            info.BasicLimitInformation.LimitFlags = flags
            if not k32.SetInformationJobObject(handle, _EXTENDED_LIMIT_INFO, ctypes.byref(info), ctypes.sizeof(info)):
                self.error = ctypes.get_last_error()
                k32.CloseHandle(handle)
                return
        self.handle = handle

    @property
    def ok(self):
        return self.handle is not None

    def add_current(self):
        """This process goes into the job; its future children land there by themselves."""
        if not self.ok:
            return False
        if _kernel32().AssignProcessToJobObject(self.handle, _kernel32().GetCurrentProcess()):
            return True
        self.error = ctypes.get_last_error()
        return False

    def add(self, pid):
        """A process by pid goes into the job (since Windows 8, and if it is already in another job: jobs nest)."""
        if not self.ok:
            return False
        k32 = _kernel32()
        proc = k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, int(pid))
        if not proc:
            return False
        try:
            return bool(k32.AssignProcessToJobObject(self.handle, proc))
        finally:
            k32.CloseHandle(proc)

    def terminate(self, code=1):
        if self.ok:
            _kernel32().TerminateJobObject(self.handle, code)

    def close(self):
        if self.ok:
            _kernel32().CloseHandle(self.handle)
            self.handle = None


_TTYS = {}   # pid -> _Tty: spawn_tty programs


class _Tty:
    """A spawn_tty program's pseudo-terminal: input, output, ConPTY itself and the process."""

    def __init__(self, inp, out):
        self.inp, self.out, self.pc, self.proc, self.pid = inp, out, wintypes.HANDLE(), None, None

    def close(self):
        k32 = _kernel32()
        _TTYS.pop(self.pid, None)
        # ClosePseudoConsole waits until its output is read (before Windows 11 24H2): read while it closes
        done = threading.Event()

        def drain():
            buf, n = ctypes.create_string_buffer(4096), wintypes.DWORD()
            while not done.is_set() and k32.ReadFile(self.out, buf, len(buf), ctypes.byref(n), None) and n.value:
                pass
        t = threading.Thread(target=drain, daemon=True, name="tty-drain")
        t.start()
        if self.pc:
            k32.ClosePseudoConsole(self.pc)
            self.pc = wintypes.HANDLE()
        done.set()
        t.join(2)
        for h in (self.inp, self.out, self.proc):
            if h:
                k32.CloseHandle(h)
        self.inp = self.out = self.proc = None


# ---- Windows API (ctypes) ------------------------------------------------------------------------------------------
_KILL_ON_JOB_CLOSE = 0x2000
_BREAKAWAY_OK = 0x0800
_EXTENDED_LIMIT_INFO = 9   # JobObjectExtendedLimitInformation
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_TH32CS_SNAPPROCESS = 0x00000002
_INVALID_HANDLE = (1 << (8 * __import__("struct").calcsize("P"))) - 1   # INVALID_HANDLE_VALUE as unsigned
_PSEUDOCONSOLE = 0x00020016           # PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE
_EXTENDED_STARTUPINFO = 0x00080000    # EXTENDED_STARTUPINFO_PRESENT
_UNICODE_ENVIRONMENT = 0x00000400     # CREATE_UNICODE_ENVIRONMENT
_USESTDHANDLES = 0x00000100           # STARTF_USESTDHANDLES
_FILE_APPEND_DATA = 0x0004
_SYNCHRONIZE = 0x00100000
_OPEN_ALWAYS = 4
_k32 = None

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                   "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    class _Coord(ctypes.Structure):
        _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

    class _StartupInfo(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                    ("lpTitle", wintypes.LPWSTR)] + [(n, wintypes.DWORD) for n in (
                        "dwX", "dwY", "dwXSize", "dwYSize", "dwXCountChars", "dwYCountChars", "dwFillAttribute",
                        "dwFlags")] + [("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                                       ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
                                       ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]

    class _StartupInfoEx(ctypes.Structure):
        _fields_ = [("StartupInfo", _StartupInfo), ("lpAttributeList", ctypes.c_void_p)]

    class _ProcessEntry(ctypes.Structure):   # PROCESSENTRY32W
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260)]

    class _ProcessInfo(ctypes.Structure):
        _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE), ("dwProcessId", wintypes.DWORD),
                    ("dwThreadId", wintypes.DWORD)]


def _kernel32():
    global _k32
    if _k32 is None:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        H = wintypes.HANDLE
        for name, args, res in (("CreateJobObjectW", (ctypes.c_void_p, wintypes.LPCWSTR), H),
                                ("SetInformationJobObject", (H, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD),
                                 wintypes.BOOL),
                                ("AssignProcessToJobObject", (H, H), wintypes.BOOL),
                                ("TerminateJobObject", (H, wintypes.UINT), wintypes.BOOL),
                                ("OpenProcess", (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD), H),
                                ("GetCurrentProcess", (), H),
                                ("GetExitCodeProcess", (H, ctypes.POINTER(wintypes.DWORD)), wintypes.BOOL),
                                ("CreatePipe", (ctypes.POINTER(H), ctypes.POINTER(H), ctypes.c_void_p, wintypes.DWORD),
                                 wintypes.BOOL),
                                ("CreatePseudoConsole", (_Coord, H, H, wintypes.DWORD, ctypes.POINTER(H)),
                                 ctypes.c_long),
                                ("ClosePseudoConsole", (H,), None),
                                ("InitializeProcThreadAttributeList", (ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                                                       ctypes.POINTER(ctypes.c_size_t)), wintypes.BOOL),
                                ("UpdateProcThreadAttribute", (ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t,
                                                               ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
                                                               ctypes.c_void_p), wintypes.BOOL),
                                ("DeleteProcThreadAttributeList", (ctypes.c_void_p,), None),
                                ("CreateProcessW", (wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
                                                    wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
                                                    ctypes.c_void_p, ctypes.c_void_p), wintypes.BOOL),
                                ("PeekNamedPipe", (H, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                                   ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p), wintypes.BOOL),
                                ("ReadFile", (H, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                              ctypes.c_void_p), wintypes.BOOL),
                                ("WriteFile", (H, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                               ctypes.c_void_p), wintypes.BOOL),
                                ("TerminateProcess", (H, wintypes.UINT), wintypes.BOOL),
                                ("CreateToolhelp32Snapshot", (wintypes.DWORD, wintypes.DWORD), H),
                                ("Process32FirstW", (H, ctypes.c_void_p), wintypes.BOOL),
                                ("Process32NextW", (H, ctypes.c_void_p), wintypes.BOOL),
                                ("GetProcessTimes", (H, *(ctypes.POINTER(wintypes.FILETIME),) * 4), wintypes.BOOL),
                                ("CreateFileW", (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                                 wintypes.DWORD, wintypes.DWORD, H), H),
                                ("CloseHandle", (H,), wintypes.BOOL)):
            fn = getattr(k32, name)
            fn.argtypes, fn.restype = args, res
        _k32 = k32
    return _k32


def has_browser(env, plat=None):
    """Whether there is a browser in which a person will see the sign-in page: on Windows yes (the desktop)."""
    return True


def dark_wake(plat=None, run=None):
    """Whether the machine is awake without the person (the Mac's DarkWake): Windows has no such check, so no."""
    return False


def spawn_tty(argv, cwd):
    """A program in a pseudo-terminal (ConPTY, Windows 10 1809+; it needs a terminal: `claude mcp login`) -> (pid, a handle
    for tty_read/tty_write/tty_close). The width is 2000 characters: the sign-in link is long, and a line break in the middle
    of it would break the search (2026-10-16: `hub mcp login` on Windows failed with "MCP sign-in is in the terminal")."""
    from core import i18n
    k32 = _kernel32()
    in_r, in_w, out_r, out_w = (wintypes.HANDLE() for _ in range(4))
    if not (k32.CreatePipe(ctypes.byref(in_r), ctypes.byref(in_w), None, 0)
            and k32.CreatePipe(ctypes.byref(out_r), ctypes.byref(out_w), None, 0)):
        raise OSError(i18n.t("cli.platform.pty_pipes", error=ctypes.get_last_error()))
    tty = _Tty(in_w, out_r)
    try:
        hr = k32.CreatePseudoConsole(_Coord(2000, 50), in_r, out_w, 0, ctypes.byref(tty.pc))
        if hr:
            raise OSError(i18n.t("cli.platform.pty_create", hr=f"{hr & 0xFFFFFFFF:#x}"))
        size = ctypes.c_size_t()
        k32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        attrs = ctypes.create_string_buffer(size.value)
        if not k32.InitializeProcThreadAttributeList(attrs, 1, 0, ctypes.byref(size)):
            raise OSError(i18n.t("cli.platform.pty_attrs", error=ctypes.get_last_error()))
        try:
            if not k32.UpdateProcThreadAttribute(attrs, 0, _PSEUDOCONSOLE, tty.pc, ctypes.sizeof(wintypes.HANDLE),
                                                 None, None):
                raise OSError(i18n.t("cli.platform.pty_attr", error=ctypes.get_last_error()))
            si = _StartupInfoEx()
            si.StartupInfo.cb = ctypes.sizeof(_StartupInfoEx)
            # standard handles are empty: without this the child takes the parent's redirected pipes (the hub is started
            # without a console) instead of the pseudo-terminal, and `claude` sees "stdin isn't a terminal"
            si.StartupInfo.dwFlags = _USESTDHANDLES
            si.lpAttributeList = ctypes.cast(attrs, ctypes.c_void_p)
            pi = _ProcessInfo()
            line = ctypes.create_unicode_buffer(subprocess.list2cmdline([shutil.which(argv[0]) or argv[0]] + argv[1:]))
            if not k32.CreateProcessW(None, line, None, None, False, _EXTENDED_STARTUPINFO | _UNICODE_ENVIRONMENT,
                                      None, cwd, ctypes.byref(si), ctypes.byref(pi)):
                raise OSError(i18n.t("cli.platform.pty_spawn", program=argv[0], error=ctypes.get_last_error()))
        finally:
            k32.DeleteProcThreadAttributeList(attrs)
    except BaseException:
        k32.CloseHandle(in_r)   # our own ends first: otherwise cleanup would wait for output from a pipe we hold ourselves
        k32.CloseHandle(out_w)
        tty.close()
        raise
    k32.CloseHandle(in_r)   # the child's ends belong to the pseudo-terminal
    k32.CloseHandle(out_w)
    k32.CloseHandle(pi.hThread)
    tty.proc, tty.pid = pi.hProcess, pi.dwProcessId
    _TTYS[tty.pid] = tty
    return tty.pid, tty


def tty_read(fd, timeout):
    """What the spawn_tty program printed in timeout seconds: bytes or b"" (nothing new)."""
    k32, end = _kernel32(), time.time() + timeout
    while True:
        avail = wintypes.DWORD()
        if k32.PeekNamedPipe(fd.out, None, 0, None, ctypes.byref(avail), None) and avail.value:
            buf, n = ctypes.create_string_buffer(min(avail.value, 65536)), wintypes.DWORD()
            if k32.ReadFile(fd.out, buf, len(buf), ctypes.byref(n), None):
                return buf.raw[:n.value]
        if time.time() >= end:
            return b""
        time.sleep(0.05)


def tty_write(fd, data):
    """Input to the spawn_tty program (Enter is b"\\r")."""
    n = wintypes.DWORD()
    _kernel32().WriteFile(fd.inp, data, len(data), ctypes.byref(n), None)


def tty_close(fd):
    fd.close()


def tty_poll(pid):
    """Whether the spawn_tty program has exited: the exit code or None."""
    tty = _TTYS.get(pid)
    if not tty or not tty.proc:
        return None
    code = wintypes.DWORD()
    if _kernel32().GetExitCodeProcess(tty.proc, ctypes.byref(code)) and code.value != _STILL_ACTIVE:
        return code.value
    return None


def tty_stop(pid, grace=3):
    """Stop a spawn_tty program together with its descendants (`claude.cmd` is cmd.exe, and node under it does the sign-in).
    There are no signals here, so grace is not needed."""
    if tty_poll(pid) is None and pid in _TTYS:
        _kill_tree(pid)


def tailscale_cli():
    """The Tailscale CLI: the installer puts it in Program Files, but it is not in PATH of a process started before the
    install (the hub). ProgramW6432 first: for a 32-bit Python ProgramFiles is "Program Files (x86)", and Tailscale is not installed there."""
    progs = os.environ.get("ProgramW6432") or os.environ.get("ProgramFiles") or r"C:\Program Files"
    exe = os.path.join(progs, "Tailscale", "tailscale.exe")
    return exe if os.path.exists(exe) else "tailscale"


TERMINAL = ("CONIN$", "CONOUT$")   # the console: one device to read, another to write


def ssh_install_hint():
    """The «OpenSSH Client» component (the hub bus signs with ssh-keygen): an administrator installs it."""
    return "Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0"


CLAUDE_EXE = "claude.exe"   # the Claude Code program in Anthropic's downloads and in %USERPROFILE%\.local\bin


def claude_platform(probe=None):
    """The Claude Code download platform, as Anthropic's install.ps1 picks it: win32-arm64 on ARM64, win32-x64 on x64
    (PROCESSOR_ARCHITEW6432 first: a 32-bit Python on a 64-bit Windows sees x86). None — 32-bit Windows, not supported.
    probe — the facts instead of asking the machine (tests): arch."""
    arch = ((probe or {}).get("arch") or os.environ.get("PROCESSOR_ARCHITEW6432") or os.environ.get("PROCESSOR_ARCHITECTURE")
            or "").upper()
    return {"ARM64": "win32-arm64", "AMD64": "win32-x64", "X64": "win32-x64"}.get(arch)


def claude_installed(home):
    """Where Claude Code's own `claude install` puts the command for the user with this profile folder."""
    return os.path.join(home, ".local", "bin", CLAUDE_EXE)


def browser_helper(folder, out):
    """A program for Claude Code's BROWSER that only writes the address it is given into `out` (the «Setup» page opens
    it in the person's own browser) -> (path, its text). %* is expanded once: the address's own % signs stay; Claude Code
    passes it in quotes (installer probe S0-1), so its & does not split the line."""
    return os.path.join(folder, "browser.cmd"), f'@echo(%*>>"{out}"\r\n'


def uninstall_service(unit, hub):
    """`hub uninstall`: the scheduler task goes; hub-up itself is stopped and waited for by the plan's next step
    (core/uninstall.halt) — `hub stop` here failed on a hub already stopped (`hub off`) and did not wait for it."""
    from core import i18n
    return [{"do": "run", "argv": ["schtasks", "/Delete", "/TN", unit["id"], "/F"], "service": True,
             "what": i18n.t("cli.uninstall.service_off", id=unit["id"])}]


def _user_path():
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ) as k:
        try:
            value, kind = winreg.QueryValueEx(k, "Path")
        except FileNotFoundError:
            return "", winreg.REG_EXPAND_SZ
    return value, kind


def _same_dir(a, b):
    norm = lambda p: os.path.normcase(os.path.normpath(os.path.expandvars(p.strip().strip('"'))))  # noqa: E731
    return bool(a.strip()) and norm(a) == norm(b)


def uninstall_path(state):
    """deploy/windows/install.ps1 put <state>\\bin (hub.cmd) into the user's PATH — an instance's <state>\\cmd
    (hub-<name>.cmd): the steps that take them out."""
    from core import i18n
    try:
        value, _ = _user_path()
    except OSError:
        return []
    return [{"do": "path", "path": folder, "what": i18n.t("cli.uninstall.path", path=folder)}
            for folder in (os.path.join(state, "bin"), os.path.join(state, "cmd"))
            if any(_same_dir(p, folder) for p in value.split(";"))]


def drop_user_path(folder):
    """Take `folder` out of the user's PATH (the registry, as install.ps1 wrote it) and tell open programs."""
    import ctypes
    import winreg
    value, kind = _user_path()
    rest = [p for p in value.split(";") if p and not _same_dir(p, folder)]
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, "Path", 0, kind, ";".join(rest))
    result = ctypes.c_size_t()
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x1A, 0, "Environment", 2, 5000, ctypes.byref(result))


def oneline_data(home=None, environ=None, platform=sys.platform):
    """The one-line install's own folder (deploy/oneline/README.md: <data>): %LOCALAPPDATA%\\Programs\\agents-hub."""
    env = os.environ if environ is None else environ
    base = env.get("LOCALAPPDATA") or os.path.join(home or os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(base, "Programs", "agents-hub")


def shortcut_dir(home=None, environ=None, platform=sys.platform):
    """The user's Start menu programs folder (core/shortcut.py): %APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs."""
    env = os.environ if environ is None else environ
    base = env.get("APPDATA") or os.path.join(home or os.path.expanduser("~"), "AppData", "Roaming")
    return os.path.join(base, "Microsoft", "Windows", "Start Menu", "Programs")


def processes_in(folder, platform=sys.platform):
    """Not looked for here: a folder whose program runs cannot be renamed (core/uninstall tries that first)."""
    return []


LATER = ".agents-hub-removing"   # core/uninstall.LATER


def remove_later(paths, pids):
    """The folders that cannot go while these processes run (`hub uninstall` itself: its python.exe is held open in
    the environment, and its cmd.exe reads bin\\hub.cmd once more when the command ends): a PowerShell apart waits for
    them to end (10 minutes at most), then takes each folder that still holds the mark LATER the command put in it — not
    while an install line holds its lock (a new install there removes the mark) — aside under a name of its own and
    deletes that, again for a minute while a process that has just exited still holds a file; rd of cmd deletes
    read-only files and takes paths past 260 characters through \\\\?\\. What it did goes to
    %TEMP%\\agents-hub-uninstall.log. -> True when started."""
    quote = lambda p: "'" + p.replace("'", "''") + "'"  # noqa: E731
    log = os.path.join(os.environ.get("TEMP") or os.path.expanduser("~"), "agents-hub-uninstall.log")
    script = ("$ErrorActionPreference = 'SilentlyContinue'\n"
              f"Wait-Process -Id {','.join(str(int(p)) for p in pids)} -Timeout 600\n"
              f"$paths = @({', '.join(quote(p) for p in paths)})\n"
              "foreach ($i in 1..30) {\n"
              "  $left = 0\n"
              "  foreach ($p in $paths) {\n"
              "    $aside = \"$p.removing-$PID\"\n"
              "    if (-not (Test-Path -LiteralPath $aside)) {\n"
              f"      if (-not (Test-Path -LiteralPath (Join-Path $p '{LATER}'))) {{ continue }}\n"
              "      $lock = Join-Path $p 'install.lock'\n"
              "      if (Test-Path -LiteralPath $lock -PathType Leaf) {\n"
              "        try { [IO.File]::Open($lock, 'Open', 'ReadWrite', 'None').Close() } catch { $left++; continue }\n"
              "      }\n"
              "      Rename-Item -LiteralPath $p -NewName (Split-Path $aside -Leaf)\n"
              "      if (-not (Test-Path -LiteralPath $aside)) { $left++; continue }\n"
              "    }\n"
              "    & cmd.exe /d /c \"rd /s /q `\"\\\\?\\$aside`\"\" 2>&1 | Out-Null\n"
              "    if (Test-Path -LiteralPath $aside) { $left++ }\n"
              "  }\n"
              "  if ($left -eq 0) { break }\n"
              "  Start-Sleep -Seconds 2\n"
              "}\n"
              "$said = foreach ($p in $paths) { if ((Test-Path -LiteralPath \"$p.removing-$PID\") -or "
              f"(Test-Path -LiteralPath (Join-Path $p '{LATER}'))) {{ 'left ' + $p }} elseif (Test-Path -LiteralPath $p) "
              "{ 'kept, a new one there ' + $p } else { 'done ' + $p } }\n"
              f"Add-Content -LiteralPath {quote(log)} -Value (\"$(Get-Date -Format s) hub uninstall: \" + ($said -join ', '))\n")
    import base64   # -EncodedCommand: a script with quotes and lines does not survive -Command's own parsing (winstand)
    cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
           "-EncodedCommand", base64.b64encode(script.encode("utf-16-le")).decode("ascii")]
    # by WMI, outside the caller's console and jobs: a DETACHED_PROCESS PowerShell has no console and ended at once
    # (winstand); without WMI — its own hidden console
    if popen_outside(cmd):
        return True
    flags = CREATE_NO_WINDOW | 0x00000200   # CREATE_NEW_PROCESS_GROUP
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    try:
        subprocess.Popen(cmd, creationflags=flags | BREAKAWAY_FROM_JOB, **kw)
    except PermissionError:
        subprocess.Popen(cmd, creationflags=flags, **kw)
    return True


def stop_processes(pids, grace=5.0):
    """Stop these processes of the user with their trees (TerminateProcess: no grace here). -> the pids still alive."""
    for pid in pids:
        born = process_start(pid)
        if born is not None:
            _kill_tree(pid, born=born)
    return [p for p in pids if alive(p)]


# ---- `hub run` (core/runjobs.py): a run apart from the hub, its wrapper and its command's group ---------------------

def run_apart_how(env=None):
    """How a `hub run` starts apart from the hub here: by WMI, outside every job of the hub (probe on Windows 11 as the
    ordinary user: it outlives the supervisor's job closing and `schtasks /End`)."""
    return "wmi"


def boot_id():
    """This boot of the machine: not needed here — the wrapper's job takes the command with it."""
    return None


def group_alive(pgid):
    """No process groups to check here: the wrapper's job holds the command's tree."""
    return False


def stop_group(pgid, hard=False):
    """Nothing here: the wrapper's job (KILL_ON_JOB_CLOSE) takes the command when the wrapper goes."""
    return None


def ask_stop(pid):
    """A process cannot be asked here: its tree is stopped (a `hub run` wrapper's job then takes the command)."""
    _kill_tree(pid)


def kill_now(pid):
    """Stop a process the caller knows to be its own at once, with its tree."""
    _kill_tree(pid)


def parent_of(pid):
    """The parent of a live process, or None: not needed here (the wrapper's job holds the command)."""
    return None


# --- the key of the Secrets vault (modules/secrets/vault.py) --------------------------------------------------------

SECRET_KEY_FILE = "secrets.key"   # in the settings folder; the bytes are sealed by DPAPI for this Windows user


def secret_key_file(folder, platform=sys.platform):
    """The file the vault key lives in: sealed by DPAPI, so a copy of it opens only for this user of this machine (or
    with the user's profile and password)."""
    return os.path.join(folder, SECRET_KEY_FILE)


def secret_key(account, folder, create=False, platform=sys.platform, run=None):
    """The vault key: 32 bytes, or None when there is none (and `create` is false). The file holds the key sealed by
    CryptProtectData (the current user's scope, no UI); another content is OSError — the vault says the key is lost
    rather than making a new one over it."""
    path = secret_key_file(folder)
    try:
        with open(path, "rb") as f:
            key = _dpapi(f.read(), seal=False)
        if len(key) != 32:
            raise OSError("the vault key is not a key")
        return key
    except FileNotFoundError:
        if not create:
            return None
    key = os.urandom(32)
    makedirs_private(folder)
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=SECRET_KEY_FILE + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(_dpapi(key, seal=True))
            f.flush()
            sync(f.fileno())
        try:   # a new name, never over one: another process that made its key meanwhile keeps it (its vault may be under it)
            os.link(tmp, path)   # NTFS hard link: fails when the name is taken
        except FileExistsError:
            return secret_key(account, folder, False)
        finally:
            os.unlink(tmp)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return key


def _dpapi(data, seal):
    """CryptProtectData / CryptUnprotectData of crypt32 for the current user, without any UI: OSError on failure."""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = crypt32.CryptProtectData if seal else crypt32.CryptUnprotectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                   wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    buf = ctypes.create_string_buffer(bytes(data), len(data))
    src, out = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), Blob()
    if not fn(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(out)):   # CRYPTPROTECT_UI_FORBIDDEN
        raise OSError(f"DPAPI: {ctypes.get_last_error()}")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))
