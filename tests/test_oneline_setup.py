"""The one-line install after the environment is unpacked (spec §4.5, §5.1, §7): the environment's install.py hands over
to deploy/oneline/setup.py of the newest code; setup.py keeps the key, takes the channel's latest layer (and hands
over to its setup.py), moves a hub from git, points <state>/app at the environment, writes the settings, the commands
and the service of each OS through that pointer, waits for the page; a run again repairs and updates without a
restart of a hub that is fine. The service manager, the page and `hub` are stand-ins (the run recorder); the layers
are real ones of core/appcode.py, the delivery server a stand-in on 127.0.0.1 (the live runs: Docker, winstand, the
Mac bridge — the task's record)."""
import tests  # noqa: F401 - keeps the machine's settings out
import contextlib
import functools
import hashlib
import http.server
import importlib.machinery
import importlib.util
import io
import itertools
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from unittest import mock

import platforms
from core import appcode, shortcut

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETUP = os.path.join(ROOT, "deploy", "oneline", "setup.py")
PACKAGE = os.path.join(ROOT, "tools", "package")
KEY = "read-key-of-the-test-0123"


def load(name, path):
    """A module of a Python text (an installer of a release before is kept as .txt: not this tree's code)."""
    spec = importlib.util.spec_from_file_location(name, path, loader=importlib.machinery.SourceFileLoader(name, path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    if mode is not None:
        os.chmod(path, mode)


REAL = ("tools/package/first_run.py", "deploy/mac/app.py", "deploy/windows/setup.py")   # what setup.py loads by path


def real(rel):
    with open(os.path.join(ROOT, *rel.split("/")), encoding="utf-8") as f:
        return f.read()


def layer(folder, version, extra=None, flavor="team"):
    """A code layer of this version and flavor in folder (a few files, the hub session's rules, bin/hub; its MANIFEST)."""
    files = {"bin/hub": "print('hub')\n", "bin/hub-up": "print('up')\n", "hub-home/CLAUDE.md": "rules\n",
             "hub-home/ONBOARDING.md": "hello\n", "core/version.py": "RUNTIME_MIN = 1\n", "README.md": "readme\n",
             "core/appmove.py": "# the move\n", "deploy/oneline/setup.py": "# the layer's own installer\n",
             **{rel: real(rel) for rel in REAL}, **(extra or {})}
    for rel, text in files.items():
        write(os.path.join(folder, *rel.split("/")), text)
    icon = os.path.join(*shortcut.ICON.split(os.sep))   # the picture of the menu shortcut: a binary file
    os.makedirs(os.path.join(folder, os.path.dirname(icon)), exist_ok=True)
    shutil.copy2(os.path.join(ROOT, icon), os.path.join(folder, icon))
    appcode.make(folder, version, flavor, "c0ffee" + version.replace(".", ""), mtime=1700000000)
    return folder


def environment(root, version="9.9.1", flavor="team"):
    """An environment folder as the factory lays it out (the parts this install reads)."""
    env = os.path.join(root, "shells", f"1-{flavor}")
    os.makedirs(os.path.join(env, "runtime", "python", "bin"))
    os.makedirs(os.path.join(env, "runtime", "node", "bin"))
    for name in ("launch.py", "install.py", "start.py"):
        shutil.copy2(os.path.join(PACKAGE, name), os.path.join(env, name))
    shutil.copy2(os.path.join(ROOT, "core", "appcode.py"), os.path.join(env, "appcode.py"))
    write(os.path.join(env, "PACKAGED"), json.dumps({"format": "app", "layout": "layer", "flavor": flavor,
                                                     "version": version, "runtime": 1, "platform": "linux-x64"}))
    write(os.path.join(env, "paths.json"), json.dumps({"python": "runtime/python/bin/python3",
                                                       "node": "runtime/node/bin/node", "mcp": "runtime/mcp/mcp.js",
                                                       "ffmpeg": "runtime/ffmpeg/bin/ffmpeg"}))
    layer(os.path.join(env, "code-seed"), version, flavor=flavor)
    return env


class Recorder:
    """The commands the installer runs: answers by their first words; everything recorded."""

    def __init__(self, test):
        self.calls, self.test = [], test
        self.find = None       # the clone `hub app find --json` names, or None
        self.finds = {}        # state folder -> what `hub app find --json` says of it (a second hub's: its instance)
        self.move_rc = 0
        self.undo_rc = 0       # `hub app undo`: 4 — the return did not finish
        self.on_move = None    # what `hub app move` does besides its exit code (its record)
        self.active = False    # systemctl --user is-active
        self.user = True       # a systemd --user manager
        self.linger = "Linger=yes"
        self.enable_rc = 0     # loginctl enable-linger: 0 — polkit lets the user, 1 — it wants a password
        self.enable_raises = None   # or what calling it raises (it hung: TimeoutExpired)
        self.manager = False   # without a login session: the manager linger started answers at /run/user/<uid>
        self.missing = ()      # programs that are not on this machine
        self.envs = []         # (command, its env) of every call that set one
        self.lnk_rc = 0        # the PowerShell that makes the shortcut's .lnk: its exit code

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        if kw.get("env") is not None:
            self.envs.append((list(cmd), kw["env"]))
        if cmd[0] in self.missing:
            raise FileNotFoundError(2, "No such file or directory", cmd[0])
        out, rc = "", 0
        words = [os.path.basename(c) if i == 0 else c for i, c in enumerate(cmd)]
        if "-EncodedCommand" in cmd:   # the shortcut: PowerShell makes the file the variable names
            rc = self.lnk_rc
            if not rc:   # the shortcut file names its hub.cmd among its strings, in UTF-16 as a .lnk does
                os.makedirs(os.path.dirname(kw["env"]["HUB_LNK"]), exist_ok=True)
                with open(kw["env"]["HUB_LNK"], "wb") as f:
                    f.write(b"L\x00\x00\x00" + kw["env"]["HUB_LNK_ARGS"].encode("utf-16-le"))
        if "app" in cmd and "find" in cmd:
            got = self.finds.get((kw.get("env") or {}).get("HUB_STATE_DIR")) or {}
            repo = got.get("repo") or self.find
            rc = 0 if repo else 3
            out = json.dumps({"repo": repo, "version": "v9.0.0", **got, "texts": {
                "title": "T", "text": "A hub from git", "move": "Move", "stay": "Stay", "later": "Later",
                "waiting": "Moving", "failed": "F", "undo_failed": "U"}}) if repo else "{}"
        elif "app" in cmd and "move" in cmd and "--done" not in cmd:
            rc = self.move_rc
            if self.on_move and not rc:
                self.on_move()
        elif "app" in cmd and "undo" in cmd:
            rc = self.undo_rc
        elif "ui" in cmd and "--print" in cmd:
            out = "http://127.0.0.1:8787/\n"
        elif words[0] == "systemctl" and "show-environment" in cmd:
            via = self.manager and self.linger == "Linger=yes" and (kw.get("env") or {}).get("XDG_RUNTIME_DIR")
            rc = 0 if self.user or via else 1
        elif words[0] == "systemctl" and "is-active" in cmd:
            rc = 0 if self.active else 3
        elif words[0] == "loginctl" and "show-user" in cmd:
            out = self.linger + "\n"
        elif words[0] == "loginctl" and "enable-linger" in cmd:
            if self.enable_raises:
                raise self.enable_raises
            rc = self.enable_rc
            if not rc:
                self.linger = "Linger=yes"
        if kw.get("stdout") not in (None, subprocess.PIPE, subprocess.DEVNULL) and hasattr(kw["stdout"], "write"):
            kw["stdout"].write(out)
        return subprocess.CompletedProcess(cmd, rc, out, "")

    def ran(self, *words):
        return [c for c in self.calls if all(w in c for w in words)]


class Server(http.server.BaseHTTPRequestHandler):
    """releases.json and the layer archives of the test, by the key (key None: to anyone, as the open build's pages);
    asked — (path, its Authorization header) of every request; redirects — {path: where it sends}."""
    files = {}
    key = KEY
    asked = []
    redirects = {}

    def do_GET(self):
        self.asked.append((self.path, self.headers.get("Authorization")))
        if self.path in self.redirects:
            self.send_response(302)
            self.send_header("Location", self.redirects[self.path])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.key is not None and self.headers.get("Authorization") != f"Bearer {self.key}":
            self.send_response(401)
            self.end_headers()
            return
        body = self.files.get(self.path)
        self.send_response(200 if body is not None else 404)
        self.send_header("Content-Length", str(len(body or b"")))
        self.end_headers()
        self.wfile.write(body or b"")

    def log_message(self, *a):
        pass


class Base(unittest.TestCase):
    os_name = "linux"
    flavor = "team"

    def setUp(self):
        if self.os_name != "windows" and os.sep != "/":
            # a Linux or macOS hub's install names its folders in a unit file and makes links: this machine's paths
            # (C:\…) are no Linux paths, and an ordinary Windows user may not make links — it runs on Linux and macOS
            self.skipTest("the install of a Linux or macOS hub: on a machine with / paths")
        self.tmp = tempfile.mkdtemp(prefix="hub-oneline-")
        self.addCleanup(self._clean)
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.home, ".hub-core")
        self.config = os.path.join(self.home, ".config", "hub-core")
        os.makedirs(self.home)
        self.env = environment(os.path.join(self.home, ".local", "share", "agents-hub"), flavor=self.flavor)
        self.keyfile = os.path.join(self.tmp, "key")
        write(self.keyfile, KEY + "\n", 0o600)
        self.run_ = Recorder(self)
        self.up = [True]       # the hub's page answers once its service is started
        self.busy = False      # another program answers on the port
        self.running = False   # the hub is up from before this run
        self.spawned = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Server)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        Server.files, Server.asked, Server.key, Server.redirects = {}, [], (KEY if self.flavor == "team" else None), {}
        self.release(None)
        patcher = mock.patch.dict(os.environ, {"HUB_CONFIG_DIR": self.config, "HUB_CONFIG": "", "HUB_STATE_DIR": "", "HUB_LANG": "en",
                                               "HOME": self.home, "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.setup = load("hub_test_oneline_setup", SETUP)
        root = mock.patch.object(self.setup, "ROOT", os.path.join(self.env, "code-seed"))   # as the seed's setup.py runs
        root.start()
        self.addCleanup(root.stop)
        self.systemd_dir = os.path.join(self.tmp, "run-systemd-system")   # the machine's init is systemd
        self.linger_dir = os.path.join(self.tmp, "linger")
        os.makedirs(self.systemd_dir)
        for name, value in (("SYSTEMD", self.systemd_dir), ("LINGER", self.linger_dir),
                            ("user_name", lambda job: "tester")):
            p = mock.patch.object(self.setup.Install, name, value)
            p.start()
            self.addCleanup(p.stop)

    def _clean(self):
        for d, dirs, files in os.walk(self.tmp):
            for n in dirs + files:
                p = os.path.join(d, n)
                if not os.path.islink(p):
                    os.chmod(p, 0o700)
        platforms.current.rmtree(self.tmp)

    def release(self, version, channel="stable", installer=None):
        """releases.json names this layer version for the channel (None — no release yet); installer: the text of its
        deploy/oneline/setup.py (a path)."""
        doc = {"channels": {}}
        if version:
            extra = {"deploy/oneline/setup.py": open(installer, encoding="utf-8").read()} if installer else None
            name = f"{version}-{self.flavor}"
            folder = layer(os.path.join(self.tmp, "build", name), version, extra, flavor=self.flavor)
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as t:
                t.add(folder, arcname=name)
            data = buf.getvalue()
            Server.files[f"/v1/layer/code-{name}.tar.gz"] = data
            doc["channels"][channel] = {"version": version, "commit": appcode.load(folder)["commit"],
                                        "layer": {"url": f"/v1/layer/code-{name}.tar.gz",
                                                  "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}}
        Server.files["/v1/releases.json"] = json.dumps(doc).encode()

    def argv(self, *more, channel="stable"):
        key = ["--key-file", self.keyfile] if self.flavor == "team" else []   # the open build has no key
        return ["--contract", "1", "--env", self.env, "--server", self.url, *key,
                "--platform", "linux-x64", "--flavor", self.flavor, "--runtime", "1", "--channel", channel,
                "--state", self.state, "--no-open", *more]

    hand_to = None   # a setup.py the hand-over to a layer's installer runs in this process (else it is only recorded)

    def install(self, *more, ask=None, channel="stable", environ=None, out=None):
        out = out or io.StringIO()
        job_env = {"PATH": "/usr/bin:/bin", "SHELL": "/bin/bash", "USER": "tester", **(environ or {})}
        kw = {"run": self.run_, "ask": ask or (lambda p: "3"), "out": out, "osname": self.os_name, "home": self.home,
              "environ": job_env, "spawn": self.spawned.append, "clock": itertools.count().__next__,
              "sleep": lambda s: None}
        if self.hand_to:
            kw = {**kw, "run": self.go_on(kw)}
        with self.steps(self.setup):
            code = self.setup.main(self.argv(*more, channel=channel), **kw)
        self.out = out.getvalue()
        return code

    @contextlib.contextmanager
    def steps(self, mod, root=None):
        """The stand-ins of an installer module's steps that reach the machine (the page; root — the code it runs from)."""
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(mod.Install, "page_up", lambda job: self.page()))
            stack.enter_context(mock.patch.object(mod.Install, "wait_page", lambda job, seconds=0: self.page()))
            if root:
                stack.enter_context(mock.patch.object(mod, "ROOT", root))
                for name, value in (("SYSTEMD", self.systemd_dir), ("LINGER", self.linger_dir),
                                    ("user_name", lambda job: "tester")):
                    stack.enter_context(mock.patch.object(mod.Install, name, value))
            yield

    def go_on(self, kw):
        """The run of the commands where a hand-over to a layer's deploy/oneline/setup.py is that installer (the text of
        hand_to) run here with the words it was given, as its own process would be; self.handed — those commands."""
        self.handed = []
        inner = load(f"hub_test_handed_{id(self)}", self.hand_to)

        def run(cmd, **more):
            if len(cmd) > 3 and cmd[0] == sys.executable and cmd[3].endswith(os.path.join("deploy", "oneline", "setup.py")):
                self.handed.append(list(cmd))
                folder = os.path.dirname(os.path.dirname(os.path.dirname(cmd[3])))
                self.handed_root = folder
                with self.steps(inner, root=folder):
                    self.handed_files = sorted(os.listdir(os.path.dirname(folder)))
                    return subprocess.CompletedProcess(cmd, inner.main(cmd[4:], **kw), "", "")
            return self.run_(cmd, **more)
        return run

    def page(self):
        started = self.spawned or any(c[:3] == ["systemctl", "--user", "restart"] or c[:2] == ["launchctl", "bootstrap"]
                                      or c[:1] == ["powershell.exe"] for c in self.run_.calls)
        return self.busy or (self.up[0] and (self.running or bool(started)))

    def unit(self):
        with open(os.path.join(self.home, ".config", "systemd", "user", "agents-hub.service"), encoding="utf-8") as f:
            return f.read()


class New(Base):
    def test_a_new_hub_from_the_environment_s_own_release(self):
        self.assertEqual(self.install(), 0, self.out)
        # the key of the line where the update reads it, for the owner only
        path = os.path.join(self.state, "layers", "deliver.json")
        with open(path) as f:
            self.assertEqual(json.load(f), {"url": self.url, "key": KEY})
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        # the pointers: the code onto the seed's layer, <state>/app onto the environment
        self.assertEqual(appcode.current(os.path.join(self.state, "code"), os.path.join(self.state, "layers")),
                         "9.9.1-team")
        self.assertEqual(os.path.realpath(os.path.join(self.state, "app")), os.path.realpath(self.env))
        self.assertEqual(os.readlink(os.path.join(self.state, "bin", "python3")),
                         os.path.join(self.state, "app", "runtime", "python", "bin", "python3"))
        self.assertEqual(os.readlink(os.path.join(self.state, "hub-home", "CLAUDE.md")),
                         os.path.join(self.state, "code", "hub-home", "CLAUDE.md"))
        # the settings: the environment's programs through the pointer
        with open(os.path.join(self.config, "hub.json")) as f:
            cfg = json.load(f)
        self.assertEqual(cfg["browser"]["mcp"], [os.path.join(self.state, "app", "runtime", "node", "bin", "node"),
                                                 os.path.join(self.state, "app", "runtime", "mcp", "mcp.js")])
        self.assertEqual(cfg["voice"]["ffmpeg"], os.path.join(self.state, "app", "runtime", "ffmpeg", "bin", "ffmpeg"))
        self.assertNotIn("update", cfg)
        # the command through the pointers; ~/.local/bin in PATH by the marked line, once
        with open(os.path.join(self.home, ".local", "bin", "hub")) as f:
            wrapper = f.read()
        self.assertIn(f'"{os.path.join(self.state, "app", "runtime", "python", "bin", "python3")}" '
                      f'"{os.path.join(self.state, "code")}/bin/hub"', wrapper)
        with open(os.path.join(self.home, ".bash_profile")) as f:
            self.assertEqual(f.read().count(self.setup.PATH_MARK), 1)
        # the service runs start.py of the environment through the pointer, for this state folder
        unit = self.unit()
        start = os.path.join(self.state, "app", "start.py")
        self.assertIn(f"ExecStart={os.path.join(self.state, 'app', 'runtime', 'python', 'bin', 'python3')} -I -B {start}\n",
                      unit)
        self.assertIn(f"Environment=HUB_STATE_DIR={self.state}\n", unit)
        self.assertTrue(self.run_.ran("systemctl", "--user", "restart", "agents-hub.service"))
        self.assertIn("The hub is running: http://127.0.0.1:8787/", self.out)
        # the core finds this service as the hub's (hub on/off/uninstall)
        import platforms
        if platforms.current.APP_OS == "linux":
            got = platforms.current.service_unit(self.state, home=self.home)
            self.assertEqual(got and got["id"], "agents-hub.service")

    def test_an_older_line_with_the_nightly_channel_installs_the_stable_release(self):
        # one channel (the owner's decision of 2026-11-09, m1917882): a nightly entry is not read, nothing about a
        # channel goes into the hub's settings
        self.release("9.9.6", channel="nightly")
        self.assertEqual(self.install(channel="nightly"), 0, self.out)
        self.assertEqual(appcode.current(os.path.join(self.state, "code"), os.path.join(self.state, "layers")),
                         "9.9.1-team")   # the environment's own seed: the nightly 9.9.6 is not taken
        with open(os.path.join(self.config, "hub.json")) as f:
            self.assertNotIn("update", json.load(f))

    def test_a_newer_release_is_downloaded_and_its_installer_goes_on(self):
        self.release("9.9.4")
        self.hand_to = SETUP
        self.assertEqual(self.install("--move"), 0, self.out)
        self.assertEqual(len(self.handed), 1)
        self.assertEqual(self.handed[0][-2:], ["--move", "--handed"])
        # it was checked apart from the state folder, then the newer installer put it in place and points the code at it
        self.assertFalse(os.path.normcase(self.handed_root).startswith(os.path.normcase(self.state)))
        self.assertEqual(self.handed_files, ["9.9.4-team", "archives"])
        folder = os.path.join(self.state, "layers", "9.9.4-team")
        self.assertEqual(appcode.diff(folder, appcode.load(folder)), [])
        self.assertTrue(os.path.isfile(os.path.join(self.state, "layers", "archives", "code-9.9.4-team.tar.gz")))
        self.assertEqual(appcode.current(os.path.join(self.state, "code"), os.path.join(self.state, "layers")),
                         "9.9.4-team")
        # where it was checked: the install's own downloads, nothing of it left there once the line ended well
        apart = os.path.dirname(self.handed_root)
        self.assertEqual(apart, os.path.join(os.path.dirname(os.path.dirname(self.env)), "downloads", "layer"))
        self.assertEqual(os.listdir(apart), ["archives"])
        self.assertEqual(os.listdir(os.path.join(apart, "archives")), [])

    def test_a_release_that_is_not_newer_is_not_downloaded(self):
        self.release("9.9.1")
        self.assertEqual(self.install(), 0, self.out)
        self.assertFalse(os.path.exists(os.path.join(self.state, "layers", "archives")))

    def test_no_answer_from_the_server_installs_the_environment_s_release_for_the_update_to_finish(self):
        self.server.shutdown()
        self.server.server_close()
        self.assertEqual(self.install(), 0, self.out)
        self.assertIn("The delivery server did not answer", self.out)
        self.assertTrue(os.path.isfile(os.path.join(self.state, "layers", "fresh.json")))

    def test_a_layer_of_another_sum_is_not_taken(self):
        self.release("9.9.4")
        doc = json.loads(Server.files["/v1/releases.json"])
        doc["channels"]["stable"]["layer"]["sha256"] = "0" * 64
        Server.files["/v1/releases.json"] = json.dumps(doc).encode()
        self.assertEqual(self.install(), 0, self.out)
        self.assertFalse(self.run_.ran(os.path.join("9.9.4-team", "deploy", "oneline", "setup.py")))
        self.assertEqual(appcode.current(os.path.join(self.state, "code"), os.path.join(self.state, "layers")),
                         "9.9.1-team")

    def test_the_key_file_must_hold_a_key(self):
        write(self.keyfile, "no key\n")
        self.assertEqual(self.install(), 1)
        self.assertIn("is not a key", self.out)
        self.assertFalse(os.path.exists(os.path.join(self.state, "layers")))

    def test_a_service_of_another_program_at_the_name_is_left_and_nothing_is_started(self):
        write(os.path.join(self.home, ".config", "systemd", "user", "agents-hub.service"),
              "[Service]\nExecStart=/usr/bin/other\n")
        self.assertEqual(self.install(), 1, self.out)
        self.assertIn("is a service of another hub or another program", self.out)
        self.assertFalse(self.run_.ran("systemctl", "--user", "restart"))

    def test_without_systemd_the_hub_starts_in_the_background_and_says_how_again(self):
        self.run_.missing = ("systemctl", "loginctl")   # a container: not even the programs
        self.up = [False]
        self.assertEqual(self.install(), 4, self.out)   # the stand-in page never answers
        self.assertEqual(self.spawned[-1], [os.path.join(self.state, "app", "runtime", "python", "bin", "python3"),
                                            "-I", "-B", os.path.join(self.state, "app", "start.py")])

    def test_linger_is_set_by_the_user_or_named_with_sudo(self):
        self.run_.linger = "Linger=no"
        self.assertEqual(self.install(), 0, self.out)
        self.assertTrue(self.run_.ran("loginctl", "enable-linger", "tester"))
        self.assertNotIn("sudo loginctl", self.out)   # the stand-in let it

    def test_linger_never_waits_for_a_password(self):
        self.run_.linger = "Linger=no"
        self.install()
        self.assertTrue(self.run_.ran("loginctl", "--no-ask-password", "enable-linger"))

    def test_linger_the_system_refuses_is_one_line_with_sudo_and_the_service_still_runs(self):
        self.run_.linger, self.run_.enable_rc = "Linger=no", 1
        self.assertEqual(self.install(), 0, self.out)
        self.assertEqual(self.out.count("sudo loginctl enable-linger tester"), 1, self.out)
        self.assertTrue(self.run_.ran("systemctl", "--user", "restart", "agents-hub.service"))
        self.assertEqual(self.spawned, [])

    def test_linger_that_hangs_still_gets_the_sudo_line(self):
        self.run_.linger, self.run_.enable_raises = "Linger=no", subprocess.TimeoutExpired(["loginctl"], 30)
        self.assertEqual(self.install(), 0, self.out)
        self.assertIn("sudo loginctl enable-linger tester", self.out)

    def test_linger_already_marked_is_not_asked_again(self):
        write(os.path.join(self.linger_dir, "tester"), "")
        self.run_.linger = "Linger=no"   # what loginctl would say is not asked at all
        self.assertEqual(self.install(), 0, self.out)
        self.assertFalse(self.run_.ran("loginctl"))

    def test_a_server_without_a_screen_is_told_the_ssh_tunnel_to_its_page(self):
        # the page listens on 127.0.0.1 only: the person's computer reaches it by one SSH command (install.sh --server
        # keeps this line for its login message)
        self.assertEqual(self.install(environ={"SSH_CONNECTION": "198.51.100.7 51000 203.0.113.5 22"}), 0, self.out)
        self.assertIn("This machine has no screen", self.out)
        self.assertIn("\n  ssh -N -L 8787:127.0.0.1:8787 -L 8788:127.0.0.1:8788 tester@<server address>\n", self.out)
        self.assertEqual(self.install(environ={"DISPLAY": ":0"}), 0, self.out)
        self.assertNotIn("ssh -N", self.out)

    def test_an_install_writes_no_page_key_settings(self):
        # the hint about other accounts of the machine is gone (the page has a password, core/pagepass.py): an earlier
        # install.sh --server that still hands its accounts on changes nothing in hub.json
        self.assertEqual(self.install(environ={"HUB_INSTALL_ACCOUNTS": "root ubuntu "}), 0, self.out)
        with open(os.path.join(self.config, "hub.json"), encoding="utf-8") as f:
            self.assertNotIn("page_key", json.load(f))
        self.assertNotIn("ubuntu", self.out)

    def test_a_shell_without_a_login_session_reaches_the_manager_linger_starts(self):
        # sudo -iu, su, cron: no XDG_RUNTIME_DIR; linger starts user@<uid>.service and its bus is at /run/user/<uid>
        self.run_.user, self.run_.manager, self.run_.linger = False, True, "Linger=no"
        self.assertEqual(self.install(), 0, self.out)
        self.assertTrue(self.run_.ran("loginctl", "--no-ask-password", "enable-linger", "tester"))
        self.assertEqual(self.spawned, [])   # a service, not a process that dies at the restart
        restart = [e for c, e in self.run_.envs if c[:3] == ["systemctl", "--user", "restart"]]
        self.assertEqual(restart[-1]["XDG_RUNTIME_DIR"], f"/run/user/{os.getuid()}")
        self.assertIn("ExecStart=", self.unit())
        self.assertNotIn("no systemd --user", self.out)
        opened = [e for c, e in self.run_.envs if "open" in c]
        self.assertEqual(opened[-1]["XDG_RUNTIME_DIR"], f"/run/user/{os.getuid()}")   # `hub open` may start the hub

    def test_a_shell_without_a_login_session_and_no_linger_keeps_the_hub_in_the_background(self):
        self.run_.user, self.run_.manager, self.run_.linger, self.run_.enable_rc = False, True, "Linger=no", 1
        self.assertEqual(self.install(), 0, self.out)
        self.assertEqual(len(self.spawned), 1)
        self.assertIn("sudo loginctl enable-linger tester", self.out)
        self.assertFalse(self.run_.ran("systemctl", "--user", "restart"))

    def test_a_machine_whose_init_is_not_systemd_asks_for_no_linger(self):
        os.rmdir(self.systemd_dir)   # a container: systemctl is there, its manager is not
        self.run_.user, self.run_.linger = False, "Linger=no"
        self.install()
        self.assertEqual(len(self.spawned), 1)
        self.assertFalse(self.run_.ran("loginctl", "--no-ask-password", "enable-linger"))
        self.assertNotIn("sudo loginctl", self.out)

    def test_a_port_another_program_answers_on_is_refused_before_anything_is_written(self):
        self.busy = True
        self.assertEqual(self.install(), 1, self.out)
        self.assertIn("Port 8787 is taken", self.out)
        self.assertFalse(os.path.exists(self.state))

    def test_ports_given_to_the_line_go_into_the_service(self):
        self.assertEqual(self.install(environ={"HUB_WEB_PORT": "18787", "HUB_FILES_PORT": "18788"}), 0, self.out)
        self.assertIn("Environment=HUB_WEB_PORT=18787\n", self.unit())
        self.assertIn("Environment=HUB_FILES_PORT=18788\n", self.unit())

    def test_a_state_folder_with_a_space_is_refused_on_linux_before_anything(self):
        self.state = os.path.join(self.home, "my hub")
        self.assertEqual(self.install(), 1, self.out)
        self.assertIn("a space or a quote", self.out)
        self.assertFalse(os.path.exists(self.state))

    def test_a_state_folder_of_its_own_leaves_the_terminal_s_hub_to_the_main_hub(self):
        write(os.path.join(self.config, "state-dir"), os.path.join(self.home, ".hub-core") + "\n")   # the main hub's
        write(os.path.join(self.home, ".hub-core", "journal.jsonl"), '{"seq":1}\n')
        self.state = os.path.join(self.home, "other-hub")
        self.assertEqual(self.install(), 0, self.out)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".local", "bin", "hub")))
        self.assertIn(f'HUB_STATE_DIR="{self.state}"', self.out)

    def test_a_mark_that_names_a_removed_hub_is_this_hub_s_now(self):
        write(os.path.join(self.config, "state-dir"), os.path.join(self.home, "gone-hub") + "\n")
        self.state = os.path.join(self.home, "other-hub")
        self.assertEqual(self.install(), 0, self.out)
        self.assertTrue(os.path.exists(os.path.join(self.home, ".local", "bin", "hub")))
        with open(os.path.join(self.config, "state-dir")) as f:
            self.assertEqual(f.read().strip(), self.state)

    def test_bash_on_linux_gets_the_line_where_a_terminal_window_reads_it(self):
        # a terminal window of a Linux desktop reads ~/.bashrc alone; Terminal.app's bash is a login one: ~/.profile
        rc = sys.platform != "darwin"
        write(os.path.join(self.home, ".profile"), "# the system's\n")
        self.assertEqual(self.install(), 0, self.out)
        for name in (".profile", ".bashrc")[:2 if rc else 1]:
            with open(os.path.join(self.home, name)) as f:
                self.assertEqual(f.read().count(self.setup.PATH_MARK), 1, name)
        self.assertEqual(os.path.exists(os.path.join(self.home, ".bashrc")), rc)
        self.install()
        with open(os.path.join(self.home, ".bashrc" if rc else ".profile")) as f:
            self.assertEqual(f.read().count(self.setup.PATH_MARK), 1)

    def test_an_archive_of_another_layer_leaves_the_one_there_as_it_is(self):
        self.release("9.9.4")
        doc = json.loads(Server.files["/v1/releases.json"])
        Server.files.pop("/v1/layer/code-9.9.4-team.tar.gz")
        self.release("9.9.3")   # the asset of 9.9.3 uploaded under 9.9.4's name
        wrong = Server.files["/v1/layer/code-9.9.3-team.tar.gz"]
        doc["channels"]["stable"]["layer"].update(sha256=hashlib.sha256(wrong).hexdigest(), size=len(wrong))
        Server.files["/v1/layer/code-9.9.4-team.tar.gz"] = wrong
        Server.files["/v1/releases.json"] = json.dumps(doc).encode()
        there = layer(os.path.join(self.state, "layers", "9.9.3-team"), "9.9.3")
        self.assertEqual(self.install(), 0, self.out)
        self.assertEqual(appcode.diff(there, appcode.load(there)), [])
        self.assertIn("the folder 9.9.3-team, not 9.9.4-team", self.out)

    def test_a_page_that_does_not_answer_is_said_with_the_log(self):
        self.up = [False]
        self.assertEqual(self.install(), 4)
        self.assertIn("did not answer", self.out)

    def test_an_unknown_word_is_64(self):
        self.assertEqual(self.setup.main(self.argv("--what")), 64)
        self.assertEqual(self.setup.main([a if a != "1" else "2" for a in self.argv()]), 64)


