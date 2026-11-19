"""`hub run` in the daemon (core/runjobs.py) with the real wrapper: a run starts apart, its outcome comes once, a run the
wrapper did not take in time is cancelled and never runs, a new life of the core picks a started run up without a
second start, a stop asks the wrapper and then stops it hard, a lost wrapper's group is stopped before the fact, a failed
OS call is not_started (never a run inside the hub), the systemd unit's settings, the folders are cleaned up."""
import tests  # noqa: F401 — keep this machine's settings out
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import platforms
from core import hubrun, reducer, runjobs
from platforms import runwrap

POSIX = os.name != "nt"
BASH = shutil.which("bash")


def env_without_service():
    return {k: v for k, v in os.environ.items() if k not in ("INVOCATION_ID", "HUB_TREE")}


class Stand:
    """A state with sessions, folded the core's way, and a Runner whose facts go into it."""

    def __init__(self, test, **runner):
        self.test, self.seq = test, 0
        self.dir = tempfile.mkdtemp(prefix="hubrun-state-")
        test.addCleanup(platforms.current.rmtree, self.dir)
        self.st = reducer.empty()
        self.facts = []
        for name in ("maps",):
            self.ev("session.requested", name, {"session_id": "s", "cwd": "/w", "channel": "cli", "owner": True})
            self.ev("session.started", name, {"session_id": "s"})
        self.runner = self.new_runner(**runner)

    def new_runner(self, **kw):
        kw.setdefault("env", env_without_service())
        return runjobs.Runner(self.dir, self.record, **kw)

    def ev(self, type, entity=None, data=None, src="test"):
        self.seq += 1
        e = {"seq": self.seq, "ts": time.time(), "src": src, "type": type, "entity": entity, "data": data or {}}
        self.st, _, why = reducer.reduce(self.st, e)
        return why

    def record(self, type, entity=None, data=None, src="core"):
        self.facts.append((type, entity, data))
        why = self.ev(type, entity, data, src="core")
        self.test.assertIsNone(why, (type, entity, data))

    def request(self, command, until=120):
        why = self.ev("run.requested", "maps", {"command": command, "cwd": self.dir, "until_at": time.time() + until,
                                                "channel": "session", "owner": False, "sender": "maps"})
        self.test.assertIsNone(why)
        return f"run{self.seq}"

    def state(self, rid):
        return self.st["hub_runs"][rid]["state"]

    def until(self, rid, states, seconds=40, runner=None):
        end = time.time() + seconds
        while time.time() < end:
            (runner or self.runner).tick(self.st)
            if self.state(rid) in states:
                return
            time.sleep(0.1)
        self.test.fail(f"{rid} stayed {self.state(rid)}: {self.facts}")

    def folder(self, rid):
        return runjobs.folder_of(self.dir, rid)

    def told(self):
        return [str(m["text"]) for m in self.st["messages"].values() if "hub run run" in str(m["text"])]


