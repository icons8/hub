"""Marketplaces, slice S2 "connected catalogs" (core/marketplaces.py): `hub marketplace add|list|remove|update|search`
— catalogs in hub.json `marketplaces`; changing commands (and the old `hub market add|install|update|remove`) need the
owner's word, as with `hub module add`: a command outside a session, or the hub with `--for mN|qN`; a plugin or module
process — never. Adding the same source again gives the same catalog; an unreachable catalog is an error with a reason,
the previous commit keeps working; the hub connects the official catalog itself (if it can't — `pending` with a reason)
and does not bring it back once the owner removed it."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import platforms
from core import catalog, codestore, journal, marketplace, marketplaces
from tests.test_marketplace import Catalog, entry, listed
from tests.test_modapi_sources import Hub, git

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB = os.path.join(REPO, "bin", "hub")
SHA = "3f1c" + "0" * 36
SHA2 = "a" * 40


def cat(name="alice", *entries, **top):
    return {"name": name, "plugins": list(entries), **top}


class Cli(unittest.TestCase):
    """bin/hub in the hub's state folder: a journal with the hub and a session, the owner's word to the hub."""

    def setUp(self):
        self.hub = Hub(self, {})
        j = journal.Journal(os.path.join(self.hub.state, "journal.jsonl"))
        j.append("session.requested", "hub", {"cwd": "/h", "kind": "hub", "channel": "cli", "owner": True})
        j.append("session.requested", "w", {"cwd": "/w", "channel": "cli", "owner": True})
        self.word = "m" + str(j.append("message.received", None, {"to": "hub", "text": "connect the catalog",
                                                                     "channel": "cli", "owner": True})["seq"])

    def run_hub(self, *args, me=None, module=None, plugin=None):
        env = {k: v for k, v in os.environ.items() if k not in ("HUB_SESSION", "HUB_EXTENSION", "HUB_MODULE")}
        env.update(HUB_STATE_DIR=self.hub.state, HUB_CONFIG=self.hub.cfg_path, HUB_CLI_WAIT="0",
                   **({"HUB_SESSION": me} if me else {}), **({"HUB_MODULE": module} if module else {}),
                   **({"HUB_EXTENSION": plugin} if plugin else {}))
        return subprocess.run([sys.executable, HUB, *args], env=env, capture_output=True, text=True)

    def refused(self, r):
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        out = r.stdout + r.stderr   # a session's refusal is in English, a module's or plugin's in the owner's language
        self.assertIn("owner", out)

    def test_changing_commands_are_the_owners_word(self):
        """Catalogs as lists of entries: the owner, the hub and agent-dev (owner 2026-11-17, skills out of the box —
        tests/test_skillsets.py); a working session, a module or a plugin process — never, --for or not."""
        src = Catalog(self, cat("alice", entry("x")))
        for sub in (["add", src.bare], ["remove", "alice"], ["update"], ["update", "alice"]):
            for who in ({"me": "w"}, {"me": "w", "extra": ["--for", self.word]}, {"module": "notes"},
                        {"plugin": "alice:x"}, {"module": "notes", "extra": ["--for", self.word]}):
                with self.subTest(sub=sub, who=who):
                    extra = who.pop("extra", [])
                    self.refused(self.run_hub("marketplace", *sub, *extra, **who))
        self.assertEqual(self.hub.cfg(), {})
        self.assertFalse(os.path.isdir(marketplace.store_root(self.hub.state)))
        r = self.run_hub("marketplace", "add", src.bare, me="hub")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(list(self.hub.cfg()["marketplaces"]), ["alice"])
        self.assertEqual(self.run_hub("marketplace", "update", me="hub").returncode, 0)
        self.assertEqual(self.run_hub("marketplace", "update").returncode, 0)                  # owner
        for who in ({"me": "w"}, {"module": "notes"}, {"plugin": "alice:x"}):                   # reading — anyone
            for sub in (["list"], ["list", "alice"], ["search", "x"]):
                r = self.run_hub("marketplace", *sub, **who)
                self.assertEqual(r.returncode, 0, (sub, who, r.stdout + r.stderr))
        r = self.run_hub("marketplace", "remove", "alice")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.hub.cfg()["marketplaces"], {})

    def said_to(self, to, text="remove the plugin", age=0, owner=True):
        j = journal.Journal(os.path.join(self.hub.state, "journal.jsonl"))
        return "m" + str(j.append("message.received", None, {"to": to, "text": text, "channel": "cli", "owner": owner},
                                  ts=time.time() - age)["seq"])

    def installed(self):
        with catalog.edit_settings(self.hub.cfg_path) as raw:
            raw["extensions"] = {"alice:x": {"source": "https://example.org/x.git", "entry": "x", "market": "alice"}}

    def test_taking_off_takes_the_owners_word_to_any_session_for_the_hub_only(self):
        """The owner wrote "remove the plugin" to the session of its area, it passed this to the hub: for taking off
        (uninstall, remove) the hub's --for mN may be his message to any session; installing and the rest stay."""
        elsewhere = self.said_to("w")
        for who, extra in ((dict(me="w"), ["--for", elsewhere]), (dict(module="notes"), ["--for", elsewhere]),
                           (dict(plugin="alice:x"), ["--for", elsewhere]), (dict(me="hub"), [])):
            self.installed()
            with self.subTest(uninstall=who):
                self.refused(self.run_hub("marketplace", "uninstall", "x", *extra, **who))
                self.assertIn("alice:x", self.hub.cfg()["extensions"])
        for sub in (["install", "x@alice"],):   # not taking off (a catalog as a list is not the owner's word any more)
            with self.subTest(sub=sub):
                r = self.run_hub("marketplace", *sub, "--for", elsewhere, me="hub")
                self.refused(r)
                self.assertIn("the owner wrote it to w, not to you", r.stdout + r.stderr)
        r = self.run_hub("module", "add", "https://example.org/m.git", "--for", elsewhere, me="hub")
        self.assertIn("the owner wrote it to w, not to you", r.stdout + r.stderr)
        r = self.run_hub("marketplace", "uninstall", "x", "--for", elsewhere, me="hub")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.hub.cfg()["extensions"], {})
        src = Catalog(self, cat("alice", entry("x")))
        self.assertEqual(self.run_hub("marketplace", "add", src.bare, "--for", self.word, me="hub").returncode, 0)
        r = self.run_hub("marketplace", "remove", "alice", "--for", elsewhere, me="hub")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.hub.cfg()["marketplaces"], {})
        for sub in (["module", "remove", "notes"], ["market", "remove", "x"]):
            r = self.run_hub(*sub, "--for", elsewhere, me="hub")   # past the gate: nothing to remove follows, not its refusal
            with self.subTest(sub=sub):
                self.assertNotEqual(r.stdout + r.stderr, "")
                self.assertNotIn("the owner's word", r.stdout + r.stderr)
                self.assertNotIn("not to you", r.stdout + r.stderr)
                self.assertNotIn("--for", r.stdout + r.stderr)

    def test_taking_off_by_a_word_that_is_not_one(self):
        self.installed()
        for what, ref in (("older than a day", self.said_to("w", age=2 * 86400)),
                          ("not the owner's", self.said_to("w", owner=False)), ("no such", "m9999")):
            with self.subTest(what):
                self.refused(self.run_hub("marketplace", "uninstall", "x", "--for", ref, me="hub"))
                self.assertIn("alice:x", self.hub.cfg()["extensions"])

    def test_the_old_market_commands_pass_the_same_gate(self):
        # was: `install|update|remove` — the hub without the owner's word
        for sub in (["install", "alice/hub-plugin-x"], ["update", "x"], ["remove", "x"]):
            for who in ({"me": "hub"}, {"me": "w"}, {"plugin": "alice:x"}, {"module": "notes"},
                        {"me": "hub", "extra": ["--for", "m999"]}):
                with self.subTest(sub=sub, who=who):
                    extra = who.pop("extra", [])
                    self.refused(self.run_hub("market", *sub, *extra, **who))
        self.assertEqual(self.hub.cfg(), {})
        r = self.run_hub("market", "add", "alice", "--for", self.word, me="hub")   # a GitHub account: gone (S8)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("hub marketplace add", r.stdout)
        self.assertEqual(self.hub.cfg(), {})

    def test_list_and_search_print_what_the_catalogs_hold(self):
        src = Catalog(self, cat("alice", entry("mascot", description="A talisman on the page", version="1.2.0",
                                                rights={"page": True}),
                                {"name": "notes", "type": "module",
                                 "source": {"git": "https://example.org/notes.git", "sha": SHA2}},
                                {"name": "bad", "source": {"github": "a/b", "sha": "1"}}))
        self.assertEqual(self.run_hub("marketplace", "add", src.bare).returncode, 0)
        out = self.run_hub("marketplace", "list").stdout
        self.assertIn("alice", out)
        self.assertIn("entries 2", out)
        out = self.run_hub("marketplace", "list", "alice").stdout
        for text in ("mascot@alice", "1.2.0", "full access","notes@alice", "module", "bad", "sha"):
            self.assertIn(text, out)
        out = self.run_hub("marketplace", "search", "talisman").stdout
        self.assertIn("mascot@alice", out)
        self.assertNotIn("notes@alice", out)
        self.assertIn("nothing", self.run_hub("marketplace", "search", "no-such-thing").stdout)
        r = self.run_hub("marketplace", "list", "nobody")
        self.assertNotEqual(r.returncode, 0)


