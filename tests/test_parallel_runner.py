"""tools/test.py: the full run in chunks across several processes (2026-10-07: 8.5 min on every merge, down to 1.5).
Any failed test is repeated once (a page one a second time after a pause); what passes there is a named flake."""
import tests  # noqa: F401 — hide the machine's settings
import contextlib
import importlib.util
import io
import os
import re
import unittest

spec = importlib.util.spec_from_file_location(
    "hub_test_runner", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "test.py"))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

NAMES = [f"tests.test_a.A.test_{i}" for i in range(10)] + ["tests.test_b.B.test_x", "tests.test_web_e2e.P.test_y"]


def fail(name):
    return 1, f"======\nFAIL: {name.rsplit('.', 1)[1]} ({name})\n------\nAssertionError\n\nRan 1 test\n\nFAILED (failures=1)", 0.1


class Runner(unittest.TestCase):
    def go(self, run, names=NAMES, sleep=None):
        calls, out = [], io.StringIO()

        def spy(names, env):
            calls.append(list(names))
            return run(names)
        runner.TIMES = os.path.join(self.dir, "times.json")
        with contextlib.redirect_stdout(out):
            code = runner.main(["-j", "3", "x"], run=spy, names_of=lambda _: names,   # with names: no lock
                               **({"sleep": sleep} if sleep else {}))
        return code, calls, out.getvalue()

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        real, runner.in_memory = runner.in_memory, lambda: None   # do not touch the machine's /dev/shm
        self.addCleanup(setattr, runner, "in_memory", real)
        self.real = {}
        for name, stub in (("own_edits", lambda root=None, cfg=None: None), ("browser_setting", lambda: False)):   # not this machine's
            self.real[name] = getattr(runner, name, None)
            setattr(runner, name, stub)
            self.addCleanup(setattr, runner, name, self.real[name])
        self.yielded = []   # nice cannot be undone: the self-check's priority is not given to this test process
        real_yield, runner.yield_machine = runner.yield_machine, lambda: self.yielded.append(True)
        self.addCleanup(setattr, runner, "yield_machine", real_yield)

    def test_chunks_keep_one_module_and_a_limit(self):
        parts = runner.chunks(NAMES)
        self.assertEqual([len(p) for p in parts], [runner.CHUNK, 10 - runner.CHUNK, 1, 1])
        self.assertTrue(all(len({n.rsplit(".", 2)[0] for n in p}) == 1 for p in parts))

    def test_all_green(self):
        code, calls, out = self.go(lambda names: (0, f"Ran {len(names)} tests in 0.1s\n\nOK", 0.1))
        self.assertEqual(code, 0)
        self.assertEqual(sorted(n for c in calls for n in c), sorted(NAMES))
        self.assertTrue(out.rstrip().endswith("OK"))
        self.assertIn("Ran 12 tests", out)

    def test_a_process_without_a_chrome_place_takes_a_chunk_without_chrome(self):
        # 03.11: the slow Chrome chunks go first; five of eight pool threads waited on the Chrome places and the rest of the
        # suite stood behind the page — the self-check ran out of the old updater's 1200 s
        from unittest import mock
        import threading
        page = [f"tests.test_{'web_e2e' if i % 2 else 'proof'}.P.test_{i}" for i in range(6)]   # one chunk each
        light, lock, inside, most, waited = threading.Event(), threading.Lock(), [0], [0], []

        def fake(names, env):
            if names[0].startswith(runner.FLAKY):
                with lock:
                    inside[0] += 1
                    most[0] = max(most[0], inside[0])
                waited.append(light.wait(5))
                with lock:
                    inside[0] -= 1
            else:
                light.set()
            return 0, f"Ran {len(names)} tests\n\nOK", 0.1
        runner.TIMES = os.path.join(self.dir, "times.json")
        with mock.patch.object(runner, "_run", fake), contextlib.redirect_stdout(io.StringIO()):
            code = runner.main(["-j", "4", "x"], names_of=lambda _: page + ["tests.test_a.A.test_0"])
        self.assertEqual(code, 0)
        self.assertEqual(waited, [True] * 6)   # the chunk without Chrome ran while the page ones were still queued
        self.assertLessEqual(most[0], runner.CHROME_AT_ONCE)

    def test_a_page_test_that_fails_under_load_is_run_once_more(self):
        flaky = "tests.test_web_e2e.P.test_y"
        seen = []

        def run(names):
            seen.append(names)
            return fail(flaky) if names == [flaky] and len(seen) < 5 and seen.count([flaky]) == 1 else \
                (0, f"Ran {len(names)} tests\n\nOK", 0.1)
        code, calls, out = self.go(run)
        self.assertEqual(code, 0, out)
        self.assertEqual(calls[-1], [flaky])
        self.assertIn("retry of the failed page tests (1: test_y): passed", out)
        self.assertIn(f"{runner.FLAKE_LINE}{flaky}\n", out)

    def test_a_second_full_run_waits_for_the_first(self):
        # 2026-10-08: two full runs at once, each with its own headless Chrome; the second waits for the lock
        import threading
        path = os.path.join(self.dir, "full.lock")
        first = runner.wait_turn(path)
        said, got = [], []
        t = threading.Thread(target=lambda: got.append(runner.wait_turn(path, every=0.05,
                                                                          say=lambda *a, **k: said.append(a[0]))))
        t.start()
        t.join(0.3)
        self.assertTrue(t.is_alive())                       # waiting
        self.assertEqual(len(said), 1)                      # and said so once
        first.close()
        t.join(3)
        self.assertFalse(t.is_alive())
        got[0].close()

    def test_the_wait_line_says_how_many_runs_are_ahead(self):
        from core import testlock
        from unittest import mock
        said = []
        with mock.patch.object(testlock, "wait_turn") as wait:
            runner.wait_turn(say=lambda *a, **k: said.append(a[0]))
        for n in (1, 3, 5, 11, 22):
            wait.call_args.kwargs["waiting"](n)
        self.assertEqual(said, [f"waiting: {w} ahead in the queue" for w in
                                ("1 run", "3 runs", "5 runs", "11 runs", "22 runs")])

    def test_the_lock_is_the_one_the_update_self_check_takes(self):
        # PR review: the lock path was separate for the run and for the update, so they would diverge and runs would go
        # at once again
        from core import testlock
        from unittest import mock
        with mock.patch.object(testlock, "wait_turn") as wait:
            runner.wait_turn()
        self.assertIsNone(wait.call_args.args[0])   # the default path is testlock.PATH

    def test_a_second_retry_after_a_pause_when_the_first_is_still_red(self):
        # 2026-10-06: an immediate retry meets the same fleet load, a false rollback; a pause and one more retry
        flaky = "tests.test_web_e2e.P.test_y"
        slept, seen = [], []

        def run(names):
            seen.append(names)
            if names == [flaky]:
                return fail(flaky) if seen.count([flaky]) < 3 else (0, "Ran 1 test\n\nOK", 0.1)
            return (0, f"Ran {len(names)} tests\n\nOK", 0.1)
        runner.TIMES = os.path.join(self.dir, "times.json")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = runner.main(["-j", "3", "x"], run=lambda names, env: run(names), names_of=lambda _: NAMES,
                               sleep=slept.append)
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(slept, [runner.RETRY_PAUSE])
        self.assertEqual(seen.count([flaky]), 3)   # the chunk run and two retries
        self.assertIn("second retry", out.getvalue())

    def test_tests_that_stall_under_load_are_retried_like_the_page_ones(self):
        # 2026-10-09: test_web_timeline and test_stream_hubd went red only under load (the self-check right after a hub
        # restart
        # rolled back a green update); they pass one at a time
        for flaky in ("tests.test_stream_hubd.S.test_x", "tests.test_web_timeline.T.test_y"):
            seen = []

            def run(names, flaky=flaky, seen=seen):
                seen.append(names)
                return fail(flaky) if names == [flaky] and seen.count([flaky]) == 1 else                     (0, f"Ran {len(names)} tests", 0.1)
            runner.TIMES = os.path.join(self.dir, "times.json")
            with contextlib.redirect_stdout(io.StringIO()):
                code = runner.main(["-j", "3", "x"], run=lambda names, env: run(names), names_of=lambda _: NAMES + [flaky],
                                   sleep=lambda s: None)
            self.assertEqual(code, 0, flaky)

    def test_two_red_retries_are_red(self):
        flaky = "tests.test_web_e2e.P.test_y"
        slept = []
        runner.TIMES = os.path.join(self.dir, "times.json")
        with contextlib.redirect_stdout(io.StringIO()):
            code = runner.main(["-j", "3", "x"], run=lambda names, env: fail(flaky) if names == [flaky] else
                               (0, f"Ran {len(names)} tests\n\nOK", 0.1), names_of=lambda _: NAMES, sleep=slept.append)
        self.assertEqual((code, slept), (1, [runner.RETRY_PAUSE]))

    def test_the_output_says_the_machine_load_at_start_and_before_each_retry(self):
        # issue #178: a red self-check under load cannot be told from a real one; a line next to "Ran ..." (in the tail
        # the developer gets): cores, load at the start and before each retry
        from unittest import mock
        flaky = "tests.test_web_e2e.P.test_y"
        loads = iter([3.42, 9.0, 1.0])
        runner.TIMES = os.path.join(self.dir, "times.json")
        out = io.StringIO()
        with mock.patch.object(runner.os, "cpu_count", lambda: 12), contextlib.redirect_stdout(out):
            runner.main(["-j", "3", "x"], run=lambda names, env: fail(flaky) if names == [flaky] else
                        (0, f"Ran {len(names)} tests\n\nOK", 0.1), names_of=lambda _: NAMES, sleep=lambda s: None,
                        load=lambda: next(loads))
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[-3], "load (12 cores): at the start 3.4, before the retry 9.0, before the second retry 1.0")
        self.assertTrue(lines[-2].startswith("Ran 12 tests"))
        self.assertEqual(lines[-1], "FAILED (chunks: 1)")

    def test_without_load_average_there_is_no_load_line(self):
        from unittest import mock
        def none():
            raise AttributeError("getloadavg")
        runner.TIMES = os.path.join(self.dir, "times.json")
        out = io.StringIO()
        with mock.patch.object(runner.os, "getloadavg", none, create=True), contextlib.redirect_stdout(out):
            code = runner.main(["-j", "3", "x"], run=lambda names, env: (0, f"Ran {len(names)} tests\n\nOK", 0.1),
                               names_of=lambda _: NAMES)
            self.assertIsNone(runner.load())
        self.assertEqual(code, 0)
        self.assertNotIn("load (", out.getvalue())

    def test_fewer_processes_on_a_busy_machine(self):
        from unittest import mock
        with mock.patch.object(runner.os, "cpu_count", lambda: 10), mock.patch.object(runner.os, "getloadavg", lambda: (1, 1, 1), create=True):
            self.assertEqual(runner.jobs(), 8)
        with mock.patch.object(runner.os, "cpu_count", lambda: 10), mock.patch.object(runner.os, "getloadavg", lambda: (15, 1, 1), create=True):
            self.assertEqual(runner.jobs(), 8)
        with mock.patch.object(runner.os, "cpu_count", lambda: 10), mock.patch.object(runner.os, "getloadavg", lambda: (80, 1, 1), create=True):
            self.assertEqual(runner.jobs(), 3)

    def test_without_load_average_at_most_four_processes(self):
        # 2026-10-09: no getloadavg on Windows, so always eight processes with Chrome; a full run ate 20 GB
        from unittest import mock
        def none():
            raise AttributeError("getloadavg")
        with mock.patch.object(runner.os, "cpu_count", lambda: 12), mock.patch.object(runner.os, "getloadavg", none, create=True):
            self.assertEqual(runner.jobs(), 4)

    def test_no_lock_flag_skips_the_machine_lock(self):
        # the update's self-check already holds the machine lock: taking it a second time would wait for itself
        from unittest import mock
        runner.TIMES = os.path.join(self.dir, "times.json")
        with mock.patch.object(runner, "wait_turn", side_effect=AssertionError("lock")), \
                contextlib.redirect_stdout(io.StringIO()):
            code = runner.main(["--no-lock"], run=lambda names, env: (0, "Ran 1 test\n\nOK", 0.1),
                              names_of=lambda _: NAMES)
        self.assertEqual(code, 0)

    def test_the_update_self_check_runs_no_browser_unless_the_hub_turns_it_on(self):
        # owner 03.11: "let's turn the browser tests off by default — they can be turned on separately" (v1.7.0 on a Mac ran
        # out of the old updater's 1200 s; Windows has skipped the page since 27.10). Browser modules: the page in Chrome
        # and the ones that find Chrome at load (test_proof, test_mermaid_render). A developer's run runs everything
        from unittest import mock
        browser = {"tests.test_web_e2e.P.test_y", "tests.test_files_e2e.F.test_z", "tests.test_web_flow_ripples.R.test_w",
                   "tests.test_proof.P.test_v", "tests.test_mermaid_render.M.test_m"}
        names = NAMES + sorted(browser - set(NAMES))
        runner.TIMES = os.path.join(self.dir, "times.json")

        def ran(argv, windows=False, setting=False):
            calls, out = [], io.StringIO()
            with mock.patch.object(runner, "WINDOWS", windows), mock.patch.object(runner, "wait_turn", lambda: None), \
                    mock.patch.object(runner, "browser_setting", lambda: setting), contextlib.redirect_stdout(out):
                code = runner.main(argv, run=lambda n, env: (calls.append(list(n)), (0, "Ran 1 test\n\nOK", 0.1))[1],
                                   names_of=lambda _: names)
            self.assertEqual(code, 0)
            return {n for c in calls for n in c}, out.getvalue()
        got, out = ran(["--no-lock"])
        self.assertEqual(got, set(names) - browser)
        self.assertIn("skipped 5 page modules (self-check; to turn on — update.browser_tests: true in hub.json)", out)
        for setting in (False, True):   # Windows: never in the self-check (27.10)
            got, out = ran(["--no-lock"], True, setting)
            self.assertEqual(got, set(names) - browser, setting)
            self.assertIn("skipped 5 page modules (self-check, Windows)", out)
        for argv, setting in ((["--no-lock"], True), (["--no-lock", "--browser-tests", "on"], False), ([], False),
                              ([], True)):
            got, out = ran(argv, setting=setting)
            self.assertEqual(got, set(names), (argv, setting))
            self.assertNotIn("page module", out)
        got, _ = ran(["--no-lock", "--browser-tests", "off"], setting=True)   # the updater's word wins over the guess
        self.assertEqual(got, set(names) - browser)

    def test_a_hub_without_edits_of_the_core_runs_no_core_tests_in_the_self_check(self):
        # owner 03.11: "if the user has no changes, the tests need not run at all — they were run here on a clean main";
        # own modules (a whole new modules/<name>/) are not edits of the core: only their own tests
        from unittest import mock
        runner.TIMES = os.path.join(self.dir, "times.json")

        def ran(argv, edits, smoke=(), modules=(True, "module mine: green")):
            calls, out, asked = [], io.StringIO(), []
            with mock.patch.object(runner, "own_edits", lambda root=None, cfg=None: ("refs/hub-update/tags/v9.9.9", edits)), \
                    mock.patch.object(runner, "smoke", lambda root=None: list(smoke)), \
                    mock.patch.object(runner, "module_tests", lambda root, names, env=None: asked.append(names) or modules), \
                    mock.patch.object(runner, "wait_turn", lambda: None), contextlib.redirect_stdout(out):
                code = runner.main(argv, run=lambda n, env: (calls.append(list(n)), (0, "Ran 1 test\n\nOK", 0.1))[1],
                                   names_of=lambda _: NAMES)
            return code, calls, out.getvalue(), asked
        code, calls, out, asked = ran(["--no-lock"], {"core": [], "modules": []})
        self.assertEqual((code, calls, asked), (0, [], []))
        self.assertIn("core tests skipped: the core code matches release v9.9.9 (checked before publishing)", out)
        self.assertEqual(out.splitlines()[-1], "OK")
        code, calls, out, asked = ran(["--no-lock"], {"core": [], "modules": ["mine"]})
        self.assertEqual((code, calls, asked), (0, [], [["mine"]]))
        self.assertIn("module mine: green", out)
        code, calls, out, asked = ran(["--no-lock"], {"core": [], "modules": ["mine"]}, modules=(False, "module mine: red"))
        self.assertEqual((code, calls), (1, []))
        self.assertTrue(out.splitlines()[-1].startswith("FAILED"))
        code, calls, out, _ = ran(["--no-lock"], {"core": [], "modules": []}, ["core/x.py: SyntaxError: invalid syntax"])
        self.assertEqual((code, calls), (1, []))   # a broken machine is red, not a silent green
        self.assertIn("core/x.py: SyntaxError", out)
        code, calls, out, asked = ran(["--no-lock"], {"core": [{"path": "core/x.py", "st": "M"}], "modules": ["mine"]})
        self.assertEqual((code, asked), (0, []))   # an edit of the core: the suite (without the browser), as decided
        self.assertEqual(sorted(n for c in calls for n in c), sorted(n for n in NAMES if not n.startswith(runner.BROWSER)))
        code, calls, out, _ = ran([], {"core": [], "modules": []})   # a developer's run: everything, as before
        self.assertEqual(sorted(n for c in calls for n in c), sorted(NAMES))
        self.assertNotIn(runner.SMOKE_SKIPPED, out)

    def test_the_updaters_word_wins_over_what_the_runner_reads_itself(self):
        # from v1.7.1 the updater says what only it knows: a second hub's own settings
        from unittest import mock
        with mock.patch.dict(os.environ, {runner.BROWSER_ENV: "on"}):
            self.assertTrue(self.real["browser_setting"]())
        with mock.patch.dict(os.environ, {runner.BROWSER_ENV: "off"}), \
                mock.patch.object(runner, "hub_config", lambda: {"update": {"browser_tests": True}}):
            self.assertFalse(self.real["browser_setting"]())
        with mock.patch.object(runner, "hub_config", lambda: {"update": {"browser_tests": True}}):
            self.assertTrue(self.real["browser_setting"]())   # the updater of v1.7.0 and earlier: read here
        self.assertEqual(runner.published({}), [runner.RELEASE_TAGS, "refs/remotes/origin/main"])
        self.assertEqual(runner.published({"update": {"remote": "up", "branch": "rel/x"}})[1], "refs/remotes/up/rel/x")
        with mock.patch.dict(os.environ, {runner.UPSTREAM_ENV: "upstream/main"}):
            self.assertEqual(runner.published({"update": {"remote": "origin"}})[1], "refs/remotes/upstream/main")
        for bad in ("..", "../x", "a/../b", "a b", 3):
            self.assertEqual(runner.published({"update": {"remote": bad}}), [runner.RELEASE_TAGS], bad)

    def test_the_check_copy_finds_its_hubs_state_folder(self):
        from unittest import mock
        seen = []
        import core.catalog
        with mock.patch.object(runner, "ROOT", os.path.join("/s", "state", "update", "work-abc")), \
                mock.patch.object(core.catalog, "load_config", lambda state: seen.append(state) or {"x": 1}):
            self.assertEqual(runner.hub_config(), {"x": 1})
        self.assertEqual(seen, [os.path.join("/s", "state")])

    def test_the_base_is_the_published_snapshot_the_copy_is_built_on(self):
        import json
        import subprocess
        import tempfile
        repo = tempfile.mkdtemp(dir=self.dir)
        env = {**os.environ, "GIT_AUTHOR_NAME": "x", "GIT_AUTHOR_EMAIL": "x@example.test", "GIT_COMMITTER_NAME": "x",
               "GIT_COMMITTER_EMAIL": "x@example.test"}

        def git(*args):
            return subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True).stdout.strip()

        def write(path, text):
            os.makedirs(os.path.dirname(os.path.join(repo, path)) or repo, exist_ok=True)
            with open(os.path.join(repo, path), "w") as f:
                f.write(text)
        base = self.real["own_edits"]
        write("core/a.py", "x = 1\n")
        write("modules/web/w.py", "w = 1\n")
        write(".gitignore", "*.log\n")
        git("init", "-q")
        git("add", "-A")
        git("commit", "-qm", "Sync")
        self.assertIsNone(base(repo, {}))   # nothing published here: tests as usual
        git("tag", "-a", "-m", "rc", "v1.0.0-rc1")
        git("update-ref", "refs/hub-update/tags/v1.0.0-rc1", "refs/tags/v1.0.0-rc1")   # not a release
        self.assertIsNone(base(repo, {}))
        git("tag", "-a", "-m", "v1", "v1.0.0")
        git("update-ref", "refs/hub-update/tags/v1.0.0", "refs/tags/v1.0.0")   # as the updater fetches them: tag objects
        self.assertEqual(base(repo, {}), ("refs/hub-update/tags/v1.0.0", {"core": [], "modules": []}))
        git("commit", "-q", "--allow-empty", "-m", "Merge release v1.0.0")   # own commit, same code
        self.assertEqual(base(repo, {})[1], {"core": [], "modules": []})
        write("x.log", "ignored\n")   # what .gitignore hides is not an edit
        write("modules/mine/module.json", json.dumps({"name": "mine"}))   # an own module: a whole new folder
        write("modules/mine/run.py", "m = 1\n")
        self.assertEqual(base(repo, {})[1], {"core": [], "modules": ["mine"]})
        write("modules/web/new.js", "")   # a new file in a folder of the release: an edit of the core
        self.assertEqual(base(repo, {})[1]["core"], [{"path": "modules/web/new.js", "st": "?"}])
        os.remove(os.path.join(repo, "modules", "web", "new.js"))
        write("core/a.py", "x = 2\n")
        git("commit", "-qam", "own edit")
        self.assertEqual(base(repo, {})[1]["core"], [{"path": "core/a.py", "st": "M"}])
        git("rm", "-q", "modules/web/w.py")   # deleted from the release
        self.assertIn({"path": "modules/web/w.py", "st": "D"}, base(repo, {})[1]["core"])
        git("checkout", "-q", "HEAD", "--", "modules/web/w.py")
        git("tag", "-a", "-m", "v2", "v1.1.0")   # a newer release in the history is the base
        git("update-ref", "refs/hub-update/tags/v1.1.0", "refs/tags/v1.1.0")
        self.assertEqual(base(repo, {})[0], "refs/hub-update/tags/v1.1.0")
        git("update-ref", "refs/remotes/fork/main", "HEAD")   # a branch the updater does not follow is not a base
        git("update-ref", "refs/remotes/origin/task-x", "HEAD")
        self.assertEqual(base(repo, {})[0], "refs/hub-update/tags/v1.1.0")
        git("update-ref", "refs/remotes/origin/main", "HEAD")   # the main channel: the newest commit of the branch inside HEAD
        self.assertEqual(base(repo, {})[0], git("rev-parse", "HEAD"))
        git("update-ref", "refs/remotes/upstream/main", "HEAD~1")
        self.assertEqual(base(repo, {"update": {"remote": "upstream"}})[0], git("rev-parse", "HEAD~1"))
        ahead = git("commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "ahead")   # the branch went on: not inside HEAD
        git("update-ref", "refs/remotes/upstream/main", ahead)
        self.assertEqual(base(repo, {"update": {"remote": "upstream"}})[0], git("rev-parse", "HEAD"))
        self.assertIsNone(base(tempfile.mkdtemp(dir=self.dir), {}))   # not a git tree

    def test_an_own_modules_own_tests_run_in_its_folder(self):
        import tempfile
        root = tempfile.mkdtemp(dir=self.dir)
        for path, text in (("modules/good/test_good.py", "import unittest\nclass G(unittest.TestCase):\n    def test_g(self):\n        pass\n"),
                           ("modules/bad/test_bad.py", "import unittest\nclass B(unittest.TestCase):\n    def test_b(self):\n        self.fail('no')\n"),
                           ("modules/none/run.py", "x = 1\n")):
            os.makedirs(os.path.dirname(os.path.join(root, path)), exist_ok=True)
            with open(os.path.join(root, path), "w") as f:
                f.write(text)
        ok, out = runner.module_tests(root, ["good", "none"])
        self.assertTrue(ok, out)
        self.assertIn("module good: green", out)
        self.assertIn("module none: no tests of its own", out)
        ok, out = runner.module_tests(root, ["good", "bad"])
        self.assertFalse(ok)
        self.assertIn("module bad: red", out)

    def test_an_own_modules_hanging_tests_are_red_with_the_reason_not_a_traceback(self):
        import subprocess
        import tempfile
        from unittest import mock
        root = tempfile.mkdtemp(dir=self.dir)
        os.makedirs(os.path.join(root, "modules", "slow"))
        with open(os.path.join(root, "modules", "slow", "test_slow.py"), "w") as f:
            f.write("x = 1\n")

        def hang(*a, **kw):
            raise subprocess.TimeoutExpired(a[0], kw.get("timeout"))
        with mock.patch.object(runner.subprocess, "run", hang):
            ok, out = runner.module_tests(root, ["slow"])
        self.assertFalse(ok)
        self.assertIn("module slow: red — the tests did not finish", out)

    def test_the_smoke_step_compiles_the_hub_under_this_python(self):
        import tempfile
        root = tempfile.mkdtemp(dir=self.dir)
        for path, text in (("core/ok.py", "x = 1\n"), ("core/bad.py", "def f(:\n"), ("bin/hub", "#!/usr/bin/env python3\nif\n"),
                           ("bin/run.sh", "#!/bin/sh\nif\n"), ("node_modules/x.py", "def (\n"), (".git/hooks/h.py", "(\n")):
            os.makedirs(os.path.dirname(os.path.join(root, path)), exist_ok=True)
            with open(os.path.join(root, path), "w") as f:
                f.write(text)
        got = runner.smoke(root)
        self.assertEqual([g.split(":")[0] for g in got], ["bin/hub", "core/bad.py"], got)
        os.remove(os.path.join(root, "core", "bad.py"))
        os.remove(os.path.join(root, "bin", "hub"))
        self.assertEqual(runner.smoke(root), [])
        with open(os.path.join(root, "core", "talks.py"), "w") as f:   # prints at import: not broken
            f.write("print('hello')\n")
        self.assertEqual(runner.smoke(root), [])
        with open(os.path.join(root, "core", "gone.py"), "w") as f:   # compiles, but this Python has no such module
            f.write("import a_module_this_python_does_not_have\n")
        got = runner.smoke(root)
        self.assertEqual(len(got), 1, got)
        self.assertTrue(got[0].startswith("core.gone: ModuleNotFoundError"), got)

    def test_the_self_check_yields_the_machine(self):
        # owner 03.11: "everyone's computers boil on the update and its tests" — the self-check (--no-lock, as the
        # installed updater calls it) takes the lowest priority and three quarters of the cores
        from unittest import mock
        runner.TIMES = os.path.join(self.dir, "times.json")

        def ran(argv):
            out = io.StringIO()
            self.yielded.clear()
            # three quarters of the cores is the POSIX rule; Windows keeps jobs() (checked below)
            with mock.patch.object(runner, "jobs", lambda: 8), mock.patch.object(runner.os, "cpu_count", lambda: 8), \
                    mock.patch.object(runner, "WINDOWS", False), \
                    mock.patch.object(runner, "wait_turn", lambda: None), contextlib.redirect_stdout(out):
                self.assertEqual(runner.main(argv, run=lambda n, env: (0, f"Ran {len(n)} tests\n\nOK", 0.1),
                                             names_of=lambda _: NAMES), 0)
            return out.getvalue(), bool(self.yielded)
        out, yielded = ran(["--no-lock"])
        self.assertIn("6 processes", out)
        self.assertTrue(yielded)
        out, yielded = ran([])   # a developer's run: as before
        self.assertIn("8 processes", out)
        self.assertFalse(yielded)
        out, _ = ran(["--no-lock", "-j", "5"])   # an explicit number wins
        self.assertIn("5 processes", out)
        with mock.patch.object(runner, "WINDOWS", False):
            self.assertEqual(runner.self_check_jobs(2), 2)
            self.assertEqual(runner.self_check_jobs(12), 9)
        with mock.patch.object(runner, "WINDOWS", True), mock.patch.object(runner, "jobs", lambda: 4):
            self.assertEqual(runner.self_check_jobs(4), 4)

    def test_the_self_check_priority_is_inherited_by_the_chunks(self):
        # a real process: the runner lowers its own priority, a chunk started after it has it too
        import subprocess
        import sys
        if not hasattr(os, "nice"):
            self.skipTest("POSIX: on Windows every chunk gets below normal priority (platforms.background)")
        import platforms.posix
        code = ("import importlib.util,os,subprocess,sys;"
                f"s=importlib.util.spec_from_file_location('r',{spec.origin!r});r=importlib.util.module_from_spec(s);"
                "s.loader.exec_module(r);print(os.nice(0));r.yield_machine();"
                "print(subprocess.run([sys.executable,'-c','import os;print(os.nice(0))'],capture_output=True,"
                "text=True,**r.CHUNK_KW).stdout.strip())")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        before, child = map(int, out.stdout.split())
        self.assertEqual(child, min(19, before + platforms.posix.GIVE_WAY), out.stderr)

    def test_a_red_outside_the_page_that_stays_red_on_its_one_retry_is_red(self):
        # 2026-11-03 (tests research, P8): any red gets one retry; a real one stays red — with no pause and no second retry
        bad, slept = "tests.test_b.B.test_x", []
        code, calls, out = self.go(lambda names: fail(bad) if names == [bad] else (0, "Ran 1 test\n\nOK", 0.1),
                                   sleep=slept.append)
        self.assertEqual(code, 1)
        self.assertEqual((sum(c == [bad] for c in calls), slept), (2, []))
        self.assertTrue(out.rstrip().splitlines()[-1].startswith("FAILED"))
        self.assertIn("AssertionError", out)
        self.assertIn("retry of the failed tests (1: test_x): red again", out)
        self.assertNotIn(runner.FLAKE_LINE, out)

    def test_one_red_outside_the_page_does_not_cancel_the_retry_of_the_page_ones(self):
        # 2026-11-03 (tests research, P8): test_hubbus_l3 (HTTP 503 under load) and two e2e with a cdp timeout -> the whole
        # run FAILED, though each passed on its own: one red outside the page cancelled the retry of all the page ones.
        # Now every red is retried once, in one process; what passed there is green, and named as a flake
        other, page = "tests.test_b.B.test_x", "tests.test_web_e2e.P.test_y"
        once = set()

        def run(names):
            red = [n for n in names if n in (other, page) and n not in once]
            once.update(red)
            return (1, "".join(f"FAIL: {n.rsplit('.', 1)[1]} ({n})\nAssertionError\n" for n in red) + "\nFAILED", 0.1) \
                if red else (0, f"Ran {len(names)} tests\n\nOK", 0.1)
        code, calls, out = self.go(run)
        self.assertEqual(code, 0, out)
        self.assertEqual(calls[-1], [other, page])   # one retry of both, in one process
        self.assertIn(f"{runner.FLAKE_LINE}{other}, {page}\n", out)
        self.assertTrue(out.rstrip().endswith("OK"))

    def test_many_reds_outside_the_page_are_a_breakage_not_load_and_get_no_retry(self):
        # a retry runs in one process: a broken core with hundreds of reds would run them all once more in a row and the
        # update self-check would run out of its time ("did not finish" instead of "red")
        from unittest import mock
        bad = [n for n in NAMES if n.startswith("tests.test_a.")]
        with mock.patch.object(runner, "RETRY_AT_MOST", len(bad) - 1, create=True):
            code, calls, out = self.go(lambda names: (1, "".join(f"FAIL: x ({n})\n" for n in names if n in bad), 0.1)
                                       if set(names) & set(bad) else (0, f"Ran {len(names)} tests\n\nOK", 0.1))
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), len(runner.chunks(NAMES)))   # the chunks only
        self.assertNotIn("retry", out)

    def test_a_retry_that_may_not_fit_the_self_checks_time_is_not_made(self):
        # review: hung tests (each until its own 30–60 s timeout) retried in a row in one process would run the update
        # self-check past the updater's 1200 s — "did not finish" instead of "red". The red chunks' time bounds what
        # their retry can take: in the self-check (--no-lock) it is made only if that still fits; a developer's run
        # has no such limit
        from unittest import mock
        reds = ["tests.test_a.A.test_0", "tests.test_b.B.test_x"]   # two chunks, 60 s each

        def run(names):
            hit = [n for n in names if n in reds]
            if hit and len(names) > 2 or names in ([reds[0]], [reds[1]]):   # the chunks (test_a's eight, test_b's one)
                return 1, "".join(f"FAIL: x ({n})\n" for n in hit) + "FAILED", 60
            return 0, f"Ran {len(names)} tests\n\nOK", 0.1
        for argv, budget, retried in ((["--no-lock"], 100, False), (["--no-lock"], 1000, True), ([], 100, True)):
            calls, out = [], io.StringIO()
            runner.TIMES = os.path.join(self.dir, "times.json")
            with mock.patch.object(runner, "SELF_CHECK_SECONDS", budget, create=True), \
                    mock.patch.object(runner, "STARTED", runner.time.monotonic(), create=True), \
                    mock.patch.object(runner, "self_check_jobs", lambda: 3), contextlib.redirect_stdout(out):
                runner.main(["-j", "3", *argv, "x"], run=lambda names, env: calls.append(list(names)) or run(names),
                            names_of=lambda _: NAMES)
            self.assertEqual(reds in calls, retried, (argv, budget, out.getvalue()))

    def test_the_self_checks_time_for_retries_follows_the_updaters_limit(self):
        # update.tests_timeout: a slow machine gives the whole run more than 1200 s, and the retries inside it more than
        # 1000 s (the updater's word in the environment; no word or a bad one — the usual budget)
        from unittest import mock
        for word, budget in (("4000", 4000 - runner.ROOM), ("1200", runner.SELF_CHECK_SECONDS), ("50", 0),
                             ("soon", runner.SELF_CHECK_SECONDS), ("", runner.SELF_CHECK_SECONDS)):
            with mock.patch.dict(runner.ENV, {runner.SECONDS_ENV: word}):
                self.assertEqual(runner.self_check_seconds(), budget, word)
        with mock.patch.dict(runner.ENV):
            runner.ENV.pop(runner.SECONDS_ENV, None)
            self.assertEqual(runner.self_check_seconds(), runner.SELF_CHECK_SECONDS)

    def test_the_updaters_limit_survives_loading_the_tests(self):
        # the tests clear the updater's words from os.environ when they load (tests/__init__.py), and the runner lists
        # them (ids) before it decides on a retry: it reads what the environment was before they loaded (ENV)
        import subprocess
        import sys
        code = ("import importlib.util, sys\n"
                "spec = importlib.util.spec_from_file_location('hub_test_runner', 'tools/test.py')\n"
                "runner = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(runner)\n"
                "runner.ids(['test_envmatrix'])\n"
                "print(runner.self_check_seconds())\n")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        r = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=120,
                           env={**os.environ, runner.SECONDS_ENV: "4000"})
        self.assertEqual(r.stdout.strip().splitlines()[-1:], [str(4000 - runner.ROOM)], r.stderr[-1500:])

    def test_a_retry_is_made_within_the_updaters_limit_though_the_usual_budget_is_spent(self):
        # the red chunks took 1100 s: not within the usual 1000, within the 4000 the updater gave (a slow machine)
        from unittest import mock
        reds = ["tests.test_a.A.test_0", "tests.test_b.B.test_x"]

        def run(names):
            hit = [n for n in names if n in reds]
            if hit and len(names) > 2 or names in ([reds[0]], [reds[1]]):
                return 1, "".join(f"FAIL: x ({n})\n" for n in hit) + "FAILED", 550
            return 0, f"Ran {len(names)} tests\n\nOK", 0.1
        for word, retried in (("1200", False), ("4000", True)):
            calls, out = [], io.StringIO()
            runner.TIMES = os.path.join(self.dir, "times.json")
            with mock.patch.dict(runner.ENV, {runner.SECONDS_ENV: word}), \
                    mock.patch.object(runner, "STARTED", runner.time.monotonic(), create=True), \
                    mock.patch.object(runner, "self_check_jobs", lambda: 3), contextlib.redirect_stdout(out):
                runner.main(["-j", "3", "--no-lock", "x"], run=lambda names, env: calls.append(list(names)) or run(names),
                            names_of=lambda _: NAMES)
            self.assertEqual(reds in calls, retried, (word, out.getvalue()))

    def test_a_test_with_several_failing_subtests_is_one_red_retried_once(self):
        # review: one test prints a FAIL line per failing subTest — one name, not twenty-five "breakages"
        bad, seen = "tests.test_b.B.test_x", []

        def run(names):
            seen.append(list(names))
            if names == [bad] and len(seen) <= 4:
                return 1, "".join(f"FAIL: test_x ({bad}) (i={i})\nAssertionError\n" for i in range(25)) + "FAILED", 0.1
            return 0, f"Ran {len(names)} tests\n\nOK", 0.1
        code, calls, out = self.go(run)
        self.assertEqual((code, calls[-1]), (0, [bad]), out)
        self.assertIn(f"{runner.FLAKE_LINE}{bad}\n", out)

    def test_page_tests_still_red_after_the_first_retry_get_the_second_when_the_rest_passed(self):
        # review: a red outside the page passed on the first retry, an e2e is still red under the same load — the page
        # one keeps its second retry after the pause, as a page-only red does
        other, page = "tests.test_b.B.test_x", "tests.test_web_e2e.P.test_y"
        count, slept = {other: 0, page: 0}, []

        def run(names):
            red = []
            for n in names:
                if n in count:
                    count[n] += 1
                    if count[n] <= (1 if n == other else 2):
                        red.append(n)
            return (1, "".join(f"FAIL: x ({n})\n" for n in red) + "FAILED", 0.1) if red else \
                (0, f"Ran {len(names)} tests\n\nOK", 0.1)
        code, calls, out = self.go(run, sleep=slept.append)
        self.assertEqual((code, slept, calls[-2:]), (0, [runner.RETRY_PAUSE], [[other, page], [page]]), out)
        self.assertIn(f"{runner.FLAKE_LINE}{other}, {page}\n", out)

    def test_a_class_that_fails_to_set_up_on_the_first_retry_gets_its_failed_tests_again_not_the_whole_class(self):
        # review: "ERROR: setUpClass (tests.test_web_e2e.P)" names the class: the second retry runs the tests that
        # failed, not every test of the class
        page, slept, seen = "tests.test_web_e2e.P.test_y", [], []

        def run(names):
            seen.append(list(names))
            if names == [page] and seen.count([page]) == 1:
                return fail(page)
            if names == [page] and seen.count([page]) == 2:
                return 1, "ERROR: setUpClass (tests.test_web_e2e.P)\nFAILED", 0.1
            return 0, f"Ran {len(names)} tests\n\nOK", 0.1
        code, calls, out = self.go(run, sleep=slept.append)
        self.assertEqual((code, slept, calls[-1]), (0, [runner.RETRY_PAUSE], [page]), out)

    def test_a_page_retry_that_dies_without_names_gets_the_second_retry_as_before(self):
        # review: the first retry of page tests killed under load (no FAIL/ERROR lines) — the pause and the second
        # retry are there for exactly that
        page, slept, seen = "tests.test_web_e2e.P.test_y", [], []

        def run(names):
            seen.append(list(names))
            if names == [page] and seen.count([page]) == 1:
                return fail(page)
            if names == [page] and seen.count([page]) == 2:
                return -9, "", 0.1
            return 0, f"Ran {len(names)} tests\n\nOK", 0.1
        code, calls, out = self.go(run, sleep=slept.append)
        self.assertEqual((code, slept), (0, [runner.RETRY_PAUSE]), out)

    def test_a_module_that_does_not_load_is_red_with_its_own_error_not_retried(self):
        # its name in the output is unittest's (_FailedTest), not a test one could run again
        broken = "tests.test_b"
        code, calls, out = self.go(lambda names: (1, "ERROR: test_b (unittest.loader._FailedTest.test_b)\n"
                                                     "ImportError: Failed to import test module: test_b\n\nFAILED", 0.1)
                                   if names == [broken] else (0, f"Ran {len(names)} tests\n\nOK", 0.1),
                                   names=[n for n in NAMES if not n.startswith(broken + ".")] + [broken])
        self.assertEqual(code, 1)
        self.assertEqual(sum(c == [broken] for c in calls), 1)
        self.assertNotIn("retry", out)
        self.assertIn("ImportError", out)

    def test_a_chunk_that_died_without_fail_lines_is_red_even_when_the_page_retry_is_green(self):
        # 2026-10-19: a chunk was killed (segfault) without FAIL/ERROR, and a failed page test in another chunk passed
        # on retry:
        # the run ended OK, and the update's self-check would have accepted unchecked code
        flaky, dead = "tests.test_web_e2e.P.test_y", "tests.test_b.B.test_x"
        seen = []

        def run(names):
            seen.append(names)
            if names == [dead]:
                return -11, "Fatal Python error: Segmentation fault", 0.1
            if names == [flaky] and seen.count([flaky]) == 1:
                return fail(flaky)
            return 0, f"Ran {len(names)} tests\n\nOK", 0.1
        code, calls, out = self.go(run)
        self.assertEqual(code, 1, out)
        self.assertIn("Segmentation fault", out)
        self.assertTrue(out.rstrip().splitlines()[-1].startswith("FAILED"))

    def test_chunks_and_retries_get_the_in_memory_folder_and_it_is_removed_after(self):
        # 2026-10-17: the run's temporaries go to /dev/shm (core/testtmp), not to the session's disk
        from unittest import mock
        flaky, envs = "tests.test_web_e2e.P.test_y", []

        def run(names, env):
            envs.append(env.get("TMPDIR"))
            return fail(flaky) if names == [flaky] and envs.count(env.get("TMPDIR")) < 3 else (0, "Ran 1 test\n\nOK", 0.1)
        runner.TIMES = os.path.join(self.dir, "times.json")
        held = object()
        with mock.patch.object(runner.testtmp, "remove") as remove, contextlib.redirect_stdout(io.StringIO()):
            code = runner.main(["x"], run=run, names_of=lambda _: NAMES, memory=lambda: ("/mem/run", held))
        self.assertEqual(code, 0)
        self.assertEqual(set(envs), {"/mem/run"})
        remove.assert_called_once_with("/mem/run", held)

    def test_the_in_memory_folder_is_removed_when_the_run_breaks(self):
        from unittest import mock
        runner.TIMES = os.path.join(self.dir, "times.json")

        def boom(names, env):
            raise KeyboardInterrupt
        with mock.patch.object(runner.testtmp, "remove") as remove, self.assertRaises(KeyboardInterrupt):
            runner.main(["x"], run=boom, names_of=lambda _: NAMES, memory=lambda: ("/mem/run", None))
        remove.assert_called_once()

    def test_the_memory_folder_is_chosen_before_the_tests_are_listed(self):
        # review: the lock's place (test-lock.json) is in the settings folder, and the layout replaces it with the tests
        # folder
        from unittest import mock
        order = []
        runner.TIMES = os.path.join(self.dir, "times.json")
        with mock.patch.object(runner.testtmp, "remove"), contextlib.redirect_stdout(io.StringIO()):
            runner.main(["x"], run=lambda names, env: (0, "Ran 1 test\n\nOK", 0.1),
                        names_of=lambda _: order.append("ids") or NAMES,
                        memory=lambda: order.append("memory") or ("/mem/run", None))
        self.assertEqual(order, ["memory", "ids"])

    def test_without_memory_the_chunks_keep_the_usual_temp_folder(self):
        envs = []
        runner.TIMES = os.path.join(self.dir, "times.json")
        with contextlib.redirect_stdout(io.StringIO()):
            runner.main(["x"], run=lambda names, env: envs.append(env) or (0, "Ran 1 test\n\nOK", 0.1),
                        names_of=lambda _: NAMES)
        self.assertTrue(envs)
        usual = {k: v for k, v in runner.ENV.items() if k != runner.STANDS_ENV}   # (a run inside a run has its own folder of stands)
        self.assertTrue(all({k: v for k, v in e.items() if k != runner.STANDS_ENV} == usual for e in envs))   # only the cache of stands is added
        self.assertTrue(all(e.get("TMPDIR") == runner.ENV.get("TMPDIR") for e in envs))

    def test_the_chunks_share_one_folder_of_built_stands_and_it_is_removed_after(self):
        folders = []

        def run(names, env):
            folder = env.get(runner.STANDS_ENV)
            folders.append((folder, os.path.isdir(folder or "")))
            return 0, "Ran 1 test\n\nOK", 0.1
        runner.TIMES = os.path.join(self.dir, "times.json")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(["x"], run=run, names_of=lambda _: NAMES), 0)
        self.assertTrue(folders)
        (folder,) = {f for f, _ in folders}
        self.assertTrue(folder and all(there for _, there in folders))
        self.assertFalse(os.path.exists(folder))
        # named as a run of the tests with our pid mark: a killed run is swept by the next one
        from modules.testchrome import litter
        self.assertTrue(litter.RUN_NAME.match(os.path.basename(folder)), folder)

    def test_a_folder_of_stands_from_outside_is_not_taken_for_the_runs(self):
        outside = os.path.join(self.dir, "outside")
        os.makedirs(outside)
        seen = []
        runner.TIMES = os.path.join(self.dir, "times.json")
        from unittest import mock
        with mock.patch.dict(runner.ENV, {runner.STANDS_ENV: outside}), contextlib.redirect_stdout(io.StringIO()):
            runner.main(["x"], run=lambda names, env: seen.append(env[runner.STANDS_ENV]) or (0, "Ran 1 test\n\nOK", 0.1),
                        names_of=lambda _: NAMES)
        self.assertTrue(seen and outside not in seen)
        self.assertTrue(os.path.isdir(outside))
        # and with no folder of its own (it could not be made) the chunks get none, not the one from outside
        seen.clear()
        with mock.patch.dict(runner.ENV, {runner.STANDS_ENV: outside}), mock.patch.object(runner, "stands_folder", return_value=None), \
                contextlib.redirect_stdout(io.StringIO()):
            runner.main(["x"], run=lambda names, env: seen.append(env.get(runner.STANDS_ENV)) or (0, "Ran 1 test\n\nOK", 0.1),
                        names_of=lambda _: NAMES)
        self.assertEqual(set(seen), {None})

    def test_a_folder_of_stands_that_cannot_be_removed_changes_neither_the_result_nor_the_memory_cleanup(self):
        from unittest import mock
        runner.TIMES = os.path.join(self.dir, "times.json")
        with mock.patch.object(runner.appcode, "remove_tree", side_effect=PermissionError("held")), \
                mock.patch.object(runner.testtmp, "remove") as remove, contextlib.redirect_stdout(io.StringIO()):
            code = runner.main(["x"], run=lambda names, env: (0, "Ran 1 test\n\nOK", 0.1), names_of=lambda _: NAMES,
                               memory=lambda: (self.dir, None))
        self.assertEqual(code, 0)
        remove.assert_called_once()

    def test_every_chunk_goes_through_the_wrap_even_after_the_tests_are_listed(self):
        # 2026-10-17: the layout (ids) loads the tests package, and tests/__init__.py sets its own HUB_CONFIG_DIR in the
        # process of
        # tools/test.py itself: wrap() no longer found test-wrap.json for each chunk, and foreign code ran without the
        # sandbox
        import json
        import tempfile
        from unittest import mock
        conf = tempfile.mkdtemp()
        with open(os.path.join(conf, "test-wrap.json"), "w") as f:
            json.dump({"argv": ["/opt/box/run"]}, f)
        listed = tempfile.mkdtemp()

        def names_of(_):
            os.environ["HUB_CONFIG_DIR"] = listed   # like tests/__init__.py on import
            return NAMES
        argvs = []

        def spawn(argv, **kw):
            argvs.append(argv)
            return mock.Mock(returncode=0, stdout="Ran 1 test\n\nOK", stderr="")
        runner.TIMES = os.path.join(self.dir, "times.json")
        self.addCleanup(setattr, runner, "PREFIX", None)
        with mock.patch.dict(os.environ, {"HUB_CONFIG_DIR": conf}), mock.patch.object(runner.subprocess, "run", spawn), \
                contextlib.redirect_stdout(io.StringIO()):
            code = runner.main(["x"], run=runner.run, names_of=names_of)
        self.assertEqual(code, 0)
        self.assertTrue(argvs)
        self.assertTrue(all(a[0] == "/opt/box/run" for a in argvs), argvs[:2])

    def test_a_chunk_whose_output_is_not_utf8_still_gives_its_output(self):
        # 2026-11-03, Windows: cmd.exe started by a test printed its error in the console's OEM code page; the chunk's
        # output did not decode, r.stdout was None and the whole run died with a TypeError, without the summary
        import sys
        code = "import sys; sys.stdout.buffer.write(b'\\xff\\x98 cmd\\n'); sys.stderr.buffer.write(b'Ran 1 test\\n\\nOK\\n')"
        self.addCleanup(setattr, runner, "PREFIX", None)
        runner.PREFIX = [sys.executable, "-c", code]   # the start of the chunk's command: it ignores the unittest part
        rc, out, _ = runner._run(["tests.test_a.A.test_0"], runner.ENV)
        self.assertEqual(rc, 0)
        self.assertIn("cmd", out)
        self.assertIn("Ran 1 test", out)

    def test_a_test_named_without_the_tests_package_is_that_test_not_its_whole_module(self):
        # 2026-11-03: `tools/test.py test_platforms.Class.test` did not load, and each such name became the whole module
        name = "test_parallel_runner.Runner.test_chunks_keep_one_module_and_a_limit"
        self.assertEqual(runner.ids([name, "test_parallel_runner.Runner.test_all_green"]),
                         ["tests." + name, "tests.test_parallel_runner.Runner.test_all_green"])
        self.assertIn("tests.test_parallel_runner.ChromeModules.test_a_new_module_is_found_by_its_import_not_by_a_mention",
                      runner.ids(["test_parallel_runner"]))   # a module, as CONTRIBUTING.md shows it: still the module


