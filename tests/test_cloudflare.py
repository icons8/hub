"""The cloudflare provider of remote access: the hub page through the owner's own Cloudflare Tunnel, behind Cloudflare
Access (core/remote.py, modules/cloudflare). Who came is taken only from the signed JWT in Cf-Access-Jwt-Assertion:
RS256 under the team's keys, its aud, its issuer, not expired, an e-mail from the owner's list or the Client ID of a
listed service token; unsigned headers are never believed, and only a connection from this machine (cloudflared) may
carry it. Off by default: no network, and a request through the tunnel is refused as any proxied one. The keys and the
tunnel's state come from fakes — nothing here reaches Cloudflare."""
import tests  # noqa: F401 — so the machine's settings are not seen
import http.client
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

from core import i18n, remote
from modules.cloudflare import cli, remote as cf
from modules.cloudflare.run import Cloudflare
from modules.files.server import Files
from modules.tailnet import access
from modules.web import server as web_server
from modules.web.server import Web
from tests.fakes.rsa import Key

KEY, OTHER = Key("kid-1"), Key("kid-1")   # OTHER: the same key id, not the team's key
SPARE = Key("kid-2")
TEAM = "myteam.cloudflareaccess.com"
AUD = "a" * 64
ME = "me@example.test"
CLIENT = "0123456789abcdef.access"   # a service token's Client ID (made up)
SETTINGS = {"enabled": True, "team": "myteam", "aud": [AUD], "emails": ["Me@Example.test"],
            "service_tokens": [CLIENT], "url": "https://hub.example.test/"}
PUBLIC = "hub.example.test"
HUB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")


def claims(**over):
    now = time.time()
    out = {"aud": [AUD], "email": ME, "exp": int(now) + 600, "iat": int(now), "nbf": int(now),
           "iss": f"https://{TEAM}", "type": "app", "sub": "0000", **over}
    return {k: v for k, v in out.items() if v is not None}


def service(**over):
    """The JWT Access gives a service token: no e-mail, the Client ID in common_name."""
    return claims(**{"email": None, "common_name": CLIENT, "sub": "", **over})


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cloudflare-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.conf = tempfile.mkdtemp(prefix="cloudflare-conf-")
        self.addCleanup(shutil.rmtree, self.conf, True)
        env = mock.patch.dict(os.environ, {"HUB_CONFIG": os.path.join(self.conf, "hub.json")})
        env.start()
        self.addCleanup(env.stop)
        self.write(SETTINGS)
        self.up, self.now = True, time.time()
        self.fetched, self.jwks = [], {"keys": [KEY.jwk(), SPARE.jwk(), {"kty": "EC", "kid": "ec"}],
                                       "public_cert": {"kid": "kid-1", "cert": "-----"}}
        self.cf = Cloudflare(self.dir, fetch=self.fetch, ready=lambda addr: self.up, clock=lambda: self.now)

    def write(self, data):
        cf.save_settings(self.dir, dict(data))

    def edit(self, **over):
        self.write({**cf.load_settings(self.dir), **over})

    def fetch(self, url):
        self.fetched.append(url)
        if isinstance(self.jwks, Exception):
            raise self.jwks
        return self.jwks


