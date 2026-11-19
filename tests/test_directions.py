"""The «Directions» module: a temporary directions folder. A fake `backlog` is put in PATH as a watchdog (the hub no
longer calls the utility, it keeps the backlog itself) — the test fails if the watchdog is ever called."""
import tests  # noqa: F401 — do not see the machine's settings
import glob, json, os, re, shutil, subprocess, sys, tempfile, unittest

from tests.fakes import lang

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "modules", "directions", "direction.py")

FAKE = r'''#!{python}
import json, os, sys
with open(os.environ["FAKE_BACKLOG_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps({{"argv": sys.argv[1:]}}, ensure_ascii=False) + "\n")
'''


def commit(repo):
    subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@example.test", "commit", "-q",
                    "--allow-empty", "-m", "init"], check=True)


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)


class DirectionCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="direction-test-")
        self.hub = os.path.join(self.tmp, "hub")
        self.bin = os.path.join(self.tmp, "bin")
        self.log = os.path.join(self.tmp, "calls.jsonl")
        os.makedirs(self.bin)
        self.make_fake_backlog()
        self.repo = os.path.join(self.tmp, "code", "backend")
        os.makedirs(os.path.join(self.repo, "apps"))
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        commit(self.repo)   # a project repo has history: the direction's executors take copies from a commit
        # its own empty state folder: no live hub journal for the board facts (on Windows HOME does not move it)
        self.env = {"HUB_DIRECTIONS": os.path.join(self.hub, "directions"), "FAKE_BACKLOG_LOG": self.log,
                    "PATH": f"{self.bin}:/usr/bin:/bin", "HOME": self.tmp,
                    "HUB_STATE_DIR": os.path.join(self.tmp, "state"),
                    "HUB_LANG": os.environ["HUB_LANG"]}   # the owner's terminal in the tests' language (tests/__init__.py)
        if os.name == "nt":   # without SYSTEMROOT and the rest Windows will not even start python; git comes from the machine's PATH
            self.env = {**{k: v for k, v in os.environ.items() if not k.startswith("HUB_")}, **self.env,
                        "PATH": self.bin + os.pathsep + os.environ["PATH"]}

    def tearDown(self):
        called = self.calls()
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.assertEqual(called, [], "the hub called the backlog utility from PATH")

    def make_fake_backlog(self):
        path = os.path.join(self.bin, "backlog")
        if os.name == "nt":   # Windows does not read shebangs: backlog.cmd → python backlog.py, like an npm wrapper
            with open(path + ".cmd", "w") as f:
                f.write(f'@"{sys.executable}" "%~dp0backlog.py" %*\n')
            path += ".py"
        with open(path, "w", encoding="utf-8") as f:
            f.write(FAKE.format(python=sys.executable))
        os.chmod(path, 0o755)

    def cli(self, *args, cwd=None, **env):
        return subprocess.run([sys.executable, SCRIPT, *args], env={**self.env, **env},
                              capture_output=True, text=True, cwd=cwd)

    def calls(self):
        try:
            with open(self.log, encoding="utf-8") as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []

    def backlog_dir(self, slug):
        return os.path.join(self.hub, "directions", slug)


class TestNew(DirectionCase):
    def test_creates_backlog_without_git(self):
        r = self.cli("new", "sso-accounts", "--repo", self.repo)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isdir(os.path.join(self.backlog_dir("sso-accounts"), "backlog")))
        with open(os.path.join(self.backlog_dir("sso-accounts"), "backlog", "config.yml"), encoding="utf-8") as f:
            self.assertIn('project_name: "sso-accounts"', f.read())

    def test_adopts_the_repos_own_backlog(self):
        # a personal hub case: the repo already has its own Backlog.md (config.yml, tasks, decisions) — the direction uses it, with no second one
        tasks = os.path.join(self.repo, "backlog", "tasks")
        os.makedirs(tasks)
        with open(os.path.join(self.repo, "backlog", "config.yml"), "w") as f:
            f.write("project_name: x\n")
        for i, st in enumerate(("To Do", "Done")):
            with open(os.path.join(tasks, f"task-{i}.md"), "w") as f:
                f.write(f"---\nid: task-{i}\nstatus: {st}\n---\n")
        r = self.cli("new", "billing", "--repo", self.repo, "--adopt")
        self.assertEqual(r.returncode, 0, r.stderr)
        link = os.path.join(self.backlog_dir("billing"), "backlog")
        self.assertEqual(os.path.realpath(link), os.path.realpath(os.path.join(self.repo, "backlog")))
        self.assertFalse(glob.glob(os.path.join(self.repo, "backlog", "docs", "*")))   # there is no second backlog
        self.assertIn("billing — Done: 1, To Do: 1", self.cli("list").stdout)
        self.assertIn("task-0", self.cli("billing", "task", "list").stdout)   # commands work on the repo's backlog
        with open(os.path.join(self.backlog_dir("billing"), "direction.json")) as f:
            self.assertTrue(json.load(f)["adopted"])
        self.cli("archive", "billing")
        self.assertTrue(os.path.isfile(os.path.join(tasks, "task-0.md")))      # the direction's archive does not touch the repo

    def test_adopt_that_cannot_link_leaves_no_empty_direction(self):
        # PR review: the folder stayed empty, and every next attempt failed with «already exists»
        import importlib.util
        from unittest import mock
        import platforms
        spec = importlib.util.spec_from_file_location("direction_inproc", SCRIPT)
        direction = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(direction)
        os.makedirs(os.path.join(self.repo, "backlog"))
        with open(os.path.join(self.repo, "backlog", "config.yml"), "w") as f:
            f.write("project_name: x\n")
        with mock.patch.dict(os.environ, {"HUB_DIRECTIONS": self.env["HUB_DIRECTIONS"]}):
            with mock.patch.object(platforms.current, "link_dir", side_effect=OSError("no permission")):
                with self.assertRaises(direction.Fail):
                    direction.new("billing", repo=self.repo, adopt=True)
            self.assertFalse(os.path.exists(self.backlog_dir("billing")))
            self.assertEqual(direction.new("billing", repo=self.repo, adopt=True), 0)   # the same command now succeeds

    def test_adopt_needs_a_backlog_in_the_repo(self):
        r = self.cli("new", "billing", "--repo", self.repo, "--adopt")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("has no ready backlog", r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.backlog_dir("billing")))

    def test_refuses_existing(self):
        self.cli("new", "sso", "--repo", self.repo)
        r = self.cli("new", "sso", "--repo", self.repo)
        self.assertEqual(r.returncode, 2)
        self.assertIn("already exists", r.stderr)

    def test_refuses_bad_slugs_without_touching_disk(self):
        for bad in ("../x", "a/b", "A", "x", "list", "new", "-x", "a" * 41,
                    f"{lang.word('words', 'hub')}-x"):   # Cyrillic letters
            r = self.cli("new", bad, "--repo", self.repo)
            self.assertEqual(r.returncode, 2, bad)
            self.assertIn("bad direction name", r.stderr, bad)
        self.assertFalse(os.path.exists(os.path.join(self.hub, "directions")))

    def test_the_utility_in_path_is_not_called(self):
        # the hub keeps the backlog itself; the backlog utility in PATH (someone may have it installed) is not called at all
        r = self.cli("new", "sso", "--repo", self.repo)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.cli("sso", "task", "create", "Slice 1").returncode, 0)
        self.assertEqual(self.cli("sso", "doc", "create", "Goal").returncode, 0)
        self.assertEqual(self.cli("sso", "task", "list").returncode, 0)
        self.assertEqual(self.cli("resume", "sso").returncode, 0)
        self.assertTrue(os.path.isfile(os.path.join(self.backlog_dir("sso"), "backlog", "config.yml")))
        self.assertEqual(self.calls(), [])
        self.assertTrue(os.path.exists(os.path.join(self.bin, "backlog")) or os.name == "nt")   # the watchdog was in place


