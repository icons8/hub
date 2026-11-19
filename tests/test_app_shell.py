"""The app shell's own update (app spec §4.4, slice A4). A release whose layer needs a newer environment comes with a
new app on the distribution server (releases.json runtime.apps): "Update" downloads it, the old shell's
tools/package/shellswap.py checks and lays it out, and at the full restart hub-up starts that old shell's watchdog and
exits (core/shellnext.py); the watchdog swaps the app (Mac: the bundle next to it, the old one to .old; Windows: a
silent Setup.exe once the hub is gone), waits for the core on the new app and puts the old one back when it does not
come. The hub writes up how it ended, and the release that waited for the app is installed next. Linux: the .deb is
downloaded and the page names the command; a newer app found under the running hub asks for the restart that takes it.

The macOS and Windows programs (hdiutil, ditto, codesign, Setup.exe, schtasks) are fakes here; the stands run the real
ones (tests/test_app_shell_mac.py on the Mac bridge, the winstand stand of the slice)."""
import tests  # noqa: F401 - keeps the machine's settings out
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import platforms
from core import shellnext
from tests import test_app_update as base

REPO = base.REPO
PKG = os.path.join(REPO, "tools", "package")


def shellswap():
    sys.path.insert(0, PKG)
    import shellswap as mod
    return mod


class Offer(base.Stand):
    """The hub's side: the offer, the press, the write-up (modules/update/layer.py)."""

    PLATFORM = "mac-arm64"

    def setUp(self):
        super().setUp()
        # the shell's own files next to its PACKAGED (the factory's layout): its launcher names them in layers/shell.json
        for n in ("launch.py", "shellswap.py"):
            shutil.copy2(os.path.join(PKG, n), self.shell)
        shutil.copy2(os.path.join(REPO, "core", "appcode.py"), self.shell)
        self.launch = base.load("hub_test_launch_shell", os.path.join(self.shell, "launch.py"))
        self.start(self.built["v1.2.3"]["seed"])
        self.release("v1.3.0", {"core/version.py": "RUNTIME_MIN = 2\n"})
        self.publish("v1.3.0", runtime_min=2)
        self.app_file = os.path.join(self.root, f"agents-hub-v1.3.0-{self.PLATFORM}.dmg")
        with open(self.app_file, "wb") as f:
            f.write(b"MZ the new app " * 100)
        self.publish_app(self.app_file, 2)
        self.ran = []

    def publish_app(self, path, runtime):
        send = sys.modules["deliver_send"] = base.load("deliver_send", os.path.join(PKG, "deliver_send.py"))
        self.addCleanup(sys.modules.pop, "deliver_send", None)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = send.main([self.url, "--key-file", self.key_file, "app", path, "--platform", self.PLATFORM,
                              "--runtime", str(runtime), "--send"])
        self.assertEqual(code, 0)

    def prepared(self, cmd, **kw):
        """shellswap.py prepare of the shell, as it ends when the new app passes: the record "ready"."""
        self.ran.append(cmd)
        rec = shellnext.read(self.state)
        shellnext.write(self.state, {**rec, "status": "ready", "watcher": [sys.executable, cmd[3]], "window": 180})
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def updater(self, claude="9.9.9", run=None, **update):
        from modules.update import layer
        cfg = {"update": {"enabled": True, "channel": "stable", "app": {"runtime": 1, "layer": True,
                                                                         "platform": self.PLATFORM}, **update},
               "dev": {"name": "hub-dev"}}
        return layer.LayerUpdater(os.path.join(self.codes(), self.current()), self.state, cfg, claude=lambda: claude,
                                  spawn=lambda *a: self.spawned.append(a), now=lambda: self.clock[0],
                                  run=run or self.prepared)

    def checked(self):
        return self.events("update.checked")[-1]["data"]


