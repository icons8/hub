"""A turn's result reaches whoever assigned the task exactly once (PR #49, analysis of the journal).

A repeat turn of an executor (`hub send`, `hub session wake "…"`) must reach the assigner, but not twice: the core does
not forward the result if the session itself wrote to the assigner in the same turn (`hub send`); the task by `--about`
is the one that was asked about, not the latest; a turn started by someone else's message or by the owner's word is not
a continuation; the continuation after «Connected» is not eaten by the late result of an interrupted turn. The
scenarios come from the journal analysis."""
import tests  # noqa: F401 — do not see the machine's settings
import unittest

from core import executors, reducer

OWNER = {"channel": "cli", "owner": True}
ONBOARD_LINE = "I can make this a direction"


def by(name):
    return {"channel": "session", "owner": False, "sender": name}


class Base(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("catalog.loaded", None, tools={"jira": {"kind": "mcp", "risk": [], "server": {}}})
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.started", "hub", session_id="s-hub")

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 1000 + self.seq, "type": type,
                                                   "entity": entity, "data": data})
        return why

    def agent(self, name):
        self.ev("session.requested", name, cwd=f"/{name}", session_id=f"s-{name}", role="agent", **OWNER)
        self.ev("session.started", name, session_id=f"s-{name}")

    def task(self, to, setter, text):
        """hub task new <to> «text» from setter (CLI: for_session = the assigning session itself)."""
        self.assertIsNone(self.ev("delegation.created", None, session=to, for_session=setter, context=text, **by(setter)))
        return f"d{self.seq}"

    def send(self, to, text, sender, **extra):
        self.assertIsNone(self.ev("message.received", None, to=to, text=text, **by(sender), **extra))
        return f"m{self.seq}"

    def turn(self, who, marker, result, own_to=None, own_text=None):
        """A turn of session who: by the marker, during the turn — its own `hub send own_to` (if set), then the result."""
        self.ev("turn.started", who, marker=marker, prompt_head="x")
        if own_to:
            self.send(own_to, own_text or result, who)
        self.ev("turn.result", who, text=result)
        self.ev("turn.stopped", who)

    # --- what was delivered ---
    def to(self, setter):
        return [m for m in self.st["messages"].values() if m["session"] == setter]

    def deliveries(self, setter, text):
        """How many times the result text reached setter: the core's relay («answer to the task») + its own `hub send`."""
        relayed = [m for m in self.to(setter) if "reply to task" in (m.get("origin") or "") and text in m["text"]]
        own = [m for m in self.to(setter) if m.get("sender") and text in m["text"]]
        return len(relayed) + len(own)

    def notes(self, setter_hub, text):
        """REPLIED lines from the core to the hub with this result."""
        return len([m for m in self.to(setter_hub) if m["text"].startswith("REPLIED") and text in m["text"]])


