"""Claude Code from Anthropic's downloads without `curl | bash` (installer spec §8, slice S1): the manifest counts only
with a valid signature of the pinned key, the binary only with its sha256 and size, a broken download continues where
it stopped, a refusal of the download server is shown in Anthropic's words, the install scripts use `hub setup claude`.
All on a fake server (tests/fakes/claude_downloads.py) and in a home folder of the test: the machine's claude is not
touched, nothing goes to the network."""
import tests  # noqa: F401 — keep the machine's settings out
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import platforms
from core import claudeinstall, fetch, i18n, net, ownclaude, pgpsig
from platforms import posix, windows
from tests import winsafe
from tests.fakes import can, claude_downloads as fake

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAT = "linux-x64"


def read(name):
    with open(os.path.join(fake.REAL, name), "rb") as f:
        return f.read()


def runner(argv, **kw):
    """The fake claude is Python with a shebang: Windows does not read shebangs, so it goes through this Python there."""
    return subprocess.run(([sys.executable] if os.name == "nt" else []) + list(argv), **kw)


class Signatures(unittest.TestCase):
    """core/pgpsig on Anthropic's own release files and on a key of the test."""

    def test_anthropics_signed_manifest_checks_out_with_the_pinned_fingerprint(self):
        key, sig, raw = read("claude-code.asc"), read("manifest.json.sig"), read("manifest.json")
        self.assertIsNone(pgpsig.verify(key, sig, raw, claudeinstall.FINGERPRINT, now=1793000000))
        self.assertEqual(list(pgpsig.keys(key)), [claudeinstall.FINGERPRINT])
        with self.assertRaises(pgpsig.Invalid):   # one byte of the manifest changed
            pgpsig.verify(key, sig, raw.replace(b"2.1.309", b"2.1.399"), claudeinstall.FINGERPRINT)
        with self.assertRaisesRegex(pgpsig.Invalid, "fingerprint"):   # another key: the pin does not let it in
            pgpsig.verify(fake.Key().armored(), sig, raw, claudeinstall.FINGERPRINT)

    def test_the_test_key_signs_like_gpg_and_the_edges_are_refused(self):
        k = fake.Key()
        data = b'{"version": "2.1.400"}'
        self.assertIsNone(pgpsig.verify(k.armored(), k.sign(data), data, k.fingerprint))
        self.assertIsNone(pgpsig.verify(k.armored(), k.sign(data, hash_algo=8), data, k.fingerprint))
        bad = {
            "a text signature": k.sign(data, sigtype=1),
            "an unknown critical subpacket": k.sign(data, critical=40),
            "an expired signature": k.sign(data, created=1000, expires=10),
            "another key's signature": fake.Key(seed=2).sign(data),
            "not OpenPGP": b"<html>",
        }
        for what, sig in bad.items():
            with self.subTest(what), self.assertRaises(pgpsig.Invalid):
                pgpsig.verify(k.armored(), sig, data, k.fingerprint)
        with self.assertRaises(pgpsig.Invalid):
            pgpsig.verify(k.armored(), k.sign(data), data + b" ", k.fingerprint)
        small = fake.Key(seed=3, bits=1024)
        with self.assertRaisesRegex(pgpsig.Invalid, "bits"):
            pgpsig.verify(small.armored(), small.sign(data), data, small.fingerprint)


