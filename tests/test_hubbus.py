"""The hub bus, slice S0 (spec hubbus §7, tests §11): the hub's two keys (signature — ssh-keygen -Y, exchange — X25519 of
the cryptography library), the fingerprint and the check code, the content sealed with HPKE (RFC 9180) under the binding
`info`, the envelope signed over the exact `head` string, the checks on receipt in the spec's order (signature → window
and repeat → limit → decryption), the repeat protection, the bus contact table with an empty `person`, and two hubs on a
stand that pass envelopes through a folder on disk — no network, no relay, no liaison.

The library is needed only with the bus on: without cryptography ≥ 47 these tests are skipped with the reason, and the
core itself works without it (class WithoutTheLibrary). For the tests, the library may lie in `.pylib` of the working copy
or at HUB_TEST_PYLIB."""
import tests  # noqa: F401 — keeps the machine's settings out
import ast
import base64
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

import platforms
from core import bus, busd, busstate, reducer
from tests import winsafe

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB = os.path.join(REPO, "bin", "hub")
LIB = os.environ.get("HUB_TEST_PYLIB") or os.path.join(REPO, ".pylib")
if os.path.isdir(LIB) and LIB not in sys.path and "cryptography" not in sys.modules:
    sys.path.insert(0, LIB)
NOW = 1_800_000_000


def _lib():
    try:
        return bus.crypto(), None
    except bus.Unavailable as e:
        return None, str(e)


LIB_OK, LIB_WHY = _lib()
SSH = shutil.which("ssh-keygen")
need_lib = unittest.skipUnless(LIB_OK, f"no cryptography >= 47 for the bus ({LIB_WHY}): .pylib or HUB_TEST_PYLIB")
need_ssh = unittest.skipUnless(SSH, "no ssh-keygen")


class Folder:
    """The «folder» transport of the stand: an envelope is a file; nothing else (no network)."""

    def __init__(self):
        self.path = tempfile.mkdtemp(prefix="bus-wire-")
        self.n = 0

    def put(self, raw):
        self.n += 1
        with open(os.path.join(self.path, f"{self.n:06d}.env"), "wb") as f:
            f.write(raw)

    def take(self):
        out = []
        for name in sorted(os.listdir(self.path)):
            with open(os.path.join(self.path, name), "rb") as f:
                out.append(f.read())
            os.unlink(os.path.join(self.path, name))
        return out


class Stand:
    """A hub on the stand: its own settings folder (the keys) and the core state folded by the reducer; the core's
    decision on an envelope is bus.receive, its fact goes through reducer.reduce as hubd would write it."""

    def __init__(self, label):
        root = tempfile.mkdtemp(prefix="bus-hub-")
        self.label = label
        self.keys = bus.generate(os.path.join(root, "config", "bus"))
        self.st = reducer.empty()
        self.seq = 0
        self.fold("core.bus_enabled", bus.enabled_fact(self.keys, LIB_OK))
        self.wire = Folder()      # what this hub has put out (its transport takes it from here)

    def fold(self, type, data, now=NOW):
        self.seq += 1
        ev = {"seq": self.seq, "ts": now, "type": type, "entity": None, "data": data, "src": "core"}
        self.st, actions, why = reducer.reduce(self.st, ev)
        assert why is None, (type, why)
        return actions

    def card(self, now=NOW):
        return bus.make_card(self.keys, self.label, now)

    def accept(self, other, name, status="known", now=NOW):
        """The owner accepted the other hub — as on S0–S2's way, «known» only with the code checked (verified)."""
        info = bus.read_card(other.card(now), now)
        self.fold("core.bus_contact_added", bus.contact_fact(info, name, status, verified=status == "known"))

    def contact(self, name):
        return self.st["bus"]["contacts"][name]

    def send(self, to, text="hello", now=NOW, **kw):
        mid, raw = bus.seal(self.keys, self.contact(to), bus.message("maps", text, **kw), now)
        self.wire.put(raw)
        return mid, raw

    def take(self, raw, now=NOW):
        v = bus.receive(self.st["bus"], self.keys, raw, now)
        if v["fact"]:
            self.fold(*v["fact"], now=now)
        if v["reply"]:
            self.wire.put(v["reply"])
        return v

    def take_from(self, other, now=NOW):
        return [self.take(raw, now) for raw in other.wire.take()]


def pair():
    a, b = Stand("Hub A"), Stand("Hub B")
    a.accept(b, "bee")
    b.accept(a, "ann")
    return a, b


@need_lib
class Box(unittest.TestCase):
    """HPKE base mode, DHKEM(X25519, HKDF-SHA256) + HKDF-SHA256 + ChaCha20-Poly1305, `info` binds v, from, to, id, ts,
    exp; the plain text is padded to a size bucket."""

    def setUp(self):
        x = LIB_OK.x25519
        self.sk = x.X25519PrivateKey.generate()
        self.pk = self.sk.public_key().public_bytes_raw()
        self.head = {"v": 1, "from": "hb1-" + "a" * 26, "to": "hb1-" + "b" * 26, "id": "1" * 32, "ts": NOW,
                     "exp": NOW + 3600}
        self.info = bus.info_of(self.head)

    def test_round_trip(self):
        for n in (0, 1, 1023, 1024, 65536):
            plain = os.urandom(n)
            self.assertEqual(bus.open_box(LIB_OK, self.sk, bus.seal_box(LIB_OK, self.pk, plain, self.info), self.info), plain)

    def test_tampering_another_info_another_key_and_a_zero_point_are_bad_box(self):
        blob = bus.seal_box(LIB_OK, self.pk, b"hello", self.info)
        self.assertNotEqual(blob, bus.seal_box(LIB_OK, self.pk, b"hello", self.info))   # a fresh ephemeral key each time
        for where in (0, 40, len(blob) - 1):       # the enc key, the cipher text, the tag
            bad = bytearray(blob)
            bad[where] ^= 1
            with self.assertRaises(bus.Error) as e:
                bus.open_box(LIB_OK, self.sk, bytes(bad), self.info)
            self.assertEqual(e.exception.code, "bad_box")
        for field in ("v", "from", "to", "id", "ts", "exp"):
            other = dict(self.head, **{field: self.head[field] + 1 if isinstance(self.head[field], int)
                                       else self.head[field][:-1] + "c"})
            with self.assertRaises(bus.Error, msg=field):
                bus.open_box(LIB_OK, self.sk, blob, bus.info_of(other))
        stranger = LIB_OK.x25519.X25519PrivateKey.generate()
        with self.assertRaises(bus.Error):
            bus.open_box(LIB_OK, stranger, blob, self.info)
        with self.assertRaises(bus.Error):
            bus.open_box(LIB_OK, self.sk, bytes(32) + blob[32:], self.info)

    def test_the_size_is_the_bucket_not_the_text(self):
        sizes = {len(bus.seal_box(LIB_OK, self.pk, os.urandom(n), self.info)) for n in (0, 10, 500, 1019)}
        self.assertEqual(sizes, {32 + 1024 + 16})
        self.assertEqual(len(bus.seal_box(LIB_OK, self.pk, os.urandom(1021), self.info)), 32 + 2048 + 16)
        msg = bus.encode_inner(bus.message("maps", "ok"))
        receipt = bus.encode_inner({"kind": "receipt", "of": "2" * 32, "status": "ok"})
        self.assertEqual(len(bus.pad(msg)), len(bus.pad(receipt)))

    def test_a_padding_with_a_tail_or_a_long_length_is_bad_box(self):
        p = bus.pad(b"abc")
        for bad in (p[:-1] + b"\1", (5000).to_bytes(4, "big") + p[4:]):
            with self.assertRaises(bus.Error):
                bus.unpad(bad)

    def test_the_self_check_fails_on_a_broken_library(self):
        self.assertIsNone(bus.selfcheck(LIB_OK))

        class Lenient:      # a decrypt that accepts anything: the self-check must catch it
            def __init__(self, s):
                self.s = s

            def encrypt(self, *a, **kw):
                return self.s.encrypt(*a, **kw)

            def decrypt(self, blob, sk, info=None):
                return bus.pad(b"x")
        broken = types.SimpleNamespace(**{k: getattr(LIB_OK, k) for k in ("hpke", "x25519", "InvalidTag", "version")},
                                       suite=Lenient(LIB_OK.suite))
        self.assertIsNotNone(bus.selfcheck(broken))

    def test_another_hpke_implementation_agrees_if_there_is_one(self):
        try:
            import pyhpke  # noqa: F401
        except ImportError:
            self.skipTest("no second HPKE implementation on this machine")


