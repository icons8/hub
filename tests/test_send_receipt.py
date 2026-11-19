"""Send receipt: /api/send waits for the core's decision and answers whether the message was accepted (seq, message id),
refused and why, or the core did not decide within decide_wait ("queued"); the outcome later — GET /api/send/receipt by
addressee and client_id: accepted (by the core's web_dedup), pending (the file is still in the inbox), refused (the
refusal memory of the page's journal reader), unknown."""
import tests  # noqa: F401 — keeps the machine's settings out
import json
import os
import threading
import time
import unittest
from urllib.parse import urlencode

from core import journal, spool, views


class Receipt(unittest.TestCase):
    def setUp(self):
        from tests.test_web import WebTest
        self.t = WebTest("test_send_goes_to_spool_with_web_channel")
        self.t.setUp()
        self.addCleanup(self.t.tearDown)
        self.t.seed()

    def send(self, **body):
        code, out = self.t.req("POST", "/api/send", {"session": "a", "text": "hi", **body})
        return code, json.loads(out)

    def receipt(self, session="a", client_id="c-1"):
        code, out = self.t.req("GET", "/api/send/receipt?" + urlencode({"session": session, "client_id": client_id}))
        return code, json.loads(out)

    def refusing(self, reason, code=None):
        """Instead of hubd: every inbox fact goes to the journal followed by its rejection (like daemon.rejection:
        reason, code, req)."""
        stop, box = threading.Event(), os.path.join(self.t.state, "spool")

        def run():
            while not stop.is_set():
                for path, e in spool.take(box):
                    ev = self.t.j.append(e["type"], e["entity"], e["data"], src=e.get("src", "web"))
                    self.t.j.append("core.rejected", e["entity"], {
                        "of": ev["seq"], "type": e["type"], "reason": reason, **({"code": code} if code else {}),
                        **{k: e["data"][k] for k in ("req",) if e["data"].get(k)}})
                    os.unlink(path)
                time.sleep(0.02)
        th = threading.Thread(target=run, daemon=True)
        th.start()
        self.t.web.decide_wait = 3

        def done():
            stop.set()
            th.join()
        self.t.core_stop = done

    def test_accepted_answers_the_number_and_the_message_id(self):
        self.t.core_decides()
        code, got = self.send(client_id="c-1")
        self.assertEqual(code, 202)
        self.assertEqual((got["ok"], got["receipt"], got["client_id"]), (True, "accepted", "c-1"))
        self.assertEqual(got["id"], f"m{got['seq']}")
        self.assertNotIn("already", got)

    def test_a_repeat_answers_the_first_message(self):
        # the client got no reply and repeated: the same message, the first one's number, no second message
        self.t.core_decides()
        _, first = self.send(client_id="c-1")
        code, again = self.send(client_id="c-1")
        self.assertEqual(code, 202)
        self.assertEqual((again["receipt"], again["already"], again["seq"], again["id"]),
                         ("accepted", True, first["seq"], first["id"]))
        st = self.state()
        self.assertEqual([m["text"] for m in st["messages"].values()], ["hi"])

    def test_refusal_is_409_with_the_core_reason_and_code(self):
        self.refusing("session a was deleted: the message cannot be delivered", "gone")
        code, got = self.send(client_id="c-2")
        self.assertEqual(code, 409)
        self.assertEqual((got["ok"], got["receipt"], got["client_id"], got["code"]), (False, "refused", "c-2", "gone"))
        self.assertIn("cannot be delivered", got["error"])
        self.assertIsInstance(got["seq"], int)
        # and later, by the receipt: the same reason
        _, later = self.receipt(client_id="c-2")
        self.assertEqual((later["receipt"], later["code"], later["seq"]), ("refused", "gone", got["seq"]))
        self.assertIn("cannot be delivered", later["error"])

    def test_not_decided_in_time_is_queued_and_the_receipt_says_pending_then_accepted(self):
        self.t.web.decide_wait = 0   # hubd is down
        code, got = self.send(client_id="c-3")
        self.assertEqual(code, 202)
        self.assertEqual((got["ok"], got["receipt"], got["queued"], got["client_id"]), (True, "queued", True, "c-3"))
        self.assertEqual(self.receipt(client_id="c-3"), (200, {"receipt": "pending"}))
        self.assertEqual(self.receipt(client_id="c-other")[1], {"receipt": "unknown"})
        self.assertEqual(self.receipt(session="b", client_id="c-3")[1], {"receipt": "unknown"})   # the key is per addressee
        self.t.core_decides()   # hubd is back
        for _ in range(100):
            _, now = self.receipt(client_id="c-3")
            if now["receipt"] != "pending":
                break
            time.sleep(0.05)
        self.assertEqual(now["receipt"], "accepted")
        self.assertEqual(now["id"], f"m{now['seq']}")

    def test_a_file_gone_from_the_spool_without_a_decision_is_unknown(self):
        self.t.web.decide_wait = 0
        self.send(client_id="c-4")
        self.t.spooled()   # the inbox file vanished without reaching the journal
        self.assertEqual(self.receipt(client_id="c-4")[1], {"receipt": "unknown"})

    def test_a_repeat_of_an_accepted_message_to_a_removed_session_is_still_accepted(self):
        # the reply is lost, the session was removed meanwhile: what was accepted stays accepted, not "no such session"
        self.t.core_decides()
        _, first = self.send(client_id="c-5")
        self.t.j.append("session.ended", "a", {"reason": "prompt_input_exit"})
        self.t.j.append("session.remove_requested", "a", {"channel": "web", "owner": True})
        self.t.settle()
        self.assertEqual(self.t.snap()["sessions"], [])
        code, again = self.send(client_id="c-5")
        self.assertEqual(code, 202)
        self.assertEqual((again["receipt"], again["already"], again["id"]), ("accepted", True, first["id"]))
        self.assertEqual(self.send(client_id="c-new")[0], 202)   # a new one: the core passes it to the hub
        self.assertEqual(self.receipt(client_id="c-5")[1]["receipt"], "accepted")

    def test_a_busy_journal_reader_does_not_hold_the_send(self):
        # the page's journal reader rereads the journal from the start (startup, rotation, language change) and holds the
        # lock — a send does not wait for it longer than READ_WAIT, the receipt answers 503 "ask later"
        self.t.web.decide_wait = 0
        self.t.web.READ_WAIT = 0.2
        with self.t.web.tail.reading:
            t0 = time.monotonic()
            code, got = self.send(client_id="c-6")
            self.assertEqual((code, got["receipt"]), (202, "queued"))
            code, out = self.receipt(client_id="c-6")
            self.assertEqual(code, 503)
            self.assertIn("error", out)
            self.assertLess(time.monotonic() - t0, 3)

    def test_bad_receipt_query_is_400(self):
        for q in ("", "session=a", "client_id=c", "session=a&client_id=" + "x" * 129):
            code, out = self.t.req("GET", "/api/send/receipt?" + q)
            self.assertEqual(code, 400, q)
            self.assertIn("error", json.loads(out))

    def test_receipt_path_belongs_to_the_core(self):
        from core import modclaims
        self.assertTrue(modclaims.core_path("/api/send/receipt"))

    def state(self):
        from core import reducer
        return reducer.fold(reducer.empty(), journal.read(self.t.j.path))


