"""The owner's deputies in the contacts book (spec d2679763, part A, slice A-1; owner 20.11, m2681621: "in the contact
list you can say on which questions — free text, or ask the hub to ask questions to set up a deputy"). The book is an
address any session writes, so a deputy is a fact of the core: the owner sets it in his terminal, or the hub on his
word (--for mN — his message, --for qN — his answer to a card) with the area quoted from those words verbatim and the
person named in them; nobody else. It is bound to the person's channel as it was when he named them: another key, an
unlinked or quarantined hub, another address in the book — and it is not active."""
import tests  # noqa: F401 — keeps the machine's settings out
import os
import tempfile
import unittest

from core import busstate, deputies, deputyroute, i18n, owner, reducer
from modules.contacts import contacts

OWNER = {"channel": "cli", "owner": True}
WEB = {"channel": "web", "owner": True}
HUB = {"channel": "session", "owner": False, "sender": "hub"}
ANNA = {"id": "c0000a0b1", "name": "Anna Lee"}
BUS = {"kind": "bus", "contact": "anna-hub"}
ZONE = "map tiles: styles and stage rollouts"
FORM = {"questions": [{"question": "What does Anna Lee decide for you?", "header": "Deputy",
                       "options": [{"label": "map tiles: styles and stage rollouts"}, {"label": "the tile cache"}]}]}


