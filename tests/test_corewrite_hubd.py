"""The core.write right in hubd (core/corewrite.py, spec C9 §8): a fake module process (tests/fakes/fake_module.py)
answers /hook/fold and /hook/actions; before writing, hubd calls it and puts the decision in the same journal line;
on a failure it folds the event itself and writes module.error right after; the budget is per tick pass. In every case
the journal folded by a reader (views.Reader) gives the same state as hubd's."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import os
import time
import unittest
from unittest import mock

from core import corewrite, daemon, journal, modrun, modules, reducer, spool, views
from tests.fakes.modstand import Stand

OWNER = {"channel": "cli", "owner": True}


class Host:
    """A host without processes: remembers actions, answers "sent"."""
    kind, screen_dialogs = "stream", False

    def __init__(self):
        self.calls = []
        self.tool_flags = lambda **kw: []

    def _do(self, do, name, **kw):
        self.calls.append((do, name, kw))
        return "sent"

    def spawn(self, name, **kw):
        return self._do("spawn", name, **kw)

    def type(self, name, **kw):
        return self._do("type", name, **kw)

    def respond(self, name, **kw):
        return self._do("respond", name, **kw)

    def release(self, name, **kw):
        return self._do("release", name, **kw)

    def kill(self, name, **kw):
        return self._do("kill", name, **kw)

    def interrupt(self, name, **kw):
        return self._do("interrupt", name, **kw)

    def end_tree(self, name, **kw):
        return self._do("end_tree", name, **kw)

    def recap(self, name, **kw):
        return self._do("recap", name, **kw)

    def compact(self, name, **kw):
        return self._do("compact", name, **kw)

    def poll(self):
        return []

    def info(self, name):
        return {"pid": 1}

    def alive(self, name):
        return True

    def done(self, do):
        return [c for c in self.calls if c[0] == do]


def stand(case, fold=("message.received",), parts=("session", "queue"), actions=False, consent=True, name="fake",
          extra=(), points=("fold", "actions")):
    """A stand with module name (core.write, fold) and session a (idle). extra — more modules {name, fold}."""
    s = Stand(case, name=name, points=list(points), caps=["core.write"],
              extra=[{"name": x["name"], "caps": ["core.write"]} for x in extra])
    for m, f, a in [(name, fold, actions)] + [(x["name"], x["fold"], False) for x in extra]:
        path = os.path.join(s.root, m, "module.json")
        with open(path) as fh:
            man = json.load(fh)
        man.update(fold=list(f), fold_state=list(parts), actions=a)
        with open(path, "w") as fh:
            json.dump(man, fh)
    if consent:
        s.cfg["modules"]["core_write"] = [name] + [x["name"] for x in extra]
    s.hub.host = Host()
    s.hub.modules.reload(s.cfg, {})
    s.hub.record("session.requested", "a", {"cwd": s.state_dir, "session_id": "s-a", **OWNER})
    s.hub.record("session.started", "a", {})
    for m in [name] + [x["name"] for x in extra]:
        if m in s.hub.modules.sync():
            s.launch(m)
            s.until_ready(m)
    s.hub.corewrite.new_pass()
    return s


def fold(s, answer=None, do="ok", **more):
    s.update(fold={"do": do, "answer": answer or {}, **more})


def nxt(s):
    return s.hub.journal.seq + 1


def lines(s):
    with open(os.path.join(s.state_dir, "journal.jsonl"), encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


def same_as_reader(case, s):
    r = views.Reader()
    r.feed_all(lines(s))
    clean = lambda st: {k: v for k, v in st.items() if not k.startswith("_")}  # noqa: E731
    case.assertEqual(clean(r.state), clean(s.hub.state))
    return r


def say(s, text="hello", to="a", **extra):
    return s.hub.record("message.received", None, {"to": to, "text": text, **OWNER, **extra})


def reset(s):
    """Clear the module's failure count as the owner would (hub modules reset), so a series of refusals does not suspend it."""
    s.hub.record("module.reset", "fake", {"module": "fake", **OWNER})
    s.hub.corewrite.calls.local.clear()


def line(s, ev):
    return next(x for x in lines(s) if x["seq"] == ev["seq"])


def turn(s, mid):
    """Session a took message mid and finished the turn — idle again."""
    s.hub.record("turn.started", "a", {"marker": mid})
    s.hub.record("turn.result", "a", {"result": "ok", "num_turns": 1})


