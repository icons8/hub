"""The relay for everyone (spec public relay): one line anyone may register with (`server.py open`, many hubs, closed by
`open --off`), the first hub's line repeated until that hub comes (`server.py bootstrap`), the daily cap of joins and
the long polls by setting, a letter that wakes only its receiver's long poll, and the signatures made in the relay's own
process (the cryptography library, the same SSHSIG text ssh-keygen writes and reads).

The registration runs against a real process (tests/test_hubbus_s2.RelayProcess); the waking, the caps and the
signatures on the Relay class and the module's functions. Without ssh-keygen everything here is skipped; the library's
signatures — also without cryptography."""
import tests  # noqa: F401 — keeps the machine's settings out
import base64
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests.test_hubbus import need_ssh
from tests.test_hubbus_s2 import SERVER, Ident, RelayProcess, server_module
from core import bus, busrelay

LIB = importlib.util.find_spec("cryptography") is not None


def need_lib(cls):
    return unittest.skipUnless(LIB, "no cryptography library")(cls)


def fp_of(n):
    return "hb1-" + format(n, "026b").replace("0", "a").replace("1", "b")


def letter(src, dst):
    """A box of the right outer form from src to dst (the relay reads the form, not the signature)."""
    ts = int(time.time())
    head = json.dumps({"v": 1, "t": "box", "id": os.urandom(16).hex(), "ts": ts, "exp": ts + 86400, "from": src,
                       "to": dst, "ct": base64.b64encode(os.urandom(32 + 1024 + 16)).decode()}, separators=(",", ":"))
    env = json.dumps({"head": head, "sig": "-----BEGIN SSH SIGNATURE-----\nx\n-----END SSH SIGNATURE-----\n"},
                     separators=(",", ":")).encode()
    return json.dumps({"to": dst, "envelope": base64.b64encode(env).decode()}).encode()


