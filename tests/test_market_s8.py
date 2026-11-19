"""Marketplaces, slice S8 (spec §4): the hub's old plugin lists are gone — the list built at release from the shared
repo's collaborators (`modules/market/trusted.json`, `modules/market/trusted.py`, its step in tools/publish.sh) and
GitHub accounts as marketplaces (`markets` in hub.json, `hub market add`). Catalogs (`hub marketplace`) replaced them.

What stays is the path catalogs do not cover: a plugin by link goes through code review (`hub market review`,
`verdict`, `install --reviewed`), and a plugin installed outside catalogs is updated through review.

A hub of the previous release keeps what it installed: nothing is written on update. A plugin installed from the old
list whose source a catalog pins is "from the catalog" on read (by source) and updates from it; the `markets` key and
the hub's own fresh list stay on disk untouched, so the previous release reads them again after a rollback."""
import tests  # noqa: F401 — keeps the machine's settings out
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import platforms
from core import catalog, marketplaces
from modules.market import market
from tests import test_web
from tests.test_marketplace import Catalog
from tests.test_mpinstall import OWL, cat, owl
from tests.test_modapi_sources import Hub, git

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB = os.path.join(REPO, "bin", "hub")
PREVIOUS = "v1.8.0"   # the last release that had the old lists: a rollback goes back to it
KANBAN = "acme/hub-plugin-kanban"   # in the old list, not in any catalog


def old_list(state, *repos):
    """The hub's fresh copy of the old list (<state>/market/trusted.json, written by the publishing hub; it outranked
    the file in the repo)."""
    path = os.path.join(state, "market", "trusted.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"api": "1.33", "built": 1, "plugins": [
            {"repo": r, "name": r.rsplit("-", 1)[-1], "description": "board", "kinds": ["page"]} for r in repos]}, f)
    return path


def author_repo(root, manifest, files):
    """A plugin author's repo on "GitHub": a working repo with one commit."""
    os.makedirs(root)
    git(root, "init", "-q", "-b", "main")
    for rel, text in {**files, market.MANIFEST: json.dumps(manifest)}.items():
        with open(os.path.join(root, rel), "w", encoding="utf-8") as f:
            f.write(text)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "start")
    return root


def raw(path):
    with open(path, "rb") as f:
        return f.read()


class Gone(unittest.TestCase):
    """Nothing installs, lists or searches by the old lists any more — with or without a readable catalog."""

    def setUp(self):
        self.hub = Hub(self, {"markets": ["acme"]})
        old_list(self.hub.state, KANBAN)
        self.gh = tempfile.mkdtemp(prefix="s8-gh-")
        self.addCleanup(platforms.current.rmtree, self.gh)
        author_repo(os.path.join(self.gh, *KANBAN.split("/")), {"name": "kanban", "description": "board", "web": "main.js"},
                    {"main.js": "export default 1\n"})
        self.asked = []

    def market(self):
        return market.Market(self.hub.cfg_path, os.path.join(self.hub.state, "market"),
                             api=lambda url: self.asked.append(url) or {"items": [{"full_name": KANBAN}]},
                             clone_url=lambda full: os.path.join(self.gh, *full.split("/")))

    def test_a_repo_of_the_old_list_installs_only_through_review(self):
        before = raw(self.hub.cfg_path)
        with self.assertRaises(market.Error) as e:
            self.market().install(KANBAN)
        self.assertIn(f"hub market review {KANBAN}", str(e.exception))
        self.assertEqual(raw(self.hub.cfg_path), before)
        self.assertFalse(os.path.exists(os.path.join(self.hub.state, "market", "acme")))

    def test_neither_the_old_list_nor_a_github_account_is_searched(self):
        m = self.market()
        self.assertEqual(m.search(""), [])
        self.assertEqual(m.trusted(), [])   # kept for the previous release's page process: always empty
        self.assertEqual(self.asked, [])    # no GitHub search by topic
        lines = marketplaces.search(self.hub.state, self.hub.cfg_path, [])
        self.assertEqual(lines, [marketplaces.NO_CATALOGS])

    def test_an_update_of_a_plugin_outside_catalogs_goes_through_review(self):
        path = os.path.join(self.hub.state, "market", "acme", "hub-plugin-kanban")
        os.makedirs(path)
        with catalog.edit_settings(self.hub.cfg_path) as cfg:   # installed by the previous release from the old list
            cfg["extensions"] = {"acme:kanban": {"path": path, "source": KANBAN, "commit": "b" * 40, "web": "main.js"}}
        before = raw(self.hub.cfg_path)
        m = self.market()
        self.assertFalse(m.updates()["acme:kanban"]["unchecked"])   # the page offers no "Update without review"
        with mock.patch.object(market.subprocess, "run", side_effect=AssertionError("git ran")), \
                self.assertRaises(market.Error) as e:                # refused before any git in its folder
            m.update("acme:kanban")
        self.assertIn("hub market review", str(e.exception))
        self.assertEqual(raw(self.hub.cfg_path), before)

    def run_hub(self, *args):
        env = {k: v for k, v in os.environ.items() if k not in ("HUB_SESSION", "HUB_EXTENSION", "HUB_MODULE")}
        env.update(HUB_STATE_DIR=self.hub.state, HUB_CONFIG=self.hub.cfg_path, HUB_CLI_WAIT="0")
        return subprocess.run([sys.executable, HUB, *args], env=env, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=120)

    def test_hub_market_add_of_an_account_is_refused_and_writes_nothing(self):
        before = raw(self.hub.cfg_path)
        r = self.run_hub("market", "add", "bob")
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("hub marketplace add", r.stdout + r.stderr)
        self.assertEqual(raw(self.hub.cfg_path), before)

    def test_hub_market_list_shows_no_account(self):
        r = self.run_hub("market", "list")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("acme", r.stdout)
        r = self.run_hub("market", "list", "acme")                    # an account: why, and no command that fails too
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("hub marketplace add", r.stdout)
        self.assertNotIn("hub marketplace list acme", r.stdout)
        with mock.patch.dict(os.environ, {"HUB_STATE_DIR": self.hub.state, "HUB_CONFIG": self.hub.cfg_path}), \
                mock.patch("sys.stdout") as out:                       # a flag is not an account: the usage
            self.assertEqual(market.main(["list", "--json"]), 2)
        self.assertIn("hub market review", "".join(c.args[0] for c in out.write.call_args_list))

    def test_a_broken_hub_json_is_said_not_a_traceback(self):
        with open(self.hub.cfg_path, "w", encoding="utf-8") as f:
            f.write("{not json")
        r = self.run_hub("market", "list", "acme")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stdout + r.stderr)


