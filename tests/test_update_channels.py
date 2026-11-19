"""Update channels (modules/update, core/version.channel): a hub from git has one channel, main — the head of the branch,
everything new at once; `update.channel: stable` of an earlier hub.json reads as main, so a hub that was on release tags
moves to main by itself with the update that brings this code. Only the app's clone (update.app) follows release tags,
vX.Y.Z of the shared repository. Real git repos: a shared (bare) one, the author's copy and a clone with a local
commit."""
import tests  # noqa: F401 — keep the machine's settings out
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest

import platforms

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOUR = 3600


class Repos(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="update-ch-")
        self.addCleanup(platforms.current.rmtree, self.root)
        self.state = os.path.join(self.root, "state")
        os.makedirs(os.path.join(self.state, "spool"))
        self.up = os.path.join(self.root, "up.git")
        self.g(self.root, "init", "-q", "--bare", "-b", "main", self.up)
        self.author = os.path.join(self.root, "author")
        self.g(self.root, "clone", "-q", self.up, self.author)
        self.commit("core.py", "a = 1\n", "start")
        self.mine = os.path.join(self.root, "mine")
        self.g(self.root, "clone", "-q", self.up, self.mine)
        self.write(self.mine, "personal.py", "mine = True\n")
        self.g(self.mine, "add", "-A")
        self.g(self.mine, "commit", "-qm", "my personal change")

    def g(self, cwd, *a):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True,
                              capture_output=True, text=True).stdout.strip()

    def write(self, repo, name, text):
        with open(os.path.join(repo, name), "w") as f:
            f.write(text)

    def commit(self, name, text, msg, tag=None):
        """The author's commit to the shared main; tag adds an annotated release tag on it."""
        self.write(self.author, name, text)
        self.g(self.author, "add", "-A")
        self.g(self.author, "commit", "-qm", msg)
        self.g(self.author, "push", "-q", "origin", "main")
        if tag:
            self.g(self.author, "tag", "-a", "-m", tag, tag)
            self.g(self.author, "push", "-q", "origin", f"refs/tags/{tag}")
        return self.g(self.author, "rev-parse", "HEAD")

    def config(self, **update):
        return {"update": {"enabled": True, "remote": "origin", "branch": "main", **update}, "dev": {"name": "hub-dev"}}

    def updater(self, **update):
        from modules.update import update as mod
        return mod.Updater(self.mine, self.state, self.config(**update), tests=lambda repo: (True, "OK"),
                           claude=lambda: None, spawn=lambda *a: None)

    def has(self, sha):
        return subprocess.run(["git", "merge-base", "--is-ancestor", sha, "HEAD"], cwd=self.mine).returncode == 0

    def checked(self):
        from core import spool
        out = []
        for path, item in spool.take(os.path.join(self.state, "spool")):
            os.unlink(path)
            if item["type"] == "update.checked":
                out.append(item["data"])
        return out


