"""The portability lint of the tests (tools/portability_lint.py): a new non-portable test is red here in a second, on
any OS, instead of on a colleague's Windows or another Python as a rolled-back update. The cases below are the lines
of tests that went red there, before and after their fix."""
import tests  # noqa: F401 — hide the machine's settings
import collections
import contextlib
import importlib.util
import io
import os
import tempfile
import textwrap
import unittest
from unittest import mock

from tools import portability_lint as lint

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def rules(src):
    return [(h.rule, h.line) for h in lint.lint(textwrap.dedent(src))]


# (what went red, the rule, the test before its fix, the line the rule names, the test after the fix)
HISTORY = [
    ("a fixture with the Python version of the developer's machine: hubs on 3.12 and 3.13 rolled the update back",
     "T5", """
        def versions(self, plat):
            return {"runtime": 1,
                    "python": {"version": "3.14.8", "license": "PSF"}}
     """, 4, """
        def versions(self, plat):
            return {"runtime": 1,
                    "python": {"version": "%d.%d.8" % sys.version_info[:2], "license": "PSF"}}
     """),
    ("echo as the program of a session: on Windows without Git Bash echo is not a program", "T1", """
        def test_spawn(self):
            h.claude = ["echo"]
            self.assertEqual(h.spawn("w", "s-1"), "spawned")
     """, 3, """
        def test_spawn(self):
            h.claude = [sys.executable, "-c", "pass"]
            self.assertEqual(h.spawn("w", "s-1"), "spawned")
     """),
    ("a preview link glued from a path: file://C:\\... on Windows", "T4", """
        def test_preview(self):
            said = f"shots: file://{shots}/a.png and file://{shots}/b.png"
     """, 3, """
        def test_preview(self):
            url = pathlib.Path(shots).as_uri()
            said = f"shots: {url}/a.png and {url}/b.png"
     """),
    ("two saves of the pool file in one tick of a coarse clock: the same time, the change unseen", "T8", """
        def touch(self):
            st = os.stat(accounts.path())
            os.utime(accounts.path(), ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
     """, 4, """
        def touch(self):   # portable: each save a second later than the last, whatever the clock's tick
            st = os.stat(accounts.path())
            self.touched = max(getattr(self, "touched", 0) + 10**9, st.st_mtime_ns + 10**9)
            os.utime(accounts.path(), ns=(st.st_atime_ns, self.touched))
     """),
    ("a linked folder of a skill without the right to make links: WinError 1314", "T10", """
        def test_a_linked_folder_is_a_right(self):
            os.makedirs(os.path.join(self.root, "k", "bin"))
            os.symlink("../lib/tools", os.path.join(self.root, "k", "bin", "tools"))
            got = self.rights()
     """, 4, """
        def test_a_linked_folder_is_a_right(self):
            os.makedirs(os.path.join(self.root, "k", "bin"))
            try:
                os.symlink("../lib/tools", os.path.join(self.root, "k", "bin", "tools"))
                linked = True
            except OSError:
                linked = False
            got = self.rights()
     """),
    ("the run's folder removed by shutil.rmtree: read-only git objects stayed on Windows", "T2", """
        _RUN = tempfile.mkdtemp(prefix="hub-core-tests-run-")
        atexit.register(shutil.rmtree, _RUN, True)
     """, 3, """
        _RUN = tempfile.mkdtemp(prefix="hub-core-tests-run-")
        atexit.register(platforms.current.rmtree, _RUN)
     """),
    ("a symlink to a folder of a project: no right to make links on Windows", "T10", """
        def test_a_linked_cwd(self):
            os.symlink(cwd, link)
            self.addCleanup(os.unlink, link)
     """, 3, """
        def test_a_linked_cwd(self):
            platforms.current.link_dir(cwd, link)
            self.addCleanup(os.rmdir if os.name == "nt" else os.unlink, link)
     """),
    ("a symlink out of the state folder (red for a normal Windows user, 03.11)", "T10", """
        class StopsAudit(unittest.TestCase):
            def test_a_symlink_from_the_state_folder_is_not_followed(self):
                os.symlink(outside, os.path.join(self.state, "x"))
     """, 4, """
        class StopsAudit(unittest.TestCase):
            @unittest.skipUnless(can.symlink(), "no right to make links here")
            def test_a_symlink_from_the_state_folder_is_not_followed(self):
                os.symlink(outside, os.path.join(self.state, "x"))
     """),
    ("a link to a Mach-O (red for a normal Windows user, 03.11)", "T10", """
        def test_mach_o_thin_and_fat(self):
            os.symlink("python3", os.path.join(self.app, "python"))
     """, 3, """
        def test_mach_o_thin_and_fat(self):
            if can.symlink():
                os.symlink("python3", os.path.join(self.app, "python"))
     """),
]