class ChromeModules(unittest.TestCase):
    """Tests in Chrome outside test_web_e2e (their own module on PageInChrome) landed in FLAKY, the shared Chrome lock and a retry under
    load, only by a manual list, and new ones were forgotten (2026-10-21: test_web_e2e_slow_chrome, test_modapi_view_e2e)."""
    TESTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests")
    USES = re.compile(r"^\s*(?:from\s+tests\s+import\s+[^#\n]*\btest_web_e2e\b|from\s+tests\.test_web_e2e\s+import"
                      r"|import\s+tests\.test_web_e2e\b)", re.M)

    def test_every_module_on_the_page_in_chrome_is_in_the_flaky_group(self):
        uses = []
        for f in sorted(os.listdir(self.TESTS)):
            if f.startswith("test_") and f.endswith(".py"):
                with open(os.path.join(self.TESTS, f), encoding="utf-8") as fh:
                    if self.USES.search(fh.read()):
                        uses.append(f"tests.{f[:-3]}.X.test_x")
        self.assertIn("tests.test_web_e2e_slow_chrome.X.test_x", uses)
        self.assertIn("tests.test_modapi_view_e2e.X.test_x", uses)
        self.assertEqual([n for n in uses if not n.startswith(runner.FLAKY)], [])

    def test_a_new_module_is_found_by_its_import_not_by_a_mention(self):
        import tempfile
        d = tempfile.mkdtemp()
        files = {"test_new_page.py": "from tests.test_web_e2e import PageInChrome\n",
                 "test_new_view.py": "import json\nfrom tests import test_modapi_http, test_web_e2e\n",
                 "test_mention.py": '"""FAIL: test_x (test_web_e2e.PageInChrome.test_x)"""\nimport json\n',
                 "test_broken.py": "def (:\n", "helper.py": "from tests import test_web_e2e\n"}
        for name, text in files.items():
            with open(os.path.join(d, name), "w") as f:
                f.write(text)
        self.assertEqual(sorted(runner.chrome_modules(d)), ["tests.test_new_page.", "tests.test_new_view."])

    def test_a_module_that_finds_a_browser_at_load_is_a_browser_one(self):
        import tempfile
        d = tempfile.mkdtemp()
        for name, text in (("test_shot.py", "from modules.testchrome import chrome\nCHROME = chrome.find()\n"),
                           ("test_finder.py", "def test():\n    CHROME = chrome.find()\n"),   # inside a test: not at load
                           ("test_plain.py", "X = 1\n")):
            with open(os.path.join(d, name), "w") as f:
                f.write(text)
        self.assertEqual(runner.chrome_finders(d), ("tests.test_shot.",))
        self.assertTrue({"tests.test_proof.", "tests.test_mermaid_render."} <= set(runner.BROWSER))

    def test_only_files_that_name_the_page_or_a_browser_are_parsed(self):
        # every run, the update self-check and the test modules that load tools/test.py paid ~2.5 s for parsing all
        # ~430 test files twice; a file without the name can hold neither the import nor `chrome.find()`
        import tempfile
        from unittest import mock

        import platforms
        d = tempfile.mkdtemp()
        self.addCleanup(platforms.current.rmtree, d)
        page = "from tests.test_web_e2e import PageInChrome\n"
        shot = "from modules.testchrome import chrome\nCHROME = chrome.find()\n"
        for name, text in (("test_page.py", page), ("test_shot.py", shot), ("test_plain.py", "X = 1\n")):
            with open(os.path.join(d, name), "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
        parsed, real = [], runner.ast.parse

        def parse(text, *a, **kw):
            parsed.append(text)
            return real(text, *a, **kw)
        with mock.patch.object(runner.ast, "parse", parse):
            self.assertEqual(runner.chrome_modules(d), ("tests.test_page.",))
            self.assertEqual(runner.chrome_finders(d), ("tests.test_shot.",))
        self.assertEqual(parsed, [page, shot])


OLD = "v1.6.1"   # the updater installed at the colleagues' hubs: it calls tools/test.py of the new release


def old_updater():
    import subprocess
    return subprocess.run(["git", "rev-parse", "-q", "--verify", f"{OLD}^{{commit}}"], cwd=ROOT,
                          capture_output=True).returncode == 0


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@unittest.skipUnless(old_updater(), f"no {OLD} in this clone")
class OldUpdater(unittest.TestCase):
    """The installed updater of v1.6.1 (its code from the tag, in its own process, as at a colleague's) runs this
    tools/test.py in the check copy: what it sees is only --no-lock, the environment without HUB_* and the copy's git."""

    def setUp(self):
        import shutil
        import subprocess
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="old-updater-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.old = os.path.join(self.tmp, "old")
        os.makedirs(self.old)
        archive = subprocess.run(["git", "archive", OLD, "--", "core", "platforms", "modules", "i18n", "hosts", "hooks"], cwd=ROOT,
                                 check=True, capture_output=True).stdout
        subprocess.run(["tar", "-x", "-C", self.old], input=archive, check=True)
        # the copy the updater checks: this runner and the code it needs, a tiny suite with one browser module
        self.hub = os.path.join(self.tmp, "state", "update", "work-x")
        for part in ("tools", "core", "platforms", "hosts", "modules", "i18n"):   # i18n: the language data core reads
            shutil.copytree(os.path.join(ROOT, part), os.path.join(self.hub, part),
                            ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(os.path.join(ROOT, ".gitignore"), self.hub)   # as in the release: __pycache__ of the run is not a change
        os.makedirs(os.path.join(self.hub, "tests"))
        for name, text in (("__init__.py", ""),
                           ("test_core_x.py", "import unittest\nclass C(unittest.TestCase):\n    def test_ok(self):\n"
                                              "        pass\n"),
                           ("test_web_e2e.py", "import unittest\nclass P(unittest.TestCase):\n    def test_page(self):\n"
                                               "        self.fail('the browser ran')\n")):
            with open(os.path.join(self.hub, "tests", name), "w") as f:
                f.write(text)
        self.env = {**os.environ, "GIT_AUTHOR_NAME": "x", "GIT_AUTHOR_EMAIL": "x@example.test",
                    "GIT_COMMITTER_NAME": "x", "GIT_COMMITTER_EMAIL": "x@example.test"}
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("commit", "-qm", "Sync")
        self.git("tag", "-a", "-m", "v9.9.9", "v9.9.9")
        self.git("update-ref", "refs/hub-update/tags/v9.9.9", "refs/tags/v9.9.9")

    def git(self, *args):
        import subprocess
        subprocess.run(["git", *args], cwd=self.hub, env=self.env, check=True, capture_output=True)

    def check(self):
        import json
        import subprocess
        import sys
        code = ("import json,sys;sys.path.insert(0,sys.argv[1]);from modules.update import update;"
                "ok,tail,*_=update.run_tests(sys.argv[2]);print(json.dumps([ok,tail]))")
        state, cfg, xdg = (os.path.join(self.tmp, n) for n in ("state", "cfg", "xdg"))
        for d in (cfg, xdg):
            os.makedirs(d, exist_ok=True)
        # its own hub: no machine settings reach the runner (it reads the usual settings folder: XDG, APPDATA)
        env = {**os.environ, "HUB_STATE_DIR": state, "HUB_CONFIG_DIR": cfg, "XDG_CONFIG_HOME": xdg, "APPDATA": xdg}
        env.pop("HUB_CONFIG", None)
        r = subprocess.run([sys.executable, "-c", code, self.old, self.hub], cwd=self.old, env=env, capture_output=True,
                           text=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_a_hub_without_own_changes_is_green_without_tests(self):
        ok, tail = self.check()
        self.assertTrue(ok, tail)
        self.assertIn("core tests skipped: the core code matches release v9.9.9", tail)
        self.assertNotIn("Ran ", tail)

    def test_a_hub_with_only_its_own_module_runs_only_that_modules_tests(self):
        import json
        mine = os.path.join(self.hub, "modules", "mine")
        os.makedirs(mine)
        with open(os.path.join(mine, "module.json"), "w") as f:
            json.dump({"name": "mine"}, f)
        with open(os.path.join(mine, "test_mine.py"), "w") as f:
            f.write("import unittest\nclass M(unittest.TestCase):\n    def test_m(self):\n        pass\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "own module")
        ok, tail = self.check()
        self.assertTrue(ok, tail)
        self.assertIn("core tests skipped", tail)
        self.assertIn("module mine: green", tail)

    def test_a_hub_with_own_changes_runs_its_tests_without_the_browser(self):
        with open(os.path.join(self.hub, "tests", "test_core_x.py"), "a") as f:
            f.write("    def test_own(self):\n        pass\n")
        self.git("commit", "-qam", "own edit")
        ok, tail = self.check()
        self.assertTrue(ok, tail)
        self.assertIn("Ran 2 tests", tail)
        self.assertIn("skipped 1 page module (self-check", tail)


if __name__ == "__main__":
    unittest.main()