class Verdicts(unittest.TestCase):
    def test_core_is_written_in_the_line_and_changes_nothing(self):
        # case 2
        s = stand(self)
        fold(s, {"mode": "core"})
        ev = say(s)
        line = lines(s)[-1] if lines(s)[-1]["seq"] == ev["seq"] else next(x for x in lines(s) if x["seq"] == ev["seq"])
        self.assertEqual(line["fold"], {"mod": "fake", "mode": "core"})
        self.assertNotIn("core_written", s.hub.state)
        self.assertFalse(os.path.exists(os.path.join(s.state_dir, modules.CORE_WRITTEN)))
        call = s.calls("fold")[0]["body"]
        self.assertEqual(call["event"]["seq"], ev["seq"])            # the module knows the event number
        self.assertEqual(call["part"]["session"]["name"], "a")       # the addressee by data.to
        self.assertEqual(call["part"]["queue"], [])
        self.assertEqual(s.hub.host.done("type")[0][2]["message"], f"m{ev['seq']}")
        same_as_reader(self, s)

    def test_after_makes_a_message_urgent(self):
        # case 3
        s = stand(self)
        s.hub.record("turn.started", "a", {})
        say(s, "first")
        fold(s, {"mode": "after", "patch": [{"op": "set", "path": ["messages", f"m{nxt(s)}", "urgent"],
                                             "value": True}]})
        ev = say(s, "urgent")
        self.assertTrue(s.hub.state["messages"][f"m{ev['seq']}"]["urgent"])
        self.assertEqual(line(s, ev)["fold"]["mode"], "after")
        with open(os.path.join(s.state_dir, modules.CORE_WRITTEN)) as f:
            self.assertEqual(json.load(f)["modules"]["fake"]["first_seq"], ev["seq"])
        self.assertIsNone(modules.core_write_refusal(s.state_dir, s.hub.state))   # this code understands it
        same_as_reader(self, s)

    def test_replace_with_its_own_message_is_accepted_for_the_cli(self):
        # case 4: the CLI sees "accepted", the feed goes by the message in the state
        s = stand(self)
        mid = f"m{nxt(s)}"
        msg = {"id": mid, "to": "a", "session": "a", "text": "rewritten by a module", "origin": "", "state": "queued",
               "attempt": 0, "deadline": None, "created": 1.0, "since": 1.0}
        fold(s, {"mode": "replace", "patch": [{"op": "set", "path": ["messages", mid], "value": msg}]})
        say(s, "original", req="r1")
        self.assertEqual(journal.decided(os.path.join(s.state_dir, "journal.jsonl"), "r1", 1), (int(mid[1:]), None))
        self.assertIn("rewritten by a module", s.hub.host.done("type")[-1][2]["text"])
        r = same_as_reader(self, s)
        self.assertEqual([it["text"] for it in r.timelines["a"] if it.get("id") == mid], ["rewritten by a module"])

    def test_after_on_an_event_the_core_rejects(self):
        # case 5: E + core.rejected, edits not written, no module error
        s = stand(self)
        fold(s, {"mode": "after", "patch": [{"op": "set", "path": ["owner", "x"], "value": 1}]})
        ev = s.hub.record("message.received", None, {"to": "ghost", "text": "x", "channel": "session", "sender": "a"})
        got = [x for x in lines(s) if x["seq"] >= ev["seq"]]
        self.assertEqual([x["type"] for x in got], ["message.received", "core.rejected"])
        self.assertNotIn("fold", got[0])
        self.assertNotIn("x", s.hub.state["owner"])
        self.assertEqual(s.events("module.error"), [])

    def test_a_patch_that_does_not_land_is_a_failure(self):
        # case 6: strict check before the write
        s = stand(self)
        fold(s, {"mode": "replace", "patch": [{"op": "set", "path": ["sessions", "ghost", "x"], "value": 1}]})
        ev = say(s)
        got = [x for x in lines(s) if x["seq"] >= ev["seq"]]
        self.assertNotIn("fold", got[0])
        self.assertIn(f"m{ev['seq']}", s.hub.state["messages"])   # the core's handling
        err = s.events("module.error")[-1]
        self.assertEqual((err["data"]["kind"], err["seq"]), ("check", ev["seq"] + 1))   # on the next line
        self.assertIn("did not apply", err["data"]["error"])
        same_as_reader(self, s)

    def test_a_state_outside_the_machine_is_a_failure(self):
        # case 7
        s = stand(self)
        for bad in ([{"op": "set", "path": ["sessions", "a", "state"], "value": "flying"}],
                    [{"op": "del", "path": ["sessions", "a", "pending"]}],
                    [{"op": "set", "path": ["sessions", "a", "name"], "value": "b"}],
                    [{"op": "set", "path": ["messages", "m1"], "value": {"state": "queued"}}]):
            reset(s)
            s.hub.corewrite.new_pass()
            fold(s, {"mode": "after", "patch": bad})
            ev = say(s)
            self.assertNotIn("fold", line(s, ev), bad)
            # an entity check or a core fold that failed on a corrupt state — a module failure plus the core's handling
            self.assertIn(s.events("module.error")[-1]["data"]["kind"], ("check", "rejected"), bad)
            self.assertIn(f"m{ev['seq']}", s.hub.state["messages"], bad)
        self.assertEqual(s.hub.state["sessions"]["a"]["name"], "a")
        same_as_reader(self, s)

    def test_patches_pass_but_the_core_tail_crashes(self):
        # case 8: a fold with edits — "core failure" → a second fold without them, module.error
        s = stand(self, fold=("session.started",))
        s.hub.record("session.exited", "a", {})
        s.hub.record("session.requested", "a", {"cwd": s.state_dir, "session_id": "s-a", **OWNER})
        ev = say(s)   # the session is coming up: the message is queued
        fold(s, {"mode": "after", "patch": [{"op": "del", "path": ["messages", f"m{ev['seq']}", "created"]}]})
        st = s.hub.record("session.started", "a", {})
        self.assertNotIn("fold", lines(s)[lines(s).index(next(x for x in lines(s) if x["seq"] == st["seq"]))])
        err = s.events("module.error")[-1]["data"]
        self.assertEqual(err["kind"], "rejected")
        self.assertIn("core failure", err["error"])
        self.assertEqual(s.hub.state["messages"][f"m{ev['seq']}"]["state"], "typing")
        same_as_reader(self, s)

    def test_reject_from_the_cli(self):
        # case 31
        s = stand(self)
        fold(s, {"mode": "reject", "reason": "not now"})
        ev = say(s, req="r2")
        got = [x for x in lines(s) if x["seq"] >= ev["seq"]]
        self.assertEqual(got[0]["fold"], {"mod": "fake", "mode": "reject", "reason": "not now"})
        self.assertEqual(got[1]["data"]["reason"], "module fake: not now")
        self.assertEqual(got[1]["data"]["mod"], "fake")
        self.assertEqual(journal.decided(os.path.join(s.state_dir, "journal.jsonl"), "r2", 1),
                         (ev["seq"], "module fake: not now"))
        self.assertNotIn(f"m{ev['seq']}", s.hub.state["messages"])
        self.assertEqual(s.hub.state["core_written"]["modules"]["fake"]["first"], ev["seq"] + 1)
        same_as_reader(self, s)

    def test_replace_with_an_empty_patch_marks_the_module(self):
        # case 32
        s = stand(self)
        fold(s, {"mode": "replace"})
        ev = say(s)
        self.assertNotIn(f"m{ev['seq']}", s.hub.state["messages"])
        self.assertEqual(s.hub.state["core_written"]["modules"]["fake"]["count"], 1)
        self.assertTrue(os.path.exists(os.path.join(s.state_dir, modules.CORE_WRITTEN)))
        same_as_reader(self, s)


