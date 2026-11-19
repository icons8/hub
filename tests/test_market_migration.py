"""Marketplaces, slice S6 "Migration and deprecation" (spec §4, §3.11): what was installed before catalogs (`hub market`,
the old store `<state>/market/…`, no `market` field) counts as "from a catalog" on read, by its source, and "Update"
(page, `hub market update`) installs it at exactly the catalog version into the new store (M2), keeping its key and its
own fields; `hub market` is a synonym of `hub marketplace` with a hint. The hub's old list and GitHub accounts as
marketplaces are gone (S8): tests/test_market_s8.py."""
import tests  # noqa: F401 — keeps the machine's settings out
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from core import catalog, marketplaces, mpinstall, version
from modules.market import market
from tests import test_web
from tests.test_marketplace import Catalog
from tests.test_mpinstall import OWL, Base, cat, owl
from tests.test_modapi_sources import Hub

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB = os.path.join(REPO, "bin", "hub")


def old_owl(hub, src, **extra):
    """An owl installed by the old `hub market` before catalogs: a folder in <state>/market, no market and no approved."""
    path = os.path.join(hub.state, "market", "someone", "owl")
    os.makedirs(path)
    with open(os.path.join(path, "main.js"), "w") as f:
        f.write("// old\n")
    with catalog.edit_settings(hub.cfg_path) as raw:
        raw["extensions"] = {"someone:owl": {"path": path, "source": src, "subdir": "plugins/owl", "commit": "b" * 40,
                                             "web": "main.js", "folder": "mine", **extra}}
    return path


class Linked(Base):
    def test_an_old_record_is_from_the_catalog_that_lists_its_source(self):
        old_owl(self.hub, self.src.bare)
        cfg = self.hub.cfg()
        self.assertEqual(marketplaces.entry_for(self.hub.state, cfg, "someone:owl"), "owl@shop")
        self.assertIsNone(marketplaces.entry_for(self.hub.state, cfg, "nobody:owl"))
        with catalog.edit_settings(self.hub.cfg_path) as raw:      # a different source: not from the catalog
            raw["extensions"]["someone:owl"]["subdir"] = "elsewhere"
        self.assertIsNone(marketplaces.entry_for(self.hub.state, self.hub.cfg(), "someone:owl"))

    def test_a_record_installed_from_a_catalog_names_its_entry(self):
        self.install()
        self.assertEqual(marketplaces.entry_for(self.hub.state, self.hub.cfg(), "shop:owl"), "owl@shop")

    def test_the_official_catalog_comes_first_when_two_list_the_same_repo(self):
        kanban = {"name": "kanban", "source": {"github": "acme/hub-plugin-kanban", "sha": "a" * 40}}
        self.src.commit(cat(owl(), kanban))
        marketplaces.update(self.hub.state, self.hub.cfg_path, "shop")
        other = Catalog(self, cat(kanban, name="zeta"), {})
        with mock.patch.object(marketplaces, "OFFICIAL", other.bare):
            marketplaces.add(self.hub.state, self.hub.cfg_path, other.bare)
            with catalog.edit_settings(self.hub.cfg_path) as raw:      # installed by the old `hub market`
                raw["extensions"] = {"acme:kanban": {"path": "/x", "source": "acme/hub-plugin-kanban", "web": "a.js"}}
            cfg = self.hub.cfg()
            self.assertEqual(marketplaces.entry_for_repo(self.hub.state, cfg, "acme/hub-plugin-kanban"), "kanban@zeta")
            self.assertEqual(marketplaces.entry_for(self.hub.state, cfg, "acme:kanban"), "kanban@zeta")

    def test_a_repo_of_a_pinned_github_entry_is_found_for_the_old_install_command(self):
        self.src.commit(cat(owl(), {"name": "kanban", "source": {"github": "Acme/hub-plugin-kanban", "sha": "a" * 40}},
                            {"name": "loose", "source": {"github": "acme/loose"}}))
        marketplaces.update(self.hub.state, self.hub.cfg_path, "shop")
        cfg = self.hub.cfg()
        self.assertEqual(marketplaces.entry_for_repo(self.hub.state, cfg, "acme/hub-plugin-kanban"), "kanban@shop")
        self.assertIsNone(marketplaces.entry_for_repo(self.hub.state, cfg, "acme/loose"))   # not pinned
        self.assertIsNone(marketplaces.entry_for_repo(self.hub.state, cfg, "acme/other"))
        (line,) = marketplaces.search(self.hub.state, self.hub.cfg_path, ["acme/hub-plugin-kanban"])   # by repo too
        self.assertIn("kanban@shop", line)
        self.assertIn("Acme/hub-plugin-kanban", line)                     # "install acme/kanban": is it the right entry


