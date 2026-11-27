"""Codex (experimental): `hub codex login|status|logout` — the hub's
Codex sign-in by device code into its Codex folder (<state>/codex-home), with a fake app-server; from a session it is
refused (sign-in is the owner's word)."""
import tests  # noqa: F401 — do not see the machine's settings (~/.config/hub-core), see tests/__init__.py
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

import platforms

from core import codexcli, i18n

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE = os.path.join(ROOT, "tests", "fakes", "fake_codex.py")
ACCOUNT = {"account": {"type": "chatgpt", "email": "a@example.com", "planType": "prolite"}, "requiresOpenaiAuth": True}
NONE = {"account": None, "requiresOpenaiAuth": True}
START = {"type": "chatgptDeviceCode", "loginId": "l1", "userCode": "ABCD-1234",
         "verificationUrl": "https://auth.example.com/codex/device"}


@unittest.skipIf(os.name == "nt", "the fake wrapper is sh")
class CodexLogin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hubcxlogin-")
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.state)
        self.bin = os.path.join(self.tmp, "codex")
        with open(self.bin, "w") as f:
            f.write(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
        os.chmod(self.bin, 0o755)
        self.replies = os.path.join(self.tmp, "replies.json")

    def tearDown(self):
        platforms.current.rmtree(self.tmp)

    def hub(self, *args, replies, session=None):
        with open(self.replies, "w") as f:
            json.dump(replies, f)
        env = {k: v for k, v in os.environ.items() if k not in ("HUB_SESSION", "HUB_EXTENSION", "HUB_MODULE")}
        env.update(HUB_STATE_DIR=self.state, HUB_CODEX_BIN=self.bin, FAKE_CODEX_REPLIES=self.replies, HUB_CLI_WAIT="0",
                   HUB_CODEX_LOGIN_WAIT="5", **({"HUB_SESSION": session} if session else {}))
        return subprocess.run([sys.executable, os.path.join(ROOT, "bin", "hub"), "codex", *args], capture_output=True,
                              text=True, env=env, timeout=60)

    def spooled(self, type):
        out = []
        for p in glob.glob(os.path.join(self.state, "spool", "*.json")):
            with open(p) as f:
                item = json.load(f)
            if item["type"] == type:
                out.append(item["data"])
        return out

    def test_login_device_code_into_hub_home(self):
        r = self.hub("login", replies={"account/read": [NONE, ACCOUNT], "account/login/start": {
            "result": START, "notes": [{"method": "account/login/completed", "after_s": 0.2,
                                        "params": {"success": True, "loginId": "l1"}}]}})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("https://auth.example.com/codex/device", r.stdout)
        self.assertIn("ABCD-1234", r.stdout)
        self.assertIn("signed in as a@example.com, plan prolite", r.stdout)
        (data,) = self.spooled("codex.login")
        self.assertEqual((data["email"], data["plan"], data["channel"], data["owner"]),
                         ("a@example.com", "prolite", "cli", True))
        with open(os.path.join(self.state, "codex-home", "config.toml")) as f:
            self.assertIn('cli_auth_credentials_store = "file"', f.read())   # sign-in is a file in the hub's folder

    def test_login_from_session_refused(self):
        r = self.hub("login", replies={"account/read": NONE}, session="maps")
        self.assertEqual(r.returncode, 1)
        self.assertIn(i18n.tr("en", "cli.codex.owner_only_login", what="login"), r.stdout)   # a session: English
        self.assertEqual(self.spooled("codex.login"), [])

    def test_already_logged_in(self):
        r = self.hub("login", replies={"account/read": ACCOUNT})
        self.assertEqual(r.returncode, 1)
        self.assertIn("already signed in as a@example.com", r.stdout)

    def test_code_not_confirmed_cancels(self):
        r = self.hub("login", replies={"account/read": NONE, "account/login/start": START, "account/login/cancel": {}})
        self.assertEqual(r.returncode, 1)
        self.assertIn("sign-in cancelled", r.stdout)
        self.assertEqual(self.spooled("codex.login"), [])

    def test_login_failed(self):
        r = self.hub("login", replies={"account/read": NONE, "account/login/start": {
            "result": START, "notes": [{"method": "account/login/completed",
                                        "params": {"success": False, "error": "denied"}}]}})
        self.assertEqual(r.returncode, 1)
        self.assertIn("denied", r.stdout)

    def test_status_and_logout(self):
        read = {"ordinaryUsageAllowed": True, "rateLimits": {"primary": {
            "usedPercent": 7, "windowDurationMins": 10080, "resetsAt": 1792905741}}}
        r = self.hub("status", replies={"account/read": ACCOUNT, "account/rateLimits/read": read})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("signed in as a@example.com, plan prolite", r.stdout)
        self.assertIn("week 93% left", r.stdout)   # the share left, as Codex's /status
        r = self.hub("status", replies={"account/read": NONE})
        self.assertEqual(r.returncode, 1)
        self.assertIn("not signed in", r.stdout)
        r = self.hub("logout", replies={"account/logout": {}})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.spooled("codex.logout")), 1)

    def test_doctor_checks_codex_not_claude(self):
        cfg = os.path.join(self.tmp, "hub.json")
        with open(cfg, "w") as f:
            json.dump({"host": "codex"}, f)
        with open(self.replies, "w") as f:
            json.dump({"account/read": ACCOUNT, "account/rateLimits/read": {"rateLimits": {"primary": {
                "usedPercent": 7, "windowDurationMins": 10080}}}}, f)
        env = {k: v for k, v in os.environ.items() if k not in ("HUB_SESSION", "HUB_EXTENSION", "HUB_MODULE")}
        env.update(HUB_STATE_DIR=self.state, HUB_CODEX_BIN=self.bin, FAKE_CODEX_REPLIES=self.replies, HUB_CONFIG=cfg,
                   HUB_CLAUDE=os.path.join(self.tmp, "no-claude"))
        r = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "hub"), "doctor"], capture_output=True, text=True,
                           env=env, timeout=60, stdin=subprocess.DEVNULL)
        self.assertIn("Codex sign-in", r.stdout)
        self.assertIn("a@example.com, plan prolite", r.stdout)
        self.assertIn("week 93% left", r.stdout)   # the share left, as Codex's /status
        self.assertNotIn("install Claude Code", r.stdout)
        with open(self.replies, "w") as f:
            json.dump({"account/read": NONE}, f)
        r = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "hub"), "doctor"], capture_output=True, text=True,
                           env=env, timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not signed in — hub codex login", r.stdout)

    def test_page_login_in_background(self):
        with open(self.replies, "w") as f:
            json.dump({"account/read": [NONE, ACCOUNT], "account/login/start": {
                "result": START, "notes": [{"method": "account/login/completed", "after_s": 0.5,
                                            "params": {"success": True}}]}}, f)
        old = {k: os.environ.get(k) for k in ("HUB_CODEX_BIN", "FAKE_CODEX_REPLIES")}
        os.environ.update(HUB_CODEX_BIN=self.bin, FAKE_CODEX_REPLIES=self.replies)
        try:
            info = codexcli.start_login(self.state)
            self.assertEqual((info["url"], info["code"]), (START["verificationUrl"], "ABCD-1234"))
            self.assertEqual(codexcli.start_login(self.state)["code"], "ABCD-1234")   # already running — the same code
            end = time.time() + 15
            while time.time() < end and not (codexcli.page_status(self.state) or {}).get("result"):
                time.sleep(0.1)
            self.assertEqual(codexcli.page_status(self.state)["result"], {"ok": True, "why": None})
            (data,) = self.spooled("codex.login")
            self.assertEqual((data["channel"], data["owner"], data["email"]), ("web", True, "a@example.com"))
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