class FromGit(Repos):
    def test_the_head_of_main_past_the_tags(self):
        self.commit("core.py", "a = 2\n", "released", tag="v1.0.0")
        head = self.commit("core.py", "a = 3\n", "fresh")
        self.assertEqual(self.updater().check(), "applied")
        self.assertTrue(self.has(head))
        c = self.checked()[-1]
        self.assertEqual(c["update_channel"], "main")
        self.assertNotIn("version", c)
        from core import version
        self.assertEqual(version.release(self.mine), "v1.0.0")              # the newest release inside is v1.0.0

    def test_stable_of_an_earlier_hub_json_is_main(self):
        # owner 18.11 (m2571664): no installing by tags from git; a hub on stable used to take only v1.0.0 and wait a day
        rel = self.commit("core.py", "a = 2\n", "released", tag="v1.0.0")
        head = self.commit("core.py", "a = 3\n", "nightly")
        for value in ("stable", "nightly", "", None, 7):
            self.assertEqual(self.updater(channel=value).check(apply=False), "available", value)
            self.assertEqual(self.checked()[-1]["update_channel"], "main")
        self.assertEqual(self.updater(channel="stable").check(), "applied")   # no delay: delay_hours is the app's
        self.assertTrue(self.has(head) and self.has(rel))
        from core import catalog, version
        for up in ({"channel": "stable"}, {"channel": "stable", "branch": "beta"}, {}, {"channel": "main"}, "abc", None, -1):
            self.assertEqual(version.channel({"update": up}), "main", up)
        self.assertEqual(version.channel("abc"), "main")
        self.assertEqual(version.channel(catalog.DEFAULTS), "main")
        self.assertEqual(version.channel({"update": {"channel": "main", "app": {"runtime": 1}}}), "stable")

    def test_a_hub_with_no_new_commits_is_current_though_a_newer_tag_is_off_main(self):
        self.g(self.author, "checkout", "-q", "-b", "exp")
        self.write(self.author, "core.py", "a = 'experiment'\n")
        self.g(self.author, "commit", "-qam", "experiment")
        self.g(self.author, "tag", "-a", "-m", "oops", "v9.0.0")
        self.g(self.author, "push", "-q", "origin", "refs/tags/v9.0.0")
        self.assertEqual(self.updater(channel="stable").check(), "current")

    def test_a_custom_branch_is_followed(self):
        self.g(self.author, "checkout", "-q", "-b", "beta")
        self.write(self.author, "core.py", "a = 'beta'\n")
        self.g(self.author, "commit", "-qam", "beta")
        self.g(self.author, "push", "-q", "origin", "beta")
        beta = self.g(self.author, "rev-parse", "HEAD")
        self.assertEqual(self.updater(branch="beta", channel="stable").check(), "applied")
        self.assertTrue(self.has(beta))

    def test_a_release_merged_by_hand_is_named_though_the_updater_did_not_fetch_it(self):
        # 05.11: a hub with updates off merged v1.8.0 by hand (git fetch origin + merge); "About the hub" said v1.7.1 —
        # the last tag the updater fetched into refs/hub-update/tags before it was switched off
        self.commit("core.py", "a = 2\n", "release", tag="v1.0.0")
        self.assertEqual(self.updater().check(), "applied")
        self.commit("core.py", "a = 3\n", "next release", tag="v1.1.0")
        self.g(self.mine, "fetch", "-q", "origin")                          # by hand: the tag comes along into refs/tags
        self.g(self.mine, "merge", "-q", "--no-edit", "origin/main")
        self.assertFalse(self.g(self.mine, "for-each-ref", "refs/hub-update/tags/v1.1.0"))
        from core import version
        self.assertEqual(version.release(self.mine), "v1.1.0")
        self.assertEqual(version.release(self.mine, self.state), "v1.1.0")

    def test_a_withdrawn_release_fetched_by_hand_too_is_not_named(self):
        # the updater removes a withdrawn tag from refs/hub-update/tags; a fetch by hand left it in refs/tags
        self.commit("core.py", "a = 2\n", "one", tag="v1.0.0")
        self.commit("core.py", "a = 3\n", "two", tag="v1.1.0")
        self.assertEqual(self.updater().check(), "applied")
        self.g(self.mine, "fetch", "-q", "origin")
        self.g(self.author, "push", "-q", "origin", ":refs/tags/v1.1.0")    # the release was withdrawn
        self.updater().check(apply=False)
        self.assertFalse(self.g(self.mine, "for-each-ref", "refs/hub-update/tags/v1.1.0"))
        self.assertTrue(self.g(self.mine, "for-each-ref", "refs/tags/v1.1.0"))
        from core import version
        self.assertEqual(version.release(self.mine, self.state), "v1.0.0")

    def test_the_page_keeps_version_and_channel_of_the_check(self):
        from core import reducer
        st, _, why = reducer.reduce(reducer.empty(), {"seq": 1, "ts": 5, "type": "update.checked", "entity": None,
                                                     "data": {"result": "available", "behind": 2, "version": "v1.2.0",
                                                              "update_channel": "stable", "channel": "cli", "owner": True}})
        self.assertIsNone(why)
        self.assertEqual((st["update_check"]["version"], st["update_check"]["update_channel"]), ("v1.2.0", "stable"))

    def test_an_install_time_of_an_earlier_check_is_still_read(self):
        # the journal of a hub that was on stable holds checks with install_after (its delay): read as before
        from core import reducer
        st, _, why = reducer.reduce(reducer.empty(), {"seq": 1, "ts": 5, "type": "update.checked", "entity": None,
                                                     "data": {"result": "available", "behind": 2, "version": "v1.2.0",
                                                              "install_after": 90000, "channel": "cli", "owner": True}})
        self.assertIsNone(why)
        self.assertEqual(st["update_check"]["install_after"], 90000)
        st, _, why = reducer.reduce(reducer.empty(), {"seq": 1, "ts": 5, "type": "update.checked", "entity": None,
                                                     "data": {"result": "available", "install_after": True,
                                                              "channel": "cli", "owner": True}})
        self.assertNotIn("install_after", st["update_check"])               # a bool is not a time

    def test_settings_have_no_channel_row(self):
        from modules.web import settings
        self.assertIsNotNone(settings.validate("update.channel", "main")[1])
        self.assertNotIn("update.channel", [r["key"] for r in settings.view({}, {})])

    def test_browser_tests_in_the_self_check_only_by_a_plain_true(self):
        # owner 03.11: off by default, turned on separately; an unclear value keeps the default
        from core import version
        self.assertTrue(version.browser_tests({"update": {"browser_tests": True}}))
        for cfg in ({}, {"update": {}}, {"update": {"browser_tests": False}}, {"update": {"browser_tests": "abc"}},
                    {"update": {"browser_tests": "1"}}, {"update": {"browser_tests": 1}}, {"update": {"browser_tests": None}},
                    {"update": "abc"}, {"update": None}, None, "abc"):
            self.assertFalse(version.browser_tests(cfg), cfg)


