"""Hub private modules (modules/update/private.py, module-extraction spec 4.2-4.5): the ensure step installs modules from
the modules repository by the pin in modules/private.json: `git ls-remote` with the machine's keys (no access: nothing
changes, a line to the owner), `hub module add` as the owner, hello; a module the owner removed is not installed again;
readiness of the shared code folder and the needs-module gate; waiting for startup with no built-in login. The fake
source is a real bare git repo with three modules; commands are the real bin/hub run against its own state folder."""
import tests  # noqa: F401 — hide the machine's settings
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock

import platforms
from core import catalog, spool
from tests.fakes.modstand import FAKE
from tests.fakes import cut

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB = os.path.join(REPO, "bin", "hub")
MODS = {"accounts": "profile", "voice-reply": "speech", "stats": "stats"}


def git(cwd, *args):
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "init.defaultBranch=main",
                        *args], cwd=cwd, capture_output=True, text=True)
    if r.returncode:
        raise AssertionError(f"git {' '.join(args)}: {r.stderr}")
    return r.stdout.strip()


def private_pin(f):
    from modules.update import private
    return private.load_pin(f.core)


class Fixture:
    """Modules repo (working copy and bare clone), a core tree with modules/private.json, hub folders."""

    def __init__(self, case):
        self.case = case
        self.root = tempfile.mkdtemp(prefix="privmods-")
        case.addCleanup(platforms.current.rmtree, self.root)
        self.work, self.bare = os.path.join(self.root, "pm"), os.path.join(self.root, "pm.git")
        os.makedirs(self.work)
        git(self.work, "init", "-q")
        for name in MODS:
            self.write(self.work, f"{name}/module.json", {"name": name, "version": "1.0.0", "api": "^2.0",
                                                          "run": [sys.executable, FAKE, "."]})
        git(self.work, "add", "-A")
        git(self.work, "commit", "-qm", "modules")
        self.commit = git(self.work, "rev-parse", "HEAD")
        git(self.work, "tag", "-a", "-m", "pm-v1.0.0", "pm-v1.0.0")
        git(self.root, "clone", "-q", "--bare", self.work, self.bare)
        self.core = os.path.join(self.root, "core")
        os.makedirs(os.path.join(self.core, "modules"))
        git(self.core, "init", "-q")
        self.pin()
        self.state = os.path.join(self.root, "state")
        os.makedirs(os.path.join(self.state, "spool"))
        self.conf = os.path.join(self.root, "conf")
        os.makedirs(self.conf)
        self.cfg_path = os.path.join(self.conf, "hub.json")
        self.write(self.conf, "hub.json", {"update": {"enabled": False}})
        env = unittest.mock.patch.dict(os.environ, {"HUB_CONFIG": self.cfg_path, "HUB_CONFIG_DIR": self.conf,
                                                    "HUB_STATE_DIR": self.state,
                                                    "HUB_TEST_PRIVATE_PIN": os.path.join(self.core, "modules",
                                                                                         "private.json")})
        env.start()
        case.addCleanup(env.stop)

    def write(self, base, rel, data):
        path = os.path.join(base, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))

    def pin(self, url=None, commit=None, ref="pm-v1.0.0", required=False):
        self.write(self.core, "modules/private.json", {
            "repo": "acme/team-modules", "url": url or self.bare, "ref": ref, "commit": commit or self.commit,
            "modules": {n: {"subdir": n, "cap": c} for n, c in MODS.items()}, "required": required})

    def cfg(self):
        return catalog.load_config(self.state)

    def ensure(self, states=None, clock=None, **kw):
        from modules.update import private
        ready = states if states is not None else (lambda sd, names: {n: {"ready": True, "status": "ok"} for n in names})
        return private.Ensure(self.core, self.state, self.cfg(), states=ready, **({"clock": clock} if clock else {}),
                              **kw)

    def events(self, kind=None):
        out = []
        for path, item in spool.take(os.path.join(self.state, "spool")):
            os.unlink(path)
            if kind is None or item["type"] == kind:
                out.append(item)
        return out

    def hub(self, *args):
        r = subprocess.run([sys.executable, HUB, *args], capture_output=True, text=True,
                           env={**{k: v for k, v in os.environ.items() if k != "HUB_SESSION"}, "HUB_STATE_DIR": self.state})
        return r.returncode, r.stdout + r.stderr

    def sources(self):
        return {s["subdir"]: s for s in (self.cfg().get("modules") or {}).get("sources") or []}


