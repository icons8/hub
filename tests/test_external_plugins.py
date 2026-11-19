"""A marketplace plugin with a git source (not a folder of the marketplace) gets into the catalog through the copy
Claude Code has already installed."""
import tests  # noqa: F401 — do not see the machine's settings (~/.config/hub-core), see tests/__init__.py
import json
import os
import shutil
import tempfile
import unittest
import unittest.mock

from core import catalog


class ExternalPlugins(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.claude = os.path.join(self.dir, "claude")
        self.market = os.path.join(self.dir, "market")
        os.makedirs(os.path.join(self.market, ".claude-plugin"))
        self.ext = {"source": "url", "url": "https://example.com/p.git", "sha": "abc"}
        self.write(os.path.join(self.market, ".claude-plugin", "marketplace.json"), {"name": "mk", "plugins": [
            {"name": "ext", "source": self.ext, "description": "External"},
            {"name": "gone", "source": self.ext, "description": "Not installed"}]})
        patch = unittest.mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.claude})
        patch.start()
        self.addCleanup(patch.stop)

    @staticmethod
    def write(path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f)

    def build(self):
        return catalog.build({**catalog.DEFAULTS, "sets": [{"name": "mk", "path": self.market}]})

    def install(self, name, path, exists=True):
        if exists:
            os.makedirs(path)
        self.write(os.path.join(self.claude, "plugins", "installed_plugins.json"),
                   {"version": 2, "plugins": {f"{name}@mk": [{"scope": "user", "installPath": path}]}})

    def test_installed_external_plugin_uses_claude_copy(self):
        path = os.path.join(self.dir, "cache", "ext", "1.0")
        self.install("ext", path)
        tool = self.build()["ext@mk"]
        self.assertEqual((tool["kind"], tool["path"], tool["description"]), ("plugin", path, "External"))
        self.assertNotIn("gone@mk", self.build())

    def test_missing_copy_is_skipped(self):
        self.install("ext", os.path.join(self.dir, "nowhere"), exists=False)
        self.assertNotIn("ext@mk", self.build())


if __name__ == "__main__":
    unittest.main()
