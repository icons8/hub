"""The core does not cut a session's working background itself (11.11, journal 06.11-11.11).

A Monitor tells the session of every event with a <task-notification> that carries its task id but no <status>; only
the notification of its end has one. The core took every such event for the end: the task left the session's
background, and an executor that was waiting for its monitored runs fell asleep 120 s later, its process tree killed
(07.11: 44 eval processes; six executors in five days). A task ends only by a notification block with its own status:
the hook lists those ids (notice.ended), the host reads the blocks the same way. A turn recorded by an older hook (no
notice.ended) folds as before.

Also: a quiet background command (a wait loop, a silent run) is a service a restart does not wait for only past Claude
Code's own longest background term (2 h), and a relaunch for new tools, a mode or the browser waits for the working
background, as a plugin update and an account switch do."""
import tests  # noqa: F401 — keep this machine's settings out
import tempfile
import unittest

from core import executors, forms, reducer, restart
from hooks.hub_hook import translate
from hosts.streamjson import StreamHost

OWNER = {"channel": "cli", "owner": True}
OUT = "/home/x/.cache/claude/tasks/"


def monitor_event(tid, line="[lane-1] green"):
    # the shape Claude Code 2.1.309 writes (transcripts): no tool-use-id, no output file, no status
    return (f"<task-notification>\n<task-id>{tid}</task-id>\n<summary>Monitor event: \"results by lane\"</summary>\n"
            f"<event>{line}</event>\n</task-notification>")


def ended(tid, status="completed", what="Background command \"full run\""):
    return (f"<task-notification>\n<task-id>{tid}</task-id>\n<tool-use-id>toolu_01{tid}</tool-use-id>\n"
            f"<output-file>{OUT}{'x' * 160}/{tid}.output</output-file>\n<status>{status}</status>\n"
            f"<summary>{what} {status}</summary>\n</task-notification>")


def ev(seq, type, entity=None, data=None, ts=None):
    return {"seq": seq, "ts": ts or seq, "src": "test", "type": type, "entity": entity, "data": data or {}}


class EndedBlocks(unittest.TestCase):
    def test_only_blocks_with_their_own_status_end_a_task(self):
        self.assertEqual(forms.ended_tasks(monitor_event("bm1")), [])
        self.assertEqual(forms.ended_tasks(ended("bc2")), ["bc2"])
        for text in (monitor_event("bm1") + "\n" + ended("bc2"), ended("bc2") + monitor_event("bm1")):
            self.assertEqual(forms.ended_tasks(text), ["bc2"], text)   # a status in the next block is not the monitor's
        self.assertEqual(forms.ended_tasks(ended("bk3", "killed") + ended("bs4", "stopped")), ["bk3", "bs4"])
        self.assertEqual(forms.ended_tasks("cat: <task-id>zz</task-id> <status>completed</status>"), [])   # not a block
        # Claude Code's own last event of a monitor has no status either, and it is the end (126 in the transcripts)
        for last in ("[Monitor expired after 30m with no events delivered. Re-arm it if you still need the watch.]",
                     "[Monitor stopped — too much output (40 events suppressed over 12s)]",
                     "[Monitor timed out — re-arm if needed.]"):
            self.assertEqual(forms.ended_tasks(monitor_event("bm1", last)), ["bm1"], last)
        self.assertEqual(forms.ended_tasks(monitor_event("bm1", "[Monitor-1] green")), [])
        # the tasks a previous process left: one block, several ids, one status
        orphans = ("<task-notification>\n<task-id>bx1</task-id>\n<task-id>bx2</task-id>\n<task-id>__orphan_summary__:shell"
                   "</task-id>\n<status>stopped</status>\n<summary>2 background shell command tasks didn't finish</summary>\n"
                   "</task-notification>")
        self.assertEqual(forms.ended_tasks(orphans), ["bx1", "bx2"])
        # what the watched command prints is not Claude Code's word: a log line with a status tag ends nothing (review)
        self.assertEqual(forms.ended_tasks(monitor_event("bm1", "GET /health <status>ok</status> 200")), [])
        self.assertEqual(forms.ended_tasks(monitor_event("bm1", "x <task-id>bz9</task-id> [Monitor expired")), [])
        # our label inside a block (a monitor tailing a session log) does not cut the notices short (second review)
        self.assertEqual(forms.ended_tasks(monitor_event("bm1", "[hub:m12 from maps] go") + "\n" + ended("bc2")), ["bc2"])
        # an agent's result quoting a notice is the agent's words
        agent = ended("ba5", what="Agent \"review\"").replace(
            "</summary>", "</summary>\n<result>saw <task-id>bm1</task-id> <status>completed</status></result>")
        self.assertEqual(forms.ended_tasks(agent), ["ba5"])

    def test_the_hook_lists_them_in_the_notice(self):
        _, data = translate({"hook_event_name": "UserPromptSubmit", "prompt": monitor_event("bm1")})
        self.assertEqual(data["notice"]["ended"], [])
        self.assertNotIn("status", data["notice"])
        _, data = translate({"hook_event_name": "UserPromptSubmit", "prompt": monitor_event("bm1") + ended("bc2")})
        self.assertEqual(data["notice"]["ended"], ["bc2"])
        _, plain = translate({"hook_event_name": "UserPromptSubmit", "prompt": "plain input"})
        self.assertNotIn("notice", plain)
        # a message glued after the notification quotes a finished block: the quote ends nothing (review)
        _, data = translate({"hook_event_name": "UserPromptSubmit",
                             "prompt": monitor_event("bm1") + "\n[hub:m7 from maps] the log said:\n" + ended("bc2")})
        self.assertEqual(data["notice"]["ended"], [])


