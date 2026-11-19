"""The hub bus, slice L0 (spec hubbus/ux §4, §5, §12): linking two hubs by one line — the link `hub-link-1:` (the card
of A, a relay permit, a one-time key), `link`/`link_ack`/`link_refused` in an introduction of protocol version 2, the
records `links` (on A) and `joins` (on B) and their steps, the table «an existing contact», the pair code both pages
show, the owners' chat (`for: owner`), the gate of the check (`verified`) on receipt and on sending, `unlink`,
`card_update`, the version agreement (`bus_v`), the masking of a link in the journal.

Two cores of the stand in one process (tests/test_hubbus_s1.LiveHub) pass envelopes through a folder; the relay is in
tests/test_hubbus_l0_relay.py. Without cryptography ≥ 47 and ssh-keygen these tests are skipped with the reason."""
import tests  # noqa: F401 — keeps the machine's settings out
import base64
import hashlib
import json
import os
import time
import unittest
from unittest import mock

from tests.test_hubbus import LIB_OK, NOW, need_lib, need_ssh
from core import bus, busd, busflow, busstate, journal, liaison, redact, reducer

OWNER = {"channel": "cli", "owner": True}
SESSION = {"channel": "session", "owner": False, "sender": "api"}


def fps(n):
    """Fingerprints of the right form, made from fixed bytes."""
    return [bus.fingerprint(bytes([i]) * 32, bytes([i + 100]) * 32) for i in range(n)]


class Masking(unittest.TestCase):
    LINK = "hub-link-1:" + "eyJ2IjoxLCJtaW5fYnVzX3YiOjJ9" + "A" * 300

    def test_redact_masks_a_link(self):
        self.assertNotIn("eyJ2Ijox", redact.mask(f"here is the link {self.LINK} — paste it"))
        self.assertIn(redact.MASK, redact.mask(self.LINK))

    def test_the_journal_masks_a_link_in_any_event(self):
        j = journal.Journal.__new__(journal.Journal)
        j.seq = 0
        for type_, data in (("message.received", {"text": f"link {self.LINK}", "to": "hub"}),
                            ("session.said", {"text": self.LINK}), ("turn.tool", {"input": {"cmd": self.LINK}})):
            ev = j.event(type_, "x", data)
            self.assertNotIn("eyJ2Ijox", json.dumps(ev), type_)
        ev = j.event("message.received", "x", {"text": "hub-link-1 without a colon and hub-link-1:"})
        self.assertEqual(ev["data"]["text"], "hub-link-1 without a colon and hub-link-1:")

    def test_a_wrapped_link_is_masked_whole(self):
        """A mail client wraps the link at 76 columns: the core reads it whole (read_link drops white space), so the
        mask takes it across the breaks."""
        body = self.LINK[len("hub-link-1:"):]
        wrapped = "hub-link-1:\n" + "\n".join(body[i:i + 76] for i in range(0, len(body), 76))
        j = journal.Journal.__new__(journal.Journal)
        j.seq = 0
        ev = j.event("message.received", "x", {"text": f"take it:\n{wrapped}\nthen link it and tell me"})
        for i in range(0, len(body) - 20, 76):
            self.assertNotIn(body[i:i + 20], json.dumps(ev))
        self.assertIn("then link it and tell me", ev["data"]["text"])   # the words after the link stay
        self.assertNotIn(body[100:120], redact.mask(wrapped))

    def test_a_word_after_a_link_stays_even_when_it_is_long(self):
        """A link on one line is not wrapped: the next line is text, even if its first word is a 16-character run
        (a file name, an id)."""
        word = "supercalifragilistic_name"
        for before, after in (("", f"\n{word} onward"), ("here ", f"\n\n{word}"), ("", f"\r\n  {word}"), ("", "\n"), ("", "")):
            self.assertEqual(redact.mask(before + self.LINK + after), before + redact.MASK + after)
        self.assertEqual(redact.mask(f"before {self.LINK} after"), f"before {redact.MASK} after")
        j = journal.Journal.__new__(journal.Journal)
        j.seq = 0
        ev = j.event("message.received", "x", {"text": f"{self.LINK}\n{word}"})
        self.assertEqual(ev["data"]["text"], f"{redact.MASK}\n{word}")

    def test_a_link_wrapped_narrow_is_masked_whole(self):
        body = self.LINK[len("hub-link-1:"):]
        inv = "hub-relay-invite-1:" + "x" * 90 + "." + body
        for width in (20, 30, 40):
            wrapped = "hub-link-1:\n" + "\n".join(body[i:i + width] for i in range(0, len(body), width))
            out = redact.mask(f"{wrapped}\nthen paste")
            self.assertNotIn("AAAAAAAAAAAAAAAA", out)
            self.assertIn(redact.MASK, out)
            self.assertTrue(out.endswith("\nthen paste"), (width, out[-40:]))
            text = "\n".join(inv[i:i + width] for i in range(0, len(inv), width))
            self.assertNotIn("yyyy", redact.mask_links(text))
            self.assertNotIn("AAAAAAAAAAAAAAAA", redact.mask_links(text))

    def test_a_word_after_an_invitation_stays_even_when_it_is_long(self):
        word = "supercalifragilistic_name"
        inv = "hub-relay-invite-1:" + "x" * 90 + "." + "y" * 400
        out = redact.mask_links(f"{inv}\n{word} onward")
        self.assertEqual(out, f"hub-relay-invite-1:{'x' * 90}.{redact.MASK}\n{word} onward")
        self.assertEqual(redact.mask_links(inv), f"hub-relay-invite-1:{'x' * 90}.{redact.MASK}")

    def test_a_wrapped_invitation_is_masked_whole(self):
        sig = "".join("abcdefghijklmnopqrstuvwxyz0123456789"[(i * 7) % 36] for i in range(400))
        inv = "hub-relay-invite-1:" + "x" * 90 + "." + sig
        wrapped = "\n".join(inv[i:i + 76] for i in range(0, len(inv), 76))
        out = redact.mask_links(f"take it:\n{wrapped}\nthen paste")
        self.assertNotIn(sig[100:120], out)
        self.assertTrue(out.endswith(f"{redact.MASK}\nthen paste"), out[-60:])


# --- the envelopes of version 2 ---------------------------------------------------------------------------------------

def head(t="box", **kw):
    h = {"v": 2, "t": t, "id": "a" * 32, "ts": NOW, "exp": NOW + 60}
    if t == "box":
        a, b = fps(2)
        h.update({"from": a, "to": b, "cls": "liaison", "ct": base64.b64encode(bytes(32 + 1024 + 16)).decode()})
    else:
        h["card"] = {"label": "A", "sign_pub": base64.b64encode(bytes(32)).decode(),
                     "enc_pub": base64.b64encode(bytes(32)).decode(), "relays": [], "agents": {}, "bus_v": 2}
    h.update(kw)
    return json.dumps({k: v for k, v in h.items() if v is not ...})


class Heads(unittest.TestCase):
    def ok(self, text):
        return bus.parse_head(text)

    def bad(self, text):
        with self.assertRaises(bus.Error):
            bus.parse_head(text)

    def test_box_v2_has_a_class_and_v1_has_none(self):
        for cls in ("chat", "line", "liaison", "ctl"):
            self.assertEqual(self.ok(head(cls=cls))["cls"], cls)
        self.bad(head(cls="other"))
        self.bad(head(cls=...))                          # v2 without a class
        self.bad(head(v=1))                              # v1 with a class
        self.ok(head(v=1, cls=...))                       # v1 as it was

    def test_intro_v2_with_at_most_one_link_field_of_exact_form(self):
        a, b = fps(2)
        sig = base64.b64encode(bytes(64)).decode()
        self.ok(head("intro"))
        self.ok(head("intro", link={"id": "b" * 32, "sig": sig}))
        self.ok(head("intro", link_ack={"id": "b" * 32, "fpB": b, "intro_id": "c" * 32}))
        self.ok(head("intro", link_refused={"id": "b" * 32, "fpB": b, "code": "expired"}))
        self.bad(head("intro", link={"id": "b" * 32, "sig": sig},
                      link_ack={"id": "b" * 32, "fpB": b, "intro_id": "c" * 32}))
        self.bad(head("intro", link={"id": "b" * 32, "sig": sig, "x": 1}))
        self.bad(head("intro", link={"id": "B" * 32, "sig": sig}))
        self.bad(head("intro", link={"id": "b" * 32, "sig": "a b"}))
        self.bad(head("intro", link_refused={"id": "b" * 32, "fpB": b, "code": "Expired!"}))
        self.bad(head("intro", v=1, link={"id": "b" * 32, "sig": sig}))   # link only in v2


