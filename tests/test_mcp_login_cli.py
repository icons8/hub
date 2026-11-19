"""`hub mcp login <session> <server>` on the core's path (the same as the page's "Sign in"): the command asks the core for a
sign-in (mcp.login_requested), the core signs in through a live session where the server waits (Claude Code's control
requests), the link and the address after the sign-in go through files for the owner only; the command prints the
link (or opens the browser of the hub's machine), waits for "signed in" / "did not work" and takes the address from
another device with `--redirect`. A server the core does not know as waiting — the old way, in the command itself (pty).
The rules of who may: a session may start a sign-in as before (any), the owner's terminal and page too, a plugin or module
process never; a server from the confirm list — only the owner, with his confirmation."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from core import i18n, journal, mcplogin, mcpwatch, reducer
from tests.fakes import lang
from core.daemon import Hub

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB = os.path.join(REPO, "bin", "hub")
OWNER = {"channel": "cli", "owner": True}
SESSION = {"channel": "session", "owner": False}
LINK = "https://auth.example/authorize?response_type=code&state=s3cr3t&redirect_uri=http%3A%2F%2Flocalhost%3A5%2Fcallback"
BACK = "http://localhost:5/callback?code=0n3t1me&state=s3cr3t"


class Fold(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.actions = reducer.empty(), 0, []

    def ev(self, type, entity=None, src="cli", **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 1000 + self.seq * 10, "type": type,
                                                      "entity": entity, "data": data, "src": src})
        self.actions += acts
        return why

    def start(self, *names):
        for n in names:
            self.ev("session.requested", n, cwd="/w", session_id="s-" + n, **OWNER)
            self.ev("session.started", n, session_id="s-" + n)
            self.ev("session.mcp", n, src="host", servers={"docs": "needs-auth", "pay": "needs-auth", "other": "connected"})

    def test_a_session_may_start_a_sign_in_as_before_and_the_named_session_goes_first(self):
        self.start("a", "b")
        self.ev("turn.started", "b", marker=None)   # busy: by itself it would go second
        self.actions = []
        why = self.ev("mcp.login_requested", "a", server="docs", session="b", **SESSION, sender="a")
        self.assertIsNone(why)
        (act,) = [x for x in self.actions if x["do"] == "mcp_login"]
        self.assertEqual(act["session"], "b")
        (att,) = self.st["mcp_logins"].values()
        self.assertEqual((att["server"], att["session"], att["state"]), ("docs", "b", "asked"))

    def test_the_owners_terminal_starts_one_too(self):
        self.start("a")
        self.assertIsNone(self.ev("mcp.login_requested", None, server="docs", **OWNER))

    def test_a_plugin_or_module_process_never_does(self):
        self.start("a")
        for who in ({"channel": "plugin", "owner": False, "sender": "x"}, {"channel": "module", "owner": False}):
            why = self.ev("mcp.login_requested", None, server="docs", **who)
            self.assertIn("not by a plugin or module process", why)
        self.assertEqual(self.st.get("mcp_logins") or {}, {})

    def test_a_server_from_the_confirm_list_is_the_owners_to_confirm_never_a_sessions(self):
        self.start("a")
        self.ev("owner.settings", None, src="core", timezone="UTC", mcp_confirm=["pay"])
        for confirmed in ({}, {"confirmed": True}):   # a session cannot confirm: it is not the owner's word
            why = self.ev("mcp.login_requested", "a", server="pay", **SESSION, sender="a", **confirmed)
            self.assertIn("confirmation", why)
        self.assertEqual(self.st.get("mcp_logins") or {}, {})
        self.assertIsNone(self.ev("mcp.login_requested", None, server="pay", confirmed=True, **OWNER))

    def test_a_session_passes_on_the_address_the_person_gave_it(self):
        self.start("a")
        self.ev("mcp.login_requested", "a", server="docs", **SESSION, sender="a")
        (att,) = self.st["mcp_logins"].values()
        self.ev("session.mcp_login", "a", src="host", attempt=att["id"], server="docs", step="auth", ok=True)
        self.actions = []
        self.assertIsNone(self.ev("mcp.callback_given", "a", attempt=att["id"], **SESSION, sender="a"))
        self.assertEqual([x["do"] for x in self.actions], ["mcp_callback"])
        why = self.ev("mcp.callback_given", None, attempt=att["id"], channel="plugin", owner=False, sender="x")
        self.assertIn("not by a plugin or module process", why)

    def test_a_session_does_not_finish_the_owners_sign_in(self):
        self.start("a")
        self.ev("mcp.login_requested", None, server="docs", channel="web", owner=True)    # the page
        (att,) = self.st["mcp_logins"].values()
        self.assertEqual(att["by"], "owner")
        self.ev("session.mcp_login", "a", src="host", attempt=att["id"], server="docs", step="auth", ok=True)
        why = self.ev("mcp.callback_given", "a", attempt=att["id"], **SESSION, sender="a")
        self.assertIn("is sent by the owner", why)
        self.assertIsNone(self.ev("mcp.callback_given", None, attempt=att["id"], channel="web", owner=True))

    def test_the_hub_is_told_to_ask_the_owner_for_a_server_on_the_confirm_list(self):
        self.ev("session.requested", "hub-core", cwd="/h", session_id="s-h", kind="hub", **OWNER)
        self.ev("session.started", "hub-core")
        self.ev("owner.settings", None, src="core", timezone="UTC", mcp_confirm=["pay"])
        self.start("a")
        info = {"mcp": ["docs", "pay"], "mcp_failed": [], "mcp_auth": ["docs", "pay"]}
        self.ev("session.info", "a", src="host", **info)
        told = {("pay" if "MCP pay " in m["text"] else "docs"): m["text"] for m in self.st["messages"].values()
                if m["session"] == "hub-core"}
        self.assertIn("hub mcp login a docs", told["docs"])
        self.assertIn("mcp_login_confirm", told["pay"])
        self.assertIn('"Sign in"', told["pay"])
        self.assertNotIn("hub mcp login a pay", told["pay"])


class Hubd(threading.Thread):
    """hubd for the command: the real core over the state folder, a host that makes the link and 'signs in' when the test
    says so (the person came back), in a thread of its own — the command is a separate process, as in life."""

    def __init__(self, state):
        super().__init__(daemon=True)
        self.state, self.stop_, self.facts, self.lock = state, threading.Event(), [], threading.Lock()
        self.signed, self.calls, self.fail, self.todo = threading.Event(), [], None, []
        self.host = types.SimpleNamespace(mcp_login=self.mcp_login, mcp_login_poll=self.poll, mcp_callback=self.callback,
                                          mcp_reconnect=self.reconnect)
        self.hub = Hub(state, self.host)
        for n in ("a", "b"):
            self.hub.record("session.requested", n, {"cwd": state, "session_id": "s-" + n, **OWNER})
            self.hub.record("session.started", n, {"session_id": "s-" + n})
            self.hub.record("session.mcp", n, {"servers": {"docs": "needs-auth"}, "addr": {"docs": {"fp": "aaaaaaaaaaaa", "host": "a.example"}}},
                            src="host")

    def fact(self, name, **data):
        with self.lock:
            self.facts.append((name, data))

    def mcp_login(self, name, server, attempt, **_):
        self.calls.append(("login", name, server))
        if self.fail:
            return self.fact(name, attempt=attempt, server=server, step="auth", ok=False, error=self.fail) or "sent"
        mcplogin.put_link(self.state, attempt, LINK)
        self.fact(name, attempt=attempt, server=server, step="auth", ok=True)
        return "sent"

    def poll(self, name, server, attempt, **_):
        if self.signed.is_set():
            self.fact(name, attempt=attempt, server=server, step="status", ok=True, status="connected")
        return "sent"

    def callback(self, name, server, attempt, url, **_):
        self.calls.append(("callback", name, url))
        self.fact(name, attempt=attempt, server=server, step="callback", ok=BACK == url)
        return "sent"

    def reconnect(self, name, server, **_):
        self.calls.append(("reconnect", name, server))
        self.signed.set()
        return "sent"

    def run(self):
        while not self.stop_.is_set():
            while self.todo:   # facts the test wants in the journal, from this thread (the core is not for two)
                self.hub.record(*self.todo.pop(0))
            self.hub.ingest()
            with self.lock:
                facts, self.facts = self.facts, []
            for name, data in facts:
                self.hub.record("session.mcp_login", name, data, src="host")
            self.hub.mcp_login_tick()
            time.sleep(0.05)

    def journal_text(self):
        with open(os.path.join(self.state, "journal.jsonl"), encoding="utf-8") as f:
            return f.read()


class Command(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="mcli-")
        self.hubd = Hubd(self.state)
        self.hubd.start()
        self.addCleanup(self.hubd.join, 5)
        self.addCleanup(self.hubd.stop_.set)
        self.env = {k: v for k, v in os.environ.items() if k not in ("HUB_SESSION", "HUB_EXTENSION", "HUB_MODULE", "DISPLAY")}
        self.env.update(HUB_STATE_DIR=self.state, HUB_CLI_WAIT="5", HOME=self.state)

    def hub(self, *args, env=None, timeout=60):
        return subprocess.run([sys.executable, HUB, "mcp", "login", *args], env={**self.env, **(env or {})},
                              capture_output=True, text=True, timeout=timeout)

    def opener(self):
        """A 'browser' for the command: a script that writes the address it was asked to open into a file."""
        path, mark = os.path.join(self.state, "open.py"), os.path.join(self.state, "opened")
        with open(path, "w") as f:
            f.write(f"import sys\nopen({mark!r}, 'a').write(sys.argv[1] + '\\n')\n")
        # quoted: webbrowser splits a command line with shlex, which drops the backslashes of a Windows path
        return {"BROWSER": f"{shlex.quote(sys.executable)} {shlex.quote(path)} %s", "DISPLAY": ":0"}, mark

    def test_without_a_browser_the_link_is_printed_and_the_address_from_the_phone_finishes_it(self):
        r = self.hub("a", "docs", "--no-browser")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(LINK, r.stdout)
        self.assertIn("hub mcp login a docs --redirect", r.stdout)
        self.assertEqual(self.hubd.calls, [("login", "a", "docs")])      # through the live session, not claude mcp login
        r = self.hub("a", "docs", "--redirect", BACK)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("docs", r.stdout)
        self.assertIn(("callback", "a", BACK), self.hubd.calls)
        self.assertIn(("reconnect", "a", "docs"), self.hubd.calls)       # the address is checked by the session, then it comes up
        text = self.hubd.journal_text()
        for secret in ("s3cr3t", "0n3t1me"):
            self.assertNotIn(secret, text)
        self.assertEqual([a["state"] for a in journal.load_state(self.state)["mcp_logins"].values()], ["done"])

    def test_with_a_browser_it_opens_the_link_and_waits_until_it_is_signed_in(self):
        env, mark = self.opener()
        p = subprocess.Popen([sys.executable, HUB, "mcp", "login", "a", "docs"], env={**self.env, **env},
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(p.kill)
        end = time.time() + 30
        while not os.path.exists(mark) and time.time() < end:
            time.sleep(0.1)
        with open(mark) as f:
            self.assertEqual(f.read().strip(), LINK)
        self.assertIsNone(p.poll())                       # it waits for the person to sign in
        self.hubd.signed.set()                            # the browser came back to Claude Code's listener
        out, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 0, out + err)
        self.assertEqual([a["state"] for a in journal.load_state(self.state)["mcp_logins"].values()], ["done"])
        self.assertNotIn(LINK, out)

    def test_a_sign_in_that_did_not_work_says_why_and_fails(self):
        self.hubd.fail = "No MCP server named docs"
        r = self.hub("a", "docs", "--no-browser")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("No MCP server named docs", r.stdout + r.stderr)

    def test_a_refusal_of_the_core_is_the_commands_reason(self):
        r = self.hub("a", "docs", "--no-browser")                     # the first one is on
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = self.hub("a", "docs", "--no-browser")                     # a second within seconds: the core says so
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(i18n.t("core.mcpwatch.started", server="docs"), r.stdout + r.stderr)   # his terminal (English in tests)

    def test_a_wrong_address_is_told_and_the_sign_in_still_waits(self):
        self.hub("a", "docs", "--no-browser")
        wrong = "http://localhost:5/callback?code=bad"
        r = self.hub("a", "docs", "--redirect", wrong)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual([a["state"] for a in journal.load_state(self.state)["mcp_logins"].values()], ["open"])
        text = self.hubd.journal_text()
        for leak in (wrong, "code=bad", "localhost:5"):     # the address itself; "bad" alone is also inside random hex ids
            self.assertNotIn(leak, text)

    def test_a_session_does_not_touch_the_address_of_a_sign_in_the_owner_began(self):
        self.hub("a", "docs", "--no-browser")                                  # from his terminal: the owner's attempt
        r = self.hub("a", "docs", "--redirect", BACK, env={"HUB_SESSION": "b"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("started by the owner", r.stdout + r.stderr)   # a session's command: in English
        self.assertFalse(os.path.isdir(os.path.join(self.state, "mcp-login", "inbox"))
                         and os.listdir(os.path.join(self.state, "mcp-login", "inbox")))
        self.assertEqual([c[0] for c in self.hubd.calls], ["login"])

    def test_an_address_that_is_not_one_is_refused_before_it_is_written(self):
        self.hub("a", "docs", "--no-browser")
        r = self.hub("a", "docs", "--redirect", "not an address")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(os.path.isdir(os.path.join(self.state, "mcp-login", "inbox"))
                         and os.listdir(os.path.join(self.state, "mcp-login", "inbox")))

    def test_a_session_runs_it_too_with_its_own_word(self):
        r = self.hub("a", "docs", "--no-browser", env={"HUB_SESSION": "b"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.hubd.calls, [("login", "a", "docs")])      # the named session goes first
        self.assertIn('"sender":"b"', self.hubd.journal_text().replace(" ", ""))

    def test_a_server_from_the_confirm_list_is_signed_in_from_his_terminal_with_confirm_not_from_a_session(self):
        self.hubd.todo.append(("owner.settings", None, {"timezone": "UTC", "mcp_confirm": ["docs"]}, "core"))
        time.sleep(0.5)
        for env, flags in (({"HUB_SESSION": "b"}, ["--confirm"]), ({}, [])):   # a session's --confirm is nothing; neither is silence
            r = self.hub("a", "docs", "--no-browser", *flags, env=env)
            self.assertNotEqual(r.returncode, 0)
            # a session reads the English, his terminal his language (English in tests)
            self.assertIn(i18n.tr("en", "core.mcpwatch.needs_confirm", server="docs"), r.stdout + r.stderr)
            self.assertEqual(self.hubd.calls, [])
        r = self.hub("a", "docs", "--no-browser", "--confirm")              # he typed it himself, with the word
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.hubd.calls, [("login", "a", "docs")])

    def test_a_core_from_before_the_update_refuses_a_session_and_the_command_goes_the_old_way(self):
        # the command is read from disk at once, hubd only after `hub restart`: its refusal "only from the page" is not
        # the command's answer
        def old(st, ev, data, actions):
            raise ValueError(lang.legacy("mcp_refusals")[0])
        with mock.patch("core.mcpwatch.login_requested", old):
            r = self.hub("a", "docs", "--no-browser", env={"PATH": os.path.join(self.state, "none")})
        self.assertIn("sign-in to docs did not start", r.stdout + r.stderr)      # the old way's own message (no claude here)
        self.assertEqual(self.hubd.calls, [])

    def test_a_server_the_core_does_not_know_as_waiting_goes_the_old_way(self):
        # no wait in the state (a login from before the update, a server of the hub catalog): the command's own pty
        # login, as before — it does not go through the core and asks it for nothing
        r = self.hub("a", "pay", "--no-browser", env={"PATH": os.path.join(self.state, "none")})
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.hubd.calls, [])
        self.assertNotIn("mcp.login_requested", self.hubd.journal_text())


if __name__ == "__main__":
    unittest.main()
