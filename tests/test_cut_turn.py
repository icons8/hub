"""A hub restart cut a turn: after startup the core tells the session to continue (02.10: a background review finished,
the hub began a restart that same second while Claude Code began a turn with the review result "Fix first"; the turn
was cut, the result never reached the session, and the work stood for three hours, surviving four more restarts)."""
import tests  # noqa: F401 — keeps the machine's settings out
import unittest

from core import forms, reducer

OWNER = {"channel": "web", "owner": True}


class CutTurn(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        for n, kind in (("hub", "hub"), ("pe", None)):
            self.ev("session.requested", n, cwd="/w", session_id=f"s-{n}", kind=kind, **OWNER)
            self.ev("session.started", n)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 100 + self.seq, "type": type,
                                                   "entity": entity, "data": data})
        return why

    def to_pe(self):
        return [m for m in self.st["messages"].values() if m["session"] == "pe"]

    def test_turn_cut_by_restart_is_continued(self):
        self.ev("turn.started", "pe", marker=None)          # a background agent's result: a turn without a marker
        self.ev("session.exited", "pe", why="reconcile")     # hub restart
        self.ev("session.started", "pe")
        (msg,) = self.to_pe()
        self.assertIn("cut off your turn", msg["text"])
        self.assertEqual(msg["origin"], forms.ORIGIN_CORE)
        self.ev("session.exited", "pe", why="reconcile")     # a free session: leave it alone
        self.ev("session.started", "pe")
        self.assertEqual(len(self.to_pe()), 1)

    def test_a_turn_lost_with_no_clean_shutdown_is_not_called_a_restart(self):
        # a machine crash: the core found the process gone at its start (crash = the previous hubd left with no clean stop)
        self.ev("turn.started", "pe", marker=None)
        self.ev("session.exited", "pe", why="reconcile", crash=True)
        self.assertTrue(self.st["sessions"]["pe"]["cut_turn"]["crash"])
        self.ev("session.started", "pe")
        (msg,) = self.to_pe()
        self.assertIn("without a clean shutdown", msg["text"])
        self.assertNotIn("restart", msg["text"])
        self.assertIn("cut off your turn", msg["text"])
        self.assertEqual(msg["origin"], forms.ORIGIN_CORE)
        self.assertTrue(msg["id"].endswith("x3"))   # the same continuation as after a restart

    def test_a_crash_with_background_work_keeps_the_list(self):
        self.ev("session.background", "pe", tasks=[{"id": "a1", "kind": "agent", "title": "Review slices 10+11"}])
        self.ev("session.exited", "pe", why="reconcile", crash=True)
        self.ev("session.started", "pe")
        (msg,) = self.to_pe()
        self.assertIn("without a clean shutdown", msg["text"])
        self.assertIn("Review slices 10+11", msg["text"])

    def test_a_journal_without_the_crash_field_folds_as_before(self):
        # an event of an earlier core has no `crash`: the restart text, no field in the cut record
        self.ev("turn.started", "pe", marker=None)
        self.ev("session.exited", "pe", why="reconcile")
        self.assertNotIn("crash", self.st["sessions"]["pe"]["cut_turn"])
        self.ev("session.started", "pe")
        (msg,) = self.to_pe()
        self.assertTrue(msg["text"].startswith("A hub restart cut off your turn"), msg["text"])

    def test_background_work_cut_by_restart_is_continued(self):
        # 02.10, restart --now: the session was free but an agent was running in the background; it was killed and the session stayed silent
        self.ev("session.background", "pe", tasks=[{"id": "a1", "kind": "agent", "title": "Review slices 10+11"}])
        self.ev("session.exited", "pe", why="reconcile")
        self.ev("session.started", "pe")
        (msg,) = self.to_pe()
        self.assertIn("Review slices 10+11", msg["text"])

    def test_outgoing_process_does_not_bring_background_back(self):
        # 16.10, journal: the hub released a process (a restart for tools) and the process, on its way out, still sent
        # background lists; the core put them back, so the new process had dead tasks "in the background" for up to
        # an hour and a half, and the next cut told the session about them a second time
        bg = [{"id": "b1", "kind": "bash", "title": "until grep -q OK t.log"}, {"id": "a1", "kind": "agent", "title": "Review"}]
        self.ev("session.background", "pe", tasks=bg)
        self.ev("session.reload_requested", "pe", **OWNER)
        self.assertEqual(self.st["sessions"]["pe"]["state"], "starting")
        self.ev("session.background", "pe", tasks=bg[1:])   # the outgoing process shuts down its tasks
        self.ev("session.background", "pe", id="b2", kind="bash", title="x")
        self.assertEqual(self.st["sessions"]["pe"]["background"], {})
        self.ev("session.started", "pe")
        (msg,) = self.to_pe()
        self.assertIn("until grep -q OK t.log", msg["text"])
        self.assertIn("Review", msg["text"])
        self.assertEqual(self.st["sessions"]["pe"]["background"], {})
        self.ev("session.exited", "pe", why="reconcile")   # do not mention the same tasks a second time
        self.ev("session.started", "pe")
        self.assertEqual(len(self.to_pe()), 1)

    def test_background_of_a_dead_process_is_not_taken_before_respawn(self):
        # the process vanished on its own (observer): lines read from its output later belong to the departed process
        self.ev("session.background", "pe", tasks=[{"id": "b1", "kind": "bash", "title": "sleep 100"}])
        self.ev("session.exited", "pe", why="process gone")
        self.ev("session.background", "pe", tasks=[{"id": "b1", "kind": "bash", "title": "sleep 100"}])
        self.ev("session.started", "pe")
        self.assertEqual(self.st["sessions"]["pe"]["background"], {})
        # the new process started its own task: keep it
        self.assertIsNone(self.ev("session.background", "pe", id="b9", kind="bash", title="tools/test.py"))
        self.assertEqual(list(self.st["sessions"]["pe"]["background"]), ["b9"])

    def test_process_given_to_the_owner_takes_its_background_away(self):
        # hub take: the hub's process is released together with its background; its last lists and the owner's terminal do not keep the background alive
        self.ev("session.background", "pe", tasks=[{"id": "a1", "kind": "agent", "title": "Review"}])
        self.ev("session.take_requested", "pe", **OWNER)
        self.assertEqual(self.st["sessions"]["pe"]["state"], "held")
        self.assertEqual(self.st["sessions"]["pe"]["background"], {})
        self.ev("session.background", "pe", tasks=[{"id": "a1", "kind": "agent", "title": "Review"}])
        self.assertEqual(self.st["sessions"]["pe"]["background"], {})
        self.ev("session.give_requested", "pe", **OWNER)
        self.ev("session.started", "pe")
        self.assertEqual(self.st["sessions"]["pe"]["background"], {})

    def test_idle_session_restarted_gets_nothing(self):
        self.ev("turn.started", "pe", marker=None)
        self.ev("turn.stopped", "pe")
        self.ev("session.exited", "pe", why="reconcile")
        self.ev("session.started", "pe")
        self.assertEqual(self.to_pe(), [])


if __name__ == "__main__":
    unittest.main()
