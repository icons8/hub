"""Event order within one tick (13.10): hooks (the spool) and Claude Code output (the host) go into the journal in the
order they happened, not "the whole spool first, then the whole host". Otherwise the result of turn A landed after the
start of turn B (in the journal "Stop A → turn.started B → turn.result A"), and the lines of a turn begun mid-tick came
before its hook, so anything that looks up the current turn in the journal got someone else's. The clocks are separate
(core, host, the time in the spool file): a replay, no sleep."""
import tests  # noqa: F401
import json
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

from core import journal, spool
from core.daemon import Hub
from hosts.streamjson import StreamHost

OWNER = {"channel": "cli", "owner": True}


def line(**msg):
    return json.dumps(msg) + "\n"


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class EventOrder(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="order-")
        self.addCleanup(shutil.rmtree, self.state, ignore_errors=True)
        self.clock = Clock()
        self.host = StreamHost(self.state, claude=("claude-not-run",))
        self.host.clock = self.clock
        self.h = Hub(self.state, self.host, registry_dir=os.path.join(self.state, "registry"), clock=self.clock)
        self.h.execute = lambda action: None         # no spawn, no typing: only the journal
        self.h.record("session.requested", "a", {"cwd": self.state, "session_id": "s-a", **OWNER})
        self.h.record("session.started", "a", {"session_id": "s-a"})

    def hook(self, at, type, entity="a", **data):
        """A spool fact with its own time `at` (as spool.put sets it in the hook process)."""
        name = spool.put(self.h.spool_dir, type, entity, data, src="hook")
        path = os.path.join(self.h.spool_dir, name + ".json")
        with open(path, encoding="utf-8") as f:
            item = json.load(f)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({**item, "ts": at}, f)

    def stdout(self, at, *lines):
        """Stdout lines of session a read at time `at`, through the same reader thread as a real process."""
        self.clock.t = at
        self.host.reader("a", types.SimpleNamespace(stdout=list(lines)))()

    def tick(self, at):
        self.clock.t = at
        self.h.tick()

    def kinds(self, *types_):
        return [e["type"] for e in journal.read(self.h.journal.path)
                if e.get("entity") == "a" and e["type"] in types_]

    def said(self):
        return [e["data"]["text"] for e in journal.read(self.h.journal.path) if e["type"] == "session.said"]

    def test_the_result_of_a_turn_comes_before_the_next_turn_that_started_in_the_same_tick(self):
        # the tick found Stop of turn A and the start of turn B in the spool, and A's result in the output: in fact
        # the result came between them
        self.hook(1001, "turn.started", marker=None, prompt_head="first")
        self.tick(1002)
        self.hook(1010, "turn.stopped")
        self.stdout(1011, line(type="result", result="done A", num_turns=2, duration_ms=1000))
        self.hook(1012, "turn.started", marker=None, prompt_head="second")
        self.tick(1013)
        self.assertEqual(self.kinds("turn.started", "turn.stopped", "turn.result"),
                         ["turn.started", "turn.stopped", "turn.result", "turn.started"])

    def test_a_turn_started_while_the_core_was_busy_has_its_hook_before_its_lines(self):
        # the core types a message mid-tick, Claude Code starts the turn at once: the hook landed in the spool after the
        # core had looked it over, and the first line of the turn went to the output before the host was polled. The
        # turn's hook comes before the turn's lines
        def execute(action):
            if action["do"] == "type":
                self.hook(1002, "turn.started", marker=action["message"], prompt_head="[hub:...]")
                self.stdout(1003, line(type="assistant", message={"content": [{"type": "text", "text": "on it"}]}))
        self.h.execute = execute
        spool.put(self.h.spool_dir, "message.received", None, {"to": "a", "text": "do it", **OWNER})
        self.tick(1001)
        self.tick(1004)
        self.assertEqual(self.kinds("turn.started", "session.said"), ["turn.started", "session.said"])

    def test_a_hook_landing_while_the_spool_is_listed_is_not_taken_for_a_clock_jump(self):
        # "now" is after the spool listing: a hook that landed between the host poll and the listing is later than P
        # but not "from the future"; its output line was read after the poll, so the hook and the line go to the next
        # tick together, in their own order
        take = spool.take
        def late(d):
            self.hook(1002.5, "turn.stopped")
            self.stdout(1002.6, line(type="assistant", message={"content": [{"type": "text", "text": "after the hook"}]}))
            self.clock.t = 1003
            return take(d)
        self.clock.t = 1001
        with mock.patch("core.spool.take", side_effect=late):
            self.h.collect()
        self.assertEqual(self.kinds("turn.stopped"), [])
        self.clock.t = 1004
        self.h.collect()
        self.assertEqual(self.kinds("turn.stopped", "session.said"), ["turn.stopped", "session.said"])

    def test_a_hook_after_the_host_was_polled_waits_for_the_next_tick_and_is_written_once(self):
        self.clock.t = 1001
        until, lines = self.host.stamped()
        self.assertEqual(lines, [])
        self.hook(1002, "turn.stopped")                                    # stamped after the host poll
        self.h.host = types.SimpleNamespace(stamped=lambda: (until, []))  # a tick that polled the host before the hook
        self.clock.t = 1003
        self.h.collect()
        self.assertEqual(self.kinds("turn.stopped"), [])
        self.assertEqual(spool.pending(self.h.spool_dir), 1)                # the file waits, not lost
        self.h.host = self.host
        self.h.collect()
        self.h.collect()
        self.assertEqual(self.kinds("turn.stopped"), ["turn.stopped"])
        self.assertEqual(spool.pending(self.h.spool_dir), 0)

    def test_a_fact_stamped_in_the_future_is_not_held_forever(self):
        # the machine clock was set back: a file stamped "from the future" is written now, not held until the clock
        # catches up
        self.hook(1000 + 3600, "turn.stopped")
        self.tick(1001)
        self.assertEqual(self.kinds("turn.stopped"), ["turn.stopped"])

    def test_a_host_without_stamps_keeps_the_old_order_hooks_then_its_facts(self):
        # the tmux host and the tests' fake hosts give only poll(): their facts go after the spool, as before
        facts = [("session.said", "a", {"text": "a line"})]
        self.h.host = types.SimpleNamespace(poll=lambda: [facts.pop()] if facts else [], info=lambda n: None,
                                            alive=lambda n: True, screen_dialogs=False)
        self.hook(999, "turn.started", marker=None, prompt_head="x")
        self.h.collect()
        self.assertEqual(self.kinds("turn.started", "session.said"), ["turn.started", "session.said"])

    def test_equal_times_put_the_hook_first_and_a_hook_at_the_poll_is_taken_now(self):
        # coarse clock (Windows, Python 3.12 — step ~15.6 ms): a hook and a line with the same time. Claude Code waits
        # for the hook (UserPromptSubmit, Stop) and writes further only after it, while a line before the hook is
        # earlier than the hook process launch (≥ 50 ms): equal time means the hook goes first. A hook with time
        # exactly P is in this tick, before the lines with P
        self.hook(1005, "turn.stopped")
        self.stdout(1005, line(type="result", result="done", num_turns=1, duration_ms=10))
        self.tick(1005)
        self.assertEqual(self.kinds("turn.stopped", "turn.result"), ["turn.stopped", "turn.result"])

    def test_after_the_clock_went_back_each_source_keeps_its_own_order(self):
        # the clock was set back mid-tick: a line before the jump has a late time, after it an early one; the same in
        # the spool. Time only decides whose fact is next; a source's own order (host queue, file name) is not broken
        self.stdout(1050, line(type="assistant", message={"content": [{"type": "text", "text": "before the jump"}]}))
        self.stdout(1001, line(type="assistant", message={"content": [{"type": "text", "text": "after the jump"}]}))
        self.hook(1060, "message.received", None, to="a", text="first", **OWNER)
        self.hook(1002, "message.received", None, to="a", text="second", **OWNER)
        self.tick(1003)
        self.assertEqual(self.said(), ["before the jump", "after the jump"])
        got = [e["data"]["text"] for e in journal.read(self.h.journal.path) if e["type"] == "message.received"]
        self.assertEqual(got, ["first", "second"])

    def test_the_spool_keeps_its_name_order_even_when_the_times_of_processes_disagree(self):
        # the file time is set by the hook or CLI process, the name strictly grows (spool._stamp): an insert placed
        # after its message does not jump ahead of it, even if the clocks of different processes differ by milliseconds
        self.hook(1001.010, "message.received", None, to="a", text="first", **OWNER)
        self.hook(1001.005, "message.received", None, to="a", text="second", **OWNER)
        self.tick(1002)
        got = [e["data"]["text"] for e in journal.read(self.h.journal.path) if e["type"] == "message.received"]
        self.assertEqual(got, ["first", "second"])

    def test_a_line_that_cannot_be_parsed_does_not_take_the_other_lines_of_the_tick_with_it(self):
        self.stdout(1001, line(type="assistant", message={"content": [{"type": "text", "text": "one"}]}),
                    line(type="assistant", message={"content": [1]}),           # malformed: the block is not an object
                    line(type="result", result="result", num_turns=1, duration_ms=10))
        self.tick(1002)
        self.assertEqual(self.said(), ["one"])
        self.assertEqual(self.kinds("turn.result"), ["turn.result"])


