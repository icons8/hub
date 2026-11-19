"""Tests are also run from a live hub session (it has its own HUB_SESSION, HUB_CLI_WAIT, HUB_CONFIG_DIR): tests/__init__.py
removes them, otherwise the tests see the live hub's settings (a report from another hub, 2026-10-03: from a session
test_tools.ConfigTest.test_config_location_standard_then_state_dir went red)."""
import os
import subprocess
import sys
import tempfile
import unittest
from tests.fakes import cut

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FromAHubSession(unittest.TestCase):
    def test_config_tests_pass_with_the_hub_session_environment(self):
        env = {**os.environ, "HUB_SESSION": "hub-dev", "HUB_CLI_WAIT": "0", "HUB_CONFIG_DIR": tempfile.mkdtemp()}
        r = subprocess.run([sys.executable, "-m", "unittest", "tests.test_tools.ConfigTest"], cwd=REPO, env=env,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])


class OneProcess(unittest.TestCase):
    @cut.needs("profile")
    def test_accounts_then_restart_tests_in_one_process(self):
        # 2026-10-07: before an update the tests run in one process; the accounts test removed HUB_CONFIG_DIR and the
        # following ones read
        # the owner's real pool: test_stream_hubd caught an extra startup and rolled back the update of a personal hub
        # (twice)
        env = {k: v for k, v in os.environ.items() if not k.startswith("HUB_")}
        r = subprocess.run([sys.executable, "-m", "unittest", "tests.test_accounts",
                            "tests.test_stream_hubd.StreamHubdTest.test_no_turns_restarts_fresh_with_same_id",
                            "tests.test_test_env.Probe"], cwd=REPO, env=env, capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])


class Probe(unittest.TestCase):
    def test_the_run_keeps_its_own_config_dir(self):
        self.assertIn("hub-core-tests-config-", os.environ.get("HUB_CONFIG_DIR", ""))

if __name__ == "__main__":
    unittest.main()
