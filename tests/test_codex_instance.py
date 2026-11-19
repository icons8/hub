"""Another hub of the machine on Codex (the spec of several hubs, 2.12, slice M9): its program codex — the machine's
one when there is one fit for the hub, else its own copy in <state>/bin (never into PATH: the main hub's critic would
start taking the personal ~/.codex), a running codex.exe replaced by renaming it aside; the host finds the hub's own
copy first; the critic and the voice take the personal ~/.codex only where they did before (a Claude hub on the shared
sign-in); `hub doctor` says when a Codex sign-in is a copy of another hub's. No model and no network."""
import tests  # noqa: F401 — keep this machine's settings out
import json
import os
import tempfile
import unittest
from unittest import mock

import platforms
from core import codexinstall, codexmodel, i18n
from tests.fakes import can
from tests.test_setup_app import Served, tarball
from tests.fakes import cut


def write(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


class Fake:
    """codexinstall.Installer without a download or a process: the versions its codex files answer, what was installed."""

    def __init__(self, test, versions, triple="x86_64-unknown-linux-musl"):
        self.versions, self.asked, self.installed = versions, [], []
        self.triple, self.plat = triple, "linux-x64"
        self.pin = {"version": "0.162.1", "assets": {"x86_64-unknown-linux-musl": ("a.tar.gz", 1, "0" * 64)}}
        self.test = test

    def version_of(self, exe):
        self.asked.append(exe)
        return self.versions.get(exe)

    def run_install(self):
        dest = codexinstall.own_place(self.test.state)
        write(dest, "codex")
        self.installed.append(dest)
        return "0.162.1", dest


def whole(path):
    """A codex of the hub's with the helper it starts for its tools and its sandbox beside it."""
    write(path, "codex")
    for beside in codexinstall.needs(path):
        write(beside, "host")
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="hubcxinst-"))
        self.addCleanup(platforms.current.rmtree, self.tmp)
        self.state = os.path.join(self.tmp, "state")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.state)
        os.makedirs(self.home)
        self.own = codexinstall.own_place(self.state)
        self.local = codexinstall.place(self.home)

    def which(self, found):
        return lambda name, **kw: found