class Process(Base):
    """What the provider's process (hub-up part `cloudflare`) writes, and when it goes to the network."""

    def test_off_by_default_means_no_network(self):
        os.unlink(cf.settings_path(self.dir))
        st = self.cf.tick()
        self.assertFalse(st["ok"])
        self.assertEqual(cf.why_text(st), i18n.t("cloudflare.why_off"))
        self.assertEqual(self.fetched, [])
        self.edit(enabled=False)
        self.assertEqual(cf.why_text(self.cf.tick()), i18n.t("cloudflare.why_off"))
        self.assertEqual(self.fetched, [])
        self.assertFalse(cf.status(self.dir)["ok"])

    def test_incomplete_settings_say_what_is_missing(self):
        self.write({"enabled": True, "url": "http://hub.example.test", "team": "evil.example.test",
                    "emails": ["nobody"]})
        st = self.cf.tick()
        self.assertFalse(st["ok"])
        for name in ("team", "aud", "url", "emails"):
            self.assertIn(name, cf.why_text(st))
        self.assertEqual(self.fetched, [])

    def test_set_up(self):
        st = self.cf.tick()
        self.assertEqual(self.fetched, [f"https://{TEAM}/cdn-cgi/access/certs"])
        self.assertTrue(st["ok"], cf.why_text(st))
        got = cf.status(self.dir)
        self.assertTrue(got["ok"])
        self.assertEqual(got["hosts"], [PUBLIC])
        self.assertEqual(sorted(got["keys"]), ["kid-1", "kid-2"])   # RSA only
        self.assertEqual(got["tunnel"], "unknown")   # no metrics address: whose cloudflared answers is unknown
        if os.name != "nt":   # file permissions are POSIX; on Windows chmod only knows read-only
            for path in (os.path.join(self.dir, cf.STATUS), cf.settings_path(self.dir)):
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600, path)

    def test_the_tunnel_by_its_own_metrics_address(self):
        asked = []
        self.cf.ready = lambda addr: asked.append(addr) or self.up
        self.edit(metrics="127.0.0.1:20299")
        self.assertEqual(self.cf.tick()["tunnel"], "up")
        self.up = False
        self.now += cf.EVERY
        self.assertEqual(self.cf.tick()["tunnel"], "down")
        self.assertEqual(set(asked), {"127.0.0.1:20299"})

    def test_keys_are_fetched_hourly_or_when_a_server_asks(self):
        self.cf.tick()
        self.now += 120
        self.cf.tick()
        self.assertEqual(len(self.fetched), 1)
        open(os.path.join(self.dir, cf.REFRESH), "w").close()     # a server met an unknown key id
        self.cf.tick()
        self.assertEqual(len(self.fetched), 2)
        self.assertFalse(os.path.exists(os.path.join(self.dir, cf.REFRESH)))
        open(os.path.join(self.dir, cf.REFRESH), "w").close()
        self.now += 10
        self.cf.tick()                                            # asked again too soon: not yet
        self.assertEqual(len(self.fetched), 2)
        self.now += cf.KEYS_EVERY
        self.cf.tick()
        self.assertEqual(len(self.fetched), 3)

    def test_a_failed_fetch_keeps_the_keys_for_a_week_at_most(self):
        self.jwks = OSError("timed out")
        st = self.cf.tick()
        self.assertFalse(st["ok"])
        self.assertIn("timed out", cf.why_text(st))
        self.jwks = {"keys": [KEY.jwk()]}
        self.now += cf.REFETCH_AFTER
        self.assertTrue(self.cf.tick()["ok"])
        self.jwks = ValueError("not JSON")       # the keys it has keep working
        self.now += cf.KEYS_EVERY
        st = self.cf.tick()
        self.assertTrue(st["ok"])
        self.assertEqual(list(st["keys"]), ["kid-1"])
        self.now += cf.KEYS_MAX_AGE
        st = self.cf.tick()
        self.assertFalse(st["ok"])
        self.assertFalse(cf.status(self.dir, now=self.now)["ok"])

    def test_a_request_for_keys_wakes_it_once_a_minute_at_most(self):
        self.cf.tick()
        open(os.path.join(self.dir, cf.REFRESH), "w").close()   # a token with a made-up key id
        self.now += 5
        self.assertFalse(self.cf.asked())
        self.edit(team="other")
        self.assertTrue(self.cf.asked())   # a new team: at once
        self.edit(team="myteam")
        self.now += cf.REFETCH_AFTER
        self.assertTrue(self.cf.asked())

    def test_a_request_for_keys_while_off_is_dropped(self):
        open(os.path.join(self.dir, cf.REFRESH), "w").close()
        self.edit(enabled=False)
        self.cf.tick()
        self.assertFalse(os.path.exists(os.path.join(self.dir, cf.REFRESH)))   # else the loop would wake every 5 s

    def test_a_broken_answer_is_a_failed_fetch(self):
        self.jwks = http.client.IncompleteRead(b"{")
        st = self.cf.tick()
        self.assertFalse(st["ok"])
        import socket
        from modules.cloudflare import run
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        self.addCleanup(srv.close)

        def garbage():
            conn, _ = srv.accept()
            conn.recv(1024)
            conn.sendall(b"not http at all\r\n\r\n")
            conn.close()
        threading.Thread(target=garbage, daemon=True).start()
        self.assertIs(run.ready(f"127.0.0.1:{srv.getsockname()[1]}"), False)   # down, not a crash of the part

    def test_another_team_drops_the_keys(self):
        self.cf.tick()
        self.edit(team="other")
        self.jwks = OSError("down")
        self.now += cf.REFETCH_AFTER
        st = self.cf.tick()
        self.assertEqual(self.fetched[-1], "https://other.cloudflareaccess.com/cdn-cgi/access/certs")
        self.assertFalse(st["ok"])
        self.assertEqual(st.get("keys") or {}, {})


