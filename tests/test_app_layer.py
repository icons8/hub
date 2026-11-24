"""The app's code layer (app spec §3.1, slice A1): the factory builds code-<version>-<flavor>.tar.gz with MANIFEST.json and
the bytecode; the open flavor leaves the built-in four out and its guard refuses a leftover; the launcher installs the
layer into <state>/app/code/<name> with `current`, checks it at every start, says what is edited and puts the release
back only when the core did not come up on an edited layer (or the owner asks)."""
import tests  # noqa: F401 - ignore the machine's settings
import importlib.util
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest

from core import appcode
from tests.fakes import cut, stands

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name):
    spec = importlib.util.spec_from_file_location(f"hub_test_{name}", os.path.join(REPO, "tools", "package", f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def g(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL).stdout.strip()


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    if mode is not None:
        os.chmod(path, mode)


def edit(path, text):
    """An edit of a read-only layer file, as a person (or an agent) would make it."""
    os.chmod(path, 0o644)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(text)


TREE = {"core/__init__.py": "", "core/version.py": "RUNTIME_MIN = 1\n", "core/x.py": "X = 1\n",
        "bin/hub-up": "print('up')\n", "tests/test_x.py": "T = 1\n", "docs/a.md": "doc\n", "evals/e.json": "{}\n",
        "modules/__init__.py": "", "modules/voice/__init__.py": "", "modules/voice/speech.py": "W = 1\n",
        "embed/acme/embed.json": '{"embed": 1, "api": "1.0"}\n', "embed/acme/modules/stats/__init__.py": "",
        "embed/acme/modules/stats/stats.py": "S = 1\n", "embed/acme/modules/voice/speak.py": "V = 1\n",
        "embed/acme/tests/test_stats.py": "T = 2\n", "README.md": "readme\n"}   # an embed: core/embed.py


class Base(unittest.TestCase):
    """A repo of releases and the layers the factory builds of it. What is built comes from the run's cache of stands
    (tests/fakes/stands.py): a step - a commit, a build - is made once per run, the other tests get a copy. A test that
    changes the repo itself (not by repo()) calls self.chain.off(); one that patches what a build runs builds without the
    cache (built(cache=False))."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hub-layer-")
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.layer = load("layer")
        self.chain = stands.Chain(self.root, "app-layer", sys.version_info[:2], stands.source_of(
            os.path.join(REPO, "tools", "package", "layer.py"), os.path.join(REPO, "core", "appcode.py")))

    def repo(self, tree=TREE, tag="v1.2.3"):
        work = os.path.join(self.root, "repo")

        def build():
            if not os.path.isdir(work):
                g(self.root, "init", "-q", "-b", "main", work)
                g(work, "config", "user.email", "x@example.test")
                g(work, "config", "user.name", "x")
            for rel, text in tree.items():
                write(os.path.join(work, rel), text, 0o755 if rel.startswith("bin/") else None)
            g(work, "add", "-A")
            # git on Windows is born with core.filemode=false: `add` records a program as 100644 (winstand, 07.11), so the
            # programs of the commit are named to the index, as they are in a real checkout
            programs = [rel for rel in tree if rel.startswith("bin/")]
            for rel in programs:
                g(work, "update-index", "--chmod=+x", rel)
            for line in g(work, "ls-files", "-s", "--", *programs).splitlines() if programs else []:
                assert line.startswith("100755 "), line
            g(work, "commit", "-qm", "c", "--allow-empty")
            if tag:
                g(work, "tag", tag)
        self.chain.step(["repo", tree, tag], ["repo"], build)
        return work

    def built(self, flavor="team", ref="v1.2.3", cache=True, **kw):
        out = os.path.join(self.root, "out")
        keep = os.path.join(self.root, f"seed-{flavor}-{ref}")
        if not cache:
            self.chain.off()
        sheet = self.chain.step(["built", flavor, ref, kw], ["out", f"seed-{flavor}-{ref}"], lambda: {
            "sheet": self.layer.build(self.work, ref, out, sys.executable, flavor=flavor, keep=keep, **kw)})["sheet"]
        return sheet, keep, os.path.join(out, sheet["archive"]["name"])


class Factory(Base):
    def setUp(self):
        super().setUp()
        self.work = self.repo()

    def test_the_layer_of_a_release_is_the_tree_without_tests_with_bytecode_and_a_manifest(self):
        sheet, seed, archive = self.built()
        self.assertEqual((sheet["name"], sheet["channel"]), ("1.2.3-team", "stable"))
        self.assertEqual(sheet["commit"], g(self.work, "rev-parse", "HEAD"))
        man = appcode.load(seed)
        self.assertEqual((man["version"], man["flavor"], man["runtime_min"]), ("1.2.3", "team", 1))
        files = set(man["files"])
        self.assertIn("core/x.py", files)
        self.assertIn("embed/acme/modules/stats/stats.py", files)      # the team flavor keeps the embeds
        self.assertFalse({f for f in files if f.startswith(("tests/", "docs/", "evals/", "embed/acme/tests/"))})
        pyc = [f for f in files if f.startswith("core/__pycache__/x.")]
        self.assertEqual(len(pyc), 1)
        with open(os.path.join(seed, *pyc[0].split("/")), "rb") as f:
            self.assertEqual(int.from_bytes(f.read(8)[4:8], "little") & 0b11, 0b11)   # checked-hash: an edit recompiles
        self.assertTrue(man["files"]["bin/hub-up"].get("x"))
        self.assertEqual(appcode.diff(seed, man, full=True), [])
        if os.name != "nt":
            self.assertEqual(os.stat(os.path.join(seed, "core", "x.py")).st_mode & 0o777, 0o444)
            self.assertEqual(os.stat(os.path.join(seed, "bin", "hub-up")).st_mode & 0o777, 0o555)
        with tarfile.open(archive) as t:
            names = t.getnames()
        self.assertIn("1.2.3-team/MANIFEST.json", names)
        self.assertEqual({n.split("/")[0] for n in names}, {"1.2.3-team"})
        self.assertEqual(sheet["archive"]["sha256"], appcode.sha256(archive))

    def test_git_in_a_layer_finds_no_repository_even_inside_one(self):
        # the layer has no git; a state folder inside a work tree (a home under git) would give `git -C <layer>` that
        # repository — the hub's update, its local edits, the private modules' address would act on it
        outer = tempfile.mkdtemp(prefix="outer-")
        self.addCleanup(appcode.remove_tree, outer)
        g(outer, "init", "-q")
        _, seed, _ = self.built()
        name, _ = appcode.install(seed, os.path.join(outer, "layers"))   # as the launcher puts it into <state>/layers
        layer = os.path.join(outer, "layers", name)
        r = subprocess.run(["git", "-C", layer, "rev-parse", "--absolute-git-dir"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertEqual(appcode.diff(layer, appcode.load(layer), full=True), [])

    def test_the_same_commit_gives_the_same_archive(self):
        _, _, first = self.built(cache=False)
        with open(first, "rb") as f:
            one = f.read()
        # an hour later (a slow machine crossed a second between two builds — the Windows stand, 07.11): the same bytes
        from unittest import mock
        real, asked = appcode.time.gmtime, []
        with mock.patch.object(appcode.time, "gmtime", lambda secs=None: asked.append(secs) or real(appcode.time.time() + 3600 if secs is None else secs)):
            _, _, second = self.built(cache=False)
        self.assertTrue(asked)   # the second layer was made here, under the patch, not taken from the cache of stands
        with open(second, "rb") as f:
            self.assertEqual(one, f.read())

    def test_programs_are_the_commits_not_the_file_systems(self):
        # git archive carries 0755 for a program; unpacked on Windows the mode is gone (os.stat has no x bit there), so
        # the layer of the same commit would differ by the OS that built it (the Windows stand, 07.11)
        from unittest import mock
        real = self.layer.tarfile.TarFile.extractall

        unpacked = []

        def no_modes(t, path, *a, **kw):   # what an unpack on Windows leaves: no program bits
            unpacked.append(path)
            real(t, path, *a, **kw)
            for d, _, names in os.walk(path):
                for n in names:
                    os.chmod(os.path.join(d, n), 0o644)
        with mock.patch.object(self.layer.tarfile.TarFile, "extractall", no_modes):
            _, seed, _ = self.built(cache=False)
        self.assertTrue(unpacked)   # built here, under the patch, not taken from the cache of stands
        man = appcode.load(seed)
        self.assertTrue(man["files"]["bin/hub-up"].get("x"))
        self.assertFalse(man["files"]["core/x.py"].get("x"))
        # the archive's modes are the manifest's too, not what the file system of the factory reports (Windows: 0o444
        # for a program without an extension, 0o555 for a .cmd)
        for rel in ("bin/hub-up", "core/x.py"):
            os.chmod(os.path.join(seed, *rel.split("/")), 0o644)
        archive = os.path.join(self.root, "again.tar.gz")
        self.layer.pack(seed, archive, man)
        with tarfile.open(archive) as t:
            modes = {m.name.split("/", 1)[1]: m.mode for m in t.getmembers() if m.isreg()}
        self.assertEqual((modes["bin/hub-up"], modes["core/x.py"]), (0o555, 0o444))

    def test_a_commit_that_is_not_a_release_is_a_nightly_after_the_latest_release(self):
        self.repo({"core/y.py": "Y = 1\n"}, tag=None)
        sheet, _, _ = self.built(ref="HEAD")
        self.assertEqual(sheet["channel"], "nightly")
        self.assertTrue(sheet["version"].startswith("1.2.3-nightly."), sheet["version"])
        self.assertTrue(sheet["version"].endswith(g(self.work, "rev-parse", "HEAD")[:9]))
        self.assertGreater(appcode.version_key(sheet["version"]), appcode.version_key("1.2.3"))
        self.assertLess(appcode.version_key(sheet["version"]), appcode.version_key("1.2.4"))

    def test_the_open_flavor_leaves_the_embeds_out(self):
        sheet, seed, _ = self.built("open")
        files = set(appcode.load(seed)["files"])
        self.assertEqual(sheet["name"], "1.2.3-open")
        self.assertFalse([f for f in files if f.startswith("embed/")])
        self.assertIn("modules/voice/speech.py", files)               # the voice's core part stays
        self.assertEqual(appcode.open_guard(seed), [])

    def test_the_guard_refuses_an_open_layer_with_a_forbidden_string_or_a_cut_file(self):
        self.repo({"core/addr.py": "URL = 'https://dist.example.test/v1'\n"}, tag=None)
        g(self.work, "tag", "-f", "v1.2.3")
        self.chain.off()
        with self.assertRaises(self.layer.Refused) as e:
            self.built("open", forbid=["dist.example.test"])
        self.assertIn("core/addr.py", str(e.exception))
        self.assertFalse([n for n in os.listdir(os.path.join(self.root, "out")) if "open" in n])   # nothing is left
        self.built("team", forbid=["dist.example.test"])   # the team flavor carries it by design
        _, seed, _ = self.built("open")
        write(os.path.join(seed, "embed", "acme", "modules", "conduit", "conduit.py"), "C = 1\n")
        self.assertEqual(appcode.open_guard(seed), ["embed/acme/modules/conduit/conduit.py: cut from the open flavor"])

    def test_a_link_in_the_tree_is_refused(self):
        if os.name == "nt":
            self.skipTest("git on Windows checks a link out as a file")
        os.symlink("x.py", os.path.join(self.work, "core", "l.py"))   # portable: skipped on Windows above
        g(self.work, "add", "-A")
        g(self.work, "commit", "-qm", "l")
        self.chain.off()
        with self.assertRaises(self.layer.Refused):
            self.built(ref="HEAD")


class Check(Base):
    def setUp(self):
        super().setUp()
        self.work = self.repo()
        _, self.seed, self.archive = self.built()
        self.codes = os.path.join(self.root, "codes")
        self.name, self.man = appcode.unpack(self.archive, self.codes)
        self.dir = os.path.join(self.codes, self.name)

    def test_an_unpacked_layer_matches_its_manifest(self):
        self.assertEqual(self.name, "1.2.3-team")
        self.assertEqual(appcode.diff(self.dir, self.man, full=True), [])

    def test_an_edit_a_new_file_and_a_removed_one_are_named_and_caches_are_not(self):
        edit(os.path.join(self.dir, "core", "x.py"), "X = 2\n")
        write(os.path.join(self.dir, "core", "new.py"), "N = 1\n")
        os.chmod(os.path.join(self.dir, "README.md"), 0o644)
        os.unlink(os.path.join(self.dir, "README.md"))
        write(os.path.join(self.dir, "core", "__pycache__", "x.cpython-399.pyc"), "other python")
        write(os.path.join(self.dir, "PACKAGED"), "{}")
        self.assertEqual(appcode.diff(self.dir, self.man), [
            {"path": "README.md", "kind": "missing"}, {"path": "core/new.py", "kind": "extra"},
            {"path": "core/x.py", "kind": "changed"}])

    def test_the_full_check_sees_an_edit_that_kept_the_size_and_time(self):
        path = os.path.join(self.dir, "core", "x.py")
        os.chmod(path, 0o644)
        with open(path, "w", newline="\n") as f:
            f.write("X = 9\n")
        os.utime(path, (self.man["mtime"], self.man["mtime"]))
        self.assertEqual(appcode.diff(self.dir, self.man), [])
        self.assertEqual(appcode.diff(self.dir, self.man, full=True), [{"path": "core/x.py", "kind": "changed"}])

    def test_restore_puts_the_release_back_and_keeps_the_edit_aside(self):
        edit(os.path.join(self.dir, "core", "x.py"), "X = 2\n")
        write(os.path.join(self.dir, "core", "new.py"), "N = 1\n")
        got = appcode.restore(self.dir, self.archive, stamp="20261107-120000")
        self.assertEqual([c["path"] for c in got["changes"]], ["core/new.py", "core/x.py"])
        self.assertEqual(appcode.diff(self.dir, self.man, full=True), [])
        self.assertEqual(got["copy"], os.path.join(self.codes, "1.2.3-team-modified-20261107-120000"))
        with open(os.path.join(got["copy"], "core", "x.py")) as f:
            self.assertIn("X = 2", f.read())
        self.assertTrue(os.path.isfile(os.path.join(got["copy"], "core", "new.py")))
        edit(os.path.join(self.dir, "core", "x.py"), "X = 3\n")   # from the seed folder too; one copy is kept
        appcode.restore(self.dir, self.seed, stamp="20261108-120000")
        self.assertEqual(sorted(n for n in os.listdir(self.codes) if "modified" in n),
                         ["1.2.3-team-modified-20261108-120000"])

    def test_an_edit_that_cannot_be_copied_aside_stops_the_restore(self):
        # audit: an unreadable edited file (chmod 000) — the copy failed silently, then the release's file replaced the
        # edit; and the previous copy went before the new one was whole
        import shutil
        from unittest import mock
        edit(os.path.join(self.dir, "core", "x.py"), "X = 2\n")
        appcode.restore(self.dir, self.archive, stamp="20261107-120000")
        edit(os.path.join(self.dir, "core", "x.py"), "X = 3\n")
        real = shutil.copyfile

        def copy(src, dst, **kw):
            if src.endswith("x.py"):
                raise PermissionError(13, "Permission denied", src)
            return real(src, dst, **kw)
        with mock.patch.object(appcode.shutil, "copyfile", copy), self.assertRaises(appcode.Bad):
            appcode.restore(self.dir, self.archive, stamp="20261108-120000")
        with open(os.path.join(self.dir, "core", "x.py")) as f:
            self.assertIn("X = 3", f.read())                  # the edit is where it was
        self.assertEqual(sorted(n for n in os.listdir(self.codes) if "modified" in n),
                         ["1.2.3-team-modified-20261107-120000"])   # the previous copy stays, no half one

    def test_a_pipe_in_place_of_a_file_has_nothing_to_keep_and_is_restored(self):
        if os.name == "nt":
            self.skipTest("no named pipes in the file system on Windows")
        path = os.path.join(self.dir, "core", "x.py")
        edit(os.path.join(self.dir, "core", "version.py"), "V = 2\n")
        kept = appcode.restore(self.dir, self.archive, stamp="20261107-120000")["copy"]
        appcode._unlink(path)
        os.mkfifo(path)   # portable: skipped on Windows above
        self.assertIsNone(appcode.restore(self.dir, self.archive, stamp="20261108-120000")["copy"])
        self.assertEqual(appcode.diff(self.dir, self.man, full=True), [])
        self.assertTrue(os.path.isfile(os.path.join(kept, "core", "version.py")))   # the copy with bytes stays

    def test_a_folder_in_place_of_a_file_stops_the_restore_before_anything_is_written(self):
        path = os.path.join(self.dir, "core", "x.py")
        edit(os.path.join(self.dir, "core", "version.py"), "V = 2\n")
        appcode._unlink(path)
        write(os.path.join(path, "mine.py"), "M = 1\n")
        with self.assertRaises(appcode.Bad):
            appcode.restore(self.dir, self.archive)
        with open(os.path.join(self.dir, "core", "version.py")) as f:
            self.assertIn("V = 2", f.read())   # nothing half-done

    def test_a_copy_of_the_same_second_is_not_overwritten(self):
        edit(os.path.join(self.dir, "core", "x.py"), "X = 2\n")
        first = appcode.restore(self.dir, self.archive, stamp="20261107-120000")["copy"]
        edit(os.path.join(self.dir, "core", "x.py"), "X = 3\n")
        second = appcode.restore(self.dir, self.archive, stamp="20261107-120000")["copy"]
        self.assertNotEqual(first, second)
        with open(os.path.join(second, "core", "x.py")) as f:
            self.assertIn("X = 3", f.read())

    def test_a_module_python_would_import_is_an_edit_a_cache_is_not(self):
        write(os.path.join(self.dir, "core", "foo.pyc"), "sourceless")
        write(os.path.join(self.dir, "core", "__pycache__", "foo.cpython-399.pyc"), "a cache")
        self.assertEqual(appcode.diff(self.dir, self.man), [{"path": "core/foo.pyc", "kind": "extra"}])

    def test_a_link_into_the_layer_is_an_edit_and_goes_with_the_restore(self):
        if os.name == "nt":
            self.skipTest("a link needs a right on Windows")
        other = os.path.join(self.root, "elsewhere")
        write(os.path.join(other, "m.py"), "M = 1\n")
        os.symlink(other, os.path.join(self.dir, "modules", "x"))   # portable: skipped on Windows above
        self.assertEqual(appcode.diff(self.dir, self.man), [{"path": "modules/x", "kind": "extra"}])
        appcode.restore(self.dir, self.archive)
        self.assertFalse(os.path.lexists(os.path.join(self.dir, "modules", "x")))
        self.assertTrue(os.path.isfile(os.path.join(other, "m.py")))   # where it led is not touched

    def test_a_folder_swapped_for_a_link_comes_back_whole(self):
        if os.name == "nt":
            self.skipTest("a link needs a right on Windows")
        import shutil
        other = os.path.join(self.root, "core-copy")
        shutil.copytree(os.path.join(self.dir, "core"), other)
        appcode.remove_tree(os.path.join(self.dir, "core"))
        os.symlink(other, os.path.join(self.dir, "core"))   # portable: skipped on Windows above
        kinds = {c["kind"] for c in appcode.diff(self.dir, self.man) if c["path"].startswith("core/")}
        self.assertEqual(kinds, {"missing"})                 # what is under the link is not the layer's
        appcode.restore(self.dir, self.archive)
        self.assertFalse(os.path.islink(os.path.join(self.dir, "core")))
        self.assertEqual(appcode.diff(self.dir, self.man, full=True), [])

    def test_another_build_of_the_same_version_is_refused(self):
        edit(os.path.join(self.dir, "core", "x.py"), "X = 2\n")
        self.repo({"core/x.py": "X = 7\n"}, tag=None)
        g(self.work, "tag", "-f", "v1.2.3")                  # the tag moved: a rebuilt release
        self.chain.off()
        _, rebuilt, _ = self.built()
        with self.assertRaises(appcode.Bad):
            appcode.restore(self.dir, rebuilt)

    def test_restore_from_another_release_touches_nothing(self):
        edit(os.path.join(self.dir, "core", "x.py"), "X = 2\n")
        self.repo({"core/x.py": "X = 5\n"}, tag="v1.2.4")
        _, other, _ = self.built(ref="v1.2.4")
        with self.assertRaises(appcode.Bad):
            appcode.restore(self.dir, other)
        with open(os.path.join(self.dir, "core", "x.py")) as f:
            self.assertIn("X = 2", f.read())

    def test_an_archive_with_a_path_outside_is_refused(self):
        bad = os.path.join(self.root, "bad.tar.gz")
        with tarfile.open(bad, "w:gz") as t:
            t.add(self.dir, arcname="1.2.3-team")
            t.add(os.path.join(self.dir, "README.md"), arcname="1.2.3-team/../evil")
        with self.assertRaises(appcode.Bad):
            appcode.unpack(bad, os.path.join(self.root, "other"))
        self.assertFalse(os.path.exists(os.path.join(self.root, "evil")))


class Launcher(Base):
    def setUp(self):
        super().setUp()
        self.work = self.repo()
        _, self.seed, self.archive = self.built()
        self.launch = load("launch")
        self.state = os.path.join(self.root, "state")
        self.mark = os.path.join(self.root, "PACKAGED")
        write(self.mark, json.dumps({"format": "app", "version": "1.2.3", "runtime": 1}))
        self.clock = [1000.0]
        self.spawned = []

    def start(self, seed=None):
        return self.launch.ensure_layer(seed or self.seed, self.mark, self.state, now=lambda: self.clock[0],
                                        run=self.spawned.append)

    def read(self, name):
        with open(os.path.join(self.state, "layers", name)) as f:
            return json.load(f)

    def test_the_first_start_installs_the_layer_and_points_current_at_it(self):
        code = self.start()
        codes = os.path.join(self.state, "layers")
        self.assertEqual(code, os.path.join(self.state, "code"))          # the path the shell and its services keep
        self.assertEqual(os.path.realpath(code), os.path.realpath(os.path.join(codes, "1.2.3-team")))
        self.assertEqual(appcode.current(code, codes), "1.2.3-team")
        with open(os.path.join(code, "PACKAGED")) as f:
            self.assertEqual(json.load(f)["format"], "app")
        self.assertEqual(self.read("check.json")["count"], 0)
        self.assertEqual(self.read("start.json")["modified"], False)
        self.assertEqual(self.spawned, [])                  # nothing edited: no watchdog
        self.assertEqual(self.start(), code)                # the second start keeps it

    def test_an_edited_layer_is_named_and_left_alone_while_it_starts(self):
        code = self.start()
        edit(os.path.join(code, "core", "x.py"), "X = 2\n")
        self.clock[0] += 10
        self.start()
        self.assertEqual(self.read("check.json")["changes"], [{"path": "core/x.py", "kind": "changed"}])
        self.assertTrue(self.read("start.json")["modified"])
        self.assertEqual(len(self.spawned), 1)              # the watchdog of this start
        self.assertEqual(self.spawned[0][1:3], ["-I", "-B"])   # nothing written next to the shell's files
        self.assertEqual(self.spawned[0][-2:], ["--watch", self.state])
        with open(os.path.join(code, "core", "x.py")) as f:
            self.assertIn("X = 2", f.read())                # a working edit is not touched

    def test_a_shell_started_beside_a_running_hub_does_not_restart_the_watch(self):
        code = self.start()
        edit(os.path.join(code, "core", "x.py"), "X = 2\n")
        self.start()
        start = self.read("start.json")
        write(os.path.join(self.state, "layers", "start.json"), json.dumps({**start, "ok": True}))   # the core is up
        write(os.path.join(self.state, "hub-up.json"), json.dumps({"core": os.getpid()}))
        self.spawned.clear()
        self.clock[0] += 10 * self.launch.REPAIR_AFTER
        self.start()                                          # a menu entry again, the hub running
        self.assertTrue(self.read("start.json")["ok"])
        self.assertEqual(self.spawned, [])
        with open(os.path.join(code, "core", "x.py")) as f:
            self.assertIn("X = 2", f.read())

    def test_a_core_that_came_up_on_an_edited_layer_keeps_the_edit(self):
        code = self.start()
        edit(os.path.join(code, "core", "x.py"), "X = 2\n")
        self.start()
        start = self.read("start.json")
        start["ok"] = True                                    # the core marks it (core/appwatch.py)
        write(os.path.join(self.state, "layers", "start.json"), json.dumps(start))
        self.clock[0] += 10 * self.launch.REPAIR_AFTER
        self.start()
        with open(os.path.join(code, "core", "x.py")) as f:
            self.assertIn("X = 2", f.read())

    def test_the_watchdog_puts_the_release_back_when_the_core_does_not_come_up(self):
        code = self.start()
        edit(os.path.join(code, "core", "x.py"), "X = 2\n")
        self.start()
        sleeps = []
        clock = lambda: self.clock[0]   # noqa: E731

        def sleep(s):
            sleeps.append(s)
            self.clock[0] += s
        self.assertEqual(self.launch.watch(self.state, now=clock, sleep=sleep, clock=clock), "repaired")
        self.assertGreaterEqual(sum(sleeps), self.launch.REPAIR_AFTER - self.launch.WATCH_TICK)
        with open(os.path.join(code, "core", "x.py")) as f:
            self.assertEqual(f.read(), "X = 1\n")
        with open(os.path.join(self.state, "restart")) as f:
            self.assertEqual(json.load(f)["parts"], [])        # hub-up restarts on the released code
        self.assertFalse(os.path.exists(os.path.join(self.state, "layers", "watch.json")))

    def test_the_watchdog_leaves_when_the_core_marks_the_start(self):
        code = self.start()
        edit(os.path.join(code, "core", "x.py"), "X = 2\n")
        self.start()

        def sleep(s):
            start = self.read("start.json")
            write(os.path.join(self.state, "layers", "start.json"), json.dumps({**start, "ok": True}))
        self.assertEqual(self.launch.watch(self.state, now=lambda: self.clock[0], sleep=sleep,
                                           clock=lambda: self.clock[0]), "ok")
        with open(os.path.join(code, "core", "x.py")) as f:
            self.assertIn("X = 2", f.read())

    def test_the_watchdogs_window_is_its_own_not_the_wall_clocks_since_an_earlier_start(self):
        code = self.start()
        edit(os.path.join(code, "core", "x.py"), "X = 2\n")
        self.start()
        self.clock[0] += 3600                                  # the machine slept, or rebooted, before the core came up
        self.start()
        ticks = []

        def sleep(s):
            ticks.append(s)
            if len(ticks) == 3:                                # the core comes up within the new window
                start = self.read("start.json")
                write(os.path.join(self.state, "layers", "start.json"), json.dumps({**start, "ok": True}))
        mono = iter(range(0, 10 ** 6, self.launch.WATCH_TICK))
        self.assertEqual(self.launch.watch(self.state, now=lambda: self.clock[0], sleep=sleep,
                                           clock=lambda: next(mono)), "ok")
        with open(os.path.join(code, "core", "x.py")) as f:
            self.assertIn("X = 2", f.read())

    def test_a_broken_release_archive_is_said_not_raised(self):
        code = self.start()
        edit(os.path.join(code, "core", "x.py"), "X = 2\n")
        archives = os.path.join(self.state, "layers", "archives")
        write(os.path.join(archives, "code-1.2.3-team.tar.gz"), "not an archive")
        write(os.path.join(self.state, "layers", "shell.json"), json.dumps({"seed": os.path.join(self.root, "nowhere")}))
        self.assertFalse(self.launch.repair(self.state, "1.2.3-team", "owner"))
        self.assertIn("not a release", self.read("restored.json")["error"])

    def test_an_edited_manifest_is_one_change_and_the_release_puts_it_back(self):
        code = self.start()
        path = os.path.join(code, "MANIFEST.json")
        os.chmod(path, 0o644)
        write(path, "{broken")
        self.start()                                            # the start does not fail on it
        self.assertEqual(self.read("check.json")["changes"], [{"path": "MANIFEST.json", "kind": "changed"}])
        self.assertEqual(self.launch.restore_now(self.state), 0)
        self.assertEqual(appcode.diff(code, appcode.load(code), full=True), [])

    def test_the_other_flavor_of_the_same_release_takes_over(self):
        self.start()
        _, open_seed, _ = self.built("open")
        link = self.start(open_seed)
        self.assertEqual(appcode.current(link, os.path.join(self.state, "layers")), "1.2.3-open")

    def test_return_the_released_by_the_owner(self):
        code = self.start()
        edit(os.path.join(code, "core", "x.py"), "X = 2\n")
        self.assertEqual(self.launch.restore_now(self.state, now=lambda: self.clock[0]), 0)
        with open(os.path.join(code, "core", "x.py")) as f:
            self.assertEqual(f.read(), "X = 1\n")
        self.assertEqual(self.read("restored.json")["why"], "owner")
        self.assertTrue(os.path.isfile(os.path.join(self.state, "restart")))

    def test_without_a_copy_of_the_release_nothing_is_touched(self):
        code = self.start()
        edit(os.path.join(code, "core", "x.py"), "X = 2\n")
        write(os.path.join(self.state, "layers", "shell.json"), json.dumps({"seed": os.path.join(self.root, "nowhere")}))
        self.assertEqual(self.launch.restore_now(self.state), 1)
        self.assertIn("no copy", self.read("restored.json")["error"])
        with open(os.path.join(code, "core", "x.py")) as f:
            self.assertIn("X = 2", f.read())

    def test_a_newer_shell_moves_current_to_its_layer_and_an_older_one_does_not(self):
        self.start()
        self.repo({"core/x.py": "X = 4\n"}, tag="v1.2.4")
        _, newer, _ = self.built(ref="v1.2.4")
        codes = os.path.join(self.state, "layers")
        link = self.start(newer)
        self.assertEqual(appcode.current(link, codes), "1.2.4-team")
        self.start(self.seed)                                                      # an older installer over it
        self.assertEqual(appcode.current(link, codes), "1.2.4-team")
        self.assertTrue(os.path.isdir(os.path.join(codes, "1.2.3-team")))          # the previous stays for a return

    def test_a_newer_shell_beside_a_running_hub_moves_it_and_asks_for_a_restart(self):
        self.start()
        write(os.path.join(self.state, "hub-up.json"), json.dumps({"core": os.getpid()}))
        self.repo({"core/x.py": "X = 4\n"}, tag="v1.2.4")
        _, newer, _ = self.built(ref="v1.2.4")
        link = self.start(newer)
        self.assertEqual(appcode.current(link, os.path.join(self.state, "layers")), "1.2.4-team")
        with open(os.path.join(self.state, "restart")) as f:
            self.assertEqual(json.load(f)["parts"], [])

    def test_a_layer_whose_manifest_does_not_read_is_not_moved_on_a_guess(self):
        code = self.start()
        os.chmod(os.path.join(code, "MANIFEST.json"), 0o644)
        write(os.path.join(code, "MANIFEST.json"), "{broken")
        self.repo({"core/x.py": "X = 4\n"}, tag="v1.2.4")
        _, newer, _ = self.built(ref="v1.2.4")
        self.assertEqual(appcode.current(self.start(newer), os.path.join(self.state, "layers")), "1.2.3-team")

    def test_a_newer_shell_does_not_bring_a_release_that_did_not_come_up(self):
        self.start()
        self.repo({"core/x.py": "X = 4\n"}, tag="v1.2.4")
        _, newer, _ = self.built(ref="v1.2.4")
        write(os.path.join(self.state, "layers", "failed.json"), json.dumps({"1.2.4-team": {"at": 1}}))
        self.assertEqual(appcode.current(self.start(newer), os.path.join(self.state, "layers")), "1.2.3-team")

    def test_the_earlier_clone_folder_is_moved_aside_not_deleted(self):
        write(os.path.join(self.state, "code", "core", "mine.py"), "M = 1\n")
        link = self.start()
        self.assertEqual(appcode.current(link, os.path.join(self.state, "layers")), "1.2.3-team")
        kept = [n for n in os.listdir(self.state) if n.startswith("code-clone-")]
        self.assertEqual(len(kept), 1)
        self.assertTrue(os.path.isfile(os.path.join(self.state, kept[0], "core", "mine.py")))

    def test_a_clone_seed_keeps_the_clone_path(self):
        self.assertFalse(self.launch.is_layer(self.work))
        self.assertTrue(self.launch.is_layer(self.seed))


class Core(Base):
    """hubd on the layer it runs from: the start is marked, an edit is one fact, a restore of the launcher is recorded."""

    def setUp(self):
        super().setUp()
        from unittest import mock
        from core import packaged
        from core.daemon import Hub
        import types
        self.work = self.repo()
        _, self.seed, _ = self.built()
        self.launch = load("launch")
        self.state = os.path.join(self.root, "state")
        mark = os.path.join(self.root, "PACKAGED")
        write(mark, json.dumps({"format": "app", "version": "1.2.3", "runtime": 1}))
        self.code = self.launch.ensure_layer(self.seed, mark, self.state, run=lambda cmd: None)
        for patch in (mock.patch.object(packaged, "REPO", self.code),
                      mock.patch.object(packaged, "MARK", os.path.join(self.code, "PACKAGED")),
                      mock.patch.dict(os.environ, {"HUB_CONFIG": os.path.join(self.root, "hub.json")})):
            patch.start()
            self.addCleanup(patch.stop)
        self.now = [1000.0]
        self.hub = Hub(self.state, types.SimpleNamespace(poll=lambda: []), clock=lambda: self.now[0])
        self.hub.execute = lambda a: None

    def facts(self, kind):
        from core import journal
        return [e["data"] for e in journal.read(self.hub.journal.path) if e["type"] == kind]

    def test_a_clean_layer_writes_nothing_and_marks_the_start(self):
        self.hub.watch_app()
        self.assertEqual(self.facts("app.code_modified"), [])
        with open(os.path.join(self.state, "layers", "start.json")) as f:
            self.assertTrue(json.load(f)["ok"])

    def test_an_edit_is_one_fact_until_it_changes(self):
        self.hub.watch_app()
        edit(os.path.join(self.code, "core", "x.py"), "X = 2\n")
        self.hub.watch_app()
        self.assertEqual(self.facts("app.code_modified"), [])          # the first full check comes 10 minutes later
        self.now[0] += 24 * 3600
        self.hub.watch_app()
        self.now[0] += 24 * 3600
        self.hub.watch_app()
        self.assertEqual(self.facts("app.code_modified"),
                         [{"name": "1.2.3-team", "count": 1, "paths": ["core/x.py"]}])
        self.assertEqual(self.hub.state["app_code"]["count"], 1)

    def test_a_move_marked_done_after_the_first_tick_is_recorded_within_a_minute(self):
        # core/appmove.py: the shell marks the move done only once the page answers — after hubd's first tick
        path = os.path.join(self.state, "app-move", "record.json")
        os.makedirs(os.path.dirname(path))
        write(path, json.dumps({"status": "moving", "repo": "/home/x/agents-hub-core", "at": 900.0}))
        self.hub.watch_app()
        write(path, json.dumps({"status": "moved", "repo": "/home/x/agents-hub-core", "at": 900.0, "done": 1001.0}))
        for _ in range(3):   # ticks within the minute: not looked at yet
            self.now[0] += 1
            self.hub.watch_app()
        self.assertEqual(self.facts("app.moved"), [])
        self.now[0] += 60
        self.hub.watch_app()
        self.now[0] += 61
        self.hub.watch_app()
        self.assertEqual(self.facts("app.moved"), [{"repo": "/home/x/agents-hub-core", "done": 1001.0, "at": 900.0}])
        self.assertEqual(self.hub.state["app_move"]["repo"], "/home/x/agents-hub-core")

    def test_the_first_full_check_comes_soon_after_the_start(self):
        self.hub.watch_app()
        path = os.path.join(self.code, "core", "x.py")
        mtime = appcode.load(self.code)["mtime"]
        os.chmod(path, 0o644)
        with open(path, "w", newline="\n") as f:
            f.write("X = 9\n")                                        # the same size and time: only the sha256 sees it
        os.utime(path, (mtime, mtime))
        self.now[0] += 601
        self.hub.watch_app()
        self.assertEqual([f["paths"] for f in self.facts("app.code_modified")], [["core/x.py"]])

    def test_an_edit_is_seen_at_the_start(self):
        edit(os.path.join(self.code, "core", "x.py"), "X = 2\n")
        self.hub.watch_app()
        self.assertEqual([f["count"] for f in self.facts("app.code_modified")], [1])
        from core.views import Reader
        from core import reducer
        r = Reader()
        r.state = reducer.copy_state(self.hub.state)
        self.assertEqual(r.snapshot(self.now[0])["app_code"]["paths"], ["core/x.py"])

    def test_the_launchers_restore_is_recorded_and_the_layer_is_clean_again(self):
        edit(os.path.join(self.code, "core", "x.py"), "X = 2\n")
        self.hub.watch_app()
        self.assertEqual(self.launch.restore_now(self.state, now=lambda: 2000.0), 0)
        self.hub.watch_app()
        self.hub.watch_app()
        restored = self.facts("app.code_restored")
        self.assertEqual([(r["why"], r["count"], r["paths"]) for r in restored], [("owner", 1, ["core/x.py"])])
        self.assertEqual([f["count"] for f in self.facts("app.code_modified")], [1, 0])
        self.assertEqual(self.hub.state["app_code"]["restored"]["why"], "owner")

    def test_the_facts_are_not_taken_from_the_mailbox(self):
        from core import spool, journal
        self.hub.watch_app()
        box = os.path.join(self.state, "spool")
        for kind, data in (("app.code_modified", {"name": "1.2.3-team", "count": 9, "paths": ["x"]}),
                           ("app.code_restored", {"name": "1.2.3-team", "at": 1, "why": "owner", "paths": []})):
            spool.put(box, kind, None, {**data, "channel": "web", "owner": True}, src="cli")
            self.hub.ingest()
        self.assertNotIn("app_code", self.hub.state)
        refused = [e["data"]["type"] for e in journal.read(self.hub.journal.path) if e["type"] == "spool.refused"]
        self.assertEqual(refused, ["app.code_modified", "app.code_restored"])

    @cut.needs("stats")
    def test_the_stats_report_names_the_release_and_the_count_of_edited_files(self):
        from modules.stats import stats
        self.assertEqual(stats.app_layer(self.code), {"version": "1.2.3", "app": {"layer": "1.2.3-team", "modified": 0}})
        edit(os.path.join(self.code, "core", "x.py"), "X = 2\n")
        self.assertEqual(stats.app_layer(self.code)["app"]["modified"], 1)
        self.assertEqual(stats.app_layer(self.work), {})          # a hub from git: no field

    def test_a_layer_that_cannot_be_read_now_does_not_stop_the_core(self):
        from unittest import mock
        from core import appcode as ac
        with mock.patch.object(ac, "diff", side_effect=PermissionError("held")):
            self.hub.watch_app()                                   # no exception out of the tick
        self.assertEqual(self.facts("app.code_modified"), [])
        self.now[0] += 61
        edit(os.path.join(self.code, "core", "x.py"), "X = 2\n")
        self.hub.watch_app()                                       # tried again a minute later
        self.assertEqual([f["count"] for f in self.facts("app.code_modified")], [1])

    def test_the_start_names_the_release_the_core_runs(self):
        # the fold ends an update's "waits for the restart" of that release (tests/test_app_update.RestartTaken)
        self.assertEqual(self.hub.app_release(), ("1.2.3", "1.2.3"))
        self.hub.start()
        self.assertEqual(self.facts("core.started")[-1]["release"], "1.2.3")

    def test_a_hub_from_git_never_looks(self):
        from unittest import mock
        from core import packaged
        with mock.patch.object(packaged, "MARK", os.path.join(self.root, "none")):
            from core.daemon import Hub
            import types
            hub = Hub(os.path.join(self.root, "s2"), types.SimpleNamespace(poll=lambda: []), clock=lambda: 0.0)
            hub.watch_app()
            self.assertNotIn("release", hub.started_data())
        self.assertIs(hub.app_layer, False)


class Page(Base):
    """POST /api/app/restore, "Return the released" in "About the hub": the shell's own launcher does it."""

    def setUp(self):
        super().setUp()
        from unittest import mock
        from core import packaged
        from tests.test_web import WebTest
        self.helper = WebTest("test_page_and_snapshot")
        self.helper.setUp()
        self.addCleanup(self.helper.tearDown)
        self.work = self.repo()
        _, self.seed, _ = self.built()
        self.launch = load("launch")
        self.state = self.helper.state
        mark = os.path.join(self.root, "PACKAGED")
        write(mark, json.dumps({"format": "app", "version": "1.2.3", "runtime": 1}))
        self.code = self.launch.ensure_layer(self.seed, mark, self.state, run=lambda cmd: None)
        self.started = []
        for patch in (mock.patch.object(packaged, "REPO", self.code),
                      mock.patch.object(packaged, "MARK", os.path.join(self.code, "PACKAGED")),
                      mock.patch("subprocess.Popen", lambda cmd, **kw: self.started.append(cmd))):
            patch.start()
            self.addCleanup(patch.stop)

    def press(self):
        code, out = self.helper.req("POST", "/api/app/restore", {})
        return code, json.loads(out)

    def test_the_button_starts_the_shells_launcher(self):
        code, out = self.press()
        self.assertEqual(code, 202, out)
        self.assertEqual(self.started, [[sys.executable, "-I", "-B", os.path.abspath(self.launch.__file__), "--restore",
                                         self.state]])

    def test_a_hub_from_git_has_nothing_to_put_back(self):
        from unittest import mock
        from core import packaged
        with mock.patch.object(packaged, "MARK", os.path.join(self.root, "none")):
            self.assertEqual(self.press()[0], 409)
        self.assertEqual(self.started, [])

    def test_a_planted_launcher_is_not_run(self):
        path = os.path.join(self.state, "layers", "shell.json")
        write(path, json.dumps({"launch": os.path.join(self.root, "evil.py"), "python": sys.executable}))
        write(os.path.join(self.root, "evil.py"), "")
        self.assertEqual(self.press()[0], 409)
        self.assertEqual(self.started, [])


if __name__ == "__main__":
    unittest.main()
