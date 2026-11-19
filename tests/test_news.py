"""What changed for sessions after an update (owner decision 2026-10-05: attach to both a session and the hub what the
session has not seen yet, not the whole crowd of updates). Items for sessions are the "### Sessions" subsection of
CHANGES.md; at startup the core learns the new ones and attaches to each session's next message only those it has not
seen — without a separate turn and without editing the system instructions (they break the conversation cache)."""
import tests  # noqa: F401 — do not see the machine's settings
import unittest

from core import news, reducer

OWNER = {"channel": "web", "owner": True}

CHANGES = """# What is new in the hub

## 2026-10-05
- The Project view on the page
- Compact a conversation: `hub session compact`

### For sessions
- A task for a busy specialist from another session goes to the hub: `hub route <mN|dN> queue | executor | drop`
- `hub delegate` is gone — `hub task new <to> --for mN`

## 2026-10-04
- Tasks are a core entity
"""


class Parse(unittest.TestCase):
    def test_session_notes_are_only_the_subsection(self):
        self.assertEqual(news.session_notes(CHANGES), [
            "A task for a busy specialist from another session goes to the hub: `hub route <mN|dN> queue | executor | drop`",
            "`hub delegate` is gone — `hub task new <to> --for mN`"])

    def test_the_owners_list_has_no_session_notes(self):
        self.assertEqual(news.changes(CHANGES), [
            {"date": "2026-10-05", "items": ["The Project view on the page", "Compact a conversation: `hub session compact`"]},
            {"date": "2026-10-04", "items": ["Tasks are a core entity"]}])

    def test_update_notes_skip_them_too(self):
        from modules.update.update import new_notes
        self.assertEqual(new_notes("", CHANGES), ["2026-10-05", "The Project view on the page",
                                                  "Compact a conversation: `hub session compact`", "2026-10-04",
                                                  "Tasks are a core entity"])


WRAPPED = """# What is new in the hub

## 2026-11-08
- A result that starts with the label `HANDOFF-DONE`, with a colon or a dash after it on the same line
  (`HANDOFF-DONE: Done ...`), counts too.
  - a nested line belongs to the item
- A one-line item

Free text after a blank line is not a part of the item.

### For sessions
- A rule for sessions that
  goes over two lines
"""
FLAT = """## 2026-11-08
- A result that starts with the label `HANDOFF-DONE`, with a colon or a dash after it on the same line (`HANDOFF-DONE: Done ...`), counts too. - a nested line belongs to the item
- A one-line item
"""
ITEM = ("A result that starts with the label `HANDOFF-DONE`, with a colon or a dash after it on the same line "
        "(`HANDOFF-DONE: Done ...`), counts too. - a nested line belongs to the item")


