"""Modules from git (Module API, slice C3, core/modsrc.py): `hub module add|update` on a bare local repo — the commit is
pinned, the code lives in its commit's folder (an update while running does not mix versions), a new commit with new
rights, plugins, ranges, a program or modules waits for `--accept`, hub.json is written under a lock, the commands are
the owner's word (from a session — only the hub with `--for mN`)."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock

import platforms
from core import catalog, lock, modrun, modsrc, modules
from tests.fakes.modstand import FAKE, Stand

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB = os.path.join(REPO, "bin", "hub")


def git(cwd, *args):
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "init.defaultBranch=main",
                        *args], cwd=cwd, capture_output=True, text=True)
    if r.returncode:
        raise AssertionError(f"git {' '.join(args)}: {r.stderr}")
    return r.stdout.strip()


def manifest(name="hello", ver="1.0.0", **extra):
    return {"name": name, "version": ver, "api": "^2.0", "run": [sys.executable, FAKE, "."], **extra}


class Source:
    """The module author's working copy and the bare repo the hub takes the module from."""

    def __init__(self, case, files=None):
        self.root = tempfile.mkdtemp(prefix="modsrc-")
        case.addCleanup(platforms.current.rmtree, self.root)
        self.work, self.bare = os.path.join(self.root, "work"), os.path.join(self.root, "hello.git")
        os.makedirs(self.work)
        git(self.work, "init", "-q")
        self.commit(files or {"module.json": manifest(), "main.py": "print('v1')\n"})
        git(self.root, "clone", "-q", "--bare", self.work, self.bare)

    def commit(self, files, message="x"):
        for rel, body in files.items():
            path = os.path.join(self.work, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if body is None:
                os.unlink(path)
                continue
            with open(path, "w", encoding="utf-8") as f:
                f.write(body if isinstance(body, str) else json.dumps(body))
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "-m", message)
        if os.path.isdir(getattr(self, "bare", "") or "/nonexistent"):
            git(self.work, "push", "-q", self.bare, "HEAD:main")
        return git(self.work, "rev-parse", "HEAD")

    def tag(self, name):
        git(self.work, "tag", name)
        git(self.work, "push", "-q", self.bare, name)


class Hub:
    """The state folder and the hub's hub.json."""

    def __init__(self, case, cfg=None):
        self.state = tempfile.mkdtemp(prefix="modsrc-hub-")
        case.addCleanup(platforms.current.rmtree, self.state)
        self.cfg_path = os.path.join(self.state, "hub.json")
        if cfg is not None:
            with open(self.cfg_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=1)

    def cfg(self):
        return catalog.read_settings(self.cfg_path)

    def found(self):
        return {f["name"]: f for f in modules.discover(self.cfg(), self.state, builtin=False)}

    def plan(self):
        return {r["name"]: r for r in modules.resolve(modules.discover(self.cfg(), self.state, builtin=False),
                                                      self.cfg(), catalog=set())}

    def add(self, url, **kw):
        return modsrc.add(self.state, self.cfg_path, url, **kw)

    def update(self, which=None, accept=False):
        return modsrc.update(self.state, self.cfg_path, which, accept)


