"""Marketplaces, slice S1 "format and storage" (core/marketplace.py, core/codestore.py): the catalog file
hub-marketplace.json and its validator (broken entries are skipped one by one, the catalog as a whole does not fail), the
catalog on disk — a bare clone and a file read from git objects, an entry's code is installed exactly at the pinned sha,
the code store of modules and plugins — one code with two roots, cleanup keeps everything that hub.json and live
processes refer to, git never asks for a password."""
import tests  # noqa: F401 — do not see the machine's settings
import contextlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import platforms
from core import codestore, marketplace, modsrc
from tests import winsafe
from tests.test_modapi_sources import Hub, Source, git, manifest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB = os.path.join(REPO, "bin", "hub")
SHA = "3f1c" + "0" * 36
SHA2 = "a" * 40


def entry(name="x", **extra):
    return {"name": name, "source": {"github": "alice/hub-plugin-x", "sha": SHA}, **extra}


def catalog(*entries, **top):
    return {"name": "alice", "plugins": list(entries), **top}


@contextlib.contextmanager
def spying():
    """The store's git commands — as the OS runs them (with the flags codestore.git adds)."""
    calls, real = [], platforms.current.run

    def spy(args, *a, **kw):
        if args[:1] == ["git"]:
            calls.append([str(x) for x in args])
        return real(args, *a, **kw)
    with mock.patch.object(platforms.current, "run", spy):
        yield calls


def listed(folder):
    """Folders without the service ones (the command lock)."""
    return sorted(n for n in os.listdir(folder) if not n.startswith("."))


