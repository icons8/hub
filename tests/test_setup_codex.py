"""Codex (experimental): «Setup» of a hub entirely on Codex (hub.json "host": "codex"). Its sessions
need the codex program and the hub's Codex sign-in (<state>/codex-home), not Claude Code: the checklist and the band
ask for those two, never install Claude Code and never start claude; the codex probes run with the hub's CODEX_HOME
(without it even `codex --version` writes into the personal ~/.codex). A Claude hub keeps its checklist as in main and
never starts codex. The file for support names the codex version on a Codex hub."""
import tests  # noqa: F401 — keep the machine's settings out (~/.config/hub-core), see tests/__init__.py
import io
import json
import os
import shutil
import tempfile
import time
import unittest
import zipfile
from contextlib import redirect_stdout
from unittest import mock

import platforms
from core import i18n, supportbundle
from modules.setup import setup
from tests.fakes import exe

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE_CODEX = os.path.join(HERE, "fakes", "fake_codex.py")
ACCOUNT = {"account": {"type": "chatgpt", "email": "a@example.com", "planType": "plus"}, "requiresOpenaiAuth": True}
NONE = {"account": None, "requiresOpenaiAuth": True}

# codex: `--version` answers here, everything else is the fake app-server; every start is a line in FAKE_CODEX_RUNS
CODEX = r'''
import json, os, runpy, sys
with open(os.environ["FAKE_CODEX_RUNS"], "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "home": os.environ.get("CODEX_HOME")}) + "\n")
if sys.argv[1:] == ["--version"]:
    print("codex-cli 0.162.1")
    sys.exit(0)
sys.argv[0] = os.environ["FAKE_CODEX_PY"]
runpy.run_path(os.environ["FAKE_CODEX_PY"], run_name="__main__")
'''
# claude must never start on a Codex hub
CLAUDE = r'''
import os, sys
open(os.environ["FAKE_CLAUDE_RAN"], "a").write(" ".join(sys.argv[1:]) + "\n")
print("2.1.309 (Claude Code)")
'''


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hubsetupcx-")
        self.addCleanup(platforms.current.rmtree, self.tmp)
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.state)
        self.runs = os.path.join(self.tmp, "codex-runs.jsonl")
        self.ran_claude = os.path.join(self.tmp, "claude-ran")
        self.replies = os.path.join(self.tmp, "replies.json")
        self.reply(NONE)
        self.codex = exe.script(os.path.join(self.tmp, "codex"), CODEX)
        self.claude = exe.script(os.path.join(self.tmp, "claude"), CLAUDE)
        self.cfg_path = os.path.join(self.tmp, "hub.json")
        self.config({"host": "codex"})
        env = {"HUB_CODEX_BIN": self.codex, "FAKE_CODEX_RUNS": self.runs, "FAKE_CODEX_PY": FAKE_CODEX,
               "FAKE_CODEX_REPLIES": self.replies, "FAKE_CLAUDE_RAN": self.ran_claude, "HUB_STATE_DIR": self.state,
               "HUB_CONFIG": self.cfg_path}
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)
        for k in ("CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_API_KEY"):   # whatever the machine has
            os.environ.pop(k, None)
        # what this machine's AppArmor says about the Chrome sandbox is not the test's business
        sandbox = mock.patch("modules.testchrome.chrome.USERNS_SWITCH", os.path.join(self.tmp, "no-such-switch"))
        sandbox.start()
        self.addCleanup(sandbox.stop)

    def config(self, cfg):
        self.cfg = cfg
        with open(self.cfg_path, "w") as f:
            json.dump(cfg, f)

    def reply(self, account):
        with open(self.replies, "w") as f:
            json.dump({"account/read": account}, f)

    def make(self):
        return setup.Setup(self.state, lambda: self.cfg, claude=[self.claude], repo=self.tmp)

    def codex_runs(self):
        try:
            with open(self.runs) as f:
                return [json.loads(x) for x in f]
        except FileNotFoundError:
            return []

    def servers(self):
        return [r for r in self.codex_runs() if r["argv"][:1] == ["app-server"]]

    def sign_in(self):
        home = os.path.join(self.state, "codex-home")
        os.makedirs(home, exist_ok=True)
        with open(os.path.join(home, "auth.json"), "w") as f:
            f.write('{"auth_mode": "chatgpt"}')

    def item(self, st, id):
        return next(i for i in st["items"] if i["id"] == id)


