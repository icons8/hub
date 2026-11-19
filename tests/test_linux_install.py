"""Installing on Linux (deploy/install.sh) — as on Mac and Windows: no root, state in ~/.hub-core, settings in
~/.config/hub-core/hub.json with the hub session (once, later edited in place), the hub and hub-telegram commands in
~/.local/bin, the hub as one systemd --user service that holds bin/hub-up. 06.10: the previous script was for a VM —
it wrote to /data/hub (needs root), installed five services piecemeal without hub-up and did not enable the hub session.

The check runs in a foreign HOME with systemctl and loginctl replaced by call recorders: the test does not touch the real system."""
import tests  # noqa: F401 — ignore this machine's settings
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from tests import winsafe

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(REPO, "deploy", "install.sh")


def stub(path, body):
    with open(path, "w") as f:
        f.write("#!/bin/sh\n" + body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)


@unittest.skipIf(os.name == "nt", "an sh script")
class LinuxInstall(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="lin-home-")
        self.addCleanup(shutil.rmtree, self.home, True)
        self.bin = os.path.join(self.home, "stubs")
        os.makedirs(self.bin)
        self.calls = os.path.join(self.home, "calls.log")
        for name in ("systemctl", "loginctl"):
            stub(os.path.join(self.bin, name), f'echo "{name} $*" >> "{self.calls}"\n'
                 + ('[ "$2" = show-user ] && echo Linger=no\n' if name == "loginctl" else "") + "exit 0\n")
        stub(os.path.join(self.bin, "claude"), "exit 0\n")
        # the first name the installer tries: the system python3.12 in /usr/bin will not intercept it (07.10, Pop!_OS 24.04)
        winsafe.symlink(self, sys.executable, os.path.join(self.bin, "python3.14"))   # portable: the installer's name
        self.env = {"HOME": self.home, "PATH": self.bin + ":/usr/bin:/bin", "HUB_INSTALL_NO_WAIT": "1",
                    "LANG": "C.UTF-8", "USER": "x"}

    def run_install(self):
        r = subprocess.run(["sh", INSTALL], env=self.env, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout + r.stderr

    def cfg(self):
        with open(os.path.join(self.home, ".config", "hub-core", "hub.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_one_user_service_with_hub_up_and_the_hub_session_on(self):
        out = self.run_install()
        state = os.path.join(self.home, ".hub-core")
        self.assertEqual(self.cfg()["hub"], {"enabled": True, "name": "hub-core", "cwd": os.path.join(state, "hub-home")})
        for name in ("CLAUDE.md", "ONBOARDING.md"):
            self.assertTrue(os.path.samefile(os.path.join(state, "hub-home", name), os.path.join(REPO, "hub-home", name)))
        with open(os.path.join(self.home, ".config", "hub-core", "state-dir")) as f:
            self.assertEqual(f.read().strip(), state)
        with open(os.path.join(self.home, ".config", "systemd", "user", "agents-hub.service")) as f:
            unit = f.read()
        self.assertIn(f"ExecStart={self.bin}/python3.14 {REPO}/bin/hub-up", unit)   # the Python found by the installer
        self.assertIn("WantedBy=default.target", unit)
        self.assertNotIn("After=default.target", unit)   # 30.09: a cycle with WantedBy — systemd dropped the service
        # stop waits for the core snapshot (platforms/posix.stop_grace): systemd does not kill earlier
        from platforms import posix
        stop = [line for line in unit.splitlines() if line.startswith("TimeoutStopSec=")]
        self.assertEqual(len(stop), 1)
        self.assertGreater(int(stop[0].split("=")[1]), posix.stop_grace(True))
        # 23.10: the OS kernel killed one process for lack of memory — systemd by default (stop) shut down the whole hub
        self.assertIn("\nOOMPolicy=continue\n", unit)
        with open(self.calls) as f:
            calls = f.read()
        self.assertIn("systemctl --user enable agents-hub.service", calls)
        self.assertIn("systemctl --user restart agents-hub.service", calls)
        self.assertIn("loginctl enable-linger", out)      # without a login the hub does not run — say how to enable it

    def test_hub_command_runs_with_the_installers_python(self):
        self.run_install()
        hub = os.path.join(self.home, ".local", "bin", "hub")
        r = subprocess.run([hub, "--help"], env={**self.env, "PATH": "/usr/bin:/bin"}, capture_output=True, text=True,
                           timeout=60)
        self.assertIn("session new", r.stdout + r.stderr)

    def test_rerun_keeps_the_owners_settings(self):
        self.run_install()
        path = os.path.join(self.home, ".config", "hub-core", "hub.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"title": "my hub"}, f)
        self.run_install()
        self.assertEqual(self.cfg(), {"title": "my hub"})

    def test_old_python_is_refused_with_a_hint(self):
        os.unlink(os.path.join(self.bin, "python3.14"))
        stub(os.path.join(self.bin, "python3"), 'case "$*" in *version_info*) exit 1;; esac\nexit 0\n')
        env = {**self.env, "PATH": self.bin + ":/nonexistent"}
        r = subprocess.run(["/bin/sh", INSTALL], env=env, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Python 3.12", r.stderr)

    def test_old_services_of_this_hub_are_removed(self):
        # the old layout — a service per part; a hand-made one may exist too (a stats-only service) — with hub-up
        # it would send a second time. Services of other programs and of another hub stay
        units = os.path.join(self.home, ".config", "systemd", "user")
        os.makedirs(units)
        mine = {"hubd.service": "bin/hubd", "hub-web.service": "modules/web/server.py", "hubstats.service": "bin/hub-stats"}
        others = {"backup.service": "/usr/bin/restic backup", "other-hub.service": "/usr/bin/python3 /srv/hub/bin/hubd"}
        for name, cmd in mine.items():
            with open(os.path.join(units, name), "w") as f:
                f.write(f"[Service]\nExecStart=/usr/bin/python3 {REPO}/{cmd}\n")
        for name, cmd in others.items():
            with open(os.path.join(units, name), "w") as f:
                f.write(f"[Service]\nExecStart={cmd}\n")
        self.run_install()
        left = set(os.listdir(units)) - {"default.target.wants"}
        self.assertEqual(left, {"agents-hub.service", *others})
        with open(self.calls) as f:
            calls = f.read()
        for name in mine:
            self.assertIn(f"systemctl --user disable --now {name}", calls)

    def test_someone_elses_hub_command_is_kept(self):
        # GitHub's `hub` in ~/.local/bin: the install removed it (rm -f) and put the hub's wrapper there, no copy
        # (tests research 03.11, S2)
        hub = os.path.join(self.home, ".local", "bin", "hub")
        os.makedirs(os.path.dirname(hub))
        stub(hub, "echo GITHUB-HUB-CLI\n")
        out = self.run_install()
        with open(hub) as f:
            self.assertEqual(f.read(), "#!/bin/sh\necho GITHUB-HUB-CLI\n")
        self.assertIn(hub, out)
        # the hub's sessions still reach the hub's own command: the service's PATH has the code's bin before ~/.local/bin
        with open(os.path.join(self.home, ".config", "systemd", "user", "agents-hub.service")) as f:
            path = next(x for x in f.read().splitlines() if x.startswith("Environment=PATH=")).split("=", 2)[2]
        self.assertEqual(shutil.which("hub", path=path), os.path.join(REPO, "bin", "hub"))
        with open(os.path.join(self.home, ".local", "bin", "hub-telegram")) as f:
            self.assertIn(f'"{REPO}/bin/hub-telegram"', f.read())

    def test_the_service_of_another_hub_is_not_taken_over(self):
        # agents-hub.service holds another hub (its own state folder): the install rewrote it for this one and restarted
        # it — the other hub was gone (tests research 03.11, S4). Now it stops before changing anything
        units = os.path.join(self.home, ".config", "systemd", "user")
        os.makedirs(units)
        theirs = f"[Service]\nExecStart=/usr/bin/python3 /srv/other/bin/hub-up\nEnvironment=HUB_STATE_DIR={self.home}/other\n"
        with open(os.path.join(units, "agents-hub.service"), "w") as f:
            f.write(theirs)
        r = subprocess.run(["sh", INSTALL], env=self.env, capture_output=True, text=True, timeout=120)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("agents-hub.service", r.stderr)
        with open(os.path.join(units, "agents-hub.service")) as f:
            self.assertEqual(f.read(), theirs)
        self.assertFalse(os.path.exists(self.calls))                                   # nothing restarted or disabled
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config", "hub-core", "state-dir")))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".local", "bin", "hub")))

    def test_a_second_hub_on_the_same_code_keeps_its_service(self):
        # a second hub's service runs hub-up of this very code with its own state: the clean-up of the earlier layout
        # matched it by ExecStart and disabled and removed it (tests research 03.11, S4)
        units = os.path.join(self.home, ".config", "systemd", "user")
        os.makedirs(units)
        second = {"agents-hub-second.service": "bin/hub-up", "hub-web-second.service": "modules/web/server.py"}
        for name, cmd in second.items():
            with open(os.path.join(units, name), "w") as f:
                f.write(f"[Service]\nExecStart=/usr/bin/python3 {REPO}/{cmd}\n"
                        f"Environment=HUB_STATE_DIR={self.home}/second\n")
        with open(os.path.join(units, "hubd.service"), "w") as f:   # this hub's own earlier layout still goes
            f.write(f"[Service]\nExecStart=/usr/bin/python3 {REPO}/bin/hubd\n")
        self.run_install()
        self.assertEqual(set(os.listdir(units)) - {"default.target.wants"}, {"agents-hub.service", *second})
        with open(self.calls) as f:
            calls = f.read()
        for name in second:
            self.assertNotIn(name, calls)
        self.assertIn("systemctl --user disable --now hubd.service", calls)

    def test_claude_outside_the_default_path_is_on_the_services_path(self):
        # npm -g under nvm: claude (and node next to it) is neither in ~/.local/bin nor in /usr/bin — a service with the old PATH did not
        # find it, no session came up
        os.unlink(os.path.join(self.bin, "claude"))
        npm = os.path.join(self.home, ".nvm", "versions", "node", "v22", "bin")
        os.makedirs(npm)
        stub(os.path.join(npm, "claude"), "exit 0\n")
        self.env["PATH"] = f"{self.bin}:{npm}:/usr/bin:/bin"
        self.run_install()
        with open(os.path.join(self.home, ".config", "systemd", "user", "agents-hub.service")) as f:
            path = next(x for x in f.read().splitlines() if x.startswith("Environment=PATH="))
        self.assertIn(npm, path.split("=", 2)[2].split(":"))

    def test_without_systemd_says_how_to_run(self):
        os.unlink(os.path.join(self.bin, "systemctl"))
        stub(os.path.join(self.bin, "systemctl"), "exit 1\n")   # a container, WSL without systemd
        out = self.run_install()
        self.assertIn("bin/hub-up", out)


if __name__ == "__main__":
    unittest.main()
