"""Google Chrome for the whole machine as the agents' browser where a Chrome in the user's folder cannot serve (one-line
install spec §5.5): Ubuntu 23.10+ gives the sandbox only to a program with an AppArmor profile, a server lacks Chrome's
libraries. «Setup» and the app's band offer one root command (modules/setup/chrome.system_command) that fetches Google's
package as root, into root's own new folder — root never runs anything a session of the hub can write."""
import tests  # noqa: F401 - keeps the machine's settings out
import os
import shlex
import subprocess
import tempfile
import unittest
from unittest import mock

import platforms
from modules.browser import browsers
from modules.setup import chrome, setup

POSIX = os.name == "posix"


def mktemp_follows_tmpdir():
    """Whether this machine's `mktemp -d` makes its folder in $TMPDIR (GNU's does; macOS's makes it in its own temp folder)."""
    base = tempfile.mkdtemp(prefix="hub-chrome-probe-")
    try:
        r = subprocess.run(["/bin/sh", "-c", "mktemp -d"], env={**os.environ, "TMPDIR": base}, capture_output=True,
                           text=True, timeout=30)
        return r.returncode == 0 and os.path.dirname(r.stdout.strip()) == base
    finally:
        platforms.current.rmtree(base)
PIN = {"version": "155.0.1", "url": "https://example.test/c.zip", "sha256": "0" * 64, "size": 10}


def which(*have):
    return lambda name: f"/usr/bin/{name}" if name in have else None


