"""The shadow of the update self-check and the check of what a merge kept of the release (dev-guard spec §5.4, §5.5,
slice 3). A hub that edited test files of the release runs its own versions of them in the self-check; after the install
the release's versions of those files run against the hub's code, outside the self-check, and a red one is a line in the
band about the hub's own edits of the core and a message to Hub Developer — the update is not held. When the release the
edits are counted from moves, the release's hunks in files of the zones the hub edits whose lines the merged file lacks
are signals for Hub Developer, answered with `hub update lost ok|restored <id>`. Real git repos of their own."""
import tests  # noqa: F401 — hide the machine's settings
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock

import platforms
from tests.test_update_takeoff import Repos
from tests.fakes import cut

RED = "FAIL: test_x (tests.test_a.T.test_x)\n"


class Fixture(Repos):
    def setUp(self):
        super().setUp()
        self.ran = []

    def runner(self, verdict="red"):
        """A stand-in for tools/test.py of the check copy: what it saw, then the verdict."""
        def run(copy, names, limit):
            self.ran.append({"names": names, "limit": limit, "test_a": self.read(copy, "tests/test_a.py"),
                             "core": self.read(copy, "core.py"), "copy": copy})
            if verdict == "timeout":
                return None
            if verdict == "boom":
                raise OSError("no python")
            return (True, "OK", []) if verdict == "green" else (False, RED, ["tests.test_a.T.test_x"])
        return run

    def edit_a_release_test(self):
        """The hub edits a release test and the core; the release moves on elsewhere; the update installs (green)."""
        self.write(self.mine, "tests/test_a.py", "t = 2  # the hub's own version\n")
        self.write(self.mine, "core.py", "a = 2\n")
        self.commit(self.mine)
        self.write(self.author, "core/daemon.py", "d = 2\n")
        self.release_sha = self.release("next")
        self.assertEqual(self.updater().check(), "applied")

    def pending(self):
        from modules.update import shadow
        return shadow.pending(self.state)

    def step(self, verdict="red", turn=None, up=None):
        from modules.update import shadow   # as a process started after the plan (the restart after the install)
        return shadow.step(up or self.updater(turn=turn), runner=self.runner(verdict), started=time.time() + 10)

    def to_dev(self, items):
        return [i["data"]["text"] for i in items if i["type"] == "message.received" and i["data"].get("to") == "hub-dev"]

    def band(self):
        self.taken()
        self.updater().check(apply=False)
        got = [i for i in self.taken() if i["type"] == "update.checked"]
        return (got[-1]["data"].get("local_edits") or {}) if got else {}