class MacOffer(Offer):
    def test_the_new_app_is_offered_with_the_button(self):
        self.assertEqual(self.updater().check(apply=False, manual=True), "needs-runtime")
        data = self.checked()
        self.assertEqual(data["shell"], {"runtime": 2, "size": os.path.getsize(self.app_file), "auto": True})
        self.assertEqual(self.ran, [])   # looking downloads nothing
        self.assertFalse(os.path.isdir(os.path.join(self.codes(), "shells")))

    def test_a_shell_without_the_swapper_keeps_the_download_link(self):
        old = os.path.join(self.root, "old-shell")
        os.makedirs(old)
        shutil.copy2(os.path.join(PKG, "launch.py"), old)   # built before slice A4: no shellswap.py next to it
        shell = self.read("layers", "shell.json")
        base.write(os.path.join(self.codes(), "shell.json"), json.dumps({**shell, "launch": os.path.join(old, "launch.py")}))
        self.assertEqual(self.updater().check(apply=True, manual=True), "needs-runtime")
        self.assertFalse(self.checked()["shell"]["auto"])
        self.assertEqual(self.ran, [])

    def test_update_downloads_the_app_has_it_checked_and_asks_for_the_restart(self):
        self.assertEqual(self.updater().check(apply=True, manual=True), "shell-ready")
        (cmd,) = self.ran
        dest = os.path.join(self.codes(), "shells", os.path.basename(self.app_file))
        self.assertEqual(cmd[1:5], ["-I", "-B", os.path.join(self.shell, "shellswap.py"), "prepare"])
        self.assertEqual(cmd[5:], [self.state, dest, "2"])
        with open(dest, "rb") as f, open(self.app_file, "rb") as want:
            self.assertEqual(f.read(), want.read())
        rec = shellnext.read(self.state)
        self.assertEqual((rec["status"], rec["then"], rec["runtime"]), ("ready", "1.3.0", 2))
        kinds = [e["type"] for e in self.events()]
        self.assertIn("hub.restart_requested", kinds)
        self.assertIn("owner.notify", kinds)
        self.assertFalse(self.exists("layers", "next.json"))   # the layer waits for the app

    def test_a_second_press_before_the_restart_does_nothing_again(self):
        self.updater().check(apply=True, manual=True)
        self.events()
        self.assertEqual(self.updater().check(apply=True, manual=True), "shell-ready")
        self.assertEqual(len(self.ran), 1)
        self.assertNotIn("hub.restart_requested", [e["type"] for e in self.events()])

    def test_an_app_that_does_not_pass_the_check_is_said_and_not_kept(self):
        def refused(cmd, **kw):
            self.ran.append(cmd)
            return subprocess.CompletedProcess(cmd, 3, "", "Hub by Icons8: the new app is not taken: codesign --verify: bad")
        self.assertEqual(self.updater(run=refused).check(apply=True, manual=True), "bad-shell")
        self.assertIn("codesign --verify", self.checked()["error"])
        self.assertEqual(os.listdir(os.path.join(self.codes(), "shells")), [])
        self.assertNotIn("hub.restart_requested", [e["type"] for e in self.events()])

    def test_a_damaged_download_is_not_handed_to_the_shell(self):
        from core import fetch

        def bad(url, dest, sha, size, *a, **kw):
            raise fetch.Failed("checksum", "the sum differs")
        u = self.updater()
        u.download = bad
        self.assertEqual(u.check(apply=True, manual=True), "bad-archive")
        self.assertEqual(self.ran, [])

    def test_done_is_written_up_once_and_the_waiting_release_is_installed_next(self):
        self.updater().check(apply=True, manual=True)
        self.events()
        shellnext.write(self.state, {**shellnext.read(self.state), "status": "done"})
        u = self.updater(app={"runtime": 2, "layer": True, "platform": self.PLATFORM})
        u.recover()
        notes = [e["data"]["text"] for e in self.events("owner.notify")]
        self.assertEqual(len(notes), 1)
        self.assertIn("2", notes[0])
        self.assertTrue(self.exists("update.install"))   # the owner's swap: its release installs as his, restarting
        u.recover()
        self.assertEqual(self.events("owner.notify"), [])   # once

    def test_a_background_swap_installs_its_release_by_a_check_that_asks_for_the_restart(self):
        self.assertEqual(self.updater().check(apply=True, manual=False), "shell-ready")
        self.assertIs(shellnext.read(self.state).get("ask"), True)
        ready = [e["data"] for e in self.events("owner.notify") if e["data"].get("ready")]
        self.assertEqual(len(ready), 1)
        shellnext.write(self.state, {**shellnext.read(self.state), "status": "done"})
        self.updater(app={"runtime": 2, "layer": True, "platform": self.PLATFORM}).recover()
        self.assertTrue(self.exists("update.now"))
        self.assertFalse(self.exists("update.install"))

    def test_a_returned_app_is_said_and_never_offered_again(self):
        self.updater().check(apply=True, manual=True)
        self.events()
        shellnext.write(self.state, {**shellnext.read(self.state), "status": "returned", "why": "timeout", "back": True})
        self.updater().recover()
        (failed,) = self.events("update.failed")
        self.assertEqual(failed["data"]["why"], "shell-returned")
        self.assertFalse(self.exists("update.install"))
        self.assertEqual(self.updater().check(apply=True, manual=True), "needs-runtime")
        self.assertNotIn("shell", self.checked())   # the download link of the shell, as before slice A4

    def test_the_downloaded_app_goes_once_written_up(self):
        self.updater().check(apply=True, manual=True)
        shellnext.write(self.state, {**shellnext.read(self.state), "status": "done"})
        u = self.updater(app={"runtime": 2, "layer": True, "platform": self.PLATFORM})
        u.recover()
        u.prune(self.current())
        self.assertEqual(os.listdir(os.path.join(self.codes(), "shells")), [])

    def test_a_broken_off_download_is_kept_for_the_next_press_and_a_lost_old_app_is_never_deleted(self):
        shells = os.path.join(self.codes(), "shells")
        os.makedirs(shells)
        base.write(os.path.join(shells, "agents-hub-v1.3.0-mac-arm64.dmg.part"), "half")
        os.utime(os.path.join(shells, "agents-hub-v1.3.0-mac-arm64.dmg.part"), (self.clock[0], self.clock[0]))
        old = os.path.join(self.root, "Agents Hub.app.old")
        os.makedirs(old)
        shellnext.write(self.state, {"status": "returned", "back": False, "reported": True, "old": old})
        self.updater().prune(self.current())
        self.assertEqual(os.listdir(shells), ["agents-hub-v1.3.0-mac-arm64.dmg.part"])
        self.assertTrue(os.path.isdir(old))
        shellnext.write(self.state, {"status": "returned", "back": True, "reported": True, "old": old})
        self.clock[0] += 25 * base.HOUR
        self.updater().prune(self.current())
        self.assertEqual(os.listdir(shells), [])
        self.assertFalse(os.path.isdir(old))

    def test_the_fold_keeps_the_shell_of_the_check(self):
        from core import reducer
        st, _, why = reducer.reduce(reducer.empty(), {"type": "update.checked", "ts": 1, "seq": 1, "data": {
            "result": "needs-runtime", "shell": {"runtime": 2, "size": 5, "auto": True, "file": "/x", "other": 1}}})
        self.assertIsNone(why)
        self.assertEqual(st["update_check"]["shell"], {"runtime": 2, "size": 5, "auto": True, "file": "/x"})
        st, _, _ = reducer.reduce(st, {"type": "update.checked", "ts": 2, "seq": 2, "data": {"result": "x",
                                                                                          "shell": {"runtime": "2"}}})
        self.assertNotIn("shell", st["update_check"])


