"""The ratchet of the published tree's language (tools/public_language.py): the number of lines with Cyrillic letters
per file can only go down while the tree is translated into English. A new Russian line (or a new file with one) is red
with the file's name; a file that got cleaner is red until `python3 tools/public_language.py --prune` keeps the gain."""
import tests  # noqa: F401 — hide the machine's settings
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import platforms
from tests import winsafe
from tools import public_language as pl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Cyrillic letters are written by code, so that this file stays clean for its own count
ZH = chr(0x0416)    # a Russian letter
GE = chr(0x0490)    # a Ukrainian letter
RU = "".join(chr(c) for c in range(0x043f, 0x0445))   # a word in Russian


def tree(test, files):
    """A git repository in a temp folder with the given files {path: bytes or str}, all added (not committed)."""
    root = tempfile.mkdtemp()
    test.addCleanup(platforms.current.rmtree, root)
    for path, data in files.items():
        full = os.path.join(root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode("utf-8"))
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    return root


class Count(unittest.TestCase):
    def test_a_line_counts_once_whatever_the_number_of_letters(self):
        root = tree(self, {"a.py": f"x = 1\n# {RU} {RU}\n{GE}\n\nprint({RU})\r\nend\n"})
        self.assertEqual(pl.count(root), {"a.py": 3})

    def test_ukrainian_and_russian_letters_both_count(self):
        root = tree(self, {"a.txt": GE + "\n", "b.txt": ZH + "\n", "c.txt": "latin only\n"})
        self.assertEqual(pl.count(root), {"a.txt": 1, "b.txt": 1})

    def test_the_other_cyrillic_blocks_count_too(self):
        # Cyrillic Supplement, Extended-C, the two Phonetic Extensions letters, Extended-A, Extended-B: both edges of each
        edges = (0x0400, 0x04ff, 0x0500, 0x052f, 0x1c80, 0x1c8f, 0x1d2b, 0x1d78, 0x2de0, 0x2dff, 0xa640, 0xa69f)
        root = tree(self, {f"{i}.txt": chr(c) + "\n" for i, c in enumerate(edges)})
        self.assertEqual(pl.count(root), {f"{i}.txt": 1 for i in range(len(edges))})
        root = tree(self, {"armenian.txt": chr(0x0530) + chr(0x1c90) + chr(0x2de0 - 1) + "\n"})   # next to the blocks, not in
        self.assertEqual(pl.count(root), {})

    def test_binary_files_are_not_text(self):
        root = tree(self, {"b.bin": b"\0" + RU.encode("utf-8"), "real.txt": RU})
        self.assertEqual(pl.count(root), {"real.txt": 1})

    def test_a_link_is_not_text(self):
        root = tree(self, {"real.txt": RU})
        winsafe.symlink(self, "real.txt", os.path.join(root, "link.txt"))
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        self.assertEqual(pl.count(root), {"real.txt": 1})

    def test_what_the_snapshot_does_not_carry_is_not_counted(self):
        # docs/ and the Russian owner's eval cases are removed by publish.sh, the paths its filter skips are not checked, untracked
        # files are not archived
        root = tree(self, {"docs/brief.md": RU, "modules/web/vendor/lib.js": RU, "core/a.py": RU,
                           "evals/locale/ru/prompts/p.json": RU})
        with open(os.path.join(root, "untracked.py"), "w", encoding="utf-8") as f:
            f.write(RU)
        self.assertEqual(pl.count(root), {"core/a.py": 1})

    def test_a_deleted_tracked_file_is_skipped(self):
        root = tree(self, {"a.py": RU, "b.py": RU})
        os.remove(os.path.join(root, "b.py"))
        self.assertEqual(pl.count(root), {"a.py": 1})

    def test_an_exempt_file_is_not_counted(self):
        root = tree(self, {"i18n/ru.json": RU, "i18n/uk.json": RU, "i18n/de.json": RU})
        self.assertEqual(pl.count(root), {"i18n/de.json": 1})   # a catalog of another language is not exempt

    def test_a_folder_entry_exempts_what_is_under_it(self):
        with mock.patch.object(pl, "EXEMPT", {"deep/": "why"}):
            root = tree(self, {"deep/a/b.txt": RU, "deeper.txt": RU})
            self.assertEqual(pl.count(root), {"deeper.txt": 1})


