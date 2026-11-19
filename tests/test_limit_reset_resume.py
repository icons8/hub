"""A turn cut by an account limit on a hub without an account pool continues by itself when the window resets
(core/limitresume.py, the daemon step resume_cut, the fact session.limit_reset). Before this, such a hub never noticed the
reset: the cut session stood until someone wrote to it (a hub restart does not continue it either), and an executor that
fell asleep after the cut waited for a hand to wake it."""
import tests  # noqa: F401 - ignore the machine's settings
import inspect
import json
import tempfile
import types
import unittest

from core import admit, executors, limitresume, reducer
from core.daemon import Hub

OWNER = {"channel": "cli", "owner": True}
CORE = {"channel": "core"}
RESETS = 5000
LIMIT_TEXT = "You've hit your session limit · resets 6:20am (Asia/Almaty)"
GRACE = limitresume.RESET_GRACE
AFTER = RESETS + GRACE + 1   # "Continue" after the reset: before the clock's pass, past the clocks' difference


def by(name):
    return {"channel": "session", "owner": False, "sender": name}


class Folding:
    """A fold with helpers (not a TestCase: a subclass does not pull in others' tests)."""

    def setUp(self):
        self.st, self.seq, self.acts = reducer.empty(), 0, []

    def ev(self, t, e=None, ts=None, **d):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts or 100 + self.seq, "type": t,
                                                      "entity": e, "data": d})
        self.acts = acts
        return why

    def limits(self, status="rejected", resets=RESETS):
        return self.ev("account.limits", None, **{"status": status, "type": "five_hour",
                                                   "windows": {"five_hour": {"used": 1, "resets": resets}}, **OWNER})

    def texts(self, name):
        return [m["text"] for m in self.st["messages"].values() if m["session"] == name]

    def continuations(self, name):
        return [t for t in self.texts(name) if "window has reset" in t]


class Cut(Folding, unittest.TestCase):
    def fleet(self):
        self.ev("session.requested", "hub", cwd="/h", session_id="s-h", kind="hub", **OWNER)
        self.ev("session.requested", "maps", cwd="/m", session_id="s-m", **OWNER)
        for n in ("hub", "maps"):
            self.ev("session.started", n)

    def cut(self, name="maps", resets=RESETS, status="rejected"):
        self.ev("message.received", None, to=name, text="do it", channel="telegram", owner=True)
        mid = f"m{self.seq}"
        self.ev("turn.started", name, marker=mid)
        self.limits(status, resets)
        self.ev("turn.result", name, text=LIMIT_TEXT, is_error=True)
        return mid

    def reset(self, name="maps", resets=RESETS, **extra):
        return self.ev("session.limit_reset", name, **{**CORE, "resets": resets, **extra})

    def test_the_cut_remembers_when_the_window_resets(self):
        self.fleet()
        self.cut()
        self.assertEqual(self.st["sessions"]["maps"]["limit_cut"]["resets"], RESETS)

    def test_a_reset_that_is_unknown_or_already_past_is_not_remembered(self):
        self.fleet()
        self.cut(status="allowed")
        self.assertIsNone(self.st["sessions"]["maps"]["limit_cut"]["resets"])
        self.ev("turn.started", "maps")
        self.cut(resets=1)
        self.assertIsNone(self.st["sessions"]["maps"]["limit_cut"]["resets"])

    def test_the_reset_continues_the_cut_turn_in_its_thread_by_the_same_process(self):
        self.fleet()
        mid = self.cut()
        self.assertIsNone(self.reset())
        maps = self.st["sessions"]["maps"]
        self.assertNotIn("limit_cut", maps)
        self.assertEqual(maps["limit_reset_done"], RESETS)
        (text,) = self.continuations("maps")
        go = [m for m in self.st["messages"].values() if m["session"] == "maps"][-1]
        self.assertEqual((go["text"], go.get("re")), (text, mid))
        self.assertEqual([a["do"] for a in self.acts if a.get("do") in ("spawn", "type")], ["type"])

    def test_a_second_fact_for_the_same_reset_continues_nothing_again(self):
        self.fleet()
        self.cut()
        self.reset()
        self.reset()
        self.assertEqual(len(self.continuations("maps")), 1)

    def test_a_fact_for_another_reset_or_a_session_that_is_not_cut_changes_nothing(self):
        self.fleet()
        self.cut()
        self.assertIsNone(self.reset(resets=RESETS + 1))
        self.assertTrue(self.st["sessions"]["maps"].get("limit_cut"))
        self.assertIsNone(self.reset("hub"))
        self.assertEqual(self.continuations("maps") + self.continuations("hub"), [])

    def test_only_the_core_writes_the_fact(self):
        self.fleet()
        self.cut()
        self.assertIsNotNone(self.ev("session.limit_reset", "maps", resets=RESETS, **OWNER))
        self.assertTrue(self.st["sessions"]["maps"].get("limit_cut"))

    def test_the_fact_from_the_mailbox_is_core_only(self):
        self.fleet()
        why = admit.check(self.st, "session.limit_reset", "maps", {**CORE, "resets": RESETS}, "session")
        self.assertEqual(why.code, admit.CORE_ONLY_CODE)

    def test_a_turn_that_began_before_the_fact_leaves_nothing_to_continue(self):
        self.fleet()
        self.cut()
        self.ev("message.received", None, to="maps", text="how is it going?", channel="web", owner=True)
        self.ev("turn.started", "maps", marker=f"m{self.seq}")
        self.assertNotIn("limit_cut", self.st["sessions"]["maps"])
        self.reset()
        self.assertEqual(self.continuations("maps"), [])

    def test_a_parked_cut_session_is_woken_with_the_continuation(self):
        self.fleet()
        self.cut()
        self.ev("session.parked", "maps", channel="core", why="restart")
        self.reset()
        self.assertEqual(len(self.continuations("maps")), 1)
        self.assertEqual(self.st["sessions"]["maps"]["state"], "starting")

    def test_a_cut_session_put_to_sleep_or_taken_by_the_owner_is_left_alone(self):
        self.fleet()
        self.cut()
        self.ev("session.ended", "maps", reason="prompt_input_exit")
        self.assertEqual(self.st["sessions"]["maps"]["state"], "asleep")
        self.reset()
        self.assertEqual(self.continuations("maps"), [])
        self.assertTrue(self.st["sessions"]["maps"].get("limit_cut"))

    def test_a_restart_does_not_continue_it_but_the_fact_after_it_does(self):
        self.fleet()
        self.cut()
        self.ev("session.exited", "maps", why="reconcile")
        self.ev("session.started", "maps", session_id="s-m")
        self.assertEqual(self.continuations("maps"), [])
        self.assertTrue(self.st["sessions"]["maps"].get("limit_cut"))
        self.reset()
        self.assertEqual(len(self.continuations("maps")), 1)

    def test_a_cut_again_for_the_same_reset_is_not_continued_a_second_time(self):
        self.fleet()
        self.cut()
        self.reset()
        self.ev("message.received", None, to="maps", text="go on", channel="web", owner=True)
        self.ev("turn.started", "maps", marker=f"m{self.seq}")
        self.limits()
        self.ev("turn.result", "maps", text=LIMIT_TEXT, is_error=True)
        self.assertEqual(limitresume.due(self.st, RESETS + limitresume.RESET_GRACE), [])


