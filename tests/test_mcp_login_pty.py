"""`hub mcp login` without a terminal (07.10, hub: "stdin isn't a terminal, so authentication can't be completed here"):
claude mcp login runs in a pseudo-terminal, Claude Code itself opens the sign-in in the owner's browser, and we wait
for the sign-in to finish."""
import tests  # noqa: F401 — keep the machine's settings out
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

from core import mcplogin
from tests import winsafe

FAKE = """import os, sys, time
if not os.isatty(0):
    print("stdin isn't a terminal"); sys.exit(1)
print("If the browser didn't open, visit:\\n  \\x1b]8;;https://mcp.example.com/authorize?code_challenge=x&redirect_uri=http%3A%2F%2Flocalhost%3A5%2Fcallback\\x07"
      "https://mcp.example.com/authorize?code_challenge=x\\x1b]8;;\\x07\\nWaiting for authorization…", flush=True)
time.sleep(1)
print("Authentication successful"); sys.exit(0)
"""


class Pty(unittest.TestCase):
    def test_runs_in_a_terminal_and_the_owner_signs_in_in_his_own_browser(self):
        # Claude Code itself opens the sign-in, in the owner's system browser; there is no second tab in the agents'
        # browser (07.10: "opens in the wrong place" — a tab in the agents' browser, with an address without client_id)
        d = tempfile.mkdtemp()
        script = os.path.join(d, "fake.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(FAKE)
        said = []
        code, out = mcplogin.login("x", d, timeout=20, argv=[sys.executable, script], say=said.append)
        self.assertEqual(code, 0, out)
        self.assertIn("Authentication successful", out)
        self.assertFalse(hasattr(mcplogin, "open_in"))
        self.assertEqual(said, ["the sign-in is open in this computer's browser; if it did not open — "
                                "https://mcp.example.com/authorize?"
                                "code_challenge=x&redirect_uri=http%3A%2F%2Flocalhost%3A5%2Fcallback"])

    @unittest.skipUnless(os.name == "nt", "POSIX: pty.fork starts even a missing program — the child exits with 127")
    def test_a_program_that_does_not_start_is_an_error_not_a_hang(self):
        # 16.10: cleanup after a failed start waited for output from a pipe whose other end the hub itself held
        import platforms
        import threading
        err = []
        t = threading.Thread(target=lambda: err.append(self.assertRaises(
            OSError, platforms.current.spawn_tty, ["no-such-program-x"], tempfile.gettempdir())), daemon=True)
        t.start()
        t.join(15)
        self.assertFalse(t.is_alive(), "spawn_tty hung")
        self.assertEqual(len(err), 1)

    def test_a_second_login_to_the_same_server_waits_for_the_first(self):
        # 16.10: the hub and a session, on one core message, logged in to the same server 4 s apart — two `claude mcp
        # login`s; the second client registration (DCR) overwrote the first's client_id: "Client ID … does not match
        # the authorize request"
        import threading
        d, run = tempfile.mkdtemp(), tempfile.mkdtemp()
        calls = os.path.join(d, "calls")
        script = os.path.join(d, "fake.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(f"open({calls!r}, 'a').write('x')\n" + FAKE)
        said, got = [], {}

        def go(who):
            got[who] = mcplogin.login("x", d, timeout=20, argv=[sys.executable, script], say=said.append, run_dir=run)
        first = threading.Thread(target=go, args=("first",), daemon=True)
        first.start()
        end = time.time() + 10
        while not os.path.exists(calls) and first.is_alive() and time.time() < end:
            time.sleep(0.05)
        self.assertTrue(os.path.exists(calls), "the first login did not start")
        go("second")
        first.join()
        with open(calls) as f:
            self.assertEqual(f.read(), "x")                    # one sign-in window for the user
        self.assertEqual(got["second"], got["first"])
        self.assertEqual(got["first"][0], 0, got["first"][1])
        self.assertIn("the sign-in to x is already going — waiting for its result, not opening a second sign-in window", said)
        got.clear()
        go("again")                                             # the sign-in is over — the next one signs in by itself
        with open(calls) as f:
            self.assertEqual(f.read(), "xx")
        self.assertEqual(got["again"][0], 0)


class NotStarted(unittest.TestCase):
    """A sign-in that did not start (no pseudo-terminal on Windows, no claude) leaves the folder as it was: the trust
    mark it set is removed, and nothing is said about it."""

    def setUp(self):
        import json
        from unittest import mock
        self.conf, self.cwd = tempfile.mkdtemp(), tempfile.mkdtemp()
        with open(os.path.join(self.cwd, ".mcp.json"), "w") as f:
            json.dump({"mcpServers": {"x": {"type": "http", "url": "https://x.example.com/mcp"}}}, f)
        self.cfg = os.path.join(self.conf, ".claude.json")
        with open(self.cfg, "w") as f:   # the folder is already known to Claude Code: no trust, another server approved
            json.dump({"projects": {"/other": {"hasTrustDialogAccepted": True},
                                    self.key(): {"enabledMcpjsonServers": ["y"], "allowedTools": []}}}, f)
        env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.conf})
        env.start()
        self.addCleanup(env.stop)

    def projects(self):
        import json
        with open(self.cfg) as f:
            return json.load(f)["projects"]

    def key(self):
        return os.path.realpath(self.cwd).replace(os.sep, "/")

    def assert_untouched(self):
        # both the trust mark and the approval of server x that this sign-in set are removed; others' stay in place
        self.assertEqual(self.projects()[self.key()], {"enabledMcpjsonServers": ["y"], "allowedTools": []})
        self.assertTrue(self.projects()["/other"]["hasTrustDialogAccepted"])

    def test_no_terminal_takes_the_trust_back(self):
        from unittest import mock
        import platforms
        said = []
        with mock.patch.object(platforms.current, "spawn_tty", side_effect=OSError("no terminal here")):
            code, out = mcplogin.login("x", self.cwd, say=said.append)
        self.assertNotEqual(code, 0)
        self.assertIn("no terminal here", out)
        self.assertEqual(said, [])
        self.assert_untouched()

    def test_no_claude_to_run_takes_the_trust_back(self):
        # POSIX: the terminal forks first and then runs claude — no claude means an exit code; Windows: a launch OSError
        from unittest import mock
        said = []
        with mock.patch.dict(os.environ, {"PATH": tempfile.mkdtemp()}):
            code, out = mcplogin.login("x", self.cwd, timeout=20, say=said.append)
        self.assertNotEqual(code, 0)
        self.assertEqual(said, [])
        self.assert_untouched()

    def test_the_waiting_helper_reports_a_failed_start(self):
        from unittest import mock
        import json
        import platforms
        work = tempfile.mkdtemp()
        with mock.patch.object(platforms.current, "spawn_tty", side_effect=OSError("no terminal here")):
            mcplogin._wait("x", self.cwd, work)
        with open(os.path.join(work, "result")) as f:
            r = json.load(f)
        self.assertNotEqual(r["code"], 0)
        self.assertIn("no terminal here", r["out"])

    def test_a_link_sign_in_that_gave_no_link_takes_the_trust_back(self):
        from unittest import mock
        said = []
        with mock.patch.dict(os.environ, {"PATH": tempfile.mkdtemp()}):   # no claude
            url, why = mcplogin.start("x", self.cwd, tempfile.mkdtemp(), wait=20, say=said.append)
        self.assertIsNone(url)
        self.assertTrue(why)
        self.assertEqual(said, [])
        self.assert_untouched()


