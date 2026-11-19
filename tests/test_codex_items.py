"""Codex (experimental): a Codex session's steps, results, background work and subagents
(hosts/codex_items.py). The lines fed to the host are the shapes `codex app-server` 0.162.1 sent in probes with a fake
Responses API (multi_agent v1 and v2, a background command, an interrupted command, apply_patch)."""
import io
import json
import os
import types
import unittest
from unittest import mock

from hosts import codex


def bare_host():
    """A CodexHost without a process: lines go straight to _line, what it writes is in popen.stdin."""
    host = codex.CodexHost.__new__(codex.CodexHost)
    popen = types.SimpleNamespace(stdin=io.StringIO(), poll=lambda: None, pid=1)
    proc = codex.CodexProcess(popen, "sid", False)
    proc.thread = "P"
    host.procs = {"maps": proc}
    return host, proc, popen


def sent(popen, method=None):
    out = [json.loads(x) for x in popen.stdin.getvalue().splitlines()]
    return [m for m in out if method is None or m.get("method") == method]


class HostSeams(unittest.TestCase):
    """The two seams this slice needs in hosts/codex.py: every item of the session's own thread, and an interrupt even
    when the session has no running turn (only its subagents work)."""

    def test_item_seam_sees_every_item_of_the_own_thread(self):
        host, proc, _ = bare_host()
        seen = []
        stub = types.SimpleNamespace(item=lambda proc, name, method, item: seen.append((name, method, item["id"])) or
                                     [("x.fact", name, {"id": item["id"]})])
        with mock.patch.object(codex, "SEAMS", codex.SEAMS + (stub,)):
            facts = host._line("maps", {"method": "item/started", "params": {
                "threadId": "P", "item": {"type": "commandExecution", "id": "c1", "command": "ls"}}})
            host._line("maps", {"method": "item/completed", "params": {
                "threadId": "P", "item": {"type": "reasoning", "id": "r1"}}})
            host._line("maps", {"method": "item/completed", "params": {   # another thread: not the session's item
                "threadId": "C", "item": {"type": "reasoning", "id": "r2"}}})
        self.assertEqual(seen, [("maps", "item/started", "c1"), ("maps", "item/completed", "r1")])
        self.assertEqual([f[0] for f in facts], ["turn.tool", "x.fact"])

    def test_interrupt_seam_runs_without_a_turn(self):
        host, proc, popen = bare_host()
        host.alive = lambda name: True
        calls = []
        stub = types.SimpleNamespace(interrupt=lambda proc, name: calls.append(name))
        with mock.patch.object(codex, "SEAMS", codex.SEAMS + (stub,)):
            host.interrupt("maps")
            proc.turn = "T1"
            host.interrupt("maps")
        self.assertEqual(calls, ["maps", "maps"])
        self.assertEqual([m["params"]["turnId"] for m in sent(popen, "turn/interrupt")], ["T1"])

    def test_input_a_child_queued_goes_out(self):
        """A slice may queue input from a subagent's line (codex_items.child): the host sends it."""
        host, proc, popen = bare_host()

        def child(proc, name, method, params):
            proc.backlog.append([{"type": "text", "text": "note"}])
            return []
        with mock.patch.object(codex.codex_items, "child", child):
            host._line("maps", {"method": "turn/completed", "params": {"threadId": "C", "turn": {"id": "T9"}}})
        self.assertEqual(sent(popen, "turn/start")[0]["params"]["input"], [{"type": "text", "text": "note"}])