class HostReadsBlocks(unittest.TestCase):
    def test_a_monitor_event_mid_turn_is_not_a_done_fact(self):
        host = StreamHost(tempfile.mkdtemp(prefix="bgk-"))
        host.inbox.put(("a", {"type": "user", "message": {"content": monitor_event("bm1")}}))
        host.inbox.put(("a", {"type": "user", "message": {"content": [
            {"type": "text", "text": monitor_event("bm1") + ended("bc2")}]}}))
        self.assertEqual([f for f in host.poll() if f[0] == "session.background"],
                         [("session.background", "a", {"id": "bc2", "done": True})])


class CoreKeepsMonitor(unittest.TestCase):
    def run_events(self, events):
        st = reducer.empty()
        for e in events:
            st, _, why = reducer.reduce(st, e)
            self.assertIsNone(why, e)
        return st

    BASE = [ev(1, "session.requested", "a", {"session_id": "s", "cwd": "/w", **OWNER}),
            ev(2, "session.started", "a", {"session_id": "s"}),
            ev(3, "turn.started", "a", {"marker": None, "prompt_head": "x"}),
            ev(4, "session.background", "a", {"tasks": [{"id": "bm1", "kind": "bash", "title": "results by lane"},
                                                         {"id": "bc2", "kind": "bash", "title": "full run"}]}),
            ev(5, "turn.stopped", "a")]

    def woken(self, prompt, seq=6, old_hook=False):
        _, data = translate({"hook_event_name": "UserPromptSubmit", "prompt": prompt})
        if old_hook:   # a turn recorded by a hook before notice.ended: the journal has no such field
            data["notice"].pop("ended")
        return self.run_events(self.BASE + [ev(seq, "turn.started", "a", data)])

    def test_a_monitor_event_keeps_the_task(self):
        st = self.woken(monitor_event("bm1"))
        self.assertEqual(sorted(st["sessions"]["a"]["background"]), ["bc2", "bm1"])

    def test_a_status_ends_only_its_task(self):
        st = self.woken(monitor_event("bm1") + ended("bc2"))
        self.assertEqual(list(st["sessions"]["a"]["background"]), ["bm1"])
        st = self.woken(ended("bm1", what="Monitor \"results by lane\""))
        self.assertEqual(list(st["sessions"]["a"]["background"]), ["bc2"])

    def test_a_turn_from_an_older_hook_folds_as_before(self):
        # the journal before notice.ended: every <task-id> in the first 200 characters ended its task, a monitor too
        st = self.woken(monitor_event("bm1"), old_hook=True)
        self.assertEqual(list(st["sessions"]["a"]["background"]), ["bc2"])
        st = self.run_events(self.BASE + [ev(6, "turn.started", "a", {"marker": None, "prompt_head":
                                                                     "<task-notification>\n<task-id>bc2</task-id>"})])
        self.assertEqual(list(st["sessions"]["a"]["background"]), ["bm1"])


class ExecutorWithMonitor(unittest.TestCase):
    """The replay of 07.11 11:05-11:07: an executor watches its eval runs with a Monitor, reports «waiting for the
    other runs» after each event; the core forgot the monitor at its first event and put the executor to sleep 120 s
    after the last report."""

    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.requested", "maps", cwd="/m", session_id="s-maps", role="agent", about="Maps", **OWNER)
        self.ev("session.started", "maps")
        self.ev("task.run_requested", "maps-eval", task="run the eval lanes", channel="session", owner=False,
                sender="maps")
        run = self.st["runs"]["maps-eval"]
        for type, entity, data in executors.events_for(self.st, "maps-eval", run, cwd="/m/.claude/worktrees/x",
                                                       worktree=None):
            self.assertIsNone(self.ev(type, entity, **data), (type, data))
        (self.d,) = [d for d in self.st["delegations"].values() if d["session"] == "maps-eval"]
        self.ev("session.started", "maps-eval")

    def ev(self, type, entity=None, ts=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts or 1000 + self.seq, "type": type,
                                                   "entity": entity, "data": data})
        return why

    def turn(self, prompt, text, ts):
        _, data = translate({"hook_event_name": "UserPromptSubmit", "prompt": prompt})
        self.ev("turn.started", "maps-eval", ts=ts, **data)
        self.ev("turn.result", "maps-eval", ts=ts + 2, text=text)
        self.ev("turn.stopped", "maps-eval", ts=ts + 2)

    def test_monitor_events_do_not_put_it_to_sleep(self):
        self.ev("turn.started", "maps-eval", marker=self.d["message"], prompt_head="task", ts=2000)
        self.ev("session.background", "maps-eval", ts=2001,
                tasks=[{"id": "bm1", "kind": "bash", "title": "results by lane"}])
        self.ev("turn.result", "maps-eval", text="Started the lanes, waiting for the runs", ts=2002)
        self.ev("turn.stopped", "maps-eval", ts=2002)
        for i in range(3):
            self.turn(monitor_event("bm1", f"[lane-{i}] green"), "Lane green, waiting for the other runs", 2100 + i * 60)
        last = 2100 + 2 * 60 + 2
        self.assertEqual(list(self.st["sessions"]["maps-eval"]["background"]), ["bm1"])
        self.assertEqual(executors.retiring(self.st, last + executors.RETIRE_AFTER), [])   # it was put to sleep here
        self.turn(ended("bm1", what="Monitor \"results by lane\""), "All lanes done: 3 of 3 green", 3000)
        self.assertEqual(executors.retiring(self.st, 3002 + executors.RETIRE_AFTER), [("sleep", "maps-eval")])


