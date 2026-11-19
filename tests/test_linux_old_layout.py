"""The old Linux layout (before 10-06: one systemd --user service per part — hubd, hub-web, hub-scheduler…, without hub-up)
stays as it was after a code update: the parts that exist only under hub-up (stats, conduit, update, watchdog…) are
missing, and nobody can run `hub restart` — the core keeps running on old code and the system agents do not appear (10-09:
this happened on Linux hubs — stats were never sent, conduit never joined the pool). A core started by such a service
moves the hub to a single service with hub-up by itself: with the deploy/install.sh installer in a separate unit (it
stops the old services, including the core itself).

The check uses unit files in a temporary HOME and a record of calls; the test does not touch a real systemd."""
import tests  # noqa: F401 — keep the machine's settings out of the test
import os
import shutil
import tempfile
import unittest
from unittest import mock

from core import oldlayout
from platforms import posix

REPO = "/home/x/agents-hub-core"
USER_GROUP = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/{}\n"


@unittest.skipIf(os.name == "nt", "systemd — POSIX")
class OldService(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="old-home-")
        self.addCleanup(shutil.rmtree, self.home, True)
        self.units = os.path.join(self.home, ".config", "systemd", "user")
        os.makedirs(self.units)
        self.cgroup = os.path.join(self.home, "cgroup")

    def unit(self, name, exec_start):
        with open(os.path.join(self.units, name), "w") as f:
            f.write(f"[Service]\nExecStart={exec_start}\nEnvironment=HUB_STATE_DIR=/home/x/.hub-core\n")

    def old(self, own="hubd.service", env=None, platform="linux", group=USER_GROUP):
        with open(self.cgroup, "w") as f:
            f.write(group.format(own))
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("INVOCATION_ID", "SYSTEMD_EXEC_PID", "HUB_UP_CHILD", "XDG_CONFIG_HOME")}
        env = {"INVOCATION_ID": "x", "SYSTEMD_EXEC_PID": str(os.getpid()), **(env or {})}
        with mock.patch.dict(os.environ, {**clean, "HOME": self.home, **env}, clear=True), \
                mock.patch.object(posix.sys, "platform", platform):
            return posix.old_service(REPO, cgroup=self.cgroup)

    def test_core_held_by_its_own_old_unit(self):
        self.unit("hubd.service", f"/usr/bin/python3 {REPO}/bin/hubd")
        self.assertEqual(self.old(), os.path.join(self.units, "hubd.service"))

    def test_the_hub_up_unit_is_not_old(self):
        self.unit("agents-hub.service", f"/usr/bin/python3 {REPO}/bin/hub-up")
        self.assertIsNone(self.old("agents-hub.service"))

    def test_a_child_of_hub_up_is_not_old(self):
        self.unit("hubd.service", f"/usr/bin/python3 {REPO}/bin/hubd")
        self.assertIsNone(self.old(env={"HUB_UP_CHILD": "1"}))

    def test_not_the_main_process_of_the_unit(self):
        # a descendant of the unit (a session, its command) is not this service's core
        self.unit("hubd.service", f"/usr/bin/python3 {REPO}/bin/hubd")
        self.assertIsNone(self.old(env={"SYSTEMD_EXEC_PID": "1"}))

    def test_another_hubs_core(self):
        self.unit("hubd.service", "/usr/bin/python3 /home/x/other-hub/bin/hubd")
        self.assertIsNone(self.old())

    def test_a_system_unit_is_left_alone(self):
        # a system (root) service is not our layout: the installer sets up only --user
        self.unit("hubd.service", f"/usr/bin/python3 {REPO}/bin/hubd")
        self.assertIsNone(self.old(group="0::/system.slice/{}\n"))

    def test_mac_is_never_old(self):
        self.unit("hubd.service", f"/usr/bin/python3 {REPO}/bin/hubd")
        self.assertIsNone(self.old(platform="darwin"))


