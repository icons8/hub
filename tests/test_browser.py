"""The browser module: local (the core starts and keeps it) and remote (any CDP), catalog tool browser:<name>,
the supervisor tells the core "responds / does not respond", the MCP launcher waits for the browser. Chrome and MCP are fakes."""
import tests  # noqa: F401 — do not see the machine's settings (~/.config/hub-core), see tests/__init__.py
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from unittest import mock

import platforms
from tests import winsafe
from core import catalog, i18n, reducer, spool, views
from modules.browser import browsers, run
from modules.browser.run import Supervisor

HERE = os.path.dirname(__file__)
FAKE_CHROME = os.path.join(HERE, "fakes", "fake_chrome.py")
FAKE_MCP = os.path.join(HERE, "fakes", "fake_mcp.py")
FAKE_MCP_STDIO = os.path.join(HERE, "fakes", "fake_mcp_stdio.py")
HUB_BROWSER = os.path.join(HERE, "..", "bin", "hub-browser")
ROOT = os.path.abspath(os.path.join(HERE, ".."))
OK = {"content": [{"type": "text", "text": "ok"}]}   # the fake MCP's reply to a call with nothing to report


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Config(unittest.TestCase):
    def test_configured_defaults_and_problems(self):
        conf = browsers.configured({"browsers": {
            "vm": {"kind": "local"}, "mac": {"kind": "remote", "url": "http://127.0.0.1:9223", "risk": ["owner-data"]},
            "cloud": {"kind": "remote", "url": "wss://cdp.example/x?token=t"}, "bad": {"kind": "remote", "url": "ftp://x"},
            "odd": {}}})
        self.assertEqual(conf["vm"]["url"], "http://127.0.0.1:9230")
        self.assertNotIn("problem", conf["cloud"])
        self.assertIn("url", conf["bad"]["problem"])
        self.assertIn("kind", conf["odd"]["problem"])
        self.assertEqual(conf["vm"]["start"], "always")                       # background — keep it always
        shown = browsers.configured({"browsers": {"own": {"kind": "local", "headless": False},
                                                  "bad": {"kind": "local", "start": "never"}}})
        self.assertEqual(shown["own"]["start"], "always")                     # the default for everyone — as before
        self.assertEqual(shown["own"]["open"], "about:blank")                 # we do not open the hub page ourselves
        mine = browsers.configured({"browsers": {"own": {"kind": "local", "headless": False, "start": "login",
                                                         "open": "http://127.0.0.1:8787"}}})["own"]
        self.assertEqual(browsers.chrome_command({"browser": {"chrome": "c"}}, "/st", "own", mine)[-1],
                         "http://127.0.0.1:8787")                             # own window — via a setting
        self.assertIn("start", shown["bad"]["problem"])
        tools = catalog.build({**catalog.DEFAULTS, "browsers": {"vm": {"kind": "local"},
                                                                "mac": {"kind": "remote", "url": "http://h:1", "risk": ["owner-data"]},
                                                                "bad": {"kind": "x"}}})
        self.assertEqual(sorted(t for t in tools if t.startswith("browser:")), ["browser:mac", "browser:vm"])
        self.assertEqual(tools["browser:mac"]["risk"], ["owner-data"])
        self.assertEqual(tools["browser:vm"]["kind"], "mcp")
        self.assertEqual(tools["browser:vm"]["server"]["args"][-2:], ["mcp", "vm"])

    def test_a_new_profile_gets_a_colour_theme_an_existing_one_does_not(self):
        # one Chrome.app, one dock icon: the agents' window is told from the person's own by its colour
        with tempfile.TemporaryDirectory() as tmp:
            profile = os.path.join(tmp, "browsers", "own")
            self.assertTrue(browsers.seed_profile(profile))
            with open(os.path.join(profile, "Default", "Preferences"), encoding="utf-8") as f:
                prefs = json.load(f)
            self.assertEqual(prefs["extensions"]["theme"]["id"], "user_color_theme_id")
            self.assertEqual(prefs["browser"]["theme"]["user_color2"], browsers.THEME_COLOR)
            self.assertNotIn("session", prefs)                                    # Chrome resets that one on macOS and Windows
            self.assertTrue(browsers.seeded(profile))
            self.assertTrue(browsers.seed_profile(os.path.join(tmp, "plain"), window=False))
            self.assertEqual(os.listdir(os.path.join(tmp, "plain")), [browsers.SEEDED])   # headless: the sign only, no colour
            prefs["browser"]["theme"]["user_color2"] = 5                           # the person chose another colour
            with open(os.path.join(profile, "Default", "Preferences"), "w", encoding="utf-8") as f:
                json.dump(prefs, f)
            self.assertFalse(browsers.seed_profile(profile))                      # a second start leaves it alone
            with open(os.path.join(profile, "Default", "Preferences"), encoding="utf-8") as f:
                self.assertEqual(json.load(f)["browser"]["theme"]["user_color2"], 5)
            own = os.path.join(tmp, "own-chrome")                                 # a profile of a Chrome that already ran
            os.makedirs(os.path.join(own, "Default"))
            with open(os.path.join(own, "Default", "Preferences"), "w", encoding="utf-8") as f:
                f.write("{}")
            self.assertFalse(browsers.seed_profile(own))
            self.assertFalse(browsers.seeded(own))
            with open(os.path.join(own, "Default", "Preferences"), encoding="utf-8") as f:
                self.assertEqual(f.read(), "{}")
            other = os.path.join(tmp, "other-chrome")                             # data under another profile name
            os.makedirs(os.path.join(other, "Profile 1"))
            self.assertFalse(browsers.seed_profile(other))
            self.assertEqual(os.listdir(other), ["Profile 1"])
            self.assertEqual(os.listdir(os.path.join(profile, "Default")), ["Preferences"])   # no half-written leftover

    def test_start_local_seeds_the_profile_before_chrome_starts(self):
        def started(**setting):
            """What the profile holds and whether Chrome restores the last session, at the moment Chrome is started."""
            with tempfile.TemporaryDirectory() as tmp:
                os.makedirs(os.path.join(tmp, "browsers"))
                seen = []
                with mock.patch.object(browsers, "screenless", lambda cfg: False):   # whatever machine runs the test
                    b = browsers.configured({"browsers": {"own": {"kind": "local", "port": 1, **setting}}})["own"]
                profile = os.path.join(tmp, "browsers", "own")
                with mock.patch.object(browsers, "find_chrome", lambda cfg, b: "/bin/chrome"), \
                        mock.patch.object(platforms.current, "popen_detached",
                                          lambda cmd, **kw: seen.append((sorted(os.listdir(profile)),
                                                                         "--restore-last-session" in cmd))):
                    run.start_local({}, tmp, "own", b)
                return seen[0]
        self.assertEqual(started(headless=False), (sorted([browsers.SEEDED, "Default"]), True))
        self.assertEqual(started(), ([browsers.SEEDED], True))                    # headless: no window, no colour to tell
        self.assertEqual(started(headless=False, args=["--profile-directory=Profile 2"]), ([], False))   # another folder

    def test_restore_last_session_only_for_a_profile_the_module_made(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"browser": {"chrome": "/bin/chrome"}}
            b = browsers.configured({"browsers": {"own": {"kind": "local", "port": 1}}})["own"]
            self.assertNotIn("--restore-last-session", browsers.chrome_command(cfg, tmp, "own", b))   # not made by us: as before
            browsers.seed_profile(browsers.profile_dir(tmp, "own", b), window=False)
            cmd = browsers.chrome_command(cfg, tmp, "own", b)
            self.assertIn("--restore-last-session", cmd)
            self.assertEqual(cmd[-1], "about:blank")                              # nothing to restore yet: the start page
            profile = browsers.profile_dir(tmp, "own", b)
            os.makedirs(os.path.join(profile, "Default", "Sessions"))
            with open(os.path.join(profile, "Default", "Sessions", "Session_1"), "wb") as f:
                f.write(b"x")
            with open(os.path.join(profile, "Default", "Preferences"), "w", encoding="utf-8") as f:
                json.dump({"profile": {"exit_type": "SessionEnded"}}, f)
            cmd = browsers.chrome_command(cfg, tmp, "own", b)
            self.assertIn("--restore-last-session", cmd)
            self.assertEqual(cmd[-1], "--restore-last-session")                   # the tabs come back, no extra start tab
            asked = browsers.configured({"browsers": {"own": {"kind": "local", "port": 1, "open": "http://127.0.0.1:8787/"}}})["own"]
            self.assertEqual(browsers.chrome_command(cfg, tmp, "own", asked)[-1], "http://127.0.0.1:8787/")   # asked for: stays
            with open(os.path.join(profile, "Default", "Preferences"), "w", encoding="utf-8") as f:
                json.dump({"profile": {"exit_type": "Crashed"}}, f)               # a crash restores nothing by itself
            self.assertEqual(browsers.chrome_command(cfg, tmp, "own", b)[-1], "about:blank")
            os.remove(os.path.join(profile, browsers.SEEDED))                     # not ours: never
            self.assertNotIn("--restore-last-session", browsers.chrome_command(cfg, tmp, "own", b))

    def test_chrome_and_mcp_commands(self):
        b = browsers.configured({"browsers": {"vm": {"kind": "local", "port": 9555}}})["vm"]
        cmd = browsers.chrome_command({"browser": {"chrome": "/bin/chrome"}}, "/st", "vm", b)
        self.assertEqual(cmd[0], "/bin/chrome")
        for flag in ("--user-data-dir=" + os.path.join("/st", "browsers", "vm"), "--remote-debugging-port=9555",
                     "--remote-debugging-address=127.0.0.1", "--headless=new"):
            self.assertIn(flag, cmd)
        self.assertNotIn("--headless=new", browsers.chrome_command({"browser": {"chrome": "c"}}, "/st", "vm", b, headless=False))
        own = browsers.configured({"browsers": {"own": {"kind": "local", "chrome": "/opt/chromium", "profile": "/p"}}})["own"]
        cmd = browsers.chrome_command({"browser": {"chrome": "/bin/chrome"}}, "/st", "own", own)
        self.assertEqual(cmd[0], "/opt/chromium")                                 # its own Chromium for one browser
        self.assertIn("--user-data-dir=/p", cmd)
        self.assertEqual(browsers.find_chrome({"browser": {"chrome": "/bin/chrome"}}, b), "/bin/chrome")  # the rest — as before
        for bad in (["/opt/chromium"], 123, ""):   # not a path: the browser shows a problem, the supervisor goes on
            got = browsers.configured({"browsers": {"own": {"kind": "local", "chrome": bad}}})["own"]
            self.assertEqual(got.get("problem"), i18n.t("cli.browser.bad_chrome"), bad)
        mcp = browsers.mcp_command({}, b)
        self.assertEqual(mcp[:3], ["chrome-devtools-mcp", "--browserUrl", "http://127.0.0.1:9555"])
        self.assertNotIn("--autoConnect", mcp)  # the owner's main Chrome — never
        ws = browsers.mcp_command({"browser": {"mcp": ["x"]}}, {"url": "wss://c/x"})
        self.assertEqual(ws[:3], ["x", "--wsEndpoint", "wss://c/x"])

    def test_remote_headers_go_through_a_private_file_not_the_command_line(self):
        # the headers of a remote browser carry its key: on chrome-devtools-mcp's command line (--wsHeaders) every user
        # of the machine read it in `ps`. They go in its --config file (1.10+), 0600 in the hub's private folder; the
        # endpoint stays on the command line too: an older MCP that does not know --config connects without headers and
        # gets refused by the remote, instead of starting a Chrome of its own
        d = tempfile.mkdtemp(prefix="ws-headers-")
        self.addCleanup(platforms.current.rmtree, d)
        b = {"url": "wss://c/x", "headers": {"Authorization": "Bearer k3y-probe"}}
        ws = browsers.mcp_command({"browser": {"mcp": ["x"]}}, b, private_dir=os.path.join(d, "mcp"))
        self.assertNotIn("k3y-probe", " ".join(ws))
        self.assertEqual(ws[:5], ["x", "--wsEndpoint", "wss://c/x", "--config", ws[4]])
        with open(ws[4], encoding="utf-8") as f:
            conf = json.load(f)
        # the tool checks its config file on its own: wsHeaders needs the endpoint in the same file
        self.assertEqual(conf, {"wsEndpoint": "wss://c/x", "wsHeaders": '{"Authorization": "Bearer k3y-probe"}'})
        if os.name == "posix":
            self.assertEqual(os.stat(ws[4]).st_mode & 0o077, 0)
            self.assertEqual(os.stat(os.path.dirname(ws[4])).st_mode & 0o077, 0)
        with self.assertRaises(ValueError):   # no private folder: the headers are not put on the command line instead
            browsers.mcp_command({"browser": {"mcp": ["x"]}}, b)

    def test_npx_does_not_read_the_session_package_json(self):
        # 20–21.10: browser:mac never connected for sessions in one repo (8 bring-ups, "Connection closed"):
        # npx reads the session folder's package.json, and npm fails there ("Invalid comparator" on the npm: alias)
        # before the MCP starts. --prefix of an empty folder steers npx away from the project; the MCP's working
        # folder stays the same (relative snapshot paths)
        b = {"url": "http://127.0.0.1:9555"}
        for npx in ("npx", "/usr/bin/npx", r"C:\node\npx.cmd"):
            cmd = browsers.mcp_command({"browser": {"mcp": [npx, "-y", "chrome-devtools-mcp@latest"]}}, b, npm_dir="/st/n")
            self.assertEqual(cmd[:5], [npx, "--prefix", "/st/n", "-y", "chrome-devtools-mcp@latest"])
        own = ["npx", "--prefix=/own", "-y", "m"]   # a --prefix of their own in the setting — leave it alone
        self.assertEqual(browsers.mcp_command({"browser": {"mcp": own}}, b, npm_dir="/st/n")[:4], own)
        self.assertEqual(browsers.mcp_command({}, b, npm_dir="/st/n")[:2], ["chrome-devtools-mcp", "--browserUrl"])
        self.assertEqual(browsers.mcp_command({"browser": {"mcp": ["npx", "m"]}}, b)[:2], ["npx", "m"])

    def test_mcp_usage_statistics_off_by_default(self):
        # 27.10: chrome-devtools-mcp sends usage statistics to Google by default and keeps a separate telemetry watchdog
        # process per server (~25 MB; 35 sessions: 0.8 GB). Data leaves the machine only on the owner's setting
        key = "CHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS"
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(key, None)
            self.assertEqual(browsers.env({}).get(key), "1")
            self.assertEqual(browsers.env({"browser": {"env": {"PATH": "/x"}}}).get(key), "1")
            self.assertNotIn(key, browsers.env({"browser": {"usage_statistics": True}}))
            self.assertEqual(browsers.env({"browser": {"usage_statistics": True, "env": {key: "1"}}}).get(key), "1")

    def test_mcp_may_write_snapshots_to_tmp_and_configured_dirs(self):
        # 16.10: 33 refusals "not within any of the configured workspace roots" — session snapshots in /tmp and in a
        # folder outside their cwd; chrome-devtools-mcp only allows the client's roots (the session folder) and its own TMPDIR
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        b = {"url": "http://127.0.0.1:9555"}

        def dirs(cmd):
            return [cmd[i + 1] for i, a in enumerate(cmd) if a == "--workspace"]
        with mock.patch.object(browsers, "WORKSPACE", [tmp]):
            self.assertEqual(dirs(browsers.mcp_command({}, b)), [tmp])
        cmd = browsers.mcp_command({"browser": {"workspace": [tmp, tmp + os.sep, os.path.join(tmp, "nope"), "."]}}, b)
        self.assertEqual(dirs(cmd), [tmp])  # no such folder (/tmp on Windows) or a relative path — no flag;
        # a repeat — once
        self.assertEqual(dirs(browsers.mcp_command({"browser": {"workspace": tmp}}, b)), [tmp])  # a string — not letters
        self.assertEqual(cmd[-2:], ["--viewport", "1440x900"])
        self.assertEqual(dirs(browsers.mcp_command({"browser": {"workspace": []}}, b)), [])  # turned off by the setting
        # the former workaround — the MCP does not accept --workspace with it ("mutually exclusive")
        free = {"browser": {"mcp": ["x", "--allow-unrestricted-paths"], "workspace": [tmp]}}
        self.assertEqual(dirs(browsers.mcp_command(free, b)), [])
        home = os.path.expanduser("~")
        self.assertEqual(dirs(browsers.mcp_command({"browser": {"workspace": ["~"]}}, b)), [home])


