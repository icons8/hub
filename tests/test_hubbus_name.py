"""The hub's own name for the other hubs (`bus.name_requested`, `hub bus name`, the field at the top of «External hubs»):
one required setting of the owner — an invitation is not made and one is not accepted without it (nothing is taken from
the page's title or the machine's name); the invitation's field only changes it for that invitation alone. The owner's
own names for other hubs (`alias`, tests/test_hubbus_rename.py) stay optional."""
import tests  # noqa: F401 — keeps the machine's settings out
import io
import json
import os
import unittest
from contextlib import redirect_stdout

from core import bus, busd, buscli, busflow, busstate, reducer
from tests.test_hubbus import need_lib, need_ssh

NOW = 1_800_000_000
OWNER = {"channel": "web", "owner": True}
LINK = "hub-link-1:" + "A" * 40 + "_-" + "b" * 30


def state(**bus_keys):
    st = reducer.empty()
    st["bus"] = busstate.empty()
    st["bus"].update(enabled=True, crypto_ready=True, me={"fp": "hb1-me", "label": "Hub"}, **bus_keys)
    return st


def fold(st, type_, data, seq=5, **who):
    ev = {"seq": seq, "ts": NOW, "type": type_, "entity": None, "src": "spool", "data": {**data, **(who or OWNER)}}
    return reducer.reduce(st, ev)


class Fold(unittest.TestCase):
    def test_the_owner_sets_the_name_and_the_card_carries_it(self):
        st, actions, why = fold(state(), "bus.name_requested", {"name": "  Sales department hub "})
        self.assertIsNone(why)
        self.assertEqual(actions, [])   # folded by itself: no daemon, no envelope
        self.assertEqual(st["bus"]["name"], "Sales department hub")
        self.assertEqual(st["bus"]["me"]["label"], "Sales department hub")   # what the next card says
        self.assertEqual(st["bus"]["last"], {"of": 5, "code": "ok"})
        self.assertTrue(busstate.folded_at_once("bus.name_requested", {"name": "x"}))

    def test_it_can_be_set_before_the_bus_is_on(self):
        st = reducer.empty()
        st["bus"] = busstate.empty()
        st, _, why = fold(st, "bus.name_requested", {"name": "Hub A"})
        self.assertIsNone(why)
        self.assertEqual(st["bus"]["name"], "Hub A")
        self.assertFalse(st["bus"]["enabled"])

    def test_only_a_name_that_can_be_shown_safely_and_only_the_owner(self):
        for name in ("", "   ", "Hub​", "Hub‮", "a\nb", "x" * 101, 5, None, ["a"]):
            st, _, why = fold(state(), "bus.name_requested", {"name": name})
            self.assertTrue(why, repr(name))
            self.assertNotIn("name", st["bus"], repr(name))
        for who in ({"channel": "session", "owner": False, "sender": "hub"},
                    {"channel": "session", "owner": True, "sender": "maps"},
                    {"channel": "plugin", "owner": False, "sender": "ext:x"}):
            st, _, why = fold(state(), "bus.name_requested", {"name": "Foreign"}, **who)
            self.assertTrue(why, who)
            self.assertNotIn("name", st["bus"], who)

    def test_no_invitation_without_the_name(self):
        for data in ({}, {"ttl": 3600}, {"label": "For a colleague"}):   # a field of the invitation is no replacement
            st, actions, why = fold(state(), "bus.link_requested", data)
            self.assertTrue(why and why.startswith("no_name"), (data, why))
            self.assertEqual(actions, [])

    def test_the_invitation_is_made_with_the_name_or_with_its_own_for_this_one(self):
        st = state(name="Sales department hub")
        _, actions, why = fold(st, "bus.link_requested", {})
        self.assertIsNone(why)
        (act,) = actions
        self.assertNotIn("label", act)   # the hub's own name: the daemon takes it
        _, actions, _ = fold(state(name="Sales department hub"), "bus.link_requested", {"label": " Sales department hub "})
        self.assertNotIn("label", actions[0])   # the same — nothing to remember
        _, actions, why = fold(state(name="Sales department hub"), "bus.link_requested", {"label": " For a partner "})
        self.assertIsNone(why)
        self.assertEqual(actions[0]["label"], "For a partner")
        for bad in ("", "a\nb", "x" * 101, 5):
            _, actions, why = fold(state(name="Hub A"), "bus.link_requested", {"label": bad})
            self.assertTrue(why, repr(bad))

    def test_no_accepting_a_link_without_the_name(self):
        data = {"path": "/x/y.link", "sha256": "0" * 64}
        st, actions, why = fold(state(), "bus.join_requested", data)
        self.assertTrue(why and why.startswith("no_name"), why)
        self.assertEqual(actions, [])
        _, actions, why = fold(state(name="Hub A"), "bus.join_requested", data)
        self.assertIsNone(why)
        self.assertEqual(actions[0]["do"], "bus_join")

    def test_a_journal_before_the_setting_is_folded_as_it_was(self):
        """A request taken before the setting existed changes nothing in the state when the journal is read again
        (the fold of a link or a join request is only an action of the daemon)."""
        for type_, data in (("bus.link_requested", {"ttl": 3600}), ("bus.join_requested", {"path": "/x", "sha256": "0" * 64})):
            st = state()
            before = json.dumps(st["bus"], sort_keys=True)
            ev = {"seq": 7, "ts": NOW, "type": type_, "entity": None, "src": "spool", "data": {**data, **OWNER}}
            new, _, _ = reducer.reduce(st, ev, known="accepted")
            self.assertEqual(json.dumps(new["bus"], sort_keys=True), before, type_)

    def test_the_view_says_the_name_is_missing(self):
        self.assertIsNone(busflow.page_view(state())["name"])
        self.assertEqual(busflow.page_view(state(name="Hub A"))["name"], "Hub A")

    def test_the_view_of_an_open_invitation_says_whose_it_is_and_its_own_name(self):
        base = {"state": "open", "exp": NOW + 3600, "ts": NOW}
        links = {"a" * 40: {**base, "label": "For partners", "person": {"id": "c1a2b3c4d", "name": "Pat Lee"}},
                 "b" * 40: dict(base),   # made before these fields: no person, no label, no failure
                 "c" * 40: {**base, "person": "junk"}}
        rows = busflow.page_view(state(name="Hub A", links=links), now=NOW)["links"]
        got = {r["id"][0]: (r["person"], r["label"]) for r in rows}
        self.assertEqual(got, {"a": ("Pat Lee", "For partners"), "b": (None, None), "c": (None, None)})


