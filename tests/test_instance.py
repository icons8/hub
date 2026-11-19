"""Several hubs on one machine (core/instance.py): the names of instances, their folders, the anchor
<state>/instance.json that the settings folder and the ports are read from, the machine's registry, handing out ports,
and adopting a second hub that stands from before the anchor."""
import tests  # noqa: F401 — keep this machine's settings out
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import platforms
from core import catalog, instance
from tests.fakes import cut

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def tmpdir(test, prefix="inst-"):
    d = tempfile.mkdtemp(prefix=prefix)
    test.addCleanup(platforms.current.rmtree, d)
    return d


def anchored(test, state, name="work2", **over):
    os.makedirs(state, exist_ok=True)
    data = {"name": name, "title": name, "config_dir": os.path.join(os.path.dirname(state), "cfg-" + name),
            "ports": {"web": 8801, "files": 8802, "browsers": {"agents": 9231}}, **over}
    instance.write(state, data)
    return data


class Names(unittest.TestCase):
    def test_instance_names(self):
        for good in ("work2", "personal", "cx", "a-b", "p1234567890abcde"):
            self.assertTrue(instance.valid_name(good), good)
        for bad in ("", "a", "Work", "2x", "-x", "x-", "a--b", "core", "main", "telegram", "open", "codex",
                    "p1234567890abcdef", "a_b", "a.b", None, 5):
            self.assertFalse(instance.valid_name(bad), bad)

    def test_commands_and_services(self):
        self.assertEqual(instance.command_name(None), "hub")
        self.assertEqual(instance.command_name("work2"), "hub-work2")
        self.assertEqual(instance.service_ids("work2"), {"launchd": "com.hub-work2",
                                                         "systemd": "agents-hub-work2.service",
                                                         "task": "Agents Hub (work2)"})


class Cookie(unittest.TestCase):
    """The cookie of the page's sign-in by a key (slice M6) is named by the hub, not by the port: cookies do not split
    by port, two hubs on 127.0.0.1 must not overwrite each other's."""
    def test_main_and_instances_have_their_own_cookie(self):
        self.assertEqual(instance.cookie_name(None), "hub_main")
        self.assertEqual(instance.cookie_name(""), "hub_main")
        self.assertEqual(instance.cookie_name("work2"), "hub_work2")
        self.assertEqual(instance.cookie_name("a-b"), "hub_a-b")
        self.assertNotEqual(instance.cookie_name("work2"), instance.cookie_name("cx"))

    def test_no_instance_takes_the_main_hubs_cookie(self):
        self.assertFalse(instance.valid_name("main"))
        with self.assertRaises(ValueError):
            instance.cookie_name("main")
        with self.assertRaises(ValueError):
            instance.cookie_name("Bad name")

    def test_an_adopted_instance_under_a_reserved_word_has_a_cookie(self):
        self.assertEqual(instance.cookie_name("codex"), "hub_codex")   # a stand ~/.hub-codex stands from before


