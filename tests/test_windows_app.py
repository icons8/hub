"""The Windows app (installer spec S6): Setup.exe (tools/package/windows/agents-hub.iss) copies the app folder of the
factory and runs its app.py — which links again what the build took out, lays the seed out as the code and runs the
code's deploy/windows/install.ps1 -App — and the Start menu opens the hub through it. What can be checked without
Windows: the files app.py writes, the commands it runs (substituted), the hub's environment setup.py --app gives the
launcher, and what the Inno script promises (no administrator, no folder question, the data kept on uninstall)."""
import tests  # noqa: F401 — hide the machine's settings
import importlib.util
import json
import ntpath
import os
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
import urllib.error
from unittest import mock

import platforms
from tests import winsafe

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIN = os.path.join(REPO, "tools", "package", "windows")
SETUP = os.path.join(REPO, "deploy", "windows", "setup.py")
PS1 = os.path.join(REPO, "deploy", "windows", "install.ps1")


def load(name):
    spec = importlib.util.spec_from_file_location(f"hub_windows_{name}", os.path.join(WIN, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Ran:
    """subprocess.run substitute: what was run, with a given exit code."""

    def __init__(self, code=0):
        self.calls, self.code = [], code

    def __call__(self, cmd, **kw):
        self.calls.append((list(cmd), kw))
        return subprocess.CompletedProcess(cmd, self.code, "", "")


class Links(unittest.TestCase):
    def setUp(self):
        self.app_dir = tempfile.mkdtemp(prefix="win-app-")
        self.addCleanup(shutil.rmtree, self.app_dir, True)
        self.app = load("app")
        os.makedirs(os.path.join(self.app_dir, "runtime", "git", "ucrt64", "bin"))
        os.makedirs(os.path.join(self.app_dir, "runtime", "git", "ucrt64", "libexec", "git-core"))
        with open(os.path.join(self.app_dir, "runtime", "git", "ucrt64", "bin", "git.exe"), "wb") as f:
            f.write(b"MZgit")
        with open(os.path.join(self.app_dir, "links.json"), "w") as f:
            json.dump({"hard": {"runtime/git/ucrt64/libexec/git-core/git.exe": "runtime/git/ucrt64/bin/git.exe"},
                       "msys": {"runtime/git/dev/fd": "/proc/self/fd", "runtime/git/dev/stdin": "/proc/self/fd/0",
                                "runtime/git/dev/shm": None}}, f)
        self.marked = []

    def test_the_links_of_the_build_come_back(self):
        made = self.app.restore_links(self.app_dir, system=self.marked.append)
        git = os.path.join(self.app_dir, "runtime", "git")
        a, b = (os.path.join(git, "ucrt64", d, "git.exe") for d in ("bin", os.path.join("libexec", "git-core")))
        self.assertTrue(os.path.samefile(a, b))                      # a hard link, not a second copy
        with open(os.path.join(git, "dev", "fd"), "rb") as f:
            # msys's own link format: the cookie, the target in UTF-16 with a BOM, a terminating NUL — and the system
            # attribute (the test takes it), which msys needs to read a file as a link
            self.assertEqual(f.read(), b"!<symlink>\xff\xfe" + "/proc/self/fd".encode("utf-16-le") + b"\x00\x00")
        # the attribute is set before the file takes its name: a link without it is never left in place
        self.assertEqual(sorted(self.marked), sorted(os.path.join(git, "dev", n + ".part") for n in ("fd", "stdin")))
        self.assertTrue(os.path.isdir(os.path.join(git, "dev", "shm")))
        self.assertEqual(made, 3)
        self.assertEqual(self.app.restore_links(self.app_dir, system=self.marked.append), 0)   # a rerun makes nothing

    def test_a_disk_without_hard_links_gets_a_copy(self):
        with mock.patch("os.link", side_effect=OSError("FAT32")):
            self.app.restore_links(self.app_dir, system=self.marked.append)
        with open(os.path.join(self.app_dir, "runtime", "git", "ucrt64", "libexec", "git-core", "git.exe"), "rb") as f:
            self.assertEqual(f.read(), b"MZgit")


class Commands(unittest.TestCase):
    def setUp(self):
        self.app_dir = tempfile.mkdtemp(prefix="win-app-")
        self.state = tempfile.mkdtemp(prefix="win-state-")
        for d in (self.app_dir, self.state):
            self.addCleanup(shutil.rmtree, d, True)
        self.app = load("app")
        self.opened = []
        self.waited = []

    def wait(self, answer):
        """The window that waits for the hub: what it was told, and whether the hub came up."""
        def wait(up, log, failed=False):
            self.waited.append((log, failed))
            return answer
        return wait

    def code(self):
        os.makedirs(os.path.join(self.state, "code", "bin"))
        open(os.path.join(self.state, "code", "bin", "hub"), "w").close()

    def test_the_start_menu_opens_a_live_hub_at_once(self):
        run = Ran()
        self.app.open_hub(self.app_dir, self.state, up=lambda: True, browser=self.opened.append, run=run)
        self.assertEqual(self.opened, ["http://127.0.0.1:8787/"])
        self.assertEqual(run.calls, [])

    def test_a_hub_that_is_off_is_turned_on_and_the_browser_opens_once_it_answers(self):
        self.code()
        run = Ran()
        self.app.open_hub(self.app_dir, self.state, up=lambda: False, browser=self.opened.append, run=run,
                          wait=self.wait(True))
        (cmd, kw), = run.calls
        self.assertEqual(cmd[1:], [os.path.join(self.state, "code", "bin", "hub"), "on"])
        self.assertEqual(cmd[0], os.path.join(self.app_dir, "runtime", "python", "python.exe"))
        self.assertNotEqual(os.path.realpath(kw["cwd"]), os.path.realpath(self.state))
        self.assertTrue(kw["env"]["PATH"].startswith(os.path.join(self.app_dir, "runtime", "git", "cmd")))
        self.assertEqual(kw["env"]["HUB_STATE_DIR"], self.state)   # not a hub from git the person's environment names
        # the browser only once the hub answers: Chromium does not let a page from a file ask 127.0.0.1 (winstand),
        # so the waiting is a window of the shell, not a page
        self.assertEqual(self.waited, [(os.path.join(self.state, "logs", "hub-up.log"), False)])
        self.assertEqual(self.opened, ["http://127.0.0.1:8787/"])

    def test_a_hub_that_never_answers_opens_no_browser(self):
        self.code()
        self.assertEqual(self.app.open_hub(self.app_dir, self.state, up=lambda: False, browser=self.opened.append,
                                           run=Ran(), wait=self.wait(False)), 1)
        self.assertEqual(self.opened, [])

    def test_no_code_yet_means_the_install_runs_first(self):
        run = Ran()
        with mock.patch.object(self.app, "install", return_value=0) as install:
            self.app.open_hub(self.app_dir, self.state, up=lambda: False, browser=self.opened.append, run=run,
                              wait=self.wait(True))
        install.assert_called_once()
        self.assertEqual(self.opened, ["http://127.0.0.1:8787/"])

    def test_a_failed_install_shows_its_log(self):
        with mock.patch.object(self.app, "install", return_value=4):
            self.assertEqual(self.app.open_hub(self.app_dir, self.state, up=lambda: False, browser=self.opened.append,
                                               run=Ran(), wait=self.wait(False)), 4)
        self.assertEqual(self.waited, [(os.path.join(self.state, "logs", "install.log"), True)])

    def test_without_a_window_it_still_waits_for_the_hub(self):
        answers = iter([False, True])
        with mock.patch.dict("sys.modules", {"tkinter": None}), mock.patch("time.sleep"):
            self.assertTrue(self.app.window(lambda: next(answers), "x.log"))

    def test_the_window_speaks_english_whatever_windows_speaks(self):
        # it comes before the hub knows its owner's language: one English text (the page then takes the catalog)
        for key, text in self.app.TEXT.items():
            self.assertIsInstance(text, str, key)
            self.assertTrue(all(c.isascii() for c in text if c.isalpha()), key)   # English letters only

    def test_uninstall_is_the_hubs_own_and_keeps_the_data_unless_asked(self):
        self.code()
        run = Ran()
        self.app.uninstall(app=self.app_dir, state=self.state, run=run)
        self.app.uninstall(purge=True, app=self.app_dir, state=self.state, run=run)
        self.assertEqual([c[0][2:] for c in run.calls], [["uninstall", "--yes"], ["uninstall", "--yes", "--purge"]])

    def test_a_command_that_cannot_run_is_a_failure_not_a_crash(self):
        # the Start menu runs under pythonw: an exception would end it with nothing on the screen
        self.code()
        def broken(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 300)
        self.assertEqual(self.app.hub(self.state, "on", run=broken, app=self.app_dir), 1)

    def test_uninstall_without_code_still_takes_the_task_off(self):
        run = Ran()
        self.app.uninstall(app=self.app_dir, state=self.state, run=run)
        self.assertEqual(run.calls[0][0], ["schtasks", "/Delete", "/TN", "Agents Hub", "/F"])

    def test_install_lays_the_code_out_then_runs_the_codes_install_script(self):
        seed = os.path.join(self.app_dir, "code-seed")
        subprocess.run(["git", "init", "-q", seed], check=True)
        os.makedirs(os.path.join(seed, "deploy", "windows"))
        open(os.path.join(seed, "deploy", "windows", "install.ps1"), "w").close()
        subprocess.run(["git", "-C", seed, "add", "-A"], check=True)
        subprocess.run(["git", "-C", seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "v"], check=True)
        subprocess.run(["git", "-C", seed, "remote", "add", "origin", "https://example.test/core.git"], check=True)
        with open(os.path.join(self.app_dir, "PACKAGED"), "w") as f:
            json.dump({"format": "app", "version": "1.2.3", "runtime": 1}, f)
        shutil.copy(os.path.join(REPO, "tools", "package", "launch.py"), self.app_dir)
        run = Ran()
        self.assertEqual(self.app.install(self.app_dir, self.state, run=run), 0)
        code = os.path.join(self.state, "code")
        self.assertTrue(os.path.isfile(os.path.join(code, "PACKAGED")))
        (end, _), (cmd, kw) = run.calls
        # Setup.exe let no process of the app live: an instance still "Running" is an orphaned cmd.exe (app spec, A0)
        self.assertEqual(end, ["schtasks", "/End", "/TN", "Agents Hub"])
        self.assertEqual(cmd[:2], ["powershell.exe", "-NoProfile"])
        self.assertEqual(cmd[cmd.index("-File") + 1:], [os.path.join(code, "deploy", "windows", "install.ps1"),
                                                         "-App", self.app_dir, "-Yes"])
        # its output into the UTF-8 log as UTF-8: a dash and a non-Latin user name, not "?" (install.ps1, in its console)
        self.assertEqual(kw["env"].get("HUB_INSTALL_OUTPUT"), "utf-8")
        with open(os.path.join(self.state, "logs", "install.log"), encoding="utf-8") as f:
            self.assertIn("install.ps1: exit code 0", f.read())
        self.assertEqual(self.app.install(self.app_dir, self.state, run=Ran(1)), 4)   # Setup.exe shows the failure

    def layer(self, relative):
        """A state folder of the layer era: <state>/code is a link to <state>/layers/L; -> (the pointer, the layer)."""
        layer = os.path.join(self.state, "layers", "L")
        os.makedirs(os.path.join(layer, "deploy", "windows"))
        os.makedirs(os.path.join(layer, "bin"))
        open(os.path.join(layer, "deploy", "windows", "install.ps1"), "w").close()
        open(os.path.join(layer, "bin", "hub"), "w").close()
        code = os.path.join(self.state, "code")
        winsafe.symlink(self, os.path.relpath(layer, self.state) if relative else layer, code)   # a junction on Windows: both are read with readlink
        return code, layer

    def test_what_setup_starts_goes_by_the_layers_own_path_not_the_junction(self):
        # Windows refuses a junction the user made in every process of Setup.exe's tree ("untrusted mount point": cmd,
        # PowerShell and Python alike, the Windows stand 07.11): install.ps1 is run by the layer's path and is told the
        # pointer to write down; `hub stop` and `hub uninstall` of the setup/uninstall steps go by it too
        for relative in (False, True):
            with self.subTest(relative=relative):
                self.state = tempfile.mkdtemp(prefix="win-state-")
                self.addCleanup(platforms.current.rmtree, self.state)
                code, layer = self.layer(relative)
                seed = types.ModuleType("launch")
                seed.ensure_code = lambda *a: code
                seed.git_hub = lambda *a, **k: None   # no hub from git here
                run = Ran()
                with mock.patch.dict(sys.modules, {"launch": seed}), mock.patch.object(self.app, "restore_links",
                                                                                      return_value=0):
                    self.assertEqual(self.app.install(self.app_dir, self.state, run=run), 0)
                (end, _), (cmd, kw) = run.calls   # the orphan's schtasks /End first, then install.ps1
                self.assertEqual(end[0], "schtasks")
                self.assertEqual(cmd[cmd.index("-File") + 1], os.path.join(layer, "deploy", "windows", "install.ps1"))
                self.assertEqual(cmd[cmd.index("-Code") + 1], code)
                self.assertEqual(kw["cwd"], layer)
                run = Ran()
                self.app.hub(self.state, "stop", run=run, app=self.app_dir)
                self.assertEqual(run.calls[0][0][1], os.path.join(layer, "bin", "hub"))

    def test_a_folder_that_is_not_a_pointer_is_its_own_path(self):
        os.makedirs(os.path.join(self.state, "code", ".git"))
        code = os.path.join(self.state, "code")
        self.assertEqual(self.app.layer_path(code), code)
        self.assertEqual(self.app.layer_path(os.path.join(self.state, "none")), os.path.join(self.state, "none"))

    def test_a_junction_target_as_windows_keeps_it_loses_its_prefix(self):
        with mock.patch.object(self.app.os, "readlink", return_value="\\\\?\\C:\\State\\layers\\L"):
            self.assertEqual(self.app.layer_path("C:\\State\\code"), "C:\\State\\layers\\L")

    def test_any_failed_step_is_written_to_the_log_setup_names(self):
        # Setup.exe and the Start menu send the person to install.log: a step that broke must be there (review)
        with mock.patch.object(self.app, "restore_links", side_effect=OSError("attribute not set")):
            self.assertEqual(self.app.install(self.app_dir, self.state, run=Ran()), 3)
        with open(os.path.join(self.state, "logs", "install.log"), encoding="utf-8") as f:
            self.assertIn("links of the build: failed: OSError('attribute not set')", f.read())

    def test_a_page_that_answers_with_an_error_is_up(self):
        def answer(*a, **kw):
            raise urllib.error.HTTPError("http://127.0.0.1:8787/", 403, "forbidden", {}, None)
        with mock.patch("urllib.request.OpenerDirector.open", answer):
            self.assertTrue(self.app.page_up())
        with mock.patch("urllib.request.OpenerDirector.open", side_effect=ConnectionRefusedError()):
            self.assertFalse(self.app.page_up())

    def test_the_state_folder_is_install_ps1s_whatever_hub_state_dir_says(self):
        with mock.patch.dict(os.environ, {"HUB_STATE_DIR": "/elsewhere", "LOCALAPPDATA": self.app_dir}):
            self.assertEqual(self.app.state_dir(), os.path.join(self.app_dir, "hub-core"))

    def test_a_cut_off_launch_still_lets_hub_up_start(self):
        self.assertEqual(self.app.launch(self.app_dir, self.state), 1)   # no seed: written down, not raised
        with open(os.path.join(self.state, "logs", "launch.log"), encoding="utf-8") as f:
            self.assertIn("start:", f.read())


class WatchdogOnWindows(unittest.TestCase):
    """The update's watchdog and the shell's launcher ask tasklist whether a process lives. Its numbers carry the
    console code page's no-break space (0xFF in cp866 and cp437: "10 560 K"), and the hub's parts run with
    PYTHONUTF8=1: reading it as text killed the watchdog right after an install (winstand) — the rollback came only
    from the watchdog the update module starts again."""

    LISTING = b'"python.exe","4242","Console","1","10\xff560 K"\r\n'

    def fake_run(self, cmd, **kw):
        out = self.LISTING
        if kw.get("text") or kw.get("encoding"):
            out = out.decode("utf-8")      # what subprocess does in UTF-8 mode: UnicodeDecodeError
        return subprocess.CompletedProcess(cmd, 0, out, b"" if not kw.get("text") else "")

    def check(self, mod):
        with mock.patch.object(mod, "WINDOWS", True), mock.patch.object(mod.subprocess, "run", self.fake_run):   # alive() follows WINDOWS, not os.name
            self.assertTrue(mod.alive(4242))
            self.assertFalse(mod.alive(4243))

    def test_the_watchdog(self):
        spec = importlib.util.spec_from_file_location("hub_trial_probe", os.path.join(REPO, "modules", "update",
                                                                                         "trial.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.check(mod)

    def test_the_launcher(self):
        spec = importlib.util.spec_from_file_location("hub_launch_probe", os.path.join(REPO, "tools", "package",
                                                                                          "launch.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.check(mod)


class AppSetup(unittest.TestCase):
    """deploy/windows/setup.py --app: the hub of the app."""

    def setUp(self):
        self.app_dir = tempfile.mkdtemp(prefix="win-app-")
        self.state = tempfile.mkdtemp(prefix="win-state-")
        for d in (self.app_dir, self.state):
            self.addCleanup(shutil.rmtree, d, True)
        spec = importlib.util.spec_from_file_location("hub_build_paths", os.path.join(REPO, "tools", "package",
                                                                                         "build.py"))
        build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build)
        self.rel = {**build.PATHS["windows"], "launch": "launch.py", "app": "app.py",
                    "mcp": "runtime/mcp/node_modules/chrome-devtools-mcp/build/src/bin/chrome-devtools-mcp.js"}
        with open(os.path.join(self.app_dir, "paths.json"), "w") as f:      # the factory's map of the programs
            json.dump(self.rel, f)
        self.s = runpy.run_path(SETUP)

    def at(self, key):
        return ntpath.join(self.app_dir, *self.rel[key].split("/"))

    def test_settings_name_the_apps_browser_mcp_and_ffmpeg(self):
        cfg = self.s["default_config"](self.state, self.app_dir)
        self.assertEqual(cfg["browser"]["mcp"], [self.at("node"), self.at("mcp")])
        self.assertTrue(cfg["browser"]["mcp"][1].endswith(ntpath.join("chrome-devtools-mcp", "build", "src", "bin",
                                                                      "chrome-devtools-mcp.js")))
        self.assertEqual(cfg["voice"]["ffmpeg"], self.at("ffmpeg"))
        self.assertNotIn("browser", self.s["default_config"](self.state))   # a hub from the script stays as it was

    def test_a_reinstall_moves_the_apps_own_settings_and_keeps_the_owners(self):
        cfg_path = os.path.join(self.state, "hub.json")
        old = ntpath.join(self.app_dir, "runtime", "mcp", "old.js")
        own = os.path.join(self.state, "ffmpeg.exe")                      # the owner's own ffmpeg, a real program
        open(own, "w").close()
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"title": "mine", "browser": {"mcp": [self.at("node"), old], "chrome": own},
                       "voice": {"ffmpeg": own}}, f)
        self.assertTrue(self.s["refresh_config"](cfg_path, REPO, self.app_dir))
        with open(cfg_path, encoding="utf-8") as f:
            got = json.load(f)
        self.assertEqual(got["browser"], {"mcp": [self.at("node"), self.at("mcp")], "chrome": own})
        self.assertEqual(got["voice"], {"ffmpeg": own})
        self.assertEqual(got["title"], "mine")
        self.assertFalse(self.s["refresh_config"](cfg_path, REPO, self.app_dir))   # nothing more to change

    def test_the_owners_mcp_on_the_apps_node_stays(self):
        cfg_path = os.path.join(self.state, "hub.json")
        script = os.path.join(self.state, "my-mcp.js")
        open(script, "w").close()
        mine = [self.at("node"), script, "--flag"]
        os.makedirs(os.path.dirname(self.at("node")), exist_ok=True)   # Windows: runtime\node; POSIX: the temp folder
        open(self.at("node"), "w").close()   # the app's node is there (on POSIX a file with "\\" in its name)
        self.addCleanup(os.unlink, self.at("node"))
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"browser": {"mcp": mine}}, f)
        self.s["refresh_config"](cfg_path, REPO, self.app_dir)
        with open(cfg_path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["browser"]["mcp"], mine)

    def test_a_program_by_name_is_the_owners(self):
        cfg_path = os.path.join(self.state, "hub.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"browser": {"mcp": ["npx", "-y", "chrome-devtools-mcp@latest"]}, "voice": {"ffmpeg": "ffmpeg"}}, f)
        self.assertFalse(self.s["refresh_config"](cfg_path, REPO, self.app_dir))

    def test_paths_into_a_removed_app_folder_follow_the_new_one(self):
        cfg_path = os.path.join(self.state, "hub.json")
        gone = os.path.join(self.state, "gone", "node.exe")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"browser": {"mcp": [gone, gone + ".js"]}, "voice": {"ffmpeg": gone}}, f)
        self.assertTrue(self.s["refresh_config"](cfg_path, REPO, self.app_dir))
        with open(cfg_path, encoding="utf-8") as f:
            got = json.load(f)
        self.assertEqual(got["voice"]["ffmpeg"], self.at("ffmpeg"))

    def test_an_unreadable_hub_json_does_not_stop_the_install(self):
        cfg_path = os.path.join(self.state, "hub.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write('{"title": "mine",}')
        self.assertFalse(self.s["refresh_config"](cfg_path, REPO, self.app_dir))
        with open(cfg_path, encoding="utf-8") as f:
            self.assertEqual(f.read(), '{"title": "mine",}')

    def test_the_launcher_gives_the_hub_the_apps_git_and_bash_and_runs_the_shell_first(self):
        code = ntpath.join(self.state, "code")
        hub_cmd, launcher = self.s["scripts"](code, self.state, self.at("python"), encoding="utf-8", app=self.app_dir)
        with open(launcher, encoding="utf-8", newline="") as f:
            text = f.read()
        lines = text.split("\r\n")
        path = next(ln for ln in lines if ln.startswith('set "PATH='))
        # the app's git first (bash for Claude Code, long paths), the person's own programs before the app's others
        self.assertTrue(path.startswith(f'set "PATH={ntpath.dirname(self.at("git"))};%PATH%;'), path)
        self.assertLess(path.index("%PATH%"), path.index(ntpath.dirname(self.at("python"))))
        self.assertIn(f'set "CLAUDE_CODE_GIT_BASH_PATH={self.at("bash")}"', lines)
        self.assertTrue(self.at("bash").endswith(ntpath.join("git", "bin", "bash.exe")))
        self.assertIn(f'set "HUB_GH={self.at("gh")}"', lines)
        # a HUB_STATE_DIR in the person's environment (a hub from git) must not take the app's hub elsewhere
        self.assertIn(f'set "HUB_STATE_DIR={self.state}"', lines)
        shell = lines.index(f'"{self.at("python")}" "{self.at("app")}" launch')
        self.assertLess(lines.index(path), shell)
        self.assertLess(shell, next(i for i, ln in enumerate(lines) if "hub-up" in ln and "--scheduled" in ln))
        self.assertNotIn(">", text)   # tests/test_windows_install: cmd's redirection may keep hub-up from starting
        with open(hub_cmd, encoding="utf-8", newline="") as f:
            hub = f.read()
        self.assertIn("setlocal", hub)                       # the terminal's PATH is not changed by the command
        self.assertIn(path, hub)
        self.assertTrue(hub.rstrip().endswith(f'"{ntpath.join(code, "bin", "hub")}" %*'))

    def test_a_stopped_hub_runs_nothing_of_the_app(self):
        """`hub off` leaves the stop mark; the task still runs every 5 minutes. Before the app's launch.py ran each
        time — it could start the update watchdog again for a hub that is off on purpose, and hold the app's Python
        while Setup.exe replaces it (review)."""
        from core import restart
        _, launcher = self.s["scripts"](ntpath.join(self.state, "code"), self.state, self.at("python"),
                                        encoding="utf-8", app=self.app_dir)
        with open(launcher, encoding="utf-8", newline="") as f:
            lines = f.read().split("\r\n")
        self.assertEqual(lines[1], f'if exist "{ntpath.join(self.state, restart.HELD)}" exit /b 0')

    def test_run_enters_the_code_from_its_own_folder_while_the_files_name_the_pointer(self):
        # under Setup.exe the pointer (a junction) cannot be entered: the code is imported from --run, and hub.cmd, the
        # launcher and hub-home still name the pointer (a new layer is followed)
        pointer = os.path.join(self.state, "code")   # nothing there: entering it would fail, as the junction does
        cfg = os.path.join(self.state, "hub.json")
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({"voice": {"ffmpeg": ntpath.join(self.app_dir, "runtime", "old", "ffmpeg.exe")}}, f)
        r = subprocess.run([sys.executable, "-I", SETUP, pointer, self.state, cfg, "--run", REPO, "--app", self.app_dir],
                           capture_output=True, text=True, encoding="utf-8", stdin=subprocess.DEVNULL, timeout=120,
                           env={**os.environ, "HUB_STATE_DIR": self.state})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("the paths of the app updated", r.stderr)   # refresh_config ran: `core.catalog` came from --run
        with open(os.path.join(self.state, "bin", "hub.cmd"), encoding="utf-8") as f:
            self.assertIn(ntpath.join(pointer, "bin", "hub"), f.read())

    def test_install_ps1_takes_the_apps_python_and_leaves_claude_code_to_the_setup_page(self):
        with open(PS1, encoding="utf-8-sig") as f:
            ps1 = f.read()
        self.assertIn('param([switch]$Yes, [string]$App, [string]$Code, [string]$Instance, [switch]$Shell)', ps1)
        # Setup.exe's tree cannot enter the junction <state>\\code: what install.ps1 runs goes by its own folder ($Run), what
        # outlives the install names the pointer ($Repo = -Code)
        self.assertIn('$Repo = if ($Code) { $Code } else { $Run }', ps1)
        self.assertIn('& $Python "$Run\\deploy\\windows\\setup.py" $Repo $State $Config --run $Run @appArgs', ps1)
        self.assertNotIn('"$Repo\\bin\\hub"', ps1.replace('`"$Repo\\bin\\hub`"', ''))
        self.assertIn('-WorkingDirectory $Repo', ps1)
        self.assertIn('Join-Path $App "runtime\\python\\python.exe"', ps1)
        self.assertIn("if (-not $App -and -not $Claude", ps1)
        self.assertIn('@("--app", $App)', ps1)


class InnoScript(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(WIN, "agents-hub.iss"), encoding="utf-8-sig") as f:
            self.iss = f.read()

    def setting(self, name):
        m = re.search(rf"(?m)^{name}=(.*)$", self.iss)
        return m and m.group(1).strip()

    def test_for_the_user_without_admin_and_without_a_folder_question(self):
        self.assertEqual(self.setting("PrivilegesRequired"), "lowest")
        self.assertIsNone(self.setting("PrivilegesRequiredOverridesAllowed"))   # no "for all users?" dialog
        self.assertEqual(self.setting("DisableDirPage"), "yes")
        self.assertEqual(self.setting("DisableProgramGroupPage"), "yes")
        self.assertEqual(self.setting("DefaultDirName"), r"{autopf}\Agents Hub")   # %LOCALAPPDATA%\Programs
        self.assertRegex(self.setting("AppId"), r"^\{\{[0-9A-F-]{36}\}$")

    def test_the_hub_is_set_up_and_taken_off_by_app_py(self):
        self.assertIn("Args := ' install'", self.iss)   # + ' --silent' for a silent install (no move dialog)
        self.assertIn("AppScript() + ' stop'", self.iss)
        self.assertIn("AppScript() + Args", self.iss)
        self.assertIn("MB_YESNO or MB_DEFBUTTON2, IDNO", self.iss)   # "delete the data?" — No by default, and silent
        self.assertIn(r'Name: "{userprograms}\Hub by Icons8"; Filename: "{app}\runtime\python\pythonw.exe"', self.iss)
        # the entry of the name before goes on an upgrade: one Hub by Icons8 in the Start menu, as on Linux
        self.assertIn(r'Type: files; Name: "{userprograms}\Agents Hub.lnk"', self.iss.split("[InstallDelete]")[1]
                      .split("\n[")[0])

    def test_the_programs_it_starts_are_where_the_factory_puts_them(self):
        spec = importlib.util.spec_from_file_location("hub_build_paths2", os.path.join(REPO, "tools", "package",
                                                                                          "build.py"))
        build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build)
        win = build.PATHS["windows"]
        self.assertIn("{app}\\" + win["pythonw"].replace("/", "\\"), self.iss)
        self.assertIn("{app}\\" + win["python"].replace("/", "\\"), self.iss)
        self.assertEqual(load("app").python("A").replace(os.sep, "/"), "A/" + win["python"])
        with open(PS1, encoding="utf-8-sig") as f:
            self.assertIn('Join-Path $App "' + win["python"].replace("/", "\\") + '"', f.read())

    def test_it_stops_before_any_change_on_a_hub_from_git_or_the_script(self):
        # installer spec 13-6 (as the Mac app does): the app's own hub has its code in the state folder; a journal
        # without it, or a page already answering, is another hub — InitializeSetup refuses before the wizard
        init = self.iss[self.iss.index("function ForeignHub"):self.iss.index("function PrepareToInstall")]
        # the app's own hub of the layer era: <state>\code is a junction to a layer with MANIFEST.json, no .git folder
        # ...which Setup.exe's own process cannot enter (RedirectionGuard: an installed app was refused as a foreign hub):
        # the layers are looked at by their own folders
        self.assertIn("OwnLayer()", init)
        own = self.iss[self.iss.index("function OwnLayer"):self.iss.index("function ForeignHub")]
        self.assertIn("'\\layers\\*'", own)
        self.assertIn("'\\MANIFEST.json'", own)
        self.assertNotIn("\\code\\", own)
        for need in ("\\code\\.git", "\\code\\MANIFEST.json", "\\journal.jsonl", "\\state.json", "PageAnswers()", "TaskExists()",
                     "Result := not ForeignHub();", "CustomMessage('Foreign')"):
            self.assertIn(need, init)
        self.assertIn("{localappdata}\\hub-core", self.iss)   # where install.ps1 and app.py put the hub

    def test_the_setup_speaks_english_and_carries_every_message(self):
        # the setup comes before the hub knows its owner's language: English only (the page then takes the catalog)
        self.assertEqual(re.findall(r'(?m)^Name: "(\w+)"', self.iss), ["en"])
        en = set(re.findall(r"(?m)^en\.(\w+)=", self.iss))
        self.assertEqual(set(re.findall(r"(?m)^(\w+)\.\w+=", self.iss[self.iss.index("[CustomMessages]"):
                                                                     self.iss.index("[InstallDelete]")])), {"en"})
        self.assertTrue({"Starting", "Failed", "Open", "AskPurge", "Foreign"} <= en)
        for name in re.findall(r"CustomMessage\('(\w+)'\)", self.iss) + re.findall(r"\{cm:(\w+)\}", self.iss):
            self.assertIn(name, en)