class Installs(unittest.TestCase):
    """core/ownclaude.fetch: the release's version into <state>/claude-code/<version>/ — never ~/.local of the person."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hub-claudeinstall-")
        self.addCleanup(platforms.current.rmtree, self.tmp)
        self.state, self.home = os.path.join(self.tmp, "state"), os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        env = mock.patch.dict(os.environ, {"HOME": self.home, "USERPROFILE": self.home})
        env.start()
        self.addCleanup(env.stop)
        self.key = fake.Key()
        self.binary = fake.fake_claude("2.1.400") + b"#" + os.urandom(300_000).hex().encode() + b"\n"
        self.srv = fake.Server(self.key, "2.1.400", {PLAT: self.binary})
        self.addCleanup(self.srv.close)
        self.lines = []

    def installer(self, **kw):
        kw.setdefault("fingerprint", self.key.fingerprint)
        kw.setdefault("platform", PLAT)
        return claudeinstall.Installer(self.state, root_url=self.srv.url, say=self.lines.append, pause=0, **kw)

    def fetch(self, ver="2.1.400", force=False, free=None, **kw):
        return ownclaude.fetch(self.state, ver, force=force, installer=self.installer(**kw), run=runner,
                               free=free or (lambda path: 10**12))

    def own(self, ver="2.1.400"):
        return ownclaude.path(self.state, ver)

    def paths(self):
        return [p for p, _ in self.srv.requests]

    def test_signed_manifest_checked_binary_into_the_hubs_folder(self):
        got = self.fetch()
        self.assertEqual(got, self.own())
        with open(got, "rb") as f:
            self.assertEqual(f.read(), self.binary)
        self.assertTrue(os.access(got, os.X_OK))   # core/fetch puts a plain file: fetch makes it a program
        self.assertEqual(self.paths(), ["/claude-code-releases/2.1.400/manifest.json",
                                        "/claude-code-releases/2.1.400/manifest.json.sig", "/keys/claude-code.asc",
                                        f"/claude-code-releases/2.1.400/{PLAT}/claude"])   # no channel pointer: the version
        self.assertEqual(sorted(os.listdir(self.home)), ["ran-version.txt"])   # nothing in ~/.local: no `claude install`
        with open(os.path.join(self.home, "ran-version.txt"), encoding="utf-8") as f:
            self.assertIn(os.path.join(self.state, "claude-code", ".part-2.1.400"), f.read())   # the file itself was asked
        self.assertEqual(sorted(os.listdir(os.path.join(self.state, "claude-code"))), [".lock", "2.1.400"])
        with open(os.path.join(self.state, "logs", "setup.log"), encoding="utf-8") as f:
            self.assertIn(f"Claude Code 2.1.400 at {got}", f.read())
        self.srv.requests.clear()
        self.assertEqual(self.fetch(), got)   # there: nothing asked
        self.assertEqual(self.srv.requests, [])

    def test_its_version_is_asked_with_claudes_own_updates_off(self):
        seen = []

        def run(argv, **kw):
            seen.append({k: kw["env"].get(k) for k in ownclaude.ENV})
            return runner(argv, **kw)
        with mock.patch.dict(os.environ, {"DISABLE_AUTOUPDATER": "0"}):
            ownclaude.fetch(self.state, "2.1.400", installer=self.installer(), run=run, free=lambda path: 10**12)
        self.assertEqual(seen, [ownclaude.ENV])

    def test_a_manifest_without_a_valid_signature_downloads_nothing(self):
        cases = {
            "another key": lambda: setattr(self.srv, "signature", fake.Key(seed=2).sign(self.srv.manifest)),
            "no signature": lambda: setattr(self.srv, "signature", None),
            "manifest changed after signing": lambda: setattr(self.srv, "manifest", self.srv.manifest + b" "),
        }
        for what, spoil in cases.items():
            with self.subTest(what):
                self.srv.manifest = fake.manifest("2.1.400", {PLAT: self.binary})
                self.srv.resign()
                spoil()
                self.srv.requests.clear()
                with self.assertRaises(claudeinstall.Failed) as e:
                    self.fetch()
                self.assertEqual(e.exception.why, "signature")
                self.assertFalse(any(p.endswith("/claude") for p in self.paths()), self.paths())
                self.assertIsNone(ownclaude.mine(self.state, "2.1.400"))

    def test_the_pinned_fingerprint_is_the_default(self):
        # a mirror or a fake server signs with its own key: the default Installer does not take it
        inst = claudeinstall.Installer(self.state, root_url=self.srv.url, pause=0, platform=PLAT)
        with self.assertRaises(claudeinstall.Failed) as e:
            ownclaude.fetch(self.state, "2.1.400", installer=inst, run=runner)
        self.assertEqual(e.exception.why, "signature")

    def test_a_signed_manifest_of_another_release_is_not_this_release(self):
        self.srv.manifest = fake.manifest("2.1.300", {PLAT: self.binary})
        self.srv.resign()
        with self.assertRaises(claudeinstall.Failed) as e:
            self.fetch()
        self.assertEqual(e.exception.why, "manifest")

    def test_a_binary_that_does_not_match_the_manifest_is_deleted_and_never_run(self):
        for what, served in {"other bytes": b"x" * len(self.binary), "shorter": self.binary[:-10],
                             "longer": self.binary + b"tail"}.items():
            with self.subTest(what):
                self.srv.binaries[PLAT] = served
                self.srv.requests.clear()
                with self.assertRaises(claudeinstall.Failed) as e:
                    self.fetch()
                self.assertEqual(e.exception.why, "checksum")
                self.assertEqual(os.listdir(os.path.join(self.state, "claude-code", ".part-2.1.400")), [])
                self.assertFalse(os.path.exists(os.path.join(self.home, "ran-version.txt")))
                self.assertEqual(sum(p.endswith("/claude") for p in self.paths()), 1)   # no retries of a wrong file

    def test_a_file_that_names_another_version_is_not_used(self):
        self.srv.binaries[PLAT] = self.binary = fake.fake_claude("2.1.100")
        self.srv.manifest = fake.manifest("2.1.400", {PLAT: self.binary})
        self.srv.resign()
        with self.assertRaises(claudeinstall.Failed) as e:
            self.fetch()
        self.assertEqual(e.exception.why, "version")
        self.assertIn("2.1.100", str(e.exception))
        self.assertEqual(os.listdir(os.path.join(self.state, "claude-code")), [".lock"])

    def test_a_broken_download_continues_from_where_it_stopped(self):
        self.srv.drop_after = [100_000, 200_000]
        self.fetch()
        got = [r for p, r in self.srv.requests if p.endswith("/claude")]
        self.assertEqual(got, [None, "bytes=100000-", "bytes=300000-"])
        self.assertTrue(os.path.exists(self.own()))

    def test_a_download_broken_on_every_try_is_kept_and_the_next_run_continues(self):
        self.srv.drop_after = [50_000] * claudeinstall.TRIES
        with mock.patch.object(fetch, "ATTEMPTS_MAX", claudeinstall.TRIES), self.assertRaises(claudeinstall.Failed) as e:
            self.fetch()   # a run's attempts are over (with bytes each time they would go on: tests/test_fetch_resume)
        self.assertEqual(e.exception.why, "broken")
        part = os.path.join(self.state, "claude-code", ".part-2.1.400", os.path.basename(self.own()) + ".part")   # claude.exe.part on Windows
        self.assertEqual(os.path.getsize(part), 50_000 * claudeinstall.TRIES)
        self.srv.requests.clear()
        self.fetch()
        self.assertEqual([r for p, r in self.srv.requests if p.endswith("/claude")],
                         [f"bytes={50_000 * claudeinstall.TRIES}-"])
        self.assertIn(i18n.t("cli.fetch.resumed", what="Claude Code 2.1.400", mb="0.2"), self.lines)
        self.assertFalse(os.path.exists(os.path.dirname(part)))

    def test_a_passing_server_failure_is_retried_and_a_missing_file_is_not(self):
        self.srv.fail_with = [503, 502]
        self.fetch()
        self.assertEqual(sum(p.endswith("/claude") for p in self.paths()), 3)
        platforms.current.rmtree(os.path.join(self.state, "claude-code"))
        self.srv.requests.clear()
        self.srv.fail_with = [404]
        with self.assertRaises(claudeinstall.Failed) as e:
            self.fetch()
        self.assertEqual(e.exception.why, "http")
        self.assertEqual(sum(p.endswith("/claude") for p in self.paths()), 1)

    def test_a_server_without_ranges_starts_over(self):
        self.srv.ranges = False
        self.srv.drop_after = [100_000]
        self.fetch()
        self.assertEqual([r for p, r in self.srv.requests if p.endswith("/claude")], [None, "bytes=100000-"])

    def test_a_refusal_of_the_download_server_is_shown_in_anthropics_words(self):
        # the channel pointer (tools/compat.py reads the stable channel through the same Installer)
        for what, status, body in (("forbidden", 403, b"denied"), ("an html page", 200, b"<html>blocked</html>"),
                                   ("unavailable for legal reasons", 451, b"")):
            with self.subTest(what):
                self.srv.pointer_status, self.srv.pointer = status, body
                with self.assertRaises(claudeinstall.Failed) as e:
                    self.installer().resolve("stable")
                self.assertEqual(e.exception.why, "region")
                self.assertIn(claudeinstall.REGION, str(e.exception))

    def test_no_server_is_a_network_failure(self):
        inst = claudeinstall.Installer(self.state, root_url="http://127.0.0.1:9", pause=0, platform=PLAT,
                                       fingerprint=self.key.fingerprint)
        with self.assertRaises(claudeinstall.Failed) as e:
            ownclaude.fetch(self.state, "2.1.400", installer=inst, run=runner)
        self.assertEqual(e.exception.why, "network")

    def test_no_build_for_this_platform(self):
        with self.assertRaises(claudeinstall.Failed) as e:
            self.fetch(platform="darwin-arm64")
        self.assertEqual(e.exception.why, "manifest")

    def test_a_second_fetch_at_the_same_time_waits_for_none(self):
        import platforms
        os.makedirs(os.path.join(self.state, "claude-code"))
        with open(os.path.join(self.state, "claude-code", ".lock"), "a+") as held:
            platforms.current.try_lock(held)
            with self.assertRaises(claudeinstall.Failed) as e:
                self.fetch()
        self.assertEqual(e.exception.why, "busy")
        self.assertEqual(self.srv.requests, [])

    def test_too_little_disk_space_downloads_nothing_and_says_how_much(self):
        free = len(self.binary) + ownclaude.SPARE - 1
        with self.assertRaises(claudeinstall.Failed) as e:
            self.fetch(free=lambda path: free)
        self.assertEqual(e.exception.why, "disk")
        self.assertIn(f"{(len(self.binary) + ownclaude.SPARE) / 1e6:.0f}", str(e.exception))
        self.assertFalse(any(p.endswith("/claude") for p in self.paths()))
        self.srv.drop_after = [100_000] * claudeinstall.TRIES   # 300 KB there: the check and the message need less
        with mock.patch.object(fetch, "ATTEMPTS_MAX", claudeinstall.TRIES), self.assertRaises(claudeinstall.Failed):
            self.fetch(free=lambda path: free + 1)
        have = 100_000 * claudeinstall.TRIES
        with self.assertRaises(claudeinstall.Failed) as e:
            self.fetch(free=lambda path: len(self.binary) - have + ownclaude.SPARE - 1)
        self.assertIn(f"{(len(self.binary) - have + ownclaude.SPARE) / 1e6:.0f}", str(e.exception))
        self.srv.drop_after = []
        self.fetch(free=lambda path: len(self.binary) - have + ownclaude.SPARE)

    def test_the_same_copy_of_another_hub_of_this_user_is_linked_not_downloaded(self):
        other = os.path.join(self.tmp, "other-state")
        twin = ownclaude.path(other, "2.1.400")
        os.makedirs(os.path.dirname(twin))
        with open(twin, "wb") as f:
            f.write(self.binary)
        os.chmod(twin, 0o755)
        from core import envs
        with mock.patch.object(envs, "hubs", lambda state: [state, other]):
            got = self.fetch(free=lambda path: 0)   # no download: the space check is not reached
        self.assertFalse(any(p.endswith("/claude") for p in self.paths()))
        self.assertTrue(os.path.samefile(got, twin))
        for spoil in (b"x" * len(self.binary), self.binary[:-1]):   # not the signed build: downloaded after all
            with self.subTest(len(spoil)):
                platforms.current.rmtree(os.path.join(self.state, "claude-code"))
                os.remove(twin)
                with open(twin, "wb") as f:
                    f.write(spoil)
                self.srv.requests.clear()
                with mock.patch.object(envs, "hubs", lambda state: [state, other]):
                    got = self.fetch()
                self.assertFalse(os.path.samefile(got, twin))
                self.assertTrue(any(p.endswith("/claude") for p in self.paths()))

    def test_force_downloads_only_a_copy_that_is_not_the_signed_one_and_replaces_the_file_whole(self):
        self.fetch()
        self.srv.requests.clear()
        self.fetch(force=True)   # the same bytes: the manifest is read, the file is not downloaded again
        self.assertFalse(any(p.endswith("/claude") for p in self.paths()))
        with open(self.own(), "ab") as f:
            f.write(b"# changed\n")
        replaced = []
        real = os.replace

        def replace(src, dst):   # the folder X/ stays: the file is swapped in one step
            if os.path.dirname(dst) != os.path.dirname(src):   # not core/fetch's own .part -> file inside the folder
                replaced.append((os.path.basename(os.path.dirname(src)), os.path.basename(dst)))
            return real(src, dst)
        with mock.patch.object(ownclaude.os, "replace", replace):
            self.fetch(force=True)
        self.assertEqual(replaced, [(".part-2.1.400", os.path.basename(self.own()))])
        with open(self.own(), "rb") as f:
            self.assertEqual(f.read(), self.binary)
        self.assertEqual(sorted(os.listdir(os.path.join(self.state, "claude-code"))), [".lock", "2.1.400"])


class Tidy(unittest.TestCase):
    """core/ownclaude.gc: three versions — the running one and the two most recently active — the newest unfinished
    download, and whatever a program still runs."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hub-ownclaude-gc-")
        self.addCleanup(platforms.current.rmtree, self.state)
        self.now = time.time()

    def version(self, ver, active):
        d = os.path.join(self.state, "claude-code", ver)
        os.makedirs(d, exist_ok=True)
        open(ownclaude.path(self.state, ver), "w").close()
        open(os.path.join(d, ownclaude.ACTIVE), "w").close()
        os.utime(os.path.join(d, ownclaude.ACTIVE), (self.now - active, self.now - active))

    def part(self, ver, age):
        d = os.path.join(self.state, "claude-code", ownclaude.PART + ver)
        os.makedirs(d, exist_ok=True)
        f = os.path.join(d, "claude.part")
        open(f, "w").close()
        os.utime(f, (self.now - age, self.now - age))
        os.utime(d, (self.now - age, self.now - age))

    def left(self):
        return sorted(os.listdir(os.path.join(self.state, "claude-code")))

    def gc(self, running, busy=lambda paths: set()):
        return ownclaude.gc(self.state, running, now=lambda: self.now, busy=busy)

    def test_the_running_and_the_two_most_recently_active_stay(self):
        for ver, age in (("2.1.100", 400), ("2.1.200", 300), ("2.1.300", 200), ("2.1.400", 100)):
            self.version(ver, age)
        self.gc("2.1.100")   # the running one is the oldest: a restart on it after B was prepared (spec, case 5)
        self.assertEqual(self.left(), ["2.1.100", "2.1.300", "2.1.400"])

    def test_the_newest_unfinished_download_stays_a_week_older_ones_go(self):
        self.version("2.1.100", 10)
        self.part("2.1.200", 2 * 86400)    # the update tries again after a day: still there to go on
        self.part("2.1.150", 3 * 86400)    # not the newest
        self.gc("2.1.100")
        self.assertEqual(self.left(), [".part-2.1.200", "2.1.100"])
        self.part("2.1.200", ownclaude.PART_DAYS * 86400 + 1)
        self.gc("2.1.100")
        self.assertEqual(self.left(), ["2.1.100"])

    def test_the_part_of_the_version_being_fetched_stays_even_when_another_is_newer(self):
        self.version("2.1.100", 10)
        self.part("2.1.200", 2 * 86400)
        self.part("2.1.300", 60)
        ownclaude.gc(self.state, "2.1.100", now=lambda: self.now, busy=lambda paths: set(), fetching="2.1.200")
        self.assertEqual(self.left(), [".part-2.1.200", ".part-2.1.300", "2.1.100"])

    def test_a_version_a_program_runs_from_stays(self):
        for ver, age in (("2.1.100", 400), ("2.1.200", 300), ("2.1.300", 200), ("2.1.400", 100)):
            self.version(ver, age)
        held = os.path.realpath(os.path.join(self.state, "claude-code", "2.1.200"))
        self.gc("2.1.400", busy=lambda paths: {p for p in paths if p == held})
        self.assertEqual(self.left(), ["2.1.200", "2.1.300", "2.1.400"])   # 2.1.100 went, the session's 2.1.200 stays

    def test_only_names_fetch_makes_are_touched_and_the_old_installers_leftovers_go(self):
        self.version("2.1.100", 10)
        os.makedirs(os.path.join(self.state, "claude-code", "notes"))
        os.makedirs(os.path.join(self.state, "downloads"))
        for n in ("claude-2.1.300-linux-x64.part", "chrome.zip"):
            open(os.path.join(self.state, "downloads", n), "w").close()
        self.gc("2.1.100")
        self.assertEqual(self.left(), ["2.1.100", "notes"])
        self.assertEqual(os.listdir(os.path.join(self.state, "downloads")), ["chrome.zip"])

    def test_a_start_marks_its_version_active_and_tidies_up(self):
        for ver, age in (("2.1.100", 400), ("2.1.200", 300), ("2.1.300", 200), ("2.1.400", 100)):
            self.version(ver, age)
        real = ownclaude.gc
        with mock.patch.object(ownclaude, "gc", lambda state, running, **kw: real(state, running, now=lambda: self.now,
                                                                                  busy=lambda paths: set())):
            ownclaude.started(self.state, "2.1.100")
        self.assertEqual(self.left(), [".lock", "2.1.100", "2.1.300", "2.1.400"])
        mark = os.path.join(self.state, "claude-code", "2.1.100", ownclaude.ACTIVE)
        self.assertGreaterEqual(os.path.getmtime(mark), self.now - 1)  # portable: set 400 s back by os.utime above