class TestRelativeHubDir(DirectionCase):
    def test_relative_hub_dir_resolves_from_callers_cwd(self):
        r = self.cli("new", "sso", "--repo", self.repo, cwd=self.tmp, HUB_DIRECTIONS="relhub/directions")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "relhub", "directions", "sso", "backlog")))


class TestRepo(DirectionCase):
    def own(self, slug):
        return os.path.join(self.tmp, "state", "direction-repos", slug)

    def test_new_without_repo_gets_its_own_git_folder_with_a_commit(self):
        # owner 06.11: a new direction starts in a git folder of its own and is a mini-hub at once — the core can give
        # it a copy only of a repo with a commit (core/minihub.born); the hub used to put it in its own folder, not git
        r = self.cli("new", "sso")
        self.assertEqual(r.returncode, 0, r.stderr)
        own = self.own("sso")
        self.assertEqual(os.path.realpath(self.cli("where", "sso").stdout.strip()), os.path.realpath(own))
        self.assertEqual(os.path.realpath(git(own, "rev-parse", "--show-toplevel").stdout.strip()), os.path.realpath(own))
        self.assertEqual(git(own, "rev-parse", "--verify", "-q", "HEAD").returncode, 0)
        self.assertEqual(git(own, "status", "--porcelain").stdout, "")
        self.assertIn(own, r.stdout)
        self.assertTrue(os.path.isdir(os.path.join(self.backlog_dir("sso"), "backlog")))

    def test_own_folder_is_never_one_already_there(self):
        # a folder left by an archived or renamed direction stays its repo, and workspaces/<name> is a session's own
        # folder (core/workspace.py): a new direction takes the next free name and leaves both alone
        self.assertEqual(self.cli("new", "sso").returncode, 0)
        old = self.own("sso")
        with open(os.path.join(old, "notes.md"), "w") as f:
            f.write("notes\n")
        self.assertEqual(self.cli("rename", "sso", "sso-old").returncode, 0)
        r = self.cli("new", "sso")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(os.path.realpath(self.cli("where", "sso").stdout.strip()), os.path.realpath(self.own("sso-2")))
        self.assertEqual(os.path.realpath(self.cli("where", "sso-old").stdout.strip()), os.path.realpath(old))
        self.assertEqual(git(old, "status", "--porcelain").stdout, "?? notes.md\n")         # untouched
        session = os.path.join(self.tmp, "state", "workspaces", "liaison")
        os.makedirs(session)
        self.assertEqual(self.cli("new", "liaison").returncode, 0)
        self.assertNotEqual(git(session, "rev-parse", "--git-dir").returncode, 0)          # a session's folder stays

    def test_new_refuses_a_repo_without_commits(self):
        # the direction would work by itself there for good (no commits in the repo): say how to fix it before anything
        bare = os.path.join(self.tmp, "code", "fresh")
        subprocess.run(["git", "init", "-q", bare], check=True)
        r = self.cli("new", "sso", "--repo", bare)
        self.assertEqual(r.returncode, 2)
        self.assertIn("commit --allow-empty", r.stderr)
        self.assertIn("--repo", r.stderr)
        self.assertFalse(os.path.exists(self.backlog_dir("sso")))
        self.assertNotEqual(git(bare, "rev-parse", "--verify", "-q", "HEAD").returncode, 0)   # their repo is untouched

    def test_new_refuses_a_folder_that_is_not_git(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        r = self.cli("new", "sso", "--repo", plain)
        self.assertEqual(r.returncode, 2)
        self.assertIn("is not a git repository", r.stderr)
        self.assertFalse(os.path.exists(self.backlog_dir("sso")))

    def test_where_prints_repo_root_even_from_a_subfolder(self):
        self.assertEqual(self.cli("new", "sso", "--repo", os.path.join(self.repo, "apps")).returncode, 0)
        r = self.cli("where", "sso")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(os.path.realpath(r.stdout.strip()), os.path.realpath(self.repo))

    def test_repo_moves_an_existing_direction(self):
        self.cli("new", "sso", "--repo", self.repo)
        other = os.path.join(self.tmp, "code", "other")
        subprocess.run(["git", "init", "-q", other], check=True)
        self.assertEqual(self.cli("repo", "sso", other).returncode, 0)
        self.assertEqual(os.path.realpath(self.cli("where", "sso").stdout.strip()), os.path.realpath(other))

    def test_where_without_repo_says_how_to_set_it(self):
        self.cli("new", "sso", "--repo", self.repo)
        os.remove(os.path.join(self.backlog_dir("sso"), "direction.json"))
        r = self.cli("where", "sso")
        self.assertEqual(r.returncode, 2)
        self.assertIn("hub direction repo sso", r.stderr)

    def test_list_and_resume_show_repo(self):
        self.cli("new", "sso", "--repo", self.repo)
        self.assertIn("code/backend", self.cli("list").stdout)
        self.assertIn("repository:", self.cli("resume", "sso").stdout)
        self.assertIn("code/backend", self.cli("resume", "sso").stdout)


class TestDecide(DirectionCase):
    def test_decide_creates_a_decision_with_its_body(self):
        self.cli("new", "sso", "--repo", self.repo)
        r = self.cli("decide", "sso", "Web only is not allowed", "--context", "Authorization for apps as well",
                     "--decision", "We support web and native", "--consequences", "Clients for old versions are needed")
        self.assertEqual(r.returncode, 0, r.stderr)
        files = glob.glob(os.path.join(self.backlog_dir("sso"), "backlog", "decisions", "decision-*.md"))
        self.assertEqual(len(files), 1)
        text = open(files[0], encoding="utf-8").read()
        self.assertIn("title: Web only is not allowed", text)
        self.assertIn("## Context\n\nAuthorization for apps as well", text)
        self.assertIn("## Decision\n\nWe support web and native", text)
        self.assertIn("## Consequences\n\nClients for old versions are needed", text)

    def test_decide_numbers_after_existing_decisions(self):
        self.cli("new", "sso", "--repo", self.repo)
        folder = os.path.join(self.backlog_dir("sso"), "backlog", "decisions")
        with open(os.path.join(folder, "decision-9 - old.md"), "w", encoding="utf-8") as f:
            f.write("---\nid: decision-9\ntitle: old\n---\n")
        self.cli("decide", "sso", "New", "--decision", "yes")
        self.assertTrue(glob.glob(os.path.join(folder, "decision-10 - *.md")))

    def test_decide_requires_the_decision_text(self):
        self.cli("new", "sso", "--repo", self.repo)
        r = self.cli("decide", "sso", "No text")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--decision", r.stderr)


class TestCleanup(DirectionCase):
    def test_resume_lists_empty_decisions_to_fix(self):
        self.cli("new", "sso", "--repo", self.repo)
        folder = os.path.join(self.backlog_dir("sso"), "backlog", "decisions")
        with open(os.path.join(folder, "decision-1 - empty.md"), "w", encoding="utf-8") as f:
            f.write("---\nid: decision-1\ntitle: empty\n---\n## Context\n\n\n\n## Decision\n\n\n\n## Consequences\n\n")
        self.cli("decide", "sso", "Full", "--decision", "has text")
        out = self.cli("resume", "sso").stdout
        self.assertIn("-- cleanup --", out)
        self.assertIn("decision-1 - empty.md", out)
        self.assertNotIn("Full.md", out.split("-- cleanup --")[1])

    def test_resume_flags_template_left_above_the_body(self):
        self.cli("new", "sso", "--repo", self.repo)
        folder = os.path.join(self.backlog_dir("sso"), "backlog", "decisions")
        with open(os.path.join(folder, "decision-1 - doubled.md"), "w", encoding="utf-8") as f:
            f.write("---\nid: decision-1\ntitle: doubled\n---\n## Context\n\n\n## Decision\n\n\n## Consequences\n\n"
                    "## Context\ntext\n\n## Decision\ndecided\n\n## Consequences\nconsequences\n")
        out = self.cli("resume", "sso").stdout
        self.assertIn("decision-1 - doubled.md", out.split("-- cleanup --")[1])

    def test_no_cleanup_section_when_clean(self):
        self.cli("new", "sso", "--repo", self.repo)
        self.cli("decide", "sso", "Full", "--decision", "has text")
        self.assertNotIn("-- cleanup --", self.cli("resume", "sso").stdout)


class TestPassthrough(DirectionCase):
    def test_args_and_cwd_reach_the_backlog_of_the_direction(self):
        self.cli("new", "sso", "--repo", self.repo)
        r = self.cli("sso", "task", "create", "Slice 2: login by password", "--ac", "e2e green", cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stderr)
        (path,) = glob.glob(os.path.join(self.backlog_dir("sso"), "backlog", "tasks", "task-1 - *.md"))
        with open(path, encoding="utf-8") as f:
            self.assertIn("- [ ] #1 e2e green", f.read())

    def test_create_ends_with_the_new_id(self):
        """An incident: `… doc create | tail -1` gave a path without an id — the session took the wrong doc and overwrote the goal doc."""
        self.cli("new", "sso", "--repo", self.repo)
        r = self.cli("sso", "doc", "create", "Requirements")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip().splitlines()[-1], "created: doc-1")
        self.assertIn("Path: backlog/docs/doc-1", r.stdout)

    def test_exit_code_is_passed_through(self):
        self.cli("new", "sso", "--repo", self.repo)
        r = self.cli("sso", "task", "view", "9")
        self.assertEqual(r.returncode, 1)

    def test_unknown_slug_lists_existing(self):
        self.cli("new", "sso", "--repo", self.repo)
        r = self.cli("billing", "task", "list")
        self.assertEqual(r.returncode, 2)
        self.assertIn("no direction «billing»", r.stderr)
        self.assertIn("sso", r.stderr)

    def test_no_args_prints_usage(self):
        r = self.cli()
        self.assertEqual(r.returncode, 2)
        self.assertIn("hub direction new <slug>", r.stderr)


