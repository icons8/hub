"""Invariants of the fold that every past regression in the reducer and the feed broke in one way or another.

Run on every scenario in tests/replays (real event shapes, a fixed clock from the scenario itself):
  - purity: the event and the input state are not changed by `reducer.reduce`, a rejected event leaves the state equal;
  - restart: the state written to disk and read back at any step continues the fold to the same end state
    (the core restarts between any two events);
  - determinism: the same events fold to the same state, and the feed reader gives the same lines.
No model, no tmux, no clock: each event carries its own time."""
import tests  # noqa: F401 — hide the machine's settings
import copy
import json
import unittest

from core import reducer, views
from tests.test_replays import REPLAYS, load, subset  # noqa: F401


def cases():
    for path in sorted(REPLAYS.glob("*.json")):
        yield path.stem, load(path)["events"]


def numbered(events):
    return [{"seq": seq, "ts": raw["t"], "src": "test", "type": raw["type"], "entity": raw.get("entity"),
             "data": raw.get("data") or {}} for seq, raw in enumerate(events, 1)]


def dump(state):
    return json.dumps(state, sort_keys=True, ensure_ascii=False)


def fold(events, step=lambda s: s):
    state, rejected = reducer.empty(), []
    for ev in events:
        state, _, why = reducer.reduce(step(state), ev)
        rejected.append(why)
    return state, rejected


class FoldInvariants(unittest.TestCase):
    def test_scenarios_are_not_empty(self):
        self.assertGreater(len(list(cases())), 10)

    def test_reduce_changes_neither_the_event_nor_the_state_it_got(self):
        for name, raw in cases():
            with self.subTest(name):
                state = reducer.empty()
                for ev in numbered(raw):
                    before, ev_before = copy.deepcopy(state), copy.deepcopy(ev)
                    new, _, why = reducer.reduce(state, ev)
                    self.assertEqual(ev, ev_before, f"seq {ev['seq']} {ev['type']}: the event was changed")
                    self.assertEqual(state, before, f"seq {ev['seq']} {ev['type']}: the input state was changed")
                    if why is not None:
                        # the cursor moves over a rejected event, nothing else does
                        self.assertEqual({**new, "seq": 0}, {**before, "seq": 0},
                                         f"seq {ev['seq']} {ev['type']}: a rejected event changed the state")
                    state = new

    def test_the_state_survives_a_restart_between_any_two_events(self):
        # the core writes the state to disk and reads it back; whatever JSON cannot carry (a number key, a tuple,
        # a set) must not decide the rest of the fold
        again = lambda s: json.loads(json.dumps(s, ensure_ascii=False))   # noqa: E731
        for name, raw in cases():
            with self.subTest(name):
                events = numbered(raw)
                straight, why_a = fold(events)
                reloaded, why_b = fold(events, again)
                self.assertEqual(why_a, why_b)
                self.assertEqual(dump(straight), dump(reloaded))

    def test_the_same_events_fold_to_the_same_state_and_feed(self):
        for name, raw in cases():
            with self.subTest(name):
                events = numbered(raw)
                (a, _), (b, _) = fold(events), fold(copy.deepcopy(events))
                self.assertEqual(dump(a), dump(b))
                lines = []
                for _ in range(2):
                    reader = views.Reader()
                    for ev in copy.deepcopy(events):
                        reader.feed(ev)
                    lines.append(dump(reader.timelines))
                self.assertEqual(lines[0], lines[1])


    def test_the_state_read_back_from_a_snapshot_event_continues_to_the_same_end(self):
        # the journal rotates: the new file starts with the whole state (core.snapshot), and the fold goes on from it.
        # What the snapshot (secrets removed, an old form upgraded) loses would be lost on every rotation
        for name, raw in cases():
            with self.subTest(name):
                events = numbered(raw)
                straight, _ = fold(events)
                for cut in range(1, len(events)):
                    before, _ = fold(events[:cut])
                    state = reducer.restore({"seq": cut, "type": "core.snapshot",
                                             "data": {"why": "rotate-start", "state": copy.deepcopy(before)}})
                    for ev in events[cut:]:
                        state, _, _ = reducer.reduce(state, ev)
                    self.assertEqual(dump(state), dump(straight), f"snapshot after event {cut}")


def with_verdicts(events):
    """The journal as hubd writes it: a rejected event is followed by its core.rejected line."""
    state, out = reducer.empty(), []
    for ev in events:
        state, _, why = reducer.reduce(state, {**ev, "seq": len(out) + 1})
        out.append({**ev, "seq": len(out) + 1})
        if why is not None:
            out.append({"seq": len(out) + 1, "ts": ev["ts"], "src": "core", "type": "core.rejected", "entity": None,
                        "data": {"of": len(out), "why": str(why)}})
            state, _, _ = reducer.reduce(state, out[-1])
    return out


class FeedInvariants(unittest.TestCase):
    def test_every_timeline_row_has_its_own_key_and_the_rows_go_by_seq(self):
        # a repeated key is a duplicated line on the page; a row out of seq order is a line that came late or was lost
        for name, raw in cases():
            with self.subTest(name):
                reader = views.Reader()
                for ev in numbered(raw):
                    reader.feed(ev)
                for session, rows in reader.timelines.items():
                    keys = [r["k"] for r in rows]
                    self.assertEqual(len(keys), len(set(keys)), f"{session}: a repeated row key")
                    seqs = [r["seq"] for r in rows if isinstance(r.get("seq"), int)]
                    self.assertEqual(seqs, sorted(seqs), f"{session}: rows out of seq order")

    def test_batch_reading_with_known_verdicts_gives_the_same_feed_as_event_by_event(self):
        # a journal batch is folded in place (the outcome of each event is known from the next line), one event at a
        # time with a copy: two code paths of the same fold must not drift apart
        for name, raw in cases():
            with self.subTest(name):
                events = with_verdicts(numbered(raw))
                one, batch = views.Reader(), views.Reader()
                for ev in copy.deepcopy(events):
                    one.feed(ev)
                batch.feed_all(copy.deepcopy(events))
                self.assertEqual(dump(one.state), dump(batch.state))
                self.assertEqual(dump(one.timelines), dump(batch.timelines))

    def test_a_snapshot_that_reuses_the_previous_one_equals_a_full_snapshot(self):
        # the snapshot is built on every event; unchanged histories are taken from the previous one. A history that
        # changed but was reused is a stale page
        for name, raw in cases():
            with self.subTest(name):
                reader, previous = views.Reader(), None
                for ev in numbered(raw):
                    reader.feed(ev)
                    now = ev["ts"] + 1
                    fast = reader.snapshot(now, previous=previous)
                    self.assertEqual(dump(fast["timelines"]), dump(reader.snapshot(now)["timelines"]),
                                     f"seq {ev['seq']} {ev['type']}")
                    previous = fast


if __name__ == "__main__":
    unittest.main()
