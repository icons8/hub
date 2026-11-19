"""The hub on a Mac as the «Agents Hub.app» bundle. launchd used to start python3 directly: «Login Items» showed a
logoless «python3», and macOS did not let it into ~/Desktop, where the repository lived — hub-up failed with «Operation
not permitted». Now the agent starts the bundle's executable: macOS attributes the name, logo and folder access to the
bundle, and the children (Python) get the same access. The bundle sits in the repository ready-made and signed."""
import tests  # noqa: F401 — do not see the machine's settings
import importlib.util
import json
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest

import platforms

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("mac_app", os.path.join(ROOT, "deploy", "mac", "app.py"))
app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(app)

ON_MAC = sys.platform == "darwin" and all(shutil.which(t) for t in ("clang", "sips", "iconutil", "codesign"))


@unittest.skipIf(os.name == "nt", "a macOS launchd agent: its paths are POSIX")
class Plists(unittest.TestCase):
    def test_agent_runs_the_bundle_executable_with_python_and_hub_up(self):
        bundle = "/state/Agents Hub.app"
        agent = app.agent_plist("/py/python3.12", "/repo", "/state", bundle)
        self.assertEqual(agent["Label"], "com.hub-core")
        self.assertEqual(agent["ProgramArguments"], [
            os.path.join(bundle, "Contents", "MacOS", app.info_plist()["CFBundleExecutable"]),
            "/py/python3.12", "/repo/bin/hub-up"])
        self.assertEqual(agent["EnvironmentVariables"]["HUB_STATE_DIR"], "/state")
        self.assertTrue(agent["EnvironmentVariables"]["PATH"].startswith("/state/bin:"))  # the hub's python3 comes first
        self.assertTrue(agent["KeepAlive"])
        self.assertEqual(agent["AssociatedBundleIdentifiers"], ["com.hub-core"])  # icon and name come from the app

    def test_launchd_waits_for_the_cores_snapshot_before_it_kills(self):
        # launchd sends SIGKILL about 20 s after SIGTERM unless the agent says otherwise; hub-up waits for the core longer
        from platforms import posix
        self.assertGreater(app.agent_plist("/py/python3", "/repo", "/state")["ExitTimeOut"], posix.stop_grace(True))

    def test_the_hub_service_runs_without_light_resource_limits(self):
        # a job without ProcessType is Standard: under load (load about 300) the core's loop got a fraction of a core,
        # read a session's facts 13 min late and waited 60 s for a process that had exited 3 s after its input closed
        agent = app.agent_plist("/py/python3", "/repo", "/state")
        self.assertEqual(agent["ProcessType"], "Interactive")
        # the mark below_hub clamps the children by: a service without it (installed before) keeps them as they were
        from platforms import posix
        self.assertEqual(agent["EnvironmentVariables"][posix.INTERACTIVE], "1")
        self.assertEqual(app.app_agent_plist("/state/code", "/state", "/Applications/Agents Hub.app")["ProcessType"],
                         "Interactive")

    def test_without_a_bundle_python_runs_directly(self):
        agent = app.agent_plist("/py/python3", "/repo", "/state")
        self.assertEqual(agent["ProgramArguments"], ["/py/python3", "/repo/bin/hub-up"])
        self.assertNotIn("AssociatedBundleIdentifiers", agent)

    def test_the_downloaded_app_runs_its_shell_from_launchd(self):
        # installer S5: the agent starts the app's own shell (deploy/mac/appshell.py), which knows its Python
        agent = app.app_agent_plist("/state/code", "/state", "/Applications/Agents Hub.app")
        self.assertEqual(agent["ProgramArguments"], ["/Applications/Agents Hub.app/Contents/MacOS/Agents Hub",
                                                     "--serve"])
        self.assertEqual(agent["WorkingDirectory"], "/state")   # not the code: launchd needs it, the shell brings it
        self.assertEqual(agent["EnvironmentVariables"]["HUB_STATE_DIR"], "/state")
        self.assertTrue(agent["KeepAlive"])
        # its own bundle identifier: the bundle of a hub from git (another hub on the same Mac) is com.hub-core with
        # another signature — macOS keeps folder permissions by identifier and signature, one would cost the other its own
        self.assertEqual(agent["AssociatedBundleIdentifiers"], [app.APP_ID])
        self.assertNotEqual(app.APP_ID, app.LABEL)
        self.assertEqual(app.info_plist("1.2.3", "13.0", app.APP_ID)["CFBundleIdentifier"], app.APP_ID)

    def test_settings_of_a_hub_from_the_app_name_the_apps_programs(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        with open(os.path.join(tmp, "paths.json"), "w") as f:
            json.dump({"node": "runtime/node/bin/node", "ffmpeg": "runtime/ffmpeg/bin/ffmpeg",
                       "mcp": "runtime/mcp/node_modules/chrome-devtools-mcp/build/src/bin/chrome-devtools-mcp.js"}, f)
        state = os.path.join(tmp, "state")
        cfg = app.app_config(state, tmp)
        self.assertEqual(cfg["browser"]["mcp"], [os.path.join(tmp, "runtime/node/bin/node"), os.path.join(
            tmp, "runtime/mcp/node_modules/chrome-devtools-mcp/build/src/bin/chrome-devtools-mcp.js")])
        self.assertEqual(cfg["voice"]["ffmpeg"], os.path.join(tmp, "runtime/ffmpeg/bin/ffmpeg"))
        self.assertEqual(cfg["hub"]["cwd"], os.path.join(state, "hub-home"))
        self.assertNotIn("sets", [k for k, v in cfg.items() if v])     # no hub-skills next to a checkout

    def test_bundle_has_name_and_logo(self):
        info = app.info_plist()
        self.assertEqual((info["CFBundleName"], info["CFBundleIdentifier"]), ("Agents Hub", "com.hub-core"))
        self.assertTrue(info["CFBundleIconFile"])
        self.assertTrue(info["LSUIElement"])  # no Dock icon


class Logo(unittest.TestCase):
    def test_oklch_to_srgb(self):
        self.assertEqual(app.oklch_hex(0.627955, 0.257683, 29.234), "#ff0000")
        self.assertEqual(app.oklch_hex(1, 0, 0), "#ffffff")
        self.assertEqual(app.oklch_hex(0, 0, 0), "#000000")

    def test_logo_svg_is_readable_by_sips(self):
        with open(os.path.join(ROOT, "modules", "web", "favicon.svg")) as f:
            self.assertNotIn("oklch", app.logo_svg(f.read()))

    def test_logo_has_margin_like_other_mac_icons(self):
        svg = app.logo_svg('<svg viewBox="0 0 32 32"><rect width="32" height="32" fill="#fff"/></svg>')
        x, y, w, h = map(float, svg.split('viewBox="')[1].split('"')[0].split())
        self.assertAlmostEqual(32 / w, 824 / 1024, places=3)
        self.assertAlmostEqual(x, -(w - 32) / 2, places=3)
        self.assertAlmostEqual(y, x, places=3)


# the open flavor's launcher: in place in the open tree, in deploy/mac/open/ of a team's (core/appcode.OPEN_SWAP)
OPEN_BUNDLE = next((p for p in (os.path.join(app.HERE, app.OPEN_NAME + ".app"),
                                os.path.join(app.HERE, "open", app.OPEN_NAME + ".app")) if os.path.isdir(p)), None)
TEAM_BUNDLE = os.path.join(app.HERE, app.NAME + ".app")


@unittest.skipUnless(sys.platform == "darwin", "macOS only")
class ShippedBundle(unittest.TestCase):
    def test_bundle_in_the_repo_is_signed_and_whole(self):
        # the team's carries the company's Developer ID; the open flavor's is signed ad hoc (the open tree has only it)
        for bundle, marks in ((TEAM_BUNDLE, ["Identifier=com.hub-core", "Authority=Developer ID Application"]),
                              (OPEN_BUNDLE, [f"Identifier={app.OPEN_ID}", "Signature=adhoc"])):
            if bundle is None or bundle == TEAM_BUNDLE and not os.path.isdir(bundle):
                continue
            with self.subTest(bundle):
                self.assertTrue(app.verified(bundle), f"{bundle}: the signature does not match — app.py build")
                out = subprocess.run(["codesign", "-dv", "--verbose=2", bundle], capture_output=True, text=True).stderr
                for mark in marks:
                    self.assertIn(mark, out)
                exe = app.executable(bundle)
                archs = subprocess.run(["lipo", "-archs", exe], capture_output=True, text=True).stdout.split()
                self.assertEqual(sorted(archs), ["arm64", "x86_64"])
                self.assertEqual(subprocess.run([exe, "/bin/sh", "-c", "exit 3"]).returncode, 3)
        self.assertIn(app.BUNDLE, (TEAM_BUNDLE, os.path.join(app.HERE, app.OPEN_NAME + ".app")))


@unittest.skipUnless(OPEN_BUNDLE, "a tree without the open flavor's launcher")
class OpenBundle(unittest.TestCase):
    """The open flavor's launcher (built here from launcher.c, app.py build - "deploy/mac/open/Hub by Icons8.app" --id
    com.icons8.hub): macOS shows the folder's name, so the folder, the executable and Info.plist say "Hub by Icons8";
    its own identifier — an ad-hoc signature is not the team's Developer ID, macOS keeps folder access by both."""

    def test_its_name_identifier_and_executable(self):
        with open(os.path.join(OPEN_BUNDLE, "Contents", "Info.plist"), "rb") as f:
            info = plistlib.load(f)
        self.assertEqual({k: info[k] for k in ("CFBundleName", "CFBundleDisplayName", "CFBundleExecutable",
                                                 "CFBundleIdentifier")},
                         {"CFBundleName": "Hub by Icons8", "CFBundleDisplayName": "Hub by Icons8",
                          "CFBundleExecutable": "Hub by Icons8", "CFBundleIdentifier": "com.icons8.hub"})
        self.assertEqual(info, app.info_plist(identifier=app.OPEN_ID, name=app.OPEN_NAME))   # what app.py build writes
        self.assertEqual(app.executable(OPEN_BUNDLE), os.path.join(OPEN_BUNDLE, "Contents", "MacOS", "Hub by Icons8"))
        self.assertNotIn(app.OPEN_ID, (app.LABEL, app.APP_ID))

    def test_universal_and_without_a_certificate(self):
        import struct
        with open(app.executable(OPEN_BUNDLE), "rb") as f:
            data = f.read()
        self.assertEqual(data[:4], b"\xca\xfe\xba\xbe")   # a fat Mach-O: its architectures follow
        count = struct.unpack(">I", data[4:8])[0]
        cpus = {struct.unpack(">i", data[8 + 20 * i:12 + 20 * i])[0] for i in range(count)}
        self.assertEqual(cpus, {0x0100000c, 0x01000007})   # arm64, x86_64
        from core import appcode
        for d, _, names in os.walk(OPEN_BUNDLE):
            for n in names:
                with open(os.path.join(d, n), "rb") as f:
                    self.assertNotIn(appcode.SIGNED, f.read(), n)   # an ad-hoc signature carries no certificate

    def test_every_place_knows_both_names(self):
        # the state folder keeps one of them; an install, an uninstall and the app's move take either
        from core import uninstall
        self.assertEqual(uninstall.BUNDLES, app.NAMES)
        r = subprocess.run([sys.executable, os.path.join(app.HERE, "app.py"), "names"], capture_output=True, text=True)
        self.assertEqual(r.stdout.splitlines(), list(app.NAMES))
        spec = importlib.util.spec_from_file_location("hub_test_build_icns", os.path.join(ROOT, "tools", "package", "build.py"))
        build = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build)
        self.assertTrue(os.path.isfile(build.ICNS), build.ICNS)   # the Windows shell's icon, in either tree

    def test_it_is_signed_ad_hoc_only(self):
        bundle = os.path.join(tempfile.mkdtemp(), app.OPEN_NAME + ".app")
        self.addCleanup(platforms.current.rmtree, os.path.dirname(bundle))
        os.makedirs(bundle)
        with self.assertRaises(SystemExit):
            app.build(bundle, "Developer ID Application: Example Ltd (ABCDE12345)")
        self.assertTrue(os.path.isdir(bundle))   # refused before anything was touched

    def test_the_other_flavors_copy_goes_once_the_agent_runs_through_this_one(self):
        state = tempfile.mkdtemp()
        self.addCleanup(platforms.current.rmtree, state)
        for name in app.NAMES:
            os.makedirs(os.path.join(state, name, "Contents", "MacOS"))
        calls = []
        python_only = app.agent_plist("/py/python3", "/repo", state)   # the new one did not verify: nothing goes
        self.assertEqual(app.drop_others(state, python_only, run=lambda *a, **k: calls.append(a[0])), [])
        new = os.path.join(state, "Hub by Icons8.app")
        agent = app.agent_plist("/py/python3", "/repo", state, new)
        self.assertEqual(app.drop_others(state, agent, run=lambda *a, **k: calls.append(a[0])),
                         [os.path.join(state, "Agents Hub.app")])
        self.assertEqual(calls, [[app.LSREGISTER, "-u", os.path.join(state, "Agents Hub.app")]])
        self.assertEqual(sorted(os.listdir(state)), ["Hub by Icons8.app"])

        def no_launchservices(*a, **k):
            raise FileNotFoundError(a[0][0])
        os.makedirs(os.path.join(state, "Agents Hub.app"))
        self.assertEqual(len(app.drop_others(state, agent, run=no_launchservices)), 1)   # goes all the same

    def test_the_agent_runs_it_by_its_name_and_identifier(self):
        agent = app.agent_plist("/py/python3", "/repo", "/state", OPEN_BUNDLE)
        self.assertEqual(agent["ProgramArguments"][0], os.path.join(OPEN_BUNDLE, "Contents", "MacOS", "Hub by Icons8"))
        self.assertEqual(agent["AssociatedBundleIdentifiers"], ["com.icons8.hub"])
        self.assertEqual(agent["Label"], "com.hub-core")   # the service is the same hub's


