"""One's own hub bus relay in one command (spec public relay §3.3): relay/install.sh writes a compose project (the relay
built from the release, Caddy for HTTPS in front), starts it, and prints the first hub's line — kept 0600, the same line
on a run again until the first hub came; what is missing (a name, Docker, Compose) is said in one line.

Docker is a stand-in on PATH here: it records what it is asked and answers as a relay would (no network, no images)."""
import tests  # noqa: F401 — keeps the machine's settings out
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

import platforms

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "relay", "install.sh")
LINE = "hub-relay-invite-1:eyJ2IjoxfQ.c2ln"
SH = shutil.which("sh") or "sh"

DOCKER = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_LOG"
case "$*" in
  *"server.py bootstrap"*)
    case "${FAKE_CODE:-0}" in
      0) printf '%s\\n' "$FAKE_LINE" ;;
      1) echo "1 hub(s) are already on this relay: a new hub comes with a link from one of them." >&2 ;;
      *) echo "a hub would not take this address" >&2 ;;
    esac
    exit "${FAKE_CODE:-0}" ;;
  *exec*) cat > /dev/null ;;   # an exec reads its stdin: with `curl | sh` that must not be the rest of the script
esac
exit 0
"""


@unittest.skipIf(os.name == "nt" or not shutil.which("sh"), "a POSIX shell: the relay runs on a Linux server")
class OwnRelay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hub-relay-sh-")
        self.addCleanup(platforms.current.rmtree, self.tmp)
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        for name, body in (("docker", DOCKER), ("uname", "#!/bin/sh\necho Linux\n"), ("getent", "#!/bin/sh\nexit 0\n"),
                           ("ss", "#!/bin/sh\nexit 0\n")):
            self.tool(name, body)
        self.dir = os.path.join(self.tmp, "hub-relay")
        self.log = os.path.join(self.tmp, "docker.log")

    def tool(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        os.chmod(path, 0o755)

    def run_it(self, *args, code="0", path=None):
        env = {"PATH": path or self.bin + os.pathsep + "/usr/bin:/bin", "HOME": self.tmp, "HUB_RELAY_DIR": self.dir,
               "FAKE_LOG": self.log, "FAKE_LINE": LINE, "FAKE_CODE": code}
        return subprocess.run([SH, SCRIPT, *args], env=env, capture_output=True, text=True, timeout=60)

    def test_the_project_starts_and_the_first_hubs_line_is_printed_and_kept(self):
        r = self.run_it("relay.example.test")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip().splitlines()[-1], LINE)          # stdout: the line alone
        with open(os.path.join(self.dir, "first-hub.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), LINE)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.dir, "first-hub.txt")).st_mode), 0o600)
        with open(os.path.join(self.dir, "compose.yaml"), encoding="utf-8") as f:
            compose = f.read()
        self.assertIn(f"build: \"{os.path.dirname(SCRIPT)}\"", compose)       # a copy from git: this copy's relay/
        self.assertIn("HUB_RELAY_URL: https://relay.example.test", compose)
        self.assertIn('"443:443"', compose)
        with open(os.path.join(self.dir, "Caddyfile"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "relay.example.test {\n\treverse_proxy relay:8080\n}\n")
        with open(self.log, encoding="utf-8") as f:
            asked = f.read()
        self.assertIn("up -d --build", asked)
        self.assertIn("/healthz", asked)                                   # waited for the relay before asking it

    def test_a_run_again_after_the_first_hub_says_links_come_from_it(self):
        self.assertEqual(self.run_it("relay.example.test").returncode, 0)
        r = self.run_it("relay.example.test", code="1")
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertIn("has hubs already", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "first-hub.txt")))   # its line was used

    def test_the_release_builds_its_own_tag(self):
        script = os.path.join(self.tmp, "relay.sh")
        with open(SCRIPT, encoding="utf-8") as f:
            text = f.read().replace("@@HUB_RELAY_REPO@@", "https://github.com/acme/hub.git") \
                .replace("@@HUB_RELAY_TAG@@", "v1.2.3")
        with open(script, "w", encoding="utf-8") as f:
            f.write(text)
        env = {"PATH": self.bin + os.pathsep + "/usr/bin:/bin", "HOME": self.tmp, "HUB_RELAY_DIR": self.dir,
               "FAKE_LOG": self.log, "FAKE_LINE": LINE}
        r = subprocess.run([SH, script, "relay.example.test"], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(self.dir, "compose.yaml"), encoding="utf-8") as f:
            self.assertIn("build: \"https://github.com/acme/hub.git#v1.2.3:relay\"", f.read())

    def test_piped_into_sh_it_runs_whole(self):
        # `curl … | sh -s <name>`: the script comes on stdin, and the execs must not read the rest of it
        with open(SCRIPT, encoding="utf-8") as f:
            text = f.read().replace("@@HUB_RELAY_REPO@@", "https://github.com/acme/hub.git") \
                .replace("@@HUB_RELAY_TAG@@", "v1.2.3")
        env = {"PATH": self.bin + os.pathsep + "/usr/bin:/bin", "HOME": self.tmp, "HUB_RELAY_DIR": self.dir,
               "FAKE_LOG": self.log, "FAKE_LINE": LINE}
        r = subprocess.run([SH, "-s", "relay.example.test"], input=text, env=env, capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip().splitlines()[-1], LINE)

    def test_a_relay_that_gives_no_line_is_a_failure_with_its_words(self):
        r = self.run_it("relay.example.test", code="2")
        self.assertEqual((r.returncode, r.stdout), (1, ""))
        self.assertIn("a hub would not take this address", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "first-hub.txt")))

    def test_what_is_missing_is_said_and_nothing_is_written(self):
        bare = os.path.join(self.tmp, "bare")   # the tools the checks use, and no docker anywhere on the path
        os.makedirs(bare)
        os.symlink(shutil.which("grep"), os.path.join(bare, "grep"))
        os.symlink(os.path.join(self.bin, "uname"), os.path.join(bare, "uname"))
        for args, path, said in (((), None, "the relay's name"), (("not a name",), None, "a DNS name"),
                                 (("203.0.113.5",), None, "not an address"),
                                 (("relay.example.test",), bare, "no Docker")):
            r = self.run_it(*args, path=path)
            self.assertEqual(r.returncode, 1, args)
            self.assertIn(said, r.stderr)
        self.tool("getent", "#!/bin/sh\nexit 2\n")
        r = self.run_it("relay.example.test")
        self.assertEqual(r.returncode, 1)
        self.assertIn("does not resolve", r.stderr)
        self.tool("getent", "#!/bin/sh\nexit 0\n")
        self.tool("ss", "#!/bin/sh\necho 'State Recv-Q Send-Q Local Address:Port'\necho 'LISTEN 0 4096 0.0.0.0:443 x'\n")
        r = self.run_it("relay.example.test")
        self.assertEqual(r.returncode, 1)
        self.assertIn("port 80 or 443 is taken", r.stderr)
        r = self.run_it("relay.example.test\nimport /etc/x")
        self.assertEqual(r.returncode, 1)
        self.assertIn("one line", r.stderr)
        self.tool("ss", "#!/bin/sh\nexit 0\n")
        self.tool("docker", '#!/bin/sh\ncase "$*" in "compose version") exit 1 ;; esac\nexit 0\n')
        r = self.run_it("relay.example.test")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no Docker Compose", r.stderr)
        self.assertFalse(os.path.exists(self.dir))


if __name__ == "__main__":
    unittest.main()
