"""The owner closes a "MCP waits for a login" row (mcp.wait_dismissed {server, fp?}): the state keeps the mark, so a
phone and a desktop agree. The row stays closed while the same wait goes on (the session restarts and reports it again,
the address becomes known); it comes back when the wait starts anew — the server was signed in or left every session
and is waiting again — or at another address."""
import tests  # noqa: F401 — do not see the machine's settings
import unittest

from core import reducer
from core.views import Reader

OWNER = {"channel": "web", "owner": True}
A = {"fp": "aaaaaaaaaaaa", "host": "a.example"}
B = {"fp": "bbbbbbbbbbbb", "host": "b.example"}


def view(st):
    r = Reader()
    r.state = st
    return r.snapshot()["mcp_waits"]


class Dismiss(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0

    def ev(self, type, entity=None, ts=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts or 1000 + self.seq, "type": type,
                                                   "entity": entity, "data": data})
        return why

    def start(self, *names):
        for n in names:
            self.ev("session.requested", n, cwd="/w", session_id="s-" + n, **OWNER)
            self.ev("session.started", n, session_id="s-" + n)

    def wait(self, name, ts, server="docs", addr=None):
        self.ev("session.mcp", name, servers={server: "needs-auth"}, **({"addr": {server: addr}} if addr else {}), ts=ts)

    def rows(self):
        return [(w["server"], w.get("host")) for w in view(self.st)]

    def test_closed_row_is_gone_for_the_snapshot(self):
        self.start("a")
        self.wait("a", 5000, addr=A)
        self.ev("session.mcp", "a", servers={"docs": "needs-auth", "pay": "needs-auth"}, addr={"docs": A, "pay": A}, ts=5001)
        self.assertIsNone(self.ev("mcp.wait_dismissed", None, server="docs", fp=A["fp"], **OWNER))
        self.assertEqual(self.rows(), [("pay", "a.example")])

    def test_a_report_of_the_same_wait_does_not_bring_it_back(self):
        self.start("a")
        self.wait("a", 5000, addr=A)
        self.ev("mcp.wait_dismissed", None, server="docs", fp=A["fp"], **OWNER)
        self.wait("a", 5100, addr=A)                                       # the next poll
        self.ev("session.started", "a", session_id="s-a", source="resume", ts=9000)   # a restart
        self.ev("session.info", "a", ts=9100, mcp=["docs"], mcp_failed=[], mcp_auth=["docs"])
        self.wait("a", 9200, addr=A)
        self.assertEqual(self.rows(), [])
        self.start("b")                                                    # another session names it: still the same wait
        self.wait("b", 9300, addr=A)
        self.assertEqual(self.rows(), [])

    def test_a_wait_that_ended_and_started_again_is_shown(self):
        self.start("a")
        self.wait("a", 5000, addr=A)
        self.ev("mcp.wait_dismissed", None, server="docs", fp=A["fp"], **OWNER)
        self.ev("session.mcp", "a", servers={"docs": "connected"}, addr={"docs": A}, ts=6000)   # signed in
        self.assertEqual(self.rows(), [])
        self.wait("a", 7000, addr=A)
        self.assertEqual(self.rows(), [("docs", "a.example")])
        self.assertEqual(view(self.st)[0]["since"], 7000)

    def test_a_wait_the_sessions_left_and_named_again_is_shown(self):
        self.start("a")
        self.wait("a", 5000, addr=A)
        self.ev("mcp.wait_dismissed", None, server="docs", fp=A["fp"], **OWNER)
        self.ev("session.info", "a", ts=6000, mcp=[], mcp_failed=[], mcp_auth=[])
        self.wait("a", 7000, addr=A)
        self.assertEqual(self.rows(), [("docs", "a.example")])

    def test_another_address_under_the_same_name_is_shown(self):
        self.start("a", "b")
        self.wait("a", 5000, addr=A)
        self.ev("mcp.wait_dismissed", None, server="docs", fp=A["fp"], **OWNER)
        self.wait("b", 5100, addr=B)
        self.assertEqual(self.rows(), [("docs", "b.example")])

    def test_an_address_learned_later_stays_closed(self):
        self.start("a")
        self.ev("session.info", "a", ts=5000, mcp=["docs"], mcp_failed=[], mcp_auth=["docs"])   # init: no address yet
        self.assertEqual(self.rows(), [("docs", None)])
        self.ev("mcp.wait_dismissed", None, server="docs", fp="", **OWNER)
        self.assertEqual(self.rows(), [])
        self.wait("a", 5300, addr=A)                                       # the first poll brings the address
        self.assertEqual(self.rows(), [])

    def test_closing_a_row_whose_address_is_learned_closes_the_unknown_one_too(self):
        self.start("a", "b")
        self.ev("session.info", "b", ts=4000, mcp=["docs"], mcp_failed=[], mcp_auth=["docs"])
        self.wait("a", 5000, addr=A)
        self.assertEqual(len(view(self.st)), 1)                            # one row: the unknown joins the known
        self.ev("mcp.wait_dismissed", None, server="docs", fp=A["fp"], **OWNER)
        self.assertEqual(self.rows(), [])

    def test_without_fp_the_only_wait_of_the_server_is_closed(self):
        self.start("a")
        self.wait("a", 5000, addr=A)
        self.assertIsNone(self.ev("mcp.wait_dismissed", None, server="docs", **OWNER))
        self.assertEqual(self.rows(), [])
        self.start("b")
        self.wait("b", 5100, server="pay", addr=A)
        self.wait("b", 5200, addr=B)
        self.assertIn("docs", self.ev("mcp.wait_dismissed", None, server="docs", **OWNER))   # two addresses: which one

    def test_a_second_server_under_the_name_learned_later_is_shown(self):
        # review: closing the row of A closed the unknown-address wait that joined it, and a session whose first poll
        # then said B (another server under the same name) took the mark over — B was hidden for good
        self.start("a", "c")
        self.wait("a", 5000, addr=A)
        self.ev("session.info", "c", ts=5100, mcp=["docs"], mcp_failed=[], mcp_auth=["docs"])   # init: no address yet
        self.ev("mcp.wait_dismissed", None, server="docs", fp=A["fp"], **OWNER)
        self.assertEqual(self.rows(), [])
        self.wait("c", 5200, addr=B)
        self.assertEqual(self.rows(), [("docs", "b.example")])

    def test_a_press_on_a_row_whose_address_was_learned_meanwhile_closes_it(self):
        # review: the page still showed the row with fp "" when the first poll brought the address — the press was
        # refused "does not wait" and the row stayed with no word
        self.start("a")
        self.ev("session.info", "a", ts=5000, mcp=["docs"], mcp_failed=[], mcp_auth=["docs"])
        self.wait("a", 5100, addr=A)
        self.assertIsNone(self.ev("mcp.wait_dismissed", None, server="docs", fp="", **OWNER))
        self.assertEqual(self.rows(), [])

    def test_only_the_owner_closes_it(self):
        self.start("a")
        self.wait("a", 5000, addr=A)
        for who in ({"channel": "session", "owner": False, "sender": "a"}, {"channel": "plugin", "owner": False},
                    {"channel": "mod:x", "owner": True}):
            self.assertTrue(self.ev("mcp.wait_dismissed", "a" if who["channel"] == "session" else None,
                                    server="docs", fp=A["fp"], **who), who)
        self.assertEqual(self.rows(), [("docs", "a.example")])

    def test_what_is_not_waiting_or_is_malformed_is_refused(self):
        self.start("a")
        self.assertIn("does not wait", self.ev("mcp.wait_dismissed", None, server="docs", **OWNER))
        self.wait("a", 5000, addr=A)
        for bad in ({"server": ""}, {"server": 5}, {"server": "docs", "fp": 5}, {"server": "docs", "fp": "x" * 41},
                    {"server": "docs", "fp": B["fp"]}):
            self.assertTrue(self.ev("mcp.wait_dismissed", None, **bad, **OWNER), bad)
        self.assertEqual(self.rows(), [("docs", "a.example")])

    def test_a_state_from_before_has_no_mark_and_folds_on(self):
        self.start("a")
        self.wait("a", 5000, addr=A)
        self.assertEqual(self.rows(), [("docs", "a.example")])             # entries without "dismissed" show
        self.assertNotIn("dismissed", self.st["mcp_waits"]["docs"][A["fp"]])
        self.ev("mcp.wait_dismissed", None, server="docs", fp=A["fp"], **OWNER)
        self.ev("mcp.wait_dismissed", None, server="docs", fp=A["fp"], **OWNER)   # twice — nothing breaks
        self.assertEqual(self.rows(), [])


if __name__ == "__main__":
    unittest.main()