class Ensure(unittest.TestCase):
    def setUp(self):
        self.f = Fixture(self)

    def test_success_installs_all_three_modules_pinned_on_the_commit(self):
        got = self.f.ensure().run()
        self.assertEqual(got["state"], "done", got)
        src = self.f.sources()
        self.assertEqual(set(src), set(MODS))
        # installed by the pin commit, not by the tag: git itself checks that exactly it arrived
        self.assertTrue(all(s["commit"] == self.f.commit and s["ref"] == self.f.commit for s in src.values()))
        self.assertEqual(set((self.f.cfg().get("modules") or {}).get("enabled") or []), set(MODS))
        self.assertTrue(got["access"]["ok"])
        evs = self.f.events()
        mods = [e["data"] for e in evs if e["type"] == "update.modules"]
        self.assertEqual(len(mods), 1)
        self.assertTrue(mods[0]["ok"])
        self.assertEqual(mods[0]["missing"], [])
        self.assertIn("owner.notify_resolved", [e["type"] for e in evs])
        self.assertNotIn("owner.notify", [e["type"] for e in evs])

    def test_no_access_changes_nothing_and_tells_the_owner_once(self):
        self.f.pin(url=os.path.join(self.f.root, "no-such.git"))
        before = catalog.read_settings(self.f.cfg_path)
        got = self.f.ensure().run()
        self.assertEqual(got["state"], "failed")
        self.assertFalse(got["access"]["ok"])
        self.assertEqual(catalog.read_settings(self.f.cfg_path), before)   # the built-in works, hub.json is the same
        evs = self.f.events()
        (note,) = [e["data"] for e in evs if e["type"] == "owner.notify"]
        self.assertEqual(note["id"], "private-modules")
        self.assertIn("no access", note["text"])
        (mods,) = [e["data"] for e in evs if e["type"] == "update.modules"]
        self.assertFalse(mods["ok"])
        self.assertFalse(mods["access"])
        self.assertEqual(sorted(mods["missing"]), sorted(MODS))
        # repeat: the same outcome, no new fact and no new line
        self.f.ensure().run()
        self.assertEqual(self.f.events(), [])

    def test_repeat_after_access_appears_installs_and_resolves_the_line(self):
        url = os.path.join(self.f.root, "later.git")
        self.f.pin(url=url)
        self.f.ensure().run()
        self.f.events()
        git(self.f.root, "clone", "-q", "--bare", self.f.work, url)   # the invitation was accepted
        got = self.f.ensure().run()
        self.assertEqual(got["state"], "done")
        types = [e["type"] for e in self.f.events()]
        self.assertIn("owner.notify_resolved", types)
        self.assertIn("update.modules", types)
        # everything is installed: a repeat installs nothing and writes nothing
        calls = []
        got = self.f.ensure(hub=lambda sd, a: calls.append(a) or (0, "")).run()
        self.assertEqual((got["state"], calls, self.f.events()), ("done", [], []))

    def test_no_hello_waits_then_fails_with_a_line(self):
        clock = [1000.0]
        never = lambda sd, names: {n: {"ready": False, "status": "ok"} for n in names}  # noqa: E731
        got = self.f.ensure(states=never, clock=lambda: clock[0]).run()
        self.assertEqual(got["state"], "pending")   # just installed: hello is still to come
        self.assertEqual(self.f.events("update.modules"), [])
        from modules.update import private
        clock[0] += private.HELLO_WAIT + 1
        got = self.f.ensure(states=never, clock=lambda: clock[0]).run()
        self.assertEqual(got["state"], "failed")
        self.assertEqual(sorted(got["missing"]), sorted(MODS))
        evs = self.f.events()
        (note,) = [e["data"] for e in evs if e["type"] == "owner.notify"]
        self.assertIn("hello", note["text"])
        # it arrived: ready again, the line is removed
        clock[0] += 60
        got = self.f.ensure(clock=lambda: clock[0]).run()
        self.assertEqual(got["state"], "done")
        self.assertIn("owner.notify_resolved", [e["type"] for e in self.f.events()])

    def test_failed_for_hello_is_looked_at_again_in_ten_minutes_not_an_hour(self):
        # modules that came up after the failure (files restored, a restart) must not stay "failed" for an hour
        from modules.update import private
        clock = [1000.0]
        never = lambda sd, names: {n: {"ready": False, "status": "ok"} for n in names}  # noqa: E731
        self.f.ensure(states=never, clock=lambda: clock[0]).run()
        clock[0] += private.HELLO_WAIT + 1
        got = self.f.ensure(states=never, clock=lambda: clock[0]).run()
        self.assertEqual(got["state"], "failed")
        self.assertEqual(got["next"], round(clock[0] + private.HELLO_FAILED_RETRY))
        self.assertFalse(self.f.ensure(clock=lambda: clock[0] + 60).due())
        self.assertTrue(self.f.ensure(clock=lambda: clock[0] + private.HELLO_FAILED_RETRY + 1).due())

    def test_not_ready_right_after_a_hub_restart_is_not_a_failure(self):
        # the module stood for a long time; the hub was restarted and the module comes up later than the update process:
        # wait for hello again
        self.f.ensure().run()
        self.f.events()
        clock = [10 ** 9]
        never = lambda sd, names: {n: {"ready": False, "status": "ok"} for n in names}  # noqa: E731
        got = self.f.ensure(states=never, clock=lambda: clock[0]).run()
        self.assertEqual(got["state"], "pending")
        self.assertEqual(self.f.events("owner.notify"), [])

    def test_record_without_code_on_this_machine_is_installed_again(self):
        # hub.json carries the record (settings from another machine), the commit folder is not here: not installed
        from core import modsrc
        self.f.ensure().run()
        src = self.f.sources()["accounts"]
        platforms.current.rmtree(modsrc.commit_dir(self.f.state, src["git"], src["commit"]))
        self.assertFalse(self.f.ensure().todo(private_pin(self.f)) == [])
        got = self.f.ensure().run()
        self.assertEqual(got["state"], "done", got)
        self.assertTrue(os.path.isdir(modsrc.commit_dir(self.f.state, src["git"], src["commit"])))

    def test_removed_by_the_owner_is_not_installed_again(self):
        self.f.ensure().run()
        self.f.events()
        code, out = self.f.hub("module", "remove", "accounts")
        self.assertEqual(code, 0, out)
        self.assertNotIn("accounts", self.f.sources())
        got = self.f.ensure().run()
        self.assertNotIn("accounts", self.f.sources())
        self.assertEqual(got["modules"]["accounts"]["state"], "declined")
        self.assertEqual(got["declined"], ["accounts"])
        self.assertEqual(got["state"], "done")   # removed is not "not installed"
        # to bring it back, run hub module add by hand; after that the step manages it
        code, out = self.f.hub("module", "add", self.f.bare, "--ref", "pm-v1.0.0", "--subdir", "accounts")
        self.assertEqual(code, 0, out)
        got = self.f.ensure().run()
        self.assertEqual(got["declined"], [])
        self.assertEqual(got["modules"]["accounts"]["state"], "ready")

    def test_removal_during_a_step_is_not_lost(self):
        # the owner removed the module while the step was installing others: the step's outcome does not overwrite his
        # refusal (otherwise the next step would bring back
        # the removed module)
        from modules.update import private
        self.f.ensure().run()
        git(self.f.work, "commit", "-q", "--allow-empty", "-m", "next")
        git(self.f.work, "tag", "-a", "-m", "pm-v1.1.0", "pm-v1.1.0")
        git(self.f.work, "push", "-q", self.f.bare, "HEAD:main", "refs/tags/pm-v1.1.0")
        self.f.pin(commit=git(self.f.work, "rev-parse", "HEAD"), ref="pm-v1.1.0")

        def hub(sd, args):
            if not private.read_state(sd).get("declined"):
                private.on_removed(sd, ["stats"])   # hub module remove stats, in the middle of the step
            return private.hub_cli(sd, args)
        got = self.f.ensure(hub=hub).run()
        self.assertEqual(got["next"], 0)   # a refusal mid-step: next pass at once, the step's outcome does not push it back
        self.assertNotEqual(self.f.ensure().run()["next"], 0)   # and this pass has the usual interval, not every minute
        self.assertEqual(got["modules"]["stats"]["state"], "declined")
        self.assertEqual(got["state"], "done")
        self.assertEqual(private.read_state(self.f.state)["declined"], ["stats"])
        self.assertEqual(private.declined(self.f.state, self.f.cfg(), private.load_pin(self.f.core)), {"stats"})

    def test_builtin_named_by_the_owner_is_declined(self):
        self.f.write(self.f.conf, "hub.json", {"update": {"enabled": False}, "modules": {"use": {"profile": "builtin"}}})
        got = self.f.ensure().run()
        self.assertNotIn("accounts", self.f.sources())
        self.assertEqual(got["modules"]["accounts"]["state"], "declined")
        self.assertEqual(set(self.f.sources()), {"voice-reply", "stats"})

    def test_turned_off_in_the_modules_tab_is_not_turned_back_on(self):
        self.f.ensure().run()
        with catalog.edit_settings(self.f.cfg_path) as raw:
            raw["modules"]["enabled"] = ["stats", "voice-reply"]
        got = self.f.ensure().run()
        self.assertEqual(got["modules"]["accounts"]["state"], "declined")
        self.assertNotIn("accounts", self.f.cfg()["modules"]["enabled"])

    def test_moved_tag_is_not_installed(self):
        git(self.f.work, "commit", "-q", "--allow-empty", "-m", "other")
        git(self.f.work, "tag", "-f", "-a", "-m", "moved", "pm-v1.0.0")
        git(self.f.work, "push", "-q", "-f", self.f.bare, "refs/tags/pm-v1.0.0")
        got = self.f.ensure().run()
        self.assertEqual(got["state"], "failed")
        self.assertEqual(self.f.sources(), {})
        self.assertIn("points to", got["why"])

    def test_no_tag_is_not_access_trouble(self):
        self.f.pin(ref="pm-v9.9.9")
        got = self.f.ensure().run()
        self.assertEqual(got["state"], "failed")
        self.assertTrue(got["access"]["ok"])
        (note,) = [e["data"] for e in self.f.events() if e["type"] == "owner.notify"]
        self.assertIn("no tag pm-v9.9.9", note["text"])

    def test_new_pin_moves_the_modules_to_the_new_commit(self):
        self.f.ensure().run()
        git(self.f.work, "commit", "-q", "--allow-empty", "-m", "next")
        git(self.f.work, "tag", "-a", "-m", "pm-v1.1.0", "pm-v1.1.0")
        git(self.f.work, "push", "-q", self.f.bare, "HEAD:main", "refs/tags/pm-v1.1.0")
        new = git(self.f.work, "rev-parse", "HEAD")
        self.f.pin(commit=new, ref="pm-v1.1.0")
        self.assertTrue(self.f.ensure().due())   # a new pin is due at once, not in an hour
        got = self.f.ensure().run()
        self.assertEqual(got["state"], "done", got)
        src = self.f.sources()
        self.assertEqual(len((self.f.cfg().get("modules") or {}).get("sources")), 3)   # replaced, not added alongside
        self.assertTrue(all(s["commit"] == new and s["ref"] == new for s in src.values()))

    def test_a_module_folder_that_went_bad_is_put_back_by_the_step(self):
        """Files of the installed commit were removed or changed (a temp cleaner, a failed disk, an edit by hand): the module is
        blocked, and without this nothing would bring them back until the owner runs `hub module update`."""
        from core import modsrc
        self.f.ensure().run()
        src = self.f.sources()["accounts"]
        folder = modsrc.commit_dir(self.f.state, src["git"], src["commit"])
        with open(os.path.join(folder, "accounts", "module.json"), "a", encoding="utf-8") as fh:
            fh.write("\n")
        shutil.rmtree(os.path.join(folder, "stats"))
        self.assertIsNotNone(modsrc.damage(self.f.state, src["git"], src["commit"]))
        e = self.f.ensure()
        self.assertTrue(e.due(boot=True))   # at hub start too, before the core
        got = e.run(boot=True)
        self.assertEqual(got["state"], "done", got)
        self.assertIsNone(modsrc.damage(self.f.state, src["git"], src["commit"]))
        self.assertFalse(self.f.ensure().due(boot=True))   # and it is not repeated

    def test_a_damaged_module_the_owner_turned_off_is_left_alone(self):
        from core import modsrc
        self.f.ensure().run()
        src = self.f.sources()["accounts"]
        with catalog.edit_settings(self.f.cfg_path) as raw:
            raw["modules"]["enabled"] = ["stats", "voice-reply"]
        shutil.rmtree(os.path.join(modsrc.commit_dir(self.f.state, src["git"], src["commit"]), "accounts"))
        from modules.update import private
        todo = self.f.ensure().todo(private.load_pin(self.f.core))
        self.assertNotIn("accounts", todo)         # the owner's word: not installed and not turned back on
        self.assertEqual(sorted(todo), ["stats", "voice-reply"])   # the commit folder is shared: the others are put back
        self.f.ensure().run()
        self.assertNotIn("accounts", self.f.cfg()["modules"]["enabled"])

    def test_tag_moved_after_the_access_check_does_not_install_other_code(self):
        # review: ls-remote saw the pinned commit, but by `hub module add` the tag had been moved: the pinned one is
        # installed
        real = __import__("modules.update.private", fromlist=["x"]).ls_remote

        def access(url, ref):
            got = real(url, ref)
            git(self.f.work, "commit", "-q", "--allow-empty", "-m", "evil")
            git(self.f.work, "tag", "-f", "-a", "-m", "moved", "pm-v1.0.0")
            git(self.f.work, "push", "-q", "-f", self.f.bare, "HEAD:main", "refs/tags/pm-v1.0.0")
            return got
        got = self.f.ensure(access=access).run()
        self.assertEqual(got["state"], "done")
        self.assertTrue(all(s["commit"] == self.f.commit for s in self.f.sources().values()))

    def test_disabled_module_stays_off_when_the_pin_moves(self):
        # review: turned off in "Modules" on the old pin: the new pin does not turn it on
        self.f.ensure().run()
        with catalog.edit_settings(self.f.cfg_path) as raw:
            raw["modules"]["enabled"] = ["accounts", "stats"]
        git(self.f.work, "commit", "-q", "--allow-empty", "-m", "next")
        git(self.f.work, "tag", "-a", "-m", "pm-v1.1.0", "pm-v1.1.0")
        git(self.f.work, "push", "-q", self.f.bare, "HEAD:main", "refs/tags/pm-v1.1.0")
        self.f.pin(commit=git(self.f.work, "rev-parse", "HEAD"), ref="pm-v1.1.0")
        got = self.f.ensure().run()
        self.assertNotIn("voice-reply", self.f.cfg()["modules"]["enabled"])
        self.assertEqual(got["modules"]["voice-reply"]["state"], "declined")
        self.assertEqual(got["state"], "done")

    def test_daily_access_recheck_failing_does_not_say_not_installed(self):
        # review: everything is installed but the daily access check failed (network): not "modules not installed"
        from modules.update import private
        clock = [5000.0]
        self.f.ensure(clock=lambda: clock[0]).run()
        self.f.events()
        clock[0] += private.ACCESS_EVERY + 1
        got = self.f.ensure(clock=lambda: clock[0], access=lambda u, r: ("no-access", None, "network")).run()
        self.assertEqual(got["state"], "done")
        self.assertFalse(got["access"]["ok"])
        self.assertEqual(self.f.events("owner.notify"), [])
        # and a moved tag is reported then too (review: do not mute the sign of a substituted release)
        clock[0] += private.ACCESS_EVERY + 1
        got = self.f.ensure(clock=lambda: clock[0], access=lambda u, r: ("ok", "b" * 40, "")).run()
        self.assertEqual(got["state"], "failed")
        self.assertIn("points to", got["why"])

    def test_reinstalled_module_gets_the_hello_grace_again(self):
        # review: it was "not ready" long ago, the pin moved and the module was installed anew: HELLO_WAIT again, not an
        # instant failure
        from modules.update import private
        clock = [5000.0]
        never = lambda sd, names: {n: {"ready": False, "status": "ok"} for n in names}  # noqa: E731
        self.f.ensure(states=never, clock=lambda: clock[0]).run()
        clock[0] += private.HELLO_WAIT + 1
        self.assertEqual(self.f.ensure(states=never, clock=lambda: clock[0]).run()["state"], "failed")
        git(self.f.work, "commit", "-q", "--allow-empty", "-m", "next")
        git(self.f.work, "tag", "-a", "-m", "pm-v1.1.0", "pm-v1.1.0")
        git(self.f.work, "push", "-q", self.f.bare, "HEAD:main", "refs/tags/pm-v1.1.0")
        self.f.pin(commit=git(self.f.work, "rev-parse", "HEAD"), ref="pm-v1.1.0")
        clock[0] += 60
        self.assertEqual(self.f.ensure(states=never, clock=lambda: clock[0]).run()["state"], "pending")

    def test_failed_remove_records_no_decline(self):
        from modules.update import private
        code, _ = self.f.hub("module", "remove", "accounts")   # not installed yet: the command refused
        self.assertEqual(code, 1)
        self.assertEqual(private.read_state(self.f.state).get("declined") or [], [])

    def test_due_hourly_and_at_boot_only_with_something_to_install(self):
        from modules.update import private
        clock = [5000.0]
        e = self.f.ensure(clock=lambda: clock[0])
        self.assertTrue(e.due(boot=True))
        e.run()
        e = self.f.ensure(clock=lambda: clock[0])
        self.assertFalse(e.due(boot=True))   # everything is installed: hub startup does not wait
        self.assertFalse(e.due())
        clock[0] += private.EVERY
        self.assertTrue(e.due())

    def test_failed_recently_does_not_hold_the_next_boot(self):
        self.f.pin(url=os.path.join(self.f.root, "no-such.git"))
        clock = [5000.0]
        self.f.ensure(clock=lambda: clock[0]).run()
        self.assertFalse(self.f.ensure(clock=lambda: clock[0] + 60).due(boot=True))

    def test_no_pin_does_nothing(self):
        os.unlink(os.path.join(self.f.core, "modules", "private.json"))
        e = self.f.ensure()
        self.assertFalse(e.due())
        self.assertIsNone(e.run())
        self.assertFalse(os.path.exists(os.path.join(self.f.state, "private-modules.json")))
        # an open snapshot without private.json: the step is silent: no "accept the invitation", no fact, no readiness
        # record
        from modules.update import private
        self.assertEqual(self.f.events(), [])
        self.assertIsNone(private.write_ready(self.f.core, self.f.state, self.f.cfg()))
        self.assertIsNone(private.report(self.f.state, self.f.cfg(), [], repo=self.f.core))
        self.assertEqual(private.lines(self.f.state, repo=self.f.core), [])


