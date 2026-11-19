"""Free session groups and a custom icon on any session: the layout lives in the core (docs/spec-session-groups.md).

The layout is one layout.set event carrying the whole layout and the revision it was computed from (base): two tabs
do not silently overwrite each other. The icon is session.iconed. Both are the owner's or the hub's word; the fold on
any data is a refusal, not a crash."""
import tests  # noqa: F401 — ignore the machine's settings
import json
import os
import subprocess
import sys
import tempfile
import unittest

from core import reducer, spool
from core.views import Reader

OWNER = {"channel": "web", "owner": True}
CLI = {"channel": "cli", "owner": True}
FROM_HUB = {"channel": "session", "owner": False, "sender": "hub"}
FROM_WORKER = {"channel": "session", "owner": False, "sender": "maps"}

HUB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")
GROUPS = [{"id": "agents", "title": "Specialists"}, {"id": "g-a1", "title": "Clients"}]


class Core(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **CLI)
        self.ev("session.requested", "devops", cwd="/k", session_id="s-d", role="agent", about="DevOps", icon="🛠",
                **FROM_HUB)
        self.ev("session.requested", "maps", cwd="/w", session_id="s-m", **FROM_HUB)
        for name in ("hub", "devops", "maps"):
            self.ev("session.started", name)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": self.seq, "type": type, "entity": entity,
                                                   "data": data})
        return why

    def lay(self, layout, base=None, who=OWNER):
        return self.ev("layout.set", None, layout=layout, base=self.rev() if base is None else base, **who)

    def rev(self):
        return (self.st.get("layout") or {}).get("rev", 0)

    # --- layout --------------------------------------------------------------------------------------------------
    def test_set_keeps_whole_layout_with_rev(self):
        self.assertIsNone(self.lay({"groups": GROUPS, "place": {"maps": "g-a1"}, "order": {"g-a1": ["maps"]}}))
        lay = self.st["layout"]
        self.assertEqual(lay["rev"], self.seq)
        self.assertEqual(lay["groups"], GROUPS)
        self.assertEqual((lay["place"], lay["order"]), ({"maps": "g-a1"}, {"g-a1": ["maps"]}))

    def test_owner_and_hub_only(self):
        self.assertIsNone(self.lay({"groups": GROUPS}, who=FROM_HUB))
        self.assertIsNone(self.lay({"groups": GROUPS}, who=CLI))
        self.assertIn("the hub or the owner", self.lay({"groups": GROUPS}, who=FROM_WORKER))

    def test_stale_base_rejected(self):
        """Two tabs: the second computed from the old layout, so it is refused instead of silently losing the first."""
        self.assertIsNone(self.lay({"groups": GROUPS}, base=0))
        self.assertIn("was changed already", self.lay({"groups": GROUPS[:1]}, base=0))
        self.assertEqual(self.st["layout"]["groups"], GROUPS)

    def test_reset_keeps_rev_growing(self):
        """"Reset" means no groups, but the revision is not zeroed: a tab opened before all the edits cannot pass with base 0."""
        self.assertIsNone(self.lay({"groups": GROUPS}))
        self.assertIsNone(self.lay(None))
        self.assertEqual(self.st["layout"], {"rev": self.seq})
        self.assertIn("was changed already", self.lay({"groups": GROUPS}, base=0))

    def test_normalizes_place_and_order(self):
        self.assertIsNone(self.lay({"groups": GROUPS,
                                    "place": {"maps": "g-a1", "devops": "g-gone", "nobody": "g-a1", "hub": 5},
                                    "order": {"g-a1": ["maps", "maps", "nobody", 7], "g-gone": ["devops"]}}))
        lay = self.st["layout"]
        self.assertEqual(lay["place"], {"maps": "g-a1"})
        self.assertEqual(lay["order"], {"g-a1": ["maps"]})

    def test_bad_shapes_are_rejections_not_crashes(self):
        bad = ["x", 5, [], {"groups": "x"}, {"groups": [{"id": "a"}]}, {"groups": [{"title": "A"}]},
               {"groups": [{"id": "a b", "title": "A"}]}, {"groups": [{"id": "a", "title": ""}]},
               {"groups": [{"id": "a", "title": "A"}, {"id": "a", "title": "B"}]},
               {"groups": [{"id": "a", "title": "x" * 61}]}, {"groups": [{"id": "x" * 41, "title": "A"}]},
               {"groups": [{"id": f"g{i}", "title": "A"} for i in range(51)]},
               {"groups": GROUPS, "place": []}, {"groups": GROUPS, "order": {"g-a1": "maps"}},
               {"groups": [None]}, {"groups": [["a", "b"]]}]
        for layout in bad:
            why = self.lay(layout)
            self.assertIsNotNone(why, layout)
            self.assertNotIn("core failure", why, layout)
        self.assertNotIn("layout", self.st)
        for base in ("1", None, [], 1.5):
            why = self.ev("layout.set", None, layout={"groups": GROUPS}, base=base, **OWNER)
            if why:
                self.assertNotIn("core failure", why)

    def test_rename_moves_place_and_bumps_rev(self):
        self.assertIsNone(self.lay({"groups": GROUPS, "place": {"maps": "g-a1"}, "order": {"g-a1": ["devops", "maps"]}}))
        old = self.rev()
        self.ev("session.ended", "maps", reason="prompt_input_exit")
        self.assertEqual(self.st["sessions"]["maps"]["state"], "asleep")
        self.assertIsNone(self.ev("session.renamed", "maps", to="atlas", **CLI))
        lay = self.st["layout"]
        self.assertEqual((lay["place"], lay["order"]), ({"atlas": "g-a1"}, {"g-a1": ["devops", "atlas"]}))
        self.assertGreater(lay["rev"], old)   # a tab with the old name will be refused

    def test_removed_keeps_place_revive_returns_new_session_starts_clean(self):
        self.assertIsNone(self.ev("session.iconed", "maps", icon="🗺", **OWNER))
        self.assertIsNone(self.lay({"groups": GROUPS, "place": {"maps": "g-a1"}, "order": {"g-a1": ["maps"]}}))
        self.ev("session.ended", "maps", reason="prompt_input_exit")
        self.assertIsNone(self.ev("session.remove_requested", "maps", **CLI))
        self.assertEqual(self.st["layout"]["place"], {"maps": "g-a1"})
        # the same conversation came back: same group and own icon
        self.assertIsNone(self.ev("session.requested", "maps", cwd="/w", session_id="s-m", revive=True, **CLI))
        self.assertEqual(self.st["layout"]["place"], {"maps": "g-a1"})
        self.assertEqual(self.st["sessions"]["maps"].get("own_icon"), "🗺")
        # a new session under the same name starts from a clean slate
        self.ev("session.ended", "maps", reason="prompt_input_exit")
        self.assertIsNone(self.ev("session.remove_requested", "maps", **CLI))
        old = self.rev()
        self.assertIsNone(self.ev("session.requested", "maps", cwd="/w2", session_id="s-new", **CLI))
        self.assertEqual((self.st["layout"]["place"], self.st["layout"]["order"]), ({}, {}))
        self.assertGreater(self.rev(), old)
        self.assertIsNone(self.st["sessions"]["maps"].get("own_icon"))

    def test_first_session_does_not_create_layout(self):
        self.assertNotIn("layout", self.st)   # the layout was not touched: there is none, the view is as before

    # --- icon ----------------------------------------------------------------------------------------------------
    def test_icon_any_session_and_clear(self):
        for name, icon in (("maps", "🗺"), ("devops", "🚀"), ("hub", "🧠"), ("maps", "👨‍👩‍👧‍👦"), ("maps", "🇫🇷"),
                           ("maps", "⚙️"), ("maps", "★")):
            self.assertIsNone(self.ev("session.iconed", name, icon=icon, **OWNER), (name, icon))
            self.assertEqual(self.st["sessions"][name]["own_icon"], icon)
        self.assertEqual(self.st["sessions"]["devops"]["icon"], "🛠")   # the agent's previous icon is untouched
        self.assertIsNone(self.ev("session.iconed", "devops", icon=None, **OWNER))
        self.assertNotIn("own_icon", self.st["sessions"]["devops"])

    def test_icon_rules(self):
        for bad in ("abc", "a", "7", "🚀 ", " ", "\u0007", "🚀" * 17, 5, ["🚀"], {"x": 1}):
            why = self.ev("session.iconed", "maps", icon=bad, **OWNER)
            self.assertIsNotNone(why, repr(bad))
            self.assertNotIn("core failure", why)
        self.assertIn("the hub or the owner", self.ev("session.iconed", "maps", icon="🚀", **FROM_WORKER))
        self.assertIsNone(self.ev("session.iconed", "maps", icon="🚀", **FROM_HUB))
        self.assertIsNotNone(self.ev("session.iconed", "nobody", icon="🚀", **OWNER))

    def test_executor_copies_raisers_icon_at_birth(self):
        """An own icon belongs to the session itself: an executor gets its starter's icon as a value at birth (like the
        previous icon); if the starter changes or removes theirs, already started executors keep the old one, with no
        live inheritance."""
        self.assertIsNone(self.ev("session.iconed", "devops", icon="🚀", **OWNER))
        self.ev("session.requested", "devops-x", cwd="/k", session_id="s-x", executor=True, raised_by="devops", **CLI)
        born = lambda: self.st["sessions"]["devops-x"].get("own_icon")
        self.assertEqual(born(), "🚀")
        self.assertIsNone(self.ev("session.iconed", "devops", icon="🧭", **OWNER))
        self.assertEqual(born(), "🚀")
        self.assertIsNone(self.ev("session.iconed", "devops", icon=None, **OWNER))
        self.assertEqual(born(), "🚀")
        self.assertIsNone(self.ev("session.iconed", "devops-x", icon=None, **OWNER))
        x = self.st["sessions"]["devops-x"]
        self.assertEqual((x.get("own_icon"), x.get("icon")), (None, "🛠"))   # removed its own: the previous one, from birth
        from core import layout
        self.assertFalse(hasattr(layout, "shown_icon"))

    def test_reraised_hub_and_system_agent_keep_icon_and_place_from_any_state(self):
        """The core restarts the hub and a system agent with a new conversation (closed, crashed, removed; not revive):
        for the owner it is the same session, so its icon and place in the layout stay."""
        self.assertIsNone(self.ev("agents.system", None, names=["devops"], channel="core"))
        self.assertIsNone(self.ev("session.iconed", "hub", icon="🧠", **OWNER))
        self.assertIsNone(self.ev("session.iconed", "devops", icon="🚀", **OWNER))
        self.assertIsNone(self.lay({"groups": GROUPS, "place": {"devops": "g-a1", "hub": "agents"},
                                    "order": {"g-a1": ["devops"]}}))
        old = self.rev()
        for state in ("closed", "failed", "removed"):
            for name, kind in (("hub", "hub"), ("devops", "work")):
                self.st["sessions"][name]["state"] = state   # as in real journals (daemon.ensure_hub, system.returning)
                self.assertIsNone(self.ev("session.requested", name, cwd="/h", session_id=f"s-{state}-{name}",
                                          kind=kind, **CLI), (name, state))
        self.assertEqual((self.st["sessions"]["hub"].get("own_icon"), self.st["sessions"]["devops"].get("own_icon")),
                         ("🧠", "🚀"))
        lay = self.st["layout"]
        self.assertEqual((lay["place"], lay["order"], lay["rev"]),
                         ({"devops": "g-a1", "hub": "agents"}, {"g-a1": ["devops"]}, old))

    def test_other_session_under_old_name_starts_clean_from_any_state(self):
        """Any other session under the old name with a new conversation (not revive) is a new one: no own icon, no
        place, even if the old one was closed or crashed rather than removed."""
        for state in ("closed", "failed"):
            self.assertIsNone(self.ev("session.iconed", "maps", icon="🗺", **OWNER))
            self.assertIsNone(self.lay({"groups": GROUPS, "place": {"maps": "g-a1"}, "order": {"g-a1": ["maps"]}}))
            old = self.rev()
            self.st["sessions"]["maps"]["state"] = state
            self.assertIsNone(self.ev("session.requested", "maps", cwd="/w", session_id=f"s-{state}", **CLI))
            self.assertIsNone(self.st["sessions"]["maps"].get("own_icon"), state)
            self.assertEqual((self.st["layout"]["place"], self.st["layout"]["order"]), ({}, {}), state)
            self.assertGreater(self.rev(), old)

    def test_rename_onto_leftover_of_removed_forgets_it_first(self):
        """The new name is still in the layout from a removed session: the renamed one takes it cleanly, with its own
        place and order, no traces of the old one and no repeat in the order; the revision grows."""
        self.ev("session.requested", "atlas", cwd="/a", session_id="s-a", **CLI)
        self.assertIsNone(self.lay({"groups": GROUPS, "place": {"atlas": "g-a1", "maps": "agents"},
                                    "order": {"g-a1": ["atlas"], "agents": ["atlas", "devops", "maps"]}}))
        self.ev("session.ended", "atlas", reason="prompt_input_exit")
        self.assertIsNone(self.ev("session.remove_requested", "atlas", **CLI))
        self.ev("session.ended", "maps", reason="prompt_input_exit")
        old = self.rev()
        self.assertIsNone(self.ev("session.renamed", "maps", to="atlas", **CLI))
        lay = self.st["layout"]
        self.assertEqual((lay["place"], lay["order"]), ({"atlas": "agents"}, {"agents": ["devops", "atlas"]}))
        self.assertGreater(lay["rev"], old)

    def test_rename_outside_layout_keeps_rev(self):
        """A session absent from the layout was renamed: the revision stays, so an open tab is not refused for nothing."""
        self.assertIsNone(self.lay({"groups": GROUPS, "place": {"devops": "g-a1"}}))
        old = self.rev()
        self.ev("session.ended", "maps", reason="prompt_input_exit")
        self.assertIsNone(self.ev("session.renamed", "maps", to="atlas", **CLI))
        self.assertEqual(self.rev(), old)

    def test_reserved_group_ids_rejected(self):
        """Ids the page uses itself (the hub on top, "Asleep or crashed", "Ungrouped", JS object properties) are
        refused instead of breaking the list."""
        for gid in ("top", "rest", "none", "__proto__", "constructor", "toString", "hasOwnProperty"):
            why = self.lay({"groups": [{"id": gid, "title": "A"}]})
            self.assertIn("is taken", why or "", gid)
        self.assertNotIn("layout", self.st)


