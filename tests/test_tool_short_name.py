"""A tool by name without @catalog. On 2026-10-05 a session asked for "frontend-design" (in the catalog it is
"frontend-design@my-skills"), the hub granted the full id — the request was not found and hung for two weeks, while
`hub tools grant <session> frontend-design` replied "not in catalog". One rule (tools.resolve): a name without
@catalog is the single catalog or session-set id with that name; if there are several — do not guess, name the full
ids in the refusal."""
import tests  # noqa: F401 — do not see the machine's settings
import unittest

from core import reducer

HUB = {"channel": "session", "owner": False, "sender": "hub"}   # as in the journal: granted by the hub session
SELF = {"channel": "session", "owner": False, "sender": "a"}
PLUGIN = {"kind": "plugin", "risk": []}


class ShortName(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("catalog.loaded", None, tools={"frontend-design@my-skills": PLUGIN, "jira@set-a": PLUGIN,
                                               "jira@set-b": PLUGIN})
        self.ev("session.requested", "hub", cwd="/w", session_id="s-hub", kind="hub", channel="cli", owner=True)
        self.ev("session.requested", "a", cwd="/w", session_id="s-a", channel="cli", owner=True)
        self.ev("session.started", "hub")
        self.ev("session.started", "a")

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": self.seq, "type": type, "entity": entity,
                                                   "data": data})
        return why

    def reqs(self):
        return [(r["tool"], r["state"]) for r in self.st["sessions"]["a"]["requests"]]

    def test_grant_under_full_id_closes_short_request(self):
        """As in the journal: a request by the short name (before the rule — known: false), granted by the full id."""
        self.st["catalog"].pop("frontend-design@my-skills")   # not in the catalog on 2026-10-05 — an "unknown" request
        self.assertIsNone(self.ev("tools.requested", "a", tool="frontend-design", why="design", **SELF))
        self.ev("catalog.loaded", None, tools={"frontend-design@my-skills": PLUGIN})
        self.assertIsNone(self.ev("tools.granted", "a", tool="frontend-design@my-skills", **HUB))
        self.assertEqual(self.reqs(), [("frontend-design@my-skills", "granted")])   # CLI, feed and bot look up by id
        self.assertEqual(self.st["sessions"]["a"]["tools"], ["frontend-design@my-skills"])

    def test_grant_by_short_name_closes_stale_request(self):
        """The state of a live hub: granted by the full id, the request by the short name hangs — grant by the short
        name closes it."""
        self.st["sessions"]["a"]["tools"].append("frontend-design@my-skills")
        self.st["sessions"]["a"]["requests"].append({"tool": "frontend-design", "why": "design", "state": "requested",
                                                     "known": False, "since": 1, "asked": True})
        self.st["sessions"]["a"]["state"] = "asleep"
        self.assertIsNone(self.ev("tools.granted", "a", tool="frontend-design", **HUB))
        self.assertEqual(self.reqs(), [("frontend-design@my-skills", "granted")])
        self.assertEqual(self.st["sessions"]["a"]["requests"][0]["had"], self.seq)   # CLI: "already connected"

    def stale(self):
        self.st["sessions"]["a"]["tools"] += ["frontend-design@my-skills", "jira@set-a"]
        self.st["sessions"]["a"]["requests"] += [
            {"tool": "frontend-design", "why": "x", "state": "requested", "known": False, "since": 1, "asked": True},
            {"tool": "jira", "why": "x", "state": "requested", "known": False, "since": 1, "asked": True}]

    def test_stale_request_closes_by_itself_with_catalog(self):
        """A hanging request for what the session already has is cleared by the next catalog — without a hub command."""
        self.stale()
        self.ev("catalog.loaded", None, tools=dict(self.st["catalog"], **{"new@x": PLUGIN}))
        self.assertEqual(self.reqs(), [("frontend-design@my-skills", "granted"), ("jira", "requested")])   # jira: two of them

    def test_stale_request_closes_when_session_starts(self):
        """The catalog did not change (it is written to the journal only on change) — cleared by the session's own
        respawn."""
        self.stale()
        self.st["sessions"]["a"]["state"] = "starting"   # it was asleep — the core brings it up
        self.assertIsNone(self.ev("session.started", "a"))
        self.assertEqual(self.reqs(), [("frontend-design@my-skills", "granted"), ("jira", "requested")])

    def test_short_request_is_stored_under_full_id(self):
        self.assertIsNone(self.ev("tools.requested", "a", tool="frontend-design", why="design", **SELF))
        self.assertEqual(self.st["sessions"]["a"]["requests"][0]["tool"], "frontend-design@my-skills")
        self.assertTrue(self.st["sessions"]["a"]["requests"][0]["known"])
        self.assertIsNone(self.ev("tools.granted", "a", tool="frontend-design", **HUB))
        self.assertEqual(self.reqs(), [("frontend-design@my-skills", "granted")])
        self.assertEqual(self.ev("tools.requested", "a", tool="frontend-design", why="more", **SELF), "already has it")

    def test_ambiguous_short_name_is_refused_with_full_ids(self):
        for kind, who in (("tools.granted", HUB), ("tools.requested", SELF)):
            why = self.ev(kind, "a", tool="jira", why="x", **who)
            self.assertIn("jira@set-a, jira@set-b", why)
        self.assertEqual(self.st["sessions"]["a"]["tools"], [])
        self.assertEqual(self.st["sessions"]["a"]["requests"], [])

    def test_exact_id_in_session_set_is_not_ambiguous(self):
        self.st["sessions"]["a"]["tools"].append("jira")   # the exact id from the earlier catalog
        self.assertEqual(self.ev("tools.granted", "a", tool="jira", **HUB), "already has it")
        self.assertEqual(self.ev("tools.requested", "a", tool="jira", why="x", **SELF), "already has it")

    def test_exact_id_wins_over_short_name(self):
        self.ev("catalog.loaded", None, tools={"jira": {"kind": "mcp", "risk": []}, "jira@set-a": PLUGIN})
        self.assertIsNone(self.ev("tools.granted", "a", tool="jira", **HUB))
        self.assertEqual(self.st["sessions"]["a"]["tools"], ["jira"])


