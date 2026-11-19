"""A promise without action is a watchdog option (owner decision 2026-10-26: remind at once). A turn ended with "I take
X next" while nothing is running in the turn and nothing in flight will do it: the watchdog asks the model (a rule
gate first, then three votes in a row on the core's facts), and the core, by its own rule (idle, queue empty, not an
executor, same turn), writes the session one reminder per result, at most once per 15 minutes. Enabled by
`"promise": true` in watch.json; rejected results are sampled into the journal (watch.promise_skipped) to measure
accuracy. The model in the tests is a fake."""
import tests  # noqa: F401 - ignore the machine's settings
import json
import os
import re
import sys
import tempfile
import unittest

from core import forms, machines, reducer, spool
from modules.watch import watch
from tests.fakes import lang

OWNER = {"channel": "web", "owner": True}
CLI = {"channel": "cli", "owner": True}
VERB = lang.words("watch", "promise_verbs")   # a session's own action verbs in Russian ("take", "sort out", "fix"...)
IF = lang.words("watch", "promise_if")        # its conditions and waits ("if", "when", "I wait"...)
# "then": promise_next has no plain alternative, the word leads one of its fragments
THEN = re.match(r"[^\W\d_]+", lang._top(lang.data("watch", "promise_next"))[2]).group()
DONE = lang.words("watch", "surrender")[7:9]   # "fixed", "redone": the past of the verbs, a report and no promise
CORE_WORD = lang.legacy("who")["core"].capitalize()   # "Core:" opening a core message, the earlier form
PROMISE = ("Checked the feed: the page freezes on a long session, the cause is the list redraw. "
           f"Next {VERB[0]} the page freeze — {VERB[5]} the redraw and {VERB[21]}.")


def write(state, events):
    with open(os.path.join(state, "journal.jsonl"), "a") as f:
        for seq, ts, type, entity, data in events:
            f.write(json.dumps({"seq": seq, "ts": ts, "type": type, "entity": entity, "data": data, "src": "t"},
                               ensure_ascii=False) + "\n")


class Gate(unittest.TestCase):
    def test_own_action_without_a_condition_opens_the_gate(self):
        for t in (PROMISE, f"Done, the tests are green. {VERB[4].capitalize()} why the disk grows.",
                  # "then I fix" with "fix" off the verb list (no prefix): only the "then <verb>" rule sees it
                  f"Slice 1 done. {THEN.capitalize()} {VERB[21][2:]} slice 2.",
                  f"Summary written. {VERB[10].capitalize()} a separate task for the import bug."):
            self.assertTrue(watch.promise_gate(t), t)

    def test_condition_report_relay_or_others_do_not(self):
        for t in (f"{IF[0].capitalize()} you agree, {VERB[18]} the spec.",
                  f"{IF[1].capitalize()} CI ends, {VERB[26]} the branch.",
                  f"{DONE[0].capitalize()} and {DONE[1]}: tests passed, commit abc.", f"The MR report — {VERB[19]} to maps.",
                  f"The link — {VERB[12]}.", f"{THEN.capitalize()} the executor opens the MR.",
                  f"{IF[3].capitalize()} for the owner's answer, then {VERB[8]} the rollout."):
            self.assertFalse(watch.promise_gate(t), t)

    def test_only_the_last_sentences_count(self):
        self.assertFalse(watch.promise_gate(f"{VERB[0].capitalize()} the import. "
                                            + f"{DONE[0].capitalize()} and {DONE[1]}, all green. " * 6))

    def test_sampling_is_by_number(self):
        self.assertEqual(watch.sampled(12345, 0.5), watch.sampled(12345, 0.5))
        self.assertTrue(all(watch.sampled(n, 1) for n in range(50)))
        self.assertFalse(any(watch.sampled(n, 0) for n in range(50)))
        self.assertTrue(10 < sum(watch.sampled(n, 0.25) for n in range(1000, 1200)) < 90)


