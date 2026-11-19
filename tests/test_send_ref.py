"""A reply in words to a client reference or note: ref in /api/send. The client sends the id of the reference (or the
note of several references) the owner answered with text from its page; the core puts it into the message.received
data as is and does not interpret it — the module that keeps the references reads the journal and builds the thread.
The same path as todo (tests/test_send_todo.py)."""
import tests  # noqa: F401 — hide the machine's settings
import json
import unittest

from core import modstate, reducer


class Page(unittest.TestCase):
    def setUp(self):
        from tests.test_web import WebTest
        self.t = WebTest("test_send_goes_to_spool_with_web_channel")
        self.t.setUp()
        self.addCleanup(self.t.tearDown)
        self.t.seed()

    def test_the_reference_goes_with_the_message_as_is(self):
        t = self.t
        self.assertEqual(t.req("POST", "/api/send", {"session": "a", "text": "and the second one?", "ref": "n41",
                                                     "quote": "Refund for the order"})[0], 202)
        (ev,) = t.spooled()
        self.assertEqual(ev["data"]["ref"], "n41")
        self.assertEqual(ev["data"]["quote"], "Refund for the order")
        self.assertTrue(ev["data"]["owner"])
        self.assertNotIn("todo", ev["data"])

    def test_ref_and_todo_are_separate_fields(self):
        t = self.t
        self.assertEqual(t.req("POST", "/api/send", {"session": "a", "text": "x", "ref": "m7", "todo": "t3"})[0], 202)
        (ev,) = t.spooled()
        self.assertEqual((ev["data"]["ref"], ev["data"]["todo"]), ("m7", "t3"))

    def test_a_bad_reference_is_refused_and_null_is_like_no_field(self):
        t = self.t
        for bad in ("", "x" * (modstate.DEDUP_KEY_MAX + 1), 7, ["m1"], {"id": "m1"}, True):
            code, body = t.req("POST", "/api/send", {"session": "a", "text": "x", "ref": bad})
            self.assertEqual(code, 400, bad)
            self.assertIn("ref", json.loads(body)["error"])
        self.assertEqual(t.spooled(), [])
        self.assertEqual(t.req("POST", "/api/send", {"session": "a", "text": "x", "ref": None})[0], 202)
        self.assertNotIn("ref", t.spooled()[0]["data"])

    def test_a_client_without_the_field_sends_as_before(self):
        t = self.t
        self.assertEqual(t.req("POST", "/api/send", {"session": "a", "text": "x"})[0], 202)
        self.assertNotIn("ref", t.spooled()[0]["data"])

    def test_the_core_does_not_read_it(self):
        # the same text with and without the field is the same message for the core: addressee, delivery, card reply
        def fold(extra):
            st = reducer.empty()
            for i, (kind, ent, data) in enumerate([
                    ("session.requested", "a", {"session_id": "s", "cwd": "/w"}),
                    ("message.received", None, {"to": "a", "text": "yes", "attachments": [], "channel": "web",
                                                "owner": True, **extra})], 1):
                st, _, why = reducer.reduce(st, {"seq": i, "ts": 100, "type": kind, "entity": ent, "data": data})
                self.assertIsNone(why)
            return st
        a, b = fold({}), fold({"ref": "n41"})
        self.assertEqual(a["sessions"], b["sessions"])
        self.assertEqual({k: v for k, v in b["messages"]["m2"].items() if k != "ref"},
                         {k: v for k, v in a["messages"]["m2"].items() if k != "ref"})


if __name__ == "__main__":
    unittest.main()
