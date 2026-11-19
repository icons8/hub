"""The owner's rules for all sessions (hub.json `rules`) go to the hub-development agent for review (owner decision
2026-10-08: it should analyse these rules, say what they may interfere with, and explain that the hub's current rules
are tuned through evals while personal ones can, and most likely do, break them). The core sends it the rules when
they appear or change, once per set."""
import tests  # noqa: F401 — ignore the machine's settings
import tempfile
import unittest
from unittest import mock

from core import news, reducer, system
from core.daemon import Hub


class Review(unittest.TestCase):
    def test_new_rules_go_to_agent_dev_once(self):
        st = reducer.empty()
        out = system.rules_review(st, ["To people in chat — only through the messenger agent"], "agent-dev")
        self.assertEqual([t for t, _, _ in out], ["message.received", "rules.reviewed"])
        msg = out[0][2]
        self.assertEqual(msg["to"], "agent-dev")
        self.assertIn("To people in chat — only through the messenger agent", msg["text"])
        self.assertIn("evals", msg["text"])
        for t, e, d in out:
            st, _, why = reducer.reduce(st, {"seq": 1, "ts": 1, "src": "core", "type": t, "entity": e, "data": d})
            self.assertIsNone(why)
        self.assertEqual(system.rules_review(st, ["To people in chat — only through the messenger agent"], "agent-dev"), [])
        self.assertEqual(len(system.rules_review(st, ["another rule"], "agent-dev")), 2)   # changed: again

    def test_no_rules_no_review_and_removal_is_quiet(self):
        st = reducer.empty()
        self.assertEqual(system.rules_review(st, [], "agent-dev"), [])
        st["rules_reviewed"] = "abc"
        self.assertEqual([t for t, _, _ in system.rules_review(st, ["  "], "agent-dev")], ["rules.reviewed"])

    def test_only_the_core_records_it(self):
        st = reducer.empty()
        _, _, why = reducer.reduce(st, {"seq": 1, "ts": 1, "src": "cli", "type": "rules.reviewed", "entity": None,
                                        "data": {"digest": "x"}})
        self.assertIsNotNone(why)

    def test_the_daemon_sends_them_from_hub_json(self):
        h = Hub(tempfile.mkdtemp(prefix="rules-d-"), type("H", (), {"alive": lambda s, n: True})(),
                registry_dir=tempfile.mkdtemp())
        h.config = {**h.config, "rules": ["A rule for everyone"]}
        sent = []
        h.record = lambda t, e=None, d=None, **k: sent.append((t, e, d))
        with mock.patch.object(system, "off", return_value=False), \
                mock.patch.object(system, "jobs", return_value=[]):
            h.ensure_jobs()
        self.assertIn("message.received", [t for t, _, _ in sent])


OWNER = {"channel": "web", "owner": True}


class Sessions(unittest.TestCase):
    """Rules are read when the claude process starts: a running session would live by the old ones until restarted
    (issue #261). The core attaches the new set to its next message, once per set, like the news section for sessions (core/news.py);
    no restart and no separate turn."""

    def setUp(self):
        self.st, self.seq, self.actions = reducer.empty(), 0, []
        for n, kind in (("hub", "hub"), ("maps", "work"), ("docs", "work")):
            self.ev("session.requested", n, cwd="/w", session_id=f"s-{n}", kind=kind, **OWNER)
            self.ev("session.started", n)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 1000 + self.seq, "type": type,
                                                      "entity": entity, "data": data})
        self.actions += acts
        return why

    def rules(self, lines):
        for t, e, d in system.rules_review(self.st, lines, None):
            self.assertIsNone(self.ev(t, e, **d))

    def typed(self, name):
        out = [a["text"] for a in self.actions if a.get("do") == "type" and a["session"] == name]
        self.actions = []
        return out

    def say(self, name, text="how are you?"):
        self.ev("message.received", None, to=name, text=text, **OWNER)
        (t,) = self.typed(name)
        self.ev("turn.started", name, marker=f"m{self.seq}", prompt_head="x")
        self.ev("turn.stopped", name)
        return t

    def test_each_live_session_gets_the_new_rules_once(self):
        self.rules(["To people — only through the messenger agent"])
        for name in ("maps", "hub"):
            t = self.say(name)
            self.assertIn("To people — only through the messenger agent", t)
            self.assertNotIn("messenger", self.say(name))          # not the second time
        self.assertEqual(self.st["messages"][f"m{self.seq - 2}"]["text"], "how are you?")   # the owner's words stay as they were

    def test_only_the_latest_set_while_it_was_away(self):
        self.rules(["first rule"])
        self.rules(["second rule"])
        t = self.say("docs")
        self.assertIn("second rule", t)
        self.assertNotIn("first rule", t)
        self.rules(["first rule"])                              # the old set was restored: shown again, as it is a change
        self.assertIn("first rule", self.say("docs"))
        self.assertNotIn("rule", self.say("docs"))

    def test_removed_rules_are_said_too(self):
        self.rules(["rule"])
        self.say("maps")
        self.rules([])
        t = self.say("maps")
        self.assertIn(news.RULES_REMOVED, t)
        self.assertNotIn("- rule", t)

    def test_nobody_is_woken_and_nothing_is_sent_by_itself(self):
        self.ev("session.parked", "docs", channel="core", why="idle")
        before, state = dict(self.st["messages"]), self.st["sessions"]["docs"]["state"]
        self.actions = []
        self.rules(["rule"])
        self.assertEqual(self.st["messages"], before)
        self.assertEqual(self.actions, [])
        self.assertEqual(self.st["sessions"]["docs"]["state"], state)

    def test_a_new_session_starts_with_todays_rules(self):
        self.rules(["rule"])
        self.ev("session.requested", "atlas", cwd="/a", session_id="s-a", **OWNER)
        self.ev("session.started", "atlas")
        self.assertNotIn("rule", self.say("atlas"))

    def test_a_revived_conversation_still_gets_them(self):
        # without --system-prompt-snapshot, claude on --resume takes the prompt of the first request, with the old rules
        self.ev("session.ended", "docs", reason="prompt_input_exit")
        self.assertIsNone(self.ev("session.remove_requested", "docs", **OWNER))
        self.rules(["rule"])
        self.assertIsNone(self.ev("session.requested", "docs", cwd="/w", session_id="s-docs", revive=True, **OWNER))
        self.ev("session.started", "docs")
        self.assertIn("- rule", self.say("docs"))

    def test_old_journals_without_the_set_attach_nothing(self):
        self.assertIsNone(self.ev("rules.reviewed", None, digest="abc", channel="core"))
        said = self.say("maps")
        self.assertNotIn(news.RULES_CHANGED, said)
        self.assertNotIn(news.RULES_REMOVED, said)

    def test_changes_notes_still_come_alongside(self):
        self.rules(["rule"])
        self.ev("news.loaded", None, notes=["`hub x` new"], channel="core", owner=False)
        t = self.say("maps")
        self.assertIn("`hub x` new", t)
        self.assertIn("- rule", t)


if __name__ == "__main__":
    unittest.main()