class Review(Base):
    """From the high-effort review: an old record newer than the catalog-pinned one is neither "newer available" nor
    "updated"; the page and the core use one catalog; an unpinned entry does not take over an old record; a malformed
    marketplaces in hub.json does not break the old path."""

    def entry(self, name, catalog_name="shop"):
        (e,) = [x for x in marketplaces.page(self.hub.state, self.hub.cfg_path)["entries"]
                if x["name"] == name and x["catalog"] == catalog_name]
        return e

    def test_an_old_record_newer_than_the_catalog_is_not_offered_a_downgrade(self):
        old_owl(self.hub, self.src.bare, version="2.0.0")           # installed from the author's head, the catalog lags (1.0.0)
        e = self.entry("owl")
        self.assertEqual(e["installed"], "someone:owl")
        self.assertFalse(e.get("newer"))
        lines, _ = marketplaces.update(self.hub.state, self.hub.cfg_path, "shop")
        self.assertNotIn("can be updated", "\n".join(lines))
        with self.assertRaises(market.Error) as err:                  # "update" is not a silent downgrade
            market.Market(self.hub.cfg_path, os.path.join(self.hub.state, "market")).update("someone:owl")
        self.assertIn("hub marketplace install owl@shop", str(err.exception))
        self.assertNotIn("market", self.hub.cfg()["extensions"]["someone:owl"])
        text = "\n".join(self.install())                              # straight from the catalog is allowed, and it says so
        self.assertIn("rolled back", text)
        with catalog.edit_settings(self.hub.cfg_path) as raw:
            raw["extensions"]["someone:owl"].update(version="0.9.0", commit="c" * 40, market=None)
            raw["extensions"]["someone:owl"].pop("market")
        self.assertTrue(self.entry("owl")["newer"])                   # the catalog is newer: "newer available"
        with catalog.edit_settings(self.hub.cfg_path) as raw:         # same version, different commit: decided by commit
            raw["extensions"]["someone:owl"].update(version="1.0.0")
        self.assertTrue(self.entry("owl")["newer"])

    def test_the_page_marks_an_old_record_in_the_catalog_the_core_updates_it_from(self):
        kanban = {"name": "kanban", "source": {"github": "acme/hub-plugin-kanban", "sha": "a" * 40}}
        self.src.commit(cat(owl(), kanban, {"name": "loose", "source": {"github": "acme/hub-plugin-loose"}}))
        marketplaces.update(self.hub.state, self.hub.cfg_path, "shop")
        other = Catalog(self, cat(kanban, name="zeta"), {})
        with catalog.edit_settings(self.hub.cfg_path) as raw:
            raw["extensions"] = {"acme:kanban": {"path": "/x", "source": "acme/hub-plugin-kanban", "web": "a.js",
                                                 "commit": "b" * 40},
                                 "acme:loose": {"path": "/y", "source": "acme/hub-plugin-loose", "web": "a.js"}}
        with mock.patch.object(marketplaces, "OFFICIAL", other.bare):
            marketplaces.add(self.hub.state, self.hub.cfg_path, other.bare)
            self.assertEqual(self.entry("kanban", "zeta")["installed"], "acme:kanban")
            self.assertNotIn("installed", self.entry("kanban", "shop"))  # the core updates from the official one, and so does the page
            lines, _ = marketplaces.update(self.hub.state, self.hub.cfg_path)   # and the "can update" hint
            self.assertIn("can be updated: kanban@zeta", "\n".join(lines))
            self.assertNotIn("kanban@shop", "\n".join(lines))
        self.assertNotIn("installed", self.entry("loose"))              # not pinned: updated the old way

    def test_a_hand_broken_marketplaces_section_does_not_break_the_old_path(self):
        old_owl(self.hub, self.src.bare)
        with catalog.edit_settings(self.hub.cfg_path) as raw:
            raw["marketplaces"] = []
        cfg = self.hub.cfg()
        self.assertIsNone(marketplaces.entry_for(self.hub.state, cfg, "someone:owl"))
        self.assertFalse(marketplaces.official_ready(self.hub.state, cfg))
        (line,) = marketplaces.search(self.hub.state, self.hub.cfg_path, [])   # the reason, not a crash
        self.assertIn("marketplaces", line)

    def test_hub_market_search_does_not_say_nothing_above_what_it_found(self):
        link = [{"repo": "acme/kanban", "name": "kanban", "kinds": ["page"], "trusted": False, "link": True,
                 "installed": False, "description": "board"}]
        with mock.patch.object(market.Market, "search", return_value=link), \
                mock.patch.dict(os.environ, {"HUB_STATE_DIR": self.hub.state, "HUB_CONFIG": self.hub.cfg_path}), \
                mock.patch("sys.stdout") as out:
            market.main(["search", "kanban"])
        text = "".join(c.args[0] for c in out.write.call_args_list)
        self.assertIn("acme/kanban", text)
        self.assertNotIn("nothing found", text)


