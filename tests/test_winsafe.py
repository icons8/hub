"""tests/winsafe.py: a test makes a link only through it — where the machine has no right to links (a Windows user
without Developer Mode, as the update's self-check of a colleague runs), the test skips with one reason or goes on
without the link, and never turns red; where it has the right, the link is made as before."""
import tests  # noqa: F401
import os
import tempfile
import unittest
from unittest import mock

from tests import winsafe
from tests.fakes import can
import platforms


class Links(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="winsafe-")
        self.addCleanup(platforms.current.rmtree, self.d)
        self.target = os.path.join(self.d, "target")
        with open(self.target, "w", encoding="utf-8") as f:
            f.write("x")
        self.link = os.path.join(self.d, "link")

    def no_right(self):
        # as on Windows without the right: the probe says no, and os.symlink itself would fail with WinError 1314
        refuse = mock.patch.object(os, "symlink", side_effect=OSError(1314, "A required privilege is not held"))
        for p in (mock.patch.object(can, "symlink", return_value=False), refuse):
            p.start()
            self.addCleanup(p.stop)

    def test_without_the_right_the_test_skips_with_the_reason_and_nothing_is_made(self):
        self.no_right()
        with self.assertRaises(unittest.SkipTest) as got:
            winsafe.symlink(self, self.target, self.link)
        self.assertEqual(str(got.exception), winsafe.NO_LINKS)
        self.assertFalse(os.path.lexists(self.link))

    def test_without_the_right_the_test_can_go_on_without_the_link(self):
        self.no_right()
        self.assertFalse(winsafe.try_symlink(self.target, self.link))
        self.assertFalse(os.path.lexists(self.link))

    @unittest.skipUnless(can.symlink(), winsafe.NO_LINKS)
    def test_with_the_right_the_link_is_made(self):
        winsafe.symlink(self, self.target, self.link)
        self.assertTrue(os.path.islink(self.link))
        os.unlink(self.link)
        self.assertTrue(winsafe.try_symlink("target", self.link))             # relative, as os.symlink takes it
        with open(self.link, encoding="utf-8") as f:
            self.assertEqual(f.read(), "x")

    @unittest.skipUnless(can.symlink(), winsafe.NO_LINKS)
    def test_another_failure_is_not_taken_for_a_missing_right(self):
        # a link where a file already is — a mistake of the test, not of the machine: it stays red
        open(self.link, "w").close()
        with self.assertRaises(FileExistsError):
            winsafe.try_symlink(self.target, self.link)
        with self.assertRaises(FileExistsError):
            winsafe.symlink(self, self.target, self.link)


if __name__ == "__main__":
    unittest.main()