class Facts(unittest.TestCase):
    def test_what_is_in_flight_is_listed(self):
        st = reducer.empty()
        st["sessions"]["a"] = {"name": "a", "state": "idle", "background": {"b1": {"id": "b1", "title": "CI !12", "since": 0}}}
        st["delegations"]["d1"] = {"id": "d1", "session": "a-x", "for_session": "a", "state": "working", "words": "slice 2"}
        st["schedule"] = {"j1": {"id": "j1", "session": "a", "state": "active", "kind": "cron", "prompt": "check the PR"}}
        text = watch.promise_facts(st, "a", 600)
        for part in ("background", "CI !12", "running 10 min", "executor a-x", "slice 2", "«j1» (recurring cron)", "check the PR"):
            self.assertIn(part, text)
        self.assertIn("nothing", watch.promise_facts(st, "b", 600))

    def test_a_command_job_that_wakes_the_session_is_named_by_its_command(self):
        # a command job has no prompt: the model read «wakes it: «»» and could not tell what covers the promise
        st = reducer.empty()
        st["sessions"]["a"] = {"name": "a", "state": "idle"}
        st["schedule"] = {"j2": {"id": "j2", "session": "hub", "state": "active", "kind": "at",
                                 "command": "hub session wake a --about d7 check CI"}}
        self.assertIn("hub session wake a --about d7 check CI", watch.promise_facts(st, "a", 600))