class OldHub:
    """A hub of the previous release, as it was: a plugin installed from the old list whose source a connected catalog
    pins, the hub's own copy of the old list, a GitHub account in `markets`. The new code writes nothing on its own;
    the plugin is "from the catalog" by its source and updates from it into the new store."""

    def setUp(self):
        self.hub = Hub(self, {})
        self.src = Catalog(self, cat(owl()), {"plugins/owl/hub-plugin.json": OWL,
                                              "plugins/owl/main.js": "export default function register() {}\n"})
        marketplaces.add(self.hub.state, self.hub.cfg_path, self.src.bare)
        self.folder = os.path.join(self.hub.state, "market", "someone", "owl")
        os.makedirs(self.folder)
        with open(os.path.join(self.folder, "main.js"), "w", encoding="utf-8") as f:
            f.write("// old\n")
        with catalog.edit_settings(self.hub.cfg_path) as cfg:
            cfg["markets"] = ["acme"]
            cfg["extensions"] = {"someone:owl": {"path": self.folder, "source": self.src.bare, "subdir": "plugins/owl",
                                                 "commit": "b" * 40, "web": "main.js", "version": "0.9.0"}}
        self.mine = old_list(self.hub.state, KANBAN)

    def market(self):
        return market.Market(self.hub.cfg_path, os.path.join(self.hub.state, "market"),
                             api=lambda url: self.fail(f"GitHub asked: {url}"))


class Migration(OldHub, unittest.TestCase):
    def test_reading_writes_nothing_and_the_plugin_is_from_the_catalog(self):
        before, mine = raw(self.hub.cfg_path), raw(self.mine)
        cfg = self.hub.cfg()
        self.assertEqual(marketplaces.entry_for(self.hub.state, cfg, "someone:owl"), "owl@shop")
        (e,) = marketplaces.page(self.hub.state, self.hub.cfg_path)["entries"]
        self.assertEqual((e["installed"], e["newer"]), ("someone:owl", True))
        self.assertEqual(self.market().search(""), [])
        self.assertEqual(self.market().updates(), {})
        self.assertEqual((raw(self.hub.cfg_path), raw(self.mine)), (before, mine))   # markets and the old list stay

    def test_update_moves_it_to_the_new_store_and_keeps_the_old_keys(self):
        text = self.market().update("someone:owl")
        self.assertIn("owl@shop", text)
        cfg = self.hub.cfg()
        ext = cfg["extensions"]["someone:owl"]
        self.assertEqual((ext["market"], ext["entry"]), ("shop", "owl"))
        self.assertTrue(os.path.isfile(os.path.join(ext["path"], "main.js")))
        self.assertEqual(cfg["markets"], ["acme"])   # the previous release reads it after a rollback


@unittest.skipUnless(subprocess.run(["git", "cat-file", "-e", f"{PREVIOUS}^{{commit}}"], cwd=REPO,
                                    capture_output=True).returncode == 0, f"no {PREVIOUS} in this clone")