@need_ssh
class OpenLine(unittest.TestCase):
    """`server.py open`: one line, many hubs; `open --off`: the line lets no one in, the hubs stay."""

    def setUp(self):
        self.relay = RelayProcess()
        self.addCleanup(self.relay.stop)

    def test_one_line_registers_many_hubs_until_it_is_closed(self):
        code, line, err = self.relay.cmd("open")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(line.splitlines()), 1)
        fields = busrelay.read_invite(line)[0]                             # the format every core reads
        self.assertEqual((fields["relay"], fields["key"], fields["by"]), (self.relay.url, self.relay.key, "operator"))
        self.assertGreater(fields["exp"], time.time() + 300 * 86400)       # a year by default
        a, b = Ident(self.relay, "oa"), Ident(self.relay, "ob")
        self.assertEqual(a.join(line, "A")["invited_by"], "operator")
        self.assertEqual(b.join(line, "B")["invited_by"], "operator")
        self.assertEqual(self.relay.cmd("open")[1], line)                  # asked again: the same line
        code, out, err = self.relay.cmd("open", "--off")
        self.assertEqual(code, 0, err)
        c = Ident(self.relay, "oc")
        with self.assertRaises(busrelay.Error) as e:
            c.join(line, "C")
        self.assertEqual(e.exception.code, "bad_invite")
        self.assertEqual(a.client.allow([b.fp]), {"allowed": 1})          # the hubs that came by it stay
        code, new, err = self.relay.cmd("open")                            # opened again: another line
        self.assertEqual(code, 0, err)
        self.assertNotEqual(new, line)
        self.assertEqual(c.join(new, "C")["invited_by"], "operator")

    def test_a_term_in_days(self):
        code, line, err = self.relay.cmd("open", "--days", "30")
        self.assertEqual(code, 0, err)
        exp = busrelay.read_invite(line)[0]["exp"]
        self.assertTrue(time.time() + 29 * 86400 < exp <= time.time() + 30 * 86400 + 60)

    def test_an_operators_invitation_stays_one_time(self):
        self.assertEqual(self.relay.cmd("open")[0], 0)
        one = self.relay.invite()
        Ident(self.relay, "ta").join(one, "A")
        with self.assertRaises(busrelay.Error) as e:
            Ident(self.relay, "tb").join(one, "B")
        self.assertEqual(e.exception.code, "bad_invite")

    def test_open_off_with_anything_else_and_unknown_words_change_nothing(self):
        line = self.relay.cmd("open")[1]
        for args in (("open", "--off", "--url", self.relay.url), ("open", "--days", "30", "--off"), ("open", "--of")):
            code, out, err = self.relay.cmd(*args)
            self.assertEqual((code, out), (2, ""), args)                   # refused, never taken for an opening
        self.assertEqual(Ident(self.relay, "xa").join(line, "A")["invited_by"], "operator")   # still open
        self.assertEqual(self.relay.cmd("open")[1], line)                  # and no second line was made

    def test_days_on_a_live_line_are_said_not_to_change_it(self):
        line = self.relay.cmd("open")[1]
        code, again, err = self.relay.cmd("open", "--days", "7")
        self.assertEqual((code, again), (0, line))
        self.assertIn("--days does not change it", err)

    def test_bootstrap_repeats_its_line_until_the_first_hub_comes(self):
        self.assertEqual(self.relay.cmd("open")[0], 0)                     # an open line is no first hub's line
        code, first, err = self.relay.cmd("bootstrap")
        self.assertEqual(code, 0, err)
        code, again, err = self.relay.cmd("bootstrap")                     # the output was lost: the same line
        self.assertEqual((code, again), (0, first), err)
        Ident(self.relay, "ba").join(first, "A")
        code, out, err = self.relay.cmd("bootstrap")                       # a hub is here: links come from it
        self.assertEqual((code, out), (1, ""))

    def test_bootstrap_never_hands_out_an_invitation_given_to_someone(self):
        given = self.relay.invite()                                        # `server.py invite`, sent to someone
        code, first, err = self.relay.cmd("bootstrap")
        self.assertEqual(code, 0, err)
        self.assertNotEqual(first, given)
        env = self.relay.env() if LIB else {**self.relay.env(), "HUB_RELAY_SIGNER": "ssh-keygen"}   # no library:
        check = subprocess.run([sys.executable, SERVER, "check"], env=env, capture_output=True, timeout=60)  # chosen
        self.assertEqual(check.returncode, 0, check.stderr)                # and the image's health check holds