class TestList(DirectionCase):
    def test_empty_when_no_directions(self):
        r = self.cli("list")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no directions", r.stdout)

    def test_counts_statuses_and_skips_archive(self):
        self.cli("new", "sso", "--repo", self.repo)
        tasks = os.path.join(self.backlog_dir("sso"), "backlog", "tasks")
        for i, status in enumerate(("To Do", "To Do", "In Progress"), 1):
            with open(os.path.join(tasks, f"task-{i} - t.md"), "w", encoding="utf-8") as f:
                f.write(f"---\nid: TASK-{i}\ntitle: t\nstatus: {status}\n---\n")
        os.makedirs(os.path.join(self.hub, "directions", "_archive", "zzarchived", "backlog"))
        r = self.cli("list")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("sso", r.stdout)
        self.assertIn("To Do: 2", r.stdout)
        self.assertIn("In Progress: 1", r.stdout)
        self.assertNotIn("zzarchived", r.stdout)


class TestArchive(DirectionCase):
    def test_moves_to_archive(self):
        self.cli("new", "sso", "--repo", self.repo)
        r = self.cli("archive", "sso")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(self.backlog_dir("sso")))
        self.assertTrue(os.path.isdir(os.path.join(self.hub, "directions", "_archive", "sso", "backlog")))

    def test_does_not_overwrite_older_archive(self):
        self.cli("new", "sso", "--repo", self.repo)
        self.cli("archive", "sso")
        self.cli("new", "sso", "--repo", self.repo)
        r = self.cli("archive", "sso")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.cli("new", "sso", "--repo", self.repo)
        self.assertEqual(self.cli("archive", "sso").returncode, 0)
        archived = sorted(os.listdir(os.path.join(self.hub, "directions", "_archive")))
        self.assertEqual(archived, ["sso", "sso-2", "sso-3"])
        for name in archived:
            self.assertIn("backlog", os.listdir(os.path.join(self.hub, "directions", "_archive", name)))


