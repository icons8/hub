"""`hub off` / `hub on` / `hub ui` (aliases hub-off, hub-on, hub-ui in the shell profile): turn the hub
off and on under the OS service (a launchd agent on macOS, systemd --user on Linux) and open the hub page. The service
is its own: the one that holds the hub with this state directory (a second hub on the machine has its own); the page
port comes from its environment."""
import tests  # noqa: F401 — ignore this machine's settings
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest

from core import i18n
from platforms import posix

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB = os.path.join(REPO, "bin", "hub")


def plist(home, label, state, port=None, hub_up=True):
    d = os.path.join(home, "Library", "LaunchAgents")
    os.makedirs(d, exist_ok=True)
    env = {"HUB_STATE_DIR": state, **({"HUB_WEB_PORT": str(port)} if port else {})}
    path = os.path.join(d, label + ".plist")
    with open(path, "wb") as f:
        plistlib.dump({"Label": label, "ProgramArguments": ["/py", "/repo/bin/hub-up" if hub_up else "/x/other"],
                       "EnvironmentVariables": env}, f)
    return path


def unit(home, name, state, port=None):
    d = os.path.join(home, ".config", "systemd", "user")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, name), "w") as f:
        f.write(f"[Service]\nExecStart=/py /repo/bin/hub-up\nEnvironment=HUB_STATE_DIR={state}\n"
                + (f"Environment=HUB_WEB_PORT={port}\n" if port else ""))


class FindsItsOwnService(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="svc-home-")
        self.state = os.path.join(self.home, ".hub-core")
        os.makedirs(self.state)

    def test_launchd_agent_with_this_state_dir_and_its_port(self):
        plist(self.home, "com.hub-core", os.path.join(self.home, "other"))           # a second hub — not ours
        plist(self.home, "com.example.app", self.state, hub_up=False)                # not a hub
        path = plist(self.home, "com.hub-personal", self.state, port=8797)
        u = posix.service_unit(self.state, home=self.home, platform="darwin")
        self.assertEqual((u["kind"], u["id"], u["path"], u["port"]), ("launchd", "com.hub-personal", path, 8797))
        c = posix.service_commands(u, uid=501)
        self.assertEqual(c["off"], ["launchctl", "bootout", "gui/501/com.hub-personal"])
        self.assertEqual(c["on"], ["launchctl", "bootstrap", "gui/501", path])
        self.assertEqual(c["status"], ["launchctl", "print", "gui/501/com.hub-personal"])

    def test_a_hand_written_agent_that_is_not_well_formed_xml_is_skipped(self):
        # launchd loads a hand-written agent with a bare "&" in a shell command line, plistlib refuses it with an expat
        # error (not a ValueError): `hub doctor`, `hub on/off/ui` and `hub secret` fell with a traceback on such a machine
        d = os.path.join(self.home, "Library", "LaunchAgents")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "com.example.a-tool.plist"), "w") as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict><key>Label</key>'
                    '<string>com.example.a-tool</string><key>ProgramArguments</key><array><string>/bin/sh</string>'
                    '<string>-c</string><string>tool >> /tmp/tool.log 2>&1</string></array></dict></plist>\n')
        path = plist(self.home, "com.hub-personal", self.state, port=8797)
        u = posix.service_unit(self.state, home=self.home, platform="darwin")
        self.assertEqual((u["id"], u["path"], u["port"]), ("com.hub-personal", path, 8797))

    def test_the_agent_of_the_downloaded_app_is_the_hubs(self):
        # installer S5: launchd runs the app's shell with --serve, which becomes hub-up — `hub on/off/uninstall` find it
        import plistlib
        path = os.path.join(self.home, "Library", "LaunchAgents", "com.hub-core.plist")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            plistlib.dump({"Label": "com.hub-core", "ProgramArguments": [
                "/Applications/Agents Hub.app/Contents/MacOS/Agents Hub", "--serve"],
                "EnvironmentVariables": {"HUB_STATE_DIR": self.state}}, f)
        u = posix.service_unit(self.state, home=self.home, platform="darwin")
        self.assertEqual((u["kind"], u["id"], u["path"]), ("launchd", "com.hub-core", path))

    def test_systemd_user_unit_with_this_state_dir(self):
        unit(self.home, "agents-hub.service", self.state)
        u = posix.service_unit(self.state, home=self.home, platform="linux")
        self.assertEqual((u["kind"], u["id"], u["port"]), ("systemd", "agents-hub.service", None))
        c = posix.service_commands(u, uid=1000)
        self.assertEqual((c["on"], c["off"]), (["systemctl", "--user", "start", "agents-hub.service"],
                                               ["systemctl", "--user", "stop", "agents-hub.service"]))
        self.assertEqual(c["status"], ["systemctl", "--user", "is-active", "--quiet", "agents-hub.service"])

    def test_no_service(self):
        self.assertIsNone(posix.service_unit(self.state, home=self.home, platform="darwin"))
        self.assertIsNone(posix.service_unit(self.state, home=self.home, platform="linux"))