class Flow(unittest.TestCase):
    def flow(self, name=None):
        puts = []
        st = state(**({"name": name} if name else {}))

        def put(type_, entity, data, say=True):
            puts.append((type_, data))
            return None   # no answer: the flow stops there
        return busflow.Flow(put, lambda: st, "/nonexistent", rejected={}), puts

    def test_link_and_join_without_a_name_are_refused_before_anything_is_asked(self):
        f, puts = self.flow()
        for done in (f.link(), f.join(LINK), f.join("hub-relay-invite-1:" + "x" * 30 + "." + "y" * 30)):
            self.assertEqual(done.code, "no_name", done)
            self.assertIn("hub bus name", done.command)
            self.assertIn("Settings", done.text)
        self.assertEqual(puts, [])

    def test_a_name_of_the_invitation_that_cannot_be_shown_is_refused_before_anything_is_asked(self):
        f, puts = self.flow("Hub A")
        for label in ("x" * 101, "a\nb", "Hub\u202e"):
            done = f.link(label=label)
            self.assertEqual(done.code, "bad_label", label)
        self.assertEqual(puts, [])

    def test_a_card_or_a_foreign_text_is_not_a_name_matter(self):
        f, _ = self.flow()
        self.assertEqual(f.join("hub-bus-card-1:" + "QUJD" * 10 + "==").code, "card")
        self.assertEqual(f.join("hello").code, "not_a_link")


