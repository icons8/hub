"""A message to a busy session goes at once or into the queue, as the urgency model decides (all
messages, the owner's too, go through it). Before, everything waited for the end of the turn. Probe: a line in
stdin in the middle of a turn reaches the model right away, after the current step, like Claude Code sockets. The
watchdog asks the urgency model about each message in a busy session's queue; "yes" is a message.inject fact, and the core types it
now. Delegations, "hand in your work", the schedule and core service messages stay in the queue only: they hold at
the turn boundary."""
import tests  # noqa: F401 — keeps the machine's settings out
import json
import os
import re
import tempfile
import unittest

from core import forms, langdata, reducer, spool
from modules.watch import watch

# the owner's short status question in Russian — the first form of the data's first alternative ("what are you doing")
DOING = re.sub(r"\([^()]*\)\?\s*", "", langdata.get("ru", "watch", "status_q").split("|")[0])

OWNER = {"channel": "web", "owner": True}


class Core(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.acts = reducer.empty(), 0, []
        self.ev("session.requested", "hub", cwd="/h", session_id="s-h", kind="hub", **OWNER)
        self.ev("session.requested", "a", cwd="/w", session_id="s-a", **OWNER)
        for n in ("hub", "a"):
            self.ev("session.started", n)
        self.ev("turn.started", "a", marker=None, prompt_head="long work")

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 100 + self.seq, "type": type,
                                                      "entity": entity, "data": data})
        self.acts += acts
        return why

    def msg(self, text, **extra):
        self.ev("message.received", None, to="a", text=text, **{**OWNER, **extra})
        return f"m{self.seq}"

    def test_injected_now_and_counted_delivered(self):
        mid = self.msg("stop, do not touch prod")
        self.assertEqual(self.st["messages"][mid]["state"], "queued")
        self.acts.clear()
        self.assertIsNone(self.ev("message.inject", mid, channel="cli", owner=True, src="watch"))
        m = self.st["messages"][mid]
        self.assertEqual((m["state"], m.get("injected")), ("delivered", True))
        (typed,) = [a for a in self.acts if a["do"] == "type"]
        self.assertIn(f"[hub:{mid}", typed["text"])
        self.assertIn("came in the middle of your turn", typed["text"])
        self.ev("turn.result", "a", text="ok")             # end of the turn: not typed again
        self.assertEqual([a for a in self.acts if a["do"] == "type" and a["message"] == mid], [typed])

    def test_turn_bound_messages_stay_queued(self):
        self.ev("delegation.created", None, session="a", context="task", channel="session", owner=False,
                sender="hub")
        (task,) = [m for m in self.st["messages"].values() if m.get("delegation")]
        self.assertIn("only through the queue", self.ev("message.inject", task["id"], channel="cli", owner=True) or "")
        self.assertEqual(self.st["messages"][task["id"]]["state"], "queued")

    def test_urgency_is_shown_and_the_owner_can_push_it_himself(self):
        # show an urgency probability in the queue and allow a click to push a message through
        from core import views
        mid = self.msg("look at the README later")
        self.assertIsNone(self.ev("message.weighed", mid, p=0.13, channel="cli", owner=True))
        r = views.Reader()
        r.state = self.st
        m = r.snapshot()["messages"][mid]
        self.assertEqual((m["state"], m["urgency"], m["injectable"]), ("queued", 0.13, True))
        self.assertIsNone(self.ev("message.inject", mid, channel="web", owner=True))   # the owner's click
        self.assertTrue(self.st["messages"][mid]["injected"])

    def test_only_a_busy_session_and_not_from_a_session(self):
        mid = self.msg("x")
        self.assertIsNotNone(self.ev("message.inject", mid, channel="session", owner=False, sender="a"))
        self.ev("turn.result", "a", text="ok")
        self.assertIsNotNone(self.ev("message.inject", mid, channel="cli", owner=True))   # no longer in the queue

    def test_owner_can_remove_a_queued_message(self):
        # three voice messages in a row were recognized badly; remove the extras from the queue without waiting for the turn
        mid = self.msg("One-two-three. One-two-three.")
        self.assertIsNone(self.ev("message.cancel", mid, channel="web", owner=True))
        m = self.st["messages"][mid]
        self.assertEqual((m["state"], m["reason"]), ("failed", "removed by owner"))
        self.assertEqual([a for a in self.acts if a.get("message") == mid], [])  # never typed

    def test_turn_bound_messages_cannot_be_removed(self):
        self.ev("delegation.created", None, session="a", context="task", channel="session", owner=False,
                sender="hub")
        (task,) = [m for m in self.st["messages"].values() if m.get("delegation")]
        self.assertIn("a task, a handover", self.ev("message.cancel", task["id"], channel="web", owner=True) or "")
        self.assertEqual(self.st["messages"][task["id"]]["state"], "queued")

    def test_a_schedule_copy_can_be_removed_and_the_job_stays(self):
        # "Remove" on schedule reminders in the queue was rejected silently by the core; 15 copies hung on a busy session
        from core import views
        self.ev("schedule.set", "a-check", session="a", at=150, prompt="check CI", channel="cli", owner=True)
        self.ev("schedule.due", "a-check", slot=150)
        (copy,) = [m for m in self.st["messages"].values() if m.get("schedule") == "a-check"]
        r = views.Reader()
        r.state = self.st
        self.assertTrue(r.snapshot()["messages"][copy["id"]]["removable"])
        self.assertIsNone(self.ev("message.cancel", copy["id"], channel="web", owner=True))
        self.assertEqual(self.st["messages"][copy["id"]]["state"], "failed")
        self.assertIn("a-check", self.st["schedule"])

    def test_a_task_is_not_offered_for_removal(self):
        from core import views
        self.ev("delegation.created", None, session="a", context="task", channel="session", owner=False,
                sender="hub")
        (task,) = [m for m in self.st["messages"].values() if m.get("delegation")]
        r = views.Reader()
        r.state = self.st
        self.assertFalse(r.snapshot()["messages"][task["id"]]["removable"])

    def test_remove_not_from_a_session_and_not_twice(self):
        mid = self.msg("x")
        self.assertIsNotNone(self.ev("message.cancel", mid, channel="session", owner=False, sender="a"))
        self.assertEqual(self.st["messages"][mid]["state"], "queued")
        self.assertIsNone(self.ev("message.cancel", mid, channel="web", owner=True))
        self.assertIsNotNone(self.ev("message.cancel", mid, channel="web", owner=True))  # no longer in the queue


