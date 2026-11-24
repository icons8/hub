"""The size cap of an agent's own CLAUDE.md (owner decisions 2026-11-08): a stricter lessons note with the file's size
and the cap; over the cap (hub.json agents.profile_max_kb, 250 by default, 0 — none) the core itself moves the oldest
unpinned items out into CLAUDE.evicted.md, never the pinned ones (a pin in the item's first line or in its section
heading), and only once the agent's pinning was confirmed (`hub profile pin`); `hub profile evicted|restore` reads and
puts one back. The check runs after an agent's turn, and an eviction waits while the agent or its executors are busy."""
import tests  # noqa: F401 — keep the machine's settings out of the test
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from core import executors, journal, profcap, reducer

PIN = profcap.PIN
OWNER = {"channel": "cli", "owner": True}
HUB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")


def by(name):
    return {"channel": "session", "owner": False, "sender": name}


def profile(old=12, pinned=2, filler=900):
    """An agent's file: a title and intro, a pinned section, a section of items with dates 01.11 .. (oldest first)."""
    lines = ["# Agent maps", "", "Its own file.", "", f"## {PIN} Owner's decisions"]
    lines += [f"- never deploy maps on Friday ({i})" for i in range(pinned)]
    lines += ["", "## Knowledge"]
    lines += [f"- recipe {i} from {i + 1:02d}.10: " + "x" * filler for i in range(old)]
    lines += [f"- {PIN} the gate: tiles only through review", "- fresh recipe without a date: " + "y" * 50, ""]
    return "\n".join(lines)


class Folder(unittest.TestCase):
    NOW = 1762600000.0   # 2025-11-08

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="profcap-")
        self.path = os.path.join(self.dir, profcap.AGENT)

    def write(self, text):
        with open(self.path, "w", encoding="utf-8", newline="") as f:
            f.write(text)

    def text(self, name=profcap.AGENT):
        with open(os.path.join(self.dir, name), encoding="utf-8") as f:
            return f.read()

    def check(self, cap=10, marked=True, busy=False, now=None):
        return profcap.check(self.dir, cap, now or self.NOW, marked=marked, busy=busy)


class Items(Folder):
    def test_pinned_items_sections_the_head_and_headings_are_fixed(self):
        its = profcap.items(profile(old=2))
        fixed = {it["text"].split(":")[0] for it in its if it["fixed"]}
        free = [it["text"] for it in its if not it["fixed"]]
        self.assertIn("Its own file.", fixed)                                     # the head before the first ##
        self.assertIn("- never deploy maps on Friday (0)", fixed)                 # under a pinned heading
        self.assertIn(f"- {PIN} the gate", fixed)                                  # a pinned item
        self.assertEqual(len(free), 3)                                             # two recipes and the fresh one
        self.assertTrue(all(it["section"] == "## Knowledge" for it in its if not it["fixed"]))

    def test_a_pinned_heading_holds_until_a_heading_of_its_level(self):
        text = f"# A\n\n## {PIN} Rules\n- r1\n\n### Sub\n- r2\n\n## Other\n- o1\n"
        fixed = {it["text"] for it in profcap.items(text) if it["fixed"] and not it["heading"]}
        self.assertEqual(fixed, {"- r1", "- r2"})

    def test_cap_from_settings_never_breaks(self):
        for v, want in ((None, 250), ("abc", 250), ("10", 250), (-1, 250), (0.5, 1), (0, 0), (100, 100),
                        (float("inf"), 250), (float("nan"), 250), (True, 250), (10 ** 400, 10 ** 400)):
            with self.subTest(v=v):
                self.assertEqual(profcap.cap_kb({"agents": {"profile_max_kb": v}}), want)
        for cfg in (None, {}, {"agents": None}, {"agents": ["x"]}, {"agents": "x"}, []):
            self.assertEqual(profcap.cap_kb(cfg), 250)

    def test_one_agents_own_cap_overrides_the_shared_one(self):
        cfg = {"agents": {"profile_max_kb": 60, "profile_max_kb_for": {"maps": 300, "docs": 0}}}
        self.assertEqual(profcap.cap_kb(cfg, "maps"), 300)
        self.assertEqual(profcap.cap_kb(cfg, "docs"), 0)                        # 0 — no cap for that agent
        self.assertEqual(profcap.cap_kb(cfg, "notes"), 60)                      # not listed: the shared cap
        self.assertEqual(profcap.cap_kb(cfg), 60)
        for v in (None, "abc", -1, True, float("nan"), [1]):                    # a bad value: the shared cap
            with self.subTest(v=v):
                self.assertEqual(profcap.cap_kb({"agents": {"profile_max_kb": 60, "profile_max_kb_for": {"maps": v}}},
                                                "maps"), 60)
        for box in (None, "x", ["maps"], {"maps": {"kb": 1}}):
            self.assertEqual(profcap.cap_kb({"agents": {"profile_max_kb_for": box}}, "maps"), 250)