@need_ssh
class Signature(unittest.TestCase):
    """`ssh-keygen -Y` on this OS — needs only OpenSSH, not the library: a key that ssh-keygen made in a fresh folder
    (its own permissions; on Windows its ACL), a signature through stdin, a check against one allowed signer."""

    def test_sign_and_verify(self):
        folder = os.path.join(tempfile.mkdtemp(prefix="bus-ssh-"), "bus")
        os.makedirs(folder)
        key = os.path.join(folder, "key")
        subprocess.run([SSH, "-q", "-t", "ed25519", "-N", "", "-C", "t", "-f", key], check=True, capture_output=True)
        with open(key + ".pub", encoding="ascii") as f:
            line = f.read()
        pub = bus._ed25519_raw(line)
        other = os.path.join(folder, "other")
        subprocess.run([SSH, "-q", "-t", "ed25519", "-N", "", "-C", "t", "-f", other], check=True, capture_output=True)
        data = json.dumps({"x": "café", "n": 1}, ensure_ascii=False).encode()
        sig = bus.sign(key, data)
        self.assertTrue(sig.startswith(bus.SIG_HEAD))
        self.assertTrue(bus.verify(pub, data, sig))
        big = b"x" * 110000                       # OpenSSH for Windows hung on stdin over ~8 KB: a whole envelope's head
        self.assertTrue(bus.verify(pub, big, bus.sign(key, big)))
        self.assertFalse(bus.verify(pub, data + b" ", sig))                    # another text
        self.assertFalse(bus.verify(pub, data, bus.sign(other, data)))         # another key
        code, git_sig, _ = bus._keygen(["-Y", "sign", "-f", key, "-n", "git"], bus.signed_bytes(data), folder)
        self.assertEqual(code, 0)
        self.assertFalse(bus.verify(pub, data, git_sig.decode()))              # another namespace (git, ssh)
        self.assertEqual(bus.openssh_pub(pub).split()[1], line.split()[1])


class SignatureFiles(unittest.TestCase):
    def test_the_signature_and_its_signer_reach_ssh_keygen_with_lf_only(self):
        # Windows text mode wrote them with CRLF: ssh-keygen of Git for Windows read no signature ("missing header") and
        # the bus failed its self-check; OpenSSH for Windows takes both, so only the ssh-keygen first in PATH decided
        seen = {}

        def keygen(args, data, folder):
            for name in ("allowed", "sig"):
                with open(os.path.join(folder, name), "rb") as f:
                    seen[name] = f.read()
            return 0, b"", ""
        sig = "-----BEGIN SSH SIGNATURE-----\nAAAA\n-----END SSH SIGNATURE-----\n"
        with mock.patch.object(bus, "_keygen", keygen):
            self.assertTrue(bus.ssh_verify(bytes(32), b"x", sig, bus.NS))
        self.assertEqual(seen["sig"], sig.encode("ascii"))
        self.assertNotIn(b"\r", seen["allowed"])

    def test_ssh_keygen_never_goes_to_an_ssh_agent(self):
        # `-Y sign` asks the SSH agent first: a password manager's agent (Bitwarden, 1Password) holds the default pipe
        # or socket and does not answer, ssh-keygen hung 30 s and the invite's "hub keys" step failed (Windows, 16.11)
        seen = {}

        def run(args, timeout, **kw):
            seen.update(kw.get("env") or {"inherited": True})
            return subprocess.CompletedProcess(args, 0, b"sig", None)
        env = {"SSH_AUTH_SOCK": r"\\.\pipe\openssh-ssh-agent", "SSH_AGENT_PID": "4242", "HUB_MARK": "kept"}
        with mock.patch.dict(os.environ, env), mock.patch.object(platforms.current, "run", run), \
                mock.patch.object(bus, "_ssh", lambda: "ssh-keygen"), tempfile.TemporaryDirectory() as d:
            self.assertEqual(bus._keygen(["-Y", "sign", "-f", "k", "-n", bus.NS], b"x", d)[0], 0)
        self.assertEqual(seen.get("SSH_AUTH_SOCK"), "")
        self.assertNotIn("SSH_AGENT_PID", seen)
        self.assertEqual(seen.get("HUB_MARK"), "kept")              # the rest of the environment is the same

    @need_ssh
    @unittest.skipIf(os.name == "nt", "a unix socket for the agent")
    def test_a_real_signature_does_not_wait_for_a_silent_agent(self):
        # the same on macOS and Linux: SSH_AUTH_SOCK names an agent that takes the connection and never answers —
        # the real ssh-keygen waited for it until the 30 s timeout
        import socket
        with tempfile.TemporaryDirectory() as d:
            key = os.path.join(d, "key")
            self.assertEqual(bus._keygen(["-q", "-t", "ed25519", "-N", "", "-C", "test", "-f", key], b"", d)[0], 0)
            srv, held, done = socket.socket(socket.AF_UNIX), [], threading.Event()
            self.addCleanup(srv.close)
            srv.bind(os.path.join(d, "agent"))
            srv.listen(4)
            srv.settimeout(0.2)   # closing the socket does not wake a blocked accept on Linux: the loop looks at done

            def serve():
                while not done.is_set():
                    try:
                        held.append(srv.accept()[0])   # reads nothing, answers nothing
                    except TimeoutError:
                        continue
                    except OSError:
                        return
            server = threading.Thread(target=serve, daemon=True)
            server.start()
            try:
                with mock.patch.dict(os.environ, {"SSH_AUTH_SOCK": os.path.join(d, "agent")}):
                    sig = bus.ssh_sign(key, b"x", bus.NS)
            finally:
                done.set()
                server.join(5)
                for c in held:
                    c.close()
            self.assertIn("BEGIN SSH SIGNATURE", sig)   # the agent never answers: only a signature made without it gets here
            self.assertEqual(held, [])                  # ... and ssh-keygen never even connected to it

    @need_ssh
    @unittest.skipUnless(os.name == "nt", "the agent's default pipe of OpenSSH for Windows")
    def test_a_real_signature_on_windows_does_not_ask_the_agent_s_default_pipe(self):
        # OpenSSH for Windows asks \\.\pipe\openssh-ssh-agent when SSH_AUTH_SOCK is not set (a password manager's agent
        # holds it); an empty SSH_AUTH_SOCK, as the core gives it, is no agent at all (winstand: unset — it waited, empty —
        # signed at once, never connected)
        from multiprocessing.connection import Client, Listener
        pipe = r"\\.\pipe\openssh-ssh-agent"
        with tempfile.TemporaryDirectory() as d:
            key = os.path.join(d, "key")
            self.assertEqual(bus._keygen(["-q", "-t", "ed25519", "-N", "", "-C", "test", "-f", key], b"", d)[0], 0)
            try:
                listener = Listener(address=pipe, family="AF_PIPE")
            except OSError:
                self.skipTest("an SSH agent of this machine holds the default pipe")
            held, done = [], threading.Event()

            def serve():
                while not done.is_set():
                    try:
                        held.append(listener.accept())   # reads nothing, answers nothing
                    except OSError:
                        return
            server = threading.Thread(target=serve, daemon=True)
            server.start()
            try:
                with mock.patch.dict(os.environ):
                    os.environ.pop("SSH_AUTH_SOCK", None)
                    sig = bus.ssh_sign(key, b"x", bus.NS)
            finally:
                made = len(held)
                done.set()
                with contextlib.suppress(OSError):
                    Client(pipe, family="AF_PIPE").close()   # wakes the accept that waits
                server.join(5)
                listener.close()
                for c in held:
                    c.close()
            self.assertIn("BEGIN SSH SIGNATURE", sig)
            self.assertEqual(made, 0)   # ssh-keygen never connected to the pipe


