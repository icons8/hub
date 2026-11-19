"""Hub sessions load Claude Code settings without user (setting_sources), but they need the `env` from the owner's
settings: it holds the login (CLAUDE_CODE_OAUTH_TOKEN) and the proxy. Real case 2026-10-02: a hub under launchd got
"Not logged in · Please run /login" on every turn while the same claude in a terminal was logged in. The values
here are made up."""
import tests  # noqa: F401 — isolation from the machine's settings (see tests/__init__.py)
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

from core import catalog
from hosts.streamjson import StreamHost

SETTINGS_ENV = {"CLAUDE_CODE_OAUTH_TOKEN": "fake-token", "HTTPS_PROXY": "http://proxy.example:3128",
                "HUB_SESSION": "from-settings"}
DUMP_ENV = "import json, os; json.dump(dict(os.environ), open(os.environ['DUMP'], 'w'))"


class UserEnvTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="claude-config-")
        self.patch = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.dir})
        self.patch.start()
        for name in SETTINGS_ENV:  # the test is also run from a claude session: its environment must not override the settings
            os.environ.pop(name, None)

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, text):
        with open(os.path.join(self.dir, "settings.json"), "w") as f:
            f.write(text)

    def test_reads_only_login_and_network_from_env(self):
        # owner decision 2026-10-02: from settings.json only the login and network; hub sessions do not need the
        # modes of their terminal
        self.write(json.dumps({"env": {"CLAUDE_CODE_OAUTH_TOKEN": "fake-token", "NO_PROXY": 3,
                                       "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"},
                               "enabledPlugins": {"x@y": True}}))
        self.assertEqual(catalog.user_env(), {"CLAUDE_CODE_OAUTH_TOKEN": "fake-token", "NO_PROXY": "3"})

    def test_hub_config_names_more_env_to_pass(self):
        # credentials of working MCPs and skills live in the env of Claude Code settings; the hub gets only names in
        # hub.json `user_env`, the values still come from settings.json and are not copied into hub.json
        self.write(json.dumps({"env": {"CLAUDE_CODE_OAUTH_TOKEN": "fake-token", "SERVICE_TOKEN": "fake-secret",
                                       "OTHER": "x"}}))
        cfg = os.path.join(self.dir, "hub.json")
        with open(cfg, "w") as f:
            json.dump({"user_env": ["SERVICE_TOKEN", "ABSENT"]}, f)
        with mock.patch.dict(os.environ, {"HUB_CONFIG": cfg}):
            self.assertEqual(catalog.user_env(), {"CLAUDE_CODE_OAUTH_TOKEN": "fake-token", "SERVICE_TOKEN": "fake-secret"})
            for broken in ("{not json", '{"user_env": "SERVICE_TOKEN"}'):
                with open(cfg, "w") as f:
                    f.write(broken)
                self.assertEqual(catalog.user_env(), {"CLAUDE_CODE_OAUTH_TOKEN": "fake-token"}, broken)

    def test_hub_config_in_the_former_place_is_read_too(self):
        # a hub with the earlier layout: hub.json is in the state folder and absent from the OS settings folder —
        # load_config reads it from there
        self.write(json.dumps({"env": {"SERVICE_TOKEN": "fake-secret"}}))
        state, conf = (os.path.join(self.dir, d) for d in ("state", "conf"))
        os.makedirs(state)
        with open(os.path.join(state, "hub.json"), "w") as f:
            json.dump({"user_env": ["SERVICE_TOKEN"]}, f)
        env = {k: v for k, v in os.environ.items() if k != "HUB_CONFIG"}
        with mock.patch.dict(os.environ, {**env, "HUB_STATE_DIR": state}, clear=True), \
                mock.patch.object(catalog, "config_dir", lambda: conf):
            self.assertEqual(catalog.user_env(), {"SERVICE_TOKEN": "fake-secret"})

    def test_unreadable_hub_config_does_not_take_the_login_away(self):
        # hub.json did not open (a folder instead of a file, no permissions) — the login and network from the
        # settings still get through
        self.write(json.dumps({"env": {"CLAUDE_CODE_OAUTH_TOKEN": "fake-token", "SERVICE_TOKEN": "fake-secret"}}))
        cfg = os.path.join(self.dir, "hub.json")
        os.mkdir(cfg)
        with mock.patch.dict(os.environ, {"HUB_CONFIG": cfg}):
            self.assertEqual(catalog.user_env(), {"CLAUDE_CODE_OAUTH_TOKEN": "fake-token"})

    def test_missing_or_broken_settings_give_nothing(self):
        self.assertEqual(catalog.user_env(), {})
        for text in ("{not json", "[]", '{"env": "x"}', '{"env": null}'):
            self.write(text)
            self.assertEqual(catalog.user_env(), {}, text)

    def test_stream_session_gets_settings_env_under_hub_names(self):
        self.write(json.dumps({"env": SETTINGS_ENV}))
        state = tempfile.mkdtemp(prefix="hubs-")
        self.addCleanup(shutil.rmtree, state, True)
        dump = os.path.join(state, "env.json")
        host = StreamHost(state, claude=(sys.executable, "-c", DUMP_ENV), env={"DUMP": dump})
        # a run from a hub session carries its MCP_TIMEOUT — the one in the environment wins, the test checks the
        # core's value
        with mock.patch.dict(os.environ):
            os.environ.pop("MCP_TIMEOUT", None)
            host.spawn("a", "00000000-0000-0000-0000-000000000001")
        self.addCleanup(host.kill_server)
        end = time.time() + 10
        while not os.path.exists(dump) and time.time() < end:
            time.sleep(0.05)
        time.sleep(0.1)
        with open(dump) as f:
            env = json.load(f)
        self.assertTrue(env.get("CLAUDE_CODE_OAUTH_TOKEN") == "fake-token")
        self.assertEqual(env.get("HTTPS_PROXY"), "http://proxy.example:3128")
        self.assertEqual(env.get("HUB_SESSION"), "a")  # hub names outrank the settings
        self.assertEqual(env.get("MCP_TIMEOUT"), "180000")  # MCP gets a longer wait at fleet start (2026-10-02, 10-07: 60 s was too little)

    def test_check_models_get_settings_env(self):
        from modules.watch import watch
        self.write(json.dumps({"env": SETTINGS_ENV}))
        out = mock.Mock(stdout=json.dumps({"structured_output": {}}))
        for call in (lambda: watch._claude("t", "p", {}),):
            with mock.patch("subprocess.run", return_value=out) as run, \
                    mock.patch.dict(os.environ, {"HUB_NO_MODEL": ""}):   # the launch is mocked — no model ban needed
                call()
            env = run.call_args.kwargs["env"]  # error messages get names only: the environment may hold keys
            self.assertTrue(env.get("CLAUDE_CODE_OAUTH_TOKEN") == "fake-token")
            self.assertEqual([k for k in env if k.startswith("HUB_")], [])  # the checking model is not a hub session


if __name__ == "__main__":
    unittest.main()