@need_lib
class Inner(unittest.TestCase):
    def test_for_owner_only_in_v2_and_only_owner(self):
        m = {**bus.message("liaison", "hello", thread="d" * 32), "for": "owner"}
        self.assertEqual(bus._inner(bus.encode_inner(m), v=2)["for"], "owner")
        with self.assertRaises(bus.Error):
            bus._inner(bus.encode_inner(m), v=1)
        with self.assertRaises(bus.Error):
            bus._inner(bus.encode_inner({**m, "for": "liaison"}), v=2)

    def test_unlink_and_card_update_have_exact_fields(self):
        ok = [{"kind": "unlink", "pair_id": "e" * 32},
              {"kind": "card_update", "label": "Hub", "agents": {"api": "API"}}]
        for d in ok:
            self.assertEqual(bus._inner(bus.encode_inner(d), v=2), d)
            with self.assertRaises(bus.Error):
                bus._inner(bus.encode_inner({**d, "x": 1}), v=2)
            with self.assertRaises(bus.Error):
                bus._inner(bus.encode_inner(d), v=1)

    def test_the_class_matches_the_kind(self):
        m = bus.message("liaison", "x", thread="d" * 32)
        self.assertEqual(bus.class_of(m), "liaison")
        self.assertEqual(bus.class_of({**m, "for": "owner"}), "chat")
        self.assertEqual(bus.class_of({"kind": "unlink", "pair_id": "e" * 32}), "ctl")
        self.assertEqual(bus.class_of({"kind": "card_update", "label": "x", "agents": {}}), "ctl")


# --- the link -----------------------------------------------------------------------------------------------------------

@need_lib
@need_ssh
class Link(unittest.TestCase):
    def setUp(self):
        from tests.test_hubbus_s1 import LiveHub
        self.a = LiveHub("Hub A")

    def keys(self, hub):
        with hub.env():
            return bus.load(bus.key_dir(hub.config))

    def test_made_and_read_back(self):
        keys = self.keys(self.a)
        text, rec, _ = bus.make_link(keys, "Hub A", NOW)
        self.assertTrue(text.startswith("hub-link-1:"))
        got = bus.read_link(text, NOW + 10)
        self.assertEqual(got["id"], rec["id"])
        self.assertEqual(got["card"]["fp"], keys.fp)
        self.assertEqual(got["card"]["agents"], {})
        self.assertEqual(got["exp"], NOW + 86400)
        self.assertEqual(rec["exp"], NOW + 86400)
        self.assertIsNone(got["invite"])
        self.assertNotIn(got["seed"], json.dumps(rec))   # A keeps only the public half

    def test_refusals(self):
        keys = self.keys(self.a)
        text, rec, _ = bus.make_link(keys, "Hub A", NOW)
        with self.assertRaises(bus.Error) as e:
            bus.read_link(text, NOW + 86400)
        self.assertEqual(e.exception.code, "stale")
        body = json.loads(bus._unb64url(text[len(bus.LINK_PREFIX):]))
        for change, code in (({"min_bus_v": 3}, "old_hub"), ({"v": 2}, "bad_link"), ({"k": "AAAA"}, "bad_link"),
                             ({"id": "X" * 32}, "bad_link"), ({"extra": 1}, "bad_link")):
            bad = bus.LINK_PREFIX + bus._b64url(json.dumps({**body, **change}).encode())
            with self.assertRaises(bus.Error, msg=change) as e:
                bus.read_link(bad, NOW)
            self.assertEqual(e.exception.code, code, change)
        card = bus._unb64(body["card"])
        forged = card.replace(b"\\u0425", b"X") if b"\\u0425" in card else card.replace("Hub".encode(), "Hob".encode())
        bad = bus.LINK_PREFIX + bus._b64url(json.dumps({**body, "card": bus._b64(forged)}).encode())
        with self.assertRaises(bus.Error) as e:
            bus.read_link(bad, NOW)
        self.assertIn(e.exception.code, ("bad_signature", "bad_format"))
        with self.assertRaises(bus.Error):
            bus.read_link("hub-bus-card-1:xyz", NOW)

    def test_the_permit_must_be_of_the_hub_of_the_card(self):
        from core import busrelay
        keys = self.keys(self.a)
        relay = {"url": "https://relay.example.test", "key": base64.b64encode(bytes(range(32))).decode()}
        relay["ref"] = busrelay.ref(relay["url"], relay["key"])
        text, rec, _ = bus.make_link(keys, "Hub A", NOW, relay=relay)
        got = bus.read_link(text, NOW)
        self.assertEqual(got["invite"]["by"], keys.fp)
        self.assertEqual(got["invite"]["exp"], rec["exp"])
        self.assertEqual(rec["invite"], got["invite"]["id"])
        other = self.keys(__import__("tests.test_hubbus_s1", fromlist=["LiveHub"]).LiveHub("B"))
        inv, _, _ = busrelay.make_invite(other, relay, NOW)
        body = json.loads(bus._unb64url(text[len(bus.LINK_PREFIX):]))
        bad = bus.LINK_PREFIX + bus._b64url(json.dumps({**body, "relay": inv}).encode())
        with self.assertRaises(bus.Error) as e:
            bus.read_link(bad, NOW)
        self.assertEqual(e.exception.code, "bad_link")

    def test_the_longest_link_fits_a_telegram_message(self):
        from core import busrelay
        keys = self.keys(self.a)
        key = base64.b64encode(bytes(range(32))).decode()
        tail = len(busrelay.ref("", key))                 # «#» and the relay's key: the card's entry is ≤ 200 in all
        url = "https://relay-" + "x" * (200 - tail - len("https://relay-") - len(".example.test")) + ".example.test"
        relay = {"url": url, "key": key, "ref": busrelay.ref(url, key)}
        self.assertEqual(len(relay["ref"]), 200)
        text, _, _ = bus.make_link(keys, "é" * 100, NOW, relay=relay, ttl=604800)
        self.assertLessEqual(len(text), 4096, len(text))
        self.assertEqual(bus.read_link(text, NOW)["card"]["label"], "é" * 100)

    def test_the_proof_of_the_one_time_key(self):
        keys = self.keys(self.a)
        text, rec, _ = bus.make_link(keys, "Hub A", NOW)
        got = bus.read_link(text, NOW)
        a, b = fps(2)
        sig = bus.link_proof(got["seed"], a, b, "c" * 32)
        self.assertEqual(len(sig), 88)
        self.assertTrue(bus.link_proof_ok(rec["pub"], sig, a, b, "c" * 32))
        for args in ((b, a, "c" * 32), (a, b, "d" * 32)):
            self.assertFalse(bus.link_proof_ok(rec["pub"], sig, *args))
        other, _, _ = bus.make_link(keys, "Hub A", NOW)
        self.assertFalse(bus.link_proof_ok(rec["pub"], bus.link_proof(bus.read_link(other, NOW)["seed"], a, b, "c" * 32),
                                           a, b, "c" * 32))
        self.assertFalse(bus.link_proof_ok(rec["pub"], "x" * 88, a, b, "c" * 32))


# --- two hubs linked through a folder -------------------------------------------------------------------------------

def make_link(hub, ttl=None):
    """The owner of `hub` presses «Link with a hub»: (the link text, its id)."""
    before = set(hub.bus.get("links") or {})
    ev = hub.ask("bus.link_requested", {**({"ttl": ttl} if ttl else {})})
    assert hub.verdict(ev) is None, hub.verdict(ev)
    (lid,) = set(hub.bus["links"]) - before
    with open(busd.link_path(hub.h.state_dir, lid), encoding="ascii") as f:
        return f.read().strip(), lid


def paste(hub, text):
    """The owner of `hub` pastes a link and presses «Link»: the page hands it to the core as a file (0600)."""
    folder = busd.join_dir(hub.h.state_dir)
    os.makedirs(folder, exist_ok=True)
    raw = text.encode("ascii")
    digest = hashlib.sha256(raw).hexdigest()
    path = os.path.join(folder, digest + ".link")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    return hub.ask("bus.join_requested", {"path": path, "sha256": digest})