@need_lib
@need_ssh
class KeysAndCard(unittest.TestCase):
    def test_fingerprint_code_and_private_files(self):
        k = bus.generate(os.path.join(tempfile.mkdtemp(prefix="bus-k-"), "bus"))
        self.assertRegex(k.fp, r"^hb1-[a-z2-7]{26}$")
        self.assertRegex(bus.code(k.fp), r"^\d{4}(-\d{4}){4}$")
        self.assertEqual(bus.code(k.fp), bus.code(k.fp))
        self.assertNotEqual(bus.fingerprint(k.sign_pub, bytes(32)), k.fp)   # the exchange key is in the fingerprint
        again = bus.load(k.folder)
        self.assertEqual((again.fp, again.enc_pub), (k.fp, k.enc_pub))
        if os.name != "nt":
            for name in ("key", "enc.key"):
                self.assertEqual(os.stat(os.path.join(k.folder, name)).st_mode & 0o777, 0o600, name)
        with self.assertRaises(bus.Error):    # keys are never overwritten
            bus.generate(k.folder)

    def test_a_half_key_set_is_not_taken_and_a_failed_generation_leaves_nothing(self):
        folder = os.path.join(tempfile.mkdtemp(prefix="bus-half-"), "bus")
        with mock.patch.object(LIB_OK.x25519.X25519PrivateKey, "generate", side_effect=OSError("no entropy")):
            with self.assertRaises(OSError):
                bus.generate(folder)
        self.assertFalse(os.path.exists(folder))
        self.assertEqual(os.listdir(os.path.dirname(folder)), [])           # no half-made folder next to it either
        os.makedirs(folder)
        k = bus.generate(folder)                                            # an empty folder is taken
        os.unlink(os.path.join(k.folder, "enc.key"))
        with self.assertRaises(bus.Error) as e:
            bus.load(folder)
        self.assertEqual(e.exception.code, "keys_broken")
        keys, why = bus.ready_keys(config_dir=os.path.dirname(folder), make=True)
        self.assertEqual((keys, why[0]), (None, "keys_broken"))           # never «made anew» over a half set

    def test_other_files_in_the_folder_and_a_left_work_folder(self):
        root = tempfile.mkdtemp(prefix="bus-other-")
        folder = os.path.join(root, "bus")
        os.makedirs(folder)
        with open(os.path.join(folder, ".DS_Store"), "w") as f:
            f.write("x")
        os.makedirs(os.path.join(root, "bus.new-old"))                   # a killed process's work folder
        with open(os.path.join(root, "bus.new-old", "enc.key"), "wb") as f:
            f.write(bytes(32))
        k = bus.generate(folder)
        self.assertEqual(bus.load(folder).fp, k.fp)
        self.assertEqual(sorted(os.listdir(root)), ["bus"])

    def test_a_card_is_signed_and_checked(self):
        a = Stand("Hub A")
        card = a.card()
        info = bus.read_card(card, NOW)
        self.assertEqual((info["fp"], info["label"], info["code"]), (a.keys.fp, "Hub A", bus.code(a.keys.fp)))
        self.assertLess(len(card), 2000)
        raw = json.loads(base64.b64decode(card[len(bus.CARD_PREFIX):]))
        head = json.loads(raw["head"])
        for change in ({"label": "Hub X"}, {"enc_pub": base64.b64encode(bytes(32)).decode()}):
            forged = dict(raw, head=json.dumps(dict(head, card=dict(head["card"], **change)), separators=(",", ":")))
            text = bus.CARD_PREFIX + base64.b64encode(json.dumps(forged).encode()).decode()
            with self.assertRaises(bus.Error, msg=change) as e:
                bus.read_card(text, NOW)
            self.assertEqual(e.exception.code, "bad_signature")
        with self.assertRaises(bus.Error):
            bus.read_card(card, NOW + 8 * 86400)        # expired