class StepLog(unittest.TestCase):
    """update.ensure_private: the hourly step with the same result does not write the same line to the log again."""

    def setUp(self):
        self.f = Fixture(self)

    def run_step(self, result):
        import contextlib
        import io
        from modules.update import private, update

        class Step:
            def __init__(self, repo, state_dir, cfg):
                self.state_dir, self.cfg = state_dir, cfg

            def due(self):
                return True

            def run(self):
                private.write_state(self.state_dir, result)
                return result
        out = io.StringIO()
        with unittest.mock.patch.object(private, "Ensure", Step), \
                unittest.mock.patch.object(private, "write_ready", lambda *a: None), contextlib.redirect_stdout(out):
            update.ensure_private(self.f.state, self.f.cfg())
        return out.getvalue()

    def test_the_same_failed_result_is_told_once_a_change_is_told_again(self):
        failed = {"state": "failed", "why": "accounts: module error (hub modules)", "commit": "c1", "ts": 1}
        self.assertIn("failed", self.run_step(failed))
        self.assertEqual(self.run_step({**failed, "ts": 3601}), "")
        self.assertEqual(self.run_step({**failed, "ts": 7201}), "")
        self.assertIn("done", self.run_step({"state": "done", "commit": "c1", "ts": 7300}))


class Address(unittest.TestCase):
    PIN = {"repo": "acme/team-modules", "url": "https://github.com/acme/team-modules.git"}

    def test_scheme_follows_origin_without_its_login(self):
        from modules.update.private import address
        cases = {
            "git@github.com:acme/agents-hub-core.git": "git@github.com:acme/team-modules.git",
            "github-work:acme/agents-hub-core": "github-work:acme/team-modules.git",
            "ssh://git@github.com/acme/agents-hub-core.git": "ssh://git@github.com/acme/team-modules.git",
            "ssh://git@github.com:22/acme/agents-hub-core": "ssh://git@github.com:22/acme/team-modules.git",
            "https://github.com/acme/agents-hub-core.git": "https://github.com/acme/team-modules.git",
            "https://someone:tok3n@github.com/acme/agents-hub-core": "https://github.com/acme/team-modules.git",
            "/home/x/core.git": self.PIN["url"], None: self.PIN["url"], "C:\\x\\core.git": self.PIN["url"],
            "file:///home/x/core.git": self.PIN["url"],
        }
        for origin, want in cases.items():
            self.assertEqual(address(self.PIN, origin), want, origin)

    def test_same_repo_in_any_form(self):
        from modules.update.private import same_repo
        pin = self.PIN
        self.assertTrue(same_repo("git@github.com:acme/team-modules.git", pin["url"], pin))
        self.assertTrue(same_repo("github-work:acme/team-modules", pin["url"], pin))
        self.assertFalse(same_repo("git@github.com:someone/team-modules.git", pin["url"], pin))
        self.assertFalse(same_repo("/home/x/other.git", "/home/x/pm.git", pin))
        # review: an address with a port (ssh over 443, own https port)
        self.assertTrue(same_repo("ssh://git@ssh.github.com:443/acme/team-modules.git", pin["url"], pin))
        self.assertTrue(same_repo("https://git.example.com:8443/acme/team-modules", pin["url"], pin))


