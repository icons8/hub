"""tools/oss_open.py: the checks of the open snapshot and of the install site, and the order of the opening's steps.
Nothing here reaches GitHub: the steps are listed, never run."""
import tests  # noqa: F401 — hide the machine's settings
import importlib.util
import json
import os
import subprocess
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load():
    spec = importlib.util.spec_from_file_location("hub_test_oss_open", os.path.join(ROOT, "tools", "oss_open.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write(root, rel, data):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode("utf-8"))


class Checks(unittest.TestCase):
    def setUp(self):
        self.mod = load()
        tmp = tempfile.TemporaryDirectory(prefix="oss-open-")
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name

    def test_the_site_address_of_an_organization_repository(self):
        self.assertEqual(self.mod.pages_url("Acme/agents-hub"), "https://acme.github.io/agents-hub/")

    def test_the_site_needs_the_scripts_and_the_list_and_takes_no_big_file(self):
        site = os.path.join(self.root, "site")
        write(site, "install.sh", "SERVER='https://acme.github.io/agents-hub'\n")
        write(site, "v1/releases.json", "{}")
        self.assertEqual(self.mod.check_pages(site, []), ["pages: no install.ps1"])
        write(site, "install.ps1", "$Server = '@@HUB_DELIVER_URL@@'\n")
        with mock.patch.object(self.mod, "PAGES_LIMIT", 10), mock.patch.object(self.mod, "SITE_LIMIT", 100):
            write(site, "v1/layer/code-1.0.0-open.tar.gz", b"x" * 60)
            write(site, "v1/layer/code-0.9.0-open.tar.gz", b"x" * 60)
            got = self.mod.check_pages(site, ["reports.example.test"])
        self.assertIn("pages: install.ps1 has no address written in", got)
        self.assertIn("pages: v1/layer/code-1.0.0-open.tar.gz is 0 MB (Pages takes files under 100 MB)", got)
        self.assertTrue(any(x.startswith("pages: ") and "in all" in x for x in got), got)
        write(site, "install.sh", "SERVER='https://reports.example.test'\n")   # a team address in the open site
        self.assertIn("pages: install.sh: carries a forbidden string", self.mod.check_pages(site, ["Reports.Example.Test"]))
        if hasattr(os, "symlink") and os.name != "nt":   # a link would publish what it points to
            os.symlink(self.root, os.path.join(site, "v1", "more"))
            self.assertIn("pages: v1/more is a link", self.mod.check_pages(site, []))

    def test_the_tree_holds_the_language_the_license_and_the_opening_day(self):
        tree = os.path.join(self.root, "tree")
        write(tree, "i18n/ru.json", '{"a": "' + chr(0x0434) + '"}')   # a catalog: its language is its own
        write(tree, "core/x.py", "X = '" + chr(0x0434) + "'\n")
        for args in (("init", "-q"), ("add", "-A")):   # the open tree is a git repository; the ratchet reads its files
            subprocess.run(["git", *args], cwd=tree, check=True, capture_output=True, stdin=subprocess.DEVNULL)
        def run(cmd, **kw):   # git names the main copy; the deny filter finds nothing
            return types.SimpleNamespace(returncode=0, stdout=".git" if "rev-parse" in cmd else "", stderr="")
        with mock.patch.object(self.mod, "run", run), \
                mock.patch.object(self.mod.shutil, "which", return_value=None), \
                mock.patch.object(self.mod.appcode, "OPEN_CHANGES_FROM", None):
            got = self.mod.check_tree(tree, [])
        self.assertIn("core/x.py: 1 lines with Cyrillic outside the language catalogs", got)
        self.assertFalse([x for x in got if x.startswith("i18n/")], got)
        self.assertIn("LICENSE: missing", got)
        self.assertIn("NOTICE: missing", got)
        self.assertIn("gitleaks is not installed: no check of key formats", got)
        self.assertTrue(any(x.startswith("appcode.OPEN_CHANGES_FROM is not set") for x in got), got)
        write(tree, "CHANGES.md", "# What's new\n\nNewest first.\n")   # the opening day set, but no entry left from it
        with mock.patch.object(self.mod, "run", run), mock.patch.object(self.mod.shutil, "which", return_value=None), \
                mock.patch.object(self.mod.appcode, "OPEN_CHANGES_FROM", "2026-03-01"):
            self.assertIn("CHANGES.md: no entry from 2026-03-01 on (the opening day's, at least)", self.mod.check_tree(tree, []))
        # the credits (owner 17.11): the README's author line and the CONTRIBUTORS.md built under it
        no_author = "README.md: no author line \"Created by <Name> ([@login](https://github.com/login))\""
        self.assertIn(no_author, got)
        self.assertIn("CONTRIBUTORS.md: missing", got)
        write(tree, "README.md", "# Hub\n\nCreated by Alice Smith ([@alice](https://github.com/alice))\n")
        write(tree, "CONTRIBUTORS.md", "# Contributors\n\nCreated by Alice Smith ([@alice](https://github.com/alice))\n")
        with mock.patch.object(self.mod, "run", run), mock.patch.object(self.mod.shutil, "which", return_value=None):
            got = self.mod.check_tree(tree, [])
        self.assertNotIn(no_author, got)
        self.assertNotIn("CONTRIBUTORS.md: missing", got)


    def test_the_catalog_is_valid_pinned_and_in_the_public_language(self):
        cat = os.path.join(self.root, "catalog")
        write(cat, "hub-marketplace.json", json.dumps({"name": "agents-hub", "plugins": []}))
        write(cat, "README.md", "The catalog\n" + chr(0x0434) + "\n")
        write(cat, "notes.md", "see reports.example.test\n")   # a team address in the public catalog
        calls = []

        def run(cmd, **kw):   # the open tree's reader: refuses; git names the main copy; the deny filter finds nothing
            calls.append(cmd)
            refuse = "core.marketplace" in cmd
            return types.SimpleNamespace(returncode=1 if refuse else 0, stdout=".git" if "rev-parse" in cmd else "",
                                         stderr="hub-marketplace.json: bad entry" if refuse else "")
        with mock.patch.object(self.mod, "run", run), mock.patch.object(self.mod.shutil, "which", return_value=None):
            got = self.mod.check_catalog(cat, "/x/open-tree", ["Reports.Example.Test"])
        self.assertIn("catalog: hub-marketplace.json: bad entry", got)
        self.assertIn("catalog: hub-marketplace.json lists no plugins", got)   # the strict reader takes an empty one
        self.assertIn("catalog: notes.md: carries a forbidden string", got)
        self.assertIn("catalog: README.md: 1 lines with Cyrillic", got)
        self.assertIn("catalog: gitleaks is not installed: no check of key formats", got)
        reader = next(c for c in calls if "core.marketplace" in c)
        self.assertEqual(reader[-2:], [os.path.abspath(cat), "--strict"])   # the strict reader, the code an open hub has
        os.makedirs(os.path.join(cat, ".git"))
        with mock.patch.object(self.mod, "run", run), mock.patch.object(self.mod.shutil, "which", return_value=None):
            self.assertIn("catalog: a .git folder (give the files, not a clone)",
                          self.mod.check_catalog(cat, "/x/open-tree", []))

    def test_every_pin_of_the_catalog_is_on_github(self):
        # the strict reader checks the file's form; an author's repository may be deleted or private (a 404 at the first
        # install of an open hub)
        entries = [{"name": "here", "source": {"github": "acme/hub-plugin-here", "sha": "a" * 40}},
                   {"name": "gone", "source": {"github": "acme/hub-plugin-gone", "sha": "b" * 40}},
                   {"name": "old", "source": {"github": "acme/hub-plugin-old", "sha": "c" * 40}, "revoked": "why"},
                   {"name": "other", "source": {"url": "https://example.test/x.git"}},
                   {"name": "slow", "source": {"github": "acme/hub-plugin-slow", "sha": "d" * 40}},
                   {"name": "nologin", "source": {"github": "acme/hub-plugin-nologin", "sha": "e" * 40}},
                   {"name": "odd", "source": {"github": "acme/x/../../orgs/y", "sha": "f" * 40}},
                   {"name": "dots", "source": {"github": "../..", "sha": "f" * 40}}]
        calls = []

        def run(cmd, **kw):
            if cmd[0] == "gh":
                self.assertEqual(kw.get("timeout"), 60)   # a stalled gh must not hold the check for hours
                calls.append(cmd[2])
            gone = "hub-plugin-gone" in cmd[2]
            if "hub-plugin-slow" in cmd[2]:
                raise self.mod.Failed("gh api: timed out")
            if "hub-plugin-nologin" in cmd[2]:
                return types.SimpleNamespace(returncode=1, stdout="", stderr="gh: To use GitHub CLI, run: gh auth login")
            return types.SimpleNamespace(returncode=1 if gone else 0, stdout="",
                                         stderr="gh: Not Found (HTTP 404)" if gone else "")
        with mock.patch.object(self.mod, "run", run), mock.patch.object(self.mod.shutil, "which", return_value="/x/gh"):
            got = self.mod.pins_missing(entries)
        self.assertEqual(got, ["gone: acme/hub-plugin-gone at " + "b" * 12 + " is not on GitHub (gh: Not Found (HTTP 404))",
                               "slow: GitHub was not asked: gh api: timed out",   # a stall is a line, the rest still run
                               "nologin: GitHub did not answer for acme/hub-plugin-nologin (gh: To use GitHub CLI, run: gh auth login)",
                               "odd: the pin acme/x/../../orgs/y@" + "f" * 12 + " is not owner/repo at a full commit",
                               "dots: the pin ../..@" + "f" * 12 + " is not owner/repo at a full commit"])
        self.assertEqual(calls, ["repos/acme/hub-plugin-here/commits/" + "a" * 40,
                                 "repos/acme/hub-plugin-gone/commits/" + "b" * 40,
                                 "repos/acme/hub-plugin-slow/commits/" + "d" * 40,
                                 "repos/acme/hub-plugin-nologin/commits/" + "e" * 40])   # nothing asked for a revoked or another source
        with mock.patch.object(self.mod, "run", run), mock.patch.object(self.mod.shutil, "which", return_value=None):
            self.assertEqual(self.mod.pins_missing(entries), ["gh is not installed: no check that the pinned commits are on GitHub"])
        cat = os.path.join(self.root, "catalog2")
        write(cat, "hub-marketplace.json", json.dumps({"name": "agents-hub", "plugins": entries}))
        with mock.patch.object(self.mod, "run", run), mock.patch.object(self.mod.shutil, "which", return_value="/x/gh"):
            self.assertIn("catalog: gone: acme/hub-plugin-gone at " + "b" * 12 + " is not on GitHub (gh: Not Found (HTTP 404))",
                          self.mod.check_catalog(cat, "/x/open-tree", []))

    def test_the_assets_are_there_and_named_for_the_tag(self):
        folder = os.path.join(self.root, "assets")
        os.makedirs(folder)
        self.assertEqual(self.mod.check_assets(folder, "v1.2.3"), [f"assets: no .tar.gz in {folder}"])
        write(folder, "agents-hub-v1.2.3-open-linux-x64.tar.gz", b"x")
        write(folder, "agents-hub-v1.2.30-open-linux-x64.tar.gz", b"x")
        self.assertEqual(self.mod.check_assets(folder, "v1.2.3"),
                         ["assets: agents-hub-v1.2.30-open-linux-x64.tar.gz is not named for v1.2.3"])


class Order(unittest.TestCase):
    def test_the_public_sees_it_whole(self):
        # the snapshot, the assets and the site are in place before the repository turns public; Pages after that (a free
        # organization has no Pages on a private repository), the read-back last
        mod = load()
        a = types.SimpleNamespace(repo_slug="acme/agents-hub", assets="/x/assets", tag="v1.0.0", work="/x/work", pushed=None)
        what = [w for w, _ in mod.steps(a, "/x/tree", "a" * 40, "/x/site")]
        first = lambda word: next(n for n, w in enumerate(what) if word in w)  # noqa: E731
        self.assertLess(first("into main"), first("release assets"))
        self.assertLess(first("release assets"), first("gh-pages ("))
        self.assertLess(first("gh-pages ("), first("public"))
        self.assertLess(first("public"), first("GitHub Pages"))
        self.assertEqual(what[-1], "the site read back")
        bare = types.SimpleNamespace(repo_slug="acme/agents-hub", assets=None, tag=None, work="/x/work", pushed=None)
        plain = [w for w, _ in mod.steps(bare, "/x/tree", "a" * 40, None)]
        self.assertFalse([w for w in plain if "Pages" in w or "assets" in w or "site" in w], plain)   # no homepage either
        self.assertFalse([w for w in plain if "catalog" in w], plain)

    def test_the_catalog_is_public_before_the_core_and_its_checks_after(self):
        # an open hub connects its official catalog at its first start: public before the core; the catalog's checks
        # read the core, so they run once it is public
        mod = load()
        with tempfile.TemporaryDirectory() as cat:
            write(cat, ".github/workflows/check.yml", "on: push\n")
            a = types.SimpleNamespace(repo_slug="acme/agents-hub", catalog_slug="acme/agents-hub-plugins", assets=None,
                                      tag=None, work="/x/work", pushed=None)
            what = [w for w, _ in mod.steps(a, "/x/tree", "a" * 40, "/x/site", cat)]
            first = lambda word: next(n for n, w in enumerate(what) if word in w)  # noqa: E731
            self.assertLess(first("into main of acme/agents-hub ("), first("into main of acme/agents-hub-plugins"))
            self.assertLess(first("acme/agents-hub-plugins public"), first("acme/agents-hub public"))
            self.assertLess(first("acme/agents-hub public"), first("the checks of acme/agents-hub-plugins on"))
            self.assertEqual(what[-1], "the catalog read without a sign-in")
            os.unlink(os.path.join(cat, ".github", "workflows", "check.yml"))   # no workflow: no step for it
            what = [w for w, _ in mod.steps(a, "/x/tree", "a" * 40, None, cat)]
            self.assertFalse([w for w in what if "checks of" in w], what)
        self.assertEqual(mod.marketplaces.OPEN_SOURCE, "icons8/agents-hub-plugins")   # the slug it opens by default


    def test_private_puts_the_code_the_catalog_and_the_release_and_opens_nothing(self):
        # --private: the snapshot, the catalog and the tag with its assets go into the repositories, which stay private;
        # no install site, nothing public, no Pages, no reporting, no read-back without a sign-in
        mod = load()
        with tempfile.TemporaryDirectory() as cat:
            write(cat, ".github/workflows/check.yml", "on: push\n")
            a = types.SimpleNamespace(repo_slug="acme/agents-hub", catalog_slug="acme/agents-hub-plugins",
                                      assets="/x/assets", tag="v1.0.0", work="/x/work", pushed=None, private=True)
            what = [w for w, _ in mod.steps(a, "/x/tree", "a" * 40, None, cat)]
        self.assertEqual(len(what), 3, what)
        self.assertIn("into main of acme/agents-hub (", what[0])
        self.assertIn("into main of acme/agents-hub-plugins", what[1])
        self.assertIn("release assets", what[2])
        with self.assertRaises(SystemExit):   # an install site needs Pages, and Pages a public repository
            mod.main(["--private", "--pages", "/x/site"])


class Steps(unittest.TestCase):
    """The snapshot step and the tag check against a local bare repository standing for the public one."""

    def setUp(self):
        self.mod = load()
        tmp = tempfile.TemporaryDirectory(prefix="oss-steps-")
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.bare = os.path.join(self.root, "public.git")
        self.git("init", "-q", "--bare", self.bare)
        self.tree = os.path.join(self.root, "tree")
        write(self.tree, "README.md", "hello\n")
        self.a = types.SimpleNamespace(repo_slug="acme/agents-hub", remote=self.bare, assets=None, tag="v1.0.0",
                                       work=os.path.join(self.root, "work"), pushed=None)
        os.makedirs(self.a.work)
        env = mock.patch.dict(os.environ, {"PUBLISH_NAME": "pub", "PUBLISH_EMAIL": "pub@example.test"})
        env.start()
        self.addCleanup(env.stop)

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL).stdout.strip()

    def snapshot(self):
        self.a.work = tempfile.mkdtemp(dir=self.root)   # a run again starts in a new folder, as a new process would
        next(do for what, do in self.mod.steps(self.a, self.tree, "c" * 40, None) if "into main" in what)()
        return self.git("--git-dir", self.bare, "rev-list", "main")

    def test_a_run_again_adds_no_second_commit_and_a_new_tree_goes_on_top(self):
        first = self.snapshot()
        self.assertEqual(len(first.split()), 1)          # the public history starts with one commit
        self.assertEqual(self.snapshot(), first)        # the same tree again: main as it was
        self.assertEqual(self.a.pushed, first)
        write(self.tree, "README.md", "hello again\n")
        later = self.snapshot().split()
        self.assertEqual(later[1:], [first])            # on top, the history kept
        self.assertEqual(self.a.pushed, later[0])

    def test_the_snapshot_co_authors_every_contributor_first_and_the_newly_thanked_later(self):
        # owner decision 2026-11-17: the open repository's first commit credits everyone of CONTRIBUTORS.md (the whole
        # history; the open CHANGES.md keeps only its own), a later one those its CHANGES.md thanks anew; the author of
        # the commit stays the publisher, and the README's author is no co-author
        author = "Created by Alice Smith ([@alice](https://github.com/alice))"
        write(self.tree, "README.md", f"# Hub\n\n{author}\n")
        write(self.tree, "CONTRIBUTORS.md", f"# Contributors\n\n{author}\n\nWith:\n\n- [@bob](https://github.com/bob): 2 pull "
                                            "requests\n- [@carol](https://github.com/carol): 1 report or idea\n")
        write(self.tree, "CHANGES.md", "## 2026-03-01\n- one — thanks: @carol\n")
        ids = {"bob": 101, "carol": 102, "dave": 103}
        state = tempfile.mkdtemp(dir=self.root)
        with mock.patch.object(self.mod.contributors, "gh_id", lambda login: ids.get(login)), \
                mock.patch.dict(os.environ, {"HUB_STATE_DIR": state}):
            self.snapshot()
            first = self.git("--git-dir", self.bare, "log", "-1", "--format=%B", "main")
            write(self.tree, "CHANGES.md", "## 2026-03-02\n- two — thanks: @dave, @alice\n\n## 2026-03-01\n- one — thanks: @carol\n")
            self.snapshot()
            later = self.git("--git-dir", self.bare, "log", "-1", "--format=%B", "main")
        self.assertTrue(first.startswith(f"Hub by Icons8, snapshot of {'c' * 12}\n\n"), first)
        self.assertEqual([x for x in first.splitlines() if x.startswith("Co-authored-by:")],
                         ["Co-authored-by: bob <101+bob@users.noreply.github.com>",
                          "Co-authored-by: carol <102+carol@users.noreply.github.com>"])
        self.assertEqual([x for x in later.splitlines() if x.startswith("Co-authored-by:")],
                         ["Co-authored-by: dave <103+dave@users.noreply.github.com>"])
        self.assertEqual(self.git("--git-dir", self.bare, "log", "-1", "--format=%an <%ae>", "main"), "pub <pub@example.test>")
        with open(os.path.join(state, "github-ids.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), ids)   # the ids are kept beside the publishing state

    def test_a_later_snapshot_catches_up_whom_the_commits_do_not_credit_yet(self):
        # the first commit of a repository made before the credits (or a login whose id was unknown) is caught up by the
        # next snapshot: the list's logins and the thanks no commit of main credits yet, nobody twice
        author = "Created by Alice Smith ([@alice](https://github.com/alice))"
        write(self.tree, "README.md", f"# Hub\n\n{author}\n")
        write(self.tree, "CONTRIBUTORS.md", f"# Contributors\n\n{author}\n\nWith:\n\n- [@bob](https://github.com/bob): 2 pull "
                                            "requests\n- [@carol](https://github.com/carol): 1 report or idea\n")
        write(self.tree, "CHANGES.md", "## 2026-03-01\n- one — thanks: @carol\n")
        ids = {"bob": 101, "carol": 102}
        state = tempfile.mkdtemp(dir=self.root)
        with mock.patch.object(self.mod.contributors, "gh_id", lambda login: ids.get(login)), \
                mock.patch.dict(os.environ, {"HUB_STATE_DIR": state}):
            self.snapshot()
            # main as it was before the credits: the same tree, no trailers
            tree = self.git("--git-dir", self.bare, "rev-parse", "main^{tree}")
            bare_sha = self.git("--git-dir", self.bare, "-c", "user.name=t", "-c", "user.email=t@example.test", "commit-tree", tree,
                                "-m", "Hub by Icons8, snapshot of old")
            self.git("--git-dir", self.bare, "update-ref", "refs/heads/main", bare_sha)
            write(self.tree, "README.md", f"# Hub\n\n{author}\n\nmore\n")
            self.snapshot()
            caught = self.git("--git-dir", self.bare, "log", "-1", "--format=%B", "main")
            write(self.tree, "README.md", f"# Hub\n\n{author}\n\nand more\n")
            self.snapshot()
            third = self.git("--git-dir", self.bare, "log", "-1", "--format=%B", "main")
        self.assertEqual([x.split()[1] for x in caught.splitlines() if x.startswith("Co-authored-by:")], ["carol", "bob"])
        self.assertNotIn("Co-authored-by", third)

    def test_the_catalog_goes_into_its_own_main_once(self):
        cat_bare = os.path.join(self.root, "catalog.git")
        self.git("init", "-q", "--bare", cat_bare)
        cat = os.path.join(self.root, "catalog")
        write(cat, "hub-marketplace.json", "{}\n")
        self.a.catalog_slug, self.a.catalog_remote = "acme/agents-hub-plugins", cat_bare

        def put():
            self.a.work = tempfile.mkdtemp(dir=self.root)
            next(do for what, do in self.mod.steps(self.a, self.tree, "c" * 40, None, cat)
                 if "into main of acme/agents-hub-plugins" in what)()
            return self.git("--git-dir", cat_bare, "rev-list", "main").split()
        first = put()
        self.assertEqual(len(first), 1)
        self.assertEqual(put(), first)                      # a run again: no second commit
        self.assertEqual(self.git("--git-dir", cat_bare, "ls-tree", "--name-only", "main"), "hub-marketplace.json")
        self.assertEqual(self.git("--git-dir", self.bare, "rev-list", "--all"), "")   # the core's repository untouched

    def test_a_rehearsal_changes_nothing_on_github(self):
        # --remote and --catalog-remote stand in for the repositories: the steps that set a real repository (public,
        # Pages, Actions, the release) say so and call nothing
        cat = os.path.join(self.root, "catalog")
        write(cat, ".github/workflows/check.yml", "on: push\n")
        self.a.catalog_slug, self.a.catalog_remote = "acme/agents-hub-plugins", os.path.join(self.root, "c.git")
        self.a.assets, self.a.pushed = self.root, "c" * 40
        calls = []
        real = self.mod.run

        def run(cmd, **kw):
            if cmd[0] == "gh" or "upload.py" in " ".join(cmd):
                calls.append(cmd)
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
            return real(cmd, **kw)
        def go():
            with mock.patch.object(self.mod, "run", run), mock.patch.object(self.mod, "put_main", lambda *x: "c" * 40), \
                    mock.patch.object(self.mod, "remote_heads", lambda *x: {"x": "c" * 40}):
                for what, do in self.mod.steps(self.a, self.tree, "c" * 40, None, cat):
                    if "read" not in what:
                        do()
            got = list(calls)
            calls.clear()
            return got
        self.assertEqual(go(), [])                         # both stand-ins
        self.a.remote = None                               # the core real, the catalog a stand-in
        got = go()
        self.assertTrue(got)
        self.assertFalse([c for c in got if any("agents-hub-plugins" in x for x in c)], got)
        self.a.remote, self.a.catalog_remote = os.path.join(self.root, "x.git"), None   # the core a stand-in only
        got = go()
        self.assertTrue(got)
        self.assertTrue(all(any("agents-hub-plugins" in x for x in c) for c in got), got)   # only the real catalog
        self.assertIn(["gh", "api", "-X", "PUT", "repos/acme/agents-hub-plugins/actions/permissions", "-F", "enabled=false"],
                      got)   # its Actions off before its push, as on the day

    def test_the_tag_is_free_or_on_main_with_this_tree(self):
        self.assertEqual(self.mod.tag_free(self.a, self.tree), [])        # nothing on the repository yet
        main = self.snapshot()
        self.git("--git-dir", self.bare, "tag", "v1.0.0", main)
        self.assertEqual(self.mod.tag_free(self.a, self.tree), [])        # a run again
        write(self.tree, "README.md", "other\n")
        self.assertEqual(self.mod.tag_free(self.a, self.tree), ["v1.0.0 is already on acme/agents-hub at a snapshot of another tree"])
        self.snapshot()
        self.assertEqual(self.mod.tag_free(self.a, self.tree), [f"v1.0.0 is already on acme/agents-hub at {main[:12]}, not at its main"])

    def test_anew_starts_the_history_again_and_takes_a_stale_tag_away(self):
        old = self.snapshot()
        write(self.tree, "README.md", "a rehearsal's second snapshot\n")
        self.snapshot()
        self.git("--git-dir", self.bare, "tag", "v1.0.0", old)   # the rehearsal's release tag, at another tree
        write(self.tree, "README.md", "the opening day\n")
        self.a.anew = True
        self.assertEqual(self.mod.tag_free(self.a, self.tree), [])   # it goes in the step, not a refusal
        got = self.snapshot().split()
        self.assertEqual(len(got), 1)   # main is this one commit: nothing of the earlier snapshots
        self.assertEqual(self.git("--git-dir", self.bare, "show", "main:README.md"), "the opening day")
        self.assertEqual(self.git("--git-dir", self.bare, "tag", "--list"), "")
        self.assertEqual(self.snapshot().split(), got)   # a run again: main as it was
        self.git("--git-dir", self.bare, "tag", "v1.0.0", got[0])   # the tag the run put: at this tree, it stays
        self.snapshot()
        self.assertEqual(self.git("--git-dir", self.bare, "tag", "--list"), "v1.0.0")

    def test_anew_takes_away_a_tag_on_an_earlier_snapshot_of_the_same_tree(self):
        first = self.snapshot()
        write(self.tree, "README.md", "the same tree, a second commit\n")
        second = self.snapshot().split()[0]
        self.git("--git-dir", self.bare, "tag", "v1.0.0", second)   # this tree, but on the earlier history
        self.a.anew = True
        got = self.snapshot().split()
        self.assertEqual(len(got), 1)
        self.assertNotIn(first, got)
        self.assertEqual(self.git("--git-dir", self.bare, "tag", "--list"), "")   # gone: it kept the old history

    def test_anew_names_other_tags(self):
        self.git("--git-dir", self.bare, "tag", "v0.9.0", self.snapshot().split()[0])
        self.a.anew = True
        self.assertIn("v0.9.0", " ".join(self.mod.still_private(self.a)))   # a stand-in: the tags are asked all the same

    def test_anew_only_while_the_repository_is_private(self):
        a = types.SimpleNamespace(repo_slug="acme/agents-hub", remote=None, tag="v1.0.0", work=self.root)
        for answer, refused in (("true\n", False), ("false\n", True)):
            with mock.patch.object(self.mod, "run", lambda *x, **k: types.SimpleNamespace(returncode=0, stdout=answer)):
                self.assertEqual(bool(self.mod.still_private(a)), refused, answer)
        self.assertEqual(self.mod.still_private(self.a), [])   # a stand-in: no tags, nothing else to ask


if __name__ == "__main__":
    unittest.main()