class Hidden(unittest.TestCase):
    """A Russian word written with escapes or chr() is a Russian word: it counts. A single letter and a range in a class do not."""
    BS = "\\"

    def esc(self, text, form="u"):
        """The text as escapes: the four-digit form, the eight-digit form, the braced form, or the names of the letters."""
        import unicodedata
        if form == "N":
            return "".join(self.BS + "N{" + unicodedata.name(c) + "}" for c in text)
        return "".join(self.BS + {"u": "u%04x", "U": "U%08x", "{": "u{%x}"}[form] % ord(c) for c in text)

    def lines(self, name, text):
        return pl.count(tree(self, {name: text}))

    def test_escaped_words_count_in_every_form_and_every_kind_of_file(self):
        for form in ("u", "U", "{", "N"):
            word = self.esc(RU, form)
            for name, line in (("a.py", f'X = "{word}"\n'), ("a.json", f'{{"k": "{word}"}}\n'), ("a.js", f'const x = "{word}";\n'),
                               ("a.md", f"{word}\n")):
                with self.subTest(form=form, file=name):
                    self.assertEqual(self.lines(name, line), {name: 1})

    def test_a_word_cut_into_literals_or_chr_calls_counts(self):
        a, b = self.esc(RU[:3]), self.esc(RU[3:])
        chrs = " + ".join(f"chr({ord(c)})" for c in RU)
        for line in (f'X = "{a}" "{b}"', f'X = "{a}" + "{b}"', f"X = {chrs}", f"X = chr(0x{ord(RU[0]):x}) + chr(0x{ord(RU[1]):x})",
                     f'X = "{a}" + chr({ord(RU[3])}) + chr({ord(RU[4])})'):
            with self.subTest(line=line):
                self.assertEqual(self.lines("a.py", line + "\n"), {"a.py": 1})

    def test_a_long_chain_of_pieces_is_joined(self):
        word = "".join(chr(c) for c in range(0x0430, 0x0438))   # eight letters, each its own piece
        for glue in (" + ", " "):
            with self.subTest(glue=glue):
                line = "X = " + glue.join('"' + self.esc(c) + '"' for c in word)
                self.assertEqual(self.lines("a.py", line + "\n"), {"a.py": 1})
        self.assertEqual(self.lines("a.py", "X = " + " + ".join(f"chr({ord(c)})" for c in word) + "\n"), {"a.py": 1})

    def test_a_doubled_backslash_is_still_an_escape_of_the_regex_engine(self):
        # in a source or in JSON the text of two escapes is turned into the letters by `re`
        line = '"' + self.esc(RU[:2]).replace(self.BS, self.BS * 2) + '"'
        self.assertEqual(self.lines("a.json", line + "\n"), {"a.json": 1})

    def test_one_letter_is_not_a_word(self):
        for line in (f'X = "{self.esc(ZH)}gents-hub"', f"X = chr({ord(ZH)})", f'X = "a{self.esc(ZH)}b{self.esc(GE)}c"'):
            with self.subTest(line=line):
                self.assertEqual(self.lines("a.py", line + "\n"), {})

    def test_a_range_in_a_class_is_not_a_word(self):
        a, z = self.esc(chr(0x0410)), self.esc(chr(0x044f))
        yo = self.esc(chr(0x0401)) + self.esc(chr(0x0451))
        for line in (f'R = re.compile("[{a}-{z}{yo}]")', f'R = re.compile("[{self.esc(chr(0x0400))}-{self.esc(chr(0x04ff))}]{{3}}")',
                     f'R = re.compile("[{a}-{z}]+")', f'"[^{yo}{self.esc(chr(0x0430))}-{z}]"'):
            with self.subTest(line=line):
                self.assertEqual(self.lines("a.py", line + "\n"), {})

    def test_a_class_does_not_cover_a_word_outside_it(self):
        a, z = self.esc(chr(0x0410)), self.esc(chr(0x044f))
        self.assertEqual(self.lines("a.py", f'R = re.compile("[{a}-{z}]|{self.esc(RU)}")\n'), {"a.py": 1})

    def test_escapes_of_other_scripts_and_controls_are_not_counted(self):
        bs = self.BS
        for text in (bs + "u200b" + bs + "u202e", bs + "u0080" + bs + "u00e9", bs + "u4e2d" + bs + "u0e01",
                     bs + "U0001F600" + bs + "U0001F601", bs + "x41" + bs + "x42", bs + "N{GREEK SMALL LETTER ALPHA}"):
            with self.subTest(text=text):
                self.assertEqual(self.lines("a.py", f'X = "{text}"\n'), {})

    def test_a_line_with_both_raw_and_escaped_letters_counts_once(self):
        self.assertEqual(self.lines("a.py", f'X = "{RU}" + "{self.esc(RU)}"\n'), {"a.py": 1})

    def test_the_hidden_words_of_a_docs_folder_are_not_checked(self):
        self.assertEqual(pl.count(tree(self, {"docs/a.py": f'X = "{self.esc(RU)}"\n'})), {})