class ForInstance(Base):
    def take(self, versions, which=None, **kw):
        self.fake = Fake(self, versions)
        return codexinstall.for_instance(self.state, home=self.home, which=self.which(which), installer=self.fake, **kw)

    def test_the_machines_codex_in_local_bin_is_taken_as_it_is(self):
        whole(self.local)
        self.assertEqual(self.take({self.local: "0.162.1"}), ("shared", self.local))
        self.assertFalse(os.path.lexists(self.own))
        self.assertEqual(self.fake.installed, [])

    def test_one_found_elsewhere_in_path_gets_a_link_in_the_hubs_folder(self):
        # the person's codex (Homebrew) is in their PATH, not in the PATH of the hub's service; the link is to the
        # command PATH found, not to its real file (Homebrew's Cellar folder goes with the next upgrade)
        if not can.symlink():
            self.skipTest("no right to make links here")
        real = write(os.path.join(self.tmp, "Cellar", "codex", "0.170.0", "bin", "codex"), "\x7fELF")
        theirs = os.path.join(self.tmp, "brew", "bin", "codex")
        os.makedirs(os.path.dirname(theirs))
        os.symlink(real, theirs)   # portable: the test is skipped above where no link can be made
        with mock.patch.object(platforms.current, "APP_OS", "linux"):
            self.assertEqual(self.take({theirs: "0.170.0"}, which=theirs), ("linked", theirs))
        self.assertEqual(os.readlink(self.own), theirs)
        self.assertEqual(self.fake.installed, [])

    def test_a_script_found_in_path_is_not_linked(self):
        # npm's codex is a node script: the hub's service has no node in PATH — its own copy instead
        theirs = write(os.path.join(self.tmp, "npm", "bin", "codex"), "#!/usr/bin/env node\n")
        with mock.patch.object(platforms.current, "APP_OS", "linux"):
            self.assertEqual(self.take({theirs: "0.170.0"}, which=theirs), ("own", self.own))
        self.assertEqual(self.fake.installed, [self.own])

    def test_an_unfit_copy_of_its_own_goes_before_the_machines_is_taken(self):
        # the host looks at <state>/bin before PATH: an old copy left there would run instead of the fit one
        write(self.own, "old")
        whole(self.local)
        self.assertEqual(self.take({self.own: "0.150.0", self.local: "0.162.1"}), ("shared", self.local))
        self.assertFalse(os.path.lexists(self.own))

    def test_on_windows_the_scheduler_task_has_the_persons_path(self):
        theirs = write(os.path.join(self.tmp, "npm", "codex.cmd"), "codex")
        with mock.patch.object(platforms.current, "APP_OS", "windows"):
            self.assertEqual(self.take({theirs: "0.170.0"}, which=theirs), ("shared", theirs))
        self.assertFalse(os.path.lexists(self.own))

    def test_none_or_an_older_one_the_pinned_release_into_the_hubs_folder(self):
        self.assertEqual(self.take({}), ("own", self.own))
        self.assertEqual(self.fake.installed, [self.own])
        os.remove(self.own)
        old = write(self.local, "old")
        self.assertEqual(self.take({self.local: "0.150.0"}), ("own", self.own))
        with open(old, encoding="utf-8") as f:
            self.assertEqual(f.read(), "old")   # the machine's own is never put over by another hub

    def test_a_copy_of_its_own_stays_when_nothing_replaces_it(self):
        # its version did not come (a slow disk once) and nothing else can be had: the copy is not deleted for nothing
        write(self.own, "codex")
        self.fake = Fake(self, {}, triple=None)
        with self.assertRaises(codexinstall.Failed):
            codexinstall.for_instance(self.state, home=self.home, which=self.which(None), installer=self.fake)
        self.assertTrue(os.path.isfile(self.own))

    def test_its_own_copy_already_there_is_kept(self):
        whole(self.own)
        whole(self.local)
        self.assertEqual(self.take({self.own: "0.162.1", self.local: "0.162.1"}), ("own", self.own))
        self.assertEqual(self.fake.installed, [])

    def test_a_copy_of_the_hubs_without_the_helper_for_its_tools_is_not_taken(self):
        # a codex the first release put: it answers --version, but every tool call of a session fails without the helper
        write(self.local, "codex")
        self.assertEqual(self.take({self.local: "0.162.1"}), ("own", self.own))
        self.assertEqual(self.fake.installed, [self.own])
        os.remove(self.own)
        whole(self.local)   # the same copy once its helper is there: taken as it is
        self.assertEqual(self.take({self.local: "0.162.1"}), ("shared", self.local))

    def test_a_dry_run_runs_nothing_and_writes_nothing(self):
        # `hub instance new … --dry-run` (the page's check): not even `codex --version` — its CODEX_HOME would be made
        os.rmdir(self.state)
        self.assertEqual(self.take({}, dry=True), ("install", self.own))
        write(self.local, "codex")
        self.assertEqual(self.take({}, dry=True), ("shared", self.local))
        self.assertEqual(self.fake.asked, [])
        self.assertFalse(os.path.exists(self.state))

    def test_a_machine_codex_has_no_build_for_is_refused(self):
        self.fake = Fake(self, {}, triple=None)
        with self.assertRaises(codexinstall.Failed) as e:
            codexinstall.for_instance(self.state, home=self.home, which=self.which(None), installer=self.fake)
        self.assertEqual(e.exception.why, "platform")