class Failures(unittest.TestCase):
    def test_silence_garbage_size_and_three_failures(self):
        # case 9: silent — the core plus module.error; the third within 5 min — suspension, then skip
        s = stand(self)
        for do in ("silent", "garbage", "big"):
            s.hub.corewrite.new_pass()
            fold(s, do=do)
            began = time.monotonic()
            ev = say(s)
            self.assertLess(time.monotonic() - began, corewrite.CALL + 0.8, do)
            self.assertNotIn("fold", next(x for x in lines(s) if x["seq"] == ev["seq"]), do)
            self.assertIn(f"m{ev['seq']}", s.hub.state["messages"], do)
        self.assertEqual([e["data"]["kind"] for e in s.events("module.error")], ["timeout", "schema", "size"])
        self.assertEqual(s.rec()["state"], "suspended")
        s.hub.corewrite.new_pass()
        fold(s, {"mode": "replace"})
        ev = say(s)
        self.assertEqual(lines(s)[-1]["fold"] if lines(s)[-1]["seq"] == ev["seq"] else None,
                         {"mod": "fake", "skipped": "suspended"})
        self.assertEqual(len(s.calls("fold")), 3)
        same_as_reader(self, s)

    def test_the_module_process_died(self):
        s = stand(self)
        fold(s, do="exit")
        ev = say(s)
        self.assertIn(f"m{ev['seq']}", s.hub.state["messages"])
        self.assertEqual(s.events("module.error")[-1]["data"]["kind"], "closed")

    def test_actions_hook_fails_and_the_patches_stay(self):
        # case 22
        s = stand(self, actions=True)
        mid = f"m{nxt(s)}"
        fold(s, {"mode": "after", "patch": [{"op": "set", "path": ["messages", mid, "urgent"], "value": True}]})
        s.update(actions={"do": "garbage"})
        ev = say(s)
        self.assertTrue(s.hub.state["messages"][mid]["urgent"])
        self.assertEqual(line(s, ev)["fold"], {"mod": "fake", "mode": "after", "patch": [
            {"op": "set", "path": ["messages", mid, "urgent"], "value": True}]})
        self.assertEqual(s.events("module.error")[-1]["data"]["hook"], "actions")
        self.assertEqual(len(s.hub.host.done("type")), 1)    # the core's actions as they are
        same_as_reader(self, s)

    def test_answers_of_any_shape_do_not_crash_hubd(self):
        # review: a list instead of a string in an edit, "²" in the number, do not a string — a module failure, not hubd
        s = stand(self, actions=True)
        for answer, actions in (({"mode": "after", "patch": [{"op": "set", "path": ["sessions", "a", "state"],
                                                              "value": ["busy"]}]}, {}),
                                ({"mode": "core"}, {"replace": {"²": {"text": "x"}}}),
                                ({"mode": "core"}, {"add": [{"do": ["x"], "session": "a"}]})):
            reset(s)
            s.hub.corewrite.new_pass()
            fold(s, answer)
            s.update(actions={"answer": actions})
            ev = say(s)
            turn(s, f"m{ev['seq']}")
            self.assertEqual(s.hub.state["sessions"]["a"]["state"], "idle", answer)
            self.assertIn(s.events("module.error")[-1]["data"]["kind"], ("check", "schema"), (answer, actions))
        same_as_reader(self, s)

    def test_a_hung_actions_hook_is_a_failure(self):
        # review: a /hook/actions timeout after a fast fold — a module failure, not a silent skip
        s = stand(self, actions=True)
        fold(s, {"mode": "core"})
        s.update(actions={"do": "silent"})
        say(s)
        err = s.events("module.error")[-1]["data"]
        self.assertEqual((err["hook"], err["kind"]), ("actions", "timeout"))
        self.assertEqual(len(s.hub.host.done("type")), 1)

    def test_no_call_without_budget_left(self):
        s = stand(self)
        s.hub.corewrite.spent["fake"] = corewrite.PASS - 0.002
        ev = say(s)
        self.assertEqual(line(s, ev)["fold"], {"mod": "fake", "skipped": "budget"})
        self.assertEqual(s.calls("fold"), [])

    def test_a_spool_item_without_a_string_type(self):
        s = stand(self, fold=("timer.*",))
        spool.put(os.path.join(s.state_dir, "spool"), 5, None, {})
        s.hub.ingest()
        self.assertEqual(lines(s)[-1]["type"], "core.rejected")
        Facts.put(Facts(), s, 5, "a", {"text": "x"})
        self.assertIn("type is a string", lines(s)[-1]["data"]["reason"])

    def test_the_hub_stops_mid_pass(self):
        # case 23
        s = stand(self)
        s.hub.stopping.append(True)
        ev = say(s)
        self.assertEqual(next(x for x in lines(s) if x["seq"] == ev["seq"])["fold"],
                         {"mod": "fake", "skipped": "stopping"})
        self.assertEqual(s.calls("fold"), [])

    def test_a_module_that_is_not_ready_is_skipped(self):
        s = stand(self)
        s.hub.state["modules"]["fake"]["ready"] = "another-run"
        ev = say(s)
        self.assertEqual(next(x for x in lines(s) if x["seq"] == ev["seq"])["fold"],
                         {"mod": "fake", "skipped": "not_ready"})

    def test_a_part_too_big_is_skipped(self):
        s = stand(self, parts=("session",))
        s.hub.state["sessions"]["a"]["about"] = "x" * (corewrite.MAX_PART + 1)
        ev = say(s)
        self.assertEqual(next(x for x in lines(s) if x["seq"] == ev["seq"])["fold"],
                         {"mod": "fake", "skipped": "part"})


