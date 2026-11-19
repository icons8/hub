"""A stuck startup: the process is alive but SessionStart is not. After an account change, spawn -> the process wrote itself
into the registry and went silent: no SessionStart, no line in the conversation; the session is "starting" for tens of
minutes, its queue and the deferred hub restart wait, and the journal watchman is silent (it watches progress, and there
is none). Only killing the process by hand helped. Now the core does it itself: no SessionStart for longer than
start_stuck_minutes (default 0: do not watch, 10 advised) -> kill and bring up the same conversation, counted in
MAX_RESTARTS, like a crashed one."""
import tests  # noqa: F401 — hide the machine's settings
import tempfile
import unittest

from core import i18n, reducer, views
from core.daemon import Hub
from core.machines import MAX_RESTARTS

START_MINUTES = 10   # the advised timeout (set in hub.json by hand: watching is off by default)
START_TIMEOUT = START_MINUTES * 60

OWNER = {"channel": "web", "owner": True}


class Reducer(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("session.requested", "a", cwd="/w", session_id="s-a", role="agent", **OWNER)
        self.ev("session.started", "a")
        self.ev("turn.started", "a", marker=None)
        self.ev("turn.stopped", "a")
        self.ev("session.exited", "a", why="process gone")    # coming up again, and went silent

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, actions, self.why = reducer.reduce(self.st, {"seq": self.seq, "ts": 100 * self.seq, "type": type,
                                                              "entity": entity, "data": data})
        return [a for a in actions if a.get("session") == entity]

    def test_a_silent_start_is_killed_and_raised_again_with_the_conversation(self):
        acts = self.ev("session.start_stuck", "a", minutes=10)
        self.assertEqual([a["do"] for a in acts], ["kill", "spawn"])
        self.assertEqual((acts[1]["session_id"], acts[1]["resume"]), ("s-a", True))
        a = self.st["sessions"]["a"]
        self.assertEqual(a["state"], "starting")
        self.ev("host.done", "a", do="spawn", result="spawned")
        self.assertEqual(self.st["sessions"]["a"]["spawned"], 100 * self.seq)   # the timeout restarts from this startup

    def test_the_term_runs_from_the_real_spawn_not_from_the_decision(self):
        # a startup may wait (gateway, modules, backoff): the timeout counts from host.done spawn, otherwise a new
        # process would be killed at once
        self.ev("session.start_stuck", "a")
        decided = self.st["sessions"]["a"]["since"]
        self.ev("host.done", "a", do="spawn", result="spawned")
        self.assertEqual(self.st["sessions"]["a"]["spawned"], 100 * self.seq)
        self.assertGreater(self.st["sessions"]["a"]["spawned"], decided)
        self.assertEqual(self.st["sessions"]["a"]["since"], decided)   # since is entering the state, others read it
        self.ev("session.started", "a")
        self.ev("host.done", "a", do="spawn", result="spawned")   # a late result after SessionStart does not touch the timeout
        self.assertEqual(self.st["sessions"]["a"]["state"], "idle")

    def test_a_spawn_that_found_the_process_running_does_not_extend_the_term(self):
        # "already running" (and "enter only" for tmux) did not start a process: otherwise a stuck one would be
        # postponed endlessly
        self.ev("host.done", "a", do="spawn", result="spawned")
        was = self.st["sessions"]["a"]["spawned"]
        for result in ("already running", "enter only"):
            self.ev("host.done", "a", do="spawn", result=result)
        self.assertEqual(self.st["sessions"]["a"]["spawned"], was)

    def test_it_counts_as_a_failed_start(self):
        before = self.st["sessions"]["a"]["restarts"]
        self.ev("session.start_stuck", "a")
        self.assertEqual(self.st["sessions"]["a"]["restarts"], before + 1)
        for _ in range(MAX_RESTARTS):
            was = self.st["sessions"]["a"]["state"]
            acts = self.ev("session.start_stuck", "a")
            if was == "starting" and self.st["sessions"]["a"]["state"] == "failed":
                break   # this time is the last
        self.assertEqual(self.st["sessions"]["a"]["state"], "failed")
        # we will not bring it up any more, but a stuck process is still killed: otherwise it would stay alive in the
        # failed state
        self.assertEqual([a["do"] for a in acts], ["kill"])

    def test_a_start_that_came_resets_the_count(self):
        self.ev("session.start_stuck", "a")
        self.ev("session.started", "a")
        self.assertEqual(self.st["sessions"]["a"]["restarts"], 0)

    def test_only_a_starting_session(self):
        self.ev("session.started", "a")
        self.assertEqual(self.ev("session.start_stuck", "a"), [])
        self.assertEqual(self.st["sessions"]["a"]["state"], "idle")

    def test_it_does_not_ask_the_hub_by_itself(self):
        """The alert about a session that does not start is the journal watchman's job (crashloop by the startup
        count), not a second line from the fact itself."""
        self.ev("session.start_stuck", "a")
        self.assertFalse(self.st["messages"])


class Feed(unittest.TestCase):
    def test_the_owner_sees_why_the_session_is_raised_again(self):
        # like "process gone" on exit: without a line the feed would be silent while the session restarted
        raw = [("session.requested", {"cwd": "/w", "session_id": "s-a", "role": "agent", **OWNER}),
               ("session.started", {"session_id": "s-a"}), ("session.exited", {"why": "process gone"}),
               ("session.start_stuck", {"minutes": 10})]
        r = views.Reader()
        r.feed_all([{"seq": i, "ts": 1000 + i, "src": "test", "type": t, "entity": "a", "data": d}
                    for i, (t, d) in enumerate(raw, 1)])
        text = i18n.t("core.views.sys.start_stuck")
        self.assertNotEqual(text, "core.views.sys.start_stuck")   # the key is in the catalog
        self.assertEqual([it["text"] for it in r.timeline("a")["items"] if it["kind"] == "sys"][-1], text)


class Daemon(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.h = Hub(tempfile.mkdtemp(prefix="stuck-"), type("H", (), {"alive": lambda s, n: True})(),
                     registry_dir=tempfile.mkdtemp(), clock=lambda: self.now[0])
        self.done, self.slow = [], False

        def execute(action):
            self.done.append(action)
            if action["do"] == "spawn" and not self.slow:   # the host brought it up at once; slow: the startup still waits in the queue
                self.h.record("host.done", action["session"], {"do": "spawn", "result": "spawned"})
        self.h.execute = execute
        self.h.config["start_stuck_minutes"] = START_MINUTES
        self.h.record("session.requested", "a", {"cwd": "/w", "session_id": "s-a", "role": "agent", **OWNER})
        self.done.clear()

    def stuck(self):
        return [a["do"] for a in self.done if a.get("session") == "a"]

    def test_no_session_start_for_too_long_raises_it_again_once(self):
        self.now[0] += START_TIMEOUT - 1
        self.h.check_starting()
        self.assertEqual(self.stuck(), [])
        self.now[0] += 1
        self.h.check_starting()
        self.h.check_starting()
        self.assertEqual(self.stuck(), ["kill", "spawn"])

    def test_a_started_session_is_left_alone(self):
        self.h.record("session.started", "a", {})
        self.now[0] += START_TIMEOUT * 3
        self.h.check_starting()
        self.assertEqual(self.stuck(), [])

    def test_the_term_is_counted_from_each_new_start(self):
        self.now[0] += START_TIMEOUT
        self.h.check_starting()
        self.done.clear()
        self.now[0] += START_TIMEOUT - 1   # a new startup, a new timeout, not at once again
        self.h.check_starting()
        self.assertEqual(self.stuck(), [])
        self.now[0] += 1
        self.h.check_starting()
        self.assertEqual(self.stuck(), ["kill", "spawn"])

    def test_a_late_spawn_is_not_killed_right_after_it_came_up(self):
        self.h.config["start_stuck_minutes"] = 1
        self.slow = True
        self.now[0] += 60
        self.h.check_starting()
        self.assertEqual(self.stuck(), ["kill", "spawn"])
        self.done.clear()
        self.now[0] += 100   # the startup waited in the queue longer than the timeout and only now went to the host
        self.h.record("host.done", "a", {"do": "spawn", "result": "spawned"})
        self.h.check_starting()
        self.assertEqual(self.stuck(), [])
        self.now[0] += 60
        self.h.check_starting()
        self.assertEqual(self.stuck(), ["kill", "spawn"])   # and if this one is silent too, the timeout is real

    def test_a_stop_of_the_core_is_seen_between_sessions(self):
        # kill takes up to 20 s per call: stopping the core between ticks is visible between sessions too (as in
        # reconcile, clean_worktrees)
        self.h.record("session.requested", "b", {"cwd": "/w", "session_id": "s-b", "role": "agent", **OWNER})
        self.done.clear()
        run = self.h.execute

        def stop_after_kill(action):
            run(action)
            if action["do"] == "kill":
                self.h.stopping.append(True)
        self.h.execute = stop_after_kill
        self.now[0] += START_TIMEOUT
        self.h.check_starting()
        self.assertEqual([(a["do"], a["session"]) for a in self.done if a["do"] == "kill"], [("kill", "a")])

    def test_the_term_is_in_hub_json(self):
        self.h.config["start_stuck_minutes"] = 3
        self.now[0] += 179
        self.h.check_starting()
        self.assertEqual(self.stuck(), [])
        self.now[0] += 1
        self.h.check_starting()
        self.assertEqual(self.stuck(), ["kill", "spawn"])

    def test_it_is_off_by_default(self):
        del self.h.config["start_stuck_minutes"]
        self.now[0] += START_TIMEOUT * 10
        self.h.check_starting()
        self.assertEqual(self.stuck(), [])

    def test_a_value_that_is_not_a_number_turns_it_off_without_breaking_the_tick(self):
        for bad in ("10", True, None, [10]):
            self.h.config["start_stuck_minutes"] = bad
            self.now[0] += START_TIMEOUT * 10
            self.h.check_starting()
        self.assertEqual(self.stuck(), [])

    def test_zero_turns_it_off(self):
        self.h.config["start_stuck_minutes"] = 0
        self.now[0] += START_TIMEOUT * 10
        self.h.check_starting()
        self.assertEqual(self.stuck(), [])

    def test_a_raise_that_waits_in_the_queue_is_not_stuck(self):
        self.h.delayed.append((self.now[0] + 5, {"do": "spawn", "session": "a"}))
        self.now[0] += START_TIMEOUT * 2
        self.h.check_starting()
        self.assertEqual(self.stuck(), [])

    def test_a_dead_process_is_left_to_reconcile(self):
        self.h.host = type("H", (), {"alive": lambda s, n: False})()
        self.now[0] += START_TIMEOUT * 2
        self.h.check_starting()
        self.assertEqual(self.stuck(), [])


if __name__ == "__main__":
    unittest.main()