class OwnPlace(Base):
    def test_the_pinned_release_goes_into_the_hubs_own_folder(self):
        import hashlib
        triple = codexinstall.target()
        if triple is None:
            self.skipTest("no Codex build for this machine")
        name = codexinstall.PIN["assets"][triple][0]
        root = os.path.join(self.tmp, "releases", "rust-v0.162.1")
        os.makedirs(root)
        data = open(tarball(os.path.join(root, name), name[: -len(".tar.gz")]), "rb").read()
        pin = {"version": "0.162.1", "assets": {triple: (name, len(data), hashlib.sha256(data).hexdigest())}}
        ran = []

        def run(cmd, **kw):
            ran.append((cmd, kw["env"]["CODEX_HOME"]))
            import subprocess
            return subprocess.CompletedProcess(cmd, 0, "codex-cli 0.162.1", "")
        inst = codexinstall.Installer(self.state, root_url=Served(self, os.path.dirname(root)).url, home=self.home,
                                      run=run, pin=pin, dest=self.own)
        self.assertEqual(inst.run_install(), ("0.162.1", self.own))
        self.assertFalse(os.path.exists(self.local))   # nothing new in PATH
        self.assertEqual(ran[-1], ([self.own, "--version"], os.path.join(self.state, "codex-home")))
        self.assertTrue(codexinstall.mine(self.own, home=self.home, state=self.state))
        self.assertFalse(codexinstall.mine(self.own, home=self.home))


class PutOver(Base):
    def test_a_running_codex_exe_is_renamed_aside_and_the_old_one_goes_next_time(self):
        dest = write(self.local, "old")
        part = write(dest + ".part", "new")

        def held(src, dst):   # Windows: a running image is not replaced, it is renamed
            raise PermissionError(13, "in use")
        codexinstall.put_over(part, dest, replace=held)
        with open(dest, encoding="utf-8") as f:
            self.assertEqual(f.read(), "new")
        with open(dest + ".old", encoding="utf-8") as f:
            self.assertEqual(f.read(), "old")
        codexinstall.put_over(write(dest + ".part", "newer"), dest, replace=os.replace)
        self.assertFalse(os.path.exists(dest + ".old"))
        with open(dest, encoding="utf-8") as f:
            self.assertEqual(f.read(), "newer")

    def test_a_second_install_while_both_still_run_takes_another_name(self):
        # two updates under a live session: the first .old is still held — the next one goes beside it
        dest = write(self.local, "v2")
        write(dest + ".old", "v1")

        def held(src, dst):
            raise PermissionError(13, "in use")

        def remove(path):
            raise PermissionError(13, "in use")
        with mock.patch.object(codexinstall.os, "remove", remove):
            codexinstall.put_over(write(dest + ".part", "v3"), dest, replace=held)
        for name, text in ((dest, "v3"), (dest + ".old", "v1"), (dest + ".old1", "v2")):
            with open(name, encoding="utf-8") as f:
                self.assertEqual(f.read(), text)
        codexinstall.put_over(write(dest + ".part", "v4"), dest, replace=os.replace)
        self.assertEqual(sorted(os.listdir(os.path.dirname(dest))), ["codex" + codexinstall._suffix()])

    def test_a_failed_move_puts_the_old_one_back(self):
        dest = write(self.local, "old")
        part = write(dest + ".part", "new")
        moves = []

        def rename(src, dst):
            moves.append((src, dst))
            if src == part:
                raise OSError(5, "denied")
            os.replace(src, dst)

        def held(src, dst):
            raise PermissionError(13, "in use")
        with self.assertRaises(OSError):
            codexinstall.put_over(part, dest, replace=held, rename=rename)
        with open(dest, encoding="utf-8") as f:
            self.assertEqual(f.read(), "old")
        self.assertFalse(os.path.exists(dest + ".old"))


class CodexBin(Base):
    def test_the_hubs_own_copy_before_path(self):
        from hosts.codex import codex_bin
        theirs = write(os.path.join(self.tmp, "path", "codex"), "codex")
        with mock.patch.dict(os.environ, {"HUB_STATE_DIR": self.state}), \
                mock.patch("hosts.codex.shutil.which", lambda name, path=None: theirs):
            os.environ.pop("HUB_CODEX_BIN", None)
            self.assertEqual(codex_bin(), theirs)
            write(self.own, "codex")
            self.assertEqual(codex_bin(), self.own)
            os.environ["HUB_CODEX_BIN"] = "/x/codex"
            self.assertEqual(codex_bin(), "/x/codex")


