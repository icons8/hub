"""The app's start without a wizard (app spec §4a, slice A11): the release's own Claude Code (core/ownclaude), the steps auto.py starts by itself and in which order, the app's checklist without GitHub and the team's
modules, hub-up starting it only in the app, the layer's download progress for the band."""
import tests  # noqa: F401 — keep the machine's settings out (~/.config/hub-core), see tests/__init__.py
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import platforms
from modules.setup import auto, job, setup
from tests.fakes import exe

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def write(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="hubsetupapp-"))
        self.addCleanup(platforms.current.rmtree, self.tmp)
        self.state = os.path.join(self.tmp, "state")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.state)
        os.makedirs(self.home)
        env = mock.patch.dict(os.environ, {"HUB_STATE_DIR": self.state})
        env.start()
        self.addCleanup(env.stop)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("XDG_DATA_HOME", None)
        sandbox = mock.patch("modules.testchrome.chrome.USERNS_SWITCH", os.path.join(self.tmp, "no-such-switch"))
        sandbox.start()
        self.addCleanup(sandbox.stop)

    def app_repo(self, fmt="app"):
        repo = os.path.join(self.tmp, f"code-{fmt}")
        os.makedirs(os.path.join(repo, "modules"), exist_ok=True)
        if os.path.isfile(os.path.join(REPO, "modules", "private.json")):   # the open flavor has no pin
            shutil.copy(os.path.join(REPO, "modules", "private.json"), os.path.join(repo, "modules"))
        if fmt:
            write(os.path.join(repo, "PACKAGED"), json.dumps({"format": fmt, "version": "1.2.3"}))
        return repo


OLD = "import sys\nprint('2.0.1 (Claude Code)') if sys.argv[1:] == ['--version'] else sys.exit(1)\n"
NEW = "import sys\nprint('9.9.9 (Claude Code)') if sys.argv[1:] == ['--version'] else sys.exit(1)\n"


class OwnCopy(Base):
    """The release's own Claude Code (core/ownclaude, spec d2263524) in «Setup»: whatever claude the person has, from
    whatever installer, the hub downloads its own copy and never runs npm or Homebrew."""

    def release(self, ver="2.1.400"):
        repo = self.app_repo()
        write(os.path.join(repo, "core", "version.py"), f'CLAUDE_TESTED = "{ver}"\nMIN_CLAUDE = CLAUDE_TESTED\n')
        return repo

    def program(self, *where, body=OLD):
        os.makedirs(os.path.join(self.tmp, *where[:-1]), exist_ok=True)
        return exe.script(os.path.join(self.tmp, *where), body)

    def item(self, claude, repo):
        return setup.Setup(self.state, lambda: {}, claude=[claude], repo=repo).status()["items"][0]

    def test_the_copy_there_is_ok_another_claude_meanwhile_is_ok_and_pending(self):
        from core import ownclaude
        repo = self.release()
        newer = self.program("lib", "node_modules", "@anthropic-ai", "claude-code", "claude", body=NEW)
        c = self.item(newer, repo)
        self.assertEqual((c["state"], c["version"], c["need"], c.get("pending")), ("ok", "9.9.9", "2.1.400", True))
        self.assertNotIn("manager", c)
        older = self.program("elsewhere", "claude")
        c = self.item(older, repo)
        self.assertEqual((c["state"], c.get("pending")), ("todo", True))
        write(ownclaude.path(self.state, "2.1.400"))
        c = self.item(older, repo)
        self.assertEqual((c["state"], c["version"], c["own"]), ("ok", "2.1.400", True))
        c = self.item(older, self.app_repo(fmt=None))   # a working copy names none: as before
        self.assertEqual((c["state"], c["version"]), ("todo", "2.0.1"))
        self.assertNotIn("pending", c)

    def test_a_page_of_an_earlier_release_asking_for_npms_update_gets_the_hubs_own_copy(self):
        older = self.program("lib", "node_modules", "@anthropic-ai", "claude-code", "claude")
        real, ran = subprocess.run, []

        def run(argv, *a, **kw):
            ran.append(os.path.basename(str(argv[0])))
            return real(argv, *a, **kw)
        with mock.patch.object(subprocess, "run", run), \
                mock.patch.dict(os.environ, {"HUB_TEST_CLAUDE_RELEASE": "2.1.400"}):
            j = job.Job(self.state, "claude_upgrade")
            job.step_claude_upgrade(j)
        with open(job.status_path(self.state, "claude_upgrade")) as f:
            st = json.load(f)
        self.assertEqual((st["state"], st["why"]), ("failed", "network"))   # the fake downloads of the tests: none
        self.assertFalse([p for p in ran if p.startswith(("npm", "brew", "claude"))], ran)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".local")))


class AppChecklist(Base):
    def test_the_app_has_no_github_no_team_and_starts_nothing_from_the_page(self):
        repo = self.app_repo()
        s = setup.Setup(self.state, lambda: {}, claude=[os.path.join(self.tmp, "none")], repo=repo)
        st = s.status(full=False)
        self.assertTrue(st["app"])
        self.assertEqual([i["id"] for i in st["items"]], ["claude", "login", "chrome", "doctor"])   # always the whole
        self.assertEqual(st["auto"], [])          # auto.py installs, not the page
        self.assertEqual((st["update"], st["waits"], st["steps_running"]), (None, {}, False))
        for step in ("github", "access", "team"):
            self.assertEqual(s.run(step)[0], 400, step)
        repo = self.app_repo(fmt=None)
        write(os.path.join(repo, "modules", "private.json"), json.dumps(   # an active pin of its own: the tree's retires the modules
            {"repo": "acme/team-modules", "url": "https://example.invalid/pm.git", "ref": "pm-v1.0.0",
             "commit": "a" * 40, "modules": {"accounts": {"subdir": "accounts", "cap": "profile"}}}))
        git = setup.Setup(self.state, lambda: {}, claude=[os.path.join(self.tmp, "none")], repo=repo)
        st = git.status(full=False)
        self.assertNotIn("app", st)
        self.assertEqual(st["auto"], ["claude"])   # a hub from git: as it was
        self.assertIn("team", [i["id"] for i in git.status()["items"]])

    def test_the_band_reads_the_layers_download_while_it_is_fresh(self):
        s = setup.Setup(self.state, lambda: {}, claude=[os.path.join(self.tmp, "none")], repo=self.app_repo(),
                        clock=lambda: 1000.0)
        path = write(os.path.join(self.state, "layers", "progress.json"), json.dumps(
            {"version": "1.9.3", "phase": "download", "done": 4_000_000, "total": 11_000_000, "first": True, "ts": 990}))
        self.assertEqual(s.status()["update"], {"version": "1.9.3", "phase": "download", "done": 4_000_000,
                                                "total": 11_000_000, "first": True})
        write(path, json.dumps({"version": "1.9.3", "phase": "download", "done": 1, "total": 2, "ts": 900}))
        self.assertIsNone(s.status()["update"])   # a process that died left it
        write(path, json.dumps({"version": "1.9.3", "phase": "nonsense", "ts": 1000}))
        self.assertIsNone(s.status()["update"])

    def test_waits_only_while_auto_runs(self):
        s = setup.Setup(self.state, lambda: {}, claude=[os.path.join(self.tmp, "none")], repo=self.app_repo())
        write(os.path.join(job.folder(self.state), "auto.json"), json.dumps({"pid": os.getpid(), "waits": {"chrome": 5}}))
        self.assertEqual((s.status()["waits"], s.status()["steps_running"]), ({"chrome": 5}, True))
        write(os.path.join(job.folder(self.state), "auto.json"), json.dumps({"pid": 2 ** 22 + 12345, "waits": {"chrome": 5}}))
        self.assertEqual((s.status()["waits"], s.status()["steps_running"]), ({}, False))