class StampedPoll(unittest.TestCase):
    def setUp(self):
        state = tempfile.mkdtemp(prefix="order-h-")
        self.addCleanup(shutil.rmtree, state, ignore_errors=True)
        self.host = StreamHost(state, claude=("claude-not-run",))
        self.host.clock = Clock(1000.0)

    def test_a_bad_block_keeps_the_good_ones_of_its_line(self):
        self.host.reader("a", types.SimpleNamespace(stdout=[
            line(type="assistant", message={"content": [{"type": "text", "text": "done"}, 1]})]))()
        self.host.clock.t = 1001.0
        _, got = self.host.stamped()
        self.assertEqual([f[1] for f in got], ["session.said"])

    def test_text_content_given_as_a_string_still_reaches_the_owner(self):
        self.host.reader("a", types.SimpleNamespace(stdout=[line(type="assistant", message={"content": "answer"})]))()
        self.host.clock.t = 1001.0
        _, got = self.host.stamped()
        self.assertEqual([(f[1], f[3].get("text")) for f in got], [("session.said", "answer")])

    def test_a_bare_json_value_on_stdout_does_not_stop_the_reader(self):
        self.host.reader("a", types.SimpleNamespace(stdout=["42\n", line(type="result", result="r", num_turns=1)]))()
        self.host.clock.t = 1001.0
        _, got = self.host.stamped()
        self.assertIn("turn.result", [f[1] for f in got])

    def test_stamped_lines_are_those_read_up_to_the_poll_and_poll_still_gives_triples(self):
        self.host.reader("a", types.SimpleNamespace(stdout=[line(type="result", result="r", num_turns=1)]))()
        self.host.clock.t = 1001.0
        until, facts = self.host.stamped()
        ((at, type, name, data),) = facts
        self.assertEqual((until, at, type, name, data["text"]), (1001.0, 1000.0, "turn.result", "a", "r"))
        self.host.inbox.put(("a", {"type": "result", "result": "r2", "num_turns": 1}))   # as old tests put it
        self.assertEqual([f[0] for f in self.host.poll()], ["turn.result"])

    def test_every_fact_of_a_line_has_the_time_of_its_line(self):
        self.host.reader("a", types.SimpleNamespace(stdout=[line(type="assistant", message={"content": [
            {"type": "text", "text": "x"}, {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]})]))()
        self.host.clock.t = 1000.5
        self.host.reader("a", types.SimpleNamespace(stdout=[line(type="result", result="r", num_turns=1)]))()
        self.host.clock.t = 1001.0
        _, facts = self.host.stamped()
        self.assertEqual([(at, type) for at, type, *_ in facts],
                         [(1000.0, "session.said"), (1000.0, "turn.tool"), (1000.5, "turn.result")])


if __name__ == "__main__":
    unittest.main()
