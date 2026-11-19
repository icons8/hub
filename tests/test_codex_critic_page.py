"""Codex (experimental): «Settings → Codex» after the sign-in offers the critic (another model checks the important
decisions before the owner does) while it is off: a line and «Enable» — the Modules card's switch (POST /api/modules/toggle,
critic.enabled); on — a short line, no button. Before the sign-in, or from a server that sends no such field — no row.
On a hub on Codex the critic's model is one of the sign-in's own list (modules/critic: codex_fit)."""
import tests  # noqa: F401
import os
import time
from unittest import mock

from core import catalog, i18n

from tests.test_web_e2e import PageInChrome

ROW = "document.querySelector('.cx-critic')"
OFF = {i18n.tr(x, "web.codex.critic_off") for x in ("en", "ru")}
ON = {i18n.tr(x, "web.codex.critic_on") for x in ("en", "ru")}


class CodexCriticPage(PageInChrome):
    def critic_off(self):
        """hub.json outlives a test (the config folder is the run's): the critic starts and ends off."""
        with catalog.edit_settings(catalog.config_path(self.state)) as cfg:
            cfg.pop("critic", None)

    def hub(self, signed=True):
        self.critic_off()
        self.addCleanup(self.critic_off)
        self.ev("codex.host", None, on=True, whole=True, channel="core", owner=False)
        if signed:
            self.ev("codex.login", None, email="hub@example.com", plan="plus", account="codex-1", channel="cli",
                    owner=True)
        self.settle()
        real = self.web.config
        self.enterContext(mock.patch.object(self.web, "config", lambda: {**real(), "host": "codex"}))
        self.enterContext(mock.patch("core.codexcli.page_status", lambda state_dir: None))
        return real

    def settings(self, **kw):
        return {"go": f"http://127.0.0.1:{self.port}/#/", **kw}

    TAB = {"eval": "localStorage.setItem('hub.settingsTab', 'codex'), location.hash = '#/settings', 1"}

    def shot(self, name):
        folder = os.environ.get("HUB_TEST_SHOTS")   # a folder: the row as a picture, light and dark
        return [{"shot": os.path.join(folder, f"{name}-{theme}.png"), "theme": theme, "width": 900, "height": 520}
                for theme in ("light", "dark")] if folder else []

    def test_enable_from_the_codex_tab_and_it_is_the_modules_switch(self):
        real = self.hub()
        got = self.page(self.settings(), self.TAB,
                        {"until": f"!!{ROW}", "ms": 10000},
                        {"eval": f"{ROW}.innerText"}, *self.shot("classic-off"),
                        {"eval": f"{ROW}.querySelector('.cx-critic-on').click(), 1"})
        self.assertTrue(got[2]["until"], got)
        self.assertIn(got[3]["eval"].split("\n")[0], OFF)
        # the switch is the Modules card's: hub.json now holds critic.enabled (the page's snapshot follows the journal's
        # settings.changed, which hubd writes from the web's spool — no hubd here, so the event comes by hand)
        end = time.time() + 10
        while not (real().get("critic") or {}).get("enabled") and time.time() < end:
            time.sleep(0.1)
        self.assertTrue((real().get("critic") or {}).get("enabled"))
        self.ev("settings.changed", None, what="critic.enabled", old=False, new=True, channel="web")
        self.settle()
        got = self.page({"until": f"{ROW}.dataset.on === '1'", "ms": 10000},
                        {"eval": f"[{ROW}.innerText, {ROW}.querySelectorAll('.btn').length]"},
                        *self.shot("classic-on"))
        self.assertTrue(got[0]["until"], got)
        self.assertIn(got[1]["eval"][0], ON)
        self.assertEqual(got[1]["eval"][1], 0)

    def test_the_same_row_in_clean(self):
        self.hub()
        got = self.page({"go": "about:blank", "look": None}, self.settings(look=None), self.TAB,
                        {"until": f"!!{ROW}", "ms": 10000},
                        {"eval": f"[document.documentElement.classList.contains('th-clean'), {ROW}.innerText.split('\\n')[0]]"},
                        *self.shot("clean-off"))
        self.assertTrue(got[3]["until"], got)
        self.assertTrue(got[4]["eval"][0] and got[4]["eval"][1] in OFF, got)

    def test_no_row_before_the_sign_in_and_none_when_the_server_did_not_send_the_field(self):
        self.hub(signed=False)
        got = self.page(self.settings(), self.TAB, {"until": "!!document.querySelector('.cx')", "ms": 10000},
                        {"sleep": 500}, {"eval": f"!!{ROW}"})
        self.assertTrue(got[2]["until"], got)
        self.assertFalse(got[4]["eval"])
        self.ev("codex.login", None, email="hub@example.com", plan="plus", account="codex-1", channel="cli", owner=True)
        self.settle()
        real = self.web.codex_view
        self.enterContext(mock.patch.object(self.web, "codex_view",
                                            lambda cfg: {"codex": {k: v for k, v in real(cfg)["codex"].items() if k != "critic"}}))
        got = self.page(self.settings(), self.TAB, {"until": "!!document.querySelector('.cx')", "ms": 10000},
                        {"sleep": 500}, {"eval": f"[document.querySelectorAll('.cx .ac-sum').length, !!{ROW}]"})
        self.assertEqual(got[4]["eval"], [1, False], got)   # the older server: the sign-in line, no critic row


def load_tests(loader, standard, pattern):
    import unittest
    return unittest.TestSuite(CodexCriticPage(n) for n in CodexCriticPage.__dict__ if n.startswith("test_"))