class Ratchet(unittest.TestCase):
    def test_growth_is_named_by_its_file(self):
        found = pl.problems({"core/a.py": 5, "core/b.py": 2}, {"core/a.py": 4, "core/b.py": 2})
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].startswith("core/a.py: 5 lines"), found)
        self.assertIn("allows 4", found[0])

    def test_a_new_file_with_cyrillic_is_growth_from_zero(self):
        found = pl.problems({"new.py": 1}, {})
        self.assertEqual(len(found), 1)
        self.assertIn("new.py", found[0])
        self.assertIn("allows 0", found[0])

    def test_less_is_red_until_the_baseline_is_lowered(self):
        found = pl.problems({"a.py": 1}, {"a.py": 3})
        self.assertEqual(len(found), 1)
        self.assertIn("a.py", found[0])
        self.assertIn("--prune", found[0])

    def test_a_translated_or_deleted_file_is_less_too(self):
        found = pl.problems({}, {"gone.py": 3})
        self.assertEqual(len(found), 1)
        self.assertIn("gone.py", found[0])

    def test_the_same_numbers_hold(self):
        self.assertEqual(pl.problems({"a.py": 3, "b.py": 1}, {"a.py": 3, "b.py": 1}), [])

    def test_prune_lowers_and_never_takes_growth(self):
        base = {"a.py": 3, "b.py": 2, "c.py": 4}
        now = {"a.py": 1, "b.py": 5, "new.py": 7}   # a fell, b rose, c is clean, new is new
        self.assertEqual(pl.pruned(now, base), {"a.py": 1, "b.py": 2})

    def test_the_command_prunes_the_baseline_and_fails_on_growth(self):
        root = tree(self, {"a.py": f"{RU}\n", "b.py": f"{RU}\n{RU}\n"})
        folder = tempfile.mkdtemp()
        self.addCleanup(platforms.current.rmtree, folder)
        base = os.path.join(folder, "base.json")
        pl.dump({"a.py": 3, "b.py": 1, "c.py": 2}, base)
        out = io.StringIO()
        with mock.patch.object(pl, "ROOT", root), mock.patch.object(pl, "BASELINE", base), \
                contextlib.redirect_stdout(out):
            code = pl.main(["--prune"])
        self.assertEqual(code, 1, out.getvalue())          # b grew from 1 to 2: still red after pruning
        self.assertIn("b.py", out.getvalue())
        with open(base, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"a.py": 1, "b.py": 1})


