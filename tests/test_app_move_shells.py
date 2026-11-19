"""The shells' side of the move from a git clone (app spec §8): the release's `hub app find --json` read by launch.py,
the dialog's three answers and the order of the steps in the Windows app.py, the Mac appshell.py and the .deb's
first_run.py — the commands they run are substituted (no service of this machine is touched)."""
import tests  # noqa: F401 — the machine's settings stay out
import importlib.util
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import platforms
from tests import winsafe

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(REPO, "tools", "package")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TEXTS = {"title": "T", "text": "move it?", "move": "Move", "stay": "Stay on the clone", "later": "Later",
         "waiting": "waiting", "failed": "failed", "undo_failed": "the return did not finish"}


class Hub:
    """subprocess.run substitute for the release's `hub app …`: find answers a clone, the rest succeed; every call kept."""

    def __init__(self, found=True, move=0):
        self.calls, self.found, self.move = [], found, move

    def __call__(self, cmd, **kw):
        cmd = list(cmd)
        if "app" in cmd:
            args = cmd[cmd.index("app") + 1:]
            self.calls.append(args)
            if args[:1] == ["find"]:
                out = json.dumps({"repo": "/home/x/agents-hub-core", "version": "v1.8.0", "texts": TEXTS}) if self.found else "{}"
                return subprocess.CompletedProcess(cmd, 0 if self.found else 3, out + "\n", "")
            if args[:1] == ["move"] and "--app" in args:
                return subprocess.CompletedProcess(cmd, self.move, "", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        self.calls.append(cmd[:1])
        return subprocess.CompletedProcess(cmd, 0, "", "")


class Layout(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="move-shell-")
        self.addCleanup(platforms.current.rmtree, self.root)
        self.launch = load("hub_launch_move", os.path.join(PKG, "launch.py"))
        self.seed = os.path.join(self.root, "code-seed")
        os.makedirs(os.path.join(self.seed, "core"))
        os.makedirs(os.path.join(self.seed, "bin"))
        for rel in ("core/appmove.py", "bin/hub"):
            open(os.path.join(self.seed, rel), "w").close()
        self.state = os.path.join(self.root, "state")
        os.makedirs(self.state)


class Seed(Layout):
    def test_git_hub_reads_the_releases_find(self):
        hub = Hub()
        got = self.launch.git_hub(self.seed, self.state, "py", run=hub)
        self.assertEqual(got["repo"], "/home/x/agents-hub-core")
        self.assertEqual(hub.calls, [["find", "--json"]])
        self.assertIsNone(self.launch.git_hub(self.seed, self.state, "py", run=Hub(found=False)))

    def test_an_older_seed_without_the_command_is_not_asked(self):
        os.unlink(os.path.join(self.seed, "core", "appmove.py"))
        hub = Hub()
        self.assertIsNone(self.launch.git_hub(self.seed, self.state, "py", run=hub))
        self.assertEqual(hub.calls, [])

    def test_the_command_runs_as_the_owner_in_this_state_folder(self):
        seen = {}

        def run(cmd, **kw):
            seen.update(kw["env"])
            return subprocess.CompletedProcess(cmd, 0, "{}", "")
        with mock.patch.dict(os.environ, {"HUB_SESSION": "maps", "HUB_STATE_DIR": "/elsewhere"}):
            self.launch.app_hub(self.seed, self.state, "stay", python="py", run=run)
        self.assertNotIn("HUB_SESSION", seen)
        self.assertEqual(seen["HUB_STATE_DIR"], self.state)


@unittest.skipIf(os.name == "nt", "the release's hub against a fake systemd unit or launchd agent")
class RealFind(unittest.TestCase):
    """launch.git_hub over the real bin/hub of this tree: the clone of a systemd unit (launchd agent on a Mac), the words in
    the hub's language."""

    def test_the_real_find(self):
        root = tempfile.mkdtemp(prefix="move-find-")
        self.addCleanup(platforms.current.rmtree, root)
        from tests.test_app_move import clone
        repo = clone(root)
        state = os.path.join(root, "state")
        units = os.path.join(root, "xdg", "systemd", "user")
        agents = os.path.join(root, "home", "Library", "LaunchAgents")
        os.makedirs(state)
        if sys.platform == "darwin":   # the service of a Mac is a launchd agent of the home folder
            os.makedirs(agents)
            with open(os.path.join(agents, "agents-hub.plist"), "wb") as f:
                plistlib.dump({"Label": "agents-hub", "ProgramArguments": ["/usr/bin/python3", f"{repo}/bin/hub-up"],
                               "EnvironmentVariables": {"HUB_STATE_DIR": state}}, f)
        else:
            os.makedirs(units)
            with open(os.path.join(units, "agents-hub.service"), "w") as f:
                f.write(f"[Service]\nExecStart=/usr/bin/python3 {repo}/bin/hub-up\nEnvironment=HUB_STATE_DIR={state}\n")
        launch = load("hub_launch_real", os.path.join(PKG, "launch.py"))
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": os.path.join(root, "xdg"), "HUB_LANG": "en",
                                          "HOME": os.path.join(root, "home")}):
            got = launch.git_hub(REPO, state, sys.executable)
        self.assertEqual(got["repo"], repo)
        self.assertIn(repo, got["texts"]["text"])
        self.assertEqual(got["texts"]["move"], "Move")