class Validator(unittest.TestCase):
    def test_a_good_catalog_reads_every_kind_of_entry_and_ignores_unknown_fields(self):
        got = marketplace.parse(catalog(
            entry("unix-terminal", version="1.15.0", description="Theme", category="themes", tags=["theme"],
                  author="alice", icon="icons/unix-terminal.svg", hubs=7, rights={"page": "full"}, future=1),
            {"name": "notes", "type": "module",
             "source": {"git": "https://example.org/bob/hub-modules.git", "sha": SHA2, "subdir": "notes", "ref": "v1"}},
            {"name": "dark", "source": "./plugins/dark"},
            {"name": "root", "source": "./"},
            description="Plugins", owner={"name": "Alice", "url": "https://example.org/alice"}, hub="^1.0", later={}))
        self.assertEqual((got["name"], got["description"], got["owner"]["name"]), ("alice", "Plugins", "Alice"))
        self.assertEqual((got["skipped"], got["warnings"]), ([], []))
        e = {x["name"]: x for x in got["entries"]}
        self.assertEqual(list(e), ["unix-terminal", "notes", "dark", "root"])
        t = e["unix-terminal"]
        self.assertEqual(t["source"], {"kind": "github", "github": "alice/hub-plugin-x",
                                       "url": "https://github.com/alice/hub-plugin-x.git", "sha": SHA})
        self.assertEqual((t["type"], t["pinned"], t["version"], t["tags"], t["hubs"], t["icon"]),
                         ("plugin", True, "1.15.0", ["theme"], 7, "icons/unix-terminal.svg"))
        self.assertNotIn("future", t)
        self.assertEqual(e["notes"]["source"], {"kind": "git", "url": "https://example.org/bob/hub-modules.git",
                                                "sha": SHA2, "ref": "v1", "subdir": "notes"})
        self.assertEqual(e["notes"]["type"], "module")
        self.assertEqual((e["dark"]["source"], e["dark"]["pinned"]), ({"kind": "path", "path": "plugins/dark"}, True))
        self.assertEqual(e["root"]["source"], {"kind": "path", "path": ""})

    def test_broken_entries_are_skipped_one_by_one_with_a_reason(self):
        bad = {
            "#2": ("not an object", "x"),
            "#3": ("name", {"source": "./a"}),
            "Big": ("name", {"name": "Big", "source": "./a"}),
            "twice": ("already in", {"name": "twice", "source": "./b"}),
            "kind": ("type", {"name": "kind", "type": "theme", "source": "./a"}),
            "nosrc": ("source", {"name": "nosrc"}),
            "noprefix": ("./", {"name": "noprefix", "source": "plugins/x"}),
            "up": ("..", {"name": "up", "source": "./../x"}),
            "up2": ("..", {"name": "up2", "source": "./a/../../x"}),
            "abs": ("./", {"name": "abs", "source": "/etc"}),
            "drive": ("C:", {"name": "drive", "source": "./C:/x"}),
            "back": ("..", {"name": "back", "source": ".\\..\\x"}),
            "gh": ("github", {"name": "gh", "source": {"github": "alice", "sha": SHA}}),
            "both": ("one of", {"name": "both", "source": {"github": "a/b", "git": "https://x.org/a.git", "sha": SHA}}),
            "local": ("local path", {"name": "local", "source": {"git": "/home/x/repo", "sha": SHA}}),
            "fileurl": ("local path", {"name": "fileurl", "source": {"git": "file:///home/x/repo", "sha": SHA}}),
            "dash": ("address", {"name": "dash", "source": {"git": "-uhttps://x.org/a.git", "sha": SHA}}),
            "sha": ("sha", {"name": "sha", "source": {"github": "a/b", "sha": "abc123"}}),
            "ref": ("ref", {"name": "ref", "source": {"github": "a/b", "sha": SHA, "ref": "--upload-pack=x"}}),
            "sub": ("subdir", {"name": "sub", "source": {"github": "a/b", "sha": SHA, "subdir": "../x"}}),
            "obj": ("source", {"name": "obj", "source": 5}),
            "helper": ("https://", {"name": "helper", "source": {"git": "gcrypt::https://x.org/a.git", "sha": SHA}}),
            "fd": ("https://", {"name": "fd", "source": {"git": "fd::3", "sha": SHA}}),
        }
        entries = [entry("good"), "x", {"source": "./a"}] + [v[1] for v in bad.values() if not isinstance(v[1], str)
                                                              and v[1].get("name")]
        entries.insert(3, {"name": "twice", "source": "./a"})
        got = marketplace.parse(catalog(*entries))
        self.assertEqual([e["name"] for e in got["entries"]], ["good", "twice"])
        why = {s["entry"]: s["why"] for s in got["skipped"]}
        for label, (word, _) in bad.items():
            with self.subTest(label):
                self.assertIn(label, why)
                self.assertIn(word, why[label])

    def test_an_external_entry_without_sha_is_shown_unpinned_and_refused_by_the_strict_check(self):
        e = {"name": "loose", "source": {"github": "alice/hub-plugin-x"}}
        got = marketplace.parse(catalog(e))
        (only,) = got["entries"]
        self.assertFalse(only["pinned"])
        self.assertIn("not pinned", " ".join(only["warnings"]))
        strict = marketplace.parse(catalog(e), strict=True)
        self.assertEqual(strict["entries"], [])
        self.assertIn("sha", strict["skipped"][0]["why"])

    def test_an_unpinned_entry_needs_an_encrypted_transport(self):
        for url in ("http://x.org/a.git", "git://x.org/a"):
            with self.subTest(url):
                got = marketplace.parse(catalog({"name": "e", "source": {"git": url}}))
                self.assertEqual(got["entries"], [])
                self.assertIn("sha", got["skipped"][0]["why"])
                pinned = marketplace.parse(catalog({"name": "e", "source": {"git": url, "sha": SHA}}))
                self.assertEqual(pinned["skipped"], [])                         # with a sha, a substitution won't pass

    def test_wrong_display_fields_are_dropped_and_the_entry_stays(self):
        got = marketplace.parse(catalog(entry(
            icon="https://example.org/x.svg", tags="theme", hubs=-1, version=3, revoked="", rights=[1], author={})))
        (e,) = got["entries"]
        for key in ("icon", "tags", "hubs", "version", "revoked", "rights", "author"):
            with self.subTest(key):
                self.assertNotIn(key, e)
                self.assertIn(key, " ".join(e["warnings"]))
        self.assertEqual(marketplace.parse(catalog(entry(hubs=True)))["entries"][0].get("hubs"), None)
        self.assertEqual(marketplace.parse(catalog(entry(revoked="dangerous")))["entries"][0]["revoked"], "dangerous")

    def test_a_catalog_that_cannot_be_read_fails_whole(self):
        for label, data in (("not a JSON object", [1]), ("name", {"plugins": []}), ("name", {"name": "Alice", "plugins": []}),
                            ("plugins", {"name": "alice"}), ("plugins", {"name": "alice", "plugins": {}}),
                            ("API", {"name": "alice", "plugins": [], "hub": "^9.0"})):
            with self.subTest(label), self.assertRaises(marketplace.Invalid) as e:
                marketplace.parse(data)
            self.assertIn(label, str(e.exception))
        with self.assertRaises(marketplace.Invalid) as e:
            marketplace.loads(b'{"name": "alice", "plugins": [')
        self.assertIn("JSON", str(e.exception))
        with self.assertRaises(marketplace.Invalid) as e:
            marketplace.loads(b" " * (marketplace.MAX_FILE + 1))
        self.assertIn("MB", str(e.exception))
        newer = marketplace.parse({"name": "alice", "plugins": [], "hub": "^1.999"})
        self.assertIn("1.999", " ".join(newer["warnings"]))

    def test_remote_addresses_a_catalog_may_point_to(self):
        for url in ("https://x.org/a.git", "http://x.org/a.git", "ssh://git@x.org/a.git", "git://x.org/a",
                    "git@x.org:a/b.git"):
            with self.subTest(url):
                self.assertEqual(marketplace.parse(catalog({"name": "e", "source": {"git": url, "sha": SHA}}))["skipped"],
                                 [])

    def test_windows_device_names_are_not_catalog_names(self):
        for name in ("con", "nul", "aux", "prn", "com1", "lpt9"):
            with self.subTest(name), self.assertRaises(marketplace.Invalid) as e:
                marketplace.parse({"name": name, "plugins": []})
            self.assertIn("Windows", str(e.exception))
        self.assertEqual(marketplace.parse({"name": "console", "plugins": []})["name"], "console")

    def test_official_and_lookalike_names_are_only_for_the_official_source(self):
        for name in ("agents-hub", "official", "hub", "hub-marketplace", "agents-hub-plugins", "agentshub-official",
                     "hub-official", "official-agents", "agents-official-x"):
            with self.subTest(name):
                self.assertTrue(marketplace.reserved(name))
                with self.assertRaises(marketplace.Invalid) as e:
                    marketplace.parse({"name": name, "plugins": []})
                self.assertIn("official", str(e.exception))
                self.assertEqual(marketplace.parse({"name": name, "plugins": []}, official=True)["name"], name)
        for name in ("hub-stuff", "my-hub", "alice", "agents", "officially-cats"):
            with self.subTest(name):
                self.assertIsNone(marketplace.reserved(name))
        with self.assertRaises(marketplace.Invalid) as e:   # a lookalike letter from another alphabet is a forgery, even from the official source
            marketplace.parse({"name": "\u0430gents-hub", "plugins": []}, official=True)
        self.assertIn("ASCII", str(e.exception))