class HubExecutor(Base):
    """A hub executor (`hub task run ex …`), the first task answered."""

    def setUp(self):
        super().setUp()
        self.ev("task.run_requested", "ex", task="check the webhooks", **by("hub"))
        for type, entity, data in executors.events_for(self.st, "ex", self.st["runs"]["ex"], cwd="/w", worktree=None):
            self.ev(type, entity, **data)
        self.ev("session.started", "ex", session_id=self.st["sessions"]["ex"]["session_id"])
        (self.d,) = self.st["delegations"].values()
        self.turn("ex", self.d["message"], "first result")
        self.assertEqual(self.deliveries("hub", "first result"), 1)

    def retire(self):
        self.ev("session.close_requested", "ex", retire=True)
        self.ev("session.exited", "ex", why="sleep")

    # --- what the PR fixes: the result of a repeat turn is delivered (on main — 0) ---
    def test_followup_by_setter_send_is_delivered_once(self):
        """The hub wrote to the executor once more, and it answered with the turn's result."""
        self.turn("ex", self.send("ex", "and now GitLab", "hub"), "result about GitLab")
        self.assertEqual(self.deliveries("hub", "result about GitLab"), 1)
        self.assertLessEqual(self.notes("hub", "result about GitLab"), 1)

    def test_followup_after_setter_wake_is_delivered_once(self):
        """`hub session wake ex "…"` from the hub."""
        self.retire()
        self.turn("ex", self.send("ex", "finish GitLab", "hub", wake=True), "after waking")
        self.assertEqual(self.deliveries("hub", "after waking"), 1)

    def test_two_followups_in_a_row_each_once(self):
        self.turn("ex", self.send("ex", "one", "hub"), "result-1")
        self.turn("ex", self.send("ex", "two", "hub"), "result-2")
        self.assertEqual((self.deliveries("hub", "result-1"), self.deliveries("hub", "result-2")), (1, 1))
        self.assertEqual(self.deliveries("hub", "first result"), 1)       # the first was not relayed again

    def test_stop_from_setter_answered_once(self):
        self.turn("ex", self.send("ex", "stop, do not merge", "hub"), "stopped, did not merge")
        self.assertEqual(self.deliveries("hub", "stopped"), 1)

    # --- duplicates: pass on main, not with the PR ---
    def test_followup_with_own_send_is_not_delivered_twice(self):
        """It wrote to the hub with `hub send` itself and ended the turn with the same result — the hub gets it once."""
        self.turn("ex", self.send("ex", "what about the MR?", "hub"), "MR is open, the frontend review is pending", own_to="hub")
        self.assertEqual(self.deliveries("hub", "MR is open"), 1)

    def test_onboarding_line_is_not_repeated_on_every_followup(self):
        """ONBOARD («can make this a direction») is appended to every result of a hub executor — and to a continuation."""
        self.turn("ex", self.send("ex", "one", "hub"), "result-1")
        self.turn("ex", self.send("ex", "two", "hub"), "result-2")
        relays = [m for m in self.to("hub") if "reply to task" in (m.get("origin") or "")]
        self.assertLessEqual(len([m for m in relays if ONBOARD_LINE in m["text"]]), 1)

    def test_tool_connected_mid_followup_keeps_the_continuation(self):
        """A turn on the hub's message, a tool was connected mid-turn, the turn was cut off, its late result closed the task —
        the result of the continuation after «Connected» still arrives."""
        mid = self.send("ex", "create a form in the admin", "hub")
        self.ev("turn.started", "ex", marker=mid, prompt_head="x")
        self.ev("tools.requested", "ex", tool="jira", why="form", **by("ex"))
        self.ev("tools.granted", "ex", tool="jira", **by("hub"))
        self.ev("turn.stopped", "ex")                                   # restart with the tool: «Connected…»
        self.ev("turn.result", "ex", text="waiting for the tool to connect")  # the late result of the interrupted turn
        cont = next(m["id"] for m in self.st["messages"].values()
                    if m["session"] == "ex" and m["text"].startswith("Connected"))
        self.turn("ex", cont, "the form is created")
        self.assertEqual(self.deliveries("hub", "the form is created"), 1)

    # --- not a continuation: neither on main nor with the PR ---
    def test_owner_wake_is_not_relayed_to_the_setter(self):
        self.retire()
        self.ev("message.received", None, to="ex", text="look at the log again", wake=True, **OWNER)
        self.turn("ex", f"m{self.seq}", "the log is clean")
        self.assertEqual(self.deliveries("hub", "the log is clean"), 0)

    def test_message_from_a_stranger_is_not_the_task(self):
        self.agent("other")
        self.turn("ex", self.send("ex", "hello", "other"), "a reply to an outsider", own_to="other")
        self.assertEqual(self.deliveries("hub", "a reply to an outsider"), 0)
        self.assertEqual(self.deliveries("other", "a reply to an outsider"), 1)

    def test_handoff_done_is_not_also_an_answer(self):
        self.turn("ex", self.send("ex", "one", "hub"), "result-1")
        self.ev("session.close_requested", "ex", **by("hub"))
        ask = next((m["id"] for m in self.st["messages"].values()
                    if m["session"] == "ex" and m["text"].startswith("HANDOFF-REQUEST")), None)
        if ask is None:
            self.skipTest("the executor was put to sleep without a handoff")
        self.turn("ex", ask, "HANDOFF-DONE\nDone: webhooks and GitLab")
        self.assertEqual(len([m for m in self.to("hub") if "Done: webhooks and GitLab" in m["text"]]), 1)


class AgentExecutor(Base):
    """An executor of a specialist (`hub task run` from an agent), asleep after handing in: the specialist itself wakes
    it for the next round (a merge request review takes days) and the result of that round goes back to the specialist."""

    def setUp(self):
        super().setUp()
        self.agent("qa")
        self.ev("turn.started", "qa", marker=None, prompt_head="x")
        self.ev("task.run_requested", "ex", task="a run on stage", **by("qa"))
        for type, entity, data in executors.events_for(self.st, "ex", self.st["runs"]["ex"], cwd="/w", worktree=None):
            self.ev(type, entity, **data)
        self.ev("turn.stopped", "qa")
        self.ev("session.started", "ex", session_id=self.st["sessions"]["ex"]["session_id"])
        (self.d,) = self.st["delegations"].values()
        self.assertEqual(self.st["sessions"]["ex"]["raised_by"], "qa")
        self.turn("ex", self.d["message"], "the run is going")
        self.ev("session.close_requested", "ex", retire=True)
        self.ev("session.exited", "ex", why="sleep")

    def test_the_raiser_wakes_him_and_the_next_round_result_goes_back_to_it(self):
        self.turn("ex", self.send("ex", "the reviewer replied, fix it", "qa", wake=True), "fixed, waiting for the review")
        self.assertEqual(self.deliveries("qa", "fixed, waiting for the review"), 1)
        self.assertEqual(self.deliveries("hub", "fixed, waiting for the review"), 0)


