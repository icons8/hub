"""Several hubs on one machine, putting an instance on (core/instance.py setup and settings, the installers with
HUB_INSTANCE, deploy/commands.py --instance): its folders next to the OS default ones, its ports handed out once past
every hub's, its own service and command; the main hub — its folders, service, `hub` and the mark of its folder — is
not touched."""
import tests  # noqa: F401 — keep this machine's settings out
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

import platforms
from core import instance
from tests import winsafe

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(REPO, "deploy", "install.sh")


def tmpdir(test, prefix="inst-"):
    d = tempfile.mkdtemp(prefix=prefix)
    test.addCleanup(platforms.current.rmtree, d)
    return d


def stub(path, body):
    with open(path, "w") as f:
        f.write("#!/bin/sh\n" + body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)


class Home:
    """The OS default folders of a test home: ~/.hub-core, ~/.config/hub-core, the registry next to them."""
    def __init__(self, test):
        self.home = tmpdir(test, "inst-home-")
        self.state = os.path.join(self.home, ".hub-core")
        self.config = os.path.join(self.home, ".config", "hub-core")
        self.registry = os.path.join(self.home, ".config", "agents-hub", "hubs")

    def setup(self, name, repo=None, **kw):
        kw.setdefault("is_free", lambda p: True)
        return instance.setup(name, repo, default_state=self.state, default_config=self.config,
                              registry=self.registry, platform="linux", **kw)


class Setup(unittest.TestCase):
    def test_a_new_instance_gets_its_folders_ports_and_names_once(self):
        h = Home(self)
        got = h.setup("work2", os.path.join(h.home, ".hub-work2", "code"), origin="https://example.test/core.git")
        self.assertEqual(got["state"], os.path.join(h.home, ".hub-work2"))
        self.assertEqual(got["config_dir"], os.path.join(h.home, ".config", "hub-work2"))
        self.assertEqual((got["ports"]["web"], got["ports"]["files"]), (8801, 8802))
        self.assertEqual(got["service"], {"kind": "systemd", "id": "agents-hub-work2.service"})
        self.assertEqual(got["command"], "hub-work2")
        self.assertEqual(got["code"], {"kind": "git", "path": os.path.join(h.home, ".hub-work2", "code"),
                                       "origin": "https://example.test/core.git"})
        self.assertEqual(instance.load(got["state"])["ports"], got["ports"])
        self.assertEqual([e["name"] for e in instance.entries(h.registry)], ["work2"])
        if os.name != "nt":
            for path in (got["state"], got["config_dir"]):
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o700, path)
        # a run again (a reinstall, a broken-off `hub instance new`) keeps the ports: links and tailnet live on them
        self.assertEqual(h.setup("work2", is_free=lambda p: False)["ports"], got["ports"])
        # the next one goes past them
        self.assertEqual(h.setup("cx")["ports"]["web"], 8803)

    def test_the_main_hubs_browsers_are_mirrored_on_ports_past_theirs(self):
        h = Home(self)
        os.makedirs(h.config)
        with open(os.path.join(h.config, "hub.json"), "w") as f:
            json.dump({"browsers": {"vm": {"kind": "local", "port": 9231},
                                    "own": {"kind": "remote", "url": "http://127.0.0.1:9232"}}}, f)
        got = h.setup("work2", browsers=["mac"])
        self.assertEqual(got["ports"]["browsers"], {"mac": 9233, "vm": 9234})

    def test_a_code_of_another_hub_is_shared(self):
        h = Home(self)
        self.assertEqual(h.setup("work2", REPO)["code"], {"kind": "shared", "path": REPO})

    def test_a_name_that_is_taken_is_refused(self):
        h = Home(self)
        for bad in ("core", "telegram", "Work"):
            with self.assertRaises(ValueError):
                h.setup(bad)
        instance.register({"name": "work2", "state": os.path.join(h.home, "elsewhere")}, h.registry)
        with self.assertRaises(ValueError):
            h.setup("work2")
        os.makedirs(os.path.join(h.home, ".hub-old"))
        with open(os.path.join(h.home, ".hub-old", "journal.jsonl"), "w") as f:
            f.write('{"seq":1}\n')
        with self.assertRaises(ValueError):   # a hub there without an anchor — not taken over by a new one
            h.setup("old")
        with self.assertRaises(ValueError):
            h.setup("ok", engine="gemini")
        self.assertFalse(os.path.exists(os.path.join(h.home, ".hub-ok")))
        with self.assertRaises(ValueError):   # hub-telegram-x would be both its command and the telegram of "x"
            h.setup("telegram-x")

    def test_ports_of_a_hub_uninstalled_without_its_data_are_not_handed_out(self):
        h = Home(self)
        first = h.setup("work2")["ports"]
        instance.unregister("work2", h.registry)     # `hub instance uninstall work2` without --purge
        self.assertEqual(h.setup("cx")["ports"]["web"], first["web"] + 2)
        # its ports went to another hub meanwhile — put on again, it gets new ones
        other = {"name": "cx", "state": os.path.join(h.home, ".hub-cx"), "ports": first}
        instance.register(other, h.registry)
        kept = os.path.join(h.home, ".config", "hub-work2", "hub.json")   # its settings stayed with its data
        with open(kept, "w") as f:
            json.dump({"title": "mine", "tailnet": {"expose": {"web": first["web"], "files": first["files"]}}}, f)
        got = h.setup("work2")["ports"]
        self.assertNotEqual(got["web"], first["web"])
        self.assertEqual(instance.load(os.path.join(h.home, ".hub-work2"))["ports"], got)
        with open(kept) as f:
            cfg = json.load(f)
        self.assertEqual((cfg["title"], cfg["tailnet"]["expose"]), ("mine", {"web": got["web"],
                                                                             "files": got["files"]}))

    def test_an_adopted_hub_without_ports_gets_them(self):
        h = Home(self)
        state = os.path.join(h.home, ".hub-pilot")
        os.makedirs(state)
        instance.write(state, {"name": "pilot", "ports": {}, "config_dir": os.path.join(h.home, "cfg")})
        instance.register({"name": "pilot", "state": state, "ports": {}}, h.registry)
        self.assertGreaterEqual(h.setup("pilot")["ports"]["web"], 8801)