class SetupExe(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="win-exe-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.mod = load("setup_exe")
        self.top = os.path.join(self.root, "agents-hub-v1.2.3-windows-x64")
        for rel in ("app.py", "app.ico", "launch.py", os.path.join("runtime", "python", "pythonw.exe")):
            os.makedirs(os.path.dirname(os.path.join(self.top, rel)), exist_ok=True)
            open(os.path.join(self.top, rel), "w").close()
        os.makedirs(os.path.join(self.top, "code-seed"))
        with open(os.path.join(self.top, "PACKAGED"), "w") as f:
            json.dump({"format": "app", "version": "1.2.3", "platform": "windows-x64"}, f)

    def test_the_exe_is_named_for_the_tag(self):
        cmd, exe = self.mod.command("ISCC.exe", self.top, os.path.join(self.root, "out"))
        self.assertEqual(os.path.basename(exe), "agents-hub-v1.2.3-windows-x64-setup.exe")
        self.assertIn("/DVersion=1.2.3", cmd)
        self.assertEqual(cmd[-1], os.path.join(WIN, "agents-hub.iss"))

    def test_a_build_of_another_platform_or_version_is_refused(self):
        with open(os.path.join(self.top, "PACKAGED"), "w") as f:
            json.dump({"format": "app", "version": "1.2.4", "platform": "windows-x64"}, f)
        with self.assertRaises(self.mod.Refused):
            self.mod.command("ISCC.exe", self.top, self.root)


if __name__ == "__main__":
    unittest.main()
