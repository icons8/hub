"""hub.json quiet_review: on a non-working day and after the working day (19:00–10:00) what waits for the owner's decision
does not go to Telegram or the phone. Cards, tool requests, their reminders pile up on the page and
come as one batch at 10:00 of the next working day; the days off are Saturday and Sunday. At once still: a card in a turn on the owner's live word, a question
beginning with 🚨. Off — the journal and the behaviour are as before.

November 2026, Moscow: Sat 07.11 and Sun 08.11 are days off."""
import tests  # noqa: F401 — keeps the machine's settings out
import datetime
import unittest

from core import owner, reducer
from tests.fakes import tz

TZ = "Europe/Moscow"
OWNER = {"channel": "web", "owner": True}
ASK = {"questions": [{"question": "Which option?", "options": [{"label": "A"}, {"label": "B"}]}]}


def ts(d, h, m=0):
    from zoneinfo import ZoneInfo
    return datetime.datetime(2026, 11, d, h, m, tzinfo=ZoneInfo(TZ)).timestamp()


def when(t):
    from zoneinfo import ZoneInfo
    return datetime.datetime.fromtimestamp(t, ZoneInfo(TZ)).strftime("%a %d %H:%M")


def setting():
    return dict(owner.REVIEW)


@tz.need
class Core(unittest.TestCase):
    def setUp(self, review=True):
        self.st, self.seq, self.now, self.actions = reducer.empty(), 0, ts(2, 12), []
        self.ev("owner.settings", None, timezone=TZ, **({"quiet_review": setting()} if review else {}))
        for n, kind in (("hub", "hub"), ("pe", None)):
            self.ev("session.requested", n, cwd="/w", session_id=f"s-{n}", kind=kind, **OWNER)
            self.ev("session.started", n)

    def ev(self, type, entity=None, at=None, **data):
        self.seq += 1
        self.now = at or self.now + 1
        self.st, self.actions, why = reducer.reduce(self.st, {"seq": self.seq, "ts": self.now, "type": type,
                                                              "entity": entity, "data": data})
        return why

    def ask(self, at, name="pe", rid="r1", question="Which option?", marker=None):
        self.ev("turn.started", name, at=at - 1, marker=marker)
        inp = {"questions": [{**ASK["questions"][0], "question": question}]}
        self.ev("permission.requested", name, at=at, request_id=rid, tool="AskUserQuestion", input=inp,
                interactive=True)
        return self.st["sessions"][name]["asks"][-1]

    def pushed(self, now):
        return [a for a in owner.alerts(self.st, now) if a["kind"] in ("question", "tool")]

    # --- the window -------------------------------------------------------------------------------------------------
    def test_saturday_waits_for_monday_10(self):
        a = self.ask(ts(7, 15))
        self.assertEqual(when(a["held_until"]), "Mon 09 10:00")
        self.assertEqual(self.pushed(ts(8, 20)), [])
        self.assertEqual(len(owner.open_asks(self.st["sessions"]["pe"], ts(8, 20))), 1)   # on the page
        deny = next(x for x in self.actions if x["do"] == "respond")["response"]["message"]
        self.assertIn("09.11 10:00", deny)   # the session knows when, and how to ask at once

    def test_wednesday_evening_waits_for_thursday_10(self):
        self.assertEqual(when(self.ask(ts(11, 19, 30))["held_until"]), "Thu 12 10:00")

    def test_the_morning_edge(self):
        a = self.ask(ts(11, 19, 30))
        self.assertEqual(self.pushed(ts(12, 9, 59)), [])
        out = self.pushed(ts(12, 10))
        self.assertEqual([(x["card"], x.get("quiet")) for x in out], [(a["id"], True)])
        self.assertEqual(out[0]["since"], ts(12, 10))

    def test_working_day_before_19_is_not_held(self):
        self.assertNotIn("held_until", self.ask(ts(11, 18, 59)))
        self.assertNotIn("held_until", self.ask(ts(12, 10), rid="r2"))
        self.assertIn("held_until", self.ask(ts(12, 9, 59), rid="r3"))

    def test_a_holiday_on_a_weekday_is_a_working_day(self):
        self.assertNotIn("held_until", self.ask(ts(4, 12)))
        self.assertEqual(when(self.ask(ts(4, 20), rid="r2")["held_until"]), "Thu 05 10:00")

    def test_saturday_is_a_day_off_for_good(self):
        self.assertEqual(when(self.ask(ts(14, 12))["held_until"]), "Mon 16 10:00")

    def test_a_turn_on_the_owners_word_goes_at_once(self):
        self.ev("message.received", None, at=ts(7, 15), to="pe", text="do the option", **OWNER)
        a = self.ask(ts(7, 15, 5), marker=f"m{self.seq}")
        self.assertNotIn("held_until", a)
        self.assertEqual([x["card"] for x in self.pushed(ts(7, 15, 5))], [a["id"]])

    def test_an_old_word_of_the_owner_does_not_count(self):
        self.ev("message.received", None, at=ts(6, 18), to="pe", text="do the option", **OWNER)
        self.assertIn("held_until", self.ask(ts(7, 15), marker=f"m{self.seq}"))

    def test_a_session_message_turn_is_held(self):
        self.ev("message.received", None, at=ts(7, 15), to="pe", text="do it", channel="session", owner=False,
                sender="hub")
        self.assertIn("held_until", self.ask(ts(7, 15, 5), marker=f"m{self.seq}"))

    def test_an_urgent_mark_goes_at_once(self):
        a = self.ask(ts(7, 15), question="🚨 Prod is down: roll back?")
        self.assertNotIn("held_until", a)
        self.assertEqual(len(self.pushed(ts(7, 15))), 1)

    def test_an_urgent_card_keeps_its_reminders(self):
        # review: the 🚨 card goes at once, so its reminders do not wait for Monday either (only the night moves them)
        a = self.ask(ts(7, 12), question="🚨 Prod is down: roll back?")
        due = [at for at, qid, _ in owner.pending(self.st) if qid == f"q:pe:{a['request_id']}"]
        self.assertEqual([when(x) for x in due], ["Sat 07 12:30"])

    def test_an_urgent_tool_request_keeps_its_reminders_and_the_push_says_so(self):
        # review round 2: a tool request with a 🚨 why is not held either; the reminder's push counts the real wait
        r = {"tool": "figma", "why": "🚨 prod mockup", "state": "needs-owner", "since": ts(7, 12)}
        self.assertIsNone(owner.review_hold(self.st, ts(7, 12), [r["why"]], ["pe"]))
        self.st["sessions"]["pe"]["requests"] = [r]
        due = [at for at, qid, _ in owner.pending(self.st) if qid == "q:pe:tool:figma"]
        self.assertEqual([when(x) for x in due], ["Sat 07 12:30"])
        self.st["owner"]["reminders"]["q:pe:tool:figma"] = 1
        (a,) = self.pushed(ts(7, 12, 31))
        self.assertIn(owner._waited(30 * 60, None), a["body"])

    def test_a_held_card_says_how_to_withdraw_it_when_asked_again_as_urgent(self):
        # review: asked again with 🚨, the held twin would still come in the morning batch
        a = self.ask(ts(7, 15))
        deny = next(x for x in self.actions if x["do"] == "respond")["response"]["message"]
        self.assertIn(f"hub question withdraw {a['id']}", deny)

    def test_away_is_stronger(self):
        self.ev("owner.away", None, at=ts(7, 12), **OWNER)
        a = self.ask(ts(7, 15))
        self.assertTrue(a.get("away"))
        self.assertNotIn("held_until", a)

    # --- deadline, reminders --------------------------------------------------------------------------------
    def test_a_held_card_does_not_expire_over_the_weekend(self):
        a = self.ask(ts(6, 19, 30))   # Friday evening: Sat, Sun and the night are 62 hours
        sess = self.st["sessions"]["pe"]
        self.assertEqual([x["id"] for x in owner.open_asks(sess, ts(9, 9, 59))], [a["id"]])
        self.assertEqual([x["id"] for x in owner.open_asks(sess, ts(10, 9, 59))], [a["id"]])   # 24 h from the release
        self.assertEqual(owner.open_asks(sess, ts(10, 10)), [])

    def test_reminders_start_from_the_release(self):
        a = self.ask(ts(7, 15))
        due = [at for at, qid, _ in owner.pending(self.st) if qid == f"q:pe:{a['request_id']}"]
        self.assertEqual([when(x) for x in due], ["Mon 09 10:30"])

    def test_a_reminder_of_a_day_card_moves_to_the_morning(self):
        a = self.ask(ts(6, 18, 40))   # Friday before 19:00: goes at once, the first reminder would be at 19:10
        self.assertNotIn("held_until", a)
        due = [at for at, qid, _ in owner.pending(self.st) if qid == f"q:pe:{a['request_id']}"]
        self.assertEqual([when(x) for x in due], ["Mon 09 10:00"])


    def test_a_tool_request_is_held(self):
        r = {"tool": "figma", "why": "mockup", "state": "needs-owner", "since": ts(7, 15),
             "held_until": owner.review_hold(self.st, ts(7, 15), ["mockup"], ["pe"])}
        self.st["sessions"]["pe"]["requests"] = [r]
        self.assertEqual(when(r["held_until"]), "Mon 09 10:00")
        self.assertEqual(self.pushed(ts(8, 12)), [])
        self.assertEqual([(x["kind"], x.get("quiet"), x["since"]) for x in self.pushed(ts(9, 10))],
                         [("tool", True, ts(9, 10))])

    # --- the setting ------------------------------------------------------------------------------------------------
    def test_turning_it_off_releases_what_is_held(self):
        self.ask(ts(7, 15))
        self.ev("owner.settings", None, at=ts(7, 16), timezone=TZ, quiet_review=False)
        self.assertNotIn("review", self.st["owner"])
        self.assertEqual(self.st["sessions"]["pe"]["asks"][0]["held_until"], ts(7, 16))
        self.assertEqual(len(self.pushed(ts(7, 16))), 1)

    def test_days_in_the_setting_are_not_taken(self):
        self.ev("owner.settings", None, timezone=TZ, quiet_review={"from": "19:00", "to": "10:00", "off": ["2026-11-04"],
                                                                    "span": ["2026-11-02", "2027-01-03"]})
        self.assertNotIn("held_until", self.ask(ts(4, 12)))
        self.assertNotIn("off", self.st["owner"]["review"])


