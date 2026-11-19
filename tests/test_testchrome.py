"""The "Chrome for tests" module (owner review of PR #17: modeled on VOICE_CMD, the system Chrome by default): the path
to the e2e tests' browser is HUB_TEST_CHROME or testchrome.path in hub.json, otherwise headless-shell / Chrome for
Testing, else the system Chrome with its own profile (on macOS never); a hub update passes the path to the tests;
"Settings → Modules" shows what is chosen."""
import tests  # noqa: F401 — keeps the machine's settings out
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

import platforms
from modules.testchrome import chrome
from modules.update import update
from modules.web import modinfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestChrome(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.exe = os.path.join(self.dir, "chrome-headless-shell")
        open(self.exe, "w").close()
        env = unittest.mock.patch.dict(os.environ)   # the run passed its own path (tests/__init__.py) — restore it after the test
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(chrome.ENV, None)
        mac = unittest.mock.patch.object(chrome, "MAC", True)   # banning the regular Chrome is a macOS rule; the tests run on Linux too
        mac.start()
        self.addCleanup(mac.stop)

    def test_the_system_chrome_is_banned_only_on_macos(self):
        # 10.10: Linux and Windows have no dock — without a special build the page tests use the system Chrome instead of
        # being skipped (110 page tests were lost on the runner)
        with unittest.mock.patch.object(chrome, "system", return_value="/sys/chrome"), \
                unittest.mock.patch.object(chrome, "shell", return_value=None), \
                unittest.mock.patch.object(chrome, "testing_build", return_value=None):
            self.assertIsNone(chrome.find({}))
            with unittest.mock.patch.object(chrome, "MAC", False):
                self.assertEqual(chrome.find({}), "/sys/chrome")

    def test_where_apparmor_denies_the_sandbox_the_system_chrome_goes_first(self):
        # 13.10: a session installed the Playwright browsers — the page, schema and snapshot tests took its
        # headless-shell, and Ubuntu (AppArmor forbids namespaces without its own profile) gave it no sandbox: "No usable
        # sandbox", everything red. The system Chrome has an AppArmor profile — it goes first; an explicit path still wins
        with unittest.mock.patch.object(chrome, "MAC", False), \
                unittest.mock.patch.object(chrome, "system", return_value="/sys/chrome"), \
                unittest.mock.patch.object(chrome, "shell", return_value="/pw/chrome-headless-shell"):
            with unittest.mock.patch.object(chrome, "sandbox_blocked", return_value=True):
                self.assertEqual(chrome.find({}), "/sys/chrome")
                self.assertEqual(chrome.find({"testchrome": {"path": self.exe}}), self.exe)
                with unittest.mock.patch.object(chrome, "system", return_value=None):
                    self.assertEqual(chrome.find({}), "/pw/chrome-headless-shell")   # there is no other — this one will do
            with unittest.mock.patch.object(chrome, "sandbox_blocked", return_value=False):
                self.assertEqual(chrome.find({}), "/pw/chrome-headless-shell")

    def test_the_sandbox_check_reads_the_kernel_switch(self):
        for text, blocked in (("1\n", True), ("0\n", False), (None, False)):
            path = os.path.join(self.dir, "userns")
            if text is None:
                path += "-missing"
            else:
                with open(path, "w") as f:
                    f.write(text)
            with unittest.mock.patch.object(chrome, "USERNS_SWITCH", path):
                self.assertIs(chrome.sandbox_blocked(), blocked, text)

    def test_headless_shell_of_playwright_in_its_new_layout(self):
        # newer Playwright versions put chrome-headless-shell in chromium_headless_shell-N/chrome-headless-shell-<os>/
        # (earlier — headless_shell); the old pattern did not find it
        home = os.path.join(self.dir, "home")
        exe = os.path.join(home, ".cache", "ms-playwright", "chromium_headless_shell-1248", "chrome-headless-shell-linux64",
                           "chrome-headless-shell")
        os.makedirs(os.path.dirname(exe))
        open(exe, "w").close()
        os.chmod(exe, 0o755)
        with unittest.mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home}):
            self.assertEqual(chrome.shell(), exe)

    def test_nothing_set_never_means_the_owners_chrome(self):
        # 10.10, owner's ban: never run the regular Google Chrome headless — neither tests nor snapshots
        with unittest.mock.patch.object(chrome, "system", return_value="/sys/chrome"), \
                unittest.mock.patch.object(chrome, "shell", return_value=None), \
                unittest.mock.patch.object(chrome, "testing_build", return_value=None):
            self.assertIsNone(chrome.configured({}))
            self.assertIsNone(chrome.find({}))
            self.assertIn("HUB_TEST_CHROME", chrome.INSTALL)

    def test_chrome_for_testing_is_the_second_choice(self):
        with unittest.mock.patch.object(chrome, "system", return_value="/sys/chrome"), \
                unittest.mock.patch.object(chrome, "shell", return_value=None), \
                unittest.mock.patch.object(chrome, "testing_build", return_value="/cft/chrome"):
            self.assertEqual(chrome.find({}), "/cft/chrome")
        with unittest.mock.patch.object(chrome, "shell", return_value="/shell"), \
                unittest.mock.patch.object(chrome, "testing_build", return_value="/cft/chrome"):
            self.assertEqual(chrome.find({}), "/shell")

    def test_settings_path_wins_over_the_system_chrome(self):
        with unittest.mock.patch.object(chrome, "system", return_value="/sys/chrome"):
            self.assertEqual(chrome.find({"testchrome": {"path": self.exe}}), self.exe)

    def test_env_wins_over_settings(self):
        other = os.path.join(self.dir, "other")
        open(other, "w").close()
        os.environ[chrome.ENV] = other
        self.assertEqual(chrome.configured({"testchrome": {"path": self.exe}}), other)

    def test_a_path_that_does_not_exist_is_ignored(self):
        with unittest.mock.patch.object(chrome, "shell", return_value=None), \
                unittest.mock.patch.object(chrome, "testing_build", return_value=None):
            self.assertIsNone(chrome.find({"testchrome": {"path": os.path.join(self.dir, "gone")}}))

    @unittest.skipIf(os.name == "nt", "chrome-headless-shell cache — macOS and Linux paths and permissions")
    def test_an_installed_headless_shell_wins_over_the_system_chrome(self):
        # 08.10: Chrome from Applications flashes in the dock on every launch, while headless-shell is not visible
        # there — we take it ourselves
        newest = os.path.join(self.dir, ".cache", "puppeteer", "chrome-headless-shell", "mac_arm-154.0.8037.57", "x",
                              "chrome-headless-shell")
        older = newest.replace("154.0.8037.57", "99.0.1.1")
        for exe in (older, newest):
            os.makedirs(os.path.dirname(exe))
            open(exe, "w").close()
            os.chmod(exe, 0o755)
        with unittest.mock.patch.dict(os.environ, {"HOME": self.dir}), \
                unittest.mock.patch.object(chrome, "system", return_value="/sys/chrome"):
            self.assertEqual(chrome.shell(), newest)   # 154 is newer than 99, though "99" is later alphabetically
            self.assertEqual(chrome.find({}), newest)
            self.assertEqual(chrome.find({"testchrome": {"path": self.exe}}), self.exe)   # an explicit path wins
            os.chmod(newest, 0o644)   # not executable — unusable
            self.assertEqual(chrome.shell(), older)
            # another vendor with a higher version number does not outrank a place higher in the list (playwright 1187 vs 154)
            other = os.path.join(self.dir, "Library", "Caches", "ms-playwright", "chromium_headless_shell-1187", "m",
                                 "headless_shell")
            os.makedirs(os.path.dirname(other))
            open(other, "w").close()
            os.chmod(other, 0o755)
            self.assertEqual(chrome.shell(), older)

    def test_no_chrome_at_all_is_none(self):
        with unittest.mock.patch.object(chrome, "shell", return_value=None), \
                unittest.mock.patch.object(chrome, "testing_build", return_value=None), \
                unittest.mock.patch.object(chrome.browsers, "find_chrome", side_effect=chrome.browsers.Problem("none")):
            self.assertIsNone(chrome.find({}))

    def test_a_refusal_on_macos_gives_the_install_command_for_this_hubs_settings_folder(self):
        # 13.11: a hub from git on a Mac with Google Chrome in /Applications and no headless shell: `hub shot` said "install
        # Google Chrome", though find() never takes the system Chrome there (the dock icon) — the session hunted a wrong path
        from modules.proof import cdp
        conf = os.path.join(self.dir, "home", "x", "second hub")
        with unittest.mock.patch.dict(os.environ, {"HUB_CONFIG_DIR": conf}), \
                unittest.mock.patch.object(chrome, "find", return_value=None):
            with self.assertRaises(cdp.Error) as got:
                cdp.Page()
            self.assertIn("npx @puppeteer/browsers install chrome-headless-shell@stable", str(got.exception))
            self.assertIn(f"--path '{conf}'", str(got.exception))
            self.assertNotIn("install Google Chrome", str(got.exception))
            with self.assertRaises(cdp.Error):   # an empty path is no Chrome either, the same refusal
                cdp.Page(chrome="")
            with unittest.mock.patch.object(chrome, "MAC", False):   # Linux, Windows: the system Chrome is taken as before
                with self.assertRaises(cdp.Error) as got:
                    cdp.Page()
                self.assertIn("install Google Chrome", str(got.exception))

    def test_headless_shell_is_looked_for_in_the_settings_folder_of_this_hub(self):
        # a second hub on the machine (HUB_CONFIG_DIR) keeps its own shell; the old fixed folder stays for those who installed there
        conf = os.path.join(self.dir, "home", "x", "second")
        mine = os.path.join(conf, "chrome-headless-shell", "mac_arm-154.0.1.1", "chrome-headless-shell-mac-arm64",
                            "chrome-headless-shell")
        old = os.path.join(self.dir, "home", ".config", "hub-core", "chrome-headless-shell", "mac_arm-99.0.1.1", "x",
                           "chrome-headless-shell")
        for exe in (mine, old):
            os.makedirs(os.path.dirname(exe))
            open(exe, "w").close()
            os.chmod(exe, 0o755)
        home = os.path.join(self.dir, "home")
        with unittest.mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home, "HUB_CONFIG_DIR": conf}):
            self.assertEqual(chrome.shell(), mine)
            os.remove(mine)
            self.assertEqual(chrome.shell(), old)

    def test_the_chrome_the_setup_downloaded_is_found_on_macos_too(self):
        # the machine's system Chrome never counts there, but the Chrome for Testing the hub itself downloaded
        # (<state>/runtime/chrome.json) is the hub's own build
        with unittest.mock.patch.object(chrome, "system", return_value="/sys/chrome"), \
                unittest.mock.patch.object(chrome, "shell", return_value=None), \
                unittest.mock.patch.object(chrome, "testing_build", return_value=None), \
                unittest.mock.patch.object(chrome.runtime, "chrome", return_value="/state/runtime/chrome/x/chrome"):
            self.assertEqual(chrome.find({}), "/state/runtime/chrome/x/chrome")

    def test_without_settings_given_it_reads_hub_json(self):
        cfg = os.path.join(self.dir, "hub.json")
        with open(cfg, "w") as f:
            json.dump({"testchrome": {"path": self.exe}}, f)
        with unittest.mock.patch.dict(os.environ, {"HUB_CONFIG": cfg}), \
                unittest.mock.patch.object(chrome, "shell", return_value=None), \
                unittest.mock.patch.object(chrome, "testing_build", return_value=None), \
                unittest.mock.patch.object(chrome, "system", return_value="/sys/chrome"):
            self.assertEqual(chrome.find(), self.exe)
            self.assertIsNone(chrome.find({}))

    def test_a_run_from_a_session_hands_the_path_to_the_tests(self):
        cfg = os.path.join(self.dir, "hub.json")
        with open(cfg, "w") as f:
            json.dump({"testchrome": {"path": self.exe}}, f)
        env = {**os.environ, "HUB_CONFIG": cfg}
        env.pop(chrome.ENV, None)
        out = subprocess.run([sys.executable, "-c", f"import tests, os; print(os.environ.get({chrome.ENV!r}, ''))"],
                             cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.strip(), self.exe, out.stderr)

    def test_update_hands_the_path_to_the_tests(self):
        seen = []

        def run(cmd, **kw):
            seen.append(kw["env"])
            return subprocess.CompletedProcess(cmd, 0, "OK\n", "")

        cfg = {"testchrome": {"path": self.exe}}
        with unittest.mock.patch.object(update.catalog, "load_config", return_value=cfg):
            update.run_tests("/repo", run=run)
        self.assertEqual(seen[0][chrome.ENV], self.exe)
        with unittest.mock.patch.object(update.catalog, "load_config", return_value={}):
            update.run_tests("/repo", run=run)
        self.assertNotIn(chrome.ENV, sorted(seen[1]))

    def test_module_card_shows_what_is_chosen(self):
        def card(cfg):
            return next(m for m in modinfo.modules(tempfile.mkdtemp(), cfg, {"sessions": []}) if m["id"] == "testchrome")

        with unittest.mock.patch.object(chrome, "system", return_value="/sys/chrome"), \
                unittest.mock.patch.object(chrome, "shell", return_value=None), \
                unittest.mock.patch.object(chrome, "testing_build", return_value=None):
            self.assertEqual((card({})["state"], card({})["detail"]),
                             ("off", "not found: browser tests are skipped"))
            on = card({"testchrome": {"path": self.exe}})
        self.assertEqual((on["state"], on["detail"]), ("on", self.exe))
        self.assertIn("set up chrome for tests", on["how"])