class Budget(unittest.TestCase):
    def test_a_burst_with_a_slow_module(self):
        # case 10: a 100 ms module — about a second with the module per pass, the rest — budget skip
        s = stand(self)
        fold(s, {"mode": "core"}, do="slow", delay=0.1)
        for i in range(15):
            say(s, f"x{i}")
        folds = [x["fold"] for x in lines(s) if x["type"] == "message.received" and "fold" in x]
        asked = [f for f in folds if f.get("mode") == "core"]
        skipped = [f for f in folds if f.get("skipped") == "budget"]
        # the budget is time, not a number of calls: on a busy machine (a background macOS account) a 100 ms call takes 200 ms —
        # the module is asked less often, but within the same pass (2026-10-18: 4–5 calls instead of ≥6 on Mac)
        self.assertGreaterEqual(s.hub.corewrite.spent["fake"], corewrite.PASS / 2)
        self.assertGreaterEqual(len(asked), 2)
        self.assertLessEqual(len(asked), 11)
        self.assertEqual(len(asked) + len(skipped), 15)   # a call cut by the rest of the pass is also a skip
        self.assertEqual(s.events("module.error"), [])
        s.hub.corewrite.new_pass()                 # a new pass — with the module again
        say(s, "after")
        self.assertEqual(lines(s)[-1]["fold"].get("mode") if "fold" in lines(s)[-1] else
                         [x for x in lines(s) if x["type"] == "message.received"][-1]["fold"]["mode"], "core")

    def test_a_call_gets_no_more_than_what_is_left_of_the_pass(self):
        s = stand(self)
        s.hub.corewrite.spent["fake"] = corewrite.PASS - 0.2
        fold(s, do="silent")
        began = time.monotonic()
        ev = say(s)
        self.assertLess(time.monotonic() - began, 0.6)
        # the module did not use up its own time — the pass budget cut it: a skip, not a failure
        self.assertEqual(line(s, ev)["fold"], {"mod": "fake", "skipped": "budget"})
        self.assertEqual(s.events("module.error"), [])

    def test_the_share_of_a_minute(self):
        s = stand(self)
        now = [1000.0]
        s.hub.corewrite.mono = lambda: now[0]
        for _ in range(7):   # more than 6 s per minute
            s.hub.corewrite.spend("fake", now[0] - 1.0)
        self.assertEqual(s.hub.corewrite.pending[-1][2]["kind"], "slow")
        s.hub.corewrite.flush()
        s.hub.corewrite.new_pass()
        ev = say(s)
        self.assertEqual(next(x for x in lines(s) if x["seq"] == ev["seq"])["fold"], {"mod": "fake", "skipped": "budget"})
        now[0] += corewrite.WINDOW + 1
        fold(s, {"mode": "core"})
        ev = say(s)
        self.assertEqual(next(x for x in lines(s) if x["seq"] == ev["seq"])["fold"], {"mod": "fake", "mode": "core"})