class TestResume(DirectionCase):
    def write(self, slug, sub, name, text):
        folder = os.path.join(self.backlog_dir(slug), "backlog", sub)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
            f.write(text)

    def test_docs_tasks_and_last_five_decisions(self):
        self.cli("new", "sso", "--repo", self.repo)
        self.cli("sso", "task", "create", "Slice 1")
        self.write("sso", "docs", "doc-1 - Goal.md", "All logins go through the new SSO; not only web.")
        for n in range(1, 8):
            self.write("sso", "decisions", f"decision-{n} - d{n}.md", f"decision number {n}")
        r = self.cli("resume", "sso")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout
        self.assertIn("== direction sso ==", out)
        self.assertIn("not only web", out)
        self.assertIn("TASK-1 - Slice 1", out)
        for n in (3, 4, 5, 6, 7):
            self.assertIn(f"decision number {n}", out)
        for n in (1, 2):
            self.assertNotIn(f"decision number {n}\n", out)
        self.assertLess(out.index("decision number 3"), out.index("decision number 7"))

    def test_in_progress_task_details_are_shown(self):
        self.cli("new", "sso", "--repo", self.repo)
        self.write("sso", "tasks", "task-3 - Login.md", "---\nid: TASK-3\ntitle: Login\nstatus: In Progress\n---\n")
        self.write("sso", "tasks", "task-4 - Later.md", "---\nid: TASK-4\ntitle: Later\nstatus: To Do\n---\n")
        out = self.cli("resume", "sso").stdout
        self.assertIn("Task TASK-3 - Login", out)
        self.assertNotIn("Task TASK-4 -", out)

    def test_decision_ten_sorts_after_nine(self):
        self.cli("new", "sso", "--repo", self.repo)
        for n in (2, 9, 10):
            self.write("sso", "decisions", f"decision-{n} - d.md", f"decision number {n}")
        out = self.cli("resume", "sso").stdout
        self.assertLess(out.index("decision number 9"), out.index("decision number 10"))

    def test_empty_backlog_says_what_to_do(self):
        self.cli("new", "sso", "--repo", self.repo)
        shutil.rmtree(os.path.join(self.backlog_dir("sso"), "backlog", "docs"))
        shutil.rmtree(os.path.join(self.backlog_dir("sso"), "backlog", "decisions"))
        r = self.cli("resume", "sso")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no documents", r.stdout)
        self.assertIn("no decisions", r.stdout)

    def test_unknown_slug(self):
        r = self.cli("resume", "nope")
        self.assertEqual(r.returncode, 2)
        self.assertIn("no direction", r.stderr)