@need_lib
@need_ssh
class Receive(unittest.TestCase):
    """The checks on receipt (spec §5.7) on two hubs of the stand through a folder."""

    def test_a_message_goes_through_the_folder_and_the_exchange_falls_silent(self):
        a, b = pair()
        mid, raw = a.send("bee", "the schema of /v1/items, please")
        self.assertNotIn(b"the schema", raw)                 # a check for an accidental leak, not a proof
        self.assertNotIn(b"maps", raw)
        (v,) = b.take_from(a)
        self.assertEqual(v["code"], "ok")
        typ, data = v["fact"]
        self.assertEqual((typ, data["text"], data["agent_from"], data["contact"], data["status"]),
                         ("core.bus_received", "the schema of /v1/items, please", "maps", "ann", "ok"))
        (receipt,) = b.wire.take()
        r = a.take(receipt)
        self.assertEqual((r["code"], r["fact"][0], r["fact"][1]["of"]), ("receipt", "core.bus_delivered", mid))
        self.assertIsNone(r["reply"])                         # a receipt does not get a receipt
        self.assertEqual(b.take_from(a), [])                  # silence
        dup = a.take(receipt, NOW + 700)                      # a relay repeats the receipt: still no answer, no chain
        self.assertEqual((dup["code"], dup["reply"]), ("dup", None))

    def test_a_lost_receipt_comes_back_on_a_repeat_without_a_chain_or_a_second_delivery(self):
        a, b = pair()
        mid, raw = a.send("bee")
        b.take_from(a)
        b.wire.take()                                         # the receipt is lost on the way
        again = b.take(raw, NOW + 30)                         # the transport repeats at once: the receipt is not resent
        self.assertEqual((again["code"], again["reply"]), ("dup", None))
        again = b.take(raw, NOW + 700)                        # ten minutes later: the receipt again, still no delivery
        self.assertEqual(again["code"], "dup")
        self.assertIsNotNone(again["reply"])
        self.assertEqual(sum(1 for e in [again] if e["fact"][0] == "core.bus_received"), 0)
        (r,) = a.take_from(b, NOW + 700)
        self.assertEqual((r["fact"][0], r["fact"][1]["of"]), ("core.bus_delivered", mid))
        self.assertEqual(b.take_from(a, NOW + 700), [])

    def test_forgery_another_key_another_to_and_a_replay(self):
        a, b = pair()
        c = Stand("Hub C")
        c.accept(b, "bee")
        b.accept(c, "cee")
        _, raw = a.send("bee", "text")
        env = json.loads(raw)
        head = json.loads(env["head"])
        cases = {}
        tampered = dict(head, ct=head["ct"][:-8] + ("A" if head["ct"][-8] != "A" else "B") + head["ct"][-7:])
        cases["text changed"] = (dict(env, head=json.dumps(tampered, separators=(",", ":"))), "bad_signature")
        # C signs A's very head: the key comes from B's table by `from`, so only the signature can tell — and it does
        cases["A's head signed by C"] = ({"head": env["head"], "sig": bus.sign(c.keys.sign_key, env["head"].encode())},
                                         "bad_signature")
        forged = json.dumps(dict(head, id="3" * 32), separators=(",", ":"))
        cases["changed and signed by another key"] = ({"head": forged, "sig": bus.sign(c.keys.sign_key, forged.encode())},
                                                      "bad_signature")
        # A's cipher text re-signed by C as its own: the signature is C's and valid, the decryption binds `from`
        own = json.dumps(dict(head, **{"from": c.keys.fp}), separators=(",", ":"))
        cases["ciphertext of A signed by C"] = ({"head": own, "sig": bus.sign(c.keys.sign_key, own.encode())}, "bad_box")
        for label, (e, want) in cases.items():
            with self.subTest(label), mock.patch.object(bus, "open_box", wraps=bus.open_box) as spy:
                v = b.take(json.dumps(e).encode())
                self.assertEqual(v["code"], want)
                self.assertIsNone(v["reply"])                 # no receipt: the sender does not learn we see it
                if want == "bad_signature":
                    spy.assert_not_called()                   # nothing is decrypted before the signature
        c.accept(a, "ann")
        _, to_c = a.send("bee")                               # an envelope for B delivered to C: `to` is not C
        self.assertEqual(c.take(to_c)["code"], "bad_signature")
        self.assertEqual(b.take(raw)["code"], "ok")
        self.assertEqual(b.take(raw)["code"], "dup")          # a replay: no second delivery

    def test_window_unknown_blocked_pending_and_muted(self):
        a, b = pair()
        _, old = a.send("bee", now=NOW - 8 * 86400)
        self.assertEqual(b.take(old)["code"], "stale")
        _, future = a.send("bee", now=NOW + 3600)
        self.assertEqual(b.take(future)["code"], "stale")
        c = Stand("Hub C")
        c.accept(b, "bee")
        _, raw = c.send("bee")
        v = b.take(raw)
        self.assertEqual((v["code"], v["fact"][0], v["fact"][1]["hub"]), ("unknown_hub", "core.bus_rejected", c.keys.fp))
        self.assertNotIn("text", v["fact"][1])
        for status in ("pending", "blocked"):
            b.fold("core.bus_contact_set", {"name": "ann", "status": status})
            _, raw = a.send("bee")
            self.assertEqual(b.take(raw)["code"], "blocked", status)
        b.fold("core.bus_contact_set", {"name": "ann", "status": "muted"})
        _, raw = a.send("bee", "look\u200bhere")              # muted: kept, no "suspicious" card
        v = b.take(raw)
        self.assertEqual((v["code"], v["fact"][1]["status"]), ("muted", "muted"))
        self.assertIsNotNone(v["reply"])

    def test_envelopes_of_a_pending_or_blocked_contact_spend_nothing(self):
        # they are refused before the signature: a stranger who writes its fingerprint must not use up its hour
        a, b = pair()
        b.fold("core.bus_contact_set", {"name": "ann", "status": "pending"})
        env = json.loads(a.send("bee")[1])
        lines = env["sig"].splitlines()
        lines[2] = lines[2][:-1] + ("A" if lines[2][-1] != "A" else "B")    # a junk signature over the same head
        forged = json.dumps({"head": env["head"], "sig": "\n".join(lines) + "\n"}).encode()
        for _ in range(5):
            self.assertEqual(b.take(forged)["code"], "blocked")
        self.assertEqual(busstate.counts(b.st["bus"], a.keys.fp, NOW), (0, 0))

    def test_hidden_characters_in_the_label_are_marked_too(self):
        a, b = pair()
        _, raw = a.send("bee", "clean", label="Hub\ue000A")
        v = b.take(raw)
        self.assertEqual((v["code"], v["fact"][1]["status"]), ("ok", "ok"))   # the liaison weighs it, no owner's card
        self.assertTrue(v["fact"][1]["finding"])

    def test_hidden_characters_are_marked(self):
        a, b = pair()
        for text in ("a\u202eb", "tag\U000e0041", "x\x07", "sel\ufe0f"):
            _, raw = a.send("bee", text)
            v = b.take(raw)
            self.assertEqual((v["code"], v["fact"][1]["status"]), ("ok", "ok"), repr(text))
            self.assertTrue(v["fact"][1]["finding"], repr(text))
        _, raw = a.send("bee", "lines\nand\ttabs are text")
        self.assertEqual(b.take(raw)["code"], "ok")

    def test_a_contact_is_not_limited_by_rate_and_three_bad_boxes_quarantine(self):
        a, b = pair()
        got = [b.take(a.send("bee")[1])["code"] for _ in range(150)]   # more than any hour's limit of the old rule
        self.assertEqual(set(got), {"ok"})
        a2, b2 = pair()

        def broken():   # a valid signature over a box that does not open
            mid, raw = a2.send("bee")
            env = json.loads(raw)
            head = json.loads(env["head"])
            ct = base64.b64decode(head["ct"])
            head = json.dumps(dict(head, ct=base64.b64encode(ct[:40] + bytes([ct[40] ^ 1]) + ct[41:]).decode()),
                              separators=(",", ":"))
            return json.dumps({"head": head, "sig": bus.sign(a2.keys.sign_key, head.encode())}).encode()
        self.assertEqual([b2.take(broken())["code"] for _ in range(3)], ["bad_box"] * 3)
        with mock.patch.object(bus, "open_box", wraps=bus.open_box) as spy:
            self.assertEqual(b2.take(a2.send("bee")[1])["code"], "quarantine")
            spy.assert_not_called()
        self.assertTrue(b2.contact("ann").get("quarantine"))

    def test_strict_head_both_parses_reject_alike(self):
        a, b = pair()
        _, raw = a.send("bee")
        env = json.loads(raw)
        head = env["head"]
        h = json.loads(head)
        bad_heads = {
            "repeated key": head[:-1] + ',"id":"' + "4" * 32 + '"}',
            "extra key": json.dumps(dict(h, x=1), separators=(",", ":")),
            "missing key": json.dumps({k: v for k, v in h.items() if k != "exp"}, separators=(",", ":")),
            "loose base64": json.dumps(dict(h, ct=h["ct"].rstrip("=") + "\n"), separators=(",", ":")),
            "float": json.dumps(dict(h, ts=float(h["ts"])), separators=(",", ":")),
            "bool": json.dumps(dict(h, v=True), separators=(",", ":")),
            "huge": json.dumps(dict(h, exp=2 ** 60), separators=(",", ":")),
            "too long": json.dumps(dict(h, exp=h["ts"] + 8 * 86400), separators=(",", ":")),
        }
        # the same bytes in a non-canonical spelling (unused low bits of the last character set): only the canonical
        # check tells it from the signed original
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
        ct = h["ct"]
        assert ct.endswith("==")
        i = alphabet.index(ct[-3])
        loose = ct[:-3] + alphabet[i - i % 16 + (i % 16 + 1) % 16] + "=="
        assert base64.b64decode(loose) == base64.b64decode(ct)
        bad_heads["same bytes, loose spelling"] = json.dumps(dict(h, ct=loose), separators=(",", ":"))
        for label, text in bad_heads.items():
            with self.subTest(label):
                with self.assertRaises(bus.Error) as e:
                    bus.parse_head(text)               # the untrusted pre-parse (to pick the key)…
                self.assertEqual(e.exception.code, "bad_format")
                signed = {"head": text, "sig": bus.sign(a.keys.sign_key, text.encode())}
                self.assertEqual(b.take(json.dumps(signed).encode())["code"], "bad_format")   # …and the whole check
        self.assertEqual(b.take(raw + b" ")["code"], "ok")      # whitespace around the outer JSON is not a change

    def test_the_largest_message_fits_in_the_envelope_limit(self):
        a, b = pair()
        text = '"' * 32000        # the worst escaping: every quote doubles in the inner JSON and again in the head
        mid, raw = a.send("bee", text)
        self.assertLessEqual(len(raw), bus.ENVELOPE_MAX)
        self.assertEqual(b.take(raw)["code"], "ok")
        with self.assertRaises(bus.Error):
            a.send("bee", "x" * (bus.INNER_MAX + 1))


