"""Session status as a single field (sessions[].status): one core rule for display, instead of every view assembling
"waiting for you" and "working" itself from state, pending, requests, background, parked. Rules apply in order, the
first match decides: failed → (closed) asleep → waiting → working → idle → asleep. waiting is a form or a card
(pending), for a sleeping session too, and a tool request for a live one (the core will not accept a decision for a
sleeping or parked one). For a failed or closed session the status follows state (review 2026-10-15, round 5: replies
to departed sessions are a separate task). A Claude Code window without a form (tmux) is working: it can be answered
only in the terminal, and the page labels it "hub attach". All cases go through a real snapshot row
(Reader.snapshot): the rule reads it, not the core's session."""
import tests  # noqa: F401 — hide the machine's settings
import unittest

from core import owner, reducer, views

ASK = {"request_id": "r1", "tool": "AskUserQuestion", "input": {"questions": [{"question": "Ok?"}]}, "since": 1}
CARD = {**ASK, "id": "q1", "since": 90}
TOOL = {"tool": "jira", "state": "needs-owner", "since": 1}
BG = {"b1": {"id": "b1"}}


def snapshot(sessions, now=100):
    st = reducer.empty()
    st["sessions"] = {n: {"name": n, "since": 1, **s} for n, s in sessions.items()}
    r = views.Reader()
    r.state = st
    return st, r.snapshot(now=now)


def status(state, **kw):
    _, snap = snapshot({"a": {"state": state, **kw}})
    return snap["sessions"][0]["status"]


class Rule(unittest.TestCase):
    def test_failed_and_closed_by_state(self):
        self.assertEqual(status("failed"), "failed")
        self.assertEqual(status("failed", background=BG), "failed")
        self.assertEqual(status("failed", asks=[CARD], requests=[TOOL]), "failed")
        self.assertEqual(status("closed", asks=[CARD], requests=[TOOL]), "asleep")

    def test_waiting_by_a_form_or_a_card_in_any_state_that_is_here(self):
        # a sleeping one counts: the reply will wake it (review 2026-10-15)
        for state in ("busy", "starting", "dialog", "idle", "held", "asleep", "absent"):
            with self.subTest(state=state):
                self.assertEqual(status(state, pending=[ASK]), "waiting")      # form, permission
                self.assertEqual(status(state, asks=[CARD]), "waiting")        # card
        # a Claude Code window without a form (tmux host): the turn runs, it can be answered only in the terminal —
        # not "waiting for you"; the page labels where to answer (review 2026-10-15, round 3)
        self.assertEqual(status("dialog"), "working")

    def test_a_tool_request_waits_in_any_state(self):
        # the core records the owner's tool decision for a sleeping and for a parked session too (2026-10-17) —
        # "waiting for you"
        for state in ("busy", "starting", "dialog", "idle", "held", "asleep", "absent"):
            with self.subTest(state=state):
                self.assertEqual(status(state, requests=[TOOL]), "waiting")
        self.assertEqual(status("parked", requests=[TOOL]), "waiting")

    def test_waiting_wins_over_background_work(self):
        self.assertEqual(status("idle", pending=[ASK], background=BG), "waiting")
        self.assertEqual(status("idle", requests=[TOOL], background=BG), "waiting")
        self.assertEqual(status("busy", asks=[CARD], background=BG), "waiting")

    def test_working(self):
        self.assertEqual(status("busy"), "working")
        self.assertEqual(status("starting"), "working")
        self.assertEqual(status("idle", background=BG), "working")   # free, background work is running
        self.assertEqual(status("idle", compacting=100.0), "working")   # free, its conversation is being compacted

    def test_idle(self):
        self.assertEqual(status("idle"), "idle")
        self.assertEqual(status("held"), "idle")                     # manual mode: the core does not manage the session
        self.assertEqual(status("held", background=BG), "idle")
        self.assertEqual(status("parked"), "idle")      # parked: the snapshot gives state idle
        self.assertEqual(status("parked", background=BG), "idle")   # the snapshot does not expose a sleeping session's background

    def test_asleep(self):
        for state in ("asleep", "closed"):
            self.assertEqual(status(state), "asleep")

    def test_not_started_and_unknown_states_are_idle(self):
        # not yet started (absent) and an unknown state — free, as in the list before: not in "Asleep or failed"
        # and not in "Delete all", which the core would reject for it (review 2026-10-15)
        for state in ("absent", "someday"):
            self.assertEqual(status(state), "idle")

    def test_guesses_are_not_waiting(self):
        # the watchdog's guess and the session's "what I am waiting for" are their own fields, not "waiting for
        # you": facts only (2026-09-30)
        self.assertEqual(status("idle", attention={"text": "answer?"}), "idle")
        self.assertEqual(status("idle", waiting={"text": "waiting for CI", "ts": 1}), "idle")
        self.assertEqual(status("idle", attention={"text": "x"}, background=BG), "working")

    def test_an_old_card_is_not_waiting(self):
        # a card older than a day is not "waiting for you" (as now: the snapshot no longer puts it into pending)
        _, snap = snapshot({"q": {"state": "idle", "asks": [CARD]}}, now=90 + owner.ASK_TTL)
        self.assertEqual(snap["sessions"][0]["status"], "idle")


