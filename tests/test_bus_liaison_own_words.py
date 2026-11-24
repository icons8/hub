"""The liaison's own words (`hub bus send <contact> --text "…"`) in a turn on one session's words continue that session's
thread with the other hub (spec d2423478), whether the session named the contact (`--to`) or not, and a thread it named
(`--thread`) wins; a turn on two sessions' words opens a new thread, and the answer in it comes to the liaison. The
liaison's decline of an answer in a thread this hub opened is refused (`decline_ours`): nothing goes to the other hub,
the liaison closes it (done) or answers in the thread. A `--thread` of a few first characters (or the fence mark of a
message) is refused, and the refusal names the full id when one live thread with that hub fits."""
import tests  # noqa: F401 — keeps the machine's settings out
import unittest

from core import busstate, liaison
from tests.test_hubbus import need_lib, need_ssh
from tests.test_hubbus_s1 import acquainted
from tests.test_hubbus_threads import answer, ask, delivered, item_of

FORM = {"questions": [{"question": "Send to bee: «Deploy is at 10:00»?", "header": "Bus", "multiSelect": False,
                       "options": [{"label": "Send", "description": ""}, {"label": "Don't send", "description": ""}]}]}


@need_lib
@need_ssh
class OwnWordsKeepTheSessionsThread(unittest.TestCase):
    def setUp(self):
        self.a, self.b, self.wire = acquainted()
        self.nb = next(iter(self.b.bus["contacts"]))
        first = ask(self.a, self.wire, "api", "Which schema does /v1/items take?")   # a live thread api <-> bee
        self.thread = first["thread"]
        answer(self.b, self.nb, self.wire, self.thread, "Schema v2")
        self.a.rec("turn.started", {"marker": delivered(self.a, "api", "Schema v2")["id"]}, entity="api")

    def own(self, text="Deploy is at 10:00", **extra):
        ev, why = self.a.as_liaison("bus.send_requested", {"contact": "bee", "text": text, **extra})
        self.assertIsNone(why)
        self.wire.settle()
        return self.a.bus["out"][f"o{ev['seq']}"]

    def back(self, thread, text="OK"):
        """bee answers in the thread: who of A gets it."""
        answer(self.b, self.nb, self.wire, thread, text)
        return self.a.bus["inbox"][item_of(self.a, text)].get("passed_to") or []

    def test_words_without_to_continue_the_sessions_thread(self):   # S6
        m1, why = self.a.say("api", "Tell them: deploy is at 10:00")
        self.assertIsNone(why)
        self.a.rec("turn.started", {"marker": m1}, entity=liaison.NAME)
        o = self.own()
        self.assertEqual(o["thread"], self.thread)
        self.assertTrue(o.get("thread_auto"))
        self.assertEqual(self.back(self.thread), ["api"])

    def test_the_thread_is_the_one_of_the_turn_the_session_wrote_in(self):
        m1, _ = self.a.say("api", "Tell them: deploy is at 10:00")          # in its turn on bee's words in the thread
        other = self.own("Another matter: the exporter")["thread"]          # then bee writes in another thread to api
        answer(self.b, self.nb, self.wire, other, "Exporter question for api")
        bid = item_of(self.a, "Exporter question for api")
        _, why = self.a.as_liaison("bus.pass_requested", {"inbox": bid, "session": "api"})
        self.assertIsNone(why)
        self.a.rec("turn.started", {"marker": delivered(self.a, "api", "Exporter question for api")["id"]}, entity="api")
        self.a.rec("turn.started", {"marker": m1}, entity=liaison.NAME)
        o = self.own()
        self.assertEqual(o["thread"], self.thread)                          # not the thread api is on now

    def test_the_thread_it_wrote_in_that_no_longer_takes_the_words_is_not_swapped_for_another(self):
        other = self.own("Another matter: the exporter")["thread"]
        answer(self.b, self.nb, self.wire, other, "Exporter question for api")
        _, why = self.a.as_liaison("bus.pass_requested", {"inbox": item_of(self.a, "Exporter question for api"),
                                                         "session": "api"})
        self.assertIsNone(why)
        m1, _ = self.a.say("api", "Tell them: deploy is at 10:00")          # still in its turn on the schema thread
        self.a.bus["threads"][self.thread]["n"] = busstate.THREAD_MAX       # which is full now
        self.a.rec("turn.started", {"marker": m1}, entity=liaison.NAME)
        o = self.own()
        self.assertNotIn(o["thread"], (self.thread, other))                 # a new one, not the exporter's

    def test_a_script_on_its_behalf_carries_no_thread_of_its_turn(self):
        ev = self.a.rec("message.received", {"to": liaison.NAME, "text": "weekly digest", "channel": "session",
                                             "owner": False, "sender": "api", "schedule_job": "j1"})
        self.assertNotIn("bus_turn_threads", self.a.st["messages"].get(f"m{ev['seq']}") or {})

    def test_an_fyi_in_its_turn_carries_the_thread_too(self):
        ev = self.a.rec("message.received", {"to": liaison.NAME, "text": "deploy at 10", "channel": "session",
                                             "owner": False, "sender": "api", "fyi": True})
        self.assertEqual(self.a.st["messages"][f"m{ev['seq']}"].get("bus_turn_threads"), {"bee": self.thread})

    def test_a_kept_thread_that_no_longer_takes_the_words_is_not_swapped_for_another(self):
        m1, _ = self.a.say("api", "Tell them: deploy is at 10:00", bus_to="bee", bus_to_via="local")
        self.assertEqual(self.a.bus["requests"][m1]["thread"], self.thread)
        self.a.bus["threads"][self.thread]["n"] = busstate.THREAD_MAX       # full now
        self.a.rec("turn.started", {"marker": m1}, entity=liaison.NAME)
        o = self.own()
        self.assertNotEqual(o["thread"], self.thread)
        self.assertEqual(o["thread"], o["id"])                              # a new one

    def test_words_without_to_by_reference_continue_it_too(self):
        m1, _ = self.a.say("api", "Deploy is at 10:00")
        self.a.rec("turn.started", {"marker": m1}, entity=liaison.NAME)
        ev, why = self.a.as_liaison("bus.send_requested", {"contact": "bee", "text_from": m1})
        self.assertIsNone(why)
        self.wire.settle()
        o = self.a.bus["out"][f"o{ev['seq']}"]
        self.assertEqual(o["thread"], self.thread)
        self.assertEqual(self.back(self.thread, "OK, 10:00"), ["api"])

    def test_his_correction_on_words_without_to_refuses_the_old_words(self):
        m1, _ = self.a.say("api", "Deploy is at 10:00")
        self.asked_about(m1)
        self.a.rec("permission.answered", {"request_id": "r1", "behavior": "allow", "channel": "web", "owner": True,
                                           "answers": {FORM["questions"][0]["question"]: "Send, but 11:00"}},
                   entity=liaison.NAME)
        mo = next(m["id"] for m in reversed(self.a.messages_to(liaison.NAME)) if m.get("answers_question"))
        self.a.rec("turn.started", {"marker": mo}, entity=liaison.NAME)
        ev, why = self.a.as_liaison("bus.send_requested", {"contact": "bee", "text_from": m1})
        self.assertTrue(str(why).startswith("owner_changed:"), why)
        o = self.own("Deploy is at 11:00")
        self.assertEqual(o["thread"], self.thread)                          # api's conversation, its turn then
        self.assertEqual(self.back(self.thread, "OK, 11:00"), ["api"])

    def asked_about(self, m1):
        """The liaison's form about m1 in its turn on it; the turn ends (the form is a card)."""
        self.a.rec("turn.started", {"marker": m1}, entity=liaison.NAME)
        self.a.rec("permission.requested", {"request_id": "r1", "tool": "AskUserQuestion", "interactive": True,
                                            "input": FORM}, entity=liaison.NAME)
        self.a.rec("turn.ended", {}, entity=liaison.NAME)

    def test_his_correction_by_reference_on_words_without_to_continues_the_sessions_thread(self):
        m1, _ = self.a.say("api", "Deploy is at 10:00")
        self.asked_about(m1)
        ev = self.a.rec("message.received", {"to": liaison.NAME, "text": "Deploy is at 11:00", "channel": "web",
                                             "owner": True})
        mo = f"m{ev['seq']}"
        self.a.rec("turn.started", {"marker": mo}, entity=liaison.NAME)
        ev, why = self.a.as_liaison("bus.send_requested", {"contact": "bee", "text_from": mo})
        self.assertIsNone(why)
        self.wire.settle()
        o = self.a.bus["out"][f"o{ev['seq']}"]
        self.assertEqual((o["thread"], busstate.whose(o)), (self.thread, "api"))
        _, why = self.a.as_liaison("bus.send_requested", {"contact": "bee", "text_from": m1})
        self.assertIsNotNone(why)                                           # answered by his words already

    def test_after_his_x_on_words_without_to_his_later_words_are_his_own(self):
        m1, _ = self.a.say("api", "Deploy is at 10:00")
        self.asked_about(m1)
        self.a.rec("question.dismissed", {"request_id": "r1", "channel": "web", "owner": True}, entity=liaison.NAME)
        ev = self.a.rec("message.received", {"to": liaison.NAME, "text": "Tell bee hello from me", "channel": "web",
                                             "owner": True})
        self.a.rec("turn.started", {"marker": f"m{ev['seq']}"}, entity=liaison.NAME)
        o = self.own("Hello from the owner")
        self.assertIsNone(o.get("asker"))
        self.assertNotEqual(o["thread"], self.thread)
        _, why = self.a.as_liaison("bus.send_requested", {"contact": "bee", "text_from": m1})
        self.assertIsNone(why)                                              # not a correction of api's words

    def test_words_with_to_continue_it_as_before(self):   # S7, control
        m1, _ = self.a.say("api", "Tell them: deploy is at 10:00", bus_to="bee", bus_to_via="local")
        self.a.rec("turn.started", {"marker": m1}, entity=liaison.NAME)
        o = self.own()
        self.assertEqual(o["thread"], self.thread)
        self.assertEqual(self.a.bus["requests"][m1]["outcome"], "sent")

    def test_the_thread_the_session_named_wins(self):
        other = self.own("The exporter is out")["thread"]   # the liaison's own thread, nobody's yet
        self.assertNotEqual(other, self.thread)
        m1, why = self.a.say("api", "About the exporter: v2 it is", bus_to="bee", bus_to_via="local", bus_thread=other)
        self.assertIsNone(why)
        self.a.rec("turn.started", {"marker": m1}, entity=liaison.NAME)
        o = self.own("About the exporter: we take v2")
        self.assertEqual(o["thread"], other)                     # not the latest thread of bee's words to api
        self.assertEqual(self.a.bus["requests"][m1]["outcome"], "sent")

    def test_words_of_a_session_with_no_live_thread_open_a_new_one(self):   # control
        self.a.up("docs")
        m1, _ = self.a.say("docs", "Tell them: docs moved")
        self.a.rec("turn.started", {"marker": m1}, entity=liaison.NAME)
        o = self.own("Docs moved to /v2")
        self.assertNotEqual(o["thread"], self.thread)

    def test_two_sessions_in_the_turn_open_a_new_thread_and_the_answer_comes_to_the_liaison(self):   # S8
        m1, _ = self.a.say("api", "Tell them: deploy is at 10:00", bus_to="bee", bus_to_via="local")
        self.a.up("web")
        m2, _ = self.a.say("web", "Anything for the release notes?")
        self.a.rec("turn.started", {"marker": m1, "also": [m2]}, entity=liaison.NAME)
        o = self.own()
        self.assertNotEqual(o["thread"], self.thread)
        self.assertIsNone(o.get("asker"))
        self.back(o["thread"], "Noted, 10:00")
        item = self.a.bus["inbox"][item_of(self.a, "Noted, 10:00")]
        self.assertEqual(item.get("passed_to") or [], [])
        self.assertTrue(any("Noted, 10:00" in m["text"] for m in self.a.messages_to(liaison.NAME)))   # not lost