class Resume(Folding, unittest.TestCase):
    """The owner's "Continue" on a session paused by a limit (POST /api/session/resume, the fact session.resume_requested):
    the cut turn continues now, as the clock or the pool would continue it, once the window is over - not before, and not
    in a session that is not paused. Before, the clients sent a reload: it restarted the process and the cut turn stood."""
    fleet, cut, reset = Cut.fleet, Cut.cut, Cut.reset

    def resume(self, name="maps", ts=None, who=OWNER):
        return self.ev("session.resume_requested", name, ts=ts, **who)

    def pooled_cut(self):
        self.ev("account.switched", None, to="main", restart=True, **CORE)
        self.fleet()
        return self.cut()

    def test_after_the_reset_it_continues_the_cut_turn_now_and_the_clock_adds_nothing(self):
        self.fleet()
        mid = self.cut()
        self.assertIsNone(self.resume(ts=AFTER))   # before the clock's grace: the clock would not continue it yet
        maps = self.st["sessions"]["maps"]
        self.assertNotIn("limit_cut", maps)
        self.assertEqual(maps["limit_reset_done"], RESETS)
        (text,) = self.continuations("maps")
        go = [m for m in self.st["messages"].values() if m["session"] == "maps"][-1]
        self.assertEqual((go["text"], go.get("re")), (text, mid))
        self.assertEqual([a["do"] for a in self.acts if a.get("do") in ("spawn", "type")], ["type"])
        self.assertEqual(limitresume.due(self.st, RESETS + 60), [])
        self.reset()
        self.assertEqual(len(self.continuations("maps")), 1)

    def test_before_the_reset_it_is_refused_with_the_time_it_continues_at(self):
        self.fleet()
        self.cut()
        why = self.resume(ts=4000)
        self.assertEqual(reducer.forms.code_of(why), "not_yet")
        self.assertIn("01:23", why)   # RESETS seconds after the epoch, the owner's zone is UTC
        self.assertTrue(self.st["sessions"]["maps"].get("limit_cut"))
        self.assertEqual(self.continuations("maps"), [])

    def test_a_later_window_that_still_refuses_holds_it_until_that_one(self):
        self.fleet()
        self.cut()
        self.limits(resets=9000)   # a longer window refuses too
        why = self.resume(ts=AFTER)
        self.assertEqual(reducer.forms.code_of(why), "not_yet")
        self.assertIn("02:30", why)
        self.assertEqual(limitresume.held(self.st, self.st["sessions"]["maps"], AFTER), ("not_yet", 9000 + GRACE))

    def test_a_reset_on_another_day_names_the_date(self):
        self.fleet()
        self.cut(resets=RESETS + 86400)
        self.assertIn("02.01 01:23", self.resume(ts=4000))

    def test_an_unknown_reset_is_tried_now(self):
        self.fleet()
        self.cut(status="allowed")
        self.assertIsNone(self.resume())
        self.assertEqual(len(self.continuations("maps")), 1)
        self.assertNotIn("limit_cut", self.st["sessions"]["maps"])

    def test_a_session_not_cut_by_a_limit_is_refused_as_not_paused(self):
        self.fleet()
        why = self.resume()
        self.assertEqual(reducer.forms.code_of(why), "not_paused")
        self.assertEqual(limitresume.held(self.st, self.st["sessions"]["maps"], 200), ("not_paused", None))
        self.cut()
        self.ev("message.received", None, to="maps", text="how is it going?", channel="web", owner=True)
        self.ev("turn.started", "maps", marker=f"m{self.seq}")   # a new turn ended the pause
        self.assertEqual(reducer.forms.code_of(self.resume(ts=AFTER)), "not_paused")
        self.assertEqual(self.continuations("maps"), [])

    def test_only_the_owner_or_the_hub_continue_it(self):
        self.fleet()
        self.cut()
        self.assertIsNotNone(self.resume(ts=AFTER, who=by("maps")))
        self.assertTrue(self.st["sessions"]["maps"].get("limit_cut"))
        self.assertIsNone(self.resume(ts=AFTER, who=by("hub")))
        self.assertEqual(len(self.continuations("maps")), 1)

    def test_a_session_put_to_sleep_is_refused_by_its_state(self):
        self.fleet()
        self.cut()
        self.ev("session.ended", "maps", reason="prompt_input_exit")
        why = self.resume(ts=AFTER)
        self.assertIn("asleep", why)
        self.assertTrue(self.st["sessions"]["maps"].get("limit_cut"))

    def test_a_parked_cut_session_is_woken_with_the_continuation(self):
        self.fleet()
        self.cut()
        self.ev("session.parked", "maps", channel="core", why="restart")
        self.assertIsNone(self.resume(ts=AFTER))
        self.assertEqual(len(self.continuations("maps")), 1)
        self.assertEqual(self.st["sessions"]["maps"]["state"], "starting")

    def test_a_pool_waiting_for_the_reset_holds_it_until_then_and_after_it_continues_in_place(self):
        mid = self.pooled_cut()
        self.ev("account.waiting", None, account="main", until=6000, why="main: limit", **CORE)
        why = self.resume(ts=5500)
        self.assertEqual(reducer.forms.code_of(why), "not_yet")
        self.assertEqual(limitresume.held(self.st, self.st["sessions"]["maps"], 5500), ("not_yet", 6000 + GRACE))
        self.assertIsNone(self.resume(ts=6000 + GRACE + 1))
        go = [m for m in self.st["messages"].values() if m["session"] == "maps"][-1]
        self.assertEqual((go["text"], go.get("re")), (reducer.LIMIT_RESET, mid))
        self.assertEqual([a["do"] for a in self.acts if a.get("do") in ("spawn", "type")], ["type"])

    def test_an_exhausted_pool_holds_it_until_the_first_account_frees(self):
        self.pooled_cut()
        self.ev("account.exhausted", None, until=7000, why="all busy", **CORE)
        why = self.resume(ts=5500)
        self.assertEqual(reducer.forms.code_of(why), "not_yet")
        self.assertIn("01:56", why)

    def test_a_pool_account_with_room_continues_it_there_once(self):
        self.pooled_cut()
        self.ev("account.switched", None, to="work", **{"from": "main"}, restart=True, **CORE)
        self.assertIsNone(limitresume.held(self.st, self.st["sessions"]["maps"], 5500))
        self.assertIsNone(self.resume(ts=5500))
        self.ev("session.started", "maps", session_id="s-m")
        self.assertEqual(sum("The Claude account limit cut off your turn" in t for t in self.texts("maps")), 1)
        self.assertNotIn("limit_cut", self.st["sessions"]["maps"])

    def test_the_owners_request_from_the_mailbox_is_admitted(self):
        self.fleet()
        self.assertIsNone(admit.check(self.st, "session.resume_requested", "maps", {"channel": "web", "owner": True},
                                      "web"))


    def test_right_at_the_reset_it_waits_for_the_clocks_difference_as_the_clock_does(self):
        # the API's clock may be a few seconds behind: a turn sent then is cut again with no reset ahead, and a cut like
        # that is never continued by the clock - the same RESET_GRACE holds the button too
        self.fleet()
        self.cut()
        why = self.resume(ts=RESETS + 1)
        self.assertEqual(reducer.forms.code_of(why), "not_yet")
        self.assertTrue(self.st["sessions"]["maps"].get("limit_cut"))
        self.assertEqual(limitresume.due(self.st, RESETS + GRACE), ["maps"])

    def codex_cut(self, resets):
        self.ev("message.received", None, to="maps", text="make a report", channel="web", owner=True)
        self.ev("turn.started", "maps", marker=list(self.st["messages"])[-1])
        self.ev("turn.result", "maps", text="Codex: usage limit reached (usageLimitExceeded)", is_error=True,
                engine="codex", limit=True, limit_resets=resets)

    def test_a_codex_cut_is_held_by_its_own_window_and_continued_with_its_own_words(self):
        self.fleet()
        self.limits(resets=9000)   # Claude's window still refuses: not about a Codex turn
        self.codex_cut(7000)
        self.assertEqual(limitresume.held(self.st, self.st["sessions"]["maps"], 6999), ("not_yet", 7000))
        self.assertIsNone(self.resume(ts=7001))
        go = [m for m in self.st["messages"].values() if m["session"] == "maps"][-1]
        self.assertEqual(go["text"], reducer.LIMIT_CUT_CODEX)

    def test_a_claude_pool_does_not_decide_for_a_codex_cut(self):
        self.ev("account.switched", None, to="main", restart=True, **CORE)
        self.fleet()
        self.codex_cut(9000)
        self.assertEqual(reducer.forms.code_of(self.resume(ts=5500)), "not_yet")


    def test_a_message_on_its_way_is_the_continuation_and_the_button_adds_none(self):
        # as the clock (due): the owner's message typed to the cut session starts the turn that ends the cut
        self.fleet()
        self.cut()
        self.ev("message.received", None, to="maps", text="and also this", channel="web", owner=True)
        self.assertEqual(reducer.forms.code_of(self.resume(ts=AFTER)), "not_paused")
        self.assertEqual(self.continuations("maps"), [])

    def test_a_codex_cut_has_no_grace_as_its_clock_and_leaves_the_claude_clock_alone(self):
        self.fleet()
        self.codex_cut(7000)
        self.assertIsNone(self.resume(ts=7001))
        self.assertNotIn("limit_reset_done", self.st["sessions"]["maps"])   # the Claude clock's mark, not Codex's