class ExperimentalApi(unittest.TestCase):
    """thread/backgroundTerminals/terminate answers -32600 "requires experimentalApi capability" without it (live
    probe 0.162.1): the session's app-server is initialised with it."""

    def test_initialize_declares_it(self):
        import tests  # noqa: F401 — no machine settings
        import os as _os
        import sys
        import tempfile
        import time
        import platforms
        from core import spool
        from core.daemon import Hub
        from hosts.codex import CodexHost
        state = tempfile.mkdtemp(prefix="hubcx-c-")
        self.addCleanup(platforms.current.rmtree, state)
        calls = _os.path.join(state, "calls.jsonl")
        fake = _os.path.join(_os.path.dirname(__file__), "fakes", "fake_codex.py")
        host = CodexHost(state, codex=(sys.executable, fake), env={"FAKE_CODEX_CALLS": calls}, all_sessions=True)
        hub = Hub(state, host, registry_dir=_os.path.join(state, "registry"))
        hub.start()
        self.addCleanup(host.kill_server)
        work = tempfile.mkdtemp(prefix="hubcx-w-")
        self.addCleanup(platforms.current.rmtree, work)
        spool.put(_os.path.join(state, "spool"), "session.requested", "s", {"cwd": work, "session_id": "sid-s"})
        end = time.time() + 15
        while time.time() < end and (hub.state["sessions"].get("s") or {}).get("state") != "idle":
            hub.tick()
            time.sleep(0.05)
        with open(calls, encoding="utf-8") as f:
            init = [json.loads(x) for x in f if json.loads(x)["method"] == "initialize"]
        self.assertEqual((hub.state["sessions"].get("s") or {}).get("state"), "idle")
        # the last one is the session's (the first, the hook's trust check of codex_rules, needs nothing experimental)
        self.assertEqual(init[-1]["params"].get("capabilities"), {"experimentalApi": True})


def note(method, thread="P", **params):
    return {"method": method, "params": {"threadId": thread, **params}}


def turn_started(turn, thread="P"):
    return note("turn/started", thread, turn={"id": turn, "status": "inProgress"})


def turn_completed(turn, thread="P", status="completed"):
    return note("turn/completed", thread, turn={"id": turn, "status": status})


def cmd(method, item_id, thread="P", status="inProgress", command="sleep 8; echo late", pid="74207", code=None, out=None):
    return note(method, thread, turnId="T", item={
        "type": "commandExecution", "id": item_id, "command": f"/bin/bash -lc '{command}'", "cwd": "/w",
        "processId": pid, "source": "unifiedExecStartup", "status": status,
        "commandActions": [{"type": "unknown", "command": command}], "aggregatedOutput": out, "exitCode": code})


def usage(thread, inp, out):
    last = {"inputTokens": inp, "outputTokens": out, "totalTokens": inp + out}
    return note("thread/tokenUsage/updated", thread, tokenUsage={"last": last, "total": last})


class Lines:
    """Feeds lines to a bare host and keeps the facts."""

    def setUp(self):
        self.host, self.proc, self.popen = bare_host()
        self.facts = []

    def feed(self, *msgs):
        for m in msgs:
            self.facts += self.host._line("maps", m)
        return self.facts

    def of(self, kind):
        return [f[2] for f in self.facts if f[0] == kind]


class Edits(Lines, unittest.TestCase):
    """A fileChange step shows the file and what changed, as Claude's Edit and Write do (the feed's diff)."""

    def change(self, *changes):
        self.feed(turn_started("T"), note("item/started", item={"type": "fileChange", "id": "e1", "changes": list(changes),
                                                                 "status": "inProgress"}))
        return self.of("turn.tool")[-1]

    def test_an_update_is_an_edit_of_its_file_with_the_hunks(self):
        step = self.change({"path": "/w/a.txt", "kind": {"type": "update", "move_path": None},
                            "diff": "@@ -1,3 +1,3 @@\n one\n-two\n+TWO\n three\n"})
        self.assertEqual((step["name"], step["input"]["file_path"]), ("Edit", "/w/a.txt"))
        self.assertEqual(step["input"]["edits"], [{"old_string": "one\ntwo\nthree", "new_string": "one\nTWO\nthree"}])

    def test_a_new_file_is_a_write(self):
        step = self.change({"path": "/w/b.txt", "kind": {"type": "add"}, "diff": "new line\n"})
        self.assertEqual((step["name"], step["input"]), ("Write", {"file_path": "/w/b.txt", "content": "new line\n"}))

    def test_several_files_one_step_the_first_files_edits_every_path(self):
        step = self.change({"path": "/w/a.txt", "kind": {"type": "update"}, "diff": "@@ -1 +1 @@\n-x\n+y\n"},
                           {"path": "/w/c.txt", "kind": {"type": "delete"}, "diff": "bye\n"})
        self.assertEqual(step["input"]["file_path"], "/w/a.txt")
        self.assertEqual(step["input"]["files"], ["/w/a.txt", "/w/c.txt"])
        # the feed draws one file's diff: c.txt's lines under a.txt's name would be a wrong diff
        self.assertEqual(step["input"]["edits"], [{"old_string": "x", "new_string": "y"}])