class Shadow(Fixture, unittest.TestCase):
    def test_the_release_version_of_an_edited_test_runs_on_the_hubs_code_and_red_is_told_once(self):
        self.edit_a_release_test()
        p = self.pending()
        self.assertEqual((p["modules"], p["files"], p["release"]), (["tests.test_a"], ["tests/test_a.py"], self.release_sha))
        self.taken()
        self.assertEqual(self.step(), "red")
        self.assertEqual(self.ran[0]["test_a"], "t = 1\n")                   # the release's version of the test
        self.assertEqual(self.ran[0]["core"], "a = 2\n")                     # against the hub's code
        self.assertEqual(self.ran[0]["names"], ["tests.test_a"])
        self.assertFalse(os.path.exists(self.ran[0]["copy"]))                # the copy is gone
        self.assertEqual(self.g(self.mine, "status", "--porcelain"), "")    # the live tree untouched
        said = self.to_dev(self.taken())
        self.assertEqual(len(said), 1)
        self.assertIn("tests.test_a.T.test_x", said[0])
        self.assertIsNone(self.pending())
        self.assertEqual(self.band()["shadow"], {"failed": ["tests.test_a.T.test_x"], "count": 1})
        from modules.update import shadow
        shadow.plan(self.state, self.mine, self.g(self.mine, "rev-parse", "HEAD"), self.release_sha, "", self.updater().cfg)
        self.assertEqual(self.step(), "red")
        self.assertEqual(self.to_dev(self.taken()), [])                     # the same release and tests: told once

    def test_green_and_not_checked_say_nothing_and_draw_nothing(self):
        for verdict, result in (("green", "green"), ("timeout", "unchecked"), ("boom", "unchecked")):
            with self.subTest(verdict):
                self.setUp()
                self.edit_a_release_test()
                self.taken()
                self.assertEqual(self.step(verdict), result)
                self.assertEqual(self.to_dev(self.taken()), [])
                self.assertNotIn("shadow", self.band())
                self.assertIsNone(self.pending())

    def test_a_hub_that_edits_no_release_test_runs_nothing(self):
        self.write(self.mine, "core.py", "a = 2\n")
        self.write(self.mine, "tests/test_own.py", "own = 1\n")             # its own new test is not the release's
        self.commit(self.mine)
        self.write(self.author, "core/daemon.py", "d = 2\n")
        self.release("next")
        self.assertEqual(self.updater().check(), "applied")
        self.assertIsNone(self.pending())

    def test_the_line_goes_when_the_hub_puts_the_release_version_back(self):
        self.edit_a_release_test()
        self.step()
        self.write(self.mine, "tests/test_a.py", "t = 1\n")
        self.commit(self.mine)
        self.assertNotIn("shadow", self.band())

    def test_a_backup_of_taken_off_edits_does_not_break_the_check(self):
        from modules.update import update
        self.edit_a_release_test()
        self.step()
        self.write(self.mine, "core.py", "a = 3\n")
        self.commit(self.mine)
        self.assertEqual(update.Updater(self.mine, self.state, self.updater().cfg, tests=lambda repo: (True, "OK"))
                         .local_edits_switch("off"), "off")
        self.write(self.mine, "tests/test_a.py", "t = 3  # edited again after the take-off\n")
        self.commit(self.mine)
        band = self.band()
        self.assertEqual(band["backup"]["count"] > 0, True)
        self.assertEqual(band["shadow"]["failed"], ["tests.test_a.T.test_x"])

    def test_a_busy_machine_keeps_it_pending(self):
        from core import testlock

        def busy(patience):
            raise testlock.Busy("busy")
        self.edit_a_release_test()
        self.assertEqual(self.step(turn=busy), "busy")
        self.assertEqual(self.ran, [])
        self.assertEqual(self.pending()["tries"], 0)

    def test_a_run_killed_again_and_again_ends_as_not_checked(self):
        from modules.update import shadow
        self.edit_a_release_test()

        def killed(copy, names, limit):
            raise KeyboardInterrupt   # the hub restarted in the middle: the process is gone, the record stays
        for _ in range(shadow.TRIES):
            with self.assertRaises(KeyboardInterrupt):
                shadow.step(self.updater(), runner=killed, started=time.time() + 10)
        self.assertEqual(self.pending()["tries"], shadow.TRIES)
        self.assertEqual(self.step(), "unchecked")
        self.assertEqual(self.ran, [])
        self.assertEqual(shadow.report(self.state)["shadow"]["why"], "interrupted")

    def test_it_waits_for_the_restart_after_the_install_and_for_a_mac_awake(self):
        from modules.update import shadow, update
        self.edit_a_release_test()
        self.assertEqual(shadow.step(self.updater(), runner=self.runner()), "waiting")   # planned by this very process
        up = update.Updater(self.mine, self.state, self.updater().cfg, tests=lambda repo: (True, "OK"),
                            dark=lambda: True)
        self.assertEqual(self.step(up=up), "waiting")                         # a dark wake: it would sleep again
        self.assertEqual((self.ran, self.pending()["tries"]), ([], 0))
        later = self.pending()["ts"] + shadow.RESTART_GRACE + 1
        with unittest.mock.patch.object(shadow.time, "time", lambda: later):
            self.assertEqual(shadow.step(self.updater(), runner=self.runner()), "red")   # no restart for hours: it runs

    def test_a_sleep_during_the_run_keeps_it_for_later(self):
        from modules.update import update
        self.edit_a_release_test()
        ticks = iter([(0, 0), (1000, 10)])                                  # 990 s of wall time the clock did not see
        up = update.Updater(self.mine, self.state, self.updater().cfg, tests=lambda repo: (True, "OK"),
                            clock=lambda: next(ticks))
        self.assertEqual(self.step(up=up), "waiting")
        self.assertEqual((self.pending()["tries"], self.pending()["sleeps"]), (0, 1))   # a sleep is not a kill
        self.assertEqual(self.to_dev(self.taken()), [])
        self.assertEqual(self.step(), "red")

    def test_red_without_names_names_the_modules_and_the_count_is_whole(self):
        from modules.update import shadow
        self.edit_a_release_test()
        self.assertEqual(shadow.step(self.updater(), runner=lambda copy, names, limit: (False, "Traceback", []),
                                     started=time.time() + 10), "red")
        self.assertEqual(self.band()["shadow"]["failed"], ["tests.test_a"])
        self.assertEqual({k: shadow.report(self.state)["shadow"][k] for k in ("red", "unnamed")}, {"red": 0, "unnamed": True})
        many = [f"tests.test_a.T.test_{i}" for i in range(45)]
        shadow.plan(self.state, self.mine, self.g(self.mine, "rev-parse", "HEAD"), self.release_sha, "", self.updater().cfg)
        shadow.step(self.updater(), runner=lambda copy, names, limit: (False, "", many), started=time.time() + 10)
        self.assertEqual(self.band()["shadow"]["count"], 45)
        self.assertEqual(shadow.report(self.state)["shadow"]["red"], 45)

    def test_a_release_test_the_hub_deleted_runs_too(self):
        os.unlink(os.path.join(self.mine, "tests", "test_a.py"))
        self.write(self.mine, "core.py", "a = 2\n")
        self.commit(self.mine)
        self.write(self.author, "core/daemon.py", "d = 2\n")
        self.release("next")
        self.assertEqual(self.updater().check(), "applied")
        self.assertEqual(self.pending()["modules"], ["tests.test_a"])
        self.assertEqual(self.step(), "red")
        self.assertEqual(self.ran[0]["test_a"], "t = 1\n")                  # put back in the copy, not in the hub
        self.assertFalse(os.path.exists(os.path.join(self.mine, "tests", "test_a.py")))
        self.assertEqual(self.band()["shadow"]["failed"], ["tests.test_a.T.test_x"])

    def test_a_module_name_that_cannot_be_imported_is_not_run(self):
        from modules.update import shadow
        self.write(self.author, "tests/test_odd-name.py", "t = 1\n")
        self.release("odd")
        self.g(self.mine, "pull", "-q")
        self.write(self.mine, "tests/test_odd-name.py", "t = 2\n")
        self.commit(self.mine)
        self.assertEqual(shadow.changed_tests(self.mine, self.g(self.mine, "rev-parse", "origin/main")),
                         (["tests/test_odd-name.py"], []))

    def test_the_limit_is_the_setting_and_zero_turns_it_off(self):
        from modules.update import shadow
        for v, want in ((120, 120), (0, 0), ("10", 600), (-1, 600), (0.5, 0), (None, 600), (True, 600),
                        (float("nan"), 600), (10 ** 400, 600), (4000, 600)):
            self.assertEqual(shadow.seconds({"update": {"shadow_seconds": v}}), want, v)
        self.assertEqual(shadow.seconds({"update": "x"}), 600)
        self.edit_a_release_test()
        up = self.updater()
        up.cfg["update"]["shadow_seconds"] = 0
        self.assertEqual(shadow.step(up, runner=self.runner()), "unchecked")
        self.assertEqual(self.ran, [])

    def test_the_report_has_numbers_not_names(self):
        from modules.update import shadow
        self.edit_a_release_test()
        self.step()
        got = shadow.report(self.state)["shadow"]
        self.assertEqual({k: got[k] for k in ("result", "modules", "red")}, {"result": "red", "modules": 1, "red": 1})
        self.assertIsInstance(got["seconds"], int)
        self.assertNotIn("test_a", json.dumps(got))

    def test_the_loop_runs_it_and_asks_for_a_look(self):
        from modules.update import update
        self.edit_a_release_test()
        cfg = self.updater().cfg
        got = update.shadow_pass(self.state, cfg, repo=self.mine, tests=lambda repo: (True, "OK"),
                                 runner=self.runner("green"), started=time.time() + 10)
        self.assertEqual(got, "green")
        self.assertTrue(os.path.exists(os.path.join(self.state, update.NOW_FLAG)))
        os.unlink(os.path.join(self.state, update.NOW_FLAG))
        self.assertIsNone(update.shadow_pass(self.state, cfg, repo=self.mine, runner=self.runner()))
        self.assertFalse(os.path.exists(os.path.join(self.state, update.NOW_FLAG)))


