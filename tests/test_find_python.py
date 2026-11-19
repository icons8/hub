"""The Mac installer used to take the first python3 it found (2026-10-02: a colleague had /usr/bin/python3 from Xcode,
which is 3.9; the hub needs 3.12+). deploy/mac/find-python.sh looks for Python 3.12 or newer: first on PATH, newer
first, then in the usual places."""
import tests  # noqa: F401 — do not see the machine's settings
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIND = os.path.join(ROOT, "deploy", "mac", "find-python.sh")


@unittest.skipIf(os.name == "nt", "the installer is for macOS and Linux — an sh script")
class FindPython(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="findpy-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path, self.extra = os.path.join(self.dir, "path"), os.path.join(self.dir, "extra")
        os.makedirs(self.path)
        os.makedirs(self.extra)

    def fake(self, where, name, version):
        """A Python that answers the version check as `version` would."""
        p = os.path.join(where, name)
        ok = 0 if tuple(version) >= (3, 12) else 1
        with open(p, "w") as f:
            f.write(f"#!/bin/sh\nexit {ok}\n")
        os.chmod(p, 0o755)
        return p

    def find(self):
        env = {"PATH": self.path, "HUB_PY_DIRS": self.extra, "HOME": self.dir}
        r = subprocess.run(["/bin/sh", FIND], env=env, capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else None

    def test_old_python3_first_in_path_is_skipped(self):
        self.fake(self.path, "python3", (3, 9))
        want = self.fake(self.path, "python3.12", (3, 12))
        self.assertEqual(self.find(), want)

    def test_newest_wins(self):
        self.fake(self.path, "python3.12", (3, 12))
        want = self.fake(self.path, "python3.14", (3, 14))
        self.assertEqual(self.find(), want)

    def test_plain_python3_if_new_enough(self):
        want = self.fake(self.path, "python3", (3, 13))
        self.assertEqual(self.find(), want)

    def test_usual_places_outside_path(self):
        self.fake(self.path, "python3", (3, 9))
        want = self.fake(self.extra, "python3.13", (3, 13))
        self.assertEqual(self.find(), want)

    def test_none(self):
        self.fake(self.path, "python3", (3, 9))
        self.assertIsNone(self.find())


if __name__ == "__main__":
    unittest.main()