class FindChrome(unittest.TestCase):
    def test_finds_app_nested_a_level_deeper_in_applications(self):
        # A DMG dragged into /Applications whole (not just the .app) — 12.10: CONNECTION_CLOSED on
        # browser:mac, Chrome was in /Applications/Google Chrome/Google Chrome.app/…, not directly in /Applications/
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        nested = os.path.join(tmp, "Google Chrome", "Google Chrome.app", "Contents", "MacOS")
        os.makedirs(nested)
        exe = os.path.join(nested, "Google Chrome")
        open(exe, "w").close()
        candidate = os.path.join(tmp, "Google Chrome.app", "Contents", "MacOS", "Google Chrome")
        with mock.patch.object(browsers, "CHROME_CANDIDATES", [candidate]):
            self.assertEqual(browsers.find_chrome({}), exe)

    def test_top_level_still_wins_over_nested(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        top = os.path.join(tmp, "Google Chrome.app", "Contents", "MacOS")
        os.makedirs(top)
        exe = os.path.join(top, "Google Chrome")
        open(exe, "w").close()
        nested = os.path.join(tmp, "Google Chrome", "Google Chrome.app", "Contents", "MacOS")
        os.makedirs(nested)
        open(os.path.join(nested, "Google Chrome"), "w").close()
        with mock.patch.object(browsers, "CHROME_CANDIDATES", [exe]):
            self.assertEqual(browsers.find_chrome({}), exe)


@unittest.skipIf(os.name == "nt", "fake Chrome is a .py instead of a program: Windows cannot launch it so")
class SupervisorTest(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hubbrowser-")
        self.port, self.rport = free_port(), free_port()
        self.remote = None
        self.write_config({"vm": {"kind": "local", "port": self.port},
                           "far": {"kind": "remote", "url": f"http://127.0.0.1:{self.rport}"}})
        self.old = os.environ.get("HUB_CONFIG")
        os.environ["HUB_CONFIG"] = os.path.join(self.state, "hub.json")
        self.sup = Supervisor(self.state)

    def tearDown(self):
        for p in list(self.sup.procs.values()) + ([self.remote] if self.remote else []):
            p.kill()
            p.wait()
        shutil.rmtree(self.state, ignore_errors=True)
        os.environ["HUB_CONFIG"] = self.old or ""
        if self.old is None:
            os.environ.pop("HUB_CONFIG")

    def write_config(self, br, mcp=None):
        with open(os.path.join(self.state, "hub.json"), "w") as f:
            json.dump({"browsers": br, "browser": {"chrome": FAKE_CHROME, "mcp": mcp or [sys.executable, FAKE_MCP]}}, f)

    def facts(self):
        d = os.path.join(self.state, "spool")
        out = [(e["entity"], e["data"]) for _, e in spool.take(d)]
        for n in os.listdir(d) if os.path.isdir(d) else []:
            os.unlink(os.path.join(d, n))
        return out

    def wait_up(self, url):
        end = time.time() + 10
        while not browsers.probe(url, timeout=0.3):
            self.assertLess(time.time(), end, url)
            time.sleep(0.1)

    def test_local_started_kept_and_restarted_remote_only_watched(self):
        self.sup.step()
        got = dict(self.facts())
        self.assertEqual((got["vm"]["up"], got["vm"]["problem"]), (False, "starting"))
        self.assertEqual((got["far"]["up"], got["far"]["problem"]), (False, "does not answer over CDP"))
        self.wait_up(f"http://127.0.0.1:{self.port}")
        with open(os.path.join(self.state, "browsers", "vm", "argv.json")) as f:
            argv = json.load(f)
        self.assertIn("--remote-debugging-address=127.0.0.1", argv)
        self.sup.step()
        self.assertEqual(self.facts(), [("vm", {"kind": "local", "url": f"http://127.0.0.1:{self.port}", "risk": [],
                                                "up": True, "version": "FakeChrome/1.0"})])
        self.sup.step()
        self.assertEqual(self.facts(), [])  # nothing changed — nothing for the core
        # it crashed — the supervisor starts it again (it never starts a remote one)
        self.sup.procs["vm"].kill()
        self.sup.procs["vm"].wait()
        self.sup.backoff.clear()
        self.sup.step()
        self.assertEqual(dict(self.facts())["vm"]["problem"], "starting")
        self.wait_up(f"http://127.0.0.1:{self.port}")
        self.remote = subprocess.Popen([sys.executable, FAKE_CHROME, f"--remote-debugging-port={self.rport}",
                                        f"--user-data-dir={self.state}/far"])
        self.wait_up(f"http://127.0.0.1:{self.rport}")
        self.sup.step()
        got = dict(self.facts())
        self.assertTrue(got["vm"]["up"] and got["far"]["up"])
        self.write_config({"vm": {"kind": "local", "port": self.port}})
        self.sup.step()
        self.assertEqual(self.facts(), [("far", {"removed": True})])

    def test_an_mcp_program_that_is_not_there_is_named_not_a_traceback(self):
        """browser.mcp named a program missing from PATH ("chrome-devtools-mcp", not installed): the launcher died in
        os.execv with FileNotFoundError, the session saw only "connection closed" and the browser's status said it
        answers. The launcher refuses at once with the program and the setting; the status of the browser says it too."""
        self.write_config({"vm": {"kind": "local", "port": self.port}}, mcp=["no-such-browser-mcp-x9", "--flag"])
        env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_LANG": "en"}
        out = subprocess.run([sys.executable, HUB_BROWSER, "mcp", "vm"], env=env, capture_output=True, text=True,
                             timeout=40)
        self.assertNotEqual(out.returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.state, "browsers", "vm", "argv.json")))   # no browser started for it
        self.assertNotIn("Traceback", out.stderr)
        self.assertIn("no-such-browser-mcp-x9", out.stderr)
        self.assertIn("browser.mcp", out.stderr)
        status = subprocess.run([sys.executable, HUB_BROWSER, "status"], env=env, capture_output=True, text=True, timeout=20)
        self.assertIn("no-such-browser-mcp-x9", status.stdout)
        self.sup.step()
        self.wait_up(f"http://127.0.0.1:{self.port}")
        self.facts()
        self.sup.step()
        (name, got), = self.facts()
        self.assertTrue(got["up"])   # Chrome itself answers
        self.assertIn("no-such-browser-mcp-x9", got["problem"])
        self.write_config({"vm": {"kind": "local", "port": self.port}})   # a program that is there: the problem goes
        self.sup.step()
        self.assertEqual(self.facts(), [("vm", {"kind": "local", "url": f"http://127.0.0.1:{self.port}", "risk": [],
                                                "up": True, "version": "FakeChrome/1.0"})])

    def test_a_bad_mcp_setting_is_not_checked_and_does_not_fail_the_supervisor(self):
        for bad in (5, [None], [""], {"x": 1}, "chrome-devtools-mcp", ["./node_modules/.bin/x"]):
            self.assertIsNone(browsers.mcp_missing({"browser": {"mcp": bad}}), bad)
        self.write_config({"vm": {"kind": "local", "port": self.port}}, mcp=5)
        self.sup.step()   # no TypeError
        self.assertEqual(dict(self.facts())["vm"]["problem"], "starting")

    def test_mcp_launcher_hands_over_through_the_platform_with_the_resolved_program(self):
        """os.execvpe on Windows starts a new process and the launcher exits: Claude Code saw the MCP server die, and
        npx (npx.cmd there) was not found without PATHEXT. The launcher goes through platforms.exec_instead, with the
        program found by shutil.which on the browser's PATH and the browser's environment."""
        from modules.browser import run
        self.write_config({"vm": {"kind": "local", "port": self.port}}, mcp=["npx", "chrome-devtools-mcp"])
        got = {}
        with mock.patch.dict(os.environ, {"HUB_STATE_DIR": self.state}), \
                mock.patch.object(run, "wait_for_browser", return_value=True), \
                mock.patch.object(run.shutil, "which", side_effect=lambda c, path=None: "/bin/" + c + ".cmd"), \
                mock.patch.object(run.platforms.current, "exec_instead",
                                  side_effect=lambda argv: got.update(argv=argv, env=dict(os.environ))):
            run.mcp("vm")
        self.assertEqual(got["argv"][0], "/bin/npx.cmd")
        self.assertIn("chrome-devtools-mcp", got["argv"])
        self.assertEqual(got["env"].get("HUB_STATE_DIR"), self.state)

    def test_the_mcp_of_a_remote_browser_never_sees_its_key_in_its_own_command_line(self):
        # the launcher becomes chrome-devtools-mcp: whatever is in that process's argv, `ps` shows to every user
        key = "Bearer k3y-of-the-remote"
        self.write_config({"far": {"kind": "remote", "url": f"ws://127.0.0.1:{self.rport}/devtools/browser/x",
                                   "headers": {"Authorization": key}}}, mcp=[sys.executable, FAKE_MCP])
        self.remote = subprocess.Popen([sys.executable, FAKE_CHROME, f"--remote-debugging-port={self.rport}",
                                        f"--user-data-dir={self.state}/far"])
        self.wait_up(f"http://127.0.0.1:{self.rport}")
        env = {**os.environ, "HUB_STATE_DIR": self.state}
        out = subprocess.run([sys.executable, HUB_BROWSER, "mcp", "far"], env=env, capture_output=True, text=True,
                             timeout=40)
        self.assertEqual(out.returncode, 0, out.stderr)
        argv = json.loads(out.stdout)
        self.assertNotIn("k3y-of-the-remote", " ".join(argv))
        with open(argv[argv.index("--config") + 1], encoding="utf-8") as f:
            self.assertEqual(json.loads(json.load(f)["wsHeaders"]), {"Authorization": key})

    def test_mcp_launcher_waits_then_becomes_mcp(self):
        self.write_config({"vm": {"kind": "local", "port": self.port}}, mcp=[sys.executable, FAKE_MCP])
        env = {**os.environ, "HUB_STATE_DIR": self.state}
        # no supervisor: the launcher starts the local browser itself and becomes the MCP
        out = subprocess.run([sys.executable, HUB_BROWSER, "mcp", "vm"], env=env, capture_output=True, text=True, timeout=40)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), ["--browserUrl", f"http://127.0.0.1:{self.port}"]
                         + (["--workspace", "/tmp"] if os.path.isdir("/tmp") else []) + ["--viewport", "1440x900"])
        status = subprocess.run([sys.executable, HUB_BROWSER, "status"], env=env, capture_output=True, text=True, timeout=20)
        self.assertIn(f"browser:vm  local  http://127.0.0.1:{self.port}  answers, FakeChrome/1.0", status.stdout)
        subprocess.run(["pkill", "-f", f"remote-debugging-port={self.port}"])
        bad = subprocess.run([sys.executable, HUB_BROWSER, "mcp", "nope"], env=env, capture_output=True, text=True, timeout=20)
        self.assertIn("browser nope is not in the settings", bad.stderr)