class AddUpdate(unittest.TestCase):
    def setUp(self):
        self.src, self.hub = Source(self), Hub(self, {"theme": "x"})

    def test_add_pins_the_commit_unpacks_its_folder_and_enables(self):
        head = git(self.src.work, "rev-parse", "HEAD")
        lines = self.hub.add(self.src.bare)
        self.assertIn(head[:12], lines[0])
        self.assertIn("hello 1.0.0", "\n".join(lines))
        cfg = self.hub.cfg()
        self.assertEqual(cfg["theme"], "x")                                    # other entries in hub.json are in place
        (src,) = cfg["modules"]["sources"]
        self.assertEqual((src["git"], src["commit"], src["pinned"]), (self.src.bare, head, False))
        self.assertEqual(cfg["modules"]["enabled"], ["hello"])
        f = self.hub.found()["hello"]
        self.assertEqual((f["origin"], f["errors"], f["commit"]), ("git", [], head))
        self.assertEqual(os.path.basename(f["dir"]), head)                    # the commit's own folder
        with open(os.path.join(f["dir"], "main.py")) as fh:
            self.assertEqual(fh.read(), "print('v1')\n")
        self.assertEqual(self.hub.plan()["hello"]["state"], "ok")

    def test_tag_and_commit_are_pinned_and_a_bad_ref_is_refused(self):
        self.src.tag("v1")
        self.hub.add(self.src.bare, ref="v1")
        self.assertTrue(self.hub.cfg()["modules"]["sources"][0]["pinned"])
        with self.assertRaises(RuntimeError):
            self.hub.add(self.src.bare, ref="nope")
        with self.assertRaises(RuntimeError):
            self.hub.add(self.src.bare, ref="--upload-pack=x")
        with self.assertRaises(RuntimeError):
            self.hub.add(self.src.bare, subdir="../x")

    def test_update_without_new_rights_moves_the_commit_into_a_new_folder(self):
        self.hub.add(self.src.bare)
        old = self.hub.found()["hello"]["dir"]
        new = self.src.commit({"main.py": "print('v2')\n", "module.json": manifest(ver="1.0.1")})
        lines, ok = self.hub.update()
        self.assertTrue(ok, lines)
        self.assertEqual(self.hub.cfg()["modules"]["sources"][0]["commit"], new)
        f = self.hub.found()["hello"]
        self.assertEqual((os.path.basename(f["dir"]), f["version"], f["errors"]), (new, "1.0.1", []))
        with open(os.path.join(old, "main.py")) as fh:                         # the previous folder is untouched
            self.assertEqual(fh.read(), "print('v1')\n")
        self.assertEqual(self.hub.update(), ([f"{self.src.bare}: already on {new[:12]}"], True))
        third = self.src.commit({"main.py": "print('v3')\n"})
        self.assertTrue(self.hub.update()[1])
        left = sorted(n for n in os.listdir(os.path.dirname(old)) if len(n) == 40)
        self.assertEqual(left, sorted([new, third]))                           # the one before the previous is removed

    def test_new_rights_wait_for_accept_and_the_module_stays_on_the_approved_commit(self):
        first = git(self.src.work, "rev-parse", "HEAD")
        self.hub.add(self.src.bare)
        for change in ({"capabilities": ["doctor"]},
                       {"requires": {"plugins": ["some-plugin"]}},             # an earlier check did not catch this
                       {"env": ["HUB_MOD_HELLO_X"]},
                       {"run": [sys.executable, "other.py"]}):
            with self.subTest(change):
                self.src.commit({"module.json": manifest(**change)})
                lines, ok = self.hub.update()
                self.assertFalse(ok)
                self.assertIn("--accept", "\n".join(lines))
                self.assertEqual(self.hub.cfg()["modules"]["sources"][0]["commit"], first)
                self.assertEqual(os.path.basename(self.hub.found()["hello"]["dir"]), first)
                self.src.commit({"module.json": manifest()})
        last = self.src.commit({"module.json": manifest(capabilities=["doctor"])})
        lines, ok = self.hub.update(accept=True)
        self.assertTrue(ok, lines)
        self.assertEqual(self.hub.cfg()["modules"]["sources"][0]["commit"], last)
        self.assertEqual(self.hub.found()["hello"]["manifest"]["capabilities"], ["doctor"])

    def test_a_new_module_in_the_source_waits_for_accept_then_is_enabled(self):
        root = Source(self, {"hello/module.json": manifest()})
        self.hub.add(root.bare)
        root.commit({"other/module.json": manifest(name="other")})
        lines, ok = self.hub.update()
        self.assertFalse(ok)
        self.assertIn("new module other", "\n".join(lines))
        self.assertNotIn("other", self.hub.cfg()["modules"]["enabled"])
        self.assertTrue(self.hub.update("hello", accept=True)[1])
        self.assertEqual(self.hub.cfg()["modules"]["enabled"], ["hello", "other"])

    def test_a_trusted_source_updates_without_accept_and_shows_what_it_asks(self):
        self.hub.add(self.src.bare)
        self.assertIn("trusted", "\n".join(modsrc.trust(self.hub.state, self.hub.cfg_path, "hello")))
        self.assertTrue(self.hub.cfg()["modules"]["sources"][0]["trusted"])
        self.assertTrue(self.hub.found()["hello"]["trusted"])
        last = self.src.commit({"module.json": manifest(capabilities=["doctor"])})
        lines, ok = self.hub.update()
        self.assertTrue(ok, lines)
        self.assertIn("capabilities: + doctor", "\n".join(lines))   # what it asks for beyond that goes on screen
        self.assertIn("without --accept", "\n".join(lines))
        self.assertEqual(self.hub.cfg()["modules"]["sources"][0]["commit"], last)
        modsrc.trust(self.hub.state, self.hub.cfg_path, self.src.bare, on=False)
        self.assertNotIn("trusted", self.hub.cfg()["modules"]["sources"][0])
        self.src.commit({"module.json": manifest(capabilities=["doctor", "cli"])})
        self.assertFalse(self.hub.update()[1])
        with self.assertRaises(RuntimeError):
            modsrc.trust(self.hub.state, self.hub.cfg_path, "ghost")

    def test_a_hand_edit_in_the_commit_folder_blocks_and_update_restores_it(self):
        self.hub.add(self.src.bare)
        folder = self.hub.found()["hello"]["dir"]
        with open(os.path.join(folder, "main.py"), "w") as fh:
            fh.write("print('hand edit, same size')")
        os.makedirs(os.path.join(folder, "__pycache__"))                       # Python bytecode is not an edit
        r = self.hub.plan()["hello"]
        self.assertEqual(r["state"], "blocked")
        self.assertIn("main.py", r["reason"])
        lines, ok = self.hub.update()
        self.assertTrue(ok, lines)
        self.assertEqual(self.hub.plan()["hello"]["state"], "ok")
        with open(os.path.join(folder, "main.py")) as fh:
            self.assertEqual(fh.read(), "print('v1')\n")

    def test_not_unpacked_yet_is_blocked_with_how_to_fix(self):
        hub = Hub(self, {"modules": {"sources": [{"git": self.src.bare, "commit": "a" * 40}], "enabled": ["x"]}})
        (r,) = hub.plan().values()
        self.assertEqual(r["state"], "blocked")
        self.assertIn("hub module update", r["reason"])

    def test_a_source_that_cannot_be_fetched_keeps_the_module_running(self):
        self.hub.add(self.src.bare)
        before = self.hub.cfg()
        platforms.current.rmtree(self.src.bare)   # Windows: .git/objects are «read-only»
        lines, ok = self.hub.update()
        self.assertFalse(ok)
        self.assertIn("previous commit", lines[0])
        self.assertEqual(self.hub.cfg(), before)
        self.assertEqual(self.hub.plan()["hello"]["state"], "ok")