class ValidateFolder(unittest.TestCase):
    """`hub marketplace validate <folder>` and `python3 -m core.marketplace validate` — for the catalog author and CI."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="market-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def write(self, rel, body):
        path = os.path.join(self.dir, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body if isinstance(body, str) else json.dumps(body, ensure_ascii=False))

    def run_cli(self, *args, module=False):
        cmd = [sys.executable, "-m", "core.marketplace"] if module else [sys.executable, HUB, "marketplace"]
        env = {**os.environ, "HUB_STATE_DIR": self.dir, "HUB_CLI_WAIT": "0"}
        return subprocess.run([*cmd, "validate", *args], cwd=REPO, env=env, capture_output=True, text=True)

    def test_a_good_folder_passes_both_ways(self):
        self.write("hub-marketplace.json", catalog(entry("x", icon="icons/x.svg"),
                                                   {"name": "dark", "source": "./plugins/dark"}))
        self.write("icons/x.svg", "<svg/>")
        self.write("plugins/dark/hub-plugin.json", {"name": "dark", "themes": ["dark.css"]})
        for module in (False, True):
            with self.subTest(module=module):
                r = self.run_cli(self.dir, module=module)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn("2", r.stdout)

    def test_problems_are_listed_and_fail_the_check(self):
        self.write("hub-marketplace.json", catalog(
            entry("x", icon="icons/none.svg"), {"name": "gone", "source": "./plugins/gone"},
            {"name": "empty", "source": "./plugins/empty"}, {"name": "up", "source": "./../x"},
            {"name": "long", "source": "./plugins/long"}))
        self.write("plugins/empty/readme.md", "x")
        self.write("plugins/long/hub-plugin.json", {"name": "long", "web": "main.js"})
        self.write("plugins/long/" + "d" * 80 + "/" + "f" * 80 + ".js", "x")
        r = self.run_cli(self.dir)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 1, out)
        for word in ("gone", "empty", "hub-plugin.json", "up", ".."):
            self.assertIn(word, out)
        self.assertIn("150", out)                      # a long path is a warning (Windows)
        self.assertIn("none.svg", out)                 # the icon is not in the catalog
        self.write("hub-marketplace.json", catalog({"name": "long", "source": "./plugins/long"}))
        self.assertEqual(self.run_cli(self.dir).returncode, 0)      # warnings are not an error…
        self.assertEqual(self.run_cli(self.dir, "--strict").returncode, 1)   # …except with --strict
        os.unlink(os.path.join(self.dir, "hub-marketplace.json"))
        r = self.run_cli(self.dir)
        self.assertEqual(r.returncode, 1)
        self.assertIn(marketplace.FILE, r.stdout + r.stderr)

    def test_a_reserved_name_is_noted_not_failed(self):
        self.write("hub-marketplace.json", {"name": "agents-hub", "plugins": [entry("x")]})
        r = self.run_cli(self.dir, "--strict")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("official", r.stdout)


class Catalog:
    """The catalog author's repo: a bare repo the hub takes hub-marketplace.json from."""

    def __init__(self, case, data, files=None):
        self.src = Source(case, {marketplace.FILE: data, **(files or {})})
        self.bare = self.src.bare

    def commit(self, data=None, files=None):
        return self.src.commit({**({marketplace.FILE: data} if data is not None else {}), **(files or {})})


class OnDisk(unittest.TestCase):
    def setUp(self):
        self.hub = Hub(self, {})

    def test_a_catalog_is_a_bare_clone_and_its_file_comes_from_git_objects(self):
        cat = Catalog(self, catalog(entry("x")))
        got = marketplace.fetch(self.hub.state, cat.bare)
        first = git(cat.src.work, "rev-parse", "HEAD")
        self.assertEqual((got["name"], got["commit"]), ("alice", first))
        folder = marketplace.catalog_dir(self.hub.state, "alice")
        self.assertEqual(sorted(os.listdir(folder)), ["repo.git"])               # no working copy
        self.assertTrue(os.path.isfile(os.path.join(folder, "repo.git", "HEAD")))
        self.assertEqual([e["name"] for e in got["catalog"]["entries"]], ["x"])
        second = cat.commit(catalog(entry("x"), entry("y")))
        again = marketplace.fetch(self.hub.state, cat.bare, name="alice")
        self.assertEqual((again["commit"], [e["name"] for e in again["catalog"]["entries"]]), (second, ["x", "y"]))
        old = marketplace.read(self.hub.state, "alice", first)                   # the previous commit is readable
        self.assertEqual([e["name"] for e in old["entries"]], ["x"])
        self.assertEqual(listed(marketplace.store_root(self.hub.state)), ["alice"])        # no temporary folder

    def test_broken_json_fails_the_fetch_and_the_last_good_commit_stays_readable(self):
        cat = Catalog(self, catalog(entry("x")))
        first = marketplace.fetch(self.hub.state, cat.bare)["commit"]
        cat.src.commit({marketplace.FILE: '{"name": "alice", "plugins": ['})
        with self.assertRaises(marketplace.Invalid) as e:
            marketplace.fetch(self.hub.state, cat.bare, name="alice")
        self.assertIn("JSON", str(e.exception))
        self.assertEqual(marketplace.read(self.hub.state, "alice", first)["name"], "alice")

    def test_a_reserved_name_from_a_foreign_source_leaves_nothing_on_disk(self):
        cat = Catalog(self, {"name": "agents-hub", "plugins": []})
        with self.assertRaises(marketplace.Invalid):
            marketplace.fetch(self.hub.state, cat.bare)
        self.assertEqual(listed(marketplace.store_root(self.hub.state)), [])
        self.assertEqual(marketplace.fetch(self.hub.state, cat.bare, official=True)["name"], "agents-hub")

    def test_a_catalog_clone_over_the_limit_is_refused_and_removed(self):
        cat = Catalog(self, catalog(entry("x")))
        with mock.patch.dict(marketplace.LIMITS, clone=10), self.assertRaises(RuntimeError) as e:
            marketplace.fetch(self.hub.state, cat.bare)
        self.assertIn("clone", str(e.exception))
        self.assertEqual(listed(marketplace.store_root(self.hub.state)), [])
        first = marketplace.fetch(self.hub.state, cat.bare)["commit"]          # connected, then grew past the limit
        cat.commit(files={"big.bin": os.urandom(1 << 16).hex()})
        for _ in range(2):                                                       # a retry — the same refusal
            with mock.patch.dict(marketplace.LIMITS, clone=1000), self.assertRaises(RuntimeError):
                marketplace.fetch(self.hub.state, cat.bare, name="alice")
        self.assertEqual(marketplace.read(self.hub.state, "alice", first)["name"], "alice")   # the previous one is readable
        with self.assertRaises(RuntimeError) as e:
            marketplace.fetch(self.hub.state, cat.bare, name="nul")
        self.assertIn("Windows", str(e.exception))

    def test_a_name_taken_by_another_source_is_refused(self):
        marketplace.fetch(self.hub.state, Catalog(self, catalog()).bare)
        with self.assertRaises(RuntimeError) as e:
            marketplace.fetch(self.hub.state, Catalog(self, catalog()).bare)
        self.assertIn("already taken", str(e.exception))

    def test_a_catalog_without_its_file(self):
        src = Source(self, {"readme.md": "x"})
        with self.assertRaises(marketplace.Invalid) as e:
            marketplace.fetch(self.hub.state, src.bare)
        self.assertIn(marketplace.FILE, str(e.exception))


