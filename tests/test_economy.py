"""Limit economy mode (mini-hub spec, section 4; mini-hubs, slice 4) — only when the hub has at least one mini-hub
(parallelism N >= 1 and a session that became a mini-hub by the session.folder fact).

The pool reserve is the lowest fill level (the larger of the 5 h and week windows; a window after its reset counts as
0) among accounts that are not busy until reset. From 85% — economy: an executor starts at once only for a task from
the owner's live word (not through a schedule) and for an urgent one, the rest wait (including a rework by an
executor of a mini-hub that already handed in); `--work normal` — on the cheap pair. At the switch threshold of the
whole pool — an executor only on the owner's explicit word. The core writes the mode as the budget.mode fact; when the
window resets, the queue rises by itself. Without mini-hubs — as before: the active account's week from 85% — on the
owner's word."""
import tests  # noqa: F401 — keep the machine's settings out
import json
import os
import tempfile
import unittest
from unittest import mock

from core import budget, executors, minihub, modelrule, reducer, views
from tests.test_minihub import OWNER, Delegating, by

CORE = {"channel": "core", "owner": False}
FAR = 10 ** 10   # window reset is far ahead


def seen(five=0.1, week=0.1, five_at=FAR, week_at=FAR, status="allowed"):
    return {"status": status, "windows": {"five_hour": {"used": five, "resets": five_at},
                                          "seven_day": {"used": week, "resets": week_at}}}


def state(accounts=None, limits=None, blocked=None, mode=None):
    st = reducer.empty()
    st["account"] = {"seen": accounts or {}, "blocked": blocked or {}}
    st["limits"] = limits or {}
    if mode:
        st["budget"] = {"mode": mode}
    return st


class Mode(unittest.TestCase):
    """budget.decide is a pure function: pool reserve and threshold."""

    def test_unknown_limits_are_normal(self):
        self.assertEqual(budget.decide(state(), None, 0.95, 1000)["mode"], "normal")

    def test_a_single_login_by_the_larger_of_its_two_windows(self):
        out = budget.decide(state(limits=seen(five=0.87, week=0.3)), None, 0.95, 1000)
        self.assertEqual((out["mode"], out["window"]), ("economy", "five_hour"))
        out = budget.decide(state(limits=seen(five=0.2, week=0.86)), None, 0.95, 1000)
        self.assertEqual((out["mode"], out["window"]), ("economy", "seven_day"))
        self.assertEqual(budget.decide(state(limits=seen(five=0.84)), None, 0.95, 1000)["mode"], "normal")

    def test_a_window_past_its_reset_is_zero(self):
        st = state(limits=seen(five=0.9, five_at=5000))
        self.assertEqual(budget.decide(st, None, 0.95, 4999)["mode"], "economy")
        self.assertEqual(budget.decide(st, None, 0.95, 5000)["mode"], "normal")
        # one window's reset does not end the mode while the other still holds it
        st = state(limits=seen(five=0.9, five_at=5000, week=0.9))
        self.assertEqual(budget.decide(st, None, 0.95, 6000)["mode"], "economy")

    def test_a_free_account_below_the_threshold_means_no_economy(self):
        st = state({"a": seen(five=0.9), "b": seen(five=0.3)})
        self.assertEqual(budget.decide(st, ["a", "b"], 0.95, 1000)["mode"], "normal")
        st = state({"a": seen(five=0.9), "b": seen(week=0.88)})
        out = budget.decide(st, ["a", "b"], 0.95, 1000)
        self.assertEqual((out["mode"], out["account"], out["window"]), ("economy", "b", "seven_day"))
        # an account with no limits seen is at 0%: nothing to save
        self.assertEqual(budget.decide(st, ["a", "b", "c"], 0.95, 1000)["mode"], "normal")

    def test_all_at_the_switch_threshold_or_blocked_only_the_owners_word(self):
        st = state({"a": seen(five=0.97), "b": seen(week=0.95)})
        self.assertEqual(budget.decide(st, ["a", "b"], 0.95, 1000)["mode"], "owner")
        st = state({"a": seen(five=1, status="rejected"), "b": seen(five=0.5)}, blocked={"a": 5000, "b": 4000})
        self.assertEqual(budget.decide(st, ["a", "b"], 0.95, 1000)["mode"], "owner")
        out = budget.decide(st, ["a", "b"], 0.95, 4500)                         # "b" is free again — its 50% counts
        self.assertEqual(out["mode"], "normal")

    def test_exit_below_five_points_under_the_threshold(self):
        st = state(limits=seen(five=0.82))
        self.assertEqual(budget.decide(st, None, 0.95, 1000)["mode"], "normal")
        st["budget"] = {"mode": "economy"}
        self.assertEqual(budget.decide(st, None, 0.95, 1000)["mode"], "economy")    # no flapping at the boundary
        st["limits"] = seen(five=0.79)
        self.assertEqual(budget.decide(st, None, 0.95, 1000)["mode"], "normal")

    def test_economy_threshold_stays_below_the_switch_threshold(self):
        st = state(limits=seen(five=0.65))
        self.assertEqual(budget.decide(st, None, 0.70, 1000)["mode"], "economy")    # E = S − 0,10 = 0,60
        st["limits"] = seen(five=0.70)
        self.assertEqual(budget.decide(st, None, 0.70, 1000)["mode"], "owner")
        self.assertEqual(budget.thresholds(0.9, 0.5), (0.4, 0.5))                   # not below 0.40
        self.assertEqual(budget.thresholds(None, 0.95), (0.85, 0.95))

    def test_the_window_reset_comes_with_the_mode(self):
        out = budget.decide(state({"a": seen(five=0.9, five_at=5000)}), ["a"], 0.95, 1000)
        self.assertEqual((out["window"], out["resets"]), ("five_hour", 5000))

    def test_a_gateway_account_is_not_a_reserve(self):
        # conduit is a gateway over several subscriptions that others use too; its share is the subscriptions of the
        # current token, and at the limit the core takes a fresh one (accounts: refresh) — do not turn economy on for it
        st = state({"login": seen(five=0.97), "conduit": seen(five=0.85)})
        out = budget.decide(st, ["login", "conduit"], 0.95, 1000, renewable={"conduit"})
        self.assertEqual(out["mode"], "normal")
        self.assertIn("conduit", out["why"])
        # the gateway is busy until reset (gave no fresh one) — like any busy account
        st["account"]["blocked"] = {"conduit": 5000}
        self.assertEqual(budget.decide(st, ["login", "conduit"], 0.95, 1000, renewable={"conduit"})["mode"], "owner")
        # an ordinary account next to it counts as before
        st = state({"a": seen(five=0.88), "conduit": seen(five=0.3)}, blocked={"conduit": 5000})
        out = budget.decide(st, ["a", "conduit"], 0.95, 1000, renewable={"conduit"})
        self.assertEqual((out["mode"], out["account"]), ("economy", "a"))

    def test_until_is_when_the_reserve_frees(self):
        st = state({"a": seen(five=0.9, five_at=5000, week=0.2), "b": seen(five=0.88, five_at=7000)})
        out = budget.decide(st, ["a", "b"], 0.95, 1000)
        self.assertEqual((out["mode"], out["until"]), ("economy", 5000))
        self.assertIn("88%", out["why"])