class NoAudio(unittest.TestCase):
    def test_the_chrome_of_the_tests_runs_without_real_audio_devices_and_the_hubs_own_with_them(self):
        # 04.11: after a full run the owner's default output (a USB DAC) went silent until its cable was replugged. The voice
        # tests open a real AudioContext and getUserMedia in the test Chrome, which is then stopped by TerminateProcess.
        # Every headless command line of the tests and dev tools (a list with "--headless…") carries *NO_AUDIO; the hub's
        # own Chrome (mermaid.args: hub shot/video, the voice reply) plays and shows the real devices and gets none of it
        import ast
        from modules.telegram import mermaid
        self.assertEqual(chrome.NO_AUDIO, ("--mute-audio", "--disable-audio-output",
                                           "--use-fake-device-for-media-stream"))
        found, bare = set(), []
        for top in ("tests", "tools"):
            for d, dirs, files in os.walk(os.path.join(ROOT, top)):
                dirs[:] = [x for x in dirs if x not in ("node_modules", "__pycache__")]
                for f in files:
                    if not f.endswith(".py"):
                        continue
                    path = os.path.join(d, f)
                    with open(path, encoding="utf-8") as fh:
                        tree = ast.parse(fh.read(), path)
                    rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
                    for node in ast.walk(tree):
                        if isinstance(node, ast.List) and any(
                                isinstance(e, ast.Constant) and isinstance(e.value, str) and e.value.startswith("--headless")
                                for e in node.elts):
                            found.add(rel)
                            if not any(isinstance(e, ast.Starred) and getattr(e.value, "attr", getattr(e.value, "id", None))
                                       == "NO_AUDIO" for e in node.elts):
                                bare.append(f"{rel}:{node.lineno}")
        self.assertLessEqual({"tests/test_web_e2e.py", "tests/test_files_e2e.py", "tools/dev/readme_demo.py"}, found)
        self.assertEqual(bare, [])
        self.assertEqual(set(mermaid.args("chrome", [])) & set(chrome.NO_AUDIO), set())

    def test_audio_contexts_that_play_nothing_have_no_output_device(self):
        # 05.11: the owner's USB DAC went silent again on a day of page tests. On top of
        # NO_AUDIO, an audio context a test makes for a stand-in microphone gets no output device at all:
        # sinkId {type: 'none'} (Chrome 110+) — the graph still runs, nothing is played anywhere. So does the page's
        # own one (the dictation level, recorder.js): it only reads the microphone, yet opened a stream on the default
        # output at every dictation. None of them plays sound; one that must would be named here with why. The one bare
        # context allowed: the dictation's fallback for a browser that refuses sinkId none (the line says so)
        made = re.compile(r"\bnew\s*(?:\([^()]*AudioContext[^()]*\)|(?:window\.)?(?:webkit)?AudioContext)\s*\(")
        silent = re.compile(r"\s*\{\s*sinkId\s*:\s*\{\s*type\s*:\s*(['\"])none\1")
        import itertools
        seen, bare = set(), []
        walk = itertools.chain(os.walk(os.path.join(ROOT, "tests")), os.walk(os.path.join(ROOT, "modules", "web", "static")))
        for d, dirs, files in walk:
            dirs[:] = [x for x in dirs if x not in ("node_modules", "__pycache__", "vendor")]
            for f in files:
                if not f.endswith((".py", ".mjs", ".js", ".html")):
                    continue
                path = os.path.join(d, f)
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
                rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
                for m in made.finditer(text):
                    seen.add(rel)
                    line = text[text.rfind("\n", 0, m.start()) + 1:text.find("\n", m.end())]
                    if not silent.match(text, m.end()):
                        bare.append(f"{rel}:{text.count(chr(10), 0, m.start()) + 1}"
                                    + (" fallback" if "sinkId refused" in line else ""))
        # the stand-in microphones of the voice tests, the dictation level
        self.assertLessEqual({"tests/test_web_e2e.py", "modules/web/static/js/recorder.js"}, seen)
        self.assertEqual([b.split(":")[0] for b in bare if b.endswith(" fallback")], ["modules/web/static/js/recorder.js"])
        self.assertEqual([b for b in bare if not b.endswith(" fallback")], [])