class EntryCode(unittest.TestCase):
    """An entry's code: exactly the pinned sha, not the author's HEAD; a lost sha is a refusal; `./path` is a subfolder of
    the catalog commit."""

    def setUp(self):
        self.hub = Hub(self, {})
        self.store = marketplace.plugins_store(self.hub.state)

    def test_the_pinned_sha_is_installed_not_the_authors_head(self):
        plugin = Source(self, {"hub-plugin.json": {"name": "x", "web": "main.js"}, "main.js": "v1"})
        first = git(plugin.work, "rev-parse", "HEAD")
        plugin.commit({"main.js": "v2"})
        cat = Catalog(self, catalog({"name": "x", "source": {"git": "https://example.invalid/x.git", "sha": first}}))
        got = marketplace.fetch(self.hub.state, cat.bare)
        src = marketplace.source_of(cat.bare, got["commit"], got["catalog"]["entries"][0])
        self.assertEqual(src, {"url": "https://example.invalid/x.git", "sha": first})
        commit, folder = marketplace.fetch_code(self.store, {**src, "url": plugin.bare})
        self.assertEqual(commit, first)
        with open(os.path.join(folder, "main.js")) as f:
            self.assertEqual(f.read(), "v1")
        self.assertIsNone(self.store.damage(plugin.bare, first))
        codestore.remove(plugin.root)                                            # the author is unreachable: the commit is already here
        with spying() as calls:
            self.assertEqual(marketplace.fetch_code(self.store, {**src, "url": plugin.bare}), (first, folder))
        self.assertFalse([a for a in calls if {"ls-remote", "fetch"} & set(a)], calls)

    def test_a_sha_the_author_no_longer_has_is_refused_and_nothing_is_unpacked(self):
        plugin = Source(self, {"hub-plugin.json": {"name": "x", "web": "main.js"}, "main.js": "v1"})
        lost = plugin.commit({"main.js": "lost"})
        git(plugin.work, "reset", "-q", "--hard", "HEAD~1")
        git(plugin.work, "push", "-q", "--force", plugin.bare, "HEAD:main")     # the author rewrote history
        git(plugin.bare, "reflog", "expire", "--expire=now", "--all")
        git(plugin.bare, "gc", "-q", "--prune=now")
        for sha in (lost, SHA2):
            with self.subTest(sha=sha), self.assertRaises(RuntimeError) as e:
                marketplace.fetch_code(self.store, {"url": plugin.bare, "sha": sha})
            self.assertIn("the author does not have", str(e.exception))
            self.assertFalse(os.path.exists(self.store.commit_dir(plugin.bare, sha)))

    def test_a_path_entry_is_the_subfolder_of_the_catalog_commit(self):
        cat = Catalog(self, catalog({"name": "dark", "source": "./plugins/dark"}),
                      {"plugins/dark/hub-plugin.json": {"name": "dark", "themes": ["dark.css"]},
                       "plugins/dark/dark.css": "body{}"})
        got = marketplace.fetch(self.hub.state, cat.bare)
        src = marketplace.source_of(cat.bare, got["commit"], got["catalog"]["entries"][0])
        self.assertEqual(src, {"url": cat.bare, "sha": got["commit"], "subdir": "plugins/dark"})
        cat.commit(files={"plugins/dark/dark.css": "body{color:red}"})          # the catalog moved ahead
        commit, folder = marketplace.fetch_code(self.store, src)
        self.assertEqual((commit, os.path.basename(os.path.dirname(os.path.dirname(folder)))), (got["commit"], got["commit"]))
        with open(os.path.join(folder, "dark.css")) as f:
            self.assertEqual(f.read(), "body{}")
        with self.assertRaises(RuntimeError) as e:
            marketplace.fetch_code(self.store, {**src, "subdir": "plugins/none"})
        self.assertIn("plugins/none", str(e.exception))