class Identity(Base):
    """identify(): the signed token only, from this machine only."""

    def setUp(self):
        super().setUp()
        self.cf.tick()

    def who(self, token=None, host=PUBLIC, peer="127.0.0.1", **headers):
        if token is not None:
            headers["Cf-Access-Jwt-Assertion"] = token
        return cf.identify(self.dir, remote.request(host, peer, headers))

    def why(self, token):
        return cf.verify(token, *cf.current(self.dir), time.time())[1]

    def test_the_signed_token_of_a_listed_person(self):
        self.assertEqual(self.who(KEY.sign(claims())), ME)
        self.assertEqual(self.who(SPARE.sign(claims())), ME)
        self.assertEqual(self.who(KEY.sign(claims(email="ME@example.TEST", aud=AUD))), ME)   # a single aud, any case
        self.assertEqual(self.who(KEY.sign(claims()), host="localhost:8787"), ME)   # a tunnel that rewrites Host

    def test_a_listed_service_token_is_a_client_not_nobody(self):
        self.assertEqual(self.who(KEY.sign(service())), "service:" + CLIENT)
        self.assertEqual(self.who(KEY.sign(service(common_name=CLIENT.upper()))), "service:" + CLIENT)
        self.assertEqual(self.why(KEY.sign(service(common_name="fedcba9876543210.access"))), "not listed")
        self.assertEqual(self.why(KEY.sign(service(common_name=None))), "no identity")
        self.edit(service_tokens=[])
        self.assertIsNone(self.who(KEY.sign(service())))

    def test_unsigned_headers_are_not_believed(self):
        self.assertIsNone(self.who(**{"Cf-Access-Authenticated-User-Email": ME}))
        self.assertIsNone(self.who(**{"Cf-Access-Authenticated-User-Email": ME, "Cf-Ray": "1"}))
        self.assertIsNone(self.who(""))

    def test_only_from_this_machine(self):
        token = KEY.sign(claims())
        for peer in ("100.3.3.3", "192.0.2.10", "203.0.113.7", "::ffff:203.0.113.7"):
            self.assertIsNone(self.who(token, peer=peer), peer)

    def test_only_its_own_host_or_a_rewritten_localhost(self):
        token = KEY.sign(claims())
        self.assertIsNone(self.who(token, host="evil.example.test"))
        self.assertIsNone(self.who(token, host=""))

    def test_refusals(self):
        now = time.time()
        signed = KEY.sign(claims())
        head, body, sig = signed.split(".")
        forged_body = KEY.sign(claims(email="evil@example.test")).split(".")[1]
        cases = {
            "not RS256": [KEY.sign(claims(), alg="none"), KEY.sign(claims(), alg="HS256")],
            "bad signature": [OTHER.sign(claims()), f"{head}.{forged_body}.{sig}", f"{head}.{body}."],
            "not for this application": [KEY.sign(claims(aud=["b" * 64])), KEY.sign(claims(aud=[]))],
            "wrong issuer": [KEY.sign(claims(iss="https://evil.cloudflareaccess.com")), KEY.sign(claims(iss=False))],
            "expired": [KEY.sign(claims(exp=int(now) - cf.SKEW - 5)), KEY.sign(claims(exp="later")),
                        KEY.sign(claims(exp=True))],
            "not valid yet": [KEY.sign(claims(nbf=int(now) + cf.SKEW + 60))],
            "no identity": [KEY.sign(claims(email=["x"])), KEY.sign(claims(email="nobody"))],
            "not listed": [KEY.sign(claims(email="guest@example.test"))],
            "not a JWT": ["a.b", "x" * (cf.MAX_TOKEN + 1), "a.b.c.d", "...", "%%%.%%%.%%%"],
        }
        for why, tokens in cases.items():
            for token in tokens:
                self.assertEqual(self.why(token), why, token[:60])
                self.assertIsNone(self.who(token), token[:60])
        self.assertIsNone(self.why(KEY.sign(claims(exp=int(now) - cf.SKEW + 20))))   # clock skew

    def test_an_unknown_key_asks_for_fresh_keys(self):
        stranger = Key("kid-9")
        self.assertEqual(self.why(stranger.sign(claims())), "unknown signing key")
        self.assertIsNone(self.who(stranger.sign(claims())))
        self.assertTrue(os.path.exists(os.path.join(self.dir, cf.REFRESH)))

    def test_keys_older_than_a_week_are_not_used(self):
        token = KEY.sign(claims())
        with mock.patch("time.time", return_value=time.time() + cf.KEYS_MAX_AGE + 60):
            self.assertIsNone(cf.identify(self.dir, remote.request(PUBLIC, "127.0.0.1",
                                                                   {"Cf-Access-Jwt-Assertion": token})))

    def test_turned_off_is_nobody_at_once(self):
        token = KEY.sign(claims())
        self.edit(enabled=False)   # before the process notices
        self.assertIsNone(self.who(token))
        self.edit(enabled=True, emails=["other@example.test"])   # a person removed from the list: at once too
        self.assertIsNone(self.who(token))

    def test_keys_of_another_team_are_not_used(self):
        token = KEY.sign(claims(iss="https://other.cloudflareaccess.com"))
        self.edit(team="other")   # the process has not fetched the new team's keys yet
        self.assertIsNone(self.who(token))

    def test_claims_by_host_and_by_cloudflare_headers(self):
        def claimed(host, **headers):
            return cf.claims(self.dir, remote.request(host, "127.0.0.1", headers))
        self.assertTrue(claimed("HUB.example.test"))
        for h in ("Cf-Ray", "Cf-Connecting-Ip", "Cf-Access-Jwt-Assertion"):
            self.assertTrue(claimed("localhost:8787", **{h: "1"}), h)
        self.assertTrue(claimed("localhost:8787", **{"Cdn-Loop": "cloudflare; loops=1"}))
        self.assertFalse(claimed("localhost:8787"))
        self.assertFalse(claimed("localhost:8787", **{"Cdn-Loop": "fastly"}))