class Pick(unittest.TestCase):
    """The one place every process of the hub takes its claude from (core/ownclaude.command, core/realclaude.find)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hub-ownclaude-pick-")
        self.addCleanup(platforms.current.rmtree, self.tmp)
        self.state = os.path.join(self.tmp, "state")
        env = mock.patch.dict(os.environ, {"HUB_STATE_DIR": self.state, "PATH": os.path.join(self.tmp, "bin")})
        env.start()
        self.addCleanup(env.stop)
        for k in ("HUB_CLAUDE", "HUB_REAL_CLAUDE", *ownclaude.ENV):
            os.environ.pop(k, None)

    def put(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(path, 0o755)
        return path

    def test_hub_claude_then_the_releases_copy_then_path(self):
        exe = "claude.exe" if os.name == "nt" else "claude"
        mine = ownclaude.path(self.state, "2.1.400")
        with mock.patch.dict(os.environ, {"HUB_TEST_CLAUDE_RELEASE": "2.1.400"}):
            self.assertEqual(ownclaude.command(), [os.path.expanduser("~/.local/bin/claude")])   # not there yet
            self.assertNotIn("DISABLE_AUTOUPDATER", os.environ)
            on_path = self.put(os.path.join(self.tmp, "bin", exe))
            self.assertEqual(os.path.normcase(ownclaude.command()[0]), os.path.normcase(on_path))
            self.put(mine)
            self.assertEqual(ownclaude.command(), [mine])
            self.assertEqual({k: os.environ.get(k) for k in ownclaude.ENV}, ownclaude.ENV)   # for what it starts
            given = self.put(os.path.join(self.tmp, "given", exe))
            with mock.patch.dict(os.environ, {"HUB_CLAUDE": given}):
                self.assertEqual(ownclaude.command(), [given])
            self.assertIsNone(ownclaude.mine(self.state, "2.1.401"))   # only the exact version
        self.assertEqual(os.path.normcase(ownclaude.command()[0]), os.path.normcase(on_path))   # a working copy names none

    def test_a_call_outside_a_session_takes_the_releases_copy_inside_one_the_sessions_own(self):
        from core import realclaude
        mine = self.put(ownclaude.path(self.state, "2.1.400"))
        session = self.put(os.path.join(self.tmp, "older", "claude"))
        with mock.patch.dict(os.environ, {"HUB_TEST_CLAUDE_RELEASE": "2.1.400"}):
            self.assertEqual(realclaude.find(), mine)
            self.assertEqual({k: os.environ.get(k) for k in ownclaude.ENV}, ownclaude.ENV)
            with mock.patch.dict(os.environ, {"HUB_REAL_CLAUDE": session}):
                self.assertEqual(realclaude.find(), session)
            with mock.patch.dict(os.environ, {"HUB_CLAUDE": session}):   # the owner's own claude: not the copy
                self.assertNotEqual(realclaude.find(), mine)

    def test_a_copy_elsewhere_is_not_the_hubs(self):
        mine = self.put(ownclaude.path(self.state, "2.1.400"))
        self.assertTrue(ownclaude.owned(mine, self.state))
        self.assertFalse(ownclaude.owned(self.put(os.path.join(self.tmp, "bin", "claude")), self.state))
        self.assertFalse(ownclaude.owned("claude", os.path.join(self.tmp, "nowhere")))


class Fetch(unittest.TestCase):
    """core/fetch alone — the downloader the app also uses for Chrome for Testing (a URL, sha256 and size pinned)."""

    def test_a_pinned_file_is_fetched_resumed_and_refused_when_it_differs(self):
        tmp = tempfile.mkdtemp(prefix="hub-fetch-")
        self.addCleanup(platforms.current.rmtree, tmp)
        data = os.urandom(250_000)
        srv = fake.Server(fake.Key(), "2.1.400", {PLAT: data})
        self.addCleanup(srv.close)
        url, dest = f"{srv.url}/claude-code-releases/2.1.400/{PLAT}/claude", os.path.join(tmp, "chrome.zip")
        srv.drop_after = [90_000]
        said = []
        got = fetch.download(url, dest, hashlib.sha256(data).hexdigest(), len(data), "Chrome", "hub setup chrome",
                             say=said.append, pause=0)
        self.assertEqual(got, dest)
        with open(dest, "rb") as f:
            self.assertEqual(f.read(), data)
        self.assertEqual([r for _, r in srv.requests], [None, "bytes=90000-"])
        with self.assertRaises(fetch.Failed) as e:
            fetch.download(url, os.path.join(tmp, "other.zip"), "0" * 64, len(data), "Chrome", "hub setup chrome",
                           pause=0)
        self.assertEqual(e.exception.why, "checksum")
        self.assertIn("hub setup chrome", str(e.exception))
        self.assertEqual(sorted(os.listdir(tmp)), ["chrome.zip"])


class Command(unittest.TestCase):
    """`hub setup claude [--force]` itself: the owner's terminal only, the pinned key, a wrong file is refused, the
    person's home gets nothing."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hub-setupclaude-")
        self.addCleanup(platforms.current.rmtree, self.tmp)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)

    def hub(self, srv, *args, **env):
        e = {k: v for k, v in os.environ.items() if k not in ("HUB_SESSION", "HUB_EXTENSION", "HUB_MODULE", "HUB_CLAUDE")}
        e.update(HOME=self.home, USERPROFILE=self.home, HUB_STATE_DIR=os.path.join(self.tmp, "state"),
                 HUB_CLAUDE_DOWNLOADS=srv.url, HUB_TEST_CLAUDE_RELEASE=srv.ver)
        e.update(env)
        return subprocess.run([sys.executable, os.path.join(ROOT, "bin", "hub"), "setup", "claude", *args], env=e,
                              capture_output=True, text=True, encoding="utf-8", stdin=subprocess.DEVNULL, timeout=120)

    def server(self, ver, binaries):
        srv = fake.Server(fake.Key(), ver, binaries)
        self.addCleanup(srv.close)
        return srv

    def test_a_fake_servers_own_key_is_refused(self):
        srv = self.server("2.1.400", {posix.claude_platform() if os.name != "nt" else windows.claude_platform():
                                      fake.fake_claude("2.1.400")})
        r = self.hub(srv)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("2.1.400", r.stderr)
        self.assertEqual(os.listdir(self.home), [])
        self.assertFalse(any(p.endswith(("/claude", "/claude.exe")) for p, _ in srv.requests))

    def test_anthropics_signed_manifest_with_a_wrong_file_is_refused(self):
        srv = self.server("2.1.309", {})
        srv.manifest, srv.signature, srv.key_file = read("manifest.json"), read("manifest.json.sig"), \
            read("claude-code.asc")
        plat = posix.claude_platform() if os.name != "nt" else windows.claude_platform()
        srv.binaries[plat] = b"not claude"
        r = self.hub(srv)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("2.1.309", r.stdout)   # the signature checked out
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(os.listdir(self.home), [])
        self.assertIsNone(ownclaude.mine(os.path.join(self.tmp, "state"), "2.1.309"))

    def test_a_copy_already_there_is_left_alone_unless_forced(self):
        srv = self.server("2.1.400", {})
        put = ownclaude.path(os.path.join(self.tmp, "state"), "2.1.400")
        os.makedirs(os.path.dirname(put))
        open(put, "w").close()
        r = self.hub(srv)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(put, r.stdout)
        self.assertEqual(srv.requests, [])
        r = self.hub(srv, "--force")   # asked again (here — refused at the signature)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertTrue(srv.requests)

    def test_a_working_copy_names_no_version_and_downloads_nothing(self):
        from tests.fakes import exe
        srv = self.server("2.1.400", {})
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        r = self.hub(srv, HUB_TEST_CLAUDE_RELEASE="", PATH=empty + os.pathsep + os.path.dirname(sys.executable))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)   # nothing to run: the install scripts say so
        exe.script(os.path.join(empty, "claude"), 'print("2.1.300 (Claude Code)")\n')
        r = self.hub(srv, HUB_TEST_CLAUDE_RELEASE="", PATH=empty + os.pathsep + os.path.dirname(sys.executable))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(empty, r.stdout)   # the line names what the hub runs instead
        self.assertEqual(srv.requests, [])

    def test_a_session_cannot_install_claude_for_everyone(self):
        srv = self.server("2.1.400", {})
        r = self.hub(srv, HUB_SESSION="maps")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(srv.requests, [])

    def test_a_channel_or_a_version_is_a_usage_error(self):
        srv = self.server("2.1.400", {})
        for arg in ("stable", "latest", "2.1.400", "nightly"):
            with self.subTest(arg):
                self.assertEqual(self.hub(srv, arg).returncode, 2)
        self.assertEqual(srv.requests, [])