@unittest.skipIf(os.name == "nt", "fake Chrome is a .py instead of a program: Windows cannot launch it so")
class OnDemand(unittest.TestCase):
    """06.10: logging in to the Mac opened an empty Chrome window (about:blank), and a closed one came back after 5 s
    (the supervisor kept the browser running). Via the browser setting (default — always, as before): start: login — the
    supervisor starts it once per machine boot, demand — never; after that a session's tool call starts it."""

    def setUp(self):
        screen = mock.patch.object(browsers, "screenless", return_value=False)   # a window needs a screen, whatever this machine has
        screen.start()
        self.addCleanup(screen.stop)
        self.state = tempfile.mkdtemp(prefix="ondemand-")
        self.port = free_port()
        self.old = os.environ.get("HUB_CONFIG")
        os.environ["HUB_CONFIG"] = os.path.join(self.state, "hub.json")
        with open(os.environ["HUB_CONFIG"], "w") as f:
            json.dump({"browsers": {"own": {"kind": "local", "port": self.port, "headless": False, "start": "login"}},
                       "browser": {"chrome": FAKE_CHROME, "mcp": [sys.executable, FAKE_MCP_STDIO]}}, f)
        self.url = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        subprocess.run(["pkill", "-f", f"remote-debugging-port={self.port}"])
        shutil.rmtree(self.state, ignore_errors=True)
        os.environ["HUB_CONFIG"] = self.old or ""
        if self.old is None:
            os.environ.pop("HUB_CONFIG")

    def supervisor(self, launched, boot="1000"):
        sup = Supervisor(self.state)
        sup.launch = lambda *a: launched.append(a[2]) or subprocess.Popen(["sleep", "0"])
        sup.running = lambda profile: None
        sup.boot = lambda: boot
        return sup

    def test_login_once_per_boot_and_a_closed_one_stays_closed_across_hub_restarts(self):
        launched = []
        sup = self.supervisor(launched)
        sup.step()
        self.assertEqual(launched, ["own"])                                   # logging in to the Mac — start it, with the hub page
        sup.procs["own"].wait()                                               # the owner closed Chrome
        for _ in range(3):
            sup.step()
        self.assertTrue(sup.reported["own"]["idle"])
        self.supervisor(launched).step()                                      # hub restart — do not bring it back
        self.assertEqual(launched, ["own"])
        self.supervisor(launched, boot="2000").step()                         # Mac reboot — again
        self.assertEqual(launched, ["own", "own"])

    def test_demand_never_launched_by_the_supervisor(self):
        with open(os.environ["HUB_CONFIG"], "w") as f:
            json.dump({"browsers": {"own": {"kind": "local", "port": self.port, "headless": False, "start": "demand"}},
                       "browser": {"chrome": FAKE_CHROME, "mcp": [sys.executable, FAKE_MCP_STDIO]}}, f)
        launched = []
        sup = self.supervisor(launched)
        sup.step()
        self.assertEqual(launched, [])
        status = [d for _, e in spool.take(os.path.join(self.state, "spool")) for d in [e["data"]]]
        self.assertEqual([(d["up"], d.get("idle"), d.get("problem")) for d in status], [(False, True, None)])
        sup.running = lambda profile: 4242                                    # a session starts it
        sup.step()
        self.assertEqual(sup.reported["own"]["problem"], "starting")
        sup.running = lambda profile: None                                    # the owner closed Chrome
        for _ in range(3):
            sup.step()
        self.assertTrue(sup.reported["own"]["idle"])
        self.assertEqual(launched, [])

    def test_a_demand_browser_whose_mcp_program_is_missing_says_so_while_it_waits(self):
        # the launcher stops before it starts Chrome: "not running, starts on a call" would be shown for ever
        with open(os.environ["HUB_CONFIG"], "w") as f:
            json.dump({"browsers": {"own": {"kind": "local", "port": self.port, "start": "demand"}},
                       "browser": {"chrome": FAKE_CHROME, "mcp": ["no-such-browser-mcp-x9"]}}, f)
        sup = self.supervisor([])
        sup.step()
        (got,) = [e["data"] for _, e in spool.take(os.path.join(self.state, "spool"))]
        self.assertTrue(got["idle"])
        self.assertIn("no-such-browser-mcp-x9", got["problem"])
        snap = {"browsers": [{"name": "own", **got}]}
        from modules.web import modinfo
        card = next(m for m in modinfo.modules(self.state, catalog.load_config(self.state), snap) if m["id"] == "browsers")
        self.assertEqual(card["state"], "off")   # the module card does not say "working"

    def hung(self, start, headless, killed, clock):
        """A supervisor whose own Chrome is alive past the launch grace and never answers over CDP."""
        with open(os.environ["HUB_CONFIG"], "w") as f:
            json.dump({"browsers": {"own": {"kind": "local", "port": self.port, "headless": headless, "start": start}},
                       "browser": {"chrome": FAKE_CHROME}}, f)
        sup = Supervisor(self.state, clock=lambda: clock[0])
        proc = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        sup.procs["own"], sup.started["own"] = proc, 0
        sup.kill_tree = lambda p: killed.append(p.pid)
        sup.running = lambda profile: None
        sup.boot = lambda: "1000"
        sup.logged["own"] = "1000"   # the login start of this boot is behind
        return sup

    def test_a_hung_headless_chrome_of_a_demand_or_login_browser_is_stopped_but_a_window_never(self):
        for start in ("demand", "login"):
            with self.subTest(start=start, headless=True):
                killed, clock = [], [run.LAUNCH_GRACE + 1]
                sup = self.hung(start, True, killed, clock)
                sup.step()
                self.assertEqual(len(killed), 1)   # no window to lose: nobody sees it, and a tool call would not bring it up
                self.assertNotIn("own", sup.procs)
                self.assertTrue(sup.reported["own"]["idle"])   # the next tool call starts a new one
            with self.subTest(start=start, headless=False):
                killed, clock = [], [run.LAUNCH_GRACE + 1]
                sup = self.hung(start, False, killed, clock)
                sup.step()
                self.assertEqual(killed, [])   # the owner's window: only said
                self.assertIn("does not answer over CDP", sup.reported["own"]["problem"])

    def test_a_hung_chrome_of_our_profile_started_elsewhere_is_stopped_only_when_headless(self):
        for cmd, dies in (("/opt/chrome --headless=new --user-data-dir=%s --remote-debugging-port=%d", True),
                          ("/opt/chrome --headless=new --user-data-dir=%s --remote-debugging-pipe --x=%d", False),
                          ("/opt/chrome --user-data-dir=%s --remote-debugging-port=%d", False)):
            with self.subTest(cmd=cmd):
                killed, clock = [], [0]
                sup = self.hung("demand", True, [], clock)
                sup.procs.clear()
                profile = browsers.profile_dir(self.state, "own", {"kind": "local", "port": self.port})
                sup.listed = lambda: [(4242, cmd % (profile, self.port))]
                sup.running = lambda p: run.running(p, sup.listed())
                sup.kill_pid = killed.append
                sup.step()
                clock[0] = run.LAUNCH_GRACE + 1
                sup.step()
                self.assertEqual(killed, [4242] if dies else [])
                if not dies:
                    self.assertIn("does not answer over CDP", sup.reported["own"]["problem"])

    def test_session_start_does_not_launch_a_tool_call_does(self):
        env = {**os.environ, "HUB_STATE_DIR": self.state}
        p = subprocess.Popen([sys.executable, HUB_BROWSER, "mcp", "own"], env=env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n")
            p.stdin.flush()
            self.assertEqual(json.loads(p.stdout.readline()), {"id": 1, "method": "initialize", "up": False, "result": OK})
            p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call"}) + "\n")
            p.stdin.flush()
            self.assertEqual(json.loads(p.stdout.readline()), {"id": 2, "method": "tools/call", "up": True, "result": OK})
            p.stdin.close()
            self.assertEqual(p.wait(timeout=10), 0, p.stderr.read())
        finally:
            p.kill()
            p.wait()
            for f in (p.stdin, p.stdout, p.stderr):
                f.close()

    def test_the_session_works_in_its_own_tab_not_someone_elses(self):
        # PR #44: chrome-devtools-mcp takes the first tab — the session must have its own, otherwise it would hijack
        # someone else's (the hub page in the owner's window). Before the session's first call the intermediary opens
        # a tab for it (new_page — it also becomes the current one); the session is not shown the answer to that
        log = os.path.join(self.state, "mcp.log")
        env = {**os.environ, "HUB_STATE_DIR": self.state, "FAKE_MCP_LOG": log}
        p = subprocess.Popen([sys.executable, HUB_BROWSER, "mcp", "own"], env=env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            for i, method in ((1, "initialize"), (2, "tools/call"), (3, "tools/call")):
                p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method,
                                          "params": {"name": "take_snapshot"}}) + "\n")
                p.stdin.flush()
                self.assertEqual(json.loads(p.stdout.readline())["id"], i)       # the session sees only its own answers
            p.stdin.close()
            self.assertEqual(p.wait(timeout=10), 0, p.stderr.read())
        finally:
            p.kill()
            p.wait()
            for f in (p.stdin, p.stdout, p.stderr):
                f.close()
        with open(log) as f:
            got = [json.loads(x) for x in f]
        self.assertEqual([(m["method"], (m.get("params") or {}).get("name")) for m in got],
                         [("initialize", "take_snapshot"), ("tools/call", "new_page"), ("tools/call", "take_snapshot"),
                          ("tools/call", "take_snapshot"), ("tools/call", "close_page")])   # its own tab — once
        self.assertEqual(got[1]["params"]["arguments"], {"url": "about:blank", "background": True})   # do not steal focus
        # the session left — close its tab (07.10: empty session tabs piled up in the owner's browser)
        self.assertEqual((got[-1]["method"], got[-1]["params"]["name"], got[-1]["params"]["arguments"]),
                         ("tools/call", "close_page", {"pageId": 2}))

    def test_the_first_call_waits_until_the_own_tab_is_open(self):
        # PR #44: chrome-devtools-mcp answers calls out of order — without waiting for the new_page answer, the
        # session's first call can go out before the tab becomes the current one, i.e. into someone else's tab
        log = os.path.join(self.state, "mcp.log")
        env = {**os.environ, "HUB_STATE_DIR": self.state, "FAKE_MCP_LOG": log, "FAKE_MCP_SLOW_NEW_PAGE": "0.5"}
        p = subprocess.Popen([sys.executable, HUB_BROWSER, "mcp", "own"], env=env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": "take_snapshot"}}) + "\n")
            p.stdin.flush()
            self.assertEqual(json.loads(p.stdout.readline())["id"], 1)
            p.stdin.close()
            self.assertEqual(p.wait(timeout=10), 0, p.stderr.read())
        finally:
            p.kill()
            p.wait()
            for f in (p.stdin, p.stdout, p.stderr):
                f.close()
        with open(log) as f:
            got = [json.loads(x) for x in f]
        self.assertEqual([m.get("answered") or m["params"]["name"] for m in got][:3],
                         ["new_page", "new_page", "take_snapshot"])   # the call, its answer, and only then the session's

    def test_no_own_tab_no_call_into_someone_elses(self):
        # the own tab did not open — the session's call does not go to the first (someone else's) tab: the session
        # gets an error with the reason, the next call tries to open its own again
        log = os.path.join(self.state, "mcp.log")
        env = {**os.environ, "HUB_STATE_DIR": self.state, "FAKE_MCP_LOG": log, "FAKE_MCP_NEW_PAGE_FAILS": "1"}
        p = subprocess.Popen([sys.executable, HUB_BROWSER, "mcp", "own"], env=env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            outs = []
            for i in (1, 2):
                p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                                          "params": {"name": "take_snapshot"}}) + "\n")
                p.stdin.flush()
                outs.append(json.loads(p.stdout.readline()))
            p.stdin.close()
            p.wait(timeout=10)
        finally:
            p.kill()
            p.wait()
            for f in (p.stdin, p.stdout, p.stderr):
                f.close()
        self.assertEqual([o["id"] for o in outs], [1, 2])
        self.assertTrue(all(o["result"]["isError"] for o in outs), outs)
        self.assertIn("own tab did not open", outs[0]["result"]["content"][0]["text"])
        with open(log) as f:
            names = [json.loads(x).get("params", {}).get("name") for x in f if '"answered"' not in x]
        self.assertNotIn("take_snapshot", names)                     # the call did not reach the browser
        self.assertEqual(names.count("new_page"), 2)                  # and a new attempt each time

    def test_a_browser_that_did_not_come_up_gives_the_session_the_reason(self):
        # a Chrome with the profile was open without the debugging port: sessions saw only "Could not connect to
        # Chrome" from chrome-devtools-mcp and did not know what to do
        log = os.path.join(self.state, "mcp.log")
        script = ("import sys; from modules.browser import run; sys.exit(run.proxy(sys.argv[1:], None, "
                  "lambda: 'Chrome without a debugging port'))")
        p = subprocess.Popen([sys.executable, "-c", script, sys.executable, FAKE_MCP_STDIO, "--browserUrl", self.url],
                             cwd=os.path.join(HERE, ".."), env={**os.environ, "FAKE_MCP_LOG": log}, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
        try:
            p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                      "params": {"name": "take_snapshot"}}) + "\n")
            p.stdin.flush()
            out = json.loads(p.stdout.readline())
            p.stdin.close()
            p.wait(timeout=10)
        finally:
            p.kill()
            p.wait()
            for f in (p.stdin, p.stdout, p.stderr):
                f.close()
        self.assertEqual(out["id"], 7)
        self.assertTrue(out["result"]["isError"])
        self.assertIn("Chrome without a debugging port", out["result"]["content"][0]["text"])
        names = []
        if os.path.exists(log):
            with open(log, encoding="utf-8") as f:
                names = [(json.loads(x).get("params") or {}).get("name") for x in f]
        self.assertNotIn("take_snapshot", names)                     # the call did not reach the browser

    def test_why_the_browser_is_not_there(self):
        from modules.browser import run
        b = {"url": "http://127.0.0.1:9223"}
        log = os.path.join("state", "browsers", "work.log")
        self.assertIn("pid 4242", run.unreachable("work", b, 4242, log))
        self.assertIn(b["url"], run.unreachable("work", b, 4242, log))
        self.assertIn(log, run.unreachable("work", b, None, log))
        held = run.unreachable("work", b, True, log)                 # the profile's lock tells it, with no pid
        self.assertIn("is running but", held)
        self.assertNotIn("True", held)

    def test_a_stopped_session_closes_its_tab_too(self):
        # the hub stops the session process (re-raise, restart) with a signal, not by end of input — the tab must still be closed.
        # Claude Code sends SIGINT first to its MCP stdio servers when it exits: without a handler for it the intermediary died
        # with KeyboardInterrupt and every session left one about:blank tab behind
        import signal
        for sig in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=sig.name):
                log = os.path.join(self.state, f"mcp-{sig.name}.log")
                env = {**os.environ, "HUB_STATE_DIR": self.state, "FAKE_MCP_LOG": log}
                p = subprocess.Popen([sys.executable, HUB_BROWSER, "mcp", "own"], env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                              "params": {"name": "take_snapshot"}}) + "\n")
                    p.stdin.flush()
                    self.assertEqual(json.loads(p.stdout.readline())["id"], 1)
                    p.send_signal(sig)
                    p.wait(timeout=10)
                finally:
                    p.kill()
                    p.wait()
                    for f in (p.stdin, p.stdout, p.stderr):
                        f.close()
                with open(log) as f:
                    names = [(json.loads(x).get("params") or {}).get("name") for x in f]
                self.assertEqual(names[-1], "close_page")

    def session(self, calls, env=None, midway=None):
        """A session's calls through the intermediary: [(tool, arguments)]. Returns what reached chrome-devtools-mcp as
        [(tool, arguments)]; midway() runs after the last answer, before the session leaves."""
        log = os.path.join(self.state, "mcp.log")
        with open(log, "w"):
            pass
        p = subprocess.Popen([sys.executable, HUB_BROWSER, "mcp", "own"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True,
                             env={**os.environ, "HUB_STATE_DIR": self.state, "FAKE_MCP_LOG": log, **(env or {})})
        try:
            for i, (name, arguments) in enumerate(calls, 1):
                p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                                          "params": {"name": name, "arguments": arguments}}) + "\n")
                p.stdin.flush()
                self.assertEqual(json.loads(p.stdout.readline())["id"], i)
            if midway:
                midway()
            p.stdin.close()
            self.assertEqual(p.wait(timeout=10), 0, p.stderr.read())
        finally:
            p.kill()
            p.wait()
            for f in (p.stdin, p.stdout, p.stderr):
                f.close()
        with open(log) as f:
            return [((m.get("params") or {}).get("name"), (m.get("params") or {}).get("arguments"))
                    for m in map(json.loads, f) if "answered" not in m]

    def chrome_tabs(self):
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(self.url + "/json/list", timeout=3) as r:
            return [t["url"] for t in json.load(r)]

    def test_a_session_that_opens_its_own_tab_first_gets_no_blank_one(self):
        # 28.10: the intermediary opened an about:blank tab on every first call; a session that started with new_page
        # worked in its own page and the blank one stayed in the owner's window until the session ended (hours)
        seen = []
        got = self.session([("new_page", {"url": "https://example.com"}), ("take_snapshot", {})],
                           midway=lambda: seen.append(self.chrome_tabs()))
        self.assertEqual(got, [("new_page", {"url": "https://example.com"}), ("take_snapshot", {})])   # no new_page of its own, no close_page
        self.assertIn("https://example.com", seen[0])

    def test_a_failed_new_page_does_not_leave_the_session_without_its_own_tab(self):
        # the session's new_page did not open: the next call is not let into a foreign tab, the intermediary makes its own as before
        got = self.session([("new_page", {"url": "https://example.com"}), ("take_snapshot", {})],
                           env={"FAKE_MCP_NEW_PAGE_FAILS": "1"})
        self.assertEqual([n for n, _ in got], ["new_page", "new_page"])   # the session's, then its own (it failed too: no snapshot)

    def test_the_blank_tab_goes_when_the_session_opens_its_own_after_it(self):
        # a session starts with list_pages (its own tab: new_page makes it current), then opens a page itself:
        # the empty tab of the intermediary is closed at once, and is not made again
        seen = []
        got = self.session([("list_pages", {}), ("new_page", {"url": "https://example.com"}), ("take_snapshot", {})],
                           midway=lambda: seen.append(self.chrome_tabs()))
        self.assertEqual(got, [("new_page", {"url": "about:blank", "background": True}), ("list_pages", {}),
                               ("new_page", {"url": "https://example.com"}), ("close_page", {"pageId": 2}),
                               ("take_snapshot", {})])
        self.assertEqual(seen[0].count("about:blank"), 0, seen[0])

    def test_the_own_tab_the_session_navigated_stays_until_it_leaves(self):
        # the tab is not blank any more — it holds the session's work: it is closed at the end of the session, not before
        got = self.session([("list_pages", {}), ("navigate_page", {"url": "https://example.org"}),
                            ("new_page", {"url": "https://example.com"})])
        self.assertEqual([n for n, _ in got], ["new_page", "list_pages", "navigate_page", "new_page", "close_page"])
        self.assertEqual(got[-1], ("close_page", {"pageId": 2}))

    def test_the_tab_chrome_opened_at_launch_is_closed_once_another_one_exists(self):
        # Chrome opens the positional about:blank (chrome_command) when it is launched; nobody owns that tab.
        # The first call launched Chrome here: the launch tab goes once the session has its own tab
        seen = []

        def midway():
            for _ in range(50):
                seen[:] = [self.chrome_tabs()]
                if seen[0].count("about:blank") == 1:   # the session's own, not the launch one
                    break
                time.sleep(0.1)
        self.session([("take_snapshot", {})], midway=midway)
        self.assertEqual(seen[0].count("about:blank"), 1, seen[0])

    def test_the_launch_tab_is_not_closed_when_it_is_the_only_one(self):
        # no other tab (the own one did not open): closing the last tab would close the window
        seen = []

        def midway():
            time.sleep(1)
            seen.append(self.chrome_tabs())
        self.session([("take_snapshot", {})], env={"FAKE_MCP_NEW_PAGE_FAILS": "1"}, midway=midway)
        self.assertEqual(seen[0], ["about:blank"])

    def test_a_later_failed_new_page_does_not_bring_back_the_blank_tab(self):
        # review of PR #322: the session works in the page it opened; a later new_page that failed made the intermediary
        # open a blank tab of its own on the next call — new_page selected it, and the session's click went there
        got = self.session([("new_page", {"url": "https://example.com"}), ("new_page", {"url": "bad:x"}), ("take_snapshot", {})])
        self.assertEqual([n for n, _ in got], ["new_page", "new_page", "take_snapshot"])

    def test_a_call_right_after_the_sessions_new_page_waits_for_its_reply(self):
        # review of PR #322: the session's new_page stands for its own tab, so the call after it waits for the reply the
        # same way (chrome-devtools-mcp answers out of order: a call sent at once could land in a foreign tab)
        log = os.path.join(self.state, "mcp.log")
        env = {**os.environ, "HUB_STATE_DIR": self.state, "FAKE_MCP_LOG": log, "FAKE_MCP_SLOW_NEW_PAGE": "0.5"}
        p = subprocess.Popen([sys.executable, HUB_BROWSER, "mcp", "own"], env=env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            for i, name in ((1, "new_page"), (2, "take_snapshot")):   # the second without waiting for the first reply
                p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                                          "params": {"name": name, "arguments": {"url": "https://example.com"}}}) + "\n")
            p.stdin.flush()
            self.assertEqual(sorted(json.loads(p.stdout.readline())["id"] for _ in (1, 2)), [1, 2])
            p.stdin.close()
            self.assertEqual(p.wait(timeout=10), 0, p.stderr.read())
        finally:
            p.kill()
            p.wait()
            for f in (p.stdin, p.stdout, p.stderr):
                f.close()
        with open(log) as f:
            got = [json.loads(x) for x in f]
        self.assertEqual([m.get("answered") or m["params"]["name"] for m in got], ["new_page", "new_page", "take_snapshot"])

    def test_a_call_with_params_null_does_not_kill_the_intermediary(self):
        # review of PR #322: params: null broke the new_page check with AttributeError, and the session lost the browser
        log = os.path.join(self.state, "mcp.log")
        env = {**os.environ, "HUB_STATE_DIR": self.state, "FAKE_MCP_LOG": log}
        p = subprocess.Popen([sys.executable, HUB_BROWSER, "mcp", "own"], env=env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            for i, params in ((1, None), (2, {"name": "take_snapshot"})):
                p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": params}) + "\n")
                p.stdin.flush()
                self.assertEqual(json.loads(p.stdout.readline())["id"], i)
            p.stdin.close()
            self.assertEqual(p.wait(timeout=10), 0, p.stderr.read())
        finally:
            p.kill()
            p.wait()
            for f in (p.stdin, p.stdout, p.stderr):
                f.close()

    def quick(self, env, **consts):
        """The intermediary with shorter waits (consts: module constants of run) — the same start as hub-browser mcp."""
        code = (f"import sys; sys.path.insert(0, {ROOT!r}); from modules.browser import run; "
                + "".join(f"run.{k} = {v!r}; " for k, v in consts.items()) + "run.main(['mcp', 'own'])")
        return subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env={**os.environ, "HUB_STATE_DIR": self.state, **env})

    def talk(self, p, sends, replies):
        for m in sends:
            p.stdin.write(json.dumps({"jsonrpc": "2.0", **m}) + "\n")
        p.stdin.flush()
        return [json.loads(p.stdout.readline()) for _ in range(replies)]

    def finish(self, p):
        """End of input: the session is gone. Returns what else the session got (nothing is expected)."""
        try:
            p.stdin.close()
            rest = p.stdout.read()
            self.assertEqual(p.wait(timeout=10), 0, p.stderr.read())
            return rest
        finally:
            p.kill()
            p.wait()
            for f in (p.stdout, p.stderr):
                f.close()

    @staticmethod
    def logged(path):
        with open(path) as f:
            return [json.loads(x) for x in f]

    @staticmethod
    def tool(i, name, **arguments):
        return {"id": i, "method": "tools/call", "params": {"name": name, "arguments": arguments}}

    def test_a_hung_browser_mcp_is_replaced_and_the_session_call_goes_through(self):
        # 05.11: on an overloaded machine chrome-devtools-mcp 1.12.0 did not connect to Chrome within 60 s; the hung
        # connection holds its lock, so every later call in that process failed too, and the session lost the browser
        # until a reload, while a new process connects in 0.2 s. The intermediary replaces the hung one itself: the new
        # one gets the session's initialize (its answer is not shown to the session), and the call goes through
        for mode in ("silent", "error"):
            with self.subTest(mode=mode):
                log, procs = os.path.join(self.state, f"mcp-{mode}.log"), os.path.join(self.state, f"procs-{mode}")
                p = self.quick({"FAKE_MCP_LOG": log, "FAKE_MCP_PROCS": procs, "FAKE_MCP_STUCK": f"1:{mode}"},
                               OWN_TAB_WAIT=3 if mode == "silent" else 15)
                init = {"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}}}
                self.assertEqual([o["id"] for o in self.talk(p, [init], 1)], [1])
                # the second call comes while the first one waits for the replacement: neither lost nor doubled
                got = self.talk(p, [{"method": "notifications/initialized"}, self.tool(2, "take_snapshot"),
                                    self.tool(3, "take_snapshot")], 2)
                self.assertEqual(sorted(o["id"] for o in got), [2, 3])
                self.assertFalse(any((o.get("result") or {}).get("isError") for o in got), got)
                self.assertEqual(self.finish(p), "")
                with open(procs) as f:
                    pids = [int(x) for x in f.read().split()]
                self.assertEqual(len(pids), 2)
                end = time.time() + 10   # it leaves with the proxy (stdin closed): not at the same instant
                while platforms.current.alive(pids[0]) and time.time() < end:
                    time.sleep(0.05)
                self.assertFalse(platforms.current.alive(pids[0]), "the hung one is stopped")
                old, new = self.logged(f"{log}.0"), self.logged(f"{log}.1")
                self.assertEqual([m["method"] for m in old], ["initialize", "notifications/initialized", "tools/call"])
                self.assertEqual([(m.get("method"), (m.get("params") or {}).get("name")) for m in new],
                                 [("initialize", None), ("notifications/initialized", None), ("tools/call", "new_page"),
                                  ("tools/call", "take_snapshot"), ("tools/call", "take_snapshot"),
                                  ("tools/call", "close_page")])
                self.assertEqual(new[0]["params"], init["params"])
                self.assertNotEqual(new[0]["id"], 1)
                self.assertEqual(sorted(m["id"] for m in new[3:5]), [2, 3])

    def test_a_browser_mcp_that_stays_hung_ends_in_an_error_that_says_what_to_do(self):
        log, procs = os.path.join(self.state, "mcp.log"), os.path.join(self.state, "procs")
        p = self.quick({"FAKE_MCP_LOG": log, "FAKE_MCP_PROCS": procs, "FAKE_MCP_STUCK": "all:silent",
                        "HUB_SESSION": "worker"}, OWN_TAB_WAIT=1)
        out = self.talk(p, [self.tool(1, "take_snapshot")], 1)[0]
        self.assertTrue(out["result"]["isError"])
        self.assertIn("hub session reload worker", out["result"]["content"][0]["text"])
        self.assertIn("after 2 replacements", out["result"]["content"][0]["text"])   # an answer to initialize is no success
        self.finish(p)
        with open(procs) as f:
            self.assertEqual(len(f.read().split()), 3)    # the first one and two replacements in a row, no more

    def test_after_the_replacements_ran_out_a_later_call_tries_a_new_series(self):
        # 10.11: on an overloaded machine three processes in a row did not connect, and the session was left without the
        # browser for its whole life, though a new process connected once the load was gone. Within the pause the next call
        # gets the same error, after it a new series of replacements starts
        for pause, procs_after, ok in ((60, 3, False), (0, 4, True)):
            with self.subTest(pause=pause):
                log, procs = os.path.join(self.state, f"mcp-{pause}.log"), os.path.join(self.state, f"procs-{pause}")
                p = self.quick({"FAKE_MCP_LOG": log, "FAKE_MCP_PROCS": procs, "FAKE_MCP_STUCK": "3:silent",
                                "HUB_SESSION": "worker"}, OWN_TAB_WAIT=1, RESPAWN_PAUSE=pause)
                first = self.talk(p, [self.tool(1, "take_snapshot")], 1)[0]
                self.assertIn("after 2 replacements", first["result"]["content"][0]["text"])
                self.assertIn(f"repeat the call in {pause} s", first["result"]["content"][0]["text"])
                second = self.talk(p, [self.tool(2, "take_snapshot")], 1)[0]
                self.assertEqual(second["id"], 2)
                self.assertEqual(not second["result"].get("isError"), ok, second)
                self.finish(p)
                with open(procs) as f:
                    self.assertEqual(len(f.read().split()), procs_after)

    def test_the_pause_before_a_new_series_counts_from_the_error_not_from_the_last_replacement(self):
        # the last replacement waits OWN_TAB_WAIT for the browser before the session gets the error, so a pause counted
        # from the replacement was already over when "repeat the call in N s" reached the session: the very next call
        # started a new series at once
        log, procs = os.path.join(self.state, "mcp.log"), os.path.join(self.state, "procs")
        p = self.quick({"FAKE_MCP_LOG": log, "FAKE_MCP_PROCS": procs, "FAKE_MCP_STUCK": "3:silent",
                        "HUB_SESSION": "worker"}, OWN_TAB_WAIT=2, RESPAWN_PAUSE=1)
        first = self.talk(p, [self.tool(1, "take_snapshot")], 1)[0]
        self.assertIn("after 2 replacements", first["result"]["content"][0]["text"])
        second = self.talk(p, [self.tool(2, "take_snapshot")], 1)[0]   # right after the error: still inside the pause
        self.assertIn("after 2 replacements", second["result"]["content"][0]["text"])
        self.finish(p)
        with open(procs) as f:
            self.assertEqual(len(f.read().split()), 3)   # no fourth process

    def test_the_error_names_an_overloaded_machine(self):
        self.assertIn("overloaded (load 33 per core)", run.overload_note(lambda: 33.3))
        self.assertEqual(run.overload_note(lambda: run.OVERLOAD), "")
        getloadavg = os.getloadavg
        del os.getloadavg   # Windows has none
        try:
            self.assertEqual(run.overload_note(), "")
        finally:
            os.getloadavg = getloadavg

    def test_a_session_call_that_hit_the_hung_connection_gets_a_new_browser_mcp_next_time(self):
        # the session opens its page itself: its own new_page gets the timeout error, the next call goes to a new process
        log, procs = os.path.join(self.state, "mcp.log"), os.path.join(self.state, "procs")
        p = self.quick({"FAKE_MCP_LOG": log, "FAKE_MCP_PROCS": procs, "FAKE_MCP_STUCK": "1:error"}, OWN_TAB_WAIT=15)
        first = self.talk(p, [self.tool(1, "new_page", url="https://example.com")], 1)[0]
        self.assertIn("waiting for the browser connection", first["result"]["content"][0]["text"])
        second = self.talk(p, [self.tool(2, "take_snapshot")], 1)[0]
        self.assertEqual(second["id"], 2)
        self.assertNotIn("isError", second.get("result") or {})
        self.finish(p)
        with open(procs) as f:
            self.assertEqual(len(f.read().split()), 2)

    def test_a_browser_mcp_that_repeats_a_cdp_timeout_at_once_is_replaced(self):
        # 12.11: under swap a page of chrome-devtools-mcp timed out on Network.enable after 180 s; puppeteer keeps that
        # failure for the tab, so every later call in that process got the same error at once for 20 minutes, while Chrome
        # itself already answered and a new process would have connected
        log, procs = os.path.join(self.state, "mcp.log"), os.path.join(self.state, "procs")
        p = self.quick({"FAKE_MCP_LOG": log, "FAKE_MCP_PROCS": procs, "FAKE_MCP_STUCK": "1:network",
                        "FAKE_MCP_STUCK_FIRST": "4"}, STUCK_FAST=3)
        first = self.talk(p, [self.tool(1, "take_snapshot")], 1)[0]
        self.assertIn("Network.enable timed out", first["result"]["content"][0]["text"])   # a slow one: not replaced yet
        second = self.talk(p, [self.tool(2, "take_snapshot")], 1)[0]
        self.assertEqual(second["id"], 2)
        self.assertNotIn("isError", second["result"], second)
        self.finish(p)
        with open(procs) as f:
            self.assertEqual(len(f.read().split()), 2)
        self.assertIn("take_snapshot", [(m.get("params") or {}).get("name") for m in self.logged(f"{log}.1")])

    def test_a_session_call_that_times_out_slowly_then_at_once_runs_on_a_new_browser_mcp(self):
        # the way of 12.11: the session's own tab is open, then its call times out slowly, the next one fails at once
        # (the session gets this one fast error too), and the call after it goes to a new process
        log, procs = os.path.join(self.state, "mcp.log"), os.path.join(self.state, "procs")
        p = self.quick({"FAKE_MCP_LOG": log, "FAKE_MCP_PROCS": procs, "FAKE_MCP_STUCK": "1:network",
                        "FAKE_MCP_STUCK_AFTER": "1", "FAKE_MCP_STUCK_FIRST": "4"}, STUCK_FAST=3)
        for i in (1, 2):
            self.assertIn("Network.enable timed out", self.talk(p, [self.tool(i, "take_snapshot")], 1)[0]["result"]["content"][0]["text"])
        self.assertNotIn("isError", self.talk(p, [self.tool(3, "take_snapshot")], 1)[0]["result"])
        self.finish(p)
        with open(procs) as f:
            self.assertEqual(len(f.read().split()), 2)
        self.assertEqual([(m.get("params") or {}).get("name") for m in self.logged(f"{log}.0") if "answered" not in m],
                         ["new_page", "take_snapshot", "take_snapshot"])
        self.assertIn(3, [m.get("id") for m in self.logged(f"{log}.1")])

    def test_a_cdp_timeout_that_comes_back_after_each_replacement_stops_at_the_limit(self):
        # review of PR #578: each new process opens its own tab fine, which reset the count of replacements, so a page
        # failure that keeps coming back replaced the process every other call and left a blank tab each time
        log, procs = os.path.join(self.state, "mcp.log"), os.path.join(self.state, "procs")
        p = self.quick({"FAKE_MCP_LOG": log, "FAKE_MCP_PROCS": procs, "FAKE_MCP_STUCK": "all:network",
                        "FAKE_MCP_STUCK_AFTER": "1", "HUB_SESSION": "worker"})
        got = [self.talk(p, [self.tool(i, "take_snapshot")], 1)[0] for i in range(1, 8)]
        self.assertTrue(all(o["result"].get("isError") for o in got), got)
        self.assertIn("hub session reload worker", got[-1]["result"]["content"][0]["text"])
        self.finish(p)
        with open(procs) as f:
            self.assertEqual(len(f.read().split()), run.RESPAWNS + 1)

    def test_after_the_limit_for_a_kept_cdp_timeout_a_later_call_tries_a_new_series(self):
        # the same as for a hung connection: the load may be gone after a while, and a session left without the browser
        # until a reload is what the replacement is for. Within the pause the call gets the error, after it a new process
        for pause, procs_after, ok in ((60, 3, False), (3, 4, True)):
            with self.subTest(pause=pause):
                log, procs = os.path.join(self.state, f"mcp-{pause}.log"), os.path.join(self.state, f"procs-{pause}")
                p = self.quick({"FAKE_MCP_LOG": log, "FAKE_MCP_PROCS": procs, "FAKE_MCP_STUCK": "3:network",
                                "FAKE_MCP_STUCK_AFTER": "1", "HUB_SESSION": "worker"}, RESPAWN_PAUSE=pause)
                got = [self.talk(p, [self.tool(i, "take_snapshot")], 1)[0] for i in range(1, 8)]
                self.assertIn("hub session reload worker", got[-1]["result"]["content"][0]["text"])
                self.assertIn(f"repeat the call in {pause} s", got[-1]["result"]["content"][0]["text"])
                time.sleep(min(pause, 3) + 0.3)
                self.assertTrue(self.talk(p, [self.tool(8, "take_snapshot")], 1)[0]["result"].get("isError"))   # a new series
                last = self.talk(p, [self.tool(9, "take_snapshot")], 1)[0]   # replaces the process after this error
                self.assertEqual(not last["result"].get("isError"), ok, last)
                self.finish(p)
                with open(procs) as f:
                    self.assertEqual(len(f.read().split()), procs_after)

    def test_a_single_slow_cdp_timeout_does_not_replace_the_browser_mcp(self):
        log, procs = os.path.join(self.state, "mcp.log"), os.path.join(self.state, "procs")
        p = self.quick({"FAKE_MCP_LOG": log, "FAKE_MCP_PROCS": procs, "FAKE_MCP_TIMEOUT_ONCE": "4"}, STUCK_FAST=3)
        self.assertIn("Network.enable timed out", self.talk(p, [self.tool(1, "take_snapshot")], 1)[0]["result"]["content"][0]["text"])
        self.assertNotIn("isError", self.talk(p, [self.tool(2, "take_snapshot")], 1)[0]["result"])
        self.finish(p)
        with open(procs) as f:
            self.assertEqual(len(f.read().split()), 1)

    def test_a_replaced_browser_mcp_that_opens_the_tab_late_closes_it(self):
        # a slow one, not a hung one: its tab opened after it was replaced, and nobody would close it in the owner's window
        log, procs = os.path.join(self.state, "mcp.log"), os.path.join(self.state, "procs")
        p = self.quick({"FAKE_MCP_LOG": log, "FAKE_MCP_PROCS": procs, "FAKE_MCP_SLOW_FIRST": "4.5"}, OWN_TAB_WAIT=3)
        out = self.talk(p, [self.tool(1, "take_snapshot")], 1)[0]
        self.assertNotIn("isError", out["result"])
        end = time.time() + 10   # the replaced one answers late and closes its tab before it goes
        while time.time() < end and '"close_page"' not in open(f"{log}.0").read():
            time.sleep(0.1)
        self.finish(p)
        old = self.logged(f"{log}.0")
        self.assertEqual([(m.get("params") or {}).get("name") for m in old if "answered" not in m], ["new_page", "close_page"])
        self.assertEqual(old[-1]["params"]["arguments"], {"pageId": 2})
        self.assertEqual(self.chrome_tabs().count("about:blank"), 0)

    def test_a_page_that_says_the_hung_words_does_not_replace_a_working_browser_mcp(self):
        # review of PR #550: the words of the timeout error in an ordinary reply (a page, a document) are no hang
        log, procs = os.path.join(self.state, "mcp.log"), os.path.join(self.state, "procs")
        p = self.quick({"FAKE_MCP_LOG": log, "FAKE_MCP_PROCS": procs,
                        "FAKE_MCP_OK_TEXT": "an issue: timed out waiting for the browser connection to be established"})
        for i in (1, 2):
            self.assertNotIn("isError", self.talk(p, [self.tool(i, "take_snapshot")], 1)[0]["result"])
        self.finish(p)
        with open(procs) as f:
            self.assertEqual(len(f.read().split()), 1)

    def test_a_late_answer_to_an_earlier_own_tab_attempt_does_not_count(self):
        # the intermediary asked for its tab with the same id every time: a late answer to an attempt that had already
        # timed out was taken for the answer to the next one, and the session's call went out before its tab was selected
        log = os.path.join(self.state, "mcp.log")
        p = self.quick({"FAKE_MCP_LOG": log, "FAKE_MCP_NEW_PAGE_DELAYS": "6,3.5"}, OWN_TAB_WAIT=4.5, RESPAWNS=0)
        self.assertTrue(self.talk(p, [self.tool(1, "take_snapshot")], 1)[0]["result"]["isError"])
        self.assertEqual(self.talk(p, [self.tool(2, "take_snapshot")], 1)[0]["id"], 2)
        self.finish(p)
        got = self.logged(log)
        asks = [m["id"] for m in got if (m.get("params") or {}).get("name") == "new_page"]
        self.assertEqual(len(set(asks)), 2, asks)                             # each attempt has its own id
        order = [m.get("answered") and m["id"] or (m.get("params") or {}).get("name") for m in got]
        self.assertLess(order.index(asks[1]), order.index("take_snapshot"), order)   # the call after its own attempt's answer
        closed = {m["params"]["arguments"]["pageId"] for m in got if (m.get("params") or {}).get("name") == "close_page"}
        self.assertEqual(closed, {2, 3})                                      # the late tab is not left behind either

    def test_launch_tabs_only_the_one_chrome_opened(self):
        # review of PR #322: two blank tabs right after the launch — one of them is another session's own tab (it came up
        # meanwhile): neither is taken for the launch tab, otherwise tidy closes that session's tab
        from modules.browser import run
        self.addCleanup(run.launch_tabs.clear)
        page = lambda i: {"id": i, "type": "page", "url": "about:blank"}
        run.launch_tabs.clear()
        run.note_launch_tabs("u", tabs=lambda u: [page("A"), page("B")], clock=lambda: 0, sleep=lambda dt: None)
        self.assertEqual(run.launch_tabs, set())
        run.note_launch_tabs("u", tabs=lambda u: [page("A")], clock=lambda: 0, sleep=lambda dt: None)
        self.assertEqual(run.launch_tabs, {"A"})

    def test_no_wait_for_a_launch_tab_when_the_last_session_comes_back(self):
        from modules.browser import run
        b = browsers.configured({"browsers": {"own": {"kind": "local", "port": 1}}})["own"]
        profile = browsers.profile_dir(self.state, "own", b)
        os.makedirs(os.path.join(profile, "Default", "Sessions"))
        with open(os.path.join(profile, "Default", "Sessions", "Session_1"), "wb") as f:
            f.write(b"x")
        with open(os.path.join(profile, browsers.SEEDED), "w", encoding="utf-8") as f:
            f.write("")
        with open(os.path.join(profile, "Default", "Preferences"), "w", encoding="utf-8") as f:
            f.write("{}")
        now, noted = [0.0], []
        with mock.patch.object(run, "note_launch_tabs", lambda *a, **k: noted.append(a)):
            self.assertTrue(run.ensure_browser({}, self.state, "own", b, probe=lambda url: now[0] >= 1, launch=lambda *a: None,
                                               running=lambda p: None, clock=lambda: now[0],
                                               sleep=lambda dt: now.__setitem__(0, now[0] + dt)))
        self.assertEqual(noted, [])                                               # a restored about:blank tab is somebody's

    def test_no_wait_for_a_launch_tab_when_chrome_opens_another_page(self):
        # review of PR #322: with open set to a page Chrome starts with no about:blank — the first call did not need to wait 3 s
        from modules.browser import run
        b = browsers.configured({"browsers": {"own": {"kind": "local", "port": 1, "headless": False,
                                                       "open": "http://127.0.0.1:8787/"}}})["own"]
        now, noted = [0.0], []
        with mock.patch.object(run, "note_launch_tabs", lambda *a, **k: noted.append(a)):
            self.assertTrue(run.ensure_browser({}, self.state, "own", b, probe=lambda url: now[0] >= 1, launch=lambda *a: None,
                                               running=lambda p: None, clock=lambda: now[0],
                                               sleep=lambda dt: now.__setitem__(0, now[0] + dt)))
            self.assertEqual(noted, [])
            b["open"] = "about:blank"
            now[0] = 0.0
            self.assertTrue(run.ensure_browser({}, self.state, "own", b, probe=lambda url: now[0] >= 1, launch=lambda *a: None,
                                               running=lambda p: None, clock=lambda: now[0],
                                               sleep=lambda dt: now.__setitem__(0, now[0] + dt)))
            self.assertEqual(len(noted), 1)

    def test_one_launch_and_not_if_chrome_is_already_starting(self):
        from modules.browser import run
        b = browsers.configured({"browsers": {"own": {"kind": "local", "port": 1, "headless": False}}})["own"]
        launched, now = [], [0.0]
        def ensure(running, up_at=3.0):
            return run.ensure_browser({}, self.state, "own", b, probe=lambda url: now[0] >= up_at,
                                      launch=lambda *a: launched.append(a), running=running,
                                      clock=lambda: now[0], sleep=lambda dt: now.__setitem__(0, now[0] + dt))
        self.assertTrue(ensure(lambda p: None))
        self.assertEqual(len(launched), 1)
        now[0] = 0.0
        self.assertTrue(ensure(lambda p: 4242))
        self.assertEqual(len(launched), 1)
        from core import lock
        other = lock.acquire(os.path.join(self.state, "browsers", "own.launch.lock"))   # a neighbouring session is launching
        now[0] = 0.0
        self.assertTrue(ensure(lambda p: None))
        self.assertEqual(len(launched), 1)
        other.close()


class McpLauncherNeverRacesTheSupervisor(unittest.TestCase):
    """04.10: if the agents' Chrome was not running, every session tried to start a new one. After a hub restart,
    eight sessions waited for the browser for 5 s each and each launched Chrome itself (their Chrome were children of
    the session: they died on the next restart, and so on again). If the supervisor is running — wait for it; if not —
    one session launches, and only if no Chrome with this profile is running yet."""

    def setUp(self):
        from core import lock
        self.lock = lock
        self.state = tempfile.mkdtemp(prefix="mcpwait-")
        os.makedirs(os.path.join(self.state, "browsers"))
        self.b = {"kind": "local", "url": "http://127.0.0.1:1", "port": 1}
        self.launched, self.now, self.up_at = [], [0.0], 30.0

    def tearDown(self):
        from modules.browser import run
        if run._launch_lock:
            run._launch_lock.close()
            run._launch_lock = None

    def wait(self, running=lambda p: None):
        from modules.browser import run
        clock = lambda: self.now[0]  # noqa: E731
        def sleep(dt):
            self.now[0] += dt
        return run.wait_for_browser({}, self.state, "mac", self.b, probe=lambda url: self.now[0] >= self.up_at,
                                    launch=lambda *a: self.launched.append(a), running=running, clock=clock, sleep=sleep)

    def test_with_the_supervisor_running_it_only_waits(self):
        held = self.lock.acquire(os.path.join(self.state, "browsers.lock"))   # the supervisor holds its lock
        self.assertTrue(self.wait())
        self.assertEqual(self.launched, [])
        held.close()

    def test_without_the_supervisor_one_launch_and_not_if_chrome_is_already_starting(self):
        self.assertTrue(self.wait())
        self.assertEqual(len(self.launched), 1)
        self.launched.clear()
        self.now[0] = 0.0
        self.assertTrue(self.wait(running=lambda p: 4242))                  # Chrome with the profile is already starting
        self.assertEqual(self.launched, [])

    def test_two_launchers_at_once_one_launches(self):
        other = self.lock.acquire(os.path.join(self.state, "browsers", "mac.launch.lock"))   # a neighbouring session is launching
        self.assertTrue(self.wait())
        self.assertEqual(self.launched, [])
        other.close()


class HungTabs(unittest.TestCase):
    """01.10: browser:mac answered sessions with "Network.enable timed out" on list_pages/new_page — one tab
    (a heavy page left from earlier sessions) did not respond over CDP, and chrome-devtools-mcp hung on it. The
    supervisor checks the tabs and closes the one that fails to respond twice in a row."""

    def test_tab_hung_twice_in_a_row_is_closed(self):
        tabs = [{"id": "ok1", "type": "page", "webSocketDebuggerUrl": "ws://x/ok1"},
                {"id": "bad", "type": "page", "webSocketDebuggerUrl": "ws://x/bad"},
                {"id": "w", "type": "service_worker", "webSocketDebuggerUrl": "ws://x/w"}]
        closed, now = [], [0]
        sup = Supervisor(tempfile.mkdtemp(prefix="hung-"), clock=lambda: now[0])
        sup.tabs = lambda url: tabs
        sup.responds = lambda ws: not ws.endswith("/bad")
        sup.close_tab = lambda url, tid: closed.append(tid)
        sup.load = lambda: 0.5                             # do not depend on the load of the machine running the tests
        sup.reap("http://x", now[0])
        self.assertEqual(closed, [])                       # the first time — only remember
        now[0] += 10
        sup.reap("http://x", now[0])
        self.assertEqual(closed, [])                       # we check no more often than REAP_EVERY
        now[0] += 120
        sup.reap("http://x", now[0])
        self.assertEqual(closed, ["bad"])
        tabs[1]["id"] = "bad2"
        now[0] += 120
        sup.reap("http://x", now[0])
        self.assertEqual(closed, ["bad"])                  # a new tab — from the first time again

    def test_an_overloaded_machine_does_not_close_tabs(self):
        # 06.10: load ~195 — tabs took longer than 5 s to answer over CDP, the supervisor closed them as hung, and each
        # browser.reset re-raised sessions with MCP. Under such load a slow tab is not a hung one
        tabs = [{"id": "slow", "type": "page", "webSocketDebuggerUrl": "ws://x/slow"}]
        closed, now, load = [], [0], [400.0]
        sup = Supervisor(tempfile.mkdtemp(prefix="hung-"), clock=lambda: now[0])
        sup.tabs, sup.responds = (lambda url: tabs), (lambda ws: False)
        sup.close_tab = lambda url, tid: closed.append(tid)
        sup.load = lambda: load[0]
        for _ in range(3):
            self.assertFalse(sup.reap("http://x", now[0]))
            now[0] += 120
        self.assertEqual(closed, [])
        load[0] = 1.0                                      # the machine eased off — the usual rules, twice in a row
        sup.reap("http://x", now[0])
        now[0] += 120
        self.assertTrue(sup.reap("http://x", now[0]))
        self.assertEqual(closed, ["slow"])

    def test_without_a_load_average_tabs_are_still_checked(self):
        # Windows has no os.getloadavg: the AttributeError from reap() escaped into the supervisor loop
        from modules.browser import run

        def missing():
            raise AttributeError("getloadavg")
        tabs = [{"id": "bad", "type": "page", "webSocketDebuggerUrl": "ws://x/bad"}]
        closed, now = [], [0]
        sup = Supervisor(tempfile.mkdtemp(prefix="hung-"), clock=lambda: now[0])
        sup.tabs, sup.responds = (lambda url: tabs), (lambda ws: False)
        sup.close_tab = lambda url, tid: closed.append(tid)
        with mock.patch.object(run.os, "getloadavg", missing, create=True):
            sup.reap("http://x", now[0])
            now[0] += 120
            self.assertTrue(sup.reap("http://x", now[0]))
        self.assertEqual(closed, ["bad"])


class CleanStop(unittest.TestCase):
    """A Chrome that is killed may lose its tabs and session cookies (a cookie set less than 30 s before is not on disk yet).
    The supervisor asks it to close itself (CDP Browser.close) first and sends the signal only to what did not go."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="cleanstop-")
        self.addCleanup(platforms.current.rmtree, self.state)
        self.now, self.asked, self.killed, self.gone = [0.0], [], [], [False]
        self.sup = Supervisor(self.state, clock=lambda: self.now[0])
        self.sup.sleep = lambda dt: self.now.__setitem__(0, self.now[0] + dt)
        self.sup.close_browser = lambda url: self.asked.append(url) or self.heard
        self.sup.kill_tree = lambda proc: self.killed.append(proc.pid)
        self.sup.kill_pid = self.killed.append
        self.sup.listed = lambda: [(1, "x")]
        self.heard = True
        self.b = browsers.configured({"browsers": {"own": {"kind": "local", "port": 9}}})["own"]

    def proc(self, leaves_after=None):
        """A Chrome of ours; it exits once the clock passes leaves_after (never: None)."""
        class P:
            pid = 31
            poll = lambda me: 0 if leaves_after is not None and self.now[0] >= leaves_after else None
        self.sup.procs["own"] = P()

    def test_its_own_chrome_that_closes_is_not_killed(self):
        self.proc(leaves_after=1)
        self.assertTrue(self.sup.stop_chrome("own", self.b))
        self.assertEqual((self.asked, self.killed), (["http://127.0.0.1:9"], []))

    def test_its_own_chrome_that_stays_is_killed_after_the_wait(self):
        self.proc()
        self.assertTrue(self.sup.stop_chrome("own", self.b))
        self.assertEqual(self.killed, [31])
        self.assertAlmostEqual(self.now[0], run.CLOSE_WAIT, delta=0.5)

    def test_a_chrome_that_could_not_be_asked_is_killed_at_once(self):
        self.heard = False
        self.proc()
        self.sup.stop_chrome("own", self.b)
        self.assertEqual((self.killed, self.now[0]), ([31], 0.0))

    def test_a_chrome_of_a_past_life_is_asked_too_and_killed_by_its_pid_if_it_stays(self):
        alive = [777]
        self.sup.running = lambda profile: alive[0]
        self.assertTrue(self.sup.stop_chrome("own", self.b))
        self.assertEqual(self.killed, [777])
        self.killed.clear(), self.now.__setitem__(0, 0.0)
        self.sup.sleep = lambda dt: alive.__setitem__(0, None)   # it leaves while we wait
        self.assertTrue(self.sup.stop_chrome("own", self.b))
        self.assertEqual(self.killed, [])

    def test_a_chrome_of_another_program_on_the_port_is_not_asked_to_close(self):
        # Browser.close goes to the port, and a person's Chrome may hold it: only our own profile's Chrome is ever stopped
        self.sup.running = lambda profile: 777
        with mock.patch.object(run, "foreign", lambda port, profile, procs: 555):
            self.sup.stop_chrome("own", self.b)
        self.assertEqual((self.asked, self.killed), ([], [777]))

    def test_a_long_message_is_framed_for_cdp(self):
        import socket
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        message = {"id": 1, "method": "Runtime.evaluate", "params": {"expression": "x" * 300}}
        browsers._ws_send(a, message)
        head = b.recv(4)
        self.assertEqual((head[0], head[1]), (0x81, 0xFE))                        # 16-bit length follows
        size = int.from_bytes(head[2:4], "big")
        rest = b""
        while len(rest) < 4 + size:
            rest += b.recv(4 + size - len(rest))
        mask, body = rest[:4], rest[4:]
        self.assertEqual(json.loads(bytes(c ^ mask[i % 4] for i, c in enumerate(body))), message)

    def test_no_chrome_nothing_to_stop(self):
        self.sup.running = lambda profile: None
        self.assertFalse(self.sup.stop_chrome("own", self.b))
        self.assertEqual(self.asked, [])

    def test_browser_close_reaches_chrome_over_cdp(self):
        import base64
        import hashlib
        import socket
        import struct
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        got = []

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/json/version":
                    body = json.dumps({"webSocketDebuggerUrl": f"ws://127.0.0.1:{self.server.server_port}/devtools/browser/x"}).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                accept = base64.b64encode(hashlib.sha1((self.headers["Sec-WebSocket-Key"] +
                                                        "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
                self.send_response(101)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", accept)
                self.end_headers()
                head = self.rfile.read(2)
                n = head[1] & 0x7F
                mask = self.rfile.read(4)
                got.append(json.loads(bytes(b ^ mask[i % 4] for i, b in enumerate(self.rfile.read(n)))))
            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.assertTrue(browsers.close_browser(f"http://127.0.0.1:{srv.server_port}"))
        self.assertEqual(got, [{"id": 1, "method": "Browser.close"}])
        with socket.socket() as s:                                                # nothing listens there: not delivered
            s.bind(("127.0.0.1", 0))
            dead = s.getsockname()[1]
        self.assertFalse(browsers.close_browser(f"http://127.0.0.1:{dead}"))


class IdleClose(unittest.TestCase):
    """An agent Chrome started by a session's tool call (start: demand or login) lived until the machine stopped: windows
    and processes piled up. With idle_minutes the supervisor closes it once no session has called a tool of it for that
    long; the next call starts it again. Off by default (0); a window on the screen (headless: false) may hold the owner's
    sign-in in progress and is never closed for idleness. The clock, the last call and the processes are fakes."""

    def setUp(self):
        screen = mock.patch.object(browsers, "screenless", return_value=False)   # a window needs a screen, whatever this machine has
        screen.start()
        self.addCleanup(screen.stop)
        self.state = tempfile.mkdtemp(prefix="bridle-")
        self.addCleanup(platforms.current.rmtree, self.state)
        self.now, self.used, self.killed, self.pid = [0.0], [None], [], [777]
        self.sup = Supervisor(self.state, clock=lambda: self.now[0], probe=lambda url: "Chrome/156.0.0.0")
        self.sup.reap = lambda url, now: False
        self.sup.disk_version = lambda cfg, b=None: None
        self.sup.foreign = lambda name, b: None
        self.sup.active = lambda name: self.used[0]
        self.sup.running = lambda profile: self.pid[0]   # started by a session's launcher: closed by its pid
        self.sup.kill_pid = self.killed.append
        self.b = self.browser()

    def browser(self, **kw):
        return browsers.configured({"browsers": {"own": {"kind": "local", "port": 9, "start": "demand", "idle_minutes": 20,
                                                         **kw}}})["own"]

    def at(self, minutes, b=None):
        self.now[0] = minutes * 60.0
        return self.sup.check({}, "own", b or self.b)

    def test_settings_default_off_and_wrong_values_are_problems(self):
        conf = browsers.configured({"browsers": {
            "a": {"kind": "local"}, "d": {"kind": "local", "start": "demand"}, "l": {"kind": "local", "start": "login"},
            "x": {"kind": "local", "start": "demand", "idle_minutes": 5}, "neg": {"kind": "local", "idle_minutes": -1},
            "str": {"kind": "local", "idle_minutes": "5"}, "yes": {"kind": "local", "idle_minutes": True},
            "nan": {"kind": "local", "idle_minutes": float("nan")}}})
        self.assertEqual([conf[n]["idle_minutes"] for n in ("a", "d", "l", "x")], [0, 0, 0, 5])   # off: as before
        self.assertEqual([n for n in conf if conf[n].get("problem")], ["neg", "str", "yes", "nan"])
        self.assertEqual(conf["neg"]["problem"], i18n.t("cli.browser.bad_idle_minutes"))

    def test_an_unused_chrome_is_closed_after_the_limit(self):
        for m in (0, 5, 19):
            self.assertTrue(self.at(m)["up"])
        self.assertEqual(self.killed, [])
        st = self.at(20)
        self.assertEqual((st["up"], st.get("idle")), (False, True))
        self.assertEqual(self.killed, [777])

    def test_a_tool_call_postpones_the_closing(self):
        self.at(0)
        self.used[0] = 15 * 60.0                      # a session's tool call at the 15th minute
        self.assertTrue(self.at(34)["up"])
        self.assertTrue(self.at(35).get("idle"))
        self.assertEqual(self.killed, [777])

    def test_its_own_launch_is_killed_whole(self):
        proc = self.sup.procs["own"] = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        proc.poll = lambda: None                      # still running, as far as the supervisor knows
        whole = []
        self.sup.kill_tree = whole.append
        self.at(0)
        self.at(21)
        self.assertEqual((whole, self.killed), ([proc], []))
        self.assertNotIn("own", self.sup.procs)

    def test_never_without_a_limit_for_an_on_screen_window_or_an_always_browser(self):
        for b in (self.browser(idle_minutes=0), self.browser(headless=False), self.browser(start="login", headless=False),
                  self.browser(start="always")):
            with self.subTest(b={k: b[k] for k in ("start", "headless", "idle_minutes")}):
                self.sup.seen.clear()
                for m in (0, 60, 600):
                    self.assertTrue(self.at(m, b)["up"])
                self.assertEqual(self.killed, [])

    def test_a_login_browser_is_closed_too(self):
        b = self.browser(start="login")
        self.sup.logged["own"] = self.sup.boot()      # started on this boot already
        self.at(0, b)
        self.assertTrue(self.at(20, b).get("idle"))
        self.assertEqual(self.killed, [777])

    def test_the_count_starts_anew_when_chrome_comes_back(self):
        self.at(0)
        self.at(25)                                   # closed
        self.sup.probe = lambda url: None
        self.pid[0] = None
        self.assertTrue(self.at(26).get("idle"))      # down: start: demand waits for a call
        self.sup.probe, self.pid[0] = (lambda url: "Chrome/156.0.0.0"), 778
        self.assertTrue(self.at(27)["up"])            # a session's call started it again
        self.assertTrue(self.at(46)["up"])
        self.assertTrue(self.at(47).get("idle"))
        self.assertEqual(self.killed, [777, 778])


class IdleProxy(unittest.TestCase):
    def test_calls_and_replies_mark_the_browser_used_and_a_returned_browser_gets_a_new_own_tab(self):
        d = tempfile.mkdtemp(prefix="brprox-")
        self.addCleanup(platforms.current.rmtree, d)
        log, marks = os.path.join(d, "mcp.log"), os.path.join(d, "touched")
        # before the third call another Chrome answers (the first one was closed for idleness meanwhile); every touch is one character
        script = ("import sys; from modules.browser import run\n"
                  "asked = []\n"
                  "def touch():\n"
                  "    with open(sys.argv[1], 'a') as f: f.write('.')\n"
                  "def changed():\n"
                  "    asked.append(1)\n"
                  "    return len(asked) == 3\n"
                  "sys.exit(run.proxy(sys.argv[2:], None, lambda: None, touch=touch, changed=changed))")
        calls = "".join(json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": "list_pages"}})
                        + "\n" for i in (1, 2, 3))
        p = subprocess.run([sys.executable, "-c", script, marks, sys.executable, FAKE_MCP_STDIO, "--browserUrl",
                            "http://127.0.0.1:9"], cwd=os.path.join(HERE, ".."), env={**os.environ, "FAKE_MCP_LOG": log},
                           input=calls, capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual([json.loads(x)["id"] for x in p.stdout.splitlines()], [1, 2, 3])
        with open(marks) as f:
            self.assertGreaterEqual(len(f.read()), 6)   # every call and every reply
        with open(log, encoding="utf-8") as f:
            names = [(m.get("params") or {}).get("name") for m in map(json.loads, f)]
        self.assertEqual(names, ["new_page", "list_pages", "list_pages", "new_page", "list_pages", "close_page"])


class ChromeWatch(unittest.TestCase):
    """changed() of the go-between: another Chrome than at the previous call, whoever brought it back."""

    def test_another_chrome_is_a_change_and_a_missing_answer_or_the_same_one_is_not(self):
        answers = iter(["ws://x/devtools/browser/1", "ws://x/devtools/browser/1", None, "ws://x/devtools/browser/2",
                        "ws://x/devtools/browser/2"])
        changed = run.chrome_watch("http://127.0.0.1:9", identity=lambda url: next(answers))
        # the first call only remembers; the same Chrome; a slow answer (a busy machine) keeps the tab; a new Chrome; the same again
        self.assertEqual([changed() for _ in range(5)], [False, False, False, True, False])

    def test_a_closed_browser_has_no_identity(self):
        self.assertIsNone(browsers.identity(f"http://127.0.0.1:{free_port()}", timeout=0.3))


@unittest.skipIf(os.name == "nt", "fake Chrome is a .py instead of a program: Windows cannot launch it so")
class IdleCloseAndBack(unittest.TestCase):
    """The whole way with a fake Chrome and a fake chrome-devtools-mcp: a session's call starts the browser, the
    supervisor closes it for idleness, and the session's next call brings it back and works in a new own tab."""

    def setUp(self):
        screen = mock.patch.object(browsers, "screenless", return_value=False)   # a window needs a screen, whatever this machine has
        screen.start()
        self.addCleanup(screen.stop)
        self.state = tempfile.mkdtemp(prefix="bridleback-")
        self.port = free_port()
        self.old = os.environ.get("HUB_CONFIG")
        os.environ["HUB_CONFIG"] = os.path.join(self.state, "hub.json")
        with open(os.environ["HUB_CONFIG"], "w") as f:
            json.dump({"browsers": {"own": {"kind": "local", "port": self.port, "start": "demand", "idle_minutes": 1}},
                       "browser": {"chrome": FAKE_CHROME, "mcp": [sys.executable, FAKE_MCP_STDIO]}}, f)
        self.url = f"http://127.0.0.1:{self.port}"
        self.profile = os.path.join(self.state, "browsers", "own")

    def tearDown(self):
        pid = run.running(self.profile)
        if pid:
            run.kill_pid(pid)
        platforms.current.rmtree(self.state)
        os.environ["HUB_CONFIG"] = self.old or ""
        if self.old is None:
            os.environ.pop("HUB_CONFIG")

    def until(self, what, why):
        end = time.monotonic() + 20
        while not what():
            self.assertLess(time.monotonic(), end, why)
            time.sleep(0.1)

    def test_a_call_after_the_closing_brings_the_browser_back_with_a_working_mcp(self):
        log = os.path.join(self.state, "mcp.log")
        p = subprocess.Popen([sys.executable, HUB_BROWSER, "mcp", "own"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True,
                             env={**os.environ, "HUB_STATE_DIR": self.state, "FAKE_MCP_LOG": log})

        def call(i):
            p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                                      "params": {"name": "take_snapshot"}}) + "\n")
            p.stdin.flush()
            return json.loads(p.stdout.readline())
        try:
            self.assertEqual(call(1), {"id": 1, "method": "tools/call", "up": True, "result": OK})
            first = run.running(self.profile)
            used = browsers.last_active(self.state, "own")
            self.assertIsNotNone(used)                # the call marked the browser as used
            now = [used]
            sup = Supervisor(self.state, clock=lambda: now[0])
            sup.disk_version = lambda cfg, b=None: None
            sup.step()
            self.assertTrue(sup.reported["own"]["up"])
            now[0] = used + 59
            sup.step()
            self.assertTrue(sup.reported["own"]["up"])
            now[0] = used + 60                        # a minute without a call
            sup.step()
            self.assertTrue(sup.reported["own"].get("idle"))
            self.until(lambda: not browsers.probe(self.url, timeout=0.3), "Chrome was not closed")
            self.assertEqual(call(2), {"id": 2, "method": "tools/call", "up": True, "result": OK})   # back, and the MCP works
            self.assertNotIn(run.running(self.profile), (None, first))
            p.stdin.close()
            self.assertEqual(p.wait(timeout=10), 0, p.stderr.read())
        finally:
            p.kill()
            p.wait()
            for f in (p.stdin, p.stdout, p.stderr):
                f.close()
        with open(log) as f:
            names = [(m.get("params") or {}).get("name") for m in map(json.loads, f) if "answered" not in m]
        # its own tab went with the old Chrome: a new one before the call that came after the closing
        self.assertEqual(names, ["new_page", "take_snapshot", "new_page", "take_snapshot", "close_page"])

    def test_a_neighbour_that_brought_the_browser_back_does_not_leave_this_session_in_a_dead_tab(self):
        # two sessions; the Chrome is closed for idleness; session A calls first and starts a new Chrome, so for B the browser
        # is up when it calls — and still its own tab went with the old one
        logs, procs = {}, {}
        for who in ("A", "B"):
            logs[who] = os.path.join(self.state, f"mcp-{who}.log")
            procs[who] = subprocess.Popen([sys.executable, HUB_BROWSER, "mcp", "own"], stdin=subprocess.PIPE,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                          env={**os.environ, "HUB_STATE_DIR": self.state, "FAKE_MCP_LOG": logs[who]})

        def call(who, i):
            procs[who].stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                                               "params": {"name": "take_snapshot"}}) + "\n")
            procs[who].stdin.flush()
            return json.loads(procs[who].stdout.readline())
        try:
            self.assertTrue(call("A", 1)["up"])
            self.assertTrue(call("B", 1)["up"])
            used = browsers.last_active(self.state, "own")
            now = [used]
            sup = Supervisor(self.state, clock=lambda: now[0])
            sup.disk_version = lambda cfg, b=None: None
            sup.step()
            now[0] = used + 60
            sup.step()
            self.until(lambda: not browsers.probe(self.url, timeout=0.3), "Chrome was not closed")
            self.assertTrue(call("A", 2)["up"])   # A brings the browser back
            self.assertTrue(call("B", 2)["up"])   # B finds it up
            for who in procs:
                procs[who].stdin.close()
                self.assertEqual(procs[who].wait(timeout=10), 0, procs[who].stderr.read())
        finally:
            for who in procs:
                procs[who].kill()
                procs[who].wait()
                for f in (procs[who].stdin, procs[who].stdout, procs[who].stderr):
                    f.close()
        for who in logs:
            with open(logs[who]) as f:
                names = [(m.get("params") or {}).get("name") for m in map(json.loads, f) if "answered" not in m]
            self.assertEqual(names, ["new_page", "take_snapshot", "new_page", "take_snapshot", "close_page"], who)