class FakeSetup:
    """The checklist's facts as a test sets them; run() records what auto.py starts."""

    def __init__(self):
        self.items = {"claude": {"id": "claude", "state": "todo", "version": None, "job": None, "own": False,
                                 "pending": True},   # an app of a release: its own copy is not there yet
                      "login": {"id": "login", "state": "wait", "job": None},
                      "chrome": {"id": "chrome", "state": "todo", "available": True, "job": None},
                      "doctor": {"id": "doctor", "state": "todo", "stale": True, "job": None}}
        self.started, self.langs = [], []

    def cfg(self):
        return {}

    def status(self, full=True, working=False):
        need = ("codex", "codex_login") if "codex" in self.items else ("claude", "login")
        blocking = [k for k in need if self.items[k]["state"] not in ("ok", "unknown")]
        return {"ready": not blocking, "blocking": blocking, "items": list(self.items.values())}

    def run(self, step, lang=None):
        self.started.append(step)
        self.langs.append(lang)
        self.set(step, "running", job={"step": step, "state": "running"})
        return 202, {}

    def set(self, step, state, **kw):
        self.items[step] = {**self.items[step], "state": state, **kw}


class Auto(Base):
    def make(self):
        self.fake, self.clock, self.told = FakeSetup(), [100.0], []
        return auto.Auto(self.state, self.fake, tell=self.told.append, clock=lambda: self.clock[0], sleep=lambda s: None,
                         working=False)

    def test_the_order_claude_and_chrome_then_the_sign_in_and_doctor_last(self):
        a = self.make()
        self.assertTrue(a.round())
        self.assertEqual(self.fake.started, ["claude", "chrome"])
        self.assertTrue(a.round())
        self.assertEqual(self.fake.started, ["claude", "chrome"])   # nothing twice, doctor waits for both
        self.fake.set("claude", "ok", version="2.1.309", job={"state": "done"})
        self.fake.set("login", "todo")
        a.round()
        self.assertEqual(self.fake.started, ["claude", "chrome", "login"])   # the link waits for the person
        self.fake.set("chrome", "ok", job={"state": "done"})
        self.fake.set("login", "running", job={"state": "running", "url": "https://x"})
        self.assertTrue(a.round())        # the sign-in waits for the person: the check waits for it (it would be red)
        self.assertEqual(self.fake.started, ["claude", "chrome", "login"])
        self.assertEqual(self.told, [])
        self.fake.set("login", "ok", job={"state": "done"})
        self.assertTrue(a.round())
        self.assertEqual(self.fake.started, ["claude", "chrome", "login", "doctor"])   # last
        self.assertEqual(self.told, ["ready"])   # it was not ready at the start: one line for the owner
        self.fake.set("doctor", "ok", stale=False, job={"state": "done"})
        self.assertFalse(a.round())       # nothing left: auto.py ends

    def test_the_network_is_waited_for_and_anything_else_is_the_bands(self):
        a = self.make()
        a.round()
        self.fake.set("claude", "failed", job={"state": "failed", "why": "network"})
        self.fake.set("chrome", "failed", job={"state": "failed", "why": "space"})
        self.assertTrue(a.round())
        self.assertEqual(self.fake.started, ["claude", "chrome"])
        self.assertIn("claude", a.waits)
        with open(os.path.join(job.folder(self.state), "auto.json")) as f:
            self.assertEqual(json.load(f)["waits"], {"claude": round(100.0 + auto.WAIT)})   # the band's "will try again"
        self.clock[0] += auto.WAIT + 1
        a.round()
        self.assertEqual(self.fake.started, ["claude", "chrome", "claude"])   # the network again; chrome — the band's Retry
        self.fake.set("claude", "failed", job={"state": "failed", "why": "network"})
        a.round()
        self.assertEqual(a.waits["claude"], self.clock[0] + 2 * auto.WAIT)   # the pause doubles
        self.assertNotIn("doctor", self.fake.started)

    def test_the_releases_own_copy_is_downloaded_whatever_claude_is_there(self):
        for state, version in (("ok", "9.9.9"), ("todo", "2.0.1"), ("failed", None)):
            with self.subTest(state=state):
                a = self.make()
                self.fake.set("claude", state, version=version, own=False, pending=True)
                self.fake.set("chrome", "ok")
                a.round()
                self.assertEqual(self.fake.started, ["claude"])
        for version in ("2.0.1", None):   # a working copy names none: nothing is installed, older or missing
            a = self.make()
            self.fake.set("claude", "todo", version=version, own=None, pending=None)
            self.fake.set("chrome", "ok")
            a.round()
            self.assertNotIn("claude", self.fake.started, version)

    def test_doctor_waits_for_the_own_copys_download(self):
        a = self.make()
        self.fake.set("claude", "ok", version="9.9.9", own=False, pending=True)
        self.fake.set("login", "ok")
        self.fake.set("chrome", "ok")
        self.assertTrue(a.round())
        self.assertEqual(self.fake.started, ["claude"])   # the item stays "ok" (PATH's claude) while it downloads
        self.assertTrue(a.round())
        self.assertEqual(self.fake.started, ["claude"])   # no doctor of the claude about to be replaced
        self.fake.set("claude", "ok", version="2.1.400", own=True, pending=None, job={"state": "done"})
        a.round()
        self.assertEqual(self.fake.started, ["claude", "doctor"])

    def test_a_program_whose_version_stumbled_is_never_installed_over(self):
        a = self.make()
        self.fake.set("claude", "todo", version=None, found=True, own=None, pending=None)
        self.fake.set("chrome", "ok")
        a.round()
        self.assertNotIn("claude", self.fake.started)

    def test_doctor_runs_again_once_the_hub_turns_ready(self):
        a = self.make()
        self.fake.set("claude", "ok", version="2.1.309", own=True, pending=None)
        self.fake.set("chrome", "ok")
        self.fake.set("login", "todo")
        a.round()
        self.assertEqual(self.fake.started, ["login"])
        self.fake.set("login", "failed", job={"state": "failed", "why": "timeout"})   # nobody came for ten minutes
        a.round()
        self.assertEqual(self.fake.started, ["login", "doctor"])   # the check of a hub still without a sign-in
        self.fake.set("doctor", "fail", stale=False, job={"state": "done"})
        self.fake.set("login", "ok", job={"state": "done"})   # signed in later from the band
        a.round()
        self.assertEqual(self.fake.started, ["login", "doctor", "doctor"])   # not left red for a hub that is ready

    def test_a_ready_hub_starts_only_a_stale_doctor(self):
        a = self.make()
        for k in ("claude", "login", "chrome"):
            self.fake.set(k, "ok")
        self.fake.set("claude", "ok", pending=None)   # its own copy is there
        self.fake.set("doctor", "ok", stale=False)
        self.assertFalse(a.round())
        self.assertEqual((self.fake.started, self.told), ([], []))

    def test_a_check_that_found_trouble_before_this_start_is_done_again(self):
        # a red band over a hub whose sessions answered: the check ran before a sign-in key came with this start
        for level in ("fail", "warn", "ok"):
            with self.subTest(level=level):
                a = self.make()
                for k in ("claude", "login", "chrome"):
                    self.fake.set(k, "ok")
                self.fake.set("claude", "ok", pending=None)
                self.fake.set("doctor", level, stale=False, job={"state": "done", "ts": 99.0})
                a.round()
                self.assertEqual(self.fake.started, [] if level == "ok" else ["doctor"])
                self.fake.set("doctor", level, stale=False, job={"state": "done", "ts": 101.0})
                a.round()   # its own check found it again: the band's, not a loop
                self.assertEqual(self.fake.started, [] if level == "ok" else ["doctor"])

    def test_the_steps_speak_the_language_the_owners_page_said(self):
        # a fresh hub on an English machine, the owner's browser in Russian (reducer owner.came)
        write(os.path.join(self.state, "state.json"), json.dumps(
            {"seq": 1, "state": {"owner": {"came": {"ts": 1, "lang": "ru", "via": "web"}}}}))
        with mock.patch.dict(os.environ, {"HUB_LANG": "en"}):
            a = self.make()
            a.round()
        self.assertEqual((self.fake.started, self.fake.langs), (["claude", "chrome"], ["ru", "ru"]))

    def test_a_chrome_the_system_does_not_let_start_is_not_downloaded(self):
        a = self.make()
        self.fake.set("chrome", "todo", sandbox="sudo agents-hub chrome")
        a.round()
        self.assertEqual(self.fake.started, ["claude"])

    def test_a_hub_from_git_runs_nothing(self):
        with mock.patch.object(setup, "is_app", lambda repo=None: False):
            self.assertEqual(auto.main([self.state]), 0)
        self.assertFalse(os.path.exists(os.path.join(job.folder(self.state), "auto.json")))


