"""The Mac install (deploy/mac/install.sh) ends with exit code 0. The last line used to be `[ -n "$INSTANCE" ] && echo …`:
for an ordinary hub (without HUB_INSTANCE) the condition is false, and after a successful install the script exited with
code 1.

The check runs in a foreign HOME, with launchctl, ditto, codesign and sleep replaced by call recorders: the test does not
touch the real system (on Linux too)."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from tests import winsafe

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(REPO, "deploy", "mac", "install.sh")


def stub(path, body):
    with open(path, "w") as f:
        f.write("#!/bin/sh\n" + body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)


class Home:
    """A foreign HOME with recorders of the system tools (shared by the test classes below)."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="mac-home-")
        self.addCleanup(shutil.rmtree, self.home, True)
        self.bin = os.path.join(self.home, "stubs")
        os.makedirs(self.bin)
        os.makedirs(os.path.join(self.home, "Library", "LaunchAgents"))
        self.calls = os.path.join(self.home, "calls.log")
        for name in ("launchctl", "ditto", "codesign", "sleep"):
            # launchctl print — "no such service": the wait after bootout does not spin; codesign — the package is unsigned
            stub(os.path.join(self.bin, name), f'echo "{name} $*" >> "{self.calls}"\n'
                 + ('[ "$1" = print ] && exit 1\n' if name == "launchctl" else "")
                 + ("exit 1\n" if name == "codesign" else "exit 0\n"))
        winsafe.symlink(self, sys.executable, os.path.join(self.bin, "python3.14"))   # portable: the installer's name
        self.env = {"HOME": self.home, "PATH": self.bin + ":/usr/bin:/bin", "LANG": "C.UTF-8", "USER": "x",
                    "SHELL": "/bin/zsh"}