@need_lib
@need_ssh
class Folding(unittest.TestCase):
    def test_contact_record_carries_an_empty_person(self):
        a, b = pair()
        c = b.contact("ann")
        self.assertEqual((c["person"], c["status"], c["fp"]), (None, "known", a.keys.fp))
        self.assertNotIn("may_write", c)    # no table of rights any more

    def test_keys_are_not_in_the_state_or_the_facts(self):
        a, b = pair()
        a.take_from(b)
        b.take(a.send("bee", "x")[1])
        dump = json.dumps([a.st, b.st])
        for k in (a.keys, b.keys):
            with open(os.path.join(k.folder, "key"), encoding="ascii") as f:
                body = "".join(f.read().splitlines()[1:-1])
            with open(os.path.join(k.folder, "enc.key"), "rb") as f:
                enc = f.read()
            for secret in (body[:40], base64.b64encode(enc).decode(), enc.hex()):
                self.assertNotIn(secret, dump)

    def test_seen_entries_are_pruned_after_their_window(self):
        a, b = pair()
        b.take(a.send("bee")[1])
        self.assertEqual(len(b.st["bus"]["seen"]), 1)
        b.take(a.send("bee", now=NOW + 9 * 86400)[1], now=NOW + 9 * 86400)
        self.assertEqual(len(b.st["bus"]["seen"]), 1)


class Facts(unittest.TestCase):
    """Who may: the owner switches the bus on and changes contacts (strict guards.from_owner); a session, a module, a
    plugin — no; core.bus_* from the spool become spool.refused (only the core writes them)."""

    def reduce(self, st, type, data, seq=1):
        ev = {"seq": seq, "ts": NOW, "type": type, "entity": None, "data": data, "src": "spool"}
        return reducer.reduce(st, ev)

    def test_only_the_owner(self):
        st = reducer.empty()
        st["sessions"]["hub"] = {"name": "hub", "state": "idle", "kind": "hub"}
        for who in ({"channel": "session", "owner": False, "sender": "hub"},
                    {"channel": "session", "owner": True, "sender": "maps"},
                    {"channel": "plugin", "owner": False, "sender": "mod:x"},
                    {"channel": "mod:x", "owner": True, "sender": "mod:x"}):     # a module handler's owner request
            for type, data in (("bus.on_requested", {}), ("bus.off_requested", {}),
                               ("bus.contact_requested", {"op": "set", "name": "ann", "status": "known"})):
                _, actions, why = self.reduce(st, type, {**data, **who})
                self.assertTrue(why, (type, who))
                self.assertEqual(actions, [])
        _, actions, why = self.reduce(st, "bus.on_requested", {"channel": "cli", "owner": True, "label": "Hub A"})
        self.assertIsNone(why)
        self.assertEqual([a["do"] for a in actions], ["bus_on"])

    def test_a_label_that_no_card_would_carry_is_refused(self):
        for label in ("My\thub", "Hub\u200b", "", "x" * 101):
            _, actions, why = self.reduce(reducer.empty(), "bus.on_requested",
                                          {"channel": "cli", "owner": True, "label": label})
            self.assertTrue(why, repr(label))

    def test_contact_changes_need_the_bus_on(self):
        _, _, why = self.reduce(reducer.empty(), "bus.contact_requested",
                                {"op": "add", "name": "ann", "card": "x", "channel": "cli", "owner": True})
        self.assertIn("hub bus on", why)

    def test_core_facts_from_the_spool_are_refused(self):
        from core.daemon import Hub
        h = Hub(tempfile.mkdtemp(prefix="bus-spool-"), types.SimpleNamespace())
        for type in ("core.bus_received", "core.bus_contact_added", "core.bus_enabled"):
            path = os.path.join(tempfile.mkdtemp(prefix="bus-sp-"), "x.json")
            h.spooled(path, {"type": type, "entity": None, "data": {"channel": "session", "sender": "maps",
                                                                     "owner": False, "fp": "hb1-x"}, "src": "session"})
        self.assertNotIn("bus", h.state)

    def test_an_old_core_does_not_crash_on_bus_facts(self):
        # a hub rolled back to a release without the bus: unknown types are rejected one by one, the fold goes on
        st = reducer.empty()
        st2, _, why = self.reduce(st, "core.bus_something_new", {})
        self.assertTrue(why)


@need_lib
@need_ssh
class OwnerPath(unittest.TestCase):
    """`hub bus on` and `hub bus contact …` through the core: the owner's request, the core's decision as a core.bus_*
    fact (keys made by the core, the card checked by it)."""

    def hub(self):
        from core.daemon import Hub
        root = tempfile.mkdtemp(prefix="bus-own-")
        self.config = os.path.join(root, "config")
        env = mock.patch.dict(os.environ, {"HUB_CONFIG_DIR": self.config})
        env.start()
        self.addCleanup(env.stop)
        h = Hub(os.path.join(root, "state"), types.SimpleNamespace())
        return h

    def test_on_makes_the_keys_then_contacts_by_card_and_code(self):
        h = self.hub()
        own = {"channel": "cli", "owner": True}
        h.record("bus.on_requested", None, {"label": "Hub A", "req": "r1", **own})
        me = h.state["bus"]
        self.assertTrue(me["enabled"])
        self.assertEqual(me["last"]["code"], "ok")
        keys = bus.load(bus.key_dir(self.config))
        self.assertEqual(me["me"]["fp"], keys.fp)
        with open(os.path.join(h.state_dir, "journal.jsonl"), encoding="utf-8") as f:
            journal = f.read()
        with open(os.path.join(keys.folder, "enc.key"), "rb") as f:
            self.assertNotIn(base64.b64encode(f.read()).decode(), journal)
        other = Stand("Hub B")
        card = other.card(NOW)
        with mock.patch.object(h, "clock", lambda: NOW):
            h.record("bus.contact_requested", None, {"op": "add", "name": "bee", "card": card, "req": "r2", **own})
            self.assertEqual(h.state["bus"]["contacts"]["bee"]["status"], "pending")
            h.record("bus.contact_requested", None, {"op": "set", "name": "bee", "status": "known",
                                                     "code": "0000-0000-0000-0000-0000", "req": "r3", **own})
            self.assertEqual(h.state["bus"]["contacts"]["bee"]["status"], "pending")
            self.assertEqual(h.state["bus"]["last"]["code"], "code_mismatch")
            h.record("bus.contact_requested", None, {"op": "set", "name": "bee", "status": "known",
                                                     "code": bus.code(other.keys.fp), "req": "r4", **own})
            self.assertEqual(h.state["bus"]["contacts"]["bee"]["status"], "known")
            h.record("bus.contact_requested", None, {"op": "add", "name": "bee2", "card": card, "req": "r5", **own})
            self.assertNotIn("bee2", h.state["bus"]["contacts"])      # the same hub twice — no
            h.record("bus.contact_requested", None, {"op": "remove", "name": "bee", "req": "r6", **own})
            self.assertNotIn("bee", h.state["bus"]["contacts"])
        h.record("bus.off_requested", None, {"req": "r7", **own})
        self.assertFalse(h.state["bus"]["enabled"])
        h.record("bus.on_requested", None, {"req": "r8", **own})
        self.assertEqual(h.state["bus"]["me"]["fp"], keys.fp)           # on again — the same identity

    def test_a_red_self_check_does_not_switch_the_bus_on(self):
        h = self.hub()
        with mock.patch.object(bus, "selfcheck", return_value="decrypt accepts a forgery"):
            h.record("bus.on_requested", None, {"channel": "cli", "owner": True, "req": "r1"})
        self.assertFalse((h.state.get("bus") or {}).get("enabled"))
        self.assertEqual(h.state["bus"]["last"]["code"], "selfcheck")

    def test_the_core_checks_itself_at_start(self):
        h = self.hub()
        h.record("bus.on_requested", None, {"channel": "cli", "owner": True, "req": "r1"})
        with mock.patch.object(bus, "selfcheck", return_value="broken"):
            h.bus_start()
        self.assertFalse(h.state["bus"]["crypto_ready"])
        h.bus_start()
        self.assertTrue(h.state["bus"]["crypto_ready"])


