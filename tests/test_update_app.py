"""The app (installer spec, S3, variant A): the hub's code is a git clone of the shared repo on a release tag, the app
shell keeps the environment (Python, git…) and a PACKAGED file with format "app". Updates go through modules/update,
with what an app needs on top: the schedule only looks (the owner installs with a button; owner decision 2026-10-06:
no auto-updates), no self-check run on the person's machine, "no access" when GitHub is not signed in, a newer app
environment (RUNTIME_MIN), a rollback to the previous release and an automatic one when the new release does not come
up. Real git repos: a shared (bare) one, the author's copy and the app's clone."""
import tests  # noqa: F401 — keeps the machine's settings out
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import platforms
from core import catalog, packaged
from tests.fakes import stands

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOUR = 3600


def g(cwd, *a):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


class Repos(unittest.TestCase):
    """A shared repo with releases v1.0.0… and the app's clone on v1.0.0 (as the seed of the app shell). The built repos
    come from the run's cache of stands (tests/fakes/stands.py): every step of a test's stand is built once per run."""
    kind = "plain"   # what a release is made of: the cache is keyed by it

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="update-app-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.state = os.path.join(self.root, "state")
        os.makedirs(os.path.join(self.state, "spool"))
        self.up = os.path.join(self.root, "up.git")
        self.author = os.path.join(self.root, "author")
        self.mine = os.path.join(self.root, "code")
        self.tags = {}
        self.chain = stands.Chain(self.root, "repos", self.kind, stands.source_of(os.path.join(REPO, "modules", "update", "trial.py")))

        def build():
            g(self.root, "init", "-q", "--bare", "-b", "main", self.up)
            g(self.root, "clone", "-q", self.up, self.author)
            self.make_release("v1.0.0", 1, None)
            g(self.root, "clone", "-q", "--branch", "v1.0.0", self.up, self.mine)   # detached on the tag, like the seed
            return {"tags": self.tags}
        self.tags = self.chain.step("start", ["up.git", "author", "code"], build)["tags"]

    def prepare(self):
        """What the author's copy gets before every release."""

    def release(self, tag, runtime=1, text=None):
        sha = self.chain.step(["release", tag, runtime, text, len(self.tags)], ["up.git", "author"],
                              lambda: {"sha": self.make_release(tag, runtime, text)})["sha"]
        self.tags[tag] = sha
        return sha

    def make_release(self, tag, runtime, text):
        self.prepare()
        os.makedirs(os.path.join(self.author, "core"), exist_ok=True)
        with open(os.path.join(self.author, "core", "version.py"), "w") as f:
            f.write(f'MIN_CLAUDE = "2.1.0"\nRUNTIME_MIN = {runtime}\n')
        with open(os.path.join(self.author, "CHANGES.md"), "w") as f:
            f.write(f"# Changes\n\n## 2026-11-0{len(self.tags) + 1}\n\n- {text or 'version ' + tag}\n")
        g(self.author, "add", "-A")
        g(self.author, "commit", "-qm", tag)
        g(self.author, "push", "-q", "origin", "main")
        g(self.author, "tag", "-a", "-m", tag, tag)
        g(self.author, "push", "-q", "origin", f"refs/tags/{tag}")
        self.tags[tag] = g(self.author, "rev-parse", "HEAD")
        return self.tags[tag]

    def cfg(self, runtime=1, **update):
        app = {"runtime": runtime, "download": "https://example.test/download"}
        return {"update": {"enabled": True, "remote": "origin", "branch": "main", "channel": "stable",
                           "app": app, **update}, "dev": {"name": "hub-dev"}}

    def updater(self, runtime=1, spawn=None, **update):
        from modules.update import update as mod
        self.spawned = []
        return mod.Updater(self.mine, self.state, self.cfg(runtime, **update), claude=lambda: None,
                           spawn=spawn or (lambda *a: self.spawned.append(a)))

    def head(self):
        return g(self.mine, "rev-parse", "HEAD")

    def events(self, kind=None):
        from core import spool
        out = []
        for path, item in spool.take(os.path.join(self.state, "spool")):
            os.unlink(path)
            out.append(item)
        return [e for e in out if kind is None or e["type"] == kind]

    def checked(self):
        return self.events("update.checked")[-1]["data"]

    def age(self, tag, hours):
        path = os.path.join(self.state, "update", "seen.json")
        with open(path) as f:
            seen = json.load(f)
        seen[tag]["seen"] = round(time.time() - hours * HOUR)
        with open(path, "w") as f:
            json.dump(seen, f)

    def install(self, tag):
        """The owner pressed "Update" with `tag` the newest release."""
        u = self.updater()
        self.assertEqual(u.check(manual=True), "applied")
        self.assertEqual(self.head(), self.tags[tag])
        return u


class Settings(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="hub-app-")
        self.addCleanup(shutil.rmtree, self.root, True)
        os.makedirs(os.path.join(self.root, "state"))

    def load(self, mark, user):
        cfgp = os.path.join(self.root, "hub.json")
        with open(cfgp, "w") as f:
            json.dump(user, f)
        path = os.path.join(self.root, "PACKAGED")
        if mark is not None:
            with open(path, "w") as f:
                json.dump(mark, f)
        with mock.patch.object(packaged, "MARK", path), mock.patch.dict(os.environ, {"HUB_CONFIG": cfgp}):
            return catalog.load_config(os.path.join(self.root, "state"))

    def test_an_app_updates_from_the_stable_channel_whatever_hub_json_says(self):
        cfg = self.load({"format": "app", "version": "1.7.0", "runtime": 2, "download": "https://example.test/d"},
                        {"update": {"enabled": False, "channel": "main"}})
        up = cfg["update"]
        self.assertEqual((up["enabled"], up["channel"]), (True, "stable"))
        self.assertEqual(up["app"], {"runtime": 2, "download": "https://example.test/d"})

    def test_an_older_package_keeps_updates_off(self):
        cfg = self.load({"format": "deb", "version": "1.0"}, {"update": {"enabled": True}})
        self.assertFalse(cfg["update"]["enabled"])
        self.assertNotIn("app", cfg["update"])

    def test_a_hub_from_git_cannot_claim_to_be_an_app(self):
        # update.app skips the self-check: a git hub with personal commits must not get that from its hub.json
        cfg = self.load(None, {"update": {"app": {"runtime": 9}}})
        self.assertNotIn("app", cfg["update"])
        self.assertTrue(cfg["update"]["enabled"])

    def test_a_download_address_that_is_not_https_is_dropped(self):
        cfg = self.load({"format": "app", "runtime": 1, "download": "javascript:alert(1)"}, {})
        self.assertEqual(cfg["update"]["app"], {"runtime": 1})


class Schedule(Repos):
    def test_the_schedule_only_looks_and_shows_a_release_after_its_delay(self):
        from modules.update import update as mod
        self.assertEqual(mod.plan("install", False, False, app=True), (False, False))   # the hourly pass of an app looks
        self.assertEqual(mod.plan("install", True, False, app=True), (True, True))      # "Update" / hub update
        self.assertEqual(mod.plan("install", False, False, app=False), (True, False))   # a hub from git: as before
        # "Check for updates" is the owner asking: the newest at once, a returned release too — also when the hourly
        # pass took the flag along (stand 01.11: after a rollback "Check" said "latest version")
        self.assertEqual(mod.plan("check", False, True, app=True), (False, True))
        self.assertEqual(mod.plan("install", False, True, app=True), (False, True))
        self.assertEqual(mod.plan("check", False, True, app=False), (False, False))     # git: install_after stays
        # a hub on a code layer installs on every pass, in the background (the owner's decision of 2026-11-09, m1917410)
        self.assertEqual(mod.plan("install", False, False, app=True, layered=True), (True, False))
        # its "Check" installs as the schedule does, not as the owner's install: a returned release stays returned
        self.assertEqual(mod.plan("check", False, True, app=True, layered=True), (True, False))
        self.assertEqual(mod.plan(None, False, False, app=True, layered=True), (False, False))
        self.release("v1.1.0")
        self.assertEqual(self.updater().check(apply=False), "current")   # in its delay: no banner yet
        self.assertNotIn("install_after", self.checked())                # an app never installs by itself
        self.age("v1.1.0", 25)
        self.assertEqual(self.updater().check(apply=False), "available")
        c = self.checked()
        self.assertEqual((c["version"], c.get("app")), ("v1.1.0", True))
        self.assertNotIn("install_after", c)
        self.assertEqual(self.head(), self.tags["v1.0.0"])                # looked, did not install

    def test_check_for_updates_shows_the_newest_at_once(self):
        self.release("v1.1.0")
        self.assertEqual(self.updater().check(apply=False, manual=True), "available")
        self.assertEqual(self.checked()["version"], "v1.1.0")

    def test_update_installs_at_once_without_a_self_check(self):
        from modules.update import update as mod
        self.release("v1.1.0")
        with mock.patch.object(mod, "run_tests", side_effect=AssertionError("self-check ran")), \
                mock.patch.object(mod, "machine_turn", side_effect=AssertionError("waited for the test lock")):
            self.install("v1.1.0")
        self.assertEqual(self.events("update.applied")[-1]["data"]["version"], "v1.1.0")