class LinuxOffer(Offer):
    PLATFORM = "linux-x64"

    def test_update_downloads_the_deb_and_names_the_command(self):
        self.assertEqual(self.updater().check(apply=True, manual=True), "shell-downloaded")
        self.assertEqual(self.ran, [])   # no swapper: root installs a .deb
        dest = os.path.join(self.codes(), "shells", os.path.basename(self.app_file))
        got = self.events()
        self.assertEqual([e for e in got if e["type"] == "update.checked"][-1]["data"]["shell"]["file"], dest)
        (note,) = [e for e in got if e["type"] == "owner.notify"]
        self.assertIn(f"apt install {dest}", note["data"]["text"])
        self.updater().check(apply=False, manual=True)
        self.assertEqual(self.checked()["shell"], {"runtime": 2, "size": os.path.getsize(self.app_file), "auto": True,
                                                   "deb": True, "file": dest})
        self.assertEqual(self.updater().check(apply=True, manual=True), "shell-downloaded")   # not downloaded again

    def test_a_newer_app_under_the_running_hub_asks_for_the_restart_once(self):
        # the restart for an app put in place by a package manager is a POSIX one: on Windows the new app's Setup.exe
        # restarts the hub itself (APP_SWAP_ON_CHANGE is False there; its own test is below)
        patch = mock.patch.object(platforms.current, "APP_SWAP_ON_CHANGE", True)
        patch.start()
        self.addCleanup(patch.stop)
        self.updater().check(apply=True, manual=True)
        self.events()
        mark = json.loads(open(self.mark).read())
        base.write(self.mark, json.dumps({**mark, "runtime": 2, "version": "1.3.0"}))   # apt put the new app in place
        self.updater().check(apply=False)
        (asked,) = self.events("hub.restart_requested")
        self.assertEqual(asked["data"]["parts"], [])
        self.updater().check(apply=False)
        self.assertEqual(self.events("hub.restart_requested"), [])
        # the service started it again: the launcher's PACKAGED in the layer — the replacement is done
        self.start(self.built["v1.2.3"]["seed"])
        self.assertFalse(shellnext.changed(self.state, os.path.join(self.codes(), self.current())))
        u = self.updater(app={"runtime": 2, "layer": True, "platform": self.PLATFORM})
        u.recover()
        self.assertEqual(len(self.events("owner.notify")), 1)
        self.assertTrue(self.exists("update.install"))


