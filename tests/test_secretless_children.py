"""Chrome and chrome-devtools-mcp start without the hub's secrets: they inherited its whole environment (the operator's
keys, and inside a session its own login and the gateway's header), and neither needs them. Values set in hub.json
browser.env are the operator's explicit choice and stay. Popen is replaced by a mock that raises: no process starts."""
import tests  # noqa: F401 — hide the machine's settings (~/.config/hub-core), see tests/__init__.py
import os
import unittest
from unittest import mock

from core import redact

SECRETS = {"GITHUB_TOKEN": "gh-fake", "CLAUDE_CODE_OAUTH_TOKEN": "login-fake", "OPENAI_API_KEY": "sk-fake",
           "HUB_MODULE_SECRET": "mod-fake", "ANTHROPIC_CUSTOM_HEADERS": "x-hub-gateway: gw-fake"}
PLAIN = {"LANG": "C.UTF-8", "HTTPS_PROXY": "http://203.0.113.5:3128", "MAX_OUTPUT_TOKENS": "100",
         "KEYBOARD_LAYOUT": "us"}


class SecretlessChildren(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.dict(os.environ, {**SECRETS, **PLAIN})
        patch.start()
        self.addCleanup(patch.stop)

    def leaked(self, env):
        return sorted(n for n in SECRETS if n in env)

    def test_child_env_drops_secret_looking_names_only(self):
        env = redact.child_env(os.environ)
        self.assertEqual(self.leaked(env), [])
        self.assertEqual({k: env.get(k) for k in PLAIN}, PLAIN)
        self.assertEqual(redact.child_env(os.environ, keep=("GITHUB_TOKEN",)).get("GITHUB_TOKEN"), "gh-fake")

    def test_browser_environment(self):
        from modules.browser import browsers
        env = browsers.env({"browser": {"env": {"PATH": "/opt/bin" + os.pathsep + "$PATH", "PROXY_TOKEN": "explicit",
                                                 "NPM_TOKEN": "$GITHUB_TOKEN"}}})
        self.assertEqual(self.leaked(env), [])
        self.assertEqual(env["PROXY_TOKEN"], "explicit")       # set on purpose in hub.json
        self.assertEqual(env["NPM_TOKEN"], "$GITHUB_TOKEN")    # a reference only to itself is expanded
        self.assertTrue(env["PATH"].startswith("/opt/bin" + os.pathsep))
        self.assertEqual(env["HTTPS_PROXY"], PLAIN["HTTPS_PROXY"])

    def test_browser_env_may_bring_its_own_name_back(self):
        from modules.browser import browsers
        env = browsers.env({"browser": {"env": {"GITHUB_TOKEN": "$GITHUB_TOKEN"}}})
        self.assertEqual(env.get("GITHUB_TOKEN"), "gh-fake")   # the operator named it: npx behind a private registry

    def popen_env(self, call):
        with mock.patch("subprocess.Popen", side_effect=OSError("not started")) as popen:
            with self.assertRaises(OSError):
                call()
        return popen.call_args.kwargs["env"]

    def test_mermaid_and_proof_chrome(self):
        from modules.proof import cdp
        from modules.telegram import mermaid
        for call in (lambda: mermaid._chrome("/bin/chrome", [], 1, lambda: True),
                     lambda: cdp.Page(chrome="/bin/chrome", timeout=1)):
            env = self.popen_env(call)
            self.assertEqual(self.leaked(env), [])
            self.assertEqual(env.get("LANG"), "C.UTF-8")


if __name__ == "__main__":
    unittest.main()