class OldCommands(Base):
    def market(self, **kw):
        return market.Market(self.hub.cfg_path, os.path.join(self.hub.state, "market"), restart=self.started.append,
                             events=lambda kind, data: self.events.append((kind, data)), **kw)

    def test_update_of_an_old_record_from_the_catalog_moves_it_to_the_new_store_at_the_catalog_version(self):
        path = old_owl(self.hub, self.src.bare, disabled=True)
        text = self.market().update("someone:owl")
        ext = self.hub.cfg()["extensions"]["someone:owl"]                  # the installed key does not change
        commit = subprocess.run(["git", "-C", self.src.src.work, "rev-parse", "HEAD"], capture_output=True,
                                text=True).stdout.strip()
        self.assertEqual((ext["market"], ext["entry"], ext["commit"], ext["folder"], ext["disabled"]),
                         ("shop", "owl", commit, "mine", True))                 # own fields and "disabled" stay as they were
        self.assertTrue(ext["path"].startswith(os.path.join(self.hub.state, "plugins") + os.sep), ext["path"])
        self.assertFalse(os.path.exists(path))                              # the old folder is gone
        self.assertIn("owl@shop", text)

    def test_an_old_skills_set_without_a_commit_is_updated_not_installed(self):
        kit = {"name": "kit", "description": "Skills", "skills": True}
        self.src.commit(cat(owl(), {"name": "kit", "source": "./plugins/kit"}),
                        {"plugins/kit/hub-plugin.json": kit,
                         "plugins/kit/.claude-plugin/marketplace.json": {"name": "kit", "plugins": []}})
        marketplaces.update(self.hub.state, self.hub.cfg_path, "shop")
        path = os.path.join(self.hub.state, "market", "someone", "kit")
        os.makedirs(path)
        with catalog.edit_settings(self.hub.cfg_path) as raw:      # a set from before catalogs: neither commit nor approved
            raw["sets"] = [{"name": "someone:kit", "path": path, "source": self.src.bare, "subdir": "plugins/kit"}]
        self.assertEqual(marketplaces.entry_for(self.hub.state, self.hub.cfg(), "someone:kit"), "kit@shop")
        text = self.market().update("someone:kit", accept=True)      # the approved set cannot be read: go on with consent
        self.assertIn("someone:kit updated from kit@shop", text)
        self.assertNotIn("rights:", text)
        (s,) = self.hub.cfg()["sets"]
        self.assertEqual((s["name"], s["market"]), ("someone:kit", "shop"))
        self.assertFalse(os.path.exists(path))

    def test_updates_does_not_ask_the_authors_head_for_a_record_the_catalog_lists(self):
        old_owl(self.hub, self.src.bare)
        asked = []
        m = market.Market(self.hub.cfg_path, os.path.join(self.hub.state, "market"),
                          clone_url=lambda full: asked.append(full) or full)
        self.assertEqual(m.updates(), {})
        self.assertEqual(asked, [])

    def test_install_of_a_repo_the_catalog_pins_goes_through_the_catalog(self):
        self.src.commit(cat(owl(), {"name": "kanban", "source": {"github": "acme/hub-plugin-kanban", "sha": "a" * 40}}))
        marketplaces.update(self.hub.state, self.hub.cfg_path, "shop")
        with mock.patch.object(mpinstall, "install", return_value=["acme:kanban installed from kanban@shop"]) as got:
            text = self.market().install("acme/hub-plugin-kanban")
        self.assertEqual(got.call_args.args[2], "kanban@shop")
        self.assertIn("installed from kanban@shop", text)

    def test_remove_of_a_catalog_install_goes_through_the_catalog_installer(self):
        self.install()
        self.assertIn("removed", self.market().remove("shop:owl"))
        self.assertEqual(self.hub.cfg()["extensions"], {})

