"""The update self-check decides by the exit code of tools/test.py: retries of flaky page tests in Chrome (2026-10-04:
the auto-update rolled back a green update twice) live there, in one place (tests/test_parallel_runner.py)."""
import tests  # noqa: F401
import contextlib
import subprocess
import unittest

from modules.update import update

FULL_FAIL = """..F.
======================================================================
FAIL: test_settings (test_web_e2e.PageInChrome.test_settings)
----------------------------------------------------------------------
AssertionError: step failed
======================================================================
ERROR: test_theme (test_web_e2e.PageInChrome.test_theme)
----------------------------------------------------------------------
Ran 698 tests in 300s

FAILED (failures=1, errors=1)
"""


class Runner:
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        self.kw = kw
        code, out = self.results.pop(0)
        return subprocess.CompletedProcess(cmd, code, out, "")


class FlakyE2E(unittest.TestCase):
    def test_a_red_run_is_red_without_a_retry_of_its_own(self):
        # 2026-10-09: retries of flaky tests live in one place, tools/test.py; the self-check decides by exit code
        run = Runner((1, FULL_FAIL))
        ok, out, failed, _ = update.run_tests("/repo", run=run)
        self.assertFalse(ok)
        self.assertEqual(len(run.calls), 1)
        self.assertIn("FAILED", out)
        self.assertEqual(failed, ["tests.test_web_e2e.PageInChrome.test_settings",
                                  "tests.test_web_e2e.PageInChrome.test_theme"])   # with the tests. prefix, as tools/test.py does

    def test_failed_names_come_from_the_whole_output_not_from_its_cut_tail(self):
        # the output tail in update.failed is 1500 characters: names of failed tests fall earlier, so they are taken
        # from the full output (format as tools/test.py prints it: "FAIL: name (tests.module.Class.name)")
        names = [f"tests.test_a.A.test_{i}" for i in range(40)]
        out = "".join(f"FAIL: test_{i} ({n})\nAssertionError\n" for i, n in enumerate(names))
        out += f"ERROR: test_0 ({names[0]})\n" + "x" * 3000 + "\nFAILED (failures=40)\n"
        ok, tail, failed, _ = update.run_tests("/repo", run=Runner((1, out)))
        self.assertNotIn("FAIL: test_0 ", tail)
        self.assertEqual(failed, names)   # no repeats (FAIL and ERROR of one test), in order; the journal gets at most MAX_FAILED

    def test_a_module_that_does_not_load_is_named_by_itself(self):
        out = "ERROR: test_x (unittest.loader._FailedTest.test_x)\nImportError\nFAILED (errors=1)\n"
        self.assertEqual(update.run_tests("/repo", run=Runner((1, out)))[2], ["tests.test_x"])

    def test_tests_run_apart_from_the_hub_console(self):
        # 2026-10-08, Windows: the run shared the hub's console, so a Ctrl+C from a test also reached the hub, which
        # shut down within a minute
        import platforms
        run = Runner((0, "OK\n"))
        update.run_tests("/repo", run=run)
        self.assertLessEqual(platforms.current.apart().items(), run.kw.items())

    def test_real_run_goes_through_the_platform_run_that_kills_the_tree_on_timeout(self):
        # 2026-10-09: on a timeout subprocess.run on Windows killed only python, leaving the test browsers orphaned
        import platforms
        from unittest import mock
        with mock.patch.object(platforms.current, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "OK\n", "")
            self.assertTrue(update.run_tests("/repo")[0])
        run.assert_called_once()

    def test_the_real_updater_takes_the_machine_lock_and_injected_tests_do_not(self):
        self.assertIs(update.Updater("/r", "/s", {}).turn, update.machine_turn)
        self.assertIs(update.Updater("/r", "/s", {}, tests=lambda repo: (True, "")).turn, contextlib.nullcontext)

    def test_a_second_full_run_waits_for_the_machine_lock(self):
        import os
        import tempfile
        from core import lock, testlock
        path = os.path.join(tempfile.mkdtemp(prefix="turn-"), "full.lock")
        held = lock.acquire(path)
        slept = []
        with self.assertRaises(testlock.Busy):
            testlock.wait_turn(sleep=slept.append, path=path, patience=30, every=10)
        self.assertEqual(slept, [10, 10, 10])
        held.close()
        testlock.wait_turn(path=path).close()

    def test_the_updater_waits_on_the_shared_machine_lock(self):
        # PR review: the updater had its own lock path and wait (like tools/test.py) — they would drift apart and
        # runs would again go at once; a scheduled run does not wait for the lock
        from core import testlock
        from unittest import mock
        with mock.patch.object(testlock, "wait_turn") as wait:
            with update.machine_turn(0):
                pass
        wait.assert_called_once_with(patience=0)

    def test_timeout_or_interrupt_stops_the_page_chromes_of_the_killed_run(self):
        # PR review: on POSIX a timeout and an interrupt kill the tools/test.py group, while the page tests' Chrome
        # lives in its own sessions
        import os
        import tempfile
        from unittest import mock
        for stop_by in (subprocess.TimeoutExpired("tools/test.py", update.TESTS_TIMEOUT), KeyboardInterrupt()):
            def stopped(cmd, **kw):
                raise stop_by
            # the cleanup of abandoned runs (Linux) is mocked too: the machine may hold directories of killed runs,
            # whose Chrome it stops itself — an extra stop_run_chromes call would turn red from the machine's state
            with mock.patch("modules.testchrome.litter.stop_run_chromes") as stop, mock.patch("core.testtmp.sweep"):
                with self.assertRaises(type(stop_by)):
                    update.run_tests("/repo", run=stopped)
            if os.name == "nt":
                stop.assert_not_called()   # Windows: taskkill /T kills the whole tree, Chrome included
            else:
                stop.assert_called_once_with(tempfile.gettempdir())

    def test_a_free_machine_lock_is_seen_without_holding_it(self):
        import os
        import tempfile
        from core import lock, testlock
        path = os.path.join(tempfile.mkdtemp(prefix="turn-"), "sub", "full.lock")
        self.assertTrue(testlock.free(path))
        held = lock.acquire(path)                         # free() released it: the lock can be taken
        self.assertFalse(testlock.free(path))
        held.close()

    def test_the_full_suite_goes_through_the_parallel_runner_without_its_own_lock(self):
        # 2026-10-09: 1607 tests in one process took 990 s against a 1200 s limit; the machine lock is already held
        # by the updater
        import os
        run = Runner((0, "OK\n"))
        update.run_tests("/repo", run=run)
        self.assertEqual(run.calls[0][1:], [os.path.join("/repo", "tools", "test.py"), "--no-lock"])

    def test_the_browser_tests_go_by_the_hubs_setting(self):
        # owner 03.11: browser tests off in the self-check by default, on by update.browser_tests; the word goes in an
        # environment variable without HUB_* (the runner of an earlier release ignores it)
        import json
        import os
        cfg = os.environ["HUB_CONFIG"]   # tests/__init__.py: this run's hub.json
        with open(cfg, "rb") as f:
            was = f.read()
        self.addCleanup(lambda: open(cfg, "wb").write(was))
        for setting, word in ((None, "off"), (True, "on"), ("1", "off")):
            with open(cfg, "w") as f:
                json.dump({"update": {"browser_tests": setting}}, f)
            run = Runner((0, "OK\n"))
            update.run_tests("/repo", run=run)
            self.assertEqual(run.calls[0][1:], [os.path.join("/repo", "tools", "test.py"), "--no-lock"])
            self.assertEqual(run.kw["env"]["AGENTS_HUB_SELF_CHECK_BROWSER"], word, setting)
            self.assertEqual(run.kw["env"]["AGENTS_HUB_SELF_CHECK_UPSTREAM"], "origin/main")

    def test_tests_that_passed_only_on_the_runners_retry_are_named(self):
        # 2026-11-03 (tests research, P8): tools/test.py retries any red once; what passed there is green, and a flake
        # the journal keeps (update.applied) — names from the whole output, not its cut tail
        names = [f"tests.test_a.A.test_{i}" for i in range(40)]
        out = f"flaky (passed on retry): {', '.join(names)}\n" + "x" * 3000 + "\nRan 7000 tests in 900.0s\nOK\n"
        ok, tail, failed, flaky = update.run_tests("/repo", run=Runner((0, out)))
        self.assertEqual((ok, failed, flaky), (True, [], names))
        self.assertEqual(update.run_tests("/repo", run=Runner((0, "OK\n")))[3], [])

    def test_the_flake_line_is_the_one_the_runner_prints(self):
        # review: two copies of the text — a reworded runner line would leave update.applied without flakes, silently
        import importlib.util
        import os
        spec = importlib.util.spec_from_file_location("hub_test_runner_flake", os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "test.py"))
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        out = f"{runner.FLAKE_LINE}tests.a.A.test_1, tests.b.B.test_2\nOK\n"
        self.assertEqual(update.run_tests("/repo", run=Runner((0, out)))[3], ["tests.a.A.test_1", "tests.b.B.test_2"])

    def test_the_flake_line_of_an_earlier_runner_is_still_read(self):
        # the update self-check runs the incoming tree's runner, and a hub may fold an output written by an earlier
        # one: every form the runner ever printed names the flakes
        self.assertGreater(len(update.FLAKE_LINES), 1)
        for line in update.FLAKE_LINES:
            out = f"{line}tests.a.A.test_1, tests.b.B.test_2\nOK\n"
            self.assertEqual(update.run_tests("/repo", run=Runner((0, out)))[3], ["tests.a.A.test_1", "tests.b.B.test_2"], line)

    def test_the_output_is_read_as_utf8_whatever_the_hubs_locale(self):
        # review: the runner writes UTF-8 (PYTHONUTF8); a hub on Windows without UTF-8 mode read it in its code page,
        # and the Russian flake line never matched
        run = Runner((0, "OK\n"))
        update.run_tests("/repo", run=run)
        self.assertEqual(run.kw.get("encoding"), "utf-8")

    def test_green_is_green_once(self):
        run = Runner((0, "OK\n"))
        self.assertTrue(update.run_tests("/repo", run=run)[0])
        self.assertEqual(len(run.calls), 1)


