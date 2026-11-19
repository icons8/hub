"""MCP servers waiting for a login, per server: since when, and which sessions need it (the snapshot's mcp_waits).
`info.mcp_auth` is dropped on every session start (the status comes again at the start of the turn), so by it alone the
waiting time restarted with every session restart and nobody saw that a login had been stale for days. Two servers under
one name with different addresses (`.mcp.json` of different projects) are two waits: a login into one does not clear the
other. And a reconnect on request (`hub mcp reconnect`): the core already reconnects a failed server by itself, once per
mcpwatch.RETRY; after the owner brought the service up or logged in elsewhere, waiting 10 minutes is for nothing."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import tempfile
import unittest

from core import mcpwatch, reducer
from core.views import Reader
from hosts.streamjson import StreamHost

OWNER = {"channel": "cli", "owner": True}
A = {"fp": "aaaaaaaaaaaa", "host": "a.example"}
B = {"fp": "bbbbbbbbbbbb", "host": "b.example"}


class Fold(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.actions = reducer.empty(), 0, []

    def ev(self, type, entity=None, ts=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts or 1000 + self.seq, "type": type,
                                                      "entity": entity, "data": data})
        self.actions += acts
        return why

    def start(self, *names):
        for n in names:
            self.ev("session.requested", n, cwd="/w", session_id="s-" + n, **OWNER)
            self.ev("session.started", n, session_id="s-" + n)

    def info(self, name, auth, ts=None, mcp=None):
        self.ev("session.info", name, ts=ts, mcp=mcp or sorted({"other", *auth}), mcp_failed=[], mcp_auth=auth)

    def waits(self):
        return {w["server"] + ("@" + w["host"] if w.get("host") else ""): w for w in view(self.st)}

    def test_a_restart_does_not_reset_since(self):
        self.start("a")
        self.info("a", ["docs"], ts=5000)
        self.ev("session.started", "a", session_id="s-a", source="resume", ts=9000)
        self.assertNotIn("mcp_auth", self.st["sessions"]["a"]["info"])   # as before: the fresh status comes with the turn
        self.assertEqual(self.waits()["docs"]["since"], 5000)
        self.info("a", ["docs"], ts=9100)
        self.assertEqual(self.waits()["docs"]["since"], 5000)

    def test_since_is_the_first_report_and_holds_while_any_session_names_it(self):
        self.start("a", "b")
        self.info("a", ["docs"], ts=5000)
        self.info("b", ["docs"], ts=6000)
        self.ev("session.info", "a", ts=7000, mcp=[], mcp_failed=[], mcp_auth=[])   # docs left a's settings
        self.assertEqual(self.waits()["docs"]["since"], 5000)
        self.assertEqual(self.waits()["docs"]["sessions"], ["b"])
        self.ev("session.info", "b", ts=8000, mcp=[], mcp_failed=[], mcp_auth=[])
        self.assertEqual(self.waits(), {})
        self.info("a", ["docs"], ts=9000)               # stale again: a new wait
        self.assertEqual(self.waits()["docs"]["since"], 9000)

    def test_a_login_clears_the_server_for_everyone_sleeping_too(self):
        self.start("a", "b")
        self.info("a", ["docs", "pay"], ts=5000)
        self.ev("session.ended", "a")
        self.info("b", ["docs"], ts=6000)
        self.ev("mcp.logged_in", "b", server="docs", **OWNER)
        self.assertEqual(sorted(self.waits()), ["pay"])
        self.assertEqual(self.waits()["pay"]["sessions"], ["a"])

    def test_a_parked_session_keeps_the_wait(self):
        self.start("a")
        self.info("a", ["docs"], ts=5000)
        self.ev("session.parked", "a", channel="core", why="restart")
        self.assertEqual(self.waits()["docs"]["sessions"], ["a"])

    def test_a_session_where_the_server_is_connected_clears_the_wait_for_all(self):
        # two executors reported the server waiting and went to sleep; it was signed in outside `hub mcp login`, other
        # sessions had it connected, while by the sleeping ones it "waited" for days
        self.start("a", "b", "c")
        self.info("a", ["docs"], ts=5000)
        self.info("b", ["docs"], ts=5100)
        self.ev("session.ended", "a")
        self.ev("session.ended", "b")
        self.info("c", [], ts=6000, mcp=["docs"])        # c has docs connected
        self.assertEqual(self.waits(), {})
        self.info("a", ["docs"], ts=7000)                # a new report: waiting again, from now
        self.assertEqual(self.waits()["docs"]["since"], 7000)
        self.ev("session.mcp", "c", servers={"docs": "connected"}, ts=8000)
        self.assertEqual(self.waits(), {})

    def test_a_removed_session_no_longer_holds_the_server(self):
        self.start("a", "b")
        self.info("a", ["docs"], ts=5000)
        self.info("b", ["pay"], ts=5000)
        self.ev("session.ended", "a")
        self.ev("session.remove_requested", "a", **OWNER)
        self.assertEqual(self.st["sessions"]["a"]["state"], "removed")
        self.assertEqual(sorted(self.waits()), ["pay"])
        self.assertEqual(sorted(self.st["mcp_waits"]), ["pay"])      # not only hidden: forgotten

    def test_a_failed_server_at_start_does_not_reset_since(self):
        self.start("a")
        self.info("a", ["docs"], ts=5000)
        self.ev("session.info", "a", ts=6000, mcp=["docs", "jira"], mcp_failed=["jira"], mcp_auth=["docs"])
        self.assertEqual(self.waits()["docs"]["since"], 5000)
        self.assertEqual(self.st["sessions"]["a"]["info"]["mcp_auth"], ["docs"])

    def test_the_mcp_watch_status_counts_too(self):
        self.start("a")
        self.info("a", [], ts=5000)
        self.ev("session.mcp", "a", servers={"docs": "needs-auth"}, ts=6000)
        self.assertEqual(self.waits()["docs"]["since"], 6000)
        self.ev("session.mcp", "a", servers={"docs": "connected"}, ts=7000)
        self.assertEqual(self.waits(), {})

    def test_a_rename_keeps_the_wait(self):
        self.start("a")
        self.info("a", ["docs"], ts=5000)
        self.ev("session.ended", "a")
        self.ev("session.renamed", "a", to="b", **OWNER)
        self.assertEqual(self.waits()["docs"]["sessions"], ["b"])

    def test_a_state_from_before_has_no_key_and_folds_on(self):
        self.start("a")
        self.st.pop("mcp_waits", None)
        for s in self.st["sessions"].values():
            s.pop("mcp_waits", None)
            s.pop("mcp_addr", None)
        self.ev("mcp.logged_in", "a", server="docs", **OWNER)
        self.info("a", ["docs"], ts=5000)
        self.assertEqual(self.waits()["docs"]["since"], 5000)

    def test_an_old_report_without_the_login_field_changes_nothing(self):
        self.start("a")
        self.info("a", ["docs"], ts=5000)
        self.ev("session.info", "a", ts=6000, mcp=["docs"], mcp_failed=[])   # a host from before 05.10
        self.assertEqual(self.waits()["docs"]["since"], 5000)

    def test_one_name_two_addresses_are_two_waits(self):
        # `.mcp.json` of two projects name different servers alike: a login into one is not a login into the other
        self.start("a", "b", "c")
        self.ev("session.mcp", "a", servers={"docs": "needs-auth"}, addr={"docs": A}, ts=5000)
        self.ev("session.mcp", "b", servers={"docs": "needs-auth"}, addr={"docs": B}, ts=5100)
        self.assertEqual(sorted(self.waits()), ["docs@a.example", "docs@b.example"])
        self.ev("mcp.logged_in", "a", server="docs", **OWNER)
        self.assertEqual(sorted(self.waits()), ["docs@b.example"])
        self.ev("session.mcp", "a", servers={"docs": "needs-auth"}, addr={"docs": A}, ts=6000)
        self.ev("session.mcp", "c", servers={"docs": "connected"}, addr={"docs": B}, ts=6100)   # B is in somewhere
        self.assertEqual(sorted(self.waits()), ["docs@a.example"])
        self.assertEqual(self.waits()["docs@a.example"]["since"], 6000)

    def test_an_address_learned_later_keeps_since(self):
        # init names servers without addresses; the first status poll brings them
        self.start("a")
        self.info("a", ["docs"], ts=5000)
        self.ev("session.mcp", "a", servers={"docs": "needs-auth"}, addr={"docs": A}, ts=5300)
        self.assertEqual(list(self.waits()), ["docs@a.example"])
        self.assertEqual(self.waits()["docs@a.example"]["since"], 5000)
        self.ev("session.started", "a", session_id="s-a", source="resume", ts=9000)
        self.info("a", ["docs"], ts=9100)               # a restart: the session's address is remembered
        self.assertEqual(list(self.waits()), ["docs@a.example"])

    def test_a_connected_server_with_an_unknown_address_does_not_clear_a_known_other(self):
        self.start("a", "c")
        self.ev("session.mcp", "a", servers={"docs": "needs-auth"}, addr={"docs": A}, ts=5000)
        self.info("c", [], ts=6000, mcp=["docs"])        # c's docs may be another server
        self.assertEqual(list(self.waits()), ["docs@a.example"])

    def test_a_login_at_a_known_address_clears_the_waits_whose_address_is_not_known(self):
        # right after an update every wait is at an unknown address: a sleeping session that named the server before its
        # first poll would "wait" for days after the login
        self.start("a", "b", "c")
        self.info("b", ["docs"], ts=4000)
        self.ev("session.ended", "b")
        self.ev("session.mcp", "a", servers={"docs": "needs-auth"}, addr={"docs": A}, ts=5000)
        self.ev("session.mcp", "c", servers={"docs": "needs-auth"}, addr={"docs": B}, ts=5100)
        self.ev("mcp.logged_in", "a", server="docs", **OWNER)
        self.assertEqual(list(self.waits()), ["docs@b.example"])

    def test_one_server_is_one_row_while_its_address_is_learned(self):
        self.start("a", "b", "c")
        self.info("b", ["docs"], ts=4000)
        self.ev("session.mcp", "a", servers={"docs": "needs-auth"}, addr={"docs": A}, ts=5000)
        self.assertEqual(view(self.st), [{"server": "docs", "since": 4000, "sessions": ["a", "b"], "host": "a.example", "fp": A["fp"]}])
        self.ev("session.mcp", "c", servers={"docs": "needs-auth"}, addr={"docs": B}, ts=5100)
        self.assertEqual([(w.get("host"), w["sessions"]) for w in view(self.st)],   # two known: which one b is — unknown
                         [(None, ["b"]), ("a.example", ["a"]), ("b.example", ["c"])])

    def test_a_pending_or_disabled_server_at_start_is_not_signed_in(self):
        self.start("a", "c")
        self.info("a", ["docs"], ts=5000)
        self.ev("session.info", "c", ts=6000, mcp=["docs"], mcp_failed=[], mcp_auth=[], mcp_connected=[])
        self.assertEqual(self.waits()["docs"]["since"], 5000)
        self.ev("session.info", "c", ts=6100, mcp=["docs"], mcp_failed=[], mcp_auth=[], mcp_connected=["docs"])
        self.assertEqual(self.waits(), {})

    def test_a_waiting_server_that_fails_or_hangs_at_start_keeps_since(self):
        # the fleet comes up after a hub restart: the server times out (failed) or is still pending — not signed in
        self.start("a")
        self.info("a", ["docs"], ts=5000)
        self.ev("session.info", "a", ts=6000, mcp=["docs"], mcp_failed=["docs"], mcp_auth=[], mcp_connected=[])
        self.assertEqual(self.waits()["docs"]["since"], 5000)
        self.ev("session.mcp", "a", servers={"docs": "pending"}, ts=6100)
        self.ev("session.mcp", "a", servers={"docs": "needs-auth"}, ts=6400)
        self.assertEqual(self.waits()["docs"]["since"], 5000)

    def test_a_disabled_server_no_longer_waits(self):
        self.start("a")
        self.info("a", ["docs"], ts=5000)
        self.ev("session.mcp", "a", servers={"docs": "disabled"}, ts=6000)
        self.assertEqual(self.waits(), {})
        self.info("a", ["docs"], ts=7000)
        self.ev("session.info", "a", ts=8000, mcp=["docs"], mcp_failed=[], mcp_auth=[], mcp_connected=[],
                mcp_disabled=["docs"])
        self.assertEqual(self.waits(), {})

    def test_an_address_learned_while_another_session_waits_there_keeps_the_older_since(self):
        self.start("a", "b")
        self.info("a", ["docs"], ts=4000)
        self.ev("session.mcp", "b", servers={"docs": "needs-auth"}, addr={"docs": A}, ts=5000)
        self.ev("session.mcp", "a", servers={"docs": "needs-auth"}, addr={"docs": A}, ts=5300)
        self.assertEqual(view(self.st), [{"server": "docs", "since": 4000, "sessions": ["a", "b"], "host": "a.example", "fp": A["fp"]}])

    def test_a_login_at_an_unknown_address_clears_a_known_one_by_the_next_poll(self):
        self.start("a", "b")
        self.ev("session.mcp", "b", servers={"docs": "needs-auth"}, addr={"docs": A}, ts=5000)
        self.ev("session.ended", "b")
        self.info("a", ["docs"], ts=5100)                # a has not been polled yet
        self.ev("mcp.logged_in", "a", server="docs", **OWNER)
        self.assertEqual(list(self.waits()), ["docs@a.example"])   # docs of a may be another server
        self.ev("session.info", "a", ts=5200, mcp=["docs"], mcp_failed=[], mcp_auth=[], mcp_connected=["docs"])
        self.ev("session.mcp", "a", servers={"docs": "connected"}, addr={"docs": A}, ts=5500)
        self.assertEqual(self.waits(), {})

    def test_a_login_does_not_restart_the_sessions_of_another_server_under_the_name(self):
        self.start("a", "b")
        self.ev("session.info", "a", mcp=["docs"], mcp_failed=[], mcp_auth=["docs"], ts=4000)
        self.ev("session.info", "b", mcp=["docs"], mcp_failed=[], mcp_auth=["docs"], ts=4000)
        self.ev("session.mcp", "a", servers={"docs": "needs-auth"}, addr={"docs": A}, ts=5000)
        self.ev("session.mcp", "b", servers={"docs": "needs-auth"}, addr={"docs": B}, ts=5000)
        self.actions = []
        self.ev("mcp.logged_in", "a", server="docs", **OWNER)
        self.assertEqual(sorted({x["session"] for x in self.actions if x.get("do") in ("release", "spawn")}), ["a"])
        self.assertEqual(self.st["sessions"]["b"]["info"]["mcp_auth"], ["docs"])
        self.assertEqual(list(self.waits()), ["docs@b.example"])

    def test_a_reconnect_that_went_through_clears_the_wait(self):
        self.start("a")
        self.info("a", ["docs"], ts=5000)
        self.ev("session.mcp_reconnected", "a", server="docs", ok=True, error=None, ts=6000)
        self.assertEqual(self.waits(), {})


def view(st):
    r = Reader()
    r.state = st
    return r.snapshot()["mcp_waits"]


class Reconnect(unittest.TestCase):
    """hub mcp reconnect <server> [--session <name>]: mcp_reconnect right now to the live sessions where the server is
    failed or waits for a login, past the mcpwatch.RETRY pause, but not more often than once per GAP per server."""

    def setUp(self):
        self.st, self.seq, self.actions = reducer.empty(), 0, []
        for n in ("a", "b", "c", "z"):
            self.ev("session.requested", n, cwd="/w", session_id="s-" + n, **OWNER)
            self.ev("session.started", n, session_id="s-" + n)
        self.ev("session.info", "a", mcp=["docs", "pay"], mcp_failed=["docs"], mcp_auth=[])
        self.ev("session.info", "b", mcp=["docs"], mcp_failed=[], mcp_auth=["docs"])
        self.ev("session.info", "c", mcp=["docs"], mcp_failed=[], mcp_auth=[])
        self.ev("session.info", "z", mcp=["docs"], mcp_failed=["docs"], mcp_auth=[])
        self.ev("session.ended", "z")                    # asleep: nobody to ask, it reconnects at start anyway
        self.actions = []

    def ev(self, type, entity=None, ts=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts or 1000 + self.seq, "type": type,
                                                      "entity": entity, "data": data})
        self.actions += acts
        return why

    def asked(self):
        out = sorted((a["session"], a["server"]) for a in self.actions if a.get("do") == "mcp_reconnect")
        self.actions = []
        return out

    def test_every_live_session_where_it_is_down_or_waits(self):
        self.assertIsNone(self.ev("mcp.reconnect_requested", None, server="docs", ts=2000, **OWNER))
        self.assertEqual(self.asked(), [("a", "docs"), ("b", "docs")])

    def test_past_the_automatic_pause_but_not_twice_in_a_row(self):
        self.assertEqual(self.asked(), [])               # the automatic attempt for a was already made at start
        self.assertIsNone(self.ev("mcp.reconnect_requested", None, server="docs", ts=2000, **OWNER))
        self.assertEqual(self.asked(), [("a", "docs"), ("b", "docs")])
        self.assertIn("docs", self.ev("mcp.reconnect_requested", None, server="docs", ts=2000 + mcpwatch.GAP - 1,
                                      **OWNER))
        self.assertEqual(self.asked(), [])
        self.assertIsNone(self.ev("mcp.reconnect_requested", None, server="docs", ts=2000 + mcpwatch.GAP, **OWNER))
        self.assertEqual(self.asked(), [("a", "docs"), ("b", "docs")])

    def test_the_automatic_retry_does_not_repeat_it_right_after(self):
        self.ev("mcp.reconnect_requested", None, server="docs", ts=5000, **OWNER)
        self.asked()
        self.ev("session.mcp", "a", servers={"docs": "failed", "pay": "connected"}, ts=5000 + 60)
        self.assertEqual(self.asked(), [])

    def test_the_pause_is_per_session(self):
        self.assertIsNone(self.ev("mcp.reconnect_requested", "a", server="docs", ts=2000, **OWNER))
        self.assertIsNone(self.ev("mcp.reconnect_requested", "b", server="docs", ts=2001, **OWNER))
        self.assertEqual(self.asked(), [("a", "docs"), ("b", "docs")])
        self.assertIn("docs", self.ev("mcp.reconnect_requested", "a", server="docs", ts=2002, **OWNER))
        self.assertIn("docs", self.ev("mcp.reconnect_requested", None, server="docs", ts=2003, **OWNER))   # both just asked
        self.assertIsNone(self.ev("mcp.reconnect_requested", None, server="docs", ts=2000 + mcpwatch.GAP, **OWNER))
        self.assertEqual(self.asked(), [("a", "docs")])  # b — not yet

    def test_a_session_that_cannot_reconnect_in_place_is_not_restarted_by_presses(self):
        self.ev("turn.started", "a", ts=1800)            # busy: the restart for the old way waits for the turn's end
        self.ev("host.done", "a", do="mcp_reconnect", server="docs", result="unsupported", ts=1900)
        self.assertIsNone(self.ev("mcp.reconnect_requested", None, server="docs", ts=2000, **OWNER))
        self.assertEqual(self.asked(), [("b", "docs")])
        self.assertIn("reload", self.ev("mcp.reconnect_requested", "a", server="docs", ts=3000, **OWNER))
        self.ev("session.mcp", "a", servers={"docs": "connected", "pay": "connected"}, ts=3100)   # the server came up
        self.assertIn("reload", self.ev("mcp.reconnect_requested", "a", server="pay", ts=3200, **OWNER))   # still tmux
        self.ev("turn.started", "b", ts=3300)
        self.ev("host.done", "b", do="mcp_reconnect", server="docs", result="unsupported", ts=3400)
        self.ev("session.mcp", "a", servers={"docs": "failed", "pay": "connected"}, ts=3500)
        self.asked()
        self.assertIn("reload", self.ev("mcp.reconnect_requested", None, server="docs", ts=4000, **OWNER))   # all such
        self.assertEqual(self.asked(), [])

    def test_one_session(self):
        self.assertIsNone(self.ev("mcp.reconnect_requested", "c", server="docs", ts=2000, **OWNER))
        self.assertEqual(self.asked(), [("c", "docs")])  # by request — even a connected one (it hangs, as the owner sees)
        self.assertIn("pay", self.ev("mcp.reconnect_requested", "b", server="pay", ts=3000, **OWNER))
        self.assertIn("z", self.ev("mcp.reconnect_requested", "z", server="docs", ts=4000, **OWNER))
        self.assertIn("nope", self.ev("mcp.reconnect_requested", "nope", server="docs", ts=5000, **OWNER))
        self.assertEqual(self.asked(), [])

    def test_nothing_to_reconnect_is_refused(self):
        self.assertIn("pay", self.ev("mcp.reconnect_requested", None, server="pay", ts=2000, **OWNER))
        for bad in ("", None, "a\nb", "x" * 201, ["docs"]):
            self.assertTrue(self.ev("mcp.reconnect_requested", None, server=bad, ts=2000, **OWNER), bad)
        self.assertEqual(self.asked(), [])

    def test_a_server_that_still_waits_for_a_login_did_not_drop(self):
        # a reconnect on request of a server waiting for a login fails: the hub was told how to log in, not "it dropped"
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.started", "hub")
        self.ev("mcp.reconnect_requested", "b", server="docs", ts=2000, **OWNER)
        told = len([m for m in self.st["messages"].values() if m["session"] == "hub"])
        self.ev("session.mcp_reconnected", "b", server="docs", ok=False, error="needs auth", ts=2001)
        self.assertEqual(len([m for m in self.st["messages"].values() if m["session"] == "hub"]), told)
        self.ev("session.mcp_reconnected", "a", server="docs", ok=False, error="handshake rejected", ts=2002)
        self.assertEqual(len([m for m in self.st["messages"].values() if m["session"] == "hub"]), told + 1)

    def test_a_session_may_ask_as_it_may_log_in(self):
        self.assertIsNone(self.ev("mcp.reconnect_requested", None, server="docs", ts=2000, channel="session",
                                  owner=False, sender="c"))
        self.assertEqual(self.asked(), [("a", "docs"), ("b", "docs")])
        self.assertEqual(self.st["mcp_reconnect_asked"]["docs"]["sessions"], ["a", "b"])


class Command(unittest.TestCase):
    """hub mcp reconnect puts mcp.reconnect_requested into the mailbox with the caller's channel; a session that does not
    exist is a call error, not a fact."""

    def hub(self, *argv, session=None):
        import os
        import subprocess
        import sys
        from core import journal
        state = tempfile.mkdtemp(prefix="st-")
        j = journal.Journal(os.path.join(state, "journal.jsonl"))
        j.append("session.requested", "maps", {"cwd": state, "session_id": "s", **OWNER})
        env = {**os.environ, "HUB_STATE_DIR": state, "HUB_CLI_WAIT": "0"}
        env.pop("HUB_SESSION", None)
        if session:
            env["HUB_SESSION"] = session
        hub = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")
        r = subprocess.run([sys.executable, hub, "mcp", "reconnect", *argv], env=env, capture_output=True, text=True,
                           timeout=60)
        spool = os.path.join(state, "spool")
        got = []
        for n in sorted(os.listdir(spool)) if os.path.isdir(spool) else []:
            with open(os.path.join(spool, n)) as f:
                got.append(json.load(f))
        return r, [(e["type"], e["entity"], e["data"]["server"], e["data"].get("channel"), e["data"].get("sender"))
                   for e in got if e["type"] == "mcp.reconnect_requested"]

    def test_all_sessions_or_one(self):
        r, got = self.hub("docs")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(got, [("mcp.reconnect_requested", None, "docs", "cli", None)])
        r, got = self.hub("docs", "--session", "maps", session="maps")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(got, [("mcp.reconnect_requested", "maps", "docs", "session", "maps")])

    def test_a_missing_session_is_a_call_error(self):
        r, got = self.hub("docs", "--session", "nope")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("nope", r.stderr)
        self.assertEqual(got, [])


class Snapshot(unittest.TestCase):
    def test_the_snapshot_lists_waiting_servers_oldest_first_without_removed_sessions(self):
        r = Reader()
        evs = [("session.requested", n, {"cwd": "/w", "session_id": "s-" + n, **OWNER}) for n in ("a", "b", "c")]
        evs += [("session.info", "c", {"mcp": ["docs"], "mcp_auth": ["docs"], "mcp_failed": []}),
                ("session.info", "a", {"mcp": ["pay"], "mcp_auth": ["pay"], "mcp_failed": []}),
                ("session.info", "b", {"mcp": ["docs", "pay"], "mcp_auth": ["docs", "pay"], "mcp_failed": []}),
                ("session.ended", "c", {}), ("session.remove_requested", "c", OWNER)]
        for i, (t, name, d) in enumerate(evs, 1):
            r.feed({"seq": i, "ts": 100 * i, "type": t, "entity": name, "data": d, "src": "cli"})
        self.assertEqual(r.state["sessions"]["c"]["state"], "removed")
        # docs: c (removed) named it first at 400, b still waits for it since then
        self.assertEqual(r.snapshot()["mcp_waits"], [{"server": "docs", "since": 400, "sessions": ["b"], "fp": ""},
                                                     {"server": "pay", "since": 500, "sessions": ["a", "b"], "fp": ""}])

    def test_no_waiting_servers_is_an_empty_list(self):
        self.assertEqual(Reader().snapshot()["mcp_waits"], [])


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
    """The address of a server comes from mcp_status (`config.url`): only its host and a fingerprint go to the journal —
    a URL may carry a key in its path or query."""

    def setUp(self):
        from hosts.streamjson import StreamProcess
        self.host = StreamHost(tempfile.mkdtemp(prefix="mw-"))
        self.popen = FakePopen()
        self.host.procs["a"] = StreamProcess(self.popen)

    def ask(self, rows):
        self.host.mcp_status("a")
        rid = self.popen.lines[-1]["request_id"]
        self.host.inbox.put(("a", {"type": "control_response", "response": {"request_id": rid, "subtype": "success",
                                                                              "response": {"mcpServers": rows}}}))
        return [f for f in self.host.poll() if f[0] == "session.mcp"]

    def test_the_address_is_a_host_and_a_fingerprint(self):
        self.host.inbox.put(("a", {"type": "system", "subtype": "init", "mcp_servers": [
            {"name": "docs", "status": "needs-auth"}, {"name": "local", "status": "connected"}]}))
        self.host.poll()
        rows = [{"name": "docs", "status": "needs-auth",
                 "config": {"type": "http", "url": "https://user:pw@Docs.Example:8443/mcp/k3y?token=s3cret#x"}},
                {"name": "local", "status": "connected", "config": {"type": "stdio", "command": "x", "args": ["--key=s"]}}]
        (fact,) = self.ask(rows)
        addr = fact[2]["addr"]
        self.assertEqual(list(addr), ["docs"])
        self.assertEqual(addr["docs"]["host"], "docs.example:8443")
        self.assertRegex(addr["docs"]["fp"], r"^[0-9a-f]{12}$")
        for secret in ("s3cret", "k3y", "pw", "user"):
            self.assertNotIn(secret, json.dumps(fact))
        self.assertEqual(self.ask(rows), [])            # once per process: the journal does not grow on every poll
        same = dict(rows[0], config={"type": "http", "url": "https://docs.example:8443/mcp/k3y?token=other"})
        self.assertEqual(self.ask([same, rows[1]]), [])  # the query is not a different server
        other = dict(rows[0], config={"type": "http", "url": "https://docs.example:8443/mcp/other"})
        (fact2,) = self.ask([other, rows[1]])
        self.assertNotEqual(fact2[2]["addr"]["docs"]["fp"], addr["docs"]["fp"])

    def test_no_address_no_new_fact(self):
        self.host.inbox.put(("a", {"type": "system", "subtype": "init", "mcp_servers": [
            {"name": "docs", "status": "connected"}]}))
        self.host.poll()
        self.assertEqual(self.ask([{"name": "docs", "status": "connected"}]), [])   # a Claude Code without config


if __name__ == "__main__":
    unittest.main()