class LightPoll(unittest.TestCase):
    """The receipt reads the journal from a page request only by its tail: the first read and a reread after rotation
    belong to the Tail.run thread (tens of seconds on a big journal), a request does not start them."""

    def test_peek_does_not_start_a_full_read(self):
        import shutil
        import tempfile
        from core.tail import Tail
        d = tempfile.mkdtemp(prefix="tail-")
        self.addCleanup(shutil.rmtree, d, True)
        j = journal.Journal(os.path.join(d, "journal.jsonl"))
        j.append("session.requested", "a", {"session_id": "s", "cwd": "/w"})
        t = Tail(j.path)
        self.assertIs(t.peek(lambda r: list(r.state["sessions"]), 1, poll=True), Tail.BUSY)   # the first read is not done here
        t.poll()
        j.append("session.requested", "b", {"session_id": "s-b", "cwd": "/w"})
        self.assertEqual(t.peek(lambda r: sorted(r.state["sessions"]), 1, poll=True), ["a", "b"])   # the tail is
        with t.reading:
            self.assertIs(t.peek(lambda r: 1, 0.05), Tail.BUSY)


class ReaderRefusals(unittest.TestCase):
    """The journal reader's refusal memory: an owner's message from the page with a client_id that was rejected -> the
    reason and code from the next core.rejected; accepted ones, another channel and messages without client_id are not in it."""

    def feed(self, r, events):
        r.feed_all(events)

    def test_refusal_is_remembered_by_addressee_and_key(self):
        r = views.Reader()
        word = {"to": "a", "text": "x", "channel": "web", "owner": True, "attachments": []}
        evs = [
            {"seq": 1, "ts": 1, "type": "session.requested", "entity": "a", "data": {"session_id": "s", "cwd": "/w"}},
            {"seq": 2, "ts": 2, "type": "message.received", "entity": None, "data": {**word, "client_id": "k"}},
            {"seq": 3, "ts": 2, "type": "core.rejected", "entity": None,
             "data": {"of": 2, "type": "message.received", "reason": "not allowed", "code": "c"}},
            {"seq": 4, "ts": 3, "type": "message.received", "entity": None,
             "data": {**word, "channel": "cli", "client_id": "k2"}},
            {"seq": 5, "ts": 3, "type": "core.rejected", "entity": None,
             "data": {"of": 4, "type": "message.received", "reason": "not allowed"}},
        ]
        self.feed(r, evs)
        self.assertEqual(r.refusal("a", "k"), {"error": "not allowed", "code": "c", "seq": 2})
        self.assertIsNone(r.refusal("a", "k2"))   # not from the page
        self.assertIsNone(r.refusal("b", "k"))

    def test_the_core_refusal_counts_even_if_this_reader_folded_the_message_as_accepted(self):
        # the core's core.rejected decides: the reader's state may have diverged from the core (snapshot, another fold version)
        r = views.Reader()
        word = {"to": "a", "text": "x", "channel": "web", "owner": True, "attachments": [], "client_id": "k"}
        r.feed({"seq": 1, "ts": 1, "type": "session.requested", "entity": "a", "data": {"session_id": "s", "cwd": "/w"}})
        r.feed({"seq": 2, "ts": 2, "type": "message.received", "entity": None, "data": word}, "accepted")
        r.feed({"seq": 3, "ts": 2, "type": "core.rejected", "entity": None,
                "data": {"of": 2, "type": "message.received", "reason": "not allowed"}})
        self.assertEqual(r.refusal("a", "k"), {"error": "not allowed", "seq": 2})

    def test_memory_is_bounded(self):
        r = views.Reader()
        word = {"to": "a", "text": "x", "channel": "web", "owner": True, "attachments": []}
        evs, seq = [], 0
        for i in range(views.REFUSALS_KEEP + 5):
            seq += 2
            evs.append({"seq": seq - 1, "ts": 1, "type": "message.received", "entity": None,
                        "data": {**word, "to": "nobody", "client_id": f"k{i}"}})
            evs.append({"seq": seq, "ts": 1, "type": "core.rejected", "entity": None,
                        "data": {"of": seq - 1, "type": "message.received", "reason": "no such session"}})
        self.feed(r, evs)
        self.assertIsNone(r.refusal("nobody", "k0"))
        self.assertIsNotNone(r.refusal("nobody", f"k{views.REFUSALS_KEEP + 4}"))
        self.assertLessEqual(len(r.refused), views.REFUSALS_KEEP)