def put(s, kind, entity=None, data=None, module="fake", **extra):
    """A fact of the module in the inbox, taken by hubd."""
    spool.put(os.path.join(s.state_dir, "spool"), kind, entity, data, src="module")
    name = sorted(n for n in os.listdir(os.path.join(s.state_dir, "spool")) if n.endswith(".json"))[-1]
    path = os.path.join(s.state_dir, "spool", name)
    with open(path, encoding="utf-8") as f:
        item = json.load(f)
    if module:
        item["module"] = module
    item.update(extra)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(item, f)
    s.hub.ingest()


class Facts(unittest.TestCase):
    def put(self, *a, **kw):
        put(*a, **kw)

    def test_a_fact_of_a_core_write_module_is_marked(self):
        s = stand(self, fold=("timer.*",))
        self.put(s, "session.said", "a", {"text": "from a module"}, by="forged", fold={"mod": "x"})
        got = lines(s)[-1]
        self.assertEqual((got["by"], got["src"], got["type"]), ("fake", "mod:fake", "session.said"))
        self.assertNotIn("fold", got)                         # hubd does not take fields from the file
        self.assertTrue(os.path.exists(os.path.join(s.state_dir, modules.CORE_WRITTEN)))
        self.assertEqual(s.hub.state["core_written"]["modules"]["fake"]["first"], got["seq"])
        r = same_as_reader(self, s)
        self.assertEqual(r.timelines["a"][-1]["mod"], "fake")

    def test_the_journal_mechanics_are_refused(self):
        # case 17
        s = stand(self, fold=("timer.*",))
        for kind in ("core.snapshot", "core.rejected", "module.reset", "mod.other.x"):
            self.put(s, kind, None, {"state": {}, "of": 1, "module": "fake"})
            got = lines(s)[-2:]
            # core.* does not go to the journal even as rejected (daemon.spooled): a spool.refused stub instead
            self.assertEqual([x["type"] for x in got],
                             ["spool.refused" if kind.startswith("core.") else kind, "core.rejected"], kind)
            self.assertIn("only the core writes" if kind.startswith("core.") else "may not write", got[1]["data"]["reason"])
        self.put(s, "mod.fake.seen", None, {"n": 1})
        self.assertEqual(lines(s)[-1]["type"], "mod.fake.seen")
        # the module's own state (mod.<name>.*) is not a write to the core state: no by and no core_written mark
        self.assertNotIn("by", lines(s)[-1])
        self.put(s, "mod.fake.set", None, {"key": "k", "value": 1})
        self.assertNotIn("by", lines(s)[-1])
        self.assertEqual(s.hub.state["mods"]["fake"]["k"]["value"], 1)
        same_as_reader(self, s)

    def test_without_core_write_only_its_own_facts(self):
        s = Stand(self, points=[], caps=[])
        s.hub.host = Host()
        s.launch()
        s.until_ready()
        self.put(s, "session.said", "a", {"text": "x"})
        self.assertEqual(lines(s)[-1]["data"]["reason"], "module fake without core.write may not write: session.said")
        self.put(s, "mod.fake.seen", None, {})
        self.assertEqual((lines(s)[-1]["type"], lines(s)[-1].get("by")), ("mod.fake.seen", None))

    def test_a_module_event_goes_to_the_interceptor_of_another_module_only(self):
        # case 18
        s = stand(self, fold=("session.said",), extra=[{"name": "other", "fold": ["message.received"]}])
        fold(s, {"mode": "core"})
        self.put(s, "session.said", "a", {"text": "own"})
        self.assertEqual(s.calls("fold"), [])                  # the module does not get its own event
        self.put(s, "session.said", "a", {"text": "foreign"}, module="other")
        self.assertEqual(len(s.calls("fold")), 1)
        self.assertEqual(lines(s)[-1]["by"], "other")
        self.assertEqual(lines(s)[-1]["fold"], {"mod": "fake", "mode": "core"})

    def test_auto_allow_from_a_module_is_a_question(self):
        # case 29
        s = stand(self, fold=("timer.*",))
        self.put(s, "permission.requested", "a", {"auto": "allow", "request_id": "r1", "tool": "Bash",
                                                  "input": {"command": "ls"}})
        self.assertEqual(s.hub.host.done("respond"), [])
        self.assertEqual(len(s.hub.state["sessions"]["a"]["pending"]), 1)

    def test_a_process_posing_as_the_module_gets_its_mark(self):
        # case 30: the label is a contract and visibility, not a boundary (risk 1)
        s = stand(self, fold=("timer.*",))
        self.put(s, "session.said", "a", {"text": "not a module"}, module="fake")
        self.assertEqual(lines(s)[-1]["by"], "fake")