class SideHome(Base):
    """critic and voice: the personal ~/.codex (None) only for a Claude hub on the shared sign-in."""

    def side(self, cfg):
        return codexmodel.side_home(self.state, cfg=cfg)

    def test_who_takes_which_folder(self):
        hub = os.path.join(self.state, "codex-home")
        # the hub folder, not a Codex pool account's (that one needs its own arguments)
        self.enterContext(mock.patch("core.codexcli.active_home", lambda state: os.path.join(state, "moddata", "a1")))
        self.assertIsNone(self.side({}))
        self.assertEqual(self.side({"host": "codex"}), hub)
        self.assertEqual(self.side({"claude_config_dir": "/x/claude"}), "")   # its own Claude, no Codex sign-in
        write(os.path.join(hub, "auth.json"), "{}")
        self.assertEqual(self.side({"claude_config_dir": "/x/claude"}), hub)
        self.assertIsNone(self.side({}))   # a sign-in in its folder changes nothing for the shared one


class Critic(Base):
    def review(self, cfg):
        from modules.critic import critic
        d = tempfile.mkdtemp(dir=self.tmp)
        draft = write(os.path.join(d, "draft.md"), "a plan")
        calls = []

        def run(cmd, cwd, stdin=None, env=None):
            calls.append((cmd, env))
            return "no objections"
        with mock.patch.dict(os.environ, {"HUB_STATE_DIR": self.state}), \
                mock.patch.object(critic.shutil, "which", lambda n, **k: "/x/" + n), \
                mock.patch.object(codexmodel, "binary", lambda: "/x/own-codex"), \
                mock.patch.object(codexmodel, "config", lambda state_dir=None: cfg):
            who, _ = critic.review({"critic": {"enabled": True}, **cfg}, draft, run=run)
        return who, calls

    def test_the_shared_sign_in_keeps_the_personal_codex(self):
        who, calls = self.review({})
        self.assertTrue(who.startswith("Codex"))
        self.assertEqual(calls[0][0][0], "/x/codex")
        self.assertIsNone(calls[0][1])   # the critic's own environment, as before: the personal ~/.codex

    def test_its_own_claude_sign_in_takes_its_own_codex_or_claude(self):
        own = {"claude_config_dir": os.path.join(self.tmp, "claude")}
        who, calls = self.review(own)
        self.assertTrue(who.startswith("Claude"), who)
        self.assertIn(i18n.t("cli.critic.no_codex_login", cmd="hub"), who)
        self.assertNotEqual(calls[0][0][0], "/x/codex")
        write(os.path.join(self.state, "codex-home", "auth.json"), "{}")
        who, calls = self.review(own)
        self.assertTrue(who.startswith("Codex"), who)
        self.assertEqual(calls[0][0][0], "/x/own-codex")
        self.assertEqual(calls[0][1]["CODEX_HOME"], os.path.join(self.state, "codex-home"))


class Voice(Base):
    @cut.needs("speech")
    def test_the_voice_takes_the_hubs_folder_by_the_same_rule(self):
        from modules.voice import speak
        got = {}

        class Stop(Exception):
            pass

        def rpc(exe, env=None):
            got.update(exe=exe, env=env)
            raise Stop()

        class Page:
            def __init__(self, **kw):
                pass

            def goto(self, *a, **kw):
                pass

            def js(self, *a):
                return "sdp"

            def close(self):
                pass
        from modules.proof import cdp
        for cfg, home in (({}, None), ({"host": "codex"}, os.path.join(self.state, "codex-home"))):
            got.clear()
            with mock.patch.dict(os.environ, {"HUB_NO_MODEL": "", "HUB_STATE_DIR": self.state}), \
                    mock.patch.object(speak, "codex", lambda: "/x/codex"), mock.patch.object(speak, "Rpc", rpc), \
                    mock.patch.object(codexmodel, "config", lambda state_dir=None, c=cfg: c), \
                    mock.patch.object(cdp, "Page", Page):
                with self.assertRaises(Stop):
                    speak.realtime("Done.", os.path.join(self.tmp, "x.webm"), speak.settings({}))
            self.assertEqual(got["exe"], "/x/codex")
            self.assertEqual(got["env"] and got["env"]["CODEX_HOME"], home)
        # its own Claude sign-in and no Codex sign-in of its own: no voice, with the reason — never the personal one
        with mock.patch.dict(os.environ, {"HUB_NO_MODEL": "", "HUB_STATE_DIR": self.state}), \
                mock.patch.object(speak, "codex", lambda: "/x/codex"), mock.patch.object(speak, "Rpc", rpc), \
                mock.patch.object(codexmodel, "config", lambda state_dir=None: {"claude_config_dir": "/x/claude"}):
            with self.assertRaises(speak.SpeakError) as e:
                speak.realtime("Done.", os.path.join(self.tmp, "x.webm"), speak.settings({}))
            self.assertIn(i18n.t("voice.speak.no_hub_codex_login", cmd="hub"), str(e.exception))


