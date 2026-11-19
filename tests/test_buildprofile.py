"""A company's build (Embed API, spec d2144426 §2.5–2.6): its profile (tools/package/buildprofile.py), the tree of the
open core and its embeds from their own repositories (tools/package/compose.py), and what the layer's MANIFEST says it
is made of, with the checks every profile passes (tools/package/layer.py embed_check)."""
import tests  # noqa: F401 — hide the machine's settings
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import platforms
from core import embed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "tools", "package")


def load(name):
    sys.path.insert(0, PKG)
    spec = importlib.util.spec_from_file_location(f"hub_test_{name}", os.path.join(PKG, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write(top, rel, text=""):
    path = os.path.join(top, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return path


def git(repo, *args):
    return subprocess.run(["git", "-c", "user.name=x", "-c", "user.email=x@example.test", *args], cwd=repo,
                          check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout.strip()


def repo_of(top, files, tag=None):
    os.makedirs(top)
    git(top, "init", "-q", "-b", "main")
    for rel, text in files.items():
        write(top, rel, text)
    git(top, "add", "-A")
    git(top, "commit", "-qm", "c")
    if tag:
        git(top, "tag", tag)
    return git(top, "rev-parse", "HEAD")


# a stats slot that keeps the Embed API's names (core/embed.NAMES["stats"]), for the contract
STATS = ("def main(argv, state_dir=None):\n    return 0\n\n\ndef settings(cfg):\n    return {'enabled': False}\n\n\n"
         "def preview(state_dir, cfg, now=None, lang=None, make=None):\n    return ''\n\n\n"
         "def page_report(state_dir, cfg, frm, to, hub=None):\n    return b''\n\n\n"
         "def show_log(state_dir, n, lang=None):\n    return ''\n")
PASSPORT = json.dumps({"embed": 1, "api": embed.API})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hub-buildprofile-")
        self.addCleanup(platforms.current.rmtree, self.tmp)
        self.prof = load("buildprofile")

    def profile(self, data):
        return write(self.tmp, f"p{len(os.listdir(self.tmp))}.json", json.dumps(data))


class Profile(Base):
    def test_a_profile_and_what_it_gives_the_factory(self):
        secrets = os.path.join(self.tmp, "secrets")
        write(secrets, "url", "https://hub.example.test\n")
        write(secrets, "build.key", "k" * 20 + "\n")
        write(secrets, "dsn", "https://abcdefgh@sentry.example.test/1\n")
        p = self.prof.load(self.profile({"profile": 1, "name": "acme", "embeds": [
            {"name": "acme", "repo": "git@git.example.test:acme/embed.git", "ref": "v3"}, {"name": "here", "repo": "."}],
            "deliver": {"url_file": "url", "key_file": "build.key", "report_file": "dsn"}, "issues": "acme/issues"}))
        self.assertEqual((p["flavor"], self.prof.wanted(p)), ("team", ["acme", "here"]))
        self.assertEqual(self.prof.deliver(p, secrets), {"url": "https://hub.example.test", "key": "k" * 20,
                                                         "report": "https://abcdefgh@sentry.example.test/1"})
        self.assertEqual(self.prof.server(p, secrets), "https://hub.example.test")
        self.assertIsNone(self.prof.forbid(p))   # the team flavor: the factory's own rule

    def test_what_is_not_a_profile(self):
        base = {"profile": 1, "name": "acme"}
        for bad in ({}, {**base, "profile": 2}, {**base, "name": "Acme Co"}, {**base, "flavor": "own"},
                    {**base, "embeds": [{"name": "x", "repo": "https://git.example.test/x.git"}]},   # no ref
                    {**base, "embeds": [{"name": "x", "repo": "."}, {"name": "x", "repo": "."}]},
                    {**base, "flavor": "open"},   # an open build names its forbidden strings, or null
                    {**base, "flavor": "open", "forbid_file": None, "deliver": {"url": "u", "key_file": "k"}},
                    {**base, "issues": "not a repo"}, {**base, "platforms": ["amiga"]},
                    {**base, "deliver": {"key_file": "k"}}):
            with self.subTest(bad=bad), self.assertRaises(self.prof.Bad):
                self.prof.load(self.profile(bad))

    def test_the_open_flavors_forbidden_strings(self):
        write(self.tmp, "deny.txt", "# the team's\nreports.example.test\n\n")
        p = self.prof.load(self.profile({"profile": 1, "name": "o", "flavor": "open", "forbid_file": "deny.txt"}))
        self.assertEqual(self.prof.forbid(p), ["reports.example.test"])
        p = self.prof.load(self.profile({"profile": 1, "name": "o", "flavor": "open", "forbid_file": None}))
        self.assertEqual(self.prof.forbid(p), [])   # consciously none: the guard checks the paths only


class Factory(Base):
    def test_an_open_profile_has_no_nightly_here(self):
        factory = load("factory")
        p = self.profile({"profile": 1, "name": "o", "flavor": "open", "forbid_file": None})
        with self.assertRaises(SystemExit) as e:
            factory.main(["layer", "HEAD", "--out", os.path.join(self.tmp, "out"), "--profile", p])
        self.assertIn("an open profile has no nightly here", str(e.exception.code))

    def test_a_broken_profile_is_a_refusal_not_a_traceback(self):
        factory = load("factory")
        p = self.profile({"profile": 1, "name": "acme", "deliver": {"url": "u", "key_file": "k", "report_file": 1}})
        with self.assertRaises(SystemExit) as e:
            factory.main(["v1.0.0", "--out", os.path.join(self.tmp, "out"), "--profile", p])
        self.assertIn("deliver:", str(e.exception.code))


class Trees(Base):
    """An open core of a tag and a company's embed in a repository of its own."""

    def setUp(self):
        super().setUp()
        self.compose = load("compose")
        self.core = os.path.join(self.tmp, "core")
        self.core_commit = repo_of(self.core, {"core/__init__.py": "", "core/x.py": "X = 1\n",
                                               "modules/__init__.py": "", "tests/test_x.py": "T = 1\n"}, "v1.0.0")
        self.acme = os.path.join(self.tmp, "acme")
        self.acme_commit = repo_of(self.acme, {"embed.json": PASSPORT, "README.md": "acme\n", ".gitignore": "*.pyc\n",
                                               "modules/stats/__init__.py": "",
                                               "modules/stats/stats.py": STATS, "tests/test_stats.py": "T = 2\n",
                                               "i18n/en.json": json.dumps({"acme.hi": "hi"})}, "v3")
        self.p = self.prof.load(self.profile({"profile": 1, "name": "acme", "embeds": [
            {"name": "acme", "repo": self.acme, "ref": "v3"}]}))


class Compose(Trees):
    def test_the_tree_of_the_core_and_its_embeds_is_one_commit(self):
        out = os.path.join(self.tmp, "out")
        made = self.compose.compose(self.p, self.core, "v1.0.0", out)
        self.assertEqual(made["core"]["commit"], self.core_commit)
        self.assertEqual(made["embeds"], [{"name": "acme", "repo": self.acme, "ref": "v3", "commit": self.acme_commit}])
        self.assertTrue(os.path.isfile(os.path.join(out, "embed", "acme", "modules", "stats", "stats.py")))
        self.assertTrue(os.path.isfile(os.path.join(out, "core", "x.py")))
        self.assertFalse(os.path.exists(os.path.join(out, "embed", "acme", "README.md")))   # its repository's own
        with open(os.path.join(out, "EMBED.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), made)
        self.assertEqual(git(out, "status", "--porcelain"), "")   # committed: the company tags it and builds
        self.assertEqual(embed.problems(out, ["acme"]), [])

    def test_an_embed_that_replaces_a_file_of_the_core_is_refused_and_nothing_is_left(self):
        write(self.acme, "tests/test_x.py", "T = 3\n")
        git(self.acme, "add", "-A")
        git(self.acme, "commit", "-qm", "over the core")
        git(self.acme, "tag", "-f", "v3")
        out = os.path.join(self.tmp, "out")
        with self.assertRaises(self.compose.Refused) as e:
            self.compose.compose(self.p, self.core, "v1.0.0", out)
        self.assertIn("embed/acme/tests/test_x.py: the core's own file", str(e.exception))
        self.assertFalse(os.path.exists(out))

    def test_an_address_is_named_without_a_sign_in(self):
        self.assertEqual(self.compose.plain("https://user:tok@git.example.test/a/b.git"), "https://git.example.test/a/b.git")
        self.assertEqual(self.compose.plain("git@git.example.test:a/b.git"), "git.example.test:a/b.git")
        self.assertEqual(self.compose.plain("/srv/a/b.git"), "/srv/a/b.git")


class Layer(Trees):
    """The layer of a profile's tree: what its MANIFEST says it is made of, and the checks every profile passes."""

    def setUp(self):
        super().setUp()
        self.layer = load("layer")
        for rel in ("core/__init__.py", "core/embed.py", "core/version.py"):   # the tree's own core reads its embeds
            write(self.core, rel, open(os.path.join(ROOT, *rel.split("/")), encoding="utf-8").read())
        write(self.core, "modules/__init__.py", open(os.path.join(ROOT, "modules", "__init__.py"), encoding="utf-8").read())
        git(self.core, "add", "-A")
        git(self.core, "commit", "-qm", "the Embed API")
        git(self.core, "tag", "-f", "v1.0.0")
        self.core_commit = git(self.core, "rev-parse", "HEAD")

    def built(self, out, **kw):
        return self.layer.build(out, "HEAD", os.path.join(self.tmp, "layer"), sys.executable, version="1.0.0",
                                keep=os.path.join(self.tmp, "seed"), **kw)

    def test_the_manifest_names_the_profile_the_core_and_each_embed(self):
        out = os.path.join(self.tmp, "out")
        made = self.compose.compose(self.p, self.core, "v1.0.0", out)
        self.built(out, profile="acme", wanted=["acme"], check=True)
        with open(os.path.join(self.tmp, "seed", "MANIFEST.json"), encoding="utf-8") as f:
            man = json.load(f)
        self.assertEqual(man["profile"], "acme")
        self.assertEqual(man["core"], made["core"])
        tree = git(out, "rev-parse", "HEAD:embed/acme")
        self.assertEqual(man["embeds"], [{"name": "acme", "repo": self.acme, "ref": "v3", "commit": self.acme_commit,
                                          "tree": tree}])
        self.assertNotIn("embed/acme/tests/test_stats.py", man["files"])   # the layer carries no tests
        self.assertNotIn("EMBED.json", man["files"])   # the MANIFEST says what it is made of
        self.assertFalse([f for f in man["files"] if f.startswith("embed/acme/") and not f.startswith(
            ("embed/acme/modules/", "embed/acme/i18n/")) and f != "embed/acme/embed.json"])   # its README stayed home
        self.assertIn("embed/acme/modules/stats/stats.py", man["files"])

    def test_an_embed_in_the_cores_own_tree(self):
        write(self.core, "embed/here/embed.json", PASSPORT)
        write(self.core, "embed/here/modules/stats/__init__.py")
        write(self.core, "embed/here/modules/stats/stats.py", STATS)
        write(self.core, "embed/here/profile.json", "{}")
        git(self.core, "add", "-A")
        git(self.core, "commit", "-qm", "own embed")
        self.built(self.core, profile="here", wanted=["here"], check=True)
        with open(os.path.join(self.tmp, "seed", "MANIFEST.json"), encoding="utf-8") as f:
            man = json.load(f)
        commit = git(self.core, "rev-parse", "HEAD")
        self.assertEqual(man["embeds"], [{"name": "here", "repo": ".", "commit": commit,
                                          "tree": git(self.core, "rev-parse", "HEAD:embed/here")}])
        self.assertNotIn("core", man)
        self.assertNotIn("embed/here/profile.json", man["files"])   # the company's build profile stays out

    def test_every_profile_is_held_to_the_rules_and_the_contract(self):
        out = os.path.join(self.tmp, "out")
        self.compose.compose(self.p, self.core, "v1.0.0", out)
        with self.assertRaises(self.layer.Refused) as e:   # the profile names another embed
            self.built(out, profile="acme", wanted=["acme", "beta"], check=True)
        self.assertIn("embed/beta: the profile's embed is not in the tree", str(e.exception))
        write(out, "embed/acme/modules/stats/stats.py", "def main(argv):\n    return 0\n")   # the rest gone
        git(out, "commit", "-qam", "a slot that lacks names")
        with self.assertRaises(self.layer.Refused) as e:
            self.built(out, profile="acme", wanted=["acme"], check=True)
        self.assertIn("stats: no settings", str(e.exception))
        self.assertFalse(os.listdir(os.path.join(self.tmp, "layer")))   # nothing is left


class ThisTree(unittest.TestCase):
    """Each profile kept in this tree's embeds loads, and this tree passes the checks its build will hold it to."""

    def test_the_profiles_here_and_their_checks(self):
        prof, layer = load("buildprofile"), load("layer")
        found = [(n, os.path.join(top, embed.PROFILE)) for n, top in embed.roots(ROOT)
                 if os.path.isfile(os.path.join(top, embed.PROFILE))]
        if not found:
            self.skipTest("no embed of this tree carries its company's profile (the open core)")
        for name, path in found:
            with self.subTest(embed=name):
                p = prof.load(path)
                self.assertIn(name, prof.wanted(p))
                layer.embed_check(ROOT, sys.executable, prof.wanted(p))
