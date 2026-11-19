"""POSIX adapter (Mac, Linux). The interface is the same as platforms/windows.py."""
import contextlib
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time

from platforms import TREE_ENV, real_pid


# every account of the machine reads the command lines of this account's processes: Linux /proc/<pid>/cmdline (unless
# mounted with hidepid); macOS shows another account's processes without their arguments
ARGS_SEEN_BY_ALL = not sys.platform == "darwin"
KILLS_BY_IMAGE = False   # taskkill /IM, wmic do not exist here: the hook (hooks/deny_hint.py) does not parse them
EOF_KEYS = "cli.platform.eof_posix"   # the catalog key of the end of input in a terminal (stdin of a module command, core/modcli)
FORWARDS_FOLDERS = True   # the core fast-forwards a free lead's folder (core/daemon.forward_folders)


def config_dir():
    """The hub's settings directory, where the OS recommends keeping it: XDG."""
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "hub-core")


def state_dir():
    """The default state directory."""
    return os.path.expanduser("~/.hub-core")


def try_lock(f):
    """Lock an open file without waiting: if it is taken, OSError."""
    import fcntl
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)


def open_log(path):
    """A log descriptor for append only: every write goes to the end, whoever else writes to the file."""
    return os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)


@contextlib.contextmanager
def exclusive(path, timeout=None):
    """Lock a file for the duration of the `with` block; if another process holds it, wait — at most timeout seconds,
    then TimeoutError (None: as long as it takes)."""
    import fcntl
    with open(path, "w") as f:
        if timeout is None:
            fcntl.flock(f, fcntl.LOCK_EX)
        else:
            end = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= end:
                        raise TimeoutError(f"{path} is held by another process") from None
                    time.sleep(0.05)
        yield


def hook_command(python, hook):
    """The core's hook for Claude Code settings: the command's fields (type, command...)."""
    return {"type": "command", "command": f'"{python}" "{hook}"'}


def hook_shell(line):
    """argv running a hook's command line from settings as Claude Code runs it: /bin/sh -c (probe of 2.1.309)."""
    return ["/bin/sh", "-c", line]


# the reason for refusing pkill/killall/pgrep (hosts/common.DENY) and killing by image name: the session sees it
# (hooks/deny_hint.py)
KILL_HINT = ("Killing by pattern (pkill, killall, pgrep, `… | xargs kill`) is closed to sessions by the hub: a pattern hits "
             "other people's processes, and that is how the hub dies (`pkill -f x -u <name>` on macOS killed everything "
             "with the user's name in its command line). Find your pid: "
             "`ps -axo pid,command | grep '<what>' | grep -v grep` — and stop only your own process: `kill <pid>`. "
             "Your own background process — by the pid from `$!` at start.")


def popen_detached(cmd, env=None, **kw):
    """A process apart from the caller, outliving it: its own process group and without the session's tree marker
    (TREE_ENV), so the session that started it (agents' Chrome from its MCP launcher) does not take it along."""
    env = {k: v for k, v in (os.environ if env is None else env).items() if k != TREE_ENV}
    return subprocess.Popen(cmd, start_new_session=True, env=env, **kw)



# what a process started apart under systemd keeps of the caller's environment (a transient unit starts with none)
_OUTSIDE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "LC_MESSAGES", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "TMPDIR")


def _outside_unit(cmd, env=None, which=shutil.which):
    """argv that runs cmd as a transient unit of the user's systemd when the caller is a unit's process (INVOCATION_ID):
    the unit's stop and its restart after an exit take its whole cgroup, a child in a session of its own with it (the
    one-line install's watchdog of a new environment died with hub-up on Ubuntu 24.04); else None."""
    env = os.environ if env is None else env
    if not sys.platform.startswith("linux") or not env.get("INVOCATION_ID") or not which("systemd-run"):
        return None
    keep = [f"--setenv={k}={v}" for k, v in env.items()
            if (k.startswith("HUB_") and k != TREE_ENV) or k in _OUTSIDE_ENV]
    return ["systemd-run", "--user", "--collect", "--quiet", *keep, "--", *cmd]


def popen_outside(cmd):
    """A process apart from the caller and outliving it (Windows needs WMI for that; here a session of its own — under
    systemd a transient unit of its own, outside the caller's cgroup)."""
    unit = _outside_unit(cmd)
    if unit:
        try:
            if subprocess.run(unit, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=60).returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            pass
    popen_detached(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True

# the app's replacement (core/shellnext.py): hub-up's exit code when the service is to start the new app, and whether a
# newer app found on disk under the running hub is taken by exiting — launchd (KeepAlive) and systemd (Restart=on-failure) start it again by the app's path
APP_SWAP_EXIT = 75
APP_SWAP_ON_CHANGE = True


def bind_alone(sock):
    """Before bind: SO_REUSEADDR as http.server sets it — here it does not take a port another socket listens on, it only
    lets a restart bind past closed connections (TIME_WAIT)."""
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)


def apart():
    """subprocess arguments: the child in its own session, so a group signal (killpg(0), the terminal's Ctrl+C) aimed at
    it or its descendants does not reach the caller. For running the update self-check tests."""
    return {"start_new_session": True}


def no_terminal():
    """subprocess arguments: the child has no controlling terminal, so ssh under git will not ask for a key passphrase
    or about an unknown host through /dev/tty (stdin is irrelevant here), but fails at once; run's timeout kills its whole group."""
    return {"start_new_session": True}


def join_command(argv):
    """argv -> a line the owner can paste into this OS's shell (the bus prints its install command)."""
    import shlex
    return shlex.join(argv)


def split_command(line):
    """A command line from settings (voice.cmd) -> argv: as in a shell."""
    import shlex
    return shlex.split(line)


def kill_detached(proc, hard=False):
    """Stop a popen_detached process as a whole, its entire group; hard means SIGKILL (a child past its time). A child
    already collected (returncode set): its group is still ours while what it left lives (the number is not reused while
    the group exists), unless a process lives under the number — then the group is gone and the number is someone
    else's."""
    if real_pid(proc.pid) and not (proc.returncode is not None and alive(proc.pid)):
        os.killpg(proc.pid, signal.SIGKILL if hard else signal.SIGTERM)


def stop_detached(pid, born):
    """Stop a popen_detached process another process started, by its number and the start time written with it (it
    leads a group of its own: the group gets SIGTERM). A number now another process's, or no start time — left alone.
    -> True when signalled."""
    if born is None or not real_pid(pid) or process_start(pid) != born:
        return False
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        return False
    return True


def on_core_stop(stop, soon):
    """hubd: stop means soon() on SIGTERM (a flag; the core writes the snapshot between ticks, as on Windows). Not a
    snapshot from the handler: it came in the middle of a tick, between the reduction and writing the fact (a snapshot
    without the event, which replay skipped) or inside a write (a repeated seq)."""
    signal.signal(signal.SIGTERM, lambda *_: soon())