class Access(Repos):
    def test_without_github_sign_in_the_app_says_so(self):
        from modules.update import update as mod
        said = "git fetch -q origin main: fatal: could not read Username for 'https://github.com': terminal prompts disabled"
        real = mod.Updater.git

        def git(self, *a, net=False, **kw):
            if net:
                raise RuntimeError(said)
            return real(self, *a, net=net, **kw)
        with mock.patch.object(mod.Updater, "git", git):
            self.assertEqual(self.updater().check(apply=False), "no-access")
            c = self.checked()
            self.assertEqual((c["result"], c["git_kind"]), ("no-access", "auth"))
            # a hub from git: the same failure stays an error (the page names the fix for a terminal)
            u = mod.Updater(self.mine, self.state, {"update": {"enabled": True}},
                            claude=lambda: None)
            with self.assertRaises(RuntimeError):
                u.check(apply=False)

    def test_a_network_failure_is_not_no_access(self):
        from modules.update import update as mod
        real = mod.Updater.git

        def git(self, *a, net=False, **kw):
            if net:
                raise RuntimeError("git fetch: fatal: unable to access: Could not resolve host: github.com")
            return real(self, *a, net=net, **kw)
        with mock.patch.object(mod.Updater, "git", git), self.assertRaises(RuntimeError):
            self.updater().check(apply=False)


class Runtime(Repos):
    def test_a_release_that_needs_a_newer_app_waits_with_a_download_link(self):
        self.release("v1.1.0", runtime=2)
        before = self.head()
        self.assertEqual(self.updater(runtime=1).check(manual=True), "needs-runtime")
        c = self.checked()
        self.assertEqual((c["need_runtime"], c["have_runtime"], c["download"]), (2, 1, "https://example.test/download"))
        self.assertEqual(self.head(), before)
        self.assertEqual(self.updater(runtime=2).check(manual=True), "applied")

    def test_a_shell_that_does_not_name_its_environment_counts_as_the_oldest(self):
        self.release("v1.1.0", runtime=2)
        u = self.updater()
        u.app = {}
        self.assertEqual(u.check(manual=True), "needs-runtime")

    def test_a_hub_from_git_has_no_runtime_gate(self):
        from modules.update import update as mod
        self.release("v1.1.0", runtime=5)
        u = mod.Updater(self.mine, self.state, {"update": {"enabled": True}},
                        tests=lambda repo: (True, "OK"), claude=lambda: None)
        self.assertEqual(u.check(manual=True), "applied")


class Rollback(Repos):
    def test_rollback_returns_the_previous_release_and_the_schedule_does_not_offer_it_again(self):
        self.release("v1.1.0")
        self.install("v1.1.0")
        self.release("v1.2.0")
        self.install("v1.2.0")
        self.events()
        self.assertEqual(self.updater().roll_back(), "rolled-back")
        self.assertEqual(self.head(), self.tags["v1.1.0"])
        failed = self.events("update.failed")
        self.assertEqual(len(failed), 1)
        d = failed[0]["data"]
        self.assertEqual((d["why"], d["version"], d["by"]), ("rolled-back", "v1.2.0", "owner"))
        self.age("v1.2.0", 48)
        self.assertEqual(self.updater().check(apply=False), "current")             # the rolled-back release: not on schedule
        self.assertEqual(self.checked().get("skipped"), "v1.2.0")
        self.assertEqual(self.updater().check(apply=False, manual=True), "available")   # "Check" shows it
        self.release("v1.3.0")
        self.assertEqual(self.updater().check(apply=False), "current")             # v1.3.0 in its delay, v1.2.0 skipped
        self.age("v1.3.0", 48)
        self.assertEqual(self.updater().check(apply=False), "available")           # a newer one is offered
        self.assertEqual(self.updater().roll_back(), "rolled-back")                # v1.1.0 → v1.0.0 (the seed)
        self.assertEqual(self.head(), self.tags["v1.0.0"])
        self.assertEqual(self.updater().roll_back(), "no-previous")
        self.assertEqual(self.head(), self.tags["v1.0.0"])

    def test_the_previous_release_is_named_in_the_check(self):
        self.release("v1.1.0")
        self.install("v1.1.0")
        self.assertEqual(self.checked().get("previous"), "v1.0.0")   # the install's own check: the history after it
        self.release("v1.2.0")
        self.install("v1.2.0")
        self.assertEqual(self.checked().get("previous"), "v1.1.0")
        self.updater().roll_back()
        c = self.checked()
        self.assertEqual((c["result"], c["version"], c.get("previous")), ("rolled-back", "v1.1.0", "v1.0.0"))

    def test_a_second_return_of_the_same_release_goes_no_further(self):
        self.release("v1.1.0")
        self.install("v1.1.0")
        self.release("v1.2.0")
        self.install("v1.2.0")
        self.assertEqual(self.updater().roll_back("v1.1.0"), "rolled-back")
        self.events()
        self.assertEqual(self.updater().roll_back("v1.1.0"), "moved")      # another tab, the old button: not to v1.0.0
        self.assertEqual(self.head(), self.tags["v1.1.0"])
        said = [e["data"]["text"] for e in self.events() if e["type"] == "owner.notify"]
        self.assertEqual(len(said), 1)                                      # said, not silent
        self.assertIn("v1.0.0", said[0])

    def test_the_install_is_finished_before_the_restart_is_asked(self):
        # a restart that kills the update process right after its request must not let recover undo the install
        from modules.update import update as mod
        self.release("v1.1.0")
        u = self.updater()
        seen = []
        real = mod.Updater.put

        def put(self, type, entity=None, data=None):
            if type == "hub.restart_requested":
                seen.append(os.path.exists(os.path.join(self.state_dir, mod.APPLYING)))
            return real(self, type, entity, data)
        with mock.patch.object(mod.Updater, "put", put):
            self.assertEqual(u.check(manual=True), "applied")
        self.assertEqual(seen, [False, False])
        self.updater().recover()
        self.assertEqual(self.head(), self.tags["v1.1.0"])

    def test_rollback_restarts_the_hub_on_the_previous_code(self):
        self.release("v1.1.0")
        self.install("v1.1.0")
        self.events()
        self.updater().roll_back()
        asks = [e["data"] for e in self.events("hub.restart_requested")]
        self.assertTrue(any(a["parts"] == [] for a in asks))

    def test_rollback_refuses_with_edits_in_the_code(self):
        self.release("v1.1.0")
        self.install("v1.1.0")
        with open(os.path.join(self.mine, "CHANGES.md"), "a") as f:
            f.write("edit\n")
        self.assertEqual(self.updater().roll_back(), "dirty")
        self.assertEqual(self.head(), self.tags["v1.1.0"])

    def test_a_hub_from_git_has_no_rollback(self):
        from modules.update import update as mod
        u = mod.Updater(self.mine, self.state, {"update": {"enabled": True}}, claude=lambda: None)
        self.assertEqual(u.roll_back(), "not-app")


