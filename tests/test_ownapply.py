"""`hub app apply` on a hub from the one-line install (core/ownsrc.py build, modules/update/layer.py apply_own): the
release's own factory builds the hub's code of the working copy's `local` — <release>+local.<sha>, its manifest naming
the release it is over and the count of edits — and the update process leaves it for the person's restart under the
same watchdog and layers/next.json as a release. Did not come up: back on the clean release, no report to the
developers, the Hub Developer told why. "Return the released" takes the edits off; a new release brings the clean code
and one line that the edits are off and still in the working copy. Real layers, the real factory and the delivery
server of tests/test_app_update.py."""
import tests  # noqa: F401 - keeps the machine's settings out
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from core import appcode, ownsrc
from tests.test_app_update import HOUR, Stand, git, write

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Apply(Stand):
    def setUp(self):
        self.reported = []
        super().setUp()

    def release(self, tag, tree=None):
        """Every release carries its own factory and its core, as a real layer does (tools/ is not left out of a layer)."""
        own = {}
        for rel in ("tools/package/layer.py", "core/appcode.py"):   # the factory runs on the release's own core
            with open(os.path.join(REPO, *rel.split("/")), encoding="utf-8") as f:
                own[rel] = f.read()
        return super().release(tag, {**own, **(tree or {})})

    def code(self):
        return os.path.join(self.codes(), self.current())

    def copy(self, url=None):
        return ownsrc.make(self.state, self.code(), url=self.work if url is None else url, say=lambda line: None)["path"]

    def edit(self, copy, text="X = 'mine'\n", path="core/x.py"):
        write(os.path.join(copy, *path.split("/")), text)
        git(copy, "add", "-A")
        git(copy, "commit", "-qm", "mine")
        return git(copy, "rev-parse", "local")

    def updater(self, claude="9.9.9", **update):
        from modules.update import layer, report
        cfg = {"update": {"enabled": True, "channel": "stable", "app": {"runtime": 1, "layer": True},
                          "report_url": "https://abcdef0123456789@sentry.example.test/7", **update}, "dev": {"name": "hub-dev"}}
        return layer.LayerUpdater(os.path.join(self.codes(), self.current()), self.state, cfg, claude=lambda: claude,
                                  spawn=lambda *a: self.spawned.append(a), now=lambda: self.clock[0],
                                  reporter=report.Reporter(self.state, cfg, now=lambda: self.clock[0],
                                                           send=lambda url, body: self.reported.append(body) or "sent"))

    def build(self):
        return ownsrc.build(self.state, self.code(), python=sys.executable, say=lambda line: None)

    def apply(self):
        """hub app apply: the build, the request, the update process's pass that takes it -> its result."""
        got = self.build()
        ownsrc.request(self.state, got)
        return got, self.updater().apply_own()

    def applied(self):
        copy = self.copy()
        sha = self.edit(copy, "MINE = 1\n", "core/mine.py")   # a new release changes core/x.py: no conflict to merge
        got, result = self.apply()
        self.assertEqual(result, "applied")
        return copy, sha, got

    # --- the build

    def test_the_build_is_the_release_plus_the_edits_by_the_releases_own_factory(self):
        copy = self.copy()
        sha = self.edit(copy)
        got = self.build()
        name = f"1.2.3+local.{sha[:9]}-team"
        self.assertEqual((got["name"], got["edits"], got["release"]), (name, 1, "1.2.3"))
        folder = os.path.join(got["stage"], name)
        man = appcode.load(folder)
        self.assertEqual((man["version"], man["commit"], man["flavor"]), (f"1.2.3+local.{sha[:9]}", sha, "team"))
        base = appcode.load(self.code())
        self.assertEqual(man["own"], {"release": "1.2.3", "commit": base["commit"], "layer": "1.2.3-team", "edits": 1})
        with open(os.path.join(folder, "core", "x.py")) as f:
            self.assertEqual(f.read(), "X = 'mine'\n")
        self.assertEqual(appcode.diff(folder, man, full=True), [])
        self.assertFalse(any(f.startswith("tests/") for f in man["files"]))
        # not a layer name: the update's prune of old layers never takes a build that waits
        self.assertFalse(appcode.NAME.match(os.path.basename(got["stage"])))
        self.assertEqual(self.current(), "1.2.3-team")   # nothing of the running hub moved

    def test_the_build_of_a_copy_of_files_needs_no_access(self):
        copy = self.copy(url="")
        self.assertEqual(ownsrc.status(self.state)["kind"], "archive")
        sha = self.edit(copy)
        man = appcode.load(os.path.join(self.build()["stage"], f"1.2.3+local.{sha[:9]}-team"))
        self.assertEqual(man["own"]["edits"], 1)

    def test_what_is_not_committed_on_local_or_not_over_the_release_is_not_built(self):
        copy = self.copy()
        with self.assertRaises(ownsrc.Refused) as e:
            self.build()
        self.assertIn("no edits", str(e.exception))
        self.edit(copy)
        write(os.path.join(copy, "core", "x.py"), "X = 'more'\n")   # not committed: the hub would not run it
        with self.assertRaises(ownsrc.Refused) as e:
            self.build()
        self.assertIn("commit", str(e.exception))
        git(copy, "checkout", "-q", "--", ".")
        git(copy, "switch", "-q", "-c", "elsewhere", "release")   # HEAD elsewhere: `local` is what is built
        self.assertTrue(self.build()["name"].startswith("1.2.3+local."))

    def test_a_syntax_error_is_refused_before_anything_is_left_for_the_restart(self):
        copy = self.copy()
        self.edit(copy, "def broken(:\n")
        with self.assertRaises(ownsrc.Refused) as e:
            self.build()
        self.assertIn("x.py", str(e.exception))
        self.assertEqual([n for n in os.listdir(self.codes()) if n.startswith(ownsrc.STAGE)], [])
        self.assertFalse(self.exists("layers", "next.json"))

    # --- the update process leaves it for the restart

    def test_apply_leaves_the_build_for_the_persons_restart_under_the_watchdog(self):
        _, sha, got = self.applied()
        name = got["name"]
        self.assertTrue(os.path.isfile(os.path.join(self.codes(), name, appcode.MANIFEST)))
        self.assertFalse(os.path.exists(got["stage"]))
        self.assertTrue(os.path.isfile(os.path.join(self.codes(), "archives", f"code-{name}.tar.gz")))
        nxt = self.read("layers", "next.json")
        self.assertEqual((nxt["name"], nxt["why"], nxt["ask"]), (name, "own", True))
        trial = self.read("update", "trial.json")
        self.assertEqual((trial["status"], trial["to"], trial["layer"]["to"], trial["layer"]["from"]),
                         ("waiting", sha, name, "1.2.3-team"))
        self.assertEqual(len(self.spawned), 1)
        events = self.events()
        notice = [e for e in events if e["type"] == "owner.notify"][-1]["data"]
        self.assertEqual(notice["ready"], {"version": f"1.2.3+local.{sha[:9]}"})   # "Restart" on the page and Telegram
        self.assertIn("(1)", notice["text"])
        self.assertFalse([e for e in events if e["type"] == "hub.restart_requested"])   # the restart is the person's
        self.assertEqual(self.read("update", "own-apply.result.json")["result"], "applied")
        self.assertFalse(self.exists("update", "own-apply.json"))

    def test_on_the_build_the_hub_names_the_release_and_the_edits_and_takes_no_update_of_the_same_release(self):
        _, sha, got = self.applied()
        appcode.take_next(self.state)              # the restart
        self.assertEqual(self.current(), got["name"])
        u = self.updater()
        self.publish("v1.2.3")                      # the release it is over: nothing to install
        self.events()
        self.assertEqual(u.check(apply=True), "current")
        info = self.events("update.checked")[-1]["data"]
        self.assertEqual(info["own"], {"release": "1.2.3", "edits": 1})
        self.assertEqual(info.get("previous"), "1.2.3")   # "Return the released"
        st = ownsrc.status(self.state, self.code())
        self.assertFalse(st["behind"])              # the copy follows the release the build is over
        self.assertEqual(ownsrc.release_of(self.code())[0]["version"], "1.2.3")

    def test_a_build_that_does_not_come_up_goes_back_to_the_clean_release_with_no_report(self):
        from modules.update import trial
        _, sha, got = self.applied()
        appcode.take_next(self.state)
        self.assertEqual(trial.roll_layer(self.state, self.read("update", "trial.json"), "timeout"), "rolled-back")
        self.assertEqual(self.current(), "1.2.3-team")
        self.events()
        self.updater().recover()
        events = self.events()
        failed = [e for e in events if e["type"] == "update.failed"][-1]["data"]
        self.assertEqual((failed["why"], failed["by"]), ("rolled-back", "timeout"))
        told = [e["data"] for e in events if e["type"] == "message.received" and e["data"].get("to") == "hub-dev"]
        self.assertEqual(len(told), 1, events)
        self.assertIn(got["name"], told[0]["text"])
        self.assertIn(os.path.join(self.state, "logs"), told[0]["text"])
        self.assertEqual(self.reported, [])        # its own build is no failed release of the developers'
        self.assertIn(got["name"], self.read("layers", "failed.json"))

    def test_a_failed_release_is_still_reported_while_an_own_build_is_not(self):
        u = self.updater()
        self.assertEqual(u.reporter.failed("trial", "1.2.3+local.abcdef123", u, "rolled-back", "x"), "off")
        self.assertEqual(u.reporter.failed("trial", "1.3.0", u, "rolled-back", "x"), "sent")
        self.assertEqual(len(self.reported), 1)

    def test_return_the_released_takes_the_edits_off(self):
        copy, sha, got = self.applied()
        appcode.take_next(self.state)
        self.events()
        self.assertEqual(self.updater().roll_back("1.2.3"), "rolled-back")
        self.assertEqual(self.read("layers", "next.json")["name"], "1.2.3-team")
        line = [e for e in self.events() if e["type"] == "owner.notify"][-1]["data"]["text"]
        self.assertIn("working copy", line)
        self.assertEqual(git(copy, "rev-parse", "local"), sha)   # the edits stay where they are

    def test_return_before_the_restart_takes_the_build_back(self):
        self.applied()
        self.assertEqual(self.updater().roll_back(), "cancelled")
        self.assertFalse(self.exists("layers", "next.json"))
        self.assertEqual(self.read("update", "trial.json")["status"], "rolled-back")
        self.assertEqual(self.current(), "1.2.3-team")

    def test_a_second_build_over_the_first_goes_back_to_the_clean_release_too(self):
        copy, _, first = self.applied()
        appcode.take_next(self.state)
        sha = self.edit(copy, "Y = 2\n", "core/y.py")
        got, result = self.apply()
        self.assertEqual(result, "applied")
        self.assertEqual(got["edits"], 2)
        trial = self.read("update", "trial.json")
        self.assertEqual((trial["layer"]["from"], trial["layer"]["to"], trial["to"]), ("1.2.3-team", got["name"], sha))
        self.assertEqual(self.read("layers", "history.json")[-1]["name"], "1.2.3-team")
        appcode.take_next(self.state)
        self.updater().prune(self.current())
        self.assertTrue(os.path.isfile(os.path.join(self.codes(), "1.2.3-team", appcode.MANIFEST)))   # the way back

    def test_a_release_waiting_for_the_restart_is_not_pushed_aside(self):
        copy = self.copy()
        self.edit(copy)
        self.release("v1.3.0")
        self.publish("v1.3.0")
        self.updater().check(apply=True)            # installed in the background, waits for the restart
        got, result = self.apply()
        self.assertEqual(result, "waits")
        self.assertEqual(self.read("layers", "next.json")["name"], "1.3.0-team")
        self.assertFalse(os.path.exists(os.path.join(self.codes(), got["name"])))

    def test_a_new_release_brings_the_clean_code_and_one_line_that_the_edits_are_off(self):
        copy, sha, got = self.applied()
        appcode.take_next(self.state)
        self.events()
        self.release("v1.3.0")
        self.publish("v1.3.0")
        self.assertEqual(self.updater().check(apply=True), "applied")
        nxt = self.read("layers", "next.json")
        self.assertEqual((nxt["name"], nxt["why"]), ("1.3.0-team", "update"))
        trial = self.read("update", "trial.json")
        self.assertEqual(trial["layer"]["from"], got["name"])   # did not come up: back to what ran
        notice = [e for e in self.events() if e["type"] == "owner.notify"][-1]["data"]["text"]
        self.assertIn("(1)", notice)
        self.assertIn("working copy", notice)
        self.assertEqual(git(copy, "rev-parse", "local"), sha)   # nothing merged, nothing tested by itself
        appcode.take_next(self.state)
        # the copy is not over the new release: the Hub Developer merges it on the person's request, then applies
        with self.assertRaises(ownsrc.Refused) as e:
            self.build()
        self.assertIn("git merge release", str(e.exception))
        ownsrc.make(self.state, self.code(), say=lambda line: None)
        git(copy, "merge", "-q", "--no-edit", "release")
        self.assertEqual(self.build()["release"], "1.3.0")

    def test_a_request_the_update_process_cannot_read_is_dropped(self):
        os.makedirs(os.path.join(self.state, "update"), exist_ok=True)
        for rec in ({"stage": "../../x", "name": "1.2.3+local.a-team"}, {"stage": ".own-x", "name": "../x"}, [], "x"):
            write(os.path.join(self.state, "update", "own-apply.json"), json.dumps(rec))
            self.assertEqual(self.updater().apply_own(), "bad")
            self.assertFalse(self.exists("update", "own-apply.json"))
        self.assertFalse(self.exists("layers", "next.json"))

    def test_a_stage_left_by_a_build_cut_off_goes_after_a_day(self):
        stage = os.path.join(self.codes(), ownsrc.STAGE + "abc")
        os.makedirs(stage)
        os.utime(stage, (self.clock[0] - HOUR,) * 2)
        u = self.updater()
        u.prune(self.current())
        self.assertTrue(os.path.isdir(stage))       # a build may be running now
        os.utime(stage, (self.clock[0] - 2 * 24 * HOUR,) * 2)
        u.prune(self.current())
        self.assertFalse(os.path.isdir(stage))

    # --- the review's edges

    def test_the_next_build_is_made_by_the_releases_factory_not_by_an_edit_of_it_running_now(self):
        copy = self.copy()
        with open(os.path.join(copy, "tools", "package", "layer.py"), "a") as f:
            f.write("\nraise SystemExit('an edit of the factory')\n")
        self.edit(copy, "MINE = 1\n", "core/mine.py")
        self.assertEqual(self.apply()[1], "applied")
        appcode.take_next(self.state)
        self.edit(copy, "Y = 2\n", "core/y.py")
        got, result = self.apply()
        self.assertEqual((result, got["edits"]), ("applied", 2))

    def test_the_restart_onto_a_build_runs_the_migration_of_its_code(self):
        from tests.test_app_update import Restart
        self.applied()
        Restart.hub_up(self).next_layer()
        self.assertTrue(self.current().startswith("1.2.3+local."))
        self.assertTrue(self.exists("migrate.pending"))

    def test_the_page_has_restart_at_once_after_apply(self):
        _, sha, got = self.applied()
        check = [e for e in self.events() if e["type"] == "update.checked"][-1]["data"]
        self.assertEqual((check["result"], check["ask_restart"], check["version"]), ("applied", True, f"1.2.3+local.{sha[:9]}"))
        self.assertNotIn("previous", check)   # no "Return" to a release while the build waits: "Return" cancels it

    def test_a_new_release_that_does_not_come_up_over_a_build_says_the_edits_run_again(self):
        # the way back is what ran (the build with the edits): the line must not repeat "the edits are off"
        from modules.update import trial
        _, _, got = self.applied()
        appcode.take_next(self.state)
        self.release("v1.3.0")
        self.publish("v1.3.0")
        self.assertEqual(self.updater().check(apply=True), "applied")
        appcode.take_next(self.state)
        self.assertEqual(trial.roll_layer(self.state, self.read("update", "trial.json"), "timeout"), "rolled-back")
        self.assertEqual(self.current(), got["name"])
        self.events()
        self.updater().recover()
        line = [e for e in self.events() if e["type"] == "owner.notify"][-1]["data"]["text"]
        self.assertIn("1.3.0", line)
        self.assertIn("with your edits of the core (1)", line)
        self.assertNotIn("+local.", line)

    def test_a_new_release_over_a_build_still_waiting_says_the_edits_will_not_run(self):
        self.applied()
        self.events()
        self.release("v1.3.0")
        self.publish("v1.3.0")
        self.assertEqual(self.updater().check(apply=True), "applied")
        self.assertEqual(self.read("layers", "next.json")["name"], "1.3.0-team")
        notice = [e for e in self.events() if e["type"] == "owner.notify"][-1]["data"]["text"]
        self.assertIn("working copy", notice)

    def test_a_request_written_while_one_is_taken_is_not_deleted_unread(self):
        copy = self.copy()
        self.edit(copy)
        got = self.build()
        ownsrc.request(self.state, got)
        u = self.updater()
        read = u._read

        def meanwhile(name, kind):
            out = read(name, kind)
            if name.startswith(ownsrc.FLAG):
                write(os.path.join(self.state, ownsrc.FLAG), json.dumps({"stage": ".own-next", "name": "x"}))
            return out
        with mock.patch.object(u, "_read", meanwhile):
            self.assertEqual(u.apply_own(), "applied")
        self.assertEqual(self.read(ownsrc.FLAG)["stage"], ".own-next")

    def test_an_unexpected_failure_is_the_commands_answer_not_a_wait(self):
        copy = self.copy()
        self.edit(copy)
        got = self.build()
        ownsrc.request(self.state, got)
        u = self.updater()
        with mock.patch.object(u, "_stage", side_effect=KeyError("history")):
            self.assertEqual(u.apply_own(), "bad")
        self.assertIn("history", ownsrc.result(self.state, got["name"])["error"])

    def test_a_build_neither_running_nor_waiting_is_pruned(self):
        copy, _, first = self.applied()
        appcode.take_next(self.state)
        self.edit(copy, "Y = 2\n", "core/y.py")
        self.assertEqual(self.apply()[1], "applied")
        appcode.take_next(self.state)
        # an older build whose sha sorts above the running one's: still no release waiting to be installed
        stale = os.path.join(self.codes(), "1.2.3+local.fffffffff-team")
        write(os.path.join(stale, appcode.MANIFEST), json.dumps({"version": "1.2.3+local.fffffffff"}))
        self.updater().prune(self.current())
        self.assertFalse(os.path.exists(stale))
        self.assertFalse(os.path.exists(os.path.join(self.codes(), first["name"])))
        self.assertTrue(os.path.isfile(os.path.join(self.codes(), "1.2.3-team", appcode.MANIFEST)))

    def test_edits_that_need_a_newer_environment_are_not_left_for_the_restart(self):
        copy = self.copy()
        self.edit(copy, "RUNTIME_MIN = 5\n", "core/version.py")
        got, result = self.apply()
        self.assertEqual(result, "needs-runtime")
        self.assertFalse(os.path.exists(got["stage"]))
        self.assertFalse(self.exists("layers", "next.json"))

    def test_a_failed_check_on_a_build_is_no_report(self):
        self.applied()
        appcode.take_next(self.state)
        u = self.updater()
        u.info, u.stage = {"error": "the server's form"}, None
        u._report("error")
        self.assertEqual(self.reported, [])

    def test_a_request_nobody_took_is_withdrawn_with_its_build(self):
        copy = self.copy()
        self.edit(copy)
        got = self.build()
        ownsrc.request(self.state, got)
        self.assertTrue(ownsrc.withdraw(self.state))
        self.assertFalse(self.exists(ownsrc.FLAG))
        self.assertFalse(os.path.exists(got["stage"]))
        self.assertFalse(ownsrc.withdraw(self.state))   # taken (or gone) already: nothing to take back
        self.assertEqual(self.updater().apply_own(), "none")