class NarrowFetch(unittest.TestCase):
    """The hub takes only the needed ref from a source: a bare clone does not pull the other branches, tags and their
    history (the source may be a repo where the whole core lies alongside)."""

    def setUp(self):
        self.src, self.hub = Source(self), Hub(self, {})
        self.main = git(self.src.work, "rev-parse", "HEAD")
        git(self.src.work, "checkout", "-q", "--orphan", "other")             # its own history: nothing in common with main
        with open(os.path.join(self.src.work, "big.txt"), "w") as f:
            f.write("other\n")
        git(self.src.work, "add", "-A")
        git(self.src.work, "commit", "-q", "-m", "other")
        self.other = git(self.src.work, "rev-parse", "HEAD")
        git(self.src.work, "push", "-q", self.src.bare, "HEAD:other")
        git(self.src.work, "tag", "v9")
        git(self.src.work, "push", "-q", self.src.bare, "v9")
        git(self.src.work, "checkout", "-q", "-f", "main")
        self.repo = modsrc.git_dir(self.hub.state, self.src.bare)

    def refs(self):
        out = subprocess.run(["git", f"--git-dir={self.repo}", "for-each-ref", "--format=%(refname)"],
                             capture_output=True, text=True, check=True).stdout.split()
        return sorted(r for r in out if not r.startswith("refs/pins/"))

    def has(self, commit):
        return subprocess.run(["git", f"--git-dir={self.repo}", "cat-file", "-e", commit],
                              capture_output=True).returncode == 0

    def test_the_default_branch_only(self):
        self.hub.add(self.src.bare)
        self.assertEqual(self.refs(), ["refs/heads/main"])
        self.assertTrue(self.has(self.main))
        self.assertFalse(self.has(self.other))                                 # the other branch and tag were not downloaded

    def test_the_default_branch_is_the_one_the_source_points_at(self):
        git(self.src.bare, "symbolic-ref", "HEAD", "refs/heads/other")
        lines = self.hub.add(self.src.bare)
        self.assertIn(self.other[:12], lines[0])
        self.assertEqual(self.refs(), ["refs/heads/other"])
        self.assertFalse(self.has(self.main))

    def test_a_branch_a_tag_and_a_commit(self):
        for ref, want, refs in (("other", self.other, ["refs/heads/other"]), ("v9", self.other, ["refs/tags/v9"]),
                                (self.other, self.other, []), (self.main, self.main, [])):
            with self.subTest(ref=ref):
                platforms.current.rmtree(modsrc.source_dir(self.hub.state, self.src.bare))   # Windows: .git/objects are «read-only»
                self.assertIn(want[:12], self.hub.add(self.src.bare, ref=ref)[0])
                self.assertEqual(self.refs(), refs)
                self.assertFalse(self.has(self.other if want == self.main else self.main))

    def test_a_short_commit_still_works(self):
        self.assertIn(self.other[:12], self.hub.add(self.src.bare, ref=self.other[:9])[0])

    def test_update_follows_a_moving_branch_and_stays_narrow(self):
        self.hub.add(self.src.bare)
        new = self.src.commit({"main.py": "print('v2')\n"})
        lines, ok = self.hub.update()
        self.assertTrue(ok, lines)
        self.assertEqual(self.hub.cfg()["modules"]["sources"][0]["commit"], new)
        self.assertEqual(self.refs(), ["refs/heads/main"])
        self.assertFalse(self.has(self.other))

    def test_a_missing_ref_is_refused_without_touching_hub_json(self):
        with self.assertRaises(RuntimeError) as e:
            self.hub.add(self.src.bare, ref="nope")
        self.assertIn("nope", str(e.exception))
        self.assertEqual(self.hub.cfg(), {})

    def test_a_hub_of_an_older_version_can_still_update_the_clone(self):
        self.hub.add(self.src.bare)                                            # a hub rollback: it calls `git fetch origin`
        self.assertEqual(subprocess.run(["git", f"--git-dir={self.repo}", "fetch", "-q", "--prune", "--force", "--tags",
                                         "origin"], capture_output=True).returncode, 0)
        self.assertEqual(git(self.repo, "symbolic-ref", "HEAD"), "refs/heads/main")

    def test_the_approved_commit_is_fetched_back_when_the_clone_no_longer_has_it(self):
        self.hub.add(self.src.bare)
        old = self.hub.cfg()["modules"]["sources"][0]["commit"]
        self.src.commit({"module.json": manifest(ver="1.1.0", capabilities=["doctor"])})
        lines, ok = self.hub.update()
        self.assertFalse(ok, lines)                                            # it asks for more — waits for --accept
        git(self.repo, "reflog", "expire", "--expire=now", "--all")
        git(self.repo, "gc", "-q", "--prune=now")                              # the previous commit is no longer in the clone
        self.assertFalse(self.has(old))
        shutil.rmtree(self.hub.found()["hello"]["dir"])
        lines, ok = self.hub.update()
        self.assertFalse(ok, lines)
        text = "\n".join(lines)
        self.assertIn("capabilities", text)                                    # beyond what was approved — only rights
        self.assertNotIn("new module", text)
        self.assertEqual(self.hub.plan()["hello"]["state"], "ok")              # and the approved commit is on disk again

    def test_a_clone_made_by_an_older_hub_keeps_working(self):
        os.makedirs(os.path.dirname(self.repo))
        git(self.hub.state, "clone", "-q", "--bare", self.src.bare, self.repo)
        git(self.repo, "config", "remote.origin.fetch", "+refs/heads/*:refs/heads/*")
        self.hub.add(self.src.bare)
        new = self.src.commit({"main.py": "print('v2')\n"})
        lines, ok = self.hub.update()
        self.assertTrue(ok, lines)
        self.assertEqual(self.hub.cfg()["modules"]["sources"][0]["commit"], new)