class Connected(unittest.TestCase):
    def setUp(self):
        self.hub = Hub(self, {})

    def mps(self):
        return self.hub.cfg().get("marketplaces") or {}

    def test_add_writes_the_catalog_and_a_second_add_of_the_same_source_refreshes_it(self):
        src = Catalog(self, cat("alice", entry("x")))
        lines = marketplaces.add(self.hub.state, self.hub.cfg_path, src.bare, now=100)
        first = git(src.src.work, "rev-parse", "HEAD")
        self.assertEqual(self.mps(), {"alice": {"source": os.path.abspath(src.bare), "commit": first, "updated": 100}})
        self.assertIn("someone else's code", "\n".join(lines))                       # third-party catalog — a warning
        second = src.commit(cat("alice", entry("x"), entry("y")))
        lines = marketplaces.add(self.hub.state, self.hub.cfg_path, src.bare + os.sep, now=200)
        self.assertIn("is already connected", "\n".join(lines))
        self.assertEqual(list(self.mps()), ["alice"])
        self.assertEqual((self.mps()["alice"]["commit"], self.mps()["alice"]["updated"]), (second, 200))
        self.assertEqual(listed(marketplace.store_root(self.hub.state)), ["alice"])

    def test_the_same_name_from_another_source_is_refused(self):
        marketplaces.add(self.hub.state, self.hub.cfg_path, Catalog(self, cat("alice")).bare, now=1)
        before = self.mps()
        with self.assertRaises(RuntimeError) as e:
            marketplaces.add(self.hub.state, self.hub.cfg_path, Catalog(self, cat("alice")).bare, now=2)
        self.assertIn("already taken", str(e.exception))
        self.assertEqual(self.mps(), before)

    def test_sources_a_catalog_is_added_from(self):
        url = marketplaces.url_of
        self.assertEqual(url("alice/hub-stuff"), "https://github.com/alice/hub-stuff.git")
        self.assertEqual(url("alice/hub-stuff.git"), "https://github.com/alice/hub-stuff.git")
        self.assertEqual(url("https://example.org/x.git"), "https://example.org/x.git")
        self.assertEqual(url("git@example.org:x/y.git"), "git@example.org:x/y.git")
        folder = self.hub.state
        self.assertEqual(url(folder), os.path.abspath(folder))
        for bad in ("", "-x", "./nope-" + "z" * 8, "ext::sh -c x", "file:///tmp/x", "a b/c", "alice"):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                url(bad)
        self.assertEqual(marketplaces.same("https://github.com/Alice/Hub-Stuff", "https://github.com/alice/hub-stuff.git"),
                         True)
        self.assertEqual(marketplaces.same("https://example.org/A.git", "https://example.org/a.git"), False)

    def test_an_unavailable_catalog_is_an_error_and_the_last_commit_keeps_working(self):
        missing = os.path.join(self.hub.state, "nowhere.git")
        with self.assertRaises(RuntimeError):
            marketplaces.add(self.hub.state, self.hub.cfg_path, self.hub.state, now=1)    # the folder is not a git repo
        self.assertEqual(self.hub.cfg(), {})
        src = Catalog(self, cat("alice", entry("x")))
        marketplaces.add(self.hub.state, self.hub.cfg_path, src.bare, now=1)
        first = self.mps()["alice"]["commit"]
        os.rename(src.bare, missing)
        lines, ok = marketplaces.update(self.hub.state, self.hub.cfg_path, now=50)
        self.assertFalse(ok)
        m = self.mps()["alice"]
        self.assertEqual((m["commit"], m["updated"], m["tried"]), (first, 1, 50))
        self.assertTrue(m["error"])
        self.assertIn("not updated", "\n".join(lines))
        self.assertIn("not updated", "\n".join(marketplaces.listing(self.hub.state, self.hub.cfg_path)))
        self.assertIn("x@alice", "\n".join(marketplaces.listing(self.hub.state, self.hub.cfg_path, "alice")))
        os.rename(missing, src.bare)                                              # it is back — the error is cleared
        src.commit(cat("alice", entry("x"), entry("y")))
        lines, ok = marketplaces.update(self.hub.state, self.hub.cfg_path, "alice", now=60)
        self.assertTrue(ok, lines)
        self.assertNotIn("error", self.mps()["alice"])
        self.assertNotIn("tried", self.mps()["alice"])

    def test_broken_json_keeps_the_previous_commit(self):
        src = Catalog(self, cat("alice", entry("x")))
        marketplaces.add(self.hub.state, self.hub.cfg_path, src.bare, now=1)
        first = self.mps()["alice"]["commit"]
        src.src.commit({marketplace.FILE: '{"name": "alice", "plugins": ['})
        lines, ok = marketplaces.update(self.hub.state, self.hub.cfg_path, "alice", now=2)
        self.assertFalse(ok)
        self.assertIn("JSON", "\n".join(lines))
        self.assertEqual(self.mps()["alice"]["commit"], first)

    def test_remove_disconnects_the_catalog_and_leaves_what_was_installed(self):
        src = Catalog(self, cat("alice", entry("x")))
        marketplaces.add(self.hub.state, self.hub.cfg_path, src.bare, now=1)
        with catalog.edit_settings(self.hub.cfg_path) as raw:
            raw["extensions"] = {"alice:x": {"path": "/p", "source": "alice/hub-plugin-x", "commit": SHA}}
        lines = marketplaces.remove(self.hub.state, self.hub.cfg_path, "alice")
        self.assertIn("alice:x", "\n".join(lines))
        self.assertEqual(self.mps(), {})
        self.assertIn("alice:x", self.hub.cfg()["extensions"])
        self.assertNotIn("marketplaces_removed", self.hub.cfg())
        self.assertEqual(listed(marketplace.store_root(self.hub.state)), [])
        with self.assertRaises(RuntimeError):
            marketplaces.remove(self.hub.state, self.hub.cfg_path, "alice")
        with self.assertRaises(RuntimeError):
            marketplaces.remove(self.hub.state, self.hub.cfg_path, "../x")

    def test_a_wrong_ref_on_a_second_add_is_not_kept(self):
        src = Catalog(self, cat("alice", entry("x")))
        marketplaces.add(self.hub.state, self.hub.cfg_path, src.bare, now=1)
        before = self.mps()
        with self.assertRaises(RuntimeError):
            marketplaces.add(self.hub.state, self.hub.cfg_path, src.bare, ref="no-such-branch", now=2)
        self.assertEqual(self.mps(), before)

    def test_hand_edited_records_do_not_break_commands_or_loop(self):
        src = Catalog(self, cat("alice", entry("x")))
        marketplaces.add(self.hub.state, self.hub.cfg_path, src.bare, now=1)
        with catalog.edit_settings(self.hub.cfg_path) as raw:
            raw["marketplaces"].update({"bare": {}, "nosrc": {"commit": SHA}, "Odd-Name": {"source": "/x"},
                                        "texty": {"source": "/y", "updated": "yesterday", "tried": None}})
        cfg = self.hub.cfg()
        self.assertTrue(marketplaces.due(cfg, 10 ** 9))                           # "yesterday" (a string) — long ago, not a TypeError
        cfg["marketplaces"] = {k: cfg["marketplaces"][k] for k in ("bare", "nosrc")}
        self.assertFalse(marketplaces.due(cfg, 10 ** 9))                          # no source — not on every turn
        text = "\n".join(marketplaces.listing(self.hub.state, self.hub.cfg_path))
        self.assertIn("no source", text)
        with self.assertRaises(RuntimeError):
            marketplaces.listing(self.hub.state, self.hub.cfg_path, "nosrc")
        self.assertIn("x@alice", "\n".join(marketplaces.search(self.hub.state, self.hub.cfg_path, [])))
        lines = marketplaces.remove(self.hub.state, self.hub.cfg_path, "Odd-Name")    # a name typed by hand — still removed
        self.assertIn("disconnected", "\n".join(lines))
        self.assertNotIn("Odd-Name", self.mps())
        with open(self.hub.cfg_path, encoding="utf-8") as f:                    # numbers that json lets through
            text = f.read()
        alice = json.dumps(self.mps()["alice"]["source"])
        with open(self.hub.cfg_path, "w", encoding="utf-8") as f:
            f.write(text.replace('"marketplaces": {', '"marketplaces": {"huge": {"source": ' + alice + ', "commit": 7, '
                                 '"updated": 1e20, "tried": Infinity}, "nan": {"source": "/z", "updated": NaN}, ', 1))
        self.assertIn("huge", "\n".join(marketplaces.listing(self.hub.state, self.hub.cfg_path)))
        self.assertTrue(marketplaces.due(catalog.read_settings(self.hub.cfg_path), 10 ** 9))
        lines, _ = marketplaces.update(self.hub.state, self.hub.cfg_path, "huge", now=5)
        self.assertTrue(lines)
        self.assertEqual(self.mps()["huge"]["tried"], 5)       # the catalog named itself alice — an error, the attempt is recorded
        self.assertFalse(marketplaces._stale(marketplaces._clean(self.mps()["huge"]), 6))
        self.assertTrue(marketplaces._stale({"source": "/z", "updated": 9e9}, 10))   # "in the future" — it is time

    def test_update_does_not_put_back_a_ref_changed_meanwhile(self):
        src = Catalog(self, cat("alice", entry("x")))
        marketplaces.add(self.hub.state, self.hub.cfg_path, src.bare, ref="main", now=1)
        git(src.src.work, "branch", "v2")
        git(src.src.work, "push", "-q", src.bare, "v2")
        real = marketplace.fetch

        def slow(*a, **kw):   # while downloading, the owner changed the ref with a repeated add
            got = real(*a, **kw)
            with catalog.edit_settings(self.hub.cfg_path) as raw:
                raw["marketplaces"]["alice"]["ref"] = "v2"
            return got
        before = self.mps()["alice"]["commit"]
        src.commit(cat("alice", entry("x"), entry("y")))                         # main moved ahead, v2 did not
        with mock.patch.object(marketplace, "fetch", slow):
            marketplaces.update(self.hub.state, self.hub.cfg_path, "alice", now=2)
        self.assertEqual((self.mps()["alice"]["ref"], self.mps()["alice"]["commit"]), ("v2", before))
        lines = marketplaces.add(self.hub.state, self.hub.cfg_path, src.bare, ref="main", now=3)
        self.assertIn("is already connected", "\n".join(lines))
        self.assertEqual(self.mps()["alice"]["ref"], "main")

    def test_auto_runs_one_at_a_time(self):
        src = Catalog(self, cat("alice", entry("x")))
        marketplaces.add(self.hub.state, self.hub.cfg_path, src.bare, now=1)
        held = os.path.join(marketplace.store_root(self.hub.state), marketplaces.AUTO_LOCK)
        script = ("import sys, time; sys.path.insert(0, sys.argv[1]); from core import lock\n"
                  "assert lock.hold(sys.argv[2]); print('held', flush=True); time.sleep(30)\n")
        p = subprocess.Popen([sys.executable, "-c", script, REPO, held], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (p.kill(), p.wait()))
        self.assertEqual(p.stdout.readline().strip(), "held")
        lines, ok = marketplaces.update(self.hub.state, self.hub.cfg_path, now=10 ** 9, auto=True)
        self.assertTrue(ok)
        self.assertIn("already running", "\n".join(lines))
        self.assertEqual(self.mps()["alice"]["updated"], 1)

    def test_update_names_installed_entries_the_catalog_has_a_newer_commit_for(self):
        src = Catalog(self, cat("alice", entry("x"), {"name": "notes", "type": "module",
                                                      "source": {"git": "https://example.org/n.git", "sha": SHA2}}))
        marketplaces.add(self.hub.state, self.hub.cfg_path, src.bare, now=1)
        with catalog.edit_settings(self.hub.cfg_path) as raw:
            raw["extensions"] = {"alice:x": {"path": "/p", "source": "Alice/hub-plugin-x", "commit": "b" * 40}}
            raw["modules"] = {"sources": [{"git": "https://example.org/n.git", "commit": SHA2}]}
        lines, ok = marketplaces.update(self.hub.state, self.hub.cfg_path, now=2)
        text = "\n".join(lines)
        self.assertTrue(ok, text)
        self.assertIn("x@alice", text)
        self.assertIn("alice:x", text)
        self.assertNotIn("notes@alice", text)                                   # already at the catalog's commit


