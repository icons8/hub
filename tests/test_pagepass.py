"""The page's password (core/pagepass.py), set per hub in "Hubs on this computer" or by `hub password set`.

Before: the page and "Files" let in any request from this machine unless the owner turned on the sign-in by a link with
a key (core/pagekey.py) — and from the tailnet or Cloudflare Access without anything. With a password, whoever the
first step lets in (this machine, the owner by a provider) still needs the cookie of the sign-in form; a hub that had
the key on and has no password yet keeps its key as before; a password puts a key nobody sees next to itself, so a
rollback to a code without passwords finds the page closed."""
import tests  # noqa: F401 — keep the machine's settings out (~/.config/hub-core), see tests/__init__.py
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.parse import urlencode

from core import pagekey, pagepass, remote
from modules.files.server import Files
from modules.tailnet import access
from modules.web.server import Web
from platforms import current

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASSWORD = "correct horse battery"
KEY = "ab" * 32


class Folder(unittest.TestCase):
    """Each test has its own settings folder: a password left in the shared one would lock out every other test."""
    def setUp(self):
        self.cfg = tempfile.mkdtemp(prefix="pagepass-cfg-")
        self.addCleanup(current.rmtree, self.cfg)
        env = mock.patch.dict(os.environ, {"HUB_CONFIG_DIR": self.cfg})
        env.start()
        self.addCleanup(env.stop)
        fast = mock.patch.object(pagepass, "ROUNDS", 1000)   # the real 600 000 rounds are measured, not repeated here
        fast.start()
        self.addCleanup(fast.stop)

    def set(self, password=PASSWORD):
        return pagepass.set_password(password)

    def cookie(self, rec=None, now=None):
        return {"Cookie": pagepass.set_cookie(rec or pagepass.read(), now=now).split(";")[0]}