class InALayer(unittest.TestCase):
    """A hub from the install line runs from a layer folder with a stop file `.git` (appcode.GIT_STOP): a git started
    there without a repository of its own looked for one in the layer and failed — no official catalog was downloaded."""

    def setUp(self):
        from core import appcode
        self.layer = tempfile.mkdtemp(prefix="layer-")
        self.addCleanup(platforms.current.rmtree, self.layer)
        with open(os.path.join(self.layer, appcode.GIT_STOP[0]), "wb") as f:
            f.write(appcode.GIT_STOP[1])
        os.makedirs(os.path.join(self.layer, "modules", "x"))
        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        os.chdir(os.path.join(self.layer, "modules", "x"))   # hubd stands in the layer, a module in its folder
        self.hub = Hub(self, {})

    def test_the_catalog_is_downloaded_from_the_layer(self):
        cat = Catalog(self, catalog(entry("x")))
        got = marketplace.fetch(self.hub.state, cat.bare)
        self.assertEqual(got["commit"], git(cat.src.work, "rev-parse", "HEAD"))

    def test_a_tag_of_a_source_is_asked_from_the_layer(self):
        src = Source(self)
        src.tag("v1")
        self.assertTrue(any(x.endswith("refs/tags/v1") for x in codestore.ls_remote(src.bare, "refs/tags/v1")))

    def test_the_cwd_is_left_as_it_is_outside_a_layer(self):
        os.chdir(self.layer)
        os.unlink(os.path.join(self.layer, ".git"))
        self.assertEqual(codestore.git_cwd(), {})
        with open(os.path.join(self.layer, ".git"), "w") as f:   # a working copy of a person's own: no stop
            f.write("gitdir: /elsewhere\n")
        self.assertEqual(codestore.git_cwd(), {})

    def test_a_repository_inside_a_layer_keeps_its_cwd(self):   # the nearest `.git` decides, as in git itself
        inner = os.path.join(self.layer, "work")
        os.makedirs(inner)
        git(inner, "init", "-q")
        os.chdir(inner)
        self.assertEqual(codestore.git_cwd(), {})

    def test_a_gone_cwd_is_no_failure_of_git(self):
        # the folder the process stood in was deleted (POSIX; Windows refuses to delete a process's folder): getcwd fails
        with mock.patch.object(os, "getcwd", side_effect=FileNotFoundError(2, "No such file or directory")):
            self.assertEqual(codestore.git_cwd(), {"cwd": tempfile.gettempdir()})

    def test_a_command_that_names_its_repository_keeps_the_cwd(self):
        seen = []
        real = platforms.current.run
        with mock.patch.object(platforms.current, "run", lambda a, t, **kw: seen.append(kw.get("cwd")) or real(a, t, **kw)):
            codestore.git("--git-dir=" + self.layer, "rev-parse")
            codestore.git("ls-remote", "--", self.layer)
        self.assertEqual(seen[0], None)
        self.assertEqual(seen[1], tempfile.gettempdir())

    def test_the_plugin_market_asks_the_authors_head_from_the_layer(self):
        from modules.market import market
        src = Source(self)
        mk = market.Market(os.path.join(self.hub.state, "m.json"), os.path.join(self.hub.state, "store"),
                           clone_url=lambda full: src.bare)
        self.assertEqual(mk._head("a/b"), git(src.work, "rev-parse", "HEAD"))