class Core(Delegating):
    """Helpers (no tests — subclasses do not inherit any): notes is a mini-hub, api-x is a specialist that has not
    switched yet (its folder was not checked: works the old way), docs is an ordinary session."""

    def eco(self, n=3):
        """Parallelism n; only notes became a mini-hub (the daemon's fact about its folder)."""
        self.parallel(n, clean=False)
        live = self.live()
        self.actions.clear()
        self.assertIsNone(self.folder("notes"))
        self.respawned()
        self.unpark(live)
        self.assertTrue(minihub.on(self.st, self.st["sessions"]["notes"]))
        self.assertFalse(minihub.on(self.st, self.st["sessions"]["api-x"]))

    def mode(self, mode, **extra):
        return self.ev("budget.mode", None, mode=mode, why=extra.pop("why", "5h window 90%"), until=FAR, **extra,
                       **CORE)

    def hub_texts(self):
        return [m["text"] for m in self.st["messages"].values() if m["session"] == "hub"]

    def owner_turn(self, name="notes", text="fix the CI"):
        """A turn on the owner's live word: the owner's message to the session and its turn."""
        self.ev("message.received", None, to=name, text=text, **OWNER)
        mid = max((m for m in self.st["messages"].values() if m.get("from_owner")), key=lambda m: m["created"])["id"]
        self.ev("turn.started", name, marker=mid)
        return mid

    def session_task_turn(self, name="notes", sender="docs"):
        """A turn on another session's task (not on the owner's word)."""
        self.ev("delegation.created", None, session=name, for_session=sender, context="check the docs", **by(sender))
        d = max(self.st["delegations"].values(), key=lambda d: int(d["id"][1:]))
        self.ev("turn.started", name, marker=d["message"])
        return d

    def end_turn(self, name="notes"):
        self.ev("turn.result", name, text="ok")
        self.ev("turn.stopped", name)

    def snapshot(self):
        r = views.Reader()
        r.state = self.st
        return r.snapshot(now=self.events[-1]["ts"])


class WithoutMinihubs(Core):
    def test_the_old_rule_stays(self):
        # a journal from before parallelism: the active account's week from 85%; the 5 h window sets no threshold
        self.ev("account.limits", None, windows={"five_hour": {"used": 0.97, "resets": FAR}})
        self.assertIsNone(self.run_task("notes-a"))
        self.assertEqual(self.state_of("notes-a"), "ready")
        self.ev("account.limits", None, windows={"seven_day": {"used": 0.9, "resets": FAR}})
        self.assertIsNone(self.run_task("notes-b"))
        self.assertIn("85", self.st["runs"]["notes-b"]["why"])

    def test_a_stale_economy_fact_changes_nothing(self):
        self.mode("economy")
        self.assertIsNone(self.run_task("notes-a"))
        self.assertEqual(self.state_of("notes-a"), "ready")
        self.assertEqual(self.hub_texts(), [])                     # no card: the mode is not in effect

    def test_parallel_without_a_minihub_yet_is_the_old_rule(self):
        # parallelism is set, but no session has switched yet (folder not checked, task in progress) — as before
        self.parallel(3, clean=False)
        self.assertIsNone(budget.mode(self.st))
        self.mode("economy")
        self.assertIsNone(self.run_task("notes-a"))
        self.assertEqual(self.state_of("notes-a"), "ready")
        self.assertEqual(self.hub_texts(), [])

    def test_parallel_zero_has_no_minihubs(self):
        self.eco()
        self.assertEqual(budget.mode(self.st), "normal")
        self.parallel(0, clean=False)
        self.assertIsNone(budget.mode(self.st))

    def test_a_removed_minihub_is_no_minihub(self):
        self.eco()
        self.assertEqual(budget.mode(self.st), "normal")
        self.ev("session.ended", "notes", reason="prompt_input_exit")
        self.assertIsNone(self.ev("session.remove_requested", "notes", **OWNER))
        self.assertEqual(self.st["sessions"]["notes"]["state"], "removed")
        self.assertIsNone(budget.mode(self.st))                    # economy is not held by a removed session
        self.mode("economy")
        self.assertIsNone(self.run_task("api-x-a", who="api-x"))
        self.assertEqual(self.state_of("api-x-a"), "ready")

    def test_no_marks_without_minihubs(self):
        self.ev("schedule.set", "j", kind="every", every=3600, start=1000, session="notes", prompt="watch the mail",
                **OWNER)
        self.ev("schedule.due", "j", slot=1001, **CORE)
        fired = next(m for m in self.st["messages"].values() if m.get("schedule") == "j")
        self.ev("turn.started", "notes", marker=fired["id"])
        self.ev("delegation.created", None, session="docs", context="T", **by("notes"))
        self.assertFalse(any("via_schedule" in x for x in self.st["delegations"].values()))
        self.assertNotIn("turn_sched", self.st["sessions"]["notes"])

    def test_no_budget_in_the_snapshot(self):
        self.mode("economy")
        self.assertIsNone(self.snapshot()["budget"])


