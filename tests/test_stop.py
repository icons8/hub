"""Hub stop and the process tree (05.10). Windows (platforms/windows.py): hubd stops via the stop pipe from hub-up,
and hub-up runs under a supervisor with a job (Job Object) that kills the process tree when its owner dies. POSIX
(platforms/posix.py): SIGTERM raises the same flag, and the snapshot is taken after the tick."""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from core import journal
from hosts.streamjson import StreamHost
from platforms import windows
from tests.fakes.procs import alive, kill

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUBD, HUB_UP, HUB = (os.path.join(REPO, "bin", n) for n in ("hubd", "hub-up", "hub"))
FAKE = os.path.join(REPO, "tests", "fakes", "fake_claude_stream.py")
WIN = unittest.skipUnless(os.name == "nt", "Windows only")

# a hub part: writes "<name> <pid>"; "hubd" stops like the real one, through the OS adapter (on_core_stop: SIGTERM on
# POSIX, the stop pipe on Windows), finishing its tick (sys.argv[4] seconds), and writes "hubd stop <pid>"; the others
# sleep
PART = """
import os, sys, time
log, name = sys.argv[1], sys.argv[2]
open(log, "a").write(f"{name} {os.getpid()}\\n")
if name == "hubd":
    sys.path.insert(0, sys.argv[3])
    from platforms import current
    stopping = []  # not an Event: set() from a signal handler can block on the lock that wait() holds
    current.on_core_stop(lambda *_: stopping.append(True), lambda: stopping.append(True))
    while not stopping:
        time.sleep(0.1)
    time.sleep(float(sys.argv[4]))
    open(log, "a").write(f"{name} stop {os.getpid()}\\n")
else:
    time.sleep(120)
"""
# "claude": starts a background command and writes its pid; lives while stdin is open (longer with --linger). The
# command has none of its pipes (otherwise it would hold the cleanup itself); with --bash it is `sleep &` through Git
# Bash, like claude's Bash on Windows; with --breakaway it is separate (popen_detached), like the Chrome that the
# session's MCP launcher starts
CLAUDE = """
import os, subprocess, sys, time
sys.path.insert(0, @REPO@)
quiet = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
sleeper = [sys.executable, "-c", "import time; time.sleep(120)"]
if "--help" in sys.argv:   # StreamHost.fresh_prompt asks --help: no command, no pid file
    sys.exit()
if "--bash" in sys.argv:
    out = os.path.join(os.path.dirname(sys.argv[1]), "winpid")
    subprocess.Popen([os.environ["TEST_GIT_BASH"], "-c",
                      "sleep 120 & cat /proc/$!/winpid > '" + out.replace(os.sep, "/") + "'; wait"], **quiet)
    while not (os.path.exists(out) and open(out).read().strip()):
        time.sleep(0.05)
    pid = int(open(out).read())
elif "--breakaway" in sys.argv:
    from platforms import windows
    pid = windows.popen_detached(sleeper, **quiet).pid
else:
    pid = subprocess.Popen(sleeper, **quiet).pid
open(sys.argv[1], "w").write(str(pid))
sys.stdin.buffer.read()
time.sleep(120 if "--linger" in sys.argv else 0)
""".replace("@REPO@", repr(REPO))


def git_bash():
    """bash.exe of Git for Windows (MSYS), not the WSL one from System32."""
    git = shutil.which("git")
    d = os.path.dirname(git) if git else ""
    for _ in range(3):
        for bash in (os.path.join(d, "bin", "bash.exe"), os.path.join(d, "usr", "bin", "bash.exe")):
            if os.path.exists(bash):
                return bash
        d = os.path.dirname(d)
    return None


def until(pred, timeout=30.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.1)
    return False


def text_of(path):
    if not os.path.exists(path):
        return ""
    with open(path) as f:
        return f.read()


def script(text):
    path = os.path.join(tempfile.mkdtemp(prefix="stop-"), "script.py")
    with open(path, "w", encoding="utf-8") as f:  # Python reads the script as UTF-8, and the comments in it are Russian
        f.write(text)
    return path


