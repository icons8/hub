"""An "FYI" message (`hub send <to> --fyi`): a status without a question does not wake the addressee and does not start
a turn for it (friction digest: 143 turns on such messages with no outgoing reaction). The core keeps it at the
addressee separately from the queue — the aside state: it is not "in the queue", so neither the watchdog ("not
delivered"), nor parking, nor sleeping, nor the hub ("X is busy — decide") waits for it. It arrives with the next
message the core prints to the addressee, in a "For your information" block."""
import tests  # noqa: F401 — ignore this machine's settings
import os
import subprocess
import sys
import tempfile
import unittest

from core import forms, guards, journal, parking, reducer, spool, views

OWNER = {"channel": "web", "owner": True}
MAPS = {"channel": "session", "owner": False, "sender": "maps"}
HUB = {"channel": "session", "owner": False, "sender": "hub"}
BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")


class Core(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.acts = reducer.empty(), 0, []
        self.ev("session.requested", "hub", cwd="/h", session_id="s-h", kind="hub", **OWNER)
        self.ev("session.requested", "devops", cwd="/k", session_id="s-d", role="agent", about="DevOps", **OWNER)
        self.ev("session.requested", "maps", cwd="/m", session_id="s-m", role="agent", about="Maps", **OWNER)
        for n in ("hub", "devops", "maps"):
            self.ev("session.started", n)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 1000 + self.seq, "type": type,
                                                      "entity": entity, "data": data})
        self.acts += acts
        return why

    def typed(self, mid):
        return [a for a in self.acts if a["do"] == "type" and a["message"] == mid]

    def send(self, text, who=MAPS, to="devops", **extra):
        why = self.ev("message.received", None, to=to, text=text, **who, **extra)
        return why, self.st["messages"].get(f"m{self.seq}")

    def deliver(self, mid, name="devops"):
        self.ev("turn.started", name, marker=mid, prompt_head="…")

    def test_a_free_session_is_not_given_a_turn_and_the_next_message_carries_it(self):
        why, fyi = self.send("task 12 went to review, nothing to do", fyi=True)
        self.assertIsNone(why)
        self.assertEqual((fyi["state"], fyi.get("fyi")), ("aside", True))
        self.assertEqual(self.typed(fyi["id"]), [])                             # not typed: no turn
        sess = self.st["sessions"]["devops"]
        self.assertEqual(sess["state"], "idle")
        self.assertTrue(parking.parkable(self.st, sess))                         # does not hold the process
        self.assertIsNone(guards.closable(self.st, {**sess, "role": "worker"}))   # and it can be put to sleep
        _, task = self.send("look into why the deploy failed", who=OWNER)
        (carrier,) = self.typed(task["id"])
        self.assertIn("look into why the deploy failed", carrier["text"])
        self.assertIn("For your information", carrier["text"])
        self.assertIn("task 12 went to review", carrier["text"])
        self.assertIn(f"— {fyi['id']} from maps: task 12", carrier["text"])
        self.assertEqual(self.st["messages"][fyi["id"]]["state"], "aside")       # delivered — when the carrier arrived
        self.deliver(task["id"])
        m = self.st["messages"][fyi["id"]]
        self.assertEqual(m["state"], "delivered")
        self.assertEqual(m["delivered_at"], self.st["messages"][task["id"]]["delivered_at"])
        self.ev("turn.result", "devops", text="ok")
        self.send("more", who=OWNER)
        self.assertNotIn("task 12", self.acts[-1]["text"])                          # does not ride a second time

    def test_it_does_not_wake_a_sleeping_or_parked_session(self):
        self.assertIsNone(self.ev("session.parked", "devops", channel="core"))
        self.acts.clear()
        _, fyi = self.send("no reply from the colleague yet", fyi=True)
        self.assertEqual(self.st["sessions"]["devops"]["state"], "parked")
        self.assertEqual([a for a in self.acts if a["do"] in ("spawn", "type")], [])
        _, task = self.send("renew the certificate", who=OWNER)                      # a regular one wakes, as before
        self.assertEqual(self.st["sessions"]["devops"]["state"], "starting")
        self.ev("session.started", "devops")
        (carrier,) = self.typed(task["id"])
        self.assertIn("no reply from the colleague yet", carrier["text"])
        self.deliver(task["id"])
        self.assertEqual(self.st["messages"][fyi["id"]]["state"], "delivered")

    def test_a_busy_specialist_gets_it_without_the_hub_deciding_and_without_an_insertion(self):
        self.ev("turn.started", "devops", marker=None, prompt_head="long work")
        hub_before = [m for m in self.st["messages"].values() if m["session"] == "hub"]
        _, fyi = self.send("the branch is merged", fyi=True)
        self.assertNotIn("held", fyi)                                           # nothing for the hub to decide
        self.assertEqual([m for m in self.st["messages"].values() if m["session"] == "hub"], hub_before)
        self.assertEqual(self.typed(fyi["id"]), [])                             # and not inserted in the middle of a turn
        self.ev("turn.result", "devops", text="ready")
        self.assertEqual(self.st["messages"][fyi["id"]]["state"], "aside")      # the turn ended — no new one started
        self.assertEqual([a for a in self.acts if a["do"] == "type"], [])

    def test_an_injected_message_carries_it_too(self):
        self.ev("turn.started", "devops", marker=None, prompt_head="long work")
        _, fyi = self.send("tests are green", fyi=True)
        _, now = self.send("stop, do not roll out", who=OWNER, now=True)
        (carrier,) = self.typed(now["id"])
        self.assertIn("tests are green", carrier["text"])
        self.assertEqual(self.st["messages"][fyi["id"]]["state"], "delivered")

    def test_a_command_does_not_carry_it(self):
        _, fyi = self.send("for your information", fyi=True)
        _, cmd = self.send("/compact", who=OWNER)
        (typed,) = self.typed(cmd["id"])
        self.assertNotIn("For your information", typed["text"])                           # the end of the command input — its arguments
        self.assertEqual(self.st["messages"][fyi["id"]]["state"], "aside")

    def test_a_carrier_that_failed_lets_the_next_one_take_it(self):
        _, fyi = self.send("for reference", fyi=True)
        _, task = self.send("task", who=OWNER)
        for _ in range(reducer.MAX_ATTEMPTS):
            m = self.st["messages"][task["id"]]
            self.ev("timer.fired", task["id"], attempt=m["attempt"], channel="core")
        self.assertEqual(self.st["messages"][task["id"]]["state"], "failed")
        self.assertEqual(self.st["messages"][fyi["id"]]["state"], "aside")
        _, again = self.send("task once more", who=OWNER)
        (carrier,) = self.typed(again["id"])
        self.assertIn("for reference", carrier["text"])

    def test_the_owner_can_remove_it_and_a_removed_session_fails_it(self):
        _, a = self.send("one", fyi=True)
        self.assertIsNone(self.ev("message.cancel", a["id"], channel="web", owner=True))
        self.assertEqual(self.st["messages"][a["id"]]["state"], "failed")
        _, b = self.send("two", fyi=True)
        self.ev("session.parked", "devops", channel="core")                     # with it — parks, like a free one
        self.assertIsNone(self.ev("session.remove_requested", "devops", **OWNER))
        self.assertEqual(self.st["messages"][b["id"]]["state"], "failed")

    def test_not_with_now_and_only_a_plain_message(self):
        self.assertIn("--fyi", self.send("x", who=OWNER, fyi=True, now=True)[0] or "")
        _, m = self.send("x", who=OWNER, fyi="yes")                              # only true is a flag, not a string
        self.assertNotEqual(m["state"], "aside")

    def test_a_task_result_still_goes_to_the_one_who_set_it(self):
        # review: --fyi to the delegator in the middle of a turn is not "already told it itself": the turn result still goes to it
        self.ev("delegation.created", None, session="devops", context="fix the feed", for_session="maps", **MAPS)
        (d,) = self.st["delegations"].values()
        self.deliver(d["message"])
        _, fyi = self.send("took it, waiting for the build", who={**MAPS, "sender": "devops"}, to="maps", fyi=True)
        self.ev("turn.result", "devops", text="the feed is fixed")
        got = [m for m in self.st["messages"].values() if m["session"] == "maps" and m["state"] != "aside"]
        self.assertTrue(any("the feed is fixed" in m["text"] for m in got), got)
        (carrier,) = [a for a in self.acts if a["do"] == "type" and a["session"] == "maps"]
        self.assertIn("took it, waiting for the build", carrier["text"])                   # and the FYI goes with this result

    def test_a_retired_executors_fyi_goes_to_who_raised_it(self):
        self.st["sessions"]["devops"].update(role="executor", raised_by="hub")
        _, fyi = self.send("the build is green", fyi=True)
        self.st["sessions"]["devops"].update(state="retired")   # handed in and fell asleep: there will be no turn
        self.ev("owner.dismissed", "devops", **OWNER)                          # any next event
        m = self.st["messages"][fyi["id"]]
        self.assertEqual((m["session"], m["state"]), ("hub", "aside"))
        self.assertEqual(m["routing_note"], "for devops — it is asleep")
        self.assertEqual(m["text"], "the build is green")                            # the sender's words — as is

    def test_the_same_fyi_to_a_sleeping_executor_and_its_raiser_is_one_line(self):
        # the sender wrote to both (`hub send A --fyi T; hub send B --fyi T`), B is A's sleeping executor: the core
        # passes B's copy to A, and A would read the same text twice
        self.st["sessions"]["devops"].update(role="executor", raised_by="maps", state="retired")
        _, first = self.send("the build is green", who=HUB, to="maps", fyi=True)
        why, second = self.send("the build is green", who=HUB, to="devops", fyi=True)
        self.assertIsNone(why)
        first = self.st["messages"][first["id"]]
        self.assertEqual((first["state"], second["session"]), ("aside", "maps"))
        self.assertEqual((second["state"], second["merged_into"]), ("delivered", first["id"]))   # accepted, not lost
        self.assertEqual(first["routing_note"], "for maps and devops")
        _, task = self.send("what is new?", who=OWNER, to="maps")
        (carrier,) = self.typed(task["id"])
        self.assertEqual(carrier["text"].count("the build is green"), 1)
        self.assertEqual(carrier["text"].count("— m"), 1)
        self.assertIn("(the core's service note: for maps and devops)", carrier["text"])

    def test_a_merged_fyi_passed_on_again_keeps_one_note(self):
        self.st["sessions"]["maps"].update(role="executor", raised_by="hub")
        self.st["sessions"]["devops"].update(role="executor", raised_by="maps", state="retired")
        _, first = self.send("the build is green", who=OWNER, to="maps", fyi=True)
        self.send("the build is green", who=OWNER, to="devops", fyi=True)
        self.st["sessions"]["maps"].update(state="retired")   # the raiser fell asleep too: it goes one level up
        self.ev("owner.dismissed", "maps", **OWNER)
        m = self.st["messages"][first["id"]]
        self.assertEqual((m["session"], m["state"], m["routing_note"]), ("hub", "aside", "for maps and devops"))

    def test_the_merge_does_not_depend_on_the_order(self):
        self.st["sessions"]["devops"].update(role="executor", raised_by="maps", state="retired")
        _, first = self.send("the build is green", who=HUB, to="devops", fyi=True)
        self.assertEqual(first["routing_note"], "for devops — it is asleep")
        _, second = self.send("the build is green", who=HUB, to="maps", fyi=True)
        first = self.st["messages"][first["id"]]
        self.assertEqual((first["state"], second["state"], second["merged_into"]), ("aside", "delivered", first["id"]))
        self.assertEqual(first["routing_note"], "for devops and maps")

    def test_a_note_from_before_the_translation_is_replaced_too(self):
        # state keeps notes written by the Russian core (FYI_FOR_RU, "for devops — it is asleep" in Russian): the merge
        # replaces that line as well
        self.st["sessions"]["devops"].update(role="executor", raised_by="maps", state="retired")
        _, first = self.send("the build is green", who=HUB, to="devops", fyi=True)
        old = reducer.FYI_FOR_RU + " devops — " + "".join(map(chr, (1086, 1085, 32, 1089, 1087, 1080, 1090)))
        self.st["messages"][first["id"]]["routing_note"] = old
        self.send("the build is green", who=HUB, to="maps", fyi=True)
        self.assertEqual(self.st["messages"][first["id"]]["routing_note"], "for devops and maps")

    def test_the_same_fyi_twice_to_one_session_stays_one(self):
        _, first = self.send("the build is green", fyi=True)
        _, second = self.send("the build is green", fyi=True)
        first = self.st["messages"][first["id"]]
        self.assertEqual((first["state"], second["state"], second["merged_into"]), ("aside", "delivered", first["id"]))
        self.assertNotIn("routing_note", first)

    def test_a_different_text_or_sender_is_not_merged(self):
        _, a = self.send("the build is green", fyi=True)
        _, b = self.send("the build is red", fyi=True)
        _, c = self.send("the build is green", who=HUB, fyi=True)
        self.assertEqual([m["state"] for m in (a, b, c)], ["aside"] * 3)

    def test_after_the_first_was_delivered_the_same_text_is_new(self):
        _, first = self.send("the build is green", fyi=True)
        _, task = self.send("task", who=OWNER)
        self.deliver(task["id"])
        self.assertEqual(self.st["messages"][first["id"]]["state"], "delivered")
        _, again = self.send("the build is green", fyi=True)
        self.assertEqual(again["state"], "aside")

    def test_a_plain_message_with_the_same_text_is_not_merged(self):
        _, fyi = self.send("the build is green", fyi=True)
        _, plain = self.send("the build is green")
        self.assertEqual((fyi["state"], plain["state"]), ("aside", "typing"))

    def test_a_fyi_without_an_address_merges_with_the_same_one_to_the_hub(self):
        # no address means the hub (to is None): the merge names it by the session, the fold does not fail
        _, first = self.send("the build is green", to=None, fyi=True)
        why, second = self.send("the build is green", to="hub", fyi=True)
        self.assertIsNone(why)
        self.assertEqual((second["state"], second["merged_into"]), ("delivered", first["id"]))

    def test_the_same_fyi_twice_to_a_sleeping_executor_keeps_its_note(self):
        # two copies to one address name no second address: when it falls asleep, the raiser still learns whom it was for
        self.st["sessions"]["devops"].update(role="executor", raised_by="maps")
        _, first = self.send("the build is green", who=HUB, fyi=True)
        self.send("the build is green", who=HUB, fyi=True)
        self.st["sessions"]["devops"].update(state="retired")
        self.ev("owner.dismissed", "devops", **OWNER)
        m = self.st["messages"][first["id"]]
        self.assertEqual((m["session"], m["routing_note"]), ("maps", "for devops — it is asleep"))

    def test_after_a_failed_carrier_it_rides_again_even_if_that_one_came_late(self):
        # the rule is simple: a carrier that did not arrive releases the FYI; its late turn will read it twice, not lose it
        _, fyi = self.send("for reference", fyi=True)
        _, task = self.send("task", who=OWNER)
        for _ in range(reducer.MAX_ATTEMPTS):
            m = self.st["messages"][task["id"]]
            self.ev("timer.fired", task["id"], attempt=m["attempt"], channel="core")
        self.deliver(task["id"])                                                # the marked turn did come after all
        self.assertEqual(self.st["messages"][fyi["id"]]["state"], "aside")
        self.ev("turn.result", "devops", text="ok")
        _, again = self.send("the next one", who=OWNER)
        self.assertIn("for reference", self.typed(again["id"])[0]["text"])
        self.deliver(again["id"])
        self.assertEqual(self.st["messages"][fyi["id"]]["state"], "delivered")

    def test_it_cannot_be_removed_while_its_carrier_is_being_typed(self):
        _, fyi = self.send("for reference", fyi=True)
        _, task = self.send("task", who=OWNER)
        r = views.Reader()
        r.state = self.st
        self.assertFalse(r.snapshot()["messages"][fyi["id"]]["removable"])    # and the page does not show "Remove"
        self.assertIn(task["id"], self.ev("message.cancel", fyi["id"], channel="web", owner=True) or "")
        self.assertEqual(self.st["messages"][fyi["id"]]["state"], "aside")

    def test_a_session_that_waits_for_the_owner_gets_it_as_an_ordinary_message(self):
        # the hub passed "the owner logged in, you can go on" with --fyi to a session whose last result asked
        # him to log in (the watchdog's "waits for you" mark); it lay aside and woke nothing. A session that waits
        # for something is woken by any word to it: the worst case is an ordinary message, as without --fyi
        self.ev("turn.result", "devops", text="Could not log in to the mail: please log in, and I will continue.")
        self.assertIsNone(self.ev("watch.needs_owner", "devops", p=1.0, text="please log in", **OWNER))
        self.acts.clear()
        _, fyi = self.send("The owner logged in to the mail — you can continue", who={**MAPS, "sender": "hub"}, fyi=True)
        self.assertNotEqual(fyi["state"], "aside")
        self.assertNotIn("fyi", fyi)                                              # the page shows an ordinary row
        self.assertIs(fyi["fyi_woke"], True)
        (typed,) = self.typed(fyi["id"])
        self.assertIn("you can continue", typed["text"])

    def test_a_parked_session_that_waits_for_the_owner_is_woken(self):
        self.ev("turn.result", "devops", text="I propose three tasks, created nothing: waiting for your approval.")
        self.ev("watch.needs_owner", "devops", p=1.0, text="waiting for your approval", **OWNER)
        self.ev("session.parked", "devops", channel="core")
        self.acts.clear()
        _, fyi = self.send("The tasks are created on the owner's word", fyi=True)
        self.assertEqual((fyi["state"], fyi["fyi_woke"]), ("queued", True))
        self.assertEqual(self.st["sessions"]["devops"]["state"], "starting")     # woken, as by an ordinary message

    def test_woken_it_counts_as_written_and_one_for_a_sleeping_executor_does_not_wake_its_raiser(self):
        self.ev("watch.needs_owner", "maps", p=1.0, text="log in", **OWNER)
        self.ev("turn.started", "devops", marker=None, prompt_head="…")
        _, fyi = self.send("ready", who={**MAPS, "sender": "devops"}, to="maps", fyi=True)
        self.assertTrue(fyi["fyi_woke"])
        self.assertIn("maps", self.st["sessions"]["devops"]["sent_in_turn"])  # as an ordinary one: the result is not sent twice
        self.ev("turn.result", "devops", text="ready")
        self.st["sessions"]["devops"].update(role="executor", raised_by="maps", state="retired")
        _, other = self.send("the build is green", who={**OWNER, "channel": "session", "owner": False, "sender": "hub"},
                             fyi=True)
        self.assertEqual((other["session"], other["state"]), ("maps", "aside"))   # the news was for the executor

    def test_an_owners_fyi_that_woke_it_answers_no_card(self):
        self.ev("turn.started", "devops", marker=None, prompt_head="…")
        self.ev("permission.requested", "devops", request_id="r1", tool="AskUserQuestion", interactive=True,
                input={"questions": [{"question": "Roll out?", "options": [{"label": "Yes"}, {"label": "No"}]}]})
        self.ev("turn.result", "devops", text="asked")
        self.ev("turn.stopped", "devops")
        self.st["sessions"]["devops"]["attention"] = {"since": self.seq}
        _, fyi = self.send("I am on a call until three", who={"channel": "cli", "owner": True}, fyi=True)
        self.assertTrue(fyi["fyi_woke"])
        self.assertNotIn("answers_question", fyi)
        self.assertEqual(len(self.st["sessions"]["devops"]["asks"]), 1)        # the card stays open

    def test_its_own_waiting_mark_or_a_core_note_leaves_it_aside(self):
        # "accepted, doing it" to a session that waits (`hub waiting`) is what --fyi is for: the mark stays fresh
        self.ev("session.waiting", "devops", text="a colleague's reply", channel="session", owner=False, sender="devops")
        _, fyi = self.send("accepted, I will send a reply", fyi=True)
        self.assertEqual(fyi["state"], "aside")
        self.assertNotIn("fyi_woke", fyi)
        self.assertNotIn("stale", self.st["sessions"]["devops"]["waiting"])
        self.ev("watch.needs_owner", "devops", p=1.0, text="decide", **OWNER)
        msg = reducer.new_message(self.st, "mcore", "devops", "a core note", 2000, origin=reducer.forms.ORIGIN_CORE,
                                  fyi=True)
        self.assertEqual(msg["state"], "aside")                                  # the core's own notes are unchanged

    def test_the_page_sees_it_set_aside_and_labelled(self):
        _, fyi = self.send("for reference", fyi=True)
        r = views.Reader()
        r.state = self.st
        m = r.snapshot()["messages"][fyi["id"]]
        self.assertEqual((m["state"], m["fyi"]), ("aside", True))
        self.assertEqual(views.MESSAGE_LABELS["aside"], "FYI")


class Cli(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="fyi-")
        j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        for n in ("maps", "devops"):
            j.append("session.requested", n, {"cwd": "/w", "session_id": f"s-{n}", "channel": "cli", "owner": True})

    def hub(self, *args, ok=True):
        env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_CLI_WAIT": "0", "HUB_SESSION": "maps"}
        r = subprocess.run([sys.executable, BIN, *args], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode == 0, ok, r.stderr + r.stdout)
        return r, [ev for _, ev in spool.take(os.path.join(self.state, "spool"))]

    def test_flag(self):
        r, (ev,) = self.hub("send", "devops", "--fyi", "task", "12", "on", "review")
        self.assertEqual((ev["data"]["to"], ev["data"]["text"], ev["data"].get("fyi")), ("devops", "task 12 on review", True))
        self.assertIn("for information", r.stdout)   # a session's command: in English
        r, evs = self.hub("send", "devops", "--fyi", "--now", "x", ok=False)
        self.assertEqual([e for e in evs if e["data"].get("text") == "x"], [])
        self.assertIn("--fyi", r.stderr)


if __name__ == "__main__":
    unittest.main()