class Economy(Core):
    def setUp(self):
        super().setUp()
        self.eco()
        self.mode("economy")

    def test_a_task_of_another_session_waits(self):
        self.session_task_turn()
        self.assertIsNone(self.run_task("notes-a"))
        self.assertEqual(self.state_of("notes-a"), "queued")
        self.assertIn("limit economy", self.st["runs"]["notes-a"]["why"])
        self.assertEqual(executors.promotable(self.st), [])        # will not rise by itself while the mode holds

    def test_a_queue_pass_takes_the_mode_once_not_once_per_request(self):
        # the mode is a walk of the sessions up to the first mini-hub; a queue pass on every daemon tick (promotable,
        # recount) takes it once for the pass, not once more for every queued request under the economy
        self.session_task_turn()
        for i in range(10):
            self.assertIsNone(self.run_task(f"notes-q{i}"))
        queued = [n for n, r in self.st["runs"].items() if r["state"] == "queued"]
        self.assertEqual(len(queued), 10)
        with mock.patch.object(budget, "mode", wraps=budget.mode) as mode:
            self.assertEqual(executors.promotable(self.st), [])
            executors.recount(self.st, 2000)
        self.assertEqual(mode.call_count, 2)   # once per pass, and it is taken (zero would fall to the weekly rule)
        self.assertTrue(all("limit economy" in self.st["runs"][n]["why"] for n in queued), queued)

    def test_the_owners_live_word_rises_at_once(self):
        self.owner_turn()
        self.assertIsNone(self.run_task("notes-a"))
        self.assertEqual(self.state_of("notes-a"), "ready")

    def test_the_owners_task_through_the_hub_rises_at_once(self):
        mid = self.owner_turn("hub", "let notes fix the CI")
        self.ev("delegation.created", None, session="notes", **{"for": mid}, context="fix the CI", **by("hub"))
        self.end_turn("hub")
        d = max(self.st["delegations"].values(), key=lambda d: int(d["id"][1:]))
        self.ev("turn.started", "notes", marker=d["message"])
        self.assertIsNone(self.run_task("notes-a"))
        self.assertEqual(self.state_of("notes-a"), "ready")

    def test_an_urgent_task_rises_at_once(self):
        self.ev("delegation.created", None, session="notes", context="prod is down", now=True, **by("hub"))
        d = max(self.st["delegations"].values(), key=lambda d: int(d["id"][1:]))
        self.ev("turn.started", "notes", marker=d["message"])
        self.assertIsNone(self.run_task("notes-a"))
        self.assertEqual(self.state_of("notes-a"), "ready")

    def test_a_repeating_schedule_set_by_the_owner_is_not_his_live_word(self):
        mid = self.owner_turn("notes", "watch the mail every hour")
        self.ev("schedule.set", "mail", kind="every", every=3600, start=1000, session="notes", prompt="watch the mail",
                **by("notes"))
        self.end_turn()
        self.assertEqual(self.st["schedule"]["mail"].get("root"), mid)   # the chain is his, the firing is background
        self.ev("schedule.due", "mail", slot=1001, **CORE)
        fired = next(m for m in self.st["messages"].values() if m.get("schedule") == "mail")
        self.ev("turn.started", "notes", marker=fired["id"])
        self.assertIsNone(self.run_task("notes-a"))
        self.assertEqual(self.state_of("notes-a"), "queued")
        # a task created in such a turn carries the mark on: an executor for a busy specialist waits too
        self.ev("delegation.created", None, session="api-x", context="reply in the mail", **by("notes"))
        d = max(self.st["delegations"].values(), key=lambda d: int(d["id"][1:]))
        self.assertTrue(d.get("via_schedule"))
        self.end_turn()
        self.ev("turn.started", "api-x", marker=d["message"])
        self.assertIsNone(self.run_task("api-x-a", who="api-x"))
        self.assertEqual(self.state_of("api-x-a"), "queued")
        # the window reset — the executor is up, but its task is still not a live word: the mark stays on it
        self.mode("normal", why="the window reset")
        self.assertIsNone(self.ev("task.run_ready", "notes-a", **CORE))
        self.start("notes-a")
        mine = next(x for x in self.st["delegations"].values() if x["session"] == "notes-a")
        self.assertTrue(mine.get("via_schedule"))

    def test_the_owners_new_message_mid_schedule_turn_starts_a_live_chain(self):
        self.ev("schedule.set", "mail", kind="every", every=3600, start=1000, session="notes", prompt="watch the mail",
                **OWNER)
        self.ev("schedule.due", "mail", slot=1001, **CORE)
        fired = next(m for m in self.st["messages"].values() if m.get("schedule") == "mail")
        self.ev("turn.started", "notes", marker=fired["id"])
        self.end_turn()
        self.owner_turn()
        self.assertIsNone(self.run_task("notes-a"))
        self.assertEqual(self.state_of("notes-a"), "ready")

    def test_normal_work_rises_on_the_economy_pair_risky_as_is(self):
        self.owner_turn()
        eco = {"model": "sonnet", "effort": "medium"}
        self.assertIsNone(self.run_task("notes-a", model="sonnet", effort="high", work="normal", economy=eco))
        self.assertIsNone(self.run_task("notes-b", model="opus", effort="medium", work="risky"))
        self.assertEqual({k: self.st["runs"]["notes-a"].get(k) for k in ("model", "effort")}, eco)
        self.assertEqual({k: self.st["runs"]["notes-b"].get(k) for k in ("model", "effort")},
                         {"model": "opus", "effort": "medium"})

    def test_the_hub_is_not_told_of_each_waiting_request(self):
        self.session_task_turn()
        before = len(self.hub_texts())
        self.assertIsNone(self.run_task("notes-a"))
        self.assertEqual(len(self.hub_texts()), before)            # what waits for economy is in the card, not one by one

    def test_window_reset_raises_the_queue_in_order_within_caps(self):
        self.session_task_turn()
        for i in range(4):
            self.assertIsNone(self.run_task(f"notes-{i}", work="normal", model="sonnet", effort="high",
                                            economy={"model": "sonnet", "effort": "medium"}))
        self.assertEqual(executors.promotable(self.st), [])
        self.mode("normal", why="the window reset")
        self.assertEqual(executors.promotable(self.st), ["notes-0", "notes-1", "notes-2"])   # parallelism is 3
        self.assertIsNone(self.ev("task.run_ready", "notes-0", **CORE))
        self.assertEqual(self.st["runs"]["notes-0"]["effort"], "high")   # raised outside economy now — its own pair

    def test_the_owners_card_buttons(self):
        self.session_task_turn()
        for i in range(2):
            self.assertIsNone(self.run_task(f"notes-{i}", work="normal", model="sonnet", effort="high",
                                            economy={"model": "sonnet", "effort": "medium"}))
        self.end_turn()
        self.assertIsNotNone(self.ev("task.run_go", "notes-0", queue=True, **by("hub")))   # without the owner's word — no
        q = self.owner_turn("hub", "raise the queue now")
        for name in ("notes-0", "notes-1"):
            self.assertIsNone(self.ev("task.run_go", name, queue=True, **{"for": q}, **by("hub")))
        self.assertEqual((self.state_of("notes-0"), self.state_of("notes-1")), ("ready", "ready"))
        self.assertEqual(self.st["runs"]["notes-0"]["effort"], "medium")   # raised in economy — the cheap pair

    def test_the_hub_routing_a_sessions_task_to_an_executor_waits_too(self):
        # a task of another session for a busy specialist (not a mini-hub yet) goes to the hub to decide; an executor
        # for it waits too
        self.ev("turn.started", "api-x", marker=None)
        self.ev("delegation.created", None, session="api-x", for_session="docs", context="check the docs", **by("docs"))
        d = max(self.st["delegations"].values(), key=lambda d: int(d["id"][1:]))
        self.assertIsNone(self.ev("message.routed", d["id"], route="executor", name="check", work="normal",
                                  model="sonnet", effort="high", economy={"model": "sonnet", "effort": "medium"},
                                  **by("hub")))
        self.assertEqual(self.state_of("api-x-check"), "queued")
        self.assertIn("limit economy", self.st["runs"]["api-x-check"]["why"])
        # the "normal only" button selects by work: for a launch through hub route it is the same as for hub task run
        self.assertEqual(self.st["runs"]["api-x-check"]["work"], "normal")

    def test_a_normal_task_routed_later_rises_on_the_economy_pair(self):
        # hub task new ... --work normal: the pair and the flag live on the task; hub route dN executor without --work
        # takes them from there
        self.ev("turn.started", "api-x", marker=None)
        self.ev("delegation.created", None, session="api-x", for_session="docs", context="check the docs", work="normal",
                model="sonnet", effort="high", economy={"model": "sonnet", "effort": "medium"}, **by("docs"))
        d = max(self.st["delegations"].values(), key=lambda d: int(d["id"][1:]))
        self.assertIsNone(self.ev("message.routed", d["id"], route="executor", name="check", **by("hub")))
        run = self.st["runs"]["api-x-check"]
        self.assertEqual((run["state"], run.get("work"), run.get("economy")),
                         ("queued", "normal", {"model": "sonnet", "effort": "medium"}))
        q = self.owner_turn("hub", "raise the queue now")
        self.assertIsNone(self.ev("task.run_go", "api-x-check", queue=True, **{"for": q}, **by("hub")))
        run = self.st["runs"]["api-x-check"]
        self.assertEqual((run["state"], run["model"], run["effort"]), ("ready", "sonnet", "medium"))
        # the hub's word outranks the task: an own --work on routing sets the pair, not the task's pair
        self.ev("delegation.created", None, session="api-x", for_session="docs", context="more", work="normal",
                model="sonnet", effort="high", economy={"model": "sonnet", "effort": "medium"}, **by("docs"))
        d = max(self.st["delegations"].values(), key=lambda d: int(d["id"][1:]))
        self.assertIsNone(self.ev("message.routed", d["id"], route="executor", name="prod", work="risky",
                                  model="opus", effort="high", **by("hub")))
        self.assertEqual({k: self.st["runs"]["api-x-prod"].get(k) for k in ("work", "economy")},
                         {"work": "risky", "economy": None})

    def test_the_queue_button_keeps_the_caps(self):
        self.parallel(1, clean=False)
        self.session_task_turn()
        for i in range(2):
            self.assertIsNone(self.run_task(f"notes-{i}"))
        self.end_turn()
        q = self.owner_turn("hub", "raise the queue now")
        for name in ("notes-0", "notes-1"):
            self.assertIsNone(self.ev("task.run_go", name, queue=True, **{"for": q}, **by("hub")))
        self.assertEqual((self.state_of("notes-0"), self.state_of("notes-1")), ("ready", "queued"))
        self.assertTrue(self.st["runs"]["notes-1"]["owner_ok"])          # the word is there — it rises once there is room

    def test_the_minihubs_queue_row_says_why(self):
        self.session_task_turn()
        self.assertIsNone(self.run_task("notes-a"))
        (q,) = self.snapshot()["queue"]
        self.assertEqual(q["why"], "limit economy — waits for the window to reset or your word")

    def test_the_line_counts_only_what_the_economy_holds(self):
        # review (high): one waiting only for room (parallelism) is not "in the queue" of economy
        self.parallel(1, clean=False)
        self.owner_turn()
        self.assertIsNone(self.run_task("notes-a"))
        self.assertIsNone(self.run_task("notes-b"))                     # a live word, but no room
        self.assertIn("1 working executors", self.st["runs"]["notes-b"]["why"])
        self.end_turn()
        self.session_task_turn()
        self.assertIsNone(self.run_task("notes-c"))                     # not live — the mode holds it
        self.assertIn("limit economy", self.st["runs"]["notes-c"]["why"])
        self.assertEqual(self.snapshot()["budget"]["queued"], 1)

    def test_a_message_from_a_scheduled_turn_about_a_live_task_is_not_live(self):
        # review (high): hub send B --about dN from a scheduled turn — the message's mark outranks the task
        mid = self.owner_turn("notes", "check the docs")
        self.ev("delegation.created", None, session="api-x", **{"for": mid}, context="check the docs", **by("notes"))
        d = max(self.st["delegations"].values(), key=lambda d: int(d["id"][1:]))
        self.end_turn()
        self.ev("schedule.set", "mail", kind="every", every=3600, start=1000, session="notes", prompt="watch the mail",
                **OWNER)
        self.ev("schedule.due", "mail", slot=1001, **CORE)
        fired = next(m for m in self.st["messages"].values() if m.get("schedule") == "mail")
        self.ev("turn.started", "notes", marker=fired["id"])
        self.ev("message.received", None, to="api-x", text="and this too", about_task=d["id"], **by("notes"))
        note = max((m for m in self.st["messages"].values() if m["session"] == "api-x"), key=lambda m: m["created"])
        self.end_turn()
        self.ev("turn.started", "api-x", marker=note["id"])
        self.assertIsNone(self.run_task("api-x-a", who="api-x"))
        self.assertEqual(self.state_of("api-x-a"), "queued")

    def test_the_snapshot_carries_the_mode(self):
        self.session_task_turn()
        self.assertIsNone(self.run_task("notes-a"))
        b = self.snapshot()["budget"]
        self.assertEqual((b["mode"], b["why"], b["until"], b["queued"]), ("economy", "5h window 90%", FAR, 1))
        self.mode("normal", why="the window reset")
        self.assertIsNone(self.snapshot()["budget"])