class Due(Folding, unittest.TestCase):
    def cut_session(self, name, ts, how="plain", resets=RESETS):
        self.ev("session.requested", name, cwd=f"/{name}", session_id=f"s-{name}", **OWNER)
        self.ev("session.started", name)
        sess = self.st["sessions"][name]
        sess["limit_cut"] = {"ts": ts, "account": None, "gen": None, "resets": resets,
                             **({"re": "m1"} if how == "owner" else {"for_delegation": "d1"} if how == "task" else {})}
        return sess

    def test_nothing_is_due_until_the_reset_plus_the_grace(self):
        self.cut_session("a", 10)
        self.assertEqual(limitresume.due(self.st, RESETS + limitresume.RESET_GRACE - 1), [])
        self.assertEqual(limitresume.due(self.st, RESETS + limitresume.RESET_GRACE), ["a"])

    def test_without_a_known_reset_or_with_an_account_pool_it_is_not_this_ones_business(self):
        self.cut_session("a", 10, resets=None)
        self.cut_session("pool", 11)["limit_cut"]["account"] = "main"
        self.assertEqual(limitresume.due(self.st, RESETS + 3600), [])

    def test_states_that_are_woken_and_states_that_are_not(self):
        for n, state in (("idle", "idle"), ("parked", "parked"), ("retired", "retired"), ("asleep", "asleep"),
                         ("held", "held"), ("closed", "closed"), ("removed", "removed"), ("failed", "failed")):
            self.cut_session(n, 10)["state"] = state
        self.assertEqual(sorted(limitresume.due(self.st, RESETS + 100, step=99)), ["idle", "parked", "retired"])

    def test_a_hub_with_an_account_pool_continues_nothing_by_the_clock_even_off_the_pool(self):
        self.cut_session("off-pool", 10)
        self.st["account"] = {"active": "main"}
        self.assertEqual(limitresume.due(self.st, RESETS + 3600), [])

    def test_a_session_with_a_message_queued_waits_for_that_turn(self):
        self.cut_session("a", 10)
        self.ev("message.received", None, to="a", text="how is it going?", channel="web", owner=True)
        self.assertEqual(limitresume.due(self.st, RESETS + 100), [])

    def test_a_message_handed_to_an_executor_does_not_hold_the_specialist(self):
        self.cut_session("a", 10)
        self.ev("message.received", None, to="a", text="how is it going?", channel="web", owner=True)
        (m,) = [m for m in self.st["messages"].values() if m["session"] == "a"]
        m["handover"] = "a-x"
        self.assertEqual(limitresume.due(self.st, RESETS + 100), ["a"])

    def test_one_continuation_per_reset(self):
        self.cut_session("a", 10)["limit_reset_done"] = RESETS
        self.assertEqual(limitresume.due(self.st, RESETS + 100), [])
        self.st["sessions"]["a"]["limit_cut"]["resets"] = RESETS + 7200
        self.assertEqual(limitresume.due(self.st, RESETS + 7200 + 100), ["a"])

    def test_the_owners_thread_first_then_tasks_then_the_rest_oldest_first_and_a_few_at_a_time(self):
        self.cut_session("rest-old", 10)
        self.cut_session("task", 11, "task")
        self.cut_session("rest-new", 12)
        self.cut_session("owner-new", 13, "owner")
        self.cut_session("owner-old", 9, "owner")
        self.cut_session("task-old", 8, "task")
        now = RESETS + 100
        first = limitresume.due(self.st, now)
        self.assertEqual(first, ["owner-old", "owner-new", "task-old", "task"])
        for n in first:
            self.st["sessions"][n].pop("limit_cut")
        self.assertEqual(limitresume.due(self.st, now), ["rest-old", "rest-new"])


