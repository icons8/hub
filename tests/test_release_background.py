"""The session process leaves outside the core's tick (2026-10-28: `hub session stop` froze hubd for 15-20 s — release
closed stdin and waited for claude right in the tick; if claude had taken the next turn, nothing was written to the
journal until it exited or was killed). release closes stdin and returns; the wait for the exit (and the kill after
RELEASE_WAIT) is in the host's own thread, and host.done release is written by the fact of the exit. Until then the
process is leaving: a spawn of the same session (or the same conversation) and its end_tree wait for the exit, as
before."""
import tests  # noqa: F401 — do not see the machine's settings (~/.config/hub-core), see tests/__init__.py
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from core import journal, spool
from core.daemon import Hub
from hosts import streamjson
from hosts.streamjson import StreamHost, StreamProcess

FAKE = os.path.join(os.path.dirname(__file__), "fakes", "fake_claude_stream.py")
OWNER = {"channel": "web", "owner": True}


class Pipe:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class LingeringPopen:
    """A claude that does not exit at once when stdin closes: it exits when exited is set (or on kill). Every wait is
    recorded with the thread it came from: the core's tick must not wait for it."""
    def __init__(self, pid=4242):
        self.pid, self.stdin, self.stdout = pid, Pipe(), Pipe()
        self.exited, self.killed, self.waits = threading.Event(), False, []

    def poll(self):
        return 0 if self.exited.is_set() else None

    def wait(self, timeout=None):
        self.waits.append((threading.current_thread() is threading.main_thread(), timeout))
        if threading.current_thread() is threading.main_thread():
            if not self.exited.is_set():   # the tick waits: the old code — do not hang the test, say it timed out
                raise subprocess.TimeoutExpired("claude", timeout or 0)
            return 0
        if not self.exited.wait(timeout):
            raise subprocess.TimeoutExpired("claude", timeout)
        return 0

    def kill(self):
        self.killed = True
        self.exited.set()

    def terminate(self):
        self.exited.set()


