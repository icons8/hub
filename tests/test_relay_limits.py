"""The limits of a relay open to everyone (spec public relay, slice R2): registration by the open line is capped a day
for the relay and per address (an IPv6 site is one address), a registered hub's signed requests have a rate (counted
after the signature: another's requests do not spend a hub's), a sender's introductions take a small share of a hub's
room and a day's number, and the database has a ceiling — over it new letters, introductions, registrations and
growing allow lists are refused with `full`, reading and acknowledging go on. Used invitations go after their month;
without the cryptography library the relay says so (its health check fails); the image runs as a numeric user.

On the Relay class and the module's functions; the address and the signature's order through a real process
(tests/test_hubbus_s2.RelayProcess). Without ssh-keygen everything that signs is skipped."""
import tests  # noqa: F401 — keeps the machine's settings out
import base64
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from unittest import mock

from tests.test_hubbus import need_ssh
from tests.test_hubbus_s2 import SERVER, Ident, RelayProcess, free_port, server_module
from tests.test_relay_public import fp_of, letter
from core import busflow, busrelay, i18n

URL = "https://relay.example.test"


def stranger(server):
    """A hub's two public keys and its fingerprint (the relay checks an introduction's card against its sender)."""
    sp, ep = os.urandom(32), os.urandom(32)
    return server.fingerprint(sp, ep), sp, ep


def card(server, relay, who, to, now, mid=None):
    """`who`'s introduction to `to` (the relay reads the form and the card's keys, not the signature) → the status."""
    fp, sp, ep = who
    head = json.dumps({"v": 1, "t": "intro", "id": mid or os.urandom(16).hex(), "ts": now, "exp": now + 86400,
                       "card": {"label": "X", "sign_pub": base64.b64encode(sp).decode(),
                                "enc_pub": base64.b64encode(ep).decode(), "relays": [], "agents": {}, "bus_v": 1}},
                      separators=(",", ":"))
    env = json.dumps({"head": head, "sig": "-----BEGIN SSH SIGNATURE-----\nx\n-----END SSH SIGNATURE-----\n"},
                     separators=(",", ":")).encode()
    body = json.dumps({"to": to, "envelope": base64.b64encode(env).decode()}).encode()
    try:
        return relay.send(server.Req(fp, os.urandom(16).hex(), now, 1), body)[0]
    except server.Refused as e:
        return e.status


def big_letter(src, dst, size=81920):
    """A box of the largest bucket from src to dst (bytes of the right outer form)."""
    ts = int(time.time())
    head = json.dumps({"v": 1, "t": "box", "id": os.urandom(16).hex(), "ts": ts, "exp": ts + 86400, "from": src,
                       "to": dst, "ct": base64.b64encode(os.urandom(32 + size + 16)).decode()}, separators=(",", ":"))
    env = json.dumps({"head": head, "sig": "-----BEGIN SSH SIGNATURE-----\nx\n-----END SSH SIGNATURE-----\n"},
                     separators=(",", ":")).encode()
    return json.dumps({"to": dst, "envelope": base64.b64encode(env).decode()}).encode()


class _Relays:
    """A Relay of the module on a database in a temporary folder, with the settings given and a clock to move."""

    def setUp(self):
        self.server = server_module()
        self.folder = tempfile.mkdtemp(prefix="relay-l-")
        self.clock = [float(int(time.time()))]

    def relay(self, **env):
        with mock.patch.dict(os.environ, env):
            r = self.server.Relay(os.path.join(self.folder, "relay.sqlite3"), clock=lambda: self.clock[0])
        self.addCleanup(r.store.db.close)   # an open database cannot be removed on Windows
        return r

    def req(self, fp):
        return self.server.Req(fp, os.urandom(16).hex(), int(self.clock[0]), 1)

    def hubs(self, relay, *fps):
        with relay.store.tx() as db:
            for fp in fps:
                db.execute("INSERT INTO hubs (fp, sign_pub, transport_pub, invited_by, invite, joined) "
                           "VALUES (?, 'x', 'y', 'operator', 'i', ?)", (fp, int(self.clock[0])))

    def join(self, relay, fp, line, addr):
        """→ the status of `fp`'s registration by `line` from `addr`, or the code of a refusal."""
        body = json.dumps({"invite": line, "sign_pub": base64.b64encode(os.urandom(32)).decode(),
                           "transport_pub": base64.b64encode(os.urandom(32)).decode(), "label": ""}).encode()
        try:
            status, got = relay.join(self.req(fp), body, addr)
        except self.server.Refused as e:
            return e.code
        return got.get("code") or ("already" if got.get("already") else status)