class Tick(unittest.TestCase):
    def hub(self):
        h = Hub(tempfile.mkdtemp(prefix="limit-reset-"), types.SimpleNamespace(poll=lambda: []))
        h.execute = lambda a: None
        self.now = [1000.0]
        h.clock = lambda: self.now[0]
        return h

    def cut(self, h, name, resets):
        h.record("session.requested", name, {"cwd": f"/{name}", "session_id": f"s-{name}", **OWNER})
        h.record("session.started", name, {"pid": 1}, src="host")
        h.record("message.received", None, {"to": name, "text": "do it", **OWNER})
        typed = next(m["id"] for m in h.state["messages"].values() if m["session"] == name and m["state"] == "typing")
        h.record("turn.started", name, {"marker": typed}, src="host")
        h.record("account.limits", None, {"status": "rejected", "type": "five_hour",
                                          "windows": {"five_hour": {"used": 1, "resets": resets}}}, src="host")
        h.record("turn.result", name, {"is_error": True, "text": LIMIT_TEXT, "turns": 2}, src="host")

    def types(self, h):
        from core import journal
        import os
        return [e for e in journal.read_tail(os.path.join(h.state_dir, "journal.jsonl")) if e["type"] == "session.limit_reset"]

    def test_the_step_writes_the_fact_once_after_the_reset_and_the_session_continues(self):
        h = self.hub()
        self.cut(h, "maps", 2000.0)
        self.assertTrue(h.state["sessions"]["maps"]["limit_cut"])
        h.resume_cut()
        self.assertEqual(self.types(h), [])
        self.now[0] = 2000.0 + limitresume.RESET_GRACE
        h.resume_cut()
        h.resume_cut()
        (e,) = self.types(h)
        self.assertEqual((e["entity"], e["data"]), ("maps", {"channel": "core", "resets": 2000.0}))
        self.assertNotIn("limit_cut", h.state["sessions"]["maps"])
        self.assertEqual(sum("window has reset" in m["text"] for m in h.state["messages"].values()), 1)

    def test_a_hub_with_many_cut_sessions_raises_a_few_per_pass(self):
        h = self.hub()
        for i in range(6):
            self.cut(h, f"s{i}", 2000.0)
        self.now[0] = 2100.0
        h.resume_cut()
        self.assertEqual(len(self.types(h)), limitresume.STEP)
        h.resume_cut()   # the next tick: no pass before EVERY (a tick is a fraction of a second)
        self.assertEqual(len(self.types(h)), limitresume.STEP)
        self.now[0] += limitresume.EVERY
        h.resume_cut()
        self.assertEqual(len(self.types(h)), 6)

    def test_the_step_runs_after_the_budget_and_before_the_queue_of_executors(self):
        src = inspect.getsource(Hub.tick)
        self.assertLess(src.index("self.budget_mode"), src.index("self.resume_cut"))
        self.assertLess(src.index("self.resume_cut"), src.index("self.run_tasks"))