class SpawnWaitsForCodexLogin(unittest.TestCase):
    """Codex (experimental): a hub entirely on Codex with no sign-in yet does not spawn sessions whose every turn would
    fail with a 401 (stand 2026-10-29: a turn of the hub session went out and got a 401 at once) — it waits for the
    sign-in, as a Claude hub waits for `claude auth login` (test_spawn_waits_for_login): no spawn, no crash, the core's
    «waits for the sign-in» (restart does not wait for such sessions). The sign-in is the file in the hub's Codex folder
    (config.toml keeps it a file) or a key in the environment; a stat, no program start. Once `hub codex login` (or
    «Settings → Codex», «Setup») wrote it, the sessions spawn by themselves."""

    def setUp(self):
        from unittest import mock
        from hosts.codex import CodexHost
        from core.daemon import Hub
        self.tmp = tempfile.mkdtemp(prefix="hubcxwait-")
        self.addCleanup(platforms.current.rmtree, self.tmp)
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.state)
        conf = os.path.join(self.tmp, "hub.json")
        with open(conf, "w") as f:
            json.dump({"host": "codex"}, f)
        patch = mock.patch.dict(os.environ, {"HUB_CONFIG": conf})
        patch.start()
        self.addCleanup(patch.stop)
        for k in ("CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_API_KEY"):   # whatever the machine has
            os.environ.pop(k, None)
        self.now = [1_000_000.0]
        self.host = CodexHost(self.state, codex=(sys.executable, FAKE), all_sessions=True, default_model="gpt-test")
        self.addCleanup(self.host.kill_server)
        self.hub = Hub(self.state, self.host, registry_dir=os.path.join(self.tmp, "reg"), clock=lambda: self.now[0])

    def events(self, kind):
        from core import journal
        return [e for e in journal.read(self.hub.journal.path) if e["type"] == kind]

    def spawned(self):
        return [e["entity"] for e in self.events("host.done") if e["data"].get("do") == "spawn"
                and e["data"].get("result") == "spawned"]

    def passes(self, n, step=5):
        for _ in range(n):
            self.now[0] += step
            self.hub.run_delayed()
            self.hub.login_tick()

    def request(self, name="hub-core", **extra):
        self.hub.record("session.requested", name, {"cwd": self.tmp, "session_id": f"s-{name}", "channel": "cli",
                                                    "owner": True, **({"kind": "hub"} if name == "hub-core" else {}),
                                                    **extra})

    def sign_in(self):
        home = os.path.join(self.state, "codex-home")
        os.makedirs(home, exist_ok=True)
        with open(os.path.join(home, "auth.json"), "w") as f:
            f.write("{}")

    def test_no_sign_in_no_spawn_then_the_sessions_spawn_by_themselves(self):
        from core import restart
        self.request()
        self.request("maps", role="agent")
        self.passes(6)
        self.assertEqual(self.spawned(), [])
        self.assertEqual([e for e in self.events("host.error") if e["data"].get("do") == "spawn"], [])
        self.assertEqual(self.hub.waiting(), {"hub-core", "maps"})
        self.assertEqual(self.hub.state["sessions"]["hub-core"]["restarts"], 0)
        self.assertTrue(self.hub.state.get("login_missing"))
        self.assertEqual([e["data"] for e in self.events("core.login_missing")], [{"missing": True}])   # said once
        self.assertEqual(restart.blockers(self.hub.state), [])
        self.sign_in()
        self.passes(2)
        self.assertEqual(sorted(self.spawned()), ["hub-core", "maps"])
        self.assertEqual(self.hub.waiting(), set())
        self.assertNotIn("login_missing", self.hub.state)
        self.assertEqual([e["data"] for e in self.events("core.login_missing")], [{"missing": True}, {"missing": False}])

    def test_signed_in_spawns_at_once(self):
        self.sign_in()
        self.request()
        self.assertEqual(self.spawned(), ["hub-core"])
        self.assertEqual(self.events("core.login_missing"), [])

    def test_a_key_in_the_environment_is_no_sign_in(self):
        # codex app-server 0.162.1 has no account with CODEX_API_KEY, OPENAI_API_KEY or CODEX_ACCESS_TOKEN set
        for k in ("CODEX_API_KEY", "OPENAI_API_KEY", "CODEX_ACCESS_TOKEN"):
            os.environ[k] = "test-key-not-real"
        self.request()
        self.assertEqual(self.spawned(), [])
        self.assertIn("hub-core", self.hub.waiting())

    def test_a_codex_host_without_the_hub_setting_is_not_held(self):
        # the hub's settings make a hub entirely on Codex (core/daemon.make_host); a host built otherwise — as the core's
        # own tests do — and a Claude hub are never held
        with open(os.environ["HUB_CONFIG"], "w") as f:
            json.dump({}, f)
        self.hub.config = self.hub.read_config()
        self.request()
        self.assertEqual(self.spawned(), ["hub-core"])
        self.assertEqual(self.events("core.login_missing"), [])

    def test_a_signed_out_hub_holds_again(self):
        # hub codex logout: the sign-in file is gone — a new spawn waits (a Claude hub never asks again once a session
        # answered; Codex's file says it without guessing)
        self.sign_in()
        self.request("maps", role="agent")
        self.hub.record("turn.result", "maps", {"text": "done", "turns": 1, "engine": "codex"})
        os.unlink(os.path.join(self.state, "codex-home", "auth.json"))
        self.request()
        self.assertEqual(self.spawned(), ["maps"])
        self.assertIn("hub-core", self.hub.waiting())