class GrantedButDown(unittest.TestCase):
    """A tool was granted, its MCP server did not come up in the session (mcp_failed / needs-auth), and the
    session asked again: "already has it" left it stuck — it did not know the tool is dead. The refusal now says that
    the server is down and what to do; for a server that came up it is still "already has it"."""

    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("catalog.loaded", None, tools={"mail": {"kind": "mcp", "risk": []},
                                               "docs@set-a": {"kind": "plugin", "risk": []}})
        self.ev("session.requested", "hub", cwd="/w", session_id="s-hub", kind="hub", channel="cli", owner=True)
        self.ev("session.requested", "a", cwd="/w", session_id="s-a", channel="cli", owner=True)
        self.ev("session.started", "hub")
        self.ev("session.started", "a")
        self.assertIsNone(self.ev("tools.requested", "a", tool="mail", why="x", **SELF))
        self.assertIsNone(self.ev("tools.granted", "a", tool="mail", **HUB))

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": self.seq, "type": type, "entity": entity,
                                                   "data": data})
        return why

    def info(self, **servers):
        self.ev("session.info", "a", model="m", mcp=["mail"], mcp_failed=servers.get("failed", []),
                mcp_auth=servers.get("auth", []), mcp_connected=servers.get("up", []), mcp_disabled=[])

    def test_failed_server_is_named_in_the_refusal(self):
        self.info(failed=["mail"])
        why = self.ev("tools.requested", "a", tool="mail", why="x", **SELF)
        self.assertIn("did not come up", why)
        self.assertIn("hub mcp reconnect mail --session a", why)
        self.assertIn("hub session reload a", why)
        self.assertIn("tell the hub", why)

    def test_the_grant_answer_is_for_the_hub(self):
        """The same case through `hub tools grant`: the hub reads it, so no "tell the hub" addressed to itself."""
        self.info(failed=["mail"])
        why = self.ev("tools.granted", "a", tool="mail", **HUB)
        self.assertIn("granted to a", why)
        self.assertIn("hub mcp reconnect mail --session a", why)
        self.assertNotIn("tell the hub", why)
        self.info(auth=["mail"])
        why = self.ev("tools.granted", "a", tool="mail", **HUB)
        self.assertIn("waiting for a login", why)
        self.assertNotIn("hub mcp login", why)

    def test_server_waiting_for_a_login_gives_the_session_no_login_command(self):
        """One login per server, and each is a browser tab for the owner: the core tells the hub once
        (mcpwatch.auth_notice); a session that asks again is told to go on without the tool, not to log in."""
        self.info(auth=["mail"])
        why = self.ev("tools.requested", "a", tool="mail", why="x", **SELF)
        self.assertIn("waiting for a login", why)
        self.assertIn("the hub has been told", why)
        self.assertNotIn("hub mcp login", why)

    def test_connected_server_is_still_already_has_it(self):
        self.info(up=["mail"])
        self.assertEqual(self.ev("tools.requested", "a", tool="mail", why="x", **SELF), "already has it")

    def test_failed_plugin_server_is_found_by_the_plugin_name(self):
        self.st["sessions"]["a"]["tools"].append("docs@set-a")
        self.info(failed=["plugin:docs:srv"])
        self.assertIn("did not come up", self.ev("tools.requested", "a", tool="docs@set-a", why="x", **SELF))

    def test_plugin_server_is_found_by_its_plugin_json_name(self):
        """A plugin's servers are named after plugin.json (catalog.plugin_servers), not after the catalog id."""
        self.ev("catalog.loaded", None, tools={"mail": {"kind": "mcp", "risk": []},
                                               "docs@set-a": {"kind": "plugin", "risk": [], "plugin_name": "doc-kit"}})
        self.st["sessions"]["a"]["tools"].append("docs@set-a")
        self.info(failed=["plugin:doc-kit:srv"])
        why = self.ev("tools.requested", "a", tool="docs@set-a", why="x", **SELF)
        self.assertIn("hub mcp reconnect plugin:doc-kit:srv --session a", why)

    def test_an_unrelated_server_with_the_same_name_is_not_the_tools(self):
        """The match goes by what the tool is: a plugin's servers are plugin:<its name>:…, a catalog server is its own
        name; another server that shares the id is not this tool's."""
        self.ev("catalog.loaded", None, tools={"mail": {"kind": "mcp", "risk": []},
                                               "docs@set-a": {"kind": "plugin", "risk": [], "plugin_name": "doc-kit"}})
        self.st["sessions"]["a"]["tools"].append("docs@set-a")
        self.info(failed=["docs"])                    # a server of its own named like the plugin's id
        self.assertEqual(self.ev("tools.requested", "a", tool="docs@set-a", why="x", **SELF), "already has it")
        self.info(failed=["plugin:mail:srv"])         # a plugin named like the catalog server
        self.assertEqual(self.ev("tools.requested", "a", tool="mail", why="x", **SELF), "already has it")

    def test_after_a_restart_the_old_process_failure_is_not_the_new_ones(self):
        """mcp_failed is the previous process's until the new one says its servers (session.info): a reload, which this
        refusal advises, must not be answered with the same refusal while the new process connects."""
        self.info(failed=["mail"])
        self.ev("session.started", "a", source="resume")
        self.assertEqual(self.ev("tools.granted", "a", tool="mail", **HUB), "already has it")
        self.info(failed=["mail"])                    # the new process says the server failed again
        self.assertIn("did not come up", self.ev("tools.granted", "a", tool="mail", **HUB))

    def test_a_catalog_server_is_found_by_its_as(self):
        """hub.json -> mcp.<id>.as: the session reports the catalog server under that name."""
        self.ev("catalog.loaded", None, tools={"mail": {"kind": "mcp", "risk": [], "as": "plugin:post:mail"}})
        self.info(failed=["plugin:post:mail"])
        why = self.ev("tools.requested", "a", tool="mail", why="x", **SELF)
        self.assertIn("hub mcp reconnect plugin:post:mail --session a", why)

    def test_the_server_came_up_again(self):
        self.info(failed=["mail"])
        self.info(up=["mail"])
        self.assertEqual(self.ev("tools.requested", "a", tool="mail", why="x", **SELF), "already has it")


if __name__ == "__main__":
    unittest.main()
