"""A new Claude Code process reads no input until its MCP servers are up (up to MCP_TIMEOUT, 180 s). The core knows it
(`mcp_init`, core/parking.py), but the state of such a session was a plain `idle`: `hub status` and the page said
nothing, the owner saw a free session that does not answer. A replay of a resumed session: session.started(resume),
the owner's line typed, session.mcp with two servers pending, the turn at +180 s. Until the turn the page snapshot and
`hub status` name the servers and the seconds left; after the turn, the system init or the exit - nothing."""
import tests  # noqa: F401 - keeps the machine's settings out
import os
import re
import subprocess
import sys
import tempfile
import unittest

import platforms
from core import journal, reducer, views
from core.parking import MCP_WAIT

HUB_CLI = os.path.join(os.path.dirname(__file__), "..", "bin", "hub")
OWNER = {"channel": "cli", "owner": True}
T0 = 1000.0
PENDING = {"srv-a": "pending", "srv-b": "pending", "srv-c": "connected"}


def feed(rows):
    """The page's read model fed with a replay: (ts, type, entity, data)."""
    reader = views.Reader()
    for seq, (ts, kind, entity, data) in enumerate(rows, 1):
        reader.feed({"seq": seq, "ts": ts, "src": "test", "type": kind, "entity": entity, "data": data})
    return reader


START = [
    (T0, "session.requested", "b", {"cwd": "/w", "session_id": "s-b", **OWNER}),
    (T0 + 7, "session.started", "b", {"source": "resume"}),
    (T0 + 7, "message.received", None, {"to": "b", "text": "continue", **OWNER}),
]
MCP = (T0 + 7.4, "session.mcp", "b", {"servers": PENDING})


def row(reader, now):
    (s,) = [x for x in reader.snapshot(now=now)["sessions"] if x["name"] == "b"]
    return s


class PageSnapshot(unittest.TestCase):
    def test_the_servers_and_the_seconds_left_are_in_the_row(self):
        reader = feed([*START, MCP])
        s = row(reader, T0 + 37)
        self.assertEqual(s["state"], "idle")   # the state machine is as it was: only the display tells the truth
        self.assertEqual(s["mcp_wait"]["servers"], ["srv-a", "srv-b"])   # the connected one is not waited for
        self.assertEqual(s["mcp_wait"]["until"], T0 + 7 + MCP_WAIT)

    def test_before_the_first_status_it_waits_for_unnamed_servers(self):
        s = row(feed(START), T0 + 8)
        self.assertEqual(s["mcp_wait"], {"servers": [], "since": T0 + 7, "until": T0 + 7 + MCP_WAIT})

    def test_a_later_status_replaces_the_list(self):
        reader = feed([*START, MCP, (T0 + 60, "session.mcp", "b", {"servers": {"srv-a": "pending", "srv-b": "connected"}})])
        self.assertEqual(row(reader, T0 + 61)["mcp_wait"]["servers"], ["srv-a"])

    def test_no_wait_after_the_turn_the_init_or_the_exit(self):
        for end in ((T0 + 187, "turn.started", "b", {"marker": None}),
                    (T0 + 20, "session.info", "b", {"mcp": ["srv-a"]}),
                    (T0 + 30, "session.exited", "b", {"code": 1})):
            with self.subTest(end[1]):
                self.assertIsNone(row(feed([*START, MCP, end]), end[0] + 1)["mcp_wait"])

    def test_a_status_with_every_server_up_ends_the_wait(self):
        """Up before the first line came: nothing to wait for, though no init comes until the owner types."""
        up = (T0 + 9, "session.mcp", "b", {"servers": {"srv-a": "connected", "srv-b": "connected"}})
        self.assertIsNone(row(feed([*START, MCP, up]), T0 + 10)["mcp_wait"])

    def test_a_status_after_the_init_is_not_a_wait(self):
        rows = [*START, (T0 + 20, "session.info", "b", {"mcp": ["srv-a"]}), (T0 + 25, "session.mcp", "b", {"servers": PENDING})]
        self.assertIsNone(row(feed(rows), T0 + 26)["mcp_wait"])

    def test_a_wait_that_ran_out_is_not_shown(self):
        """No system init and no turn for longer than MCP_TIMEOUT: Claude Code gave up waiting, the claim would be stale."""
        self.assertIsNone(row(feed([*START, MCP]), T0 + 7 + MCP_WAIT + 1)["mcp_wait"])

    def test_the_servers_of_the_previous_process_are_forgotten(self):
        reader = feed([*START, MCP, (T0 + 50, "session.started", "b", {"source": "resume"})])
        self.assertEqual(row(reader, T0 + 51)["mcp_wait"]["servers"], [])

    def test_a_sleeping_session_waits_for_nothing(self):
        """A process that ended without session.exited (release, a respawn) leaves the mark: no process, no wait."""
        reader = feed([*START, MCP, (T0 + 20, "session.ended", "b", {})])
        s = row(reader, T0 + 21)
        self.assertEqual(s["state"], "asleep")
        self.assertIsNone(s["mcp_wait"])

    def test_clear_in_the_same_process_is_not_a_wait(self):
        reader = feed([*START, MCP, (T0 + 50, "session.started", "b", {"source": "clear", "session_id": "s-b2"})])
        self.assertIsNone(row(reader, T0 + 51)["mcp_wait"])


