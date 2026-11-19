"""A second hub on the same Mac (owner decision 2026-10-03): its own state folder, its own settings directory
(hub.json, Telegram bot, contacts, watchdog), its own launchd service and its own ports - it does not disturb the
first one. Install: `HUB_INSTANCE=personal deploy/mac/install.sh` -> ~/.hub-personal, ~/.config/hub-personal,
com.hub-personal, page :8797, Files :8798, command `hub-personal`."""
import tests  # noqa: F401 - ignore the machine's settings
import importlib.util
import json
import os
import unittest
from unittest import mock

from core import catalog

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("mac_app2", os.path.join(ROOT, "deploy", "mac", "app.py"))
app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(app)


class SecondHub(unittest.TestCase):
    def test_config_dir_follows_hub_config_dir(self):
        with mock.patch.dict(os.environ, {"HUB_CONFIG_DIR": "/x/hub-personal"}):
            self.assertEqual(catalog.config_dir(), "/x/hub-personal")
            env = {k: v for k, v in os.environ.items() if k != "HUB_CONFIG"}
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(catalog.config_path("/state"), os.path.join("/x/hub-personal", "hub.json"))

    def test_agent_of_a_second_hub_has_its_own_label_and_env(self):
        env = {"HUB_CONFIG_DIR": "/cfg/p", "HUB_WEB_PORT": "8797", "HUB_FILES_PORT": "8798"}
        agent = app.agent_plist("/py", "/repo", "/state-p", label="com.hub-personal", env=env)
        self.assertEqual(agent["Label"], "com.hub-personal")
        self.assertEqual({k: agent["EnvironmentVariables"][k] for k in env}, env)
        self.assertEqual(agent["EnvironmentVariables"]["HUB_STATE_DIR"], "/state-p")
        # review of PR #18: `hub stop` under the agent refuses and names its stop command
        self.assertEqual(agent["EnvironmentVariables"]["HUB_SERVICE"], "launchctl bootout gui/$(id -u)/com.hub-personal")
        work = app.agent_plist("/py", "/repo", "/state")
        self.assertEqual(work["Label"], "com.hub-core")                         # the first hub is unchanged
        self.assertNotIn("HUB_WEB_PORT", work["EnvironmentVariables"])

    def test_page_knows_its_files_port(self):
        from tests import test_web
        with mock.patch.dict(os.environ, {"HUB_FILES_PORT": "8798"}):
            h = test_web.WebTest("test_page_and_snapshot")
            h.setUp()
            try:
                h.seed()
                self.assertEqual(json.loads(h.req("GET", "/api/snapshot")[1])["files_port"], 8798)
            finally:
                h.tearDown()
        for f in ("feed.js", "markdown.js"):
            with open(os.path.join(ROOT, "modules", "web", "static", "js", f)) as fh:
                self.assertNotIn(":8788", fh.read(), f)                         # the Files port comes from the snapshot

    def test_installer_knows_instances(self):
        with open(os.path.join(ROOT, "deploy", "mac", "install.sh")) as f:
            sh = f.read()
        # the folders, the label and the ports of an instance come from core/instance.py (one place for all three
        # installers); tests/test_instance_install.py runs it
        for needle in ("HUB_INSTANCE", '"$REPO/core/instance.py" setup "$INSTANCE"', "LABEL=$INST_SERVICE",
                       'commands.py" --instance "$INSTANCE"'):
            self.assertIn(needle, sh)


if __name__ == "__main__":
    unittest.main()