def on_hub_up_stop(stop):
    """hub-up: stop means stop(signum, frame) on SIGTERM and Ctrl+C."""
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)


def run_hub_up(main, script, argv, say, core_of):
    """hub-up on its own, without a supervisor; in UTF-8 (utf8_mode), as on Windows under the supervisor."""
    utf8_mode([script] + argv)
    return main(argv)


def supervisor():
    """The pid of the supervisor over hub-up, or None: none here (the parent is an OS service or a shell and may go away)."""
    return None


def restart_gap():
    """How long `hub stop` waits for hub-up to write about itself after a full restart: exec keeps the pid, so 0."""
    return 0


def ext_command(cmd):
    """The command of a plugin process (run in its manifest): as it is here."""
    return cmd


def restart_hub_up(script, argv):
    """hub-up again on the new code: exec, the same process for launchd."""
    os.execv(sys.executable, [sys.executable, script] + argv)


def background():
    """subprocess arguments for heavy work beside the hub: nothing here (priority is a Windows concern, platforms/windows)."""
    return {}


TASKPOLICY = "/usr/sbin/taskpolicy"
INTERACTIVE = "HUB_SERVICE_INTERACTIVE"   # set by the launchd agent of deploy/mac/app.py (ProcessType Interactive)


def below_hub(cmd, cwd=None, platform=sys.platform):
    """The command of heavy work beside the hub (sessions, plugin processes, archive parses), as it must start so that it
    does not take the processor from the hub's parts. macOS: the hub service runs as Interactive (deploy/mac/app.py) —
    a Standard launchd job gets light resource limits, and under load the core's loop got a fraction of a core and read
    the sessions' facts minutes late; a child would inherit Interactive, so the utility QoS clamp puts it back at the
    level every job of a Standard service has. Only under that service (its mark INTERACTIVE): a service installed
    before it, or a hub started from a terminal, keeps its children where they were. taskpolicy execs the command: the
    pid and the argv stay the program's. cwd: the folder the command starts in (a program path relative to it).
    Elsewhere unchanged (Linux: give_way for the archive parse; Windows: background())."""
    if platform != "darwin" or os.environ.get(INTERACTIVE) != "1" or not os.path.exists(TASKPOLICY):
        return list(cmd)
    prog = cmd[0] if not cwd or cmd[0].startswith("/") or "/" not in cmd[0] else cwd.rstrip("/") + "/" + cmd[0]
    if not shutil.which(prog):   # a missing program still raises at Popen, as without the clamp
        return list(cmd)
    return [TASKPOLICY, "-c", "utility", *cmd]


GIVE_WAY = 10   # the nice increment of a background job that must not slow the hub


def give_way():
    """This process yields the CPU to the hub: a long background job of the hub itself (the feed's archive parses after
    a parse version bump — hours of CPU on a big hub). On Windows the priority is given at the start (background())."""
    try:
        os.nice(GIVE_WAY)
    except OSError:   # not allowed here: the job runs as it is
        pass


def spawn_part(cmd, core, tree=False, **kw):
    """A hub part under hub-up -> (process, stop handle: not needed here, SIGTERM). tree is not needed either: `exec`
    in a plugin's run.sh replaces sh with the plugin's process."""
    return subprocess.Popen(cmd, **kw), None


def stop_part(proc, handle):
    """Ask a part to stop: SIGTERM (hubd writes the snapshot after the tick)."""
    if proc.poll() is None:
        proc.terminate()


def stop_grace(core):
    """How long to wait for a part after stop_part. The core writes the snapshot after a tick, and a tick can be long (git:
    up to 30 s per call), so it gets longer. OS services give hub-up even more before killing it (deploy/mac/app.py
    STOP_TIMEOUT, TimeoutStopSec in deploy/install.sh)."""
    return 60 if core else 15


def session_tree(popen):
    """A session's process tree: not kept here, the claude process itself is stopped, as before."""
    return None


def terminate_session(popen, tree):
    """Stop the session's process."""
    popen.terminate()


LINUX = sys.platform.startswith("linux")
APP_OS = "mac" if sys.platform == "darwin" else "linux"   # the app's name of this OS (core/appmove.py)
CASE_BLIND = sys.platform == "darwin"   # the default file system (APFS) ignores case: .ENV is .env (hooks/repo_rules.py)


def console_encoding():
    """The encoding of the .cmd files the hub writes (Windows: the console code page); here UTF-8."""
    return "utf-8"


