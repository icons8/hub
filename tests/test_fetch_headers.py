"""core/fetch: a Retry-After or Content-Length that str.isdigit() takes and int() does not ("²") is a passing failure of the
download, not a ValueError out of it."""
import tests  # noqa: F401 — do not see the machine's settings (~/.config/hub-core), see tests/__init__.py
import hashlib
import os
import socketserver
import tempfile
import threading
import unittest

import platforms
from core import fetch


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        while self.rfile.readline().strip():
            pass
        self.wfile.write(self.server.answer)


class FetchHeaders(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hubfetch-")
        self.server = None

    def tearDown(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        platforms.current.rmtree(self.tmp)

    def serve(self, answer):
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.answer = answer
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.server.server_address[1]}/f"

    def broken(self, answer):
        url = self.serve(answer)
        with self.assertRaises(fetch.Failed) as caught:
            fetch.download(url, os.path.join(self.tmp, "f"), hashlib.sha256(b"x" * 10).hexdigest(), 10, "f", "again",
                           pause=0.01)
        return caught.exception

    def test_retry_after_that_is_not_ascii_digits_is_a_failed_attempt(self):
        self.assertEqual(self.broken(b"HTTP/1.1 503 Busy\r\nRetry-After: \xb2\r\nContent-Length: 0\r\nConnection: close\r\n\r\n").why,
                         "broken")

    def test_content_length_that_is_not_ascii_digits_is_a_failed_attempt(self):
        self.assertEqual(self.broken(b"HTTP/1.1 200 OK\r\nContent-Length: \xb2\r\nConnection: close\r\n\r\nabc").why,
                         "broken")


if __name__ == "__main__":
    unittest.main()
