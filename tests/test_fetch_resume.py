"""core/fetch: a download that breaks off keeps every byte that came, and the next attempt goes on from there — a stall
in the middle of a block, a server that answers a Range with the whole file, and a link that gives a little each time."""
import tests  # noqa: F401 — do not see the machine's settings (~/.config/hub-core), see tests/__init__.py
import hashlib
import http.server
import os
import re
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
        body, start, end, status, extra = srv.body, 0, len(srv.body) - 1, 200, {}
        if self.headers.get("Range") and srv.ranges:
            first, _, last = self.headers["Range"].split("=")[1].partition("-")
            start, end = int(first), min(int(last), end) if last else end
            status, extra = 206, {"Content-Range": f"bytes {start}-{end}/{len(body)}"}
        mine = body[start:end + 1]
        if srv.silent > 0 or srv.limit and len(mine) > srv.limit:   # a middlebox that takes the whole answer before it lets a byte through
            srv.silent -= 1                      # and the answer is too big for the client to wait for
            srv.release.wait(5)
            self.close_connection = True
            return
        if srv.refusals:
            self.send_response(srv.refusals.pop(0))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        cap = srv.caps.pop(0) if srv.caps else None
        self.send_response(status)
        self.send_header("Content-Length", str(len(mine)))
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
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
        self.server.body, self.server.caps, self.server.ranges, self.server.limit = self.body, [], True, 0
        self.server.refusals, self.server.silent = [], 0
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
        with mock.patch.object(fetch, "ATTEMPTS_MAX", 1), self.assertRaises(fetch.Failed):
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

    def test_an_answer_too_big_to_wait_for_is_asked_in_smaller_ones(self):
        # the whole file, and every answer over 1.2 MB, comes after more silence than the socket's: nothing of it ever
        # arrives; the next attempts ask for less, and what they bring is kept
        self.server.limit = 1_200_000
        self.download()
        with open(self.dest, "rb") as f:
            self.assertEqual(f.read(), self.body)
        self.assertEqual(self.server.asked[0], None)
        self.assertTrue(any(re.fullmatch(r"bytes=\d+-\d+", a or "") for a in self.server.asked))   # a window of its own

    def test_a_whole_file_answered_to_a_window_that_is_shorter_than_pinned_is_not_this_file(self):
        self.server.ranges = False   # a proxy that drops the Range
        self.server.silent = 1       # the first answer is the silence that narrows the window
        with self.assertRaises(fetch.Failed) as e:
            fetch.download(self.url, self.dest, self.sha, SIZE + 5, "f", "again", pause=0.01)
        self.assertEqual(e.exception.why, "checksum")
        self.assertEqual(len(self.server.asked), 2)

    def test_a_refusal_is_no_silence_and_asks_for_no_smaller_window(self):
        self.server.refusals = [503, 503]
        self.download()
        self.assertEqual(self.server.asked, [None, None, None])

    def test_an_answer_that_is_too_big_at_every_size_is_a_failure_with_what_the_attempts_were(self):
        self.server.limit = 1000
        log = []
        with self.assertRaises(fetch.Failed):
            fetch.download(self.url, self.dest, self.sha, SIZE, "f", "again", pause=0.01, log=log.append)
        # the whole file, three narrower windows down to the least, then the tries of that size
        self.assertEqual(len(self.server.asked), 3 + fetch.TRIES)
        self.assertEqual(self.server.asked[-1], f"bytes=0-{fetch.WINDOW_MIN - 1}")
        self.assertEqual(self.part(), 0)
        self.assertEqual(len(log), len(self.server.asked))
        self.assertIn(f"window {fetch.WINDOW_MIN}", log[-1])

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
