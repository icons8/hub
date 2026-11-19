"""The relay's connections at once: each holds a thread, so there is a cap for the whole process and one for an address;
over it a connection is refused at once with an unsigned 503 `connections` / `connections_addr` (not 429: a hub reads
an unsigned 429 as «rate»), without a thread and without a request read. A freed place is taken again; a long poll holds
its place for as long as it waits; reading one request (its line, headers and body) has a clock of its own, so a client
sending a byte at a time no longer keeps its place. Behind a trusted proxy every connection is the proxy's: the address's
cap counts the requests under way of the address the proxy names.

relay/server.py served in this process on a free port of 127.0.0.1 (the loopback is not one client: a proxy without
HUB_RELAY_TRUST_PROXY, a stand), the long poll and its signature replaced; an address from elsewhere through
Server.process_request on a socket pair."""
import tests  # noqa: F401 — keeps the machine's settings out
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests.test_hubbus import need_ssh
from tests.test_hubbus_s2 import server_module

FP = "hb1-" + "a" * 26


def until(test, seconds=5.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if test():
            return True
        time.sleep(0.01)
    return test()


def read_all(sock):
    got = b""
    while True:
        part = sock.recv(65536)
        if not part:
            return got
        got += part


def parse(raw):
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1]) if lines and lines[0] else 0
    headers = {k.strip().lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:])}
    return status, headers, body


def request(path, xff=None):
    extra = f"X-Forwarded-For: {xff}\r\n" if xff else ""
    return f"GET {path} HTTP/1.1\r\nHost: relay.example.test\r\n{extra}Connection: close\r\n\r\n".encode()