class Trial(Repos):
    """After an app update the previous code's watchdog waits for the hub to come up on the new release."""

    def test_an_app_update_leaves_a_trial_and_starts_the_watchdog(self):
        self.release("v1.1.0")
        self.install("v1.1.0")
        with open(os.path.join(self.state, "update", "trial.json")) as f:
            trial = json.load(f)
        self.assertEqual((trial["from"], trial["to"], trial["status"]), (self.tags["v1.0.0"], self.tags["v1.1.0"], "waiting"))
        self.assertEqual((trial["version"], trial["previous"]), ("v1.1.0", "v1.0.0"))
        self.assertGreaterEqual(trial["window"], 180)
        self.assertEqual(len(self.spawned), 1)
        script = self.spawned[0][0]
        self.assertTrue(script.startswith(os.path.join(self.state, "update")))   # a copy of the previous code's watchdog
        self.assertTrue(os.path.isfile(script))

    def test_a_watchdog_rollback_is_written_up_by_the_previous_code(self):
        self.release("v1.1.0")
        self.install("v1.1.0")
        from modules.update import trial as watch
        with open(os.path.join(self.state, "update", "trial.json")) as f:
            t = json.load(f)
        self.assertEqual(watch.roll_tree(self.mine, self.state, t, "timeout"), "rolled-back")
        self.assertEqual(self.head(), self.tags["v1.0.0"])
        self.events()
        self.updater().recover()   # the previous code came up: its update process writes it up
        d = self.events("update.failed")[-1]["data"]
        self.assertEqual((d["why"], d["version"], d["by"]), ("rolled-back", "v1.1.0", "timeout"))
        self.assertFalse(os.path.exists(os.path.join(self.state, "update", "rolledback.json")))
        self.age("v1.1.0", 48)
        self.assertEqual(self.updater().check(apply=False), "current")   # not offered again on schedule

    def test_a_watchdog_rollback_keeps_edits_of_the_code(self):
        self.release("v1.1.0")
        self.install("v1.1.0")
        from modules.update import trial as watch
        with open(os.path.join(self.state, "update", "trial.json")) as f:
            t = json.load(f)
        with open(os.path.join(self.mine, "CHANGES.md"), "a") as f:
            f.write("my note\n")
        self.assertEqual(watch.roll_tree(self.mine, self.state, t, "timeout"), "rolled-back")
        self.assertEqual(self.head(), self.tags["v1.0.0"])
        self.assertIn("my note", g(self.mine, "stash", "show", "-p", "stash@{0}"))

    def roll_setup(self):
        self.release("v1.1.0")
        self.install("v1.1.0")
        from modules.update import trial as watch
        with open(os.path.join(self.state, "update", "trial.json")) as f:
            t = json.load(f)
        return watch, t, os.path.join(self.state, "update", "rolledback.json")

    def test_a_write_up_that_does_not_get_through_leaves_the_code_and_the_trial_as_they_were(self):
        watch, t, ours = self.roll_setup()
        with mock.patch.object(watch, "put", side_effect=PermissionError(5, "Access is denied")), \
                self.assertRaises(PermissionError):
            watch.roll_tree(self.mine, self.state, t, "timeout")
        self.assertEqual(self.head(), self.tags["v1.1.0"])              # not reset without its write-up
        self.assertFalse(os.path.exists(ours))
        self.assertEqual(watch.load(self.state)["status"], "waiting")   # a resumed watchdog rolls back again
        self.assertEqual(watch.roll_tree(self.mine, self.state, t, "timeout"), "rolled-back")
        self.assertEqual(self.head(), self.tags["v1.0.0"])

    def test_a_reset_that_git_refuses_leaves_no_write_up(self):
        watch, t, ours = self.roll_setup()
        real = watch.git
        refuse = lambda repo, *a: subprocess.CompletedProcess(a, 1, "", "refused") if a[0] == "reset" else real(repo, *a)
        with mock.patch.object(watch, "git", refuse):
            self.assertEqual(watch.roll_tree(self.mine, self.state, t, "timeout"), "failed")
        self.assertFalse(os.path.exists(ours))
        self.assertEqual(watch.load(self.state)["status"], "failed")

    def test_a_rollback_whose_closing_write_failed_is_finished_not_taken_for_moved(self):
        watch, t, ours = self.roll_setup()
        again = dict(t)                                                 # what the watchdog still holds: "waiting"
        self.assertEqual(watch.roll_tree(self.mine, self.state, t, "timeout"), "rolled-back")
        self.assertEqual(watch.roll_tree(self.mine, self.state, again, "timeout"), "rolled-back")
        self.assertTrue(os.path.exists(ours))                           # the previous code's update process writes it up
        self.assertEqual(self.head(), self.tags["v1.0.0"])

    def test_a_reader_of_the_note_does_not_kill_the_watchdog_when_the_reset_is_refused(self):
        # launch.py or a starting hub may hold the note open: taking it back must not raise, the trial is "failed" anyway
        watch, t, ours = self.roll_setup()
        real = watch.git
        refuse = lambda repo, *a: subprocess.CompletedProcess(a, 1, "", "refused") if a[0] == "reset" else real(repo, *a)

        def busy(name):
            raise PermissionError(5, "Access is denied")
        with mock.patch.object(watch, "git", refuse), mock.patch.object(os, "unlink", busy), \
                mock.patch.object(watch, "WINDOWS", False):
            self.assertEqual(watch.roll_tree(self.mine, self.state, t, "timeout"), "failed")
        self.assertEqual(watch.load(self.state)["status"], "failed")

    def test_a_note_of_an_earlier_trial_is_not_the_note_of_this_one(self):
        # a note nobody wrote up (another release's) and the code put back on `from` by hand: this trial is "moved"
        watch, t, ours = self.roll_setup()
        watch.put(ours, {**self.note_of(t), "to": "f" * 40})
        watch.git(self.mine, "reset", "-q", "--hard", t["from"])
        self.assertEqual(watch.roll_tree(self.mine, self.state, t, "timeout"), "moved")
        self.assertEqual(watch.load(self.state)["status"], "moved")

    def test_a_note_of_this_trial_with_the_code_back_on_from_closes_it(self):
        watch, t, ours = self.roll_setup()
        watch.put(ours, self.note_of(t))
        watch.git(self.mine, "reset", "-q", "--hard", t["from"])
        self.assertEqual(watch.roll_tree(self.mine, self.state, t, "timeout"), "rolled-back")

    def test_a_failed_write_up_of_the_watchdog_is_not_repeated(self):
        self.release("v1.1.0")
        self.install("v1.1.0")
        from modules.update import trial as watch
        from modules.update import update as mod
        with open(os.path.join(self.state, "update", "trial.json")) as f:
            t = json.load(f)
        watch.roll_tree(self.mine, self.state, t, "timeout")   # with the file closed: Windows does not replace an open one
        with mock.patch.object(mod.Updater, "_rolled_back", side_effect=OSError("disk")), self.assertRaises(OSError):
            self.updater().recover()
        self.assertFalse(os.path.exists(os.path.join(self.state, "update", "rolledback.json")))

    def note_of(self, t):
        return {**{k: t.get(k) for k in ("from", "to", "version", "previous")}, "why": "timeout", "ts": 1}

    def test_a_note_of_a_reset_that_did_not_happen_is_dropped_not_written_up(self):
        # the watchdog was killed (or the machine rebooted) between its note and the end of `git reset --hard`:
        # the hub is on the new release, nothing was rolled back
        watch, t, ours = self.roll_setup()
        watch.put(ours, self.note_of(t))
        u = self.updater()
        with mock.patch.object(type(u), "_rolled_back") as rolled:
            u.recover()
        rolled.assert_not_called()
        self.assertFalse(os.path.exists(ours))
        self.assertEqual(self.events("update.failed"), [])
        self.assertEqual(watch.load(self.state)["status"], "waiting")   # the watchdog goes on with the trial
        self.assertEqual(self.spawned[-1][-1], "--resume")

    def test_a_note_is_left_to_a_watchdog_that_is_in_the_middle_of_the_reset(self):
        watch, t, ours = self.roll_setup()
        watch.put(ours, self.note_of(t))
        u = self.updater()
        with mock.patch.object(type(u), "_watchdog_alive", return_value=True):
            u.recover()
        self.assertTrue(os.path.exists(ours))

    def test_a_rollback_found_done_by_the_next_start_closes_the_trial_too(self):
        # the watchdog reset the code and died before it closed the trial; the hub came up on the previous code before
        # the next watchdog pass: the write-up closes the trial, no second watchdog waits 180 s to call it "moved"
        watch, t, ours = self.roll_setup()
        watch.put(ours, self.note_of(t))
        watch.git(self.mine, "reset", "-q", "--hard", t["from"])
        self.assertEqual(watch.load(self.state)["status"], "waiting")
        u = self.updater()
        u.recover()
        d = self.events("update.failed")[-1]["data"]
        self.assertEqual((d["why"], d["by"]), ("rolled-back", "timeout"))
        self.assertEqual(watch.load(self.state)["status"], "rolled-back")
        self.assertEqual(self.spawned, [])
        self.assertFalse(os.path.exists(ours))

    def test_a_rollback_the_watchdog_closed_is_written_up_though_the_code_moved_on_since(self):
        # reset done, trial closed "rolled-back", and the code then moved (a hand checkout, another install): the head is
        # not `from` any more, but the rollback did happen
        watch, t, ours = self.roll_setup()
        watch.put(ours, self.note_of(t))
        watch.save(self.state, {**t, "status": "rolled-back"})
        u = self.updater()
        with mock.patch.object(type(u), "_rolled_back") as rolled:
            u.recover()
        rolled.assert_called_once()
        self.assertEqual(rolled.call_args.args[0], t["to"])
        self.assertFalse(os.path.exists(ours))

    def test_a_start_without_a_note_does_not_ask_git_anything(self):
        # the head matters only to a note: git refusing at a start must not fail the recovery of an app with no note
        u = self.updater()
        with mock.patch.object(type(u), "git", side_effect=RuntimeError("git: refused")):
            u.recover()

    def test_a_note_that_cannot_be_taken_back_is_not_written_up_and_does_not_fail_the_start(self):
        # a reader holds the note: writing up now would repeat on every start, the next start tries again
        watch, t, ours = self.roll_setup()
        watch.put(ours, self.note_of(t))
        watch.git(self.mine, "reset", "-q", "--hard", t["from"])
        u = self.updater()
        real = os.unlink

        def held(name, *a, **k):
            if os.path.basename(name) == "rolledback.json":
                raise PermissionError(5, "Access is denied")
            return real(name, *a, **k)
        with mock.patch.object(type(u), "_rolled_back") as rolled, mock.patch.object(os, "unlink", held):
            u.recover()
        rolled.assert_not_called()
        self.assertTrue(os.path.exists(ours))
        with mock.patch.object(type(u), "_rolled_back") as rolled:
            u.recover()
        rolled.assert_called_once()
        self.assertFalse(os.path.exists(ours))

    def test_the_owners_return_closes_the_trial_and_takes_the_watchdogs_note_with_it(self):
        # "Return" while the watchdog's note is there (it wrote it, then the trial stayed "waiting"): the owner's return
        # is the write-up; the note left behind would be written up again at the next start, as a timeout
        watch, t, ours = self.roll_setup()
        watch.put(ours, self.note_of(t))
        self.assertEqual(self.updater().roll_back(), "rolled-back")
        self.assertEqual(watch.load(self.state)["status"], "rolled-back")
        self.assertFalse(os.path.exists(ours))
        self.events()
        self.updater().recover()
        self.assertEqual([e["data"]["by"] for e in self.events("update.failed")], [])

    def test_an_install_killed_in_the_middle_is_undone(self):
        self.release("v1.1.0")
        u = self.updater()
        with mock.patch.object(type(u), "_after_ff", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                u.check(manual=True)
        self.assertTrue(os.path.exists(os.path.join(self.state, "update", "applying.json")))
        self.updater().recover()
        self.assertEqual(self.head(), self.tags["v1.0.0"])
        self.assertFalse(os.path.exists(os.path.join(self.state, "update", "applying.json")))
        self.install("v1.1.0")

    def test_an_undone_install_keeps_edits_of_the_code(self):
        # the undo resets the code: an edit in it (the owner's own, or a half-written checkout) goes into the code's
        # stash, not away
        self.release("v1.1.0")
        u = self.updater()
        with mock.patch.object(type(u), "_after_ff", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                u.check(manual=True)
        with open(os.path.join(self.mine, "CHANGES.md"), "a") as f:
            f.write("my note\n")
        self.updater().recover()
        self.assertEqual(self.head(), self.tags["v1.0.0"])
        self.assertIn("my note", g(self.mine, "stash", "show", "-p", "stash@{0}"))


class Watchdog(unittest.TestCase):
    """modules/update/trial.py: when the trial clock runs and what it decides (pure)."""

    def decide(self, **kw):
        from modules.update import trial
        t = {"started": 1000, "window": 180, "to": "abcdef1234", "status": "waiting", **kw.pop("trial", {})}
        return trial.decide(t, **kw)

    def test_the_hub_came_up_on_the_new_release(self):
        about = {"core_started": 1010, "web_started": 1005, "code": {"running": "abcdef1"}}
        self.assertEqual(self.decide(about=about, core_gone=1001, now=1020), "ok")

    def test_the_old_page_still_answers_and_the_core_has_not_restarted_yet(self):
        # the restart waits for busy sessions: the clock has not started, however long it takes
        about = {"core_started": 900, "web_started": 1001, "code": {"running": "abcdef1"}}
        self.assertEqual(self.decide(about=about, core_gone=None, now=1000 + 5 * HOUR), "wait")

    def test_the_new_release_does_not_come_up_in_time(self):
        self.assertEqual(self.decide(about=None, core_gone=1100, now=1100 + 179), "wait")
        self.assertEqual(self.decide(about=None, core_gone=1100, now=1100 + 181), "rollback")
        old = {"core_started": 1150, "web_started": 1150, "code": {"running": "0000000"}}   # up, but not on the new code
        self.assertEqual(self.decide(about=old, core_gone=1100, now=1100 + 181), "rollback")

    def test_a_resumed_watchdog_waits_while_the_core_of_the_install_still_runs(self):
        # the update process restarted alone while the full restart waits for busy sessions: no clock yet
        from modules.update import trial
        d = tempfile.mkdtemp(prefix="trial-")
        self.addCleanup(platforms.current.rmtree, d)
        up = os.path.join(d, "hub-up.json")
        with open(up, "w") as f:
            json.dump({"core": os.getpid()}, f)
        self.assertTrue(trial.still_old_core({"core_pid": os.getpid(), "up_file": up}))
        with open(up, "w") as f:
            json.dump({"core": os.getpid() + 1}, f)                     # hub-up names another core: it restarted
        self.assertFalse(trial.still_old_core({"core_pid": os.getpid(), "up_file": up}))

    def test_a_trial_that_is_over_is_left_alone(self):
        self.assertEqual(self.decide(trial={"status": "ok"}, about=None, core_gone=1100, now=99999), "done")

    def test_the_trial_file_is_replaced_while_a_reader_holds_it(self):
        # on Windows a reader (the page, a command) makes os.replace fail with PermissionError until it closes the file
        from modules.update import trial
        d = tempfile.mkdtemp(prefix="trial-")
        self.addCleanup(platforms.current.rmtree, d)
        os.makedirs(os.path.join(d, "update"))
        real, calls = os.replace, []

        def busy(src, dst):
            calls.append(dst)
            if len(calls) < 3:
                raise PermissionError(5, "Access is denied")
            return real(src, dst)
        with mock.patch.object(os, "replace", busy), mock.patch.object(trial, "WINDOWS", True):
            trial.save(d, {"status": "waiting"})
        self.assertEqual(len(calls), 3)
        self.assertEqual(trial.load(d), {"status": "waiting"})
        self.assertEqual(os.listdir(os.path.join(d, "update")), ["trial.json"])

    def test_a_replace_that_never_gets_through_leaves_no_temp_file_and_raises(self):
        from modules.update import trial
        d = tempfile.mkdtemp(prefix="trial-")
        self.addCleanup(platforms.current.rmtree, d)
        os.makedirs(os.path.join(d, "update"))
        dst = os.path.join(d, "update", "trial.json")
        with mock.patch.object(os, "replace", side_effect=PermissionError(5, "Access is denied")) as rep, \
                mock.patch.object(trial, "WINDOWS", True), self.assertRaises(PermissionError):
            trial.put(dst, {"status": "waiting"}, patience=0.05)
        self.assertGreater(rep.call_count, 1)                           # it did wait
        self.assertEqual(os.listdir(os.path.join(d, "update")), [])

    def test_a_refusal_outside_windows_is_not_waited_for(self):
        from modules.update import trial
        d = tempfile.mkdtemp(prefix="trial-")
        self.addCleanup(platforms.current.rmtree, d)
        with mock.patch.object(os, "replace", side_effect=PermissionError(13, "Permission denied")) as rep, \
                mock.patch.object(trial, "WINDOWS", False), self.assertRaises(PermissionError):
            trial.replace(os.path.join(d, "a"), os.path.join(d, "b"))
        self.assertEqual(rep.call_count, 1)

    def test_the_restart_request_does_not_take_the_cores_temp_name(self):
        # the core writes <flag>.tmp itself (core/daemon.py): two writers on one name lose or break each other's request
        from modules.update import trial
        d = tempfile.mkdtemp(prefix="trial-")
        self.addCleanup(platforms.current.rmtree, d)
        flag = os.path.join(d, "restart")
        core_tmp = flag + ".tmp"
        with open(core_tmp, "w") as f:
            f.write('{"parts": ["web"]}')
        seen = []

        def taken(_):                                                   # hub-up reads the request and removes the flag
            with open(flag) as f:
                seen.append(json.load(f))
            os.unlink(flag)
        self.assertEqual(trial.restart_hub({"restart_flag": flag}, sleep=taken), "taken")
        self.assertEqual(seen, [{"parts": [], "why": "update watchdog"}])
        with open(core_tmp) as f:
            self.assertEqual(f.read(), '{"parts": ["web"]}')

    def test_a_file_that_a_reader_holds_is_removed_when_it_lets_go(self):
        from modules.update import trial
        d = tempfile.mkdtemp(prefix="trial-")
        self.addCleanup(platforms.current.rmtree, d)
        f = os.path.join(d, "note")
        with open(f, "w"):
            pass
        real, calls = os.unlink, []

        def busy(name):
            calls.append(name)
            if len(calls) < 3:
                raise PermissionError(5, "Access is denied")
            return real(name)
        with mock.patch.object(os, "unlink", busy), mock.patch.object(trial, "WINDOWS", True):
            trial.remove(f)
        self.assertFalse(os.path.exists(f))
        trial.remove(f)                                                 # already gone: nothing to do

    def test_a_file_that_stays_held_is_given_up_on_without_raising(self):
        from modules.update import trial
        with mock.patch.object(os, "unlink", side_effect=PermissionError(5, "Access is denied")) as un, \
                mock.patch.object(trial, "WINDOWS", True):
            trial.remove("note", patience=0.05)
        self.assertGreater(un.call_count, 1)

    def run_to_rollback(self, rolled, **patch):
        from modules.update import trial
        d = tempfile.mkdtemp(prefix="trial-")
        self.addCleanup(platforms.current.rmtree, d)
        os.makedirs(os.path.join(d, "update"))
        trial.save(d, {"status": "waiting", "repo": d, "to": "a" * 40, "core_pid": 0, "port": 1, "window": 1, "started": 0})
        self.state = d
        clock = [1000.0]
        with mock.patch.object(trial, "roll_tree", side_effect=rolled) as roll, \
                mock.patch.object(trial, "restart_hub", return_value="taken") as restart:
            got = trial.run(d, now=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s),
                            about=lambda port: None)
        return got, roll, restart

    def test_a_rollback_that_cannot_be_written_is_tried_again_on_the_next_pass(self):
        got, roll, restart = self.run_to_rollback([PermissionError(5, "Access is denied"), "rolled-back"])
        self.assertEqual((got, roll.call_count, restart.call_count), ("rolled-back", 2, 1))

    def test_a_rollback_that_never_gets_through_ends_the_trial_as_failed(self):
        # a missing repo folder or no git in PATH does not pass by itself: the watchdog does not spin for ever
        import contextlib
        import io
        from modules.update import trial
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            # finite, so that the old code (it spins) ends red instead of hanging
            got, roll, restart = self.run_to_rollback([FileNotFoundError(2, "git is not found")] * 200 + ["rolled-back"])
        self.assertEqual(got, "failed")
        self.assertEqual(roll.call_count, trial.TAKE_FLAG)
        restart.assert_not_called()
        saved = trial.load(self.state)
        self.assertEqual(saved["status"], "failed")
        self.assertIn("git is not found", saved["error"])
        self.assertEqual(out.getvalue().count("trying again"), 1)       # one line at the first retry, not one per pass
        self.assertEqual(out.getvalue().count("giving up"), 1)          # and one when it gives up, with the last error
        self.assertIn("git is not found", out.getvalue().split("giving up")[1])

    def test_a_rollback_that_was_done_but_could_not_be_closed_still_restarts_the_hub(self):
        # roll_tree has reset the code and marked the trial "rolled-back" when its closing save fails, and keeps failing
        # (the trial file is held): the code is on the previous release already, so the hub must be asked to come up on it
        # — ending as "failed" would leave the hub running the bad release
        import contextlib
        import io
        from modules.update import trial
        real = trial.save
        saves = []

        def save(state, t):
            saves.append(1)
            if len(saves) > 2:   # the test's own and the run's first one go through
                raise PermissionError(5, "Access is denied")
            return real(state, t)

        def rolled(repo, state, t, why):
            t["status"] = "rolled-back"
            raise PermissionError(5, "Access is denied")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(trial, "save", save):
            got, roll, restart = self.run_to_rollback(rolled)
        self.assertEqual((got, roll.call_count, restart.call_count), ("rolled-back", trial.TAKE_FLAG, 1))
        self.assertIn("giving up", out.getvalue())

    def test_a_restart_request_that_cannot_be_written_falls_back_to_stopping_hub_up(self):
        from modules.update import trial
        d = tempfile.mkdtemp(prefix="trial-")
        self.addCleanup(platforms.current.rmtree, d)
        with mock.patch.object(trial, "put", side_effect=PermissionError(5, "Access is denied")):
            self.assertEqual(trial.restart_hub({"restart_flag": os.path.join(d, "restart"), "up_file": os.path.join(d, "none")},
                                               sleep=lambda s: None), "no-hub-up")

    def test_what_depends_on_the_os_follows_the_one_switch(self):
        # the tests (and the PR's backoff) flip trial.WINDOWS: alive() and the stop of hub-up follow it, not os.name
        from modules.update import trial
        def no_run(*a, **k):
            raise AssertionError("tasklist/taskkill outside Windows")
        with mock.patch.object(trial, "WINDOWS", False), mock.patch.object(subprocess, "run", no_run), \
                mock.patch.object(os, "kill") as kill:
            self.assertTrue(trial.alive(4242))
            kill.assert_called_once_with(4242, 0)
            d = tempfile.mkdtemp(prefix="trial-")
            self.addCleanup(platforms.current.rmtree, d)
            up = os.path.join(d, "hub-up.json")
            with open(up, "w") as f:
                json.dump({"pid": 4242, "started": 7}, f)
            with mock.patch.object(trial, "alive", side_effect=[True, False]), \
                    mock.patch.object(trial, "is_hub_up", return_value=True):
                self.assertEqual(trial.restart_hub({"up_file": up}, sleep=lambda s: None), "stopped")

    def test_the_watchdog_runs_alone_on_the_stdlib(self):
        # it is copied out of the code before the update and runs while the new code may be broken: no hub imports
        with open(os.path.join(REPO, "modules", "update", "trial.py")) as f:
            src = f.read()
        self.assertNotRegex(src, r"(?m)^\s*(from|import)\s+(core|modules|platforms|hosts)\b")


class RestartHub(unittest.TestCase):
    """trial.restart_hub stops only the hub-up that wrote hub-up.json (the audit of 03.11: the file outlives a crash, the
    number goes to another program, and the watchdog killed it — on Windows with its whole tree). The victims are the
    test's own processes; one carries "hub-up" in its command line."""

    def setUp(self):
        from modules.update import trial
        self.trial = trial
        from platforms import current
        self.dir = tempfile.mkdtemp(prefix="trial-restart-")
        self.addCleanup(current.rmtree, self.dir)
        self.up = os.path.join(self.dir, "hub-up.json")

    def victim(self, *argv, ignore_term=False):
        code = "import signal, time\n" + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_term else "") + \
               "print('ready', flush=True)\ntime.sleep(60)"
        p = subprocess.Popen([sys.executable, "-c", code, *argv], stdout=subprocess.PIPE)
        self.addCleanup(p.stdout.close)
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        p.stdout.readline()
        # collected as soon as it exits, as hub-up's parent (the service) does: not a zombie that still answers signal 0
        threading.Thread(target=p.wait, daemon=True).start()
        return p

    def restart(self, up):
        with open(self.up, "w") as f:
            json.dump(up, f)
        return self.trial.restart_hub({"up_file": self.up}, sleep=lambda s: time.sleep(0.05))

    def test_the_hub_up_that_wrote_the_file_is_stopped(self):
        from platforms import current
        p = self.victim("hub-up")
        self.assertIn(self.restart({"pid": p.pid, "started": current.process_start(p.pid)}), ("stopped", "killed"))
        self.assertIsNotNone(p.wait(10))

    def test_the_watchdog_reads_the_start_time_as_hub_up_writes_it(self):
        # the watchdog is a copy of the previous release, hub-up writes with platforms: one value for one process
        from platforms import current
        p = self.victim("hub-up")
        self.assertIsNotNone(self.trial.started(p.pid))
        self.assertEqual(self.trial.started(p.pid), current.process_start(p.pid))
        self.assertEqual(self.trial.started(os.getpid()), current.process_start(os.getpid()))

    def test_a_number_that_went_to_another_program_is_left_alone(self):
        from platforms import current
        p = self.victim("hub-up")
        born = current.process_start(p.pid)
        later = born + 1 if isinstance(born, int) else born + " later"
        earlier = born - 1 if isinstance(born, int) else "earlier " + born
        for up in ({"pid": p.pid, "started": later},        # same number, the process started after hub-up wrote it
                   {"pid": p.pid, "started": earlier},
                   {"pid": p.pid},                          # a file of an earlier hub-up: cannot be checked
                   {"pid": p.pid, "started": None}):
            self.assertEqual(self.restart(up), "not-hub-up", up)
        self.assertEqual(self.restart({"pid": os.getpid(), "started": current.process_start(os.getpid())}),
                         "not-hub-up")
        self.assertIsNone(p.poll())

    @unittest.skipUnless(hasattr(signal, "SIGKILL"), "POSIX: the command line decides too; on Windows the start time (to 100 ns) names the process")
    def test_the_right_start_time_of_a_process_that_is_not_hub_up_is_left_alone(self):
        from platforms import current
        other = self.victim("server")
        self.assertEqual(self.restart({"pid": other.pid, "started": current.process_start(other.pid)}), "not-hub-up")
        self.assertIsNone(other.poll())

    @unittest.skipUnless(hasattr(signal, "SIGKILL"), "signals: macOS and Linux")
    def test_a_foreign_process_of_another_user_does_not_break_the_watchdog(self):
        from platforms import current
        p = self.victim("hub-up")
        with mock.patch.object(os, "kill", side_effect=PermissionError):
            self.assertEqual(self.restart({"pid": p.pid, "started": current.process_start(p.pid)}), "not-hub-up")

    @unittest.skipUnless(hasattr(signal, "SIGKILL"), "signals: macOS and Linux")
    def test_no_hard_kill_once_the_number_names_another_process(self):
        # SIGTERM, 30 s, SIGKILL: the start time is checked again before SIGKILL
        from platforms import current
        p = self.victim("hub-up", ignore_term=True)
        born = current.process_start(p.pid)
        times = iter([born, "another"])
        with mock.patch.object(self.trial, "started", side_effect=lambda pid: next(times)):
            self.assertEqual(self.restart({"pid": p.pid, "started": born}), "not-hub-up")
        self.assertIsNone(p.poll())   # SIGTERM ignored, no SIGKILL to the "other" process
        # review: ps fails on the second look — not checkable, so no SIGKILL either (and not reported as stopped)
        lines = iter(["python hub-up", ""])
        with mock.patch.object(self.trial, "command", side_effect=lambda pid: next(lines)):
            self.assertEqual(self.restart({"pid": p.pid, "started": born}), "not-hub-up")
        self.assertIsNone(p.poll())

    def test_on_windows_the_handle_it_checked_is_the_one_it_stops(self):
        T = 133_000_000_000_000_000

        class K32:
            killed = []

            def OpenProcess(self, access, inherit, pid):
                return ("h", pid, T)

            def GetProcessTimes(self, handle, created, *rest):
                created._obj.dwHighDateTime, created._obj.dwLowDateTime = handle[2] >> 32, handle[2] & 0xFFFFFFFF
                return True

            def TerminateProcess(self, handle, code):
                self.killed.append(handle[1])
                return True

            def CloseHandle(self, handle):
                return True
        k32 = K32()
        self.assertFalse(self.trial.stop_nt(5120, T + 1, k32=k32))   # the number is another process's now
        self.assertEqual(k32.killed, [])
        self.assertTrue(self.trial.stop_nt(5120, T, k32=k32))
        self.assertEqual(k32.killed, [5120])


