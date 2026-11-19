"""A reply in words to a client to-do item: todo in /api/send. The client sends the id of the item the owner
answered with text; the core puts it into the message.received data as is and does not interpret it — the to-do
module reads the journal."""
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

    def test_the_item_goes_with_the_message_as_is(self):
        t = self.t
        self.assertEqual(t.req("POST", "/api/send", {"session": "a", "text": "yes", "todo": "q12:question"})[0], 202)
        (ev,) = t.spooled()
        self.assertEqual(ev["data"]["todo"], "q12:question")
        self.assertTrue(ev["data"]["owner"])

    def test_a_bad_item_is_refused_and_null_is_like_no_field(self):
        t = self.t
        for bad in ("", "x" * (modstate.DEDUP_KEY_MAX + 1), 7, ["q1"], {"id": "q1"}, True):
            code, body = t.req("POST", "/api/send", {"session": "a", "text": "x", "todo": bad})
            self.assertEqual(code, 400, bad)
            self.assertIn("todo", json.loads(body)["error"])
        self.assertEqual(t.spooled(), [])
        self.assertEqual(t.req("POST", "/api/send", {"session": "a", "text": "x", "todo": None})[0], 202)
        self.assertNotIn("todo", t.spooled()[0]["data"])

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
        a, b = fold({}), fold({"todo": "q12"})
        self.assertEqual(a["sessions"], b["sessions"])
        self.assertEqual({k: v for k, v in b["messages"]["m2"].items() if k != "todo"},
                         {k: v for k, v in a["messages"]["m2"].items() if k != "todo"})


if __name__ == "__main__":
    unittest.main()