class Shown(unittest.TestCase):
    """What the page and `hub version` say on a build of the person's edits."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hub-own-shown-")
        self.addCleanup(appcode.remove_tree, self.state)

    def test_about_the_hub_names_the_release_and_the_edits(self):
        from modules.web.server import Web
        man = {"version": "1.2.3+local.abcdef123", "commit": "b" * 40, "built": "2026-11-09T00:00:00Z",
               "own": {"release": "1.2.3", "commit": "a" * 40, "layer": "1.2.3-team", "edits": 2}}
        layer = os.path.join(self.state, "layers", "1.2.3+local.abcdef123-team")
        with mock.patch("core.appwatch.layer", lambda repo: layer), mock.patch("core.appwatch.manifest", lambda root: man):
            code = Web(self.state).code()
        self.assertEqual(code["own"], {"release": "1.2.3", "edits": 2})
        self.assertEqual(code["version"], "1.2.3+local.abcdef123")   # an older page names the build itself
        man.pop("own")
        with mock.patch("core.appwatch.layer", lambda repo: layer), mock.patch("core.appwatch.manifest", lambda root: man):
            self.assertNotIn("own", Web(self.state).code())

    def test_the_check_carries_the_build_to_the_page_and_nothing_else(self):
        from core import reducer
        for own, want in (({"release": "1.2.3", "edits": 2}, {"release": "1.2.3", "edits": 2}), ("abc", None),
                          ({"release": "1.2.3", "edits": True}, None), ({"release": None, "edits": 1}, None), (None, None)):
            st, _, why = reducer.reduce(reducer.empty(), {"seq": 1, "ts": 1, "type": "update.checked", "entity": None,
                                                          "data": {"result": "current", "own": own, "channel": "cli",
                                                                   "owner": True}})
            self.assertIsNone(why)
            self.assertEqual(st["update_check"].get("own"), want, own)

    def hub(self, *args, code=REPO, **env):
        base = {k: v for k, v in os.environ.items() if k not in ("HUB_SESSION", "HUB_EXTENSION", "HUB_MODULE")}
        return subprocess.run([sys.executable, os.path.join(code, "bin", "hub"), *args], capture_output=True, text=True,
                              timeout=60, env={**base, "HUB_STATE_DIR": self.state, "HUB_LANG": "en", **env})

    def test_hub_app_apply_is_the_owners_and_the_hub_developers_only(self):
        r = self.hub("app", "apply", HUB_SESSION="someone")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("owner's command", r.stdout + r.stderr)
        r = self.hub("app", "apply", HUB_SESSION="hub-dev")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)   # let through: this hub runs from git, not a layer
        self.assertIn("not a code layer", r.stderr)

    def test_hub_version_of_a_build_names_the_release_its_commit_and_the_edits(self):
        code = os.path.join(self.state, "layers", "1.9.0+local.abcdef123-team")
        shutil.copytree(REPO, code, ignore=shutil.ignore_patterns(".git", "tests", "docs", "evals", "__pycache__",
                                                                  "node_modules", "experiments", "media"))
        release = "a" * 40
        appcode.make(code, "1.9.0+local.abcdef123", "team", "b" * 40, issues="acme/hub-issues",
                     own={"release": "1.9.0", "commit": release, "layer": "1.9.0-team", "edits": 3})
        mark = os.path.join(self.state, "PACKAGED.mark")
        with open(mark, "w") as f:
            json.dump({"format": "app", "layout": "layer", "version": "1.9.0", "runtime": 1}, f)
        r = self.hub("version", code=code, HUB_PACKAGED_MARK=mark)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"1.9.0 · {release} + your edits of the core (3), the build {'b' * 40}", r.stdout)
        self.assertIn("https://github.com/acme/hub-issues/issues/new", r.stdout)


if __name__ == "__main__":
    unittest.main()
