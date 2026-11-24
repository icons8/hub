"""The journal watchdog (owner decision 2026-09-30: a daemon that watches every line of the journal and fires either
on the number of lines or when there were no lines for a while; the collection window is 30 seconds; it is a system
function, not a schedule entry). A process under hub-up reads the journal on the fly, accumulates new steps per
session and asks the model (Haiku); trouble is a watch.alert fact, and the core writes to the hub at most once per
half hour per session."""
import tests  # noqa: F401 - ignore the machine's settings
import json
import os
import tempfile
import unittest

from core import forms, reducer, spool, views
from modules.watch import watch
from tests.fakes import lang

OWNER = {"channel": "cli", "owner": True}


def journal(state, events):
    with open(os.path.join(state, "journal.jsonl"), "a") as f:
        for seq, ts, type, entity, data in events:
            f.write(json.dumps({"seq": seq, "ts": ts, "type": type, "entity": entity, "data": data, "src": "t"},
                               ensure_ascii=False) + "\n")


def base():
    return [(1, 100, "session.requested", "hub", {"cwd": "/h", "session_id": "s-h", "kind": "hub", **OWNER}),
            (2, 100, "session.requested", "a", {"cwd": "/w", "session_id": "s-a", **OWNER}),
            (3, 101, "session.started", "hub", {}), (4, 101, "session.started", "a", {}),
            (5, 110, "turn.started", "a", {"marker": None, "prompt_head": "fix the test"})]


def loop(start_seq, start_ts, n=3, step=2):
    out = []
    for i in range(n):
        out.append((start_seq + 2 * i, start_ts + step * i, "turn.tool", "a",
                    {"id": f"t{start_seq}{i}", "name": "Bash", "input": {"command": "npm test"}}))
        out.append((start_seq + 2 * i + 1, start_ts + step * i + 1, "turn.tool_result", "a",
                    {"id": f"t{start_seq}{i}", "error": True, "text": "Error: ENOENT package.json"}))
    return out


class Fake:
    """Stands in for Haiku: remembers what was asked, answers by a word in the text."""

    def __init__(self):
        self.asked = []
        self.broken = False

    def __call__(self, state_text):
        if self.broken:
            raise OSError("network")
        self.asked.append(state_text)
        if "ENOENT" in state_text:
            return {"trouble": True, "kind": "loop", "reason": "three npm test runs in a row with ENOENT",
                    "advice": "stop the turn and check the folder"}
        if "silent mid-turn" in state_text:
            return {"trouble": True, "kind": "hung", "reason": "", "advice": ""}
        return {"trouble": False, "kind": "ok", "reason": "", "advice": ""}


