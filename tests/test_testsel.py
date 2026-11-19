"""`tools/test.py --changed`: which test modules a diff touches (core/testsel.py). A small repo of its own stands in for the
real one: each way a test can depend on a file (import, a script by its shebang, a path, a folder it reads, the page) gets
a case, plus the cases where the answer is "run everything" and the ones that must NOT select a test."""
import tests  # noqa: F401 — do not see the machine's settings
import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from core import testsel

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEAD = '''import os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
'''
FILES = {
    "core/__init__.py": "",
    "core/a.py": "X = 1\n",
    "core/b.py": "from core import a\n",
    "core/i18n.py": 'import os\nDIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "i18n")\n',
    "modules/__init__.py": "",
    "modules/web/__init__.py": "",
    "modules/web/server.py": 'import os\nSTATIC = os.path.join(os.path.dirname(__file__), "static")\n',
    "modules/web/static/js/x.js": "export const x = 1\n",
    "modules/web/static/css/x.css": "a {}\n",
    "i18n/en.json": "{}\n",
    "hub-home/CLAUDE.md": "a\n",
    "plugins/p/SKILL.md": "a\n",
    "docs/note.md": "a\n",
    "bin/tool": "#!/usr/bin/env python3\nfrom core import a\n",
    "tools/test.py": "pass\n",
    "tests/__init__.py": "",
    "tests/fakes/f.py": "pass\n",
    # imports a module that imports core.a — transitive
    "tests/test_import.py": "from core import b\n",
    # runs a script by its path: depends on what the script imports
    "tests/test_script.py": HEAD + 'import subprocess\nsubprocess.run([os.path.join(ROOT, "bin", "tool")])\n',
    # reads one file by a path built from constants
    "tests/test_budget.py": HEAD + 'open(os.path.join(ROOT, "hub-home", "CLAUDE.md"))\n',
    # a path as the whole string, and the same name mentioned in a sentence
    "tests/test_string_path.py": 'F = "docs/note.md"\n',
    "tests/test_sentence.py": 'MSG = "see docs/note.md for the rule"\n',
    # imports the core's i18n (which names the folder), which most tests do: a translation file must not select this
    "tests/test_uses_i18n.py": "from core import i18n\n",
    # reads a fixed data folder
    "tests/test_plugins_dir.py": HEAD + 'N = os.listdir(os.path.join(ROOT, "plugins"))\n',
    # walks the whole code tree with a loop variable: a sentinel
    "tests/test_walker.py": HEAD + 'for top in ("core", "modules"):\n    for r, d, f in os.walk(os.path.join(ROOT, top)):\n        pass\n',
    # git ls-files without a path: the whole tree
    "tests/test_lsfiles.py": HEAD + 'import subprocess\nsubprocess.run(["git", "ls-files"], cwd=ROOT)\n',
    # the page in Chrome
    "tests/test_web_e2e.py": "from modules.web import server\n",
    "tests/test_page_extra.py": "from tests import test_web_e2e\n",
    "tests/test_unrelated.py": "import os\n",
    # pathlib forms: a chain and Path(a, b, c); the prefix of a chain is not a reference of its own
    "tests/test_pathlib.py": 'from pathlib import Path\nROOT = Path(__file__).resolve().parents[1]\nA = ROOT / "plugins" / "p" / "SKILL.md"\n'
                             'B = Path(ROOT, "docs", "note.md")\n',
    # a name that looks like a dotted module but is a file name
    "tests/test_dotted.py": 'N = "modules.json"\n',
    "plugins/q/OTHER.md": "a\n",
    "docs/other.md": "a\n",
}


def git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t"})


