"""A session of another hub of this computer and this hub's `hub` (2026-11-26: a second hub's Codex session found the main
hub's bin\\hub.cmd first in the user's PATH; that file sets the main hub's HUB_STATE_DIR over the caller's, and the main
hub took the session for one of its own). The guard is in that file, as in hub-<name>.cmd of an instance: from a session
whose HUB_STATE_DIR is not this hub's it refuses. bin/hub itself takes an explicit HUB_STATE_DIR from a session as it is
(a session checks a copy of its hub's state, an install script runs `HUB_STATE_DIR="$STATE" bin/hub setup claude`)."""
import tests  # noqa: F401 — keep the machine's settings out
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

import platforms

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def tmpdir(test):
    d = tempfile.mkdtemp(prefix="hub-other-")
    test.addCleanup(platforms.current.rmtree, d)
    return d


class ExplicitStateDir(unittest.TestCase):
    def test_a_session_with_an_explicit_state_folder_reaches_that_folder(self):
        root = tmpdir(self)
        own, copy = os.path.join(root, "hub"), os.path.join(root, "copy")
        for d in (own, copy):
            os.makedirs(d)
        env = {k: v for k, v in os.environ.items() if k not in ("HUB_EXTENSION", "HUB_MODULE", "HUB_SCHEDULE_JOB")}
        env.update(HUB_STATE_DIR=copy, HUB_CONFIG=os.path.join(copy, "hub.json"),
                   HUB_CONFIG_DIR=os.path.join(copy, "config"), HUB_CLI_WAIT="0", HUB_LANG="en",
                   HUB_SESSION="maps", **{platforms.TREE_ENV: f"maps@{own}"})   # its tree names its own hub
        r = subprocess.run([sys.executable, os.path.join(REPO, "bin", "hub"), "send", "hub", "hello"], env=env,
                           capture_output=True, text=True, encoding="utf-8", timeout=60, stdin=subprocess.DEVNULL)
        self.assertNotEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertNotEqual(os.listdir(os.path.join(copy, "spool")), [])
        self.assertFalse(os.path.exists(os.path.join(own, "spool")))


class MainHubCmd(unittest.TestCase):
    """deploy/windows/setup.py: the app's bin\\hub.cmd of the main hub (Setup.exe, the one-line install)."""
    def write(self):
        spec = importlib.util.spec_from_file_location("win_setup_other", os.path.join(REPO, "deploy", "windows", "setup.py"))
        setup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(setup)
        self.state, app, self.repo = tmpdir(self), tmpdir(self), tmpdir(self)
        with open(os.path.join(app, "paths.json"), "w") as f:
            json.dump({"git": "runtime/git/cmd/git.exe"}, f)
        os.makedirs(os.path.join(self.repo, "bin"))
        with open(os.path.join(self.repo, "bin", "hub"), "w") as f:   # what reaches the hub: its state folder
            f.write("import os\nprint('state=' + os.environ.get('HUB_STATE_DIR', ''))\n")
        hub_cmd, _ = setup.scripts(self.repo, self.state, sys.executable, encoding="utf-8" if os.name != "nt" else None,
                                   app=app, shell=True)
        return hub_cmd

    def test_the_file_refuses_a_session_of_another_hub_before_setting_its_own_folder(self):
        with open(self.write(), encoding="utf-8", errors="replace", newline="") as f:
            lines = f.read().split("\r\n")
        refuse, own = lines.index("exit /b 2"), next(i for i, x in enumerate(lines) if x.startswith('set "HUB_STATE_DIR='))
        self.assertLess(refuse, own)
        self.assertIn("if not defined HUB_SESSION goto hub", lines)

    @unittest.skipUnless(os.name == "nt", "cmd.exe")
    def test_cmd_runs_it_for_the_owner_and_its_own_sessions_and_refuses_another_hubs(self):
        hub_cmd = self.write()
        other = tmpdir(self)

        def run(**extra):
            env = {k: v for k, v in os.environ.items() if k not in ("HUB_SESSION", "HUB_STATE_DIR")}
            env.update(extra)
            return subprocess.run(["cmd", "/d", "/c", hub_cmd, "status"], env=env, capture_output=True, text=True,
                                  timeout=60, stdin=subprocess.DEVNULL)
        mine = f"state={self.state}"
        for case in ({}, {"HUB_STATE_DIR": other}, {"HUB_SESSION": "maps"},
                     {"HUB_SESSION": "maps", "HUB_STATE_DIR": self.state},
                     {"HUB_SESSION": "maps", "HUB_STATE_DIR": self.state.upper()}):
            r = run(**case)
            self.assertEqual((r.returncode, r.stdout.strip()), (0, mine), (case, r.stderr))
        r = run(HUB_SESSION="maps", HUB_STATE_DIR=other)
        self.assertEqual((r.returncode, r.stdout), (2, ""), r.stderr)
        self.assertIn("hub bus", r.stderr)


if __name__ == "__main__":
    unittest.main()