class Detector(unittest.TestCase):
    def setUp(self):
        self.conf = tempfile.mkdtemp(prefix="privdet-")
        self.addCleanup(platforms.current.rmtree, self.conf)
        home = unittest.mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": os.path.join(self.conf, "claude")})
        home.start()
        self.addCleanup(home.stop)

    def put(self, name, data):
        os.makedirs(os.path.dirname(os.path.join(self.conf, name)), exist_ok=True)
        with open(os.path.join(self.conf, name), "w") as f:
            json.dump(data, f)

    def test_what_is_needed(self):
        from modules.update.private import needed
        self.assertEqual(needed({}, self.conf), {"accounts": False, "voice-reply": False, "stats": False})
        self.assertTrue(needed({"gateway": {"enabled": True}}, self.conf)["accounts"])
        self.assertTrue(needed({"voice_reply": {"enabled": True}}, self.conf)["voice-reply"])
        self.assertTrue(needed({"stats": {"enabled": True}}, self.conf)["stats"])
        self.put("accounts.json", {"accounts": [{"name": "a", "token": "x"}], "off": True})   # and paused
        self.assertTrue(needed({}, self.conf)["accounts"])

    def test_conduit_login_counts_unless_off(self):
        from modules.update.private import needed
        self.put("claude/.credentials.json", {"mcpOAuth": {"k": {"serverName": "conduit", "accessToken": "t"}}})
        self.assertTrue(needed({}, self.conf)["accounts"])
        self.put("conduit.json", {"off": True})
        self.assertFalse(needed({}, self.conf)["accounts"])

    def test_conduit_login_of_a_plugin_server_counts(self):
        # Claude Code names a plugin's MCP server «plugin:<plugin>:<server>»
        from modules.update.private import needed
        self.put("claude/.credentials.json", {"mcpOAuth": {"k": {"serverName": "plugin:x:conduit-other", "accessToken": "t"}}})
        self.assertFalse(needed({}, self.conf)["accounts"])
        self.put("claude/.credentials.json", {"mcpOAuth": {"k": {"serverName": "plugin:tools:conduit", "accessToken": "t"}}})
        self.assertTrue(needed({}, self.conf)["accounts"])


class Gate(unittest.TestCase):
    """Gate R2 (4.4): a target with required is not merged while this hub or another hub of this code folder needs a
    function and the module does not provide it."""

    def setUp(self):
        self.f = Fixture(self)
        self.target = json.dumps({"repo": "acme/team-modules", "url": self.f.bare, "ref": "pm-v1.0.0",
                                  "commit": self.f.commit, "modules": {n: {"subdir": n, "cap": c} for n, c in MODS.items()},
                                  "required": True})

    def stats_on(self):
        with catalog.edit_settings(self.f.cfg_path) as raw:
            raw["stats"] = {"enabled": True}

    def test_not_required_or_not_needed_passes(self):
        from modules.update import private
        cfg = self.f.cfg()
        self.assertEqual(private.gate(self.f.core, self.f.state, cfg, self.target.replace('"required": true',
                                                                                          '"required": false')), [])
        self.assertEqual(private.gate(self.f.core, self.f.state, cfg, self.target, config_dir=self.f.conf), [])
        self.assertEqual(private.gate(self.f.core, self.f.state, cfg, None), [])

    def test_needed_without_module_holds(self):
        from modules.update import private
        self.stats_on()
        got = private.gate(self.f.core, self.f.state, self.f.cfg(), self.target, config_dir=self.f.conf)
        self.assertEqual(got, ["stats: the module is not installed"])
        self.f.ensure().run()   # installed, but the built-in still runs the report
        got = private.gate(self.f.core, self.f.state, self.f.cfg(), self.target, config_dir=self.f.conf)
        self.assertEqual(got, ["stats: the built-in code handles it"])
        from core import providers
        providers.write(self.f.state, {"stats": {"state": "module", "module": "stats"}})
        self.assertEqual(private.gate(self.f.core, self.f.state, self.f.cfg(), self.target, config_dir=self.f.conf), [])

    def test_an_app_code_layer_gets_no_readiness_record(self):
        # the app's code layer (core/appcode.py) is no git clone: a record in it would be an edit of the layer at every
        # start, and git would find the repository the layer sits in and write there
        from core import appcode
        from modules.update import private
        outer = os.path.join(self.f.root, "outer")
        os.makedirs(outer)
        git(outer, "init", "-q")
        self.stats_on()
        for stop in (False, True):   # a layer of the factory carries its .git file (appcode.GIT_STOP), a bare one none
            with self.subTest(stop=stop):
                layer = os.path.join(outer, "layers", f"1.0.{int(stop)}-team")
                self.f.write(layer, "MANIFEST.json", "{}")
                self.f.write(layer, "modules/private.json", self.target)
                if stop:
                    self.f.write(layer, appcode.GIT_STOP[0], appcode.GIT_STOP[1].decode())
                self.assertIsNone(private.write_ready(layer, self.f.state, self.f.cfg()))
                written = [os.path.join(d, n) for top in (layer, os.path.join(outer, ".git")) for d, _, names in os.walk(top)
                           for n in names if private.READY_DIR in d]
                self.assertEqual(written, [])
                # no .git of its own is made: a bare layer keeps none, the factory's keeps its file
                self.assertEqual(os.path.exists(os.path.join(layer, ".git")), stop)
                self.assertFalse(os.path.isdir(os.path.join(layer, ".git")))
                self.assertEqual(private.others(layer, self.f.state), {})

    def test_other_hub_on_the_same_code_folder_holds_until_forgotten(self):
        from core import providers
        from modules.update import private
        other = os.path.join(self.f.root, "state2")
        os.makedirs(other)
        # a second hub without access: no module there, but statistics is on
        self.f.ensure().run()
        providers.write(self.f.state, {"stats": {"state": "module", "module": "stats"}})
        self.stats_on()
        with unittest.mock.patch.object(private, "needed", lambda cfg, conf=None: {"accounts": False,
                                                                                    "voice-reply": False, "stats": True}):
            private.write_ready(self.f.core, other, {"stats": {"enabled": True}})
            mine = private.write_ready(self.f.core, self.f.state, self.f.cfg())
        self.assertEqual(mine["modules"]["stats"], {"needed": True, "provider": "module"})
        got = private.gate(self.f.core, self.f.state, self.f.cfg(), self.target, config_dir=self.f.conf)
        key = private.hub_key(other)
        self.assertEqual(got, [f"hub {os.path.realpath(other)} ({key}): stats — the module is not installed"])
        # an old record (the hub sleeps for a week) holds the same way
        path = os.path.join(private.ready_dir(self.f.core), key + ".json")
        with open(path) as fh:
            rec = json.load(fh)
        rec["ts"] = 1
        with open(path, "w") as fh:
            json.dump(rec, fh)
        self.assertEqual(len(private.gate(self.f.core, self.f.state, self.f.cfg(), self.target, config_dir=self.f.conf)), 1)
        # removed for good: its record is removed by the owner (hub update forget-follower, by this hub's code folder)
        private.forget(self.f.core, key)
        self.assertEqual(private.gate(self.f.core, self.f.state, self.f.cfg(), self.target, config_dir=self.f.conf), [])

    def test_forget_follower_command_removes_the_record(self):
        # bench case: the record was removed but printing failed (TypeError: t() got multiple values for argument 'key')
        from modules.update import private
        other = os.path.join(self.f.root, "state2")
        os.makedirs(other)
        private.write_ready(self.f.core, other, {"stats": {"enabled": True}})
        key = private.hub_key(other)
        code, out = self.f.hub("update", "forget-follower", key)
        self.assertEqual(code, 0, out)
        self.assertIn(key, out)
        self.assertEqual(private.others(self.f.core, self.f.state), {})

    def test_forget_follower_command_checks_the_key(self):
        code, out = self.f.hub("update", "forget-follower", "nope")
        self.assertEqual(code, 1)
        self.assertIn("16 characters", out)
        code, out = self.f.hub("update", "forget-follower", "0123456789abcdef")
        self.assertEqual(code, 1)
        self.assertIn("there is no record", out)

    def test_broken_record_holds(self):
        from modules.update import private
        folder = private.ready_dir(self.f.core)
        os.makedirs(folder)
        with open(os.path.join(folder, "0123456789abcdef.json"), "w") as fh:
            fh.write("{")
        got = private.gate(self.f.core, self.f.state, self.f.cfg(), self.target, config_dir=self.f.conf)
        self.assertEqual(got, ["record 0123456789abcdef: unreadable"])