class RunningProfile(unittest.TestCase):
    """The supervisor finds the Chrome of its profile by command line — to wait for it or to stop it."""

    def test_a_profile_whose_path_extends_ours_is_not_ours(self):
        # by substring, the profile …/browsers/vm matched the Chrome of profile …/browsers/vm2, and the supervisor stopped someone else's browser
        from modules.browser import run
        ps = ("  101 /opt/chrome --user-data-dir=/st/browsers/vm2 --remote-debugging-pipe\n"
              "  102 /opt/chrome --type=renderer --user-data-dir=/st/browsers/vm\n"
              "  103 /opt/chrome --user-data-dir=/st/browsers/vm/sub\n")
        out = subprocess.CompletedProcess([], 0, ps, "")
        with mock.patch.object(run.subprocess, "run", return_value=out):
            self.assertIsNone(run.running("/st/browsers/vm"))
            self.assertEqual(run.running("/st/browsers/vm2"), 101)
        out.stdout += "  104 /opt/chrome --user-data-dir=/st/browsers/vm --remote-debugging-port=9555\n"
        with mock.patch.object(run.subprocess, "run", return_value=out):
            self.assertEqual(run.running("/st/browsers/vm"), 104)
        out.stdout = "  105 /opt/chrome --user-data-dir=/st/my browsers/vm\n"   # a path with a space: the line ends there
        with mock.patch.object(run.subprocess, "run", return_value=out):
            self.assertEqual(run.running("/st/my browsers/vm"), 105)