class Base(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.now, self.events = reducer.empty(), 0, 1000, []
        for n, kind in (("hub", "hub"), ("maps", None), ("chat", None)):
            self.ev("session.requested", n, cwd="/w", session_id=f"s-{n}", kind=kind, **WEB)
            self.ev("session.started", n)
        bus = busstate._bus(self.st)
        bus["enabled"] = True
        bus["contacts"]["anna-hub"] = {"fp": "SHA256:anna", "status": "known", "verified": True, "person": ANNA,
                                       "label": "Anna's hub"}

    def ev(self, type, entity=None, ts=None, **data):
        self.seq += 1
        self.now = ts or self.now + 1
        ev = {"seq": self.seq, "ts": self.now, "type": type, "entity": entity, "data": data}
        self.st, self.actions, why = reducer.reduce(self.st, ev)
        if why is None:   # what a journal would replay (bus state set by hand stays out: replays use events only)
            self.events.append(ev)
        return why

    def said(self, text, to="hub"):
        """The owner's message to a session: its mN."""
        self.assertIsNone(self.ev("message.received", None, to=to, text=text, **WEB))
        return f"m{self.seq}"

    def set(self, zone=ZONE, who=OWNER, channel=BUS, person=ANNA, **more):
        return self.ev("contact.deputy_set", None, person=person, names=["Anna"], zone=zone, path=channel,
                       **who, **more)

    def rec(self):
        return (self.st.get("deputies") or {}).get(ANNA["id"])


class WhoSetsIt(Base):
    def test_the_owner_in_his_terminal_sets_a_deputy_with_the_channel_of_her_hub(self):
        self.assertIsNone(self.set(**{"not": "anything about payments"}))
        rec = self.rec()
        self.assertEqual((rec["zone"], rec["not"], rec["by"], rec["on"]), (ZONE, "anything about payments", "owner",
                                                                           True))
        self.assertEqual(rec["channel"], {"kind": "bus", "contact": "anna-hub", "fp": "SHA256:anna"})
        self.assertEqual(list(deputies.active(self.st)), [ANNA["id"]])

    def test_a_session_a_plugin_and_the_hub_without_his_word_do_not(self):
        for who in ({"channel": "session", "owner": False, "sender": "maps"},
                    {"channel": "plugin", "owner": False, "sender": "notes"}, HUB):
            self.assertIn("a deputy is set by the owner", self.set(who=who) or "", who)
        self.assertIsNone(self.rec())

    def test_a_session_cannot_cite_the_owners_words_either(self):
        mid = self.said(f"Anna is my deputy for {ZONE}", to="maps")
        why = self.set(who={"channel": "session", "owner": False, "sender": "maps"}, **{"for": mid})
        self.assertIn("a deputy is set by the owner", why or "")

    def test_the_hub_on_his_message_quoting_the_area(self):
        mid = self.said(f"Make Anna my deputy: {ZONE}, but nothing about payments")
        self.assertIsNone(self.set(who=HUB, **{"for": mid, "not": "nothing about payments"}))
        self.assertEqual((self.rec()["by"], self.rec()["word"]), (mid, mid))

    def test_a_declined_name_counts_as_named(self):
        mid = self.said(f"Κάνε τον Γιώργο αναπληρωτή: {ZONE}")   # a declined name (Greek, as any inflected language)
        self.assertIsNone(self.ev("contact.deputy_set", None, person={"id": "c0000a0b2", "name": "Γιώργος"}, names=[],
                                  zone=ZONE, path={"kind": "via", "session": "chat",
                                                      "address": {"teams_chat": "19:abc"}}, **HUB, **{"for": mid}))

    def test_the_hub_cannot_put_its_own_words_for_the_area(self):
        mid = self.said("Anna will help while I am away")
        why = self.set(who=HUB, zone="everything", **{"for": mid})
        self.assertIn("is not the owner's words", why or "")
        mid = self.said(f"Make Anna my deputy: {ZONE}")
        why = self.set(who=HUB, **{"for": mid, "not": "payments"})   # "does not decide" is his text too
        self.assertIn("is not the owner's words", why or "")
        self.assertIsNone(self.rec())

    def test_words_that_name_someone_else_are_refused(self):
        mid = self.said(f"Make Mark my deputy: {ZONE}")
        self.assertIn("do not name Anna Lee", self.set(who=HUB, **{"for": mid}) or "")

    def test_a_day_old_or_elsewhere_written_word_is_refused(self):
        mid = self.said(f"Make Anna my deputy: {ZONE}")
        self.now += 86400 + 5
        self.assertIn("h old", self.set(who=HUB, **{"for": mid}) or "")
        mid = self.said(f"Make Anna my deputy: {ZONE}", to="maps")
        self.assertIn("not to you", self.set(who=HUB, **{"for": mid}) or "")

    def test_the_hub_on_his_answer_to_its_card(self):
        self.ev("turn.started", "hub", marker=None)
        self.ev("permission.requested", "hub", request_id="r1", tool="AskUserQuestion", input=FORM, interactive=True)
        qid = f"q{self.seq}"
        self.ev("permission.answered", "hub", request_id="r1", behavior="allow",
                answers={FORM["questions"][0]["question"]: ZONE}, **WEB)
        self.assertIsNone(self.set(who=HUB, **{"for": qid}))
        self.assertEqual(self.rec()["by"], qid)
        self.ev("contact.deputy_off", None, person=ANNA, names=[], **OWNER)
        why = self.set(who=HUB, zone="the tile cache", **{"for": qid})   # an option he did not choose
        self.assertIn("is not the owner's words", why or "")

    def test_a_removed_deputy_is_not_set_again_from_the_same_words(self):
        mid = self.said(f"Make Anna my deputy: {ZONE}")
        self.assertIsNone(self.set(who=HUB, **{"for": mid}))
        off = self.said("Anna is no longer my deputy")
        self.assertIsNone(self.ev("contact.deputy_off", None, person=ANNA, names=["Anna"], **HUB, **{"for": off}))
        self.assertFalse(self.rec()["on"])
        self.assertEqual(deputies.active(self.st), {})
        self.assertIn("already set Anna Lee", self.set(who=HUB, **{"for": mid}) or "")
        self.assertIsNone(self.set())   # the owner himself: his new word
        self.assertTrue(self.rec()["on"])

    def test_removing_someone_who_is_not_a_deputy_is_refused(self):
        self.assertIn("is not a deputy", self.ev("contact.deputy_off", None, person=ANNA, names=[], **OWNER) or "")

    def test_the_area_is_a_text_of_a_sane_size(self):
        self.assertIn("the area is the owner's text", self.set(zone="") or "")
        self.assertIn("the area is the owner's text", self.set(zone="x" * 501) or "")
        self.assertIn("the owner's text", self.set(**{"not": "y" * 501}) or "")
        self.assertIn("a person of the contacts book", self.set(person={"id": "anna", "name": "Anna"}) or "")


class Channel(Base):
    def test_no_channel_is_refused_with_what_is_missing(self):
        self.assertIn("hub contacts link", self.set(channel=None) or "")

    def test_a_hub_not_checked_or_not_hers_is_refused(self):
        c = self.st["bus"]["contacts"]["anna-hub"]
        c["verified"] = False
        self.assertIn("pair code", self.set() or "")
        self.st["bus"]["contacts"]["anna-hub"].update(verified=True, person={"id": "c0000a0b9", "name": "Mark"})
        self.assertIn("another record", self.set() or "")

    def test_through_the_session_of_her_channel(self):
        ch = {"kind": "via", "session": "chat", "address": {"teams_chat": "19:abc", "email": "Anna@X.com"}}
        self.assertIsNone(self.set(channel=ch))
        self.assertEqual(self.rec()["channel"]["address"], {"teams_chat": "19:abc", "email": "anna@x.com"})
        self.assertIsNone(deputies.channel_why(self.st, self.rec(), {"teams_chat": "19:abc", "email": "anna@x.com"}))
        self.assertEqual(deputies.channel_why(self.st, self.rec(), {"teams_chat": "19:zzz"})[0], "address")
        self.assertIn("gone", self.set(channel={**ch, "session": "nobody"}) or "")
        self.assertIn("has no channel", self.set(channel={**ch, "address": {}}) or "")

    def test_her_hub_changing_makes_the_deputy_inactive_until_named_again(self):
        self.set()
        c = self.st["bus"]["contacts"]["anna-hub"]
        for change, back in (({"fp": "SHA256:other"}, {"fp": "SHA256:anna"}), ({"quarantine": 5}, {"quarantine": None}),
                             ({"status": "muted"}, {"status": "known"}),
                             ({"person": {"id": "c0000a0b9", "name": "Mark"}}, {"person": ANNA})):
            c.update(change)
            self.assertEqual(deputies.active(self.st), {}, change)
            c.update(back)
            self.assertEqual(list(deputies.active(self.st)), [ANNA["id"]])
        del self.st["bus"]["contacts"]["anna-hub"]
        self.assertEqual(deputies.active(self.st), {})

    def test_a_renamed_record_keeps_the_deputy(self):
        self.set()
        self.st["bus"]["contacts"]["anna-hub"]["person"] = {"id": ANNA["id"], "name": "Anna Lee-Cole"}
        self.assertEqual(list(deputies.active(self.st)), [ANNA["id"]])


class AwayWithTheFlag(Base):
    """`hub away --deputies [<person> …]`, `/away deputies`: the flag lives one away; without it away is as before
    (A5). The hub turns it on only on the owner's word."""

    def setUp(self):
        super().setUp()
        self.set()
        self.ev("contact.deputy_set", None, person={"id": "c0000a0b2", "name": "Boris"}, names=["Borya"],
                zone="ruby backends", path={"kind": "via", "session": "chat", "address": {"teams_chat": "19:b"}},
                **OWNER)

    def test_without_the_flag_nobody_is_included(self):
        self.assertIsNone(self.ev("owner.away", None, **WEB))
        self.assertEqual(deputies.included(self.st), {})
        self.assertNotIn("deputies", self.st["owner"])

    def test_all_or_the_named_ones_and_names_that_fit_nobody(self):
        self.assertIsNone(self.ev("owner.away", None, deputies=True, **WEB))
        self.assertEqual(sorted(deputies.included(self.st)), ["c0000a0b1", "c0000a0b2"])
        self.ev("owner.back", None, **WEB)
        self.assertEqual(deputies.included(self.st), {})
        self.assertNotIn("deputies", self.st["owner"])
        self.assertIsNone(self.ev("owner.away", None, deputies=["Borya", "Mark"], **WEB))
        self.assertEqual(list(deputies.included(self.st)), ["c0000a0b2"])
        self.assertEqual(self.st["owner"]["deputies"]["unknown"], ["Mark"])

    def test_away_again_adds_to_the_period(self):
        self.ev("owner.away", None, deputies=["Anna"], **WEB)
        self.ev("owner.away", None, hours=2, **WEB)   # a new period without the flag keeps it
        self.assertEqual(list(deputies.included(self.st)), [ANNA["id"]])
        self.ev("owner.away", None, deputies=["Boris"], **WEB)
        self.assertEqual(sorted(deputies.included(self.st)), ["c0000a0b1", "c0000a0b2"])

    def test_the_hub_turns_it_on_only_on_his_word(self):
        self.assertIn("only on the owner's word", self.ev("owner.away", None, deputies=True, **HUB) or "")
        self.assertEqual(self.st["owner"]["state"], "present")
        mid = self.said("I am off till tonight, questions to the deputies")
        self.assertIsNone(self.ev("owner.away", None, deputies=True, **HUB, **{"for": mid}))
        self.assertEqual(len(deputies.included(self.st)), 2)

    def test_a_deputy_whose_channel_changed_is_left_out(self):
        self.ev("owner.away", None, deputies=True, **WEB)
        self.st["bus"]["contacts"]["anna-hub"]["fp"] = "SHA256:other"
        self.assertEqual(list(deputies.included(self.st)), ["c0000a0b2"])

    def test_the_page_snapshot_says_who_and_how_many(self):
        from core import views

        def owner_view():
            r = views.Reader()
            r.state = self.st
            return r.snapshot()["owner"]
        self.ev("owner.away", None, deputies=True, **WEB)
        o = owner_view()
        self.assertEqual((sorted(o["deputies"]), o["at_deputies"]), (["Anna Lee", "Boris"], 0))
        self.ev("owner.back", None, **WEB)
        self.assertNotIn("deputies", owner_view())

    def test_the_lines_the_away_prints(self):
        from core import deputycli
        self.ev("owner.away", None, deputies=["Anna", "Mark"], **WEB)
        lines = deputycli.away_lines(self.st, status=True)
        self.assertIn(f"Anna Lee («{ZONE}»)", lines[0])
        self.assertIn("Not deputies: Mark", lines[1])
        self.assertIn("at the deputies now: 0", lines[2])


TG = {"channel": "telegram", "owner": True}
MODULE = {"channel": "module:notes", "owner": True}


class AwayHeld(Base):
    """An away with the deputies ("hold until /back or the term") is not ended by his messages from
    the page or Telegram — only by `hub back` / `/back` or the end of --hours; an away without the flag is as before
    (09.10: a forgotten /away ends with his next word)."""

    def setUp(self):
        super().setUp()
        self.set()

    def writes(self, **who):
        self.assertIsNone(self.ev("message.received", None, to="hub", text="are you there", **who))

    def test_his_words_from_the_page_telegram_or_a_module_do_not_end_it(self):
        self.ev("owner.away", None, deputies=True, **WEB)
        for who in (WEB, TG, MODULE):
            self.writes(**who)
            self.assertEqual(self.st["owner"]["state"], "away", who)
            self.assertEqual(list(deputies.included(self.st)), [ANNA["id"]], who)
        self.assertNotIn("summary", self.st["owner"])

    def test_hub_back_ends_it(self):
        self.ev("owner.away", None, deputies=True, **WEB)
        self.writes(**WEB)
        self.assertIsNone(self.ev("owner.back", None, **OWNER))
        self.assertEqual(self.st["owner"]["state"], "present")
        self.assertEqual(deputies.included(self.st), {})

    def test_the_term_ends_it(self):
        self.ev("owner.away", None, hours=2, deputies=True, **WEB)
        self.writes(**TG)
        self.assertEqual(self.st["owner"]["state"], "away")
        self.assertIsNone(self.ev("timer.fired", "owner", ts=self.st["owner"]["until"] + 1))
        self.assertEqual(self.st["owner"]["state"], "present")

    def test_a_new_period_without_the_flag_keeps_it_held(self):
        self.ev("owner.away", None, deputies=True, **WEB)
        self.ev("owner.away", None, hours=3, **WEB)
        self.writes(**WEB)
        self.assertEqual(self.st["owner"]["state"], "away")

    def test_away_without_the_flag_ends_with_his_word_as_before(self):
        for who in (WEB, TG):
            with self.subTest(who):
                self.setUp()
                self.ev("owner.away", None, **WEB)
                self.writes(**who)
                self.assertEqual(self.st["owner"]["state"], "present")

    def test_a_flag_no_deputy_fits_holds_nothing(self):
        from core import deputycli
        self.ev("owner.away", None, deputies=["Mark"], **WEB)
        lines = deputycli.away_lines(self.st, status=True)
        for key in ("cli.away.deputies_held", "cli.away.deputies_held_open"):
            self.assertNotIn(i18n.t(key), lines)
        self.writes(**WEB)
        self.assertEqual(self.st["owner"]["state"], "present")

    def test_a_deputy_removed_mid_away_lets_his_word_end_it(self):
        self.ev("owner.away", None, deputies=True, **WEB)
        self.writes(**WEB)
        self.assertEqual(self.st["owner"]["state"], "away")
        self.ev("contact.deputy_off", None, person=ANNA, names=[], **OWNER)
        self.writes(**WEB)
        self.assertEqual(self.st["owner"]["state"], "present")

    def test_the_flag_of_a_finished_away_does_not_hold_the_next_one(self):
        self.ev("owner.away", None, deputies=True, **WEB)
        self.ev("owner.back", None, **WEB)
        self.ev("owner.away", None, **WEB)
        self.writes(**WEB)
        self.assertEqual(self.st["owner"]["state"], "present")

    def test_the_status_says_it_is_held_and_names_the_term_only_when_there_is_one(self):
        from core import deputycli
        self.ev("owner.away", None, deputies=True, **WEB)
        self.assertEqual(deputycli.away_lines(self.st, status=True)[-1], i18n.t("cli.away.deputies_held_open"))
        self.ev("owner.back", None, **WEB)
        self.ev("owner.away", None, hours=2, deputies=True, **WEB)
        self.assertEqual(deputycli.away_lines(self.st, status=True)[-1], i18n.t("cli.away.deputies_held"))
        self.ev("owner.back", None, **WEB)
        self.ev("owner.away", None, **WEB)
        self.assertEqual(deputycli.away_lines(self.st, status=True), [])

    def test_a_deputy_whose_channel_is_down_holds_it_and_their_card_waits_for_him(self):
        from core import deputycli, views
        self.ev("owner.away", None, deputies=True, **WEB)
        self.st["bus"]["contacts"]["anna-hub"]["quarantine"] = True   # her hub is in quarantine now
        self.assertEqual(deputies.included(self.st), {})
        self.writes(**WEB)
        self.assertEqual(self.st["owner"]["state"], "away")
        self.assertEqual(deputycli.away_lines(self.st, status=True)[-1], i18n.t("cli.away.deputies_held_open"))
        r = views.Reader()
        r.state = self.st
        self.assertEqual(r.snapshot()["owner"]["deputies"], [])   # nobody takes questions now, yet it is held
        self.assertTrue(r.snapshot()["owner"]["away_held"])
        self.ev("turn.started", "maps", marker=None)
        self.ev("permission.requested", "maps", request_id="r1", tool="AskUserQuestion", input=TWO, interactive=True)
        why = self.ev("question.routed", "maps", id=f"q{self.seq}", person="Anna", sender="maps", **SAY)
        self.assertIn("in quarantine", why or "")
        self.assertIn("waits for the owner", why or "")


class HisCardWhileHeld(Base):
    """He writes to the hub while the away with the deputies holds (his words do not end it): the hub's card in the turn
    on his message is in front of him at once — on the page, in Telegram, with push — not "until his return". The
    hub's cards are his alone (no deputy, no hub answers them), and a deputy's area is set only from his own words: held
    back, such a card left him no way to change an area while away but to type it out himself. Any other card of the
    away waits for him as before."""

    def setUp(self):
        super().setUp()
        self.set()
        self.ev("owner.away", None, deputies=True, **WEB)

    def hub_asks(self, marker, name="hub", ts=None):
        self.ev("turn.started", name, marker=marker, ts=ts)
        self.ev("permission.requested", name, request_id=f"r{self.seq}", tool="AskUserQuestion", input=FORM,
                interactive=True)
        (resp,) = [a["response"] for a in self.actions if a.get("do") == "respond"]
        return self.st["sessions"][name]["asks"][-1], resp["message"]

    def test_the_hubs_card_on_his_message_is_in_front_of_him_at_once(self):
        for who in (TG, WEB):
            with self.subTest(who):
                self.setUp()
                self.assertIsNone(self.ev("message.received", None, to="hub", text="give Anna two areas", **who))
                ask, told = self.hub_asks(f"m{self.seq}")
                self.assertEqual(self.st["owner"]["state"], "away")
                self.assertNotIn("away", ask)
                self.assertFalse(owner.waits_return(ask))
                self.assertNotIn("they will see it when they are back", told)

    def test_any_other_card_of_the_away_waits_for_him(self):
        self.assertIsNone(self.ev("message.received", None, to="maps", text="pick the name", **TG))
        mine = f"m{self.seq}"
        ask, told = self.hub_asks(mine, name="maps")   # a session's card on his message: deputies and the hub take those
        self.assertTrue(ask.get("away"))
        self.ev("turn.result", "maps", text="asked")
        self.ev("turn.stopped", "maps")
        self.ev("message.received", None, to="hub", text="the tests are green", channel="session", owner=False,
                sender="maps")
        ask, told = self.hub_asks(f"m{self.seq}")   # the hub's card on a session's message
        self.assertTrue(ask.get("away"))
        self.assertIn("they will see it when they are back", told)
        self.ev("turn.result", "hub", text="asked")
        self.ev("turn.stopped", "hub")
        ask, _ = self.hub_asks(None)   # on its own
        self.assertTrue(ask.get("away"))

    def test_a_word_from_the_terminal_does_not_bring_it(self):
        # the CLI is not necessarily him (modules write that way too): it does not end an away either
        self.assertIsNone(self.ev("message.received", None, to="hub", text="give Anna two areas", **OWNER))
        ask, _ = self.hub_asks(f"m{self.seq}")
        self.assertTrue(ask.get("away"))

    def test_an_insertion_mid_turn_does_not_take_it_away(self):
        self.assertIsNone(self.ev("message.received", None, to="hub", text="give Anna two areas", **TG))
        mine = f"m{self.seq}"
        self.ev("turn.started", "hub", marker=mine)
        self.assertIsNone(self.ev("message.received", None, to="hub", text="the tests are green", channel="session",
                                  owner=False, sender="maps"))
        note = f"m{self.seq}"
        self.assertIsNone(self.ev("message.inject", note, channel="cli", owner=True, src="watch"))
        self.ev("turn.started", "hub", marker=note)   # read mid-turn: the same turn
        self.assertEqual(self.st["sessions"]["hub"]["turn_head"], mine)
        self.ev("permission.requested", "hub", request_id="r1", tool="AskUserQuestion", input=FORM, interactive=True)
        self.assertNotIn("away", self.st["sessions"]["hub"]["asks"][-1])

    def test_his_message_of_hours_ago_does_not_bring_it(self):
        self.assertIsNone(self.ev("message.received", None, to="hub", text="give Anna two areas", **TG))
        ask, _ = self.hub_asks(f"m{self.seq}", ts=self.now + owner.LIVE_WORD + 60)
        self.assertTrue(ask.get("away"))

    def test_his_return_leaves_it_as_it_was(self):
        self.assertIsNone(self.ev("message.received", None, to="hub", text="give Anna two areas", **TG))
        ask, _ = self.hub_asks(f"m{self.seq}")
        self.ev("owner.back", None, **OWNER)
        self.assertNotIn("back_at", ask)   # its time ran from when it was asked: he saw it then
        self.assertEqual(owner.ask_since(ask), ask["since"])


TWO = {"questions": [{"question": "Which file name: tiles.toml or tiles_config.toml?",
                      "options": [{"label": "tiles.toml (Recommended)", "description": "short"},
                                  {"label": "tiles_config.toml"}]}]}
BORIS = {"id": "c0000a0b2", "name": "Boris"}
SAY = {"channel": "session", "owner": False}


class Route(Base):
    """A card in a deputy's area goes to them (A6–A7): through the session of their channel here (the bus one — in
    tests/test_deputies_bus.py). Their words come back to the session that asked as the deputy's word — never the
    owner's; the card stays at them until the session closes it, returns it, or the ground goes."""

    def setUp(self):
        super().setUp()
        self.ev("contact.deputy_set", None, person=BORIS, names=["Borya"], zone="map tiles",
                path={"kind": "via", "session": "chat", "address": {"teams_chat": "19:b"}}, **OWNER)
        self.ev("owner.away", None, deputies=["Boris"], **WEB)

    def ask(self, name="maps", rid="r1", form=TWO):
        self.ev("turn.started", name, marker=None)
        self.ev("permission.requested", name, request_id=rid, tool="AskUserQuestion", input=form, interactive=True)
        return f"q{self.seq}"

    def route(self, qid, who="Boris", by="maps", name="maps", **more):
        return self.ev("question.routed", name, id=qid, **({"back": True} if who is None else {"person": who}),
                       address={"teams_chat": "19:b"}, sender=by, **SAY, **more)

    def to(self, name):
        return [m for m in self.st["messages"].values() if m["session"] == name and m.get("origin") == " from core"]

    def card(self, qid, name="maps"):
        return next((a for a in self.st["sessions"][name].get("asks") or [] if a["id"] == qid), None)

    def answer(self, qid, words="tiles.toml, like the files next to it", msg="t1", by="chat", route=None):
        route = route or self.card(qid)["deputy"]["route"]
        return self.ev("question.deputy_answered", "maps", id=qid, route=route, msg=msg, text=words,
                       sender=by, **SAY, **{"from": "Boris"})

    def test_the_session_is_told_of_the_deputies_with_its_card(self):
        self.ev("turn.started", "maps", marker=None)
        self.ev("permission.requested", "maps", request_id="r1", tool="AskUserQuestion", input=TWO, interactive=True)
        (act,) = [a for a in self.actions if a.get("do") == "respond"]
        said = act["response"]["message"]
        self.assertIn("Boris — «map tiles»", said)
        self.assertIn(f"hub question route q{self.seq} <name>", said)
        self.assertIn("a question about the deputy's own request", said)

    def test_away_without_the_flag_tells_the_session_nothing_new(self):
        self.ev("owner.back", None, **WEB)
        self.ev("owner.away", None, **WEB)
        self.ev("turn.started", "maps", marker=None)
        self.ev("permission.requested", "maps", request_id="r1", tool="AskUserQuestion", input=TWO, interactive=True)
        (act,) = [a for a in self.actions if a.get("do") == "respond"]
        self.assertEqual(act["response"]["message"], reducer.ASK_AWAY.format(away=reducer.AWAY, qid=f"q{self.seq}")
                         + reducer.ASK_AWAY_HUB.format(qid=f"q{self.seq}"))

    def test_to_the_session_of_their_channel_with_who_asks_and_why_them(self):
        qid = self.ask()
        self.assertIsNone(self.route(qid, note="the folder has tiles.json already"))
        (msg,) = self.to("chat")
        dep = self.card(qid)["deputy"]
        self.assertEqual((dep["state"], dep["name"], dep["session"]), ("asked", "Boris", "chat"))
        for part in (f"Route {dep['route']}", "[" + dep["route"] + "]", "For Boris to decide", "deputy for «map tiles»",
                     "From the session maps", "tiles_config.toml", "the folder has tiles.json already",
                     f"hub answer maps --card {qid} --route {dep['route']}", "teams_chat 19:b"):
            self.assertIn(part, msg["text"])

    def test_the_answer_comes_to_the_session_as_the_deputys_word(self):
        qid = self.ask()
        self.route(qid)
        self.assertIsNone(self.answer(qid))
        (msg,) = [m for m in self.to("maps") if m.get("deputy_reply")]
        self.assertIn("«tiles.toml, like the files next to it»", msg["text"])
        self.assertIn("It is Boris's word, not the owner's", msg["text"])
        for k in ("from_owner", "answers_question", "re", "on_behalf"):
            self.assertFalse(msg.get(k), k)
        self.assertEqual(self.card(qid)["deputy"]["state"], "answered")   # delivered is not decided: still open
        self.assertIn("is recorded on", self.answer(qid) or "")           # the same Teams message again
        self.assertIsNone(self.answer(qid, words="and the cache too", msg="t2"))   # a second message: taken

    def test_only_the_session_of_the_route_or_the_hub_records_it(self):
        qid = self.ask()
        self.route(qid)
        self.assertIn("are recorded by the session of their channel", self.answer(qid, by="seo") or "")
        self.assertIn("has no route", self.answer(qid, route="r1") or "")
        self.assertIsNone(self.answer(qid, by="hub"))

    def test_the_session_closes_the_card_on_the_answer_and_it_is_in_the_summary(self):
        qid = self.ask()
        self.route(qid)
        self.assertIn("no deputy's answer", self.ev("owner.decided", None, text="x", card=qid, sender="maps",
                                                    **SAY) or "")
        self.answer(qid)
        self.assertIsNone(self.ev("owner.decided", None, text="tiles.toml · Boris, deputy · rename back", card=qid,
                                  sender="maps", **SAY))
        self.assertIsNone(self.card(qid))
        self.ev("owner.back", None, **WEB)
        sm = self.st["owner"]["summary"]
        (row,) = sm["deputies"]
        self.assertEqual((row["card"], row["deputy"], row["via"], row["session"]), (qid, "Boris", "chat", "maps"))
        self.assertEqual(sm["decided"], [])   # not twice

    def test_not_theirs_comes_back_to_the_owner_and_not_to_them_again(self):
        qid = self.ask()
        self.route(qid)
        self.assertIsNone(self.route(qid, who=None))
        self.assertEqual(self.card(qid)["deputy"]["state"], "returned")
        self.assertIn("came back", self.route(qid) or "")
        self.assertIn("is not at a deputy", self.route(qid, who=None) or "")

    def test_refusals(self):
        qid = self.ask()
        self.assertIn("not a deputy of this away", self.route(qid, who="Anna") or "")   # a deputy, not on the flag
        self.assertIn("from the session that asked", self.route(qid, by="seo") or "")
        self.assertIn("no open card", self.route("q999") or "")
        urgent = self.ask("maps", "r2", {"questions": [{"question": "🚨 prod is down: roll back?",
                                                        "options": [{"label": "Yes"}]}]})
        self.assertIn("the owner's own", self.route(urgent) or "")
        self.route(qid)
        self.assertIn("is at Boris already", self.route(qid) or "")
        self.ev("owner.back", None, **WEB)
        qid2 = self.ask("maps", "r3")
        self.assertIn("not away with the deputies", self.route(qid2) or "")

    def test_the_hub_routes_too_and_does_not_answer_a_card_at_a_deputy(self):
        qid = self.ask()
        self.assertIsNone(self.route(qid, by="hub"))
        why = self.ev("permission.answered", "maps", request_id="r1", behavior="allow", card=qid, why="x · y",
                      answers={TWO["questions"][0]["question"]: "tiles.toml"}, **HUB)
        self.assertIn("is at the deputy Boris", why or "")

    def test_a_card_at_a_deputy_does_not_go_to_the_hub_as_an_away_card(self):
        qid = self.ask()
        self.route(qid)
        self.ev("turn.result", "maps", text="asked")
        self.ev("turn.stopped", "maps")
        self.assertEqual([m for m in self.to("hub") if qid in m["text"]], [])

    def test_the_owner_answering_first_takes_the_ground_away(self):
        qid = self.ask()
        self.route(qid)
        self.answer(qid)
        self.ev("permission.answered", "maps", request_id="r1", behavior="allow",
                answers={TWO["questions"][0]["question"]: "tiles_config.toml"}, **WEB)
        (void,) = [m for m in self.to("maps") if "no longer a ground" in m["text"]]
        self.assertIn("the owner answered the card himself", void["text"])

    def test_removing_the_deputy_or_coming_back_returns_the_card(self):
        qid = self.ask()
        self.route(qid)
        self.ev("contact.deputy_off", None, person=BORIS, names=[], **OWNER)
        self.assertEqual(self.card(qid)["deputy"]["state"], "returned")
        (gone,) = [m for m in self.to("maps") if "did not reach Boris" in m["text"]]
        self.assertIn("removed the deputy", gone["text"])

    def test_back_lists_the_cards_still_waiting_and_makes_them_his(self):
        qid = self.ask()
        self.route(qid)
        self.ev("owner.back", None, **WEB)
        sm = self.st["owner"]["summary"]
        self.assertEqual([(w["card"], w["deputy"]) for w in sm["deputies_waiting"]], [(qid, "Boris")])
        self.assertEqual(self.card(qid)["deputy"]["state"], "returned")
        self.assertIn("has no route", self.answer(qid) or "")

    def test_an_answer_after_the_ground_is_gone_is_refused(self):
        qid = self.ask()
        self.route(qid)
        rid = self.card(qid)["deputy"]["route"]
        self.ev("owner.back", None, **WEB)
        self.assertIn("has no route", self.answer(qid, route=rid) or "")

    def test_a_changed_address_in_the_book_is_not_asked(self):
        qid = self.ask()
        why = self.ev("question.routed", "maps", id=qid, person="Boris", address={"teams_chat": "19:zzz"},
                      sender="maps", **SAY)
        self.assertIn("address in the book changed", why or "")
        self.assertIsNone(self.card(qid).get("deputy"))

    def test_a_replay_gives_the_same_state_and_messages(self):
        qid = self.ask()
        self.route(qid)
        self.answer(qid)
        st = reducer.empty()
        for ev in self.events:
            st, _, _ = reducer.reduce(st, ev)
        self.assertEqual(st["deputies"], self.st["deputies"])
        self.assertEqual(self.card(qid), next(a for a in st["sessions"]["maps"]["asks"] if a["id"] == qid))
        self.assertEqual(sorted(m["text"] for m in st["messages"].values()),
                         sorted(m["text"] for m in self.st["messages"].values()))

class AfterReview(Route):
    """What the review of the branch found (/code-review high, 20.11)."""

    def test_a_short_name_is_matched_whole_and_a_declined_one_by_its_start(self):
        self.assertFalse(deputies.names_in("I will also be away", ["Al"]))
        self.assertTrue(deputies.names_in("Al decides the tiles", ["Al"]))
        self.assertTrue(deputies.names_in("Κάνε τον Γιώργο αναπληρωτή", ["Γιώργος"]))
        self.assertTrue(deputies.names_in("ρώτα τη Ζωή", ["Ζωή"]))
        self.assertFalse(deputies.names_in("Annabelle and Annette", ["Ann"]))

    def test_a_decision_on_a_deputys_answer_is_not_the_owners_word_for_a_direction(self):
        qid = self.ask()
        self.route(qid)
        self.answer(qid)
        self.assertIn("the decision itself", self.ev("owner.decided", None, text=" ", card=qid, sender="maps",
                                                    **SAY) or "")
        self.ev("owner.decided", None, text="tiles.toml · Boris, deputy · rename back", card=qid, sender="maps", **SAY)
        d = self.st["decisions"][-1]
        self.assertEqual((d.get("card"), d.get("slug"), d.get("told")), (qid, None, "maps"))   # an undo tells maps
        self.assertFalse([a for a in self.actions if a.get("do") == "board_decision"])

    def test_only_the_deputys_own_words_answer_the_route(self):
        qid = self.ask()
        self.route(qid)
        rid = self.card(qid)["deputy"]["route"]
        why = self.ev("question.deputy_answered", "maps", id=qid, route=rid, msg="t9", text="ship it", sender="chat",
                      **SAY, **{"from": "Mark"})
        self.assertIn("is not Boris", why or "")
        self.assertIsNone(self.ev("question.deputy_answered", "maps", id=qid, route=rid, msg="t9", text="ok",
                                  sender="chat", **SAY, **{"from": "Borya"}))   # his alias

    def test_a_card_back_from_the_deputy_goes_to_the_hub_while_away(self):
        qid = self.ask()
        self.route(qid)
        self.ev("turn.result", "maps", text="asked")
        self.ev("turn.stopped", "maps")
        self.assertEqual([m for m in self.to("hub") if qid in m["text"]], [])
        self.route(qid, who=None)
        (msg,) = [m for m in self.to("hub") if f"hub answer maps --card {qid}" in m["text"]]
        self.assertNotIn("hub question route", msg["text"])   # its one deputy gave it back: no deputy takes it now

    def test_a_module_or_plugin_does_not_turn_the_flag_on(self):
        self.ev("owner.back", None, **WEB)
        for who in ({"channel": "mod:notes", "owner": True}, {"channel": "plugin", "owner": False, "sender": "x"}):
            self.assertIsNotNone(self.ev("owner.away", None, deputies=True, **who), who)
            self.assertNotIn("deputies", self.st["owner"])


class AfterReview2(Route):
    """The second round of the review (/code-review medium, 20.11)."""

    def test_the_owner_answering_sends_the_hub_nothing(self):
        qid = self.ask()
        self.route(qid)
        self.ev("permission.answered", "maps", request_id="r1", behavior="allow",
                answers={TWO["questions"][0]["question"]: "tiles.toml"}, **WEB)
        self.assertEqual([m for m in self.to("hub") if qid in m["text"]], [])

    def test_a_changed_channel_gives_the_card_back_and_tells_both_sessions(self):
        qid = self.ask()
        self.route(qid)
        self.st["sessions"]["chat"]["state"] = "closed"   # the session of his channel is gone
        self.ev("message.received", None, to="maps", text="anything", sender="hub", **SAY)   # a later event checks it
        self.assertEqual(self.card(qid)["deputy"]["state"], "returned")
        self.assertTrue([m for m in self.to("maps") if "did not reach Boris" in m["text"]])
        self.assertTrue([m for m in self.to("hub") if f"--card {qid}" in m["text"]])

    def test_a_withdrawn_card_stops_the_channel(self):
        qid = self.ask()
        self.route(qid)
        rid = self.card(qid)["deputy"]["route"]
        self.ev("question.withdrawn", "maps", id=qid, why="settled", sender="maps", **SAY)
        (stop,) = [m for m in self.to("chat") if f"Route {rid}" in m["text"] and "is closed" in m["text"]]
        self.assertIn("withdrawn", stop["text"])

    def test_undoing_a_deputy_decision_tells_the_session(self):
        qid = self.ask()
        self.route(qid)
        self.answer(qid)
        self.ev("owner.decided", None, text="tiles.toml · Boris, deputy · rename back", card=qid, sender="maps", **SAY)
        seq = self.st["decisions"][-1]["seq"]
        self.ev("owner.decided_revoked", None, seq=seq, **WEB)
        self.assertTrue([m for m in self.to("maps") if f"answer to question {qid} is withdrawn" in m["text"]])

    def test_from_is_the_deputy_whole_not_a_similar_name(self):
        qid = self.ask()
        self.route(qid)
        rid = self.card(qid)["deputy"]["route"]
        why = self.ev("question.deputy_answered", "maps", id=qid, route=rid, msg="t7", text="yes", sender="chat",
                      **SAY, **{"from": "Borisa"})
        self.assertIn("is not Boris", why or "")


class AfterCritic(Route):
    """What the critic of the result found (hub critic, 20.11)."""

    def test_a_piece_of_an_answer_is_not_a_quote(self):
        form = {"questions": [{"question": "What does Anna Lee decide for you?", "options": [{"label": "x"}]}]}
        self.ev("turn.started", "hub", marker=None)
        self.ev("permission.requested", "hub", request_id="rq", tool="AskUserQuestion", input=form, interactive=True)
        qid = f"q{self.seq}"
        self.ev("permission.answered", "hub", request_id="rq", behavior="allow",
                answers={form["questions"][0]["question"]: "everything except production (Recommended)"}, **WEB)
        self.assertIn("is not the owner's words", self.set(who=HUB, zone="production", **{"for": qid}) or "")
        self.assertIsNone(self.set(who=HUB, zone="everything except production", **{"for": qid}))

    def card_answer(self, text):
        form = {"questions": [{"question": "What does Anna Lee decide for you?", "options": [{"label": "x"}]}]}
        self.ev("turn.started", "hub", marker=None)
        self.ev("permission.requested", "hub", request_id="rq", tool="AskUserQuestion", input=form, interactive=True)
        qid = f"q{self.seq}"
        self.ev("permission.answered", "hub", request_id="rq", behavior="allow",
                answers={form["questions"][0]["question"]: text}, **WEB)
        return qid

    def test_the_area_and_does_not_decide_are_two_halves_of_one_answer(self):
        qid = self.card_answer("Everything in maps, including the stage rollouts; except merging into the core")
        why = self.set(who=HUB, zone="Everything in maps, including the stage rollouts",
                       **{"not": "except merging into the core", "for": qid})
        self.assertIsNone(why)
        rec = self.rec()
        self.assertEqual((rec["zone"], rec["not"]), ("Everything in maps, including the stage rollouts",
                                                     "except merging into the core"))

    def test_two_halves_must_be_the_whole_answer_not_what_is_left_of_it(self):
        qid = self.card_answer("Everything in maps, including the stage rollouts; except merging into the core")
        # the exception is dropped from "does not decide": the answer is not carried whole
        self.assertIn("is not the owner's words", self.set(who=HUB, zone="Everything in maps",
                                                            **{"not": "merging into the core", "for": qid}) or "")
        self.assertIn("is not the owner's words", self.set(who=HUB, zone="including the stage rollouts",
                                                            **{"not": "except merging into the core",
                                                               "for": qid}) or "")

    def test_the_answer_is_split_where_the_owner_put_a_sign_not_anywhere(self):
        qid = self.card_answer("Everything in maps; except merging into the core")
        why = self.set(who=HUB, zone="Everything", **{"not": "in maps; except merging into the core", "for": qid})
        self.assertIn("is not the owner's words", why or "")
        qid = self.card_answer("Everything in maps (except the core)")   # the mark in brackets is not a split either
        why = self.set(who=HUB, zone="Everything in maps", **{"not": "except the core", "for": qid})
        self.assertIn("is not the owner's words", why or "")

    def test_the_halves_of_a_recommended_option_are_quoted_without_its_mark(self):
        qid = self.card_answer("Everything in maps; except merging into the core (Recommended)")
        self.assertIsNone(self.set(who=HUB, zone="Everything in maps",
                                   **{"not": "except merging into the core", "for": qid}))
        qid = self.card_answer("Everything in maps; except merging into the core (Recommended)")   # or with it
        self.assertIsNone(self.set(who=HUB, zone="Everything in maps",
                                   **{"not": "except merging into the core (Recommended)", "for": qid}))

    def test_a_bracket_the_owner_wrote_is_not_dropped_from_the_halves(self):
        qid = self.card_answer("Everything in maps; except merging into the core (and billing)")
        why = self.set(who=HUB, zone="Everything in maps", **{"not": "except merging into the core", "for": qid})
        self.assertIn("is not the owner's words", why or "")
        self.assertIsNone(self.set(who=HUB, zone="Everything in maps",
                                   **{"not": "except merging into the core (and billing)", "for": qid}))

    def test_a_half_repeated_in_the_answer_is_found_at_the_right_place(self):
        qid = self.card_answer("maps, maps; except the core")
        self.assertIsNone(self.set(who=HUB, zone="maps, maps", **{"not": "except the core", "for": qid}))

    def test_the_halves_keep_their_order(self):
        qid = self.card_answer("Everything in maps; except merging into the core")
        why = self.set(who=HUB, zone="except merging into the core", **{"not": "Everything in maps", "for": qid})
        self.assertIn("is not the owner's words", why or "")

    def test_a_does_not_decide_that_the_answer_never_said_is_refused(self):
        qid = self.card_answer("Everything in maps; except merging into the core")
        why = self.set(who=HUB, zone="Everything in maps", **{"not": "except payments", "for": qid})
        self.assertIn("is not the owner's words", why or "")

    def test_a_does_not_decide_from_another_card_is_refused(self):
        self.card_answer("except payments")
        qid = self.card_answer("Everything in maps")
        why = self.set(who=HUB, zone="Everything in maps", **{"not": "except payments", "for": qid})
        self.assertIn("is not the owner's words", why or "")
        self.assertIsNone(self.set(who=HUB, zone="Everything in maps", **{"for": qid}))

    def test_an_answer_with_files_attached_on_the_page_is_still_quoted(self):
        # the page puts the paths of attached files under his answer (cards.js withFiles): the answer is still his words
        form = {"questions": [{"question": "What does Anna Lee decide for you?", "options": [{"label": "x"}]}]}
        self.ev("turn.started", "hub", marker=None)
        self.ev("permission.requested", "hub", request_id="rq", tool="AskUserQuestion", input=form, interactive=True)
        qid = f"q{self.seq}"
        self.ev("permission.answered", "hub", request_id="rq", behavior="allow", **WEB,
                answers={form["questions"][0]["question"]: "staging\n\nAttached files:\n- /home/x/uploads/plan.png"})
        self.assertIsNone(self.set(who=HUB, zone="staging", **{"for": qid}))

    def test_a_similar_name_is_not_named(self):
        self.assertFalse(deputies.names_in("Make Maria my deputy", ["Marina"]))
        self.assertTrue(deputies.names_in("Make Marina's team my deputy", ["Marina"]))

    def test_words_of_a_removed_appointment_never_set_it_again(self):
        m1 = self.said(f"Make Anna my deputy: {ZONE}")
        self.assertIsNone(self.set(who=HUB, **{"for": m1}))
        self.ev("contact.deputy_off", None, person=ANNA, names=[], **OWNER)
        m2 = self.said("Make Anna my deputy: the tile cache")
        self.assertIsNone(self.set(who=HUB, zone="the tile cache", **{"for": m2}))
        self.assertIn("already set Anna Lee", self.set(who=HUB, **{"for": m1}) or "")

    def test_a_repeat_message_is_refused_even_after_many(self):
        qid = self.ask()
        self.route(qid)
        for n in range(25):
            self.assertIsNone(self.answer(qid, words=f"part {n}", msg=f"t{n}"))
        self.assertIn("is recorded on", self.answer(qid, msg="t0") or "")
        long = "x" * 300
        self.assertIsNone(self.answer(qid, msg=long))
        self.assertIn("is recorded on", self.answer(qid, msg=long) or "")

    def test_a_changed_address_at_the_answer_gives_the_card_back(self):
        qid = self.ask()
        self.route(qid)
        rid = self.card(qid)["deputy"]["route"]
        self.assertIsNone(self.ev("question.deputy_answered", "maps", id=qid, route=rid, msg="t1", text="ok",
                                  address={"teams_chat": "19:new"}, sender="chat", **SAY, **{"from": "Boris"}))
        self.assertEqual(self.card(qid)["deputy"]["state"], "returned")
        self.assertFalse([m for m in self.to("maps") if m.get("deputy_reply")])

    def test_a_changed_address_at_the_decision_is_refused(self):
        qid = self.ask()
        self.route(qid)
        self.answer(qid)
        why = self.ev("owner.decided", None, text="x · Boris, deputy · y", card=qid, address={"teams_chat": "19:new"},
                      sender="maps", **SAY)
        self.assertIn("no longer a ground", why or "")

    def test_the_summary_has_all_the_deputys_words(self):
        qid = self.ask()
        self.route(qid)
        self.answer(qid, words="tiles.toml" + " pad" * 100, msg="t1")
        self.answer(qid, words="but only after the stage check", msg="t2")
        self.ev("owner.decided", None, text="tiles.toml · Boris, deputy · rename back", card=qid, sender="maps", **SAY)
        self.ev("owner.back", None, **WEB)
        (row,) = self.st["owner"]["summary"]["deputies"]
        self.assertIn("but only after the stage check", row["text"])


class AfterReleaseReview(Route):
    """The review of the release range (/code-review high, 20.11)."""

    def closed(self, rid):
        return [m for m in self.to("chat") if f"Route {rid}" in m["text"] and "is closed" in m["text"]]

    def test_the_session_of_their_channel_that_cannot_reach_them_returns_the_card(self):
        qid = self.ask()
        self.route(qid)
        self.assertIsNone(self.route(qid, who=None, by="chat"))   # as its message from the core tells it
        self.assertEqual(self.card(qid)["deputy"]["state"], "returned")
        self.assertTrue([m for m in self.to("maps") if "did not reach Boris" in m["text"]])
        self.assertTrue([m for m in self.to("hub") if f"--card {qid}" in m["text"]])
        self.assertIn("from the session that asked", self.route(qid, who=None, by="seo") or "")

    def test_a_card_returned_by_the_session_that_asked_closes_the_route(self):
        qid = self.ask()
        self.route(qid)
        rid = self.card(qid)["deputy"]["route"]
        self.route(qid, who=None)
        (stop,) = self.closed(rid)
        self.assertIn("went back to the owner", stop["text"])
        self.assertFalse([m for m in self.to("maps") if "did not reach Boris" in m["text"]])   # it returned it itself

    def test_the_owner_answering_in_words_or_closing_it_closes_the_route(self):
        for how in ("words", "dismissed", "stopped"):
            with self.subTest(how):
                self.setUp()
                qid = self.ask()
                self.route(qid)
                rid = self.card(qid)["deputy"]["route"]
                if how == "words":
                    # from his terminal: words there do not end the away (from the page or Telegram they do)
                    self.ev("message.received", None, to="maps", text="tiles.toml", card=qid, **OWNER)
                elif how == "dismissed":
                    self.ev("question.dismissed", "maps", request_id="r1", **WEB)
                else:
                    self.st["sessions"]["maps"]["role"] = "executor"   # only an executor is stopped at once
                    self.assertIsNone(self.ev("session.stop_requested", "maps", **WEB))
                self.assertIsNone(self.card(qid))
                self.assertEqual(len(self.closed(rid)), 1)

    def test_closing_a_card_without_an_answer_is_not_told_as_his_answer(self):
        qid = self.ask()
        self.route(qid)
        rid = self.card(qid)["deputy"]["route"]
        self.answer(qid)
        self.ev("question.dismissed", "maps", request_id="r1", **WEB)
        self.assertFalse([m for m in self.to("maps") if "answered the card himself" in m["text"]])
        (stop,) = self.closed(rid)
        self.assertIn("withdrawn", stop["text"])

    def test_the_channel_returning_a_card_already_back_hears_that_not_who(self):
        qid = self.ask()
        self.route(qid)
        self.ev("owner.back", None, **WEB)
        self.assertIn("is not at a deputy", self.route(qid, who=None, by="chat") or "")

    def test_earlier_words_of_a_removed_deputy_stay_spent(self):
        m1 = self.said(f"Make Anna my deputy: {ZONE}")
        self.assertIsNone(self.set(who=HUB, **{"for": m1}))
        m2 = self.said("Make Anna my deputy: the tile cache")
        self.assertIsNone(self.set(who=HUB, zone="the tile cache", **{"for": m2}))
        self.ev("contact.deputy_off", None, person=ANNA, names=[], **OWNER)
        self.assertIn("already set Anna Lee", self.set(who=HUB, **{"for": m1}) or "")


class DecidesAll(Route):
    """A deputy who decides everything in their area (owner 22.11: "Deputies may do everything"; "they act for me —
    I am not there in fact"): `hub contacts deputy … --all`, his word as for the record itself. A card of their area
    goes to them with the gates the core keeps for him otherwise (🚨, money, the hub's own card); never a card of the
    liaison or the bus — his correspondence with other hubs, answered only by him. Set again without the mark, or
    --off, and it is gone; the cards it took come back to him."""

    VIA = {"kind": "via", "session": "chat", "address": {"teams_chat": "19:b"}}
    URGENT = {"questions": [{"question": "🚨 prod is down: roll back?", "options": [{"label": "Roll back"}]}]}

    def mark(self, on=True, who=OWNER, zone="map tiles", **more):
        return self.ev("contact.deputy_set", None, person=BORIS, names=["Borya"], zone=zone, path=self.VIA,
                       **({"all": True} if on else {}), **who, **more)

    def boris(self):
        return self.st["deputies"][BORIS["id"]]

    def test_without_the_mark_the_owners_gates_stay_his(self):
        urgent = self.ask("maps", "r2", self.URGENT)
        self.assertIn("the owner's own", self.route(urgent) or "")
        hubs = self.ask("hub", "r3")
        self.assertIn("the owner's own", self.route(hubs, by="hub", name="hub") or "")
        self.assertIsNone(self.card(hubs, "hub").get("deputy"))

    def test_with_the_mark_a_gate_of_their_area_goes_to_them(self):
        self.assertIsNone(self.mark())
        self.assertTrue(self.boris()["all"])
        urgent = self.ask("maps", "r2", self.URGENT)
        self.assertIsNone(self.route(urgent))
        self.assertTrue(self.card(urgent)["deputy"]["all"])
        msg = self.to("chat")[-1]
        self.assertIn("acts for the owner in «map tiles»", msg["text"])
        hubs = self.ask("hub", "r3")
        self.assertIsNone(self.route(hubs, by="hub", name="hub"))
        money = self.ask("maps", "r4")
        self.card(money)["budget_card"] = True
        self.assertIsNone(self.route(money))

    def test_never_a_core_card_of_the_bus(self):
        self.mark()
        qid = self.ask()
        self.card(qid)["hubbus"] = {"item": "b1"}
        self.assertIn("a card of the hub bus", self.route(qid) or "")   # the liaison's own forms: DeputyLiaison

    def test_the_mark_is_the_owners_word_like_the_record(self):
        self.assertIn("a deputy is set by the owner", self.mark(who=HUB) or "")
        self.assertIn("a deputy is set by the owner", self.mark(who={"channel": "session", "owner": False,
                                                                     "sender": "maps"}) or "")
        self.assertNotIn("all", self.boris())
        yes = self.said("Borya, yes")
        self.assertIn("is not the owner's words", self.mark(who=HUB, **{"for": yes}) or "")   # a bare "yes"
        mid = self.said("Borya acts for me while I am away: map tiles, everything in it")
        self.assertIsNone(self.mark(who=HUB, **{"for": mid}))
        self.assertTrue(self.boris()["all"])

    def test_set_again_without_it_or_removed_and_the_cards_come_back(self):
        self.mark()
        urgent = self.ask("maps", "r2", self.URGENT)
        self.route(urgent)
        self.assertIsNone(self.mark(on=False))
        self.assertNotIn("all", self.boris())
        self.assertEqual(self.card(urgent)["deputy"]["state"], "returned")
        self.assertIn("the owner's own", self.route(urgent) or "")
        u2 = self.ask("maps", "r3", self.URGENT)
        self.assertIn("the owner's own", self.route(u2) or "")
        self.mark()
        self.assertIsNone(self.route(u2))
        self.ev("contact.deputy_off", None, person=BORIS, names=[], **OWNER)
        self.assertEqual(self.card(u2)["deputy"]["state"], "returned")

    def test_only_a_deputy_of_this_away(self):
        self.ev("contact.deputy_set", None, person=ANNA, names=[], zone="everything", path=BUS, all=True, **OWNER)
        urgent = self.ask("maps", "r2", self.URGENT)
        self.assertIn("not a deputy of this away", self.route(urgent, who="Anna") or "")
        self.assertIn("the owner's own", self.route(urgent) or "")   # Boris: without the mark

    def test_a_wide_area(self):
        for zone in ("everything except billing", "the whole backend"):
            with self.subTest(zone):
                self.setUp()
                self.assertIsNone(self.mark(zone=zone))
                qid = self.ask("maps", "r2", self.URGENT)
                self.assertIsNone(self.route(qid))
                self.assertIn(f"«{zone}»", self.to("chat")[-1]["text"])

    def test_their_own_request_goes_marked_to_them_and_in_the_summary(self):
        self.mark()
        qid = self.ask()
        self.assertIsNone(self.route(qid, own=True))
        self.assertTrue(self.card(qid)["deputy"]["own"])
        self.assertIn("your own request", self.to("chat")[-1]["text"])
        self.answer(qid)
        self.ev("owner.decided", None, text="tiles.toml · Boris, deputy · rename back", card=qid, sender="maps", **SAY)
        self.ev("owner.back", None, **WEB)
        (row,) = self.st["owner"]["summary"]["deputies"]
        self.assertIn("their own request", row["text"])
        self.assertTrue(row["own"])

    def test_without_the_mark_their_own_request_is_the_owners(self):
        qid = self.ask()
        self.assertIn("their own request", self.route(qid, own=True) or "")

    def test_what_the_sessions_and_the_deputy_are_told(self):
        self.mark()
        self.ev("turn.started", "maps", marker=None)
        self.ev("permission.requested", "maps", request_id="r9", tool="AskUserQuestion", input=TWO, interactive=True)
        (act,) = [a for a in self.actions if a.get("do") == "respond"]
        said = act["response"]["message"]
        hub = deputyroute.hub_text(self.st)
        out = deputyroute._frame(self.st, self.st["sessions"]["maps"], self.card(f"q{self.seq}"), self.boris(), "")
        for text in (said, hub, out):   # the three things the owner keeps whatever the mark: and his hands
            for part in ("personal data", "correspondence", "in the owner's name", "Touch ID"):
                self.assertIn(part, text)
        self.assertIn("Boris — «map tiles» (everything in it)", said)
        self.assertIn("--own", said)

    def test_the_lines_the_owner_sees(self):
        from core import deputycli
        self.mark()
        self.assertIn("decides everything in it", deputycli.line(self.st, self.boris()))
        self.assertIn("everything in it", "\n".join(deputycli.away_lines(self.st, status=True)))
        self.mark(on=False)
        self.assertNotIn("everything", deputycli.line(self.st, self.boris()))


class DecidesAllAfterReview(DecidesAll):
    """What /code-review high found in the branch (22.11)."""

    def said_to(self, name="maps", rid="r9", form=None):
        self.ev("turn.started", name, marker=None)
        self.ev("permission.requested", name, request_id=rid, tool="AskUserQuestion", input=form or TWO,
                interactive=True)
        (act,) = [a for a in self.actions if a.get("do") == "respond"]
        return act["response"]["message"]

    def test_a_session_with_a_card_he_keeps_is_told_of_a_deputy_who_decides_everything(self):
        self.assertNotIn("deputies now", self.said_to(form=self.URGENT))   # no such deputy: as before
        self.assertNotIn("deputies now", self.said_to("hub", "r8"))
        self.mark()
        said = self.said_to(rid="r7", form=self.URGENT)   # 🚨 reaches him at once, and the deputy may take it
        self.assertIn("Boris — «map tiles» (everything in it)", said)
        self.assertIn(f"hub question route q{self.seq} <name>", said)
        self.assertIn("deputies now", self.said_to("hub", "r6"))   # the hub's own card too

    def test_a_bus_card_on_the_hub_is_never_theirs(self):
        self.mark()
        qid = self.ask("hub", "r3")
        self.card(qid, "hub")["hubbus"] = {"item": "b1"}   # no liaison: the bus puts its card on the hub
        self.assertIn("a card of the hub bus", self.route(qid, by="hub", name="hub") or "")

    def test_the_words_of_a_plain_deputy_do_not_set_the_mark(self):
        mid = self.said("Borya decides map tiles for me")
        self.assertIsNone(self.mark(on=False, who=HUB, **{"for": mid}))
        self.assertIn("does not decide everything", self.mark(who=HUB, **{"for": mid}) or "")
        self.assertNotIn("all", self.boris())
        mid = self.said("Borya acts for me on map tiles, everything there")
        self.assertIsNone(self.mark(who=HUB, **{"for": mid}))

    def test_named_again_with_the_mark_the_card_may_go_to_them_again(self):
        qid = self.ask()
        self.route(qid)
        self.mark()   # the owner: everything in it — the card came back with the old appointment
        self.assertEqual(self.card(qid)["deputy"]["state"], "returned")
        self.assertIsNone(self.route(qid))
        self.assertTrue(self.card(qid)["deputy"]["all"])
        self.route(qid, who=None)   # "not mine" under this appointment: not to them again
        self.assertIn("came back", self.route(qid) or "")


class DecidesAllAfterReview2(DecidesAll):
    """The second review of the fixes (medium, 22.11)."""

    def test_a_card_they_gave_back_may_go_to_them_once_they_decide_everything(self):
        qid = self.ask()
        self.route(qid)
        self.route(qid, who=None)   # "not mine" as a plain deputy
        self.assertIn("came back", self.route(qid) or "")
        self.mark()
        self.assertIsNone(self.route(qid))

    def test_the_words_that_set_the_mark_set_it_again(self):
        m1 = self.said("Borya acts for me on map tiles, everything there")
        self.assertIsNone(self.mark(who=HUB, **{"for": m1}))
        m2 = self.said("Borya decides map tiles for me")
        self.assertIsNone(self.mark(on=False, who=HUB, **{"for": m2}))
        self.assertIn("does not decide everything", self.mark(who=HUB, **{"for": m2}) or "")
        self.assertIsNone(self.mark(who=HUB, **{"for": m1}))
        self.assertTrue(self.boris()["all"])

    def test_a_record_from_before_the_mark_counts_its_words_as_plain(self):
        mid = self.said("Borya decides map tiles for me")
        self.assertIsNone(self.mark(on=False, who=HUB, **{"for": mid}))
        del self.boris()["plain"]   # as an earlier core wrote it
        self.assertIn("does not decide everything", self.mark(who=HUB, **{"for": mid}) or "")
        m2 = self.said("Borya acts for me on map tiles, everything there")
        self.assertIsNone(self.mark(who=HUB, **{"for": m2}))
        self.assertEqual(self.boris()["plain"], [mid])

    def answered(self, *texts):
        """His answer to a card of the hub with one question per text: its qN."""
        form = {"questions": [{"question": f"Boris, question {n}?", "options": [{"label": "x"}]}
                              for n, _ in enumerate(texts)]}
        self.ev("turn.started", "hub", marker=None)
        self.ev("permission.requested", "hub", request_id=f"rq{self.seq}", tool="AskUserQuestion", input=form,
                interactive=True)
        qid, rid = f"q{self.seq}", f"rq{self.seq - 1}"
        self.ev("permission.answered", "hub", request_id=rid, behavior="allow",
                answers={q["question"]: t for q, t in zip(form["questions"], texts)}, **WEB)
        return qid

    def test_the_answer_a_record_of_an_older_core_was_made_from_sets_the_mark(self):
        # the hub asked for the mark by a CLI that already knew it, an older reducer dropped it: the record has neither
        # the mark nor "plain", and the same words with --all are refused as the words of a plain deputy
        qid = self.answered("all in the area: map tiles", "as now")
        self.assertIsNone(self.mark(on=False, who=HUB, zone="all in the area: map tiles", **{"for": qid}))
        del self.boris()["plain"]
        self.assertIsNone(self.mark(who=HUB, zone="all in the area: map tiles", **{"for": qid}))
        self.assertTrue(self.boris()["all"])
        self.assertEqual(self.boris()["plain"], [])
        # the mark is the same record with the same words once more: still a fresh mark each time
        self.assertIsNone(self.mark(who=HUB, zone="all in the area: map tiles", **{"for": qid}))

    def test_a_record_of_an_older_core_keeps_its_other_words_plain(self):
        qid = self.answered("all in the area: map tiles", "map tiles only")
        self.assertIsNone(self.mark(on=False, who=HUB, zone="map tiles only", **{"for": qid}))
        del self.boris()["plain"]
        # another answer of the same card than the one the record is made from
        self.assertIn("does not decide everything",
                      self.mark(who=HUB, zone="all in the area: map tiles", **{"for": qid}) or "")
        # the same answer with another "does not decide" than the record has
        qid = self.answered("all in the area: map tiles", "production")
        self.assertIsNone(self.mark(on=False, who=HUB, zone="all in the area: map tiles", **{"for": qid}))
        del self.boris()["plain"]
        self.assertIn("does not decide everything", self.mark(who=HUB, zone="all in the area: map tiles",
                                                              **{"not": "production"}, **{"for": qid}) or "")
        # a free message: the core does not read it, the older words stay plain
        mid = self.said("Borya decides map tiles for me")
        self.assertIsNone(self.mark(on=False, who=HUB, **{"for": mid}))
        del self.boris()["plain"]
        self.assertIn("does not decide everything", self.mark(who=HUB, **{"for": mid}) or "")
        self.assertNotIn("all", self.boris())

    def test_a_card_he_keeps_names_only_the_deputies_who_take_it(self):
        self.ev("contact.deputy_set", None, person=ANNA, names=[], zone=ZONE, path=BUS, **OWNER)
        self.ev("owner.away", None, deputies=["Anna"], **WEB)
        self.mark()
        self.ev("turn.started", "maps", marker=None)
        self.ev("permission.requested", "maps", request_id="r9", tool="AskUserQuestion", input=self.URGENT,
                interactive=True)
        (act,) = [a for a in self.actions if a.get("do") == "respond"]
        self.assertIn("Boris — «map tiles» (everything in it)", act["response"]["message"])
        self.assertNotIn("Anna Lee", act["response"]["message"])


class DeputyGates(DecidesAll):
    """Slice 2 (owner 22.11: "Deputies may do everything… they act for me"): the decision of a deputy who decides
    everything, recorded on a card (`hub decided --card qN`), is `--for qN` for the core's own gates — a tool, an
    executor, a spec, an agent's role — as the owner's answer to that card would be; never a tool with the owner's own
    data (risk owner-data), never naming a deputy. It stops counting when the appointment, the channel or the away
    changes, the decision is withdrawn, or a day passes."""

    CATALOG = {"atlas": {"kind": "mcp", "risk": ["owner-data"], "description": "maps with the owner's sign-in"},
               "tiles@geo": {"kind": "plugin", "risk": ["code"], "plugin_name": "tiles", "description": "tile hooks"}}
    TOOL = {"questions": [{"question": "Connect tiles@geo and atlas to maps for the legend fix?",
                           "options": [{"label": "Connect (Recommended)"}, {"label": "No"}]}]}

    def setUp(self):
        super().setUp()
        self.ev("catalog.loaded", None, tools=self.CATALOG)

    def decided(self, form=None, name="maps", rid="r5", by=None, mark=True):
        if mark:
            self.mark()
        q = self.ask(name, rid, form or self.TOOL)
        self.assertIsNone(self.route(q, by=by or name, name=name))
        self.assertIsNone(self.answer(q, words="Yes, connect them"))
        self.assertIsNone(self.ev("owner.decided", None, text="connect them · Boris, deputy · disconnect", card=q,
                                  sender=by or name, **SAY))
        return q

    def grant(self, tool, ref, name="maps"):
        return self.ev("tools.granted", name, tool=tool, **{"for": ref}, sender="hub", **SAY)

    def test_a_tool_the_card_names_is_granted_on_their_decision(self):
        q = self.decided()
        self.assertIsNone(self.grant("tiles@geo", q))
        self.assertIn("tiles@geo", self.st["sessions"]["maps"]["tools"])

    def test_never_a_tool_with_the_owners_own_data(self):
        q = self.decided()
        self.assertIn("the owner's own data", self.grant("atlas", q) or "")
        self.assertNotIn("atlas", self.st["sessions"]["maps"]["tools"])

    def test_a_card_that_does_not_name_the_tool_is_not_a_yes_to_it(self):
        q = self.decided(self.URGENT)
        self.assertIn("do not name tiles@geo", self.grant("tiles@geo", q) or "")

    def test_a_plain_deputys_decision_opens_no_gate(self):
        q = self.decided(mark=False)
        self.assertIsNotNone(self.grant("tiles@geo", q))
        self.assertNotIn("tiles@geo", self.st["sessions"]["maps"]["tools"])

    def test_it_stops_counting(self):
        for how in ("unmarked", "removed", "back", "a day", "undo", "channel"):
            with self.subTest(how):
                self.setUp()
                q = self.decided()
                if how == "unmarked":
                    self.mark(on=False)
                elif how == "removed":
                    self.ev("contact.deputy_off", None, person=BORIS, names=[], **OWNER)
                elif how == "back":
                    self.ev("owner.back", None, **WEB)
                    self.ev("owner.away", None, deputies=["Boris"], **WEB)   # a new away: a new flag
                elif how == "a day":
                    self.now += 86400 + 5
                elif how == "undo":
                    seq = next(d["seq"] for d in self.st["decisions"] if d.get("card") == q)
                    self.assertIsNone(self.ev("owner.decided_revoked", None, seq=seq, sender="maps", **SAY))
                else:
                    self.st["sessions"]["chat"]["state"] = "closed"   # the session of his channel is gone
                self.assertIsNotNone(self.grant("tiles@geo", q), how)
                self.assertNotIn("tiles@geo", self.st["sessions"]["maps"]["tools"], how)

    def test_an_executor_on_their_decision_and_its_tools(self):
        q = self.decided()
        self.assertIsNone(self.ev("task.run_requested", "maps-fix", task="fix the legend", tools=["tiles@geo", "atlas"],
                                  **{"for": q}, sender="hub", **SAY))
        run = self.st["runs"]["maps-fix"]
        self.assertEqual(run.get("ask"), ["atlas"])   # the owner's own data waits for him
        why = self.ev("task.run_requested", "chat-fix", task="fix it", **{"for": q}, sender="chat", **SAY)
        self.assertIn("--for", why or "")   # another session's card is not its word

    def test_a_spec_on_their_decision(self):
        from core import delegations
        q = self.decided()
        d = {"id": "d7", "for_session": "maps"}
        ev = {"seq": self.seq + 1, "ts": self.now + 1, "data": {"for": q, "sender": "maps", **SAY}}
        self.assertEqual(delegations.spec_who(self.st, d, ev), "maps")
        self.mark(on=False)
        q2 = self.decided(mark=False, rid="r6")
        with self.assertRaises(reducer.Rejected):
            delegations.spec_who(self.st, d, {**ev, "data": {**ev["data"], "for": q2}})

    def test_no_deputy_is_named_on_a_deputys_decision(self):
        q = self.decided()
        why = self.ev("contact.deputy_set", None, person=ANNA, names=[], zone="the whole backend", path=BUS,
                      sender="hub", **SAY, **{"for": q})
        self.assertIsNotNone(why)
        self.assertNotIn(ANNA["id"], self.st.get("deputies") or {})

    def test_the_summary_says_which_gate_it_opened(self):
        q = self.decided()
        self.grant("tiles@geo", q)
        self.ev("owner.back", None, **WEB)
        (row,) = self.st["owner"]["summary"]["deputies"]
        self.assertIn("it opened: a tool", row["text"])


class DeputyGatesAfterReview(DeputyGates):
    """/code-review high of slice 2."""

    def test_the_sessions_own_decision_does_not_name_the_tool(self):
        self.mark()
        q = self.ask("maps", "r5", self.URGENT)
        self.route(q)
        self.answer(q, words="Yes, roll back")
        self.ev("owner.decided", None, text="roll back; and grant tiles@geo · Boris, deputy · —", card=q, sender="maps",
                **SAY)
        self.assertIn("do not name tiles@geo", self.grant("tiles@geo", q) or "")

    def test_an_executors_risky_tool_the_card_does_not_name_waits(self):
        q = self.decided(self.URGENT)
        self.assertIsNone(self.ev("task.run_requested", "maps-fix", task="roll back", tools=["tiles@geo"],
                                  **{"for": q}, sender="hub", **SAY))
        self.assertEqual(self.st["runs"]["maps-fix"].get("ask"), ["tiles@geo"])

    def test_the_liaison_closes_only_its_form_by_the_narrow_command(self):
        from core import liaison
        from hooks import liaison_guard
        for cmd, ok in (("hub decided --card q5 'sent · Boris, deputy · —'", True), ("hub decided list", False),
                        ("hub decided --card q5 --file /home/x/.ssh/id", False), ("hub decided undo 7", False),
                        ("hub decided 'a decision'", False), ("hub task run x", False),
                        ("hub decided --card q5 'sent the list - Boris, deputy'", True),
                        ("hub decided --card q5 -", False), ("hub decided --card q5 --file=/home/x/a", False)):
            self.assertEqual(liaison_guard.plain_hub(cmd), ok, cmd)
        self.assertFalse(liaison.closes_form(["decided", "--card", "q5", "-"]))


class DeputyLiaison(DecidesAll):
    """Slice 2: a card of the liaison (its form "Send" / "Don't send" on words of a session to another hub) goes to a
    deputy who decides everything — the hub sends it, the liaison hears their word and closes the card itself; a core
    card of the bus (whom to trust: a pair code not checked, a link offer, an introduction) stays the owner's."""

    def setUp(self):
        super().setUp()
        self.ev("session.requested", "liaison", cwd="/w", session_id="s-liaison", **WEB)
        self.ev("session.started", "liaison")
        self.st["sessions"]["liaison"]["liaison"] = True

    SEND = {"questions": [{"question": "Send maps-dev's words to masha-hub: «the tile legend moves to the bottom-left»?",
                           "options": [{"label": "Send"}, {"label": "Don't send"}]}]}

    def test_only_a_deputy_who_decides_everything_takes_it(self):
        q = self.ask("liaison", "r5", self.SEND)
        self.assertIn("the liaison's", self.route(q, by="hub", name="liaison") or "")
        self.mark()
        self.assertIsNone(self.route(q, by="hub", name="liaison"))

    def test_the_hub_hears_of_it_while_away(self):
        self.mark()
        q = self.ask("liaison", "r5", self.SEND)
        self.ev("turn.result", "liaison", text="asked")
        self.ev("turn.stopped", "liaison")
        self.assertTrue([m for m in self.to("hub") if q in m["text"]])

    def test_the_liaison_hears_their_word_and_closes_the_card(self):
        self.mark()
        q = self.ask("liaison", "r5", self.SEND)
        self.route(q, by="hub", name="liaison")
        self.ev("question.deputy_answered", "liaison", id=q, route=self.card(q, "liaison")["deputy"]["route"],
                msg="t1", text="Send it", sender="chat", **SAY, **{"from": "Boris"})
        (msg,) = [m for m in self.to("liaison") if m.get("deputy_reply")]
        self.assertIn("Send", msg["text"])
        self.assertIn(f"hub decided --card {q}", msg["text"])
        self.assertNotIn("hub question route", msg["text"])   # closed to the liaison
        self.assertIsNotNone(self.ev("owner.decided", None, text="sent", sender="liaison", **SAY))   # no card: closed
        self.assertIsNone(self.ev("owner.decided", None, text="sent · Boris, deputy · —", card=q, sender="liaison",
                                  **SAY))
        self.assertIsNone(self.card(q, "liaison"))
        self.assertNotIn(q, self.st.get("deputy_words") or {})   # its "Send" opens no gate of the core

    def test_named_anew_the_liaison_is_not_told_to_route_its_form(self):
        self.mark()
        q = self.ask("liaison", "r5", self.SEND)
        self.route(q, by="hub", name="liaison")
        self.mark(zone="map tiles and their legend")
        (gone,) = [m for m in self.to("liaison") if q in m["text"]]
        self.assertIn("did not reach Boris", gone["text"])   # the hub routes its forms: no command it cannot run
        self.assertNotIn("hub question route", gone["text"])


class OneCardOneDeputy(DecidesAll):
    """One card — one deputy (spec d2679763, A6: "the one they pick may give it back; it does not go to a second"). A
    card that was at a deputy and came back — "not mine", the owner changed their area, not delivered — goes to that
    deputy again (named anew) or waits for the owner; the core refuses it to another deputy. 23.11: a mailing launch
    went to the deputy for mailings, came back when the owner widened their area, and the session sent it on to the
    deputy for the backend; the core had told it "do not send it again" and took the route all the same."""

    ANN = {"id": "c0000a0b3", "name": "Ann Lee"}
    ANN_VIA = {"kind": "via", "session": "chat", "address": {"teams_chat": "19:a"}}

    def setUp(self):
        super().setUp()
        self.ev("contact.deputy_set", None, person=self.ANN, names=["Ann"], zone="docs", path=self.ANN_VIA, all=True,
                **OWNER)
        self.ev("owner.away", None, deputies=["Boris", "Ann"], **WEB)

    def to_ann(self, qid, by="maps", name="maps"):
        return self.ev("question.routed", name, id=qid, person="Ann Lee", address={"teams_chat": "19:a"}, sender=by,
                       **SAY)

    def test_a_card_back_from_one_deputy_does_not_go_to_another(self):
        qid = self.ask()
        self.assertIsNone(self.route(qid))
        self.mark()   # the owner widens Boris's area: the card comes back under the old appointment
        self.assertEqual(self.card(qid)["deputy"]["state"], "returned")
        why = self.to_ann(qid) or ""
        self.assertIn("was at Boris", why)
        self.assertEqual(self.card(qid)["deputy"]["name"], "Boris")
        self.assertIsNone(self.route(qid))   # Boris, named anew, may take it again

    def test_given_back_by_the_session_or_the_hub_it_does_not_go_to_another(self):
        for back in ("maps", "hub"):
            with self.subTest(back):
                self.setUp()
                qid = self.ask()
                self.route(qid)
                self.assertIsNone(self.route(qid, who=None, by=back))
                self.assertIn("was at Boris", self.to_ann(qid, by="hub") or "")
                self.assertIn("came back", self.route(qid) or "")

    def test_a_card_no_deputy_had_goes_to_any_of_them(self):
        qid = self.ask()
        self.assertIsNone(self.to_ann(qid))
        self.assertEqual(self.card(qid)["deputy"]["name"], "Ann Lee")

    def test_named_anew_the_session_hears_only_that_deputy_and_the_hub_none(self):
        qid = self.ask()
        self.route(qid)
        self.mark(zone="map tiles and their legend")
        (gone,) = [m for m in self.to("maps") if "came back from Boris" in m["text"]]
        self.assertIn(f'hub question route {qid} "Boris"', gone["text"])
        self.assertIn("Never to another deputy", gone["text"])
        (hub,) = [m for m in self.to("hub") if qid in m["text"]]
        self.assertNotIn("hub question route", hub["text"])   # the session that asked routes it, not both
        self.ev("contact.deputy_off", None, person=BORIS, names=[], **OWNER)   # removed, not named anew: as before
        q2 = self.ask("maps", "r2")
        self.assertIsNone(self.to_ann(q2))
        self.ev("contact.deputy_off", None, person=self.ANN, names=[], **OWNER)
        self.assertIn("did not reach Ann Lee", self.to("maps")[-1]["text"])

    def test_named_anew_without_the_mark_a_card_the_owner_keeps_is_told_as_before(self):
        self.mark()
        urgent = self.ask("maps", "r2", self.URGENT)
        self.assertIsNone(self.route(urgent))   # Boris decides everything: the 🚨 card is his to take
        self.mark(on=False)   # named anew, without the mark: the card is the owner's again, and not Boris's
        (gone,) = [m for m in self.to("maps") if urgent in m["text"]]
        self.assertIn("did not reach Boris", gone["text"])
        self.assertIn("the owner's own", self.route(urgent) or "")

    def test_the_hub_is_not_told_a_deputy_who_gave_the_card_back(self):
        qid = self.ask()
        self.route(qid, by="hub")
        self.route(qid, who=None, by="hub")
        self.assertEqual(deputyroute.hub_text(self.st, self.card(qid)), "")
        self.ev("owner.back", None, **WEB)
        self.ev("owner.away", None, deputies=["Boris", "Ann"], **WEB)
        said = deputyroute.hub_text(self.st, self.card(qid))
        self.assertIn("Ann Lee", said)
        self.assertNotIn("Boris", said)   # gave it back before: ROUTE_TRIED

    def test_a_new_away_is_a_new_start(self):
        qid = self.ask()
        self.route(qid)
        self.ev("owner.back", None, **WEB)
        self.assertEqual(self.card(qid)["deputy"]["state"], "returned")
        self.ev("owner.away", None, deputies=["Ann"], **WEB)
        self.assertIsNone(self.to_ann(qid))


class CardInHeldAway(Base):
    """A card asked while the away is held for the deputies: his words do not end that away, so there is no return to
    count it from (the hub's `hub send maps --for mN --card qN` was refused as "no open card asked before the owner's
    words" with the card open and asked hours before them). It is in front of him from his first word after it, as at
    a return: that word answers none, the next ones may."""

    def setUp(self):
        super().setUp()
        self.set()
        self.ev("owner.away", None, deputies=True, **WEB)

    def ask(self, name="maps", rid="r1"):
        self.ev("turn.started", name, marker=None)
        self.ev("permission.requested", name, request_id=rid, tool="AskUserQuestion", input=TWO, interactive=True)
        return f"q{self.seq}"

    def writes(self, text, to="hub", **who):
        self.assertIsNone(self.ev("message.received", None, to=to, text=text, **(who or TG)))
        return f"m{self.seq}"

    def relay(self, word, qid, to="maps"):
        return self.ev("message.received", None, to=to, text="tiles.toml", card=qid, **{"for": word}, **HUB)

    def card(self, qid, name="maps"):
        return next((a for a in self.st["sessions"][name].get("asks") or [] if a["id"] == qid), None)

    def test_the_hub_names_the_card_his_later_words_answer(self):
        qid = self.ask()
        self.writes("how many were decided without me")
        word = self.writes("tiles.toml")
        self.assertIsNone(self.relay(word, qid))
        self.assertIsNone(self.card(qid))
        self.assertEqual(self.st["messages"][f"m{self.seq}"]["answers_question"], qid)
        self.assertEqual(self.st["owner"]["state"], "away")   # the away stays held
        self.assertTrue(deputies.held(self.st))

    def test_the_first_word_after_it_answers_none(self):
        qid = self.ask()
        first = self.writes("tiles.toml")
        self.assertIn("has no open card", self.relay(first, qid) or "")
        self.assertIsNotNone(self.card(qid))

    def test_a_card_asked_after_his_words_or_closed_or_of_another_session_is_still_refused(self):
        word = self.writes("tiles.toml")
        qid = self.ask()
        self.assertIn("has no open card", self.relay(word, qid) or "")   # asked after them
        self.writes("are you there")
        later = self.writes("tiles.toml")
        self.assertIn("has no open card", self.relay(later, qid, to="chat") or "")   # another session's
        self.assertIsNone(self.relay(later, qid))
        self.assertIn("has no open card", self.relay(later, qid) or "")   # closed by that answer

    def test_words_from_his_terminal_do_not_put_it_in_front_of_him(self):
        qid = self.ask()
        self.writes("are you there", **OWNER)
        word = self.writes("tiles.toml", **OWNER)
        self.assertIn("has no open card", self.relay(word, qid) or "")

    def test_his_words_while_held_count_after_he_is_back(self):
        qid = self.ask()
        self.writes("are you there")
        word = self.writes("tiles.toml")
        self.ev("owner.back", None, **OWNER)
        self.assertIsNone(self.relay(word, qid))

    def test_words_that_name_no_card_answer_none_until_he_is_back(self):
        # the card is not in his Telegram while held: the single open one is not taken for what his words answer
        qid = self.ask()
        self.ask("hub", "r2")
        for to in ("maps", "hub"):
            self.writes("are you there", to=to)
            self.writes("tiles.toml", to=to)
        self.assertIsNotNone(self.card(qid))
        self.assertEqual(len(self.st["sessions"]["hub"]["asks"]), 1)
        self.writes("tiles.toml", to="maps", card=qid, **TG)   # his own words that name it (copied) still do
        self.assertIsNone(self.card(qid))

    def test_the_hubs_note_offers_it_only_for_words_that_may_answer_it(self):
        qid = self.ask()
        first = self.writes("are you there")
        self.assertIsNone(owner.cards_note(self.st, first, self.now, hub="hub"))   # the core would refuse --card
        word = self.writes("tiles.toml")
        self.assertIn(f"{qid} at maps", owner.cards_note(self.st, word, self.now, hub="hub"))
        self.ev("owner.back", None, **OWNER)
        later = self.ask("chat", "r3")   # asked with him here, after his words: listed as before
        self.assertIn(f"{later} at chat", owner.cards_note(self.st, word, self.now, hub="hub"))

    def test_the_card_still_waits_for_his_return(self):
        qid = self.ask()
        self.writes("are you there")
        self.assertTrue(owner.waits_return(self.card(qid)))   # no push, no Telegram, no expiry while held


class Book(unittest.TestCase):
    def test_a_deputy_shows_in_her_record_and_the_file_gives_none(self):
        path = os.path.join(tempfile.mkdtemp(prefix="contacts-"), "contacts.json")
        contacts.add(path, "Anna Lee", email="anna@x.com", source="owner")
        rows = contacts.update(path, lambda rows: [rows[0].update(id=ANNA["id"], deputy=ZONE), rows][1])
        (row,) = contacts.people(path)
        self.assertNotIn("deputy", contacts.line(row))   # the file's own field is nothing
        line = contacts.line(row, deputies={ANNA["id"]: {"on": True, "zone": ZONE, "not": "payments"}})
        self.assertIn(ZONE, line)
        self.assertIn("payments", line)
        self.assertNotIn(ZONE, contacts.line(row, deputies={ANNA["id"]: {"on": False, "zone": ZONE}}))
        self.assertTrue(rows)


if __name__ == "__main__":
    unittest.main()


class Command(unittest.TestCase):
    """`hub contacts deputy` in the owner's terminal: the record gets an id, the channel is the session --via names
    with her addresses now; the line it prints and `hub contacts` show the area."""

    def test_set_list_and_remove(self):
        from tests.test_cli_edges import Base as CliBase

        class Cli(CliBase):
            def runTest(self):
                pass
        cli = Cli()
        cli.setUp()
        book = os.path.join(tempfile.mkdtemp(prefix="contacts-"), "contacts.json")
        contacts.add(book, "Anna Lee", teams_chat="19:abc", aliases=["Anna"], source="owner")
        cli.core()
        run = lambda *a: cli.hub("contacts", *a, HUB_CONTACTS=book, HUB_CLI_WAIT="5")  # noqa: E731
        r = run("deputy", "Anna", ZONE, "--via", "a", "--not", "payments")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"Anna Lee — deputy: «{ZONE}»", r.stdout)
        self.assertIn("does not decide: «payments»", r.stdout)
        self.assertIn("through the session a", r.stdout)
        (row,) = contacts.people(book)
        self.assertRegex(row.get("id") or "", r"c[0-9a-f]{8}")
        self.assertIn(ZONE, run("deputies").stdout)
        self.assertIn(f"deputy for «{ZONE}»", run().stdout)
        r = cli.hub("away", "--hours", "2", "--deputies", "Anna", HUB_CONTACTS=book, HUB_CLI_WAIT="5")
        self.assertIn(f"go to them: Anna Lee («{ZONE}»)", r.stdout, r.stderr)
        r = run("deputy", "Anna", ZONE, "--via", "a", "--all")   # she decides everything in it
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("decides everything in it", r.stdout)
        r = cli.hub("away", "--status", HUB_CONTACTS=book)
        self.assertIn(f"Anna Lee («{ZONE}», everything in it)", r.stdout, r.stderr)
        self.assertNotEqual(run("deputy", "Anna", "--off", "--all").returncode, 0)   # the usage
        r = run("deputy", "Anna", ZONE, "--via", "a")   # set again without it: the mark is gone, and it says so
        self.assertNotIn("decides everything in it", r.stdout)
        self.assertIn("Anna Lee no longer decides everything in the area", r.stdout)
        r = cli.hub("away", "--status", HUB_CONTACTS=book)
        self.assertIn("at the deputies now: 0", r.stdout, r.stderr)
        cli.hub("back", HUB_CLI_WAIT="5")
        r = run("deputy", "Anna", "--off")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("No deputies", run("deputies").stdout)
        self.assertNotEqual(run("deputy", "Nobody", ZONE).returncode, 0)
        self.assertNotEqual(run("deputy", "Anna").returncode, 0)   # no area: the usage


