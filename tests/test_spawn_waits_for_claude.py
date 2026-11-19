"""A new hub with no Claude Code yet (the app before «Setup» installed it): the core does not spawn sessions and does not
count it as crashes — no host.error, no session.exited, no restarts (the watchdog wrote «crashloop» about the hub's own
session). The spawn waits; once the program is there (`hub setup claude`, the page's «Setup») the session spawns by
itself, without a restart. A hub with Claude Code spawns as before."""
import tests  # noqa: F401
import os
import tempfile
import unittest

import platforms
from core import journal, restart
from core.daemon import Hub
from hosts.streamjson import StreamHost
from tests.fakes import exe

VERSION = "print('2.1.300 (Claude Code)')\n"


class SpawnWaitsForClaude(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="noclaude-")
        self.addCleanup(platforms.current.rmtree, self.root)
        self.state = os.path.join(self.root, "state")
        os.makedirs(self.state)
        # the core's own command: `which claude` or ~/.local/bin/claude — a path, here to a program not installed yet
        self.claude = os.path.join(self.root, "bin", "claude")
        self.now = [1_000_000.0]
        self.host = StreamHost(self.state, claude=(self.claude,))
        self.hub = Hub(self.state, self.host, registry_dir=os.path.join(self.root, "reg"), clock=lambda: self.now[0])

    def events(self, kind):
        return [e for e in journal.read(self.hub.journal.path) if e["type"] == kind]

    def passes(self, n, step=5, observe=True):
        """What hubd does between ticks for a starting session: observe the process, run the delayed spawns."""
        for _ in range(n):
            self.now[0] += step
            if observe:
                self.hub.observe()
            self.hub.run_delayed()

    def request(self, name="hub-core", **extra):
        self.hub.record("session.requested", name, {"cwd": self.root, "session_id": f"s-{name}", "channel": "cli",
                                                    "owner": True, **({"kind": "hub"} if name == "hub-core" else {}),
                                                    **extra})

    def install(self, path=None):
        path = path or self.claude
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.name == "nt":   # what the OS starts by a path without an extension is path.exe, not exe.script's .cmd
            open(path + ".exe", "w").close()   # these tests stand in for the spawn: it is never run
            return path + ".exe"
        exe.script(path, VERSION)   # «Setup» put it there; no hub restart
        return path

    def uninstall(self):
        os.unlink(self.claude + (".exe" if os.name == "nt" else ""))

    def stand_in(self):
        """The host's spawn recorded: no process (the unit tests do not observe after it)."""
        spawned = []
        real = self.host.spawn
        self.host.spawn = lambda name, **kw: (spawned.append((name, kw)), "spawned")[1]
        self.addCleanup(setattr, self.host, "spawn", real)
        return spawned

    def test_no_claude_no_spawn_attempts_and_no_crashes(self):
        self.request()
        self.passes(40)
        sess = self.hub.state["sessions"]["hub-core"]
        self.assertEqual([e["data"] for e in self.events("host.error") if e["data"].get("do") == "spawn"], [])
        self.assertEqual(self.events("session.exited"), [])
        self.assertEqual(sess["restarts"], 0)
        self.assertEqual(sess["state"], "starting")
        self.assertIn("hub-core", self.hub.waiting())   # deferred: observe does not count it as a process that is gone
        # the wait is the core's state, said once; a new hub gets no notice (the page's band says it)
        self.assertEqual([e["data"] for e in self.events("core.claude_missing")], [{"path": self.claude}])
        self.assertEqual(self.hub.state["claude_missing"]["path"], self.claude)
        self.assertEqual(self.events("owner.notify"), [])

    def test_a_session_spawns_by_itself_once_claude_is_installed(self):
        spawned = self.stand_in()
        self.request()
        self.request("maps", role="agent")
        self.passes(10)
        self.assertEqual(spawned, [])
        self.install()
        self.now[0] += 3
        self.hub.run_delayed()   # (not observe: the stand-in spawn starts no process to see)
        self.assertEqual(sorted(n for n, _ in spawned), ["hub-core", "maps"])
        self.assertEqual(self.hub.waiting(), set())
        self.assertEqual(self.hub.state["sessions"]["hub-core"]["restarts"], 0)
        self.assertNotIn("claude_missing", self.hub.state)
        self.assertEqual([e["data"] for e in self.events("core.claude_missing")], [{"path": self.claude}, {"path": None}])

    def test_with_claude_the_spawn_goes_at_once(self):
        self.install()
        spawned = self.stand_in()
        self.request()
        self.assertEqual([n for n, _ in spawned], ["hub-core"])
        self.assertEqual(self.hub.waiting(), set())
        self.assertEqual(self.events("core.claude_missing"), [])

    def test_a_command_by_name_is_left_to_the_os(self):
        # HUB_CLAUDE="claude --x" or a host made with the bare name: the OS looks it up at spawn, as before
        self.host.claude = ["claude-not-on-this-machine"]
        spawned = self.stand_in()
        self.request()
        self.assertEqual([n for n, _ in spawned], ["hub-core"])

    def test_restart_of_the_whole_hub_does_not_wait_for_sessions_that_wait_for_claude(self):
        # the crash loop gave the hub session restarts > 0 (restart skipped it); a waiting one has none
        self.request()
        self.request("maps", role="agent")
        self.passes(3)
        self.assertEqual(restart.blockers(self.hub.state), [])
        self.install()
        self.stand_in()
        self.passes(2, observe=False)
        self.assertNotIn("claude_missing", self.hub.state)
        # spawned, no SessionStart yet: a real «starting» holds it, as before
        self.assertEqual(restart.blockers(self.hub.state), ["hub-core: coming up", "maps: coming up"])

    def test_restart_still_waits_for_a_session_spawned_before_the_program_went(self):
        self.install()
        self.stand_in()
        self.request("maps", role="agent")                         # spawned: its process is starting
        self.uninstall()
        self.request()                                          # this one waits for Claude Code
        self.assertTrue(self.hub.state.get("claude_missing"))
        self.assertEqual(restart.blockers(self.hub.state), ["maps: coming up"])

    def worked_and_lost(self):
        self.install()
        spawned = self.stand_in()
        self.request("maps", role="agent")
        self.hub.record("session.started", "maps", {"source": "startup"})
        self.uninstall()                                          # a broken update took the program away
        self.hub.record("session.exited", "maps", {"why": "process gone"})
        return spawned

    def ticks(self, seconds, step=5):
        for _ in range(int(seconds // step)):
            self.now[0] += step
            self.hub.run_delayed()
            self.hub.claude_tick()

    def test_a_hub_that_worked_and_lost_claude_tells_the_owner_until_it_is_back(self):
        spawned = self.worked_and_lost()
        self.ticks(30)
        self.assertEqual(self.events("owner.notify"), [])          # not at once: an update swaps the program
        self.ticks(60)
        notes = [e["data"] for e in self.events("owner.notify")]
        self.assertEqual(len(notes), 1)
        self.assertIn(self.claude, notes[0]["text"])
        self.assertIn("n:core:claude-missing", self.hub.state["notices"])
        self.ticks(60)
        self.assertEqual(len(self.events("owner.notify")), 1)      # once
        self.install()
        self.ticks(5)
        self.assertEqual([n for n, _ in spawned], ["maps", "maps"])
        self.assertNotIn("n:core:claude-missing", self.hub.state.get("notices") or {})
        self.assertNotIn("claude_missing", self.hub.state)

    def test_a_short_gap_of_the_program_does_not_page_the_owner(self):
        spawned = self.worked_and_lost()
        self.ticks(10)
        self.install()                                             # Claude Code's own update put the new one in
        self.ticks(120)
        self.assertEqual(self.events("owner.notify"), [])
        self.assertEqual([n for n, _ in spawned], ["maps", "maps"])

    def test_a_spawn_that_then_waits_for_something_else_is_not_lost(self):
        # Claude Code came, but the previous process (or modules, the login module, the gateway) still holds the spawn
        spawned = self.stand_in()
        self.request()
        self.passes(2, observe=False)
        self.install()
        real, held = self.hub.wait_release, [True]

        def once(action):
            if held[0]:
                held[0] = False
                self.hub.delayed.append((self.now[0] + 1, action))
                return True
            return real(action)
        self.hub.wait_release = once
        self.passes(3, observe=False)
        self.assertEqual([n for n, _ in spawned], ["hub-core"])

    def test_what_the_host_learned_from_the_missing_program_is_asked_anew(self):
        self.request()
        self.passes(1, observe=False)
        self.host.fresh_prompt = False      # `claude --help` before the install: no such program, no new flag
        self.install()
        self.stand_in()
        self.passes(1, observe=False)
        self.assertIsNone(self.host._fresh)

    def test_a_newer_spawn_of_the_session_replaces_the_waiting_one(self):
        # a reload, an account switch or the gateway while it waited: the spawn with the newest arguments goes, once
        spawned = self.stand_in()
        self.request()
        self.passes(2, observe=False)
        self.hub.state.setdefault("account", {})["active"] = "newest"   # the pool the newer spawn was decided in
        self.hub.execute({"do": "spawn", "session": "hub-core", "session_id": "s-hub-core", "cwd": self.root,
                          "account": "newest"})
        self.passes(2, observe=False)
        self.install()
        self.passes(2, observe=False)
        self.assertEqual([(n, kw.get("account")) for n, kw in spawned], [("hub-core", "newest")])

    def test_a_newer_spawn_after_the_install_drops_the_one_still_waiting(self):
        # the program came, the waiting spawn has not left the delayed queue yet, and a reload spawns the session at
        # once: the waiting one must not go after it as a second process of the same session
        spawned = self.stand_in()
        self.request()
        self.passes(2, observe=False)
        self.install()
        self.hub.state.setdefault("account", {})["active"] = "newest"   # the pool the newer spawn was decided in
        self.hub.execute({"do": "spawn", "session": "hub-core", "session_id": "s-hub-core", "cwd": self.root,
                          "account": "newest"})
        self.passes(3, observe=False)
        self.assertEqual([(n, kw.get("account")) for n, kw in spawned], [("hub-core", "newest")])

    def test_installed_elsewhere_than_the_path_hubd_started_with(self):
        # hubd started with ~/.local/bin/claude (nothing on PATH); the person installed it with npm or brew
        other = os.path.join(self.root, "brew", "claude")
        self.host.claude_from = lambda: [other]   # make_host gives the host claude_command
        spawned = self.stand_in()
        self.request()
        self.passes(2, observe=False)
        self.assertEqual(spawned, [])
        self.install(other)
        self.passes(2, observe=False)
        self.assertEqual([n for n, _ in spawned], ["hub-core"])
        self.assertEqual(self.host.claude, [other])

    def test_the_releases_own_copy_takes_over_the_next_spawns_without_a_restart(self):
        # spec d2263524: the hub runs claude from PATH until the update's pass downloads its release's copy (core/ownclaude);
        # the next spawn goes on it — no restart; what the host learned from the previous program is asked anew
        from unittest import mock
        from core import ownclaude
        on_path = self.install()
        self.host.claude_from = lambda: [on_path]
        self.hub.claude_release = "2.1.400"
        self.host.fresh_prompt = True
        spawned = self.stand_in()
        self.request()
        self.passes(2, observe=False)
        self.assertEqual(len(spawned), 1)
        self.assertEqual(list(self.host.claude), [self.claude])   # no copy yet: what hubd started with
        mine = self.install(ownclaude.path(self.state, "2.1.400").removesuffix(".exe"))
        with mock.patch.dict(os.environ, {k: "0" for k in ownclaude.ENV}):
            self.request("maps", role="agent")
            self.passes(2, observe=False)
            self.assertEqual({k: os.environ[k] for k in ownclaude.ENV}, ownclaude.ENV)   # for what hubd starts now
        self.assertEqual([n for n, _ in spawned], ["hub-core", "maps"])
        self.assertEqual(self.host.claude, [mine])
        self.assertIsNone(self.host._fresh)
        self.assertTrue(os.path.isfile(os.path.join(self.state, "claude-code", "2.1.400", ownclaude.ACTIVE)))
        with mock.patch.dict(os.environ, {"HUB_CLAUDE": on_path}):   # the owner's own claude stays as it is
            self.host.claude = [on_path]
            self.hub.claude_own()
            self.assertEqual(self.host.claude, [on_path])

    def test_the_mark_goes_when_claude_comes_with_no_spawn_left_waiting(self):
        self.request()
        self.passes(2, observe=False)
        self.hub.record("session.remove_requested", "hub-core", {"channel": "cli", "owner": True})
        self.install()
        self.passes(1, observe=False)
        self.hub.claude_tick()
        self.assertNotIn("claude_missing", self.hub.state)

    def test_present_is_the_file_the_os_starts(self):
        from core.daemon import claude_present
        os.makedirs(os.path.dirname(self.claude))
        with open(self.claude + ".cmd", "w") as f:   # Windows adds only .exe to a path without an extension
            f.write("@echo off\n")
        self.assertFalse(claude_present(self.claude))
        self.install()
        self.assertTrue(claude_present(self.claude))
        if os.name != "nt":
            plain = os.path.join(self.root, "bin", "plain")
            open(plain, "w").close()                 # not runnable: the spawn would fail as a missing one does
            self.assertFalse(claude_present(plain))

    def test_only_the_core_writes_the_mark(self):
        from core import spool
        spool.put(os.path.join(self.state, "spool"), "core.claude_missing", None, {"path": "/x/claude"})
        self.hub.collect()
        self.assertNotIn("claude_missing", self.hub.state)


FAKE = os.path.join(os.path.dirname(__file__), "fakes", "fake_claude_stream.py")


@unittest.skipIf(os.name == "nt", "a script by path without .exe: the stand-in for claude.exe is POSIX here")
class TheHubSessionComesUpAfterTheInstall(unittest.TestCase):
    """Whole ticks of hubd, a stand-in Claude Code that answers the protocol: put at the path while hubd runs."""

    def test_ticks_without_claude_then_the_install_brings_the_hub_session_up(self):
        import time
        from core import spool
        root = tempfile.mkdtemp(prefix="noclaude-live-")
        self.addCleanup(platforms.current.rmtree, root)
        state = os.path.join(root, "state")
        os.makedirs(state)
        claude = os.path.join(root, "bin", "claude")
        host = StreamHost(state, claude=(claude,), env={"FAKE_TURN_S": "0.2"})
        self.addCleanup(host.kill_server)
        hub = Hub(state, host, registry_dir=os.path.join(root, "reg"))
        hub.start()
        spool.put(os.path.join(state, "spool"), "session.requested", "hub-core",
                  {"cwd": root, "session_id": "s-hub", "kind": "hub", "channel": "cli", "owner": True})
        end = time.time() + 1
        while time.time() < end:
            hub.tick()
            time.sleep(0.03)
        kinds = [e["type"] for e in journal.read(hub.journal.path)]
        self.assertIn("core.claude_missing", kinds)   # it got as far as the spawn
        self.assertNotIn("host.error", kinds)
        self.assertNotIn("session.exited", kinds)
        self.assertEqual(hub.state["sessions"]["hub-core"]["restarts"], 0)
        os.makedirs(os.path.dirname(claude))
        exe.script(claude, f"import runpy, sys\nsys.argv[0] = {FAKE!r}\nrunpy.run_path({FAKE!r}, run_name='__main__')\n")
        end = time.time() + 15
        while time.time() < end and hub.state["sessions"]["hub-core"]["state"] != "idle":
            hub.tick()
            time.sleep(0.03)
        self.assertEqual(hub.state["sessions"]["hub-core"]["state"], "idle")
        self.assertEqual(hub.state["sessions"]["hub-core"]["restarts"], 0)
        self.assertEqual([e["data"]["source"] for e in journal.read(hub.journal.path) if e["type"] == "session.started"],
                         ["startup"])


if __name__ == "__main__":
    unittest.main()