@need_ssh
class OpenLineCaps(_Relays, unittest.TestCase):
    """The open line lets anyone in — so many a day for the whole relay and from one address."""

    def test_joins_by_the_open_line_are_capped_a_day_per_address_and_for_the_relay(self):
        relay = self.relay(HUB_RELAY_OPEN_DAY="3", HUB_RELAY_OPEN_ADDR_DAY="2")
        line = relay.operator_record(URL, kind=self.server.OPEN, again=True)[0]
        self.assertEqual([self.join(relay, fp_of(i), line, "203.0.113.5") for i in (1, 2, 3)],
                         [200, 200, "joins_addr"])                          # two from one address
        self.assertEqual(self.join(relay, fp_of(1), line, "203.0.113.5"), "already")   # a hub here binds again
        self.assertEqual(self.join(relay, fp_of(4), line, "203.0.113.6"), 200)
        self.assertEqual(self.join(relay, fp_of(5), line, "203.0.113.7"), "joins_open")   # three for the relay
        one = relay.operator_invite(URL)                                   # an operator's one-time invitation
        self.assertEqual(self.join(relay, fp_of(6), one, "203.0.113.5"), 200)   # is not the open line's
        relay.close_open()                                                 # closed and opened again: the same day
        again = relay.operator_record(URL, kind=self.server.OPEN, again=True)[0]
        self.assertEqual(self.join(relay, fp_of(7), again, "203.0.113.8"), "joins_open")
        self.clock[0] += 86400 + 1                                         # 24 hours on
        self.assertEqual(self.join(relay, fp_of(8), again, "203.0.113.5"), 200)

    def test_the_caps_are_on_by_default_and_zero_lifts_them(self):
        relay = self.relay()
        self.assertEqual((relay.open_day, relay.open_addr_day), (self.server.OPEN_DAY, self.server.OPEN_ADDR_DAY))
        self.assertTrue(self.server.OPEN_DAY > 0 and self.server.OPEN_ADDR_DAY > 0)
        lifted = self.relay(HUB_RELAY_OPEN_DAY="0", HUB_RELAY_OPEN_ADDR_DAY="0")
        line = lifted.operator_record(URL, kind=self.server.OPEN, again=True)[0]
        self.assertEqual({self.join(lifted, fp_of(i), line, "203.0.113.5") for i in range(12)}, {200})

    def test_an_ipv6_site_is_one_address(self):
        key = self.server.client_key
        self.assertEqual(key("2001:db8:1:2::5"), key("2001:db8:1:ffff:1::9"))   # a site's /48: its many /64s
        self.assertNotEqual(key("2001:db8:1::5"), key("2001:db8:2::5"))
        self.assertEqual(key("::ffff:203.0.113.5"), "203.0.113.5")
        self.assertEqual(key("64:ff9b::cb00:7105"), "203.0.113.5")         # NAT64: the IPv4 client behind it
        self.assertNotEqual(key("2001:db8:1:2::5", 64), key("2001:db8:1:3::5", 64))
        self.assertEqual(key("203.0.113.5"), "203.0.113.5")
        self.assertEqual(key("not an address"), "not an address")

    def test_the_proxys_address_is_read_the_same_way(self):
        def addr(xff):
            fake = types.SimpleNamespace(headers={"X-Forwarded-For": xff}, client_address=("127.0.0.1", 0))
            with mock.patch.dict(os.environ, {"HUB_RELAY_TRUST_PROXY": "1"}):
                return self.server.Handler.addr(fake)
        self.assertEqual(addr("198.51.100.1, 203.0.113.9"), "203.0.113.9")   # the last: the one the proxy wrote

    def test_failures_lock_out_a_host_not_its_neighbours(self):
        relay = self.relay()
        for _ in range(self.server.LOOSE_PER_MINUTE):
            relay.loose_spend("2001:db8:1:2::5")
        with self.assertRaises(self.server.Refused):
            relay.loose_check("2001:db8:1:2::9")                           # its /64
        relay.loose_check("2001:db8:1:3::5")                               # another /64 of the same site goes on

    def test_the_server_counts_by_the_callers_address(self):
        with mock.patch.dict(os.environ, {"HUB_RELAY_OPEN_ADDR_DAY": "1"}):
            relay = RelayProcess()
        self.addCleanup(relay.stop)
        line = relay.cmd("open")[1]
        Ident(relay, "ca").join(line, "A")
        with self.assertRaises(busrelay.Error) as e:
            Ident(relay, "cb").join(line, "B")
        self.assertEqual(e.exception.code, "joins_addr")


