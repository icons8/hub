"""The owner's own name for a linked hub (`alias` of a bus contact; `hub bus contact rename`, the page's «Rename»): it is
kept only on this hub, shown wherever the contact's name is shown (the page's views, the cards, the notices, the
terminal), the label the other hub gave itself stays beside it; the address of the contact (its local name) and the
trust (its keys) never depend on it. Only the owner sets it; no control, format or separator characters; empty — cleared."""
import tests  # noqa: F401 — keeps the machine's settings out
import copy
import io
import unittest
from contextlib import redirect_stdout
import types

from core import buscards, buscli, busd, busflow, busstate, reducer

NOW = 1_800_000_000
OWNER = {"channel": "web", "owner": True}


def state():
    st = reducer.empty()
    st["bus"] = busstate.empty()
    st["bus"].update(enabled=True, crypto_ready=True, me={"fp": "hb1-me", "label": "Me"})
    st["bus"]["contacts"]["peer"] = {"fp": "hb1-d", "label": "Hub of P.", "status": "known", "verified": True,
                                      "sign_pub": "S", "enc_pub": "E", "may_write": [], "auto_from": []}
    return st


def rename(st, alias, name="peer", seq=5, **who):
    ev = {"seq": seq, "ts": NOW, "type": "bus.contact_requested", "entity": None, "src": "spool",
          "data": {"op": "rename", "name": name, "alias": alias, **(who or OWNER)}}
    return reducer.reduce(st, ev)


class Fold(unittest.TestCase):
    def test_the_owner_sets_and_clears_the_name_and_nothing_else_changes(self):
        st = state()
        before = copy.deepcopy(st["bus"]["contacts"]["peer"])
        st, actions, why = rename(st, "  Colleague's hub (laptop)  ")
        self.assertIsNone(why)
        self.assertEqual(actions, [])   # decided by the fold itself: no action of the daemon, no key, no envelope
        c = st["bus"]["contacts"]["peer"]
        self.assertEqual(c["alias"], "Colleague's hub (laptop)")
        self.assertEqual({k: v for k, v in c.items() if k != "alias"}, before)   # the card's label, the keys, the rights
        self.assertEqual(busstate.shown(c), "Colleague's hub (laptop)")
        self.assertEqual(st["bus"]["last"], {"of": 5, "code": "ok"})
        st, _, why = rename(st, "   ", seq=6)
        self.assertIsNone(why)
        self.assertEqual(st["bus"]["contacts"]["peer"], before)
        self.assertEqual(busstate.shown(st["bus"]["contacts"]["peer"]), "Hub of P.")

    def test_only_the_owner_may(self):
        for who in ({"channel": "session", "owner": False, "sender": "hub"},
                    {"channel": "session", "owner": True, "sender": "maps"},
                    {"channel": "plugin", "owner": False, "sender": "ext:x"},
                    {"channel": "mod:x", "owner": True, "sender": "mod:x"}):
            st, _, why = rename(state(), "A foreign name", **who)
            self.assertTrue(why, who)
            self.assertNotIn("alias", st["bus"]["contacts"]["peer"], who)

    def test_a_name_that_cannot_be_shown_safely_is_refused(self):
        for alias in ("Hub​", "Hub‮", "a\nb", "a\tb", "a b", "x" * 101, 5, None, ["a"]):
            st, _, why = rename(state(), alias)
            self.assertTrue(why, repr(alias))
            self.assertNotIn("alias", st["bus"]["contacts"]["peer"], repr(alias))
        st, _, why = rename(state(), "x" * 100)
        self.assertIsNone(why)

    def test_an_unknown_contact_and_the_bus_off_are_refused(self):
        _, _, why = rename(state(), "Name", name="nobody")
        self.assertIn("no bus contact", why)
        st = state()
        st["bus"]["enabled"] = False
        _, _, why = rename(st, "Name")
        self.assertIn("hub bus on", why)

    def test_it_survives_a_new_card_from_the_other_hub(self):
        st, _, _ = rename(state(), "Colleague's hub")
        busstate._apply_card(st["bus"]["contacts"]["peer"], {"label": "Peer's laptop", "agents": {}})
        c = st["bus"]["contacts"]["peer"]
        self.assertEqual((c["label"], busstate.shown(c)), ("Peer's laptop", "Colleague's hub"))

    def test_the_replay_of_the_journal_gives_the_same_state(self):
        a, b = state(), state()
        for seq, alias in ((5, "One"), (6, "Two")):
            a, _, _ = rename(a, alias, seq=seq)
            b, _, _ = rename(b, alias, seq=seq)
        self.assertEqual(a, b)
        self.assertEqual(a["bus"]["contacts"]["peer"]["alias"], "Two")


