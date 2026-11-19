"""Out of memory on Linux (23.10: an executor's script ate the memory, the OS kernel killed it alone, and systemd with
the default OOMPolicy=stop stopped the whole hub: all sessions, the core, the page). Now the service survives
(OOMPolicy=continue: in the installer's unit, and for units of earlier installs through a drop-in that hub-up writes),
and the OS picks test runs first as victims (oom_score_adj 1000), then sessions and everything they started (800);
the core, the page and hub-up stay as they were.

Checked against a substituted /proc folder and a substituted systemctl: the test never touches real hub processes or
machine services."""
import tests  # noqa: F401 — hide the machine's settings
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from platforms import posix

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def adj(proc, pid, value=None):
    path = os.path.join(proc, str(pid), "oom_score_adj")
    if value is not None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(f"{value}\n")
    with open(path) as f:
        return int(f.read())


class Score(unittest.TestCase):
    def setUp(self):
        self.proc = tempfile.mkdtemp(prefix="proc-")
        self.addCleanup(shutil.rmtree, self.proc, True)
        for name, value in (("PROC", self.proc), ("LINUX", True)):
            p = mock.patch.object(posix, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_raises_the_score(self):
        adj(self.proc, 42, 200)
        posix.oom_first(42, 800)
        self.assertEqual(adj(self.proc, 42), 800)

    def test_never_lowers_it(self):
        # a session started from a test run (1000) does not become more valuable to the OS; a user cannot go below their
        # own anyway
        adj(self.proc, 42, 1000)
        posix.oom_first(42, 800)
        self.assertEqual(adj(self.proc, 42), 1000)

    def test_errors_are_silent(self):
        posix.oom_first(42, 800)            # no such process (gone at once)
        os.makedirs(os.path.join(self.proc, "43"))
        with open(os.path.join(self.proc, "43", "oom_score_adj"), "w") as f:
            f.write("x")
        posix.oom_first(43, 800)            # not a number: leave it alone

    def test_other_systems_untouched(self):
        adj(self.proc, 42, 200)
        with mock.patch.object(posix, "LINUX", False):
            posix.oom_first(42, 800)
        self.assertEqual(adj(self.proc, 42), 200)

    def test_windows_has_the_same_call(self):
        from platforms import windows
        self.assertIsNone(windows.oom_first(42, 800))


@unittest.skipUnless(sys.platform.startswith("linux"), "oom_score_adj exists only on Linux")
class RealProc(unittest.TestCase):
    def test_a_child_gets_the_score_and_passes_it_down(self):
        # the child first lowers its own score (down to the user's lower limit, which is allowed): under tools/test.py
        # it is already 1000, and the "raised" check would be empty
        child = subprocess.Popen([sys.executable, "-c", "import subprocess, sys\n"
                                  "open('/proc/self/oom_score_adj', 'w').write('500')\n"
                                  "print(open('/proc/self/oom_score_adj').read().strip(), flush=True); sys.stdin.readline()\n"
                                  "print(open('/proc/self/oom_score_adj').read().strip(), subprocess.run(['cat', "
                                  "'/proc/self/oom_score_adj'], capture_output=True, text=True).stdout.strip())"],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.kill)
        if child.stdout.readline().strip() != "500":
            self.skipTest("the score cannot be lowered here")
        posix.oom_first(child.pid, 600)
        self.assertEqual(child.communicate("\n", timeout=10)[0].split(), ["600", "600"])


class Session(unittest.TestCase):
    def test_spawned_claude_gets_the_session_score(self):
        from hosts import streamjson
        from hosts.streamjson import StreamHost
        state = tempfile.mkdtemp(prefix="oom-host-")
        self.addCleanup(shutil.rmtree, state, True)
        host = StreamHost(state, claude=(sys.executable, "-c", "import sys, time; '--help' in sys.argv or time.sleep(30)"))
        calls = []
        with mock.patch.object(streamjson.platforms.current, "oom_first", lambda pid, score: calls.append((pid, score))):
            host.spawn("a", "00000000-0000-0000-0000-000000000001")
        self.addCleanup(host.kill_server)
        self.assertEqual(calls, [(host.procs["a"].popen.pid, streamjson.OOM_SESSION)])
        self.assertEqual(streamjson.OOM_SESSION, 800)


@unittest.skipUnless(sys.platform.startswith("linux"), "oom_score_adj exists only on Linux")
class TestRun(unittest.TestCase):
    """tools/test.py is the first victim: 1000 for itself, and the chunks and everything under them inherit it."""
    def test_chunks_run_with_the_highest_score(self):
        out = tempfile.mkdtemp(prefix="oom-run-")
        self.addCleanup(shutil.rmtree, out, True)
        dump = os.path.join(out, "adj")
        # the outer run is already 1000, so the inner one starts with a lowered score: it must set 1000 itself
        r = subprocess.run(["sh", "-c", 'echo 500 > /proc/self/oom_score_adj && exec "$@"', "sh", sys.executable,
                            os.path.join(REPO, "tools", "test.py"), "-j", "1", "tests.test_oom.Probe.test_dump"], cwd=REPO, capture_output=True, text=True, timeout=120,
                           env={**os.environ, "OOM_PROBE": dump})
        self.assertTrue(r.stdout.rstrip().endswith("OK"), r.stdout + r.stderr)
        with open(dump) as f:
            self.assertEqual(f.read().strip(), "1000")


class Probe(unittest.TestCase):
    """A chunk of the TestRun run: writes its own oom_score_adj (skipped outside it)."""
    def test_dump(self):
        if not os.environ.get("OOM_PROBE"):
            self.skipTest("only from TestRun")
        with open("/proc/self/oom_score_adj") as f, open(os.environ["OOM_PROBE"], "w") as out:
            out.write(f.read())


class OldUnit(unittest.TestCase):
    """A unit from an earlier install without OOMPolicy: a code update does not rewrite it (the installer does not run).
    hub-up, the main process of its own systemd --user service, writes the drop-in `<unit>.d/oom.conf` and reloads
    systemd (daemon-reload: the service is not restarted, the rule applies at once)."""
    UNIT = "agents-hub.service"

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="oom-home-")
        self.addCleanup(shutil.rmtree, self.home, True)
        self.units = os.path.join(self.home, ".config", "systemd", "user")
        os.makedirs(self.units)
        self.write_unit("[Service]\nExecStart=/usr/bin/python3 /x/bin/hub-up\nRestart=on-failure\n")
        self.cgroup = os.path.join(self.home, "cgroup")
        self.set_cgroup(f"0::/user.slice/user-1000.slice/user@1000.service/app.slice/{self.UNIT}\n")
        self.calls = []
        self.elsewhere = ""   # unit drop-ins from other systemd folders (/etc/systemd/user/…): only `cat` sees them
        p = mock.patch.dict(os.environ, {"SYSTEMD_EXEC_PID": str(os.getpid())})
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("XDG_CONFIG_HOME", None)

    def write_unit(self, text):
        with open(os.path.join(self.units, self.UNIT), "w") as f:
            f.write(text)

    def set_cgroup(self, text):
        with open(self.cgroup, "w") as f:
            f.write(text)

    def run_cmd(self, cmd, **kw):
        self.calls.append(cmd)
        if cmd[2] == "cat":   # like systemctl: the unit and all its drop-ins
            import glob
            out = ""
            for path in [os.path.join(self.units, self.UNIT), *sorted(glob.glob(self.dropin()[:-8] + "*.conf"))]:
                with open(path) as f:
                    out += f"# {path}\n{f.read()}\n"
            return subprocess.CompletedProcess(cmd, 0, out + self.elsewhere, "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def keep(self):
        return posix.keep_on_oom(cgroup=self.cgroup, home=self.home, run=self.run_cmd)

    def dropin(self):
        return os.path.join(self.units, self.UNIT + ".d", "oom.conf")

    def test_adds_the_policy_and_reloads(self):
        self.assertTrue(self.keep())
        with open(self.dropin()) as f:
            self.assertEqual(f.read(), "[Service]\nOOMPolicy=continue\n")
        self.assertEqual(self.calls, [["systemctl", "--user", "cat", self.UNIT], ["systemctl", "--user", "daemon-reload"]])
        self.calls.clear()
        self.assertFalse(self.keep())          # the second time: already there
        self.assertEqual(self.calls, [["systemctl", "--user", "cat", self.UNIT]])

    def test_a_policy_of_the_owner_stays(self):
        plain = "[Service]\nExecStart=/usr/bin/python3 /x/bin/hub-up\n"
        self.write_unit(plain + "OOMPolicy = kill\n")   # systemd allows spaces around "="
        self.assertFalse(self.keep())
        self.write_unit(plain)
        os.makedirs(os.path.dirname(self.dropin()))
        with open(os.path.join(os.path.dirname(self.dropin()), "mine.conf"), "w") as f:
            f.write("[Service]\nOOMPolicy=stop\n")
        self.assertFalse(self.keep())
        os.unlink(os.path.join(os.path.dirname(self.dropin()), "mine.conf"))
        self.elsewhere = "# /etc/systemd/user/agents-hub.service.d/50-oom.conf\n[Service]\nOOMPolicy=stop\n"
        self.assertFalse(self.keep())
        self.assertFalse(os.path.exists(self.dropin()))
        self.assertNotIn(["systemctl", "--user", "daemon-reload"], self.calls)

    def test_systemctl_cannot_tell(self):
        # cannot tell whether the user has their own rule: do not write the drop-in
        fail = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "Failed to connect to bus")  # noqa: E731
        self.assertFalse(posix.keep_on_oom(cgroup=self.cgroup, home=self.home, run=fail))
        self.assertFalse(os.path.exists(self.dropin()))

    def test_not_a_user_service_main_process(self):
        with mock.patch.dict(os.environ, {"SYSTEMD_EXEC_PID": "1"}):   # from a terminal, tmux, a session under the service
            self.assertFalse(self.keep())
        self.set_cgroup(f"0::/system.slice/{self.UNIT}\n")              # a system service: not our file
        self.assertFalse(self.keep())
        self.set_cgroup("0::/user.slice/user-1000.slice/user@1000.service/app.slice/other.service\n")
        self.assertFalse(self.keep())                                     # the unit is not in the user's folder
        self.assertFalse(os.path.exists(self.dropin()))
        self.assertEqual(self.calls, [])

    def test_installer_unit_has_it_already(self):
        with open(os.path.join(REPO, "deploy", "install.sh")) as f:
            self.assertIn("\nOOMPolicy=continue\n", f.read())

    def test_hub_up_calls_it(self):
        with open(os.path.join(REPO, "bin", "hub-up")) as f:
            self.assertIn("plat.keep_on_oom()", f.read())

    def test_failures_are_silent(self):
        os.chmod(self.units, 0o500)
        self.addCleanup(os.chmod, self.units, 0o700)
        if os.access(self.units, os.W_OK):
            self.skipTest("root writes into a closed folder too")
        self.assertFalse(self.keep())

        def reload_fails(cmd, **kw):
            if cmd[2] == "daemon-reload":
                raise subprocess.TimeoutExpired(cmd, 10)
            return self.run_cmd(cmd)
        os.chmod(self.units, 0o700)
        self.assertTrue(posix.keep_on_oom(cgroup=self.cgroup, home=self.home, run=reload_fails))


if __name__ == "__main__":
    unittest.main()