class Windows(Layout):
    def setUp(self):
        super().setUp()
        self.app_dir = os.path.join(self.root, "app")
        os.makedirs(self.app_dir)
        shutil.copy2(os.path.join(PKG, "launch.py"), self.app_dir)
        shutil.move(self.seed, os.path.join(self.app_dir, "code-seed"))
        self.app = load("hub_windows_app_move", os.path.join(PKG, "windows", "app.py"))
        sys.modules.pop("launch", None)
        self.addCleanup(sys.modules.pop, "launch", None)
        self.code = os.path.join(self.state, "code")
        os.makedirs(os.path.join(self.code, "bin"))
        open(os.path.join(self.code, "bin", "hub"), "w").close()
        env = mock.patch.dict(os.environ, {})
        env.start()
        self.addCleanup(env.stop)

    def install(self, hub, choice="move", up=True, **kw):
        sys.path.insert(0, self.app_dir)
        try:
            import launch
        finally:
            sys.path.remove(self.app_dir)
        with mock.patch.object(launch, "ensure_code", return_value=self.code), mock.patch.object(self.app, "LATE", 0):
            return self.app.install(self.app_dir, self.state, run=hub, ask=lambda texts: choice, up=lambda: up,
                                    wait=lambda done, log, late: done(), **kw)

    def test_move_runs_prepare_the_install_then_done(self):
        hub = Hub()
        self.assertEqual(self.install(hub), 0)
        self.assertEqual(hub.calls, [["find", "--json"], ["move", "/home/x/agents-hub-core", "--app", self.app_dir],
                                     ["schtasks"], ["powershell.exe"], ["move", "--done"]])   # /End of the orphan, then install.ps1

    def test_the_page_not_up_undoes(self):
        hub = Hub()
        self.assertEqual(self.install(hub, up=False), 4)
        self.assertEqual(hub.calls[-1], ["undo", "--failed"])

    def test_a_refused_prepare_installs_nothing(self):
        hub = Hub(move=1)
        self.assertEqual(self.install(hub), 5)
        self.assertNotIn(["powershell.exe"], hub.calls)

    def test_a_failed_prepare_whose_clone_did_not_start_again_is_the_unfinished_return(self):
        hub = Hub(move=4)   # appmove.NotBack
        self.assertEqual(self.install(hub), 7)
        self.assertNotIn(["powershell.exe"], hub.calls)

    def test_later_and_stay_change_nothing(self):
        for choice, calls in (("later", [["find", "--json"]]), ("stay", [["find", "--json"], ["stay"]])):
            hub = Hub()
            self.assertEqual(self.install(hub, choice=choice), 0)
            self.assertEqual(hub.calls, calls)

    def test_silent_asks_nothing_and_moves_nothing(self):
        hub = Hub()
        self.assertEqual(self.install(hub, silent=True), 0)
        self.assertEqual(hub.calls, [["find", "--json"]])

    def test_a_script_hub_that_is_not_a_clone_is_left_alone(self):
        with open(os.path.join(self.state, "hub-up.cmd"), "w") as f:
            f.write('@echo off\r\n"C:\\py\\python.exe" "C:\\other\\bin\\hub-up"\r\n')
        hub = Hub(found=False)
        self.assertEqual(self.install(hub), 6)
        self.assertNotIn(["powershell.exe"], hub.calls)

    def test_setup_lets_a_hub_of_the_install_script_through_to_the_dialog(self):
        with open(os.path.join(PKG, "windows", "agents-hub.iss"), encoding="utf-8") as f:
            iss = f.read()
        self.assertIn("FileExists(HubState() + '\\hub-up.cmd')", iss)
        self.assertIn("DirExists(HubState() + '\\layers')", iss)
        self.assertIn("' --silent'", iss)


