"""The guard `codex` of the tests and the evals (tools/codex_guard.py): no codex without a CODEX_HOME of its own.

A bare `codex --version` writes its helper folder into the person's ~/.codex/tmp (five such starts in one wave of the Codex
experiment, 2026-11-05); the guard first in PATH refuses it with the reason, the rest goes on to the real binary."""
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import platforms
import tests
from tests.fakes import exe
from tools import codex_guard

# the "real" codex of a test: records its CODEX_HOME, arguments and stdin, answers with code 7
REAL = r'''import json, os, sys
with open(os.environ["GUARD_TEST_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps({"home": os.environ.get("CODEX_HOME"), "args": sys.argv[1:], "stdin": sys.stdin.read()}) + "\n")
sys.exit(7)
'''


class Guard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(platforms.current.rmtree, self.tmp)
        self.person = os.path.join(self.tmp, "person")   # the person's home: their ~/.codex lies there
        self.bin = os.path.join(self.tmp, "machine-bin")
        os.makedirs(os.path.join(self.person, ".codex"))
        os.makedirs(self.bin)
        self.real = exe.script(os.path.join(self.bin, "codex"), REAL)
        self.log = os.path.join(self.tmp, "calls.jsonl")

    def guard(self, path=None):
        with mock.patch.dict(os.environ, {"PATH": path if path is not None else self.bin}):
            return codex_guard.install(os.path.join(self.tmp, "guard"), self.person)

    def start(self, guard, home, *args, stdin=""):
        env = {k: v for k, v in os.environ.items() if k != "CODEX_HOME"}
        env.update(GUARD_TEST_LOG=self.log, **({"CODEX_HOME": home} if home is not None else {}))
        return subprocess.run([guard, *args], input=stdin, capture_output=True, text=True, encoding="utf-8", env=env,
                              timeout=60)

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as f:
            return [json.loads(line) for line in f]

    def test_without_codex_home_it_is_refused_with_the_reason(self):
        r = self.start(self.guard(), None, "--version")
        self.assertEqual(r.returncode, codex_guard.REFUSED)
        self.assertIn("CODEX_HOME is not set", r.stderr)
        self.assertEqual(self.calls(), [])   # the real one never ran
        r = self.start(self.guard(), "", "--version")   # an empty one is none: Codex takes ~/.codex then
        self.assertEqual(r.returncode, codex_guard.REFUSED)
        self.assertEqual(self.calls(), [])

    def test_the_persons_own_codex_folder_is_refused(self):
        guard = self.guard()
        own = os.path.join(self.person, ".codex")
        os.makedirs(os.path.join(own, "sub"))
        for home in (own, own + os.sep, os.path.join(own, "sub"), os.path.join(self.person, ".", ".codex")):
            r = self.start(guard, home, "app-server")
            self.assertEqual(r.returncode, codex_guard.REFUSED, home)
            self.assertIn("the personal one", r.stderr)
        self.assertEqual(self.calls(), [])

    def test_a_folder_of_its_own_goes_to_the_real_one_with_args_stdin_and_code(self):
        home = os.path.join(self.tmp, "own-home")
        os.makedirs(home)
        r = self.start(self.guard(), home, "app-server", "-c", "features.hooks=true", stdin='{"id": 0}\n')
        self.assertEqual(r.returncode, 7, r.stderr)
        self.assertEqual(self.calls(), [{"home": home, "args": ["app-server", "-c", "features.hooks=true"],
                                         "stdin": '{"id": 0}\n'}])
        # a sibling whose name starts like the person's folder is not it
        sibling = os.path.join(self.person, ".codex-evals")
        os.makedirs(sibling)
        self.assertEqual(self.start(self.guard(), sibling, "--version").returncode, 7)

    def test_the_real_one_from_local_bin_when_path_has_none(self):
        local = os.path.join(self.person, ".local", "bin")
        os.makedirs(local)
        exe.script(os.path.join(local, "codex"), REAL)
        guard = self.guard(path=os.path.join(self.tmp, "empty"))
        self.assertIsNotNone(guard)
        self.assertEqual(self.start(guard, os.path.join(self.tmp, "h"), "--version").returncode, 7)

    def test_no_real_codex_no_guard(self):
        # a test that checks "codex is not installed" sees the same as before
        self.assertIsNone(self.guard(path=os.path.join(self.tmp, "empty")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "guard", "codex")))

    def test_the_guard_does_not_find_itself_as_the_real_one(self):
        folder = os.path.join(self.tmp, "guard")
        self.guard()
        # a second install with the guard folder first in PATH: the real one is still the machine's
        again = self.guard(path=folder + os.pathsep + self.bin)
        with open(again if os.name != "nt" else again[:-4] + ".py", encoding="utf-8") as f:
            real = ast.literal_eval(re.search(r"^REAL, PERSONAL = (.+)$", f.read(), re.M).group(1))[0]
        # the same file: Windows' which() gives the extension as PATHEXT has it (codex.CMD)
        self.assertEqual(os.path.normcase(real), os.path.normcase(os.path.abspath(self.real)))


    def test_a_run_under_a_guard_keeps_the_path_as_it_is(self):
        # an eval started from the tests (or a nested run): the outer guard is already first — no second folder before
        # it, the PATH the run was given stays (a machine with a real codex outside the sandbox, envmatrix 2026-11-05)
        outer = self.guard()
        path = os.path.dirname(outer) + os.pathsep + self.bin
        with mock.patch.dict(os.environ, {"PATH": path}):
            got = codex_guard.on_path(self.person)
            self.assertEqual(os.environ["PATH"], path)
        self.assertEqual(os.path.normcase(got), os.path.normcase(outer))


class InTheTests(unittest.TestCase):
    def test_codex_from_the_tests_path_is_the_guard_or_none(self):
        # tests/__init__ puts the guard first in PATH when the machine has a real codex: what a test finds by PATH (as
        # hosts/codex.codex_bin does) is never the real binary
        found = shutil.which("codex")
        real = codex_guard.real_codex(tests.REAL_HOME, skip=tests._guard)
        if not real:
            self.skipTest("no codex on this machine")
        self.assertIsNotNone(found)
        with open(found if os.name != "nt" else os.path.splitext(found)[0] + ".py", "rb") as f:
            self.assertTrue(codex_guard.MARK.encode() in f.read(), f"{found} is not a guard")
        r = subprocess.run([found, "--version"], capture_output=True, text=True, encoding="utf-8", timeout=60,
                           env={k: v for k, v in os.environ.items() if k != "CODEX_HOME"})
        self.assertEqual(r.returncode, codex_guard.REFUSED)


class InTheEvals(unittest.TestCase):
    def test_both_eval_runs_put_the_guard_first(self):
        # evals run in the person's own profile: the guard goes first in PATH before anything starts
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "evals"))
        self.addCleanup(sys.path.pop, 0)
        import common
        for name in ("prompts", "run"):
            mod = __import__(name)
            with self.subTest(name), mock.patch.object(mod, "guard_codex", side_effect=SystemExit("guard")), \
                    mock.patch.object(sys, "argv", [name, "x.json"]):
                with self.assertRaises(SystemExit) as got:
                    mod.main()
                self.assertEqual(str(got.exception), "guard")
        with mock.patch.dict(os.environ, {"PATH": os.environ.get("PATH") or ""}):
            got = common.guard_codex()
            if got:
                self.assertEqual(os.environ["PATH"].split(os.pathsep)[0], os.path.dirname(got))


if __name__ == "__main__":
    unittest.main()
