"""Background commands and agents of sessions (owner decision 30.09: show processes in the UI as Claude Code does,
otherwise it is unclear what a session is doing; a live case: a session is free but waits for a production job). The
host sees a background launch and writes the session.background fact; the core keeps the list on the session (it
survives snapshots and page restarts) and removes a task when Claude Code wakes the session with its
task-notification or the session process exits."""
import tests  # noqa: F401 — keep this machine's settings out
import tempfile
import unittest

from core import reducer, views
from hosts.streamjson import StreamHost

BASH = {"command": "until kubectl get job …; do sleep 10; done", "description": "Wait for generate job to finish",
        "run_in_background": True}


def ev(seq, type, entity=None, data=None, ts=None):
    return {"seq": seq, "ts": ts or seq, "src": "test", "type": type, "entity": entity, "data": data or {}}


class HostSeesBackground(unittest.TestCase):
    def test_launch_replies_become_facts(self):
        host = StreamHost(tempfile.mkdtemp(prefix="bg-"))
        use = lambda i, n, inp: {"type": "tool_use", "id": i, "name": n, "input": inp}  # noqa: E731
        res = lambda i, t: {"type": "tool_result", "tool_use_id": i, "content": t}  # noqa: E731
        for msg in (
            {"type": "assistant", "message": {"content": [use("t1", "Bash", BASH), use("t2", "Bash", {"command": "ls"}),
                                                          use("t3", "Agent", {"description": "Map billing",
                                                                              "run_in_background": True})]}},
            {"type": "user", "message": {"content": [
                res("t1", "Command running in background with ID: brtv1. Output is being written to: /tmp/x"),
                res("t2", "a.txt\nCommand running in background with ID: fake"),  # just command output
                res("t3", [{"type": "text", "text": "Async agent launched successfully.\nagentId: ag77 (internal)"}])]}},
        ):
            host.inbox.put(("a", msg))
        facts = [f for f in host.poll() if f[0] == "session.background"]
        self.assertEqual(facts, [
            ("session.background", "a", {"id": "brtv1", "kind": "bash", "title": "Wait for generate job to finish",
                                          "output": "/tmp/x"}),
            ("session.background", "a", {"id": "ag77", "kind": "agent", "title": "Map billing"})])


class HelpersModel(unittest.TestCase):
    """18.11: a hub's three helper agents ran for half an hour and the owner took the work for stopped: the session's row
    says "3 helpers · <model>". The model is what the start of the call named (Claude Code's Agent `model`, a Codex
    subagent's); Claude Code's whole list of the background does not name it, so the core keeps it."""

    def test_the_agent_call_names_the_model_and_the_core_keeps_it_through_the_list(self):
        host = StreamHost(tempfile.mkdtemp(prefix="bg-"))
        host.inbox.put(("a", {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Agent", "input": {"description": "Review security", "model": "opus",
                                                                          "run_in_background": True}},
            {"type": "tool_use", "id": "t2", "name": "Agent", "input": {"description": "Review ops", "run_in_background": True}},
            {"type": "tool_use", "id": "t3", "name": "Bash", "input": {"command": "sleep 9", "model": "x",
                                                                         "run_in_background": True}}]}}))
        host.inbox.put(("a", {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "Async agent launched successfully.\nagentId: ag1"},
            {"type": "tool_result", "tool_use_id": "t2", "content": "Async agent launched successfully.\nagentId: ag2"},
            {"type": "tool_result", "tool_use_id": "t3", "content": "Command running in background with ID: b3"}]}}))
        facts = [f[2] for f in host.poll() if f[0] == "session.background"]
        self.assertEqual([(f["id"], f.get("model")) for f in facts], [("ag1", "opus"), ("ag2", None), ("b3", None)])
        base = [ev(1, "session.requested", "a", {"session_id": "s", "cwd": "/w"}),
                ev(2, "session.started", "a", {"session_id": "s"}),
                ev(3, "turn.started", "a", {"marker": None, "prompt_head": "x"}),
                *[ev(4 + i, "session.background", "a", f) for i, f in enumerate(facts)],
                # Claude Code's whole list names no model; a Codex list names its own; a non-string one is not taken
                ev(7, "session.background", "a", {"tasks": [
                    {"id": "ag1", "kind": "agent", "title": "Review security"},
                    {"id": "ag2", "kind": "agent", "title": "Review ops", "model": {"x": 1}},
                    {"id": "ag3", "kind": "agent", "title": "Probe", "model": "gpt-x"},
                    {"id": "b3", "kind": "bash", "title": "sleep 9", "model": "gpt-x"}]})]
        st = reducer.empty()
        for e in base:
            st, _, why = reducer.reduce(st, e)
            self.assertIsNone(why, e)
        bg = st["sessions"]["a"]["background"]
        self.assertEqual({k: v.get("model") for k, v in bg.items()}, {"ag1": "opus", "ag2": None, "ag3": "gpt-x", "b3": None})
        r = views.Reader()   # `hub show` names it too
        r.state = st
        shown = views.show(r, "a")
        self.assertIn("  in the background, agent on opus: Review security", shown)
        self.assertIn("  in the background, agent: Review ops", shown)