class Launch(unittest.TestCase):
    """tools/package/launch.py: what the app shell does at every start before hub-up (S5–S7 call it)."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="hub-launch-")
        self.addCleanup(shutil.rmtree, self.root, True)
        sys.path.insert(0, os.path.join(REPO, "tools", "package"))
        self.addCleanup(sys.path.remove, os.path.join(REPO, "tools", "package"))
        import launch
        self.launch = launch
        self.seed = os.path.join(self.root, "app", "code-seed")
        self.mark = os.path.join(self.root, "app", "PACKAGED")
        stands.cached(["launch", stands.source_of(os.path.join(REPO, "modules", "update", "trial.py"))], self.root,
                      ["up.git", "w", "app"], self.build_stand)
        self.state = os.path.join(self.root, "state")

    def build_stand(self):
        up = os.path.join(self.root, "up.git")
        g(self.root, "init", "-q", "--bare", "-b", "main", up)
        work = os.path.join(self.root, "w")
        g(self.root, "clone", "-q", up, work)
        with open(os.path.join(work, "a.py"), "w") as f:
            f.write("a = 1\n")
        with open(os.path.join(work, ".gitignore"), "w") as f:
            f.write("__pycache__/\n")
        os.makedirs(os.path.join(work, "modules", "update"))
        shutil.copyfile(os.path.join(REPO, "modules", "update", "trial.py"),
                        os.path.join(work, "modules", "update", "trial.py"))   # the watchdog of the release on disk
        g(work, "add", "-A")
        g(work, "commit", "-qm", "v1")
        g(work, "tag", "-a", "-m", "v1.0.0", "v1.0.0")
        g(work, "push", "-q", "origin", "main", "v1.0.0")
        g(self.root, "clone", "-q", "--depth", "1", "--branch", "v1.0.0", "file://" + up, self.seed)
        g(self.seed, "remote", "set-url", "origin", "https://example.test/core.git")
        with open(self.mark, "w") as f:
            json.dump({"format": "app", "runtime": 1}, f)

    def test_a_mark_is_written_while_a_reader_holds_it_and_a_failed_one_leaves_no_temp_file(self):
        # on Windows a reader makes os.replace fail with PermissionError until it closes the file
        dst = os.path.join(self.state, "update", "trial.json")
        real, calls = os.replace, []

        def busy(src, to):
            calls.append(to)
            if len(calls) < 3:
                raise PermissionError(5, "Access is denied")
            return real(src, to)
        with mock.patch.object(os, "replace", busy), mock.patch.object(self.launch, "WINDOWS", True):
            self.launch._write(dst, {"status": "waiting"})
        self.assertEqual(len(calls), 3)
        with open(dst) as f:
            self.assertEqual(json.load(f), {"status": "waiting"})
        with mock.patch.object(os, "replace", side_effect=PermissionError(5, "Access is denied")), \
                mock.patch.object(self.launch, "WINDOWS", True), self.assertRaises(PermissionError):
            self.launch._write(dst, {"status": "ok"}, patience=0.05)
        self.assertEqual(os.listdir(os.path.dirname(dst)), ["trial.json"])   # no <name>.<pid>.tmp left behind

    def test_what_depends_on_the_os_follows_the_one_switch(self):
        # launch.WINDOWS is the switch of the retry; alive() and the detached start follow it, not os.name
        def no_run(*a, **k):
            raise AssertionError("tasklist outside Windows")
        with mock.patch.object(self.launch, "WINDOWS", False), mock.patch.object(subprocess, "run", no_run), \
                mock.patch.object(os, "kill") as kill:
            self.assertTrue(self.launch.alive(4242))
            kill.assert_called_once_with(4242, 0)
        for windows in (False, True):   # (not a process of a systemd unit: that one starts it through systemd-run)
            with self.subTest(windows=windows), mock.patch.object(self.launch, "WINDOWS", windows), \
                    mock.patch.dict(os.environ, {"INVOCATION_ID": ""}), mock.patch.object(subprocess, "Popen") as popen, \
                    mock.patch.object(self.launch, "_outside", return_value=False) as outside:
                self.launch.detached(["x"])
                self.assertEqual("creationflags" in popen.call_args.kwargs, windows)
                self.assertEqual("start_new_session" in popen.call_args.kwargs, not windows)
                self.assertEqual(outside.called, windows)   # Windows: WMI first, outside the task's job (A4)
        with mock.patch.object(self.launch, "WINDOWS", True), mock.patch.object(subprocess, "Popen") as popen, \
                mock.patch.object(self.launch, "_outside", return_value=True):
            self.launch.detached(["x"])
            popen.assert_not_called()

    def test_a_job_without_the_right_to_break_away_still_starts_the_detached_one(self):
        # a launcher inside a job that refuses CREATE_BREAKAWAY_FROM_JOB (a scheduler task, runas on Windows — the
        # stand 07.11): the watchdog and the trial start without the flag, as platforms.windows.popen_detached
        calls = []

        def popen(cmd, **kw):
            calls.append(kw["creationflags"])
            if kw["creationflags"] & 0x01000000:
                raise PermissionError(5, "Access is denied")
            return "started"
        with mock.patch.object(self.launch, "WINDOWS", True), mock.patch.object(subprocess, "Popen", popen), \
                mock.patch.object(self.launch, "_outside", return_value=False):   # WMI did not start it: the fallback
            self.assertEqual(self.launch.detached(["x"]), "started")
        self.assertEqual([bool(f & 0x01000000) for f in calls], [True, False])
        self.assertTrue(all(f & 0x00000008 for f in calls))   # DETACHED_PROCESS both times

    def test_the_first_start_makes_the_code_a_clone_of_the_seed(self):
        code = self.launch.ensure_code(self.seed, self.mark, self.state)
        self.assertEqual(code, os.path.join(self.state, "code"))
        self.assertEqual(g(code, "remote", "get-url", "origin"), "https://example.test/core.git")
        self.assertEqual(g(code, "describe", "--tags"), "v1.0.0")
        with open(os.path.join(code, "PACKAGED")) as f:
            self.assertEqual(json.load(f)["format"], "app")
        self.assertEqual(g(code, "status", "--porcelain"), "")               # PACKAGED is not a change of the code
        with open(os.path.join(code, "a.py"), "w") as f:
            f.write("a = 2\n")                                                 # an update moved the code
        self.launch.ensure_code(self.seed, self.mark, self.state)
        with open(os.path.join(code, "a.py")) as f:
            self.assertEqual(f.read(), "a = 2\n")                             # a later start does not replace it

    def test_an_interrupted_first_copy_is_done_again(self):
        os.makedirs(os.path.join(self.state, "code.part", "junk"))
        code = self.launch.ensure_code(self.seed, self.mark, self.state)
        self.assertEqual(g(code, "describe", "--tags"), "v1.0.0")
        self.assertFalse(os.path.exists(os.path.join(self.state, "code.part")))

    def test_a_seed_owned_by_another_user_is_cloned_all_the_same(self):
        # the .deb puts the shell into /opt as root: git refused the clone "dubious ownership" (Docker check of S4)
        with mock.patch.dict(os.environ, {"GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"}):
            code = self.launch.ensure_code(self.seed, self.mark, self.state)
        self.assertEqual(g(code, "remote", "get-url", "origin"), "https://example.test/core.git")

    def test_the_first_start_takes_the_bytecode_the_factory_compiled(self):
        # build.py compiles the seed in checked-hash mode: valid after the clone, so the first start compiles nothing
        subprocess.run([sys.executable, "-m", "compileall", "-q", "--invalidation-mode", "checked-hash", self.seed],
                       check=True, capture_output=True)
        code = self.launch.ensure_code(self.seed, self.mark, self.state)
        pyc = os.path.join(code, "__pycache__")
        self.assertEqual(os.listdir(pyc), os.listdir(os.path.join(self.seed, "__pycache__")))
        before = {n: os.stat(os.path.join(pyc, n)).st_mtime_ns for n in os.listdir(pyc)}
        r = subprocess.run([sys.executable, "-c", "import a"], cwd=code, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual({n: os.stat(os.path.join(pyc, n)).st_mtime_ns for n in os.listdir(pyc)}, before)
        self.assertEqual(g(code, "status", "--porcelain"), "")

    def newer_seed(self, version="v1.1.0", runtime_min=None):
        """A later release of the shared repo and the seed a newer installer carries (a depth-1 clone on its tag)."""
        work = os.path.join(self.root, "w")
        with open(os.path.join(work, "a.py"), "w") as f:
            f.write(f"a = '{version}'\n")
        os.makedirs(os.path.join(work, "core"), exist_ok=True)
        with open(os.path.join(work, "core", "version.py"), "w") as f:
            f.write(f"RUNTIME_MIN = {runtime_min or 1}\n")
        g(work, "add", "-A")
        g(work, "commit", "-qm", version)
        g(work, "tag", "-a", "-m", version, version)
        g(work, "push", "-q", "origin", "main", version)
        seed = os.path.join(self.root, "app-" + version, "code-seed")
        g(self.root, "clone", "-q", "--depth", "1", "--branch", version, "file://" + os.path.join(self.root, "up.git"), seed)
        g(seed, "remote", "set-url", "origin", "https://example.test/core.git")
        return seed

    def update_file(self, name):
        with open(os.path.join(self.state, "update", name)) as f:
            return json.load(f)

    def test_a_newer_seed_moves_the_code_the_way_the_update_does(self):
        code = self.launch.ensure_code(self.seed, self.mark, self.state)
        old = g(code, "rev-parse", "HEAD")
        seed = self.newer_seed()
        self.assertEqual(self.launch.ensure_code(seed, self.mark, self.state), code)
        new = g(seed, "rev-parse", "HEAD")
        self.assertEqual(g(code, "rev-parse", "HEAD"), new)
        with open(os.path.join(code, "a.py")) as f:
            self.assertEqual(f.read(), "a = 'v1.1.0'\n")
        self.assertEqual(g(code, "status", "--porcelain"), "")
        self.assertEqual(g(code, "remote", "get-url", "origin"), "https://example.test/core.git")
        self.assertTrue(os.path.isfile(os.path.join(code, "PACKAGED")))
        from core import version
        self.assertEqual(version.release(code), "v1.1.0")                     # "About the hub" names the new release
        # what the update's _applying/_install/_trial leave: the installed release, the one to go back to, the watchdog
        self.assertEqual(self.update_file("installed"), {"commit": new, "version": "v1.1.0"})
        self.assertEqual(self.update_file("history.json"), [{"commit": old, "version": "v1.0.0"}])
        self.assertFalse(os.path.exists(os.path.join(self.state, "update", "applying.json")))
        self.assertTrue(os.path.exists(os.path.join(self.state, "migrate.pending")))   # hub migrate --auto on the new code
        self.assertFalse(os.path.exists(os.path.join(self.state, "restart")))           # no hub was running: nothing to restart
        trial = self.update_file("trial.json")
        self.assertEqual((trial["from"], trial["to"], trial["status"], trial["version"], trial["previous"], trial["repo"]),
                         (old, new, "waiting", "v1.1.0", "v1.0.0", code))
        self.assertEqual(trial["restart_flag"], os.path.join(self.state, "restart"))
        self.assertEqual(trial["up_file"], os.path.join(self.state, "hub-up.json"))
        script = os.path.join(self.state, "update", "trial-run.py")
        with open(script) as f, open(os.path.join(REPO, "modules", "update", "trial.py")) as want:
            self.assertEqual(f.read(), want.read())
        ran = []                                                               # the launcher's next step starts it
        self.assertTrue(self.launch.resume_trial(self.state, sys.executable, run=ran.append))
        self.assertEqual(ran[0][1:3], [script, self.state])

    def test_the_code_of_the_seed_is_the_same_whichever_way_it_got_there(self):
        # a hub that started on v1.0.0 and was moved is the same tree as a first start on v1.1.0
        code = self.launch.ensure_code(self.seed, self.mark, self.state)
        seed = self.newer_seed()
        self.launch.ensure_code(seed, self.mark, self.state)
        fresh = self.launch.ensure_code(seed, self.mark, os.path.join(self.root, "state2"))
        self.assertEqual(g(code, "rev-parse", "HEAD^{tree}"), g(fresh, "rev-parse", "HEAD^{tree}"))

    def stays(self, seed, why):
        """The next start with this seed leaves the code where it is, writes no record of an update, does not fail."""
        code = os.path.join(self.state, "code")
        head, before = g(code, "rev-parse", "HEAD"), self.update_files()
        self.assertEqual(self.launch.ensure_code(seed, self.mark, self.state), code, why)
        self.assertEqual(g(code, "rev-parse", "HEAD"), head, why)
        self.assertEqual(self.update_files(), before, why)

    def update_files(self):
        out = {}
        for name in os.listdir(os.path.join(self.state, "update")) if os.path.isdir(
                os.path.join(self.state, "update")) else []:
            with open(os.path.join(self.state, "update", name), "rb") as f:
                out[name] = f.read()
        return out

    def test_the_same_or_an_older_seed_leaves_the_code(self):
        code = self.launch.ensure_code(self.seed, self.mark, self.state)
        self.stays(self.seed, "the same release")
        seed = self.newer_seed()
        self.launch.ensure_code(seed, self.mark, self.state)
        self.stays(self.seed, "an older installer over a newer code")
        self.assertEqual(g(code, "describe", "--tags"), "v1.1.0")

    def test_edits_or_commits_of_the_person_are_not_touched(self):
        code = self.launch.ensure_code(self.seed, self.mark, self.state)
        with open(os.path.join(code, "a.py"), "a") as f:
            f.write("# mine\n")
        seed = self.newer_seed()
        self.stays(seed, "an edit")
        g(code, "commit", "-qam", "mine")
        self.stays(seed, "a commit on top of the release")
        with open(os.path.join(code, "a.py")) as f:
            self.assertIn("# mine", f.read())

    def test_code_on_a_branch_is_somebodys_work(self):
        code = self.launch.ensure_code(self.seed, self.mark, self.state)
        g(code, "checkout", "-q", "-b", "mine")
        self.stays(self.newer_seed(), "a branch at the release's commit")

    def test_a_release_the_hub_was_taken_back_from_is_not_installed_again(self):
        code = self.launch.ensure_code(self.seed, self.mark, self.state)
        seed = self.newer_seed()
        os.makedirs(os.path.join(self.state, "update"))
        with open(os.path.join(self.state, "update", "skipped.json"), "w") as f:
            json.dump({g(seed, "rev-parse", "HEAD"): "v1.1.0"}, f)
        self.stays(seed, "rolled back earlier")
        self.assertEqual(g(code, "describe", "--tags"), "v1.0.0")

    def test_a_release_needing_a_newer_environment_than_the_shell_has_waits(self):
        self.launch.ensure_code(self.seed, self.mark, self.state)
        seed = self.newer_seed(runtime_min=2)                                   # the shell's PACKAGED says runtime 1
        self.stays(seed, "RUNTIME_MIN above the shell's runtime")
        with open(self.mark, "w") as f:
            json.dump({"format": "app", "runtime": 2}, f)
        self.launch.ensure_code(seed, self.mark, self.state)
        self.assertEqual(g(os.path.join(self.state, "code"), "describe", "--tags"), "v1.1.0")

    def test_an_update_in_the_middle_is_not_piled_on(self):
        self.launch.ensure_code(self.seed, self.mark, self.state)
        seed = self.newer_seed()
        os.makedirs(os.path.join(self.state, "update"))
        for name in ("applying.json", "trial.json"):
            with open(os.path.join(self.state, "update", name), "w") as f:
                json.dump({"before": "x", "status": "waiting"}, f)
            self.stays(seed, name)
            os.unlink(os.path.join(self.state, "update", name))

    def test_a_move_that_git_refuses_is_undone_and_does_not_fail_the_start(self):
        code = self.launch.ensure_code(self.seed, self.mark, self.state)
        seed = self.newer_seed()
        # a file of the person where the release adds its own: git will not overwrite it
        os.makedirs(os.path.join(code, "core"))
        with open(os.path.join(code, "core", "version.py"), "w") as f:
            f.write("mine\n")
        self.stays(seed, "git refuses")
        self.assertFalse(os.path.exists(os.path.join(self.state, "update", "applying.json")))
        self.assertFalse(os.path.exists(os.path.join(self.state, "update", "installed")))

    def test_a_release_the_watchdog_took_back_waits_for_the_updates_own_mark(self):
        # the watchdog's mark (rolledback.json, a trial that did not end ok) comes before the update's skipped.json,
        # which a running hub writes later: a start in between must not move the code onto the same release again
        self.launch.ensure_code(self.seed, self.mark, self.state)
        seed = self.newer_seed()
        sha = g(seed, "rev-parse", "HEAD")
        os.makedirs(os.path.join(self.state, "update"))
        with open(os.path.join(self.state, "update", "rolledback.json"), "w") as f:
            json.dump({"from": "a", "to": sha}, f)
        self.stays(seed, "rolledback.json")
        os.unlink(os.path.join(self.state, "update", "rolledback.json"))
        for status in ("rolled-back", "failed"):
            with open(os.path.join(self.state, "update", "trial.json"), "w") as f:
                json.dump({"to": sha, "status": status}, f)
            self.stays(seed, status)

    def test_a_failure_after_the_trial_is_written_leaves_neither_trial_nor_migration(self):
        code = self.launch.ensure_code(self.seed, self.mark, self.state)
        seed = self.newer_seed()
        with open(os.path.join(self.state, "hub-up.json"), "w") as f:
            json.dump({"core": os.getpid()}, f)
        with mock.patch.object(self.launch, "alive", side_effect=OSError("boom")):   # after the checkout, before the end
            self.assertEqual(self.launch.ensure_code(seed, self.mark, self.state), code)
        self.assertEqual(sorted(self.update_files()), ["trial-run.py"])     # the old watchdog's copy is all that is left
        self.assertFalse(os.path.exists(os.path.join(self.state, "update", "trial.json")))
        self.assertFalse(os.path.exists(os.path.join(self.state, "migrate.pending")))
        self.assertEqual(g(code, "describe", "--tags"), "v1.0.0")

    def test_the_helper_copies_of_the_updates_constants_match_it(self):
        # launch.py imports nothing of the hub, so it keeps its own copies: a drift would write records the update misreads
        from modules.update import update as mod
        self.assertEqual((self.launch.HISTORY_KEEP, self.launch.TRIAL_WINDOW), (mod.HISTORY_KEEP, mod.TRIAL_WINDOW))
        self.assertEqual(self.launch.UPDATE, mod.UPDATE_DIR)

    def test_the_hub_that_is_running_keeps_the_watchdog_waiting_for_its_core(self):
        self.launch.ensure_code(self.seed, self.mark, self.state)
        with open(os.path.join(self.state, "hub-up.json"), "w") as f:
            json.dump({"pid": os.getpid(), "core": os.getpid()}, f)
        self.launch.ensure_code(self.newer_seed(), self.mark, self.state)
        self.assertEqual(self.update_file("trial.json")["core_pid"], os.getpid())
        with open(os.path.join(self.state, "restart")) as f:                      # and is asked to restart on the new code
            self.assertEqual(json.load(f)["parts"], [])

    def test_a_trial_left_by_a_reboot_gets_its_watchdog_again(self):
        os.makedirs(os.path.join(self.state, "update"))
        with open(os.path.join(self.state, "update", "trial.json"), "w") as f:
            json.dump({"status": "waiting", "pid": 0}, f)
        script = os.path.join(self.state, "update", "trial-run.py")
        open(script, "w").close()
        ran = []
        self.assertTrue(self.launch.resume_trial(self.state, sys.executable, run=ran.append))
        self.assertEqual(ran[0][1:3], [script, self.state])
        with open(os.path.join(self.state, "update", "trial.json"), "w") as f:
            json.dump({"status": "ok"}, f)
        self.assertFalse(self.launch.resume_trial(self.state, sys.executable, run=ran.append))


class SeedThenUpdate(Repos):
    """The code a newer installer moved to its seed goes on with the update's button and "Return" as if the update had
    moved it: the seed is depth 1 (no history), the code is a clone of it."""

    kind = "watchdog"

    def prepare(self):
        os.makedirs(os.path.join(self.author, "modules", "update"), exist_ok=True)   # the watchdog of every release
        shutil.copyfile(os.path.join(REPO, "modules", "update", "trial.py"),
                        os.path.join(self.author, "modules", "update", "trial.py"))

    def seed_at(self, tag):
        seed = os.path.join(self.root, "seed-" + tag)
        g(self.root, "clone", "-q", "--depth", "1", "--branch", tag, "file://" + self.up, seed)
        g(seed, "remote", "set-url", "origin", self.up)
        return seed

    def read_update(self, name):
        with open(os.path.join(self.state, "update", name)) as f:
            return json.load(f)

    def test_the_update_and_the_return_button_work_on_the_moved_code(self):
        sys.path.insert(0, os.path.join(REPO, "tools", "package"))
        self.addCleanup(sys.path.remove, os.path.join(REPO, "tools", "package"))
        import launch
        mark = os.path.join(self.root, "PACKAGED")
        with open(mark, "w") as f:
            json.dump({"format": "app", "runtime": 1}, f)
        self.release("v1.1.0")
        shutil.rmtree(self.mine, ignore_errors=True)
        self.mine = launch.ensure_code(self.seed_at("v1.0.0"), mark, self.state)
        self.assertEqual(launch.ensure_code(self.seed_at("v1.1.0"), mark, self.state), self.mine)
        self.assertEqual(self.head(), self.tags["v1.1.0"])
        self.release("v1.2.0")
        theirs = {n: self.read_update(n) for n in ("trial.json", "history.json", "installed")}
        u = self.updater()
        self.assertEqual(u.check(manual=True), "applied")                       # a release later than the seed's
        for name, mine in theirs.items():   # the launcher's records have the shape of the update's own
            own = self.read_update(name)
            self.assertEqual(sorted(mine[0] if isinstance(mine, list) else mine), sorted(own[-1] if isinstance(own, list) else own), name)
        self.assertEqual(self.head(), self.tags["v1.2.0"])
        self.assertEqual(self.updater().check(manual=True), "current")
        self.assertEqual(self.updater().roll_back(), "rolled-back")
        self.assertEqual(self.head(), self.tags["v1.1.0"])


class Cli(unittest.TestCase):
    def run_hub(self, mark, *args):
        root = tempfile.mkdtemp(prefix="hub-app-cli-")
        self.addCleanup(shutil.rmtree, root, True)
        state = os.path.join(root, "state")
        os.makedirs(state)
        path = os.path.join(root, "PACKAGED")
        if mark is not None:
            with open(path, "w") as f:
                json.dump(mark, f)
        env = {k: v for k, v in os.environ.items() if not k.startswith("HUB_")}
        env.update(HUB_STATE_DIR=state, HUB_PACKAGED_MARK=path, HUB_LANG="en", HUB_CONFIG=os.path.join(root, "none.json"))
        r = subprocess.run([sys.executable, os.path.join(REPO, "bin", "hub"), "update", *args], env=env,
                           capture_output=True, text=True, timeout=60)
        return r, state

    def test_hub_update_rollback_in_an_app_asks_the_update_process(self):
        r, state = self.run_hub({"format": "app", "runtime": 1}, "rollback")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(os.path.join(state, "update.rollback")))

    def test_hub_update_in_an_app_installs(self):
        r, state = self.run_hub({"format": "app", "runtime": 1})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(os.path.join(state, "update.install")))

    def test_hub_update_rollback_names_a_release_or_nothing(self):
        r, state = self.run_hub({"format": "app", "runtime": 1}, "rollback", "v1.2.3")
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(state, "update.rollback")) as f:
            self.assertEqual(f.read(), "v1.2.3")
        r, state = self.run_hub({"format": "app", "runtime": 1}, "rollback", "1.2.3")
        self.assertNotEqual(r.returncode, 0)                                # a typo is said, not a silent no-op
        self.assertFalse(os.path.exists(os.path.join(state, "update.rollback")))

    def test_hub_update_rollback_on_a_code_layer_names_its_version(self):
        layer = {"format": "app", "layout": "layer", "runtime": 1}
        for version in ("1.2.3", "v1.2.3", "1.2.3-nightly.202611071200.abcdef123"):
            r, state = self.run_hub(layer, "rollback", version)
            self.assertEqual(r.returncode, 0, r.stderr)
            with open(os.path.join(state, "update.rollback")) as f:
                self.assertEqual(f.read(), version)
        r, state = self.run_hub(layer, "rollback", "../x")
        self.assertNotEqual(r.returncode, 0)

    def test_hub_update_rollback_is_only_for_an_app(self):
        r, state = self.run_hub(None, "rollback")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(state, "update.rollback")))


if __name__ == "__main__":
    unittest.main()