class RouteCommand(unittest.TestCase):
    """`hub question route`, `hub answer --route`, `hub decided --card` from sessions, through the real CLI."""

    def cards(self):
        """A hub away with Boris as a deputy and a card of session a: (the CLI test base, hub(...), qN)."""
        from tests.test_cli_edges import Base as CliBase

        class Cli(CliBase):
            def runTest(self):
                pass
        cli = Cli()
        cli.setUp()   # sessions a and b
        book = os.path.join(tempfile.mkdtemp(prefix="contacts-"), "contacts.json")
        contacts.add(book, "Boris", teams_chat="19:b", source="owner")
        contacts.update(book, lambda rows: rows[0].update(id=BORIS["id"]))
        cli.j.append("contact.deputy_set", None, {"person": BORIS, "names": [], "zone": "map tiles", **OWNER,
                                                  "path": {"kind": "via", "session": "b",
                                                           "address": {"teams_chat": "19:b"}}})
        cli.j.append("owner.away", None, {"deputies": True, **WEB})
        cli.j.append("turn.started", "a", {"marker": None})
        ev = cli.j.append("permission.requested", "a", {"request_id": "r1", "tool": "AskUserQuestion", "input": TWO,
                                                        "interactive": True})
        cli.core()
        return cli, lambda *a, me: cli.hub(*a, me=me, HUB_CONTACTS=book, HUB_CLI_WAIT="5"), f"q{ev['seq']}"

    def test_show_and_status_say_who_has_the_card(self):
        cli, hub, qid = self.cards()
        line = lambda: hub("show", qid, me="a").stdout.splitlines()[1]  # noqa: E731
        status = lambda: next(x for x in hub("status", me="a").stdout.splitlines() if qid in x)  # noqa: E731
        self.assertEqual(line(), "state: open — waits for the owner's answer")
        self.assertIn(f"{qid} waits for the owner", status())
        self.assertEqual(hub("question", "route", qid, "Boris", me="a").returncode, 0)
        self.assertEqual(line(), "state: open — routed to Boris (deputy), waits for their answer")
        self.assertIn(f"{qid} is at Boris (deputy), waits for their answer", status())
        self.assertNotIn("waits for the owner", status())
        route = next(a for a in cli_state(cli)["sessions"]["a"]["asks"] if a["id"] == qid)["deputy"]["route"]
        hub("answer", "a", "--card", qid, "--route", route, "--from", "Boris", "--msg", "t1", "tiles.toml", me="b")
        # nobody waits any more: not "open" (a session reads that as the owner's wait), and the line names the command
        self.assertEqual(line(), f'state: answered by Boris (deputy) — close the card: hub decided --card {qid} '
                                 '"<what> · Boris, deputy · how to roll back"')
        self.assertIn(f"{qid} answered by Boris (deputy), not waiting for the owner", status())
        self.assertNotIn("waits for the owner", status())
        self.assertEqual(hub("question", "route", qid, "--owner", me="a").returncode, 0)   # back to the owner
        self.assertEqual(line(), "state: open — waits for the owner's answer")
        self.assertIn(f"{qid} waits for the owner", status())

    def test_the_route_line_tells_whose_answer_is_whose(self):
        """After the route the session reads who is who in the answers it will get: the owner's head and the deputy's."""
        cli, hub, qid = self.cards()
        out = hub("question", "route", qid, "Boris", me="a").stdout
        self.assertIn(reducer.OWNER_ANSWER_HEAD.format(id=qid), out)
        self.assertIn(f"Reply on {qid} from Boris", out)
        self.assertTrue(deputyroute.REPLY.startswith("Reply on {qid} from {name}"))   # the head the line promises
        self.assertNotIn("owner himself", hub("question", "route", qid, "--owner", me="a").stdout)

    def test_route_answer_and_close(self):
        cli, hub, qid = self.cards()
        hub("question", "route", qid, "Boris", "--own", me="a")   # their own request (refused by the fold: no mark)
        import json
        with open(os.path.join(cli.state, "journal.jsonl"), encoding="utf-8") as f:
            routed = [json.loads(x)["data"] for x in f if '"question.routed"' in x]
        self.assertEqual([(d["person"], d.get("own")) for d in routed], [("Boris", True)])
        self.assertIsNone(next(a for a in cli_state(cli)["sessions"]["a"]["asks"] if a["id"] == qid).get("deputy"))
        r = hub("question", "route", qid, "Boris", me="a")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("went to Boris through b", r.stdout)
        route = next(a for a in cli_state(cli)["sessions"]["a"]["asks"] if a["id"] == qid)["deputy"]["route"]
        r = hub("answer", "a", "--card", qid, "--route", route, "--from", "Boris", "--msg", "t1", "tiles.toml", me="b")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = hub("decided", "--card", qid, "tiles.toml · Boris, deputy · rename back", me="a")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse([a for a in cli_state(cli)["sessions"]["a"].get("asks") or [] if a["id"] == qid])
        self.assertNotEqual(hub("question", "route", qid, me="a").returncode, 0)


