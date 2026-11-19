"""Eval of the watchdog on the journal of 30.09–03.10 (owner decision 03.10: all 8 fixes): of 23 alerts, 5 were
right. Eight fixes — each on cases from the eval report."""
import tests  # noqa: F401 — keep the machine's settings out
import json
import os
import shutil
import tempfile
import unittest

from core import forms, reducer, spool
from modules.watch import watch
from tests.fakes import lang

T0 = 1_000_000.0


def ev(ts, type, entity="s", **data):
    return {"seq": 0, "ts": T0 + ts, "type": type, "entity": entity, "data": data}


def tool(ts, name, **inp):
    return ev(ts, "turn.tool", name=name, input=inp)


class Plausible(unittest.TestCase):
    def test_1_hub_session_is_not_judged_for_loop_or_errors(self):
        # the hub retells other sessions' troubles — a "hub loop" about two other sessions
        steps = [ev(0, "turn.started", prompt_head="x")] + [tool(i, "Bash", command="hub show x") for i in range(1, 5)]
        hub = {"kind": "hub", "state": "busy"}
        self.assertFalse(watch.plausible("loop", steps, T0 + 10, hub))
        self.assertFalse(watch.plausible("errors", steps + [ev(6, "turn.tool_result", error=True)] * 3, T0 + 10, hub))
        self.assertTrue(watch.plausible("hung", steps, T0 + 1000, hub))

    def test_2_scheduled_turns_are_never_a_loop(self):
        # the session writes its result as text — three false "loops" on scheduled checks
        steps = []
        for i in range(4):
            steps += [ev(i * 100, "turn.started", prompt_head=f"{lang.head('m1', kind='schedule')} check the chat"),
                      tool(i * 100 + 1, "mcp__ms365__list-chat-messages", chatId="c"),
                      ev(i * 100 + 2, "turn.result", text="Nothing new, the task is in progress.")]
        self.assertFalse(watch.plausible("loop", steps, T0 + 500, {"state": "idle"}))

    def test_3_edit_test_edit_is_work_not_a_loop(self):
        start = [ev(0, "turn.started", prompt_head="fix the tests")]
        work = []
        for i in range(4):
            work += [tool(10 * i + 1, "Edit", file_path="a.py"), tool(10 * i + 2, "Bash", command="pytest -q")]
        self.assertFalse(watch.plausible("loop", start + work, T0 + 100, {"state": "busy"}))
        same = [tool(i, "Bash", command="curl -s http://x/health") for i in range(1, 4)]
        self.assertTrue(watch.plausible("loop", start + same, T0 + 100, {"state": "busy"}))
        # identical calls in different turns are not a loop
        split = [tool(1, "Bash", command="ls"), ev(2, "turn.started", prompt_head="y"), tool(3, "Bash", command="ls"),
                 tool(4, "Bash", command="ls")]
        self.assertFalse(watch.plausible("loop", start + split, T0 + 100, {"state": "busy"}))

    def test_4_waiting_for_a_permission_is_not_hung(self):
        steps = [ev(0, "turn.started", prompt_head="x"), ev(5, "permission.requested", tool="Bash")]
        self.assertFalse(watch.plausible("hung", steps, T0 + 900, {"state": "busy"}))

    def test_4b_a_request_the_core_allowed_itself_does_not_hide_a_hang(self):
        # permissions.auto_allow: the core answered itself, nobody waits for the owner — the silence after that is a hang
        steps = [ev(0, "turn.started", prompt_head="x"), {**ev(5, "permission.requested", tool="Bash", auto="allow"),
                                                          "src": "host"}]
        self.assertTrue(watch.plausible("hung", steps, T0 + 900, {"state": "busy"}))
        steps[-1]["src"] = "session"     # the mark is not from the host — the core holds the request for the owner: it waits, not hung
        self.assertFalse(watch.plausible("hung", steps, T0 + 900, {"state": "busy"}))

    def test_5_background_agent_running_is_not_hung(self):
        steps = [ev(0, "turn.started", prompt_head="x"), tool(1, "Agent", description="review")]
        sess = {"state": "busy", "background": {"a1": {"id": "a1", "kind": "agent", "title": "review"}}}
        self.assertFalse(watch.plausible("hung", steps, T0 + 900, sess))


