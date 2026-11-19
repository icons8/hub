"""The core does not fail on deferred actions (review 05.10): the time in tick was taken only on passes that re-read
the catalog (every 30 s), so a deferred restart of a crashed hub on another pass brought hubd down with an
UnboundLocalError, in a loop."""
import tests  # noqa: F401
import json
import tempfile
import types
import unittest

from core.daemon import Hub


class DelayedActions(unittest.TestCase):
    def test_a_delayed_action_between_catalog_reloads_does_not_crash(self):
        h = Hub(tempfile.mkdtemp(prefix="tick-"), types.SimpleNamespace())
        ran = []
        h.execute = lambda a: ran.append(a)
        h.state["sessions"]["hub"] = {"state": "starting"}
        h.delayed = [(0, {"do": "spawn", "session": "hub"})]
        h.next_catalog = 10 ** 12                      # the catalog is not re-read on this pass
        h.run_delayed()
        self.assertEqual(len(ran), 1)
        self.assertEqual(h.delayed, [])


class RunWithATakenName(unittest.TestCase):
    def test_a_rejected_launch_fails_the_run_instead_of_giving_tasks_to_the_other_session(self):
        # review 05.10: the name was taken while the request waited — session.requested was rejected, yet the task was
        # given to another session every 0.3 s
        from unittest import mock
        h = Hub(tempfile.mkdtemp(prefix="taken-"), types.SimpleNamespace())
        h.execute = lambda a: None
        own = {"channel": "cli", "owner": True}
        h.record("session.requested", "hub", {"cwd": "/h", "session_id": "s-h", "kind": "hub", **own})
        h.record("session.requested", "maps", {"cwd": "/m", "session_id": "s-m", "role": "agent", **own})
        h.record("session.requested", "maps-x", {"cwd": "/m", "session_id": "s-x", **own})
        h.record("session.started", "maps-x", {})
        h.state["runs"] = {"maps-x": {"by": "maps", "task": "do it", "state": "ready", "ts": 1}}
        with mock.patch("core.daemon.worktree.prepare", return_value=(None, None, None)):
            h.run_tasks()
            h.run_tasks()
        self.assertNotIn("maps-x", h.state.get("runs") or {})
        tasks = [d for d in h.state["delegations"].values() if d["session"] == "maps-x"]
        self.assertEqual(tasks, [])



class ConfigChangeReloadsCatalog(unittest.TestCase):
    def test_an_import_into_hub_json_is_in_the_catalog_on_the_next_pass_not_after_the_interval(self):
        # review 06.10: import mcp said "added", yet hub tools list and task run --tools did not see it for another 30 s
        import json
        import os
        from unittest import mock
        d = tempfile.mkdtemp(prefix="cfg-")
        cfg = os.path.join(d, "hub.json")
        with mock.patch.dict(os.environ, {"HUB_CONFIG": cfg}):
            h = Hub(d, types.SimpleNamespace())
            h.reload_catalog_if_due()
            self.assertNotIn("tracker", h.state["catalog"])
            h.next_catalog = 10 ** 12                  # the scheduled check is far off
            with open(cfg, "w") as f:
                json.dump({"mcp": {"tracker": {"server": {"type": "http", "url": "https://example.com/mcp"},
                                               "description": "Tasks", "risk": ["owner-data"]}}}, f)
            h.reload_catalog_if_due()
            self.assertEqual(h.state["catalog"]["tracker"]["risk"], ["owner-data"])


class HubModeIsFixed(unittest.TestCase):
    def test_the_hub_session_is_always_bypass_permissions_whatever_hub_json_says(self):
        # decision: bypass permissions mode is the default with no way to choose another (an install through
        # claude code sometimes tries to set another mode for safety), and no setting is needed at all
        import json
        import os
        from unittest import mock
        d = tempfile.mkdtemp(prefix="mode-")
        cfg = os.path.join(d, "hub.json")
        with open(cfg, "w") as f:
            json.dump({"hub": {"enabled": True, "mode": "default"}}, f)
        with mock.patch.dict(os.environ, {"HUB_CONFIG": cfg}):
            h = Hub(d, types.SimpleNamespace())
            h.execute = lambda a: None
            h.reload_catalog_if_due()
            h.ensure_hub()
        req = h.state["sessions"][h.config["hub"]["name"]]
        self.assertIn(req.get("mode"), (None, "bypassPermissions"))