@unittest.skipUnless(BASH, "the run's shell is bash")
class Daemon(unittest.TestCase):
    def test_a_run_starts_apart_and_its_outcome_comes_once(self):
        s = Stand(self)
        rid = s.request("echo out-$((6*7)); exit 4")
        s.until(rid, ("running", "ended"))
        s.until(rid, ("ended",))
        self.assertEqual([f[0] for f in s.facts].count("core.run_ended"), 1)
        told = s.told()
        self.assertEqual(len(told), 1)
        self.assertIn("exit 4", told[0])
        self.assertIn("out-42", told[0])
        for _ in range(3):
            s.runner.tick(s.st)
        self.assertEqual([f[0] for f in s.facts].count("core.run_ended"), 1)

    def test_the_command_runs_without_the_sessions_tree_mark_and_as_the_session(self):
        s = Stand(self, env={**env_without_service(), "HUB_TREE": "maps@/x"})
        rid = s.request('echo "tree=${HUB_TREE:-none} session=$HUB_SESSION run=$HUB_RUN"')
        s.until(rid, ("ended",))
        self.assertIn(f"tree=none session=maps run={rid}", s.told()[0])

    def test_a_run_not_taken_in_time_is_cancelled_and_never_runs(self):
        s = Stand(self, launch_apart=lambda argv, run: ("session", None, None))   # the OS "started" nothing
        rid = s.request("touch ran")
        runjobs.START_WAIT, keep = 1, runjobs.START_WAIT
        try:
            s.until(rid, ("ended",), seconds=20)
        finally:
            runjobs.START_WAIT = keep
        d = s.folder(rid)
        self.assertEqual(s.st["hub_runs"][rid]["reason"], "not_started")
        self.assertTrue(os.path.exists(os.path.join(d, runwrap.CANCELLED)))
        # the folder stays for days: the command and its environment are not kept, how it was started is
        cancelled = runwrap.read_json(os.path.join(d, runwrap.CANCELLED))
        self.assertEqual((cancelled.keys() & {"argv", "env"}, cancelled["how"]), (set(), "session"))
        self.assertEqual(s.runner.launched(d, s.st["hub_runs"][rid]), {"how": "session"})
        # the late wrapper wakes up: no spec to take, the command does not run
        subprocess.run([sys.executable, "-I", os.path.join(d, "wrap.py"), d], timeout=30)
        self.assertFalse(os.path.exists(os.path.join(s.dir, "ran")))

    def test_a_new_life_of_the_core_picks_a_started_run_up(self):
        s = Stand(self)
        rid = s.request("sleep 3; echo picked")
        s.until(rid, ("running",))
        calls = []
        again = s.new_runner(launch_apart=lambda argv, run: calls.append(argv) or ("session", None, None))
        s.until(rid, ("ended",), runner=again)
        self.assertEqual(calls, [])
        self.assertIn("picked", s.told()[0])

    def test_a_queued_run_whose_wrapper_took_it_is_picked_up_too(self):
        s = Stand(self)
        rid = s.request("sleep 2; echo late")
        d = s.folder(rid)
        s.runner.tick(s.st)   # starts the launch thread
        end = time.time() + 20
        while not os.path.exists(os.path.join(d, runwrap.PIDS)) and time.time() < end:
            time.sleep(0.05)
        self.assertEqual(s.state(rid), "queued")   # the old life died before recording the start
        again = s.new_runner(launch_apart=lambda argv, run: self.fail("started twice"))
        s.until(rid, ("ended",), runner=again)
        self.assertEqual([f[0] for f in s.facts].count("core.run_started"), 1)

    def test_a_stop_asks_the_wrapper(self):
        s = Stand(self)
        rid = s.request("sleep 60")
        s.until(rid, ("running",))
        self.assertIsNone(s.ev("run.stop_requested", rid, {"channel": "session", "owner": False, "sender": "maps"}))
        s.until(rid, ("ended",))
        self.assertIn("stopped by maps", s.told()[0])

    @unittest.skipUnless(POSIX, "SIGSTOP freezes the wrapper")
    def test_a_wrapper_that_does_not_answer_is_stopped_hard(self):
        s = Stand(self)
        rid = s.request("sleep 60")
        s.until(rid, ("running",))
        pids = runwrap.read_json(os.path.join(s.folder(rid), runwrap.PIDS))
        os.kill(pids["pid"], signal.SIGSTOP)
        self.addCleanup(lambda: _kill(pids["pid"]))
        runjobs.HARD_AFTER, keep = 0.5, runjobs.HARD_AFTER
        try:
            s.ev("run.stop_requested", rid, {"channel": "cli", "owner": True})
            s.until(rid, ("ended",), seconds=60)
        finally:
            runjobs.HARD_AFTER = keep
        run = s.st["hub_runs"][rid]
        self.assertEqual((run["reason"], run.get("leftover")), ("stopped", "none"))
        self.assertFalse(platforms.current.group_alive(pids["pgid"]))

    @unittest.skipUnless(POSIX, "a group of its own")
    def test_a_lost_wrapper_s_group_is_stopped_before_the_fact(self):
        s = Stand(self)
        rid = s.request("sleep 60")
        s.until(rid, ("running",))
        pids = runwrap.read_json(os.path.join(s.folder(rid), runwrap.PIDS))
        self.assertIsNotNone(s.st["hub_runs"][rid].get("born"))
        os.kill(pids["pid"], signal.SIGKILL)   # "End task" on the wrapper alone: the command lives on
        s.until(rid, ("ended",))
        run = s.st["hub_runs"][rid]
        self.assertEqual((run["reason"], run.get("leftover")), ("lost", "none"))
        self.assertFalse(platforms.current.group_alive(pids["pgid"]))
        self.assertIn("lost without a result", s.told()[0])

    @unittest.skipUnless(POSIX, "a group of its own")
    def test_a_group_whose_leader_is_someone_else_is_left_alone(self):
        s = Stand(self)
        other = subprocess.Popen(["sleep", "30"], start_new_session=True)   # a group that took the number meanwhile
        self.addCleanup(lambda: (other.kill(), other.wait()))
        run = {"pgid": other.pid, "born": -1, "how": "session", "state": "running"}   # the run's leader started at -1
        self.assertEqual(s.runner._stop_group(run), "unknown")
        self.assertIsNone(other.poll())   # not signalled

    def test_a_failed_os_call_is_not_started(self):
        s = Stand(self, launch_apart=lambda argv, run: ("unit", "hub-run-x", "systemd-run: Failed to connect to bus"))
        rid = s.request("touch ran")
        s.until(rid, ("ended",))
        run = s.st["hub_runs"][rid]
        self.assertEqual((run["reason"], run["error"]), ("not_started", "systemd-run: Failed to connect to bus"))
        self.assertIn("not started: systemd-run: Failed to connect to bus", s.told()[0])

    def test_no_bash_is_not_started(self):
        s = Stand(self, env={**env_without_service(), "PATH": tempfile.mkdtemp()})
        rid = s.request("echo x")
        s.until(rid, ("ended",))
        self.assertIn("bash not found", s.st["hub_runs"][rid]["error"])