class Settings(unittest.TestCase):
    def data(self, **over):
        return {"name": "work2", "title": "Work 2", "ports": {"web": 8801, "files": 8802, "browsers": {"mac": 9233}},
                "code": {"kind": "git", "path": "/s/code", "origin": "https://example.test/core.git"},
                "engine": "claude", "claude": "shared", **over}

    def test_own_ports_browser_profile_and_the_main_hubs_channel(self):
        base = {"hub": {"enabled": True}, "browsers": {"mac": {"kind": "local", "port": 9223, "headless": False,
                                                                "risk": ["owner-data"], "profile": "/Users/x/agent"}}}
        main = {"update": {"channel": "stable", "follow": True}}
        cfg = instance.settings(base, self.data(), "/s", main)
        self.assertEqual(cfg["title"], "Work 2")
        self.assertEqual(cfg["tailnet"], {"expose": {"web": 8801, "files": 8802}})
        self.assertEqual(cfg["browsers"]["mac"]["port"], 9233)
        self.assertEqual(cfg["browsers"]["mac"]["profile"], os.path.join("/s", "browsers", "mac"))
        self.assertEqual(cfg["browsers"]["mac"]["risk"], [])          # a fresh profile holds no one's sign-ins
        self.assertFalse(cfg["browsers"]["mac"]["headless"])
        self.assertEqual(cfg["update"], {"channel": "stable"})
        self.assertNotIn("host", cfg)
        self.assertNotIn("claude_config_dir", cfg)

    def test_no_origin_no_update_shared_code_follows(self):
        cfg = instance.settings({}, self.data(code={"kind": "git", "path": "/s/code", "origin": ""}), "/s", {})
        self.assertEqual(cfg["update"], {"enabled": False})
        cfg = instance.settings({}, self.data(code={"kind": "shared", "path": REPO}), "/s", {})
        self.assertEqual(cfg["update"], {"enabled": False, "follow": True})

    def test_codex_engine_and_an_own_claude_sign_in(self):
        cfg = instance.settings({}, self.data(engine="codex", claude="own"), "/s", {})
        self.assertEqual(cfg["host"], "codex")
        self.assertEqual(cfg["claude_config_dir"], os.path.join("/s", "claude"))

    def test_a_browser_of_the_main_hub_comes_with_its_settings_not_its_profile(self):
        main = {"browsers": {"mac": {"kind": "local", "port": 9230, "start": "demand", "profile": "/home/x/p",
                                     "risk": ["owner-data"]}}}
        cfg = instance.settings({}, self.data(), "/s", main)
        self.assertEqual(cfg["browsers"]["mac"]["start"], "demand")
        self.assertEqual(cfg["browsers"]["mac"]["profile"], os.path.join("/s", "browsers", "mac"))


