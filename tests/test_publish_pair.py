"""tools/publish_pair.py and the release of a pair (spec d2597953 §2.2, §2.4): one local commit published as two
snapshots — the open core into its repository, the embed into its own closed one — and one release number over both.
Bare repositories in a temp folder stand for both; nothing reaches GitHub. The checks of the trees have their own tests
(test_oss_open, test_publish): here they are stood in for, and only their place in the order is held."""
import tests  # noqa: F401 — hide the machine's settings
import importlib.util
import json
import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name):
    spec = importlib.util.spec_from_file_location(f"hub_test_{name}", os.path.join(ROOT, "tools", f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write(root, rel, text):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def git(*args, cwd=None):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.test", *args], cwd=cwd, check=True,
                          capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout.strip()


TODAY = time.strftime("%Y-%m-%d")
AUTHOR = "Created by Alice Smith ([@alice](https://github.com/alice))"


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="pair-")
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.repo = os.path.join(self.root, "repo")
        self.core = os.path.join(self.root, "core.git")
        self.embed = os.path.join(self.root, "embed.git")
        self.state = os.path.join(self.root, "state")
        os.makedirs(self.state)
        for bare in (self.core, self.embed):
            git("init", "-q", "--bare", "-b", "main", bare)
        git("init", "-q", "-b", "main", self.repo)
        write(self.repo, "README.md", f"# Hub\n\n{AUTHOR}\n")
        write(self.repo, "CHANGES.md", f"# What's new\n\n## {TODAY}\n\n- one\n")
        write(self.repo, "core/version.py", 'CLAUDE_TESTED = "1.0.0"\n')
        write(self.repo, "embed/acme/embed.json", '{"embed": 1, "api": "1.0"}\n')
        write(self.repo, "embed/acme/modules/pool/module.json", '{"name": "pool"}\n')
        write(self.repo, ".open-deny", "reports.example.test\n")
        write(self.repo, ".gitignore", ".open-deny\n")
        self.commit()
        env = mock.patch.dict(os.environ, {"PUBLISH_NAME": "pub", "PUBLISH_EMAIL": "pub@example.test",
                                           "HUB_STATE_DIR": self.state})
        env.start()
        self.addCleanup(env.stop)

    def commit(self, msg="x"):
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "--allow-empty", "-m", msg, cwd=self.repo)
        return git("rev-parse", "HEAD", cwd=self.repo)

    def log(self, bare):
        r = subprocess.run(["git", "--git-dir", bare, "rev-list", "main"], capture_output=True, text=True)
        return r.stdout.split() if r.returncode == 0 else []

    def files(self, bare):
        return git("--git-dir", bare, "ls-tree", "-r", "--name-only", "main").split()

    def published(self):
        with open(os.path.join(self.state, "published.json"), encoding="utf-8") as f:
            return json.load(f)