class HubUpStartsIt(Base):
    PROBE = ("import json, runpy, subprocess, sys\nseen = []\n"
             "subprocess.Popen = lambda cmd, **kw: seen.append([cmd[-2].replace('\\\\', '/').split('/')[-1], cmd[-1]])\n"
             "g = runpy.run_path(sys.argv[1], run_name='probe')\ng['start_setup']()\nprint(json.dumps(seen))\n")

    def probe(self, mark):
        env = {k: v for k, v in os.environ.items() if k not in ("HUB_UP_PARTS", "HUB_PACKAGED_MARK")}
        if mark:
            env["HUB_PACKAGED_MARK"] = mark
        r = subprocess.run([sys.executable, "-c", self.PROBE, os.path.join(REPO, "bin", "hub-up")], capture_output=True,
                           text=True, encoding="utf-8", timeout=60, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_only_the_app_starts_the_steps(self):
        app = write(os.path.join(self.tmp, "PACKAGED"), json.dumps({"format": "app", "version": "1.2.3", "runtime": 1}))
        self.assertEqual(self.probe(app), [["auto.py", self.state]])
        deb = write(os.path.join(self.tmp, "PACKAGED.deb"), json.dumps({"format": "deb", "version": "1.2.3"}))
        self.assertEqual(self.probe(deb), [])
        self.assertEqual(self.probe(None), [])   # this checkout: a hub from git


if __name__ == "__main__":
    unittest.main()


def tarball(path, member, body=b"#!/bin/sh\necho codex-cli 0.162.1\n"):
    import io
    import tarfile
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo(member)
        info.size, info.mode = len(body), 0o755
        tar.addfile(info, io.BytesIO(body))
    return path


HELPER = b"#!/bin/sh\necho code-mode-host\n"
BWRAP = b"#!/bin/sh\necho bwrap\n"


class Served:
    """A folder of release files behind http://127.0.0.1:<port> (the releases' layout rust-v<version>/<asset>)."""

    def __init__(self, test, root):
        import functools
        import http.server
        import threading
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=root)
        handler.log_message = lambda *a: None
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        test.addCleanup(self.srv.server_close)
        test.addCleanup(self.srv.shutdown)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"