@unittest.skipIf(os.name == "nt", "an sh script")
class LinuxInstance(unittest.TestCase):
    """deploy/install.sh with HUB_INSTANCE: before it the second hub on Linux shared ~/.config/hub-core with the main
    one, took the name agents-hub.service (refused) and the page port 8787, and rewrote `hub`."""
    def setUp(self):
        self.home = tmpdir(self, "lin-inst-")
        self.bin = os.path.join(self.home, "stubs")
        os.makedirs(self.bin)
        self.calls = os.path.join(self.home, "calls.log")
        for name in ("systemctl", "loginctl"):
            stub(os.path.join(self.bin, name), f'echo "{name} $*" >> "{self.calls}"\n'
                 + ('[ "$2" = show-user ] && echo Linger=no\n' if name == "loginctl" else "") + "exit 0\n")
        stub(os.path.join(self.bin, "claude"), "exit 0\n")
        winsafe.symlink(self, sys.executable, os.path.join(self.bin, "python3.14"))   # portable: the installer's name
        self.env = {"HOME": self.home, "PATH": self.bin + ":/usr/bin:/bin", "HUB_INSTALL_NO_WAIT": "1",
                    "LANG": "C.UTF-8", "USER": "x"}

    def run_install(self, **env):
        r = subprocess.run(["sh", INSTALL], env={**self.env, **env}, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout + r.stderr

    def tree(self):
        out = {}
        for root, dirs, files in os.walk(self.home):
            dirs[:] = [d for d in dirs if d != "stubs"]
            for f in files:
                p = os.path.join(root, f)
                with open(p, "rb") as fh:
                    out[os.path.relpath(p, self.home)] = fh.read()
        return out

    def test_an_instance_next_to_the_main_hub_leaves_the_main_one_alone(self):
        self.run_install()
        main = {k: v for k, v in self.tree().items() if k != "calls.log"}
        out = self.run_install(HUB_INSTANCE="work2")
        after = self.tree()
        for path, body in main.items():   # not a byte of the main hub changed: its unit, hub.json, hub, the mark
            self.assertEqual(after.get(path), body, path)
        state = os.path.join(self.home, ".hub-work2")
        cfg = os.path.join(self.home, ".config", "hub-work2")
        anchor = instance.load(state)
        web, files = anchor["ports"]["web"], anchor["ports"]["files"]
        self.assertGreaterEqual(web, 8801)
        self.assertEqual(anchor["config_dir"], cfg)
        self.assertEqual(anchor["code"]["kind"], "shared")
        with open(os.path.join(self.home, ".config", "systemd", "user", "agents-hub-work2.service")) as f:
            unit = f.read()
        for line in (f"Environment=HUB_STATE_DIR={state}", f"Environment=HUB_CONFIG_DIR={cfg}",
                     f"Environment=HUB_WEB_PORT={web}", f"Environment=HUB_FILES_PORT={files}"):
            self.assertIn("\n" + line + "\n", unit)
        with open(os.path.join(cfg, "hub.json")) as f:
            hub_json = json.load(f)
        self.assertEqual(hub_json["hub"]["cwd"], os.path.join(state, "hub-home"))
        self.assertEqual(hub_json["tailnet"]["expose"], {"web": web, "files": files})
        self.assertEqual(hub_json["update"], {"enabled": False, "follow": True})
        with open(self.calls) as f:
            calls = f.read()
        self.assertIn("systemctl --user restart agents-hub-work2.service", calls)
        self.assertFalse(os.path.exists(os.path.join(cfg, "state-dir")))
        self.assertIn(f"http://127.0.0.1:{web}/", out)
        self.assertIn("hub-work2", out)
        self.assertEqual([e["name"] for e in instance.entries(
            os.path.join(self.home, ".config", "agents-hub", "hubs"))], ["work2"])
        # hub-work2 leads to this hub; a session of another hub is refused, one of its own is not
        cmd = os.path.join(self.home, ".local", "bin", "hub-work2")
        env = {**self.env, "PATH": "/usr/bin:/bin"}
        r = subprocess.run([cmd, "--help"], env=env, capture_output=True, text=True, timeout=60)
        self.assertIn("session new", r.stdout + r.stderr)
        r = subprocess.run([cmd, "status"], env={**env, "HUB_SESSION": "maps",
                                                 "HUB_STATE_DIR": os.path.join(self.home, ".hub-core")},
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2)
        self.assertIn("hub bus", r.stderr)
        with open(os.path.join(state, "bin", "hub")) as f:   # its sessions' own `hub`, first in their PATH
            self.assertIn(f'HUB_STATE_DIR="{state}"', f.read())
        with open(os.path.join(self.home, ".local", "bin", "hub-telegram-work2")) as f:
            self.assertIn(f'HUB_CONFIG_DIR="{cfg}"', f.read())

    def test_a_rerun_keeps_the_ports(self):
        self.run_install(HUB_INSTANCE="work2")
        first = instance.load(os.path.join(self.home, ".hub-work2"))["ports"]
        self.run_install(HUB_INSTANCE="work2")
        self.assertEqual(instance.load(os.path.join(self.home, ".hub-work2"))["ports"], first)

    def test_the_command_of_another_hub_is_not_overwritten(self):
        bin_ = os.path.join(self.home, ".local", "bin")
        os.makedirs(bin_)
        theirs = '#!/bin/sh\n# agents-hub instance\nHUB_STATE_DIR="/x/.hub-telegram-yy" HUB_CONFIG_DIR="/c" exec ' \
                 '"/py" "/r/bin/hub" "$@"\n'
        with open(os.path.join(bin_, "hub-telegram-yy"), "w") as f:
            f.write(theirs)
        subprocess.run(["sh", INSTALL], env={**self.env, "HUB_INSTANCE": "yy"}, capture_output=True, text=True,
                       timeout=120)
        with open(os.path.join(bin_, "hub-telegram-yy")) as f:
            self.assertEqual(f.read(), theirs)
        self.assertTrue(os.path.exists(os.path.join(bin_, "hub-yy")))

    def test_a_reserved_name_installs_nothing(self):
        r = subprocess.run(["sh", INSTALL], env={**self.env, "HUB_INSTANCE": "telegram"}, capture_output=True,
                           text=True, timeout=120)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".hub-telegram")))
        self.assertFalse(os.path.exists(self.calls))



class Machine:
    """A test home with the main hub standing (its journal) — HOME and the OS folders of Windows point into it."""
    def __init__(self, test):
        self.home = tmpdir(test, "inst-mach-")
        env = {"HOME": self.home, "USERPROFILE": self.home, "LOCALAPPDATA": os.path.join(self.home, "lad"),
               "APPDATA": os.path.join(self.home, "ad")}
        p = mock.patch.dict(os.environ, env)
        p.start()
        test.addCleanup(p.stop)
        for k in ("XDG_CONFIG_HOME", "HUB_STATE_DIR", "HUB_CONFIG_DIR", "HUB_WEB_PORT", "HUB_FILES_PORT",
                  "HUB_SESSION"):
            q = mock.patch.dict(os.environ)
            q.start()
            os.environ.pop(k, None)
            test.addCleanup(q.stop)
        self.main = platforms.current.state_dir()
        self.config = platforms.current.config_dir()
        os.makedirs(self.main)
        os.makedirs(self.config, exist_ok=True)
        with open(os.path.join(self.main, "journal.jsonl"), "w") as f:
            f.write('{"seq":1}\n')


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


