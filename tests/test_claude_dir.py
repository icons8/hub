"""claude_config_dir in hub.json: the hub keeps its own Claude Code folder even when the owner's environment points to
another (his Claude outside the hub is on a different subscription), so logging in to one does not touch the other
(Windows hub, 2026-10-18)."""
import tests  # noqa: F401 — keeps the machine's settings out
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import platforms
from core import catalog

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def hub_json(value):
    path = os.path.join(tempfile.mkdtemp(prefix="claude-dir-"), "hub.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({} if value is None else {"claude_config_dir": value}, f)
    return path


class Pin(unittest.TestCase):
    def pin(self, value, env):
        state = tempfile.mkdtemp(prefix="claude-dir-state-")
        old = os.environ.get("HUB_CONFIG")
        os.environ["HUB_CONFIG"] = hub_json(value)
        try:
            return catalog.pin_claude_dir(state, env)
        finally:
            os.environ.pop("HUB_CONFIG") if old is None else os.environ.__setitem__("HUB_CONFIG", old)

    def test_own_folder_wins_over_the_owners_environment(self):
        hub = os.path.join(tempfile.gettempdir(), "hub-claude")
        env = self.pin(hub, {"CLAUDE_CONFIG_DIR": "/personal"})
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], os.path.abspath(hub))

    def test_default_folder_drops_the_variable(self):
        # with CLAUDE_CONFIG_DIR=~/.claude Claude Code looks for .claude.json inside the folder, and MCP and projects would vanish
        env = self.pin("~/.claude", {"CLAUDE_CONFIG_DIR": "/personal", "PATH": "x"})
        self.assertNotIn("CLAUDE_CONFIG_DIR", env)
        self.assertEqual(env["PATH"], "x")

    def test_a_link_to_the_default_folder_is_the_default_folder(self):
        home = tempfile.mkdtemp(prefix="claude-dir-home-")
        os.makedirs(os.path.join(home, ".claude"))
        platforms.current.link_dir(os.path.join(home, ".claude"), os.path.join(home, "link"))
        with mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home}):
            env = self.pin(os.path.join(home, "link"), {"CLAUDE_CONFIG_DIR": "/personal"})
        self.assertNotIn("CLAUDE_CONFIG_DIR", env)

    def test_no_setting_leaves_the_environment_alone(self):
        self.assertEqual(self.pin(None, {"CLAUDE_CONFIG_DIR": "/personal"})["CLAUDE_CONFIG_DIR"], "/personal")
        self.assertNotIn("CLAUDE_CONFIG_DIR", self.pin("", {}))

    def test_a_removed_setting_gives_back_the_launch_environment(self):
        # hub-up restarts in place (exec) with an already pinned environment: a removed setting must not stick
        hub = os.path.join(tempfile.gettempdir(), "hub-claude")
        env = self.pin(hub, {"CLAUDE_CONFIG_DIR": "/personal"})
        self.assertEqual(self.pin(None, env)["CLAUDE_CONFIG_DIR"], "/personal")
        env = self.pin(hub, {})
        self.assertNotIn("CLAUDE_CONFIG_DIR", self.pin("", env))

    def test_spaces_around_the_path_are_not_part_of_it(self):
        hub = os.path.join(tempfile.gettempdir(), "hub-claude")
        self.assertEqual(self.pin(f" {hub} ", {})["CLAUDE_CONFIG_DIR"], os.path.abspath(hub))

    def test_a_relative_path_is_not_taken(self):
        # a relative path would give two different folders for hub-up (a service, cwd /) and for hub run from a terminal
        with contextlib.redirect_stderr(io.StringIO()) as err:
            env = self.pin("hub-claude", {"CLAUDE_CONFIG_DIR": "/personal"})
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/personal")
        self.assertIn("claude_config_dir", err.getvalue())

PART = """import os, sys
with open(sys.argv[1], "a", encoding="utf-8") as f:
    f.write(os.environ.get("CLAUDE_CONFIG_DIR", "-") + "\\n")
"""


class Registry(unittest.TestCase):
    def test_hubd_reads_the_session_registry_of_the_hub_folder(self):
        """Claude Code writes the session registry (<pid>.json: socket, status) into the CLAUDE_CONFIG_DIR folder; the core reads it from there."""
        from core.daemon import Hub
        state = tempfile.mkdtemp(prefix="claude-dir-hubd-")
        self.addCleanup(shutil.rmtree, state, True)
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/hub-claude"}):
            hub = Hub(state, mock.Mock())
        self.assertEqual(hub.registry_dir, os.path.join("/hub-claude", "sessions"))


class HubUpPassesItOn(unittest.TestCase):
    def test_parts_of_the_hub_get_its_folder(self):
        tmp = tempfile.mkdtemp(prefix="claude-dir-up-")
        state, out, part = os.path.join(tmp, "state"), os.path.join(tmp, "seen"), os.path.join(tmp, "part.py")
        os.makedirs(state)
        with open(part, "w", encoding="utf-8") as f:
            f.write(PART)
        hub = os.path.join(tmp, "hub-claude")
        env = {k: v for k, v in os.environ.items() if k not in ("HUB_SERVICE", "INVOCATION_ID", "XPC_SERVICE_NAME")}
        env.update(HUB_STATE_DIR=state, HUB_CONFIG=hub_json(hub), CLAUDE_CONFIG_DIR=os.path.join(tmp, "personal"),
                   HUB_UP_PARTS=json.dumps({"web": [part, out]}), HUB_UP_EXT="{}")
        p = subprocess.Popen([sys.executable, os.path.join(REPO, "bin", "hub-up")], env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            end = time.time() + 30
            while time.time() < end and not os.path.exists(out):
                time.sleep(0.1)
        finally:
            open(os.path.join(state, "stop"), "w").close()
            try:
                p.wait(60)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        with open(out, encoding="utf-8") as f:
            self.assertEqual(f.readline().strip(), os.path.abspath(hub))


if __name__ == "__main__":
    unittest.main()