@unittest.skipIf(os.name == "nt", "the Mac app shell")
class Mac(Layout):
    def setUp(self):
        super().setUp()
        self.hub_dir = os.path.join(self.root, "Agents Hub.app", "Contents", "Resources", "hub")
        os.makedirs(self.hub_dir)
        shutil.copy2(os.path.join(PKG, "launch.py"), self.hub_dir)
        shutil.copy2(os.path.join(REPO, "deploy", "mac", "appshell.py"), self.hub_dir)
        shutil.move(self.seed, os.path.join(self.hub_dir, "code-seed"))
        sys.path.insert(0, self.hub_dir)
        try:
            self.shell = load("hub_appshell_move", os.path.join(self.hub_dir, "appshell.py"))
        finally:
            sys.path.remove(self.hub_dir)
        os.makedirs(os.path.join(self.state, "logs"))
        self.code = os.path.join(self.state, "code")
        os.makedirs(os.path.join(self.code, "bin"))
        open(os.path.join(self.code, "bin", "hub"), "w").close()

    def test_the_dialogs_buttons(self):
        for said, want in (("Move", "move"), ("Stay on the clone", "stay"), ("", "later")):
            with mock.patch.object(self.shell.subprocess, "run",
                                   return_value=subprocess.CompletedProcess([], 0, said + "\n", "")) as run:
                self.assertEqual(self.shell.choose(TEXTS), want)
            self.assertEqual(run.call_args.args[0][-3:], ["Later", "Stay on the clone", "Move"])

    def move(self, hub, installed=True, page=True):
        sh = self.shell
        fake = lambda code, state, *a, **kw: hub(["py", "hub", "app", *a])   # noqa: E731
        with mock.patch.object(sh.launch, "app_hub", side_effect=fake), mock.patch.object(sh, "notify"), \
                mock.patch.object(sh.launch, "ensure_code", return_value=self.code), \
                mock.patch.object(sh, "link"), mock.patch.object(sh, "install", return_value=installed), \
                mock.patch.object(sh, "wait_page", return_value=page), mock.patch.object(sh, "dialog") as dialog:
            found = {"repo": "/home/x/agents-hub-core", "texts": TEXTS}
            return sh.move(self.state, found, 8787), dialog

    def test_move_prepare_install_done(self):
        hub = Hub()
        got, dialog = self.move(hub)
        self.assertEqual(got, 0)
        self.assertEqual(hub.calls, [["move", "/home/x/agents-hub-core", "--app", self.hub_dir], ["move", "--done"]])
        dialog.assert_not_called()

    def test_the_page_not_up_undoes_and_says_so(self):
        hub = Hub()
        got, dialog = self.move(hub, page=False)
        self.assertEqual(got, 4)
        self.assertEqual(hub.calls[-1], ["undo", "--failed"])
        dialog.assert_called_once()

    def test_a_refused_prepare_installs_nothing(self):
        hub = Hub(move=1)
        got, _ = self.move(hub)
        self.assertEqual(got, 6)
        self.assertEqual(len(hub.calls), 1)

    def test_a_failed_prepare_whose_clone_did_not_start_again_says_to_undo(self):
        got, dialog = self.move(Hub(move=4))   # appmove.NotBack
        self.assertEqual(got, 6)
        self.assertIn("the return did not finish", dialog.call_args.args[0])