class Chain(unittest.TestCase):
    def test_relayed_owners_words_from_a_scheduled_turn_are_not_live(self):
        # review (medium): hub send --for mN from a scheduled turn — the message's mark outranks "the owner's words"
        from core import chain
        st = reducer.empty()
        st["messages"] = {"m1": {"id": "m1", "from_owner": True}, "m2": {"id": "m2", "re": "m1"},
                          "m3": {"id": "m3", "re": "m1", "via_schedule": True}}
        self.assertEqual([chain.scheduled(st, m) for m in ("m1", "m2", "m3")], [False, False, True])


class Rework(Core):
    """A rework by a mini-hub executor that already handed in (hub session wake ... --about dN) follows the same economy
    rules: a task not from the live word goes to the queue with no line to the hub; from the live word — at once."""

    def asleep(self, live=False):
        """notes delegated a task to the executor notes-ci, which handed in and went to sleep; notes is in a turn on the
        same task again."""
        self.eco()
        if live:
            mid = self.owner_turn()
            d = None
        else:
            d = self.session_task_turn()
        self.assertIsNone(self.run_task("notes-ci"))
        self.end_turn()
        self.start("notes-ci")
        (sub,) = [x for x in self.st["delegations"].values() if x["session"] == "notes-ci"]
        self.ev("turn.started", "notes-ci", marker=sub["message"])
        self.ev("turn.result", "notes-ci", text="done")
        self.ev("turn.stopped", "notes-ci")
        report = f"m{self.seq - 1}x1"
        self.st["sessions"]["notes-ci"].update(state="retired")
        self.ev("turn.started", "notes", marker=report)
        return sub, d if d else mid

    def wake(self, sub, text="finish the test"):
        return self.ev("message.received", None, to="notes-ci", text=text, wake=True, about=sub["id"], **by("notes"))

    def test_a_rework_not_by_the_owners_word_waits_without_a_line_to_the_hub(self):
        sub, _ = self.asleep()
        self.mode("economy")
        before = len(self.hub_texts())
        self.assertIsNone(self.wake(sub))
        run = self.st["runs"]["notes-ci"]
        self.assertEqual((run["state"], run["wake"]["about"]), ("queued", sub["id"]))
        self.assertIn("limit economy", run["why"])
        self.assertEqual(len(self.hub_texts()), before)            # what waits for economy is in the card, not one by one
        self.assertEqual(executors.promotable(self.st), [])
        self.mode("normal", why="the window reset")
        self.assertEqual(executors.promotable(self.st), ["notes-ci"])
        mark = self.seq
        self.assertIsNone(self.ev("task.run_ready", "notes-ci", **CORE))
        self.assertEqual([m["text"] for m in self.to("notes-ci", mark)], ["finish the test"])

    def test_a_rework_of_the_owners_task_wakes_at_once(self):
        sub, _ = self.asleep(live=True)
        self.mode("economy")
        mark = self.seq
        self.assertIsNone(self.wake(sub))
        self.assertNotIn("notes-ci", self.st.get("runs") or {})
        self.assertEqual([m["text"] for m in self.to("notes-ci", mark)], ["finish the test"])

    def test_at_the_pool_threshold_even_the_owners_task_waits_for_his_word(self):
        sub, _ = self.asleep(live=True)
        self.mode("owner", why="all pool accounts at 95%")
        self.assertIsNone(self.wake(sub))
        self.assertEqual(self.st["runs"]["notes-ci"]["state"], "queued")
        self.assertIn("explicit word", self.st["runs"]["notes-ci"]["why"])
        (q,) = self.snapshot()["queue"]
        self.assertEqual(q["why"], "pool limit — waits for your word")


