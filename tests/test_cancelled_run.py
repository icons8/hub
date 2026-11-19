"""A cancelled executor does not hold the task (2026-10-07, the watchdog fired three times in a night: a message sat
"queued" for 177 min with a free specialist). The hub gave a busy specialist's task to an executor (hub route …
executor), which waited in the queue by budget; the hub cancelled it (hub task cancel) and returned the task to the
specialist's queue (hub route … queue), but the message stayed marked "handed to an executor": it was never delivered
to the specialist, and route answered "no longer waiting for a decision"."""
import tests  # noqa: F401 — keeps the machine's settings out
import unittest

from tests.test_task_model import HUB, MAPS, TaskModel


class CancelledRun(TaskModel):
    def route_to_executor(self):
        self.ev("turn.started", "devops", marker=None, prompt_head="long work")
        self.assertIsNone(self.ev("delegation.created", None, session="devops", context="check the certificate",
                                  for_session="maps", **MAPS))
        d = f"d{self.seq}"
        msg = self.st["delegations"][d]["message"]
        self.assertIsNone(self.ev("message.routed", d, route="executor", name="certs", **HUB))
        return self.st["messages"][msg]["handover"], msg

    def test_cancelling_the_executor_returns_the_task_to_the_specialist(self):
        run, msg = self.route_to_executor()
        self.assertIsNone(self.ev("task.run_cancelled", run, **HUB))
        self.assertNotIn("handover", self.st["messages"][msg])
        self.ev("turn.result", "devops", text="ok")
        self.ev("turn.stopped", "devops")
        self.assertEqual(self.st["messages"][msg]["state"], "typing")       # the specialist became free and received it

    def test_a_stale_handover_does_not_hold_the_message_forever(self):
        # journals from before the fix: the mark stayed, and there is no executor either in the queue or as a session
        run, msg = self.route_to_executor()
        self.st["runs"].pop(run)
        self.ev("turn.result", "devops", text="ok")
        self.ev("turn.stopped", "devops")
        self.assertEqual(self.st["messages"][msg]["state"], "typing")


def load_tests(loader, standard, pattern):
    """Only our own: TaskModel's tests run in test_task_model (as the base here they ran once more)."""
    return unittest.TestSuite(CancelledRun(n) for n in CancelledRun.__dict__ if n.startswith("test"))