class BusyReader(unittest.TestCase):
    """What the core accepted is read without the journal read lock — from what the last poll published (Tail.dedup):
    under load a poll holds the lock for seconds (waiting for the GIL behind threads that encode the snapshot), and the
    receipt of an already accepted message answered 503 for minutes on end."""
    setUp, send, receipt = Receipt.setUp, Receipt.send, Receipt.receipt

    def test_an_accepted_message_is_accepted_while_the_reader_is_busy(self):
        self.t.core_decides()
        _, first = self.send(client_id="c-7")
        self.assertEqual(first["receipt"], "accepted")
        self.t.settle()
        self.t.web.READ_WAIT = 0.2
        real, waited = self.t.web.tail.peek, []
        self.t.web.tail.peek = lambda *a, **k: (waited.append(a), real(*a, **k))[1]   # waiting for the lock only through peek
        with self.t.web.tail.reading:
            self.assertEqual(self.receipt(client_id="c-7"),
                             (200, {"receipt": "accepted", "seq": first["seq"], "id": first["id"]}))
            code, again = self.send(client_id="c-7")   # a repeat after a disconnect — the first one's number
            self.assertEqual((code, again["receipt"], again["already"], again["seq"], again["id"]),
                             (202, "accepted", True, first["seq"], first["id"]))
            self.assertEqual(waited, [])   # the accepted one is answered without waiting for the lock (neither receipt nor repeat)
            self.assertEqual(self.receipt(client_id="c-other")[0], 503)   # not accepted — as before: ask later

    def test_a_message_accepted_after_the_last_poll_is_found_once_the_lock_is_free(self):
        # a message accepted after the last journal poll, with the lock busy, is not yet in what was published: 503
        # "ask later" (not unknown and not pending — the client would not put the words back in the field);
        # accepted once the lock is free
        self.t.core_decides()
        self.t.web.READ_WAIT = 0.2
        with self.t.web.tail.reading:
            _, first = self.send(client_id="c-8")
            self.assertEqual(first["receipt"], "accepted")
            self.assertEqual(self.receipt(client_id="c-8")[0], 503)
        code, got = self.receipt(client_id="c-8")
        self.assertEqual((code, got["receipt"], got["seq"]), (200, "accepted", first["seq"]))


if __name__ == "__main__":
    unittest.main()