class Official(unittest.TestCase):
    """The official catalog: the hub connects it itself on first start; if the download fails — pending, retry later."""

    def setUp(self):
        self.hub = Hub(self, {})
        self.src = Catalog(self, cat("agents-hub", entry("x")))
        patch = mock.patch.object(marketplaces, "OFFICIAL", self.src.bare)
        patch.start()
        self.addCleanup(patch.stop)

    def mps(self):
        return self.hub.cfg().get("marketplaces") or {}

    def test_the_first_run_connects_it_and_a_removed_one_stays_removed(self):
        self.assertTrue(marketplaces.due(self.hub.cfg(), 10))
        lines, ok = marketplaces.update(self.hub.state, self.hub.cfg_path, now=10, auto=True)
        self.assertTrue(ok, lines)
        m = self.mps()["agents-hub"]
        self.assertEqual((m["official"], m["updated"]), (True, 10))
        self.assertFalse(marketplaces.due(self.hub.cfg(), 10 + 3600))
        self.assertTrue(marketplaces.due(self.hub.cfg(), 10 + marketplaces.REFRESH))
        self.assertIn("official", "\n".join(marketplaces.listing(self.hub.state, self.hub.cfg_path)))
        marketplaces.remove(self.hub.state, self.hub.cfg_path, "agents-hub")
        self.assertEqual(self.hub.cfg()["marketplaces_removed"], ["agents-hub"])
        self.assertFalse(marketplaces.due(self.hub.cfg(), 10 ** 6))
        marketplaces.update(self.hub.state, self.hub.cfg_path, now=20, auto=True)
        self.assertEqual(self.mps(), {})
        marketplaces.add(self.hub.state, self.hub.cfg_path, self.src.bare, now=30)      # the owner brought it back themselves
        self.assertEqual(self.hub.cfg()["marketplaces_removed"], [])
        self.assertTrue(self.mps()["agents-hub"]["official"])

    def test_a_catalog_that_does_not_download_is_pending_and_retried_later(self):
        gone = self.src.bare + ".gone"
        os.rename(self.src.bare, gone)
        lines, ok = marketplaces.update(self.hub.state, self.hub.cfg_path, now=10, auto=True)
        self.assertFalse(ok)
        m = self.mps()["agents-hub"]
        self.assertTrue(m["pending"])
        self.assertEqual((m["official"], m["tried"]), (True, 10))
        self.assertNotIn("commit", m)
        self.assertIn("not downloaded", "\n".join(marketplaces.listing(self.hub.state, self.hub.cfg_path)))
        self.assertFalse(marketplaces.due(self.hub.cfg(), 11))                   # not on every hub-up turn
        self.assertTrue(marketplaces.due(self.hub.cfg(), 10 + marketplaces.REFRESH))
        os.rename(gone, self.src.bare)
        lines, ok = marketplaces.update(self.hub.state, self.hub.cfg_path, now=20)         # "Check" — right away
        self.assertTrue(ok, lines)
        m = self.mps()["agents-hub"]
        self.assertNotIn("pending", m)
        self.assertTrue(m["commit"])

    def test_auto_refreshes_only_what_is_due(self):
        marketplaces.update(self.hub.state, self.hub.cfg_path, now=10, auto=True)
        self.src.commit(cat("agents-hub", entry("x"), entry("y")))
        marketplaces.update(self.hub.state, self.hub.cfg_path, now=20, auto=True)
        self.assertEqual(self.mps()["agents-hub"]["updated"], 10)
        marketplaces.update(self.hub.state, self.hub.cfg_path, now=10 + marketplaces.REFRESH, auto=True)
        self.assertEqual(self.mps()["agents-hub"]["updated"], 10 + marketplaces.REFRESH)

    def test_the_official_name_from_a_foreign_source_is_refused(self):
        with mock.patch.object(marketplaces, "OFFICIAL", None):
            self.assertFalse(marketplaces.due(self.hub.cfg(), 10))
            with self.assertRaises(RuntimeError) as e:
                marketplaces.add(self.hub.state, self.hub.cfg_path, self.src.bare, now=1)
        self.assertIn("official", str(e.exception))
        self.assertEqual(self.hub.cfg(), {})