class SetupCodex(Base):
    def test_an_instance_puts_its_own_copy_over_a_foreign_one(self):
        # `hub-<name> setup codex`: the host looks at <state>/bin first, so a foreign older codex in PATH is no reason
        # to refuse there (the main hub still refuses: a copy in ~/.local/bin would never run)
        from core import instance
        foreign = write(os.path.join(self.tmp, "npm", "codex"), "codex")
        put = []
        with mock.patch("hosts.codex.codex_bin", lambda state_dir=None: foreign), \
                mock.patch.object(codexinstall.Installer, "version_of", lambda self, e: "0.150.0"), \
                mock.patch.object(codexinstall.Installer, "run_install",
                                  lambda self: put.append(self.dest) or ("0.162.1", self.dest)), \
                mock.patch.object(instance, "load", lambda state: {"name": "maps"}):
            self.assertEqual(codexinstall.main([], self.state), 0)
        self.assertEqual(put, [self.own])


class PendingSignIn(Base):
    def test_the_hubs_removal_stops_a_sign_in_still_waiting_for_its_code(self):
        # the page's sign-in runs apart from hub-up; on Windows its codex.exe held the folder `--purge` deletes (probe
        # on the ordinary user, 08.11). By the number and the start time it wrote; without a start time — left alone
        import subprocess
        import sys
        from core import codexcli, uninstall
        from tests.fakes import procs
        child = platforms.current.popen_detached([sys.executable, "-c", "import time; time.sleep(60)"],
                                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                 stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: child.poll() is None and platforms.current.kill_detached(child, hard=True))
        os.makedirs(codexcli.login_dir(self.state))
        codexcli._write(self.state, "pid", {"pid": child.pid, "ts": 1})
        self.assertFalse(codexcli.stop_login(self.state))
        self.assertIsNone(child.poll())
        codexcli._write(self.state, "pid", {"pid": child.pid, "ts": 1, "born": platforms.current.process_start(child.pid)})
        uninstall.stop_codex_login(self.state)
        child.wait(20)
        self.assertFalse(procs.alive(child.pid))


class CopiedSignIn(Base):
    def test_a_sign_in_copied_from_another_hub_is_named(self):
        from core import instance, modcli
        other = os.path.join(self.tmp, "other")
        reg = os.path.join(self.tmp, "registry")

        def auth(state, token):
            write(os.path.join(state, "codex-home", "auth.json"), json.dumps({"tokens": {"refresh_token": token}}))
        auth(self.state, "rt-1")
        auth(other, "rt-2")
        entries = [{"name": "cx", "state": other, "gone": False}, {"name": "me", "state": self.state, "gone": False}]
        with mock.patch.object(instance, "entries", lambda folder=None: entries), \
                mock.patch.object(instance, "main_state", lambda: os.path.join(self.tmp, "main")):
            self.assertIsNone(instance.codex_copy_of(self.state, reg))
            self.assertEqual(modcli.codex_copy_rows(self.state), [])
            auth(other, "rt-1")
            self.assertEqual(instance.codex_copy_of(self.state, reg), "hub-cx")
            rows = modcli.codex_copy_rows(self.state)
            self.assertEqual([r[0] for r in rows], ["fail"])
            self.assertIn("hub-cx", rows[0][2])
            self.assertIn("hub codex login", rows[0][2])   # this hub's own command: the main one here
            platforms.current.rmtree(os.path.join(self.state, "codex-home"))
            self.assertIsNone(instance.codex_copy_of(self.state, reg))   # no sign-in of its own: nothing to compare


if __name__ == "__main__":
    unittest.main()