@unittest.skipIf(sys.platform not in ("darwin", "linux"), "launchd and systemd")
class Cli(unittest.TestCase):
    """Hub commands — through fake launchctl / systemctl first in PATH: they record how they were called."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="svc-cli-")
        self.state = os.path.join(self.home, ".hub-core")
        os.makedirs(self.state)
        self.bin, self.calls = os.path.join(self.home, "bin"), os.path.join(self.home, "calls")
        self.loaded = os.path.join(self.home, "loaded")
        os.makedirs(self.bin)
        tool = "launchctl" if sys.platform == "darwin" else "systemctl"
        with open(os.path.join(self.bin, tool), "w") as f:   # "the service is running" — the file `loaded`; on/off set it
            f.write(f'#!/bin/sh\necho "$*" >> {self.calls}\ncase "$*" in\n'
                    f'  *print*|*is-active*) [ -e {self.loaded} ];;\n'
                    f'  *bootstrap*|*" start "*) touch {self.loaded};;\n'
                    f'  *bootout*|*" stop "*) rm -f {self.loaded};;\nesac\n')
        os.chmod(os.path.join(self.bin, tool), 0o755)
        self.browser = os.path.join(self.bin, "open-page")
        with open(self.browser, "w") as f:
            f.write(f'#!/bin/sh\necho "opened $1" >> {self.calls}\n')
        os.chmod(self.browser, 0o755)
        if sys.platform == "darwin":
            plist(self.home, "com.hub-core", self.state, port=8799)
        else:
            unit(self.home, "agents-hub.service", self.state, port=8799)

    def hub(self, *args, session=None):
        env = {k: v for k, v in os.environ.items() if k not in ("HUB_SESSION", "HUB_EXTENSION", "HUB_WEB_PORT")}
        env.update(HOME=self.home, HUB_STATE_DIR=self.state, PATH=self.bin + os.pathsep + env.get("PATH", ""),
                   XDG_CONFIG_HOME=os.path.join(self.home, ".config"), BROWSER=self.browser)
        if session:
            env["HUB_SESSION"] = session
        return subprocess.run([sys.executable, HUB, *args], env=env, capture_output=True, text=True, encoding="utf-8",
                              timeout=60)

    def calls_made(self):
        try:
            with open(self.calls) as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def test_off_on_and_already(self):
        open(self.loaded, "w").close()
        out = self.hub("off")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(any("bootout" in c or " stop " in f" {c} " for c in self.calls_made()))
        self.assertFalse(os.path.exists(self.loaded))
        # what brings it back by itself: launchd at the next sign-in, systemd at the next boot when its manager lingers
        boot = sys.platform != "darwin" and posix.lingers()
        said, not_said = ("cli.service.stays_off_login", "cli.service.stays_off_boot")[::-1 if boot else 1]
        self.assertIn(i18n.tr("en", said), out.stdout)
        self.assertNotIn(i18n.tr("en", not_said), out.stdout)
        self.assertIn("already", self.hub("off").stdout)                          # already off — leave it
        out = self.hub("on")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(os.path.exists(self.loaded))
        self.assertIn("http://localhost:8799/", out.stdout)                   # the port comes from the service
        self.assertIn("already", self.hub("on").stdout)

    def test_ui_opens_the_page_on_the_services_port(self):
        out = self.hub("ui")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("opened http://localhost:8799/", self.calls_made())

    def test_on_without_a_service_says_how_to_start(self):
        for r, _, fs in os.walk(self.home):
            for f in fs:
                if f.endswith((".plist", ".service")):
                    os.remove(os.path.join(r, f))
        out = self.hub("on")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("hub-up", out.stderr)
        self.hub("ui")
        self.assertIn("opened http://localhost:8787/", self.calls_made())        # without a service — the default port

    def test_a_session_may_not_switch_the_hub_off(self):
        open(self.loaded, "w").close()
        self.assertNotEqual(self.hub("off", session="worker").returncode, 0)
        self.assertNotEqual(self.hub("on", session="worker").returncode, 0)
        self.assertNotEqual(self.hub("ui", session="worker").returncode, 0)   # and it does not open a tab for the owner
        self.assertEqual(self.calls_made(), [])


if __name__ == "__main__":
    unittest.main()