@need_lib
@need_ssh
@unittest.skipIf(os.name == "nt" or not os.path.isdir(LIB), "the library for <state>/pylib is linked from .pylib (POSIX)")
class CommandsOnACore(unittest.TestCase):
    """The owner's terminal against a running core: `hub bus …` puts the request into the spool, the core (here — a Hub
    draining the spool in a thread) decides; the library comes from <state>/pylib, as after `hub bus on --install`."""

    def setUp(self):
        import threading
        from core.daemon import Hub
        root = tempfile.mkdtemp(prefix="bus-cli-")
        self.state, config = os.path.join(root, "state"), os.path.join(root, "config")
        os.makedirs(self.state)
        winsafe.symlink(self, os.path.realpath(LIB), os.path.join(self.state, "pylib"))
        env = mock.patch.dict(os.environ, {"HUB_CONFIG_DIR": config})
        env.start()
        self.addCleanup(env.stop)
        self.env = {k: v for k, v in os.environ.items() if k not in ("HUB_SESSION", "HUB_EXTENSION", "HUB_MODULE")}
        self.env.update(HUB_STATE_DIR=self.state, HUB_CONFIG_DIR=config, HUB_LANG="en", HUB_CLI_WAIT="20")
        hub = Hub(self.state, types.SimpleNamespace())
        stop = threading.Event()

        def drain():
            while not stop.is_set():
                hub.ingest()
                stop.wait(0.1)
        t = threading.Thread(target=drain, daemon=True)
        t.start()
        self.addCleanup(lambda: (stop.set(), t.join(5)))

    def hub(self, *args):
        r = subprocess.run([sys.executable, HUB, "bus", *args], env=self.env, capture_output=True, text=True, cwd=REPO,
                           timeout=120)
        return r.returncode, r.stdout, r.stderr

    def test_on_card_contact_off(self):
        self.assertIn("bus: off", self.hub()[1])
        rc, out, err = self.hub("on", "--label", "Hub A")
        self.assertEqual(rc, 0, out + err)
        self.assertRegex(out, r"fingerprint hb1-[a-z2-7]{26}")
        self.assertRegex(out, r"check code \d{4}(-\d{4}){4}")
        rc, card, hint = self.hub("card")
        self.assertEqual(rc, 0, card + hint)
        self.assertEqual(bus.read_card(card, time.time())["label"], "Hub A")
        self.assertIn("check code", hint)
        other = Stand("Hub B")
        theirs = bus.make_card(other.keys, "Hub B", time.time())
        rc, out, err = self.hub("contact", "add", theirs, "--as", "bee")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("not accepted yet", out)
        self.assertIn(bus.code(other.keys.fp), out)
        rc, out, _ = self.hub("contact", "set", "bee", "--status", "known", "--code", "1111-2222-3333-4444-5555")
        self.assertNotEqual(rc, 0)
        self.assertIn("does not match", out)
        rc, out, _ = self.hub("contact", "set", "bee", "--status", "known", "--code", bus.code(other.keys.fp))
        self.assertEqual(rc, 0, out)
        rc, out, _ = self.hub("contact", "ls")
        self.assertIn("bee · Hub B · known", out)
        self.assertIn("contacts: 1", self.hub("status")[1])
        rc, out, _ = self.hub("off")
        self.assertEqual(rc, 0, out)
        self.assertIn("bus: off", self.hub()[1])


class Waiting(unittest.TestCase):
    """The command waits for the core's decision in the journal (not by re-reading the big state), and a decision the
    fold refused is not reported as done."""

    def journal(self, events):
        path = os.path.join(tempfile.mkdtemp(prefix="bus-j-"), "journal.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")
        return path

    def test_waited_finds_after_the_mark_and_survives_a_missing_file(self):
        from core import journal
        path = self.journal([{"seq": 1, "type": "x", "data": {}}])
        start = journal.mark(path)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"seq": 2, "type": "core.bus_enabled", "data": {"of": 9}}) + "\n")
        self.assertEqual(journal.waited(path, start, lambda e: e["data"].get("of") == 9, 0)["seq"], 2)
        self.assertIsNone(journal.waited(path, start, lambda e: e["data"].get("of") == 8, 0.3))
        missing = os.path.join(tempfile.mkdtemp(prefix="bus-j-"), "journal.jsonl")
        self.assertIsNone(journal.waited(missing, journal.mark(missing), lambda e: True, 0.3))

    def test_a_refused_decision_is_not_ok(self):
        from core import buscli
        state = tempfile.mkdtemp(prefix="bus-w-")
        path = os.path.join(state, "journal.jsonl")
        open(path, "w").close()

        def put(type_, entity, data, say=True):
            with open(path, "a", encoding="utf-8") as f:
                for e in ({"seq": 5, "type": type_, "data": data},
                          {"seq": 6, "type": "core.bus_contact_set", "data": {"name": "ann", "of": 5}},
                          {"seq": 7, "type": "core.rejected", "data": {"of": 6, "reason": "no such bus contact"}}):
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
            return 5
        cli = buscli.Cli(put, lambda: {}, state, None, None, {})
        self.assertEqual(cli.ask("bus.contact_requested", {"op": "set"}), ("rejected", "no such bus contact"))