class MultiLine(unittest.TestCase):
    """An item of CHANGES.md is its "- " line and the indented lines that follow (owner, 2026-11-08: the update notice cut
    an item at the end of its first line)."""

    def test_the_owners_item_is_whole(self):
        self.assertEqual(news.changes(WRAPPED), [{"date": "2026-11-08", "items": [ITEM, "A one-line item"]}])

    def test_the_session_item_is_whole(self):
        self.assertEqual(news.session_notes(WRAPPED), ["A rule for sessions that goes over two lines"])

    def test_the_update_notice_has_the_whole_item(self):
        from modules.update.update import new_notes
        self.assertEqual(new_notes("", WRAPPED), ["2026-11-08", ITEM, "A one-line item"])

    def test_a_rewrap_is_not_a_new_item(self):
        from modules.update.update import new_notes
        self.assertEqual(new_notes(FLAT, WRAPPED), [])
        self.assertEqual(new_notes(WRAPPED, FLAT), [])

    def test_a_changed_later_line_is_a_new_item(self):
        from modules.update.update import new_notes
        self.assertEqual(new_notes(WRAPPED, WRAPPED.replace("counts too", "counts as well")),
                         [ITEM.replace("counts too", "counts as well")])

    def test_a_session_item_is_not_new_after_the_first_line_was_all_that_was_known(self):
        st = reducer.empty()
        news.load(st, {"seq": 1}, {"notes": ["A rule for sessions that", "A rule that has"]})   # a hub that read first lines
        heads = news.session_heads(WRAPPED)
        self.assertEqual(heads, ["A rule for sessions that"])
        news.load(st, {"seq": 2}, {"notes": news.session_notes(WRAPPED), "heads": heads})
        self.assertEqual(st["news"], [{"text": "A rule that has", "seq": 1},
                                      {"text": "A rule for sessions that goes over two lines", "seq": 1}])

    def test_an_item_that_only_starts_like_a_known_one_is_new(self):
        st = reducer.empty()
        news.load(st, {"seq": 1}, {"notes": ["A rule for sessions that"]})
        news.load(st, {"seq": 2}, {"notes": ["A rule for sessions that is new"]})   # no heads: a different item
        self.assertEqual([n["seq"] for n in st["news"]], [1, 2])

    def test_the_offer_with_a_button_fits_one_message(self):
        got = news.bullets(["word " * 300] * 12, total=2800)
        self.assertTrue(0 < len(got) <= 2800, len(got))
        self.assertEqual(len(news.bullets(["x"] * 20)), 12 * 4 - 1)

    def test_the_release_takes_whole_items_and_a_rewrap_is_not_new(self):
        from tools import release
        wrapped = WRAPPED.split("\nFree text")[0]
        self.assertEqual(release.notes(wrapped, FLAT), "No CHANGES.md entries since the previous release.")
        more = wrapped.replace("- A one-line item", "- A one-line item\n- A new item\n  on two lines")
        self.assertEqual(release.notes(more, FLAT), "## 2026-11-08\n\n- A new item\n  on two lines")

    def test_a_blank_line_ends_an_item_in_the_release_too(self):
        from tools import release
        body = "- A\n\n  an indented paragraph\n- B"
        self.assertEqual(release._items(body), ["- A", "- B"])

    def test_the_layer_release_notes_are_items_not_lines(self):
        from modules.update import layer
        text = "## 2026-11-08\n\n- First item\n  goes on\n  - and a nested one\n- Second\n"
        self.assertEqual(layer.release_items(text), ["2026-11-08", "First item goes on - and a nested one", "Second"])
        self.assertEqual(layer.release_items("- what is new\n- another\n"), ["what is new", "another"])
        long = layer.release_items("- " + "word " * 1000)[0]
        self.assertTrue(long.endswith("word…") and len(long) <= news.ITEM_MAX, long[-20:])

    def test_the_notice_cuts_a_long_item_at_a_word(self):
        got = news.bullets(["short", "word " * 400])
        first, second = got.split("\n")
        self.assertEqual(first, "• short")
        self.assertTrue(second.endswith("word…") and len(second) <= news.NOTICE_ITEM + 2, second[-20:])

    def test_shorten_cuts_at_a_word(self):
        self.assertEqual(news.shorten("one two three", 20), "one two three")
        self.assertEqual(news.shorten("one two three", 9), "one two…")
        self.assertEqual(news.shorten("abcdefghij", 5), "abcd…")