class InstanceNew(unittest.TestCase):
    def setUp(self):
        self.m = Machine(self)
        self.repo = os.path.join(self.m.home, "core")
        os.makedirs(os.path.join(self.repo, "bin"))
        with open(os.path.join(self.repo, "bin", "hub"), "w") as f:
            f.write("print('hub')\n")
        git("init", "-q", "-b", "main", cwd=self.repo)
        git("-c", "user.name=x", "-c", "user.email=x@example.test", "add", "-A", cwd=self.repo)
        git("-c", "user.name=x", "-c", "user.email=x@example.test", "commit", "-qm", "one", cwd=self.repo)
        git("remote", "add", "origin", "https://example.test/core.git", cwd=self.repo)
        self.calls, self.log = [], ""

    def run_(self, argv, env=None, **kw):
        self.calls.append((argv, env))
        if env and env.get("HUB_INSTANCE") and self.log:   # the installer: its hub-up wrote why the page is not up
            state = instance.folders(env["HUB_INSTANCE"])["state"]
            os.makedirs(os.path.join(state, "logs"), exist_ok=True)
            with open(os.path.join(state, "logs", "hub-up.log"), "w") as f:
                f.write(self.log)
        return subprocess.CompletedProcess(argv, self.rc)

    def new(self, *argv, up=True, rc=0):
        from core import instancecli
        self.rc, out = rc, []
        with mock.patch.object(instancecli, "answers", lambda port, timeout=2: up):
            code = instancecli.new(list(argv), repo=self.repo, run=self.run_, wait=0, say=out.append,
                                   platform="linux", sleep=lambda s: None)
        return code, "\n".join(out)

    def test_a_clone_of_this_hubs_commit_and_the_installer_with_its_name(self):
        code, out = self.new("work2", "--title", "Work 2")
        self.assertEqual(code, 0, out)
        state = instance.folders("work2")["state"]
        clone = os.path.join(state, "code")
        self.assertEqual(git("rev-parse", "HEAD", cwd=clone), git("rev-parse", "HEAD", cwd=self.repo))
        self.assertEqual(git("config", "--get", "remote.origin.url", cwd=clone), "https://example.test/core.git")
        if os.name != "nt":   # a local clone on one disk links the objects: seconds and almost no space
            objs = [os.path.join(r, f) for r, _, fs in os.walk(os.path.join(clone, ".git", "objects")) for f in fs]
            self.assertTrue(objs and all(os.stat(o).st_nlink > 1 for o in objs))
        argv, env = self.calls[0]
        self.assertEqual(argv, ["sh", os.path.join(clone, "deploy", "install.sh"), "-y"])
        self.assertEqual(env["HUB_INSTANCE"], "work2")
        self.assertNotIn("HUB_STATE_DIR", env)
        anchor = instance.load(state)
        self.assertEqual((anchor["title"], anchor["code"]["kind"], anchor["code"]["origin"]),
                         ("Work 2", "git", "https://example.test/core.git"))
        self.assertIn(f"http://127.0.0.1:{anchor['ports']['web']}/", out)
        self.assertIn("hub-work2", out)

    def test_no_origin_is_said_before_and_after_the_install(self):
        # a clone of a hub without origin never updates: the command says so (in the plan too: --dry-run) and how to fix it
        git("remote", "remove", "origin", cwd=self.repo)
        code, out = self.new("work2", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("remote add origin", out)
        code, out = self.new("work2")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.count("remote add origin"), 2, out)   # the plan, and again under "the hub is up"
        self.assertIn(os.path.join(instance.folders("work2")["state"], "code"), out)
        self.assertEqual(instance.load(instance.folders("work2")["state"])["code"]["origin"], "")
        code, out = self.new("work3", "--origin", "https://example.test/core.git")   # an address given: nothing to say
        self.assertEqual(code, 0, out)
        self.assertNotIn("remote add origin", out)

    def test_an_origin_of_the_main_hub_is_passed_on_in_silence(self):
        code, out = self.new("work2")
        self.assertEqual(code, 0, out)
        self.assertNotIn("remote add origin", out)

    def test_a_page_that_does_not_answer_takes_the_service_off_and_keeps_the_folders(self):
        state = instance.folders("work2")["state"]
        self.log = "12.11 hub-up: web exited with code 1\n"
        code, out = self.new("work2", up=False)
        self.assertEqual(code, 1)
        self.assertIn("web exited with code 1", out)
        argv, env = self.calls[-1]
        # a clone of an earlier core has no such flag and would refuse the whole command
        self.assertEqual(argv[-2:], ["uninstall", "--yes"])
        self.assertEqual(argv[-3], os.path.join(state, "code", "bin", "hub"))
        # one that has it: the executors' working copies stay, a name that comes back may have sessions with work in them
        os.makedirs(os.path.join(state, "code", "core"), exist_ok=True)
        with open(os.path.join(state, "code", "core", "uninstall.py"), "w") as f:
            f.write('keep = "--keep-copies" in argv\n')
        self.assertEqual(self.new("work2", up=False)[0], 1)
        self.assertEqual(self.calls[-1][0][-3:], ["uninstall", "--yes", "--keep-copies"])
        self.assertEqual(env["HUB_STATE_DIR"], state)
        self.assertTrue(os.path.isdir(os.path.join(state, "code")))
        # again: the same folders and ports, no second clone
        ports = instance.load(state)["ports"]
        self.assertEqual(self.new("work2")[0], 0)
        self.assertEqual(instance.load(state)["ports"], ports)

    def test_refusals_before_anything_is_written(self):
        from core import instancecli
        for argv in (["core"], ["x"], ["work2", "--engine", "gemini"], ["work2", "--engine", "codex", "--claude",
                                                                      "own"], ["work2", "--port", "1"]):
            with self.assertRaises(instancecli.Refused, msg=argv):
                self.new(*argv)
        os.makedirs(os.path.join(self.m.home, ".local", "bin"))
        stub(os.path.join(self.m.home, ".local", "bin", "hub-maps"), "echo another program\n")
        with self.assertRaises(instancecli.Refused):
            self.new("maps")
        os.remove(os.path.join(self.m.main, "journal.jsonl"))
        with self.assertRaises(instancecli.Refused):   # no main hub: the first hub is always the main one
            self.new("work2")
        self.assertEqual(self.calls, [])
        self.assertEqual(instance.entries(), [])

    def test_a_failure_before_the_anchor_leaves_nothing_under_the_name(self):
        from core import instancecli
        with mock.patch.object(instance, "setup", side_effect=ValueError("no free pair of ports")):
            with self.assertRaises(instancecli.Refused):
                self.new("work2")
        self.assertFalse(os.path.exists(instance.folders("work2")["state"]))
        self.assertEqual(self.new("work2")[0], 0)

    def test_its_own_command_from_before_is_no_obstacle(self):
        state = instance.folders("pers")["state"]
        os.makedirs(state)
        instance.write(state, {"name": "pers", "ports": {"web": 8811, "files": 8812}})
        bin_ = os.path.join(self.m.home, ".local", "bin")
        os.makedirs(bin_)
        with open(os.path.join(bin_, "hub-pers"), "w") as f:   # the Mac installer's wrapper before the instances
            f.write(f'#!/bin/sh\nHUB_STATE_DIR="{state}" HUB_CONFIG_DIR="/c" exec "/py" "/r/bin/hub" "$@"\n')
        self.assertEqual(self.new("pers")[0], 0)
        with open(os.path.join(bin_, "hub-pers"), "w") as f:   # the same name, another hub's
            f.write('#!/bin/sh\nHUB_STATE_DIR="/elsewhere" HUB_CONFIG_DIR="/c" exec "/py" "/r/bin/hub" "$@"\n')
        from core import instancecli
        with self.assertRaises(instancecli.Refused):
            self.new("pers")

    def test_dry_run_writes_nothing(self):
        code, out = self.new("work2", "--dry-run")
        self.assertEqual((code, self.calls), (0, []))
        self.assertIn(instance.folders("work2")["state"], out)
        self.assertFalse(os.path.exists(instance.folders("work2")["state"]))

    def test_codex_and_an_own_claude_go_into_the_anchor(self):
        from core import instancecli
        with mock.patch.object(instancecli, "codex_for", lambda state, say=None, dry=False: ("shared", "/x/codex")):
            self.assertEqual(self.new("cx", "--engine", "codex")[0], 0)
        self.assertEqual(self.new("pers", "--claude", "own")[0], 0)
        cx = instance.load(instance.folders("cx")["state"])
        pers = instance.load(instance.folders("pers")["state"])
        self.assertEqual((cx["engine"], pers["claude"]), ("codex", "own"))

    def test_a_codex_hub_gets_its_codex_before_its_service(self):
        # decided 9.5: no codex on the machine — the install puts its own; none to be had — no service, the folders
        # (and a download broken off) stay, the same command goes on
        from core import instancecli
        said = []

        def none(state, say=None, dry=False):
            said.append(dry)
            if not dry:
                raise instancecli.Refused("no network")
            return "install", os.path.join(state, "bin", "codex")
        with mock.patch.object(instancecli, "codex_for", none):
            code, out = self.new("cx", "--engine", "codex")
        state = instance.folders("cx")["state"]
        self.assertEqual((code, self.calls, said), (1, [], [True, False]), out)
        self.assertIn(os.path.join(state, "bin", "codex"), out)   # the plan says where it goes
        self.assertIn("no network", out)
        self.assertTrue(instance.load(state))
        self.assertFalse(any(e["name"] == "cx" for e in instance.entries()))
        with mock.patch.object(instancecli, "codex_for", lambda state, say=None, dry=False: ("own", "/x/codex")):
            code, out = self.new("cx", "--engine", "codex")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(any(e["name"] == "cx" for e in instance.entries()))


