"""The hub bus, slice S2 (spec hubbus §5.9, §5.11, §7): the relay — a server anyone can run (relay/server.py), the
hub's relay transport (modules/bus_relay), invitations any registered hub makes, and the core's side: no relay
address of its own (only from an invitation), who may hand and offer envelopes, the transport view.

The server runs as a real process on a free port of this machine; the stand's two cores (tests/test_hubbus_s1.LiveHub)
talk through it the way two hubs on different machines would. Without ssh-keygen the server tests are skipped; without
cryptography ≥ 47 — the stand (tests/test_hubbus.py)."""
import tests  # noqa: F401 — keeps the machine's settings out
import base64
import contextlib
import hashlib
import importlib.util
import json
import os
import re
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from tests.test_hubbus import LIB_OK, need_lib, need_ssh
from core import bus, busd, busrelay, busstate, liaison

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(HERE, "relay", "server.py")
OWNER = {"channel": "cli", "owner": True}


def server_module():
    spec = importlib.util.spec_from_file_location("relay_server", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class RelayProcess:
    """relay/server.py serve on a free port of this machine, its database in a temporary folder."""

    def __init__(self, folder=None, second=None):
        """`second`: the relay's clock stands still at this second of a minute (the requests carry the real time, the
        relay accepts them within five minutes): the minute windows of the rate limits do not turn over in the middle
        of a series, however slow the machine is (a series that takes seconds here takes a minute on a loaded one)."""
        self.second = second
        self.folder = folder or tempfile.mkdtemp(prefix="relay-")
        self.db = os.path.join(self.folder, "relay.sqlite3")
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.proc, self.log = None, b""
        self.start()

    def env(self):
        return {**os.environ, "HUB_RELAY_DB": self.db, "HUB_RELAY_PORT": str(self.port), "HUB_RELAY_HOST": "127.0.0.1",
                "HUB_RELAY_URL": self.url}

    SHIFTED = ("import sys, time; sys.path.insert(0, sys.argv[1]); import server; import os; "
               "still = time.time() + (float(sys.argv[2]) - time.time()) % 60; "
               "server.serve(os.environ['HUB_RELAY_DB'], '127.0.0.1', int(os.environ['HUB_RELAY_PORT']), "
               "clock=lambda: still)")

    def start(self):
        args = [sys.executable, SERVER, "serve"]
        if self.second is not None:
            args = [sys.executable, "-c", self.SHIFTED, os.path.dirname(SERVER), str(self.second)]
        self.proc = subprocess.Popen(args, env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        line = self.proc.stdout.readline().decode()
        assert "listening" in line, line
        self.key = line.rsplit("key ", 1)[1].strip()

    def stop(self):
        """Stop the process; what it printed is kept in `log` (it must hold no text of a message either)."""
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(10)
        if self.proc and not self.proc.stdout.closed:
            self.log += self.proc.stdout.read()
            self.proc.stdout.close()

    def cmd(self, *args):
        r = subprocess.run([sys.executable, SERVER, *args], env=self.env(), capture_output=True, text=True, timeout=60)
        return r.returncode, r.stdout.strip(), r.stderr.strip()

    def invite(self):
        code, out, err = self.cmd("invite")
        assert code == 0, err
        return out

    def dump(self):
        """Every byte the relay keeps: the database and its write-ahead log."""
        out = b""
        for name in sorted(os.listdir(self.folder)):
            if name.startswith("relay.sqlite3"):
                with open(os.path.join(self.folder, name), "rb") as f:
                    out += f.read()
        return out


class Ident:
    """A hub's identity for the server tests: its own ssh key (the exchange key is only bytes here) and a transport
    key — no bus library needed."""

    def __init__(self, relay, name="hub"):
        self.folder = tempfile.mkdtemp(prefix=f"id-{name}-")
        # -C: without it the comment is user@host, and a non-ASCII user name breaks the ascii read below
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "hub-test", "-f",
                        os.path.join(self.folder, "key")], check=True, stdin=subprocess.DEVNULL)
        with open(os.path.join(self.folder, "key.pub"), encoding="ascii") as f:
            sp = bus._ed25519_raw(f.read())
        ep = os.urandom(32)
        self.keys = bus.Keys(sign_key=os.path.join(self.folder, "key"), sign_pub=sp, enc_pub=ep,
                             fp=bus.fingerprint(sp, ep))
        self.fp = self.keys.fp
        self.tkey = busrelay.transport_key(self.folder, make=True)[0]
        self.relay = relay
        self.client = busrelay.Client(relay.url, relay.key, self.fp, self.tkey)

    def join(self, invite, label="Hub"):
        return self.client.join(self.keys, invite, label)

    def invite(self):
        text, _, _ = busrelay.make_invite(self.keys, {"url": self.relay.url, "key": self.relay.key}, time.time())
        self.client.invite(text)
        return text


def box(src, dst, size=1024, mid=None, ts=None, exp=None, t="box"):
    """An envelope of the right outer form (the relay cannot tell a sealed box from random bytes of a bucket's size)."""
    ts = int(time.time()) if ts is None else ts
    head = {"v": 1, "t": "box", "id": mid or os.urandom(16).hex(), "ts": ts, "exp": ts + 7 * 86400 if exp is None else exp,
            "from": src.fp, "to": dst.fp,
            "ct": base64.b64encode(os.urandom(32 + size + 16)).decode()}
    head_text = json.dumps(head, separators=(",", ":"))
    return json.dumps({"head": head_text, "sig": bus.sign(src.keys.sign_key, head_text.encode())},
                      separators=(",", ":")).encode(), head["id"]


def intro(src):
    """src's own card as an envelope, as the core sends it to a hub it accepted."""
    card = {"label": "Hub", "sign_pub": bus._b64(src.keys.sign_pub), "enc_pub": bus._b64(src.keys.enc_pub),
            "relays": [], "agents": {}, "bus_v": 1}
    ts = int(time.time())
    head = json.dumps({"v": 1, "t": "intro", "id": os.urandom(16).hex(), "ts": ts, "exp": ts + 86400, "card": card},
                      separators=(",", ":"))
    return json.dumps({"head": head, "sig": bus.sign(src.keys.sign_key, head.encode())}, separators=(",", ":")).encode()


# --- the protocol: the same on both sides, no relay address in the core -------------------------------------------