@unittest.skipUnless(ON_MAC, "macOS only, with clang, sips, iconutil, codesign")
class Launcher(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.bundle = os.path.join(cls.tmp, "Agents Hub.app")
        app.build(cls.bundle)
        cls.exe = os.path.join(cls.bundle, "Contents", "MacOS", app.NAME)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def test_universal_and_signed(self):
        archs = subprocess.run(["lipo", "-archs", self.exe], capture_output=True, text=True).stdout.split()
        self.assertEqual(sorted(archs), ["arm64", "x86_64"])
        self.assertTrue(app.verified(self.bundle))

    def test_the_folders_name_is_the_apps(self):
        # the open flavor's bundle is built so (macOS shows the folder's name: CFBundleDisplayName alone did not)
        bundle = os.path.join(self.tmp, "named", app.OPEN_NAME + ".app")
        app.build(bundle)   # its identifier by default: the open flavor's own
        self.assertTrue(os.path.isfile(os.path.join(bundle, "Contents", "MacOS", app.OPEN_NAME)))
        self.assertEqual(app.executable(bundle), os.path.join(bundle, "Contents", "MacOS", app.OPEN_NAME))
        self.assertEqual(app.bundle_id(bundle), app.OPEN_ID)
        self.assertTrue(app.verified(bundle))
        out = subprocess.run(["codesign", "-dv", bundle], capture_output=True, text=True).stderr
        self.assertIn("Signature=adhoc", out)

    def test_copy_keeps_the_signature(self):
        copy = os.path.join(self.tmp, "copy", "Agents Hub.app")
        subprocess.run(["ditto", self.bundle, copy], check=True)
        self.assertTrue(app.verified(copy))

    def test_icon_on_the_executable_keeps_it_runnable(self):
        """«Login Items» take the icon from the executable (it used to show a generic icon)."""
        copy = os.path.join(self.tmp, "icon", "Agents Hub.app")
        subprocess.run(["ditto", self.bundle, copy], check=True)
        self.assertTrue(app.set_file_icon(copy))
        exe = os.path.join(copy, "Contents", "MacOS", app.NAME)
        self.assertIn("com.apple.ResourceFork", subprocess.run(["xattr", exe], capture_output=True, text=True).stdout)
        self.assertEqual(subprocess.run(["codesign", "--verify", copy]).returncode, 0)
        self.assertEqual(subprocess.run([exe, "/bin/sh", "-c", "exit 3"]).returncode, 3)

    def shell_app(self):
        """A copy of the bundle with the downloaded app's shell: Resources/hub/appshell.py run by its Python."""
        copy = os.path.join(tempfile.mkdtemp(dir=self.tmp), "Agents Hub.app")
        subprocess.run(["ditto", self.bundle, copy], check=True)
        hub = os.path.join(copy, "Contents", "Resources", "hub")
        os.makedirs(os.path.join(hub, "runtime", "python", "bin"))
        py = os.path.join(hub, "runtime", "python", "bin", "python3")   # a file, not a link out of the app (the seal)
        with open(py, "w") as f:
            f.write(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        os.chmod(py, 0o755)
        with open(os.path.join(hub, "appshell.py"), "w") as f:
            f.write("import sys; print(' '.join(sys.argv[1:]), sys.flags.isolated, sys.flags.dont_write_bytecode)\n")
        open(os.path.join(hub, "x.dylib"), "w").close()
        # sealed again, as macapp.py leaves it: a quarantined app with a broken seal is not run at all
        subprocess.run(["codesign", "--force", "--sign", "-", copy], check=True, capture_output=True)
        return os.path.join(copy, "Contents", "MacOS", app.NAME)

    def test_opened_by_finder_the_app_runs_its_shell(self):
        exe = self.shell_app()
        for args, want in (([], "open 1 1"), (["-psn_0_12345"], "open 1 1"), (["--serve"], "serve 1 1")):
            with self.subTest(args):
                r = subprocess.run([exe, *args], capture_output=True, text=True, timeout=30)
                self.assertEqual((r.returncode, r.stdout.strip()), (0, want), r.stderr)
        # a program as before: the hub from git starts hub-up through the same launcher
        self.assertEqual(subprocess.run([exe, "/bin/sh", "-c", "exit 3"]).returncode, 3)

    def test_the_downloads_quarantine_goes_before_anything_inside_runs(self):
        """macOS kills a quarantined program the app starts (the bridge, 01.11: SIGKILL), even after the person allowed
        the app: the launcher takes the mark off the app's files first."""
        exe = self.shell_app()
        app_dir = os.path.dirname(os.path.dirname(os.path.dirname(exe)))
        inside = os.path.join(app_dir, "Contents", "Resources", "hub", "x.dylib")
        # the app as after "Open Anyway": its own mark carries the approval (0x40; without it macOS kills the launcher
        # started from a process — the bridge, 01.11), its files still the download's (0083)
        subprocess.run(["xattr", "-w", "com.apple.quarantine", "00c3;6a000000;Safari;", app_dir], check=True)
        for path in (inside, os.path.join(app_dir, "Contents", "Resources", "hub", "appshell.py")):
            subprocess.run(["xattr", "-w", "com.apple.quarantine", "0083;6a000000;Safari;", path], check=True)
        r = subprocess.run([exe], capture_output=True, text=True, timeout=30)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "open 1 1"), r.stderr)
        for path in (app_dir, inside):
            self.assertNotEqual(subprocess.run(["xattr", "-p", "com.apple.quarantine", path],
                                               capture_output=True).returncode, 0, path)

    def test_from_the_image_or_translocated_it_only_asks_to_move_it(self):
        exe = self.shell_app()
        app_dir = os.path.dirname(os.path.dirname(os.path.dirname(exe)))
        moved = os.path.join(self.tmp, "x", "AppTranslocation", "0A1B", "d", "Agents Hub.app")
        os.makedirs(os.path.dirname(moved))
        subprocess.run(["ditto", app_dir, moved], check=True)
        stubs = tempfile.mkdtemp(dir=self.tmp)
        said = os.path.join(stubs, "said")
        with open(os.path.join(stubs, "osascript"), "w") as f:
            f.write(f'#!/bin/sh\necho "$@" > "{said}"\n')
        os.chmod(os.path.join(stubs, "osascript"), 0o755)
        r = subprocess.run([os.path.join(moved, "Contents", "MacOS", app.NAME)], capture_output=True, text=True,
                           timeout=30, env={**os.environ, "PATH": stubs + ":/usr/bin:/bin"})
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stdout, "")                         # the shell did not run
        with open(said) as f:
            self.assertIn("Applications", f.read())
        # --serve from such a path (launchd) is not a person: it runs
        r = subprocess.run([os.path.join(moved, "Contents", "MacOS", app.NAME), "--serve"], capture_output=True,
                           text=True, timeout=30)
        self.assertEqual(r.stdout.strip(), "serve 1 1")

    def test_without_a_shell_no_arguments_is_still_a_usage_error(self):
        self.assertEqual(subprocess.run([self.exe], capture_output=True).returncode, 64)

    def test_launcher_passes_child_exit_code(self):
        self.assertEqual(subprocess.run([self.exe, "/bin/sh", "-c", "exit 3"]).returncode, 3)

    def test_launcher_forwards_sigterm_to_child(self):
        """launchd stops the agent with SIGTERM to the launcher; hub-up must receive it and stop the children."""
        code = "import signal,sys,time; signal.signal(15, lambda *a: sys.exit(7)); print('ready', flush=True); " \
               "time.sleep(30)"
        p = subprocess.Popen([self.exe, sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        self.assertEqual(p.stdout.readline().strip(), "ready")
        p.send_signal(signal.SIGTERM)
        self.assertEqual(p.wait(timeout=10), 7)
        p.stdout.close()


if __name__ == "__main__":
    unittest.main()
