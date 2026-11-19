"""The Linux package, version 2 (installer spec S7): the .deb carries the release factory's shell (tools/package/build.py:
runtime/, code-seed/, PACKAGED, launch.py) as it is; `agents-hub` runs the shell's launcher (the code becomes a clone of
the seed in the user's folder), then the hub. Chrome for Testing is not in the package: where Ubuntu 23.10+ forbids the
sandbox to a program without an AppArmor profile, a root helper puts Chrome into /opt/agents-hub/chrome (a folder a user
cannot write) and gives exactly that path a `userns` profile; a profile on a path a user writes would let any program
past the restriction, and `--no-sandbox` is not for a browser that reads whatever the agents open.
No network, no root, no AppArmor here: the downloads are zips built by the test, the loader is a recording stub."""
import tests  # noqa: F401 - keeps the machine's settings out
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

from core import packaged, runtime
from modules.setup import chrome, setup
from tests import winsafe
from tests.fakes import lang

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(REPO, "tools", "package")
DEB = os.path.join(PKG, "deb")
POSIX = os.name == "posix"


def g(cwd, *a):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


@unittest.skipUnless(POSIX, "the .deb is Linux")
class Shell(unittest.TestCase):
    """/opt/agents-hub as the .deb lays it out (a copy of the factory's shell + lib/): first_run.py at its place."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hub-deb2-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.opt = os.path.join(self.tmp, "opt", "agents-hub")
        self.home = os.path.join(self.tmp, "home")
        up = os.path.join(self.tmp, "up.git")
        work = os.path.join(self.tmp, "work")
        g(self.tmp, "init", "-q", "--bare", "-b", "main", up)
        g(self.tmp, "init", "-q", "-b", "main", work)
        for name, body in (("hub-home/CLAUDE.md", "rules\n"), ("hub-home/ONBOARDING.md", "hello\n"), ("bin/hub-up", "x\n")):
            os.makedirs(os.path.dirname(os.path.join(work, name)), exist_ok=True)
            with open(os.path.join(work, name), "w") as f:
                f.write(body)
        for pkg in ("core", "platforms", "i18n"):   # what the launcher's one step in the code folder imports (hub.json's lock)
            shutil.copytree(os.path.join(REPO, pkg), os.path.join(work, pkg), ignore=shutil.ignore_patterns("__pycache__"))
        g(work, "remote", "add", "origin", up)
        g(work, "add", "-A")
        g(work, "commit", "-qm", "v1")
        g(work, "tag", "-a", "-m", "v1.0.0", "v1.0.0")
        g(work, "push", "-q", "origin", "main", "v1.0.0")
        os.makedirs(os.path.join(self.opt, "lib"))
        g(self.tmp, "clone", "-q", "--depth", "1", "--branch", "v1.0.0", "file://" + up, os.path.join(self.opt, "code-seed"))
        g(os.path.join(self.opt, "code-seed"), "remote", "set-url", "origin", "https://example.test/core.git")
        shutil.copy(os.path.join(PKG, "first_run.py"), os.path.join(self.opt, "lib"))
        shutil.copy(os.path.join(PKG, "launch.py"), self.opt)
        with open(os.path.join(self.opt, "PACKAGED"), "w") as f:
            json.dump({"format": "app", "version": "1.0.0", "runtime": 1, "shared": os.path.join(self.opt, "chrome")}, f)
        os.makedirs(os.path.join(self.opt, "bin"))
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("HUB_")}
        self.env.update(HOME=self.home, TZ="Europe/Paris")
        os.makedirs(self.home)

    def first_run(self, *args):
        return subprocess.run([sys.executable, os.path.join(self.opt, "lib", "first_run.py"), *args], env=self.env,
                              capture_output=True, text=True, timeout=120, cwd=self.tmp)

    def test_the_first_run_makes_the_code_a_clone_of_the_seed_and_prints_where(self):
        r = self.first_run()
        self.assertEqual(r.returncode, 0, r.stderr)
        code = os.path.join(self.home, ".hub-core", "code")
        self.assertEqual(r.stdout.strip(), code)                    # the shell script runs hub-up from there
        self.assertEqual(g(code, "describe", "--tags"), "v1.0.0")
        self.assertEqual(g(code, "remote", "get-url", "origin"), "https://example.test/core.git")
        with open(os.path.join(code, "PACKAGED")) as f:
            self.assertEqual(json.load(f)["shared"], os.path.join(self.opt, "chrome"))

    def test_settings_point_to_the_shell_and_the_session_rules_to_the_code(self):
        self.first_run()
        with open(os.path.join(self.home, ".config", "hub-core", "hub.json")) as f:
            cfg = json.load(f)
        self.assertEqual(cfg["browser"]["mcp"], [os.path.join(self.opt, "bin", "chrome-devtools-mcp")])
        self.assertEqual(cfg["voice"]["ffmpeg"], os.path.join(self.opt, "bin", "ffmpeg"))
        link = os.path.join(self.home, ".hub-core", "hub-home", "CLAUDE.md")
        self.assertEqual(os.readlink(link), os.path.join(self.home, ".hub-core", "code", "hub-home", "CLAUDE.md"))
        self.assertEqual(read(link), "rules\n")

    def test_under_xdg_config_home_the_settings_and_the_state_mark_go_where_the_hub_reads_them(self):
        # the hub reads hub.json from $XDG_CONFIG_HOME/hub-core, and so do the installers' mark and the readers of it
        xdg = os.path.join(self.tmp, "xdg")
        self.env["XDG_CONFIG_HOME"] = xdg
        r = self.first_run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isfile(os.path.join(xdg, "hub-core", "hub.json")))
        self.assertEqual(read(xdg, "hub-core", "state-dir").strip(), os.path.join(self.home, ".hub-core"))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config", "hub-core")))

    def test_a_blank_mark_in_dot_config_is_no_mark(self):
        mark = os.path.join(self.home, ".config", "hub-core", "state-dir")
        os.makedirs(os.path.dirname(mark))
        open(mark, "w").close()
        self.env["XDG_CONFIG_HOME"] = os.path.join(self.tmp, "xdg")
        self.assertEqual(self.first_run().returncode, 0)
        self.assertEqual(read(self.tmp, "xdg", "hub-core", "state-dir").strip(), os.path.join(self.home, ".hub-core"))

    def test_a_mark_of_an_earlier_start_in_dot_config_is_not_shadowed_by_a_new_one(self):
        # readers look under XDG first: a second mark there, naming the default folder, would win over the earlier one
        mark = os.path.join(self.home, ".config", "hub-core", "state-dir")
        os.makedirs(os.path.dirname(mark))
        with open(mark, "w") as f:
            f.write("/home/x/chosen\n")
        self.env["XDG_CONFIG_HOME"] = os.path.join(self.tmp, "xdg")
        self.assertEqual(self.first_run().returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "xdg", "hub-core", "state-dir")))

    def test_an_update_of_the_code_survives_the_next_start_and_nothing_is_rewritten(self):
        self.first_run()
        code = os.path.join(self.home, ".hub-core", "code")
        with open(os.path.join(code, "hub-home", "CLAUDE.md"), "w") as f:
            f.write("rules of a newer release\n")                     # an update moved the code in place
        cfg = os.path.join(self.home, ".config", "hub-core", "hub.json")
        before = os.stat(cfg).st_mtime_ns
        r = self.first_run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(read(self.home, ".hub-core", "hub-home", "CLAUDE.md"), "rules of a newer release\n")
        self.assertEqual(os.stat(cfg).st_mtime_ns, before)

    def test_a_hub_of_the_first_version_of_the_package_moves_to_the_clone(self):
        # version 1 linked the rules to /opt/agents-hub/app/hub-home; there is no app/ any more
        hh = os.path.join(self.home, ".hub-core", "hub-home")
        os.makedirs(hh)
        winsafe.symlink(self, os.path.join(self.opt, "app", "hub-home", "CLAUDE.md"), os.path.join(hh, "CLAUDE.md"))
        self.first_run()
        self.assertEqual(read(hh, "CLAUDE.md"), "rules\n")

    def test_a_rules_file_the_person_wrote_in_place_of_the_link_stays(self):
        hh = os.path.join(self.home, ".hub-core", "hub-home")
        os.makedirs(hh)
        with open(os.path.join(hh, "CLAUDE.md"), "w") as f:
            f.write("my own rules\n")
        self.first_run()
        self.assertFalse(os.path.islink(os.path.join(hh, "CLAUDE.md")))
        self.assertEqual(read(hh, "CLAUDE.md"), "my own rules\n")
        self.assertTrue(os.path.islink(os.path.join(hh, "ONBOARDING.md")))        # the other one is linked as usual

    def test_an_upgraded_package_forgets_the_chrome_it_no_longer_carries(self):
        # the first package carried Chrome for Testing in /opt/agents-hub/chrome and wrote its path into hub.json once
        cfg_dir = os.path.join(self.home, ".config", "hub-core")
        os.makedirs(cfg_dir)
        old = os.path.join(self.opt, "chrome", "chrome")
        mine = {"browser": {"chrome": old, "mcp": ["m"]}, "language": "ru", "telegram": {"token": "t"},
                "browsers": {"local": {"kind": "local",
                                       "description": lang.legacy("package_chrome")}}}
        path = os.path.join(cfg_dir, "hub.json")
        with open(path, "w") as f:
            json.dump(mine, f)
        os.chmod(path, 0o600)
        r = self.first_run()
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(path) as f:
            got = json.load(f)
        self.assertEqual(got["browser"], {"mcp": ["m"]})
        self.assertEqual((got["language"], got["telegram"]), ("ru", {"token": "t"}))
        self.assertEqual(got["browsers"]["local"]["description"],
                         "The hub's Chrome for Testing: its own profile, without your sign-ins")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)                 # it holds tokens
        own = {"browser": {"chrome": "/opt/google/chrome/chrome"}}              # a Chrome the owner chose stays
        with open(path, "w") as f:
            json.dump(own, f)
        self.first_run()
        with open(path) as f:
            self.assertEqual(json.load(f), own)

    def test_a_broken_seed_is_said_in_words_and_the_exit_code_is_not_zero(self):
        shutil.rmtree(os.path.join(self.opt, "code-seed"))
        r = self.first_run()
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("code-seed", r.stderr)


@unittest.skipUnless(POSIX, "the .deb is Linux")
class SharedChrome(unittest.TestCase):
    """The root helper: Chrome into <opt>/chrome/<version>/…, a profile for exactly that program."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hub-deb2-chrome-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.opt = os.path.join(self.tmp, "opt", "agents-hub")
        self.apparmor = os.path.join(self.tmp, "etc", "apparmor.d")
        os.makedirs(os.path.join(self.apparmor, "abi"))
        open(os.path.join(self.apparmor, "abi", "4.0"), "w").close()
        os.makedirs(self.opt)
        self.loaded = []
        self.zip_path = os.path.join(self.tmp, "chrome.zip")

    def make_zip(self, entries=None, body="#!/bin/sh\necho 'Google Chrome for Testing 155.0.8059.39'\n"):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name, data, mode in entries or [("chrome-linux64/", b"", 0o40755), ("chrome-linux64/chrome", body.encode(), 0o100775),
                                                ("chrome-linux64/resources.pak", b"pak", 0o100666)]:
                info = zipfile.ZipInfo(name)
                info.external_attr = mode << 16
                z.writestr(info, data)
        with open(self.zip_path, "wb") as f:
            f.write(buf.getvalue())
        import hashlib
        return {"version": "155.0.8059.39", "url": "https://example.test/chrome.zip", "size": len(buf.getvalue()),
                "sha256": hashlib.sha256(buf.getvalue()).hexdigest()}

    def install(self, pin, **kw):
        def download(url, dest, sha, size, what, again, **_):
            shutil.copy(self.zip_path, dest)
            return dest
        kw.setdefault("root_only", False)
        with mock.patch("core.fetch.download", download):
            return chrome.install_shared(self.opt, pin, apparmor_dir=self.apparmor,
                                         load=lambda path: self.loaded.append(path) or True, **kw)

    def test_chrome_goes_under_the_package_folder_and_nobody_but_root_can_write_there(self):
        pin = self.make_zip()
        exe = self.install(pin)
        self.assertEqual(exe, os.path.join(self.opt, "chrome", "155.0.8059.39", "chrome-linux64", "chrome"))
        for top, dirs, files in os.walk(os.path.join(self.opt, "chrome")):
            for name in dirs + files:
                mode = os.lstat(os.path.join(top, name)).st_mode
                self.assertFalse(mode & (stat.S_IWGRP | stat.S_IWOTH), os.path.join(top, name))
        self.assertTrue(os.access(exe, os.X_OK))
        with open(os.path.join(self.opt, "chrome", "chrome.json")) as f:
            self.assertEqual(json.load(f), {"version": "155.0.8059.39",
                                            "path": "155.0.8059.39/chrome-linux64/chrome"})

    def test_the_profile_names_the_one_program_and_nothing_a_user_could_write(self):
        exe = self.install(self.make_zip())
        text = read(self.apparmor, "agents-hub-chrome")
        self.assertIn(f"profile agents-hub-chrome {exe} flags=(unconfined)", text)
        self.assertIn("userns,", text)
        self.assertIn("abi <abi/4.0>,", text)
        # a pattern or a path of the user's would open the restriction to anything that can be put there
        named = [line.split(" ")[2] for line in text.splitlines() if line.startswith("profile ")]
        self.assertEqual(named, [exe])
        self.assertNotRegex(exe.replace(self.tmp, ""), r"[*?{}\[\],^]")
        self.assertNotIn("/home", text)
        self.assertEqual(self.loaded, [os.path.join(self.apparmor, "agents-hub-chrome")])

    def test_the_newest_abi_the_system_has_is_the_one_in_the_profile(self):
        open(os.path.join(self.apparmor, "abi", "5.0"), "w").close()
        self.install(self.make_zip())
        self.assertIn("abi <abi/5.0>,", read(self.apparmor, "agents-hub-chrome"))

    def test_a_system_without_apparmor_gets_chrome_and_no_profile(self):
        shutil.rmtree(self.apparmor)
        exe = self.install(self.make_zip())
        self.assertTrue(os.path.isfile(exe))
        self.assertFalse(os.path.exists(self.apparmor))
        self.assertEqual(self.loaded, [])

    def test_an_older_apparmor_gets_no_profile_its_parser_would_refuse_the_userns_rule(self):
        os.unlink(os.path.join(self.apparmor, "abi", "4.0"))        # Ubuntu 22.04: abi 3.0 at best, no restriction either
        exe = self.install(self.make_zip())
        self.assertTrue(os.path.isfile(exe))
        self.assertEqual(os.listdir(self.apparmor), ["abi"])
        self.assertEqual(self.loaded, [])

    def test_the_profile_does_not_use_the_variable_the_parser_cannot_resolve_without_the_kernel(self):
        exe = self.install(self.make_zip())
        text = read(self.apparmor, "agents-hub-chrome")
        self.assertNotIn("@{exec_path}", text)           # apparmor_parser -Q: "variable exec_path is never declared"
        self.assertIn(f"\n  {exe} mr,\n", text)

    def test_a_path_with_characters_of_the_profile_language_is_refused(self):
        bad = os.path.join(self.tmp, "opt{a,b}", "agents-hub")
        os.makedirs(bad)
        with self.assertRaises(chrome.Refused):
            with mock.patch("core.fetch.download", lambda *a, **k: self.fail("downloaded")):
                chrome.install_shared(bad, self.make_zip(), apparmor_dir=self.apparmor, load=lambda p: True,
                                      root_only=False)
        self.assertFalse(os.path.exists(os.path.join(self.apparmor, "agents-hub-chrome")))

    def test_a_program_that_does_not_start_leaves_no_profile_and_no_record(self):
        pin = self.make_zip(body="#!/bin/sh\nexit 3\n")
        with self.assertRaises(Exception):
            self.install(pin)
        self.assertFalse(os.path.exists(os.path.join(self.apparmor, "agents-hub-chrome")))
        self.assertFalse(os.path.exists(os.path.join(self.opt, "chrome", "chrome.json")))

    def test_the_helper_works_only_as_root_because_a_folder_a_user_owns_is_the_hole(self):
        with self.assertRaises(chrome.Refused) as cm:
            with mock.patch("os.geteuid", return_value=1000), \
                    mock.patch("core.fetch.download", lambda *a, **k: self.fail("downloaded")):
                chrome.install_shared(self.opt, self.make_zip(), apparmor_dir=self.apparmor, load=lambda p: True)
        self.assertEqual(cm.exception.why, "root")

    def test_an_archive_that_reaches_outside_is_refused_as_for_a_user(self):
        pin = self.make_zip([("../evil", b"x", 0o100755), ("chrome-linux64/chrome", b"x", 0o100755)])
        with self.assertRaises(chrome.Refused):
            self.install(pin)
        self.assertFalse(os.path.exists(os.path.join(self.opt, "evil")))

    def test_a_newer_chrome_replaces_the_older_and_the_profile_follows(self):
        old = self.install(self.make_zip())
        pin = dict(self.make_zip(), version="156.0.1.2")
        new = self.install(pin)
        self.assertFalse(os.path.exists(old))
        self.assertIn(new, read(self.apparmor, "agents-hub-chrome"))
        self.assertNotIn(old, read(self.apparmor, "agents-hub-chrome"))

    def test_the_hub_finds_the_shared_chrome_through_PACKAGED(self):
        exe = self.install(self.make_zip())
        mark = os.path.join(self.tmp, "PACKAGED")
        with open(mark, "w") as f:
            json.dump({"format": "app", "shared": os.path.join(self.opt, "chrome")}, f)
        state = os.path.join(self.tmp, "state")
        os.makedirs(state)
        with mock.patch.object(packaged, "MARK", mark):
            self.assertEqual(runtime.chrome(state), exe)
            # a record that leads out of the folder is not followed
            with open(os.path.join(self.opt, "chrome", "chrome.json"), "w") as f:
                json.dump({"version": "1", "path": "../../../../bin/sh"}, f)
            self.assertIsNone(runtime.chrome(state))
        with mock.patch.object(packaged, "MARK", os.path.join(self.tmp, "none")):
            self.assertIsNone(runtime.chrome(state))

    def test_the_shared_chrome_comes_before_one_in_the_users_folder(self):
        exe = self.install(self.make_zip())
        state = os.path.join(self.tmp, "state")
        mine = os.path.join(state, "runtime", "chrome", "1", "chrome")
        os.makedirs(os.path.dirname(mine))
        open(mine, "w").close()
        with open(os.path.join(state, "runtime", "chrome.json"), "w") as f:
            json.dump({"version": "1", "path": "chrome/1/chrome"}, f)
        mark = os.path.join(self.tmp, "PACKAGED")
        with open(mark, "w") as f:
            json.dump({"format": "app", "shared": os.path.join(self.opt, "chrome")}, f)
        with mock.patch.object(packaged, "MARK", mark):
            self.assertEqual(runtime.chrome(state), exe)           # the one that can start with the sandbox


