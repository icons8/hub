"""The hub's servers start without a reverse lookup of their own address name. http.server calls socket.getfqdn(host) on
bind — a request to the name service; where the resolver is slow (2026-10-14: on a GitHub Mac runner — tens of seconds
for 127.0.0.1), the page, "Files" and the tests' fake Chrome would take just as long to start. Here the name lookup
fails or hangs; this file never goes to a real DNS."""
import tests  # noqa: F401
import contextlib
import http.client
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler
from unittest import mock

import platforms
from core import httpd
from modules.tailnet import access
from tests.fakes import cut

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE_CHROME = os.path.join(ROOT, "tests", "fakes", "fake_chrome.py")


def lookup(*_):
    raise AssertionError("the server asked the name service for its address")


class NoLookup(unittest.TestCase):
    def listens(self, make):
        with mock.patch.object(socket, "getfqdn", lookup):
            server = make()
        self.addCleanup(server.server_close)
        self.assertEqual((server.server_address[0], server.server_name), ("127.0.0.1", "127.0.0.1"))
        self.assertEqual(server.server_port, server.server_address[1])

    def test_the_shared_base(self):
        self.listens(lambda: httpd.Server(("127.0.0.1", 0), BaseHTTPRequestHandler))

    def test_the_hub_page_and_files_server(self):
        self.listens(lambda: access.Server(("127.0.0.1", 0), BaseHTTPRequestHandler))

    def test_the_server_of_a_module_process(self):
        # core/hubmod.serve: every module of the hub waits for the resolver at its start otherwise
        from core import hubmod
        folder = tempfile.mkdtemp(prefix="httpd-")
        self.addCleanup(platforms.current.rmtree, folder)
        with open(os.path.join(folder, "module.json"), "w") as f:
            json.dump({"name": "probe", "version": "1.0.0"}, f)
        env = {"HUB_MODULE_PORT": "0", "HUB_MODULE_SECRET": "x"}
        with mock.patch.dict(os.environ, env), mock.patch.object(socket, "getfqdn", lookup):
            server = hubmod.serve({}, folder=folder, block=False)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.assertEqual(server.server_name, "127.0.0.1")

    @cut.needs("gateway")
    def test_the_gateway(self):
        from modules.gateway import gateway
        with mock.patch.object(socket, "getfqdn", lookup):
            g = gateway.Gateway("http://127.0.0.1:9", env={}, report=lambda *a: None, clock=lambda: 1000.0)
            g.start()
        self.addCleanup(g.stop)
        self.assertEqual(g.server.server_name, "127.0.0.1")

    @cut.needs("gateway")
    def test_the_fake_api_of_the_gateway_self_check(self):
        # it starts before claude does; claude does not start here
        from modules.gateway import gateway
        with mock.patch.object(socket, "getfqdn", lookup):
            got = gateway.init_tools(["claude"], {}, False, run=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", ""))
        self.assertIsNone(got[0])


class NoNagle(unittest.TestCase):
    """A response goes out as two writes (headers, then the body): with Nagle's algorithm the second waits for the
    first to be acknowledged, and the browser delays that — ~40 ms on every request over an already open connection
    (2026-10-18: "Files" and the page — every file and API call except the first). The hub's servers send at once
    (TCP_NODELAY)."""

    def test_keep_alive_requests_do_not_wait_for_the_ack(self):
        class Two(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def do_GET(self):
                body = str(self.connection.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY) != 0).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = httpd.Server(("127.0.0.1", 0), Two)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        c = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        self.addCleanup(c.close)
        for _ in range(3):   # one connection
            c.request("GET", "/")
            self.assertEqual(c.getresponse().read(), b"True")


class SlowNames(unittest.TestCase):
    """A subprocess whose name service hangs (sitecustomize): the fake Chrome of the supervisor tests still answers."""

    def test_the_fake_chrome_does_not_wait_for_the_name_service(self):
        root = tempfile.mkdtemp(prefix="httpd-")
        self.addCleanup(shutil.rmtree, root, True)
        site = os.path.join(root, "site")
        os.makedirs(site)
        with open(os.path.join(site, "sitecustomize.py"), "w") as f:
            f.write("import socket, time\nsocket.getfqdn = lambda *a: time.sleep(120)\n")
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        proc = subprocess.Popen([sys.executable, FAKE_CHROME, f"--remote-debugging-port={port}",
                                 f"--user-data-dir={root}/profile"], env={**os.environ, "PYTHONPATH": site})
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        end = time.time() + 10
        while True:
            try:
                got = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1))
                break
            except OSError:
                self.assertLess(time.time(), end, "the fake Chrome did not answer in 10 s")
                time.sleep(0.1)
        self.assertEqual(got["Browser"], "FakeChrome/1.0")


