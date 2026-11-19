"""The cache of built test stands (tests/fakes/stands.py): a stand is built once for the run, every other test gets a copy
in its own folder; a wrong or damaged entry is never given out."""
import tests  # noqa: F401 - ignore the machine's settings
import json
import os
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

import platforms
from core import appcode
from tests.fakes import stands


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.test", *args], cwd=cwd, check=True,
                          capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout.strip()


TIME = 1_700_000_000 * 10 ** 9   # a file time the stand sets and its copy has to keep (a layer's check goes by it)


class Cached(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hub-stands-")
        self.addCleanup(appcode.remove_tree, self.tmp)
        self.base = os.path.join(self.tmp, "cache")
        os.makedirs(self.base)
        env = mock.patch.dict(os.environ, {stands.ENV: self.base})
        env.start()
        self.addCleanup(env.stop)
        self.builds = []
        self.build_calls = []

    def root(self):
        return tempfile.mkdtemp(prefix="root-", dir=self.tmp)

    def entries(self):
        return sorted(os.path.join(self.base, d) for d in os.listdir(self.base) if not d.endswith((".lock", ".nocache")))

    def text_of(self, entry):
        with open(os.path.join(entry, stands.META), encoding="utf-8") as f:
            return json.load(f)["data"]["text"]

    def build_in(self, root, text="one"):
        """A stand: a folder of files (one read-only, one a program, a nested folder) and a repo with a clone of it."""
        def build():
            self.builds.append(root)
            work = os.path.join(root, "work")
            os.makedirs(os.path.join(work, "sub"))
            for rel, body in (("a.txt", text), ("sub/b.txt", "b\n")):
                with open(os.path.join(work, rel), "w") as f:
                    f.write(body)
            os.chmod(os.path.join(work, "sub", "b.txt"), 0o444)
            os.utime(os.path.join(work, "a.txt"), ns=(TIME, TIME))
            with open(os.path.join(work, "run.sh"), "w") as f:
                f.write("#!/bin/sh\n")
            os.chmod(os.path.join(work, "run.sh"), 0o755)
            up = os.path.join(root, "up.git")
            git(root, "init", "-q", "--bare", "-b", "main", up)
            git(root, "clone", "-q", up, os.path.join(root, "copy"))
            return {"text": text}
        return build

    def get(self, root, text="one", parts=None):
        return stands.cached(parts or ["t", text], root, ["work", "up.git", "copy"], self.build_in(root, text))

    def test_a_stand_is_built_once_and_the_others_get_a_copy_of_it(self):
        first, second = self.root(), self.root()
        self.assertEqual(self.get(first), {"text": "one"})
        self.assertEqual(self.get(second), {"text": "one"})
        self.assertEqual(self.builds, [first])
        with open(os.path.join(second, "work", "a.txt")) as f:
            self.assertEqual(f.read(), "one")
        if os.name != "nt":
            self.assertEqual(os.stat(os.path.join(second, "work", "sub", "b.txt")).st_mode & 0o777, 0o444)
            self.assertEqual(os.stat(os.path.join(second, "work", "run.sh")).st_mode & 0o777, 0o755)
        self.assertEqual(os.stat(os.path.join(second, "work", "a.txt")).st_mtime_ns, TIME)   # portable: set by the test above

    def test_the_copies_are_their_own_a_change_in_one_is_not_in_the_cache(self):
        first, second, third = self.root(), self.root(), self.root()
        self.get(first)
        self.get(second)
        with open(os.path.join(second, "work", "a.txt"), "w") as f:
            f.write("changed")
        self.get(third)
        with open(os.path.join(third, "work", "a.txt")) as f:
            self.assertEqual(f.read(), "one")

    def test_a_different_input_is_a_different_stand(self):
        first, second = self.root(), self.root()
        self.get(first, "one")
        self.get(second, "two")
        self.assertEqual(self.builds, [first, second])
        with open(os.path.join(second, "work", "a.txt")) as f:
            self.assertEqual(f.read(), "two")

    def test_the_address_of_a_remote_in_a_clone_follows_the_folder(self):
        first, second = self.root(), self.root()
        self.get(first)
        self.get(second)
        self.assertEqual(os.path.realpath(git(os.path.join(second, "copy"), "remote", "get-url", "origin")),
                         os.path.realpath(os.path.join(second, "up.git")))
        # and it works: a push from the copy lands in the bare repo of its own folder, not of the first one's
        with open(os.path.join(second, "copy", "x"), "w") as f:
            f.write("x")
        git(os.path.join(second, "copy"), "add", "-A")
        git(os.path.join(second, "copy"), "commit", "-qm", "x")
        git(os.path.join(second, "copy"), "push", "-q", "origin", "HEAD:main")
        self.assertEqual(git(os.path.join(second, "up.git"), "rev-parse", "main"), git(os.path.join(second, "copy"), "rev-parse", "HEAD"))
        self.assertEqual(subprocess.run(["git", "-C", os.path.join(first, "up.git"), "rev-parse", "-q", "--verify", "main"],
                                        capture_output=True).returncode, 1)

    def test_the_forms_of_a_windows_path_in_a_config_are_all_rewritten(self):
        old, new = "C:\\Users\\a\\tmp\\root-1", "C:\\Users\\a\\tmp\\root-2"
        self.assertEqual([a.replace(old, new) for a in stands._forms("C:\\x")], ["C:\\x", "C:\\\\x", "C:/x"])
        root = self.root()
        os.makedirs(os.path.join(root, "r", ".git"))
        path = os.path.join(root, "r", ".git", "config")
        with open(path, "w") as f:
            f.write(f'[remote "origin"]\n\turl = {old.replace(chr(92), chr(92) * 2)}\\\\up.git\n\tpushurl = {old.replace(chr(92), "/")}/up.git\n')
        stands._relocate(root, ["r"], old, new)
        with open(path) as f:
            text = f.read()
        self.assertIn(new.replace("\\", "\\\\") + "\\\\up.git", text)
        self.assertIn(new.replace("\\", "/") + "/up.git", text)
        self.assertNotIn("root-1", text)

    def test_a_damaged_entry_is_built_again_not_given_out(self):
        first, second = self.root(), self.root()
        self.get(first)
        entry, = self.entries()
        with open(os.path.join(entry, stands.TREE, "work", "a.txt"), "w") as f:
            f.write("tampered")
        self.assertEqual(self.get(second), {"text": "one"})
        self.assertEqual(self.builds, [first, second])
        with open(os.path.join(second, "work", "a.txt")) as f:
            self.assertEqual(f.read(), "one")
        third = self.root()
        self.get(third)
        self.assertEqual(self.builds, [first, second])   # the entry built again is whole
        with open(os.path.join(third, "work", "a.txt")) as f:
            self.assertEqual(f.read(), "one")

    def test_an_entry_of_another_input_under_this_key_is_not_given_out(self):
        first, second, third = self.root(), self.root(), self.root()
        self.get(first, "one")
        self.get(second, "two")
        by_text = {self.text_of(e): e for e in self.entries()}
        one, two = by_text["one"], by_text["two"]
        # the entry of "two" put under the key of "one": its meta says another key
        platforms.current.rmtree(one)
        os.rename(two, one)
        self.assertEqual(self.get(third, "one"), {"text": "one"})
        self.assertEqual(self.builds, [first, second, third])
        with open(os.path.join(third, "work", "a.txt")) as f:
            self.assertEqual(f.read(), "one")

    def test_a_cut_short_entry_is_built_again(self):
        first, second = self.root(), self.root()
        self.get(first)
        entry, = self.entries()
        os.unlink(os.path.join(entry, stands.TREE, "work", "sub", "b.txt"))
        self.assertEqual(self.get(second), {"text": "one"})
        self.assertEqual(self.builds, [first, second])
        self.assertTrue(os.path.isfile(os.path.join(second, "work", "sub", "b.txt")))

    def test_without_the_folder_of_the_run_every_stand_is_built(self):
        first, second = self.root(), self.root()
        with mock.patch.dict(os.environ, {stands.ENV: os.path.join(self.tmp, "none")}):
            self.get(first)
            self.get(second)
        self.assertEqual(self.builds, [first, second])
        self.assertEqual(self.entries(), [])

    def test_processes_asking_at_once_build_it_once(self):
        roots = [self.root() for _ in range(6)]
        threads = [threading.Thread(target=self.get, args=(r,)) for r in roots]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(len(self.builds), 1)
        for r in roots:
            self.assertTrue(os.path.isfile(os.path.join(r, "work", "a.txt")), r)

    def test_a_copy_that_cannot_be_made_leaves_the_folders_as_they_were_and_the_entry_alone(self):
        first, second, third = self.root(), self.root(), self.root()
        self.get(first)
        os.makedirs(os.path.join(second, "work"))
        with open(os.path.join(second, "work", "mine.txt"), "w") as f:
            f.write("an earlier step")
        real = stands._copy

        def refuses(src, dst, names, modes=None):
            if modes is not None:
                raise OSError("no room")
            return real(src, dst, names, modes)
        with mock.patch.object(stands, "_copy", refuses):
            self.assertEqual(self.get(second), {"text": "one"})        # built here, in the folder it has
        self.assertEqual(self.builds, [first, second])
        self.assertEqual(len(self.entries()), 1)                      # the entry was not taken for damaged
        self.get(third)
        self.assertEqual(self.builds, [first, second])                # and the next test takes it
        self.assertEqual([n for n in os.listdir(third) if n.startswith(".stand-")], [])

    def test_a_cache_that_cannot_be_written_does_not_fail_the_test_and_is_not_tried_again(self):
        first, second = self.root(), self.root()
        with mock.patch.object(stands, "_publish", return_value=False):
            self.assertEqual(self.get(first), {"text": "one"})
        self.assertEqual(self.entries(), [])
        self.assertEqual([n for n in os.listdir(self.base) if ".part-" in n], [])   # nothing half-written left
        self.assertEqual(len([n for n in os.listdir(self.base) if n.endswith(".nocache")]), 1)
        stored = []
        with mock.patch.object(stands, "_store", lambda *a: stored.append(a)):
            self.assertEqual(self.get(second), {"text": "one"})
        self.assertEqual((self.builds, stored), ([first, second], []))

    def test_a_disk_that_fills_while_the_entry_is_written_does_not_fail_the_test(self):
        root = self.root()
        real = stands._copy

        def full(src, dst, names, modes=None):
            if modes is None:
                raise OSError(28, "No space left on device")
            return real(src, dst, names, modes)
        with mock.patch.object(stands, "_copy", full):
            self.assertEqual(self.get(root), {"text": "one"})
        self.assertEqual(self.entries(), [])
        self.assertEqual([n for n in os.listdir(self.base) if ".part-" in n], [])

    def test_a_lock_that_cannot_be_taken_means_a_build_not_an_error(self):
        root = self.root()
        with mock.patch.object(stands.lock, "exclusive", side_effect=PermissionError("no")):
            self.assertEqual(self.get(root), {"text": "one"})
        self.assertEqual(self.builds, [root])

    def test_a_copy_that_would_keep_the_old_folders_address_is_not_given_out(self):
        # a path git wrote in another spelling than the temp folder gave (the case of a drive letter, 8.3 names)
        first, second = self.root(), self.root()

        def build_in(root):
            def build():
                self.builds.append(root)
                os.makedirs(os.path.join(root, "repo"))
                with open(os.path.join(root, "repo", "config"), "w") as f:
                    f.write("url = " + root.upper() + "/up.git\n")
            return build
        for root in (first, second):
            stands.cached(["odd"], root, ["repo"], build_in(root))
        self.assertEqual(self.builds, [first, second])

    def test_the_data_of_a_stand_is_the_same_for_the_test_that_built_it_and_the_others(self):
        first, second = self.root(), self.root()
        got = [stands.cached(["tuple"], root, ["work"], lambda root=root: (os.makedirs(os.path.join(root, "work")),
                                                                         {"pair": (1, 2)})[1]) for root in (first, second)]
        self.assertEqual(got, [{"pair": [1, 2]}, {"pair": [1, 2]}])

    def test_a_name_the_build_did_not_make_is_an_error_not_a_quiet_miss(self):
        with self.assertRaises(FileNotFoundError):
            stands.cached(["none"], self.root(), ["nothing"], lambda: {})

    def test_a_step_is_keyed_by_the_steps_before_it(self):
        def run(extra):
            root = self.root()
            chain = stands.Chain(root, "s")
            chain.step("first", ["work"], self.build_one(root))
            chain.step(extra, ["work"], self.build_two(root, extra))
            return root, chain
        a, _ = run("two")
        b, _ = run("two")
        c, _ = run("other")
        self.assertEqual(self.build_calls, [("first", a), ("two", a), ("other", c)])
        for root, want in ((a, "two"), (b, "two"), (c, "other")):
            with open(os.path.join(root, "work", "step")) as f:
                self.assertEqual(f.read(), want)

    def test_a_chain_that_is_off_builds_and_gives_nothing_to_the_cache(self):
        root = self.root()
        chain = stands.Chain(root, "s")
        chain.step("first", ["work"], self.build_one(root))
        chain.off()
        chain.step("two", ["work"], self.build_two(root, "two"))
        other = self.root()
        chain = stands.Chain(other, "s")
        chain.step("first", ["work"], self.build_one(other))
        chain.step("two", ["work"], self.build_two(other, "two"))
        self.assertEqual([n for n, _ in self.build_calls], ["first", "two", "two"])   # the second was built, not taken

    def test_the_same_step_after_a_different_one_is_not_the_same_stand(self):
        def run(first):
            root = self.root()
            chain = stands.Chain(root, "s")
            chain.step(first, ["work"], self.build_one(root, first))
            chain.step("tail", ["work"], self.build_two(root, "tail"))
            return root
        run("a")
        b = run("b")
        self.assertEqual([n for n, _ in self.build_calls], ["a", "tail", "b", "tail"])
        with open(os.path.join(b, "work", "step")) as f:
            self.assertEqual(f.read(), "tail")

    def test_an_entry_that_holds_other_folders_than_asked_is_not_given_out(self):
        first, second = self.root(), self.root()
        self.get(first)
        entry, = self.entries()
        path = os.path.join(entry, stands.META)
        with open(path, encoding="utf-8") as f:
            meta = json.load(f)
        meta["names"] = ["work"]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(meta, f)
        self.assertEqual(self.get(second), {"text": "one"})
        self.assertEqual(self.builds, [first, second])
        self.assertTrue(os.path.isdir(os.path.join(second, "up.git")))

    def build_one(self, root, name="first"):
        def build():
            self.build_calls.append((name, root))
            os.makedirs(os.path.join(root, "work"))
            with open(os.path.join(root, "work", "step"), "w") as f:
                f.write(name)
        return build

    def build_two(self, root, text):
        def build():
            self.build_calls.append((text, root))
            with open(os.path.join(root, "work", "step"), "w") as f:
                f.write(text)
        return build


if __name__ == "__main__":
    unittest.main()
