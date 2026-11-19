"""Hub clients outside the page (a phone) use the same API as the page; this covers the few things added for them:
the owner's language and the list of optional things (features) in the snapshot."""
import tests  # noqa: F401 — hide the machine's settings (~/.config/hub-core), see tests/__init__.py
import tempfile
import unittest

from core import langdata, reducer, views


class OwnerLanguage(unittest.TestCase):
    def snapshot(self, lang):
        r = views.Reader()
        if lang:
            r.state["owner"]["lang"] = lang
        return r.snapshot()

    def test_snapshot_carries_the_language_code(self):
        for name, code in langdata.legacy("lang_codes").items():   # the earlier states named the language in Russian
            self.assertEqual(self.snapshot(name)["owner"]["lang"], code)
        self.assertEqual(self.snapshot("English")["owner"]["lang"], "en")
        self.assertIsNone(self.snapshot(None)["owner"]["lang"])


class Features(unittest.TestCase):
    def test_push_is_listed_only_when_switched_on_and_with_a_key(self):
        from modules.web.server import Web
        web = Web(tempfile.mkdtemp(prefix="hubweb-"))
        web.config = lambda: {}
        self.assertEqual(web.with_clock({})["features"], ["upload.raw", "accounts.use", "send.tasks", "feed.post_by_id", "bus", "about.addresses", "about.lite", "send.app", "setup", "feed.on_behalf", "session.resume", "stream.delta2", "stream.feed", "page.password", "file.thumb"])
        key = {"key_file": "/k.p8", "key_id": "K", "team_id": "T", "topic": "app.x"}
        web.config = lambda: {"push": key}                       # key present, module off — same as absent
        self.assertEqual(web.with_clock({})["features"], ["upload.raw", "accounts.use", "send.tasks", "feed.post_by_id", "bus", "about.addresses", "about.lite", "send.app", "setup", "feed.on_behalf", "session.resume", "stream.delta2", "stream.feed", "page.password", "file.thumb"])
        web.config = lambda: {"push": {**key, "enabled": True}}
        # push.alerts (API 1.26): the snapshot carries alerts with push keys and alerts_rev — the client reconciles banners by them
        self.assertEqual(web.with_clock({})["features"], ["push", "push.alerts", "upload.raw", "accounts.use", "send.tasks", "feed.post_by_id", "bus", "about.addresses", "about.lite", "send.app", "setup", "feed.on_behalf", "session.resume", "stream.delta2", "stream.feed", "page.password", "file.thumb"])
        from core import modclaims
        self.assertIn("push.alerts", modclaims.CORE_FEATURES)


class PushEndpoint(unittest.TestCase):
    TOKEN = "ab" * 32

    def web(self, cfg):
        from modules.web.server import Web
        web = Web(tempfile.mkdtemp(prefix="hubweb-"))
        web.config = lambda: cfg
        return web

    def test_registration_is_refused_while_the_module_is_off_and_works_when_on(self):
        key = {"key_file": "/k.p8", "key_id": "K", "team_id": "T", "topic": "app.x"}
        for cfg in ({}, {"push": key}):
            web = self.web(cfg)
            self.assertEqual(web.act("/api/push/register", {"token": self.TOKEN})[0], 409)
            self.assertEqual(web.push.data["tokens"], {})
        web = self.web({"push": {**key, "enabled": True}})
        self.assertEqual(web.act("/api/push/register", {"token": self.TOKEN})[0], 200)
        self.assertEqual(web.act("/api/push/register", {"token": "nope"})[0], 400)
        self.assertEqual(list(web.push.data["tokens"]), [self.TOKEN])

    def test_caps_keep_only_the_known_and_a_bare_reregister_keeps_them(self):
        # caps (API 1.26): what the client can do; the core keeps the known ones (clear — removes a banner with a silent push)
        # and drops the rest; without the field (old client, re-registration at startup) the previous ones stay, as with mode
        key = {"key_file": "/k.p8", "key_id": "K", "team_id": "T", "topic": "app.x", "enabled": True}
        web = self.web({"push": key})
        tokens = web.push.data["tokens"]
        self.assertEqual(web.act("/api/push/register", {"token": self.TOKEN, "caps": ["clear", "remind", 5]})[0], 200)
        self.assertEqual(tokens[self.TOKEN]["caps"], ["clear"])
        web.act("/api/push/register", {"token": self.TOKEN})
        self.assertEqual(tokens[self.TOKEN]["caps"], ["clear"])
        web.act("/api/push/register", {"token": self.TOKEN, "caps": []})
        self.assertNotIn("caps", tokens[self.TOKEN])
        web.act("/api/push/register", {"token": self.TOKEN, "caps": "clear"})   # not a list — nothing
        self.assertNotIn("caps", tokens[self.TOKEN])


class ModulesCard(unittest.TestCase):
    def card(self, push_cfg, state_dir=None):
        from modules.web import modinfo
        cfg = {"push": push_cfg} if push_cfg is not None else {}
        return next(m for m in modinfo.modules(state_dir or tempfile.mkdtemp(), cfg, {"sessions": []}) if m["id"] == "push")

    def test_off_by_default_with_a_switch_then_part_without_a_key_then_on(self):
        c = self.card(None)
        self.assertEqual((c["state"], c["toggle"], c["enabled"]), ("off", True, False))
        key = {"key_file": "/k.p8", "key_id": "K", "team_id": "T", "topic": "app.x"}
        self.assertEqual(self.card({"enabled": True})["state"], "part")
        self.assertIn("APNs key", self.card({"enabled": True})["detail"])
        self.assertEqual(self.card({**key, "enabled": True})["state"], "on")
        self.assertEqual(self.card(key)["state"], "off")        # key without enabling — off

    def test_the_switch_is_in_the_settings_whitelist(self):
        from modules.web import modinfo, settings
        self.assertEqual(modinfo.TOGGLES["push"], "push.enabled")
        self.assertIn("push.enabled", [row[0] for row in settings.S])


if __name__ == "__main__":
    unittest.main()
