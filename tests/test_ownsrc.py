"""The core's working copy of a hub from the one-line install (core/ownsrc.py): `hub app src` makes <state>/own/core — a
clone of the release's source on its commit when there is access, else a repository of the release's own files; the
branch `release` follows the release the hub runs, `local` keeps the person's edits and is never touched. The Hub
Developer of a packaged hub is the one Hub Developer, in <state>/own (plugins-dev of an earlier core moves there)."""
import tests  # noqa: F401 - ignore the machine's settings
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from core import appcode, ownsrc, packaged
from tests.test_app_layer import TREE, Base, edit, g, write

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Copy(Base):
    def setUp(self):
        super().setUp()
        self.chain.off()   # the repo moves on in these tests: no cached steps
        self.work = self.repo()
        self.state = os.path.join(self.root, "state")
        os.makedirs(self.state)
        self.said = []

    def layer_of(self, ref="v1.2.3", flavor="team", **kw):
        _, seed, _ = self.built(flavor=flavor, ref=ref, cache=False, **kw)
        return seed

    def make(self, code, url=None):
        return ownsrc.make(self.state, code, url=url, say=self.said.append)

    def test_without_a_source_the_copy_is_the_releases_own_files_on_local(self):
        code = self.layer_of()
        got = self.make(code)
        copy = os.path.join(self.state, "own", "core")
        self.assertEqual(got["path"], copy)
        self.assertEqual((got["kind"], got["version"], got["edits"], got["dirty"]), ("archive", "1.2.3", 0, False))
        commit = g(self.work, "rev-parse", "HEAD")
        self.assertEqual(got["commit"], commit)
        self.assertEqual(g(copy, "symbolic-ref", "--short", "HEAD"), "local")
        self.assertEqual(g(copy, "rev-parse", "release"), g(copy, "rev-parse", "local"))
        self.assertEqual(g(copy, "log", "-1", "--format=%s"), f"release 1.2.3 ({commit})")
        files = set(g(copy, "ls-files").splitlines())
        self.assertIn("core/x.py", files)
        self.assertIn("bin/hub-up", files)
        # the release's sources only: no tests (the layer has none), no bytecode, no manifest, no git stop of the layer
        self.assertFalse({f for f in files if f.startswith("tests/") or "__pycache__" in f or f in ("MANIFEST.json", ".git")})
        with open(os.path.join(copy, "core", "x.py")) as f:
            self.assertEqual(f.read(), "X = 1\n")
        if os.name != "nt":
            self.assertTrue(os.access(os.path.join(copy, "bin", "hub-up"), os.X_OK))
            self.assertTrue(os.access(os.path.join(copy, "core", "x.py"), os.W_OK))   # editable, unlike the layer

    def test_with_access_the_copy_is_a_clone_on_the_releases_commit_with_its_tests(self):
        code = self.layer_of()
        g(self.work, "commit", "-qm", "after the release", "--allow-empty")   # the source has moved on since
        got = self.make(code, url=self.work)
        copy = got["path"]
        self.assertEqual((got["kind"], got["source"], got["edits"]), ("clone", self.work, 0))
        tagged = g(self.work, "rev-parse", "v1.2.3^{commit}")
        self.assertEqual(g(copy, "rev-parse", "local"), tagged)
        self.assertEqual(g(copy, "rev-parse", "release"), tagged)
        self.assertIn("tests/test_x.py", g(copy, "ls-files").splitlines())
        self.assertEqual(g(copy, "rev-parse", "origin/main"), g(self.work, "rev-parse", "main"))   # a PR starts there

    def test_the_layer_names_its_source_and_the_copy_clones_it(self):
        g(self.work, "remote", "add", "origin", "https://example.test/acme/hub-core.git")
        code = self.layer_of(issues="acme/hub-issues")
        man = appcode.load(code)
        self.assertEqual((man["source"], man["issues"]), ("https://example.test/acme/hub-core.git", "acme/hub-issues"))
        with mock.patch.object(ownsrc.codestore, "git", wraps=ownsrc.codestore.git) as git:
            got = self.make(code)
        clone = [c for c in git.call_args_list if c.args[:1] == ("clone",)]
        self.assertEqual(clone[0].kwargs.get("remote"), "https://example.test/acme/hub-core.git")
        # no such server: the copy is made of the release's files, and the Hub Developer is told why
        self.assertEqual(got["kind"], "archive")
        self.assertTrue(any("no clone of https://example.test/acme/hub-core.git" in line for line in self.said), self.said)

    def test_a_source_with_a_sign_in_in_it_is_not_written_into_the_layer(self):
        g(self.work, "remote", "add", "origin", "https://user:token@example.test/acme/hub-core.git")
        self.assertNotIn("source", appcode.load(self.layer_of()))
        with self.assertRaises(self.layer.Refused):
            self.layer.build(self.work, "v1.2.3", os.path.join(self.root, "out2"), sys.executable,
                             source="https://user:token@example.test/x.git")
        with self.assertRaises(self.layer.Refused):
            self.layer.build(self.work, "v1.2.3", os.path.join(self.root, "out2"), sys.executable, issues="not a repo")

    def test_a_url_that_looks_like_an_option_is_not_an_option(self):
        got = self.make(self.layer_of(), url="--upload-pack=touch " + os.path.join(self.root, "pwned"))
        self.assertEqual(got["kind"], "archive")
        self.assertFalse(os.path.exists(os.path.join(self.root, "pwned")))

    def test_an_edited_layer_gives_no_copy(self):
        code = self.layer_of()
        edit(os.path.join(code, "core", "x.py"), "EDITED = 1\n")
        with self.assertRaises(ownsrc.Refused) as e:
            self.make(code)
        self.assertIn("core/x.py", str(e.exception))
        self.assertFalse(os.path.exists(ownsrc.path(self.state)))
        self.assertEqual([n for n in os.listdir(os.path.join(self.state, "own")) if n.startswith(".core-")], [])

    def test_a_hub_from_git_has_no_copy(self):
        with self.assertRaises(ownsrc.Refused):
            self.make(REPO)
        self.assertEqual(ownsrc.status(self.state), {})

    def person_edits(self, copy):
        write(os.path.join(copy, "core", "mine.py"), "MINE = 1\n")
        g(copy, "add", "-A")
        g(copy, "-c", "user.name=p", "-c", "user.email=p@example.test", "commit", "-qm", "mine")
        return g(copy, "rev-parse", "local")

    def test_a_new_release_moves_release_and_leaves_local_alone(self):
        got = self.make(self.layer_of())
        mine = self.person_edits(got["path"])
        old = g(got["path"], "rev-parse", "release")
        self.assertEqual(ownsrc.status(self.state)["edits"], 1)
        self.repo(tree={**TREE, "core/x.py": "X = 2\n"}, tag="v1.2.4")
        code = self.layer_of(ref="v1.2.4")
        self.assertTrue(ownsrc.status(self.state, code)["behind"])
        got = self.make(code)
        copy = got["path"]
        self.assertEqual((got["version"], got["behind"], got["edits"]), ("1.2.4", False, 1))
        self.assertEqual(g(copy, "rev-parse", "local"), mine)                  # the person's edit, untouched
        self.assertEqual(g(copy, "symbolic-ref", "--short", "HEAD"), "local")
        self.assertEqual(g(copy, "rev-parse", "release^"), old)                # the new release follows the old one
        self.assertEqual(g(copy, "show", "release:core/x.py"), "X = 2")
        with open(os.path.join(copy, "core", "x.py")) as f:
            self.assertEqual(f.read(), "X = 1\n")                              # no merge by itself
        self.assertTrue(any("git merge release" in line for line in self.said), self.said)
        g(copy, "-c", "user.name=p", "-c", "user.email=p@example.test", "merge", "-q", "--no-edit", "release")
        with open(os.path.join(copy, "core", "x.py")) as f:   # on request: the release merges into the edits
            self.assertEqual(f.read(), "X = 2\n")
        self.assertTrue(os.path.isfile(os.path.join(copy, "core", "mine.py")))
        self.assertEqual(ownsrc.status(self.state)["edits"], 1)   # the edit; the merge is not one

    def test_a_clone_fetches_the_new_release(self):
        got = self.make(self.layer_of(), url=self.work)
        self.person_edits(got["path"])
        self.repo(tree={**TREE, "core/x.py": "X = 2\n"}, tag="v1.2.4")
        got = self.make(self.layer_of(ref="v1.2.4"))
        self.assertEqual(g(got["path"], "rev-parse", "release"), g(self.work, "rev-parse", "v1.2.4^{commit}"))
        self.assertEqual((got["version"], got["edits"]), ("1.2.4", 1))

    def test_a_new_release_touches_no_file_of_the_working_tree(self):
        got = self.make(self.layer_of())
        copy = got["path"]
        with open(os.path.join(copy, "core", "x.py"), "a") as f:
            f.write("DRAFT = 1\n")                                            # uncommitted
        write(os.path.join(copy, "CLAUDE.local.md"), "mine\n")               # ignored by the release's .gitignore
        with open(os.path.join(copy, ".gitignore"), "a") as f:
            f.write("CLAUDE.local.md\n")
        self.assertTrue(ownsrc.status(self.state)["dirty"])
        self.repo(tree={**TREE, "core/x.py": "X = 2\n"}, tag="v1.2.4")
        got = self.make(self.layer_of(ref="v1.2.4"))
        self.assertEqual(g(copy, "show", "release:core/x.py"), "X = 2")
        with open(os.path.join(copy, "core", "x.py")) as f:
            self.assertEqual(f.read(), "X = 1\nDRAFT = 1\n")
        self.assertTrue(os.path.isfile(os.path.join(copy, "CLAUDE.local.md")))
        self.assertEqual(g(copy, "symbolic-ref", "--short", "HEAD"), "local")
        self.assertEqual(g(copy, "worktree", "list", "--porcelain").count("worktree "), 1)   # its side tree is gone
        self.assertEqual([n for n in os.listdir(os.path.dirname(copy)) if n.startswith(".release-")], [])

    def test_a_detached_head_follows_and_release_out_is_refused_with_the_step(self):
        got = self.make(self.layer_of())
        copy = got["path"]
        g(copy, "checkout", "-q", "--detach", "HEAD")                        # a rebase in progress looks like this
        self.repo(tree={**TREE, "core/x.py": "X = 2\n"}, tag="v1.2.4")
        code = self.layer_of(ref="v1.2.4")
        self.assertEqual(self.make(code)["version"], "1.2.4")
        self.repo(tree={**TREE, "core/x.py": "X = 3\n"}, tag="v1.2.5")
        code = self.layer_of(ref="v1.2.5")
        g(copy, "checkout", "-q", "release")
        with self.assertRaises(ownsrc.Refused) as e:
            self.make(code)
        self.assertIn("git switch local", str(e.exception))
        g(copy, "checkout", "-q", "local")
        self.assertEqual(self.make(code)["version"], "1.2.5")

    def test_a_folder_of_its_own_named_core_is_not_taken_for_the_copy(self):
        code = self.layer_of()
        write(os.path.join(self.state, "own", "core", "hub-plugin.json"), "{}")
        with self.assertRaises(ownsrc.Refused) as e:
            self.make(code)
        self.assertIn("move it away", str(e.exception))
        self.assertTrue(os.path.isfile(os.path.join(self.state, "own", "core", "hub-plugin.json")))

    def test_release_out_in_another_working_tree_is_refused_with_the_step(self):
        copy = self.make(self.layer_of())["path"]
        g(copy, "worktree", "add", "-q", os.path.join(self.root, "try"), "release")
        self.repo(tree={**TREE, "core/x.py": "X = 2\n"}, tag="v1.2.4")
        with self.assertRaises(ownsrc.Refused) as e:
            self.make(self.layer_of(ref="v1.2.4"))
        self.assertIn("git switch local", str(e.exception))
        self.assertEqual(g(copy, "log", "--format=%s", "release").count("release 1.2.4"), 0)   # nothing half-made

    def test_a_run_cut_off_before_its_note_makes_no_second_release_commit(self):
        copy = self.make(self.layer_of())["path"]
        with open(os.path.join(copy, ".git", "hub-source.json")) as f:
            note = f.read()
        self.repo(tree={**TREE, "core/x.py": "X = 2\n"}, tag="v1.2.4")
        code = self.layer_of(ref="v1.2.4")
        self.make(code)
        with open(os.path.join(copy, ".git", "hub-source.json"), "w") as f:   # as if cut off before the note
            f.write(note)
        self.make(code)
        self.assertEqual(g(copy, "log", "--format=%s", "release").count("release 1.2.4"), 1)

    def test_an_empty_folder_at_own_core_is_no_ones(self):
        os.makedirs(os.path.join(self.state, "own", "core"))
        self.assertEqual(self.make(self.layer_of())["kind"], "archive")

    def test_a_git_with_no_answer_gives_the_copy_of_files(self):
        code = self.layer_of()
        real = ownsrc.codestore.git

        def slow(*args, **kw):
            if args[:1] == ("clone",):
                raise RuntimeError("git: no answer within 900 s")
            return real(*args, **kw)
        with mock.patch.object(ownsrc.codestore, "git", side_effect=slow):
            got = self.make(code, url="https://example.test/acme/hub-core.git")
        self.assertEqual(got["kind"], "archive")
        self.assertTrue(any("no answer within 900 s" in line for line in self.said), self.said)

    def failing_clone(self, held=()):
        """git whose clone dies the way a garbling proxy makes it: a half pack in the folder, code 128."""
        real = ownsrc.codestore.git

        class Failed:
            returncode, stdout, stderr = 128, "", "fatal: fetch-pack: invalid index-pack output"

        def git(*args, **kw):
            if args[:1] == ("clone",):
                pack = os.path.join(args[-1], ".git", "objects", "pack")
                os.makedirs(pack)
                held.append(args[-1])
                with open(os.path.join(pack, "tmp_pack_x"), "w") as f:
                    f.write("x")
                return Failed()
            return real(*args, **kw)
        return mock.patch.object(ownsrc.codestore, "git", side_effect=git)

    def test_a_clone_that_failed_and_cannot_be_removed_still_gives_the_copy_of_files(self):
        # Windows: the half-made clone's files are held for a while; the copy of files is made in a folder of its own
        g(self.work, "remote", "add", "origin", "https://example.test/acme/hub-core.git")   # the release names its source
        code, held = self.layer_of(), []
        remove = appcode.remove_tree

        def refuse(p):
            if p in held:
                raise PermissionError(13, "Access is denied", p)
            remove(p)
        with self.failing_clone(held), mock.patch.object(ownsrc.appcode, "remove_tree", side_effect=refuse):
            got = self.make(code)
        self.assertEqual(got["kind"], "archive")
        self.assertTrue(os.path.isdir(os.path.join(got["path"], ".git")))
        self.assertTrue(any("invalid index-pack output" in line and "--repo" in line for line in self.said), self.said)
        # the folder the system held is a leftover of that run: the next one sweeps it
        self.assertTrue(os.path.isdir(held[0]))
        self.assertEqual(self.make(code)["kind"], "archive")   # the copy exists already: the sweep comes before follow
        self.assertEqual([n for n in os.listdir(os.path.join(self.state, "own")) if n.startswith(".core-")], [])
        keep = os.path.join(self.state, "own", ".core-notes")   # not a work folder of ours: left alone
        os.makedirs(keep)
        self.make(code)
        self.assertTrue(os.path.isdir(keep))

    def test_a_copy_that_cannot_be_made_or_put_in_place_is_a_refusal_and_leaves_no_folder(self):
        code = self.layer_of()
        own = os.path.join(self.state, "own")
        with mock.patch.object(ownsrc, "_write_meta", side_effect=PermissionError(13, "Access is denied")):
            with self.assertRaises(ownsrc.Refused) as caught:   # made by the release's own files: the way out is a clone
                self.make(code)
            self.assertIn("hub app src --repo", str(caught.exception))
            with self.assertRaises(ownsrc.Refused) as named:   # a clone the person named is not told to name one
                self.make(code, url=self.work)
            self.assertNotIn("--repo", str(named.exception))
        with mock.patch.object(ownsrc.os, "replace", side_effect=PermissionError(13, "Access is denied")):
            with self.assertRaises(ownsrc.Refused) as moved:
                self.make(code)
        self.assertIn("Access is denied", str(moved.exception))
        self.assertEqual([n for n in os.listdir(own) if n != ".core.lock"], [])
        self.assertEqual(ownsrc.status(self.state), {})
        self.assertEqual(self.make(code)["kind"], "archive")   # and the lock is free again

    def test_the_open_flavor_names_no_source_of_its_own(self):
        g(self.work, "remote", "add", "origin", "https://example.test/acme/hub-core.git")
        self.assertNotIn("source", appcode.load(self.layer_of(flavor="open")))


