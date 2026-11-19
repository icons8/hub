"""hub-browser: the browsers' supervisor (one process per machine) and the launcher of the MCP server for sessions.

  hub-browser serve            keep the local browsers running (start: demand — only watch, login — bring up once per
                               machine boot); every change of "answers / does not answer" goes to the core as the
                               `browser.status` fact (the page and `hub status` read it from the core)
  hub-browser mcp <name>       (a session starts it as an MCP server) wait for the browser and become chrome-devtools-mcp;
                               start: demand/login — a go-between: the browser comes up on a tool call, not at start
  hub-browser status [--json]  the browsers from the settings and whether they answer right now
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import platforms  # noqa: E402
from core import catalog, i18n, lock, spool  # noqa: E402
from core.daemon import default_state_dir  # noqa: E402
from modules.browser import browsers  # noqa: E402

EVERY = 5
MAX_BACKOFF = 60
CRASH_FREE = 3        # starts in a row after which Chrome died before it ever answered: from here the pause grows past MAX_BACKOFF
MAX_PARK = 3600       # the longest pause between starts of a Chrome that keeps dying at start
MCP_WAIT = 30
REAP_EVERY = 60  # how often to check the local browser's tabs
OVERLOAD = 4     # CPU load per core above which a slow tab is not a hung one
# how long to wait for a launched Chrome to answer over CDP: with the owner's profile after a Mac reboot it comes up
# in over a minute (02.10: waiting 10-16 s and launching again produced "a pile of chromes")
LAUNCH_GRACE = 120
DISK_EVERY = 300     # how often to look at the Chrome version on disk
OWNED_EVERY = 60     # how often to look whose Chrome answers on a local browser's port (Supervisor.foreign)
CLOSE_WAIT = 5       # how long Chrome that was asked to close itself (Browser.close) gets before the signal
UPGRADE_WAIT = 600   # how long to wait for sessions with the browser to free up: the old Chrome is already broken after an update


def kill_tree(proc):
    """Kill a launched Chrome as a whole: it starts separately (platforms: popen_detached)."""
    try:
        platforms.current.kill_detached(proc)
    except (OSError, ProcessLookupError):
        pass


def kill_pid(pid):
    """Kill a Chrome not launched by this supervisor (by its previous life or by the MCP launcher): it starts as
    a separate process group, so kill that group, otherwise the process itself."""
    if not platforms.real_pid(pid):
        return
    try:
        if hasattr(os, "killpg") and os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def listed():
    """The machine's processes as (pid, command line), every user's; None when they cannot be listed (no ps)."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    got = []
    for line in out.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if pid.isdigit():
            got.append((int(pid), cmd))
    return got


def _flag(name, value):
    # the whole value: profile .../vm must not find the Chrome of profile .../vm2 (it would be killed as ours)
    return re.compile(re.escape(f"--{name}={value}") + r"(?=\s|$)")


def _main_chrome(cmd):
    return "--type=" not in cmd and "chrome-devtools-mcp" not in cmd


def running(profile, procs=None):
    """pid of the main Chrome process with this profile, if it is already running (from the hub's previous life or earlier)."""
    ours = _flag("user-data-dir", profile)
    for pid, cmd in (listed() if procs is None else procs) or []:
        if ours.search(cmd) and _main_chrome(cmd):
            return pid
    return None


def alive(profile):
    """A Chrome of this profile is running: by the process listing, or by the profile's own lock when the listing cannot
    tell (no `ps` on Windows, a `ps` timeout on a loaded machine). pid or True; only for a yes/no, never to kill."""
    return running(profile) or browsers.profile_held(profile)


def foreign(port, profile, procs):
    """pid of a Chrome that listens for CDP on this port with another profile (another hub's, a person's own) — not
    this browser's; None when there is none or nothing tells (the processes cannot be listed, the port's program is no
    Chrome started with the flag). A second hub on the same port drove the first one's Chrome with its sign-ins and
    closed its tabs (the lab, decisions 2928–2952)."""
    on_port, ours = _flag("remote-debugging-port", port), _flag("user-data-dir", profile)
    held = [(pid, cmd) for pid, cmd in procs or [] if on_port.search(cmd) and _main_chrome(cmd)]
    if not held or any(ours.search(cmd) for _, cmd in held):
        return None
    return held[0][0]


def start_local(cfg, state_dir, name, b):
    """Launch a local browser separately from the caller (it outlives it), output goes to browsers/<name>.log."""
    d = os.path.join(state_dir, "browsers")
    profile = browsers.profile_dir(state_dir, name, b)
    if not any(str(a).startswith("--profile-directory") for a in b.get("args") or []):   # Chrome reads another folder then
        browsers.seed_profile(profile, window=not b.get("headless", True))   # a colour is for a window: headless has no icon
    os.makedirs(profile, exist_ok=True)
    with open(os.path.join(d, f"{name}.log"), "ab") as log:
        return platforms.current.popen_detached(browsers.chrome_command(cfg, state_dir, name, b), stdout=log,
                                                stderr=log, stdin=subprocess.DEVNULL, env=browsers.env(cfg))


def load_per_core():
    """CPU load per core over a minute; 0 where the OS does not report it (Windows has no getloadavg)."""
    try:
        return os.getloadavg()[0] / (os.cpu_count() or 1)
    except (AttributeError, OSError):
        return 0.0


def boot_time():
    """When the machine booted (s): "at Mac login" is once per boot. None if unknown (then once per life of
    the supervisor)."""
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.boottime"], capture_output=True, text=True,
                                 timeout=5).stdout
            return re.search(r"sec = (\d+)", out).group(1)
        with open("/proc/stat") as f:
            return next(line.split()[1] for line in f if line.startswith("btime"))
    except (OSError, AttributeError, StopIteration, subprocess.SubprocessError):
        return None


CHROME_LOG = re.compile(r"\[\d+:\d+:[\d/.]+:(\w+):[^\]]*\]\s*(.*)")   # [pid:tid:time:LEVEL:file.cc(line)] message


