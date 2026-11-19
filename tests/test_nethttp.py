"""core/nethttp: one request through the owner's proxy for the clients that used urllib (stats, conduit, the bus relay)
and those that built the request by hand (core/fetch, Cloudflare). Any failure is an OSError with a reason that holds no
bytes of a broken answer; NO_PROXY in either spelling still sends a host past the proxy. Only 127.0.0.1."""
import tests  # noqa: F401 — keep the machine's settings out
import http.server
import os
import socket
import threading
import unittest
from unittest import mock

from core import net, nethttp
from tests.fakes.proxy import alert_proxy, proxied


def serve(test, code=200, body=b"hello", headers=()):
    """An HTTP server on 127.0.0.1 that answers every request alike: (its address, [(method, path, headers, body)])."""
    got = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def answer(self):
            n = int(self.headers.get("Content-Length") or 0)
            got.append((self.command, self.path, dict(self.headers), self.rfile.read(n) if n else b""))
            self.send_response(code)
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        do_GET = do_POST = answer

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    test.addCleanup(srv.server_close)
    test.addCleanup(srv.shutdown)
    return f"http://127.0.0.1:{srv.server_port}", got


def listener(test):
    """A TCP port on 127.0.0.1 that counts the connections it takes and closes them: (port, a list that grows)."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    test.addCleanup(srv.close)
    came = []

    def run():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            came.append(1)
            conn.close()
    threading.Thread(target=run, daemon=True).start()
    return srv.getsockname()[1], came


class Request(unittest.TestCase):
    def test_status_headers_and_the_answer_up_to_the_limit(self):
        url, got = serve(self, 201, b"0123456789", [("X-Relay-Sig", "s-1")])
        status, head, body = nethttp.request("POST", url + "/v1/x?a=1", b'{"v":1}', {"Content-Type": "application/json"},
                                             5, 4)
        self.assertEqual((status, head.get("X-Relay-Sig"), body), (201, "s-1", b"0123"))
        self.assertEqual(got[0][:2], ("POST", "/v1/x?a=1"))
        self.assertEqual((got[0][2]["Content-Type"], got[0][3]), ("application/json", b'{"v":1}'))
        self.assertEqual(nethttp.request("GET", url, timeout=5)[2], b"0123456789")   # no limit — the whole answer

    def test_a_refusal_of_the_server_is_an_answer_not_a_failure(self):
        url, _ = serve(self, 404, b"no")
        self.assertEqual(nethttp.request("GET", url + "/x", timeout=5)[::2], (404, b"no"))

    def test_a_redirect_is_not_followed(self):
        url, got = serve(self, 302, b"", [("Location", "http://127.0.0.1:9/elsewhere")])
        self.assertEqual(nethttp.request("GET", url, timeout=5)[0], 302)
        self.assertEqual(len(got), 1)

    def test_not_an_http_address_is_an_oserror(self):
        for url in ("ftp://example.test/x", "https:///x", "https://example.test:port/x", "example.test"):
            with self.subTest(url=url), self.assertRaisesRegex(OSError, "not an http"):
                nethttp.request("GET", url, timeout=5)

    def test_nobody_listening_is_an_oserror(self):
        port, _ = listener(self)
        with self.assertRaises(OSError):
            nethttp.request("GET", f"http://127.0.0.1:{port}/", timeout=5)   # closed at once, no answer

    def test_a_proxy_that_listens_on_tls_is_an_oserror_without_the_alerts_bytes(self):
        # 2026-11-06: urllib read such a proxy's alert as a status line; BadStatusLine is not an OSError and its text is
        # the alert's bytes — here it is a TLS failure towards the proxy, and nothing of the answer gets into the text
        with proxied(alert_proxy(self)):
            with self.assertRaises(OSError) as e:
                nethttp.request("GET", "https://report.example.test/v1/hello", timeout=5)
        self.assertNotIn("\x15", str(e.exception))

    def test_a_broken_answer_is_named_not_quoted(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        self.addCleanup(srv.close)

        def run():
            conn, _ = srv.accept()
            with conn:
                conn.recv(4096)
                conn.sendall(b"\x15\x03\x03\x00\x02\x02\x16 not http\r\n\r\n")
                conn.shutdown(socket.SHUT_WR)
        threading.Thread(target=run, daemon=True).start()
        with self.assertRaisesRegex(OSError, r"^BadStatusLine$"):
            nethttp.request("GET", f"http://127.0.0.1:{srv.getsockname()[1]}/", timeout=5)


class PlainHttp(unittest.TestCase):
    """Plain http to an outside host goes through the machine's http proxy, as urllib sent it (net.connection sends
    plain http directly: a local server); the hub's own addresses stay direct."""

    def test_the_whole_address_goes_to_the_http_proxy_with_its_login(self):
        proxy, got = serve(self, 200, b"via proxy")
        with mock.patch.dict(os.environ, {"http_proxy": proxy.replace("http://", "http://u%40x:p%3Ay@"),
                                          "HTTP_PROXY": proxy.replace("http://", "http://u%40x:p%3Ay@"),
                                          "no_proxy": "", "NO_PROXY": ""}):
            status, _, body = nethttp.request("GET", "http://user:pw@gate.example.test:8080/a?b=1", timeout=5)
        self.assertEqual((status, body), (200, b"via proxy"))
        method, path, headers, _ = got[0]
        self.assertEqual((method, path), ("GET", "http://gate.example.test:8080/a?b=1"))   # no login of the target
        self.assertEqual(headers["Proxy-Authorization"], "Basic dUB4OnA6eQ==")   # u@x:p:y
        self.assertEqual(headers["Host"], "gate.example.test:8080")

    def test_the_hubs_own_address_stays_direct(self):
        url, got = serve(self, 200, b"direct")
        port, at_proxy = listener(self)
        with proxied(f"http://127.0.0.1:{port}"):
            self.assertEqual(nethttp.request("GET", url + "/x", timeout=5)[2], b"direct")
        self.assertEqual((len(at_proxy), got[0][1]), (0, "/x"))