class Again(Base):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.install(), 0, self.out)
        self.run_.calls.clear()
        self.run_.active = True
        self.running = True

    def test_a_hub_that_is_fine_is_not_restarted(self):
        self.assertEqual(self.install(), 0, self.out)
        self.assertFalse(self.run_.ran("systemctl", "--user", "restart"))
        self.assertFalse(self.run_.ran("systemctl", "--user", "daemon-reload"))

    def test_a_service_that_is_gone_is_written_again(self):
        os.unlink(os.path.join(self.home, ".config", "systemd", "user", "agents-hub.service"))
        self.assertEqual(self.install(), 0, self.out)
        self.assertTrue(self.run_.ran("systemctl", "--user", "restart", "agents-hub.service"))

    def test_an_edited_layer_gets_its_released_files_back_and_the_running_hub_a_restart(self):
        path = os.path.join(self.state, "code", "README.md")
        os.chmod(path, 0o644)
        with open(path, "a") as f:
            f.write("an edit\n")
        self.assertEqual(self.install(), 0, self.out)
        with open(path) as f:
            self.assertEqual(f.read(), "readme\n")
        self.assertIn("the released files are back", self.out)
        with open(os.path.join(self.state, "restart")) as f:
            self.assertEqual(json.load(f)["parts"], [])

    def test_a_newer_release_is_installed_as_the_update_button_does(self):
        self.release("9.9.4")
        self.hand_to = SETUP   # the newer release's installer does the run again
        self.assertEqual(self.install(), 0, self.out)
        self.assertEqual(len(self.handed), 1)
        # the archive it downloaded is where the update takes it from instead of downloading it again
        self.assertTrue(os.path.isfile(os.path.join(self.state, "layers", "archives", "code-9.9.4-team.tar.gz")))
        self.assertEqual(len(self.run_.ran(os.path.join(self.state, "code", "bin", "hub"), "update")), 1)
        self.assertFalse(os.path.exists(os.path.join(self.state, "layers", "9.9.4-team")))   # the update's to take
        self.assertIn("A newer release 9.9.4 is out", self.out)

    def test_the_owner_s_own_settings_stay(self):
        path = os.path.join(self.config, "hub.json")
        with open(path) as f:
            cfg = json.load(f)
        cfg["browser"]["mcp"] = ["npx", "-y", "my-mcp"]
        cfg["voice"]["ffmpeg"] = os.path.join(self.tmp, "gone", "ffmpeg")   # a program that is not there any more
        with open(path, "w") as f:
            json.dump(cfg, f)
        self.assertEqual(self.install(), 0, self.out)
        with open(path) as f:
            cfg = json.load(f)
        self.assertEqual(cfg["browser"]["mcp"], ["npx", "-y", "my-mcp"])
        self.assertEqual(cfg["voice"]["ffmpeg"], os.path.join(self.state, "app", "runtime", "ffmpeg", "bin", "ffmpeg"))

    def test_the_ports_of_the_service_stay_when_the_line_runs_again_without_them(self):
        unit = os.path.join(self.home, ".config", "systemd", "user", "agents-hub.service")
        with open(unit) as f:
            text = f.read()
        with open(unit, "w") as f:
            f.write(text.replace("Environment=HUB_STATE_DIR", "Environment=HUB_WEB_PORT=18787\nEnvironment=HUB_STATE_DIR"))
        with mock.patch("platforms.current.service_unit", lambda state, home=None: {"port": 18787, "files_port": None}):
            self.install()
            self.assertIn("Environment=HUB_WEB_PORT=18787\n", self.unit())
            self.install(environ={"HUB_FILES_PORT": "18790"})   # one given: the other one stays
        self.assertIn("Environment=HUB_WEB_PORT=18787\n", self.unit())
        self.assertIn("Environment=HUB_FILES_PORT=18790\n", self.unit())

    def test_the_line_of_another_key_keeps_the_new_key(self):
        write(self.keyfile, "another-key-of-the-test-9\n")
        Server.files = {}
        self.install()
        with open(os.path.join(self.state, "layers", "deliver.json")) as f:
            self.assertEqual(json.load(f)["key"], "another-key-of-the-test-9")


