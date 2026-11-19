"""The page's event streams (/api/stream) build the snapshot together: one with_clock build and one parts json for the
same snapshot and language, not one per connection. 25.10: about 16 streams (apps, tabs, modules) were connected to a
live page and the snapshot was 16 MB; each stream built it itself on every event under the GIL, and the first event of
a new stream took 5.5 s."""
import tests  # noqa: F401 — hide the machine's settings (~/.config/hub-core), see tests/__init__.py
import http.client
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from core import httpd, journal
from modules.web import server as web_server
from modules.web.server import Web


class StreamShared(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hubweb-")
        self.j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        self.j.append("session.requested", "a", {"cwd": "/w", "session_id": "s-a", "channel": "cli", "owner": True})
        self.web = Web(self.state)
        self.web.tail.poll()
        self.server = self.web.serve(port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.builds = 0
        built = self.web.with_clock

        def counted(*a, **kw):
            self.builds += 1
            return built(*a, **kw)
        self.web.with_clock = counted

    def tearDown(self):
        self.web.stop.set()
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.state, ignore_errors=True)

    def first_event(self, out, i):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        c.request("GET", "/api/stream", headers={"Host": f"127.0.0.1:{self.port}"})
        r = c.getresponse()
        lines = []
        while len(lines) < 2:
            line = r.fp.readline().decode()
            if line.startswith(("event:", "data:")):
                lines.append(line)
        out[i] = lines
        c.close()

    def test_streams_share_one_build_of_the_same_snapshot(self):
        out = {}
        threads = [threading.Thread(target=self.first_event, args=(out, i)) for i in range(5)]
        # a build lives STREAM_GAP (1 s); under the load of a test run the fifth stream may come later and there will be
        # two builds
        with mock.patch.object(web_server, "STREAM_GAP", 60.0):
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
        self.assertEqual(len(out), 5)
        self.assertEqual({tuple(v) for v in out.values()}, {tuple(out[0])})   # everyone gets the same event
        self.assertEqual(out[0][0], "event: snapshot\n")
        self.assertEqual(self.builds, 1)


class StreamView(StreamShared):
    """Web.stream_view directly: what does not share a build, where JSON is not built, how long a stuck builder is
    waited for."""

    def setUp(self):
        super().setUp()
        self.dumps = 0
        parts = self.web.stream_parts

        def counted(full):
            self.dumps += 1
            return parts(full)
        self.web.stream_parts = counted

    def now(self):
        return self.web.tail.snapshot, self.web.tail.records

    def expire(self):
        v = self.web._view
        self.web._view = (*v[:3], v[3] - 10, *v[4:])

    def test_unchanged_key_does_not_serialize_even_after_expiry(self):
        snap, rec = self.now()
        key, parts = self.web.stream_view(snap, rec, "en")
        self.assertIsNotNone(parts)
        self.assertEqual((self.builds, self.dumps), (1, 1))
        self.assertEqual(self.web.stream_view(snap, rec, "en", seen=key), (key, None))   # fresh: neither a build nor JSON
        self.assertEqual((self.builds, self.dumps), (1, 1))
        self.expire()
        self.assertEqual(self.web.stream_view(snap, rec, "en", seen=key), (key, None))   # old: a comparison, but no JSON
        self.assertEqual((self.builds, self.dumps), (2, 1))
        self.assertEqual(self.web.stream_view(snap, rec, "en")[1], parts)   # another stream gets it from the same build
        self.assertEqual((self.builds, self.dumps), (2, 2))

    def test_new_snapshot_or_records_are_not_served_from_the_old_build(self):
        snap, rec = self.now()
        key, _ = self.web.stream_view(snap, rec, "en")
        self.j.append("session.requested", "b", {"cwd": "/w", "session_id": "s-b", "channel": "cli", "owner": True})
        self.web.tail.poll()
        snap2, rec2 = self.now()
        self.assertIsNot(snap2, snap)
        key2, parts2 = self.web.stream_view(snap2, rec2, "en")
        self.assertNotEqual(key2[0], key[0])
        self.assertIn('"b"', parts2["sessions"])
        self.web.stream_view(snap2, dict(rec2), "en")   # other module records of the same snapshot get their own build
        self.assertEqual(self.builds, 3)

    def test_the_core_state_taken_in_place_of_the_pages_own_is_news_without_a_new_event(self):
        snap, rec = self.now()
        key, _ = self.web.stream_view(snap, rec, "en")
        again = dict(snap)   # the same place in the journal, what the state holds is not the same (Tail.adopt_core_state)
        self.assertEqual(self.web.stream_view(again, rec, "en", seen=key), (key, None))   # without the mark the stream is silent
        key2, parts2 = self.web.stream_view(dict(snap), rec, "en", seen=key, rev=1)   # the published snapshot is a new one
        self.assertNotEqual(key2, key)
        self.assertIsNotNone(parts2)

    def test_a_build_from_before_the_stream_connected_is_followed_by_a_fresh_one(self):
        snap, rec = self.now()
        self.web.stream_view(snap, rec, "en")   # another stream built it
        since = time.monotonic()
        cfg = self.web.config
        self.web.config = lambda: {**cfg(), "title": "Renamed"}   # settings changed without a journal event
        key, parts = self.web.stream_view(snap, rec, "en", since=since)
        self.assertNotIn("Renamed", parts["hub_title"])   # the shared build is no older than STREAM_GAP
        self.assertEqual(self.builds, 1)
        self.expire()   # the stream will not ask again before STREAM_GAP
        while time.monotonic() <= since:   # ...so its next build is later than its connection, on the Windows clock too
            time.sleep(0.005)              # (~15.6 ms ticks: without this the build shares the tick and counts as older)
        key2, parts2 = self.web.stream_view(snap, rec, "en", seen=key, since=since)
        self.assertNotEqual(key2, key)   # the key changed: the stream will send the changed parts
        self.assertIn("Renamed", parts2["hub_title"])
        self.assertEqual(self.web.stream_view(snap, rec, "en", seen=key2, since=since), (key2, None))

    def test_a_build_started_on_the_same_clock_tick_as_the_stream_counts_as_older(self):
        # the Windows clock ticks in steps of ~15.6 ms: the build and the stream connection share one timestamp, while
        # the settings are already different
        snap, rec = self.now()
        self.web.stream_view(snap, rec, "en")
        key, _ = self.web.stream_view(snap, rec, "en", since=self.web._view[7])
        self.assertIsNone(key[0])   # marked: a fresh build follows

    def test_a_new_stream_catches_up_with_settings_changed_before_it_connected(self):
        # another stream built the snapshot; settings changed without a journal event; a new stream took that build, and
        # right after it, without waiting for an event, it gets fresh parts (before, only with the next journal event)
        with mock.patch.object(web_server, "STREAM_GAP", 3.0):   # the new stream reaches the shared build even under load
            self.first_event({}, 0)
            cfg = self.web.config
            self.web.config = lambda: {**cfg(), "title": "Renamed"}
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            c.request("GET", "/api/stream", headers={"Host": f"127.0.0.1:{self.port}"})
            r = c.getresponse()
            got = []
            try:
                while len(got) < 4:   # the snapshot and then a patch
                    line = r.fp.readline().decode()
                    if not line:
                        break   # the stream is closed
                    if line.startswith(("event:", "data:")):
                        got.append(line)
            except TimeoutError:
                pass
            c.close()
        self.assertEqual(len(got), 4, [x[:80] for x in got])
        self.assertNotIn("Renamed", got[1])   # the snapshot is from the shared build, otherwise the check below proves nothing
        self.assertEqual(got[2], "event: patch\n")
        self.assertIn("Renamed", got[3])

    def test_languages_are_not_shared(self):
        snap, rec = self.now()
        en = self.web.stream_view(snap, rec, "en")[1]["hub_title"]
        ru = self.web.stream_view(snap, rec, "ru")[1]["hub_title"]
        self.assertNotEqual(en, ru)
        self.assertEqual(self.web.stream_view(snap, rec, "en")[1]["hub_title"], en)
        self.assertEqual(self.builds, 3)

    def stall(self):
        go, inside = threading.Event(), threading.Event()
        built = self.web.with_clock

        def slow(*a, **kw):
            inside.set()
            go.wait(20)
            return built(*a, **kw)
        self.web.with_clock = slow
        self.addCleanup(go.set)
        snap, rec = self.now()
        threading.Thread(target=self.web.stream_view, args=(snap, rec, "en"), daemon=True).start()
        self.assertTrue(inside.wait(5))
        self.web.with_clock = built
        return go

    def test_a_departed_waiter_does_not_wait_for_a_stalled_builder(self):
        self.stall()
        snap, rec = self.now()
        started = time.monotonic()
        with self.assertRaises(httpd.Gone):
            self.web.stream_view(snap, rec, "en", alive=lambda: False)
        self.assertLess(time.monotonic() - started, 3)

    def test_a_stalled_builder_is_waited_for_a_bounded_time(self):
        self.stall()
        snap, rec = self.now()
        with mock.patch.object(web_server, "READ_WAIT", 1.0):
            started = time.monotonic()
            key, parts = self.web.stream_view(snap, rec, "en")
            self.assertLess(time.monotonic() - started, 4)
        self.assertIsNotNone(parts)
        # the next event of this and other streams comes without waiting again for the same stuck builder
        with mock.patch.object(web_server, "READ_WAIT", 30.0):
            started = time.monotonic()
            self.assertIsNotNone(self.web.stream_view(snap, rec, "en")[1])
            self.assertLess(time.monotonic() - started, 4)

    def test_sharing_resumes_when_the_stalled_builder_returns(self):
        go = self.stall()
        snap, rec = self.now()
        with mock.patch.object(web_server, "READ_WAIT", 1.0):
            self.web.stream_view(snap, rec, "en")
        go.set()
        end = time.time() + 10
        while self.web.view_stalled:
            self.assertLess(time.time(), end, "the returned collector did not clear the mark")
            time.sleep(0.05)
        builds = self.builds
        self.web.stream_view(snap, rec, "en")   # from the returned builder's build, without its own
        self.assertEqual(self.builds, builds)

    def test_a_late_stall_mark_does_not_stop_sharing(self):
        # the waiter marked "stuck" after the builder had already returned (a race): the lock is free, so the shared
        # build proceeds and clears the mark
        snap, rec = self.now()
        self.web.stream_view(snap, rec, "en")
        self.web.view_stalled = True
        builds = self.builds
        self.web.stream_view(snap, rec, "en")
        self.assertEqual(self.builds, builds)
        self.assertFalse(self.web.view_stalled)

    @unittest.skipUnless(os.path.isdir("/dev/fd"), "descriptors are counted through /dev/fd")
    def test_a_stream_client_that_left_frees_its_socket_while_the_builder_stalls(self):
        self.stall()
        before = len(os.listdir("/dev/fd"))
        for _ in range(5):   # the client did not wait for the first event and left
            c, r = http.client.HTTPConnection("127.0.0.1", self.port, timeout=0.3), None
            try:
                c.request("GET", "/api/stream", headers={"Host": f"127.0.0.1:{self.port}"})
                r = c.getresponse()
                r.fp.readline()
            except OSError:
                pass
            finally:
                if r is not None:
                    r.close()   # the response holds the socket through its own file: close that too
                c.close()
        end = time.time() + 5
        while (extra := len(os.listdir("/dev/fd")) - before) > 1:
            self.assertLess(time.time(), end, f"{extra} sockets of departed stream clients are open")
            time.sleep(0.1)

    test_streams_share_one_build_of_the_same_snapshot = None


if __name__ == "__main__":
    unittest.main()