class Folder(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hub-own-")
        self.addCleanup(appcode.remove_tree, self.state)

    def test_plugins_dev_moves_to_own_and_stays_a_pointer(self):
        old = os.path.join(self.state, "plugins-dev")
        write(os.path.join(old, "my-theme", "hub-plugin.json"), "{}")
        got = ownsrc.settle(self.state)
        own = os.path.join(self.state, "own")
        self.assertEqual(got, own)
        self.assertTrue(os.path.isfile(os.path.join(own, "my-theme", "hub-plugin.json")))
        self.assertTrue(appcode.is_pointer(old))
        self.assertTrue(os.path.isfile(os.path.join(old, "my-theme", "hub-plugin.json")))   # hub.json lines still lead there
        self.assertEqual(ownsrc.settle(self.state), own)   # once
        self.assertEqual(ownsrc.path(self.state), os.path.join(own, "core"))

    def test_both_real_folders_are_left_as_they_are(self):
        for name in ("plugins-dev", "own"):
            os.makedirs(os.path.join(self.state, name, name))
        self.assertEqual(ownsrc.settle(self.state), os.path.join(self.state, "own"))
        self.assertTrue(os.path.isdir(os.path.join(self.state, "plugins-dev", "plugins-dev")))
        self.assertFalse(appcode.is_pointer(os.path.join(self.state, "plugins-dev")))

    def test_a_move_the_system_refuses_now_keeps_plugins_dev_until_the_next_start(self):
        os.makedirs(os.path.join(self.state, "plugins-dev"))
        with mock.patch.object(ownsrc.os, "replace", side_effect=PermissionError("in use")):
            self.assertEqual(ownsrc.settle(self.state), os.path.join(self.state, "plugins-dev"))
        self.assertFalse(os.path.exists(os.path.join(self.state, "own")))   # not made: the move can still happen
        self.assertEqual(os.listdir(os.path.join(self.state, "plugins-dev")), [])   # no mark left in the folder in use
        self.assertEqual(ownsrc.path(self.state), os.path.join(self.state, "plugins-dev", "core"))
        self.assertEqual(ownsrc.settle(self.state), os.path.join(self.state, "own"))

    def test_a_pointer_the_system_refused_is_made_by_the_next_settle(self):
        # audit: the move went, the pointer did not — the error was swallowed and nothing made it later
        import platforms
        old = os.path.join(self.state, "plugins-dev")
        write(os.path.join(old, "my-theme", "hub-plugin.json"), "{}")
        with mock.patch.object(platforms.current, "link_dir", side_effect=OSError("no right")):
            self.assertEqual(ownsrc.settle(self.state), os.path.join(self.state, "own"))
        self.assertFalse(os.path.lexists(old))
        ownsrc.settle(self.state)
        self.assertTrue(appcode.is_pointer(old))
        self.assertTrue(os.path.isfile(os.path.join(old, "my-theme", "hub-plugin.json")))
        self.assertEqual(sorted(os.listdir(self.state)), ["own", "plugins-dev"])   # nothing of the retry is left

    def test_a_settle_that_lost_the_move_to_another_one_answers_own(self):
        # review: two settles at once — the second one's move finds plugins-dev gone, it is not the folder to use
        old, own = os.path.join(self.state, "plugins-dev"), os.path.join(self.state, "own")
        os.makedirs(old)
        real = os.replace

        def other_first(src, dst):
            real(src, dst)
            raise FileNotFoundError(2, "No such file or directory", src)
        with mock.patch.object(ownsrc.os, "replace", other_first):
            self.assertEqual(ownsrc.settle(self.state), own)
        self.assertTrue(appcode.is_pointer(old))

    def test_a_move_refused_by_an_own_that_appeared_meanwhile_keeps_plugins_dev(self):
        old = os.path.join(self.state, "plugins-dev")
        write(os.path.join(old, "mine", "hub-plugin.json"), "{}")

        def own_first(src, dst):
            write(os.path.join(dst, "x"), "")
            raise OSError(39, "Directory not empty", src)
        with mock.patch.object(ownsrc.os, "replace", own_first):
            self.assertEqual(ownsrc.settle(self.state), old)

    def test_a_hub_that_never_had_plugins_dev_gets_no_pointer(self):
        ownsrc.settle(self.state)
        ownsrc.settle(self.state)
        self.assertEqual(os.listdir(self.state), ["own"])

    def test_the_hub_developer_of_a_package_is_the_one_hub_developer_in_own(self):
        from core import dev
        os.makedirs(os.path.join(self.state, "plugins-dev"))
        mark = os.path.join(self.state, "PACKAGED")
        with open(mark, "w") as f:
            json.dump({"format": "app", "layout": "layer", "version": "1.9.0"}, f)
        with mock.patch.object(packaged, "MARK", mark):
            name, data = dev.spec({}, self.state)
        self.assertEqual((name, data["tools"], data["cwd"]), ("hub-dev", ["core:hub-developer"],
                                                               os.path.join(self.state, "own")))
        self.assertFalse(os.path.isdir(os.path.join(REPO, "plugins", "hub-app-developer")))   # one Hub Developer

    def test_a_session_that_still_names_the_old_plugin_starts_with_the_hub_developer(self):
        from core import catalog
        cat = catalog.build({})
        self.assertNotIn("core:hub-app-developer", cat)
        flags = catalog.launch_flags(["core:hub-app-developer"], {"base": []}, cat, "agent", self.state, None, self.state,
                                     "hub-dev")
        dirs = [flags[i + 1] for i, f in enumerate(flags[:-1]) if f == "--plugin-dir"]
        self.assertEqual(dirs, [os.path.join(REPO, "plugins", "hub-developer")])


class Cli(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hub-own-cli-")
        self.addCleanup(appcode.remove_tree, self.state)

    def hub(self, *args, code=REPO, **env):
        base = {k: v for k, v in os.environ.items() if k not in ("HUB_SESSION", "HUB_EXTENSION", "HUB_MODULE")}
        return subprocess.run([sys.executable, os.path.join(code, "bin", "hub"), *args], capture_output=True, text=True,
                              timeout=60, env={**base, "HUB_STATE_DIR": self.state, "HUB_LANG": "en", **env})

    def test_hub_app_src_is_the_owners_and_the_hub_developers_only(self):
        r = self.hub("app", "src", HUB_SESSION="someone")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("owner's command", r.stdout + r.stderr)
        r = self.hub("app", "move", "x", "--app", "y", HUB_SESSION="hub-dev")   # the move stays the owner's
        self.assertIn("owner's command", r.stdout + r.stderr)
        r = self.hub("app", "src", HUB_SESSION="hub-dev")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)   # let through: this hub runs from git, not a layer
        self.assertIn("not a code layer", r.stderr)

    def test_hub_version_of_a_layer_names_the_whole_commit_where_issues_go_and_the_copy(self):
        code = os.path.join(self.state, "layers", "1.9.0-team")
        shutil.copytree(REPO, code, ignore=shutil.ignore_patterns(".git", "tests", "docs", "evals", "__pycache__",
                                                                  "node_modules", "experiments", "media"))
        commit = "a" * 40
        appcode.make(code, "1.9.0", "team", commit, issues="acme/hub-issues")
        mark = os.path.join(self.state, "PACKAGED.mark")
        with open(mark, "w") as f:
            json.dump({"format": "app", "layout": "layer", "version": "1.9.0", "runtime": 1}, f)
        r = self.hub("version", code=code, HUB_PACKAGED_MARK=mark)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"1.9.0 · {commit}", r.stdout)
        self.assertIn("https://github.com/acme/hub-issues/issues/new", r.stdout)
        self.assertNotIn("working copy", r.stdout)
        own = os.path.join(self.state, "own", "core")
        g(self.state, "init", "-q", "-b", "local", own)
        g(own, "-c", "user.name=p", "-c", "user.email=p@example.test", "commit", "-q", "--allow-empty", "-m", "release")
        g(own, "branch", "release")
        g(own, "-c", "user.name=p", "-c", "user.email=p@example.test", "commit", "-q", "--allow-empty", "-m", "mine")
        with open(os.path.join(own, ".git", "hub-source.json"), "w") as f:
            json.dump({"kind": "archive", "version": "1.9.0", "commit": commit}, f)
        r = self.hub("version", code=code, HUB_PACKAGED_MARK=mark)
        self.assertIn(f"working copy of the core: {own} (release 1.9.0), own commits on local: 1", r.stdout)


if __name__ == "__main__":
    unittest.main()