class TestModule(DirectionCase):
    """The module in the hub: the `hub direction` command and the skill in the catalog as the `directions` tool."""

    def test_hub_cli(self):
        hub = os.path.join(REPO, "bin", "hub")
        r = subprocess.run([sys.executable, hub, "direction", "new", "sso", "--repo", self.repo],
                           env={**self.env, "HUB_STATE_DIR": self.tmp}, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run([sys.executable, hub, "direction", "list"], env={**self.env, "HUB_STATE_DIR": self.tmp},
                           capture_output=True, text=True)
        self.assertIn("sso", r.stdout)

    def test_help_after_the_slug_is_the_backlog_help(self):
        # eval finding: the skill says «--help — the list», but bin/hub caught --help as the second word and printed the general help
        self.as_hub("new", "sso", "--repo", self.repo)
        general = self.as_hub("--help").stdout
        for args in (("sso", "--help"), ("sso", "task", "--help"), ("sso", "-h")):
            r = self.as_hub(*args)
            self.assertEqual(r.returncode, 0, (args, r.stderr))
            self.assertIn("task edit", r.stdout, args)
            self.assertNotEqual(r.stdout, general, args)
        self.assertNotIn("task edit", general)   # without a direction — the help of hub direction, as before
        for args in (("nope", "--help"), ("Bad_Name", "-h")):
            r = self.as_hub(*args)
            self.assertEqual((r.returncode, r.stdout), (0, general), args)
        r = self.as_hub("list", "--help")   # a subcommand of hub direction: its own help, not the backlog's
        self.assertEqual(r.returncode, 0)
        self.assertIn("hub direction list", r.stdout)
        self.assertNotIn("task edit", r.stdout)

    def as_hub(self, *args, me="hub"):
        from core import journal
        if not os.path.exists(os.path.join(self.tmp, "journal.jsonl")):
            j, owner = journal.Journal(os.path.join(self.tmp, "journal.jsonl")), {"channel": "cli", "owner": True}
            j.append("session.requested", "hub", {"cwd": "/h", "session_id": "s-h", "kind": "hub", **owner})
            j.append("session.requested", "fixer", {"cwd": "/w", "session_id": "s-w", **owner})
        env = {**self.env, "HUB_STATE_DIR": self.tmp, "HUB_CLI_WAIT": "0", "HUB_SESSION": me}
        return subprocess.run([sys.executable, os.path.join(REPO, "bin", "hub"), "direction", *args], env=env,
                              capture_output=True, text=True)

    def backlog_events(self):
        from core import spool
        taken = spool.take(os.path.join(self.tmp, "spool"))
        for path, _ in taken:   # what was read goes away, as the core does it
            os.remove(path)
        return [e["data"] for _, e in taken if e["type"] == "task.backlog"]

    def test_the_hub_takes_a_lead_off_a_direction(self):
        # 2026-11-06: no way to take a backlog off a session that had taken it by mistake
        self.as_hub("new", "payments", "--repo", self.repo)
        self.backlog_events()
        r = self.as_hub("lead", "payments", "--drop", "fixer")
        self.assertEqual(r.returncode, 0, r.stderr)
        from core import spool
        taken = spool.take(os.path.join(self.tmp, "spool"))
        (ev,) = [e["data"] for _, e in taken if e["type"] == "task.backlog_dropped"]
        self.assertEqual((ev["slug"], ev["session"], ev["slugs"]), ("payments", "fixer", ["payments"]))
        self.assertEqual([e["type"] for _, e in taken if e["type"].startswith("task.backlog")], ["task.backlog_dropped"])
        from core import i18n
        self.assertNotIn(i18n.t("cli.direction.lead_dropped", name="fixer", slug="payments"), r.stdout)   # the core has not answered
        for path, _ in taken:
            os.remove(path)
        for args in (("lead", "payments", "--drop"), ("lead", "payments", "fixer", "--drop"),
                     ("lead", "payments", "--drop", "fixer", "extra")):   # a usage error puts nothing into the spool
            r = self.as_hub(*args)
            self.assertNotEqual(r.returncode, 0, args)
            self.assertEqual(spool.take(os.path.join(self.tmp, "spool")), [], args)
        r = self.as_hub("lead", "nope", "--drop", "fixer")
        self.assertNotEqual(r.returncode, 0)

    def test_the_hub_names_whose_direction_it_is(self):
        # the hub created a direction for the work of executor fixer without naming it — the backlog waited for a
        # session «payments» that did not exist, while the hub saw «direction payments: …» and considered everything done
        r = self.as_hub("new", "payments", "--repo", self.repo)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no session payments", r.stdout)   # the hub's command: in English
        self.assertIn("hub direction lead payments <session>", r.stdout)
        self.assertEqual(self.backlog_events()[0]["slug"], "payments")
        r = self.as_hub("lead", "payments", "fixer")
        self.assertEqual(r.returncode, 0, r.stderr)
        (ev,) = self.backlog_events()
        self.assertEqual((ev["slug"], ev["session"]), ("payments", "fixer"))
        r = self.as_hub("new", "billing", "--repo", self.repo, "--session", "fixer")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("no session", r.stdout)
        (ev,) = self.backlog_events()
        self.assertEqual((ev["slug"], ev["session"]), ("billing", "fixer"))
        r = self.as_hub("lead", "nope", "fixer")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.backlog_events(), [])
        self.assertEqual(self.as_hub("new", "ops", "--repo", self.repo, me="fixer").returncode, 0)   # its own — as before
        self.assertEqual([(e["slug"], e.get("session")) for e in self.backlog_events()], [("ops", None)])

    def test_a_bare_lead_is_a_question_and_records_nothing_from_any_caller(self):
        # `hub direction lead <slug>` without a session name once recorded a hand-off: the hub, asking who leads seven
        # directions, handed each of them to a session named like the slug; a plain session made itself the lead
        from core import journal
        for slug in ("payments", "billing"):
            self.assertEqual(self.as_hub("new", slug, "--repo", self.repo).returncode, 0)
        self.as_hub("lead", "payments", "fixer")
        j = journal.Journal(os.path.join(self.tmp, "journal.jsonl"))
        for ev in self.backlog_events():   # the core recorded them
            j.append("task.backlog", None, ev)
        for me in ("hub", "fixer"):
            r = self.as_hub("lead", "payments", me=me)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("payments", r.stdout)
            self.assertIn("fixer", r.stdout)
            self.assertEqual(self.backlog_events(), [], me)
        r = self.as_hub("lead", "billing", me="fixer")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("fixer", r.stdout)   # nobody leads it, and asking did not make the asker the lead
        self.assertEqual(self.backlog_events(), [])
        self.assertEqual(self.as_hub("lead", "nope").returncode, 2)
        self.assertEqual(self.backlog_events(), [])
        r = self.as_hub("lead", "payments", "fixer")   # handing over by name is as before
        self.assertEqual(r.returncode, 0, r.stderr)
        (ev,) = self.backlog_events()
        self.assertEqual((ev["slug"], ev["session"]), ("payments", "fixer"))
        # a renamed direction: the session keeps the name it took the direction under, and it is still its lead
        self.assertEqual(self.as_hub("rename", "payments", "pay").returncode, 0)
        self.backlog_events()
        for name in ("pay", "payments"):
            r = self.as_hub("lead", name, me="hub")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("fixer", r.stdout, name)
        self.assertEqual(self.backlog_events(), [])
        r = self.as_hub("lead", "--drop")   # not a slug: the usage of --drop, nothing recorded
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.backlog_events(), [])

    def test_the_hub_texts_name_the_session_of_the_direction(self):
        # the core's ONBOARD said to ask the executor, the hub's CLAUDE.md said to create it without naming the session
        from core.executors import ONBOARD
        self.assertIn("--session {name}", ONBOARD)
        for rel in ("hub-home/CLAUDE.md", "plugins/hub-coordinator/skills/fleet/SKILL.md"):
            with open(os.path.join(REPO, rel), encoding="utf-8") as f:
                self.assertIn("--session <its name>", f.read(), rel)

    def test_modules_card_says_the_backlog_is_the_hubs_own(self):
        # the hub does not call the Backlog.md utility; the «Directions» card neither refers to it nor names a path to it
        from modules.web import modinfo
        card = next(m for m in modinfo.modules(self.tmp, {}, {"sessions": []}) if m["id"] == "directions")
        self.assertEqual(card["state"], "on")
        self.assertIn("built into the hub", card["detail"])
        self.assertNotIn("npm", card["detail"])

    def test_skill_in_catalog(self):
        from core import catalog
        tool = catalog.build({})["directions"]
        self.assertEqual((tool["kind"], tool["risk"]), ("plugin", []))
        self.assertTrue(os.path.exists(os.path.join(tool["path"], "skills", "direction", "SKILL.md")))


SESSION_STEPS = (   # what direction sessions actually call (hub journal: task edit — 96% of calls)
    ("task", "create", "Slice 1: login", "--ac", "login works", "--ac", "the test is green", "-d", "what and why"),
    ("task", "create", "Second",),
    ("task", "edit", "1", "--status", "In Progress", "--append-notes", "did A"),
    ("task", "edit", "1", "--append-notes", "did B", "--check-ac", "1"),
    ("task", "edit", "2", "-s", "Done", "--notes", "closed: moved into slice 1"),
    ("doc", "create", "Goal"),
    ("doc", "update", "doc-1", "--content", "Goal: login by key.\nInvariant: do not touch prod."),
)