class Detector(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="promise-")
        write(self.state, [(1, 100, "session.requested", "hub", {"cwd": "/h", "session_id": "s-h", "kind": "hub", **OWNER}),
                           (2, 100, "session.requested", "a", {"cwd": "/w", "session_id": "s-a", **OWNER}),
                           (3, 101, "session.started", "hub", {}), (4, 101, "session.started", "a", {})])
        self.cfg = {"promise": True}
        self.asked, self.answers = [], []
        self.w = watch.Watcher(self.state, self.cfg, ask=lambda t: {"trouble": False}, needs=None, urgent=None,
                               recap=lambda text: "", promise=self.model)
        self.sample = dict(watch.PROMISE_SAMPLE)
        self.step(102)

    def tearDown(self):
        watch.PROMISE_SAMPLE.update(self.sample)

    def model(self, text):
        self.asked.append(text)
        got = self.answers.pop(0) if self.answers else {"promise": True, "covered": False, "what": "look into the freeze"}
        if isinstance(got, Exception):
            raise got
        return got

    def step(self, now, kind="watch.promise"):
        self.w.step(now)
        return [ev for path, ev in spool.take(os.path.join(self.state, "spool")) if not os.unlink(path)
                if ev["type"] == kind]

    def turn(self, seq, text, prompt="fix the feed", origin=forms.ORIGIN_OWNER, data=None):
        write(self.state, [(seq, 110, "message.received", None, {"to": "a", "text": prompt, **(data or OWNER)}),
                           (seq + 1, 111, "turn.started", "a", {"marker": f"m{seq}", "prompt_head": f"[hub:m{seq}{origin}] {prompt}"}),
                           (seq + 2, 112, "turn.tool", "a", {"id": "t1", "name": "Read", "input": {"file_path": "/w/a.js"}}),
                           (seq + 3, 120, "turn.result", "a", {"text": text}), (seq + 4, 121, "turn.stopped", "a", {})])

    def test_a_promise_with_nothing_in_flight_is_caught_once(self):
        self.turn(10, PROMISE)
        self.assertEqual(self.step(125), [])                  # right after the result - still waiting (PROMISE_DELAY)
        (ev,) = self.step(131)
        self.assertEqual(ev["entity"], "a")
        d = ev["data"]
        self.assertEqual((d["of"], d["turn"], d["votes"], d["what"]), (13, 11, 3, "look into the freeze"))
        self.assertIn(f"Next {VERB[0]}", d["text"])
        self.assertIn("nothing", d["facts"])
        self.assertEqual(len(self.asked), 3)                  # three votes in a row
        self.assertTrue(self.asked[0].startswith("FACTS:\n") and self.asked[0].endswith(f"{VERB[21]}."))
        self.assertEqual(self.step(140), [])                  # one result - once

    def test_one_no_stops_the_vote(self):
        watch.PROMISE_SAMPLE["model"] = 1
        self.answers = [{"promise": True, "covered": False, "what": "x"}, {"promise": True, "covered": True, "what": "x"}]
        self.turn(10, PROMISE)
        (ev,) = self.step(131, "watch.promise_skipped")       # rejected by the model - sampled into the journal, no reminder
        self.assertEqual(len(self.asked), 2)                  # the second vote says "covered" - no third one is asked
        self.assertEqual((ev["data"]["stage"], ev["data"]["votes"], ev["data"]["of"]), ("model", [True, False], 13))

    def test_the_gate_spares_the_model(self):
        watch.PROMISE_SAMPLE["gate"] = 1
        self.turn(10, f"{DONE[0].capitalize()} and {DONE[1]}: tests passed, commit abc. " * 3)
        (ev,) = self.step(131, "watch.promise_skipped")
        self.assertEqual(self.asked, [])
        self.assertEqual(ev["data"]["stage"], "gate")

    def test_off_by_default(self):
        self.cfg.clear()
        self.turn(10, PROMISE)
        self.assertEqual(self.step(131), [])
        self.assertEqual(self.asked, [])

    def test_schedule_wait_and_core_turns_are_not_checked(self):
        # a schedule turn, a `hub wait` check, a turn on a core message (and on the reminder itself) - one reminder per promise
        self.turn(10, PROMISE, prompt="[waiting for reply] reply to the PR. Check now; …", origin=forms.ORIGIN_CORE)
        self.assertEqual(self.step(131), [])
        self.assertEqual(self.asked, [])
        self.turn(20, PROMISE)
        (fired,) = self.step(141)
        # the hub recorded a fact and the core reminded; the turn on the reminder ended the same way again - there is no
        # second reminder, even if the turn is long and its start left the watchdog's step window (review: WINDOW steps)
        write(self.state, [(30, 142, "watch.promise", "a", fired["data"]),
                           (31, 143, "turn.started", "a", {"marker": "m30",
                                                           "prompt_head": f"{lang.head('m30', kind='core')} {CORE_WORD}: …"})]
              + [(40 + i, 144, "turn.tool", "a", {"id": f"t{i}", "name": "Read", "input": {"file_path": f"/w/{i}"}})
                 for i in range(watch.WINDOW + 5)]
              + [(200, 150, "turn.result", "a", {"text": PROMISE}), (201, 151, "turn.stopped", "a", {})])
        self.w.refresh(144)
        self.assertEqual(self.w.state["messages"]["m30"]["origin"], forms.ORIGIN_CORE)
        self.assertEqual(self.step(170), [])
        self.assertEqual(len(self.asked), 3)

    def test_a_core_line_injected_mid_turn_keeps_the_owners_turn(self):
        # an insertion mid-turn (watchdog, --now) is not a turn start: the owner's turn result is checked
        write(self.state, [(10, 110, "message.received", None, {"to": "a", "text": "fix the feed", **OWNER}),
                           (11, 111, "turn.started", "a", {"marker": "m10", "prompt_head": f"{lang.head('m10', kind='owner')} fix"}),
                           (12, 112, "turn.started", "a", {"prompt_head": "[waiting for reply] reply to the PR. Check now; …"}),
                           (13, 120, "turn.result", "a", {"text": PROMISE}), (14, 121, "turn.stopped", "a", {})])
        (ev,) = self.step(131)
        self.assertEqual(ev["data"]["of"], 13)

    def test_a_letter_read_with_no_stop_before_it_starts_a_turn_of_its_own(self):
        # the fold's decision (turn_seq): the session is still busy with the owner's word, no Stop came, and a `hub wait`
        # check (not inserted) starts the next turn — it is not the owner's turn, its promise is not checked
        write(self.state, [(10, 110, "message.received", None, {"to": "a", "text": "fix the feed", **OWNER}),
                           (11, 111, "turn.started", "a", {"marker": "m10", "prompt_head": f"{lang.head('m10', kind='owner')} fix"}),
                           (13, 113, "message.received", None, {"to": "a", "text": "check the PR", **OWNER}),
                           (14, 114, "turn.started", "a", {"marker": "m13",
                                                           "prompt_head": "[waiting for reply] reply to the PR. Check now; …"}),
                           (15, 120, "turn.result", "a", {"text": PROMISE}), (16, 121, "turn.stopped", "a", {})])
        self.assertEqual(self.step(131), [])
        self.assertEqual(self.asked, [])

    def test_a_busy_queued_or_executor_session_is_not_checked(self):
        self.turn(10, PROMISE)
        write(self.state, [(20, 122, "message.received", None, {"to": "a", "text": "one more question", **OWNER})])
        self.assertEqual(self.step(131), [])                  # it is already being written to - the message will wake it by itself
        self.assertEqual(self.asked, [])
        self.turn(30, PROMISE)
        self.w.refresh(125)
        self.w.state["sessions"]["a"]["role"] = "executor"   # an executor: the result went to whoever assigned it, finished ones are not woken
        self.assertEqual(self.step(131), [])
        self.assertEqual(self.asked, [])

    def test_a_new_turn_drops_the_check(self):
        self.turn(10, PROMISE)
        write(self.state, [(20, 122, "turn.started", "a", {"prompt_head": "typed in the terminal"})])
        self.assertEqual(self.step(131), [])
        self.assertEqual(self.asked, [])

    def test_a_model_failure_is_retried_later(self):
        self.answers = [ValueError("no answer")]
        self.turn(10, PROMISE)
        self.assertEqual(self.step(131), [])
        self.assertEqual(self.step(140), [])                  # the retry comes after the window, not on the next pass
        self.assertEqual(len(self.asked), 1)
        (ev,) = self.step(131 + watch.WINDOW_SECONDS)
        self.assertEqual(ev["data"]["of"], 13)

    def test_off_model_switch_wins(self):
        self.cfg["off"] = True
        self.turn(10, PROMISE)
        self.assertEqual(self.step(131), [])
        self.assertEqual(self.asked, [])


