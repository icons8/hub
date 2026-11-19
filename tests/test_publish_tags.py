"""Release in two steps (tools/publish.sh): --nightly — a snapshot into main without the test stands; --tag vX.Y.Z /
--release — a release tag on an already published snapshot, after the stands (compat, own checks). A bare "shared"
repository in a temp folder."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

import platforms

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@unittest.skipIf(os.name == "nt", "publish.sh — sh")
class Tags(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.repo = os.path.join(self.root, "repo")
        self.state = os.path.join(self.root, "state")
        self.config = os.path.join(self.root, "config")
        os.makedirs(os.path.join(self.repo, "tools"))
        os.makedirs(self.state)
        os.makedirs(self.config)
        for name in ("publish.sh", "publish_allow.py", "publish-allow.txt", "publish-deny.txt", "publish_checks.py",
                     "publish_state.py", "release.py", "publish_remote.py", "private_check.py", "contributors.py",
                     "publish_guard.py"):
            with open(os.path.join(REPO, "tools", name)) as f, open(os.path.join(self.repo, "tools", name), "w") as g:
                g.write(f.read())
        # private_check reads the runner's earlier line forms from the language data of the repository it belongs to
        shutil.copytree(os.path.join(REPO, "i18n", "data"), os.path.join(self.repo, "i18n", "data"))
        with open(os.path.join(self.repo, "tools", "public_language.py"), "w") as g:
            g.write("raise SystemExit(0)\n")   # the language ratchet has its own test (test_publish)
        with open(os.path.join(self.repo, "tools", "oss_open.py"), "w") as g:
            g.write('OPEN_TARGETS = ("icons8/hub",)\n')   # the real list is read in test_publish
        with open(os.path.join(self.repo, "tools", "i18n.py"), "w") as g:
            g.write("raise SystemExit(0)\n")   # the translations gate has its own test (test_publish)
        # without its own deny patterns publish.sh does not publish (closed by default): a harmless one, in
        # .gitignore, like a hub's
        with open(os.path.join(self.repo, ".publish-deny"), "w") as f:
            f.write("forbidden-name\n")
        with open(os.path.join(self.repo, ".gitignore"), "w") as f:
            f.write(".publish-deny\n__pycache__/\n")
        self.remote = os.path.join(self.root, "remote.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.remote], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", self.repo], check=True)
        self.compat(0)
        self.change("- first item")

    def git(self, *a, cwd=None):
        return subprocess.run(["git", "-C", cwd or self.repo, "-c", "user.name=t", "-c", "user.email=t@example.com", *a],
                              check=True, capture_output=True, text=True).stdout.strip()

    def compat(self, code):
        """Compatibility stand: prints a line (so it is visible it ran) and exits with code."""
        with open(os.path.join(self.repo, "tools", "compat.py"), "w") as f:
            f.write(f"import sys\nprint('compatibility table')\nsys.exit({code})\n")

    def change(self, *items, day=None):
        """A new CHANGES.md item under today's date (at the top) — and a commit."""
        path = os.path.join(self.repo, "CHANGES.md")
        old = open(path).read() if os.path.exists(path) else ""
        head = f"## {day or time.strftime('%Y-%m-%d')}\n\n"
        body = old[len(head):] if old.startswith(head) else "\n" + old if old else ""
        with open(path, "w") as f:
            f.write(head + "\n".join(items) + "\n" + body)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "x")

    def publish(self, *flags):
        env = {**os.environ, "PUBLISH_NAME": "t", "PUBLISH_EMAIL": "t@example.com", "HUB_STATE_DIR": self.state,
               "HUB_CONFIG_DIR": self.config, "PATH": os.path.dirname(sys.executable) + ":/usr/bin:/bin"}
        return subprocess.run(["sh", os.path.join(self.repo, "tools", "publish.sh"), self.remote, *flags],
                              capture_output=True, text=True, env=env, timeout=60)

    def remote_ref(self, ref):
        r = subprocess.run(["git", "-C", self.remote, "rev-parse", "--verify", "-q", ref], capture_output=True, text=True)
        return r.stdout.strip() or None

    def nightly(self):
        out = self.publish("--nightly")
        self.assertEqual(out.returncode, 0, out.stderr)
        return out

    def read(self, name):
        with open(os.path.join(self.state, name)) as f:
            return json.load(f)

    def test_nightly_pushes_main_without_the_stands_and_records_the_snapshot(self):
        self.compat(3)   # the stand is red — nightly does not call it
        self.git("commit", "-q", "-am", "red compat")
        out = self.nightly()
        self.assertNotIn("compatibility table", out.stdout)
        pub = self.read("published.json")
        self.assertEqual(pub["sha"], self.git("rev-parse", "HEAD"))
        self.assertEqual(pub["pub"], self.remote_ref("main"))

    def test_nightly_with_stands_runs_them_before_main_is_pushed(self):
        # hubs on the main channel take the main snapshot at once: the release benches run before it is first available
        self.compat(3)
        self.git("commit", "-q", "-am", "red compat")
        out = self.publish("--nightly", "--stands")
        self.assertEqual(out.returncode, 5, out.stderr)
        self.assertIn("compatibility table", out.stdout)
        self.assertIsNone(self.remote_ref("main"))
        self.compat(0)
        self.git("commit", "-q", "-am", "green compat")
        with open(os.path.join(self.config, "publish-checks"), "w") as f:
            f.write("#!/bin/sh\necho own checks are red; exit 1\n")
        os.chmod(os.path.join(self.config, "publish-checks"), 0o755)
        out = self.publish("--nightly", "--stands")
        self.assertEqual(out.returncode, 7, out.stderr)
        self.assertIsNone(self.remote_ref("main"))
        os.unlink(os.path.join(self.config, "publish-checks"))
        out = self.publish("--nightly", "--stands")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("compatibility table", out.stdout)
        self.assertEqual(self.read("published.json")["pub"], self.remote_ref("main"))

    def test_stands_flag_is_only_for_nightly(self):
        out = self.publish("--stands")
        self.assertEqual(out.returncode, 2, out.stderr)
        self.assertIsNone(self.remote_ref("main"))

    def test_without_nightly_a_publication_still_runs_the_stands(self):
        self.compat(3)
        self.git("commit", "-q", "-am", "red compat")
        out = self.publish()
        self.assertEqual(out.returncode, 5, out.stderr)
        self.assertIsNone(self.remote_ref("main"))

    def pinning_compat(self, version, code=0):
        """A compat stand that logs its arguments and, with --pin <tree>, writes `version` as the tree's CLAUDE_TESTED (code:
        the exit code of --pin). The version lives outside the repo: a later call only moves Anthropic's stable."""
        log, ver = os.path.join(self.root, "compat-args"), os.path.join(self.root, "stable")
        with open(ver, "w") as f:
            f.write(version)
        with open(os.path.join(self.repo, "tools", "compat.py"), "w") as f:
            f.write(f"import os, re, sys\nopen({log!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
                    f"if sys.argv[1:2] == ['--pin']:\n"
                    f"    p = os.path.join(sys.argv[2], 'core', 'version.py')\n"
                    f"    t = open(p).read()\n"
                    f"    open(p, 'w').write(re.sub(r'CLAUDE_TESTED = \"[^\"]*\"', 'CLAUDE_TESTED = \"' + open({ver!r}).read() + '\"', t))\n"
                    f"    sys.exit({code})\n"
                    f"print('compatibility table')\n")
        os.makedirs(os.path.join(self.repo, "core"), exist_ok=True)
        with open(os.path.join(self.repo, "core", "version.py"), "w") as f:
            f.write('PLUGIN_API = "1.9"\nCLAUDE_TESTED = "2.1.218"   # pinned by publish.sh\n')
        with open(os.path.join(self.repo, "core", "forms.py"), "w") as f:   # release.py reads its headings
            f.write('SESSION_SECTIONS = ("### For sessions",)\n')
        self.git("add", "-A")
        if self.git("status", "--porcelain"):   # a later call with the same code changes nothing in the repo
            self.git("commit", "-q", "-m", "version")
        return log

    def test_every_snapshot_carries_the_claude_the_tag_is_tested_on(self):
        # owner decision 2026-11-11: test only on the latest Claude Code at the time of the release, and a hub that needs a
        # newer one installs it itself: the snapshot pins the stable channel's version, the tag's stands test exactly it
        log = self.pinning_compat("2.1.400")
        self.nightly()
        self.assertIn('CLAUDE_TESTED = "2.1.400"', self.git("show", "main:core/version.py", cwd=self.remote))
        with open(os.path.join(self.repo, "core", "version.py")) as f:
            self.assertIn('CLAUDE_TESTED = "2.1.218"', f.read())   # the working copy keeps its own
        out = self.publish("--tag", "v1.0.0")
        self.assertEqual(out.returncode, 0, out.stderr)
        with open(log) as f:
            calls = f.read().splitlines()
        self.assertTrue(calls[0].startswith("--pin "), calls)
        self.assertEqual(calls[-1], "--versions 2.1.400")

    def test_a_newer_claude_alone_is_no_new_snapshot(self):
        # Anthropic's stable went on while the code did not: no Sync for that alone (the hubs on main would restart onto an
        # untested claude); the next snapshot with a change of its own carries the new version
        self.pinning_compat("2.1.400")
        self.nightly()
        snap = self.remote_ref("main")
        self.pinning_compat("2.1.401")   # only Anthropic's stable moved
        out = self.nightly()
        self.assertIn("nothing new", out.stdout)
        self.assertEqual(self.remote_ref("main"), snap)
        self.assertEqual(self.read("published.json")["sha"], self.git("rev-parse", "HEAD"))
        self.change("- second item")
        self.nightly()
        self.assertNotEqual(self.remote_ref("main"), snap)
        self.assertIn('CLAUDE_TESTED = "2.1.401"', self.git("show", "main:core/version.py", cwd=self.remote))

    def test_no_claude_version_no_snapshot(self):
        self.pinning_compat("2.1.400", code=2)
        for flags in (("--nightly",), ()):
            out = self.publish(*flags)
            self.assertEqual(out.returncode, 11, out.stderr)
            self.assertIn("CLAUDE_TESTED", out.stderr)
        self.assertIsNone(self.remote_ref("main"))

    def test_tag_goes_on_the_published_snapshot_after_the_stands(self):
        self.nightly()
        out = self.publish("--tag", "v1.0.0")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("compatibility table", out.stdout)
        self.assertEqual(self.remote_ref("refs/tags/v1.0.0^{commit}"), self.remote_ref("main"))
        self.assertEqual(self.git("cat-file", "-t", "v1.0.0", cwd=self.remote), "tag")   # annotated
        self.assertIn("first item", self.git("tag", "-l", "--format=%(contents)", "v1.0.0", cwd=self.remote))
        rel = self.read("published-stable.json")
        self.assertEqual((rel["tag"], rel["sha"], rel["pub"]), ("v1.0.0", self.git("rev-parse", "HEAD"),
                                                                 self.remote_ref("main")))

    def test_a_refused_release_records_nothing(self):
        self.nightly()
        self.compat(3)
        self.git("commit", "-q", "-am", "red compat")
        self.nightly()
        self.assertEqual(self.publish("--tag", "v1.0.0").returncode, 5)
        self.assertFalse(os.path.exists(os.path.join(self.state, "published-stable.json")))

    def test_red_stands_stop_the_tag(self):
        self.nightly()
        self.compat(3)
        self.git("commit", "-q", "-am", "red compat")
        self.nightly()
        out = self.publish("--tag", "v1.0.0")
        self.assertEqual(out.returncode, 5, out.stderr)
        self.assertIsNone(self.remote_ref("refs/tags/v1.0.0"))

    def test_own_checks_stop_the_tag(self):
        self.nightly()
        with open(os.path.join(self.config, "publish-checks"), "w") as f:
            f.write("#!/bin/sh\necho own checks are red; exit 1\n")
        os.chmod(os.path.join(self.config, "publish-checks"), 0o755)
        out = self.publish("--tag", "v1.0.0")
        self.assertEqual(out.returncode, 7, out.stderr)
        self.assertIsNone(self.remote_ref("refs/tags/v1.0.0"))

    def test_the_tag_needs_head_to_be_what_main_published(self):
        out = self.publish("--tag", "v1.0.0")                      # nothing has been published yet
        self.assertEqual(out.returncode, 2, out.stderr)
        self.nightly()
        self.change("- not published")
        out = self.publish("--tag", "v1.0.0")
        self.assertEqual(out.returncode, 2, out.stderr)
        self.assertIn("--nightly", out.stderr)
        self.assertNotIn("compatibility table", out.stdout)
        self.assertIsNone(self.remote_ref("refs/tags/v1.0.0"))

    def test_a_tag_twice_or_older_is_refused(self):
        self.nightly()
        self.assertEqual(self.publish("--tag", "v1.2.0").returncode, 0)
        self.change("- second item")
        self.nightly()
        for tag in ("v1.2.0", "v1.1.9"):
            out = self.publish("--tag", tag)
            self.assertEqual(out.returncode, 2, out.stderr)
        self.assertIsNone(self.remote_ref("refs/tags/v1.1.9"))
        out = self.publish("--tag", "v1.2.1")
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_a_snapshot_is_released_once(self):
        self.nightly()
        self.assertEqual(self.publish("--tag", "v1.0.0").returncode, 0)
        out = self.publish("--tag", "v1.1.0")
        self.assertEqual(out.returncode, 2, out.stderr)
        self.assertIn("v1.0.0", out.stderr)

    def test_bad_tag_names_are_refused(self):
        self.nightly()
        for bad in ("1.0.0", "v1.0", "v1.0.0-rc1", "--check"):
            self.assertEqual(self.publish("--tag", bad).returncode, 2, bad)

    def test_release_numbers_minor_once_a_day_and_patch_after(self):
        self.nightly()
        out = self.publish("--release")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIsNotNone(self.remote_ref("refs/tags/v1.0.0"))   # first release
        self.change("- a fix")
        self.nightly()
        self.assertEqual(self.publish("--release").returncode, 0)
        self.assertIsNotNone(self.remote_ref("refs/tags/v1.0.1"))   # second in a day — a patch
        # the previous release was yesterday: the next one is a minor
        work = os.path.join(self.root, "w")
        self.git("clone", "-q", self.remote, work, cwd=self.root)
        env = {**os.environ, "GIT_COMMITTER_DATE": "2000-01-01T12:00:00"}
        subprocess.run(["git", "-C", work, "-c", "user.name=t", "-c", "user.email=t@example.com", "tag", "-a", "-m", "old",
                        "v1.3.0", "HEAD~1"], check=True, env=env)
        self.git("push", "-q", "origin", "refs/tags/v1.3.0", cwd=work)
        self.change("- new tomorrow")
        self.nightly()
        out = self.publish("--release")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIsNotNone(self.remote_ref("refs/tags/v1.4.0"))

    def test_tag_notes_are_the_entries_since_the_previous_release(self):
        self.nightly()
        self.assertEqual(self.publish("--tag", "v1.0.0").returncode, 0)
        self.change("- second item", "", "### For sessions", "", "- for sessions only")
        self.nightly()
        self.assertEqual(self.publish("--tag", "v1.0.1").returncode, 0)
        notes = self.git("tag", "-l", "--format=%(contents)", "v1.0.1", cwd=self.remote)
        self.assertIn("second item", notes)
        self.assertNotIn("first item", notes)
        self.assertNotIn("for sessions only", notes)

    def test_tag_notes_keep_the_day_headers(self):
        # review: git tag -m drops lines starting with "#" by default — the "## 2026-…" day headings vanished
        self.nightly()
        self.assertEqual(self.publish("--tag", "v1.0.0").returncode, 0)
        self.assertIn("## " + time.strftime("%Y-%m-%d"), self.git("tag", "-l", "--format=%(contents)", "v1.0.0",
                                                                   cwd=self.remote))

    def test_long_tag_notes_go_whole(self):
        # 2026-10-18, v1.1.0: notes of ~140 KB as one `git tag -m` argument — above the single-argument limit on Linux
        # (128 KiB; on Windows the whole command line is up to 32k characters): OSError E2BIG after green stands
        # as in a real release: the day is small, but there are many days since the previous release
        def write(text):
            with open(os.path.join(self.repo, "CHANGES.md"), "w") as f:
                f.write(text)
            self.git("add", "-A")
            self.git("commit", "-q", "-m", "x")
        old = "## 2001-01-01\n\n- first item\n"
        write(old)
        self.nightly()   # first publication — the whole CHANGES.md, with any date
        self.assertEqual(self.publish("--tag", "v1.0.0").returncode, 0)
        days = "".join(f"## 2001-01-{d:02d}\n\n" + "".join(f"- item {d}.{i} " + "é" * 200 + "\n" for i in range(200))
                       + "\n" for d in (3, 2))
        write(f"## {time.strftime('%Y-%m-%d')}\n\n- last item\n\n" + days + old)
        self.nightly()
        out = self.publish("--tag", "v1.0.1")
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        notes = self.git("tag", "-l", "--format=%(contents)", "v1.0.1", cwd=self.remote)
        self.assertGreater(len(notes.encode()), 140_000)
        self.assertTrue(notes.startswith("v1.0.1\n\n## "), notes[:80])
        self.assertIn("- last item", notes)

    def test_an_older_snapshot_is_not_released_under_a_newer_number(self):
        # review: published.json was stale (main was released from another copy) — a newer tag was put on a snapshot
        # older than the release
        self.nightly()
        old_head, old_pub = self.git("rev-parse", "HEAD"), open(os.path.join(self.state, "published.json")).read()
        self.change("- second item")
        self.nightly()
        self.assertEqual(self.publish("--tag", "v1.0.0").returncode, 0)
        self.git("reset", "-q", "--hard", old_head)
        with open(os.path.join(self.state, "published.json"), "w") as f:
            f.write(old_pub)
        out = self.publish("--tag", "v1.1.0")
        self.assertNotEqual(out.returncode, 0, out.stdout)
        self.assertIn("does not continue", out.stderr + out.stdout)
        self.assertIsNone(self.remote_ref("refs/tags/v1.1.0"))

    def test_a_tag_off_main_does_not_hold_the_number(self):
        # review: a tag on a foreign commit (experience) was counted as a release — the next one refused "not newer
        # than v9.0.0"
        self.nightly()
        side = os.path.join(self.root, "side")
        subprocess.run(["git", "clone", "-q", self.remote, side], check=True)
        self.git("commit", "-q", "--allow-empty", "-m", "experiment", cwd=side)
        self.git("tag", "-a", "-m", "oops", "v9.0.0", cwd=side)
        self.git("push", "-q", "origin", "refs/tags/v9.0.0", cwd=side)
        out = self.publish("--release")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.remote_ref("refs/tags/v1.0.0^{commit}"), self.remote_ref("main"))

    def test_a_name_taken_off_main_is_refused_before_the_stands(self):
        self.nightly()
        side = os.path.join(self.root, "side2")
        subprocess.run(["git", "clone", "-q", self.remote, side], check=True)
        self.git("commit", "-q", "--allow-empty", "-m", "experiment", cwd=side)
        self.git("tag", "-a", "-m", "oops", "v1.0.0", cwd=side)
        self.git("push", "-q", "origin", "refs/tags/v1.0.0", cwd=side)
        out = self.publish("--release", "--check")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("is taken", out.stderr + out.stdout)
        self.assertNotIn("compatibility table", out.stdout)

    def test_check_tag_runs_no_stands_and_pushes_nothing(self):
        self.nightly()
        out = self.publish("--tag", "v1.0.0", "--check")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("v1.0.0", out.stdout)
        self.assertNotIn("compatibility table", out.stdout)
        self.assertIsNone(self.remote_ref("refs/tags/v1.0.0"))
        self.assertFalse(os.path.exists(os.path.join(self.state, "published-stable.json")))

    def test_nothing_new_still_records_what_is_published(self):
        self.nightly()
        os.unlink(os.path.join(self.state, "published.json"))
        self.git("commit", "-q", "--allow-empty", "-m", "local only")
        out = self.publish("--nightly")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("nothing new", out.stdout)
        self.assertEqual(self.read("published.json")["sha"], self.git("rev-parse", "HEAD"))
        self.assertEqual(self.publish("--tag", "v1.0.0").returncode, 0)

    # main on the remote could be moved by something other than publish.sh (2026-10-21: a colleague with write access
    # merged two PRs straight into main, and a release by snapshot silently rolled them back): foreign commits after
    # the last publication — a refusal with a list, until they are sorted out
    def push_remote(self, *, name="Colleague", mail="colleague@example.com", subject="Fix from a PR", file="pr.txt"):
        """A commit straight into the remote's main — like a PR merge on GitHub; its sha."""
        work = os.path.join(self.root, f"work-{len(os.listdir(self.root))}")
        subprocess.run(["git", "clone", "-q", self.remote, work], check=True, capture_output=True)
        with open(os.path.join(work, file), "w") as f:
            f.write(subject + "\n")
        run = ["git", "-C", work, "-c", f"user.name={name}", "-c", f"user.email={mail}"]
        subprocess.run([*run, "add", "-A"], check=True)
        subprocess.run([*run, "commit", "-q", "-m", subject], check=True)
        subprocess.run([*run, "push", "-q", "origin", "main"], check=True, capture_output=True)
        return self.remote_ref("main")

    def test_a_commit_on_remote_main_not_made_by_publish_stops_the_snapshot(self):
        self.nightly()
        theirs = self.push_remote()
        self.change("- second item")
        out = self.publish("--nightly")
        self.assertEqual(out.returncode, 6, out.stderr)
        for part in (theirs[:7], "Colleague", "Fix from a PR", "--overwrite-remote"):
            self.assertIn(part, out.stderr)
        self.assertEqual(self.remote_ref("main"), theirs)          # their commit is in place
        out = self.publish("--nightly", "--check")                 # the check shows the same and does not pass
        self.assertEqual(out.returncode, 6, out.stderr)
        self.assertIn(theirs[:7], out.stderr)
        self.assertEqual(self.remote_ref("main"), theirs)
        out = self.publish("--nightly", "--overwrite-remote")      # explicit: sorted out — overwrite
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotEqual(self.remote_ref("main"), theirs)
        self.change("- third item")                              # clean from here: the last publication is after them
        self.nightly()

    def test_only_syncs_of_the_publisher_after_the_publication_pass(self):
        self.nightly()
        self.push_remote(name="t", mail="t@example.com", subject="Sync 2026-01-02")   # a snapshot from another copy
        self.change("- second item")
        self.nightly()
        self.push_remote(name="t", mail="t@example.com", subject="hand-made fix")     # the same author, but not Sync
        self.change("- third item")
        out = self.publish("--nightly")
        self.assertEqual(out.returncode, 6, out.stderr)
        self.assertIn("hand-made fix", out.stderr)

    def test_a_commit_after_an_older_overwriting_sync_is_not_listed_again(self):
        self.nightly()
        self.push_remote(subject="Lost long ago")
        self.push_remote(name="t", mail="t@example.com", subject="Sync 2026-01-02", file="pr2.txt")
        self.change("- second item")
        out = self.publish("--nightly")
        self.assertEqual(out.returncode, 0, out.stderr)    # pub from published.json is older; the rollback was a snapshot
        self.assertNotIn("Lost long ago", out.stderr)

    def test_without_a_record_the_last_sync_of_the_publisher_is_the_base(self):
        self.nightly()
        os.unlink(os.path.join(self.state, "published.json"))
        theirs = self.push_remote()
        self.change("- second item")
        out = self.publish("--nightly")
        self.assertEqual(out.returncode, 6, out.stderr)
        self.assertIn(theirs[:7], out.stderr)

    def test_a_rewritten_remote_history_stops_the_snapshot(self):
        self.nightly()
        work = os.path.join(self.root, "rewrite")
        subprocess.run(["git", "init", "-q", "-b", "main", work], check=True)
        subprocess.run(["git", "-C", work, "-c", "user.name=Colleague", "-c", "user.email=c@example.com", "commit", "-q",
                        "--allow-empty", "-m", "new root"], check=True)
        subprocess.run(["git", "-C", work, "push", "-q", "-f", self.remote, "main"], check=True, capture_output=True)
        self.change("- second item")
        out = self.publish("--nightly")
        self.assertEqual(out.returncode, 6, out.stderr)
        self.assertIn("--overwrite-remote", out.stderr)
        self.assertEqual(self.publish("--nightly", "--overwrite-remote").returncode, 0)

    def test_their_commit_already_carried_over_is_nothing_new(self):
        self.nightly()
        theirs = self.push_remote()
        with open(os.path.join(self.repo, "pr.txt"), "w") as f:    # carried over to ours — snapshot matched their main
            f.write("Fix from a PR\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "carried over")
        out = self.publish("--nightly")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("nothing new", out.stdout)
        self.assertEqual(self.remote_ref("main"), theirs)

    def test_a_commit_on_remote_main_stops_the_tag_too(self):
        self.nightly()
        self.push_remote()
        out = self.publish("--tag", "v1.0.0")
        self.assertEqual(out.returncode, 6, out.stderr)
        self.assertIn("Fix from a PR", out.stderr)
        self.assertNotIn("compatibility table", out.stdout)
        self.assertIsNone(self.remote_ref("refs/tags/v1.0.0"))
        out = self.publish("--tag", "v1.0.0", "--check")
        self.assertEqual(out.returncode, 6, out.stderr)
        self.assertIn("Fix from a PR", out.stderr)
        out = self.publish("--release", "--overwrite-remote")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIsNotNone(self.remote_ref("refs/tags/v1.0.0"))


    # --- test of private modules against the candidate (extraction spec §2.2): nightly, tag and --check alike ---

    def private_repo(self, pin_head=False, form=0):
        """A local "private modules repo": two commits, tools/test.py prints lines like the real one. It is red if the
        candidate's core has a BREAK file or the self.flag flag file exists; the second commit is always red. The pin in
        modules/private.json in the candidate tree — on the first commit (on the second with pin_head). form: which of
        the lines private_check reads (tools/private_check.FORMS) the runner prints."""
        sys.path.insert(0, os.path.join(REPO, "tools"))
        import private_check
        ran, skipped = private_check.FORMS[form]
        self.priv = os.path.join(self.root, "private")
        self.flag = os.path.join(self.root, "red-flag")
        script = (
            "import os, sys\n"
            "core = os.environ['HUB_CORE_REPO']\n"
            f"red = {{RED}} or os.path.exists(os.path.join(core, 'BREAK')) or os.path.exists({self.flag!r})\n"
            f"print('WORK (abc) · accounts: ' + ('FAILED' if red else 'OK') + ' — {ran} 3, {skipped} 0')\n"
            f"skip = 2 if os.path.exists(os.path.join(core, 'SKIP')) else 0\n"
            f"print('WORK (abc) · stats: OK — {ran} 2, {skipped} %d' % skip)\n"
            "print('FAILED' if red else 'OK')\n"
            "sys.exit(1 if red else 0)\n")
        os.makedirs(os.path.join(self.priv, "tools"))
        subprocess.run(["git", "init", "-q", "-b", "main", self.priv], check=True)
        shas = []
        for red in (False, True):
            with open(os.path.join(self.priv, "tools", "test.py"), "w") as f:
                f.write(script.replace("{RED}", repr(red)))
            self.git("add", "-A", cwd=self.priv)
            self.git("commit", "-q", "-m", "x", cwd=self.priv)
            shas.append(self.git("rev-parse", "HEAD", cwd=self.priv))
        self.pin(self.priv, shas[1] if pin_head else shas[0])
        return shas

    def pin(self, url, commit, modules=("accounts", "stats")):
        os.makedirs(os.path.join(self.repo, "modules"), exist_ok=True)
        with open(os.path.join(self.repo, "modules", "private.json"), "w") as f:
            json.dump({"repo": "x/pm", "url": url, "ref": "pm-v1.0.0", "commit": commit,
                       "modules": {m: {"subdir": m, "cap": "stats"} for m in modules}}, f)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "pin")

    def test_green_private_modules_let_nightly_and_check_through(self):
        self.private_repo()
        out = self.publish("--nightly", "--check")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("accounts", out.stdout)
        self.assertIsNone(self.remote_ref("main"))
        self.nightly()
        self.assertIsNotNone(self.remote_ref("main"))

    def test_the_line_of_the_modules_runner_is_read_in_each_of_its_forms(self):
        # the pinned modules repository prints its own runner's line: the form it printed before the English output too
        sys.path.insert(0, os.path.join(REPO, "tools"))
        import private_check
        for form in range(len(private_check.FORMS)):
            with self.subTest(form=form):
                self.private_repo(form=form)
                try:
                    out = self.publish("--nightly", "--check")
                    self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
                    self.assertIn("accounts 3 tests", out.stdout)
                finally:
                    platforms.current.rmtree(self.priv)   # read-only git objects on Windows

    def test_red_private_modules_stop_nightly_and_check(self):
        self.private_repo()
        with open(os.path.join(self.repo, "BREAK"), "w") as f:   # a core edit after which the module goes red
            f.write("x")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "break")
        for flags in (("--nightly",), ("--nightly", "--check")):
            out = self.publish(*flags)
            self.assertEqual(out.returncode, 8, out.stderr)
            self.assertIn("accounts: FAILED", out.stdout + out.stderr)
            self.assertIn("not publishing", out.stderr)
        self.assertIsNone(self.remote_ref("main"))
        self.assertFalse(os.path.exists(os.path.join(self.state, "published.json")))

    def test_skipped_tests_of_the_modules_are_not_a_pass(self):
        # a core without what the module needs: its tests are skipped ("needs a newer version"), no red — the release
        # would go out unchecked
        self.private_repo()
        with open(os.path.join(self.repo, "SKIP"), "w") as f:
            f.write("x")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "skip")
        out = self.publish("--nightly", "--check")
        self.assertEqual(out.returncode, 8, out.stderr)
        self.assertIn("stats", out.stderr)

    def test_a_runner_that_never_ends_is_killed_with_its_children(self):
        sys.path.insert(0, os.path.join(REPO, "tools"))
        import private_check
        runner = os.path.join(self.root, "slow.py")
        pidfile = os.path.join(self.root, "child.pid")
        with open(runner, "w") as f:
            f.write("import subprocess, sys, time\n"
                    f"c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                    f"open({pidfile!r}, 'w').write(str(c.pid))\n"
                    "time.sleep(60)\n")
        old = private_check.TEST_WAIT
        private_check.TEST_WAIT = 2
        self.addCleanup(setattr, private_check, "TEST_WAIT", old)
        with self.assertRaises(private_check.Refused):
            private_check.run(runner, self.root, dict(os.environ))
        with open(pidfile) as f:
            pid = int(f.read())
        def gone():   # killed: not in the table or a zombie (without PID 1 to reap them, in a container)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            try:
                with open(f"/proc/{pid}/stat") as f:
                    return f.read().rsplit(")", 1)[1].split()[0] == "Z"
            except FileNotFoundError:   # the zombie was reaped between two checks
                return True
            except OSError:
                return False
        deadline = time.time() + 5
        ended = gone()
        while not ended and time.time() < deadline:
            time.sleep(0.1)
            ended = gone()
        self.assertTrue(ended)   # do not call gone() again: the state could change between checks

    def test_nothing_new_does_not_touch_the_modules_repo(self):
        self.private_repo()
        self.nightly()
        os.rename(self.priv, self.priv + ".gone")      # no access, but nothing to publish
        out = self.publish("--nightly")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("nothing new", out.stdout)

    def test_the_pinned_commit_is_tested_not_the_head_of_the_modules(self):
        self.private_repo()             # the green first one is pinned, the modules repo head is red
        self.assertEqual(self.publish("--nightly", "--check").returncode, 0)
        self.pin(self.priv, self.git("rev-parse", "HEAD", cwd=self.priv))   # now the red one is pinned
        self.assertEqual(self.publish("--nightly", "--check").returncode, 8)

    def test_no_access_to_the_modules_repo_is_a_refusal_not_a_skip(self):
        self.private_repo()
        self.pin(os.path.join(self.root, "no-such-repo"), "a" * 40)
        out = self.publish("--nightly", "--check")
        self.assertEqual(out.returncode, 8, out.stderr)
        self.assertIn("no access", out.stderr)
        self.assertIsNone(self.remote_ref("main"))

    def test_a_pinned_commit_missing_in_the_modules_repo_is_a_refusal(self):
        self.private_repo()
        self.pin(self.priv, "b" * 40)
        out = self.publish("--nightly", "--check")
        self.assertEqual(out.returncode, 8, out.stderr)
        self.assertIn("pinned commit", out.stderr)

    def test_a_module_the_runner_did_not_run_is_a_refusal(self):
        self.private_repo()
        self.pin(self.priv, self.git("rev-list", "--max-parents=0", "HEAD", cwd=self.priv),
                 modules=("accounts", "stats", "voice-reply"))
        out = self.publish("--nightly", "--check")
        self.assertEqual(out.returncode, 8, out.stderr)
        self.assertIn("voice-reply", out.stderr)

    def test_a_broken_pin_is_a_refusal(self):
        self.private_repo()
        with open(os.path.join(self.repo, "modules", "private.json"), "w") as f:
            f.write("{")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "broken pin")
        self.assertEqual(self.publish("--nightly", "--check").returncode, 8)

    def test_no_pin_means_nothing_to_test(self):
        out = self.publish("--nightly", "--check")
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_a_pin_that_retires_the_modules_means_nothing_to_test(self):
        # the release that returns the hubs to the built-in code (modules/update/private.py parse_retired)
        os.makedirs(os.path.join(self.repo, "modules"), exist_ok=True)
        with open(os.path.join(self.repo, "modules", "private.json"), "w") as f:
            json.dump({"repo": "acme/team-modules", "url": "https://example.invalid/pm.git", "modules": {},
                       "retired": {"accounts": {"subdir": "accounts", "cap": "profile"}}}, f)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "retired pin")
        out = self.publish("--nightly", "--check")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("retired", out.stdout + out.stderr)

    def test_a_retiring_pin_the_hubs_would_not_read_is_a_refusal(self):
        # hubs read it with modules/update/private.py parse_retired: a malformed one retires nothing there
        os.makedirs(os.path.join(self.repo, "modules"), exist_ok=True)
        for bad in ({"repo": "no-slash", "url": "https://example.invalid/pm.git"},
                    {"repo": "acme/pm", "url": "https://example.invalid/pm.git", "cap": "voice"}):
            with open(os.path.join(self.repo, "modules", "private.json"), "w") as f:
                json.dump({"repo": bad["repo"], "url": bad["url"], "modules": {},
                           "retired": {"accounts": {"subdir": "accounts", "cap": bad.get("cap", "profile")}}}, f)
            self.git("add", "-A")
            self.git("commit", "-q", "-m", "bad retired pin")
            self.assertEqual(self.publish("--nightly", "--check").returncode, 8, bad)

    def test_red_private_modules_stop_the_tag_before_the_stands(self):
        self.private_repo()
        self.nightly()
        with open(self.flag, "w") as f:    # modules went red after the snapshot (e.g. a golden updated)
            f.write("x")
        for flags in (("--tag", "v1.0.0", "--check"), ("--tag", "v1.0.0"), ("--tag", "v1.0.0", "--skip-compat")):
            out = self.publish(*flags)
            self.assertEqual(out.returncode, 8, out.stderr)
            self.assertIn("accounts: FAILED", out.stdout + out.stderr)
            self.assertNotIn("compatibility table", out.stdout)
        self.assertIsNone(self.remote_ref("refs/tags/v1.0.0"))
        os.unlink(self.flag)
        self.assertEqual(self.publish("--tag", "v1.0.0").returncode, 0)

    def test_a_tag_without_access_to_the_modules_repo_is_refused(self):
        self.private_repo()
        self.nightly()
        os.rename(self.priv, self.priv + ".gone")
        out = self.publish("--tag", "v1.0.0", "--check")
        self.assertEqual(out.returncode, 8, out.stderr)
        self.assertIn("no access", out.stderr)


    def test_a_failing_comparison_is_an_error_not_clean_nor_foreign(self):
        sys.path.insert(0, os.path.join(REPO, "tools"))
        import publish_remote
        not_a_repo = os.path.join(self.root, "empty")   # a repo without commits: git log fails
        subprocess.run(["git", "init", "-q", not_a_repo], check=True)
        env = {**os.environ, "HUB_STATE_DIR": self.state, "PUBLISH_NAME": "t", "PUBLISH_EMAIL": "t@example.com"}
        out = subprocess.run([sys.executable, publish_remote.__file__, not_a_repo, self.remote], capture_output=True,
                             text=True, env=env, timeout=60)
        self.assertEqual(out.returncode, 3, out.stderr)
        with self.assertRaises(publish_remote.Failed):
            publish_remote.problem(not_a_repo, "HEAD", self.remote, "", "t", "t@example.com")


if __name__ == "__main__":
    unittest.main()
