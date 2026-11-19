"""Agent forks are gone: since 2026-10-04 a task gets an executor (`hub task run`), the remnants of the mechanism were
removed on 2026-10-17. Hubs of earlier versions may have sessions with the fork and fork_of roles in their state and
fork events in the journal: the fold accepts them without consequences, an old fork is an ordinary session. The
retirement of finished ones is an executor rule (core/executors.py)."""
import tests  # noqa: F401 - ignore the machine's settings
import copy
import os
import subprocess
import sys
import tempfile
import time
import unittest

from core import executors, reducer, spool
from hosts.streamjson import StreamHost

OWNER = {"channel": "cli", "owner": True}
HUB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")


class Base(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.actions = reducer.empty(), 0, []
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.requested", "devops", cwd="/k8s", session_id="s-dev", role="agent", about="DevOps",
                tools=["browser:mac"], mode="auto", **OWNER)
        self.ev("session.started", "hub")
        self.ev("session.started", "devops")
        self.ev("turn.started", "devops", marker=None, prompt_head="a long task")

    def ev(self, type, entity=None, ts=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts or 100 + self.seq, "type": type,
                                                      "entity": entity, "data": data})
        self.actions += acts
        return why

    def mine(self, name):
        return sorted(m["text"] for m in self.st["messages"].values() if m["session"] == name)


class OldForks(Base):
    def old_fork(self, name="devops-2"):
        """A fork as an earlier hub version left it: the fork role, fork_of, the agent's conversation for the first start."""
        self.ev("session.requested", name, cwd="/k8s/.claude/worktrees/" + name, session_id="s-" + name, **OWNER)
        s = self.st["sessions"][name]
        s.update(role="fork", fork_of="devops", fork_from="s-dev", profile="s-dev", about="DevOps",
                 worktree={"path": s["cwd"], "branch": "fork/" + name, "repo": "/k8s"})
        return s

    def test_old_fork_events_fold_without_consequences(self):
        self.old_fork()
        self.ev("message.received", None, ts=200, to="devops", text="task", channel="session", owner=False,
                sender="hub")
        before = copy.deepcopy(self.st)
        for kind, data in (("session.fork_requested", {"channel": "cli", "owner": True}),
                           ("session.fork_failed", {"reason": "folder is taken"}),
                           ("session.queue_moved", {"to": "devops-2"})):
            n = len(self.actions)
            self.assertIsNone(self.ev(kind, "devops", **data), kind)
            self.assertEqual(self.actions[n:], [], kind)
        self.st.pop("seq", None), before.pop("seq", None)
        self.assertEqual(self.st, before)                       # no messages to the hub, no queue transfer

    def test_an_old_fork_retires_no_more(self):
        f = self.old_fork()
        self.ev("session.started", "devops-2")
        self.assertEqual(executors.retiring(self.st, f["since"] + 10 ** 6), [])          # idle - not put to sleep
        n = len(self.actions)
        self.ev("session.close_requested", "devops-2", retire=True)
        self.assertNotIn("sleep", [a["do"] for a in self.actions[n:]])           # leaving without handing over work is not for it
        self.assertEqual(self.st["sessions"]["devops-2"]["closing"], "handoff")  # an ordinary sleep
        self.ev("session.ended", "devops-2")
        self.assertEqual(self.st["sessions"]["devops-2"]["state"], "asleep")
        self.assertEqual(executors.retiring(self.st, 10 ** 7), [])                       # asleep - not removed

    def test_an_old_fork_starts_its_own_conversation(self):
        self.old_fork()
        n = len(self.actions)
        self.ev("session.exited", "devops-2", why="crash")                     # restarted - without a copy of the conversation
        (spawn,) = [a for a in self.actions[n:] if a.get("session") == "devops-2" and a["do"] == "spawn"]
        self.assertNotIn("fork_from", spawn)
        self.assertNotIn("--fork-session", StreamHost(tempfile.mkdtemp(prefix="old-fork-"), claude=("echo",))
                         .command("devops-2", "s-devops-2"))

    def removed(self):
        self.ev("session.ended", "devops-2")
        self.assertIsNone(self.ev("session.remove_requested", "devops-2", **OWNER))

    def test_messages_to_an_old_fork_stay_with_it(self):
        self.old_fork()
        self.ev("message.received", None, to="devops-2", text="another question", **OWNER)
        self.removed()
        self.assertEqual(self.mine("devops"), [])                               # nothing moved to the agent
        (m,) = [m for m in self.st["messages"].values() if m["text"] == "another question"]
        self.assertEqual((m["session"], m["state"]), ("devops-2", "failed"))
        self.ev("message.received", None, to="devops-2", text="and now?", **OWNER)
        self.assertNotIn("and now?", self.mine("devops"))                      # a removed one is not the agent's address

    def test_a_revived_old_fork_is_a_worker(self):
        f = self.old_fork()
        self.removed()
        self.assertIsNone(self.ev("session.requested", "devops-2", session_id=f["session_id"], cwd=f["cwd"],
                                  revive=True, worktree=f["worktree"], **OWNER))
        s = self.st["sessions"]["devops-2"]
        self.assertEqual((s["role"], s["worktree"]["path"]), ("worker", f["cwd"]))

    def test_a_removed_old_fork_cleans_its_worktree(self):
        self.old_fork()
        self.removed()
        self.assertEqual([s["name"] for s in executors.worktrees_to_clean(self.st)], ["devops-2"])