class View(unittest.TestCase):
    def feed(self, events):
        r = Reader()
        for i, (t, name, d) in enumerate(events, 1):
            r.feed({"seq": i, "ts": i, "type": t, "entity": name, "data": d, "src": "web"})
        return r.snapshot()

    def test_snapshot_without_layout_is_null(self):
        snap = self.feed([("session.requested", "maps", {"cwd": "/w", "session_id": "s", **CLI})])
        self.assertIsNone(snap["layout"])
        self.assertIsNone(snap["sessions"][0]["own_icon"])

    def test_snapshot_carries_layout_and_icons(self):
        snap = self.feed([
            ("session.requested", "devops", {"cwd": "/k", "session_id": "s", "role": "agent", "about": "x",
                                             "icon": "🛠", **CLI}),
            ("session.iconed", "devops", {"icon": "🚀", **OWNER}),
            ("layout.set", None, {"layout": {"groups": GROUPS, "place": {"devops": "g-a1"}}, "base": 0, **OWNER})])
        s = snap["sessions"][0]
        self.assertEqual((s["icon"], s["own_icon"]), ("🚀", "🚀"))   # an old client sees its own icon in icon
        self.assertEqual(snap["layout"]["groups"], GROUPS)
        self.assertEqual(snap["layout"]["place"], {"devops": "g-a1"})
        self.assertEqual(snap["layout"]["rev"], 3)

    def test_snapshot_shows_own_icon_or_icon(self):
        """Snapshot: icon is the own or the previous one; for an executor it is its starter's icon at birth, not the current one."""
        snap = self.feed([
            ("session.requested", "devops", {"cwd": "/k", "session_id": "s", "role": "agent", "about": "x",
                                             "icon": "🛠", **CLI}),
            ("session.iconed", "devops", {"icon": "🚀", **OWNER}),
            ("session.requested", "devops-x", {"cwd": "/k", "session_id": "s-x", "executor": True,
                                               "raised_by": "devops", **CLI}),
            ("session.iconed", "devops", {"icon": "🧭", **OWNER})])
        x = next(s for s in snap["sessions"] if s["name"] == "devops-x")
        self.assertEqual((x["icon"], x["own_icon"]), ("🚀", "🚀"))

    def test_old_state_without_layout_restores(self):
        """An old state snapshot (from before the layout) is readable, and there is no layout."""
        st = reducer.restore({"seq": 7, "data": {"state": {"sessions": {}, "messages": {}}}})
        self.assertNotIn("layout", st)
        st, _, why = reducer.reduce(st, {"seq": 8, "ts": 8, "type": "layout.set", "entity": None,
                                         "data": {"layout": {"groups": GROUPS}, "base": 0, **OWNER}})
        self.assertIsNone(why)
        self.assertEqual(st["layout"]["rev"], 8)