class Folders(unittest.TestCase):
    def test_folders_sit_next_to_the_main_hubs(self):
        main_state, main_cfg = os.path.join("/home/x", ".hub-core"), os.path.join("/home/x", ".config", "hub-core")
        n = os.path.normpath
        self.assertEqual(instance.folders("work2", main_state, main_cfg),
                         {"state": n(os.path.join("/home/x", ".hub-work2")),
                          "config": n(os.path.join("/home/x", ".config", "hub-work2"))})
        # the Windows spelling (no dot): %LOCALAPPDATA%\hub-core -> hub-<name>
        self.assertEqual(instance.folders("cx", os.path.join("/l", "hub-core"), os.path.join("/r", "hub-core")),
                         {"state": n(os.path.join("/l", "hub-cx")), "config": n(os.path.join("/r", "hub-cx"))})

    def test_the_name_a_folder_spells(self):
        main = os.path.join("/home/x", ".hub-core")
        self.assertEqual(instance.name_of_folder(os.path.join("/home/x", ".hub-personal"), main), "personal")
        self.assertEqual(instance.name_of_folder(os.path.join("/home/x", ".hub-pilot-claude"), main), "pilot-claude")
        self.assertEqual(instance.name_of_folder(os.path.join("/home/x", ".hub-codex"), main), "codex")
        for other in (".hub-core", "hub-core-tests-state-abc", ".hub-", ".hub-Bad", "stand", ".hub-main"):
            self.assertIsNone(instance.name_of_folder(os.path.join("/home/x", other), main), other)
        # only next to the default folder: a copy of a hub in /tmp/trial/.hub-trial is not an instance of this machine
        self.assertIsNone(instance.name_of_folder(os.path.join("/tmp", "trial", ".hub-trial"), main))
        # Windows spelling: no dot
        self.assertEqual(instance.name_of_folder(os.path.join("/l", "hub-cx"), os.path.join("/l", "hub-core")), "cx")

    def test_a_folder_that_is_a_link_to_another_disk_is_still_next_to_the_default(self):
        from tests import winsafe
        root = tmpdir(self)
        home, disk = os.path.join(root, "home"), os.path.join(root, "disk")
        os.makedirs(os.path.join(disk, "hub-personal"))
        os.makedirs(os.path.join(disk, "hub-core"))
        os.makedirs(home)
        winsafe.symlink(self, os.path.join(disk, "hub-personal"), os.path.join(home, ".hub-personal"),
                        target_is_directory=True)
        winsafe.symlink(self, os.path.join(disk, "hub-core"), os.path.join(home, ".hub-core"), target_is_directory=True)
        self.assertEqual(instance.name_of_folder(os.path.join(home, ".hub-personal"), os.path.join(home, ".hub-core")),
                         "personal")

    def test_a_default_folder_of_another_name_gives_no_instance_folder(self):
        with self.assertRaises(ValueError):   # never the main hub's own folder back
            instance.folders("work2", os.path.join("/data", "state"), os.path.join("/data", "cfg"))
        self.assertIsNone(instance.name_of_folder(os.path.join("/data", "state2"), os.path.join("/data", "state")))

    def test_the_registry_is_next_to_the_settings_folder(self):
        with mock.patch.object(platforms.current, "config_dir", lambda: os.path.join("/home/x", ".config", "hub-core")):
            self.assertEqual(instance.registry_dir(),
                             os.path.normpath(os.path.join("/home/x", ".config", "agents-hub", "hubs")))


class Anchor(unittest.TestCase):
    def setUp(self):
        self.root = tmpdir(self)
        self.state = os.path.join(self.root, ".hub-work2")
        env = mock.patch.dict(os.environ, {"HUB_STATE_DIR": self.state})
        env.start()
        self.addCleanup(env.stop)
        for k in ("HUB_CONFIG", "HUB_CONFIG_DIR", "HUB_WEB_PORT", "HUB_FILES_PORT"):
            os.environ.pop(k, None)

    def test_the_main_hub_has_no_anchor_and_keeps_its_folders_and_ports(self):
        os.makedirs(self.state)
        self.assertIsNone(instance.load(self.state))
        self.assertIsNone(instance.name())
        self.assertEqual(catalog.config_dir(), platforms.current.config_dir())
        self.assertEqual((instance.port("web"), instance.port("files")), (8787, 8788))
        env = {}
        self.assertIsNone(instance.anchor(env, self.state))
        self.assertEqual(env, {"HUB_STATE_DIR": self.state})   # nothing but the folder it already is
        self.assertEqual(os.listdir(self.state), [])   # no new file at a hub with one instance

    def test_a_process_that_lost_the_settings_folder_reads_it_from_the_anchor(self):
        data = anchored(self, self.state)
        self.assertEqual(catalog.config_dir(), data["config_dir"])
        self.assertEqual(catalog.config_path(self.state), os.path.join(data["config_dir"], "hub.json"))
        os.environ["HUB_CONFIG_DIR"] = "/elsewhere"   # the variable, when it came, still wins
        self.assertEqual(catalog.config_dir(), "/elsewhere")

    def test_without_its_state_folder_a_process_is_the_main_hubs(self):
        anchored(self, self.state)
        os.environ.pop("HUB_STATE_DIR")
        self.assertEqual(catalog.config_dir(), platforms.current.config_dir())
        self.assertEqual(instance.port("web"), 8787)

    def test_ports_come_from_the_environment_then_the_anchor(self):
        anchored(self, self.state)
        self.assertEqual((instance.port("web"), instance.port("files")), (8801, 8802))
        os.environ["HUB_WEB_PORT"] = "8811"
        self.assertEqual(instance.port("web"), 8811)
        os.environ["HUB_WEB_PORT"] = "x"   # not a port: the anchor's
        self.assertEqual(instance.port("web"), 8801)

    def test_the_parts_read_the_anchors_ports(self):
        anchored(self, self.state)
        from modules.files import server as files
        from modules.web import server as web
        from modules.cloudflare import remote
        self.assertEqual(files.web_port(), 8801)
        self.assertEqual(web.files_port(self.state), 8802)
        self.assertEqual(remote.page_port(self.state), 8801)
        os.environ["HUB_WEB_PORT"], os.environ["HUB_FILES_PORT"] = "8901", "8902"
        self.assertEqual((files.web_port(), web.files_port(self.state), remote.page_port(self.state)),
                         (8901, 8902, 8901))

    def test_the_anchor_is_for_its_user_only_and_a_broken_one_is_none(self):
        anchored(self, self.state)
        if os.name != "nt":   # portable: the ACL of Windows is not a mode
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.state, instance.FILE)).st_mode), 0o600)
        with open(os.path.join(self.state, instance.FILE), "w") as f:
            f.write("{not json")
        self.assertIsNone(instance.load(self.state))
        self.assertEqual(catalog.config_dir(), platforms.current.config_dir())
        with open(os.path.join(self.state, instance.FILE), "w") as f:
            json.dump({"name": "Bad name"}, f)
        self.assertIsNone(instance.load(self.state))

    def test_anchor_gives_the_children_what_the_environment_lacks(self):
        data = anchored(self, self.state)
        env = {"HUB_WEB_PORT": "8911"}
        self.assertEqual(instance.anchor(env, self.state), data)
        self.assertEqual(env, {"HUB_STATE_DIR": self.state, "HUB_CONFIG_DIR": data["config_dir"],
                               "HUB_WEB_PORT": "8911", "HUB_FILES_PORT": "8802"})

    def test_a_rewritten_anchor_is_read_anew(self):
        anchored(self, self.state)
        self.assertEqual(instance.port("web"), 8801)
        anchored(self, self.state, ports={"web": 8803, "files": 8804})
        self.assertEqual(instance.port("web"), 8803)