class CodexFromReleases(Base):
    def stand(self, version="0.162.1", member=None, body=None, tamper=False):
        import hashlib
        from core import codexinstall
        triple = codexinstall.target()
        if triple is None:
            self.skipTest("no Codex build for this machine")
        name = codexinstall.PIN["assets"][triple][0]
        root = os.path.join(self.tmp, "releases")
        os.makedirs(os.path.join(root, f"rust-v{version}"))
        path = tarball(os.path.join(root, f"rust-v{version}", name), member or name[: -len(".tar.gz")],
                       **({"body": body} if body else {}))
        data = open(path, "rb").read()
        sha = hashlib.sha256(data).hexdigest()
        # the helper codex starts for its tools (code_mode_host is on by default): an asset of its own in the release
        hname = name.replace("codex-", "codex-code-mode-host-", 1)
        hpath = tarball(os.path.join(root, f"rust-v{version}", hname), hname[: -len(".tar.gz")], body=HELPER)
        hdata = open(hpath, "rb").read()
        hsha = "0" * 64 if tamper == "helper" else hashlib.sha256(hdata).hexdigest()
        pin = {"version": version, "assets": {triple: (name, len(data), "0" * 64 if tamper is True else sha)},
               "helpers": {triple: (hname, len(hdata), hsha)}}
        if triple.endswith("linux-musl"):   # the Linux sandbox's bubblewrap: an asset of its own too
            bname = f"bwrap-{triple}.tar.gz"
            bpath = tarball(os.path.join(root, f"rust-v{version}", bname), bname[: -len(".tar.gz")], body=BWRAP)
            bdata = open(bpath, "rb").read()
            pin["bwrap"] = {triple: (bname, len(bdata), "0" * 64 if tamper == "bwrap" else hashlib.sha256(bdata).hexdigest())}
        self.served = os.path.join(root, f"rust-v{version}")
        return Served(self, root).url, pin

    def installer(self, url, pin, answer="codex-cli 0.162.1"):
        from core import codexinstall
        self.ran = []

        def run(cmd, **kw):
            self.ran.append((cmd, kw.get("env", {}).get("CODEX_HOME")))
            return subprocess.CompletedProcess(cmd, 0, answer, "")
        return codexinstall.Installer(self.state, root_url=url, home=self.home, run=run, pin=pin)

    def test_the_pinned_release_goes_to_local_bin_checked_and_run_with_the_hubs_codex_home(self):
        from core import codexcli, codexinstall
        url, pin = self.stand()
        got, path = self.installer(url, pin).run_install()
        self.assertEqual((got, path), ("0.162.1", codexinstall.place(self.home)))
        with open(path, "rb") as f:
            self.assertIn(b"codex-cli", f.read())
        self.assertFalse(os.listdir(os.path.join(self.state, "downloads")))   # the archive goes
        cmd, home = self.ran[-1]
        self.assertEqual((cmd, home), ([path, "--version"], codexcli.home_of(self.state)))   # never the personal ~/.codex

    def test_the_helper_codex_starts_for_its_tools_goes_next_to_it(self):
        # 0.162.1 runs a tool call through codex-code-mode-host beside the codex file (a separate asset of the release:
        # the codex archive holds the one file) — without it every tool call of a session failed
        from core import codexinstall
        url, pin = self.stand()
        got, path = self.installer(url, pin).run_install()
        helper = codexinstall.helper_of(path)
        self.assertEqual((os.path.dirname(helper), os.path.basename(helper).split(".")[0]),
                         (os.path.dirname(path), "codex-code-mode-host"))
        with open(helper, "rb") as f:
            self.assertEqual(f.read(), HELPER)
        if os.name == "posix":
            self.assertTrue(os.access(helper, os.X_OK))
        self.assertTrue(codexinstall.complete(path))
        self.assertFalse(os.listdir(os.path.join(self.state, "downloads")))   # both archives go
        beside = [os.path.basename(path), os.path.basename(helper)] + (["codex-resources"] if "linux" in codexinstall.target() else [])
        self.assertEqual(sorted(os.listdir(os.path.dirname(path))), sorted(beside))   # no .part left

    def test_the_bubblewrap_of_codexs_sandbox_goes_beside_it_on_linux(self):
        # without bwrap in PATH or codex-resources/bwrap beside the codex, a command of a session in a sandbox mode (and of
        # the bus liaison) panics "bubblewrap is unavailable" — a machine without the system's bubblewrap (a container, a
        # slim server) lost them; Windows and macOS have no such file
        from core import codexinstall
        url, pin = self.stand()
        got, path = self.installer(url, pin).run_install()
        if "linux" not in codexinstall.target():
            self.assertEqual(codexinstall.needs(path), [codexinstall.helper_of(path)])
            self.assertNotIn("bwrap", pin)
            return
        bwrap = codexinstall.bwrap_of(path)
        self.assertEqual(os.path.relpath(bwrap, os.path.dirname(path)), os.path.join("codex-resources", "bwrap"))
        with open(bwrap, "rb") as f:
            self.assertEqual(f.read(), BWRAP)
        self.assertTrue(os.access(bwrap, os.X_OK))
        self.assertEqual(os.listdir(os.path.dirname(bwrap)), ["bwrap"])   # no .part left
        self.assertTrue(codexinstall.complete(path, home=self.home))
        os.remove(bwrap)
        self.assertFalse(codexinstall.complete(path, home=self.home))   # codex and its helper alone are not whole on Linux

    def test_a_helper_that_does_not_match_its_sum_stops_the_install_before_codex_is_replaced(self):
        from core import codexinstall
        url, pin = self.stand(tamper="helper")
        write(codexinstall.place(self.home), "the codex that works")
        with self.assertRaises(codexinstall.Failed) as e:
            self.installer(url, pin).run_install()
        self.assertEqual(e.exception.why, "checksum")
        with open(codexinstall.place(self.home)) as f:
            self.assertEqual(f.read(), "the codex that works")
        self.assertFalse(os.path.exists(codexinstall.helper_of(codexinstall.place(self.home))))

    def test_a_bubblewrap_that_does_not_match_its_sum_stops_the_install_before_codex_is_replaced(self):
        from core import codexinstall
        if "linux" not in (codexinstall.target() or ""):
            self.skipTest("bubblewrap is Linux's")
        url, pin = self.stand(tamper="bwrap")
        write(codexinstall.place(self.home), "the codex that works")
        with self.assertRaises(codexinstall.Failed) as e:
            self.installer(url, pin).run_install()
        self.assertEqual(e.exception.why, "checksum")
        with open(codexinstall.place(self.home)) as f:
            self.assertEqual(f.read(), "the codex that works")
        self.assertFalse(os.path.exists(codexinstall.helper_of(codexinstall.place(self.home))))   # nothing was placed

    def test_every_build_of_the_pin_has_a_helper_with_its_sum(self):
        from core import codexinstall
        pin = codexinstall.PIN
        self.assertEqual(set(pin["helpers"]), set(pin["assets"]))
        for triple, (name, size, sha) in pin["helpers"].items():
            self.assertEqual(name, pin["assets"][triple][0].replace("codex-", "codex-code-mode-host-", 1))
            self.assertRegex(sha, r"^[0-9a-f]{64}$")
            self.assertGreater(size, 1e6)
        # a Linux build has the bubblewrap of the sandbox, the others have none
        self.assertEqual(set(pin["bwrap"]), {t for t in pin["assets"] if t.endswith("linux-musl")})
        for triple, (name, size, sha) in pin["bwrap"].items():
            self.assertEqual(name, f"bwrap-{triple}.tar.gz")
            self.assertRegex(sha, r"^[0-9a-f]{64}$")
            self.assertGreater(size, 1e5)

    def test_what_must_be_beside_a_codex_depends_on_the_machine(self):
        from core import codexinstall
        path = os.path.join(self.tmp, "codex")
        self.assertEqual(codexinstall.needs(path, "linux-x64"), [codexinstall.helper_of(path), codexinstall.bwrap_of(path)])
        self.assertEqual(codexinstall.needs(path, "linux-arm64-musl")[1], codexinstall.bwrap_of(path))
        for plat in ("darwin-arm64", "darwin-x64", "win32-x64", "win32-arm64"):
            self.assertEqual(codexinstall.needs(path, plat), [codexinstall.helper_of(path)], plat)

    def test_a_codex_the_hub_put_without_its_helper_is_not_complete(self):
        from core import codexinstall
        path = write(codexinstall.place(self.home), "codex")
        self.assertFalse(codexinstall.complete(path, home=self.home))
        for beside in codexinstall.needs(path):
            write(beside, "host")
        self.assertTrue(codexinstall.complete(path, home=self.home))
        # not the hub's copy (npm's, Homebrew's, a link to either): its own installer brought what it needs
        theirs = write(os.path.join(self.tmp, "npm", "codex"), "codex")
        self.assertTrue(codexinstall.complete(theirs, home=self.home))

    def test_hub_setup_codex_adds_only_the_helper_to_a_codex_of_1_9_0(self):
        from core import codexinstall
        url, pin = self.stand()
        path = write(codexinstall.place(self.home), "codex of 1.9.0")
        os.chmod(path, 0o755)
        inst = self.installer(url, pin)
        got, at = inst.run_install(extras_only=True)
        self.assertEqual((got, at), ("0.162.1", path))
        with open(path) as f:
            self.assertEqual(f.read(), "codex of 1.9.0")   # the file that works is not downloaded again
        self.assertTrue(codexinstall.complete(path, home=self.home))
        self.assertEqual(os.listdir(os.path.join(self.state, "downloads")), [])

    def test_a_codex_with_its_helper_but_no_bubblewrap_gets_only_the_bubblewrap(self):
        # the first fix put the helper and no bubblewrap: a hub of it on a machine without the system's one
        import contextlib
        import io
        from core import codexinstall
        if "linux" not in (codexinstall.target() or ""):
            self.skipTest("bubblewrap is Linux's")
        url, pin = self.stand()
        path = write(os.path.join(self.tmp, "mine", "codex"), "codex of 1.9.1")
        write(codexinstall.helper_of(path), "host")
        env = {"HUB_CODEX_BIN": path, "HUB_CODEX_DOWNLOADS": url}
        with mock.patch.dict(os.environ, env), mock.patch.object(codexinstall, "PIN", pin), \
                mock.patch("core.codexinstall.place", lambda home=None: path), \
                mock.patch.object(codexinstall.Installer, "version_of", lambda self, e: "0.162.1"), \
                mock.patch("core.codexinstall.Installer.unpack", autospec=True,
                           side_effect=codexinstall.Installer.unpack) as unpack:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(codexinstall.main([], self.state), 0)
            self.assertEqual([c.args[3] for c in unpack.call_args_list], [codexinstall.bwrap_of(path)])
        self.assertTrue(codexinstall.complete(path, home=self.home, state=self.state))

    def test_hub_setup_codex_heals_a_codex_of_1_9_0_and_leaves_a_whole_one_alone(self):
        import contextlib
        import io
        from core import codexinstall
        url, pin = self.stand()
        path = write(os.path.join(self.tmp, "mine", "codex"), "codex of 1.9.0")
        env = {"HUB_CODEX_BIN": path, "HUB_CODEX_DOWNLOADS": url}
        with mock.patch.dict(os.environ, env), mock.patch.object(codexinstall, "PIN", pin), \
                mock.patch("core.codexinstall.place", lambda home=None: path), \
                mock.patch.object(codexinstall.Installer, "version_of", lambda self, e: "0.162.1"), \
                mock.patch("core.codexinstall.Installer.unpack", autospec=True,
                           side_effect=codexinstall.Installer.unpack) as unpack:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(codexinstall.main([], self.state), 0)
            self.assertTrue(os.path.isfile(codexinstall.helper_of(path)))
            self.assertEqual([c.args[3] for c in unpack.call_args_list], codexinstall.needs(path))   # only those
            with open(path) as f:
                self.assertEqual(f.read(), "codex of 1.9.0")
            os.environ["HUB_CODEX_DOWNLOADS"] = "http://127.0.0.1:9"   # nothing to fetch: a second run asks no server
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(codexinstall.main([], self.state), 0)
            self.assertIn("already here", out.getvalue())

    def test_the_pages_job_fetches_only_the_helper_for_a_pinned_codex_that_lacks_it(self):
        from core import codexinstall
        url, pin = self.stand()
        path = write(codexinstall.place(self.home), "codex of 1.9.0")
        os.chmod(path, 0o755)
        inst = self.installer(url, pin)
        with mock.patch("core.codexinstall.Installer.unpack", autospec=True,
                        side_effect=codexinstall.Installer.unpack) as unpack:
            inst.ensure_whole()
            self.assertEqual([c.args[3] for c in unpack.call_args_list], codexinstall.needs(path))
            for beside in codexinstall.needs(path):
                os.remove(beside)
            os.remove(path)   # none at all: the whole install
            unpack.reset_mock()
            inst.ensure_whole()
            self.assertEqual([c.args[3] for c in unpack.call_args_list], [*codexinstall.needs(path), path])

    def test_hub_setup_codex_puts_the_helper_beside_the_codex_that_runs(self):
        # the hub's own copy at <state>/bin (what a second hub puts) on a hub that is not marked as one: the helper goes
        # beside it, not into ~/.local/bin
        import contextlib
        import io
        from core import codexinstall
        url, pin = self.stand()
        path = write(codexinstall.own_place(self.state), "codex of 1.9.0")
        with mock.patch.dict(os.environ, {"HUB_CODEX_BIN": path, "HUB_CODEX_DOWNLOADS": url}), \
                mock.patch.object(codexinstall, "PIN", pin), \
                mock.patch("core.codexinstall.place", lambda home=None: os.path.join(self.home, "codex")), \
                mock.patch.object(codexinstall.Installer, "version_of", lambda self, e: "0.162.1"):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(codexinstall.main([], self.state), 0)
        self.assertTrue(os.path.isfile(codexinstall.helper_of(path)))
        self.assertEqual(os.listdir(self.home), [])

    def test_a_sum_that_does_not_match_is_never_placed(self):
        from core import codexinstall
        url, pin = self.stand(tamper=True)
        with self.assertRaises(codexinstall.Failed) as e:
            self.installer(url, pin).run_install()
        self.assertEqual(e.exception.why, "checksum")
        self.assertFalse(os.path.exists(codexinstall.place(self.home)))
        self.assertEqual(self.ran, [])

    def test_an_archive_without_the_binary_or_too_old_a_version_is_refused(self):
        from core import codexinstall
        url, pin = self.stand(member="something-else")
        with self.assertRaises(codexinstall.Failed) as e:
            self.installer(url, pin).run_install()
        self.assertEqual(e.exception.why, "install")
        url, pin = self.stand(version="0.150.0")
        with self.assertRaises(codexinstall.Failed) as e:
            self.installer(url, pin, answer="codex-cli 0.150.0").run_install()
        self.assertEqual(e.exception.why, "old")

    def test_the_machines_build(self):
        from core import codexinstall
        self.assertEqual(codexinstall.target("darwin-arm64"), "aarch64-apple-darwin")
        self.assertEqual(codexinstall.target("linux-x64"), "x86_64-unknown-linux-musl")
        self.assertEqual(codexinstall.target("win32-x64"), "x86_64-pc-windows-msvc")
        self.assertIsNone(codexinstall.target(""))
        self.assertEqual(set(codexinstall.TRIPLES.values()), set(codexinstall.PIN["assets"]))
        with self.assertRaises(codexinstall.Failed) as e:
            codexinstall.Installer(self.state, pin={"version": "0.162.1", "assets": {}}).run_install()
        self.assertEqual(e.exception.why, "platform")
        self.assertEqual(set(codexinstall.PIN["assets"]), {f"{a}-{s}" for a in ("x86_64", "aarch64") for s in (
            "apple-darwin", "unknown-linux-musl", "pc-windows-msvc")})


