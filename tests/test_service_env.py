"""The core's own model calls (the watchdog and its "working on" line, hubmod.model through core/modelcall, the critic's
claude, the stops audit) run on their own models: a model remap that the owner's settings env and hub.json `user_env`
give sessions (ANTHROPIC_DEFAULT_*_MODEL, ANTHROPIC_MODEL, ANTHROPIC_SMALL_FAST_MODEL, CLAUDE_CODE_SUBAGENT_MODEL) does
not reach them. Real case 2026-10-27: such names in a hub's `user_env` sent every session and every service call to
opus, `modelcall model=haiku` answered from opus. Sessions keep the remap; a person's own provider (an own
ANTHROPIC_BASE_URL that is not the hub's gateway, Bedrock, Vertex) keeps it everywhere, as before. Values are made up."""
import tests  # noqa: F401 — isolation from the machine's settings (see tests/__init__.py)
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import platforms
from core import catalog, profiles

REMAP = {"ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-opus-x", "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-opus-x",
         "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-x", "ANTHROPIC_MODEL": "claude-opus-x",
         "ANTHROPIC_SMALL_FAST_MODEL": "claude-opus-x", "CLAUDE_CODE_SUBAGENT_MODEL": "claude-opus-x"}
LOGIN = {"CLAUDE_CODE_OAUTH_TOKEN": "fake-token", "SERVICE_TOKEN": "fake-secret"}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ServiceEnv(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="service-env-")
        self.addCleanup(platforms.current.rmtree, self.dir)
        self.cfg = os.path.join(self.dir, "hub.json")
        self.settings({})
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump({"user_env": [*REMAP, "SERVICE_TOKEN"]}, f)
        state = os.path.join(self.dir, "state")
        os.makedirs(state)
        patch = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.dir, "HUB_CONFIG": self.cfg,
                                             "HUB_STATE_DIR": state})
        patch.start()
        self.addCleanup(patch.stop)
        for name in (*REMAP, *LOGIN, "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS", profiles.UPSTREAM_ENV,
                     "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"):
            os.environ.pop(name, None)   # the test may run inside a hub session: its environment must not count

    def settings(self, extra):
        with open(os.path.join(self.dir, "settings.json"), "w", encoding="utf-8") as f:
            json.dump({"env": {**LOGIN, **REMAP, **extra}}, f)

    def assert_plain(self, env, what):
        self.assertEqual(sorted(set(REMAP) & set(env)), [], what)
        self.assertEqual((env.get("CLAUDE_CODE_OAUTH_TOKEN"), env.get("SERVICE_TOKEN")), ("fake-token", "fake-secret"),
                         what)   # the login and the owner's other names still get through

    def stops_audit(self):
        spec = importlib.util.spec_from_file_location("stops_audit_env",
                                                      os.path.join(ROOT, "modules", "agent_dev", "stops-audit.py"))
        sa = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sa)
        return sa

    def service_envs(self):
        """{caller: env} of every core service call, as each builds it."""
        from core import modelcall
        from modules.critic import critic
        from modules.watch import watch
        got = {}

        def run(cmd, **kw):
            got["modelcall"] = kw["env"]
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": "ok", "total_cost_usd": 0.001}), "")
        with mock.patch.dict(os.environ, {"HUB_NO_MODEL": ""}):
            modelcall.call("p", "t", "haiku", None, 64, 30, run=run)
        return {"watch": watch.model_env(), "modelcall": got["modelcall"], "critic": critic.model_env(None),
                "stops-audit": self.stops_audit().model_env()}

    def test_sessions_keep_the_remap_service_calls_do_not(self):
        self.assertEqual({k: v for k, v in catalog.user_env().items() if k in REMAP}, REMAP)   # sessions: as before
        for caller, env in self.service_envs().items():
            self.assert_plain(env, caller)
        self.assert_plain(catalog.service_env(), "service_env")

    def test_a_remap_in_the_callers_own_environment_does_not_reach_them_either(self):
        # the stops audit and the critic run from a session's Bash: its environment already has the remap
        self.settings({k: "" for k in REMAP})   # names listed, values empty: the remap comes only from the process
        with mock.patch.dict(os.environ, REMAP):
            for caller, env in self.service_envs().items():
                self.assertEqual(sorted(set(REMAP) & set(env)), [], caller)

    def test_a_persons_own_provider_keeps_the_names(self):
        # there the names are how the person names the provider's models: everything as before — the provider in the
        # process's environment or in the owner's settings env
        for own in ({"ANTHROPIC_BASE_URL": "https://llm.example.test"}, {"CLAUDE_CODE_USE_BEDROCK": "1"},
                    {"CLAUDE_CODE_USE_VERTEX": "true"}):
            self.settings({})
            with mock.patch.dict(os.environ, own):
                for caller, env in {"service_env": catalog.service_env(os.environ), **self.service_envs()}.items():
                    self.assertEqual({k: env.get(k) for k in REMAP}, REMAP, f"{own} {caller}")
        self.settings({"ANTHROPIC_BASE_URL": "https://llm.example.test"})
        for caller, env in {"service_env": catalog.service_env(), **self.service_envs()}.items():
            self.assertEqual({k: env.get(k) for k in REMAP}, REMAP, f"settings {caller}")

    def test_a_cloud_provider_from_the_accounts_login_keeps_the_names(self):
        # a login module's profile may turn the call to Bedrock or Vertex after the remap was dropped: there the names
        # name the provider's models, and without them "haiku" is an id the provider may not have
        def bedrock(env, account):
            env["CLAUDE_CODE_USE_BEDROCK"] = "1"
            return env
        with mock.patch.object(profiles, "model_env", bedrock):
            for caller, env in self.service_envs().items():
                self.assertEqual({k: env.get(k) for k in REMAP}, REMAP, caller)
        with mock.patch.object(profiles, "model_env", lambda env, account: env):   # a profile on the API: no remap
            for caller, env in self.service_envs().items():
                self.assert_plain(env, caller)

    def test_the_hubs_gateway_is_not_an_own_provider(self):
        # sessions and hub-up on the account gateway have ANTHROPIC_BASE_URL of 127.0.0.1: still the Anthropic API
        gate = profiles.through({}, 8771, "s3cret", stub=False)
        with mock.patch.dict(os.environ, gate):
            for caller, env in {"service_env": catalog.service_env(), **self.service_envs()}.items():
                self.assertEqual(sorted(set(REMAP) & set(env)), [], caller)

    def test_a_process_of_the_previous_release_still_gets_its_environment(self):
        # the page server and Telegram of the previous release keep their catalog in memory until hub restart and may
        # load the new watcher file first: its model environment must not fail on the missing service_env
        import types
        from modules.watch import watch
        old = types.SimpleNamespace(user_env=catalog.user_env)
        with mock.patch.object(watch, "catalog", old):
            env = watch.model_env()
        self.assertEqual((env.get("CLAUDE_CODE_OAUTH_TOKEN"), env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL")),
                         ("fake-token", "claude-opus-x"))   # as that release built it


if __name__ == "__main__":
    unittest.main()
