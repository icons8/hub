"""The Codex pool cut out of the tree (the open flavor, core/codexslot.py): what a hub without it does."""
import tests  # noqa: F401 — keep the machine's settings out
import contextlib
import io
import os
import unittest
from unittest import mock

from core import clihelp, codexcli, codexslot, hubmod, modrun, version


class WithoutThePool(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(codexslot, "present", lambda: False))
        self.enterContext(mock.patch.object(codexslot, "code", lambda: None))

    def test_the_core_offers_no_codex_point(self):
        self.assertFalse(set(modrun.callable_points()) & set(version.CODEX_POINTS))

    def test_a_module_with_codex_hooks_still_starts(self):
        # one module version serves cores with and without the pool: hello leaves the point out (hubmod._hello_for)
        hubmod._check_start({"capabilities": ["profile"]}, {"codex.list": None, "profile.list": None}, {})

    def test_the_pool_commands_are_a_usage_error(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(codexcli.main(["add"], "/nonexistent"), 2)
        self.assertNotIn("add", err.getvalue().split("|")[-1])

    def test_no_pool_drops_what_the_state_has_of_one(self):
        got = []
        st = {"codex": {"pool": {"on": True, "profiles": [{"name": "codex-2", "home": "/x"}]},
                        "account": {"active": "codex-2"}}}
        codexslot.NoPool().tick(lambda: st, lambda kind, data: got.append(kind))
        self.assertEqual(got, ["codex.pool", "codex.switched"])
        got.clear()
        codexslot.NoPool().tick(lambda: {}, lambda kind, data: got.append(kind))
        self.assertEqual(got, [])


class BrokenEmbed(unittest.TestCase):
    def test_a_pool_that_does_not_import_is_said_not_a_traceback(self):
        def boom():
            raise SyntaxError("half-updated")
        err = io.StringIO()
        with mock.patch.object(codexslot, "code", boom), contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(codexcli.main(["use", "codex-2"], "/nonexistent"), 2)
        self.assertIn("half-updated", err.getvalue())


class Help(unittest.TestCase):
    def test_a_pool_word_gets_the_pool_entry_and_login_the_sign_ins(self):
        h = clihelp.load()
        if not codexslot.present():
            self.skipTest("the Codex pool is cut out of this tree")
        self.assertIn("use <account>", h.entry("codex", ["use"]).forms[0])
        self.assertIn("use <account>", h.render(["codex", "use"]))
        self.assertEqual(h.entry("codex", ["login"]).key, "codex")
        self.assertEqual(h.entry("codex", []).key, "codex")


if __name__ == "__main__":
    unittest.main()