class Move(unittest.TestCase):
    def test_a_renamed_file_takes_its_entry_along(self):
        base = {"old.py": 5, "other.py": 2}
        got = pl.moved({"new.py": 5, "other.py": 2}, base, "old.py", "new.py", {"new.py", "other.py"})
        self.assertEqual(got[0], {"new.py": 5, "other.py": 2})

    def test_it_takes_the_new_number_when_the_file_got_cleaner(self):
        self.assertEqual(pl.moved({"new.py": 3}, {"old.py": 5}, "old.py", "new.py", {"new.py"})[0], {"new.py": 3})
        self.assertEqual(pl.moved({}, {"old.py": 5}, "old.py", "new.py", {"new.py"})[0], {})

    def test_it_never_takes_growth_an_unknown_or_a_taken_name(self):
        files = {"new.py"}
        self.assertIsNone(pl.moved({"new.py": 6}, {"old.py": 5}, "old.py", "new.py", files)[0])
        self.assertIsNone(pl.moved({"new.py": 1}, {"old.py": 5}, "gone.py", "new.py", files)[0])
        self.assertIsNone(pl.moved({"new.py": 1}, {"old.py": 5, "new.py": 1}, "old.py", "new.py", files)[0])
        self.assertIn("more than", pl.moved({"new.py": 6}, {"old.py": 5}, "old.py", "new.py", files)[1])

    def test_the_new_name_must_be_a_published_file_and_the_old_one_gone(self):
        # a typo or a file not added to git yet would drop the allowance for good (prune never raises it)
        got = pl.moved({}, {"old.py": 5}, "old.py", "nwe.py", {"new.py"})
        self.assertIsNone(got[0])
        self.assertIn("nwe.py", got[1])
        got = pl.moved({"new.py": 3}, {"old.py": 5}, "old.py", "new.py", {"new.py", "old.py"})   # a copy, not a rename
        self.assertIsNone(got[0])
        self.assertIn("old.py", got[1])

    def test_the_command_moves_the_entry_in_the_file(self):
        root = tree(self, {"new.py": f"{RU}\n"})
        folder = tempfile.mkdtemp()
        self.addCleanup(platforms.current.rmtree, folder)
        base = os.path.join(folder, "base.json")
        pl.dump({"old.py": 1}, base)
        out = io.StringIO()
        with mock.patch.object(pl, "ROOT", root), mock.patch.object(pl, "BASELINE", base), \
                contextlib.redirect_stdout(out):
            self.assertEqual(pl.main(["--move", "old.py", "nwe.py"]), 1)   # a typo: refused, the file untouched
            self.assertEqual(pl.load(base), {"old.py": 1})
            self.assertEqual(pl.main(["--move", "old.py", "new.py"]), 0)
        self.assertEqual(pl.load(base), {"new.py": 1})


class Policy(unittest.TestCase):
    def test_every_exemption_has_a_reason_and_a_path_that_exists(self):
        for path, why in pl.EXEMPT.items():
            self.assertTrue(why.strip(), f"{path}: no reason")
            self.assertTrue(os.path.exists(os.path.join(ROOT, path)), f"{path}: gone — drop it from EXEMPT")

    def test_the_baseline_names_no_exempt_or_unpublished_file(self):
        base = pl.load()
        published = set(pl.published(ROOT))
        for path, n in base.items():
            self.assertGreater(n, 0, f"{path}: a zero in the baseline — run tools/public_language.py --prune")
            self.assertFalse(pl.exempt(path), f"{path}: exempt, it should not be in the baseline")
            self.assertIn(path, published, f"{path}: not a published file any more — run tools/public_language.py --prune")

    def test_this_tooling_is_clean_itself(self):
        for path in ("tools/public_language.py", "tests/test_public_language.py"):
            self.assertEqual(pl.lines_with_cyrillic(os.path.join(ROOT, path)), 0, path)

    def test_the_update_self_check_does_not_run_it(self):
        # a hub with its own edits of the core must not roll an update back over how the project's own text is written
        with open(os.path.join(ROOT, "tools", "test.py"), encoding="utf-8") as f:
            line = next(x for x in f if x.startswith("DEV_ONLY"))
        self.assertIn("tests.test_public_language.", line)

    def test_the_tree_does_not_grow(self):
        found = pl.problems(pl.count(ROOT), pl.load())
        self.assertFalse(found, "the language of the published tree (tools/public_language.py):\n" + "\n".join(found))


if __name__ == "__main__":
    unittest.main()
