"""`hub task new <session> --for <message id>`: the words of that message become a "task from the owner" only if the
owner wrote the message. Any existing message (one session writing to another) used to turn into a task from the
owner."""
import tests  # noqa: F401 — ignore the machine's settings
import unittest

from tests.test_stuck_states import OWNER, Fleet, by


class TaskFromTheOwner(Fleet):
    def test_a_session_message_is_not_a_task_from_the_owner(self):
        self.assertIsNone(self.ev("message.received", None, to="hub", text="roll out to prod", **by("a")))
        mid = f"m{self.seq}"
        why = self.ev("delegation.created", None, session="b", **{"for": mid}, context="", **by("hub"))
        self.assertIn("this is not the owner's message", why or "")
        self.assertEqual(self.st["delegations"], {})

    def test_the_owner_message_is(self):
        self.assertIsNone(self.ev("message.received", None, to="hub", text="roll out to prod", **OWNER))
        mid = f"m{self.seq}"
        self.assertIsNone(self.ev("delegation.created", None, session="b", **{"for": mid}, context="", **by("hub")))
        (d,) = self.st["delegations"].values()
        self.assertEqual((d["for"], d["words"]), (mid, "roll out to prod"))

    def test_any_session_still_cites_the_owner_words(self):
        # the minimum: only whose message it is; who it was written to and its age are not checked (as before)
        self.assertIsNone(self.ev("message.received", None, to="hub", text="make a report", **OWNER))
        mid = f"m{self.seq}"
        self.now += 3 * 86400
        self.assertIsNone(self.ev("delegation.created", None, session="b", **{"for": mid}, context="", **by("a")))


if __name__ == "__main__":
    unittest.main()