class TestBuiltinBacklog(DirectionCase):
    """The hub keeps a direction's backlog itself (first when the utility was absent, now always), in Backlog.md-format files."""

    def setUp(self):
        super().setUp()
        self.assertEqual(self.cli("new", "sso", "--repo", self.repo).returncode, 0)

    def run_steps(self):
        for step in SESSION_STEPS:
            r = self.cli("sso", *step)
            self.assertEqual(r.returncode, 0, (step, r.stderr))
        return r

    def test_a_blank_document_text_is_refused_and_the_document_stays(self):
        # a failed script gave an empty `$(cat …)` and `doc update --content ""` wiped the document
        self.cli("sso", "doc", "create", "Goal", "--content", "Goal: login by key.")
        (path,) = glob.glob(os.path.join(self.backlog_dir("sso"), "backlog", "docs", "doc-1 - *.md"))
        with open(path, encoding="utf-8") as h:
            before = h.read()
        for blank in ("", "   ", "\n\n", " \t\n"):
            for step in (("doc", "update", "doc-1", "--content", blank), ("doc", "update", "doc-1", "-c=" + blank),
                         ("doc", "update", "doc-1", "--title", "Renamed", "--content", blank),
                         ("doc", "create", "Second", "--content", blank)):
                r = self.cli("sso", *step)
                self.assertEqual(r.returncode, 2, (step, r.stdout, r.stderr))
                self.assertIn("is empty", r.stderr)
            with open(path, encoding="utf-8") as h:
                self.assertEqual(h.read(), before)   # not the text, not the title, not updated_date
            self.assertEqual(glob.glob(os.path.join(os.path.dirname(path), "doc-2 - *.md")), [])
        # no --content at all: the title alone is still an update, a document without a text is still a create
        self.assertEqual(self.cli("sso", "doc", "update", "doc-1", "--title", "Renamed").returncode, 0)
        self.assertEqual(self.cli("sso", "doc", "create", "Second").returncode, 0)

    def test_a_session_leads_the_direction_without_the_utility(self):
        r = self.cli("sso", "task", "create", "Slice 1: login", "--ac", "login works")
        self.assertIn("Created task TASK-1", r.stdout)
        self.assertEqual(r.stdout.strip().splitlines()[-1], "created: TASK-1")
        shutil.rmtree(os.path.join(self.backlog_dir("sso"), "backlog", "tasks"))
        os.makedirs(os.path.join(self.backlog_dir("sso"), "backlog", "tasks"))
        self.run_steps()
        base = os.path.join(self.backlog_dir("sso"), "backlog")
        (one,) = glob.glob(os.path.join(base, "tasks", "task-1 - *.md"))
        self.assertEqual(os.path.basename(one), "task-1 - Slice-1-login.md")
        with open(one, encoding="utf-8") as h:
            text = h.read()
        self.assertIn("title: 'Slice 1: login'\nstatus: In Progress\n", text)
        self.assertIn("<!-- AC:BEGIN -->\n- [x] #1 login works\n- [ ] #2 the test is green\n<!-- AC:END -->", text)
        self.assertIn("<!-- SECTION:NOTES:BEGIN -->\ndid A\n\ndid B\n<!-- SECTION:NOTES:END -->", text)
        self.assertIn("<!-- SECTION:DESCRIPTION:BEGIN -->\nwhat and why\n<!-- SECTION:DESCRIPTION:END -->", text)
        lst = self.cli("sso", "task", "list", "--plain").stdout
        self.assertEqual(lst.strip(), "In Progress:\n  TASK-1 - Slice 1: login\n\nDone:\n  TASK-2 - Second")
        self.assertEqual(self.cli("sso", "task", "list", "--plain", "--exclude-status", "Done").stdout.strip(),
                         "In Progress:\n  TASK-1 - Slice 1: login")
        view = self.cli("sso", "task", "view", "TASK-1", "--plain").stdout
        self.assertIn("Task TASK-1 - Slice 1: login", view)
        self.assertIn("- [x] #1 login works", view)
        self.assertIn("did B", view)
        out = self.cli("resume", "sso").stdout
        self.assertIn("Invariant: do not touch prod.", out)
        self.assertIn("-- in progress: TASK-1 --", out)
        self.assertIn("sso — Done: 1, In Progress: 1", self.cli("list").stdout)
        self.assertEqual(self.cli("sso", "doc", "list", "--plain").stdout.strip(), "doc-1 - Goal")
        self.assertEqual(self.cli("sso", "task", "archive", "2").returncode, 0)
        self.assertTrue(glob.glob(os.path.join(base, "archive", "tasks", "task-2 - *.md")))
        self.assertEqual(self.cli("sso", "task", "create", "Third").stdout.splitlines()[0], "Created task TASK-3")

    def test_what_it_cannot_do_it_says_so(self):
        r = self.cli("sso", "milestone", "list")
        self.assertEqual(r.returncode, 2)
        self.assertIn("task create", r.stderr)
        self.assertNotIn("npm", r.stderr)
        r = self.cli("sso", "task", "edit", "9", "--status", "Done")
        self.assertEqual(r.returncode, 1)
        self.assertIn("9", r.stderr)
        r = self.cli("sso", "task", "edit", "1", "--bogus", "x")
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("npm", r.stderr)

    def test_commands_that_sessions_try_by_habit(self):
        # hub journal: sessions call --help, `task show`/`doc show`, `decision list`, --version — it used to answer «cannot do»
        self.run_steps()
        for args in (("--help",), ("task", "--help"), ("task", "edit", "--help"), ("doc", "create", "--help"),
                     ("doc", "-h"), ("--version",)):
            r = self.cli("sso", *args)
            self.assertEqual(r.returncode, 0, (args, r.stderr))
            self.assertTrue(r.stdout.strip(), args)
        self.assertIn("task edit", self.cli("sso", "--help").stdout)
        self.assertEqual(self.cli("sso", "task", "show", "TASK-1").stdout, self.cli("sso", "task", "view", "1").stdout)
        self.assertEqual(self.cli("sso", "doc", "show", "1").stdout, self.cli("sso", "doc", "view", "doc-1").stdout)
        self.assertIn("Invariant: do not touch prod.", self.cli("sso", "doc", "show", "1").stdout)
        self.assertIn("no decisions", self.cli("sso", "decision", "list", "--plain").stdout)
        self.assertEqual(self.cli("decide", "sso", "Schema", "--decision", "so").returncode, 0)
        self.assertEqual(self.cli("sso", "decision", "list").stdout.strip(), "decision-1 - Schema")
        self.assertEqual(self.cli("sso", "decision", "view", "1").returncode, 0)
        self.assertEqual(self.cli("sso", "decision", "view", "7").returncode, 1)
        with open(os.path.join(self.backlog_dir("sso"), "backlog", "decisions", "decision-draft.md"), "w") as f:
            f.write("no number\n")
        self.assertEqual(self.cli("sso", "decision", "list").returncode, 0)   # a file without a number does not break the list

    def test_doc_type_flag_is_the_type_not_the_title(self):
        self.assertEqual(self.cli("sso", "doc", "create", "Goal", "-t", "guide").returncode, 0)
        (path,) = glob.glob(os.path.join(self.backlog_dir("sso"), "backlog", "docs", "doc-1 - *.md"))
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("title: Goal\ntype: guide\n", text)

    def test_new_that_cannot_write_leaves_no_empty_direction(self):
        from unittest import mock
        import importlib.util
        spec = importlib.util.spec_from_file_location("direction_inproc2", SCRIPT)
        direction = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(direction)
        with mock.patch.dict(os.environ, {"HUB_DIRECTIONS": os.path.join(self.tmp, "d2")}), \
                mock.patch("modules.directions.minibacklog.init", side_effect=OSError("read only")):
            with self.assertRaises(direction.Fail):
                direction.new("ops", repo=self.repo)
            self.assertFalse(os.path.exists(os.path.join(self.tmp, "d2", "ops")))