class Actions(unittest.TestCase):
    def test_the_text_of_type_is_replaced_and_the_marker_kept(self):
        s = stand(self, actions=True)
        fold(s, {"mode": "core"})
        mid = f"m{nxt(s)}"
        s.update(actions={"answer": {"replace": {"0": {"text": f"[hub:{mid}] swapped text"}}}})
        say(s)
        self.assertEqual(s.hub.host.done("type")[-1][2]["text"], f"[hub:{mid}] swapped text")
        got = s.calls("actions")[0]["body"]
        self.assertEqual(got["actions"][0]["do"], "type")
        self.assertIn(f"[hub:{mid} ", got["actions"][0]["text"])
        self.assertEqual(line(s, {"seq": int(mid[1:])})["fold"]["actions"],
                         {"replace": {"0": {"text": f"[hub:{mid}] swapped text"}}})
        self.assertEqual(s.hub.state["core_written"]["modules"]["fake"]["count"], 1)   # editing actions is an intervention
        same_as_reader(self, s)
        # confirmation is by the label; if it did not come — a retry by timeout with the core's text
        s.update(actions={"answer": {}})
        s.hub.record("timer.fired", mid, {"attempt": 1, "n": 1})
        self.assertNotIn("swapped", s.hub.host.done("type")[-1][2]["text"])

    def test_a_dropped_type_keeps_the_wait(self):
        # a removed type does not lift the wait: the retry by timeout prints the message again
        s = stand(self, actions=True)
        mid = f"m{nxt(s)}"
        fold(s, {"mode": "core"})
        s.update(actions={"answer": {"drop": [0], "add": [{"do": "interrupt", "session": "a"}]}})
        say(s)
        self.assertEqual(s.hub.host.done("type"), [])
        self.assertEqual(len(s.hub.host.done("interrupt")), 1)
        self.assertEqual(s.hub.state["messages"][mid]["state"], "typing")
        self.assertEqual([t[1] for t in reducer.pending_timers(s.hub.state)], [mid])
        same_as_reader(self, s)
        s.update(actions={"answer": {}})
        s.hub.record("timer.fired", mid, {"attempt": 1, "n": 1})
        self.assertEqual(len(s.hub.host.done("type")), 1)

    def test_taking_the_delivery_is_a_patch_of_the_queue(self):
        # whoever takes delivery over removes the message from the queue by an edit — the core neither prints nor waits for it
        s = stand(self, actions=True)
        mid = f"m{nxt(s)}"
        fold(s, {"mode": "after", "patch": [{"op": "set", "path": ["messages", mid, "state"], "value": "delivered"}]})
        s.update(actions={"answer": {"add": [{"do": "type", "session": "a", "text": "own typing"}]}})
        say(s)
        self.assertEqual([c[2]["text"] for c in s.hub.host.done("type")], ["own typing"])
        self.assertEqual(s.hub.state["messages"][mid]["state"], "delivered")
        self.assertEqual(reducer.pending_timers(s.hub.state), [])
        same_as_reader(self, s)

    def test_refused_edits_leave_the_cores_actions(self):
        # cases 20, 21
        s = stand(self, actions=True)
        for edit in ({"add": [{"do": "spawn", "session": "a"}]}, {"replace": {"0": {"session": "b"}}},
                     {"drop": [5]}):
            reset(s)
            before = len(s.hub.host.done("type"))
            fold(s, {"mode": "core"})
            s.update(actions={"answer": edit})
            ev = say(s)
            self.assertEqual(len(s.hub.host.done("type")), before + 1, edit)
            turn(s, f"m{ev['seq']}")
            self.assertEqual(s.events("module.error")[-1]["data"]["hook"], "actions", edit)
            self.assertNotIn("actions", [x for x in lines(s) if x["type"] == "message.received"][-1]["fold"])

    def test_restart_by_a_dirty_mark(self):
        # case 20: a restart — by a dirty edit (_pump → release + spawn)
        s = stand(self, fold=("session.info",))
        fold(s, {"mode": "after", "patch": [{"op": "set", "path": ["sessions", "a", "dirty"], "value": True}]})
        s.hub.record("session.info", "a", {})
        self.assertEqual([c[0] for c in s.hub.host.calls[-2:]], ["release", "spawn"])

    def test_drop_task_note(self):
        s = stand(self, actions=True)
        acts = [{"do": "task_note", "session": None, "delegation": "d1", "who": "a", "text": "t"}]
        self.assertEqual(corewrite.edit_actions(acts, {"drop": [0]}, s.hub.state), [])

    def test_three_messages_are_not_glued(self):
        # case 19: _pump prints one message to a session per event — an edit cannot merge them
        s = stand(self, fold=("session.started",), actions=True)
        s.hub.record("session.exited", "a", {})
        s.hub.record("session.requested", "a", {"cwd": s.state_dir, "session_id": "s-a", **OWNER})
        for i in range(3):
            say(s, f"word {i}")
        fold(s, {"mode": "core"})
        s.update(actions={"answer": {}})
        s.hub.record("session.started", "a", {})
        self.assertEqual([a["do"] for a in s.calls("actions")[0]["body"]["actions"]], ["type"])