class Store(unittest.TestCase):
    def setUp(self):
        self.src = Source(self, {"module.json": manifest(), "main.py": "v1", "hub-plugin.json": {"name": "x", "run": ["x"]}})
        self.hub = Hub(self, {})

    def plugin(self, commit):
        store = marketplace.plugins_store(self.hub.state)
        store.fetch(self.src.bare, commit)
        return store, store.unpack(self.src.bare, commit)

    def edit(self, **cfg):
        with modsrc.catalog.edit_settings(self.hub.cfg_path) as raw:
            raw.update(cfg)

    def test_one_repo_as_a_module_and_a_plugin_on_different_commits_keeps_both_folders(self):
        a = git(self.src.work, "rev-parse", "HEAD")
        b = self.src.commit({"main.py": "v2"})
        c = self.src.commit({"main.py": "v3"})
        self.hub.add(self.src.bare, ref=a)
        store, folder_b = self.plugin(b)
        _, folder_c = self.plugin(c)
        self.edit(extensions={"alice:x": {"path": folder_b, "source": "alice/x", "commit": b}})
        self.assertNotEqual(os.path.dirname(folder_b), modsrc.source_dir(self.hub.state, self.src.bare))  # different roots
        self.hub.update()                         # module cleanup: a plugin folder of the same repo is not theirs
        store.cleanup(self.hub.cfg(), self.src.bare)
        self.assertTrue(os.path.isdir(modsrc.commit_dir(self.hub.state, self.src.bare, a)))
        self.assertTrue(os.path.isdir(folder_b))
        self.assertFalse(os.path.exists(folder_c))                              # nothing refers to it
        self.assertFalse(os.path.exists(folder_c + codestore.LISTING))

    def test_skill_sets_and_live_plugin_processes_keep_their_folders(self):
        b = self.src.commit({"main.py": "v2"})
        c = self.src.commit({"main.py": "v3"})
        store, folder_b = self.plugin(b)
        _, folder_c = self.plugin(c)
        self.edit(sets=[{"name": "x", "path": os.path.join(folder_b, "skills")}])
        with open(os.path.join(self.hub.state, "hub-up.json"), "w") as f:     # hub-up: the process is still in folder c
            json.dump({"pid": 1, "exts": {"ext:alice:x": folder_c}}, f)
        store.cleanup(self.hub.cfg(), self.src.bare)
        self.assertTrue(os.path.isdir(folder_b) and os.path.isdir(folder_c))
        os.unlink(os.path.join(self.hub.state, "hub-up.json"))
        store.cleanup(self.hub.cfg(), self.src.bare)
        self.assertTrue(os.path.isdir(folder_b))
        self.assertFalse(os.path.exists(folder_c))

    def test_the_plugin_root_counts_extra_files_as_damage_the_module_root_does_not(self):
        head = git(self.src.work, "rev-parse", "HEAD")
        self.hub.add(self.src.bare)
        store, folder = self.plugin(head)
        module = modsrc.commit_dir(self.hub.state, self.src.bare, head)
        for root in (folder, module):
            os.makedirs(os.path.join(root, "__pycache__"))
            with open(os.path.join(root, "__pycache__", "main.cpython-312.pyc"), "w") as f:
                f.write("x")
        self.assertIsNone(store.damage(self.src.bare, head))                     # bytecode is not an edit
        for root in (folder, module):
            with open(os.path.join(root, "json.py"), "w") as f:                 # a module planted next to main.py
                f.write("x")
        self.assertIn("json.py", store.damage(self.src.bare, head))
        self.assertIsNone(modsrc.damage(self.hub.state, self.src.bare, head))  # for modules — as before
        store.unpack(self.src.bare, head)                                        # unpacking again — nothing extra left
        self.assertFalse(os.path.exists(os.path.join(folder, "json.py")))
        self.assertIsNone(store.damage(self.src.bare, head))
        for noise in (".DS_Store", "Thumbs.db", "desktop.ini"):                  # file manager traces are not code
            with open(os.path.join(folder, noise), "w") as f:
                f.write("x")
        self.assertIsNone(store.damage(self.src.bare, head))

    def test_data_a_plugin_keeps_in_its_folder_survives_a_repair_of_the_code_but_planted_code_does_not(self):
        head = git(self.src.work, "rev-parse", "HEAD")
        store, folder = self.plugin(head)
        os.makedirs(os.path.join(folder, "cache", "deep"))
        for rel in ("cache.db", os.path.join("cache", "deep", "k.json")):
            with open(os.path.join(folder, rel), "w") as f:
                f.write("data")
        with open(os.path.join(folder, "main.py"), "w") as f:                    # the code was edited
            f.write("evil")
        with open(os.path.join(folder, "json.py"), "w") as f:                    # planted next to main.py
            f.write("x")
        store.unpack(self.src.bare, head)
        with open(os.path.join(folder, "main.py")) as f:
            self.assertEqual(f.read(), "v1")
        self.assertFalse(os.path.exists(os.path.join(folder, "json.py")))
        for rel in ("cache.db", os.path.join("cache", "deep", "k.json")):
            with open(os.path.join(folder, rel)) as f:
                self.assertEqual(f.read(), "data", rel)
        self.assertIsNone(store.damage(self.src.bare, head))                     # data is not code, it does not count as an edit
        store.unpack(self.src.bare, head)                                        # and unpacking again leaves it alone
        self.assertTrue(os.path.isfile(os.path.join(folder, "cache.db")))

    def test_files_claude_code_loads_are_planted_code_not_data(self):
        head = git(self.src.work, "rev-parse", "HEAD")
        store, folder = self.plugin(head)
        planted = [".mcp.json", os.path.join("hooks", "hooks.json"), os.path.join("skills", "x", "SKILL.md"), "x.dll"]
        for rel in planted + ["settings.json"]:
            os.makedirs(os.path.dirname(os.path.join(folder, rel)), exist_ok=True)
            with open(os.path.join(folder, rel), "w") as f:
                f.write("x")
        with open(store.listing_path(self.src.bare, head)) as f:
            listing = json.load(f)["files"]
        self.assertEqual(sorted(codestore._extra(folder, listing)), sorted(r.replace(os.sep, "/") for r in planted))
        store.unpack(self.src.bare, head)
        self.assertEqual([r for r in planted if os.path.exists(os.path.join(folder, r))], [])
        self.assertTrue(os.path.isfile(os.path.join(folder, "settings.json")))   # the plugin's data
        self.assertIsNone(store.damage(self.src.bare, head))

    def test_a_failed_repair_is_a_runtime_error_and_the_next_unpack_finishes_it(self):
        head = git(self.src.work, "rev-parse", "HEAD")
        store, folder = self.plugin(head)
        with open(os.path.join(folder, "main.py"), "w") as f:
            f.write("evil")
        with mock.patch("shutil.copy2", side_effect=PermissionError("busy")):
            with self.assertRaises(RuntimeError):
                store.unpack(self.src.bare, head)
        store.unpack(self.src.bare, head)
        self.assertIsNone(store.damage(self.src.bare, head))
        with open(os.path.join(folder, "main.py")) as f:
            self.assertEqual(f.read(), "v1")

    def test_the_listing_after_a_repair_holds_the_real_times_so_damage_does_not_hash_every_file(self):
        head = git(self.src.work, "rev-parse", "HEAD")
        store, folder = self.plugin(head)
        os.utime(os.path.join(folder, "module.json"), ns=(10**18, 10**18))        # intact, but the time differs
        with open(os.path.join(folder, "main.py"), "w") as f:
            f.write("evil")
        store.unpack(self.src.bare, head)
        with mock.patch("core.codestore._hash", side_effect=AssertionError("hash")):
            self.assertIsNone(store.damage(self.src.bare, head))

    def test_data_next_to_a_listed_file_that_became_a_folder_does_not_stop_the_repair(self):
        head = git(self.src.work, "rev-parse", "HEAD")
        store, folder = self.plugin(head)
        os.unlink(os.path.join(folder, "main.py"))
        os.makedirs(os.path.join(folder, "main.py"))                              # a folder in place of a file
        with open(os.path.join(folder, "main.py", "x.txt"), "w") as f:
            f.write("x")
        store.unpack(self.src.bare, head)
        with open(os.path.join(folder, "main.py")) as f:
            self.assertEqual(f.read(), "v1")

    def test_a_planted_link_is_removed_by_the_repair_and_nothing_behind_it_is_touched(self):
        head = git(self.src.work, "rev-parse", "HEAD")
        store, folder = self.plugin(head)
        evil = tempfile.mkdtemp(prefix="evil-")
        self.addCleanup(shutil.rmtree, evil, True)
        with open(os.path.join(evil, "keep.db"), "w") as f:
            f.write("theirs")
        winsafe.symlink(self, evil, os.path.join(folder, "json"))
        os.unlink(os.path.join(folder, "main.py"))
        winsafe.symlink(self, os.path.join(evil, "keep.db"), os.path.join(folder, "main.py"))   # a link out instead
        store.unpack(self.src.bare, head)
        self.assertFalse(os.path.lexists(os.path.join(folder, "json")))
        self.assertFalse(os.path.islink(os.path.join(folder, "main.py")))
        with open(os.path.join(evil, "keep.db")) as f:
            self.assertEqual(f.read(), "theirs")
        self.assertIsNone(store.damage(self.src.bare, head))

    def test_a_planted_folder_link_is_damage_in_the_plugin_root(self):
        head = git(self.src.work, "rev-parse", "HEAD")
        store, folder = self.plugin(head)
        evil = tempfile.mkdtemp(prefix="evil-")
        self.addCleanup(shutil.rmtree, evil, True)
        winsafe.symlink(self, evil, os.path.join(folder, "json"))                # a json package next to main.py
        self.assertIn("json", store.damage(self.src.bare, head) or "")

    def test_a_folder_link_from_the_commit_is_listed_and_checked(self):
        src = Source(self, {"hub-plugin.json": {"name": "x", "web": "lib/main.js"}, "lib/main.js": "x"})
        winsafe.symlink(self, "lib", os.path.join(src.work, "web"))
        head = src.commit({})
        store = marketplace.plugins_store(self.hub.state)
        store.fetch(src.bare, head)
        folder = store.unpack(src.bare, head)
        self.assertTrue(os.path.islink(os.path.join(folder, "web")))
        self.assertIsNone(store.damage(src.bare, head))
        os.unlink(os.path.join(folder, "web"))
        winsafe.symlink(self, "/", os.path.join(folder, "web"))                  # the link was redirected
        self.assertIn("web", store.damage(src.bare, head) or "")

    def test_the_archive_goes_through_a_file_not_memory(self):
        head = git(self.src.work, "rev-parse", "HEAD")
        with spying() as calls:
            _, folder = self.plugin(head)
            modsrc.add(self.hub.state, self.hub.cfg_path, self.src.bare)
        archives = [a for a in calls if "archive" in a]
        self.assertEqual(len(archives), 2)                                       # both the plugin and the module
        self.assertTrue(all(any(x.startswith("--output=") for x in a) for a in archives), archives)
        self.assertEqual([n for n in os.listdir(os.path.dirname(folder)) if ".tar" in n], [])   # the archive is removed

    def test_limits_refuse_too_many_files_too_much_data_and_a_huge_clone(self):
        head = git(self.src.work, "rev-parse", "HEAD")
        for limits, word in (({"files": 2}, "files"), ({"unpacked": 10}, "MB"), ({"clone": 10}, "clone")):
            with self.subTest(limits):
                store = codestore.Store(Hub(self, {}).state, "plugins", strict=True,
                                        limits={**marketplace.LIMITS, **limits})
                with spying() as calls, self.assertRaises(RuntimeError) as e:
                    store.fetch(self.src.bare, head)
                    store.unpack(self.src.bare, head)
                self.assertIn(word, str(e.exception))
                self.assertFalse(os.path.exists(store.commit_dir(self.src.bare, head)))
                self.assertFalse([a for a in calls if "archive" in a])            # refused before the archive is written
        store = marketplace.plugins_store(self.hub.state)                        # normal limits — installs
        store.fetch(self.src.bare, head)
        self.assertTrue(os.path.isdir(store.unpack(self.src.bare, head)))
        self.hub.add(self.src.bare)                                              # modules have no limits — as before

    def test_the_clone_limit_counts_what_one_fetch_brings_not_the_grown_clone(self):
        head = git(self.src.work, "rev-parse", "HEAD")
        store = marketplace.plugins_store(self.hub.state)
        store.fetch(self.src.bare, head)
        grown = codestore._size(store.git_dir(self.src.bare))
        nxt = self.src.commit({"main.py": "v2"})
        tight = codestore.Store(self.hub.state, "plugins", strict=True, limits={**marketplace.LIMITS, "clone": grown})
        self.assertEqual(tight.fetch(self.src.bare, nxt)[0], nxt)                # the clone grew past the limit — not a refusal
        self.assertGreater(codestore._size(store.git_dir(self.src.bare)), grown)
        big = self.src.commit({"big.bin": os.urandom(1 << 16).hex()})
        tiny = codestore.Store(self.hub.state, "plugins", strict=True, limits={**marketplace.LIMITS, "clone": 1000})
        for _ in range(2):                                                       # a refusal, and a retry is a refusal too
            with self.assertRaises(RuntimeError):
                tiny.fetch(self.src.bare, big)
        store.unpack(self.src.bare, nxt)                                         # the commit that stands — from the same clone
        self.assertIsNone(store.damage(self.src.bare, nxt))


