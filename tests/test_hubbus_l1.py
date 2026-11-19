"""The hub bus, slice L1 (spec hubbus/ux §3.5, §4.2, §4.4, §10, §11): the owner's path in functions and two commands —
`Flow.ensure` (OpenSSH, the library into `<state>/pylib`, the self-check, the keys, the card, the relay, each step a
progress fact with the owner's words and a ready command when it fails), `hub bus link` on A, `hub bus join -` on B
(a link, or the relay operator's invitation for the first hub of a relay), `hub bus reset`, the bus rows of
`hub doctor`. pip, ssh-keygen and the network are faked where a test is about the path; the end-to-end case runs two
clean stand hubs through the relay of this tree (relay/server.py): link → join → «Linked» → the owners' chat both ways
→ unlink. A real install into a clean folder runs with HUB_TEST_REAL_INSTALL=1 (it needs PyPI)."""
import tests  # noqa: F401 — keeps the machine's settings out
import base64
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

from tests.test_hubbus import LIB_OK, need_lib, need_ssh
from core import bus, busd, busflow, buscli, busstate, i18n, liaison

OWNER = {"channel": "cli", "owner": True}
SESSION = {"channel": "session", "owner": False, "sender": "maps"}


def flow_of(hub, **kw):
    """A Flow of the owner of a stand hub: his requests are folded at once (the core's record is synchronous)."""
    rejected = {}

    def put(type_, entity, data, say=True):
        ev = hub.rec(type_, {**data, **OWNER}, entity)
        why = hub.verdict(ev)
        if why:
            rejected[ev["seq"]] = why
        return ev["seq"]
    return busflow.Flow(put, lambda: hub.h.state, hub.h.state_dir, rejected=rejected, config_dir=hub.config, **kw)


def facts(hub, type_):
    out = []
    with open(os.path.join(hub.h.state_dir, "journal.jsonl"), encoding="utf-8") as f:
        for line in f:
            e = json.loads(line)
            if e["type"] == type_:
                out.append(e)
    return out


class Finding(unittest.TestCase):
    """The one paste box takes three kinds of text by their prefix; a message may carry words around the token."""

    LINK = "hub-link-1:" + "A" * 40 + "_-" + "b" * 30

    def test_kinds(self):
        self.assertEqual(busflow.find(self.LINK), ("link", self.LINK))
        inv = "hub-relay-invite-1:" + "x" * 30 + "." + "y" * 30
        self.assertEqual(busflow.find(inv), ("invite", inv))
        card = "hub-bus-card-1:" + "QUJD" * 10 + "=="
        self.assertEqual(busflow.find(card), ("card", card))
        self.assertEqual(busflow.find("plain text"), (None, None))
        self.assertIsNone(busflow.kind_of(""))

    def test_words_around_and_a_broken_line(self):
        head, tail = self.LINK[:40], self.LINK[40:]
        text = f"A link for one connection:\n{head}\n{tail}\nValid for 24 hours"
        self.assertEqual(busflow.find(text), ("link", self.LINK))
        # a short word after the token is not glued on
        self.assertEqual(busflow.find(self.LINK + "\nok thanks"), ("link", self.LINK))

    def test_the_full_stop_after_an_invitation_is_not_its_part(self):
        inv = "hub-relay-invite-1:" + "x" * 30 + "." + "y" * 30
        self.assertEqual(busflow.find(f"Here is the invite: {inv}."), ("invite", inv))

    def test_the_first_token_wins(self):
        inv = "hub-relay-invite-1:" + "x" * 30
        self.assertEqual(busflow.find(f"{inv} and {self.LINK}")[0], "invite")


class Words(unittest.TestCase):
    def test_every_code_has_words(self):
        for code in ("no_ssh", "no_lib", "no_network", "no_pip", "install_failed", "selfcheck", "no_relay", "used",
                     "expired", "own_link", "other_relay", "blocked", "pasted", "lost", "bad_link", "old_relay",
                     "old_hub", "not_a_link", "card", "no_answer", "queued"):
            text = busflow.why(code)
            # its own words, not the generic text with the code (in English the code can be a word of the text: "used")
            self.assertTrue(text and text != i18n.t("bus.flow.code.other", code=code), (code, text))
        self.assertIn("weird_code", busflow.why("weird_code"))


class FakeHub:
    """Only what Flow.ensure needs from a core, for the steps before the keys: no library, no core."""

    def __init__(self):
        self.state_dir = tempfile.mkdtemp(prefix="bus-l1-")
        self.notes, self.said, self.puts = [], [], []

    def flow(self, run=None):
        return busflow.Flow(lambda *a, **k: self.puts.append(a) or None, lambda: {}, self.state_dir,
                            note=lambda t, d: self.notes.append((t, d)),
                            say=lambda *a: self.said.append(a), run=run)


class Installing(unittest.TestCase):
    """The library step: missing — pip (faked) into `<state>/pylib`; the network, no pip, a failure — each its code, the
    owner's words and the command; consent off — only the command."""

    def setUp(self):
        self.hub = FakeHub()
        self.calls = []
        self.lib = types.SimpleNamespace(name="lib")
        self.tries = []

        def crypto(state_dir=None):
            self.tries.append(state_dir)
            if self.installed:
                return self.lib
            raise bus.Unavailable("no_lib", "no cryptography: No module named 'cryptography'")
        self.installed = False
        p = mock.patch.object(bus, "crypto", crypto)
        p.start()
        self.addCleanup(p.stop)

    def run_pip(self, rc, out, installs=False):
        def run(cmd):
            self.calls.append(cmd)
            if installs:
                self.installed = True
            return rc, out
        return run

    def steps(self):
        return [(d["step"], d["state"], d.get("code")) for t, d in self.hub.notes if t == "bus.setup_step"]

    def test_installed_into_pylib_of_the_state(self):
        got = self.hub.flow(self.run_pip(0, "Successfully installed cryptography-50.0", installs=True)).library(True)
        self.assertIs(got, self.lib)
        (cmd,) = self.calls
        self.assertIn(os.path.join(self.hub.state_dir, "pylib"), cmd)
        self.assertEqual(self.steps(), [("lib", "run", None), ("lib", "ok", None)])

    def test_no_network(self):
        out = ("WARNING: Retrying ... NewConnectionError: Failed to establish a new connection: [Errno -3] Temporary "
               "failure in name resolution\nERROR: Could not find a version that satisfies the requirement")
        got = self.hub.flow(self.run_pip(1, out)).library(True)
        self.assertEqual((got.code, got.step), ("no_network", "lib"))
        self.assertIn("pip", got.command)
        self.assertIn(got.text, busflow.why("no_network"))
        self.assertEqual(self.steps()[-1], ("lib", "fail", "no_network"))
        self.assertEqual([d.get("command") for t, d in self.hub.notes][-1], got.command)

    def test_no_pip(self):
        got = self.hub.flow(self.run_pip(1, "/usr/bin/python3: No module named pip")).library(True)
        self.assertEqual(got.code, "no_pip")

    def test_other_failure_names_its_tail(self):
        got = self.hub.flow(self.run_pip(1, "building wheel\nerror: Rust compiler missing")).library(True)
        self.assertEqual(got.code, "install_failed")
        self.assertIn("Rust compiler missing", got.text)

    def test_without_consent_only_the_command(self):
        got = self.hub.flow(self.run_pip(0, "")).library(False)
        self.assertEqual(got.code, "no_lib")
        self.assertEqual(self.calls, [])
        self.assertIn("--target", got.command)

    def test_installed_but_still_missing(self):
        got = self.hub.flow(self.run_pip(0, "ok")).library(True)
        self.assertEqual(got.code, "no_lib")

    def test_no_ssh_keygen_first_with_the_way_to_it(self):
        with mock.patch.object(busflow.shutil, "which", lambda name: None):
            got = self.hub.flow(self.run_pip(0, "")).ensure()
        self.assertEqual((got.code, got.step), ("no_ssh", "ssh"))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.steps(), [("ssh", "fail", "no_ssh")])
        import platforms.windows
        self.assertIn("OpenSSH.Client", platforms.windows.ssh_install_hint())

    def test_a_reason_with_line_breaks_is_kept_printable(self):
        self.hub.flow().step("card", "fail", "card", "ssh-keygen:\nline two\x1b[2J")
        (t, d), = self.hub.notes
        self.assertTrue(d["reason"].isprintable(), d)
        self.assertIn("line two", d["reason"])

    def test_a_progress_fact_that_fails_does_not_stop_the_path(self):
        def note(t, d):
            raise RuntimeError("an older core")
        f = busflow.Flow(lambda *a, **k: None, lambda: {}, self.hub.state_dir, note=note,
                         run=self.run_pip(0, "", installs=True))
        self.assertIs(f.library(True), self.lib)


