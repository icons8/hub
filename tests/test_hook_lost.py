"""A core hook cancelled by its timeout (a loaded machine: the interpreter starts longer than the hook may run). Claude
Code goes on without it, so the hook's fact is lost: the session stayed "starting" without SessionStart until
start_stuck killed it, and without UserPromptSubmit the typed message hung "typing" while the session worked, was
retyped into the void and failed. On stream-json the host sees both on claude's own output (hook_response with outcome
cancelled, the line taken into the turn) and puts the lost fact itself; hubd writes it only while the core still lacks
it, so a hook that wrote its fact before being killed gives no second one."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

import platforms
from core import journal, spool
from core.daemon import Hub
from hosts.streamjson import StreamHost

FAKE = os.path.join(os.path.dirname(__file__), "fakes", "fake_claude_stream.py")


class HookLost(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hooklost-")
        self.addCleanup(platforms.current.rmtree, self.state)

    def hub(self, **env):
        host = StreamHost(self.state, claude=(sys.executable, FAKE), env={"FAKE_TURN_S": "0.2", **env})
        hub = Hub(self.state, host, registry_dir=os.path.join(self.state, "registry"))
        hub.start()
        self.addCleanup(host.kill_server)
        self.h = hub
        return hub

    def cli(self, type, entity=None, **data):
        spool.put(os.path.join(self.state, "spool"), type, entity, data)

    def until(self, pred, timeout=15.0):
        end = time.time() + timeout
        while time.time() < end:
            self.h.tick()
            if pred():
                return
            time.sleep(0.03)
        self.fail(f"did not wait; {json.dumps(self.h.state['sessions'].get('a'), default=str)[:600]} "
                  f"{json.dumps(self.h.state['messages'], default=str)[:600]}")

    def events(self, type):
        return [e for e in journal.read(self.h.journal.path) if e["type"] == type]

    def session_up(self):
        self.cli("session.requested", "a", cwd=self.state, session_id="sid-a")
        self.until(lambda: self.h.state["sessions"].get("a", {}).get("state") in ("idle", "parked"))

    def send(self, text="hello"):
        before = set(self.h.state["messages"])
        self.cli("message.received", to="a", text=text)
        self.until(lambda: set(self.h.state["messages"]) - before)
        (mid,) = set(self.h.state["messages"]) - before
        return mid

    def turn_done(self, mid):
        self.until(lambda: self.h.state["messages"][mid]["state"] == "delivered"
                   and any(e["data"].get("text") == "ok: hello" for e in self.events("session.said"))
                   and self.h.state["sessions"]["a"]["state"] == "idle")

    def test_lost_prompt_hook_delivers_by_the_line_claude_took(self):
        self.hub(FAKE_HOOKS_LOST="UserPromptSubmit")
        self.session_up()
        mid = self.send()
        self.turn_done(mid)
        started = [e for e in self.events("turn.started") if e["data"].get("marker") == mid]
        self.assertEqual(len(started), 1, started)
        self.assertEqual(started[0]["src"], "host")
        self.assertEqual(started[0]["data"]["prompt_head"][:len(f"[hub:{mid}")], f"[hub:{mid}")
        self.assertTrue(self.h.state["sessions"]["a"].get("has_history"))   # a later spawn continues the conversation
        # typed once: no retyping into a process that already took the line
        self.assertEqual([e for e in self.events("host.done") if e["data"].get("do") == "type"
                          and e["data"].get("message") == mid and e["data"].get("result") != "sent"], [])

    def test_lost_start_hook_brings_the_session_up(self):
        self.hub(FAKE_HOOKS_LOST="SessionStart")
        self.session_up()
        (up,) = self.events("session.started")
        self.assertEqual((up["src"], up["data"].get("source"), up["data"].get("session_id")),
                         ("host", "startup", "sid-a"))
        mid = self.send()
        self.turn_done(mid)

    def test_hook_that_wrote_before_it_was_cut_gives_one_fact(self):
        # killed while the interpreter exits: the fact is in the spool, claude says "cancelled" all the same
        self.hub(FAKE_HOOKS_CUT="SessionStart,UserPromptSubmit")
        self.session_up()
        mid = self.send()
        self.turn_done(mid)
        self.assertEqual([e["src"] for e in self.events("session.started")], ["hook"])
        self.assertEqual([e["src"] for e in self.events("turn.started") if e["data"].get("marker") == mid], ["hook"])

    def test_hook_that_outlived_its_timeout_gives_one_fact(self):
        # the host put the turn's start first; the hook, not killed, wrote it a moment later: that one is not written again
        self.hub(FAKE_HOOKS_LATE="UserPromptSubmit")
        self.session_up()
        mid = self.send()
        self.turn_done(mid)   # the late hook wrote before the turn went on: by its result the file is taken
        self.assertEqual([e["src"] for e in self.events("turn.started") if e["data"].get("marker") == mid], ["host"])
        self.assertEqual(self.h.state["sessions"]["a"]["state"], "idle")

    def test_hooks_in_time_change_nothing(self):
        self.hub()
        self.session_up()
        mid = self.send()
        self.turn_done(mid)
        self.assertEqual({e["src"] for e in self.events("session.started") + self.events("turn.started")}, {"hook"})


class Watch(unittest.TestCase):
    """HookWatch on the lines alone: which of them give the hook's fact."""

    def setUp(self):
        from hosts.streamjson import HookWatch
        self.w = HookWatch()

    def feed(self, *lines, sent=("m5",)):
        return [f for line in lines for f in self.w.line(line, "sid-a", set(sent))]

    @staticmethod
    def hook(event, outcome="success", source=None, hid="h1", sub="hook_response"):
        return {"type": "system", "subtype": sub, "hook_id": hid, "hook_event": event, "outcome": outcome,
                "hook_name": f"{event}:{source}" if source else event, "session_id": "sid-a"}

    @staticmethod
    def took(text):
        return {"type": "user", "message": {"role": "user", "content": text}, "isReplay": True, "parent_tool_use_id": None}

    def test_only_a_line_the_host_typed_and_only_once(self):
        cut = self.hook("UserPromptSubmit", "cancelled")
        self.assertEqual(self.feed(cut, self.took("[hub:m7] not ours")), [])
        (fact,) = self.feed(self.took("[hub:m5] hello"))
        self.assertEqual((fact[0], fact[1]["marker"], fact[1]["fallback"]), ("turn.started", "m5", "hook"))
        # a retelling of the same line (after /compact) and a later line once the turn went on give nothing
        self.assertEqual(self.feed(self.took("[hub:m5] hello")), [])
        self.assertEqual(self.feed({"type": "assistant", "message": {"content": []}}, self.took("[hub:m6] next"),
                                   sent=("m5", "m6")), [])

    def test_no_fact_while_the_hook_answers(self):
        self.assertEqual(self.feed(self.hook("UserPromptSubmit"), self.took("[hub:m5] hello")), [])

    def test_start_fact_once_every_start_hook_answered(self):
        started = [self.hook("SessionStart", None, "resume", h, "hook_started") for h in ("h1", "h2")]
        self.assertEqual(self.feed(*started, self.hook("SessionStart", "cancelled", "resume", "h1")), [])
        (fact,) = self.feed(self.hook("SessionStart", "success", "resume", "h2"))
        self.assertEqual(fact, ("session.started", {"session_id": "sid-a", "source": "resume", "fallback": "hook"}))
        self.assertEqual(self.feed(self.hook("SessionStart", "cancelled", "compact")), [])   # not a start of the process
        # /clear in the live process: the line already names the new conversation
        (fact,) = self.feed(self.hook("SessionStart", "cancelled", "clear"))
        self.assertEqual(fact[1]["source"], "clear")

    def test_the_core_takes_only_what_it_lacks(self):
        from core import reducer
        st = {"sessions": {"a": {"state": "idle", "session_id": "sid-a"}}, "aliases": {"was:old": "a"},
              "messages": {"m5": {"session": "a", "state": "typing"}, "m6": {"session": "a", "state": "delivered"}}}
        lost = lambda kind, **d: reducer.hook_lost(st, kind, "a", d)
        self.assertTrue(lost("turn.started", marker="m5"))
        self.assertTrue(reducer.hook_lost(st, "turn.started", "old", {"marker": "m5"}))   # renamed, the process kept its name
        self.assertFalse(lost("turn.started", marker="m6"))   # delivered: the hook said it
        self.assertFalse(lost("session.started", source="startup", session_id="sid-a"))   # not starting: the hook said it
        self.assertTrue(lost("session.started", source="clear", session_id="sid-b"))
        self.assertFalse(lost("session.started", source="clear", session_id="sid-a"))
        st["sessions"]["a"]["state"] = "starting"
        self.assertTrue(lost("session.started", source="resume", session_id="sid-a"))
        self.assertFalse(lost("session.started", source="resume", session_id="sid-x"))