class EnvAgain(Base):
    """A run again whose environment is not the one the hub runs on (spec §5.1, §6.2)."""

    def setUp(self):
        super().setUp()
        self.assertEqual(self.install(), 0, self.out)   # as Again's: the hub of a first run, up
        self.run_.calls.clear()
        self.run_.active = True
        self.running = True

    def another(self, name, runtime=1, whole=True, swaps=True):
        env = os.path.join(os.path.dirname(self.env), name)
        shutil.copytree(self.env, env, symlinks=True)
        with open(os.path.join(env, "PACKAGED")) as f:
            mark = json.load(f)
        write(os.path.join(env, "PACKAGED"), json.dumps({**mark, "runtime": runtime, **({"swaps": ["folder"]} if swaps else {})}))
        if whole:
            write(os.path.join(env, ".agents-hub-shell.json"), json.dumps({"platform": "linux-x64", "runtime": runtime}))
        return env

    def swapping(self):
        """The environment the hub runs on: its watchdog moves pointers (its PACKAGED says so)."""
        with open(os.path.join(self.env, "PACKAGED")) as f:
            mark = json.load(f)
        write(os.path.join(self.env, "PACKAGED"), json.dumps({**mark, "swaps": ["folder"]}))

    def points_at(self):
        return os.path.realpath(os.path.join(self.state, "app"))

    def test_a_broken_environment_left_behind_moves_the_pointer_and_restarts_the_hub(self):
        new = self.another("1-team-5")
        self.env = new
        self.assertEqual(self.install(), 0, self.out)
        self.assertEqual(self.points_at(), os.path.realpath(new))
        self.assertTrue(self.run_.ran("systemctl", "--user", "restart", "agents-hub.service"))

    def test_without_systemd_a_hub_up_on_the_left_one_restarts_itself_through_the_pointer(self):
        self.run_.user = False   # a container: the hub runs in the background, not under a service
        new = self.another("1-team-6")
        self.env = new
        self.assertEqual(self.install(), 0, self.out)
        self.assertEqual(len(self.run_.ran(os.path.join(self.state, "code", "bin", "hub"), "restart")), 1)
        self.assertEqual(self.spawned, [])   # not a second hub next to the running one

    def test_a_newer_one_next_to_a_whole_one_is_left_to_the_update(self):
        write(os.path.join(self.env, ".agents-hub-shell.json"), "{}")   # the one it runs on is whole
        self.swapping()
        was, new = self.env, self.another("2-team", runtime=2)
        self.env = new
        self.assertEqual(self.install(), 0, self.out)
        self.assertEqual(self.points_at(), os.path.realpath(was))
        self.assertFalse(self.run_.ran("systemctl", "--user", "restart"))
        self.assertIn("A newer environment is ready", self.out)
        with open(os.path.join(self.state, "layers", "env-next.json")) as f:
            self.assertEqual(json.load(f)["file"], os.path.realpath(new))   # kept from the pruning for the update
        self.assertFalse(os.path.exists(os.path.join(self.state, "layers", "shell-update.json")))   # no record touched
        self.assertTrue(os.path.isdir(new))

    def test_a_line_of_another_flavor_is_taken_even_with_an_older_number(self):
        write(os.path.join(self.env, ".agents-hub-shell.json"), "{}")
        with open(os.path.join(self.env, "PACKAGED")) as f:
            mark = json.load(f)
        write(os.path.join(self.env, "PACKAGED"), json.dumps({**mark, "runtime": 6}))
        new = self.another("5-open", runtime=5)
        with open(os.path.join(new, "PACKAGED")) as f:
            write(os.path.join(new, "PACKAGED"), json.dumps({**json.load(f), "flavor": "open"}))
        self.env = new
        self.install()
        self.assertEqual(self.points_at(), os.path.realpath(new))

    def test_without_systemd_a_hub_that_does_not_stop_is_not_joined_by_a_second(self):
        self.run_.user = False
        new = self.another("1-team-8")
        self.env = new
        self.up = [False]
        self.running = False
        stops = []
        real = Recorder.__call__

        def run(rec, cmd, **kw):
            if cmd[-1:] == ["stop"]:
                stops.append(cmd)
                self.busy = True   # it answers again, and did not stop
                return subprocess.CompletedProcess(cmd, 1, "not stopped", "")
            return real(rec, cmd, **kw)
        with mock.patch.object(Recorder, "__call__", run), \
                mock.patch.object(self.setup.Install, "page_up", lambda job: self.busy):
            self.assertNotEqual(self.install(), 0)
        self.assertTrue(stops)
        self.assertEqual(self.spawned, [])

    def test_an_older_one_next_to_a_newer_whole_one_is_no_step_back(self):
        write(os.path.join(self.env, ".agents-hub-shell.json"), "{}")
        with open(os.path.join(self.env, "PACKAGED")) as f:
            mark = json.load(f)
        write(os.path.join(self.env, "PACKAGED"), json.dumps({**mark, "runtime": 6}))   # the hub is on 6 (nightly)
        was = self.env
        self.env = self.another("5-team", runtime=5)   # the stable line brings 5
        self.assertEqual(self.install(), 0, self.out)
        self.assertEqual(self.points_at(), os.path.realpath(was))
        self.assertFalse(self.run_.ran("systemctl", "--user", "restart"))

    def test_a_newer_one_and_an_old_watchdog_without_pointers_moves_the_pointer_now(self):
        write(os.path.join(self.env, ".agents-hub-shell.json"), "{}")
        new = self.another("2-team", runtime=2)
        self.env = new
        self.assertEqual(self.install(), 0, self.out)
        self.assertEqual(self.points_at(), os.path.realpath(new))

    def test_a_new_one_the_hub_does_not_come_up_on_goes_back_to_the_whole_one(self):
        write(os.path.join(self.env, ".agents-hub-shell.json"), "{}")
        was, new = self.env, self.another("1-open")
        self.env = new
        self.up = [False]
        calls = []

        def back(job, env, real=self.setup.Install.back_to):
            calls.append(env)
            self.up[0] = True
            return real(job, env)
        with mock.patch.object(self.setup.Install, "back_to", back):
            code = self.install()
        self.assertEqual(code, self.setup.NOT_UP, self.out)
        self.assertEqual(calls, [os.path.realpath(was)])
        self.assertEqual(self.points_at(), os.path.realpath(was))
        self.assertIn("did not come up on the new environment", self.out)

    def test_the_environments_no_hub_needs_go_at_the_end(self):
        write(os.path.join(self.env, ".agents-hub-shell.json"), "{}")
        stale = self.another("0-team", runtime=0)
        self.assertEqual(self.install(), 0, self.out)
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.isdir(self.env))


