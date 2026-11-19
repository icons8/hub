"""hub doctor tells whether Claude Code is signed in (its own `claude auth status`, as the host asks before a spawn) and
that an update waits for modules (needs-module): without them doctor was green while every session got "Not logged in"
or the hub stood on an old release."""
import tests  # noqa: F401 — hide the machine's settings
import os
import sys
import tempfile
import unittest
import unittest.mock

import platforms
from core import journal, modcli, providers
from tests.fakes import cut


class DoctorLogin(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="doctor-login-")
        self.addCleanup(platforms.current.rmtree, self.root)
        self.state = os.path.join(self.root, "state")
        os.makedirs(os.path.join(self.state, "spool"))
        self.asked = []
        self.answer = None

    def login(self, claude, env):
        self.asked.append((claude, env))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def row(self, what, st=None):
        with unittest.mock.patch.object(journal, "load_state", lambda state_dir: st or {}):
            rows = modcli.checks(self.state, [sys.executable], login=self.login)
        got = [r for r in rows if r[1] == what]
        return got[0] if got else None

    def test_no_sign_in_without_a_login_module_fails(self):
        self.answer = {"loggedIn": False, "authMethod": "none"}
        level, _, detail = self.row("Claude sign-in")
        self.assertEqual(level, "fail")
        self.assertIn("claude /login", detail)
        self.assertEqual(self.asked[0][0], [sys.executable])

    def test_signed_in_is_ok_with_the_method(self):
        self.answer = {"loggedIn": True, "authMethod": "claude.ai", "email": "a@example.invalid"}
        self.assertEqual(self.row("Claude sign-in"), ("ok", "Claude sign-in", "claude.ai · a@example.invalid"))

    def test_no_sign_in_with_a_login_module_warns_that_sessions_hang_on_it(self):
        self.answer = {"loggedIn": False}
        st = {"providers": {"profile": {"state": "module", "module": "pool"}}}
        with unittest.mock.patch.object(providers, "active", lambda cap, cfg, recs: "pool" if cap == "profile" else None):
            level, _, detail = self.row("Claude sign-in", st)
        self.assertEqual(level, "warn")
        self.assertIn("pool", detail)

    @cut.needs("profile")
    def test_no_sign_in_with_the_built_in_pool_is_ok(self):
        # the hub's own accounts pool (accounts.json, no login module): sessions come up on its tokens — the normal
        # state, not a remark; no usable account in it (paused) — the failure it was
        import json
        from modules.accounts import accounts
        self.answer = {"loggedIn": False}
        cfg = os.path.join(self.root, "cfg")
        os.makedirs(cfg)
        pool = {"accounts": [{"name": "work", "token": "t" * 20}], "switch_at": 0.95}
        with unittest.mock.patch.dict(os.environ, {"HUB_CONFIG_DIR": cfg}):
            with open(accounts.path(), "w") as f:
                json.dump(pool, f)
            level, _, detail = self.row("Claude sign-in")
            self.assertEqual((level, detail), ("ok", "sessions sign in through the hub's accounts"))
            with open(accounts.path(), "w") as f:   # paused: the machine's own sign-in is the one sessions use
                json.dump({**pool, "off": True}, f)
            self.assertEqual(self.row("Claude sign-in")[0], "fail")

    @cut.needs("profile")
    def test_a_working_built_in_pool_is_ok_even_when_claude_does_not_answer(self):
        import json
        from modules.accounts import accounts
        self.answer = None
        cfg = os.path.join(self.root, "cfg")
        os.makedirs(cfg)
        with unittest.mock.patch.dict(os.environ, {"HUB_CONFIG_DIR": cfg}):
            with open(accounts.path(), "w") as f:
                json.dump({"accounts": [{"name": "work", "token": "t" * 20}], "switch_at": 0.95}, f)
            self.assertEqual(self.row("Claude sign-in")[0], "ok")

    def test_an_unknown_answer_or_a_crash_is_a_warning_not_a_failure(self):
        self.answer = None
        self.assertEqual(self.row("Claude sign-in")[0], "warn")
        self.answer = OSError("boom")
        self.assertEqual(self.row("Claude sign-in")[0], "warn")

    def test_from_a_session_its_own_login_keys_are_not_counted(self):
        self.answer = {"loggedIn": False}
        with unittest.mock.patch.dict(os.environ, {"HUB_SESSION": "maps", "CLAUDE_CODE_OAUTH_TOKEN": "x" * 20}):
            self.row("Claude sign-in")
        env = self.asked[0][1]
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)

    def test_a_key_in_the_hubs_environment_is_signed_in_without_asking(self):
        self.answer = {"loggedIn": False}
        with unittest.mock.patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "x" * 20}):
            level, _, detail = self.row("Claude sign-in")
        self.assertEqual(level, "ok")
        self.assertEqual(self.asked, [])                               # the host does not ask either

    def test_a_key_sessions_get_from_the_owners_claude_settings_counts(self):
        # sessions start with the login of the owner's Claude Code settings (catalog.user_env): so does the check —
        # not the doctor process's own environment (a red band over a hub whose sessions answered)
        import json
        self.answer = {"loggedIn": False}
        home = os.path.join(self.root, "claude")
        os.makedirs(home)
        with open(os.path.join(home, "settings.json"), "w") as f:
            json.dump({"env": {"CLAUDE_CODE_OAUTH_TOKEN": "x" * 20}}, f)
        with unittest.mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": home}):
            os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            level, _, _ = self.row("Claude sign-in")
        self.assertEqual((level, self.asked), ("ok", []))

    def test_the_whole_claude_command_is_asked_not_only_its_first_word(self):
        self.answer = {"loggedIn": True}
        with unittest.mock.patch.object(journal, "load_state", lambda state_dir: {}):
            modcli.checks(self.state, [sys.executable, "cli.js"], login=self.login)
        self.assertEqual(self.asked[0][0], [sys.executable, "cli.js"])

    def test_a_strange_update_check_does_not_bring_doctor_down(self):
        self.answer = {"loggedIn": True}
        self.assertIsNone(self.row("update", {"update_check": "x"}))

    def test_no_login_callable_no_row(self):
        with unittest.mock.patch.object(journal, "load_state", lambda state_dir: {}):
            rows = modcli.checks(self.state, [sys.executable])
        self.assertNotIn("Claude sign-in", [r[1] for r in rows])

    def test_an_update_waiting_for_modules_is_named(self):
        self.answer = {"loggedIn": True}
        st = {"update_check": {"result": "needs-module", "need_module": ["accounts: the module is still in the shadow"]}}
        level, _, detail = self.row("update", st)
        self.assertEqual(level, "warn")
        self.assertIn("accounts: the module is still in the shadow", detail)
        self.assertIsNone(self.row("update", {"update_check": {"result": "uptodate"}}))


if __name__ == "__main__":
    unittest.main()
