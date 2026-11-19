"""The environment of a module process (core/modrun.launch): claude login keys from the hub's environment do not go to
the module; the core gives the module a model (the model right), not a service token; the rest of the environment is
the same as hub-up's."""
import tests  # noqa: F401 — keeps the machine's settings out
import shutil
import sys
import tempfile
import types
import unittest
import unittest.mock

from core import modcli, modlaunch, modrun


class ModuleEnv(unittest.TestCase):
    def launched_env(self, env):
        state = tempfile.mkdtemp(prefix="modenv-")
        self.addCleanup(shutil.rmtree, state, True)
        got = {}

        def spawn(cmd, cwd, e):
            got.update(e)
            return types.SimpleNamespace(pid=None)
        modrun.launch(state, "maps", {"cmd": ["true"], "cwd": state}, spawn, env=env)
        return got

    def test_login_keys_are_not_passed_to_the_module(self):
        keys = {"CLAUDE_CODE_OAUTH_TOKEN": "t", "ANTHROPIC_API_KEY": "k", "ANTHROPIC_AUTH_TOKEN": "a",
                "ANTHROPIC_CUSTOM_HEADERS": "x-api-key: h", "CLAUDE_CODE_SESSION_ACCESS_TOKEN": "s",
                "CLAUDE_CODE_USE_BEDROCK": "1"}
        self.assertTrue(all(modlaunch.login_name(k) for k in keys))   # the same list as the core's login rule
        env = self.launched_env({**keys, "PATH": "/bin", "LANG": "C.UTF-8", "HUB_SESSION": "maps"})
        self.assertEqual([k for k in keys if k in env], [])
        self.assertEqual((env["PATH"], env["LANG"], env["HUB_MODULE"]), ("/bin", "C.UTF-8", "maps"))
        self.assertNotIn("HUB_SESSION", env)


class ModuleProgram(unittest.TestCase):
    """`python3` and `python` in run mean the hub's own Python on any OS (on Windows `python3` is often a Store stub)."""

    def spawned(self, cmd, dev=False):
        state = tempfile.mkdtemp(prefix="modcmd-")
        self.addCleanup(shutil.rmtree, state, True)
        got = []
        want = {"cmd": cmd, "cwd": state, **({"dev": True} if dev else {})}

        def spawn(c, cwd, e, **out):
            got.append(c)
            return types.SimpleNamespace(pid=None)
        modrun.launch(state, "maps", want, spawn, env={})
        self.addCleanup(modrun._close_log, "maps")   # the dev module's log is open in hub-up; Windows would not delete the folder holding it
        self.assertEqual(want["cmd"], cmd)   # want (and the launch key with it) does not change
        return got[0]

    def test_the_first_word_python_is_the_hubs_python(self):
        for word in ("python3", "python"):
            for dev in (False, True):
                with self.subTest(word=word, dev=dev):
                    self.assertEqual(self.spawned([word, "main.py", "--x"], dev)[1:][-2:], ["main.py", "--x"])
                    self.assertEqual(self.spawned([word, "main.py"], dev)[0], sys.executable)

    def test_other_programs_and_other_words_are_left_alone(self):
        for cmd in (["node", "main.js"], ["python3.12", "main.py"], ["./python3", "x"], ["sh", "-c", "python3 x"],
                    [sys.executable, "main.py"]):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.spawned(cmd), cmd)

    def test_doctor_does_not_look_for_python_in_path(self):
        with unittest.mock.patch("shutil.which", return_value=None):
            self.assertTrue(modcli._program(["python3", "main.py"], "/nonexistent"))
            self.assertTrue(modcli._program(["python", "main.py"], "/nonexistent"))
            self.assertFalse(modcli._program(["node", "main.js"], "/nonexistent"))


if __name__ == "__main__":
    unittest.main()