@unittest.skipIf(os.name == "nt", "the pointer here is a link; the Windows junction is the winstand stand's")
class OnelineNew(unittest.TestCase):
    """A main hub of the one-line install (<state>/app → its environment, the code a layer): `hub instance new` gives
    the new hub its own copy of the main hub's layer and the layer's deploy/oneline/setup.py --as-instance on the same
    environment, the main hub's build key and channel."""

    def setUp(self):
        from core import appcode
        self.m = Machine(self)
        data = os.path.join(self.m.home, ".local", "share", "agents-hub", "shells")
        os.makedirs(data)
        self.env = os.path.join(os.path.realpath(data), "1-team")   # the main hub's pointer leads there by its real path
        os.makedirs(os.path.join(self.env, "runtime", "python", "bin"))
        for name, text in ((".agents-hub-shell.json", "{}"), ("PACKAGED", json.dumps(
                {"format": "app", "layout": "layer", "flavor": "team", "runtime": 1, "platform": "linux-x64"}))):
            with open(os.path.join(self.env, name), "w") as f:
                f.write(text)
        os.symlink(self.env, os.path.join(self.m.main, "app"))
        layers = os.path.join(self.m.main, "layers")
        self.layer = os.path.join(layers, "9.9.1-team")
        for rel, text in (("bin/hub", "print('hub')\n"), ("deploy/oneline/setup.py", "# the installer\n")):
            os.makedirs(os.path.dirname(os.path.join(self.layer, rel)), exist_ok=True)
            with open(os.path.join(self.layer, rel), "w") as f:
                f.write(text)
        appcode.make(self.layer, "9.9.1", "team", "c0ffee991", mtime=1700000000)
        with open(os.path.join(self.layer, "PACKAGED"), "w") as f:
            f.write(json.dumps({"format": "app", "layout": "layer", "version": "9.9.1"}))
        os.symlink(self.layer, os.path.join(self.m.main, "code"))
        with open(os.path.join(layers, "deliver.json"), "w") as f:
            json.dump({"url": "https://deliver.example.test", "key": "build-key-of-the-test-01"}, f)
        with open(os.path.join(self.m.config, "hub.json"), "w") as f:
            json.dump({"update": {"channel": "main"}}, f)
        self.calls = []

    def run_(self, argv, env=None, **kw):
        key = argv[argv.index("--key-file") + 1]
        with open(key) as f:
            self.calls.append((argv, env, f.read().strip(), os.stat(key).st_mode & 0o077))
        return subprocess.CompletedProcess(argv, 0)

    def test_its_own_copy_of_the_layer_and_the_installer_of_the_line_as_an_instance(self):
        from core import instancecli
        out = []
        with mock.patch.object(instancecli, "answers", lambda port, timeout=2: True):
            code = instancecli.new(["work2"], repo=os.path.join(self.m.main, "code"), run=self.run_, wait=0,
                                   say=out.append, platform="linux", sleep=lambda s: None)
        self.assertEqual(code, 0, out)
        state = instance.folders("work2")["state"]
        (argv, env, key, mode), = self.calls
        own = os.path.join(state, "layers", "9.9.1-team")
        self.assertTrue(os.path.isfile(os.path.join(own, "bin", "hub")))   # a copy, not the main hub's folder
        self.assertEqual(argv[:4], [os.path.join(self.env, "runtime", "python", "bin", "python3"), "-I", "-B",
                                    os.path.join(own, "deploy", "oneline", "setup.py")])
        for flag, value in (("--env", self.env), ("--server", "https://deliver.example.test"), ("--state", state),
                            ("--as-instance", "work2"), ("--channel", "nightly"), ("--platform", "linux-x64")):
            self.assertEqual(argv[argv.index(flag) + 1], value, flag)
        self.assertIn("--handed", argv)
        self.assertEqual((key, mode), ("build-key-of-the-test-01", 0))
        self.assertFalse(os.path.exists(argv[argv.index("--key-file") + 1]))   # gone with the run
        self.assertEqual(env["HUB_INSTANCE"], "work2")
        self.assertEqual(instance.load(state)["code"], {"kind": "layer", "path": os.path.join(state, "code")})

    def test_no_build_key_of_the_main_hub_is_refused_before_anything_is_written(self):
        from core import instancecli
        os.unlink(os.path.join(self.m.main, "layers", "deliver.json"))
        with self.assertRaises(instancecli.Refused):
            instancecli.new(["work2"], repo=os.path.join(self.m.main, "code"), run=self.run_, wait=0,
                            say=lambda t: None, platform="linux", sleep=lambda s: None)
        self.assertFalse(os.path.exists(instance.folders("work2")["state"]))
        self.assertEqual(instance.entries(), [])

    def test_the_open_build_has_no_key_and_its_line_gets_no_key_file(self):
        from core import instancecli
        with open(os.path.join(self.env, "PACKAGED"), "w") as f:
            json.dump({"format": "app", "layout": "layer", "flavor": "open", "runtime": 1, "platform": "linux-x64"}, f)
        with open(os.path.join(self.m.main, "layers", "deliver.json"), "w") as f:
            json.dump({"url": "https://pages.example.test/agents-hub"}, f)
        got = []
        run = lambda argv, env=None, **kw: got.append(argv) or subprocess.CompletedProcess(argv, 0)  # noqa: E731
        with mock.patch.object(instancecli, "answers", lambda port, timeout=2: True):
            code = instancecli.new(["work2"], repo=os.path.join(self.m.main, "code"), run=run, wait=0,
                                   say=lambda t: None, platform="linux", sleep=lambda s: None)
        self.assertEqual(code, 0)
        (argv,) = got
        self.assertNotIn("--key-file", argv)
        self.assertEqual(argv[argv.index("--flavor") + 1], "open")
        self.assertEqual(argv[argv.index("--server") + 1], "https://pages.example.test/agents-hub")
        self.assertEqual([n for n in os.listdir(instance.folders("work2")["state"]) if n.startswith(".install-key")], [])

    def test_the_open_build_without_an_address_is_still_refused(self):
        from core import instancecli
        with open(os.path.join(self.env, "PACKAGED"), "w") as f:
            json.dump({"format": "app", "layout": "layer", "flavor": "open", "runtime": 1, "platform": "linux-x64"}, f)
        os.unlink(os.path.join(self.m.main, "layers", "deliver.json"))
        with self.assertRaises(instancecli.Refused):
            instancecli.new(["work2"], repo=os.path.join(self.m.main, "code"), run=self.run_, wait=0,
                            say=lambda t: None, platform="linux", sleep=lambda s: None)

    def test_an_app_hub_not_of_the_one_line_install_is_still_refused(self):
        from core import instancecli
        os.unlink(os.path.join(self.m.main, "app"))
        with self.assertRaises(instancecli.Refused):
            instancecli.new(["work2"], repo=os.path.join(self.m.main, "code"), run=self.run_, wait=0,
                            say=lambda t: None, platform="linux", sleep=lambda s: None)
        self.assertEqual(self.calls, [])