class Supervisor:
    def __init__(self, state_dir, clock=time.time, probe=browsers.probe):
        self.state_dir = state_dir
        self.clock = clock
        self.probe = probe
        self.spool = os.path.join(state_dir, "spool")
        self.procs = {}      # name -> Popen of the local browser
        self.backoff = {}    # name -> (when allowed again, pause)
        self.said = set()    # browsers whose "no screen: headless" line is already in the log
        self.log_from = {}   # name -> size of its Chrome log when the last start began (what is after it is this start's)
        self.crashes = {}    # name -> the starts in a row that died before the browser answered: n, what was launched, the status line
        self.reported = {}   # name -> last reported status
        self.hung = {}       # browser address -> {ids of tabs that did not answer last time}
        self.reaped = {}     # browser address -> when the tabs were checked
        self.tabs, self.responds, self.close_tab = browsers.tabs, browsers.tab_responds, browsers.close_tab
        self.load = load_per_core
        self.launch, self.kill_tree, self.running, self.kill_pid = start_local, kill_tree, running, kill_pid
        self.close_browser, self.sleep = browsers.close_browser, time.sleep
        self.listed, self._listing = listed, (float("-inf"), None)
        self.owned = {}      # name -> (when the Chrome on its port was looked at, pid of another's there or None)
        self._disk = {}   # Chrome path → (when we looked, its version on disk)
        self.upgrade = {}    # name -> when we noticed that the Chrome on disk is newer than the running one
        self.renewed = {}    # name -> when its Chrome was killed in favour of a new one: do not kill again while it comes up
        self.started = {}    # name -> when launched (or a foreign launch was noticed): wait up to LAUNCH_GRACE
        self.boot = boot_time
        self.active = lambda name: browsers.last_active(state_dir, name)   # when a session last used the browser
        self.seen = {}       # name -> when its Chrome was first seen up: the idle count starts there if no session used it
        self.logged = {}     # name -> the machine boot on which a start: login browser was already started or answered

    def login_mark(self, name):
        return os.path.join(self.state_dir, "browsers", f"{name}.login")

    def login_due(self, name):
        """start: login: whether to start it now: it was not started on this machine boot yet and did not answer (a mark in a
        file: a hub restart does not bring back a browser the owner closed)."""
        boot = self.boot()
        if name not in self.logged:
            try:
                with open(self.login_mark(name)) as f:
                    self.logged[name] = f.read().strip()
            except OSError:
                self.logged[name] = None
        return boot is None and self.logged[name] is None or boot is not None and self.logged[name] != boot

    def logged_in(self, name):
        boot = self.boot() or "?"
        if self.logged.get(name) == boot:
            return
        self.logged[name] = boot
        try:
            os.makedirs(os.path.dirname(self.login_mark(name)), exist_ok=True)
            with open(self.login_mark(name), "w") as f:
                f.write(boot)
        except OSError:
            pass

    def reap(self, url, now):
        """A tab that does not answer over CDP twice in a row is closed: chrome-devtools-mcp hangs on it
        ("Network.enable timed out" for all sessions, 01.10). Not more often than once per REAP_EVERY."""
        if now - self.reaped.get(url, -REAP_EVERY) < REAP_EVERY:
            return
        self.reaped[url] = now
        if self.load() > OVERLOAD:
            # the machine is overloaded: the tab answers slowly rather than hanging, do not close it or reset sessions
            # (06.10, load ~195: closing -> browser.reset -> bringing sessions up with MCP -> more load, in a circle)
            self.hung.pop(url, None)
            return False
        pages = [t for t in self.tabs(url) if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        bad = {t["id"] for t in pages if not self.responds(t["webSocketDebuggerUrl"])}
        gone = bad & self.hung.get(url, set())
        for tid in gone:
            print(f"hub-browser: tab {tid} did not answer over CDP twice in a row — closing it", flush=True)
            self.close_tab(url, tid)
        self.hung[url] = bad - gone
        return bool(gone)

    def step(self):
        cfg = catalog.load_config(self.state_dir)
        conf = browsers.configured(cfg)
        missing = browsers.mcp_missing(cfg) if conf else None   # one program for every browser
        for name, b in conf.items():
            status = self.check(cfg, name, b)
            if missing and not status.get("problem"):   # Chrome answers or waits for a call, but no session can use it
                status = {**status, "problem": i18n.tr(i18n.owner(), "cli.browser.mcp_missing", name=name, program=missing)}
            self.report(name, status)
        for name in [n for n in self.reported if n not in conf]:
            self.report(name, {"removed": True})
            self.reported.pop(name, None)
            for per_browser in (self.crashes, self.backoff, self.log_from):
                per_browser.pop(name, None)
            self.said.discard(name)

    def check(self, cfg, name, b):
        status = {"kind": b.get("kind"), "url": b.get("url"), "risk": b.get("risk", []), "up": False, "version": None}
        if b.get("problem"):
            return {**status, "problem": b["problem"]}
        version = self.probe(b["url"])
        if version:
            self.backoff.pop(name, None)
            self.crashes.pop(name, None)
            self.started.pop(name, None)
            if b.get("start") == "login":
                self.logged_in(name)
            if b["kind"] == "local":
                other = self.foreign(name, b)
                if other:
                    return {**status, "problem": other}
                why = self.renew(cfg, name, b, version)
                if why:
                    return {**status, "problem": why}
                if self.idle(name, b):
                    return {**status, "idle": True}
                try:
                    if self.reap(b["url"], self.clock()):  # sessions with this browser reconnect MCP
                        spool.put(self.spool, "browser.reset", name,
                                  {"why": i18n.tr(i18n.owner(), "cli.browser.reset_hung_tab")}, src="browser")
                except Exception as e:  # noqa: BLE001 — tab cleanup must not bring the supervisor down
                    print(f"hub-browser: checking tabs of {name}: {e!r}", flush=True)
            return {**status, "up": True, "version": version}
        self.seen.pop(name, None)
        if b["kind"] != "local":
            return {**status, "problem": i18n.tr(i18n.owner(), "cli.browser.no_cdp")}
        now = self.clock()
        proc = self.procs.get(name)
        if proc is not None and proc.poll() is not None and name in self.started and not self.running(
                browsers.profile_dir(self.state_dir, name, b)):
            self.start_failed(cfg, name, b, proc)   # died before it ever answered
            proc = None
        if proc is not None and proc.poll() is None:
            if now - self.started.get(name, now) < LAUNCH_GRACE:
                return {**status, "problem": i18n.tr(i18n.owner(), "cli.browser.starting")}
            if b.get("start") in ("demand", "login") and not b.get("headless", True):
                # the owner's window: do not kill and do not start again
                return {**status, "problem": i18n.tr(i18n.owner(), "cli.browser.running_no_cdp", pid=proc.pid)}
            self.kill_tree(proc)  # alive but never answered: the whole Chrome, not only the main process
            self.procs.pop(name, None)
            self.started.pop(name, None)   # headless on demand: no window to lose; the next tool call starts a new one
        elif proc is None or b.get("start") in ("demand", "login"):
            other = self.running(browsers.profile_dir(self.state_dir, name, b))
            if other:  # a Chrome with this profile is already running: never start a second one on the same profile
                if now - self.started.setdefault(name, now) < LAUNCH_GRACE:
                    return {**status, "problem": i18n.tr(i18n.owner(), "cli.browser.starting")}
                if b.get("start") not in ("demand", "login") or not self.headless(other, b["port"]):
                    # a window on the screen may be the owner's; and only a demand or login browser is not restarted otherwise
                    return {**status, "problem": i18n.tr(i18n.owner(), "cli.browser.profile_running_no_cdp", pid=other)}
                print(f"hub-browser: {name}: headless Chrome {other} does not answer over CDP — stopping it", flush=True)
                self.kill_pid(other)
                self.started.pop(name, None)
                return {**status, "problem": i18n.tr(i18n.owner(), "cli.browser.starting")}
        if b.get("start") in ("demand", "login") and not (b["start"] == "login" and self.login_due(name)):
            # a session's tool call (ensure_browser) will start it, not the supervisor
            self.procs.pop(name, None)
            self.started.pop(name, None)
            return {**status, "idle": True}
        crash = self.crashes.get(name)
        if crash and crash["sig"] != self.signature(cfg, name, b):   # other settings: the cause may be gone, try at once
            self.crashes.pop(name)
            self.backoff.pop(name, None)
            crash = None
        when, pause = self.backoff.get(name, (0, 1))
        if now < when:
            return {**status, "problem": crash["problem"] if crash and crash["n"] > CRASH_FREE
                    else i18n.tr(i18n.owner(), "cli.browser.crashed")}
        if b.get("no_screen") and name not in self.said:
            self.said.add(name)
            print(f"hub-browser: {name}: headless: false, but this machine has no screen (no DISPLAY or WAYLAND_DISPLAY "
                  "for Chrome, browser.env included) — starting it headless", flush=True)
        self.log_from[name] = self.log_size(name)
        try:
            self.procs[name] = self.launch(cfg, self.state_dir, name, b)
        except (OSError, browsers.Problem) as e:
            self.backoff[name] = (now + MAX_BACKOFF, MAX_BACKOFF)
            return {**status, "problem": str(e)[:200]}
        self.started[name] = now
        if b.get("start") == "login":
            self.logged_in(name)
        pause = min(MAX_BACKOFF, pause * 2)
        self.backoff[name] = (now + max(pause, 10), pause)
        return {**status, "problem": i18n.tr(i18n.owner(), "cli.browser.starting")}

    def signature(self, cfg, name, b):
        """What a launch of this browser is made of (the command, the environment): the same, and it fails the same."""
        try:
            return (tuple(browsers.chrome_command(cfg, self.state_dir, name, b)),
                    json.dumps((cfg.get("browser") or {}).get("env"), sort_keys=True, default=str))
        except browsers.Problem as e:
            return str(e)

    def start_failed(self, cfg, name, b, proc):
        """A Chrome we started died before it answered. Remove what it left in the profile; after CRASH_FREE such starts
        in a row wait longer each time (up to MAX_PARK) and say why in the status, so a machine that cannot start it
        (no screen, a broken profile) is not tried every minute for ever."""
        self.procs.pop(name, None)
        self.started.pop(name, None)
        browsers.clean_metrics(browsers.profile_dir(self.state_dir, name, b))
        n = self.crashes.get(name, {"n": 0})["n"] + 1
        why = self.why_died(name, proc)
        print(f"hub-browser: {name}: Chrome died at start ({n} in a row): {why}", flush=True)
        problem = i18n.tr(i18n.owner(), "cli.browser.crashed")
        if n > CRASH_FREE:
            delay = min(MAX_PARK, MAX_BACKOFF * 2 ** (n - CRASH_FREE))
            when, pause = self.backoff.get(name, (0, 1))
            self.backoff[name] = (max(when, self.clock() + delay), pause)
            problem = i18n.tr(i18n.owner(), "cli.browser.crash_loop", count=n, why=why, minutes=max(1, round(delay / 60)))
        self.crashes[name] = {"n": n, "sig": self.signature(cfg, name, b), "problem": problem}

    def log_size(self, name):
        try:
            return os.path.getsize(os.path.join(self.state_dir, "browsers", f"{name}.log"))
        except OSError:
            return 0

    def why_died(self, name, proc):
        """The last two errors Chrome printed to the log since this start (the log keeps every run; the line that killed it
        comes after the noise, like a missing bus), else its exit code."""
        try:
            with open(os.path.join(self.state_dir, "browsers", f"{name}.log"), "rb") as f:
                f.seek(min(self.log_from.get(name, 0), os.path.getsize(f.name)))
                text = f.read(65536).decode("utf-8", "replace")
        except OSError:
            text = ""
        errors = [m.group(2) for m in map(CHROME_LOG.match, text.splitlines()) if m and m.group(1) in ("ERROR", "FATAL")]
        return ("; ".join(errors[-2:]) if errors else f"exit code {proc.poll()}")[:160]

    def headless(self, pid, port):
        """Is this Chrome (of our profile) a headless one that was started for this browser's port: it has no window,
        so no owner's work in it to lose, and it is the hung Chrome of a past life of this supervisor. One without our
        port may be another program's (a pipe, another port): not ours to stop."""
        on_port = _flag("remote-debugging-port", port)
        return any(p == pid and "--headless" in cmd and on_port.search(cmd) for p, cmd in self.listed() or [])

    def foreign(self, name, b):
        """Another program's Chrome answers on this browser's port (foreign()): the line why it is not taken, or None.
        Checked when this supervisor did not start the one that answers, again after OWNED_EVERY."""
        proc = self.procs.get(name)
        if proc is not None and proc.poll() is None:
            return None
        now = self.clock()
        when, pid = self.owned.get(name, (-OWNED_EVERY, None))
        if now - when >= OWNED_EVERY:
            at, procs = self._listing
            if now - at >= 1:   # one listing of the machine's processes for all the browsers of a step
                procs = self.listed()
                self._listing = (now, procs)
            pid = foreign(int(b["port"]), browsers.profile_dir(self.state_dir, name, b), procs) if procs else None
            self.owned[name] = (now, pid)
        return i18n.tr(i18n.owner(), "cli.browser.port_foreign", port=b["port"], pid=pid) if pid else None

    def disk_version(self, cfg, b=None):
        now = self.clock()
        try:
            exe = browsers.find_chrome(cfg, b)
        except browsers.Problem:
            return None
        if now - self._disk.get(exe, (-DISK_EVERY,))[0] >= DISK_EVERY:
            self._disk[exe] = (now, browsers.disk_version(exe))
        return self._disk[exe][1]

    def busy(self, name):
        """Is there a session with this browser in the middle of a turn (by the core snapshot, state.json)."""
        from core import journal
        try:
            sessions = (journal.read_state_file(self.state_dir).get("state") or {}).get("sessions") or {}
        except (OSError, ValueError):
            return False
        return any(f"browser:{name}" in (s.get("tools") or []) and s.get("state") in ("busy", "dialog")
                   for s in sessions.values())

    def stop_chrome(self, name, b):
        """Stop the Chrome of its own profile: its own process as a whole, a foreign launch (a past life) by pid. First it
        is asked to close itself (CLOSE_WAIT s): a Chrome that is killed may lose its tabs and session cookies; the signal
        goes to what did not leave."""
        proc = self.procs.pop(name, None)
        if proc is not None and proc.poll() is None:
            if not self.closed(name, b, lambda: proc.poll() is not None):
                self.kill_tree(proc)
            return True
        profile = browsers.profile_dir(self.state_dir, name, b)
        pid = self.running(profile)
        if pid:
            if not self.closed(name, b, lambda: not self.running(profile)):
                self.kill_pid(pid)
            return True
        return False

    def closed(self, name, b, gone):
        """Ask the Chrome of the browser to close itself and wait for gone(); False if it could not be asked or did not go.
        Not asked when another program's Chrome holds the port: the request goes to the port, not to our profile's Chrome."""
        procs = self.listed()
        if procs and foreign(int(b["port"]), browsers.profile_dir(self.state_dir, name, b), procs) is not None:
            return False
        if not self.close_browser(b["url"]):
            return False
        end = self.clock() + CLOSE_WAIT
        while not gone():
            if self.clock() >= end:
                return False
            self.sleep(0.2)
        return True

    def idle(self, name, b):
        """start: demand or login with idle_minutes: no session has called a tool of the browser for that long — close its
        Chrome; the next call starts it again (ensure_browser). Never a window on the screen (headless: false): the owner
        may be signing in there, and that makes no tool calls. True if closed."""
        limit = (b.get("idle_minutes") or 0) * 60
        if not limit or b.get("start") not in ("demand", "login") or not b.get("headless", True):
            self.seen.pop(name, None)
            return False
        now = self.clock()
        seen = self.seen.setdefault(name, now)
        if now - max(seen, self.active(name) or 0) < limit:
            return False
        print(f"hub-browser: {name}: no tool call for {b['idle_minutes']:g} min — closing Chrome", flush=True)
        self.seen.pop(name, None)
        return self.stop_chrome(name, b)

    def renew(self, cfg, name, b, version):
        """Chrome was updated on disk, but the old process is running: new tabs die in it ("No rendezvous
        client"), and sessions get ERR_ABORTED on navigate. Restart its own Chrome when sessions with it are free (but not
        longer than UPGRADE_WAIT), and browser.reset: sessions will bring MCP up again. A state line or None."""
        now = self.clock()
        if now - self.renewed.get(name, -LAUNCH_GRACE) < LAUNCH_GRACE:
            return None
        disk = self.disk_version(cfg, b)
        if not browsers.newer(disk, version):
            self.upgrade.pop(name, None)
            return None
        since = self.upgrade.setdefault(name, now)
        if now - since < UPGRADE_WAIT and self.busy(name):
            return None
        old = browsers._version(version)
        print(f"hub-browser: {name}: Chrome was updated on disk ({old} → {disk}) — restarting", flush=True)
        self.stop_chrome(name, b)
        self.upgrade.pop(name, None)
        self.renewed[name] = now
        spool.put(self.spool, "browser.reset", name,
                  {"why": i18n.tr(i18n.owner(), "cli.browser.reset_updated", old=old, new=disk)}, src="browser")
        return i18n.tr(i18n.owner(), "cli.browser.restart_updated")

    def fresh(self, cfg):
        """`hub restart browser`: also kill an already running Chrome of its own profiles, otherwise the new supervisor
        would pick up the same old process."""
        now = self.clock()
        for name, b in browsers.configured(cfg).items():
            if b.get("kind") == "local" and not b.get("problem") and self.stop_chrome(name, b):
                print(f"hub-browser: {name}: hub restart browser — stopping the running Chrome", flush=True)
                self.renewed[name] = now
                spool.put(self.spool, "browser.reset", name, {"why": "hub restart browser"}, src="browser")

    def report(self, name, status):
        if self.reported.get(name) == status:
            return
        self.reported[name] = status
        spool.put(self.spool, "browser.status", name, status, src="browser")


def supervised(state_dir):
    """Is the browser supervisor running (holding browsers.lock): then it starts Chrome, not the sessions."""
    f = lock.acquire(os.path.join(state_dir, "browsers.lock"))
    if f is None:
        return True
    f.close()
    return False


_launch_lock = None   # held until the process ends (it becomes MCP): neighbouring launchers do not start a second Chrome


def wait_for_browser(cfg, state_dir, name, b, probe=browsers.probe, launch=start_local, running=alive,
                     clock=time.time, sleep=time.sleep):
    """Wait for the browser. Start Chrome itself only without a supervisor, by one launcher (a lock) and if a
    Chrome with this profile is not running yet (if the agent chrome is not running, every session tries to
    start a new one: eight sessions after a restart each launched a Chrome, as children of themselves). True if it answers."""
    global _launch_lock
    start, tried = clock(), False
    while not probe(b["url"]):
        if b["kind"] == "local" and not tried and clock() - start >= 5:
            tried = True
            if not supervised(state_dir) and not running(browsers.profile_dir(state_dir, name, b)):
                os.makedirs(os.path.join(state_dir, "browsers"), exist_ok=True)
                mine = lock.acquire(os.path.join(state_dir, "browsers", f"{name}.launch.lock"))
                if mine is not None:
                    _launch_lock = mine
                    launch(cfg, state_dir, name, b)
        if clock() - start > MCP_WAIT:
            return False
        sleep(0.5)
    return True


launch_tabs = set()   # ids of the about:blank tabs of a Chrome this process launched (chrome_command's positional url)
tidying = threading.Lock()   # tidy runs in its own thread on every new page: one at a time


def note_launch_tabs(url, tabs=browsers.tabs, clock=time.time, sleep=time.sleep, wait=3):
    """A Chrome that we just launched opens its about:blank tab by itself, and nobody owns it: remember it, close_launch_tabs
    closes it once another tab exists. The tab appears a little after CDP starts answering: wait for it."""
    end = clock() + wait
    while True:
        try:
            found = {t["id"] for t in tabs(url) if t.get("type") == "page" and t.get("url") == "about:blank"}
        except (OSError, ValueError, KeyError):
            found = set()
        if found or clock() >= end:
            if len(found) == 1:   # Chrome opens one; two or more: another session's own tab is among them — leave them all
                launch_tabs.update(found)
            return
        sleep(0.2)


def close_launch_tabs(url, tabs=browsers.tabs, close=browsers.close_tab):
    """Another tab exists: close the launch tab if it is still empty. Never the last tab: that would close the window."""
    with tidying:
        if launch_tabs:
            _close_launch_tabs(url, tabs, close)


def _close_launch_tabs(url, tabs, close):
    try:
        pages = [t for t in tabs(url) if t.get("type") == "page"]
        blank = [t["id"] for t in pages if t["id"] in launch_tabs and t.get("url") == "about:blank"]
        launch_tabs.intersection_update(blank)   # one that holds a page now is somebody's: let it be
        if blank and len(pages) > len(blank):
            for tid in blank:
                close(url, tid)
            launch_tabs.clear()
    except (OSError, ValueError, KeyError):
        pass


def ensure_browser(cfg, state_dir, name, b, probe=browsers.probe, launch=start_local, running=alive,
                   clock=time.time, sleep=time.sleep, wait=LAUNCH_GRACE):
    """A browser on demand (start: demand, login): start Chrome if it does not answer, and wait. One
    launcher starts it (a lock) and only if a Chrome with this profile is not running yet. True if it answers."""
    start, tried, launched = clock(), False, False
    profile = browsers.profile_dir(state_dir, name, b)
    blank = (b.get("open") or "about:blank") == "about:blank" and not browsers.restoring(profile)   # before the launch
    while not probe(b["url"]):
        if not tried:
            tried = True
            os.makedirs(os.path.join(state_dir, "browsers"), exist_ok=True)
            mine = lock.acquire(os.path.join(state_dir, "browsers", f"{name}.launch.lock"))
            if mine is not None:
                try:
                    if not running(profile):
                        launch(cfg, state_dir, name, b)
                        launched = True
                finally:
                    mine.close()
        if clock() - start > wait:
            if launched and not running(profile):   # it died at start: what it left is a disk filled call after call
                browsers.clean_metrics(profile)
            return False
        sleep(0.5)
    if launched and blank:   # another start page, or the tabs of the last session, are not an empty tab
        note_launch_tabs(b["url"], clock=clock, sleep=sleep)
    return True


def unreachable(name, b, pid, log):
    """Why the browser did not come up: the session gets this instead of "Could not connect to Chrome" from
    chrome-devtools-mcp."""
    if pid:
        pid = "" if pid is True else f" (pid {pid})"   # True: the profile's lock tells it, with no pid
        return (f"Chrome with the profile of browser {name} is running{pid} but does not answer at {b['url']}: it "
                "was probably opened without the debugging port. Ask the owner to close this Chrome entirely (all its "
                "windows); the next call will bring the browser up itself.")
    return f"Chrome did not answer within {LAUNCH_GRACE} s; what is wrong with it is in {log}."


OWN_TAB, OWN_TAB_CLOSE = "hub-own-tab", "hub-own-tab-close"   # ids of our calls: their replies are not shown to the session
STALE_CLOSE, INIT = "hub-own-tab-stale", "hub-init"   # closing a tab of a late attempt; the session's initialize, replayed
OWN_TAB_WAIT = 30   # s: how long to wait for the new_page reply before the session's first call
RESPAWNS = 2        # replacements of a hung chrome-devtools-mcp in a row, then the session is told to reload
RESPAWN_PAUSE = 60  # s after the last replacement of a series that ran out: then a call starts a new series
STOP_WAIT = 3       # s: how long a replaced chrome-devtools-mcp gets to exit before it is killed
# chrome-devtools-mcp 1.12 whose first connection to the browser hung: the hung connection holds its lock, so every
# later call in that process fails the same way, while a new process connects at once (05.11)
HUNG = "waiting for the browser connection"
# A page of chrome-devtools-mcp whose setup timed out (Network.enable under swap, 12.11): puppeteer keeps that failure for
# the tab, so every later call that lists the pages fails the same way at once until the process goes. Coming back faster
# than STUCK_FAST s it is that kept failure, not a slow browser: a real one waits for protocolTimeout (180 s)
STUCK = "timed out. Increase the 'protocolTimeout'"
STUCK_FAST = 10


def overload_note(load=load_per_core):
    """On a machine loaded several times over its cores Chrome answers in tens of seconds and no replacement connects."""
    per_core = load()
    if per_core <= OVERLOAD:
        return ""
    return f" The machine is overloaded (load {per_core:.0f} per core): Chrome answers slowly, free memory or CPU."


def retire(proc):
    """Stop a replaced chrome-devtools-mcp by its pid: npx forwards SIGINT to the node under it (as when Claude Code
    leaves), a kill after STOP_WAIT would leave that node behind, so it is the last resort."""
    try:
        proc.stdin.close()
    except (OSError, ValueError):
        pass
    try:
        proc.send_signal(signal.SIGINT if os.name == "posix" else signal.SIGTERM)
        proc.wait(STOP_WAIT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    except OSError:
        pass


def proxy(cmd, env, ensure, stdin=None, stdout=None, tidy=lambda: None, touch=lambda: None, changed=lambda: False):
    """The MCP intermediary: chrome-devtools-mcp connects to the browser itself on a tool call, so before every tools/call
    start the browser if it is not there (ensure: None if it answers, otherwise the reason, which the session gets). Session start does not start the browser. Before the session's first call, its own
    tab (new_page also makes it current): otherwise chrome-devtools-mcp takes the first one and the session steers a foreign one, for example
    the hub page in the owner's window. A session call goes out only after the reply to new_page:
    chrome-devtools-mcp answers calls out of order, and without waiting it could land in a foreign tab. MCP replies go
    to the session, except replies to the intermediary's calls. A tab that stays empty is of no use to anyone: a session that opens
    its page itself (new_page) needs no blank one; if it already has one, it is closed (28.10: blank tabs in the owner's window
    for as long as the session lived). tidy closes the empty tab Chrome opened at launch, once another one exists. touch says
    "the browser is in use" (every call and every line of reply: the supervisor closes an idle Chrome by it); changed says,
    once the browser answers, whether it is another Chrome than before: one that came back (closed for idleness, by the
    owner, or brought up by a neighbouring session) has none of the old tabs, so the session's own one is opened anew.
    A chrome-devtools-mcp whose connection to the browser hung (no reply to its own tab, the HUNG error, or the STUCK error
    at once) is replaced by a new one before the next call: up to RESPAWNS in a row, then the session gets an error that
    says to reload. The STUCK ones count apart, until a session call goes well or RESPAWN_PAUSE passes."""
    stdin, stdout = stdin or sys.stdin.buffer, stdout or sys.stdout.buffer
    kid = {"proc": None}   # the current chrome-devtools-mcp: a replaced one is no longer heard
    page, opened, closed = {}, threading.Event(), threading.Event()   # the number of its own tab; replies to new_page, close_page
    page_calls = set()   # ids of the session's new_page calls: their replies show whether its own tab is still empty
    adopting = threading.Event()   # cleared while the session's new_page that stands for its own tab is unanswered
    adopting.set()
    write, out = threading.Lock(), threading.Lock()   # the child's stdin and the session's stdout are written from two threads
    own = {"id": None, "n": 0}   # the id of the current attempt at its own tab: each attempt has its own, a late reply is stale
    hello = {}   # the session's initialize and notifications/initialized: a replacement gets them too
    pending = {}   # the session's requests the current child has not answered: id -> message
    asked = {}   # tools/call id -> when it went to the current child
    hung, inited = threading.Event(), threading.Event()
    respawned = [0]   # replacements in a row (back to 0 after a reply that went well)
    # replacements for the STUCK error in a row: back to 0 only after a session call went well, as its own tab opens in a new
    # process before the failure comes back, and a reset by it would replace the process every other call for ever
    stuck = {"n": 0}
    last_respawn = [0.0]   # when the last replacement was made, or the last time the series gave up

    def say(data):
        with out:
            stdout.write(data)
            stdout.flush()

    def failure(id, text):
        return (json.dumps({"jsonrpc": "2.0", "id": id, "result": {"isError": True, "content": [
            {"type": "text", "text": text}]}}, ensure_ascii=False) + "\n").encode()

    def error(id, text):
        say(failure(id, text))

    def send(data, wait=-1):
        """wait: how long to wait for the other writer; the signal handler runs in the session's thread and must not wait
        for itself (it may have interrupted a write) — busy is OSError, as a closed pipe."""
        if not write.acquire(timeout=wait):
            raise OSError("stdin busy")
        try:
            kid["proc"].stdin.write(data)
            kid["proc"].stdin.flush()
        except ValueError:   # stdin of a replaced child is closed
            raise OSError("stdin closed")
        finally:
            write.release()

    def call(id, name, arguments, wait=-1):
        asked[id] = time.monotonic()
        send((json.dumps({"jsonrpc": "2.0", "id": id, "method": "tools/call", "params": {
            "name": name, "arguments": arguments}}) + "\n").encode(), wait)

    aside = []   # writes started from the replies' thread: the end of the session waits for them

    def call_aside(*a):
        """From the replies' thread: a write that waits for the child's stdin must not stop the reading of its stdout."""
        def go():
            try:
                call(*a)
            except (OSError, ValueError):   # ValueError: stdin is already closed
                pass
        aside.append(threading.Thread(target=go, daemon=True))
        aside[-1].start()

    def text_of(msg):
        return " ".join(c.get("text") or "" for c in (msg.get("result") or {}).get("content") or [])

    def own_tab_unused(msg):
        """The session's new_page is answered, and its own tab (ours, not the session's) is still about:blank: close it."""
        if msg.get("id") not in page_calls or page.get("id") is None:
            return
        if re.search(rf"^{page['id']}: about:blank$", text_of(msg), re.M):
            number, page["id"], page["adopted"] = page["id"], None, True   # and it is not made again
            call_aside(OWN_TAB_CLOSE, "close_page", {"pageId": number})

    def own_reply(msg):
        res = msg.get("result") or {}
        text = text_of(msg)
        m = re.search(r"^(\d+): .*\[selected\]", text, re.M)
        ok = m and not res.get("isError") and not msg.get("error")
        if msg["id"] != own["id"]:   # an attempt that already timed out: its tab is nobody's
            if ok and int(m.group(1)) != page.get("id"):
                call_aside(STALE_CLOSE, "close_page", {"pageId": int(m.group(1))})
            return
        if ok:
            page["id"] = int(m.group(1))
        else:   # it did not open or the reply format differs: do not let the session's call into a foreign tab
            page["why"] = (text or str(msg.get("error") or ""))[:200] or "chrome-devtools-mcp did not answer"
        opened.set()

    def stale(proc, line):
        """A replaced one still says something: the session does not hear it (its unanswered calls got their answer). A late
        answer to its own tab opened a tab that is nobody's: the replaced one closes it before it goes."""
        g = gone.get(proc)
        try:
            msg = json.loads(line) or {}
        except ValueError:
            return
        if not g or msg.get("id") not in (g["id"], STALE_CLOSE):
            return
        m = re.search(r"^(\d+): .*\[selected\]", text_of(msg), re.M)
        if msg["id"] == g["id"] and m and not (msg.get("result") or {}).get("isError"):
            try:
                proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": STALE_CLOSE, "method": "tools/call", "params": {
                    "name": "close_page", "arguments": {"pageId": int(m.group(1))}}}) + "\n").encode())
                proc.stdin.flush()
                return
            except (OSError, ValueError):
                pass
        g["done"].set()

    def answers(proc):
        for line in proc.stdout:
            if proc is not kid["proc"]:
                stale(proc, line)
                continue
            try:
                msg = json.loads(line) or {}
                reply = "result" in msg or "error" in msg   # not a request of chrome-devtools-mcp to the session
                method = (pending.get(msg.get("id")) or {}).get("method") if reply else None
                failed = (msg.get("result") or {}).get("isError") or msg.get("error")
                why = (text_of(msg) + " " + str(msg.get("error") or "")) if failed else ""   # not a page that says so
                sent = asked.pop(msg.get("id"), None) if reply else None
                fast = STUCK in why and sent is not None and time.monotonic() - sent < STUCK_FAST
                if fast and time.monotonic() - last_respawn[0] >= RESPAWN_PAUSE:
                    stuck["n"] = 0   # the last replacement was a while ago (the load may be gone): a new series, as for HUNG
                if HUNG in why or (fast and stuck["n"] < RESPAWNS):
                    stuck["hit"] = fast
                    hung.set()
                elif fast and method == "tools/call":
                    line = failure(msg["id"], f"Browser: chrome-devtools-mcp fails at once even after {stuck['n']} "
                                   f"replacements in a row ({why.strip()[:200]}). Reconnect the session's browser: hub "
                                   f"session reload {os.environ.get('HUB_SESSION') or '<session>'}, or repeat the call in "
                                   f"{RESPAWN_PAUSE} s." + overload_note())
                elif (reply and not failed
                      and (method == "tools/call" or str(msg.get("id")).startswith(OWN_TAB + ":"))):
                    respawned[0] = 0   # a tool call went well: it works (an answer to initialize does not say so)
                    if method == "tools/call":
                        stuck["n"] = 0
                if isinstance(msg.get("id"), str) and msg["id"].startswith(OWN_TAB + ":"):
                    own_reply(msg)
                    continue
                if msg.get("id") == OWN_TAB_CLOSE:
                    closed.set()
                    continue
                if msg.get("id") == STALE_CLOSE:
                    continue
                if isinstance(msg.get("id"), str) and msg["id"].startswith(INIT + ":"):
                    inited.set()
                    continue
                if reply:
                    pending.pop(msg.get("id"), None)
                if msg.get("id") in page_calls:
                    mine = "adopting" in page and page["adopting"] == msg["id"]
                    if mine:
                        del page["adopting"]
                    if (msg.get("result") or {}).get("isError") or msg.get("error"):
                        if mine:   # the page that stood for its own tab did not open: the next call makes its own as usual
                            page.pop("adopted", None)
                    else:
                        own_tab_unused(msg)
                        threading.Thread(target=tidy, daemon=True).start()
                    if mine:
                        adopting.set()
            except (ValueError, AttributeError, TypeError, OSError):
                pass
            touch()
            say(line)
        if proc is not kid["proc"]:
            return
        closed.set()
        os._exit(proc.wait())

    gone = {}   # a replaced chrome-devtools-mcp -> {"id": its own-tab attempt left unanswered, "done": it is settled}

    def let_go(old):
        if old in gone:
            gone[old]["done"].wait(OWN_TAB_WAIT)
        retire(old)
        gone.pop(old, None)

    def start():
        kid["proc"] = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
        threading.Thread(target=answers, args=(kid["proc"],), daemon=True).start()

    def respawn():
        """A new chrome-devtools-mcp instead of the hung one, introduced to it as the session was: initialize (its reply is
        not shown) and notifications/initialized. The session's unanswered calls get an error (a call may have been done,
        so it is not repeated), the other requests (tools/list and the like) go to the new one."""
        old, left = kid["proc"], dict(pending)
        pending.clear()
        asked.clear()
        # the new one numbers the pages anew and selects the first one (a foreign tab): the session gets a tab of its own again
        for k in ("id", "adopted", "adopting"):
            page.pop(k, None)
        adopting.set()
        hung.clear()
        inited.clear()
        respawned[0] += 1
        stuck["n"] += stuck.pop("hit", False)
        last_respawn[0] = time.monotonic()
        print(f"hub-browser: chrome-devtools-mcp (pid {old.pid}) does not connect to the browser — replacing it "
              f"({respawned[0]} of {RESPAWNS})", file=sys.stderr, flush=True)
        if own.get("late"):   # before the swap: its late answer may come at once
            gone[old] = {"id": own.pop("late"), "done": threading.Event()}
        with write:
            start()
        threading.Thread(target=let_go, args=(old,), daemon=True).start()
        init = hello.get("initialize")
        if init and init.get("id") not in left:
            send((json.dumps({**init, "id": f"{INIT}:{respawned[0]}"}) + "\n").encode())
            inited.wait(OWN_TAB_WAIT)
        for id, msg in left.items():
            if msg.get("method") != "tools/call":
                pending[id] = msg
                send((json.dumps(msg) + "\n").encode())
        if "initialized" in hello:
            send(hello["initialized"])
        for id, msg in left.items():
            if msg.get("method") == "tools/call":
                error(id, "Browser: chrome-devtools-mcp hung and was replaced, the call was not made. Repeat the call.")

    def reload_hint():
        return ("Browser: chrome-devtools-mcp does not connect to the browser even after " + str(respawned[0])
                + " replacements in a row. Reconnect the session's browser: hub session reload "
                + (os.environ.get("HUB_SESSION") or "<session>") + f", or repeat the call in {RESPAWN_PAUSE} s."
                + overload_note())

    def new_series():
        """The replacements ran out a while ago (the load may be gone): the next call may replace again."""
        if hung.is_set() and respawned[0] >= RESPAWNS and time.monotonic() - last_respawn[0] >= RESPAWN_PAUSE:
            respawned[0] = 0

    def own_tab():
        """Open its own tab; a hung chrome-devtools-mcp is replaced and asked again. True if it is open."""
        while True:
            if hung.is_set() and respawned[0] < RESPAWNS:
                respawn()
            own["n"] += 1
            own["id"] = f"{OWN_TAB}:{own['n']}"
            opened.clear()
            page.pop("why", None)
            call(own["id"], "new_page", {"url": "about:blank", "background": True})
            if not opened.wait(OWN_TAB_WAIT):
                page["why"] = f"no answer within {OWN_TAB_WAIT} s"
                own["late"] = own["id"]
                hung.set()
                stuck["hit"] = False   # silence, not the kept page failure
            own["id"] = None
            if page.get("id") is not None:
                return True
            if not hung.is_set() or respawned[0] >= RESPAWNS:
                last_respawn[0] = time.monotonic()   # "repeat in N s" counts from this error, not from the replacement
                return False

    start()

    def close_own_tab(*_):
        """The session is gone: close its tab, otherwise empty session tabs pile up in the browser (07.10)."""
        for t in aside:   # a close of the unused own tab may still be on its way
            t.join(3)
        if page.get("id") is None or closed.is_set():
            return
        try:
            call(OWN_TAB_CLOSE, "close_page", {"pageId": page["id"]}, wait=1)
        except OSError:
            return
        closed.wait(3)

    def stop(*_):
        close_own_tab()
        os._exit(0)
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)   # Claude Code sends SIGINT first when it exits: otherwise its tab stays open
    for line in stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            msg = None
        try:
            if isinstance(msg, dict) and msg.get("method") == "initialize":
                hello["initialize"] = msg
            elif isinstance(msg, dict) and msg.get("method") == "notifications/initialized":
                hello["initialized"] = line
            if isinstance(msg, dict) and msg.get("method") == "tools/call":
                touch()
                why = ensure()
                if why:
                    error(msg.get("id"), "Browser: " + why)
                    continue
                if changed():   # another Chrome: its own tab went with the old one
                    page.pop("id", None)
                    page.pop("adopted", None)
                new_series()
                if hung.is_set() and respawned[0] < RESPAWNS:
                    respawn()
                params = msg.get("params")
                if isinstance(params, dict) and params.get("name") == "new_page" and msg.get("id") is not None:
                    page_calls.add(msg["id"])
                    if page.get("id") is None and not page.get("adopted"):
                        # the session opens its page itself: it becomes the current one, a blank tab is not needed
                        page["adopted"], page["adopting"] = True, msg["id"]
                        adopting.clear()
                elif not adopting.is_set() and not adopting.wait(OWN_TAB_WAIT):
                    # until that page is open a call could land in a foreign tab (replies come out of order): wait, as for our own
                    adopting.set()
                if page.get("id") is None and not page.get("adopted"):
                    if not own_tab():
                        # its own tab did not open: do not steer a foreign one (the owner's window): an error to the session, the next call
                        # will try again
                        error(msg.get("id"), reload_hint() if hung.is_set() else (
                            "Browser: the session's own tab did not open (" + page.get("why", "") + ") — the call was "
                            "not made, so as not to work in someone else's tab. Repeat the call."))
                        continue
                    threading.Thread(target=tidy, daemon=True).start()   # its own tab exists: the launch one is not needed
            if isinstance(msg, dict) and msg.get("id") is not None and msg.get("method"):
                pending[msg["id"]] = msg
                if msg["method"] == "tools/call":
                    asked[msg["id"]] = time.monotonic()
            send(line)
        except OSError:
            break
    close_own_tab()
    try:
        kid["proc"].stdin.close()
    except OSError:
        pass
    return kid["proc"].wait()