class SetupFold(unittest.TestCase):
    """`bus.setup_step`: the owner's process reports, the core keeps the last run; `bus.reset_requested`: the identity
    goes, its fingerprint is remembered as retired."""

    def fold(self, st, kind, data, seq=1):
        from core import reducer
        acts = []
        why = busstate.fold(st, {"type": kind, "seq": seq, "ts": 100 + seq, "data": data}, data, acts)
        return why, acts

    def test_the_last_run_by_steps(self):
        st = {"sessions": {}}
        self.assertIsNone(self.fold(st, "bus.setup_step", {"step": "ssh", "state": "ok", **OWNER})[0])
        self.assertIsNone(self.fold(st, "bus.setup_step", {"step": "lib", "state": "fail", "code": "no_network",
                                                           "command": "python3 -m pip install x", **OWNER}, 2)[0])
        steps = st["bus"]["setup"]["steps"]
        self.assertEqual(steps["lib"]["code"], "no_network")
        self.assertFalse(st["bus"]["enabled"])
        self.fold(st, "bus.setup_step", {"step": "ssh", "state": "ok", **OWNER}, 3)   # a new run
        self.assertEqual(list(st["bus"]["setup"]["steps"]), ["ssh"])

    def test_only_the_owner_and_only_the_known_steps(self):
        st = {"sessions": {}}
        self.assertIn("only the owner", self.fold(st, "bus.setup_step", {"step": "ssh", "state": "ok", **SESSION})[0])
        self.assertTrue(self.fold(st, "bus.setup_step", {"step": "rm", "state": "ok", **OWNER})[0])
        self.assertTrue(self.fold(st, "bus.setup_step", {"step": "lib", "state": "ok", "reason": "x\x1b[2J",
                                                         **OWNER})[0])

    def test_reset_drops_the_identity(self):
        st = {"sessions": {}, "bus": {**busstate.empty(), "enabled": True, "me": {"fp": "hb1-" + "a" * 26},
                                      "contacts": {"x": {"fp": "hb1-" + "b" * 26, "status": "known"}},
                                      "relay": {"url": "https://relay.example.test", "key": "k"}}}
        why, acts = self.fold(st, "bus.reset_requested", OWNER)
        self.assertIsNone(why)
        b = st["bus"]
        self.assertEqual((b["enabled"], b["contacts"], b["relay"], b["me"]), (False, {}, None, {}))
        self.assertEqual(b["retired"], ["hb1-" + "a" * 26])
        self.assertIn({"do": "bus_reset", "session": None, "fp": "hb1-" + "a" * 26}, acts)
        self.assertIn("only the owner", self.fold(st, "bus.reset_requested", SESSION, 2)[0])