def system_language():
    """The language tag the system speaks, for the hub's first greeting before its owner has written ('ru-RU'); '' when
    it does not say. A Mac keeps it apart from LANG (launchd gives the hub none), the first of its languages."""
    tag = ""
    if sys.platform == "darwin":
        try:   # ( "ru-RU", "en-US" ) on lines of their own
            done = subprocess.run(["defaults", "read", "-g", "AppleLanguages"], capture_output=True, text=True,
                                  timeout=5)
            tag = next(iter(re.findall(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]+)*", done.stdout)), "") if done.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            tag = ""
    # a stray LC_ALL=C of a service manager must not hide the language the user's LANG names
    tags = [tag, *(os.environ.get(k) or "" for k in ("LC_ALL", "LC_MESSAGES", "LANG"))]
    tag = next((t for t in (re.split(r"[.@]", t)[0] for t in tags) if t and t not in ("C", "POSIX")), "")
    return tag.replace("_", "-")


PROC = "/proc"


def oom_first(pid, score):
    """Linux: a process (and everything it starts later, which inherits it) goes earlier than others as a victim of the OS
    kernel when memory runs out: oom_score_adj no lower than score. Only upward: the user can't lower it, and the test run
    (1000) does not make its sessions more valuable. Errors silently: the process is gone, /proc is closed. Mac: unchanged (2026-10-23)."""
    if not LINUX:
        return
    path = os.path.join(PROC, str(pid), "oom_score_adj")
    try:
        with open(path) as f:
            if int(f.read().strip()) >= score:
                return
        with open(path, "w") as f:
            f.write(str(score))
    except (OSError, ValueError):
        pass


def keep_on_oom(cgroup="/proc/self/cgroup", home=None, run=subprocess.run):
    """hub-up is the main process of its systemd --user service, and its file has no OOMPolicy: a drop-in
    `<unit>.d/oom.conf` with OOMPolicy=continue and daemon-reload (the service is not restarted, the rule takes effect at once).
    Without it, systemd by default (stop) stops the whole hub when the OS kernel has killed one process for lack of memory
    (2026-10-23); with it, only the victim dies: hub-up restarts the failed part, the core restarts the session. The installer
    writes the rule into the unit itself (deploy/install.sh); this is for units of earlier installs: a code update does not
    rewrite them. The owner's own OOMPolicy is left alone, wherever it lies (the unit, its drop-ins in any systemd folder:
    `systemctl --user cat` shows all of it). A unit not from the user's folder (system, foreign) is not ours. True means it was put in place."""
    import re
    own = _own_unit(cgroup)
    if not own or not own[1]:
        return False
    unit = os.path.join(_units_dir(home), own[0])
    if not os.path.isfile(unit):
        return False
    try:
        r = run(["systemctl", "--user", "cat", own[0]], capture_output=True, text=True, timeout=10,
                stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    if r.returncode != 0 or re.search(r"^\s*OOMPolicy\s*=", r.stdout or "", re.M):
        return False
    path = os.path.join(unit + ".d", "oom.conf")
    try:
        os.makedirs(unit + ".d", exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            f.write("[Service]\nOOMPolicy=continue\n")
        os.replace(path + ".tmp", path)
    except OSError:
        return False
    try:
        run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        pass   # it did not reload: takes effect at the next start of the service
    return True


def end_tree(tree):
    """claude is gone: we do not clean up after it here, as before: its commands live in their own OS sessions (Claude Code's
    setsid), and end_tagged stops them by the marker when the session is gone for good."""


def _ps_tags(plain, with_env):
    """macOS: the environment is not in /proc; `ps -E` prints it after the command line. The marker comes from the environment
    only: the tail of the `ps -wwE` line after the same `ps -ww` line (arguments with the text "HUB_TREE=..." are foreign,
    not the marker). The value runs to the next " NAME=" or the end: the hub folder may contain spaces. -> {pid: TREE_ENV value}."""
    import re

    def lines(out):
        rows = {}
        for line in out.splitlines():
            pid, _, rest = line.strip().partition(" ")
            if pid.isdigit():
                rows[int(pid)] = rest
        return rows
    args, found = lines(plain), {}
    for pid, rest in lines(with_env).items():
        tail = rest[len(args[pid]):] if pid in args and rest.startswith(args[pid]) else None
        if tail is None or tail[:1] not in ("", " "):
            continue   # was born or changed exec between the ps calls
        m = re.search(rf"(?:^|\s){TREE_ENV}=(.*?)(?=\s+[A-Za-z_][A-Za-z0-9_]*=|$)", tail)
        if m:
            found[pid] = m.group(1)
    return found


def _tags():
    """{pid: marker} of own processes (only the user's own). Linux: /proc/<pid>/environ, macOS: `ps -E`."""
    if os.path.isdir("/proc/self"):
        found, me, key = {}, os.getuid(), (TREE_ENV + "=").encode()
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                if os.stat(f"/proc/{d}").st_uid != me:
                    continue
                with open(f"/proc/{d}/environ", "rb") as f:
                    env = f.read()
            except OSError:
                continue
            for item in env.split(b"\0"):
                if item.startswith(key):
                    found[int(d)] = item[len(key):].decode("utf-8", "replace")
                    break
        return found
    try:
        # ps itself without the caller's marker: otherwise it would find itself in the list (checked on Mac, 2026-10-16)
        env = {k: v for k, v in os.environ.items() if k != TREE_ENV}
        plain, with_env = (subprocess.run(["ps", flags, "-U", str(os.getuid()), "-o", "pid=,command="],
                                          capture_output=True, text=True, errors="replace", timeout=20, env=env).stdout
                           for flags in ("-ww", "-wwE"))
    except (OSError, subprocess.SubprocessError):
        return {}
    return _ps_tags(plain, with_env)


def _stat(pid):
    """Linux: (parent, state) from /proc/<pid>/stat; None means no such process or not Linux."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().rsplit(")", 1)[1].split()
        return int(fields[1]), fields[0]
    except (OSError, ValueError, IndexError):
        return None


def _parents():
    """The caller itself and all above it: not to be stopped even if they carry the marker (hub-up started from a session)."""
    mine, pid = {os.getpid()}, os.getppid()
    while real_pid(pid) and pid not in mine:
        mine.add(pid)
        st = _stat(pid)
        pid = st[0] if st else _parent_of(pid)
    return mine


def _running(pid):
    """Alive and not a zombie (a zombie waits for its child's wait, signal 0 still sees it)."""
    st = _stat(pid)
    return st[1] != "Z" if st else alive(pid) and not os.path.isdir("/proc/self")


def end_tagged(match, grace=3.0):
    """Stop the processes whose tree marker (TREE_ENV) fits match(value): SIGTERM, and after grace seconds SIGKILL for
    those still alive and still with the same marker (the pid may have gone to another process). It does not touch itself
    or its ancestors. -> how many processes were found."""
    skip = _parents()
    found = [p for p, tag in _tags().items() if p not in skip and real_pid(p) and match(tag)]
    for pid in found:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    left, end = found, time.time() + grace
    while left and time.time() < end:
        time.sleep(0.1)
        left = [p for p in left if _running(p)]
    if left:
        still = _tags()
        for pid in left:
            if pid in still and match(still[pid]):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
    return len(found)


def reap(pid, session_id):
    """A session process from a previous life of hubd: if it is that one (the sessionId is in the command line), stop it. True means stopped."""
    try:
        cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
    except OSError:
        return False
    if session_id not in cmd or "stream-json" not in cmd or not real_pid(pid):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except PermissionError:
        return False   # not ours: the number went to another user's process
    except OSError:
        return True    # gone
    try:
        for _ in range(50):
            os.kill(pid, 0)
            time.sleep(0.1)
        # still there after 5 s: the same session, unless it exited and the number went to another process meanwhile
        if session_id in command_line(pid):
            os.kill(pid, signal.SIGKILL)
    except OSError:
        pass  # gone (and the number may have gone to another user's process meanwhile)
    return True


def _tag_of(pid):
    """The tree marker (TREE_ENV) of one own process, or None. Linux: its /proc/<pid>/environ; macOS: the `ps -E` list."""
    if not os.path.isdir("/proc/self"):
        return _tags().get(pid)
    try:
        if os.stat(f"/proc/{pid}").st_uid != os.getuid():
            return None
        with open(f"/proc/{pid}/environ", "rb") as f:
            env = f.read()
    except OSError:
        return None
    key = (TREE_ENV + "=").encode()
    return next((item[len(key):].decode("utf-8", "replace") for item in env.split(b"\0") if item.startswith(key)), None)


def reap_tagged(pid, match):
    """A session process from a previous life of hubd whose command line carries no session id (Codex app-server): if
    it is alive, ours and match(its tree marker, its command line), stop it as reap does. True means stopped."""
    if not real_pid(pid) or not _running(pid):
        return False
    tag = _tag_of(pid)
    if tag is None or not match(tag, command_line(pid)):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except PermissionError:
        return False
    except OSError:
        return True
    for _ in range(50):
        if not _running(pid):
            return True
        time.sleep(0.1)
    if _tag_of(pid) == tag:   # still the same process after 5 s (the number did not go to another one meanwhile)
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return True


def page_path(path):
    """A path as the page asked for it (file:///x is "/x") as a machine path: here "/x" is the path."""
    return path


def native_path(path):
    """Whether this is a path here: a path with a Windows drive (C:) is not a path on POSIX, it can't be made absolute."""
    return not (len(path) >= 2 and path[1] == ":")


def long_path(path):
    """A path for file calls: there is no 260-character limit here, as is."""
    return path


def rmdir(path):
    """Remove an empty folder; a refusal (PermissionError: the parent is not writable) is the caller's to handle."""
    os.rmdir(path)


def cut_junctions(path):
    """Junctions are Windows only; git and rmtree here do not walk through a symlink, nothing to cut. -> 0."""
    return 0


def rmtree(path):
    """Delete a folder entirely; skip what could not be deleted (like ignore_errors, retrying after chmod). -> whether the
    folder remained."""
    import shutil

    def again(func, p, _):
        # rmtree works through descriptors: func may be os.open/os.scandir, which can't be retried without arguments; skip
        if func not in (os.unlink, os.remove, os.rmdir) or os.path.islink(p):   # chmod of a link changes its target
            return
        try:
            os.chmod(p, 0o700)
            func(p)
        except OSError:
            pass
    shutil.rmtree(path, onexc=again)
    return os.path.exists(path)


def has_exec_bit(path):
    """The file has the execute bit set (a bundled program without an extension is recognised by it)."""
    return os.access(path, os.X_OK)


def alive(pid):
    """Whether the process is alive: signal 0; a foreign live process (EPERM) is alive."""
    if not real_pid(pid):
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def process_start(pid):
    """When the process started, or None (no such process, not readable): with the pid it names one process, since a
    number written to a file may go to another one. Linux: clock ticks since boot (/proc/<pid>/stat, field 22); macOS:
    `ps -o lstart=` in the C locale (to the second)."""
    if not real_pid(pid):
        return None
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


def service():
    """hub-up is itself the process of an OS service -> the command that stops it; otherwise None. The environment and
    process group of a service are inherited by all its descendants, so hub-up from a hub session under a service, from
    tmux or an editor under systemd is not a service (review: `hub stop` advised stopping a foreign service, up to a
    working hub). systemd: hub-up is the main process of the unit (SYSTEMD_EXEC_PID, systemd 248+); the unit is from its
    own process group, `--user` if it is under user@. launchd (the install agent without HUB_SERVICE): the job label with
    a file in ~/Library/LaunchAgents and the parent is launchd (itself or through the package launcher "Agents Hub.app").
    A descendant of hub-up (the HUB_UP_CHILD marker, core/restart.CHILD) is not a service: `nohup hub-up &` from a session
    under launchd becomes an orphan and moves to launchd with its XPC_SERVICE_NAME."""
    if os.environ.get("HUB_UP_CHILD"):
        return None
    label = os.environ.get("XPC_SERVICE_NAME") or ""
    if sys.platform == "darwin" and label and 1 in (os.getppid(), _parent_of(os.getppid())) and os.path.exists(
            os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents", label + ".plist")):
        return f"launchctl bootout gui/{os.getuid()}/{label}"
    own = _own_unit()
    if own:
        return f"systemctl --user stop {own[0]}" if own[1] else f"sudo systemctl stop {own[0]}"
    return None


def _own_unit(cgroup="/proc/self/cgroup"):
    """(unit, under systemd --user): the systemd unit whose main process is this process; otherwise None."""
    if os.environ.get("SYSTEMD_EXEC_PID") != str(os.getpid()):
        return None
    try:
        with open(cgroup) as f:
            group = f.read().strip().splitlines()[-1]
    except (OSError, IndexError):
        group = ""
    unit = group.rsplit("/", 1)[-1]
    return (unit, "/user@" in group) if unit.endswith(".service") else None


def _units_dir(home=None):
    home = home or os.path.expanduser("~")
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"), "systemd", "user")


def _runs_hubd(path, repo, state=None):
    """The unit file runs this repo's core (bin/hubd), not hub-up; state: also with this state directory."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return False
    if not any(os.path.join(repo, "bin", "hubd") in x.split() for x in lines if x.startswith("ExecStart=")):
        return False
    env = dict(x.split("=", 2)[1:] for x in lines if x.startswith("Environment=") and x.count("=") >= 2)
    return state is None or os.path.realpath(env.get("HUB_STATE_DIR") or state_dir()) == os.path.realpath(state)


def old_units(repo, state, home=None):
    """systemd --user services of the earlier Linux layout (before 2026-10-06: a service per part, hubd, hub-web..., no
    hub-up) whose core is from this repo: [unit file path]. On such a layout there are no parts that exist only under
    hub-up (statistics, conduit, update...), and nobody can carry out `hub restart`."""
    import glob
    if sys.platform == "darwin":
        return []
    return [p for p in sorted(glob.glob(os.path.join(_units_dir(home), "*.service"))) if _runs_hubd(p, repo, state)]


def old_service(repo, home=None, cgroup=None):
    """This process is the core held by a service of the earlier layout (old_units) -> the path of its file; otherwise None."""
    if sys.platform == "darwin" or os.environ.get("HUB_UP_CHILD"):
        return None
    own = _own_unit(cgroup or "/proc/self/cgroup")
    if not own or not own[1]:
        return None
    path = os.path.join(_units_dir(home), own[0])
    return path if _runs_hubd(path, repo) else None


ONELINE_START = "/app/start.py"   # the one-line install's service runs <state>/app/start.py (tools/package/start.py)


def service_unit(state_dir, home=None, platform=sys.platform):
    """The OS service that holds the hub with this state directory (`hub on` / `hub off`), or None: a launchd agent from
    ~/Library/LaunchAgents (macOS) or a systemd --user unit (Linux) with hub-up and this hub's HUB_STATE_DIR: a second hub
    on the machine finds its own (or the Mac app's agent, which runs the app with --serve; or the one-line install's,
    which runs start.py of the environment through the pointer <state>/app — deploy/oneline/setup.py). -> {"kind", "id",
    "path", "port", "files_port"}: HUB_WEB_PORT and HUB_FILES_PORT from its environment, if set."""
    import glob
    home = home or os.path.expanduser("~")
    want = os.path.realpath(state_dir)
    if platform == "darwin":
        import plistlib
        from xml.parsers.expat import ExpatError
        for path in sorted(glob.glob(os.path.join(home, "Library", "LaunchAgents", "*.plist"))):
            try:
                with open(path, "rb") as f:
                    agent = plistlib.load(f)
            except (OSError, ValueError, plistlib.InvalidFileException, ExpatError):   # a hand-written agent launchd still loads
                continue
            env = agent.get("EnvironmentVariables") or {}
            args = [str(a) for a in agent.get("ProgramArguments") or []]
            # the downloaded app (installer S5): its shell with --serve becomes hub-up (deploy/mac/appshell.py)
            app = len(args) == 2 and args[1] == "--serve" and args[0].endswith("/Contents/MacOS/Agents Hub")
            if (app or any(a.endswith(("hub-up", ONELINE_START)) for a in args)) \
                    and os.path.realpath(env.get("HUB_STATE_DIR") or "") == want:
                return {"kind": "launchd", "id": agent.get("Label") or "", "path": path, "port": _port(env),
                        "files_port": _port(env, "HUB_FILES_PORT")}
        return None
    for path in sorted(glob.glob(os.path.join(_units_dir(home), "*.service"))):
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        env = dict(x.split("=", 2)[1:] for x in lines if x.startswith("Environment=") and x.count("=") >= 2)
        if any(x.startswith("ExecStart=") and ("hub-up" in x or ONELINE_START in x) for x in lines) \
                and os.path.realpath(env.get("HUB_STATE_DIR") or "") == want:
            return {"kind": "systemd", "id": os.path.basename(path), "path": path, "port": _port(env),
                    "files_port": _port(env, "HUB_FILES_PORT")}
    return None


def service_commands(unit, uid=None):
    """Service commands: turn on, turn off, is it running (code 0 means yes)."""
    if unit["kind"] == "launchd":
        uid = os.getuid() if uid is None else uid
        return {"on": ["launchctl", "bootstrap", f"gui/{uid}", unit["path"]],
                "off": ["launchctl", "bootout", f"gui/{uid}/{unit['id']}"],
                "status": ["launchctl", "print", f"gui/{uid}/{unit['id']}"]}
    return {"on": ["systemctl", "--user", "start", unit["id"]], "off": ["systemctl", "--user", "stop", unit["id"]],
            "status": ["systemctl", "--user", "is-active", "--quiet", unit["id"]]}


LINGER = "/var/lib/systemd/linger"   # logind's mark per user whose manager lingers (readable by anyone)


def lingers():
    """systemd keeps this user's manager without a sign-in (loginctl enable-linger, which the installer asks for): a
    stopped service of theirs starts again at the next boot only; without it, at the next sign-in as well."""
    import pwd   # the user of this process, not $USER (sudo -E, su keep someone else's)
    return os.path.isfile(os.path.join(LINGER, pwd.getpwuid(os.getuid()).pw_name))


def _port(env, key="HUB_WEB_PORT"):
    try:
        return int(env.get(key) or 0) or None
    except ValueError:
        return None

def reveal_command(path, platform=sys.platform):
    """Show a file in its folder: Finder with the file selected (macOS), otherwise the folder in a file manager."""
    return ["open", "-R", path] if platform == "darwin" else ["xdg-open", os.path.dirname(path)]


# the file manager's desktop-entry name (lower case, a part of it) -> the id the page turns into the button's name
FILE_MANAGERS = (("nautilus", "files"), ("elementary.files", "files"), ("org.gnome.files", "files"),
                 ("dolphin", "dolphin"), ("thunar", "thunar"), ("nemo", "nemo"), ("pcmanfm", "pcmanfm"), ("caja", "caja"))


def _graphical_login(env, which, run):
    """Does this user have a screen although the service's own environment has no display: the systemd user manager
    knows DISPLAY or WAYLAND_DISPLAY, or a login session of the user is x11 or wayland (a tty or ssh one is not)."""
    def ask(*cmd):
        if not which(cmd[0]):
            return ""
        try:
            return run(list(cmd), capture_output=True, text=True, timeout=2).stdout or ""
        except (OSError, subprocess.SubprocessError):
            return ""
    if any(line.startswith(("DISPLAY=", "WAYLAND_DISPLAY=")) for line in ask("systemctl", "--user", "show-environment").split("\n")):
        return True
    sessions = [env["XDG_SESSION_ID"]] if env.get("XDG_SESSION_ID") else [
        cols[0] for cols in (line.split() for line in ask("loginctl", "list-sessions", "--no-legend").split("\n"))
        if len(cols) > 1 and cols[1] == str(os.getuid())][:10]
    return any(ask("loginctl", "show-session", sid, "-p", "Type", "--value").strip() in ("x11", "wayland")
               for sid in sessions)


def file_manager(env=None, platform=sys.platform, which=shutil.which, run=subprocess.run):
    """What the hub machine shows a folder in, as an id for the page: "finder" (macOS), a known Linux file manager's id
    ("files", "dolphin", ...) or "folder" (a desktop whose manager is not known); None — no desktop, nothing to open a
    window on. Linux has a desktop when xdg-open exists and the user has a display: in the service's environment
    (DISPLAY, WAYLAND_DISPLAY) or, for a systemd --user service that was not given it, in the user manager's environment
    or a graphical login session."""
    if platform == "darwin":
        return "finder"
    env = os.environ if env is None else env
    if not which("xdg-open"):
        return None
    if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")) and not _graphical_login(env, which, run):
        return None
    try:
        out = run(["xdg-mime", "query", "default", "inode/directory"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return "folder"
    name = (out or "").strip().lower()
    return next((fm for part, fm in FILE_MANAGERS if part in name), "folder")


def _parent_of(pid):
    try:
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, timeout=10).stdout
        return int(out.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def replace(src, dst):
    """Atomically replace dst with the file src."""
    os.replace(src, dst)


def sync(fd, platform=sys.platform):
    """Flush an open file to the disk itself. On macOS fsync stops in the drive's cache, which is lost on power loss:
    F_FULLFSYNC goes through it; a filesystem without it gets a plain fsync."""
    if platform == "darwin":
        import fcntl
        try:
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
            return
        except (AttributeError, OSError):
            pass
    os.fsync(fd)


def sync_dir(path):
    """A rename or a new name in a folder survives power loss only when the folder itself is synchronised.
    Best effort: the rename has already happened, and some filesystems do not synchronise a folder."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        sync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def makedirs_private(path):
    """A folder of the hub that others must not read."""
    os.makedirs(path, mode=0o700, exist_ok=True)


def link_dir(src, link):
    """link is a link to the folder src."""
    os.symlink(src, link)


def run(args, timeout, **kw):
    """subprocess.run with a timeout. A child in its own session (start_new_session, see apart): on a timeout or an
    interrupt we stop its whole group: tools/test.py executors and their descendants, not only the launcher."""
    if not kw.get("start_new_session"):
        return subprocess.run(args, timeout=timeout, **kw)
    check, data = kw.pop("check", False), kw.pop("input", None)
    if kw.pop("capture_output", False):
        kw["stdout"] = kw["stderr"] = subprocess.PIPE
    if data is not None:
        kw["stdin"] = subprocess.PIPE
    with subprocess.Popen(args, **kw) as p:
        try:
            out, err = p.communicate(data, timeout=timeout)
        except BaseException as e:
            # as subprocess.run: both a timeout and Ctrl+C / exit by a signal stop the child, otherwise Popen.__exit__ would
            # wait out the whole run (review: up to 20 min with the machine lock). The child is already reaped (communicate
            # reached wait): we leave its group alone, since the number could have gone to another. Not poll(): it would
            # reap a launcher that has exited, while the group with executors that hold the pipes would stay alive
            if p.returncode is None and real_pid(p.pid):
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    p.kill()
            if isinstance(e, subprocess.TimeoutExpired):
                p.communicate()   # read the pipes to the end: the descendants are killed, they will close
            raise
    if check and p.returncode:
        raise subprocess.CalledProcessError(p.returncode, args, out, err)
    return subprocess.CompletedProcess(args, p.returncode, out, err)


def command_line(pid):
    """A process's command line, or "" (no such process / no access). Linux: /proc, which a container without `ps`
    (procps) has too — there `hub stop` and `hub uninstall` took a running hub-up for gone; macOS: ps."""
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except FileNotFoundError:
        if os.path.isdir("/proc/self"):
            return ""   # /proc is there: no such process
    except (OSError, ValueError):
        return ""
    try:
        return subprocess.run(["ps", "-o", "command=", "-p", str(int(pid))], capture_output=True, text=True,
                              timeout=30).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def claude_problem(version):
    """What this Claude Code version lacks for the hub on this OS: a text for the owner, or None."""
    return None


def python():
    """Python for the hub's own processes that hub-up does not start (the MCP server in a session)."""
    return [sys.executable]


def utf8_mode(argv):
    """The hub's own Python process in UTF-8: output (claude reads it) and the hub's files. The locale is usually UTF-8
    anyway; if it is not (LC_ALL=C with PYTHONUTF8=0: a server, a container, cron; printing Russian failed with
    UnicodeEncodeError), the same process again with -X utf8 (exec: same pid). We do not put PYTHONUTF8 in the
    environment, since the owner's scripts would inherit it. argv is the script path and arguments; interpreter flags (-u, -I...) stay."""
    import locale
    if sys.flags.utf8_mode or locale.getencoding().lower().replace("-", "").replace("_", "") == "utf8":
        return
    sys.stdout.flush()
    sys.stderr.flush()
    flags = sys.orig_argv[1:len(sys.orig_argv) - len(sys.argv)]
    os.execv(sys.executable, [sys.executable, *flags, "-X", "utf8"] + argv)


def read_stdin():
    """The whole stdin of a `hub ... -` command, as text: UTF-8, a byte that is not UTF-8 becomes U+FFFD (sys.stdin is
    strict under a UTF-8 locale and the command failed with UnicodeDecodeError). POSIX text mode does not translate line
    ends, so the bytes are the same text."""
    return sys.stdin.buffer.read().decode("utf-8", "replace")


def output_text(raw):
    """A child program's output (bytes) as text: UTF-8, broken bytes replaced, no failure."""
    return raw.decode("utf-8", "replace")


def umask(mask):
    """Set this process's umask -> the previous one (core/perms.py)."""
    return os.umask(mask)


def file_modes():
    """Whether the owner/group/other bits keep a file closed here (core/perms.py): yes."""
    return True


def owner_env(env):
    """The environment for the owner's commands (sessions, schedule jobs) from the hub's environment."""
    return env


def nested(shim, state_dir):
    """The `claude` wrapper for a session (hosts/nested/claude) -> (a folder first in PATH, the session's environment
    variables): it runs by itself, through the shebang."""
    return os.path.dirname(shim), {}


def job_shell(command, env, state_dir, shell):
    """A schedule command job or pre-check (a shell string) -> (what to start, shell=, env): /bin/sh, as is. hub.json
    `schedule.shell` is for Windows only."""
    return command, True, env


def job_quote(args, shell):
    """A schedule command from a list of arguments (the core's own pre-checks): in double quotes, as before."""
    return " ".join(f'"{a}"' for a in args)


def exec_instead(argv):
    """This process becomes the command argv (the hosts/nested/claude wrapper becomes the real claude): exec."""
    os.execv(argv[0], argv)


def spec_loader():
    """The command that starts a program from a private spec file {"env", "umask"} and removes the file; the file's
    path and the program's argv follow (hosts/tmux.py: keys stay off the command line). The variables go on top of the
    process's own (a tmux pane's TERM, TMUX); isolated (-I): it runs in the session's project, whose json.py must not
    replace the stdlib's."""
    return [sys.executable, "-I", "-c", "import json, os, sys\n"
            "path = sys.argv[1]\n"
            "with open(path, encoding='utf-8') as f:\n    spec = json.load(f)\n"
            "os.unlink(path)\n"
            "if spec['umask'] >= 0:\n    os.umask(spec['umask'])\n"
            "os.execvpe(sys.argv[2], sys.argv[2:], {**os.environ, **spec['env']})\n"]


def has_browser(env, plat=None):
    """Whether there is a browser in which a person will see the sign-in page: macOS yes, Linux only with a display (DISPLAY
    or WAYLAND_DISPLAY); a server without a display, no (2026-10-11: MCP sign-in goes as a link, core/mcplogin.py)."""
    plat = plat or sys.platform
    return plat == "darwin" or bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))


GRAPHICS = 0x2   # kIOPMSystemCapabilityGraphics (IOKit IOPMPrivate.h): a full wake has it, a dark wake does not


def dark_wake(plat=None, run=None):
    """Whether the Mac is awake without the person: a maintenance wake or Power Nap between sleeps (DarkWake), the kernel's
    system capabilities without graphics; it sleeps again in a minute or three (2026-11-03: the update suite started in
    such wakes and ran 3 hours). Linux, or ioreg did not answer: no, as before."""
    if (plat or sys.platform) != "darwin":
        return False
    try:
        out = (run or subprocess.run)(["/usr/sbin/ioreg", "-r", "-d1", "-n", "IOPMrootDomain"], capture_output=True,
                                      text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    caps = re.search(r'"System Capabilities" = (\d+)', out or "")
    return bool(caps) and not int(caps.group(1)) & GRAPHICS


def spawn_tty(argv, cwd):
    """A program in a pseudo-terminal (it needs a terminal: `claude mcp login`, 2026-10-07) -> (pid, fd to read the output)."""
    import pty
    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.chdir(cwd)
            os.execvp(argv[0], argv)
        finally:
            os._exit(127)
    return pid, fd


def tty_read(fd, timeout):
    """What the spawn_tty program printed in timeout seconds: bytes or b"" (nothing new, output closed)."""
    import select
    if not select.select([fd], [], [], timeout)[0]:
        return b""
    try:
        return os.read(fd, 4096)
    except OSError:
        return b""


def tty_write(fd, data):
    """Input to the spawn_tty program (Enter is b"\\r")."""
    os.write(fd, data)


def tty_close(fd):
    os.close(fd)


def tty_poll(pid):
    """Whether the spawn_tty program has exited: the exit code or None."""
    done, status = os.waitpid(pid, os.WNOHANG)
    return os.waitstatus_to_exitcode(status) if done else None


def tty_stop(pid, grace=3):
    """Stop a spawn_tty program: SIGTERM, and after grace seconds SIGKILL. `claude setup-token` does not hear SIGTERM at
    the code prompt, and a wait without a limit hung the caller (2026-10-07)."""
    import time
    if not real_pid(pid):
        return
    try:
        os.kill(pid, 15)
        end = time.time() + grace
        while time.time() < end:
            if os.waitpid(pid, os.WNOHANG)[0]:
                return
            time.sleep(0.1)
        os.kill(pid, 9)
        os.waitpid(pid, 0)
    except OSError:
        pass


def tailscale_cli():
    """The Tailscale CLI: on Mac it is inside the app (usually not in PATH), on Linux `tailscale` from PATH."""
    app = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    return app if os.path.exists(app) else "tailscale"


TERMINAL = ("/dev/tty", "/dev/tty")   # where a yes is asked while stdin carries data (to read, to write)


def ssh_install_hint():
    """How OpenSSH comes to this machine (the hub bus signs with ssh-keygen): macOS has it always."""
    return None if sys.platform == "darwin" else "sudo apt install openssh-client   # or: sudo dnf install openssh-clients"


CLAUDE_EXE = "claude"   # the Claude Code program in Anthropic's downloads and in ~/.local/bin (core/claudeinstall)


def claude_platform(probe=None):
    """The Claude Code download platform of this machine, as Anthropic's install.sh picks it: darwin-arm64|darwin-x64|
    linux-x64|linux-arm64, -musl on a musl Linux; an arm Mac under Rosetta gets arm64. None — not supported.
    probe — the facts instead of asking the machine (tests): system, machine, translated, musl."""
    import platform
    probe = probe or {}
    system = probe.get("system") or platform.system()
    machine = (probe.get("machine") or platform.machine()).lower()
    translated, musl = probe.get("translated"), probe.get("musl")
    os_name = {"Darwin": "darwin", "Linux": "linux"}.get(system)
    arch = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(machine)
    if not os_name or not arch:
        return None
    if os_name == "darwin" and arch == "x64":
        if translated is None:
            try:   # the full path: launchd and `env -i` give a PATH without /usr/sbin
                r = subprocess.run(["/usr/sbin/sysctl", "-n", "sysctl.proc_translated"], capture_output=True,
                                   text=True, stdin=subprocess.DEVNULL, timeout=10, check=False)
                translated = r.stdout.strip() == "1"
            except (OSError, subprocess.SubprocessError):
                translated = False
        if translated:
            arch = "arm64"
    if os_name == "linux":
        if musl is None:
            musl = any(os.path.exists(f"/lib/libc.musl-{m}.so.1") for m in ("x86_64", "aarch64"))
            if not musl:
                try:
                    r = subprocess.run(["ldd", "/bin/ls"], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                                       timeout=10, check=False)
                    musl = "musl" in (r.stdout + r.stderr)
                except (OSError, subprocess.SubprocessError):
                    musl = False
        return f"linux-{arch}-musl" if musl else f"linux-{arch}"
    return f"{os_name}-{arch}"


def claude_installed(home):
    """Where Claude Code's own `claude install` puts the command for the user with this home folder."""
    return os.path.join(home, ".local", "bin", CLAUDE_EXE)


def browser_helper(folder, out):
    """A program for Claude Code's BROWSER that only writes the address it is given into `out` (the «Setup» page opens
    it in the person's own browser) -> (path, its text)."""
    quoted = "'" + out.replace("'", "'\\''") + "'"
    return os.path.join(folder, "browser"), f"#!/bin/sh\nprintf '%s\\n' \"$1\" >> {quoted}\n"


def uninstall_service(unit, hub):
    """`hub uninstall`: the steps that stop the hub and take its OS service off (core/uninstall.plan). A service that is
    not loaded any more is no failure (launchctl: 3, 5, 113; systemctl disable: 1). "service": the step that turns the
    service off — when it fails, the plan does not stop a hub-up the service may start again."""
    from core import i18n
    if unit["kind"] == "launchd":
        steps = [{"do": "run", "argv": ["launchctl", "bootout", f"gui/{os.getuid()}/{unit['id']}"], "ok": (3, 5, 36, 113),
                  "service": True, "what": i18n.t("cli.uninstall.service_off", id=unit["id"])}]
    else:
        steps = [{"do": "run", "argv": ["systemctl", "--user", "disable", "--now", unit["id"]], "ok": (1, 5),
                  "service": True, "what": i18n.t("cli.uninstall.service_off", id=unit["id"])}]
    steps.append({"do": "remove", "path": unit["path"], "what": i18n.t("cli.uninstall.service_file", path=unit["path"])})
    if os.path.isdir(unit["path"] + ".d"):   # keep_on_oom's drop-in
        steps.append({"do": "tree", "path": unit["path"] + ".d",
                      "what": i18n.t("cli.uninstall.service_file", path=unit["path"] + ".d")})
    if unit["kind"] == "systemd":
        # no systemd --user to reach (a container: the installer wrote the unit, nothing runs it; systemctl looks for
        # the manager's sockets in XDG_RUNTIME_DIR or the session bus) — nothing to reload, its exit 1 is no failure;
        # with one, a failure is said
        run_dir = os.environ.get("XDG_RUNTIME_DIR") or ""
        alone = not os.environ.get("DBUS_SESSION_BUS_ADDRESS") and not (
            run_dir and any(os.path.exists(os.path.join(run_dir, *p)) for p in (("bus",), ("systemd", "private"))))
        steps.append({"do": "run", "argv": ["systemctl", "--user", "daemon-reload"], "ok": (1,) if alone else (),
                      "what": "systemctl --user daemon-reload"})
    return steps


def uninstall_path(state):
    """The hub's folder in the user's PATH: the install scripts here do not put it there."""
    return []


def drop_user_path(folder):
    """Nothing to drop here (uninstall_path gives no such step)."""


def oneline_data(home=None, environ=None, platform=sys.platform):
    """The one-line install's own folder (deploy/oneline/README.md: <data>): the environments in shells/, the downloads,
    the install log. Linux — under XDG_DATA_HOME, as the script puts it."""
    home = home or os.path.expanduser("~")
    env = os.environ if environ is None else environ
    if platform != "darwin" and env.get("XDG_DATA_HOME"):
        return os.path.join(env["XDG_DATA_HOME"], "agents-hub")
    return os.path.join(home, ".local", "share", "agents-hub")


def shortcut_dir(home=None, environ=None, platform=sys.platform):
    """The folder of the applications menu entries of this user (core/shortcut.py): Linux — XDG_DATA_HOME (an absolute
    one, as the spec has it) or ~/.local/share, then applications/. A Mac has none (no shortcut there) -> None."""
    if platform == "darwin":
        return None
    home = home or os.path.expanduser("~")
    env = os.environ if environ is None else environ
    base = env.get("XDG_DATA_HOME")
    return os.path.join(base if base and os.path.isabs(base) else os.path.join(home, ".local", "share"), "applications")


def processes_in(folder, platform=sys.platform):
    """The pids of this user's processes whose program lies in folder (an environment a hub still runs from), this
    process aside. Linux: /proc/<pid>/exe; macOS: libproc's proc_pidpath (the program's real path, not the one it was
    started by: a link that now leads elsewhere does not hide it). A process whose program cannot be read is not counted."""
    inside = os.path.join(os.path.realpath(folder), "")
    me, out = os.getpid(), []
    if platform == "darwin":
        import ctypes
        try:
            lib = ctypes.CDLL("/usr/lib/libproc.dylib")
            n = lib.proc_listallpids(None, 0)
            pids = (ctypes.c_int * (max(n, 0) + 64))()
            n = lib.proc_listallpids(pids, ctypes.sizeof(pids))
        except (OSError, AttributeError):
            return []
        buf = ctypes.create_string_buffer(4096)
        for pid in pids[:max(n, 0)]:
            if pid <= 0 or pid == me or lib.proc_pidpath(pid, buf, ctypes.sizeof(buf)) <= 0:
                continue
            if os.path.realpath(buf.value.decode("utf-8", "replace")).startswith(inside):
                out.append(pid)
        return out
    try:
        names = os.listdir("/proc")
    except OSError:
        return []
    for name in names:
        if not name.isdigit() or int(name) == me:
            continue
        try:
            exe = os.readlink(f"/proc/{name}/exe")
        except OSError:
            continue
        if exe.endswith(" (deleted)"):
            exe = exe[:-len(" (deleted)")]
        if os.path.join(exe, "").startswith(inside) or exe.startswith(inside):
            out.append(int(name))
    return out


def remove_later(paths, pids):
    """Here a folder a running program lies in can be deleted at once: nothing is left for later."""
    return None


def stop_processes(pids, grace=5.0):
    """Stop these processes of the user (hub uninstall: what of the hub outlived hub-up — a setup step with its child):
    SIGTERM to the process's own group when it leads one (its children with it), else to it; SIGKILL after grace to
    what is left. -> the pids still alive."""
    def send(sig):
        for pid in pids:
            try:
                if os.getpgid(pid) == pid and pid != os.getpgrp():
                    os.killpg(pid, sig)
                else:
                    os.kill(pid, sig)
            except OSError:
                pass
    pids = [p for p in pids if real_pid(p)]
    send(signal.SIGTERM)
    end = time.time() + grace
    while time.time() < end and [p for p in pids if _running(p)]:
        time.sleep(0.2)
    pids = [p for p in pids if _running(p)]
    send(signal.SIGKILL)
    time.sleep(0.2)
    return [p for p in pids if _running(p)]


# ---- `hub run` (core/runjobs.py): a run apart from the hub, its wrapper and its command's group ---------------------

def run_apart_how(env=None):
    """How a `hub run` starts apart from the hub here: "unit" — a transient unit of the user's systemd when hub-up is a
    unit's process (the unit's stop takes its whole cgroup, a session of its own with it), else "session" — a session
    of its own (launchd stops a job's process group, not other sessions: a probe on macOS 15)."""
    return "unit" if _outside_unit(["true"], env) else "session"


def boot_id():
    """This boot of the machine (Linux), else None: a process number and start time written before a reboot name
    nothing now."""
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip() or None
    except OSError:
        return None


def group_alive(pgid):
    """Whether a process group of this number exists (a group's number is not reused while it does)."""
    if not real_pid(pgid):
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def stop_group(pgid, hard=False):
    """SIGTERM (hard: SIGKILL) to a process group the caller has checked is its own."""
    if real_pid(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL if hard else signal.SIGTERM)
        except OSError:
            pass


def ask_stop(pid):
    """Ask a process the caller knows to be its own to stop (SIGTERM): a `hub run` wrapper then stops its command."""
    if real_pid(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def kill_now(pid):
    """Stop a process the caller knows to be its own at once (SIGKILL)."""
    if real_pid(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def parent_of(pid):
    """The parent of a live process, or None."""
    if not real_pid(pid):
        return None
    try:
        with open(f"/proc/{pid}/stat") as f:
            return int(f.read().rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        pass
    try:
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, timeout=10,
                             env={**os.environ, "LC_ALL": "C"}).stdout.strip()
        return int(out) if out else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


# --- the key of the Secrets vault (modules/secrets/vault.py) --------------------------------------------------------

SECRET_SERVICE = "hub-core-secrets"   # the macOS Keychain item's service; its account is the hub's name
SECRET_KEY_FILE = "secrets.key"       # Linux: the key in the settings folder, never in the state folder


def secret_key_file(folder, platform=sys.platform):
    """The file the vault key lives in, or None where the OS keeps it (macOS: the login Keychain)."""
    return None if platform == "darwin" else os.path.join(folder, SECRET_KEY_FILE)


def secret_key(account, folder, create=False, platform=sys.platform, run=None):
    """The vault key: 32 bytes, or None when there is none (and `create` is false). macOS: a generic password of the login
    Keychain through /usr/bin/security — the key goes on its stdin (`security -i`), not in its arguments, which `ps`
    shows. Linux: a file 0600 in the settings folder 0700. Another value than a key there is OSError: the vault says the
    key is lost rather than making a new one over it."""
    run = run or subprocess.run
    if platform == "darwin":
        got = run(["/usr/bin/security", "find-generic-password", "-s", SECRET_SERVICE, "-a", account, "-w"],
                  capture_output=True, text=True, timeout=30)
        if got.returncode == 0:
            return _key_from_hex(got.stdout.strip())
        if got.returncode != 44:   # 44: no such item
            raise OSError(f"security find-generic-password: exit {got.returncode}")
        if not create:
            return None
        key = os.urandom(32)
        made = run(["/usr/bin/security", "-i"], input=f"add-generic-password -U -s {SECRET_SERVICE} -a {account} "
                   f"-w {key.hex()}\n", capture_output=True, text=True, timeout=30)
        # `security -i` may exit 0 for a command that failed inside it: the key counts only once it reads back
        if made.returncode != 0 or secret_key(account, folder, False, platform, run) != key:
            raise OSError(f"security add-generic-password: exit {made.returncode}, not stored")
        return key
    path = secret_key_file(folder, platform)
    try:
        with open(path, encoding="ascii") as f:
            return _key_from_hex(f.read().strip())
    except FileNotFoundError:
        if not create:
            return None
    key = os.urandom(32)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=SECRET_KEY_FILE + ".", suffix=".tmp")   # 0600 at once
    try:
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(key.hex() + "\n")
            f.flush()
            sync(f.fileno())
        try:   # a new name, never over one: another process that made its key meanwhile keeps it (its vault may be under it)
            os.link(tmp, path)
        except FileExistsError:
            return secret_key(account, folder, False, platform, run)
        finally:
            os.unlink(tmp)
        sync_dir(folder)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return key


def _key_from_hex(text):
    if not re.fullmatch(r"[0-9a-f]{64}", text or ""):
        raise OSError("the vault key is not a key")
    return bytes.fromhex(text)