class HubdStopTest(unittest.TestCase):
    def start(self):
        """hubd as hub-up starts it (platforms: spawn_part); we wait for catalog.loaded: the catalog is built after
        `git rev-parse`, where hubd used to hang while the stop was read from stdin (05.10)."""
        self.state = tempfile.mkdtemp(prefix="hubd-stop-")
        env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_CLAUDE": f"{sys.executable} {FAKE}"}
        from platforms import current
        self.p, self.handle = current.spawn_part([sys.executable, HUBD], True, env=env, stdin=subprocess.PIPE,
                                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (self.p.poll() is None and (self.p.kill(), self.p.wait()), self.p.stdin.close()))
        path = os.path.join(self.state, "journal.jsonl")
        self.assertTrue(until(lambda: os.path.exists(path) and any(
            e["type"] == "catalog.loaded" for e in journal.read(path))), "hubd did not come up")
        return path

    def last_snapshot(self, path):
        snaps = [e for e in journal.read(path) if e["type"] == "core.snapshot"]
        return snaps[-1]["data"]["why"] if snaps else None

    @unittest.skipIf(os.name == "nt", "POSIX")
    def test_sigterm_stops_with_a_snapshot(self):
        # POSIX: SIGTERM, snapshot after the tick
        path = self.start()
        self.p.send_signal(signal.SIGTERM)
        self.assertEqual(self.p.wait(30), 0)
        self.assertEqual(self.last_snapshot(path), "stop")

    @WIN
    def test_closed_stop_channel_stops_with_a_snapshot(self):
        # terminate() is TerminateProcess, the SIGTERM handler is not called, so there was no "stop" snapshot; hub-up
        # now closes the stop pipe for hubd and it stops itself
        path = self.start()
        windows.stop_part(self.p, self.handle)
        self.assertEqual(self.p.wait(30), 0)
        self.assertEqual(self.last_snapshot(path), "stop")

    @WIN
    def test_stdin_is_not_a_stop_signal(self):
        path = self.start()
        self.addCleanup(os.close, self.handle)
        self.p.stdin.close()
        time.sleep(2)
        self.assertIsNone(self.p.poll())  # stdin is a terminal or someone else's pipe: EOF on it is no reason to stop
        self.assertNotEqual(self.last_snapshot(path), "stop")

    @WIN
    @unittest.skipUnless(shutil.which("git"), "no git")
    def test_children_of_a_watching_process_do_not_hang(self):
        # 05.10, live hub: hubd with stop-by-stdin hung on the first `git rev-parse`, because git inherited stdin that
        # another thread was reading. The stop pipe does not pass to children, and nobody reads stdin
        child = script("import sys, subprocess, time\nsys.path.insert(0, sys.argv[1])\nfrom platforms import current\n"
                       "current.on_core_stop(lambda *_: None, lambda: None)\ntime.sleep(0.5)\n"
                       "try:\n    subprocess.run(['git', '--version'], capture_output=True, timeout=15)\n"
                       "    print('ok', flush=True)\n"
                       "except subprocess.TimeoutExpired:\n    print('hung', flush=True)\n")
        p, handle = windows.spawn_part([sys.executable, child, REPO], True, stdout=subprocess.PIPE,
                                       stdin=subprocess.PIPE)
        try:
            line = p.stdout.readline().decode().strip()  # the child's stdin is open, as for parts under hub-up
        finally:
            os.close(handle)
            p.stdin.close()
            p.wait(30)
            p.stdout.close()
        self.assertEqual(line, "ok")