@need_ssh
class Settings(unittest.TestCase):
    """The daily cap of joins by members' invitations and the long polls at once, by setting."""

    def setUp(self):
        self.server = server_module()
        self.folder = tempfile.mkdtemp(prefix="relay-p-")

    def relay(self, **env):
        with mock.patch.dict(os.environ, env):
            r = self.server.Relay(os.path.join(self.folder, "relay.sqlite3"))
        self.addCleanup(r.store.db.close)   # an open database cannot be removed on Windows
        return r

    def test_the_daily_cap_of_joins_is_a_setting_and_zero_lifts_it(self):
        relay = self.relay()
        member = Ident.__new__(Ident)
        member.folder = tempfile.mkdtemp(prefix="id-m-")
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "hub-test", "-f",
                        os.path.join(member.folder, "key")], check=True, stdin=subprocess.DEVNULL)
        with open(os.path.join(member.folder, "key.pub"), encoding="ascii") as f:
            sp = bus._ed25519_raw(f.read())
        ep = os.urandom(32)
        member.keys = bus.Keys(sign_key=os.path.join(member.folder, "key"), sign_pub=sp, enc_pub=ep,
                               fp=bus.fingerprint(sp, ep))
        now = int(time.time())
        with relay.store.tx() as db:
            db.execute("INSERT INTO hubs (fp, sign_pub, transport_pub, invited_by, invite, joined) VALUES (?, ?, ?, ?, ?, ?)",
                       (member.keys.fp, bus._b64(sp), bus._b64(os.urandom(32)), "operator", "i", now))
            for i in range(50):                                            # fifty joined by members' links today
                db.execute("INSERT INTO invites (id, by, made, exp, signer, used_by, used) VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (os.urandom(16).hex(), member.keys.fp, now, now + 86400, "s", fp_of(i), now))
        text, iid, exp = busrelay.make_invite(member.keys, {"url": "https://relay.example.test", "key": relay.key}, now)
        with relay.store.tx() as db:
            db.execute("INSERT INTO invites (id, by, made, exp, signer) VALUES (?, ?, ?, ?, ?)",
                       (iid, member.keys.fp, now, exp, bus._b64(sp)))
        with relay.store.tx() as db, self.assertRaises(self.server.Refused) as e:
            relay.check_invite(db, text, now)
        self.assertEqual(e.exception.code, "joins_day")
        lifted = self.relay(HUB_RELAY_JOINS_DAY="0")
        with lifted.store.tx() as db:
            self.assertEqual(lifted.check_invite(db, text, now), (iid, member.keys.fp, False))

    def test_long_polls_at_once_are_a_setting(self):
        relay = self.relay(HUB_RELAY_POLLS="1")
        a, b = fp_of(1), fp_of(2)
        got = {}
        t = threading.Thread(target=lambda: got.update(a=relay.inbox(self.server.Req(a, os.urandom(16).hex(),
                                                                                    int(time.time()), 1), 2)))
        t.start()
        deadline = time.monotonic() + 10
        while relay.polls < 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        status, body = relay.inbox(self.server.Req(b, os.urandom(16).hex(), int(time.time()), 1), 2)
        self.assertEqual((status, body["code"]), (503, "busy"))
        t.join(10)
        self.assertEqual(got["a"][0], 200)
        self.assertEqual(self.relay().polls_max, self.server.POLLS)       # no setting: as before
        for bad in ("0", "-1", "²", "many"):                                 # zero polls would refuse every hub
            self.assertEqual(self.relay(HUB_RELAY_POLLS=bad).polls_max, self.server.POLLS, bad)


@need_ssh
class Waking(unittest.TestCase):
    """A letter wakes the long poll of its receiver alone: the other hubs' polls go on waiting and do not look into the
    database (each look takes the store's one lock)."""

    def test_a_letter_wakes_only_its_receiver(self):
        server = server_module()
        relay = server.Relay(os.path.join(tempfile.mkdtemp(prefix="relay-w-"), "relay.sqlite3"))
        self.addCleanup(relay.store.db.close)
        sender, hubs = fp_of(0), [fp_of(i) for i in range(1, 21)]
        now = int(time.time())
        with relay.store.tx() as db:
            for fp in [sender, *hubs]:
                db.execute("INSERT INTO hubs (fp, sign_pub, transport_pub, invited_by, invite, joined) "
                           "VALUES (?, 'x', 'y', 'operator', 'i', ?)", (fp, now))
            db.executemany("INSERT INTO allow (hub, sender) VALUES (?, ?)", [(fp, sender) for fp in hubs])
        looks, waiting = {}, relay._waiting
        lock = threading.Lock()

        def counted(fp):
            with lock:
                looks[fp] = looks.get(fp, 0) + 1
            return waiting(fp)
        relay._waiting = counted
        got = {}

        def poll(fp):
            got[fp] = relay.inbox(server.Req(fp, os.urandom(16).hex(), now, 1), 4)
        threads = [threading.Thread(target=poll, args=(fp,)) for fp in hubs]
        for t in threads:
            t.start()
        self.addCleanup(lambda: [t.join(10) for t in threads])   # before the database closes (cleanups run backwards)
        deadline = time.monotonic() + 10
        while (relay.polls < len(hubs) or len(looks) < len(hubs)) and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.2)
        with lock:
            looks.clear()
        status, _ = relay.send(server.Req(sender, os.urandom(16).hex(), now, 1), letter(sender, hubs[0]))
        self.assertEqual(status, 200)
        deadline = time.monotonic() + 3
        while hubs[0] not in got and time.monotonic() < deadline:
            time.sleep(0.01)
        with lock:
            seen = dict(looks)
        self.assertEqual(len(got[hubs[0]][1]["items"]), 1)                 # the receiver got its letter at once
        self.assertEqual(set(seen), {hubs[0]}, seen)                       # and nobody else looked


    def test_a_letter_that_comes_while_the_poll_looks_is_not_missed(self):
        server = server_module()
        relay = server.Relay(os.path.join(tempfile.mkdtemp(prefix="relay-w-"), "relay.sqlite3"))
        self.addCleanup(relay.store.db.close)
        sender, hub = fp_of(0), fp_of(1)
        now = int(time.time())
        with relay.store.tx() as db:
            for fp in (sender, hub):
                db.execute("INSERT INTO hubs (fp, sign_pub, transport_pub, invited_by, invite, joined) "
                           "VALUES (?, 'x', 'y', 'operator', 'i', ?)", (fp, now))
            db.execute("INSERT INTO allow (hub, sender) VALUES (?, ?)", (hub, sender))
        waiting, sent = relay._waiting, []

        def look_then_letter(fp):   # the letter lands right after the poll's look, before it waits
            got = waiting(fp)
            if not sent:
                sent.append(relay.send(server.Req(sender, os.urandom(16).hex(), now, 1), letter(sender, hub)))
            return got
        relay._waiting = look_then_letter
        waits, wait = [], threading.Condition.wait
        with mock.patch.object(threading.Condition, "wait", lambda c, timeout=None: waits.append(timeout) or
                               wait(c, timeout)):
            status, body = relay.inbox(server.Req(hub, os.urandom(16).hex(), now, 1), 8)
        self.assertEqual((status, len(body["items"])), (200, 1))
        self.assertEqual(waits, [])                                        # it looked again at once, it did not wait