class Rules(unittest.TestCase):
    def test_each_failure_of_the_past_is_named_before_its_fix_and_not_after(self):
        for why, rule, before, line, after in HISTORY:
            with self.subTest(why):
                self.assertEqual(rules(before), [(rule, line)])
                self.assertEqual(rules(after), [])

    def test_helpers_and_exceptions_are_not_violations(self):
        src = """
            @unittest.skipUnless(can.symlink(), "no right to make links here")
            class Links(unittest.TestCase):
                def test_a(self):
                    pathlib.Path(a).symlink_to(b)
            def test_b(self):
                try:
                    os.symlink(a, b)
                except (PermissionError, NotImplementedError):
                    self.skipTest("no links")
            if sys.version_info >= (3, 12):
                pass
            time.sleep(0.1)
            self.assertEqual(acts, ["kill", "spawn"])
            subprocess.run([sys.executable, "-c", "pass"])
            url = "file:///tmp/a.png"
            platforms.current.rmtree(d)
            self.assertLess(len(calls), 5)
            os.utime(p, (1, 1))
            acts = ["kill", "spawn"]
            executed = ["kill", "spawn"]
            kwargs = ["true", "false"]
            if sys.version_info[:2] in ((3, 12), (3, 13)):
                pass
            self.assertEqual(said, "took 3.15 s")
            # portable: a pause is the subject of this test
            time.sleep(1)
        """
        self.assertEqual(rules(src), [])

    def test_a_skip_by_the_os_or_the_probe_by_its_own_name_is_a_guard(self):
        src = """
            from tests.fakes.can import symlink
            @unittest.skipIf(os.name == "nt", "posix only")
            class Posix(unittest.TestCase):
                def test_a(self):
                    os.symlink(a, b)
                    subprocess.run(["sh", "-c", x])
            @unittest.skipUnless(symlink(), "no right to make links here")
            def test_b(self):
                os.symlink(a, b)
            @unittest.skipIf(sys.platform.startswith("win"), "posix only")
            def test_c(self):
                os.symlink(a, b)
            def test_d(self):
                if sys.platform != "win32":
                    subprocess.run(["chmod", "+x", p])
                if os.name == "nt":
                    pass
                else:
                    os.symlink(a, b)
        """
        self.assertEqual(rules(src), [])

    def test_a_skip_that_still_runs_on_windows_is_no_guard(self):
        src = """
            @unittest.skipUnless(os.name == "nt", "windows only")
            def test_a(self):
                os.symlink(a, b)
            @unittest.skipIf(sys.platform == "darwin", "not on a mac")
            def test_b(self):
                subprocess.run(["echo", "x"])
            @unittest.skipUnless(hasattr(os, "fork"), "fork")
            def test_c(self):
                os.symlink(a, b)
            def test_d(self):
                if os.name == "nt":
                    os.symlink(a, b)
            from tests.fakes.can import symlink
            def test_e(self):
                symlink = d / "l"
                os.symlink(a, symlink)
        """
        self.assertEqual(rules(src), [("T10", 4), ("T1", 7), ("T10", 10), ("T10", 13), ("T10", 17)])

    def test_a_probe_in_one_method_does_not_cover_the_others(self):
        src = """
            class Links(unittest.TestCase):
                @unittest.skipUnless(can.symlink(), "no right to make links here")
                def test_a(self):
                    os.symlink(a, b)
                def test_b(self):
                    os.symlink(a, b)
        """
        self.assertEqual(rules(src), [("T10", 7)])

    def test_a_mark_needs_its_reason_and_its_line(self):
        self.assertEqual(rules("time.sleep(1)   # portable:\n"), [("T6", 1)])
        self.assertEqual(rules("# portable: the pause is tested\nx = 1\ntime.sleep(1)\n"), [("T6", 3)])

    def test_what_each_rule_names(self):
        src = """
            r = subprocess.run(["rm", "-rf", d])
            p = subprocess.Popen(("sleep", "5"))
            from shutil import rmtree
            self.addCleanup(shutil.rmtree, d, True)
            u = "file://" + path
            v = "file://{}".format(path)
            exe = "python3.13"
            if want == (3, 13):
                pass
            time.sleep(0.5)
            self.assertLess(time.monotonic() - started, 4)
            t = os.path.getmtime(p)
            os.symlink(a, b)
            pathlib.Path(a).symlink_to(b)
            import shutil as sh
            sh.rmtree(d)
            subprocess.run(f"rm -rf {d}", shell=True)
            cmd = ["sh", "-c", x]
            host = StreamHost(state, claude=("echo",))
            v = {"version": "3.14.8"}
            n = os.stat(p).st_mtime_ns
            p = ["sh", "-c", x]
            subprocess.run(p)
            out = "Python 3.12.4"
        """
        self.assertEqual(rules(src), [("T1", 2), ("T1", 3), ("T2", 4), ("T2", 5), ("T4", 6), ("T4", 7), ("T5", 8),
                                      ("T5", 9), ("T6", 11), ("T7", 12), ("T8", 13), ("T10", 14), ("T10", 15),
                                      ("T2", 17), ("T1", 18), ("T1", 19), ("T1", 20), ("T5", 21), ("T8", 22),
                                      ("T1", 23), ("T5", 25)])

    def test_a_file_that_does_not_parse_has_none(self):
        self.assertEqual(lint.lint("def (:\n"), [])