class OfficialSource(unittest.TestCase):
    """The official catalog: a team's tree names its own in modules/private.json (the repository of its modules, the same
    access as for them), the open tree — without that file — has the open core's catalog repository; tests and stands do
    not go to GitHub for it (HUB_NO_OFFICIAL_MARKETPLACE)."""

    def official(self, env):
        r = subprocess.run([sys.executable, "-c", "from core import marketplaces as m; print(m.OFFICIAL, m.due({}, 10))"], cwd=REPO,
                           env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_the_official_catalog_is_the_trees_own(self):
        from core import embed
        pin = embed.file("modules/private.json", REPO)   # the core's own or an embed's
        if pin:   # a team's tree: the repository its modules come from
            with open(pin, encoding="utf-8") as f:
                got = json.load(f)
            if "catalog" in got:   # Embed API 1.2: the open one, and the team's next to it — a key the core reads
                self.assertEqual(marketplaces.SOURCE, marketplaces.OPEN_SOURCE)
                self.assertEqual(marketplaces.team_of(REPO), (got["catalog"]["name"], got["repo"],
                                                              got["catalog"].get("ref")))
            else:
                self.assertEqual(marketplaces.SOURCE, got["repo"])
        else:                     # the open tree (core/appcode.OPEN_CUT leaves the file out)
            self.assertEqual(marketplaces.SOURCE, marketplaces.OPEN_SOURCE)
        source = marketplaces.SOURCE
        with mock.patch.object(marketplaces, "OFFICIAL", source):
            self.assertEqual(marketplaces.official_url(), f"https://github.com/{source}.git")
            self.assertTrue(marketplaces.is_official("https://github.com/" + source.upper()))   # GitHub case does not matter
            self.assertFalse(marketplaces.is_official(f"https://github.com/{source}-x.git"))
            self.assertFalse(marketplaces.is_official(f"https://example.org/{source}.git"))

    def test_without_a_pin_of_its_own_the_tree_has_the_open_catalog(self):
        with tempfile.TemporaryDirectory() as tree:
            self.assertEqual(marketplaces.source_of(tree), "icons8/agents-hub-plugins")   # the open tree
            os.makedirs(os.path.join(tree, "modules"))
            pin = os.path.join(tree, "modules", "private.json")
            for text, want in (('{"repo": "team/modules"}', "team/modules"),
                               ('{"repo": "team/modules/extra"}', "icons8/agents-hub-plugins"),   # not owner/name
                               ('{"repo": 7}', "icons8/agents-hub-plugins"), ("[]", "icons8/agents-hub-plugins"),
                               ("{not json", "icons8/agents-hub-plugins")):
                with open(pin, "w", encoding="utf-8") as f:
                    f.write(text)
                self.assertEqual(marketplaces.source_of(tree), want, text)

    def test_a_hub_connects_it_on_its_own_tests_and_stands_do_not(self):
        env = {k: v for k, v in os.environ.items() if k != "HUB_NO_OFFICIAL_MARKETPLACE"}
        self.assertEqual(self.official(env), f"{marketplaces.SOURCE} True")              # a new hub: hub-up downloads it
        self.assertEqual(self.official({**env, "HUB_NO_OFFICIAL_MARKETPLACE": "1"}), "None False")
        self.assertEqual(os.environ.get("HUB_NO_OFFICIAL_MARKETPLACE"), "1")   # tests/__init__: hub-up in tests does not download it


def plugin(name, author="alice", sha=SHA, **extra):
    return entry(name, source={"github": f"{author}/hub-plugin-{name}", "sha": sha}, **extra)


class TwoOfficials(unittest.TestCase):
    """Embed API 1.2: a team's tree names its catalog next to the open one (modules/private.json "catalog"). Until then
    the team's catalog was the official one under `agents-hub`; on the new core it is the second official one under its
    own name, on its branch, and `agents-hub` is the open catalog again. What was installed keeps its key and settings:
    only `market` follows the entry's name (R0 binds what had none, R1 takes the old record, R2 moves to the catalog that
    has the name)."""

    def setUp(self):
        self.hub = Hub(self, {})
        # the team's catalog repo: main — the frozen list the old cores read; the branch team — its own name, its entries
        self.team = Catalog(self, cat("agents-hub", plugin("a"), plugin("b"), plugin("t", "bob")))
        self.branch({"name": "icons8-team", "plugins": [plugin("t", "bob")]})
        self.open = Catalog(self, cat("agents-hub", plugin("a", "icons8", SHA2), plugin("b", "icons8", SHA2)))
        self.old_core()
        marketplaces.update(self.hub.state, self.hub.cfg_path, now=10, auto=True)
        self.assertEqual(self.mps()["agents-hub"]["source"], self.team.bare)

    def branch(self, data):
        work = self.team.src.work
        if git(work, "rev-parse", "--abbrev-ref", "HEAD") != "team":
            git(work, "checkout", "-q", "-B", "team")
        with open(os.path.join(work, marketplace.FILE), "w", encoding="utf-8") as f:
            json.dump(data, f)
        git(work, "commit", "-q", "-am", "team")
        git(work, "push", "-q", "-f", self.team.bare, "team")
        git(work, "checkout", "-q", "main")
        return git(work, "rev-parse", "team")

    def patch(self, **values):
        for k, v in values.items():
            p = mock.patch.object(marketplaces, k, v)
            p.start()
            self.addCleanup(p.stop)

    def old_core(self):
        self.patch(OFFICIAL=self.team.bare, TEAM=None)

    def new_core(self):
        self.patch(OFFICIAL=self.open.bare, TEAM=("icons8-team", self.team.bare, "team"))

    def mps(self):
        return self.hub.cfg().get("marketplaces") or {}

    def install(self, **exts):
        with catalog.edit_settings(self.hub.cfg_path) as raw:
            raw["extensions"] = exts

    def exts(self):
        return self.hub.cfg().get("extensions") or {}

    def auto(self, now=20):
        lines, ok = marketplaces.update(self.hub.state, self.hub.cfg_path, now=now, auto=True)
        self.assertTrue(ok, lines)
        return lines

    def test_installs_keep_their_keys_and_follow_their_names(self):
        self.install(**{"alice:a": {"source": "alice/hub-plugin-a", "commit": SHA, "market": "agents-hub", "entry": "a",
                                    "color": "red", "disabled": True},
                        "alice:b": {"source": "alice/hub-plugin-b", "commit": SHA},      # from before catalogs
                        "bob:t": {"source": "bob/hub-plugin-t", "commit": SHA, "market": "agents-hub", "entry": "t"}})
        self.new_core()
        self.auto()
        mps = self.mps()
        self.assertEqual(sorted(mps), ["agents-hub", "icons8-team"])
        self.assertEqual((mps["agents-hub"]["source"], mps["agents-hub"]["official"]), (self.open.bare, True))
        self.assertEqual((mps["icons8-team"]["source"], mps["icons8-team"]["ref"], mps["icons8-team"]["official"]),
                         (self.team.bare, "team", True))
        self.assertEqual(marketplace._origin(marketplace._repo(self.hub.state, "agents-hub")), self.open.bare)
        exts = self.exts()
        self.assertEqual(sorted(exts), ["alice:a", "alice:b", "bob:t"])                 # the same keys: the settings stay
        self.assertEqual({k: (x.get("market"), x.get("entry"), x.get("bound")) for k, x in exts.items()},
                         {"alice:a": ("agents-hub", "a", None), "alice:b": ("agents-hub", "b", True),
                          "bob:t": ("icons8-team", "t", None)})
        self.assertEqual((exts["alice:a"]["color"], exts["alice:a"]["disabled"], exts["alice:a"]["source"]),
                         ("red", True, "alice/hub-plugin-a"))                              # nothing else changes
        self.assertNotIn("marketplaces_removed", self.hub.cfg())
        before = self.hub.cfg()
        self.auto(30)
        self.assertEqual(self.hub.cfg(), before)                                       # once: a second pass is the same
        cfg = self.hub.cfg()
        self.assertEqual(marketplaces.entry_for(self.hub.state, cfg, "alice:a"), "a@agents-hub")
        self.assertEqual(marketplaces.entry_for(self.hub.state, cfg, "bob:t"), "t@icons8-team")

    def test_skill_sets_are_bound_and_follow_as_plugins_do(self):
        with catalog.edit_settings(self.hub.cfg_path) as raw:
            raw["sets"] = [{"name": "alice:b", "source": "alice/hub-plugin-b", "commit": SHA},
                           {"name": "bob:t", "source": "bob/hub-plugin-t", "commit": SHA, "market": "agents-hub",
                            "entry": "t"},
                           {"name": "x@claude", "plugin": True, "entry": "x", "market": "claude", "source": "c/x"}]
        self.new_core()
        self.auto()
        got = {s["name"]: (s.get("market"), s.get("entry")) for s in self.hub.cfg()["sets"]}
        self.assertEqual(got, {"alice:b": ("agents-hub", "b"), "bob:t": ("icons8-team", "t"),
                               "x@claude": ("claude", "x")})

    def test_a_module_from_the_team_catalog_follows_it(self):
        mod = {"name": "m", "type": "module", "source": {"git": "https://example.org/m.git", "sha": SHA}}
        self.team.commit(cat("agents-hub", plugin("a"), mod))
        self.branch({"name": "icons8-team", "plugins": [mod]})
        marketplaces.update(self.hub.state, self.hub.cfg_path, "agents-hub", now=15)
        with catalog.edit_settings(self.hub.cfg_path) as raw:
            raw["modules"] = {"sources": [{"git": "https://example.org/m.git", "commit": SHA, "market": "agents-hub",
                                           "entry": "m"}]}
        self.new_core()
        self.auto()
        self.assertEqual(self.hub.cfg()["modules"]["sources"][0]["market"], "icons8-team")

    def test_without_the_new_key_nothing_moves(self):
        self.install(**{"alice:a": {"source": "alice/hub-plugin-a", "commit": SHA, "market": "agents-hub", "entry": "a"}})
        before = self.hub.cfg()
        marketplaces.update(self.hub.state, self.hub.cfg_path, now=10 + marketplaces.REFRESH, auto=True)
        self.assertEqual(sorted(self.mps()), ["agents-hub"])
        self.assertEqual(self.exts(), before["extensions"])

    def test_a_removed_official_keeps_both_away(self):
        marketplaces.remove(self.hub.state, self.hub.cfg_path, "agents-hub")
        self.new_core()
        self.auto()
        self.assertEqual(self.mps(), {})
        self.assertEqual(self.hub.cfg()["marketplaces_removed"], ["agents-hub", "icons8-team"])
        self.auto(30)
        self.assertEqual(self.hub.cfg()["marketplaces_removed"], ["agents-hub", "icons8-team"])

    def test_an_unreadable_old_official_waits_and_the_hub_stays_on_it(self):
        self.install(**{"alice:b": {"source": "alice/hub-plugin-b", "commit": SHA}})
        with catalog.edit_settings(self.hub.cfg_path) as raw:
            raw["marketplaces"]["agents-hub"].pop("commit")
            raw["marketplaces"]["agents-hub"]["pending"] = "no network"
        before = self.hub.cfg()
        self.new_core()
        marketplaces.update(self.hub.state, self.hub.cfg_path, now=20, auto=True)
        self.assertEqual(sorted(self.mps()), ["agents-hub"])                          # neither the open one nor the team's
        self.assertEqual(self.mps()["agents-hub"]["source"], self.team.bare)
        self.assertEqual(self.exts(), before["extensions"])
        with catalog.edit_settings(self.hub.cfg_path) as raw:                          # it downloads again
            raw["marketplaces"]["agents-hub"] = {**before["marketplaces"]["agents-hub"], "commit": git(
                self.team.src.work, "rev-parse", "main"), "updated": 20}
            raw["marketplaces"]["agents-hub"].pop("pending")
        self.auto(30)
        self.assertEqual(sorted(self.mps()), ["agents-hub", "icons8-team"])
        self.assertEqual(self.exts()["alice:b"]["market"], "agents-hub")

    def test_a_removal_cut_off_after_hub_json_is_finished_by_the_next_pass(self):
        self.new_core()
        real = codestore.remove

        def cut(path, *a, **kw):
            if os.path.basename(path) == "agents-hub":
                raise OSError("the machine went down")
            return real(path, *a, **kw)
        with mock.patch.object(codestore, "remove", cut):
            with self.assertRaises(OSError):
                marketplaces.update(self.hub.state, self.hub.cfg_path, now=20, auto=True)
        self.assertNotIn("agents-hub", self.mps())                                    # hub.json is all new
        self.auto(30)
        self.assertEqual(self.mps()["agents-hub"]["source"], self.open.bare)
        self.assertEqual(marketplace._origin(marketplace._repo(self.hub.state, "agents-hub")), self.open.bare)

    def test_a_record_moves_only_when_both_read_fine_and_then_its_new_catalog_revokes(self):
        self.install(**{"bob:t": {"source": "bob/hub-plugin-t", "commit": SHA, "market": "agents-hub", "entry": "t"}})
        self.new_core()
        self.auto()
        self.assertEqual(self.exts()["bob:t"]["market"], "icons8-team")
        # t goes public, its own repo (revoked there) — read; then the team's branch drops it while the open one's next
        # download fails
        self.open.commit(cat("agents-hub", plugin("a", "icons8", SHA2), plugin("b", "icons8", SHA2),
                             plugin("t", "bob", revoked="broken")))
        marketplaces.update(self.hub.state, self.hub.cfg_path, "agents-hub", now=30)
        self.assertEqual(self.exts()["bob:t"]["market"], "icons8-team")                # in both: stays
        self.branch({"name": "icons8-team", "plugins": []})
        with catalog.edit_settings(self.hub.cfg_path) as raw:
            raw["marketplaces"]["agents-hub"]["error"] = "no network"
        marketplaces.update(self.hub.state, self.hub.cfg_path, "icons8-team", now=40)
        self.assertEqual(self.exts()["bob:t"]["market"], "icons8-team")                # the open one did not read fine
        lines, ok = marketplaces.update(self.hub.state, self.hub.cfg_path, now=50)
        self.assertTrue(ok, lines)
        t = self.exts()["bob:t"]
        self.assertEqual((t["market"], t.get("disabled"), t.get("revoked")), ("agents-hub", True, "broken"))

    def test_a_same_named_entry_of_other_code_does_not_take_a_record(self):
        self.install(**{"bob:t": {"source": "bob/hub-plugin-t", "commit": SHA, "market": "agents-hub", "entry": "t"}})
        self.new_core()
        self.auto()
        self.open.commit(cat("agents-hub", plugin("a", "icons8", SHA2), plugin("t", "mallory", revoked="x")))
        self.branch({"name": "icons8-team", "plugins": []})
        lines, ok = marketplaces.update(self.hub.state, self.hub.cfg_path, now=50)
        self.assertTrue(ok, lines)
        t = self.exts()["bob:t"]
        self.assertEqual((t["market"], t.get("disabled")), ("icons8-team", None))

    def test_without_the_team_catalog_under_its_new_name_nothing_moves(self):
        self.install(**{"alice:b": {"source": "alice/hub-plugin-b", "commit": SHA}})
        git(self.team.src.work, "push", "-q", self.team.bare, ":team")                # the branch is not there yet
        before = self.hub.cfg()
        self.new_core()
        marketplaces.update(self.hub.state, self.hub.cfg_path, now=20, auto=True)
        self.assertEqual(sorted(self.mps()), ["agents-hub"])
        self.assertEqual(self.mps()["agents-hub"]["source"], self.team.bare)          # the hub stays on the old one
        self.assertEqual(self.exts(), before["extensions"])
        self.assertFalse(marketplaces.due(self.hub.cfg(), 30))                        # no process every 10 minutes
        self.assertTrue(marketplaces.due(self.hub.cfg(), 10 + marketplaces.REFRESH))  # its own period retries
        self.branch({"name": "icons8-team", "plugins": [plugin("t", "bob")]})
        self.auto(10 + marketplaces.REFRESH)
        self.assertEqual(sorted(self.mps()), ["agents-hub", "icons8-team"])
        self.assertEqual(self.exts()["alice:b"]["market"], "agents-hub")

    def test_a_team_plugin_is_the_teams_at_once_and_an_open_namesakes_revocation_misses_it(self):
        self.install(**{"bob:t": {"source": "bob/hub-plugin-t", "commit": SHA, "market": "agents-hub", "entry": "t"}})
        self.open.commit(cat("agents-hub", plugin("a", "icons8", SHA2), plugin("t", "mallory", revoked="leaks")))
        self.new_core()
        self.auto()
        t = self.exts()["bob:t"]
        self.assertEqual((t["market"], t.get("disabled")), ("icons8-team", None))

    def test_the_open_catalog_pending_does_not_hold_the_teams_plugins(self):
        os.rename(self.open.bare, self.open.bare + ".gone")
        self.install(**{"bob:t": {"source": "bob/hub-plugin-t", "commit": SHA, "market": "agents-hub", "entry": "t"}})
        self.new_core()
        marketplaces.update(self.hub.state, self.hub.cfg_path, now=20, auto=True)
        self.assertTrue(self.mps()["agents-hub"]["pending"])
        self.assertEqual(self.exts()["bob:t"]["market"], "icons8-team")

    def test_a_market_of_the_wrong_shape_brings_nothing_down(self):
        self.install(**{"x:y": {"source": "x/y", "commit": SHA, "market": ["agents-hub"], "entry": "y"},
                        "bob:t": {"source": "bob/hub-plugin-t", "commit": SHA, "market": "agents-hub", "entry": "t"}})
        self.new_core()
        self.auto()
        self.assertEqual(self.exts()["x:y"]["market"], ["agents-hub"])

    def test_the_builds_branch_wins_over_the_one_written_at_adoption(self):
        self.new_core()
        self.auto()
        work = self.team.src.work
        git(work, "checkout", "-q", "-B", "team2", "team")
        git(work, "push", "-q", self.team.bare, "team2")
        git(work, "checkout", "-q", "main")
        self.patch(TEAM=("icons8-team", self.team.bare, "team2"))
        marketplaces.update(self.hub.state, self.hub.cfg_path, "icons8-team", now=40)
        self.assertEqual(self.mps()["icons8-team"]["ref"], "team2")

    def test_a_name_in_both_is_the_teams_at_the_move_and_then_stays_where_it_is(self):
        self.install(**{"bob:t": {"source": "bob/hub-plugin-t", "commit": SHA, "market": "agents-hub", "entry": "t"}})
        self.open.commit(cat("agents-hub", plugin("a", "icons8", SHA2), plugin("t", "bob")))
        self.new_core()
        self.auto()
        self.assertEqual(self.exts()["bob:t"]["market"], "icons8-team")              # the team's list names it
        with catalog.edit_settings(self.hub.cfg_path) as raw:
            raw["extensions"]["bob:t"]["market"] = "agents-hub"
        marketplaces.update(self.hub.state, self.hub.cfg_path, now=40)
        self.assertEqual(self.exts()["bob:t"]["market"], "agents-hub")               # in both: R2 leaves it

    def test_the_open_catalog_revokes_at_once_when_it_is_connected(self):
        self.install(**{"alice:a": {"source": "alice/hub-plugin-a", "commit": SHA, "market": "agents-hub", "entry": "a"}})
        self.open.commit(cat("agents-hub", plugin("a", "icons8", SHA2, revoked="leaks keys")))
        self.new_core()
        self.auto()
        a = self.exts()["alice:a"]
        self.assertEqual((a.get("disabled"), a.get("revoked")), (True, "leaks keys"))

    def test_an_install_newer_than_its_entry_is_not_offered_a_rollback(self):
        e = marketplace.parse(cat("agents-hub", plugin("a", version="1.0.0")), official=True)["entries"][0]
        for rec in ({"market": "agents-hub", "entry": "a", "version": "2.0.0", "bound": True}, {"version": "2.0.0"}):
            self.assertFalse(marketplaces._newer(self.hub.state, "agents-hub", SHA2, e, SHA2[:-1] + "b", rec), rec)
        for rec in ({"market": "agents-hub", "entry": "a", "version": "0.9.0", "bound": True},
                    {"market": "agents-hub", "entry": "a", "version": "2.0.0"}):   # installed by the catalog: the commit
            self.assertTrue(marketplaces._newer(self.hub.state, "agents-hub", SHA2, e, SHA2[:-1] + "b", rec), rec)


class TeamOf(unittest.TestCase):
    """modules/private.json of a tree: "repo" alone — the team's catalog instead of the open one (as before Embed API
    1.2); "repo" with "catalog" — the open one under `agents-hub` and the team's next to it under its own name."""

    def pinned(self, text):
        tree = tempfile.mkdtemp()
        self.addCleanup(platforms.current.rmtree, tree)
        os.makedirs(os.path.join(tree, "modules"))
        with open(os.path.join(tree, "modules", "private.json"), "w", encoding="utf-8") as f:
            f.write(text)
        return marketplaces.source_of(tree), marketplaces.team_of(tree)

    def test_the_catalog_key(self):
        open_ = marketplaces.OPEN_SOURCE
        for text, want in (
                ('{"repo": "team/modules"}', ("team/modules", None)),
                ('{"repo": "team/modules", "catalog": {"name": "team-x", "ref": "team"}}',
                 (open_, ("team-x", "team/modules", "team"))),
                ('{"repo": "team/modules", "catalog": {"name": "team-x"}}', (open_, ("team-x", "team/modules", None))),
                ('{"repo": "team/modules", "catalog": {"name": "agents-hub"}}', (open_, None)),   # reserved
                ('{"repo": "team/modules", "catalog": {"name": "openai-curated"}}', (open_, None)),
                ('{"repo": "team/modules", "catalog": {"name": "Team X"}}', (open_, None)),
                ('{"repo": "team/modules", "catalog": {"name": "team-x", "ref": "a..b"}}', (open_, None)),
                ('{"repo": "team/modules", "catalog": {"name": "team-x", "ref": 7}}', (open_, None)),
                ('{"repo": "team/modules", "catalog": "team-x"}', (open_, None)),   # a broken key: never the old meaning
                ('{"catalog": {"name": "team-x"}}', (open_, None)),
                ("[]", (open_, None))):
            self.assertEqual(self.pinned(text), want, text)


class Kick(unittest.TestCase):
    """hub-up every CHECK seconds: catalogs that are due are updated by a separate process, `hub marketplace update --auto`."""

    def test_a_process_only_when_due_and_not_while_one_runs(self):
        hub = Hub(self, {})
        started = []

        class Proc:
            def __init__(self, args, **kw):
                started.append((args, kw.get("env")))
                self.code = None

            def poll(self):
                return self.code
        with mock.patch.dict(os.environ, {"HUB_CONFIG": hub.cfg_path, "HUB_SESSION": "w"}), \
                mock.patch.object(marketplaces.subprocess, "Popen", Proc):
            self.assertIsNone(marketplaces.kick(hub.state, None, now=10))
            with catalog.edit_settings(hub.cfg_path) as raw:
                raw["marketplaces"] = {"alice": {"source": "/x", "commit": SHA, "updated": 0}}
            p = marketplaces.kick(hub.state, None, now=10 + marketplaces.REFRESH)
            self.assertEqual(len(started), 1)
            self.assertEqual(started[0][0][-3:], ["marketplace", "update", "--auto"])
            self.assertNotIn("HUB_SESSION", started[0][1])                      # the hub, not a session
            self.assertIs(marketplaces.kick(hub.state, p, now=10 + marketplaces.REFRESH), p)
            p.code = 0
            marketplaces.kick(hub.state, p, now=10 + marketplaces.REFRESH)
            self.assertEqual(len(started), 2)
        with open(hub.cfg_path, "w") as f:
            f.write("{")
        with mock.patch.dict(os.environ, {"HUB_CONFIG": hub.cfg_path}):
            self.assertIsNone(marketplaces.kick(hub.state, None, now=10 ** 9))   # hub.json is unreadable — silently


if __name__ == "__main__":
    unittest.main()