class BuiltinBacklogTwoWriters(unittest.TestCase):
    """Sessions of one direction call the backlog at the same time."""

    def test_two_tasks_created_at_once_get_different_numbers(self):
        # the next number is the largest on disk + 1: two creates read one folder and both wrote task-1. The first create
        # waits here (up to a second) to see whether the second takes the same number
        import contextlib
        import io
        import threading
        from unittest import mock
        from modules.directions import minibacklog
        cwd = tempfile.mkdtemp(prefix="backlog-race-")
        self.addCleanup(shutil.rmtree, cwd, True)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(minibacklog.main(["init", "race"], cwd), 0)
        real, picked, both = minibacklog.Backlog.next_id, threading.Event(), threading.Event()
        first, codes = [], []

        def next_id(self, *a, **kw):
            n = real(self, *a, **kw)
            if not first:
                first.append(n)
                picked.set()
                both.wait(1)
            else:
                both.set()
            return n

        def create(title):
            codes.append(minibacklog.main(["task", "create", title], cwd))
        with mock.patch.object(minibacklog.Backlog, "next_id", next_id), contextlib.redirect_stdout(io.StringIO()):
            a = threading.Thread(target=create, args=("first",))
            a.start()
            self.assertTrue(picked.wait(10))
            b = threading.Thread(target=create, args=("second",))
            b.start()
            a.join(10)
            b.join(10)
        self.assertEqual(codes, [0, 0])
        names = sorted(os.path.basename(f) for f in glob.glob(os.path.join(cwd, "backlog", "tasks", "*.md")))
        self.assertEqual(names, ["task-1 - first.md", "task-2 - second.md"])
        # the lock is in the hub's folder, not in the direction's repo: there it would be an extra file in git
        self.assertEqual(sorted(os.listdir(cwd)), ["backlog"])
        state = os.environ["HUB_STATE_DIR"]
        self.assertTrue(glob.glob(os.path.join(state, "locks", "backlog-*.lock")))

    def test_two_decisions_recorded_at_once_get_different_numbers(self):
        # the same for decisions: two `hub decided` read one folder and both wrote decision-1
        import contextlib
        import io
        import threading
        from unittest import mock
        from modules.directions import direction
        root = tempfile.mkdtemp(prefix="decide-race-")
        self.addCleanup(shutil.rmtree, root, True)
        os.makedirs(os.path.join(root, "dd", "backlog"))
        folder = os.path.join(root, "dd", "backlog", "decisions")
        real, listed, both = os.listdir, threading.Event(), threading.Event()
        first = []

        def listdir(path="."):
            names = real(path)
            if path == folder:
                if not first:
                    first.append(1)
                    listed.set()
                    both.wait(1)
                else:
                    both.set()
            return names

        def decide(title):
            direction.decide("dd", [title, "--decision", "yes"])
        with mock.patch.dict(os.environ, {"HUB_DIRECTIONS": root}), \
                mock.patch.object(direction.os, "listdir", listdir), contextlib.redirect_stdout(io.StringIO()):
            a = threading.Thread(target=decide, args=("first",))
            a.start()
            self.assertTrue(listed.wait(10))
            b = threading.Thread(target=decide, args=("second",))
            b.start()
            a.join(10)
            b.join(10)
        self.assertEqual(sorted(real(folder)), ["decision-1 - first.md", "decision-2 - second.md"])
        self.assertEqual(sorted(real(os.path.join(root, "dd"))), ["backlog"])

    def test_the_task_list_and_the_decisions_take_the_same_lock(self):
        # tasks and decisions are one direction folder: one lock, by the real path (a link to it is the same lock)
        from modules.directions import minibacklog
        cwd = tempfile.mkdtemp(prefix="backlog-lock-")
        self.addCleanup(shutil.rmtree, cwd, True)
        link = cwd + "-link"
        import platforms
        platforms.current.link_dir(cwd, link)   # Windows without the right to create links — a junction
        self.addCleanup(os.rmdir if os.name == "nt" else os.unlink, link)   # on Windows a link to a folder is removed with rmdir
        self.assertEqual(minibacklog.lock_path(cwd), minibacklog.lock_path(link))
        self.assertNotEqual(minibacklog.lock_path(cwd), minibacklog.lock_path(tempfile.gettempdir()))