@need_lib
@need_ssh
class DeclineInOurThread(unittest.TestCase):
    def setUp(self):
        self.a, self.b, self.wire = acquainted()
        self.nb = next(iter(self.b.bus["contacts"]))

    def opened_by_a(self):
        ev, why = self.a.as_liaison("bus.send_requested", {"contact": "bee", "text": "The exporter is out, please check"})
        self.assertIsNone(why)
        self.wire.settle()
        return self.a.bus["out"][f"o{ev['seq']}"]["thread"]

    def test_a_decline_of_an_answer_in_our_thread_goes_nowhere(self):
        thread = self.opened_by_a()
        answer(self.b, self.nb, self.wire, thread, "Checked, all fine")
        bid = item_of(self.a, "Checked, all fine")
        before = len(self.b.bus["inbox"])
        _, why = self.a.as_liaison("bus.decline_requested", {"inbox": bid, "why": "already sent, not sending again"})
        self.assertTrue(str(why).startswith("decline_ours:"), why)
        self.assertIn(f"hub bus done {bid}", why)
        self.assertIn(f"--thread {thread}", why)
        self.wire.settle()
        self.assertEqual(len(self.b.bus["inbox"]), before)                         # nothing reached bee
        self.assertIsNone(self.a.bus["inbox"][bid]["outcome"])
        _, why = self.a.as_liaison("bus.done_requested", {"inbox": bid})
        self.assertIsNone(why)
        self.assertEqual(self.a.bus["inbox"][bid]["outcome"], "read")

    def test_the_liaison_reads_that_it_is_an_answer_to_this_hubs_words(self):
        thread = self.opened_by_a()
        answer(self.b, self.nb, self.wire, thread, "Checked, all fine")
        msg = next(m for m in reversed(self.a.messages_to(liaison.NAME)) if "Checked, all fine" in m["text"])
        self.assertIn(str(liaison.IN_OURS), msg["text"])

    def test_its_frame_offers_no_decline_of_its_own_words(self):
        thread = self.opened_by_a()
        answer(self.b, self.nb, self.wire, thread, "Checked, all fine")
        bid = item_of(self.a, "Checked, all fine")
        msg = next(m for m in reversed(self.a.messages_to(liaison.NAME)) if "Checked, all fine" in m["text"])
        self.assertNotIn(f"hub bus decline {bid}", msg["text"])
        self.assertIn(f"hub bus done {bid}", msg["text"])

    def test_a_decline_in_their_thread_still_tells_them(self):   # control
        b_thread = None
        _, why = self.b.as_liaison("bus.send_requested", {"contact": self.nb, "text": "Can you send the build log?"})
        self.assertIsNone(why)
        self.wire.settle()
        bid = item_of(self.a, "Can you send the build log?")
        b_thread = self.a.bus["inbox"][bid]["thread"]
        msg = next(m for m in reversed(self.a.messages_to(liaison.NAME)) if "build log" in m["text"])
        self.assertNotIn(str(liaison.IN_OURS), msg["text"])
        _, why = self.a.as_liaison("bus.decline_requested", {"inbox": bid, "why": "not ours to share"})
        self.assertIsNone(why)
        self.wire.settle()
        got = [x for x in self.b.bus["inbox"].values() if x.get("thread") == b_thread]
        self.assertTrue(any(busstate.is_decline_notice(x["text"]) for x in got))