class Specialist(Base):
    """A long-lived specialist with an old task from the hub (`hub task new dev`, for_session=hub): with the PR every new
    hub message is a «continuation» of that task (journal analysis: otherwise every hub message is a «continuation» of the old task)."""

    def setUp(self):
        super().setUp()
        self.agent("dev")
        self.d1 = self.task("dev", "hub", "fix the feed")
        self.turn("dev", self.st["delegations"][self.d1]["message"], "the feed is fixed")

    def test_new_request_answered_by_own_send_once(self):
        """The specialist answered the hub itself with `hub send` — the turn's result is not relayed in addition."""
        self.turn("dev", self.send("dev", "look at the voice bug", "hub"), "voice: the cause is the codec", own_to="hub")
        self.assertEqual(self.deliveries("hub", "voice: the cause is the codec"), 1)

    def test_about_names_the_task_it_continues(self):
        """`hub send dev --about d1 …` with two open tasks from the hub: the PR takes the latest (d2), not the named one."""
        d2 = self.task("dev", "hub", "update the README")
        self.turn("dev", self.st["delegations"][d2]["message"], "README updated")
        self.turn("dev", self.send("dev", "about the feed: and mobile?", "hub", about=self.d1), "mobile too")
        wrong = [m for m in self.to("hub") if "mobile too" in m["text"] and d2 in (m.get("origin") or "") + m["text"]]
        self.assertEqual(wrong, [])


class Peers(Base):
    """A session assigned a task to a neighbour (`hub task new maps-ops`, for_session=ux): their correspondence continues
    over `hub send` — with the fix applied to any message, every such turn arrived twice."""

    def setUp(self):
        super().setUp()
        self.agent("maps-ops")
        self.agent("ux")
        self.d = self.task("maps-ops", "ux", "deploy the service")
        self.turn("maps-ops", self.st["delegations"][self.d]["message"], "deployed", own_to=None)

    def test_peer_reply_by_own_send_once(self):
        self.turn("maps-ops", self.send("maps-ops", "redeploy on main 1c2101c", "ux"), "redeploy done, healthy",
                  own_to="ux")
        self.assertEqual(self.deliveries("ux", "redeploy done"), 1)

    def test_cancelled_executor_then_specialist_answers_once(self):
        """4bb14dd: a task was given to an executor, cancelled — it returned to the specialist; its result goes to the assigner, once."""
        self.ev("turn.started", "maps-ops", marker=None, prompt_head="long work")
        d = self.task("maps-ops", "ux", "check the certificate")
        msg = self.st["delegations"][d]["message"]
        self.assertIsNone(self.ev("message.routed", d, route="executor", name="certs", **by("hub")))
        run = self.st["messages"][msg]["handover"]
        self.assertIsNone(self.ev("task.run_cancelled", run, **by("hub")))
        self.ev("turn.result", "maps-ops", text="long work")
        self.ev("turn.stopped", "maps-ops")
        self.turn("maps-ops", msg, "the certificate is fine")
        self.assertEqual(self.deliveries("ux", "the certificate is fine"), 1)
        self.turn("maps-ops", self.send("maps-ops", "thanks, and the deadline?", "ux"), "deadline until May", own_to="ux")
        self.assertEqual(self.deliveries("ux", "deadline until May"), 1)


class FirstTurn(Base):
    """Already on main: during a task turn the session writes the result to the assigner itself and ends the turn with it (journal analysis: it happened with every fifth relay)."""

    def test_task_turn_with_own_send_is_delivered_once(self):
        self.agent("dev")
        d = self.task("dev", "hub", "fix the feed")
        self.turn("dev", self.st["delegations"][d]["message"], "the feed is fixed", own_to="hub")
        self.assertEqual(self.deliveries("hub", "the feed is fixed"), 1)


if __name__ == "__main__":
    unittest.main()