class RealRunner(unittest.TestCase):
    """run_release_tests calls the copy's tools/test.py as the self-check does, without the browser, and reads its names."""

    def setUp(self):
        self.copy = tempfile.mkdtemp(prefix="shadow-run-")
        self.addCleanup(platforms.current.rmtree, self.copy)
        os.makedirs(os.path.join(self.copy, "tools"))

    def stub(self, body):
        with open(os.path.join(self.copy, "tools", "test.py"), "w", newline="\n") as f:
            f.write("import sys, time\n" + body)

    def test_the_arguments_and_the_failed_names(self):
        from modules.update import shadow
        self.stub("print(' '.join(sys.argv[1:]))\nprint('FAIL: test_x (tests.test_a.T.test_x)')\nsys.exit(1)\n")
        ok, out, failed = shadow.run_release_tests(self.copy, ["tests.test_a"], 60)
        self.assertFalse(ok)
        self.assertIn("--no-lock --browser-tests off tests.test_a", out)
        self.assertEqual(failed, ["tests.test_a.T.test_x"])

    def test_green(self):
        from modules.update import shadow
        self.stub("print('OK')\n")
        self.assertEqual(shadow.run_release_tests(self.copy, ["tests.test_a"], 60), (True, "OK\n", []))

    def test_the_runner_is_told_the_shadows_own_limit(self):
        """The runner counts its retries against the limit it is told (tools/test.py SECONDS_ENV); without the word it
        counts from the self-check's 1000 s and a retry past the shadow's 600 s ends "not checked"."""
        from modules.update import shadow
        self.stub("import os\nprint(os.environ.get('AGENTS_HUB_SELF_CHECK_SECONDS'))\n")
        self.assertEqual(shadow.run_release_tests(self.copy, ["tests.test_a"], 600), (True, "600\n", []))
        self.assertEqual(shadow.run_release_tests(self.copy, ["tests.test_a"], 90)[1], "90\n")

    def test_over_the_limit_is_none(self):
        from modules.update import shadow
        self.stub("time.sleep(30)\n")
        self.assertIsNone(shadow.run_release_tests(self.copy, ["tests.test_a"], 1))