class AsInstance(Base):
    """`hub instance new` of a hub of the one-line install runs setup.py --as-instance <name> for the new hub's folder
    (core/instancecli.py oneline): its own service, commands, settings and ports — of its anchor."""

    def setUp(self):
        super().setUp()
        self.assertEqual(self.install(), 0, self.out)   # the main hub
        from core import instance
        self.anchor = instance.setup("work2", os.path.join(self.home, ".hub-work2", "code"), kind="layer",
                                     default_state=self.state, default_config=self.config, platform="linux",
                                     is_free=lambda p: True)
        self.main_state, self.state = self.state, self.anchor["state"]
        self.run_.calls.clear()

    def test_its_own_unit_commands_settings_and_no_mark_of_the_main_hub(self):
        mark = os.path.join(self.config, "state-dir")
        with open(mark) as f:
            main_mark = f.read()
        self.assertEqual(self.install("--as-instance", "work2", "--handed"), 0, self.out)
        web, files = self.anchor["ports"]["web"], self.anchor["ports"]["files"]
        cfg_dir = self.anchor["config_dir"]
        with open(os.path.join(self.home, ".config", "systemd", "user", "agents-hub-work2.service")) as f:
            unit = f.read()
        for line in ("Description=Hub by Icons8 (work2)", f"Environment=HUB_STATE_DIR={self.state}",
                     f"Environment=HUB_WEB_PORT={web}", f"Environment=HUB_FILES_PORT={files}",
                     f"Environment=HUB_CONFIG_DIR={cfg_dir}"):
            self.assertIn(line + "\n", unit)
        self.assertTrue(self.run_.ran("systemctl", "--user", "restart", "agents-hub-work2.service"))
        self.assertIn("agents-hub.service", os.listdir(os.path.join(self.home, ".config", "systemd", "user")))
        with open(os.path.join(self.home, ".local", "bin", "hub-work2")) as f:
            self.assertIn(f'HUB_STATE_DIR="{self.state}"', f.read())
        with open(mark) as f:
            self.assertEqual(f.read(), main_mark)   # `hub` of the terminal stays the main hub's
        with open(os.path.join(cfg_dir, "hub.json")) as f:
            cfg = json.load(f)
        self.assertEqual((cfg["title"], cfg["tailnet"]["expose"]), ("work2", {"web": web, "files": files}))
        self.assertEqual(os.path.realpath(os.path.join(self.state, "app")), os.path.realpath(self.env))
        with open(os.path.join(self.state, "layers", "deliver.json")) as f:
            self.assertEqual(json.load(f)["key"], KEY)

    def test_a_folder_without_its_anchor_is_refused(self):
        self.state = os.path.join(self.home, ".hub-other")
        self.assertEqual(self.install("--as-instance", "other", "--handed"), self.setup.FAILED)
        self.assertIn("hub instance new other", self.out)
        self.assertFalse(os.path.exists(os.path.join(self.state, "app")))


class FromGit(Base):
    def setUp(self):
        super().setUp()
        self.clone = os.path.join(self.home, "agents-hub-core")
        self.run_.find = self.clone
        write(os.path.join(self.state, "journal.jsonl"), '{"seq":1}\n')

    def test_nobody_to_ask_changes_nothing(self):
        self.assertEqual(self.install("--no-input"), 3, self.out)
        self.assertEqual(sorted(os.listdir(self.state)), ["journal.jsonl"])
        self.assertIn("--move", self.out)

    def test_stay_and_later_in_the_terminal(self):
        self.assertEqual(self.install(ask=lambda p: "2"), 0, self.out)
        self.assertTrue(self.run_.ran("app", "stay"))
        self.assertEqual(self.install(ask=lambda p: ""), 0, self.out)
        self.assertIn("Nothing was changed", self.out)
        self.assertEqual(sorted(os.listdir(self.state)), ["journal.jsonl"])

    def test_move_stops_the_clone_s_hub_before_the_pointer_and_marks_it_done(self):
        self.assertEqual(self.install(ask=lambda p: "1"), 0, self.out)
        moves = [i for i, c in enumerate(self.run_.calls) if "app" in c and "move" in c]
        self.assertEqual(self.run_.calls[moves[0]][-3:], [self.clone, "--app", self.env])
        self.assertEqual(self.run_.calls[moves[-1]][-2:], ["move", "--done"])
        self.assertTrue(os.path.islink(os.path.join(self.state, "app")))
        self.assertIn("Moved", self.out)

    def test_a_move_from_a_shell_without_a_login_session_stops_the_clone_s_service_through_the_manager(self):
        # sudo -iu: `hub app move` stops the clone's systemd --user unit before this run writes its own service
        self.run_.user, self.run_.manager, self.run_.linger = False, True, "Linger=no"
        self.assertEqual(self.install("--move"), 0, self.out)
        move = [e for c, e in self.run_.envs if "move" in c and "--done" not in c]
        self.assertEqual(move[0]["XDG_RUNTIME_DIR"], f"/run/user/{os.getuid()}")
        self.assertEqual(len(self.run_.ran("loginctl", "--no-ask-password", "enable-linger")), 1)   # asked once

    def test_a_move_that_does_not_come_up_goes_back_to_the_clone(self):
        self.up = [False]
        self.assertEqual(self.install("--move"), 5, self.out)
        self.assertTrue(self.run_.ran("app", "undo", "--failed"))

    def test_a_port_still_answering_after_the_clone_stopped_is_another_program_s_and_the_move_goes_back(self):
        self.busy = True
        self.assertEqual(self.install("--move"), 5, self.out)
        self.assertIn("Port 8787 is taken", self.out)
        self.assertTrue(self.run_.ran("app", "undo", "--failed"))
        self.assertFalse(self.run_.ran("systemctl", "--user", "restart"))

    def test_a_move_the_clone_s_hub_refuses_changes_nothing_more(self):
        self.run_.move_rc = 1
        self.assertEqual(self.install("--move"), 1, self.out)
        self.assertFalse(os.path.lexists(os.path.join(self.state, "app")))
        self.assertFalse(self.run_.ran("systemctl", "--user", "restart"))

    def broken_off(self):
        """The record of a move a run of the line before broke off: the clone's hub stopped, nothing came up."""
        write(os.path.join(self.state, "app-move", "record.json"),
              json.dumps({"v": 1, "status": "moving", "repo": self.clone, "os": "linux", "saved": [], "into": "saved-1"}))

    def index(self, *words):
        return [i for i, c in enumerate(self.run_.calls) if all(w in c for w in words)]

    def test_a_move_a_run_before_broke_off_is_undone_first_then_asked_again(self):
        self.broken_off()
        self.assertEqual(self.install("--move"), 0, self.out)
        undo, move = self.index("app", "undo", "--failed"), self.index("app", "move", self.clone)
        self.assertTrue(undo and move and undo[0] < move[0], self.run_.calls)
        self.assertIn("broke off the move", self.out)

    def test_a_broken_off_move_whose_clone_unit_is_gone_is_undone_and_moved_again(self):
        # Linux: the move took the clone's unit away and pointed `hub` at the app's code — `hub app find` sees no clone
        # until the undo puts the unit back
        clone = self.clone

        class Back(type(self.run_)):
            def __call__(self, cmd, **kw):
                if "app" in cmd and "undo" in cmd:
                    self.find = clone
                return super().__call__(cmd, **kw)
        self.run_ = Back(self)
        self.broken_off()
        self.assertEqual(self.install("--move"), 0, self.out)
        self.assertTrue(self.index("app", "move", self.clone), self.out)

    def test_a_broken_off_move_whose_undo_does_not_finish_installs_nothing_more(self):
        self.broken_off()
        self.run_.undo_rc = 4
        self.assertEqual(self.install("--move"), 1, self.out)
        self.assertIn("app undo", self.out)
        self.assertFalse(self.index("app", "move", self.clone))
        self.assertFalse(os.path.lexists(os.path.join(self.state, "app")))

    def test_a_hub_up_on_the_app_whose_move_was_not_marked_done_is_marked_now(self):
        self.assertEqual(self.install("--move"), 0, self.out)   # the move came up; its --done was lost (a kill)
        self.broken_off()
        self.run_.calls.clear()
        self.running = True
        with mock.patch("core.restart.running", return_value={"pid": 1}):
            self.assertEqual(self.install(), 0, self.out)
        self.assertFalse(self.index("app", "undo"))
        self.assertTrue(self.index("app", "move", "--done"), self.run_.calls)

    def test_ctrl_c_after_the_clone_s_hub_stopped_puts_it_back_on_the_clone(self):
        self.run_.on_move = self.broken_off   # the move's record, as `hub app move` writes it
        with mock.patch.object(self.setup.Install, "folders", side_effect=KeyboardInterrupt):
            self.assertEqual(self.install("--move"), 130, self.out)
        self.assertTrue(self.index("app", "undo", "--failed"), self.run_.calls)
        self.assertIn("interrupted", self.out)

    def test_ctrl_c_while_hub_app_move_runs_waits_for_it_then_goes_back(self):
        # the terminal gives the child the Ctrl+C too: it is not killed a moment later — this run goes on once it is
        # done, and puts the hub back on the clone if the move is still half done
        import signal

        def moving():
            self.broken_off()
            signal.raise_signal(signal.SIGINT)
            self.run_.after_signal = True
        self.run_.on_move = moving
        old = signal.signal(signal.SIGINT, signal.default_int_handler)
        self.addCleanup(signal.signal, signal.SIGINT, old)
        self.assertEqual(self.install("--move"), 130, self.out)
        self.assertTrue(getattr(self.run_, "after_signal", False))
        self.assertTrue(self.index("app", "undo", "--failed"), self.run_.calls)

    def test_a_ctrl_c_before_the_move_changed_anything_undoes_nothing(self):
        with mock.patch.object(self.setup.Install, "folders", side_effect=KeyboardInterrupt):
            self.assertEqual(self.install("--move"), 130, self.out)
        self.assertFalse(self.index("app", "undo"))

    def test_a_broken_off_move_with_another_install_s_hub_up_is_left_to_it(self):
        self.broken_off()
        self.run_.find = None
        with mock.patch("core.restart.running", return_value={"pid": 1}), \
                mock.patch.object(platforms.current, "command_line", return_value="/opt/agents-hub/python hub-up"):
            self.install("--move")
        self.assertFalse(self.index("app", "undo"))

    def test_a_return_that_did_not_finish_is_undone_again_whatever_hub_is_up(self):
        write(os.path.join(self.state, "app-move", "record.json"),
              json.dumps({"v": 1, "status": "moving", "returning": True, "repo": self.clone, "os": "linux",
                          "saved": [], "into": "saved-1"}))
        with mock.patch("core.restart.running", return_value={"pid": 1}), \
                mock.patch.object(platforms.current, "command_line", return_value="/opt/agents-hub/python hub-up"):
            self.install("--move")
        self.assertTrue(self.index("app", "undo", "--failed"))

    def test_a_broken_off_move_whose_clone_s_hub_came_up_again_is_undone_first(self):
        self.broken_off()
        hub_up = os.path.join(self.clone, "bin", "hub-up")
        with mock.patch("core.restart.running", return_value={"pid": 1}), \
                mock.patch.object(platforms.current, "command_line", return_value=f"/usr/bin/python3 {hub_up}"):
            self.assertEqual(self.install("--move"), 0, self.out)
        self.assertTrue(self.index("app", "undo", "--failed"))

    def test_a_move_that_failed_midway_and_did_not_get_back_says_to_undo(self):
        self.run_.move_rc = 4
        self.assertEqual(self.install("--move"), 1, self.out)
        self.assertIn("app undo", self.out)
        self.assertNotIn("runs as before", self.out)

    def test_a_hub_neither_from_git_nor_this_install_is_left_alone(self):
        self.run_.find = None   # its service runs a hub-up that is not of a git clone
        write(os.path.join(self.home, ".config", "systemd", "user", "agents-hub.service"),
              f"[Service]\nExecStart=/usr/bin/python3 /home/x/hub/bin/hub-up\nEnvironment=HUB_STATE_DIR={self.state}\n")
        from platforms import posix   # a Linux machine's services, on a Mac too (there: launchd agents)
        with mock.patch.object(platforms.current, "service_unit", functools.partial(posix.service_unit, platform="linux")):
            self.assertEqual(self.install(), 1, self.out)
        self.assertIn("put neither by this install nor from a git clone", self.out)
        self.assertEqual(sorted(os.listdir(self.state)), ["journal.jsonl"])

    def test_a_hub_nothing_runs_is_said_as_such(self):
        self.run_.find = None
        self.assertEqual(self.install(), 1, self.out)
        self.assertIn("nothing runs it", self.out)
        self.assertEqual(sorted(os.listdir(self.state)), ["journal.jsonl"])