class Raised(Folding):
    """A hub with a specialist that raises executors (not a TestCase: the pool's class below does not run these twice)."""

    def setUp(self):
        super().setUp()
        self.before()
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.started", "hub")
        self.ev("session.requested", "frontend", cwd="/fe", session_id="s-fe", role="agent", about="Frontend", **OWNER)
        self.ev("session.started", "frontend")

    def raise_one(self, name, raiser="frontend"):
        self.assertIsNone(self.ev("task.run_requested", name, task="fix CI", **by(raiser)))
        for t, e, d in executors.events_for(self.st, name, self.st["runs"][name], cwd="/x", worktree=None):
            self.assertIsNone(self.ev(t, e, **d))
        self.ev("session.started", name)
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == name]
        self.ev("turn.started", name, marker=d["message"])
        return d["id"]

    def cut_and_retire(self, name, did):
        self.limits()
        self.ev("turn.result", name, text=LIMIT_TEXT, is_error=True)
        self.ev("turn.stopped", name)
        self.assertIsNone(self.ev("session.close_requested", name, retire=True))
        self.assertEqual(self.st["sessions"][name]["state"], "retired")
        self.assertEqual(self.st["sessions"][name]["limit_cut"]["for_delegation"], did)
        self.assertEqual(self.st["delegations"][did]["state"], "stopped")

    def before(self):
        """What the hub has before its sessions come up."""

    def cut_and_stop(self, name, did):
        """Cut by the limit, then stopped by the owner: asleep on purpose."""
        self.limits()
        self.ev("turn.result", name, text=LIMIT_TEXT, is_error=True)
        self.ev("turn.stopped", name)
        self.assertIsNone(self.ev("session.stop_requested", name, **OWNER))
        x = self.st["sessions"][name]
        self.assertEqual((x["state"], x["stopped"]["by"], x["limit_cut"]["for_delegation"]), ("retired", "owner", did))

    def woken(self, name, did, text):
        x = self.st["sessions"][name]
        self.assertEqual(x["state"], "starting")
        self.assertNotIn("limit_cut", x)
        (go,) = [m for m in self.st["messages"].values() if m["session"] == name and m["origin"] == reducer.forms.ORIGIN_CORE
                 and m.get("for_delegation") == did]
        self.assertEqual(go["text"], text)
        self.assertEqual(self.st["delegations"][did]["state"], "working")
        self.assertEqual(self.st["delegations"][did]["message"], go["id"])

    def asleep(self, name):
        self.assertEqual(self.st["sessions"][name]["state"], "retired")
        self.assertEqual([m for m in self.st["messages"].values() if m["session"] == name and m.get("for_delegation")
                          and m["origin"] == reducer.forms.ORIGIN_CORE], [])