def two_modules(case):
    """A source with two modules in folders aa and bb, both added (one by one, --subdir) at one commit."""
    src = Source(case, {"aa/module.json": manifest("aa"), "bb/module.json": manifest("bb")})
    hub = Hub(case, {"theme": "x"})
    for sub in ("aa", "bb"):
        hub.add(src.bare, subdir=sub)
    return src, hub


class Remove(unittest.TestCase):
    """`hub module remove <name|address>`: the source or folder and the enablement leave hub.json, the commit's files leave the disk."""

    def remove(self, hub, which):
        return modsrc.remove(hub.state, hub.cfg_path, which)

    def test_a_git_module_leaves_hub_json_and_the_disk(self):
        src, hub = Source(self), Hub(self, {"theme": "x"})
        hub.add(src.bare)
        with catalog.edit_settings(hub.cfg_path) as raw:
            for key in ("dev", "model", "core_write"):
                raw["modules"][key] = ["hello", "other"]
        folder = hub.found()["hello"]["dir"]
        lines = self.remove(hub, "hello")
        self.assertIn("hello", "\n".join(lines))
        cfg = hub.cfg()
        self.assertEqual(cfg["theme"], "x")
        self.assertEqual(cfg["modules"], {"sources": [], "enabled": [], "dev": ["other"], "model": ["other"],
                                          "core_write": ["other"]})     # consents do not pass to the next module with this name
        self.assertFalse(os.path.exists(folder))
        self.assertFalse(os.path.exists(modsrc.source_dir(hub.state, src.bare)))   # and the clone too: there are no more sources
        self.assertEqual(hub.found(), {})

    def test_one_of_two_modules_of_a_repo_leaves_the_other_and_the_shared_files(self):
        src, hub = two_modules(self)
        folder = hub.found()["aa"]["dir"]
        self.remove(hub, "aa")
        cfg = hub.cfg()
        self.assertEqual([s["subdir"] for s in cfg["modules"]["sources"]], ["bb"])
        self.assertEqual(cfg["modules"]["enabled"], ["bb"])
        self.assertEqual(sorted(hub.found()), ["bb"])
        self.assertTrue(os.path.isdir(os.path.dirname(folder)))                # one commit — the second one needs the folder
        self.assertTrue(os.path.isdir(modsrc.git_dir(hub.state, src.bare)))
        self.assertEqual(hub.plan()["bb"]["state"], "ok")

    def test_a_repo_by_its_address_takes_all_its_modules(self):
        src, hub = two_modules(self)
        lines = self.remove(hub, src.bare)
        self.assertEqual(hub.cfg()["modules"], {"sources": [], "enabled": []})
        self.assertIn("aa", "\n".join(lines))
        self.assertIn("bb", "\n".join(lines))
        self.assertFalse(os.path.exists(modsrc.source_dir(hub.state, src.bare)))

    def test_a_name_among_several_modules_of_one_source_does_not_take_the_others(self):
        src = Source(self, {"aa/module.json": manifest("aa"), "bb/module.json": manifest("bb")})
        folder = tempfile.mkdtemp(prefix="modsrc-dir-")
        self.addCleanup(shutil.rmtree, folder, True)
        for name in ("aa", "bb"):
            os.makedirs(os.path.join(folder, name))
            with open(os.path.join(folder, name, "module.json"), "w") as f:
                json.dump(manifest(name), f)
        hub = Hub(self, {})
        hub.add(src.bare)                                                      # without subdir: both modules are one source
        modsrc.add_path(hub.state, hub.cfg_path, folder)                       # and the second pair is one folder (the same names)
        before = hub.cfg()
        for which in ("aa", "bb"):
            with self.assertRaises(RuntimeError) as e:
                self.remove(hub, which)
            self.assertIn("to remove them all", str(e.exception))
        self.assertEqual(hub.cfg(), before)
        self.remove(hub, folder)
        self.assertEqual(hub.cfg()["modules"]["paths"], [])
        self.assertEqual(hub.cfg()["modules"]["enabled"], ["aa", "bb"])        # the source provides the same names — enabled
        self.assertEqual(len(hub.cfg()["modules"]["sources"]), 1)              # the source remained

    def test_a_source_not_unpacked_any_more_goes_by_its_address_or_folder_name(self):
        src, hub = two_modules(self)
        platforms.current.rmtree(modsrc.source_dir(hub.state, src.bare))
        self.remove(hub, "aa")                                                 # the folder name, while the source's files are absent
        self.assertEqual([s["subdir"] for s in hub.cfg()["modules"]["sources"]], ["bb"])
        self.assertEqual(hub.cfg()["modules"]["enabled"], ["bb"])
        self.remove(hub, src.bare)
        self.assertEqual(hub.cfg()["modules"], {"sources": [], "enabled": []})

    def test_a_folder_module_is_unlisted_and_its_files_stay(self):
        hub = Hub(self, {})
        folder = tempfile.mkdtemp(prefix="modsrc-dir-")
        self.addCleanup(shutil.rmtree, folder, True)
        with open(os.path.join(folder, "module.json"), "w") as f:
            json.dump(manifest("loose"), f)
        modsrc.add_path(hub.state, hub.cfg_path, folder, dev=True)
        self.assertEqual(hub.cfg()["modules"]["dev"], ["loose"])
        self.remove(hub, "loose")
        self.assertEqual(hub.cfg()["modules"], {"paths": [], "enabled": [], "dev": []})
        self.assertTrue(os.path.isfile(os.path.join(folder, "module.json")))   # we do not touch foreign files

    def test_a_built_in_and_an_unknown_name_are_refused_and_hub_json_is_not_touched(self):
        src, hub = Source(self), Hub(self, {})
        hub.add(src.bare)
        before = hub.cfg()
        repo = tempfile.mkdtemp(prefix="modsrc-core-")
        self.addCleanup(shutil.rmtree, repo, True)
        os.makedirs(os.path.join(repo, "modules", "inner"))
        with open(os.path.join(repo, "modules", "inner", "module.json"), "w") as f:
            json.dump(manifest("inner"), f)
        with unittest.mock.patch.object(modules, "REPO", repo):
            for which, word in (("inner", "built-in"), ("nope", "nope")):
                with self.subTest(which=which):
                    with self.assertRaises(RuntimeError) as e:
                        self.remove(hub, which)
                    self.assertIn(word, str(e.exception))
        self.assertEqual(hub.cfg(), before)

    def test_the_folder_a_running_process_works_in_stays_until_it_stops(self):
        src, hub = Source(self), Hub(self, {})
        hub.add(src.bare)
        folder = hub.found()["hello"]["dir"]
        os.makedirs(modrun.run_dir(hub.state))
        modrun._write(os.path.join(modrun.run_dir(hub.state), "hello.want.json"),
                      {"name": "hello", "cmd": ["x"], "cwd": folder, "key": "k"})
        self.remove(hub, "hello")
        self.assertTrue(os.path.isdir(folder))
        self.assertEqual(hub.cfg()["modules"]["sources"], [])
        os.unlink(os.path.join(modrun.run_dir(hub.state), "hello.want.json"))   # the process was stopped by hub-up
        self.assertEqual(modsrc.update(hub.state, hub.cfg_path), (["there are no modules from git in hub.json"], True))
        self.assertFalse(os.path.exists(modsrc.source_dir(hub.state, src.bare)))   # the next command cleaned up the remainder

    def test_foreign_folders_in_the_sources_directory_are_left_alone(self):
        hub = Hub(self, {})
        mine, empty = (os.path.join(hub.state, modsrc.DIR, n) for n in ("mine", "empty"))
        os.makedirs(mine)
        os.makedirs(empty)
        with open(os.path.join(mine, "notes.txt"), "w") as f:
            f.write("x")
        modsrc.update(hub.state, hub.cfg_path)
        self.assertTrue(os.path.isfile(os.path.join(mine, "notes.txt")))
        self.assertTrue(os.path.isdir(empty))

    def test_a_typo_in_the_modules_section_does_not_make_every_source_look_orphaned(self):
        src, hub = Source(self), Hub(self, {})
        hub.add(src.bare)
        (source,) = hub.cfg()["modules"]["sources"]
        folder = modsrc.commit_dir(hub.state, src.bare, source["commit"])
        for name, broken in (("extra item", {"sources": [source, "typo"]}), ("git is not a string", {"sources": [{"git": 5}]}),
                             ("section is not an object", "oops"), ("list is not a list", {"sources": "oops"})):
            with self.subTest(name):
                with catalog.edit_settings(hub.cfg_path) as raw:
                    raw["modules"] = broken
                modsrc.update(hub.state, hub.cfg_path)
                self.assertTrue(os.path.isdir(modsrc.git_dir(hub.state, src.bare)))
                self.assertTrue(os.path.isdir(folder))