class Migrate(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="old-state-")
        self.addCleanup(shutil.rmtree, self.state, True)
        self.calls, self.code, self.stderr = [], 0, ""

    def run_cmd(self, cmd, **kw):
        self.calls.append(cmd)
        return mock.Mock(returncode=self.code, stdout="", stderr=self.stderr)

    def migrate(self, unit="/home/x/.config/systemd/user/hubd.service", now=1000.0):
        with mock.patch.object(oldlayout.platforms.current, "old_service", return_value=unit), \
                mock.patch.dict(os.environ, {"PATH": "/home/x/.nvm/bin:/usr/bin:/bin"}), mock.patch("builtins.print"):
            return oldlayout.migrate(self.state, run=self.run_cmd, now=now)

    def test_core_runs_the_installer_in_its_own_unit(self):
        self.assertTrue(self.migrate())
        [cmd] = self.calls
        self.assertEqual(cmd[:2], ["systemd-run", "--user"])
        self.assertEqual(cmd[-2:], ["sh", os.path.join(oldlayout.REPO, "deploy", "install.sh")])
        # the same state folder, without waiting for the page; the core's PATH — the installer finds the same claude
        self.assertIn(f"--setenv=HUB_STATE_DIR={self.state}", cmd)
        self.assertIn("--setenv=HUB_INSTALL_NO_WAIT=1", cmd)
        self.assertIn("--setenv=PATH=/home/x/.nvm/bin:/usr/bin:/bin", cmd)

    def test_once_an_hour_at_most(self):
        # the migration failed (no systemd-run, the installer crashed) — the service starts the core again: do not repeat every time
        self.migrate(now=1000.0)
        self.assertFalse(self.migrate(now=1000.0 + 600))
        self.assertTrue(self.migrate(now=1000.0 + 3700))
        self.assertEqual(len(self.calls), 2)

    def test_nothing_under_hub_up(self):
        self.assertFalse(self.migrate(unit=None))
        self.assertEqual(self.calls, [])

    def on_restart(self, units):
        with mock.patch.object(oldlayout.platforms.current, "old_units", return_value=units):
            return oldlayout.on_restart(self.state, 1000.0, run=self.run_cmd)

    def test_restart_on_the_old_layout_waits_for_the_core_then_installs(self):
        # `hub restart` runs new code from disk while the core is still old: the installer does the restart once the
        # core drops the restart file (sessions are free)
        said = self.on_restart(["/home/x/.config/systemd/user/hubd.service"])
        self.assertIn("agents-hub.service", said)
        [cmd] = self.calls
        self.assertEqual(cmd[:2], ["systemd-run", "--user"])
        self.assertEqual(cmd[-3:], [os.path.join(self.state, "restart"), os.path.join(self.state, oldlayout.SINCE),
                                    os.path.join(oldlayout.REPO, "deploy", "install.sh")])
        self.assertEqual(os.stat(os.path.join(self.state, oldlayout.SINCE)).st_mtime, 1000.0)

    @unittest.skipUnless(shutil.which("sh"), "needs sh (the wait loop is for Linux; under a Windows service there is no sh on PATH)")
    def test_the_waiting_installer_takes_only_a_fresh_restart_file(self):
        # the waiting loop itself (sh): a restart file from an earlier request is not a signal, a new one from the core triggers the installer
        import subprocess
        import time
        flag, done = os.path.join(self.state, "restart"), os.path.join(self.state, "installed")
        open(flag, "w").close()
        os.utime(flag, (900.0, 900.0))
        self.on_restart(["/home/x/.config/systemd/user/hubd.service"])
        loop = self.calls[0][self.calls[0].index("-c") - 1:]
        fake = os.path.join(self.state, "install.sh")
        with open(fake, "w") as f:
            f.write(f'touch "{done}"\n')
        p = subprocess.Popen(loop[:-1] + [fake])
        self.addCleanup(p.kill)
        time.sleep(2.5)
        self.assertIsNone(p.poll())
        os.utime(flag, None)
        self.assertEqual(p.wait(timeout=10), 0)
        self.assertTrue(os.path.exists(done))

    def test_the_waiting_installer_is_not_doubled(self):
        self.code, self.stderr = 1, "Failed to start transient service unit: Unit agents-hub-migrate-wait.service already exists."
        self.assertIn("when the core is ready", self.on_restart(["/home/x/.config/systemd/user/hubd.service"]))

    def test_without_systemd_run_says_how_to_move(self):
        self.code, self.stderr = 1, "Failed to connect to bus"
        self.assertIn(os.path.join("deploy", "install.sh"), self.on_restart(["/home/x/.config/systemd/user/hubd.service"]))

    def test_restart_under_hub_up_is_untouched(self):
        self.assertIsNone(self.on_restart([]))
        self.assertEqual(self.calls, [])


@unittest.skipIf(os.name == "nt", "systemd — POSIX")
class OldUnits(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="old-units-")
        self.addCleanup(shutil.rmtree, self.home, True)
        self.units = os.path.join(self.home, ".config", "systemd", "user")
        os.makedirs(self.units)

    def unit(self, name, exec_start, state="/home/x/.hub-core"):
        with open(os.path.join(self.units, name), "w") as f:
            f.write(f"[Service]\nExecStart={exec_start}\n" + (f"Environment=HUB_STATE_DIR={state}\n" if state else ""))

    def found(self, state="/home/x/.hub-core"):
        with mock.patch.object(posix.sys, "platform", "linux"), mock.patch.dict(os.environ, {"HOME": self.home}):
            os.environ.pop("XDG_CONFIG_HOME", None)
            return [os.path.basename(p) for p in posix.old_units(REPO, state, home=self.home)]

    def test_the_core_unit_of_this_hub(self):
        self.unit("hubd.service", f"/usr/bin/python3 {REPO}/bin/hubd")
        self.unit("hub-web.service", f"/usr/bin/python3 {REPO}/modules/web/server.py")
        self.unit("agents-hub.service", f"/usr/bin/python3 {REPO}/bin/hub-up")
        self.assertEqual(self.found(), ["hubd.service"])

    def test_another_state_dir_is_another_hub(self):
        # tests and a second hub on the same machine: a core service with a different state folder is not about this hub
        self.unit("hubd.service", f"/usr/bin/python3 {REPO}/bin/hubd")
        self.assertEqual(self.found("/tmp/hub-core-tests-state-x"), [])

    def test_without_state_dir_in_the_unit_it_is_the_default(self):
        self.unit("hubd.service", f"/usr/bin/python3 {REPO}/bin/hubd", state=None)
        self.assertEqual(self.found(os.path.join(self.home, ".hub-core")), ["hubd.service"])


if __name__ == "__main__":
    unittest.main()