class ResumeOnDisk(unittest.TestCase):
    def test_conversation_on_disk_is_resumed(self):
        # the core never learned a turn began (has_history unset), the conversation is on disk: --session-id would fail
        # with "Session ID … is already in use" on every raise
        from hosts import worktree
        state = tempfile.mkdtemp(prefix="hooklost-")
        self.addCleanup(platforms.current.rmtree, state)
        conf, argv = os.path.join(state, "claude"), os.path.join(state, "argv")
        folder = os.path.join(conf, "projects", worktree.slug(state))
        os.makedirs(folder)
        open(os.path.join(folder, "sid-a.jsonl"), "w").close()
        # the hub's own CLAUDE_CONFIG_DIR (catalog.pin_claude_dir): its sessions inherit it
        self.enterContext(mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": conf}))
        host = StreamHost(state, claude=(sys.executable, FAKE), env={"FAKE_ARGV_DIR": argv})
        self.addCleanup(host.kill_server)
        for sid in ("sid-a", "sid-b"):
            host.spawn(f"s-{sid}", sid, resume=False, cwd=state)
        end = time.time() + 10
        while time.time() < end and len(os.listdir(argv) if os.path.isdir(argv) else []) < 2:
            time.sleep(0.05)
        seen = {}
        for f in os.listdir(argv):
            with open(os.path.join(argv, f)) as fh:
                a = json.load(fh)
            seen[a[a.index("-n") + 1]] = a
        self.assertEqual(seen["s-sid-a"][seen["s-sid-a"].index("sid-a") - 1], "--resume")
        self.assertEqual(seen["s-sid-b"][seen["s-sid-b"].index("sid-b") - 1], "--session-id")


if __name__ == "__main__":
    unittest.main()
