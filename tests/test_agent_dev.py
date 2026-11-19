"""Agent Developer is there from the start, like Hub Developer (owner decision 2026-10-02: agent-dev is available right
after start, as hub-dev is, and is added to the core as a module): an agent that creates new hub agents through
evals."""
import tests  # noqa: F401 — keep the machine's settings out
import os
import tempfile
import unittest

from core import catalog, reducer
from modules.agent_dev import agent

CFG = {"agent_dev": {"name": "agent-dev"}}


class AgentDeveloper(unittest.TestCase):
    def test_its_skill_and_folder(self):
        state = tempfile.mkdtemp(prefix="agentdev-")
        name, data = agent.spec(CFG, state)
        self.assertEqual((name, data["role"], data["icon"], data["tools"]), ("agent-dev", "agent", "🧪",
                                                                            ["core:agent-developer"]))
        self.assertTrue(os.path.isdir(data["cwd"]) and data["cwd"].startswith(state))
        st, _, why = reducer.reduce(reducer.empty(), {"seq": 1, "ts": 1, "type": "session.requested", "entity": name,
                                                      "data": {**data, "session_id": "s"}})
        self.assertIsNone(why)
        self.assertEqual(st["sessions"]["agent-dev"]["role"], "agent")

    def test_its_plugin_is_in_the_catalog(self):
        self.assertIn("core:agent-developer", catalog.build(catalog.load_config(tempfile.mkdtemp())))


if __name__ == "__main__":
    unittest.main()