def joined(hub):
    (rec,) = hub.bus["joins"].values()
    return rec


def tick(hub):
    """A tick of that hub's core (its timers, a join going on from its step), with its folders; the throttle of
    retries is reset — the test's time does not run between two ticks."""
    hub.h.__dict__.pop("bus_link_tried", None)
    with hub.env():
        busd.tick(hub.h)


def notices(hub):
    out = []
    with open(os.path.join(hub.h.state_dir, "journal.jsonl"), encoding="utf-8") as f:
        for line in f:
            e = json.loads(line)
            if e["type"] == "owner.notify":
                out.append(e["data"])
    return out


def journal_text(hub):
    with open(os.path.join(hub.h.state_dir, "journal.jsonl"), encoding="utf-8") as f:
        return f.read()


@need_lib
@need_ssh
class Linking(unittest.TestCase):
    def setUp(self):
        from tests.test_hubbus_s1 import LiveHub, Wire
        self.a, self.b = LiveHub("Hub A"), LiveHub("Hub B")
        self.wire = Wire(self.a, self.b)

    def link(self):
        text, lid = make_link(self.a)
        ev = paste(self.b, text)
        self.assertIsNone(self.b.verdict(ev))
        self.wire.settle()
        return text, lid

    def contact(self, hub):
        (name, c), = hub.bus["contacts"].items()
        return name, c

    def test_one_link_two_contacts_the_same_pair_code(self):
        text, lid = self.link()
        a, b = self.a, self.b
        self.assertEqual(a.bus["links"][lid]["state"], "used")
        self.assertEqual(a.bus["links"][lid]["used_fp"], b.bus["me"]["fp"])
        self.assertEqual(joined(b)["step"], "linked")
        na, ca = self.contact(a)
        nb, cb = self.contact(b)
        self.assertEqual((ca["fp"], cb["fp"]), (b.bus["me"]["fp"], a.bus["me"]["fp"]))
        for c in (ca, cb):
            self.assertEqual((c["status"], c["verified"], c["via"], c["pair"]), ("known", False, "link", lid))
            self.assertEqual(c["bus_v"], 2)
        code = bus.contact_code(ca)   # the pair code with the commitment: tests/test_hubbus_l0b.py
        self.assertEqual(code, bus.contact_code(cb))
        self.assertTrue(any(code in n["text"] for n in notices(a)), notices(a))
        self.assertTrue(any(code in n["text"] for n in notices(b)), notices(b))
        # the secret never reached a journal
        seed = bus.read_link(text, a.now)["seed"]
        for hub in (a, b):
            with hub.env():
                busd.write_view(hub.h)
            kept = journal_text(hub) + json.dumps(hub.h.state, default=str)
            for name in ("view.json", "transport.json"):
                with open(os.path.join(hub.h.state_dir, "bus", name), encoding="utf-8") as f:
                    kept += f.read()
            self.assertNotIn(seed, kept)
            self.assertNotIn(text[len(bus.LINK_PREFIX):][:40], kept)
        # the link file of A and the join file of B are gone once the link is done
        self.assertFalse(os.path.exists(busd.link_path(a.h.state_dir, lid)))
        self.assertEqual(os.listdir(busd.join_dir(b.h.state_dir)), [])
        self.assertFalse(os.path.exists(busd.joined_path(b.h.state_dir, lid)))

    def test_the_link_file_is_the_owners_only(self):
        if os.name == "nt":
            self.skipTest("POSIX modes")
        _, lid = make_link(self.a)
        self.assertEqual(os.stat(busd.link_path(self.a.h.state_dir, lid)).st_mode & 0o777, 0o600)

    def test_only_the_owner_makes_pastes_revokes_and_chats(self):
        a = self.a
        a.up("api")
        for kind, data in (("bus.link_requested", {}), ("bus.join_requested", {"path": "/x", "sha256": "0" * 64}),
                           ("bus.link_revoke_requested", {"id": "a" * 32}), ("bus.join_cancel_requested", {"id": "a" * 32}),
                           ("bus.chat_requested", {"contact": "x", "text": "hi"})):
            for who in (SESSION, {"channel": "plugin", "owner": False, "sender": "mod:x"},
                        {"channel": "mod:x", "owner": True}):
                ev = a.rec(kind, {**data, **who})
                self.assertIn("owner", a.verdict(ev) or "", (kind, who))

    def test_a_link_from_a_file_outside_the_cores_folder_is_refused(self):
        text, _ = make_link(self.a)
        path = os.path.join(self.b.h.state_dir, "elsewhere.link")
        with open(path, "w", encoding="ascii") as f:
            f.write(text)
        self.b.ask("bus.join_requested", {"path": path, "sha256": hashlib.sha256(text.encode()).hexdigest()})
        self.assertEqual(self.b.bus.get("joins") or {}, {})
        self.assertEqual(self.b.bus["last"]["code"], "outside")

    def test_own_link(self):
        text, _ = make_link(self.a)
        paste(self.a, text)
        self.assertEqual(self.a.bus.get("joins") or {}, {})
        self.assertEqual(self.a.bus["last"]["code"], "own_link")

    def test_a_stale_link_on_b(self):
        text, _ = make_link(self.a)
        self.b.now = self.a.now + 86400 + 1
        paste(self.b, text)
        self.assertEqual(self.b.bus["last"]["code"], "stale")
        self.assertEqual(self.b.bus["contacts"], {})

    # the refusals of A (§5.1, §4.2 step 6)
    def test_a_takes_the_link_by_its_own_clock_expired_is_refused_with_a_signed_answer(self):
        text, lid = make_link(self.a)
        paste(self.b, text)
        self.a.now += 86400 + 1          # A was offline; B's envelope says it was sent in time
        self.b.now = self.a.now          # the clocks agree; B still waits (its term is the link's plus an hour)
        self.wire.settle()
        self.assertEqual(self.a.bus["links"][lid]["state"], "expired")
        self.assertEqual(self.a.bus["contacts"], {})
        rec = joined(self.b)
        self.assertEqual((rec["state"], rec.get("code")), ("refused", "expired"))
        self.assertEqual(self.b.bus["contacts"], {})   # the contact this join made goes

    def test_used_by_another_hub_second_b_is_refused(self):
        from tests.test_hubbus_s1 import LiveHub, Wire
        c = LiveHub("Hub C")
        wire = Wire(self.a, self.b, c)
        text, lid = make_link(self.a)
        paste(self.b, text)
        paste(c, text)
        wire.settle()
        self.assertEqual(self.a.bus["links"][lid]["state"], "used")
        winner = self.a.bus["links"][lid]["used_fp"]
        loser = c if winner == self.b.bus["me"]["fp"] else self.b
        self.assertEqual((joined(loser)["state"], joined(loser).get("code")), ("refused", "used"))
        self.assertEqual(len(self.a.bus["contacts"]), 1)
        self.assertEqual(loser.bus["contacts"], {})

    def test_revoked_link_is_refused_and_its_file_goes(self):
        text, lid = make_link(self.a)
        ev = self.a.ask("bus.link_revoke_requested", {"id": lid})
        self.assertIsNone(self.a.verdict(ev))
        self.assertEqual(self.a.bus["links"][lid]["state"], "revoked")
        self.assertFalse(os.path.exists(busd.link_path(self.a.h.state_dir, lid)))
        paste(self.b, text)
        self.wire.settle()
        self.assertEqual((joined(self.b)["state"], joined(self.b).get("code")), ("refused", "revoked"))

    def test_a_wrong_one_time_key_gets_nothing(self):
        text, lid = make_link(self.a)
        body = json.loads(bus._unb64url(text[len(bus.LINK_PREFIX):]))
        body["k"] = bus._b64url(os.urandom(32))
        paste(self.b, bus.LINK_PREFIX + bus._b64url(json.dumps(body).encode()))
        self.wire.settle()
        self.assertEqual(self.a.bus["links"][lid]["state"], "open")
        self.assertEqual(self.a.bus["contacts"], {})
        self.assertEqual(joined(self.b)["step"], "intro_sent")    # no answer: B waits
        self.assertIn('"code":"bad_link"', journal_text(self.a).replace(" ", ""))

    def test_an_unknown_link_id_gets_nothing(self):
        text, lid = make_link(self.a)
        body = json.loads(bus._unb64url(text[len(bus.LINK_PREFIX):]))
        body["id"] = "f" * 32
        paste(self.b, bus.LINK_PREFIX + bus._b64url(json.dumps(body).encode()))
        self.wire.settle()
        self.assertEqual(self.a.bus["contacts"], {})
        self.assertEqual(joined(self.b)["step"], "intro_sent")

    # the failures of §4.3
    def test_b_cut_before_the_introduction_continues_from_its_step(self):
        text, lid = make_link(self.a)
        with mock.patch.object(busd, "join_intro", side_effect=OSError("cut")):
            paste(self.b, text)
        self.assertEqual(joined(self.b)["step"], "contact")
        tick(self.b)
        tick(self.b)
        self.assertEqual(joined(self.b)["step"], "intro_sent")
        intros = [o for o in self.b.bus["out"].values() if o.get("kind") == "intro"]
        self.assertEqual(len(intros), 1)
        self.wire.settle()
        self.assertEqual(joined(self.b)["step"], "linked")
        self.assertEqual(len(self.b.bus["contacts"]), 1)

    def test_a_lost_answer_the_same_introduction_again_gets_the_same_ack(self):
        text, lid = make_link(self.a)
        paste(self.b, text)
        (oid, o), = [(k, x) for k, x in self.b.bus["out"].items() if x.get("kind") == "intro"]
        raw = base64.b64decode(o["envelope"])
        self.wire.move(self.b)                   # B → A: A answers
        self.a.bus["out"].clear()                # the answer is lost on the way
        folder = busd.in_dir(self.a.h.state_dir)
        path = os.path.join(folder, hashlib.sha256(raw).hexdigest() + ".env")
        with open(path, "wb") as f:
            f.write(raw)
        self.a.ask("bus.offered", {"path": path, "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)})
        self.wire.settle()
        self.assertEqual(joined(self.b)["step"], "linked")
        self.assertEqual(len(self.a.bus["contacts"]), 1)

    def test_used_and_the_contact_are_one_fact(self):
        self.link()
        with open(os.path.join(self.a.h.state_dir, "journal.jsonl"), encoding="utf-8") as f:
            evs = [json.loads(x) for x in f]
        used = [e for e in evs if e["type"] == "core.bus_link_used"]
        self.assertEqual(len(used), 1)
        self.assertFalse([e for e in evs if e["type"] == "core.bus_contact_added"])
        st = reducer.empty()
        for e in evs:
            if e["type"] == "core.bus_link_used":
                break
            st = reducer.reduce(st, e)[0]
        self.assertEqual(st["bus"]["links"][used[0]["data"]["id"]]["state"], "open")
        self.assertEqual(st["bus"]["contacts"], {})
        st, _, why = reducer.reduce(st, used[0])
        self.assertIsNone(why)
        self.assertEqual(st["bus"]["links"][used[0]["data"]["id"]]["state"], "used")
        self.assertEqual(len(st["bus"]["contacts"]), 1)

    def test_a_late_ack_after_the_waiting_time_still_links(self):
        text, lid = make_link(self.a)
        paste(self.b, text)
        self.wire.move(self.b)
        self.b.now = self.b.now + 86400 + 3601
        tick(self.b)
        self.assertEqual(joined(self.b)["state"], "expired")
        self.wire.settle()
        self.assertEqual((joined(self.b)["state"], joined(self.b)["step"]), ("linked", "linked"))

    def test_cancel_before_and_after_the_introduction(self):
        text, lid = make_link(self.a)
        with mock.patch.object(busd, "join_intro", side_effect=OSError("cut")):
            paste(self.b, text)
        jid = next(iter(self.b.bus["joins"]))
        self.b.ask("bus.join_cancel_requested", {"id": jid})
        self.assertEqual(joined(self.b)["state"], "cancelled")
        self.assertEqual(self.b.bus["contacts"], {})
        tick(self.b)
        self.assertEqual([o for o in self.b.bus["out"].values() if o.get("kind") == "intro"], [])

    def test_cancel_after_the_introduction_unlinks_when_the_ack_comes(self):
        text, lid = make_link(self.a)
        paste(self.b, text)
        jid = next(iter(self.b.bus["joins"]))
        self.b.ask("bus.join_cancel_requested", {"id": jid})
        self.assertEqual(joined(self.b)["state"], "cancelling")
        self.wire.settle()
        self.assertEqual(joined(self.b)["state"], "cancelled")
        self.assertEqual(self.b.bus["contacts"], {})
        self.assertEqual(self.a.bus["contacts"], {})         # A got the unlink

    def test_cancel_after_the_introduction_without_an_answer_drops_the_contact(self):
        text, lid = make_link(self.a)
        paste(self.b, text)
        jid = next(iter(self.b.bus["joins"]))
        self.b.ask("bus.join_cancel_requested", {"id": jid})
        self.b.now += 86400 + 3601
        tick(self.b)
        self.assertEqual(joined(self.b)["state"], "cancelled")
        self.assertEqual(self.b.bus["contacts"], {})

    def test_a_cut_between_used_and_the_answer_the_tick_sends_it(self):
        text, lid = make_link(self.a)
        paste(self.b, text)
        with mock.patch.object(busd, "link_acks"):          # hubd fell after core.bus_link_used, before the ack
            self.wire.move(self.b)
        self.assertEqual(self.a.bus["links"][lid]["state"], "used")
        self.assertTrue(self.a.bus["links"][lid].get("ack"))
        self.assertEqual([o for o in self.a.bus["out"].values() if o.get("kind") == "intro"], [])
        tick(self.a)
        self.assertFalse(self.a.bus["links"][lid].get("ack"))
        tick(self.a)
        self.assertEqual(len([o for o in self.a.bus["out"].values() if o.get("kind") == "intro"]), 1)   # once
        self.wire.settle()
        self.assertEqual(joined(self.b)["step"], "linked")

    def test_a_cut_before_the_fact_leaves_files_the_tick_removes(self):
        with mock.patch.object(self.a.h, "record", side_effect=OSError("cut")):
            with self.a.env(), self.assertRaises(OSError):
                busd.link(self.a.h, {"ttl": 86400})
        self.assertEqual(self.a.bus["links"], {})
        folder = os.path.dirname(busd.link_path(self.a.h.state_dir, "x"))
        self.assertEqual(len(os.listdir(folder)), 1)
        tick(self.a)
        self.assertEqual(os.listdir(folder), [])

    def test_crossed_links_end_with_one_pair_on_both_hubs(self):
        from tests.test_hubbus_s1 import LiveHub, Wire
        for order in ("ab", "ba"):
            a, b = LiveHub("Hub A"), LiveHub("Hub B")
            wire = Wire(a, b)
            la, ida = make_link(a)
            lb, idb = make_link(b)
            paste(b, la)
            paste(a, lb)
            first, second = (b, a) if order == "ab" else (a, b)
            wire.move(first)                 # one introduction arrives first, its answer and the other cross
            wire.move(second)
            wire.settle()
            (ca,), (cb,) = a.bus["contacts"].values(), b.bus["contacts"].values()
            self.assertEqual(ca["pair"], cb["pair"], order)
            self.assertEqual(ca["pair"], min(ida, idb), order)
            self.assertEqual({joined(a)["state"], joined(b)["state"]}, {"linked"}, order)
            # an unlink of either side takes the contact on the other
            name = next(iter(a.bus["contacts"]))
            a.ask("bus.contact_requested", {"op": "remove", "name": name})
            wire.settle()
            self.assertEqual(b.bus["contacts"], {}, order)

    def test_a_join_that_never_introduced_itself_expires_and_leaves_nothing(self):
        text, lid = make_link(self.a)
        with mock.patch.object(busd, "join_intro"):          # the relay never answered: no introduction made
            paste(self.b, text)
        self.assertEqual(len(self.b.bus["contacts"]), 1)
        self.b.now += 86400 + 3601
        with mock.patch.object(busd, "join_intro"):
            tick(self.b)
        self.assertEqual(joined(self.b)["state"], "expired")
        self.assertEqual(self.b.bus["contacts"], {})
        self.assertFalse(os.path.exists(busd.joined_path(self.b.h.state_dir, lid)))

    def test_a_contact_a_never_confirmed_goes_with_the_joins_record(self):
        text, lid = make_link(self.a)
        paste(self.b, text)                                  # the introduction went; A is gone for good
        self.b.now += 86400 + 3601
        tick(self.b)
        self.assertEqual((joined(self.b)["state"], len(self.b.bus["contacts"])), ("expired", 1))
        self.b.now += busstate.LINKS_KEEP
        tick(self.b)
        self.assertEqual(joined(self.b)["state"], "forgotten")
        self.assertEqual(self.b.bus["contacts"], {})

    def test_revoking_drops_a_permit_not_recorded_yet(self):
        self.a.bus["relay"] = None
        from core import busrelay
        relay = {"url": "https://relay.example.test", "key": base64.b64encode(bytes(range(32))).decode(), "bus_v": 2}
        relay["ref"] = busrelay.ref(relay["url"], relay["key"])
        self.a.bus["relay"] = relay
        _, lid = make_link(self.a)
        self.assertEqual([j["do"] for j in self.a.bus["jobs"].values()], ["invite"])
        self.a.ask("bus.link_revoke_requested", {"id": lid})
        self.assertEqual(self.a.bus["jobs"], {})            # never recorded on the relay: nothing to drop there either

    def test_an_old_joins_cleanup_keeps_a_contact_linked_again_since(self):
        text, lid = make_link(self.a)
        paste(self.b, text)
        self.b.bus["out"].clear()                            # the introduction was lost: A never heard of it
        self.b.now += 86400 + 3601
        tick(self.b)
        self.assertEqual(joined(self.b)["state"], "expired")
        self.a.now = self.b.now
        _, lid2 = self.link()                                # linked again by a fresh link
        (name, c), = self.b.bus["contacts"].items()
        self.assertEqual(c["pair"], lid2)
        self.b.now += busstate.LINKS_KEEP
        tick(self.b)
        self.assertEqual(self.b.bus["joins"][lid]["state"], "forgotten")
        self.assertIn(name, self.b.bus["contacts"])          # the old join's cleanup does not take the live contact

    def test_a_new_link_after_a_finished_one_replaces_the_pair(self):
        self.link()
        (_, c), = self.b.bus["contacts"].items()
        first = c["pair"]
        self.a.now += 60
        self.b.now += 60
        _, second = self.link()
        self.assertEqual(c["pair"], second)
        self.assertEqual(self.a.bus["contacts"][next(iter(self.a.bus["contacts"]))]["pair"], second)
        self.assertNotEqual(first, second)

    # «an existing contact» (§4.5)
    def test_existing_contact_table(self):
        a, b = self.a, self.b
        # pending, the same fingerprint (the old way): known, verified and rights unchanged
        a.ask("bus.contact_requested", {"op": "add", "name": "bee", "card": b.card()})
        self.assertEqual(a.bus["contacts"]["bee"]["status"], "pending")
        self.link()
        c = a.bus["contacts"]["bee"]
        self.assertEqual((c["status"], c["verified"], c["pair"]), ("known", False, next(iter(a.bus["links"]))))
        # known: idempotent, only the pair changes; rights and verified stay
        a.ask("bus.contact_requested", {"op": "verify", "name": "bee"})
        a.ask("bus.contact_requested", {"op": "set", "name": "bee", "may_write": ["api"], "auto_from": ["api"]})
        old = a.bus["contacts"]["bee"]["pair"]
        a.now += 60   # a new link later — the owner's explicit action, not one crossing the first
        b.now += 60
        _, lid2 = self.link()
        c = a.bus["contacts"]["bee"]
        self.assertEqual((c["status"], c["verified"], c["pair"]), ("known", True, lid2))
        self.assertNotEqual(old, lid2)
        self.assertEqual(len(a.bus["contacts"]), 1)

    def test_blocked_contact_beats_the_link(self):
        a, b = self.a, self.b
        a.ask("bus.contact_requested", {"op": "add", "name": "bee", "card": b.card(), "code": b.code()})
        a.ask("bus.contact_requested", {"op": "set", "name": "bee", "status": "blocked"})
        _, lid = self.link()
        self.assertEqual(a.bus["contacts"]["bee"]["status"], "blocked")
        self.assertEqual(a.bus["links"][lid]["state"], "open")
        self.assertEqual(joined(b)["step"], "intro_sent")    # no answer

    def test_quarantine_stays(self):
        a, b = self.a, self.b
        a.ask("bus.contact_requested", {"op": "add", "name": "bee", "card": b.card(), "code": b.code()})
        a.bus["contacts"]["bee"]["quarantine"] = a.now
        self.link()
        self.assertEqual(a.bus["contacts"]["bee"]["quarantine"], a.now)
        self.assertEqual(a.bus["contacts"]["bee"]["status"], "known")

    def test_the_same_name_another_hub_gets_a_suffix(self):
        from tests.test_hubbus_s1 import LiveHub
        a = self.a
        other = LiveHub("Hub B")
        a.ask("bus.contact_requested", {"op": "add", "name": "hub-b", "card": other.card(), "code": other.code()})
        self.link()
        names = sorted(a.bus["contacts"])
        self.assertEqual(len(names), 2, names)
        self.assertNotEqual(a.bus["contacts"][names[0]]["fp"], a.bus["contacts"][names[1]]["fp"])

    def test_after_unlink_a_new_link_is_like_no_contact(self):
        a, b = self.a, self.b
        self.link()
        (name, _), = a.bus["contacts"].items()
        a.ask("bus.contact_requested", {"op": "remove", "name": name})
        self.wire.settle()
        self.assertEqual((a.bus["contacts"], b.bus["contacts"]), ({}, {}))
        self.assertTrue(any("unlinked you" in n["text"] for n in notices(b)), notices(b))
        self.link()
        self.assertEqual(len(a.bus["contacts"]), 1)
        self.assertEqual(len(b.bus["contacts"]), 1)


# --- the check gate, the owners' chat, unlink, the catalog -----------------------------------------------------------

@need_lib
@need_ssh
class Gate(unittest.TestCase):
    def setUp(self):
        from tests.test_hubbus_s1 import LiveHub, Wire
        self.a, self.b = LiveHub("Hub A"), LiveHub("Hub B")
        self.wire = Wire(self.a, self.b)
        text, self.lid = make_link(self.a)
        paste(self.b, text)
        self.wire.settle()
        self.na = next(iter(self.a.bus["contacts"]))
        self.nb = next(iter(self.b.bus["contacts"]))
        self.a.up("api")
        self.b.up("hub-core")

    def test_no_agent_traffic_before_the_check_either_way(self):
        a, b = self.a, self.b
        mid, why = a.say("api", "schema", bus_to=self.na, bus_to_via="local")
        self.assertIsNone(why)
        ev, why = a.as_liaison("bus.send_requested", {"contact": self.na, "text_from": mid})
        self.assertIn("not checked", why or "")
        # a forged envelope of a liaison message from A's keys still is not delivered to B's liaison
        with a.env():
            keys = bus.load(bus.key_dir(a.config))
        _, raw = bus.seal(keys, a.bus["contacts"][self.na], bus.message("api", "for the agent"), a.now, cls="liaison")
        self.offer(b, raw)
        (item,) = b.bus["inbox"].values()
        self.assertEqual(item["status"], "unverified")
        self.assertFalse([m for m in b.messages_to(liaison.NAME) if "for the agent" in m.get("text", "")])
        # receipt says held
        (rec,) = [o for o in b.bus["out"].values() if o.get("kind") == "receipt"]
        self.assertIsNotNone(rec)
        # the owner of B checks the code: the held item goes to the liaison now
        b.ask("bus.contact_requested", {"op": "verify", "name": self.nb})
        self.assertTrue(b.bus["contacts"][self.nb]["verified"])
        self.assertEqual(item["status"], "ok")
        self.assertTrue([m for m in b.messages_to(liaison.NAME) if "for the agent" in m.get("text", "")])
        bid = next(k for k, x in b.bus["inbox"].items() if x is item)
        ev, why = b.as_liaison("bus.pass_requested", {"inbox": bid, "session": "hub-core"})
        self.assertIsNone(why)   # the check is the gate; where to pass is the liaison's choice

    def offer(self, hub, raw):
        folder = busd.in_dir(hub.h.state_dir)
        os.makedirs(folder, exist_ok=True)
        digest = hashlib.sha256(raw).hexdigest()
        path = os.path.join(folder, digest + ".env")
        with open(path, "wb") as f:
            f.write(raw)
        hub.ask("bus.offered", {"path": path, "sha256": digest, "size": len(raw)})

    def test_verify_by_the_pair_code_or_by_the_click(self):
        a = self.a
        ev = a.ask("bus.contact_requested", {"op": "verify", "name": self.na, "code": "1234"})
        self.assertEqual(a.bus["last"]["code"], "code_mismatch")
        self.assertFalse(a.bus["contacts"][self.na]["verified"])
        code = bus.contact_code(a.bus["contacts"][self.na])
        a.ask("bus.contact_requested", {"op": "set", "name": self.na, "status": "known", "code": code})
        self.assertTrue(a.bus["contacts"][self.na]["verified"])
        ev = a.rec("bus.contact_requested", {"op": "verify", "name": self.na, **SESSION})
        self.assertIn("owner", a.verdict(ev))

    def test_the_old_form_of_the_owners_chat_from_here_reaches_the_liaison_of_the_other_hub_as_a_message(self):
        # a request without `to_hub` is the old chat (a rollback's journal, an old caller): sent as before, and the hub that
        # reads it (this one included) takes it as an ordinary message to its liaison once the pair is checked
        a, b = self.a, self.b
        a.ask("bus.contact_requested", {"op": "verify", "name": self.na})
        b.ask("bus.contact_requested", {"op": "verify", "name": self.nb})
        ev = a.ask("bus.chat_requested", {"contact": self.na, "text": "Hello, this is owner A"})
        self.assertIsNone(a.verdict(ev))
        self.assertEqual(a.cards(), [])
        self.wire.settle()
        (item,) = b.bus["inbox"].values()
        self.assertEqual((item.get("for"), item.get("chat"), item["status"], item["text"]), (None, True, "ok", "Hello, this is owner A"))
        (o,) = [x for x in a.bus["out"].values() if x.get("kind") == "message"]
        self.assertEqual((o["status"], o["cls"]), ("delivered", "chat"))
        self.assertTrue(any("Hello" in n["text"] for n in notices(b)))

    def test_a_secret_in_the_chat_needs_a_confirmation(self):
        a = self.a
        key = "sk-ant-" + "a1b2" * 8
        a.ask("bus.chat_requested", {"contact": self.na, "text": f"key {key}"})
        self.assertEqual(a.bus["last"]["code"], "secret")
        self.assertEqual([o for o in a.bus["out"].values() if o.get("kind") == "message"
                          and o.get("status") == "signed"], [])
        a.ask("bus.chat_requested", {"contact": self.na, "text": f"key {key}", "confirm": True})
        self.assertEqual(len([o for o in a.bus["out"].values() if o.get("status") == "signed"]), 1)

    def test_a_session_cannot_speak_as_the_owner(self):
        a = self.a
        ev = a.rec("bus.chat_requested", {"contact": self.na, "text": "I am the owner", **SESSION})
        self.assertIn("owner", a.verdict(ev))
        ev, why = a.as_liaison("bus.send_requested", {"contact": self.na, "text": "x", "for": "owner"})
        self.assertTrue(why)

    def test_a_chat_to_a_hub_of_version_1_is_refused_with_words(self):
        a = self.a
        a.bus["contacts"][self.na]["bus_v"] = 1
        a.ask("bus.chat_requested", {"contact": self.na, "text": "hello"})
        self.assertEqual(a.bus["last"]["code"], "old_hub")

    def test_telling_the_hub_is_the_owners_own_message_and_the_answer_in_its_thread_is_for_him(self):
        # «tell the hub» — an ordinary message to the other liaison (no card here), its answer in that thread
        # goes to the page and Telegram, not to this hub's liaison; every other thread is the liaison's as before
        a, b = self.a, self.b
        a.ask("bus.contact_requested", {"op": "verify", "name": self.na})
        b.ask("bus.contact_requested", {"op": "verify", "name": self.nb})
        ev = a.ask("bus.chat_requested", {"contact": self.na, "text": "Who publishes the catalog on your side?", "to_hub": True})
        self.assertIsNone(a.verdict(ev))
        self.assertEqual(a.cards(), [])
        self.wire.settle()
        (o,) = [x for x in a.bus["out"].values() if x.get("kind") == "message"]
        self.assertEqual((o["status"], o["cls"], o["source"], "for" in o), ("delivered", "liaison", "owner", False))
        self.assertTrue(a.bus["threads"][o["thread"]]["owner"])
        (item,) = b.bus["inbox"].values()
        self.assertEqual((item["agent_from"], item["status"], item.get("for"), item["outcome"]), ("owner", "ok", None, None))
        self.assertTrue([m for m in b.messages_to(liaison.NAME) if "publishes the catalog" in m.get("text", "")])
        bid = next(iter(b.bus["inbox"]))
        _, why = b.as_liaison("bus.send_requested", {"contact": self.nb, "thread": item["thread"],
                                                     "text": "The duty person will answer today"})
        self.assertIsNone(why)
        self.wire.settle()
        (back,) = a.bus["inbox"].values()
        self.assertEqual((back["for"], back["reply"], back["outcome"], back["text"]),
                         ("owner", True, "owner", "The duty person will answer today"))
        self.assertFalse([m for m in a.messages_to(liaison.NAME) if "duty person" in m.get("text", "")])   # no turn here
        self.assertTrue(any("duty person" in n["text"] and n.get("bus", {}).get("kind") == "chat" for n in notices(a)))
        rows = busflow.chat_rows({"bus": a.bus}, self.na, said={})
        self.assertEqual(sorted((r["way"], r["kind"], r.get("reply")) for r in rows),
                         [("in", "chat", True), ("out", "chat", None)])
        # a message of that hub in no thread of ours is the liaison's, as before
        _, raw = bus.seal(self.keys_of(b), b.bus["contacts"][self.nb], bus.message("qa", "new matter"), b.now, cls="liaison")
        self.offer(a, raw)
        news = [x for x in a.bus["inbox"].values() if x["text"] == "new matter"]
        self.assertEqual([(x.get("for"), x["outcome"]) for x in news], [(None, None)])
        self.assertTrue([m for m in a.messages_to(liaison.NAME) if "new matter" in m.get("text", "")])
        self.assertIn(bid, b.bus["inbox"])

    def old_chat(self, hub_from, to_contact, text, thread="a1" * 16):
        """What a hub of the previous release sends as its owners' chat: a message with `for: owner`, agent_from owner."""
        inner = {**bus.message("owner", text, thread=thread), "for": "owner"}
        _, raw = bus.seal(self.keys_of(hub_from), hub_from.bus["contacts"][to_contact], inner, hub_from.now, cls="chat")
        return raw

    def test_an_old_hubs_owners_chat_goes_to_our_liaison_like_a_tell_the_hub_and_is_answered_in_its_thread(self):
        # a hub of the previous release writes `for: owner` — here that is an ordinary message of that hub to our
        # liaison (the contact's rights, never the owner's order); the page and Telegram still show it, no «unanswered» bubble
        a, b = self.a, self.b
        a.ask("bus.contact_requested", {"op": "verify", "name": self.na})
        b.ask("bus.contact_requested", {"op": "verify", "name": self.nb})
        self.offer(a, self.old_chat(b, self.nb, "Hello from an older version"))
        (item,) = a.bus["inbox"].values()
        self.assertEqual((item["status"], item.get("for"), item.get("chat"), item["outcome"]), ("ok", None, True, None))
        self.assertTrue([m for m in a.messages_to(liaison.NAME) if "Hello from an older version" in m.get("text", "")])
        self.assertTrue(any("older version" in n["text"] and n.get("bus", {}).get("kind") == "chat" for n in notices(a)))
        bid = next(iter(a.bus["inbox"]))
        _, why = a.as_liaison("bus.send_requested", {"contact": self.na, "thread": item["thread"], "text": "Hello, this is the liaison"})
        self.assertIsNone(why)
        self.assertEqual(a.bus["inbox"][bid]["outcome"], "answered")
        rows = busflow.chat_rows({"bus": a.bus}, self.na, said={})
        self.assertEqual(sorted((r["way"], r["kind"]) for r in rows), [("in", "chat"), ("out", "thread")])
        self.assertEqual(busflow.list_view({"bus": a.bus})["contacts"][0]["chat_in"], 1)

    def test_an_old_hubs_owners_chat_before_the_check_is_held_like_any_message_for_agents(self):
        a, b = self.a, self.b
        self.offer(a, self.old_chat(b, self.nb, "early"))
        (item,) = a.bus["inbox"].values()
        self.assertEqual(item["status"], "unverified")
        self.assertFalse([m for m in a.messages_to(liaison.NAME) if "early" in m.get("text", "")])
        a.ask("bus.contact_requested", {"op": "verify", "name": self.na})
        self.assertTrue([m for m in a.messages_to(liaison.NAME) if "early" in m.get("text", "")])   # the check lets it through

    def reply_stand(self):
        """The other hub's owner told us (a thread of theirs is open here); our liaison is about to answer in it."""
        a, b = self.a, self.b
        a.ask("bus.contact_requested", {"op": "verify", "name": self.na})
        b.ask("bus.contact_requested", {"op": "verify", "name": self.nb})
        b.ask("bus.chat_requested", {"contact": self.nb, "text": "Who publishes the catalog?", "to_hub": True})
        self.wire.settle()
        (item,) = a.bus["inbox"].values()
        return item

    def test_the_liaisons_answer_in_a_thread_the_other_side_opened_goes_without_a_card(self):
        a = self.a
        item = self.reply_stand()
        self.assertTrue(a.bus["threads"][item["thread"]]["theirs"])
        _, why = a.as_liaison("bus.send_requested", {"contact": self.na, "thread": item["thread"], "text": "The duty person, today"})
        self.assertIsNone(why)
        self.assertEqual(a.cards(), [])
        self.wire.settle()
        (o,) = [x for x in a.bus["out"].values() if x.get("kind") == "message"]
        self.assertEqual((o["status"], o["approved"]), ("delivered", "auto"))

    def test_a_new_thread_from_us_goes_out_and_a_secret_in_the_answer_is_refused_to_the_liaison(self):
        a = self.a
        item = self.reply_stand()
        a.as_liaison("bus.send_requested", {"contact": self.na, "text": "A new conversation from us"})
        a.as_liaison("bus.send_requested", {"contact": self.na, "thread": item["thread"],
                                            "text": "key sk-ant-" + "a1b2" * 8})
        self.assertEqual(a.cards(), [])
        outs = [o for o in a.bus["out"].values() if o.get("kind") == "message"]
        self.assertEqual([(o["status"], o.get("code")) for o in outs], [("signed", None), ("refused", "secret")])
        self.assertTrue(any("Not sent" in m["text"] and "secret" in m["text"].lower() or "key" in m["text"]
                            for m in a.messages_to(liaison.NAME)))

    def keys_of(self, hub):
        with hub.env():
            return bus.load(bus.key_dir(hub.config))

    def test_telling_the_hub_waits_for_the_check_and_a_key_like_text_for_a_yes(self):
        a = self.a
        ev = a.ask("bus.chat_requested", {"contact": self.na, "text": "hello", "to_hub": True})
        self.assertIn("not checked", a.verdict(ev) or "")
        self.assertEqual([o for o in a.bus["out"].values() if o.get("kind") == "message"], [])
        a.ask("bus.contact_requested", {"op": "verify", "name": self.na})
        a.ask("bus.contact_requested", {"op": "set", "name": self.na, "status": "muted"})
        ev = a.ask("bus.chat_requested", {"contact": self.na, "text": "hello", "to_hub": True})
        self.assertIn("not accepted", a.verdict(ev) or "")   # a muted hub: the real reason, not «not checked»
        a.ask("bus.contact_requested", {"op": "set", "name": self.na, "status": "known"})
        key = "sk-ant-" + "a1b2" * 8
        a.ask("bus.chat_requested", {"contact": self.na, "text": f"key {key}", "to_hub": True})
        self.assertEqual(a.bus["last"]["code"], "secret")
        self.assertEqual(a.cards(), [])
        a.ask("bus.chat_requested", {"contact": self.na, "text": f"key {key}", "to_hub": True, "confirm": True})
        self.assertEqual(len([o for o in a.bus["out"].values() if o.get("kind") == "message" and o.get("status") == "signed"]), 1)

    def test_after_the_check_liaison_messages_go_as_v2_with_a_class(self):
        a, b = self.a, self.b
        a.ask("bus.contact_requested", {"op": "verify", "name": self.na})
        b.ask("bus.contact_requested", {"op": "verify", "name": self.nb, "may_write": ["hub-core"]})
        mid, _ = a.say("api", "API schema", bus_to=self.na, bus_to_via="local")
        a.as_liaison("bus.send_requested", {"contact": self.na, "text_from": mid})
        (o,) = [x for x in a.bus["out"].values() if x.get("kind") == "message"]
        raw = base64.b64decode(o["envelope"])
        h = json.loads(json.loads(raw)["head"])
        self.assertEqual((h["v"], h["cls"]), (2, "liaison"))
        self.wire.settle()
        (item,) = [x for x in b.bus["inbox"].values() if x.get("text") == "API schema"]
        self.assertEqual(item["status"], "ok")
        self.assertEqual(o["status"], "delivered")

    def test_a_contact_of_version_1_gets_v1_envelopes(self):
        a = self.a
        a.ask("bus.contact_requested", {"op": "verify", "name": self.na})
        a.bus["contacts"][self.na]["bus_v"] = 1
        mid, _ = a.say("api", "x", bus_to=self.na, bus_to_via="local")
        a.as_liaison("bus.send_requested", {"contact": self.na, "text_from": mid})
        (o,) = [x for x in a.bus["out"].values() if x.get("kind") == "message"]
        h = json.loads(json.loads(base64.b64decode(o["envelope"]))["head"])
        self.assertEqual(h["v"], 1)
        self.assertNotIn("cls", h)

    def test_two_unanswered_v2_messages_turn_the_contact_to_v1(self):
        a = self.a
        c = a.bus["contacts"][self.na]
        self.assertEqual(busstate.effective_v(a.bus, c), 2)
        a.rec("core.bus_v2_unanswered", {"name": self.na})
        self.assertEqual(busstate.effective_v(a.bus, c), 2)
        a.rec("core.bus_v2_unanswered", {"name": self.na})
        self.assertEqual(busstate.effective_v(a.bus, c), 1)
        with a.env():
            a.h.record("bus.peer_seen", None, {"fp": c["fp"], "bus_v": 2}, src="bus-relay")
        self.assertEqual(busstate.effective_v(a.bus, c), 2)
        a.rec("core.bus_v2_unanswered", {"name": self.na})
        a.rec("core.bus_v2_unanswered", {"name": self.na})
        with a.env():
            a.h.record("bus.peer_seen", None, {"fp": c["fp"], "bus_v": 1}, src="bus-relay")
        self.assertEqual(busstate.effective_v(a.bus, c), 1)   # the hub was rolled back: the relay says so

    def test_the_relay_of_version_1_carries_no_v2(self):
        a = self.a
        c = a.bus["contacts"][self.na]
        a.bus["relay"] = {"url": "https://relay.example.test", "key": "x", "ref": "x", "bus_v": 1}
        self.assertEqual(busstate.effective_v(a.bus, c), 1)

    def test_a_hub_of_version_1_refuses_a_v2_head_before_any_contact_no_quarantine(self):
        """What an older core does with a v2 box: the head is not of its form — bad_format before the contact is looked
        up (as for v=3 here): nothing counted, no bad_box, no quarantine however many come."""
        a, b = self.a, self.b
        with a.env():
            keys = bus.load(bus.key_dir(a.config))
        for _ in range(5):
            _, raw = bus.seal(keys, a.bus["contacts"][self.na], bus.message("api", "x"), a.now, cls="liaison")
            env = json.loads(raw)
            h = json.loads(env["head"])
            h["v"] = 3
            env["head"] = json.dumps(h)
            self.offer(b, json.dumps(env).encode())
        c = b.bus["contacts"][self.nb]
        self.assertNotIn("quarantine", c)
        self.assertEqual(b.bus["bad"], {})
        self.assertEqual(b.bus["rate"].get(c["fp"]), None)

    def test_no_class_is_limited_by_rate_and_a_lying_class_is_refused(self):
        a, b = self.a, self.b
        b.ask("bus.contact_requested", {"op": "verify", "name": self.nb})
        with a.env():
            keys = bus.load(bus.key_dir(a.config))
        for i in range(130):   # more than the old hour's limit of the class
            _, raw = bus.seal(keys, a.bus["contacts"][self.na], bus.message("api", f"x{i}"), a.now, cls="liaison")
            self.offer(b, raw)
        _, raw = bus.seal(keys, a.bus["contacts"][self.na], {**bus.message("owner", "chat"), "for": "owner"}, a.now,
                          cls="chat")
        self.offer(b, raw)
        self.assertEqual(len(b.bus["inbox"]), 131)
        self.assertNotIn('"code":"rate"', journal_text(b).replace(" ", ""))
        # a box whose class lies about its kind is refused and counted against the class it named
        _, raw = bus.seal(keys, a.bus["contacts"][self.na], bus.message("api", "falsehood"), a.now, cls="chat")
        self.offer(b, raw)
        self.assertIn("bad_cls", journal_text(b))
        self.assertNotIn("falsehood", [x["text"] for x in b.bus["inbox"].values()])

    def test_a_late_unlink_of_an_old_link_removes_nothing(self):
        a, b = self.a, self.b
        with a.env():
            keys = bus.load(bus.key_dir(a.config))
        _, raw = bus.seal(keys, a.bus["contacts"][self.na], {"kind": "unlink", "pair_id": "0" * 32}, a.now, cls="ctl")
        self.offer(b, raw)
        self.assertEqual(len(b.bus["contacts"]), 1)
        self.assertIn("stale_unlink", journal_text(b))

    def test_an_unchecked_hub_cannot_rename_itself(self):
        a, b = self.a, self.b
        with a.env():
            keys = bus.load(bus.key_dir(a.config))
        _, raw = bus.seal(keys, a.bus["contacts"][self.na], {"kind": "card_update", "label": "Trusted hub",
                                                              "agents": {"x": "y"}}, a.now, cls="ctl")
        self.offer(b, raw)
        self.assertEqual(b.bus["contacts"][self.nb]["label"], "Hub A")
        self.assertEqual(b.bus["contacts"][self.nb]["agents"], {})
        self.assertNotIn("Trusted", json.dumps(busstate.view(b.st), ensure_ascii=False))
        b.ask("bus.contact_requested", {"op": "verify", "name": self.nb})   # what it sent meanwhile is applied now
        self.assertEqual(b.bus["contacts"][self.nb]["agents"], {"x": "y"})

    def test_the_old_way_needs_the_typed_code(self):
        from tests.test_hubbus_s1 import LiveHub
        a = self.a
        other = LiveHub("Hub C")
        a.ask("bus.contact_requested", {"op": "add", "name": "vee", "card": other.card()})
        a.ask("bus.contact_requested", {"op": "verify", "name": "vee"})
        self.assertEqual(a.bus["last"]["code"], "code_needed")
        self.assertFalse(a.bus["contacts"]["vee"]["verified"])


@need_lib
@need_ssh
class Catalog(unittest.TestCase):
    def test_the_card_names_the_exposed_agents_from_the_settings(self):
        from tests.test_hubbus_s1 import LiveHub
        from tests.test_hubbus_s2 import Stand
        a = LiveHub("Hub A")
        path = os.path.join(a.config, "hub.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"bus": {"exposed": {"maps": "map work"}}}, f)
        with mock.patch.dict(os.environ, {"HUB_CONFIG": path}):
            code, card = Stand.cli(self, a, ["card"])
        self.assertEqual(code, 0, card)
        self.assertEqual(bus.read_card(card, time.time())["agents"], {"maps": "map work"})


class RelayAddress(unittest.TestCase):
    def test_a_port_out_of_range_or_not_a_number_is_refused(self):
        from core import busrelay
        for url in ("https://relay.example.test:99999", "https://relay.example.test:abc"):
            with self.assertRaises(busrelay.Error, msg=url):
                busrelay.check_url(url)
        self.assertEqual(busrelay.check_url("https://relay.example.test:8443/"), "https://relay.example.test:8443")


class TransportJobs(unittest.TestCase):
    """The relay transport's jobs (modules/bus_relay): a permit recorded once (a repeat after a lost answer is not a
    failure), an unused permit dropped, a used one said so, a refusal for good told to the core, a network failure
    tried again later without telling."""

    def setUp(self):
        from modules.bus_relay import run
        from tests.test_hubbus_s2 import FakeClient
        self.run = run
        self.state = __import__("tempfile").mkdtemp(prefix="relay-j-")
        self.now = 1_800_000_000
        self.put = []
        self.t = run.Transport(self.state, put=lambda k, d: self.put.append((k, d)), clock=lambda: self.now,
                               log=lambda text: None)
        self.fake = FakeClient()
        self.fake.revoked = True
        self.fake.invite = lambda text: self.fake.calls.append(("invite", text)) or self._fail("invite")
        self.fake.revoke = lambda iid: self.fake.calls.append(("revoke", iid)) or self._fail("revoke") or self.fake.revoked
        self.fake.invites = lambda: []
        self.t.client = lambda v: self.fake
        self.t.hello_at = self.now   # the relay's version was asked just now
        os.makedirs(busd.jobs_dir(self.state))

    def _fail(self, what):
        from core import busrelay
        code = (self.fake.fail or {}).get(what)
        if code:
            raise busrelay.Error(code)

    def view(self, jobs):
        return {"enabled": True, "me": fps(1)[0], "allow": [], "out": [], "jobs": jobs,
                "relay": {"url": "https://relay.example.test", "key": base64.b64encode(b"k" * 32).decode(), "ts": 1,
                          "bus_v": 2}}

    def job_file(self, jid, data):
        with open(os.path.join(busd.jobs_dir(self.state), f"{jid}.json"), "w", encoding="utf-8") as f:
            json.dump(data, f)

    def test_invite_revoke_and_their_refusals(self):
        self.job_file("i1", {"invite": "hub-relay-invite-1:x"})
        self.t.chores(self.view([{"job": "i1", "do": "invite"}]))
        self.assertEqual(self.put, [("bus.job_done", {"job": "i1"})])
        self.put.clear()
        self.fake.fail = {"invite": "conflict"}
        self.t.job_tries.clear()
        self.t.chores(self.view([{"job": "i1", "do": "invite"}]))
        self.assertEqual(self.put, [("bus.job_done", {"job": "i1"})])       # recorded before: done
        self.put.clear()
        self.fake.fail = {"invite": "network"}
        self.t.job_tries.clear()
        self.t.chores(self.view([{"job": "i1", "do": "invite"}]))
        self.assertEqual(self.put, [])                                      # tried again later, not told
        self.t.chores(self.view([{"job": "i1", "do": "invite"}]))
        self.assertEqual(len([c for c in self.fake.calls if c[0] == "invite"]), 3)   # not again at once
        self.fake.fail = {}
        self.fake.revoked = False
        self.t.chores(self.view([{"job": "r1", "do": "revoke", "invite": "a" * 32}]))
        self.assertEqual(self.put, [("bus.job_done", {"job": "r1", "code": "used"})])

    def test_the_relays_version_is_asked_again_after_a_while(self):
        self.t.hello_at = self.now
        self.t.chores(self.view([]))
        self.assertNotIn(("hello",), self.fake.calls)
        self.now += self.run.PEER_EVERY
        self.t.chores(self.view([]))
        self.assertIn(("hello",), self.fake.calls)
        self.assertIn(("bus.relay_seen", {"bus_v": 1}), self.put)   # the relay went back to v1: the core learns it

    def test_a_job_whose_file_is_not_there_waits(self):
        self.t.chores(self.view([{"job": "i2", "do": "invite"}]))
        self.assertEqual(self.put, [])


class OldCore(unittest.TestCase):
    def test_the_fold_of_new_facts_without_the_bus_refuses_them_one_by_one(self):
        st = reducer.empty()
        for type_ in ("core.bus_link_used", "core.bus_join_step", "core.bus_unlinked", "core.bus_card_updated"):
            ev = {"seq": 1, "ts": NOW, "type": type_, "entity": None, "data": {"id": "x"}, "src": "core"}
            _, _, why = reducer.reduce(st, ev)
            self.assertTrue(why, type_)

    def test_transport_facts_only_from_a_transport(self):
        st = reducer.empty()
        st["bus"] = busstate.empty()
        st["bus"]["enabled"] = True
        for type_ in ("bus.job_done", "bus.invites_seen", "bus.peer_seen", "bus.relay_seen"):
            ev = {"seq": 1, "ts": NOW, "type": type_, "entity": None, "src": "spool",
                  "data": {"channel": "session", "owner": False, "sender": "api"}}
            _, _, why = reducer.reduce(st, ev)
            self.assertIn("transport", why or "", type_)


if __name__ == "__main__":
    unittest.main()
