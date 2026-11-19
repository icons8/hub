"""A patch of /api/stream?delta=2 carries only the changed items of a big section (stream-delta): one changed session
used to resend the whole `sessions` list, one new message the whole `messages` dictionary — hundreds of KB per event.
Bytes are counted, not time. The page's applier (static/js/stream-delta.js) and the reference one here must give the
very state a whole-section patch gives: the same values in the same order."""
import tests  # noqa: F401
import http.client
import json
import random
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from tests.test_web_stream_shared import StreamShared
from modules.web import stream_delta

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
dumps = lambda v: json.dumps(v, ensure_ascii=False)


def apply(snap, patch):
    """The reference applier — the rules of EXTENDING.md word for word; None — the frame does not fit, reconnect."""
    patch = dict(patch)
    n, deltas, gone = patch.pop("_n", None), patch.pop("_delta", {}), patch.pop("_gone", [])
    if (deltas if n is None else n != snap.get("_n", 0) + 1):
        return None
    out = {**snap, **patch}
    for k in gone:
        out.pop(k, None)
    if n is not None:
        out["_n"] = n
    for name, d in deltas.items():
        cur, by = out.get(name), d.get("by")
        if by is None:
            if not isinstance(cur, dict):
                return None
            rows = dict(cur)
            news = d.get("set", {})
        else:
            if not isinstance(cur, list):
                return None
            rows = {r[by]: r for r in cur}
            news = {r[by]: r for r in d.get("set", [])}
        for k in d.get("del", []):
            rows.pop(k, None)
        rows.update(news)   # an existing key keeps its place, a new one goes to the end
        if "order" in d:
            if sorted(d["order"]) != sorted(rows):
                return None
            rows = {k: rows[k] for k in d["order"]}
        out[name] = rows if by is None else list(rows.values())
    return out


def patch_of(old, new):
    """What the stream sends for old → new (both {section: value}), parsed."""
    n = old.get("_n", 0) + 1
    return json.loads(stream_delta.patch({k: dumps(v) for k, v in old.items() if k != "_n"},
                                         {k: dumps(v) for k, v in new.items()}, n))


def big(n=400, pad="x" * 300):
    return {"sessions": [{"name": f"s{i}", "state": "idle", "pad": pad} for i in range(n)],
            "messages": {f"m{i}": {"id": f"m{i}", "text": pad} for i in range(n)},
            "seq": 1, "features": ["stream.delta2"]}