class Rollback(OldHub, unittest.TestCase):
    """Back to the previous release after the new code ran: its code (from the tag) reads the same hub.json and
    folders — what it installed and what the new code moved into the new store load and list as before."""

    def setUp(self):
        super().setUp()
        self.old = tempfile.mkdtemp(prefix="s8-old-")
        self.addCleanup(platforms.current.rmtree, self.old)
        tree = subprocess.run(["git", "archive", PREVIOUS], cwd=REPO, check=True, capture_output=True).stdout
        subprocess.run(["tar", "-x", "-C", self.old], input=tree, check=True)

    def run_old(self, probe):
        """A probe in the previous release's tree on this hub -> its last line, as JSON."""
        env = {k: v for k, v in os.environ.items() if not k.startswith("HUB_")}
        env.update(PYTHONPATH=self.old, PYTHONDONTWRITEBYTECODE="1", HUB_STATE_DIR=self.hub.state,
                   HUB_CONFIG=self.hub.cfg_path)
        r = subprocess.run([sys.executable, "-c", probe, self.hub.state, self.hub.cfg_path], cwd=self.old, env=env,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_the_previous_release_reads_the_hub_after_the_new_code(self):
        self.market().update("someone:owl")
        got = self.run_old(
            "import json, sys\n"
            "from core import catalog, marketplaces\n"
            "from modules.market import market\n"
            "state, cfg = sys.argv[1], sys.argv[2]\n"
            "m = market.Market(cfg, state + '/market', api=lambda url: {'items': []})\n"
            "page = marketplaces.page(state, cfg)\n"
            "print(json.dumps({'search': [p['repo'] for p in m.search('')], 'list': m.list(),\n"
            "                  'updates': m.updates(), 'markets': catalog.read_settings(cfg).get('markets'),\n"
            "                  'installed': {e['name']: e.get('installed') for e in page['entries']}}))\n")
        self.assertEqual(got["markets"], ["acme"])
        self.assertEqual(got["installed"], {"owl": "someone:owl"})   # the moved plugin is its catalog install
        self.assertEqual(got["updates"], {})
        self.assertIn(KANBAN, got["search"])   # its own old list again (the hub's fresh copy outranks the repo file)

    def test_the_previous_page_process_runs_the_new_market_module(self):
        # the update's self-check: the previous release's page server keeps its core (core.marketplaces among them) in
        # memory and imports modules.market from the new files — its catalog search asks the new module for the old list
        os.remove(os.path.join(self.old, "modules", "market", "trusted.py"))
        os.remove(os.path.join(self.old, "modules", "market", "trusted.json"))
        with open(os.path.join(REPO, "modules", "market", "market.py"), "rb") as f, \
                open(os.path.join(self.old, "modules", "market", "market.py"), "wb") as out:
            out.write(f.read())
        got = self.run_old(
            "import json, sys\n"
            "from core import catalog, marketplaces\n"
            "from modules.market import market\n"
            "state, cfg = sys.argv[1], sys.argv[2]\n"
            "m = market.Market(cfg, state + '/market', restart=None, events=None, via='web')\n"
            "print(json.dumps({'search': marketplaces.search(state, cfg, []),\n"
            "    'market': m.search(''), 'updates': m.updates(), 'trusted': m.trusted(),\n"
            "    'entries': [e['name'] for e in marketplaces.page(state, cfg)['entries']]}))\n")
        self.assertNotIn(KANBAN, "\n".join(got["search"]))   # no old-list lines, and no crash
        self.assertEqual((got["market"], got["updates"], got["trusted"], got["entries"]), ([], {}, [], ["owl"]))


class Page(test_web.WebTest):
    def setUp(self):
        super().setUp()
        self.cfg = os.path.join(self.state, "hub.json")
        with open(self.cfg, "w") as f:
            json.dump({}, f)
        env = mock.patch.dict(os.environ, {"HUB_CONFIG": self.cfg})
        env.start()
        self.addCleanup(env.stop)
        old_list(self.state, KANBAN)
        self.seed()

    def test_the_page_search_has_no_old_list(self):
        code, body = self.req("GET", "/api/plugins/search?q=")
        self.assertEqual(code, 200, body)
        self.assertEqual(json.loads(body)["plugins"], [])


class Tree(unittest.TestCase):
    """The release tree carries no old list and no step that fills it."""

    def test_no_old_list_and_no_collector(self):
        for gone in ("modules/market/trusted.json", "modules/market/trusted.py"):
            self.assertFalse(os.path.exists(os.path.join(REPO, gone)), gone)
        for name in ("tools/publish.sh", "tools/publish_state.py", "tools/envmatrix.py", "core/hubhelp.txt",
                     "EXTENDING.md", "SECURITY.md", "README.md"):
            with open(os.path.join(REPO, name), encoding="utf-8") as f:
                text = f.read()
            self.assertNotIn("trusted.json", text, name)
            self.assertNotIn("modules.market.trusted", text, name)


def load_tests(loader, standard, pattern):   # Page: only its own tests, not the ones inherited from WebTest
    suite = unittest.TestSuite()
    for cls in (Gone, Migration, Rollback, Page, Tree):
        suite.addTests(cls(n) for n in cls.__dict__ if n.startswith("test"))
    return suite


if __name__ == "__main__":
    unittest.main()