@unittest.skipIf(os.name == "nt", "the .deb")
class Deb(Layout):
    def setUp(self):
        super().setUp()
        self.deb = os.path.join(self.root, "opt")
        os.makedirs(os.path.join(self.deb, "lib"))
        shutil.copy2(os.path.join(PKG, "launch.py"), self.deb)
        shutil.move(self.seed, os.path.join(self.deb, "code-seed"))
        self.first = load("hub_first_run_move", os.path.join(PKG, "first_run.py"))
        env = mock.patch.dict(os.environ, {"HUB_STATE_DIR": self.state})
        env.start()
        self.addCleanup(env.stop)

    def step(self, argv, choice="move", hub=None):
        hub = hub or Hub()
        return self.first.move_step(argv, root=self.deb, ask=lambda texts: choice, run=hub), hub

    def test_the_three_answers(self):
        self.assertEqual(self.step(["--move"], "later")[0], 3)
        code, hub = self.step(["--move"], "stay")
        self.assertEqual((code, hub.calls[-1]), (3, ["stay"]))
        code, hub = self.step(["--move"], None)   # no way to ask: nothing changes
        self.assertEqual((code, len(hub.calls)), (3, 1))
        code, hub = self.step(["--move"], "move")
        self.assertEqual((code, hub.calls[-1]), (self.first.MOVING, ["move", "/home/x/agents-hub-core", "--app", self.deb]))
        self.assertEqual(self.step(["--move"], "move", Hub(move=1))[0], 1)
        self.assertEqual(self.step(["--move"], "move", Hub(move=4))[0], 2)   # NotBack: the clone did not start again
        with open(os.path.join(REPO, "tools", "package", "deb", "files", "agents-hub"), encoding="utf-8") as f:
            self.assertIn('[ "$RC" -ne 2 ] || fail', f.read())

    def test_no_clone_goes_on_as_a_new_start(self):
        self.assertEqual(self.step(["--move"], "move", Hub(found=False))[0], 0)

    def test_after_the_page_done_or_undo_only_while_moving(self):
        code, hub = self.step(["--moved"])
        self.assertEqual((code, hub.calls), (self.first.NO_MOVE, []))
        self.assertEqual(self.step(["--move-failed"])[0], self.first.NO_MOVE)
        os.makedirs(os.path.join(self.state, "app-move"))
        with open(os.path.join(self.state, "app-move", "record.json"), "w") as f:
            json.dump({"status": "moving"}, f)
        self.assertEqual(self.step(["--moved"])[1].calls, [["move", "--done"]])
        code, hub = self.step(["--move-failed"])
        self.assertEqual((code, hub.calls), (0, [["undo", "--failed"]]))   # back on the clone
        failing = Hub()
        failing.__class__ = type("Failing", (Hub,), {"__call__": lambda me, cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "")})
        self.assertEqual(self.step(["--move-failed"], hub=failing)[0], 2)   # the return did not finish

    def test_the_terminal_answer(self):
        with mock.patch.dict(os.environ, {"DISPLAY": "", "WAYLAND_DISPLAY": ""}), \
                mock.patch("builtins.input", return_value="2"):
            self.assertEqual(self.first.choose(TEXTS, tty=True), "stay")
        with mock.patch.dict(os.environ, {"DISPLAY": "", "WAYLAND_DISPLAY": ""}):
            self.assertIsNone(self.first.choose(TEXTS, tty=False))

    def test_the_script_asks_before_the_start_and_confirms_after_the_page(self):
        with open(os.path.join(PKG, "deb", "files", "agents-hub"), encoding="utf-8") as f:
            text = f.read()
        self.assertLess(text.index("first_run.py\" --move"), text.index("CODE=$("))
        self.assertIn("first_run.py\" --moved", text)
        self.assertIn("first_run.py\" --move-failed", text)
        # a failure of the start itself, or of the service, while a move is half done goes back to the clone first
        self.assertIn('moved_back "${MSG:-', text)
        self.assertIn('first_run.py" --moved || true', text)   # also a move an earlier start left half done
        self.assertIn('enable --now agents-hub.service || moved_back', text)


if __name__ == "__main__":
    unittest.main()
