"""tools/publish.sh is closed by default: no deny patterns, or a failed check - nothing is published; the snapshot is
checked whole (binary files, links, common forms), the hub state (published.json) is tied to one
address. Only local bare repositories - nothing is sent anywhere else."""
import tests  # noqa: F401 - ignore the machine's settings
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests import winsafe

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(cwd, *args, env=None, check=True):
    return subprocess.run(list(args), cwd=cwd, env=env, check=check, capture_output=True, text=True)


@unittest.skipIf(os.name == "nt" or not shutil.which("sh"), "publish.sh — sh")
class Publish(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="publish-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.src = os.path.join(self.root, "src")
        os.makedirs(os.path.join(self.src, "tools"))
        for name in ("publish.sh", "publish_allow.py", "publish-allow.txt", "publish-deny.txt", "publish_checks.py",
                     "publish_state.py", "release.py", "publish_remote.py", "private_check.py", "contributors.py",
                     "publish_guard.py"):
            shutil.copy(os.path.join(ROOT, "tools", name), os.path.join(self.src, "tools", name))
        # private_check reads the runner's earlier line forms from the language data of the repository it belongs to
        shutil.copytree(os.path.join(ROOT, "i18n", "data"), os.path.join(self.src, "i18n", "data"))
        self.write("tools/public_language.py", "raise SystemExit(0)\n")   # the real one: test_the_language_of_the_tree_is_held
        self.write("tools/oss_open.py", 'OPEN_TARGETS = ("icons8/hub", "Acme/Open-Catalog")\n')   # the real list: OpenTargets
        self.write("tools/i18n.py", "raise SystemExit(0)\n")   # the translations gate: test_a_release_carries_every_language
        self.write("CHANGES.md", f"## {datetime.date.today():%Y-%m-%d}\n\n- First.\n")
        self.write("app.py", "a = 1\n")
        self.write(".publish-deny", "forbidden-name\n")
        self.write(".gitignore", ".publish-deny\n.publish-deny-case\n__pycache__/\n")
        self.git(self.src, "init", "-q", "-b", "main")
        self.commit("start")
        self.bare = os.path.join(self.root, "pub.git")
        self.git(self.root, "init", "-q", "--bare", "-b", "main", self.bare)
        self.state = os.path.join(self.root, "state")
        os.makedirs(self.state)
        # this machine's gitleaks is used only in tests about it (via a substitute); here it is not in PATH
        self.env = {**os.environ, "PUBLISH_NAME": "Hub", "PUBLISH_EMAIL": "hub@example.invalid",
                    "HUB_STATE_DIR": self.state, "HUB_CONFIG_DIR": os.path.join(self.root, "config"),
                    "PATH": os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep)
                                            if not os.path.exists(os.path.join(p, "gitleaks")))}
        self.env.pop("GIT_DIR", None)

    def git(self, cwd, *args, check=True):
        return run(cwd, "git", "-c", "user.name=t", "-c", "user.email=t@t", *args, check=check).stdout.strip()

    def write(self, name, text):
        path = os.path.join(self.src, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def commit(self, msg="change"):
        self.git(self.src, "add", "-A")
        self.git(self.src, "commit", "-qm", msg)

    def publish(self, *args, repo=None, url=None):
        repo = repo or self.src
        return run(repo, "sh", os.path.join(repo, "tools", "publish.sh"), url or self.bare, "--nightly", *args,
                   env=self.env, check=False)

    def refs(self, bare=None):
        return self.git(bare or self.bare, "for-each-ref", "--format=%(refname)", check=False).split()

    def test_without_deny_patterns_nothing_is_published(self):
        os.unlink(os.path.join(self.src, ".publish-deny"))
        r = self.publish("--check")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn(".publish-deny", r.stderr)
        self.write(".publish-deny", "")       # empty is the same as absent
        self.assertEqual(self.publish().returncode, 3)
        self.assertEqual(self.refs(), [])

    def test_an_open_repository_is_refused_in_every_form_of_its_address(self):
        # the full tree goes only to the team's own repository; the open ones are filled by tools/oss_open.py. Nothing
        # may leave (no network protocol is allowed here), the refusal comes before any work
        env = {**self.env, "GIT_ALLOW_PROTOCOL": "file"}
        for url in ("https://github.com/icons8/hub", "https://github.com/icons8/hub.git", "https://GitHub.com/Icons8/HUB.git",
                    "https://github.com/icons8/hub/", "https://user:tok@github.com/icons8/hub.git",
                    "git@github.com:icons8/hub.git", "git@github.com:Icons8/Hub", "ssh://git@github.com/icons8/hub.git",
                    "ssh://git@ssh.github.com:443/icons8/hub.git", "git://github.com/icons8/hub.git",
                    "https://github.com/acme/open-catalog.git"):
            for args in (("--check",), ("--tag", "v1.2.3"), ("--release",)):
                with self.subTest(url=url, args=args):
                    r = run(self.src, "sh", os.path.join(self.src, "tools", "publish.sh"), url, *args, env=env, check=False)
                    self.assertEqual(r.returncode, 12, r.stderr)
                    self.assertIn("oss_open.py", r.stderr)
                    self.assertIn("open repository", r.stderr)
        self.assertEqual(self.refs(), [])

    def test_other_addresses_are_not_taken_for_an_open_one(self):
        # a lookalike, a folder with the same ending (a rehearsal), the team's own repository - the check passes them
        import importlib.util
        spec = importlib.util.spec_from_file_location("publish_guard", os.path.join(ROOT, "tools", "publish_guard.py"))
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        for url in ("https://github.com/icons8/hub2.git", "https://github.com/icons8/hub-old", "https://github.com/xicons8/hub",
                    "https://github.com/acme/team-core.git", "git@github.com:acme/hub.git", "hub", "",
                    "/srv/icons8/hub.git", "../icons8/hub", "file:///srv/icons8/hub.git", "C:\\git\\icons8\\hub.git"):
            with self.subTest(url=url):
                self.assertNotEqual(guard.slug_of(url), "icons8/hub")
        self.assertEqual(self.publish("--check").returncode, 0)   # the rehearsal bare repository of every test here

    def test_a_broken_pattern_is_an_error_not_clean(self):
        self.write(".publish-deny", "forbidden-name\n(\n")
        r = self.publish()
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertEqual(self.refs(), [])

    def test_a_release_carries_every_language(self):
        # a ready language without a phrase (tools/i18n.py check --release) stops the snapshot with its own code: the new
        # phrases of a release reach every language, not English until someone remembers to translate
        self.write("tools/i18n.py", "import sys\nprint('de: web.x: missing', file=sys.stderr)\n"
                                    "raise SystemExit(1 if sys.argv[1:] == ['check', '--release'] else 0)\n")
        self.commit()
        for args in (("--check",), ("--nightly",), ()):
            r = self.publish(*args)
            self.assertEqual(r.returncode, 10, r.stderr)
            self.assertIn("tools/translate.py", r.stderr)
        self.assertEqual(self.refs(), [])

    def test_the_language_of_the_tree_is_held_by_its_own_step(self):
        # the ratchet of Cyrillic lines (tools/public_language.py) is run by publish.sh itself: a nightly without the full run
        # still cannot carry new Russian; its own exit code, nothing is pushed
        from tools import public_language as pl
        shutil.copy(os.path.join(ROOT, "tools", "public_language.py"), os.path.join(self.src, "tools", "public_language.py"))
        self.write("tools/public_language_baseline.json", "{}\n")
        self.git(self.src, "add", "-A")
        pl.dump(pl.count(self.src), os.path.join(self.src, "tools", "public_language_baseline.json"))   # what the copy holds now
        self.commit()
        self.assertEqual(self.publish("--check").returncode, 0)
        self.write("app.py", "a = 1\n# " + "".join(chr(c) for c in range(0x043f, 0x0445)) + "\n")
        self.commit()
        for args in (("--check",), ()):
            r = self.publish(*args)
            self.assertEqual(r.returncode, 9, r.stderr)
            self.assertIn("app.py: 1 lines with Cyrillic", r.stderr)
        self.assertEqual(self.refs(), [])

    def test_a_denied_word_stops_it(self):
        self.write("notes.md", "thanks Forbidden-Name\n")
        self.commit()
        r = self.publish()
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("notes.md:1:", r.stderr)
        self.assertEqual(self.refs(), [])

    def test_a_failing_filter_is_an_error_not_clean(self):
        # grep's output used to go into a filter through a pipe without pipefail: a failed filter meant "clean"
        self.write("notes.md", "thanks forbidden-name\n")
        self.write("tools/publish_allow.py", "raise SystemExit(2)\n")
        self.commit()
        r = self.publish()
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertEqual(self.refs(), [])

    def test_a_worktree_uses_the_patterns_of_the_main_checkout(self):
        # the patterns are in .gitignore: a worktree has none of its own, and the check passed "silently clean"
        self.write("notes.md", "thanks forbidden-name\n")
        self.commit()
        wt = os.path.join(self.root, "wt")
        self.git(self.src, "worktree", "add", "-q", "--detach", wt)
        self.assertFalse(os.path.exists(os.path.join(wt, ".publish-deny")))
        r = self.publish(repo=wt)
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("notes.md", r.stderr)
        self.assertEqual(self.refs(), [])

    def test_a_worktree_adds_its_own_patterns_to_the_main_ones(self):
        self.write("notes.md", "a word that only this copy forbids: edition-only\n")
        self.commit()
        wt = os.path.join(self.root, "wt")
        self.git(self.src, "worktree", "add", "-q", "--detach", wt)
        with open(os.path.join(wt, ".publish-deny"), "w") as f:
            f.write("Edition-Only\n")
        r = self.publish("--check", repo=wt)
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("notes.md", r.stderr)
        self.assertEqual(self.publish("--check").returncode, 0)   # the main copy is not affected

    def test_without_the_shared_patterns_nothing_is_published(self):
        os.unlink(os.path.join(self.src, "tools", "publish-deny.txt"))
        self.commit()
        r = self.publish("--check")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("publish-deny.txt", r.stderr)
        self.assertEqual(self.refs(), [])

    def test_a_home_path_stops_it_whatever_the_operators_patterns_are(self):
        # tools/publish-deny.txt ships with the code: every hub checks the same forms (home paths, key-like strings)
        self.write("notes.md", "see /Users/" + "someone/notes\n")
        self.commit()
        r = self.publish()
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("notes.md", r.stderr)
        self.assertEqual(self.refs(), [])

    def test_an_anchored_pattern_counts(self):
        # grep found the line, but the filter matched the pattern against "path:line:text", where ^ never matches
        self.write(".publish-deny-case", "^Secret-Start\n")
        self.write("notes.md", "intro\nSecret-Start lines\n")
        self.commit()
        r = self.publish("--check")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("notes.md:2:", r.stderr)

    def test_patterns_saved_with_crlf_still_count(self):
        # a pattern file from Windows: a trailing \r must not make a pattern match nothing
        self.write(".publish-deny-case", "Secret-Start\r\n")
        self.write("notes.md", "Secret-Start lines\n")
        self.commit()
        r = self.publish("--check")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("notes.md:1:", r.stderr)

    def test_a_binary_file_is_checked_too(self):
        # grep reports it only as "binary file matches", and the filter dropped that
        with open(os.path.join(self.src, "blob.bin"), "wb") as f:
            f.write(b"\x00\x01\x02 forbidden-name \x00\xff\n")
        self.commit()
        r = self.publish("--check")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("blob.bin", r.stderr)

    def test_only_the_vendored_library_and_the_signed_app_are_skipped(self):
        # modules/web/vendor, not any vendor folder; the former plugin list (modules/market/trusted.json) is checked too
        self.write("modules/web/vendor/lib.js", "// forbidden-name\n")
        # a signed package: the signature carries the certificate holder's name
        self.write("deploy/mac/Agents Hub.app/Contents/_CodeSignature/CodeResources", "forbidden-name\n")
        self.commit()
        self.assertEqual(self.publish("--check").returncode, 0)
        for path in ("plugins/x/vendor/lib.js", "modules/market/trusted.json", "deploy/mac/Other.app/Contents/Info.plist",
                     "deploy/mac/Agents Hub.app/Contents/Info.plist"):
            with self.subTest(path=path):
                self.write(path, "forbidden-name\n")
                self.commit()
                r = self.publish("--check")
                self.assertEqual(r.returncode, 3, r.stderr)
                self.assertIn(path, r.stderr)
                os.unlink(os.path.join(self.src, path))
                self.commit()

    def stand_in(self, name, body):
        tools = os.path.join(self.root, "stand-ins")
        os.makedirs(tools, exist_ok=True)
        with open(os.path.join(tools, name), "w") as f:
            f.write("#!/bin/sh\n" + body)
        os.chmod(os.path.join(tools, name), 0o755)
        if not self.env["PATH"].startswith(tools + os.pathsep):
            self.env["PATH"] = tools + os.pathsep + self.env["PATH"]

    def test_the_deny_check_is_the_same_with_a_bsd_grep(self):
        # BSD grep (macOS) reads options differently from GNU: the snapshot check does not depend on grep at all
        real = shutil.which("grep")
        self.stand_in("grep", 'for a; do shift; [ "$a" = -Z ] || set -- "$@" "$a"; done\n'
                              f'exec "{real}" "$@"\n')
        self.write("modules/web/vendor/lib.js", "// forbidden-name\n")
        self.commit()
        self.assertEqual(self.publish("--check").returncode, 0)
        self.write(".publish-deny-case", "^Secret-Start\n")
        self.write("notes.md", "intro\nSecret-Start lines\n")
        self.commit()
        r = self.publish("--check")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("notes.md:2:", r.stderr)
        self.assertNotIn("lib.js", r.stderr)

    def test_a_symbolic_link_is_checked_by_its_target(self):
        # the snapshot publishes a link's target as is: a home path in it is published too
        winsafe.symlink(self, "/opt/forbidden-name/tool", os.path.join(self.src, "tool"))
        self.commit()
        r = self.publish("--check")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("tool:1:/opt/forbidden-name/tool", r.stderr)

    def test_an_unresolved_conflict_stops_it(self):
        self.write("CHANGES.md", f"## {datetime.date.today():%Y-%m-%d}\n\n<<<<<<< HEAD\n- First.\n=======\n"
                                 "- Second.\n>>>>>>> other\n")
        self.commit()
        r = self.publish("--check")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("merge conflict", r.stderr)

    def test_gitleaks_findings_stop_it(self):
        self.stand_in("gitleaks", 'echo "Finding: a token" >&2; exit 1\n')
        r = self.publish("--check")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("gitleaks", r.stderr)
        self.assertEqual(self.publish().returncode, 3)
        self.assertEqual(self.refs(), [])

    def test_gitleaks_reads_the_snapshot(self):
        seen = os.path.join(self.root, "seen")
        self.stand_in("gitleaks", f'pwd > "{seen}"; ls -a >> "{seen}"; exit 0\n')
        self.write("docs/spec.md", "internal\n")
        self.commit()
        self.assertEqual(self.publish("--check").returncode, 0)
        with open(seen) as f:
            where, *names = f.read().split("\n")
        self.assertNotEqual(os.path.realpath(where), os.path.realpath(self.src))
        self.assertIn("app.py", names)
        self.assertNotIn("docs", names)

    def test_a_gitleaks_without_dir_is_named_not_taken_for_a_leak(self):
        # gitleaks before 8.19 (apt on Ubuntu 24.04) does not know "dir": every release used to stop with "found keys"
        self.stand_in("gitleaks", 'echo "unknown command \\"$1\\"" >&2; exit 1\n')
        r = self.publish()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("gitleaks is older than 8.19", r.stderr)
        self.assertIn("refs/heads/main", self.refs())

    def test_a_broken_gitleaks_stops_it(self):
        # a failure, not an old version: the check is closed by default
        self.stand_in("gitleaks", 'echo "exec format error" >&2; exit 1\n')
        r = self.publish("--check")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertNotIn("older than", r.stderr)

    def test_without_gitleaks_it_says_so_and_goes_on(self):
        r = self.publish()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("gitleaks is not installed", r.stderr)
        self.assertIn("refs/heads/main", self.refs())

    @unittest.skipUnless(shutil.which("gitleaks"), "gitleaks is not installed")
    def test_a_secret_only_gitleaks_knows_stops_it(self):
        self.env["PATH"] = os.path.dirname(shutil.which("gitleaks")) + os.pathsep + self.env["PATH"]
        shutil.copy(os.path.join(ROOT, ".gitleaks.toml"), os.path.join(self.src, ".gitleaks.toml"))
        # a Slack webhook address: it is not in the common forms; assembled from parts so it does not appear in this file
        self.write("notify.py", 'URL = "https://hooks.slack.com/services/' + "T0A1B2C3D/B0A1B2C3D/" +
                   "a1B2c3D4e5F6g7H8i9J0k1L2" + '"\n')
        self.commit()
        r = self.publish("--check")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("gitleaks", r.stderr)

    def test_private_parts_stay_out_of_the_snapshot(self):
        # docs/ is committed but not published; the operator's own files are in .gitignore and stay out of the snapshot
        self.write("docs/spec.md", "internal spec\n")
        self.write("evals/locale/ru/prompts/case.json", "{}\n")   # the cases of a Russian-speaking owner stay at home
        self.write("evals/prompts/case.json", "{}\n")
        self.write(".gitignore",
                   ".publish-deny\n.publish-deny-case\n__pycache__/\nCLAUDE.local.md\n.claude/worktrees/\n")
        self.commit()
        self.write(".publish-deny-case", "Forbidden-Case\n")
        self.write("CLAUDE.local.md", "this machine\n")
        self.write(".claude/worktrees/wt/notes.md", "copy\n")
        r = self.publish()
        self.assertEqual(r.returncode, 0, r.stderr)
        names = self.git(self.bare, "ls-tree", "-r", "--name-only", "main").split()
        self.assertIn("app.py", names)
        self.assertIn("evals/prompts/case.json", names)
        self.assertEqual([n for n in names if n.startswith(("docs/", "evals/locale/", ".claude/", ".publish-deny", "CLAUDE.local"))], [])

    def test_an_edit_under_the_unpublished_folders_does_not_stop_a_release(self):
        # docs/ and evals/locale/ru/ are not in the snapshot: a tracked file edited there is no unpublished change
        self.write(".gitignore", ".publish-deny\n.publish-deny-case\n__pycache__/\n")
        self.write("docs/spec.md", "internal spec\n")
        self.write("evals/locale/ru/prompts/case.json", "{}\n")
        self.commit()
        self.write(".publish-deny-case", "Forbidden-Case\n")
        self.write("docs/spec.md", "edited\n")
        self.write("evals/locale/ru/prompts/case.json", '{"edited": 1}\n')
        r = self.publish()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.write("app.py", "A = 2\n")   # a tracked file of the snapshot, edited and not committed: that does stop it
        self.assertEqual(self.publish().returncode, 2)

    def test_the_first_publication_takes_changes_of_any_date(self):
        # a new repo gets the whole CHANGES.md: its latest entry may be from any day
        self.write("CHANGES.md", "## 2020-01-02\n\n- Old.\n")
        self.commit()
        r = self.publish()
        self.assertEqual(r.returncode, 0, r.stderr)
        # after that a new entry must again be under the publication day
        self.write("CHANGES.md", "## 2020-01-03\n\n- New.\n\n## 2020-01-02\n\n- Old.\n")
        self.commit()
        r = self.publish()
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn("heading of the day", r.stderr)

    def test_a_day_of_changes_longer_than_one_argument_goes_out(self):
        # the day's entry is the snapshot's commit message; past 128 KB one command-line argument is refused
        # ("Argument list too long") — a release day with many items stopped the publication
        item = "- " + "word " * 199 + "\n"
        self.write("CHANGES.md", f"## {datetime.date.today():%Y-%m-%d}\n\n" + item * 150 + "\n### For sessions\n- A rule.\n")
        self.commit()
        r = self.publish()
        self.assertEqual(r.returncode, 0, r.stderr)
        body = self.git(self.bare, "log", "-1", "--format=%B", "main")
        self.assertTrue(body.startswith("Sync "))
        self.assertEqual(body.count("word"), 199 * 150)
        self.assertIn("### For sessions", body)   # a heading of the entry is no comment line of the message

    def test_the_snapshot_credits_the_author_and_co_authors_the_newly_thanked(self):
        # owner decision 2026-11-17: the README's author line heads the contributors built from the whole CHANGES.md,
        # and the snapshot's commit co-authors those thanked since the last one (their ids — a cache beside the state)
        day = f"## {datetime.date.today():%Y-%m-%d}\n\n"
        with open(os.path.join(self.state, "github-ids.json"), "w", encoding="utf-8") as f:
            json.dump({"bob": 101, "carol": 102}, f)
        self.write("README.md", "# Hub\n\nCreated by Alice Smith ([@alice](https://github.com/alice))\n")
        self.write("CHANGES.md", day + "- One — thanks: @bob (PR #7)\n\n## 2020-01-01\n\n- Old — thanks: @alice\n")
        self.commit()
        r = self.publish()
        self.assertEqual(r.returncode, 0, r.stderr)
        body = self.git(self.bare, "log", "-1", "--format=%B", "main")
        self.assertTrue(body.rstrip().endswith("\n\nCo-authored-by: bob <101+bob@users.noreply.github.com>"), body)
        self.assertNotIn("alice", body.lower().split("co-authored-by", 1)[1])   # the author authors the commit
        listing = self.git(self.bare, "show", "main:CONTRIBUTORS.md")
        self.assertIn("- [@bob](https://github.com/bob): 1 pull request", listing)
        self.assertIn("## Contributors", self.git(self.bare, "show", "main:README.md"))
        self.write("CHANGES.md", day + "- Two — thanks: @carol\n- One — thanks: @bob (PR #7)\n\n## 2020-01-01\n\n- Old.\n")
        self.commit()
        r = self.publish()
        self.assertEqual(r.returncode, 0, r.stderr)
        body = self.git(self.bare, "log", "-1", "--format=%B", "main")
        self.assertEqual([x for x in body.splitlines() if x.startswith("Co-authored-by:")],
                         ["Co-authored-by: carol <102+carol@users.noreply.github.com>"])
        self.write("README.md", "# Hub\n")   # the author line is gone: no snapshot without its credits
        self.commit()
        r = self.publish()
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("no author line", r.stderr)

    def test_a_snapshot_after_one_without_credits_names_everyone_not_yet_credited_once(self):
        # the credits by the counts alone left the colleagues of the older snapshots out of the repository's contributors:
        # the next snapshot names whoever the thanks name and no commit of the remote main credits yet, then nobody twice
        day = f"## {datetime.date.today():%Y-%m-%d}\n\n"
        names = ["bob", "carol", "dave", "erin"]
        nogh = os.path.join(self.root, "nogh")   # no id can be asked: the first snapshot is made without trailers
        os.makedirs(nogh)
        with open(os.path.join(nogh, "gh"), "w") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.chmod(os.path.join(nogh, "gh"), 0o755)
        self.write("README.md", "# Hub\n\nCreated by Alice Smith ([@alice](https://github.com/alice))\n")
        self.write("CHANGES.md", day + "- Old — thanks: @bob (PR #1)\n- Older — thanks: @carol (PR #2)\n")
        self.commit()
        before = self.env["PATH"]
        self.env["PATH"] = nogh + os.pathsep + before
        r = self.publish()
        self.env["PATH"] = before
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Co-authored-by", self.git(self.bare, "log", "-1", "--format=%B", "main"))
        with open(os.path.join(self.state, "github-ids.json"), "w", encoding="utf-8") as f:
            json.dump({x: 100 + n for n, x in enumerate(names)}, f)
        self.write("CHANGES.md", day + "- New — thanks: @dave (PR #3)\n- Old — thanks: @bob (PR #1)\n"
                                   "- Older — thanks: @carol (PR #2)\n- Erin — thanks: @erin\n")
        self.commit()
        r = self.publish()
        self.assertEqual(r.returncode, 0, r.stderr)
        body = self.git(self.bare, "log", "-1", "--format=%B", "main")
        got = sorted(x.split()[1] for x in body.splitlines() if x.startswith("Co-authored-by:"))
        self.assertEqual(got, ["bob", "carol", "dave", "erin"], body)
        self.write("CHANGES.md", day + "- Fifth — thanks: @bob (PR #4)\n- New — thanks: @dave (PR #3)\n"
                                   "- Old — thanks: @bob (PR #1)\n- Older — thanks: @carol (PR #2)\n- Erin — thanks: @erin\n")
        self.commit()
        r = self.publish()
        self.assertEqual(r.returncode, 0, r.stderr)
        body = self.git(self.bare, "log", "-1", "--format=%B", "main")
        self.assertNotIn("Co-authored-by", body)   # all four are credited by the history: nobody twice

    def second_address(self):
        """A second public repo (a rehearsal, another organization) and its own commit."""
        other = os.path.join(self.root, "other.git")
        self.git(self.root, "init", "-q", "--bare", "-b", "main", other)
        return other

    def recorded(self):
        with open(os.path.join(self.state, "published.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_the_state_of_the_hub_is_kept_for_one_address(self):
        other = self.second_address()
        self.assertEqual(self.publish().returncode, 0)
        first = self.recorded()
        self.assertEqual(first["url"], self.bare)
        self.assertEqual(first["pub"], self.git(self.bare, "rev-parse", "main"))   # release.py puts the tag on it
        self.assertEqual(first["sha"], self.git(self.src, "rev-parse", "HEAD"))
        self.write("app.py", "a = 2\n")
        self.commit("more")
        r = self.publish(url=other)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("refs/heads/main", self.refs(other))
        self.assertEqual(self.recorded(), first)   # a rehearsal does not move what the Updates page compares against
        self.assertIn(self.bare, r.stderr)          # and says whom the state belongs to
        r = self.publish(url=other)                 # "nothing new" does not record a foreign address either
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.recorded(), first)

    def test_nothing_new_records_the_snapshot_of_its_own_address(self):
        self.assertEqual(self.publish().returncode, 0)
        os.unlink(os.path.join(self.state, "published.json"))
        r = self.publish()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("nothing new", r.stdout)
        self.assertEqual(self.recorded()["pub"], self.git(self.bare, "rev-parse", "main"))

    def test_the_configured_address_is_the_hubs_own(self):
        other = self.second_address()
        self.assertEqual(self.publish().returncode, 0)
        self.git(self.src, "config", "hub.release-url", other)
        self.write("app.py", "a = 2\n")
        self.commit("more")
        self.assertEqual(self.publish(url=other).returncode, 0)
        self.assertEqual(self.recorded()["url"], other)
        self.write("app.py", "a = 3\n")
        self.commit("even more")
        self.assertEqual(self.publish().returncode, 0)
        self.assertEqual(self.recorded()["url"], other)

    def test_a_check_leaves_the_state_alone(self):
        self.assertEqual(self.publish("--check").returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.state, "published.json")))

    GITHUB = "https://github.com/example/core.git"

    def via_github(self):
        """A GitHub address that git swaps for a local bare repo: nothing goes to the network."""
        self.env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0=f"url.{self.bare}.insteadOf",
                        GIT_CONFIG_VALUE_0=self.GITHUB)

    def test_no_plugin_list_is_built_for_the_snapshot(self):
        # the hub's old plugin list (modules/market/trusted.json, built at every release from the collaborators of the
        # shared repo) is gone (marketplace spec §4, S8): a collector left in a tree is not run, gh is not needed, and
        # neither the snapshot nor the hub's state folder gets a list
        ran = os.path.join(self.root, "collector-ran")
        self.write("modules/__init__.py", "")
        self.write("modules/market/__init__.py", "")
        self.write("modules/market/trusted.py", f"open({ran!r}, 'w').close()\n")
        self.commit("a collector left in the tree")
        self.env["PATH"] = os.pathsep.join(p for p in self.env["PATH"].split(os.pathsep)
                                           if not os.path.exists(os.path.join(p, "gh")))
        self.via_github()
        r = self.publish(url=self.GITHUB)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.refs(), ["refs/heads/main"])
        self.assertFalse(os.path.exists(ran))
        self.assertNotIn("trusted.json", r.stderr)
        self.assertEqual(self.git(self.bare, "ls-tree", "-r", "--name-only", "main", "modules/market"),
                         "modules/market/__init__.py\nmodules/market/trusted.py")
        self.assertFalse(os.path.exists(os.path.join(self.state, "market", "trusted.json")))