class Early(Base):
    """The installer of the environment's seed only downloads and checks the channel's newer layer and hands over to its
    setup.py before it reads the state folder: what the line refuses, asks and does is the latest release's."""

    def setUp(self):
        super().setUp()
        self.release("9.9.4")

    def hand_overs(self):
        return [c for c in self.run_.calls
                if c[3:4] and c[3].endswith(os.path.join("9.9.4-team", "deploy", "oneline", "setup.py"))]

    def handed_over(self):
        handed = self.hand_overs()
        self.assertEqual(len(handed), 1, self.out)
        self.assertEqual(handed[0][4:], self.argv() + ["--handed"])
        return handed[0]

    def test_a_hub_the_line_does_not_take_is_the_latest_installer_s_to_refuse(self):
        write(os.path.join(self.state, "state.json"), "{}")   # a hub's data nothing runs
        self.assertEqual(self.install(), 0, self.out)   # the stand-in of the newer installer's process: 0
        self.handed_over()
        self.assertEqual(sorted(os.listdir(self.state)), ["state.json"])   # nothing of the line in that folder
        self.assertFalse(os.path.exists(self.config))

    def test_a_hub_from_git_is_the_latest_installer_s_to_ask_about(self):
        self.run_.find = os.path.join(self.home, "agents-hub-core")
        write(os.path.join(self.state, "journal.jsonl"), '{"seq":1}\n')
        self.assertEqual(self.install(), 0, self.out)
        self.handed_over()
        self.assertFalse(self.run_.ran("app", "find"))   # not even read by the seed
        self.assertEqual(sorted(os.listdir(self.state)), ["journal.jsonl"])

    def test_a_second_hub_is_the_latest_installer_s(self):
        self.assertEqual(self.install("--instance", "maps"), 0, self.out)
        self.assertEqual(self.hand_overs()[0][-3:], ["--instance", "maps", "--handed"])
        self.assertFalse(os.path.exists(self.state))

    def test_the_layer_is_checked_apart_and_a_run_again_after_one_that_failed_downloads_it_no_more(self):
        real = Recorder.__call__

        def run(rec, cmd, **kw):
            if cmd[3:4] and cmd[3].endswith(os.path.join("oneline", "setup.py")):
                rec.calls.append(list(cmd))
                return subprocess.CompletedProcess(cmd, 1, "", "")   # the newer installer failed (a busy port)
            return real(rec, cmd, **kw)
        with mock.patch.object(Recorder, "__call__", run):
            self.assertEqual(self.install(), 1)
        setup = self.handed_over()[3]
        data = os.path.dirname(os.path.dirname(self.env))
        self.assertEqual(setup, os.path.join(data, "downloads", "layer", "9.9.4-team", "deploy", "oneline", "setup.py"))
        self.assertFalse(os.path.exists(os.path.dirname(os.path.dirname(os.path.dirname(setup)))))   # the layer: gone
        Server.files.pop("/v1/layer/code-9.9.4-team.tar.gz")   # the server would not give it again
        self.run_.calls.clear()
        self.assertEqual(self.install(), 0, self.out)
        self.handed_over()
        self.assertNotIn("did not pass", self.out)
        self.assertEqual(os.listdir(os.path.join(os.path.dirname(os.path.dirname(self.env)), "downloads", "layer",
                                                 "archives")), [])   # ended well: gone

    def test_what_else_is_in_its_folder_goes_and_never_fails_the_line(self):
        apart = os.path.join(os.path.dirname(os.path.dirname(self.env)), "downloads", "layer")
        for rel in ("stray-file", os.path.join("9.9.2-team", "x"), os.path.join("archives", "code-9.9.2-team.tar.gz")):
            write(os.path.join(apart, rel), "left by a run before")
        self.assertEqual(self.install(), 0, self.out)
        self.assertEqual(os.listdir(apart), ["archives"])
        self.assertEqual(os.listdir(os.path.join(apart, "archives")), [])

    def test_a_folder_it_cannot_write_lets_this_code_go_on(self):
        downloads = os.path.join(os.path.dirname(os.path.dirname(self.env)), "downloads")
        write(downloads, "a file, not a folder")
        self.assertEqual(self.install(), 0, self.out)
        self.assertFalse(self.hand_overs())
        self.assertEqual(appcode.current(os.path.join(self.state, "code"), os.path.join(self.state, "layers")),
                         "9.9.1-team")

    def test_its_exit_code_is_the_line_s(self):
        real = Recorder.__call__

        def run(rec, cmd, **kw):
            if cmd[3:4] and cmd[3].endswith(os.path.join("oneline", "setup.py")):
                return subprocess.CompletedProcess(cmd, 3, "", "")
            return real(rec, cmd, **kw)
        with mock.patch.object(Recorder, "__call__", run):
            self.assertEqual(self.install(), 3)

    def test_a_bad_key_is_this_installer_s_to_refuse_and_nothing_is_downloaded(self):
        write(self.keyfile, "no key\n")
        self.assertEqual(self.install(), 1)
        self.assertIn("is not a key", self.out)
        self.assertFalse(self.hand_overs())
        self.assertNotIn("Downloading", self.out)

    def test_no_answer_is_said_once(self):
        self.server.shutdown()
        self.server.server_close()
        self.assertEqual(self.install(), 0, self.out)
        self.assertEqual(self.out.count("The delivery server did not answer"), 1, self.out)

    def test_a_handed_over_installer_hands_over_no_more(self):
        self.assertEqual(self.install("--handed"), 0, self.out)
        self.assertFalse(self.hand_overs())
        self.assertEqual(appcode.current(os.path.join(self.state, "code"), os.path.join(self.state, "layers")),
                         "9.9.1-team")


class Contract(Base):
    """Contract 1 both ways: the environments of 1.8.90 on the delivery server (their seed's installer reads the state
    folder and hands over after the question) go on to this installer, and this environment's seed hands over to a
    layer whose installer is of 1.8.90."""
    OLD = os.path.join(ROOT, "tests", "golden", "oneline-setup-1.8.90.txt")   # deploy/oneline/setup.py of 1.8.90

    def test_an_environment_of_1_8_90_goes_on_to_this_installer(self):
        self.setup = load("hub_test_oneline_setup_1890", self.OLD)
        self.release("9.9.4", installer=SETUP)
        self.hand_to = SETUP
        with self.steps(self.setup, root=os.path.join(self.env, "code-seed")):
            self.assertEqual(self.install(), 0, self.out)
        self.assertEqual(self.handed[0][-1], "--handed")
        self.assertEqual(appcode.current(os.path.join(self.state, "code"), os.path.join(self.state, "layers")),
                         "9.9.4-team")
        self.assertIn(f"ExecStart={os.path.join(self.state, 'app', 'runtime', 'python', 'bin', 'python3')}", self.unit())
        self.assertIn("The hub is running", self.out)

    def test_a_move_from_an_environment_of_1_8_90_is_not_asked_twice(self):
        self.setup = load("hub_test_oneline_setup_1890", self.OLD)
        self.release("9.9.4", installer=SETUP)
        self.hand_to = SETUP
        self.run_.find = os.path.join(self.home, "agents-hub-core")
        write(os.path.join(self.state, "journal.jsonl"), '{"seq":1}\n')
        asked = []
        with self.steps(self.setup, root=os.path.join(self.env, "code-seed")):
            self.assertEqual(self.install(ask=lambda p: asked.append(p) or "1"), 0, self.out)
        self.assertEqual(len(asked), 1)
        self.assertEqual(self.handed[0][-2:], ["--handed", "--move"])   # 1.8.90's order
        self.assertTrue(self.run_.ran("app", "move", "--done"))

    def test_this_environment_goes_on_to_an_installer_of_1_8_90(self):
        self.release("9.9.4", installer=self.OLD)
        self.hand_to = self.OLD
        self.assertEqual(self.install(), 0, self.out)
        self.assertEqual(self.handed[0][4:], self.argv() + ["--handed"])
        # that one takes only a layer of the state folder for its own: the seed's, and the update brings the latest
        self.assertEqual(appcode.current(os.path.join(self.state, "code"), os.path.join(self.state, "layers")),
                         "9.9.1-team")
        self.assertTrue(os.path.isfile(os.path.join(self.state, "layers", "fresh.json")))
        self.assertIn("The hub is running", self.out)


