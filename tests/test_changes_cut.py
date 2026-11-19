"""The cut of the CHANGES.md history (tools/changes_cut.py): after a release tag the published file becomes English and the
earlier history leaves the tree. The readers of the file are the previous updater of a colleague's hub (new_notes, its code
is that of the tag), the core (items for sessions), the "Updates" page and tools/release.py: the cut file must give them
nothing old. The real history is not read here: the files are synthetic, in the shape of the tag's file (the earlier
sessions heading, the earlier language of the items)."""
import tests  # noqa: F401 — do not see the machine's settings
import os
import subprocess
import sys
import tempfile
import unittest

from core import forms, news
from modules.update.update import new_notes
from tools import changes_cut, release

OLD_HEADING = forms.SESSION_SECTIONS[0]
RU = "".join(map(chr, (1087, 1088, 1080, 1074, 1077, 1090)))   # a word in Russian

TAG = f"""# Header of the tag

## 2026-11-05
- {RU} one
- {RU} two
  with a continuation line

{OLD_HEADING}
- {RU} rule for sessions

## 2026-11-04
- {RU} three
"""
# main after the tag: the old entries stay, a few are added (English, as the rule is from the tag on)
MAIN = f"""# Header of the tag

## 2026-11-06
- New item one
  with a continuation line

### For sessions
- New rule for sessions

## 2026-11-05
- {RU} one
- Added to a day the tag has
- {RU} two
  with a continuation line

{OLD_HEADING}
- {RU} rule for sessions

## 2026-11-04
- {RU} three
"""


class Kept(unittest.TestCase):
    def test_kept_are_the_items_the_tag_did_not_have(self):
        blocks = changes_cut.kept(MAIN, TAG)
        self.assertEqual([(b["date"], len(b["owner"]), len(b["sessions"])) for b in blocks],
                         [("2026-11-06", 1, 1), ("2026-11-05", 1, 0)])
        self.assertEqual(blocks[0]["owner"][0], ["- New item one", "  with a continuation line"])

    def test_nothing_new_keeps_nothing(self):
        self.assertEqual(changes_cut.kept(TAG, TAG), [])

    def test_an_unknown_subheading_is_refused(self):
        with self.assertRaises(changes_cut.Refused):
            changes_cut.parse("## 2026-11-05\n### Other\n- x\n")

    def test_the_parser_agrees_with_the_readers_on_the_real_files(self):
        # the cut runs on the real file at the tag: its items must be the ones news sees (no subheading it drops, no item it
        # skips, no line it loses). The published file is English after the cut, the earlier history is kept outside the
        # published tree (docs/): it is read when it is there, which is where the cut is prepared
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        paths = [p for p in (os.path.join(root, "CHANGES.md"), os.path.join(root, changes_cut.HISTORY)) if os.path.exists(p)]
        for path in paths:
            with open(path, encoding="utf-8") as f:
                text = f.read()
            blocks = changes_cut.parse(text)   # strict: a line it does not know is refused
            self.assertTrue(blocks, path)   # not a vacuous agreement of two empty lists
            self.assertEqual([" ".join(x.strip() for x in i)[2:].strip() for b in blocks for i in b["sessions"]], news.session_notes(text), path)
            owner = [i for c in news.changes(text, limit=10 ** 6) for i in c["items"]]
            self.assertEqual([" ".join(x.strip() for x in i)[2:].strip() for b in blocks for i in b["owner"]], owner, path)

    def test_an_item_with_an_edited_continuation_is_kept(self):
        # the old updater compares the first line only; the cut does not lose the edit
        main = MAIN.replace("  with a continuation line\n\n" + OLD_HEADING, "  with a continuation line, edited\n\n" + OLD_HEADING)
        self.assertEqual(main.count("edited"), 1)
        self.assertEqual([i[0] for b in changes_cut.kept(main, TAG) for i in b["owner"] if b["date"] == "2026-11-05"],
                         ["- Added to a day the tag has", f"- {RU} two"])

    def test_a_line_the_parser_would_drop_is_refused(self):
        with self.assertRaises(changes_cut.Refused):
            changes_cut.parse("## 2026-11-05\n- item\nA paragraph that belongs to no item\n")
        changes_cut.parse("# Header\n\nA paragraph of the header is fine\n\n## 2026-11-05\n- item\n")
        changes_cut.parse("## Not a date\nbad\n", strict=False)   # the tag's file is only compared with


