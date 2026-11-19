"""An MCP server waiting for a login (needs-auth) is not "connected" (10-05: a session came up before the owner logged in
to the service — session.info listed the server among the connected ones, with no tools, and nobody knew the session had
to be restarted after the login). The host marks such servers separately, `hub status` shows them."""
import tests  # noqa: F401
import json
import os
import subprocess
import sys
import tempfile
import unittest

from core import journal

HUB_CLI = os.path.join(os.path.dirname(__file__), "..", "bin", "hub")


class Host(unittest.TestCase):
    def test_needs_auth_is_listed_apart(self):
        from hosts.streamjson import StreamHost
        host = StreamHost(tempfile.mkdtemp(prefix="na-"))
        host.inbox.put(("a", {"type": "system", "subtype": "init", "model": "m", "cwd": "/w", "mcp_servers": [
            {"name": "figma", "status": "connected"}, {"name": "claude-design", "status": "needs-auth"},
            {"name": "jira", "status": "failed"}]}))
        (fact,) = [f for f in host.poll() if f[0] == "session.info"]
        info = fact[2]
        self.assertEqual((info["mcp_failed"], info["mcp_auth"]), (["jira"], ["claude-design"]))

    def test_builtin_commands_are_the_slash_commands_that_are_not_skills(self):
        # 10-07: the marker after a built-in command became its argument (/effort); which commands are built in is a Claude Code fact
        from hosts.streamjson import StreamHost
        host = StreamHost(tempfile.mkdtemp(prefix="na-"))
        host.inbox.put(("a", {"type": "system", "subtype": "init", "model": "m", "cwd": "/w", "mcp_servers": [],
                              "slash_commands": ["repo-work", "tool:fix", "compact", "effort", "init"],
                              "skills": ["repo-work"]}))
        (fact,) = [f for f in host.poll() if f[0] == "session.info"]
        # a plugin command (tool:fix) is a prompt, like a skill: a turn with a hook, the marker is needed
        self.assertEqual(fact[2]["builtins"], ["compact", "effort", "init"])


class Status(unittest.TestCase):
    def test_hub_status_says_which_servers_wait_for_a_login(self):
        state = tempfile.mkdtemp(prefix="na-st-")
        j = journal.Journal(os.path.join(state, "journal.jsonl"))
        j.append("session.requested", "bf", {"cwd": "/w", "session_id": "s-bf", "channel": "cli", "owner": True})
        j.append("session.started", "bf", {"session_id": "s-bf"})
        j.append("session.info", "bf", {"mcp": ["claude-design"], "mcp_failed": [], "mcp_auth": ["claude-design"]})
        out = subprocess.run([sys.executable, HUB_CLI, "status"], capture_output=True, text=True,
                             env={**os.environ, "HUB_STATE_DIR": state}).stdout
        self.assertIn("waiting for sign-in: claude-design", out)
        self.assertIn("hub mcp login bf", out)   # the next step is to log in without an interactive session (10-07)


class Restart(unittest.TestCase):
    def test_a_new_start_drops_the_old_login_mark_until_the_session_reports_again(self):
        # 10-05: after a restart with the key in the config, hub status still said the servers were waiting for a login — the MCP status
        # arrives only at the start of a turn, and before the turn session.info held what it had before the restart
        from core import reducer
        st, seq = reducer.empty(), 0

        def ev(type, entity=None, **data):
            nonlocal st, seq
            seq += 1
            st, _, why = reducer.reduce(st, {"seq": seq, "ts": 100 + seq, "type": type, "entity": entity, "data": data})
            return why
        ev("session.requested", "d", cwd="/w", session_id="s-d", channel="cli", owner=True)
        ev("session.started", "d", session_id="s-d")
        ev("session.info", "d", mcp=["icons8mcp"], mcp_failed=[], mcp_auth=["icons8mcp"], model="m")
        ev("session.started", "d", session_id="s-d", source="resume")
        info = st["sessions"]["d"]["info"]
        self.assertNotIn("mcp_auth", info)
        self.assertEqual(info["model"], "m")                         # the rest of the session's info is not lost
        ev("session.info", "d", mcp=["icons8mcp"], mcp_failed=[], mcp_auth=["icons8mcp"])
        self.assertEqual(st["sessions"]["d"]["info"]["mcp_auth"], ["icons8mcp"])   # really waiting — visible again

    def test_a_login_clears_the_mark_at_sleeping_sessions_too(self):
        # 10-07: after logging in to two servers through hub mcp login, hub status for sleeping sessions still said
        # "waiting for a login" — the mark was cleared only on a new start. A login is shared per server: it clears for all at once
        from core import reducer
        st, seq = reducer.empty(), 0

        def ev(type, entity=None, **data):
            nonlocal st, seq
            seq += 1
            st, _, why = reducer.reduce(st, {"seq": seq, "ts": 100 + seq, "type": type, "entity": entity, "data": data})
            return why
        for n in ("a", "b"):
            ev("session.requested", n, cwd="/w", session_id="s-" + n, channel="cli", owner=True)
            ev("session.started", n, session_id="s-" + n)
            ev("session.info", n, mcp=["pay-test", "pay-live"], mcp_failed=[], mcp_auth=["pay-test", "pay-live"])
        ev("session.ended", "b")
        self.assertEqual(st["sessions"]["b"]["state"], "asleep")
        ev("mcp.logged_in", "b", server="pay-test")
        self.assertEqual(st["sessions"]["a"]["info"]["mcp_auth"], ["pay-live"])
        self.assertEqual(st["sessions"]["b"]["info"]["mcp_auth"], ["pay-live"])
        self.assertEqual(st["sessions"]["a"]["state"], "starting")   # a live one restarts with the server, as before
        self.assertEqual(st["sessions"]["b"]["state"], "asleep")     # a sleeping one is not woken


if __name__ == "__main__":
    unittest.main()