class TestRename(DirectionCase):
    """Renaming a direction (growth-plan to growth-profit): the command did not exist."""

    def test_rename_moves_the_backlog_and_keeps_repo_and_board_port(self):
        self.cli("new", "growth-plan", "--repo", self.repo)
        old = self.backlog_dir("growth-plan")
        os.makedirs(os.path.join(old, "backlog"), exist_ok=True)
        with open(os.path.join(old, "backlog", "config.yml"), "w") as f:
            f.write('project_name: "growth-plan"\nstatuses: ["To Do", "Done"]\n')
        with open(os.path.join(old, "direction.json")) as f:
            meta = {**json.load(f), "board_port": 6421}
        with open(os.path.join(old, "direction.json"), "w") as f:
            json.dump(meta, f)
        r = self.cli("rename", "growth-plan", "growth-profit")
        self.assertEqual(r.returncode, 0, r.stderr)
        new = self.backlog_dir("growth-profit")
        self.assertFalse(os.path.exists(old))
        with open(os.path.join(new, "direction.json")) as f:
            self.assertEqual(json.load(f), {**meta, "was": ["growth-plan"]})  # the repository and the board port are the same
        # the old name stays an address: sessions and schedule jobs remember it in their texts
        r2 = self.cli("resume", "growth-plan")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertIn("== direction growth-profit ==", r2.stdout)
        with open(os.path.join(new, "backlog", "config.yml")) as f:
            self.assertEqual(f.read(), 'project_name: "growth-profit"\nstatuses: ["To Do", "Done"]\n')
        self.assertIn("hub session rename growth-plan growth-profit", r.stdout)  # the core renames the session
        self.assertIn("growth-profit", self.cli("list").stdout)
        self.assertEqual(os.path.realpath(self.cli("where", "growth-profit").stdout.strip()),
                         os.path.realpath(self.repo))

    def test_refuses_missing_taken_and_bad_names(self):
        self.cli("new", "aaa", "--repo", self.repo)
        self.cli("new", "bbb", "--repo", self.repo)
        for args in (("rename", "nope", "ccc"), ("rename", "aaa", "bbb"), ("rename", "aaa", "Bad Name"),
                     ("rename", "aaa", "list"), ("rename", "aaa")):
            r = self.cli(*args)
            self.assertEqual(r.returncode, 2, args)
        self.assertTrue(os.path.isdir(self.backlog_dir("aaa")))


if __name__ == "__main__":
    unittest.main()


class StatusCase(DirectionCase):
    """A session wrote `-s done` (journal 24.10): the board showed two columns, «Done» and «done», and the counters did not
    count the card as closed. The status is the spelling from config.yml's `statuses` — on write and on read; a status outside
    the list stays as written."""

    def setUp(self):
        super().setUp()
        self.assertEqual(self.cli("new", "sso", "--repo", self.repo).returncode, 0)
        self.tasks = os.path.join(self.backlog_dir("sso"), "backlog", "tasks")

    def status_in_file(self, n):
        (path,) = glob.glob(os.path.join(self.tasks, f"task-{n} - *.md"))
        with open(path, encoding="utf-8") as f:
            return re.search(r"^status: (.*)$", f.read(), re.M).group(1)

    def write_card(self, n, status):
        with open(os.path.join(self.tasks, f"task-{n} - Old.md"), "w", encoding="utf-8") as f:
            f.write(f"---\nid: TASK-{n}\ntitle: Old\nstatus: {status}\n---\n")

    def test_write_takes_the_spelling_from_the_list(self):
        self.cli("sso", "task", "create", "One", "-s", "in progress")
        self.cli("sso", "task", "create", "Two")
        self.assertEqual(self.status_in_file(1), "In Progress")
        for given, want in (("done", "Done"), ("DONE", "Done"), (" done ", "Done"), ("to do", "To Do")):
            self.assertEqual(self.cli("sso", "task", "edit", "2", "-s", given).returncode, 0)
            self.assertEqual(self.status_in_file(2), want, given)

    def test_a_status_outside_the_list_is_written_as_given(self):
        self.cli("sso", "task", "create", "One")
        self.cli("sso", "task", "edit", "1", "-s", "Blocked")
        self.assertEqual(self.status_in_file(1), "Blocked")

    def test_the_list_of_the_direction_is_used(self):
        with open(os.path.join(self.backlog_dir("sso"), "backlog", "config.yml"), "w") as f:
            f.write('statuses: ["To Do", "Review", "Done"]\n')
        self.cli("sso", "task", "create", "One", "-s", "review")
        self.assertEqual(self.status_in_file(1), "Review")

    def test_a_card_written_in_lower_case_reads_as_done(self):
        from modules.directions import direction
        self.write_card(1, "Done")
        self.write_card(2, "done")
        self.write_card(3, "in progress")
        path = self.backlog_dir("sso")
        self.assertEqual([c["status"] for c in sorted(direction.cards(path), key=lambda c: c["id"])],
                         ["Done", "Done", "In Progress"])
        self.assertEqual(direction.status_counts(path), {"Done": 2, "In Progress": 1})
        lst = self.cli("sso", "task", "list", "--plain").stdout
        self.assertEqual(lst.count("Done:"), 1)
        self.assertNotIn("\ndone:", "\n" + lst)
        resume = self.cli("resume", "sso").stdout
        self.assertIn("-- in progress: TASK-3 --", resume)
        self.assertNotIn("TASK-2", resume.split("-- in progress")[0])

    def test_a_list_with_the_default_statuses_in_another_case_keeps_the_cores_spelling(self):
        # review of PR #343: the core counts "Done"/"In Progress" by their spelling; a config.yml spelling them in lower
        # case turned every "Done" card into "done" — open for the core
        from modules.directions import direction
        with open(os.path.join(self.backlog_dir("sso"), "backlog", "config.yml"), "w") as f:
            f.write('statuses: ["to do", "in progress", "done", "review", "Done"]\n')
        self.write_card(1, "Done")
        self.cli("sso", "task", "create", "Two", "-s", "done")
        self.cli("sso", "task", "create", "Three", "-s", "REVIEW")
        self.assertEqual(self.status_in_file(2), "Done")
        self.assertEqual(self.status_in_file(3), "review")
        self.assertEqual(direction.status_counts(self.backlog_dir("sso")), {"Done": 2, "review": 1})
        from modules.directions import minibacklog
        self.assertEqual(minibacklog.statuses_of(os.path.join(self.backlog_dir("sso"), "backlog")),
                         ["To Do", "In Progress", "Done", "review"])   # one column per status

    def test_a_blank_status_does_not_empty_the_card(self):
        # review of PR #343: `-s " "` wrote `status: ` with no value — the card fell out of every column
        self.cli("sso", "task", "create", "One", "-s", "in progress")
        self.assertEqual(self.cli("sso", "task", "edit", "1", "-s", " ").returncode, 0)
        self.assertEqual(self.status_in_file(1), "In Progress")


class FoldedField(unittest.TestCase):
    def test_a_long_title_folded_over_lines_is_read_whole(self):
        # a board task title folded in YAML (title: >-) was read as «>-» — in the direction thread and on the «Board»
        from modules.directions import direction
        text = "---\nid: TASK-13\ntitle: >-\n  Editor role on stage — after\n  registration\nstatus: To Do\n---\n"
        self.assertEqual(direction.field(text, "title"), "Editor role on stage — after registration")
        self.assertEqual(direction.field(text, "status"), "To Do")
        self.assertEqual(direction.field("title: 'Slice 2'\n", "title"), "Slice 2")
