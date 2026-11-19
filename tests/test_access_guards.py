"""Who may answer a session's permission request and open, delete, stop or interrupt a session. Lawful: the owner (page,
Telegram, CLI outside sessions), the hub and the core itself (daemon events with no channel or with channel core).
Another session and a plugin process (channel plugin, API 1.2) may not: a plugin is promised its own facts, a
notification to the owner and a message to a session (EXTENDING.md), not the owner's rights."""
import tests  # noqa: F401 — keeps the machine's settings out
import unittest

from core import reducer

WEB = {"channel": "web", "owner": True}
TELEGRAM = {"channel": "telegram", "owner": True}
CLI = {"channel": "cli", "owner": True}
HUB = {"channel": "session", "owner": False, "sender": "hub"}
MAPS = {"channel": "session", "owner": False, "sender": "maps"}
NOTES = {"channel": "session", "owner": False, "sender": "notes"}
PLUGIN = {"channel": "plugin", "owner": False, "sender": "hello"}


class Base(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.acts = reducer.empty(), 0, []
        self.ev("session.requested", "hub", cwd="/h", session_id="s-h", kind="hub", **CLI)
        for n in ("maps", "notes"):
            self.ev("session.requested", n, cwd=f"/w/{n}", session_id=f"s-{n}", **CLI)
        for n in ("hub", "maps", "notes"):
            self.ev("session.started", n)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 1000 + self.seq, "type": type,
                                                      "entity": entity, "data": data})
        self.acts += acts
        return why

    def responded(self):
        return [a for a in self.acts if a["do"] == "respond"]


class PermissionAnswer(Base):
    def ask(self):
        self.ev("turn.started", "notes", prompt_head="work")
        self.ev("permission.requested", "notes", request_id="r1", tool="Bash", input={"command": "rm -rf build"})
        self.assertEqual(self.st["sessions"]["notes"]["state"], "dialog")
        self.acts = []

    def test_another_session_cannot_answer(self):
        self.ask()
        why = self.ev("permission.answered", "notes", request_id="r1", behavior="allow", **MAPS)
        self.assertIn("the owner, the hub", why or "")
        self.assertEqual(self.responded(), [])
        self.assertEqual(self.st["sessions"]["notes"]["state"], "dialog")
        self.assertEqual([r["request_id"] for r in self.st["sessions"]["notes"]["pending"]], ["r1"])

    def test_the_session_cannot_answer_its_own(self):
        self.ask()
        self.assertIsNotNone(self.ev("permission.answered", "notes", request_id="r1", behavior="allow", **NOTES))
        self.assertEqual(self.responded(), [])

    def test_a_plugin_process_cannot_answer(self):
        self.ask()
        self.assertIsNotNone(self.ev("permission.answered", "notes", request_id="r1", behavior="allow", **PLUGIN))
        self.assertEqual(self.responded(), [])

    def test_the_raiser_answers_its_executor_and_no_one_else_does(self):
        self.ev("session.requested", "maps-1", cwd="/w/maps", session_id="s-x", executor=True, raised_by="maps")
        self.ev("session.started", "maps-1")
        self.ev("turn.started", "maps-1", prompt_head="work")
        self.ev("permission.requested", "maps-1", request_id="r2", tool="Bash", input={"command": "rm -rf build"})
        self.acts = []
        self.assertIsNotNone(self.ev("permission.answered", "maps-1", request_id="r2", behavior="allow", **NOTES))
        self.assertIsNotNone(self.ev("permission.answered", "maps-1", request_id="r2", behavior="allow",
                                     **{**PLUGIN, "sender": "maps"}))          # the starter's name, but not from a session
        self.assertEqual(self.responded(), [])
        self.assertIsNone(self.ev("permission.answered", "maps-1", request_id="r2", behavior="allow", **MAPS))
        (r,) = self.responded()
        self.assertEqual((r["request_id"], r["response"]["behavior"]), ("r2", "allow"))

    def test_owner_words_handed_on_by_another_session_do_not_answer_its_form(self):
        # review: any session can run `hub task new <session> --for mN`, and the core gave the owner's words to it as the
        # answer to that session's form (via channel core), bypassing the rule "the owner, the hub or the starter answers"
        self.ev("turn.started", "notes", prompt_head="work")
        self.ev("permission.requested", "notes", request_id="r1", tool="AskUserQuestion",
                input={"questions": [{"question": "Which one do we take?", "options": [{"label": "A"}]}]})
        plugin_as_hub = {**PLUGIN, "sender": "hub"}                    # a plugin process named like the hub is not the hub
        for i, (to, by) in enumerate((("maps", MAPS), ("hub", plugin_as_hub), ("hub", HUB))):
            self.ev("message.received", None, to=to, text=f"take B {i}", **WEB)
            self.acts = []
            self.assertIsNone(self.ev("delegation.created", None, session="notes", context="", **{"for": f"m{self.seq}"},
                                      **by))
            if by is not HUB:
                self.assertEqual(self.responded(), [])
                self.assertEqual(self.st["sessions"]["notes"]["state"], "dialog")
        (r,) = self.responded()                                    # the hub relayed his words: that is an answer
        self.assertEqual(r["response"]["updatedInput"]["answers"], {"Which one do we take?": "take B 2"})
        # the starter to its own executor: also an answer (it can answer that executor's form itself)
        self.ev("session.requested", "maps-1", cwd="/w/maps", session_id="s-x", executor=True, raised_by="maps")
        self.ev("session.started", "maps-1")
        self.ev("turn.started", "maps-1", prompt_head="work")
        self.ev("permission.requested", "maps-1", request_id="r2", tool="AskUserQuestion",
                input={"questions": [{"question": "Which?", "options": [{"label": "A"}]}]})
        self.ev("message.received", None, to="maps", text="the second", **WEB)
        self.acts = []
        self.assertIsNone(self.ev("delegation.created", None, session="maps-1", context="", **{"for": f"m{self.seq}"},
                                  **MAPS))
        (r,) = self.responded()
        self.assertEqual(r["response"]["updatedInput"]["answers"], {"Which?": "the second"})

    def test_owner_hub_and_core_answer(self):
        for who in (WEB, TELEGRAM, CLI, HUB, {}, {"channel": "core", "owner": True}):
            with self.subTest(who=who):
                self.ask()
                self.assertIsNone(self.ev("permission.answered", "notes", request_id="r1", behavior="deny",
                                          message="no", **who))
                (r,) = self.responded()
                self.assertEqual((r["request_id"], r["response"]["behavior"]), ("r1", "deny"))
                self.ev("turn.stopped", "notes")


