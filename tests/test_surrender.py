"""The surrender detector is a watchdog option (owner decision 2026-10-06: it is optional; a position changes only on
new data). The owner doubted ("are you sure?"), and the session answered in the same turn with a concession ("you are
right, changed it") without checking the disputed point (no read, no search, no command) - the core tells it to check
properly and answer again with a line "Rechecked: ... -> hold / change / partly"; its new answer goes to the owner in
the same thread. Enabled by `"surrender": true` in watch.json. By rules, without a model."""
import tests  # noqa: F401 - ignore the machine's settings
import json
import os
import re
import tempfile
import unittest

from core import reducer, spool
from modules.watch import watch
from tests.fakes import lang

OWNER = {"channel": "web", "owner": True}
DOUBTS = lang.words("watch", "doubt")         # the owner's Russian doubt ("I don't think so", "it seems to me"...)
GIVE_IN = lang.words("watch", "surrender")    # a session's Russian concession ("my mistake", "changed", "fixed"...)
MISTAKE, CHANGED = GIVE_IN[2], GIVE_IN[6]
RECHECKED = lang.word("watch", "recheck").capitalize()
DOUBT = f"{DOUBTS[6].capitalize()} the port is 8080?"   # "it seems to me the port is 8080?"


def _first_form(fragment):
    """A regex alternative of the data written out: optional groups and classes, word edges and the rest-of-sentence
    tails dropped, the first choice of a group taken."""
    for pattern, by in ((r"\([^()]*\)\?", ""), (r"\(([^|()]*)\|[^()]*\)", r"\1"), (r"\[[^\]]*\][?*]", ""),
                        (r"\\[b?]", "")):
        fragment = re.sub(pattern, by, fragment)
    return fragment.strip()


DOUBT_FORMS = [_first_form(x) for x in lang._top(lang.data("watch", "doubt")) if x not in DOUBTS]


def write(state, events):
    with open(os.path.join(state, "journal.jsonl"), "a") as f:
        for seq, ts, type, entity, data in events:
            f.write(json.dumps({"seq": seq, "ts": ts, "type": type, "entity": entity, "data": data, "src": "t"},
                               ensure_ascii=False) + "\n")


class Rules(unittest.TestCase):
    def test_doubt(self):
        # the phrases with no plain word in the data (a regex of their own) — each written out by its first form
        for t in tuple(f"{w}, the cache has nothing to do with it?" for w in DOUBT_FORMS + DOUBTS):
            self.assertTrue(watch.doubt(t), t)
        for t in (f"{lang.word('watch', 'hub_order').capitalize()} the log report", "thanks, great",
                  f"{lang.word('watch', 'promise_verbs', 6).capitalize()} the certificate on prod"):
            self.assertFalse(watch.doubt(t), t)

    def test_surrender(self):
        for t in (f"{CHANGED.capitalize()} to 8080.", f"{lang.word('words', 'yes', 1).capitalize()} — {GIVE_IN[7]}.",
                  f"{GIVE_IN[8].capitalize()}.", f"{MISTAKE.capitalize()}, {GIVE_IN[5]}."):
            self.assertTrue(watch.surrender(t), t)
        for t in (f"{RECHECKED}: yesterday's log → hold, the cache has nothing to do with it.",
                  f"{lang.word('words', 'lessons_no').capitalize()}, the port is correct: 8080 in the config.",
                  f"{MISTAKE.capitalize()}.\n\n{RECHECKED}: the config → change"):
            self.assertFalse(watch.surrender(t), t)

    def test_checked(self):
        hub_send = {"name": "Bash", "input": {"command": "hub send maps 'ok'"}}
        self.assertFalse(watch.checked([{"name": "Edit", "input": {"file_path": "/w/a.py"}}, {"name": "TodoWrite"},
                                        hub_send]))
        self.assertTrue(watch.checked([{"name": "Read", "input": {"file_path": "/w/a.py"}}]))
        self.assertTrue(watch.checked([{"name": "Bash", "input": {"command": "grep -n port conf.yml"}}]))


