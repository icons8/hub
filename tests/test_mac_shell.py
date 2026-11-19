"""The downloaded "Agents Hub.app" (installer spec S5): its shell, deploy/mac/appshell.py, which the app's executable runs
with "open" (Finder) or "serve" (the launchd agent).

A person double-clicks the app: it must not take over a hub installed from git, must clone the code from its seed, register the agent through deploy/mac/install.sh in app mode and open the hub's page — and a second double
click only opens the page. (The disk image, App Translocation and the quarantine are the launcher's — tests/test_mac_app.py
on a Mac.) launchd starts the agent: the code, the app's programs first in PATH (the git of /usr/bin on a
fresh Mac is a stub asking for Xcode tools), then hub-up of the code.

The app is laid out in a temporary folder the way tools/package/macapp.py does (the shell and launch.py frozen in
Contents/Resources/hub, the seed a git clone of a tag of this repository's Mac files); launchctl, osascript, open and
defaults are recorders; the "page" is a small HTTP server of the test. Runs on Linux too."""
import tests  # noqa: F401 — the machine's settings stay out
import http.server
import importlib.util
import json
import os
import plistlib
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from tests import winsafe

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(REPO, "tools", "package")
EXE = os.path.join("Contents", "MacOS", "Agents Hub")


def g(cwd, *a):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def stub(path, body):
    with open(path, "w") as f:
        f.write("#!/bin/sh\n" + body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def load_shell(hub):
    sys.path.insert(0, hub)
    try:
        spec = importlib.util.spec_from_file_location("hub_appshell_test", os.path.join(hub, "appshell.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(hub)
    return mod


class Hello(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


@unittest.skipIf(os.name == "nt", "the Mac app shell")
class AppShell(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="mac-shell-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = os.path.join(self.root, "home")
        os.makedirs(os.path.join(self.home, "Library", "LaunchAgents"))
        self.app = os.path.join(self.root, "Applications", "Agents Hub.app")
        self.hub = os.path.join(self.app, "Contents", "Resources", "hub")
        self.lay_out_app(self.hub)
        self.stubs = os.path.join(self.root, "stubs")
        os.makedirs(self.stubs)
        self.calls = os.path.join(self.home, "calls.log")
        self.alive = os.path.join(self.home, "agent.alive")
        self.port = free_port()
        # launchd: bootstrap starts "the hub" — a page server on the port — and print answers while it runs
        stub(os.path.join(self.stubs, "launchctl"),
             f'echo "launchctl $*" >> "{self.calls}"\n'
             f'case "$1" in\n'
             f'  print) [ -e "{self.alive}" ] && kill -0 "$(cat "{self.alive}")" 2>/dev/null && exit 0; exit 113 ;;\n'
             f'  bootstrap) "{sys.executable}" -m http.server --bind 127.0.0.1 {self.port} >/dev/null 2>&1 &\n'
             f'    echo $! > "{self.alive}" ;;\n'
             f'  bootout) [ -e "{self.alive}" ] && kill "$(cat "{self.alive}")" 2>/dev/null; rm -f "{self.alive}" ;;\n'
             f'esac\nexit 0\n')
        self.addCleanup(self.stop_hub)
        for name in ("osascript", "open", "ditto", "codesign", "sleep"):
            stub(os.path.join(self.stubs, name), f'echo "{name} $*" >> "{self.calls}"\n')
        stub(os.path.join(self.stubs, "defaults"), 'echo \'(\n    "ru-RU"\n)\'\n')
        self.env = {"HOME": self.home, "PATH": self.stubs + ":/usr/bin:/bin", "LANG": "C.UTF-8", "USER": "x",
                    "SHELL": "/bin/zsh", "HUB_WEB_PORT": str(self.port)}

    def lay_out_app(self, hub):
        """As macapp.py: the platform folder of build.py, the shell frozen next to launch.py, the seed on a tag."""
        os.makedirs(os.path.join(hub, "runtime", "python", "bin"))
        winsafe.symlink(self, sys.executable, os.path.join(hub, "runtime", "python", "bin", "python3"))
        for d in ("node/bin", "gh/bin", "ffmpeg/bin", "mcp/node_modules/chrome-devtools-mcp/build/src/bin"):
            os.makedirs(os.path.join(hub, "runtime", d))
        for rel in ("node/bin/node", "ffmpeg/bin/ffmpeg", "mcp/node_modules/chrome-devtools-mcp/build/src/bin/m.js"):
            stub(os.path.join(hub, "runtime", rel), "exit 0\n")
        os.makedirs(os.path.join(hub, "bin"))
        stub(os.path.join(hub, "bin", "git"), 'exec /usr/bin/git "$@"\n')   # the app's git wrapper
        shutil.copy2(os.path.join(REPO, "deploy", "mac", "appshell.py"), hub)
        shutil.copy2(os.path.join(PKG, "launch.py"), hub)
        with open(os.path.join(hub, "PACKAGED"), "w") as f:
            json.dump({"format": "app", "version": "1.2.3", "runtime": 1, "platform": "mac-arm64"}, f)
        with open(os.path.join(hub, "paths.json"), "w") as f:
            json.dump({"python": "runtime/python/bin/python3", "node": "runtime/node/bin/node",
                       "ffmpeg": "runtime/ffmpeg/bin/ffmpeg", "launch": "launch.py",
                       "mcp": "runtime/mcp/node_modules/chrome-devtools-mcp/build/src/bin/m.js"}, f)
        # the seed: what install.sh of the code reads, committed and tagged
        up = os.path.join(self.root, "up")
        files = subprocess.run(["git", "ls-files", "-z", "deploy/mac", "deploy/commands.py", "hub-home", "bin",
                                "tools/package/first_run.py",
                                "modules/web/favicon.svg"], cwd=REPO, capture_output=True, text=True,
                               check=True).stdout.split("\0")
        for rel in files:
            if rel and ".app/" not in rel and os.path.isfile(os.path.join(REPO, rel)):   # neither flavor's bundle
                os.makedirs(os.path.join(up, os.path.dirname(rel)), exist_ok=True)
                shutil.copy2(os.path.join(REPO, rel), os.path.join(up, rel))
        g(up, "init", "-q", "-b", "main")
        g(up, "add", "-A")
        g(up, "commit", "-qm", "v1.2.3")
        g(up, "tag", "v1.2.3")
        g(self.root, "clone", "-q", "--depth", "1", "--branch", "v1.2.3", "file://" + up, os.path.join(hub, "code-seed"))
        g(os.path.join(hub, "code-seed"), "remote", "set-url", "origin", "https://example.test/core.git")

    def stop_hub(self):
        try:
            with open(self.alive) as f:
                os.kill(int(f.read()), 15)
        except (OSError, ValueError):
            pass

    def page(self):
        """Something else answering on the hub's port."""
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), Hello)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)

    def open_app(self, hub=None, **env):
        hub = hub or self.hub
        r = subprocess.run([os.path.join(hub, "runtime", "python", "bin", "python3"), "-I", "-B",
                            os.path.join(hub, "appshell.py"), "open"], env={**self.env, **env}, capture_output=True,
                           text=True, timeout=300)
        return r.returncode, r.stdout + r.stderr

    def recorded(self):
        try:
            with open(self.calls) as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def state(self, *rel):
        return os.path.join(self.home, ".hub-core", *rel)

    def test_first_open_installs_the_hub_in_app_mode_and_opens_the_page(self):
        code, out = self.open_app()
        self.assertEqual(code, 0, out + self.recorded())
        calls = self.recorded()
        self.assertIn(f"open http://127.0.0.1:{self.port}/", calls)
        self.assertNotIn("osascript", calls)                                          # no dialog on the way
        self.assertIn(f"launchctl bootstrap gui/{os.getuid()} ", calls)
        code_dir = self.state("code")
        self.assertEqual(g(code_dir, "describe", "--tags"), "v1.2.3")                 # the seed cloned
        self.assertEqual(os.readlink(self.state("app")), self.hub)
        with open(os.path.join(self.home, "Library", "LaunchAgents", "com.hub-core.plist"), "rb") as f:
            agent = plistlib.load(f)
        self.assertEqual(agent["ProgramArguments"], [os.path.join(self.app, EXE), "--serve"])
        self.assertEqual(agent["WorkingDirectory"], self.state())   # exists before the code does
        self.assertEqual(agent["EnvironmentVariables"]["HUB_STATE_DIR"], self.state())
        with open(os.path.join(self.home, ".config", "hub-core", "hub.json")) as f:
            cfg = json.load(f)
        app_link = self.state("app")
        self.assertEqual(cfg["browser"]["mcp"], [os.path.join(app_link, "runtime/node/bin/node"), os.path.join(
            app_link, "runtime/mcp/node_modules/chrome-devtools-mcp/build/src/bin/m.js")])
        self.assertEqual(cfg["voice"]["ffmpeg"], os.path.join(app_link, "runtime/ffmpeg/bin/ffmpeg"))
        self.assertNotIn("browsers", cfg)                    # no Homebrew paths, no owner's Chrome of a git install
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.home, ".config", "hub-core", "hub.json")).st_mode),
                         0o600)
        self.assertEqual(os.readlink(self.state("bin", "python3")), os.path.join(app_link, "runtime/python/bin/python3"))
        with open(os.path.join(self.home, ".local", "bin", "hub")) as f:
            wrapper = f.read()
        self.assertIn(f'PATH="{app_link}/bin:', wrapper)                             # the app's git in a terminal too
        self.assertNotIn("ditto", calls)                     # no bundle of a git install copied or built
        self.assertFalse(os.path.exists(self.state("Agents Hub.app")))
        with open(self.state("logs", "setup.log")) as f:
            self.assertIn("app: installing", f.read())

    def test_second_open_while_the_hub_runs_only_opens_the_page(self):
        self.assertEqual(self.open_app()[0], 0)
        os.unlink(self.calls)
        code, out = self.open_app()
        self.assertEqual(code, 0, out)
        calls = self.recorded()
        self.assertNotIn("bootstrap", calls)
        self.assertNotIn("bootout", calls)
        self.assertIn(f"open http://127.0.0.1:{self.port}/", calls)

    def test_a_stopped_agent_is_registered_again(self):
        self.assertEqual(self.open_app()[0], 0)
        self.stop_hub()                    # launchd does not have it (logged out, `hub off`)
        os.unlink(self.alive)
        os.unlink(self.calls)
        self.assertEqual(self.open_app()[0], 0)
        self.assertIn("bootstrap", self.recorded())

    def test_a_moved_app_takes_the_agent_and_the_link_over(self):
        self.assertEqual(self.open_app()[0], 0)
        moved = os.path.join(self.root, "Elsewhere", "Agents Hub.app")
        shutil.copytree(self.app, moved, symlinks=True)
        hub = os.path.join(moved, "Contents", "Resources", "hub")
        os.unlink(self.calls)
        code, out = self.open_app(hub)
        self.assertEqual(code, 0, out)
        self.assertIn("bootstrap", self.recorded())
        with open(os.path.join(self.home, "Library", "LaunchAgents", "com.hub-core.plist"), "rb") as f:
            self.assertEqual(plistlib.load(f)["ProgramArguments"], [os.path.join(moved, EXE), "--serve"])
        self.assertEqual(os.readlink(self.state("app")), hub)

    def test_a_hub_from_git_is_left_alone(self):
        with open(os.path.join(self.home, "Library", "LaunchAgents", "com.hub-core.plist"), "wb") as f:
            plistlib.dump({"Label": "com.hub-core", "ProgramArguments": ["/state/Agents Hub.app/Contents/MacOS/Agents Hub",
                                                                         "/py/python3", "/repo/bin/hub-up"]}, f)
        code, out = self.open_app()
        self.assertEqual(code, 3, out)
        calls = self.recorded()
        self.assertIn("osascript", calls)
        self.assertIn("already installed", calls)   # English, though the Mac speaks Russian (defaults): before the
        # hub knows its owner's language, the app speaks English (the page then takes the catalog)
        self.assertNotIn("launchctl bootstrap", calls)
        self.assertFalse(os.path.exists(self.state("code")))

    def test_settings_of_a_hub_installed_another_way_are_left_alone(self):
        # `hub uninstall` of a hub from git keeps its data: not "a hub is installed", but its settings are not the app's
        os.makedirs(os.path.join(self.home, ".config", "hub-core"))
        with open(os.path.join(self.home, ".config", "hub-core", "hub.json"), "w") as f:
            f.write("{}")
        code, _ = self.open_app()
        self.assertEqual(code, 3)
        self.assertIn("Settings and data of a hub", self.recorded())
        self.assertNotIn("already installed", self.recorded())
        self.assertFalse(os.path.exists(self.state("code")))

    def test_the_apps_own_settings_left_by_an_uninstall_are_taken_back(self):
        self.assertEqual(self.open_app()[0], 0)
        self.stop_hub()
        os.unlink(os.path.join(self.home, "Library", "LaunchAgents", "com.hub-core.plist"))   # `hub uninstall`
        shutil.rmtree(self.state())                                                         # …and the state gone
        os.unlink(self.calls)
        code, out = self.open_app()
        self.assertEqual(code, 0, out + self.recorded())
        self.assertIn("bootstrap", self.recorded())
        self.assertTrue(os.path.isdir(self.state("sandbox")))       # the project of the kept hub.json is there again

    def test_data_of_a_hub_installed_another_way_without_its_settings_is_left_alone(self):
        # its settings folder gone, its journal kept in ~/.hub-core: the app would run the core over someone's journal
        os.makedirs(self.state())
        with open(self.state("journal.jsonl"), "w") as f:
            f.write('{"seq": 1}\n')
        code, _ = self.open_app()
        self.assertEqual(code, 3)
        self.assertIn("Data of a hub installed", self.recorded())
        self.assertFalse(os.path.exists(self.state("code")))

    def test_the_apps_own_data_without_its_code_is_taken_back(self):
        # an install or an update cut off: the journal stays, the code or app.json is gone — the app's own still
        self.assertEqual(self.open_app()[0], 0)
        for gone in ("code", "app.json"):
            with self.subTest(gone):
                self.stop_hub()
                path = self.state(gone)
                shutil.rmtree(path) if os.path.isdir(path) else os.unlink(path)
                with open(self.state("journal.jsonl"), "a") as f:
                    f.write('{"seq": 1}\n')
                code, out = self.open_app()
                self.assertEqual(code, 0, out + self.recorded())

    def test_a_git_hubs_settings_mentioning_the_apps_folder_are_still_not_the_apps(self):
        os.makedirs(os.path.join(self.home, ".config", "hub-core"))
        with open(os.path.join(self.home, ".config", "hub-core", "hub.json"), "w") as f:
            json.dump({"projects": {"x": {"path": self.state("app") + "/x"}},
                       "browser": {"mcp": ["/opt/homebrew/bin/npx", "chrome-devtools-mcp"]}}, f)
        self.assertEqual(self.open_app()[0], 3)

    def test_a_fresh_account_has_no_launch_agents_folder_yet(self):
        shutil.rmtree(os.path.join(self.home, "Library"))
        code, out = self.open_app()
        self.assertEqual(code, 0, out + self.recorded())
        self.assertTrue(os.path.exists(os.path.join(self.home, "Library", "LaunchAgents", "com.hub-core.plist")))
        self.assertTrue(os.path.isdir(self.state("sandbox")))    # the project hub.json names

    def test_hub_on_off_and_uninstall_find_the_apps_agent(self):
        self.assertEqual(self.open_app()[0], 0)
        sys.path.insert(0, REPO)
        try:
            from platforms import posix
        finally:
            sys.path.remove(REPO)
        u = posix.service_unit(self.state(), home=self.home, platform="darwin")
        self.assertEqual((u["kind"], u["id"]), ("launchd", "com.hub-core"))

    def test_a_busy_port_is_said_before_anything_is_installed(self):
        self.page()                                            # something answers, no agent of ours
        code, out = self.open_app()
        self.assertEqual(code, 3, out)
        self.assertIn(str(self.port), self.recorded())
        self.assertFalse(os.path.exists(self.state("code")))

    def test_an_install_that_fails_says_where_the_log_is(self):
        stub(os.path.join(self.stubs, "launchctl"), f'echo "launchctl $*" >> "{self.calls}"\n'
             '[ "$1" = bootstrap ] && exit 5\n[ "$1" = print ] && exit 113\nexit 0\n')
        code, out = self.open_app()
        self.assertEqual(code, 4, out)
        self.assertIn("setup.log", self.recorded())
        self.assertFalse(os.path.exists(self.state("app.json")))   # the next open tries again

    def test_a_page_that_does_not_come_says_so(self):
        shell = load_shell(self.hub)
        ticks = iter(range(0, 1000, 50))
        self.assertFalse(shell.wait_page(free_port(), seconds=120, clock=lambda: next(ticks), sleep=lambda s: None))

    def test_the_mac_s_proxy_does_not_answer_for_the_page(self):
        self.page()                                    # "a proxy" that answers anything
        shell = load_shell(self.hub)
        with mock.patch.dict(os.environ, {"http_proxy": f"http://127.0.0.1:{self.port}", "no_proxy": "",
                                          "HTTP_PROXY": f"http://127.0.0.1:{self.port}", "NO_PROXY": ""}):
            self.assertFalse(shell.wait_page(free_port(), seconds=0, sleep=lambda s: None))


