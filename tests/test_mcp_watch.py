"""An MCP that dropped in the middle of work is reconnected at once, in place. Before, the core knew the MCP status only
from init at start and fixed a server that failed to come up by restarting the session after the turn; a drop in the
middle of work it did not see at all.

The host asks Claude Code for `mcp_status` (a control request, no model turn) at the core's request — every few minutes
and right after an mcp__… tool error; a failed server gets `mcp_reconnect`. If that fails — tell the hub; the session
only if it stumbled on it itself (otherwise a pointless wake-up)."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import tempfile
import unittest

from core import mcpwatch, reducer
from hosts.streamjson import StreamHost

OWNER = {"channel": "cli", "owner": True}


class FakePopen:
    pid = 1

    def __init__(self):
        self.lines = []
        self.stdin = self

    def poll(self):
        return None

    def write(self, line):
        self.lines.append(json.loads(line))

    def flush(self):
        pass


class Host(unittest.TestCase):
    def setUp(self):
        from hosts.streamjson import StreamProcess
        self.host = StreamHost(tempfile.mkdtemp(prefix="mw-"))
        self.popen = FakePopen()
        self.host.procs["a"] = StreamProcess(self.popen)

    def test_status_and_reconnect_are_control_requests(self):
        self.host.mcp_status("a")
        self.host.mcp_reconnect("a", server="maps")
        (st, rc) = self.popen.lines
        self.assertEqual((st["type"], st["request"]), ("control_request", {"subtype": "mcp_status"}))
        self.assertEqual(rc["request"], {"subtype": "mcp_reconnect", "serverName": "maps"})

    def test_an_unknown_request_is_marked_unsupported(self):
        self.host.mcp_reconnect("a", server="maps")
        rid = self.popen.lines[-1]["request_id"]
        self.host.inbox.put(("a", {"type": "control_response", "response": {
            "request_id": rid, "subtype": "error", "error": "Unsupported control request subtype: mcp_reconnect"}}))
        (fact,) = self.host.poll()
        self.assertEqual(fact[0], "session.mcp_reconnected")
        self.assertTrue(fact[2]["unsupported"])

    def test_answers_become_facts(self):
        self.host.mcp_status("a")
        self.host.mcp_reconnect("a", server="maps")
        self.host.mcp_reconnect("a", server="docs")
        st, ok, bad = (x["request_id"] for x in self.popen.lines)
        for rid, resp in ((st, {"subtype": "success", "response": {"mcpServers": [
                {"name": "maps", "status": "failed", "error": "Connection closed"},
                {"name": "docs", "status": "connected"}, {"name": "design", "status": "needs-auth"}]}}),
                (ok, {"subtype": "success", "response": {}}), (bad, {"subtype": "error", "error": "Connection closed"})):
            self.host.inbox.put(("a", {"type": "control_response", "response": {"request_id": rid, **resp}}))
        facts = self.host.poll()
        self.assertEqual(facts[0], ("session.mcp", "a", {"servers": {"maps": "failed", "docs": "connected",
                                                                    "design": "needs-auth"}}))
        self.assertEqual(facts[1], ("session.mcp_reconnected", "a", {"server": "maps", "ok": True, "error": None}))
        self.assertEqual(facts[2], ("session.mcp_reconnected", "a", {"server": "docs", "ok": False,
                                                                     "error": "Connection closed"}))

    def test_a_calm_status_is_said_once(self):
        # a check every few minutes: all connected and unchanged — not a fact, the journal does not grow for nothing
        def ask():
            self.host.mcp_status("a")
            rid = self.popen.lines[-1]["request_id"]
            self.host.inbox.put(("a", {"type": "control_response", "response": {
                "request_id": rid, "subtype": "success", "response": {"mcpServers": [{"name": "docs", "status": status}]}}}))
            return [f for f in self.host.poll() if f[0] == "session.mcp"]
        status = "connected"
        self.assertEqual(len(ask()), 1)
        self.assertEqual(ask(), [])
        status = "failed"
        self.assertEqual(len(ask()), 1)
        self.assertEqual(len(ask()), 1)            # while not connected — every time: the core decides when to retry

    def ask(self, servers):
        self.host.mcp_status("a")
        rid = self.popen.lines[-1]["request_id"]
        self.host.inbox.put(("a", {"type": "control_response", "response": {"request_id": rid, "subtype": "success",
            "response": {"mcpServers": [{"name": n, "status": s} for n, s in servers.items()]}}}))
        return [f for f in self.host.poll() if f[0] == "session.mcp"]

    def test_a_server_waiting_for_login_for_days_is_not_said_every_few_minutes(self):
        # on the hub a server waits for sign-in for days: a fact on every poll would be hundreds of journal lines a day per session
        self.assertEqual(len(self.ask({"docs": "connected", "pay": "needs-auth"})), 1)
        self.assertEqual(self.ask({"docs": "connected", "pay": "needs-auth"}), [])
        self.assertEqual(len(self.ask({"docs": "connected", "pay": "connected"})), 1)   # changed — report

    def test_the_poll_is_compared_with_what_init_said(self):
        # init: it was down (the core learned it from session.info); it came up by itself before the poll — the core must learn that too
        self.assertEqual(len(self.ask({"docs": "connected"})), 1)
        self.host.inbox.put(("a", {"type": "system", "subtype": "init", "mcp_servers": [
            {"name": "docs", "status": "failed"}]}))
        self.host.poll()
        self.assertEqual(self.ask({"docs": "connected"}), [("session.mcp", "a", {"servers": {"docs": "connected"}})])
        self.host.inbox.put(("a", {"type": "system", "subtype": "init", "mcp_servers": [
            {"name": "docs", "status": "connected"}]}))
        self.host.poll()
        self.assertEqual(self.ask({"docs": "connected"}), [])                  # as in init — the core knows

    def test_a_failed_mcp_tool_is_marked(self):
        self.host.inbox.put(("a", {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "mcp__maps__search", "input": {}},
            {"type": "tool_use", "id": "t2", "name": "Bash", "input": {}}]}}))
        self.host.inbox.put(("a", {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "MCP error -32000: Connection closed"},
            {"type": "tool_result", "tool_use_id": "t2", "is_error": True, "content": "exit 1"}]}}))
        res = [f[2] for f in self.host.poll() if f[0] == "turn.tool_result"]
        self.assertEqual([r.get("mcp") for r in res], ["maps", None])


class OtherHost(unittest.TestCase):
    def test_tmux_does_not_reconnect_on_the_spot_but_does_not_fail_either(self):
        from hosts.tmux import TmuxHost
        host = TmuxHost.__new__(TmuxHost)   # tmux is not needed: the answer does not come from tmux
        self.assertEqual(host.mcp_reconnect("a", server="maps"), "unsupported")
        self.assertFalse(hasattr(host, "mcp_status"))   # check_mcp sends no questions to a host without status


class Core(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.actions = reducer.empty(), 0, []
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.requested", "rel", cwd="/r", session_id="s-r", role="agent", about="Releases", **OWNER)
        self.ev("session.started", "hub")
        self.ev("session.started", "rel")
        self.ev("session.info", "rel", mcp=["maps", "docs"], mcp_failed=[], mcp_auth=[])

    def ev(self, type, entity=None, ts=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts or 1000 + self.seq, "type": type,
                                                      "entity": entity, "data": data})
        self.actions += acts
        return why

    def did(self, do):
        return [a for a in self.actions if a.get("do") == do]

    def to(self, name):
        return [m["text"] for m in self.st["messages"].values() if m["session"] == name]

    def test_a_dropped_server_is_reconnected_on_the_spot_once(self):
        self.ev("session.mcp", "rel", servers={"maps": "failed", "docs": "connected"}, ts=2000)
        self.assertEqual(self.did("mcp_reconnect"), [{"do": "mcp_reconnect", "session": "rel", "server": "maps"}])
        self.assertFalse(self.st["sessions"]["rel"].get("dirty"))              # without restarting the session
        self.assertEqual(self.st["sessions"]["rel"]["info"]["mcp_failed"], ["maps"])   # hub status sees it
        self.ev("session.mcp", "rel", servers={"maps": "failed"}, ts=2060)
        self.assertEqual(len(self.did("mcp_reconnect")), 1)                    # not in a loop
        self.ev("session.mcp", "rel", servers={"maps": "failed"}, ts=2000 + mcpwatch.RETRY)
        self.assertEqual(len(self.did("mcp_reconnect")), 2)                    # but not forever

    def test_a_quiet_fix_wakes_nobody(self):
        self.ev("session.mcp", "rel", servers={"maps": "failed"})
        self.ev("session.mcp_reconnected", "rel", server="maps", ok=True)
        self.assertEqual(self.to("rel"), [])
        self.assertEqual(self.to("hub"), [])
        self.assertEqual(self.st["sessions"]["rel"]["info"]["mcp_failed"], [])

    def test_a_failed_tool_asks_for_the_status_and_the_session_hears_about_the_fix(self):
        self.ev("turn.tool_result", "rel", id="t1", error=True, text="Connection closed", mcp="maps")
        self.assertEqual(self.did("mcp_status"), [{"do": "mcp_status", "session": "rel"}])
        self.ev("turn.tool_result", "rel", id="t2", error=True, text="Connection closed", mcp="maps")
        self.assertEqual(len(self.did("mcp_status")), 1)                       # one error is enough
        self.ev("session.mcp", "rel", servers={"maps": "failed"})
        self.ev("session.mcp_reconnected", "rel", server="maps", ok=True)
        (text,) = self.to("rel")
        self.assertIn("maps", text)
        self.assertIn("repeat it", text)

    def test_a_session_that_searched_for_the_dropped_server_is_told_to_repeat(self):
        # deferred MCP tools are fetched through ToolSearch: the session found nothing and did not call mcp__
        self.ev("session.mcp", "rel", servers={"maps": "failed", "docs": "connected"})
        self.ev("turn.tool", "rel", id="t1", name="ToolSearch", input={"query": "+maps route"})
        self.ev("session.mcp_reconnected", "rel", server="maps", ok=True)
        (text,) = self.to("rel")
        self.assertIn("repeat it", text)

    def test_a_session_that_did_not_touch_the_dropped_server_is_not_woken(self):
        self.ev("session.mcp", "rel", servers={"maps": "failed", "docs": "connected"})
        self.ev("turn.tool", "rel", id="t1", name="Bash", input={"command": "ls"})
        self.ev("turn.tool", "rel", id="t2", name="ToolSearch", input={"query": "+docs page"})
        self.ev("session.mcp_reconnected", "rel", server="maps", ok=True)
        self.assertEqual(self.to("rel"), [])

    def test_a_server_named_in_the_result_counts_as_touched(self):
        self.ev("session.mcp", "rel", servers={"maps": "failed"})
        self.ev("turn.result", "rel", text="route not built: maps unavailable")
        self.ev("session.mcp_reconnected", "rel", server="maps", ok=True)
        self.assertEqual(len(self.to("rel")), 1)

    def test_a_failed_server_and_a_server_waiting_for_login_at_the_same_start(self):
        # one failed on a timeout, the other waits for sign-in: the sign-in is not lost (the hub was told, after sign-in the session picks it up)
        self.ev("session.info", "rel", mcp=["maps", "docs"], mcp_failed=["maps"], mcp_auth=["docs"])
        self.assertEqual(self.st["sessions"]["rel"]["info"]["mcp_auth"], ["docs"])
        self.assertEqual(self.st["sessions"]["rel"]["info"]["mcp_failed"], ["maps"])
        self.assertIn("hub mcp login rel docs", self.to("hub")[-1])
        self.actions.clear()
        self.ev("mcp.logged_in", None, server="docs", **OWNER)
        self.assertEqual([a["do"] for a in self.actions if a.get("session") == "rel"], ["release", "spawn"])   # idle

    def test_a_host_that_cannot_reconnect_restarts_the_session_when_it_is_free(self):
        self.ev("session.info", "rel", mcp=["maps"], mcp_failed=["maps"], mcp_auth=[])
        self.assertEqual(self.did("mcp_reconnect"), [{"do": "mcp_reconnect", "session": "rel", "server": "maps"}])
        self.actions.clear()
        self.ev("host.done", "rel", do="mcp_reconnect", result="unsupported", server="maps")
        self.assertEqual([a["do"] for a in self.actions if a.get("session") == "rel"], ["release", "spawn"])   # idle
        self.actions.clear()
        self.ev("session.info", "rel", mcp=["maps"], mcp_failed=["maps"], mcp_auth=[], ts=1000 + 3 * mcpwatch.RETRY)
        self.assertEqual(self.did("mcp_reconnect"), [])                        # after a restart — not in a loop
        self.ev("session.info", "rel", mcp=["maps"], mcp_failed=[], mcp_auth=[], ts=1001 + 3 * mcpwatch.RETRY)
        self.ev("session.info", "rel", mcp=["maps"], mcp_failed=["maps"], mcp_auth=[], ts=1002 + 3 * mcpwatch.RETRY)
        self.assertEqual(len(self.did("mcp_reconnect")), 1)                    # it had connected — a new drop, a new attempt

    def test_a_claude_code_without_reconnect_restarts_the_session_and_does_not_retry(self):
        self.ev("session.mcp", "rel", servers={"maps": "failed"}, ts=2000)
        self.assertEqual(len(self.did("mcp_reconnect")), 1)
        self.actions.clear()
        self.ev("session.mcp_reconnected", "rel", server="maps", ok=False, unsupported=True, ts=2001,
                error="Unsupported control request subtype: mcp_reconnect")
        self.assertEqual([a["do"] for a in self.actions if a.get("session") == "rel"], ["release", "spawn"])   # idle
        self.assertEqual(self.to("hub"), [])                                   # nothing to the hub or the session
        self.assertEqual(self.to("rel"), [])
        self.actions.clear()
        self.ev("session.mcp", "rel", servers={"maps": "failed"}, ts=2000 + mcpwatch.RETRY - 1)
        self.assertEqual(self.did("mcp_reconnect"), [])                        # tried holds: no requests in a loop
        self.ev("session.info", "rel", mcp=["maps"], mcp_failed=["maps"], mcp_auth=[], ts=2000 + 3 * mcpwatch.RETRY)
        self.assertEqual(self.did("mcp_reconnect"), [])                        # and after a restart — not in a loop
        self.assertFalse(self.st["sessions"]["rel"].get("dirty"))

    def test_a_failed_reconnect_goes_to_the_hub_once(self):
        self.ev("turn.tool_result", "rel", id="t1", error=True, text="x", mcp="maps")
        self.ev("session.mcp", "rel", servers={"maps": "failed"}, ts=2000)
        self.ev("session.mcp_reconnected", "rel", server="maps", ok=False, error="handshake rejected", ts=2001)
        (hub,) = self.to("hub")
        self.assertIn("maps", hub)
        self.assertIn("rel", hub)
        self.assertIn("handshake rejected", hub)
        self.assertEqual(len(self.to("rel")), 1)                               # it stumbled — it knows the attempt failed
        self.ev("session.mcp", "rel", servers={"maps": "failed"}, ts=2000 + mcpwatch.RETRY)
        self.ev("session.mcp_reconnected", "rel", server="maps", ok=False, error="handshake rejected",
                ts=2001 + mcpwatch.RETRY)
        self.assertEqual(len(self.to("hub")), 1)                               # to the hub — not every 10 minutes
        self.ev("session.mcp", "rel", servers={"maps": "connected"}, ts=5000)   # it came up — we will report again if it falls
        self.ev("session.mcp", "rel", servers={"maps": "failed"}, ts=5100)
        self.ev("session.mcp_reconnected", "rel", server="maps", ok=False, error="x", ts=5101)
        self.assertEqual(len(self.to("hub")), 2)

    def start(self, *names, ts):
        for n in names:
            self.ev("session.requested", n, cwd=f"/{n}", session_id=f"s-{n}", **OWNER)
            self.ev("session.started", n)
            self.ev("session.info", n, mcp=["maps", "design"], mcp_failed=["design"], mcp_auth=[], ts=ts)

    def tick(self, names, ts, error="refused by policy"):
        for i, n in enumerate(names):
            self.ev("session.mcp", n, servers={"maps": "connected", "design": "failed"}, ts=ts + i)
            self.ev("session.mcp_reconnected", n, server="design", ok=False, error=error, ts=ts + i)

    def test_a_server_down_for_every_session_goes_to_the_hub_once_with_the_list(self):
        # the server is unreachable from this machine by design (a desktop app's localhost): before, a message to the hub
        # for every new session with this server; now one per (server, reason) per day, with a list of sessions
        names = [f"fe{i}" for i in range(5)]
        self.start(*names, ts=2000)
        for k in range(6):
            self.tick(names, 2000 + k * mcpwatch.RETRY)
        (hub,) = self.to("hub")
        self.assertIn("design", hub)
        self.assertIn("refused by policy", hub)
        for n in names:
            self.assertIn(n, hub)                                            # all that have it down right now
        self.start("fe5", ts=9000)                                           # a new session the same day — silently
        self.tick(["fe5"], 9000)
        self.assertEqual(len(self.to("hub")), 1)
        self.start("fe7", ts=9100)                                           # a new reason — report again
        self.tick(["fe7"], 9100 + mcpwatch.RETRY, error="handshake rejected")
        self.assertEqual(len(self.to("hub")), 2)
        self.assertIn("fe7", self.to("hub")[-1])
        self.start("fe6", ts=10000 + mcpwatch.DAY)                            # a day has passed — report again
        self.tick(["fe6"], 10000 + mcpwatch.DAY, error="handshake rejected")
        self.assertEqual(len(self.to("hub")), 3)

    def test_a_server_that_came_up_and_fell_again_is_said_again(self):
        self.start("fe0", "fe1", ts=2000)
        self.tick(["fe0", "fe1"], 2000)
        self.assertEqual(len(self.to("hub")), 1)
        self.ev("session.mcp", "fe0", servers={"maps": "connected", "design": "connected"}, ts=3000)
        self.tick(["fe0"], 3050)                                             # it is still down on fe1 — not "came up"
        self.assertEqual(len(self.to("hub")), 1)
        self.ev("session.mcp", "fe0", servers={"maps": "connected", "design": "connected"}, ts=3080)
        self.ev("session.mcp_reconnected", "fe1", server="design", ok=True, ts=3090)   # it came up everywhere
        self.tick(["fe0"], 3100)
        self.assertEqual(len(self.to("hub")), 2)

    def test_a_recovery_seen_only_by_a_new_session_counts(self):
        # the session reported to the hub is gone; the server came up on a new one — the next drop the same day goes to the hub again
        self.start("fe0", ts=2000)
        self.tick(["fe0"], 2000)
        self.ev("session.ended", "fe0")
        self.ev("session.requested", "fe1", cwd="/fe1", session_id="s-fe1", **OWNER)
        self.ev("session.started", "fe1")
        self.ev("session.info", "fe1", mcp=["maps", "design"], mcp_failed=[], mcp_auth=[], ts=3000)
        self.tick(["fe1"], 4000)
        self.assertEqual(len(self.to("hub")), 2)

    def test_a_session_whose_reason_keeps_changing_tells_the_hub_once(self):
        self.start("fe0", ts=2000)
        for k in range(6):
            self.tick(["fe0"], 2000 + k * mcpwatch.RETRY, error=("bad config", "refused by policy")[k % 2])
        self.assertEqual(len(self.to("hub")), 1)

    def test_numbers_in_the_reason_do_not_make_it_new(self):
        self.start("fe0", "fe1", ts=2000)
        self.tick(["fe0"], 2000, error="refused by policy 127.0.0.1:3845 (request 17)")
        self.tick(["fe1"], 2100, error="refused by policy 127.0.0.1:3845 (request 18)")
        self.assertEqual(len(self.to("hub")), 1)

    def test_a_login_that_expired_mid_session_goes_to_the_hub_as_at_start(self):
        self.ev("session.mcp", "rel", servers={"maps": "needs-auth", "docs": "connected"})
        self.assertEqual(self.did("mcp_reconnect"), [])                        # a sign-in cannot be restored by reconnecting
        (hub,) = self.to("hub")
        self.assertIn("hub mcp login rel maps", hub)
        self.assertEqual(self.st["sessions"]["rel"]["info"]["mcp_auth"], ["maps"])

    def test_due_lists_live_sessions_with_servers_every_few_minutes(self):
        self.ev("session.requested", "plain", cwd="/p", session_id="s-p", **OWNER)
        self.ev("session.started", "plain")
        self.assertEqual(mcpwatch.due(self.st, {}, 10_000), ["rel"])          # without MCP — nothing to ask
        self.assertEqual(mcpwatch.due(self.st, {"rel": 10_000 - 60}, 10_000), [])
        self.assertEqual(mcpwatch.due(self.st, {"rel": 10_000 - mcpwatch.EVERY}, 10_000), ["rel"])

    def blip(self, server, ts, error="read ECONNRESET"):
        """One round of the ladder: the session is asked at the due time, the server is still failed, the reconnect fails."""
        self.ev("session.mcp", "rel", servers={server: "failed"}, ts=ts)
        self.ev("session.mcp_reconnected", "rel", server=server, ok=False, error=error, ts=ts + 1)

    def test_a_network_blip_is_retried_quietly_and_nobody_hears_of_it(self):
        # 31.10: a short network drop (ECONNRESET at several remote MCP servers) — one reconnect in the same drop failed,
        # the hub was told at once and the session was told to work without the server; the next try came in 10 min
        self.ev("turn.tool_result", "rel", id="t1", error=True, text="read ECONNRESET", mcp="maps")
        self.blip("maps", 2000)
        self.assertEqual(self.to("hub"), [])
        self.assertEqual(self.to("rel"), [])
        nxt = self.st["sessions"]["rel"]["mcp_watch"]["fix"]["maps"]["next"]
        self.assertLess(nxt - 2001, mcpwatch.RETRY)                             # the next try is soon, not in 10 min
        self.assertEqual(mcpwatch.due(self.st, {"rel": 2001}, nxt - 1), [])
        self.assertEqual(mcpwatch.due(self.st, {"rel": 2001}, nxt), ["rel"])   # the daemon asks for status at that time
        self.assertEqual(mcpwatch.due(self.st, {"rel": nxt}, nxt + 1), [])     # once
        self.ev("session.mcp", "rel", servers={"maps": "failed"}, ts=nxt)
        self.assertEqual(len(self.did("mcp_reconnect")), 2)
        self.ev("session.mcp_reconnected", "rel", server="maps", ok=True, ts=nxt + 1)
        self.assertEqual(self.to("hub"), [])
        (text,) = self.to("rel")                                               # it stumbled — it repeats now
        self.assertIn("connected again", text)

    def test_a_server_that_stays_down_through_the_ladder_goes_to_the_hub_once(self):
        ts = 2000
        for _ in range(len(mcpwatch.LADDER)):
            self.blip("maps", ts)
            self.assertEqual(self.to("hub"), [])
            ts = self.st["sessions"]["rel"]["mcp_watch"]["fix"]["maps"]["next"]
        self.blip("maps", ts)
        (hub,) = self.to("hub")
        self.assertIn("ECONNRESET", hub)
        self.blip("maps", ts + mcpwatch.RETRY)                                 # then the old pace, without new notices
        self.assertEqual(len(self.to("hub")), 1)
        self.assertEqual(len(self.did("mcp_reconnect")), len(mcpwatch.LADDER) + 2)

    def test_a_server_error_of_the_service_is_retried_quietly_too(self):
        self.blip("maps", 2000, error="HTTP 500 Internal Server Error")
        self.assertEqual(self.to("hub"), [])

    def test_a_permanent_error_goes_to_the_hub_at_once(self):
        self.blip("maps", 2000, error="spawn npx ENOENT")
        (hub,) = self.to("hub")
        self.assertIn("ENOENT", hub)


class StatusRow(unittest.TestCase):
    """A granted tool whose MCP server did not come up: `hub status` listed it among the session's tools and said nothing
    else, as if it worked (a Codex session's browser MCP timed out at every start). The live session's row says which
    server is down and how to bring it back; an asleep one's list is its last process's and is not shown."""

    def setUp(self):
        self.events, self.seq = [], 0
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.requested", "rel", cwd="/r", session_id="s-r", role="agent", about="Releases", **OWNER)
        self.ev("session.started", "hub")
        self.ev("session.started", "rel")
        self.ev("session.info", "rel", host="codex", mcp=["browser:agents"], mcp_login_pty=False)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.events.append({"seq": self.seq, "ts": 1000 + self.seq, "type": type, "entity": entity, "data": data})

    def status(self):
        import os
        import subprocess
        import sys
        import platforms
        tmp = tempfile.mkdtemp(prefix="mcp-status-")
        self.addCleanup(platforms.current.rmtree, tmp)
        with open(os.path.join(tmp, "journal.jsonl"), "w", encoding="utf-8") as f:
            for e in self.events:
                f.write(json.dumps(e) + "\n")
        env = {**os.environ, "HUB_STATE_DIR": tmp, "HUB_CLI_WAIT": "0", "HUB_LANG": "en"}
        env.pop("HUB_SESSION", None)
        hub = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")
        r = subprocess.run([sys.executable, hub, "status"], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_a_failed_server_is_named_with_the_way_back(self):
        self.ev("session.mcp", "rel", servers={"browser:agents": "failed"})
        out = self.status()
        self.assertIn("browser:agents", out)
        self.assertIn("hub mcp reconnect browser:agents --session rel", out)

    def test_a_connected_server_or_an_asleep_session_has_no_row(self):
        self.ev("session.mcp", "rel", servers={"browser:agents": "connected"})
        self.assertNotIn("hub mcp reconnect", self.status())
        self.ev("session.mcp", "rel", servers={"browser:agents": "failed"})
        self.ev("session.ended", "rel")
        self.assertNotIn("hub mcp reconnect", self.status())


if __name__ == "__main__":
    unittest.main()