class Retiring(Base):
    def sleeping_executor(self, name="fix-1", backlog=False):
        self.ev("session.requested", name, session_id=f"s-{name}", cwd="/w", executor=True, raised_by="hub", **OWNER)
        self.ev("session.started", name)
        for m in self.st["messages"].values():
            if m["session"] == name:
                m["state"] = "delivered"
        self.ev("turn.started", name, marker=None, prompt_head="task")
        self.ev("turn.result", name, text="done")
        if backlog:
            self.st["sessions"][name]["backlog"] = True
        self.ev("session.close_requested", name, retire=True)
        self.ev("session.ended", name)   # the process is gone - asleep
        s = self.st["sessions"][name]
        self.assertEqual(s["state"], "asleep" if backlog else "retired")
        return s

    def test_a_handed_in_executor_is_removed_after_a_day_unless_switched_off(self):
        # PR #87: an enabled period (hub.json executors.remove_after_hours) removes a finished one; bring it back with hub session revive
        s = self.sleeping_executor()
        day = 24 * 3600
        self.assertEqual(executors.retiring(self.st, s["since"] + day - 1, keep=day), [])                  # too early
        self.assertEqual(executors.retiring(self.st, s["since"] + day, keep=day), [("remove", "fix-1")])
        self.assertEqual(executors.retiring(self.st, s["since"] + 10 * day), [])                   # off by default
        self.assertEqual(executors.retiring(self.st, s["since"] + 3 * 3600, keep=2 * 3600), [("remove", "fix-1")])
        self.assertIsNone(self.ev("session.remove_requested", "fix-1", retired=True))
        self.assertEqual(self.st["sessions"]["fix-1"]["state"], "removed")

    def test_the_cleanup_leaves_agents_directions_and_executors_with_open_work(self):
        self.sleeping_executor("dir-1", backlog=True)                                            # direction
        self.ev("session.close_requested", "devops", retire=True)                               # an agent (a specialist)
        self.assertEqual(executors.retiring(self.st, 10 ** 8, keep=3600), [])

    def test_turn_on_a_task_marked_waiting_still_binds(self):
        # a turn with the assignment's marker is the assignment's turn, even if it was marked "waiting for you"
        self.ev("delegation.created", None, session="devops", context="task", channel="session", owner=False,
                sender="hub")
        self.ev("permission.requested", "devops", request_id="r1", tool="AskUserQuestion", interactive=True,
                input={"questions": [{"question": "Q?", "options": [{"label": "yes"}]}]})
        (d,) = self.st["delegations"].values()
        self.ev("session.exited", "devops")                       # restarted
        self.ev("session.started", "devops")
        self.assertEqual(self.st["delegations"][d["id"]]["state"], "working")   # a question on another matter did not mark it
        self.ev("turn.started", "devops", marker=d["message"], prompt_head="task")
        self.ev("turn.result", "devops", text="task done")
        self.assertEqual((self.st["delegations"][d["id"]]["state"], self.st["delegations"][d["id"]]["reply"]),
                         ("replied", "task done"))


class Host(unittest.TestCase):
    def test_spawn_with_a_removed_worktree_does_not_crash(self):
        # PR #7: revive after retirement crashed - FileNotFoundError on a removed working copy
        state = tempfile.mkdtemp(prefix="wt-host-")
        marker = os.path.join(state, "cwd.txt")
        host = StreamHost(state, claude=(sys.executable, "-c", f"open({marker!r}, 'w').write(__import__('os').getcwd())"))
        host.fresh_prompt = False   # the `claude --help` probe would write its own folder
        repo = os.path.join(state, "repo")
        os.makedirs(repo)  # the working copy under it was already removed by worktree.remove - the repo itself is unaffected
        missing = os.path.join(repo, ".claude", "worktrees", "maps-2")
        self.assertEqual(host.spawn("maps-2", "s-b2", cwd=missing), "spawned")
        try:
            for _ in range(50):
                if os.path.exists(marker):
                    break
                time.sleep(0.05)
            with open(marker) as f:
                self.assertEqual(os.path.realpath(f.read()), os.path.realpath(repo))  # did not crash - it ran in the repo of the copy
        finally:
            host.kill_server()


class Cli(unittest.TestCase):
    def test_manual_fork_is_gone(self):
        state = tempfile.mkdtemp(prefix="old-fork-")
        env = {**os.environ, "HUB_STATE_DIR": state, "HUB_CLI_WAIT": "0"}
        env.pop("HUB_SESSION", None)
        # eval 2026-10-17: the old command printed the whole hub help without a word about what to do instead of a fork
        for args in (["devops"], [], ["devops", "check robots.txt"], ["--help"], ["devops", "-h"]):
            r = subprocess.run([sys.executable, HUB, "session", "fork", *args], env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 1, args)
            self.assertIn("hub task run", r.stderr, args)
            self.assertLess(len(r.stderr.splitlines()), 3, args)
            self.assertEqual(r.stdout, "", args)
        self.assertEqual([ev for _, ev in spool.take(os.path.join(state, "spool"))], [])


if __name__ == "__main__":
    unittest.main()
