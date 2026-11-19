"""Parts of the hub (hubd, web, mod:*, ext:*) take NO_PROXY/no_proxy from the env of the owner's Claude Code settings on
every start: hub-up runs for days with the environment it was started with, and a host added to NO_PROXY in settings.json
reached sessions but not the parts (a module's urllib kept going through the https proxy and got reset, 2026-10-29). A proxy itself (HTTPS_PROXY), the extra CA file and login names stay out of the parts."""
import tests  # noqa: F401 — keeps the machine's settings out
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PART = """import json, os, sys, time
names = ("NO_PROXY", "no_proxy", "HTTPS_PROXY", "NODE_EXTRA_CA_CERTS", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
with open(sys.argv[1] + ".tmp", "w", encoding="utf-8") as f:
    json.dump({k: os.environ.get(k) for k in names}, f)
os.replace(sys.argv[1] + ".tmp", sys.argv[1])
time.sleep(600)
"""

DROP = ("HUB_SERVICE", "INVOCATION_ID", "XPC_SERVICE_NAME", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy",
        "https_proxy", "no_proxy", "NODE_EXTRA_CA_CERTS", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_AUTH_TOKEN")


class PartsTakeProxyFromSettings(unittest.TestCase):
    def seen(self, settings_env, outer):
        tmp = tempfile.mkdtemp(prefix="parts-proxy-")
        self.addCleanup(shutil.rmtree, tmp, True)
        state, out, part = os.path.join(tmp, "state"), os.path.join(tmp, "seen"), os.path.join(tmp, "part.py")
        claude = os.path.join(tmp, "claude")
        os.makedirs(state)
        os.makedirs(claude)
        with open(part, "w", encoding="utf-8") as f:
            f.write(PART)
        with open(os.path.join(claude, "settings.json"), "w", encoding="utf-8") as f:
            json.dump({"env": settings_env}, f)
        config = os.path.join(tmp, "hub.json")
        with open(config, "w", encoding="utf-8") as f:
            json.dump({}, f)
        env = {k: v for k, v in os.environ.items() if k not in DROP}
        env.update(outer, HUB_STATE_DIR=state, HUB_CONFIG=config, CLAUDE_CONFIG_DIR=claude,
                   HUB_UP_PARTS=json.dumps({"web": [part, out]}), HUB_UP_EXT="{}")
        p = subprocess.Popen([sys.executable, os.path.join(REPO, "bin", "hub-up")], env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            end = time.time() + 30
            while time.time() < end and not os.path.exists(out):
                time.sleep(0.1)
        finally:
            open(os.path.join(state, "stop"), "w").close()
            try:
                p.wait(60)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        with open(out, encoding="utf-8") as f:
            return json.load(f)

    def test_settings_proxy_names_win_over_the_old_environment(self):
        seen = self.seen({"NO_PROXY": "new.example", "no_proxy": "new.example"},
                         {"NO_PROXY": "old.example", "no_proxy": "old.example"})
        self.assertEqual(seen["NO_PROXY"], "new.example")
        self.assertEqual(seen["no_proxy"], "new.example")

    def test_one_spelling_in_settings_beats_the_other_in_the_old_environment(self):
        seen = self.seen({"NO_PROXY": "new.example"}, {"no_proxy": "old.example"})
        self.assertEqual(seen["NO_PROXY"], "new.example")
        self.assertEqual(seen["no_proxy"], "new.example")   # urllib reads the lowercase one first

    def test_the_extra_ca_file_from_settings_does_not_reach_parts(self):
        seen = self.seen({"NODE_EXTRA_CA_CERTS": "/x/ca.pem"}, {"NODE_EXTRA_CA_CERTS": "/outer/ca.pem"})
        self.assertEqual(seen["NODE_EXTRA_CA_CERTS"], "/outer/ca.pem")
        self.assertIsNone(self.seen({"NODE_EXTRA_CA_CERTS": "/x/ca.pem"}, {})["NODE_EXTRA_CA_CERTS"])

    def test_login_names_from_settings_do_not_reach_parts(self):
        seen = self.seen({"ANTHROPIC_API_KEY": "fake-key", "CLAUDE_CODE_OAUTH_TOKEN": "fake-token",
                          "NO_PROXY": "new.example"}, {})
        self.assertIsNone(seen["ANTHROPIC_API_KEY"])
        self.assertIsNone(seen["CLAUDE_CODE_OAUTH_TOKEN"])
        self.assertEqual(seen["NO_PROXY"], "new.example")

    def test_a_proxy_from_settings_does_not_reach_parts(self):
        seen = self.seen({"HTTPS_PROXY": "http://settings.example:1", "NO_PROXY": "new.example"}, {})
        self.assertIsNone(seen["HTTPS_PROXY"])
        self.assertEqual(seen["NO_PROXY"], "new.example")

    def test_the_outer_proxy_is_not_replaced_by_the_one_in_settings(self):
        seen = self.seen({"HTTPS_PROXY": "http://settings.example:1"}, {"HTTPS_PROXY": "http://outer.example:1"})
        self.assertEqual(seen["HTTPS_PROXY"], "http://outer.example:1")

    def test_a_name_missing_in_settings_keeps_the_process_value(self):
        seen = self.seen({}, {"HTTPS_PROXY": "http://outer.example:1"})
        self.assertEqual(seen["HTTPS_PROXY"], "http://outer.example:1")
        self.assertIsNone(seen["NO_PROXY"])


if __name__ == "__main__":
    unittest.main()