class PreviousRelease(unittest.TestCase):
    """A process of the previous release (its platforms and core modules in memory) imports core/realclaude and
    core/ownclaude fresh from the new tree after the update merged it (the critic, the watchdog line): they work."""

    def test_the_releases_copy_is_found_from_a_process_of_the_previous_release(self):
        from tests.test_old_page_server import last_release
        old = last_release()
        if not old:
            self.skipTest("a clone without release tags")
        tmp = tempfile.mkdtemp(prefix="hub-ownclaude-old-")
        self.addCleanup(platforms.current.rmtree, tmp)
        tree, state = os.path.join(tmp, "tree"), os.path.join(tmp, "state")
        have = subprocess.run(["git", "ls-tree", "--name-only", old], cwd=ROOT, check=True, capture_output=True,
                              text=True).stdout.split()
        # the release's own tree as the hubs have it: its core reads i18n/data at import (the tag is not in main's history)
        archive = subprocess.run(["git", "archive", old, "--", *[n for n in ("platforms", "core", "i18n") if n in have]],
                                 cwd=ROOT, check=True, capture_output=True).stdout
        os.makedirs(tree)
        subprocess.run(["tar", "-x", "-C", tree], input=archive, check=True)
        probe = ("import os, sys\nsys.path.insert(0, os.getcwd())\n"
                 "import platforms, core.version, core.catalog, core.fetch  # the previous release's, in memory\n"
                 "for name in ('ownclaude', 'realclaude', 'statemark'):   # the merge puts the new files in place\n"
                 f"    open(os.path.join('core', name + '.py'), 'w').write(open({ROOT!r} + '/core/' + name + '.py').read())\n"
                 "from core import realclaude, ownclaude\nprint(realclaude.find())\nprint(ownclaude.command()[0])\n")
        mine = ownclaude.path(state, "2.1.400")
        os.makedirs(os.path.dirname(mine))
        open(mine, "w").close()
        env = {k: v for k, v in os.environ.items() if k not in ("HUB_REAL_CLAUDE", "HUB_CLAUDE")}
        env.update(HUB_STATE_DIR=state, HUB_TEST_CLAUDE_RELEASE="2.1.400", PYTHONDONTWRITEBYTECODE="1")
        r = subprocess.run([sys.executable, "-c", probe], cwd=tree, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split("\n")[:2], [mine, mine])


