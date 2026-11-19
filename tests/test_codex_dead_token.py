"""Codex: a sign-in whose token is dead (a revoked refresh token, the app-server answers 401 to every account request)
must not trap the hub: "Sign out" always ends at "not signed in", a sign-in goes on from a clean folder; a network
error is not a dead token — the sign-in file stays (tests/fakes/codex_deadauth.py, no model)."""
import tests  # noqa: F401 — do not see the machine's settings (~/.config/hub-core), see tests/__init__.py
import glob
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import platforms

from core import codexcli

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE = os.path.join(ROOT, "tests", "fakes", "codex_deadauth.py")


@unittest.skipIf(os.name == "nt", "the fake wrapper is sh")
class DeadToken(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hubcxdead-")
        self.state = os.path.join(self.tmp, "state")
        self.home = codexcli.home_of(self.state)
        os.makedirs(self.home)
        self.auth = os.path.join(self.home, "auth.json")
        with open(self.auth, "w") as f:
            json.dump({"tokens": {"refresh_token": "revoked"}}, f)
        self.bin = os.path.join(self.tmp, "codex")
        with open(self.bin, "w") as f:
            f.write(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
        os.chmod(self.bin, 0o755)
        self.calls = os.path.join(self.tmp, "calls.jsonl")

    def tearDown(self):
        platforms.current.rmtree(self.tmp)

    def env(self, dead, err=None):
        env = {k: v for k, v in os.environ.items() if k not in ("HUB_SESSION", "HUB_EXTENSION", "HUB_MODULE")}
        env.update(HUB_STATE_DIR=self.state, HUB_CODEX_BIN=self.bin, FAKE_DEAD=dead, HUB_CLI_WAIT="0",
                   HUB_CODEX_LOGIN_WAIT="5", FAKE_CODEX_CALLS=self.calls)
        if err:
            env["FAKE_ERR"] = err
        return env

    def hub(self, *args, dead="401", err=None):
        return subprocess.run([sys.executable, os.path.join(ROOT, "bin", "hub"), "codex", *args], capture_output=True,
                              text=True, env=self.env(dead, err), timeout=60)

    def spooled(self, type):
        out = []
        for p in glob.glob(os.path.join(self.state, "spool", "*.json")):
            with open(p) as f:
                item = json.load(f)
            if item["type"] == type:
                out.append(item["data"])
        return out

    def methods(self):
        with open(self.calls) as f:
            return [json.loads(line)["method"] for line in f if line.strip()]

    def test_logout_with_a_dead_token_ends_signed_out(self):
        r = self.hub("logout")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.auth))
        self.assertEqual(len(self.spooled("codex.logout")), 1)

    def test_logout_when_initialize_is_refused(self):
        r = self.hub("logout", dead="init")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.auth))
        self.assertEqual(len(self.spooled("codex.logout")), 1)

    def test_login_with_a_dead_token_goes_on_from_clean(self):
        r = self.hub("login", dead="read")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ABCD-1234", r.stdout)
        self.assertIn("signed in as new@example.com", r.stdout)
        self.assertIn("account/login/start", self.methods())
        (data,) = self.spooled("codex.login")
        self.assertEqual(data["email"], "new@example.com")

    def test_login_with_a_dead_token_that_cannot_log_out(self):
        r = self.hub("login", dead="401")   # the app-server refuses the sign-out too: the file goes by the core's hand
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("account/login/start", self.methods())

    def test_login_when_initialize_is_refused(self):
        r = self.hub("login", dead="init")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("account/login/start", self.methods())

    def test_network_down_keeps_the_sign_in_file(self):
        for err in ("error sending request for url (https://chatgpt.com/backend-api/wham/workspaces): dns error",
                    "failed to refresh token: error sending request for url (http://127.0.0.1:4010/oauth/token)",
                    "request timed out after 4010 ms: 4012 bytes"):
            for what in ("logout", "login"):
                r = self.hub(what, err=err)
                self.assertEqual(r.returncode, 1, err + r.stdout + r.stderr)
                self.assertTrue(os.path.exists(self.auth), what + err)
        self.assertEqual(self.spooled("codex.logout"), [])
        self.assertNotIn("account/login/start", self.methods())

    def test_the_words_of_a_dead_token(self):
        for err in ("workspace routing discovery unauthorized (401)", "refresh token was revoked",
                    "Your access token could not be refreshed. Please log out and sign in again."):
            self.assertTrue(codexcli.dead_token(RuntimeError(err)), err)
        for err in ("error sending request for url (http://h:4010/x)", "codex app-server closed",
                    "failed to refresh token: connection reset"):
            self.assertFalse(codexcli.dead_token(RuntimeError(err)), err)

    def test_login_again_with_a_dead_token(self):   # a lost account of the pool: sign out first, the sign-out is refused
        with mock.patch.dict(os.environ, self.env("401")):
            ok, email, plan, why = codexcli.login_flow(self.state, lambda url, code: None, wait=5, again=True)
        self.assertEqual((ok, email, why), (True, "new@example.com", None))
        self.assertEqual(self.methods().count("account/login/start"), 1)

    def test_pool_account_logout_removes_only_its_auth_file(self):
        acc = os.path.join(self.state, "moddata", "codex", "acc-1")
        os.makedirs(os.path.join(acc, "skills"))
        with open(os.path.join(acc, "auth.json"), "w") as f:
            f.write("{}")
        with open(os.path.join(acc, "config.toml"), "w") as f:
            f.write("x = 1\n")
        env = self.env("401")
        env["CODEX_HOME"] = "ignored"
        code = ("import sys; sys.path.insert(0, %r); from core import codexcli; codexcli.logout_home(%r, %r)"
                % (ROOT, self.state, acc))
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(os.listdir(acc)), ["config.toml", "skills"])
        self.assertTrue(os.path.exists(self.auth))   # the hub's own sign-in is another folder's


if __name__ == "__main__":
    unittest.main()
