"""A session's required login module (hub.json session_providers, PR #235): without it a session never goes to a foreign login."""
import os
import tempfile
import unittest
from unittest import mock

from core import catalog
from core import cutout  # noqa: E402 — the open flavor's run has no built-in pool (core/cutout.py)
accounts = None
if cutout.present("profile"):
    from modules.accounts import accounts
from core.daemon import Hub
from hosts.tmux import TmuxHost
from tests.test_modapi_hooks import Mods, host as bare
from tests.fakes import cut


def host(mods):
    """A host as hubd builds it: the login module is chosen from the settings the core holds."""
    h = bare(mods)
    h.providers = lambda: catalog.load_config(h.state_dir).get("session_providers") or {}
    return h


@cut.needs("profile")
class RequiredProvider(unittest.TestCase):
    def setUp(self):
        self.cfg = tempfile.mkdtemp(prefix="required-provider-")
        self.addCleanup(os.environ.__setitem__, "HUB_CONFIG_DIR", os.environ["HUB_CONFIG_DIR"])
        os.environ["HUB_CONFIG_DIR"] = self.cfg
        settings = mock.patch.dict(os.environ, {"HUB_CONFIG": os.path.join(self.cfg, "hub.json")})
        settings.start()
        self.addCleanup(settings.stop)
        accounts.save({"accounts": [{"name": "work", "token": "POOL"}]})
        catalog.write_private(os.path.join(self.cfg, "hub.json"), {"session_providers": {"selected": "provider"}})

    def test_unavailable_provider_never_uses_pool_or_owner_login(self):
        for answers in ({}, {"provider": {}}, {"provider": {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:9"}}}):
            with self.subTest(answers=answers):
                h = host(Mods(self, answers))
                h.env["ANTHROPIC_API_KEY"] = "OWNER"
                with self.assertRaisesRegex(RuntimeError, "the required sign-in module"):
                    h.launch_env("selected", account="work")

    def test_selected_provider_excludes_other_credentials_and_gateway(self):
        m = Mods(self, {"provider": {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:9", "ANTHROPIC_AUTH_TOKEN": "PROVIDER"},
                                    "args": ["--model", "provider-model"]}})
        h = host(m)
        h.env.update(ANTHROPIC_API_KEY="OWNER", ANTHROPIC_CUSTOM_HEADERS="X-Key: OLD")
        with mock.patch.object(h, "_gateway_env", side_effect=AssertionError("must bypass pool gateway")):
            env = h.launch_env("selected", account="work", gateway="pool")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "PROVIDER")
        for key in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_CUSTOM_HEADERS", "HUB_ACCOUNT"):
            self.assertNotIn(key, env)

    def test_different_provider_cannot_replace_selected_provider(self):
        m = Mods(self, {"provider": {"env": {"ANTHROPIC_BASE_URL": "http://first", "ANTHROPIC_AUTH_TOKEN": "FIRST"}},
                        "other": {"env": {"ANTHROPIC_BASE_URL": "http://second", "ANTHROPIC_AUTH_TOKEN": "SECOND"}}})
        with self.assertRaisesRegex(RuntimeError, "the required sign-in module"):
            host(m).launch_env("selected", account="work")

    def test_other_session_keeps_existing_account(self):
        env = host(Mods(self, {})).launch_env("unselected", account="work")
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "POOL")
        self.assertEqual(env["HUB_ACCOUNT"], "work")

    def test_invalid_explicit_selection_does_not_fall_back(self):
        for value in ("", False, [], {}):
            catalog.write_private(os.path.join(self.cfg, "hub.json"), {"session_providers": {"selected": value}})
            with self.subTest(value=value), self.assertRaisesRegex(RuntimeError, "the required sign-in module"):
                host(Mods(self, {})).launch_env("selected", account="work")

    def test_a_malformed_mapping_does_not_stop_other_sessions(self):
        # a typo in a hub.json section does not stop the hub (catalog.load_config): not an object — same as unset
        catalog.write_private(os.path.join(self.cfg, "hub.json"), {"session_providers": ["selected", "provider"]})
        env = host(Mods(self, {})).launch_env("unselected", account="work")
        self.assertEqual(env["HUB_ACCOUNT"], "work")

    def test_a_broken_hub_json_keeps_the_selection_the_core_holds(self):
        # hubd keeps the previous settings (a broken hub.json is no reason to let a session bypass the module, nor
        # to refuse to start the others): the choice comes from them, not from disk on every start
        h = bare(Mods(self, {}))
        hub = Hub(h.state_dir, h, registry_dir=h.state_dir)
        with open(os.path.join(self.cfg, "hub.json"), "w", encoding="utf-8") as f:
            f.write("{not json")
        hub.load_catalog()
        with self.assertRaisesRegex(RuntimeError, "the required sign-in module"):
            h.launch_env("selected", account="work")
        self.assertEqual(h.launch_env("unselected", account="work")["HUB_ACCOUNT"], "work")

    def test_the_tmux_host_refuses_a_selected_session(self):
        # the tmux host does not call launch.env modules — it cannot honor a required login: do not start on the owner's login
        h = TmuxHost(tempfile.mkdtemp(prefix="required-provider-tmux-"), claude=("claude",))
        h.providers = lambda: {"selected": "provider"}
        h._tmux = mock.Mock(side_effect=AssertionError("tmux must not be called"))
        h.alive = lambda name: False
        with self.assertRaisesRegex(RuntimeError, "the required sign-in module"):
            h.spawn("selected", "sid")