class Cut(unittest.TestCase):
    def cut(self, main=MAIN, date="2026-11-06"):
        # the items of MAIN that are Russian would be refused: this file is the English kept part on its own
        return changes_cut.cut(main, TAG, date)[0]

    def test_the_cut_file_is_english_with_the_cut_entry_first(self):
        new = self.cut()
        self.assertFalse(changes_cut.CYRILLIC.search(new))
        self.assertEqual(next(ln for ln in new.splitlines() if ln.startswith("## ")), "## 2026-11-06")
        first = news.changes(new)[0]
        self.assertEqual(first["items"][0], changes_cut.CUT_ITEM)   # the page and publish.sh want an entry under the first date
        self.assertEqual(news.changes(new)[1], {"date": "2026-11-05", "items": ["Added to a day the tag has"]})

    def test_the_previous_updater_shows_only_new_lines(self):
        # what a hub shows after the update: new_notes over its file and the new one (a hub on an earlier tag shows the
        # first line of "New item one" only: the items are whole since 2026-11-08)
        notes = new_notes(TAG, self.cut())
        self.assertEqual(notes, ["2026-11-06", changes_cut.CUT_ITEM, "New item one with a continuation line",
                                 "Added to a day the tag has"])
        self.assertFalse([n for n in notes if RU in n])

    def test_a_hub_of_the_main_channel_sees_the_same(self):
        # its file is MAIN (it already has the kept items): only the cut entry is new to it
        self.assertEqual(new_notes(MAIN, self.cut()), [changes_cut.CUT_ITEM])

    def test_sessions_get_only_the_new_rule(self):
        new = self.cut()
        self.assertEqual(news.session_notes(new), ["New rule for sessions"])
        st = {"news": []}
        news.load(st, {"seq": 1}, {"notes": news.session_notes(TAG)})   # the hub on the tag remembers the file it had
        news.load(st, {"seq": 2}, {"notes": news.session_notes(new)})
        text = news.unseen(st, {"news_seen": 1})
        self.assertIn("New rule for sessions", text)
        self.assertNotIn(RU, text)
        self.assertEqual(text.count("•"), 1)

    def test_the_sessions_heading_is_the_english_one(self):
        # a file of every live tag knows it (forms.SESSION_SECTIONS); the old one is only for the files they already have
        self.assertIn("### For sessions", self.cut().splitlines())
        self.assertNotIn(OLD_HEADING, self.cut())

    def test_the_next_release_takes_the_entries_since_the_tag(self):
        self.assertEqual(release.notes(self.cut(), TAG),
                         f"## 2026-11-06\n\n- {changes_cut.CUT_ITEM}\n- New item one\n  with a continuation line\n\n"
                         "## 2026-11-05\n\n- Added to a day the tag has")

    def test_the_cut_on_the_day_of_the_items_does_not_double_the_heading(self):
        # two headings of one date cannot be told apart by the readers: one block per date
        new = changes_cut.render([{"date": "2026-11-05", "owner": [["- Item"]], "sessions": []}], "2026-11-05")
        self.assertEqual(sum(ln == "## 2026-11-05" for ln in new.splitlines()), 1)
        self.assertEqual(news.changes(new)[0]["items"], [changes_cut.CUT_ITEM, "Item"])

    def test_russian_left_in_the_kept_part_is_refused(self):
        main = MAIN.replace("- New item one", f"- {RU} new")
        with self.assertRaises(changes_cut.Refused) as e:
            changes_cut.cut(main, TAG, "2026-11-06")
        self.assertIn("1 kept lines", str(e.exception))

    def test_an_entry_dated_after_the_cut_day_is_refused(self):
        with self.assertRaises(changes_cut.Refused):
            changes_cut.cut(MAIN, TAG, "2026-11-05")

    def test_a_cut_file_is_not_cut_again(self):
        with self.assertRaises(changes_cut.Refused):
            changes_cut.cut(self.cut(), TAG, "2026-11-07")


