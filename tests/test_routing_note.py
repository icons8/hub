"""A message to an executor that has handed in (is asleep) or has left goes to whoever started it, with a "how to
wake it" hint and its previous result. The hint is a service note from the core for the recipient, not the sender's
words: the message text (feed, quote, edit, reply to it) keeps the sender's words as they are; the recipient sees the
note in the delivered input as a separate block, "Service note from the core"."""
import tests  # noqa: F401 — hide the machine's settings
import json
import pathlib
import unittest

from core import forms, reducer, views

OWNER = {"channel": "web", "owner": True}
MAPS = {"channel": "session", "owner": False, "sender": "maps"}


class RoutingNote(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.acts = reducer.empty(), 0, []
        self.ev("session.requested", "hub", cwd="/h", session_id="s-h", kind="hub", **OWNER)
        self.ev("session.requested", "maps", cwd="/m", session_id="s-m", role="agent", about="Maps", **OWNER)
        self.ev("session.requested", "devops", cwd="/k", session_id="s-d", role="agent", about="DevOps", **OWNER)
        for n in ("hub", "maps", "devops"):
            self.ev("session.started", n)
        self.st["sessions"]["devops"].update(role="executor", raised_by="maps")
        self.ev("delegation.created", None, session="devops", context="fix the feed", for_session="maps", **MAPS)
        (d,) = self.st["delegations"].values()
        self.ev("turn.started", "devops", marker=d["message"], prompt_head="…")
        self.ev("turn.result", "devops", text="the feed is fixed")
        self.st["sessions"]["devops"].update(state="retired")   # handed in and fell asleep

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 1000 + self.seq, "type": type,
                                                      "entity": entity, "data": data})
        self.acts += acts
        return why

    def typed(self, mid):
        return [a for a in self.acts if a["do"] == "type" and a["message"] == mid]

    def test_owner_words_stay_his_and_the_raiser_gets_the_note_separately(self):
        self.assertIsNone(self.ev("message.received", None, to="devops", text="check the phone too", **OWNER))
        m = self.st["messages"][f"m{self.seq}"]
        self.assertEqual((m["session"], m["text"]), ("maps", "check the phone too"))
        self.assertIn("handed in its task and is asleep", m["routing_note"])
        self.assertIn("the feed is fixed", m["routing_note"])                       # its previous result is in the note
        (row,) = [it for _, it in views._items(self.st, {"seq": self.seq, "ts": 1000 + self.seq,
                                                          "type": "message.received", "entity": None,
                                                          "data": {"to": "devops", "text": "x", **OWNER}})
                  if it["kind"] == "in"]
        self.assertEqual(row["text"], "check the phone too")                    # the feed shows the sender's words
        acts = []
        reducer._typed(dict(m), 3000, acts)                                       # what maps will get when typed
        (act,) = acts
        words, note = act["text"].split("The core's service note:")
        self.assertIn("check the phone too", words)
        self.assertNotIn("hub session wake", words)
        self.assertIn("hub session wake devops", note)

    def test_core_message_to_the_sleeper_keeps_its_text(self):
        reducer.new_message(self.st, "m9999", "devops", "check result: ok", 2000, origin=forms.ORIGIN_CORE)
        m = self.st["messages"]["m9999"]
        self.assertEqual((m["session"], m["text"]), ("maps", "check result: ok"))
        self.assertIn("For executor devops", m["routing_note"])

    def test_fyi_that_waited_for_a_carrier_keeps_its_text(self):
        self.st["sessions"]["devops"].update(state="idle")
        self.ev("message.received", None, to="devops", text="the build is green", fyi=True, channel="session",
                owner=False, sender="hub")
        fyi = self.st["messages"][f"m{self.seq}"]
        self.st["sessions"]["devops"].update(state="retired")
        self.ev("owner.dismissed", "devops", **OWNER)
        m = self.st["messages"][fyi["id"]]
        self.assertEqual((m["session"], m["text"]), ("maps", "the build is green"))
        self.assertEqual(m["routing_note"], "for devops — it is asleep")           # one line: no result, no wake hint

    def test_an_fyi_that_waited_is_not_sent_back_to_the_sender_who_raised_the_executor(self):
        # accepted while the executor worked, then it fell asleep: "to whoever raised it" would be the sender's own queue
        self.st["sessions"]["devops"].update(state="idle")
        self.ev("message.received", None, to="devops", text="the build is green", fyi=True, **MAPS)
        fyi = self.st["messages"][f"m{self.seq}"]
        self.assertEqual((fyi["session"], fyi["state"]), ("devops", "aside"))
        self.st["sessions"]["devops"].update(state="retired")
        self.ev("owner.dismissed", "devops", **OWNER)
        m = self.st["messages"][fyi["id"]]
        self.assertEqual((m["session"], m["state"]), ("devops", "failed"))
        self.assertIn("raised", m["reason"])
        self.assertEqual([x for x in self.st["messages"].values()
                          if x["session"] == "maps" and "the build is green" in x["text"]], [])

    def test_an_fyi_from_the_hub_that_raised_the_executor_is_withdrawn_too(self):
        self.st["sessions"]["devops"].update(state="idle", raised_by="hub")
        self.ev("message.received", None, to="devops", text="restarted", fyi=True, channel="session", owner=False,
                sender="hub")
        self.st["sessions"]["devops"].update(state="retired")
        self.ev("owner.dismissed", "devops", **OWNER)
        (m,) = [x for x in self.st["messages"].values() if x["text"] == "restarted"]
        self.assertEqual((m["session"], m["state"]), ("devops", "failed"))

    def test_a_dropped_fyi_is_the_only_one_dropped(self):
        # the same FYI from another sender is still passed to the raiser (see the test above for a carrier)
        self.st["sessions"]["devops"].update(state="idle")
        self.ev("message.received", None, to="devops", text="a", fyi=True, **MAPS)
        self.ev("message.received", None, to="devops", text="b", fyi=True, channel="session", owner=False, sender="hub")
        self.st["sessions"]["devops"].update(state="retired")
        self.ev("owner.dismissed", "devops", **OWNER)
        a, b = [m for m in self.st["messages"].values() if m["text"] in ("a", "b")]
        self.assertEqual((a["state"], b["session"], b["state"]), ("failed", "maps", "aside"))

    def test_a_routed_fyi_riding_with_another_message_keeps_its_note(self):
        # "for your information" rides at the end of the input of someone else's message (_ride); the note rides too,
        # otherwise the recipient does not know the message was not addressed to it
        self.ev("message.received", None, to="devops", text="the build is green", fyi=True, channel="session",
                owner=False, sender="hub")
        (fyi,) = [m for m in self.st["messages"].values() if m["text"] == "the build is green"]
        self.assertEqual((fyi["session"], fyi["state"]), ("maps", "aside"))
        for m in [m for m in self.st["messages"].values() if m["session"] == "maps" and m["state"] == "typing"]:
            self.ev("turn.started", "maps", marker=m["id"], prompt_head="…")   # the executor's result comes before ours
            self.ev("turn.result", "maps", text="accepted")
            self.ev("turn.stopped", "maps")
        self.acts.clear()
        self.ev("message.received", None, to="maps", text="what about the maps?", **OWNER)
        (act,) = [a for a in self.acts if a["do"] == "type"]
        self.assertIn("the build is green", act["text"])
        self.assertIn("for devops — it is asleep", act["text"])
        self.assertNotIn("the feed is fixed", act["text"])                           # its result is not retold in an FYI
        self.assertNotIn("hub session wake", act["text"])

    def test_fyi_to_the_sleeper_gets_a_one_line_note(self):
        self.ev("message.received", None, to="devops", text="the build is green", fyi=True, channel="session",
                owner=False, sender="hub")
        m = self.st["messages"][f"m{self.seq}"]
        self.assertEqual((m["session"], m["state"], m["text"]), ("maps", "aside", "the build is green"))
        self.assertEqual(m["routing_note"], "for devops — it is asleep")

    def test_a_plain_message_to_the_sleeper_keeps_the_long_note(self):
        self.ev("message.received", None, to="devops", text="the build is green", channel="session", owner=False,
                sender="hub")
        m = self.st["messages"][f"m{self.seq}"]
        self.assertIn("This was written to executor devops — it has handed in its task and is asleep", m["routing_note"])
        self.assertIn("the feed is fixed", m["routing_note"])
        self.assertIn('hub session wake devops "<what to do>"', m["routing_note"])

    def test_the_raiser_writing_to_its_own_sleeper_is_refused_with_the_wake_hint(self):
        # the message would go "to whoever raised it" = back into the sender's own queue: the sleeper gets nothing
        before = len(self.st["messages"])
        why = self.ev("message.received", None, to="devops", text="also check the phone", **MAPS)
        self.assertIn("hub session wake devops", why)
        self.assertIn("--about d", why)
        self.assertIn("asleep", why)
        self.assertEqual(len(self.st["messages"]), before)
        self.assertNotIn("also check the phone", " ".join(m["text"] for m in self.st["messages"].values()))

    def test_the_hint_names_the_task_of_the_sleeper(self):
        (d,) = self.st["delegations"].values()
        why = self.ev("message.received", None, to="devops", text="x", **MAPS)
        self.assertIn(f"hub session wake devops --about {d['id']}", why)

    def test_fyi_from_the_raiser_to_its_own_sleeper_is_refused_too(self):
        # an FYI back to the sender's own queue ("for devops — it is asleep") would tell it nothing it does not know
        before = len(self.st["messages"])
        why = self.ev("message.received", None, to="devops", text="the build is green", fyi=True, **MAPS)
        self.assertIn("hub session wake devops", why)
        self.assertEqual(len(self.st["messages"]), before)

    def test_the_hub_that_raised_the_sleeper_is_refused_as_well(self):
        self.st["sessions"]["devops"].update(raised_by="hub")
        why = self.ev("message.received", None, to="devops", text="x", channel="session", owner=False, sender="hub")
        self.assertIn("hub session wake devops", why)

    def test_the_owner_and_other_sessions_still_reach_the_raiser(self):
        self.assertIsNone(self.ev("message.received", None, to="devops", text="x", **OWNER))
        self.assertEqual(self.st["messages"][f"m{self.seq}"]["session"], "maps")
        self.assertIsNone(self.ev("message.received", None, to="devops", text="y", channel="session", owner=False,
                                  sender="hub"))
        self.assertEqual(self.st["messages"][f"m{self.seq}"]["session"], "maps")

    def test_the_raiser_writing_to_its_removed_executor_is_refused_not_sent_back_to_itself(self):
        # "a message to a departed executor goes to whoever raised it": from the raiser that is its own queue, and the
        # executor gets nothing. As with the sleeper (SEND_OWN_SLEEPER), the sender is told why
        self.st["sessions"]["devops"]["state"] = "removed"
        for extra in ({}, {"fyi": True}):
            with self.subTest(extra=extra):
                before = len(self.st["messages"])
                why = self.ev("message.received", None, to="devops", text="also check the phone", **MAPS, **extra)
                self.assertIn("devops has left (removed)", why or "")
                self.assertEqual(len(self.st["messages"]), before)

    def test_the_refusal_names_the_next_step_in_every_language(self):
        # the raiser is told what to do instead: bring the same executor back, or raise a new one. The commands are
        # not translated, so the check does not depend on the language of the output
        self.st["sessions"]["devops"]["state"] = "removed"
        why = self.ev("message.received", None, to="devops", text="x", **MAPS)
        self.assertIn("hub session revive devops", why or "")
        self.assertIn("hub task run", why or "")
        for path in sorted(pathlib.Path(__file__).resolve().parents[1].glob("i18n/*.json")):
            if path.name in ("sync.json", "pending.json", "stage.json", "glossary.json"):
                continue
            text = json.loads(path.read_text(encoding="utf-8")).get("core.reducer.send_self_gone")
            if text is not None:
                with self.subTest(language=path.stem):
                    self.assertIn("hub session revive {name}", text)
                    self.assertIn("hub task run", text)

    def test_the_hub_that_raised_the_removed_executor_is_refused_as_well(self):
        self.st["sessions"]["devops"].update(state="removed", raised_by="hub")
        why = self.ev("message.received", None, to="devops", text="x", channel="session", owner=False, sender="hub")
        self.assertIn("you raised it", why or "")

    def test_the_owner_and_other_sessions_still_reach_the_raiser_of_a_removed_executor(self):
        self.st["sessions"]["devops"]["state"] = "removed"
        self.assertIsNone(self.ev("message.received", None, to="devops", text="x", **OWNER))
        m = self.st["messages"][f"m{self.seq}"]
        self.assertEqual(m["session"], "maps")
        self.assertIn("it has already left", m.get("routing_note") or "")
        self.assertIsNone(self.ev("message.received", None, to="devops", text="y", channel="session", owner=False,
                                  sender="hub"))
        self.assertEqual(self.st["messages"][f"m{self.seq}"]["session"], "maps")


if __name__ == "__main__":
    unittest.main()
