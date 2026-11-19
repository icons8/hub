"""hub session reload: the session process anew with the same conversation (--resume) when it is free. On 2026-10-01
and 2026-10-02 a session's MCP did not connect at startup (CONNECT_TIMEOUT) — there was no command, the process was
killed by hand by PID."""
import tests  # noqa: F401 — do not see the machine's settings
import unittest

from core import reducer

OWNER = {"channel": "web", "owner": True}
HUB = {"channel": "session", "owner": False, "sender": "hub"}


class Reload(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        for n, kind in (("hub", "hub"), ("rw", None), ("seo", None)):
            self.ev("session.requested", n, cwd="/w", session_id=f"s-{n}", kind=kind, **OWNER)
            self.ev("session.started", n)
        self.ev("turn.started", "rw", marker=None)
        self.ev("turn.stopped", "rw")  # there was a turn — the conversation is on disk

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, actions, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 100 + self.seq, "type": type,
                                                         "entity": entity, "data": data})
        self.why = why
        return actions

    def test_idle_session_is_respawned_with_its_conversation(self):
        acts = self.ev("session.reload_requested", "rw", **HUB)
        self.assertEqual([a["do"] for a in acts if a.get("session") == "rw"], ["release", "spawn"])
        spawn = next(a for a in acts if a["do"] == "spawn")
        self.assertEqual((spawn["session_id"], spawn["resume"]), ("s-rw", True))
        self.assertEqual(self.st["sessions"]["rw"]["state"], "starting")

    def test_busy_session_reloads_after_its_turn(self):
        self.ev("turn.started", "rw", marker=None)
        self.assertEqual([a for a in self.ev("session.reload_requested", "rw", **OWNER) if a.get("session") == "rw"],
                         [])
        acts = self.ev("turn.stopped", "rw")
        self.assertEqual([a["do"] for a in acts if a.get("session") == "rw"], ["release", "spawn"])

    def test_session_may_reload_itself_but_not_another(self):
        self.ev("session.reload_requested", "rw", channel="session", owner=False, sender="seo")
        self.assertTrue(self.why)
        acts = self.ev("session.reload_requested", "rw", channel="session", owner=False, sender="rw")
        self.assertEqual([a["do"] for a in acts if a.get("session") == "rw"], ["release", "spawn"])

    def to_rw(self):
        return [m for m in self.st["messages"].values() if m["session"] == "rw"]

    def test_session_reloaded_at_its_own_request_mid_turn_continues(self):
        # a session mid-turn asked for reload for the sake of a fixed tool and ended the turn with "I'll do it after
        # the restart" — after the respawn it stood idle until the owner wrote himself
        self.ev("message.received", None, to="rw", text="deploy it", **OWNER)
        mid = f"m{self.seq}"
        self.ev("turn.started", "rw", marker=mid)
        self.ev("session.reload_requested", "rw", channel="session", owner=False, sender="rw")
        acts = self.ev("turn.stopped", "rw")
        self.assertEqual([a["do"] for a in acts if a.get("session") == "rw"], ["release", "spawn"])
        self.ev("session.started", "rw", session_id="s-rw")
        (go,) = [m for m in self.to_rw() if m["id"] != mid]
        self.assertIn("at your request", go["text"])
        self.assertEqual(go["root"], mid)   # the continuation's result goes where the owner asked
        self.ev("session.exited", "rw", why="reconcile")   # next respawn of a free session — no second "continue"
        self.ev("session.started", "rw", session_id="s-rw")
        self.assertEqual(len(self.to_rw()), 2)

    def test_reload_asked_again_in_its_continuation_is_not_continued_again(self):
        # the tool never came up: the continuation asks for a restart again — no second "continue", otherwise a loop
        self.ev("message.received", None, to="rw", text="deploy it", **OWNER)
        self.ev("turn.started", "rw", marker=f"m{self.seq}")
        self.ev("session.reload_requested", "rw", channel="session", owner=False, sender="rw")
        self.ev("turn.stopped", "rw")
        self.ev("session.started", "rw", session_id="s-rw")
        (go,) = [m for m in self.to_rw() if "at your request" in m["text"]]
        asked = lambda: len([m for m in self.to_rw() if "at your request" in m["text"]])   # noqa: E731
        for marker in (go["id"], None):   # in the continuation itself and in a turn its background woke — too
            self.ev("turn.started", "rw", marker=marker)
            self.ev("session.reload_requested", "rw", channel="session", owner=False, sender="rw")
            acts = self.ev("turn.stopped", "rw")
            self.assertEqual([a["do"] for a in acts if a.get("session") == "rw"], ["release", "spawn"])   # restart — yes
            self.ev("session.started", "rw", session_id="s-rw")
            self.assertEqual(asked(), 1)
        self.ev("message.received", None, to="rw", text="now something else", **OWNER)   # a new matter — continues again
        self.ev("turn.started", "rw", marker=f"m{self.seq}")
        self.ev("session.reload_requested", "rw", channel="session", owner=False, sender="rw")
        self.ev("turn.stopped", "rw")
        self.ev("session.started", "rw", session_id="s-rw")
        self.assertEqual(asked(), 2)

    def test_reload_by_owner_or_hub_does_not_tell_to_continue(self):
        self.ev("turn.started", "rw", marker=None)
        self.ev("session.reload_requested", "rw", **HUB)   # the requester will say what is next
        self.ev("turn.stopped", "rw")
        self.ev("session.started", "rw", session_id="s-rw")
        self.assertEqual(self.to_rw(), [])

    def test_failed_mcp_at_start_is_reconnected_on_the_spot(self):
        # before, a server that did not come up at startup was fixed by one session restart after the turn — and was
        # unavailable again. Now — a reconnect in place, at once and then no more often than mcpwatch.RETRY, without
        # restarting the session (core/mcpwatch)
        acts = self.ev("session.info", "rw", mcp=["ms365", "jira"], mcp_failed=["ms365"])
        self.assertEqual([a for a in acts if a.get("session") == "rw"],
                         [{"do": "mcp_reconnect", "session": "rw", "server": "ms365"}])
        self.assertFalse(self.st["sessions"]["rw"].get("dirty"))
        acts = self.ev("session.info", "rw", mcp=["ms365", "jira"], mcp_failed=["ms365"])
        self.assertEqual([a for a in acts if a.get("session") == "rw"], [])   # right away again — not in circles

    def test_starting_session_is_raised_again_at_once(self):
        # the process is alive, no SessionStart — the session is "starting" for tens of minutes; reload set dirty, and
        # it applies in a free minute, which a session that has not come up never has. Only killing the process by
        # hand helped
        self.ev("session.exited", "rw", why="process gone")
        self.ev("session.info", "rw", mcp=["jira"])                     # the process registered, no session.started
        rw = self.st["sessions"]["rw"]
        self.assertEqual(rw["state"], "starting")
        restarts = rw["restarts"]
        acts = [a for a in self.ev("session.reload_requested", "rw", **OWNER) if a.get("session") == "rw"]
        self.assertEqual([a["do"] for a in acts], ["kill", "spawn"])
        self.assertEqual((acts[1]["session_id"], acts[1]["resume"]), ("s-rw", True))
        rw = self.st["sessions"]["rw"]
        self.assertEqual((rw["state"], rw["since"], rw["dirty"]), ("starting", 100 + self.seq, False))
        self.assertEqual(rw["restarts"], restarts)                      # the owner's request is not a failed respawn

    def test_reload_of_a_starting_session_is_not_a_loop(self):
        self.ev("session.exited", "rw", why="process gone")
        for _ in range(3):                                              # each reload is its own respawn, no self-call
            acts = self.ev("session.reload_requested", "rw", **OWNER)
            self.assertEqual([a["do"] for a in acts if a.get("session") == "rw"], ["kill", "spawn"])
        self.assertEqual(self.st["sessions"]["rw"]["state"], "starting")
        self.assertEqual(self.ev("session.started", "rw"), [])           # it came up — we do nothing more
        self.assertEqual(self.ev("turn.stopped", "rw"), [])
        self.assertEqual(self.st["sessions"]["rw"]["state"], "idle")

    def test_starting_session_reloads_only_for_owner_hub_or_itself(self):
        self.ev("session.exited", "rw", why="process gone")
        acts = self.ev("session.reload_requested", "rw", channel="session", owner=False, sender="seo")
        self.assertTrue(self.why)
        self.assertEqual([a for a in acts if a.get("session") == "rw"], [])

    def test_sleeping_session_is_not_reloaded(self):
        self.ev("session.ended", "seo")
        self.ev("session.reload_requested", "seo", **HUB)
        self.assertTrue(self.why)

    def test_refusal_names_the_next_step(self):
        # a bare "only a live session is restarted" says nothing about what to do (hub request 26.10): the refusal by
        # state names the command: revive returns a failed session, wake wakes a sleeping one; reload never revives
        for _ in range(reducer.MAX_RESTARTS + 1):
            self.ev("session.exited", "rw")
        self.assertEqual(self.st["sessions"]["rw"]["state"], "failed")
        self.assertEqual([a for a in self.ev("session.reload_requested", "rw", **HUB) if a.get("session") == "rw"], [])
        self.assertIn("crashed", self.why)
        self.assertIn("hub session revive rw", self.why)
        self.assertEqual(self.st["sessions"]["rw"]["state"], "failed")
        self.ev("session.ended", "seo")
        self.ev("session.reload_requested", "seo", **HUB)
        self.assertIn("hub session wake seo", self.why)
        self.ev("session.reload_requested", "nobody", **HUB)
        self.assertIn("no such session", self.why)

    def test_refusal_names_wake_for_every_sleeping_state(self):
        # a parked session and a stopped executor sleep too: the refusal names wake for them, not the generic
        # "not allowed now"; reload itself does not bring them up
        self.ev("session.parked", "seo", channel="core", why="idle")
        self.ev("session.requested", "seo-1", cwd="/w", session_id="s-x", executor=True, raised_by="seo", **HUB)
        self.ev("session.started", "seo-1")
        self.ev("session.stop_requested", "seo-1", **HUB)
        for name, state in (("seo", "parked"), ("seo-1", "retired")):
            self.assertEqual(self.st["sessions"][name]["state"], state)
            acts = self.ev("session.reload_requested", name, **HUB)
            self.assertEqual([a for a in acts if a.get("session") == name], [])
            self.assertIn(f"hub session wake {name}", self.why)
            self.assertIn(f"in {state})", self.why)
            self.assertEqual(self.st["sessions"][name]["state"], state)
        # another session may not restart it: it is refused for that, without advice on waking someone else's session
        self.ev("session.reload_requested", "seo", channel="session", owner=False, sender="rw")
        self.assertIn("the owner, the hub or the session itself", self.why)
        self.assertNotIn("hub session wake", self.why)
        self.ev("session.reload_requested", "nobody", channel="session", owner=False, sender="rw")
        self.assertIn("the owner, the hub or the session itself", self.why)   # whether the name exists is not told either


if __name__ == "__main__":
    unittest.main()