class Overload(unittest.TestCase):
    """Under overload (2026-10-25: load 120–250, swap full) a request thread waits for a lock or the GIL for minutes, the
    client leaves on its own timeout and retries — while the departed client's socket stays open until the thread
    finishes its work: in ~15 min the page collected 289 descriptors against a limit of 256 (CLOSED and CLOSE_WAIT), then
    "Too many open files" and every request dropped. The process's servers together hold at most MAX_CONNECTIONS
    connections — each its own half (the page listens on both localhost and the tailnet), the excess gets 503 at once; a
    silent connection is closed after conn_timeout."""

    def setUp(self):
        limit = mock.patch.object(httpd, "MAX_CONNECTIONS", 8)   # 4 per server
        limit.start()
        self.addCleanup(limit.stop)
        self.release = threading.Event()
        self.addCleanup(self.release.set)

    def server(self, idle=None):
        release = self.release

        class Busy(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def do_GET(self):
                release.wait(10)   # the journal reader is busy
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        server = access.Server(("127.0.0.1", 0), Busy)
        if idle is not None:
            server.conn_timeout = idle
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return server

    @staticmethod
    def ask(port, timeout):
        """A request like a module with a read timeout: the answer or None (the client left)."""
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        try:
            c.request("GET", "/api/snapshot")
            r = c.getresponse()
            return r.status, r.getheader("Retry-After"), r.read()
        except OSError:
            return None
        finally:
            c.close()

    def test_clients_that_gave_up_do_not_pile_up_sockets(self):
        server = self.server()
        before = len(os.listdir("/dev/fd")) if os.path.isdir("/dev/fd") else None
        started = time.monotonic()
        got = [self.ask(server.server_port, 0.5) for _ in range(12)]
        # four wait for the busy reader and leave on timeout; the rest are refused at once, not given one more hanging socket
        self.assertEqual(got[:4], [None] * 4)
        self.assertEqual(got[4:], [(503, "1", b"")] * 8)
        self.assertLess(time.monotonic() - started, 4.5)
        if before is not None:
            time.sleep(0.2)
            self.assertLessEqual(len(os.listdir("/dev/fd")) - before, 4 + 1)
        self.release.set()   # the reader is free: there is room again
        end = time.time() + 5
        while (got := self.ask(server.server_port, 2)) != (200, None, b"ok"):
            self.assertLess(time.time(), end, got)
            time.sleep(0.05)

    def test_a_client_whose_request_comes_after_the_refusal_still_gets_503(self):
        # Windows: the accept thread answered 503 and closed before the request arrived; the close with its bytes
        # unread sent a reset, and Windows dropped the 503 already received — ConnectionAbortedError, 10 of 10
        server = self.server()
        self.assertEqual([self.ask(server.server_port, 0.5) for _ in range(4)], [None] * 4)   # every slot is taken
        got = []
        for _ in range(3):
            with socket.create_connection(("127.0.0.1", server.server_port), timeout=2) as s:
                time.sleep(0.05)   # the request comes a little after the connection
                try:
                    s.sendall(b"GET /api/snapshot HTTP/1.1\r\nHost: x\r\n\r\n")
                    got.append(s.recv(100).split(b"\r\n")[0])
                except OSError as e:
                    got.append(type(e).__name__)
        self.assertEqual(got, [b"HTTP/1.1 503 Service Unavailable"] * 3)

    def test_silent_connections_do_not_hold_up_the_accept_thread(self):
        # review: waiting for the request in the accept thread made 64 silent sockets (a browser's preconnects)
        # under overload ~13 s without accepting anybody
        server = self.server()
        self.assertEqual([self.ask(server.server_port, 0.5) for _ in range(4)], [None] * 4)   # every slot is taken
        silent = [socket.create_connection(("127.0.0.1", server.server_port), timeout=2) for _ in range(10)]
        for s in silent:
            self.addCleanup(s.close)
        started = time.monotonic()
        self.assertEqual(self.ask(server.server_port, 2), (503, "1", b""))
        self.assertLess(time.monotonic() - started, 0.5)

    def test_a_refused_connection_is_closed_once_its_client_leaves(self):
        # the drain holds a descriptor only while the client is still there; its own drain — not the process-wide count
        server = self.server()
        self.assertEqual([self.ask(server.server_port, 0.5) for _ in range(4)], [None] * 4)
        with mock.patch.object(httpd, "DRAIN", httpd.Drain()) as drain:
            self.assertEqual(self.ask(server.server_port, 2), (503, "1", b""))   # the client read the answer and left
            end = time.monotonic() + 10
            while drain.count and time.monotonic() < end:
                time.sleep(0.02)
            self.assertEqual(drain.count, 0)

    def test_a_drain_that_failed_lets_go_of_its_sockets_and_the_next_one_starts_again(self):
        # review: an unexpected error (select on Windows) ended the thread for good — the count stuck at
        # DRAIN_MAX and the sockets leaked
        real = httpd.selectors.DefaultSelector

        class Broken(real):
            def select(self, timeout=None):
                raise OSError("select failed")
        drain = httpd.Drain()
        held, client = socket.socketpair()
        self.addCleanup(client.close)
        with mock.patch.object(httpd.selectors, "DefaultSelector", Broken), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(drain.add(held))
            end = time.monotonic() + 10
            while (drain.count or drain.thread) and time.monotonic() < end:
                time.sleep(0.02)
        self.assertEqual((drain.count, drain.thread), (0, None))
        self.assertEqual(held.fileno(), -1)   # closed, not leaked
        again, other = socket.socketpair()
        self.addCleanup(again.close)
        self.assertTrue(drain.add(again))   # a new thread
        other.close()
        end = time.monotonic() + 10
        while drain.count and time.monotonic() < end:
            time.sleep(0.02)
        self.assertEqual(drain.count, 0)

    def test_each_listener_has_its_half_of_the_budget(self):
        """localhost and the tailnet are two servers of one process: each has half of MAX_CONNECTIONS. Together — no more
        than the computed limit, and a fully occupied tailnet (a slot is taken before the owner check) does not block localhost."""
        local, tailnet = self.server(), self.server()
        self.assertEqual([self.ask(tailnet.server_port, 0.3) for _ in range(4)], [None] * 4)
        self.assertEqual(self.ask(tailnet.server_port, 2), (503, "1", b""))
        self.assertEqual([self.ask(local.server_port, 0.3) for _ in range(4)], [None] * 4)   # localhost has its own
        self.assertEqual(self.ask(local.server_port, 2), (503, "1", b""))

    def test_clients_wait_while_the_accepting_thread_is_busy(self):
        """The accept thread cannot keep up (waits for the GIL): connections wait in the OS queue and are not reset after the fifth."""
        server = access.Server(("127.0.0.1", 0), BaseHTTPRequestHandler)   # serve_forever is not running — nothing accepts
        self.addCleanup(server.server_close)
        got = []
        for _ in range(20):
            try:
                s = socket.create_connection(("127.0.0.1", server.server_port), timeout=1)
                self.addCleanup(s.close)
                s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
                got.append(True)
            except OSError:
                got.append(False)
        self.assertEqual(got, [True] * 20)

    def test_a_silent_connection_is_closed(self):
        server = self.server(idle=0.3)
        s = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
        self.addCleanup(s.close)
        started = time.monotonic()
        self.assertEqual(s.recv(1), b"")   # the server closed it itself, it does not wait forever
        self.assertLess(time.monotonic() - started, 3)

    def test_a_client_that_does_not_read_frees_its_place(self):
        """The client asked for a big response and does not read (the phone fell asleep): the write stalls, after
        conn_timeout the thread abandons it, the slot in the budget is freed."""
        class Big(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def do_GET(self):
                body = b"x" * (64 << 20) if self.path == "/big" else b"ok"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = access.Server(("127.0.0.1", 0), Big)
        server.conn_timeout = 0.3
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        stuck = []
        for _ in range(4):   # the whole budget is held by those who do not read
            s = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            s.sendall(b"GET /big HTTP/1.1\r\nHost: x\r\n\r\n")
            stuck.append(s)
            self.addCleanup(s.close)
        end = time.time() + 10
        while (got := self.ask(server.server_port, 2)) != (200, None, b"ok"):
            self.assertLess(time.time(), end, got)
            time.sleep(0.1)


class Gone(unittest.TestCase):
    """httpd.gone: whether the client left — without waiting, received bytes stay in place."""

    def pair(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        a.settimeout(7)
        return a, b

    def test_waiting_client(self):
        a, _ = self.pair()
        self.assertFalse(httpd.gone(a))
        self.assertEqual(a.gettimeout(), 7)   # the socket timeout is unchanged

    def test_next_request_already_sent(self):
        a, b = self.pair()
        b.sendall(b"GET / HTTP/1.1\r\n")
        time.sleep(0.05)
        self.assertFalse(httpd.gone(a))
        self.assertEqual(a.recv(3), b"GET")   # peeked, not consumed

    def test_closed_client(self):
        a, b = self.pair()
        b.close()
        self.assertTrue(httpd.gone(a))


if __name__ == "__main__":
    unittest.main()


class Pieces(unittest.TestCase):
    """A socket timeout on sendall covers the whole call: a multi-megabyte page response to a slow client (a phone on the
    tailnet) would be cut off in the middle of conn_timeout. The page writes in chunks — the timeout applies to each."""

    def test_a_long_answer_is_written_in_pieces(self):
        sent = []

        class Sock:
            def sendall(self, b):
                sent.append(len(b))

            def fileno(self):
                return -1
        self.assertEqual(httpd.Writer(Sock()).write(b"x" * (httpd.CHUNK * 2 + 5)), httpd.CHUNK * 2 + 5)
        self.assertEqual(sent, [httpd.CHUNK, httpd.CHUNK, 5])

    def test_a_slow_reader_gets_the_whole_answer(self):
        # the client reads slower than conn_timeout allows for the whole response, but every chunk goes out in time — the
        # whole response arrives. 5 s, not 1: on macOS a 64 KB chunk to a reader with a 32 KB window waits up to 3 s (Mac
        # bridge probe; Linux — up to 0.5 s). 10 s on Windows: it takes ~4 MB of the response into the socket at
        # once, and the next chunk waits until this reader takes ~2 MB of it — 6.2 s
        from modules.web.server import Web
        state = tempfile.mkdtemp(prefix="hubslow-")
        self.addCleanup(shutil.rmtree, state, True)
        web = Web(state)
        server = web.serve(port=0)
        server.conn_timeout = 10.0 if os.name == "nt" else 5.0
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(web.stop.set)
        big = os.path.join(state, "big.js")
        with open(big, "wb") as f:
            f.write(b"x" * (16 << 20))
        s = socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=20)
        self.addCleanup(s.close)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32 << 10)
        with mock.patch("modules.web.server.STATIC", state):
            s.sendall(f"GET /static/big.js HTTP/1.1\r\nHost: 127.0.0.1:{server.server_address[1]}\r\n"
                      f"Connection: close\r\n\r\n".encode())
            got, t0 = 0, time.monotonic()
            while True:
                chunk = s.recv(16 << 10)
                if not chunk:
                    break
                got += len(chunk)
                if time.monotonic() - t0 < server.conn_timeout + 1:
                    time.sleep(0.05)   # slow for longer than conn_timeout: the whole response does not go out within it
        self.assertGreater(time.monotonic() - t0, server.conn_timeout)
        self.assertGreater(got, 16 << 20)