class Evict(Folder):
    def test_under_the_cap_nothing_changes(self):
        self.write(profile(old=2))
        got = self.check(cap=100)
        self.assertEqual(got["state"], "under")
        self.assertEqual(self.text(), profile(old=2))
        self.assertFalse(profcap.has_evicted(self.dir))
        self.assertEqual(self.check(cap=100)["state"], "unchanged")   # an untouched file is not read again

    def test_over_the_cap_unmarked_moves_nothing(self):
        self.write(profile())
        got = self.check(cap=5, marked=False)
        self.assertEqual(got["state"], "unmarked")
        self.assertEqual(self.text(), profile())
        self.assertFalse(profcap.has_evicted(self.dir))

    def test_over_the_cap_the_oldest_unpinned_go_whole_down_to_the_target(self):
        self.write(profile())
        before = len(profile().encode())
        got = self.check(cap=5)
        self.assertEqual(got["state"], "evicted")
        self.assertLessEqual(got["bytes"], 5 * 1024 * profcap.TARGET)
        now = self.text()
        for keep in ("never deploy maps on Friday (1)", "the gate: tiles only through review", "Its own file.",
                     "## Knowledge", "fresh recipe without a date"):
            self.assertIn(keep, now)
        gone = [e["text"] for e in profcap.entries(self.text(profcap.EVICTED))]
        self.assertTrue(gone[0].startswith("- recipe 0 from 01.10"))               # the oldest date first
        self.assertEqual([g.split(" from")[0] for g in gone], [f"- recipe {i}" for i in range(len(gone))])
        self.assertTrue(all(g.endswith("x" * 900) for g in gone))                  # whole items
        self.assertNotIn("recipe 0 ", now)
        self.assertEqual(len(self.text(profcap.BEFORE).encode()), before)          # the file before the eviction
        self.assertEqual(got["evicted"], [e["id"] for e in profcap.entries(self.text(profcap.EVICTED))])

    def test_a_file_not_in_utf8_is_not_rewritten(self):
        # release review 2026-11-09: read with errors="replace", the trimmed file and its copy before the eviction both
        # held U+FFFD where the agent's own byte was — no copy of it was left
        self.write(profile())
        with open(self.path, "ab") as f:
            f.write(b"\n- caf\xe9 note from 01.10\n")
        raw = open(self.path, "rb").read()
        got = self.check(cap=5)
        self.assertEqual(got["state"], "not_utf8")
        self.assertEqual(open(self.path, "rb").read(), raw)
        self.assertFalse(os.path.exists(os.path.join(self.dir, profcap.BEFORE)))

    def test_a_code_block_never_closed_moves_nothing_below_it(self):
        # release review 2026-11-09: an unclosed fence made the rest of the file — pinned sections included — one free
        # item, and an eviction moved the pinned rules out of the profile
        self.write("# Agent\n\n## Notes\n\n- note from 01.10 " + "x" * 3000 + "\n\n```bash\necho open\n\n## Rules 📌\n- never push to prod\n\n"
                   + "".join(f"- rule {i} from 0{i % 9 + 1}.10 " + "y" * 900 + "\n\n" for i in range(6)))
        self.check(cap=4)
        self.assertIn("never push to prod", self.text())

    def test_pinned_over_the_cap_moves_nothing(self):
        self.write(profile(old=3, pinned=40, filler=10))
        got = self.check(cap=1)
        self.assertEqual(got["state"], "fixed_over")
        self.assertFalse(profcap.has_evicted(self.dir))

    def test_pinned_between_the_target_and_the_cap_every_free_item_goes(self):
        text = profile(old=4, pinned=0, filler=300)
        fixed = sum(len(it["text"].encode()) + 1 for it in profcap.items(text) if it["fixed"])
        cap = -(-fixed * 100 // 95) // 1024 + 1    # the fixed part is above 90% of a cap a little over it
        pad = f"# Agent maps\n\n{'z' * (int(cap * 1024 * 0.93) - fixed)}\n\n"
        text = pad + text.split("\n", 1)[1]
        self.write(text)
        got = self.check(cap=cap)
        self.assertEqual(got["state"], "evicted")
        self.assertEqual([it for it in profcap.items(self.text()) if not it["fixed"]], [])

    def test_busy_waits_and_then_evicts_without_a_file_change(self):
        self.write(profile())
        got = self.check(cap=5, busy=True)
        self.assertEqual((got["state"], got["pending"]), ("busy", True))
        self.assertEqual(self.text(), profile())
        got = self.check(cap=5)                      # the file untouched: the pending eviction still runs
        self.assertEqual(got["state"], "evicted")

    def test_a_lower_cap_alone_rechecks_the_untouched_file(self):
        self.write(profile())
        self.assertEqual(self.check(cap=100)["state"], "under")
        self.assertEqual(self.check(cap=5)["state"], "evicted")

    def test_the_file_moved_under_the_eviction_is_kept_and_retried(self):
        self.write(profile())
        real = profcap._same

        def edited(path, st):   # an agent's edit between the read and the replace
            with open(path, "a", encoding="utf-8") as f:
                f.write("- a new lesson\n")
            return real(path, st)
        with mock.patch.object(profcap, "_same", edited):
            got = self.check(cap=5)
        self.assertEqual((got["state"], got["pending"]), ("changed", True))
        self.assertTrue(self.text().endswith("- a new lesson\n"))          # the agent's edit is not lost
        self.assertIn("recipe 0 ", self.text())                            # nothing removed from the file yet
        got = self.check(cap=5)
        self.assertEqual(got["state"], "evicted")
        self.assertIn("- a new lesson", self.text())
        ids = [e["id"] for e in profcap.entries(self.text(profcap.EVICTED))]
        self.assertEqual(len(ids), len(set(ids)))                           # the retry made no duplicates

    def test_restore_puts_the_item_back_in_its_section_and_the_round_trip_keeps_one_entry(self):
        self.write(profile())
        self.check(cap=5)
        first = profcap.entries(self.text(profcap.EVICTED))[0]
        ok, why = profcap.restore(self.dir, first["id"], self.NOW + 10)
        self.assertTrue(ok, why)
        now = self.text()
        self.assertIn(first["text"], now)
        self.assertLess(now.index("## Knowledge"), now.index(first["text"]))
        # restored, it is the youngest: a check under a lower cap moves out others, not it
        got = self.check(cap=4, now=self.NOW + 20)
        self.assertEqual(got["state"], "evicted")
        self.assertIn(first["text"], self.text())

    def test_an_item_written_back_by_the_agent_leaves_the_evicted(self):
        self.write(profile())
        self.check(cap=5)
        first = profcap.entries(self.text(profcap.EVICTED))[0]
        self.write(self.text() + first["text"] + "\n")
        self.check(cap=50)
        self.assertNotIn(first["text"], [e["text"] for e in profcap.entries(self.text(profcap.EVICTED))])

    def test_the_same_text_in_two_sections_is_two_ids_and_a_collision_gets_a_suffix(self):
        self.assertNotEqual(profcap.item_id("## A", "- x"), profcap.item_id("## B", "- x"))
        found = [{"id": profcap.item_id("## A", "- x"), "when": "w", "section": "## A", "text": "- y"}]
        self.assertTrue(profcap._put(found, "## A", "- x", "w2").endswith("-2"))
        self.assertEqual(profcap._put(found, "## A", "- x", "w3"), found[1]["id"])   # the same item: one entry
        self.assertEqual((len(found), found[1]["when"]), (2, "w3"))

    def test_search_by_topic_and_the_latest(self):
        self.write(profile())
        self.check(cap=5)
        hits, total = profcap.search(self.dir, "RECIPE 1 ")
        self.assertEqual((total, [h["text"].split(" from")[0] for h in hits]), (1, ["- recipe 1"]))
        shown, total = profcap.search(self.dir, None, 2)
        self.assertEqual((len(shown), total > 2), (2, True))


class Pin(Folder):
    SPEC = "## Knowledge\n- the gate\nrecipe 3 from\n"

    def test_pin_marks_named_items_and_seeds_the_cut(self):
        self.write(profile(old=4, filler=10))
        cut = "## Knowledge\n- a recipe cut by hand\n- recipe 2 from 03.10: " + "x" * 10 + "\n"
        got = profcap.pin(self.dir, self.SPEC + "\n## " + PIN + " Owner's decisions\n*\n", cut, now=self.NOW)
        self.assertTrue(got["ok"], got)
        self.assertEqual((got["pinned"], got["seeded"]), (1, 1))           # the gate was pinned; recipe 2 is in the file
        self.assertIn(f"- {PIN} recipe 3 from", self.text())
        self.assertEqual([e["text"] for e in profcap.entries(self.text(profcap.EVICTED))], ["- a recipe cut by hand"])

    def test_pin_with_a_problem_writes_nothing(self):
        self.write(profile(old=4, filler=10))
        for spec in ("## Knowledge\n- no such item\n", "## Missing\n- recipe 1\n", "## Knowledge\n- recipe\n", "- recipe 1\n"):
            with self.subTest(spec=spec):
                got = profcap.pin(self.dir, spec, "## Knowledge\n- cut\n")
                self.assertFalse(got["ok"])
                self.assertTrue(got["problems"])
                self.assertEqual(self.text(), profile(old=4, filler=10))
                self.assertFalse(profcap.has_evicted(self.dir))


class ReviewCases(Folder):
    def test_a_code_block_is_not_pinned_by_its_fence(self):
        text = "# A\n\n## K\n```bash\n# build it\nmake\n```\n\n## B\n- b1\n"
        self.write(text)
        got = profcap.pin(self.dir, "## K\n```bash\n")
        self.assertFalse(got["ok"])
        self.assertIn("code block", got["problems"][0])
        self.assertEqual(self.text(), text)
        self.assertTrue(profcap.pin(self.dir, "## K\n*\n")["ok"])         # its section is
        self.assertIn(f"## {PIN} K", self.text())
        self.assertTrue(all(it["fixed"] for it in profcap.items(self.text()) if it["section"].endswith("K")))

    def test_the_pinned_size_counts_blank_lines_so_a_file_over_by_them_is_fixed_over(self):
        # the pinned part with its blank lines is 1028 bytes; its items alone with a newline each (the old count) 1011
        text = "# A\n\n## " + PIN + " R\n" + "".join(f"- {'r' * 59}\n\n" for _ in range(16)) + "## K\n- free\n"
        self.write(text)
        got = self.check(cap=1)
        self.assertEqual(got["state"], "fixed_over")                          # the free item alone would not do
        self.assertEqual(self.text(), text)
        self.assertFalse(os.path.exists(os.path.join(self.dir, profcap.BEFORE)))

    def test_restore_goes_to_its_own_section_not_under_a_nested_one_and_ignores_a_pin_in_the_heading(self):
        self.write("# A\n\n## Rules\n- a1\n\n### " + PIN + " Sub\n- s1\n\n## Other\n- o1\n")
        found = [{"id": profcap.item_id("## Rules", "- old a"), "when": "w", "section": "## Rules", "text": "- old a"}]
        with open(os.path.join(self.dir, profcap.EVICTED), "w", encoding="utf-8") as f:
            f.write(profcap.render(found))
        self.write(self.text().replace("## Rules", f"## {PIN} Rules"))       # pinned since: the same section
        ok, why = profcap.restore(self.dir, found[0]["id"], self.NOW)
        self.assertTrue(ok, why)
        now = self.text()
        self.assertLess(now.index("- old a"), now.index("### "))
        self.assertGreater(now.index("- old a"), now.index("- a1"))

    def test_a_restored_item_overwritten_by_a_stale_copy_stays_in_the_evicted(self):
        self.write(profile())
        self.check(cap=5)
        stale = self.text()
        first = profcap.entries(self.text(profcap.EVICTED))[0]
        self.assertTrue(profcap.restore(self.dir, first["id"], self.NOW + 10)[0])
        self.assertIn(first["id"], {e["id"] for e in profcap.entries(self.text(profcap.EVICTED))})
        self.write(stale)                                                     # a script wrote back what it read before
        self.check(cap=50, now=self.NOW + 20)
        self.assertIn(first["id"], {e["id"] for e in profcap.entries(self.text(profcap.EVICTED))})
        self.assertTrue(profcap.restore(self.dir, first["id"], self.NOW + 30)[0])
        self.assertTrue(profcap.restore(self.dir, first["id"], self.NOW + 31)[0])   # twice: one copy in the file
        self.assertEqual(self.text().count(first["text"]), 1)
        self.check(cap=50, now=self.NOW + 40)
        self.assertNotIn(first["id"], {e["id"] for e in profcap.entries(self.text(profcap.EVICTED))})

    def test_a_version_or_a_decimal_is_not_a_date(self):
        for text in ("needs Python 3.12", "the suite takes 1.5 min", "claude 2.1.218",  # portable: text, not a runtime
                     "v1.10.5 is out"):
            self.assertIsNone(profcap._text_date(text, self.NOW), text)
        self.assertIsNotNone(profcap._text_date("seen 05.11 again", self.NOW))
        self.assertIsNotNone(profcap._text_date("on 2025-10-03", self.NOW))
        self.assertIsNotNone(profcap._text_date("at 2025-10-03T12:00", self.NOW))


# --- the fold ------------------------------------------------------------------------------------------------------
class Fold(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.actions = reducer.empty(), 0, []
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.requested", "maps", cwd="/m", session_id="s-maps", role="agent", about="Maps", **OWNER)
        self.ev("session.started", "maps")
        self.ev("session.requested", "dev", cwd="/d", session_id="s-dev", role="agent", about="Dev", **OWNER)
        self.ev("session.started", "dev")
        self.st["system"] = ["dev", "trainer"]

    def ev(self, type, entity=None, ts=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts or 1000 + self.seq, "type": type,
                                                      "entity": entity, "data": data})
        self.actions += acts
        return why

    def to(self, name, word):
        return [m for m in self.st["messages"].values() if m["session"] == name and word in m["text"]]

    def test_an_agents_turn_asks_for_the_check_an_ordinary_sessions_does_not(self):
        self.ev("session.requested", "plain", cwd="/p", session_id="s-p", **OWNER)
        self.ev("session.started", "plain")
        for name in ("maps", "plain"):
            self.ev("turn.started", name)
            self.ev("turn.result", name, text="done")
        checks = [a for a in self.actions if a["do"] == "profile_check"]
        self.assertEqual(checks, [{"do": "profile_check", "session": "maps", "profile": "s-maps"}])

    def test_over_the_cap_unmarked_one_fold_message_until_under_again(self):
        size = {"bytes": 300 * 1024, "fixed": 1000, "cap": 250}
        self.assertIsNone(self.ev("core.profile_size", "maps", **size))
        self.assertIsNone(self.ev("core.profile_size", "maps", **{**size, "bytes": 310 * 1024}))
        (msg,) = self.to("maps", "over the 250 KB cap")
        self.assertIn("300 KB", msg["text"])
        self.assertIn("do not touch prohibitions, gates and the owner's corrections", msg["text"])
        self.assertFalse(msg.get("fyi"))                                   # a job of the turn
        self.ev("core.profile_size", "maps", **{**size, "bytes": 200 * 1024})
        self.ev("core.profile_size", "maps", **size)
        self.assertEqual(len(self.to("maps", "over the 250 KB cap")), 2)

    def test_marked_and_pinned_over_the_cap_tells_the_agent_aside_and_the_hub_developer(self):
        self.ev("core.profile_marked", "maps", pinned=3, seeded=0)
        self.ev("core.profile_size", "maps", bytes=300 * 1024, fixed=260 * 1024, cap=250)
        self.ev("core.profile_size", "maps", bytes=301 * 1024, fixed=261 * 1024, cap=250)
        (note,) = self.to("maps", "the pinned part of your CLAUDE.md")
        self.assertTrue(note.get("fyi"))
        (dev,) = self.to("dev", "maps's CLAUDE.md")
        self.assertIn("260 KB", dev["text"])

    def test_an_eviction_is_a_note_aside(self):
        self.ev("core.profile_marked", "maps", pinned=3, seeded=0)
        self.ev("core.profile_evicted", "maps", n=4, ids=["e" + "a" * 12], before=300 * 1024, after=200 * 1024, cap=250)
        (note,) = self.to("maps", "moved 4 of the oldest unpinned items")
        self.assertTrue(note.get("fyi"))
        self.assertIn("hub profile evicted", note["text"])

    def test_restore_is_the_agents_own_or_the_hubs_word(self):
        rid = "e" + "b" * 12
        self.assertIsNone(self.ev("profile.restore_requested", "maps", id=rid, **by("maps")))
        self.assertIsNone(self.ev("profile.restore_requested", "maps", id=rid, **OWNER))
        self.assertIsNone(self.ev("profile.restore_requested", "maps", id=rid, **by("hub")))
        self.assertIsNotNone(self.ev("profile.restore_requested", "maps", id=rid, **by("dev")))
        self.assertIsNotNone(self.ev("profile.restore_requested", "maps", id="x; rm", **by("maps")))
        self.assertEqual(len([a for a in self.actions if a["do"] == "profile_restore"]), 3)

    def test_pin_is_the_owners_the_hubs_or_the_trainers(self):
        spec, cut = os.path.abspath("/x/p.md"), os.path.abspath("/x/c.md")   # absolute on this system (C:\x\… on Windows)
        self.assertIsNotNone(self.ev("profile.pin_requested", "maps", spec=spec, **by("maps")))
        self.assertIsNone(self.ev("profile.pin_requested", "maps", spec=spec, **by("trainer")))
        self.assertIsNotNone(self.ev("profile.pin_requested", "maps", spec="p.md", **OWNER))
        self.assertIsNone(self.ev("profile.pin_requested", "maps", spec=spec, cut=cut, **OWNER))

    def test_a_bad_size_fact_is_refused_and_a_profile_fact_needs_an_agent(self):
        self.assertIsNotNone(self.ev("core.profile_size", "maps", bytes="1", fixed=0, cap=250))
        self.assertIsNotNone(self.ev("core.profile_size", "maps", bytes=-1, fixed=0, cap=250))
        self.assertIsNotNone(self.ev("core.profile_size", "hub", bytes=1, fixed=0, cap=250))

    def lessons_note(self, size=None):
        if size:
            self.ev("core.profile_size", "maps", **size)
        self.ev("task.run_requested", "maps-1", task="fix tiles", **by("maps"))
        run = self.st["runs"]["maps-1"]
        for type, entity, data in executors.events_for(self.st, "maps-1", run, cwd="/m/.claude/worktrees/x",
                                                       worktree=None):
            self.ev(type, entity, **data)
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == "maps-1"]
        self.ev("session.started", "maps-1")
        self.ev("turn.started", "maps-1", marker=d["message"], ts=2000)
        self.ev("turn.result", "maps-1", text="Done.\n\n**Lessons**\n- tiles — by version", ts=2001)
        self.ev("turn.stopped", "maps-1", ts=2001)
        self.ev("session.close_requested", "maps-1", retire=True, ts=2200)
        (note,) = [m for m in self.st["messages"].values() if m["session"] == "maps" and "handed in its task" in m["text"]]
        return note["text"]

    def test_the_lessons_note_is_strict_and_names_the_size_and_the_cap(self):
        text = self.lessons_note({"bytes": 238 * 1024, "fixed": 1000, "cap": 250})
        for part in ("only what will change the next decision of a future session",
                     "first find the line on that topic — it is there: correct it, do not add one",
                     "one fact — one line, no PR or commit numbers and no dates unless the date is the point",
                     f"with {PIN} at the start of its line", "your CLAUDE.md is 238 KB of the 250 KB cap",
                     "tiles — by version"):
            self.assertIn(part, text)
        self.assertNotIn("what will be useful and is not recorded yet", text)

    def test_the_lessons_note_without_a_known_size_or_cap(self):
        self.assertNotIn("KB", self.lessons_note())
        self.setUp()
        self.assertIn("your CLAUDE.md is 300 KB;", self.lessons_note({"bytes": 300 * 1024, "fixed": 0, "cap": 0}) + ";")

    def test_the_executors_lessons_block_is_strict(self):
        rule = executors.REPORT_RULE.format(raiser="maps")
        self.assertIn("only what will change the next decision of a future session and the profile does not say", rule)
        self.assertIn("no PR or commit numbers", executors.TAKES)