class OwnerOnly(Core):
    def setUp(self):
        super().setUp()
        self.eco()
        self.mode("owner", why="all pool accounts at 95%")

    def test_even_the_live_word_waits_for_his_explicit_word(self):
        self.owner_turn()
        self.assertIsNone(self.run_task("notes-a"))
        self.assertEqual(self.state_of("notes-a"), "queued")
        self.ev("message.received", None, to="hub", text="raise notes-a", **OWNER)
        mid = max((m for m in self.st["messages"].values() if m.get("from_owner")), key=lambda m: m["created"])["id"]
        self.assertIsNone(self.ev("task.run_go", "notes-a", **{"for": mid}, **by("hub")))
        self.assertEqual(self.state_of("notes-a"), "ready")
        self.assertIsNone(self.ev("task.run_requested", "mine", task="x", **OWNER))   # the owner himself — at once
        self.assertEqual(self.state_of("mine"), "ready")


class Card(Core):
    def setUp(self):
        super().setUp()
        self.eco()

    def test_one_card_on_entry_and_one_more_at_the_pool_threshold(self):
        self.mode("economy", window="five_hour", account="work", used=0.87, eta=FAR - 1)
        self.assertEqual(len(self.hub_texts()), 1)
        self.mode("economy", window="seven_day", account="work", used=0.88)   # the same mode, a new window — no card
        self.assertEqual(len(self.hub_texts()), 1)
        text = self.hub_texts()[0]
        for part in ("Limit economy", "card", "hub task go --queue", "forecast", "work"):
            self.assertIn(part, text)
        self.mode("owner", why="all pool accounts at 95%")
        self.assertEqual(len(self.hub_texts()), 2)
        self.assertIn("the owner's explicit word", self.hub_texts()[1])
        self.mode("economy")                                             # down from "owner's word only" — no card
        self.assertEqual(len(self.hub_texts()), 2)
        self.mode("normal", why="the window reset")
        self.assertEqual(len(self.hub_texts()), 3)
        self.assertNotIn("one card", self.hub_texts()[2])

    def test_the_last_minihub_off_still_withdraws_the_card(self):
        self.mode("economy")
        self.assertEqual(len(self.hub_texts()), 1)
        self.solo("notes")                                               # "solo" — no mini-hubs left
        self.assertIsNone(budget.mode(self.st))
        self.mode("normal", why="no mini-hubs")
        self.assertEqual(len(self.hub_texts()), 2)
        self.assertIn("withdraw it", self.hub_texts()[1])
        self.assertIn("the earlier rule", self.hub_texts()[1])           # review (high): the queue will not rise by itself

    def test_no_forecast_past_the_window_reset(self):
        self.mode("economy", window="five_hour", used=0.87, resets=5000, eta=6000)
        self.assertNotIn("forecast", self.hub_texts()[0])
        self.mode("normal")
        self.mode("economy", window="five_hour", used=0.87, resets=5000, eta=4000)
        self.assertIn("forecast", self.hub_texts()[-1])

    def test_only_the_core_writes_the_mode(self):
        self.assertIsNotNone(self.ev("budget.mode", None, mode="economy", **by("hub")))
        self.assertIsNotNone(self.ev("budget.mode", None, mode="soon", **CORE))