class Platforms(unittest.TestCase):
    def test_the_download_platform_is_picked_as_anthropics_scripts_pick_it(self):
        cases = [({"system": "Darwin", "machine": "arm64"}, "darwin-arm64"),
                 ({"system": "Darwin", "machine": "x86_64", "translated": True}, "darwin-arm64"),   # Rosetta
                 ({"system": "Darwin", "machine": "x86_64", "translated": False}, "darwin-x64"),
                 ({"system": "Linux", "machine": "x86_64", "musl": False}, "linux-x64"),
                 ({"system": "Linux", "machine": "aarch64", "musl": True}, "linux-arm64-musl"),
                 ({"system": "Linux", "machine": "i686", "musl": False}, None),
                 ({"system": "FreeBSD", "machine": "amd64"}, None)]
        for probe, want in cases:
            self.assertEqual(posix.claude_platform(probe), want, probe)
        for arch, want in (("AMD64", "win32-x64"), ("ARM64", "win32-arm64"), ("x86", None)):
            self.assertEqual(windows.claude_platform({"arch": arch}), want, arch)


class Scripts(unittest.TestCase):
    SCRIPTS = ["deploy/install.sh", "deploy/mac/install.sh", "deploy/windows/install.ps1",
               "tools/package/deb/files/agents-hub"]

    def test_install_scripts_use_hub_setup_claude_not_curl_into_a_shell(self):
        piped = re.compile(r"(curl|wget)[^\n|]*\|\s*(ba|z)?sh\b|(irm|Invoke-RestMethod|iwr)[^\n|]*\|\s*(iex|Invoke-Expression)",
                           re.I)
        for path in self.SCRIPTS:
            with open(os.path.join(ROOT, path), encoding="utf-8") as f:
                text = f.read()
            with self.subTest(path):
                self.assertIsNone(piped.search(text))
                self.assertIn("setup claude", text)

    def test_the_downloader_carries_the_record_of_its_last_comparison(self):
        # MIN_CLAUDE moves with every release (publish.sh pins it), so the record is not tied to it: a snapshot with
        # another MIN_CLAUDE stays green in the hubs' own update check
        self.assertRegex(claudeinstall.REVIEWED["date"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertRegex(claudeinstall.REVIEWED["claude"], r"^\d+\.\d+\.\d+$")
        for name in ("install.sh", "install.ps1"):
            self.assertRegex(claudeinstall.REVIEWED[name], r"^[0-9a-f]{64}$")


class Proxy(unittest.TestCase):
    def test_downloads_go_through_the_owners_http_proxy(self):
        with mock.patch.dict(os.environ, {"https_proxy": "http://u:p@proxy.example:3128", "no_proxy": ""}):
            conn = net.connection("https", "downloads.claude.ai", 443, 5)
        self.assertEqual((conn.host, conn.port), ("proxy.example", 3128))
        self.assertEqual(conn._tunnel_host, "downloads.claude.ai")
        self.assertTrue(conn._tunnel_headers["Proxy-Authorization"].startswith("Basic "))
        with mock.patch.dict(os.environ, {"https_proxy": "http://proxy.example:3128", "no_proxy": "claude.ai"}):
            self.assertEqual(net.connection("https", "downloads.claude.ai", 443, 5).host, "downloads.claude.ai")


if __name__ == "__main__":
    unittest.main()
