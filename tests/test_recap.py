"""What a session is working on right now, in one line, like recap in Claude Code (owner decision 2026-10-03). The
watchdog writes the fact session.recap: from the current item of the to-do list (TodoWrite) without a model, otherwise
Haiku from the latest steps. At most once per RECAP_EVERY and only when new steps have appeared."""
import tests  # noqa: F401 — hide the machine's settings
import os
import tempfile
import unittest

from core import reducer, spool, views
from modules.watch import watch
from tests.fakes import lang


class Recap(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="recap-")
        self.asked = []
        self.w = watch.Watcher(self.dir, cfg={"off": True}, recap=lambda text: self.asked.append(text) or "Fixes the feed test")
        self.w.started = True
        self.w.state["sessions"]["a"] = {"name": "a", "state": "busy", "since": 0, "pending": []}

    def feed(self, type, ts, **d):
        self.w.feed({"seq": int(ts), "ts": ts, "type": type, "entity": "a", "data": d}, ts)

    def taken(self):
        out = []
        for path, item in spool.take(os.path.join(self.dir, "spool")):
            os.unlink(path)
            out.append(item)
        return out

    def test_from_the_todo_list_without_a_model(self):
        self.feed("turn.started", 1, prompt_head="fix the feed")
        self.feed("turn.tool", 2, name="TodoWrite", input={"todos": [
            {"content": "Test", "status": "completed"},
            {"content": "Fix the loading", "activeForm": "Fixing the feed loading", "status": "in_progress"}]})
        self.w.recaps(10)
        (ev,) = self.taken()
        self.assertEqual((ev["type"], ev["entity"], ev["data"]["text"], ev["data"]["by"]),
                         ("session.recap", "a", "Fixing the feed loading", "todo"))
        self.assertEqual(self.asked, [])

    def test_from_steps_by_the_model_rarely_and_only_on_news(self):
        self.feed("turn.started", 1, prompt_head="fix the feed")
        for k in range(watch.RECAP_STEPS):
            self.feed("turn.tool", 2 + k, name="Bash", input={"command": f"pytest {k}"})
        self.w.recaps(30)
        (ev,) = self.taken()
        self.assertEqual((ev["data"]["text"], ev["data"]["by"]), ("Fixes the feed test", "model"))
        self.assertIn("pytest", self.asked[0])
        self.w.recaps(30 + watch.RECAP_EVERY + 1)               # no new steps: does not ask
        self.assertEqual(self.taken(), [])
        for k in range(watch.RECAP_STEPS):
            self.feed("turn.tool", 40 + k, name="Read", input={"file_path": f"f{k}"})
        self.w.recaps(40)                                       # too early
        self.assertEqual(self.taken(), [])
        self.w.recaps(30 + watch.RECAP_EVERY + 2)
        self.assertEqual(len(self.taken()), 1)

    def test_task_line_right_at_turn_start(self):
        # owner decision 2026-10-03: as soon as the task is known, write at once what it does
        self.feed("turn.started", 1, prompt_head="Task from the owner: fix the feed loading on the page")
        self.w.recaps(2)
        (ev,) = self.taken()
        self.assertEqual(ev["data"]["text"], "Fixes the feed test")
        self.assertIn("fix the feed loading", self.asked[0])
        self.w.recaps(3)                                         # the same task: does not ask a second time
        self.assertEqual(self.taken(), [])

    def test_next_turn_gets_its_own_line_at_once(self):
        self.feed("turn.started", 1, prompt_head="fix the feed")
        self.w.recaps(2)
        self.feed("turn.result", 3, text="done")
        self.feed("turn.started", 4, prompt_head="write a journal test")
        self.w.recaps(5)                                         # a new task: at once, does not wait for RECAP_EVERY
        self.assertEqual(len(self.taken()), 2)
        self.assertIn("write a journal test", self.asked[-1])

    def test_long_turn_keeps_reconciling(self):
        # the watchdog window is WINDOW steps: on a long turn the window-based counter stalled and the line stopped
        # updating
        self.feed("turn.started", 1, prompt_head="a big task")
        t = 2
        for _ in range(watch.WINDOW + 10):
            self.feed("turn.tool", t, name="Bash", input={"command": f"step {t}"}); t += 1
        self.w.recaps(t)
        self.taken()
        for _ in range(watch.RECAP_STEPS):
            self.feed("turn.tool", t, name="Read", input={"file_path": f"f{t}"}); t += 1
        self.w.recaps(t + watch.RECAP_EVERY)
        self.assertEqual(len(self.taken()), 1)

    def test_free_session_is_not_asked(self):
        self.w.state["sessions"]["a"]["state"] = "idle"
        for k in range(watch.RECAP_STEPS):
            self.feed("turn.tool", 2 + k, name="Bash", input={"command": "x"})
        self.w.recaps(100)
        self.assertEqual(self.taken(), [])

    def test_core_keeps_it_and_the_page_sees_it(self):
        st = reducer.empty()
        for i, (t, e, d) in enumerate([("session.requested", "a", {"cwd": "/w", "session_id": "s"}),
                                       ("session.recap", "a", {"text": "Fixes the feed", "by": "model",
                                                               "channel": "cli", "owner": True})], 1):
            st, _, why = reducer.reduce(st, {"seq": i, "ts": i, "type": t, "entity": e, "data": d})
            self.assertIsNone(why)
        self.assertEqual(st["sessions"]["a"]["recap"]["text"], "Fixes the feed")
        _, _, why = reducer.reduce(st, {"seq": 9, "ts": 9, "type": "session.recap", "entity": "a",
                                        "data": {"text": "x", "channel": "session", "sender": "a"}})
        self.assertIsNotNone(why)                                # a session does not write its own recap
        r = views.Reader()
        r.state = st
        self.assertEqual(r.snapshot(now=10)["sessions"][0]["recap"]["text"], "Fixes the feed")


    def test_small_input_and_no_thinking(self):
        # decision of 2026-10-05 to shrink the thinking context; a probe on 6 live turns: 12 steps and 500 characters
        # of the task without thinking give the same line for $0.003 instead of $0.01
        self.feed("turn.started", 1, prompt_head="task " + "x" * 3000)
        for k in range(40):
            self.feed("turn.tool", 2 + k, name="Bash", input={"command": f"step{k:02d}"})
        self.w.recaps(100)
        text = self.asked[0]
        self.assertEqual([k for k in range(40) if f"step{k:02d}" in text], list(range(40 - watch.RECAP_STEPS_SEEN, 40)))
        self.assertEqual(watch.RECAP_STEPS_SEEN, 12)
        self.assertLess(text.count("x"), 520)
        calls = []
        orig = watch._claude
        watch._claude = lambda text, prompt, schema, quick=True: calls.append(quick) or {"now": "Writes a test"}
        try:
            self.assertEqual(watch.recap_model("steps"), "Writes a test")
        finally:
            watch._claude = orig
        self.assertEqual(calls, [True])

    def test_how_much_context_the_model_sees_is_a_setting(self):
        # owner decision 2026-10-12: how much context to capture goes into settings, so the prompt need not change
        self.w.cfg = {"off": True, "recap_steps": 3, "recap_task": 20}
        self.feed("turn.started", 1, prompt_head="fix the feed loading on the page and write a journal test")
        for k in range(10):
            self.feed("turn.tool", 2 + k, name="Bash", input={"command": f"step{k:02d}"})
        self.w.recaps(100)
        text = self.asked[0]
        self.assertEqual([k for k in range(10) if f"step{k:02d}" in text], [7, 8, 9])
        self.assertIn("fix the feed loading\n", text)       # exactly 20 characters of the task
        self.assertNotIn("journal test", text)

    def test_default_context_is_the_old_constants(self):
        self.w.cfg = {"off": True}
        self.assertEqual(self.w.recap_limits(), (watch.RECAP_STEPS_SEEN, watch.RECAP_TASK))
        self.w.cfg = {"off": True, "recap_steps": "many", "recap_task": -5}   # junk in watch.json does not crash the watchdog
        self.assertEqual(self.w.recap_limits(), (watch.RECAP_STEPS_SEEN, watch.RECAP_TASK))

    def test_a_turn_without_steps_is_said_to_be_only_starting(self):
        # 2026-10-12: from the task alone the model writes an intention as an action ("Fixes…"), which alarmed people
        # and got them stopped
        self.feed("turn.started", 1, prompt_head="fix the feed")
        self.w.recaps(2)
        self.assertIn("No steps yet", self.asked[0])
        self.assertIn("No steps yet", watch.RECAP_PROMPT)
        self.assertIn("what it is taking on", watch.RECAP_PROMPT)

    def test_the_model_is_told_the_owners_language(self):
        # the watchdog prompts are English: the model writes the owner's line in their language, given as the first line
        # of the input
        self.feed("turn.started", 1, prompt_head="fix the feed")
        self.w.recaps(2)
        self.assertTrue(self.asked[0].startswith("Owner's language: Russian.\n"))   # unknown: like the page before C1
        # the earlier Russian names of the languages too (language data)
        old_en = next(k for k, v in lang.legacy("lang_codes").items() if v == "en")
        for word, name in (("en", "English"), (old_en, "English"), ("ru", "Russian")):
            self.w.state["owner"]["lang"] = word
            self.w.state["seq"] += 1
            self.assertEqual(self.w.lang_line(), f"Owner's language: {name}.\n", word)

    def test_a_turn_with_steps_is_not_called_starting(self):
        self.feed("turn.started", 1, prompt_head="fix the feed")
        for k in range(watch.RECAP_STEPS):
            self.feed("turn.tool", 2 + k, name="Bash", input={"command": f"step{k}"})
        self.w.recaps(100)
        self.assertNotIn("No steps yet", self.asked[0])

    def test_model_answer_wrapped_in_json_is_unwrapped(self):
        self.assertEqual(watch.clean_recap('{"activity": "Integrates the update"}'), "Integrates the update")
        self.assertEqual(watch.clean_recap("«Writes a test»"), "Writes a test")

    def test_broken_json_from_the_model_does_not_reach_the_page(self):
        """Haiku answered {"status": "…"}}, an extra brace; json.loads failed and the raw string went to the page."""
        self.assertEqual(watch.clean_recap('{"status": "Studies the pull request for merging"}}'),
                         "Studies the pull request for merging")
        self.assertEqual(watch.clean_recap('{"now": "Writes a rotation test'), "Writes a rotation test")
        self.assertEqual(watch.clean_recap('```json\n{"now": "Fixes the feed"}\n```'), "Fixes the feed")
        self.assertEqual(watch.clean_recap('{"now": ""}}'), "")
        self.assertEqual(watch.clean_recap('Checks "waiting" against the journal'), 'Checks "waiting" against the journal')


