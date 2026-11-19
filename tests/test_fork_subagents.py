"""Forks of helper agents (Claude Code 2.1.302 enabled them in `claude -p`): a fork gets the whole session conversation
and in a long conversation costs more than an ordinary agent. hub.json `fork_subagents: false` (or "Settings → General
→ Sessions → Advanced") turns them off for sessions — CLAUDE_CODE_FORK_SUBAGENT=0 in the claude environment; by
default — whatever Claude Code decides. The value comes from the settings the core holds (a broken hub.json keeps the
previous settings, like the rest)."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from core.daemon import Hub
from hosts.streamjson import StreamHost
from hosts.tmux import TmuxHost
from modules.web import settings

VAR = "CLAUDE_CODE_FORK_SUBAGENT"


class ForkSubagentsOption(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="fork-")
        self.addCleanup(shutil.rmtree, self.state, True)
        self.config = os.path.join(self.state, "hub.json")
        self.addCleanup(os.environ.pop, "HUB_CONFIG", None)
        os.environ["HUB_CONFIG"] = self.config

    def hub_json(self, **cfg):
        with open(self.config, "w", encoding="utf-8") as f:
            json.dump(cfg, f)

    def wired(self, host):
        """A host as hubd builds it: the variables come from its settings."""
        self.hub = Hub(self.state, host, registry_dir=self.state)
        return host

    def stream_env(self):
        return self.wired(StreamHost(self.state, claude=("echo",))).launch_env("s")

    def tmux_env(self):
        host = self.wired(TmuxHost(self.state, claude=("claude",)))
        calls = []
        host._tmux = lambda *a, **kw: calls.append(a) or mock.Mock(returncode=1, stdout="")
        host.alive = lambda name: False
        host.spawn("s", "sid", cwd=self.state)
        args = next(c for c in calls if c[0] == "new-session")
        spec = next(a for a in args if os.path.dirname(a) == os.path.join(self.state, "launch"))
        with open(spec, encoding="utf-8") as f:   # the pane's environment goes through a private file, not `tmux -e`
            return json.load(f)["env"]

    def test_off_reaches_every_session_process(self):
        self.hub_json(fork_subagents=False)
        self.assertEqual(self.stream_env()[VAR], "0")
        self.assertEqual(self.tmux_env()[VAR], "0")

    def test_a_broken_hub_json_keeps_what_the_core_holds(self):
        self.hub_json(fork_subagents=False)
        host = self.wired(StreamHost(self.state, claude=("echo",)))
        with open(self.config, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.hub.load_catalog()   # the core re-read the settings and keeps the previous ones
        self.assertEqual(host.launch_env("s")[VAR], "0")

    def test_default_leaves_claude_code_alone(self):
        self.hub_json()
        with mock.patch.dict(os.environ, {VAR: "1"}):   # a value of its own in the hub environment — left as it was
            self.assertEqual(self.stream_env().get(VAR), "1")
        with mock.patch.dict(os.environ):
            os.environ.pop(VAR, None)
            self.assertNotIn(VAR, sorted(self.stream_env()))
            self.assertNotIn(VAR, sorted(self.tmux_env()))

    def test_on_the_settings_tab(self):
        row = next(r for r in settings.S if r[0] == "fork_subagents")
        self.assertEqual(row[1:6], ("hub", "bool", True, "sessions", True))


if __name__ == "__main__":
    unittest.main()
