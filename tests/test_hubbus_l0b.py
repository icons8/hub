"""The hub bus, slice L0b (spec hubbus/ux §5.2): the pair code with a commitment. A man in the middle of the link's
channel who knows both fingerprints in advance must not be able to pick his keys so that the two pages show the same
code: A commits to a fresh nonce in the link, B adds the id of its introduction, A reveals the nonce as the id of its
answer, and the code is a hash of both fingerprints and both nonces — the man in the middle gets one guess per link,
none offline.

Two (or four) cores of the stand in one process (tests/test_hubbus_s1.LiveHub) pass envelopes through a folder. Without
cryptography ≥ 47 and ssh-keygen these tests are skipped with the reason."""
import tests  # noqa: F401 — keeps the machine's settings out
import base64
import hashlib
import json
import os
import re
import unittest

from tests.test_hubbus import need_lib, need_ssh
from tests.test_hubbus_l0 import fps, joined, make_link, notices, paste
from core import bus, busd

SHORT = 4      # the digits the stand's shortened code keeps: offline grinding is cheap, a chance match is 10⁻⁴
POOL = 500     # the keys the man in the middle tries for each side (500 × 500 pairs: no match ≈ e⁻²⁵)


def fps_only(fp1, fp2):
    """The code as a function of the two fingerprints alone (the formula of L0) — what the man in the middle grinds."""
    lo, hi = sorted((fp1, fp2))
    n = int.from_bytes(hashlib.sha256(b"agents-hub-bus-v1 pair\0" + lo.encode() + b"\0" + hi.encode()).digest()[:16],
                       "big") % 10 ** 24
    return f"{n:024d}"


def short(code):
    return re.sub(r"\D", "", code)[-SHORT:]


def shown(hub):
    """The pair code the owner of `hub` was told when the link was done."""
    for n in notices(hub):
        m = re.search(r"\d+(?:-\d+)+", n["text"])
        if m:
            return m.group(0)
    raise AssertionError(f"no code told: {notices(hub)}")


def candidates(n):
    lib = bus.crypto()
    out = []
    for _ in range(n):
        sk, ek = lib.ed25519.Ed25519PrivateKey.generate(), lib.x25519.X25519PrivateKey.generate()
        out.append((bus.fingerprint(sk.public_key().public_bytes_raw(), ek.public_key().public_bytes_raw()), (sk, ek)))
    return out


def contact(hub):
    (c,) = hub.bus["contacts"].values()
    return c


@need_lib
@need_ssh
class Grinding(unittest.TestCase):
    def mitm(self):
        """One man in the middle of a link's channel: (the code A's page shows, the code B's page shows). He grinds his
        keys offline so that the code, as a function of the fingerprints, agrees on both sides (shortened), then stands
        between A and B: A's link goes to him, B gets his link in its place."""
        from tests.test_hubbus_s1 import LiveHub, Wire
        a, b = LiveHub("Hub A"), LiveHub("Hub B")
        fpa, fpb = a.bus["me"]["fp"], b.bus["me"]["fp"]
        xs = {short(fps_only(fpa, fp)): keys for fp, keys in candidates(POOL)}
        x = y = None
        for fp, keys in candidates(POOL):
            if short(fps_only(fp, fpb)) in xs:
                x, y = xs[short(fps_only(fp, fpb))], keys
                break
        self.assertIsNotNone(x, "the grinding found no pair: the stand's numbers are wrong")
        mb, ma = LiveHub("Hub B", keys=x), LiveHub("Hub A", keys=y)
        text, _ = make_link(a)
        paste(mb, text)
        Wire(a, mb).settle()
        text, _ = make_link(ma)
        paste(b, text)
        Wire(ma, b).settle()
        self.assertEqual(joined(b)["state"], "linked")
        self.assertEqual(joined(mb)["state"], "linked")
        return short(shown(a)), short(shown(b))

    def test_a_man_in_the_middle_grinding_keys_gets_no_matching_code(self):
        # the owners accept any two equal codes: the attack passed if the pages agree. Grinding makes them agree every
        # time; by chance two shortened codes agree once in 10⁴ — so the attack counts only if it worked in both of two
        # independent runs (by chance: 10⁻⁸)
        runs = [self.mitm() for _ in range(2)]
        self.assertFalse(all(ca == cb for ca, cb in runs), f"the pages show the same code: the attack passed {runs}")

    def test_the_code_of_a_rests_on_its_hidden_nonce_not_only_on_what_the_joiner_chose(self):
        """The joiner fixes its key and its introduction's id before A reveals its nonce; the same introduction against
        the same link record with another nonce gives another code — nothing the joiner sees before A's answer
        determines it (and the link carries only the commitment: Commitment, the second intro: refused)."""
        from tests.test_hubbus_s1 import LiveHub
        a, x = LiveHub("Hub A"), LiveHub("Hub X")
        text, lid = make_link(a)
        with x.env():
            keys = bus.load(bus.key_dir(x.config))
        eid = "e" * 32
        sig = bus.link_proof(bus.read_link(text, None)["seed"], a.bus["me"]["fp"], keys.fp, eid, keys.lib)
        _, raw = bus.link_intro(keys, "Hub X", x.now, "link", {"id": lid, "sig": sig}, id=eid)
        codes = set()
        for nonce in ("1" * 32, "2" * 32, "3" * 32):
            st = json.loads(json.dumps(a.bus))
            st["links"][lid]["nonce"] = nonce
            with a.env():
                got = bus.receive(st, keys_of(a), raw, a.now)
            self.assertEqual(got["code"], "link_used")
            codes.add(got["fact"][1]["sas"])
        self.assertEqual(len(codes), 3)


