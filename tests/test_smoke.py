"""deploy/smoke.sh — a clean install checked end to end: the Linux installer into a temporary home folder with the service
manager cut off, hub-up started by hand as the installer says, the page and `hub status`, then `hub uninstall --purge
--yes` and nothing of the hub left. It runs the real installer, hub-up and uninstall — no stand-ins but claude — so it
fails whenever one of them stops fitting the others (a step `hub uninstall` cannot do in a container, a file the
installer leaves behind). Its --here mode refuses a machine where a hub already is, before it changes anything."""
import tests  # noqa: F401 — keep this machine's settings out
import os
import signal
import subprocess
import sys
import tempfile
import unittest

import platforms

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SMOKE = os.path.join(REPO, "deploy", "smoke.sh")


@unittest.skipUnless(sys.platform.startswith("linux"), "the temporary-home mode is Linux's: launchd cannot be cut off")
class Smoke(unittest.TestCase):
    def env(self, home):
        return {"HOME": home, "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8",
                "TMPDIR": tempfile.gettempdir(), "HUB_NO_OFFICIAL_MARKETPLACE": "1", "HUB_NO_MODEL": "1",
                "HUB_NO_STATS": "1", "HUB_NO_SYSTEM_AGENTS": "1"}

    def test_install_page_status_uninstall_and_nothing_left(self):
        home = tempfile.mkdtemp(prefix="smoke-home-")
        self.addCleanup(platforms.current.rmtree, home)
        # its own process group: a run that overstays is ended with the hub-up it started, which takes its parts down
        p = subprocess.Popen(["sh", SMOKE], env=self.env(home), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, text=True, start_new_session=True)
        try:
            out, _ = p.communicate(timeout=300)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGTERM)   # portable: Linux only (the class), a group of its own
            try:
                out, _ = p.communicate(timeout=90)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)   # portable: as above
                out, _ = p.communicate()
            self.fail("smoke.sh overstayed 300 s:\n" + out)
        self.assertEqual(p.returncode, 0, out)
        for phase in ("install", "start", "page", "status", "uninstall", "clean"):
            self.assertRegex(out, rf"(?m)^phase {phase} ok ", out)
        self.assertTrue(out.rstrip().endswith("SMOKE OK"), out)
        self.assertEqual(os.listdir(home), [])   # the caller's home is not where it installs

    def test_a_line_that_does_not_download_fails_its_install_and_leaves_nothing(self):
        home = tempfile.mkdtemp(prefix="smoke-line-")
        self.addCleanup(platforms.current.rmtree, home)
        r = subprocess.run(["sh", SMOKE, "--line", "http://127.0.0.1:9/i/some-key-0123456789/install.sh"],
                           env=self.env(home), capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 1, out)
        self.assertRegex(out, r"(?m)^phase install fail ", out)
        self.assertRegex(out, r"(?m)^phase clean ok ", out)
        self.assertIn("did not download", out)
        self.assertNotIn("some-key-0123456789", out)   # the build key of the address is not printed
        self.assertTrue(out.rstrip().endswith("SMOKE FAIL"), out)
        self.assertEqual(os.listdir(home), [])

    def test_a_wrong_flag_or_a_line_without_its_address_is_a_usage_error(self):
        home = tempfile.mkdtemp(prefix="smoke-args-")
        self.addCleanup(platforms.current.rmtree, home)
        for args in (["--what"], ["--line"], ["--line", ""]):
            r = subprocess.run(["sh", SMOKE, *args], env=self.env(home), capture_output=True, text=True, timeout=60,
                               stdin=subprocess.DEVNULL)
            self.assertEqual(r.returncode, 2, (args, r.stdout + r.stderr))
            self.assertIn("usage", r.stderr)

    def test_here_refuses_a_machine_with_the_environments_of_a_one_line_hub(self):
        home = tempfile.mkdtemp(prefix="smoke-here-")
        self.addCleanup(platforms.current.rmtree, home)
        shells = os.path.join(home, ".local", "share", "agents-hub", "shells")
        os.makedirs(shells)
        r = subprocess.run(["sh", SMOKE, "--here", "--line", "http://127.0.0.1:9/i/k/install.sh"], env=self.env(home),
                           capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("agents-hub exists", r.stderr)
        self.assertTrue(os.path.isdir(shells))

    def test_here_refuses_a_machine_with_a_hub(self):
        home = tempfile.mkdtemp(prefix="smoke-here-")
        self.addCleanup(platforms.current.rmtree, home)
        journal = os.path.join(home, ".hub-core", "journal.jsonl")
        os.makedirs(os.path.dirname(journal))
        with open(journal, "w") as f:
            f.write('{"seq":1}\n')
        r = subprocess.run(["sh", SMOKE, "--here"], env=self.env(home), capture_output=True, text=True, timeout=60,
                           stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("not a clean machine", r.stderr)
        self.assertTrue(os.path.exists(journal))
        self.assertEqual(sorted(os.listdir(home)), [".hub-core"])


if __name__ == "__main__":
    unittest.main()
