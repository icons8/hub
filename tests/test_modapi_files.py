"""Module API 2.4 for modules with files: hubmod.servable — whether /api/file serves this path (the page rule, core/shown.py),
hubmod.data_dir — the module's own data folder, data: fonts in the HTML view sandbox."""
import tests  # noqa: F401 — keep the machine's settings out of the test
import os
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from core import hubmod, journal


class ModuleApi(unittest.TestCase):
    def setUp(self):
        self.state = os.path.realpath(tempfile.mkdtemp(prefix="hubshown-"))
        self.addCleanup(shutil.rmtree, self.state, ignore_errors=True)
        env = mock.patch.dict(os.environ, {"HUB_STATE_DIR": self.state, "HUB_MODULE": "files"})
        env.start()
        self.addCleanup(env.stop)
        hubmod._shown = None
        self.addCleanup(setattr, hubmod, "_shown", None)

    def test_servable_answers_like_the_page(self):
        j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        p = os.path.join(self.state, "r.pdf")
        with open(p, "w") as f:
            f.write("x")
        self.assertIsNone(hubmod.servable(p))
        j.append("owner.file", None, {"path": p, "caption": "", "channel": "session", "owner": False, "sender": "a"})
        self.assertEqual(hubmod.servable(p), p)

    def test_data_dir_is_private_and_per_module(self):
        d = hubmod.data_dir()
        self.assertEqual(d, os.path.join(self.state, "moddata", "files"))
        self.assertTrue(os.path.isdir(d))
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)
            os.chmod(d, 0o755)
            hubmod.data_dir()
            self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)
        for bad in ("", "../x", "a/b"):
            with mock.patch.dict(os.environ, {"HUB_MODULE": bad}):
                with self.assertRaises(RuntimeError):
                    hubmod.data_dir()


class ViewSandbox(unittest.TestCase):
    def test_fonts_inline_and_nothing_else_widened(self):
        from modules.web import modproxy
        parts = dict(p.strip().split(" ", 1) for p in modproxy.VIEW_CSP.split(";"))
        self.assertEqual(parts["font-src"], "data:")
        self.assertEqual(parts["default-src"], "'none'")
        # popups: a link with target="_blank" in a view opens (the opened window leaves the sandbox); no same-origin,
        # forms or top navigation
        self.assertEqual(parts["sandbox"], "allow-scripts allow-popups allow-popups-to-escape-sandbox")
        self.assertEqual(parts["img-src"], "data: blob:")
        self.assertNotIn("connect-src", parts)


if __name__ == "__main__":
    unittest.main()
