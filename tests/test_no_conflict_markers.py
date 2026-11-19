"""The repo has no unresolved merge conflicts (2026-10-07: markers got into CHANGES.md on GitHub and every hub saw them
on the Updates page). A marker is a line starting with "<<<<<<< " or ">>>>>>> "."""
import tests  # noqa: F401 - ignore the machine's settings
import os
import re
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARK = re.compile(r"^(<{7}|>{7}) ", re.M)


class NoConflictMarkers(unittest.TestCase):
    def test_tracked_files_have_no_conflict_markers(self):
        try:
            files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("not a git copy (package)")
        bad = []
        for f in files:
            try:
                with open(os.path.join(ROOT, f), encoding="utf-8") as fh:
                    text = fh.read()
            except (OSError, UnicodeDecodeError):
                continue
            bad += [f"{f}:{text[:m.start()].count(chr(10)) + 1}" for m in MARK.finditer(text)]
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