class Wider(unittest.TestCase):
    """What a new commit asks for beyond what was approved (an earlier check caught only rights and new dependencies)."""

    def wider(self, old, new):
        f = lambda m: {m["name"]: {"name": m["name"], "version": m["version"], "manifest": m, "errors": []}}   # noqa
        return modsrc.wider(f(old), f(new))

    def test_what_counts_as_more(self):
        base = manifest(requires={"modules": {"a": "^1.0"}}, optional={"modules": {"b": "^1.0"}}, env=["HUB_MOD_HELLO_A"],
                        capabilities=["cli"])
        more = [{"capabilities": ["cli", "doctor"]}, {"requires": {"modules": {"a": "^1.0", "c": "^1"}}},
                {"requires": {"modules": {"a": ">=1.0"}}},                     # a changed range
                {"requires": {"modules": {"a": "^1.0"}, "plugins": ["p"]}},
                {"optional": {"modules": {"b": "^1.0", "d": "^1"}}},
                {"env": ["HUB_MOD_HELLO_A", "HUB_MOD_HELLO_B"]},
                {"run": [sys.executable, "main.py", "--x"]},
                {"provides": ["launch-env"]},
                {"web": {"plugin": "p.js"}}]                                   # a field of later slices — also
        for change in more:
            with self.subTest(change):
                self.assertTrue(self.wider(base, {**base, **change}))
        same = [{"version": "1.2.0"}, {"description": "new"}, {"capabilities": []}, {"api": ">=2.0,<3"},
                {"budget": {"hello": 1}}, {"env": []}]
        for change in same:
            with self.subTest(change):
                self.assertEqual(self.wider(base, {**base, **change}), [])