@need_lib
@need_ssh
class OnStandHubs(unittest.TestCase):
    """Flow on cores of the stand (tests/test_hubbus_s1.LiveHub), the folder transport between them."""

    def setUp(self):
        from tests.test_hubbus_s1 import LiveHub, Wire
        now = int(time.time())
        self.a, self.b = LiveHub("Hub A", now=now), LiveHub("Hub B", now=now)
        self.wire = Wire(self.a, self.b)

    def settle(self):
        for hub in (self.a, self.b):
            hub.h.__dict__.pop("bus_link_tried", None)
            with hub.env():
                busd.tick(hub.h)
        self.wire.settle()
        for hub in (self.a, self.b):
            with hub.env():
                busd.tick(hub.h)
                busd.write_view(hub.h)

    def link_text(self):
        from tests.test_hubbus_l0 import make_link
        return make_link(self.a)[0]

    def test_ensure_on_a_hub_without_a_relay_stops_at_the_relay(self):
        with self.a.env():
            got = flow_of(self.a).ensure()
        self.assertEqual((got.code, got.step), ("no_relay", "relay"))
        steps = self.a.bus["setup"]["steps"]
        self.assertEqual([steps[s]["state"] for s in busflow.STEPS], ["ok"] * 5 + ["fail"])
        with self.a.env():
            self.assertTrue(flow_of(self.a).ensure(need_relay=False).ok)

    def test_join_by_a_pasted_message_links_and_names_the_pair_code(self):
        text = self.link_text()
        said = []
        with self.b.env():
            got = flow_of(self.b, idle=self.settle, say=lambda *a: said.append(a)).join(f"here is the link:\n{text}\n", wait=30)
        self.assertTrue(got.ok, got)
        (ca,), (cb,) = self.a.bus["contacts"].values(), self.b.bus["contacts"].values()
        self.assertTrue(got.data["pair_code"])
        self.assertEqual(got.data["pair_code"], bus.contact_code(cb))
        self.assertEqual(bus.contact_code(ca), bus.contact_code(cb))
        self.assertEqual(got.data["label"], "Hub A")
        self.assertIn(("join", "linked", "linked"), said)
        self.assertEqual(os.listdir(busd.join_dir(self.b.h.state_dir)), [])

    def test_revoke_and_cancel_are_answered_by_the_fold_itself(self):
        # the fold decides these two from the request (no core.bus_* fact follows): the page and Telegram waited the whole
        # term for a fact and showed «no answer» on a link that was revoked
        from tests.test_hubbus_l0 import joined, make_link, paste
        text, lid = make_link(self.a)
        with self.a.env():
            f = flow_of(self.a, wait=0.2)
            self.assertTrue(f.revoke(lid).ok)
            self.assertEqual(f.revoke(lid).code, "rejected")   # the second press: the core's refusal, not «no answer»
        self.assertEqual(self.a.bus["links"][lid]["state"], "revoked")
        text, lid = make_link(self.a)
        with mock.patch.object(busd, "join_intro", side_effect=OSError("cut")):
            paste(self.b, text)
        with self.b.env():
            got, _ = flow_of(self.b, wait=0.2).ask("bus.join_cancel_requested", {"id": next(iter(self.b.bus["joins"]))})
        self.assertTrue(got.ok, got)
        self.assertEqual(joined(self.b)["state"], "cancelled")

    def test_join_refusals(self):
        text = self.link_text()
        with self.a.env():
            self.assertEqual(flow_of(self.a).join(text).code, "own_link")
        with self.b.env():
            f = flow_of(self.b, idle=self.settle)
            self.assertEqual(f.join("hello").code, "not_a_link")
            self.assertEqual(f.join(self.a.card()).code, "card")
            self.assertEqual(f.join(text[:-6] + "AAAAAA").code, "bad_link")
            self.assertTrue(f.join(text, wait=30).ok)
            self.assertEqual(f.join(text).code, "pasted")

    def busy_put(self, hub):
        """The put of a terminal whose core is busy: the request lies in the mailbox, the core has not recorded it (None)."""
        from core import spool

        def put(type_, entity, data, say=True):
            spool.put(os.path.join(hub.h.state_dir, "spool"), type_, entity, {**data, **OWNER, "req": "r" + str(time.time())},
                      src="cli")
        return put

    def queued_joins(self, hub):
        from core import spool
        out = []
        for _, item in spool.take(os.path.join(hub.h.state_dir, "spool")):
            if item["type"] == "bus.join_requested":
                out.append(item)
        return out

    def test_a_busy_core_keeps_the_invitation_file_and_says_the_request_runs_by_itself(self):
        text = self.link_text()
        with self.b.env():
            f = busflow.Flow(self.busy_put(self.b), lambda: self.b.h.state, self.b.h.state_dir, config_dir=self.b.config)
            got = f.join(text)
        self.assertEqual(got.code, "queued", got)
        self.assertEqual(got.text, busflow.why("queued"))
        self.assertIn("hub bus", got.text)
        # the file the core will read when it gets to the mailbox is still there
        self.assertEqual(len(os.listdir(busd.join_dir(self.b.h.state_dir))), 1)
        # the core gets to the mailbox: the request is carried out, not refused as «not a file»
        with self.b.env():
            self.b.h.ingest()
        self.settle()
        self.assertEqual(facts(self.b, "core.bus_request_failed"), [])
        (cb,) = self.b.bus["contacts"].values()
        self.assertEqual(self.b.bus["joins"][next(iter(self.b.bus["joins"]))]["state"], "linked")
        self.assertEqual(os.listdir(busd.join_dir(self.b.h.state_dir)), [])

    def test_a_file_whose_request_waits_in_the_mailbox_is_not_swept_however_long_the_core_takes(self):
        text = self.link_text()
        with self.b.env():
            f = busflow.Flow(self.busy_put(self.b), lambda: self.b.h.state, self.b.h.state_dir, config_dir=self.b.config)
            self.assertEqual(f.join(text).code, "queued")
        (name,) = os.listdir(busd.join_dir(self.b.h.state_dir))
        path = os.path.join(busd.join_dir(self.b.h.state_dir), name)
        old = time.time() - 3 * 3600   # the core was busy for hours: the request is still there
        os.utime(path, (old, old))
        stray = os.path.join(busd.join_dir(self.b.h.state_dir), "0" * 64 + ".link")   # nobody asked for this one
        with open(stray, "w") as g:
            g.write("x")
        os.utime(stray, (old, old))
        with self.b.env():
            busd.link_files(self.b.h)
        self.assertTrue(os.path.exists(path))
        self.assertFalse(os.path.exists(stray))
        with self.b.env():
            self.b.h.ingest()
        self.settle()
        self.assertEqual(facts(self.b, "core.bus_request_failed"), [])
        self.assertEqual(len(self.b.bus["contacts"]), 1)

    def test_a_repeat_while_the_first_waits_in_the_mailbox_asks_nothing_twice(self):
        text = self.link_text()
        with self.b.env():
            f = busflow.Flow(self.busy_put(self.b), lambda: self.b.h.state, self.b.h.state_dir, config_dir=self.b.config)
            self.assertEqual(f.join(text).code, "queued")
            self.assertEqual(f.join(text).code, "queued")
        self.assertEqual(len(self.queued_joins(self.b)), 1)
        with self.b.env():
            self.b.h.ingest()
        self.settle()
        self.assertEqual(facts(self.b, "core.bus_request_failed"), [])
        self.assertEqual(len(self.b.bus["contacts"]), 1)
        self.assertEqual(len(self.a.bus["contacts"]), 1)

    def test_a_repeat_after_the_mailbox_was_taken_is_a_refusal_not_a_second_contact(self):
        text = self.link_text()
        with self.b.env():
            f = busflow.Flow(self.busy_put(self.b), lambda: self.b.h.state, self.b.h.state_dir, config_dir=self.b.config)
            self.assertEqual(f.join(text).code, "queued")
            self.b.h.ingest()
        self.settle()
        with self.b.env():
            again = flow_of(self.b, idle=self.settle).join(text)
        self.assertEqual(again.code, "pasted", again)
        self.assertEqual(len(self.b.bus["contacts"]), 1)

    def test_a_refusal_before_the_core_read_the_file_still_removes_it(self):
        text = self.link_text()
        with self.b.env():
            got = flow_of(self.b, idle=self.settle).join(text, wait=30)
            self.assertTrue(got.ok, got)
            again = flow_of(self.b, idle=self.settle).join(text)
        self.assertEqual(again.code, "pasted")
        self.assertEqual(os.listdir(busd.join_dir(self.b.h.state_dir)), [])

    def test_the_terminal_of_a_busy_core_is_not_told_it_is_refused(self):
        text = self.link_text()
        out, err = io.StringIO(), io.StringIO()
        with self.b.env(), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("sys.stdin", io.StringIO(text)):
            code = buscli.main(["join", "-", "--yes"], self.busy_put(self.b), lambda: self.b.h.state, self.b.h.state_dir,
                               rejected={}, note=lambda kind, data: None, idle=lambda: None)
        self.assertEqual(code, 0, out.getvalue() + err.getvalue())
        self.assertIn(busflow.why("queued"), out.getvalue())
        self.assertNotIn(busflow.i18n.t("cli.bus.refused", reason=""), out.getvalue())

    def test_the_hint_after_a_join_names_the_contact(self):
        text = self.link_text()
        out, err = io.StringIO(), io.StringIO()
        with self.b.env(), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("sys.stdin", io.StringIO(text)):
            f = flow_of(self.b)
            code = buscli.main(["join", "-", "--yes"], f.put, lambda: self.b.h.state, self.b.h.state_dir,
                               rejected=f.rejected, note=lambda kind, data: None, idle=self.settle)
        self.assertEqual(code, 0, out.getvalue() + err.getvalue())
        (name,) = self.b.bus["contacts"]
        self.assertIn(f"hub bus contact verify {name}", out.getvalue())
        self.assertNotIn("<", out.getvalue().split("verify", 1)[1].split("\n")[0])

    # --- a session brings the invitation of another hub (hub bus offer) -------------------------------------------------

    def offer(self, text, session="maps", args=("-",), make=True):
        """`hub bus offer` of a session of B: (exit code, what it printed)."""
        if make:
            self.b.up(session)
        rejected = {}

        def put(type_, entity, data, say=True):
            ev = self.b.rec(type_, {**data, "channel": "session", "owner": False, "sender": session}, entity)
            why = self.b.verdict(ev)
            if why:
                rejected[ev["seq"]] = why
            return ev["seq"]
        out, err = io.StringIO(), io.StringIO()
        with self.b.env(), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("sys.stdin", io.StringIO(text)):
            code = buscli.main(["offer", *args], put, lambda: self.b.h.state, self.b.h.state_dir, me=session,
                               rejected=rejected, note=lambda kind, data: None, idle=lambda: None)
        return code, out.getvalue() + err.getvalue()

    def press(self, key="link"):
        (card,) = self.b.cards()
        ev = self.b.answer(card, key)
        self.assertIsNone(self.b.verdict(ev))
        return ev

    def test_a_session_brings_an_invitation_as_a_card_and_the_press_joins_it(self):
        text = self.link_text()
        code, said = self.offer(f"here is the link from a friend:\n{text}\n")
        self.assertEqual(code, 0, said)
        self.assertIn(busflow.i18n.t("cli.bus.offer_done", label="Hub A"), said)
        # a card to the owner, nothing joined yet, the file is the core's own copy
        (card,) = self.b.cards()
        self.assertEqual(card["hubbus"]["kind"], "offer")
        question = card["input"]["questions"][0]["question"]
        self.assertIn("maps", question)
        self.assertIn("Hub A", question)
        self.assertEqual(list(card["hubbus"]["labels"]), ["link", "no"])
        self.assertEqual((self.b.bus["contacts"], self.b.bus["joins"]), ({}, {}))
        self.assertEqual(os.listdir(busd.offer_dir(self.b.h.state_dir)), [])
        (rec,) = self.b.bus["offers"].values()
        self.assertEqual((rec["state"], rec["by"]), ("open", "maps"))
        self.assertEqual(len(os.listdir(os.path.dirname(busd.offered_path(self.b.h.state_dir, "x")))), 1)
        # «Link»: the join of a pasted invitation, the core's copy goes
        self.press("link")
        self.settle()
        (name,) = self.b.bus["contacts"]
        self.assertEqual(self.b.bus["joins"][next(iter(self.b.bus["joins"]))]["state"], "linked")
        self.assertFalse(self.b.bus["contacts"][name]["verified"])   # the check of the pair code is a separate press
        self.assertEqual(len(self.a.bus["contacts"]), 1)
        self.assertEqual(self.b.cards(), [])
        self.assertEqual(next(iter(self.b.bus["offers"].values()))["state"], "taken")
        self.assertEqual(os.listdir(os.path.dirname(busd.offered_path(self.b.h.state_dir, "x"))), [])
        self.assertEqual(os.listdir(busd.join_dir(self.b.h.state_dir)), [])
        notes = [e for e in facts(self.b, "owner.notify") if (e["data"].get("bus") or {}).get("kind") == "linked"]
        self.assertEqual(len(notes), 1)   # «Linked», the code and «Matches» — the same notice as for a pasted one

    def test_the_same_invitation_twice_makes_one_card(self):
        text = self.link_text()
        self.assertEqual(self.offer(text)[0], 0)
        code, said = self.offer(text)
        self.assertEqual(code, 0, said)
        self.assertIn(busflow.i18n.t("cli.bus.offer_again", label="Hub A"), said)
        self.assertEqual(len(self.b.cards()), 1)
        # a request that slipped past the session's own look (two sessions at once): the core refuses it as `offered`
        raw = text.encode("ascii")
        digest = hashlib.sha256(raw).hexdigest()
        folder = busd.offer_dir(self.b.h.state_dir)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, digest + ".link")
        with open(path, "wb") as f:
            f.write(raw)
        ev = self.b.rec("bus.offer_requested", {"path": path, "sha256": digest, "channel": "session", "owner": False,
                                                "sender": "tiles"})
        self.assertIsNone(self.b.verdict(ev))
        (failed,) = [e for e in facts(self.b, "core.bus_request_failed") if e["data"]["of"] == ev["seq"]]
        self.assertEqual(failed["data"]["code"], "offered")
        self.assertFalse(os.path.exists(path))
        self.assertEqual(len(self.b.cards()), 1)

    def test_no_and_the_close_button_do_nothing_and_remove_the_file(self):
        text = self.link_text()
        self.offer(text)
        self.press("no")
        self.assertEqual((self.b.bus["contacts"], self.b.bus["joins"]), ({}, {}))
        self.assertEqual(next(iter(self.b.bus["offers"].values()))["state"], "declined")
        self.assertEqual(os.listdir(os.path.dirname(busd.offered_path(self.b.h.state_dir, "x"))), [])
        # a session does not bring it again at once after «No» (a day, then the record goes)
        code, said = self.offer(text)
        self.assertEqual(code, 1, said)
        self.assertIn(busflow.why("owner_said_no"), said)
        self.assertEqual(self.b.cards(), [])
        # the close button of a card is no yes either
        other = self.link_text()
        self.assertEqual(self.offer(other)[0], 0)
        (card,) = self.b.cards()
        self.b.rec("question.dismissed", {"request_id": card["request_id"], "id": card["id"], "channel": "web",
                                          "owner": True}, entity=liaison.NAME)
        self.assertEqual(self.b.cards(), [])
        self.assertEqual((self.b.bus["contacts"], self.b.bus["joins"]), ({}, {}))
        from tests.test_hubbus_l0 import make_link
        text2 = make_link(self.a, ttl=604800)[0]
        self.b.now += 2 * 86400   # the records of the old «No» and of the closed card go with the next offer
        self.assertEqual(self.offer(text2)[0], 0)
        self.assertEqual(len(self.b.cards()), 1)
        self.assertEqual(len(self.b.bus["offers"]), 1)

    def test_a_repeat_while_the_offer_waits_in_the_mailbox_makes_no_second_request(self):
        text = self.link_text()
        self.b.up("maps")

        def put(type_, entity, data, say=True):   # the core is busy: the request lies in the mailbox
            from core import spool
            spool.put(os.path.join(self.b.h.state_dir, "spool"), type_, entity,
                      {**data, "channel": "session", "owner": False, "sender": "maps", "req": "r" + str(time.time())}, src="session")
        outs = []
        for _ in range(2):
            out = io.StringIO()
            with self.b.env(), contextlib.redirect_stdout(out), mock.patch("sys.stdin", io.StringIO(text)):
                code = buscli.main(["offer", "-"], put, lambda: self.b.h.state, self.b.h.state_dir, me="maps",
                                   rejected={}, note=lambda kind, data: None, idle=lambda: None)
            outs.append((code, out.getvalue()))
        self.assertEqual([c for c, _ in outs], [0, 0], outs)
        self.assertIn(busflow.i18n.t("cli.bus.offer_queued", label="Hub A"), outs[1][1])
        self.assertEqual(sum(1 for n in os.listdir(os.path.join(self.b.h.state_dir, "spool"))
                             if "bus.offer_requested" in open(os.path.join(self.b.h.state_dir, "spool", n)).read()), 1)
        with self.b.env():
            self.b.h.ingest()
        self.assertEqual(len(self.b.cards()), 1)
        self.assertEqual(facts(self.b, "core.bus_request_failed"), [])

    def test_the_owners_own_terminal_does_not_bring_an_invitation(self):
        out = io.StringIO()
        with self.b.env(), contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as stop:
            buscli.main(["offer", "-"], lambda *a, **k: None, lambda: self.b.h.state, self.b.h.state_dir, me=None,
                        rejected={}, note=lambda kind, data: None, idle=lambda: None)
        self.assertIn("hub bus join -", str(stop.exception))
        ev = self.b.rec("bus.offer_requested", {"path": "x", "sha256": "0" * 64, "channel": "cli", "owner": True})
        self.assertTrue(self.b.verdict(ev))

    def test_a_press_that_cannot_join_is_told_to_the_owner(self):
        text = self.link_text()
        self.offer(text)
        # the same invitation was pasted by hand meanwhile: the press finds it taken
        with self.b.env():
            self.assertTrue(flow_of(self.b, idle=self.settle).join(text, wait=30).ok)
        self.press("link")
        notes = [e["data"] for e in facts(self.b, "owner.notify") if e["data"].get("title") == busflow.i18n.t("core.bus.offer_failed_title")]
        self.assertEqual(len(notes), 1, notes)
        self.assertEqual(len(self.b.bus["contacts"]), 1)

    def test_the_bus_off_takes_the_cards_and_the_copies_of_the_invitations(self):
        self.offer(self.link_text())
        self.assertEqual(len(os.listdir(os.path.dirname(busd.offered_path(self.b.h.state_dir, "x")))), 1)
        self.b.ask("bus.off_requested", {})
        self.assertEqual(self.b.cards(), [])
        self.assertEqual(os.listdir(os.path.dirname(busd.offered_path(self.b.h.state_dir, "x"))), [])

    def test_the_copy_of_a_pressed_offer_is_not_swept_before_it_is_taken(self):
        self.offer(self.link_text())
        (lid,) = self.b.bus["offers"]
        self.b.bus["offers"][lid]["state"] = "taken"   # the press is folded, the action has not run yet
        with self.b.env():
            busd.link_files(self.b.h)
        self.assertTrue(os.path.exists(busd.offered_path(self.b.h.state_dir, lid)))

    def test_a_no_is_forgotten_after_a_day_and_the_bus_off_is_not_a_no(self):
        from tests.test_hubbus_l0 import make_link
        text = make_link(self.a, ttl=604800)[0]
        self.assertEqual(self.offer(text)[0], 0)
        self.press("no")
        self.assertEqual(self.offer(text)[0], 1)
        self.b.now += 2 * 86400   # the record of the «No» is a day old: the invitation may come again, no other offer needed
        self.assertEqual(self.offer(text)[0], 0)
        self.assertEqual(len(self.b.cards()), 1)
        # the bus off and on again: the pending offer is not a refusal of the owner
        self.b.ask("bus.off_requested", {})
        self.b.ask("bus.on_requested", {})
        self.b.up(liaison.NAME)
        code, said = self.offer(text)
        self.assertEqual(code, 0, said)
        self.assertEqual(len(self.b.cards()), 1)

    def test_no_session_to_show_the_card_is_said_to_the_session_and_nothing_is_kept(self):
        self.b.up("maps")
        del self.b.st["sessions"][liaison.NAME]
        code, said = self.offer(self.link_text(), make=False)
        self.assertEqual(code, 1, said)
        self.assertIn(busflow.why("no_card"), said)
        self.assertEqual(self.b.bus.get("offers") or {}, {})
        self.assertEqual(os.listdir(os.path.dirname(busd.offered_path(self.b.h.state_dir, "x")))
                         if os.path.isdir(os.path.dirname(busd.offered_path(self.b.h.state_dir, "x"))) else [], [])

    def test_only_the_owner_presses(self):
        self.offer(self.link_text())
        (card,) = self.b.cards()
        q = card["input"]["questions"][0]["question"]
        ev = self.b.rec("permission.answered", {"request_id": card["request_id"], "behavior": "allow",
                                                "answers": {q: card["hubbus"]["labels"]["link"]}, "channel": "session",
                                                "owner": False, "sender": "maps"}, entity=liaison.NAME)
        self.assertTrue(self.b.verdict(ev))
        self.assertEqual(len(self.b.cards()), 1)
        self.assertEqual(self.b.bus["joins"], {})

    def test_who_may_bring_and_how_many(self):
        text = self.link_text()
        base = {"path": "x", "sha256": "0" * 64}
        for extra in ({"channel": "plugin", "owner": False, "sender": "mod:x"},
                      {"channel": "session", "owner": False, "sender": liaison.NAME}):
            self.assertTrue(self.b.verdict(self.b.rec("bus.offer_requested", {**base, **extra})), extra)
        for _ in range(busstate.OFFER_MAX):
            self.assertEqual(self.offer(self.link_text())[0], 0)
        code, said = self.offer(self.link_text())
        self.assertEqual(code, 1, said)
        self.assertIn("offers_full", said)
        self.assertEqual(len(self.b.cards()), busstate.OFFER_MAX)

    def test_what_is_not_an_invitation_or_is_no_good_is_said_to_the_session(self):
        self.assertEqual(self.offer("plain text")[0], 1)
        code, said = self.offer(self.a.card())
        self.assertEqual(code, 1, said)
        self.assertIn(busflow.i18n.t("cli.bus.offer_not_link"), said)
        code, said = self.offer(self.link_text()[:-6] + "AAAAAA")
        self.assertEqual(code, 1, said)
        self.assertIn(busflow.why("bad_link"), said)
        self.assertEqual(self.b.cards(), [])

    def test_an_offer_nobody_pressed_ends_with_its_term_and_leaves_no_file(self):
        self.offer(self.link_text())
        self.b.now += 2 * 86400
        self.settle()
        self.assertEqual(self.b.cards(), [])
        self.assertEqual(next(iter(self.b.bus["offers"].values()))["state"], "expired")
        self.assertEqual(os.listdir(os.path.dirname(busd.offered_path(self.b.h.state_dir, "x"))), [])

    # --- the relay on the card, the outcome to the session, the hint to a session that tried `join` -------------------

    RELAY = {"url": "https://relay.example.test", "key": base64.b64encode(bytes(range(32))).decode()}

    def relay_link(self):
        """A's link that carries the permit of a relay (as `hub bus link` of a hub on a relay makes it)."""
        relay = {**self.RELAY, "ref": busflow.busrelay.ref(self.RELAY["url"], self.RELAY["key"])}
        with self.a.env():
            keys = bus.load(bus.key_dir(self.a.config))
        return bus.make_link(keys, "Hub A", int(time.time()), relay=relay)[0]

    def told(self, session="maps"):
        return [m["text"] for m in self.b.messages_to(session)]

    def test_the_card_names_the_relay_the_link_would_give_a_hub_without_one(self):
        self.assertEqual(self.offer(self.relay_link())[0], 0)
        (card,) = self.b.cards()
        question = card["input"]["questions"][0]["question"]
        self.assertIn(self.RELAY["url"], question)
        self.assertIn(busflow.i18n.t("core.bus.card.offer_relay", relay=self.RELAY["url"]), question)

    def test_the_card_does_not_name_a_relay_the_hub_is_on_already_or_a_link_has_none(self):
        self.b.bus["relay"] = {**self.RELAY, "ref": busflow.busrelay.ref(self.RELAY["url"], self.RELAY["key"])}
        self.assertEqual(self.offer(self.relay_link())[0], 0)
        (card,) = self.b.cards()
        self.assertNotIn(self.RELAY["url"], card["input"]["questions"][0]["question"])
        self.b.ask("bus.off_requested", {})
        self.b.bus["relay"] = None
        self.b.ask("bus.on_requested", {})
        self.assertEqual(self.offer(self.link_text())[0], 0)
        (card,) = self.b.cards()
        self.assertNotIn(busflow.i18n.t("core.bus.card.offer_relay", relay=""), card["input"]["questions"][0]["question"])

    def test_the_session_hears_that_the_hub_is_linked(self):
        self.offer(self.link_text())
        self.assertEqual(self.told(), [])
        self.press("link")
        self.settle()
        (name,) = self.b.bus["contacts"]
        lang = busflow.i18n.state_lang(self.b.st)
        self.assertEqual(self.told(), [busflow.i18n.tr(lang, "core.bus.offer_told_linked", label="Hub A", name=name)])
        self.settle()   # nothing repeats
        self.assertEqual(len(self.told()), 1)

    def test_the_session_hears_the_owners_no(self):
        self.offer(self.link_text())
        self.press("no")
        lang = busflow.i18n.state_lang(self.b.st)
        self.assertEqual(self.told(), [busflow.i18n.tr(lang, "core.bus.offer_told_no", label="Hub A")])

    def test_the_session_hears_why_the_press_did_not_link(self):
        text = self.link_text()
        self.offer(text)
        with self.b.env():   # pasted by hand meanwhile: the press finds the link taken
            self.assertTrue(flow_of(self.b, idle=self.settle).join(text, wait=30).ok)
        self.press("link")
        lang = busflow.i18n.state_lang(self.b.st)
        self.assertEqual(self.told(), [busflow.i18n.tr(lang, "core.bus.offer_told_failed", label="Hub A",
                                                       why=busflow.why("pasted", lang=lang))])

    def test_the_session_hears_that_the_copy_of_the_invitation_was_gone_at_the_press(self):
        self.offer(self.link_text())
        (lid,) = self.b.bus["offers"]
        os.unlink(busd.offered_path(self.b.h.state_dir, lid))
        self.press("link")
        lang = busflow.i18n.state_lang(self.b.st)
        self.assertEqual(self.told(), [busflow.i18n.tr(lang, "core.bus.offer_told_failed", label="Hub A",
                                                       why=busflow.why("lost", lang=lang))])

    def taken(self):
        """The offer pressed «Link»; the join of the core's copy is on its first step (nothing settled yet)."""
        self.offer(self.link_text())
        self.press("link")
        (lid,) = self.b.bus["joins"]
        return lid

    def test_the_session_hears_a_refusal_of_the_join_after_the_press(self):
        lid = self.taken()
        self.assertEqual(self.told(), [])
        self.b.rec("core.bus_join_step", {"id": lid, "step": "refused", "code": "other_relay"})
        lang = busflow.i18n.state_lang(self.b.st)
        self.assertEqual(self.told(), [busflow.i18n.tr(lang, "core.bus.offer_told_failed", label="Hub A",
                                                       why=busflow.why("other_relay", lang=lang))])

    def test_a_late_answer_after_the_term_still_corrects_what_the_session_was_told(self):
        lid = self.taken()
        self.b.rec("core.bus_join_step", {"id": lid, "step": "expired"})
        self.assertEqual(len(self.told()), 1)
        self.settle()
        self.assertEqual(self.b.bus["joins"][lid]["state"], "linked")
        self.assertEqual(len(self.told()), 2)
        (name,) = self.b.bus["contacts"]
        self.assertEqual(self.told()[1], busflow.i18n.tr(busflow.i18n.state_lang(self.b.st), "core.bus.offer_told_linked",
                                                         label="Hub A", name=name))

    def test_the_session_hears_that_the_owner_cancelled_the_join(self):
        lid = self.taken()
        self.b.ask("bus.join_cancel_requested", {"id": lid})
        self.settle()   # the introduction went already: the cancel ends with A's answer
        self.assertEqual(self.b.bus["joins"][lid]["state"], "cancelled")
        lang = busflow.i18n.state_lang(self.b.st)
        self.assertEqual(self.told(), [busflow.i18n.tr(lang, "core.bus.offer_told_cancel", label="Hub A")])

    def test_a_cancel_before_the_introduction_is_told_at_once(self):
        lid = self.taken()
        self.b.bus["joins"][lid].pop("intro_id", None)
        self.b.ask("bus.join_cancel_requested", {"id": lid})
        self.assertEqual(self.b.bus["joins"][lid]["state"], "cancelled")
        self.assertEqual(len(self.told()), 1)

    def test_a_cancel_that_waits_for_the_other_hub_is_told_when_it_ends(self):
        lid = self.taken()
        self.b.bus["joins"][lid]["intro_id"] = "i" * 16   # the introduction went: A may still answer
        self.b.ask("bus.join_cancel_requested", {"id": lid})
        self.assertEqual((self.b.bus["joins"][lid]["state"], self.told()), ("cancelling", []))
        self.b.rec("core.bus_join_step", {"id": lid, "step": "linked"})
        lang = busflow.i18n.state_lang(self.b.st)
        self.assertEqual((self.b.bus["joins"][lid]["state"], self.told()),
                         ("cancelled", [busflow.i18n.tr(lang, "core.bus.offer_told_cancel", label="Hub A")]))

    def test_a_crash_of_the_core_in_the_press_is_a_refusal_without_its_text_for_the_session(self):
        self.offer(self.link_text())
        with mock.patch.object(busd, "join", side_effect=RuntimeError("/secret/path broke")):
            self.press("link")
        lang = busflow.i18n.state_lang(self.b.st)
        self.assertEqual(self.told(), [busflow.i18n.tr(lang, "core.bus.offer_told_failed", label="Hub A",
                                                       why=busflow.why("error", lang=lang))])
        self.assertNotIn("secret", self.told()[0])

    def test_nobody_is_told_when_the_session_that_brought_it_is_gone(self):
        self.offer(self.link_text())
        del self.b.st["sessions"]["maps"]
        self.press("no")
        self.assertEqual(self.told(), [])

    def test_the_session_hears_that_nobody_pressed_in_time(self):
        self.offer(self.link_text())
        self.b.now += 2 * 86400
        self.settle()
        lang = busflow.i18n.state_lang(self.b.st)
        self.assertEqual(self.told(), [busflow.i18n.tr(lang, "core.bus.offer_told_expired", label="Hub A")])

    def test_the_session_hears_that_the_bus_went_off(self):
        self.offer(self.link_text())
        self.b.ask("bus.off_requested", {})
        lang = busflow.i18n.state_lang(self.b.st)
        self.assertEqual(self.told(), [busflow.i18n.tr(lang, "core.bus.offer_told_off", label="Hub A")])

    def test_the_words_to_the_session_are_in_the_language_of_the_state_not_of_the_process(self):
        self.b.st["ui_lang_default"] = "ru"
        self.addCleanup(busflow.i18n.use, busflow.i18n.current())
        busflow.i18n.use("en")
        self.offer(self.link_text())
        self.press("no")
        self.assertEqual(self.told(), [busflow.i18n.tr("ru", "core.bus.offer_told_no", label="Hub A")])
        self.assertNotEqual(busflow.i18n.tr("ru", "core.bus.offer_told_no", label="Hub A"),
                            busflow.i18n.tr("en", "core.bus.offer_told_no", label="Hub A"))

    def test_a_session_that_tries_join_is_pointed_to_offer(self):
        self.b.up("maps")
        for me, hint in (("maps", True), (liaison.NAME, False)):
            with self.b.env(), mock.patch("sys.stdin", io.StringIO(self.link_text())), \
                    self.assertRaises(SystemExit) as stop:
                buscli.main(["join", "-"], lambda *a, **k: None, lambda: self.b.h.state, self.b.h.state_dir, me=me,
                            rejected={}, note=lambda kind, data: None, idle=lambda: None)
            said = str(stop.exception)
            self.assertIn(busflow.i18n.t("cli.bus.owner_only", what="hub bus join"), said)
            self.assertEqual(busflow.i18n.t("cli.bus.join_offer_hint") in said, hint, said)
        self.assertIn("hub bus offer -", busflow.i18n.t("cli.bus.join_offer_hint"))

    def test_waiting_for_an_offline_a(self):
        text = self.link_text()
        with self.b.env():
            got = flow_of(self.b).join(text, wait=0)
        self.assertEqual(got.code, "waiting")
        self.assertIn("Hub A", got.text)

    def test_reset_removes_the_keys_and_the_next_on_makes_new_ones(self):
        old = self.b.bus["me"]["fp"]
        keydir = bus.key_dir(self.b.config)
        with self.b.env():
            got = flow_of(self.b).reset()
        self.assertTrue(got.ok, got)
        self.assertFalse(any(os.path.exists(os.path.join(keydir, n)) for n in bus.KEY_FILES))
        self.assertEqual((self.b.bus["enabled"], self.b.bus["contacts"]), (False, {}))
        self.b.ask("bus.on_requested", {})
        self.assertTrue(self.b.bus["enabled"])
        self.assertNotEqual(self.b.bus["me"]["fp"], old)

    def test_keys_a_cut_left_after_reset_are_never_taken_back(self):
        old = self.b.bus["me"]["fp"]
        keydir = bus.key_dir(self.b.config)
        saved = {}
        for n in bus.KEY_FILES:
            with open(os.path.join(keydir, n), "rb") as f:
                saved[n] = f.read()
        with self.b.env():
            flow_of(self.b).reset()
        for n, raw in saved.items():   # the daemon's removal did not happen (a cut)
            with open(os.path.join(keydir, n), "wb") as f:
                f.write(raw)
            os.chmod(os.path.join(keydir, n), 0o600)
        self.b.ask("bus.on_requested", {})
        self.assertTrue(self.b.bus["enabled"])
        self.assertNotEqual(self.b.bus["me"]["fp"], old)

    def test_a_hub_whose_core_has_the_library_installs_nothing_for_a_link(self):
        calls = []
        with self.a.env(), mock.patch.object(busflow.Flow, "has_library", lambda self: False):
            got = flow_of(self.a, run=lambda cmd: calls.append(cmd) or (1, "")).ensure(need_relay=False)
        self.assertTrue(got.ok, got)
        self.assertEqual(calls, [])
        steps = self.a.bus["setup"]["steps"]
        self.assertEqual([steps[s]["state"] for s in busflow.STEPS], ["ok", "skip", "skip", "skip", "ok", "skip"])
        # the key files moved away under the running core: the short path still sees it
        keydir = bus.key_dir(self.a.config)
        os.replace(os.path.join(keydir, "key.pub"), os.path.join(keydir, "key.pub.aside"))
        with self.a.env(), mock.patch.object(busflow.Flow, "has_library", lambda self: False):
            got = flow_of(self.a).ensure(need_relay=False)
        self.assertEqual((got.code, got.step), ("key_changed", "card"))

    def test_a_refused_reset_keeps_the_hub_on_its_relay(self):
        self.b.bus["relay"] = {"url": "https://relay.example.test", "key": "a2V5", "ref": "x"}
        left = []
        with self.b.env(), mock.patch.object(busflow.busrelay.Client, "leave", lambda c: left.append(c)), \
                mock.patch.object(busflow.busrelay, "transport_key", lambda *a, **k: ("key", "pub")):
            f = flow_of(self.b)
            with mock.patch.object(f, "ask", lambda *a: (busflow.Done("rejected", "no"), None)):
                self.assertEqual(f.reset().code, "rejected")
            self.assertEqual(left, [])
            self.assertTrue(f.reset().ok)
        self.assertEqual(len(left), 1)

    def test_a_reset_cut_halfway_leaves_no_way_back_to_the_old_keys(self):
        old = self.b.bus["me"]["fp"]
        keydir = bus.key_dir(self.b.config)
        saved = {}
        for n in ("key", "key.pub"):
            with open(os.path.join(keydir, n), "rb") as f:
                saved[n] = f.read()
        with self.b.env():
            flow_of(self.b).reset()
        for n, raw in saved.items():   # the cut: enc.key went, the signing key stayed
            with open(os.path.join(keydir, n), "wb") as f:
                f.write(raw)
            os.chmod(os.path.join(keydir, n), 0o600)
        self.b.ask("bus.on_requested", {})
        self.assertTrue(self.b.bus["enabled"], self.b.bus.get("last"))
        self.assertNotEqual(self.b.bus["me"]["fp"], old)

    def test_an_incomplete_set_of_other_keys_is_the_owners_to_decide(self):
        keydir = bus.key_dir(self.b.config)
        with self.b.env():
            flow_of(self.b).reset()
        work = tempfile.mkdtemp(prefix="bus-l1-k-")
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", os.path.join(work, "key")], check=True,
                       stdin=subprocess.DEVNULL)
        for n in ("key", "key.pub"):   # someone else's half set (a backup put back by hand), no exchange key
            os.replace(os.path.join(work, n), os.path.join(keydir, n))
        self.b.ask("bus.on_requested", {})
        self.assertFalse(self.b.bus["enabled"])
        self.assertEqual(self.b.bus["last"]["code"], "keys_broken")
        self.assertTrue(os.path.exists(os.path.join(keydir, "key.pub")))

    def test_doctor_rows(self):
        rows = busflow.doctor_rows(self.a.h.state_dir, self.a.h.state)
        self.assertEqual(rows[0][0], "ok")
        self.assertIn(self.a.bus["me"]["fp"], rows[0][2])
        self.assertIn(("warn", busflow.i18n.t("bus.doctor.what"), busflow.why("no_relay")), rows)
        self.assertEqual(busflow.doctor_rows(self.a.h.state_dir, {}),
                         [("ok", busflow.i18n.t("bus.doctor.what"), busflow.i18n.t("bus.doctor.off"))])
        st = {"bus": {"enabled": True, "crypto_ready": False, "failed": {"code": "keys_broken", "reason": "x"},
                      "me": {}, "setup": {"steps": {"lib": {"step": "lib", "state": "fail", "code": "no_network",
                                                            "command": "pip install"}}}}}
        rows = busflow.doctor_rows(self.a.h.state_dir, st)
        self.assertEqual(rows[0][0], "fail")
        self.assertTrue(any("pip install" in r[2] for r in rows))