class Background(Lines, unittest.TestCase):
    """A command the session's turn left running is its background (the session is not free); its end wakes the
    session with Claude Code's <task-notification>; a stopped turn takes its running command with it."""

    def test_a_command_left_running_is_background_until_it_ends_then_a_notification(self):
        self.feed(turn_started("T"), cmd("item/started", "call_1"), turn_completed("T"))
        self.assertEqual(self.of("session.background"), [{"tasks": [
            {"id": "call_1", "kind": "bash", "title": "sleep 8; echo late"}]}])
        kinds = [f[0] for f in self.facts]
        self.assertLess(kinds.index("turn.result"), kinds.index("session.background"))
        # its row is not "stopped" at the turn's end: it went to the background, as Claude's run_in_background says
        self.assertEqual(self.of("turn.tool_result"), [{"id": "call_1", "error": False,
                                                        "text": "Command running in background with ID: call_1"}])
        self.assertEqual(sent(self.popen, "turn/start"), [])
        self.feed(cmd("item/completed", "call_1", status="completed", code=0, out="late\n"))
        self.assertEqual(self.of("session.background")[-1], {"tasks": []})
        (start,) = sent(self.popen, "turn/start")
        text = start["params"]["input"][0]["text"]
        self.assertTrue(text.startswith("<task-notification>\n<task-id>call_1</task-id>"))
        self.assertIn("exit code 0", text)
        self.assertIn("late", text)
        from hooks.hub_hook import translate   # the core reads it as Claude Code's notification: a service row
        kind, data = translate({"hook_event_name": "UserPromptSubmit", "prompt": text})
        self.assertEqual((kind, data["marker"], data["notice"]["status"]), ("turn.started", None, "completed"))

    def test_a_command_that_ends_within_its_turn_is_not_background(self):
        self.feed(turn_started("T"), cmd("item/started", "call_1"),
                  cmd("item/completed", "call_1", status="completed", code=0), turn_completed("T"))
        self.assertEqual(self.of("session.background"), [])
        self.assertEqual(sent(self.popen, "turn/start"), [])

    def test_an_interrupted_turn_stops_its_running_command_and_nothing_wakes_the_session(self):
        self.feed(turn_started("T"), cmd("item/started", "call_1", pid="57808"))
        self.proc.turn = "T"
        self.feed(turn_completed("T", status="interrupted"))
        (stop,) = sent(self.popen, "thread/backgroundTerminals/terminate")
        self.assertEqual(stop["params"], {"threadId": "P", "processId": "57808"})
        self.assertEqual(self.of("session.background"), [])   # stopped: not the session's background
        self.feed(cmd("item/completed", "call_1", status="failed", pid="57808"))
        self.assertEqual(sent(self.popen, "turn/start"), [])

    def test_background_of_an_earlier_turn_survives_an_interrupt(self):
        self.feed(turn_started("T"), cmd("item/started", "call_1"), turn_completed("T"),
                  turn_started("T2"), cmd("item/started", "call_2", pid="2"), turn_completed("T2", status="interrupted"))
        self.assertEqual([s["params"]["processId"] for s in sent(self.popen, "thread/backgroundTerminals/terminate")],
                         ["2"])
        self.assertEqual([t["id"] for t in self.of("session.background")[-1]["tasks"]], ["call_1"])


V1_SPAWN = {"type": "collabAgentToolCall", "id": "call_1", "tool": "spawnAgent", "senderThreadId": "P",
            "prompt": "check the tests", "model": "gpt-x", "agentsStates": {}}
V2_SPAWN = {"type": "subAgentActivity", "model": "gpt-x", "reasoningEffort": None, "id": "call_1", "kind": "started",
            "agentThreadId": "01a1-c", "agentPath": "/root/probe"}