class RestartWaitsForQuietWork(unittest.TestCase):
    def test_a_silent_wait_under_two_hours_holds_and_a_longer_one_is_a_service(self):
        bg = {"id": "b1", "kind": "bash", "title": "until grep -q DONE log", "since": 0}
        self.assertFalse(restart.service(bg, 50 * 60))           # 11.11: it was a service from the 30th minute
        self.assertFalse(restart.service(bg, 2 * 3600 - 1))
        self.assertTrue(restart.service(bg, 2 * 3600 + 1))
        self.assertFalse(restart.service({**bg, "out_at": 2 * 3600 - 60}, 2 * 3600 + 1))   # it writes: work


class RelaunchWaitsForBackground(unittest.TestCase):
    """A new tool for a session whose command runs in the background: the relaunch (release, then --resume) waits for
    it, as a plugin update and an account switch do (restart.working); before, release cut it (claude does not exit
    while its background runs: killed after 20 s, the task orphaned, the session told to start it again)."""

    def setUp(self):
        self.st, self.seq, self.actions = reducer.empty(), 0, []
        self.ev("catalog.loaded", None, tools={"jira": {"kind": "mcp", "server": {}, "risk": [], "description": ""}})
        self.ev("session.requested", "a", cwd="/w", session_id="s-a", **OWNER)
        self.ev("session.started", "a")
        self.st["sessions"]["a"]["has_history"] = True
        self.ev("session.background", "a", id="b1", kind="bash", title="full run")
        self.actions = []

    def ev(self, type, entity=None, ts=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts or 1000 + self.seq, "type": type,
                                                      "entity": entity, "data": data})
        self.actions += acts
        return why

    def did(self):
        return [a["do"] for a in self.actions if a["do"] in ("release", "spawn")]

    def test_a_granted_tool_waits_for_the_run(self):
        self.assertIsNone(self.ev("tools.granted", "a", tool="jira", **OWNER))
        self.assertEqual(self.did(), [])
        self.assertEqual(list(self.st["sessions"]["a"]["background"]), ["b1"])
        self.ev("session.background", "a", tasks=[])
        self.assertEqual(self.did(), ["release", "spawn"])

    def test_an_explicit_reload_does_not_wait(self):
        self.assertIsNone(self.ev("session.reload_requested", "a", **OWNER))
        self.assertEqual(self.did(), ["release", "spawn"])

    def test_a_message_for_the_session_goes_with_the_new_tools(self):
        # review: the message would be typed into the process without the tool; it relaunches, as before
        self.ev("tools.granted", "a", tool="jira", **OWNER)
        self.assertEqual(self.did(), [])
        self.ev("message.received", None, to="a", text="file the ticket in jira", **OWNER)
        self.assertEqual(self.did(), ["release", "spawn"])
        # the run is cut, as on main: the core forgets it and tells the session after the launch
        self.assertEqual(self.st["sessions"]["a"]["background"], {})
        self.assertEqual(self.st["sessions"]["a"]["cut_turn"]["background"], ["full run"])

    def test_a_revoked_tool_goes_at_once(self):
        self.ev("tools.granted", "a", tool="jira", **OWNER)
        self.ev("session.background", "a", tasks=[])
        self.ev("session.started", "a")
        self.ev("session.background", "a", id="b2", kind="bash", title="full run")
        self.actions = []
        self.assertIsNone(self.ev("tools.revoked", "a", tool="jira", **OWNER))
        self.assertEqual(self.did(), ["release", "spawn"])

    def test_a_tool_the_session_asked_for_goes_at_once(self):
        self.assertIsNone(self.ev("tools.requested", "a", tool="jira", why="to file the ticket", channel="session",
                                  owner=False, sender="a"))
        self.assertIsNone(self.ev("tools.granted", "a", tool="jira", **OWNER))
        self.assertEqual(self.did(), ["release", "spawn"])


if __name__ == "__main__":
    unittest.main()
