"""A job set by an executor (or handed to it) stays when the executor is removed, and nobody could remove it but the
hub and the owner. Whoever raised the executor may change its jobs, and only they."""
import tests  # noqa: F401 — ignore this machine's settings
import unittest

from core import executors, reducer

OWNER = {"channel": "cli", "owner": True}


def by(name):
    return {"channel": "session", "owner": False, "sender": name}


class RaiserEditsJobs(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        for name in ("maps", "docs"):
            self.ev("session.requested", name, cwd=f"/{name}", session_id=f"s-{name}", role="agent", about=name, **OWNER)
            self.ev("session.started", name)
        self.assertIsNone(self.ev("task.run_requested", "maps-1", task="fix the build", **by("maps")))
        for type, entity, data in executors.events_for(self.st, "maps-1", self.st["runs"]["maps-1"],
                                                       cwd="/m/.claude/worktrees/maps-1", worktree=None):
            self.ev(type, entity, **data)
        self.ev("session.started", "maps-1")
        self.assertIsNone(self.ev("schedule.set", "maps-1-tick", every="1h", prompt="check the build", session="maps-1",
                                  **by("maps-1")))

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 2000 + self.seq, "type": type,
                                                   "entity": entity, "data": data})
        return why

    def test_the_raiser_removes_the_job_of_its_removed_executor(self):
        self.ev("session.remove_requested", "maps-1", **OWNER)
        self.assertIsNotNone(self.st["sessions"]["maps-1"])
        self.assertIsNone(self.ev("schedule.remove_requested", "maps-1-tick", **by("maps")))
        self.assertEqual(self.st["schedule"]["maps-1-tick"]["state"], "removed")

    def test_another_specialist_cannot(self):
        self.assertIsNotNone(self.ev("schedule.remove_requested", "maps-1-tick", **by("docs")))
        self.assertEqual(self.st["schedule"]["maps-1-tick"]["state"], "active")

    def test_a_job_the_executor_set_for_someone_else_is_the_targets_not_the_raisers(self):
        self.assertIsNone(self.ev("schedule.set", "maps-1-for-docs", every="1h", prompt="look", session="docs",
                                  **by("maps-1")))
        self.assertIsNotNone(self.ev("schedule.remove_requested", "maps-1-for-docs", **by("maps")))
        self.assertIsNone(self.ev("schedule.remove_requested", "maps-1-for-docs", **by("docs")))

    def test_a_command_of_the_executor_is_removed_by_the_raiser(self):
        self.assertIsNone(self.ev("schedule.set", "maps-1-mon", every="1h", command="true", **by("maps-1")))
        self.assertIsNotNone(self.ev("schedule.remove_requested", "maps-1-mon", **by("docs")))
        self.assertIsNone(self.ev("schedule.remove_requested", "maps-1-mon", **by("maps")))

    def test_a_session_that_is_not_an_executor_is_not_covered(self):
        # "maps" is a specialist (not an executor) and raised by nobody: docs cannot touch its job
        self.assertIsNone(self.ev("schedule.set", "maps-own", every="1h", prompt="p", session="maps", **by("maps")))
        self.assertIsNotNone(self.ev("schedule.remove_requested", "maps-own", **by("docs")))


if __name__ == "__main__":
    unittest.main()