@unittest.skipUnless(sys.platform.startswith("linux"), "the systemd branch")
class Systemd(unittest.TestCase):
    def test_a_unit_of_its_own_in_the_runs_slice(self):
        calls = []

        class R:
            returncode, stdout, stderr = 0, "", ""

        def run(argv, **kw):
            calls.append(argv)
            return R()
        d = tempfile.mkdtemp(prefix="hubrun-state-")
        self.addCleanup(platforms.current.rmtree, d)
        runner = runjobs.Runner(d, lambda *a: None, run=run, env={"INVOCATION_ID": "x", "PATH": os.environ["PATH"]})
        if runner.platform.run_apart_how(runner.env) != "unit":
            self.skipTest("no systemd-run here")
        how, unit, error = runner._launch_apart(["py", "wrap.py", "f"], {"id": "run9", "until_at": time.time() + 3600})
        mark = runjobs.space(d)
        self.assertEqual((how, unit, error), ("unit", f"hub-run-{mark}-run9", None))
        self.assertEqual(calls[0], ["systemctl", "--user", "set-property", "--runtime", f"hub-runs-{mark}.slice",
                                    "MemoryMax=50%"])
        argv = calls[1]
        for part in (f"--unit=hub-run-{mark}-run9", f"--slice=hub-runs-{mark}.slice", "OOMPolicy=continue",
                     "TimeoutStopSec=15"):
            self.assertIn(part, argv)
        self.assertTrue(any(a.startswith("RuntimeMaxSec=36") for a in argv), argv)
        self.assertEqual(argv[-3:], ["py", "wrap.py", "f"])
        R.returncode, R.stderr = 1, "Failed to connect to bus"
        self.assertEqual(runner._launch_apart(["py"], {"id": "run10", "until_at": time.time() + 60})[2],
                         "systemd-run: Failed to connect to bus")


class Sweep(unittest.TestCase):
    def test_old_and_too_big_ended_folders_go_live_ones_stay(self):
        d = tempfile.mkdtemp(prefix="hubrun-state-")
        self.addCleanup(platforms.current.rmtree, d)
        now = time.time()
        root = os.path.join(d, runjobs.FOLDER)

        def make(rid, size=10):
            os.makedirs(os.path.join(root, rid))
            with open(os.path.join(root, rid, runwrap.LOG), "wb") as f:
                f.write(b"x" * size)
        for rid in ("run1", "run2", "run3", "run4", "orphan"):
            make(rid, 1000)
        held = runwrap.hold(os.path.join(root, "run4"))   # a wrapper still runs it
        self.addCleanup(held.close)
        old = now - 8 * 86400
        os.utime(os.path.join(root, "orphan"), (old, old))
        st = {"hub_runs": {"run1": {"state": "ended", "ended_at": old}, "run2": {"state": "ended", "ended_at": now - 10},
                           "run3": {"state": "ended", "ended_at": now - 5}, "run4": {"state": "ended",
                                                                                    "ended_at": old}}}
        runjobs.ENDED_BYTES, keep = 1500, runjobs.ENDED_BYTES
        try:
            runjobs.Runner(d, lambda *a: None).sweep(st, now)
        finally:
            runjobs.ENDED_BYTES = keep
        self.assertEqual(sorted(os.listdir(root)), ["run3", "run4"])


