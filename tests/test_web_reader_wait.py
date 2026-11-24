"""The page waits for the journal reader lock with a limit. The journal poll holds it, and a re-read from the start
(startup, language change) takes minutes under load (25.10: load 120–250): requests to /api/about, /api/feed,
/api/history… hung without a deadline, clients timed out, and the sockets of departed clients (CLOSED, CLOSE_WAIT)
piled up until "Too many open files". Now, when a client leaves, the request is dropped and the socket closes at
once; if the lock is not obtained within READ_WAIT — 503 with Retry-After; if the lock is free — the reply is as before.
/api/feed does not take the lock at all: it answers from the posts of the latest completed poll (PublishedFeed)."""
import tests  # noqa: F401 — keep the machine's settings out (~/.config/hub-core), see tests/__init__.py
import http.client
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from modules.web import server as web_server
from modules.web.server import Web


class ReaderWait(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hubweb-")
        self.web = Web(self.state)
        self.server = self.web.serve(port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.web.stop.set()
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.state, ignore_errors=True)

    def ask(self, path, timeout):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            c.request("GET", path, headers={"Host": f"127.0.0.1:{self.port}"})
            r = c.getresponse()
            return r.status, r.getheader("Retry-After"), r.read()
        except OSError:
            return None
        finally:
            c.close()

    def held(self):
        end = time.time() + 10
        while not self.web.tail.ready:   # the first journal pass is done — from here the tail poll holds the lock
            self.assertLess(time.time(), end)
            time.sleep(0.05)
        self.assertTrue(self.web.tail.reading.acquire(timeout=10))
        released = []

        def release():
            if not released:
                released.append(1)
                self.web.tail.reading.release()
        self.addCleanup(release)
        return release

    def test_a_stuck_reader_answers_503_and_recovers(self):
        release = self.held()
        with mock.patch.object(web_server, "READ_WAIT", 1.0):
            started = time.monotonic()
            got = self.ask("/api/usage", 20)   # "About the hub" does not wait for the lock (a published snapshot)
            self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(got[:2], (503, "1"))
        self.assertIn("error", json.loads(got[2]))
        release()
        got = self.ask("/api/usage", 20)
        self.assertEqual(got[0], 200)

    @unittest.skipUnless(os.path.isdir("/dev/fd"), "descriptors are counted through /dev/fd")
    def test_clients_that_left_do_not_hold_sockets_until_the_lock(self):
        self.held()
        before = len(os.listdir("/dev/fd"))
        for path in ["/api/usage", "/api/history?session=x", "/api/timeline?session=x"] * 5:
            self.assertIsNone(self.ask(path, 0.3))   # the client did not wait and left
        end = time.time() + 5
        while (extra := len(os.listdir("/dev/fd")) - before) > 1:
            self.assertLess(time.time(), end, f"{extra} sockets of departed clients are still open")
            time.sleep(0.1)


class FirstPass(ReaderWait):
    """The first pass of a large journal holds the lock to the end (25.10: 96 MB under swap — over 10 min). While it
    runs, "About the hub" answers without journal numbers, and anyone else who needs the reader gets 503 at once, with
    no 15 s spent on a thread and a socket."""

    def setUp(self):
        with mock.patch("core.tail.Tail.run", lambda *a: None):   # the journal poll does not start: the first pass is "in progress"
            super().setUp()
        self.assertFalse(self.web.tail.ready)

    def test_about_answers_and_journal_paths_refuse_at_once(self):
        started = time.monotonic()
        code, _, body = self.ask("/api/about", 20)
        about = json.loads(body)
        self.assertEqual((code, about["core_started"], about["sessions"]), (200, None, None))
        self.assertIn("code", about)
        for path in ["/api/feed?plugin=x", "/api/history?session=x", "/api/timeline?session=x"]:
            self.assertEqual(self.ask(path, 20)[:2], (503, "1"), path)
        self.assertLess(time.monotonic() - started, 5)
        self.web.tail.poll()   # the pass is over
        self.assertEqual(self.ask("/api/feed?plugin=x", 20)[0], 200)

    def test_about_tells_the_cores_start_before_the_first_pass_is_over(self):
        # the update's watchdog (modules/update/trial.py) takes "the core came up" from /api/about: a hub that folds its
        # journal for minutes after a reboot is up, not "did not come up" (a rollback 180 s after the start)
        from core import journal
        from modules.web import pagekeyweb
        j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        j.append("core.started", None, {"pid": 1}, ts=1000.0)
        late = j.append("core.started", None, {"pid": 2}, ts=2000.0)
        j.append("message.received", None, {"text": 'about "type":"core.started"'})
        self.assertFalse(self.web.tail.published)
        code, _, body = self.ask("/api/about", 20)
        self.assertEqual((code, json.loads(body)["core_started"]), (200, late["ts"]))
        self.assertEqual(pagekeyweb.about_alive(self.web)["core_started"], late["ts"])
        self.assertIsNone(json.loads(body)["sessions"])   # the numbers of the journal still wait for the pass
        self.web.tail.poll()
        self.assertEqual(json.loads(self.ask("/api/about", 20)[2])["core_started"], self.web.tail.started)

    test_a_stuck_reader_answers_503_and_recovers = None
    test_clients_that_left_do_not_hold_sockets_until_the_lock = None


class FirstPassFails(ReaderWait):
    """The thread of the journal poll dies with an exception in the first pass: the page cannot read its journal, and
    that is not "the core came up" for the update's watchdog (it would keep a release whose page answers 503)."""

    def setUp(self):
        from core import journal
        from core.tail import Tail
        self.state = tempfile.mkdtemp(prefix="hubweb-")
        journal.Journal(os.path.join(self.state, "journal.jsonl")).append("core.started", None, {"pid": 1})
        with mock.patch.object(Tail, "poll", side_effect=RuntimeError("boom")), mock.patch("threading.excepthook"):
            self.web = Web(self.state)
            self.server = self.web.serve(port=0)
            end = time.time() + 10
            while not self.web.tail.failed:
                self.assertLess(time.time(), end)
                time.sleep(0.02)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    test_a_stuck_reader_answers_503_and_recovers = None
    test_clients_that_left_do_not_hold_sockets_until_the_lock = None

    def test_a_failed_pass_tells_no_start_and_the_watchdog_does_not_say_ok(self):
        from modules.update import trial
        from modules.web import pagekeyweb
        self.assertFalse(self.web.tail.published)
        code, _, body = self.ask("/api/about", 20)
        about = json.loads(body)
        self.assertEqual((code, about["core_started"]), (200, None))
        self.assertIsNone(pagekeyweb.about_alive(self.web)["core_started"])
        asked = {**pagekeyweb.about_alive(self.web), "web_started": time.time() + 1}
        waiting = {"status": "waiting", "started": 0, "to": "abcdef0123", "layer": None}
        asked["code"] = {"running": "abcdef0123"}
        self.assertNotEqual(trial.decide(waiting, asked, None, time.time()), "ok")
        self.assertEqual(trial.decide(waiting, {**asked, "core_started": time.time()}, None, time.time()), "ok")   # the same answer with a start


class PublishedFeed(FirstPass):
    test_about_tells_the_cores_start_before_the_first_pass_is_over = None

    def test_plugin_feed_does_not_wait_for_an_unpublished_journal_pass(self):
        from core import journal
        j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        first = j.append("ext.post", None, {"plugin": "inbox", "text": "First", "owner": True, "channel": "web"})
        self.web.tail.poll()
        expected = self.web.tail.reader.feed_since("inbox", 0)
        release = self.held()
        try:
            j.append("ext.post", None, {"plugin": "inbox", "text": "Second", "owner": True, "channel": "web"})
            with mock.patch.object(web_server, "READ_WAIT", 0.15):
                got = self.ask("/api/feed?plugin=inbox", 5)
            self.assertEqual(got[0], 200)
            self.assertEqual(json.loads(got[2]), {"posts": expected})
        finally:
            release()
        self.web.tail.poll()
        got = self.ask(f"/api/feed?plugin=inbox&since={first['seq']}", 5)
        self.assertEqual([p["text"] for p in json.loads(got[2])["posts"]], ["Second"])
        self.assertEqual(json.loads(self.ask("/api/feed?plugin=missing", 5)[2]), {"posts": []})

    def test_published_posts_are_detached_and_trim_only_on_publication(self):
        from core import journal, reducer
        j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        with mock.patch.object(reducer, "FEED_MAX", 2):
            for text in ("First", "Second"):
                j.append("ext.post", None, {"plugin": "inbox", "text": text, "owner": True, "channel": "web"})
            self.web.tail.poll()
            old = self.web.tail.feed_posts["inbox"]
            self.assertIsNot(old[0], self.web.tail.reader.state["feeds"]["inbox"][0])
            j.append("ext.post", None, {"plugin": "inbox", "text": "Third", "owner": True, "channel": "web"})
            self.web.tail.poll()
        self.assertEqual([p["text"] for p in old], ["First", "Second"])
        self.assertEqual([p["text"] for p in self.web.tail.feed_posts["inbox"]], ["Second", "Third"])

    def test_only_changed_channels_are_copied_again(self):
        from core import journal
        j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        for plugin in ("inbox", "notes"):
            j.append("ext.post", None, {"plugin": plugin, "text": "First", "owner": True, "channel": "web"})
        self.web.tail.poll()
        notes, inbox = self.web.tail.feed_posts["notes"], self.web.tail.feed_posts["inbox"]
        j.append("ext.post", None, {"plugin": "inbox", "text": "Second", "owner": True, "channel": "web"})
        self.web.tail.poll()
        self.assertIs(self.web.tail.feed_posts["notes"], notes, "a quiet channel keeps its published copy")
        self.assertIsNot(self.web.tail.feed_posts["inbox"], inbox)
        self.assertEqual([p["text"] for p in self.web.tail.feed_posts["inbox"]], ["First", "Second"])
        self.web.tail.reader = type(self.web.tail.reader)()   # a reread from the start: a new reader, the same posts
        self.web.tail.offset = 0
        self.web.tail.poll()
        self.assertIsNot(self.web.tail.feed_posts["notes"], notes, "a new reader copies every channel anew")
        self.assertEqual([p["text"] for p in self.web.tail.feed_posts["notes"]], ["First"])
        from core.tail import Tail
        clock = Tail(os.path.join(self.state, "journal.jsonl"), page=False)   # the scheduler's: it serves no feed
        clock.poll()
        self.assertEqual((clock.feed_posts, len(clock.reader.feed_since("notes", 0))), ({}, 1))


if __name__ == "__main__":
    unittest.main()
