"""A card to the owner that went stale (owner.ASK_TTL) with no answer wakes the session that asked it, once
(core/askexpired.py, the daemon step revive_asks, the fact ask.expired). Before this, a stale card only left the screen: the
session had ended its turn (a card does not hold it) and slept, so the question was seen by nobody and nobody could ask it
again."""
import tests  # noqa: F401 - ignore the machine's settings
import inspect
import tempfile
import types
import unittest
from unittest import mock

from core import askexpired, owner, reducer
from core.daemon import Hub

OWNER = {"channel": "web", "owner": True}
CORE = {"channel": "core"}
T0 = 10_000
LATE = T0 + owner.ASK_TTL + 1000


def form(text):
    return {"questions": [{"question": text, "header": "Question", "options": [{"label": "Yes"}, {"label": "No"}]}]}


class Folding:
    """A fold with helpers (not a TestCase: a subclass does not pull in others' tests)."""

    def setUp(self):
        self.st, self.seq, self.ts = reducer.empty(), 0, T0
        for name, kind in (("hub", "hub"), ("maps", None), ("docs", None)):
            self.ev("session.requested", name, cwd=f"/{name}", session_id=f"s-{name}", **({"kind": kind} if kind else {}),
                    **OWNER)
            self.ev("session.started", name)

    def ev(self, type, entity=None, ts=None, **data):
        self.seq += 1
        self.ts = ts or self.ts + 1
        self.st, self.acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": self.ts, "type": type, "entity": entity,
                                                           "data": data})
        return why

    def ask(self, name="maps", text="Connect the notes module?", rid=None, ts=None):
        """The session asks with a form and ends its turn: the card is open, the session is free."""
        rid = rid or f"r{self.seq}"
        self.ev("turn.started", name, ts=ts, marker=None, prompt_head="x")
        self.ev("permission.requested", name, request_id=rid, tool="AskUserQuestion", interactive=True, input=form(text))
        self.ev("turn.result", name, text="asked")
        self.ev("turn.stopped", name)
        return self.st["sessions"][name]["asks"][-1]["id"]

    def park(self, name="maps"):
        self.assertIsNone(self.ev("session.parked", name, **CORE, why="idle"))
        self.assertEqual(self.st["sessions"][name]["state"], "parked")

    def sess(self, name="maps"):
        return self.st["sessions"][name]

    def texts(self, name="maps"):
        return [m["text"] for m in self.st["messages"].values() if m["session"] == name and "have gone stale" in m["text"]]

    def fire(self, name="maps", ids=None, ts=LATE, **extra):
        ids = ids or [a["id"] for a in self.sess(name)["asks"]]
        return self.ev("ask.expired", name, ts=ts, **CORE, asks=ids, **extra)


