"""Marketplaces, slice S4 "Modules from a catalog": an entry `type: module` is installed by
`hub marketplace install <entry>[@<catalog>]` — if the module is not there yet: a source in `modules.sources` exactly
at the catalog's commit (`ref` = `commit` = sha, `pinned`) with `market` and `entry`; if the module is already
installed (from a catalog or by `hub module add`): the update path to the catalog's sha — a new commit that asks for
more than was approved waits for `--accept`, the source in hub.json does not change; the `trusted` mark on a source
from a catalog has no effect. The command is the owner's word, like `hub module add`."""
import tests  # noqa: F401 — keep the machine's settings out
import os
import subprocess
import sys
import unittest
from unittest import mock

from core import catalog, journal, marketplaces, modsrc, mpinstall
from tests.test_marketplace import Catalog
from tests.test_modapi_sources import Hub, Source, git, manifest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB = os.path.join(REPO, "bin", "hub")


def folder_catalog(module=None, name="alice", **entry):
    """A catalog with a module right in its own repo (`./modules/notes`) — the entry's source is the catalog itself at its commit."""
    data = {"name": name, "plugins": [{"name": "notes", "type": "module", "source": "./modules/notes", **entry}]}
    return data, {"modules/notes/module.json": module or manifest(name="notes"), "modules/notes/main.py": "print(1)\n"}