class Ratchet(unittest.TestCase):
    def hits(self, src, path="tests/test_x.py"):
        return lint.lint(textwrap.dedent(src), path)

    def test_only_what_the_baseline_does_not_hold_is_new(self):
        old = self.hits("""
            time.sleep(1)
            shutil.rmtree(d)
        """)
        base = collections.Counter(lint.key(h) for h in old)
        moved = self.hits("""
            x = 1

            shutil.rmtree(d)
            time.sleep(1)
        """)
        self.assertEqual(lint.new(moved, base), [])
        again = self.hits("""
            time.sleep(1)
            time.sleep(1)
            shutil.rmtree(d)
        """)
        self.assertEqual([(h.rule, h.line) for h in lint.new(again, base)], [("T6", 3)])   # one more of the same line
        edited = self.hits("""
            time.sleep(2)
            shutil.rmtree(d)
        """)
        self.assertEqual([(h.rule, h.line) for h in lint.new(edited, base)], [("T6", 2)])   # an edited line is new
        self.assertEqual([h.rule for h in lint.new(old, collections.Counter())], ["T6", "T2"])
        other = self.hits("time.sleep(1)\n", "tests/test_y.py")   # the baseline is per file
        self.assertEqual([h.path for h in lint.new(other, base)], ["tests/test_y.py"])

    def test_pruning_drops_what_was_fixed_and_takes_nothing_new(self):
        base = collections.Counter(lint.key(h) for h in self.hits("""
            time.sleep(1)
            time.sleep(1)
            shutil.rmtree(d)
        """))
        now = self.hits("""
            time.sleep(1)
            time.sleep(5)
        """)
        left = lint.pruned(now, base)
        self.assertEqual(sorted(left.values()), [1])
        self.assertEqual(lint.new(now, left)[0].text, "time.sleep(5)")
        d = tempfile.mkdtemp()
        lint.dump(left, os.path.join(d, "b.json"))
        self.assertEqual(lint.load(os.path.join(d, "b.json")), left)

    def test_the_tests_bring_no_new_violation(self):
        found = lint.new(lint.scan(ROOT), lint.load())
        self.assertFalse(found, "new non-portable code in tests/ (fix it, or mark a deliberate case on its line "
                                    "with `# portable: <why>`):\n" + "\n".join(lint.describe(h) for h in found))


class SelfCheck(unittest.TestCase):
    def test_the_update_self_check_does_not_run_the_lint(self):
        # the lint guards new tests while the core is written; on a hub with its own tests (an edit of the core, the full
        # self-check) their style is no reason to roll its update back
        spec = importlib.util.spec_from_file_location("hub_test_runner_lint", os.path.join(ROOT, "tools", "test.py"))
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        runner.TIMES = os.path.join(tempfile.mkdtemp(), "times.json")
        names = ["tests.test_a.A.test_x", "tests.test_portability_lint.Ratchet.test_the_tests_bring_no_new_violation"]

        def ran(argv):
            calls = []
            with mock.patch.object(runner, "own_edits", lambda root=None, cfg=None: None), \
                    mock.patch.object(runner, "yield_machine", lambda: None), \
                    mock.patch.object(runner, "wait_turn", lambda: None), \
                    mock.patch.object(runner, "browser_setting", lambda: False), \
                    mock.patch.object(runner, "in_memory", lambda: None), contextlib.redirect_stdout(io.StringIO()):
                code = runner.main(argv, run=lambda n, env: (calls.append(list(n)), (0, "Ran 1 test\n\nOK", 0.1))[1],
                                   names_of=lambda _: names)
            self.assertEqual(code, 0)
            return {n for c in calls for n in c}
        self.assertEqual(ran(["--no-lock"]), {"tests.test_a.A.test_x"})
        self.assertEqual(ran([]), set(names))
        self.assertEqual(ran(["--no-lock", "tests.test_portability_lint"]), set(names))   # asked for by name


if __name__ == "__main__":
    unittest.main()
