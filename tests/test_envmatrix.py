"""tools/envmatrix.py: the suite in the environments of other people's machines — a clean copy of the commit, a home folder
with decoys that the tests must not touch, the updater's environment and the hostile one; one Python (the one that runs it:
a hub from the install line carries its own); the run itself goes through the copy's own tools/test.py --no-lock."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

import platforms
from tests.fakes import can

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tools"))
import envmatrix  # noqa: E402

# the copy's runner in these tests: says how it was started and what it saw, and does what FAKE_MODE asks
RUNNER = r'''import json, os, sys
mode = os.environ.get("FAKE_MODE", "ok")
seen = {"argv": sys.argv[1:], "umask": oct(os.umask(0o022)), "home": os.path.expanduser("~"),
        "env": {k: os.environ.get(k) for k in ("HUB_SESSION", "AGENTS_HUB_SELF_CHECK_BROWSER", "LANG", "TZ", "HTTPS_PROXY",
                                               "HUB_TEST_CHROME", "HUB_TEST_CLAUDE", "CLAUDECODE", "GIT_DIR")}}
print("SEEN " + json.dumps(seen))
if mode == "touch":   # a test that writes into the owner's Claude settings
    with open(os.path.join(os.path.expanduser("~"), ".claude.json"), "a") as f:
        f.write("x")
if mode == "litter":
    os.makedirs(os.path.join(os.path.expanduser("~"), ".cache", "x"), exist_ok=True)
    open(os.path.join(os.path.expanduser("~"), ".cache", "x", "left"), "w").close()
if mode == "red":
    print("=" * 70)
    print("FAIL: test_a (tests.test_x.X.test_a)")
    print("-" * 70)
    print("AssertionError: 1 != 2")
    print("")
    print("-" * 70)
    print("Ran 2 tests in 0.1s")
    print("FAILED (chunks: 1)")
    sys.exit(1)
if mode == "smoke":   # a copy that is a published release: the runner checks only the smoke step
    print(os.environ["FAKE_SMOKE"] + ": the core code matches release v9.9.9")
    print("OK")
    sys.exit(0)
print("Ran 2 tests in 0.1s (1 chunks, 1 processes)")
print("OK")
'''


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def commit(root, files, message="c"):
    for rel, text in files.items():
        path = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="envmatrix-test-")
        self.addCleanup(platforms.current.rmtree, self.tmp)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q", "-b", "main")
        # the core's own check of the bus library (core/bus.crypto): here it works when FAKE_BUS is not "broken"
        bus = "import os\ndef crypto():\n    if os.environ.get('FAKE_BUS') == 'broken':\n        raise ImportError('built for another Python')\n"
        self.sha = commit(self.repo, {"tools/test.py": RUNNER, "core/a.py": "A = 1\n", "docs/private.md": "x\n",
                                      "evals/locale/ru/prompts/p.json": "{}\n",
                                      "core/__init__.py": "", "core/bus.py": bus})


class Tree(Base):
    def test_a_clone_of_the_commit_with_origin_pointing_nowhere(self):
        dest = envmatrix.tree(self.repo, self.sha, os.path.join(self.tmp, "hub"))
        self.assertEqual(git(dest, "rev-parse", "HEAD^{tree}"), git(self.repo, "rev-parse", self.sha + "^{tree}"))
        self.assertEqual(git(dest, "remote", "get-url", "origin"), envmatrix.NOWHERE)
        # no remote-tracking refs: the runner would otherwise take the copy for the published release and skip the suite
        self.assertEqual(git(dest, "for-each-ref", "refs/remotes"), "")
        self.assertEqual(git(dest, "status", "--porcelain"), "")   # no litter of a git hub's folder on top of the commit

    def test_what_only_a_git_hub_needs_is_gone(self):
        # a hub from the install line carries its own Python and is no clone of the shared repository: no profile of another
        # Python, no copy laid over the shared history, no litter of a person's clone
        self.assertEqual(sorted(envmatrix.PROFILES), ["browser", "claude", "hostile", "selfcheck"])
        for gone in ("DIRTY", "find_python", "minor"):
            self.assertFalse(hasattr(envmatrix, gone), gone)
        for flag in ("--upstream", "--python"):
            with self.assertRaises(SystemExit):
                envmatrix.main([flag, "x", "--repo", self.repo], say=lambda *a, **k: None, wait_turn=lambda **k: None)
        self.assertEqual(envmatrix.main(["--repo", self.repo, "--profiles", "py312"], say=lambda *a, **k: None,
                                        wait_turn=lambda **k: None), 2)


class Home(Base):
    def test_a_changed_or_removed_decoy_and_a_new_file_are_told_apart(self):
        at = os.path.join(self.tmp, "home")
        before = envmatrix.home(at)
        self.assertEqual(envmatrix.touched(at, before), {"changed": [], "removed": [], "added": []})
        with open(os.path.join(at, ".claude.json"), "a") as f:
            f.write("x")
        os.remove(os.path.join(at, ".hub-core", "journal.jsonl"))
        os.makedirs(os.path.join(at, ".cache"))
        open(os.path.join(at, ".cache", "new"), "w").close()
        self.assertEqual(envmatrix.touched(at, before), {"changed": [".claude.json"], "removed": [".hub-core/journal.jsonl"],
                                                          "added": [".cache/new"]})

    def test_the_hostile_home_signs_commits_and_has_hooks_that_refuse(self):
        at = os.path.join(self.tmp, "home")
        envmatrix.home(at, hostile=True)
        with open(os.path.join(at, ".gitconfig")) as f:
            text = f.read()
        self.assertIn("gpgsign = true", text)
        self.assertIn("hooksPath = ~/.githooks", text)
        self.assertTrue(os.path.isfile(os.path.join(at, ".githooks", "pre-commit")))


class Environment(Base):
    BASE = {"PATH": os.environ.get("PATH", ""), "HUB_SESSION": "live", "HUB_TEST_CHROME": "/x/chrome", "CLAUDECODE": "1",
            "GIT_DIR": "/elsewhere/.git", "XDG_CONFIG_HOME": "/real/home/.config", "TMPDIR": "/real/tmp", "LANG": "ru_RU.UTF-8"}

    def env(self, name, **kw):
        return envmatrix.environment(envmatrix.PROFILES[name], self.BASE, "/h", "/t", sys.executable, **kw)

    def test_the_updaters_environment_points_the_home_folders_at_the_decoys(self):
        env = self.env("selfcheck")
        for k in ("HUB_SESSION", "HUB_TEST_CHROME", "CLAUDECODE", "GIT_DIR", "XDG_CONFIG_HOME"):
            self.assertNotIn(k, env)
        self.assertEqual([env[k] for k in ("HOME", "USERPROFILE", "TMPDIR", "AGENTS_HUB_SELF_CHECK_BROWSER", "LANG")],
                         ["/h", "/h", "/t", "off", "ru_RU.UTF-8"])
        self.assertEqual(env["AGENTS_HUB_SELF_CHECK_SECONDS"], "1200")   # the updater's limit (modules/update run_tests)

    def test_hostile_browser_and_claude_profiles(self):
        env = self.env("hostile")
        self.assertEqual([env[k] for k in ("LANG", "LC_ALL", "TZ", "HUB_SESSION", "HTTPS_PROXY", "https_proxy")],
                         ["C", "C", envmatrix.TZ, "envmatrix-session", envmatrix.DEAD_PROXY, envmatrix.DEAD_PROXY])
        self.assertTrue(env["XDG_CONFIG_HOME"].startswith("/h"))
        env = self.env("browser", chrome="/c/chrome")
        self.assertEqual((env["AGENTS_HUB_SELF_CHECK_BROWSER"], env["HUB_TEST_CHROME"]), ("on", "/c/chrome"))
        env = self.env("claude", claude="/c/claude")
        self.assertEqual((env["HUB_TEST_CLAUDE"], env["HUB_REAL_CLAUDE"], env["HTTPS_PROXY"]),
                         ("/c/claude", "/c/claude", envmatrix.DEAD_PROXY))


class Run(Base):
    def run_profile(self, name, mode="ok", **env):
        res = envmatrix.run(name, envmatrix.PROFILES[name], self.repo, self.sha, os.path.join(self.tmp, "work"),
                            sys.executable, base={**os.environ, "FAKE_MODE": mode, **env}, timeout=120)
        with open(res["log"], encoding="utf-8") as f:
            seen = json.loads(next(x for x in f.read().splitlines() if x.startswith("SEEN "))[5:])
        return res, seen

    def test_the_copys_own_runner_goes_as_the_self_check_with_the_decoy_home(self):
        res, seen = self.run_profile("selfcheck")
        self.assertEqual((res["status"], res["ran"]), (envmatrix.OK, 2), res)
        self.assertEqual(seen["argv"], ["--no-lock"])
        self.assertTrue(os.path.samefile(seen["home"], os.path.join(self.tmp, "work", "selfcheck", "home")))
        self.assertEqual(seen["env"]["AGENTS_HUB_SELF_CHECK_BROWSER"], "off")
        self.assertTrue(res["bus"])
        self.assertNotIn("bus skipped", envmatrix.table({"selfcheck": res}))

    def test_a_bus_library_that_does_not_work_for_this_python_is_said(self):
        # 03.11: a library built for another Python imports its version and fails on the first key — ~250 bus tests skipped
        res, _ = self.run_profile("selfcheck", FAKE_BUS="broken")
        self.assertEqual(res["status"], envmatrix.OK)
        self.assertFalse(res["bus"])
        self.assertIn("bus skipped", envmatrix.table({"selfcheck": res}))

    def test_hostile_runs_with_umask_077_and_a_leaked_session(self):
        mine = os.umask(0o022)
        os.umask(0o002 if mine == 0o077 else mine)   # a umask of our own other than 077 (this suite may run under 077 itself)
        self.addCleanup(os.umask, mine)
        before = os.umask(0o022)
        os.umask(before)
        res, seen = self.run_profile("hostile")
        self.assertEqual(res["status"], envmatrix.OK, res)
        if can.inherit_umask():   # Windows: a new process does not take the parent's umask — the profile cannot give it
            self.assertEqual(seen["umask"], "0o77")
        self.assertEqual((seen["env"]["HUB_SESSION"], seen["env"]["TZ"]), ("envmatrix-session", envmatrix.TZ))
        after = os.umask(before)
        self.assertEqual(after, before)   # the script's own umask is back

    def test_a_test_that_touches_a_decoy_is_red_even_when_the_suite_is_green(self):
        res, _ = self.run_profile("selfcheck", "touch")
        self.assertEqual(res["status"], envmatrix.RED)
        self.assertIn(".claude.json", res["detail"])

    def test_litter_in_the_home_folder_is_listed_not_red(self):
        res, _ = self.run_profile("selfcheck", "litter")
        self.assertEqual(res["status"], envmatrix.OK)
        self.assertEqual(res["home"]["added"], [".cache/x/left"])
        self.assertIn(".cache/x/left", envmatrix.table({"selfcheck": res}))

    def test_a_smoke_only_run_is_green_by_the_line_the_runner_prints(self):
        # the copy's own runner says it skipped the core tests: that is green with no "Ran", as on a hub
        import importlib.util
        spec = importlib.util.spec_from_file_location("hub_test_runner_smoke", os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "test.py"))
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        self.assertEqual(envmatrix.SMOKE, runner.SMOKE_SKIPPED)
        res, _ = self.run_profile("selfcheck", "smoke", FAKE_SMOKE=runner.SMOKE_SKIPPED)
        self.assertEqual(res["status"], envmatrix.OK, res)
        self.assertIn("v9.9.9", res["detail"])

    def test_reds_are_named(self):
        res, _ = self.run_profile("selfcheck", "red")
        self.assertEqual((res["status"], res["failed"]), (envmatrix.RED, ["tests.test_x.X.test_a"]))
        self.assertEqual(res["reasons"]["tests.test_x.X.test_a"], "AssertionError: 1 != 2")

    def test_main_holds_the_machine_lock_for_the_whole_matrix(self):
        events = []

        class Held:
            def close(self):
                events.append("released")

        def wait_turn(**kw):
            events.append("taken")
            return Held()
        said = []
        orig = envmatrix.run

        def run(*a, **k):
            events.append("run")
            return orig(*a, **k)
        envmatrix.run = run
        try:
            code = envmatrix.main(["--repo", self.repo, "--profiles", "selfcheck,hostile", "--timeout", "120",
                                   "--work", os.path.join(self.tmp, "work")], say=lambda *a, **k: said.append(a[0]),
                                  wait_turn=wait_turn)
        finally:
            envmatrix.run = orig
        self.assertEqual(code, 0, "\n".join(said))
        self.assertEqual(events, ["taken", "run", "run", "released"])
        self.assertEqual(envmatrix.main(["--repo", self.repo, "--profiles", "nosuch"], say=said.append,
                                        wait_turn=wait_turn), 2)
        events.clear()
        envmatrix.run = run
        try:   # a part of the suite: without the machine lock, as tools/test.py runs it
            self.assertEqual(envmatrix.main(["--repo", self.repo, "--profiles", "selfcheck", "--only", "tests.test_x",
                                             "--work", os.path.join(self.tmp, "work2")], say=lambda *a, **k: said.append(a[0]),
                                            wait_turn=wait_turn), 0)
        finally:
            envmatrix.run = orig
        self.assertEqual(events, ["run"])


if __name__ == "__main__":
    unittest.main()