@need_ssh
class HubRate(_Relays, unittest.TestCase):
    """A registered hub's signed requests: so many at once, then so many a minute."""

    def test_a_hubs_requests_are_limited_and_the_limit_refills(self):
        relay = self.relay(HUB_RELAY_HUB_MINUTE="60", HUB_RELAY_HUB_BURST="3")
        ticks = [100.0]
        relay.ticks = lambda: ticks[0]
        a, b = fp_of(1), fp_of(2)
        for _ in range(3):
            relay.hub_spend(a)
        with self.assertRaises(self.server.Refused) as e:
            relay.hub_spend(a)
        self.assertEqual((e.exception.status, e.exception.code), (429, "rate"))
        relay.hub_spend(b)                                                 # another hub has its own
        with self.assertRaises(self.server.Refused):
            relay.hub_spend(a, spend=False)                                # the look before the signature
        ticks[0] += 1.0                                                    # one a second
        relay.hub_spend(a, spend=False)                                    # a look spends nothing
        relay.hub_spend(a)
        with self.assertRaises(self.server.Refused):
            relay.hub_spend(a)
        ticks[0] += 3600                                                   # an hour: no more than the burst
        for _ in range(3):
            relay.hub_spend(a)
        with self.assertRaises(self.server.Refused):
            relay.hub_spend(a)

    def test_the_defaults_and_bad_settings(self):
        relay = self.relay()
        self.assertEqual((relay.hub_minute, relay.hub_burst), (self.server.HUB_MINUTE, self.server.HUB_BURST))
        for bad in ("0", "-1", "many"):                                    # zero would refuse every hub
            r = self.relay(HUB_RELAY_HUB_MINUTE=bad, HUB_RELAY_HUB_BURST=bad)
            self.assertEqual((r.hub_minute, r.hub_burst), (self.server.HUB_MINUTE, self.server.HUB_BURST), bad)

    def test_only_a_hubs_own_signed_requests_spend_its_limit(self):
        with mock.patch.dict(os.environ, {"HUB_RELAY_HUB_BURST": "4", "HUB_RELAY_HUB_MINUTE": "1"}):
            relay = RelayProcess()
        self.addCleanup(relay.stop)
        line = relay.cmd("open")[1]
        a, b = Ident(relay, "ra"), Ident(relay, "rb")
        a.join(line, "A")                                                  # one
        b.join(line, "B")
        forged = busrelay.Client(relay.url, relay.key, a.fp, b.tkey)       # a's name, another's key
        for _ in range(5):
            with self.assertRaises(busrelay.Error) as e:
                forged.allow([])
            self.assertEqual(e.exception.code, "bad_signature")
        for _ in range(3):                                                 # two, three, four: still a's own
            self.assertEqual(a.client.allow([]), {"allowed": 0})
        with self.assertRaises(busrelay.Error) as e:
            a.client.allow([])
        self.assertEqual(e.exception.code, "rate")
        self.assertEqual(b.client.allow([]), {"allowed": 0})              # b goes on

    def test_a_repeated_request_spends_nothing_of_the_hubs_limit(self):
        with mock.patch.dict(os.environ, {"HUB_RELAY_HUB_BURST": "3", "HUB_RELAY_HUB_MINUTE": "1"}):
            relay = RelayProcess()
        self.addCleanup(relay.stop)
        a = Ident(relay, "pa")
        a.join(relay.cmd("open")[1], "A")                                  # one
        server = server_module()
        body = json.dumps({"from": []}).encode()
        ts, nonce = str(int(time.time())), os.urandom(16).hex()
        sig = server.sign(a.tkey, server.request_line(relay.key, "POST", "/v1/allow", ts, nonce, body))
        headers = {"X-Hub-Fp": a.fp, "X-Hub-Time": ts, "X-Hub-Nonce": nonce, "Content-Type": "application/json",
                   "X-Hub-Sig": base64.b64encode(sig.encode()).decode()}
        codes = []
        for _ in range(6):                                                 # the same request, seen and sent again
            try:
                with urllib.request.urlopen(urllib.request.Request(relay.url + "/v1/allow", data=body, method="POST",
                                                                   headers=headers), timeout=30) as r:
                    codes.append(r.status)
            except urllib.error.HTTPError as e:
                e.close()
                codes.append(e.code)
        self.assertEqual(codes, [200] + [409] * 5)                         # two
        self.assertEqual(a.client.allow([]), {"allowed": 0})              # three: a's own still go