@need_ssh
class Introductions(unittest.TestCase):
    """Introductions come from any registered hub — on an open relay, from anyone: they have their own room in a hub's
    queue and never take the place of the boxes its contacts send."""

    def test_introductions_do_not_fill_the_boxes_room(self):
        server = server_module()
        relay = server.Relay(os.path.join(tempfile.mkdtemp(prefix="relay-i-"), "relay.sqlite3"))
        self.addCleanup(relay.store.db.close)
        now = int(time.time())
        hub, friend = fp_of(1), fp_of(2)
        strangers = []
        for _ in range(3):
            sp, ep = os.urandom(32), os.urandom(32)
            strangers.append((server.fingerprint(sp, ep), sp, ep))
        with relay.store.tx() as db:
            for fp in [hub, friend, *(s[0] for s in strangers)]:
                db.execute("INSERT INTO hubs (fp, sign_pub, transport_pub, invited_by, invite, joined) "
                           "VALUES (?, 'x', 'y', 'operator', 'i', ?)", (fp, now))
            db.execute("INSERT INTO allow (hub, sender) VALUES (?, ?)", (hub, friend))

        def card(fp, sp, ep):
            head = json.dumps({"v": 1, "t": "intro", "id": os.urandom(16).hex(), "ts": now, "exp": now + 86400,
                               "card": {"label": "X", "sign_pub": base64.b64encode(sp).decode(),
                                        "enc_pub": base64.b64encode(ep).decode(), "relays": [], "agents": {},
                                        "bus_v": 1}}, separators=(",", ":"))
            env = json.dumps({"head": head, "sig": "-----BEGIN SSH SIGNATURE-----\nx\n-----END SSH SIGNATURE-----\n"},
                             separators=(",", ":")).encode()
            return relay.send(server.Req(fp, os.urandom(16).hex(), now, 1),
                              json.dumps({"to": hub, "envelope": base64.b64encode(env).decode()}).encode())[0]
        one = strangers[0]
        self.assertEqual([card(*one) for _ in range(server.INTROS_PAIR + 1)],
                         [200] * server.INTROS_PAIR + [503])                # a few from one hub, then wait
        with mock.patch.object(server, "INTROS_TO", server.INTROS_PAIR + 2), \
                mock.patch.object(server, "QUEUE_TO", (server.INTROS_PAIR + 2, server.QUEUE_TO[1])):
            self.assertEqual([card(*s) for s in strangers[1:]], [200, 200])
            self.assertEqual(card(*strangers[2]), 503)                     # the introductions' room is full
            # a box of a contact still goes in: the strangers' introductions are not in the boxes' count
            self.assertEqual(relay.send(server.Req(friend, os.urandom(16).hex(), now, 1), letter(friend, hub))[0], 200)