class Copies(unittest.TestCase):
    """An intercepted event costs no deep copy of the whole state and does not knock the fold's spare off (Hub.fold): on a big
    hub each such copy is tens of ms per event (11-08: +190 ms per message with a module that intercepts every message)."""

    def count(self, s, *steps):
        """Run steps and one ordinary event after them; -> how many times the live or the spare state was copied whole."""
        n = [0]
        real = reducer.copy_state

        def counted(o):
            if o is s.hub.state or o is s.hub.spare:
                n[0] += 1
            return real(o)
        with mock.patch.object(reducer, "copy_state", counted):
            for step in steps:
                step()
                if s.hub.spare_of is s.hub.state:   # level right after the step too, not only after the next event
                    self.assertEqual(s.hub.spare, s.hub.state)
            s.hub.record("session.said", "a", {"text": "after"})   # the spare is re-copied on the next event, if at all
        self.assertIs(s.hub.spare_of, s.hub.state)
        self.assertEqual(s.hub.spare, s.hub.state)
        same_as_reader(self, s)
        return n[0]

    def ready(self, **kw):
        s = stand(self, **kw)
        s.hub.record("session.said", "a", {"text": "warm"})   # the spare runs level with the live state
        return s

    def test_verdicts_copy_nothing(self):
        for name, answer in (("core", {"mode": "core"}),
                             ("after", {"mode": "after", "patch": [{"op": "set", "path": ["owner", "x"], "value": 1}]}),
                             ("replace", {"mode": "replace"}),
                             ("reject", {"mode": "reject", "reason": "not now"})):
            with self.subTest(name):
                s = self.ready()
                fold(s, answer)
                self.assertEqual(self.count(s, lambda: say(s)), 0)
                self.assertEqual(s.hub.state["owner"].get("x"), 1 if name == "after" else None)

    def test_an_actions_edit_copies_nothing(self):
        s = self.ready(actions=True)
        fold(s, {"mode": "core"})
        mid = f"m{nxt(s)}"
        s.update(actions={"answer": {"replace": {"0": {"text": f"[hub:{mid}] swapped"}}}})
        self.assertEqual(self.count(s, lambda: say(s)), 0)
        self.assertEqual(s.hub.state["core_written"]["modules"]["fake"]["count"], 1)

    def test_inbox_facts_copy_nothing(self):
        s = self.ready(fold=("timer.*",))
        self.assertEqual(self.count(s, lambda: put(s, "session.said", "a", {"text": "by a module"}),
                                    lambda: put(s, "mod.other.x", None, {"n": 1})), 0)
        self.assertEqual([x["type"] for x in lines(s)[-3:-1]], ["mod.other.x", "core.rejected"])
        self.assertEqual(s.hub.state["core_written"]["modules"]["fake"]["count"], 1)

    def test_a_failed_check_falls_back_to_the_cores_fold(self):
        # the trial fold with edits is undone: the state is the core's own, and the spare is level again
        s = self.ready()
        fold(s, {"mode": "after", "patch": [{"op": "set", "path": ["sessions", "a", "state"], "value": "flying"}]})
        ev = []
        self.assertEqual(self.count(s, lambda: ev.append(say(s))), 1)   # the spare is copied anew once, as before
        self.assertNotEqual(s.hub.state["sessions"]["a"]["state"], "flying")
        self.assertIn(f"m{ev[0]['seq']}", s.hub.state["messages"])
        self.assertNotIn("fold", line(s, ev[0]))
        self.assertEqual(s.events("module.error")[-1]["data"]["kind"], "check")


    def test_an_event_the_core_rejects_costs_no_more_than_before(self):
        # the trial fold and the core's own fold are both rejected: the spare is copied anew for the core's fold and for
        # the rejection, as for a rejected event without modules plus one (the old code: three copies)
        s = self.ready()
        fold(s, {"mode": "after", "patch": [{"op": "set", "path": ["owner", "x"], "value": 1}]})
        got = []
        n = self.count(s, lambda: got.append(s.hub.record("message.received", None, {"to": "ghost", "text": "x",
                                                                                     "channel": "session", "sender": "a"})))
        self.assertEqual(n, 2)
        self.assertEqual([x["type"] for x in lines(s) if x["seq"] >= got[0]["seq"]][:2], ["message.received", "core.rejected"])
        self.assertNotIn("x", s.hub.state["owner"])