class Subagents(Lines, unittest.TestCase):
    """A Codex subagent (its own thread in the session's process): its steps under the step that spawned it, its
    tokens in the session's turn, its work the session's background; an interrupt stops it."""

    def spawn_v1(self, child="01a1-c"):
        self.feed(turn_started("T"),
                  note("item/started", item={**V1_SPAWN, "status": "inProgress", "receiverThreadIds": []}),
                  note("item/completed", item={**V1_SPAWN, "status": "completed", "receiverThreadIds": [child]}),
                  turn_started("CT", child))

    def test_v1_steps_go_under_the_spawning_step_and_tokens_into_the_turn(self):
        self.spawn_v1()
        self.assertEqual(self.of("turn.tool")[0]["name"], "Agent")
        self.feed(cmd("item/started", "call_3", "01a1-c", command="ls"), usage("01a1-c", 10, 2),
                  cmd("item/completed", "call_3", "01a1-c", status="completed", code=0, out="a\n"),
                  note("item/completed", "01a1-c", item={"type": "agentMessage", "id": "m4", "text": "all green"}),
                  usage("P", 5, 1), turn_completed("CT", "01a1-c"), turn_completed("T"))
        child = [f for f in self.facts if f[0] in ("turn.tool", "turn.tool_result", "session.said") and
                 f[2].get("parent")]
        self.assertEqual([(f[0], f[2]["parent"]) for f in child],
                         [("turn.tool", "call_1"), ("turn.tool_result", "call_1"), ("session.said", "call_1")])
        self.assertEqual([f[0] for f in self.facts].count("turn.started"), 0)   # the child's input is not the session's
        (res,) = self.of("turn.result")
        self.assertEqual((res["tokens_in"], res["tokens_out"]), (15, 3))
        self.assertEqual(self.of("session.background"), [])   # it ended within the session's turn

    def test_a_subagent_running_on_is_background_its_end_notifies_and_its_tokens_count(self):
        self.spawn_v1()
        self.feed(turn_completed("T"))
        self.assertEqual(self.of("session.background"), [{"tasks": [
            {"id": "agent_01a1c", "kind": "agent", "title": "check the tests"}]}])
        self.feed(cmd("item/started", "call_3", "01a1-c", command="pytest"))   # its own step: not a task of its own
        self.assertEqual(len(self.of("session.background")), 1)
        self.feed(usage("01a1-c", 100, 20), cmd("item/completed", "call_3", "01a1-c", status="completed", code=0),
                  note("item/completed", "01a1-c", item={"type": "agentMessage", "id": "m4", "text": "found 2 bugs"}),
                  turn_completed("CT", "01a1-c"))
        self.assertEqual(self.of("session.background")[-1], {"tasks": []})
        self.assertEqual(self.of("codex.spent"), [{"model": "gpt-x", "tokens_in": 100, "tokens_out": 20}])
        (start,) = sent(self.popen, "turn/start")
        text = start["params"]["input"][0]["text"]
        self.assertTrue(text.startswith("<task-notification>\n<task-id>agent_01a1c</task-id>"))
        self.assertIn("found 2 bugs", text)

    def test_v2_subagent_activity(self):
        self.feed(turn_started("T"), note("item/started", item=V2_SPAWN), note("item/completed", item=V2_SPAWN),
                  turn_started("CT", "01a1-c"), cmd("item/started", "call_3", "01a1-c", command="ls"))
        self.assertEqual([(t["name"], t.get("parent")) for t in self.of("turn.tool")],
                         [("Agent", None), ("Bash", "call_1")])
        self.assertEqual(self.of("turn.tool")[0]["input"]["description"], "/root/probe")
        done = {**V2_SPAWN, "id": "subagent-completed-CT", "kind": "completed", "model": None}
        self.feed(turn_completed("CT", "01a1-c"), note("item/started", item=done), note("item/completed", item=done))
        self.assertEqual(len(self.of("turn.tool")), 2)   # its end is not a step

    def test_interrupt_stops_a_subagent_even_when_the_session_is_free(self):
        self.host.alive = lambda name: True
        self.spawn_v1()
        self.feed(cmd("item/started", "call_3", "01a1-c", pid="55203"))
        self.proc.turn = "T"
        self.feed(turn_completed("T"))
        self.host.interrupt("maps")
        self.assertEqual([m["params"] for m in sent(self.popen, "turn/interrupt")],
                         [{"threadId": "01a1-c", "turnId": "CT"}])
        self.feed(turn_completed("CT", "01a1-c", status="interrupted"))
        self.assertEqual([m["params"] for m in sent(self.popen, "thread/backgroundTerminals/terminate")],
                         [{"threadId": "01a1-c", "processId": "55203"}])
        self.feed(cmd("item/completed", "call_3", "01a1-c", status="failed", pid="55203"))
        self.assertEqual(self.of("session.background")[-1], {"tasks": []})
        self.assertEqual(sent(self.popen, "turn/start"), [])   # stopped by the owner: no notification

    def test_a_subagent_being_stopped_with_the_turn_does_not_flash_as_background(self):
        self.host.alive = lambda name: True
        self.spawn_v1()
        self.proc.turn = "T"
        self.host.interrupt("maps")
        self.feed(turn_completed("T", status="interrupted"), turn_completed("CT", "01a1-c", status="interrupted"))
        self.assertEqual(self.of("session.background"), [])

    def test_steps_file_for_the_page(self):
        import tempfile
        from core import subagents
        with tempfile.TemporaryDirectory() as tmp:
            self.proc.state_dir = tmp
            self.host.state_dir = tmp
            self.host._remember("maps", "sid", "P")
            self.spawn_v1()
            self.feed(cmd("item/started", "call_3", "01a1-c", command="ls"),
                      cmd("item/completed", "call_3", "01a1-c", status="completed", code=0, out="a\n"),
                      note("item/completed", "01a1-c", item={"type": "agentMessage", "id": "m4", "text": "done"}))
            mid = subagents.steps(os.path.join(tmp, "no-claude"), "/w", "sid", "call_1", state_dir=tmp)
            self.feed(cmd("item/started", "call_5", "01a1-c", command="sleep 600"))   # a long step shows at once
            running = subagents.steps(os.path.join(tmp, "no-claude"), "/w", "sid", "call_1", state_dir=tmp)
            self.assertEqual(running["items"][-1]["status"], "running")
            self.feed(turn_completed("CT", "01a1-c"))   # written at most every SAVE_S, at the turn's end at once
            got = subagents.steps(os.path.join(tmp, "no-claude"), "/w", "sid", "call_1", state_dir=tmp)
            self.assertIsNone(subagents.steps(os.path.join(tmp, "no-claude"), "/w", "other", "call_1", state_dir=tmp))
        self.assertEqual([i.get("name") for i in mid["items"]], ["Bash"])   # the first step went at once
        self.assertEqual(got["agent"]["description"], "check the tests")
        self.assertEqual([(i["kind"], i.get("name"), i.get("status")) for i in got["items"]],
                         [("tool", "Bash", "done"), ("said", None, None), ("tool", "Bash", "running")])
        self.assertEqual(got["items"][0]["result"], "a\n")