@need_ssh
@need_lib
class Signatures(unittest.TestCase):
    """The relay's own process signs and checks with the library — the very text ssh-keygen -Y writes, so a hub's
    ssh-keygen reads the relay's answers and the relay reads the hub's requests; ssh-keygen stays the way without the
    library (HUB_RELAY_SIGNER=ssh-keygen, or no cryptography installed)."""

    def setUp(self):
        self.server = server_module()
        self.folder = tempfile.mkdtemp(prefix="relay-s-")
        self.key = os.path.join(self.folder, "key")
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "relay-test", "-f", self.key], check=True,
                       stdin=subprocess.DEVNULL)
        with open(self.key + ".pub", encoding="ascii") as f:
            self.pub = self.server.raw_pub(f.read())
        self.data = b"agents-hub-relay-v1 request\n" + os.urandom(200)

    def test_the_library_writes_the_same_text_as_ssh_keygen(self):
        self.assertEqual(self.server.sign_lib(self.key, self.data), self.server.sign_ssh(self.key, self.data))

    def test_each_way_reads_the_others_signature(self):
        by_lib, by_ssh = self.server.sign_lib(self.key, self.data), self.server.sign_ssh(self.key, self.data)
        self.assertTrue(self.server.verify_lib(self.pub, self.data, by_ssh))
        self.assertTrue(self.server.verify_ssh(self.pub, self.data, by_lib))

    def test_a_wrong_byte_key_or_namespace_is_refused_both_ways(self):
        good = self.server.sign_ssh(self.key, self.data)
        other = subprocess.run(["ssh-keygen", "-Y", "sign", "-f", self.key, "-n", "agents-hub-bus-v1"],
                               input=self.data, capture_output=True, check=True).stdout.decode()
        stranger = os.urandom(32)
        for check in (self.server.verify_lib, self.server.verify_ssh):
            self.assertFalse(check(self.pub, self.data + b"x", good), check.__name__)
            self.assertFalse(check(stranger, self.data, good), check.__name__)
            self.assertFalse(check(self.pub, self.data, other), check.__name__)
            self.assertFalse(check(self.pub, self.data, good.replace("A", "B", 1)), check.__name__)
        for junk in ("", "-----BEGIN SSH SIGNATURE-----\n-----END SSH SIGNATURE-----\n", "not a signature"):
            self.assertFalse(self.server.verify_lib(self.pub, self.data, junk))

    def test_the_way_is_chosen_by_the_library_and_the_setting(self):
        self.assertIs(self.server.sign, self.server.sign_lib)
        with mock.patch.dict(os.environ, {"HUB_RELAY_SIGNER": "ssh-keygen"}):
            forced = server_module()
        self.assertIs(forced.sign, forced.sign_ssh)
        self.assertIs(forced.verify, forced.verify_ssh)


if __name__ == "__main__":
    unittest.main()