class Protocol(unittest.TestCase):
    def test_the_hub_and_the_server_sign_and_read_the_same_lines(self):
        srv = server_module()
        args = ("a2V5", "POST", "/v1/send?x=1", 1793000000, "ab" * 16, b'{"to":"x"}')
        self.assertEqual(busrelay.request_line(*args), srv.request_line(*args))
        self.assertEqual(busrelay.response_line("cd" * 16, 403, b"{}", "ef" * 32),
                         srv.response_line("cd" * 16, 403, b"{}", "ef" * 32))
        req = ("hb1-" + "a" * 26, 1793000000, "ab" * 16, "c2ln", "POST", "/v1/send", b"{}")
        self.assertEqual(busrelay.request_digest(*req), srv.request_digest(*req))
        self.assertEqual(busrelay.bind_line("a2V5", "hb1-" + "a" * 26, "dA=="), srv.bind_line("a2V5", "hb1-" + "a" * 26, "dA=="))
        self.assertEqual(busrelay.invite_line(b"{}"), srv.invite_line(b"{}"))
        sp, ep = os.urandom(32), os.urandom(32)
        self.assertEqual(srv.fingerprint(sp, ep), bus.fingerprint(sp, ep))
        payload = json.dumps({"v": 1, "relay": "https://relay.example", "key": base64.b64encode(b"k" * 32).decode(),
                              "id": "ab" * 16, "by": "operator", "exp": 1793000000}).encode()
        text = busrelay.invite_text(payload, bus.SIG_HEAD + "\nx\n")
        self.assertEqual(srv.read_invite(text)[0], busrelay.read_invite(text)[0])
        self.assertEqual(srv.invite_text(payload, bus.SIG_HEAD + "\nx\n"), text)

    def test_the_address_comes_only_from_an_invitation(self):
        """The core's code names no relay (an embedded address is against open source): no address literal in the
        transport and the core's bus files; a hub that was never given an invitation has no relay."""
        for rel in ("core/busrelay.py", "core/busd.py", "core/busstate.py", "core/buscli.py", "core/bus.py",
                    "modules/bus_relay/run.py", "bin/hub-bus-relay"):
            with open(os.path.join(HERE, rel), encoding="utf-8") as f:
                text = f.read()
            found = re.findall(r"https?://[A-Za-z0-9.\-\[\]:]+", text)
            self.assertEqual(found, [], rel)
        self.assertIsNone(busstate.empty()["relay"])
        self.assertIsNone(busstate.transport_view({"bus": {**busstate.empty(), "enabled": True}})["relay"])

    def test_the_address_plain_http_only_to_this_machine(self):
        for ok in ("https://relay.example", "https://relay.example:8443/bus", "http://127.0.0.1:8080", "http://localhost:9",
                   "http://[::1]:8080"):
            self.assertTrue(busrelay.check_url(ok))
        for bad in ("http://203.0.113.5:8080", "http://relay.example", "https://user:pw@relay.example",
                    "https://relay.example/?a=1", "ftp://relay.example", "https://", "https://relay.example/#x"):
            with self.assertRaises(busrelay.Error, msg=bad):
                busrelay.check_url(bad)

    def test_an_invitation_of_another_form_is_not_read(self):
        payload = json.dumps({"v": 1, "relay": "https://relay.example", "key": base64.b64encode(b"k" * 32).decode(),
                              "id": "ab" * 16, "by": "operator", "exp": 1793000000}).encode()
        good = busrelay.invite_text(payload, bus.SIG_HEAD + "\nx\n")
        self.assertEqual(busrelay.read_invite(good)[0]["relay"], "https://relay.example")
        for bad in (good[:30] + good[33:], good.replace(busrelay.INVITE_PREFIX, "hub-relay-invite-2:"), "", None, "x" * 5000,
                    busrelay.invite_text(payload.replace(b'"v": 1', b'"v": true'), bus.SIG_HEAD),
                    busrelay.invite_text(payload.replace(b"https://relay.example", b"http://203.0.113.5"), bus.SIG_HEAD)):
            with self.assertRaises(busrelay.Error):
                busrelay.read_invite(bad)

    def test_the_card_names_the_relay_by_address_and_key(self):
        key = base64.b64encode(os.urandom(32)).decode()
        ref = busrelay.ref("https://relay.example", key)
        self.assertEqual(busrelay.read_ref(ref), {"url": "https://relay.example", "key": key})
        self.assertIsNone(busrelay.read_ref("https://relay.example"))
        self.assertLessEqual(len(ref), 200)   # a card's relay entry


class Network(unittest.TestCase):
    """The hub reaches its relay through the owner's proxy the way it goes elsewhere (core/net), so a proxy that itself
    listens on TLS works, and a broken answer is a network refusal, not a traceback. Only 127.0.0.1, no relay process."""

    def client(self, url):
        patch = mock.patch.object(busrelay.bus, "ssh_sign", lambda *a: "sig")   # what a relay checks; the fakes do not
        patch.start()
        self.addCleanup(patch.stop)
        return busrelay.Client(url, base64.b64encode(bytes(32)).decode(), "hb1-" + "a" * 26, "transport-key")

    def test_a_proxy_that_listens_on_tls_is_a_network_refusal(self):
        # 2026-11-06: with HTTPS_PROXY=https://… urllib sent the proxy a bare CONNECT and read its TLS alert as a status
        # line; BadStatusLine is neither OSError nor ValueError, so it went past the relay transport as a traceback
        from tests.fakes.proxy import alert_proxy, proxied
        client = self.client("https://relay.example")
        with proxied(alert_proxy(self)):
            for ask in (client.hello, client.invites):
                with self.assertRaises(busrelay.Error) as e:
                    ask()
                self.assertEqual(e.exception.code, "network")

    def test_the_relay_is_reached_through_the_hubs_own_connection(self):
        import http.server
        import threading
        seen = []

        class Relay(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, self.headers.get("X-Hub-Sig"), self.headers.get("X-Hub-Bus-V")))
                self.send_response(200)
                self.send_header("Content-Length", "11")
                self.end_headers()
                self.wfile.write(b'{"bus_v":2}')

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Relay)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        client = self.client(f"http://127.0.0.1:{srv.server_port}")
        made, real = [], busrelay.net.connection
        with mock.patch.object(busrelay.net, "connection", lambda *a: made.append(a) or real(*a)):
            self.assertEqual(client.hello(), 2)
            with self.assertRaises(busrelay.Error) as e:   # the fake does not sign its answer
                client.invites()
        self.assertEqual((e.exception.code, e.exception.status), ("not_relay", 200))
        self.assertEqual(seen, [("/v1/hello", None, None), ("/v1/invites", base64.b64encode(b"sig").decode(), "2")])
        self.assertEqual(made, [("http", "127.0.0.1", srv.server_port, client.timeout)] * 2)


# --- the server --------------------------------------------------------------------------------------------------------

