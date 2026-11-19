"""A tool only together with its skill (owner decision 2026-10-01): an MCP server setting `with: [<plugin>]`
means a session with this tool is always started with those plugins too; the catalog shows it."""
import tests  # noqa: F401 — hide the machine's settings
import json
import os
import tempfile
import unittest

from core import catalog


class WithSkill(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="with-")
        market = os.path.join(self.dir, "set")
        os.makedirs(os.path.join(market, ".claude-plugin"))
        os.makedirs(os.path.join(market, "plugins", "hire", "skills", "hire"))
        with open(os.path.join(market, ".claude-plugin", "marketplace.json"), "w") as f:
            json.dump({"name": "set", "plugins": [{"name": "hire", "source": "./plugins/hire", "description": "how"}]}, f)
        self.cfg = {**catalog.load_config(self.dir), "sets": [{"name": "set", "path": market}],
                    "mcp": {"people": {"server": {"command": "x"}, "description": "hire people", "risk": ["money"],
                                       "with": ["hire@set"]}}}
        self.tools = catalog.build(self.cfg)

    def flags(self, tools):
        return catalog.launch_flags(tools=tools, cfg=self.cfg, catalog=self.tools, kind="work", cwd=self.dir,
                                    project="all", state_dir=self.dir, name="s")

    def test_the_tool_brings_its_skill(self):
        self.assertEqual(self.tools["people"]["with"], ["hire@set"])
        flags = self.flags(["people"])
        dirs = [flags[i + 1] for i, f in enumerate(flags) if f == "--plugin-dir"]
        self.assertIn(self.tools["hire@set"]["path"], dirs)
        with open(flags[flags.index("--mcp-config") + 1]) as f:
            self.assertIn("people", json.load(f)["mcpServers"])

    def test_the_skill_alone_has_no_tool(self):
        flags = self.flags(["hire@set"])
        self.assertNotIn("--mcp-config", flags[flags.index("--strict-mcp-config"):] if "--mcp-config" not in flags
                         else [])


if __name__ == "__main__":
    unittest.main()
