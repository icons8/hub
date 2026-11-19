"""After a full hub restart the whole fleet starts its MCP servers at once; a server that hangs keeps Claude Code
from reading the first stdin line until MCP_TIMEOUT (core/catalog.py, 180 s): `system init` and the turn wait for it
(probe on Claude Code 2.1.302: a server that never answers, MCP_TIMEOUT=20000 -> the first turn at 20.5 s). The core
used to retry the typed line every CONFIRM_SECONDS and give it up after MAX_ATTEMPTS (120 s), and the watcher reported
"not delivered" at UNDELIVERED (150 s) - on a session that is fine and starts the turn at 180 s by itself.
A replay of the hub journal: the resumed session typed at +7 s, session.mcp with two servers
pending at +7.4 s, "already sent" at +37/+67/+97, failed at +127, watch.alert at +150, the turn at ~+187."""
import tests  # noqa: F401 - keeps the machine's settings out
import os
import tempfile
import unittest

from core import reducer, spool
from core.parking import MCP_WAIT
from core.machines import COMMAND_CONFIRM_SECONDS, CONFIRM_SECONDS, GAVE_UP, MAX_ATTEMPTS
from modules.watch import watch
from tests.test_watch import OWNER, journal


class Delivery(unittest.TestCase):
    T0 = 1000

    def setUp(self):
        self.start()

    def start(self, text="continue", host=None):
        """A resumed process after a full restart; the line is typed at once (the queue head of a free session)."""
        self.st, self.seq, self.acts = reducer.empty(), 0, []
        if host:
            self.ev(self.T0, "core.started", None, host=host)
        self.ev(self.T0, "session.requested", "b", cwd="/w", session_id="s-b", **OWNER)
        self.ev(self.T0 + 7, "session.started", "b", source="resume")
        self.ev(self.T0 + 7, "message.received", None, to="b", text=text, **OWNER)
        self.mid = f"m{self.seq}"
        self.assertEqual(self.state(), "typing")

    def ev(self, ts, type, entity=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts, "type": type, "entity": entity,
                                                      "data": data})
        self.acts += acts
        return why

    def clock(self, until):
        """The daemon's timers (hubd.fire_timers): every second, timer.fired for each deadline that has passed."""
        for now in range(self.T0 + 8, until + 1):
            for deadline, mid, attempt in reducer.pending_timers(self.st, now):
                if deadline <= now and mid == self.mid:
                    self.ev(now, "timer.fired", mid, attempt=attempt, n=attempt)

    def state(self):
        return self.st["messages"][self.mid]["state"]

    def test_a_line_waiting_for_mcp_init_is_not_given_up(self):
        t0 = self.T0
        self.ev(t0 + 7.4, "session.mcp", "b", servers={"srv-a": "pending", "srv-b": "pending", "srv-c": "pending"})
        self.clock(t0 + 7 + MAX_ATTEMPTS * CONFIRM_SECONDS)
        # 127 s after typing: the CLI still waits for its MCP servers (up to MCP_WAIT), the line is in its stdin
        self.assertLess(MAX_ATTEMPTS * CONFIRM_SECONDS, MCP_WAIT)
        self.assertEqual(self.state(), "typing",
                         "the core gave the line up while Claude Code was still connecting MCP servers")
        self.assertEqual([a for a in self.acts if a.get("do") == "type"][1:], [], "typed again into a busy stdin")
        self.clock(int(t0 + 7 + MCP_WAIT))
        self.ev(t0 + 7 + MCP_WAIT + 0.5, "session.info", "b", mcp=["srv-a", "srv-b", "srv-c"], mcp_failed=["srv-a"])
        self.ev(t0 + 7 + MCP_WAIT + 1, "turn.started", "b", marker=self.mid)
        self.assertEqual(self.state(), "delivered")

    def test_no_init_within_mcp_timeout_is_given_up_as_before(self):
        self.clock(int(self.T0 + 7 + MCP_WAIT))
        self.assertEqual(self.state(), "typing")
        self.clock(int(self.T0 + 7 + MCP_WAIT + (MAX_ATTEMPTS + 1) * CONFIRM_SECONDS))
        self.assertEqual(self.state(), "failed")
        self.assertEqual(self.st["messages"][self.mid]["reason"], GAVE_UP)

    def test_a_quick_init_leaves_the_usual_attempts(self):
        self.ev(self.T0 + 9, "session.info", "b", mcp=["srv-c"])
        self.clock(self.T0 + 7 + MAX_ATTEMPTS * CONFIRM_SECONDS)
        self.assertEqual(self.state(), "failed", "after system init the line is retried and given up as before")
        self.assertEqual(len([a for a in self.acts if a.get("do") == "type"]), MAX_ATTEMPTS)

    def test_a_line_typed_into_the_previous_process_is_retried_as_before(self):
        """A new process started after the line was typed: that line is not in its stdin, nothing to wait for."""
        self.ev(self.T0 + 9, "session.info", "b", mcp=["srv-c"])
        self.ev(self.T0 + 20, "session.started", "b", source="resume")
        self.clock(self.T0 + 7 + MAX_ATTEMPTS * CONFIRM_SECONDS)
        self.assertEqual(self.state(), "failed", "a line typed into the previous process waited for the new one's MCP")

    def test_a_turn_ends_the_wait_without_session_info(self):
        self.ev(self.T0 + 9, "turn.started", "b", marker=None)
        self.ev(self.T0 + 10, "turn.result", "b")
        self.clock(self.T0 + 7 + MAX_ATTEMPTS * CONFIRM_SECONDS)
        self.assertEqual(self.state(), "failed")

    def test_an_exited_process_ends_the_wait(self):
        self.ev(self.T0 + 9, "session.exited", "b", code=1)
        self.assertNotIn("mcp_init", self.st["sessions"]["b"])

    def test_clear_in_the_same_process_ends_the_wait(self):
        self.ev(self.T0 + 9, "session.started", "b", source="clear", session_id="s-b2")
        self.assertNotIn("mcp_init", self.st["sessions"]["b"])

    def test_a_tmux_host_keeps_the_usual_attempts(self):
        """tmux sends no session.info: its sessions do not wait for it."""
        self.start(host="tmux")
        self.assertNotIn("mcp_init", self.st["sessions"]["b"])
        self.clock(self.T0 + 7 + MAX_ATTEMPTS * CONFIRM_SECONDS)
        self.assertEqual(self.state(), "failed")

    def test_a_command_keeps_its_own_term(self):
        """A Claude Code command waits COMMAND_CONFIRM_SECONDS, longer than the MCP wait: retried as before."""
        self.start(text="/compact")
        self.assertGreater(COMMAND_CONFIRM_SECONDS, MCP_WAIT)
        self.clock(self.T0 + 7 + COMMAND_CONFIRM_SECONDS)
        msg = self.st["messages"][self.mid]
        self.assertEqual((msg["state"], msg["attempt"]), ("typing", 2))
        self.assertEqual(msg["deadline"], self.T0 + 7 + 2 * COMMAND_CONFIRM_SECONDS)


