"""After the "Connected: X" message a session ended its turn three times in a day with nothing or with
"looking…", and the core counted the empty result as the reply to the task (2026-10-01). Now the connection message
carries the text of the open task the tool was requested for, and an empty turn result is not a reply."""
import tests  # noqa: F401 — hide the machine's settings
import pathlib
import unittest

from core import forms
from tests.test_replays import load, run

REPLAY = pathlib.Path(__file__).parent / "replays" / "tools_owner_yes_in_chat_and_continue.json"


class Continue(unittest.TestCase):
    def setUp(self):
        self.events = load(REPLAY)["events"]

    def go(self, *more):
        t = self.events[-1]["t"]
        return run(self.events + [{**e, "t": t + i + 1} for i, e in enumerate(more)])[0]

    def test_connected_message_repeats_the_task(self):
        st = self.go()
        text = st["messages"]["m15x2"]["text"]
        self.assertIn("Connected: stripe", text)
        words = next(e["data"]["text"] for e in self.events if e["type"] == "message.received" and e["data"].get("owner"))
        self.assertIn(words, text)   # the owner's words from the task
        self.assertIn("look at the mail", text)                    # hub context
        self.assertIn("d11", text)

    def test_own_reload_with_a_new_tool_is_one_continue(self):
        busy = self.events[:12]
        t = busy[-1]["t"]
        more = [{"type": "tools.approved", "entity": "w", "data": {"tool": "stripe", "channel": "web", "owner": True}},
                {"type": "session.reload_requested", "entity": "w",
                 "data": {"channel": "session", "owner": False, "sender": "w"}},
                {"type": "turn.stopped", "entity": "w"},
                {"type": "session.started", "entity": "w", "data": {"session_id": "sid-w"}}]
        st = run(busy + [{**e, "t": t + i + 1} for i, e in enumerate(more)])[0]
        core = [m["text"] for m in st["messages"].values() if m["session"] == "w" and m.get("origin") == forms.ORIGIN_CORE]
        self.assertEqual(len(core), 1)
        self.assertIn("Connected: stripe", core[0])

    def test_empty_turn_is_not_an_answer(self):
        st = self.go({"type": "turn.started", "entity": "w", "data": {"marker": "m15x2"}},
                     {"type": "turn.result", "entity": "w", "data": {"text": "  "}})
        self.assertEqual(st["delegations"]["d11"]["state"], "working")
        st = self.go({"type": "turn.started", "entity": "w", "data": {"marker": "m15x2"}},
                     {"type": "turn.result", "entity": "w", "data": {"text": ""}},
                     {"type": "turn.started", "entity": "w", "data": {"marker": None}},
                     {"type": "turn.result", "entity": "w", "data": {"text": "The google reply came yesterday."}})
        d = st["delegations"]["d11"]
        self.assertEqual((d["state"], d["reply"]), ("replied", "The google reply came yesterday."))


if __name__ == "__main__":
    unittest.main()