class Due(Folding, unittest.TestCase):
    def test_nothing_is_due_until_the_card_is_a_day_old(self):
        q = self.ask(ts=T0)
        self.assertEqual(askexpired.due(self.st, T0 + owner.ASK_TTL - 1), [])
        self.assertEqual(askexpired.due(self.st, T0 + owner.ASK_TTL + 1), [("maps", [q])])

    def test_a_card_older_than_the_ceiling_is_never_raised(self):
        # cards stay in asks forever: without a ceiling the first tick after an update would raise month-old questions
        self.assertEqual(askexpired.MAX_AGE, 7 * 24 * 3600)
        old = self.ask("maps", "Old question?", ts=T0)
        since = owner.ask_since(self.sess()["asks"][-1])
        self.assertEqual(askexpired.due(self.st, since + askexpired.MAX_AGE - 1), [("maps", [old])])
        self.assertEqual(askexpired.due(self.st, since + askexpired.MAX_AGE), [])
        self.assertEqual(askexpired.due(self.st, T0 + 30 * 24 * 3600), [])
        fresh = self.ask("maps", "Fresh question?", ts=T0 + 30 * 24 * 3600)
        at = T0 + 31 * 24 * 3600 + 10
        self.assertEqual(askexpired.due(self.st, at), [("maps", [fresh])])
        self.assertIsNone(self.fire(ids=[old, fresh], ts=at))
        (text,) = self.texts()
        self.assertIn("Fresh question?", text)
        self.assertNotIn("Old question?", text)

    def test_the_card_of_a_parked_session_that_nobody_answered_is_due(self):
        q = self.ask()
        self.park()
        self.assertEqual(askexpired.due(self.st, LATE), [("maps", [q])])

    def test_states_that_are_woken_and_states_that_are_not(self):
        for n, state in (("idle", "idle"), ("parked", "parked"), ("busy", "busy"), ("dialog", "dialog"),
                         ("starting", "starting"), ("asleep", "asleep"), ("retired", "retired"), ("held", "held"),
                         ("closed", "closed"), ("failed", "failed"), ("removed", "removed")):
            self.ev("session.requested", n, cwd=f"/{n}", session_id=f"s-{n}", **OWNER)
            self.ev("session.started", n)
            self.ask(n)
            self.sess(n)["state"] = state
        woken = sorted(n for n, _ in askexpired.due(self.st, LATE, step=99) if n not in ("maps", "docs"))
        self.assertEqual(woken, ["idle", "parked"])

    def test_an_answered_dismissed_or_withdrawn_card_is_not_due(self):
        a, b, c = self.ask("maps", rid="a"), self.ask("maps", "Second?", rid="b"), self.ask("maps", "Third?", rid="c")
        self.assertIsNone(self.ev("permission.answered", "maps", request_id="a", behavior="allow",
                                  answers={"Connect the notes module?": "Yes"}, **OWNER))
        self.assertIsNone(self.ev("question.dismissed", "maps", request_id="b", **OWNER))
        self.assertIsNone(self.ev("question.withdrawn", "maps", id=c, sender="maps", channel="session"))
        self.assertEqual(askexpired.due(self.st, LATE), [])

    def test_a_session_that_has_asked_again_is_not_woken(self):
        self.ask(ts=T0)
        self.ask("maps", "Connect the notes module? Now with context", ts=LATE - 100)
        self.assertEqual(askexpired.due(self.st, LATE), [])

    def test_a_session_with_something_in_flight_is_left_alone(self):
        for what in ("pending", "background", "limit_cut", "net_cut"):
            name = f"x-{what}"
            self.ev("session.requested", name, cwd=f"/{name}", session_id=f"s-{name}", **OWNER)
            self.ev("session.started", name)
            self.ask(name)
            self.sess(name)[what] = {} if what != "pending" else [{"request_id": "p", "tool": "Bash", "input": {}}]
            if what == "background":
                self.sess(name)[what] = {"b1": {"id": "b1", "title": "build"}}
            if what in ("limit_cut", "net_cut"):
                self.sess(name)[what] = {"ts": T0, "account": None}
        self.assertNotIn("x-pending", dict(askexpired.due(self.st, LATE, step=99)))
        self.assertNotIn("x-background", dict(askexpired.due(self.st, LATE, step=99)))
        self.assertNotIn("x-limit_cut", dict(askexpired.due(self.st, LATE, step=99)))
        self.assertNotIn("x-net_cut", dict(askexpired.due(self.st, LATE, step=99)))

    def test_an_open_tool_request_is_asking_already(self):
        # review 29.10: another question to the owner is still open - no "he never answered" next to it
        self.ask()
        self.sess()["requests"] = [{"tool": "notes", "state": "needs-owner", "since": LATE - 10}]
        self.assertEqual(askexpired.due(self.st, LATE), [])
        self.sess()["requests"][0]["state"] = "granted"
        self.assertEqual(len(askexpired.due(self.st, LATE)), 1)

    def test_a_session_handing_in_is_left_alone(self):
        self.ask()
        for closing in ("handoff", "ready"):   # handing over its work before sleep (machines.CLOSING)
            self.sess()["closing"] = closing
            self.assertEqual(askexpired.due(self.st, LATE), [], closing)
        self.sess()["closing"] = "none"
        self.assertEqual(len(askexpired.due(self.st, LATE)), 1)

    def test_a_closed_tasks_card_does_not_hide_the_same_question_open_on_another_task(self):
        # review 29.10: one card per question was picked before the task check - the closed one hid the open one
        a = self.ask(text="Which document do we upload?", ts=T0)
        b = self.ask(text="Which document do we upload?", ts=T0 + 5)
        self.sess()["asks"][0]["delegation"], self.sess()["asks"][1]["delegation"] = "d1", "d2"
        self.st["delegations"]["d1"] = {"id": "d1", "state": "replied", "session": "maps"}
        self.st["delegations"]["d2"] = {"id": "d2", "state": "waiting-owner", "session": "maps"}
        self.assertEqual(askexpired.due(self.st, LATE), [("maps", [b])])
        self.assertIsNone(self.fire(ids=[a, b]))
        (text,) = self.texts()
        self.assertIn(b, text)
        self.assertNotIn(a + " ", text)

    def test_a_hub_bus_card_is_not_the_sessions_question(self):
        # a card of the hub bus is the core's question about a contact's message: asked again with a form it would
        # lose the way the owner's "Send" goes (review 29.10)
        self.ask()
        self.sess()["asks"][-1]["hubbus"] = {"contact": "c1"}
        self.assertEqual(askexpired.due(self.st, LATE), [])
        self.assertEqual(askexpired.stale(self.sess(), LATE, self.st["delegations"]), [])

    def test_a_queued_message_is_not_a_reason_to_write_a_second_one(self):
        self.ask()
        self.ev("message.received", None, to="maps", text="hello", **OWNER)
        self.sess()["state"] = "idle"
        for m in self.st["messages"].values():
            m["state"] = "queued"
        self.assertEqual(askexpired.due(self.st, LATE), [])

    def test_a_card_whose_task_is_over_is_not_due(self):
        q = self.ask()
        self.sess()["asks"][-1]["delegation"] = "d1"
        self.st["delegations"]["d1"] = {"id": "d1", "session": "maps", "state": "replied"}
        self.assertEqual(askexpired.due(self.st, LATE), [])
        self.st["delegations"]["d1"]["state"] = "waiting-owner"
        self.assertEqual(askexpired.due(self.st, LATE), [("maps", [q])])

    def test_the_owner_away_waits_for_him_and_then_for_the_grace(self):
        q = self.ask()
        self.assertIsNone(self.ev("owner.away", None, ts=T0 + 100, hours=None, **OWNER))
        self.assertEqual(askexpired.due(self.st, LATE), [])
        back = LATE + 10
        self.assertIsNone(self.ev("owner.back", None, ts=back, **OWNER))
        self.assertEqual(askexpired.due(self.st, back + owner.BACK_GRACE - 1), [])
        self.assertEqual(askexpired.due(self.st, back + owner.BACK_GRACE), [("maps", [q])])

    def test_a_card_asked_while_he_was_away_does_not_expire_until_a_day_after_he_is_back(self):
        self.ev("owner.away", None, ts=T0 + 10, hours=None, **OWNER)
        self.ask(ts=T0 + 20)
        self.assertTrue(self.sess()["asks"][0].get("away"))
        back = T0 + owner.ASK_TTL + 500
        self.ev("owner.back", None, ts=back, **OWNER)
        self.assertEqual(askexpired.due(self.st, back + 100), [])
        self.assertEqual(askexpired.due(self.st, back + owner.ASK_TTL - 1), [])
        self.assertEqual(len(askexpired.due(self.st, back + owner.ASK_TTL + 1)), 1)

    def test_the_night_is_no_reason_to_wait(self):
        self.st["owner"]["tz"] = "UTC"
        for hour in (0, 3, 23):
            at = 86400 * 100 + hour * 3600 + 5
            self.st["sessions"]["maps"]["asks"] = []
            self.ev("turn.started", "maps", ts=at - owner.ASK_TTL, marker=None)
            self.ev("permission.requested", "maps", request_id=f"n{hour}", tool="AskUserQuestion", interactive=True,
                    input=form(f"Night {hour}?"))
            self.ev("turn.result", "maps", text="asked")
            self.ev("turn.stopped", "maps")
            self.sess()["asks"][-1]["since"] = at - owner.ASK_TTL
            self.assertEqual(len(askexpired.due(self.st, at)), 1, hour)

    def test_the_limit_saving_mode_holds_it_back(self):
        q = self.ask()
        for mode, due in (("economy", False), ("owner", False), ("normal", True), (None, True)):
            with mock.patch("core.budget.mode", return_value=mode):
                self.assertEqual(askexpired.due(self.st, LATE), [("maps", [q])] if due else [], mode)

    def test_a_question_that_was_already_revived_is_not_woken_again_however_it_is_asked(self):
        self.ask("maps", "Connect the notes module?", ts=T0)
        self.fire()
        self.ask("maps", "  connect   THE notes   MODULE? ", ts=LATE + 10)
        self.assertEqual(askexpired.due(self.st, LATE + 10 + owner.ASK_TTL + 5), [])
        self.assertEqual(askexpired.due(self.st, LATE + 5), [])

    def test_the_stalest_sessions_first_and_a_few_at_a_time(self):
        for i, ts in enumerate((T0 + 30, T0 + 10, T0 + 20)):
            self.ask(("maps", "docs", "hub")[i], f"Question {i}?", ts=ts)
        names = [n for n, _ in askexpired.due(self.st, LATE, step=99)]
        self.assertEqual(names, ["docs", "hub", "maps"])
        self.assertEqual(len(askexpired.due(self.st, LATE)), askexpired.STEP)
        self.assertEqual(askexpired.STEP, 2)