def chrome_watch(url, identity=browsers.identity):
    """changed() for proxy: True when the Chrome at url is another one than at the previous call. A first call, or an
    answer that does not come (a slow machine, not a closed browser), changes nothing: the tab is kept."""
    last = []

    def changed():
        now = identity(url)
        if now is None:
            return False
        before, last[:] = (last[0] if last else None), [now]
        return before is not None and before != now
    return changed


def mcp(name):
    """A session connects the browser:<name> tool: wait for the browser and replace itself with chrome-devtools-mcp."""
    state_dir = default_state_dir()
    cfg = catalog.load_config(state_dir)
    b = browsers.configured(cfg).get(name)
    if not b or b.get("problem"):
        sys.exit(i18n.t("cli.browser.not_configured_problem", name=name, problem=b["problem"]) if b
                 else i18n.t("cli.browser.not_configured", name=name))
    missing = browsers.mcp_missing(cfg)
    if missing:   # the reason, not a traceback of exec; at once, not after waiting for the browser
        sys.exit(i18n.t("cli.browser.mcp_missing", name=name, program=missing))
    npm_dir = os.path.join(state_dir, "browsers", ".npx")   # empty: see mcp_command; profiles are browsers/<name>
    os.makedirs(npm_dir, exist_ok=True)
    cmd = browsers.mcp_command(cfg, b, npm_dir, private_dir=os.path.join(state_dir, "browsers", ".mcp", name))
    if b["kind"] == "local" and b.get("start") in ("demand", "login"):
        def ensure():
            if ensure_browser(cfg, state_dir, name, b):
                return None
            return unreachable(name, b, alive(browsers.profile_dir(state_dir, name, b)),
                               os.path.join(state_dir, "browsers", f"{name}.log"))
        sys.exit(proxy(cmd, browsers.env(cfg), ensure, tidy=lambda: close_launch_tabs(b["url"]),
                       touch=lambda: browsers.touch_active(state_dir, name), changed=chrome_watch(b["url"])))
    if not wait_for_browser(cfg, state_dir, name, b):
        sys.exit(i18n.t("cli.browser.mcp_no_answer", name=name, url=b["url"], seconds=MCP_WAIT))
    # through the platform: exec on Windows starts a new process and this one exits, so Claude Code would see the MCP
    # server die. npx there is npx.cmd, found only by shutil.which (PATHEXT)
    env = browsers.env(cfg)
    os.environ.clear()
    os.environ.update(env)
    platforms.current.exec_instead([shutil.which(cmd[0], path=env.get("PATH")) or cmd[0], *cmd[1:]])