class List(unittest.TestCase):
    def test_the_main_hub_and_the_instances(self):
        from core import instancecli
        m = Machine(self)
        state = instance.folders("work2")["state"]
        cfg = instance.folders("work2")["config"]
        os.makedirs(cfg)
        with open(os.path.join(cfg, "hub.json"), "w") as f:
            json.dump({"host": "codex"}, f)
        instance.setup("work2", None, is_free=lambda p: True)
        instance.register({"name": "gone2", "state": os.path.join(m.home, "nowhere"), "ports": {"web": 8899}})
        rows = {r["command"]: r for r in instancecli.rows()}
        self.assertEqual(set(rows), {"hub", "hub-work2", "hub-gone2"})
        self.assertEqual(rows["hub"]["url"], "http://127.0.0.1:8787/")
        self.assertEqual((rows["hub-work2"]["engine"], rows["hub-work2"]["running"]), ("codex", False))
        self.assertTrue(rows["hub-gone2"]["gone"])
        self.assertEqual(rows["hub-work2"]["state"], state)
        out = []
        self.assertEqual(instancecli.show([], say=out.append), 0)
        self.assertIn("hub instance uninstall gone2", "\n".join(out))
        self.assertEqual(instancecli.remove(["gone2"], say=out.append), 0)
        self.assertEqual([e["name"] for e in instance.entries()], ["work2"])
        # the instance's own uninstall gets the flags as typed: it removes the executors' copies unless told to keep them
        calls = []
        run = lambda argv, **kw: calls.append(argv) or types.SimpleNamespace(returncode=0)   # noqa: E731
        instancecli.remove(["work2", "--yes"], run=run)
        instancecli.remove(["work2", "--keep-copies", "--yes"], run=run)
        self.assertEqual([calls[0][-2:], calls[1][-3:]], [["uninstall", "--yes"], ["uninstall", "--yes", "--keep-copies"]])

    def test_the_main_hubs_code_kind_a_layer_with_its_git_stop_file_and_a_clone(self):
        from core import appcode, instancecli
        m = Machine(self)
        layer = os.path.join(m.main, "layers", "9.9.1-team")
        os.makedirs(os.path.join(layer, "bin"))
        with open(os.path.join(layer, "bin", "hub"), "w") as f:
            f.write("print('hub')\n")
        with open(os.path.join(layer, appcode.GIT_STOP[0]), "wb") as f:
            f.write(appcode.GIT_STOP[1])
        appcode.make(layer, "9.9.1", "team", "c0ffee991", mtime=1700000000)
        clone = os.path.join(m.home, "core")
        os.makedirs(os.path.join(clone, "bin"))
        git("init", "-q", clone)
        kinds = {}
        for code in (layer, clone):
            with mock.patch.object(instancecli, "main_code", lambda: code):
                kinds[code] = {r["command"]: r for r in instancecli.rows()}["hub"]["code"]["kind"]
        self.assertEqual(kinds, {layer: "layer", clone: "git"})