class Core(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("session.requested", "a", cwd="/w", session_id="s-a", **OWNER)
        self.ev("session.started", "a")
        self.ev("message.received", None, to="a", text="fix the feed", **OWNER)
        self.mid = f"m{self.seq}"
        self.ts = 2000
        self.ev("turn.started", "a", marker=self.mid, prompt_head="fix the feed")
        self.turn = self.seq
        self.ev("turn.result", "a", text=PROMISE)
        self.ev("turn.stopped", "a")
        self.of = self.seq - 1

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": getattr(self, "ts", 1000) + self.seq,
                                                   "type": type, "entity": entity, "data": data})
        return why

    def promise(self, **extra):
        return self.ev("watch.promise", "a", **{"of": self.of, "turn": self.turn, "what": "look into the freeze",
                                                "votes": 3, **CLI, **extra})

    def nudges(self):
        return [m for m in self.st["messages"].values() if m["session"] == "a" and m["text"].startswith("Core: your last turn")]

    def test_one_reminder_per_result_in_the_same_chain(self):
        self.assertIsNone(self.promise())
        (m,) = self.nudges()
        self.assertIn('"look into the freeze"', m["text"])
        self.assertIn('"—"', m["text"])
        self.assertEqual(m["origin"], forms.ORIGIN_CORE)
        self.assertEqual(m.get("root"), self.st["sessions"]["a"].get("turn_root"))   # the answer goes where the question was asked
        self.assertIsNotNone(self.promise())                  # this result has already been reported
        self.assertEqual(len(self.nudges()), 1)

    def test_not_more_than_once_in_the_cooldown(self):
        self.assertIsNone(self.promise())
        for m in self.nudges():
            m["state"] = "delivered"
        self.ev("turn.started", "a", marker=self.nudges()[0]["id"], prompt_head="Core: your last turn")
        self.ev("turn.result", "a", text=PROMISE)
        self.ev("turn.stopped", "a")
        turn = self.seq - 2
        self.assertIsNotNone(self.promise(of=self.seq - 1, turn=turn))   # too early: PROMISE_COOLDOWN has not passed
        self.ts += reducer.PROMISE_COOLDOWN
        self.assertIsNone(self.promise(of=self.seq - 1, turn=turn))
        self.assertEqual(len(self.nudges()), 2)

    def test_a_stale_fact_is_dropped_quietly(self):
        # result A -> the model thinks -> the session did what it promised in a new turn and is idle again ->
        # the fact about A sends no reminder
        self.ev("turn.started", "a", prompt_head="typed in the terminal")
        self.ev("turn.stopped", "a")
        self.assertIsNone(self.promise())
        self.assertEqual(self.nudges(), [])
        # busy while the watchdog read the journal earlier - quiet; without seen_at - a real refusal
        self.ev("turn.started", "a", prompt_head="typed in the terminal")
        self.assertIsNone(self.promise(turn=self.seq, seen_at=1000))
        self.assertIsNotNone(self.promise(turn=self.seq))
        self.assertEqual(self.nudges(), [])

    def test_refused_for_the_session_itself_executors_and_sleepers(self):
        self.assertIsNotNone(self.promise(channel="session", owner=False, sender="a"))
        self.st["sessions"]["a"]["role"] = "executor"
        self.assertIsNotNone(self.promise())
        self.st["sessions"]["a"]["role"] = "worker"
        self.st["sessions"]["a"]["state"] = "asleep"
        self.assertIsNotNone(self.promise())
        self.assertEqual(self.nudges(), [])

    def test_a_queued_message_wakes_it_instead(self):
        self.ev("message.received", None, to="a", text="one more question", **OWNER)
        self.assertIsNotNone(self.promise())
        self.assertEqual(self.nudges(), [])

    def test_a_session_named_like_the_key_and_old_entries(self):
        # the st["watch"] keys are also session names (alarms to the hub): a session named "promise" does not break
        # reminders; entries older than the pause do not pile up
        self.ev("session.requested", "promise", cwd="/p", session_id="s-p", **OWNER)
        self.ev("session.requested", "hub", cwd="/h", session_id="s-h", kind="hub", **OWNER)
        self.assertIsNone(self.ev("watch.alert", "promise", kind="loop", reason="x", **CLI))
        self.assertIsNone(self.promise())
        self.st["watch"][reducer.PROMISE_KEY]["gone"] = {"of": 1, "at": 0}
        self.ts += reducer.PROMISE_COOLDOWN
        for m in self.nudges():
            m["state"] = "delivered"
        self.ev("turn.started", "a", prompt_head="typed")
        self.ev("turn.result", "a", text=PROMISE)
        self.ev("turn.stopped", "a")
        self.assertIsNone(self.promise(of=self.seq - 1, turn=self.seq - 2))
        self.assertEqual(list(self.st["watch"][reducer.PROMISE_KEY]), ["a"])

    def test_the_eval_reads_this_text(self):
        # the reminder text is a prompt for sessions: every turn of the eval case `note-promise` carries this constant of
        # the version under test (⟦KEY⟧ + consts), not a copy that could drift from it
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "evals"))
        from common import find_case
        path = find_case("note-promise")
        if not path:   # a Russian owner's case: evals/locale/ru/ is not in a published copy
            self.skipTest("evals/locale/ru/ is not in this copy")
        with open(path, encoding="utf-8") as f:
            case = json.load(f)
        for turn in case["turns"]:
            self.assertIn("⟦PROMISE_NUDGE⟧", turn["input"], turn["id"])
            spec = turn["consts"]["PROMISE_NUDGE"]
            self.assertEqual((spec["file"], spec["name"]), ("core/reducer.py", "PROMISE_NUDGE"), turn["id"])
            self.assertTrue(spec["format"]["what"], turn["id"])
        self.assertIn("{what}", reducer.PROMISE_NUDGE)

    def test_a_skipped_sample_is_a_fact(self):
        self.assertIn("watch.promise_skipped", machines.FACTS)
        before = json.dumps(self.st, sort_keys=True, default=str)
        self.assertIsNone(self.ev("watch.promise_skipped", "a", of=self.of, stage="model", **CLI))
        after = json.loads(json.dumps(self.st, sort_keys=True, default=str))
        after["seq"] = json.loads(before)["seq"]
        self.assertEqual(json.dumps(after, sort_keys=True), json.dumps(json.loads(before), sort_keys=True))


if __name__ == "__main__":
    unittest.main()
