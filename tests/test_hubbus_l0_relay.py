"""The hub bus, slice L0 through a real relay (spec hubbus/ux §4.2, §5.1, §6): a link made on A — its permit recorded
on the relay by A's transport; pasted on B — B registers by that permit (a job of its transport), introduces itself,
A answers; the owners' chat and the unlink through the relay; the relay's word about the versions; a permit used by a
hub that did not come with the link — the owner of A is told.

The server is relay/server.py of this tree (HUB_TEST_RELAY_SERVER — another copy, e.g. of the relay's slice before it
is merged): a relay that does not announce `bus_v` 2 in /v1/hello cannot carry these envelopes — the class is skipped
and says so. Without cryptography ≥ 47 and ssh-keygen — skipped too."""
import tests  # noqa: F401 — keeps the machine's settings out
import base64
import contextlib
import json
import os
import subprocess
import sys
import time
import unittest
import urllib.request

from tests.test_hubbus import need_lib, need_ssh
from tests.test_hubbus_s2 import SERVER, Ident, RelayProcess
from tests.test_hubbus_l0 import joined, make_link, notices, paste
from core import bus, busd, busrelay

RELAY = os.environ.get("HUB_TEST_RELAY_SERVER") or SERVER
OWNER = {"channel": "cli", "owner": True}


class Relay(RelayProcess):
    """The relay process of RELAY on a free port."""

    def start(self):
        self.proc = subprocess.Popen([sys.executable, RELAY, "serve"], env=self.env(), stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT)
        line = self.proc.stdout.readline().decode()
        assert "listening" in line, line
        self.key = line.rsplit("key ", 1)[1].strip()

    def cmd(self, *args):
        r = subprocess.run([sys.executable, RELAY, *args], env=self.env(), capture_output=True, text=True, timeout=60)
        return r.returncode, r.stdout.strip(), r.stderr.strip()

    def version(self):
        with urllib.request.urlopen(self.url + "/v1/hello", timeout=10) as r:
            return json.loads(r.read()).get("bus_v") or 1


