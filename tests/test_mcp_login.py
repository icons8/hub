"""A repo's MCP that waits for a login (OAuth), without an interactive session (owner decision 2026-10-07: authorize a
repository's MCP as soon as the repository is connected). The core tells the hub which servers wait for a login in which
session (once per server); `hub mcp login <session> <server>` runs `claude mcp login <server>` in the session's folder
(the login happens in a browser on the Mac), and sessions with that server are restarted into the same conversation
when they are free."""
import tests  # noqa: F401 — keeps the machine's settings out
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest

from core import reducer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB = os.path.join(REPO, "bin", "hub")
OWNER = {"channel": "cli", "owner": True}


class Core(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.acts = reducer.empty(), 0, []
        self.ev("session.requested", "hub-core", cwd="/h", session_id="s-h", kind="hub", **OWNER)
        self.ev("session.started", "hub-core")
        for n in ("maps", "maps-fix"):
            self.ev("session.requested", n, cwd="/w/backend", session_id=f"s-{n}", **OWNER)
            self.ev("session.started", n)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 1000 + self.seq, "type": type,
                                                      "entity": entity, "data": data})
        self.acts += acts
        return why

    def to_hub(self):
        return [m["text"] for m in self.st["messages"].values() if m["session"] == "hub-core"]

    def test_the_hub_is_told_once_per_server(self):
        # 2026-10-07, owner after a restart: the hub kept asking for a login per session, while Claude Code has one
        # login per server
        info = {"mcp": ["data", "pay"], "mcp_failed": [], "mcp_auth": ["pay"]}
        self.ev("session.info", "maps", **info)
        self.ev("session.info", "maps", **info)                     # the next start, the same again: do not repeat
        self.ev("session.info", "maps-fix", **info)                 # the same server in another session: already said
        self.ev("session.requested", "web", cwd="/w/web", session_id="s-web", **OWNER)
        self.ev("session.started", "web")
        self.ev("session.info", "web", **info)                     # and in another folder: the same server, the same login
        (text,) = self.to_hub()
        self.assertIn("hub mcp login maps pay", text)
        self.assertIn("once", text)
        self.assertIn("money", text)                               # money and prod need the owner's word

    def test_after_login_sessions_with_the_server_are_reloaded(self):
        self.ev("session.info", "maps", mcp=["pay"], mcp_failed=[], mcp_auth=["pay"])
        self.ev("session.info", "maps-fix", mcp=["data"], mcp_failed=[], mcp_auth=[])
        self.acts.clear()
        self.assertIsNone(self.ev("mcp.logged_in", "maps", server="pay", **OWNER))
        self.assertEqual([a["session"] for a in self.acts if a["do"] == "spawn"], ["maps"])   # a free session: at once

    def test_after_login_a_busy_session_reconnects_the_server_mid_turn(self):
        # after the login a busy session does not wait for the turn to end with "waiting for login": the server reconnects now
        self.ev("session.info", "maps", mcp=["pay"], mcp_failed=[], mcp_auth=["pay"])
        self.ev("turn.started", "maps", marker=None, prompt_head="x")
        self.acts.clear()
        self.assertIsNone(self.ev("mcp.logged_in", "maps", server="pay", **OWNER))
        self.assertEqual([a for a in self.acts if a.get("session") == "maps"],
                         [{"do": "mcp_reconnect", "session": "maps", "server": "pay"}])
        self.ev("session.mcp_reconnected", "maps", server="pay", ok=True)
        self.assertEqual(self.st["sessions"]["maps"]["info"]["mcp_auth"], [])   # hub status: no longer "waiting for login"


class Script(unittest.TestCase):
    def test_the_waiting_process_starts_as_a_script(self):
        # `hub mcp login` starts the module by its file (python core/mcplogin.py wait ...): the hub code is on the path
        # only after the imports, so a top-level import of a hub package would kill the waiting process at once
        r = subprocess.run([sys.executable, os.path.join(REPO, "core", "mcplogin.py")], capture_output=True, text=True,
                           timeout=60, env={**os.environ, "PYTHONPATH": ""}, cwd=tempfile.gettempdir())
        self.assertEqual((r.returncode, r.stderr), (0, ""))


class Command(unittest.TestCase):
    def test_runs_claude_mcp_login_in_the_sessions_folder_and_records_it(self):
        # and on Windows (2026-10-16: it failed there with "MCP login is in a terminal" because there was no pseudo-terminal)
        state, work, bin_dir = (tempfile.mkdtemp(prefix=p) for p in ("st-", "w-", "bin-"))
        log = os.path.join(bin_dir, "calls")
        fake = os.path.join(bin_dir, "claude")
        with open(fake, "w") as f:
            f.write(f'#!/bin/sh\necho "$PWD $*" >> "{log}"\nexit 0\n')
        os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
        with open(fake + ".cmd", "w") as f:
            f.write(f'@echo %CD% %*>> "{log}"\r\n')
        from core import journal
        j = journal.Journal(os.path.join(state, "journal.jsonl"))
        j.append("session.requested", "maps", {"cwd": work, "session_id": "s", **OWNER})
        # a computer with a browser (a display): login as before; without one, by a link (tests/test_mcp_login_link.py)
        env = {**os.environ, "HUB_STATE_DIR": state, "PATH": bin_dir + os.pathsep + os.environ["PATH"],
               "HUB_CLI_WAIT": "0", "DISPLAY": ":0"}
        env.pop("HUB_SESSION", None)
        r = subprocess.run([sys.executable, HUB, "mcp", "login", "maps", "pay"], env=env, capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(log) as f:
            where, *args = f.read().split()
        self.assertEqual(os.path.normcase(os.path.realpath(where)), os.path.normcase(os.path.realpath(work)))
        self.assertEqual(args, ["mcp", "login", "pay"])
        spooled = []
        for n in sorted(os.listdir(os.path.join(state, "spool"))):
            with open(os.path.join(state, "spool", n)) as f:
                spooled.append(json.load(f))
        self.assertEqual([(e["type"], e["entity"], e["data"]["server"]) for e in spooled],
                         [("mcp.logged_in", "maps", "pay")])


if __name__ == "__main__":
    unittest.main()