class TimeLimit(unittest.TestCase):
    """A slow machine runs the suite longer than the usual 1200 s: hub.json update.tests_timeout is the limit of the run."""

    def test_the_limit_is_set_in_hub_json(self):
        # one process runs the whole suite in 35 minutes on a slow machine, against a limit of 20: every update would
        # fail on the timeout
        for word in (4000, "4000", 4000.0):   # a string or a float of a hand-edited file too
            self.assertEqual(update.tests_timeout({"update": {"tests_timeout": word}}), 4000, word)
        for bad in ("soon", None, 0, [], {}, -5, "-5", True, False, 0.5, float("inf"), float("nan")):
            # a bool is 1 second, a negative one a run that never starts
            self.assertEqual(update.tests_timeout({"update": {"tests_timeout": bad}}), update.TESTS_TIMEOUT, bad)
        self.assertEqual(update.tests_timeout({"update": "off"}), update.TESTS_TIMEOUT)
        self.assertEqual(update.tests_timeout({}), update.TESTS_TIMEOUT)

    def test_the_limit_only_raises_the_usual_one_and_has_a_ceiling(self):
        # review: "10" ended every update on its timeout (a try every hour, never applied); 10**400 and 1e308 made the
        # run itself raise OverflowError — the update was "unfinished" for good
        for small in (10, "10", 1199):
            self.assertEqual(update.tests_timeout({"update": {"tests_timeout": small}}), update.TESTS_TIMEOUT, small)
        for big in (10 ** 400, 1e308, update.TESTS_TIMEOUT_MAX + 1):
            self.assertEqual(update.tests_timeout({"update": {"tests_timeout": big}}), update.TESTS_TIMEOUT_MAX)
        import subprocess
        import sys
        r = subprocess.run([sys.executable, "-c", "pass"], capture_output=True,
                           timeout=update.tests_timeout({"update": {"tests_timeout": 10 ** 400}}))
        self.assertEqual(r.returncode, 0)

    def test_the_limit_reaches_the_run_and_the_runner(self):
        # direct calls of run_tests too: the limit is the config's, and the runner keeps its retries inside it
        # (tools/test.py: SECONDS_ENV), not inside its own 1000 s
        from unittest import mock
        run = Runner((0, "OK\n"))
        with mock.patch.object(update.catalog, "load_config", return_value={"update": {"tests_timeout": 3600}}):
            update.run_tests("/repo", run=run)
        self.assertEqual(run.kw["timeout"], 3600)
        self.assertEqual(run.kw["env"]["AGENTS_HUB_SELF_CHECK_SECONDS"], "3600")
        run = Runner((0, "OK\n"))
        with mock.patch.object(update.catalog, "load_config", return_value={}):
            update.run_tests("/repo", run=run)
        self.assertEqual(run.kw["timeout"], update.TESTS_TIMEOUT)
        self.assertEqual(run.kw["env"]["AGENTS_HUB_SELF_CHECK_SECONDS"], str(update.TESTS_TIMEOUT))


if __name__ == "__main__":
    unittest.main()