@unittest.skipIf(os.name == "nt", "the Mac app shell")
class Paths(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="mac-shell-")
        self.addCleanup(shutil.rmtree, self.root, True)
        hub = os.path.join(self.root, "A.app", "Contents", "Resources", "hub")
        os.makedirs(hub)
        shutil.copy2(os.path.join(REPO, "deploy", "mac", "appshell.py"), hub)
        shutil.copy2(os.path.join(PKG, "launch.py"), hub)
        for d in ("bin", "runtime/node/bin", "runtime/gh/bin"):
            os.makedirs(os.path.join(hub, d))
        self.hub = hub
        self.shell = load_shell(hub)

    def test_the_apps_programs_come_first_in_path_once(self):
        env = self.shell.serve_env({"PATH": "/usr/bin:/bin"})
        first = env["PATH"].split(":")
        self.assertEqual(first[:3], [os.path.join(self.hub, "bin"), os.path.join(self.hub, "runtime/node/bin"),
                                     os.path.join(self.hub, "runtime/gh/bin")])   # ffmpeg is not there: not added
        self.assertEqual(first[3:], ["/usr/bin", "/bin"])
        self.assertEqual(self.shell.serve_env(env), env)

    def test_serve_brings_the_code_and_becomes_hub_up(self):
        state = os.path.join(self.root, "state")
        got = {}
        with mock.patch.dict(os.environ, {"HUB_STATE_DIR": state, "PATH": "/usr/bin:/bin"}), \
                mock.patch.object(self.shell.launch, "ensure_code", return_value=self.root) as ensure, \
                mock.patch.object(self.shell.launch, "resume_trial") as trial, \
                mock.patch.object(self.shell.os, "chdir") as chdir:
            os.makedirs(state)
            self.shell.serve(execve=lambda exe, argv, env: got.update(exe=exe, argv=argv, env=env))
            self.assertEqual(os.environ["PATH"].split(":")[0], os.path.join(self.hub, "bin"))
        python = os.path.join(self.hub, "runtime", "python", "bin", "python3")
        ensure.assert_called_once_with(os.path.join(self.hub, "code-seed"), os.path.join(self.hub, "PACKAGED"), state)
        trial.assert_called_once_with(state, python)
        chdir.assert_called_once_with(self.root)
        self.assertEqual(got["argv"], [python, os.path.join(self.root, "bin", "hub-up")])
        self.assertTrue(got["env"]["PATH"].startswith(os.path.join(self.hub, "bin") + ":"))
        self.assertEqual(os.readlink(os.path.join(state, "app")), self.hub)

    def test_serve_without_code_exits_for_launchd_to_retry(self):
        state = os.path.join(self.root, "state")
        with mock.patch.dict(os.environ, {"HUB_STATE_DIR": state}), \
                mock.patch.object(self.shell.launch, "ensure_code", side_effect=RuntimeError("git clone: no space")):
            with mock.patch("sys.stderr"):
                self.assertEqual(self.shell.serve(execve=lambda *a: self.fail("exec")), 1)

    def test_an_agent_plist_that_is_not_well_formed_xml_is_unreadable_not_a_crash(self):
        # plistlib refuses a bare "&" with an expat error, which is not a ValueError
        d = os.path.join(self.root, "home", "Library", "LaunchAgents")
        os.makedirs(d)
        with open(os.path.join(d, self.shell.LABEL + ".plist"), "w") as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict><key>ProgramArguments</key>'
                    '<string>a & b</string></dict></plist>\n')
        with mock.patch.dict(os.environ, {"HOME": os.path.join(self.root, "home")}):
            self.assertIsNone(self.shell.agent())

    def test_which_agent_is_the_apps(self):
        exe = "/Applications/Agents Hub.app/Contents/MacOS/Agents Hub"
        self.assertTrue(self.shell.ours([exe, "--serve"]))
        self.assertFalse(self.shell.ours([exe, "/py/python3", "/repo/bin/hub-up"]))   # a hub from git with its bundle
        self.assertFalse(self.shell.ours(["/py/python3", "/repo/bin/hub-up"]))
        self.assertFalse(self.shell.ours(None))

    def test_every_message_is_one_english_text(self):
        # the dialogs come before the hub knows its owner's language: one English text each, whatever the Mac speaks
        for key, text in self.shell.TEXT.items():
            self.assertIsInstance(text, str, key)
            self.assertTrue(all(c.isascii() for c in text if c.isalpha()), key)   # English letters only


if __name__ == "__main__":
    unittest.main()