class Pair(Base):
    def setUp(self):
        super().setUp()
        self.mod = load("publish_pair")
        self.claude = "1.0.0"   # what the stable channel names: the stand-in pin writes it
        self.pushed = []
        self.checked = {"open": [], "embed": [], "translations": [], "suite": []}

        def pin(tree):
            path = os.path.join(tree, "core", "version.py")
            with open(path, encoding="utf-8") as f:
                text = f.read()
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text.replace(text.split('"')[1], self.claude, 1))
        real_put = self.mod.put

        def put(side, message):
            self.pushed.append(side.url)
            return real_put(side, message)
        for name, fn in (("pin", pin), ("put", put),
                         ("check_open", lambda tree, forbid: self.checked["open"]),
                         ("check_embed", lambda tree: self.checked["embed"]),
                         ("translations", lambda repo: self.checked["translations"]),
                         ("suite", lambda tree: self.checked["suite"])):
            p = mock.patch.object(self.mod, name, fn)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(self.mod.contributors, "gh_id", lambda login: None)
        p.start()
        self.addCleanup(p.stop)

    def publish(self, *flags, core=None, embed=None):
        return self.mod.main([core or self.core, "--embed", embed or self.embed, "--repo", self.repo, *flags])

    def test_one_command_puts_the_embed_and_then_the_core_and_records_both(self):
        self.assertEqual(self.publish(), 0)
        self.assertEqual(self.pushed, [self.embed, self.core])   # what nobody reads goes first
        self.assertEqual(sorted(self.files(self.embed)), ["embed.json", "modules/pool/module.json"])   # the embed's root
        core = self.files(self.core)
        self.assertIn("README.md", core)
        self.assertIn("CONTRIBUTORS.md", core)
        self.assertFalse([f for f in core if f.startswith("embed/")], core)   # the open snapshot carries no embed
        self.assertNotIn(".open-deny", core)
        got = self.published()
        self.assertEqual(got["sha"], git("rev-parse", "HEAD", cwd=self.repo))
        self.assertEqual(got["url"], self.core)
        self.assertEqual(got["pub"], self.log(self.core)[0])
        self.assertEqual(got["embed_url"], self.embed)
        self.assertEqual(got["embed_pub"], self.log(self.embed)[0])
        for bare in (self.core, self.embed):   # a snapshot: "Sync <day>" by the publisher, no local message
            self.assertEqual(git("--git-dir", bare, "log", "-1", "--format=%an <%ae>|%s", "main"),
                             f"pub <pub@example.test>|Sync {TODAY}")
        self.assertIn("- one", git("--git-dir", self.core, "log", "-1", "--format=%B", "main"))

    def test_a_tree_that_did_not_change_gets_no_commit(self):
        self.publish()
        core, embed = self.log(self.core), self.log(self.embed)
        self.commit("nothing changed")
        self.assertEqual(self.publish(), 0)   # a run again: neither repository moves
        self.assertEqual((self.log(self.core), self.log(self.embed)), (core, embed))
        self.assertEqual(self.published()["sha"], git("rev-parse", "HEAD", cwd=self.repo))   # HEAD is what is out
        write(self.repo, "embed/acme/modules/pool/module.json", '{"name": "pool", "v": 2}\n')
        self.commit("the pool only")
        self.pushed.clear()
        self.assertEqual(self.publish(), 0)
        self.assertEqual(self.pushed, [self.embed])   # the embed's fix: no commit in the open repository
        self.assertEqual(self.log(self.core), core)
        self.assertEqual(self.log(self.embed)[1:], embed)
        self.assertEqual(self.published()["embed_pub"], self.log(self.embed)[0])
        write(self.repo, "core/x.py", "X = 1\n")
        self.commit("the core only")
        self.pushed.clear()
        self.assertEqual(self.publish(), 0)
        self.assertEqual(self.pushed, [self.core])
        self.assertEqual(self.log(self.core)[1:], core)
        self.assertEqual(self.published()["pub"], self.log(self.core)[0])

    def test_only_a_new_claude_code_is_no_snapshot(self):
        # Anthropic's stable moved, the code did not: the open hubs on main would restart onto an untested claude
        self.publish()
        core = self.log(self.core)
        self.claude = "1.0.1"
        self.commit("nothing")
        self.assertEqual(self.publish(), 0)
        self.assertEqual(self.log(self.core), core)
        write(self.repo, "core/x.py", "X = 1\n")   # with a change of the code the new one goes
        self.commit("code")
        self.publish()
        self.assertIn('CLAUDE_TESTED = "1.0.1"', git("--git-dir", self.core, "show", "main:core/version.py"))

    def test_a_check_red_on_either_tree_puts_nothing(self):
        for what in ("open", "embed", "translations", "suite"):
            self.checked[what] = [f"{what} is red"]
            self.assertEqual(self.publish(), 3, what)
            self.checked[what] = []
            self.assertEqual((self.log(self.core), self.log(self.embed)), ([], []), what)
            self.assertFalse(os.path.exists(os.path.join(self.state, "published.json")), what)

    def test_check_puts_nothing_and_records_nothing(self):
        self.assertEqual(self.publish("--check"), 0)
        self.assertEqual((self.log(self.core), self.log(self.embed), self.pushed), ([], [], []))
        self.assertFalse(os.path.exists(os.path.join(self.state, "published.json")))

    def test_the_embed_never_goes_to_an_open_repository(self):
        # the embed is the company's: an address of the open repositories (tools/publish_guard.py) in any form is refused
        for url in ("https://github.com/icons8/hub.git", "git@github.com:Icons8/Hub", "https://github.com/icons8/agents-hub-plugins"):
            self.assertEqual(self.publish(embed=url), 12, url)
        self.assertEqual(self.publish(embed=self.core), 2)   # one repository for both: the embed would go public with it
        self.assertEqual((self.log(self.core), self.log(self.embed), self.pushed), ([], [], []))

    def test_a_mix_up_of_the_addresses_lays_no_tree_over_another_repository(self):
        # the team's repository of the full tree (publish.sh's Sync commits by the same author: no foreign commit there)
        team = os.path.join(self.root, "team.git")
        git("init", "-q", "--bare", "-b", "main", team)
        git("push", "-q", team, "HEAD:refs/heads/main", cwd=self.repo)
        self.assertEqual(self.publish(embed=team), 2)   # its main holds no embed at the root
        self.assertEqual(self.publish(core=team), 2)    # its main holds embed/: not the open core's
        self.assertEqual((self.log(self.core), self.log(self.embed), self.pushed), ([], [], []))
        self.assertEqual(self.publish(), 0)
        self.assertEqual(self.publish(), 0)   # its own repositories pass: the embed's root, no embed/ in the core's

    def test_a_new_embed_repository_with_a_readme_takes_the_snapshot(self):
        # a closed repository made on GitHub with "Add a README": a main of its own, no core in it
        work = os.path.join(self.root, "w")
        git("init", "-q", "-b", "main", work)
        write(work, "README.md", "the embed\n")
        git("add", "-A", cwd=work)
        subprocess.run(["git", "-c", "user.name=pub", "-c", "user.email=pub@example.test", "commit", "-q", "-m",
                        "Sync 2026-01-01"], cwd=work, check=True)
        git("push", "-q", self.embed, "main", cwd=work)
        self.assertEqual(self.publish(), 0)
        self.assertEqual(len(self.log(self.embed)), 2)
        self.assertNotIn("README.md", self.files(self.embed))   # the snapshot is the embed's folder

    def test_the_co_authors_come_before_the_first_push(self):
        def broken(*a, **k):
            raise OSError("the state folder is read-only")
        with mock.patch.object(self.mod.contributors, "credit", broken):
            self.assertEqual(self.publish(), 3)
        self.assertEqual((self.log(self.core), self.log(self.embed), self.pushed), ([], [], []))

    def test_check_goes_to_the_end_over_foreign_commits(self):
        self.publish()
        work = os.path.join(self.root, "w")
        git("clone", "-q", self.core, work)
        write(work, "x.txt", "a person's commit\n")
        git("add", "-A", cwd=work)
        git("commit", "-q", "-m", "merged on the remote", cwd=work)
        git("push", "-q", "origin", "main", cwd=work)
        write(self.repo, "core/x.py", "X = 1\n")
        self.commit()
        self.checked["open"] = ["open is red"]
        self.assertEqual(self.publish("--check"), 3)   # the red check is said, not hidden behind the foreign commit
        self.checked["open"] = []
        self.assertEqual(self.publish("--check"), 6)   # and the foreign commit last

    def test_a_foreign_commit_in_either_repository_stops_both(self):
        self.publish()
        work = os.path.join(self.root, "w")
        git("clone", "-q", self.embed, work)
        write(work, "README.md", "a person's commit\n")
        git("add", "-A", cwd=work)
        git("commit", "-q", "-m", "merged on the remote", cwd=work)
        git("push", "-q", "origin", "main", cwd=work)
        core, embed = self.log(self.core), self.log(self.embed)
        write(self.repo, "core/x.py", "X = 1\n")
        write(self.repo, "embed/acme/modules/pool/module.json", '{"name": "pool", "v": 2}\n')
        self.commit()
        self.assertEqual(self.publish(), 6)   # the snapshot would roll it back; the core waits too: all or nothing
        self.assertEqual((self.log(self.core), self.log(self.embed)), (core, embed))
        self.assertEqual(self.publish("--overwrite-remote"), 0)
        self.assertEqual(self.log(self.embed)[1:], embed)
        self.assertEqual(self.log(self.core)[1:], core)

    def test_a_snapshot_of_the_opening_is_a_snapshot_not_a_foreign_commit(self):
        # the open repository starts with tools/oss_open.py's commit ("Hub by Icons8, snapshot of …"): ours as well
        work = os.path.join(self.root, "w")
        git("init", "-q", "-b", "main", work)
        write(work, "README.md", "the opening\n")
        git("add", "-A", cwd=work)
        subprocess.run(["git", "-c", "user.name=pub", "-c", "user.email=pub@example.test", "commit", "-q", "-m",
                        "Hub by Icons8, snapshot of " + "c" * 12], cwd=work, check=True)
        git("push", "-q", self.core, "main", cwd=work)
        self.assertEqual(self.publish(), 0)
        self.assertEqual(len(self.log(self.core)), 2)

    def test_new_entries_go_under_today(self):
        self.publish()
        core = self.log(self.core)
        write(self.repo, "CHANGES.md", f"# What's new\n\n## 2099-01-01\n\n- under another day\n\n## {TODAY}\n\n- one\n")
        self.commit()
        self.assertEqual(self.publish(), 4)
        self.assertEqual(self.log(self.core), core)

    def test_the_embed_folder_is_the_one_under_embed(self):
        write(self.repo, "embed/other/embed.json", "{}\n")
        self.commit()
        self.assertEqual(self.publish(), 2)   # two embeds: which one? --embed-name says
        self.assertEqual(self.publish("--embed-name", "acme"), 0)
        self.assertNotIn("other", " ".join(self.files(self.embed)))