class AtRestart(unittest.TestCase):
    """hub-up's full restart (core/shellnext.at_restart)."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hub-shell-restart-")
        self.addCleanup(platforms.current.rmtree, self.state)
        os.makedirs(os.path.join(self.state, "layers"))
        self.watcher = [sys.executable, os.path.join(PKG, "shellswap.py")]
        self.said, self.spawned = [], []
        self.t = [0.0]

    def rec(self, **kw):
        shellnext.write(self.state, {"status": "ready", "platform": "mac-arm64", "runtime": 2, "watcher": self.watcher,
                                     **kw})

    def run_it(self, then, service=True):
        def spawn(cmd):
            self.spawned.append(cmd)
            shellnext.write(self.state, {**shellnext.read(self.state), "status": then})
        return shellnext.at_restart(self.state, self.state, self.said.append, spawn=spawn, clock=lambda: self.t[0],
                                    sleep=lambda s: self.t.__setitem__(0, self.t[0] + s), service=service)

    def test_a_ready_app_starts_the_old_shells_watchdog_and_hub_up_exits(self):
        self.rec()
        self.assertEqual(self.run_it("swapped"), platforms.current.APP_SWAP_EXIT)
        self.assertEqual(self.spawned, [[*self.watcher[:1], "-I", "-B", self.watcher[1], "swap", self.state]])

    def test_a_swap_that_failed_lets_hub_up_start_again_on_the_old_app(self):
        self.rec()
        self.assertIsNone(self.run_it("failed"))

    def test_a_watchdog_that_never_answers_lets_hub_up_start_again(self):
        self.rec()
        self.assertIsNone(self.run_it("ready"))
        self.assertGreaterEqual(self.t[0], shellnext.WAIT)
        self.assertEqual(shellnext.read(self.state)["status"], "failed")   # a late watchdog then moves nothing

    def test_a_mac_swap_that_has_begun_is_seen_out(self):
        self.rec()
        open(os.path.join(self.state, "layers", shellnext.CLAIM), "w").close()   # the watchdog's
        self.assertEqual(self.run_it("swapping"), platforms.current.APP_SWAP_EXIT)
        self.assertGreaterEqual(self.t[0], shellnext.WAIT_SWAP)
        self.assertEqual(shellnext.read(self.state)["status"], "swapping")   # not marked: it is moving the folders

    def test_windows_lets_the_repeat_start_the_hub_again_when_the_swap_does_not_begin(self):
        self.rec(platform="windows-x64")
        self.assertIsNone(self.run_it("failed"))
        self.assertFalse(os.path.exists(os.path.join(self.state, "stopped")))

    def test_windows_holds_the_scheduler_away_while_setup_runs(self):
        self.rec(platform="windows-x64")
        self.assertEqual(self.run_it("swapping"), platforms.current.APP_SWAP_EXIT)
        self.assertTrue(os.path.exists(os.path.join(self.state, "stopped")))

    def test_a_watchdog_that_is_not_there_is_a_failure_not_a_hang(self):
        self.rec(watcher=[sys.executable, os.path.join(self.state, "gone.py")])
        self.assertIsNone(self.run_it("swapped"))
        self.assertEqual(self.spawned, [])
        self.assertEqual(shellnext.read(self.state)["status"], "failed")

    def test_a_newer_app_on_disk_exits_only_under_a_service(self):
        shell = os.path.join(self.state, "shell")
        layer = os.path.join(self.state, "layer")
        for d, runtime in ((shell, 2), (layer, 1)):
            os.makedirs(d)
            base.write(os.path.join(d, "PACKAGED"), json.dumps({"format": "app", "version": "1.3.0", "runtime": runtime}))
        base.write(os.path.join(self.state, "layers", "shell.json"), json.dumps({"launch": os.path.join(shell, "launch.py")}))
        if not platforms.current.APP_SWAP_ON_CHANGE:   # Windows: the new app's Setup.exe restarts the hub itself
            self.assertIsNone(shellnext.at_restart(self.state, layer, self.said.append, service=True))
            return
        self.assertEqual(shellnext.at_restart(self.state, layer, self.said.append, service=True), platforms.current.APP_SWAP_EXIT)
        self.assertIsNone(shellnext.at_restart(self.state, layer, self.said.append, service=False))

    def test_hub_up_asks_before_it_switches_the_layer_or_starts_again(self):
        with open(os.path.join(REPO, "bin", "hub-up"), encoding="utf-8") as f:
            src = f.read()
        part = src[src.index('os.environ[RESTARTED] = "1"'):src.index("plat.restart_hub_up(self_script(), argv)")]
        self.assertLess(part.index("shellnext.at_restart("), part.index("next_layer()"))
        self.assertIn("sys.exit(", part[part.index("shellnext.at_restart("):part.index("next_layer()")])


class Swap(unittest.TestCase):
    """The old shell's watchdog (tools/package/shellswap.py) with fakes of the system's programs."""

    PLATFORM = "mac-arm64"

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="hub-shellswap-")
        self.addCleanup(platforms.current.rmtree, self.root)
        self.sw = shellswap()
        self.state = os.path.join(self.root, "state")
        os.makedirs(os.path.join(self.state, "layers"))
        self.calls, self.t, self.wall = [], [0.0], [1_800_000_000.0]
        self.on_sleep = []
        self.kicked = []
        env = mock.patch.dict(os.environ, {"HUB_SHELL_KICK": json.dumps([sys.executable, "-c", "pass"])})
        env.start()
        self.addCleanup(env.stop)

    # the clocks: a sleep moves both; on_sleep — what happens meanwhile (the new app's core marking its start)
    def clock(self):
        return self.t[0]

    def now(self):
        return self.wall[0]

    def sleep(self, s):
        self.t[0] += s
        self.wall[0] += s
        for f in list(self.on_sleep):
            f()

    def comes_up(self, after=3):
        """The new app's core marks its start (core/appwatch.came_up) after this many sleeps."""
        left = [after]

        def tick():
            left[0] -= 1
            if left[0] == 0:
                base.write(os.path.join(self.state, "layers", "start.json"),
                           json.dumps({"name": "1.3.0-team", "ok": True, "at": self.wall[0]}))
        self.on_sleep.append(tick)

    def record(self):
        return self.sw.read(self.state)

    def shell_json(self, shell):
        base.write(os.path.join(self.state, "layers", "shell.json"),
                   json.dumps({"launch": os.path.join(shell, "launch.py"), "python": sys.executable}))

    def shell_files(self, hub, runtime, platform=None):
        os.makedirs(hub, exist_ok=True)
        for n in ("launch.py", "shellswap.py", "appcode.py"):
            shutil.copy2(os.path.join(PKG, n) if n != "appcode.py" else os.path.join(REPO, "core", n), hub)
        base.write(os.path.join(hub, "PACKAGED"), json.dumps({"format": "app", "layout": "layer", "version": "1.2.3",
                                                              "runtime": runtime, "platform": platform or self.PLATFORM}))