class Pair(unittest.TestCase):
    def test_economy_pair_by_default_and_from_settings(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_BASE_URL": "", "ANTHROPIC_MODEL": ""}):
            self.assertEqual(modelrule.economy({}), {"model": "sonnet", "effort": "medium"})
            cfg = {"executors": {"economy": {"model": "haiku", "effort": "low"}}}
            self.assertEqual(modelrule.economy(cfg), {"model": "haiku", "effort": "low"})
            # normal is no costlier than economy — economy stays
            self.assertIsNone(modelrule.economy({"executors": {"normal": {"model": "haiku", "effort": "high"}}}))

    def test_no_downgrade_on_an_own_endpoint(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_BASE_URL": "https://llm.example.com"}):
            self.assertIsNone(modelrule.economy({}))

    def test_the_pair_and_the_threshold_are_settings(self):
        from modules.web import settings
        for key in ("executors.economy.model", "executors.economy.effort", "budget.economy_at"):
            self.assertIn(key, settings.KEYS)
        self.assertEqual(settings.validate("budget.economy_at", 0.8), (0.8, None))
        self.assertIsNone(settings.validate("budget.economy_at", 1.5)[0])


class Cli(unittest.TestCase):
    """hub task run puts the economy pair into the request; hub task go --queue gives the owner's word to the waiting ones
    in order."""

    def setUp(self):
        from core import journal
        self.state = tempfile.mkdtemp(prefix="eco-cli-")
        self.cfg = os.path.join(self.state, "hub.json")
        with open(self.cfg, "w") as f:
            json.dump({}, f)
        self.j = journal.Journal(os.path.join(self.state, "journal.jsonl"))

    def hub(self, *args, me="notes", **env_extra):
        import subprocess
        import sys
        from core import spool
        env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_CONFIG": self.cfg, "HUB_CLI_WAIT": "0"}
        for k in ("HUB_SESSION", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL"):
            env.pop(k, None)
        env.update(env_extra)
        if me:
            env["HUB_SESSION"] = me
        r = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                         "bin", "hub"), *args], env=env, capture_output=True, text=True)
        return r, [ev for _, ev in spool.take(os.path.join(self.state, "spool"))]

    def test_run_carries_the_economy_pair_only_for_normal_work(self):
        data = self.hub("task", "run", "x", "summary", "--work", "normal")[1][-1]["data"]
        self.assertEqual((data["work"], data["economy"]), ("normal", {"model": "sonnet", "effort": "medium"}))
        self.assertNotIn("economy", self.hub("task", "run", "y", "prod", "--work", "risky")[1][-1]["data"])
        data = self.hub("task", "run", "z", "summary", "--work", "normal", ANTHROPIC_BASE_URL="https://llm.example.com")[1]
        self.assertNotIn("economy", data[-1]["data"])                       # own endpoint — no downgrade

    def test_queue_button_gives_the_owners_word_to_the_waiting_in_order(self):
        self.j.append("session.requested", "notes", {"cwd": "/n", "session_id": "s-n", "role": "agent", **OWNER})
        self.j.append("session.started", "notes", {})
        self.j.append("owner.settings", None, {"timezone": "UTC", "parallel": 3, **CORE})
        self.j.append("session.folder", "notes", {"git": "ok", "dirty": 0, **CORE}, src="core")
        self.j.append("budget.mode", None, {"mode": "economy", "why": "5h window 90%", **CORE})
        for name, work in (("notes-b", "risky"), ("notes-a", "normal")):
            self.j.append("task.run_requested", name, {"task": "x", "work": work, **by("notes")})
        r, evs = self.hub("task", "go", "--queue", "normal", "--for", "q7", me="hub")
        self.assertEqual([(e["type"], e["entity"], e["data"]["queue"], e["data"]["for"]) for e in evs],
                         [("task.run_go", "notes-a", True, "q7")])
        r, evs = self.hub("task", "go", "--queue", "--for", "q7", me="hub")
        self.assertEqual([e["entity"] for e in evs][-2:], ["notes-b", "notes-a"])   # the inbox is not cleared — past ones too
        r, _ = self.hub("task", "queue", me="hub")
        self.assertIn("limit economy", r.stdout)                           # a session reads the waits in English

    def test_the_owner_reads_why_a_request_waits_in_his_language(self):
        # the wait reason and the budget's reason in it are the core's phrases: the journal and the state hold the
        # English with their marks (why_l10n); a session's `hub task queue` prints the English, the owner's terminal his
        # language — as before the English texts
        from core import coretext, i18n
        self.j.append("session.requested", "notes", {"cwd": "/n", "session_id": "s-n", "role": "agent", **OWNER})
        self.j.append("session.started", "notes", {})
        self.j.append("owner.settings", None, {"timezone": "UTC", "parallel": 3, **CORE})
        self.j.append("session.folder", "notes", {"git": "ok", "dirty": 0, **CORE}, src="core")
        why = budget.WHY_ECONOMY_WINDOW.format(pct=90, window=budget._window_text("five_hour"))
        self.j.append("budget.mode", None, json.loads(json.dumps(coretext.put({"mode": "economy", **CORE}, "why", why))))
        self.j.append("task.run_requested", "notes-a", {"task": "x", **by("notes")})
        r, _ = self.hub("task", "queue", me="hub")
        self.assertIn("limit economy (window 5 h 90%)", r.stdout)
        r, _ = self.hub("task", "queue", me=None, HUB_LANG="ru")   # a Russian owner's terminal
        ru = i18n.tr("ru", "core.budget.wait_economy", why=i18n.tr(
            "ru", "core.budget.why_economy_window", pct=90, window=i18n.tr("ru", "core.budget.window_five_hour")))
        self.assertIn(ru, r.stdout)
        self.assertIn(i18n.tr("ru", "cli.task.queue_economy", why=i18n.tr(
            "ru", "core.budget.why_economy_window", pct=90, window=i18n.tr("ru", "core.budget.window_five_hour"))),
            r.stdout)                                                      # the mode's line above the queue too
        self.assertNotIn("limit economy", r.stdout)