class Lost(Fixture, unittest.TestCase):
    """The base moved: the release's hunks in zone files the hub edits are looked for in the merged file."""

    def setUp(self):
        super().setUp()
        self.write(self.author, "core/tools.py", "t = 1\nu = 2\nv = 3\n")
        self.write(self.author, "core.py", "a = 1\nb = 2\nc = 3\n")
        self.release("longer")
        self.merge()
        self.band()                                                         # the first look remembers the base
        self.write(self.mine, "core/tools.py", "mine = 1\nt = 1\nu = 2\nv = 3\n")   # the guards zone
        self.write(self.mine, "core.py", "a = 2\nb = 2\nc = 3\n")         # the plain core
        self.commit(self.mine)

    def release_guard(self):
        self.write(self.author, "core/tools.py", "t = 1\nu = 2\nv = 3\nguard_check = True\n")
        self.write(self.author, "core.py", "a = 1\nb = 2\nc = 3\nplain_feature = 1\n")
        return self.release("a new check")

    def merge(self, *how):
        self.g(self.mine, "fetch", "-q", "origin")
        self.g(self.mine, "merge", "-q", "--no-edit", *how, "origin/main")

    def test_a_hunk_the_merge_dropped_is_a_signal_for_hub_developer_and_a_line(self):
        new = self.release_guard()
        self.merge("-s", "ours")                                            # by hand, keeping only its own files
        lost = self.band()["lost"]
        self.assertEqual([i["path"] for i in lost["items"]], ["core/tools.py"])   # core.py is plain core: not looked at
        from modules.update import shadow
        sig = shadow.open_signals(self.state)
        self.assertEqual((len(sig), sig[0]["release"], sig[0]["lines"]), (1, new, ["+guard_check = True"]))
        msg = [i["data"]["text"] for i in self.last if i["type"] == "message.received"]
        self.assertEqual(len(msg), 1)
        self.assertIn(sig[0]["id"], msg[0])
        self.assertIn("guard_check = True", msg[0])
        self.band()
        self.assertEqual([i for i in self.last if i["type"] == "message.received"], [])   # told once

    def band(self):
        self.taken()
        self.updater().check(apply=False)
        self.last = self.taken()
        got = [i for i in self.last if i["type"] == "update.checked"]
        return (got[-1]["data"].get("local_edits") or {}) if got else {}

    def test_a_merge_that_kept_the_release_says_nothing(self):
        self.release_guard()
        self.merge()
        self.assertNotIn("lost", self.band())
        self.assertEqual([i for i in self.last if i["type"] == "message.received"], [])

    def test_an_answer_takes_the_line_off(self):
        from modules.update import shadow, update
        self.release_guard()
        self.merge("-s", "ours")
        self.band()
        ident = shadow.open_signals(self.state)[0]["id"]
        shadow.answer(self.state, ident, "ok")
        self.assertEqual(update.shadow_pass(self.state, self.updater().cfg, repo=self.mine), None)
        self.assertTrue(os.path.exists(os.path.join(self.state, update.NOW_FLAG)))
        self.assertEqual(shadow.open_signals(self.state), [])
        self.assertNotIn("lost", self.band())
        self.assertEqual(shadow.report(self.state)["lost"], {"open": 0, "signals": 1, "ok": 1, "restored": 0})
        self.assertFalse(os.listdir(os.path.join(self.state, shadow.ANSWERS)))

    def test_a_removed_line_the_merge_kept_is_a_signal_unless_the_release_still_has_it(self):
        from modules.update import shadow
        self.write(self.author, "core/tools.py", "t = 1\nu = 2\nv = 3\nkeep_me = 1\nkeep_me = 1\n")
        self.release("twice")
        self.g(self.mine, "pull", "-q", "--no-rebase", "origin", "main")
        self.band()
        self.write(self.author, "core/tools.py", "t = 1\nv = 3\nkeep_me = 1\n")   # two removing hunks
        self.release("take lines out")
        self.merge("-s", "ours")                                            # the hub keeps both
        self.band()
        # "u = 2" is gone from the release: kept by the merge, a signal; "keep_me = 1" is still in the release: none
        self.assertEqual([s["lines"] for s in shadow.open_signals(self.state)], [["-u = 2"]])

    def test_an_answer_needs_a_known_word_and_id(self):
        from modules.update import shadow
        for word, ident in (("maybe", "0123abcd"), ("ok", "../x"), ("ok", "0123ABCD"), ("ok", "")):
            with self.assertRaises(ValueError):
                shadow.answer(self.state, ident, word)

    def test_an_update_the_updater_merged_is_checked_and_a_clean_one_opens_nothing(self):
        from modules.update import shadow
        new = self.release_guard()
        self.assertEqual(self.updater().check(), "applied")
        self.assertEqual(shadow.report(self.state)["lost"]["signals"], 0)
        with open(os.path.join(self.state, shadow.LOST), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["base"], new)

    def test_a_conflict_kept_with_the_hubs_side_is_a_signal(self):
        self.write(self.mine, "core/tools.py", "mine = 1\nt = 1\nu = 2\nv = 3\nmore = 1\n")
        self.commit(self.mine)
        self.release_guard()
        self.assertEqual(self.updater().check(), "conflict")                # the same place: the hub-dev merges by hand
        self.merge("-X", "ours")
        self.assertNotIn("guard_check", self.read(self.mine, "core/tools.py"))
        self.assertEqual([i["path"] for i in self.band()["lost"]["items"]], ["core/tools.py"])