class Formula(unittest.TestCase):
    def test_commit_and_code_vectors(self):
        a, b = fps(2)
        lid, na, nb = "0" * 31 + "1", "a" * 32, "b" * 32
        c = bus.commit(lid, na)
        self.assertEqual(c, hashlib.sha256(b"agents-hub-bus-v1 commit\0" + lid.encode() + b"\0" + na.encode()).digest())
        code = bus.sas_code(a, b, lid, na, nb)
        self.assertRegex(code, r"^\d{3}-\d{3}$")
        n = int.from_bytes(hashlib.sha256(b"agents-hub-bus-v1 sas\0" + "\0".join((a, b, lid, na, nb)).encode())
                           .digest()[:16], "big") % 10 ** 6
        self.assertEqual(code.replace("-", ""), f"{n:06d}")
        self.assertEqual(code, VECTOR)
        # the roles count: A and B swapped, or any nonce changed — another code
        self.assertNotEqual(bus.sas_code(b, a, lid, na, nb), code)
        self.assertNotEqual(bus.sas_code(a, b, lid, nb, na), code)


VECTOR = "553-277"   # sas_code of Formula: pinned, a change of the formula shows here


@need_lib
@need_ssh
class Commitment(unittest.TestCase):
    def setUp(self):
        from tests.test_hubbus_s1 import LiveHub, Wire
        self.a, self.b = LiveHub("Hub A"), LiveHub("Hub B")
        self.wire = Wire(self.a, self.b)

    def test_both_pages_show_the_same_six_digits_and_the_link_carries_only_the_commitment(self):
        text, lid = make_link(self.a)
        body = json.loads(base64.urlsafe_b64decode(text[len(bus.LINK_PREFIX):] + "=="))
        nonce = self.a.bus["links"][lid]["nonce"]
        self.assertEqual(base64.urlsafe_b64decode(body["c"] + "="), bus.commit(lid, nonce))
        self.assertNotIn(nonce, text)
        paste(self.b, text)
        self.wire.settle()
        code = shown(self.a)
        self.assertRegex(code, r"^\d{3}-\d{3}$")
        self.assertEqual(shown(self.b), code)
        for hub in (self.a, self.b):
            self.assertEqual(bus.contact_code(contact(hub)), code)
        # the code is of this link's nonces: A's nonce came back as the id of its answer, B's is its introduction's id
        self.assertEqual(code, bus.sas_code(self.a.bus["me"]["fp"], self.b.bus["me"]["fp"], lid, nonce,
                                            joined(self.b)["intro_id"]))

    def test_an_answer_that_does_not_open_the_commitment_does_not_link(self):
        text, lid = make_link(self.a)
        self.a.bus["links"][lid]["nonce"] = "f" * 32   # A (or one in its place) reveals another nonce than it committed
        paste(self.b, text)
        self.wire.settle()
        self.assertEqual(joined(self.b)["state"], "active")
        self.assertIsNone(bus.contact_code(contact(self.b)))
        ev = self.b.ask("bus.contact_requested", {"op": "verify", "name": next(iter(self.b.bus["contacts"]))})
        self.assertEqual(self.b.bus["last"]["code"], "code_needed", self.b.verdict(ev))

    def test_the_joining_hub_waits_for_the_code_and_does_not_take_the_other_pages_code_before_it(self):
        """A's page shows the pair code as soon as B's introduction came, B's — only when A's answer came back: in
        between B's page must say it waits (not offer a field for «the other hub's code»), and the pair code typed
        there is not taken as the code of the hub (it cannot be checked yet) — with a refusal that says why."""
        from core import busflow
        text, lid = make_link(self.a)
        self.a.bus["links"][lid]["nonce"] = "f" * 32   # A's answer does not come as it should: B waits
        paste(self.b, text)
        self.wire.settle()
        name = next(iter(self.b.bus["contacts"]))
        (row,) = busflow.page_view(self.b.st)["contacts"]
        self.assertIsNone(row["code"])
        self.assertTrue(row["code_wait"])
        typed = shown(self.a)   # the code A's page shows already
        for form in (typed, typed.replace("-", ""), " " + typed.replace("-", " ")):
            self.b.ask("bus.contact_requested", {"op": "verify", "name": name, "code": form})
            self.assertEqual(self.b.bus["last"]["code"], "pair_code_unknown", form)
        self.assertFalse(contact(self.b).get("verified"))
        self.assertIn("20", busflow.why("pair_code_unknown"))
        # the join ran out its term: no answer is expected in time — the field is back, the old way stays open
        joined(self.b)["state"] = "expired"
        (row,) = busflow.page_view(self.b.st)["contacts"]
        self.assertFalse(row["code_wait"])
        self.b.ask("bus.contact_requested", {"op": "verify", "name": name, "code": typed})
        self.assertEqual(self.b.bus["last"]["code"], "code_mismatch")
        self.b.ask("bus.contact_requested", {"op": "verify", "name": name, "code": bus.code(contact(self.b)["fp"])})
        self.assertTrue(contact(self.b)["verified"])

    def test_a_contact_that_is_not_joining_has_no_wait_and_takes_the_hub_code(self):
        from core import busflow
        a = self.a
        a.ask("bus.contact_requested", {"op": "add", "name": "bee", "card": self.b.card()})
        (row,) = busflow.page_view(a.st)["contacts"]
        self.assertFalse(row["code_wait"])
        a.ask("bus.contact_requested", {"op": "verify", "name": "bee", "code": "686-167"})
        self.assertEqual(a.bus["last"]["code"], "code_mismatch")   # not waiting for any: the pair code is just wrong
        a.ask("bus.contact_requested", {"op": "verify", "name": "bee", "code": self.b.code()})
        self.assertTrue(a.bus["contacts"]["bee"]["verified"])

    def test_the_joined_pair_has_no_wait_and_takes_its_pair_code(self):
        from core import busflow
        text, lid = make_link(self.a)
        paste(self.b, text)
        self.wire.settle()
        for hub in (self.a, self.b):
            (row,) = busflow.page_view(hub.st)["contacts"]
            self.assertFalse(row["code_wait"])
        self.b.ask("bus.contact_requested", {"op": "verify", "name": next(iter(self.b.bus["contacts"])),
                                             "code": shown(self.a)})
        self.assertTrue(contact(self.b)["verified"])

    def test_a_second_introduction_with_another_id_gets_used_and_the_code_stays(self):
        text, lid = make_link(self.a)
        paste(self.b, text)
        self.wire.settle()
        code = shown(self.a)
        b = self.b
        with b.env():
            keys = bus.load(bus.key_dir(b.config))
        seed = bus.read_link(text, None)["seed"]
        eid = "e" * 32
        sig = bus.link_proof(seed, self.a.bus["me"]["fp"], keys.fp, eid, keys.lib)
        _, raw = bus.link_intro(keys, "Hub B", b.now, "link", {"id": lid, "sig": sig}, id=eid)
        with self.a.env():
            got = bus.receive(self.a.bus, keys_of(self.a), raw, self.a.now)
        self.assertEqual(got["code"], "link_refused")
        self.assertEqual(got["fact"][1]["code"], "used")
        self.assertEqual(bus.contact_code(contact(self.a)), code)

    def test_crossed_links_one_code_on_both_hubs(self):
        from tests.test_hubbus_s1 import LiveHub, Wire
        for order in ("ab", "ba"):
            a, b = LiveHub("Hub A"), LiveHub("Hub B")
            wire = Wire(a, b)
            la, _ = make_link(a)
            lb, _ = make_link(b)
            paste(b, la)
            paste(a, lb)
            first, second = (b, a) if order == "ab" else (a, b)
            wire.move(first)
            wire.move(second)
            wire.settle()
            ca, cb = contact(a), contact(b)
            self.assertEqual(ca["pair"], cb["pair"], order)
            self.assertIsNotNone(bus.contact_code(ca), order)
            self.assertEqual(bus.contact_code(ca), bus.contact_code(cb), order)


    def test_a_new_link_while_the_old_answer_is_late_one_pair_and_one_code(self):
        """A takes link 1, its answer to B is late; A makes link 2, B pastes it, A takes it; the answers come to B in
        either order — both hubs end with link 2 (made last) and its code."""
        from tests.test_hubbus_s1 import LiveHub, Wire
        for order in ("12", "21"):
            a, b = LiveHub("Hub A"), LiveHub("Hub B")
            wire = Wire(a, b)
            t1, l1 = make_link(a)
            paste(b, t1)
            wire.move(b)                      # A takes link 1; its answer stays with A
            a.now += 60
            b.now += 60
            t2, l2 = make_link(a)
            paste(b, t2)
            wire.move(b)                      # A takes link 2
            self.assertEqual(contact(a)["pair"], l2, order)
            acks = {o["link"]: oid for oid, o in answers(a).items()}
            for lid in ((l1, l2) if order == "12" else (l2, l1)):
                move_one(wire, a, acks[lid])
            wire.settle()
            self.assertEqual({j["state"] for j in b.bus["joins"].values()}, {"linked"}, order)
            self.assertEqual(contact(b)["pair"], l2, order)
            self.assertEqual(bus.contact_code(contact(a)), bus.contact_code(contact(b)), order)
            self.assertIsNotNone(bus.contact_code(contact(b)), order)
            # the last notice names the pair's code; a link done after the pair and losing to it tells nothing
            told = [n["text"] for n in notices(b) if re.search(r"\d{3}-\d{3}", n["text"])]
            self.assertIn(bus.contact_code(contact(b)), told[-1], order)
            self.assertEqual(len(told), 2 if order == "12" else 1, (order, told))

    def test_a_link_pasted_first_but_never_done_does_not_take_the_pair(self):
        """A makes link 1, then link 2; B pastes 2 (the contact is made by it), then 1; only link 1 goes through, then
        A refuses link 2: both hubs keep link 1 and its code, and the refusal of 2 does not take B's contact."""
        a, b = self.a, self.b
        t1, l1 = make_link(a)
        a.now += 60
        b.now += 60
        t2, l2 = make_link(a)
        paste(b, t2)
        paste(b, t1)
        intros = {o["link"]: oid for oid, o in answers(b, "link").items()}
        move_one(self.wire, b, intros[l1])
        self.wire.move(a)
        self.assertEqual(b.bus["joins"][l1]["state"], "linked")
        self.assertEqual((contact(a)["pair"], contact(b)["pair"]), (l1, l1))
        self.assertEqual(bus.contact_code(contact(b)), bus.contact_code(contact(a)))
        a.ask("bus.link_revoke_requested", {"id": l2})
        self.wire.settle()
        self.assertEqual(b.bus["joins"][l2]["state"], "refused")
        self.assertEqual(len(b.bus["contacts"]), 1)
        self.assertEqual(contact(b)["pair"], l1)
        self.assertEqual(bus.contact_code(contact(b)), bus.contact_code(contact(a)))

    def test_only_the_ack_reveals_the_nonce(self):
        """The nonce leaves A only as the id of the ack to the hub that used the link — never in the link, never in a
        refusal (used, revoked, expired), never twice to different hubs."""
        from tests.test_hubbus_s1 import LiveHub, Wire
        text, lid = make_link(self.a)
        nonce = self.a.bus["links"][lid]["nonce"]
        self.assertNotIn(nonce, text)
        paste(self.b, text)
        self.wire.move(self.b)
        acks = answers(self.a)
        self.assertEqual([o["id"] for o in acks.values()], [nonce])
        c = LiveHub("Hub C")                      # a second hub with the same link: refused, and not with the nonce
        self.wire = Wire(self.a, self.b, c)
        paste(c, text)
        self.wire.move(c)
        refusals = answers(self.a, "link_refused")
        self.assertEqual(len(refusals), 1)
        self.assertNotIn(nonce, [o["id"] for o in refusals.values()])
        self.wire.settle()
        self.assertEqual(joined(c)["state"], "refused")
        self.assertEqual(joined(self.b)["state"], "linked")

    def test_a_link_cannot_be_pasted_again_once_the_introduction_went(self):
        """Each paste is a new nonce of B: a hub in the middle refusing until the code suits it would get a try per
        paste — after the introduction went, the same link is refused («pasted»); a new link is needed."""
        text, lid = make_link(self.a)
        self.a.ask("bus.link_revoke_requested", {"id": lid})
        paste(self.b, text)
        self.wire.settle()
        self.assertEqual(joined(self.b)["state"], "refused")
        self.assertTrue(joined(self.b).get("intro_id"))
        paste(self.b, text)
        self.assertEqual(self.b.bus["last"]["code"], "pasted")
        self.assertEqual(len(self.b.bus["joins"]), 1)


