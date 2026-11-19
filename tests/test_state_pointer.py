"""The mark of the default state folder (hub-core/state-dir in the settings folder, written by the installers). On Linux
the installer puts it under $XDG_CONFIG_HOME, where the hub also reads hub.json; every reader looks there first and then
in ~/.config (installs that ignored XDG). A reader that looked only in ~/.config sent a hub on a custom state folder to
the default one."""
import tests  # noqa: F401 — keep the machine's settings out of the test
import importlib.util
import os
import shutil
import platforms
import subprocess
import tempfile
import unittest
from unittest import mock

from core import daemon, runtime, statemark, uninstall

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STOPS_AUDIT = os.path.join(ROOT, "modules", "agent_dev", "stops-audit.py")
DEB_WRAPPER = os.path.join(ROOT, "tools", "package", "deb", "files", "hub")


def put(*parts, text):
    os.makedirs(os.path.dirname(os.path.join(*parts)), exist_ok=True)
    with open(os.path.join(*parts), "w") as f:
        f.write(text)


@unittest.skipIf(os.name == "nt", "the mark of the Linux installer; Windows keeps the settings in %APPDATA%")
class Pointer(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="pointer-"))
        self.addCleanup(platforms.current.rmtree, self.tmp)
        self.home, self.xdg = os.path.join(self.tmp, "home"), os.path.join(self.tmp, "xdg")
        os.makedirs(self.home)
        self.env = {"HOME": self.home, "XDG_CONFIG_HOME": self.xdg}
        patch = mock.patch.dict(os.environ, self.env)
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("HUB_STATE_DIR", None)

    def readers(self):
        spec = importlib.util.spec_from_file_location("stops_audit_pointer", STOPS_AUDIT)
        audit = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(audit)
        return {"daemon": daemon.default_state_dir, "runtime": runtime.state_dir,
                "uninstall": lambda: uninstall.default_state(self.home), "stops-audit": audit.hub_dir}

    def test_the_mark_under_xdg_is_found(self):
        put(self.xdg, "hub-core", "state-dir", text="/home/x/state-a\n")
        for name, read in self.readers().items():
            with self.subTest(name):
                self.assertEqual(read(), "/home/x/state-a")

    def test_the_mark_of_an_install_that_ignored_xdg_is_found(self):
        put(self.home, ".config", "hub-core", "state-dir", text="/home/x/state-b\n")
        for name, read in self.readers().items():
            with self.subTest(name):
                self.assertEqual(read(), "/home/x/state-b")

    def test_xdg_goes_first(self):
        put(self.xdg, "hub-core", "state-dir", text="/home/x/state-a\n")
        put(self.home, ".config", "hub-core", "state-dir", text="/home/x/state-b\n")
        for name, read in self.readers().items():
            with self.subTest(name):
                self.assertEqual(read(), "/home/x/state-a")

    def test_the_plain_environment_variable_goes_before_any_mark(self):
        put(self.xdg, "hub-core", "state-dir", text="/home/x/state-a\n")
        with mock.patch.dict(os.environ, {"HUB_STATE_DIR": "/home/x/state-c"}):
            for name, read in self.readers().items():
                if name != "uninstall":   # it names the folder to remove, not the one the variable chose
                    with self.subTest(name):
                        self.assertEqual(read(), "/home/x/state-c")

    def test_the_home_of_someone_else_has_only_its_own_mark(self):
        other = os.path.join(self.tmp, "other")
        self.assertEqual(statemark.pointers(other), [os.path.join(other, ".config", "hub-core", "state-dir")])
        put(self.xdg, "hub-core", "state-dir", text="/home/x/state-a\n")
        self.assertIsNone(statemark.default_state(other))

    def test_one_folder_written_two_ways_is_one_mark(self):
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.home, ".config") + os.sep   # the default, with a slash
        self.assertEqual(statemark.pointers(), [os.path.join(self.home, ".config", "hub-core", "state-dir")])

    def test_a_purge_removes_every_mark_that_names_the_folder(self):
        state, cfg = os.path.join(self.tmp, "state-a"), os.path.join(self.tmp, "cfg")
        put(state, "journal.jsonl", text="")
        put(self.xdg, "hub-core", "state-dir", text=state + "\n")
        put(self.home, ".config", "hub-core", "state-dir", text=state + "\n")
        steps = uninstall.plan(state, cfg, home=self.home, repo=self.tmp, purge=True)
        gone = {s["path"] for s in steps if s["do"] == "remove"}
        self.assertTrue({os.path.join(self.xdg, "hub-core", "state-dir"),
                         os.path.join(self.home, ".config", "hub-core", "state-dir")} <= gone)

    def test_the_command_of_a_package_finds_the_state_folder_by_the_mark_under_xdg(self):
        state = os.path.join(self.tmp, "state-a")
        put(state, "code", "bin", "hub", text="")
        put(self.xdg, "hub-core", "state-dir", text=state + "\n")
        env = {k: v for k, v in os.environ.items() if not k.startswith("HUB_")}
        r = subprocess.run(["sh", DEB_WRAPPER], env=env, capture_output=True, text=True, timeout=30)
        self.assertNotIn("Agents Hub", r.stderr)   # not the "the hub has not been started yet" message: it reached the exec of the packaged Python


if __name__ == "__main__":
    unittest.main()
