"""Resending without a duplicate: client_id in /api/send. A client that got no reply (disconnect, timeout) sends the
same message again with the same id — the answer is 202, as the first time (on a refusal it would mark the delivered
message "not sent"), and the session has one message: the fold rejects the repeat, like modules' dedup_key
(core/modstate.py)."""
import tests  # noqa: F401 — keeps the machine's settings out
import json
import os
import shutil
import tempfile
import unittest

from core import flow, journal, modstate, reducer

WORD = {"to": "a", "text": "hi", "attachments": [], "channel": "web", "owner": True}


class Fold(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.put("session.requested", "a", session_id="s", cwd="/w")

    def put(self, kind, entity=None, ts=100, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts, "type": kind, "entity": entity,
                                                   "data": data})
        return why

    def test_a_repeat_from_the_page_is_refused_with_the_number_of_the_accepted_message(self):
        self.assertIsNone(self.put("message.received", **WORD, client_id="c-1"))
        mid = f"m{self.seq}"
        before = len(self.st["messages"])
        why = self.put("message.received", **{**WORD, "text": "hi (again)"}, client_id="c-1", ts=200)
        self.assertIn(f"already accepted — {mid}", why)
        self.assertEqual(len(self.st["messages"]), before)
        self.assertIsNone(self.put("message.received", **WORD, client_id="c-2"))   # another message has its own
        self.assertEqual(len(self.st["messages"]), before + 1)

    def test_a_day_later_the_same_id_is_a_new_message(self):
        self.put("message.received", **WORD, client_id="c-1")
        self.assertIsNone(self.put("message.received", **WORD, client_id="c-1", ts=100 + modstate.DEDUP_WINDOW))

    def test_only_the_owner_on_the_page_has_the_key_space(self):
        # client_id is checked only on the owner's page channel; the key of a module named web is its own, not shared
        self.put("message.received", **WORD, client_id="c-1")
        for other in ({"channel": "cli"}, {"channel": "telegram"}, {"owner": False}):
            for _ in range(2):
                self.assertIsNone(self.put("message.received", **{**WORD, **other}, client_id="c-1"), other)
        self.put("module.ready", module="web", version="1.0.0", points=[], launch="L", channel="core")
        self.assertIsNone(self.put("message.received", **{**WORD, "channel": "mod:web"}, dedup_key="c-1"))
        self.assertIn("c-1", self.st["web_dedup"]["a"])
        self.assertIn("c-1", self.st["mod_dedup"]["web"])

    def test_the_same_id_to_another_session_is_another_message(self):
        # a client may number messages with its own ids in each feed: the key is per addressee
        self.put("session.requested", "b", session_id="s-b", cwd="/w")
        self.put("message.received", **WORD, client_id="1")
        self.assertIsNone(self.put("message.received", **{**WORD, "to": "b"}, client_id="1"))
        self.assertIsNotNone(self.put("message.received", **{**WORD, "to": "b"}, client_id="1"))

    def test_ids_of_a_session_nobody_writes_to_any_more_leave_after_the_window(self):
        self.put("session.requested", "b", session_id="s-b", cwd="/w")
        self.put("message.received", **{**WORD, "to": "b"}, client_id="old", ts=0)
        self.put("message.received", **WORD, client_id="new", ts=modstate.DEDUP_WINDOW + 1)
        self.assertEqual(self.st["web_dedup"], {"a": {"new": self.st["web_dedup"]["a"]["new"]}})

    def test_only_an_accepted_message_takes_the_id(self):
        # a message the core refused for another reason does not take the id: a repeat is then a real send
        self.assertIsNotNone(self.put("message.received", **{**WORD, "fyi": True, "wake": True}, client_id="c-3"))
        self.assertNotIn("web_dedup", self.st)
        self.assertIsNone(self.put("message.received", **WORD, client_id="c-3"))

    def test_bad_id_is_refused_and_no_id_changes_nothing(self):
        self.assertIn("client_id", self.put("message.received", **WORD, client_id="x" * 129))
        self.assertIn("client_id", self.put("message.received", **WORD, client_id=5))
        for _ in range(2):
            self.assertIsNone(self.put("message.received", **WORD))
        self.assertNotIn("web_dedup", self.st)

    def test_a_module_cannot_rewrite_the_ids(self):
        from core import corewrite
        self.assertIn("web_dedup", corewrite.CLOSED)


class Replay(unittest.TestCase):
    """The journal as hubd writes it: the repeat is recorded and then rejected; a fold from scratch gives one message, the
same result."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="cid-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.j = journal.Journal(os.path.join(self.d, "journal.jsonl"))

    def write(self, kind, entity, data):
        st = reducer.fold(reducer.empty(), journal.read(self.j.path))
        ev = self.j.append(kind, entity, data, src="web")
        _, _, why = reducer.reduce(st, ev)
        if why:
            self.j.append("core.rejected", entity, {"of": ev["seq"], "type": kind, "reason": why})
        return why

    def test_replay_gives_one_message_and_the_poster_draws_one_arrow(self):
        self.write("session.requested", "a", {"session_id": "s", "cwd": "/w"})
        self.assertIsNone(self.write("message.received", None, {**WORD, "client_id": "c-1"}))
        self.assertIsNotNone(self.write("message.received", None, {**WORD, "client_id": "c-1"}))
        st = reducer.fold(reducer.empty(), journal.read(self.j.path))
        self.assertEqual([m["text"] for m in st["messages"].values()], ["hi"])
        with open(self.j.path, "rb") as f:
            got = [s for s in flow.signals(f.read().splitlines()) if s["kind"] == "message"]
        self.assertEqual(len(got), 1)   # "Flow" from the journal: a rejected repeat is not a second arrow


class Page(unittest.TestCase):
    def setUp(self):
        from tests.test_web import WebTest
        self.t = WebTest("test_send_goes_to_spool_with_web_channel")
        self.t.setUp()
        self.addCleanup(self.t.tearDown)
        self.t.seed()

    def test_the_id_goes_with_the_message_and_a_bad_one_is_refused(self):
        t = self.t
        self.assertEqual(t.req("POST", "/api/send", {"session": "a", "text": "x", "client_id": "c-1"})[0], 202)
        (ev,) = t.spooled()
        self.assertEqual(ev["data"]["client_id"], "c-1")
        for bad in ("", "x" * 129, 7, ["c"]):
            code, body = t.req("POST", "/api/send", {"session": "a", "text": "x", "client_id": bad})
            self.assertEqual(code, 400, bad)
            self.assertIn("client_id", json.loads(body)["error"])
        self.assertEqual(t.spooled(), [])
        self.assertEqual(t.req("POST", "/api/send", {"session": "a", "text": "x", "client_id": None})[0], 202)
        self.assertNotIn("client_id", t.spooled()[0]["data"])   # null is the same as no field (the client's field is optional)

    def test_a_repeat_is_answered_like_the_first_and_the_session_gets_one_message(self):
        t = self.t
        t.core_decides()
        for _ in range(2):
            self.assertEqual(t.req("POST", "/api/send", {"session": "a", "text": "hi", "client_id": "c-9"})[0], 202)
        import time
        for _ in range(100):
            if not os.listdir(os.path.join(t.state, "spool")):
                break
            time.sleep(0.05)
        st = reducer.fold(reducer.empty(), journal.read(t.j.path))
        self.assertEqual([m["text"] for m in st["messages"].values()], ["hi"])


if __name__ == "__main__":
    unittest.main()