class ProxySetting(unittest.TestCase):
    """The proxy variable the way urllib takes it: `host:port` without a scheme is an http:// proxy (urllib accepted it;
    urlsplit gave no host and http.client a TypeError — a traceback in stats, conduit and the relay); a setting with no
    host is a failure with a reason."""

    def test_host_and_port_without_a_scheme_is_an_http_proxy(self):
        port, at_proxy = listener(self)
        with proxied(f"127.0.0.1:{port}"):
            with self.assertRaises(OSError):   # the proxy closes at once: no CONNECT answer
                nethttp.request("GET", "https://outside.example.test/", timeout=5)
            self.assertEqual(net.proxy_for("https", "outside.example.test").geturl(), f"http://127.0.0.1:{port}")
        self.assertEqual(len(at_proxy), 1)

    def test_a_setting_with_no_host_is_an_oserror(self):
        with proxied("http://:3128"):
            with self.assertRaisesRegex(OSError, "not an address"):
                nethttp.request("GET", "https://outside.example.test/", timeout=5)


class NoProxy(unittest.TestCase):
    """A host in NO_PROXY goes directly, whichever spelling names it (POSIX keeps both, Windows has one variable); a
    host outside it goes to the proxy. The target name maps to 127.0.0.1, and the hub's own addresses are always
    direct, so a made-up name stands for an outside host."""
    HOST = "direct.example.test"

    def attempt(self, env):
        """(connections the proxy took, connections the target took); "{port}" in env — the target's port."""
        proxy_port, at_proxy = listener(self)
        target_port, at_target = listener(self)
        real = socket.getaddrinfo

        def resolve(host, *a, **kw):
            return real("127.0.0.1" if host == self.HOST else host, *a, **kw)
        with mock.patch.dict(os.environ), mock.patch.object(socket, "getaddrinfo", resolve):
            for k in ("https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY", "no_proxy", "NO_PROXY"):
                os.environ.pop(k, None)
            os.environ.update({"https_proxy": f"http://127.0.0.1:{proxy_port}",
                               **{k: v.format(port=target_port) for k, v in env.items()}})
            with self.assertRaises(OSError):   # both ends close at once: no answer either way
                nethttp.request("GET", f"https://{self.HOST}:{target_port}/", timeout=5)
        return len(at_proxy), len(at_target)

    def test_no_proxy_in_lowercase(self):
        self.assertEqual(self.attempt({"no_proxy": self.HOST}), (0, 1))

    def test_no_proxy_in_uppercase(self):
        self.assertEqual(self.attempt({"NO_PROXY": self.HOST}), (0, 1))

    def test_no_proxy_with_the_port(self):
        # urllib matched NO_PROXY against host:port as well; core/net asked about the bare host only
        self.assertEqual(self.attempt({"NO_PROXY": self.HOST + ":{port}"}), (0, 1))

    def test_a_host_outside_no_proxy_goes_to_the_proxy(self):
        self.assertEqual(self.attempt({"NO_PROXY": "other.example.test"}), (1, 0))


if __name__ == "__main__":
    unittest.main()