class Admission(Base):
    """access.allowed / core/remote.admit with the provider on and off."""

    def setUp(self):
        super().setUp()
        self.cf.tick()
        self.jwt = KEY.sign(claims())

    def allowed(self, host=PUBLIC, peer="127.0.0.1", **headers):
        return access.allowed(self.dir, host, peer, headers={"Cf-Ray": "8f-WAW", **headers})

    def test_admit_names_who_came(self):
        self.assertEqual(remote.admit(self.dir, PUBLIC, "127.0.0.1", headers={"Cf-Access-Jwt-Assertion": self.jwt}),
                         {"local": False, "provider": "cloudflare", "who": ME})
        self.assertEqual(remote.admit(self.dir, "localhost:8787", "127.0.0.1", headers={}), {"local": True})
        self.assertIsNone(remote.admit(self.dir, PUBLIC, "127.0.0.1", headers={}))

    def test_through_the_tunnel_is_never_local(self):
        h = {"Cf-Access-Jwt-Assertion": self.jwt}
        self.assertTrue(self.allowed(**h))
        self.assertTrue(self.allowed("localhost:8788", **h))
        self.assertFalse(access.local("localhost:8788", "127.0.0.1", headers=h))   # Files: projects, not the disk

    def test_off_is_as_any_proxy(self):
        self.edit(enabled=False)
        self.assertFalse(self.allowed(**{"Cf-Access-Jwt-Assertion": self.jwt}))
        self.assertFalse(self.allowed("localhost:8787", **{"Cf-Access-Jwt-Assertion": self.jwt}))
        self.assertTrue(access.allowed(self.dir, "localhost:8787", "127.0.0.1", headers={}))

    def test_a_tailnet_peer_with_a_token_gets_nothing_from_it(self):
        with open(os.path.join(self.dir, "tailnet.json"), "w") as f:
            json.dump({"ok": True, "ts": time.time(),
                       "owner": "owner@x", "ip": "100.9.9.9", "hosts": ["100.9.9.9:8787"]}, f)
        whois = lambda _, p: {"100.1.1.1": "owner@x", "100.2.2.2": "guest@x"}.get(p)   # noqa: E731
        h = {"Cf-Access-Jwt-Assertion": self.jwt, "Cf-Ray": "1"}
        for host in ("100.9.9.9:8787", PUBLIC, "localhost:8787"):
            self.assertFalse(access.allowed(self.dir, host, "100.2.2.2", whois=whois, headers=h), host)
            self.assertFalse(access.allowed(self.dir, host, "192.0.2.10", whois=whois, headers=h), host)
        self.assertTrue(access.allowed(self.dir, "100.9.9.9:8787", "100.1.1.1", whois=whois, headers=h))   # as before

    def test_a_failing_provider_closes_only_its_own_door(self):
        with mock.patch.object(cf, "verify", side_effect=RuntimeError("bug")):
            self.assertFalse(self.allowed(**{"Cf-Access-Jwt-Assertion": self.jwt}))
            self.assertTrue(access.allowed(self.dir, "localhost:8787", "127.0.0.1", headers={}))