class HostSeesCompletionMidTurn(unittest.TestCase):
    """Live case 30.09 14:48: a background command finished during a turn. Claude Code delivered the
    task-notification within that same turn, there was no new turn, and the task stayed "in background" forever:
    the session could not be put to sleep, the hub could not be restarted."""

    def test_notification_inside_a_turn_becomes_a_done_fact(self):
        host = StreamHost(tempfile.mkdtemp(prefix="bg-"))
        note = "<task-notification>\n<task-id>bj1bde32c</task-id>\n<status>completed</status>\n</task-notification>"
        host.inbox.put(("a", {"type": "user", "message": {"content": note}}))
        host.inbox.put(("a", {"type": "user", "message": {"content": [{"type": "text", "text": note.replace(
            "bj1bde32c", "ag77")}]}}))
        host.inbox.put(("a", {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t9", "content": "cat: <task-id>zzz</task-id>"}]}}))  # command output
        self.assertEqual([f for f in host.poll() if f[0] == "session.background"], [
            ("session.background", "a", {"id": "bj1bde32c", "done": True}),
            ("session.background", "a", {"id": "ag77", "done": True})])


class HostTakesTheTaskListFromClaudeCode(unittest.TestCase):
    """30.09 17:28: tests that went to the background on timeout finished mid-turn, yet the core kept showing them
    "in background" for another half hour (what was that background command?). In the stream such a
    completion arrives not as a message but as a background_tasks_changed system event carrying the whole list of
    background tasks, and that list is the truth."""

    def test_list_replaces_what_the_core_holds(self):
        host = StreamHost(tempfile.mkdtemp(prefix="bg-"))
        host.inbox.put(("a", {"type": "system", "subtype": "background_tasks_changed", "tasks": [
            {"task_id": "b1", "task_type": "local_bash", "description": "Run the full test suite"},
            {"task_id": "ag7", "task_type": "local_agent", "description": "Map the code"}]}))
        host.inbox.put(("a", {"type": "system", "subtype": "background_tasks_changed", "tasks": []}))
        self.assertEqual([f for f in host.poll() if f[0] == "session.background"], [
            ("session.background", "a", {"tasks": [{"id": "b1", "kind": "bash", "title": "Run the full test suite"},
                                                   {"id": "ag7", "kind": "agent", "title": "Map the code"}]}),
            ("session.background", "a", {"tasks": []})])


class HostSeesAForkAsABackgroundAgent(unittest.TestCase):
    """Claude Code 2.1.302 enabled forks in `claude -p` (Agent with subagent_type "fork"): a fork always goes to the
    background, even without run_in_background, and inherits the parent's whole conversation. The stream is like a
    background agent's (captured from 2.1.302 on a fake API, 17.10): the background list, the fork's text with the id
    of its step, an empty list and a second turn on the notification. A shape guard: the host needed no changes for
    forks, and the test pins the captured stream."""

    def test_fork_stream_is_the_background_agent_stream(self):
        host = StreamHost(tempfile.mkdtemp(prefix="bg-"))
        tid, use = "a39c2dbe46fdf6286", "toolu_01X"
        for msg in (
            {"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
                {"type": "tool_use", "id": use, "name": "Agent",
                 "input": {"description": "probe", "prompt": "do it", "subagent_type": "fork"}}]}},
            {"type": "system", "subtype": "background_tasks_changed", "tasks": [
                {"task_id": tid, "run_id": "r1", "task_type": "local_agent", "subagent_type": "fork",
                 "description": "probe"}]},
            {"type": "system", "subtype": "task_started", "task_id": tid, "tool_use_id": use, "description": "probe",
             "subagent_type": "fork", "is_backgrounded": True, "task_type": "local_agent", "prompt": "do it"},
            {"type": "user", "parent_tool_use_id": None, "message": {"content": [
                {"type": "tool_result", "tool_use_id": use, "content": [
                    {"type": "text", "text": "Async agent launched successfully. (This tool result is internal "
                                             "metadata.)\nagentId: " + tid}]}]}},
            {"type": "assistant", "parent_tool_use_id": use, "message": {"content": [
                {"type": "text", "text": "fork report"}]}},
            {"type": "system", "subtype": "task_updated", "task_id": tid, "patch": {"status": "completed"}},
            {"type": "system", "subtype": "task_notification", "task_id": tid, "tool_use_id": use,
             "status": "completed"},
            {"type": "system", "subtype": "background_tasks_changed", "tasks": []},
            {"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
                {"type": "text", "text": "started"}]}},
            {"type": "result", "subtype": "success", "result": "started", "num_turns": 2, "duration_ms": 151},
            {"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
                {"type": "text", "text": "the fork is done"}]}},
            {"type": "result", "subtype": "success", "result": "the fork is done", "num_turns": 1, "duration_ms": 19},
        ):
            host.inbox.put(("a", msg))
        facts = host.poll()
        self.assertEqual([f[2] for f in facts if f[0] == "session.background"], [
            {"tasks": [{"id": tid, "kind": "agent", "title": "probe"}]}, {"tasks": []}])
        self.assertEqual([f[2] for f in facts if f[0] == "session.said"], [
            {"text": "fork report", "parent": use}, {"text": "started"}, {"text": "the fork is done"}])
        self.assertEqual([f[2]["text"] for f in facts if f[0] == "turn.result"], ["started", "the fork is done"])