class NoMixOfVersions(unittest.TestCase):
    """An update while the module process is running: the old process reads its files from its commit's folder until
    restarted, the new one from the new folder; hub-up restarts it when want changes."""

    def test_running_process_keeps_its_files_and_the_new_want_points_at_the_new_commit(self):
        src = Source(self)
        s = Stand(self, name="other")   # a stand: hubd and the module launch the same way as with hub-up
        cfg_path = os.path.join(s.state_dir, "hub.json")
        modsrc.add(s.state_dir, cfg_path, src.bare)
        s.hub.modules.reload(catalog.read_settings(cfg_path), {})
        first = s.hub.modules.sync()["hello"]
        s.launch("hello")
        s.until_ready("hello")
        src.commit({"module.json": manifest(ver="1.0.1")})
        self.assertTrue(modsrc.update(s.state_dir, cfg_path)[1])
        run = modrun.run_of(s.state_dir, "hello")
        kind, got = modrun.post(run["port"], run["secret"], "/hook/hello", {}, 5)
        self.assertEqual((kind, got["version"]), (None, "1.0.0"))              # the old process — its own files
        s.hub.modules.reload(catalog.read_settings(cfg_path), {})
        second = s.hub.modules.sync()["hello"]
        self.assertNotEqual(first["key"], second["key"])                       # hub-up will restart it
        self.assertNotEqual(first["cwd"], second["cwd"])
        self.assertTrue(os.path.isdir(first["cwd"]))                           # and the previous folder is intact while it lives


class Lock(unittest.TestCase):
    def test_edit_waits_for_the_lock_and_keeps_a_change_made_meanwhile(self):
        hub = Hub(self, {"a": 1})
        done = []
        with lock.exclusive(hub.cfg_path + ".lock"):
            t = threading.Thread(target=lambda: done.append(modsrc.add_source(hub.cfg_path, {"git": "u", "commit": "c"},
                                                                              ["m"])))
            t.start()
            time.sleep(0.3)
            self.assertEqual(done, [])                                         # waits for the lock
            with open(hub.cfg_path, "w") as f:                                 # while it waits — an edit by another writer
                json.dump({"a": 1, "b": 2}, f)
        t.join(10)
        cfg = hub.cfg()
        self.assertEqual((cfg["b"], cfg["modules"]["enabled"]), (2, ["m"]))


