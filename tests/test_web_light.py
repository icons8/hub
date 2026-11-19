"""The page over a slow link (hub on another machine, 2026-10-11): responses and the stream are gzipped if the browser
accepts it; static files are checked by ETag (304 without a body) - a file edited on disk shows up at once, as before."""
import tests  # noqa: F401 - ignore the machine's settings (~/.config/hub-core), see tests/__init__.py
import codecs
import gzip
import http.client
import json
import os
import platform
import shutil
import sys
import tempfile
import threading
import time
import unittest
import zlib

from core import journal
from modules.web.server import Web


class Light(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hubweb-")
        self.j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        self.web = Web(self.state)
        self.server = self.web.serve(port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.web.stop.set()
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.state, ignore_errors=True)

    def get(self, path, **headers):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", path, headers={"Host": f"127.0.0.1:{self.port}", **headers})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r, data

    def test_json_is_gzipped_only_when_accepted(self):
        self.j.append("session.requested", "a", {"session_id": "s", "cwd": "/w"})
        self.web.tail.poll()
        r, plain = self.get("/api/snapshot")
        self.assertIsNone(r.getheader("Content-Encoding"))
        r, packed = self.get("/api/snapshot", **{"Accept-Encoding": "gzip, deflate, br"})
        self.assertEqual(r.getheader("Content-Encoding"), "gzip")
        self.assertIn("Accept-Encoding", r.getheader("Vary") or "")
        self.assertEqual(int(r.getheader("Content-Length")), len(packed))
        self.assertLess(len(packed), len(plain))
        self.assertEqual(json.loads(gzip.decompress(packed))["sessions"][0]["name"], "a")
        r, _ = self.get("/api/snapshot", **{"Accept-Encoding": "gzip;q=0, br"})
        self.assertIsNone(r.getheader("Content-Encoding"))

    def test_static_revalidates_by_etag(self):
        for path in ("/", "/static/js/main.js", "/vendor/mermaid.min.js"):
            r, body = self.get(path, **{"Accept-Encoding": "gzip"})
            self.assertEqual(r.status, 200, path)
            self.assertEqual(r.getheader("Content-Encoding"), "gzip", path)
            self.assertEqual(r.getheader("Cache-Control"), "no-cache", path)   # revalidate every time - fresh code at once
            tag = r.getheader("ETag")
            self.assertTrue(tag, path)
            r, body = self.get(path, **{"If-None-Match": tag, "Accept-Encoding": "gzip"})
            self.assertEqual((r.status, body), (304, b""), path)
            self.assertEqual(r.getheader("ETag"), tag)
            r, body = self.get(path, **{"If-None-Match": 'W/"other"'})
            self.assertEqual(r.status, 200, path)
            self.assertIsNone(r.getheader("Content-Encoding"), path)
            self.assertTrue(body)

    def test_stream_is_gzipped_event_by_event(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/api/stream", headers={"Host": f"localhost:{self.port}", "Accept-Encoding": "gzip"})
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader("Content-Encoding"), "gzip")
        z, text = zlib.decompressobj(31), ""
        utf8 = codecs.getincrementaldecoder("utf-8")()   # a read chunk may cut a character in half
        seeded, deadline = False, time.time() + 5
        while time.time() < deadline and '"name": "a"' not in text:
            text += utf8.decode(z.decompress(r.fp.read1(65536)))   # every event arrives at once, not on close
            if "event: snapshot" in text and not seeded:
                seeded = True
                self.j.append("session.requested", "a", {"session_id": "s", "cwd": "/w"})
                self.web.tail.poll()
        c.close()
        self.assertTrue(text.startswith("event: snapshot\ndata: {"), text[:80])
        self.assertIn("event: patch", text)
        self.assertIn('"name": "a"', text)

    def test_ranges_and_small_answers_stay_plain(self):
        r, _ = self.get("/api/flow?since=0", **{"Accept-Encoding": "gzip"})
        self.assertIsNone(r.getheader("Content-Encoding"))   # do not compress small responses



class Arenas(unittest.TestCase):
    """Page memory on Linux: one malloc arena per thread held freed memory (2026-10-15: 181 MB in 5 arenas) - the page
    server limits them to two at start; where there is no glibc, it does nothing."""
    def test_page_limits_glibc_arenas_at_start(self):
        from unittest import mock
        from modules.web import server
        with mock.patch.object(server, "_arenas") as arenas, \
                mock.patch.object(server, "Web", side_effect=RuntimeError("no need to go further")):
            with self.assertRaises(RuntimeError):
                server.main()
        arenas.assert_called_once_with()
        libc = mock.Mock()
        libc.mallopt.return_value = 1
        with mock.patch("ctypes.CDLL", return_value=libc):
            self.assertTrue(server._arenas())
        libc.mallopt.assert_called_once_with(-8, 2)

    def test_without_glibc_nothing_happens(self):
        from unittest import mock
        from modules.web import server
        for cdll in (mock.Mock(side_effect=OSError), mock.Mock(side_effect=TypeError),   # no libc; Windows
                     mock.Mock(return_value=mock.Mock(spec=[]))):                         # macOS: no mallopt
            with mock.patch("ctypes.CDLL", cdll):
                self.assertFalse(server._arenas())

    @unittest.skipUnless(sys.platform.startswith("linux") and platform.libc_ver()[0] == "glibc", "glibc only")
    def test_real_glibc(self):
        from modules.web import server
        self.assertTrue(server._arenas())


if __name__ == "__main__":
    unittest.main()
