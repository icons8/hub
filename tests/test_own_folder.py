"""A session without a folder starts in its own folder, not in the shared sandbox (owner decision 2026-10-08: by
default a session starts in a folder named by its ID, so that system agents do not end up in the sandbox)."""
import tests  # noqa: F401 — hide the machine's settings
import os
import subprocess
import sys
import tempfile
import unittest

from core import journal, spool, workspace

HUB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")


class OwnFolder(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="own-")
        journal.Journal(os.path.join(self.state, "journal.jsonl")).append(
            "session.requested", "hub", {"cwd": "/w", "session_id": "h", "kind": "hub"})

    def new(self, *args, me="hub"):
        env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_CLI_WAIT": "0", "HUB_SESSION": me}
        r = subprocess.run([sys.executable, HUB, "session", "new", *args], env=env, capture_output=True, text=True,
                           cwd=tempfile.mkdtemp(prefix="elsewhere-"))
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        (ev,) = [ev for _, ev in spool.take(os.path.join(self.state, "spool"))]
        return ev["data"]["cwd"]

    def test_without_a_folder_the_session_gets_its_own(self):
        cwd = self.new("notes", "--agent", "--about", "notes")
        self.assertEqual(cwd, os.path.join(self.state, "workspaces", "notes"))
        self.assertTrue(os.path.isdir(cwd))
        self.assertEqual(workspace.path(self.state, "notes"), cwd)

    def test_a_named_folder_stays(self):
        repo = tempfile.mkdtemp(prefix="repo-")
        self.assertEqual(self.new("maps", "--cwd", repo), repo)

    def test_an_executor_without_a_repo_works_in_its_own_folder_too(self):
        from core.daemon import Hub
        h = Hub(self.state, type("H", (), {"alive": lambda s, n: True, "spawn": lambda *a, **k: None})(),
                registry_dir=tempfile.mkdtemp())
        self.assertEqual(h.executor_base({}, {}, "fix-notes"), os.path.join(self.state, "workspaces", "fix-notes"))
        self.assertEqual(h.executor_base({"repo": "/r"}, {}, "x"), "/r")
        self.assertEqual(h.executor_base({}, {"cwd": "/w"}, "x"), "/w")


if __name__ == "__main__":
    unittest.main()
