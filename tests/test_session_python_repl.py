"""Sessions never need Python's interactive REPL, and on Windows with Python 3.13+ it is a trap: an empty heredoc
(`python3 - <<'EOF'` with the closing line swallowed by quotes) leaves `python -` with stdin = NUL, Python takes it for a
terminal, starts PyREPL, which fails in windows_console.getheightwidth (WinError 123) and retries forever, a core busy for
days (a colleague's report). PYTHON_BASIC_REPL=1 closes the whole class: every session of the hub, Claude and Codex, gets
it, whatever the hub's own or the owner's environment says."""
import tests  # noqa: F401 — keep the machine's settings out of the test
import os
import sys
import tempfile
import unittest
from unittest import mock

from hosts.codex import CodexHost
from hosts.streamjson import StreamHost

FAKE_CODEX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakes", "fake_codex.py")


class SessionPythonRepl(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="repl-state-")

    def test_a_claude_session_gets_the_basic_repl(self):
        host = StreamHost(self.state, claude=(sys.executable,))   # not run: only its environment
        self.assertEqual(host.launch_env("maps")["PYTHON_BASIC_REPL"], "1")

    def test_the_hubs_own_value_does_not_turn_it_off(self):
        host = StreamHost(self.state, claude=(sys.executable,))
        with mock.patch.dict(os.environ, {"PYTHON_BASIC_REPL": "0"}):
            self.assertEqual(host.launch_env("maps")["PYTHON_BASIC_REPL"], "1")
        other = StreamHost(self.state, claude=(sys.executable,), env={"PYTHON_BASIC_REPL": ""})
        self.assertEqual(other.launch_env("maps")["PYTHON_BASIC_REPL"], "1")

    def test_a_codex_session_gets_the_basic_repl(self):
        home = os.path.join(self.state, "codex-home")
        host = CodexHost(self.state, codex=(sys.executable, FAKE_CODEX), env={}, all_sessions=True)
        with mock.patch.dict(os.environ, {"PYTHON_BASIC_REPL": "0"}):
            self.assertEqual(host._env("maps", None, {}, home=home)["PYTHON_BASIC_REPL"], "1")


if __name__ == "__main__":
    unittest.main()