class SessionControl(Base):
    def executor(self):
        self.ev("session.requested", "maps-1", cwd="/w/maps", session_id="s-x", executor=True, raised_by="maps")
        self.ev("session.started", "maps-1")
        self.assertEqual(self.st["sessions"]["maps-1"]["role"], "executor")

    def test_a_plugin_process_cannot_remove_a_session(self):
        self.st["sessions"]["notes"]["state"] = "asleep"
        why = self.ev("session.remove_requested", "notes", **PLUGIN)
        self.assertIn("the hub or the owner", why or "")
        self.assertEqual(self.st["sessions"]["notes"]["state"], "asleep")

    def test_a_plugin_process_cannot_open_stop_or_interrupt(self):
        self.assertIsNotNone(self.ev("session.requested", "new", cwd="/w/new", session_id="s-new", **PLUGIN))
        self.assertNotIn("new", self.st["sessions"])
        self.assertFalse([m for m in self.st["messages"].values() if m["session"] == "hub"])   # and it is no request to the hub either
        self.executor()
        self.assertIsNotNone(self.ev("session.stop_requested", "maps-1", **PLUGIN))
        self.ev("turn.started", "maps-1", prompt_head="work")
        self.acts = []
        self.assertIsNotNone(self.ev("session.interrupt_requested", "maps-1", **PLUGIN))
        self.assertEqual(self.st["sessions"]["maps-1"]["state"], "busy")
        self.assertFalse([a for a in self.acts if a["do"] in ("interrupt", "release", "kill")])

    def test_owner_and_hub_still_remove_open_and_stop(self):
        for i, who in enumerate((WEB, TELEGRAM, CLI, HUB)):
            with self.subTest(who=who):
                name = f"tmp{i}"
                self.assertIsNone(self.ev("session.requested", name, cwd=f"/w/{name}", session_id=f"s-{name}", **who))
                self.ev("session.started", name)
                self.st["sessions"][name]["state"] = "asleep"
                self.assertIsNone(self.ev("session.remove_requested", name, **who))
                self.assertEqual(self.st["sessions"][name]["state"], "removed")
        self.executor()
        self.ev("turn.started", "maps-1", prompt_head="work")
        self.assertIsNone(self.ev("session.interrupt_requested", "maps-1", **WEB))
        self.assertIsNone(self.ev("session.stop_requested", "maps-1", **HUB))
        self.assertEqual(self.st["sessions"]["maps-1"]["state"], "retired")

    def test_a_session_asks_the_hub_to_open_one(self):
        self.assertIsNone(self.ev("session.requested", "new", cwd="/w/new", session_id="s-new", **MAPS))
        self.assertNotIn("new", self.st["sessions"])
        self.assertTrue([m for m in self.st["messages"].values() if m["session"] == "hub"])


if __name__ == "__main__":
    unittest.main()