class Deliver(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.actions = reducer.empty(), 0, []
        for n, kind in (("hub", "hub"), ("maps", "work")):
            self.ev("session.requested", n, cwd="/w", session_id=f"s-{n}", kind=kind, **OWNER)
            self.ev("session.started", n)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 1000 + self.seq, "type": type,
                                                      "entity": entity, "data": data})
        self.actions += acts
        return why

    def typed(self, name):
        out = [a["text"] for a in self.actions if a.get("do") == "type" and a["session"] == name]
        self.actions = []
        return out

    def say(self, name, text="how are you?"):
        self.ev("message.received", None, to=name, text=text, **OWNER)
        (t,) = self.typed(name)
        self.ev("turn.started", name, marker=f"m{self.seq}", prompt_head="x")
        self.ev("turn.stopped", name)
        return t

    def test_each_session_gets_what_it_has_not_seen_once(self):
        self.assertIsNone(self.ev("news.loaded", None, notes=["`hub route` for busy ones", "`hub delegate` removed"],
                                  channel="core", owner=False))
        for name in ("maps", "hub"):
            t = self.say(name)
            self.assertIn("`hub route` for busy ones", t)
            self.assertIn("`hub delegate` removed", t)
            self.assertNotIn("`hub route`", self.say(name))                      # a second time — no
        self.ev("news.loaded", None, notes=["`hub route` for busy ones", "`hub delegate` removed"],   # restart
                channel="core", owner=False)
        self.ev("news.loaded", None, notes=["`hub route` for busy ones", "`hub delegate` removed", "`hub x` new"],
                channel="core", owner=False)
        t = self.say("maps")
        self.assertIn("`hub x` new", t)
        self.assertNotIn("`hub route`", t)                                         # only the unseen
        self.assertEqual(self.st["messages"][f"m{self.seq - 2}"]["text"], "how are you?")   # the owner's words — as they were

    def new_items(self, after=0):
        return [n for n in self.st["news"] if n["seq"] > after]

    def test_a_restart_with_more_items_than_the_limit_announces_nothing_again(self):
        total = news.KEEP + 26
        notes = [f"item {n}" for n in range(total, 0, -1)]            # the file lists the newest first
        for _ in range(3):                                            # each start of the core
            self.ev("news.loaded", None, notes=notes, channel="core", owner=False)
        self.assertEqual({n["text"] for n in self.st["news"]}, set(notes))
        t = self.say("maps")
        shown = [f"\u2022 item {n}" for n in range(total - news.SHOW + 1, total + 1)]   # the newest ones, not the oldest
        self.assertEqual([l for l in t.splitlines() if l.startswith("\u2022")], shown)
        for _ in range(3):
            self.ev("news.loaded", None, notes=notes, channel="core", owner=False)
            self.assertNotIn("\u2022", self.say("maps"))

    def test_the_state_the_earlier_code_wrote_costs_one_announcement_not_a_cycle(self):
        notes = [f"item {n}" for n in range(news.KEEP + 26, 0, -1)]
        self.st["news"] = [{"text": t, "seq": 0} for t in notes[26:]]  # what the earlier code kept: the oldest 100
        self.ev("news.loaded", None, notes=notes, channel="core", owner=False)
        self.assertEqual(len(self.st["news"]), news.KEEP + 26)
        self.assertEqual({n["text"] for n in self.new_items()}, set(notes[:26]))
        before = [dict(n) for n in self.st["news"]]
        self.ev("news.loaded", None, notes=notes, channel="core", owner=False)
        self.assertEqual(self.st["news"], before)                     # nothing added the second time

    def test_items_the_file_dropped_are_forgotten_beyond_the_limit_the_listed_are_kept(self):
        self.ev("news.loaded", None, notes=[f"old {n}" for n in range(news.KEEP)], channel="core", owner=False)
        notes = [f"item {n}" for n in range(news.KEEP + 5, 0, -1)]
        self.ev("news.loaded", None, notes=notes, channel="core", owner=False)
        self.assertEqual({n["text"] for n in self.st["news"]}, set(notes))
        self.ev("news.loaded", None, notes=["last"] + notes, channel="core", owner=False)
        self.assertEqual([n["text"] for n in self.st["news"]][-1], "last")
        self.assertEqual(len(self.st["news"]), news.KEEP + 6)

    def test_a_new_session_starts_with_todays_rules(self):
        self.ev("news.loaded", None, notes=["`hub route` for busy ones"], channel="core", owner=False)
        self.ev("session.requested", "atlas", cwd="/a", session_id="s-a", **OWNER)
        self.ev("session.started", "atlas")
        self.assertNotIn("hub route", self.say("atlas"))

    def test_only_the_core_loads_them(self):
        self.assertIsNotNone(self.ev("news.loaded", None, notes=["x"], channel="session", owner=False, sender="maps"))


if __name__ == "__main__":
    unittest.main()