class Waiting(unittest.TestCase):
    """4.5: with no built-in login in the tree, startup waits for the login module while it is installed or coming up."""

    def setUp(self):
        self.f = Fixture(self)
        with open(os.path.join(self.f.conf, "accounts.json"), "w") as fh:
            json.dump({"accounts": [{"name": "a", "token": "x"}]}, fh)

    @cut.needs("profile")
    def test_builtin_in_the_tree_never_waits(self):
        from modules.update import private
        self.assertTrue(private.builtin_present("profile"))   # this release: the built-in pool is in the tree
        private.write_state(self.f.state, {"state": "installing", "ts": 10 ** 10})
        self.assertIsNone(private.waiting(self.f.state, self.f.cfg(), repo=REPO, now=10 ** 10, config_dir=self.f.conf))

    def test_without_builtin_waits_while_installing_or_starting(self):
        from modules.update import private
        cfg = self.f.cfg()
        private.write_state(self.f.state, {"state": "installing", "ts": 1000})
        self.assertEqual(private.waiting(self.f.state, cfg, repo=self.f.core, now=1001, config_dir=self.f.conf),
                         "module accounts is being installed")
        self.assertIsNone(private.waiting(self.f.state, cfg, repo=self.f.core, now=1000 + private.INSTALLING_TTL,
                                          config_dir=self.f.conf))   # the step died midway: do not wait forever
        self.f.ensure().run()
        cfg = self.f.cfg()
        self.assertEqual(private.waiting(self.f.state, cfg, ready=lambda n: False, status=lambda n: "ok",
                                         repo=self.f.core, config_dir=self.f.conf), "module accounts is coming up")
        self.assertIsNone(private.waiting(self.f.state, cfg, ready=lambda n: True, repo=self.f.core,
                                          config_dir=self.f.conf))
        self.assertIsNone(private.waiting(self.f.state, cfg, ready=lambda n: False, status=lambda n: "error",
                                          repo=self.f.core, config_dir=self.f.conf))   # stopped: the machine's login
        # review: the step installs another module (or repeats without access) while the login module is already ready:
        # do not wait
        private.write_state(self.f.state, {**private.read_state(self.f.state), "state": "installing",
                                           "ts": time.time()})
        self.assertIsNone(private.waiting(self.f.state, cfg, ready=lambda n: True, repo=self.f.core,
                                          config_dir=self.f.conf))

    def test_no_pool_or_declined_does_not_wait(self):
        from modules.update import private
        private.write_state(self.f.state, {"state": "installing", "ts": 1000, "declined": ["accounts"]})
        self.assertIsNone(private.waiting(self.f.state, self.f.cfg(), repo=self.f.core, now=1001,
                                          config_dir=self.f.conf))
        os.unlink(os.path.join(self.f.conf, "accounts.json"))
        private.write_state(self.f.state, {"state": "installing", "ts": 1000})
        self.assertIsNone(private.waiting(self.f.state, self.f.cfg(), repo=self.f.core, now=1001,
                                          config_dir=self.f.conf))


