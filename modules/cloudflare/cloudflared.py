"""cloudflared as a part of the hub (the tunnel `hub remote cloudflare setup` made): the pinned build is fetched once
into the state folder (core/fetch: sha256 and size pinned here), and the cloudflare part (run.py) runs it as its child —
the tunnel lives while the part lives.

The child: its own process group (platforms.current.apart(); on Windows a job that dies with the part), the tunnel's
token in the environment (TUNNEL_TOKEN — not in the arguments, which `ps` shows), metrics on a free port of 127.0.0.1
chosen at each start (two hubs on one machine never read each other's /ready), no self-update (the pin decides the
version), no diagnostics over Cloudflare's management service. A child left by a part that was killed (no chance to
stop it) is found by its pid file and stopped at the next start, if it is still this hub's cloudflared (its program,
this state folder): two connectors of one tunnel would share its traffic. On Windows the part's job ends the child.
"""
import hashlib
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import time

import platforms

VERSION = "2026.10.7"
DOWNLOADS = "https://github.com/cloudflare/cloudflared/releases/download"
ROOT_ENV = "HUB_CLOUDFLARED_DOWNLOADS"   # tests: a fake server (the sha256 below still decides)
# (OS, machine) -> (file of the release, sha256, size); checked against the release's digests (GitHub) on 2026-11-01
FILES = {
    ("linux", "amd64"): ("cloudflared-linux-amd64",
                         "1f99fb518b83dec2ba8d870a7cc991d82a77e6196295a3d2754ba17ab9f91872", 40274445),
    ("linux", "arm64"): ("cloudflared-linux-arm64",
                         "3df43aedc9ff291d1735b5cb576cb97e0b157488796db2e32b28aecabb445775", 37667293),
    ("darwin", "amd64"): ("cloudflared-darwin-amd64.tgz",
                          "02f3edfd87684dd5b705607688e4eb4b323f6e777b81e7b3a85af556aa356848", 21834416),
    ("darwin", "arm64"): ("cloudflared-darwin-arm64.tgz",
                          "3586ccf397e98e5f437e60e7c307ca6ac463e110580503af8bb8e6f9ebf831bd", 19847161),
    ("windows", "amd64"): ("cloudflared-windows-amd64.exe",
                           "2493dbc168923bb3e59e5e7716f71deced92992eea9acd6e9d74f84ac7751343", 55390512),
}
MACHINES = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}
MAX_BINARY = 200 << 20   # bytes taken out of the macOS archive at most
PIDFILE = "cloudflared.pid"
LOG = "cloudflared.log"
LOG_MAX = 5 << 20        # the log starts over past this
BACKOFF = (5, 15, 60, 300)   # seconds before starting again after an exit
STEADY = 600             # running this long, a later exit starts the backoff over


class Missing(Exception):
    """No pinned build for this machine and no cloudflared on PATH."""


def system():
    name = "darwin" if sys.platform == "darwin" else "windows" if os.name == "nt" else sys.platform
    return name, MACHINES.get(platform.machine().lower(), platform.machine().lower())


def home(state_dir):
    return os.path.join(state_dir, "cloudflared", VERSION)


def path(state_dir):
    return os.path.join(home(state_dir), "cloudflared.exe" if os.name == "nt" else "cloudflared")


def ensure(state_dir, say=None):
    """The path of the cloudflared this hub runs: the pinned build (fetched now when missing; core.fetch.Failed when it
    could not be) or, for a machine without a pinned build, the one on PATH (Missing when none)."""
    from core import fetch, i18n
    pinned = FILES.get(system())
    if not pinned:
        found = shutil.which("cloudflared")
        if found:
            return found
        raise Missing(i18n.t("cloudflare.setup.no_binary", system="-".join(system())))
    target = path(state_dir)
    if os.path.isfile(target):
        return target
    name, sha, size = pinned
    os.makedirs(home(state_dir), exist_ok=True)
    got = os.path.join(home(state_dir), name)
    if say:
        say(i18n.t("cloudflare.setup.fetching", version=VERSION, mb=f"{size / 1e6:.0f}"))
    base = os.environ.get(ROOT_ENV) or DOWNLOADS
    fetch.download(f"{base.rstrip('/')}/{VERSION}/{name}", got, sha, size, "cloudflared",
                   "hub remote cloudflare setup")
    if name.endswith(".tgz"):
        _unpack(got, target)
        os.remove(got)
    else:
        os.replace(got, target)
    os.chmod(target, 0o700)
    return target