@need_ssh
class IntroductionLimits(_Relays, unittest.TestCase):
    """Introductions come from anyone registered: one sender takes a small share of a hub's room, and introduces itself
    to so many hubs a day."""

    def test_ten_hubs_do_not_fill_a_hubs_room(self):
        relay = self.relay()
        now = int(self.clock[0])
        hub = fp_of(1)
        many = [stranger(self.server) for _ in range(11)]
        self.hubs(relay, hub, *(s[0] for s in many))
        for s in many[:10]:
            got = [card(self.server, relay, s, hub, now) for _ in range(self.server.INTROS_PAIR + 1)]
            self.assertEqual(got[-1], 503)                                 # each at its own share
        self.assertEqual(card(self.server, relay, many[10], hub, now), 200)   # the eleventh still comes in

    def test_a_sender_introduces_itself_to_so_many_hubs_a_day(self):
        relay = self.relay()
        now = int(self.clock[0])
        who = stranger(self.server)
        to = [fp_of(i) for i in range(1, 5)]
        self.hubs(relay, who[0], *to)
        with relay.store.tx() as db:
            db.execute("INSERT INTO allow (hub, sender) VALUES (?, ?)", (to[3], who[0]))
        mid = os.urandom(16).hex()
        with mock.patch.object(self.server, "INTROS_DAY", 3):
            self.assertEqual([card(self.server, relay, who, to[0], now, mid)]
                             + [card(self.server, relay, who, t, now) for t in to[1:]], [200, 200, 200, 403])
            self.assertEqual(card(self.server, relay, who, to[0], now, mid), 200)   # the same one again: no new one
            self.assertEqual(relay.send(self.req(who[0]), letter(who[0], to[3]))[0], 200)   # boxes are not counted
            self.clock[0] += 86400
            self.assertEqual(card(self.server, relay, who, to[3], int(self.clock[0])), 200)   # the next day
            reopened = self.server.Relay(relay.store.path, clock=lambda: self.clock[0])   # a restart forgets nothing
            self.addCleanup(reopened.store.db.close)
            for t in to[:2]:
                self.assertEqual(card(self.server, reopened, who, t, int(self.clock[0])), 200)
            self.assertEqual(card(self.server, reopened, who, to[2], int(self.clock[0])), 403)