@unittest.skipIf(os.name == "nt", "an sh script")
class MacInstall(Home, unittest.TestCase):

    def run_install(self, **env):
        r = subprocess.run(["sh", INSTALL], env={**self.env, **env}, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(self.calls) as f:
            self.assertIn(f"launchctl bootstrap gui/{os.getuid()} ", f.read())   # reached the end
        return r.stdout + r.stderr

    def test_ordinary_hub_exits_zero(self):
        self.run_install()
        self.assertTrue(os.path.exists(os.path.join(self.home, "Library", "LaunchAgents", "com.hub-core.plist")))
        self.assertTrue(os.path.exists(os.path.join(self.home, ".local", "bin", "hub")))

    def test_second_hub_exits_zero_and_says_where_it_is(self):
        out = self.run_install(HUB_INSTANCE="spare")
        # its ports are handed out past every hub of the machine (core/instance.py), not one 8797 for every second hub
        from core import instance
        state = os.path.join(self.home, ".hub-spare")
        anchor = instance.load(state)
        self.assertEqual(anchor["service"], {"kind": "launchd", "id": "com.hub-spare"})
        self.assertIn(f"Hub spare: page http://127.0.0.1:{anchor['ports']['web']}/, command hub-spare", out)
        self.assertTrue(os.path.exists(os.path.join(self.home, ".local", "bin", "hub-spare")))
        with open(os.path.join(self.home, "Library", "LaunchAgents", "com.hub-spare.plist"), "rb") as f:
            agent = plistlib.load(f)
        env = agent["EnvironmentVariables"]
        self.assertEqual((env["HUB_STATE_DIR"], env["HUB_CONFIG_DIR"], env["HUB_WEB_PORT"]),
                         (state, os.path.join(self.home, ".config", "hub-spare"), str(anchor["ports"]["web"])))
        with open(os.path.join(self.home, ".config", "hub-spare", "hub.json")) as f:
            browser = json.load(f)["browsers"]["mac"]
        self.assertEqual((browser["port"], browser["profile"]),
                         (anchor["ports"]["browsers"]["mac"], os.path.join(state, "browsers", "mac")))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config", "hub-spare", "state-dir")))

    def checkout_without_bundle(self):
        """A copy of what the installer reads from the repository, without the signed bundle."""
        repo = os.path.join(self.home, "repo-without-bundle")
        for rel in subprocess.run(["git", "ls-files", "-z", "deploy/mac", "deploy/commands.py", "hub-home", "bin",
                                    "modules/web/favicon.svg"],
                                  cwd=REPO, capture_output=True, text=True, check=True).stdout.split("\0"):
            if rel and ".app/" not in rel and os.path.isfile(os.path.join(REPO, rel)):   # neither flavor's bundle
                os.makedirs(os.path.join(repo, os.path.dirname(rel)), exist_ok=True)
                shutil.copy2(os.path.join(REPO, rel), os.path.join(repo, rel))
        return os.path.join(repo, "deploy", "mac", "install.sh")

    def open_checkout(self):
        """The same checkout as the open flavor has it: its ad-hoc "Hub by Icons8.app" in deploy/mac (core/appcode.OPEN_SWAP)."""
        script = self.checkout_without_bundle()
        src = next(p for p in (os.path.join(REPO, "deploy", "mac", "Hub by Icons8.app"),
                               os.path.join(REPO, "deploy", "mac", "open", "Hub by Icons8.app")) if os.path.isdir(p))
        shutil.copytree(src, os.path.join(os.path.dirname(script), "Hub by Icons8.app"), symlinks=True)
        return script

    def stub_toolchain(self, clang=True):
        """clang, sips and iconutil write the file they are asked for; codesign signs but does not verify (so the plist step
        takes no bundle and leaves LaunchServices and the file icon alone, which a Linux box has not got): the bundle build
        runs for real, the compiler and the system tools do not."""
        stub(os.path.join(self.bin, "codesign"), f'echo "codesign $*" >> "{self.calls}"\n'
             'case " $* " in *" --verify "*) exit 1 ;; esac\nexit 0\n')
        stub(os.path.join(self.bin, "sips"), 'while [ $# -gt 0 ]; do [ "$1" = --out ] && : > "$2"; shift; done\n')
        stub(os.path.join(self.bin, "iconutil"), 'while [ $# -gt 0 ]; do [ "$1" = -o ] && : > "$2"; shift; done\n')
        if clang:
            stub(os.path.join(self.bin, "clang"), f'echo "clang $*" >> "{self.calls}"\n'
                 'while [ $# -gt 0 ]; do [ "$1" = -o ] && : > "$2"; shift; done\n')

    def install_from(self, script):
        r = subprocess.run(["sh", script], env=self.env, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(self.calls) as f:
            calls = f.read()
        self.assertIn(f"launchctl bootstrap gui/{os.getuid()} ", calls)
        return r.stdout + r.stderr, calls

    def test_without_a_ready_bundle_the_installer_builds_its_own(self):
        # a checkout without the signed bundle: the installer builds one from launcher.c, ad-hoc signed
        script = self.checkout_without_bundle()
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(script), "Agents Hub.app")))
        self.stub_toolchain()
        _, calls = self.install_from(script)
        bundle = os.path.join(self.home, ".hub-core", "Agents Hub.app")
        self.assertTrue(os.path.isfile(os.path.join(bundle, "Contents", "MacOS", "Agents Hub")))
        self.assertTrue(os.path.isfile(os.path.join(bundle, "Contents", "Info.plist")))
        self.assertRegex(calls, r"codesign --force --sign - .*Agents Hub\.app")
        self.assertIn("launcher.c", calls)
        # the repository stays clean: the bundle goes to the state folder, not next to launcher.c
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(script), "Agents Hub.app")))

    def test_the_open_flavors_bundle_goes_into_the_state_folder_by_its_name(self):
        # the open flavor's checkout: its bundle "Hub by Icons8.app" is copied as it is (no build); the team's copy an
        # earlier install left stays while the new one does not verify (codesign here says no): the agent runs Python
        script = self.open_checkout()
        old = os.path.join(self.home, ".hub-core", "Agents Hub.app", "Contents")
        os.makedirs(old)
        self.stub_toolchain()
        out, calls = self.install_from(script)
        repo = os.path.dirname(script)
        self.assertIn(f"ditto {os.path.join(repo, 'Hub by Icons8.app')} "
                      f"{os.path.join(self.home, '.hub-core', 'Hub by Icons8.app')}", calls)
        self.assertNotIn("clang", calls)
        self.assertNotIn("--sign", calls)   # signed already, ad hoc: not signed again
        self.assertTrue(os.path.exists(os.path.dirname(old)))   # tests/test_mac_app.py: it goes once the new one runs

    @unittest.skipUnless(sys.platform == "darwin", "the real ditto and codesign of a Mac")
    def test_on_a_mac_the_agent_runs_through_the_ad_hoc_bundle(self):
        # the open flavor's bundle comes ad hoc and as a clone has it (no file attributes): the signature check holds
        # (codesign --verify --strict) and launchd starts the hub through it, not Python directly
        script = self.open_checkout()
        old = os.path.join(self.home, ".hub-core", "Agents Hub.app", "Contents")   # the team's copy of before
        os.makedirs(old)
        for name in ("ditto", "codesign"):
            os.unlink(os.path.join(self.bin, name))
        bundle = os.path.join(self.home, ".hub-core", "Hub by Icons8.app")
        import importlib.util
        spec = importlib.util.spec_from_file_location("hub_test_mac_app_install", os.path.join(REPO, "deploy", "mac", "app.py"))
        app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(app)
        self.addCleanup(subprocess.run, [app.LSREGISTER, "-u", bundle], capture_output=True)
        self.install_from(script)
        with open(os.path.join(self.home, "Library", "LaunchAgents", "com.hub-core.plist"), "rb") as f:
            agent = plistlib.load(f)
        self.assertEqual(agent["ProgramArguments"][0], os.path.join(bundle, "Contents", "MacOS", "Hub by Icons8"))
        self.assertEqual(agent["AssociatedBundleIdentifiers"], ["com.icons8.hub"])
        self.assertFalse(os.path.exists(os.path.dirname(old)))   # the agent runs through the new one: the old one went
        # the icon set on its executable after the check (app.py set_file_icon) is outside the strict check, as before
        self.assertEqual(subprocess.run(["codesign", "--verify", bundle], capture_output=True).returncode, 0)

    def test_without_a_ready_bundle_and_without_clang_the_install_goes_on(self):
        script = self.checkout_without_bundle()
        self.stub_toolchain(clang=False)
        # /usr/bin on a Mac holds the clang shim: leave only what the installer needs
        for name in ("sh", "uname", "id", "dirname", "ln", "mkdir", "rm", "mv", "cat", "printf", "chmod", "cp", "grep", "head", "sed",
                     "tr", "ls", "date", "env", "readlink", "basename", "test", "cut", "sort", "mktemp", "touch", "wc",
                     "tail", "awk"):
            for d in ("/usr/bin", "/bin"):
                if os.path.exists(os.path.join(d, name)) and not os.path.exists(os.path.join(self.bin, name)):
                    winsafe.symlink(self, os.path.join(d, name), os.path.join(self.bin, name))
        self.env["PATH"] = self.bin
        out, _ = self.install_from(script)
        self.assertIn("No clang", out)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".hub-core", "Agents Hub.app")))
        self.assertTrue(os.path.exists(os.path.join(self.home, "Library", "LaunchAgents", "com.hub-core.plist")))