def socket_dir(test):
    """A short folder for a socket, as Chrome's: a socket path is limited to ~100 bytes, the run's TMPDIR is longer."""
    folder = tempfile.mkdtemp(prefix="hub-sock-", dir="/tmp" if os.path.isdir("/tmp") else None)
    test.addCleanup(platforms.current.rmtree, folder)
    return folder


def hold_profile(test, profile):
    """What a live Chrome does to its profile (Windows: lockfile open with no sharing; elsewhere: SingletonLock to this
    host and a live pid, SingletonSocket a link to a listening socket). Returns the release."""
    os.makedirs(profile, exist_ok=True)
    if os.name == "nt":
        import _winapi
        handle = [_winapi.CreateFile(os.path.join(profile, "lockfile"), 0x40000000, 0, 0, 4, 0x80, 0)]  # write, no sharing
        return lambda: handle and _winapi.CloseHandle(handle.pop())
    lock, link = os.path.join(profile, "SingletonLock"), os.path.join(profile, "SingletonSocket")
    winsafe.symlink(test, f"{socket.gethostname()}-{os.getpid()}", lock)
    folder = socket_dir(test)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(os.path.join(folder, "SingletonSocket"))
    listener.listen(4)
    winsafe.symlink(test, os.path.join(folder, "SingletonSocket"), link)

    def release():
        listener.close()
        for path in (lock, link):
            if os.path.lexists(path):
                os.unlink(path)
    return release


