"""Background processes of a session that is gone (2026-10-16). Claude Code runs each Bash command in its own OS session
(setsid): a dev server, a test database, a waiter outlive claude and live on as orphans until killed by hand; on the hub
server such processes held gigabytes of memory (a sleeping executor's dev server, 1.7 GB). On Windows the session tree is
killed by the job (tests/test_stop.py), on POSIX by a mark in the environment (platforms.TREE_ENV: session name and hub
folder), which everything the session started inherits. Marked processes are killed only when the session leaves for
good or hands its work in: the executor fell asleep, the session was deleted or closed, the hub was stopped by `hub stop`.
Parking and a hub restart do not touch them: after coming back the session continues with them."""
import tests  # noqa: F401 — hide the machine's settings
import os
import subprocess
import sys
import tempfile
import time
import unittest

import platforms
from core import executors, reducer
from platforms import posix
from core import restart
from core.daemon import stop_sessions
from hosts.streamjson import StreamHost
from tests.fakes.procs import alive, kill

POSIX = unittest.skipIf(os.name == "nt", "POSIX: on Windows the session tree is held by a job")
TAG = platforms.TREE_ENV

# "claude": runs a command in its own OS session (like Claude Code's Bash) and writes its pid; lives while stdin is
# open.
# With --stubborn the command does not hear SIGTERM
CLAUDE = """
import os, subprocess, sys
if "--help" in sys.argv:   # StreamHost.fresh_prompt asks --help
    sys.exit()
code = "import signal, time\\n" + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n" if "--stubborn" in sys.argv else "") \\
    + "time.sleep(120)"
p = subprocess.Popen([sys.executable, "-c", code], start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
open(sys.argv[1], "w").write(str(p.pid))
sys.stdin.buffer.read()
"""
SLEEPER = [sys.executable, "-c", "import time; time.sleep(120)"]