class Command(unittest.TestCase):
    def test_debian_and_ubuntu_get_google_s_package_fetched_by_root_into_its_own_folder(self):
        cmd = chrome.system_command(which("apt-get", "curl"), "linux-x64")
        self.assertTrue(cmd.startswith("sudo sh -c '"), cmd)
        inner = shlex.split(cmd)[3]
        self.assertIn('d=$(mktemp -d) && chmod 755 "$d" && cd "$d"', inner)
        self.assertIn(f"curl -fsSLO {chrome.GOOGLE_DEB}", inner)
        # a stale index (a cloud image) fails on 404s of the dependencies; a broken third-party repository fails the
        # update — the install goes on either way
        self.assertIn("(apt-get update -qq || :) && apt-get install -y ./google-chrome-stable_current_amd64.deb", inner)

    def test_without_curl_wget_fetches_and_without_either_there_is_no_command(self):
        self.assertIn(f"wget -q {chrome.GOOGLE_DEB}", chrome.system_command(which("apt-get", "wget"), "linux-x64"))
        self.assertIsNone(chrome.system_command(which("apt-get"), "linux-x64"))

    def test_fedora_gets_the_rpm_by_dnf(self):
        self.assertEqual(chrome.system_command(which("dnf", "curl"), "linux-x64"),
                         f"sudo dnf install -y {chrome.GOOGLE_RPM}")

    def test_no_command_where_google_has_no_package(self):
        for plat in ("linux-arm64", "mac-arm64", "windows-x64", None):
            with mock.patch.object(chrome, "key", lambda: None):
                self.assertIsNone(chrome.system_command(which("apt-get", "curl", "dnf"), plat), plat)
        self.assertIsNone(chrome.system_command(which("pacman", "curl"), "linux-x64"))

    def test_the_command_runs_nothing_from_the_hub_s_folders(self):
        for cmd in (chrome.system_command(which("apt-get", "curl"), "linux-x64"),
                    chrome.system_command(which("apt-get", "wget"), "linux-x64"),
                    chrome.system_command(which("dnf"), "linux-x64")):
            words = shlex.split(cmd)
            inner = shlex.split(words[3]) if words[1:3] == ["sh", "-c"] else []
            for word in words[:3] + inner + words[4:]:
                # what root runs: system programs by name, the package by https, a file of the folder it made itself
                self.assertFalse(word.startswith(("/", "~", "$HOME")) or "HOME" in word or ".hub" in word, (word, cmd))
                if "://" in word:
                    self.assertTrue(word.startswith("https://dl.google.com/"), word)

    @unittest.skipUnless(POSIX and mktemp_follows_tmpdir(), "a shell command of Linux: root's GNU mktemp")
    def test_the_command_as_a_shell_runs_it(self):
        # stand-ins for sudo, curl and apt-get: what the shell makes of the quoting, where the package lands, that a
        # failed index update does not stop the install and the folder is gone after it
        for update_rc in (0, 100):
            with self.subTest(update_rc=update_rc):
                tmp = tempfile.mkdtemp(prefix="hub-chrome-cmd-")
                self.addCleanup(platforms.current.rmtree, tmp)
                bin_dir, log = os.path.join(tmp, "bin"), os.path.join(tmp, "apt.log")
                os.makedirs(bin_dir)
                stubs = {"sudo": 'exec "$@"\n',
                         "curl": 'for a; do u=$a; done\necho deb > "${u##*/}"\n',
                         "apt-get": f'[ "$1" = update ] && exit {update_rc}\n'
                                    f'echo "$PWD|$*|$(cat "$3")" > {shlex.quote(log)}\nexit 7\n'}
                for name, body in stubs.items():
                    path = os.path.join(bin_dir, name)
                    with open(path, "w", newline="\n") as f:
                        f.write("#!/bin/sh\n" + body)
                    os.chmod(path, 0o755)
                cmd = chrome.system_command(which("apt-get", "curl"), "linux-x64")
                env = {**os.environ, "PATH": bin_dir + os.pathsep + "/usr/bin" + os.pathsep + "/bin", "TMPDIR": tmp}
                r = subprocess.run(["/bin/sh", "-c", cmd], env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(r.returncode, 7, r.stderr)   # apt-get's own code comes back
                with open(log) as f:
                    where, args, body = f.read().strip().split("|")
                self.assertEqual(os.path.dirname(where), tmp)   # a folder of its own, made by root's mktemp
                self.assertEqual((args, body), ("install -y ./google-chrome-stable_current_amd64.deb", "deb"))
                self.assertFalse(os.path.exists(where))         # the package does not stay behind


class Item(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hub-chrome-item-")
        self.addCleanup(platforms.current.rmtree, self.tmp)
        self.switch = os.path.join(self.tmp, "userns")
        for p in (mock.patch("modules.testchrome.chrome.USERNS_SWITCH", self.switch),
                  mock.patch.object(chrome, "pinned", return_value=PIN),
                  mock.patch.object(chrome, "system_command", return_value="sudo dnf install -y x"),
                  mock.patch.object(browsers, "find_chrome", side_effect=browsers.Problem("none"))):
            p.start()
            self.addCleanup(p.stop)
        self.s = setup.Setup(self.tmp, lambda: {})

    def block(self, on):
        with open(self.switch, "w") as f:
            f.write("1\n" if on else "0\n")

    def test_where_the_sandbox_is_forbidden_the_row_names_the_command(self):
        self.block(True)
        row = self.s.item_chrome({})
        self.assertEqual((row["state"], row["sandbox"], row["system"]), ("todo", "", "sudo dnf install -y x"))

    def test_a_package_s_own_command_comes_first(self):
        self.block(True)
        with mock.patch.object(chrome, "sandbox_command", return_value="sudo agents-hub chrome"):
            row = self.s.item_chrome({})
        self.assertEqual(row["sandbox"], "sudo agents-hub chrome")
        self.assertNotIn("system", row)

    def test_a_chrome_that_came_and_does_not_start_gets_the_command_another_failure_does_not(self):
        self.block(False)
        for why, has in (("start", True), ("checksum", False), ("network", False)):
            with mock.patch.object(self.s, "job", lambda step, why=why: {"state": "failed", "why": why}):
                row = self.s.item_chrome({})
            self.assertEqual(("system" in row, row["state"]), (has, "failed"), why)

    def test_nothing_is_said_where_a_chrome_of_the_user_s_folder_serves(self):
        self.block(False)
        self.assertNotIn("system", self.s.item_chrome({}))

    def test_the_command_line_text_names_it(self):
        self.block(True)
        self.assertIn("sudo dnf install -y x", chrome.sandbox_help())


if __name__ == "__main__":
    unittest.main()
