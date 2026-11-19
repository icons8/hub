"""_pump skips an idle session with no messages and no marks (reducer._pump_quiet): on a hub with hundreds of sessions,
a full pass over each at every event made the page's first pass over the journal take more than half an hour
(2026-10-25). Skipping does not change the outcome: each scenario is folded twice - with the skip and with the full
pass - and the state and actions match; the scenario's preconditions (the session is idle, the needed _pump branch
fired) are checked on the result."""
import tests  # noqa: F401 - ignore the machine's settings
import ast
import copy
import inspect
import json
import textwrap
import unittest
from unittest import mock

from core import reducer

OWNER = {"channel": "cli", "owner": True}
NAMES = ("a", "b", "c", "z")   # z is only for the tick: an accepted event after which _pump runs


def tick():
    return {"type": "owner.seen", "entity": "z", "data": {"channel": "web", "owner": True}}


def put(name, **kv):
    def f(st):
        st["sessions"][name].update(kv)
    return f


def base():
    steps = []
    for n in NAMES:
        steps += [{"type": "session.requested", "entity": n,
                   "data": {"cwd": f"/w/{n}", "session_id": f"s-{n}", **OWNER}},
                  {"type": "session.started", "entity": n}]
    return steps + [lambda st: [s.pop("dirty", None) for s in st["sessions"].values()]]


def check(cond, what):
    def f(st):
        assert cond(st), what
    return f