def call(port, method, path, host, headers=None, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, path, json.dumps(body) if body is not None else None, {"Host": host, **(headers or {})})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


class Servers(Base):
    """The page and Files behind the tunnel: cloudflared reaches 127.0.0.1 with the public Host and the JWT."""

    def setUp(self):
        super().setUp()
        self.cf.tick()
        self.web = Web(self.dir)
        self.web.decide_wait = 0
        self.server = self.web.serve(port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.web.stop.set)
        self.jwt = KEY.sign(claims())

    def page(self, path="/api/snapshot", host=PUBLIC, jwt=True, method="GET", body=None, **headers):
        h = {**({"Cf-Access-Jwt-Assertion": jwt if isinstance(jwt, str) else self.jwt} if jwt else {}),
             "Cf-Ray": "8f-WAW", "Cf-Connecting-Ip": "203.0.113.7", "X-Forwarded-For": "203.0.113.7",
             "Cdn-Loop": "cloudflare", **headers}
        return call(self.port, method, path, host, h, body)[0]

    def test_the_listed_person_and_client_only(self):
        self.assertEqual(self.page(), 200)
        self.assertEqual(self.page(jwt=KEY.sign(service())), 200)
        self.assertEqual(self.page(host="localhost:8787"), 200)   # a tunnel that rewrites Host
        self.assertEqual(self.page(jwt=False), 403)
        self.assertEqual(self.page(jwt=False, **{"Cf-Access-Authenticated-User-Email": ME}), 403)
        self.assertEqual(self.page(jwt=KEY.sign(claims(email="guest@example.test"))), 403)
        self.assertEqual(self.page(jwt=KEY.sign(service(common_name="fedcba9876543210.access"))), 403)
        self.assertEqual(self.page(jwt=KEY.sign(claims(aud=["b" * 64]))), 403)
        self.assertEqual(self.page(jwt=KEY.sign(claims(iss="https://evil.cloudflareaccess.com"))), 403)
        self.assertEqual(self.page(jwt=KEY.sign(claims(exp=int(time.time()) - cf.SKEW - 5))), 403)
        self.assertEqual(self.page(jwt=OTHER.sign(claims())), 403)
        self.assertEqual(self.page(host="evil.example.test"), 403)

    def test_a_post_from_the_page_behind_the_tunnel(self):
        own = {"Origin": "https://" + PUBLIC, "Sec-Fetch-Site": "same-origin", "X-Hub": "1",
               "Content-Type": "application/json"}
        self.assertEqual(self.page("/api/paths", method="POST", body={"items": []}, **own), 200)
        self.assertEqual(self.page("/api/paths", method="POST", body={"items": []}, jwt=False, **own), 403)

    def test_the_refusal_names_cloudflare(self):
        code, body = call(self.port, "GET", "/api/snapshot", PUBLIC, {"Cf-Ray": "8f-WAW"})
        self.assertEqual((code, json.loads(body)["error"]), (403, i18n.t("web.srv.err_cloudflare")))
        self.edit(enabled=False)
        code, body = call(self.port, "GET", "/api/snapshot", PUBLIC, {"Cf-Ray": "8f-WAW"})
        self.assertEqual((code, json.loads(body)["error"]), (403, i18n.t("web.srv.err_proxied")))

    def test_a_signed_in_person_refused_for_another_reason_hears_that_reason(self):
        code, body = call(self.port, "POST", "/api/send", PUBLIC,
                          {"Cf-Access-Jwt-Assertion": self.jwt, "Cf-Ray": "8f-WAW", "Content-Type": "application/json"},
                          {"session": "maps", "text": "hi"})   # no X-Hub: 1
        self.assertEqual(code, 403)
        self.assertEqual(json.loads(body)["error"], i18n.t("web.srv.err_not_from_page"))

    def test_off_is_slice_zero(self):
        self.edit(enabled=False)
        self.assertEqual(self.page(), 403)
        self.assertEqual(self.page(host="localhost:8787"), 403)
        self.assertEqual(call(self.port, "GET", "/api/snapshot", f"localhost:{self.port}")[0], 200)

    def test_an_open_stream_is_checked_again(self):
        with mock.patch.object(web_server, "STREAM_WAIT", 0.2), mock.patch.object(web_server, "STREAM_RECHECK", 0.5):
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            c.request("GET", "/api/stream", None, {"Host": PUBLIC, "Cf-Access-Jwt-Assertion": self.jwt,
                                                     "Cf-Ray": "8f-WAW"})
            r = c.getresponse()
            self.assertEqual(r.status, 200)
            r.fp.readline()
            self.edit(emails=["other@example.test"], service_tokens=[])   # the owner removed the person
            ended = threading.Event()

            def drain():
                try:
                    while r.fp.readline():
                        pass
                except OSError:
                    pass
                ended.set()
            threading.Thread(target=drain, daemon=True).start()
            self.assertTrue(ended.wait(8), "the stream stayed open for a removed person")
            c.close()

    def test_files_lets_in_projects_not_the_disk(self):
        files = Files(config=lambda: {"projects": {}}, state_dir=self.dir)
        server = files.serve(port=0)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        h = {"Cf-Access-Jwt-Assertion": self.jwt, "Cf-Ray": "8f-WAW"}
        code, body = call(port, "GET", "/api/roots", "localhost:8788", h)
        self.assertEqual(code, 200, body)
        self.assertNotIn("Mac", [r["name"] for r in json.loads(body)])
        code, body = call(port, "GET", "/api/roots", "localhost:8788", {"Cf-Ray": "8f-WAW"})
        self.assertEqual((code, json.loads(body)["error"]), (403, i18n.t("files.cloudflare")))