class ProfileHeld(unittest.TestCase):
    """17.11: every session with an on-demand browser runs its own MCP proxy, and it launches Chrome when the CDP probe
    (1.5 s) does not answer and `ps` does not list a Chrome of the profile. On Windows there is no `ps` (on a loaded Mac it
    times out): on a loaded machine each slow probe started chrome.exe again, the live Chrome took the start over and
    opened one more empty window, several a day. The profile's own lock tells that Chrome is alive."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="held-")
        self.addCleanup(platforms.current.rmtree, self.state)
        self.b = browsers.configured({"browsers": {"own": {"kind": "local", "port": 1, "headless": False}}})["own"]
        self.profile = browsers.profile_dir(self.state, "own", self.b)

    def test_a_live_chrome_holds_its_profile_and_lets_it_go(self):
        self.assertFalse(browsers.profile_held(self.profile))          # no profile yet
        release = hold_profile(self, self.profile)
        self.assertTrue(browsers.profile_held(self.profile))
        release()
        self.assertFalse(browsers.profile_held(self.profile))          # Windows: the lockfile stays after a crash, unheld

    @unittest.skipIf(os.name == "nt", "SingletonLock is the POSIX lock")
    def test_a_lock_left_by_a_dead_chrome_or_another_host_is_not_held(self):
        os.makedirs(self.profile)
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        link = os.path.join(self.profile, "SingletonLock")
        winsafe.symlink(self, f"{socket.gethostname()}-{dead.pid}", link)
        self.assertFalse(browsers.profile_held(self.profile))
        os.unlink(link)
        winsafe.symlink(self, f"another-host-{os.getpid()}", link)
        self.assertFalse(browsers.profile_held(self.profile))

    @unittest.skipIf(os.name == "nt", "SingletonLock is the POSIX lock")
    def test_a_lock_whose_pid_went_to_another_live_process_is_not_held(self):
        # Chrome died and left SingletonLock; its pid went to an unrelated process, which is alive. Nothing listens on
        # SingletonSocket: Chrome is not there and is launched, as before.
        os.makedirs(self.profile)
        winsafe.symlink(self, f"{socket.gethostname()}-{os.getpid()}", os.path.join(self.profile, "SingletonLock"))
        self.assertFalse(browsers.profile_held(self.profile))
        gone = os.path.join(socket_dir(self), "SingletonSocket")                    # the dead Chrome's socket folder
        winsafe.symlink(self, gone, os.path.join(self.profile, "SingletonSocket"))
        self.assertFalse(browsers.profile_held(self.profile))                       # no file
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead.bind(gone)                                                             # a socket file, nobody listens
        dead.close()
        self.assertFalse(browsers.profile_held(self.profile))                       # ECONNREFUSED

    def test_windows_only_a_sharing_violation_is_held(self):
        # Chrome keeps the lockfile open with no sharing: opening it fails with ERROR_SHARING_VIOLATION (32). A read-only
        # lockfile or an ACL fails with ERROR_ACCESS_DENIED (5): no Chrome is there, it is launched.
        os.makedirs(self.profile)
        with open(os.path.join(self.profile, "lockfile"), "wb"):
            pass

        def failing(winerror):
            def opener(*a, **kw):
                e = PermissionError(13, "Permission denied")
                e.winerror = winerror
                raise e
            return opener
        for winerror, held in ((32, True), (5, False)):
            with mock.patch.object(browsers.os, "name", "nt"), \
                    mock.patch.object(browsers, "_open_shared", failing(winerror)):
                self.assertEqual(browsers.profile_held(self.profile), held, winerror)
        with mock.patch.object(browsers.os, "name", "nt"), mock.patch.object(browsers, "_open_shared", lambda p: None):
            self.assertFalse(browsers.profile_held(self.profile))          # opened: a lockfile left by a crash

    @staticmethod
    def drop_launch_lock():
        if run._launch_lock:
            run._launch_lock.close()
            run._launch_lock = None

    def test_a_slow_probe_does_not_launch_a_second_chrome(self):
        release = hold_profile(self, self.profile)
        self.addCleanup(release)
        launched, now = [], [0.0]
        clock, sleep = (lambda: now[0]), (lambda dt: now.__setitem__(0, now[0] + dt))
        with mock.patch.object(run, "listed", return_value=None):         # Windows: no ps; a Mac: ps timed out
            self.assertFalse(run.ensure_browser({}, self.state, "own", self.b, probe=lambda url: False,
                                                launch=lambda *a: launched.append(a), clock=clock, sleep=sleep, wait=2))
            self.assertEqual(launched, [])
            now[0] = 0.0
            with mock.patch.object(run, "supervised", return_value=False):
                self.addCleanup(self.drop_launch_lock)
                self.assertFalse(run.wait_for_browser({}, self.state, "own", self.b, probe=lambda url: False,
                                                      launch=lambda *a: launched.append(a), clock=clock, sleep=sleep))
            self.assertEqual(launched, [])
            release()
            now[0] = 0.0
            run.ensure_browser({}, self.state, "own", self.b, probe=lambda url: False,
                               launch=lambda *a: launched.append(a), clock=clock, sleep=sleep, wait=2)
            self.assertEqual(len(launched), 1)                            # Chrome gone: it is launched as before


class ResetReconnectsSessions(unittest.TestCase):
    """01.10: after a hung tab was closed, a session still got "Network.enable timed out" — its
    chrome-devtools-mcp kept the old state. The supervisor closed the tab — a browser.reset fact; sessions with this
    browser restart with the same conversation when idle (the MCP comes up again)."""

    def test_sessions_with_the_browser_get_restarted_before_their_next_message(self):
        # 06.10: under load (load ~195) the supervisor closed "hung" tabs every couple of minutes, and on each
        # browser.reset the core immediately re-raised all idle sessions with the browser together with their MCP — the
        # load grew, tabs hung again. Now a session is re-raised only before its own next message
        from core import reducer
        st, acts, seq = reducer.empty(), [], [0]

        def ev(t, e=None, **d):
            nonlocal st
            seq[0] += 1
            st, a, why = reducer.reduce(st, {"seq": seq[0], "ts": seq[0], "type": t, "entity": e, "data": d})
            acts.extend(a)
            return why
        for name, tools in (("a", ["browser:mac"]), ("b", ["browser:mac"]), ("c", [])):
            ev("session.requested", name, cwd="/w", session_id=f"s-{name}", tools=tools, channel="cli", owner=True)
            ev("session.started", name)
            ev("turn.started", name, marker=None, prompt_head="x")
            ev("turn.result", name, text="ok")
        ev("turn.started", "b", marker=None, prompt_head="working")
        acts.clear()
        self.assertIsNone(ev("browser.reset", "mac", why="a hung tab was closed"))
        self.assertEqual([a for a in acts if a["do"] in ("release", "spawn")], [])   # does not touch idle ones
        ev("turn.result", "b", text="ok")
        ev("turn.stopped", "b")
        self.assertEqual([a for a in acts if a["do"] in ("release", "spawn")], [])   # nor one that finished its turn
        ev("message.received", None, to="a", text="open the page", channel="web", owner=True)
        self.assertEqual([a["session"] for a in acts if a["do"] == "spawn"], ["a"])   # the message goes with a new MCP
        self.assertFalse([a for a in acts if a["do"] == "type"])
        ev("session.started", "a", session_id="s-a", source="resume")
        self.assertTrue([a for a in acts if a["do"] == "type" and a["session"] == "a"])   # and is delivered after the bring-up
        acts.clear()
        ev("message.received", None, to="c", text="no browser", channel="web", owner=True)
        self.assertFalse([a for a in acts if a["do"] == "spawn"])


class CoreView(unittest.TestCase):
    def test_status_fact_and_view(self):
        st = reducer.empty()
        evs = [("session.requested", "a", {"cwd": "/w", "session_id": "s", "tools": ["browser:vm"]}),
               ("browser.status", "vm", {"kind": "local", "url": "u", "up": False, "problem": "starting"}),
               ("browser.status", "vm", {"kind": "local", "url": "u", "up": True, "version": "V"}),
               ("browser.status", "own", {"kind": "local", "url": "u2", "up": False, "idle": True}),
               ("browser.status", "gone", {"kind": "remote", "up": False}),
               ("browser.status", "gone", {"removed": True})]
        r = views.Reader()
        for i, (t, e, d) in enumerate(evs, 1):
            ev = {"seq": i, "ts": 100.0 + i, "src": "t", "type": t, "entity": e, "data": d}
            st, _, why = reducer.reduce(st, ev)
            self.assertIsNone(why)
            r.feed(ev)
        self.assertEqual(st["browsers"]["vm"]["since"], 103.0)
        self.assertNotIn("gone", st["browsers"])
        view = r.snapshot()["browsers"]
        self.assertEqual([(b["name"], b["up"], b["idle"], b["version"], b["users"]) for b in view],
                         [("own", False, True, None, []), ("vm", True, None, "V", ["a"])])


if __name__ == "__main__":
    unittest.main()
