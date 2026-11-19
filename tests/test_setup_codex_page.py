"""Codex (experimental): the page's «Setup» of a hub entirely on Codex — the band asks to sign in to
Codex (not to Claude), and «Sign in» starts the hub's device-code sign-in (POST /api/codex/login, as «Settings →
Codex») and shows the code to enter and Codex's link; there is no «Setup» tab to go to."""
import tests  # noqa: F401
from unittest import mock

from core import i18n
from tests import test_web
from tests.test_web_e2e import PageInChrome

URL = "https://auth.example.com/codex/device"


class CodexSetupPage(PageInChrome):
    def test_the_band_leads_to_the_codex_sign_in_and_shows_its_code(self):
        started = []
        phase = ["todo"]

        class Stub(test_web.ReadySetup):
            def status(self, local=False, full=True, working=False):
                login = {"id": "codex_login", "state": "todo", "job": None} if phase[0] == "todo" else \
                    {"id": "codex_login", "state": "running", "job": {"state": "running", "url": URL, "code": "ABCD-1234"}}
                return {"ready": False, "blocking": ["codex_login"], "local": False, "auto": [], "items": [
                    {"id": "codex", "state": "ok", "version": "0.162.1", "path": "/x/codex"}, login,
                    {"id": "chrome", "state": "ok", "path": "/x/chrome", "job": None},
                    {"id": "doctor", "state": "ok", "rows": [["ok", "hubd", "ok"]], "ts": 1, "stale": False, "job": None}]
                    if full else [{"id": "codex", "state": "ok", "version": "0.162.1"}, login]}

        def start_login(state_dir, wait=15.0):
            started.append(state_dir)
            phase[0] = "running"
            return {"url": URL, "code": "ABCD-1234"}

        self.web._setup = Stub()
        real = self.web.config
        self.enterContext(mock.patch.object(self.web, "config", lambda: {**real(), "host": "codex"}))
        self.enterContext(mock.patch("core.codexcli.start_login", start_login))
        self.enterContext(mock.patch("core.codexcli.page_status", lambda state_dir: None))
        # the band itself gives the sign-in (a second hub put with "Engine: Codex" lands here first, the spec of
        # several hubs, 2.12): its button starts the device code, the code and Codex's link come into the band
        got = self.page({"go": f"http://127.0.0.1:{self.port}/#/"},
                        {"until": "!!document.querySelector('.setupbar:not([hidden]) .btn')", "ms": 10000},
                        {"eval": "document.querySelector('.setupbar-line').textContent"},
                        {"eval": "[...document.querySelectorAll('.setupbar .btn')].map((b) => b.textContent)"},
                        {"eval": "document.querySelector('.setupbar .btn.primary').click(), 1"},
                        {"until": "((document.querySelector('.setupbar .su-device-code')||{}).textContent||'')"
                                  " === 'ABCD-1234'", "ms": 10000},
                        {"eval": "document.querySelector('.setupbar a.btn.primary').href"},
                        # Settings has no «Setup» tab, and About has no card: the engine and the check are done
                        {"eval": "location.hash = '#/settings', 1"},
                        {"until": "!!document.querySelector('[data-settings-tab=about]')", "ms": 10000},
                        {"sleep": 800},
                        {"eval": "[!!document.querySelector('[data-settings-tab=setup]'), document.querySelectorAll('.su-item').length]"})
        self.assertTrue(got[1]["until"] and got[5]["until"] and got[8]["until"], got)
        self.assertIn(got[2]["eval"], {i18n.tr(x, "web.setup.cx_g_login_todo") for x in ("en", "ru")})
        # no "Claude instead": a hub from git chooses its engine in hub.json (/api/setup/host is the app's)
        self.assertEqual(len(got[3]["eval"]), 1, got[3])
        self.assertEqual(got[6]["eval"], URL)
        self.assertEqual(got[10]["eval"], [False, 0])
        self.assertEqual(len(started), 1)


def load_tests(loader, standard, pattern):
    import unittest
    return unittest.TestSuite(CodexSetupPage(n) for n in CodexSetupPage.__dict__ if n.startswith("test_"))