class FromCatalog(unittest.TestCase):
    """The install code for a ready entry source ({url, sha, subdir?}) — the module's external repo."""

    def setUp(self):
        self.src, self.hub = Source(self), Hub(self, {})
        self.first = git(self.src.work, "rev-parse", "HEAD")

    def sources(self):
        return self.hub.cfg()["modules"]["sources"]

    def install(self, sha, accept=False):
        return modsrc.from_catalog(self.hub.state, self.hub.cfg_path, {"url": self.src.bare, "sha": sha},
                                   market="alice", entry="hello", accept=accept)

    def test_a_new_module_is_added_pinned_on_the_catalog_commit(self):
        second = self.src.commit({"main.py": "print('v2')\n"})   # the author's HEAD moved ahead — the catalog's sha is installed
        lines, ok = self.install(self.first)
        self.assertTrue(ok, lines)
        self.assertEqual(self.sources(), [{"git": self.src.bare, "ref": self.first, "commit": self.first,
                                           "pinned": True, "market": "alice", "entry": "hello"}])
        self.assertEqual(self.hub.cfg()["modules"]["enabled"], ["hello"])
        self.assertEqual(os.path.basename(self.hub.found()["hello"]["dir"]), self.first)
        self.assertNotEqual(second, self.first)
        self.assertIn("rights:", "\n".join(lines))                                # what the module may do — on screen

    def test_a_new_sha_with_a_new_right_waits_for_accept_and_hub_json_stays(self):
        self.install(self.first)
        before = self.hub.cfg()
        new = self.src.commit({"module.json": manifest(capabilities=["core.write"])})
        lines, ok = self.install(new)
        self.assertFalse(ok, lines)
        text = "\n".join(lines)
        self.assertIn("core.write", text)
        self.assertIn("hub marketplace install hello@alice --accept", text)
        self.assertEqual(self.hub.cfg(), before)                                  # the source is unchanged
        self.assertEqual(os.path.basename(self.hub.found()["hello"]["dir"]), self.first)
        lines, ok = self.install(new, accept=True)
        self.assertTrue(ok, lines)
        self.assertEqual(self.sources(), [{"git": self.src.bare, "ref": new, "commit": new, "pinned": True,
                                           "market": "alice", "entry": "hello"}])
        self.assertEqual(self.hub.found()["hello"]["manifest"]["capabilities"], ["core.write"])

    def test_a_new_sha_with_the_same_rights_moves_without_accept(self):
        self.install(self.first)
        new = self.src.commit({"main.py": "print('v2')\n"})
        lines, ok = self.install(new)
        self.assertTrue(ok, lines)
        self.assertEqual((self.sources()[0]["commit"], self.sources()[0]["ref"]), (new, new))
        self.assertEqual(len(self.sources()), 1)
        lines, ok = self.install(new)                                               # a repeat — already on it
        self.assertTrue(ok, lines)
        self.assertIn("already on", "\n".join(lines))

    def test_a_module_added_by_address_goes_the_update_path_not_add(self):
        # was installed by `hub module add` (a branch, no catalog) — install from a catalog does not replace the source around wider
        self.hub.add(self.src.bare)
        modsrc.trust(self.hub.state, self.hub.cfg_path, "hello")                   # trusted has no effect for a catalog
        new = self.src.commit({"module.json": manifest(capabilities=["core.write"])})
        lines, ok = self.install(new)
        self.assertFalse(ok, lines)
        self.assertEqual(self.sources(), [{"git": self.src.bare, "commit": self.first, "pinned": False,
                                           "trusted": True}])
        self.assertTrue(self.install(new, accept=True)[1])
        self.assertEqual(self.sources(), [{"git": self.src.bare, "ref": new, "commit": new, "pinned": True,
                                           "market": "alice", "entry": "hello"}])        # trusted removed

    def test_reinstall_keeps_a_module_the_owner_turned_off_and_says_so(self):
        self.install(self.first)
        with catalog.edit_settings(self.hub.cfg_path) as raw:
            raw["modules"]["enabled"] = []
        new = self.src.commit({"main.py": "print('v2')\n"})
        lines, ok = self.install(new)
        self.assertTrue(ok, lines)
        self.assertEqual(self.hub.cfg()["modules"]["enabled"], [])
        self.assertIn("disabled: hello", "\n".join(lines))

    def test_a_trusted_mark_is_not_shown_for_a_module_from_the_catalog(self):
        self.install(self.first)
        lines = modsrc.trust(self.hub.state, self.hub.cfg_path, "hello")
        self.assertIn("the mark is not set", "\n".join(lines))
        self.assertNotIn("trusted", self.sources()[0])
        self.assertNotIn("trusted", self.hub.found()["hello"])

    def test_a_hand_written_subdir_with_a_slash_is_the_same_source(self):
        root = Source(self, {"mods/hello/module.json": manifest()})
        self.hub.add(root.bare, subdir="mods/hello/")
        head = git(root.work, "rev-parse", "HEAD")
        lines, ok = modsrc.from_catalog(self.hub.state, self.hub.cfg_path,
                                        {"url": root.bare, "sha": head, "subdir": "mods/hello"}, market="alice",
                                        entry="hello")
        self.assertTrue(ok, lines)
        self.assertEqual(len(self.sources()), 1)
        self.assertIn("now from the catalog alice", "\n".join(lines))
        self.assertEqual(self.sources()[0]["market"], "alice")

    def test_hub_module_add_stores_a_subdir_the_way_the_catalog_does(self):
        root = Source(self, {"mods/hello/module.json": manifest()})
        self.hub.add(root.bare, subdir="mods/hello/")
        self.assertEqual(self.sources()[0]["subdir"], "mods/hello")              # as in a catalog entry
        self.hub.add(root.bare, subdir="mods\\hello")                            # the same source — not a second one
        self.assertEqual([s["subdir"] for s in self.sources()], ["mods/hello"])

    def test_going_back_to_an_older_catalog_commit_is_a_rollback(self):
        with mock.patch.dict(os.environ, {"GIT_COMMITTER_DATE": "2030-01-01T00:00:00 +0000"}):
            new = self.src.commit({"main.py": "print('v2')\n"})
        self.install(new)
        lines, ok = self.install(self.first)
        self.assertTrue(ok, lines)
        text = "\n".join(lines)
        self.assertIn("rolled back", text)
        self.assertNotIn("now on", text)
        self.assertEqual(self.sources()[0]["commit"], self.first)
        lines, ok = self.install(new)                                              # and forward again — the usual case
        self.assertIn("now on", "\n".join(lines))

    def test_rights_the_catalog_names_for_a_module_are_checked_against_its_manifest(self):
        said = {"capabilities": ["core.write"]}
        lines, ok = modsrc.from_catalog(self.hub.state, self.hub.cfg_path, {"url": self.src.bare, "sha": self.first},
                                        market="alice", entry="hello", rights=said)
        self.assertTrue(ok, lines)                                                 # promised more — allowed
        new = self.src.commit({"module.json": manifest(capabilities=["core.write", "core.model"], env=["X_KEY"])})
        lines, ok = modsrc.from_catalog(self.hub.state, self.hub.cfg_path, {"url": self.src.bare, "sha": new},
                                        market="alice", entry="hello", accept=True, rights=said)
        self.assertFalse(ok, lines)                                                # even with --accept: the catalog lies
        text = "\n".join(lines)
        self.assertIn("promises less", text)
        self.assertIn("core.model", text)
        self.assertIn("X_KEY", text)
        self.assertEqual(self.sources()[0]["commit"], self.first)
        hub2 = Hub(self, {})
        with self.assertRaises(RuntimeError) as e:                                 # and on the first install
            modsrc.from_catalog(hub2.state, hub2.cfg_path, {"url": self.src.bare, "sha": new}, market="alice",
                                entry="hello", rights={"capabilities": []})
        self.assertIn("promises less", str(e.exception))
        self.assertNotIn("modules", hub2.cfg())
        self.assertEqual(os.listdir(os.path.join(hub2.state, modsrc.DIR)), [".lock"])

    def test_a_module_already_at_the_catalog_commit_is_checked_against_the_catalogs_rights_too(self):
        new = self.src.commit({"module.json": manifest(capabilities=["core.write"])})
        self.hub.add(self.src.bare)                                                # installed by a direct address at new
        lines, ok = modsrc.from_catalog(self.hub.state, self.hub.cfg_path, {"url": self.src.bare, "sha": new},
                                        market="alice", entry="hello", rights={"capabilities": []})
        self.assertFalse(ok, lines)
        self.assertIn("promises less", "\n".join(lines))
        self.assertNotIn("market", self.sources()[0])                              # did not become "from a catalog"

    def test_a_clone_of_a_refused_new_source_does_not_stay_on_disk(self):
        with self.assertRaises(RuntimeError):
            self.install("b" * 40)
        self.assertEqual(os.listdir(os.path.join(self.hub.state, modsrc.DIR)), [".lock"])

    def test_a_refused_new_source_without_module_json_leaves_nothing_on_disk(self):
        bare = Source(self, {"README.md": "x"})
        with self.assertRaises(RuntimeError):
            modsrc.from_catalog(self.hub.state, self.hub.cfg_path,
                                {"url": bare.bare, "sha": git(bare.work, "rev-parse", "HEAD")}, market="a", entry="x")
        self.assertEqual(os.listdir(os.path.join(self.hub.state, modsrc.DIR)), [".lock"])

    def test_the_same_repo_by_another_spelling_of_its_address_is_the_same_source(self):
        self.hub.add(self.src.bare)
        modsrc.from_catalog(self.hub.state, self.hub.cfg_path, {"url": self.src.bare + os.sep, "sha": self.first},
                            market="alice", entry="hello")
        self.assertEqual([s["git"] for s in self.sources()], [self.src.bare])

    def test_trust_does_not_skip_accept_for_a_module_from_the_catalog(self):
        self.install(self.first)
        modsrc.trust(self.hub.state, self.hub.cfg_path, "hello")
        new = self.src.commit({"module.json": manifest(capabilities=["core.write"])})
        self.assertFalse(self.install(new)[1])
        self.assertEqual(self.sources()[0]["commit"], self.first)

    def test_a_sha_the_author_no_longer_has_is_refused_and_nothing_changes(self):
        self.install(self.first)
        before = self.hub.cfg()
        lines, ok = self.install("b" * 40)                                       # already installed — the update path
        self.assertFalse(ok, lines)
        self.assertIn("the author does not have", "\n".join(lines))
        self.assertEqual(self.hub.cfg(), before)
        hub2 = Hub(self, {})                                                       # not installed yet — a refusal
        with self.assertRaises(RuntimeError) as e:
            modsrc.from_catalog(hub2.state, hub2.cfg_path, {"url": self.src.bare, "sha": "b" * 40}, market="alice",
                                entry="hello")
        self.assertIn("the author does not have", str(e.exception))
        self.assertNotIn("modules", hub2.cfg())

    def test_a_source_without_module_json_is_refused(self):
        bare = Source(self, {"README.md": "x"})
        with self.assertRaises(RuntimeError):
            modsrc.from_catalog(self.hub.state, self.hub.cfg_path,
                                {"url": bare.bare, "sha": git(bare.work, "rev-parse", "HEAD")}, market="a", entry="x")
        self.assertNotIn("modules", self.hub.cfg())