@need_lib
@need_ssh
class ThroughTheRelay(unittest.TestCase):
    def setUp(self):
        from tests.test_hubbus_s1 import LiveHub
        self.relay = Relay()
        self.addCleanup(self.relay.stop)
        if self.relay.version() < 2:
            self.skipTest(f"{RELAY}: the relay does not announce bus_v 2 (the relay's slice L2 is not in this tree; "
                          "HUB_TEST_RELAY_SERVER=<its server.py>)")
        now = int(time.time())
        self.a, self.b = LiveHub("Hub A", now=now), LiveHub("Hub B", now=now)
        self.cli(self.a, ["relay", "join", self.relay.invite()])
        self.ta, self.tb = self.transport(self.a), self.transport(self.b)

    def cli(self, hub, argv):
        from core import buscli
        import io

        def put(type_, entity, data, say=True):
            return hub.rec(type_, {**data, **OWNER}, entity)["seq"]
        out = io.StringIO()
        with hub.env(), contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = buscli.main(argv, put, lambda: hub.h.state, hub.h.state_dir)
        self.assertEqual(code, 0, out.getvalue())
        return out.getvalue().strip()

    def transport(self, hub):
        from modules.bus_relay.run import Transport

        def put(kind, data):
            with hub.env():
                hub.h.record(kind, None, data, src="bus-relay")
        t = Transport(hub.h.state_dir, hub.config, put=put, log=lambda text: None)
        t.stop.wait = lambda s: None   # the stand does not sleep between passes
        return t

    def settle(self, rounds=12, hubs=None):
        """Each hub: its tick (timers, a join going on), its view, its transport's jobs, hands and takes — until quiet."""
        for _ in range(rounds):
            moved = 0
            for hub, t in hubs or ((self.a, self.ta), (self.b, self.tb)):
                hub.now = max(hub.now, int(time.time()))
                hub.h.__dict__.pop("bus_link_tried", None)
                with hub.env():
                    busd.tick(hub.h)
                    busd.write_view(hub.h)
                before = hub.h.state["seq"]
                v = t.view()
                if v is None:
                    continue
                t.job_tries.clear()
                moved += t.hand(v) + t.take(v, wait=0)
                moved += hub.h.state["seq"] != before   # the transport told the core something
            if not moved:
                return

    def test_link_chat_and_unlink_through_the_relay(self):
        a, b = self.a, self.b
        text, lid = make_link(a)
        self.assertTrue(a.bus["jobs"])                          # the permit waits for the transport
        self.settle()
        self.assertTrue(a.bus["links"][lid]["posted"])
        self.assertEqual(a.bus["relay"]["bus_v"], 2)
        self.assertIsNone(b.bus["relay"])
        paste(b, text)
        self.settle()
        self.assertEqual(joined(b)["step"], "linked", joined(b))
        self.assertEqual(b.bus["relay"]["url"], self.relay.url)  # B registered by the link's permit
        self.assertEqual(a.bus["links"][lid]["state"], "used")
        na, nb = next(iter(a.bus["contacts"])), next(iter(b.bus["contacts"]))
        code = bus.contact_code(a.bus["contacts"][na])
        self.assertEqual(code, bus.contact_code(b.bus["contacts"][nb]))
        self.assertTrue(any(code in n["text"] for n in notices(a)))
        self.assertTrue(any(code in n["text"] for n in notices(b)))
        # the relay heard both speak version 2
        self.settle()
        self.assertEqual((a.bus["contacts"][na].get("peer") or {}).get("v"), 2)
        # the old form of the owners' chat both ways, before any check: for the other hub it is a message to its liaison,
        # so it waits for the check there (the receipt says «held»)
        a.ask("bus.chat_requested", {"contact": na, "text": "Hello from owner A over the relay"})
        self.settle()
        b.ask("bus.chat_requested", {"contact": nb, "text": "And hello to you"})
        self.settle()
        self.assertEqual([(x["text"], x["status"]) for x in b.bus["inbox"].values() if x.get("chat")],
                         [("Hello from owner A over the relay", "unverified")])
        self.assertEqual([(x["text"], x["status"]) for x in a.bus["inbox"].values() if x.get("chat")],
                         [("And hello to you", "unverified")])
        for hub in (a, b):
            self.assertEqual([o.get("remote") for o in hub.bus["out"].values() if o.get("kind") == "message"], ["held"])
        # the relay keeps no text and no one-time key
        dump = self.relay.dump()
        seed = bus.read_link(text, a.now)["seed"]
        for secret in ("Hello from owner".encode(), seed.encode(), text[len(bus.LINK_PREFIX):][:40].encode()):
            self.assertNotIn(secret, dump)
        # unlink
        a.ask("bus.contact_requested", {"op": "remove", "name": na})
        self.settle()
        self.assertEqual(b.bus["contacts"], {})
        self.assertTrue(any("unlinked you" in n["text"] for n in notices(b)))

    def test_a_permit_used_by_a_hub_that_did_not_come_is_told_to_the_owner(self):
        a = self.a
        text, lid = make_link(a)
        self.settle(hubs=((a, self.ta),))
        thief = Ident(self.relay, "thief")
        invite = bus.read_link(text, a.now)["invite_text"]
        thief.join(invite)                                      # registered by the stolen permit, never introduced
        self.ta.watched = (None, 0)
        self.settle(hubs=((a, self.ta),))
        self.assertEqual(a.bus["links"][lid]["invite_used"]["fp"], thief.fp)
        self.assertFalse([n for n in notices(a) if thief.fp in n["text"]])   # not before ten minutes
        a.now += 601
        with a.env():
            busd.tick(a.h)
        self.assertEqual(a.bus["links"][lid]["misused"], thief.fp)
        told = [n for n in notices(a) if thief.fp in n["text"]]
        self.assertEqual(len(told), 1)
        with a.env():
            busd.tick(a.h)
        self.assertEqual(len([n for n in notices(a) if thief.fp in n["text"]]), 1)   # once

    def test_revoke_drops_the_unused_permit_on_the_relay(self):
        a = self.a
        text, lid = make_link(a)
        self.settle(hubs=((a, self.ta),))
        a.ask("bus.link_revoke_requested", {"id": lid})
        self.settle(hubs=((a, self.ta),))
        self.assertEqual(a.bus["links"][lid].get("revoked_relay"), "ok")
        thief = Ident(self.relay, "late")
        with self.assertRaises(busrelay.Error) as e:
            thief.join(bus.read_link(text, a.now)["invite_text"])
        self.assertEqual(e.exception.code, "bad_invite")

    def test_every_signed_request_names_the_hubs_version(self):
        a = self.a
        rows = []
        orig = busrelay.Client._http

        def spy(client, method, target, body, headers, timeout, limit):
            rows.append(dict(headers))
            return orig(client, method, target, body, headers, timeout, limit)
        busrelay.Client._http = spy
        try:
            self.settle(hubs=((a, self.ta),))
        finally:
            busrelay.Client._http = orig
        signed = [r for r in rows if "X-Hub-Sig" in r]
        self.assertTrue(signed)
        self.assertTrue(all(r.get("X-Hub-Bus-V") == "2" for r in signed), signed[:1])


if __name__ == "__main__":
    unittest.main()