class AppClone(Repos):
    """The app's clone (update.app, core/packaged.apply) follows release tags: the newest by number, on main's history,
    after its delay (update.delay_hours, 24 by default)."""

    def updater(self, **update):
        return super().updater(app={"runtime": 1}, **{"delay_hours": 0, **update})

    def test_the_newest_release_tag_not_the_head_of_main(self):
        self.commit("core.py", "a = 9\n", "release nine", tag="v1.9.0")
        ten = self.commit("core.py", "a = 10\n", "release ten", tag="v1.10.0")   # by numbers, not by letters: 10 > 9
        self.commit("core.py", "a = 2\n", "v1.2.0 by name, older by number", tag="v1.2.0")   # the tag is newer by time
        later = self.commit("core.py", "a = 11\n", "not released yet")
        self.commit("junk.py", "x = 1\n", "junk tags", tag="v2.0")          # not vX.Y.Z — not a release
        self.g(self.author, "tag", "-a", "-m", "rc", "v3.0.0-rc1")
        self.g(self.author, "push", "-q", "origin", "refs/tags/v3.0.0-rc1")
        self.assertEqual(self.updater().check(apply=False), "available")
        (c,) = self.checked()
        self.assertEqual((c["update_channel"], c["version"]), ("stable", "v1.10.0"))
        self.assertEqual(self.updater().check(), "applied")
        self.assertTrue(self.has(ten))
        self.assertFalse(self.has(later))
        from core import version
        self.assertEqual(version.release(self.mine), "v1.10.0")
        self.assertEqual(self.updater().check(), "current")

    def test_a_tag_off_main_is_not_a_release(self):
        rel = self.commit("core.py", "a = 1.5\n", "release", tag="v1.0.0")
        self.g(self.author, "checkout", "-q", "-b", "exp")
        self.write(self.author, "core.py", "a = 'experiment'\n")
        self.g(self.author, "commit", "-qam", "experiment")
        self.g(self.author, "tag", "-a", "-m", "oops", "v9.0.0")
        self.g(self.author, "push", "-q", "origin", "refs/tags/v9.0.0")
        self.assertEqual(self.updater().check(), "applied")
        self.assertTrue(self.has(rel))
        from core import version
        self.assertEqual(version.release(self.mine), "v1.0.0")

    def test_no_release_tags_install_nothing(self):
        head = self.commit("core.py", "a = 2\n", "new feature")
        self.assertEqual(self.updater().check(), "no-release")
        self.assertFalse(self.has(head))
        c = self.checked()[-1]
        self.assertEqual((c["result"], c["update_channel"]), ("no-release", "stable"))
        self.assertNotIn("version", c)

    def test_notes_are_the_release_entries_only(self):
        self.commit("CHANGES.md", "## 2026-01-02\n\n- in the release\n", "notes", tag="v1.0.0")
        self.commit("CHANGES.md", "## 2026-01-03\n\n- after the release\n\n## 2026-01-02\n\n- in the release\n", "more")
        self.assertEqual(self.updater().check(apply=False), "available")
        notes = self.checked()[-1]["notes"]
        self.assertIn("in the release", notes)
        self.assertNotIn("after the release", notes)

    def test_a_removed_tag_is_forgotten(self):
        self.commit("core.py", "a = 2\n", "one", tag="v1.0.0")
        self.commit("core.py", "a = 3\n", "two", tag="v1.1.0")
        self.assertEqual(self.updater().check(apply=False), "available")
        self.assertEqual(self.checked()[-1]["version"], "v1.1.0")
        self.g(self.author, "push", "-q", "origin", ":refs/tags/v1.1.0")    # the release was withdrawn
        self.assertEqual(self.updater().check(apply=False), "available")
        self.assertEqual(self.checked()[-1]["version"], "v1.0.0")

    def test_the_delay_is_set_in_hours(self):
        from core import version
        app = {"app": {"runtime": 1}}
        self.assertEqual(version.delay_hours({"update": app}), 24)
        self.assertEqual(version.delay_hours({"update": {**app, "delay_hours": 2}}), 2)
        self.assertEqual(version.delay_hours({"update": {**app, "delay_hours": 0}}), 0)
        self.assertEqual(version.delay_hours({"update": {**app, "delay_hours": "x"}}), 24)
        self.assertEqual(version.delay_hours({"update": {**app, "delay_hours": -3}}), 24)
        self.assertEqual(version.delay_hours({}), 0)                         # a hub from git has none
        self.assertEqual(version.delay_hours({"update": {"delay_hours": 5}}), 0)

    def age(self, tag, hours):
        """The app saw the tag `hours` hours ago (its own time, from seen.json)."""
        path = os.path.join(self.state, "update", "seen.json")
        with open(path) as f:
            seen = json.load(f)
        seen[tag]["seen"] = round(time.time() - hours * HOUR)
        with open(path, "w") as f:
            json.dump(seen, f)

    def test_a_ripe_older_release_is_offered_while_the_newer_waits(self):
        self.commit("core.py", "a = 2\n", "one", tag="v1.0.0")
        self.assertEqual(self.updater(delay_hours=24).check(apply=False), "current")    # in its delay: no banner
        self.age("v1.0.0", 25)
        self.commit("core.py", "a = 3\n", "two", tag="v1.1.0")
        self.assertEqual(self.updater(delay_hours=24).check(apply=False), "available")
        self.assertEqual(self.checked()[-1]["version"], "v1.0.0")
        self.assertEqual(self.updater(delay_hours=24).check(apply=False, manual=True), "available")
        self.assertEqual(self.checked()[-1]["version"], "v1.1.0")                       # "Check": the newest at once

    def test_a_tag_removed_in_its_delay_is_never_offered(self):
        self.commit("core.py", "a = 2\n", "one", tag="v1.0.0")
        self.assertEqual(self.updater(delay_hours=24).check(apply=False), "current")
        self.g(self.author, "push", "-q", "origin", ":refs/tags/v1.0.0")
        self.age("v1.0.0", 25)
        self.assertEqual(self.updater(delay_hours=24).check(apply=False), "no-release")

    def test_a_damaged_seen_record_gets_a_time_once_and_ripens_from_it(self):
        # an entry without a time (corrupted, foreign format) was recomputed as "now" on every pass — the release never matured
        path = os.path.join(self.state, "update", "seen.json")
        self.commit("core.py", "a = 2\n", "release", tag="v1.0.0")
        self.assertEqual(self.updater(delay_hours=24).check(apply=False), "current")    # in its delay: no banner
        with open(path) as f:
            first = json.load(f)["v1.0.0"]["commit"]
        for bad in ("garbage", {"commit": first}):
            with open(path, "w") as f:
                json.dump({"v1.0.0": bad}, f)
            self.assertEqual(self.updater(delay_hours=24).check(apply=False), "current")
            with open(path) as f:
                seen = json.load(f)
            self.assertIsInstance(seen["v1.0.0"]["seen"], (int, float))     # the time is recorded, not recomputed
            seen["v1.0.0"]["seen"] = round(time.time() - 25 * HOUR)
            with open(path, "w") as f:
                json.dump(seen, f)
            self.assertEqual(self.updater(delay_hours=24).check(apply=False), "available")