class FromGitInstance(Base):
    """A second hub from git (an installer run with HUB_INSTANCE: ~/.hub-<name>, its settings folder in its service):
    the line with --instance <name>, or with HUB_STATE_DIR on its folder, moves that hub as itself; the plain line says
    which line moves it."""

    def setUp(self):
        super().setUp()
        from core import instance
        self.clone = os.path.join(self.home, "agents-hub-personal")
        self.anchor = instance.setup("personal", self.clone, default_state=self.state, default_config=self.config,
                                     platform="linux", is_free=lambda p: True)
        self.istate = self.anchor["state"]
        write(os.path.join(self.istate, "journal.jsonl"), '{"seq":1}\n')
        self.run_.finds[self.istate] = {"repo": self.clone, "instance": "personal",
                                        "config_dir": self.anchor["config_dir"]}

    def moved_as_itself(self):
        moves = [(c, e) for c, e in self.run_.envs if "app" in c and "move" in c and "--done" not in c]
        self.assertEqual(moves[0][0][-3:], [self.clone, "--app", self.env])
        self.assertEqual(moves[0][1]["HUB_STATE_DIR"], self.istate)
        self.assertTrue(self.run_.ran("app", "move", "--done"))
        self.assertFalse(self.run_.ran("instance", "new"))
        units = os.path.join(self.home, ".config", "systemd", "user")
        with open(os.path.join(units, "agents-hub-personal.service")) as f:
            unit = f.read()
        for line in (f"Environment=HUB_STATE_DIR={self.istate}", f"Environment=HUB_CONFIG_DIR={self.anchor['config_dir']}",
                     f"Environment=HUB_WEB_PORT={self.anchor['ports']['web']}"):
            self.assertIn(line + "\n", unit)
        self.assertFalse(os.path.exists(os.path.join(units, "agents-hub.service")))   # the main hub's: none made
        self.assertTrue(os.path.isfile(os.path.join(self.home, ".local", "bin", "hub-personal")))
        self.assertFalse(os.path.lexists(os.path.join(self.home, ".local", "bin", "hub")))
        self.assertEqual(os.path.realpath(os.path.join(self.istate, "app")), os.path.realpath(self.env))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".hub-core")))   # no main hub was made next to it

    def test_the_line_with_its_name_moves_that_hub(self):
        self.assertEqual(self.install("--instance", "personal", "--move"), 0, self.out)
        self.moved_as_itself()
        self.assertIn("The command: hub-personal", self.out)
        self.assertIn("hub-personal app undo", self.out)

    def test_a_hub_of_that_name_the_line_does_not_know_is_not_installed_over(self):
        self.run_.finds = {}   # not from git (or its find failed): never `hub instance new` over a hub that runs
        self.assertEqual(self.install("--instance", "personal", "--move"), 1, self.out)
        self.assertFalse(self.run_.ran("instance", "new"))
        self.assertIn(self.istate, self.out)
        self.assertFalse(os.path.lexists(os.path.join(self.istate, "app")))

    def test_the_line_with_its_state_folder_moves_that_hub(self):
        self.state = self.istate
        self.assertEqual(self.install("--move"), 0, self.out)
        self.moved_as_itself()

    def test_nobody_to_ask_changes_nothing(self):
        before = sorted(os.listdir(self.istate))
        self.assertEqual(self.install("--instance", "personal", "--no-input"), 3, self.out)
        self.assertEqual(sorted(os.listdir(self.istate)), before)
        self.assertFalse(self.run_.ran("app", "move"))

    def test_the_plain_line_names_the_line_that_moves_it_and_the_log_never_the_key(self):
        log = os.path.join(self.tmp, "install.log")
        self.assertEqual(self.install(environ={"HUB_INSTALL_LOG": log}, ask=lambda p: "2"), 0, self.out)
        line = f"curl -fsSL {self.url}/i/{KEY}/install.sh | bash -s -- --instance personal"
        self.assertEqual(self.out.count(line), 2, self.out)   # at the start and again at the end
        self.assertIn(self.clone, self.out)
        with open(log, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("--instance personal", text)
        self.assertNotIn(KEY, text)
        self.assertFalse(self.run_.ran("app", "move"))   # its own hub is not moved by the plain line
        self.assertTrue(os.path.lexists(os.path.join(self.state, "app")))   # a main hub next to it: asked for

    def test_the_plain_line_with_no_main_hub_asks_to_move_it(self):
        asked = []
        self.assertEqual(self.install(ask=lambda p: asked.append(p) or "1"), 0, self.out)
        self.assertEqual(len(asked), 1)
        self.assertIn("Move personal", self.out)
        self.moved_as_itself()

    def test_the_plain_line_with_no_main_hub_later_changes_nothing(self):
        before = sorted(os.listdir(self.istate))
        self.assertEqual(self.install(ask=lambda p: ""), 0, self.out)
        self.assertIn("Nothing was changed", self.out)
        self.assertFalse(os.path.exists(self.state))
        self.assertEqual(sorted(os.listdir(self.istate)), before)
        self.assertFalse(self.run_.ran("app", "move"))

    def test_the_plain_line_with_no_main_hub_and_nobody_to_ask_installs_nothing(self):
        for flags in (("--no-input",), ("--yes",)):
            with self.subTest(flags=flags):
                self.assertEqual(self.install(*flags), 3, self.out)
                self.assertIn(f"curl -fsSL {self.url}/i/{KEY}/install.sh | bash -s -- --instance personal", self.out)
                self.assertIn("--stay", self.out)
                self.assertFalse(os.path.exists(self.state))
                self.assertFalse(self.run_.ran("app", "move"))
                self.assertFalse(os.path.exists(os.path.join(self.home, ".config", "systemd", "user")))

    def test_the_plain_line_with_its_answer_given(self):
        self.assertEqual(self.install("--move"), 0, self.out)
        self.moved_as_itself()

    def test_move_with_several_says_which_and_installs_nothing(self):
        from core import instance
        other = instance.setup("maps", os.path.join(self.home, "agents-hub-maps"), default_state=self.state,
                               default_config=self.config, platform="linux", is_free=lambda p: True)
        write(os.path.join(other["state"], "journal.jsonl"), '{"seq":1}\n')
        self.run_.finds[other["state"]] = {"repo": os.path.join(self.home, "agents-hub-maps"), "instance": "maps",
                                           "config_dir": other["config_dir"]}
        self.assertEqual(self.install("--move"), 3, self.out)
        self.assertIn("--move does not say which", self.out)
        self.assertFalse(self.run_.ran("app", "move"))
        self.assertFalse(os.path.exists(self.state))
        asked = []
        self.assertEqual(self.install(ask=lambda p: asked.append(p) or "1"), 0, self.out)   # the first by name: maps
        self.assertIn("(1 to 4)", asked[0])
        moves = [(c, e) for c, e in self.run_.envs if "app" in c and "move" in c and "--done" not in c]
        self.assertEqual(moves[0][1]["HUB_STATE_DIR"], other["state"])

    def test_the_main_hub_s_folder_by_the_install_s_mark_is_asked_about_too(self):
        self.state = os.path.join(self.home, "hubs", "main")
        write(os.path.join(self.config, "state-dir"), self.state + "\n")   # the main hub is put there (core/statemark)
        self.assertEqual(self.install("--no-input"), 3, self.out)
        self.assertFalse(os.path.exists(self.state))

    def test_a_folder_the_line_was_given_is_never_asked_about(self):
        self.state = os.path.join(self.home, "my-hub")
        self.assertEqual(self.install("--no-input"), 0, self.out)
        self.assertTrue(os.path.lexists(os.path.join(self.state, "app")))
        self.assertFalse(self.run_.ran("app", "move"))

    def test_after_an_installer_of_1_8_90_nothing_of_it_is_left_when_nothing_is_installed(self):
        # it made ~/.hub-core/layers (the key, the layer, its archive) before it handed over
        self.setup = load("hub_test_oneline_setup_1890", Contract.OLD)
        self.release("9.9.4", installer=SETUP)
        self.hand_to = SETUP
        with self.steps(self.setup, root=os.path.join(self.env, "code-seed")):
            self.assertEqual(self.install(ask=lambda p: ""), 0, self.out)
        self.assertEqual(len(self.handed), 1)
        self.assertIn("Nothing was changed", self.out)
        self.assertFalse(os.path.exists(self.state))

    def test_the_plain_line_with_stay_installs_a_main_hub_next_to_it(self):
        self.assertEqual(self.install("--stay"), 0, self.out)
        self.assertTrue(os.path.lexists(os.path.join(self.state, "app")))
        self.assertFalse(self.run_.ran("app", "move"))
        self.assertFalse(self.run_.ran("app", "stay"))   # its clone is not told anything

    def test_a_main_hub_left_without_a_service_is_said_and_left_as_it_is(self):
        # a Mac whose work hub moved to another machine: ~/.hub-core keeps its data, no service runs it
        for name, text in (("state.json", "{}"), ("journal.jsonl", "")):
            write(os.path.join(self.state, name), text)
        before = sorted(os.listdir(self.state))
        for flags in ((), ("--no-input",), ("--yes",)):   # a line with no terminal is the same
            with self.subTest(flags=flags):
                self.assertEqual(self.install(*flags), 1, self.out)
                self.assertIn("nothing runs it", self.out)
                self.assertNotIn("from git, run", self.out)
                self.assertIn("--instance personal", self.out)   # the line that moves the live hub of this machine
                self.assertEqual(sorted(os.listdir(self.state)), before)
        self.assertFalse(self.run_.ran("systemctl", "--user", "restart"))

    def test_the_line_on_linux_and_on_windows(self):
        self.assertEqual(self.setup.move_line("https://d.example.test", "k" * 20, "maps", False),
                         f"curl -fsSL https://d.example.test/i/{'k' * 20}/install.sh | bash -s -- --instance maps")
        self.assertEqual(self.setup.move_line("https://d.example.test", "k" * 20, "maps", True),
                         f"& ([scriptblock]::Create((irm https://d.example.test/i/{'k' * 20}/install.ps1))) "
                         f"-Instance maps")


class Instance(Base):
    def test_a_second_hub_is_the_main_hub_s_instance_new(self):
        self.assertEqual(self.install(), 0, self.out)
        self.run_.calls.clear()
        self.assertEqual(self.install("--instance", "maps"), 0, self.out)
        self.assertEqual(self.run_.ran("instance", "new")[0][-3:], ["new", "maps", "--yes"])

    def test_no_main_hub_first_the_line_without_it(self):
        self.assertEqual(self.install("--instance", "maps"), 1, self.out)
        self.assertIn("install it first", self.out)


class OpenBuild(Base):
    """The open build: its line has no key — the pages give releases.json and the layers to anyone, and a key file is no
    word of its contract; the update reads the same address without a key."""
    flavor = "open"

    def test_a_new_hub_keeps_the_address_without_a_key_and_asks_with_none(self):
        self.release("9.9.1")
        self.assertEqual(self.install(), 0, self.out)
        with open(os.path.join(self.state, "layers", "deliver.json")) as f:
            self.assertEqual(json.load(f), {"url": self.url})
        self.assertEqual(appcode.current(os.path.join(self.state, "code"), os.path.join(self.state, "layers")),
                         "9.9.1-open")
        self.assertTrue(Server.asked)
        self.assertEqual({auth for _, auth in Server.asked}, {None})

    def test_a_newer_release_is_downloaded_without_a_key_and_its_installer_goes_on(self):
        self.release("9.9.4")
        self.assertEqual(self.install(), 0, self.out)
        handed = [c for c in self.run_.calls
                  if c[3:4] and c[3].endswith(os.path.join("9.9.4-open", "deploy", "oneline", "setup.py"))]
        self.assertEqual(len(handed), 1, self.out)
        self.assertEqual(handed[0][4:], self.argv() + ["--handed"])
        self.assertNotIn("--key-file", handed[0])
        self.assertIn("/v1/layer/code-9.9.4-open.tar.gz", [p for p, _ in Server.asked])
        self.assertEqual({auth for _, auth in Server.asked}, {None})

    def test_a_layer_whose_redirect_leaves_the_pages_and_the_releases_hosts_is_not_asked_there(self):
        self.release("9.9.4")
        Server.redirects["/v1/layer/code-9.9.4-open.tar.gz"] = "https://downloads.example.test/code-9.9.4-open.tar.gz"
        self.assertEqual(self.install(), 0, self.out)   # the environment's own release goes on
        self.assertIn("not an address to download from: https://downloads.example.test/", self.out)
        self.assertEqual([p for p, _ in Server.asked].count("/v1/layer/code-9.9.4-open.tar.gz"), 1)
        self.assertEqual(appcode.current(os.path.join(self.state, "code"), os.path.join(self.state, "layers")),
                         "9.9.1-open")

    def test_only_the_open_build_goes_without_a_key_file(self):
        self.assertNotIn("--key-file", self.setup.parse(self.argv()))
        team = [w if w != "open" else "team" for w in self.argv()]
        with self.assertRaises(ValueError):
            self.setup.parse(team)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.setup.main(team), 64)

    def test_the_line_that_moves_a_second_hub_has_no_key(self):
        self.assertEqual(self.setup.move_line("https://d.example.test/agents-hub", None, "maps", False),
                         "curl -fsSL https://d.example.test/agents-hub/install.sh | bash -s -- --instance maps")
        self.assertEqual(self.setup.move_line("https://d.example.test/agents-hub", None, "maps", True),
                         "& ([scriptblock]::Create((irm https://d.example.test/agents-hub/install.ps1))) -Instance maps")


class Mac(Base):
    os_name = "mac"

    def mac_stub(self, src=None, flavor="team"):
        """deploy/mac/app.py of this tree, signatures taken as checked; the seed carries the bundle src (this tree's)."""
        real = load("hub_test_mac_app", os.path.join(ROOT, "deploy", "mac", "app.py"))
        stub = mock.Mock(wraps=real)
        stub.NAME, stub.NAMES, stub.LSREGISTER, stub.agent_plist = real.NAME, real.NAMES, real.LSREGISTER, real.agent_plist
        stub.verified = lambda bundle: True
        stub.set_file_icon = lambda bundle: True
        src = src or real.BUNDLE
        bundle = os.path.join(self.env, "code-seed", "deploy", "mac", os.path.basename(src))   # the code's bundle
        os.chmod(os.path.dirname(bundle), 0o755)
        shutil.copytree(src, bundle, symlinks=True)
        os.chmod(os.path.join(self.env, "code-seed", appcode.MANIFEST), 0o644)
        appcode.make(os.path.join(self.env, "code-seed"), "9.9.1", flavor, "c0ffee991", mtime=1700000000)
        return stub, os.path.basename(src)

    def install_mac(self, stub):
        def ditto(cmd, **kw):
            if cmd[0] == "ditto":
                shutil.copytree(cmd[1], cmd[2], symlinks=True)
            return Recorder.__call__(self.run_, cmd, **kw)
        self.run_.__class__ = type("MacRecorder", (Recorder,), {"__call__": lambda s, cmd, **kw: ditto(cmd, **kw)})
        with mock.patch.object(self.setup.Install, "mac", lambda job: stub):
            self.assertEqual(self.install(), 0, self.out)

    def test_the_open_flavors_ad_hoc_bundle_takes_the_place_of_the_one_before(self):
        # the open flavor's layer carries "Hub by Icons8.app" (ad hoc, core/appcode.OPEN_SWAP): the agent runs through
        # it by its name and identifier; the team's copy an earlier install left goes, out of LaunchServices first
        import plistlib
        src = next(p for p in (os.path.join(ROOT, "deploy", "mac", "Hub by Icons8.app"),
                               os.path.join(ROOT, "deploy", "mac", "open", "Hub by Icons8.app")) if os.path.isdir(p))
        stub, name = self.mac_stub(src, "open")
        old = os.path.join(self.state, "Agents Hub.app")
        os.makedirs(os.path.join(old, "Contents", "MacOS"))
        self.install_mac(stub)
        with open(os.path.join(self.home, "Library", "LaunchAgents", "com.hub-core.plist"), "rb") as f:
            agent = plistlib.load(f)
        self.assertEqual(agent["ProgramArguments"][0],
                         os.path.join(self.state, "Hub by Icons8.app", "Contents", "MacOS", "Hub by Icons8"))
        self.assertEqual(agent["AssociatedBundleIdentifiers"], ["com.icons8.hub"])
        self.assertFalse(os.path.exists(old))
        self.assertTrue(self.run_.ran(stub.LSREGISTER, "-u"))
        with open(os.path.join(src, "Contents", "MacOS", "Hub by Icons8"), "rb") as a, \
                open(os.path.join(self.state, "Hub by Icons8.app", "Contents", "MacOS", "Hub by Icons8"), "rb") as b:
            self.assertEqual(a.read(), b.read())

    def test_the_agent_runs_start_py_through_the_signed_bundle_and_git_gets_its_wrapper(self):
        import plistlib
        stub, name = self.mac_stub()

        self.install_mac(stub)
        with open(os.path.join(self.home, "Library", "LaunchAgents", "com.hub-core.plist"), "rb") as f:
            agent = plistlib.load(f)
        exe = os.path.join(self.state, name, "Contents", "MacOS", name[:-4])
        self.assertEqual(agent["ProgramArguments"], [
            exe, os.path.join(self.state, "app", "runtime", "python", "bin", "python3"), "-I", "-B",
            os.path.join(self.state, "app", "start.py")])
        self.assertEqual(agent["WorkingDirectory"], self.state)
        self.assertEqual(agent["EnvironmentVariables"]["HUB_STATE_DIR"], self.state)
        with open(os.path.join(self.state, "bin", "git")) as f:
            wrapper = f.read()
        self.assertIn(f"G={os.path.join(self.state, 'app', 'runtime', 'git')}\n", wrapper)
        self.assertIn('GIT_EXEC_PATH="$G/libexec/git-core"', wrapper)
        self.assertTrue(self.run_.ran("launchctl", "bootstrap"))
        # the core finds the agent as the hub's
        import platforms
        if hasattr(platforms.current, "ONELINE_START"):
            got = platforms.current.service_unit(self.state, home=self.home, platform="darwin")
            self.assertEqual(got and got["id"], "com.hub-core")


    def launchctl(self, stops_for, rejects=0, error="Bootstrap failed: 5: Input/output error\n"):
        """A launchd where the agent booted out is held for `stops_for` answers of `print` (its hub is still stopping;
        the plist's ExitTimeOut is 75 s), and a bootstrap while it is held, or the first `rejects`, says 5."""
        seen = {"out": False, "prints": 0, "bootstraps": 0}

        def run(cmd, **kw):
            if cmd[:2] == ["launchctl", "bootout"]:
                seen["out"] = True
            elif cmd[:2] == ["launchctl", "print"] and seen["out"]:
                seen["prints"] += 1
                return Recorder.__call__(self.run_, cmd, **kw) if seen["prints"] <= stops_for else \
                    subprocess.CompletedProcess(cmd, 113, "", "Could not find service")
            elif cmd[:2] == ["launchctl", "bootstrap"]:
                seen["bootstraps"] += 1
                if seen["prints"] <= stops_for or seen["bootstraps"] <= rejects:
                    Recorder.__call__(self.run_, cmd, **kw)
                    return subprocess.CompletedProcess(cmd, 5, "", error)
            return Recorder.__call__(self.run_, cmd, **kw)
        self.run_.__class__ = type("LaunchdRecorder", (Recorder,), {"__call__": lambda s, cmd, **kw: run(cmd, **kw)})
        self.run_.stage = seen

    def mac_install(self):
        real = load("hub_test_mac_app_launchd", os.path.join(ROOT, "deploy", "mac", "app.py"))
        with mock.patch.object(self.setup.Install, "mac", lambda job: real):
            return self.install()

    def test_a_hub_that_stops_slowly_is_waited_for_as_long_as_launchd_does(self):
        self.launchctl(stops_for=60)   # longer than a half minute, shorter than the 75 s before launchd kills it
        self.assertEqual(self.mac_install(), 0, self.out)
        self.assertEqual(self.run_.stage["bootstraps"], 1)   # not tried while the old agent was still there

    def test_a_bootstrap_launchd_still_refuses_is_tried_again(self):
        self.launchctl(stops_for=0, rejects=1)   # print says "gone", bootstrap says 5 once
        self.assertEqual(self.mac_install(), 0, self.out)
        self.assertEqual(self.run_.stage["bootstraps"], 2)

    def test_another_refusal_of_bootstrap_is_not_tried_again(self):
        self.launchctl(stops_for=0, rejects=99, error="Bootstrap failed: 22: Invalid argument\n")
        self.assertEqual(self.mac_install(), 1, self.out)
        self.assertEqual(self.run_.stage["bootstraps"], 1)
        self.assertIn("Invalid argument", self.out)

    def test_a_bootstrap_that_never_succeeds_is_the_error_of_the_step(self):
        self.launchctl(stops_for=0, rejects=99)
        self.assertEqual(self.mac_install(), 1, self.out)
        self.assertIn("Bootstrap failed: 5", self.out)