class StateKeys(unittest.TestCase):
    def test_the_list_goes_with_mcp_init(self):
        st = reducer.empty()
        for seq, (ts, kind, entity, data) in enumerate([*START, MCP, (T0 + 187, "turn.started", "b", {"marker": None})], 1):
            st, _, _ = reducer.reduce(st, {"seq": seq, "ts": ts, "type": kind, "entity": entity, "data": data})
        self.assertNotIn("mcp_init", st["sessions"]["b"])
        self.assertNotIn("mcp_pending", st["sessions"]["b"])

    def test_a_process_gone_without_an_exit_takes_its_list_too(self):
        """Release, park, take write no session.exited: the core drops the mark there, and the list goes with it."""
        st = reducer.empty()
        for seq, (ts, kind, entity, data) in enumerate([*START, MCP, (T0 + 20, "session.ended", "b", {})], 1):
            st, _, _ = reducer.reduce(st, {"seq": seq, "ts": ts, "type": kind, "entity": entity, "data": data})
        self.assertNotIn("mcp_init", st["sessions"]["b"])
        self.assertNotIn("mcp_pending", st["sessions"]["b"])


class HubStatus(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="st-mcp-start-")
        self.addCleanup(platforms.current.rmtree, self.state)
        self.j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        self.j.append("session.requested", "b", {"cwd": "/w", "session_id": "s-b", **OWNER})
        self.j.append("session.started", "b", {"source": "resume", "session_id": "s-b"})

    def lines(self):
        env = {**os.environ, "HUB_STATE_DIR": self.state}
        env.pop("HUB_SESSION", None)
        out = subprocess.run([sys.executable, HUB_CLI, "status"], capture_output=True, text=True, env=env).stdout
        return [ln for ln in out.splitlines() if "MCP" in ln]

    def test_the_status_names_the_servers_and_the_seconds_left(self):
        self.j.append("session.mcp", "b", {"servers": PENDING})
        (line,) = self.lines()
        m = re.search(r"waiting for MCP: srv-a, srv-b \((\d+) s left\)", line)
        self.assertIsNotNone(m, line)
        self.assertTrue(MCP_WAIT - 60 < int(m.group(1)) <= MCP_WAIT, line)

    def test_no_servers_named_yet(self):
        (line,) = self.lines()
        self.assertRegex(line, r"waiting for MCP \(\d+ s left\)")

    def test_nothing_after_the_turn(self):
        self.j.append("session.mcp", "b", {"servers": PENDING})
        self.j.append("turn.started", "b", {"marker": None, "prompt_head": "x"})
        self.assertEqual(self.lines(), [])


if __name__ == "__main__":
    unittest.main()
