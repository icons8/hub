"""The page on a large journal: its journal reader reads it from the start under its own lock (startup, rotation of
the wrong file, language change - tens of seconds, minutes under load and swap). Page requests do not wait for that
lock: what can be served comes from the snapshot published by the last poll, the rest gets 503 with Retry-After at
once; connections are capped, and while the journal is not fully read a request does not wait longer than READ_WAIT
(2026-10-25: waiting requests piled up, clients left, sockets stayed - Errno 24; the connection cap and the timeout
are in core/httpd.py)."""
import tests  # noqa: F401 - ignore the machine's settings
import http.client
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

from core import journal, views
from modules.web import server as web_server
from modules.web.server import Web


class Gate:
    """views.Reader.feed_all waits while it is closed: the journal reader stands mid-read from the start, holding its lock."""

    def __init__(self):
        self.open, self.inside, self.orig = threading.Event(), threading.Event(), views.Reader.feed_all
        gate = self

        def feed_all(reader, events):
            if events and not gate.open.is_set():
                gate.inside.set()
                gate.open.wait(30)
            return gate.orig(reader, events)
        self.patch = mock.patch.object(views.Reader, "feed_all", feed_all)

    def close(self):
        self.open.clear()
        self.inside.clear()


class DuringTheRead(unittest.TestCase):
    PATHS = ("/api/timeline?session=a&since=0", "/api/history?session=a", "/api/history/search?session=a&q=hello",
             "/api/feed?plugin=x", "/api/flow?since=0", "/api/usage", "/api/modules", "/api/subagent?session=a&tool=t1",
             "/api/send/receipt?session=a&client_id=c-1")

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hubreplay-")
        self.addCleanup(shutil.rmtree, self.state, True)
        self.j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        self.j.append("session.requested", "a", {"session_id": "s", "cwd": "/w"})
        self.j.append("session.started", "a", {"session_id": "s"})
        self.gate = Gate()
        self.gate.patch.start()
        self.addCleanup(self.gate.patch.stop)
        self.addCleanup(self.gate.open.set)
        self.web = Web(self.state)
        # waiting for the lock longer than the client timeout (5 s): the test sees a request waiting for it as a
        # drop, not as a response time
        self.web.READ_WAIT = 60
        patch = mock.patch.object(web_server, "READ_WAIT", 60)   # Handler.reading
        patch.start()
        self.addCleanup(patch.stop)
        self.server = self.web.serve(port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.web.stop.set)
        self.assertTrue(self.gate.inside.wait(10))   # the page's journal thread is mid-first-read

    def get(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", path, headers={"Host": f"127.0.0.1:{self.port}", "X-Hub": "1"})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, r.getheader("Retry-After"), json.loads(body) if body else None

    def release(self):
        self.gate.open.set()
        for _ in range(1000):
            if self.web.tail.ready:
                return
            time.sleep(0.01)
        self.fail("the journal was not read to the end")

    def test_the_first_read_answers_every_request_at_once(self):
        for path in self.PATHS:
            code, retry, out = self.get(path)
            self.assertEqual(code, 503, path)
            self.assertIn("error", out, path)
            if "receipt" not in path:
                self.assertEqual(retry, "1", path)
        # an action on a session is not "no such session" (a refusal the client will not retry) but "ask later"
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("POST", "/api/session/icon", json.dumps({"session": "a", "icon": "x"}),
                  {"Host": f"127.0.0.1:{self.port}", "X-Hub": "1", "Content-Type": "application/json"})
        r = c.getresponse()
        r.read()
        c.close()
        self.assertEqual((r.status, r.getheader("Retry-After")), (503, "1"))
        code, _, about = self.get("/api/about")   # version and paths at once; what it does not know yet is null
        self.assertEqual(code, 200)
        self.assertEqual((about["core_started"], about["sessions"]), (None, None))
        self.assertTrue(about["code"] is None or isinstance(about["code"], dict))
        self.release()
        code, _, tl = self.get("/api/timeline?session=a&since=0")
        self.assertEqual(code, 200)
        self.assertIn("items", tl)
        self.assertEqual(self.get("/api/about")[2]["sessions"], 1)
        self.assertEqual(self.get("/api/usage")[0], 200)

    def test_a_reread_from_the_start_keeps_the_last_snapshot(self):
        self.release()
        self.gate.close()
        # the journal is a different file, not from a rotation (a copy): the reader reads it from the start
        tmp = self.j.path + ".copy"
        shutil.copy(self.j.path, tmp)
        os.replace(tmp, self.j.path)
        self.j.append("session.requested", "b", {"session_id": "s-b", "cwd": "/w"})
        self.assertTrue(self.gate.inside.wait(10))
        self.assertFalse(self.web.tail.ready)
        code, _, snap = self.get("/api/snapshot")
        self.assertEqual((code, [s["name"] for s in snap["sessions"]]), (200, ["a"]))   # the previous snapshot
        self.assertEqual(self.get("/api/about")[2]["sessions"], 1)
        self.assertEqual(self.get("/api/timeline?session=a&since=0")[:2], (503, "1"))
        self.release()
        self.assertEqual(sorted(s["name"] for s in self.get("/api/snapshot")[2]["sessions"]), ["a", "b"])


class ResetReader(unittest.TestCase):
    """The reader was reset (the journal is not a continuation of what was read) and reading the new file to the end
    failed this time (another rotation happened meanwhile): until it is read, requests see "busy", not an empty reader."""

    def test_a_reset_reader_is_busy_until_it_is_read(self):
        from core.tail import Tail
        d = tempfile.mkdtemp(prefix="tail-reset-")
        self.addCleanup(shutil.rmtree, d, True)
        j = journal.Journal(os.path.join(d, "journal.jsonl"))
        j.append("session.requested", "a", {"session_id": "s", "cwd": "/w"})
        t = Tail(j.path)
        t.poll()
        self.assertEqual(t.peek(lambda r: sorted(r.state["sessions"]), 1), ["a"])
        tmp = j.path + ".copy"
        shutil.copy(j.path, tmp)
        os.replace(tmp, j.path)   # a different file, not from a rotation - read from the start
        real = os.fstat

        def moved(fd):   # another rotation happened meanwhile
            st = real(fd)
            return os.stat_result((st.st_mode, st.st_ino + 1) + tuple(st)[2:])
        with mock.patch("core.tail.os.fstat", moved):
            t.poll()
        self.assertIs(t.peek(lambda r: sorted(r.state["sessions"]), 1), Tail.BUSY)
        t.poll()
        self.assertEqual(t.peek(lambda r: sorted(r.state["sessions"]), 1), ["a"])

    def test_a_reset_reader_without_a_whole_line_yet_is_still_busy(self):
        from core.tail import Tail
        d = tempfile.mkdtemp(prefix="tail-reset-")
        self.addCleanup(shutil.rmtree, d, True)
        j = journal.Journal(os.path.join(d, "journal.jsonl"))
        j.append("session.requested", "a", {"session_id": "s", "cwd": "/w"})
        t = Tail(j.path)
        t.poll()
        with open(j.path + ".new", "w") as f:
            f.write('{"seq": 1, "type": "session.requested"')   # a new journal: the first line is still being written
        os.replace(j.path + ".new", j.path)
        t.poll()
        self.assertIs(t.peek(lambda r: sorted(r.state["sessions"]), 1), Tail.BUSY)
    def test_a_journal_gone_after_a_reset_is_still_busy(self):
        # the journal is swapped not by one rename (a copy, Windows: delete and rename) - it is absent mid-way: that is not an empty hub
        from core.tail import Tail
        d = tempfile.mkdtemp(prefix="tail-reset-")
        self.addCleanup(shutil.rmtree, d, True)
        j = journal.Journal(os.path.join(d, "journal.jsonl"))
        j.append("session.requested", "a", {"session_id": "s", "cwd": "/w"})
        t = Tail(j.path)
        t.poll()
        os.unlink(j.path)
        t.poll()
        self.assertIs(t.peek(lambda r: sorted(r.state["sessions"]), 1), Tail.BUSY)

    def test_a_new_hub_without_a_journal_is_ready(self):
        from core.tail import Tail
        d = tempfile.mkdtemp(prefix="tail-new-")
        self.addCleanup(shutil.rmtree, d, True)
        t = Tail(os.path.join(d, "journal.jsonl"))
        t.poll()
        self.assertEqual(t.peek(lambda r: sorted(r.state["sessions"]), 1), [])



if __name__ == "__main__":
    unittest.main()