class Watcher(unittest.TestCase):
    def test_asks_the_model_once_per_queued_message_of_a_busy_session(self):
        state = tempfile.mkdtemp(prefix="inject-")
        with open(os.path.join(state, "journal.jsonl"), "w") as f:
            for i, (t, e, d) in enumerate([
                    ("session.requested", "a", {"cwd": "/w", "session_id": "s", **OWNER}),
                    ("session.started", "a", {}),
                    ("turn.started", "a", {"marker": None, "prompt_head": "fixing the rollout"}),
                    ("message.received", None, {"to": "a", "text": "stop, the rollout is cancelled", **OWNER}),
                    ("message.received", None, {"to": "a", "text": "look at the README later", **OWNER})], 1):
                f.write(json.dumps({"seq": i, "ts": 100 + i, "type": t, "entity": e, "data": d}, ensure_ascii=False)
                        + "\n")
        asked = []
        w = watch.Watcher(state, {}, ask=lambda t: {"trouble": False, "kind": "ok"}, needs=None,
                          urgent=lambda text: asked.append(text) or (0.9 if "stop" in text else 0.1),
                          recap=lambda text: "")
        w.step(110)
        w.step(111)
        evs = [e for _, e in spool.take(os.path.join(state, "spool"))]
        self.assertEqual([(e["type"], e["entity"]) for e in evs if e["type"] == "message.inject"], [("message.inject", "m4")])
        self.assertEqual(len(asked), 2)                    # once per message
        self.assertIn("fixing the rollout", asked[0])              # the model sees what the addressee is busy with

    def test_service_notes_get_no_urgency_badge(self):
        # "urgency 0%" was shown in the queue on a core service message ("While you were busy..."); that is a rule, not an estimate
        state = tempfile.mkdtemp(prefix="inject-")
        with open(os.path.join(state, "journal.jsonl"), "w") as f:
            for i, (t, e, d) in enumerate([
                    ("session.requested", "a", {"cwd": "/w", "session_id": "s", **OWNER}),
                    ("session.started", "a", {}),
                    ("turn.started", "a", {"marker": None, "prompt_head": "fixing the rollout"})], 1):
                f.write(json.dumps({"seq": i, "ts": 100 + i, "type": t, "entity": e, "data": d}, ensure_ascii=False)
                        + "\n")
        asked = []
        w = watch.Watcher(state, {}, ask=lambda t: {"trouble": False, "kind": "ok"}, needs=None,
                          urgent=lambda text: asked.append(text) or 0.1, recap=lambda text: "")
        w.step(110)
        spool.take(os.path.join(state, "spool"))
        for mid, origin, text in [("m90x6", forms.ORIGIN_CORE, "While you were busy, m88 (\"…\") went to the executor"),
                                  ("m91", forms.ORIGIN_OWNER, f"{DOING}?")]:
            w.state["messages"][mid] = {"id": mid, "session": "a", "state": "queued", "origin": origin, "text": text}
        w.inject(111)
        evs = [(e["type"], e["entity"], e["data"]["p"]) for _, e in spool.take(os.path.join(state, "spool"))]
        self.assertEqual(evs, [("message.weighed", "m91", 1.0), ("message.inject", "m91", 1.0)])
        self.assertEqual(asked, [])


if __name__ == "__main__":
    unittest.main()
