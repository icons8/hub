"""During the update's self-check (~8 min) the previous page server is still running while the new files are already on disk:
it imports modules lazily (modules.gateway, modules.accounts, ...) and calls them as it did before. A function removed from
a module on disk broke the page for the whole window: "No connection to the hub" (v1.4.0 server + gateway without
page_view). The test starts the page server of the last release on the current tree and asks for the page's snapshot."""
import tests  # noqa: F401 — keep the machine's settings out (~/.config/hub-core), see tests/__init__.py
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from core import cutout  # noqa: E402 — the open flavor's run has no built-in gateway (core/cutout.py)
gw = None
if cutout.present("gateway"):
    from modules.gateway import gateway as gw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEEP = ("core", "modules", "platforms", "i18n", "hosts", "hooks", "hub-home", "observers", "plugins", "bin", "embed")


def last_release():
    """The newest vX.Y.Z tag of this clone: the page server that ran during the update is the one of the release the
    hub was on (a hardcoded number goes stale at every release). None: a clone without release tags."""
    try:
        tags = subprocess.run(["git", "tag", "--list", "v[0-9]*", "--sort=-v:refname"], cwd=ROOT, check=True,
                              capture_output=True, text=True).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        return None
    return next((t for t in tags if re.fullmatch(r"v\d+\.\d+\.\d+", t)), None)


OLD = last_release()   # the release whose page server ran during the update



