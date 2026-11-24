"""core/fetch: a download that breaks off keeps every byte that came, and the next attempt goes on from there — a stall
in the middle of a block, a server that answers a Range with the whole file, and a link that gives a little each time."""
import tests  # noqa: F401 — do not see the machine's settings (~/.config/hub-core), see tests/__init__.py
import hashlib
import http.server
import os
import tempfile
import threading
import unittest
from unittest import mock

import platforms
from core import fetch, nethttp

SIZE = 3 * (1 << 20) + 5000


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        srv = self.server
        srv.asked.append(self.headers.get("Range"))
        body, start, status, extra = srv.body, 0, 200, {}
        if self.headers.get("Range") and srv.ranges:
            start = int(self.headers["Range"].split("=")[1].rstrip("-"))
            status, extra = 206, {"Content-Range": f"bytes {start}-{len(body) - 1}/{len(body)}"}
        cap = srv.caps.pop(0) if srv.caps else None
        self.send_response(status)
        self.send_header("Content-Length", str(len(body) - start))
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        mine = body[start:]
        self.wfile.write(mine if cap is None else mine[:cap])
        self.wfile.flush()
        if cap is not None:   # the link goes silent: the client's timeout is what ends this attempt
            srv.release.wait(5)
        self.close_connection = True


class Resume(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hubresume-")
        self.dest = os.path.join(self.tmp, "code.tar.gz")
        self.body = os.urandom(SIZE)
        self.sha = hashlib.sha256(self.body).hexdigest()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.body, self.server.caps, self.server.ranges = self.body, [], True
        self.server.asked, self.server.release = [], threading.Event()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/f"
        real = nethttp.send     # a stall is felt after half a second, not a minute
        patch = mock.patch.object(fetch.nethttp, "send", lambda *a, **kw: real(*a, **{**kw, "timeout": 0.5}))
        patch.start()
        self.addCleanup(patch.stop)

    def tearDown(self):
        self.server.release.set()
        self.server.shutdown()
        self.server.server_close()
        platforms.current.rmtree(self.tmp)

    def download(self):
        return fetch.download(self.url, self.dest, self.sha, SIZE, "f", "again", pause=0.01)

    def part(self):
        return os.path.getsize(self.dest + ".part") if os.path.isfile(self.dest + ".part") else 0

    def test_a_link_that_gives_less_than_a_block_each_time_still_gets_there(self):
        # 400 KB and silence, over and over: nothing of a 1 MiB block ever completes
        self.server.caps = [400_000] * 12
        self.download()
        with open(self.dest, "rb") as f:
            self.assertEqual(f.read(), self.body)
        self.assertGreater(len(self.server.asked), fetch.TRIES)   # more than one run's tries: they went with progress

    def test_a_stall_keeps_the_bytes_of_the_unfinished_block(self):
        self.server.caps = [(1 << 20) + 300_000]
        with mock.patch.object(fetch, "ATTEMPTS_MAX", 1), self.assertRaises(fetch.Failed):
            self.download()
        self.assertEqual(self.part(), (1 << 20) + 300_000)

    def test_a_server_that_answers_a_range_with_the_whole_file_costs_the_part_nothing(self):
        self.server.ranges = False
        with open(self.dest + ".part", "wb") as f:
            f.write(self.body[:2_000_000])
        self.server.caps = [500_000]   # this answer breaks off before it is past what the part already holds
        with mock.patch.object(fetch, "TRIES", 1), self.assertRaises(fetch.Failed):
            self.download()
        self.assertEqual(self.part(), 2_000_000)

    def test_a_server_that_answers_a_range_with_the_whole_file_is_gone_through_to_the_end(self):
        self.server.ranges = False
        with open(self.dest + ".part", "wb") as f:
            f.write(self.body[:2_000_000])
        self.download()
        with open(self.dest, "rb") as f:
            self.assertEqual(f.read(), self.body)

    def test_a_whole_answer_that_is_not_the_file_of_the_part_starts_it_over(self):
        self.server.ranges = False
        with open(self.dest + ".part", "wb") as f:
            f.write(os.urandom(2_000_000))   # another file's bytes
        self.download()
        with open(self.dest, "rb") as f:
            self.assertEqual(f.read(), self.body)
        self.assertEqual(self.server.asked, ["bytes=2000000-", None])

    def test_a_run_past_its_time_starts_no_new_attempt_and_keeps_the_bytes(self):
        self.server.caps = [400_000] * 12
        with mock.patch.object(fetch, "RUN_SECONDS", 0), self.assertRaises(fetch.Failed):
            self.download()
        self.assertEqual(len(self.server.asked), 1)
        self.assertEqual(self.part(), 400_000)

    def test_progress_is_not_called_for_every_read_of_the_socket(self):
        calls = []
        fetch.download(self.url, self.dest, self.sha, SIZE, "f", "again", pause=0.01, progress=lambda d, t: calls.append(d))
        self.assertLessEqual(len(calls), SIZE // fetch.PROGRESS_STEP + 3)
        self.assertEqual(calls[-1], SIZE)

    def test_a_range_that_is_answered_goes_on_where_the_part_ends(self):
        with open(self.dest + ".part", "wb") as f:
            f.write(self.body[:2_000_000])
        self.download()
        self.assertEqual(self.server.asked, ["bytes=2000000-"])
        with open(self.dest, "rb") as f:
            self.assertEqual(f.read(), self.body)


if __name__ == "__main__":
    unittest.main()