# --- the daemon ----------------------------------------------------------------------------------------------------
class Daemon(unittest.TestCase):
    def setUp(self):
        from core.daemon import Hub
        self.state = tempfile.mkdtemp(prefix="profcap-d-")
        self.h = Hub(self.state, types.SimpleNamespace(profile_used={}))
        self.h.record("session.requested", "maps", {"cwd": "/m", "session_id": "s-maps", "role": "agent", **OWNER})
        self.dir = os.path.join(self.state, "agents", "s-maps")
        os.makedirs(self.dir)
        with open(os.path.join(self.dir, profcap.AGENT), "w", encoding="utf-8") as f:
            f.write(profile())

    def facts(self, kind):
        return [e for e in journal.read(self.h.journal.path) if e["type"] == kind]

    def run_check(self, cap):
        self.h.config = {"agents": {"profile_max_kb": cap}}
        self.h.execute({"do": "profile_check", "session": "maps", "profile": "s-maps"})

    def test_an_agents_own_cap_is_the_one_checked_and_journalled(self):
        self.h.record("core.profile_marked", "maps", {"pinned": 1})
        self.h.config = {"agents": {"profile_max_kb": 5, "profile_max_kb_for": {"maps": 0}}}
        self.h.execute({"do": "profile_check", "session": "maps", "profile": "s-maps"})
        self.assertEqual(self.facts("core.profile_evicted"), [])          # no cap for this agent: nothing goes
        (size,) = self.facts("core.profile_size")
        self.assertEqual(size["data"]["cap"], 0)
        self.h.config["agents"]["profile_max_kb_for"] = {"maps": 100}
        self.h.execute({"do": "profile_check", "session": "maps", "profile": "s-maps"})
        self.assertEqual(self.facts("core.profile_size")[-1]["data"]["cap"], 100)
        self.assertEqual(self.facts("core.profile_evicted"), [])          # under its own cap, over the shared one
        self.h.config["agents"]["profile_max_kb_for"] = {"other": 0}
        self.h.execute({"do": "profile_check", "session": "maps", "profile": "s-maps"})
        self.assertEqual(len(self.facts("core.profile_evicted")), 1)      # not listed: the shared 5 KB holds

    def test_bad_settings_never_stop_the_tick(self):
        for cap in ("abc", "10", -1, 0.5, None, float("inf"), [1], {"x": 1}):
            with self.subTest(cap=cap):
                self.run_check(cap)
                self.h.profile_pending()
        self.assertEqual(self.facts("core.rejected"), [])

    def test_a_size_fact_only_when_it_moves_and_an_eviction_only_when_marked(self):
        self.run_check(100)
        self.run_check(100)
        self.assertEqual(len(self.facts("core.profile_size")), 1)
        self.run_check(5)
        self.assertEqual(self.facts("core.profile_evicted"), [])          # not marked: nothing goes
        self.assertEqual(self.h.state["sessions"]["maps"]["profile_size"]["cap"], 5)
        self.h.record("core.profile_marked", "maps", {"pinned": 1})
        self.run_check(5)
        (ev,) = self.facts("core.profile_evicted")
        self.assertGreater(ev["data"]["n"], 0)
        self.assertLessEqual(self.h.state["sessions"]["maps"]["profile_size"]["bytes"], 5 * 1024)

    def test_an_eviction_waits_for_the_agents_executors_and_runs_on_the_tick(self):
        self.h.record("core.profile_marked", "maps", {"pinned": 1})
        self.h.state["sessions"]["maps-1"] = {"name": "maps-1", "state": "busy", "profile": "s-maps", "role": "executor"}
        self.run_check(5)
        self.assertEqual(self.facts("core.profile_evicted"), [])
        self.assertTrue(self.h.state["sessions"]["maps"]["profile_size"]["pending"])
        self.h.profile_pending()
        self.assertEqual(self.facts("core.profile_evicted"), [])          # still busy
        self.h.state["sessions"]["maps-1"]["state"] = "asleep"
        self.h.profile_pending()
        self.assertEqual(len(self.facts("core.profile_evicted")), 1)
        self.assertFalse(self.h.state["sessions"]["maps"]["profile_size"]["pending"])

    def test_a_dialog_is_busy_too_and_a_failing_retry_waits(self):
        self.h.record("core.profile_marked", "maps", {"pinned": 1})
        self.h.state["sessions"]["maps-1"] = {"name": "maps-1", "state": "dialog", "profile": "s-maps", "role": "executor"}
        self.run_check(5)
        self.assertEqual(self.facts("core.profile_evicted"), [])
        self.h.state["sessions"]["maps-1"]["state"] = "asleep"
        calls = []

        def broken(*a, **k):
            calls.append(1)
            raise OSError("read-only")
        with mock.patch.object(profcap, "check", broken):
            self.h.profile_pending()
            self.h.profile_pending()
        self.assertEqual(len(calls), 1)                                       # not every tick
        self.h.profile_failed()["maps"] -= 10 ** 4
        self.h.profile_pending()
        self.assertEqual(len(self.facts("core.profile_evicted")), 1)

    def test_pin_reads_only_a_markdown_list_and_journals_no_line_of_it(self):
        secret = os.path.join(self.state, "secrets.env")
        with open(secret, "w") as f:
            f.write("TOKEN=abc123\n")
        self.h.execute({"do": "profile_pin", "session": "maps", "profile": "s-maps", "spec": secret, "cut": None})
        listed = os.path.join(self.state, "list.md")
        with open(listed, "w") as f:
            f.write("TOKEN=abc123\n")
        self.h.execute({"do": "profile_pin", "session": "maps", "profile": "s-maps", "spec": listed, "cut": None})
        failed = self.facts("core.profile_pin_failed")
        self.assertEqual(len(failed), 2)
        self.assertNotIn("abc123", json.dumps([e["data"] for e in failed]))
        self.assertFalse(self.h.state["sessions"]["maps"].get("profile_marked"))

    def test_a_restore_through_the_core_survives_a_stale_copy_written_back(self):
        self.h.record("core.profile_marked", "maps", {"pinned": 1})
        self.run_check(5)
        path = os.path.join(self.dir, profcap.AGENT)
        with open(path, encoding="utf-8") as f:
            stale = f.read()
        eid = self.facts("core.profile_evicted")[0]["data"]["ids"][0]
        self.h.execute({"do": "profile_restore", "session": "maps", "profile": "s-maps", "id": eid})
        with open(path, "w", encoding="utf-8") as f:   # a script of the agent wrote back what it read before the restore
            f.write(stale)
        self.run_check(50)
        with open(os.path.join(self.dir, profcap.EVICTED), encoding="utf-8") as f:
            self.assertIn(eid, {e["id"] for e in profcap.entries(f.read())})

    @unittest.skipUnless(hasattr(os, "mkfifo"), "a FIFO is POSIX")
    def test_pin_refuses_a_fifo_and_a_link_to_a_device(self):
        fifo = os.path.join(self.state, "list.md")
        os.mkfifo(fifo)
        self.assertIsNone(self.h.pin_file(fifo))                              # does not block the tick
        link = os.path.join(self.state, "zero.md")
        os.symlink(os.devnull, link)   # portable: skipped where there is no mkfifo (Windows)
        self.assertIsNone(self.h.pin_file(link))
        big = os.path.join(self.state, "big.md")
        with open(big, "wb") as f:
            f.write(b"x" * (2 * 1024 * 1024 + 1))
        self.assertIsNone(self.h.pin_file(big))

    def test_restore_and_pin_through_the_core(self):
        spec = os.path.join(self.state, "pinned.md")
        with open(spec, "w", encoding="utf-8") as f:
            f.write("## Knowledge\n- recipe 11\n")
        self.h.execute({"do": "profile_pin", "session": "maps", "profile": "s-maps", "spec": spec, "cut": None})
        self.assertTrue(self.h.state["sessions"]["maps"].get("profile_marked"))
        self.run_check(5)
        eid = self.facts("core.profile_evicted")[0]["data"]["ids"][0]
        self.h.execute({"do": "profile_restore", "session": "maps", "profile": "s-maps", "id": eid})
        (got,) = self.facts("core.profile_restored")
        self.assertTrue(got["data"]["ok"])