class Windows(Base):
    os_name = "windows"

    def test_the_task_is_install_ps1_in_its_shell_mode_with_the_policy_bypassed(self):
        with mock.patch("platforms.current.state_dir", lambda: self.state):
            self.assertEqual(self.install(), 0, self.out)
        ps = self.run_.ran("powershell.exe")[0]
        self.assertEqual(ps[ps.index("-ExecutionPolicy") + 1], "Bypass")
        self.assertEqual(ps[ps.index("-File") + 1], os.path.join(self.state, "code", "deploy", "windows", "install.ps1"))
        self.assertEqual(ps[ps.index("-App") + 1:], [os.path.join(self.state, "app"), "-Shell", "-Yes"])

    def test_a_state_folder_of_its_own_is_refused(self):
        with mock.patch("platforms.current.state_dir", lambda: os.path.join(self.tmp, "LocalAppData", "hub-core")):
            self.assertEqual(self.install(), 1)
        self.assertIn("On Windows the hub lives in", self.out)


class Shortcut(Base):
    """The menu shortcut "Hub by Icons8" (spec §0 item 9): a .desktop file on Linux, a .lnk in the Start menu on Windows,
    none on a Mac; `hub open` of its own hub; one per hub, never doubled by a run again; a file of someone else's under
    its name stays."""

    def setUp(self):
        super().setUp()
        from platforms import posix   # the menu folder of a Linux machine, on a Mac too (it has none)
        p = mock.patch.object(platforms.current, "shortcut_dir", functools.partial(posix.shortcut_dir, platform="linux"))
        p.start()
        self.addCleanup(p.stop)

    def apps(self):
        return os.path.join(self.home, ".local", "share", "applications")

    def desktop(self, name="agents-hub.desktop"):
        with open(os.path.join(self.apps(), name), encoding="utf-8") as f:
            return f.read()

    def test_linux_gets_a_desktop_file_that_opens_the_page_of_this_hub(self):
        self.assertEqual(self.install(environ={"DISPLAY": ":0"}), 0, self.out)
        text = self.desktop()
        py = os.path.join(self.state, "app", "runtime", "python", "bin", "python3")
        self.assertEqual(text.splitlines()[0], shortcut.MARK)
        for line in ("[Desktop Entry]", "Type=Application", "Name=Hub by Icons8", "Terminal=false",
                     f"Exec=env HUB_STATE_DIR={self.state} {py} -B {os.path.join(self.state, 'code', 'bin', 'hub')} open",
                     f"Icon={os.path.join(self.state, 'code', shortcut.ICON)}",
                     "Comment=A fleet of Claude Code sessions in your browser and your phone"):
            self.assertIn(line + "\n", text)
        self.assertTrue(os.path.isfile(os.path.join(self.state, "code", shortcut.ICON)))
        self.assertIn('The shortcut "Hub by Icons8" is in the applications menu', self.out)

    def test_a_machine_with_no_screen_gets_the_file_but_is_not_told_about_a_menu(self):
        # a server: the line about the applications menu would sit right above "This machine has no screen"
        self.assertEqual(self.install(), 0, self.out)
        self.assertTrue(os.path.isfile(os.path.join(self.apps(), "agents-hub.desktop")))
        self.assertNotIn("applications menu", self.out)
        self.assertIn("This machine has no screen", self.out)

    def test_a_wayland_session_has_a_menu_too(self):
        self.assertEqual(self.install(environ={"WAYLAND_DISPLAY": "wayland-0"}), 0, self.out)
        self.assertIn('The shortcut "Hub by Icons8" is in the applications menu', self.out)

    def test_a_run_again_does_not_double_it_nor_say_it_again(self):
        self.assertEqual(self.install(environ={"DISPLAY": ":0"}), 0, self.out)
        first = self.desktop()
        self.assertEqual(self.install(environ={"DISPLAY": ":0"}), 0, self.out)
        self.assertEqual(os.listdir(self.apps()), ["agents-hub.desktop"])
        self.assertEqual(self.desktop(), first)
        self.assertNotIn("applications menu", self.out)

    def test_a_file_of_someone_else_s_under_the_name_stays(self):
        write(os.path.join(self.apps(), "agents-hub.desktop"), "[Desktop Entry]\nName=Mine\n")
        self.assertEqual(self.install(), 0, self.out)
        self.assertEqual(self.desktop(), "[Desktop Entry]\nName=Mine\n")
        self.assertIn("leaving it as it is", self.out)

    def test_xdg_data_home_is_where_the_menu_entries_go(self):
        data = os.path.join(self.tmp, "xdg-data")
        self.assertEqual(self.install(environ={"XDG_DATA_HOME": data}), 0, self.out)
        self.assertTrue(os.path.isfile(os.path.join(data, "applications", "agents-hub.desktop")))
        self.assertFalse(os.path.exists(self.apps()))

    def test_a_failed_shortcut_is_a_line_and_the_install_goes_on(self):
        with mock.patch.object(shortcut, "desktop", side_effect=OSError("disk full")):
            self.assertEqual(self.install(), 0, self.out)
        self.assertIn("The menu shortcut was not made (OSError: disk full)", self.out)
        self.assertIn("The hub is running", self.out)

    def test_a_hub_left_on_its_clone_gets_none(self):
        self.run_.find = os.path.join(self.tmp, "clone")
        self.assertEqual(self.install("--stay"), 0, self.out)
        self.assertFalse(os.path.exists(self.apps()))

    def test_a_second_hub_has_its_own_named_for_it_the_main_one_stays(self):
        self.assertEqual(self.install(), 0, self.out)
        from core import instance
        anchor = instance.setup("work2", os.path.join(self.home, ".hub-work2", "code"), kind="layer",
                                default_state=self.state, default_config=self.config, platform="linux",
                                is_free=lambda p: True)
        main_state, self.state = self.state, anchor["state"]
        self.run_.calls.clear()
        self.assertEqual(self.install("--as-instance", "work2", "--handed"), 0, self.out)
        self.assertEqual(sorted(os.listdir(self.apps())), ["agents-hub-work2.desktop", "agents-hub.desktop"])
        text = self.desktop("agents-hub-work2.desktop")
        self.assertIn("Name=Hub by Icons8 (work2)\n", text)
        self.assertIn(f"Exec=env HUB_STATE_DIR={anchor['state']} HUB_CONFIG_DIR={anchor['config_dir']} ", text)
        self.assertIn(f"HUB_STATE_DIR={main_state} ", self.desktop())

    def test_the_exec_line_quotes_what_the_menu_reads_specially(self):
        text = shortcut.desktop("", ["env", "A=/home/a b/x", "/p/$HOME/50%/q\"x\\y", "open"], "c")
        self.assertIn(r'Exec=env "A=/home/a b/x" "/p/\\$HOME/50%%/q\\"x\\\\y" open' + "\n", text)   # twice escaped

    def test_the_icon_is_made_only_of_a_png_up_to_256(self):
        with open(os.path.join(ROOT, shortcut.ICON), "rb") as f:
            png = f.read()
        data = shortcut.ico(png)
        self.assertEqual(data[:6], b"\x00\x00\x01\x00\x01\x00")
        self.assertEqual(data[22:], png)
        self.assertEqual((data[6], data[7]), (192, 192))
        self.assertIsNone(shortcut.ico(b"not a picture, long enough to read a header from"))


