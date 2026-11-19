"""Hub Developer from the start and updating from the shared repository (owner decision 2026-10-02: people will check
the hub out from the private repo, so they must have the Developer hub from the start, and the hub must pull updates
from upstream)."""
import tests  # noqa: F401 — do not see the machine's settings
import unittest

from core import dev, reducer

OWNER = {"channel": "web", "owner": True}


class Developer(unittest.TestCase):
    def test_its_skill_and_folder(self):
        name, data = dev.spec({"dev": {"name": "hub-dev"}})
        self.assertEqual((name, data["role"], data["tools"], data["cwd"]), ("hub-dev", "agent", ["core:hub-developer"],
                                                                          dev.REPO))
        st, _, why = reducer.reduce(reducer.empty(), {"seq": 1, "ts": 1, "type": "session.requested", "entity": name,
                                                      "data": {**data, "session_id": "s"}})
        self.assertIsNone(why)
        self.assertEqual(st["sessions"]["hub-dev"]["role"], "agent")
        self.assertEqual(dev.spec({"dev": {"enabled": False}})[0], "hub-dev")   # it cannot be turned off (core/system.py)


class Update(unittest.TestCase):
    """Real git repos: the shared (bare) one and a colleague's clone with a personal commit."""

    def setUp(self):
        import os
        import subprocess
        import tempfile
        self.os, self.sp = os, subprocess
        self.root = tempfile.mkdtemp(prefix="update-")
        self.state = os.path.join(self.root, "state")
        os.makedirs(os.path.join(self.state, "spool"))
        g = lambda cwd, *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd,  # noqa: E731
                                           check=True, capture_output=True, text=True).stdout
        self.g = g
        self.up = os.path.join(self.root, "up.git")
        g(self.root, "init", "-q", "--bare", "-b", "main", self.up)
        self.author = os.path.join(self.root, "author")
        g(self.root, "clone", "-q", self.up, self.author)
        self.write(self.author, "core.py", "a = 1\n")
        g(self.author, "add", "-A"); g(self.author, "commit", "-qm", "start"); g(self.author, "push", "-q", "origin", "main")
        self.mine = os.path.join(self.root, "mine")
        g(self.root, "clone", "-q", self.up, self.mine)
        self.write(self.mine, "personal.py", "mine = True\n")
        g(self.mine, "add", "-A"); g(self.mine, "commit", "-qm", "my personal change")

    def write(self, repo, name, text):
        with open(self.os.path.join(repo, name), "w") as f:
            f.write(text)

    def upstream(self, name="core.py", text="a = 2\n", msg="new feature"):
        self.write(self.author, name, text)
        self.g(self.author, "commit", "-qam", msg) if name == "core.py" else (
            self.g(self.author, "add", "-A"), self.g(self.author, "commit", "-qm", msg))
        self.g(self.author, "push", "-q", "origin", "main")

    def updater(self, ok=True):
        from modules.update import update
        cfg = {"update": {"enabled": True, "remote": "origin", "branch": "main", "channel": "main"}, "dev": {"name": "hub-dev"}}
        return update.Updater(self.mine, self.state, cfg, tests=lambda repo: (ok, "FAILED (failures=1)"))

    def spooled(self):
        from core import spool
        out = []
        for path, item in spool.take(self.os.path.join(self.state, "spool")):
            self.os.unlink(path)
            out.append(item)
        return out

    def test_applies_on_top_of_personal_commits_and_restarts(self):
        self.assertEqual(self.updater().check(), "current")
        self.upstream()
        self.assertEqual(self.updater().check(), "applied")
        log = self.g(self.mine, "log", "--format=%s")
        self.assertIn("my personal change", log)
        self.assertIn("new feature", log)
        evs = self.spooled()
        self.assertEqual([e["type"] for e in evs], ["update.checked", "update.testing", "update.applied", "hub.restart_requested",
                                                    "hub.restart_requested", "owner.notify", "update.checked"])
        # the page at once, without waiting for sessions: JS is served from disk (new), while the web process stays old until a
        # full restart — without the server's new paths (/i18n.json) the page is empty; "Files" with it (who they let in)
        self.assertEqual([e["data"]["parts"] for e in evs if e["type"] == "hub.restart_requested"], [["web", "files"], []])
        last = evs[-1]["data"]
        self.assertEqual((last["result"], last["behind"]), ("applied", 1))
        self.assertIn("new feature", last["notes"])
        self.assertTrue(self.os.path.exists(self.os.path.join(self.state, "migrate.pending")))  # the data migration — after the restart

    def test_a_machine_without_a_git_identity_still_merges(self):
        # a Windows stand: git has no user.name/email — merge refused with «Please tell me who you are», and the update
        # looked like a conflict with personal edits («in: ?»)
        from unittest import mock
        empty = self.os.path.join(self.root, "gitconfig")
        open(empty, "w").close()
        self.g(self.mine, "config", "user.useConfigOnly", "true")   # without this git would guess the address from the machine name
        with mock.patch.dict(self.os.environ, {"GIT_CONFIG_GLOBAL": empty}):
            self.upstream()
            self.assertEqual(self.updater().check(), "applied")
        self.assertIn("new feature", self.g(self.mine, "log", "--format=%s"))

    def claude_release(self, ver="2.1.290"):
        """A release in the shared repo that runs Claude Code `ver` from the hub's folder (its CLAUDE_TESTED)."""
        self.os.makedirs(self.os.path.join(self.author, "core"), exist_ok=True)
        self.upstream("core/version.py", f'CLAUDE_TESTED = "{ver}"\nMIN_CLAUDE = CLAUDE_TESTED\n', "runs a newer claude")
        return {"update": {"enabled": True, "remote": "origin", "branch": "main", "channel": "main"},
                "dev": {"name": "hub-dev"}}

    def own(self, ver):
        """The release's copy as core/ownclaude leaves it: <state>/claude-code/<ver>/claude[.exe]. -> its path."""
        from core import ownclaude
        path = ownclaude.path(self.state, ver)
        self.os.makedirs(self.os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("#!/bin/sh\n")
        return path

    def fetched(self, put, failed=None):
        """A claude_install of the test: records the version; puts the copy in place or raises `failed`."""
        def install(state, need):
            put.append(need)
            if failed:
                raise failed
            return self.own(need)
        return install

    def test_the_releases_claude_is_downloaded_into_the_hubs_folder_before_the_merge(self):
        # owner 2026-11-13 (m2261407): "the update could download the Claude Code version it needs by itself, without a
        # model, quietly, and use that version from its own runtime" — the person's claude is not asked or touched
        from modules.update import update
        cfg, put = self.claude_release(), []

        def install(state, need):
            put.append((state, need, self.g(self.mine, "log", "-1", "--format=%s").strip()))
            return self.own(need)
        u = update.Updater(self.mine, self.state, cfg, tests=lambda repo: (True, "OK"), claude=lambda: "9.9.9",
                           claude_install=install)
        self.assertEqual(u.check(), "applied")
        self.assertEqual(put, [(self.state, "2.1.290", "my personal change")])   # before the merge, whatever claude is there
        checked = [e for e in self.spooled() if e["type"] == "update.checked"][-1]["data"]
        self.assertEqual(checked.get("claude_installed"), "2.1.290")
        self.assertNotIn("claude_failed", checked)

    def test_an_updater_of_before_the_folder_neither_holds_nor_installs_claude_for_a_release(self):
        # the published updaters (v1.8.0 and the nightlies until 2026-11-11) read `MIN_CLAUDE = "<version>"` of the
        # release: held it for a person ("needs-claude") or ran `claude install` into ~/.local/bin. The snapshot publish.sh
        # pins (compat --pin) carries no such literal, so they install it and leave claude alone; this updater reads the
        # pinned version. The working copy names none (spec d2263524, R1): a hub from it runs claude from PATH
        import os
        import importlib.util
        import re
        import shutil
        import tempfile
        import platforms
        from core import ownclaude
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        spec = importlib.util.spec_from_file_location("hub_test_compat", os.path.join(repo, "tools", "compat.py"))
        compat = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(compat)
        old = re.compile(r'^MIN_CLAUDE\s*=\s*"([\d.]+)"', re.M)   # the gate of those updaters, verbatim
        tree = tempfile.mkdtemp(prefix="update-pin-")
        self.addCleanup(platforms.current.rmtree, tree)
        os.makedirs(os.path.join(tree, "core"))
        shutil.copy(os.path.join(repo, "core", "version.py"), os.path.join(tree, "core", "version.py"))
        compat.pin(tree, "2.1.400")
        with open(os.path.join(tree, "core", "version.py"), encoding="utf-8") as f:
            text = f.read()
        self.assertIsNone(old.search(text))
        self.assertEqual(ownclaude.named(text), "2.1.400")
        self.assertEqual(ownclaude.release(tree), "2.1.400")
        self.assertIsNone(ownclaude.named('PLUGIN_API = "1.9"\nMIN_CLAUDE = "2.1.290"\n'))   # a release before the folder
        self.assertIsNone(ownclaude.named('CLAUDE_TESTED = ""\n'))

    def test_a_release_waits_for_its_claude(self):
        # spec d2263524, decision R3: the sessions of a release run its own Claude Code — a failed download holds the
        # release (no merge), told once, tried again a day later or at once by `hub update`
        import json
        from core import claudeinstall
        from modules.update import update
        cfg, put = self.claude_release(), []
        before = self.g(self.mine, "rev-parse", "HEAD")
        failing = self.fetched(put, claudeinstall.Failed("network", "downloads.claude.ai: no connection"))
        u = lambda install=failing: update.Updater(self.mine, self.state, cfg, tests=lambda repo: (True, "OK"),  # noqa: E731
                                                   claude=lambda: "2.1.218", claude_install=install)
        self.assertEqual(u().check(), "claude-wait")
        self.assertEqual(self.g(self.mine, "rev-parse", "HEAD"), before)
        evs = self.spooled()
        (line,) = [e for e in evs if e["data"].get("id") == update.CLAUDE_NOTICE]
        self.assertIn("2.1.290", line["data"]["text"])
        self.assertIn("no connection", line["data"]["text"])
        checked = evs[-1]["data"]
        self.assertEqual((checked["result"], checked["need_claude"]), ("claude-wait", "2.1.290"))
        self.assertIn("no connection", checked["claude_failed"])
        self.assertEqual(u().check(), "claude-wait")   # within the day: not tried again, not told again
        self.assertEqual(put, ["2.1.290"])
        self.assertEqual([e for e in self.spooled() if e["data"].get("id") == update.CLAUDE_NOTICE], [])
        self.assertEqual(u().check(manual=True), "claude-wait")   # `hub update` tries at once
        self.assertEqual(put, ["2.1.290"] * 2)
        path = self.os.path.join(self.state, update.CLAUDE_TRY)
        with open(path) as f:
            tried = json.load(f)
        with open(path, "w") as f:   # a day on: the download goes through, the release with it
            json.dump({**tried, "at": tried["at"] - update.CLAUDE_AGAIN - 1}, f)
        self.spooled()
        self.assertEqual(u(self.fetched(put)).check(), "applied")
        self.assertEqual(put, ["2.1.290"] * 3)
        self.assertIn("runs a newer claude", self.g(self.mine, "log", "--format=%s"))
        self.assertEqual([e["type"] for e in self.spooled() if e["data"].get("id") == update.CLAUDE_NOTICE],
                         ["owner.notify_resolved"])

    def test_a_hub_already_on_the_release_downloads_its_claude(self):
        # a hub an updater of before the folder moved onto this release (it ran claude from PATH), or whose download
        # failed: nothing new to merge, its own copy is downloaded now. A failure is told once (the sessions keep the
        # claude they run) and tried again a day later, or at once by `hub update`; a pass that only looks ("Check" of
        # a hub from git) downloads nothing
        import json
        from core import claudeinstall
        from modules.update import update
        self.claude_release()
        self.g(self.mine, "pull", "-q", "--no-rebase", "--no-edit", "origin", "main")
        cfg, put = {"update": {"enabled": True, "remote": "origin", "branch": "main", "channel": "main"}}, []
        failing = self.fetched(put, claudeinstall.Failed("network", "no connection"))
        u = lambda install=failing: update.Updater(self.mine, self.state, cfg, tests=lambda repo: (True, "OK"),  # noqa: E731
                                                   claude=lambda: "2.1.300", claude_install=install)
        self.assertEqual(u().check(apply=False), "current")   # "Check": only a look
        self.assertEqual(put, [])
        self.assertEqual(u().check(), "current")
        self.assertEqual(put, ["2.1.290"])
        evs = self.spooled()
        (line,) = [e for e in evs if e["data"].get("id") == update.CLAUDE_NOTICE]
        self.assertIn("2.1.300", line["data"]["text"])   # the claude the sessions keep meanwhile
        self.assertIn("no connection", evs[-1]["data"]["claude_failed"])
        self.assertEqual(u().check(), "current")   # within the day: not tried again
        self.assertEqual(put, ["2.1.290"])
        self.assertEqual(u().check(manual=True), "current")   # `hub update` tries at once
        self.assertEqual(put, ["2.1.290"] * 2)
        path = self.os.path.join(self.state, update.CLAUDE_TRY)
        with open(path) as f:
            tried = json.load(f)
        with open(path, "w") as f:   # a day on
            json.dump({**tried, "at": tried["at"] - update.CLAUDE_AGAIN - 1}, f)
        self.spooled()
        self.assertEqual(u(self.fetched(put)).check(), "current")
        self.assertEqual(put, ["2.1.290"] * 3)
        evs = self.spooled()
        self.assertEqual([e["type"] for e in evs if e["data"].get("id") == update.CLAUDE_NOTICE], ["owner.notify_resolved"])
        self.assertEqual(evs[-1]["data"].get("claude_installed"), "2.1.290")
        self.assertFalse(self.os.path.exists(path))
        self.assertEqual(u(lambda *a: self.fail("downloaded twice")).check(), "current")   # there: nothing to do

    def test_the_running_releases_copy_does_not_clear_the_next_releases_failure(self):
        # the next release's download failed (its record and line); a later pass stops before it and finds the running
        # release's copy there — the next one's record and line stay (its retry keeps its day)
        import json
        from core import claudeinstall
        from modules.update import update
        self.os.makedirs(self.os.path.join(self.mine, "core"), exist_ok=True)
        self.write(self.mine, "core/version.py", 'CLAUDE_TESTED = "2.1.218"\n')
        self.g(self.mine, "add", "-A"); self.g(self.mine, "commit", "-qm", "own release")
        self.own("2.1.218")
        cfg = self.claude_release()
        u = update.Updater(self.mine, self.state, cfg, tests=lambda repo: (True, "OK"), claude=lambda: "2.1.218",
                           claude_install=self.fetched([], claudeinstall.Failed("network", "no connection")))
        self.assertEqual(u.check(), "claude-wait")
        self.spooled()
        u = update.Updater(self.mine, self.state, cfg, claude=lambda: "2.1.218", claude_install=lambda *a: self.fail("x"))
        u.info = {}
        self.assertTrue(u._claude_up("2.1.218"))
        with open(self.os.path.join(self.state, update.CLAUDE_TRY)) as f:
            self.assertEqual(json.load(f)["need"], "2.1.290")
        self.assertEqual([e for e in self.spooled() if e["data"].get("id") == update.CLAUDE_NOTICE], [])

    def test_a_second_hub_on_the_same_code_downloads_its_own_copy(self):
        # update.follow: the first hub merges the code; the second one only follows it — and gets its own copy
        from modules.update import update
        self.claude_release()
        self.g(self.mine, "pull", "-q", "--no-rebase", "--no-edit", "origin", "main")
        put = []
        cfg = {"update": {"enabled": True, "remote": "origin", "branch": "main", "channel": "main", "follow": True}}
        u = update.Updater(self.mine, self.state, cfg, claude=lambda: "2.1.218", claude_install=self.fetched(put))
        self.assertIn(u.check(), ("following", "current"))
        self.assertEqual(put, ["2.1.290"])

    def test_a_hub_waiting_for_its_next_release_downloads_its_claude_too(self):
        # not only "current": a red or dirty pass — the hub stays on its release and gets its copy
        from modules.update import update
        self.claude_release()
        self.g(self.mine, "pull", "-q", "--no-rebase", "--no-edit", "origin", "main")
        self.upstream()
        self.write(self.mine, "personal.py", "mine = False\n")   # an edit not committed: the next release waits
        cfg = {"update": {"enabled": True, "remote": "origin", "branch": "main", "channel": "main"}}
        put = []
        u = update.Updater(self.mine, self.state, cfg, tests=lambda repo: (True, "OK"),
                           claude=lambda: "2.1.218", claude_install=self.fetched(put))
        self.assertEqual(u.check(), "dirty")
        self.assertEqual(put, ["2.1.290"])

    def test_a_failed_download_of_the_next_release_is_not_withdrawn_in_the_same_pass(self):
        # the next release's copy failed and the pass stayed: the running release's copy is there — its check must not
        # withdraw the line and the record just made, or every pass downloads again
        from core import claudeinstall
        from modules.update import update
        self.os.makedirs(self.os.path.join(self.mine, "core"), exist_ok=True)
        self.write(self.mine, "core/version.py", 'CLAUDE_TESTED = "2.1.218"\n')
        self.g(self.mine, "add", "-A"); self.g(self.mine, "commit", "-qm", "own release")
        self.own("2.1.218")
        self.claude_release()
        cfg = {"update": {"enabled": True, "remote": "origin", "branch": "main", "channel": "main"}}
        put = []
        for _ in range(2):
            u = update.Updater(self.mine, self.state, cfg, tests=lambda repo: (True, "OK"), claude=lambda: "2.1.218",
                               claude_install=self.fetched(put, claudeinstall.Failed("network", "no connection")))
            self.assertEqual(u.check(), "claude-wait")
        self.assertEqual(put, ["2.1.290"])   # once: the second pass is within the day
        kinds = [e["type"] for e in self.spooled() if e["data"].get("id") == update.CLAUDE_NOTICE]
        self.assertEqual(kinds, ["owner.notify"])

    def test_a_dark_wake_downloads_no_claude(self):
        from modules.update import update
        self.claude_release()
        self.g(self.mine, "pull", "-q", "--no-rebase", "--no-edit", "origin", "main")
        self.upstream()
        cfg = {"update": {"enabled": True, "remote": "origin", "branch": "main", "channel": "main"}}
        put = []   # a list, not self.fail: the install's own failures are caught
        u = update.Updater(self.mine, self.state, cfg, tests=lambda repo: (True, "OK"), claude=lambda: "2.1.218",
                           dark=lambda: True, claude_install=self.fetched(put))
        self.assertEqual(u.check(), "asleep")
        self.assertEqual(put, [])

    def test_a_hub_on_its_release_downloads_no_claude_in_a_dark_wake(self):
        # "current" (like "waiting", "needs-runtime") passes no dark-wake check of a new release: the download of its own
        # copy must not start either
        from modules.update import update
        self.claude_release()
        self.g(self.mine, "pull", "-q", "--no-rebase", "--no-edit", "origin", "main")
        cfg = {"update": {"enabled": True, "remote": "origin", "branch": "main", "channel": "main"}}
        put = []
        u = lambda dark: update.Updater(self.mine, self.state, cfg, tests=lambda repo: (True, "OK"),  # noqa: E731
                                        claude=lambda: "2.1.218", dark=lambda: dark, claude_install=self.fetched(put))
        self.assertEqual(u(True).check(), "current")
        self.assertEqual(put, [])
        self.assertEqual(u(True).check(manual=True), "current")   # the owner asking downloads even then
        self.assertEqual(put, ["2.1.290"])

    def test_a_version_file_that_is_not_utf8_is_no_version(self):
        from modules.update import update
        self.os.makedirs(self.os.path.join(self.mine, "core"), exist_ok=True)
        with open(self.os.path.join(self.mine, "core", "version.py"), "wb") as f:
            f.write('CLAUDE_TESTED = "2.1.290"  # \u0432\n'.encode("cp1251"))
        u = update.Updater(self.mine, self.state, {"update": {"enabled": True, "channel": "main"}},
                           claude_install=lambda *a: self.fail("installed"))
        self.assertIsNone(u._min_claude(self.mine))

    def test_a_download_already_under_way_is_no_failure(self):
        # «Setup» downloads the copy (an app hub's start, modules/setup/auto.py) while the update check wants it too: the
        # second finds the lock busy — no line for the owner, the next pass looks again
        from core import claudeinstall
        from modules.update import update
        self.claude_release()
        self.g(self.mine, "pull", "-q", "--no-rebase", "--no-edit", "origin", "main")
        cfg = {"update": {"enabled": True, "remote": "origin", "branch": "main", "channel": "main"}}
        u = update.Updater(self.mine, self.state, cfg, claude=lambda: "2.1.218",
                           claude_install=self.fetched([], claudeinstall.Failed("busy", "another fetch is going on")))
        self.assertEqual(u.check(), "current")
        evs = self.spooled()
        self.assertEqual([e for e in evs if e["data"].get("id") == update.CLAUDE_NOTICE], [])
        self.assertNotIn("claude_failed", evs[-1]["data"])
        self.assertFalse(self.os.path.exists(self.os.path.join(self.state, update.CLAUDE_TRY)))
        self.upstream("core/version.py", 'CLAUDE_TESTED = "2.1.300"\n', "the next release")   # before a merge: busy
        before = self.g(self.mine, "rev-parse", "HEAD")
        u = update.Updater(self.mine, self.state, cfg, tests=lambda repo: (True, "OK"), claude=lambda: "2.1.218",
                           claude_install=self.fetched([], claudeinstall.Failed("busy", "another fetch is going on")))
        self.assertEqual(u.check(), "busy")   # main() tries again after BUSY_RETRY, minutes
        self.assertIn("busy", update.RETRY_SOON)
        self.assertEqual(self.g(self.mine, "rev-parse", "HEAD"), before)
        self.assertEqual([e for e in self.spooled() if e["data"].get("id") == update.CLAUDE_NOTICE], [])

    def test_a_hub_that_does_not_update_leaves_its_claude_alone(self):
        # updates off (spec d2263524, R2) or a hub on Codex: nothing is downloaded, claude is not even asked
        from modules.update import update
        self.claude_release()
        self.g(self.mine, "pull", "-q", "--no-rebase", "--no-edit", "origin", "main")
        for cfg in ({"update": {"enabled": False}}, {"update": {"enabled": True, "channel": "main"}, "host": "codex"}):
            with self.subTest(cfg=cfg):
                asked = []
                u = update.Updater(self.mine, self.state, cfg, claude=lambda: asked.append(1) or "2.1.218",
                                   claude_install=lambda *a: self.fail("installed"))
                self.assertIn(u.check(), ("disabled", "current"))
                self.assertEqual(asked, [])

    def test_a_copy_already_there_or_a_claude_of_the_owners_choice_is_not_downloaded(self):
        from unittest import mock
        from modules.update import update
        for case in ("there", "HUB_CLAUDE"):
            with self.subTest(case=case):
                self.setUp()
                cfg = self.claude_release()
                env = {"HUB_CLAUDE": "/opt/x/claude"} if case == "HUB_CLAUDE" else {}
                if case == "there":
                    self.own("2.1.290")
                with mock.patch.dict(self.os.environ, env):
                    u = update.Updater(self.mine, self.state, cfg, tests=lambda repo: (True, "OK"),
                                       claude=lambda: "2.1.100", claude_install=lambda *a: self.fail("downloaded"))
                    self.assertEqual(u.check(), "applied")

    def test_the_signed_build_goes_into_the_hubs_folder_and_a_refused_one_holds_the_release(self):
        # the whole path through core/ownclaude on Anthropic's fake downloads: the signed manifest, the checked binary in
        # <state>/claude-code/<version>/, its own --version; the person's home gets nothing (no `claude install`). A
        # manifest of another key downloads nothing and the release waits
        import subprocess
        import sys
        import tempfile
        import platforms
        from core import ownclaude
        from modules.update import update
        from tests.fakes import claude_downloads as fake
        plat = "linux-x64"
        for signer in ("right", "other"):
            with self.subTest(signer=signer):
                self.setUp()
                cfg = self.claude_release()
                home = tempfile.mkdtemp(prefix="update-home-")
                self.addCleanup(platforms.current.rmtree, home)
                key = fake.Key()
                srv = fake.Server(key, "2.1.290", {plat: fake.fake_claude("2.1.290")})
                self.addCleanup(srv.close)
                if signer == "other":
                    srv.signature = fake.Key(seed=2).sign(srv.manifest)
                run = lambda argv, **kw: subprocess.run(([sys.executable] if self.os.name == "nt" else []) + list(argv), **kw)  # noqa: E731

                def install(state, need):
                    return update.install_claude(state, need, root_url=srv.url, fingerprint=key.fingerprint, run=run,
                                                 platform=plat, pause=0)
                from unittest import mock
                with mock.patch.dict(self.os.environ, {"HOME": home, "USERPROFILE": home}):
                    u = update.Updater(self.mine, self.state, cfg, tests=lambda repo: (True, "OK"),
                                       claude=lambda: "2.1.218", claude_install=install)
                    got = u.check()
                own = ownclaude.mine(self.state, "2.1.290")
                if signer == "right":
                    self.assertEqual(got, "applied")
                    self.assertEqual(own, ownclaude.path(self.state, "2.1.290"))
                    with open(self.os.path.join(home, "ran-version.txt")) as f:   # the downloaded file itself was asked
                        self.assertIn(self.os.path.join(self.state, "claude-code"), f.read())
                else:
                    self.assertEqual(got, "claude-wait")
                    self.assertIsNone(own)
                    self.assertFalse(any(p.endswith("/claude") for p, _ in srv.requests))   # nothing downloaded
                self.assertEqual(sorted(self.os.listdir(home)), ["ran-version.txt"] if signer == "right" else [])

    def test_a_codex_hub_does_not_wait_for_claude(self):
        # Codex (experimental): a hub with "host": "codex" runs no claude — its update downloads none and does not wait;
        # claude is not even asked for its version
        from modules.update import update
        self.claude_release()
        cfg = {"update": {"enabled": True, "remote": "origin", "branch": "main", "channel": "main"}, "dev": {"name": "hub-dev"},
               "host": "codex"}
        asked = []
        u = update.Updater(self.mine, self.state, cfg, tests=lambda repo: (True, "OK"),
                           claude=lambda: asked.append(1) or "2.1.218", claude_install=lambda *a: self.fail("downloaded"))
        self.assertEqual(u.check(), "applied")
        self.assertEqual(asked, [])

    def test_red_tests_roll_back_and_tell_the_developer(self):
        before = self.g(self.mine, "rev-parse", "HEAD")
        self.upstream()
        self.assertEqual(self.updater(ok=False).check(), "red")
        self.assertEqual(self.g(self.mine, "rev-parse", "HEAD"), before)
        evs = [e for e in self.spooled() if e["type"] != "update.checked"]
        self.assertEqual([e["type"] for e in evs], ["update.testing", "update.failed", "message.received"])
        self.assertEqual(evs[0]["data"]["from"], before.strip())
        # from the update module (the «from the update module» label): there is no «hub update» session (core/admit.py)
        self.assertEqual((evs[2]["data"]["to"], evs[2]["data"]["channel"], evs[2]["data"]["sender"]),
                         ("hub-dev", "plugin", "mod:update"))
        self.assertIn("the hub's code is untouched", evs[2]["data"]["text"])

    def test_the_failed_update_records_the_machine_load(self):
        # issue #178: a red under load cannot be told from a real one — update.failed carries the load before the merge and
        # when the tests finished (also on a timeout: there is no output then); behaviour is the same. If the load cannot be
        # read (Windows) — the field is absent
        from unittest import mock
        from modules.update import update
        self.upstream()
        loads = iter([2.04, 7.96])
        with mock.patch.object(update.os, "cpu_count", lambda: 12), mock.patch.object(update, "load", lambda: next(loads)):
            self.assertEqual(self.updater(ok=False).check(), "red")
        (ev,) = [e for e in self.spooled() if e["type"] == "update.failed"]
        self.assertEqual(ev["data"]["load"], {"cpus": 12, "before": 2.0, "after": 8.0})

        def stuck(repo):
            raise TimeoutError("1200 s")
        cfg = {"update": {"enabled": True, "remote": "origin", "branch": "main", "channel": "main"}, "dev": {"name": "hub-dev"}}
        loads = iter([1.0, 3.0])
        with mock.patch.object(update.os, "cpu_count", lambda: 4), mock.patch.object(update, "load", lambda: next(loads)):
            self.assertEqual(update.Updater(self.mine, self.state, cfg, tests=stuck).check(), "red")
        (ev,) = [e for e in self.spooled() if e["type"] == "update.failed"]
        self.assertEqual(ev["data"]["load"], {"cpus": 4, "before": 1.0, "after": 3.0})
        with mock.patch.object(update, "load", lambda: None):
            self.assertEqual(update.Updater(self.mine, self.state, cfg, tests=stuck).check(), "red")
        (ev,) = [e for e in self.spooled() if e["type"] == "update.failed"]
        self.assertNotIn("load", ev["data"])
        self.assertIsNotNone(update.load()) if hasattr(update.os, "getloadavg") else self.assertIsNone(update.load())

    def test_the_failed_update_names_the_red_tests(self):
        # the end of the output is cut to 500 characters, there may be more red tests — their names go in a separate field
        from modules.update import update
        self.upstream()
        out = "FAIL: test_x (tests.test_a.A.test_x)\nERROR: test_y (tests.test_b.B.test_y)\n" + "." * 800
        cfg = {"update": {"enabled": True, "remote": "origin", "branch": "main", "channel": "main"}, "dev": {"name": "hub-dev"}}
        names = ["tests.test_a.A.test_x", "tests.test_b.B.test_y"]
        u = update.Updater(self.mine, self.state, cfg, tests=lambda repo: (False, out, names))
        self.assertEqual(u.check(), "red")
        (failed,) = [e for e in self.spooled() if e["type"] == "update.failed"]
        self.assertEqual(failed["data"]["failed"], names)
        self.assertEqual((failed["data"]["why"], failed["data"]["tail"]), ("tests", out[-500:]))   # the old keys are as they were

    def test_a_test_run_that_returns_only_a_tail_gets_the_names_from_it(self):
        from modules.update import update
        self.upstream()
        cfg = {"update": {"enabled": True, "remote": "origin", "branch": "main", "channel": "main"}, "dev": {"name": "hub-dev"}}
        u = update.Updater(self.mine, self.state, cfg, tests=lambda repo: (False, "FAIL: test_x (tests.test_a.A.test_x)\nFAILED"))
        self.assertEqual(u.check(), "red")
        (failed,) = [e for e in self.spooled() if e["type"] == "update.failed"]
        self.assertEqual(failed["data"]["failed"], ["tests.test_a.A.test_x"])

    def test_the_message_to_the_developer_lists_the_red_tests_when_there_are_any(self):
        from modules.update import update
        cfg = {"update": {"enabled": True, "remote": "origin", "branch": "main", "channel": "main"}, "dev": {"name": "hub-dev"}}
        names = [f"tests.test_a.A.test_{i}" for i in range(12)]
        cases = ((names[:2], "Failed: tests.test_a.A.test_0, tests.test_a.A.test_1\n"),
                 (names, "Failed: " + ", ".join(names[:10]) + " …2 more\n"), ([], None))
        for n, (failed, expect) in enumerate(cases, 3):
            self.upstream(text=f"a = {n}\n")
            u = update.Updater(self.mine, self.state, cfg, tests=lambda repo, f=failed: (False, "FAILED", f))
            self.assertEqual(u.check(), "red")
            (msg,) = [e for e in self.spooled() if e["type"] == "message.received"][-1:]
            text = msg["data"]["text"]
            if expect:
                self.assertIn(f"The end of the output:\nFAILED\n{expect}Find", text)
            else:
                self.assertNotIn("Failed:", text)

    def test_many_red_tests_are_counted_in_full_and_cut_only_in_the_journal(self):
        # review: the list was cut to 30 before counting — «…20 more» with 200 failed
        from modules.update import update
        self.upstream()
        cfg = {"update": {"enabled": True, "remote": "origin", "branch": "main", "channel": "main"}, "dev": {"name": "hub-dev"}}
        names = [f"tests.test_a.A.test_{i}" for i in range(update.MAX_FAILED + 10)]
        u = update.Updater(self.mine, self.state, cfg, tests=lambda repo: (False, "FAILED", names))
        self.assertEqual(u.check(), "red")
        evs = self.spooled()   # reads and clears the spool — once
        (failed,) = [e for e in evs if e["type"] == "update.failed"]
        self.assertEqual(failed["data"]["failed"], names[:update.MAX_FAILED])
        (msg,) = [e for e in evs if e["type"] == "message.received"]
        self.assertIn(f" …{len(names) - 10} more\n", msg["data"]["text"])

    def test_a_red_pair_is_not_rechecked_until_something_changes(self):
        # a red upstream was rechecked every hour — 20 minutes of tests and browsers, with the same code
        from modules.update import update
        runs = []
        cfg = {"update": {"enabled": True, "remote": "origin", "branch": "main", "channel": "main"}, "dev": {"name": "hub-dev"}}
        def red(repo):
            runs.append(1)
            return False, "FAILED"
        self.upstream()
        for _ in range(3):
            self.assertEqual(update.Updater(self.mine, self.state, cfg, tests=red).check(), "red")
        self.assertEqual(len(runs), update.RED_TIMES)                           # red twice — we do not run it further
        self.upstream("core.py", "a = 3\n", "another")   # upstream moved — we check again
        self.assertEqual(update.Updater(self.mine, self.state, cfg, tests=red).check(), "red")
        self.assertEqual(len(runs), update.RED_TIMES + 1)

    def test_the_owner_asking_for_the_update_is_tested_again_on_a_red_pair(self):
        # PR review: the red pair cache is only for the schedule; hub update after a fix must not silently return red
        from modules.update import update
        runs = []
        cfg = {"update": {"enabled": True, "remote": "origin", "branch": "main", "channel": "main"}, "dev": {"name": "hub-dev"}}
        def red(repo):
            runs.append(1)
            return False, "FAILED"
        self.upstream()
        update.Updater(self.mine, self.state, cfg, tests=red).check()
        update.Updater(self.mine, self.state, cfg, tests=red).check(manual=True)
        self.assertEqual(len(runs), 2)

    def test_a_red_pair_does_not_wait_for_the_machine_lock_unless_the_owner_asks(self):
        # PR review: a scheduled pass waited for the lock for up to an hour to return a cached red, and did not hear the owner's flags
        import contextlib
        from modules.update import update
        cfg = {"update": {"enabled": True, "remote": "origin", "branch": "main", "channel": "main"}, "dev": {"name": "hub-dev"}}
        runs, turns = [], []
        def red(repo):
            runs.append(1)
            return False, "FAILED"
        @contextlib.contextmanager
        def turn(patience):
            turns.append(1)
            yield
        self.upstream()
        update.Updater(self.mine, self.state, cfg, tests=red).check()
        update.Updater(self.mine, self.state, cfg, tests=red).check()
        self.assertEqual((len(runs), len(turns)), (2, 0))
        self.assertEqual(update.Updater(self.mine, self.state, cfg, tests=red, turn=turn).check(), "red")
        self.assertEqual((len(runs), len(turns)), (2, 0))                      # the lock was not taken, the tests were not run
        update.Updater(self.mine, self.state, cfg, tests=red, turn=turn).check(manual=True)
        self.assertEqual((len(runs), len(turns)), (3, 1))                      # the owner asks — again, under the lock

    def test_tests_that_did_not_finish_do_not_make_a_red_pair(self):
        # PR review: the tests did not run (timeout, failure) — the pair is not remembered, the update is not blocked until a new commit
        import subprocess as sp
        from modules.update import update
        cfg = {"update": {"enabled": True, "remote": "origin", "branch": "main", "channel": "main"}, "dev": {"name": "hub-dev"}}
        def slow(repo):
            raise sp.TimeoutExpired("unittest", 1200)
        self.upstream()
        u = update.Updater(self.mine, self.state, cfg, tests=slow)
        self.assertEqual(u.check(), "red")
        self.assertEqual(u.red_seen(), "")

    def test_the_machine_lock_is_taken_before_the_merge(self):
        # PR review: the lock after the merge kept merged unverified code in the working copy for up to an hour of waiting
        from modules.update import update
        before = self.g(self.mine, "rev-parse", "HEAD")
        self.upstream()
        cfg = {"update": {"enabled": True, "remote": "origin", "branch": "main", "channel": "main"}, "dev": {"name": "hub-dev"}}
        seen = []
        import contextlib
        @contextlib.contextmanager
        def turn(patience):
            seen.append(self.g(self.mine, "rev-parse", "HEAD"))   # the merge had not yet happened inside the lock
            yield
        u = update.Updater(self.mine, self.state, cfg, tests=lambda repo: (True, "OK"), turn=turn)
        self.assertEqual(u.check(), "applied")
        self.assertEqual(seen, [before])

    def test_a_lock_that_cannot_be_taken_leaves_the_checkout_untouched(self):
        from modules.update import update
        before = self.g(self.mine, "rev-parse", "HEAD")
        self.upstream()
        cfg = {"update": {"enabled": True, "remote": "origin", "branch": "main", "channel": "main"}, "dev": {"name": "hub-dev"}}
        def stuck(patience):
            raise RuntimeError("another full test run has been going on the machine for an hour")
        u = update.Updater(self.mine, self.state, cfg, tests=lambda repo: (True, "OK"), turn=stuck)
        with self.assertRaises(RuntimeError):
            u.check()
        self.assertEqual(self.g(self.mine, "rev-parse", "HEAD"), before)
        self.assertEqual(u.red_seen(), "")

    def test_a_scheduled_pass_does_not_wait_for_a_busy_machine_lock(self):
        # PR review: a scheduled install waited for the lock for up to an hour — the update loop did not hear the owner's buttons
        from unittest import mock
        from core import lock, testlock
        from modules.update import update
        before = self.g(self.mine, "rev-parse", "HEAD")
        self.upstream()
        cfg = {"update": {"enabled": True, "remote": "origin", "branch": "main", "channel": "main"}, "dev": {"name": "hub-dev"}}
        path = self.os.path.join(self.root, "full.lock")
        held = lock.acquire(path)
        self.addCleanup(held.close)
        slept = []
        with mock.patch.object(testlock, "PATH", path), mock.patch.object(testlock, "_sleep", slept.append), \
                mock.patch.object(testlock, "settings", return_value=None), mock.patch.object(update, "LOCK_WAIT", 20):
            u = update.Updater(self.mine, self.state, cfg, tests=lambda repo: (True, "OK"), turn=update.machine_turn)
            self.assertEqual(u.check(), "busy")
            self.assertEqual(slept, [])                                          # did not wait
            with self.assertRaises(testlock.Busy):
                u.check(manual=True)                                             # the owner asks — waits LOCK_WAIT
            self.assertEqual(slept, [10, 10])
        self.assertEqual(self.g(self.mine, "rev-parse", "HEAD"), before)

    def test_one_red_is_tested_again_and_a_flake_does_not_block_the_update(self):
        # PR review: the red could be accidental (page tests under load) — a single scheduled red does not block
        from modules.update import update
        cfg = {"update": {"enabled": True, "remote": "origin", "branch": "main", "channel": "main"}, "dev": {"name": "hub-dev"}}
        results = [(False, "FAILED"), (True, "OK")]
        self.upstream()
        u = update.Updater(self.mine, self.state, cfg, tests=lambda repo: results.pop(0))
        self.assertEqual(u.check(), "red")
        self.assertEqual(u.red_seen(), "")                                      # a single red — not remembered
        self.assertEqual(update.Updater(self.mine, self.state, cfg, tests=lambda repo: results.pop(0)).check(),
                         "applied")

    def test_a_red_pair_from_an_old_version_of_the_file_is_not_remembered(self):
        # before this fix the file held only the pair, without a count
        from modules.update import update
        with open(self.os.path.join(self.state, update.RED_FLAG), "w") as f:
            f.write("u1 h1")
        self.assertEqual(update.Updater(self.mine, self.state, {}).red_seen(), "")

    def test_green_forgets_the_red_pair(self):
        from modules.update import update
        cfg = {"update": {"enabled": True, "remote": "origin", "branch": "main", "channel": "main"}, "dev": {"name": "hub-dev"}}
        self.upstream()
        self.assertEqual(self.updater(ok=False).check(), "red")
        self.assertEqual(self.updater(ok=False).check(), "red")
        self.assertTrue(update.Updater(self.mine, self.state, cfg).red_seen())
        self.g(self.mine, "commit", "-q", "--allow-empty", "-m", "fix")   # our code moved — check again
        self.assertEqual(self.updater(ok=True).check(), "applied")
        self.assertEqual(update.Updater(self.mine, self.state, cfg).red_seen(), "")

    def test_tests_that_time_out_are_red_and_roll_back(self):
        # analysis: TimeoutExpired left merged unverified code in place — «current» on the next pass
        import subprocess as sp
        from modules.update import update
        before = self.g(self.mine, "rev-parse", "HEAD")
        self.upstream()
        cfg = {"update": {"enabled": True, "remote": "origin", "branch": "main", "channel": "main"}, "dev": {"name": "hub-dev"}}
        def slow(repo):
            raise sp.TimeoutExpired("unittest", 1200)
        self.assertEqual(update.Updater(self.mine, self.state, cfg, tests=slow).check(), "red")
        self.assertEqual(self.g(self.mine, "rev-parse", "HEAD"), before)

    def test_red_rollback_keeps_commits_made_meanwhile(self):
        # analysis: reset --hard erased what an executor merged into main while the tests ran (now the check runs in a copy,
        # and the live tree is not touched)
        from modules.update import update
        self.upstream()
        cfg = {"update": {"enabled": True, "remote": "origin", "branch": "main", "channel": "main"}, "dev": {"name": "hub-dev"}}
        def red_after_a_commit(repo):
            self.write(self.mine, "mine-during-tests.py", "x = 1\n")
            self.g(self.mine, "add", "-A"); self.g(self.mine, "commit", "-qm", "work merged while tests ran")
            return False, "FAILED (failures=1)"
        self.assertEqual(update.Updater(self.mine, self.state, cfg, tests=red_after_a_commit).check(), "red")
        log = self.g(self.mine, "log", "--format=%s")
        self.assertIn("work merged while tests ran", log)                       # the other work is in place
        self.assertFalse(self.os.path.exists(self.os.path.join(self.mine, "core.py")) and
                         open(self.os.path.join(self.mine, "core.py")).read() == "a = 2\n")   # the update was backed out

    def test_red_rollback_keeps_commits_made_while_waiting_for_the_lock(self):
        # PR review: before was taken before the lock (we wait for it up to an hour) — a commit in that time was erased by the reset --hard rollback
        import contextlib
        from modules.update import update
        self.upstream()
        cfg = {"update": {"enabled": True, "remote": "origin", "branch": "main", "channel": "main"}, "dev": {"name": "hub-dev"}}
        @contextlib.contextmanager
        def turn(patience):
            self.write(self.mine, "mine-while-waiting.py", "x = 1\n")
            self.g(self.mine, "add", "-A"); self.g(self.mine, "commit", "-qm", "work committed while waiting")
            yield
        u = update.Updater(self.mine, self.state, cfg, tests=lambda repo: (False, "FAILED (failures=1)"), turn=turn)
        self.assertEqual(u.check(), "red")
        self.assertIn("work committed while waiting", self.g(self.mine, "log", "--format=%s"))
        with open(self.os.path.join(self.mine, "core.py")) as f:
            self.assertEqual(f.read(), "a = 1\n")                                # the update was backed out

    def test_upstream_merged_by_hand_while_waiting_for_the_lock_is_current(self):
        # PR review: after waiting for the lock the commit count and the «what's new» were the old ones — what was already merged went into the report
        import contextlib
        from modules.update import update
        self.upstream()
        cfg = {"update": {"enabled": True, "remote": "origin", "branch": "main", "channel": "main"}, "dev": {"name": "hub-dev"}}
        runs = []
        @contextlib.contextmanager
        def turn(patience):
            self.g(self.mine, "merge", "-q", "--no-edit", "origin/main")
            yield
        u = update.Updater(self.mine, self.state, cfg, tests=lambda repo: runs.append(1) or (True, "OK"), turn=turn)
        self.assertEqual(u.check(), "current")
        self.assertEqual(runs, [])
        (ev,) = [e for e in self.spooled() if e["type"] == "update.checked"]
        self.assertEqual(ev["data"]["behind"], 0)

    def test_edits_left_while_waiting_for_the_lock_stop_the_update(self):
        import contextlib
        from modules.update import update
        before = self.g(self.mine, "rev-parse", "HEAD")
        self.upstream()
        cfg = {"update": {"enabled": True, "remote": "origin", "branch": "main", "channel": "main"}, "dev": {"name": "hub-dev"}}
        @contextlib.contextmanager
        def turn(patience):
            self.write(self.mine, "personal.py", "dirty = 1\n")
            yield
        u = update.Updater(self.mine, self.state, cfg, tests=lambda repo: (True, "OK"), turn=turn)
        self.assertEqual(u.check(), "dirty")
        self.assertEqual(self.g(self.mine, "rev-parse", "HEAD"), before)
        with open(self.os.path.join(self.mine, "personal.py")) as f:
            self.assertEqual(f.read(), "dirty = 1\n")

    def test_an_interrupted_check_is_rolled_back_on_the_next_start(self):
        # analysis: update killed in the middle of tests — no rollback, unverified code came up on restart
        from modules.update import update
        before = self.g(self.mine, "rev-parse", "HEAD").strip()
        self.upstream()
        self.g(self.mine, "fetch", "-q", "origin")
        self.g(self.mine, "merge", "-q", "--no-edit", "origin/main")
        merged = self.g(self.mine, "rev-parse", "HEAD").strip()
        with open(self.os.path.join(self.state, update.TESTING_FLAG), "w") as f:
            f.write(f"{before} {merged}")
        cfg = {"update": {"enabled": True, "remote": "origin", "branch": "main", "channel": "main"}, "dev": {"name": "hub-dev"}}
        update.Updater(self.mine, self.state, cfg).recover()
        self.assertEqual(self.g(self.mine, "rev-parse", "HEAD").strip(), before)
        self.assertFalse(self.os.path.exists(self.os.path.join(self.state, update.TESTING_FLAG)))

    def test_an_interrupted_check_keeps_edits_of_the_tree_before_rolling_back(self):
        # the rollback resets the tree: an edit left in it goes into the code's stash, not away
        from modules.update import update
        before = self.g(self.mine, "rev-parse", "HEAD").strip()
        self.upstream()
        self.g(self.mine, "fetch", "-q", "origin")
        self.g(self.mine, "merge", "-q", "--no-edit", "origin/main")
        merged = self.g(self.mine, "rev-parse", "HEAD").strip()
        self.write(self.mine, "personal.py", "mine = 'edited'\n")
        with open(self.os.path.join(self.state, update.TESTING_FLAG), "w") as f:
            f.write(f"{before} {merged}")
        cfg = {"update": {"enabled": True, "remote": "origin", "branch": "main", "channel": "main"}, "dev": {"name": "hub-dev"}}
        update.Updater(self.mine, self.state, cfg).recover()
        self.assertEqual(self.g(self.mine, "rev-parse", "HEAD").strip(), before)
        self.assertIn("mine = 'edited'", self.g(self.mine, "stash", "show", "-p", "stash@{0}"))

    def test_edits_that_cannot_be_kept_stop_the_rollback_and_the_dev_is_told_so(self):
        from unittest import mock
        from modules.update import update
        before = self.g(self.mine, "rev-parse", "HEAD").strip()
        self.upstream()
        self.g(self.mine, "fetch", "-q", "origin")
        self.g(self.mine, "merge", "-q", "--no-edit", "origin/main")
        merged = self.g(self.mine, "rev-parse", "HEAD").strip()
        self.write(self.mine, "personal.py", "mine = 'edited'\n")
        with open(self.os.path.join(self.state, update.TESTING_FLAG), "w") as f:
            f.write(f"{before} {merged}")
        cfg = {"update": {"enabled": True, "remote": "origin", "branch": "main", "channel": "main"}, "dev": {"name": "hub-dev"}}
        with mock.patch.object(update.Updater, "_keep_edits", return_value=False):
            update.Updater(self.mine, self.state, cfg).recover()
        self.assertEqual(self.g(self.mine, "rev-parse", "HEAD").strip(), merged)
        with open(self.os.path.join(self.mine, "personal.py")) as f:
            self.assertEqual(f.read(), "mine = 'edited'\n")
        told = [e["data"]["text"] for e in self.spooled() if e["type"] == "message.received"]
        self.assertTrue(told and "did NOT roll back" in told[-1], told)
        self.assertTrue(self.os.path.exists(self.os.path.join(self.state, update.TESTING_FLAG)))   # tried again later

    def test_conflict_and_dirty_tree(self):
        self.write(self.mine, "core.py", "a = 'mine'\n")
        self.g(self.mine, "commit", "-qam", "personal edit of core")
        self.upstream()
        u = self.updater()
        self.assertEqual(u.check(), "conflict")
        self.assertEqual(self.g(self.mine, "status", "--porcelain"), "")       # the merge was aborted, the tree is clean
        self.assertIn("core.py", [e for e in self.spooled() if e["type"] == "message.received"][0]["data"]["text"])
        self.assertEqual(u.check(), "conflict")
        self.assertEqual([e["type"] for e in self.spooled()], ["update.failed", "update.checked"])  # to the developer — once
        self.write(self.mine, "personal.py", "dirty = 1\n")
        self.assertEqual(self.updater().check(), "dirty")

    def test_the_developer_hears_about_a_release_once_not_every_hour(self):
        # issue #260: main() builds a new Updater for every check — the «already told» memory lived for one check, and
        # the conflict of the same release reached the developer every hour (5 times in a row); the test checked the same object
        told = lambda: [e["data"]["text"] for e in self.spooled() if e["type"] == "message.received"]  # noqa: E731
        self.write(self.mine, "core.py", "a = 'mine'\n")
        self.g(self.mine, "commit", "-qam", "personal edit of core")
        self.upstream()
        self.assertEqual(self.updater().check(), "conflict")
        self.assertEqual(len(told()), 1)
        self.assertEqual(self.updater().check(), "conflict")                  # the next hour — a new Updater
        self.assertEqual(told(), [])
        u = self.updater()
        self.assertEqual(u.check(manual=True), "conflict")                     # the owner asks — say it again
        self.assertEqual(len(told()), 1)
        self.write(self.mine, "other.py", "x = 1\n")                           # committed their own work, and the conflict is the same — again
        self.g(self.mine, "add", "-A"); self.g(self.mine, "commit", "-qm", "another personal commit")
        self.assertEqual(self.updater().check(), "conflict")
        self.assertEqual(len(told()), 1)
        from unittest import mock
        from core import spool
        real = spool.put

        def broken(folder, type, *a, **kw):
            if type == "message.received":
                raise OSError("disk full")
            return real(folder, type, *a, **kw)
        self.g(self.mine, "commit", "-q", "--allow-empty", "-m", "one more")
        with mock.patch.object(spool, "put", broken), self.assertRaises(OSError):   # it did not go out — we did not remember it
            self.updater().check()
        self.assertEqual(told(), [])
        self.assertEqual(self.updater().check(), "conflict")
        self.assertEqual(len(told()), 1)
        self.write(self.mine, "personal.py", "dirty = 1\n")
        self.assertEqual(self.updater().check(), "dirty")
        self.assertEqual(len(told()), 1)
        self.assertEqual(self.updater().check(), "dirty")
        self.assertEqual(told(), [])
        self.upstream("core.py", "a = 3\n", "next release")                  # a new commit in the shared repo — a new message
        self.assertEqual(self.updater().check(), "dirty")
        self.assertEqual(len(told()), 1)

    def test_a_red_pair_is_told_once_and_a_new_local_fix_is_told_again(self):
        # issue #260: a red pair is checked on the schedule twice (RED_TIMES) — one message; the developer committed a
        # fix and the update is red again — they must find out about it
        told = lambda: [e for e in self.spooled() if e["type"] == "message.received"]  # noqa: E731
        self.upstream()
        self.assertEqual(self.updater(ok=False).check(), "red")
        self.assertEqual(len(told()), 1)
        self.assertEqual(self.updater(ok=False).check(), "red")
        self.assertEqual(told(), [])
        self.write(self.mine, "personal.py", "mine = 2\n")
        self.g(self.mine, "commit", "-qam", "my fix")
        self.assertEqual(self.updater(ok=False).check(), "red")
        self.assertEqual(len(told()), 1)
        # the tests did not finish (timeout), and the next hour they finished and failed on the same pair — report it with their output
        from modules.update import update
        self.g(self.mine, "commit", "-q", "--allow-empty", "-m", "my second fix")
        cfg = {"update": {"enabled": True, "remote": "origin", "branch": "main", "channel": "main"}, "dev": {"name": "hub-dev"}}

        def stuck(repo):
            raise TimeoutError("1200 s")
        self.assertEqual(update.Updater(self.mine, self.state, cfg, tests=stuck).check(), "red")
        self.assertEqual(len(told()), 1)
        self.assertEqual(self.updater(ok=False).check(), "red")
        self.assertEqual(len(told()), 1)

    def test_no_remote_is_the_upstream_itself(self):
        self.g(self.mine, "remote", "remove", "origin")
        self.assertEqual(self.updater().check(), "no-remote")
        (e,) = self.spooled()
        self.assertEqual((e["type"], e["data"]["result"]), ("update.checked", "no-remote"))

    def test_every_check_tells_the_page(self):
        # owner request: check for updates from the «About the hub» tab: the result of every check is a fact in the
        # journal, and the page shows it and what to do next
        from modules.update import update
        u = update.Updater(self.mine, self.state, {"update": {"enabled": False}})
        self.assertEqual(u.check(), "disabled")
        self.assertEqual(self.updater().check(), "current")
        self.upstream()
        self.write(self.mine, "personal.py", "dirty = 1\n")
        self.assertEqual(self.updater().check(), "dirty")
        got = [(e["data"]["result"], e["data"].get("behind")) for e in self.spooled() if e["type"] == "update.checked"]
        self.assertEqual(got, [("disabled", None), ("current", 0), ("dirty", 1)])
        from core import reducer
        st, _, why = reducer.reduce(reducer.empty(), {"seq": 1, "ts": 50, "type": "update.checked", "entity": None,
                                                     "data": {"result": "dirty", "behind": 1, "notes": ["x"],
                                                              "channel": "cli", "owner": True}})
        self.assertIsNone(why)
        self.assertEqual(st["update_check"], {"result": "dirty", "behind": 1, "notes": ["x"], "error": None, "ts": 50})

    def test_a_following_hub_tells_whether_its_code_is_fresh(self):
        # a second hub on the same folder (update.follow): the first merges the code, but the check tells the truth rather than «disabled»
        from modules.update import update
        follow = lambda: update.Updater(self.mine, self.state, {"update": {"enabled": False, "follow": True}})  # noqa: E731
        head = self.g(self.mine, "rev-parse", "HEAD")
        self.assertEqual(follow().check(apply=False), "following")
        self.upstream(msg="new feature")
        self.g(self.author, "tag", "v1.0.0")
        self.g(self.author, "push", "-q", "origin", "v1.0.0")
        self.write(self.mine, "personal.py", "dirty = 1\n")                 # edits in the tree do not interfere with the check
        self.first_hub_fetched()
        self.assertEqual(follow().check(), "following")
        self.assertEqual(self.g(self.mine, "rev-parse", "HEAD"), head)      # nothing was merged
        self.g(self.mine, "checkout", "personal.py")
        self.g(self.mine, "merge", "-q", "--no-edit", "origin/main")
        self.assertEqual(follow().check(), "following")
        first, second, third = [e["data"] for e in self.spooled()]
        self.assertEqual((first["result"], first["behind"], first.get("version")), ("following", 0, None))
        # a hub from git follows main (core/version.channel): how many commits wait, no release to name as the target
        self.assertEqual((second["result"], second["behind"], second.get("version"), second.get("have_version")),
                         ("following", 1, None, None))
        self.assertEqual((third["behind"], third.get("version"), third["have_version"]), (0, None, "v1.0.0"))

    def first_hub_fetched(self):
        """The first hub on the same folder (update.enabled) fetched the shared repo with its own check — its fetch, not ours."""
        first = self.updater()
        first.info = {}
        first._target("origin", "main")

    def test_a_following_hub_does_not_fetch_into_the_shared_folder(self):
        # PR #259 review: the second hub's fetch would go into the shared code folder next to the first one's (ref locks,
        # FETCH_HEAD, the network twice) — it only looks at what the first hub has already fetched
        from modules.update import update
        follow = lambda: update.Updater(self.mine, self.state, {"update": {"enabled": False, "follow": True}})  # noqa: E731
        self.upstream(msg="new feature")
        self.g(self.author, "tag", "v1.0.0")
        self.g(self.author, "push", "-q", "origin", "v1.0.0")
        refs = self.g(self.mine, "for-each-ref")
        self.assertEqual(follow().check(), "following")
        self.assertEqual(self.g(self.mine, "for-each-ref"), refs)            # neither origin/main nor tags were fetched
        self.assertFalse(self.os.path.exists(self.os.path.join(self.mine, ".git", "FETCH_HEAD")))
        self.first_hub_fetched()
        self.g(self.mine, "remote", "set-url", "origin", self.os.path.join(self.root, "gone.git"))   # there is no network — none needed
        self.assertEqual(follow().check(), "following")
        first, second = [e["data"] for e in self.spooled()]
        self.assertEqual(first["behind"], 0)
        self.assertEqual((second["behind"], second.get("version")), (1, None))

    def test_following_check_reaches_the_page(self):
        from core import reducer
        st, _, why = reducer.reduce(reducer.empty(), {"seq": 1, "ts": 50, "type": "update.checked", "entity": None,
                                                     "data": {"result": "following", "behind": 1, "version": "v1.0.0",
                                                              "have_version": "v0.9.0", "channel": "cli", "owner": True}})
        self.assertIsNone(why)
        self.assertEqual((st["update_check"]["result"], st["update_check"]["version"], st["update_check"]["have_version"]),
                         ("following", "v1.0.0", "v0.9.0"))

    def test_disabled_without_follow_stays_disabled(self):
        from modules.update import update
        for up in ({"enabled": False}, {"enabled": False, "follow": False}):
            self.assertEqual(update.Updater(self.mine, self.state, {"update": up}).check(), "disabled")

    def test_disabled_without_an_origin_says_so(self):
        # a hub made by `hub instance new` from a clone without origin stayed off in silence: the page said only "turned off
        # in the settings" — now it names the cause (no address) and the way out
        from core import instance
        from modules.update import update
        self.g(self.mine, "remote", "remove", "origin")
        off = lambda up: update.Updater(self.mine, self.state, {"update": up}).check()  # noqa: E731
        self.assertEqual(off({"enabled": False}), "disabled")   # not an instance (a core developer's hub): as before
        instance.write(self.state, {"name": "work2", "ports": {"web": 8811, "files": 8812},
                                    "code": {"kind": "git", "path": self.mine, "origin": ""}})
        for up in ({"enabled": False}, {"enabled": False, "follow": False}):
            self.assertEqual(off(up), "no-origin")
        self.assertEqual(off({"enabled": True}), "no-origin")   # enabled before the address: not "the shared hub itself"
        self.assertEqual(off({"enabled": False, "follow": True}), "disabled")   # a second hub on the same code: as before
        self.g(self.mine, "remote", "add", "origin", self.mine)   # the address was added by hand, updates still off
        self.assertEqual(off({"enabled": False}), "disabled")

    def test_check_only_looks_and_install_applies(self):
        # owner decision: checking for an update is not verifying the code, it is just looking whether there is a new
        # version; the «Install» button installs it (and the hub itself once an hour)
        before = self.g(self.mine, "rev-parse", "HEAD")
        self.upstream(msg="new feature")
        self.assertEqual(self.updater().check(apply=False), "available")
        self.assertEqual(self.g(self.mine, "rev-parse", "HEAD"), before)        # nothing was merged
        (e,) = self.spooled()
        self.assertEqual((e["type"], e["data"]["result"], e["data"]["behind"]), ("update.checked", "available", 1))
        self.assertIn("new feature", e["data"]["notes"])
        self.assertEqual(self.updater().check(), "applied")

    def test_a_second_hub_on_the_same_code_restarts_when_the_code_changes(self):
        # a second hub on the same folder (its updates are off, the first merges the code) lived on old code until a
        # manual restart
        from modules.update import update
        off = {"update": {"enabled": False, "follow": True}}
        f = update.Follower(self.mine, self.state)
        self.assertIsNone(f.tick(off))
        self.upstream()
        self.g(self.mine, "fetch", "-q", "origin")
        self.g(self.mine, "merge", "-q", "--no-edit", "origin/main")
        new = self.g(self.mine, "rev-parse", "HEAD").strip()
        marker = update.testing_marker(self.mine)
        open(marker, "w").close()                                        # the first hub is still verifying the merged code with tests
        self.assertIsNone(f.tick(off))
        self.os.unlink(marker)
        self.assertIsNone(f.tick({"update": {"enabled": True, "follow": True}}))   # its own updates — a restart follows them anyway
        self.assertEqual(f.tick(off), new)
        self.assertIsNone(f.tick(off))                                   # it asks once
        web, ev = self.spooled()
        self.assertEqual((web["type"], web["data"]["parts"]), ("hub.restart_requested", ["web", "files"]))
        self.assertEqual((ev["type"], ev["data"]["parts"], ev["data"]["now"]), ("hub.restart_requested", [], False))

    def test_a_hub_without_follow_does_not_restart_when_the_code_changes(self):
        # PR review: a hub running from a development folder (updates off, the developer changes the code themselves) would
        # restart on every merge into main
        from modules.update import update
        f = update.Follower(self.mine, self.state)
        self.upstream()
        self.g(self.mine, "fetch", "-q", "origin")
        self.g(self.mine, "merge", "-q", "--no-edit", "origin/main")
        for cfg in ({"update": {"enabled": False}}, {}, {"update": {"enabled": False, "follow": False}}):
            self.assertIsNone(f.tick(cfg))
        self.assertEqual(self.spooled(), [])

    def test_flags_choose_what_to_do(self):
        import time
        from modules.update import update
        now = time.time()
        # the second — «the owner asks»: an install by flag, not by schedule (PR review: the flag was read twice)
        self.assertEqual(update.due(self.state, now, 3600, now), (None, False))
        self.assertEqual(update.due(self.state, now - 4000, 3600, now), ("install", False))   # once an hour — it installs by itself
        open(self.os.path.join(self.state, update.NOW_FLAG), "w").close()
        self.assertEqual(update.due(self.state, now, 3600, now), ("check", False))
        self.assertEqual(update.due(self.state, now, 3600, now), (None, False))              # the flag is cleared
        open(self.os.path.join(self.state, update.NOW_FLAG), "w").close()
        open(self.os.path.join(self.state, update.INSTALL_FLAG), "w").close()
        self.assertEqual(update.due(self.state, now, 3600, now), ("install", True))          # an install absorbs the check
        self.assertEqual(update.due(self.state, now, 3600, now), (None, False))


if __name__ == "__main__":
    unittest.main()