class Release(Base):
    """tools/release.py over the pair: the number is newer than the last release of both repositories, the tag goes
    into the embed's first, and an embed's fix alone is a release of the embed alone."""

    def setUp(self):
        super().setUp()
        self.pair = load("publish_pair")
        for name, fn in (("pin", lambda tree: None), ("check_open", lambda tree, forbid: []),
                         ("check_embed", lambda tree: []), ("translations", lambda repo: []), ("suite", lambda tree: [])):
            p = mock.patch.object(self.pair, name, fn)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(self.pair.contributors, "gh_id", lambda login: None)
        p.start()
        self.addCleanup(p.stop)
        self.mod = load("release")
        self.pushes = []
        real = self.mod.git

        def logged(repo, *args, **kw):
            if args[:1] == ("push",):
                self.pushes.append((os.path.basename(repo), args[-1]))
            return real(repo, *args, **kw)
        for name, value in (("ROOT", self.repo), ("state_dir", lambda: self.state), ("git", logged),
                            ("stands", lambda *a, **k: None), ("private", lambda *a, **k: None)):
            p = mock.patch.object(self.mod, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.publish()

    def publish(self):
        self.assertEqual(self.pair.main([self.core, "--embed", self.embed, "--repo", self.repo]), 0)

    def tag(self, bare, name, ref="main"):
        git("--git-dir", bare, "tag", "-a", "-m", name, name, ref)

    def tags(self, bare):
        return git("--git-dir", bare, "tag", "--list", "--points-at", "main").split()

    def release(self, tag=None):
        return self.mod.release(self.core, tag, name="pub", mail="pub@example.test")

    def test_the_number_is_newer_than_both_repositories_and_the_embed_is_tagged_first(self):
        self.tag(self.core, "v1.2.0")
        self.tag(self.embed, "v1.2.0")
        self.tag(self.embed, "v1.2.1")   # a release of the embed alone, the same day
        write(self.repo, "core/x.py", "X = 1\n")
        self.commit()
        self.publish()
        with self.assertRaises(self.mod.Refused) as e:
            self.release("v1.2.1")   # the embed has it: the team's build of it would never be offered
        self.assertIn("v1.2.1", str(e.exception))
        self.assertEqual(self.release(), "v1.2.2")   # not v1.2.1: newer than the embed's too
        self.assertEqual(self.tags(self.core), ["v1.2.2"])
        self.assertIn("v1.2.2", self.tags(self.embed))
        self.assertEqual([ref for _, ref in self.pushes], ["refs/tags/v1.2.2", "refs/tags/v1.2.2"])
        with open(os.path.join(self.state, "published-stable.json"), encoding="utf-8") as f:
            got = json.load(f)
        self.assertEqual((got["tag"], got["pub"], got["embed_pub"], got["embed_url"]),
                         ("v1.2.2", self.log(self.core)[0], self.log(self.embed)[0], self.embed))

    def test_the_embed_goes_first(self):
        order = []
        real = self.mod.git

        def git_(repo, *args, **kw):
            if args[:1] == ("push",):
                remote = real(repo, "config", "remote.origin.url")
                order.append(remote)
            return real(repo, *args, **kw)
        with mock.patch.object(self.mod, "git", git_):
            self.release()
        self.assertEqual(order, [self.embed, self.core])

    def test_a_release_stopped_after_the_embeds_tag_goes_on_with_the_cores(self):
        real = self.mod.git

        def git_(repo, *args, **kw):
            if args[:1] == ("push",) and real(repo, "config", "remote.origin.url") == self.core:
                raise self.mod.Refused("the network is gone", 3)
            return real(repo, *args, **kw)
        with mock.patch.object(self.mod, "git", git_), self.assertRaises(self.mod.Refused):
            self.release()
        self.assertEqual((self.tags(self.embed), self.tags(self.core)), (["v1.0.0"], []))
        with self.assertRaises(self.mod.Refused) as e:   # another number would leave the embed's v1.0.0 alone
            self.release("v1.0.1")
        self.assertIn("v1.0.0", str(e.exception))
        self.assertEqual(self.release(), "v1.0.0")   # the same number, the core's tag only
        self.assertEqual((self.tags(self.embed), self.tags(self.core)), (["v1.0.0"], ["v1.0.0"]))
        self.assertFalse(os.path.exists(os.path.join(self.state, "release-pending.json")))
        with self.assertRaises(self.mod.Refused):   # done: nothing to go on with
            self.release()

    def test_a_release_that_pushed_nothing_leaves_nothing_to_go_on_with(self):
        real = self.mod.git

        def git_(repo, *args, **kw):
            if args[:1] == ("push",):
                raise self.mod.Refused("the network is gone", 3)
            return real(repo, *args, **kw)
        with mock.patch.object(self.mod, "git", git_), self.assertRaises(self.mod.Refused):
            self.release()
        self.assertFalse(os.path.exists(os.path.join(self.state, "release-pending.json")))
        self.assertEqual(self.release("v1.1.0"), "v1.1.0")   # any number: nothing half done
        self.assertEqual((self.tags(self.embed), self.tags(self.core)), (["v1.1.0"], ["v1.1.0"]))

    def test_a_fix_of_the_embed_alone_is_a_release_of_the_embed(self):
        self.assertEqual(self.release(), "v1.0.0")
        core = self.log(self.core)
        write(self.repo, "embed/acme/modules/pool/module.json", '{"name": "pool", "v": 2}\n')
        self.commit()
        self.publish()
        self.assertEqual(self.log(self.core), core)   # the open repository did not move
        self.assertEqual(self.release(), "v1.0.1")
        self.assertEqual(self.tags(self.core), ["v1.0.0"])   # the open core keeps its release
        self.assertEqual(self.tags(self.embed), ["v1.0.1"])
        with self.assertRaises(self.mod.Refused):   # nothing new in either: no release
            self.release()

    def test_a_new_core_on_an_embed_already_released_tags_both(self):
        self.release()
        write(self.repo, "core/x.py", "X = 1\n")
        self.commit()
        self.publish()
        self.assertEqual(self.release(), "v1.0.1")
        self.assertEqual(self.tags(self.core), ["v1.0.1"])
        self.assertEqual(sorted(self.tags(self.embed)), ["v1.0.0", "v1.0.1"])   # the team's build reads the embed at it

    def test_a_published_record_of_the_core_alone_releases_as_before(self):
        with open(os.path.join(self.state, "published.json"), encoding="utf-8") as f:
            got = json.load(f)
        for key in ("embed_url", "embed_pub"):
            got.pop(key)
        with open(os.path.join(self.state, "published.json"), "w", encoding="utf-8") as f:
            json.dump(got, f)
        self.assertEqual(self.release(), "v1.0.0")
        self.assertEqual(self.tags(self.embed), [])
        self.assertEqual(self.tags(self.core), ["v1.0.0"])


if __name__ == "__main__":
    unittest.main()