class Fold(Folding, unittest.TestCase):
    def test_the_fact_writes_one_message_for_the_session_with_all_its_stale_questions(self):
        self.ask("maps", "First question?", ts=T0)
        self.ask("maps", "Second question?", ts=T0 + 5)
        self.park()
        self.assertIsNone(self.fire())
        (text,) = self.texts()
        self.assertIn("First question?", text)
        self.assertIn("Second question?", text)
        self.assertEqual(self.sess()["state"], "starting")
        m = [m for m in self.st["messages"].values() if m["session"] == "maps" and "have gone stale" in m["text"]][0]
        self.assertEqual(m["origin"], reducer.forms.ORIGIN_CORE)

    def test_the_message_dates_a_question_by_when_it_was_asked_not_by_his_return(self):
        self.ev("owner.away", None, ts=T0 + 10, hours=None, **OWNER)
        self.ask(ts=T0 + 20)
        back = T0 + 3 * 86400
        self.ev("owner.back", None, ts=back, **OWNER)
        ask = self.sess()["asks"][0]
        self.assertIsNone(self.fire(ts=back + owner.ASK_TTL + 1))
        (text,) = self.texts()
        self.assertIn("asked " + owner._local(ask["since"], "UTC").strftime("%d.%m %H:%M"), text)
        self.assertNotIn(owner._local(back, "UTC").strftime("%d.%m"), text)

    def test_the_cards_stay_where_they_are(self):
        self.ask()
        self.fire()
        self.assertEqual(len(self.sess()["asks"]), 1)

    def test_a_second_identical_fact_writes_nothing_more(self):
        self.ask()
        self.fire()
        self.fire()
        self.assertEqual(len(self.texts()), 1)

    def test_only_the_core_writes_the_fact(self):
        self.ask()
        ids = [a["id"] for a in self.sess()["asks"]]
        for data in ({"channel": "cli", "owner": True}, {"channel": "session", "sender": "maps"}, {}):
            self.assertIsNotNone(self.ev("ask.expired", "maps", asks=ids, **data))
        self.assertEqual(self.texts(), [])

    def test_a_fact_that_no_longer_holds_changes_nothing(self):
        q = self.ask()
        self.assertIsNone(self.ev("permission.answered", "maps", request_id=self.sess()["asks"][0]["request_id"],
                                  behavior="allow", answers={"Connect the notes module?": "Yes"}, **OWNER))
        self.sess()["asks"] = []
        self.fire("maps", [q])
        self.assertEqual(self.texts(), [])
        self.assertIsNone(self.ev("ask.expired", "nobody", **CORE, asks=["q1"]))
        q2 = self.ask("docs")
        self.sess("docs")["state"] = "asleep"
        self.fire("docs", [q2])
        self.assertEqual(self.texts("docs"), [])
        self.assertEqual(self.sess("docs")["state"], "asleep")

    def test_a_card_that_is_not_yet_stale_is_not_in_the_message(self):
        self.ask("maps", "Old question?", ts=T0)
        self.ask("maps", "Fresh question?", ts=LATE - 3600)
        self.fire()
        (text,) = self.texts()
        self.assertIn("Old question?", text)
        self.assertNotIn("Fresh question?", text)

    def test_the_message_asks_to_ask_again_or_to_stay_silent(self):
        self.ask()
        self.fire()
        (text,) = self.texts()
        self.assertIn("AskUserQuestion", text)
        self.assertIn('"—"', text)
        self.assertIn("Connect the notes module?", text)
        self.assertNotIn("withdr", text)

    def test_the_session_does_not_get_a_second_message_for_the_same_question_asked_again(self):
        self.ask(ts=T0)
        self.fire()
        self.ask(ts=LATE + 100)
        self.fire(ts=LATE + 100 + owner.ASK_TTL + 5)
        self.assertEqual(len(self.texts()), 1)

    def test_the_revived_memory_is_forgotten_by_age_and_never_raises_a_question_twice(self):
        # review 29.10: kept by count (50), the evicted ones of a session with more stale questions were raised again
        # every PAUSE; a question raised more than MAX_AGE ago has no card young enough to be raised again
        for i in range(70):
            self.ask("maps", f"Question number {i}?", ts=T0)
        self.assertIsNone(self.fire())
        self.assertEqual(len(self.sess()["revived"]), 70)
        self.assertEqual(askexpired.due(self.st, LATE + askexpired.PAUSE), [])
        self.assertEqual(askexpired.stale(self.sess(), LATE + 3 * 86400, self.st["delegations"]), [])
        self.ask("maps", "New question?", ts=LATE + askexpired.MAX_AGE)
        self.fire(ts=LATE + askexpired.MAX_AGE + owner.ASK_TTL + 5)
        self.assertEqual(list(self.sess()["revived"]), [askexpired.key(self.sess()["asks"][-1])])

    def test_a_question_asked_again_after_its_raise_is_not_raised_once_the_first_raise_is_old(self):
        # review 29.10: forgetting by age must keep a question whose later card is still young
        self.ask("maps", "Question Q?", ts=T0)
        self.fire()
        self.ask("maps", "Question Q?", ts=T0 + 6 * 86400)
        self.ask("docs", "Another?", ts=T0 + 7 * 86400)
        self.ask("maps", "Another one at maps?", ts=T0 + 7 * 86400)
        self.fire(ts=T0 + 8 * 86400 + 43200)   # a different question of maps: the prune runs, Q raised > 7 days ago
        self.assertIn(askexpired.key(self.sess()["asks"][1]), self.sess()["revived"])
        self.assertEqual([n for n, _ in askexpired.due(self.st, T0 + 8 * 86400 + 43200 + askexpired.PAUSE)], ["docs"])