class Memory(Lines, unittest.TestCase):
    def test_a_busy_old_subagent_keeps_its_steps_while_newer_ones_come_and_go(self):
        from hosts import codex_items
        Subagents.spawn_v1(self, "old")
        for n in range(codex_items.AGENTS_KEPT + 5):
            item = {**V1_SPAWN, "id": f"s{n}", "status": "completed", "receiverThreadIds": [f"c{n}"]}
            self.feed(note("item/completed", item=item), cmd("item/started", f"x{n}", f"c{n}", command="ls"),
                      cmd("item/started", f"y{n}", "old", command="pwd"))
        work = self.proc.cx_items
        self.assertEqual(len(work.steps["call_1"]), codex_items.AGENTS_KEPT + 5)
        self.assertLessEqual(len(work.steps), codex_items.AGENTS_KEPT)


class ReviewEdges(Lines, unittest.TestCase):
    """Edges the review of slice C found."""

    def test_v1_subagent_lines_before_its_spawn_step_completes_move_under_the_step(self):
        import tempfile
        from core import subagents
        with tempfile.TemporaryDirectory() as tmp:
            self.proc.state_dir = self.host.state_dir = tmp
            self.host._remember("maps", "sid", "P")
            self.feed(turn_started("T"),
                      note("item/started", item={**V1_SPAWN, "status": "inProgress", "receiverThreadIds": []}),
                      turn_started("CT", "01a1-c"), cmd("item/started", "call_3", "01a1-c", command="ls"),
                      note("item/completed", item={**V1_SPAWN, "status": "completed", "receiverThreadIds": ["01a1-c"]}),
                      cmd("item/completed", "call_3", "01a1-c", status="completed", code=0, out="a\n"),
                      turn_completed("CT", "01a1-c"))
            got = subagents.steps(os.path.join(tmp, "no"), "/w", "sid", "call_1", state_dir=tmp)
            left = os.listdir(os.path.join(tmp, "sessions", "maps", "codex-agents"))
        self.assertEqual([(i["id"], i["status"]) for i in got["items"]], [("call_3", "done")])
        self.assertEqual(left, ["call_1.json"])

    def test_a_subagent_closed_without_its_turns_end_frees_the_session(self):
        self.feed(turn_started("T"),
                  note("item/completed", item={**V1_SPAWN, "status": "completed", "receiverThreadIds": ["01a1-c"]}),
                  turn_started("CT", "01a1-c"), turn_completed("T"))
        self.assertEqual(len(self.of("session.background")[-1]["tasks"]), 1)
        self.feed(cmd("item/started", "call_3", "01a1-c", command="make"), note("thread/closed", "01a1-c"))
        # closed (closeAgent — the session's own doing): no news; what it left running is the session's background
        self.assertEqual([t["id"] for t in self.of("session.background")[-1]["tasks"]], ["call_3_01a1c"])
        self.assertEqual(sent(self.popen, "turn/start"), [])

    def test_a_subagent_that_errors_out_is_news(self):
        self.feed(turn_started("T"),
                  note("item/completed", item={**V1_SPAWN, "status": "completed", "receiverThreadIds": ["01a1-c"]}),
                  turn_started("CT", "01a1-c"), turn_completed("T"),
                  note("thread/status/changed", "01a1-c", status={"type": "systemError"}))
        self.assertEqual(self.of("session.background")[-1], {"tasks": []})
        (start,) = sent(self.popen, "turn/start")
        self.assertIn("<status>failed</status>", start["params"]["input"][0]["text"])

    def test_an_evicted_subagent_keeps_its_earlier_steps_on_disk(self):
        import tempfile
        from hosts import codex_items
        with tempfile.TemporaryDirectory() as tmp:
            self.proc.state_dir = tmp
            self.feed(note("item/completed", item={**V1_SPAWN, "status": "completed", "receiverThreadIds": ["old"]}),
                      turn_started("OT", "old"), cmd("item/started", "o1", "old", command="pwd"))
            for n in range(codex_items.AGENTS_KEPT + 2):
                item = {**V1_SPAWN, "id": f"s{n}", "status": "completed", "receiverThreadIds": [f"c{n}"]}
                self.feed(note("item/completed", item=item), cmd("item/started", f"x{n}", f"c{n}", command="ls"))
            self.assertNotIn("call_1", self.proc.cx_items.steps)
            self.feed(cmd("item/completed", "o1", "old", status="completed", code=0, out="/w\n"),
                      cmd("item/started", "o2", "old", command="ls"), turn_completed("OT", "old"))
            with open(codex_items.steps_path(tmp, "maps", "call_1"), encoding="utf-8") as f:
                items = json.load(f)["items"]
        self.assertEqual([(i["id"], i["status"]) for i in items], [("o1", "done"), ("o2", "running")])

    def test_a_refused_stop_gives_the_command_back_to_the_session(self):
        self.feed(turn_started("T"), cmd("item/started", "call_1", pid="57808"))
        self.proc.turn = "T"
        self.feed(turn_completed("T", status="interrupted"))
        (stop,) = sent(self.popen, "thread/backgroundTerminals/terminate")
        self.feed({"id": stop["id"], "error": {"code": -32600, "message": "requires experimentalApi capability"}})
        self.assertEqual([t["id"] for t in self.of("session.background")[-1]["tasks"]], ["call_1"])
        self.feed(cmd("item/completed", "call_1", status="completed", code=0, pid="57808"))
        self.assertEqual(self.of("session.background")[-1], {"tasks": []})
        self.assertEqual(sent(self.popen, "turn/start"), [])   # the owner stopped it: its end wakes no one

    def test_only_a_spawn_is_an_agent_step(self):
        self.feed(turn_started("T"), note("item/started", item={**V1_SPAWN, "tool": "wait", "status": "inProgress",
                                                                 "receiverThreadIds": ["01a1-c"], "prompt": None}))
        self.assertEqual(self.of("turn.tool")[-1]["name"], "AgentWait")
        self.assertEqual(self.of("turn.tool")[-1]["input"], {"agents": ["01a1-c"]})
        for call, want in (("closeAgent", "AgentClose"), ("", "AgentCall")):
            self.feed(note("item/started", item={**V1_SPAWN, "id": call or "x", "tool": call, "status": "inProgress",
                                                  "receiverThreadIds": []}))
            self.assertEqual(self.of("turn.tool")[-1]["name"], want)

    def test_a_declined_mcp_call_says_so(self):
        call = {"type": "mcpToolCall", "id": "m1", "server": "files", "tool": "read", "status": "declined"}
        self.feed(turn_started("T"), note("item/completed", item=call))
        got = self.of("turn.tool_result")[0]
        self.assertEqual((got["error"], got["text"], got["mcp"]), (True, "declined", "files"))