class CodexLimitsLine(unittest.TestCase):
    """hub codex limits says a window as Codex's own /status does — the share left ("47% left"): the owner compares the
    two, and the share used ("53%") read as a stale or wrong number (2026-11-26)."""

    def test_the_share_left(self):
        res = {"rateLimits": {"primary": {"usedPercent": 53, "windowDurationMins": 10080, "resetsAt": None}},
               "ordinaryUsageAllowed": True}
        self.assertEqual(codexcli.windows_line(res), i18n.t("cli.codex.window_seven_day") + " 47% left")
        i18n.use("ru")
        self.addCleanup(i18n.use, "en")
        self.assertEqual(codexcli.windows_line(res), i18n.t("cli.codex.window_seven_day") + " " + i18n.tr("ru", "web.limits.left", pct=47))

    def test_never_below_zero(self):
        # PR #783 review: a reading over the limit (usedPercent 120) said "-20% left"; the page clamps at 0 (limitpct.js)
        res = {"rateLimits": {"primary": {"usedPercent": 120, "windowDurationMins": 10080, "resetsAt": None}},
               "ordinaryUsageAllowed": False}
        self.assertTrue(codexcli.windows_line(res).startswith(i18n.t("cli.codex.window_seven_day") + " 0% left"),
                        codexcli.windows_line(res))

if __name__ == "__main__":
    unittest.main()