class Registry(unittest.TestCase):
    def setUp(self):
        self.root = tmpdir(self)
        self.reg = os.path.join(self.root, "hubs")

    def test_register_list_unregister(self):
        a = os.path.join(self.root, ".hub-a1")
        os.makedirs(a)
        instance.register({"name": "a1", "state": a, "ports": {"web": 8801, "files": 8802}}, self.reg)
        instance.register({"name": "b2", "state": os.path.join(self.root, ".hub-b2")}, self.reg)
        got = instance.entries(self.reg)
        self.assertEqual([(e["name"], e["gone"]) for e in got], [("a1", False), ("b2", True)])
        if os.name != "nt":   # portable: the ACL of Windows is not a mode
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.reg, "a1.json")).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(self.reg).st_mode), 0o700)
        self.assertTrue(instance.unregister("b2", self.reg))
        self.assertFalse(instance.unregister("b2", self.reg))
        self.assertFalse(instance.unregister("../x", self.reg))
        self.assertEqual([e["name"] for e in instance.entries(self.reg)], ["a1"])

    def test_garbage_in_the_registry_is_skipped(self):
        os.makedirs(self.reg)
        for f, text in (("x1.json", "{bad"), ("y2.json", '{"name": "other", "state": "/s"}'), ("z3.json", "[]"),
                        ("note.txt", "hi"), ("w4.json", '{"name": "w4"}')):
            with open(os.path.join(self.reg, f), "w") as fh:
                fh.write(text)
        self.assertEqual(instance.entries(self.reg), [])
        self.assertEqual(instance.entries(os.path.join(self.root, "none")), [])