class Facts(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="watch-eval-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.w = watch.Watcher(self.dir, cfg={"off": True})
        self.seq = 0

    def feed(self, t, type, entity=None, **data):
        self.seq += 1
        self.w.feed({"seq": self.seq, "ts": T0 + t, "type": type, "entity": entity, "data": data}, T0 + t)

    def alerts(self, kind=None):
        out = [e for _, e in spool.take(os.path.join(self.dir, "spool")) if e["type"] == "watch.alert"]
        for n in os.listdir(os.path.join(self.dir, "spool")) if os.path.isdir(os.path.join(self.dir, "spool")) else []:
            os.unlink(os.path.join(self.dir, "spool", n))
        return [e for e in out if kind is None or e["data"]["kind"] == kind]

    def session(self, name):
        self.feed(0, "session.requested", name, cwd="/w", session_id=f"s-{name}", channel="web", owner=True)
        self.feed(1, "session.started", name)

    def test_6_stall_after_a_cut_turn_is_reported(self):
        self.session("pe")
        self.w.started = True
        self.feed(10, "turn.started", "pe", marker=None)
        self.feed(11, "session.exited", "pe", why="reconcile")      # a restart cut the turn
        self.feed(15, "session.started", "pe")
        self.w.facts(T0 + 15 + 19 * 60)
        self.assertEqual(self.alerts("stalled"), [])
        self.w.facts(T0 + 15 + 21 * 60)
        (a,) = self.alerts("stalled")
        self.assertEqual((a["entity"], a["data"]["kind"]), ("pe", "stalled"))
        self.w.facts(T0 + 15 + 40 * 60)
        self.assertEqual(self.alerts("stalled"), [])                         # once

    def test_6_new_turn_after_the_cut_clears_it(self):
        self.session("pe")
        self.w.started = True
        self.feed(10, "turn.started", "pe", marker=None)
        self.feed(11, "session.exited", "pe", why="reconcile")
        self.feed(15, "session.started", "pe")
        self.feed(60, "turn.started", "pe", marker=None)
        self.feed(70, "turn.stopped", "pe")
        self.w.facts(T0 + 3600)
        self.assertEqual(self.alerts("stalled"), [])

    def test_7_phantom_undelivered_is_checked_against_the_core(self):
        self.session("pe")
        self.w.started = True
        self.w.state["messages"]["m50x3"] = {"id": "m50x3", "session": "pe", "state": "typing", "created": T0 + 2,
                                             "attempt": 2, "text": "continue", "origin": forms.ORIGIN_CORE}
        with open(os.path.join(self.dir, "state.json"), "w") as f:     # the core has no such message
            json.dump({"seq": 100, "state": {"messages": {}}}, f)
        self.w.facts(T0 + 3 * 3600)
        self.assertEqual(self.alerts(), [])
        with open(os.path.join(self.dir, "state.json"), "w") as f:     # it exists and hangs — an alert
            json.dump({"seq": 100, "state": {"messages": {"m50x3": {"state": "typing"}}}}, f)
        self.w.told.clear()
        self.w.facts(T0 + 3 * 3600)
        self.assertEqual([a["data"]["kind"] for a in self.alerts()], ["undelivered"])


class Urgency(unittest.TestCase):
    def test_8_core_service_is_never_inserted_and_owner_status_question_always(self):
        self.assertEqual(watch.urgency_rule({"origin": forms.ORIGIN_CORE, "text": "Hub restarted"}), 0.0)
        # a short status question in Russian (the rule is the language data's; the eval's case was "what are you doing now?")
        self.assertEqual(watch.urgency_rule({"origin": forms.ORIGIN_OWNER, "text": lang.word("watch", "status_q") + "?"}), 1.0)
        self.assertIsNone(watch.urgency_rule({"origin": forms.ORIGIN_OWNER, "text": "make another sales report"}))
        self.assertIsNone(watch.urgency_rule({"origin": f" {forms.OLD_FROM} hub-core", "text": "stop, do not touch prod"}))


if __name__ == "__main__":
    unittest.main()