def _unpack(archive, target):
    """The one `cloudflared` file of the macOS archive (a regular file, no links, no paths) -> target."""
    with tarfile.open(archive, "r:gz") as tar:
        member = next((m for m in tar.getmembers() if m.name.lstrip("./") == "cloudflared"), None)
        if member is None or not member.isfile() or member.size > MAX_BINARY:
            raise OSError("no cloudflared in the archive")
        src = tar.extractfile(member)
        with open(target + ".part", "wb") as out:
            shutil.copyfileobj(src, out)
    os.replace(target + ".part", target)


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fingerprint(token):
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def _runs(pid, binary):
    """Does this live process run `binary` (not another program that got the pid since, not the owner's own
    cloudflared)? POSIX: /proc or ps; the program or, for a script, its interpreter's first argument."""
    try:
        if os.path.isdir("/proc"):
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                return binary in [x.decode(errors="replace") for x in f.read().split(b"\0")[:2]]
        command = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                                 timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return binary in command   # the path is inside this hub's state folder: no other program has it


def wait_stopped(state_dir, timeout=15.0, sleep=time.sleep):
    """Wait until the part has stopped this hub's cloudflared (its pid file is gone), at most `timeout` seconds."""
    end = time.monotonic() + timeout
    while os.path.exists(os.path.join(state_dir, PIDFILE)) and time.monotonic() < end:
        sleep(0.5)


class Connector:
    """The tunnel's cloudflared, run by the cloudflare part: want() keeps it running with this token (started again
    after an exit, with a backoff), stop() ends it."""

    def __init__(self, state_dir, popen=subprocess.Popen, clock=time.monotonic):
        self.state_dir, self.popen, self.clock = state_dir, popen, clock
        self.proc = self.tree = None
        self.metrics = None      # "127.0.0.1:N" of the running child
        self.token = None        # fingerprint of the token it runs with
        self.started = self.exits = 0
        self.next_start = float("-inf")
        self.leftover()

    def pidfile(self):
        return os.path.join(self.state_dir, PIDFILE)

    def leftover(self):
        """Stop the cloudflared a killed part of this hub left running (by its pid file), so one tunnel has one
        connector. A pid file copied from another hub, or a pid now held by another program, is left alone."""
        try:
            with open(self.pidfile(), encoding="utf-8") as f:
                got = json.load(f)
            pid, binary = int(got.get("pid") or 0), got.get("binary")
            ours = got.get("state_dir") == os.path.realpath(self.state_dir)
        except (OSError, ValueError, AttributeError, TypeError):
            return
        if os.name != "nt" and ours and pid > 0 and isinstance(binary, str) and platforms.current.alive(pid) \
                and _runs(pid, binary):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        self._forget()

    def _forget(self):
        try:
            os.unlink(self.pidfile())
        except OSError:
            pass

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def want(self, binary, token):
        """Keep cloudflared running with `token`; -> running now."""
        self.trim()
        if self.running() and self.token != fingerprint(token):
            self.stop()   # a new token (rotate): the old one no longer works
        if self.proc is not None and not self.running():
            self.exits = 1 if self.clock() - self.started >= STEADY else self.exits + 1
            self.next_start = self.clock() + BACKOFF[min(self.exits, len(BACKOFF)) - 1]
            self._end()
        if self.proc is None and self.clock() >= self.next_start:
            self.start(binary, token)
        return self.running()

    def log(self):
        return os.path.join(self.state_dir, "logs", LOG)

    def trim(self):
        """The log starts over past LOG_MAX, also under a child that runs for weeks (it appends: O_APPEND; Windows
        does not give the child an appending handle — there it starts over at the next start)."""
        if os.name == "nt" and self.running():
            return
        try:
            if os.path.getsize(self.log()) > LOG_MAX:
                os.truncate(self.log(), 0)
        except OSError:
            pass

    def start(self, binary, token):
        port = free_port()
        log_path = self.log()
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        env = {**os.environ, "TUNNEL_TOKEN": token}
        args = [binary, "tunnel", "--no-autoupdate", "--management-diagnostics=false", "--metrics",
                f"127.0.0.1:{port}", "run"]
        with open(log_path, "ab") as log:
            try:
                self.proc = self.popen(args, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                       **platforms.current.apart())
            except OSError as e:
                self.proc = None
                self.next_start = self.clock() + BACKOFF[-1]
                log.write(f"hub: cloudflared did not start: {e}\n".encode())
                return
        self.tree = platforms.current.session_tree(self.proc)
        self.metrics, self.token, self.started = f"127.0.0.1:{port}", fingerprint(token), self.clock()
        try:
            from core import catalog
            catalog.write_private(self.pidfile(), {"pid": self.proc.pid, "binary": binary,
                                                   "state_dir": os.path.realpath(self.state_dir)})
        except OSError:
            pass

    def _end(self):
        if self.tree is not None:
            platforms.current.end_tree(self.tree)
        self.proc = self.tree = self.metrics = self.token = None
        self._forget()

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                try:
                    self.proc.wait(5)
                except subprocess.TimeoutExpired:
                    pass
        if self.proc is not None:
            self._end()
        self.next_start = float("-inf")
