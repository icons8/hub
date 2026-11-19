"""Chrome was updated on disk, but the hub's agent browser kept running on the old process for days: new tabs died
(the profile log said "No rendezvous client, terminating process"), sessions got ERR_ABORTED and timeouts on navigate
and "Requesting main frame too early" on new_page. `hub restart browser` did not help: it restarted the watchdog while
the old Chrome stayed. Now the watchdog compares the version of the running Chrome (CDP /json/version) with the app on
disk and, if the disk one is newer, restarts its own Chrome (once sessions using it are free, but no longer than
UPGRADE_WAIT), then browser.reset; `hub restart browser` also kills a Chrome of its profile that is already running."""
import tests  # noqa: F401 — ignore this machine's settings
import json
import os
import plistlib
import shutil
import tempfile
import unittest

import platforms
from modules.browser import browsers, run


class Proc:
    pid = 4242

    def __init__(self):
        self.alive = True

    def poll(self):
        return None if self.alive else 0


class Base(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="brup-")
        self.addCleanup(shutil.rmtree, self.state)
        self.now, self.up, self.disk = [0.0], ["HeadlessChrome/154.0.8037.93"], ["154.0.8037.98"]
        self.launched, self.killed, self.killed_pids, self.elsewhere = [], [], [], [None]
        self.sup = run.Supervisor(self.state, clock=lambda: self.now[0], probe=lambda url: self.up[0])
        self.sup.reap = lambda url, now: False
        self.sup.disk_version = lambda cfg, b=None: self.disk[0]
        self.sup.launch = lambda cfg, state_dir, name, b: self.launched.append(Proc()) or self.launched[-1]
        self.sup.kill_tree = lambda proc: self.killed.append(proc) or setattr(proc, "alive", False)
        self.sup.kill_pid = lambda pid: self.killed_pids.append(pid)
        self.sup.running = lambda profile: self.elsewhere[0]
        self.b = {"kind": "local", "url": "http://127.0.0.1:9", "port": 9, "risk": []}

    def at(self, t):
        self.now[0] = t
        return self.sup.check({}, "mac", self.b)

    def resets(self):
        d = os.path.join(self.state, "spool")
        out = []
        for n in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            with open(os.path.join(d, n)) as f:
                ev = json.load(f)
            if ev["type"] == "browser.reset":
                out.append((ev["entity"], ev["data"]["why"]))
        return out

    def sessions(self, **states):
        with open(os.path.join(self.state, "state.json"), "w") as f:
            json.dump({"seq": 1, "state": {"sessions": {
                n: {"state": s, "tools": ["browser:mac"]} for n, s in states.items()}}}, f)


class ChromeUpdatedOnDisk(Base):
    def test_a_newer_chrome_on_disk_restarts_our_chrome_and_resets_sessions(self):
        self.elsewhere[0] = 777                       # started by the watchdog's previous life — killed by pid
        st = self.at(0)
        self.assertEqual(self.killed_pids, [777])
        self.assertIn("Chrome was updated", st["problem"])
        self.assertEqual(self.resets(), [("mac", "Chrome was updated: 154.0.8037.93 → 154.0.8037.98")])
        self.at(5)                                     # the old one still answers — do not kill a second time
        self.assertEqual(self.killed_pids, [777])

    def test_our_own_process_is_killed_whole(self):
        self.up[0] = None
        self.at(0)                                     # the watchdog started Chrome itself
        self.up[0] = "HeadlessChrome/154.0.8037.93"
        self.at(10)
        self.assertEqual((len(self.killed), self.killed_pids), (1, []))

    def test_same_version_or_unknown_disk_version_leaves_it_alone(self):
        for disk in ("154.0.8037.93", "153.0.1.1", None):
            self.disk[0] = disk
            st = self.at(0)
            self.assertTrue(st["up"])
        self.elsewhere[0] = 777
        self.assertEqual((self.killed, self.killed_pids, self.resets()), ([], [], []))

    def test_waits_for_busy_sessions_but_not_forever(self):
        self.elsewhere[0] = 777
        self.sessions(a="busy", b="idle")
        self.assertTrue(self.at(0)["up"])
        self.assertTrue(self.at(60)["up"])
        self.assertEqual(self.killed_pids, [])
        self.sessions(a="idle", b="idle")
        self.at(120)
        self.assertEqual(self.killed_pids, [777])

    def test_a_session_busy_for_long_does_not_hold_the_broken_chrome(self):
        self.elsewhere[0] = 777
        self.sessions(a="busy")
        self.at(0)
        self.at(run.UPGRADE_WAIT - 1)
        self.assertEqual(self.killed_pids, [])
        self.at(run.UPGRADE_WAIT + 1)
        self.assertEqual(self.killed_pids, [777])