# --- what the agent sees -------------------------------------------------------------------------------------------
class Prompt(unittest.TestCase):
    def setUp(self):
        from hosts.streamjson import StreamHost
        self.state = tempfile.mkdtemp(prefix="profcap-p-")
        cfg = os.path.join(self.state, "hub.json")
        with open(cfg, "w") as f:
            json.dump({}, f)
        patch = mock.patch.dict(os.environ, {"HUB_CONFIG": cfg})
        patch.start()
        self.addCleanup(patch.stop)
        self.host = StreamHost(self.state, claude=(sys.executable,))
        self.host.fresh_prompt = False
        self.dir = os.path.join(self.state, "agents", "p")
        os.makedirs(self.dir)
        self.file = os.path.join(self.dir, profcap.AGENT)
        with open(self.file, "w", encoding="utf-8") as f:
            f.write(profile(old=2))

    def prompt(self, **kw):
        cmd = self.host.command("w", "s-1", agent_file=self.file, **kw)
        with open(cmd[cmd.index("--append-system-prompt-file") + 1], encoding="utf-8") as f:
            return f.read()

    def test_the_evicted_line_only_once_there_are_evicted_items(self):
        self.assertNotIn("hub profile evicted", self.prompt())
        self.assertNotIn("hub profile evicted", self.prompt(executor_of="maps"))
        with open(os.path.join(self.dir, profcap.EVICTED), "w", encoding="utf-8") as f:
            f.write(profcap.render([{"id": "e" + "c" * 12, "when": "w", "section": "## K", "text": "- old"}]))
        self.assertIn("hub profile restore <id>", self.prompt())
        executor = self.prompt(executor_of="maps")
        self.assertIn("moved out of maps's CLAUDE.md", executor)
        self.assertNotIn("hub profile restore", executor)

    def test_the_agent_is_told_to_pin_and_to_correct_not_add(self):
        text = self.prompt()
        self.assertIn(f"Pin with {PIN} at the start of its line an owner's correction", text)
        self.assertIn("first find the line on that topic", text)