class Daemon(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="watch-")
        self.fake = Fake()
        journal(self.state, base())
        self.w = watch.Watcher(self.state, {}, ask=self.fake, needs=None, urgent=None, recap=lambda text: "")
        self.assertEqual(self.step(115), [])  # start: the past is read but not checked
        self.assertEqual(self.fake.asked, [])

    def step(self, now):
        self.w.step(now)
        return [ev for path, ev in spool.take(os.path.join(self.state, "spool")) if not os.unlink(path)]

    def test_window_30_seconds_collects_new_lines(self):
        journal(self.state, loop(6, 120))                      # 6 lines in 5 seconds
        self.assertEqual(self.step(126), [])                   # the window is still collecting
        self.assertEqual(self.fake.asked, [])
        (ev,) = self.step(120 + watch.WINDOW_SECONDS + 1)      # the window closed - one question about everything collected
        self.assertEqual((ev["type"], ev["entity"], ev["data"]["kind"]), ("watch.alert", "a", "loop"))
        self.assertEqual(len(self.fake.asked), 1)
        self.assertIn("Bash: npm test", self.fake.asked[0])
        self.assertIn("error", self.fake.asked[0])
        self.assertEqual(self.step(200), [])                   # nothing new - we do not ask
        self.assertEqual(len(self.fake.asked), 1)

    def test_many_lines_fire_before_the_window_ends(self):
        journal(self.state, loop(6, 120, n=watch.BATCH // 2, step=0))   # BATCH lines in a second
        (ev,) = self.step(122)
        self.assertEqual(ev["data"]["kind"], "loop")

    def test_no_lines_for_a_while_in_a_running_turn(self):
        self.assertEqual(self.step(110 + watch.SILENT - 5), [])
        (ev,) = self.step(110 + watch.SILENT + 1)              # the turn is running, no lines for longer than the threshold
        self.assertEqual((ev["entity"], ev["data"]["kind"]), ("a", "hung"))
        self.assertIn("silent mid-turn", self.fake.asked[-1])
        n = len(self.fake.asked)
        self.step(110 + watch.SILENT + 30)                     # the same silence is not asked about a second time in a row
        self.assertEqual(len(self.fake.asked), n)
        self.step(110 + 2 * watch.SILENT + 2)                  # silent for as long again - we ask again
        self.assertEqual(len(self.fake.asked), n + 1)

    def test_finished_turn_is_not_trouble_and_idle_silence_is_not_checked(self):
        journal(self.state, [(6, 130, "turn.result", "a", {"text": "done"})])
        self.assertEqual(self.step(130 + watch.WINDOW_SECONDS + 1), [])
        self.assertEqual(len(self.fake.asked), 0)                  # an ordinary end of turn - we do not ask the model (2026-10-05)
        self.assertEqual(self.step(130 + 10 * watch.SILENT), [])   # an idle session is silent - that is normal
        self.assertEqual(len(self.fake.asked), 0)
        journal(self.state, [(7, 4000, "turn.started", "a", {"marker": None, "prompt_head": "more"}),
                             (8, 4001, "turn.result", "a", {"text": "API Error", "is_error": True})])
        self.step(4001 + watch.WINDOW_SECONDS + 1)
        self.assertEqual(len(self.fake.asked), 1)                  # a turn ending with an error - we ask

    def test_watcher_does_not_feed_on_its_own_alerts(self):
        journal(self.state, [(6, 131, "watch.alert", "a", {"kind": "loop", "trouble": 0.9}),
                             (7, 132, "core.rejected", "a", {"reason": "too early"})])
        self.assertEqual([e for e in self.step(131 + watch.WINDOW_SECONDS + 1) if e["type"] == "watch.alert"], [])
        self.assertEqual(self.fake.asked, [])

    def test_model_failure_is_retried_with_the_same_lines(self):
        journal(self.state, loop(6, 120))
        self.fake.broken = True
        self.assertEqual(self.step(120 + watch.WINDOW_SECONDS + 1), [])
        self.fake.broken = False
        self.assertEqual(self.step(120 + watch.WINDOW_SECONDS + 5), [])     # no hammering: retry after the window
        (ev,) = self.step(120 + 2 * watch.WINDOW_SECONDS + 3)
        self.assertEqual(ev["data"]["kind"], "loop")

    # --- one model (Haiku via claude -p, since 2026-10-05): decides and writes the reason and the advice ---
    def test_model_decides_and_explains(self):
        journal(self.state, loop(6, 120))
        (ev,) = self.step(120 + watch.WINDOW_SECONDS + 1)
        self.assertEqual((ev["data"]["kind"], ev["data"]["reason"], ev["data"]["advice"]),
                         ("loop", "three npm test runs in a row with ENOENT", "stop the turn and check the folder"))
        self.assertNotIn("trouble", ev["data"])
        st = reducer.empty()   # to the hub - without "confidence 0%": Haiku has none
        for e in base():
            st, _, _ = reducer.reduce(st, dict(zip(("seq", "ts", "type", "entity", "data"), e)))
        st, _, why = reducer.reduce(st, {**ev, "seq": 50, "ts": 400})
        self.assertIsNone(why)
        (note,) = [m for m in st["messages"].values() if m["session"] == "hub"]
        self.assertNotIn("confidence", note["text"])
        self.assertIn("Watchdog's advice: stop the turn", note["text"])

    def test_model_says_ok_is_not_an_alert(self):
        self.fake.__class__ = type("F", (Fake,), {"__call__": lambda s, t: s.asked.append(t) or
                                                  {"trouble": False, "kind": "ok", "reason": "", "advice": ""}})
        journal(self.state, loop(6, 120))
        self.assertEqual(self.step(120 + watch.WINDOW_SECONDS + 1), [])
        self.assertEqual(len(self.fake.asked), 1)

    def test_few_questions_per_pass(self):
        # Haiku answers in seconds, but a pass also holds message urgency: at most ASKS step questions per pass
        more = [(20 + i, 100, "session.requested", f"s{i}", {"cwd": "/w", "session_id": f"s-{i}", **OWNER})
                for i in range(4)]
        more += [(30 + i, 110, "turn.started", f"s{i}", {"marker": None, "prompt_head": "x"}) for i in range(4)]
        journal(self.state, more)
        self.step(116)
        journal(self.state, [(40 + i, 120, "turn.tool", f"s{i}", {"name": "Read", "input": {"file_path": f"/f{i}"}})
                             for i in range(4)])
        self.step(120 + watch.WINDOW_SECONDS + 1)
        self.assertEqual(len(self.fake.asked), watch.ASKS)
        self.step(120 + watch.WINDOW_SECONDS + 3)
        self.assertEqual(len(self.fake.asked), 4)

    # --- what is visible from the core's state without a model ---
    def test_undelivered_message_is_a_fact_not_a_guess(self):
        journal(self.state, [(6, 130, "turn.result", "a", {"text": "done"}),
                             (7, 140, "message.received", None, {"to": "a", "text": "another task", **OWNER})])
        self.assertEqual(self.step(141), [])
        self.fake.asked.clear()
        (ev,) = self.step(140 + watch.UNDELIVERED + 1)         # the session is idle, yet the message is still queued
        self.assertEqual((ev["entity"], ev["data"]["kind"]), ("a", "undelivered"))
        self.assertIn("m7", ev["data"]["reason"])
        self.assertEqual(self.step(140 + watch.UNDELIVERED + 20), [])  # once per message

    def test_message_queued_behind_a_long_turn_is_not_undelivered(self):
        # a journal replay of 2026-09-30: ten false "not delivered" - the message waited for the turn to end and went right after
        journal(self.state, [(6, 140, "message.received", None, {"to": "a", "text": "after the turn", **OWNER})])
        self.assertEqual(self.step(140 + watch.UNDELIVERED + 60), [] if not self.fake.asked else [])
        journal(self.state, [(7, 600, "turn.result", "a", {"text": "done"})])   # the turn ended - the session is idle
        self.assertEqual([e for e in self.step(601) if e["data"].get("kind") == "undelivered"], [])
        (ev,) = [e for e in self.step(600 + watch.UNDELIVERED + 1) if e["data"].get("kind") == "undelivered"]
        self.assertEqual(ev["entity"], "a")                    # idle for a long time, yet the message has not gone

    def test_message_queued_during_a_slow_compact_is_not_undelivered(self):
        # after a restart the conversation is large - the auto-compact before delivery really runs up to 600 s
        # (COMMAND_CONFIRM_SECONDS), while UNDELIVERED=150 s raised the alarm on a live, running compact - a false "not delivered"
        journal(self.state, [(6, 120, "turn.result", "a", {"text": "done"}),
                             (7, 121, "session.compact_requested", "a", OWNER),
                             (8, 130, "message.received", None, {"to": "a", "text": "another task", **OWNER})])
        self.assertEqual([e for e in self.step(130 + watch.UNDELIVERED + 1) if e["data"].get("kind") == "undelivered"],
                         [])
        # the compact really hung (no session.compacted) - after COMMAND_CONFIRM_SECONDS that is real trouble
        from core.machines import COMMAND_CONFIRM_SECONDS
        (ev,) = [e for e in self.step(121 + COMMAND_CONFIRM_SECONDS + watch.UNDELIVERED + 1)
                 if e["data"].get("kind") == "undelivered"]
        self.assertEqual(ev["entity"], "a")

    def test_core_bug_is_reported(self):
        journal(self.state, [(6, 130, "core.rejected", "a", {"of": 5, "type": "session.renamed",
                                                             "reason": "core failure on event session.renamed: KeyError: 'job' (reducer.py:346)"})])
        (ev,) = self.step(131)
        self.assertEqual(ev["data"]["kind"], "corebug")
        self.assertIn("KeyError", ev["data"]["reason"])

    def test_core_bug_is_known_by_its_code_and_by_both_spellings(self):
        # the reason code (core/forms.py) - without it the start of the text: today's Russian "core failure" and a future English one
        journal(self.state, [(6, 130, "core.rejected", "a", {"of": 5, "type": "x", "code": "core_failure",
                                                             "reason": "KeyError: 'job' (reducer.py:346)"}),
                             (7, 130, "core.rejected", "a", {"of": 5, "type": "y",
                                                             "reason": "core failure on y: KeyError: 'z' (reducer.py:1)"}),
                             (8, 130, "core.rejected", "a", {"of": 5, "type": "z", "code": "other",
                                                             "reason": "against the rule"})])
        said = " | ".join(e["data"]["reason"] for e in self.step(131) if e["data"].get("kind") == "corebug")
        self.assertIn("KeyError: 'job'", said)
        self.assertIn("KeyError: 'z'", said)
        self.assertNotIn("against the rule", said)

    def test_hub_in_trouble_goes_to_the_owner(self):
        journal(self.state, [(6, 120, "turn.started", "hub", {"marker": None, "prompt_head": "x"})]
                + [(s, ts, t, "hub", d) for s, ts, t, _, d in loop(7, 121)])
        # the hub's "loop" - it retells other people's trouble: not an alarm (eval of 2026-10-03, item 1)
        self.assertEqual(self.step(121 + watch.WINDOW_SECONDS + 1), [])
        # a hung hub goes to the owner: the hub cannot help itself
        self.fake.__class__ = type("F", (Fake,), {"__call__": lambda s, t: (s.asked.append(t) or
                                                  {"trouble": True, "kind": "hung"})})
        (ev,) = [e for e in self.step(121 + watch.SILENT + 200) if e["type"] == "owner.notify"]
        self.assertIn("hub", ev["data"]["text"])

    # --- false alarms found by the hub on 2026-09-30: "hung" on a 4-7 s pause, "errors" from a single command error ---
    def test_hung_needs_real_silence_and_one_error_is_not_errors(self):
        says = {"kind": "hung"}
        self.fake.__class__ = type("F", (Fake,), {"__call__": lambda s, t: (s.asked.append(t) or
                                                  {"trouble": True, "kind": says["kind"]})})
        journal(self.state, [(6, 120, "turn.tool", "a", {"id": "t1", "name": "Bash", "input": {"command": "ls x"}}),
                             (7, 121, "turn.tool_result", "a", {"id": "t1", "error": True, "text": "No such file"})])
        self.step(116)
        self.assertFalse(watch.plausible("hung", self.w.steps["a"], 121 + 31))  # 31 s of silence is not "hung"
        self.fake.asked.clear()
        says["kind"] = "errors"
        journal(self.state, [(8, 160, "turn.tool", "a", {"id": "t2", "name": "Bash", "input": {"command": "ls y"}})])
        self.assertEqual(self.step(160 + watch.WINDOW_SECONDS + 1), [])     # one error is not "stuck on errors"
        says["kind"] = "hung"
        journal(self.state, [(9, 200, "turn.tool", "a", {"id": "t3", "name": "mcp__browser_mac__navigate_page",
                                                         "input": {"url": "https://x"}})])
        self.assertEqual(self.step(200 + watch.WINDOW_SECONDS + 1), [])     # the browser waits for its own timeout
        self.assertFalse(watch.plausible("hung", self.w.steps["a"], 200 + watch.HUNG_MIN + 5))
        (ev,) = self.step(200 + watch.SILENT + 5)                           # really silent - an alarm
        self.assertEqual(ev["data"]["kind"], "hung")

    def test_a_command_inside_its_timeout_is_not_hung(self):
        # 10-03 23:34, 10-04 23:34, 10-05 02:34: "hung" - a full test run took longer than 5 minutes, and Haiku
        # confirmed it as a second opinion. A call without an answer within its own timeout is work, not a hang
        t = lambda type, ts, **d: {"type": type, "ts": ts, "data": d}  # noqa: E731
        tests = [t("turn.started", 0, prompt_head="fix the page"),
                 t("turn.tool", 10, name="Bash", input={"command": "python3 -m unittest discover", "timeout": 600000})]
        self.assertFalse(watch.plausible("hung", tests, 10 + 6 * 60, {"state": "busy"}))
        self.assertTrue(watch.plausible("hung", tests, 10 + 11 * 60, {"state": "busy"}))   # limit of 10 min + a minute
        tests[1]["data"]["input"].pop("timeout")
        self.assertFalse(watch.plausible("hung", tests, 10 + 170, {"state": "busy"}))      # 2 min by default
        self.assertTrue(watch.plausible("hung", tests, 10 + 190, {"state": "busy"}))
        agent = tests[:1] + [t("turn.tool", 10, name="Agent", input={"description": "analysis"})]
        self.assertFalse(watch.plausible("hung", agent, 10 + 20 * 60, {"state": "busy"}))
        done = tests + [t("turn.tool_result", 20, text="OK")]
        self.assertTrue(watch.plausible("hung", done, 20 + watch.HUNG_MIN + 1, {"state": "busy"}))

    def test_waiting_for_the_owners_answer_is_not_hung(self):
        # 2026-09-30: "hung" at 73% on a session that waited 26 minutes for the owner's answer to a form question
        steps = [{"type": "turn.tool", "ts": 100, "data": {"name": "AskUserQuestion"}}]
        self.assertFalse(watch.plausible("hung", steps, 100 + 26 * 60, {"state": "dialog", "pending": [{"x": 1}]}))
        self.assertFalse(watch.plausible("silent", steps, 100 + 26 * 60, {"state": "dialog", "pending": [{"x": 1}]}))
        self.assertTrue(watch.plausible("hung", steps, 100 + 26 * 60, {"state": "busy", "pending": []}))
        head = watch.digest("a", {"state": "dialog", "since": 100, "pending": [{"tool": "AskUserQuestion"}]},
                            steps, 0, 200).split("\n")[0]
        self.assertIn("for the owner's answer", head)

    def test_remainders_of_the_eval(self):
        # eval of 2026-10-03 after 53f20bb: three false "hung" and service insertions remained
        t = lambda type, ts, **d: {"type": type, "ts": ts, "data": d}  # noqa: E731
        now = 100 + 13 * 60
        # (a) asked by a form mid-turn and launched a background agent, Claude Code reported "no background" - waiting for an answer
        steps = [t("turn.started", 0, prompt_head="read the transcripts"),
                 t("turn.tool", 10, name="Agent", input={"description": "x"}),
                 t("turn.tool_result", 10, text="Async agent launched successfully."),
                 t("permission.requested", 20, tool="AskUserQuestion"),
                 t("turn.tool", 30, name="Bash"), t("turn.tool_result", 31), t("session.said", 100, text="summary")]
        self.assertFalse(watch.plausible("hung", steps, now, {"state": "busy", "pending": []}))
        steps[3] = t("turn.tool", 20, name="Read")
        self.assertFalse(watch.plausible("hung", steps, now, {"state": "busy"}))   # waiting for its background agent
        del steps[1:3]
        self.assertTrue(watch.plausible("hung", steps, now, {"state": "busy"}))
        # (b) a scheduled turn of 5-6 minutes without steps - nobody is waiting; "hung" - from 10 minutes
        sched = [t("turn.started", 0, prompt_head=f"{lang.head('m1', kind='schedule')} [⏰ schedule seo-a] reread the chat")]
        self.assertFalse(watch.plausible("hung", sched, 6 * 60, {"state": "busy"}))
        self.assertTrue(watch.plausible("hung", sched, watch.HUNG_SCHEDULE + 1, {"state": "busy"}))
        # (c) a 0.8 threshold for sessions cut needed answers ("already done", "MR merged" - 0.62-0.78): owner decision
        # 2026-10-03: "0.6 + hub service notes" - one threshold, while the hub's service notes are cut by a rule before the model
        hub = "hub-core"
        say = lambda text: watch.urgency_rule({"origin": f" {forms.OLD_FROM} {hub}", "text": text}, hub)  # noqa: E731
        # the hub's notes in Russian from the data's words: "Hub:", "card", "connected", "the owner", "go on"
        head, (card, _, connected) = lang.word("words", "hub").capitalize(), lang.words("watch", "hub_service")
        boss = lang.legacy("who")["owner"]
        self.assertEqual(say(f"{head}: outline waits for the {boss}'s yes on the {card}; after it the core brings you up."),
                         0.0)
        self.assertEqual(say(f"{head}: {connected}, requested ms365 for you."), 0.0)
        # "clones removed, sessions cleaned up": no plain word of the data says it, its own regex — written out
        removed = ", ".join(x.replace(r"\w*", "") for x in lang._top(lang.data("watch", "hub_service"))
                            if r"\w* " in x and ".*" not in x)
        self.assertEqual(say(f"{head}: {removed}."), 0.0)
        self.assertIsNone(say(f"{head}: the {boss} ordered a forced restart of the hub — your check will break."))
        self.assertIsNone(say("stop, do not touch prod"))
        self.assertIsNone(say(f"{head}: ms365 {connected}, but the turn came out empty. "
                              f"{lang.word('watch', 'hub_order').capitalize()}: the {boss} (m17) asked to do it again."))
        self.assertIsNone(watch.urgency_rule({"origin": f" {forms.OLD_FROM} devops", "text": "MR merged, connected"}, hub))  # a session
        self.assertFalse(hasattr(watch, "URGENT_SESSION"))

    def test_process_exits_are_not_a_loop(self):
        # 10-02, hub: "going in circles" from three process exits - two hub restarts and one mass crash of all
        # sessions; the session had no steps of its own
        steps = [{"type": "session.exited", "ts": 100, "data": {"why": "reconcile"}},
                 {"type": "session.exited", "ts": 200, "data": {"why": "reconcile"}},
                 {"type": "session.exited", "ts": 300, "data": {"why": "process gone"}}]
        self.assertFalse(watch.plausible("loop", steps, 330))
        work = [{"type": "turn.tool", "ts": 300 + k, "data": {"name": "Bash", "input": {"command": "make"}}}
                for k in range(6)]
        self.assertTrue(watch.plausible("loop", steps + work, 330))
        w = watch.Watcher(tempfile.mkdtemp())
        w.state["sessions"]["a"] = {"name": "a", "state": "idle"}
        w.feed({"seq": 1, "ts": 1, "type": "session.exited", "entity": "a", "data": {"why": "reconcile"}}, 2)
        self.assertNotIn("a", w.steps)                    # a hub restart is not a session step

    def test_a_start_that_hung_is_a_process_event_like_an_exit(self):
        # the core killed the process without SessionStart and restarted it (session.start_stuck): like a process exit -
        # visible to the watchdog in the steps, but not counted as a loop
        stuck = {"type": "session.start_stuck", "ts": 300, "data": {"minutes": 10}}
        self.assertIn("start hung", watch._line(stuck) or "")
        calls = [{"type": "turn.tool", "ts": 200 + k, "data": {"name": "Bash", "input": {"command": "curl x"}}}
                 for k in range(watch.LOOP_SAME)]
        self.assertTrue(watch.plausible("loop", calls, 330))
        self.assertFalse(watch.plausible("loop", calls + [stuck], 330))   # a restart cut the turn - not a loop
        w = watch.Watcher(tempfile.mkdtemp())
        w.state["sessions"]["a"] = {"name": "a", "state": "starting"}
        w.feed({"seq": 1, "ts": 1, "type": "session.start_stuck", "entity": "a", "data": {"minutes": 10}}, 2)
        self.assertEqual([e["type"] for e in w.steps["a"]], ["session.start_stuck"])

    def test_hub_wait_checks_are_not_a_loop(self):
        # 10-01, hub: "going in circles" for a session that checked for an answer via `hub wait` every 2/5/10/20 min
        # and ended with "-" - that is waiting, not a loop
        steps = []
        for k in range(4):
            steps += [{"type": "turn.started", "ts": 100 + k, "data": {"prompt_head": f"{lang.head('m1', kind='hub')} [⏰ schedule gp-wait-ab-1] "
                                                                         f"{forms.WAIT_MARKS[0]} Ann in Telegram"}},
                      {"type": "turn.tool", "ts": 100 + k, "data": {"name": "mcp__telegram__get_history", "input": {"chat_id": 1}}},
                      {"type": "turn.result", "ts": 100 + k, "data": {"text": "—"}}]
        self.assertFalse(watch.plausible("loop", steps, 200))
        self.assertIn("hub wait", watch._line(steps[0]))
        # a loop is only inside one turn: three identical calls without an edit between them (eval of 2026-10-03, items 2-3)
        steps.append({"type": "turn.started", "ts": 105, "data": {"prompt_head": "do X"}})
        steps += [{"type": "turn.tool", "ts": 106 + k, "data": {"name": "Bash", "input": {"command": "curl x"}}}
                  for k in range(3)]
        self.assertTrue(watch.plausible("loop", steps, 200))

    def test_same_command_with_other_flags_is_a_loop(self):
        # 2026-09-30: a session installed dependencies nine times with different flags and bypassed the hook with a
        # script in /tmp - a loop although there were no identical calls; an edit of a working file between attempts is work, not a loop
        bash = lambda c: {"type": "turn.tool", "ts": 110, "data": {"name": "Bash", "input": {"command": c}}}  # noqa: E731
        steps = [{"type": "turn.started", "ts": 100, "data": {"prompt_head": "make the contract"}},
                 bash("mkdir -p .log; corepack pnpm install > .log/i.log 2>&1; echo install=$?"),
                 bash('TOKEN="$(cat /tmp/t)" corepack pnpm install > .log/i.log'),
                 {"type": "turn.tool", "ts": 111, "data": {"name": "Write", "input": {"file_path": "/tmp/i.sh"}}},
                 bash("sh /tmp/i.sh"),
                 bash("corepack pnpm install --prefer-offline > .log/i.log"),
                 bash("corepack pnpm install --filter x --prefer-offline")]
        self.assertTrue(watch.plausible("loop", steps, 200))
        steps.insert(5, {"type": "turn.tool", "ts": 112, "data": {"name": "Edit", "input": {"file_path": "/w/.npmrc"}}})
        self.assertFalse(watch.plausible("loop", steps, 200))
        tests = [steps[0]] + [bash(f"cd /w && grep -n x f{i}.py | head") for i in range(6)]
        self.assertFalse(watch.plausible("loop", tests, 200))      # different searches are not a loop

    def test_scheduled_checks_with_quiet_results_are_not_a_loop(self):
        # 10-02, hub: a session on a schedule task checked a payout status once an hour, no change - "-"
        steps = []
        for k in range(4):
            steps += [{"type": "turn.started", "ts": 100 + k, "data": {"prompt_head": f"{lang.head('m1', kind='schedule')} [⏰ schedule pay-35da] Check the payout status"}},
                      {"type": "turn.tool", "ts": 100 + k, "data": {"name": "Bash", "input": {"command": "curl status"}}},
                      {"type": "turn.result", "ts": 100 + k, "data": {"text": "—"}}]
        self.assertFalse(watch.plausible("loop", steps, 200))
        self.assertIn("schedule job", watch._line(steps[0]))
        steps[-1] = {"type": "turn.result", "ts": 104, "data": {"text": "an error again, retrying"}}
        self.assertFalse(watch.plausible("loop", steps, 200))  # on a schedule is never a loop (eval of 2026-10-03, item 2)

    # --- the turn result waits for the owner, but there is no form question (owner complaint 2026-09-30: a session
    # reported but asked nothing, so he has to go and check by hand whether anything is needed there) ---
    def result(self, text, seq=6, ts=130):
        journal(self.state, [(seq, ts, "turn.result", "a", {"text": text})])

    def needs(self, now):
        return [e for e in self.step(now) if e["type"] == "watch.needs_owner"]

    def test_final_message_that_needs_the_owner_is_marked(self):
        self.w.needs = lambda text: {"needs": "tell me" in text, "ask": "Say whether it fits" if "tell me" in text else ""}
        self.result("Done: checked the rules. " * 5 + "If that suits you, tell me — I will do it that way.")
        (ev,) = self.needs(131)
        self.assertEqual((ev["entity"], ev["data"]["p"], ev["data"]["ask"]), ("a", 1.0, "Say whether it fits"))
        self.assertIn("tell me — I will do it that way.", ev["data"]["text"])
        self.assertEqual(self.needs(140), [])                   # one result - once

    def test_the_kept_end_of_a_result_starts_where_a_sentence_starts(self):
        # the owner read items "oogle asks…" and "…` from a-link.txt": the core kept the last 300 characters of a result,
        # cut from the middle of a word, and the beginning of the sentence was lost
        self.w.needs = lambda text: {"needs": True, "ask": "Send the code"}
        long = "Checked the account and the payment settings, all in place. " * 6
        self.result(long + "The service asks for an SMS code on your phone.\n- the key is in the file link.txt\nSend the code — I will finish.")
        (ev,) = self.needs(131)
        text = ev["data"]["text"]
        self.assertLessEqual(len(text), 300)
        self.assertTrue(text.endswith("Send the code — I will finish."))
        self.assertTrue(text.startswith(("Checked", "The service", "- the key")), text[:40])
        self.assertIn("The service asks for an SMS code", text)

    def test_one_sentence_longer_than_the_limit_keeps_its_beginning(self):
        self.w.needs = lambda text: {"needs": True, "ask": "Answer"}
        self.result("First " + "very " * 80 + "long.")
        (ev,) = self.needs(131)
        self.assertTrue(ev["data"]["text"].startswith("First very"))
        self.assertTrue(ev["data"]["text"].endswith("…"))
        self.assertLessEqual(len(ev["data"]["text"]), 300)

    def test_an_executor_of_a_minihub_is_not_judged(self):
        # the mini-hub bus: a mini-hub executor's result goes to the mini-hub, the owner does not wait for it - we
        # do not call the model (review: high)
        from unittest import mock
        asked = []
        self.w.needs = lambda text: asked.append(text) or {"needs": True, "ask": "x"}
        self.result("Your yes is needed to merge. " * 4)
        with mock.patch("core.minihub.boss", lambda st, s: "m"):
            self.assertEqual(self.needs(131), [])
        self.assertEqual(asked, [])

    def test_only_the_ending_is_judged(self):
        # 2026-09-30: an "answer?" mark on a session captioned "Approve the release" - taken from the middle of the
        # result, while it ended with advice to clean the demo database. The end of the result decides: that is
        # where the session says what it waits for
        seen = []
        self.w.needs = lambda text: seen.append(text) or {"needs": "release approval" in text, "ask": ""}
        self.result("Prod — only after your release approval. " + "Checked and noted in the README. " * 60
                    + "For the meeting it is worth clearing the demo database with rm -rf data.")
        self.assertEqual(self.needs(131), [])
        self.assertTrue(seen[0].startswith("Owner's language: "))   # the language line, then the end of the result
        self.assertLessEqual(len(seen[0].split("\n", 1)[1]), watch.NEEDS_TAIL)
        self.assertTrue(seen[0].endswith("rm -rf data."))

    def test_a_wait_for_an_executor_is_not_the_owners(self):
        # "Waiting for iPhone frames" with no addressee was attributed to the owner by the model: frames, runs, merges
        # and results are done by executors and agents - the prompt says such a "waiting" is not a request to the owner
        # (eval: false 45/70 -> 1/70)
        seen = []
        self.w.needs = lambda text: seen.append(text) or {"needs": False, "ask": ""}
        self.result("Accepted the new connection alert on Mac in both themes. " * 3 + "Waiting for iPhone frames.")
        self.assertEqual(self.needs(131), [])
        self.assertTrue(seen[0].endswith("Waiting for iPhone frames."))   # the waiting line reaches the model whole
        # the rule is general, without a list of particulars ("frames, run, build...") and short - the prompt goes out on every result
        prompt = watch.NEEDS_PROMPT
        for words in ("other sessions", "not named", "asks the owner directly", "only a person can give"):
            self.assertIn(words, prompt)
        for detail in ("screenshots", "frames", "a test run", "a build", "a merge", "a release"):
            self.assertNotIn(detail, prompt)
        self.assertLess(len(prompt), 1550)   # was 1400; two rules of 2026-11-16 added ~130 each

    def test_a_reported_blocker_with_its_own_retry_is_not_the_owners(self):
        # "The computer is locked, so WhatsApp can't send now; the send is scheduled for 08:15" became an owner to-do
        # "unlock the computer before 08:15 and send": nothing was asked of him, the session had the send scheduled.
        # The rule stays general and needs the session's own step: a reported obstacle with no retry or schedule of its
        # own ("locked, the message did not go out") is still the owner's (eval evals/prompts/needs-owner-blocker.json:
        # without the condition, locked-no-retry went from true 8/8 to 0/8). The stub model makes this a check of the
        # prompt's words only
        text = "The computer is locked, so WhatsApp can't send now; the send is scheduled for 08:15. " * 2
        seen = []
        self.w.needs = lambda t: seen.append(t) or {"needs": False, "ask": ""}
        self.result(text)
        self.assertEqual(self.needs(131), [])
        self.assertTrue(seen[0].endswith("scheduled for 08:15."))   # the scheduled step reaches the model whole
        prompt = watch.NEEDS_PROMPT
        for words in ("an obstacle reported with the session's own retry", "or scheduled step", "no ask of the owner"):
            self.assertIn(words, prompt)
        self.assertLess(len(prompt), 1550)

    def test_an_action_possible_only_later_is_not_the_owners_yet(self):
        # A result listed "your action: pick up the file" while the job that makes the file still ran: the owner got a
        # to-do he could not act on. An action that waits for what still runs is a future step, even when the text
        # lists it as needed (eval evals/prompts/needs-deferred-action.json)
        self.assertIn("only after what still runs", watch.NEEDS_PROMPT)
        self.assertLess(len(watch.NEEDS_PROMPT), 1550)

    def test_a_yes_on_a_card_another_session_brings_is_a_future_step(self):
        # "The permission for prod is yours; session X brings the card" got the "answer?" mark in 7 of 8 tries with one
        # wording and 0 of 8 with another: the owner saw a guess next to a result that asked nothing, then the card.
        # The example sits in the future-step rule (eval evals/prompts/needs-card-elsewhere.json: 22/40 -> 39/40); a
        # yes asked right in the result stays the owner's
        self.assertIn("\"your yes, on the card X brings\") when nothing is needed now", watch.NEEDS_PROMPT)
        self.assertLess(len(watch.NEEDS_PROMPT), 1550)

    def ask_card(self, seq, session, question, ts=120):
        journal(self.state, [(seq, ts, "permission.requested", session,
                              {"request_id": f"r{seq}", "tool": "AskUserQuestion", "interactive": True,
                               "input": {"questions": [{"question": question, "options": [{"label": "Yes"}]}]}})])

    def test_an_open_card_of_the_session_holds_the_mark_back_without_the_model(self):
        # a session that waits on its own open card restates it in every result (an executor handed in, a reply came):
        # the model had the card in OPEN CARDS and still marked "waits for you" — a week: 231 of 1385 marks, 162 of
        # them only a reminder of the card. The page already shows the session waiting, with the card's question: the
        # result waits without the model while the card is open, and is judged once it closes with no newer result —
        # the other ask in it (21 of the 231) is not lost
        self.ask_card(6, "a", "Which error display to choose?")
        seen = []
        self.w.needs = lambda text: seen.append(text) or {"needs": True, "ask": "Say yes to the merge"}
        self.result("The executor handed in, no lessons. " * 3 + "Your decision waits in the card about the error "
                    "display. Your yes to the merge is needed too.", seq=7, ts=130)
        self.assertEqual(self.needs(131), [])
        self.assertEqual(self.needs(131 + 10 * watch.WINDOW_SECONDS), [])   # still open: still held
        self.assertEqual(seen, [])
        self.ask_card(8, "hub", "Another session's question", ts=140)      # another session's card holds nothing
        journal(self.state, [(9, 2500, "permission.answered", "a", {"request_id": "r6", "behavior": "allow",
                                                                    "answers": {"Which error display to choose?": "Yes"},
                                                                    **OWNER})])
        (ev,) = self.needs(2501 + watch.WINDOW_SECONDS)
        self.assertEqual(ev["data"]["ask"], "Say yes to the merge")
        self.assertNotIn("OPEN CARDS", seen[0])          # the other session's card is not named: no block
        # the usual path: the answer starts a new turn, and its result is judged as usual, without the card
        self.ask_card(10, "a", "Which error display to choose?", ts=2600)
        self.result("Waits in the card. Your yes to the merge is needed too. " * 3, seq=11, ts=2610)
        self.assertEqual(self.needs(2611), [])
        journal(self.state, [(12, 2700, "permission.answered", "a", {"request_id": "r10", "behavior": "allow",
                                                                     "answers": {"Which error display to choose?": "Yes"},
                                                                     **OWNER}),
                             (13, 2701, "turn.started", "a", {"marker": None, "prompt_head": "Yes"}),
                             (14, 2710, "turn.result", "a", {"text": "Done the display. Your yes to the merge is still needed. " * 3})])
        (ev,) = self.needs(2711)
        self.assertIn("still needed", ev["data"]["text"])

    def test_a_card_of_another_session_named_in_the_result_is_listed(self):
        # a session restated another session's card in a result about something else
        self.ask_card(6, "hub", "Where to put the button?")
        seen = []
        self.w.needs = lambda text: seen.append(text) or {"needs": False, "ask": ""}
        self.result("The panel matches the mock-up, no remarks. " * 3 + "The other session still needs your answer in q6.",
                    seq=7, ts=130)
        self.needs(131)
        self.assertIn("- q6 (session hub): Where to put the button?", seen[0])

    def test_the_sessions_the_result_names_are_listed(self):
        # A result reported an item returned to the session that had set it: "that is item 14 of the session hub; she
        # asked me to close it, but only she can, so I returned it to her". The model read "only she can" as the owner,
        # and the owner got a to-do "close the item". The other sessions the result names go with it, as the open cards
        # do (eval evals/prompts/needs-other-session.json); its own name, a removed session and a word inside another
        # word are not listed, and a result naming none gets no block
        journal(self.state, [(30, 112, "session.requested", "maps", {"cwd": "/m", "session_id": "s-m", **OWNER}),
                             (31, 113, "session.started", "maps", {})])
        self.w.refresh(114)
        self.w.state["sessions"]["maps"]["state"] = "removed"
        seen = []
        self.w.needs = lambda text: seen.append(text) or {"needs": False, "ask": ""}
        text = ("The link is already in the profile, that is item 14 of the session hub. She asked me to close the "
                "item itself, but only she can close it, so I returned it to her. Not the hubs list, not maps, not a.")
        self.result(text, seq=7, ts=130)
        self.needs(131)
        self.assertIn("SESSIONS: hub - the text names these sessions of the hub; they are agents", seen[0])
        self.assertTrue(seen[0].endswith(text))
        self.result("The panel matches the mock-up, no remarks. " * 3 + "Nothing is needed from you.", seq=8, ts=140)
        self.needs(141)
        self.assertNotIn("SESSIONS", seen[1])

    def test_the_cards_the_result_names_are_capped(self):
        journal(self.state, [(30, 112, "session.requested", "zz", {"cwd": "/z", "session_id": "s-z", **OWNER}),
                             (31, 113, "session.started", "zz", {})])
        for i in range(10):
            self.ask_card(40 + i, "zz", f"Question {i}")
        seen = []
        self.w.needs = lambda text: seen.append(text) or {"needs": False, "ask": ""}
        self.result("The panel matches the mock-up, no remarks. " * 3 + "The other session still needs your answers in "
                    + ", ".join(f"q{40 + i}" for i in range(10)) + ".", seq=60, ts=130)
        self.needs(131)
        self.assertEqual(seen[0].count("\n- q"), watch.CARDS_MAX)

    def mark(self, seq, ts, session="a"):
        journal(self.state, [(seq, ts, "watch.needs_owner", session, {"p": 1.0, "ask": "x", "channel": "cli", "owner": True})])

    def test_the_owners_dismissal_holds_the_mark_back_for_a_while(self):
        # a session woken by other sessions ends every few minutes with the same list; the owner dismissed its
        # "answer?" and it came back with the next result (a week: half of 1385 marks came after a click with no word
        # of his since). The click holds for DISMISS_HOLD, no model; his word to the session ends it at once
        seen = []
        self.w.needs = lambda text: seen.append(text) or {"needs": True, "ask": "Say yes to the prod rollout"}
        ask = "Your yes to the prod rollout is needed. " * 4
        self.result(ask)
        self.assertEqual(len(self.needs(131)), 1)
        t = 200
        self.mark(7, t - 50)
        journal(self.state, [(8, t, "owner.dismissed", "a", OWNER),
                             (9, t + 60, "turn.started", "a", {"marker": None, "prompt_head": "the build is green"}),
                             (10, t + 70, "turn.result", "a", {"text": ask})])
        self.assertEqual(self.needs(t + 71), [])
        self.assertEqual(len(seen), 1)                    # held back by the fact, the model is not asked
        late = t + watch.DISMISS_HOLD
        journal(self.state, [(11, late, "turn.started", "a", {"marker": None, "prompt_head": "the build is green"}),
                             (12, late + 10, "turn.result", "a", {"text": ask})])
        self.assertEqual(len(self.needs(late + 11)), 1)   # an hour later the result is judged again
        self.mark(13, late + 15)
        journal(self.state, [(14, late + 20, "owner.dismissed", "a", OWNER),
                             (15, late + 25, "turn.started", "a", {"marker": None, "prompt_head": "the build is green"}),
                             (16, late + 26, "turn.result", "a", {"text": ask})])
        self.assertEqual(self.needs(late + 27), [])
        journal(self.state, [(17, late + 30, "message.received", None, {"to": "a", "text": "noted", "fyi": True, **OWNER})])
        self.assertEqual(len(self.needs(late + 31)), 1)   # he wrote to it after the click: the held result is judged at once
        journal(self.state, [(18, late + 40, "turn.started", "a", {"marker": None, "prompt_head": "the build is green"}),
                             (19, late + 45, "turn.result", "a", {"text": ask})])
        self.mark(20, late + 46, "hub")
        journal(self.state, [(21, late + 50, "owner.dismissed", "hub", OWNER),
                             (22, late + 52, "owner.dismissed", "a", OWNER)])   # no mark on it: a stale second click
        self.assertEqual(len(self.needs(late + 71)), 1)   # holds nothing; another session's click holds only that one
        self.mark(23, late + 75)
        journal(self.state, [(24, late + 80, "owner.dismissed", "a", OWNER),
                             (25, late + 90, "turn.started", "a", {"marker": None, "prompt_head": "the build is green"})])
        self.ask_card(26, "a", "Roll out now?", ts=late + 91)
        journal(self.state, [(27, late + 95, "permission.answered", "a", {"request_id": "r26", "behavior": "allow",
                                                                          "answers": {"Roll out now?": "Yes"}, **OWNER}),
                             (28, late + 100, "turn.result", "a", {"text": ask})])
        self.assertEqual(len(self.needs(late + 101)), 1)  # answering its card is his word too
        self.mark(29, late + 150)
        journal(self.state, [(30, late + 200, "owner.dismissed", "a", OWNER),
                             (31, late + 210, "turn.started", "a", {"marker": None, "prompt_head": "a new task came"}),
                             (32, late + 220, "turn.result", "a", {"text": "A new question: which region for the bucket? " * 3})])
        self.assertEqual(self.needs(late + 221), [])
        (ev,) = self.needs(late + 200 + watch.DISMISS_HOLD + 1)   # nothing newer came: judged after the hour, not dropped
        self.assertIn("which region", ev["data"]["text"])

    def test_a_new_session_under_a_dismissed_ones_name_starts_clean(self):
        self.w.needs = lambda text: {"needs": True, "ask": "Say yes to the merge"}
        journal(self.state, [(6, 115, "turn.result", "a", {"text": "ok"})])
        self.mark(7, 120)
        journal(self.state, [(8, 125, "owner.dismissed", "a", OWNER)])
        self.assertEqual(self.needs(126), [])
        self.assertIn("a", self.w.dismissed)
        self.w.state["sessions"]["a"]["state"] = "removed"   # taken off the list; a new executor gets its name
        journal(self.state, [(9, 130, "session.requested", "a", {"cwd": "/w2", "session_id": "s-a2", **OWNER}),
                             (10, 131, "session.started", "a", {}),
                             (11, 132, "turn.started", "a", {"marker": None, "prompt_head": "fix the build"}),
                             (12, 140, "turn.result", "a", {"text": "Your yes to the merge is needed. " * 4})])
        self.assertEqual(len(self.needs(141)), 1)

    def test_no_block_without_open_cards_and_a_stale_card_is_not_open(self):
        from core import owner
        seen = []
        self.w.needs = lambda text: seen.append(text) or {"needs": True, "ask": "x"}
        self.result("Your yes to the merge is needed. " * 4)
        self.assertEqual(len(self.needs(131)), 1)
        self.assertNotIn("OPEN CARDS", seen[0])
        self.ask_card(7, "a", "An old question", ts=140)
        self.result("Your yes to the merge is needed, question q7. " * 4, seq=8, ts=140 + owner.ASK_TTL + 5)
        self.assertEqual(len(self.needs(140 + owner.ASK_TTL + 6)), 1)
        self.assertNotIn("OPEN CARDS", seen[1])                 # past its day the card is not what the owner sees

    def test_plain_report_is_not_marked(self):
        self.w.needs = lambda text: {"needs": False, "ask": ""}
        self.result("Done and checked: the tests passed, commit abc. " * 3)
        self.assertEqual(self.needs(131), [])

    def test_no_mark_when_the_session_is_working_again(self):
        asked = []
        self.w.needs = lambda text: asked.append(text) or {"needs": True, "ask": "x"}
        journal(self.state, [(6, 130, "turn.result", "a", {"text": "Your yes is needed to merge. " * 4}),
                             (7, 131, "turn.started", "a", {"marker": None, "prompt_head": "yes, merge"})])
        self.assertEqual(self.needs(132), [])                   # the owner already answered, the session is working
        self.assertEqual(asked, [])

    def test_no_mark_when_a_new_turn_starts_while_the_model_reads_the_result(self):
        # journal of 10-01..10-16: 189 refusals "the session is already working" - while the model read the result
        # (seconds, or minutes behind a pass with other questions), the session started a new turn. The watchdog
        # checks the fresh journal before the fact
        def needs(text):
            journal(self.state, [(7, 131, "turn.started", "a", {"marker": None, "prompt_head": "yes, merge"})])
            return {"needs": True, "ask": "x"}
        self.w.needs = needs
        self.result("Your yes is needed to merge. " * 4)
        self.assertEqual(self.needs(132), [])

    def test_the_mark_says_what_journal_it_was_decided_on(self):
        self.w.needs = lambda text: {"needs": True, "ask": "x"}
        self.result("Your yes is needed to merge. " * 4)
        (ev,) = self.needs(131)
        self.assertEqual(ev["data"]["seen_at"], 130)   # the core tells a stale fact from a real refusal

    def test_no_model_call_about_a_result_the_session_already_moved_past(self):
        # a watchdog pass is long ("what it is working on" lines, the queue): a result after which the session is
        # already in a new turn is not carried to the model - fresh journal before the call
        asked = []
        self.w.needs = lambda text: asked.append(text) or {"needs": True, "ask": "x"}
        journal(self.state, [(6, 120, "session.requested", "b", {"cwd": "/b", "session_id": "s-b", **OWNER}),
                             (7, 121, "session.started", "b", {}),
                             (8, 122, "turn.started", "b", {"marker": None, "prompt_head": "x"})])
        self.w.step(123)
        self.result("Your yes is needed to merge. " * 4, seq=9, ts=125)
        # b's "what it is working on" line is a model call; meanwhile a started a new turn
        self.w.recap = lambda text: journal(self.state, [(10, 126, "turn.started", "a", {"marker": None,
                                                                                         "prompt_head": "yes"})]) or ""
        self.w.recapped.clear()
        self.w.turn_recap.clear()
        self.assertEqual(self.needs(131), [])
        self.assertEqual(asked, [])

    def test_message_is_not_inserted_when_the_turn_ends_while_the_model_weighs_it(self):
        # 179 refusals "the message is not queued": while the model weighed urgency, the turn ended and the core
        # delivered the message itself (or the owner inserted it with a button)
        journal(self.state, [(6, 120, "message.received", None, {"to": "a", "text": "stop, do not touch prod", **OWNER})])

        def urgent(text):
            journal(self.state, [(7, 121, "turn.result", "a", {"text": "done"})])
            return 0.99
        self.w.urgent = urgent
        got = self.step(122)
        self.assertEqual([e["type"] for e in got if e["type"] == "message.inject"], [])

    def test_message_held_for_the_hub_is_weighed_after_the_hubs_decision(self):
        # 3 refusals "the message waits for the hub's decision" - the watchdog did not check held; the rule is shared
        # with the core. A message awaiting a decision is not carried to the model: weighed too early, it would not
        # be weighed again after the hub's decision (review)
        journal(self.state, [(6, 120, "message.received", None, {"to": "a", "text": "stop, do not touch prod", **OWNER})])
        self.w.step(121)   # urgent=None - the message is read, not weighed
        self.w.state["messages"]["m6"]["held"] = True
        asked = []
        self.w.urgent = lambda text: asked.append(text) or 0.99
        self.assertEqual([e["type"] for e in self.step(122) if e["type"] == "message.inject"], [])
        self.assertEqual(asked, [])
        self.w.state["messages"]["m6"]["held"] = False
        self.assertEqual([e["type"] for e in self.step(123) if e["type"] == "message.inject"], ["message.inject"])

    def test_rotation_in_the_middle_of_a_pass_does_not_hide_later_results(self):
        # review: a refresh mid-pass sees the rotation - the repeated tail is the past, but what came after is new
        self.w.needs = lambda text: {"needs": True, "ask": "x"}
        snap = {"seq": 6, "ts": 120, "type": "core.snapshot", "entity": None,
                "data": {"why": "rotate-start", "state": self.w.state}, "src": "core"}
        path = os.path.join(self.state, "journal.jsonl")
        os.replace(path, path + ".old")
        with open(path, "w") as f:
            f.write(json.dumps(snap, ensure_ascii=False) + "\n")
        self.w.refresh(121)
        self.result("Your yes is needed to merge. " * 4, seq=7, ts=130)
        self.w.refresh(131)
        (ev,) = self.needs(132)
        self.assertEqual(ev["entity"], "a")

    def test_inserted_message_says_what_journal_it_was_decided_on(self):
        journal(self.state, [(6, 120, "message.received", None, {"to": "a", "text": "stop, do not touch prod", **OWNER})])
        self.w.urgent = lambda text: 0.99
        (ev,) = [e for e in self.step(122) if e["type"] == "message.inject"]
        self.assertEqual(ev["data"]["seen_at"], 120)

    def test_model_failure_on_a_result_is_retried(self):
        calls = []

        def needs(text):
            calls.append(text)
            if len(calls) == 1:
                raise OSError("claude -p: no answer in 90 s")
            return {"needs": True, "ask": "Say yes to merging the MR"}
        self.w.needs = needs
        self.result("The fix is ready, I recommend merging before the rollout. " * 3)
        self.assertEqual(self.needs(131), [])
        self.assertEqual(self.needs(131 + watch.WINDOW_SECONDS - 5), [])   # the retry comes after the window
        (ev,) = self.needs(131 + watch.WINDOW_SECONDS + 1)
        self.assertEqual(ev["data"]["ask"], "Say yes to merging the MR")

    def test_off_in_settings_asks_nothing(self):
        w = watch.Watcher(self.state, {"off": True}, ask=self.fake, recap=lambda text: "")
        journal(self.state, loop(6, 120))
        w.step(115)
        w.step(500)
        self.assertEqual(self.fake.asked, [])


class Core(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        for seq, ts, t, name, d in base():
            self.ev(t, name, ts, **d)

    def ev(self, type, entity, ts, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts, "type": type, "entity": entity,
                                                   "data": data})
        return why

    def to_hub(self):
        return [m["text"] for m in self.st["messages"].values() if m["session"] == "hub"]

    def test_alert_goes_to_the_hub_with_cooldown(self):
        alert = {"kind": "loop", "trouble": 0.93, "why": "6 times in a row Bash: npm test with an error", "channel": "cli",
                 "owner": True}
        self.assertIsNone(self.ev("watch.alert", "a", 200, **alert))
        (text,) = self.to_hub()
        self.assertIn("a", text)
        self.assertIn("is going in circles", text)
        self.assertIn("hub show a", text)
        self.assertIn("too early", self.ev("watch.alert", "a", 200 + 60, **alert))  # for the same session - not more often
        self.assertEqual(len(self.to_hub()), 1)
        self.assertIsNone(self.ev("watch.alert", "a", 200 + reducer.WATCH_COOLDOWN + 1, **alert))
        self.assertEqual(len(self.to_hub()), 2)
        # a different trouble for the same session does not wait; the second model's reason and advice are in the message
        self.assertIsNone(self.ev("watch.alert", "a", 200 + reducer.WATCH_COOLDOWN + 5, kind="undelivered",
                                  reason="message m7 not delivered for 3 min", advice="wake it up", **OWNER))
        self.assertIn("message m7 not delivered for 3 min", self.to_hub()[-1])
        self.assertIn("wake it up", self.to_hub()[-1])

    def test_needs_owner_mark_lives_until_the_owner_writes_or_the_session_works(self):
        self.ev("turn.result", "a", 120, text="…tell me — I will do it that way.")
        mark = {"p": 0.95, "text": "If it suits, tell me — I will do it that way.", "ask": "Confirm the plan", **OWNER}
        self.assertIsNone(self.ev("watch.needs_owner", "a", 130, **mark))
        att = self.st["sessions"]["a"]["attention"]
        self.assertEqual((att["ask"], att["since"]), ("Confirm the plan", 130))
        snap = views.Reader()
        snap.state = self.st
        self.assertEqual([s["attention"]["ask"] for s in snap.snapshot()["sessions"] if s["name"] == "a"],
                         ["Confirm the plan"])
        self.ev("message.received", None, 140, to="hub", text="another session", **OWNER)
        self.assertIn("attention", self.st["sessions"]["a"])    # wrote to another one - the mark stays
        self.ev("message.received", None, 150, to="a", text="yes, do it", **OWNER)
        self.assertNotIn("attention", self.st["sessions"]["a"])  # answered it - cleared
        self.ev("watch.needs_owner", "a", 160, **mark)
        self.ev("turn.started", "a", 170, marker=None, prompt_head="x")
        self.assertNotIn("attention", self.st["sessions"]["a"])  # the session is working again - cleared
        self.assertIsNotNone(self.ev("watch.needs_owner", "a", 180, channel="session", owner=False, sender="a"))
        self.assertIsNotNone(self.ev("watch.needs_owner", "nope", 180, **mark))

    def test_cli_attention_off(self):
        # hub attention off — a session about itself; the hub and the owner name the session; anything else is a usage hint
        import subprocess, sys
        hub = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")
        state = tempfile.mkdtemp(prefix="attention-cli-")
        def run(me, *args):
            env = {**os.environ, "HUB_STATE_DIR": state, "HUB_CLI_WAIT": "0"}
            for k in ("HUB_SESSION", "HUB_EXTENSION", "HUB_MODULE", "HUB_SCHEDULE_JOB"):   # not a module, not a job
                env.pop(k, None)
            if me:
                env["HUB_SESSION"] = me
            r = subprocess.run([sys.executable, hub, "attention", *args], capture_output=True, text=True, env=env)
            box, got = os.path.join(state, "spool"), []
            for n in sorted(os.listdir(box)) if os.path.isdir(box) else []:
                with open(os.path.join(box, n)) as f:
                    got.append(json.load(f))
                os.unlink(os.path.join(box, n))
            return r.returncode, [(i["type"], i["entity"], i["data"].get("channel"), i["data"].get("sender"))
                                  for i in got]
        self.assertEqual(run("maps", "off"), (0, [("session.attention_off", "maps", "session", "maps")]))
        self.assertEqual(run("hub", "off", "maps"), (0, [("session.attention_off", "maps", "session", "hub")]))
        self.assertEqual(run(None, "off", "maps"), (0, [("session.attention_off", "maps", "cli", None)]))
        for args in ((), ("on",), ("off", "a", "b")):
            code, got = run("maps", *args)
            self.assertNotEqual(code, 0, args)
            self.assertEqual(got, [], args)

    def test_the_session_drops_its_own_mark_and_its_turn_result_does_not_set_it_again(self):
        # the owner saw "waits for you" twice for one matter: the session was asked to drop its mark, but no command
        # could (the mark is the core's), and the result of that very turn set the mark again
        own = {"channel": "session", "owner": False, "sender": "a"}
        mark = {"p": 1.0, "ask": "Send the code", "text": "Send the code", **OWNER}
        self.ev("turn.result", "a", 120, text="Send the code")
        self.assertIsNone(self.ev("watch.needs_owner", "a", 121, **mark))
        self.ev("session.requested", "b", 122, cwd="/b", session_id="s-b", **OWNER)
        self.ev("session.started", "b", 122)
        self.assertIsNotNone(self.ev("session.attention_off", "a", 123, channel="session", owner=False, sender="b"))
        self.assertIn("attention", self.st["sessions"]["a"])     # another session does not speak for it
        self.assertIsNone(self.ev("session.attention_off", "a", 124, **own))
        self.assertNotIn("attention", self.st["sessions"]["a"])  # free: cleared at once
        # mid-turn: the mark is already gone, and the result of this turn does not set it again
        self.ev("turn.started", "a", 130, marker=None, prompt_head="drop your mark")
        self.assertIsNone(self.ev("session.attention_off", "a", 131, **own))
        self.ev("turn.result", "a", 132, text="Dropped it. The code is still needed — the owner already has the item.")
        self.assertIsNotNone(reducer.attention_refused(self.st, self.st["sessions"]["a"]))
        self.assertIsNotNone(self.ev("watch.needs_owner", "a", 133, **mark))
        self.assertNotIn("attention", self.st["sessions"]["a"])
        # the next turn is judged as usual
        self.ev("turn.started", "a", 140, marker=None, prompt_head="next")
        self.ev("turn.result", "a", 141, text="Tell me whether to do it.")
        self.assertIsNone(self.ev("watch.needs_owner", "a", 142, **mark))
        # the hub and the owner can drop it too
        self.assertIsNone(self.ev("session.attention_off", "a", 143, channel="session", owner=False, sender="hub"))
        self.assertNotIn("attention", self.st["sessions"]["a"])
        self.assertIsNotNone(self.ev("session.attention_off", "nope", 145, **OWNER))

    def test_the_hub_dropping_a_mark_does_not_silence_a_turn_still_running(self):
        # review: the hub dropped a stale mark of a session that had just started a new turn, and that turn's
        # question to the owner got no mark — the hub and the owner speak of the mark they saw, not the next result
        mark = {"p": 1.0, "ask": "Say whether to merge", "text": "Tell me whether to merge", **OWNER}
        self.ev("turn.result", "a", 120, text="Done")
        self.ev("turn.started", "a", 130, marker=None, prompt_head="next")
        self.assertIsNone(self.ev("session.attention_off", "a", 131, channel="session", owner=False, sender="hub"))
        self.assertIsNone(self.ev("session.attention_off", "a", 132, **OWNER))
        self.ev("turn.result", "a", 133, text="Tell me whether to merge.")
        self.assertIsNone(reducer.attention_refused(self.st, self.st["sessions"]["a"]))
        self.assertIsNone(self.ev("watch.needs_owner", "a", 134, **mark))
        self.assertIn("attention", self.st["sessions"]["a"])
        # a free session with a mark: the hub's word holds for the result it saw
        self.assertIsNone(self.ev("session.attention_off", "a", 135, channel="session", owner=False, sender="hub"))
        self.assertIsNotNone(self.ev("watch.needs_owner", "a", 136, **mark))
        # the hub saw a mark, and the session meanwhile went through a whole new turn not judged yet: no mark to drop,
        # so its result is judged as usual (critic)
        self.ev("turn.started", "a", 140, marker=None, prompt_head="more")
        self.ev("turn.result", "a", 141, text="Tell me whether to merge the second one.")
        self.assertIsNone(self.ev("session.attention_off", "a", 142, channel="session", owner=False, sender="hub"))
        self.assertIsNone(self.ev("watch.needs_owner", "a", 143, **mark))
        self.assertIn("attention", self.st["sessions"]["a"])

    def test_a_schedule_job_does_not_drop_the_mark_of_its_session(self):
        # a schedule script on behalf of a session is not its turn: it does not judge what the turn waits for
        mark = {"p": 1.0, "ask": "Say whether to merge", "text": "Tell me whether to merge", **OWNER}
        self.ev("turn.result", "a", 120, text="Tell me whether to merge.")
        self.assertIsNone(self.ev("watch.needs_owner", "a", 121, **mark))
        self.assertIsNotNone(self.ev("session.attention_off", "a", 122, channel="session", owner=False, sender="a",
                                     schedule_job="watch-ci"))
        self.assertIn("attention", self.st["sessions"]["a"])
        # the hub's script naming the session — the same; a command job of the owner's schedule speaks as his terminal
        self.assertIsNotNone(self.ev("session.attention_off", "a", 123, channel="session", owner=False, sender="hub",
                                     schedule_job="watch-ci"))
        self.assertIsNone(self.ev("session.attention_off", "a", 124, channel="cli", owner=True, schedule_job="cleanup"))
        self.assertNotIn("attention", self.st["sessions"]["a"])

    def test_a_fact_overtaken_after_what_the_watcher_saw_is_dropped_without_a_refusal(self):
        # the watchdog decided on the journal up to seen_at, and the session changed state later - the fact is stale:
        # do nothing and write no refusal (507 "refusals" over 16 days were such races); without seen_at, or if the
        # change happened before what the watchdog saw, it is a real refusal, as before
        self.ev("turn.result", "a", 120, text="tell me — I will do it")
        self.ev("turn.started", "a", 125, marker=None, prompt_head="yes")
        mark = {"p": 1.0, "ask": "x", "text": "tell me", **OWNER}
        self.assertIsNone(self.ev("watch.needs_owner", "a", 126, seen_at=121, **mark))
        self.assertNotIn("attention", self.st["sessions"]["a"])
        self.assertIn("already working", self.ev("watch.needs_owner", "a", 126, **mark))
        self.assertIn("already working", self.ev("watch.needs_owner", "a", 126, seen_at=125, **mark))
        self.assertIn("unknown session", self.ev("watch.needs_owner", "nope", 126, seen_at=1, **mark))

    def test_a_message_overtaken_after_what_the_watcher_saw_is_not_a_refusal(self):
        self.ev("message.received", None, 120, to="a", text="stop", **OWNER)
        mid = f"m{self.seq}"
        self.ev("turn.result", "a", 125, text="done")      # the turn ended - the core prints the message itself
        self.assertNotEqual(self.st["messages"][mid]["state"], "queued")
        self.assertIsNone(self.ev("message.inject", mid, 126, seen_at=121, **OWNER))
        self.assertFalse(self.st["messages"][mid].get("injected"))
        self.assertIn("not in the queue", self.ev("message.inject", mid, 126, **OWNER))
        self.assertIn("not in the queue", self.ev("message.inject", mid, 126, seen_at=125, **OWNER))
        # the message is queued, but a rule keeps it out of the turn (an assignment) - not a race, a refusal
        self.ev("turn.started", "a", 130, marker=None, prompt_head="x")
        self.ev("delegation.created", None, 131, session="a", context="task", channel="session", owner=False,
                sender="hub")
        (task,) = [m for m in self.st["messages"].values() if m.get("delegation")]
        self.assertIn("queue", self.ev("message.inject", task["id"], 132, seen_at=1, **OWNER))

    def test_a_mark_for_a_session_free_again_is_set_and_a_minihub_refusal_stays(self):
        # review, second round: the session is idle again (a restart for tools) - the result still waits for the
        # owner: silently losing the mark is worse than an extra one; quiet only for "already working" and "removed"
        self.ev("turn.result", "a", 120, text="tell me — I will do it")
        self.ev("turn.started", "a", 125, marker=None, prompt_head="yes")
        self.ev("turn.result", "a", 126, text="did it")
        self.assertIsNone(self.ev("watch.needs_owner", "a", 127, seen_at=121, p=1.0, ask="x", **OWNER))
        self.assertIn("attention", self.st["sessions"]["a"])
        from unittest import mock
        self.ev("turn.started", "a", 130, marker=None, prompt_head="x")
        self.ev("turn.result", "a", 131, text="more")
        with mock.patch("core.minihub.boss", lambda st, s: "m"):
            self.assertIn("mini-hub", self.ev("watch.needs_owner", "a", 132, seen_at=121, p=1.0, ask="x", **OWNER))
        self.assertIsNotNone(self.ev("watch.needs_owner", "a", 127, seen_at=121, p=1.0, ask="x", channel="session",
                                     owner=False, sender="a"))

    def test_an_inject_refusal_is_quiet_only_by_its_own_change(self):
        # "not queued" goes by the message's time, "not in the turn" by the session's time: someone else's change does not hide a refusal
        self.ev("message.received", None, 120, to="a", text="stop", **OWNER)
        mid = f"m{self.seq}"
        self.ev("turn.result", "a", 121, text="done")    # delivered before what the watchdog saw
        self.ev("turn.started", "a", 125, marker=None, prompt_head="x")   # and the session changed after
        self.assertIn("not in the queue", self.ev("message.inject", mid, 126, seen_at=122, **OWNER))

    def test_background_output_of_a_task_that_never_was_is_still_refused(self):
        # review: a change of session state (a new turn) is not the end of the background; output of a foreign id is a real refusal
        self.ev("turn.started", "a", 125, marker=None, prompt_head="x")
        self.assertIn("background task", self.ev("session.background_output", "a", 126, seen_at=121, id="b9", at=124,
                                         line="x", **OWNER))
        # another task ended - output of a foreign id is still a refusal
        self.ev("session.background", "a", 126, id="b0", kind="bash", title="x")
        self.ev("session.background", "a", 126.5, id="b0", done=True)
        self.assertIn("background task", self.ev("session.background_output", "a", 126.6, seen_at=121, id="b9", at=124,
                                         line="x", **OWNER))
        # the owner took the session (hub take): the process has not exited yet, but the background is cleared -
        # also the end of the background
        self.ev("session.background", "a", 126.7, id="b3", kind="bash", title="x")
        self.ev("session.take_requested", "a", 126.9, **OWNER)
        self.assertEqual(self.st["sessions"]["a"]["state"], "held")
        self.assertIsNone(self.ev("session.background_output", "a", 127, seen_at=126.75, id="b3", at=126.7,
                                  line="x", **OWNER))
        self.ev("session.give_requested", "a", 127, **OWNER)
        self.ev("session.started", "a", 127)
        # the process left together with the background - the end of the background
        self.ev("session.background", "a", 127, id="b1", kind="bash", title="npm test")
        self.ev("session.exited", "a", 130, code=0)
        self.assertIsNone(self.ev("session.background_output", "a", 131, seen_at=128, id="b1", at=129, line="x",
                                  **OWNER))
        self.assertIn("watchdog", self.ev("session.background_output", "a", 131, seen_at=128, id="b1", at=129,
                                        line="x", channel="session", owner=False, sender="a"))

    def test_background_output_of_a_task_that_ended_after_the_watcher_saw_it(self):
        self.ev("session.background", "a", 120, id="b1", kind="bash", title="npm test")
        self.ev("session.background", "a", 121, id="b2", kind="bash", title="server")
        self.ev("session.background", "a", 125, id="b1", done=True)
        out = {"id": "b1", "at": 124, "line": "[killed]", **OWNER}
        self.assertIsNone(self.ev("session.background_output", "a", 126, seen_at=122, **out))
        self.assertIn("background task", self.ev("session.background_output", "a", 126, **out))
        self.assertIn("background task", self.ev("session.background_output", "a", 126, seen_at=125, **out))
        # the full list from Claude Code: what is not there has finished
        self.ev("session.background", "a", 130, tasks=[])
        self.assertIsNone(self.ev("session.background_output", "a", 131, seen_at=129, id="b2", at=128, line="x",
                                  **OWNER))

    def test_alert_about_unknown_session_or_from_a_session_is_rejected(self):
        self.assertIsNotNone(self.ev("watch.alert", "nope", 200, kind="loop", **OWNER))
        self.assertIsNotNone(self.ev("watch.alert", "a", 200, kind="loop", channel="session", owner=False, sender="a"))


class GuessOutput(unittest.TestCase):
    def test_finds_the_background_command_output_of_the_session(self):
        # 2026-10-09: Windows has no os.getuid - 1182 failures in hub-up.log; the base is faked, os.path.join is real
        from unittest import mock
        base = tempfile.mkdtemp(prefix="guess-out-")
        path = os.path.join(base, "D--Projects-demo", "sid1", "tasks", "b1.output")
        os.makedirs(os.path.dirname(path))
        open(path, "w").close()
        sess = {"cwd": "D:/Projects/demo", "session_id": "sid1"}
        with mock.patch.object(watch, "_tasks_base", lambda: base):
            self.assertEqual(os.path.normpath(watch.guess_output(sess, "b1")), os.path.normpath(path))
            self.assertIsNone(watch.guess_output(sess, "nope"))

    def test_the_base_needs_no_getuid_on_windows(self):
        if os.name != "nt":
            self.assertIn(f"claude-{os.getuid()}", watch._tasks_base())
        else:
            self.assertTrue(watch._tasks_base().endswith("claude"))



class UndeliveredNotice(unittest.TestCase):
    """"Not delivered" is an alarm while the message waits (complaint 2026-10-26: a "the hub seems to have not delivered
    a message to it" notice hung for hours after delivery). The watchdog counts by its own view of the journal - after
    a rotation it spends minutes reading it to the end and sees the past; the core checks the alarm against the
    queue: it rejects a stale one and clears an open one as soon as the message was delivered, never arrived, or was
    withdrawn."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="watch-")
        journal(self.state, base())
        self.w = watch.Watcher(self.state, {}, ask=Fake(), needs=None, urgent=None, recap=lambda text: "")
        self.w.step(115)
        self.st = reducer.empty()
        for seq, ts, t, name, d in base():
            self.st, _, _ = reducer.reduce(self.st, {"seq": seq, "ts": ts, "type": t, "entity": name, "data": d})
        self.seq = 5

    def both(self, type, entity, ts, data):
        """An event into the watchdog's journal and into the core - under one number."""
        journal(self.state, [(self.seq + 1, ts, type, entity, data)])
        return self.ev(type, entity, ts, data)

    def ev(self, type, entity, ts, data, src="t"):
        self.seq += 1
        e = {"seq": self.seq, "ts": ts, "type": type, "entity": entity, "data": data, "src": src}
        self.st, _, why = reducer.reduce(self.st, e)
        return why

    def alert(self, now):
        self.w.step(now)
        got = [ev for path, ev in spool.take(os.path.join(self.state, "spool")) if not os.unlink(path)]
        return [e for e in got if e["type"] in ("owner.notify", "watch.alert")]

    def to_hub(self, ts=140):
        """A message to the hub in the core and in the watchdog's journal: the core prints it to an idle hub at once (typing)."""
        self.both("turn.result", "hub", 120, {"text": "done"})
        self.both("message.received", None, ts, {"to": "hub", "text": "from a plugin", **OWNER})
        (mid,) = [m for m, x in self.st["messages"].items() if x["session"] == "hub"]
        self.assertEqual(self.st["messages"][mid]["state"], "typing")
        return mid

    def test_hub_alert_is_bound_to_the_message_and_closes_on_delivery(self):
        mid = self.to_hub()
        (ev,) = self.alert(140 + watch.UNDELIVERED + 1)
        self.assertEqual(ev["type"], "owner.notify")   # the hub cannot deliver a message to itself - to the owner
        self.assertEqual((ev["data"]["id"], ev["data"]["message"]), (f"undelivered:{mid}", mid))
        self.assertIsNone(self.ev("owner.notify", None, 300, ev["data"], src="watch"))
        key = f"n:watch:undelivered:{mid}"
        self.assertIn(key, self.st["notices"])
        self.ev("turn.started", "hub", 320, {"marker": mid, "prompt_head": "from a plugin"})
        self.assertEqual(self.st["messages"][mid]["state"], "delivered")
        self.assertNotIn(key, self.st["notices"])   # delivered - the notification is cleared by itself

    def test_late_alert_after_delivery_is_refused(self):
        # 2026-10-26: journal rotation at 11:49, delivery at 11:52, an alarm to the owner at 11:55 - from a stale view
        mid = self.to_hub()
        (ev,) = self.alert(140 + watch.UNDELIVERED + 1)
        self.ev("turn.started", "hub", 290, {"marker": mid, "prompt_head": "from a plugin"})
        self.assertIn("arrived or was withdrawn",self.ev("owner.notify", None, 300, ev["data"], src="watch"))
        self.assertNotIn(f"n:watch:undelivered:{mid}", self.st.get("notices") or {})

    def give_up(self, mid):
        for _ in range(10):
            m = self.st["messages"][mid]
            if m["state"] != "typing":
                break
            self.ev("timer.fired", mid, m["deadline"], {"attempt": m["attempt"], "n": m["attempt"]})
        self.assertEqual(self.st["messages"][mid]["state"], "failed")

    def test_alert_stays_when_delivery_fails_for_good(self):
        # the core no longer prints to the hub, only the owner can restart it - the alarm is right until delivery
        mid = self.to_hub()
        (ev,) = self.alert(140 + watch.UNDELIVERED + 1)
        self.ev("owner.notify", None, 300, ev["data"], src="watch")
        self.give_up(mid)
        self.assertIn(f"n:watch:undelivered:{mid}", self.st["notices"])
        self.ev("turn.started", "hub", 900, {"marker": mid, "prompt_head": "from a plugin"})   # delivered later - cleared
        self.assertNotIn(f"n:watch:undelivered:{mid}", self.st["notices"])

    def test_late_alert_after_final_failure_is_accepted(self):
        mid = self.to_hub()
        (ev,) = self.alert(140 + watch.UNDELIVERED + 1)
        self.give_up(mid)
        self.assertIsNone(self.ev("owner.notify", None, 900, ev["data"], src="watch"))
        self.assertIn(f"n:watch:undelivered:{mid}", self.st["notices"])

    def test_alert_stays_when_the_message_is_gone_from_the_state(self):
        # core.pruned (rotation, cleanup) removed a message that never arrived: "the core gave up" does not vanish silently
        mid = self.to_hub()
        (ev,) = self.alert(140 + watch.UNDELIVERED + 1)
        self.ev("owner.notify", None, 300, ev["data"], src="watch")
        self.give_up(mid)
        self.assertIsNone(self.ev("core.pruned", None, 400, {"before": 10 ** 9}))
        self.assertNotIn(mid, self.st["messages"])
        self.assertIn(f"n:watch:undelivered:{mid}", self.st["notices"])

    def test_repeated_alert_for_an_open_notice_is_not_refused(self):
        # a repeat on an already open notification (the watchdog restarted) updates it, even if the message is not in the state
        mid = self.to_hub()
        (ev,) = self.alert(140 + watch.UNDELIVERED + 1)
        self.ev("owner.notify", None, 300, ev["data"], src="watch")
        self.give_up(mid)
        self.ev("core.pruned", None, 400, {"before": 10 ** 9})
        key = f"n:watch:undelivered:{mid}"
        self.assertIsNone(self.ev("owner.notify", None, 500, ev["data"], src="watch"))
        self.assertEqual(self.st["notices"][key]["at"], 500)

    def test_new_alert_about_a_message_the_core_does_not_have_is_refused(self):
        alert = {"title": "Hub watchdog", "text": "not delivered", "source": "watch", "channel": "cli", "owner": True,
                 "id": "undelivered:m999", "message": "m999"}
        self.assertIn("arrived or was withdrawn",self.ev("owner.notify", None, 300, alert, src="watch"))
        self.assertNotIn("n:watch:undelivered:m999", self.st.get("notices") or {})

    def test_queue_head_is_the_numerically_first_message(self):
        # m9 and m10 of the same moment: as strings "m10" < "m9" - the core and the watchdog take m9 first (one
        # function defines the order)
        while self.seq < 8:
            self.both("turn.result", "hub", 132, {"text": "done"})
        self.both("message.received", None, 131, {"to": "a", "text": "two", **OWNER})
        self.both("message.received", None, 131, {"to": "a", "text": "three", **OWNER})
        self.assertEqual(sorted(m for m, x in self.st["messages"].items() if x["session"] == "a"), ["m10", "m9"])
        self.both("turn.result", "a", 140, {"text": "done"})
        self.assertEqual(self.st["messages"]["m9"]["state"], "typing")
        self.assertEqual(reducer.queue_key({"id": "m9", "created": 1}) < reducer.queue_key({"id": "m10", "created": 1}),
                         True)

    def test_watcher_judges_the_head_chosen_by_the_core_order(self):
        # the session is idle, several are queued unprinted (a rare moment between ticks): the alarm is about what the
        # core will print first: the urgent one, then the oldest, with equal `created` by number (m9 before m10)
        st = self.w.state
        st["sessions"]["a"].update(state="idle", since=100)
        plain = {"session": "a", "state": "queued", "created": 100, "attempt": 0}
        st["messages"] = {"m10": {**plain, "id": "m10"}, "m9": {**plain, "id": "m9"},
                          "m11": {**plain, "id": "m11", "urgent": True, "created": 101}}
        (ev,) = [e for e in self.alert(100 + watch.UNDELIVERED + 1) if e["type"] == "watch.alert" and e["entity"] == "a"]
        self.assertEqual(ev["data"]["message"], "m11")
        del st["messages"]["m11"]
        self.w.told.clear()
        (ev,) = [e for e in self.alert(100 + watch.UNDELIVERED + 2) if e["type"] == "watch.alert" and e["entity"] == "a"]
        self.assertEqual(ev["data"]["message"], "m9")

    def test_update_of_an_open_notice_for_another_message_is_checked(self):
        mid = self.to_hub()
        (ev,) = self.alert(140 + watch.UNDELIVERED + 1)
        self.ev("owner.notify", None, 300, ev["data"], src="watch")
        other = {**ev["data"], "message": "m999"}   # the same notification id, but a different message that the core does not have
        self.assertIn("arrived or was withdrawn",self.ev("owner.notify", None, 310, other, src="watch"))
        self.assertEqual(self.st["notices"][f"n:watch:undelivered:{mid}"]["message"], mid)

    def test_alert_closes_when_the_owner_removes_the_message(self):
        # withdrawn by the owner (or by the hub, a session close, a hook) - failed, but not "the core gave up": the alarm is stale
        self.both("message.received", None, 140, {"to": "a", "text": "into the queue", **OWNER})   # a is busy - queued
        (mid,) = [m for m, x in self.st["messages"].items() if x["session"] == "a"]
        alert = {"title": "Hub watchdog", "text": "not delivered", "source": "watch", "channel": "cli", "owner": True,
                 "id": f"undelivered:{mid}", "message": mid}
        self.assertIsNone(self.ev("owner.notify", None, 300, alert, src="watch"))
        self.assertIsNone(self.ev("message.cancel", mid, 310, OWNER))
        self.assertEqual(self.st["messages"][mid]["state"], "failed")
        self.assertNotIn(f"n:watch:undelivered:{mid}", self.st["notices"])
        self.assertIn("arrived or was withdrawn",self.ev("owner.notify", None, 320, alert, src="watch"))

    def test_alert_about_a_session_to_the_hub_is_refused_once_delivered(self):
        self.both("turn.result", "a", 130, {"text": "done"})
        self.both("message.received", None, 140, {"to": "a", "text": "another task", **OWNER})
        (mid,) = [m for m, x in self.st["messages"].items() if x["session"] == "a"]
        (ev,) = self.alert(140 + watch.UNDELIVERED + 1)
        self.assertEqual((ev["type"], ev["data"]["message"]), ("watch.alert", mid))   # not to the owner - to the hub
        self.ev("turn.started", "a", 290, {"marker": mid, "prompt_head": "another task"})
        self.assertIn("arrived or was withdrawn",self.ev("watch.alert", "a", 300, ev["data"], src="watch"))
        self.assertEqual([m for m in self.st["messages"].values() if m["session"] == "hub"], [])

    def test_the_hubs_note_reads_in_the_owners_language(self):
        # the watchdog's reason and advice are the core's phrases: their marks ride in watch.alert (reason_l10n,
        # advice_l10n) through the inbox file, the hub reads the English, the Russian owner the Russian
        from core import coretext, i18n
        self.both("turn.result", "a", 130, {"text": "done"})
        self.both("message.received", None, 140, {"to": "a", "text": "another task", **OWNER})
        (ev,) = self.alert(140 + watch.UNDELIVERED + 1)
        self.assertEqual(ev["type"], "watch.alert")
        self.assertIn("reason_l10n", ev["data"])
        self.assertIsNone(self.ev("watch.alert", "a", 200, ev["data"], src="watch"))
        (m,) = [m for m in self.st["messages"].values() if m["session"] == "hub"]
        self.assertIn("and the session is free", m["text"])
        ru = coretext.shown(m, "ru")
        self.assertIn(i18n.tr("ru", "core.watch.undelivered_advice"), ru)
        self.assertNotIn("and the session is free", ru)
        self.assertNotIn("Check whether its process is alive", ru)

    def test_only_the_head_of_the_queue_is_undelivered(self):
        # 2026-10-26: "not delivered" named the 37th in the hub's queue while the first one stood there - the host printed it for minutes
        self.both("turn.result", "a", 130, {"text": "done"})
        self.both("message.received", None, 140, {"to": "a", "text": "the first", **OWNER})
        self.both("message.received", None, 141, {"to": "a", "text": "the second", **OWNER})
        first, second = sorted((m for m, x in self.st["messages"].items() if x["session"] == "a"), key=lambda m: int(m[1:]))
        (ev,) = self.alert(141 + watch.UNDELIVERED + 1)
        self.assertEqual(ev["data"]["message"], first)
        self.both("turn.started", "a", 300, {"marker": first, "prompt_head": "the first"})
        self.both("turn.result", "a", 301, {"text": "ok"})
        (ev,) = self.alert(301 + watch.UNDELIVERED + 1)     # now the second is the head, and it is stuck
        self.assertEqual(ev["data"]["message"], second)


class TextFilesAreUtf8(unittest.TestCase):
    """Every text open() in the watchdog names utf-8: on a machine whose locale is not UTF-8 (Windows cp1251) the
    default encoding garbles non-ASCII text in state.json, the spool and the journal replay."""

    def test_every_text_open_names_the_encoding(self):
        import ast
        root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "modules", "watch")
        bad = []
        for name in sorted(os.listdir(root)):
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open"):
                    continue
                mode = node.args[1] if len(node.args) > 1 else next(
                    (k.value for k in node.keywords if k.arg == "mode"), None)
                if isinstance(mode, ast.Constant) and "b" in str(mode.value):
                    continue
                if not any(k.arg == "encoding" for k in node.keywords):
                    bad.append(f"{name}:{node.lineno}")
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