class Daemon(unittest.TestCase):
    """The daemon writes budget.mode when the mode changes — only when mini-hubs exist."""

    def setUp(self):
        from core.daemon import Hub
        self.h = Hub(tempfile.mkdtemp(prefix="eco-d-"), type("H", (), {"alive": lambda s, n: True,
                                                                        "poll": lambda s: []})(),
                     registry_dir=tempfile.mkdtemp())
        self.h.execute = lambda a: None
        self.h.load_catalog()   # hub settings as the owner.settings fact: parallelism defaults to 3
        self.h.record("session.requested", "notes", {"cwd": "/n", "session_id": "s-n", "role": "agent", **OWNER})
        self.h.record("session.started", "notes", {})

    def modes(self):
        from core import journal
        return [e["data"]["mode"] for e in journal.read_tail(os.path.join(self.h.state_dir, "journal.jsonl"))
                if e["type"] == "budget.mode"]

    def test_mode_is_written_on_change_and_only_with_minihubs(self):
        self.h.record("account.limits", None, {"windows": {"five_hour": {"used": 0.9, "resets": self.h.clock() + 3600}}},
                      src="host")
        self.h.budget_mode()
        self.assertEqual(self.modes(), [])                               # no mini-hubs — as before
        self.h.record("session.folder", "notes", {"git": "ok", "dirty": 0, "channel": "core"})
        self.assertTrue(minihub.on(self.h.state, self.h.state["sessions"]["notes"]))
        self.h.budget_mode()
        self.h.budget_mode()
        self.assertEqual(self.modes(), ["economy"])                      # once — on change
        self.h.record("account.limits", None, {"windows": {"five_hour": {"used": 0.1, "resets": self.h.clock() + 3600}}},
                      src="host")
        self.h.budget_mode()
        self.assertEqual(self.modes(), ["economy", "normal"])

    def test_a_new_window_in_the_same_mode_is_written_too(self):
        # review (high): the 5 h window reset, the week holds — same mode, but a different reason and deadline (page, card)
        now = self.h.clock()
        self.h.record("session.folder", "notes", {"git": "ok", "dirty": 0, "channel": "core"})
        self.h.record("account.limits", None, {"windows": {"five_hour": {"used": 0.9, "resets": now + 3600},
                                                           "seven_day": {"used": 0.86, "resets": now + 86400}}}, src="host")
        self.h.budget_mode()
        self.assertEqual(self.h.state["budget"]["window"], "five_hour")
        self.h.record("account.limits", None, {"windows": {"five_hour": {"used": 0.1, "resets": now + 3600},
                                                           "seven_day": {"used": 0.87, "resets": now + 86400}}}, src="host")
        self.h.budget_mode()
        self.h.budget_mode()
        self.assertEqual(self.modes(), ["economy", "economy"])
        self.assertEqual((self.h.state["budget"]["window"], self.h.state["budget"]["until"]), ("seven_day", now + 86400))

    def test_no_minihubs_left_back_to_normal(self):
        self.h.record("session.folder", "notes", {"git": "ok", "dirty": 0, "channel": "core"})
        self.h.record("account.limits", None, {"windows": {"five_hour": {"used": 0.9, "resets": self.h.clock() + 3600}}},
                      src="host")
        self.h.budget_mode()
        self.h.record("session.solo_set", "notes", {"on": True, **OWNER})
        self.h.budget_mode()
        self.assertEqual(self.modes(), ["economy", "normal"])

    def test_a_gateway_account_brings_no_economy(self):
        # "pool reserve 85% (5 h window of account conduit), forecast — runs out at 12:24" is the share of one gateway
        # subscription
        now = self.h.clock()
        self.h.record("session.folder", "notes", {"git": "ok", "dirty": 0, "channel": "core"})
        self.h.record("account.switched", None, {"to": "conduit", "from": "login", "restart": True, "why": "limit"})
        self.h.record("account.limits", None, {"windows": {"five_hour": {"used": 0.97, "resets": now + 3600}},
                                               "account": "login"}, src="host")
        self.h.record("account.limits", None, {"windows": {"five_hour": {"used": 0.85, "resets": now + 3600}},
                                               "account": "conduit"}, src="host")
        pool = {"accounts": [{"name": "login", "login": True}, {"name": "conduit", "command": ["conduit", "token"]}],
                "switch_at": 0.95}
        with mock.patch.object(self.h.profiles.builtin, "pool", lambda: pool):
            self.h.budget_mode()
        self.assertEqual(self.modes(), [])

    def test_forecast_by_the_pace_of_the_reserve_account(self):
        now = self.h.clock()
        for i, used in enumerate((0.5, 0.7, 0.86)):
            self.h.record("account.limits", None, {"windows": {"five_hour": {"used": used, "resets": now + 4 * 3600}}},
                          src="host")
        path = os.path.join(self.h.state_dir, "journal.jsonl")
        with open(path) as f:
            lines = [json.loads(x) for x in f]
        for e, ts in zip([e for e in lines if e["type"] == "account.limits"], (now - 3600, now - 1800, now)):
            e["ts"] = ts
        with open(path, "w") as f:
            f.write("".join(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n" for e in lines))
        from core import usage
        with mock.patch.object(usage, "_scan", wraps=usage._scan) as scan:
            eta = budget.forecast(self.h.state_dir, {}, None, "five_hour", now)
            self.assertEqual(budget.forecast(self.h.state_dir, {}, None, "five_hour", now), eta)
        self.assertEqual(scan.call_count, 1)                             # a daemon tick does not re-read the journal
        self.assertIsNotNone(eta)
        self.assertAlmostEqual(eta, now + 0.14 / (0.36 / 3600), delta=60)



class BudgetGate(Core):
    """The budget gate (hub.json budget.owner_gate, as the owner.settings fact): on by default — at the week threshold, in
    economy and at the whole pool's limit an executor waits for the owner's word; off — the limit is not a question for
    the owner: the week threshold and economy hold nobody (economy only moves normal to the cheap pair), and at the
    whole pool's limit executors wait for the window reset. Queue texts, limit lines and the hub's lines follow the
    setting; when it is switched off, an open economy card is withdrawn."""

    def gate(self, on):
        return self.ev("owner.settings", None, timezone="UTC", budget_gate=on, channel="core")

    def ask_card(self, marker, rid="r-eco"):
        """The hub, in a turn on the message marker, asks the owner with a form — a card."""
        self.ev("turn.started", "hub", marker=marker)
        self.ev("permission.requested", "hub", request_id=rid, tool="AskUserQuestion", interactive=True,
                input={"questions": [{"question": "Raise the queue now?", "header": "Limit",
                                      "options": [{"label": "Wait for the reset"}, {"label": "Raise"}]}]})
        self.ev("turn.result", "hub", text="asked")
        self.ev("turn.stopped", "hub")
        return self.st["sessions"]["hub"]["asks"][-1]["id"]

    def card_msg(self):
        return max((m for m in self.st["messages"].values() if m["session"] == "hub"), key=lambda m: m["created"])["id"]

    def test_on_by_default_the_week_threshold_waits_for_the_word(self):
        self.ev("account.limits", None, windows={"seven_day": {"used": 0.9, "resets": FAR}})
        self.assertTrue(budget.gate(self.st))
        self.assertIsNone(self.run_task("notes-a"))
        self.assertEqual(self.state_of("notes-a"), "queued")
        self.assertIn("the owner's word", self.st["runs"]["notes-a"]["why"])

    def test_off_the_week_threshold_holds_nobody(self):
        self.gate(False)
        self.assertFalse(budget.gate(self.st))
        self.ev("account.limits", None, windows={"seven_day": {"used": 0.9, "resets": FAR}})
        self.assertIsNone(self.run_task("notes-a"))
        self.assertEqual(self.state_of("notes-a"), "ready")

    def test_off_economy_only_moves_normal_to_the_cheap_pair(self):
        self.eco()
        self.gate(False)
        self.mode("economy")
        self.assertEqual(self.hub_texts(), [])                     # nothing to decide — no card
        self.session_task_turn()
        eco = {"model": "sonnet", "effort": "medium"}
        self.assertIsNone(self.run_task("notes-a", model="sonnet", effort="high", work="normal", economy=eco))
        self.assertEqual(self.state_of("notes-a"), "ready")
        self.assertEqual({k: self.st["runs"]["notes-a"].get(k) for k in ("model", "effort")}, eco)
        self.assertNotIn("your word", self.snapshot()["budget"].get("why") or "")
        self.assertIs(self.snapshot()["budget"]["gate"], False)

    def test_on_the_whole_pool_at_the_limit_waits_for_the_word(self):
        self.eco()
        self.mode("owner", why="all accounts at the threshold")
        self.session_task_turn()
        self.assertIsNone(self.run_task("notes-a"))
        (q,) = self.snapshot()["queue"]
        self.assertEqual(q["why"], "pool limit — waits for your word")
        self.assertIs(self.snapshot()["budget"]["gate"], True)

    def test_off_the_whole_pool_at_the_limit_waits_for_the_reset(self):
        self.eco()
        self.gate(False)
        self.mode("owner", why="all accounts at the threshold")
        (text,) = self.hub_texts()
        self.assertIn("wait for the window to reset", text)
        self.assertNotIn("Ask the owner", text)
        self.assertNotIn("explicit word", text)
        self.session_task_turn()
        self.assertIsNone(self.run_task("notes-a"))
        run = self.st["runs"]["notes-a"]
        self.assertEqual(run["state"], "queued")
        self.assertIn("waits for the window to reset", run["why"])
        self.assertNotIn("explicit word", run["why"])
        (q,) = self.snapshot()["queue"]
        self.assertEqual(q["why"], "pool limit — waits for the window to reset")
        self.assertEqual(executors.promotable(self.st), [])
        self.mode("normal", why="the window reset")
        self.assertEqual(executors.promotable(self.st), ["notes-a"])   # rises by itself, without the owner's word
        self.assertIn("comes up by itself", self.hub_texts()[-1])
        self.assertNotIn("withdraw it", self.hub_texts()[-1])              # there was no card — nothing to withdraw

    def test_switching_it_off_releases_the_queue_and_rewrites_why(self):
        self.ev("account.limits", None, windows={"seven_day": {"used": 0.9, "resets": FAR}})
        self.assertIsNone(self.run_task("notes-a"))
        self.assertEqual(executors.promotable(self.st), [])
        self.gate(False)
        self.assertEqual(executors.promotable(self.st), ["notes-a"])
        self.assertNotIn("the owner's word", self.st["runs"]["notes-a"]["why"])

    def test_switching_it_off_at_the_pool_limit_rewrites_the_queue_rows(self):
        self.eco()
        self.mode("owner", why="all accounts at the threshold")
        self.session_task_turn()
        self.assertIsNone(self.run_task("notes-a"))
        self.assertIn("explicit word", self.st["runs"]["notes-a"]["why"])
        self.gate(False)
        self.assertIn("waits for the window to reset", self.st["runs"]["notes-a"]["why"])
        self.assertEqual(self.snapshot()["queue"][0]["why"], "pool limit — waits for the window to reset")

    def test_switching_it_off_withdraws_the_open_economy_card(self):
        self.eco()
        self.ev("message.received", None, to="hub", text="what about the mail?", **OWNER)
        other = self.ask_card(self.card_msg(), rid="r-other")      # another hub card — stays
        self.mode("economy")
        qid = self.ask_card(self.card_msg())
        asks = lambda: [a["id"] for a in self.st["sessions"]["hub"]["asks"]]   # noqa: E731
        self.assertEqual(asks(), [other, qid])
        self.assertIsNone(self.gate(False))
        self.assertEqual(asks(), [other])
        self.assertIn(qid, self.hub_texts()[-1])
        self.assertIn("The core removed the economy card", self.hub_texts()[-1])
        n = len(self.hub_texts())
        self.gate(False)                                           # the same value — nothing new
        self.assertEqual(len(self.hub_texts()), n)

    def test_switching_it_off_keeps_a_hub_question_asked_in_a_turn_that_took_more_than_the_card(self):
        # the hub's turn took both the economy card and another message: its question may be about something else —
        # the gate does not withdraw it
        self.eco()
        self.mode("economy")
        card = self.card_msg()
        self.ev("message.received", None, to="hub", text="what about the mail?", **OWNER)
        mail = self.card_msg()
        self.ev("turn.started", "hub", marker=card, also=[mail])
        self.ev("permission.requested", "hub", request_id="r-mixed", tool="AskUserQuestion", interactive=True,
                input={"questions": [{"question": "Send the letter?", "header": "Mail",
                                      "options": [{"label": "Yes"}, {"label": "No"}]}]})
        (qid,) = [a["id"] for a in self.st["sessions"]["hub"]["asks"]]
        self.assertIsNone(self.gate(False))
        self.assertEqual([a["id"] for a in self.st["sessions"]["hub"]["asks"]], [qid])

    def test_switching_it_off_withdraws_both_cards_when_economy_went_on_to_the_pool_limit(self):
        # economy, then the whole-pool threshold: the owner has two open limit cards — the gate withdraws both
        self.eco()
        self.mode("economy")
        first = self.ask_card(self.card_msg(), rid="r-eco")
        self.mode("owner", why="all accounts at the threshold")
        second = self.ask_card(self.card_msg(), rid="r-pool")
        asks = lambda: [a["id"] for a in self.st["sessions"]["hub"]["asks"]]   # noqa: E731
        self.assertEqual(asks(), [first, second])
        self.assertIsNone(self.gate(False))
        self.assertEqual(asks(), [])
        self.assertIn(first, self.hub_texts()[-1])
        self.assertIn(second, self.hub_texts()[-1])

    def test_switching_it_on_again_changes_no_card(self):
        self.eco()
        self.gate(False)
        self.mode("economy")
        self.gate(True)
        self.assertEqual(self.hub_texts(), [])


class BudgetGateSetting(unittest.TestCase):
    def hub(self, cfg):
        from core import catalog
        from core.daemon import Hub
        h = Hub(tempfile.mkdtemp(prefix="gate-d-"), type("H", (), {"alive": lambda s, n: True})(),
                registry_dir=tempfile.mkdtemp())
        base = catalog.load_config(h.state_dir)
        with mock.patch.object(catalog, "load_config", lambda _d: {**base, **cfg}):
            h.load_catalog()
        return h

    def test_on_by_default_off_by_the_setting(self):
        self.assertTrue(budget.gate(self.hub({}).state))
        self.assertFalse(budget.gate(self.hub({"budget": {"owner_gate": False}}).state))
        self.assertTrue(budget.gate(self.hub({"budget": {"owner_gate": True}}).state))

    def test_the_page_can_switch_it(self):
        from modules.web import settings
        self.assertIs(settings.KEYS["budget.owner_gate"][3], True)
        self.assertEqual(settings.validate("budget.owner_gate", False), (False, None))
        self.assertEqual(settings.texts("en")[0]["budget_owner_gate"][0], "Executors wait for your word at the limit")


if __name__ == "__main__":
    unittest.main()