class WithoutTheLibrary(unittest.TestCase):
    """cryptography is needed only with the bus on: the core imports nothing of it, and `hub bus on` says why it
    refuses and how to install the library."""

    def test_the_core_does_not_import_the_library(self):
        code = ("import sys; import core.bus, core.busstate, core.reducer, core.daemon; "
                "print(any(m == 'cryptography' or m.startswith('cryptography.') for m in sys.modules))")
        out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "False", out.stderr)

    def fake_library(self, body):
        """A `cryptography` that shadows any real one for a child process (PYTHONPATH comes before site-packages)."""
        root = tempfile.mkdtemp(prefix="bus-fake-")
        os.makedirs(os.path.join(root, "cryptography"))
        with open(os.path.join(root, "cryptography", "__init__.py"), "w") as f:
            f.write(body)
        if "46" in body:
            info = os.path.join(root, "cryptography-46.0.5.dist-info")
            os.makedirs(info)
            with open(os.path.join(info, "METADATA"), "w") as f:
                f.write("Metadata-Version: 2.1\nName: cryptography\nVersion: 46.0.5\n")
        return root

    def test_on_without_the_library_refuses_with_the_command(self):
        for label, body in (("none", "raise ImportError('no')\n"), ("old", "__version__ = '46.0.5'\n")):
            with self.subTest(label):
                state = tempfile.mkdtemp(prefix="bus-nolib-")
                env = {k: v for k, v in os.environ.items() if k != "HUB_SESSION"}
                env.update(HUB_STATE_DIR=state, HUB_CONFIG_DIR=tempfile.mkdtemp(prefix="bus-nolib-cfg-"), HUB_LANG="en",
                           PYTHONPATH=self.fake_library(body))
                r = subprocess.run([sys.executable, HUB, "bus", "on"], env=env, capture_output=True, text=True, cwd=REPO)
                self.assertNotEqual(r.returncode, 0)
                out = r.stdout + r.stderr
                self.assertIn("cryptography", out)
                self.assertIn(os.path.join(state, "pylib"), out)
                self.assertIn("--target", out)
                self.assertIn("--install", out)
                spool = os.path.join(state, "spool")
                self.assertFalse(os.path.isdir(spool) and os.listdir(spool))     # nothing was asked of the core

    def test_the_install_command_replaces_an_old_copy(self):
        cmd = bus.install_command(tempfile.mkdtemp(prefix="bus-ic-"))
        self.assertIn("--upgrade", cmd)
        self.assertIn("--target", cmd)

    def test_a_card_without_the_library_in_the_terminal_says_how_to_get_it(self):
        from core import buscli
        import contextlib
        import io
        out = io.StringIO()
        st = {"bus": {"enabled": True, "me": {"fp": "hb1-" + "a" * 26}}}
        with mock.patch.object(bus, "load", side_effect=bus.Unavailable("no_lib", "no cryptography")), \
                contextlib.redirect_stdout(out), mock.patch.dict(os.environ, {"HUB_LANG": "en"}):
            rc = buscli.main(["card"], put=None, current=lambda: st, state_dir=tempfile.mkdtemp(prefix="bus-c-"))
        self.assertEqual(rc, 1)
        self.assertIn("--target", out.getvalue())

    def test_old_versions_are_refused(self):
        with mock.patch.object(bus, "_import", side_effect=lambda: types.SimpleNamespace(__version__="46.0.5")):
            bus._LIB = None
            with self.assertRaises(bus.Unavailable) as e:
                bus.crypto(tempfile.mkdtemp())
        bus._LIB = None
        self.assertIn("46.0.5", str(e.exception))

    def test_a_session_may_not_switch_it_on(self):
        env = dict(os.environ, HUB_SESSION="maps", HUB_STATE_DIR=tempfile.mkdtemp(prefix="bus-ses-"), HUB_LANG="en")
        r = subprocess.run([sys.executable, HUB, "bus", "on"], env=env, capture_output=True, text=True, cwd=REPO)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("owner", r.stdout + r.stderr)