class Repo(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        for rel, text in FILES.items():
            self.put(rel, text)
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "base")

    def put(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def pick(self, *changed, **kw):
        res = testsel.select(self.root, changed=list(changed), source=testsel.Disk(self.root), **kw)
        self.assertIsNone(res["full"], res["full"])
        return {t.split("/")[-1][:-3] for t in res["tests"]}, {t.split("/")[-1][:-3] for t in res["sentinels"]}

    def chosen(self, *changed):
        a, b = self.pick(*changed)
        return a | b


class Dependencies(Repo):
    def test_import_is_followed_transitively(self):
        self.assertIn("test_import", self.chosen("core/a.py"))
        self.assertIn("test_import", self.chosen("core/b.py"))

    def test_a_script_is_found_by_its_shebang_and_its_imports_count(self):
        got = self.chosen("core/a.py")
        self.assertIn("test_script", got)          # test -> bin/tool (a path) -> core.a (an import)
        self.assertIn("test_script", self.chosen("bin/tool"))

    def test_a_path_built_from_constants_selects_the_test_that_reads_it(self):
        self.assertIn("test_budget", self.chosen("hub-home/CLAUDE.md"))
        self.assertNotIn("test_budget", self.chosen("core/a.py"))

    def test_a_whole_string_path_counts_and_a_sentence_mentioning_it_does_not(self):
        got = self.chosen("docs/note.md")
        self.assertIn("test_string_path", got)
        self.assertNotIn("test_sentence", got)

    def test_a_translation_file_does_not_select_every_test_that_imports_the_i18n_module(self):
        # core/i18n.py names the folder, but nearly every test imports it: the reference is not passed on
        many = dict(FILES)
        for i in range(10):
            self.put(f"tests/test_many{i}.py", "from core import i18n\n")
        git(self.root, "add", "-A")
        got = self.chosen("i18n/en.json")
        self.assertFalse({f"test_many{i}" for i in range(10)} & got)

    def test_a_fixed_data_folder_is_a_dependency_and_not_a_sentinel(self):
        a, s = self.pick("plugins/p/SKILL.md")
        self.assertIn("test_plugins_dir", a)
        self.assertNotIn("test_plugins_dir", s)
        self.assertNotIn("test_plugins_dir", self.chosen("docs/note.md"))

    def test_the_page_static_selects_the_page_tests_and_those_built_on_them(self):
        for f in ("modules/web/static/js/x.js", "modules/web/static/css/x.css"):
            got = self.chosen(f)
            self.assertLessEqual({"test_web_e2e", "test_page_extra"}, got, f)
            self.assertNotIn("test_unrelated", got)

    def test_translations_select_the_page_tests(self):
        self.assertLessEqual({"test_web_e2e", "test_page_extra"}, self.chosen("i18n/en.json"))

    def test_pathlib_chains_and_Path_with_several_parts_name_the_file_and_not_their_prefix(self):
        self.assertIn("test_pathlib", self.chosen("plugins/p/SKILL.md"))
        self.assertIn("test_pathlib", self.chosen("docs/note.md"))
        self.assertNotIn("test_pathlib", self.chosen("plugins/q/OTHER.md"))
        self.assertNotIn("test_pathlib", self.chosen("docs/other.md"))

    def test_a_file_name_that_looks_like_a_module_name_is_not_an_import(self):
        self.assertNotIn("test_dotted", self.chosen("modules/__init__.py"))

    def test_a_deleted_file_still_selects_who_imported_it(self):
        os.remove(os.path.join(self.root, "core", "a.py"))
        self.assertIn("test_import", self.chosen("core/a.py"))

    def test_a_changed_test_is_in_its_own_selection(self):
        self.assertIn("test_unrelated", self.chosen("tests/test_unrelated.py"))


class Corners(Repo):
    def test_a_relative_import_of_a_deleted_module_still_selects_the_importer(self):
        self.put("core/helper.py", "H = 1\n")
        self.put("core/user.py", "from .helper import H\n")
        self.put("tests/test_user.py", "from core import user\n")
        git(self.root, "add", "-A")
        os.remove(os.path.join(self.root, "core", "helper.py"))
        self.assertIn("test_user", self.chosen("core/helper.py"))

    def test_a_script_at_the_root_of_the_page_folder_finds_its_import(self):
        self.put("front.js", "import { x } from './lib.js'\n")
        self.put("lib.js", "export const x = 1\n")
        self.put("tests/test_front.py", HEAD + 'P = os.path.join(ROOT, "front.js")\n')
        self.assertIn("test_front", self.chosen("lib.js"))

    def test_a_changed_python_file_that_does_not_parse_means_a_full_run(self):
        self.put("core/a.py", "def (:\n")
        res = testsel.select(self.root, changed=["core/a.py"], source=testsel.Disk(self.root))
        self.assertIn("does not parse", res["full"])

    def test_changes_no_test_depends_on_are_named(self):
        res = testsel.select(self.root, changed=["docs/other.md", "core/a.py"], source=testsel.Disk(self.root))
        self.assertEqual(res["uncovered"], ["docs/other.md"])
        self.assertIn("no test depends on: docs/other.md", "\n".join(testsel.report(res)))

    def test_the_lock_is_for_page_tests_and_big_selections_only(self):
        small = testsel.select(self.root, changed=["core/a.py"], source=testsel.Disk(self.root))
        page = testsel.select(self.root, changed=["modules/web/static/js/x.js"], source=testsel.Disk(self.root))
        self.assertEqual(small["chrome"], [])
        self.assertIn("tests/test_web_e2e.py", page["chrome"])
        self.assertTrue(testsel.heavy(page))
        self.assertFalse(testsel.heavy({**small, "all": 100}))

    def test_the_default_base_is_not_head_on_main_when_the_remote_is_further_back(self):
        remote = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, remote, True)
        git(self.root, "branch", "-M", "main")
        first = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.root, capture_output=True, text=True).stdout.strip()
        self.put("core/a.py", "X = 2\n")
        git(self.root, "commit", "-qam", "edit")
        git(self.root, "update-ref", "refs/remotes/origin/main", first)
        self.assertEqual(testsel.default_base(self.root), first)
        self.assertEqual(testsel.changed_files(self.root, first), ["core/a.py"])