class Report(unittest.TestCase):
    def test_private_field_counts_failures_pauses_and_fallbacks(self):
        f = Fixture(self)
        f.ensure().run()
        from modules.update import private
        evs = [{"type": "module.error", "ts": 100 + i, "data": {"module": "stats", "kind": "timeout", "hook": "x"}}
               for i in range(3)]
        evs += [{"type": "module.error", "ts": 200, "data": {"module": "accounts", "kind": "shadow"}},
                {"type": "module.provider", "ts": 300, "data": {"cap": "speech", "state": "builtin",
                                                                "module": "voice-reply"}}]
        got = private.report(f.state, f.cfg(), evs, repo=f.core)
        self.assertEqual(got["ref"], "pm-v1.0.0")
        self.assertTrue(got["access"]["ok"])
        self.assertEqual(got["modules"]["stats"]["failed"], 3)
        self.assertEqual(got["modules"]["stats"]["paused"], 1)   # three failures in the window: pause, like the core
        self.assertEqual(got["modules"]["accounts"]["failed"], 0)  # a shadow mismatch is not a failure
        self.assertEqual(got["modules"]["voice-reply"]["fallback"], 1)
        self.assertEqual(got["modules"]["stats"]["state"], "ready")
        self.assertNotIn("why", json.dumps(got))   # without reason texts

    def test_failures_are_counted_by_call_and_kind_in_the_cores_words_only(self):
        """Which call fails and how (a hot point late on a slow machine, a dead process after a restart, a crash) — as
        counts; a hook or kind the core does not have (a forged or a future event) is "other", the text never goes."""
        f = Fixture(self)
        f.ensure().run()
        from modules.update import private

        def err(hook, kind, **more):
            return {"type": "module.error", "ts": 1, "data": {"module": "accounts", "hook": hook, "kind": kind,
                                                               "error": "secret text", **more}}
        evs = [err("profile.list", "timeout"), err("profile.list", "timeout"), err("profile.token", "connect"),
               err("process", "exit"), err("cli", "slow"), err("profile.list", "shadow"), err("launch", "late"),
               err("../etc", "timeout"), err("profile.list", "<b>"), err(None, None), err("fold", "rejected"),
               err("fold", "check"), err("http", "connect")]
        got = private.report(f.state, f.cfg(), evs, repo=f.core)["modules"]
        self.assertEqual(got["accounts"]["errors"], {"profile.list:timeout": 2, "profile.token:connect": 1,
                                                     "process:exit": 1, "cli:slow": 1, "other:timeout": 1,
                                                     "profile.list:other": 1, "other:other": 1, "fold:rejected": 1,
                                                     "fold:check": 1, "http:connect": 1})
        self.assertEqual(got["accounts"]["failed"], sum(got["accounts"]["errors"].values()))
        self.assertNotIn("errors", got["stats"])
        self.assertNotIn("secret text", json.dumps(got))

    def test_a_return_to_the_builtin_is_counted_as_a_failure_only_by_its_cause(self):
        """A planned return (the owner's word, edits, removal) and a failed shadow are not failures of the module: fault counts only
        the ones caused by a failure while it led or could not be called. An event of an older core is judged by its hold."""
        f = Fixture(self)
        f.ensure().run()
        from modules.update import private

        def back(ts, cap, **extra):
            return {"type": "module.provider", "ts": ts, "data": {"cap": cap, "state": "builtin", "module": {
                "profile": "accounts", "stats": "stats", "speech": "voice-reply"}[cap], **extra}}
        evs = [back(1, "profile", cause="owner"), back(2, "profile", cause="shadow"), back(3, "profile", cause="paused"),
               back(4, "profile", cause="edits"), back(5, "stats", cause="fail"), back(6, "stats", cause="stopped"),
               back(7, "speech", cause="removed"),
               back(8, "stats", hold={"module": "stats", "kind": "fail"}),     # a core before the field: by the hold
               back(9, "profile", hold={"module": "accounts", "kind": "shadow"}),
               back(10, "speech"),                                              # no cause, no hold: not counted as a failure
               back(11, "speech", cause="made-up")]
        got = private.report(f.state, f.cfg(), evs, repo=f.core)["modules"]
        self.assertEqual({n: (m["fallback"], m["fault"], m.get("causes")) for n, m in got.items()}, {
            "accounts": (5, 1, {"owner": 1, "shadow": 2, "paused": 1, "edits": 1}),
            "stats": (3, 3, {"fail": 2, "stopped": 1}),
            "voice-reply": (3, 0, {"removed": 1, "unknown": 2})})
        self.assertNotIn("causes", private.report(f.state, f.cfg(), [], repo=f.core)["modules"]["stats"])

    def test_the_voice_module_that_voiced_once_is_proved_in_the_report(self):
        """Voicing happens only when the owner gets a reply, so no failures in the window prove nothing there: the report says
        whether the voice module has voiced since it took over (providers.said) and says it only for the module that leads."""
        f = Fixture(self)
        f.ensure().run()
        from core import providers
        from modules.update import private
        mods = lambda: private.report(f.state, f.cfg(), [], repo=f.core)["modules"]
        providers.write(f.state, {"speech": {"state": "module", "module": "voice-reply"}})
        self.assertNotIn("proved", mods()["voice-reply"])
        providers.write(f.state, {"speech": {"state": "module", "module": "voice-reply", "proved": 5.0}})
        self.assertIs(mods()["voice-reply"].get("proved"), True)
        providers.write(f.state, {"speech": {"state": "builtin", "module": "voice-reply", "proved": 5.0}})
        self.assertNotIn("proved", mods()["voice-reply"])

    def test_no_pin_no_field(self):
        from modules.update import private
        self.assertIsNone(private.report(tempfile.mkdtemp(), {}, [], repo=tempfile.mkdtemp()))


class WhyCode(unittest.TestCase):
    """A short reason code per module in private-modules.json and the report (no texts): why a module is not ready, visible
    from the stats of a hub nobody can look into."""

    def setUp(self):
        self.f = Fixture(self)

    def codes(self, got):
        return {n: m.get("code") for n, m in got["modules"].items()}

    def report(self):
        from modules.update import private
        return private.report(self.f.state, self.f.cfg(), [], repo=self.f.core)

    def test_a_failed_access_check_has_the_code_of_why_and_no_text(self):
        """The report's access says why git did not let us in as a code (git | auth | network | no-access); an ok one has none."""
        from modules.update import private
        clock = [5000.0]
        self.f.ensure(clock=lambda: clock[0]).run()
        self.assertEqual(self.report()["access"].get("ok"), True)
        self.assertNotIn("code", self.report()["access"])
        for why, code in (("git does not start: [Errno 2] No such file", "git"),
                          ("git: fatal: Authentication failed for 'https://x'", "auth"),
                          ("git: fatal: unable to access: Could not resolve host: x", "network"),
                          ("fatal: something else", "no-access")):
            clock[0] += private.ACCESS_EVERY + 1
            self.f.ensure(clock=lambda: clock[0], access=lambda u, r, w=why: ("no-access", None, w)).run()
            got = self.report()["access"]
            self.assertEqual((got["ok"], got["code"]), (False, code))
            self.assertNotIn(why, json.dumps(self.report()))

    def test_no_access_no_tag_and_moved_tag_have_their_codes(self):
        self.f.pin(url=os.path.join(self.f.root, "no-such.git"))
        got = self.f.ensure().run()
        self.assertEqual(set(self.codes(got).values()), {"no-access"})
        self.assertEqual(set(self.codes(self.report()).values()), {"no-access"})
        self.f.pin(ref="pm-v9.9.9")
        self.assertEqual(set(self.codes(self.f.ensure().run()).values()), {"no-ref"})
        self.f.pin()
        git(self.f.work, "commit", "-q", "--allow-empty", "-m", "other")
        git(self.f.work, "tag", "-f", "-a", "-m", "moved", "pm-v1.0.0")
        git(self.f.work, "push", "-q", "-f", self.f.bare, "refs/tags/pm-v1.0.0")
        self.assertEqual(set(self.codes(self.f.ensure().run()).values()), {"tag"})

    def test_failed_install_has_its_code(self):
        got = self.f.ensure(hub=lambda sd, a: (1, "secret reason")).run()
        self.assertEqual(set(self.codes(got).values()), {"install"})
        self.assertNotIn("secret", json.dumps(self.report()))

    def test_hello_that_did_not_come_is_hello_and_a_ready_module_has_no_code(self):
        from modules.update import private
        clock = [1000.0]
        never = lambda sd, names: {n: {"ready": n != "stats", "status": "ok"} for n in names}  # noqa: E731
        self.f.ensure(states=never, clock=lambda: clock[0]).run()
        clock[0] += private.HELLO_WAIT + 1
        got = self.f.ensure(states=never, clock=lambda: clock[0]).run()
        self.assertEqual(self.codes(got), {"accounts": None, "voice-reply": None, "stats": "hello"})
        self.assertEqual(self.codes(self.report()), {"accounts": None, "voice-reply": None, "stats": "hello"})
        stopped = lambda sd, names: {n: {"ready": False, "status": "suspended"} for n in names}  # noqa: E731
        got = self.f.ensure(states=stopped, clock=lambda: clock[0]).run()
        self.assertEqual(set(self.codes(got).values()), {"stopped"})

    def test_state_not_read_is_not_a_module_failure_and_is_told_apart_from_hello(self):
        from modules.update import private
        clock = [1000.0]
        unread = lambda sd, names: None   # noqa: E731 — the read timed out
        self.f.ensure(states=unread, clock=lambda: clock[0]).run()
        self.f.events()
        clock[0] += private.HELLO_WAIT + 1
        got = self.f.ensure(states=unread, clock=lambda: clock[0]).run()
        self.assertEqual(set(self.codes(got).values()), {"read"})
        self.assertEqual(got["state"], "pending")     # we do not know: not a failure of the module and not "done"
        self.assertEqual(got["missing"], [])
        self.assertEqual(self.f.events("owner.notify"), [])
        self.assertEqual(got["next"], round(clock[0] + private.EVERY))   # and not a heavy read every minute
        self.assertEqual(set(self.codes(self.report()).values()), {"read"})
        self.assertNotIn("code", self.report())   # not a failure: no code of the step itself
        self.assertIn("unread", private.lines(self.f.state, self.f.core)[0])
        # one module unread, the rest answered
        part = lambda sd, names: {n: ({"ready": False, "status": "unknown", "unread": True} if n == "stats"   # noqa: E731
                                      else {"ready": True, "status": "ok"}) for n in names}
        got = self.f.ensure(states=part, clock=lambda: clock[0]).run()
        self.assertEqual(self.codes(got), {"accounts": None, "voice-reply": None, "stats": "read"})

    def test_unread_for_long_becomes_a_failure_with_a_line(self):
        from modules.update import private
        clock = [1000.0]
        unread = lambda sd, names: None   # noqa: E731
        self.f.ensure(states=unread, clock=lambda: clock[0]).run()
        self.f.events()
        clock[0] += private.HELLO_WAIT + 1
        self.assertEqual(self.f.ensure(states=unread, clock=lambda: clock[0]).run()["state"], "pending")   # the clock of the read starts
        clock[0] += private.UNREAD_FAIL - 10
        self.assertEqual(self.f.ensure(states=unread, clock=lambda: clock[0]).run()["state"], "pending")
        clock[0] += 20
        got = self.f.ensure(states=unread, clock=lambda: clock[0]).run()
        self.assertEqual((got["state"], got["code"], set(self.codes(got).values())), ("failed", "read", {"read"}))
        self.assertEqual(self.report()["code"], "read")
        self.assertEqual(len(self.f.events("owner.notify")), 1)

    def test_a_long_hello_wait_then_a_failed_read_is_not_a_failure_at_once(self):
        # hello did not come for hours (read fine); the read fails once: "we do not know", the clock of the read starts now
        from modules.update import private
        clock = [1000.0]
        never = lambda sd, names: {n: {"ready": False, "status": "ok"} for n in names}  # noqa: E731
        self.f.ensure(states=never, clock=lambda: clock[0]).run()
        clock[0] += private.UNREAD_FAIL + 100
        got = self.f.ensure(states=lambda sd, names: None, clock=lambda: clock[0]).run()
        self.assertEqual((got["state"], set(self.codes(got).values())), ("pending", {"read"}))
        # the read works again: the verdict is about hello
        got = self.f.ensure(states=never, clock=lambda: clock[0]).run()
        self.assertEqual((got["state"], set(self.codes(got).values())), ("failed", {"hello"}))
        self.assertEqual(got["unread"], {})

    def test_stopped_module_has_its_own_step_code(self):
        stopped = lambda sd, names: {n: {"ready": False, "status": "error"} for n in names}  # noqa: E731
        from modules.update import private
        clock = [1000.0]
        got = self.f.ensure(states=stopped, clock=lambda: clock[0]).run()
        self.assertEqual((got["state"], got["code"]), ("failed", "stopped"))

    def test_a_stopped_module_that_came_back_is_looked_at_at_once_not_in_an_hour(self):
        # hub modules reset: the module is raised again and answers hello; the line of "hub modules" must not stay
        # "not everything is installed" until the hourly step
        from modules.update import private
        stopped = lambda sd, names: {n: {"ready": False, "status": "error"} for n in names}  # noqa: E731
        clock = [1000.0]
        self.assertEqual(self.f.ensure(states=stopped, clock=lambda: clock[0]).run()["code"], "stopped")
        clock[0] += 60
        e = self.f.ensure(clock=lambda: clock[0])
        with unittest.mock.patch.object(private, "_answered", lambda sd, names: {}):
            self.assertFalse(e.due())   # still stopped: the hourly step stays
        with unittest.mock.patch.object(private, "_answered",
                                        lambda sd, names: {n: {"ready": True, "status": "ok"} for n in names}):
            self.assertTrue(e.due())    # answered hello: look now
            self.assertEqual(e.run()["state"], "done")
        self.assertEqual(private.lines(self.f.state, self.f.core)[0].split(": ")[1].split(";")[0], "in place")

    def test_a_module_without_hello_that_came_up_is_looked_at_at_once_too(self):
        from modules.update import private
        never = lambda sd, names: {n: {"ready": False, "status": "ok"} for n in names}  # noqa: E731
        clock = [1000.0]
        self.f.ensure(states=never, clock=lambda: clock[0]).run()
        clock[0] += private.HELLO_WAIT + 10
        self.assertEqual(self.f.ensure(states=never, clock=lambda: clock[0]).run()["code"], "hello")
        clock[0] += 30
        with unittest.mock.patch.object(private, "_answered",
                                        lambda sd, names: {n: {"ready": True, "status": "ok"} for n in names}):
            self.assertTrue(self.f.ensure(clock=lambda: clock[0]).due())

    def test_the_step_code_is_that_of_the_first_reason(self):
        self.f.pin(url=os.path.join(self.f.root, "no-such.git"))
        got = self.f.ensure().run()
        self.assertEqual((got["code"], self.report()["code"]), ("no-access", "no-access"))
        self.f.pin()
        self.assertNotIn("code", self.f.ensure().run())

    def test_codes_in_the_report_are_a_closed_set(self):
        from modules.update import private
        self.f.ensure().run()
        st = private.read_state(self.f.state)
        st["modules"]["stats"] = {"state": "failed", "code": "secret reason"}
        private.write_state(self.f.state, st)
        self.assertNotIn("code", self.report()["modules"]["stats"])
        st["code"] = "secret reason"
        private.write_state(self.f.state, st)
        self.assertNotIn("code", self.report())