class Stub(unittest.TestCase):
    """The main hub is removed while an instance stays (decision 9.3): `hub` becomes a stub that names the hubs left;
    uninstalling the last one takes the stub away."""
    def test_main_goes_stub_stays_last_instance_takes_it(self):
        from core import uninstall
        m = Machine(self)
        bin_ = os.path.join(m.home, ".local", "bin")
        os.makedirs(bin_)
        sys.path.insert(0, os.path.join(REPO, "deploy"))
        self.addCleanup(sys.path.remove, os.path.join(REPO, "deploy"))
        import commands
        if os.name != "nt":
            for cmd in ("hub", "hub-telegram"):
                commands.put(bin_, cmd, commands.wrapper(REPO, sys.executable, cmd))
        got = instance.setup("work2", None, is_free=lambda p: True)
        steps = uninstall.plan(m.main, m.config, home=m.home)
        stubs = [s for s in steps if s["do"] == "stub"]
        self.assertEqual(len(stubs), 1)
        self.assertNotIn(stubs[0]["path"], [s.get("path") for s in steps if s["do"] in ("remove", "path")])
        self.assertEqual(uninstall.execute(steps, say=lambda x: None), 0)
        self.assertTrue(uninstall.is_stub(stubs[0]["path"]))
        if os.name != "nt":
            r = subprocess.run([stubs[0]["path"]], capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 1)
            self.assertIn("hub-work2", r.stderr)
            self.assertTrue(commands.ours(stubs[0]["path"], "hub"))   # the main hub's install puts its hub over it
            self.assertFalse(os.path.exists(os.path.join(bin_, "hub-telegram")))
        steps = uninstall.plan(got["state"], got["config_dir"], home=m.home)
        self.assertIn("unregister", [s["do"] for s in steps])
        self.assertIn("unstub", [s["do"] for s in steps])
        self.assertEqual(uninstall.execute(steps, say=lambda x: None), 0)
        self.assertFalse(os.path.exists(stubs[0]["path"]))
        self.assertEqual(instance.entries(), [])

    def test_a_hub_alone_leaves_no_stub(self):
        from core import uninstall
        m = Machine(self)
        self.assertEqual([s for s in uninstall.plan(m.main, m.config, home=m.home) if s["do"] in ("stub", "unstub")],
                         [])


class TelegramBot(unittest.TestCase):
    def test_a_token_of_another_hub_is_refused_before_the_bot_is_asked(self):
        m = Machine(self)
        with open(os.path.join(m.config, "telegram.json"), "w") as f:
            json.dump({"token": "123:main-bot"}, f)
        got = instance.setup("work2", None, is_free=lambda p: True)
        mine = os.path.join(got["config_dir"], "telegram.json")
        self.assertEqual(instance.bot_holder("123:main-bot", mine), "hub")
        self.assertIsNone(instance.bot_holder("456:new-bot", mine))
        self.assertIsNone(instance.bot_holder("123:main-bot", os.path.join(m.config, "telegram.json")))   # its own
        with open(mine, "w") as f:
            json.dump({"token": "456:new-bot"}, f)
        self.assertEqual(instance.bot_holder("456:new-bot", os.path.join(m.config, "telegram.json")), "hub-work2")
        import io
        from modules.telegram import bot
        with mock.patch.dict(os.environ, {"HUB_CONFIG_DIR": got["config_dir"]}), \
                mock.patch.object(bot, "Api", side_effect=AssertionError("asked Telegram")), \
                mock.patch.object(sys, "stdin", io.StringIO("123:main-bot\n")):
            with self.assertRaises(SystemExit) as e:
                bot.main(["token"])
        self.assertIn("hub", str(e.exception.code))
        with open(mine) as f:
            self.assertEqual(json.load(f)["token"], "456:new-bot")