class Replay(Repo):
    """tools/dev/selection_replay.py over a history of its own: the check that a selection keeps the commit's own tests."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("selection_replay", os.path.join(ROOT, "tools", "dev", "selection_replay.py"))
        cls.replay = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.replay)

    def commit(self, msg, files):
        for rel, text in files.items():
            self.put(rel, text)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", msg)

    def test_a_commit_whose_own_test_is_found_is_not_a_miss_and_one_whose_test_is_not_found_is(self):
        self.commit("source and the test that covers it", {"core/a.py": "X = 2\n", "tests/test_import.py": "from core import b\n# 2\n"})
        self.commit("source and a test the graph cannot tie to it", {"core/a.py": "X = 3\n", "tests/test_unrelated.py": "import os\n# 2\n"})
        self.commit("only tests", {"tests/test_import.py": "from core import b\n# 3\n"})
        rows, tests_only = self.replay.replay(self.root, "main", 3)
        self.assertEqual(tests_only, 1)
        self.assertEqual([r["missed"] for r in reversed(rows)], [[], ["tests/test_unrelated.py"]])
        text = "\n".join(self.replay.summary(rows, tests_only))
        self.assertIn("misses (the commit's own test is not in the selection): 1", text)
        self.assertIn("only tests: 1", text)

    def test_a_commit_in_the_past_is_read_as_it_was_then(self):
        self.commit("add a test", {"tests/test_late.py": "from core import a\n"})
        self.commit("change the source", {"core/a.py": "X = 5\n"})
        (row,) = [r for r in self.replay.replay(self.root, "main", 1)[0]]
        self.assertEqual(row["files"], ["core/a.py"])
        self.assertLess(row["selected"], row["total"])


class Sentinels(Repo):
    def test_tests_that_walk_the_tree_always_run(self):
        for change in ("docs/note.md", "core/a.py", "hub-home/CLAUDE.md"):
            a, s = self.pick(change)
            self.assertLessEqual({"test_walker", "test_lsfiles"}, s | a, change)
        a, s = self.pick("docs/note.md")
        self.assertLessEqual({"test_walker", "test_lsfiles"}, s)    # not by a dependency: by what they do
        self.assertNotIn("test_unrelated", s | a)

    def test_the_reason_names_what_the_test_walks(self):
        res = testsel.select(self.root, changed=["docs/note.md"], source=testsel.Disk(self.root))
        self.assertIn("core/", res["sentinels"]["tests/test_walker.py"])
        self.assertIn("whole repo tree", res["sentinels"]["tests/test_lsfiles.py"])


class Doubt(Repo):
    def full(self, *changed):
        return testsel.select(self.root, changed=list(changed), source=testsel.Disk(self.root))["full"]

    def test_the_runner_and_the_shared_helpers_mean_a_full_run(self):
        for f in ("tools/test.py", "tests/__init__.py", "tests/fakes/f.py"):
            self.assertTrue(self.full(f), f)

    def test_an_unknown_file_type_means_a_full_run(self):
        self.put("misc/blob.xyz", "x")
        self.assertIn("unknown file type", self.full("misc/blob.xyz"))

    def test_a_shebang_script_without_an_extension_is_known(self):
        self.assertIsNone(self.full("bin/tool"))

    def test_too_many_files_mean_a_full_run(self):
        names = [f"docs/n{i}.md" for i in range(testsel.MANY + 1)]
        self.assertIn(str(len(names)), self.full(*names))

    def test_no_changes_select_nothing(self):
        res = testsel.select(self.root, changed=[], source=testsel.Disk(self.root))
        self.assertEqual((res["full"], res["tests"], res["sentinels"]), (None, {}, {}))


class TheDiff(Repo):
    def test_commits_since_the_base_the_working_tree_and_new_files_all_count(self):
        git(self.root, "checkout", "-q", "-b", "work")
        self.put("core/a.py", "X = 2\n")
        git(self.root, "commit", "-qam", "edit")
        self.put("docs/note.md", "b\n")                 # working tree
        self.put("docs/new.md", "n\n")                  # not tracked yet
        self.assertEqual(testsel.changed_files(self.root, testsel.default_base(self.root)),
                         ["core/a.py", "docs/new.md", "docs/note.md"])

    def test_a_base_that_looks_like_an_option_is_refused(self):
        self.assertIsNone(testsel.changed_files(self.root, "--output=/tmp/x"))
        self.assertIn("could not compare", testsel.select(self.root, "--output=/tmp/x")["full"])

    def test_without_a_main_branch_it_is_a_full_run_with_the_reason(self):
        git(self.root, "branch", "-m", "trunk")
        res = testsel.select(self.root)
        self.assertIn("no base", res["full"])

    def test_select_reads_the_diff_itself(self):
        git(self.root, "checkout", "-q", "-b", "work")
        self.put("hub-home/CLAUDE.md", "b\n")
        res = testsel.select(self.root)
        self.assertEqual(res["changed"], ["hub-home/CLAUDE.md"])
        self.assertIn("tests/test_budget.py", res["tests"])

    def test_the_report_says_what_and_why(self):
        res = testsel.select(self.root, changed=["core/a.py"], source=testsel.Disk(self.root))
        text = "\n".join(testsel.report(res))
        self.assertIn("files changed: 1", text)
        self.assertIn("tests.test_import — depends on core/a.py (through core/b.py)", text)
        self.assertIn("sentinel", text)
        full = "\n".join(testsel.report(testsel.select(self.root, changed=["tools/test.py"], source=testsel.Disk(self.root))))
        self.assertIn("full run: tools/test.py", full)


class RealTree(unittest.TestCase):
    """The same questions over this repo: they fail when a way of depending on a file stops being understood."""

    @classmethod
    def setUpClass(cls):
        cls.graph = testsel.Graph(testsel.Disk(ROOT))

    def chosen(self, *changed):
        res = testsel.select(ROOT, changed=list(changed), graph=self.graph)
        self.assertIsNone(res["full"])
        return set(res["tests"]) | set(res["sentinels"])

    def test_page_script_selects_the_page_in_chrome(self):
        got = self.chosen("modules/web/static/js/main.js")
        self.assertLessEqual({"tests/test_web_e2e.py", "tests/test_files_e2e.py", "tests/test_web.py"}, got)

    def test_instruction_files_select_their_budget_test(self):
        self.assertIn("tests/test_budget.py", self.chosen("hub-home/CLAUDE.md"))

    def test_translations_select_the_translation_test_and_the_page(self):
        got = self.chosen("i18n/en.json")
        self.assertLessEqual({"tests/test_i18n.py", "tests/test_web_e2e.py"}, got)
        self.assertLess(len(got), len(self.graph.tests) // 2)

    def test_a_leaf_change_selects_a_small_part(self):
        self.assertLess(len(self.chosen("docs/decisions.md")), len(self.graph.tests) // 3)

    def test_a_core_import_selects_the_test_of_that_core_file(self):
        self.assertIn("tests/test_testsel.py", self.chosen("core/testsel.py"))

    def test_the_tree_walking_tests_are_the_sentinels(self):
        res = testsel.select(ROOT, changed=["docs/decisions.md"], graph=self.graph)
        self.assertLessEqual({"tests/test_public_tree.py", "tests/test_platforms.py", "tests/test_old_page_server.py",
                              "tests/test_no_conflict_markers.py", "tests/test_i18n.py"}, set(res["sentinels"]) | set(res["tests"]))


class Runner(unittest.TestCase):
    """tools/test.py: --changed runs the selected modules, --list only shows them, a full-run verdict runs everything."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("hub_test_runner_sel", os.path.join(ROOT, "tools", "test.py"))
        cls.runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.runner)

    def go(self, res, *args):
        asked, out = [], io.StringIO()

        def names_of(names):
            asked.append(list(names))
            return [f"{n}.A.test_x" for n in names] or ["tests.test_all.A.test_x"]
        ran = []

        def run(names, env):
            ran.append(list(names))
            return 0, f"Ran {len(names)} tests in 0.1s\n\nOK", 0.1
        self.runner.TIMES = os.path.join(tempfile.mkdtemp(), "times.json")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.runner.TIMES), True)
        real = self.runner.in_memory
        self.runner.in_memory = lambda: None
        self.addCleanup(setattr, self.runner, "in_memory", real)
        with mock.patch.object(testsel, "select", return_value=res), mock.patch.object(self.runner, "wait_turn") as turn, \
                contextlib.redirect_stdout(out):
            code = self.runner.main(["-j", "2", *args], run=run, names_of=names_of)
        return code, asked, ran, out.getvalue(), turn

    def res(self, **kw):
        base = {"full": None, "base": "abcdef123456", "changed": ["core/a.py"], "all": 5, "sentinels": {}, "uncovered": [],
                "chrome": [],
                "tests": {"tests/test_a.py": ["tests/test_a.py", "core/a.py"]}}
        return {**base, **kw}

    def test_the_selected_modules_are_run_and_the_choice_is_printed(self):
        res = self.res(all=30, sentinels={"tests/test_walk.py": "walks the whole repo tree"})
        code, asked, ran, out, turn = self.go(res, "--changed")
        self.assertEqual((code, asked), (0, [["tests.test_a", "tests.test_walk"]]))
        self.assertIn("test modules selected: 2 of 30", out)
        self.assertIn("tests.test_walk — sentinel", out)
        self.assertEqual(sum(len(c) for c in ran), 2)
        turn.assert_not_called()

    def test_list_only_shows(self):
        code, asked, ran, out, _ = self.go(self.res(), "--changed", "--list")
        self.assertEqual((code, asked, ran), (0, [], []))
        self.assertIn("tests.test_a", out)

    def test_a_full_verdict_runs_everything_with_the_reason(self):
        code, asked, ran, out, turn = self.go(self.res(full="tools/test.py — the shared test-run helper"), "--changed")
        self.assertEqual(asked, [[]])
        self.assertIn("full run: tools/test.py", out)
        turn.assert_called_once()

    def test_no_changes_run_nothing(self):
        code, asked, ran, out, _ = self.go(self.res(changed=[], tests={}), "--changed")
        self.assertEqual((code, asked, ran), (0, [], []))
        self.assertIn("no changes", out)

    def test_a_selection_with_page_tests_takes_the_machine_lock(self):
        res = self.res(all=30, chrome=["tests/test_web_e2e.py"],
                       tests={"tests/test_web_e2e.py": ["tests/test_web_e2e.py", "x"]})
        *_, turn = self.go(res, "--changed")
        turn.assert_called_once()

    def test_a_selection_of_nothing_runs_nothing(self):
        code, asked, ran, out, turn = self.go(self.res(tests={}), "--changed")
        self.assertEqual((code, asked, ran), (0, [], []))
        self.assertIn("nothing to run", out)
        turn.assert_not_called()

    def test_a_big_selection_takes_the_machine_lock(self):
        res = self.res(all=6, tests={f"tests/test_{i}.py": [f"tests/test_{i}.py", "x"] for i in range(3)})
        *_, turn = self.go(res, "--changed")
        turn.assert_called_once()

    def test_a_test_name_after_changed_is_refused_with_a_hint(self):
        with self.assertRaises(SystemExit) as e, contextlib.redirect_stderr(io.StringIO()):
            self.runner.main(["--changed", "tests.test_a"], run=None, names_of=lambda n: [])
        self.assertIn("base to compare", str(e.exception))

    def test_a_base_is_passed_through(self):
        with mock.patch.object(testsel, "select", return_value=self.res()) as sel, contextlib.redirect_stdout(io.StringIO()):
            self.runner.main(["--changed", "origin/main", "--list"], run=None, names_of=lambda n: [])
        sel.assert_called_once_with(self.runner.ROOT, "origin/main")

    def test_names_and_changed_do_not_mix_and_list_needs_changed(self):
        for argv in (["tests.test_a", "--changed"], ["--list"]):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                self.runner.main(argv, run=None, names_of=lambda n: [])


if __name__ == "__main__":
    unittest.main()