class Delta(unittest.TestCase):
    def same(self, old, new):
        p = patch_of(old, new)
        got = apply(old, p)
        self.assertEqual(got.pop("_n"), p.pop("_n"))
        self.assertEqual(dumps(got), dumps(new), "the order of items and keys counts too")
        return p

    def test_one_changed_item_is_a_small_patch(self):
        old = big()
        whole = len(dumps({"sessions": old["sessions"]}).encode())
        self.assertGreater(whole, 100_000)
        new = {**old, "seq": 2, "sessions": [{**s, "state": "working"} if s["name"] == "s7" else s for s in old["sessions"]]}
        raw = stream_delta.patch({k: dumps(v) for k, v in old.items()}, {k: dumps(v) for k, v in new.items()})
        self.assertLess(len(raw.encode()), 10_000)
        p = self.same(old, new)
        self.assertEqual(p["_delta"]["sessions"], {"by": "name", "set": [{"name": "s7", "state": "working", "pad": "x" * 300}]})
        self.assertEqual(p["seq"], 2)
        self.assertNotIn("sessions", p)
        self.assertNotIn("messages", p["_delta"])

    def test_one_new_message_is_a_small_patch(self):
        old = big()
        new = {**old, "messages": {**old["messages"], "m900": {"id": "m900", "text": "hi"}}}
        raw = stream_delta.patch({k: dumps(v) for k, v in old.items()}, {k: dumps(v) for k, v in new.items()})
        self.assertLess(len(raw.encode()), 10_000)
        self.assertEqual(self.same(old, new)["_delta"], {"messages": {"set": {"m900": {"id": "m900", "text": "hi"}}}})

    def test_add_change_remove_and_reorder(self):
        old = big(40)
        rows = old["sessions"]
        cases = {
            "add at the end": rows + [{"name": "new", "state": "idle"}],
            "add in front": [{"name": "new", "state": "idle"}] + rows,
            "remove": rows[:5] + rows[6:],
            "reorder": rows[::-1],
            "move to front": [rows[9]] + rows[:9] + rows[10:],
            "all at once": [{"name": "new"}] + [{**r, "state": "x"} if i % 7 == 0 else r for i, r in enumerate(rows[3:])][::-1],
            "emptied": [],
        }
        for name, sessions in cases.items():
            with self.subTest(name):
                self.same(old, {**old, "sessions": sessions})
        self.assertNotIn("order", patch_of(old, {**old, "sessions": cases["add at the end"]})["_delta"]["sessions"])
        self.assertNotIn("order", patch_of(old, {**old, "sessions": cases["remove"]})["_delta"]["sessions"])
        self.assertIn("order", patch_of(old, {**old, "sessions": cases["move to front"]})["_delta"]["sessions"])
        msgs = old["messages"]
        for name, messages in {"removed": {k: v for k, v in msgs.items() if k != "m3"},
                               "key order": dict(reversed(list(msgs.items()))),
                               "changed": {**msgs, "m3": {"id": "m3", "text": "edited"}}}.items():
            with self.subTest(name):
                self.same(old, {**old, "messages": messages})

    def test_what_has_no_keys_goes_whole(self):
        pad = "x" * 300
        for name, before, after in [
                ("small section", {"a": 1}, {"a": 2}),
                ("list of strings", [pad] * 40, [pad] * 41),
                ("no common key field", [{"n": i, "pad": pad} for i in range(40)], [{"n": i, "pad": pad} for i in range(41)]),
                ("key repeats", [{"id": "a", "pad": pad}] * 40, [{"id": "a", "pad": pad}] * 41),
                ("key is not a string", [{"id": i, "pad": pad} for i in range(40)], [{"id": i, "pad": pad} for i in range(41)]),
                ("type changed", [{"id": str(i), "pad": pad} for i in range(40)], {str(i): pad for i in range(40)}),
                ("key field changed", [{"id": str(i), "pad": pad} for i in range(40)], [{"name": str(i), "pad": pad} for i in range(40)]),
                ("all changed and reordered", {str(i): pad for i in range(40)}, {str(i): pad + "y" for i in reversed(range(40))})]:
            with self.subTest(name):
                p = self.same({"x": before, "seq": 1}, {"x": after, "seq": 1})
                self.assertEqual(p, {"x": after})

    def test_new_and_vanished_sections(self):
        old = big(40)
        new = {k: v for k, v in old.items() if k != "features"} | {"extra": {"a": 1}}
        p = self.same(old, new)
        self.assertEqual(p, {"extra": {"a": 1}, "_gone": ["features"]})

    def test_nothing_changed_is_no_patch(self):
        old = {k: dumps(v) for k, v in big(5).items()}
        self.assertIsNone(stream_delta.patch(old, dict(old)))

    def test_a_section_is_parsed_once_per_change_not_once_per_stream(self):
        states = [big(40)]
        for i in range(3):
            states.append({**states[-1], "sessions": states[-1]["sessions"] + [{"name": f"new{i}"}]})
        texts = [{k: dumps(v) for k, v in s.items()} for s in states]
        stream_delta._splits.clear(), stream_delta._deltas.clear()
        with mock.patch.object(stream_delta, "split", wraps=stream_delta.split) as split:
            for step in range(3):
                bodies = {stream_delta.patch(texts[step], texts[step + 1], step + 1) for stream in range(8)}
                self.assertEqual(len(bodies), 1)
            self.assertEqual(split.call_count, 4, "the first old side, then each new side once — for 8 streams")
            lagging = stream_delta.patch(texts[0], texts[3], 1)   # a stream two steps behind: its own delta, right too
            self.assertEqual(split.call_count, 5)
        self.assertEqual([s["name"] for s in json.loads(lagging)["_delta"]["sessions"]["set"]], ["new0", "new1", "new2"])
        self.assertEqual(sorted(stream_delta._splits), ["sessions"], "one split per big changed section is kept")

    def test_frame_number_and_a_lost_frame(self):
        a, b, c = big(40), big(40), big(40)
        b["sessions"] = b["sessions"] + [{"name": "one"}]
        c["sessions"] = b["sessions"] + [{"name": "two"}]
        texts = [{k: dumps(v) for k, v in s.items()} for s in (a, b, c)]
        p1 = json.loads(stream_delta.patch(texts[0], texts[1], 1))
        p2 = json.loads(stream_delta.patch(texts[1], texts[2], 2))
        self.assertEqual((p1["_n"], p2["_n"]), (1, 2))
        got = apply(apply(a, p1), p2)
        got.pop("_n")
        self.assertEqual(dumps(got), dumps(c))
        self.assertIsNone(apply(a, p2), "frame 1 was lost: the client must reconnect, not apply frame 2")
        self.assertIsNone(apply(apply(a, p1), p1), "the same frame twice")

    def test_random_walks_end_in_the_same_state(self):
        rnd = random.Random(7)
        for walk in range(30):
            state = big(rnd.randint(0, 30), "x" * rnd.randint(100, 400))
            seen = dict(state)
            for step in range(8):
                rows = [dict(r) for r in state["sessions"]]
                msgs = dict(state["messages"])
                for _ in range(rnd.randint(0, 4)):
                    op = rnd.choice(["add", "front", "del", "edit", "shuffle", "msg", "unmsg", "none"])
                    if op == "add":
                        rows.append({"name": f"n{walk}-{step}-{rnd.random()}", "state": "new"})
                    elif op == "front":
                        rows.insert(0, {"name": f"f{walk}-{step}-{rnd.random()}", "state": "new"})
                    elif op == "del" and rows:
                        rows.pop(rnd.randrange(len(rows)))
                    elif op == "edit" and rows:
                        rows[rnd.randrange(len(rows))]["state"] = str(rnd.random())
                    elif op == "shuffle":
                        rnd.shuffle(rows)
                    elif op == "msg":
                        msgs[f"m{rnd.randint(0, 60)}"] = {"text": str(rnd.random())}
                    elif op == "unmsg" and msgs:
                        msgs.pop(rnd.choice(list(msgs)))
                state = {**state, "sessions": rows, "messages": msgs, "seq": state["seq"] + 1}
                seen = apply(seen, patch_of(seen, state))
                self.assertEqual(dumps({k: v for k, v in seen.items() if k != "_n"}), dumps(state), f"walk {walk} step {step}")
            self.assertEqual(seen["_n"], 8)