class Commands(Base):
    """hub remote cloudflare set|status|on|off — the settings file in the settings folder, 0600."""

    def run_cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(list(argv), self.dir)
        return code, out.getvalue()

    def test_set_then_on(self):
        os.unlink(cf.settings_path(self.dir))
        code, out = self.run_cli("set", "--team", "myteam", "--aud", AUD, "--email", "Me@Example.test",
                                 "--email", "two@example.test", "--token-id", CLIENT, "--url", "https://hub.example.test")
        self.assertEqual(code, 0, out)
        got = cf.load_settings(self.dir)
        self.assertEqual((got["team"], got["aud"], got["emails"], got["service_tokens"], got["url"]),
                         ("myteam", [AUD], ["Me@Example.test", "two@example.test"], [CLIENT], "https://hub.example.test"))
        self.assertFalse(got.get("enabled"))   # set does not turn it on
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(os.stat(cf.settings_path(self.dir)).st_mode), 0o600)
        code, out = self.run_cli("on")
        self.assertEqual(code, 0, out)
        self.assertTrue(cf.load_settings(self.dir)["enabled"])
        self.assertTrue(os.path.exists(os.path.join(self.dir, cf.REFRESH)))   # the process wakes up at once
        code, out = self.run_cli("off")
        self.assertEqual(code, 0)
        self.assertFalse(cf.load_settings(self.dir)["enabled"])

    def test_set_replaces_only_what_it_names(self):
        code, _ = self.run_cli("set", "--email", "new@example.test")
        self.assertEqual(code, 0)
        got = cf.load_settings(self.dir)
        self.assertEqual((got["emails"], got["service_tokens"], got["team"]), (["new@example.test"], [CLIENT], "myteam"))
        self.run_cli("set", "--no-token-ids")
        self.assertEqual(cf.load_settings(self.dir)["service_tokens"], [])

    def test_on_refuses_incomplete_settings(self):
        self.write({})
        code, out = self.run_cli("on")
        self.assertNotEqual(code, 0)
        self.assertIn("team", out)
        self.assertFalse(cf.load_settings(self.dir).get("enabled"))

    def test_bad_values_are_refused(self):
        for argv in (("--team", "evil.example.test"), ("--url", "http://hub.example.test"), ("--email", "nobody"),
                     ("--aud", ""), ("--token-id", "a b")):
            code, out = self.run_cli("set", *argv)
            self.assertNotEqual(code, 0, argv)
        self.assertEqual(cf.load_settings(self.dir)["team"], "myteam")

    def test_status_shows_keys_tunnel_and_reach_apart(self):
        self.cf.tick()
        with mock.patch.object(cli, "reach", return_value=("ok", "Access")):
            code, out = self.run_cli("status")
        self.assertEqual(code, 0)
        for word in ("keys", "tunnel", "reach"):
            self.assertIn(word, out)

    def test_doctor_does_not_call_a_checked_cloudflared_a_refused_proxy(self):
        from core import modcli
        self.cf.tick()
        with mock.patch.object(access, "proxy_programs", return_value=["cloudflared"]), \
                mock.patch.object(access, "refuses_proxied", return_value=True):
            self.assertEqual([w for _, w, d in modcli.proxy_rows({"web": 1}, self.dir) if "cloudflared" in d], [])
            self.edit(enabled=False)
            self.assertEqual(len([d for _, _, d in modcli.proxy_rows({"web": 1}, self.dir) if "cloudflared" in d]), 1)

    def test_doctor_rows(self):
        saved = cf.load_settings(self.dir)
        os.unlink(cf.settings_path(self.dir))
        self.assertEqual(cli.doctor_rows(self.dir, reach=lambda u: ("ok", "")), [])   # never set up: not a word
        self.write({**saved, "enabled": False})
        self.assertEqual([r[0] for r in cli.doctor_rows(self.dir, reach=lambda u: ("ok", ""))], ["ok"])   # set, off
        self.write(saved)
        self.cf.tick()
        with mock.patch.object(cli, "reach", return_value=("fail", "200")):
            rows = cli.doctor_rows(self.dir)
        what = [w for _, w, _ in rows]
        self.assertEqual(what, ["cloudflare · keys", "cloudflare · tunnel", "cloudflare · reach"])
        self.assertEqual(rows[0][0], "ok")
        self.assertEqual(rows[2][0], "fail")