class Views(unittest.TestCase):
    def setUp(self):
        self.st, _, _ = rename(state(), "Colleague's hub (laptop)")

    def test_the_page_reads_the_name_as_the_label_and_the_stated_one_beside_it(self):
        v = busflow.page_view(self.st, now=10)["contacts"][0]
        self.assertEqual((v["label"], v["alias"], v["stated"]), ("Colleague's hub (laptop)", "Colleague's hub (laptop)", "Hub of P."))
        v = busflow.list_view(self.st)["contacts"][0]
        self.assertEqual((v["name"], v["label"], v["alias"], v["stated"]),
                         ("peer", "Colleague's hub (laptop)", "Colleague's hub (laptop)", "Hub of P."))

    def test_without_a_name_everything_is_as_before(self):
        st = state()
        for v in (busflow.page_view(st, now=10)["contacts"][0], busflow.list_view(st)["contacts"][0]):
            self.assertEqual((v["label"], v["alias"], v["stated"]), ("Hub of P.", None, "Hub of P."))

    def test_the_view_for_commands_and_the_cards_use_it(self):
        v = busstate.view(self.st)["contacts"]["peer"]
        self.assertEqual((v["label"], v["stated"]), ("Colleague's hub (laptop)", "Hub of P."))   # a search finds both
        contact = self.st["bus"]["contacts"]["peer"]
        card = buscards.intro("ru", {"label": "Hub of P.", "fp": "hb1-d", "code": "123-456", "agents": {}}, "peer")
        self.assertEqual(card["labels"]["accept"].count("peer"), 1)   # the name the card offers is the local one

    def test_unlinked_by_the_other_hub_is_told_by_the_owners_name(self):
        st = self.st
        st["bus"]["contacts"]["peer"]["pair"] = "p1"
        st, _, why = reducer.reduce(st, {"seq": 9, "ts": NOW, "type": "core.bus_contact_removed", "entity": None,
                                         "src": "core", "data": {"name": "peer", "unlinked": {
                                             "fp": "hb1-d", "pair": "p1", "label": "Colleague's hub (laptop)", "id": "x"}}})
        self.assertIsNone(why)
        self.assertEqual(st["bus"]["unlinked"]["hb1-d"]["label"], "Colleague's hub (laptop)")


class Cli(unittest.TestCase):
    """`hub bus contact rename <name> "<name>"`: the owner's word, folded at once; the line says what is shown now."""

    def cli(self, st, me=None):
        self.puts = []

        def put(type_, entity, data, say=False):
            self.puts.append((type_, data))
            nonlocal st
            st, _, why = rename(st, data["alias"], name=data["name"], seq=len(self.puts))
            if why:
                self.rejected[len(self.puts)] = why
            return len(self.puts)
        self.rejected = {}
        return buscli.Cli(put, lambda: st, "/nonexistent", me, False, self.rejected)

    def run_cli(self, cli, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli.contact(list(args))
        return rc, out.getvalue()

    def test_rename_and_clear(self):
        cli = self.cli(state())
        rc, out = self.run_cli(cli, "rename", "peer", "Colleague's hub (laptop)")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.puts[0], ("bus.contact_requested", {"op": "rename", "name": "peer",
                                                                  "alias": "Colleague's hub (laptop)"}))
        self.assertIn("Colleague's hub (laptop)", out)
        rc, out = self.run_cli(cli, "ls")
        self.assertIn("Colleague's hub (laptop)", out)
        self.assertIn("Hub of P.", out)   # what the hub calls itself — small, beside it
        rc, out = self.run_cli(cli, "rename", "peer", "")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.puts[1][1]["alias"], "")
        self.assertIn("Hub of P.", out)
        rc, out = self.run_cli(cli, "ls")
        self.assertEqual(out.count("Hub of P."), 1)

    def test_a_refusal_is_told_and_a_session_may_not(self):
        cli = self.cli(state())
        rc, out = self.run_cli(cli, "rename", "peer", "bad​name")
        self.assertEqual(rc, 1)
        self.assertIn("100", out)
        with self.assertRaises(SystemExit):
            self.cli(state(), me="maps").contact(["rename", "peer", "x"])

    def test_the_wrong_number_of_words_is_the_usage(self):
        with self.assertRaises(SystemExit):
            self.cli(state()).contact(["rename", "peer"])


class Notice(unittest.TestCase):
    """The other owner's words reach this owner (the page's banner, Telegram) under the owner's own name for the hub."""

    def test_the_chat_notice_names_the_hub_as_the_owner_does(self):
        st, _, _ = rename(state(), "Colleague's hub (laptop)")
        recorded = []
        hub = types.SimpleNamespace(state=st, config={}, record=lambda t, e, d: recorded.append((t, d)))
        busd._after(hub, "core.bus_chat_received", {"contact": "peer", "status": "ok", "text": "Hello", "for": "owner"})
        (type_, data), = recorded
        self.assertEqual(type_, "owner.notify")
        self.assertIn("Colleague's hub (laptop)", data["title"])
        self.assertNotIn("Hub of P.", data["title"])
        self.assertEqual(data["bus"], {"kind": "chat", "contact": "peer", "label": "Colleague's hub (laptop)"})


if __name__ == "__main__":
    unittest.main()