@unittest.skipUnless(NODE, "Node is required to run the page's applier")
class PageApplier(unittest.TestCase):
    """static/js/stream-delta.js against the same scenarios: the page ends in the state a whole patch would give."""

    def run_js(self, cases):
        body = r'''
import fs from 'node:fs';
const m = await import('data:text/javascript;base64,' + Buffer.from(fs.readFileSync('modules/web/static/js/stream-delta.js', 'utf8')).toString('base64'));
const cases = JSON.parse(fs.readFileSync(0, 'utf8'));
const out = cases.map((c) => { let s = c.snap; const kept = []; for (const p of c.patches) { const before = s; s = s && m.applyPatch(s, p);
  if (s && before) kept.push(Object.keys(before).filter((k) => !k.startsWith('_') && before[k] === s[k])); }
  return { state: s, kept, n: s ? s._n : null, keys: s ? Object.keys(s).filter((k) => k.startsWith('_')) : [] }; });
process.stdout.write(JSON.stringify(out));
'''
        r = subprocess.run([NODE, "--input-type=module", "-e", body], cwd=ROOT, input=json.dumps(cases),
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr[-4000:])
        return json.loads(r.stdout)

    def test_page_applier_matches_whole_patches(self):
        rnd = random.Random(11)
        cases, wanted = [], []
        for walk in range(25):
            state = big(rnd.randint(0, 25), "x" * rnd.randint(100, 400))
            case = {"snap": state, "patches": []}
            for step in range(6):
                rows = [dict(r) for r in state["sessions"]]
                msgs = dict(state["messages"])
                for _ in range(rnd.randint(1, 4)):
                    op = rnd.choice(["add", "front", "del", "edit", "shuffle", "msg", "unmsg"])
                    if op == "add":
                        rows.append({"name": f"n{step}-{rnd.random()}"})
                    elif op == "front":
                        rows.insert(0, {"name": f"f{step}-{rnd.random()}"})
                    elif op == "del" and rows:
                        rows.pop(rnd.randrange(len(rows)))
                    elif op == "edit" and rows:
                        rows[rnd.randrange(len(rows))]["state"] = str(rnd.random())
                    elif op == "shuffle":
                        rnd.shuffle(rows)
                    elif op == "msg":
                        msgs[f"m{rnd.randint(0, 60)}"] = {"text": str(rnd.random())}
                    elif op == "unmsg" and msgs:
                        msgs.pop(rnd.choice(list(msgs)))
                new = {**state, "sessions": rows, "messages": msgs, "seq": state["seq"] + 1}
                raw = stream_delta.patch({k: dumps(v) for k, v in state.items()}, {k: dumps(v) for k, v in new.items()}, step + 1)
                case["patches"].append(json.loads(raw))
                state = new
            cases.append(case)
            wanted.append(state)
        for got, want in zip(self.run_js(cases), wanted):
            self.assertEqual(dumps(got["state"]), dumps(want))
            # the frame number is the page's own: not a section a plugin sees in hub.snapshot()
            self.assertEqual((got["n"], got["keys"]), (6, []))

    def test_page_applier_old_patches_lost_frames_and_untouched_parts(self):
        a = big(30)
        b = {**a, "sessions": a["sessions"] + [{"name": "one"}]}
        c = {**b, "sessions": b["sessions"] + [{"name": "two"}]}
        t = [{k: dumps(v) for k, v in s.items()} for s in (a, b, c)]
        p1, p2 = json.loads(stream_delta.patch(t[0], t[1], 1)), json.loads(stream_delta.patch(t[1], t[2], 2))
        old_format = {"sessions": b["sessions"], "_gone": ["features"]}   # a server without deltas: whole sections, no _n
        out = self.run_js([{"snap": a, "patches": [p2]}, {"snap": a, "patches": [p1, p1]}, {"snap": a, "patches": [p1, p2]},
                           {"snap": a, "patches": [old_format]},
                           {"snap": a, "patches": [{"_n": 1, "_delta": {"seq": {"set": {"a": 1}}}}]},
                           {"snap": {"x": {"a": 1, "b": 2}}, "patches": [{"_n": 1, "_delta": {"x": {"order": ["a", "a"]}}}]},
                           {"snap": a, "patches": [{k: v for k, v in p1.items() if k != "_n"}]},
                           {"snap": a, "patches": [p1, {"seq": 5}, p2]}])
        self.assertIsNone(out[0]["state"], "a lost frame: null — the page reconnects")
        self.assertIsNone(out[1]["state"], "a repeated frame")
        self.assertEqual(dumps(out[2]["state"]["sessions"]), dumps(c["sessions"]))
        self.assertIn("messages", out[2]["kept"][0], "an untouched section stays the same object")
        self.assertNotIn("sessions", out[2]["kept"][0], "a changed section is a new object: the page compares by identity")
        self.assertEqual(dumps(out[3]["state"]), dumps({k: v for k, v in b.items() if k != "features"}))
        self.assertIsNone(out[4]["state"], "a delta for what is not a keyed section")
        self.assertIsNone(out[5]["state"], "order must name every key once")
        self.assertIsNone(out[6]["state"], "a delta without a frame number")
        # a whole patch without a number in between keeps the number held (it is not carried by the snapshot's spread)
        self.assertEqual(dumps(out[7]["state"]["sessions"]), dumps(c["sessions"]))
        self.assertIsNone(apply({"x": {"a": 1, "b": 2}}, {"_n": 1, "_delta": {"x": {"order": ["a", "a"]}}}))
        self.assertIsNone(apply(a, {k: v for k, v in p1.items() if k != "_n"}))


