"""Module endpoints on the page (Module API, spec §2.2): /api/mod/<name>/… — a proxy to the module process with the same
login as the page, a mandatory X-Hub: 1, the body and the response streamed (not read into memory), with limits; HTML
views /mod/<name>/view/… — without X-Hub, in a CSP sandbox and without the owner's word; the module puts the owner's
word into the inbox through the mod:<name> channel. The module process is tests/fakes/fake_http_module.py, started the
same way hub-up starts it (core/modrun.launch)."""
import tests  # noqa: F401 — do not see the machine's settings
import hashlib
import http.client
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
from unittest import mock

from core import guards, journal, modrun, modules, reducer, spool, tools
from modules.web import modproxy
from modules.web.server import Web
from tests.fakes.procs import kill

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakes", "fake_http_module.py")


class ModHttp(unittest.TestCase):
    """A page on a temporary journal and a fake module with endpoints: the process is started and ready (module.ready in the journal)."""
    http = None   # the manifest's http field
    caps = ("http",)

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="modhttp-")
        self.addCleanup(shutil.rmtree, self.state, True)
        self.j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        self.log_path = os.path.join(self.state, "module.log")
        self.procs = []
        self.addCleanup(self.stop_modules)
        self.web = Web(self.state)
        self.web.decide_wait = 0
        self.server = self.web.serve(port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.stop_web)

    def stop_web(self):
        self.web.stop.set()
        self.server.shutdown()
        self.server.server_close()

    def stop_modules(self):
        for p in self.procs:
            kill(p.pid)
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                pass

    def module(self, name="fake", caps=None, http=None, ready=True):
        d = os.path.join(self.state, "mods", name)
        os.makedirs(d, exist_ok=True)
        man = {"name": name, "version": "1.0.0", "api": "^2.1", "run": [sys.executable, FAKE, "."],
               "capabilities": list(self.caps if caps is None else caps)}
        if http or self.http:
            man["http"] = http or self.http
        self.assertEqual(modules.validate(man), [])
        with open(os.path.join(d, "module.json"), "w") as f:
            json.dump(man, f)
        want = {"name": name, "cmd": man["run"], "cwd": d, "version": "1.0.0", "order": 0, "budget": {},
                "capabilities": man["capabilities"], "key": "k"}
        os.makedirs(modrun.run_dir(self.state), exist_ok=True)
        modrun._write(os.path.join(modrun.run_dir(self.state), f"{name}.want.json"), want)

        def spawn(cmd, cwd, env):
            p = subprocess.Popen(cmd, cwd=cwd, env={**env, "FAKE_MODULE_LOG": self.log_path},
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.procs.append(p)
            return p
        proc = modrun.launch(self.state, name, want, spawn)
        run = modrun.run_of(self.state, name)
        end = time.time() + 15
        while True:
            try:
                socket.create_connection(("127.0.0.1", run["port"]), 0.2).close()
                break
            except OSError:
                self.assertLess(time.time(), end, "the module process is not listening")
                time.sleep(0.05)
        if ready:
            self.j.append("module.ready", name, {"module": name, "version": "1.0.0", "points": [],
                                                 "launch": run["launch"], "channel": "core", "owner": False})
        self.web.tail.poll()
        return proc

    def req(self, method, path, body=None, headers=None, host=None, timeout=10):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        h = {"Host": host or f"127.0.0.1:{self.port}", **(headers or {})}
        c.request(method, path, body, h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data, r

    def api(self, method, path, body=None, headers=None, **kw):
        return self.req(method, path, body, {"X-Hub": "1", **(headers or {})}, **kw)

    def logged(self, event=None):
        try:
            with open(self.log_path, encoding="utf-8") as f:
                got = [json.loads(x) for x in f if x.strip()]
        except FileNotFoundError:
            return []
        return [g for g in got if event is None or g["event"] == event]

    def requests_seen(self):
        return [g for g in self.logged("request") if not g["path"].startswith("/hook/")]

    def until(self, pred, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end:
            got = pred()
            if got:
                return got
            time.sleep(0.05)
        return pred()

    def spooled(self):
        return [e for _, e in spool.take(os.path.join(self.state, "spool"))]

    def raw(self, head, body=b""):
        """A request over a raw socket: headers as is (no Content-Length from http.client)."""
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        s.sendall(head.encode() + body)
        return s

    @staticmethod
    def status_of(s):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
        return int(data.split(b" ", 2)[1]) if data.startswith(b"HTTP/") else None, data


class WithoutModules(ModHttp):
    def test_paths_of_modules_answer_as_before_when_there_is_no_module(self):
        # a hub without modules: module endpoints did not appear — the responses are the same as before them
        self.assertEqual(self.api("GET", "/api/mod/x/y")[:2], (404, '{"error":"No such page"}'.encode()))
        self.assertEqual(self.req("GET", "/mod/x/view/a")[:2], (404, '{"error":"No such page"}'.encode()))
        code, msg = self.web.act("/api/mod/x/y", {})
        st, body, _ = self.api("POST", "/api/mod/x/y", b"{}", {"Content-Type": "application/json"})
        self.assertEqual((st, json.loads(body)), (code, {"error": msg}))
        self.assertEqual(self.req("POST", "/api/mod/x/y", b"{}")[:2],
                         (403, '{"error":"The request is not from the hub page"}'.encode()))
        for method in ("PUT", "PATCH", "DELETE"):
            self.assertEqual(self.api(method, "/api/mod/x/y")[0], 501)
        self.assertFalse(os.path.exists(modrun.run_dir(self.state)))

    def test_module_known_by_another_name_does_not_change_foreign_paths(self):
        self.module()
        self.assertEqual(self.api("GET", "/api/mod/other/y")[:2], (404, '{"error":"No such page"}'.encode()))
        self.assertEqual(self.api("PUT", "/api/stream")[0], 501)


class Entry(ModHttp):
    def test_only_from_the_page_entry_and_with_x_hub(self):
        self.module()
        self.assertEqual(self.req("GET", "/api/mod/fake/echo", host="evil.example:80")[0], 403)
        self.assertEqual(self.req("GET", "/api/mod/fake/echo")[0], 403)                 # no X-Hub
        self.assertEqual(self.req("POST", "/api/mod/fake/echo", b"x")[0], 403)
        self.assertEqual(self.req("GET", "/mod/fake/view/page", host="evil.example:80")[0], 403)
        self.assertEqual(self.requests_seen(), [])
        self.assertEqual(self.api("GET", "/api/mod/fake/echo")[0], 200)

    def test_cors_preflight_is_not_answered(self):
        # a foreign site sends X-Hub only after a preflight check — the core does not let it through
        self.module()
        self.assertEqual(self.req("OPTIONS", "/api/mod/fake/echo", headers={
            "Origin": "http://evil.example", "Access-Control-Request-Headers": "x-hub"})[0], 501)
        self.assertEqual(self.requests_seen(), [])

    def test_module_without_the_http_right_has_no_routes(self):
        self.module(caps=[])
        st, body, _ = self.api("GET", "/api/mod/fake/echo")
        self.assertEqual(st, 404)
        self.assertIn("http", json.loads(body)["error"])
        self.assertEqual(self.requests_seen(), [])

    def test_not_ready_or_suspended_module_answers_503(self):
        self.module(ready=False)
        st, body, _ = self.api("GET", "/api/mod/fake/echo")
        self.assertEqual(st, 503)
        self.assertIn("not ready", json.loads(body)["error"])
        run = modrun.run_of(self.state, "fake")
        self.j.append("module.ready", "fake", {"module": "fake", "version": "1.0.0", "points": [],
                                               "launch": run["launch"], "channel": "core", "owner": False})
        for _ in range(modrun.FAILS):
            self.j.append("module.error", "fake", {"module": "fake", "hook": "x", "kind": "timeout", "error": "e",
                                                   "channel": "core", "owner": False})
        self.web.tail.poll()
        st, body, _ = self.api("GET", "/api/mod/fake/echo")
        self.assertEqual(st, 503)
        self.assertIn("suspended", json.loads(body)["error"])
        self.assertEqual(self.requests_seen(), [])


class Proxy(ModHttp):
    def test_request_reaches_the_module_as_is_with_secret_and_owner_flag(self):
        self.module()
        st, body, r = self.api("POST", "/api/mod/fake/echo?q=1&w=2", b'{"a": 1}',
                               {"Content-Type": "application/json", "X-Hub-Secret": "forged", "X-Hub-Owner": "0",
                                "X-Hub-Anything": "x", "X-Client": "c"})
        self.assertEqual(st, 200)
        got = json.loads(body)
        self.assertEqual((got["method"], got["path"], got["size"]), ("POST", "/api/mod/fake/echo?q=1&w=2", 8))
        self.assertEqual(got["sha256"], hashlib.sha256(b'{"a": 1}').hexdigest())
        h = got["headers"]
        self.assertEqual(h["x-hub-secret"], modrun.run_of(self.state, "fake")["secret"])
        self.assertEqual((h["x-hub-owner"], h["x-client"], h["content-type"]), ("1", "c", "application/json"))
        self.assertNotIn("x-hub-anything", h)
        self.assertTrue(all(g["secret_ok"] for g in self.requests_seen()))
        self.assertEqual(r.getheader("Content-Type"), "application/json")

    def test_a_request_of_a_module_view_is_not_the_owners_word(self):
        # the page passes a module's HTML view request (modview.js) with X-Hub-View: 1 — to the module without X-Hub-Owner
        self.module()
        st, body, _ = self.api("GET", "/api/mod/fake/echo", headers={"X-Hub-View": "1"})
        h = json.loads(body)["headers"]
        self.assertEqual((st, h.get("x-hub-view")), (200, "1"))
        self.assertNotIn("x-hub-owner", h)
        st, body, _ = self.api("GET", "/api/mod/fake/echo", headers={"X-Hub-View": "0"})
        self.assertEqual(json.loads(body)["headers"].get("x-hub-owner"), "1")

    def test_the_hubs_sign_in_never_reaches_the_module(self):
        # what signs the owner in to the page (the hub's cookie, Basic auth of a proxy in front, Cloudflare Access) is
        # the hub's: a module that got it could open the page itself; the module's own cookies and other headers go on
        self.module()
        st, body, _ = self.api("GET", "/api/mod/fake/echo", headers={
            "Authorization": "Basic dXNlcjpwYXNz", "X-Client": "c",
            "Cookie": "hub_main=page-mark; CF_Authorization=edge-mark; CF_AppSession=app-mark; theme=dark; "
                      "CF_Binding=b; hub_maps=other-hub-mark; Hub_layout=wide; cf_filter=open; =nameless; mod_seen=2"})
        h = json.loads(body)["headers"]
        self.assertEqual(st, 200)
        self.assertNotIn("authorization", h)
        self.assertEqual((h.get("cookie"), h.get("x-client")),
                         ("theme=dark; Hub_layout=wide; cf_filter=open; mod_seen=2", "c"))
        st, body, _ = self.api("GET", "/api/mod/fake/echo", headers={"Cookie": "CF_Authorization=edge-mark"})
        self.assertEqual(st, 200)
        self.assertNotIn("cookie", json.loads(body)["headers"])

    def test_a_modules_answer_cannot_clear_or_plant_the_sign_in(self):
        # the answer comes from the hub's own origin: Set-Cookie of the hub's or Access's cookie, or Clear-Site-Data,
        # would sign the owner out (or plant a cookie under the hub's name); the module's own cookies go to the browser
        self.module()
        st, _, r = self.api("GET", "/api/mod/fake/cookies")
        self.assertEqual(st, 200)
        self.assertEqual(r.headers.get_all("Set-Cookie"),
                         ["mod_seen=2; Path=/", "Hub_layout=wide; Path=/", "cf_filter=open"])
        self.assertEqual((r.getheader("Clear-Site-Data"), r.getheader("WWW-Authenticate")), (None, None))

    def test_cloudflare_access_headers_never_reach_the_module(self):
        # a request through Cloudflare Access carries its token; the page checked it (modules/cloudflare/remote.py),
        # the module gets neither it nor a service token's id and secret
        h = mock.Mock(headers={"Cf-Access-Jwt-Assertion": "jwt-mark", "CF-Access-Client-Id": "id-mark",
                               "cf-access-client-secret": "secret-mark", "Cf-Access-Authenticated-User-Email": "a@b.test",
                               "Authorization": "Bearer header-mark", "Cookie": "CF_Authorization=edge-mark; theme=dark",
                               "Cf-Ray": "ray-1", "Accept": "text/plain"})
        head = modproxy.Proxy.head(None, h, "GET", "api", {"port": 1, "secret": "module-mark"}, 0, "/api/mod/x/y")
        for mark in (b"jwt-mark", b"id-mark", b"secret-mark", b"a@b.test", b"header-mark", b"edge-mark"):
            self.assertNotIn(mark, head)
        for line in (b"Cookie: theme=dark", b"Cf-Ray: ray-1", b"Accept: text/plain", b"X-Hub-Secret: module-mark"):
            self.assertIn(line + b"\r\n", head)

    def test_every_method_goes_through(self):
        self.module()
        for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            st, body, _ = self.api(method, "/api/mod/fake/echo", b"" if method in ("GET", "DELETE") else b"z")
            self.assertEqual((st, json.loads(body)["method"]), (200, method))

    def test_module_answer_status_passes_through(self):
        self.module()
        st, body, _ = self.api("GET", "/api/mod/fake/nothing-here")
        self.assertEqual((st, json.loads(body)), (404, {"error": "no such route"}))
        self.assertEqual(self.spooled(), [])   # the module's answer, not its failure

    def test_connection_stays_usable_after_a_sized_answer(self):
        self.module()
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        for _ in range(3):
            c.request("GET", "/api/mod/fake/echo", headers={"X-Hub": "1"})
            r = c.getresponse()
            self.assertEqual(r.status, 200)
            r.read()
        c.request("GET", "/api/snapshot")
        self.assertEqual(c.getresponse().status, 200)
        c.close()


class Body(ModHttp):
    http = {"max_body": 4 << 20}

    def test_body_without_length_is_411_and_over_the_limit_413(self):
        self.module()
        s = self.raw(f"POST /api/mod/fake/echo HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nX-Hub: 1\r\n"
                     "Transfer-Encoding: chunked\r\n\r\n", b"3\r\nabc\r\n0\r\n\r\n")
        self.assertEqual(self.status_of(s)[0], 411)
        s.close()
        s = self.raw(f"POST /api/mod/fake/echo HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nX-Hub: 1\r\n\r\n")
        self.assertEqual(self.status_of(s)[0], 411)
        s.close()
        st, body, _ = self.api("POST", "/api/mod/fake/echo", b"x" * ((4 << 20) + 1))
        self.assertEqual(st, 413)
        self.assertIn("4 MB", json.loads(body)["error"])
        self.assertEqual(self.requests_seen(), [])
        self.assertEqual(self.api("POST", "/api/mod/fake/echo", b"x" * (4 << 20))[0], 200)

    def test_body_streams_to_the_module_before_the_client_finished(self):
        # the body does not accumulate in the page's memory: the first chunk is at the module while the second is not yet sent
        self.module()
        half = b"a" * (1 << 20)
        s = self.raw(f"POST /api/mod/fake/first HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nX-Hub: 1\r\n"
                     f"Content-Length: {2 * len(half)}\r\n\r\n", half)
        self.assertTrue(self.until(lambda: self.logged("first")), "the module did not get the start of the body before the send ended")
        s.sendall(half)
        code, data = self.status_of(s)
        head, _, rest = data.partition(b"\r\n\r\n")
        size = int(next(x for x in head.split(b"\r\n") if x.lower().startswith(b"content-length:")).split(b":")[1])
        while len(rest) < size:
            rest += s.recv(65536)
        s.close()
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(rest)["sha256"], hashlib.sha256(half + half).hexdigest())

    def test_at_most_four_uploads_per_module_at_once(self):
        self.module()
        slow = []
        for _ in range(modproxy.BODIES):
            slow.append(self.raw(f"POST /api/mod/fake/echo HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nX-Hub: 1\r\n"
                                 "Content-Length: 10\r\n\r\n", b"12345"))
        self.until(lambda: len(self.requests_seen()) == modproxy.BODIES)
        st, body, _ = self.api("POST", "/api/mod/fake/echo", b"x")
        self.assertEqual(st, 429)
        self.assertEqual(self.api("GET", "/api/mod/fake/echo")[0], 200)   # without a body — allowed
        slow[0].sendall(b"67890")
        self.assertEqual(self.status_of(slow[0])[0], 200)
        for s in slow:
            s.close()
        self.assertEqual(self.until(lambda: self.api("POST", "/api/mod/fake/echo", b"x")[0] == 200 or None), True)

    def test_client_that_stops_sending_is_cut_after_the_idle_time(self):
        self.module()
        with mock.patch.object(modproxy, "IDLE", 0.5):
            s = self.raw(f"POST /api/mod/fake/echo HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nX-Hub: 1\r\n"
                         "Content-Length: 10\r\n\r\n", b"12345")
            s.settimeout(10)
            t = time.time()
            self.assertEqual(s.recv(100), b"")   # the page closed the connection
            self.assertLess(time.time() - t, 5)
            s.close()
        self.assertEqual(self.api("POST", "/api/mod/fake/echo", b"x")[0], 200)   # room freed up


class Answer(ModHttp):
    def test_stream_comes_event_by_event_and_ends_with_the_module(self):
        self.module()
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", "/api/mod/fake/sse?n=3&gap=0.3", headers={"X-Hub": "1"})
        r = c.getresponse()
        self.assertEqual((r.status, r.getheader("Content-Type")), (200, "text/event-stream"))
        t = time.time()
        first = r.fp.readline()
        self.assertEqual(first, b"event: tick\n")
        self.assertLess(time.time() - t, 0.25)   # the first event comes at once, not after the whole stream
        rest = r.read()
        self.assertEqual(rest.count(b"event: tick"), 2)
        c.close()

    def test_an_open_stream_is_cut_once_its_reader_is_no_longer_let_in(self):
        # a remote sign-in (core/remote.py) that expired or was revoked must not keep a module stream open
        self.module()
        with mock.patch.object(modproxy, "RECHECK", 0.3), \
                mock.patch("modules.tailnet.access.local", return_value=False), \
                mock.patch("modules.tailnet.access.allowed", return_value=True):   # as a remote reader let in
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            c.request("GET", "/api/mod/fake/sse?n=0&gap=0.1", headers={"X-Hub": "1"})
            r = c.getresponse()
            self.assertEqual(r.status, 200)
            r.fp.readline()
            with mock.patch("modules.tailnet.access.allowed", return_value=False):
                ended = threading.Event()

                def drain():   # ends when the page closes the connection
                    try:
                        r.read()
                    except (OSError, http.client.HTTPException):
                        pass
                    ended.set()
                threading.Thread(target=drain, daemon=True).start()
                cut = ended.wait(5)
            c.close()
            self.assertTrue(cut, "the module stream stayed open for a reader no longer let in")

    def test_client_gone_closes_the_connection_to_the_module(self):
        self.module()
        s = self.raw(f"GET /api/mod/fake/sse?n=0&gap=0.1 HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nX-Hub: 1\r\n\r\n")
        code, _ = self.status_of(s)
        self.assertEqual(code, 200)
        s.close()
        self.assertTrue(self.until(lambda: self.logged("gone")), "the module keeps writing into a stream that is gone")

    def test_module_breaking_mid_answer_ends_the_client_answer_and_the_page_lives(self):
        self.module()
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", "/api/mod/fake/cut", headers={"X-Hub": "1"})
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        with self.assertRaises(http.client.IncompleteRead):
            r.read()
        c.close()
        self.assertEqual(self.req("GET", "/api/snapshot")[0], 200)
        self.assertEqual(self.api("GET", "/api/mod/fake/echo")[0], 200)

    def test_silent_stream_is_closed_after_idle_and_long_one_after_max_stream(self):
        self.module(http={"max_stream": 1})
        with mock.patch.object(modproxy, "IDLE", 0.5):
            t = time.time()
            st, body, _ = self.api("GET", "/api/mod/fake/hang")
            self.assertEqual((st, body), (200, b"event: tick\ndata: 0\n\n"))
            self.assertLess(time.time() - t, 5)
        t = time.time()
        st, body, _ = self.api("GET", "/api/mod/fake/sse?n=0&gap=0.1")   # pings keep coming, but the stream's deadline passed
        self.assertEqual(st, 200)
        self.assertGreater(body.count(b"event: tick"), 3)
        self.assertLess(time.time() - t, 5)

    def test_large_answer_is_passed_without_buffering(self):
        self.module()
        n = 64 << 20
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        c.request("GET", f"/api/mod/fake/big?n={n}", headers={"X-Hub": "1"})
        r = c.getresponse()
        self.assertEqual((r.status, r.getheader("Content-Length")), (200, str(n)))
        got = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            got += len(chunk)
        self.assertEqual(got, n)
        c.close()

    def test_at_most_eight_streams_per_module(self):
        self.module()
        open_ = []
        for _ in range(modproxy.STREAMS):
            s = self.raw(f"GET /api/mod/fake/sse?n=0&gap=0.2 HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nX-Hub: 1\r\n\r\n")
            self.assertEqual(self.status_of(s)[0], 200)
            open_.append(s)
        self.assertEqual(self.api("GET", "/api/mod/fake/sse?n=1")[0], 429)
        self.assertEqual(self.api("GET", "/api/mod/fake/echo")[0], 200)   # not a stream — allowed
        for s in open_:
            s.close()
        self.assertTrue(self.until(lambda: self.api("GET", "/api/mod/fake/sse?n=1")[0] == 200))

    def test_connections_to_all_modules_are_capped(self):
        self.module()
        with mock.patch.object(modproxy, "TOTAL", 2):
            held = [self.raw(f"GET /api/mod/fake/sse?n=0&gap=0.2 HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                             "X-Hub: 1\r\n\r\n") for _ in range(2)]
            for s in held:
                self.assertEqual(self.status_of(s)[0], 200)
            st, body, _ = self.api("GET", "/api/mod/fake/echo")
            self.assertEqual(st, 503)
            self.assertEqual(self.req("GET", "/api/snapshot")[0], 200)   # the page's ordinary endpoints are alive
            for s in held:
                s.close()
            self.assertTrue(self.until(lambda: self.api("GET", "/api/mod/fake/echo")[0] == 200))


class Failures(ModHttp):
    def test_garbage_or_silence_before_headers_is_502_and_a_module_error(self):
        self.module()
        st, body, _ = self.api("GET", "/api/mod/fake/garbage")
        self.assertEqual(st, 502)
        self.assertIn("fake", json.loads(body)["error"])
        with mock.patch.object(modproxy, "IDLE", 0.5):
            self.assertEqual(self.api("GET", "/api/mod/fake/nohead")[0], 502)
        errs = [e for e in self.spooled() if e["type"] == "module.error"]
        self.assertEqual([(e["data"]["module"], e["data"]["hook"], e["data"]["kind"]) for e in errs],
                         [("fake", "http", "schema"), ("fake", "http", "timeout")])

    def test_a_module_length_or_status_that_is_not_ascii_digits_does_not_drop_the_page_connection(self):
        # "²" is a digit to str.isdigit() and not a number to int(): not a length (the body ends with the connection) and not a status
        self.module()
        st, body, _ = self.api("GET", "/api/mod/fake/digitlen")
        self.assertEqual((st, body), (200, b"ok"))
        self.assertEqual(self.api("GET", "/api/mod/fake/digitstatus")[0], 502)

    def test_header_with_a_bare_line_break_is_not_passed_to_the_client(self):
        self.module()
        st, _, r = self.api("GET", "/api/mod/fake/inject")
        self.assertEqual((st, r.getheader("Set-Cookie")), (502, None))

    def test_dead_process_is_503_retry_after_and_one_failure_however_often_it_is_called(self):
        proc = self.module()
        kill(proc.pid)
        proc.wait(10)
        for _ in range(modrun.FAILS + 2):
            st, _, r = self.api("GET", "/api/mod/fake/echo")
            self.assertEqual((st, r.getheader("Retry-After")), (503, "2"))
        errs = [e["data"] for e in self.spooled() if e["type"] == "module.error"]
        self.assertEqual([(e["hook"], e["kind"]) for e in errs], [("http", "connect")])   # one record per launch

    def test_a_process_that_drops_the_connection_unanswered_is_still_a_502_each_time(self):
        self.module()
        for _ in range(modrun.FAILS):
            self.assertEqual(self.api("GET", "/api/mod/fake/drop")[0], 502)
        self.assertEqual(self.api("GET", "/api/mod/fake/echo")[0], 503)   # suspended by its own failure

    def crash_while_polled(self, proc):
        """The module process crashes, the client polls the endpoint every 0.2 s; hub-up notices the exit not at once
        (until then the process is a zombie, the launch file is in place), writes exit, and 2 s later — a new launch.
        The response codes the client sees."""
        kill(proc.pid)   # no wait: a zombie, as with hub-up before its poll
        codes = []

        def poll(seconds):
            end = time.time() + seconds
            while time.time() < end:
                st, _, r = self.api("GET", "/api/mod/fake/echo")
                codes.append((st, r.getheader("Retry-After")))
                time.sleep(0.2)
        poll(1.2)
        modrun.exited(self.state, "fake", proc.wait(10), 60)   # like hub-up
        poll(0.6)
        new = self.module()
        poll(0.4)
        return codes, new

    def journal_records(self):
        for e in self.spooled():
            self.j.append(e["type"], e.get("entity"), e["data"])
        self.web.tail.poll()
        return journal.load_state(self.state).get("modules") or {}

    def test_one_crash_while_a_client_polls_is_one_failure_and_no_pause(self):
        codes, _ = self.crash_while_polled(self.module())
        # 502 — only a request that the process accepted and did not answer while dying (the module might have executed it)
        self.assertLessEqual([c for c, _ in codes].count(502), 1, codes)
        self.assertTrue(all(ra == "2" for c, ra in codes if c == 503), codes)
        self.assertEqual(codes[-1], (200, None))
        rec = self.journal_records()["fake"]
        self.assertEqual((rec["state"], len(rec["errors"])), ("ok", 1))

    def test_three_crashes_in_a_row_still_pause_the_module(self):
        proc = self.module()
        for _ in range(modrun.FAILS):
            _, proc = self.crash_while_polled(proc)
        rec = self.journal_records()["fake"]
        self.assertEqual((rec["state"], rec["suspensions"]), ("suspended", 1))
        self.assertIn("suspended", json.loads(self.api("GET", "/api/mod/fake/echo")[1])["error"])


class Views(ModHttp):
    def test_view_is_sandboxed_needs_no_x_hub_and_carries_no_owner_word(self):
        self.module()
        st, body, r = self.req("GET", "/mod/fake/view/page", headers={"X-Hub-Owner": "1", "X-Hub-Secret": "x"})
        self.assertEqual(st, 200)
        self.assertIn(b"<script>", body)
        self.assertIn(modproxy.VIEW_CSP, r.headers.get_all("Content-Security-Policy"))
        self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")
        seen = self.requests_seen()[-1]
        self.assertEqual(seen["path"], "/mod/fake/view/page")
        self.assertNotIn("x-hub-owner", seen["headers"])
        self.assertTrue(seen["secret_ok"])

    def test_view_is_get_only(self):
        self.module()
        st, _, r = self.api("POST", "/mod/fake/view/page", b"{}")
        self.assertEqual((st, r.getheader("Allow")), (405, "GET"))
        self.assertEqual(self.requests_seen(), [])

    def test_owner_word_from_an_owner_request_only(self):
        self.module()
        self.assertEqual(json.loads(self.req("GET", "/mod/fake/view/owner")[1]), {"owner": False})
        self.assertEqual(self.spooled(), [])
        self.assertEqual(json.loads(self.api("POST", "/api/mod/fake/owner?to=a", b"")[1]), {"owner": True})
        facts = self.spooled()
        self.assertEqual([(e["type"], e["data"]["channel"], e["data"]["owner"]) for e in facts],
                         [("message.received", "mod:fake", True)])


class OwnerChannel(unittest.TestCase):
    def test_module_channel_is_an_owner_channel(self):
        for ch, ok in (("mod:fake", True), ("mod:a-b", True), ("mod:", False), ("mod:Bad", False),
                       ("module:x", False), ("session", False), ("web", True)):
            self.assertEqual(tools.from_owner({"owner": True, "channel": ch}), ok, ch)
            self.assertEqual(guards.from_owner({}, {"owner": True, "channel": ch}) is None, ok, ch)
            self.assertFalse(tools.from_owner({"owner": False, "channel": ch}))

    def test_message_from_a_module_channel_is_the_owner_word(self):
        st = reducer.empty()
        for i, (t, e, d) in enumerate([("session.requested", "a", {"session_id": "s", "cwd": "/w"}),
                                       ("session.started", "a", {"session_id": "s"}),
                                       ("message.received", None, {"to": "a", "text": "hi", "channel": "mod:fake",
                                                                   "owner": True})], 1):
            st, _, why = reducer.reduce(st, {"seq": i, "ts": 1000.0 + i, "type": t, "entity": e, "data": d,
                                             "src": "module"})
            self.assertIsNone(why)
        msg = st["messages"]["m3"]
        self.assertTrue(msg["from_owner"])


class Manifest(unittest.TestCase):
    def man(self, **extra):
        return {"name": "m", "version": "1.0.0", "api": "^2.1", "run": ["x"], "capabilities": ["http"], **extra}

    def test_http_field(self):
        self.assertEqual(modules.validate(self.man(http={"max_body": 1 << 30, "max_stream": 86400})), [])
        self.assertEqual(modules.validate(self.man()), [])
        for bad in ({"max_body": (1 << 30) + 1}, {"max_body": 0}, {"max_stream": 86401}, {"max_stream": True},
                    {"other": 1}, []):
            self.assertTrue(modules.validate(self.man(http=bad)), bad)
        self.assertTrue(modules.validate({**self.man(http={"max_body": 10}), "capabilities": []}))

    def test_limits_from_the_manifest(self):
        self.assertEqual(modproxy.limits({}), (modproxy.MAX_BODY, modproxy.MAX_STREAM))
        self.assertEqual(modproxy.limits({"http": {"max_body": 5, "max_stream": 7}}), (5, 7))


class WithHubd(unittest.TestCase):
    def test_module_raised_by_the_core_answers_through_the_page(self):
        # the whole path: hubd writes want, the process is started as by hub-up, hello -> module.ready, the page proxies
        from tests.fakes.modstand import Stand
        s = Stand(self, caps=["http"])
        with open(os.path.join(s.root, "fake", "module.json")) as f:
            man = json.load(f)
        man["run"] = [sys.executable, FAKE, "."]
        with open(os.path.join(s.root, "fake", "module.json"), "w") as f:
            json.dump(man, f)
        s.hub.modules.reload(s.cfg, {})
        s.launch()
        s.until_ready()
        web = Web(s.state_dir)
        server = web.serve(port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (web.stop.set(), server.shutdown(), server.server_close()))
        web.tail.poll()
        port = server.server_address[1]
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("PUT", "/api/mod/fake/echo", b"abc", {"X-Hub": "1"})
        r = c.getresponse()
        self.assertEqual((r.status, json.loads(r.read())["size"]), (200, 3))
        c.close()


class ReadLock(ModHttp):
    def test_module_path_does_not_wait_for_journal_read_lock(self):
        """A module endpoint does not wait for the page's journal read lock: under load a poll holds it for seconds, while the
        proxy looks at module records on every request — those published by the last poll (Tail.records)."""
        self.module()
        self.web.tail.reading.acquire()
        try:
            t = time.monotonic()
            st, body, _ = self.api("GET", "/api/mod/fake/echo", timeout=5)
            self.assertEqual((st, json.loads(body)["method"]), (200, "GET"))
            self.assertLess(time.monotonic() - t, 3)
        finally:
            self.web.tail.reading.release()

    def test_module_not_yet_known_to_the_journal_reader_answers_starting_at_once(self):
        """The poll has not yet published the module's readiness records (the page has just started, the journal is read from
        the beginning) — the endpoint answers «starting» with Retry-After and does not wait for the read lock until the read ends."""
        self.module(ready=False)
        self.web.tail.reading.acquire()
        try:
            t = time.monotonic()
            st, _, r = self.api("GET", "/api/mod/fake/echo", timeout=5)
            self.assertEqual((st, r.getheader("Retry-After")), (503, "2"))
            self.assertLess(time.monotonic() - t, 3)
        finally:
            self.web.tail.reading.release()


if __name__ == "__main__":
    unittest.main()