class StatesRead(unittest.TestCase):
    """private.module_states: the readiness of modules from the files hubd writes (no journal read); the heavy read in
    a subprocess only for the rest, and its failure is told apart from "no hello"."""

    def setUp(self):
        from core import modrun
        self.modrun = modrun
        self.state = tempfile.mkdtemp(prefix="privstates-")
        self.addCleanup(platforms.current.rmtree, self.state)
        os.makedirs(modrun.run_dir(self.state))

    def launch(self, name, launch, ready, want=True):
        self.modrun._write(os.path.join(self.modrun.run_dir(self.state), f"{name}.run.json"),
                           {"pid": 1, "port": 1, "secret": "x", "launch": launch, "started": 1})
        self.modrun._write(self.modrun.slice_path(self.state, name), {"ready": ready, "mods": {}, "others": 0})
        if want:
            self.modrun._write(os.path.join(self.modrun.run_dir(self.state), f"{name}.want.json"), {"cmd": ["x"]})

    def test_ready_by_the_slice_reads_no_journal(self):
        from modules.update import private
        self.launch("stats", "L1", "L1")
        with unittest.mock.patch.object(private.subprocess, "run", side_effect=AssertionError("heavy read")):
            got = private.module_states(self.state, ["stats"])
        self.assertEqual(got, {"stats": {"ready": True, "status": "ok"}})

    def test_the_rest_goes_to_the_heavy_read_and_its_failure_is_marked(self):
        import subprocess as sp
        from modules.update import private
        self.launch("stats", "L1", "L1")
        self.launch("accounts", "L2", "OLD")   # hello was in the previous launch
        with unittest.mock.patch.object(private.subprocess, "run", side_effect=sp.TimeoutExpired("x", 120)) as run:
            got = private.module_states(self.state, ["stats", "accounts"])
        self.assertEqual(run.call_args.args[0][-1], "accounts")   # only for the one that is not ready by the slice
        self.assertEqual(got["stats"], {"ready": True, "status": "ok"})
        self.assertTrue(got["accounts"]["unread"])
        self.assertFalse(got["accounts"]["ready"])

    def test_a_stopped_process_or_no_want_is_not_ready_by_files(self):
        from modules.update import private
        self.launch("stats", "L1", "L1", want=False)   # hub-up no longer keeps it
        self.launch("accounts", "L2", None)             # no hello yet
        out = unittest.mock.Mock(returncode=0, stdout=json.dumps({n: {"ready": False, "status": "ok"} for n in
                                                                  ("stats", "accounts")}))
        with unittest.mock.patch.object(private.subprocess, "run", return_value=out) as run:
            got = private.module_states(self.state, ["stats", "accounts"])
        self.assertEqual(run.call_args.args[0][-2:], ["stats", "accounts"])
        self.assertFalse(got["stats"]["ready"] or got["accounts"]["ready"])

    def test_heavy_read_answers(self):
        from modules.update import private
        self.launch("accounts", "L2", None)
        out = unittest.mock.Mock(returncode=0, stdout=json.dumps({"accounts": {"ready": False, "status": "blocked"}}))
        with unittest.mock.patch.object(private.subprocess, "run", return_value=out):
            got = private.module_states(self.state, ["accounts"])
        self.assertEqual(got, {"accounts": {"ready": False, "status": "blocked"}})