class CleanHub:
    """A stand core with the bus off (tests/test_hubbus_s1.LiveHub without its `bus on`)."""

    def __new__(cls, label, now):
        from tests.test_hubbus_s1 import FakeHost, LiveHub
        from core.daemon import Hub
        hub = LiveHub.__new__(LiveHub)
        root = tempfile.mkdtemp(prefix="bus-l1-")
        hub.label, hub.now = label, now
        hub.config = os.path.join(root, "config")
        hub.book = os.path.join(hub.config, "contacts.json")
        with hub.env():
            hub.h = Hub(os.path.join(root, "state"), FakeHost())
            hub.h.clock = lambda: hub.now
            hub.ask("bus.name_requested", {"name": label})   # a link and a join need the hub's own name
        return hub


@need_lib
@need_ssh
class TwoCommandsThroughTheRelay(unittest.TestCase):
    """Two clean hubs, the relay of this tree: A pastes the operator's invitation (the first hub of the relay), makes a
    link; B pastes the link — both linked, one pair code; the owners' chat both ways without a turn; the unlink."""

    def setUp(self):
        from tests.test_hubbus_l0_relay import RELAY, Relay
        self.relay = Relay()
        self.addCleanup(self.relay.stop)
        if self.relay.version() < 2:
            self.skipTest(f"{RELAY}: the relay does not announce bus_v 2")
        now = int(time.time())
        self.a, self.b = CleanHub("Hub A", now), CleanHub("Hub B", now)
        self.ta, self.tb = self.transport(self.a), self.transport(self.b)

    def transport(self, hub):
        from modules.bus_relay.run import Transport

        def put(kind, data):
            with hub.env():
                hub.h.record(kind, None, data, src="bus-relay")
        t = Transport(hub.h.state_dir, hub.config, put=put, log=lambda text: None)
        t.stop.wait = lambda s: None
        return t

    def settle(self, rounds=12):
        for _ in range(rounds):
            moved = 0
            for hub, t in ((self.a, self.ta), (self.b, self.tb)):
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
                moved += hub.h.state["seq"] != before
            if not moved:
                return

    def cli(self, hub, argv, stdin="", code=0, idle=None, wait=None):
        def put(type_, entity, data, say=True):
            ev = hub.rec(type_, {**data, **OWNER}, entity)
            why = hub.verdict(ev)
            if why:
                rejected[ev["seq"]] = why
            return ev["seq"]
        rejected = {}
        out, err = io.StringIO(), io.StringIO()
        with hub.env(), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("sys.stdin", io.StringIO(stdin)), \
                mock.patch.object(busflow, "POSTED_WAIT", busflow.POSTED_WAIT if wait is None else wait):
            got = buscli.main(argv, put, lambda: hub.h.state, hub.h.state_dir, rejected=rejected,
                              note=lambda kind, data: hub.rec(kind, {**data, **OWNER}), idle=idle or self.settle)
        self.assertEqual(got, code, out.getvalue() + err.getvalue())
        return out.getvalue().strip(), err.getvalue()

    def test_link_join_chat_unlink(self):
        a, b = self.a, self.b
        # A: the operator's invitation — the bus comes up whole and the hub is on the relay
        out, err = self.cli(a, ["join", "-", "--yes"], stdin=self.relay.invite() + "\n")
        self.assertTrue(a.bus["enabled"])
        self.assertEqual(a.bus["relay"]["url"], self.relay.url)
        self.assertIn(busflow.i18n.t("cli.bus.invite_next"), out)
        # A: one line on stdout — the link
        link, err = self.cli(a, ["link", "--yes"])
        self.assertEqual(busflow.kind_of(link), "link")
        self.assertEqual(len(link.splitlines()), 1)
        self.assertNotIn(busflow.i18n.t("cli.bus.link_unposted"), err)   # its permit is on the relay already
        (rec,) = a.bus["links"].values()
        self.assertTrue(rec["posted"])
        # B: the link on stdin — linked, the pair code
        out, err = self.cli(b, ["join", "-", "--yes"], stdin=link + "\n")
        code = bus.contact_code(next(iter(b.bus["contacts"].values())))
        self.assertTrue(code)
        self.assertIn(code, out)
        self.assertEqual(b.bus["relay"]["url"], self.relay.url)
        self.settle()
        (na, ca), = a.bus["contacts"].items()
        (nb, cb), = b.bus["contacts"].items()
        self.assertEqual((ca["status"], cb["status"], ca["via"], cb["via"]), ("known", "known", "link", "link"))
        # the pair code in the list, the check with one command (the pair code is on both screens)
        out, _ = self.cli(b, ["contact", "ls"])
        self.assertIn(busflow.i18n.t("cli.bus.pair_unchecked", code=code, name=nb), out)
        self.cli(b, ["contact", "verify", nb])
        self.assertTrue(b.bus["contacts"][nb]["verified"])
        out, _ = self.cli(b, ["contact", "ls"])
        self.assertIn(busflow.i18n.t("cli.bus.pair_checked", code=code), out)
        wrong = "111-111" if code != "111-111" else "222-222"
        self.cli(a, ["contact", "verify", na, "--code", wrong], code=1)   # a typed code must match
        self.assertFalse(a.bus["contacts"][na]["verified"])
        self.cli(a, ["chat", na, "early"], code=1)   # «tell the hub» waits for the check, like any message for agents
        self.cli(a, ["contact", "verify", na])
        # the owner's words to the other hub both ways; no session turn
        turns = len(facts(a, "turn.started")) + len(facts(b, "turn.started"))
        self.cli(a, ["chat", na, "Hello", "from", "A"])
        self.settle()
        self.cli(b, ["chat", nb, "-"], stdin="And hello to you")
        self.settle()
        out, _ = self.cli(b, ["chat", nb])
        self.assertIn("← owner: Hello from A", out)
        out_a, _ = self.cli(a, ["chat", na])
        self.assertRegex(out_a, r"→ Hello from A +\[delivered\]")   # the mark of ours; the other hub's words carry what became of them
        self.assertIn("→ And hello to you", out)
        out, _ = self.cli(a, ["chat", na])
        self.assertIn("← owner: And hello to you", out)
        self.assertEqual(len(facts(a, "turn.started")) + len(facts(b, "turn.started")), turns)
        # the link never reached a journal or a state
        for hub in (a, b):
            with open(os.path.join(hub.h.state_dir, "journal.jsonl"), encoding="utf-8") as f:
                kept = f.read() + json.dumps(hub.h.state, default=str)
            self.assertNotIn(link[len(bus.LINK_PREFIX):][:40], kept)
        # unlink on A — B sees it
        self.cli(a, ["contact", "remove", na])
        self.settle()
        self.assertEqual(a.bus["contacts"], {})
        self.assertEqual(b.bus["contacts"], {})

    def test_the_link_is_never_an_argument_and_a_question_needs_a_terminal(self):
        out, _ = self.cli(self.b, ["join", "hub-link-1:" + "A" * 40], code=2)
        self.assertIn("hub bus join -", out)
        with mock.patch.object(buscli, "_confirm", lambda q: None):
            out, _ = self.cli(self.a, ["link"], code=1)
        self.assertIn("--yes", out)
        self.assertFalse(self.a.bus.get("enabled") if self.a.h.state.get("bus") else False)
        with mock.patch.object(buscli, "_confirm", lambda q: False):
            self.cli(self.a, ["join", "-"], stdin=self.relay.invite(), code=1)
        self.assertFalse((self.a.h.state.get("bus") or {}).get("enabled"))

    def test_an_invitation_on_a_hub_already_on_a_relay(self):
        self.cli(self.a, ["join", "-", "--yes"], stdin=self.relay.invite())
        out, _ = self.cli(self.a, ["join", "-", "--yes"], stdin=self.relay.invite())   # the same relay: nothing spent
        self.assertIn(self.relay.url, out)
        other = self.relay.invite().replace("hub-relay-invite-1:", "")
        import base64
        body, _, sig = other.partition(".")
        d = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        d["key"] = base64.b64encode(b"k" * 32).decode()
        forged = "hub-relay-invite-1:" + base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=") + "." + sig
        out, _ = self.cli(self.a, ["join", "-", "--yes"], stdin=forged, code=1)
        self.assertIn(busflow.why("relay_other"), out)

    def test_a_key_like_chat_text_needs_a_yes(self):
        self.cli(self.a, ["join", "-", "--yes"], stdin=self.relay.invite())
        link, _ = self.cli(self.a, ["link", "--yes"])
        self.cli(self.b, ["join", "-", "--yes"], stdin=link)
        (na, _), = self.a.bus["contacts"].items()
        self.cli(self.a, ["contact", "verify", na])
        text = "my key " + "sk-" + "proj-" + "q" * 24
        to_liaison = len([m for m in self.a.st["messages"].values() if m.get("session") == liaison.NAME])
        with mock.patch.object(buscli, "_confirm", lambda q: False):
            out, err = self.cli(self.a, ["chat", na, text], code=1)
        self.assertIn(busflow.why("secret"), err)
        self.assertFalse([o for o in self.a.bus["out"].values() if o.get("press") and o["status"] != "refused"])
        self.assertFalse([o for o in self.a.bus["out"].values() if o.get("text") == text])   # no body kept
        self.assertEqual(len([m for m in self.a.st["messages"].values() if m.get("session") == liaison.NAME]),
                         to_liaison)   # the liaison has no part in it
        out, _ = self.cli(self.a, ["chat", na, "--yes", text])
        self.assertIn(busflow.i18n.t("cli.bus.chat_sent", name=na), out)
        out, _ = self.cli(self.a, ["chat", na])
        self.assertEqual(out.count("sk-"), 1, out)   # the held-back one is not in the history

    def test_a_permit_not_on_the_relay_yet_is_said(self):
        self.cli(self.a, ["join", "-", "--yes"], stdin=self.relay.invite())
        out, err = self.cli(self.a, ["link", "--yes"], idle=lambda: None, wait=0)
        self.assertEqual(busflow.kind_of(out), "link")
        self.assertIn(busflow.i18n.t("cli.bus.link_unposted"), err)

    def test_link_without_a_relay_says_what_to_paste(self):
        out, err = self.cli(self.a, ["link", "--yes"], code=1)
        self.assertIn(busflow.why("no_relay"), out)
        self.assertTrue(self.a.bus["enabled"])   # the bus is on: the next step is the invitation