@need_ssh
class Ceiling(_Relays, unittest.TestCase):
    """The space the database takes (its pages less the free ones) has a ceiling: over it the new is refused with
    `full`, what is there is read and acknowledged — and an acknowledgement makes room again."""

    def used(self, relay):
        db = relay.store.db
        return (db.execute("PRAGMA page_count").fetchone()[0] - db.execute("PRAGMA freelist_count").fetchone()[0]) \
            * db.execute("PRAGMA page_size").fetchone()[0]

    def test_over_the_ceiling_the_new_is_refused_and_the_old_is_read(self):
        relay = self.relay()
        a, b = fp_of(1), fp_of(2)
        self.hubs(relay, a, b)
        with relay.store.tx() as db:
            db.execute("INSERT INTO allow (hub, sender) VALUES (?, ?)", (b, a))
            db.execute("INSERT INTO allow (hub, sender) VALUES (?, ?)", (a, b))
        relay.db_max = self.used(relay) + 300 * 1024

        def send(src, dst):
            try:
                status, got = relay.send(self.req(src), big_letter(src, dst))
            except self.server.Refused as e:
                return e.status, e.code
            return status, got.get("code")
        got = [send(a, b) for _ in range(8)]
        self.assertIn((507, "full"), got)                                  # full before eight of 80 KB
        self.assertEqual(got[got.index((507, "full")):], [(507, "full")] * (8 - got.index((507, "full"))))
        relay.db_max = self.used(relay)                                    # right at the ceiling: nothing more
        self.assertEqual(card(self.server, relay, stranger(self.server), b, int(self.clock[0])), 507)
        line = relay.operator_record(URL, kind=self.server.OPEN, again=True)[0]
        self.assertEqual(self.join(relay, fp_of(9), line, "203.0.113.5"), "full")   # no new hub
        self.assertEqual(self.join(relay, a, line, "203.0.113.5"), "already")         # a hub here binds again
        with self.assertRaises(self.server.Refused) as e:                 # an allow list does not grow
            relay.allow(self.req(b), json.dumps({"from": [a, fp_of(3)]}).encode())
        self.assertEqual(e.exception.code, "full")
        self.assertEqual(relay.allow(self.req(b), json.dumps({"from": [a]}).encode())[0], 200)   # nor is it stuck
        status, box = relay.inbox(self.req(b), 0)                         # what waits is read
        self.assertEqual(status, 200)
        self.assertTrue(box["items"])
        items = [[x["from"], x["id"]] for x in box["items"]]
        self.assertEqual(relay.ack(self.req(b), json.dumps({"items": items}).encode()),
                         (200, {"removed": len(items)}))
        self.assertEqual(send(a, b), (200, None))                         # its pages are free again

    def test_the_ceiling_is_a_setting(self):
        self.assertEqual(self.relay().db_max, self.server.DB_MAX_MB * 2 ** 20)
        self.assertEqual(self.relay(HUB_RELAY_DB_MAX_MB="100").db_max, 100 * 2 ** 20)
        self.assertEqual(self.relay(HUB_RELAY_DB_MAX_MB="lots").db_max, self.server.DB_MAX_MB * 2 ** 20)
        self.assertLessEqual(self.server.DB_MAX_MB * 2 ** 20, 8 * 2 ** 30)   # under a volume of 10 GiB, with its WAL


