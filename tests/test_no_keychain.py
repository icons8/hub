"""Tests do not touch the Mac keychain (tests/__init__.py). The update self-check runs the tests on the owner's Mac, and a
real claude in them (tests/test_mcp_login_live.py: a finished MCP sign-in) stores its token through `security
add-generic-password`; with the tests' temporary HOME macOS finds no login keychain and shows the owner the window «A
keychain cannot be found to store …» (2026-10-30, an update to v1.6.0). In tests `security` answers as an empty keychain
that takes no writes: Claude Code then keeps the sign-in in its credentials file, and nothing reaches the keychain."""
import tests  # noqa: F401 — do not see the machine's settings
import os
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipIf(os.name == "nt", "Windows: no `security`, Claude Code keeps sign-ins in the Credential Manager")
class NoKeychain(unittest.TestCase):
    def path_only(self):
        """The environment as the live claude tests build it: PATH from the tests' environment, a temporary HOME, no USER."""
        return {"PATH": os.environ["PATH"], "HOME": tempfile.mkdtemp(prefix="nokc-home-")}

    def test_security_is_the_tests_stand_in(self):
        found = shutil.which("security", path=self.path_only()["PATH"])
        self.assertEqual(os.path.realpath(found or ""),
                         os.path.realpath(os.path.join(tempfile.gettempdir(), "bin", "security")))

    def test_a_write_finds_no_keychain_and_a_lookup_finds_nothing(self):
        """The codes Claude Code reads (2.1.312): a lookup and a removal — 44 «no such item» (any other code on a read
        is «unreadable», and then it saves the sign-in nowhere); a write, by arguments or by `security -i` with the
        command on stdin — 50 «no default keychain»: not 0 (stored) and not 36 (locked, retry later), so the sign-in goes
        to the credentials file."""
        env = self.path_only()
        for args, stdin, code in (
                (["-i"], 'add-generic-password -U -a "x" -s "maps-1" -X "7b7d"\n', 50),
                (["add-generic-password", "-U", "-a", "x", "-s", "maps-1", "-X", "7b7d"], None, 50),
                (["find-generic-password", "-a", "x", "-w", "-s", "maps-1"], None, 44),
                (["delete-generic-password", "-a", "x", "-s", "maps-1"], None, 44),
                (["show-keychain-info"], None, 50)):
            r = subprocess.run(["security", *args], input=stdin or "", capture_output=True, text=True, env=env,
                               timeout=10)
            self.assertEqual((r.returncode, r.stdout), (code, ""), args)
            self.assertIn("keychain", r.stderr)


if __name__ == "__main__":
    unittest.main()