class DeltaStream(StreamShared):
    """The stream itself: ?delta=2 gets item deltas and frame numbers; without it and with ?delta=1 — whole sections."""

    def setUp(self):
        super().setUp()
        for i in range(60):   # a section worth a delta: small ones go whole
            self.j.append("session.requested", f"s{i:02}", {"cwd": "/w/" + "x" * 200, "session_id": f"id-{i}", "channel": "cli", "owner": True})
        self.web.tail.poll()

    def open(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(c.close)
        c.request("GET", path, headers={"Host": f"127.0.0.1:{self.port}"})
        r = c.getresponse()
        self.addCleanup(r.close)
        self.assertEqual(r.status, 200)
        return r

    def event(self, r):
        kind = None
        while True:
            line = r.fp.readline().decode()
            self.assertTrue(line, "stream closed before an event")
            if line.startswith("event:"):
                kind = line[6:].strip()
            elif line.startswith("data:"):
                return kind, line[5:].strip()

    def sessions_patch(self, r, state=None):
        for _ in range(20):   # a patch of another key (the clock) may come first
            kind, raw = self.event(r)
            self.assertEqual(kind, "patch")
            p = json.loads(raw)
            if state is not None:
                state = apply(state, p)
                self.assertIsNotNone(state)
            if "sessions" in p or "sessions" in p.get("_delta", {}):
                return p, raw, state
        self.fail("no patch with sessions")

    def test_delta_stream_sends_one_session_and_old_streams_the_whole_list(self):
        new, old, one = self.open("/api/stream?delta=2"), self.open("/api/stream"), self.open("/api/stream?delta=1")
        kind, raw = self.event(new)
        self.assertEqual(kind, "snapshot")
        state = json.loads(raw)
        self.assertIn("stream.delta2", state["features"])
        self.assertNotIn("_n", state)
        for r in (old, one):
            self.assertEqual(self.event(r)[0], "snapshot")
        self.j.append("session.requested", "maps", {"cwd": "/w", "session_id": "s-maps", "channel": "cli", "owner": True})
        self.web.tail.poll()
        p, raw, state = self.sessions_patch(new, state)
        self.assertNotIn("sessions", p)
        self.assertEqual([s["name"] for s in p["_delta"]["sessions"]["set"]], ["maps"])
        self.assertEqual(p["_delta"]["sessions"]["by"], "name")
        self.assertLess(len(raw.encode()), 10_000)
        for r in (old, one):
            whole, raw_old, _ = self.sessions_patch(r)
            self.assertNotIn("_delta", whole)
            self.assertNotIn("_n", whole)
            self.assertEqual(len(whole["sessions"]), 62)
            self.assertGreater(len(raw_old.encode()), 10_000)
        # the state built from deltas is the state the old stream holds
        self.assertEqual(dumps(state["sessions"]), dumps(whole["sessions"]))
        self.j.append("session.requested", "atlas", {"cwd": "/w", "session_id": "s-atlas", "channel": "cli", "owner": True})
        self.web.tail.poll()
        p2, _, state = self.sessions_patch(new, state)
        self.assertGreater(p2["_n"], p["_n"])
        self.assertIn("atlas", [s["name"] for s in state["sessions"]])


def load_tests(loader, standard, pattern):
    """Only our own: StreamShared already runs in test_web_stream_shared."""
    suite = unittest.TestSuite()
    for case in (Delta, PageApplier, DeltaStream):
        suite.addTests(case(n) for n in case.__dict__ if n.startswith("test"))
    return suite


if __name__ == "__main__":
    unittest.main()