class Lifecycle(unittest.TestCase):
    def test_disabled_module_leaves_the_state(self):
        # case 25
        s = stand(self)
        mid = f"m{nxt(s)}"
        fold(s, {"mode": "after", "patch": [{"op": "set", "path": ["messages", mid, "urgent"], "value": True}]})
        say(s)
        s.cfg["modules"]["enabled"] = []
        s.hub.modules.reload(s.cfg, {})
        s.hub.modules.sync()
        ev = say(s, "after")
        self.assertNotIn("fold", lines(s)[-1] if lines(s)[-1]["seq"] == ev["seq"] else
                         next(x for x in lines(s) if x["seq"] == ev["seq"]))
        self.assertTrue(s.hub.state["messages"][mid]["urgent"])
        self.assertIn("fake", s.hub.state["core_written"]["modules"])

    def test_crash_after_the_answer_before_the_write(self):
        # case 11: the box fact is a file; on startup the module is asked again
        s = stand(self, fold=("session.said",))
        fold(s, {"mode": "core"})
        spool.put(os.path.join(s.state_dir, "spool"), "session.said", "a", {"text": "x"})
        real = s.hub.journal.write
        with mock.patch.object(s.hub.journal, "write", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                s.hub.ingest()
        self.assertEqual(len(s.calls("fold")), 1)
        s.hub.journal.write = real
        hub = daemon.Hub(s.state_dir, Host(), registry_dir=s.state_dir)
        hub.modules.reload(s.cfg, {})
        hub.modules.sync()
        hub.corewrite.new_pass()
        hub.ingest()
        self.assertEqual(len(s.calls("fold")), 2)
        got = [x for x in lines(s) if x["type"] == "session.said"]
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["fold"], {"mod": "fake", "mode": "core"})

    def test_a_torn_error_line_leaves_the_event(self):
        # case 12
        s = stand(self)
        fold(s, do="garbage")
        ev = say(s)
        path = os.path.join(s.state_dir, "journal.jsonl")
        with open(path, "rb") as f:
            raw = f.read()
        cut = raw.rstrip(b"\n").rfind(b"\n")
        with open(path, "wb") as f:
            f.write(raw[:cut + 1] + raw[cut + 1:cut + 20])
        j = journal.Journal(path)
        self.assertEqual(j.seq, lines(s)[-1]["seq"])
        self.assertIn(ev["seq"], [x["seq"] for x in lines(s)])

    def test_periodic_snapshot_holds_the_patched_state(self):
        # case 16
        s = stand(self)
        mid = f"m{nxt(s)}"
        fold(s, {"mode": "after", "patch": [{"op": "set", "path": ["messages", mid, "urgent"], "value": True}]})
        say(s)
        s.hub.snapshot("periodic")
        snap = [x for x in lines(s) if x["type"] == "core.snapshot"][-1]
        self.assertTrue(snap["data"]["state"]["messages"][mid]["urgent"])
        self.assertIn("core_written", snap["data"]["state"])

    def test_hub_modules_warns_and_touched_lists_what_it_changed(self):
        import subprocess
        import sys
        s = stand(self)
        mid = f"m{nxt(s)}"
        fold(s, {"mode": "after", "patch": [{"op": "set", "path": ["messages", mid, "urgent"], "value": True}]})
        say(s)
        s.hub.save_state()
        cfg = os.path.join(s.state_dir, "hub.json")
        with open(cfg, "w") as f:
            json.dump(s.cfg, f)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = {k: v for k, v in os.environ.items() if k not in ("HUB_SESSION", "HUB_EXTENSION")}
        env.update(HUB_STATE_DIR=s.state_dir, HUB_CONFIG=cfg, HUB_CLI_WAIT="0")

        def hub(*args):
            return subprocess.run([sys.executable, os.path.join(root, "bin", "hub"), *args], env=env,
                                  capture_output=True, text=True, encoding="utf-8", timeout=60)
        out = hub("modules").stdout
        self.assertIn("writes to the core state: fake", out)
        self.assertIn("1 record", out)
        self.assertEqual(json.loads(hub("modules", "--json").stdout)[0]["core_written"]["count"], 1)
        out = hub("modules", "touched", "fake").stdout
        self.assertIn(f"messages.{mid}", out)
        hub("modules", "forget", "fake")
        self.assertEqual(spool.pending(os.path.join(s.state_dir, "spool")), 1)
        s.hub.ingest()
        self.assertTrue(s.hub.state["core_written"]["modules"]["fake"]["forgot"])
        self.assertNotIn("core.rejected", [x["type"] for x in lines(s)[-1:]])

    def test_module_facts_left_while_hubd_was_down_are_taken_at_start(self):
        # review: on startup the modules come before the box (start: modules.sync, then ingest)
        s = stand(self, fold=("timer.*",))
        cfg = os.path.join(s.state_dir, "hub.json")
        with open(cfg, "w") as f:
            json.dump(s.cfg, f)
        path = spool.put(os.path.join(s.state_dir, "spool"), "session.said", "a", {"text": "before the respawn"})
        p = os.path.join(s.state_dir, "spool", path + ".json")
        with open(p, encoding="utf-8") as f:
            item = json.load(f)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({**item, "module": "fake"}, f)
        with mock.patch.dict(os.environ, {"HUB_CONFIG": cfg}):
            hub = daemon.Hub(s.state_dir, Host(), registry_dir=s.state_dir)
            hub.start()
        got = [x for x in lines(s) if x["type"] == "session.said" and x["data"].get("text") == "before the respawn"]
        self.assertEqual([x.get("by") for x in got], ["fake"])

    def test_restart_of_hubd_replays_the_same_state(self):
        s = stand(self)
        mid = f"m{nxt(s)}"
        fold(s, {"mode": "after", "patch": [{"op": "set", "path": ["messages", mid, "urgent"], "value": True}]})
        say(s)
        s.hub.save_state()
        os.unlink(os.path.join(s.state_dir, "state.json"))
        st = journal.load_state(s.state_dir)
        self.assertEqual({k: v for k, v in st.items() if not k.startswith("_")},
                         {k: v for k, v in s.hub.state.items() if not k.startswith("_")})


if __name__ == "__main__":
    unittest.main()