@tz.need
class Off(Core):
    """Without the setting: no field in the state, nothing held."""
    def setUp(self):
        super().setUp(review=False)

    def test_saturday_waits_for_monday_10(self):
        a = self.ask(ts(7, 15))
        self.assertNotIn("held_until", a)
        self.assertNotIn("review", self.st["owner"])
        self.assertEqual(len(self.pushed(ts(7, 15))), 1)
        deny = next(x for x in self.actions if x["do"] == "respond")["response"]["message"]
        self.assertNotIn("non-working", deny)


    def test_a_held_card_does_not_expire_over_the_weekend(self):
        self.ask(ts(6, 19, 30))
        self.assertEqual(owner.open_asks(self.st["sessions"]["pe"], ts(7, 19, 30)), [])

    # the rest is about the setting being on
    test_wednesday_evening_waits_for_thursday_10 = test_the_morning_edge = test_working_day_before_19_is_not_held = \
        test_a_holiday_on_a_weekday_is_a_working_day = test_saturday_is_a_day_off_for_good = test_a_turn_on_the_owners_word_goes_at_once = \
        test_an_old_word_of_the_owner_does_not_count = test_a_session_message_turn_is_held = \
        test_an_urgent_mark_goes_at_once = test_away_is_stronger = test_reminders_start_from_the_release = \
        test_a_reminder_of_a_day_card_moves_to_the_morning = test_a_tool_request_is_held = \
        test_turning_it_off_releases_what_is_held = test_days_in_the_setting_are_not_taken = \
        test_an_urgent_card_keeps_its_reminders = test_a_held_card_says_how_to_withdraw_it_when_asked_again_as_urgent = \
        test_an_urgent_tool_request_keeps_its_reminders_and_the_push_says_so = None