class Detector(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="surrender-")
        write(self.state, [(1, 100, "session.requested", "hub", {"cwd": "/h", "session_id": "s-h", "kind": "hub", **OWNER}),
                           (2, 100, "session.requested", "a", {"cwd": "/w", "session_id": "s-a", **OWNER}),
                           (3, 101, "session.started", "hub", {}), (4, 101, "session.started", "a", {})])
        self.cfg = {"surrender": True}
        self.w = watch.Watcher(self.state, self.cfg, ask=lambda t: {"trouble": False}, needs=None, urgent=None,
                               recap=lambda text: "")
        self.step(102)

    def step(self, now):
        self.w.step(now)
        return [ev for path, ev in spool.take(os.path.join(self.state, "spool")) if not os.unlink(path)
                if ev["type"] == "watch.surrender"]

    def turn(self, seq, doubt, tools, answer):
        evs = [(seq, 110, "message.received", None, {"to": "a", "text": doubt, **OWNER}),
               (seq + 1, 111, "turn.started", "a", {"marker": f"m{seq}", "prompt_head": doubt})]
        for i, t in enumerate(tools):
            evs.append((seq + 2 + i, 112, "turn.tool", "a", {"id": f"t{i}", **t}))
        evs += [(seq + 10, 120, "turn.result", "a", {"text": answer}), (seq + 11, 121, "turn.stopped", "a", {})]
        write(self.state, evs)

    def test_a_surrender_without_a_check_is_caught_once(self):
        self.turn(10, DOUBT, [{"name": "Edit", "input": {"file_path": "/w/conf.yml"}}],
                  f"{MISTAKE.capitalize()}, {CHANGED} to 9090.")
        (ev,) = self.step(125)
        self.assertEqual((ev["entity"], ev["data"]["message"]), ("a", "m10"))
        self.assertEqual(self.step(130), [])

    def test_a_checked_answer_or_no_doubt_passes(self):
        self.turn(10, DOUBT, [{"name": "Read", "input": {"file_path": "/w/conf.yml"}}],
                  f"{MISTAKE.capitalize()}, the config has 9090 — {CHANGED}.")
        self.turn(30, f"{lang.word('words', 'yes', 1).capitalize()}, change the port to 9090", [], f"{CHANGED.capitalize()}.")
        self.assertEqual(self.step(125), [])

    def test_off_by_default(self):
        self.cfg.clear()
        self.turn(10, DOUBT, [], f"{MISTAKE.capitalize()}, {CHANGED}.")
        self.assertEqual(self.step(125), [])


class Core(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("session.requested", "hub", cwd="/h", session_id="s-h", kind="hub", **OWNER)
        self.ev("session.requested", "a", cwd="/w", session_id="s-a", **OWNER)
        self.ev("session.started", "a")
        self.ev("message.received", None, to="a", text=DOUBT, **OWNER)
        self.mid = f"m{self.seq}"

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 1000 + self.seq, "type": type,
                                                   "entity": entity, "data": data})
        return why

    def test_the_session_is_asked_to_recheck_and_answer_the_owner_again(self):
        self.assertIsNone(self.ev("watch.surrender", "a", message=self.mid, channel="cli", owner=True))
        (m,) = [m for m in self.st["messages"].values() if m.get("re") == self.mid]
        self.assertEqual(m["session"], "a")
        for part in ("Rechecked:", "hold / change / partly", DOUBT):
            self.assertIn(part, m["text"])
        self.assertIsNotNone(self.ev("watch.surrender", "a", message=self.mid, channel="cli", owner=True))  # once
        self.assertIsNotNone(self.ev("watch.surrender", "a", message=self.mid, channel="session", owner=False,
                                     sender="hub"))

    def test_only_for_the_owners_message(self):
        self.ev("message.received", None, to="a", text=DOUBT, channel="session", owner=False, sender="hub")
        self.assertIsNotNone(self.ev("watch.surrender", "a", message=f"m{self.seq}", channel="cli", owner=True))


if __name__ == "__main__":
    unittest.main()