def until(pred, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


class HostReleaseTest(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hub-release-")
        self.addCleanup(shutil.rmtree, self.state, True)
        self.host = StreamHost(self.state, claude=(sys.executable, FAKE))
        self.popen = LingeringPopen()
        self.host.procs["a"] = StreamProcess(self.popen)
        self.host.procs["a"].session_id = "sid-a"
        self.facts = []

    def released(self):
        self.facts += [f for f in self.host.poll() if f[0] == "host.done"]
        return [f for f in self.facts if f[2].get("do") == "release"]

    def tick_waits(self):
        return [w for w in self.popen.waits if w[0]]

    def test_release_does_not_wait_in_the_tick(self):
        result = self.host.release("a")
        self.assertEqual(self.tick_waits(), [])            # the tick did not wait for the process
        self.assertEqual(result, streamjson.RELEASING)
        self.assertTrue(self.popen.stdin.closed)
        self.assertIsNone(self.popen.poll())               # it is still running its turn,
        self.assertFalse(self.host.alive("a"))             # but the core no longer talks to it, as after the old release
        self.assertEqual(self.released(), [])              # no fact until it exits: it is leaving
        self.popen.exited.set()
        self.assertTrue(until(lambda: self.released()))
        (fact,) = self.released()
        self.assertEqual((fact[1], fact[2]["result"]), ("a", "released"))
        self.assertFalse(self.host.alive("a"))
        self.assertTrue(self.popen.stdout.closed)
        self.assertFalse(self.host.leaving("a"))

    def test_release_now_terminates_at_once_and_still_waits_outside_the_tick(self):
        # a stop: no waiting for the step, and no reading of what is still in stdin
        with mock.patch.object(streamjson.platforms.current, "terminate_session", lambda p, tree: p.terminate()):
            self.assertEqual(self.host.release("a", now=True), streamjson.RELEASING)
        self.assertEqual(self.tick_waits(), [])
        self.assertTrue(until(lambda: self.released()))
        self.assertEqual(self.released()[0][2]["result"], "stopped")
        self.assertFalse(self.popen.killed)

    def test_a_stop_of_a_leaving_process_terminates_it_too(self):
        self.host.release("a")   # an ordinary release (park, tools): waits up to RELEASE_WAIT
        terms = []
        with mock.patch.object(streamjson.platforms.current, "terminate_session", lambda p, tree: terms.append(p.pid)):
            with mock.patch.object(streamjson, "STOP_WAIT", 0.2):
                self.assertEqual(self.host.release("a", now=True), streamjson.RELEASING)   # still one wait, one fact
            self.assertEqual(terms, [4242])
            # it ignored the SIGTERM: killed after STOP_WAIT, not after the 20 s of the release it was leaving by
            self.assertTrue(until(lambda: self.released(), timeout=3))
        self.assertTrue(self.popen.killed)
        self.assertEqual(self.released()[0][2]["result"], "killed")
        time.sleep(0.2)
        self.assertEqual(len(self.released()), 1)

    def test_a_stop_that_sigterm_does_not_end_is_killed_after_stop_wait(self):
        with mock.patch.object(streamjson.platforms.current, "terminate_session", lambda p, tree: None), \
                mock.patch.object(streamjson, "STOP_WAIT", 0.2):
            self.host.release("a", now=True)
            self.assertTrue(until(lambda: self.released(), timeout=3))
        self.assertTrue(self.popen.killed)
        self.assertEqual(self.released()[0][2]["result"], "killed")

    def test_a_stop_of_a_process_already_gone_does_not_say_stopped(self):
        self.popen.exited.set()   # it exited by itself (a crash) before the stop was carried out
        terms = []
        with mock.patch.object(streamjson.platforms.current, "terminate_session", lambda p, tree: terms.append(p.pid)):
            self.host.release("a", now=True)
        self.assertTrue(until(lambda: self.released()))
        self.assertEqual((terms, self.released()[0][2]["result"]), ([], "released"))

    def test_stuck_process_is_killed_in_the_background(self):
        with mock.patch.object(streamjson, "RELEASE_WAIT", 0.1):
            self.host.release("a")
            self.assertTrue(until(lambda: self.released()))
        self.assertTrue(self.popen.killed)
        self.assertEqual(self.released()[0][2]["result"], "killed")
        self.assertEqual(self.tick_waits(), [])

    def test_leaving_is_known_by_name_and_by_conversation(self):
        self.assertFalse(self.host.leaving("a", "sid-a"))   # not released: an ordinary live process
        self.host.release("a")
        self.assertTrue(self.host.leaving("a"))
        self.assertTrue(self.host.leaving("b", "sid-a"))    # a rename: the new name resumes the same conversation
        self.assertFalse(self.host.leaving("b", "sid-b"))
        self.popen.exited.set()
        self.assertTrue(until(lambda: self.host.procs["a"].left))
        self.assertTrue(self.host.leaving("a"))             # exited, but its fact is not in the journal yet: a spawn would
        self.assertTrue(self.released())                    # go before host.done release
        self.assertFalse(self.host.leaving("a"))
        self.assertFalse(self.host.leaving("b", "sid-a"))

    def test_repeated_release_is_one_wait(self):
        self.host.release("a")
        self.assertEqual(self.host.release("a"), streamjson.RELEASING)
        self.popen.exited.set()
        self.assertTrue(until(lambda: self.host.procs["a"].left))
        self.assertEqual(self.host.release("a"), streamjson.RELEASING)   # exited, its fact not taken: hubd writes no second
        self.assertTrue(until(lambda: self.released()))
        time.sleep(0.2)
        self.assertEqual(len(self.released()), 1)
        self.assertEqual(self.host.release("a"), "released")   # its fact is taken: nothing to wait for, as before

    def test_end_tree_waits_for_the_exit(self):
        # end_tree after release (a stopped executor): its background commands — after claude has exited, as before;
        # the tree mark is on claude too, so an end_tree now would kill it in the middle of its exit
        ended = []
        host = self.host

        def end_tree(self, name, **_):
            ended.append((name, host.leaving(name)))   # the session's new claude carries the same mark: no spawn yet
            return "ended 1"
        with mock.patch.object(streamjson.Trees, "end_tree", end_tree):
            self.host.release("a")
            self.assertEqual(self.host.end_tree("a"), streamjson.AFTER_RELEASE)
            self.assertEqual(ended, [])
            self.popen.exited.set()
            self.assertTrue(until(lambda: self.released()))
            self.assertEqual(ended, [("a", True)])
            self.assertEqual(self.released()[0][2].get("tree"), "ended 1")
            self.assertEqual(self.host.end_tree("a"), "ended 1")   # no process leaving — at once
        self.assertEqual(ended, [("a", True), ("a", False)])

    def test_shutdown_waits_for_the_leaving(self):
        # a core restart in the middle of a stop: the process is stopped with the others, and its end_tree is not lost
        ended = []
        with mock.patch.object(streamjson.Trees, "end_tree", lambda self, name, **_: ended.append(name) or "ended 0"), \
                mock.patch.object(streamjson.platforms.current, "terminate_session", lambda p, tree: p.terminate()):
            self.host.release("a")
            self.host.end_tree("a")
            self.host.shutdown(wait=2)
        self.assertEqual(ended, ["a"])
        self.assertTrue(self.host.procs["a"].left)


class HubdReleaseTest(unittest.TestCase):
    """hubd with the stream-json host and a fake claude that lingers after stdin is closed (FAKE_LINGER_FILE)."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hubs-release-")
        self.linger = os.path.join(self.state, "let-go")
        patch = mock.patch.object(streamjson, "STOP_WAIT", 30)
        patch.start()
        self.addCleanup(patch.stop)
        # the stop does not reach the lingering claude on any OS: on Windows it is TerminateJobObject, which ignoring
        # SIGTERM (FAKE_LINGER_TERM) does not survive; it leaves by the linger file or is killed after STOP_WAIT
        self.term = mock.patch.object(streamjson.platforms.current, "terminate_session", lambda popen, tree: None)
        self.term.start()
        self.addCleanup(self.term.stop)
        self.hub = self.new_hub()
        self.hub.start()

    def tearDown(self):
        open(self.linger, "w").close()
        for p in self.hub.host.procs.values():   # it ignores SIGTERM (FAKE_LINGER_TERM): kill, not 10 s of kill_server
            if p.popen.poll() is None:
                p.popen.kill()
        self.hub.host.kill_server()
        time.sleep(0.2)
        shutil.rmtree(self.state, ignore_errors=True)

    def new_hub(self):
        # FAKE_LINGER_TERM: a stop terminates the process at once (release now); this claude ignores SIGTERM and lingers
        # until the file, so the tests see a stop whose process is still leaving (STOP_WAIT is long here, setUp)
        host = StreamHost(self.state, claude=(sys.executable, FAKE),
                          env={"FAKE_TURN_S": "0.2", "FAKE_LINGER_FILE": self.linger, "FAKE_LINGER_TERM": "1"})
        return Hub(self.state, host, registry_dir=os.path.join(self.state, "registry"))

    def cli(self, type, entity=None, **data):
        spool.put(os.path.join(self.state, "spool"), type, entity, data)

    def until(self, pred, timeout=15.0):
        end = time.time() + timeout
        while time.time() < end:
            self.hub.tick()
            if pred():
                return
            time.sleep(0.03)
        self.fail(f"did not wait; {self.hub.state['sessions']} {self.hub.state['messages']}")

    def sess(self, name):
        return self.hub.state["sessions"].get(name, {})

    def events(self, type, entity=None, **match):
        return [e for e in journal.read(self.hub.journal.path)
                if e["type"] == type and (entity is None or e["entity"] == entity)
                and all(e["data"].get(k) == v for k, v in match.items())]

    def send(self, to, text):
        before = set(self.hub.state["messages"])
        self.cli("message.received", to=to, text=text)
        self.until(lambda: set(self.hub.state["messages"]) - before)
        (mid,) = set(self.hub.state["messages"]) - before
        return mid

    def delivered(self, mid):
        return self.hub.state["messages"][mid]["state"] == "delivered"

    def new_session(self, name, executor=False):
        self.cli("session.requested", name, cwd=self.state, session_id=f"sid-{name}")
        self.until(lambda: self.sess(name).get("state") in ("idle", "parked"))
        mid = self.send(name, "hi")
        self.until(lambda: self.delivered(mid) and self.sess(name)["state"] == "idle")
        if executor:   # `hub session stop` is for executors (reducer), and a stopped one is gone for good (end_tree)
            self.cli("task.adopted", name, **OWNER)
            self.until(lambda: self.sess(name).get("role") == "executor")

    def stop(self, name):
        self.cli("session.stop_requested", name, **OWNER)
        self.until(lambda: self.sess(name)["state"] == "retired")

    def let_go(self, name, result="stopped"):
        """The lingering process may exit; result — how its release went: stopped (a stop: release now) or released."""
        open(self.linger, "w").close()
        self.until(lambda: self.events("host.done", name, do="release"))
        self.assertEqual(self.events("host.done", name, do="release")[-1]["data"]["result"], result)

    def test_stop_does_not_freeze_the_core(self):
        self.new_session("a", executor=True)
        self.new_session("b")
        self.stop("a")
        running = lambda: self.hub.host.procs["a"].popen.poll() is None   # noqa: E731
        self.assertTrue(running())                         # the old code waited for it right in the tick
        self.assertEqual(self.events("host.done", "a", do="release"), [])
        mid = self.send("b", "while a is exiting")             # the core goes on: another session gets its message
        self.until(lambda: self.delivered(mid))
        self.assertTrue(running())
        self.let_go("a")
        self.assertFalse(self.hub.host.alive("a"))
        self.assertEqual(self.sess("a")["state"], "retired")

    def test_stop_ends_the_process_at_once_without_waiting_for_its_turn(self):
        # an ordinary claude dies on SIGTERM: a stop (release now) does not wait for the end of its step or for stdin
        # to be read (2026-10-27: after an interrupt it took a new turn on "STOP" inserted mid-step)
        self.hub.host.env.pop("FAKE_LINGER_TERM")
        self.term.stop()   # the real stop
        self.new_session("a", executor=True)
        started = time.time()
        self.stop("a")
        self.until(lambda: self.events("host.done", "a", do="release"), timeout=5)
        fact = self.events("host.done", "a", do="release")[-1]["data"]
        self.assertEqual(fact["result"], "stopped")
        self.assertLess(time.time() - started, 3)
        self.assertFalse(os.path.exists(self.linger))   # it did not wait for the linger file: SIGTERM ended it
        self.assertIsNotNone(self.hub.host.procs["a"].popen.poll())

    def test_repeated_stop_is_refused_and_released_once(self):
        self.new_session("a", executor=True)
        self.stop("a")
        self.cli("session.stop_requested", "a", **OWNER)
        self.until(lambda: any(e["data"].get("type") == "session.stop_requested" for e in self.events("core.rejected")))
        self.let_go("a")
        for _ in range(10):
            self.hub.tick()
            time.sleep(0.03)
        self.assertEqual(len(self.events("host.done", "a", do="release")), 1)

    def test_wake_while_leaving_spawns_after_the_exit(self):
        self.new_session("a", executor=True)
        self.stop("a")
        self.cli("session.wake_requested", "a", **OWNER)
        self.until(lambda: self.sess("a")["state"] == "starting")
        spawns = len(self.events("host.done", "a", do="spawn"))
        for _ in range(15):   # the old process has not exited: the same conversation is not raised a second time
            self.hub.tick()
            time.sleep(0.03)
        self.assertEqual(len(self.events("host.done", "a", do="spawn")), spawns)
        self.let_go("a")
        self.until(lambda: self.sess("a")["state"] == "idle")
        done = [e for e in self.events("host.done", "a") if e["data"]["do"] in ("release", "spawn")]
        self.assertEqual([(e["data"]["do"], e["data"]["result"]) for e in done][-2:],
                         [("release", "stopped"), ("spawn", "spawned")])
        self.assertEqual(self.events("session.exited", "a"), [])

    def test_message_while_leaving_does_not_raise_it(self):
        self.new_session("a", executor=True)
        self.stop("a")
        before = len(self.events("host.done", "a", do="spawn"))
        self.cli("message.received", to="a", text="while exiting")
        for _ in range(15):
            self.hub.tick()
            time.sleep(0.03)
        self.assertEqual(len(self.events("host.done", "a", do="spawn")), before)
        self.let_go("a")
        self.assertEqual(self.sess("a")["state"], "retired")

    def test_reload_spawns_after_the_exit(self):
        # release + spawn of the same session (a tools change, reload): the new process only after the old one exits
        self.new_session("a", executor=True)
        self.cli("session.reload_requested", "a", **OWNER)
        self.until(lambda: self.sess("a")["state"] == "starting")
        for _ in range(15):
            self.hub.tick()
            time.sleep(0.03)
        self.assertIsNone(self.hub.host.procs["a"].popen.poll())
        self.assertEqual(self.events("host.done", "a", do="release"), [])
        self.let_go("a", "released")
        self.until(lambda: self.sess("a")["state"] == "idle")
        done = [(e["data"]["do"], e["data"]["result"]) for e in self.events("host.done", "a")
                if e["data"]["do"] in ("release", "spawn")]
        self.assertEqual(done[-2:], [("release", "released"), ("spawn", "spawned")])

    def test_a_late_exit_is_said_once(self):
        # the host's thread does not finish (a pipe that does not close): after RELEASE_LATE — one host.error, and the
        # spawn keeps waiting (a second claude on the conversation would be worse); it goes once the exit is known
        open(self.linger, "w").close()
        self.new_session("a")
        stuck = [True]
        real = self.hub.host.leaving
        self.hub.host.leaving = lambda name, session_id=None: stuck[0] and name == "a" or real(name, session_id)
        with mock.patch("core.daemon.RELEASE_LATE", 0.3):
            self.cli("session.reload_requested", "a", **OWNER)
            self.until(lambda: self.events("host.error", "a", do="release"))
            for _ in range(20):
                self.hub.tick()
                time.sleep(0.03)
            self.assertEqual(len(self.events("host.error", "a", do="release")), 1)
            self.assertEqual(self.sess("a")["state"], "starting")
            stuck[0] = False
            self.until(lambda: self.sess("a")["state"] == "idle")
        self.assertEqual(self.events("host.done", "a", do="spawn")[-1]["data"]["result"], "spawned")

    def test_core_restart_in_the_middle_of_a_stop(self):
        self.new_session("a", executor=True)
        self.stop("a")
        spawned = self.events("host.done", "a", do="spawn")[-1]["seq"]
        self.hub.host.shutdown(wait=3)                     # hubd restarts: the leaving process goes with the others
        self.assertFalse(self.hub.host.alive("a"))
        self.hub = self.new_hub()
        self.hub.start()
        for _ in range(15):
            self.hub.tick()
            time.sleep(0.03)
        self.assertEqual(self.sess("a")["state"], "retired")
        self.assertFalse(self.hub.host.alive("a"))
        self.assertEqual(self.events("host.done", "a", do="spawn")[-1]["seq"], spawned)


if __name__ == "__main__":
    unittest.main()