@need_lib
@need_ssh
class AShortThreadId(unittest.TestCase):
    def setUp(self):
        self.a, self.b, self.wire = acquainted()
        self.thread = ask(self.a, self.wire, "api", "Which schema does /v1/items take?")["thread"]

    def test_the_liaisons_refusal_names_the_full_id(self):
        _, why = self.a.as_liaison("bus.send_requested", {"contact": "bee", "text": "v2", "thread": self.thread[:8]})
        self.assertTrue(str(why).startswith("unknown_thread:"), why)
        self.assertIn(self.thread, why)

    def test_a_sessions_refusal_names_it_too(self):
        _, why = self.a.say("api", "v2 it is", bus_to="bee", bus_to_via="local", bus_thread=self.thread[:8])
        self.assertIn(self.thread, str(why))

    def test_no_guess_when_two_threads_start_so(self):
        twin = self.thread[:8] + "0" * 24
        self.a.bus["threads"][twin] = {**self.a.bus["threads"][self.thread]}
        _, why = self.a.as_liaison("bus.send_requested", {"contact": "bee", "text": "v2", "thread": self.thread[:8]})
        self.assertTrue(str(why).startswith("unknown_thread:"), why)
        self.assertNotIn(self.thread, why)

    def test_the_fence_mark_of_their_message_names_its_thread(self):
        answer(self.b, next(iter(self.b.bus["contacts"])), self.wire, self.thread, "Schema v2")
        marker = self.a.bus["inbox"][item_of(self.a, "Schema v2")]["marker"]
        self.assertFalse(self.thread.startswith(marker[4:]))                     # random, not a part of the id
        _, why = self.a.as_liaison("bus.send_requested", {"contact": "bee", "text": "v2", "thread": marker})
        self.assertIn(self.thread, str(why))

    def test_the_mark_copied_with_its_brackets_names_it_too(self):
        answer(self.b, next(iter(self.b.bus["contacts"])), self.wire, self.thread, "Schema v2")
        marker = self.a.bus["inbox"][item_of(self.a, "Schema v2")]["marker"]
        _, why = self.a.as_liaison("bus.send_requested", {"contact": "bee", "text": "v2", "thread": "<<<" + marker})
        self.assertIn(self.thread, str(why))

    def test_a_prefix_is_never_taken_as_the_thread(self):
        _, why = self.a.as_liaison("bus.send_requested", {"contact": "bee", "text": "v2", "thread": self.thread[:8]})
        self.assertIsNotNone(why)
        self.assertFalse([o for o in self.a.bus["out"].values() if o.get("text") == "v2"])


if __name__ == "__main__":
    unittest.main()