class Handler(http.server.BaseHTTPRequestHandler):
    """A git server that demands a password."""
    def do_GET(self):  # noqa: N802
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="x"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


class NeverAsks(unittest.TestCase):
    """The hub's git asks for a password neither in a window nor in a terminal: a source behind a password is an error
    (the credential helper and ssh-agent work as they are)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="askpass-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.marker = os.path.join(self.tmp, "asked")
        script = os.path.join(self.tmp, "askpass.py")
        with open(script, "w") as f:
            f.write(f"#!{sys.executable}\nopen({self.marker!r}, 'a').write('asked\\n')\nprint('secret')\n")
        # git runs askpass as a program, without a shell: an executable file (Windows — .cmd)
        if os.name == "nt":
            ask = os.path.join(self.tmp, "askpass.cmd")
            with open(ask, "w") as f:
                f.write(f'@"{sys.executable}" "{script}" %*\n')
        else:
            ask = script
            os.chmod(script, 0o700)
        gitconfig = os.path.join(self.tmp, "gitconfig")
        with open(gitconfig, "w") as f:
            f.write(f"[core]\n\taskPass = {ask.replace(chr(92), '/')}\n")
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.url = f"http://127.0.0.1:{server.server_address[1]}/x.git"
        env = mock.patch.dict(os.environ, {"GIT_ASKPASS": ask, "SSH_ASKPASS": ask, "GIT_CONFIG_GLOBAL": gitconfig,
                                           "SSH_ASKPASS_REQUIRE": "force", "NO_PROXY": "127.0.0.1",
                                           "no_proxy": "127.0.0.1"})
        env.start()
        self.addCleanup(env.stop)
        self.hub = Hub(self, {})

    def test_a_source_behind_a_password_is_an_error_without_asking(self):
        for name, call in (("module", lambda: modsrc.fetch(self.hub.state, self.url)),
                           ("module add", lambda: self.hub.add(self.url)),
                           ("plugin", lambda: marketplace.plugins_store(self.hub.state).fetch(self.url, SHA2)),
                           ("catalog", lambda: marketplace.fetch(self.hub.state, self.url))):
            with self.subTest(name), self.assertRaises(RuntimeError):
                call()
        self.assertFalse(os.path.exists(self.marker), "git called askpass")

    def test_file_transport_is_closed_for_remote_sources_only(self):
        src = Source(self)
        with spying() as calls, contextlib.suppress(RuntimeError):
            modsrc.fetch(self.hub.state, src.bare)
            modsrc.fetch(self.hub.state, self.url)
        network = [a for a in calls if {"ls-remote", "fetch"} & set(a)]           # the commands that go to the source
        remote = [a for a in network if self.url in a]
        local = [a for a in network if src.bare in a]
        self.assertTrue(remote and local)
        self.assertTrue(all("protocol.file.allow=never" in a for a in remote), remote)
        self.assertFalse(any("protocol.file.allow=never" in a for a in local), local)
        self.assertTrue(all("core.askPass=" in a for a in calls), calls)

    def test_git_credential_manager_does_not_open_a_sign_in_window(self):
        """The hub downloads the official catalog and modules itself, in the background: the Git Credential Manager helper
        (Windows, Mac) with no sign-in would open a GitHub sign-in window — with GCM_INTERACTIVE=never it quietly answers
        "no", as without a helper."""
        seen, real = [], platforms.current.run

        def spy(args, *a, **kw):
            if args[:1] == ["git"]:
                seen.append((kw.get("env") or {}).get("GCM_INTERACTIVE"))
            return real(args, *a, **kw)
        with mock.patch.dict(os.environ, {"GCM_INTERACTIVE": "always"}), \
                mock.patch.object(platforms.current, "run", spy), contextlib.suppress(RuntimeError):
            marketplace.fetch(self.hub.state, self.url)                         # over the network, like the official catalog
        self.assertTrue(seen)
        self.assertEqual(set(seen), {"never"})

    def test_git_runs_without_the_terminal(self):
        """ssh asks for a key passphrase and an unknown host through the terminal (/dev/tty), not through stdin: git runs
        in its own OS session, without a controlling terminal — there ssh cannot ask and fails with an error."""
        seen, real = [], platforms.current.run

        def spy(args, *a, **kw):
            if args[:1] == ["git"]:
                seen.append((kw.get("start_new_session"), kw.get("stdin")))
            return real(args, *a, **kw)
        with mock.patch.object(platforms.current, "run", spy):
            modsrc.fetch(self.hub.state, Source(self).bare)
        self.assertTrue(seen)
        self.assertEqual(set(seen), {(platforms.current.no_terminal().get("start_new_session"), subprocess.DEVNULL)})
        from platforms import posix, windows
        self.assertEqual(posix.no_terminal(), {"start_new_session": True})
        self.assertEqual(windows.no_terminal(), {})   # a hidden console would hide ssh's question

    def test_local_and_remote_addresses(self):
        for url in ("/srv/x.git", "x.git", "./x", "file:///srv/x.git", "C:\\x\\y.git", "C:/x", "\\\\server\\share\\x"):
            with self.subTest(url):
                self.assertTrue(codestore.is_local(url))
        for url in ("https://example.org/x.git", "ssh://git@example.org/x.git", "git@example.org:x/y.git",
                    "example.org:x.git", "git://example.org/x"):
            with self.subTest(url):
                self.assertFalse(codestore.is_local(url))


if __name__ == "__main__":
    unittest.main()