@unittest.skipUnless(POSIX, "the .deb is Linux")
class RootHelper(unittest.TestCase):
    def load(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("hub_chrome_root", os.path.join(DEB, "files", "chrome_root.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_root_runs_nothing_that_is_not_root_s_and_closed_to_others(self):
        helper = self.load()
        tmp = tempfile.mkdtemp(prefix="hub-deb2-trust-")
        self.addCleanup(shutil.rmtree, tmp, True)
        me = os.getuid()                                   # the test stands in for root: the owner it trusts is this user
        os.makedirs(os.path.join(tmp, "seed", "core"))
        for rel in ("seed/core/a.py", "seed/b.py", "launch.py"):
            open(os.path.join(tmp, rel), "w").close()
        for top, dirs, files in os.walk(tmp):
            for name in dirs + files:
                os.chmod(os.path.join(top, name), 0o755)
        os.chmod(tmp, 0o755)
        seed, launch = os.path.join(tmp, "seed"), os.path.join(tmp, "launch.py")
        self.assertIsNone(helper.trusted(tmp, seed, launch, uid=me))
        self.assertEqual(helper.trusted(seed, uid=me + 1), seed)                 # not the trusted owner's
        deep = os.path.join(seed, "core", "a.py")
        os.chmod(deep, 0o666)                                                    # a nested file open to others
        self.assertEqual(helper.trusted(seed, uid=me), deep)
        self.assertIsNone(helper.trusted(seed, uid=me, deep=False))              # only the root itself is looked at
        os.chmod(deep, 0o644)
        os.chmod(os.path.join(seed, "core"), 0o775)                              # a folder open to the group
        self.assertEqual(helper.trusted(seed, uid=me), os.path.join(seed, "core"))
        os.chmod(os.path.join(seed, "core"), 0o755)
        os.chmod(launch, 0o666)                                                  # a root that is a single file
        self.assertEqual(helper.trusted(launch, uid=me), launch)
        os.chmod(launch, 0o644)
        loose = os.path.join(tmp, "loose")
        open(loose, "w").close()
        os.chmod(loose, 0o666)
        winsafe.symlink(self, loose, os.path.join(seed, "link"))                 # a link counts by what it leads to
        self.assertEqual(helper.trusted(seed, uid=me), os.path.join(seed, "link"))
        os.unlink(os.path.join(seed, "link"))
        # leads nowhere, inside: runs nothing
        winsafe.symlink(self, os.path.join(seed, "nowhere"), os.path.join(seed, "dangling"))
        self.assertIsNone(helper.trusted(seed, uid=me))
        winsafe.symlink(self, tmp, os.path.join(seed, "out"))                    # a folder outside: its files are never looked at
        self.assertEqual(helper.trusted(seed, uid=me), os.path.join(seed, "out"))
        os.unlink(os.path.join(seed, "out"))
        self.assertIsNotNone(helper.trusted(os.path.join(tmp, "none"), uid=me))  # a path we cannot look at is no good

    def test_without_root_it_only_says_how(self):
        helper = self.load()
        with mock.patch("os.geteuid", return_value=1000), mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(helper.main(), 1)
        self.assertIn("sudo agents-hub chrome", err.getvalue())

    def test_it_speaks_english_whatever_the_system_or_the_caller_speaks(self):
        # the package comes before the hub knows its owner's language: its lines and the core's steps (HUB_LANG) English
        helper = self.load()
        env = {"LANG": "ru_RU.UTF-8", "LC_ALL": "ru_RU.UTF-8", "HUB_LANG": "ru"}
        with mock.patch.dict(os.environ, env), mock.patch("os.geteuid", return_value=1000), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(helper.main(), 1)
            self.assertEqual(os.environ["HUB_LANG"], "en")
        self.assertEqual(err.getvalue(), "Needs root: sudo agents-hub chrome\n")


class Blocked(unittest.TestCase):
    """Where the system forbids the sandbox to a program without a profile, «Setup» does not download a Chrome that
    cannot start: it says what to do."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hub-deb2-blocked-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.state)
        self.switch = os.path.join(self.tmp, "userns")
        patch = mock.patch("modules.testchrome.chrome.USERNS_SWITCH", self.switch)
        patch.start()
        self.addCleanup(patch.stop)
        self.pin = {"version": "155.0.1", "url": "https://example.test/c.zip", "sha256": "0" * 64, "size": 10}

    def block(self, on=True):
        with open(self.switch, "w") as f:
            f.write("1\n" if on else "0\n")

    def mark(self, **data):
        path = os.path.join(self.tmp, "PACKAGED")
        with open(path, "w") as f:
            json.dump({"format": "app", **data}, f)
        return mock.patch.object(packaged, "MARK", path)

    def test_install_refuses_before_downloading_where_the_sandbox_is_forbidden(self):
        self.block()
        with mock.patch("core.fetch.download", lambda *a, **k: self.fail("downloaded 190 MB for nothing")):
            with self.assertRaises(chrome.Refused) as cm:
                chrome.install(self.state, self.pin)
        self.assertEqual(cm.exception.why, "sandbox")

    def test_the_text_names_the_command_of_the_package_when_there_is_one(self):
        self.block()
        with self.mark(shared="/opt/agents-hub/chrome"):
            self.assertIn("sudo agents-hub chrome", chrome.sandbox_help())
        with self.mark():
            self.assertNotIn("sudo agents-hub chrome", chrome.sandbox_help())

    def test_the_setup_row_tells_instead_of_offering_a_download(self):
        self.block()
        s = setup.Setup(self.state, lambda: {})
        with self.mark(shared="/opt/agents-hub/chrome"), mock.patch("modules.browser.browsers.find_chrome",
                                                                    side_effect=__import__("modules.browser.browsers", fromlist=["x"]).Problem("none")), \
                mock.patch.object(chrome, "pinned", return_value=self.pin):
            row = s.item_chrome({})
        self.assertEqual(row["sandbox"], "sudo agents-hub chrome")
        self.assertEqual(row["state"], "todo")

    def test_nothing_changes_where_the_sandbox_is_allowed(self):
        self.block(False)
        s = setup.Setup(self.state, lambda: {})
        with mock.patch("modules.browser.browsers.find_chrome", side_effect=__import__("modules.browser.browsers", fromlist=["x"]).Problem("none")), \
                mock.patch.object(chrome, "pinned", return_value=self.pin):
            row = s.item_chrome({})
        self.assertNotIn("sandbox", row)
        self.assertTrue(row["available"])


@unittest.skipUnless(POSIX, "the .deb is Linux")
class Scripts(unittest.TestCase):
    def test_build_takes_the_shell_of_the_factory_not_a_snapshot_of_named_folders(self):
        text = read(DEB, "build.sh")
        self.assertIn("--tag", text)
        self.assertNotIn("--runtime-only", text)
        self.assertNotIn("archive HEAD", text)
        inner = read(DEB, "build-in.sh")
        for part in ("code-seed", "PACKAGED", "launch.py", "licenses"):
            self.assertIn(part, inner)

    def test_the_package_tells_the_hub_where_its_chrome_is(self):
        inner = read(DEB, "build-in.sh")
        self.assertIn('"shared"', inner)
        self.assertIn("/opt/agents-hub/chrome", inner)

    def test_the_root_helper_runs_in_an_environment_of_its_own_with_the_proxy_and_the_language(self):
        tmp = tempfile.mkdtemp(prefix="hub-deb2-env-")
        self.addCleanup(shutil.rmtree, tmp, True)
        fake = os.path.join(tmp, "runtime", "python", "bin", "python3")
        os.makedirs(os.path.dirname(fake))
        with open(fake, "w") as f:
            f.write('#!/bin/sh\nenv | sort\necho "ARGS $*"\n')
        os.chmod(fake, 0o755)
        script = os.path.join(tmp, "agents-hub")
        with open(script, "w") as f:
            f.write(read(DEB, "files", "agents-hub").replace("/opt/agents-hub", tmp))
        env = {"PATH": "/usr/bin:/bin", "HOME": "/home/x", "TMPDIR": "/home/x/tmp", "SSL_CERT_FILE": "/home/x/ca.pem",
               "LD_PRELOAD": "/home/x/evil.so", "LANG": "ru_RU.UTF-8", "https_proxy": "http://proxy.example:3128",
               "no_proxy": ""}                                   # set but empty stays as it is
        r = subprocess.run(["sh", script, "chrome"], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line and not line.startswith("ARGS"))
        self.assertEqual(got.get("PATH"), "/usr/sbin:/usr/bin:/sbin:/bin")
        for name in ("HOME", "TMPDIR", "SSL_CERT_FILE", "LD_PRELOAD"):
            self.assertNotIn(name, got)
        self.assertEqual((got["LANG"], got["https_proxy"], got["no_proxy"]), ("ru_RU.UTF-8", "http://proxy.example:3128", ""))
        self.assertNotIn("http_proxy", got)                       # unset stays unset
        self.assertIn(f"ARGS -I {tmp}/lib/chrome_root.py", r.stdout)

    def test_a_failed_first_run_reaches_a_menu_entry_that_has_no_terminal(self):
        text = read(DEB, "files", "agents-hub")
        self.assertRegex(text, r'first_run\.py[^\n]*\|\| \{[^}]*fail')

    def test_the_command_runs_the_launcher_and_the_hub_from_the_code_folder(self):
        text = read(DEB, "files", "agents-hub")
        self.assertIn("first_run.py", text)
        self.assertIn("hub-up", text)
        self.assertIn("chrome_root.py", text)
        self.assertNotIn("/opt/agents-hub/app", text)
        wrapper = read(DEB, "files", "hub")
        self.assertNotIn("/opt/agents-hub/app", wrapper)
        self.assertIn("code/bin/hub", wrapper)

    def test_the_menu_entry_starts_the_hub_and_opens_the_page(self):
        text = read(DEB, "files", "agents-hub.desktop")
        self.assertIn("Exec=/opt/agents-hub/bin/agents-hub", text)
        self.assertIn("Icon=agents-hub", text)
        self.assertIn("Terminal=false", text)
        self.assertIn("icon-512.png", read(DEB, "build-in.sh"))

    def test_postinst_installs_chrome_only_where_the_system_asks_for_it_and_never_fails_the_install(self):
        text = read(DEB, "files", "postinst")
        self.assertIn("apparmor_restrict_unprivileged_userns", text)
        self.assertIn("chrome_root.py", text)
        self.assertIn("|| ", text)
        self.assertIn("AGENTS_HUB_NO_CHROME", text)
        self.assertIn("sudo agents-hub chrome", text)

    def test_removal_takes_the_downloaded_chrome_and_the_profile_along(self):
        text = read(DEB, "files", "postrm")
        self.assertIn("/opt/agents-hub/chrome", text)
        self.assertIn("/etc/apparmor.d/agents-hub-chrome", text)

    def test_the_shell_scripts_parse(self):
        for name in ("agents-hub", "hub", "postinst", "postrm"):
            r = subprocess.run(["sh", "-n", os.path.join(DEB, "files", name)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"{name}: {r.stderr}")
        for name in ("build.sh", "build-in.sh", "check.sh", "check-in.sh"):
            r = subprocess.run(["sh", "-n", os.path.join(DEB, name)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"{name}: {r.stderr}")


if __name__ == "__main__":
    unittest.main()