class HealOnTheAppEnvironment(unittest.TestCase):
    """A hub of the one-line install (or the app) whose environment came without the library (runtime 1): the bus is
    on, the core's check at start says no_lib — the core puts the wheels the release pins for its platform into
    <state>/pylib itself (by their sha256, no pip), in the background, once per start, and the bus works without the
    owner's step; a failure is the reason in one line. Downloads here are a copy of a wheel the test made: no network."""

    FP = "hb1-" + "a" * 26
    FACT = {"fp": FP, "sign_pub": "c2lnbg==", "enc_pub": "ZW5j", "lib": "50.0.2"}

    def hub(self, app=True, platform="linux-x64"):
        from core import packaged
        from core.daemon import Hub
        self.root = tempfile.mkdtemp(prefix="bus-heal-")
        self.addCleanup(platforms.current.rmtree, self.root)
        env = mock.patch.dict(os.environ, {"HUB_CONFIG_DIR": os.path.join(self.root, "config")})
        env.start()
        self.addCleanup(env.stop)
        mark = mock.patch.object(packaged, "info", return_value={"format": "app", "runtime": 1, "platform": platform}
                                 if app else None)
        mark.start()
        self.addCleanup(mark.stop)
        h = Hub(os.path.join(self.root, "state"), types.SimpleNamespace())
        h.record("core.bus_enabled", None, dict(self.FACT))
        h.record("core.bus_key_failed", None, {"code": "no_lib", "reason": "no cryptography: No module named 'x'"})
        self.got = []
        return h

    def wheel(self, version="50.0.2", inside=None):
        import hashlib
        import zipfile
        path = os.path.join(self.root, f"cryptography-{version}-cp311-abi3-manylinux2014_x86_64.whl")
        info = f"cryptography-{version}.dist-info"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("cryptography/__init__.py", f'__version__ = "{version}"\n')
            z.writestr(f"{info}/METADATA", f"Metadata-Version: 2.4\nName: cryptography\nVersion: {inside or version}\n")
            z.writestr(f"{info}/WHEEL", "Wheel-Version: 1.0\n")
        with open(path, "rb") as f:
            sha = hashlib.sha256(f.read()).hexdigest()
        return path, {"key": "cryptography", "url": "https://files.example.test/" + os.path.basename(path),
                      "sha256": sha, "size": os.path.getsize(path), "name": os.path.basename(path), "version": version}

    def net(self, src=None, fail=None):
        """core/fetch.download: the wheel copied in as if downloaded, or the failure it raises."""
        from core import fetch

        def download(url, dest, sha, size, what, again, **kw):
            self.got.append((url, sha, size))
            if fail:
                raise fetch.Failed(*fail)
            shutil.copyfile(src, dest)
            return dest
        return mock.patch.object(fetch, "download", side_effect=download)

    def settle(self, h):
        """A tick starts the background, its end is told on the tick after it."""
        busd.tick(h)
        self.join()
        busd.tick(h)

    @staticmethod
    def join():
        for t in [t for t in threading.enumerate() if t.name == "bus-heal"]:
            t.join(10)

    def test_the_core_puts_the_pinned_wheel_in_itself_and_the_bus_works(self):
        h = self.hub()
        path, part = self.wheel()
        with mock.patch.object(busd, "_pinned", return_value=[part]), self.net(path), \
                mock.patch.object(bus, "start_check", return_value=[("core.bus_enabled", dict(self.FACT))]):
            self.settle(h)
            busd.tick(h)
        self.assertEqual(self.got, [(part["url"], part["sha256"], part["size"])])   # once, exactly the pin
        lib = bus.pylib(h.state_dir)
        self.assertTrue(os.path.isfile(os.path.join(lib, "cryptography", "__init__.py")))
        self.assertTrue(os.path.isfile(os.path.join(lib, "cryptography-50.0.2.dist-info", "METADATA")))
        self.assertFalse(os.path.exists(lib + ".new") or os.path.exists(lib + ".old"))
        self.assertFalse(os.path.exists(os.path.join(h.state_dir, "bus", "wheels")))   # the downloads are not kept
        self.assertTrue(h.state["bus"]["crypto_ready"])
        self.assertIsNone(h.state["bus"]["failed"])

    def test_no_network_is_one_line_with_its_reason_and_is_not_repeated_in_this_start(self):
        h = self.hub()
        _, part = self.wheel()
        with mock.patch.object(busd, "_pinned", return_value=[part]), self.net(fail=("broken", "the download broke")):
            self.settle(h)
            for _ in range(3):
                busd.tick(h)
        self.assertEqual(len(self.got), 1)
        self.assertFalse(h.state["bus"]["crypto_ready"])
        self.assertEqual(h.state["bus"]["failed"], {"code": "no_network", "reason": "the download broke",
                                                     "ts": h.state["bus"]["failed"]["ts"]})

    def test_a_wheel_that_is_not_the_pin_puts_nothing_in(self):
        h = self.hub()
        lib = bus.pylib(h.state_dir)
        os.makedirs(os.path.join(lib, "keep"))
        path, part = self.wheel(inside="49.0.0")
        with mock.patch.object(busd, "_pinned", return_value=[part]), self.net(path):
            self.settle(h)
        self.assertEqual(h.state["bus"]["failed"]["code"], "install_failed")
        self.assertIn("49.0.0", h.state["bus"]["failed"]["reason"])
        self.assertEqual(os.listdir(lib), ["keep"])                    # the folder as it was
        self.assertFalse(os.path.exists(lib + ".new"))

    def test_installed_but_still_not_loading_says_why_now(self):
        h = self.hub()
        path, part = self.wheel()
        with mock.patch.object(busd, "_pinned", return_value=[part]), self.net(path), \
                mock.patch.object(bus, "start_check", return_value=[]), \
                mock.patch.object(bus, "crypto", side_effect=bus.Unavailable("no_lib", "GLIBC_2.28 not found")):
            self.settle(h)
        f = h.state["bus"]["failed"]
        self.assertEqual(f["code"], "no_lib")
        self.assertIn("GLIBC_2.28", f["reason"])
        self.assertIn(bus.pylib(h.state_dir), f["reason"])

    def test_an_outcome_after_the_owner_fixed_the_bus_says_nothing(self):
        h = self.hub()
        _, part = self.wheel()
        with mock.patch.object(busd, "_pinned", return_value=[part]), self.net(fail=("broken", "the download broke")):
            busd.tick(h)
            self.join()
            h.record("core.bus_enabled", None, dict(self.FACT))      # `hub bus on --install` meanwhile
            busd.tick(h)
        self.assertTrue(h.state["bus"]["crypto_ready"])

    def test_a_hub_from_git_is_not_touched(self):
        h = self.hub(app=False)      # its Python is the owner's: `hub bus on --install`, with his consent
        with self.net(fail=("broken", "x")):
            self.settle(h)
        self.assertEqual(self.got, [])
        self.assertEqual(h.state["bus"]["failed"]["code"], "no_lib")

    def test_a_platform_with_no_pinned_wheel_is_not_touched(self):
        h = self.hub(platform="windows-arm64")      # no cryptography >= 47 wheel for Windows on ARM
        with self.net(fail=("broken", "x")):
            self.settle(h)
        self.assertEqual(self.got, [])
        self.assertEqual(h.state["bus"]["failed"]["code"], "no_lib")

    def test_the_wheels_are_the_release_s_pins_of_the_platform(self):
        from core import packaged
        with open(os.path.join(REPO, "tools", "package", "versions.json"), encoding="utf-8") as f:
            v = json.load(f)
        minor = ".".join(v["python"]["version"].split(".")[:2])
        with mock.patch.object(packaged, "info", return_value={"format": "app", "platform": "linux-x64"}):
            got = busd._pinned()
        if "%d.%d" % sys.version_info[:2] != minor:
            self.assertEqual(got, [])        # another Python than the environment's: its wheels may not load here
            return
        self.assertEqual({p["key"] for p in got}, {"cryptography", "cffi", "pycparser"})
        c = next(p for p in got if p["key"] == "cryptography")
        self.assertEqual((c["sha256"], c["version"]), (v["cryptography"]["files"]["linux-x64"]["sha256"],
                                                       v["cryptography"]["version"]))

    def test_the_environment_s_own_library_wins_over_what_the_core_put_in_for_an_older_one(self):
        from importlib import metadata
        state = tempfile.mkdtemp(prefix="bus-own-")
        self.addCleanup(platforms.current.rmtree, state)
        lib = bus.pylib(state)
        os.makedirs(lib)
        for own, used in (("50.0.2", False), ("46.0.5", True), (None, True)):
            with self.subTest(own=own):
                def version(name, own=own):
                    if own is None:
                        raise metadata.PackageNotFoundError(name)
                    return own
                bus._LIB = None
                with mock.patch.object(sys, "path", [p for p in sys.path if p != lib]), \
                        mock.patch.object(metadata, "version", side_effect=version), \
                        mock.patch.object(bus, "_import", side_effect=ImportError("stop")):
                    with self.assertRaises(bus.Unavailable):
                        bus.crypto(state)
                    self.assertEqual(lib in sys.path, used)
        bus._LIB = None

    def test_the_set_of_wheels_goes_whole_or_not_at_all(self):
        from core import wheels
        root = tempfile.mkdtemp(prefix="bus-pins-")
        self.addCleanup(platforms.current.rmtree, root)
        minor = "%d.%d" % sys.version_info[:2]
        tag = "cp%d%d" % sys.version_info[:2]

        def pin(name):
            return {"url": "https://files.example.test/" + name, "sha256": "a" * 64, "size": 10}
        v = {"python": {"version": minor + ".8"},
             "cryptography": {"version": "50.0.2", "wheel": True, "files": {
                 "linux-x64": pin("cryptography-50.0.2-cp311-abi3-manylinux2014_x86_64.whl")}},
             "cffi": {"version": "2.1.1", "wheel": True, "files": {
                 "linux-x64": pin(f"cffi-2.1.1-{tag}-{tag}-manylinux2014_x86_64.whl")}}}
        path = os.path.join(root, "versions.json")
        with open(path, "w") as f:
            json.dump(v, f)
        self.assertEqual([p["key"] for p in wheels.pinned(path, "linux-x64", minor)], ["cryptography", "cffi"])
        v["cffi"]["files"]["linux-x64"] = pin("cffi-2.1.1-cp399-cp399-manylinux2014_x86_64.whl")   # of another Python
        with open(path, "w") as f:
            json.dump(v, f)
        self.assertEqual(wheels.pinned(path, "linux-x64", minor), [])      # not cryptography without cffi

    def test_only_a_missing_or_old_library_is_put_in(self):
        h = self.hub()
        h.record("core.bus_key_failed", None, {"code": "keys_broken", "reason": "the key set is incomplete"})
        _, part = self.wheel()
        with mock.patch.object(busd, "_pinned", return_value=[part]), self.net(fail=("broken", "x")):
            self.settle(h)
        self.assertEqual(self.got, [])

    def test_the_bus_off_puts_nothing_in(self):
        h = self.hub()
        h.record("bus.off_requested", None, {"channel": "cli", "owner": True, "req": "r1"})
        _, part = self.wheel()
        with mock.patch.object(busd, "_pinned", return_value=[part]), self.net(fail=("broken", "x")):
            self.settle(h)
        self.assertEqual(self.got, [])


class NoOwnCrypto(unittest.TestCase):
    """The core has no cryptographic primitives of its own (spec §5.2.1): the bus calls cryptography (HPKE) and hashlib
    (the fingerprint and the check code) only. Checked by the syntax tree, not by grep."""

    def test_no_primitives_in_the_bus_code(self):
        for name in ("bus.py", "busstate.py"):
            with open(os.path.join(REPO, "core", name), encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                    for m in mods:
                        self.assertFalse(m.split(".")[0] in ("hmac", "random", "ctypes", "nacl", "Crypto"), (name, m))
                if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
                    self.assertLess(abs(node.value), 2 ** 64, (name, node.lineno))     # no field primes, no curve constants
                if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "pow":
                    self.assertLess(len(node.args), 3, (name, node.lineno))           # no modular exponentiation
                if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow) and isinstance(node.right, ast.Constant):
                    self.assertLessEqual(node.right.value, 64, (name, node.lineno))
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "hashlib":
                    self.assertEqual(node.attr, "sha256", (name, node.lineno))
                if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.LShift, ast.RShift, ast.BitXor)):
                    self.fail(f"{name}:{node.lineno}: bit arithmetic — a primitive of its own?")


if __name__ == "__main__":
    unittest.main()