class GentleStop(unittest.TestCase):
    """05.11: the test Chrome was only killed (TerminateProcess of its tree), its audio and GPU
    processes never ended their streams themselves. Now it is first asked to close over CDP (Browser.close), as a user
    closes it, and killed only if it is still alive after that. Nothing real is started: a stand-in process and CDP."""

    class Proc:
        """A stand-in Popen: exits after the close request if `closes`."""
        pid = 424242

        def __init__(self, log, closes=True):
            self.log, self.closes, self.returncode = log, closes, None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            if self.returncode is None:
                if not (self.closes and "close" in self.log):
                    raise subprocess.TimeoutExpired("chrome", timeout)
                self.returncode = 0
            return self.returncode

    def setUp(self):
        self.log, self.answers = [], True   # answers: Chrome took the close request
        for p in (unittest.mock.patch.object(platforms.current, "kill_detached", side_effect=self.kill),
                  unittest.mock.patch.object(chrome, "close", side_effect=self.close)):
            p.start()
            self.addCleanup(p.stop)

    def kill(self, proc):
        self.log.append("kill")
        if proc.returncode is None:
            proc.returncode = 1

    def close(self, profile):
        self.log.append("close")
        return self.answers

    def test_a_chrome_that_closes_exits_by_itself_and_its_leftovers_are_swept(self):
        proc = self.Proc(self.log)
        chrome.stop(proc, profile="/p")
        # exited by itself (0, not the kill's 1); the sweep after it only reaches what it left in its group
        self.assertEqual((self.log, proc.returncode), (["close", "kill"], 0))

    def test_a_chrome_still_alive_after_the_request_is_killed(self):
        proc = self.Proc(self.log, closes=False)
        chrome.stop(proc, profile="/p")
        self.assertEqual((self.log, proc.returncode), (["close", "kill"], 1))

    def test_no_answer_over_cdp_means_the_kill(self):
        self.answers = False
        proc = self.Proc(self.log)
        chrome.stop(proc, profile="/p")
        self.assertEqual(self.log, ["close", "kill"])

    def test_without_a_profile_it_is_killed_as_before(self):
        chrome.stop(self.Proc(self.log))
        self.assertEqual(self.log, ["kill"])

    def test_the_page_tests_pass_their_profile(self):
        import ast
        calls = []
        for f in ("test_web_e2e.py", "test_files_e2e.py"):
            with open(os.path.join(ROOT, "tests", f), encoding="utf-8") as fh:
                calls += [(f, n.lineno, any(k.arg == "profile" for k in n.keywords)) for n in ast.walk(ast.parse(fh.read()))
                          if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "stop"
                          and isinstance(n.func.value, ast.Name) and n.func.value.id == "testchrome"]
        self.assertEqual({f for f, _, _ in calls}, {"test_web_e2e.py", "test_files_e2e.py"})
        self.assertEqual([c for c in calls if not c[2]], [])