class Password(Folder):
    def test_the_file_is_the_switch(self):
        self.assertEqual(pagepass.state(), "open")
        self.assertTrue(pagepass.let_in({}))
        rec = self.set()
        self.assertEqual(pagepass.state(), "password")
        self.assertNotIn(PASSWORD, open(pagepass.path(), encoding="utf-8").read())
        if os.name != "nt":
            self.assertEqual(os.stat(pagepass.path()).st_mode & 0o777, 0o600)
        self.assertTrue(pagepass.matches(PASSWORD))
        self.assertFalse(pagepass.matches(PASSWORD + "x"))
        self.assertFalse(pagepass.matches(None))
        self.assertEqual(rec["kdf"], "pbkdf2_sha256")
        self.assertTrue(pagepass.take_off())
        self.assertEqual(pagepass.state(), "open")
        self.assertIsNone(pagekey.read())   # the key it put next to itself goes with it
        self.assertFalse(pagepass.take_off())

    def test_the_length(self):
        self.assertEqual(pagepass.fits("short"), "web.pass.too_short")
        self.assertEqual(pagepass.fits("x" * 257), "web.pass.too_long")
        self.assertEqual(pagepass.fits(None), "web.pass.too_short")
        self.assertIsNone(pagepass.fits("mot de passe très long"))
        with self.assertRaises(ValueError):
            pagepass.set_password("short")
        self.assertEqual(pagepass.state(), "open")

    def test_a_damaged_file_lets_nobody_in(self):
        rec = self.set()
        good = self.cookie(rec)
        with open(pagepass.path(), "w") as f:
            f.write('{"v": 1}')
        self.assertEqual(pagepass.state(), "broken")
        self.assertFalse(pagepass.let_in(good))
        self.assertFalse(pagepass.let_in(good, remote=True))
        self.assertEqual(pagepass.attempt(PASSWORD)[0], "none")

    def test_the_cookie(self):
        rec = self.set()
        now = time.time()
        self.assertFalse(pagepass.let_in({}))
        self.assertFalse(pagepass.let_in(None))   # an earlier release's server in memory: no headers
        self.assertTrue(pagepass.let_in(self.cookie(rec)))
        self.assertTrue(pagepass.let_in(self.cookie(rec), remote=True))
        c = pagepass.set_cookie(rec)
        for part in ("hub_main=v1.", "HttpOnly", "SameSite=Lax", "Path=/", f"Max-Age={30 * 86400}"):
            self.assertIn(part, c)
        self.assertNotIn("Secure", c)
        self.assertIn("Secure", pagepass.set_cookie(rec, secure=True))
        value = c.split(";")[0].split("=", 1)[1]
        # other programs on 127.0.0.1 set their own cookies on the same host (cookies do not split by port)
        self.assertTrue(pagepass.let_in({"Cookie": f"sid=1; hub_main=old; theme=dark; hub_main={value}"}))
        self.assertFalse(pagepass.let_in({"Cookie": f"hub_work2={value}"}))   # another hub's cookie on this host
        at, _, sig = value[3:].partition(".")
        self.assertFalse(pagepass.let_in({"Cookie": f"hub_main=v1.{int(at) + 1}.{sig}"}))   # forged time
        self.assertFalse(pagepass.let_in(self.cookie(rec, now=now - 31 * 86400)))   # ran out
        self.assertFalse(pagepass.let_in(self.cookie(rec, now=now + 3600)))   # from the future
        self.assertTrue(pagepass.let_in(self.cookie(rec, now=now - 29 * 86400)))
        # digits that are not ASCII ("²" in a latin-1 header) are no time: the valid cookie after it still counts
        self.assertTrue(pagepass.let_in({"Cookie": f"hub_main=v1.\xb2.x; hub_main={value}"}))
        self.assertIsNone(pagepass.renewal({"Cookie": "hub_main=v1.\xb2.x"}))
        self.set("another password")   # a change: every cookie of the old secret is out
        self.assertFalse(pagepass.let_in(self.cookie(rec)))

    def test_the_page_load_renews_a_cookie_older_than_a_day(self):
        rec = self.set()
        self.assertIsNone(pagepass.renewal(self.cookie(rec)))
        self.assertIsNone(pagepass.renewal({}))
        got = pagepass.renewal(self.cookie(rec, now=time.time() - 2 * 86400))
        self.assertTrue(got.startswith("hub_main=v1."))
        self.assertTrue(pagepass.let_in({"Cookie": got.split(";")[0]}))

    def test_tries_lock_the_form_and_a_right_one_clears(self):
        self.set()
        now = 1_800_000_000
        for _ in range(4):
            self.assertEqual(pagepass.attempt("wrong one", now=now), ("wrong", 0))
        self.assertEqual(pagepass.attempt("wrong one", now=now), ("wrong", 60))
        with mock.patch.object(pagepass, "matches", side_effect=AssertionError("checked while locked")):
            self.assertEqual(pagepass.attempt(PASSWORD, now=now + 10), ("locked", 50))
        self.assertEqual(pagepass.attempt("wrong one", now=now + 61), ("wrong", 120))
        self.assertEqual(pagepass.attempt(PASSWORD, now=now + 200), ("ok", 0))
        self.assertEqual(pagepass.attempt("wrong one", now=now + 201), ("wrong", 0))   # cleared
        for fails in range(2, 30):
            pagepass.attempt("wrong one", now=now + 10 ** 6 * fails)
        self.assertEqual(pagepass.attempt("wrong one", now=now + 10 ** 9)[1], pagepass.LOCK_MAX)

    def test_tries_at_once_see_each_other(self):
        # many wrong tries sent together: each reads the count the previous one wrote — the lock comes after five
        self.set()
        got = []
        threads = [threading.Thread(target=lambda: got.append(pagepass.attempt("wrong one")[0])) for _ in range(20)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(got.count("wrong"), 5)
        self.assertEqual(got.count("locked"), 15)

    def test_one_counter_per_hub_whoever_checks(self):
        # the form of the hub and the row of a neighbour's page check the same file: one ceiling
        self.set()
        other = tempfile.mkdtemp(prefix="pagepass-other-")
        self.addCleanup(current.rmtree, other)
        pagepass.set_password(PASSWORD, other)
        now = 1_800_000_000
        for _ in range(5):
            pagepass.attempt("wrong one", other, now=now)
        self.assertEqual(pagepass.attempt(PASSWORD, other, now=now + 1)[0], "locked")
        self.assertEqual(pagepass.attempt(PASSWORD, now=now + 1)[0], "ok")   # this hub's counter is its own

    def test_a_rollback_finds_the_page_closed(self):
        # the code of an earlier release knows only the key: with a password set, its check is closed, and its old
        # cookie (the key before the password) no longer opens it
        pagekey.write(KEY)
        self.set()
        self.assertTrue(pagekey.read())
        self.assertNotEqual(pagekey.read(), KEY)
        self.assertFalse(pagekey.let_in({}))
        self.assertFalse(pagekey.let_in({"Cookie": "hub_main=" + KEY}))
        self.assertFalse(pagepass.let_in({"Cookie": "hub_main=" + pagekey.read()}))   # the new code: the password only

    def test_a_cut_between_the_two_files_leaves_it_closed(self):
        from core import catalog
        with mock.patch.object(catalog, "write_private", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.set()
        self.assertEqual(pagepass.state(), "key")   # a key nobody has: closed from this machine
        self.assertFalse(pagepass.let_in({}))
        self.set()
        with mock.patch.object(pagekey, "off", side_effect=OSError("busy")):
            with self.assertRaises(OSError):
                pagepass.take_off()
        self.assertEqual(pagepass.state(), "key")
        self.assertFalse(pagepass.let_in({}))

    def test_the_old_key_until_a_password(self):
        pagekey.write(KEY)
        self.assertEqual(pagepass.state(), "key")
        self.assertFalse(pagepass.let_in({}))
        self.assertTrue(pagepass.let_in({"Cookie": "hub_main=" + KEY}))
        self.assertTrue(pagepass.let_in({}, remote=True))   # from the tailnet it never needed the key
        self.assertEqual(pagepass.mark_secret(), KEY)

    def test_the_local_token(self):
        state = tempfile.mkdtemp(prefix="pagepass-state-")
        self.addCleanup(current.rmtree, state)
        self.assertFalse(pagepass.local_ok({"X-Hub-Local": ""}, state, "/api/secrets/fill"))
        token = pagepass.new_local(state)
        self.assertEqual(pagepass.local_header(state), {"X-Hub-Local": token})
        self.assertTrue(pagepass.local_ok({"X-Hub-Local": token}, state, "/api/secrets/fill"))
        self.assertFalse(pagepass.local_ok({"X-Hub-Local": token}, state, "/api/send"))
        self.assertTrue(pagepass.local_ok({"X-Hub-Local": token}, state, "/api/secrets/add"))   # `hub secret add`
        for owners in ("/api/secrets/copy", "/api/secrets/save", "/api/secrets/restore", "/api/secrets/remove"):
            self.assertFalse(pagepass.local_ok({"X-Hub-Local": token}, state, owners), owners)
        self.assertFalse(pagepass.local_ok({"X-Hub-Local": "x" * 64}, state, "/api/secrets/fill"))
        self.assertFalse(pagepass.local_ok(None, state, "/api/secrets/fill"))
        pagepass.new_local(state)   # a new start of the page
        self.assertFalse(pagepass.local_ok({"X-Hub-Local": token}, state, "/api/secrets/fill"))


class Admit(Folder):
    def setUp(self):
        super().setUp()
        self.d = tempfile.mkdtemp(prefix="pagepass-state-")
        self.addCleanup(current.rmtree, self.d)
        with open(os.path.join(self.d, "tailnet.json"), "w") as f:
            json.dump({"ok": True, "ts": time.time(),
                       "owner": "owner@x", "ip": "100.9.9.9", "hosts": ["100.9.9.9:8787"]}, f)
        # the owner's phone, and this very machine by its tailnet address (whois names the device: the owner's)
        self.whois = lambda _, p: {"100.1.1.1": "owner@x", "100.9.9.9": "owner@x"}.get(p)

    def admit(self, host, peer, headers):
        return remote.admit(self.d, host, peer, headers=headers, whois=self.whois)

    def test_open_as_before(self):
        self.assertEqual(self.admit("localhost:8787", "127.0.0.1", {}), {"local": True})
        self.assertEqual(self.admit("100.9.9.9:8787", "100.1.1.1", {})["who"], "owner@x")
        self.assertIsNone(self.admit("100.9.9.9:8787", "100.2.2.2", {}))

    def test_a_password_everywhere(self):
        rec = self.set()
        signed = self.cookie(rec)
        self.assertIsNone(self.admit("localhost:8787", "127.0.0.1", {}))
        self.assertEqual(self.admit("localhost:8787", "127.0.0.1", signed), {"local": True})
        self.assertIsNone(self.admit("100.9.9.9:8787", "100.1.1.1", {}))   # the owner's phone: the password too
        self.assertEqual(self.admit("100.9.9.9:8787", "100.1.1.1", signed)["who"], "owner@x")
        # another account of this machine through its tailnet address: whois names the machine's owner
        self.assertIsNone(self.admit("100.9.9.9:8787", "100.9.9.9", {}))
        with mock.patch.object(access, "tailscale_whois", self.whois):
            self.assertTrue(remote.needs_signin(self.d, "100.9.9.9:8787", "100.1.1.1", {}))
            self.assertFalse(remote.needs_signin(self.d, "100.9.9.9:8787", "100.2.2.2", {}))   # a stranger: 403
        self.assertTrue(remote.needs_signin(self.d, "localhost:8787", "127.0.0.1", {}))
        self.assertTrue(remote.needs_key("localhost:8787", "127.0.0.1", {}))   # the name an earlier server calls

    def test_the_tailnet_address_of_this_machine_today(self):
        # before the password: a request of another account to this machine's tailnet address is the owner's by whois
        # — with a password that door asks for it as any other (test_a_password_everywhere)
        self.assertEqual(self.admit("100.9.9.9:8787", "100.9.9.9", {})["who"], "owner@x")


def ask(port, method, path, headers, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    data = body if isinstance(body, (str, bytes)) or body is None else json.dumps(body)
    c.request(method, path, data, headers)
    r = c.getresponse()
    out = r.read()
    c.close()
    return r.status, dict((k.lower(), v) for k, v in r.getheaders()), out


class PageServer(Folder):
    def setUp(self):
        super().setUp()
        self.state = tempfile.mkdtemp(prefix="pagepass-web-")
        self.addCleanup(current.rmtree, self.state)
        self.web = Web(self.state)
        self.web.decide_wait = 0
        self.put = []
        self.server = self.web.serve(port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.web.stop.set)

    def req(self, method, path, extra=None, body=None):
        h = {"Host": f"localhost:{self.port}", "X-Hub": "1", "Content-Type": "application/json", **(extra or {})}
        return ask(self.port, method, path, h, body)

    def sign_in(self, password=PASSWORD, extra=None):
        h = {"Content-Type": "application/x-www-form-urlencoded", **(extra or {})}
        h.pop("X-Hub", None)
        return ask(self.port, "POST", "/signin", {"Host": f"localhost:{self.port}", **h}, urlencode({"password": password}))

    def test_open_the_page_is_as_before(self):
        self.assertEqual(self.req("GET", "/api/snapshot")[0], 200)
        self.assertEqual(self.req("GET", "/")[0], 200)
        self.assertEqual(json.loads(self.req("GET", "/api/signin")[2]), {"signin": "open"})
        self.assertEqual(json.loads(self.req("GET", "/api/about")[2])["signin"], "open")
        self.assertIn("page.password", json.loads(self.req("GET", "/api/snapshot")[2])["features"])

    def test_without_the_sign_in_nothing_but_the_form(self):
        self.set()
        code, _, body = self.req("GET", "/api/snapshot")
        self.assertEqual(code, 401)
        self.assertEqual(json.loads(body)["code"], "signin")
        self.assertEqual(self.req("POST", "/api/send", None, {"session": "a", "text": "hi"})[0], 401)
        self.assertEqual(self.req("GET", "/static/js/main.js")[0], 401)
        self.assertEqual(self.req("GET", "/api/signin")[0], 401)
        code, head, body = self.req("GET", "/")
        self.assertEqual(code, 200)   # installers and app shells wait for "/" to answer
        page = body.decode()
        self.assertIn('action="/signin"', page)
        self.assertIn('type="password"', page)
        self.assertIn("hub password off", page)   # forgotten: the way back
        self.assertNotIn("/static/", page)   # not the hub page, no data
        self.assertNotIn("set-cookie", head)

    def test_the_form_signs_this_browser_in(self):
        self.set()
        code, head, body = self.sign_in("not the password")
        self.assertEqual(code, 401)
        self.assertNotIn("set-cookie", head)
        self.assertIn("form", body.decode())
        self.assertIn('action="/signin"', self.req("GET", "/signin")[2].decode())   # its address opened again: the form
        code, head, _ = self.sign_in()
        self.assertEqual((code, head["location"]), (303, "/"))
        self.assertIn("HttpOnly", head["set-cookie"])
        cookie = {"Cookie": head["set-cookie"].split(";")[0]}
        self.assertEqual(self.req("GET", "/signin", cookie)[1]["location"], "/")   # signed in: the page
        self.assertEqual(self.req("GET", "/api/snapshot", cookie)[0], 200)
        self.assertEqual(self.req("POST", "/api/paths", cookie, {"items": []})[0], 200)
        self.assertEqual(json.loads(self.req("GET", "/api/about", cookie)[2])["signin"], "password")
        # a foreign site cannot send the form (and a stranger is refused before it)
        h = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"}
        self.assertEqual(self.sign_in(extra=h)[0], 403)
        self.assertEqual(self.sign_in(extra={"X-Forwarded-For": "203.0.113.7"})[0], 403)
        # signing out: this browser's cookie is gone
        code, head, _ = self.req("POST", "/api/signout", cookie, {})
        self.assertEqual(code, 200)
        self.assertIn("Max-Age=0", head["set-cookie"])
        # and the owner's files this browser kept by revalidation (/api/file is private, no-cache) leave its disk cache
        self.assertEqual(head["clear-site-data"], '"cache"')

    def test_the_form_locks_after_five_wrong(self):
        self.set()
        for _ in range(5):
            self.sign_in("wrong password")
        code, _, body = self.sign_in()
        self.assertEqual(code, 429)
        self.assertIn("min", body.decode())
        self.assertEqual(self.req("GET", "/api/snapshot", self.cookie())[0], 200)   # signed-in browsers are not touched

    def test_the_page_renews_an_older_cookie(self):
        rec = self.set()
        code, head, _ = self.req("GET", "/", self.cookie(rec, now=time.time() - 2 * 86400))
        self.assertEqual(code, 200)
        self.assertIn("hub_main=v1.", head["set-cookie"])
        self.assertNotIn("set-cookie", self.req("GET", "/", self.cookie(rec))[1])   # fresh: no renewal

    def test_the_update_watchdog_and_a_neighbour_see_the_hub_without_the_sign_in(self):
        # modules/update/trial.py (the previous release's copy, which knows no password) asks /api/about from this
        # machine: without an answer every update would roll back; a neighbour's page asks how this hub is signed in to
        from modules.update import trial
        self.set()
        got = trial.ask_about(self.port)
        self.assertEqual(set(got), {"core_started", "web_started", "code", "signin"})
        self.assertEqual(got["signin"], "password")
        self.assertNotIn(self.state, json.dumps(got))
        self.assertIn("state", json.loads(self.req("GET", "/api/about", self.cookie())[2]))   # signed in: the whole card

    def test_this_tab_sets_the_password_and_stays_signed_in(self):
        code, head, body = self.req("POST", "/api/instances", None, {"op": "password", "name": "", "new": "short"})
        self.assertEqual(code, 400)
        self.assertEqual(pagepass.state(), "open")
        code, head, body = self.req("POST", "/api/instances", None, {"op": "password", "name": "", "new": PASSWORD})
        self.assertEqual(code, 200, body)
        self.assertEqual(json.loads(body)["signin"], "password")
        cookie = {"Cookie": head["set-cookie"].split(";")[0]}
        self.assertEqual(self.req("GET", "/api/snapshot", cookie)[0], 200)
        self.assertEqual(self.req("GET", "/api/snapshot")[0], 401)
        # a change needs the current one
        self.assertEqual(self.req("POST", "/api/instances", cookie, {"op": "password", "name": "", "new": "x" * 9})[0], 403)
        self.assertEqual(self.req("POST", "/api/instances", cookie, {"op": "password", "name": "", "new": "x" * 9,
                                                                       "current": "wrong one"})[0], 403)
        code, head, _ = self.req("POST", "/api/instances", cookie, {"op": "password", "name": "", "new": "x" * 9,
                                                                    "current": PASSWORD})
        self.assertEqual(code, 200)
        self.assertEqual(self.req("GET", "/api/snapshot", cookie)[0], 401)   # every browser signs in again…
        cookie = {"Cookie": head["set-cookie"].split(";")[0]}
        self.assertEqual(self.req("GET", "/api/snapshot", cookie)[0], 200)   # …this one at once
        self.assertEqual(self.req("POST", "/api/instances", cookie, {"op": "password_off", "name": "",
                                                                       "current": "x" * 9})[0], 200)
        self.assertEqual(pagepass.state(), "open")
        self.assertEqual(self.req("GET", "/api/snapshot")[0], 200)
        # the password is nowhere in the hub's folder
        for folder, _, names in os.walk(self.state):
            for n in names:
                with open(os.path.join(folder, n), "rb") as f:
                    self.assertNotIn(PASSWORD.encode(), f.read(), os.path.join(folder, n))

    def test_an_open_stream_is_cut_when_the_password_is_set(self):
        from modules.web import server as web_server
        with mock.patch.object(web_server, "STREAM_WAIT", 0.2), mock.patch.object(web_server, "STREAM_RECHECK", 0.5):
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            c.request("GET", "/api/stream", None, {"Host": f"localhost:{self.port}"})
            r = c.getresponse()
            self.assertEqual(r.status, 200)
            r.fp.readline()
            self.set()   # another account's tab opened before: it must not keep the stream
            ended = threading.Event()

            def drain():
                try:
                    while r.fp.readline():
                        pass
                except OSError:
                    pass
                ended.set()
            threading.Thread(target=drain, daemon=True).start()
            self.assertTrue(ended.wait(8), "the stream stayed open without the sign-in")
            c.close()

    def test_the_hub_s_own_command_fills_a_secret_without_the_cookie(self):
        self.set()
        token = pagepass.local_header(self.state)
        self.assertTrue(token)
        body = {"item": "a", "field": "nope", "session": "maps"}
        self.assertEqual(self.req("POST", "/api/secrets/fill", None, body)[0], 401)
        self.assertEqual(self.req("POST", "/api/secrets/fill", token, body)[0], 400)   # let in: its own checks answer
        self.assertEqual(self.req("POST", "/api/send", token, {"session": "a", "text": "hi"})[0], 401)   # that path only
        h = {**token, "X-Forwarded-For": "203.0.113.7"}
        self.assertEqual(self.req("POST", "/api/secrets/fill", h, body)[0], 403)   # not this machine

    def test_the_cookie_does_not_go_to_a_module(self):
        from modules.web.modproxy import Proxy
        import email.message
        rec = self.set()
        value = self.cookie(rec)["Cookie"]
        msg = email.message.Message()
        msg["Host"] = "localhost:8787"
        msg["Cookie"] = f"theme=dark; {value}; hub_work2=x"
        h = mock.Mock(headers=msg)
        head = Proxy.head(mock.Mock(), h, "GET", "view", {"port": 1, "secret": "s"}, 0, "/").decode()
        self.assertNotIn(value, head)
        self.assertIn("Cookie: theme=dark", head)

    def test_the_owner_from_the_tailnet_signs_in_too(self):
        rec = self.set()
        with open(os.path.join(self.state, "tailnet.json"), "w") as f:
            json.dump({"ok": True, "ts": time.time(),
                       "owner": "owner@x", "ip": "127.0.0.1", "hosts": [f"100.9.9.9:{self.port}"]}, f)
        host = {"Host": f"100.9.9.9:{self.port}"}
        # the tailnet path by its Host and whois; the test server listens on 127.0.0.1, so the peer is replaced
        with mock.patch.object(remote.Tailnet, "claims", lambda *_: True), \
                mock.patch.object(access, "tailscale_whois", lambda *_: "owner@x"), \
                mock.patch.object(access, "local", lambda *a, **k: False):
            self.assertEqual(self.req("GET", "/api/snapshot", host)[0], 401)
            self.assertIn('action="/signin"', self.req("GET", "/", host)[2].decode())
            self.assertEqual(self.req("GET", "/api/about", host)[0], 401)   # the short card is this machine's only
            self.assertEqual(self.req("GET", "/api/snapshot", {**host, **self.cookie(rec)})[0], 200)
            code, head, _ = ask(self.port, "POST", "/signin", {**host, "Content-Type": "application/x-www-form-urlencoded"},
                                urlencode({"password": PASSWORD}))
            self.assertEqual(code, 303)
            self.assertNotIn("Secure", head["set-cookie"])   # the tailnet is plain http
        with mock.patch.object(remote.Tailnet, "claims", lambda *_: True), \
                mock.patch.object(access, "tailscale_whois", lambda *_: "stranger@x"), \
                mock.patch.object(access, "local", lambda *a, **k: False):
            self.assertEqual(self.req("GET", "/", host)[0], 403)   # a stranger never sees the form

    def test_the_old_key_until_a_password(self):
        pagekey.write(KEY)
        self.assertEqual(self.req("GET", "/api/snapshot")[0], 401)
        page = self.req("GET", "/")[2].decode()
        self.assertIn("hub password set", page)
        self.assertNotIn('action="/signin"', page)   # no password to type yet
        code, head, _ = self.req("GET", "/?key=" + KEY)   # the old link still signs this browser in
        self.assertEqual((code, head["location"]), (303, "/"))
        self.assertIn("hub_main=" + KEY, head["set-cookie"])
        self.assertEqual(self.req("GET", "/api/snapshot", {"Cookie": "hub_main=" + KEY})[0], 200)
        code, head, _ = self.req("GET", "/?key=" + "cd" * 32)
        self.assertEqual(code, 200)
        self.assertNotIn("set-cookie", head)
        self.assertEqual(json.loads(self.req("GET", "/api/about")[2])["signin"], "key")
        self.set()
        code, head, _ = self.req("GET", "/?key=" + pagekey.read())   # an app shell of an earlier build: no cookie
        self.assertEqual((code, head.get("set-cookie")), (303, None))
        self.assertEqual(self.req("GET", "/api/snapshot", {"Cookie": "hub_main=" + KEY})[0], 401)

    def test_the_notice_of_the_old_key(self):
        from modules.web import signin
        put = []
        self.web.put = lambda kind, entity, data: put.append((kind, data))
        with open(os.path.join(self.state, signin.OLD_TOLD), "w") as f:
            json.dump({"told": ["bob"]}, f)
        self.assertFalse(signin.notice(self.web))
        self.assertEqual(put, [("owner.notify_resolved", {"id": "page-key-users", "source": "web"})])   # the old hint
        pagekey.write(KEY)
        self.assertTrue(signin.notice(self.web))
        self.assertTrue(signin.notice(self.web))
        self.assertEqual([k for k, _ in put], ["owner.notify_resolved", "owner.notify"])   # once
        self.assertEqual(put[1][1]["id"], signin.NOTICE)
        self.set()   # from the terminal as well: the next look withdraws it
        self.assertFalse(signin.notice(self.web))
        self.assertEqual(put[-1], ("owner.notify_resolved", {"id": signin.NOTICE, "source": "web"}))


class Neighbours(PageServer):
    """The password of another hub of this machine from this hub's row list: its settings folder, its own counter."""
    def setUp(self):
        super().setUp()
        self.other = tempfile.mkdtemp(prefix="pagepass-neighbour-")
        self.addCleanup(current.rmtree, self.other)
        from core import instancecli
        from modules.web import instancesweb
        self.knows = True
        rows = [{"name": "", "command": "hub", "state": self.state, "config_dir": self.cfg, "ports": {"web": self.port},
                 "running": True},
                {"name": "personal", "command": "hub-personal", "state": "/home/x/.hub-personal",
                 "config_dir": self.other, "ports": {"web": 1}, "running": True}]
        for target, name, value in ((instancecli, "rows", lambda: rows),
                                    (instancesweb, "knows_password", lambda row: self.knows)):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def op(self, **body):
        code, head, out = self.req("POST", "/api/instances", None, {"name": "personal", **body})
        return code, head, json.loads(out)

    def test_set_change_and_take_off_a_neighbour_s(self):
        code, head, _ = self.op(op="password", new=PASSWORD)
        self.assertEqual(code, 200)
        self.assertNotIn("set-cookie", head)   # not this hub's: this tab gets no cookie
        self.assertEqual(pagepass.state(self.other), "password")
        self.assertEqual(pagepass.state(), "open")
        self.assertEqual(self.op(op="password", new="y" * 9)[0], 403)   # without its current one
        self.assertEqual(self.op(op="password_off", current="wrong one")[0], 403)
        self.assertEqual(self.op(op="password_off", current=PASSWORD)[0], 200)
        self.assertEqual(pagepass.state(self.other), "open")
        rows = json.loads(self.req("GET", "/api/instances")[2])["hubs"]
        self.assertEqual([(r["name"], r["signin"]) for r in rows], [("", "open"), ("personal", "open")])

    def test_not_on_an_earlier_version_nor_behind_its_old_key(self):
        self.knows = False
        self.assertEqual(self.op(op="password", new=PASSWORD)[0], 409)
        self.assertEqual(pagepass.state(self.other), "open")
        self.knows = True
        pagekey.write(KEY, self.other)
        self.assertEqual(self.op(op="password", new=PASSWORD)[0], 409)   # its key holds the door; this page has not it
        self.assertEqual(pagekey.read(self.other), KEY)

    def test_the_hubs_without_a_password(self):
        # a hub without one opens the others: the list under the hubs (and whoever else needs it) — core/pagepass.unprotected
        self.assertEqual(pagepass.unprotected(state_dir=self.state), ["", "personal"])
        self.set()
        self.assertEqual(pagepass.unprotected(state_dir=self.state), ["personal"])
        pagepass.set_password(PASSWORD, self.other)
        self.assertEqual(pagepass.unprotected(state_dir=self.state), [])
        self.assertEqual(pagepass.unprotected(lambda row: False, self.state), ["personal"])   # its code knows none
        self.assertEqual(json.loads(self.req("GET", "/api/instances", self.cookie())[2])["unprotected"], [])

    def test_removing_a_hub_with_a_password_asks_it(self):
        from core import instance
        from modules.web import instancesweb
        pagepass.set_password(PASSWORD, self.other)
        started = []
        with mock.patch.object(instance, "entries", lambda: [{"name": "personal"}]), \
                mock.patch.object(instancesweb, "_start", lambda web, data: started.append(data) or (202, {"ok": True})):
            self.assertEqual(self.op(op="remove", confirm="personal")[0], 403)   # the name is not enough
            self.assertEqual(self.op(op="remove", password=PASSWORD)[0], 202)
        self.assertEqual([d["name"] for d in started], ["personal"])


class CloudflareAccess(Folder):
    def test_the_person_cloudflare_names_signs_in_too(self):
        # the real check of the signed Access token (tests/test_cloudflare.py's servers, borrowed, not inherited)
        from tests import test_cloudflare
        case = test_cloudflare.Servers("test_the_listed_person_and_client_only")
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.assertEqual(case.page(), 200)
        rec = self.set()
        self.assertEqual(case.page(), 401)
        self.assertEqual(case.page(jwt=False), 403)   # no token: refused before the form
        self.assertEqual(case.page(Cookie=self.cookie(rec)["Cookie"]), 200)


class FilesServer(Folder):
    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp(prefix="pagepass-files-")
        self.addCleanup(current.rmtree, self.dir)
        self.files = Files(config=lambda: {"projects": {}}, state_dir=self.dir)
        self.server = self.files.serve(port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def req(self, method, path, extra=None, body=None):
        h = {"Host": f"127.0.0.1:{self.port}", "X-Hub": "1", "Content-Type": "application/json", **(extra or {})}
        return ask(self.port, method, path, h, body)

    def test_files_share_the_sign_in_of_the_page(self):
        self.assertEqual(self.req("GET", "/api/roots")[0], 200)
        rec = self.set()
        code, _, body = self.req("GET", "/api/roots")
        self.assertEqual(code, 401)
        self.assertEqual(json.loads(body)["code"], "signin")
        self.assertEqual(self.req("POST", "/api/save", None, {})[0], 401)
        self.assertEqual(self.req("GET", "/api/roots", self.cookie(rec))[0], 200)
        self.assertEqual(self.req("GET", "/api/roots", {"X-Forwarded-For": "203.0.113.7"})[0], 403)

    def test_a_report_takes_its_own_files_by_the_mark(self):
        # a /render report runs in a sandbox (an opaque origin): its css and images go without the cookie, the mark in
        # its address stands for it — for the report's folder and below only (its script can read the address)
        root = tempfile.mkdtemp(prefix="pagepass-report-")
        self.addCleanup(current.rmtree, root)
        for name, body in (("sub/r.html", "<link rel=stylesheet href=assets/s.css><p>report</p>"),
                           ("sub/assets/s.css", "p{color:red}"), ("other/x.css", "secret"), ("top.css", "secret")):
            os.makedirs(os.path.dirname(os.path.join(root, name)), exist_ok=True)
            with open(os.path.join(root, name), "w") as f:
                f.write(body)
        self.files.config = lambda: {"files": {"roots": {"demo": root}}}
        rec = self.set()
        cookie = self.cookie(rec)
        code, head, _ = self.req("GET", "/render/demo/sub/r.html", cookie)
        self.assertEqual(code, 303)
        marked = head["location"]
        self.assertTrue(marked.startswith("/render/~") and marked.endswith("/demo/sub/r.html"), marked)
        self.assertEqual(self.req("GET", marked, cookie)[0], 200)
        mark = marked.split("/")[2]
        sandboxed = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Dest": "style"}
        self.assertEqual(self.req("GET", f"/render/{mark}/demo/sub/assets/s.css", sandboxed)[0], 200)   # by the mark
        self.assertNotEqual(self.req("GET", "/render/demo/sub/assets/s.css", sandboxed)[0], 200)   # no cookie, no mark
        for elsewhere in ("other/x.css", "top.css", "sub/../other/x.css", "sub/%2e%2e/other/x.css", "sub//x.css",
                          "sub/..%2Fother/x.css", "sub/..%2ftop.css", "sub/%2E%2E%2Ftop.css"):
            self.assertNotEqual(self.req("GET", f"/render/{mark}/demo/{elsewhere}", sandboxed)[0], 200, elsewhere)
        self.assertNotEqual(self.req("GET", f"/render/~{'0' * 32}/demo/sub/assets/s.css", sandboxed)[0], 200)
        self.assertEqual(self.req("GET", f"/render/{mark}/demo/sub/assets/s.css",
                                  {"X-Forwarded-For": "203.0.113.7"})[0], 403)   # not this machine
        with mock.patch("time.time", return_value=time.time() + 3 * 86400):
            self.assertNotEqual(self.req("GET", f"/render/{mark}/demo/sub/assets/s.css", sandboxed)[0], 200)
        self.set("another password")
        self.assertNotEqual(self.req("GET", f"/render/{mark}/demo/sub/assets/s.css", sandboxed)[0], 200)   # a change


class Cli(Folder):
    def hub(self, *args, stdin=None, env_extra=None):
        env = {k: v for k, v in os.environ.items() if k not in ("HUB_SESSION", "HUB_EXTENSION", "HUB_MODULE")}
        state = tempfile.mkdtemp(prefix="pagepass-cli-")
        self.addCleanup(current.rmtree, state)
        env.update(HUB_STATE_DIR=state, HUB_WEB_PORT="8801", HUB_LANG="en", **(env_extra or {}))
        return subprocess.run([sys.executable, os.path.join(ROOT, "bin", "hub"), *args], capture_output=True,
                              text=True, encoding="utf-8", env=env, timeout=120, input=stdin or "")

    def test_set_status_off(self):
        r = self.hub("password")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no password", r.stdout)
        r = self.hub("password", "set", stdin="one password\nanother one\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(pagepass.state(), "open")
        r = self.hub("password", "set", stdin="short\nshort\n")
        self.assertNotEqual(r.returncode, 0)
        r = self.hub("password", "set", stdin=f"{PASSWORD}\n{PASSWORD}\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(PASSWORD, r.stdout + r.stderr)
        self.assertEqual(pagepass.state(), "password")
        self.assertTrue(pagepass.matches(PASSWORD))
        self.assertIn("password off", self.hub("password").stdout)
        r = self.hub("ui", "--print")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "http://localhost:8801/"), r.stderr)   # no key in it
        r = self.hub("password", "off")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(pagepass.state(), "open")
        self.assertIsNone(pagekey.read())

    def test_hub_ui_keeps_the_old_link_until_a_password(self):
        pagekey.write(KEY)
        r = self.hub("ui", "--print")
        self.assertEqual(r.stdout.strip(), f"http://localhost:8801/?key={KEY}")

    def test_a_session_cannot(self):
        r = self.hub("password", "off", env_extra={"HUB_SESSION": "maps"})
        self.assertNotEqual(r.returncode, 0)
        self.set()
        r = self.hub("password", "off", env_extra={"HUB_SESSION": "maps"})
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(pagepass.state(), "password")
        r = self.hub("password", "set", stdin="x" * 9 + "\n" + "x" * 9 + "\n", env_extra={"HUB_SESSION": "maps"})
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue(pagepass.matches(PASSWORD))

    def test_doctor_row(self):
        from core import modcli
        self.assertEqual(modcli.password_rows(), [])
        self.set()
        self.assertEqual(modcli.password_rows()[0][0], "ok")
        with open(pagepass.path(), "w") as f:
            f.write("x")
        self.assertEqual(modcli.password_rows()[0][0], "fail")


class AppShells(Folder):
    """The app's launchers (frozen copies: they import nothing of the hub) open the old link only in the state "key":
    the key a password keeps for a rollback is never opened (a command line every account reads, the history)."""
    def load(self, name, path):
        import importlib.util
        spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_the_key_only_without_a_password(self):
        deb = self.load("pagepass_open_page", "tools/package/deb/files/open_page.py")
        win = self.load("pagepass_win_app", "tools/package/windows/app.py")
        state = tempfile.mkdtemp(prefix="pagepass-shell-")
        self.addCleanup(current.rmtree, state)
        env = {"HUB_CONFIG_DIR": self.cfg, "HUB_STATE_DIR": state}
        pagekey.write(KEY)
        self.assertTrue(deb.target("http://127.0.0.1:8787/", env).startswith("file://"))   # the opener of the old link
        appdata = tempfile.mkdtemp(prefix="pagepass-appdata-")   # %APPDATA%\\hub-core of the Windows app
        self.addCleanup(current.rmtree, appdata)
        folder = os.path.join(appdata, "hub-core")
        pagekey.write(KEY, folder)
        with mock.patch.dict(os.environ, {"APPDATA": appdata}):
            self.assertIn("?key=" + KEY, win.page_url(8787))
            pagepass.set_password(PASSWORD, folder)
            self.assertEqual(win.page_url(8787), "http://127.0.0.1:8787/")
        self.set()
        self.assertEqual(deb.target("http://127.0.0.1:8787/", env), "http://127.0.0.1:8787/")


class Bot(Folder):
    def test_page(self):
        from modules.telegram import bot
        self.assertIn("/page", bot.COMMANDS)
        d = tempfile.mkdtemp(prefix="pagepass-bot-")
        self.addCleanup(current.rmtree, d)
        b = bot.Bot(d, api=None)
        with mock.patch.dict(os.environ, {"HUB_WEB_PORT": "8801"}):
            pagekey.write(KEY)
            self.assertIn("http://localhost:8801/", b.page_text())
            self.assertNotIn("key=", b.page_text())   # never a key into a chat


if __name__ == "__main__":
    unittest.main()
