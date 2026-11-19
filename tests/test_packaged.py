"""A hub from a package (.deb, .app): a PACKAGED file sits next to the code. There is no update from git and a setting
cannot turn it on (owner decision 2026-10-06: no auto-updates). Such a hub has a different Hub Developer: it does not
edit the hub's code - it writes plugins and files issues (owner decision 2026-10-06: the new hub-developer is only for
installs from the app; installs from source stay as they were)."""
import tests  # noqa: F401 - ignore the machine's settings
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from core import catalog, packaged

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Packaged(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="hub-pkg-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.state = os.path.join(self.root, "state")
        os.makedirs(self.state)

    def mark(self, version="1.2.3"):
        path = os.path.join(self.root, "PACKAGED")
        with open(path, "w") as f:
            json.dump({"version": version, "format": "deb"}, f)
        return mock.patch.object(packaged, "MARK", path)

    def config(self, user):
        path = os.path.join(self.root, "hub.json")
        with open(path, "w") as f:
            json.dump(user, f)
        return mock.patch.dict(os.environ, {"HUB_CONFIG": path})

    def test_from_git_nothing_changes(self):
        with mock.patch.object(packaged, "MARK", os.path.join(self.root, "PACKAGED")), self.config({}):
            self.assertIsNone(packaged.info())
            cfg = catalog.load_config(self.state)
        self.assertTrue(cfg["update"]["enabled"])

    def test_package_turns_off_update_even_if_settings_ask(self):
        with self.mark(), self.config({"update": {"enabled": True}}):
            self.assertEqual(packaged.info()["version"], "1.2.3")
            cfg = catalog.load_config(self.state)
        self.assertFalse(cfg["update"]["enabled"])

    def test_update_manual_of_a_layer_hub_reaches_the_update_module_through_load_config(self):
        path = os.path.join(self.root, "PACKAGED")
        with open(path, "w") as f:
            json.dump({"version": "1.2.3", "format": "app", "layout": "layer", "runtime": 1}, f)
        with mock.patch.object(packaged, "MARK", path), self.config({"update": {"manual": True}}):
            cfg = catalog.load_config(self.state)
        from modules.update import update
        self.assertTrue(cfg["update"]["enabled"])
        self.assertTrue(update.button_only(cfg))

    def test_hub_developer_of_a_package_is_the_hub_developer_in_its_own_folder(self):
        # owner decision 2026-11-09: the full Hub Developer stays with everyone — on a package it edits the core's
        # working copy (core/ownsrc.py), never the package code
        from core import dev
        cfg = {"dev": {"name": "hub-dev"}}
        _, src = dev.spec(cfg, self.state)
        self.assertEqual(src["tools"], ["core:hub-developer"])      # from source - as before
        self.assertEqual(src["cwd"], dev.REPO)
        with self.mark():
            name, app = dev.spec(cfg, self.state)
        self.assertEqual(name, "hub-dev")
        self.assertEqual(app["tools"], ["core:hub-developer"])
        self.assertEqual(app["cwd"], os.path.join(self.state, "own"))   # its own folder, not the package code
        self.assertTrue(os.path.isdir(app["cwd"]))
        self.assertIn("working copy", app["about"])

    def test_hub_version_names_the_package_and_where_issues_go(self):
        path = os.path.join(self.root, "PACKAGED")
        with open(path, "w") as f:
            json.dump({"version": "1.2.3", "format": "deb", "issues": "acme/hub-issues"}, f)
        r = subprocess.run([sys.executable, os.path.join(REPO, "bin", "hub"), "version"], capture_output=True, text=True,
                           timeout=30, env={**os.environ, "HUB_STATE_DIR": self.state, "HUB_PACKAGED_MARK": path})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("1.2.3", r.stdout)
        self.assertIn("https://github.com/acme/hub-issues/issues/new", r.stdout)

    def test_hub_up_has_no_update_part_in_a_package(self):
        # bin/hub-up reads the marker next to the code: a repo copy with PACKAGED has no update part
        copy = os.path.join(self.root, "app")
        shutil.copytree(REPO, copy, ignore=shutil.ignore_patterns(".git", "tests", "docs", "__pycache__", "node_modules"))
        probe = "import runpy; g = runpy.run_path(%r, run_name='probe'); print(','.join(g['PARTS']))"
        def parts():
            r = subprocess.run([sys.executable, "-c", probe % os.path.join(copy, "bin", "hub-up")], capture_output=True,
                               text=True, timeout=30, env={k: v for k, v in os.environ.items() if k != "HUB_UP_PARTS"})
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stdout.strip().split(",")
        self.assertIn("update", parts())
        with open(os.path.join(copy, "PACKAGED"), "w") as f:
            json.dump({"version": "1.2.3"}, f)
        got = parts()
        self.assertNotIn("update", got)
        self.assertIn("hubd", got)
        with open(os.path.join(copy, "PACKAGED"), "w") as f:   # the app updates through modules/update (installer S3)
            json.dump({"version": "1.2.3", "format": "app", "runtime": 1}, f)
        self.assertIn("update", parts())

    def test_hub_update_command_says_how_to_update_a_package(self):
        with self.mark():
            r = subprocess.run([sys.executable, os.path.join(REPO, "bin", "hub"), "update"], capture_output=True,
                               text=True, timeout=30, env={**os.environ, "HUB_STATE_DIR": self.state,
                                                           "HUB_PACKAGED_MARK": packaged.MARK})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("a new version of the package", r.stderr)
        self.assertFalse(os.listdir(self.state))   # no update flag was written


if __name__ == "__main__":
    unittest.main()