def until(pred, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def text_of(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


@POSIX
class Host(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="leftovers-")
        self.script = os.path.join(self.dir, "claude.py")
        with open(self.script, "w") as f:
            f.write(CLAUDE)

    def host(self, *extra):
        host = StreamHost(self.dir, claude=[sys.executable, self.script, os.path.join(self.dir, "pid"), *extra])
        self.addCleanup(host.shutdown, 2.0)
        return host

    def session(self, host, name, *extra):
        """Bring a session up; -> the pid of its command, which would outlive claude."""
        pid_file = os.path.join(self.dir, "pid")
        if os.path.exists(pid_file):
            os.unlink(pid_file)
        host.claude = [sys.executable, self.script, pid_file, *extra]
        host.spawn(name, f"00000000-0000-0000-0000-00000000000{len(host.procs)}", cwd=self.dir)
        self.assertTrue(until(lambda: text_of(pid_file)), "the session command did not start")
        pid = int(text_of(pid_file))
        self.addCleanup(kill, pid, time.time())
        return pid

    def tagged(self, tag, detached=False):
        """A process with the session mark that it did not start now (a previous hub life) or that the core
        started separately (Chrome)."""
        env = {**os.environ, TAG: tag}
        if detached:
            p = platforms.current.popen_detached(SLEEPER, env=env, stdin=subprocess.DEVNULL)
        else:
            p = subprocess.Popen(SLEEPER, env=env, start_new_session=True)
        self.addCleanup(lambda: (kill(p.pid), p.wait()))
        return p.pid

    def test_release_keeps_the_commands_and_end_tree_takes_them(self):
        # parking and restart are release: the session comes back and continues with its dev server; a sleeping executor
        # is
        # also end_tree (2026-10-16: the nuxt dev of a sleeping executor held 1.7 GB)
        host = self.host()
        child = self.session(host, "a")
        host.release("a")
        time.sleep(0.5)
        self.assertTrue(alive(child))
        host.end_tree("a")
        self.assertTrue(until(lambda: not alive(child)))

    def test_end_tree_takes_only_its_own_session_of_its_own_hub(self):
        host = self.host()
        a, b = self.session(host, "a"), self.session(host, "b")
        other_hub = self.tagged(f"a@{tempfile.mkdtemp(prefix='other-hub-')}")   # the same name in another hub (tests, bench)
        host.release("a")
        host.end_tree("a")
        self.assertTrue(until(lambda: not alive(a)))
        self.assertTrue(alive(b))
        self.assertTrue(alive(other_hub))

    def test_commands_from_before_a_restart_are_still_its_own(self):
        # the mark is the hub's name and folder, not the startup number: a dev server started before parking or a
        # restart is also hers
        host = self.host()
        before = self.tagged(host.tree_tag("a"))
        host.end_tree("a")
        self.assertTrue(until(lambda: not alive(before)))

    def test_what_the_core_starts_apart_is_not_in_the_tree(self):
        # the agents' Chrome is also started by the session's MCP launcher (popen_detached): it is shared, the session
        # does not take it away
        host = self.host()
        chrome = self.tagged(host.tree_tag("a"), detached=True)
        host.end_tree("a")
        time.sleep(0.5)
        self.assertTrue(alive(chrome))

    def test_a_command_deaf_to_sigterm_is_killed(self):
        host = self.host()
        child = self.session(host, "a", "--stubborn")
        host.release("a")
        started = time.time()
        host.end_tree("a")
        self.assertTrue(until(lambda: not alive(child)))
        self.assertLess(time.time() - started, 10)

    def test_a_restart_keeps_the_commands_hub_stop_takes_them(self):
        host = self.host()
        a = self.session(host, "a")
        asleep = self.tagged(host.tree_tag("parked"))     # parked: there is no claude process, its command is alive
        other_hub = self.tagged(f"a@{tempfile.mkdtemp(prefix='other-hub-')}")
        host.shutdown(2.0)
        time.sleep(0.5)
        self.assertTrue(alive(a) and alive(asleep))
        host.shutdown(2.0, final=True)
        self.assertTrue(until(lambda: not alive(a) and not alive(asleep)))
        self.assertTrue(alive(other_hub))

    def test_the_caller_and_its_parents_are_never_taken(self):
        # hub-up or hubd started from a session would carry its mark: cleanup does not kill itself and those above it
        tag = f"x@{self.dir}"
        out = os.path.join(self.dir, "out")
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import platforms; "
                f"n = platforms.current.end_tagged(lambda v: v == {tag!r}); open(sys.argv[2], 'w').write(str(n))")
        p = subprocess.run([sys.executable, "-c", code, os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            out], env={**os.environ, TAG: tag}, timeout=30)
        self.assertEqual((p.returncode, text_of(out)), (0, "0"))


@POSIX
class MacListing(unittest.TestCase):
    """macOS: there is no process environment in /proc: `ps -E` prints it after the command line."""
    def test_tags_are_read_from_ps_e(self):
        plain = ("  101 node dev.js\n"
                 "  102 grep HUB_TREE=a@/Users/x/.hub-core\n"
                 "  103 /bin/sleep 5\n"
                 "  104 bash\n"
                 "  106 sh -c run\n")
        with_env = ("  101 node dev.js PATH=/usr/bin HUB_TREE=a@/Users/x/.hub-core TERM=xterm\n"
                    "  102 grep HUB_TREE=a@/Users/x/.hub-core HOME=/Users/x\n"   # text in the arguments is not a mark
                    "  103 /bin/sleep 5 HUB_TREE=a@/Users/x/my hub\n"
                    "  104 bash\n"
                    "  105 python3 new.py HUB_TREE=a@/Users/x/.hub-core\n"      # born between ps calls
                    "  106 sh -c runner HUB_TREE=a@/Users/x/.hub-core\n")      # changed by exec: argv is longer
        self.assertEqual(posix._ps_tags(plain, with_env), {101: "a@/Users/x/.hub-core", 103: "a@/Users/x/my hub"})


OWNER = {"channel": "web", "owner": True}


class Core(unittest.TestCase):
    """When the core kills a session's processes (the end_tree action) and when it does not."""
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("session.requested", "hub", cwd="/h", session_id="s-h", kind="hub", **OWNER)
        self.ev("session.requested", "maps", cwd="/m", session_id="s-m", role="agent", about="Maps", **OWNER)
        self.ev("session.started", "maps")

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, self.acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 1000 + self.seq, "type": type,
                                                           "entity": entity, "data": data})
        return why

    def ends(self):
        return [a["session"] for a in self.acts if a["do"] == "end_tree"]

    def executor(self, done=True):
        """Like the daemon: a specialist brought up an executor, it came up and took a task (core/executors.events_for); done
        hands in the turn result."""
        self.assertIsNone(self.ev("task.run_requested", "maps-ci", task="fix the export", channel="session",
                                  owner=False, sender="maps"))
        for type, entity, data in executors.events_for(self.st, "maps-ci", self.st["runs"]["maps-ci"], cwd="/m/w",
                                                       worktree=None):
            self.assertIsNone(self.ev(type, entity, **data), (type, data))
        self.ev("session.started", "maps-ci")
        self.assertEqual(self.st["sessions"]["maps-ci"]["role"], "executor")
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == "maps-ci"]
        self.ev("turn.started", "maps-ci", marker=d["message"], prompt_head="task")
        if done:
            self.ev("turn.result", "maps-ci", text="Done, branch task/maps-ci")
            self.ev("turn.stopped", "maps-ci")

    def test_an_executor_that_fell_asleep_takes_its_commands_after_release(self):
        self.executor()
        self.assertIsNone(self.ev("session.close_requested", "maps-ci", retire=True, channel="core", owner=False))
        self.assertEqual(self.st["sessions"]["maps-ci"]["state"], "retired")
        self.assertEqual([a["do"] for a in self.acts if a.get("session") == "maps-ci"][-2:], ["release", "end_tree"])

    def test_a_stopped_executor_takes_them_too(self):
        self.executor(done=False)
        self.assertIsNone(self.ev("session.stop_requested", "maps-ci", **OWNER))
        self.assertEqual(self.st["sessions"]["maps-ci"]["state"], "retired")
        self.assertEqual(self.ends(), ["maps-ci"])

    def test_a_parked_session_keeps_them(self):
        # a free one with no job comes back on the first message and continues with its bench
        self.assertIsNone(self.ev("session.parked", "maps", channel="core"))
        self.assertEqual(self.st["sessions"]["maps"]["state"], "parked")
        self.assertEqual(self.ends(), [])

    def test_a_specialist_falling_asleep_keeps_them(self):
        # a specialist sleeps until the next job: a bench for the owner or a waiter may be there on purpose
        self.assertIsNone(self.ev("session.ended", "maps", reason="prompt_input_exit"))
        self.assertEqual(self.st["sessions"]["maps"]["state"], "asleep")
        self.assertEqual(self.ends(), [])

    def test_a_removed_session_takes_them(self):
        self.assertIsNone(self.ev("session.parked", "maps", channel="core"))
        self.assertIsNone(self.ev("session.remove_requested", "maps", **OWNER))
        self.assertEqual(self.st["sessions"]["maps"]["state"], "removed")
        self.assertEqual(self.ends(), ["maps"])


class Stop(unittest.TestCase):
    """hubd stops: `hub stop` is for good (the stop file from hub-up), otherwise a restart."""
    class FakeHost:
        def __init__(self):
            self.calls = []

        def shutdown(self, final=False):
            self.calls.append(final)

    def test_hub_stop_takes_the_trees_a_restart_does_not(self):
        d = tempfile.mkdtemp(prefix="leftovers-stop-")
        host = self.FakeHost()
        stop_sessions(host, d)
        open(os.path.join(d, restart.STOP), "w").close()
        stop_sessions(host, d)
        self.assertEqual(host.calls, [False, True])


if __name__ == "__main__":
    unittest.main()
