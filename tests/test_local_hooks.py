"""Hooks for local customisation (tools/test.py, tools/publish.sh): the settings folder may hold test-wrap.json — how to
run chunks of tests (a sandbox, a container) — and an executable publish-checks — own checks before a release. No files —
everything works as before."""
import tests  # noqa: F401 — keep the machine's settings out
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name):
    spec = importlib.util.spec_from_file_location("hub_" + name, os.path.join(ROOT, "tools", name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)


class TestWrap(Case):
    def test_no_file_means_chunks_run_as_before(self):
        self.assertEqual(load("test").wrap(self.dir), [])

    def test_the_file_gives_the_command_that_starts_every_chunk(self):
        with open(os.path.join(self.dir, "test-wrap.json"), "w") as f:
            json.dump({"argv": ["/opt/box/run", "--quiet"]}, f)
        self.assertEqual(load("test").wrap(self.dir), ["/opt/box/run", "--quiet"])

    def test_a_broken_file_is_not_silently_ignored(self):
        # a silent run without the guard is worse than a red one: a broken setting is visible at once
        path = os.path.join(self.dir, "test-wrap.json")
        for bad in ("{", '{"argv": "run"}', '{"argv": []}', '{"argv": [1]}'):
            with open(path, "w") as f:
                f.write(bad)
            with self.assertRaises(SystemExit, msg=bad):
                load("test").wrap(self.dir)


sh_check = unittest.skipIf(os.name == "nt", "the check file is an sh script with a shebang: Windows cannot run it that way")


class PublishChecks(Case):
    def check(self, body=None):
        path = os.path.join(self.dir, "publish-checks")
        if body is not None:
            with open(path, "w") as f:
                f.write(body)
            os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        return load("publish_checks").run("abc123", self.dir)

    def test_no_file_nothing_to_check(self):
        self.assertEqual(self.check(), 0)

    @sh_check
    def test_the_file_gets_the_commit_and_its_exit_code_decides(self):
        out = os.path.join(self.dir, "got")
        self.assertEqual(self.check(f"#!/bin/sh\necho \"$1\" > {out}\nexit 0\n"), 0)
        self.assertEqual(open(out).read().strip(), "abc123")
        self.assertEqual(self.check("#!/bin/sh\nexit 7\n"), 7)

    @sh_check
    def test_the_check_starts_in_the_repository_root(self):
        out = os.path.join(self.dir, "cwd")
        elsewhere = os.getcwd()
        os.chdir(self.dir)   # by this point publish.sh sits in the snapshot's temporary folder
        self.addCleanup(os.chdir, elsewhere)
        self.check(f"#!/bin/sh\npwd > {out}\n")
        self.assertEqual(os.path.realpath(open(out).read().strip()), os.path.realpath(ROOT))

    @sh_check
    def test_a_file_that_is_not_executable_is_an_error_not_a_pass(self):
        path = os.path.join(self.dir, "publish-checks")
        with open(path, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(path, 0o644)
        self.assertNotEqual(load("publish_checks").run("abc", self.dir), 0)

    def test_publish_sh_calls_it_before_the_push(self):
        text = open(os.path.join(ROOT, "tools", "publish.sh")).read()
        self.assertLess(text.index("publish_checks.py"), text.index("git push -q origin main"))


if __name__ == "__main__":
    unittest.main()