class Executors(Raised, unittest.TestCase):
    """An executor cut by the limit falls asleep after two minutes idle (retired): the core wakes it with its task."""

    def test_an_executor_the_owner_stopped_after_the_cut_is_left_asleep(self):
        # 11.11: the clock woke it with its task, as one the core had put to sleep - the owner's stop was undone
        did = self.raise_one("frontend-ci")
        self.cut_and_stop("frontend-ci", did)
        self.assertEqual(limitresume.due(self.st, RESETS + 100), [])
        self.ev("session.limit_reset", "frontend-ci", **CORE, resets=RESETS)   # a fact written anyway (an older daemon)
        self.asleep("frontend-ci")
        self.assertEqual(self.st["delegations"][did]["state"], "stopped")
        self.assertIsNone(self.ev("session.resume_requested", "frontend-ci", ts=AFTER, **OWNER))   # his "Continue" does
        self.woken("frontend-ci", did, reducer.LIMIT_RESET)

    def test_continue_before_the_reset_on_a_stopped_executor_does_not_promise_it_goes_on_by_itself(self):
        did = self.raise_one("frontend-ci")
        self.cut_and_stop("frontend-ci", did)
        why = self.ev("session.resume_requested", "frontend-ci", ts=RESETS - 60, **OWNER)
        self.assertEqual(reducer.forms.code_of(why), "not_yet")
        self.assertIn("does not continue by itself", why)
        self.assertNotIn("continues by itself", why)

    def test_a_retired_executor_cut_by_the_limit_is_woken_with_its_task(self):
        did = self.raise_one("frontend-ci")
        self.cut_and_retire("frontend-ci", did)
        self.assertIsNone(self.ev("session.limit_reset", "frontend-ci", **CORE, resets=RESETS))
        x = self.st["sessions"]["frontend-ci"]
        self.assertEqual(x["state"], "starting")
        self.assertNotIn("limit_cut", x)
        (go,) = [m for m in self.st["messages"].values() if m["session"] == "frontend-ci" and "window has reset" in m["text"]]
        self.assertEqual((go["origin"], go.get("for_delegation")), (reducer.forms.ORIGIN_CORE, did))
        self.assertEqual(self.st["delegations"][did]["state"], "working")
        self.assertEqual(self.st["delegations"][did]["message"], go["id"])

    def test_the_owners_continue_wakes_a_retired_executor_with_its_task(self):
        did = self.raise_one("frontend-ci")
        self.cut_and_retire("frontend-ci", did)
        self.assertIsNone(self.ev("session.resume_requested", "frontend-ci", ts=AFTER, **OWNER))
        self.assertEqual(self.st["sessions"]["frontend-ci"]["state"], "starting")
        (go,) = [m for m in self.st["messages"].values() if m["session"] == "frontend-ci" and "its window has reset" in m["text"]]
        self.assertEqual(go.get("for_delegation"), did)
        self.assertEqual(self.st["delegations"][did]["state"], "working")

    def test_an_executor_whose_task_was_accepted_meanwhile_is_left_asleep(self):
        did = self.raise_one("frontend-ci")
        self.cut_and_retire("frontend-ci", did)
        self.st["delegations"][did]["state"] = "done"
        self.ev("session.limit_reset", "frontend-ci", **CORE, resets=RESETS)
        self.assertEqual(self.st["sessions"]["frontend-ci"]["state"], "retired")
        self.assertEqual(sum("window has reset" in m["text"] for m in self.st["messages"].values()), 0)

    def test_it_is_woken_once_however_many_facts_come(self):
        did = self.raise_one("frontend-ci")
        self.cut_and_retire("frontend-ci", did)
        self.ev("session.limit_reset", "frontend-ci", **CORE, resets=RESETS)
        self.ev("session.limit_reset", "frontend-ci", **CORE, resets=RESETS)
        self.assertEqual(sum("window has reset" in m["text"] for m in self.st["messages"].values()), 1)

    def test_with_the_raisers_places_taken_it_waits_in_the_queue_and_is_woken_when_one_frees_up(self):
        did = self.raise_one("frontend-ci")
        self.cut_and_retire("frontend-ci", did)
        live = [f"frontend-{i}" for i in range(executors.LIMIT_SPECIALIST)]
        for n in live:
            self.raise_one(n)
        self.assertIsNone(self.ev("session.limit_reset", "frontend-ci", **CORE, resets=RESETS))
        self.assertEqual(self.st["sessions"]["frontend-ci"]["state"], "retired")
        run = self.st["runs"]["frontend-ci"]
        self.assertEqual((run["state"], run["by"], run["wake"]["about"]), ("queued", "frontend", did))
        self.assertEqual(sum("window has reset" in m["text"] for m in self.st["messages"].values()), 0)
        self.assertEqual(limitresume.due(self.st, RESETS + 100), [])
        self.assertEqual(executors.promotable(self.st, 1000), [])
        self.st = json.loads(json.dumps(self.st))   # reloaded from state.json meanwhile: plain data, no text objects
        self.ev("turn.result", live[0], text="done")
        self.ev("turn.stopped", live[0])
        self.assertIsNone(self.ev("session.close_requested", live[0], retire=True))
        self.assertEqual(executors.promotable(self.st, 1000), ["frontend-ci"])
        self.assertIsNone(self.ev("task.run_ready", "frontend-ci", **CORE, owner=False))
        self.assertEqual(self.st["sessions"]["frontend-ci"]["state"], "starting")
        self.assertNotIn("frontend-ci", self.st["runs"])
        (go,) = [m for m in self.st["messages"].values() if m["session"] == "frontend-ci" and "window has reset" in m["text"]]
        self.assertEqual((go["origin"], go.get("for_delegation")), (reducer.forms.ORIGIN_CORE, did))
        self.assertEqual(self.st["delegations"][did]["state"], "working")
        self.assertEqual([x[2] for x in go["l10n"]], ["core.msg.limit_reset"])   # the owner reads it in his language

    def test_an_executor_the_core_may_not_wake_is_reported_to_whoever_raised_it(self):
        did = self.raise_one("frontend-ci")
        self.cut_and_retire("frontend-ci", did)
        self.st["owner"]["parallel"] = 0
        self.ev("session.limit_reset", "frontend-ci", **CORE, resets=RESETS)
        x = self.st["sessions"]["frontend-ci"]
        self.assertEqual(x["state"], "retired")
        self.assertNotIn("limit_cut", x)
        self.assertEqual(limitresume.due(self.st, RESETS + 100), [])
        (note,) = [t for t in self.texts("frontend") if "frontend-ci" in t and "limit" in t.lower()]
        self.assertIn(f"hub session wake frontend-ci", note)

    def test_the_note_to_the_raiser_does_not_tell_it_to_continue_by_itself(self):
        # review: _wake_cut took the gate's default and the raiser got a "will continue frontend: hub send frontend" note about itself
        did = self.raise_one("frontend-ci")
        self.cut_and_retire("frontend-ci", did)
        self.st["owner"]["parallel"] = 0
        self.ev("session.limit_reset", "frontend-ci", **CORE, resets=RESETS)
        (note,) = [t for t in self.texts("frontend") if "frontend-ci" in t and "limit" in t.lower()]
        self.assertIn("parallelism 0", note)
        self.assertNotIn("continues", note)
        self.assertNotIn("hub send frontend", note)