class CodexChecklist(Base):
    def test_a_codex_hub_asks_for_codex_and_its_sign_in_not_for_claude(self):
        st = self.make().status()
        self.assertEqual([i["id"] for i in st["items"]], ["codex", "codex_login", "chrome", "doctor"])
        self.assertEqual((st["ready"], st["blocking"], st["auto"]), (False, ["codex_login"], []))
        self.assertEqual((self.item(st, "codex")["state"], self.item(st, "codex")["version"]), ("ok", "0.162.1"))
        self.assertEqual(self.item(st, "codex_login")["state"], "todo")
        self.assertFalse(os.path.exists(self.ran_claude))   # claude never started
        (ver,) = self.codex_runs()
        self.assertEqual((ver["argv"], ver["home"]), (["--version"], os.path.join(self.state, "codex-home")))
        self.assertEqual(self.servers(), [])   # no sign-in file: signed out without a program start

    def test_no_codex_holds_the_hub_and_nothing_installs_by_itself(self):
        os.environ["HUB_CODEX_BIN"] = os.path.join(self.tmp, "none")
        st = self.make().status()
        self.assertEqual((st["ready"], st["blocking"], st["auto"]), (False, ["codex", "codex_login"], []))
        self.assertEqual([self.item(st, x)["state"] for x in ("codex", "codex_login")], ["todo", "wait"])
        self.assertFalse(os.path.exists(self.ran_claude))

    def test_signed_in_is_green_with_its_account(self):
        self.sign_in()
        self.reply(ACCOUNT)
        st = self.make().status()
        self.assertTrue(st["ready"], st)
        login = self.item(st, "codex_login")
        self.assertEqual((login["state"], login["via"], login["account"]), ("ok", "own", {"email": "a@example.com",
                                                                                       "plan": "plus"}))
        (srv,) = self.servers()
        self.assertEqual(srv["home"], os.path.join(self.state, "codex-home"))   # the hub's Codex folder, not ~/.codex
        self.make().status()
        s = self.make()
        s.status()
        s.status()
        self.assertEqual(len(self.servers()), 3)   # one app-server per Setup while the answer is fresh (cached)

    def test_a_sign_in_file_codex_does_not_accept_is_red(self):
        self.sign_in()
        self.reply(NONE)
        st = self.make().status()
        self.assertEqual((st["ready"], self.item(st, "codex_login")["state"]), (False, "todo"))

    def test_a_key_in_the_environment_is_no_sign_in(self):
        # codex app-server 0.162.1 answers account/read null with CODEX_API_KEY, OPENAI_API_KEY or CODEX_ACCESS_TOKEN set
        for k in ("CODEX_API_KEY", "OPENAI_API_KEY", "CODEX_ACCESS_TOKEN"):
            os.environ[k] = "test-key-not-real"
        st = self.make().status()
        self.assertEqual((st["ready"], self.item(st, "codex_login")["state"]), (False, "todo"))
        self.assertEqual(self.servers(), [])

    def test_a_new_sign_in_is_asked_anew_and_the_cache_does_not_grow(self):
        self.sign_in()
        self.reply(NONE)
        s = self.make()
        self.assertEqual(self.item(s.status(), "codex_login")["state"], "todo")
        self.reply(ACCOUNT)
        path = os.path.join(self.state, "codex-home", "auth.json")
        for i in range(3):   # a sign-in, then refreshes: Codex rewrites the file
            os.utime(path, ns=(10 ** 9 * (i + 1), 10 ** 9 * (i + 1)))
            self.assertEqual(self.item(s.status(), "codex_login")["state"], "ok")
        self.assertEqual(len([k for k in s.cache if str(k).startswith("codex_account")]), 1)

    def test_the_band_of_a_working_hub_starts_no_app_server(self):
        self.sign_in()
        st = self.make().status(full=False, working=True)
        self.assertTrue(st["ready"])
        self.assertEqual([i["id"] for i in st["items"]], ["codex", "codex_login"])
        self.assertEqual(self.item(st, "codex_login")["via"], "working")
        self.assertEqual(self.servers(), [])

    def test_a_working_hub_signed_out_is_red(self):
        # hub codex logout on a hub whose sessions answered: its spawns wait for a sign-in (core/daemon.wait_codex_login),
        # so the band says so — the file tells it without guessing, unlike Claude's `auth status`
        st = self.make().status(full=False, working=True)
        self.assertEqual((st["ready"], st["blocking"]), (False, ["codex_login"]))
        self.assertEqual(self.servers(), [])

    def test_a_sign_in_from_the_page_shows_its_link_and_code_then_how_it_ended(self):
        d = os.path.join(self.state, "codex-login")
        os.makedirs(d)
        with open(os.path.join(d, "pid.json"), "w") as f:
            json.dump({"pid": os.getpid(), "ts": time.time()}, f)
        with open(os.path.join(d, "info.json"), "w") as f:
            json.dump({"url": "https://auth.example.com/codex/device", "code": "ABCD-1234", "pid": os.getpid()}, f)
        login = self.item(self.make().status(), "codex_login")
        self.assertEqual((login["state"], login["job"]["url"], login["job"]["code"]),
                         ("running", "https://auth.example.com/codex/device", "ABCD-1234"))
        with open(os.path.join(d, "result.json"), "w") as f:
            json.dump({"ok": False, "why": "the code expired"}, f)
        login = self.item(self.make().status(), "codex_login")
        self.assertEqual((login["state"], login["job"]["why"]), ("failed", "the code expired"))
        self.assertNotIn("code", login["job"])   # an ended sign-in shows no code to enter
        with open(os.path.join(d, "result.json"), "w") as f:
            json.dump({"ok": True, "why": None}, f)
        self.assertEqual(self.item(self.make().status(), "codex_login")["state"], "todo")   # no sign-in file after all

    def test_a_sign_in_whose_process_is_gone_is_not_running(self):
        d = os.path.join(self.state, "codex-login")
        os.makedirs(d)
        for pid, ts in ((2 ** 22 + 12345, time.time()), (os.getpid(), time.time() - 3600)):   # gone; a pid reused since
            with open(os.path.join(d, "pid.json"), "w") as f:
                json.dump({"pid": pid, "ts": ts}, f)
            login = self.item(self.make().status(), "codex_login")
            self.assertEqual((login["state"], login["job"]["why"]), ("failed", "gone"))

    def test_hub_setup_in_the_terminal(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = setup.cli(self.state, [self.claude])
        self.assertEqual(code, 1)
        text = out.getvalue()
        self.assertIn(i18n.t("cli.setup.cx_g_codex"), text)
        self.assertIn(i18n.t("cli.setup.cx_g_codex_login"), text)
        self.assertNotIn(i18n.t("cli.setup.item_claude"), text.replace(i18n.t("cli.setup.cx_g_codex"), ""))
        self.assertFalse(os.path.exists(self.ran_claude))
        self.sign_in()
        self.reply(ACCOUNT)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(setup.cli(self.state, [self.claude]), 0)
        self.assertIn("a@example.com", out.getvalue())


class ClaudeHubAsMain(Base):
    def test_a_claude_hub_keeps_its_checklist_and_never_starts_codex(self):
        self.config({})
        st = self.make().status()
        self.assertEqual([i["id"] for i in st["items"]][:2], ["claude", "login"])
        self.assertNotIn("codex", [i["id"] for i in st["items"]])
        self.assertEqual(self.codex_runs(), [])
        self.assertFalse(os.path.isdir(os.path.join(self.state, "codex-home")))


class SupportBundle(Base):
    def test_a_codex_hub_names_the_codex_version_taken_with_its_own_folder(self):
        self.sign_in()
        token = "tok-" + "q" * 30
        with open(os.path.join(self.state, "codex-home", "auth.json"), "w") as f:
            json.dump({"tokens": {"refresh_token": token}}, f)
        _, data = supportbundle.build(self.state, claude=[self.claude], doctor_text="ok\n")
        z = zipfile.ZipFile(io.BytesIO(data))
        self.assertEqual(json.loads(z.read("versions.json"))["codex"], "codex-cli 0.162.1")
        self.assertEqual([r["home"] for r in self.codex_runs()], [os.path.join(self.state, "codex-home")])
        self.assertNotIn(token.encode(), data)

    def test_a_claude_hub_starts_no_codex(self):
        self.config({})
        _, data = supportbundle.build(self.state, claude=[self.claude], doctor_text="ok\n")
        z = zipfile.ZipFile(io.BytesIO(data))
        self.assertNotIn("codex", json.loads(z.read("versions.json")))
        self.assertEqual(self.codex_runs(), [])


if __name__ == "__main__":
    unittest.main()
