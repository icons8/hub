"""Tests clean up after themselves (04.10: a disk filled to 100%; the temp folder held 16 thousand directories from test
runs, ~10 thousand a day, plus Chrome profiles from e2e). One run directory (tests/__init__.py) holds every mkdtemp,
subprocesses' too (TMPDIR); it is deleted on exit."""
import tests  # noqa: F401
import os
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class CleanUp(unittest.TestCase):
    def test_a_run_leaves_nothing_in_the_temp_folder(self):
        tmp = tempfile.mkdtemp(prefix="clean-check-")
        abandoned = os.path.join(tmp, "hub-core-tests-run-old12345")   # an abandoned run (killed by a rollback)
        os.makedirs(abandoned)
        with open(os.path.join(abandoned, "pid"), "w") as f:
            f.write(str(2 ** 22 + 12345))   # its process is gone
        old = time.time() - 2 * 86400
        os.utime(abandoned, (old, old))
        env = {**os.environ, "TMPDIR": tmp}
        r = subprocess.run([sys.executable, "-m", "unittest", "tests.test_task_record"], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        self.assertEqual(os.listdir(tmp), [])                          # neither our own nor the abandoned one


if __name__ == "__main__":
    unittest.main()