class Fixture(Folding, unittest.TestCase):
    """The case that started it: a store account page asks for a document, the session answers with the card, goes
    quiet and is parked two hours later; a day on, nobody has seen the question."""

    def test_the_parked_session_is_woken_once_a_day_after_its_last_question(self):
        q = self.ask("maps", "The other side asks for a new document. Which do we upload?", ts=T0)
        self.ev("turn.started", "maps", ts=T0 + 3600, marker=None)
        self.ev("turn.result", "maps", ts=T0 + 3700, text="Waiting for the document, the question is in the card")
        self.ev("turn.stopped", "maps", ts=T0 + 3701)
        self.ev("session.parked", "maps", ts=T0 + 7200, **CORE, why="idle")
        self.assertEqual(askexpired.due(self.st, T0 + owner.ASK_TTL - 1), [])
        (found,) = askexpired.due(self.st, T0 + owner.ASK_TTL + 1)
        self.assertEqual(found, ("maps", [q]))
        self.assertIsNone(self.ev("ask.expired", "maps", ts=T0 + owner.ASK_TTL + 1, **CORE, asks=found[1]))
        self.assertEqual(self.sess()["state"], "starting")
        (text,) = self.texts()
        self.assertIn("Which do we upload?", text)
        self.assertEqual(askexpired.due(self.st, T0 + owner.ASK_TTL + 100), [])