@unittest.skipUnless(os.environ.get("HUB_TEST_REAL_INSTALL") == "1", "a real install needs PyPI: HUB_TEST_REAL_INSTALL=1")
class RealInstall(unittest.TestCase):
    """The library from PyPI into `<state>/pylib` of a clean folder, with this interpreter, then imported by a separate
    process from there alone (spec §11)."""

    def test_install_into_a_clean_folder(self):
        state = tempfile.mkdtemp(prefix="bus-l1-real-")
        f = busflow.Flow(lambda *a, **k: None, lambda: {}, state, note=lambda t, d: None)
        with mock.patch.object(bus, "_LIB", None), mock.patch.object(bus, "crypto", side_effect=[
                bus.Unavailable("no_lib", "none"), types.SimpleNamespace()]):
            got = f.library(True)
        self.assertFalse(isinstance(got, busflow.Done), getattr(got, "text", ""))
        probe = ("import sys; sys.path.insert(0, sys.argv[1]); import cryptography; "
                 "from cryptography.hazmat.primitives import hpke; print(cryptography.__version__)")
        r = subprocess.run([sys.executable, "-I", "-c", probe, os.path.join(state, "pylib")], capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertGreaterEqual(int(r.stdout.split(".")[0]), bus.MIN_LIB[0])


if __name__ == "__main__":
    unittest.main()