class MacSwap(Swap):
    def setUp(self):
        super().setUp()
        self.apps = os.path.join(self.root, "Applications")
        self.app = os.path.join(self.apps, "Agents Hub.app")
        self.make_app(self.app, 1)
        self.shell_json(os.path.join(self.app, "Contents", "Resources", "hub"))
        self.dmg = os.path.join(self.root, "image")   # the fake hdiutil "mounts" this folder
        self.make_app(os.path.join(self.dmg, "Agents Hub.app"), 2)

    def make_app(self, app, runtime, ident="com.hub-core.app", team=None, broken=False):
        import plistlib
        self.shell_files(os.path.join(app, "Contents", "Resources", "hub"), runtime)
        with open(os.path.join(app, "Contents", "Info.plist"), "wb") as f:
            plistlib.dump({"CFBundleIdentifier": ident}, f)
        if team:
            base.write(os.path.join(app, "TEAM"), team)
        if broken:
            base.write(os.path.join(app, "BROKEN-SEAL"), "")

    def fake(self, args, **kw):
        """hdiutil, ditto, codesign, xattr — as the Mac's, on folders."""
        self.calls.append(list(args))
        prog = args[0]
        out, code = "", 0
        if prog == "hdiutil" and args[1] == "attach":
            mount, src = args[args.index("-mountpoint") + 1], args[-1]
            shutil.copytree(src, mount, dirs_exist_ok=True)
        elif prog == "hdiutil":
            platforms.current.rmtree(args[2])
            os.makedirs(args[2], exist_ok=True)
        elif prog == "ditto":
            shutil.copytree(args[1], args[2], symlinks=True)
        elif prog == "codesign" and args[1] == "--verify":
            code = 1 if os.path.exists(os.path.join(args[-1], "BROKEN-SEAL")) else 0
            out = "a sealed resource is missing or invalid" if code else ""
        elif prog == "codesign":
            team = os.path.join(args[-1], "TEAM")
            out = f"TeamIdentifier={open(team).read() if os.path.exists(team) else 'not set'}"
        return subprocess.CompletedProcess(args, code, "", out)

    def runtime_at(self, app):
        with open(os.path.join(app, "Contents", "Resources", "hub", "PACKAGED")) as f:
            return json.load(f)["runtime"]

    def prepare(self):
        return self.sw.prepare(self.state, self.dmg, 2, run=self.fake, now=self.now)

    def swap(self, **kw):
        return self.sw.swap(self.state, run=self.fake, clock=self.clock, sleep=self.sleep, now=self.now, **kw)

    def test_prepare_lays_the_checked_app_next_to_the_installed_one(self):
        rec = self.prepare()
        new = os.path.join(self.apps, ".Agents Hub.app.new")
        self.assertEqual((rec["status"], rec["app"], rec["new"], rec["old"]), ("ready", self.app, new, self.app + ".old"))
        self.assertEqual(self.runtime_at(new), 2)
        self.assertEqual(self.runtime_at(self.app), 1)   # nothing in place yet
        verify = [c for c in self.calls if c[:2] == ["codesign", "--verify"]]
        self.assertEqual(verify, [["codesign", "--verify", "--deep", "--strict", new]])
        self.assertIn(["xattr", "-dr", "com.apple.quarantine", new], self.calls)
        self.assertEqual(rec["watcher"][1], os.path.join(self.app, "Contents", "Resources", "hub", "shellswap.py"))

    def test_a_broken_seal_another_bundle_a_foreign_team_or_too_old_an_app_is_refused(self):
        for kw, why in (({"broken": True}, "codesign"), ({"ident": "com.example.other"}, "bundle"),
                        ({"team": "OTHERTEAM1"}, "team"), ({"runtime": 1}, "environment")):
            with self.subTest(why):
                platforms.current.rmtree(self.dmg)
                self.make_app(os.path.join(self.dmg, "Agents Hub.app"), kw.pop("runtime", 2), **kw)
                if why == "team":
                    base.write(os.path.join(self.app, "TEAM"), "L87ZGZ3STE")
                with self.assertRaises(self.sw.Refused) as e:
                    self.prepare()
                self.assertIn(why, str(e.exception))
                self.assertFalse(os.path.exists(os.path.join(self.apps, ".Agents Hub.app.new")))
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(os.path.join(self.app, "TEAM"))

    def test_the_swap_puts_the_new_app_in_place_and_the_core_coming_up_ends_it(self):
        self.prepare()
        self.comes_up()
        self.assertEqual(self.swap(), "done")
        self.assertEqual(self.runtime_at(self.app), 2)
        self.assertEqual(self.runtime_at(self.app + ".old"), 1)
        rec = self.record()
        self.assertEqual(rec["status"], "done")
        self.assertTrue(rec["watcher"][1].startswith(self.app + ".old" + os.sep))   # a resumed watchdog runs the old one
        verify = [c for c in self.calls if c[:2] == ["codesign", "--verify"]]
        self.assertEqual(verify[-1], ["codesign", "--verify", "--deep", "--strict", self.app])   # in place, before it runs

    def test_a_new_app_whose_core_never_comes_goes_back_to_the_old_one(self):
        self.prepare()
        self.assertEqual(self.swap(), "returned")
        self.assertEqual(self.runtime_at(self.app), 1)
        self.assertEqual(self.runtime_at(self.app + ".bad"), 2)
        rec = self.record()
        self.assertEqual((rec["status"], rec["why"], rec["back"]), ("returned", "timeout", True))
        self.assertGreaterEqual(self.t[0], 180)   # its whole window

    def test_a_start_mark_from_before_the_swap_is_not_the_new_apps(self):
        self.prepare()
        base.write(os.path.join(self.state, "layers", "start.json"),
                   json.dumps({"name": "1.2.3-team", "ok": True, "at": self.wall[0] - 60}))
        self.assertEqual(self.swap(), "returned")

    def test_a_reboot_after_the_swap_resumes_the_watch_with_a_new_window(self):
        self.prepare()
        rec = self.record()
        os.replace(self.app, rec["old"])
        os.replace(rec["new"], self.app)
        self.sw.update(self.state, status="swapping", since=self.wall[0])
        self.comes_up()
        self.assertEqual(self.sw.swap(self.state, resume=True, run=self.fake, clock=self.clock, sleep=self.sleep,
                                      now=self.now), "done")

    def test_a_reboot_before_the_swap_leaves_the_old_app(self):
        self.prepare()
        self.sw.update(self.state, status="swapping", since=self.wall[0])
        self.assertEqual(self.sw.swap(self.state, resume=True, run=self.fake, clock=self.clock, sleep=self.sleep,
                                      now=self.now), "failed")
        self.assertEqual(self.runtime_at(self.app), 1)

    def test_the_launcher_starts_a_watchdog_of_a_record_without_one(self):
        self.prepare()
        started = []
        self.sw.update(self.state, status="swapped", since=self.wall[0], pid=None)
        self.assertTrue(self.sw.resume(self.state, run=started.append))
        self.assertEqual(started[0][-3:], ["swap", self.state, "--resume"])
        self.sw.update(self.state, status="ready")
        self.assertFalse(self.sw.resume(self.state, run=started.append))   # hub-up starts that one, at its restart

    def test_a_watchdog_late_for_hub_up_moves_nothing(self):
        self.prepare()
        t = [0.0]
        # hub-up waited its 120 s: it claims the replacement and comes up on the old app
        self.assertFalse(shellnext._swap(self.state, self.record(), lambda text: None, lambda cmd: None,
                                          lambda: t[0], lambda s: t.__setitem__(0, t[0] + s)))
        self.assertEqual(self.swap(), "failed")   # the watchdog started at last
        self.assertEqual(self.runtime_at(self.app), 1)
        self.assertTrue(os.path.isdir(os.path.join(self.apps, ".Agents Hub.app.new")))

    def test_hub_up_late_for_a_watchdog_that_has_begun_waits_for_it(self):
        self.prepare()
        self.assertTrue(self.sw.claim(self.state, "watchdog"))   # it claimed, then took long to write "swapping"
        t = [0.0]
        self.assertTrue(shellnext._swap(self.state, self.record(), lambda text: None, lambda cmd: None,
                                        lambda: t[0], lambda s: t.__setitem__(0, t[0] + s)))
        self.assertGreaterEqual(t[0], shellnext.WAIT_SWAP)   # then exits: the service takes what is at the path
        self.assertEqual(self.record()["status"], "ready")

    def test_a_live_number_without_a_fresh_beat_is_not_the_watchdog(self):
        self.prepare()
        started = []
        self.sw.update(self.state, status="swapped", since=self.wall[0], pid=os.getpid(), beat=self.wall[0] - 3600)
        self.assertTrue(self.sw.resume(self.state, run=started.append, now=self.now))   # a reboot gave its number away
        self.sw.update(self.state, beat=self.wall[0] - 5)
        self.assertFalse(self.sw.resume(self.state, run=started.append, now=self.now))   # it is watching
        self.assertEqual(len(started), 1)

    def test_a_record_that_is_not_ready_is_left_alone(self):
        self.assertEqual(self.swap(), "none")
        self.prepare()
        self.sw.update(self.state, status="done")
        self.assertEqual(self.swap(), "done")
        self.assertEqual(self.runtime_at(self.app), 1)