class Ports(unittest.TestCase):
    def test_the_first_free_pair_past_all_hubs(self):
        entries = [{"name": "a1", "ports": {"web": 8801, "files": 8802, "browsers": {"agents": 9231}}},
                   {"name": "b2", "ports": "garbage"}]
        taken = instance.taken(entries + [{"name": "c3", "ports": {"web": "8803", "files": "8804"}}],
                               main_browsers=[9230, "x"])
        self.assertEqual(taken, {8787, 8788, 8801, 8802, 8803, 8804, 9231, 9230})   # numbers, also from strings
        busy = {8806, 9232}   # held by a program the registry does not see (another user's hub)
        got = instance.allocate(taken, browsers=["agents", "scraper"], is_free=lambda p: p not in busy)
        self.assertEqual(got, {"web": 8807, "files": 8808, "browsers": {"agents": 9233, "scraper": 9234}})

    def test_no_free_pair_is_an_error(self):
        with self.assertRaises(ValueError):
            instance.allocate(set(), is_free=lambda p: False)

    def test_a_listening_port_is_not_free(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        self.addCleanup(s.close)
        self.assertFalse(instance.free(s.getsockname()[1]))


class Adoption(unittest.TestCase):
    """A second hub from before the anchor (Mac HUB_INSTANCE, a systemd-run stand) gets its anchor once, by facts."""
    def setUp(self):
        self.root = tmpdir(self)
        self.main_state = os.path.join(self.root, ".hub-core")
        self.main_cfg = os.path.join(self.root, "config", "hub-core")
        self.state = os.path.join(self.root, ".hub-personal")
        self.reg = os.path.join(self.root, "hubs")
        os.makedirs(self.state)
        self.env = {"HUB_STATE_DIR": self.state, "HUB_CONFIG_DIR": os.path.join(self.root, "config", "hub-personal"),
                    "HUB_WEB_PORT": "8797", "HUB_FILES_PORT": "8798"}

    def adopt(self, **kw):
        return instance.adopt(self.state, self.env, self.main_state, self.main_cfg, default_state=self.main_state,
                              registry=self.reg, now=1000, **kw)

    def test_a_second_hub_with_its_own_settings_is_adopted_once_nothing_moving(self):
        got = self.adopt(service={"kind": "launchd", "id": "com.hub-personal", "path": "/p"}, repo="/code/b",
                         main_repo="/code/a")
        self.assertEqual(got, {"name": "personal", "title": "personal", "config_dir": self.env["HUB_CONFIG_DIR"],
                               "ports": {"web": 8797, "files": 8798},
                               "service": {"kind": "launchd", "id": "com.hub-personal"}, "command": "hub-personal",
                               "code": {"kind": "git", "path": "/code/b"}, "tmux": {"socket": "hub", "prefix": "hub-"},
                               "created": 1000, "by": "adopted"})
        self.assertEqual(instance.load(self.state), got)
        self.assertEqual(instance.entries(self.reg), [{**got, "state": self.state, "gone": False}])
        self.assertIsNone(self.adopt())   # once
        self.assertEqual(sorted(os.listdir(self.state)), [instance.FILE])   # nothing else written there

    def test_the_main_hubs_code_is_shared_a_stand_inside_its_folder_is_its_own(self):
        self.assertEqual(self.adopt(repo="/code/a", main_repo="/code/a")["code"]["kind"], "shared")
        os.remove(os.path.join(self.state, instance.FILE))
        got = self.adopt(repo=os.path.join(self.state, "code"), service={"kind": "transient", "id": "x.service"})
        self.assertEqual((got["code"]["kind"], got["service"]), ("git", {"kind": "transient", "id": "x.service"}))

    def test_what_is_not_adopted(self):
        cases = {
            "the main hub": dict(state=self.main_state),
            "a second hub on the main hub's settings": dict(env={"HUB_CONFIG_DIR": ""}),
            "the main hub's settings named": dict(env={"HUB_CONFIG_DIR": self.main_cfg}),
            "a folder that spells no name": dict(state=os.path.join(self.root, "stand")),
        }
        for why, case in cases.items():
            state = case.get("state", self.state)
            os.makedirs(state, exist_ok=True)
            env = {**self.env, **case.get("env", {})}
            self.assertIsNone(instance.adopt(state, env, self.main_state, self.main_cfg, default_state=self.main_state,
                                             registry=self.reg), why)
            self.assertFalse(os.path.exists(os.path.join(state, instance.FILE)), why)
        self.assertEqual(instance.entries(self.reg), [])

    def test_an_anchor_that_failed_to_write_is_written_at_the_next_start(self):
        real = instance.write
        with mock.patch.object(instance, "write", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.adopt()
        self.assertEqual([e["name"] for e in instance.entries(self.reg)], ["personal"])   # the entry went first
        with mock.patch.object(instance, "write", real):
            self.assertEqual(self.adopt()["name"], "personal")   # its own entry does not hold the name against it

    def test_a_service_without_a_file(self):
        self.assertEqual(instance.service_of("systemctl --user stop run-u42.service"),
                         {"kind": "transient", "id": "run-u42.service"})
        self.assertEqual(instance.service_of("launchctl bootout gui/501/com.hub-personal"),
                         {"kind": "launchd", "id": "com.hub-personal"})
        # the Mac agent's own HUB_SERVICE, launchd does not expand it
        self.assertEqual(instance.service_of("launchctl bootout gui/$(id -u)/com.hub-personal"),
                         {"kind": "launchd", "id": "com.hub-personal"})
        self.assertEqual(instance.service_of("sudo systemctl stop x.service"),
                         {"kind": "other", "stop": "sudo systemctl stop x.service"})
        self.assertIsNone(instance.service_of(None))

    def test_a_name_another_hub_holds_in_the_registry_is_not_taken(self):
        instance.register({"name": "personal", "state": os.path.join(self.root, "elsewhere")}, self.reg)
        self.assertIsNone(self.adopt())
        self.assertIsNone(instance.load(self.state))

    def test_the_code_a_service_runs(self):
        root = tmpdir(self)
        import plistlib
        unit = os.path.join(root, "agents-hub.service")
        with open(unit, "w") as f:
            f.write("[Service]\nExecStart=/usr/bin/python3 /home/x/agents-hub-core/bin/hub-up\n")
        spaced = os.path.join(root, "agents-hub-b.service")
        with open(spaced, "w") as f:
            f.write('[Service]\nExecStart="/usr/bin/python3" "/home/x/App Support/hub/bin/hub-up"\n')
        plist = os.path.join(root, "com.hub-core.plist")
        with open(plist, "wb") as f:
            plistlib.dump({"Label": "com.hub-core",
                           "ProgramArguments": ["/usr/bin/python3", "/home/y/Application Support/hub/bin/hub-up"]}, f)
        self.assertEqual(instance.code_of(unit), os.path.normpath("/home/x/agents-hub-core"))
        self.assertEqual(instance.code_of(spaced), os.path.normpath("/home/x/App Support/hub"))
        self.assertEqual(instance.code_of(plist), os.path.normpath("/home/y/Application Support/hub"))
        odd = os.path.join(root, "agents-hub-c.service")
        with open(odd, "w") as f:
            f.write('[Service]\nExecStart=/bin/sh -c "echo \nExecStart=/usr/bin/python3 %h/hub/bin/hub-up\n')
        self.assertEqual(instance.code_of(odd), os.path.normpath(os.path.expanduser("~/hub")))
        self.assertIsNone(instance.code_of(os.path.join(root, "none")))


CHILD = """import json, os, sys, time
keys = ("HUB_STATE_DIR", "HUB_CONFIG_DIR", "HUB_WEB_PORT", "HUB_FILES_PORT")
with open(os.environ["PROBE_OUT"] + ".tmp", "w") as f:
    json.dump({k: os.environ.get(k) for k in keys}, f)
os.replace(os.environ["PROBE_OUT"] + ".tmp", os.environ["PROBE_OUT"])
time.sleep(300)
"""


class HubUp(unittest.TestCase):
    """hub-up hands its children HUB_STATE_DIR and an instance's settings folder and ports from its anchor, and adopts a
    second hub that stands from before the anchor."""
    def setUp(self):
        self.root = tmpdir(self)
        self.home = os.path.join(self.root, "home")
        os.makedirs(self.home)
        self.part = os.path.join(self.root, "part.py")
        with open(self.part, "w") as f:
            f.write(CHILD)
        self.out = os.path.join(self.root, "out.json")

    def run_hub_up(self, state, **env):
        base = {k: v for k, v in os.environ.items() if k not in ("HUB_SERVICE", "INVOCATION_ID", "XPC_SERVICE_NAME",
                                                                 "HUB_CONFIG_DIR", "HUB_WEB_PORT", "HUB_FILES_PORT",
                                                                 "XDG_CONFIG_HOME", "SYSTEMD_EXEC_PID")}
        base.update(**self.dirs(), HUB_STATE_DIR=state,
                    HUB_UP_PARTS=json.dumps({"hubd": [self.part]}), PROBE_OUT=self.out, **env)
        p = subprocess.Popen([sys.executable, os.path.join(ROOT, "bin", "hub-up")], env=base,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop, p)
        end = time.time() + 60
        while time.time() < end and not os.path.exists(self.out) and p.poll() is None:
            time.sleep(0.1)
        self.assertTrue(os.path.exists(self.out), "the part did not start")
        with open(self.out) as f:
            return json.load(f)

    def dirs(self):
        return {"HOME": self.home, "USERPROFILE": self.home, "APPDATA": os.path.join(self.home, "AppData", "Roaming"),
                "LOCALAPPDATA": os.path.join(self.home, "AppData", "Local")}

    def stop(self, p):
        if p.poll() is None:
            p.terminate()
            p.wait(60)

    def test_the_children_of_an_instance_get_its_settings_and_ports(self):
        state = os.path.join(self.root, ".hub-work2")
        data = anchored(self, state)
        got = self.run_hub_up(state)
        self.assertEqual(got, {"HUB_STATE_DIR": state, "HUB_CONFIG_DIR": data["config_dir"], "HUB_WEB_PORT": "8801",
                               "HUB_FILES_PORT": "8802"})

    def test_the_main_hubs_children_get_only_its_state_folder(self):
        state = os.path.join(self.root, "main-state")
        os.makedirs(state)
        got = self.run_hub_up(state)
        self.assertEqual(got, {"HUB_STATE_DIR": state, "HUB_CONFIG_DIR": None, "HUB_WEB_PORT": None,
                               "HUB_FILES_PORT": None})
        self.assertFalse(os.path.exists(os.path.join(state, instance.FILE)))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config", "agents-hub")))

    def test_a_second_hub_from_before_is_adopted_at_start(self):
        with mock.patch.dict(os.environ, self.dirs()):   # next to the child's default ~/.hub-core (%LOCALAPPDATA%\hub-core)
            state = instance.folders("personal")["state"]
        os.makedirs(state)
        cfg = os.path.join(self.root, "cfg-personal")
        got = self.run_hub_up(state, HUB_CONFIG_DIR=cfg, HUB_WEB_PORT="8797", HUB_FILES_PORT="8798")
        self.assertEqual(got["HUB_CONFIG_DIR"], cfg)
        data = instance.load(state)
        self.assertEqual((data["name"], data["config_dir"], data["ports"], data["by"]),
                         ("personal", cfg, {"web": 8797, "files": 8798}, "adopted"))
        reg = [f for _, _, files in os.walk(self.home) for f in files if f == "personal.json"]
        self.assertEqual(reg, ["personal.json"])   # the registry entry, in this user's folder


class TmuxSocket(unittest.TestCase):
    """The tmux host of an instance has its own tmux server: on one server two hubs saw each other's sessions."""
    def test_socket(self):
        from core import daemon
        root = tmpdir(self)
        main = os.path.join(root, "main")
        os.makedirs(main)
        with mock.patch.dict(os.environ, {"HUB_HOST": "tmux"}):
            self.assertEqual((daemon.make_host(main).socket, daemon.make_host(main).prefix), ("hub", "hub-"))
            state = os.path.join(root, ".hub-work2")
            anchored(self, state)
            host = daemon.make_host(state)
            self.assertEqual((host.socket, host.prefix), ("hub-work2", "work2-"))
            # an adopted hub keeps the server it shared: its live sessions are there
            anchored(self, state, tmux={"socket": "hub", "prefix": "hub-"})
            self.assertEqual((daemon.make_host(state).socket, daemon.make_host(state).prefix), ("hub", "hub-"))


@cut.needs("conduit")
class ConduitExchange(unittest.TestCase):
    """Two hubs of a machine on one ~/.claude: the gateway kills a refresh token once exchanged, so the exchange is one
    at a time under a lock next to the sign-in's storage — the second reads the pair the first wrote."""
    def setUp(self):
        self.root = tmpdir(self)
        self.claude = os.path.join(self.root, "claude")
        env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.claude})
        env.start()
        self.addCleanup(env.stop)
        self.hub_settings(os.path.join(self.root, "cfg-a"))

    def hub_settings(self, folder):
        os.makedirs(folder, exist_ok=True)
        cfg = mock.patch.object(catalog, "config_dir", lambda: folder)
        cfg.start()
        self.addCleanup(cfg.stop)

    def test_the_second_exchange_waits_and_takes_the_fresh_pair(self):
        from modules.conduit import conduit
        from tests.test_conduit import Http, Store, creds
        store = Store(creds(expires_in=-5))
        reads, real_read = [], store.read
        store.read = lambda: (reads.append(1), real_read())[1]
        entered, go = threading.Event(), threading.Event()

        class Slow(Http):
            def __call__(self, url, body=None, headers=None):
                got = super().__call__(url, body, headers)
                if url == "https://gate.example/token" and not entered.is_set():
                    entered.set()   # the first exchange spent ref-1 and waits before writing the pair back
                    go.wait(30)
                return got

        http, results = Slow(), {}

        def fetch(who):
            try:
                results[who] = conduit.fetch(store, http=http, now=1000.0)
            except RuntimeError as e:
                results[who] = e
        a = threading.Thread(target=fetch, args=("a",))
        a.start()
        self.assertTrue(entered.wait(30))
        b = threading.Thread(target=fetch, args=("b",))
        b.start()
        end = time.time() + 1.5
        while time.time() < end and len(reads) < 2:   # without the lock b reads the dead pair at once
            time.sleep(0.02)
        go.set()
        a.join(30)
        b.join(30)
        self.assertIsInstance(results["a"], str)
        self.assertEqual(results["b"], results["a"])
        self.assertEqual(len([u for u, _, _ in http.calls if u == "https://gate.example/token"]), 1)

    def test_a_pair_one_hub_could_not_write_back_is_taken_by_another(self):
        from modules.conduit import conduit
        from tests.test_conduit import Http, Store, creds
        store, http = Store(creds(expires_in=-5)), Http()
        real = store.write
        store.write = mock.Mock(side_effect=OSError("held"))
        with self.assertRaises(OSError):   # hub A: the gateway gave a new pair, the storage did not take it
            conduit.fetch(store, http=http, now=1000.0)
        store.write = real
        self.hub_settings(os.path.join(self.root, "cfg-b"))   # hub B, its own settings, the same ~/.claude
        self.assertIsInstance(conduit.fetch(store, http=http, now=1000.0), str)
        self.assertEqual(len([u for u, _, _ in http.calls if u == "https://gate.example/token"]), 1)   # no dead token spent
        self.assertEqual(store.blob["mcpOAuth"]["conduit|ab"]["refreshToken"], "ref-2")
        self.assertFalse(os.path.exists(conduit.pending_path()))

    def test_a_pair_kept_by_an_earlier_version_in_the_settings_is_still_taken(self):
        from modules.conduit import conduit
        from tests.test_conduit import Http, Store, creds
        store, http = Store(creds(expires_in=-5)), Http()
        http.dead.add("ref-1")   # the earlier version exchanged it; its write failed
        with open(os.path.join(self.root, "cfg-a", "conduit-pending.json"), "w") as f:
            json.dump({"key": "conduit|ab", "was": conduit._digest("ref-1"),
                       "pair": {"accessToken": "acc-2", "refreshToken": "ref-2", "expiresAt": 9_000_000}}, f)
        self.assertIsInstance(conduit.fetch(store, http=http, now=1000.0), str)
        self.assertEqual(store.blob["mcpOAuth"]["conduit|ab"]["refreshToken"], "ref-2")
        self.assertFalse(os.path.exists(os.path.join(self.root, "cfg-a", "conduit-pending.json")))

    def test_a_pair_for_another_sign_in_does_not_cost_the_right_one(self):
        from modules.conduit import conduit
        from tests.test_conduit import Http, Store, creds
        store, http = Store(creds(expires_in=-5)), Http()
        http.dead.add("ref-1")
        os.makedirs(self.claude, exist_ok=True)
        with open(conduit.pending_path(), "w") as f:   # of another sign-in
            json.dump({"key": "conduit|zz", "was": "x", "pair": {}}, f)
        with open(os.path.join(self.root, "cfg-a", "conduit-pending.json"), "w") as f:   # the one for this sign-in
            json.dump({"key": "conduit|ab", "was": conduit._digest("ref-1"),
                       "pair": {"accessToken": "acc-2", "refreshToken": "ref-2", "expiresAt": 9_000_000}}, f)
        self.assertIsInstance(conduit.fetch(store, http=http, now=1000.0), str)
        self.assertEqual(store.blob["mcpOAuth"]["conduit|ab"]["refreshToken"], "ref-2")

    def test_a_fresh_sign_in_takes_no_lock(self):
        from modules.conduit import conduit
        from tests.test_conduit import Http, Store, creds
        with mock.patch.object(conduit, "exchange_lock", side_effect=AssertionError("locked")):
            self.assertIsInstance(conduit.fetch(Store(creds()), http=Http(), now=1000.0), str)


if __name__ == "__main__":
    unittest.main()