class Install(unittest.TestCase):
    """`mpinstall.install` for a module entry of a connected catalog — the same revocation and sha checks as for a plugin."""

    def setUp(self):
        self.hub = Hub(self, {})

    def connect(self, data, files):
        src = Catalog(self, data, files)
        marketplaces.add(self.hub.state, self.hub.cfg_path, src.bare, now=1)
        return src

    def test_a_module_entry_inside_the_catalog_is_installed_and_listed_as_installed(self):
        src = self.connect(*folder_catalog())
        commit = git(src.src.work, "rev-parse", "HEAD")
        lines = mpinstall.install(self.hub.state, self.hub.cfg_path, "notes")
        self.assertIn("notes", "\n".join(lines))
        self.assertEqual(self.hub.cfg()["modules"]["sources"],
                         [{"git": os.path.abspath(src.bare), "ref": commit, "subdir": "modules/notes",
                           "commit": commit, "pinned": True, "market": "alice", "entry": "notes"}])
        self.assertEqual(self.hub.cfg()["modules"]["enabled"], ["notes"])
        self.assertIn("installed", "\n".join(marketplaces.listing(self.hub.state, self.hub.cfg_path, "alice")))

    def test_reinstall_after_a_catalog_commit_with_a_new_right_is_refused_without_accept(self):
        src = self.connect(*folder_catalog())
        mpinstall.install(self.hub.state, self.hub.cfg_path, "notes@alice")
        before = self.hub.cfg()["modules"]
        data, files = folder_catalog(manifest(name="notes", capabilities=["core.write"]))
        src.commit(data, files)
        marketplaces.update(self.hub.state, self.hub.cfg_path, now=2)
        with self.assertRaises(mpinstall.NeedsConsent) as e:
            mpinstall.install(self.hub.state, self.hub.cfg_path, "notes@alice")
        self.assertIn("--accept", str(e.exception))
        self.assertTrue(any("core.write" in w for w in e.exception.shown), e.exception.shown)
        self.assertEqual(self.hub.cfg()["modules"], before)
        mpinstall.install(self.hub.state, self.hub.cfg_path, "notes@alice", accept=True)
        self.assertEqual(self.hub.cfg()["modules"]["sources"][0]["commit"],
                         self.hub.cfg()["marketplaces"]["alice"]["commit"])

    def test_revoked_and_unpinned_module_entries_are_not_installed(self):
        self.connect(*folder_catalog(revoked="found a vulnerability"))
        with self.assertRaises(mpinstall.Error) as e:
            mpinstall.install(self.hub.state, self.hub.cfg_path, "notes")
        self.assertIn("revoked", str(e.exception))
        self.assertNotIn("modules", self.hub.cfg())
        lines = mpinstall.install(self.hub.state, self.hub.cfg_path, "notes", despite_revoked=True)   # the owner's word
        self.assertIn("found a vulnerability", "\n".join(lines))
        self.assertEqual(self.hub.cfg()["modules"]["sources"][0]["entry"], "notes")
        hub2 = Hub(self, {})
        src = Catalog(self, {"name": "bob", "plugins": [
            {"name": "notes", "type": "module", "source": {"git": "https://example.org/notes.git"}}]})
        marketplaces.add(hub2.state, hub2.cfg_path, src.bare, now=1)
        with self.assertRaises(mpinstall.Error) as e:
            mpinstall.install(hub2.state, hub2.cfg_path, "notes@bob")
        self.assertIn("not pinned", str(e.exception))
        self.assertNotIn("hub market review", str(e.exception))   # the code review is the plugins' path, not modules'
        self.assertNotIn("modules", hub2.cfg())

    def test_module_rights_in_the_catalog_reach_the_install_and_the_listing(self):
        self.connect(*folder_catalog(module=manifest(name="notes", capabilities=["core.write"]),
                                     rights={"capabilities": []}))
        with self.assertRaises(mpinstall.Error) as e:
            mpinstall.install(self.hub.state, self.hub.cfg_path, "notes")
        self.assertIn("promises less", str(e.exception))
        self.assertNotIn("modules", self.hub.cfg())
        hub2 = Hub(self, {})
        src = Catalog(self, *folder_catalog(module=manifest(name="notes", capabilities=["core.write"]),
                                             rights={"capabilities": ["core.write"]}))
        marketplaces.add(hub2.state, hub2.cfg_path, src.bare, now=1)
        self.assertIn("core rights: core.write", "\n".join(marketplaces.listing(hub2.state, hub2.cfg_path, "alice")))
        mpinstall.install(hub2.state, hub2.cfg_path, "notes")
        self.assertEqual(hub2.cfg()["modules"]["sources"][0]["entry"], "notes")

    def test_module_rights_of_a_wrong_shape_are_a_warning_of_the_validator(self):
        from core import marketplace
        got = marketplace.parse({"name": "alice", "plugins": [
            {"name": "notes", "type": "module", "source": "./notes", "rights": {"capabilities": "core.write"}}]})
        (e,) = got["entries"]
        self.assertEqual(e["rights"], {"capabilities": []})   # a malformed promise — nothing is promised, not "skip the check"
        self.assertIn("rights", " ".join(e["warnings"]))

    def test_an_entry_of_a_catalog_that_does_not_read_names_the_reason(self):
        self.connect(*folder_catalog())
        with catalog.edit_settings(self.hub.cfg_path) as raw:
            raw["marketplaces"]["alice"]["commit"] = "c" * 40
        with self.assertRaises(RuntimeError) as e:
            mpinstall.install(self.hub.state, self.hub.cfg_path, "notes")
        self.assertIn("unreadable: alice", str(e.exception))

    def test_an_unknown_entry_and_a_name_in_two_catalogs(self):
        self.connect(*folder_catalog())
        with self.assertRaises(RuntimeError):
            mpinstall.install(self.hub.state, self.hub.cfg_path, "ghost")
        self.connect(*folder_catalog(name="bob"))
        with self.assertRaises(RuntimeError) as e:
            mpinstall.install(self.hub.state, self.hub.cfg_path, "notes")
        self.assertIn("notes@bob", str(e.exception))
        mpinstall.install(self.hub.state, self.hub.cfg_path, "notes@bob")
        self.assertEqual(self.hub.cfg()["modules"]["sources"][0]["market"], "bob")

    def test_uninstall_of_a_module_names_hub_module_remove(self):
        self.connect(*folder_catalog())
        mpinstall.install(self.hub.state, self.hub.cfg_path, "notes")
        with self.assertRaises(mpinstall.Error) as e:
            mpinstall.uninstall(self.hub.state, self.hub.cfg_path, "notes@alice")
        self.assertIn("hub module remove", str(e.exception))
        self.assertEqual(len(self.hub.cfg()["modules"]["sources"]), 1)