def _kill(pid):
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(BASH, "the run's shell is bash")
class Uninstall(unittest.TestCase):
    def test_hub_uninstall_stops_the_live_runs(self):
        s = Stand(self)
        rid = s.request("sleep 60")
        s.until(rid, ("running",))
        d = s.folder(rid)
        self.assertEqual(runjobs.stop_all(s.dir, wait=20), 0)   # it answered the stop request in time
        self.assertFalse(runwrap.alive(d))
        self.assertEqual(runwrap.read_json(os.path.join(d, runwrap.RESULT))["by"], "uninstall")

    @unittest.skipUnless(POSIX, "SIGSTOP freezes the wrapper")
    def test_a_frozen_wrapper_s_group_goes_too(self):
        s = Stand(self)
        rid = s.request("sleep 60")
        s.until(rid, ("running",))
        d = s.folder(rid)
        pids = runwrap.read_json(os.path.join(d, runwrap.PIDS))
        os.kill(pids["pid"], signal.SIGSTOP)
        self.addCleanup(lambda: _kill(pids["pid"]))
        self.assertEqual(runjobs.stop_all(s.dir, wait=1), 1)
        self.assertFalse(runwrap.alive(d))
        end = time.time() + 10
        while platforms.current.group_alive(pids["pgid"]) and time.time() < end:
            time.sleep(0.1)
        self.assertFalse(platforms.current.group_alive(pids["pgid"]))


@unittest.skipUnless(BASH, "the run's shell is bash")
class ReviewEdges(unittest.TestCase):
    def test_a_missing_folder_is_not_started_never_another_folder(self):
        s = Stand(self)
        rid = s.request("touch ran")
        s.st["hub_runs"][rid]["cwd"] = os.path.join(s.dir, "gone")   # the worktree was removed meanwhile
        s.until(rid, ("ended",))
        self.assertIn("is not there", s.st["hub_runs"][rid]["error"])
        self.assertFalse(os.path.exists(os.path.join(os.path.expanduser("~"), "ran")))

    def test_an_os_call_that_raises_is_cancelled_by_the_rename(self):
        def boom(argv, run):
            raise subprocess.TimeoutExpired(["systemd-run"], 60)
        s = Stand(self, launch_apart=boom)
        rid = s.request("touch ran")
        s.until(rid, ("ended",))
        d = s.folder(rid)
        self.assertEqual(s.st["hub_runs"][rid]["reason"], "not_started")
        self.assertTrue(os.path.exists(os.path.join(d, runwrap.CANCELLED)))
        subprocess.run([sys.executable, "-I", os.path.join(d, "wrap.py"), d], timeout=30)   # a late wrapper: nothing
        self.assertFalse(os.path.exists(os.path.join(s.dir, "ran")))

    def test_a_start_picked_up_by_a_new_life_knows_how_it_runs(self):
        s = Stand(self)
        rid = s.request("sleep 2")
        d = s.folder(rid)
        s.runner.tick(s.st)
        end = time.time() + 20
        while not os.path.exists(os.path.join(d, runwrap.PIDS)) and time.time() < end:
            time.sleep(0.05)
        again = s.new_runner(launch_apart=lambda argv, run: self.fail("started twice"))
        s.until(rid, ("running", "ended"), runner=again)
        started = [f for f in s.facts if f[0] == "core.run_started"]
        self.assertEqual(started[0][2]["how"], "session")
        s.until(rid, ("ended",), runner=again)


class LogRotationFails(unittest.TestCase):
    def test_the_pipe_is_still_read(self):
        d = tempfile.mkdtemp(prefix="hubrun-log-")
        os.makedirs(os.path.join(d, runwrap.OLD_LOG, "x"))   # out.1.log is a folder: the move fails
        log = runwrap.Log(d, part=20, free_min=0)
        for i in range(10):
            log.write(f"line {i}\n".encode())   # no exception
        log.close()
        self.assertIn("line 9", open(os.path.join(d, runwrap.LOG)).read())

    def test_a_write_after_close_is_dropped(self):
        d = tempfile.mkdtemp(prefix="hubrun-log-")
        log = runwrap.Log(d, free_min=0)
        log.close()
        log.write(b"late output of a detached descendant\n")   # no exception