class ClaudeRecap(unittest.TestCase):
    """The original Claude Code recap (owner decision 2026-10-03): the /recap command works in -p stream-json too, as a
    synthetic reply and a result with num_turns 0, without hooks (not a turn). The core asks for it after a big turn;
    the host recognizes the reply and writes session.recap."""

    def test_host_turns_the_recap_reply_into_a_fact_not_a_turn(self):
        from hosts.streamjson import StreamHost, StreamProcess

        class Pipe:
            def __init__(self):
                self.lines = []
            def write(self, s):
                self.lines.append(s)
            def flush(self):
                pass
        host = StreamHost(tempfile.mkdtemp(prefix="cr-"))
        popen = type("P", (), {"stdin": Pipe(), "poll": lambda self: None, "pid": 1})()
        host.procs["a"] = StreamProcess(popen)
        self.assertEqual(host.recap("a"), "sent")
        self.assertIn('"/recap"', popen.stdin.lines[0])
        # first the real turn finishes (its result as always), then the /recap reply
        host.inbox.put(("a", {"type": "result", "result": "done", "num_turns": 7}))
        host.inbox.put(("a", {"type": "assistant", "message": {"model": "<synthetic>", "content": [
            {"type": "text", "text": "Fixing the feed on the page. Next, run the tests."}]}}))
        host.inbox.put(("a", {"type": "result", "result": "Fixing the feed on the page. Next, run the tests.",
                              "num_turns": 0}))
        facts = host.poll()
        self.assertEqual([f[0] for f in facts], ["turn.result", "session.recap"])
        self.assertEqual(facts[1][2], {"text": "Fixing the feed on the page. Next, run the tests.", "by": "claude"})
        host.inbox.put(("a", {"type": "assistant", "message": {"model": "<synthetic>", "content": [
            {"type": "text", "text": "API Error"}]}}))
        self.assertEqual([f[0] for f in host.poll()], ["session.said"])    # without a request: an ordinary reply

    def test_core_asks_after_a_big_turn_and_keeps_it(self):
        st = reducer.empty()
        seq = [0]

        def ev(t, e, **d):
            seq[0] += 1
            nonlocal st
            st, acts, why = reducer.reduce(st, {"seq": seq[0], "ts": seq[0], "type": t, "entity": e, "data": d})
            return acts, why
        ev("session.requested", "a", cwd="/w", session_id="s", channel="web", owner=True)
        ev("session.started", "a")
        ev("turn.started", "a", marker=None)
        for k in range(reducer.RECAP_TOOLS):
            ev("turn.tool", "a", name="Bash")
        acts, _ = ev("turn.result", "a", text="done")
        self.assertIn({"do": "recap", "session": "a"}, acts)
        ev("turn.stopped", "a")
        _, why = ev("session.recap", "a", text="Fixed the feed. Next, the tests.", by="claude", channel="host")
        self.assertIsNone(why)
        self.assertEqual(st["sessions"]["a"]["away"]["text"], "Fixed the feed. Next, the tests.")
        r = views.Reader()
        for e in [{"seq": 1, "ts": 1, "type": "session.requested", "entity": "a", "data": {"cwd": "/w", "session_id": "s"}},
                  {"seq": 2, "ts": 2, "type": "session.recap", "entity": "a",
                   "data": {"text": "Fixed the feed.", "by": "claude", "channel": "host"}}]:
            r.feed(e)
        self.assertIn("Fixed the feed.", [it.get("text") for it in r.timelines["a"]][-1])
        small = reducer.empty()
        st = small
        ev("session.requested", "b", cwd="/w", session_id="s2", channel="web", owner=True)
        ev("session.started", "b")
        ev("turn.started", "b", marker=None)
        acts, _ = ev("turn.result", "b", text="ok")                       # a small turn: do not ask
        self.assertNotIn({"do": "recap", "session": "b"}, acts)


if __name__ == "__main__":
    unittest.main()