class Command(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="changes-cut-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = self.tmp.name

        def git(*a):
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *a], cwd=self.repo,
                           check=True, capture_output=True)
        git("init", "-q", "-b", "main")
        self.write(TAG)
        git("add", "CHANGES.md")
        git("commit", "-q", "-m", "tag")
        git("tag", "v1.0.0")
        self.write(MAIN)

    def write(self, text):
        with open(os.path.join(self.repo, "CHANGES.md"), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def read(self, name):
        with open(os.path.join(self.repo, name), encoding="utf-8") as f:
            return f.read()

    def run_cut(self, *args):
        return changes_cut.main([*args, "--repo", self.repo])

    def test_apply_moves_the_history_and_writes_the_english_file(self):
        self.assertEqual(self.run_cut("apply", "v1.0.0", "--date", "2026-11-06"), 0)
        self.assertEqual(self.read(changes_cut.HISTORY), MAIN)   # the whole earlier file, Russian included
        self.assertFalse(changes_cut.CYRILLIC.search(self.read("CHANGES.md")))
        self.assertEqual(new_notes(TAG, self.read("CHANGES.md"))[0], "2026-11-06")

    def test_apply_does_not_overwrite_a_history_file(self):
        os.makedirs(os.path.join(self.repo, "docs"))
        with open(os.path.join(self.repo, changes_cut.HISTORY), "w") as f:
            f.write("mine")
        self.assertEqual(self.run_cut("apply", "v1.0.0", "--date", "2026-11-06"), 2)
        self.assertEqual(self.read(changes_cut.HISTORY), "mine")
        self.assertEqual(self.read("CHANGES.md"), MAIN)

    def test_a_date_that_is_not_a_day_is_refused(self):
        for bad in ("06.11.2026", "2026-13-45", "2026-02-31", "2026-11-06\n", "20261106"):
            self.assertEqual(self.run_cut("apply", "v1.0.0", "--date", bad), 2, bad)
        self.assertEqual(self.read("CHANGES.md"), MAIN)

    def test_no_temp_file_is_left_behind(self):
        self.assertEqual(self.run_cut("apply", "v1.0.0", "--date", "2026-11-06"), 0)
        self.assertEqual(sorted(n for n in os.listdir(self.repo) if n.endswith(".new")), [])
        self.assertEqual(os.listdir(os.path.join(self.repo, "docs")), [os.path.basename(changes_cut.HISTORY)])

    def test_a_tag_file_that_is_not_utf8_is_refused(self):
        with open(os.path.join(self.repo, "CHANGES.md"), "wb") as f:
            f.write(b"## 2026-11-05\n- bad \xff byte\n")
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-am", "x"],
                       cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "tag", "v1.0.1"], cwd=self.repo, check=True, capture_output=True)
        self.write(MAIN)
        self.assertEqual(self.run_cut("status", "v1.0.1"), 2)

    def test_a_stray_item_before_the_first_date_is_refused(self):
        with self.assertRaises(changes_cut.Refused):
            changes_cut.parse("# Header\n- an item with no date\n## 2026-11-05\n- x\n")

    def test_a_russian_word_spelled_with_escapes_counts_as_russian(self):
        word = "".join("\\u%04x" % ord(c) for c in RU)   # as it would sit in a code line
        self.assertTrue(changes_cut.cyrillic([{"date": "d", "owner": [["- " + word]], "sessions": []}]))

    def test_a_rerun_after_an_interrupted_cut_needs_no_force(self):
        self.assertEqual(self.run_cut("apply", "v1.0.0", "--date", "2026-11-06"), 0)
        self.write(MAIN)   # CHANGES.md was not replaced, the history file is already there with the same text
        self.assertEqual(self.run_cut("apply", "v1.0.0", "--date", "2026-11-06"), 0)

    def test_the_tag_file_is_read_as_utf8_whatever_the_locale(self):
        env = {**os.environ, "LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"}
        tool = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "changes_cut.py")
        r = subprocess.run([sys.executable, tool, "status", "v1.0.0", "--repo", self.repo], env=env, capture_output=True)
        self.assertEqual((r.returncode, r.stdout.startswith(b"kept: 3 items")), (0, True), r.stderr)

    def test_a_refusal_changes_nothing(self):
        self.write(MAIN.replace("- New item one", f"- {RU} new"))
        before = self.read("CHANGES.md")
        self.assertEqual(self.run_cut("apply", "v1.0.0", "--date", "2026-11-06"), 2)
        self.assertEqual(self.read("CHANGES.md"), before)
        self.assertFalse(os.path.exists(os.path.join(self.repo, changes_cut.HISTORY)))

    def test_status_says_what_is_left_to_translate(self):
        self.assertEqual(self.run_cut("status", "v1.0.0"), 0)
        self.write(MAIN.replace("- New item one", f"- {RU} new"))
        self.assertEqual(self.run_cut("status", "v1.0.0"), 1)


if __name__ == "__main__":
    unittest.main()