class Cli(unittest.TestCase):
    def run_cli(self, argv, st, answer="ok"):
        puts = []

        def put(type_, entity, data, say=True):
            puts.append((type_, data))
            return 1
        out = io.StringIO()
        with redirect_stdout(out):
            code = buscli.main(argv, put, lambda: st, "/nonexistent", me=None, outsider=None, rejected={},
                               note=lambda t, d: None, idle=lambda: None)
        return code, out.getvalue(), puts

    def test_the_liaison_note_goes_out_as_a_request(self):
        from core import liaison
        puts = []

        def put(type_, entity, data, say=True):
            puts.append((type_, data))
            return 1
        out = io.StringIO()
        with redirect_stdout(out):
            code = buscli.main(["note", "anna", "--on", "o1", "the", "text"], put, lambda: state(), "/nonexistent",
                               me=liaison.NAME, outsider=None, rejected={}, note=lambda t, d: None, idle=lambda: None)
        self.assertEqual(code, 0)
        self.assertEqual(puts, [("bus.note", {"contact": "anna", "text": "the text", "on": "o1"})])

    def test_no_attribute_of_the_cli_hides_a_method(self):
        cli = buscli.Cli(None, lambda: {}, "/nonexistent", None, None, {}, note=lambda t, d: None)
        methods = {n for n in dir(buscli.Cli) if callable(getattr(buscli.Cli, n)) and not n.startswith("__")}
        self.assertEqual(sorted(set(vars(cli)) & methods), [])

    def test_name_shows_and_says_what_is_missing(self):
        code, out, puts = self.run_cli(["name"], state(name="Hub A"))
        self.assertEqual((code, puts), (0, []))
        self.assertIn("Hub A", out)
        code, out, puts = self.run_cli(["name"], state())
        self.assertEqual((code, puts), (0, []))
        self.assertIn("hub bus name", out)

    def test_link_and_join_without_a_name_say_the_command_and_ask_nothing(self):
        for argv in (["link", "--yes"], ["join", "-"]):
            code, out, puts = self.run_cli(argv, state())
            self.assertEqual((code, puts), (1, []), argv)
            self.assertIn("hub bus name", out)

    def test_the_usage_names_the_new_words(self):
        from core import i18n
        text = i18n.t("cli.bus.usage")
        self.assertIn("name [", text)
        self.assertIn("--label", text)


@need_lib
@need_ssh
class Linked(unittest.TestCase):
    """A real pair: the invitation carries the name of the invitation, and so does the answer to it."""

    def setUp(self):
        from tests.test_hubbus_s1 import LiveHub, Wire
        self.a, self.b = LiveHub("Hub A"), LiveHub("Hub B")
        self.wire = Wire(self.a, self.b)

    def link_of(self, data):
        a = self.a
        before = set(a.bus["links"])
        ev = a.ask("bus.link_requested", data)
        self.assertIsNone(a.verdict(ev))
        (lid,) = set(a.bus["links"]) - before
        with open(busd.link_path(a.h.state_dir, lid), encoding="ascii") as f:
            return f.read().strip(), lid

    def test_the_hub_name_is_in_the_link_and_the_name_of_the_invitation_instead_of_it(self):
        text, lid = self.link_of({})
        self.assertEqual(bus.read_link(text, self.a.now)["card"]["label"], "Hub A")
        self.assertNotIn("label", self.a.bus["links"][lid])
        text, lid = self.link_of({"label": "Hub A for partners"})
        self.assertEqual(bus.read_link(text, self.a.now)["card"]["label"], "Hub A for partners")
        self.assertEqual(self.a.bus["links"][lid]["label"], "Hub A for partners")   # kept for the answer

    def test_the_other_hub_is_called_by_the_name_of_the_invitation(self):
        from tests.test_hubbus_l0 import paste
        a, b = self.a, self.b
        text, _ = self.link_of({"label": "Hub A for partners"})
        self.assertIsNone(b.verdict(paste(b, text)))
        self.wire.settle()
        (cb,) = b.bus["contacts"].values()
        self.assertEqual(cb["label"], "Hub A for partners")
        (ca,) = a.bus["contacts"].values()
        self.assertEqual(ca["label"], "Hub B")   # B made no field: its own name
