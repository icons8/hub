"""The core.write right, the pure part (core/corewrite.py, spec S9): manifest fields and the graph, module answers by
schema, folding a record with the module's decision (fold) and with the by mark — one code for hubd and for all journal
readers; editing actions; the core_written mark; rotation. Spec §8 cases with a module process — tests/test_corewrite_hubd.py."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import os
import tempfile
import unittest
from unittest import mock

from core import corewrite, flow, journal, modules, reducer, views
from core.modrun import BadResult

OWNER = {"channel": "cli", "owner": True}


def manifest(**extra):
    return {"name": "m", "version": "1.0.0", "api": "^2.2", "run": ["python3", "main.py"],
            "capabilities": ["core.write"], **extra}


class Manifest(unittest.TestCase):
    def test_fold_fields_need_core_write(self):
        self.assertEqual(modules.validate(manifest(fold=["message.received", "timer.*"], fold_state=["session"],
                                                   actions=True)), [])
        errs = modules.validate({**manifest(fold=["message.received"]), "capabilities": []})
        self.assertTrue(any("core.write" in e for e in errs), errs)

    def test_the_journal_mechanics_are_not_intercepted(self):
        for bad in ("core.snapshot", "core.*", "module.error", "mod.x.y", "mod.*"):
            self.assertTrue(modules.validate(manifest(fold=[bad])), bad)
        self.assertTrue(modules.validate(manifest(fold=["*"])))
        self.assertTrue(modules.validate(manifest(fold="message.received")))
        self.assertTrue(modules.validate(manifest(fold=["message.received"], fold_state=["sessions"])))
        self.assertTrue(modules.validate(manifest(fold=["message.received"], actions="yes")))
        self.assertTrue(modules.validate(manifest(actions=True)))

    def test_overlap_of_types_and_families(self):
        self.assertTrue(corewrite.overlap("timer.*", "timer.fired"))
        self.assertTrue(corewrite.overlap("turn.*", "turn.tool.*"))
        self.assertTrue(corewrite.overlap("message.received", "message.received"))
        self.assertFalse(corewrite.overlap("turn.tool", "turn.tool_result"))
        self.assertFalse(corewrite.overlap("session.*", "sessions.x"))


class Graph(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="cw-graph-")

    def put(self, name, **extra):
        d = os.path.join(self.root, name)
        os.makedirs(d)
        with open(os.path.join(d, "module.json"), "w") as f:
            json.dump({**manifest(**extra), "name": name}, f)

    def resolve(self, **mods):
        cfg = {"modules": {"paths": [self.root], "enabled": sorted(os.listdir(self.root)), **mods}}
        return {r["name"]: r for r in modules.resolve(modules.discover(cfg, self.root, builtin=False), cfg)}

    def test_core_write_needs_the_owners_word(self):
        self.put("w", fold=["message.received"])
        got = self.resolve()
        self.assertEqual(got["w"]["state"], "blocked")
        self.assertIn("modules.core_write", got["w"]["reason"])
        self.assertEqual(self.resolve(core_write=["w"])["w"]["state"], "ok")

    def test_two_interceptors_of_one_type_are_both_blocked_until_one_is_chosen(self):
        self.put("a", fold=["timer.*"])
        self.put("b", fold=["timer.fired"])
        self.put("c", fold=["turn.result"])
        got = self.resolve(core_write=["a", "b", "c"])
        self.assertEqual((got["a"]["state"], got["b"]["state"], got["c"]["state"]), ("blocked", "blocked", "ok"))
        self.assertIn("modules.use.fold", got["a"]["reason"])
        got = self.resolve(core_write=["a", "b", "c"], use={"fold": "b"})
        self.assertEqual((got["a"]["state"], got["b"]["state"]), ("blocked", "ok"))

    def test_no_hub_command_or_page_setting_writes_the_consent(self):
        # consent is the owner's word in hub.json: the page and hub commands write only a whitelist (without modules)
        from modules.web import settings
        self.assertFalse([k for k, row in settings.KEYS.items() if str(row[0]).startswith("modules")])


class Answers(unittest.TestCase):
    def check(self, res, seq=10):
        return corewrite.fold_answer(res, {"name": "m", "seq": seq})

    def test_modes_and_their_forms(self):
        self.assertEqual(self.check({}), {"mode": "core"})
        self.assertEqual(self.check({"mode": "replace"}), {"mode": "replace", "patch": []})
        self.assertEqual(self.check({"mode": "reject", "reason": " not allowed "}), {"mode": "reject", "reason": "not allowed"})
        ok = {"mode": "after", "patch": [{"op": "set", "path": ["messages", "m10", "urgent"], "value": True},
                                         {"op": "del", "path": ["owner", "x"]}]}
        self.assertEqual(self.check(ok), ok)

    def test_out_of_schema(self):
        bad = [[], {"mode": "maybe"}, {"mode": "core", "patch": [{"op": "del", "path": ["a"]}]},
               {"mode": "reject"}, {"mode": "reject", "reason": "x" * 301},
               {"mode": "after", "extra": 1},
               {"mode": "after", "patch": [{"op": "set", "path": ["a"]}]},
               {"mode": "after", "patch": [{"op": "del", "path": ["a"], "value": 1}]},
               {"mode": "after", "patch": [{"op": "set", "path": [], "value": 1}]},
               {"mode": "after", "patch": [{"op": "set", "path": ["sessions", 1], "value": 1}]},
               {"mode": "after", "patch": [{"op": "mv", "path": ["a"]}]},
               {"mode": "after", "patch": [{"op": "set", "path": ["a"], "value": 1}] * 257}]
        for root in ("seq", "core_written", "modules", "mods", "mod_dedup", "_wake"):
            bad.append({"mode": "after", "patch": [{"op": "set", "path": [root], "value": 1}]})
        # a new message id with a number above the event's — the number of a future core message
        bad.append({"mode": "replace", "patch": [{"op": "set", "path": ["messages", "m11"], "value": {}}]})
        bad.append({"mode": "replace", "patch": [{"op": "set", "path": ["messages", "m12x1"], "value": {}}]})
        for mid in ("m12a1", "m12o", "m12x5-maps"):   # and with any tail, as a label (forms.MSG_TAIL)
            bad.append({"mode": "replace", "patch": [{"op": "set", "path": ["messages", mid], "value": {}}]})
        for b in bad:
            with self.assertRaises(BadResult, msg=b):
                self.check(b)
        self.check({"mode": "replace", "patch": [{"op": "set", "path": ["messages", "m10x1"], "value": {}}]})

    def test_action_edits_by_number(self):
        acts = [{"do": "type", "session": "a", "message": "m1", "attempt": 1, "attachments": [], "text": "[hub:m1] x"},
                {"do": "task_note", "session": None, "delegation": "d1", "who": "a", "text": "t"},
                {"do": "spawn", "session": "a", "session_id": "s", "delay": 5}]
        st = {"sessions": {"a": {}, "b": {}}, "messages": {"m1": {"session": "a"}, "m2": {"session": "b"}}}

        def edit(res):
            return corewrite.edit_actions(acts, corewrite.actions_answer(res, {}), st)

        got = edit({"drop": [1], "replace": {"0": {"text": "[hub:m1] other"}},
                    "add": [{"do": "interrupt", "session": "b"}]})
        self.assertEqual([a["do"] for a in got], ["type", "spawn", "interrupt"])
        self.assertEqual(got[0]["text"], "[hub:m1] other")
        self.assertEqual(acts[0]["text"], "[hub:m1] x")          # the core's list is untouched
        self.assertEqual(edit({"drop": [2]})[-1]["do"], "task_note")   # any action may be dropped
        refused = [{"add": [{"do": "spawn", "session": "a"}]},                     # changes state
                   {"add": [{"do": "task_note", "session": None}]},
                   {"replace": {"2": {"session_id": "x"}}},                        # spawn fields
                   {"replace": {"2": {"delay": 0}}},
                   {"replace": {"1": {"text": "x"}}},                              # task_note
                   {"replace": {"0": {"session": "b"}}},
                   {"replace": {"0": {"attempt": 9}}},
                   {"replace": {"5": {"text": "x"}}}, {"drop": [3]},               # outside the list
                   {"drop": [0], "replace": {"0": {"text": "x"}}},
                   {"add": [{"do": "interrupt", "session": "ghost"}]},
                   {"add": [{"do": "type", "session": "a", "text": "x", "message": "m2"}]},
                   {"add": [{"do": "type", "session": "a"}]},
                   {"add": [{"do": "kill", "session": "a", "delay": 1}]}]
        for r in refused:
            with self.assertRaises(BadResult, msg=r):
                edit(r)
        for r in ([], {"drop": "0"}, {"replace": {"x": {}}}, {"add": [1]}, {"other": 1}, {"drop": [True]},
                  {"replace": {"²": {"text": "x"}}}, {"add": [{"do": ["type"], "session": "a"}]}):
            with self.assertRaises(BadResult, msg=r):
                corewrite.actions_answer(r, {})


class Checks(unittest.TestCase):
    """Checking entities after an edit — on any JSON from a module's answer, without exceptions (review: a list instead
    of a string crashed hubd)."""

    def setUp(self):
        self.before = reducer.empty()
        for kind, data in (("session.requested", {"cwd": "/w", "session_id": "s", **OWNER}), ("session.started", {})):
            self.before, _, _ = reducer.reduce(self.before, {"seq": 1, "ts": 1, "type": kind, "entity": "a", "data": data})
        self.before, _, _ = reducer.reduce(self.before, {"seq": 2, "ts": 2, "type": "turn.started", "entity": "a",
                                                         "data": {}})
        self.before, _, _ = reducer.reduce(self.before, {"seq": 3, "ts": 3, "type": "message.received", "entity": None,
                                                         "data": {"to": "a", "text": "x", **OWNER}})

    def problems(self, ops):
        after = reducer.copy_state(self.before)
        self.assertEqual(corewrite.patch(after, ops), 0)
        return corewrite.entity_problems(self.before, after, ops)

    def test_values_of_any_shape(self):
        for value in (["busy"], {"x": 1}, None, 5):
            self.assertTrue(self.problems([{"op": "set", "path": ["sessions", "a", "state"], "value": value}]), value)
            self.assertTrue(self.problems([{"op": "set", "path": ["sessions", "a", "closing"], "value": value}]), value)
            self.assertTrue(self.problems([{"op": "set", "path": ["messages", "m3", "session"], "value": [value]}]))
        self.assertEqual(self.problems([{"op": "set", "path": ["sessions", "a", "note"], "value": [1]}]), [])

    def test_a_removed_session_with_live_references(self):
        self.assertIn("referenced by m3",self.problems([{"op": "del", "path": ["sessions", "a"]}])[0])
        self.assertTrue(self.problems([{"op": "set", "path": ["sessions"], "value": {}}]))


class Fold(unittest.TestCase):
    """A record with a module's decision is folded by the core code — the same in hubd (known=None) and in a reader (known=accepted)."""

    def setUp(self):
        self.events = []
        self.st = reducer.empty()
        self.put("session.requested", "a", cwd="/w", session_id="s-a", **OWNER)
        self.put("session.started", "a")

    def put(self, kind, entity=None, fold=None, by=None, **data):
        ev = {"seq": len(self.events) + 1, "ts": 100.0 + len(self.events), "src": "core", "type": kind,
              "entity": entity, "data": data}
        if by:
            ev["by"] = by
        if fold:
            ev["fold"] = fold
        self.st, actions, why = reducer.reduce(self.st, ev)
        self.events.append(ev)
        if why:
            self.events.append({"seq": len(self.events) + 1, "ts": ev["ts"], "src": "core", "type": "core.rejected",
                                "entity": entity, "data": {"of": ev["seq"], "type": kind, "reason": why,
                                                           **({"mod": fold["mod"]} if fold and fold.get("mode")
                                                              == "reject" else {})}})
            self.st, _, _ = reducer.reduce(self.st, self.events[-1])
        return actions, why

    def reader(self, events=None):
        r = views.Reader()
        r.feed_all(events or self.events)
        return r

    def same_as_reader(self):
        r = self.reader()
        self.assertEqual({k: v for k, v in r.state.items() if not k.startswith("_")},
                         {k: v for k, v in self.st.items() if not k.startswith("_")})
        return r

    def test_core_and_skipped_change_nothing(self):
        # case 2 and skip: state is as without the module, no mark
        plain = reducer.reduce(self.st, {"seq": 3, "ts": 102.0, "type": "message.received", "entity": None,
                                         "data": {"to": "a", "text": "x", **OWNER}})
        acts, _ = self.put("message.received", fold={"mod": "m", "mode": "core"}, to="a", text="x", **OWNER)
        self.assertEqual(self.st, plain[0])
        self.assertEqual(acts, plain[1])
        self.put("message.received", fold={"mod": "m", "skipped": "budget"}, to="a", text="y", **OWNER)
        self.assertNotIn("core_written", self.st)
        self.same_as_reader()

    def test_after_patches_what_the_core_made(self):
        # case 3: urgent goes first in _pump
        self.put("turn.started", "a")
        self.put("message.received", to="a", text="first", **OWNER)
        acts, _ = self.put("message.received", fold={"mod": "m", "mode": "after", "patch": [
            {"op": "set", "path": ["messages", "m5", "urgent"], "value": True}]}, to="a", text="urgent", **OWNER)
        self.assertTrue(self.st["messages"]["m5"]["urgent"])
        self.assertEqual(self.st["core_written"], {"format": 1, "modules": {"m": {"first": 5, "last": 5, "since": 104.0,
                                                                                  "count": 1}}})
        r = self.same_as_reader()
        self.assertEqual([it.get("mod") for it in r.timelines["a"] if it.get("id") == "m5"], ["m"])
        self.assertEqual([it.get("mod") for it in r.timelines["a"] if it.get("id") == "m4"], [None])

    def test_replace_skips_the_core_and_still_runs_the_tail(self):
        # case 4: the module puts a message itself — the core prints it (_pump), the timeline follows the message in state
        msg = {"id": "m3", "to": "a", "session": "a", "text": "from a module", "origin": "", "state": "queued",
               "attempt": 0, "deadline": None, "created": 102.0, "since": 102.0}
        acts, why = self.put("message.received", fold={"mod": "m", "mode": "replace", "patch": [
            {"op": "set", "path": ["messages", "m3"], "value": msg}]}, to="a", text="original", **OWNER)
        self.assertIsNone(why)
        self.assertEqual([a["do"] for a in acts], ["type"])
        self.assertIn("from a module", acts[0]["text"])
        r = self.same_as_reader()
        self.assertEqual([it["text"] for it in r.timelines["a"] if it.get("id") == "m3"], ["from a module"])

    def test_after_on_a_rejected_event_is_rejected_whole(self):
        # case 5 in a reader: _apply rejected — the edits are not applied
        _, why = self.put("message.received", fold={"mod": "m", "mode": "after", "patch": [
            {"op": "set", "path": ["owner", "x"], "value": 1}]}, to="ghost", text="x", channel="session", sender="a")
        self.assertIn("no session", why)
        self.assertNotIn("x", self.st["owner"])

    def test_a_patch_that_does_not_land_is_skipped_at_the_reader(self):
        # cases 6 and 13 in a reader: an operation without a parent is skipped, the rest apply; the count goes under a service key
        self.put("owner.away", fold={"mod": "m", "mode": "after", "patch": [
            {"op": "set", "path": ["sessions", "ghost", "x"], "value": 1},
            {"op": "del", "path": ["owner", "nope"]},
            {"op": "set", "path": ["sessions", "a", "flag"], "value": [1, 2]}]}, **OWNER)
        self.assertEqual(self.st.pop("_patch_missed"), 2)
        self.assertEqual(self.st["sessions"]["a"]["flag"], [1, 2])
        self.same_as_reader()

    def test_reject_is_a_verdict_for_every_reader(self):
        # case 24: cancelling a rename — reject; Reader does not move the timeline and does not touch state
        self.put("session.said", "a", text="before")
        _, why = self.put("session.renamed", "a", fold={"mod": "m", "mode": "reject", "reason": "name taken"},
                          to="b", **OWNER)
        self.assertEqual(why, "module m: name taken")
        self.assertIn("a", self.st["sessions"])
        self.assertEqual(self.st["core_written"]["modules"]["m"]["first"], 5)   # the mark is set by a refusal with mod
        r = self.same_as_reader()
        self.assertIn("a", r.timelines)
        self.assertNotIn("b", r.timelines)
        # the last line is cut off after E: a reader without a verdict folds E — and rejects it itself
        r = self.reader(self.events[:-1])
        self.assertIn("a", r.state["sessions"])

    def test_replace_that_honestly_renames_moves_the_feed(self):
        self.put("session.said", "a", text="before")
        st_b = {**self.st["sessions"]["a"], "name": "b"}
        self.put("session.renamed", "a", fold={"mod": "m", "mode": "replace", "patch": [
            {"op": "del", "path": ["sessions", "a"]}, {"op": "set", "path": ["sessions", "b"], "value": st_b}]},
                 to="b", **OWNER)
        r = self.same_as_reader()
        self.assertEqual(sorted(r.state["sessions"]), ["b"])
        self.assertIn("b", r.timelines)          # Reader goes by the event: the timeline moved by data.to
        self.assertNotIn("a", r.timelines)

    def test_replace_on_a_tool_result_still_closes_the_step(self):
        self.put("turn.started", "a")
        self.put("turn.tool", "a", id="t1", name="Bash", input={"command": "ls"})
        self.put("turn.tool_result", "a", fold={"mod": "m", "mode": "replace", "patch": []}, id="t1", text="ok")
        r = self.same_as_reader()
        step = [it for it in r.timelines["a"] if it.get("kind") == "tool"][0]
        self.assertEqual(step["status"], "done")

    def test_replace_with_an_empty_patch_is_an_intervention(self):
        # case 32
        self.put("session.said", "a", fold={"mod": "m", "mode": "replace", "patch": []}, text="quiet")
        self.assertEqual(self.st["core_written"]["modules"]["m"]["count"], 1)
        r = self.same_as_reader()
        self.assertEqual([it.get("mod") for it in r.timelines["a"] if it.get("text") == "quiet"], ["m"])

    def test_a_module_fact_with_by_is_folded_by_the_core_and_marked(self):
        self.put("session.said", "a", by="m", text="from a module")
        self.assertEqual(self.st["core_written"]["modules"]["m"]["first"], 3)
        r = self.same_as_reader()
        self.assertEqual(r.timelines["a"][-1].get("mod"), "m")
        self.assertEqual(corewrite.module_marks(self.events), {"m": {"count": 1, "first": 3, "last": 3}})

    def test_the_owners_word_from_a_module_is_marked(self):
        # case 29 in the fold: the "auto" permission comes only from the host
        acts, _ = self.put("permission.requested", "a", by="m", auto="allow", request_id="r1", tool="Bash",
                           input={"command": "ls"})
        self.assertEqual(acts, [])
        self.assertEqual(len(self.st["sessions"]["a"]["pending"]), 1)

    def test_the_last_line_without_a_verdict_folds_the_same(self):
        # case 15
        self.put("message.received", fold={"mod": "m", "mode": "after", "patch": [
            {"op": "set", "path": ["messages", "m3", "urgent"], "value": True}]}, to="a", text="x", **OWNER)
        r = views.Reader()
        for e in self.events:
            r.feed(e, None)
        self.assertEqual(r.state, self.st)

    def test_a_crash_of_the_core_after_the_mark_is_counted(self):
        self.put("session.said", "a", by="m", text="x")
        self.events.append({"seq": 9, "ts": 1, "type": "core.rejected", "entity": None,
                            "data": {"of": 8, "reason": "core failure on event x: KeyError"}})
        self.st, _, _ = reducer.reduce(self.st, self.events[-1])
        self.assertEqual(self.st["core_written"]["crashes"], 1)

    def test_a_new_write_after_forget_shows_the_warning_again(self):
        self.put("session.said", "a", by="m", text="x")
        self.put("module.forget", module="m", **OWNER)
        self.put("session.said", "a", by="m", text="again")
        self.assertNotIn("forgot", self.st["core_written"]["modules"]["m"])

    def test_forget_hides_the_warning_only(self):
        self.assertIsNotNone(self.put("module.forget", module="m", **OWNER)[1])   # never wrote — nothing to clear
        self.put("session.said", "a", by="m", text="x")
        self.assertIsNotNone(self.put("module.forget", module="m", channel="session", sender="a")[1])
        # a plugin or module process (channel plugin) is not the owner: a module cannot clear a warning about itself
        self.assertIsNotNone(self.put("module.forget", module="m", channel="plugin", owner=False, sender="mod:m")[1])
        self.assertIsNone(self.put("module.forget", module="m", **OWNER)[1])
        self.assertTrue(self.st["core_written"]["modules"]["m"]["forgot"])

    def test_old_code_without_fold_diverges(self):
        # case 27 (rollback to before S1 is not protected — see CHANGES): the old fold does not know fold and folds differently
        self.put("message.received", fold={"mod": "m", "mode": "replace", "patch": []}, to="a", text="x", **OWNER)
        old = reducer.replay([{k: v for k, v in e.items() if k not in ("fold", "by")} for e in self.events])
        self.assertIn("m3", old["messages"])
        self.assertNotIn("m3", self.st["messages"])
        # case 28: code that does not know the field reads the line without failing — the core's fold as before
        self.assertNotIn("core.rejected", [e["type"] for e in self.events])

    def test_rotation_carries_the_lines_and_replays_the_patches(self):
        # case 14: rotate-start — replay of part of the journal by the same code, carried-over lines keep by/fold as is
        d = tempfile.mkdtemp(prefix="cw-rot-")
        self.put("message.received", fold={"mod": "m", "mode": "after", "patch": [
            {"op": "set", "path": ["messages", "m3", "urgent"], "value": True}]}, to="a", text="x", **OWNER)
        for i in range(5):
            self.put("session.said", "a", by="m", text=f"t{i}")
        with open(os.path.join(d, "journal.jsonl"), "w", encoding="utf-8") as f:
            for e in self.events:
                f.write(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n")
        journal.rotate(d, self.st, keep=3, per_session=0)
        rotated = journal.read(os.path.join(d, "journal.jsonl"))
        start = rotated[0]["data"]["state"]
        self.assertTrue(start["messages"]["m3"]["urgent"])
        self.assertEqual(start["core_written"]["modules"]["m"]["count"], 3)
        self.assertEqual([e.get("by") for e in rotated[1:-1]], ["m", "m", "m"])
        # a snapshot without `decisions` (this state never recorded one) is marked done by the replay: an empty list
        self.assertEqual({**reducer.replay(rotated), "seq": 0, "decisions": []}, {**self.st, "seq": 0, "decisions": []})

    def test_after_rotation_the_reader_skips_what_lands_nowhere_and_ends_on_the_snapshot(self):
        # case 13: an edit of a nested key was carried over for an entity created by a line that was not carried over
        self.put("session.requested", "b", cwd="/w", session_id="s-b", **OWNER)
        self.put("owner.away", fold={"mod": "m", "mode": "after", "patch": [
            {"op": "set", "path": ["sessions", "b", "note"], "value": "x"}]}, **OWNER)
        start = reducer.empty()
        start.update(seq=0)
        rotated = [{"seq": 0, "ts": 0, "src": "core", "type": "core.snapshot", "entity": None,
                    "data": {"why": "rotate-start", "state": reducer.empty()}},
                   self.events[-1],
                   {"seq": 99, "ts": 1, "src": "core", "type": "core.snapshot", "entity": None,
                    "data": {"why": "rotate", "state": {k: v for k, v in self.st.items() if not k.startswith("_")}}}]
        r = views.Reader()
        r.feed_all(rotated[:2])
        self.assertEqual(r.state.get("_patch_missed"), 1)
        r.feed_all(rotated[2:])
        self.assertEqual(r.state["sessions"]["b"]["note"], "x")


class Flow(unittest.TestCase):
    def test_signals_from_a_module_line_are_marked(self):
        lines = [json.dumps(e, ensure_ascii=False, separators=(",", ":")).encode() for e in (
            {"seq": 1, "ts": 1, "type": "message.received", "entity": None, "data": {"to": "a", "channel": "web"}},
            {"seq": 2, "ts": 2, "type": "message.received", "entity": None, "data": {"to": "a", "channel": "web"},
             "by": "m"},
            {"seq": 3, "ts": 3, "type": "message.received", "entity": None, "data": {"to": "a", "channel": "web"},
             "fold": {"mod": "m", "mode": "after", "patch": []}},
            {"seq": 4, "ts": 4, "type": "message.received", "entity": None, "data": {"to": "a", "channel": "web"},
             "fold": {"mod": "m", "mode": "core"}})]
        self.assertEqual([s.get("mod") for s in flow.signals(lines)], [None, "m", "m", None])

    def test_the_live_flow_marks_it_too(self):
        r = views.Reader()
        r.feed_all([{"seq": 1, "ts": 1, "type": "session.requested", "entity": "a", "data": {"cwd": "/w", **OWNER}},
                    {"seq": 2, "ts": 2, "type": "message.received", "entity": None, "by": "m",
                     "data": {"to": "a", "text": "x", **OWNER}}])
        self.assertEqual([f.get("mod") for f in r.flow], ["m"])


class Show(unittest.TestCase):
    def test_hub_show_marks_module_lines(self):
        r = views.Reader()
        r.feed_all([{"seq": 1, "ts": 1, "type": "session.requested", "entity": "a", "data": {"cwd": "/w", **OWNER}},
                    {"seq": 2, "ts": 2, "type": "session.said", "entity": "a", "by": "m", "data": {"text": "from a module"}},
                    {"seq": 3, "ts": 3, "type": "session.said", "entity": "a", "data": {"text": "own"}}])
        out = views.show(r, "a")
        self.assertIn("[mod:m]", next(x for x in out if "from a module" in x))
        self.assertNotIn("[mod:", next(x for x in out if "own" in x))


class WithoutModules(unittest.TestCase):
    def test_journal_lines_are_byte_for_byte_as_before(self):
        # case 1: no modules — recorded by the old path; the sample was written by code from before the Module API (tests/golden/scenario.py)
        from tests.golden import scenario
        here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden")
        with open(scenario.run(tempfile.mkdtemp(prefix="cw-golden-")), "rb") as f:
            got = f.read()
        with open(os.path.join(here, "journal_without_modules.jsonl"), "rb") as f:
            want = f.read().replace(b"\r\n", b"\n")   # git with core.autocrlf (Windows) checks the sample out with CRLF
        self.assertEqual(got.decode("utf-8").splitlines(), want.decode("utf-8").splitlines())
        self.assertEqual(got, want)


class Rollback(unittest.TestCase):
    """Case 26: code after S1 and before S9 (mark format 0) does not start on a journal with core.write records."""

    def test_code_that_does_not_understand_the_mark_refuses(self):
        d = tempfile.mkdtemp(prefix="cw-back-")
        st = reducer.empty()
        corewrite.mark(st, "m", {"seq": 5, "ts": 1})
        with mock.patch.object(modules, "CORE_WRITE_FORMAT", 0):
            self.assertIsNotNone(modules.core_write_refusal(d, st))            # the key is in state.json
            with open(os.path.join(d, modules.CORE_WRITTEN), "w") as f:
                json.dump({"format": 1, "modules": {"m": {"first_seq": 5, "ts": 1}}}, f)
            self.assertIsNotNone(modules.core_write_refusal(d))                # a file without state.json
        self.assertIsNone(modules.core_write_refusal(d, st))                   # this code understands it



class ClosedRoots(unittest.TestCase):
    def test_the_records_of_code_edits_and_of_the_move_are_closed(self):
        # release review 2026-11-09: config_seen was closed, its siblings of "About the hub" were not
        for root in ("config_seen", "app_code", "app_move"):
            with self.assertRaises(BadResult, msg=root):
                corewrite._ops([{"op": "del", "path": [root]}], 1)


if __name__ == "__main__":
    unittest.main()