class McpServer(Lines, unittest.TestCase):
    """An MCP tool's result names its server, as from the claude host: its error makes mcpwatch ask for the status."""

    def test_an_mcp_tools_error_names_its_server_and_wakes_the_watch(self):
        from core import mcpwatch
        call = {"type": "mcpToolCall", "id": "m1", "server": "files", "tool": "read", "arguments": {}}
        self.feed(turn_started("T"), note("item/started", item={**call, "status": "inProgress"}),
                  note("item/completed", item={**call, "status": "failed", "error": {"message": "closed"}}),
                  note("item/completed", "01a1-c", item={**call, "id": "m2", "status": "failed",
                                                         "error": {"message": "closed"}}))
        own, child = self.of("turn.tool_result")
        self.assertEqual((own["mcp"], own["error"], child["mcp"]), ("files", True, "files"))
        st, actions = {"sessions": {"maps": {"name": "maps", "state": "busy"}}}, []
        mcpwatch.tool_error(st, {"entity": "maps", "ts": 100}, own, actions)
        self.assertEqual(actions, [{"do": "mcp_status", "session": "maps"}])

    def test_other_steps_have_no_server(self):
        self.feed(turn_started("T"), cmd("item/started", "c1"), cmd("item/completed", "c1", status="completed", code=0))
        self.assertNotIn("mcp", self.of("turn.tool_result")[0])