class ShortcutWindows(Base):
    os_name = "windows"

    def setUp(self):
        super().setUp()
        self.menu = os.path.join(self.tmp, "Roaming", "StartMenuPrograms")
        for name, value in (("shortcut_dir", lambda home=None, environ=None, platform=None: self.menu),
                            ("state_dir", lambda: self.state)):
            p = mock.patch.object(platforms.current, name, value)
            p.start()
            self.addCleanup(p.stop)

    def powershell(self):
        calls = [c for c in self.run_.envs if "-EncodedCommand" in c[0]]
        self.assertEqual(len(calls), 1, self.run_.calls)
        return calls[0]

    def test_the_start_menu_gets_a_lnk_that_runs_hub_cmd_open_minimized(self):
        self.assertEqual(self.install(), 0, self.out)
        cmd, env = self.powershell()
        self.assertEqual(env["HUB_LNK"], os.path.join(self.menu, "Hub by Icons8.lnk"))
        self.assertEqual(env["HUB_LNK_ARGS"], f'/c ""{os.path.join(self.state, "bin", "hub.cmd")}" open"')
        self.assertTrue(env["HUB_LNK_TARGET"].lower().endswith("cmd.exe"))
        self.assertEqual(env["HUB_LNK_DIR"], self.home)
        self.assertEqual(env["HUB_LNK_TEXT"], "A fleet of Claude Code sessions in your browser and your phone")
        import base64
        script = base64.b64decode(cmd[cmd.index("-EncodedCommand") + 1]).decode("utf-16-le")
        self.assertEqual(script, shortcut.lnk_script())
        self.assertIn("$s.WindowStyle = 7", script)
        self.assertIn('The shortcut "Hub by Icons8" is in the Start menu', self.out)
        # its icon: an .ico of the hub's picture next to the state folder's files
        with open(env["HUB_LNK_ICON"], "rb") as f:
            self.assertEqual(f.read()[:6], b"\x00\x00\x01\x00\x01\x00")
        self.assertEqual(env["HUB_LNK_ICON"], os.path.join(self.state, "agents-hub.ico"))

    def test_a_run_again_makes_the_same_file_and_says_nothing(self):
        self.assertEqual(self.install(), 0, self.out)
        self.run_.envs.clear()
        self.assertEqual(self.install(), 0, self.out)
        self.assertEqual(os.listdir(self.menu), ["Hub by Icons8.lnk"])
        self.assertNotIn("Start menu", self.out)

    def test_a_run_again_with_the_icon_gone_makes_the_shortcut_again(self):
        self.assertEqual(self.install(), 0, self.out)
        os.remove(os.path.join(self.state, "agents-hub.ico"))
        self.run_.envs.clear()
        self.assertEqual(self.install(), 0, self.out)
        self.assertEqual(self.powershell()[1]["HUB_LNK_ICON"], os.path.join(self.state, "agents-hub.ico"))

    def test_a_lnk_of_someone_else_s_under_the_name_stays(self):
        os.makedirs(self.menu)
        with open(os.path.join(self.menu, "Hub by Icons8.lnk"), "wb") as f:
            f.write(b"another program's shortcut" + b"." * 60)
        self.assertEqual(self.install(), 0, self.out)
        self.assertEqual([c for c in self.run_.envs if "-EncodedCommand" in c[0]], [])
        with open(os.path.join(self.menu, "Hub by Icons8.lnk"), "rb") as f:
            self.assertEqual(f.read(), b"another program's shortcut" + b"." * 60)
        self.assertIn("leaving it as it is", self.out)

    def test_the_lnk_of_the_former_name_goes_once_the_new_one_is_there(self):
        # it was "Agents Hub" on Windows and "Hub by Icons8" on Linux: one name, one entry in the Start menu
        os.makedirs(self.menu)
        old = os.path.join(self.menu, "Agents Hub.lnk")
        with open(old, "wb") as f:
            f.write(b"L\x00\x00\x00" + os.path.join(self.state, "bin", "hub.cmd").encode("utf-16-le"))
        self.assertEqual(self.install(), 0, self.out)
        self.assertEqual(os.listdir(self.menu), ["Hub by Icons8.lnk"])
        self.assertIn('The shortcut "Hub by Icons8" is in the Start menu', self.out)
        with open(old, "wb") as f:   # another hub's, or another program's, under the former name stays
            f.write(b"L\x00\x00\x00" + os.path.join(self.tmp, "other", "bin", "hub.cmd").encode("utf-16-le"))
        self.assertEqual(self.install(), 0, self.out)
        self.assertEqual(sorted(os.listdir(self.menu)), ["Agents Hub.lnk", "Hub by Icons8.lnk"])

    def test_the_installers_of_earlier_releases_still_find_the_name(self):
        # their setup.py runs on this core and names the shortcut with shortcut.title (Contract, the 1.8.90 handover)
        self.assertEqual((shortcut.title(), shortcut.title("work2")), ("Hub by Icons8", "Hub by Icons8 (work2)"))

    def test_the_lnk_of_the_former_name_stays_while_the_new_one_is_not_made(self):
        os.makedirs(self.menu)
        old = os.path.join(self.menu, "Agents Hub.lnk")
        with open(old, "wb") as f:
            f.write(b"L\x00\x00\x00" + os.path.join(self.state, "bin", "hub.cmd").encode("utf-16-le"))
        self.run_.lnk_rc = 1
        self.assertEqual(self.install(), 0, self.out)
        self.assertEqual(os.listdir(self.menu), ["Agents Hub.lnk"])

    def test_a_lnk_a_stopped_powershell_left_is_made_again(self):
        os.makedirs(self.menu)
        open(os.path.join(self.menu, "Hub by Icons8.lnk"), "wb").close()
        self.assertEqual(self.install(), 0, self.out)
        self.assertEqual(self.powershell()[1]["HUB_LNK"], os.path.join(self.menu, "Hub by Icons8.lnk"))

    def test_a_picture_that_cannot_be_written_leaves_a_shortcut_without_an_icon(self):
        with mock.patch.object(shortcut, "ico", side_effect=OSError("read-only")):
            self.assertEqual(self.install(), 0, self.out)
        self.assertEqual(self.powershell()[1]["HUB_LNK_ICON"], "")
        self.assertNotIn("was not made", self.out)

    def test_a_powershell_that_fails_is_a_line_not_a_failed_install(self):
        self.run_.lnk_rc = 1
        self.assertEqual(self.install(), 0, self.out)
        self.assertIn("The menu shortcut was not made", self.out)


class ShortcutMac(Base):
    os_name = "mac"

    def test_a_mac_has_no_shortcut(self):
        with mock.patch.object(platforms.current, "shortcut_dir", lambda *a, **k: None):
            self.assertEqual(self.install(), 0, self.out)
        self.assertNotIn("shortcut", self.out)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".local", "share", "applications")))


class Report(Base):
    def test_a_failed_step_goes_to_the_team_s_sentry_with_its_name(self):
        os.chmod(self.env, 0o755)
        write(os.path.join(self.env, "deliver.json"), json.dumps({"url": "https://x.example.test", "key": KEY,
                                                                 "report": "http://k1234567@127.0.0.1:9/1"}))
        write(os.path.join(self.home, ".config", "systemd", "user", "agents-hub.service"), "[Service]\nExecStart=/x\n")
        from modules.update import report
        sent = []
        with mock.patch.object(report, "_post", lambda where, body: sent.append(body) or "sent"):
            self.assertEqual(self.install(), 1, self.out)
        self.assertEqual(len(sent), 1)
        self.assertIn(b'"step": "install.service"', sent[0])
        self.assertNotIn(KEY.encode(), sent[0])   # the build key never goes into a report


class WindowsLauncher(unittest.TestCase):
    def test_the_shell_mode_launcher_prepares_by_start_py(self):
        win = load("hub_test_windows_setup", os.path.join(ROOT, "deploy", "windows", "setup.py"))
        tmp = tempfile.mkdtemp()
        self.addCleanup(platforms.current.rmtree, tmp)
        app = os.path.join(tmp, "state", "app")
        write(os.path.join(app, "paths.json"), json.dumps({"python": "runtime/python/python.exe"}))
        _, launcher = win.scripts(os.path.join(tmp, "state", "code"), os.path.join(tmp, "state"), "py.exe", app=app,
                                  shell=True)
        with open(launcher, encoding="utf-8", newline="") as f:
            text = f.read()
        self.assertIn(f'"py.exe" -I -B "{app}\\start.py" --prepare\r\n', text)
        self.assertNotIn("app.py", text)
        self.assertLess(text.index("start.py"), text.index("hub-up"))


class Open(unittest.TestCase):
    def test_hub_open_without_a_service_and_a_page_names_how_to_start(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(platforms.current.rmtree, tmp)
        env = {k: v for k, v in os.environ.items() if k not in ("HUB_SESSION", "HUB_EXTENSION", "HUB_MODULE")}
        env.update(HUB_STATE_DIR=tmp, HUB_WEB_PORT="9", HOME=tmp, HUB_LANG="en")
        r = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "hub"), "open", "--print"], env=env,
                           capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("there is no hub service", r.stderr)

    @unittest.skipUnless(sys.platform.startswith("linux"), "a Linux without a display: elsewhere hub ui opens a browser")
    def test_hub_ui_on_a_server_prints_the_link_and_the_ssh_tunnel_to_it(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(platforms.current.rmtree, tmp)
        env = {k: v for k, v in os.environ.items()
               if k not in ("HUB_SESSION", "HUB_EXTENSION", "HUB_MODULE", "DISPLAY", "WAYLAND_DISPLAY", "SSH_CONNECTION")}
        env.update(HUB_STATE_DIR=tmp, HUB_CONFIG_DIR=tmp, HUB_WEB_PORT="8790", HUB_FILES_PORT="8791", HOME=tmp,
                   HUB_LANG="en")
        r = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "hub"), "ui"], env=env, capture_output=True,
                           text=True, timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertEqual(lines[0], "http://localhost:8790/")
        self.assertIn("no screen", lines[1])
        self.assertTrue(lines[2].startswith("  ssh -N -L 8790:127.0.0.1:8790 -L 8791:127.0.0.1:8791 "), lines)
        self.assertTrue(lines[2].endswith("@<server address>"), lines)
        r = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "hub"), "ui", "--print"], env=env,
                           capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual(r.stdout, "http://localhost:8790/\n")   # --print: the link alone, as scripts read it


class Programs(unittest.TestCase):
    def test_refresh_keeps_the_owner_s_and_takes_the_environment_s(self):
        setup = load("hub_test_oneline_setup3", SETUP)
        tmp = tempfile.mkdtemp()
        self.addCleanup(platforms.current.rmtree, tmp)
        shells = os.path.join(tmp, "shells")
        old, new = os.path.join(shells, "0-team"), os.path.join(shells, "1-team")
        for d in (old, new):
            write(os.path.join(d, "ffmpeg"), "")
        path = os.path.join(tmp, "hub.json")
        mine = os.path.join(tmp, "my-ffmpeg")
        write(mine, "")
        for have, want in ((os.path.join(old, "ffmpeg"), "NEW"), (mine, mine), (None, "NEW"),
                           (os.path.join(tmp, "nowhere"), "NEW"), ("ffmpeg", "ffmpeg")):
            with open(path, "w") as f:
                json.dump({"voice": {"ffmpeg": have}} if have else {}, f)
            setup.refresh_programs(path, {"ffmpeg": "NEW"}, new, shells)
            with open(path) as f:
                self.assertEqual(json.load(f)["voice"]["ffmpeg"], want, have)


class Bootstrap(unittest.TestCase):
    """tools/package/install.py: the environment's own steps once, then the newest setup.py there is."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hub-oneline-boot-")
        self.addCleanup(self._clean)
        self.env = environment(self.tmp)
        self.state = os.path.join(self.tmp, "state")
        self.boot = load("hub_test_oneline_install", os.path.join(self.env, "install.py"))

    def _clean(self):
        for d, dirs, files in os.walk(self.tmp):
            for n in dirs + files:
                if not os.path.islink(os.path.join(d, n)):
                    os.chmod(os.path.join(d, n), 0o700)
        platforms.current.rmtree(self.tmp)

    def current(self, version, edit=False):
        layers = os.path.join(self.state, "layers")
        layer(os.path.join(layers, f"{version}-team"), version)
        appcode.point(os.path.join(self.state, "code"), layers, f"{version}-team")
        if edit:
            path = os.path.join(layers, f"{version}-team", "README.md")
            os.chmod(path, 0o644)
            with open(path, "a") as f:
                f.write("x\n")
        return os.path.join(layers, f"{version}-team")

    def test_the_newest_unedited_code_installs(self):
        seed = os.path.join(self.env, "code-seed")
        self.assertEqual(self.boot.code_to_run(self.state, self.env), seed)          # no hub yet
        newer = self.current("9.9.5")
        self.assertEqual(self.boot.code_to_run(self.state, self.env), newer)
        self.current("9.9.0")
        self.assertEqual(self.boot.code_to_run(self.state, self.env), seed)          # older than the seed
        self.current("9.9.6", edit=True)
        self.assertEqual(self.boot.code_to_run(self.state, self.env), seed)          # edited: the setup.py puts it back

    def test_the_environment_is_sealed_once_and_the_arguments_go_on(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 7, "", "")
        argv = ["--contract", "1", "--env", self.env, "--state", self.state, "--no-input"]
        self.assertEqual(self.boot.main(argv, run=run), 7)
        self.assertTrue(os.path.isfile(os.path.join(self.env, self.boot.SEALED)))
        self.assertEqual(calls[-1][-len(argv):], argv)
        self.assertEqual(calls[-1][-len(argv) - 1], os.path.join(self.env, "code-seed", "deploy", "oneline", "setup.py"))
        if os.name != "nt":   # Windows keeps a folder's write bit (its read-only attribute means nothing there)
            self.assertFalse(os.stat(self.env).st_mode & 0o222)
            self.assertFalse(os.stat(os.path.join(self.env, "start.py")).st_mode & 0o222)
        calls.clear()
        self.assertEqual(self.boot.main(argv, run=run), 7)
        self.assertFalse([c for c in calls if "compileall" in c])   # sealed: once

    def test_another_contract_is_refused(self):
        self.assertEqual(self.boot.main(["--contract", "2", "--env", self.env], run=None), 64)


class Start(unittest.TestCase):
    """tools/package/start.py: the layer of a start, the key of the line kept, the environment first in PATH."""

    def test_a_start_keeps_the_installer_s_key_and_puts_the_programs_first(self):
        tmp = tempfile.mkdtemp(prefix="hub-oneline-start-")
        self.addCleanup(platforms.current.rmtree, tmp)
        env = environment(tmp)
        state = os.path.join(tmp, "state")
        layers = os.path.join(state, "layers")
        os.makedirs(layers)
        appcode.point(os.path.join(state, "app"), os.path.dirname(env), os.path.basename(env))
        write(os.path.join(layers, "deliver.json"), json.dumps({"url": "https://x.example.test", "key": KEY}))
        write(os.path.join(env, "deliver.json"), json.dumps({"url": "https://y.example.test", "key": "k" * 20,
                                                            "report": "https://k1234567@sentry.example.test/1"}))
        start = load("hub_test_oneline_start", os.path.join(state, "app", "start.py"))
        execs = []
        # (on Windows hub-up.cmd sets PATH, not start.py: its logic is the same everywhere)
        with mock.patch.dict(os.environ, {"HUB_STATE_DIR": state, "PATH": "/usr/bin"}), \
                mock.patch.object(start, "WINDOWS", False), mock.patch.object(start.launch, "detached", lambda cmd: None):
            self.assertEqual(start.main([], execve=lambda *a: execs.append(a)), 0)
            path = os.environ["PATH"].split(os.pathsep)
        self.assertEqual([os.path.normpath(p) for p in path[:2]],
                         [os.path.join(state, "bin"), os.path.join(state, "app", "runtime", "node", "bin")])
        self.assertEqual(execs[0][1][-1], os.path.join(state, "code", "bin", "hub-up"))
        with open(os.path.join(layers, "deliver.json")) as f:
            self.assertEqual(json.load(f), {"url": "https://x.example.test", "key": KEY,
                                            "report": "https://k1234567@sentry.example.test/1"})
        self.assertEqual(appcode.current(os.path.join(state, "code"), layers), "9.9.1-team")


if __name__ == "__main__":
    unittest.main()