class Command(unittest.TestCase):
    """`hub update lost`: Hub Developer, its executor, the hub or the owner answers a signal the check opened."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="shadow-cli-")
        self.addCleanup(platforms.current.rmtree, self.state)
        os.makedirs(os.path.join(self.state, "spool"))
        os.makedirs(os.path.join(self.state, "update"))
        with open(os.path.join(self.state, "update", "lost.json"), "w") as f:
            json.dump({"base": "a" * 40, "open": [{"id": "0123abcd", "path": "core/tools.py", "hunk": "@@ -1 +1,2 @@",
                                                   "release": "a" * 40, "lines": ["guard_check = True"]}]}, f)

    def hub(self, *args, session=None):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = {k: v for k, v in os.environ.items() if k not in ("HUB_SESSION", "HUB_EXTENSION", "HUB_MODULE")}
        env.update(HUB_STATE_DIR=self.state, PYTHONUTF8="1", **({"HUB_SESSION": session} if session else {}))
        return subprocess.run([sys.executable, os.path.join(repo, "bin", "hub"), "update", "lost", *args],
                              env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)

    def answered(self):
        folder = os.path.join(self.state, "update", "lost-answers")
        out = {}
        for n in os.listdir(folder) if os.path.isdir(folder) else []:
            with open(os.path.join(folder, n), encoding="utf-8") as f:
                out[n] = f.read()
        return out

    def test_the_list_and_an_answer(self):
        r = self.hub()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("0123abcd  core/tools.py", r.stdout)
        self.assertIn("guard_check = True", r.stdout)
        r = self.hub("restored", "0123abcd", session="hub-dev")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.answered(), {"0123abcd": "restored"})

    def test_an_unknown_id_or_word_is_refused(self):
        for args in (("ok", "ffffffff"), ("maybe", "0123abcd"), ("ok",), ("ok", "0123abcd", "more")):
            self.assertNotEqual(self.hub(*args).returncode, 0, args)
        self.assertEqual(self.answered(), {})

    def test_another_session_is_refused(self):
        r = self.hub("ok", "0123abcd", session="maps")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("hub-dev", r.stderr)
        self.assertEqual(self.answered(), {})


class Shape(unittest.TestCase):
    """The fold keeps only what the page can show as plain lines: test names, signal ids, one-line paths."""

    def shape(self, **v):
        from core import reducer
        return reducer.local_edits_shape({"count": 1, "files": [], **v})

    def test_good_lines_are_kept(self):
        got = self.shape(shadow={"failed": ["tests.test_a.T.test_x"], "count": 3, "version": "v1.9.0"},
                         lost={"items": [{"id": "0123abcd", "path": "core/tools.py"}], "count": 2})
        self.assertEqual(got["shadow"], {"failed": ["tests.test_a.T.test_x"], "count": 3, "version": "v1.9.0"})
        self.assertEqual(got["lost"], {"items": [{"id": "0123abcd", "path": "core/tools.py"}], "count": 2})

    def test_anything_else_is_dropped(self):
        for bad in ({"failed": ["rm -rf /"]}, {"failed": "tests.test_a"}, {"failed": []}, None, "x"):
            self.assertNotIn("shadow", self.shape(shadow=bad), bad)
        for bad in ({"items": [{"id": "x", "path": "a"}]}, {"items": [{"id": "0123abcd", "path": "a\nb"}]},
                    {"items": "x"}, {"items": [{"id": "0123abcd"}]}, None):
            self.assertNotIn("lost", self.shape(lost=bad), bad)
        got = self.shape(shadow={"failed": ["tests.test_a.T.x", "tests.test_b.T.y"], "count": 1},
                         lost={"items": [{"id": "0123abcd", "path": "a"}], "count": True})
        self.assertEqual((got["shadow"]["count"], got["lost"]["count"]), (2, 1))   # never fewer than named


class Stats(unittest.TestCase):
    """The statistics report carries the numbers of the checks next to the files of the hub's own edits."""

    @cut.needs("stats")
    def test_the_counts_go_into_local(self):
        from modules.stats import stats
        from modules.update import shadow
        state = tempfile.mkdtemp(prefix="shadow-stats-")
        self.addCleanup(platforms.current.rmtree, state)
        os.makedirs(os.path.join(state, "update"))
        with open(os.path.join(state, shadow.SHADOW), "w") as f:
            json.dump({"last": {"release": "b" * 40, "result": "red", "modules": 2, "failed": ["tests.test_a.T.x"],
                                "seconds": 41}}, f)
        self.assertEqual(stats.guard_counts(state)["shadow"],
                         {"result": "red", "modules": 2, "red": 1, "release": "bbbbbbb", "seconds": 41})
        self.assertEqual(stats.guard_counts(os.path.join(state, "nothing")), {})
        from unittest import mock
        with mock.patch.object(stats, "local_changes", lambda repo, cfg: {"base": "abc1234", "ahead": 0, "files": []}), \
                mock.patch.object(stats, "github", lambda: None), mock.patch.object(stats, "guarded", lambda: True):
            local = stats.identity(state, cfg={})["local"]
        self.assertEqual((local["base"], local["shadow"]["red"]), ("abc1234", 1))


if __name__ == "__main__":
    unittest.main()