class Deprecated(unittest.TestCase):
    def test_hub_market_is_deprecated_in_favour_of_hub_marketplace(self):
        (line,) = version.deprecated(["hub market"])
        self.assertIn("hub marketplace", line)
        self.assertEqual(set(version.DEPRECATED), {"hub market"})   # the hub's old list and accounts are gone (S8)


class Cli(Base):
    def run_hub(self, *args):
        env = {k: v for k, v in os.environ.items() if k not in ("HUB_SESSION", "HUB_EXTENSION", "HUB_MODULE")}
        env.update(HUB_STATE_DIR=self.hub.state, HUB_CONFIG=self.hub.cfg_path, HUB_CLI_WAIT="0")
        return subprocess.run([sys.executable, HUB, *args], env=env, capture_output=True, text=True)

    def test_hub_market_update_says_the_new_command_and_moves_the_plugin(self):
        old_owl(self.hub, self.src.bare)
        r = self.run_hub("market", "update", "someone:owl")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("hub marketplace install owl@shop", r.stdout)       # what to type now
        self.assertIn("deprecated", r.stdout)
        self.assertEqual(self.hub.cfg()["extensions"]["someone:owl"]["market"], "shop")

    def test_hub_market_search_says_the_new_command(self):
        r = self.run_hub("market", "search", "owl")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("hub marketplace search", r.stdout)
        self.assertIn("owl@shop", r.stdout)


class Page(test_web.WebTest):
    def setUp(self):
        super().setUp()
        self.cfg = os.path.join(self.state, "hub.json")
        with open(self.cfg, "w") as f:
            json.dump({}, f)
        env = mock.patch.dict(os.environ, {"HUB_CONFIG": self.cfg})
        env.start()
        self.addCleanup(env.stop)
        self.src = Catalog(self, cat(owl()), {"plugins/owl/hub-plugin.json": OWL,
                                              "plugins/owl/main.js": "export default 1\n"})
        marketplaces.add(self.state, self.cfg, self.src.bare)
        self.hub = type("H", (), {"state": self.state, "cfg_path": self.cfg})()
        self.seed()

    def test_update_of_an_old_record_from_the_page_goes_to_the_catalog_version(self):
        old_owl(self.hub, self.src.bare)
        with mock.patch("modules.market.market.Market.install") as head:
            code, body = self.req("POST", "/api/plugins/update", {"name": "someone:owl"})
        self.assertEqual(code, 200, body)
        head.assert_not_called()                                            # not the author's head
        ext = json.loads(open(self.cfg).read())["extensions"]["someone:owl"]
        self.assertEqual((ext["market"], ext["entry"]), ("shop", "owl"))


def load_tests(loader, standard, pattern):   # Page: only its own tests, not the ones inherited from WebTest
    suite = unittest.TestSuite()
    for cls in (Linked, Review, OldCommands, Deprecated, Cli, Page):
        suite.addTests(cls(n) for n in cls.__dict__ if n.startswith("test"))
    return suite


if __name__ == "__main__":
    unittest.main()