class TwoBuilds(Base):
    """Two local browsers on two Chromium builds: an update of one build restarts only the browser on that build (the
    neighbour test DiskVersion.test_two_builds_… checks the other side: no restarts while both are current)."""

    def setUp(self):
        super().setUp()
        del self.sup.disk_version                                  # the real one, with its cache
        self.versions = {"/bin/chrome": "154.0.8037.98", "/opt/other-chromium/chrome": "156.0.1.1"}
        orig = browsers.disk_version
        browsers.disk_version = lambda exe, **kw: self.versions[exe]
        self.addCleanup(setattr, browsers, "disk_version", orig)
        self.cfg = {"browser": {"chrome": "/bin/chrome"}}
        self.other = {**self.b, "chrome": "/opt/other-chromium/chrome"}

    def test_only_the_browser_whose_own_build_was_updated_is_restarted(self):
        self.elsewhere[0] = 777
        self.versions["/opt/other-chromium/chrome"] = "156.0.2.2"
        self.assertIsNone(self.sup.renew(self.cfg, "mac", self.b, "HeadlessChrome/154.0.8037.98"))
        self.assertIsNotNone(self.sup.renew(self.cfg, "other", self.other, "HeadlessChrome/156.0.1.1"))
        self.assertEqual([r[0] for r in self.resets()], ["other"])


class RestartAsked(Base):
    def test_hub_restart_browser_kills_the_running_chrome_of_our_profile(self):
        self.elsewhere[0] = 777
        self.sup.fresh({"browsers": {"mac": {"kind": "local", "port": 9}, "far": {"kind": "remote",
                                                                                 "url": "http://h:1"}}})
        self.assertEqual(self.killed_pids, [777])     # the remote one is not ours — leave it alone
        self.assertEqual(self.resets(), [("mac", "hub restart browser")])

    def test_nothing_running_nothing_killed(self):
        self.sup.fresh({"browsers": {"mac": {"kind": "local", "port": 9}}})
        self.assertEqual((self.killed_pids, self.resets()), ([], []))