class PumpQuiet(unittest.TestCase):
    def run_both(self, steps):
        """steps are events (dict), edits and state checks (functions st -> None); the result with and without the skip."""
        out = []
        for full in (False, True):
            with mock.patch.object(reducer, "_pump_quiet", (lambda s: False) if full else reducer._pump_quiet):
                st, seq, acts = reducer.empty(), 0, []
                for step in steps:
                    if callable(step):
                        step(st)
                        continue
                    seq += 1
                    e = {"seq": seq, "ts": 100 + seq, "entity": None, "data": {}, **copy.deepcopy(step)}
                    st, a, why = reducer.reduce(st, e)
                    self.assertIsNone(why, (e, why))
                    acts.append((e["type"], a))
            out.append(json.loads(json.dumps({"state": st, "actions": acts}, sort_keys=True, default=str)))
        self.assertEqual(out[0], out[1])
        return out[0]

    def last(self, got):
        return [a["do"] for a in got["actions"][-1][1]]

    def test_marks_that_make_an_idle_session_act(self):
        idle = check(lambda st: st["sessions"]["a"]["state"] == "idle", "a is free before the tick")
        cases = {   # mark -> what fired the branch
            "respawn_minihub": ({"respawn_minihub": None}, lambda g: "respawn_minihub" not in g["state"]["sessions"]["a"]),
            "respawn_minihub=False": ({"respawn_minihub": False},
                                      lambda g: "respawn_minihub" not in g["state"]["sessions"]["a"]),
            "compact=None": ({"compact": None}, lambda g: "compact" not in g["state"]["sessions"]["a"]),
            "compact=''": ({"compact": ""}, lambda g: "compact" not in g["state"]["sessions"]["a"]),
            "compact": ({"compact": "squeeze"}, lambda g: "compact" in self.last(g)),
            "dirty": ({"dirty": True}, lambda g: "release" in self.last(g)),
            "rename_to": ({"rename_to": "a2"}, lambda g: "a2" in g["state"]["sessions"]),
            "plugins_stale": ({"plugins_stale": ["p@x"]}, lambda g: "plugins_stale" not in g["state"]["sessions"]["a"]),
        }
        for label, (kv, fired) in cases.items():
            with self.subTest(label):
                got = self.run_both(base() + [idle, put("a", **kv), tick()])
                self.assertTrue(fired(got), label)

    def test_falsy_marks_do_nothing(self):
        for kv in ({"dirty": False}, {"limit_cut": None}, {"rename_to": ""}, {"plugins_stale": []},
                   {"browser_stale": True}, {"compacting": 1}):
            with self.subTest(kv):
                got = self.run_both(base() + [put("a", **kv), tick()])
                self.assertEqual(self.last(got), [])
                self.assertEqual(got["state"]["sessions"]["a"]["state"], "idle")

    def test_idle_session_on_a_stale_account_with_a_cut_turn(self):
        got = self.run_both(base() + [put("a", account="old", account_gen=1, limit_cut={"ts": 1}),
                                      put("b", account="old", account_gen=1), tick()])
        self.assertEqual(self.last(got).count("release"), 1)   # the one cut off by the limit goes to a new account; b waits for work

    def test_queued_and_typing(self):
        got = self.run_both(base() + [{"type": "message.received", "data": {"to": "a", "text": "one", **OWNER}},
                                      {"type": "message.received", "data": {"to": "a", "text": "two", **OWNER}}, tick()])
        states = sorted(m["state"] for m in got["state"]["messages"].values())
        self.assertEqual(states, ["queued", "typing"])

    def test_aside_rides_its_carrier(self):
        got = self.run_both(base() + [
            {"type": "turn.started", "entity": "c", "data": {"marker": "x"}},
            {"type": "message.received", "data": {"to": "c", "text": "FYI", "fyi": True, **OWNER}},
            check(lambda st: [m["state"] for m in st["messages"].values()] == ["aside"], "the aside waits for a carrier"),
            {"type": "message.received", "data": {"to": "c", "text": "carrier", **OWNER}},
            {"type": "turn.result", "entity": "c", "data": {"text": "ok"}},
            check(lambda st: st["messages"]["m10"].get("with") == "m11", "the aside rides with the carrier"),
            {"type": "turn.started", "entity": "c", "data": {"marker": "m11"}}, tick()])
        self.assertEqual({k: m["state"] for k, m in got["state"]["messages"].items()},
                         {"m10": "delivered", "m11": "delivered"})
        self.assertNotIn("with", got["state"]["messages"]["m10"])

    def test_urgent_for_a_busy_session(self):
        def urgent(st):
            (m,) = st["messages"].values()
            assert m["state"] == "queued"
            m["urgent"] = True
        got = self.run_both(base() + [{"type": "turn.started", "entity": "b", "data": {"marker": "y"}},
                                      {"type": "message.received", "data": {"to": "b", "text": "a case", **OWNER}},
                                      urgent, tick()])
        self.assertIn("type", self.last(got))

    def test_handover_to_a_missing_executor(self):
        def handover(st):
            (m,) = st["messages"].values()
            m["handover"] = "nobody"
        got = self.run_both(base() + [{"type": "turn.started", "entity": "a", "data": {"marker": "x"}},
                                      {"type": "message.received", "data": {"to": "a", "text": "a case", **OWNER}},
                                      handover, {"type": "turn.result", "entity": "a", "data": {"text": "result"}}])
        (m,) = got["state"]["messages"].values()
        self.assertNotIn("handover", m)
        self.assertEqual(m["state"], "typing")

    def test_every_mark_pump_reads_is_known_to_the_skip(self):
        """_pump_quiet is a hand-kept list of marks on which _pump branches work even without messages: a new branch on
        a new mark would silently not fire for idle sessions. The marks come from parsing the _pump code (not from that
        list): each is either in _pump_quiet or in the list below - those that act only with a message or only refine
        another branch."""
        def marks(fn):
            out = set()
            for n in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(fn)))):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ("get", "pop") \
                        and isinstance(n.func.value, ast.Name) and n.func.value.id == "sess" and n.args \
                        and isinstance(n.args[0], ast.Constant):
                    out.add(n.args[0].value)
                elif isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name) and n.value.id == "sess" \
                        and isinstance(n.slice, ast.Constant):
                    out.add(n.slice.value)
                elif isinstance(n, ast.Compare) and isinstance(n.left, ast.Constant) \
                        and any(isinstance(c, ast.Name) and c.id == "sess" for c in n.comparators):
                    out.add(n.left.value)
            return out
        with_message_only = {"state", "kind", "pending", "background", "hold_respawn", "browser_stale", "compacting",
                             "forwarding"}   # the core moves the folder: it only holds typing
        # read above the skip, for a session mid-turn (busy, dialog) — the skip is for idle ones: an MCP granted mid-turn
        # joins the running process (mcp_live) with the session's tools
        before_the_skip = {"mcp_live", "tools"}
        self.assertTrue(marks(reducer._pump_quiet))
        self.assertEqual(marks(reducer._pump) - marks(reducer._pump_quiet) - with_message_only - before_the_skip, set(),
                         "a new mark in _pump: does the branch work without messages? yes — in _pump_quiet, no — in the list")


if __name__ == "__main__":
    unittest.main()