@unittest.skipIf(os.name == "nt", "an sh script")
class AppMode(Home, unittest.TestCase):
    """HUB_APP: the downloaded app's shell runs install.sh of the code (the whole path — tests/test_mac_shell.py)."""

    def run_app(self, link=True, **env):
        app = os.path.join(self.home, "Applications", "Agents Hub.app")
        hub = os.path.join(app, "Contents", "Resources", "hub")
        os.makedirs(os.path.join(hub, "runtime", "python", "bin"))
        winsafe.symlink(self, sys.executable, os.path.join(hub, "runtime", "python", "bin", "python3"))
        with open(os.path.join(hub, "paths.json"), "w") as f:
            f.write('{"node": "runtime/node/bin/node"}')
        if link:
            os.makedirs(os.path.join(self.home, ".hub-core"))
            winsafe.symlink(self, hub, os.path.join(self.home, ".hub-core", "app"))
        return subprocess.run(["sh", INSTALL], env={**self.env, "HUB_APP": app, **env}, capture_output=True, text=True,
                              timeout=120), app

    def test_app_mode_registers_the_apps_shell_and_asks_nothing(self):
        r, app = self.run_app()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("Claude Code", r.stdout + r.stderr)          # the hub's "Setup" page brings it
        import plistlib
        with open(os.path.join(self.home, "Library", "LaunchAgents", "com.hub-core.plist"), "rb") as f:
            self.assertEqual(plistlib.load(f)["ProgramArguments"],
                             [os.path.join(app, "Contents", "MacOS", "Agents Hub"), "--serve"])
        with open(self.calls) as f:
            calls = f.read()
        self.assertNotIn("ditto", calls)
        self.assertNotIn("codesign", calls)                            # the app is not touched or re-checked

    def test_app_mode_only_for_the_main_hub(self):
        r, _ = self.run_app(HUB_INSTANCE="spare")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.home, "Library", "LaunchAgents", "com.hub-spare.plist")))

    def test_app_mode_without_the_apps_environment_stops(self):
        r, _ = self.run_app(link=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Agents Hub.app", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.home, "Library", "LaunchAgents", "com.hub-core.plist")))


if __name__ == "__main__":
    unittest.main()