def old_server_source():
    if OLD is None:
        return None
    try:
        return subprocess.run(["git", "show", f"{OLD}:modules/web/server.py"], cwd=ROOT, check=True,
                              capture_output=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None   # a clone without the release history (the published snapshot): only the unit test runs


@unittest.skipUnless(gw, "the built-in gateway is cut out of this tree")
class PageView(unittest.TestCase):
    """What the previous page server asks of modules.gateway: gw.page_view(cfg, state_dir, now, core) -> the "gateway"
    block of Settings -> Accounts."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hubgw-")
        self.addCleanup(shutil.rmtree, self.state, True)

    def test_page_view_keeps_its_shape(self):
        off = gw.page_view({}, self.state, 100, None)
        self.assertEqual(set(off), {"enabled", "level", "text"})
        self.assertIs(off["enabled"], False)
        self.assertEqual(off["level"], "off")
        on = gw.page_view({"gateway": {"enabled": True}}, self.state, 100, {"on": True})
        self.assertIs(on["enabled"], True)
        self.assertIn(on["level"], ("ok", "warn", "bad"))
        self.assertTrue(on["text"])


@unittest.skipIf(old_server_source() is None, f"no release tag with a page server in this clone ({OLD})")
class OldServerOnNewTree(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hubold-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.tree = os.path.join(self.tmp, "tree")
        os.makedirs(self.tree)
        for name in KEEP:
            if os.path.isdir(os.path.join(ROOT, name)):
                shutil.copytree(os.path.join(ROOT, name), os.path.join(self.tree, name),
                                ignore=shutil.ignore_patterns("__pycache__"))
        with open(os.path.join(self.tree, "modules", "web", "server.py"), "wb") as f:
            f.write(old_server_source())
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        state, cfg = os.path.join(self.tmp, "state"), os.path.join(self.tmp, "cfg")
        os.makedirs(state)
        os.makedirs(cfg)
        self.log = open(os.path.join(self.tmp, "log"), "w+b")
        self.addCleanup(self.log.close)
        env = {**os.environ, "HUB_STATE_DIR": state, "HUB_CONFIG_DIR": cfg, "HUB_WEB_PORT": str(self.port),
               "PYTHONDONTWRITEBYTECODE": "1"}
        env.pop("HUB_SESSION", None)
        self.proc = subprocess.Popen([sys.executable, os.path.join("modules", "web", "server.py")], cwd=self.tree,
                                     env=env, stdout=self.log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        self.addCleanup(self.stop)

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()

    def get(self, path):
        end = time.time() + 60
        while time.time() < end:
            try:
                c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
                c.request("GET", path, headers={"Host": f"127.0.0.1:{self.port}"})
                try:
                    r = c.getresponse()
                    return r.status, r.read()
                finally:
                    c.close()
            except ConnectionRefusedError:   # not listening yet
                if self.proc.poll() is not None:
                    break
                time.sleep(0.2)
            except (OSError, http.client.HTTPException):   # the handler died on the request: the log says why
                break
        self.log.seek(0)
        self.fail(f"no answer for {path}: {self.log.read().decode(errors='replace')[-2000:]}")

    def test_the_page_answers(self):
        code, body = self.get("/api/snapshot")
        self.log.seek(0)
        self.assertEqual(code, 200, self.log.read().decode(errors="replace")[-2000:])
        snap = json.loads(body)
        self.assertIn("gateway", (snap.get("accounts") or {}))
        for path in ("/api/about", "/api/modules", "/api/usage", "/api/flow", "/api/plugins/updates", "/"):
            self.assertEqual(self.get(path)[0], 200, path)   # the other modules it imports from disk answer as before

    def test_it_serves_the_clean_characters(self):
        """Clean's characters (modules/web/static/clean/faces.js) are files of the page that the page server of the last release
        serves too, as it did not know .jpg then: a 404 for them is an empty frame in every list row and feed byline until the
        hub is restarted (the new page, the old server)."""
        with open(os.path.join(ROOT, "modules", "web", "static", "clean", "faces.js"), encoding="utf-8") as f:
            names = set(re.findall(r'"([\w-]+\.(?:png|jpg|jpeg|webp|gif))"', f.read()))
        self.assertGreaterEqual(len(names), 24)
        for name in sorted(names):
            code, body = self.get("/static/clean/a/" + name)
            self.assertEqual(code, 200, name)
            self.assertTrue(body.startswith(b"\x89PNG\r\n\x1a\n"), f"{name} is not a png the old server could type")


class Fallbacks(unittest.TestCase):
    """Fallback names for a process of the last release: getattr(<hub module>, "NAME", <fallback>) anywhere in the tree.
    It only covers a name the old module in memory lacks; in a new process the real one is taken. So a fallback for a
    core module is its current value (it never decides differently); a fallback for platforms.current is what the last
    release did on every OS — it had no such name at all (PLATFORM below), and the test does not depend on this OS."""
    SOURCES = ("core", "modules", "hosts", "platforms", "observers", "hooks", "plugins", "bin")
    PLATFORM = {"FORWARDS_FOLDERS": "False", "file_modes": "lambda: False", "no_terminal": "lambda: {}",
                "has_exec_bit": "lambda p: False"}

    def fallbacks(self):
        """[(file:line, module name or "platforms.current", NAME, fallback node)] over the whole tree."""
        import ast
        out = []
        for top in self.SOURCES:
            for d, dirs, files in os.walk(os.path.join(ROOT, top)):
                dirs[:] = [x for x in dirs if x not in ("__pycache__", "node_modules")]
                for f in files:
                    path = os.path.join(d, f)
                    if not f.endswith(".py"):
                        if top != "bin":
                            continue
                        with open(path, "rb") as fh:
                            first = fh.readline()
                        if not (first.startswith(b"#!") and b"python" in first):
                            continue
                    with open(path, encoding="utf-8") as fh:
                        tree = ast.parse(fh.read())
                    alias = {}
                    for n in ast.walk(tree):
                        if isinstance(n, ast.Import):
                            for a in n.names:
                                alias[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
                        elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
                            for a in n.names:
                                alias[a.asname or a.name] = f"{n.module}.{a.name}"
                    for n in ast.walk(tree):
                        if not (isinstance(n, ast.Call) and getattr(n.func, "id", None) == "getattr"
                                and len(n.args) == 3 and isinstance(n.args[1], ast.Constant)):
                            continue
                        owner = ast.unparse(n.args[0])
                        head, _, rest = owner.partition(".")
                        mod = alias.get(head, "") + ("." + rest if rest else "")
                        if mod != "platforms.current" and not mod.startswith(("core.", "modules.", "hosts.")):
                            continue   # an object, not a hub module
                        out.append((f"{os.path.relpath(path, ROOT)}:{n.lineno}", mod, n.args[1].value, n.args[2]))
        return out

    def test_each_fallback_repeats_what_it_stands_for(self):
        import ast
        import importlib
        found = self.fallbacks()
        self.assertTrue(found)
        for where, mod, name, node in found:
            if mod == "platforms.current":
                self.assertEqual(ast.unparse(node), self.PLATFORM.get(name), f"{where}: {name} not in PLATFORM")
                continue
            module = importlib.import_module(mod)
            self.assertTrue(hasattr(module, name), f"{where}: {mod} has no {name}")
            try:
                got = ast.literal_eval(node)
            except ValueError:   # an expression (a compiled pattern): evaluated where it stands
                home = importlib.import_module(where.rsplit(":", 1)[0][:-3].replace(os.sep, "."))
                got = eval(compile(ast.Expression(node), where, "eval"), vars(home))
            self.assertEqual(got, getattr(module, name), f"{where}: {mod}.{name}")

    def test_an_old_adapter_does_not_make_plugin_data_planted(self):
        # codestore._planted under the platforms of v1.4.0 (no has_exec_bit): a data file with the X bit is not code -
        # _heal wipes "planted" files, and on Windows os.access(X_OK) holds for every file (review: data loss)
        import platforms
        import stat
        import types
        from unittest import mock
        from core import codestore
        folder = tempfile.mkdtemp(prefix="hubplant-")
        self.addCleanup(shutil.rmtree, folder, True)
        path = os.path.join(folder, "notes.txt")
        with open(path, "w") as f:
            f.write("data")
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        with mock.patch.object(platforms, "current", types.SimpleNamespace()):
            self.assertFalse(codestore._planted("notes.txt", path))

    def test_platform_names_exist_on_every_os(self):
        # the fallback is for the old adapter only: both adapters of this release have the name
        from platforms import posix, windows
        used = {name for _, mod, name, _ in self.fallbacks() if mod == "platforms.current"}
        self.assertEqual(used, set(self.PLATFORM))
        for name in self.PLATFORM:
            self.assertTrue(hasattr(posix, name) and hasattr(windows, name), name)


@unittest.skipIf(old_server_source() is None, f"no release tag with a page server in this clone ({OLD})")
class OldModulesInMemory(OldServerOnNewTree):
    """As at a colleague's: the previous updater merges the release into the live tree while the page server of the
    last release runs with its own core.* already imported, and what it imports lazily comes from the new files (the
    new modules/watch/watch.py took SLEEP from the old core.machines: ImportError on every snapshot, "No connection"
    for the whole self-check). cold: the merge before the server's first request (the update starts with the hub);
    warm: after it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hubold-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.tree = os.path.join(self.tmp, "tree")
        os.makedirs(self.tree)
        archive = subprocess.run(["git", "archive", OLD, "--", *[n for n in KEEP if self.in_release(n)]], cwd=ROOT,
                                 check=True, capture_output=True).stdout
        subprocess.run(["tar", "-x", "-C", self.tree], input=archive, check=True)
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        state, cfg = os.path.join(self.tmp, "state"), os.path.join(self.tmp, "cfg")
        os.makedirs(state)
        os.makedirs(cfg)
        self.log = open(os.path.join(self.tmp, "log"), "w+b")
        self.addCleanup(self.log.close)
        env = {**os.environ, "HUB_STATE_DIR": state, "HUB_CONFIG_DIR": cfg, self.PORT_ENV: str(self.port),
               "PYTHONDONTWRITEBYTECODE": "1"}
        env.pop("HUB_SESSION", None)
        self.proc = subprocess.Popen([sys.executable, *self.CMD], cwd=self.tree,
                                     env=env, stdout=self.log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        self.addCleanup(self.stop)
        end = time.time() + 60
        while b"127.0.0.1" not in self.read_log() and time.time() < end and self.proc.poll() is None:
            time.sleep(0.1)
        time.sleep(1)   # its background readers start: what they import at start is the release's own

    @staticmethod
    def in_release(name):
        return subprocess.run(["git", "cat-file", "-e", f"{OLD}:{name}"], cwd=ROOT, capture_output=True).returncode == 0

    def read_log(self):
        self.log.seek(0)
        return self.log.read()

    def merge(self):
        """The tree becomes the current one, as after git merge: removed files go, new ones come; the process keeps
        what it has imported."""
        for name in KEEP:
            path = os.path.join(self.tree, name)
            if os.path.isdir(path):
                shutil.rmtree(path)
            if os.path.isdir(os.path.join(ROOT, name)):
                shutil.copytree(os.path.join(ROOT, name), path, ignore=shutil.ignore_patterns("__pycache__"))

    def answers(self, paths):
        for path in paths:
            code, body = self.get(path)
            self.assertEqual(code, 200, f"{path}: {self.read_log().decode(errors='replace')[-2000:]}")
            if path == "/api/snapshot":
                self.assertIn("sessions", json.loads(body))

    CMD, PORT_ENV = (os.path.join("modules", "web", "server.py"),), "HUB_WEB_PORT"
    PATHS = ("/api/snapshot", "/api/about", "/api/modules", "/api/usage", "/api/flow", "/api/plugins/updates", "/")
    FIRST = "/api/snapshot"   # what the warm server has answered before the merge

    def test_the_page_answers(self):   # cold
        self.merge()
        self.answers(self.PATHS)

    def test_a_warm_server_answers(self):
        self.answers((self.FIRST,))
        self.merge()
        self.answers(self.PATHS)

    def test_it_serves_the_clean_characters(self):
        self.merge()   # the files come with the merge
        super().test_it_serves_the_clean_characters()


@unittest.skipIf(old_server_source() is None, f"no release tag with a page server in this clone ({OLD})")
class OldFilesInMemory(OldModulesInMemory):
    """The same for "Files" of the last release: its themes come from the page server module (modules.web.server)."""
    CMD, PORT_ENV = (os.path.join("bin", "hub-files"),), "HUB_FILES_PORT"
    PATHS = ("/", "/api/themes", "/api/sessions")
    FIRST = "/"

    @unittest.skip("the page's own files are not served by Files")
    def test_it_serves_the_clean_characters(self):
        pass


if __name__ == "__main__":
    unittest.main()