class Browsers(unittest.TestCase):
    def test_a_chrome_of_another_profile_on_the_port_is_not_taken(self):
        from modules.browser import run
        procs = [(10, "/usr/bin/chrome --remote-debugging-port=9231 --user-data-dir=/home/x/.hub-core/browsers/vm"),
                 (11, "/usr/bin/chrome --type=renderer --remote-debugging-port=9231"),
                 (12, "/usr/bin/chrome --remote-debugging-port=92310 --user-data-dir=/home/x/mine")]
        self.assertEqual(run.foreign(9231, "/home/x/.hub-work2/browsers/vm", procs), 10)
        self.assertIsNone(run.foreign(9231, "/home/x/.hub-core/browsers/vm", procs))   # its own
        self.assertIsNone(run.foreign(9232, "/home/x/mine", procs))                    # nobody on it that tells
        self.assertIsNone(run.foreign(9231, "/x", None))                               # no list of processes
        state = tmpdir(self)
        with open(os.path.join(state, "hub.json"), "w") as f:
            json.dump({"browsers": {"vm": {"kind": "local", "port": 9231}}}, f)
        with mock.patch.dict(os.environ, {"HUB_CONFIG": os.path.join(state, "hub.json")}):
            sup = run.Supervisor(state, probe=lambda url: "FakeChrome")   # no version: no update to restart for
            sup.listed, sup.reap = (lambda: procs), (lambda url, now: False)
            from modules.browser import browsers
            from core import catalog
            cfg = catalog.load_config(state)
            got = sup.check(cfg, "vm", browsers.configured(cfg)["vm"])
            self.assertFalse(got["up"])
            self.assertIn("9231", got["problem"])
            sup.owned.clear()
            sup._listing = (float("-inf"), None)
            sup.listed = lambda: [(10, procs[0][1].replace("/home/x/.hub-core/browsers/vm",
                                                           browsers.profile_dir(state, "vm", {})))]
            self.assertTrue(sup.check(cfg, "vm", browsers.configured(cfg)["vm"])["up"])


class Tailnet(unittest.TestCase):
    def test_without_expose_the_instances_own_ports(self):
        from modules.tailnet.run import Tailnet as T
        state = tmpdir(self)
        instance.write(state, {"name": "work2", "ports": {"web": 8801, "files": 8802}})
        with mock.patch.dict(os.environ):
            os.environ.pop("HUB_WEB_PORT", None)
            os.environ.pop("HUB_FILES_PORT", None)
            self.assertEqual(T(state, config=lambda: {}).expose(), {"web": 8801, "files": 8802})
            self.assertEqual(T(tmpdir(self), config=lambda: {}).expose(), {"web": 8787, "files": 8788})


class WindowsFiles(unittest.TestCase):
    """deploy/windows/setup.py --instance and the scheduler task of an instance (run on any OS: files and names)."""
    def test_launcher_and_commands_carry_the_instance(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("win_setup", os.path.join(REPO, "deploy", "windows", "setup.py"))
        setup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(setup)
        state, cfg = tmpdir(self), tmpdir(self)
        instance.write(state, {"name": "work2", "ports": {"web": 8801, "files": 8802},
                               "service": {"kind": "task", "id": "Agents Hub (work2)"}})
        hub_cmd, launcher = setup.scripts("C:\\code", state, "C:\\py\\python.exe", encoding="utf-8",
                                          instance="work2", config=cfg)
        with open(launcher, encoding="utf-8", newline="") as f:
            text = f.read()
        for line in (f'set "HUB_STATE_DIR={state}"', f'set "HUB_CONFIG_DIR={cfg}"', 'set "HUB_WEB_PORT=8801"',
                     'set "HUB_FILES_PORT=8802"'):
            self.assertIn(line + "\r\n", text)
        with open(os.path.join(state, "cmd", "hub-work2.cmd"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("if not defined HUB_SESSION goto hub", text)
        self.assertIn("exit /b 2", text)
        from platforms import windows
        self.assertEqual(windows._task_of(state), "Agents Hub (work2)")
        self.assertEqual(windows._task_of(tmpdir(self)), "Agents Hub")

    def test_a_second_hub_of_the_one_line_install_carries_both_and_starts_by_start_py(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("win_setup3", os.path.join(REPO, "deploy", "windows", "setup.py"))
        setup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(setup)
        state, cfg = tmpdir(self), tmpdir(self)
        instance.write(state, {"name": "work2", "ports": {"web": 8801, "files": 8802}})
        app = tmpdir(self)
        with open(os.path.join(app, "paths.json"), "w") as f:
            json.dump({"python": "runtime/python/python.exe", "git": "runtime/git/cmd/git.exe"}, f)
        hub_cmd, launcher = setup.scripts("C:\\code", state, "C:\\py\\python.exe", encoding="utf-8", app=app,
                                          instance="work2", config=cfg, shell=True)
        with open(launcher, encoding="utf-8", newline="") as f:
            text = f.read()
        lines = text.split("\r\n")
        self.assertTrue(lines[1].startswith("if exist ") and lines[1].endswith("stopped\" exit /b 0"), lines[1])
        self.assertIn('set "HUB_CONFIG_DIR=' + cfg + '"', lines)
        self.assertIn('set "HUB_WEB_PORT=8801"', lines)
        prepare = [x for x in lines if "start.py" in x]
        self.assertEqual(prepare, ['"C:\\py\\python.exe" -I -B "' + app + '\\start.py" --prepare'])
        self.assertTrue(any(x.startswith('set "PATH=') and "git" in x for x in lines))   # the environment's
        self.assertLess(lines.index(prepare[0]), next(i for i, x in enumerate(lines) if "hub-up" in x))
        with open(os.path.join(state, "cmd", "hub-work2.cmd"), encoding="utf-8") as f:
            self.assertIn("exit /b 2", f.read())

class MacBundle(unittest.TestCase):
    def test_an_instances_bundle_is_its_own_in_login_items(self):
        import importlib.util
        import plistlib
        spec = importlib.util.spec_from_file_location("mac_app2", os.path.join(REPO, "deploy", "mac", "app.py"))
        app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(app)
        bundle = os.path.join(tmpdir(self), "Agents Hub.app")
        os.makedirs(os.path.join(bundle, "Contents"))
        with open(os.path.join(bundle, "Contents", "Info.plist"), "wb") as f:
            plistlib.dump(app.info_plist(identifier="com.hub-spare"), f)
        agent = app.agent_plist("/py", "/repo", "/state", bundle, label="com.hub-spare")
        self.assertEqual(agent["AssociatedBundleIdentifiers"], ["com.hub-spare"])


if __name__ == "__main__":
    unittest.main()
