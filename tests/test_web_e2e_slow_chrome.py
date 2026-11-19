"""Chrome for e2e did not come up (06.10: machine under load, CDP did not open within 15 s — 7 page tests failed with
ECONNREFUSED, and auto-update rolled back because of the machine, not the code): the page test is skipped with a reason."""
import tests  # noqa: F401 — keep the machine's settings out
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

from tests import test_web_e2e


@unittest.skipUnless(test_web_e2e.READY, "Google Chrome and node are needed")
@unittest.skipIf(os.name == "nt", "the fake Chrome is a script with a shebang and /usr/bin/false: macOS and Linux only")
class SlowChrome(unittest.TestCase):
    def test_a_chrome_that_never_listens_skips_the_page_test(self):
        fake = os.path.join(tempfile.mkdtemp(), "chrome")
        with open(fake, "w") as f:
            f.write(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")   # alive, but never opens the CDP port
        os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
        result = unittest.TestResult()
        with mock.patch.object(test_web_e2e, "CHROME", fake), mock.patch.object(test_web_e2e, "CDP_WAIT", 1):
            test_web_e2e.PageInChrome("test_feed_of_the_open_session_live").run(result)
        self.assertEqual((result.failures, result.errors), ([], []))
        self.assertEqual(len(result.skipped), 1)
        self.assertIn("Chrome did not start within 1 s", result.skipped[0][1])

    def test_a_chrome_that_exits_skips_at_once(self):
        result = unittest.TestResult()
        with mock.patch.object(test_web_e2e, "CHROME", "/usr/bin/false"), \
                mock.patch.object(test_web_e2e, "CDP_WAIT", 30):
            test_web_e2e.PageInChrome("test_feed_of_the_open_session_live").run(result)
        self.assertEqual((result.failures, result.errors), ([], []))
        self.assertEqual(len(result.skipped), 1)
        self.assertIn("Chrome exited", result.skipped[0][1])


if __name__ == "__main__":
    unittest.main()