class WindowsSwap(Swap):
    PLATFORM = "windows-x64"

    def setUp(self):
        super().setUp()
        self.app = os.path.join(self.root, "Programs", "Agents Hub")
        self.shell_files(self.app, 1)
        os.makedirs(os.path.join(self.app, "runtime", "python"))
        base.write(os.path.join(self.app, "runtime", "python", "python.exe"), "the old python")
        self.shell_json(self.app)
        self.setup = os.path.join(self.root, "agents-hub-v1.3.0-windows-x64-setup.exe")
        with open(self.setup, "wb") as f:
            f.write(b"MZ setup")
        self.setup_does = "install"
        self.signature_says = "Valid\r\nCN=Example Org, O=Example Org, L=Somewhere, C=US\r\n"   # Get-AuthenticodeSignature

    def fake(self, args, **kw):
        """Setup.exe (installs, refuses or installs something that never starts), schtasks, powershell — recorded."""
        self.calls.append(list(args))
        code = 0
        if args[0] == "powershell":
            self.checked = kw["env"].get("HUB_SIGNED_FILE")
            return subprocess.CompletedProcess(args, 0, self.signature_says, "")
        if args[0] == self.setup:
            if self.setup_does == "refuse":   # PrepareToInstall: a process held the files — nothing changed
                code = 7
            else:
                base.write(os.path.join(self.app, "PACKAGED"), json.dumps({"format": "app", "runtime": 2,
                                                                           "platform": self.PLATFORM}))
                base.write(os.path.join(self.app, "runtime", "python", "python.exe"), "the new python")
        return subprocess.CompletedProcess(args, code, "", "")

    def swap(self, **kw):
        return self.sw.swap(self.state, run=self.fake, clock=self.clock, sleep=self.sleep, now=self.now,
                            alive=lambda pid: False, **kw)

    def prepare(self):
        return self.sw.prepare(self.state, self.setup, 2, run=self.fake, now=self.now)

    def runtime_at(self, folder):
        with open(os.path.join(folder, "PACKAGED")) as f:
            return json.load(f)["runtime"]

    def test_prepare_copies_the_installed_app_for_the_watchdog_and_the_way_back(self):
        rec = self.prepare()
        old = self.app + ".old"
        self.assertEqual(rec["watcher"], [os.path.join(old, "runtime", "python", "python.exe"),
                                          os.path.join(old, "shellswap.py")])
        self.assertEqual(self.runtime_at(old), 1)
        self.assertEqual(rec["window"], 300)   # a fresh Windows takes up to ~50 s to the core's first line (A0)

    def test_not_a_program_is_refused(self):
        with open(self.setup, "wb") as f:
            f.write(b"<html>")
        with self.assertRaises(self.sw.Refused):
            self.prepare()

    def signed_app(self):
        with open(os.path.join(self.app, "PACKAGED")) as f:
            mark = json.load(f)
        base.write(os.path.join(self.app, "PACKAGED"), json.dumps({**mark, "publisher": "Example Org"}))

    def test_a_signed_app_takes_a_setup_validly_signed_by_its_publisher(self):
        self.signed_app()
        self.assertEqual(self.prepare()["status"], "ready")
        self.assertEqual(self.checked, self.setup)   # Windows' own Get-AuthenticodeSignature on the downloaded file
        for says in ('Valid\r\nCN=Example Org, O="Example Org", C=US\r\n',   # quoted, as Windows writes a value with ,
                     "\ufeffValid\r\nCN=Example Org, O=Example Org\r\n"):       # a UTF-8 preamble before the first line
            self.signature_says = says
            self.assertEqual(self.prepare()["status"], "ready", says)

    def test_a_signed_app_refuses_a_setup_not_validly_signed_by_its_publisher(self):
        self.signed_app()
        for says in ("NotSigned\r\n\r\n", "HashMismatch\r\nCN=Example Org, O=Example Org\r\n",
                     "Valid\r\nCN=Example Org, O=Another Org\r\n", "Valid\r\nCN=Example Org\r\n", ""):
            self.signature_says = says
            with self.subTest(says), self.assertRaises(self.sw.Refused):
                self.prepare()
            self.assertEqual(self.sw.read(self.state).get("status"), None)   # nothing laid out for a swap

    def test_an_unsigned_app_does_not_ask_for_a_signature(self):
        self.signature_says = "NotSigned\r\n\r\n"   # an app built without the signing settings (any but the team's)
        self.assertEqual(self.prepare()["status"], "ready")
        self.assertFalse([c for c in self.calls if c[0] == "powershell"])

    def test_setup_installs_and_the_core_coming_up_ends_it(self):
        self.prepare()
        with open(os.path.join(self.state, "stopped"), "w"):
            pass   # hub-up's mark for the scheduler's repeat
        self.comes_up()
        self.assertEqual(self.swap(), "done")
        (setup,) = [c for c in self.calls if c[0] == self.setup]
        self.assertEqual(setup[1:4], ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"])
        self.assertEqual(self.runtime_at(self.app), 2)
        # the watchdog starts the task itself (the postinstall may not have): an orphaned instance ended first
        self.assertEqual([c[1] for c in self.calls if c[0] == "schtasks"], ["/End", "/Run"])
        self.assertFalse(os.path.exists(os.path.join(self.state, "stopped")))

    def test_a_hub_the_postinstall_started_is_not_ended(self):
        self.prepare()
        base.write(os.path.join(self.state, "hub-up.json"), json.dumps({"pid": 4242}))
        self.comes_up()
        gone = [False]

        def alive(pid):   # the old hub-up is gone before Setup; the postinstall's hub-up (the same number here) runs after
            return gone[0] and pid == 4242
        real = self.fake

        def fake(args, **kw):
            got = real(args, **kw)
            if args[0] == self.setup:
                gone[0] = True
            return got
        self.assertEqual(self.sw.swap(self.state, run=fake, clock=self.clock, sleep=self.sleep, now=self.now,
                                      alive=alive), "done")
        self.assertEqual([c[1] for c in self.calls if c[0] == "schtasks"], ["/Run"])

    def test_a_setup_that_changed_nothing_starts_the_old_hub_again(self):
        self.prepare()
        with open(os.path.join(self.state, "stopped"), "w"):
            pass   # hub-up's mark for the scheduler's repeat
        self.setup_does = "refuse"
        self.assertEqual(self.swap(), "failed")
        self.assertEqual(self.runtime_at(self.app), 1)
        tasks = [c[1] for c in self.calls if c[0] == "schtasks"]
        self.assertEqual(tasks, ["/End", "/Run"])   # an orphaned instance ended first (A0: IgnoreNew)
        self.assertFalse(os.path.exists(os.path.join(self.state, "stopped")))

    def test_a_new_app_that_never_brings_the_core_up_is_replaced_by_the_copy(self):
        self.prepare()
        self.assertEqual(self.swap(), "returned")
        self.assertEqual(self.runtime_at(self.app), 1)
        with open(os.path.join(self.app, "runtime", "python", "python.exe")) as f:
            self.assertEqual(f.read(), "the old python")
        self.assertEqual(self.runtime_at(self.app + ".bad"), 2)
        self.assertTrue(any(c[0] == "powershell.exe" for c in self.calls))   # the new app's processes ended first
        self.assertEqual([c[1] for c in self.calls if c[0] == "schtasks"][-2:], ["/End", "/Run"])
        self.assertFalse(os.path.exists(os.path.join(self.state, "stopped")))

    def test_a_copy_that_cannot_come_back_still_starts_the_hub(self):
        self.prepare()
        real = os.replace

        def held(a, b):
            if a == self.app:
                raise PermissionError(32, "in use", a)
            return real(a, b)
        with mock.patch.object(os, "replace", held):
            self.assertEqual(self.swap(), "returned")
        rec = self.record()
        self.assertIs(rec["back"], False)
        self.assertFalse(os.path.exists(os.path.join(self.state, "stopped")))   # held for good, nobody would start it
        self.assertEqual([c[1] for c in self.calls if c[0] == "schtasks"][-1], "/Run")

    def test_a_reboot_in_the_middle_of_setup_brings_the_copy_back(self):
        self.prepare()
        self.sw.update(self.state, status="swapping", since=self.wall[0])
        base.write(os.path.join(self.app, "half"), "a Setup cut off")
        self.assertEqual(self.sw.swap(self.state, resume=True, run=self.fake, clock=self.clock, sleep=self.sleep,
                                      now=self.now, alive=lambda pid: False), "returned")
        self.assertFalse(os.path.exists(os.path.join(self.app, "half")))


class Factory(unittest.TestCase):
    def test_the_shell_carries_the_swapper_and_the_names_agree(self):
        with open(os.path.join(PKG, "build.py"), encoding="utf-8") as f:
            self.assertIn('"shellswap.py"', f.read())
        sw = shellswap()
        self.assertEqual((sw.RECORD, sw.HELD, sw.CLAIM), (shellnext.RECORD, shellnext.HELD, shellnext.CLAIM))
        from core import restart
        self.assertEqual(sw.HELD, restart.HELD)
        with open(os.path.join(REPO, "deploy", "mac", "app.py"), encoding="utf-8") as f:
            self.assertIn(f'APP_ID = "{sw.BUNDLE_ID}"', f.read())

    def test_the_setup_waits_for_the_apps_processes_and_lets_no_restart_manager_close_them(self):
        with open(os.path.join(PKG, "windows", "agents-hub.iss"), encoding="utf-8-sig") as f:
            iss = f.read()
        self.assertRegex(iss, r"(?m)^CloseApplications=no$")
        prep = iss[iss.index("function PrepareToInstall"):iss.index("procedure CurStepChanged")]
        self.assertIn("AppBusy()", prep)
        self.assertIn("CustomMessage('Held')", prep)
        foreign = iss[iss.index("function ForeignHub"):iss.index("function InitializeSetup")]
        self.assertIn("\\layers\\shell.json", foreign)   # an app on a code layer is the app's own, not a foreign hub


if __name__ == "__main__":
    unittest.main()