class TagWatchGone(unittest.TestCase):
    """The tag watcher of a publishing hub is gone (hub-dev 18.11, after one channel): tags are only offered to old app
    clones after their delay, so "a foreign tag — delete it before hubs install it" has nobody to protect."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="tag-watch-")
        self.addCleanup(platforms.current.rmtree, self.root)
        self.state = os.path.join(self.root, "state")
        os.makedirs(os.path.join(self.state, "spool"))
        self.repo = os.path.join(self.root, "core")   # the core's working repo without an update remote: the publishing one
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        with open(os.path.join(self.state, "published.json"), "w") as f:
            json.dump({"url": "https://example.test/core.git", "sha": "0" * 40, "at": 1}, f)

    def test_hub_doctor_of_a_publishing_hub_has_no_tag_watcher_row(self):
        from unittest import mock
        from core import journal, modcli
        with mock.patch.object(journal, "load_state", lambda state_dir: {}):
            rows = modcli.checks(self.state, [sys.executable], repo=self.repo)
        self.assertTrue(rows)
        self.assertFalse([r for r in rows if "tag" in str(r[1]).lower()], rows)

    def test_its_fact_in_an_earlier_journal_is_still_read(self):
        from core import reducer
        st, _, why = reducer.reduce(reducer.empty(), {"seq": 1, "ts": 5, "type": "release.watch", "entity": None,
                                                     "data": {"checked": 5, "tags": 2, "channel": "cli", "owner": True}})
        self.assertIsNone(why)


def last_release():
    """The newest vX.Y.Z tag of this clone — the release whose updater the hubs on stable run when the next tag comes
    (a hardcoded number goes stale at every release). None: a clone without release tags (the published snapshot)."""
    try:
        tags = subprocess.run(["git", "tag", "--list", "v[0-9]*", "--sort=-v:refname"], cwd=ROOT, check=True,
                              capture_output=True, text=True).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        return None
    return next((t for t in tags if re.fullmatch(r"v\d+\.\d+\.\d+", t)), None)


OLD = last_release()
KEEP = ("core", "modules", "platforms", "i18n", "hosts", "hooks", "bin", "embed")


def old_tree(dest):
    """The code of the last release unpacked into dest; False when this clone has no such history."""
    if OLD is None:
        return False
    have = subprocess.run(["git", "ls-tree", "--name-only", OLD], cwd=ROOT, capture_output=True, text=True).stdout.split()
    got = subprocess.run(["git", "archive", "--format=tar", OLD, *[k for k in KEEP if k in have]], cwd=ROOT,
                         capture_output=True)
    if got.returncode or not got.stdout:
        return False
    path = os.path.join(dest, "old.tar")
    with open(path, "wb") as f:
        f.write(got.stdout)
    with tarfile.open(path) as t:
        t.extractall(os.path.join(dest, "old"), filter="data")
    return os.path.isfile(os.path.join(dest, "old", "modules", "update", "update.py"))


@unittest.skipIf(OLD is None, "no release tag in this clone")
class FromTheLastRelease(Repos):
    """A hub on stable runs the updater of the last release and gets to main with no step of a person. What that updater
    does is read from its own code (core/version.channel of the tag), not assumed: one that still follows release tags
    takes the next tag and only then this code moves the hub to main; one with a single channel is on main at once."""

    def old_follows_tags(self, code):
        got = subprocess.run([sys.executable, "-c",
                              "import sys\nsys.path.insert(0, sys.argv[1])\nfrom core import version\n"
                              "print(version.channel({'update': {'channel': 'stable'}}))", code],
                             capture_output=True, text=True, timeout=60, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(got.returncode, 0, got.stderr[-3000:])
        return got.stdout.strip() == "stable"

    def old_check(self, code):
        script = ("import json, sys\n"
                  "sys.path.insert(0, sys.argv[1])\n"
                  "from modules.update import update\n"
                  "cfg = json.loads(sys.argv[4])\n"
                  "u = update.Updater(sys.argv[2], sys.argv[3], cfg, tests=lambda repo: (True, 'OK'), claude=lambda: None)\n"
                  "print(u.check())\n")
        got = subprocess.run([sys.executable, "-c", script, code, self.mine, self.state,
                              json.dumps(self.config(channel="stable", delay_hours=0))],
                             capture_output=True, text=True, timeout=300,
                             env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(got.returncode, 0, got.stderr[-3000:])
        return got.stdout.strip().splitlines()[-1]

    def test_the_next_tag_moves_a_stable_hub_to_main(self):
        if not old_tree(self.root):
            self.skipTest(f"the code of {OLD} is not in this clone")
        code = os.path.join(self.root, "old")
        self.commit("core.py", "a = 2\n", "the last release", tag="v1.0.0")
        self.assertEqual(self.old_check(code), "applied")                   # the hub sits on the last release
        with open(os.path.join(ROOT, "core", "version.py")) as f:            # what the old updater reads of the new one
            os.makedirs(os.path.join(self.author, "core"), exist_ok=True)
            # without the snapshot's CLAUDE_TESTED: the release would wait for a claude this machine may not have
            self.write(self.author, os.path.join("core", "version.py"),
                       re.sub(r'(?m)^CLAUDE_TESTED = ".*"', 'CLAUDE_TESTED = ""', f.read()))
        nxt = self.commit("core.py", "a = 3\n", "the release with one channel", tag="v1.1.0")
        later = self.commit("core.py", "a = 4\n", "nightly after it")
        self.assertEqual(self.old_check(code), "applied")                   # the old updater takes what it follows
        self.assertTrue(self.has(nxt))
        self.assertEqual(self.has(later), not self.old_follows_tags(code))   # the next tag, or at once the head of main
        self.assertEqual(self.old_check(code), "current")                   # nothing newer to take
        # this code: main, though hub.json says stable (nothing left to take if the old updater was on main already)
        self.assertEqual(self.updater(channel="stable").check(), "applied" if self.old_follows_tags(code) else "current")
        self.assertTrue(self.has(later))


if __name__ == "__main__":
    unittest.main()
