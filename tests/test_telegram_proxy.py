"""Telegram through a proxy with an https:// scheme (a contributor's PR of 03.10: urllib cannot use such a proxy and
fails with "Connection reset by peer"; so we use our own TLS-in-TLS tunnel). Refinements on acceptance: NO_PROXY is
honored as in urllib; a file download with a server error raises an exception instead of saving the error text as the
file."""
import tests  # noqa: F401 — hide the machine's settings
import base64
import os
import socket
import threading
import unittest
from unittest import mock

from modules.telegram import bot as tg
from tests.test_telegram import FakeBotApi


class Proxy(unittest.TestCase):
    def test_no_proxy_is_respected(self):
        env = {k: v for k, v in os.environ.items() if k.lower() not in ("https_proxy", "http_proxy", "no_proxy")}
        env.update(HTTPS_PROXY="https://proxy.example:3128", NO_PROXY="api.telegram.org")
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(tg._proxy_url("https", "api.telegram.org"))
            self.assertEqual(tg._proxy_url("https", "files.example").hostname, "proxy.example")

    def test_http_proxy_gets_connect_with_auth(self):
        """A proxy with an http:// scheme gets CONNECT to the target (the bot does not connect to the target directly)."""
        seen = []
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        self.addCleanup(srv.close)

        def proxy():
            conn, _ = srv.accept()
            with conn:
                data = b""
                while not data.endswith(b"\r\n\r\n"):
                    chunk = conn.recv(1)
                    if not chunk:
                        break
                    data += chunk
                seen.append(data.decode())
                conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
        t = threading.Thread(target=proxy, daemon=True)
        t.start()
        env = {k: v for k, v in os.environ.items() if k.lower() not in ("https_proxy", "http_proxy", "no_proxy")}
        env.update(HTTPS_PROXY=f"http://user:pass@127.0.0.1:{srv.getsockname()[1]}")
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(OSError):
                tg.request("GET", "https://api.telegram.org/bot1:x/getMe", timeout=5)
        t.join(5)
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].startswith("CONNECT api.telegram.org:443 "), seen[0])
        self.assertIn("Proxy-Authorization: Basic " + base64.b64encode(b"user:pass").decode(), seen[0])

    def test_download_error_is_an_error_not_a_file(self):
        api = FakeBotApi()
        self.addCleanup(api.server.shutdown)
        tg_api = tg.Api("1:x", base=api.base)
        self.assertEqual(tg_api.download("f1"), b"JPEGDATA")
        handler = api.server.RequestHandlerClass

        def missing(self):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"ok":false,"error_code":404,"description":"Not Found"}')
        with mock.patch.object(handler, "do_GET", missing):
            with self.assertRaises(tg.TgError):
                tg_api.download("f1")


if __name__ == "__main__":
    unittest.main()