def answers(hub, field="link_ack"):
    """The introductions with a link field the hub has signed and not handed yet: {out: {link: id, id: envelope id}}."""
    out = {}
    for oid, o in hub.bus["out"].items():
        if o.get("status") == "signed" and o.get("kind") == "intro":
            head = json.loads(json.loads(base64.b64decode(o["envelope"]))["head"])
            if field in head:
                out[oid] = {"link": head[field]["id"], "id": head["id"]}
    return out


def move_one(wire, src, oid):
    """The folder transport for one envelope of `src` (Wire.move moves them all)."""
    keep = {k: v for k, v in src.bus["out"].items() if k != oid and v.get("status") == "signed"}
    for v in keep.values():
        v["status"] = "held-by-test"
    try:
        wire.move(src)
    finally:
        for v in keep.values():
            v["status"] = "signed"


class AnswerAgain(unittest.TestCase):
    """A's answer sent again has the same id (the nonce) and new bytes: while the first waits on the relay it says
    409 conflict — for an introduction of ours that is the same answer waiting, handed; for anything else — a retry."""

    def run_hand(self, kind):
        import tempfile
        from unittest import mock
        from core import busrelay
        from modules.bus_relay.run import Transport
        state = tempfile.mkdtemp(prefix="l0b-")
        os.makedirs(busd.out_dir(state))
        with open(os.path.join(busd.out_dir(state), "i1.env"), "wb") as f:
            f.write(b"{}")
        put = []
        t = Transport(state, put=lambda k, d: put.append((k, d)), clock=lambda: 1000, log=lambda text: None)

        class Client:
            def allow(self, fps):
                pass

            def send(self, fp, raw):
                raise busrelay.Error("conflict", "another envelope with this id is waiting", 409)

        view = {"relay": {"url": "x"}, "out": [{"out": "i1", "fp": "hb1-" + "a" * 26, "kind": kind,
                                                 "status": "signed"}]}
        with mock.patch.object(Transport, "chores"), mock.patch.object(Transport, "client", lambda s, v: Client()):
            t.hand(view)
        return put, t

    def test_an_introduction_already_waiting_is_handed(self):
        put, t = self.run_hand("intro")
        self.assertEqual(put, [("bus.handed", {"out": "i1", "via": "relay"})])

    def test_a_message_with_a_conflict_is_tried_again(self):
        put, t = self.run_hand("message")
        self.assertEqual(put, [])
        self.assertEqual(t.tries["i1"][0], 1)


def keys_of(hub):
    return bus.load(bus.key_dir(hub.config))


if __name__ == "__main__":
    unittest.main()