class Watcher(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="watch-")
        journal(self.state, [(1, 100, "session.requested", "b", {"cwd": "/w", "session_id": "s-b", **OWNER}),
                             (2, 107, "session.started", "b", {"source": "resume"}),
                             (3, 107, "message.received", None, {"to": "b", "text": "continue", **OWNER}),
                             (4, 107.4, "session.mcp", "b", {"servers": {"srv-a": "pending", "srv-b": "pending"}})])
        self.w = watch.Watcher(self.state, {}, ask=lambda text: {"trouble": False}, needs=None, urgent=None,
                               recap=lambda text: "")
        self.step(108)

    def step(self, now):
        self.w.step(now)
        return [ev for path, ev in spool.take(os.path.join(self.state, "spool")) if not os.unlink(path)]

    def alerts(self, now):
        return [e for e in self.step(now) if e["data"].get("kind") == "undelivered"]

    def test_no_alarm_while_the_session_still_connects_mcp(self):
        self.assertLess(watch.UNDELIVERED, MCP_WAIT)
        self.assertEqual(self.alerts(107 + watch.UNDELIVERED + 1), [],
                         "a false 'not delivered': the turn starts by itself once MCP_TIMEOUT runs out")

    def test_no_init_within_mcp_timeout_alarms_after_it(self):
        self.assertEqual(self.alerts(107 + MCP_WAIT + watch.UNDELIVERED - 1), [])
        self.assertEqual(len(self.alerts(107 + MCP_WAIT + watch.UNDELIVERED + 1)), 1)

    def test_a_quick_init_alarms_as_before(self):
        journal(self.state, [(5, 109, "session.info", "b", {"mcp": ["srv-c"]})])
        self.step(110)
        self.assertEqual(len(self.alerts(107 + watch.UNDELIVERED + 1)), 1)


if __name__ == "__main__":
    unittest.main()
