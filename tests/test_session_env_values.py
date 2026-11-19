"""hub.json `session_env_values` — fixed environment variables for hub sessions only, without touching the owner's
Claude Code settings (their terminal keeps its own values). Only names from a short list pass (a secret does not travel
through hub.json); the value from hub.json wins over the same name from settings.json. No section — as before."""
import tests  # noqa: F401 — do not see the machine's settings
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

from core import catalog
from core.daemon import Hub
from hosts.streamjson import StreamHost
from hosts.tmux import TmuxHost

VAR = "CLAUDE_CODE_SUBAGENT_MODEL"


class SessionEnvValues(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="session-env-")
        self.addCleanup(shutil.rmtree, self.state, True)
        self.config = os.path.join(self.state, "hub.json")
        patch = mock.patch.dict(os.environ, {"HUB_CONFIG": self.config, "CLAUDE_CONFIG_DIR": self.state})
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop(VAR, None)
        catalog._session_env_logged.clear()

    def hub_json(self, **cfg):
        with open(self.config, "w", encoding="utf-8") as f:
            json.dump(cfg, f)

    def settings_env(self, **env):
        with open(os.path.join(self.state, "settings.json"), "w", encoding="utf-8") as f:
            json.dump({"env": env}, f)

    def stream_env(self):
        host = StreamHost(self.state, claude=("echo",))
        self.hub = Hub(self.state, host, registry_dir=self.state)
        return host.launch_env("s")

    def tmux_env(self):
        host = TmuxHost(self.state, claude=("claude",))
        self.hub = Hub(self.state, host, registry_dir=self.state)
        calls = []
        host._tmux = lambda *a, **kw: calls.append(a) or mock.Mock(returncode=1, stdout="")
        host.alive = lambda name: False
        host.spawn("s", "sid", cwd=self.state)
        args = next(c for c in calls if c[0] == "new-session")
        spec = next(a for a in args if os.path.dirname(a) == os.path.join(self.state, "launch"))
        with open(spec, encoding="utf-8") as f:   # the pane's environment goes through a private file, not `tmux -e`
            return json.load(f)["env"]

    def test_the_value_reaches_every_session_process(self):
        self.hub_json(session_env_values={VAR: "sonnet"})
        self.assertEqual(self.stream_env()[VAR], "sonnet")
        self.assertEqual(self.tmux_env()[VAR], "sonnet")

    def test_it_wins_over_settings_json(self):
        self.settings_env(**{VAR: "haiku"})
        self.hub_json(user_env=[VAR], session_env_values={VAR: "sonnet"})
        self.assertEqual(catalog.user_env()[VAR], "haiku")   # the settings value is what the old code would pass
        self.assertEqual(self.stream_env()[VAR], "sonnet")

    def test_another_name_is_ignored_and_logged(self):
        self.hub_json(session_env_values={VAR: "sonnet", "ANTHROPIC_API_KEY": "fake-secret", "EXTRA": "1"})
        err = io.StringIO()
        with redirect_stderr(err):
            env = self.stream_env()
            self.assertEqual(env[VAR], "sonnet")
            self.assertNotEqual(env.get("ANTHROPIC_API_KEY"), "fake-secret")
            self.assertNotIn("EXTRA", env)
            self.stream_env()
        self.assertIn("EXTRA", err.getvalue())
        self.assertIn("ANTHROPIC_API_KEY", err.getvalue())
        self.assertNotIn("fake-secret", err.getvalue())   # the value is never printed
        self.assertEqual(err.getvalue().count("EXTRA"), 1)   # one line per name, not per spawn

    def test_not_a_string_or_not_an_object_gives_nothing(self):
        for bad in ({VAR: 3}, {VAR: ""}, ["sonnet"], "sonnet"):
            self.hub_json(session_env_values=bad)
            with redirect_stderr(io.StringIO()):
                self.assertNotIn(VAR, self.stream_env(), bad)

    def test_the_value_is_stripped_and_blank_is_skipped(self):
        self.hub_json(session_env_values={VAR: "  sonnet\n"})
        self.assertEqual(self.stream_env()[VAR], "sonnet")
        self.hub_json(session_env_values={VAR: " \t\n"})
        self.assertNotIn(VAR, self.stream_env())

    def test_a_section_of_the_wrong_shape_gets_a_log_line(self):
        self.hub_json(session_env_values=["sonnet"])
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertNotIn(VAR, self.stream_env())
        self.assertIn("session_env_values", err.getvalue())

    def test_a_bad_name_in_the_log_is_trimmed_without_control_characters(self):
        name = "X\x1b[2J\u202e" + "Y" * 500
        self.hub_json(session_env_values={name: "1"})
        err = io.StringIO()
        with redirect_stderr(err):
            self.stream_env()
        line = err.getvalue()
        self.assertIn("X", line)
        self.assertIn("YYYYYYYY", line)
        for bad in ("\x1b", "\u202e"):
            self.assertNotIn(bad, line)
        self.assertLess(len(line), 300)
        catalog._session_env_logged.clear()
        self.hub_json(session_env_values={chr(0xE0001) * 200: "1"})   # each escapes to ten characters
        err = io.StringIO()
        with redirect_stderr(err):
            self.stream_env()
        self.assertLess(len(err.getvalue()), 300)

    def test_no_section_is_as_before(self):
        self.hub_json()
        self.assertNotIn(VAR, self.stream_env())
        self.hub_json(fork_subagents=False)
        self.assertEqual(self.stream_env()["CLAUDE_CODE_FORK_SUBAGENT"], "0")
        self.hub_json(fork_subagents=False, session_env_values={VAR: "sonnet"})
        env = self.stream_env()
        self.assertEqual((env["CLAUDE_CODE_FORK_SUBAGENT"], env[VAR]), ("0", "sonnet"))


if __name__ == "__main__":
    unittest.main()