class Cli(unittest.TestCase):
    def test_evicted_shows_the_agents_items_by_topic(self):
        state = tempfile.mkdtemp(prefix="profcap-c-")
        j = journal.Journal(os.path.join(state, "journal.jsonl"))
        j.append("session.requested", "maps", {"cwd": "/m", "session_id": "s-maps", "role": "agent", **OWNER})
        folder = os.path.join(state, "agents", "s-maps")
        os.makedirs(folder)
        with open(os.path.join(folder, profcap.EVICTED), "w", encoding="utf-8") as f:
            f.write(profcap.render([{"id": "e" + "d" * 12, "when": "2025-11-01 10:00 UTC", "section": "## Knowledge",
                                     "text": "- tiles: rebuild by version"},
                                    {"id": "e" + "f" * 12, "when": "2025-11-01 10:00 UTC", "section": "## Knowledge",
                                     "text": "- geocoder: cache a day"}]))
        env = {k: v for k, v in os.environ.items() if k != "HUB_SESSION"}
        env.update(HUB_STATE_DIR=state, HUB_LANG="en")
        run = lambda *a: subprocess.run([sys.executable, HUB, "profile", *a], env=env, capture_output=True,  # noqa: E731
                                        text=True, encoding="utf-8")
        r = run("evicted", "maps", "--grep", "TILES")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("- tiles: rebuild by version", r.stdout)
        self.assertNotIn("geocoder", r.stdout)
        r = run("evicted", "maps", "-n", "1")
        self.assertIn("geocoder", r.stdout)
        self.assertIn("1 earlier", r.stdout)
        r = run("restore", "e" + "0" * 12, "maps")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("No evicted item", r.stdout + r.stderr)
        r = run("evicted", "nobody")
        self.assertNotEqual(r.returncode, 0)


class Settings(unittest.TestCase):
    def test_the_cap_is_a_whole_number_in_the_settings(self):
        from modules.web import settings
        self.assertEqual(settings.validate("agents.profile_max_kb", 200, "en"), (200, None))
        self.assertEqual(settings.validate("agents.profile_max_kb", 0, "en"), (0, None))
        for bad in (-1, 0.5, "250", None, 10 ** 6):
            self.assertIsNone(settings.validate("agents.profile_max_kb", bad, "en")[0])
        self.assertEqual(settings.KEYS["agents.profile_max_kb"][3], profcap.CAP_KB)


if __name__ == "__main__":
    unittest.main()