class Matrix(unittest.TestCase):
    """The exact value for each state × flag (form, card, tool request, resolved request, background)."""
    # state: (no flags, form, card, needs-owner request, granted, denied, requested — resolved or with the hub,
    # background)
    WANT = {
        "failed": ("failed",) * 8,
        "busy": ("working", "waiting", "waiting", "waiting", "working", "working", "working", "working"),
        "starting": ("working", "waiting", "waiting", "waiting", "working", "working", "working", "working"),
        "dialog": ("working", "waiting", "waiting", "waiting", "working", "working", "working", "working"),
        "idle": ("idle", "waiting", "waiting", "waiting", "idle", "idle", "idle", "working"),
        "held": ("idle", "waiting", "waiting", "waiting", "idle", "idle", "idle", "idle"),
        "asleep": ("asleep", "waiting", "waiting", "waiting", "asleep", "asleep", "asleep", "asleep"),
        "closed": ("asleep",) * 8,
        "absent": ("idle", "waiting", "waiting", "waiting", "idle", "idle", "idle", "idle"),
    }

    def test_every_state_and_sign(self):
        signs = ({}, {"pending": [ASK]}, {"asks": [CARD]}, {"requests": [TOOL]},
                 *({"requests": [{**TOOL, "state": st}]} for st in ("granted", "denied", "requested")),
                 {"background": BG})
        for state, want in self.WANT.items():
            for kw, w in zip(signs, want):
                with self.subTest(state=state, sign=list(kw)):
                    self.assertEqual(status(state, **kw), w)


class Terminal(unittest.TestCase):
    def test_terminal_is_a_fact_of_the_core(self):
        # a Claude Code window without a turn form is a field of the snapshot row, not a display guess (review
        # 2026-10-15): the page and "Project" label it "hub attach", clients do as they like
        _, snap = snapshot({"t": {"state": "dialog"},                              # window without a form (tmux)
                            "c": {"state": "dialog", "asks": [CARD]},              # and a card — a hint next to it
                            "f": {"state": "dialog", "pending": [ASK]},            # turn form — answered on the page
                            "b": {"state": "busy"}})
        got = {s["name"]: (s["terminal"], s["status"]) for s in snap["sessions"]}
        self.assertEqual(got, {"t": (True, "working"), "c": (True, "waiting"), "f": (False, "waiting"),
                               "b": (False, "working")})


class OneRule(unittest.TestCase):
    def test_status_and_questions_to_the_owner_agree_for_live_sessions(self):
        # the status "waiting for you" of live sessions covers the same questions as those put to the owner
        # (reminders, Telegram, pushes: owner.questions, owner.alerts); for a failed or closed session the status
        # follows state — their questions stay there as before (accepted for this branch, review 2026-10-15;
        # a task of the direction)
        st, snap = snapshot({
            "f": {"state": "failed", "asks": [CARD]},
            "c": {"state": "closed", "requests": [TOOL]},
            "t": {"state": "dialog"},
            "r": {"state": "asleep", "requests": [TOOL]},
            "s": {"state": "asleep", "asks": [CARD]},
            "q": {"state": "idle", "asks": [CARD]},
            "b": {"state": "busy", "pending": [ASK]},
            "i": {"state": "idle"},
        })
        got = {s["name"]: s["status"] for s in snap["sessions"]}
        self.assertEqual(got, {"f": "failed", "c": "asleep", "t": "working", "r": "waiting", "s": "waiting",
                               "q": "waiting", "b": "waiting", "i": "idle"})
        waiting = {n for n, v in got.items() if v == "waiting"}
        self.assertEqual({q[1] for q in owner.questions(st, 100)}, waiting | {"f", "c"})
        self.assertEqual({a["session"] for a in owner.alerts(st, 100)}, waiting | {"f", "c"})   # push triggers


class InSnapshot(unittest.TestCase):
    def test_every_session_has_status_and_old_fields_stay(self):
        _, snap = snapshot({
            "w": {"state": "busy"},
            "q": {"state": "idle", "asks": [CARD]},
            "p": {"state": "parked"},
            "c": {"state": "parked", "asks": [CARD]},
            "z": {"state": "asleep"},
        })
        got = {s["name"]: s for s in snap["sessions"]}
        self.assertEqual({n: s["status"] for n, s in got.items()},
                         {"w": "working", "q": "waiting", "p": "idle", "c": "waiting", "z": "asleep"})
        self.assertEqual(got["p"]["state"], "idle")            # the old fields are as they were
        self.assertEqual(len(got["q"]["pending"]), 1)


if __name__ == "__main__":
    unittest.main()