class PoolExecutors(Raised, unittest.TestCase):
    """A hub with an account pool: an executor that fell asleep after the cut (the pool waited for the reset or all accounts
    were busy, and the core put it to sleep two minutes idle) continues by itself when the pool has room again - the same
    moment a live cut session continues (account.switched: free on the account it was cut on, or another account) - as on a
    hub without a pool. Before, only live and parked ones continued, and the executor's task stood until someone woke it."""

    def before(self):
        self.ev("account.switched", None, to="main", restart=True, **CORE)

    def test_the_reset_of_the_account_it_was_cut_on_wakes_it_with_its_task(self):
        did = self.raise_one("frontend-ci")
        self.cut_and_retire("frontend-ci", did)
        self.ev("account.waiting", None, account="main", until=RESETS, why="main: limit", **CORE)
        self.asleep("frontend-ci")
        self.ev("account.switched", None, to="main", **{"from": "main"}, free=True, reset=True, **CORE)
        self.woken("frontend-ci", did, reducer.LIMIT_RESET)
        self.assertIn("frontend-ci", [a["session"] for a in self.acts if a.get("do") == "spawn"])
        (note,) = [str(a["text"]) for a in self.acts if a.get("do") == "task_note" and a["delegation"] == did]
        self.assertEqual(note, "frontend-ci woken: the limit window has reset")

    def test_another_account_with_room_wakes_it_on_that_one(self):
        did = self.raise_one("frontend-ci")
        self.cut_and_retire("frontend-ci", did)
        self.ev("account.exhausted", None, blocked={"main": 9000}, until=9000, why="all busy", **CORE)
        self.asleep("frontend-ci")
        self.ev("account.switched", None, to="work", **{"from": "main"}, restart=True, **CORE)
        self.woken("frontend-ci", did, reducer.LIMIT_CUT)
        (note,) = [str(a["text"]) for a in self.acts if a.get("do") == "task_note" and a["delegation"] == did]
        self.assertEqual(note, "frontend-ci woken: the account pool has room again")   # no window has reset

    def test_the_owners_continue_on_another_account_says_so_in_the_trail(self):
        # review: the trail followed a flag of the caller, so "Continue" on another account said "the window has reset"
        did = self.raise_one("frontend-ci")
        self.cut_and_retire("frontend-ci", did)
        self.st["account"]["active"] = "work"   # the pool is on another account (a state left by an earlier core)
        self.assertIsNone(self.ev("session.resume_requested", "frontend-ci", ts=AFTER, **OWNER))
        self.woken("frontend-ci", did, reducer.LIMIT_CUT)
        (note,) = [str(a["text"]) for a in self.acts if a.get("do") == "task_note" and a["delegation"] == did]
        self.assertEqual(note, "frontend-ci woken: the account pool has room again")
    def test_it_is_woken_once(self):
        did = self.raise_one("frontend-ci")
        self.cut_and_retire("frontend-ci", did)
        self.ev("account.switched", None, to="main", **{"from": "main"}, free=True, reset=True, **CORE)
        self.ev("account.switched", None, to="work", **{"from": "main"}, restart=True, **CORE)
        self.assertEqual(sum("cut off your turn" in m["text"] for m in self.st["messages"].values()), 1)

    def test_a_pool_without_room_or_the_same_login_without_a_reset_leaves_it_asleep(self):
        did = self.raise_one("frontend-ci")
        self.cut_and_retire("frontend-ci", did)
        self.ev("account.waiting", None, account="main", until=RESETS, **CORE)
        self.ev("account.exhausted", None, blocked={"main": 9000}, until=9000, **CORE)
        self.ev("account.switched", None, to="main", manual=True, channel="cli", owner=True)   # the same one, still busy
        self.asleep("frontend-ci")
        self.assertTrue(self.st["sessions"]["frontend-ci"]["limit_cut"])

    def test_an_executor_the_owner_stopped_after_the_cut_is_left_asleep(self):
        did = self.raise_one("frontend-ci")
        self.cut_and_stop("frontend-ci", did)
        self.ev("account.switched", None, to="main", **{"from": "main"}, free=True, reset=True, **CORE)
        self.ev("account.switched", None, to="work", **{"from": "main"}, restart=True, **CORE)
        self.asleep("frontend-ci")

    def test_with_the_raisers_places_taken_it_waits_in_the_queue(self):
        did = self.raise_one("frontend-ci")
        self.cut_and_retire("frontend-ci", did)
        for i in range(executors.LIMIT_SPECIALIST):
            self.raise_one(f"frontend-{i}")
        self.ev("account.switched", None, to="main", **{"from": "main"}, free=True, reset=True, **CORE)
        self.asleep("frontend-ci")
        run = self.st["runs"]["frontend-ci"]
        self.assertEqual((run["state"], run["by"], run["wake"]["about"]), ("queued", "frontend", did))
        self.ev("account.switched", None, to="work", **{"from": "main"}, restart=True, **CORE)   # moved while it waited
        self.ev("turn.result", "frontend-0", text="done")
        self.ev("turn.stopped", "frontend-0")
        self.assertIsNone(self.ev("session.close_requested", "frontend-0", retire=True))
        self.assertEqual(executors.promotable(self.st, 1000), ["frontend-ci"])
        self.assertIsNone(self.ev("task.run_ready", "frontend-ci", **CORE, owner=False))
        self.woken("frontend-ci", did, reducer.LIMIT_CUT)   # the words of the pool as it is now: another account

    def test_an_executor_the_core_may_not_wake_is_reported_to_whoever_raised_it(self):
        did = self.raise_one("frontend-ci")
        self.cut_and_retire("frontend-ci", did)
        self.st["owner"]["parallel"] = 0
        self.ev("account.switched", None, to="work", **{"from": "main"}, restart=True, **CORE)
        self.asleep("frontend-ci")
        self.assertNotIn("limit_cut", self.st["sessions"]["frontend-ci"])
        (note,) = [t for t in self.texts("frontend") if "frontend-ci" in t and "limit" in t.lower()]
        self.assertIn("hub session wake frontend-ci", note)
        self.assertIn("The account pool has room again", note)   # another account: no window has reset