class HubUpStopTest(unittest.TestCase):
    def start(self, hubd_tick=0, python=sys.executable):
        self.state = tempfile.mkdtemp(prefix="hubup-stop-")
        self.log = os.path.join(self.state, "parts.log")
        self.seen = {}   # pid -> when it was first read from the log (procs.kill: the process started by then)
        part = script(PART)
        # a service is only the hub's own: a runner under systemd (INVOCATION_ID) or launchd must not make hub-up a
        # "service"
        env = {k: v for k, v in os.environ.items() if k not in ("HUB_SERVICE", "INVOCATION_ID", "XPC_SERVICE_NAME")}
        env.update(HUB_STATE_DIR=self.state, HUB_UP_PARTS=json.dumps(
            {"hubd": [part, self.log, "hubd", REPO, str(hubd_tick)], "web": [part, self.log, "web"]}))
        # stdin is empty: the stop must not depend on it (a runner's stdin can be closed or an open pipe)
        self.p = subprocess.Popen([python, HUB_UP], env=env, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.cleanup)
        self.assertTrue(until(lambda: len(self.pids()) == 2), "parts did not come up")
        return env

    def cleanup(self):
        if self.p.poll() is None:  # by its own stop: kill() on POSIX would leave the parts alive
            open(os.path.join(self.state, "stop"), "w").close()
            try:
                self.p.wait(90)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait()
        for pid in self.pids().values():
            kill(pid, self.seen[pid])

    def lines(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [x.split() for x in f.read().splitlines() if x]

    def pids(self):
        pids = {x[0]: int(x[1]) for x in self.lines() if len(x) == 2}
        if pids:
            at = time.time()   # after the read: every process read started by then
            for pid in pids.values():
                self.seen.setdefault(pid, at)
        return pids

    def test_hub_stop_waits_for_hubd_and_stops_everything(self):
        env = self.start()
        hubd = self.pids()["hubd"]
        started = time.time()
        out = subprocess.run([sys.executable, HUB, "stop"], env=env, capture_output=True, text=True,
                             encoding="utf-8", timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.p.wait(30), 0)
        self.assertLess(time.time() - started, 12)  # hub-up did not wait its 15 s to kill hubd
        self.assertEqual([x for x in self.lines() if x[1] == "stop"], [["hubd", "stop", str(hubd)]])  # by itself, once
        self.assertTrue(until(lambda: not any(alive(pid) for pid in self.pids().values())))

    @WIN
    def test_hubd_finishing_a_long_tick_still_stops_by_itself(self):
        # PR #18 review: on Windows hubd writes the "stop" snapshot after the tick, while hub-up waited only 15 s and
        # then killed it, so there was no snapshot. The core tick takes 17 s: hub-up waits, hubd stops by itself
        env = self.start(hubd_tick=17)
        hubd = self.pids()["hubd"]
        out = subprocess.run([sys.executable, HUB, "stop"], env=env, capture_output=True, text=True,
                             encoding="utf-8", timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.p.wait(90), 0)
        self.assertEqual([x for x in self.lines() if x[1] == "stop"], [["hubd", "stop", str(hubd)]])

    @WIN
    def test_supervisor_waits_for_hubd_when_hub_up_dies(self):
        # PR #18 review: hub-up was killed by pid (Task Manager, taskkill) or crashed, and the supervisor exited at
        # once; the job killed hubd midway through a stop on the closed pipe, so there was no "stop" snapshot. Now it
        # waits for the core
        self.hub_up_dies(sys.executable)

    @WIN
    def test_supervisor_waits_for_hubd_also_from_a_venv(self):
        # review: the venv python.exe is a launcher and the real Python is its child; the supervisor knew the
        # launcher's pid while hub-up.json held the real one, so the core was not found and there was nobody to wait for
        venv = tempfile.mkdtemp(prefix="hub-venv-")
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", venv], check=True, timeout=120)
        self.hub_up_dies(os.path.join(venv, "Scripts", "python.exe"))

    def hub_up_dies(self, python):
        self.start(hubd_tick=3, python=python)
        hubd = self.pids()["hubd"]
        with open(os.path.join(self.state, "hub-up.json")) as f:
            up = json.load(f)
        self.assertEqual(up["core"], hubd)
        subprocess.run(["taskkill", "/PID", str(up["pid"]), "/F"], capture_output=True)
        self.p.wait(60)
        self.assertEqual([x for x in self.lines() if x[1] == "stop"], [["hubd", "stop", str(hubd)]])
        self.assertTrue(until(lambda: not any(alive(pid) for pid in self.pids().values())))

    @WIN
    def test_parts_get_utf8_and_the_owners_value_for_sessions(self):
        # hub parts run in UTF-8 (PYTHONUTF8=1 from the supervisor), and the owner's original value travels with them,
        # for owner_env
        probe = script("import json, os, sys, time\nopen(sys.argv[1], 'w').write(json.dumps("
                       "[os.environ.get('PYTHONUTF8'), os.environ.get('HUB_UP_PYTHONUTF8_WAS')]))\ntime.sleep(120)\n")
        seen = os.path.join(tempfile.mkdtemp(prefix="utf8-env-"), "seen")
        self.state = tempfile.mkdtemp(prefix="hubup-utf8-")
        self.log = os.path.join(self.state, "parts.log")
        env = {k: v for k, v in os.environ.items() if k != "PYTHONUTF8"}
        env.update(HUB_STATE_DIR=self.state, HUB_UP_PARTS=json.dumps({"web": [probe, seen]}))
        self.p = subprocess.Popen([sys.executable, HUB_UP], env=env, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (self.p.kill(), self.p.wait(30)))
        self.assertTrue(until(lambda: text_of(seen)))
        self.assertEqual(json.loads(text_of(seen)), ["1", ""])

    @WIN
    def test_killed_supervisor_takes_the_whole_tree(self):
        # 05.10: after `taskkill /F` on hub-up, hubd, web, files and hubsched stayed alive and held ports and
        # hubd.lock. Now everything is in the supervisor's job: when it dies, all die
        self.start()
        self.p.kill()  # TerminateProcess, like "End task" and taskkill /F
        self.p.wait(30)
        self.assertTrue(until(lambda: not any(alive(pid) for pid in self.pids().values())))


@WIN
class JobTest(unittest.TestCase):
    def test_terminate_takes_children_started_later(self):
        pid_file = os.path.join(tempfile.mkdtemp(prefix="job-"), "child")
        root = subprocess.Popen([sys.executable, script(CLAUDE), pid_file], stdin=subprocess.PIPE)
        job = windows.Job()
        self.assertTrue(job.add(root.pid))
        self.assertTrue(until(lambda: text_of(pid_file)))
        child = int(text_of(pid_file))
        self.addCleanup(kill, child, time.time())
        job.terminate()
        job.close()
        root.wait(30)
        root.stdin.close()
        self.assertTrue(until(lambda: not alive(child)))

    def test_breakaway_child_outlives_the_job(self):
        # session Chrome (modules/browser start_local) leaves the hub's job and outlives it
        holder = subprocess.Popen([sys.executable, "-c", (
            "import subprocess, sys, time; sys.path.insert(0, sys.argv[1]); from platforms import windows\n"
            "job = windows.Job(kill_on_close=True, breakaway_ok=True); assert job.add_current()\n"
            "inside = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
            "out = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],"
            " creationflags=0x00000008 | 0x00000200 | windows.BREAKAWAY_FROM_JOB)\n"
            "print(inside.pid, out.pid, flush=True); time.sleep(120)"), REPO],
            stdout=subprocess.PIPE, text=True)
        inside, out = (int(x) for x in holder.stdout.readline().split())
        holder.stdout.close()
        for pid in (inside, out):
            self.addCleanup(kill, pid, time.time())
        holder.kill()
        holder.wait(30)
        self.assertTrue(until(lambda: not alive(inside)))
        self.assertTrue(alive(out))


@WIN
class SessionTreeTest(unittest.TestCase):
    def host(self, *extra):
        self.dir = tempfile.mkdtemp(prefix="sess-tree-")
        self.pid_file = os.path.join(self.dir, "child")
        host = StreamHost(self.dir, claude=[sys.executable, script(CLAUDE), self.pid_file, *extra])
        host.spawn("a", "00000000-0000-0000-0000-000000000001", cwd=self.dir)
        self.addCleanup(host.shutdown)
        self.assertTrue(until(lambda: text_of(self.pid_file)))
        child = int(text_of(self.pid_file))
        self.addCleanup(kill, child, time.time())
        return host, child

    def test_kill_takes_background_commands(self):
        # 05.10: after the hub stopped, a session's `bash -c "… sleep 300"` stayed an orphan, since only claude was
        # killed
        host, child = self.host("--linger")
        host.kill("a")
        self.assertTrue(until(lambda: not alive(child), 10))

    @unittest.skipUnless(git_bash(), "no Git Bash")
    def test_a_git_bash_background_command_dies_with_the_session(self):
        # PR #18 review: with BREAKAWAY_OK on the session job, MSYS gave bash's children CREATE_BREAKAWAY_FROM_JOB,
        # so `sleep &` left the job and outlived the session
        os.environ["TEST_GIT_BASH"] = git_bash()
        self.addCleanup(os.environ.pop, "TEST_GIT_BASH", None)
        host, sleep = self.host("--bash", "--linger")
        host.kill("a")
        self.assertTrue(until(lambda: not alive(sleep), 10))

    def test_chrome_started_by_the_session_lives_with_it(self):
        # the session's MCP launcher starts Chrome itself when the browser part is not running: the session job does not
        # let it out (BREAKAWAY_OK would also give orphans to Git Bash commands), and popen_detached works without
        # leaving the job
        host, chrome = self.host("--linger", "--breakaway")
        host.kill("a")
        self.assertTrue(until(lambda: not alive(chrome), 10))

    def test_release_sweeps_what_claude_left(self):
        host, child = self.host()
        started = time.time()
        host.release("a")  # claude exited by itself on EOF, its background command did not
        self.assertLess(time.time() - started, 10)
        self.assertTrue(until(lambda: not alive(child), 10))

    def test_reap_finds_the_old_process_without_ps(self):
        # reap() called `ps -o`, which does not exist on Windows (or is Git Bash's ps without -o), so a process from a
        # past life was not killed
        sid = "00000000-0000-0000-0000-0000000000aa"
        d = tempfile.mkdtemp(prefix="reap-")
        pid_file = os.path.join(d, "child")
        old = subprocess.Popen([sys.executable, script(CLAUDE), pid_file, "--output-format", "stream-json",
                                "--session-id", sid, "--linger"], stdin=subprocess.PIPE)
        self.addCleanup(lambda: (old.poll() is None and old.kill(), old.stdin.close()))
        self.assertTrue(until(lambda: text_of(pid_file)))
        child = int(text_of(pid_file))
        self.addCleanup(kill, child, time.time())
        host = StreamHost(d)
        self.assertFalse(host.reap(old.pid, "00000000-0000-0000-0000-0000000000bb"))  # someone else's sessionId: leave it alone
        self.assertTrue(host.reap(old.pid, sid))
        old.wait(30)
        self.assertTrue(until(lambda: not alive(child)))


if __name__ == "__main__":
    unittest.main()
