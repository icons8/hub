"""The hub bus relay, slice L2 (spec hubbus-ux §4.0, §6 (a)–(e)): what the relay takes for linking two hubs by one link
and for the protocol's version 2 — the outer form of v2 envelopes next to v1, withdrawing an unused invitation, the
quota that reserves the live invitations, the hub's bus version announced by a header and asked by its peers, and the
operator's first invitation (`server.py bootstrap`).

The server runs as a real process on a free port (tests/test_hubbus_s2.RelayProcess); the quota's time rules are
checked on the Relay class with a clock of the test's own. Without ssh-keygen everything here is skipped."""
import tests  # noqa: F401 — keeps the machine's settings out
import base64
import contextlib
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from tests.test_hubbus import need_ssh
from tests.test_hubbus_s2 import Ident, RelayProcess, box, intro, server_module
from core import bus, busrelay


def head_envelope(src, head):
    """An envelope with the given head, signed by src's own key (the relay reads the form only)."""
    text = json.dumps(head, separators=(",", ":"))
    return json.dumps({"head": text, "sig": bus.sign(src.keys.sign_key, text.encode())}, separators=(",", ":")).encode()


def box_head(src, dst, **over):
    ts = int(time.time())
    head = {"v": 2, "t": "box", "id": os.urandom(16).hex(), "ts": ts, "exp": ts + 86400, "from": src.fp, "to": dst.fp,
            "ct": base64.b64encode(os.urandom(32 + 1024 + 16)).decode(), "cls": "chat"}
    head.update(over)
    return {k: v for k, v in head.items() if v is not None}


def intro_head(src, **over):
    ts = int(time.time())
    card = {"label": "Hub", "sign_pub": bus._b64(src.keys.sign_pub), "enc_pub": bus._b64(src.keys.enc_pub),
            "relays": [], "agents": {}, "bus_v": 2}
    head = {"v": 2, "t": "intro", "id": os.urandom(16).hex(), "ts": ts, "exp": ts + 86400, "card": card}
    head.update(over)
    return {k: v for k, v in head.items() if v is not None}


def raw_sig():
    """A one-time key's signature as the link carries it: standard base64 of 64 bytes (88 characters)."""
    return base64.b64encode(os.urandom(64)).decode()


