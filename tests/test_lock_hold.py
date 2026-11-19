"""The "one process" lock is held while the process lives (analysis 2026-10-05: `if lock.acquire(...) is None` dropped
the file, the garbage collector released the lock — a second Telegram bot, "no watchdog" in the browser launcher)."""
import tests  # noqa: F401
import gc
import os
import tempfile
import unittest

from core import lock


class Hold(unittest.TestCase):
    def test_held_until_the_process_ends(self):
        path = os.path.join(tempfile.mkdtemp(prefix="lock-"), "x.lock")
        self.assertTrue(lock.hold(path))
        self.addCleanup(lambda: lock._held.pop().close())  # the test is not a hub process: release it, no ResourceWarning
        gc.collect()
        self.assertIsNone(lock.acquire(path))          # busy — the module holds the reference
        self.assertFalse(lock.hold(path))

    def test_the_parts_hold_their_locks(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for f in ("modules/telegram/bot.py", "modules/browser/run.py", "modules/tailnet/run.py"):
            with open(os.path.join(root, f), encoding="utf-8") as fh:
                src = fh.read()
            self.assertNotIn("if lock.acquire(", src, f)


if __name__ == "__main__":
    unittest.main()
