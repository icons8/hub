"""Windows without the right to create symlinks (WinError 1314, developer mode off): a link to a directory is a junction.
`hub tools import skill` and `hub direction new --adopt` used to fail on such a machine."""
import tests  # noqa: F401
import os
import stat
import tempfile
import unittest

import platforms


class LinkDir(unittest.TestCase):
    def test_a_link_to_a_folder_reads_through(self):
        d = tempfile.mkdtemp(prefix="link-dir-")
        src, link = os.path.join(d, "src"), os.path.join(d, "link")
        os.makedirs(src)
        with open(os.path.join(src, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("description")
        platforms.current.link_dir(src, link)
        self.assertTrue(os.path.lexists(link))
        with open(os.path.join(link, "SKILL.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "description")

    def test_metacharacters_in_the_source_name_are_taken_literally(self):
        # PR review: cmd /c mklink cut the path at & — the junction pointed elsewhere, the rest ran as a separate command
        for name in ("R&D", "50%", "a^b"):
            d = tempfile.mkdtemp(prefix="link-dir-")
            src, link = os.path.join(d, name, "proj"), os.path.join(d, "link")
            os.makedirs(src)
            with open(os.path.join(src, "x.txt"), "w", encoding="utf-8") as f:
                f.write(name)
            platforms.current.link_dir(src, link)
            with open(os.path.join(link, "x.txt"), encoding="utf-8") as f:
                self.assertEqual(f.read(), name)
            self.assertEqual(sorted(os.listdir(d)), sorted([name, "link"]))   # no extra links alongside


@unittest.skipUnless(os.name == "nt", "Windows only")
class Junction(unittest.TestCase):
    def setUp(self):
        from unittest import mock
        self.d = tempfile.mkdtemp(prefix="link-dir-")
        self.src = os.path.join(self.d, "R&D", "proj")
        os.makedirs(self.src)
        no_right = mock.patch.object(os, "symlink", side_effect=OSError(1314, "no permission"))
        no_right.start()
        self.addCleanup(no_right.stop)

    def test_without_the_symlink_right_it_is_a_junction(self):
        link = os.path.join(self.d, "link")
        platforms.current.link_dir(self.src, link)
        # not os.path.isjunction: it does not exist before Python 3.12, and 3.11 is still supported (tests/__init__.py)
        self.assertEqual(os.lstat(link).st_reparse_tag, stat.IO_REPARSE_TAG_MOUNT_POINT)
        self.assertEqual(sorted(os.listdir(self.d)), ["R&D", "link"])

    def test_when_both_ways_fail_the_message_has_both_reasons(self):
        link = os.path.join(self.d, "link")
        os.mkdir(link)   # occupied: a junction cannot be created
        with self.assertRaises(OSError) as e:
            platforms.current.link_dir(self.src, link)
        self.assertIn("no permission", str(e.exception))
        self.assertIn("junction", str(e.exception))


if __name__ == "__main__":
    unittest.main()