@cut.needs_pin()   # waiting needs the modules to wait for: the open flavor cuts the pin
class SpawnWaitsForTheSignInModule(unittest.TestCase):
    """4.5 in hubd: with no built-in login, startup waits for the login module while private.waiting names a reason,
    no longer than PRIVATE_WAIT; then it uses the machine's login and one line goes to the owner. A built-in login in
    the tree: start at once."""

    def stand(self, clock=None):
        from tests.fakes.modstand import Stand
        s = Stand(self, caps=["doctor"], clock=clock)
        self.spawned = []
        test = self

        class Host:
            kind, screen_dialogs = "stream", False
            tool_flags = staticmethod(lambda **kw: [])

            def spawn(self, name, **kw):
                test.spawned.append(name)
                return "spawned"

            def alive(self, name):
                return False

            def info(self, name):
                return {"pid": 1}

            def poll(self):
                return []
        s.hub.host = Host()
        s.hub.modules.sync()
        return s

    def request(self, s, name="a"):
        s.hub.record("session.requested", name, {"cwd": s.state_dir, "session_id": f"s-{name}", "channel": "cli",
                                                 "owner": True})

    def test_builtin_in_the_tree_spawns_at_once(self):
        s = self.stand()
        self.request(s)
        self.assertEqual(self.spawned, ["a"])

    def test_waits_while_the_module_installs_then_spawns(self):
        from modules.update import private
        why = ["the accounts module is being installed"]
        with unittest.mock.patch.object(private, "waiting", lambda *a, **k: why[0]), \
                unittest.mock.patch.object(private, "builtin_present", lambda cap, repo=None: False), \
                unittest.mock.patch.object(private, "load_pin", lambda *a, **k: {"modules": {"accounts": {}}}):   # a release without it, with its pin
            s = self.stand()
            self.request(s)
            self.assertEqual(self.spawned, [])
            self.assertIn("a", s.hub.waiting())
            why[0] = None   # installed and came up
            s.hub.run_delayed()
            self.assertEqual(self.spawned, [])   # postponed by a step
            s.until(lambda: (s.hub.run_delayed(), self.spawned)[1], timeout=10)
        self.assertEqual(self.spawned, ["a"])
        self.assertEqual(s.events("owner.notify"), [])

    def test_at_most_the_wait_then_on_the_machine_login_with_one_line(self):
        from modules.update import private
        now = [time.time()]
        with unittest.mock.patch.object(private, "waiting", lambda *a, **k: "the accounts module is starting"), \
                unittest.mock.patch.object(private, "builtin_present", lambda cap, repo=None: False), \
                unittest.mock.patch.object(private, "load_pin", lambda *a, **k: {"modules": {"accounts": {}}}):
            s = self.stand(clock=lambda: now[0])
            self.request(s, "a")
            self.request(s, "b")
            now[0] += private.PRIVATE_WAIT - 1
            s.hub.run_delayed()
            self.assertEqual(self.spawned, [])
            now[0] += 2
            s.hub.run_delayed()
        self.assertEqual(sorted(self.spawned), ["a", "b"])
        notes = [e["data"] for e in s.events("owner.notify")]
        self.assertEqual(len(notes), 1)
        self.assertIn("sessions sign in through this machine's Claude Code", notes[0]["text"])


class UpdateGate(unittest.TestCase):
    """needs-module in modules/update (4.4): a target whose private.json has required is not merged until the needed function
    is with the module; the check is repeated under the lock right before the fast-forward."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="privgate-")
        self.addCleanup(platforms.current.rmtree, self.root)
        self.state = os.path.join(self.root, "state")
        os.makedirs(os.path.join(self.state, "spool"))
        conf = os.path.join(self.root, "conf")
        os.makedirs(conf)
        env = unittest.mock.patch.dict(os.environ, {"HUB_CONFIG_DIR": conf})
        env.start()
        self.addCleanup(env.stop)
        self.up = os.path.join(self.root, "up.git")
        git(self.root, "init", "-q", "--bare", "-b", "main", self.up)
        self.author = os.path.join(self.root, "author")
        git(self.root, "clone", "-q", self.up, self.author)
        self.commit("core.py", "a = 1\n")
        self.mine = os.path.join(self.root, "mine")
        git(self.root, "clone", "-q", self.up, self.mine)
        pin = {"repo": "acme/team-modules", "url": "/nowhere.git", "ref": "pm-v1.0.0", "commit": "a" * 40,
               "modules": {n: {"subdir": n, "cap": c} for n, c in MODS.items()}, "required": True}
        self.r2 = self.commit("modules/private.json", json.dumps(pin))

    def commit(self, name, text):
        path = os.path.join(self.author, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        git(self.author, "add", "-A")
        git(self.author, "commit", "-qm", f"change {name}")
        git(self.author, "push", "-q", "origin", "main")
        return git(self.author, "rev-parse", "HEAD")

    def updater(self, tests=None):
        from modules.update import update
        cfg = {"update": {"enabled": True, "remote": "origin", "branch": "main", "channel": "main", "delay_hours": 0},
               "dev": {"name": "hub-dev"}, "stats": {"enabled": True}}
        return update.Updater(self.mine, self.state, cfg, tests=tests or (lambda repo: (True, "OK")))

    def has(self, sha):
        return subprocess.run(["git", "merge-base", "--is-ancestor", sha, "HEAD"], cwd=self.mine).returncode == 0

    def items(self):
        out = []
        for path, item in spool.take(os.path.join(self.state, "spool")):
            os.unlink(path)
            out.append(item)
        return out

    def test_release_without_builtin_waits_for_the_module(self):
        u = self.updater()
        self.assertEqual(u.check(), "needs-module")
        self.assertFalse(self.has(self.r2))
        items = self.items()
        (c,) = [i["data"] for i in items if i["type"] == "update.checked"]
        self.assertEqual((c["result"], c["need_module"]), ("needs-module", ["stats: the module is not installed"]))
        (note,) = [i["data"] for i in items if i["type"] == "owner.notify"]
        self.assertEqual(note["id"], "needs-module")
        self.assertEqual(self.updater().check(), "needs-module")   # the same reason: the line is not repeated
        self.assertEqual([i for i in self.items() if i["type"] == "owner.notify"], [])

    def test_the_gate_reads_the_target_with_a_git_path_on_windows_too(self):
        # 26.10, Windows update stand: PIN is an OS path (modules\private.json there), and `git show <sha>:modules\...`
        # found nothing, so the gate saw no private.json and let the release through
        from modules.update import private
        with unittest.mock.patch.object(private, "PIN", "modules\\private.json"):
            self.assertEqual(self.updater().check(), "needs-module")
        self.assertFalse(self.has(self.r2))

    def test_module_ready_lets_it_in(self):
        from core import providers
        from modules.update import private
        with open(os.path.join(self.state, private.STATE), "w") as f:
            json.dump({}, f)
        # the stats module is installed (source in settings) and runs the report
        u = self.updater()
        u.cfg["modules"] = {"sources": [{"git": "/nowhere.git", "subdir": "stats", "commit": "a" * 40}],
                            "enabled": ["stats"]}
        providers.write(self.state, {"stats": {"state": "module", "module": "stats"}})
        self.assertEqual(u.check(), "applied")
        self.assertTrue(self.has(self.r2))

    def test_gate_is_checked_again_right_before_the_fast_forward(self):
        from core import providers
        u = self.updater()
        u.cfg["modules"] = {"sources": [{"git": "/nowhere.git", "subdir": "stats", "commit": "a" * 40}],
                            "enabled": ["stats"]}
        providers.write(self.state, {"stats": {"state": "module", "module": "stats"}})
        head = git(self.mine, "rev-parse", "HEAD")

        def tests_then_module_falls_back(repo):   # while the tests ran, the module failed: the report is back with the built-in
            providers.write(self.state, {"stats": {"state": "builtin", "module": "stats"}})
            return True, "OK"

        u.tests = tests_then_module_falls_back
        self.assertEqual(u.check(), "needs-module")
        self.assertEqual(git(self.mine, "rev-parse", "HEAD"), head)


if __name__ == "__main__":
    unittest.main()