@need_ssh
class Connections(unittest.TestCase):
    def serve(self, **env):
        """The relay in a thread of this process with the module's constants as the test set them before."""
        patch = mock.patch.dict(os.environ, {"HUB_RELAY_POLLS": "1", **env})
        patch.start()
        self.addCleanup(patch.stop)
        box, ready = [], threading.Event()
        db = os.path.join(tempfile.mkdtemp(prefix="relay-c-"), "relay.sqlite3")
        t = threading.Thread(target=self.mod.serve, args=(db, "127.0.0.1", 0),
                             kwargs={"ready": lambda s: (box.append(s), ready.set())}, daemon=True)
        t.start()
        self.assertTrue(ready.wait(30))
        self.srv = box[0]
        self.port = self.srv.server_address[1]
        self.gate = threading.Event()
        self.addCleanup(t.join, 10)
        self.addCleanup(self.srv.shutdown)
        self.addCleanup(self.gate.set)
        relay = self.srv.RequestHandlerClass.relay
        self.addCleanup(relay.store.db.close)
        return relay

    def setUp(self):
        self.mod = server_module()

    def held(self):
        held = getattr(self.srv, "held", None)
        return held.n if held else -1

    def open(self, data=b""):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.addCleanup(s.close)
        if data:
            s.sendall(data)
        return s

    def ask(self, path, xff=None):
        """(status, headers, body, seconds) of one request on a new connection."""
        began = time.monotonic()
        s = self.open(request(path, xff))
        got = parse(read_all(s))
        return (*got, time.monotonic() - began)

    def long_polls(self, relay):
        """GET /v1/inbox waits until the test opens the gate: the signature is not what is checked here."""
        self.inside = threading.Semaphore(0)

        def inbox(req, wait):
            self.inside.release()
            self.gate.wait(20)
            return 200, {"items": []}
        relay.authenticate = lambda *a, **k: self.mod.Req(FP, "0" * 32, int(time.time()), 1)
        relay.inbox = inbox

    def refused(self, got, code):
        status, headers, body, seconds = got
        try:
            said = json.loads(body).get("code")
        except ValueError:
            said = body
        self.assertEqual((status, said), (503, code), got)
        self.assertNotIn("x-relay-sig", headers)       # unsigned: nothing of the request was read
        self.assertLess(seconds, 3)

    def test_over_the_cap_a_connection_is_refused_at_once_and_a_freed_place_is_taken_again(self):
        self.mod.CONNS = 3
        self.serve()
        idle = [self.open() for _ in range(3)]           # a connection that never sends a byte holds its place too
        until(lambda: self.held() == 3)
        self.refused(self.ask("/healthz"), "connections")
        idle[0].close()
        self.assertTrue(until(lambda: self.held() == 2), self.held())
        status, _, body, _ = self.ask("/healthz")
        self.assertEqual((status, body), (200, b"ok"))
        self.assertTrue(until(lambda: self.held() == 2), self.held())   # the answered one has given its place back

    def test_a_long_poll_holds_its_place_and_the_reading_clock_does_not_cut_it(self):
        self.mod.CONNS, self.mod.READ_FOR = 2, 0.3
        relay = self.serve()
        self.long_polls(relay)
        poll = self.open(request("/v1/inbox?wait=25"))
        self.assertTrue(self.inside.acquire(timeout=5))
        slow = self.open(b"GET /v1/nothing HTTP/1.1\r\nHost: re\r\n")   # its headers never end
        until(lambda: self.held() == 2)
        self.refused(self.ask("/healthz"), "connections")        # the long poll is in the cap
        self.assertEqual(read_all(slow), b"")                    # the reading clock closed it, unanswered...
        self.assertTrue(until(lambda: self.held() == 1), self.held())
        self.assertEqual(dict(relay.loose), {})                  # (what it read is no request: no 404, no failure)
        self.assertEqual(self.ask("/healthz")[0], 200)           # ...and its place is free
        self.gate.set()                                          # the long poll waited past the clock and answers
        status, _, body = parse(read_all(poll))
        self.assertEqual((status, json.loads(body)), (200, {"items": []}))

    def test_a_body_sent_slowly_is_cut_by_the_same_clock(self):
        self.mod.READ_FOR = 0.3
        relay = self.serve()
        slow = self.open(b"POST /v1/send HTTP/1.1\r\nHost: relay.example.test\r\nContent-Length: 100\r\n\r\n{")
        self.assertEqual(read_all(slow), b"")                    # cut, unanswered, before the test's socket's 5 s
        self.assertTrue(until(lambda: self.held() == 0), self.held())
        self.assertEqual(dict(relay.loose), {})

    def test_one_address_has_a_cap_of_its_own_and_others_go_on(self):
        self.mod.CONNS, self.mod.CONNS_ADDR = 50, 2
        self.serve()

        def came_from(addr):
            mine, theirs = socket.socketpair()
            self.addCleanup(mine.close)
            mine.settimeout(5)
            self.srv.process_request(theirs, (addr, 40000))
            return mine
        first = [came_from("203.0.113.5"), came_from("2001:db8:1:2::5"),
                 came_from("203.0.113.5"), came_from("2001:db8:1:2::9")]   # an IPv6 host — its /64
        until(lambda: self.held() == 4)
        for mine in (came_from("203.0.113.5"), came_from("2001:db8:1:2::77")):
            began = time.monotonic()
            self.refused((*parse(read_all(mine)), time.monotonic() - began), "connections_addr")
        other = came_from("203.0.113.6")
        other.sendall(request("/healthz"))
        self.assertEqual(parse(read_all(other))[0], 200)
        for _ in range(3):                                       # the loopback is no one address: not capped
            self.assertEqual(self.ask("/healthz")[0], 200)
        first[0].close()
        self.assertTrue(until(lambda: self.held() == 3), self.held())
        again = came_from("203.0.113.5")
        again.sendall(request("/healthz"))
        self.assertEqual(parse(read_all(again))[0], 200)

    def test_behind_a_trusted_proxy_the_cap_counts_the_requests_of_the_address_it_names(self):
        self.mod.CONNS_ADDR = 1
        self.long_polls(self.serve(HUB_RELAY_TRUST_PROXY="1"))
        poll = self.open(request("/v1/inbox?wait=25", xff="203.0.113.7"))
        self.assertTrue(self.inside.acquire(timeout=5))
        self.refused(self.ask("/v1/inbox?wait=25", xff="198.51.100.2, 203.0.113.7"), "connections_addr")
        self.assertEqual(self.ask("/v1/nothing", xff="203.0.113.8")[0], 404)   # another address goes on
        self.gate.set()
        self.assertEqual(parse(read_all(poll))[0], 200)
        self.assertEqual(self.ask("/v1/nothing", xff="203.0.113.7")[0], 404)   # its request done — its place free

    def test_a_private_network_is_a_proxy_not_one_client(self):
        self.assertIsNone(self.mod.counted("127.0.0.1"))
        for net in self.mod.UNCOUNTED:
            self.assertIsNone(self.mod.counted(str(next(net.hosts()))), net)
        self.assertIsNone(self.mod.counted("::ffff:127.0.0.1"))
        self.assertEqual(self.mod.counted("fd00::5"), "fd00::/64")
        self.assertEqual(self.mod.counted("203.0.113.5"), "203.0.113.5")
        self.assertEqual(self.mod.counted("2001:db8:1:2::5"), "2001:db8:1:2::/64")

    def test_a_short_file_limit_is_raised_or_takes_connections_off(self):
        import resource
        limits = {"now": (1024, 4096)}

        def setrlimit(_, value):
            limits["now"] = value
        with mock.patch.object(resource, "getrlimit", lambda _: limits["now"]), \
                mock.patch.object(resource, "setrlimit", setrlimit):
            self.assertEqual(self.mod.room_for(1024), 1024)
            self.assertEqual(limits["now"], (1024 + self.mod.REFUSED_MAX + 128, 4096))   # raised to what it needs
            limits["now"] = (1024, 1024)
            self.assertEqual(self.mod.room_for(1024), 1024 - self.mod.REFUSED_MAX - 128)

        def refused(*_):
            raise ValueError("not allowed")
        with mock.patch.object(resource, "getrlimit", lambda _: (512, resource.RLIM_INFINITY)), \
                mock.patch.object(resource, "setrlimit", refused):   # not raised: the soft limit is what there is
            self.assertEqual(self.mod.room_for(1024), 512 - self.mod.REFUSED_MAX - 128)

    def test_the_cap_is_never_below_two_connections_a_long_poll(self):
        self.mod.CONNS = 3
        self.serve(HUB_RELAY_POLLS="40")
        self.assertEqual(getattr(self.srv, "conns", None), 80)


if __name__ == "__main__":
    unittest.main()
