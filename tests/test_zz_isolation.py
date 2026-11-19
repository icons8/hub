import os
import subprocess
import sys
import tempfile
import unittest

import tests  # noqa: F401 — isolation from the machine's settings (see tests/__init__.py)


class IsolationTest(unittest.TestCase):
    def test_not_inside_a_live_session(self):
        self.assertNotIn("HUB_SESSION", sorted(os.environ))
        state = os.environ["HUB_STATE_DIR"]
        # this run's own folder, not an inherited one: the substring ".hub-core" in a path does not mean a live core,
        # since the hub's private TMPDIR (~/.hub-core/tmp) contains it (16.10, a colleague's hub: a full run rejected
        # such a TMPDIR)
        self.assertTrue(os.path.basename(state).startswith("hub-core-tests-state-"), state)
        self.assertEqual(os.path.dirname(state), tempfile.gettempdir())

    def test_machine_config_hidden(self):
        self.assertIn("hub-core-tests-config", os.environ.get("HUB_CONFIG", ""))
        self.assertIn("hub-core-tests-claude-", os.environ.get("CLAUDE_CONFIG_DIR", ""))


class PrivateTmpdirTest(unittest.TestCase):
    def test_hub_private_tmpdir_is_fine(self):
        """A TMPDIR under ~/.hub-core (the hub's private directory) is no reason for the isolation check to fail."""
        with tempfile.TemporaryDirectory() as home:
            tmp = os.path.join(home, ".hub-core", "tmp")
            os.makedirs(tmp)
            env = {**os.environ, "TMPDIR": tmp, "HUB_STATE_DIR": os.path.join(home, ".hub-core")}
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            out = subprocess.run([sys.executable, "-m", "unittest", "tests.test_zz_isolation.IsolationTest"],
                                 cwd=root, env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(out.returncode, 0, out.stderr)
