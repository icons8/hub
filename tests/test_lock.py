import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# counts under the lock: read the number, wait, write +1 — without mutual exclusion the count is lost
WORKER = """
import sys, time
sys.path.insert(0, sys.argv[1])
from core import lock
path, count = sys.argv[2], sys.argv[3]
for _ in range(int(sys.argv[4])):
    with lock.exclusive(path):
        with open(count) as f:
            n = int(f.read() or 0)
        time.sleep(0.002)
        with open(count, "w") as f:
            f.write(str(n + 1))
"""


class ExclusiveTest(unittest.TestCase):
    def test_processes_wait_for_each_other(self):
        d = tempfile.mkdtemp(prefix="lock-")
        lock_path, count = os.path.join(d, "x.lock"), os.path.join(d, "count")
        open(count, "w").close()
        procs = [subprocess.Popen([sys.executable, "-c", WORKER, REPO, lock_path, count, "15"]) for _ in range(3)]
        for p in procs:
            self.assertEqual(p.wait(60), 0)
        with open(count) as f:
            self.assertEqual(f.read(), "45")

    def test_core_loads_without_fcntl(self):
        # 10-05: on Windows `import core.daemon` failed on `import fcntl` (core/taskrec) — hub-up and hubd did not start.
        # Without fcntl the core and task recording load on any OS: the module is blocked the same way it is missing on Windows
        code = ("import sys; sys.modules['fcntl'] = None; sys.path.insert(0, sys.argv[1]); "
                "import core.daemon, core.taskrec; print('ok')")
        out = subprocess.run([sys.executable, "-c", code, REPO], capture_output=True, text=True, timeout=60)
        self.assertEqual((out.returncode, out.stdout.strip()), (0, "ok"), out.stderr)


if __name__ == "__main__":
    unittest.main()