class Cli(unittest.TestCase):
    """`hub marketplace install` — the owner's word, like `hub module add`."""

    def setUp(self):
        self.hub = Hub(self, {})
        j = journal.Journal(os.path.join(self.hub.state, "journal.jsonl"))
        j.append("session.requested", "hub", {"cwd": "/h", "kind": "hub", "channel": "cli", "owner": True})
        j.append("session.requested", "w", {"cwd": "/w", "channel": "cli", "owner": True})
        self.word = "m" + str(j.append("message.received", None, {"to": "hub", "text": "install the notes module, notes@alice",
                                                                     "channel": "cli", "owner": True})["seq"])
        src = Catalog(self, *folder_catalog())
        marketplaces.add(self.hub.state, self.hub.cfg_path, src.bare, now=1)

    def run_hub(self, *args, me=None, module=None):
        env = {k: v for k, v in os.environ.items() if k not in ("HUB_SESSION", "HUB_EXTENSION", "HUB_MODULE")}
        env.update(HUB_STATE_DIR=self.hub.state, HUB_CONFIG=self.hub.cfg_path, HUB_CLI_WAIT="0",
                   **({"HUB_SESSION": me} if me else {}), **({"HUB_MODULE": module} if module else {}))
        return subprocess.run([sys.executable, HUB, "marketplace", *args], env=env, capture_output=True, text=True)

    def test_only_the_owners_word_installs(self):
        for who in ({"me": "w"}, {"me": "hub"}, {"me": "hub", "extra": ["--for", "m999"]}, {"module": "x"},
                    {"me": "w", "extra": ["--for", self.word]}):
            with self.subTest(who=who):
                extra = who.pop("extra", [])
                r = self.run_hub("install", "notes", *extra, **who)
                self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
                out = r.stdout + r.stderr   # a session's refusal is in English, a module's in the owner's language
                self.assertIn("owner", out)
        self.assertNotIn("modules", self.hub.cfg())
        # a catalog the hub added itself names its entries as it likes: its entry is named in full (18.11)
        j = journal.Journal(os.path.join(self.hub.state, "journal.jsonl"))
        short = "m" + str(j.append("message.received", None, {"to": "hub", "text": "install the notes module",
                                                               "channel": "cli", "owner": True})["seq"])
        r = self.run_hub("install", "notes", "--for", short, me="hub")
        self.assertIn("do not name notes@alice", r.stdout + r.stderr)
        with catalog.edit_settings(self.hub.cfg_path) as raw:   # the mark alone, not the official address
            raw["marketplaces"]["alice"]["official"] = True
        r = self.run_hub("install", "notes", "--for", short, me="hub")
        self.assertIn("do not name notes@alice", r.stdout + r.stderr)
        with catalog.edit_settings(self.hub.cfg_path) as raw:
            raw["marketplaces"]["alice"].pop("official")
        self.assertNotIn("modules", self.hub.cfg())
        r = self.run_hub("install", "notes@alice", "--for", self.word, me="hub")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.hub.cfg()["modules"]["sources"][0]["entry"], "notes")
        self.assertEqual(self.hub.cfg()["modules"]["enabled"], ["notes"])

    def test_accept_and_despite_revoked_from_the_hub_need_a_card_answer_for_a_module_too(self):
        for flag in ("--accept", "--despite-revoked"):
            with self.subTest(flag=flag):
                r = self.run_hub("install", "notes@alice", flag, "--for", self.word, me="hub")   # mN is not a card answer
                self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn("qN", r.stdout + r.stderr)
        self.assertNotIn("modules", self.hub.cfg())


if __name__ == "__main__":
    unittest.main()