class Cli(unittest.TestCase):
    """Commands as data, the way put in bin/hub writes them (channel/owner on top), straight through the fold."""
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="groups-")
        self.events = [{"seq": 1, "ts": 1, "type": "session.requested", "entity": "maps",
                        "data": {"cwd": "/w", "session_id": "s-m", **CLI}}]
        self.write()

    def write(self):
        with open(os.path.join(self.state, "journal.jsonl"), "w") as f:
            f.writelines(json.dumps(e, ensure_ascii=False) + "\n" for e in self.events)

    def cli(self, *args, env=None):
        e = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_CLI_WAIT": "0"}
        e.pop("HUB_SESSION", None)
        e.update(env or {})
        r = subprocess.run([sys.executable, HUB, *args], env=e, capture_output=True, text=True)
        out = []
        for path, ev in spool.take(os.path.join(self.state, "spool")):   # the reader removes the file, as hubd does
            os.remove(path)
            out.append(ev)
        for ev in out:   # as hubd does: into the journal under the next number, if the core accepted it
            st = reducer.replay(self.events)
            nxt = {"seq": len(self.events) + 1, "ts": len(self.events) + 1, "type": ev["type"],
                   "entity": ev.get("entity"), "data": ev.get("data") or {}}
            _, _, why = reducer.reduce(st, nxt)
            self.assertIsNone(why, (args, why))
            self.events.append(nxt)
        self.write()
        return r, out

    def test_icon_and_clear(self):
        _, (ev,) = self.cli("session", "icon", "maps", "🗺")
        self.assertEqual((ev["type"], ev["entity"], ev["data"]["icon"], ev["data"]["owner"]),
                         ("session.iconed", "maps", "🗺", True))
        self.assertEqual(reducer.replay(self.events)["sessions"]["maps"]["own_icon"], "🗺")
        self.cli("session", "icon", "maps", "--clear")
        self.assertNotIn("own_icon", reducer.replay(self.events)["sessions"]["maps"])

    def test_layout_move_show_reset(self):
        r, _ = self.cli("layout")
        self.assertIn("was not changed", r.stdout)
        self.cli("layout", "move", "maps", "Clients")
        lay = reducer.replay(self.events)["layout"]
        new = [g for g in lay["groups"] if g["title"] == "Clients"]
        self.assertEqual([g["id"] for g in lay["groups"][:3]], ["agents", "directions", "workers"])
        self.assertEqual(lay["place"], {"maps": new[0]["id"]})
        self.cli("layout", "move", "maps", "specialists")   # by title, case-insensitive
        self.assertEqual(reducer.replay(self.events)["layout"]["place"], {"maps": "agents"})
        r, _ = self.cli("layout")
        self.assertIn("Specialists (agents): maps", r.stdout)
        r, out = self.cli("layout", "move", "nobody", "Clients")
        self.assertEqual((out, r.returncode != 0), ([], True))
        self.cli("layout", "reset")
        self.assertNotIn("groups", reducer.replay(self.events)["layout"])

    def test_the_hub_session_names_an_untouched_group_by_the_title_it_was_shown(self):
        # a session reads hub output in English while the stored titles are the owner's: the shown title names the
        # group too, no new group next to it
        from core import i18n
        self.events.append({"seq": 2, "ts": 2, "type": "session.requested", "entity": "hub-core",
                            "data": {"cwd": "/w", "session_id": "s-h", "kind": "hub", **CLI}})
        self.write()
        cli, me = self.cli, {"HUB_SESSION": "hub-core"}
        r, _ = cli("layout", env=me)
        shown = i18n.tr("en", "web.sidebar.group_agents")
        self.assertIn(shown, r.stdout)
        cli("layout", "move", "maps", shown, env=me)
        lay = reducer.replay(self.events)["layout"]
        self.assertEqual(lay["place"], {"maps": "agents"})
        self.assertEqual([g["id"] for g in lay["groups"]], ["agents", "directions", "workers"])
        self.assertEqual(lay["groups"][0]["title"], i18n.t("web.sidebar.group_agents"))   # kept for the owner
        # the stored layout still answers to the title the session was shown
        self.events.append({"seq": len(self.events) + 1, "ts": 9, "type": "session.requested", "entity": "geo",
                            "data": {"cwd": "/w", "session_id": "s-g", **CLI}})
        self.write()
        cli("layout", "move", "geo", shown.upper(), env=me)
        lay = reducer.replay(self.events)["layout"]
        self.assertEqual(lay["place"], {"maps": "agents", "geo": "agents"})
        self.assertEqual(len(lay["groups"]), 3)

    def test_hub_skill_names_the_commands(self):
        """The hub changes the layout and icons too: its fleet skill names the commands (otherwise it does not know them)."""
        path = os.path.join(os.path.dirname(os.path.dirname(HUB)), "plugins", "hub-coordinator", "skills", "fleet",
                            "SKILL.md")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        for cmd in ("hub session icon", "hub layout", "move <name> <group>", "role --icon"):
            self.assertIn(cmd, text)


if __name__ == "__main__":
    unittest.main()
