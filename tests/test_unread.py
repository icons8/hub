"""Read / unread (owner decision 2026-09-30: the state of whether the last message is read or unread was missing —
the list showed "free, free, free" with no hint where something new arrived). New is a session's turn result (what
goes to the owner) that arrived after the owner last looked at the session on the page or wrote to it."""
import tests  # noqa: F401 — hide the machine's settings
import unittest

from core import reducer, views

OWNER = {"channel": "web", "owner": True}


class Unread(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("session.requested", "hub", cwd="/h", session_id="s-h", kind="hub", **OWNER)
        self.ev("session.requested", "a", cwd="/w", session_id="s-a", **OWNER)
        self.ev("session.started", "hub")
        self.ev("session.started", "a")

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 100 + self.seq, "type": type,
                                                   "entity": entity, "data": data})
        return why

    def unread(self):
        r = views.Reader()
        r.state = self.st
        return {s["name"]: s["unread"] for s in r.snapshot()["sessions"]}

    def turn(self, name, text):
        self.ev("turn.started", name, marker=None, prompt_head="x")
        self.ev("turn.result", name, text=text)

    def test_result_is_unread_until_seen(self):
        self.assertEqual(self.unread(), {"hub": False, "a": False})
        self.turn("a", "Done: MR !5, a yes is needed to merge.")
        self.assertEqual(self.unread()["a"], True)
        self.assertIsNone(self.ev("owner.seen", "a", seq=self.seq, **OWNER))
        self.assertEqual(self.unread()["a"], False)
        self.turn("a", "One more")
        self.assertEqual(self.unread()["a"], True)            # new after viewing — unread again

    def test_writing_to_the_session_means_read(self):
        self.turn("a", "Result")
        self.ev("message.received", None, to="a", text="ok, go on", **OWNER)
        self.assertEqual(self.unread()["a"], False)

    def test_empty_or_failed_turn_is_not_news(self):
        self.ev("turn.started", "a", marker=None, prompt_head="x")
        self.ev("turn.result", "a", text="", is_error=True)
        self.assertEqual(self.unread()["a"], False)

    def test_seen_is_the_owners_word_and_never_goes_back(self):
        self.turn("a", "Result")
        self.assertIsNotNone(self.ev("owner.seen", "a", seq=self.seq, channel="session", owner=False, sender="a"))
        self.assertEqual(self.unread()["a"], True)
        top = self.seq
        self.ev("owner.seen", "a", seq=top, **OWNER)
        self.ev("owner.seen", "a", seq=1, **OWNER)              # an old tab does not roll it back
        self.assertEqual(self.st["owner"]["seen"]["a"], top)
        self.assertIsNotNone(self.ev("owner.seen", "nope", seq=1, **OWNER))

    def test_watchers_guess_shows_only_after_reading_and_stays_until_the_owner_acts(self):
        # owner decision 2026-09-30: set the mark only after the blue dot is cleared and nothing was done — while
        # unread, the dot shows; read but not answered — "reply?"; answered the session — cleared
        self.turn("a", "Prod only after your approval of the rollout.")
        self.ev("watch.needs_owner", "a", p=0.9, ask="Approve the rollout", text="…", **OWNER)
        r = views.Reader()
        r.state = self.st
        a = {s["name"]: s for s in r.snapshot()["sessions"]}["a"]
        self.assertEqual((a["unread"], a["attention"]["ask"]), (True, "Approve the rollout"))  # the page will show the dot
        self.ev("owner.seen", "a", seq=self.seq, **OWNER)
        self.assertIn("attention", self.st["sessions"]["a"])                         # read — the mark stayed
        self.ev("message.received", None, to="a", text="ok", **OWNER)
        self.assertNotIn("attention", self.st["sessions"]["a"])

    def test_owner_dismisses_the_watchers_guess(self):
        # owner decision 2026-10-01: a click on "reply?" clears this flag
        self.turn("a", "Prod after your approval.")
        self.ev("watch.needs_owner", "a", p=0.9, ask="Approve the rollout", text="…", **OWNER)
        self.assertIsNotNone(self.ev("owner.dismissed", "a", channel="session", owner=False, sender="a"))
        self.assertIn("attention", self.st["sessions"]["a"])
        self.assertIsNone(self.ev("owner.dismissed", "a", **OWNER))
        self.assertNotIn("attention", self.st["sessions"]["a"])
        self.assertIsNotNone(self.ev("owner.dismissed", "nope", **OWNER))

    def test_rename_keeps_read_state(self):
        self.turn("a", "Result")
        self.ev("owner.seen", "a", seq=self.seq, **OWNER)
        self.st["sessions"]["a"]["state"] = "asleep"
        self.ev("session.renamed", "a", to="b", **OWNER)
        self.assertEqual(self.unread()["b"], False)


if __name__ == "__main__":
    unittest.main()