class Tick(unittest.TestCase):
    def hub(self):
        h = Hub(tempfile.mkdtemp(prefix="ask-expired-"), types.SimpleNamespace(poll=lambda: []))
        h.execute = lambda a: None
        self.now = [T0 + 1.0]
        h.clock = lambda: self.now[0]
        return h

    def ask(self, h, name, text="Connect the module?"):
        h.record("session.requested", name, {"cwd": f"/{name}", "session_id": f"s-{name}", **OWNER})
        h.record("session.started", name, {"pid": 1}, src="host")
        h.record("turn.started", name, {"marker": None, "prompt_head": "x"}, src="host")
        h.record("permission.requested", name, {"request_id": f"r-{name}", "tool": "AskUserQuestion", "interactive": True,
                                                "input": form(text)}, src="host")
        h.record("turn.result", name, {"text": "asked", "turns": 1}, src="host")
        h.record("turn.stopped", name, {}, src="host")

    def facts(self, h):
        import os
        from core import journal
        return [e for e in journal.read_tail(os.path.join(h.state_dir, "journal.jsonl")) if e["type"] == "ask.expired"]

    def test_the_step_writes_the_fact_once_after_the_day_and_the_session_gets_the_message(self):
        h = self.hub()
        self.ask(h, "maps")
        h.revive_asks()
        self.assertEqual(self.facts(h), [])
        self.now[0] = h.state["sessions"]["maps"]["asks"][0]["since"] + owner.ASK_TTL
        h.revive_asks()
        h.revive_asks()
        (e,) = self.facts(h)
        self.assertEqual((e["entity"], e["data"]["channel"], len(e["data"]["asks"])), ("maps", "core", 1))
        self.assertEqual(sum("have gone stale" in m["text"] for m in h.state["messages"].values()), 1)

    def test_a_pile_of_old_cards_is_woken_two_sessions_a_minute(self):
        # a tick is ~0.3 s: "two per tick" would wake a pile in a second (review 29.10) - two, then a pause
        h = self.hub()
        for i in range(5):
            self.ask(h, f"s{i}", f"Question {i}?")
        self.now[0] += owner.ASK_TTL + 100
        h.revive_asks()
        self.assertEqual(len(self.facts(h)), askexpired.STEP)
        self.now[0] += askexpired.PAUSE - 1
        h.revive_asks()
        self.assertEqual(len(self.facts(h)), askexpired.STEP)
        self.now[0] += 1
        h.revive_asks()
        self.assertEqual(len(self.facts(h)), 4)
        self.now[0] += askexpired.PAUSE
        h.revive_asks()
        self.assertEqual(len(self.facts(h)), 5)
        self.assertEqual(askexpired.PAUSE, 60)

    def test_the_step_is_in_the_tick(self):
        src = inspect.getsource(Hub.tick)
        self.assertIn("self.revive_asks", src)
        self.assertLess(src.index("self.budget_mode"), src.index("self.revive_asks"))


if __name__ == "__main__":
    unittest.main()