@unittest.skipIf(os.name == "nt" or not shutil.which("sh"), "publish.sh — sh")
class Pipefail(unittest.TestCase):
    def test_an_sh_without_pipefail_hands_the_script_to_bash(self):
        # dash before 0.5.13 (/bin/sh on Ubuntu 24.04) does not know pipefail: `set -o pipefail` crashed the script
        # before the checks, and without it a failed part of the pipe counted as success
        with open(os.path.join(ROOT, "tools", "publish.sh")) as f:
            text = f.read()
        head = text[:text.index("REPO=")]
        self.assertIn("set -o pipefail", head)
        self.assertIn('exec bash "$0" "$@"', head)


class OpenTargets(unittest.TestCase):
    def test_the_refused_list_is_the_one_oss_open_opens_by_default(self):
        sys.path[:0] = [ROOT, os.path.join(ROOT, "tools", "package")]
        self.addCleanup(lambda: [sys.path.remove(p) for p in (ROOT, os.path.join(ROOT, "tools", "package")) if p in sys.path])
        from tools import oss_open
        self.assertEqual(oss_open.OPEN_TARGETS, (oss_open.SLUG, oss_open.marketplaces.OPEN_SOURCE))
        self.assertEqual(oss_open.SLUG, "icons8/hub")


if __name__ == "__main__":
    unittest.main()