def own_root():
    """A session folder that is its own key: a `.git` above the temp folder (an empty one in a home folder) would key
    it by that root (_git_root), and the folder-itself expectations below would not hold."""
    work = tempfile.mkdtemp()
    os.mkdir(os.path.join(work, ".git"))
    return work


class Trust(unittest.TestCase):
    def test_an_untrusted_folder_is_marked_and_the_rest_of_the_config_kept(self):
        # 07.10: "wiki is from .mcp.json and awaiting approval" — the folder was not trusted
        import json
        d, work = tempfile.mkdtemp(), own_root()
        cfg = os.path.join(d, ".claude.json")
        with open(cfg, "w") as f:
            json.dump({"numStartups": 7, "projects": {"/other": {"hasTrustDialogAccepted": True, "x": 1}}}, f)
        self.assertTrue(mcplogin.approve(work, None, config=cfg))
        self.assertFalse(mcplogin.approve(work, None, config=cfg))          # the second time — already trusted
        with open(cfg) as f:
            data = json.load(f)
        self.assertEqual(data["numStartups"], 7)
        self.assertEqual(data["projects"]["/other"], {"hasTrustDialogAccepted": True, "x": 1})
        self.assertTrue(data["projects"][os.path.realpath(work).replace(os.sep, "/")]["hasTrustDialogAccepted"])

    def test_only_the_server_signed_into_is_approved(self):
        # 17.10, claude 2.1.302: a trusted folder is not enough — '"x" is from .mcp.json and awaiting approval' until x
        # is in the folder's enabledMcpjsonServers. Only the server being signed into is approved, not everything from
        # .mcp.json; only our own mark is removed
        import json
        d, work = tempfile.mkdtemp(), own_root()
        with open(os.path.join(work, ".mcp.json"), "w") as f:
            json.dump({"mcpServers": {"x": {}, "z": {}}}, f)
        cfg = os.path.join(d, ".claude.json")
        key = os.path.realpath(work).replace(os.sep, "/")
        with open(cfg, "w") as f:
            json.dump({"projects": {key: {"hasTrustDialogAccepted": True, "enabledMcpjsonServers": ["y"]}}}, f)

        def proj():
            with open(cfg) as f:
                return json.load(f)["projects"].get(key)
        self.assertEqual(mcplogin.approve(work, "x", config=cfg), {"server"})
        self.assertEqual(proj(), {"hasTrustDialogAccepted": True, "enabledMcpjsonServers": ["y", "x"]})
        self.assertEqual(mcplogin.approve(work, "x", config=cfg), set())       # the second time — already approved
        mcplogin.withdraw(work, {"server"}, "x", config=cfg)
        self.assertEqual(proj(), {"hasTrustDialogAccepted": True, "enabledMcpjsonServers": ["y"]})
        mcplogin.withdraw(work, {"server"}, "y", config=cfg)                   # the last one — without an empty list
        self.assertEqual(proj(), {"hasTrustDialogAccepted": True})
        with open(os.path.join(d, ".mcp.json"), "w") as f:
            json.dump({"mcpServers": {"x": {}}}, f)
        self.assertEqual(mcplogin.approve(d, "x", config=cfg), {"trust", "server"})   # an unknown folder — trust too
        mcplogin.withdraw(d, {"trust", "server"}, "x", config=cfg)
        with open(cfg) as f:
            self.assertNotIn(os.path.realpath(d).replace(os.sep, "/"), json.load(f)["projects"])
        self.assertEqual(mcplogin.approve(work, "plugin:p:x", config=cfg), set())   # not from .mcp.json — not approved
        self.assertEqual(proj(), {"hasTrustDialogAccepted": True})

    def test_a_server_the_owner_rejected_stays_rejected(self):
        import json
        d, work = tempfile.mkdtemp(), own_root()
        with open(os.path.join(work, ".mcp.json"), "w") as f:
            json.dump({"mcpServers": {"x": {}}}, f)
        cfg = os.path.join(d, ".claude.json")
        key = os.path.realpath(work).replace(os.sep, "/")
        with open(cfg, "w") as f:
            json.dump({"projects": {key: {"hasTrustDialogAccepted": True, "disabledMcpjsonServers": ["x"]}}}, f)
        self.assertEqual(mcplogin.approve(work, "x", config=cfg), set())
        with open(cfg, "w") as f:   # a rejection that Claude Code wrote into the folder's settings
            json.dump({"projects": {key: {"hasTrustDialogAccepted": True}}}, f)
        os.makedirs(os.path.join(work, ".claude"))
        with open(os.path.join(work, ".claude", "settings.local.json"), "w") as f:
            json.dump({"disabledMcpjsonServers": ["x"]}, f)
        self.assertEqual(mcplogin.approve(work, "x", config=cfg), set())
        with open(os.path.join(work, ".mcp.json"), "w") as f:   # not a dict — it has no servers
            json.dump({"mcpServers": "x"}, f)
        os.remove(os.path.join(work, ".claude", "settings.local.json"))
        self.assertEqual(mcplogin.approve(work, "x", config=cfg), set())

    def _repo_with_worktree(self):
        """A main repo folder and a worktree of it, laid out as git does (`.git` file -> worktrees/<name>, commondir)."""
        import json
        base = os.path.realpath(tempfile.mkdtemp())
        main, wt = os.path.join(base, "maps"), os.path.join(base, "maps", ".claude", "worktrees", "w1")
        meta = os.path.join(main, ".git", "worktrees", "w1")
        os.makedirs(meta)
        os.makedirs(wt)
        with open(os.path.join(meta, "commondir"), "w") as f:
            f.write("../..\n")
        with open(os.path.join(wt, ".git"), "w") as f:
            f.write(f"gitdir: {meta}\n")
        with open(os.path.join(wt, ".mcp.json"), "w") as f:
            json.dump({"mcpServers": {"maps-api": {}}}, f)
        return main, wt

    def test_a_worktree_is_approved_under_the_main_repo_key(self):
        # 28.10, claude 2.1.309: the project key is the root of the git repo - for a worktree the MAIN repo (for a
        # subfolder the top of the repo); an approval under the worktree's own path was ignored - "awaiting approval"
        # in a session whose folder is a worktree
        import json
        main, wt = self._repo_with_worktree()
        cfg = os.path.join(tempfile.mkdtemp(), ".claude.json")
        mine = wt.replace(os.sep, "/")
        with open(cfg, "w") as f:   # an old entry under the worktree's own path (earlier hub versions) is not ours to touch
            json.dump({"projects": {mine: {"enabledMcpjsonServers": ["old"]}}}, f)

        def projects():
            with open(cfg) as f:
                return json.load(f)["projects"]
        self.assertEqual(mcplogin.approve(wt, "maps-api", config=cfg), {"trust", "server"})
        self.assertEqual(projects()[main.replace(os.sep, "/")],
                         {"hasTrustDialogAccepted": True, "enabledMcpjsonServers": ["maps-api"]})
        self.assertEqual(projects()[mine], {"enabledMcpjsonServers": ["old"]})
        self.assertEqual(mcplogin.approve(wt, "maps-api", config=cfg), set())
        mcplogin.withdraw(wt, {"trust", "server"}, "maps-api", config=cfg)
        self.assertEqual(projects(), {mine: {"enabledMcpjsonServers": ["old"]}})

    def test_a_subfolder_of_a_repo_uses_the_top_of_the_repo_as_the_key(self):
        import json
        main, _ = self._repo_with_worktree()
        sub = os.path.join(main, "app")
        os.makedirs(sub)
        with open(os.path.join(sub, ".mcp.json"), "w") as f:
            json.dump({"mcpServers": {"maps-api": {}}}, f)
        cfg = os.path.join(tempfile.mkdtemp(), ".claude.json")
        with open(cfg, "w") as f:
            json.dump({}, f)
        self.assertEqual(mcplogin.approve(sub, "maps-api", config=cfg), {"trust", "server"})
        with open(cfg) as f:
            self.assertEqual(list(json.load(f)["projects"]), [main.replace(os.sep, "/")])

    def test_a_rejection_in_the_main_repo_settings_holds_for_its_worktree(self):
        import json
        main, wt = self._repo_with_worktree()
        cfg = os.path.join(tempfile.mkdtemp(), ".claude.json")
        with open(cfg, "w") as f:
            json.dump({}, f)
        os.makedirs(os.path.join(main, ".claude"), exist_ok=True)
        with open(os.path.join(main, ".claude", "settings.local.json"), "w") as f:
            json.dump({"disabledMcpjsonServers": ["maps-api"]}, f)
        self.assertNotIn("server", mcplogin.approve(wt, "maps-api", config=cfg))   # at most the trust mark

    def test_the_folder_key_is_written_as_claude_code_reads_it(self):
        # 12.10, Windows: Claude Code looks the folder up in projects by a path with "/" (C:/Users/…); a mark under
        # C:\Users\… had no effect — "plugin:context7:context7 is from .mcp.json and awaiting approval" on every
        # hub mcp login
        import json
        from unittest import mock
        win = "C:\\Users\\u\\repo"   # a Windows path on macOS/Linux too: separator and realpath behave as on Windows

        def run(projects):
            cfg = os.path.join(tempfile.mkdtemp(), ".claude.json")
            with open(cfg, "w") as f:
                json.dump({"projects": projects}, f)
            with mock.patch("os.sep", "\\"), mock.patch("os.path.realpath", side_effect=lambda p, _r=os.path.realpath: p if p == win else _r(p)):
                marked = bool(mcplogin.approve(win, None, config=cfg))
            with open(cfg) as f:
                return marked, json.load(f)["projects"]

        self.assertEqual(run({}), (True, {"C:/Users/u/repo": {"hasTrustDialogAccepted": True}}))

    @unittest.skipIf(os.name == "nt", "links and file permissions")
    def test_a_linked_config_stays_a_link_and_keeps_its_mode(self):
        # ~/.claude.json in a dotfiles folder: replacing the link with a file would split the settings into two copies
        import json
        d, work = tempfile.mkdtemp(), tempfile.mkdtemp()
        real = os.path.join(d, "dotfiles", "claude.json")
        os.makedirs(os.path.dirname(real))
        with open(real, "w") as f:
            json.dump({"numStartups": 1}, f)
        os.chmod(real, 0o600)
        cfg = os.path.join(d, ".claude.json")
        winsafe.symlink(self, real, cfg)
        self.assertTrue(mcplogin.approve(work, None, config=cfg))
        self.assertTrue(os.path.islink(cfg))
        with open(real) as f:
            self.assertTrue(json.load(f)["projects"][os.path.realpath(work)]["hasTrustDialogAccepted"])
        self.assertEqual(os.stat(real).st_mode & 0o777, 0o600)

    def test_the_mark_waits_for_the_lock_and_keeps_what_another_writer_added(self):
        # two sign-ins at once (two servers) or Claude Code itself (`claude mcp add -s user`): a later writer must not
        # overwrite an earlier one's edit. Claude Code locks the settings with a neighbouring folder, <config>.lock
        # (proper-lockfile); the hub takes the same lock
        import json
        import threading
        d, work = tempfile.mkdtemp(), own_root()
        cfg = os.path.join(d, ".claude.json")
        with open(cfg, "w") as f:
            json.dump({"projects": {}}, f)
        done = []
        os.mkdir(cfg + ".lock")
        t = threading.Thread(target=lambda: done.append(bool(mcplogin.approve(work, None, config=cfg))))
        t.start()
        t.join(0.5)
        self.assertEqual(done, [])                                      # waits while another holds the lock
        with open(cfg, "w") as f:
            json.dump({"projects": {"/other": {"hasTrustDialogAccepted": True}}}, f)
        os.rmdir(cfg + ".lock")
        t.join(10)
        self.assertEqual(done, [True])
        self.assertFalse(os.path.exists(cfg + ".lock"))                 # released after the write
        with open(cfg) as f:
            data = json.load(f)
        self.assertEqual(set(data["projects"]), {"/other", os.path.realpath(work).replace(os.sep, "/")})

    def test_a_lock_that_cannot_be_taken_over_gives_up_in_time(self):
        # a file (or a non-empty folder) stands in place of the lock: rmdir cannot take it, and the wait must not loop forever
        import json
        import threading
        d, work = tempfile.mkdtemp(), tempfile.mkdtemp()
        cfg = os.path.join(d, ".claude.json")
        with open(cfg, "w") as f:
            json.dump({"projects": {}}, f)
        with open(cfg + ".lock", "w") as f:
            f.write("x")
        old = time.time() - 60
        os.utime(cfg + ".lock", (old, old))
        done = []
        with mock.patch.object(mcplogin, "LOCK_WAIT", 0.5):
            t = threading.Thread(target=lambda: done.append(bool(mcplogin.approve(work, None, config=cfg))), daemon=True)
            t.start()
            t.join(5)
        self.assertEqual(done, [False])   # gave up: no mark, the sign-in goes on without it

    def test_a_lock_left_over_is_taken_over(self):
        # a writer killed holding the lock leaves its folder behind; Claude Code treats it as abandoned after 10 s
        import json
        d, work = tempfile.mkdtemp(), tempfile.mkdtemp()
        cfg = os.path.join(d, ".claude.json")
        with open(cfg, "w") as f:
            json.dump({"projects": {}}, f)
        os.mkdir(cfg + ".lock")
        old = time.time() - 60
        os.utime(cfg + ".lock", (old, old))
        self.assertTrue(mcplogin.approve(work, None, config=cfg))
        self.assertEqual(sorted(os.listdir(d)), [".claude.json"])        # nothing of the old lock left behind

    def test_a_lock_taken_over_by_another_waiter_in_between_is_not_removed(self):
        # two waiters see the same abandoned lock; the first takes it over and makes its own; the second must not
        # remove that fresh lock of the first (both would write ~/.claude.json at once and one edit would be lost)
        import json
        # the lock path as the code sees it: it takes the settings by realpath (macOS: /var is /private/var)
        d, work = os.path.realpath(tempfile.mkdtemp()), tempfile.mkdtemp()
        cfg = os.path.join(d, ".claude.json")
        lock = cfg + ".lock"
        with open(cfg, "w") as f:
            json.dump({"projects": {}}, f)
        os.mkdir(lock)
        old = time.time() - 60
        os.utime(lock, (old, old))
        real, theirs = os.stat, []

        def stat(path, *a, **kw):
            got = real(path, *a, **kw)
            if path == lock and not theirs:   # right after this waiter looked: the other one takes the lock over
                os.rmdir(lock)
                os.mkdir(lock)
                theirs.append(real(lock))
            return got
        with mock.patch.object(mcplogin, "LOCK_WAIT", 0.5), mock.patch.object(mcplogin.os, "stat", stat):
            done = mcplogin.approve(work, None, config=cfg)
        self.assertTrue(theirs)                                         # the other waiter did come in between
        self.assertFalse(done)                                          # waited for it and gave up in time
        now = os.stat(lock)
        # portable: the same folder by its identity (inode and the time it was made with), not an order of two writes
        self.assertEqual((now.st_ino, now.st_mtime_ns), (theirs[0].st_ino, theirs[0].st_mtime_ns))   # theirs, in place
        self.assertEqual(sorted(os.listdir(d)), [".claude.json", ".claude.json.lock"])

    def test_waiters_of_the_hub_take_a_leftover_over_one_at_a_time(self):
        # the look "still the folder I saw" and the removal are two steps: two waiters may both look before either
        # removes; so a waiter of the hub takes a leftover over only while no other is doing it
        import json
        import threading
        import platforms
        from core import catalog
        d, work = tempfile.mkdtemp(), tempfile.mkdtemp()
        cfg = os.path.join(d, ".claude.json")
        with open(cfg, "w") as f:
            json.dump({"projects": {}}, f)
        os.mkdir(cfg + ".lock")
        old = time.time() - 60
        os.utime(cfg + ".lock", (old, old))
        done = []
        os.makedirs(catalog.config_dir(), exist_ok=True)
        with platforms.current.exclusive(os.path.join(catalog.config_dir(), "claude-settings.lock")):
            t = threading.Thread(target=lambda: done.append(bool(mcplogin.approve(work, None, config=cfg))), daemon=True)
            t.start()
            t.join(0.5)
            self.assertEqual(done, [])
            # portable: the time was set by os.utime above, the folder is the same one
            self.assertEqual(os.stat(cfg + ".lock").st_mtime, old)        # not removed while another takes it over
        t.join(10)
        self.assertEqual(done, [True])
        # no settings folder of the hub to lock in (a file stands in its place): a leftover is still taken over
        os.mkdir(cfg + ".lock")
        os.utime(cfg + ".lock", (old, old))
        with open(os.path.join(d, "not-a-folder"), "w") as f:
            f.write("x")
        with mock.patch.dict(os.environ, {"HUB_CONFIG_DIR": os.path.join(d, "not-a-folder", "cfg")}), \
                mock.patch.object(mcplogin, "LOCK_WAIT", 2):
            self.assertTrue(mcplogin.approve(tempfile.mkdtemp(), None, config=cfg))   # a folder not trusted yet


if __name__ == "__main__":
    unittest.main()