class Declined(Lines, unittest.TestCase):
    """A step the owner refused in a permission card (status declined) ends as an error in the feed, as Claude's."""

    def test_a_declined_edit_and_command_are_errors(self):
        change = {"type": "fileChange", "id": "e1", "changes": [{"path": "/w/a.txt", "kind": {"type": "add"},
                                                                 "diff": "x\n"}]}
        self.feed(turn_started("T"), note("item/started", item={**change, "status": "inProgress"}),
                  note("item/completed", item={**change, "status": "declined"}),
                  cmd("item/started", "c1"), cmd("item/completed", "c1", status="declined"))
        edit, run = self.of("turn.tool_result")
        self.assertEqual((edit["error"], edit["text"]), (True, "declined"))
        self.assertEqual((run["error"], run["text"]), (True, "declined"))

    def test_a_completed_one_is_not(self):
        change = {"type": "fileChange", "id": "e1", "changes": []}
        self.feed(turn_started("T"), note("item/completed", item={**change, "status": "completed"}))
        self.assertFalse(self.of("turn.tool_result")[0]["error"])


class Features(unittest.TestCase):
    def test_both_subagent_generations_on_and_not_pinned_off(self):
        from hosts import codex_items, codex_set
        self.assertEqual(set(codex_items.features()), {"multi_agent", "multi_agent_v2"})
        pins = codex_set.args(codex_items.features())
        self.assertFalse([p for p in pins if "multi_agent" in p])
        self.assertIn("features.memories=false", pins)   # the rest of the isolation stays

    def test_the_liaison_has_no_subagents(self):
        # it reads another hub's words; Claude's liaison has Agent and Task denied (core/liaison.DENY)
        from hosts import codex_items, codex_set
        self.assertEqual(codex_items.features(liaison=True), ())
        self.assertIn("features.multi_agent=false", codex_set.args(codex_items.features(liaison=True)))


class ClaudeHubAsMain(unittest.TestCase):
    """On a hub without Codex /api/subagent answers as before: Claude's transcript or nothing."""

    def test_no_codex_files_no_steps(self):
        import tempfile
        from core import subagents
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "sessions", "maps"))
            with mock.patch.dict(os.environ, {"HUB_STATE_DIR": tmp}):
                self.assertIsNone(subagents.steps(os.path.join(tmp, "claude"), "/w", "sid", "toolu_A"))


if __name__ == "__main__":
    unittest.main()