class CodexInTheApp(Base):
    def make(self, codex=None, version=None):
        from core import codexinstall
        s = setup.Setup(self.state, lambda: {"host": "codex"}, repo=self.app_repo())
        s.codex_program = lambda: codex
        s.codex_version = lambda: version
        self.own = codexinstall.place()
        return s

    def test_codex_none_old_and_the_hubs_own(self):
        st = self.make().status()
        self.assertEqual(([i["id"] for i in st["items"]], st["host"]), (["codex", "codex_login", "chrome", "doctor"], "codex"))
        x = st["items"][0]
        self.assertEqual((x["state"], x["own"], x["need"]), ("todo", False, "0.162.1"))
        x = self.make(codex=os.path.join(self.tmp, "npm", "codex"), version="0.150.0").status()["items"][0]
        self.assertEqual((x["state"], x["own"]), ("todo", False))   # npm's: the band says to update it there
        with mock.patch("core.codexinstall.place", lambda home=None: os.path.join(self.tmp, "mine", "codex")):
            write(os.path.join(self.tmp, "mine", "codex"))
            x = self.make(codex=os.path.join(self.tmp, "mine", "codex"), version="0.150.0").status()["items"][0]
        self.assertTrue(x["own"])
        x = self.make(codex="/x/codex", version="0.162.1").status()["items"][0]
        self.assertEqual(x["state"], "ok")

    def test_the_hubs_own_codex_without_its_helper_is_not_done(self):
        # the first release put the codex alone: it answers --version, every tool call of a session fails — the band
        # (and the auto start behind it) puts it anew
        from core import codexinstall
        mine = os.path.join(self.tmp, "mine", "codex")
        with mock.patch("core.codexinstall.place", lambda home=None: mine):
            write(mine)
            x = self.make(codex=mine, version="0.162.1").status()["items"][0]
            self.assertEqual((x["state"], x["own"], x["version"]), ("todo", True, "0.162.1"))
            for beside in codexinstall.needs(mine):
                write(beside)
            x = self.make(codex=mine, version="0.162.1").status()["items"][0]
            self.assertEqual(x["state"], "ok")
        x = self.make(codex=os.path.join(self.tmp, "npm", "codex"), version="0.170.0").status()["items"][0]
        self.assertEqual(x["state"], "ok")   # someone else's brings its own

    def test_doctor_names_a_codex_without_its_helper(self):
        from core import modcli
        os.makedirs(os.path.join(self.tmp, "mine"))
        path = exe.script(os.path.join(self.tmp, "mine", "codex"), "print('codex-cli 0.162.1')\n")
        with mock.patch.dict(os.environ, {"HUB_CODEX_BIN": path}), \
                mock.patch("core.codexinstall.place", lambda home=None: path), \
                mock.patch("core.codexcli._prepare", lambda *a, **kw: None):
            rows = modcli.codex_checks(self.state)
            self.assertEqual(rows[0][0], "fail")
            self.assertIn("codex-code-mode-host", rows[0][2])
            self.assertIn("setup codex", rows[0][2])

    def test_doctor_names_the_missing_bubblewrap_of_a_codex_that_has_its_helper(self):
        from core import codexinstall, modcli
        if "linux" not in (codexinstall.target() or ""):
            self.skipTest("bubblewrap is Linux's")
        os.makedirs(os.path.join(self.tmp, "mine"))
        path = exe.script(os.path.join(self.tmp, "mine", "codex"), "print('codex-cli 0.162.1')\n")
        write(codexinstall.helper_of(path))
        with mock.patch.dict(os.environ, {"HUB_CODEX_BIN": path}), \
                mock.patch("core.codexinstall.place", lambda home=None: path), \
                mock.patch("core.codexcli._prepare", lambda *a, **kw: None):
            rows = modcli.codex_checks(self.state)
            self.assertEqual(rows[0][0], "fail")
            self.assertIn(os.path.join("codex-resources", "bwrap"), rows[0][2])
            self.assertNotIn("codex-code-mode-host", rows[0][2])
            self.assertIn("setup codex", rows[0][2])

    def test_auto_installs_codex_then_starts_its_sign_in_once(self):
        fake = FakeSetup()
        fake.items = {"codex": {"id": "codex", "state": "todo", "version": None, "own": False, "job": None},
                      "codex_login": {"id": "codex_login", "state": "wait", "job": None},
                      "chrome": {"id": "chrome", "state": "ok", "job": None},
                      "doctor": {"id": "doctor", "state": "ok", "stale": False, "job": None}}
        signed = []
        fake.codex_sign_in = lambda: signed.append(1)
        a = auto.Auto(self.state, fake, clock=lambda: 100.0, sleep=lambda s: None, working=False)
        self.assertTrue(a.round())
        self.assertEqual(fake.started, ["codex"])
        fake.set("codex", "ok", version="0.162.1", job={"state": "done"})
        fake.set("codex_login", "todo")
        self.assertTrue(a.round())
        a.round()
        self.assertEqual(signed, [1])   # its code waits on the band; never a second one on top
        fake.items["codex"] = {"id": "codex", "state": "failed", "version": None, "own": False, "found": True, "job": None}
        b = auto.Auto(self.state, fake, clock=lambda: 100.0, sleep=lambda s: None, working=False)
        fake.started.clear()
        b.round()
        self.assertEqual(fake.started, [])   # npm's whose --version stumbled: not a second copy into ~/.local/bin
        fake.items["codex"] = {"id": "codex", "state": "todo", "version": "0.150.0", "own": False, "job": None}
        b = auto.Auto(self.state, fake, clock=lambda: 100.0, sleep=lambda s: None, working=False)
        fake.started.clear()
        b.round()
        self.assertEqual(fake.started, [])   # an older one of npm or Homebrew: never installed over

    def test_doctor_names_an_old_codex(self):
        from core import modcli
        old = exe.script(os.path.join(self.tmp, "codex"), "print('codex-cli 0.150.0')\n")
        with mock.patch.dict(os.environ, {"HUB_CODEX_BIN": old}), \
                mock.patch("core.codexcli._prepare", lambda *a, **kw: None):
            rows = modcli.codex_checks(self.state)
        self.assertEqual(rows[0][0], "fail")
        self.assertIn("0.162.1", rows[0][2])