def status(as_json):
    cfg = catalog.load_config(default_state_dir())
    rows = []
    missing = browsers.mcp_missing(cfg)
    for name, b in browsers.configured(cfg).items():
        v = None if b.get("problem") else browsers.probe(b["url"])
        problem = b.get("problem") or (missing and i18n.t("cli.browser.mcp_missing", name=name, program=missing))
        rows.append({"name": name, "kind": b.get("kind"), "url": b.get("url"), "up": bool(v), "version": v,
                     "problem": problem, "risk": b.get("risk")})
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return
    if not rows:
        print(i18n.t("cli.browser.none"))
    for r in rows:
        state = i18n.t("cli.browser.up", version=r["version"]) if r["up"] else \
            i18n.t("cli.browser.down_problem", problem=r["problem"]) if r["problem"] else i18n.t("cli.browser.down")
        if r["up"] and r["problem"]:   # Chrome answers, its MCP cannot start
            state += f" — {r['problem']}"
        print(f"browser:{r['name']}  {r['kind']}  {r['url']}  {state}"
              + (i18n.t("cli.browser.risk", risk=", ".join(r["risk"])) if r["risk"] else ""))


def serve():
    state_dir = default_state_dir()
    os.makedirs(os.path.join(state_dir, "browsers"), exist_ok=True)
    if not lock.hold(os.path.join(state_dir, "browsers.lock")):
        sys.exit("the browser supervisor is already running: one per machine")
    sup = Supervisor(state_dir)
    if os.environ.pop("HUB_RESTART_ASKED", None):   # hub-up: a restart by `hub restart browser`, not a hub start
        sup.fresh(catalog.load_config(state_dir))
    while True:
        sup.step()
        time.sleep(EVERY)


def main(argv):
    if argv[:1] == ["serve"]:
        serve()
    elif argv[:1] == ["mcp"] and len(argv) == 2:
        mcp(argv[1])
    elif argv[:1] == ["status"]:
        status("--json" in argv)
    else:
        sys.exit(i18n.t("cli.browser.usage"))


if __name__ == "__main__":
    main(sys.argv[1:])