@need_ssh
class Keeping(_Relays, unittest.TestCase):
    """What only grew: invitations used long ago go after their month (the quota counts 30 days); the per-address
    table of failed requests drops old minutes, never the current ones."""

    def test_used_invitations_go_after_their_month(self):
        relay = self.relay()
        now = int(self.clock[0])
        rows = {"old": (now - 40 * 86400, now - 31 * 86400, fp_of(1), now - 31 * 86400, 0),
                "recent": (now - 2 * 86400, now + 86400, fp_of(2), now - 86400, 0),
                "line": (now - 40 * 86400, now + 300 * 86400, None, None, self.server.OPEN)}
        with relay.store.tx() as db:
            for name, (made, exp, used_by, used, multi) in rows.items():
                db.execute("INSERT INTO invites (id, by, made, exp, signer, used_by, used, multi) "
                           "VALUES (?, 'operator', ?, ?, 's', ?, ?, ?)", (name, made, exp, used_by, used, multi))
            for name, exp in (("closed_today", now - 10), ("closed_long_ago", now - 86400 - 10)):
                db.execute("INSERT INTO withdrawn (id, by, at, exp) VALUES (?, 'operator', ?, ?)", (name, now, exp))
        relay.store.sweep(now)
        self.assertEqual({r[0] for r in relay.store.all("SELECT id FROM invites")}, {"recent", "line"})
        # a withdrawn mark stays a day past its term: a line closed today still counts the hubs it let in today
        self.assertEqual({r[0] for r in relay.store.all("SELECT id FROM withdrawn")}, {"closed_today"})

    def test_a_full_table_of_failures_keeps_the_ones_over_their_limit(self):
        relay = self.relay()
        bad = "203.0.113.5"
        for _ in range(self.server.LOOSE_PER_MINUTE):
            relay.loose_spend(bad)
        minute = int(self.clock[0]) // 60
        relay.loose.update({f"198.51.{i // 256}.{i % 256}": (minute - 1, 1) for i in range(10001)})
        relay.loose_spend("203.0.113.99")                                 # past minutes go
        with self.assertRaises(self.server.Refused):
            relay.loose_check(bad)                                         # still over its minute
        self.assertLess(len(relay.loose), 10)
        relay.loose.most = 100                                             # a flood of this minute's addresses
        relay.loose.update({f"2001:db8:{i}::/64": (minute, 1) for i in range(20001)})
        relay.loose_spend("203.0.113.98")
        with self.assertRaises(self.server.Refused):
            relay.loose_check(bad)                                         # never wiped
        self.assertLess(len(relay.loose), 10)