class ImportWithoutCore(unittest.TestCase):
    def test_import_answers_quickly_and_without_an_error_when_the_core_is_not_running(self):
        # PR #29: without hubd the import waited 35 s for the catalog and exited with an error, though the tool was already written to the settings
        import os
        import subprocess
        import sys
        import time
        d = tempfile.mkdtemp(prefix="imp-")
        os.makedirs(os.path.join(d, "home", ".claude", "skills", "notes"))
        with open(os.path.join(d, "home", ".claude", "skills", "notes", "SKILL.md"), "w") as f:
            f.write("---\nname: notes\ndescription: notes\n---\n")
        env = {**os.environ, "HOME": os.path.join(d, "home"), "HUB_STATE_DIR": os.path.join(d, "state"),
               "HUB_CONFIG": os.path.join(d, "hub.json"), "HUB_CLI_WAIT": "1"}
        env.pop("HUB_SESSION", None)
        env.pop("CLAUDE_CONFIG_DIR", None)
        hub = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")
        t = time.time()
        r = subprocess.run([sys.executable, hub, "tools", "import", "skill", "notes"], env=env, capture_output=True,
                           text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess(time.time() - t, 10)
        self.assertIn("the core picks it up", r.stdout)


class StopMidTick(unittest.TestCase):
    def test_stop_during_a_long_step_skips_the_rest_of_the_tick(self):
        # PR #18 review: on Windows the core writes the "stop" snapshot after a tick, while hub-up waits for it only so long; a tick with long
        # steps (git — up to 30 s per working copy) ate that time — there was no snapshot, sessions stayed orphans
        from unittest import mock
        from core import daemon
        host = types.SimpleNamespace(poll=lambda: [], info=lambda name: None, screen_dialogs=False)
        h = Hub(tempfile.mkdtemp(prefix="stop-tick-"), host)
        h.start = lambda: None
        removed = []

        def remove(info):  # a stop was requested while git was running
            removed.append(info["path"])
            h.stopping.append(True)
            return True, "the working copy is removed"
        two = [{"name": n, "worktree": {"path": f"/wt/{n}"}} for n in ("a", "b")]

        class Now:
            """Cleaning up copies runs in a background thread (clean_worktrees): here it runs at once on start(), otherwise the
            test is a race between the thread and the tick (17.10: under load the tick reached load_catalog before the stop)."""
            def __init__(self, target, args=(), **_):
                self.run = lambda: target(*args)

            def start(self):
                self.run()

            def is_alive(self):
                return False
        with (mock.patch.object(daemon.executors, "worktrees_to_clean", return_value=two),
              mock.patch.object(daemon.worktree, "remove", side_effect=remove),
              mock.patch.object(daemon.threading, "Thread", Now),
              mock.patch.object(h, "load_catalog") as catalog):
            daemon.serve(h)                           # returned — next, main writes the snapshot
        self.assertEqual(removed, ["/wt/a"])          # the second copy — for the next launch
        catalog.assert_not_called()                   # and the tick did not go any further

    def test_stop_during_start_skips_the_rest_of_it(self):
        # PR #18 review: the core did not see a stop before the first tick — start() in full, and reap is a PowerShell per session
        from unittest import mock
        host = types.SimpleNamespace(poll=lambda: [], info=lambda name: None, screen_dialogs=False)
        h = Hub(tempfile.mkdtemp(prefix="stop-start-"), host)
        h.load_catalog = lambda: h.stopping.append(True)   # a stop was requested while the catalog was being built
        with mock.patch.object(h, "reap") as reap, mock.patch.object(h, "tick") as tick:
            from core import daemon
            daemon.serve(h)
        reap.assert_not_called()
        tick.assert_not_called()

    def test_sigterm_on_posix_only_raises_the_flag(self):
        # the handler itself wrote the snapshot in the middle of a tick: a signal between the fold and the write gave a snapshot without the event
        # (replay skipped it), inside the write — a repeated seq
        import signal
        from unittest import mock
        from platforms import posix
        handlers, stopped, soon = {}, [], []
        with mock.patch.object(posix.signal, "signal", side_effect=lambda sig, h: handlers.__setitem__(sig, h)):
            posix.on_core_stop(lambda *_: stopped.append(True), lambda: soon.append(True))
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        self.assertEqual((stopped, soon), ([], [True]))

    def test_the_core_gets_longer_to_stop_than_a_part(self):
        from platforms import posix
        self.assertEqual((posix.stop_grace(True), posix.stop_grace(False)), (60, 15))

    def test_stop_between_retiring_executors(self):
        from unittest import mock
        from core import daemon
        h = Hub(tempfile.mkdtemp(prefix="stop-retire-"), types.SimpleNamespace())
        recorded = []
        h.record = lambda type, name=None, data=None, **_: (recorded.append(name), h.stopping.append(True))
        with mock.patch.object(daemon.executors, "retiring", return_value=[("sleep", "f1"), ("sleep", "f2")]):
            h.retire_executors()
        self.assertEqual(recorded, ["f1"])


class RejectionWithItsFact(unittest.TestCase):
    def test_a_rejected_fact_never_shows_in_the_journal_without_its_rejection(self):
        # 14.10: hubd wrote the fact, then folded it and only then the refusal; under load the
        # CLI (journal.decided: "after the fact, one more pass in 0.1 s") managed to read the fact without the refusal and printed
        # "accepted" for a rejected command. The fact and the refusal are now one write
        from unittest import mock
        from core import journal
        h = Hub(tempfile.mkdtemp(prefix="rej-"), types.SimpleNamespace())
        h.execute = lambda a: None
        path, seen, real = h.journal.path, [], journal._write

        def spy(p, data):   # what a reader sees after each write
            real(p, data)
            with open(path, encoding="utf-8") as g:
                seen.append(g.read())
        with mock.patch.object(journal, "_write", spy):
            h.record("session.waiting", "nobody", {"text": "x", "channel": "cli", "owner": True})
        self.assertTrue(seen)
        for text in seen:
            if '"session.waiting"' in text:
                self.assertIn('"core.rejected"', text)


class LateRejectionIsDelivered(unittest.TestCase):
    def test_the_note_from_a_rejection_the_cli_gave_up_on_reaches_an_idle_session(self):
        # review 14.10: the actions of the refusal fold were dropped — the note "The core rejected your command" was put "to print"
        # at a free session, and nobody told the host to print it
        h = Hub(tempfile.mkdtemp(prefix="late-"), types.SimpleNamespace())
        ran = []
        h.execute = lambda a: ran.append(a)
        own = {"channel": "cli", "owner": True}
        h.record("session.requested", "devops", {"cwd": "/w", "session_id": "s-d", **own})
        h.record("session.started", "devops", {})
        ran.clear()
        mine = {"channel": "session", "owner": False, "sender": "devops"}
        h.record("cli.gave_up", None, {"req": "r1", "what": "schedule.remove_requested job-x", **mine})
        h.record("schedule.remove_requested", "job-x", {"req": "r1", **mine})
        notes = [m for m in h.state["messages"].values() if m["session"] == "devops" and "rejected your command" in m["text"]]
        self.assertEqual([m["state"] for m in notes], ["typing"])
        self.assertTrue([a for a in ran if a.get("session") == "devops" and notes[0]["id"] in json.dumps(a)], ran)


class ClaudeCommand(unittest.TestCase):
    def test_a_path_with_spaces_is_taken_whole(self):
        # str.split() cut a Windows profile path with a space in a folder name in two
        import os
        from unittest import mock
        from core import daemon
        folder = tempfile.mkdtemp(prefix="claude cmd ")
        exe = os.path.join(folder, "my claude")
        open(exe, "w").close()
        with mock.patch.dict(os.environ, {"HUB_CLAUDE": exe}):
            self.assertEqual(daemon.claude_command(), [exe])
        with mock.patch.dict(os.environ, {"HUB_CLAUDE": f'python3 "{exe}" --x'}):   # a command line — as in the OS
            self.assertEqual(daemon.claude_command(), ["python3", exe, "--x"])


if __name__ == "__main__":
    unittest.main()


class QueueAfterTheWindowReset(unittest.TestCase):
    def test_a_budget_queue_rises_when_the_reset_passes_without_any_new_event(self):
        # the weekly window reset passed by the daemon's clock, there are no new limit events: a request that waited for budget
        # comes up on the nearest pass instead of standing until the next event
        from unittest import mock
        now = [1000.0]
        h = Hub(tempfile.mkdtemp(prefix="reset-"), types.SimpleNamespace(), clock=lambda: now[0])
        h.execute = lambda a: None
        own = {"channel": "cli", "owner": True}
        h.record("session.requested", "hub", {"cwd": "/h", "session_id": "s-h", "kind": "hub", **own})
        h.record("session.requested", "maps", {"cwd": "/m", "session_id": "s-m", "role": "agent", **own})
        h.record("session.started", "maps", {})
        h.record("account.limits", None, {"windows": {"seven_day": {"used": 0.95, "resets": 5000}}, **own})
        h.record("task.run_requested", "maps-x", {"task": "do it", "by": "maps", "channel": "session",
                                                  "owner": False, "sender": "maps"})
        self.assertEqual(h.state["runs"]["maps-x"]["state"], "queued")
        with mock.patch("core.daemon.worktree.prepare", return_value=(None, None, None)):
            now[0] = 4999
            h.run_tasks()
            self.assertEqual(h.state["runs"]["maps-x"]["state"], "queued")       # the window has not reset yet
            now[0] = 5001
            h.run_tasks()
        self.assertNotEqual((h.state.get("runs") or {}).get("maps-x", {}).get("state"), "queued")
        self.assertIn("maps-x", h.state["sessions"])