class DiskVersion(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "a macOS app: the .app/Contents/MacOS path")
    def test_mac_app_reads_info_plist(self):
        d = tempfile.mkdtemp(prefix="app-")
        self.addCleanup(shutil.rmtree, d)
        exe = os.path.join(d, "Google Chrome.app", "Contents", "MacOS", "Google Chrome")
        os.makedirs(os.path.dirname(exe))
        open(exe, "w").close()
        with open(os.path.join(d, "Google Chrome.app", "Contents", "Info.plist"), "wb") as f:
            plistlib.dump({"CFBundleShortVersionString": "154.0.8037.98"}, f)
        self.assertEqual(browsers.disk_version(exe), "154.0.8037.98")

    @unittest.skipIf(os.name == "nt", "a macOS app: the .app/Contents/MacOS path")
    def test_an_info_plist_that_is_not_well_formed_xml_is_an_unknown_version(self):
        d = tempfile.mkdtemp(prefix="app-")
        self.addCleanup(shutil.rmtree, d)
        exe = os.path.join(d, "Google Chrome.app", "Contents", "MacOS", "Google Chrome")
        os.makedirs(os.path.dirname(exe))
        open(exe, "w").close()
        with open(os.path.join(d, "Google Chrome.app", "Contents", "Info.plist"), "w") as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict><key>CFBundleName</key>'
                    '<string>a & b</string></dict></plist>\n')
        self.assertIsNone(browsers.disk_version(exe))

    def test_windows_install_reads_the_version_folder(self):
        d = tempfile.mkdtemp(prefix="win-")
        self.addCleanup(shutil.rmtree, d)
        exe = os.path.join(d, "chrome.exe")
        open(exe, "w").close()
        for v in ("153.0.1.2", "154.0.8037.98", "SetupMetrics"):
            os.makedirs(os.path.join(d, v))
        self.assertEqual(browsers.disk_version(exe, run=lambda cmd: ""), "154.0.8037.98")

    def test_other_systems_ask_the_binary(self):
        self.assertEqual(browsers.disk_version("/home/x/chrome", run=lambda cmd: "Google Chrome 154.0.8037.98 \n"),
                         "154.0.8037.98")
        self.assertIsNone(browsers.disk_version("/home/x/chrome", run=lambda cmd: ""))

    def test_the_supervisor_reads_the_version_of_the_browsers_own_binary(self):
        state = tempfile.mkdtemp(prefix="brup-")
        self.addCleanup(platforms.current.rmtree, state)
        sup = run.Supervisor(state)
        asked = []
        orig = browsers.disk_version
        browsers.disk_version = lambda exe, **kw: asked.append(exe) or {"/bin/a": "1.0", "/bin/b": "2.0"}[exe]
        self.addCleanup(setattr, browsers, "disk_version", orig)
        cfg = {"browser": {"chrome": "/bin/a"}}
        self.assertEqual(sup.disk_version(cfg), "1.0")                        # as before: browser.chrome
        self.assertEqual(sup.disk_version(cfg, {"chrome": "/bin/b"}), "2.0")  # a browser with its own Chromium
        self.assertEqual(sup.disk_version(cfg), "1.0")                        # one binary does not shadow the other
        self.assertEqual(asked, ["/bin/a", "/bin/b"])                         # and each is looked up once per interval

    def test_two_builds_do_not_restart_each_other_in_a_loop(self):
        # two local browsers on two Chromium builds, each running its own build's version: neither is "older than the
        # Chrome on disk", whichever is checked first — one shared disk version would restart one of them on every check
        state = tempfile.mkdtemp(prefix="brup-")
        self.addCleanup(platforms.current.rmtree, state)
        now = [0.0]
        sup = run.Supervisor(state, clock=lambda: now[0])
        sup.running, sup.kill_pid, sup.kill_tree = (lambda profile: None), (lambda pid: None), (lambda proc: None)
        orig = browsers.disk_version
        browsers.disk_version = lambda exe, **kw: {"/bin/a": "154.0.8037.98", "/bin/b": "150.0.7000.10"}[exe]
        self.addCleanup(setattr, browsers, "disk_version", orig)
        cfg = {"browser": {"chrome": "/bin/a"}}
        a = {"kind": "local", "port": 9101, "profile": os.path.join(state, "a")}
        b = {"kind": "local", "port": 9102, "profile": os.path.join(state, "b"), "chrome": "/bin/b"}
        for t in range(0, 4 * run.DISK_EVERY, run.DISK_EVERY // 2):
            now[0] = t
            for name, br, running in (("b", b, "Chrome/150.0.7000.10"), ("a", a, "HeadlessChrome/154.0.8037.98")):
                self.assertIsNone(sup.renew(cfg, name, br, running), (t, name))
        self.assertFalse(os.path.isdir(os.path.join(state, "spool")) and os.listdir(os.path.join(state, "spool")))

    def test_newer(self):
        self.assertTrue(browsers.newer("154.0.8037.98", "HeadlessChrome/154.0.8037.93"))
        self.assertTrue(browsers.newer("155.0.1.0", "Chrome/154.0.8037.93"))
        self.assertFalse(browsers.newer("154.0.8037.93", "Chrome/154.0.8037.93"))
        self.assertFalse(browsers.newer(None, "Chrome/154.0.8037.93"))
        self.assertFalse(browsers.newer("154.0.8037.98", "?"))


if __name__ == "__main__":
    unittest.main()