@need_ssh
class Signer(unittest.TestCase):
    """Without the library the relay signs with ssh-keygen — slower: serve says so, and the health check fails unless
    the operator chose it (HUB_RELAY_SIGNER=ssh-keygen)."""

    NO_LIB = ("import sys; sys.modules['cryptography'] = None; sys.path.insert(0, sys.argv[1]); import server; "
              "sys.exit(server.main(sys.argv[2:]))")

    def run_it(self, *args, **env):
        folder = tempfile.mkdtemp(prefix="relay-n-")
        e = {k: v for k, v in os.environ.items() if k != "HUB_RELAY_SIGNER"}
        e.update(HUB_RELAY_DB=os.path.join(folder, "relay.sqlite3"), **env)
        return subprocess.run([sys.executable, "-c", self.NO_LIB, os.path.dirname(SERVER), *args], env=e,
                              capture_output=True, text=True, timeout=60)

    def test_the_health_check_fails_without_the_library(self):
        r = self.run_it("check")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("cryptography", r.stderr)
        self.assertEqual(self.run_it("check", HUB_RELAY_SIGNER="ssh-keygen").returncode, 0)   # chosen: fine

    def test_serve_says_it_signs_with_ssh_keygen(self):
        folder = tempfile.mkdtemp(prefix="relay-n-")
        env = {**os.environ, "HUB_RELAY_DB": os.path.join(folder, "relay.sqlite3"), "HUB_RELAY_PORT": str(free_port()),
               "HUB_RELAY_HOST": "127.0.0.1"}
        env.pop("HUB_RELAY_SIGNER", None)
        proc = subprocess.Popen([sys.executable, "-c", self.NO_LIB, os.path.dirname(SERVER), "serve"], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        lines = []
        reader = threading.Thread(target=lambda: lines.extend(proc.stdout.readline() for _ in range(2)), daemon=True)
        reader.start()
        reader.join(20)
        proc.terminate()
        proc.wait(10)
        proc.stdout.close()
        text = b"".join(lines).decode()
        self.assertIn("listening", text)
        self.assertIn("ssh-keygen", text.split("listening", 1)[1])


class Pod(unittest.TestCase):
    """What a pod gives the relay: Kubernetes puts HUB_RELAY_PORT=tcp://<ip>:<port> into every pod of a namespace with
    a Service named hub-relay (the docker links' variables) — that is the Service's port, not the relay's; and a pod is
    stopped by SIGTERM — the relay closes its database and goes at once, not after the grace period's kill."""

    def test_a_service_link_in_the_port_is_not_the_relays_port(self):
        server = server_module()
        self.assertEqual(server.listen_port("9090"), 9090)
        self.assertEqual(server.listen_port(" 9090\r"), 9090)                # a line of an .env file
        self.assertEqual(server.listen_port(None), 8080)
        self.assertEqual(server.listen_port("tcp://203.0.113.7:80"), 8080)   # the Service's: the relay keeps its own
        for bad in ("http", "70000", "0", "tcp://x:80 9"):
            with self.assertRaises(SystemExit):
                server.listen_port(bad)

    @need_ssh
    @unittest.skipIf(os.name == "nt", "no SIGTERM on Windows: a process there is ended, not asked")
    def test_sigterm_closes_the_database_and_exits_at_once(self):
        import signal
        relay = RelayProcess()
        self.addCleanup(relay.stop)
        a = Ident(relay, "sa")
        a.join(relay.cmd("open")[1], "A")
        seen = sqlite3.connect(relay.db)
        self.addCleanup(seen.close)
        nonces = seen.execute("SELECT COUNT(*) FROM nonces").fetchone()[0]
        got = []

        def poll():
            try:
                got.append(a.client.inbox(25))
            except busrelay.Error as e:
                got.append(e.code)
        poller = threading.Thread(target=poll, daemon=True)
        poller.start()
        deadline = time.monotonic() + 10                                   # the poll is in: its nonce is written
        while seen.execute("SELECT COUNT(*) FROM nonces").fetchone()[0] == nonces and time.monotonic() < deadline:
            time.sleep(0.02)
        seen.close()                                                       # the relay's connection is the last one
        relay.proc.send_signal(signal.SIGTERM)
        relay.proc.wait(20)                                                # not the poll's 25 s, nor a kill's
        poller.join(20)
        self.assertEqual(relay.proc.returncode, 0)                         # its own exit, not the signal's
        self.assertEqual(got, ["network"])                                 # the poll's connection is cut, not a 500
        self.assertFalse(os.path.exists(relay.db + "-wal"))               # closed: its log folded into the file


class Image(unittest.TestCase):
    def test_the_image_runs_as_a_numeric_user(self):
        """A pod with runAsNonRoot takes only a numeric USER (a name cannot be checked to be non-root)."""
        with open(os.path.join(os.path.dirname(SERVER), "Dockerfile"), encoding="utf-8") as f:
            text = f.read()
        user = re.findall(r"^USER\s+(\S+)\s*$", text, re.M)
        self.assertEqual(len(user), 1)
        self.assertRegex(user[0], r"^[1-9][0-9]*(:[0-9]+)?$")
        self.assertIn(f"--uid {user[0].split(':')[0]}", text)


class OwnersWords(unittest.TestCase):
    def test_a_relay_at_its_limit_is_said_in_words(self):
        for code in ("joins_day", "joins_open", "joins_addr", "full"):
            self.assertEqual(busflow.why(code), i18n.t("bus.flow.code.relay_limit"), code)


if __name__ == "__main__":
    unittest.main()