class Owner(unittest.TestCase):
    """Both commands are the owner's word: the owner (a command outside a session) or the hub on his message (`--for mN`)."""

    def setUp(self):
        from core import journal
        self.src, self.hub = Source(self), Hub(self, {})
        j = journal.Journal(os.path.join(self.hub.state, "journal.jsonl"))
        j.append("session.requested", "hub", {"cwd": "/h", "kind": "hub", "channel": "cli", "owner": True})
        j.append("session.requested", "w", {"cwd": "/w", "channel": "cli", "owner": True})
        self.word = "m" + str(j.append("message.received", None, {"to": "hub", "channel": "cli", "owner": True,
                                                                     "text": f"install the hello module from {self.src.bare}"})["seq"])

    def run_hub(self, *args, me=None, module=None):
        env = {k: v for k, v in os.environ.items() if k not in ("HUB_SESSION", "HUB_EXTENSION", "HUB_MODULE")}
        env.update(HUB_STATE_DIR=self.hub.state, HUB_CONFIG=self.hub.cfg_path, HUB_CLI_WAIT="0",
                   **({"HUB_SESSION": me} if me else {}), **({"HUB_MODULE": module} if module else {}))
        return subprocess.run([sys.executable, HUB, "module", *args], env=env, capture_output=True, text=True)

    def test_a_session_and_the_hub_without_the_owners_word_are_refused(self):
        for me, extra in (("w", []), ("hub", []), ("hub", ["--for", "m999"]), ("w", ["--for", self.word])):
            with self.subTest(me=me, extra=extra):
                r = self.run_hub("add", self.src.bare, *extra, me=me)
                self.assertNotEqual(r.returncode, 0)
                out = r.stderr + r.stdout   # the gate answers a session in English; the core's refusal of a word is its own
                self.assertIn("owner", out)
        self.assertEqual(self.hub.cfg(), {})

    def test_a_module_process_is_not_the_owner(self):
        # a core module process (HUB_MODULE, without HUB_SESSION) is not the owner's word: it does not install foreign code into the hub
        for sub in (["add", self.src.bare], ["update", "--accept"], ["trust", "hello"], ["remove", "hello"]):
            r = self.run_hub(*sub, module="notes")
            self.assertNotEqual(r.returncode, 0, sub)
            self.assertIn("owner", r.stderr + r.stdout)
        self.assertEqual(self.hub.cfg(), {})

    def test_the_hub_on_his_answer_to_another_sessions_card(self):
        # the owner answered «Connect» on a specialist's card — the hub installs the module by --for qN without asking again
        from core import journal
        j = journal.Journal(os.path.join(self.hub.state, "journal.jsonl"))
        q = {"questions": [{"question": f"Connect the hello module from {self.src.bare}?", "header": "Module",
                            "options": [{"label": "Connect"}, {"label": "No need"}]}]}
        j.append("session.started", "w", {})
        j.append("turn.started", "w", {"marker": None, "prompt_head": "x"})
        card = "q" + str(j.append("permission.requested", "w", {"request_id": "r1", "tool": "AskUserQuestion",
                                                                "interactive": True, "input": q})["seq"])
        j.append("turn.result", "w", {"text": "asked"})
        j.append("turn.stopped", "w", {})
        r = self.run_hub("add", self.src.bare, "--for", card, me="hub")                  # there is no answer yet
        self.assertNotEqual(r.returncode, 0, r.stderr + r.stdout)
        j.append("permission.answered", "w", {"request_id": "r1", "behavior": "allow", "channel": "web", "owner": True,
                                              "answers": {q["questions"][0]["question"]: "Connect"}})
        r = self.run_hub("add", self.src.bare, "--for", card, me="w")                    # not the hub — no
        self.assertNotEqual(r.returncode, 0, r.stderr + r.stdout)
        r = self.run_hub("add", self.src.bare, "--for", card, me="hub")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(self.hub.cfg()["modules"]["enabled"], ["hello"])

    def test_the_hubs_word_names_the_module_it_installs(self):
        # 18.11: owner_gate took any fresh word of the owner, so "show me the status" let the hub install a module, widen
        # its rights or install a plugin from a catalog — new code in the hub the owner never saw named
        from core import journal
        j = journal.Journal(os.path.join(self.hub.state, "journal.jsonl"))
        other = "m" + str(j.append("message.received", None, {"to": "hub", "text": "show me the status",
                                                               "channel": "cli", "owner": True})["seq"])
        env = {k: v for k, v in os.environ.items() if k not in ("HUB_SESSION", "HUB_EXTENSION", "HUB_MODULE")}
        env.update(HUB_STATE_DIR=self.hub.state, HUB_CONFIG=self.hub.cfg_path, HUB_CLI_WAIT="0", HUB_SESSION="hub")
        for cmd in (["module", "add", self.src.bare], ["module", "trust", "hello"], ["module", "dev", "hello"],
                    ["module", "update", "--accept"], ["module", "update", "hello", "--accept"],
                    ["marketplace", "install", "owl@shop"], ["market", "install", "alice/owl"]):
            with self.subTest(cmd=cmd):
                r = subprocess.run([sys.executable, HUB, *cmd, "--for", other], env=env, capture_output=True, text=True)
                self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertRegex(r.stdout + r.stderr, "do not name|one module by name")
        self.assertEqual(self.hub.cfg(), {})
        # a new source names itself in full: its short name is the requester's choice
        status = "m" + str(j.append("message.received", None, {"to": "hub", "text": "install hello, and show me the "
                                                                "status of evil", "channel": "cli", "owner": True})["seq"])
        here = tempfile.mkdtemp(prefix="modsrc-cwd-")
        self.addCleanup(platforms.current.rmtree, here)
        os.makedirs(os.path.join(here, "status"))                              # a folder the hub made itself
        for src in ("https://example.org/evil/status.git", "git@example.org:evil/hello.git", self.src.bare, "status"):
            with self.subTest(src=src):
                r = subprocess.run([sys.executable, HUB, "module", "add", src, "--for", status], env=env,
                                   capture_output=True, text=True, cwd=here)
                self.assertIn("do not name", r.stdout + r.stderr)
        github = "m" + str(j.append("message.received", None, {"to": "hub", "text": "install alice/notes",
                                                               "channel": "cli", "owner": True})["seq"])
        r = subprocess.run([sys.executable, HUB, "module", "add", "https://example.org/alice/notes.git", "--for", github],
                           env=env, capture_output=True, text=True)   # owner/repo is GitHub's: another host is chosen
        self.assertIn("do not name", r.stdout + r.stderr)
        from core import tools   # GitHub's own (no clone here: the gate's names only)
        self.assertIn("alice/notes", tools.name_forms("https://github.com/alice/notes.git", bare=False))
        self.assertIn("alice/notes", tools.name_forms("git@github.com:alice/notes.git", bare=False))
        r = self.run_hub("add", self.src.bare, "--for", self.word, me="hub")      # names the address
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(self.hub.cfg()["modules"]["enabled"], ["hello"])
        for cmd in (["update"], ["trust", "hello", "--off"], ["dev", "hello", "--off"]):   # within its rights, taking off
            with self.subTest(cmd=cmd):
                r = self.run_hub(*cmd, "--for", other, me="hub")
                self.assertNotIn("do not name", r.stdout + r.stderr)
        r = self.run_hub("trust", "hello", "--for", status, me="hub")             # installed: its short name will do
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        r = self.run_hub("update", "--accept", "--for", status, me="hub")         # every module: none is named
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("one module by name", r.stdout + r.stderr)

    def test_an_installed_plugin_is_named_by_its_short_name(self):
        # 26.11: the owner answered "Check and install" on the hub's card about updating an installed plugin; the
        # hub's `market install <its link> --for qN` was refused as not named — its short name was chosen by the owner
        # when he installed it, not by its author. A new source keeps naming itself in full.
        from core import journal
        with open(self.hub.cfg_path, "w") as f:
            json.dump({"extensions": {"alice:owl": {"source": "Alice/hub-plugin-owl", "path": "/p/owl", "commit": "a"},
                                      "alice:barn": {"source": "alice/hub-plugin-farm", "path": "/p/farm",
                                                     "commit": "b"}}}, f)
        j = journal.Journal(os.path.join(self.hub.state, "journal.jsonl"))
        word = "m" + str(j.append("message.received", None, {"to": "hub", "text": "update owl and barn",
                                                             "channel": "cli", "owner": True})["seq"])
        q = {"questions": [{"question": "owl 1.0 -> 1.1: update?", "header": "Plugin",
                            "options": [{"label": "Check and install"}, {"label": "No need"}]}]}
        j.append("session.started", "w", {})
        j.append("turn.started", "w", {"marker": None, "prompt_head": "x"})
        card = "q" + str(j.append("permission.requested", "w", {"request_id": "r9", "tool": "AskUserQuestion",
                                                                "interactive": True, "input": q})["seq"])
        j.append("turn.stopped", "w", {})
        j.append("permission.answered", "w", {"request_id": "r9", "behavior": "allow", "channel": "web", "owner": True,
                                              "answers": {q["questions"][0]["question"]: "Check and install"}})
        env = {k: v for k, v in os.environ.items() if k not in ("HUB_SESSION", "HUB_EXTENSION", "HUB_MODULE")}
        env.update(HUB_STATE_DIR=self.hub.state, HUB_CONFIG=self.hub.cfg_path, HUB_CLI_WAIT="0", HUB_SESSION="hub")
        for ref in (word, card):
            for link, named in (("Alice/hub-plugin-owl", True), ("https://github.com/alice/hub-plugin-owl.git", True),
                                ("alice/hub-plugin-farm", ref == word),   # its key alice:barn, not the repo
                                ("evil/owl", False), ("evil/hub-plugin-owl", False),   # someone else's, same name
                                ("bob/hub-plugin-newt", False)):   # new
                with self.subTest(ref=ref, link=link):
                    r = subprocess.run([sys.executable, HUB, "market", "install", link, "--reviewed", "--for", ref],
                                       env=env, capture_output=True, text=True)
                    out = r.stdout + r.stderr
                    self.assertNotEqual(r.returncode, 0, out)   # not reviewed: refused past the gate
                    (self.assertNotIn if named else self.assertIn)("do not name", out)

    def test_a_link_takes_short_names_only_from_what_it_updates(self):
        # the short names of an update are the installed record's, not the link's (its last part is the requester's
        # choice); a skill set entry is <entry>@<catalog>, not its catalog's name; a link installs the repo's root, so
        # a plugin from a folder of that repo (a catalog's entry keeps the catalog's address) is not what it updates
        from core import journal
        with open(self.hub.cfg_path, "w") as f:
            json.dump({"extensions": {"alice:owl": {"source": "alice/hub-plugin-owl", "path": "/p/owl", "commit": "a"},
                                      "carol:kite": {"source": "carol/plugins", "subdir": "kite", "path": "/p/kite",
                                                     "commit": "b", "market": "kites", "entry": "kite"}},
                       "sets": [{"name": "shop", "entry": "lark", "plugin": True, "source": "dave/hub-plugin-lark",
                                 "market": "shop", "path": "/p/lark", "commit": "c"},
                                # a plugin of skills only by link: a set under its key; a whole set is no plugin
                                {"name": "erin:wren", "source": "erin/hub-plugin-wren", "path": "/p/wren", "commit": "d"},
                                {"name": "birds", "source": "erin/hub-plugin-wren", "path": "/p/birds"}]}, f)
        j = journal.Journal(os.path.join(self.hub.state, "journal.jsonl"))
        env = {k: v for k, v in os.environ.items() if k not in ("HUB_SESSION", "HUB_EXTENSION", "HUB_MODULE")}
        env.update(HUB_STATE_DIR=self.hub.state, HUB_CONFIG=self.hub.cfg_path, HUB_CLI_WAIT="0", HUB_SESSION="hub")
        status = "https://github.com/alice/hub-plugin-owl/tree/main/status"
        for text, link, named in (("show me the status", status, False), ("update owl", status, True),
                                  ("check the shop catalog", "dave/hub-plugin-lark", False),
                                  ("update lark", "dave/hub-plugin-lark", True),
                                  ("update kite", "carol/plugins", False),
                                  ("update wren", "erin/hub-plugin-wren", True),
                                  ("check the birds set", "erin/hub-plugin-wren", False)):
            word = "m" + str(j.append("message.received", None, {"to": "hub", "text": text, "channel": "cli",
                                                                 "owner": True})["seq"])
            with self.subTest(text=text, link=link):
                r = subprocess.run([sys.executable, HUB, "market", "install", link, "--reviewed", "--for", word],
                                   env=env, capture_output=True, text=True)
                out = r.stdout + r.stderr
                self.assertNotEqual(r.returncode, 0, out)   # not reviewed: refused past the gate
                (self.assertNotIn if named else self.assertIn)("do not name", out)

    def test_the_owner_and_the_hub_on_his_word(self):
        r = self.run_hub("add", self.src.bare, "--for", self.word, me="hub")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(self.hub.cfg()["modules"]["enabled"], ["hello"])
        self.src.commit({"module.json": manifest(capabilities=["doctor"])})
        r = self.run_hub("update")
        self.assertEqual(r.returncode, 1, r.stderr + r.stdout)
        self.assertIn("--accept", r.stdout)
        r = self.run_hub("update", "hello", "--accept")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)

    def test_remove_is_the_owners_word_too(self):
        self.assertEqual(self.run_hub("add", self.src.bare).returncode, 0)
        for me, extra in (("w", []), ("hub", []), ("w", ["--for", self.word])):
            r = self.run_hub("remove", "hello", *extra, me=me)
            self.assertNotEqual(r.returncode, 0)
            out = r.stderr + r.stdout   # the gate answers a session in English; the core's refusal of a word is its own
            self.assertIn("owner", out)
        self.assertEqual(self.hub.cfg()["modules"]["enabled"], ["hello"])
        r = self.run_hub("remove", "hello", "--for", self.word, me="hub")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(self.hub.cfg()["modules"], {"sources": [], "enabled": []})
        r = self.run_hub("remove", "hello")                                   # already gone — a clear reason, a nonzero code
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("hello", r.stderr + r.stdout)


class Untouched(unittest.TestCase):
    """A hub without module sources behaves as without this slice: no folders, no records."""

    def test_no_sources_no_folders(self):
        hub = Hub(self, {"modules": {"paths": []}})
        self.assertEqual(modsrc.discover(hub.cfg(), hub.state), [])
        self.assertEqual(modsrc.update(hub.state, hub.cfg_path), (["there are no modules from git in hub.json"], True))
        self.assertEqual(sorted(os.listdir(hub.state)), ["hub.json"])
        self.assertEqual(hub.cfg(), {"modules": {"paths": []}})


if __name__ == "__main__":
    unittest.main()