def signed(h, method, path, payload=None, version=None):
    """A request signed by h's transport key, with exactly the headers named here (X-Hub-Bus-V only if version is
    given): (status, the answer's JSON)."""
    body = b"" if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    ts, nonce = int(time.time()), os.urandom(16).hex()
    sig = bus.ssh_sign(h.tkey, busrelay.request_line(h.relay.key, method, path, ts, nonce, body), busrelay.NS)
    headers = {"X-Hub-Fp": h.fp, "X-Hub-Time": str(ts), "X-Hub-Nonce": nonce, "Content-Type": "application/json",
               "X-Hub-Sig": base64.b64encode(sig.encode()).decode()}
    if version is not None:
        headers["X-Hub-Bus-V"] = str(version)
    req = urllib.request.Request(h.relay.url + path, data=body if method == "POST" else None, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        with e:
            return e.code, json.loads(e.read())


def peer(h, fp):
    return h.client.call("GET", "/v1/peer", query=f"fp={fp}")


def revoke(h, iid):
    return h.client.call("POST", "/v1/invite/revoke", {"id": iid})


def bus_v(value):
    """The hub's client sends X-Hub-Bus-V as `value` (None — without it), as a hub of another version would."""
    from unittest import mock
    orig = busrelay.Client._http

    def http(client, method, target, body, headers, timeout, limit):
        headers = {k: v for k, v in headers.items() if k != "X-Hub-Bus-V"}
        return orig(client, method, target, body, {**headers, **({"X-Hub-Bus-V": value} if value else {})}, timeout,
                    limit)
    return mock.patch.object(busrelay.Client, "_http", http)


@need_ssh
class Relay2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.relay = RelayProcess()

    @classmethod
    def tearDownClass(cls):
        cls.relay.stop()

    def joined(self, name="hub", by=None):
        h = Ident(self.relay, name)
        h.join(by.invite() if by else self.relay.invite(), name)
        return h

    def refused(self, code, fn, *args):
        with self.assertRaises(busrelay.Error) as e:
            fn(*args)
        self.assertEqual(e.exception.code, code)

    # (a) the outer form: v2 next to v1
    def test_v2_boxes_with_a_class_go_through_next_to_v1(self):
        a, b = self.joined("va"), self.joined("vb")
        b.client.allow([a.fp])
        for cls in ("chat", "line", "liaison", "ctl"):
            self.assertEqual(a.client.send(b.fp, head_envelope(a, box_head(a, b, cls=cls)))["stored"], "new")
        self.assertEqual(a.client.send(b.fp, box(a, b)[0])["stored"], "new")          # v1, as before
        for head in (box_head(a, b, cls=None),                    # v2 without a class
                     box_head(a, b, v=1),                         # a class in a v1 box
                     box_head(a, b, cls="other"),                 # a class not of the four
                     box_head(a, b, cls=1),
                     box_head(a, b, v=3),                         # a version the relay does not know
                     box_head(a, b, link={"id": "0" * 32, "sig": raw_sig()})):   # an intro's field in a box
            self.refused("bad_envelope", a.client.send, b.fp, head_envelope(a, head))
        got = b.client.inbox(0)
        self.assertEqual(len(got), 5)
        b.client.ack([(x["from"], x["id"]) for x in got])

    def test_v2_introductions_with_a_link_its_answer_or_a_refusal(self):
        a, b = self.joined("ia"), self.joined("ib")
        link = {"id": os.urandom(16).hex(), "sig": raw_sig()}
        ack = {"id": link["id"], "fpB": b.fp, "intro_id": os.urandom(16).hex()}
        refused = {"id": link["id"], "fpB": b.fp, "code": "expired"}
        b.client.send(a.fp, head_envelope(b, intro_head(b, link=link)))                 # B to A: «I came by the link»
        a.client.send(b.fp, head_envelope(a, intro_head(a, link_ack=ack)))              # A's answer
        c = self.joined("ic")
        a.client.send(c.fp, head_envelope(a, intro_head(a, link_refused=refused)))
        a.client.send(c.fp, head_envelope(a, intro_head(a)))                            # a plain v2 card
        d = self.joined("id")
        d.client.send(c.fp, intro(d))                                                   # v1, as before
        for head in (intro_head(b, v=1, link=link),                                    # a link in a v1 intro
                     intro_head(b, link=link, link_ack=ack),                           # two at once
                     intro_head(b, link={**link, "fp": b.fp}),                         # a field more inside
                     intro_head(b, link={"id": link["id"]}),                           # a field less
                     intro_head(b, link={**link, "id": "X" * 32}),
                     intro_head(b, link={**link, "sig": "a b"}),                       # not one printable word
                     intro_head(b, link={**link, "sig": "x" * 1025}),
                     intro_head(b, link_ack={**ack, "fpB": "hb1-x"}),
                     intro_head(b, link_ack={**ack, "intro_id": 5}),
                     intro_head(b, link_refused={**refused, "code": "Expired!"}),
                     intro_head(b, cls="ctl"),                                         # a class is a box's field
                     intro_head(b, other={})):
            self.refused("bad_envelope", b.client.send, c.fp, head_envelope(b, head))

    # (b) withdrawing an unused invitation
    def test_a_hub_withdraws_only_its_own_unused_invitation(self):
        a = self.joined("wa")
        b = self.joined("wb", by=a)
        used = [i["id"] for i in a.client.invites() if i["used_by"] == b.fp][0]
        text = a.invite()
        iid = busrelay.read_invite(text)[0]["id"]
        self.refused("no_invite", revoke, b, iid)                 # another hub's: as if there were none
        self.refused("used", revoke, a, used)                     # a used one is a membership: the operator's
        self.refused("no_invite", revoke, a, os.urandom(16).hex())
        self.refused("bad_request", revoke, a, "x")
        self.assertEqual(revoke(a, iid), {"revoked": True})
        self.refused("no_invite", revoke, a, iid)
        self.refused("bad_invite", Ident(self.relay, "wc").join, text, "C")   # a withdrawn invitation lets no one in
        self.assertNotIn(iid, [i["id"] for i in a.client.invites()])
        code, out, _ = self.relay.cmd("invites")
        self.assertRegex(out, iid[:8] + r".* withdrawn ")
        self.refused("conflict", a.client.invite, text)           # the same signed text again: it does not come back
        self.refused("bad_invite", Ident(self.relay, "wd").join, text, "D")

    def test_five_times_make_withdraw_make_never_blocks_the_sixth(self):
        a = self.joined("qa")
        live = [busrelay.read_invite(a.invite())[0]["id"] for _ in range(5)]
        self.refused("quota", a.invite)                           # five unused at once at most
        for _ in range(5):
            revoke(a, live.pop(0))
            live.append(busrelay.read_invite(a.invite())[0]["id"])
            self.refused("quota", a.invite)
        self.assertEqual(sorted(i["id"] for i in a.client.invites()), sorted(live))

    # (d) the bus version: a header on every signed request, asked by registered hubs only
    def test_the_bus_version_follows_the_requests_and_registered_hubs_ask_it(self):
        a, b = self.joined("ba"), self.joined("bb")
        self.assertEqual(signed(a, "POST", "/v1/allow", {"from": []}, version=2)[0], 200)
        self.assertEqual(peer(b, a.fp), {"fp": a.fp, "bus_v": 2})
        self.assertEqual(signed(a, "GET", "/v1/invites")[0], 200)              # no header: the hub went back to v1
        self.assertEqual(peer(b, a.fp)["bus_v"], 1)
        self.assertEqual(signed(a, "POST", "/v1/allow", {"from": []}, version=2)[0], 200)
        self.assertEqual(peer(b, a.fp)["bus_v"], 2)
        self.assertEqual(signed(a, "POST", "/v1/allow", {"from": []}, version="two")[1]["code"], "bad_request")
        self.assertEqual(signed(a, "POST", "/v1/allow", {"from": []}, version=0)[1]["code"], "bad_request")
        self.assertEqual(peer(b, a.fp)["bus_v"], 2)                            # a refused request changes nothing
        self.assertEqual(signed(a, "POST", "/v1/allow", {"from": []}, version=7)[0], 200)
        self.assertEqual(peer(b, a.fp)["bus_v"], 2)       # above what this relay takes: the version usable here
        self.assertEqual(peer(a, a.fp)["fp"], a.fp)
        self.refused("unknown_to", peer, b, "hb1-" + "a" * 26)
        self.refused("bad_request", peer, b, "nope")
        stranger = Ident(self.relay, "bs")
        self.refused("unknown_hub", peer, stranger, a.fp)                      # only registered hubs ask
        gone = self.joined("bg")
        gone.client.leave()
        self.refused("unknown_to", peer, b, gone.fp)                           # left — the same as unknown
        removed = self.joined("br")
        self.relay.cmd("remove", removed.fp)
        self.refused("unknown_to", peer, b, removed.fp)
        # the hub's client names its version (2) with every request, the join too; a refused join naming another one
        # changes nothing
        with bus_v("1"):
            self.refused("removed", removed.join, None, "again")
            self.refused("bad_invite", gone.join, None, "again")
        with contextlib.closing(sqlite3.connect(self.relay.db)) as db:         # a refused join writes no record
            got = dict(db.execute("SELECT fp, bus_v FROM hubs WHERE fp IN (?, ?)", (removed.fp, gone.fp)).fetchall())
        self.assertEqual(got, {removed.fp: 2, gone.fp: 2})

    def test_the_version_at_join_and_on_a_new_transport_key(self):
        a = Ident(self.relay, "ja")
        body_inv = self.relay.invite()
        # join through the hub's client with the header added: the relay records 2 from the first request
        with bus_v("2"):
            a.join(body_inv, "A")
        b = self.joined("jb")
        self.assertEqual(peer(b, a.fp)["bus_v"], 2)
        with bus_v(None):
            a.join(None, "A")                                                  # rebinding without the header
        self.assertEqual(peer(b, a.fp)["bus_v"], 1)

    def test_strangers_and_unknown_fingerprints_cost_the_relay_nothing(self):
        relay = RelayProcess(second=5)                            # the limit counts a calendar minute: the clock stands
        try:
            a = Ident(relay, "la")
            a.join(relay.invite(), "A")
            before = os.path.getsize(relay.db)
            codes = []
            for _ in range(200):                                  # a registered hub asks about random fingerprints
                fp = bus.fingerprint(os.urandom(32), os.urandom(32))
                try:
                    peer(a, fp)
                    codes.append("found")
                except busrelay.Error as e:
                    codes.append(e.code)
            self.assertEqual(set(codes), {"unknown_to"})          # a registered hub's requests have no rate limit
            codes = []
            for _ in range(40):                                   # unregistered fingerprints: each costs the address
                s = Ident(relay, "ls")
                try:
                    peer(s, a.fp)
                except busrelay.Error as e:
                    codes.append(e.code)
            self.assertEqual(set(codes[:30]), {"unknown_hub"})
            self.assertEqual(set(codes[30:]), {"rate"})
            with contextlib.closing(sqlite3.connect(relay.db)) as db:   # closed: Windows removes no open database
                self.assertEqual(db.execute("SELECT COUNT(*) FROM hubs").fetchone()[0], 1)
            self.assertLess(os.path.getsize(relay.db) - before, 64 * 1024)
        finally:
            relay.stop()

    # (c) the quota reserves the live invitations: 19 used + 5 open + 5 joins at once
    def test_used_and_live_invitations_together_stay_within_twenty(self):
        relay = RelayProcess()
        try:
            a = Ident(relay, "ua")
            a.join(relay.invite(), "A")
            for i in range(15):
                Ident(relay, f"u{i}").join(a.invite(), f"U{i}")
            texts = [a.invite() for _ in range(5)]                # 15 used + 5 live = 20
            self.refused("quota", a.invite)
            got = {}
            hubs = [Ident(relay, f"c{i}") for i in range(5)]

            def join(h, text):
                try:
                    got[h.fp] = h.join(text, "C")["fp"]
                except busrelay.Error as e:
                    got[h.fp] = e.code
            threads = [threading.Thread(target=join, args=(h, t)) for h, t in zip(hubs, texts)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(60)
            self.assertEqual(sorted(got), sorted(got.values()))   # all five joined
            self.refused("quota", a.invite)                       # 20 used in 30 days
            mine = a.client.invites()
            self.assertEqual(sum(1 for i in mine if i["used"]), 20)
            # 19 used + 1 live: the next four are refused, and five joins at once take only the one
            relay2 = RelayProcess()
            try:
                b = Ident(relay2, "ub")
                b.join(relay2.invite(), "B")
                for i in range(19):
                    Ident(relay2, f"v{i}").join(b.invite(), f"V{i}")
                one = b.invite()
                for _ in range(4):
                    self.refused("quota", b.invite)
                got.clear()
                hubs = [Ident(relay2, f"d{i}") for i in range(5)]
                threads = [threading.Thread(target=join, args=(h, one)) for h in hubs]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(60)
                self.assertEqual(sorted(v == k for k, v in got.items()), [False] * 4 + [True], got)
                self.assertEqual(sum(1 for i in b.client.invites() if i["used"]), 20)
                self.refused("quota", b.invite)
            finally:
                relay2.stop()
        finally:
            relay.stop()

    # (e) the operator's first invitation
    def test_bootstrap_prints_the_first_hubs_invitation_once(self):
        relay = RelayProcess()
        try:
            code, out, err = relay.cmd("bootstrap")
            self.assertEqual(code, 0, err)
            self.assertEqual(len(out.splitlines()), 1)                         # stdout: the line only
            self.assertTrue(out.startswith(busrelay.INVITE_PREFIX))            # the prefix the page's field knows
            fields = busrelay.read_invite(out)[0]
            self.assertEqual((fields["relay"], fields["key"], fields["by"]), (relay.url, relay.key, "operator"))
            self.assertIn("first hub", err)
            a = Ident(relay, "fa")
            self.assertEqual(a.join(out, "A")["invited_by"], "operator")
            code, out2, err = relay.cmd("bootstrap")                           # a hub is here: links come from it
            self.assertEqual((code, out2), (1, ""))
            self.assertIn("server.py invite", err)
        finally:
            relay.stop()

    def test_bootstrap_twice_gives_no_second_first_hub(self):
        relay = RelayProcess()
        try:
            code, first, _ = relay.cmd("bootstrap")
            self.assertEqual(code, 0)
            code, again, err = relay.cmd("bootstrap")                          # the first one is live and unused:
            self.assertEqual((code, again), (0, first), err)                  # the same line, not a second one
            with contextlib.closing(sqlite3.connect(relay.db)) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM invites").fetchone()[0], 1)
        finally:
            relay.stop()

    def test_bootstrap_needs_an_address_a_hub_will_take(self):
        relay = RelayProcess()
        try:
            env = {k: v for k, v in relay.env().items() if k != "HUB_RELAY_URL"}
            import subprocess
            import sys
            from tests.test_hubbus_s2 import SERVER
            r = subprocess.run([sys.executable, SERVER, "bootstrap"], env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual((r.returncode, r.stdout), (2, ""))
            for url in ("http://203.0.113.5:8080", "ftp://relay.example.test", "https://relay.example.test/x?y",
                        "https://relay.example.test:99999", "https://relay.example.test:abc"):
                code, out, err = relay.cmd("bootstrap", "--url", url)
                self.assertEqual((code, out), (2, ""), url)
            self.assertEqual(relay.cmd("invite", "--url", "http://203.0.113.5:8080")[:2], (2, ""))
            code, out, _ = relay.cmd("bootstrap", "--url", "https://relay.example.test/")
            self.assertEqual(code, 0)
            self.assertEqual(busrelay.read_invite(out)[0]["relay"], "https://relay.example.test")
        finally:
            relay.stop()

    def test_the_relays_address_rule_is_the_hub_cores(self):
        server = server_module()
        for url in ("https://relay.example.test", "https://relay.example.test:8443/r/", "http://127.0.0.1:8080",
                    "http://localhost:1", "http://[::1]:8080", "http://203.0.113.5", "https://user@relay.example.test",
                    "https://relay.example.test/x#y", "https://relay.example.test/?q", "ftp://relay.example.test",
                    "https:///x", "https://relay.example.test/a b", "https://" + "r" * 200 + ".test",
                    "https://relay.example.test:99999", "https://relay.example.test:abc"):
            try:
                hub = busrelay.check_url(url)
            except busrelay.Error:
                hub = None
            self.assertEqual(server.check_url(url), hub, url)

    def test_hello_names_the_bus_version_the_relay_takes(self):
        with urllib.request.urlopen(self.relay.url + "/v1/hello", timeout=10) as r:
            self.assertEqual(json.loads(r.read()), {"v": 1, "key": self.relay.key, "bus_v": 2})


@need_ssh
class Quota(unittest.TestCase):
    """The quota's time rules on the Relay class with a clock of the test's own (the requests' signatures are not the
    point here: the operations are called as the HTTP layer calls them after it checked one)."""

    def setUp(self):
        self.server = server_module()
        self.now = 1_900_000_000
        self.folder = tempfile.mkdtemp(prefix="relay-q-")
        self.relay = self.server.Relay(os.path.join(self.folder, "relay.sqlite3"), clock=lambda: self.now)
        self.addCleanup(self.relay.store.db.close)   # an open database cannot be removed on Windows (the run's temp folder)
        self.url = "https://relay.example.test"

    def req(self, h):
        return self.server.Req(h.fp, os.urandom(16).hex(), self.now, 1)

    def join(self, h, invite):
        body = json.dumps({"invite": invite, "sign_pub": bus._b64(h.keys.sign_pub), "enc_pub": bus._b64(h.keys.enc_pub),
                           "transport_pub": base64.b64encode(os.urandom(32)).decode(), "bind": "", "label": "H"}).encode()
        return self.relay.join(self.req(h), body)

    def ident(self, name):
        h = Ident.__new__(Ident)
        h.folder = tempfile.mkdtemp(prefix=f"id-{name}-")
        import subprocess
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "hub-test", "-f",
                        os.path.join(h.folder, "key")], check=True, stdin=subprocess.DEVNULL)   # -C: as Ident
        with open(os.path.join(h.folder, "key.pub"), encoding="ascii") as f:
            sp = bus._ed25519_raw(f.read())
        ep = os.urandom(32)
        h.keys = bus.Keys(sign_key=os.path.join(h.folder, "key"), sign_pub=sp, enc_pub=ep, fp=bus.fingerprint(sp, ep))
        h.fp = h.keys.fp
        return h

    def invite(self, h):
        text, _, _ = busrelay.make_invite(h.keys, {"url": self.url, "key": self.relay.key}, self.now)
        status, got = self.relay.invite(self.req(h), json.dumps({"invite": text}).encode())
        return status, got, text

    def test_a_database_of_the_code_before_knows_every_hub_as_version_one(self):
        folder = tempfile.mkdtemp(prefix="relay-old-")
        path = os.path.join(folder, "relay.sqlite3")
        with contextlib.closing(sqlite3.connect(path)) as db, db:  # the S2 schema, one hub in it; closed, as above
            db.executescript(self.server.SCHEMA.replace(", bus_v INTEGER NOT NULL DEFAULT 1", ""))
            db.execute("INSERT INTO hubs (fp, sign_pub, transport_pub, invited_by, invite, joined) "
                       "VALUES ('hb1-" + "a" * 26 + "', 'x', 'y', 'operator', 'i', 1)")
        relay = self.server.Relay(path, clock=lambda: self.now)
        self.addCleanup(relay.store.db.close)   # as in setUp
        status, got = relay.peer(self.server.Req("hb1-" + "b" * 26, os.urandom(16).hex(), self.now, 2),
                                 "fp=hb1-" + "a" * 26)
        self.assertEqual((status, got), (200, {"fp": "hb1-" + "a" * 26, "bus_v": 1}))

    def test_forgotten_invitations_that_expired_do_not_spend_the_month(self):
        a = self.ident("a")
        self.assertEqual(self.join(a, self.relay.operator_invite(self.url))[0], 200)
        for _ in range(4):                                       # 20 made in a month, none used, each expired
            for _ in range(5):
                self.assertEqual(self.invite(a)[0], 200)
            self.now += 7 * 86400 + 1
        self.assertEqual(self.invite(a)[0], 200)                 # nothing used, nothing live: the quota is free
        count = "SELECT COUNT(*) FROM invites WHERE by = ?"
        self.relay.store.swept = 0
        self.relay.store.sweep(self.now)                          # expired within a day: kept (a late join hears why)
        self.assertEqual(self.relay.store.one(count, a.fp)[0], 6)
        self.now += 86400
        self.relay.store.swept = 0
        self.relay.store.sweep(self.now)                          # then dropped: they do not pile up
        self.assertEqual(self.relay.store.one(count, a.fp)[0], 1)

    def test_an_invitation_lives_ten_minutes_at_least(self):
        a = self.ident("a")
        self.join(a, self.relay.operator_invite(self.url))
        text, _, _ = busrelay.make_invite(a.keys, {"url": self.url, "key": self.relay.key},
                                          self.now - busrelay.INVITE_TTL + 60)   # a term of one minute
        with self.assertRaises(self.server.Refused) as e:
            self.relay.invite(self.req(a), json.dumps({"invite": text}).encode())
        self.assertEqual((e.exception.status, e.exception.code), (400, "bad_invite"))

    def test_an_invitation_counts_by_when_it_was_used_not_when_it_was_made(self):
        a = self.ident("a")
        self.join(a, self.relay.operator_invite(self.url))
        n = 0
        for _ in range(4):                                       # five made, used six days later: four times
            texts = [self.invite(a)[2] for _ in range(5)]
            self.now += 6 * 86400
            for t in texts:
                n += 1
                self.assertEqual(self.join(self.ident(f"h{n}"), t)[0], 200)
        self.now += 8 * 86400                                     # the first five were made 32 days ago, used 26 ago
        self.assertEqual(self.invite(a)[0], 429)                  # 20 used in 30 days
        self.now += 4 * 86400 + 1                                 # the first five's use is 30 days old
        self.assertEqual(self.invite(a)[0], 200)


if __name__ == "__main__":
    unittest.main()