class SignatureFiles(unittest.TestCase):
    def test_the_relay_hands_ssh_keygen_the_signature_and_its_signer_with_lf_only(self):
        # as the core's ssh_verify (tests.test_hubbus.SignatureFiles): with CRLF the ssh-keygen of Git for Windows read
        # no signature, and the relay refused every signed request of a hub
        relay, seen = server_module(), {}

        def run(args, data):
            for flag in ("-f", "-s"):
                with open(args[args.index(flag) + 1], "rb") as f:
                    seen[flag] = f.read()
            return 0, b"", b""
        sig = "-----BEGIN SSH SIGNATURE-----\nAAAA\n-----END SSH SIGNATURE-----\n"
        with mock.patch.object(relay, "_run", run):
            self.assertTrue(relay.verify_ssh(bytes(32), b"x", sig))   # the way without the library
        self.assertEqual(seen["-s"], sig.encode("ascii"))
        self.assertNotIn(b"\r", seen["-f"])


@need_ssh
class Server(unittest.TestCase):
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

    def test_registration_by_invitation_once(self):
        inv = self.relay.invite()
        a = Ident(self.relay, "a")
        got = a.join(inv, "A")
        self.assertEqual((got["fp"], got["invited_by"]), (a.fp, "operator"))
        self.assertTrue(a.join(inv, "A")["already"])           # a lost answer: the same hub again, nothing spent
        b = Ident(self.relay, "b")
        with self.assertRaises(busrelay.Error) as e:
            b.join(inv, "B")                                     # one invitation — one hub
        self.assertEqual(e.exception.code, "bad_invite")
        with self.assertRaises(busrelay.Error) as e:
            Ident(self.relay, "c").join(None, "C")               # a new hub needs one
        self.assertEqual(e.exception.code, "bad_invite")
        with self.assertRaises(busrelay.Error) as e:
            Ident(self.relay, "c").client.allow([])              # unregistered: nothing else
        self.assertEqual(e.exception.code, "unknown_hub")

    def test_one_invitation_two_hubs_at_once_one_wins(self):
        import threading
        inv = self.relay.invite()
        hubs = [Ident(self.relay, f"p{i}") for i in range(4)]
        got = {}

        def join(h):
            try:
                got[h.fp] = h.join(inv, "P")["fp"]
            except busrelay.Error as e:
                got[h.fp] = e.code
        threads = [threading.Thread(target=join, args=(h,)) for h in hubs]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(sorted(v == k for k, v in got.items()), [False, False, False, True], got)
        self.assertEqual({v for k, v in got.items() if v != k}, {"bad_invite"})

    def test_a_registered_hub_makes_invitations_signed_by_its_key_within_a_quota(self):
        a = self.joined("a")
        b = self.joined("b", by=a)
        code, out, _ = self.relay.cmd("hubs")
        self.assertEqual(code, 0)
        self.assertIn(f"{b.fp}  'b'  invited by {a.fp}", out)   # the operator sees who invited whom
        mine = a.client.invites()
        self.assertEqual([(i["used_by"]) for i in mine], [b.fp])
        texts = [a.invite() for _ in range(5)]                     # five unused at once at most
        self.assertEqual(len(texts), 5)
        with self.assertRaises(busrelay.Error) as e:
            a.invite()
        self.assertEqual(e.exception.code, "quota")
        # an invitation another hub's key signed, or one changed after signing, is not taken
        fields, payload, sig = busrelay.read_invite(texts[0])
        forged = busrelay.invite_text(payload.replace(str(fields["exp"]).encode(), str(fields["exp"] - 60).encode()), sig)
        with self.assertRaises(busrelay.Error) as e:
            Ident(self.relay, "x").join(forged, "X")
        self.assertEqual(e.exception.code, "bad_invite")
        other = busrelay.invite_text(payload, bus.ssh_sign(b.keys.sign_key, busrelay.invite_line(payload), busrelay.NS))
        with self.assertRaises(busrelay.Error) as e:
            Ident(self.relay, "x").join(other, "X")
        self.assertEqual(e.exception.code, "bad_invite")
        with self.assertRaises(busrelay.Error):   # an invitation in another hub's name is not recorded
            text, _, _ = busrelay.make_invite(b.keys, {"url": self.relay.url, "key": self.relay.key}, time.time())
            a.client.invite(text)
        Ident(self.relay, "y").join(texts[1], "Y")                 # the real one works once

    def test_the_operator_removes_a_hub_and_what_it_invited(self):
        a = self.joined("ra")
        b = self.joined("rb", by=a)
        c = self.joined("rc", by=b)
        spare = a.invite()
        raw, _ = box(a, c)
        c.client.allow([a.fp])
        a.client.send(c.fp, raw)
        code, out, _ = self.relay.cmd("remove", b.fp, "--tree")
        self.assertEqual(code, 0)
        self.assertEqual(set(re.findall(r"hb1-\w+", out)), {b.fp, c.fp})
        for h in (b, c):
            with self.assertRaises(busrelay.Error) as e:
                h.client.inbox(0)
            self.assertEqual(e.exception.code, "removed")
            with self.assertRaises(busrelay.Error) as e:
                h.join(None, "again")                              # a removed fingerprint does not come back
            self.assertEqual(e.exception.code, "removed")
        self.assertEqual(a.client.inbox(0), [])
        with sqlite3.connect(self.relay.db) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM queue WHERE dst = ? OR src = ?", (c.fp, c.fp)).fetchone()[0], 0)
        Ident(self.relay, "rd").join(spare, "D")                   # a's own invitation still works: a is here

    def test_a_request_is_good_once_and_only_for_this_relay(self):
        a = self.joined("na")
        c = a.client
        body = b'{"from":[]}'
        ts, nonce = int(time.time()), os.urandom(16).hex()
        sig = bus.ssh_sign(a.tkey, busrelay.request_line(self.relay.key, "POST", "/v1/allow", ts, nonce, body), busrelay.NS)
        headers = {"X-Hub-Fp": a.fp, "X-Hub-Time": str(ts), "X-Hub-Nonce": nonce,
                   "X-Hub-Sig": base64.b64encode(sig.encode()).decode(), "Content-Type": "application/json"}
        import urllib.request
        import urllib.error

        def post(h, data=body):
            req = urllib.request.Request(self.relay.url + "/v1/allow", data=data, headers=h, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                with e:
                    return e.code, json.loads(e.read())
        self.assertEqual(post(headers)[0], 200)
        self.assertEqual(post(headers), (409, {"code": "replay"}))                                   # repeated
        self.assertEqual(post({**headers, "X-Hub-Nonce": os.urandom(16).hex()})[1]["code"], "bad_signature")
        self.assertEqual(post({**headers, "X-Hub-Nonce": os.urandom(16).hex()}, b'{"from":["x"]}')[1]["code"],
                         "bad_signature")                                                            # another body
        req = urllib.request.Request(self.relay.url + "/v1/ack", data=body, method="POST",
                                     headers={**headers, "X-Hub-Nonce": nonce})
        with self.assertRaises(urllib.error.HTTPError) as e:                                         # another path
            urllib.request.urlopen(req, timeout=10)
        with e.exception:
            self.assertIn(json.loads(e.exception.read())["code"], ("bad_signature", "replay"))
        other = base64.b64encode(os.urandom(32)).decode()
        ts2, n2 = int(time.time()), os.urandom(16).hex()
        sig2 = bus.ssh_sign(a.tkey, busrelay.request_line(other, "POST", "/v1/allow", ts2, n2, body), busrelay.NS)
        self.assertEqual(post({**headers, "X-Hub-Time": str(ts2), "X-Hub-Nonce": n2,
                               "X-Hub-Sig": base64.b64encode(sig2.encode()).decode()})[1]["code"], "bad_signature")
        old = int(time.time()) - 400
        sig3 = bus.ssh_sign(a.tkey, busrelay.request_line(self.relay.key, "POST", "/v1/allow", old, n2, body), busrelay.NS)
        self.assertEqual(post({**headers, "X-Hub-Time": str(old), "X-Hub-Nonce": n2,
                               "X-Hub-Sig": base64.b64encode(sig3.encode()).decode()})[1]["code"], "clock")
        # the hub's own key cannot sign requests: only the transport key it bound
        bad = busrelay.Client(self.relay.url, self.relay.key, a.fp, a.keys.sign_key)
        with self.assertRaises(busrelay.Error) as e:
            bad.allow([])
        self.assertEqual(e.exception.code, "bad_signature")
        self.assertEqual(c.allow([])["allowed"], 0)

    def test_an_answer_not_signed_by_the_relay_key_is_not_taken(self):
        a = self.joined("ka")
        wrong = busrelay.Client(self.relay.url, base64.b64encode(os.urandom(32)).decode(), a.fp, a.tkey)
        with self.assertRaises(busrelay.Error) as e:
            wrong.inbox(0)
        self.assertIn(e.exception.code, ("not_relay", "bad_signature"))

    def test_a_new_transport_key_is_bound_only_by_the_hub_key(self):
        a = self.joined("ta")
        old = a.tkey
        os.unlink(old)
        os.unlink(old + ".pub")
        new = busrelay.transport_key(a.folder, make=True)[0]
        a.client = busrelay.Client(self.relay.url, self.relay.key, a.fp, new)
        self.assertTrue(a.join(None, "A")["already"])      # a new machine: the hub's key binds the new transport key
        self.assertEqual(a.client.inbox(0), [])
        x = Ident(self.relay, "tx")                         # someone else's key cannot bind a transport key to a
        x.keys = bus.Keys(**{**vars(x.keys), "fp": a.fp, "sign_pub": a.keys.sign_pub, "enc_pub": a.keys.enc_pub})
        x.client = busrelay.Client(self.relay.url, self.relay.key, a.fp, x.tkey)
        with self.assertRaises(busrelay.Error) as e:
            x.join(None, "A")
        self.assertEqual(e.exception.code, "bad_signature")

    def test_send_take_and_acknowledge_only_ones_own(self):
        a, b, c = self.joined("sa"), self.joined("sb"), self.joined("sc")
        raw, mid = box(a, b, 2048)
        with self.assertRaises(busrelay.Error) as e:
            a.client.send(b.fp, raw)                       # b does not take envelopes from a yet
        self.assertEqual(e.exception.code, "not_allowed")
        b.client.allow([a.fp])
        self.assertEqual(a.client.send(b.fp, raw)["stored"], "new")
        self.assertEqual(a.client.send(b.fp, raw)["stored"], "already")   # a repeat takes no new place
        other, _ = box(a, b, 2048, mid=mid)
        with self.assertRaises(busrelay.Error) as e:
            a.client.send(b.fp, other)
        self.assertEqual(e.exception.code, "conflict")
        with self.assertRaises(busrelay.Error) as e:
            c.client.send(b.fp, raw)                        # c cannot hand a's envelope
        self.assertEqual(e.exception.code, "not_yours")
        self.assertEqual(c.client.inbox(0), [])             # b's queue is b's
        self.assertEqual(c.client.ack([(a.fp, mid)])["removed"], 0)
        items = b.client.inbox(0)
        self.assertEqual([(i["from"], i["id"], base64.b64decode(i["envelope"])) for i in items], [(a.fp, mid, raw)])
        self.assertEqual(b.client.ack([(a.fp, mid), (c.fp, "cd" * 16)])["removed"], 1)
        self.assertEqual(b.client.inbox(0), [])
        # an introduction goes to any registered hub, no allow list asked; ten of one sender wait for a hub at once
        # (the share of one sender in a hub's room: tests/test_relay_limits)
        for _ in range(10):
            c.client.send(b.fp, intro(c))
        with self.assertRaises(busrelay.Error) as e:
            c.client.send(b.fp, intro(c))
        self.assertEqual(e.exception.code, "busy")
        with self.assertRaises(busrelay.Error) as e:
            a.client.send("hb1-" + "a" * 26, box(a, b)[0])  # an envelope for another hub than named
        self.assertEqual(e.exception.code, "bad_envelope")

    def test_the_relay_takes_only_the_envelope_form_and_the_term(self):
        a, b = self.joined("fa"), self.joined("fb")
        b.client.allow([a.fp])
        now = int(time.time())
        for raw in (box(a, b, 1000)[0],                                      # not a bucket's size
                    box(a, b, ts=now - 8 * 86400, exp=now - 86400)[0],       # expired
                    box(a, b, ts=now + 3600)[0],                             # from the future
                    box(a, b, ts=now, exp=now + 8 * 86400)[0],               # longer than 7 days
                    box(a, b, ts=now, exp=now)[0],                           # inverted
                    b"not json", json.dumps({"head": "{}", "sig": "x"}).encode()):
            with self.assertRaises(busrelay.Error) as e:
                a.client.send(b.fp, raw)
            self.assertEqual(e.exception.code, "bad_envelope", raw[:80])

    def test_a_long_poll_answers_when_an_envelope_comes(self):
        a, b = self.joined("la"), self.joined("lb")
        b.client.allow([a.fp])
        import threading
        got = {}

        def poll():
            t = time.monotonic()
            got["items"] = b.client.inbox(10)
            got["took"] = time.monotonic() - t
        th = threading.Thread(target=poll)
        th.start()
        time.sleep(1.0)
        a.client.send(b.fp, box(a, b)[0])
        th.join(15)
        self.assertEqual(len(got["items"]), 1)
        self.assertLess(got["took"], 6)

    def test_leaving_drops_the_queue_and_the_registration(self):
        a, b = self.joined("va"), self.joined("vb")
        b.client.allow([a.fp])
        a.client.allow([b.fp])
        a.client.send(b.fp, box(a, b)[0])
        b.client.send(a.fp, box(b, a)[0])                   # handed by b before it leaves
        c = self.joined("vc", by=b)
        b.client.leave()
        with self.assertRaises(busrelay.Error) as e:
            b.client.inbox(0)
        self.assertEqual(e.exception.code, "unknown_hub")
        with sqlite3.connect(self.relay.db) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM queue WHERE dst = ?", (b.fp,)).fetchone()[0], 0)
        self.assertEqual([i["from"] for i in a.client.inbox(0)], [b.fp])   # what it handed is still delivered
        y = self.joined("vy")
        c.client.leave()
        c.join(y.invite(), "back")                                         # c comes back on another hub's invitation
        code, out, _ = self.relay.cmd("remove", b.fp, "--tree")          # leaving does not take it out of the tree
        self.assertEqual(set(re.findall(r"hb1-\w+", out)), {b.fp, c.fp})

    def test_an_answer_to_a_changed_request_is_not_taken_for_the_hubs_own(self):
        """Someone on the way changes a hub's request (its fingerprint, its signature) and keeps the nonce: the relay
        signs its refusal, but over that changed request — the hub does not take it as the answer to its own."""
        import urllib.request
        import urllib.error
        a = self.joined("ma")
        body = b'{"from":[]}'
        ts, nonce = int(time.time()), os.urandom(16).hex()
        sig = bus.ssh_sign(a.tkey, busrelay.request_line(self.relay.key, "POST", "/v1/allow", ts, nonce, body), busrelay.NS)
        sig64 = base64.b64encode(sig.encode()).decode()
        forged = {"X-Hub-Fp": "hb1-" + "b" * 26, "X-Hub-Time": str(ts), "X-Hub-Nonce": nonce, "X-Hub-Sig": sig64,
                  "Content-Type": "application/json"}
        req = urllib.request.Request(self.relay.url + "/v1/allow", data=body, headers=forged, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=10)
        with e.exception as r:
            status, got, rsig = r.code, r.read(), r.headers.get("X-Relay-Sig")
        self.assertEqual(json.loads(got)["code"], "unknown_hub")
        relay_pub = base64.b64decode(self.relay.key)
        theirs = busrelay.request_digest(forged["X-Hub-Fp"], ts, nonce, sig64, "POST", "/v1/allow", body)
        mine = busrelay.request_digest(a.fp, ts, nonce, sig64, "POST", "/v1/allow", body)
        sig_text = base64.b64decode(rsig).decode()
        self.assertTrue(bus.ssh_verify(relay_pub, busrelay.response_line(nonce, status, got, theirs), sig_text,
                                       busrelay.NS))
        self.assertFalse(bus.ssh_verify(relay_pub, busrelay.response_line(nonce, status, got, mine), sig_text,
                                        busrelay.NS))


class FakeClient:
    """The relay as the transport sees it, without a network: what was sent, what waits, what each call answers."""

    def __init__(self, items=(), fail=None):
        self.calls, self.items, self.fail = [], list(items), fail or {}

    def allow(self, fps):
        self.calls.append(("allow", list(fps)))

    def send(self, to, raw):
        self.calls.append(("send", to))
        if "send" in self.fail:
            raise busrelay.Error(self.fail["send"])

    def inbox(self, wait=0):
        self.calls.append(("inbox", wait))
        return list(self.items)

    def ack(self, items):
        self.calls.append(("ack", list(items)))

    def hello(self):   # the relay's protocol version (slice L0: GET /v1/hello)
        self.calls.append(("hello",))
        return 1


class TransportPasses(unittest.TestCase):
    """The relay transport's passes (modules/bus_relay): retries, a full core folder, a new join."""

    def setUp(self):
        from modules.bus_relay import run
        self.run = run
        self.state = tempfile.mkdtemp(prefix="relay-t-")
        self.now = 1_800_000_000
        self.put = []
        self.t = run.Transport(self.state, put=lambda k, d: self.put.append((k, d)), clock=lambda: self.now,
                               log=lambda text: None)
        self.fake = FakeClient()
        self.t.client = lambda v: self.fake
        os.makedirs(busd.out_dir(self.state))

    def view(self, out=(), allow=(), ts=1):
        for o in out:
            with open(os.path.join(busd.out_dir(self.state), f"{o['out']}.env"), "wb") as f:
                f.write(b"x")
        return {"enabled": True, "me": "hb1-" + "a" * 26, "allow": list(allow),
                "relay": {"url": "https://relay.example", "key": base64.b64encode(b"k" * 32).decode(), "ts": ts}, "out": list(out)}

    def test_a_failed_repeat_of_a_handed_message_waits_its_retry(self):
        o = {"out": "o1", "fp": "hb1-" + "b" * 26, "id": "ab" * 16, "exp": self.now + 86400, "kind": "message",
             "status": "handed", "handed": self.now - 700}
        self.fake.fail = {"send": "busy"}
        v = self.view([o])
        self.t.hand(v)
        self.t.hand(v)
        self.assertEqual([c for c in self.fake.calls if c[0] == "send"], [("send", o["fp"])])   # not again a second later
        self.now += self.run.AGAIN[0]          # its next step without a receipt, not the short retry of a new one
        self.t.hand(v)
        self.assertEqual(len([c for c in self.fake.calls if c[0] == "send"]), 2)

    def test_what_does_not_fit_is_left_on_the_relay_without_asking_again_at_once(self):
        self.fake.items = [{"from": "hb1-" + "b" * 26, "id": "ab" * 16, "envelope": base64.b64encode(b"y" * 64).decode()}]
        waited = []
        self.t.stop.wait = lambda s: waited.append(s)
        with mock.patch.object(self.run, "IN_BYTES", 10):
            self.assertEqual(self.t.take(self.view(), wait=0), 0)
        self.assertEqual([c[0] for c in self.fake.calls], ["inbox"])   # no ack
        self.assertEqual(waited, [self.run.IDLE])

    def test_a_waiting_item_that_is_not_an_envelope_is_acknowledged_and_dropped(self):
        self.fake.items = [{"from": "hb1-" + "b" * 26, "id": "ab" * 16, "envelope": "not base64!"}]
        self.t.take(self.view(), wait=0)
        self.assertEqual(self.fake.calls[-1], ("ack", [("hb1-" + "b" * 26, "ab" * 16)]))
        self.assertEqual(self.put, [])

    def test_the_client_resets_the_allow_list_on_a_new_join(self):
        """The real client(): another join time in the view — a new client and the allow list sent again."""
        from modules.bus_relay import run
        cfg = tempfile.mkdtemp(prefix="relay-cfg-")
        if not shutil_which("ssh-keygen"):
            self.skipTest("no ssh-keygen")
        busrelay.transport_key(cfg, make=True)
        t = run.Transport(self.state, cfg, put=lambda k, d: None, clock=lambda: self.now, log=lambda text: None)
        t.allowed = (["x"], self.now)
        c1 = t.client(self.view(ts=1))
        self.assertIsNone(t.allowed[0])
        t.allowed = (["x"], self.now)
        self.assertIs(t.client(self.view(ts=1)), c1)
        self.assertEqual(t.allowed[0], ["x"])
        self.assertIsNot(t.client(self.view(ts=2)), c1)
        self.assertIsNone(t.allowed[0])


def shutil_which(name):
    import shutil
    return shutil.which(name)


# --- the core: who may hand and offer, the owner's relay, the transport view ------------------------------------------

@need_lib
@need_ssh
class Core(unittest.TestCase):
    def hub(self):
        from tests.test_hubbus_s1 import LiveHub
        return LiveHub("Hub A", now=int(time.time()))

    def invitation(self, url="https://relay.example"):
        payload = json.dumps({"v": 1, "relay": url, "key": base64.b64encode(os.urandom(32)).decode(),
                              "id": os.urandom(16).hex(), "by": "operator", "exp": int(time.time()) + 3600}).encode()
        return busrelay.invite_text(payload, bus.SIG_HEAD + "\nx\n")

    def test_only_the_owner_joins_a_relay_and_the_card_names_it(self):
        a = self.hub()
        a.up("api")
        inv = self.invitation()
        for who in ({"channel": "session", "owner": False, "sender": "api"},
                    {"channel": "plugin", "owner": False, "sender": "mod:x"},
                    {"channel": "mod:x", "owner": True}):
            ev = a.rec("bus.relay_requested", {"op": "join", "invite": inv, **who})
            self.assertIn("only the owner", a.verdict(ev))
        self.assertIsNone(a.bus.get("relay"))
        ev = a.ask("bus.relay_requested", {"op": "join", "invite": inv})
        self.assertIsNone(a.verdict(ev))
        self.assertEqual(a.bus["relay"]["url"], "https://relay.example")
        ev = a.ask("bus.relay_requested", {"op": "join", "invite": inv.replace("hub-relay-invite-1:", "x")})
        self.assertIn("invitation", a.verdict(ev))
        with a.env():
            keys = bus.load(bus.key_dir(a.config))
        card = bus.read_card(bus.make_card(keys, "A", a.now, relays=busd.relays_of(a.bus)), a.now)
        self.assertEqual([busrelay.read_ref(r)["url"] for r in card["relays"]], ["https://relay.example"])
        ev = a.ask("bus.relay_requested", {"op": "leave"})
        self.assertIsNone(a.verdict(ev))
        self.assertIsNone(a.bus["relay"])

    def test_a_transport_is_the_owner_the_relay_part_or_a_module_with_the_right(self):
        a = self.hub()
        b = self.hub()
        a.ask("bus.contact_requested", {"op": "add", "name": "bee", "card": b.card(), "code": b.code()})
        (oid,) = [k for k, o in a.bus["out"].items() if o.get("kind") == "intro"]
        with a.env():
            a.up("api")
            ev = a.h.record("bus.handed", None, {"out": oid, "via": "x", "channel": "session", "owner": False,
                                                 "sender": "api"})
        self.assertIn("transport", a.verdict(ev))
        with a.env():
            ev = a.h.record("bus.handed", None, {"out": oid, "via": "relay"}, src="bus-relay")
        self.assertIsNone(a.verdict(ev))
        self.assertNotIn(oid, a.bus["out"])
        # a module: the daemon checks the right before the fact is written
        a.h.modules.found = []
        a.h.modules.written = {"carrier": {"capabilities": ["bus.transport"]}, "other": {"capabilities": ["net"]}}
        self.assertIsNone(a.h.bus_transport_admits("mod:carrier"))
        self.assertIn("bus.transport", a.h.bus_transport_admits("mod:other"))
        self.assertIn("bus.transport", a.h.bus_transport_admits("ext:plugin"))
        verdicts = {}
        for name in ("carrier", "other"):   # hubmod.fact of a module process: the core names the sender
            item = {"type": "bus.offered", "entity": None, "src": "module", "ts": time.time(),
                    "data": {"path": os.path.join(busd.in_dir(a.h.state_dir), "x.env"), "sha256": "0" * 64,
                             "size": 1, "channel": "plugin", "owner": False, "sender": f"mod:{name}"}}
            path = os.path.join(a.h.state_dir, f"spool-{name}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(item, f)
            seq = a.h.journal.seq
            with a.env():
                a.h.spooled(path, item)
            verdicts[name] = a.verdict({"seq": seq + 1})
        self.assertNotIn("bus.transport", verdicts["carrier"] or "")   # taken by the right (the file itself is not there)
        self.assertIn("bus.transport", verdicts["other"])

    def test_the_transport_view_names_no_keys_and_no_texts(self):
        from tests.test_hubbus_s1 import Wire, acquainted
        with mock.patch("tests.test_hubbus_s1.NOW", int(time.time())):
            a, b, wire = acquainted()
        busd.write_view(a.h)
        with open(busd.transport_path(a.h.state_dir), encoding="utf-8") as f:
            text = f.read()
        v = json.loads(text)
        self.assertEqual(v["allow"], [b.bus["me"]["fp"]])
        self.assertTrue(v["enabled"])
        for secret in (a.bus["me"]["sign_pub"], a.bus["me"]["enc_pub"], "envelope", "text"):
            self.assertNotIn(secret, text)


# --- the stand: two hubs through a real relay ------------------------------------------------------------------------

@need_lib
@need_ssh
class Stand(unittest.TestCase):
    """Two cores of the stand, a real relay process, the relay transport of each hub: introduction → sending → the
    answer in the thread; the relay keeps only sealed bytes."""

    def setUp(self):
        from tests.test_hubbus_s1 import LiveHub
        self.relay = RelayProcess()
        self.addCleanup(self.relay.stop)
        now = int(time.time())
        self.a, self.b = LiveHub("Hub A", now=now), LiveHub("Hub B", now=now)
        self.join(self.a, self.relay.invite())
        self.join(self.b, self.cli(self.a, ["relay", "invite"])[1].splitlines()[0])   # A invites B
        self.ta, self.tb = self.transport(self.a), self.transport(self.b)

    def cli(self, hub, argv):
        """`hub bus …` in the owner's terminal of that hub: (exit code, what it printed)."""
        from core import buscli
        import io

        def put(type_, entity, data, say=True):
            return hub.rec(type_, {**data, **OWNER}, entity)["seq"]
        out = io.StringIO()
        with hub.env(), contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = buscli.main(argv, put, lambda: hub.h.state, hub.h.state_dir)
        return code, out.getvalue().strip()

    def join(self, hub, invite):
        code, out = self.cli(hub, ["relay", "join", invite])
        self.assertEqual(code, 0, out)
        self.assertEqual(hub.bus["relay"]["url"], self.relay.url)

    def transport(self, hub):
        from modules.bus_relay.run import Transport

        def put(kind, data):
            with hub.env():
                hub.h.record(kind, None, data, src="bus-relay")
        return Transport(hub.h.state_dir, hub.config, put=put, log=lambda text: None)

    def settle(self, rounds=6):
        """Each hub writes its transport view, hands what it has and takes what waits, until nothing moves (True) or
        the rounds are used up (False)."""
        for _ in range(rounds):
            moved = 0
            for hub, t in ((self.a, self.ta), (self.b, self.tb)):
                hub.now = int(time.time())
                busd.write_view(hub.h)
                v = t.view()
                moved += t.hand(v) + t.take(v, wait=0)
            if not moved:
                return True
        return False

    def test_introduction_sending_and_the_answer_through_the_relay(self):
        a, b = self.a, self.b
        code, card = self.cli(b, ["card"])
        self.assertIn(busrelay.ref(self.relay.url, self.relay.key), json.dumps(bus.read_card(card, time.time())))
        a.ask("bus.contact_requested", {"op": "add", "name": "bee", "card": card, "code": b.code()})
        self.settle()                                          # A's introduction reaches B through the relay
        (intro_card,) = b.cards()
        b.answer(intro_card, "accept")
        name_at_b = next(iter(b.bus["contacts"]))
        self.settle()
        a.up("api")
        b.up("maps")
        text = "Please send the response schema GET /v1/items over the relay"
        mid, why = a.say("api", text, bus_to="bee", bus_to_via="local")
        self.assertIsNone(why)
        _, why = a.as_liaison("bus.send_requested", {"contact": "bee", "agent": "maps", "text_from": mid})
        self.assertIsNone(why)
        self.assertEqual(a.cards(), [])
        # delivery is the rounds of the relay passes (take with wait=0), not a wait for a poll or a retry: the message and
        # the quiet after it fit in the rounds. Not a wall-clock limit: on a loaded machine one round is seconds
        self.assertTrue(self.settle(), "the exchange over the relay did not settle in the rounds")
        (item,) = [x for x in b.bus["inbox"].values() if x.get("text") == text]
        self.assertEqual((item["agent_from"], item["contact"]), ("api", name_at_b))
        bid = next(k for k, x in b.bus["inbox"].items() if x is item)
        _, why = b.as_liaison("bus.pass_requested", {"inbox": bid, "session": "maps"})
        self.assertIsNone(why)
        reply = "Schema: {items: [...], cursor}"
        mid2, _ = b.say("maps", reply, bus_to=name_at_b, bus_to_via="local")
        _, why = b.as_liaison("bus.send_requested", {"contact": name_at_b, "thread": item["thread"], "text_from": mid2})
        self.assertIsNone(why)
        self.assertEqual(b.cards(), [])
        self.settle()
        answers = [x for x in a.bus["inbox"].values() if x.get("text") == reply]
        self.assertEqual(len(answers), 1)
        self.assertEqual(answers[0]["source"], "api")
        self.assertEqual([o["status"] for o in a.bus["out"].values() if o.get("kind") == "message"], ["delivered"])
        self.assertEqual([o["status"] for o in b.bus["out"].values() if o.get("kind") == "message"], ["delivered"])
        self.relay.stop()
        dump = self.relay.dump() + self.relay.log
        # the agent names as the open JSON would carry them: three bare letters match the relay's binary by chance
        # (b"api" in 660 KB of ciphertext and pages of SQLite — a red in about one run of twenty-five)
        for words in (text, reply, '"api"', '"maps"', "schem", "cursor"):
            self.assertNotIn(words.encode("utf-8"), dump)
            self.assertNotIn(base64.b64encode(words.encode("utf-8"))[:12], dump)

    def test_an_offline_receiver_gets_it_later_and_a_relay_restart_loses_nothing(self):
        a, b = self.a, self.b
        code, card = self.cli(b, ["card"])
        a.ask("bus.contact_requested", {"op": "add", "name": "bee", "card": card, "code": b.code()})
        self.settle()
        b.answer(b.cards()[0], "accept")
        self.settle()
        a.ask("bus.contact_requested", {"op": "set", "name": "bee", "auto_from": ["api"]})
        sent = []
        for i in range(3):
            mid, _ = a.say("api", f"message {i}", bus_to="bee", bus_to_via="local")
            a.as_liaison("bus.send_requested", {"contact": "bee", "text_from": mid})
            for card in a.cards():   # the first message to a new contact is always a card
                a.answer(card, "send")
            sent.append(f"message {i}")
            if i == 1:
                self.relay.stop()                     # the relay goes down between two envelopes
                busd.write_view(a.h)
                with self.assertRaises(busrelay.Error):
                    self.ta.hand(self.ta.view())
                self.relay.start()
                self.ta.tries.clear()                 # the retry's time has come
        busd.write_view(a.h)
        self.ta.hand(self.ta.view())                  # B is offline: nothing is taken yet
        self.assertEqual([x for x in b.bus["inbox"].values() if x.get("text", "").startswith("message")], [])
        self.settle()                                 # B comes back
        got = sorted(x["text"] for x in b.bus["inbox"].values() if x.get("text", "").startswith("message"))
        self.assertEqual(got, sent)                   # each once
        self.assertEqual(sorted(o["status"] for o in a.bus["out"].values() if o.get("kind") == "message"),
                         ["delivered"] * 3)

    def test_no_transport_while_the_cores_crypto_check_fails(self):
        """A core whose start check failed (crypto_ready false) names nothing to a transport: it does not start."""
        self.a.rec("core.bus_key_failed", {"code": "selfcheck", "reason": "test"})
        busd.write_view(self.a.h)
        self.assertIsNone(self.ta.view())

    def test_the_transport_works_with_the_hub_keys_closed_to_it(self):
        """The transport reads only its own key: the folder of the hub's keys can be closed to it."""
        if os.name == "nt" or os.geteuid() == 0:
            self.skipTest("closing a folder by its mode needs a POSIX user that is not root")
        folder = bus.key_dir(self.a.config)
        os.chmod(folder, 0)
        self.addCleanup(os.chmod, folder, stat.S_IRWXU)
        busd.write_view(self.a.h)
        self.ta.hand(self.ta.view())
        self.assertEqual(self.ta.take(self.ta.view(), wait=0), 0)
        with self.assertRaises(OSError):
            os.listdir(folder)


# --- the S1 tails the relay touches: an unknown hub knocks, a load of strangers ---------------------------------------

@need_lib
@need_ssh
class Knock(unittest.TestCase):
    def offer(self, hub, raw):
        folder = busd.in_dir(hub.h.state_dir)
        os.makedirs(folder, exist_ok=True)
        digest = hashlib.sha256(raw).hexdigest()
        path = os.path.join(folder, digest + ".env")
        with open(path, "wb") as f:
            f.write(raw)
        hub.ask("bus.offered", {"path": path, "sha256": digest, "size": len(raw)})

    def stranger(self, hub, stranger):
        """An envelope of a hub that is not a contact (sealed for us: we are its contact, it is not ours)."""
        with stranger.env():
            keys = bus.load(bus.key_dir(stranger.config))
        me = hub.bus["me"]
        return bus.seal(keys, {"fp": me["fp"], "enc_pub": me["enc_pub"]}, bus.message("api", "hush-hush"), hub.now)[1]

    def notices(self, hub):
        return [e for e in self.journal(hub) if e["type"] == "owner.notify" and (e["data"] or {}).get("source") == "bus"]

    def journal(self, hub):
        with open(os.path.join(hub.h.state_dir, "journal.jsonl"), encoding="utf-8") as f:
            return [json.loads(line) for line in f]

    def test_one_notice_an_hour_without_the_content(self):
        from tests.test_hubbus_s1 import LiveHub
        now = int(time.time())
        a = LiveHub("Hub A", now=now)
        x, y = LiveHub("X", now=now), LiveHub("Y", now=now)
        self.offer(a, self.stranger(a, x))
        (n,) = self.notices(a)
        self.assertIn(x.bus["me"]["fp"], n["data"]["text"])
        self.assertNotIn("hush-hush", n["data"]["text"])
        self.offer(a, self.stranger(a, x))
        self.offer(a, self.stranger(a, y))
        self.assertEqual(len(self.notices(a)), 1)          # the hour is not over
        a.now += 3601
        a.h.clock = lambda: a.now
        self.offer(a, self.stranger(a, x))
        self.assertEqual(len(self.notices(a)), 1)          # x — once a day
        self.offer(a, self.stranger(a, y))
        notes = self.notices(a)
        self.assertEqual(len(notes), 2)
        self.assertIn("3", notes[1]["data"]["text"])       # the quiet ones since the last notice are counted
        self.assertEqual(a.bus["inbox"], {})

    def test_strangers_beyond_the_hour_are_counted_not_written(self):
        from tests.test_hubbus_s1 import LiveHub
        now = int(time.time())
        a = LiveHub("Hub A", now=now)
        raw = b"not an envelope"
        with mock.patch.object(busd, "REJECTED_HOUR", 5):
            for _ in range(8):
                self.offer(a, raw + os.urandom(4))
            rejected = [e for e in self.journal(a) if e["type"] == "core.bus_rejected"]
            self.assertEqual(len(rejected), 5)
            a.now += 3600
            a.h.clock = lambda: a.now
            self.offer(a, raw)
        many = [e["data"] for e in self.journal(a) if e["type"] == "core.bus_rejected" and e["data"]["code"] == "many"]
        self.assertEqual([m["n"] for m in many], [3])

    def test_introductions_of_strangers_are_checked_twenty_a_minute(self):
        from tests.test_hubbus_s1 import LiveHub
        now = int(time.time())
        a = LiveHub("Hub A", now=now)
        x = LiveHub("X", now=now)
        with x.env():
            keys = bus.load(bus.key_dir(x.config))
        with mock.patch.object(busd, "INTRO_CHECKS", 2):
            for _ in range(4):
                self.offer(a, bus.intro_envelope(keys, "X", now))
        codes = [e["data"].get("code") for e in self.journal(a) if e["type"] == "core.bus_rejected"]
        self.assertEqual(codes.count("rate"), 2)
        self.assertEqual(len(a.bus["intros"]), 1)

    def test_introduction_cards_wait_for_a_free_place(self):
        """Through a transport without the relay's day limit (a folder, a module): not more than INTRO_CARDS strangers'
        cards wait for the owner at once; the others are refused before their key is checked."""
        from tests.test_hubbus_s1 import LiveHub
        now = int(time.time())
        a = LiveHub("Hub A", now=now)
        xs = [LiveHub(f"X{i}", now=now) for i in range(3)]
        with mock.patch.object(bus, "INTRO_CARDS", 2):
            for x in xs:
                with x.env():
                    keys = bus.load(bus.key_dir(x.config))
                self.offer(a, bus.intro_envelope(keys, x.label, now))
        self.assertEqual(len([r for r in a.bus["intros"].values() if r.get("card")]), 2)
        last = [e["data"] for e in self.journal(a) if e["type"] == "core.bus_rejected"][-1]
        self.assertEqual((last["code"], last["limit"]), ("rate", "intro_cards"))

    def test_knocks_past_the_hours_records_are_counted_into_the_next_notice(self):
        from tests.test_hubbus_s1 import LiveHub
        now = int(time.time())
        a = LiveHub("Hub A", now=now)
        x = LiveHub("X", now=now)
        with mock.patch.object(busd, "REJECTED_HOUR", 2):
            for _ in range(5):
                self.offer(a, self.stranger(a, x))
            a.now += 2 * 86400
            a.h.clock = lambda: a.now
            y = LiveHub("Y", now=a.now)
            self.offer(a, self.stranger(a, y))
        notes = self.notices(a)
        self.assertEqual(len(notes), 2)
        self.assertIn(": 4.", notes[1]["data"]["text"])   # 1 written quietly + 3 past the hour's records


if __name__ == "__main__":
    unittest.main()