class SetupCodexCommand(Base):
    def test_a_foreign_codex_first_in_path_is_not_shadowed(self):
        from core import codexinstall
        os.makedirs(os.path.join(self.tmp, "npm"), exist_ok=True)
        foreign = exe.script(os.path.join(self.tmp, "npm", "codex"), "print('codex-cli 0.150.0')\n")
        with mock.patch("hosts.codex.codex_bin", lambda state_dir=None: foreign), \
                mock.patch.object(codexinstall.Installer, "version_of", lambda self, e: "0.150.0"), \
                mock.patch.object(codexinstall.Installer, "run_install", lambda self: self.fail("installed")):
            self.assertEqual(codexinstall.main(["--force"], self.state), 1)


class EngineChoice(Base):
    def web(self, app):
        from modules.web import server
        cfg = os.path.join(self.tmp, "cfg", "hub.json")
        write(cfg, json.dumps({"hub": {"enabled": True}}))
        env = mock.patch.dict(os.environ, {"HUB_CONFIG": cfg})
        env.start()
        self.addCleanup(env.stop)
        w = server.Web.__new__(server.Web)
        w.state_dir, w.spool = self.state, os.path.join(self.state, "spool")
        w.t = lambda key, **kw: key
        w._setup = setup.Setup(self.state, lambda: {}, repo=self.app_repo("app" if app else None))
        w.setup = lambda: w._setup
        w._setup.codex_program, w._setup.codex_version = lambda: "/x/codex", lambda: "0.162.1"   # Codex CLI is there
        self.steps = []
        w._setup.run = lambda step, **kw: self.steps.append(step) or (202, {"ok": True, "step": step})
        return w, cfg

    def taken(self):
        from core import spool
        got = spool.take(os.path.join(self.state, "spool"))
        for path, _ in got:   # read once: the next look sees only what came after
            os.remove(path)
        return [(item["type"], item["data"].get("parts"), item["data"].get("now"), item["data"].get("owner"))
                for _, item in got]

    def no_codex(self, w):
        w._setup.codex_program, w._setup.codex_version = lambda: None, lambda: None

    def installer(self, fail=None):
        from core import codexinstall

        class Fake:
            def ensure_whole(self):
                if fail:
                    raise codexinstall.Failed("network", fail)
                return "0.162.1", "/x/codex"
        return Fake()

    def test_codex_not_there_is_installed_before_the_restart(self):
        # 2026-11-19, a fresh hub: «Codex» on the band asked the restart at once, Codex CLI came 7 s later — hubd came up
        # with a name to nothing, the hook's trust was never written and every session start was refused
        w, cfg = self.web(app=True)
        self.no_codex(w)
        self.assertEqual(w.setup_host({"host": "codex"}), (202, {"ok": True, "host": "codex", "installing": True}))
        with open(cfg, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["host"], "codex")
        self.assertEqual((self.steps, self.taken()), (["codex"], []))   # the install first, no restart yet
        j = job.Job(self.state, "codex")
        job.step_codex(j, installer=self.installer())
        self.assertEqual(self.taken(), [("hub.restart_requested", [], True, True)])   # now, once it is in place
        self.assertFalse(job.codex_restart_waits(self.state))
        job.step_codex(job.Job(self.state, "codex"), installer=self.installer())
        self.assertEqual(self.taken(), [])   # a later install (Settings, a retry) restarts nothing

    def test_a_failed_install_restarts_nothing_and_the_band_says_why(self):
        w, cfg = self.web(app=True)
        self.no_codex(w)
        w.setup_host({"host": "codex"})
        job.step_codex(job.Job(self.state, "codex"), installer=self.installer(fail="no network to github.com"))
        self.assertEqual(self.taken(), [])
        w._setup.cache.clear()
        x = w._setup.item_codex()
        self.assertEqual((x["state"], x["job"]["why"]), ("failed", "network"))
        self.assertIn("no network", x["job"]["text"])
        job.step_codex(job.Job(self.state, "codex"), installer=self.installer())   # «Retry» on the band
        self.assertEqual(self.taken(), [("hub.restart_requested", [], True, True)])

    def test_back_to_claude_while_codex_installs_restarts_nothing(self):
        w, cfg = self.web(app=True)
        self.no_codex(w)
        w.setup_host({"host": "codex"})
        self.assertEqual(w.setup_host({"host": "claude"}), (200, {"ok": True, "host": "claude"}))
        with open(cfg, encoding="utf-8") as f:
            self.assertNotIn("host", json.load(f))
        job.step_codex(job.Job(self.state, "codex"), installer=self.installer())
        self.assertEqual(self.taken(), [])   # hubd never left Claude: neither the switch back nor the install restarts
        w._setup.codex_program, w._setup.codex_version = lambda: "/x/codex", lambda: "0.162.1"
        w.setup_host({"host": "codex"})   # Codex CLI is there now: the restart at once, as before
        self.assertEqual(self.taken(), [("hub.restart_requested", [], True, True)])

    def test_a_restart_asked_is_asked_once_and_its_mark_goes_when_hubd_comes_up(self):
        w, cfg = self.web(app=True)
        self.no_codex(w)
        w.setup_host({"host": "codex"})
        job.step_codex(job.Job(self.state, "codex"), installer=self.installer())
        self.assertEqual(job.codex_restart(self.state), job.ASKED)   # the old run of auto.py still holds the sign-in
        job.step_codex(job.Job(self.state, "codex"), installer=self.installer())
        self.assertEqual(len(self.taken()), 1)   # asked once
        from tests.test_hub_up_extensions import load_hub_up
        load_hub_up().drop_codex_restart()   # hub-up starts hubd anew: it reads the host from hub.json (HUB_STATE_DIR)
        self.assertIsNone(job.codex_restart(self.state))

    def test_a_spool_that_fails_after_the_install_still_finishes_the_step(self):
        w, cfg = self.web(app=True)
        self.no_codex(w)
        w.setup_host({"host": "codex"})
        with mock.patch("core.spool.put", side_effect=OSError("disk full")):
            job.step_codex(job.Job(self.state, "codex"), installer=self.installer())
        self.assertEqual(w._setup.job("codex")["state"], "done")

    def test_codex_put_in_place_by_hand_after_a_failed_install_restarts_from_auto(self):
        fake = FakeSetup()
        fake.items = {"codex": {"id": "codex", "state": "ok", "version": "0.170.0", "own": False, "found": True,
                                "job": {"state": "failed", "why": "network"}},
                      "codex_login": {"id": "codex_login", "state": "todo", "job": None},
                      "chrome": {"id": "chrome", "state": "ok", "job": None},
                      "doctor": {"id": "doctor", "state": "ok", "stale": False, "job": None}}
        fake.codex_sign_in = lambda: self.fail("a sign-in before the restart onto Codex")
        job.restart_after_codex(self.state)
        auto.Auto(self.state, fake, clock=lambda: 0.0, sleep=lambda s: None, working=False).round()
        self.assertEqual(self.taken(), [("hub.restart_requested", [], True, True)])
        self.assertEqual(job.codex_restart(self.state), job.ASKED)

    def test_auto_starts_no_codex_sign_in_before_the_restart_onto_codex(self):
        fake = FakeSetup()
        fake.items = {"codex": {"id": "codex", "state": "ok", "version": "0.162.1", "own": True, "job": None},
                      "codex_login": {"id": "codex_login", "state": "todo", "job": None},
                      "chrome": {"id": "chrome", "state": "ok", "job": None},
                      "doctor": {"id": "doctor", "state": "ok", "stale": False, "job": None}}
        signed = []
        fake.codex_sign_in = lambda: signed.append(1)
        job.restart_after_codex(self.state)
        a = auto.Auto(self.state, fake, clock=lambda: 100.0, sleep=lambda s: None, working=False)
        a.round()
        self.assertEqual(signed, [])   # hubd still runs on Claude: the sign-in is the run after the restart
        job.restart_after_codex(self.state, on=False)
        a.round()
        self.assertEqual(signed, [1])

    def test_codex_on_the_band_writes_the_host_and_restarts_the_hub(self):
        from core import spool
        w, cfg = self.web(app=True)
        self.assertEqual(w.setup_host({"host": "codex"})[0], 202)
        self.assertEqual(json.load(open(cfg))["host"], "codex")
        kinds = [(item["type"], item["data"].get("parts"), item["data"].get("owner")) for _, item in
                 spool.take(os.path.join(self.state, "spool"))]
        self.assertEqual(kinds, [("hub.restart_requested", [], True)])
        self.assertEqual(w.setup_host({"host": "codex"}), (200, {"ok": True, "host": "codex", "same": True}))
        self.assertEqual(w.setup_host({"host": "claude"})[0], 202)
        self.assertNotIn("host", json.load(open(cfg)))
        self.assertEqual(w.setup_host({"host": "gpt"})[0], 400)

    def test_a_hub_from_git_chooses_in_settings(self):
        w, cfg = self.web(app=False)
        self.assertEqual(w.setup_host({"host": "codex"})[0], 400)
        self.assertNotIn("host", json.load(open(cfg)))