@unittest.skipIf(os.name == "nt", "Windows: the tree is found only from a live root, a collected one has none")
class GroupSweep(unittest.TestCase):
    def test_what_a_closed_chrome_left_in_its_group_does_not_outlive_the_stop(self):
        # review of PR #441: the root exiting after Browser.close returned at once, a renderer or helper still alive in
        # its group was left an orphan (09.10: orphan browsers of a full run took 20 GB). A real group, a stand-in close:
        # the root starts a child and exits
        import time
        from tests.fakes import procs
        tmp = tempfile.mkdtemp(prefix="hubchrome-")
        self.addCleanup(platforms.current.rmtree, tmp)
        pidfile = os.path.join(tmp, "child")
        code = ("import subprocess, sys; p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                f"open({pidfile!r} + '.tmp', 'w').write(str(p.pid)); __import__('os').replace({pidfile!r} + '.tmp', {pidfile!r})")
        proc = platforms.current.popen_detached([sys.executable, "-c", code])
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        deadline = time.monotonic() + 30
        while not os.path.exists(pidfile) and time.monotonic() < deadline:
            time.sleep(0.05)
        with open(pidfile, encoding="utf-8") as f:
            child = int(f.read())
        self.addCleanup(procs.kill, child)
        with unittest.mock.patch.object(chrome, "close", return_value=True):
            chrome.stop(proc, profile="/p")
        self.assertIsNotNone(proc.returncode)
        while procs.alive(child) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(procs.alive(child))


class CloseRequest(unittest.TestCase):
    """chrome.close: Browser.close to the endpoint Chrome wrote into its profile (DevToolsActivePort), a stand-in socket."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.profile = tmp.name
        p = unittest.mock.patch("modules.proof.cdp.Socket")
        self.socket = p.start()
        self.addCleanup(p.stop)

    def test_the_request_goes_to_the_endpoint_of_the_profile(self):
        with open(os.path.join(self.profile, "DevToolsActivePort"), "w", encoding="utf-8") as f:
            f.write("9555\n/devtools/browser/abc\n")
        self.assertTrue(chrome.close(self.profile))
        self.assertEqual(self.socket.call_args.args[0], "ws://127.0.0.1:9555/devtools/browser/abc")
        ws = self.socket.return_value
        self.assertEqual(json.loads(ws.send.call_args.args[0])["method"], "Browser.close")
        ws.close.assert_called_once()

    def test_no_endpoint_or_no_answer_is_false(self):
        self.assertFalse(chrome.close(self.profile))   # Chrome wrote no DevToolsActivePort
        self.socket.assert_not_called()
        with open(os.path.join(self.profile, "DevToolsActivePort"), "w", encoding="utf-8") as f:
            f.write("9555\n/devtools/browser/abc\n")
        self.socket.side_effect = ConnectionRefusedError
        self.assertFalse(chrome.close(self.profile))


if __name__ == "__main__":
    unittest.main()