class CoreKeepsBackground(unittest.TestCase):
    def run_events(self, events):
        st = reducer.empty()
        for e in events:
            st, _, why = reducer.reduce(st, e)
            self.assertIsNone(why, e)
        return st

    def test_until_notification_or_exit(self):
        base = [ev(1, "session.requested", "a", {"session_id": "s", "cwd": "/w"}),
                ev(2, "session.started", "a", {"session_id": "s"}),
                ev(3, "turn.started", "a", {"marker": None, "prompt_head": "x"}),
                ev(4, "session.background", "a", {"id": "brtv1", "kind": "bash", "title": "Wait"}, ts=100),
                ev(5, "session.background", "a", {"id": "ag77", "kind": "agent", "title": "Map"}, ts=101),
                ev(6, "turn.stopped", "a")]
        st = self.run_events(base)
        self.assertEqual(sorted(st["sessions"]["a"]["background"]), ["ag77", "brtv1"])
        self.assertEqual(st["sessions"]["a"]["background"]["brtv1"]["since"], 100)
        r = views.Reader()
        r.state = st
        self.assertEqual([b["id"] for b in r.snapshot()["sessions"][0]["background"]], ["brtv1", "ag77"])
        st = self.run_events(base + [ev(7, "turn.started", "a", {"marker": None, "prompt_head":
                                                                 "<task-notification>\n<task-id>brtv1</task-id>"})])
        self.assertEqual(list(st["sessions"]["a"]["background"]), ["ag77"])
        st = self.run_events(base + [ev(7, "session.background", "a", {"id": "ag77", "done": True})])  # mid-turn
        self.assertEqual(list(st["sessions"]["a"]["background"]), ["brtv1"])
        # the whole list from Claude Code: whatever is missing is removed, whatever remains keeps its launch time
        st = self.run_events(base + [ev(7, "session.background", "a", {"tasks": [
            {"id": "brtv1", "kind": "bash", "title": "Wait"}, {"id": "n2", "kind": "bash", "title": "New"}]}, ts=200)])
        bg = st["sessions"]["a"]["background"]
        self.assertEqual((sorted(bg), bg["brtv1"]["since"], bg["n2"]["since"]), (["brtv1", "n2"], 100, 200))
        st = self.run_events(base + [ev(7, "session.background", "a", {"tasks": []})])
        self.assertEqual(st["sessions"]["a"]["background"], {})
        st = self.run_events(base + [ev(7, "session.exited", "a", {"why": "reconcile"})])
        self.assertEqual(st["sessions"]["a"]["background"], {})


class RespawnCutsBackground(unittest.TestCase):
    """Live case 06.10: a session was granted a tool and the core brought it up again (release + spawn) with the same
    conversation. The process left together with three background agents and a command, but without session.exited
    the list stayed: five "In background" rows hung for over an hour, the watchdog kept waking the owner, and the
    session waited for agents that no longer existed."""

    def test_a_respawn_drops_the_old_process_background_and_tells_the_session(self):
        owner = {"channel": "cli", "owner": True}
        st = reducer.empty()
        acts = []
        for e in (ev(1, "session.requested", "a", {"session_id": "s", "cwd": "/w", **owner}),
                  ev(2, "session.started", "a", {"session_id": "s"}),
                  ev(3, "turn.started", "a", {"marker": None, "prompt_head": "x"}),
                  ev(4, "session.background", "a", {"id": "ag1", "kind": "agent", "title": "Maps ads research"}),
                  ev(5, "session.background", "a", {"id": "b2", "kind": "bash", "title": "until done"}),
                  ev(6, "turn.result", "a", {"text": "Waiting for the research."}),
                  ev(7, "session.effort_set", "a", {"level": "high", **owner})):
            st, acts, why = reducer.reduce(st, e)
            self.assertIsNone(why, e)
        self.assertEqual([a["do"] for a in acts if a["do"] in ("release", "spawn")], ["release", "spawn"])
        self.assertEqual(st["sessions"]["a"]["background"], {})
        st, _, _ = reducer.reduce(st, ev(8, "session.started", "a", {"session_id": "s", "source": "resume"}))
        told = [m["text"] for m in st["messages"].values() if m.get("to") == "a" or m.get("session") == "a"]
        self.assertTrue(any("Maps ads research" in t and "until done" in t for t in told), told)
        self.assertTrue(any("The hub restarted you" in t and "A hub restart" not in t for t in told), told)   # not "of the hub"

if __name__ == "__main__":
    unittest.main()