@tz.need
class Hours(unittest.TestCase):
    def test_hours(self):
        self.assertEqual(owner.review_hours(True), {"from": "19:00", "to": "10:00"})
        self.assertEqual(owner.review_hours({"from": "20:30", "to": "x"}), {"from": "20:30", "to": "10:00"})
        for off in (None, False, "yes", 1):
            self.assertIsNone(owner.review_hours(off))


class Daemon(unittest.TestCase):
    """hubd hands the setting to the core; without the setting — no field."""
    def setUp(self):
        import os
        import tempfile
        from unittest import mock
        self.state = tempfile.mkdtemp(prefix="quiet-review-")
        self.cfg = os.path.join(self.state, "hub.json")
        env = mock.patch.dict(os.environ, {"HUB_CONFIG": self.cfg})
        env.start()
        self.addCleanup(env.stop)

    def settings(self, cfg, at=None):
        import json
        import os
        import time
        import types
        from core import journal
        from core.daemon import Hub
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump({"timezone": "UTC", **cfg}, f)
        h = Hub(self.state, types.SimpleNamespace(), clock=(lambda: at) if at else time.time)
        h.execute = lambda a: None
        h.load_catalog()
        return [e["data"] for e in journal.read(h.journal.path) if e["type"] == "owner.settings"]

    def test_off_no_field(self):
        got = self.settings({})
        self.assertTrue(got)
        self.assertFalse([d for d in got if "quiet_review" in d])

    def test_on(self):
        import json
        import os
        # a workday cache in the state folder is not read: the days off are Saturday and Sunday
        with open(os.path.join(self.state, "workcal-2026.json"), "w", encoding="utf-8") as f:
            json.dump({"off": ["03-04"] * 60}, f)
        (rv,) = [d["quiet_review"] for d in self.settings({"quiet_review": True}) if "quiet_review" in d]
        self.assertEqual(rv, {"from": "19:00", "to": "10:00"})


if __name__ == "__main__":
    unittest.main()