def cli_state(cli):
    from core import journal
    return journal.load_state(cli.state)


class ThePageOfHubs(unittest.TestCase):
    """Settings → External hubs: the hub's card shows its person's area as the owner's deputy. The page's view reads a
    pick of the state (busweb.state): the deputies must be in it (the stand showed the card without the line)."""

    def test_the_card_has_the_area(self):
        import types
        from core import busflow
        from modules.web import busweb
        st = {"sessions": {"hub": {"kind": "hub", "state": "idle"}},
              "bus": {"enabled": True, "contacts": {"anna-hub": {"fp": "SHA256:anna", "status": "known", "verified": True,
                                                                 "person": ANNA, "label": "Anna's hub"}}},
              "deputies": {ANNA["id"]: {"person": ANNA, "zone": ZONE, "not": "payments", "on": True, "seq": 5,
                                        "channel": {"kind": "bus", "contact": "anna-hub", "fp": "SHA256:anna"}}}}
        tail = types.SimpleNamespace(offset=10, BUSY=object(), peek=lambda fn, *a, **k: fn(types.SimpleNamespace(state=st)))
        web = types.SimpleNamespace(tail=tail, state_dir=tempfile.mkdtemp(prefix="busweb-"), READ_WAIT=1)
        picked = busweb.BusWeb(web).state()
        (c,) = busflow.page_view(picked)["contacts"]
        self.assertEqual(c["deputy"], {"zone": ZONE, "not": "payments"})
        st["bus"]["contacts"]["anna-hub"]["fp"] = "SHA256:other"
        (c,) = busflow.page_view(busweb.BusWeb(web).state())["contacts"]
        self.assertIn("another key", c["deputy"]["off"])