class Command(Base):
    """The `hub` command: a session may look, only the owner or the hub turns it on and off or changes it."""

    def hub(self, *args, me=None):
        env = {k: v for k, v in os.environ.items() if k not in ("HUB_SESSION", "HUB_EXTENSION", "HUB_MODULE")}
        env.update({"HUB_STATE_DIR": self.dir, "HUB_CLI_WAIT": "0", **({"HUB_SESSION": me} if me else {})})
        return subprocess.run([sys.executable, HUB, "remote", "cloudflare", *args], env=env, capture_output=True,
                              text=True, encoding="utf-8", timeout=30)

    def test_a_session_may_look_only(self):
        self.assertEqual(self.hub("status", me="maps").returncode, 0)
        for args in (("on",), ("off",), ("set", "--email", "evil@example.test")):
            got = self.hub(*args, me="maps")
            self.assertNotEqual(got.returncode, 0, args)
            self.assertIn(i18n.tr("en", "cloudflare.cli.only_owner"), got.stderr, args)   # a session reads English
        self.assertEqual(cf.load_settings(self.dir)["emails"], SETTINGS["emails"])
        got = self.hub("off")   # the owner in a terminal
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertFalse(cf.load_settings(self.dir)["enabled"])


class Reach(unittest.TestCase):
    """The end-to-end probe of the public address, without credentials: who answers in front of the hub."""

    def test_a_broken_answer_is_a_warning(self):
        class Opener:
            def open(self, *a, **k):
                raise http.client.BadStatusLine("x")
        with mock.patch("urllib.request.build_opener", return_value=Opener()):
            self.assertEqual(cli.reach("https://hub.example.test")[0], "warn")

    def test_verdicts(self):
        cases = [((302, {"Location": "https://myteam.cloudflareaccess.com/cdn-cgi/access/login"}, b""), "ok"),
                 ((401, {}, b""), "ok"),
                 ((403, {}, b'{"error": "x"}'), "warn"),
                 ((200, {}, b"<html>"), "fail")]
        for answer, level in cases:
            self.assertEqual(cli.judge(*answer)[0], level, answer)


if __name__ == "__main__":
    unittest.main()
