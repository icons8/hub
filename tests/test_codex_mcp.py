"""Codex (experimental): MCP of a Codex session — the same facts the claude host gives the core
(hosts/streamjson.py mcp_status / mcp_reconnect / mcp_authenticate / mcp_oauth_callback_url): the servers' status as
session.mcp, a reconnect, an OAuth sign-in from the page and `hub mcp login`, an MCP server's question to the user.
App-server's answers are shaped as the 2026-11-05 probe of codex-cli 0.162.1 showed them (hosts/codex_mcp.py)."""
import tests  # noqa: F401 — do not see the machine's settings (~/.config/hub-core), see tests/__init__.py
import io
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import platforms

from core import journal, mcplogin, spool
from core.daemon import Hub
from hosts import codex, codex_mcp
from hosts.streamjson import mcp_addr

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
FAKE_B = os.path.join(HERE, "fakes", "codex_b.py")   # fake_codex with MCP as 0.162.1 does it
URL = "https://mcp.example.test/v1/mcp"


def row(name, runtime, auth="unsupported", origin=None):
    return {"name": name, "runtimeStatus": runtime, "authStatus": auth, "httpOrigin": origin, "tools": {},
            "resources": [], "resourceTemplates": []}


class Unit(unittest.TestCase):
    """The seams on a CodexProcess whose stdin is a buffer: what the host writes to app-server and what facts it gives."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hubcxb-")
        host = codex.CodexHost.__new__(codex.CodexHost)
        host.state_dir = self.state
        host.inbox, host.lock, host.clock = queue.Queue(), threading.Lock(), time.time
        popen = types.SimpleNamespace(stdin=io.StringIO(), poll=lambda: None, pid=1)
        proc = codex.CodexProcess(popen, "sid", False)
        proc.thread = "t1"
        proc.start_params = {"config": {"mcp_servers": {"docs": {"url": URL}, "local": {"command": "srv"}}}}
        host.procs = {"a": proc}
        self.host, self.proc, self.popen = host, proc, popen

    def tearDown(self):
        platforms.current.rmtree(self.state)

    def sent(self, method=None):
        lines = [json.loads(x) for x in self.popen.stdin.getvalue().splitlines()]
        return [m for m in lines if method is None or m.get("method") == method]

    def answer(self, method, result=None, error=None, last=True):
        """App-server's answer to the host's last (or first) request of `method`, through the host as it would come."""
        reqs = self.sent(method)
        req = reqs[-1] if last else reqs[0]
        msg = {"id": req["id"], **({"error": error} if error else {"result": result})}
        return self.host._line("a", msg)

    def inbox(self):
        """Facts of the answers the seam put into the host's inbox (the callback, a reconnect with nothing to do)."""
        out = []
        while not self.host.inbox.empty():
            name, msg, _ = self.host.inbox.get()
            out += self.host._line(name, msg)
        return out

    def note(self, method, **params):
        return self.host._line("a", {"method": method, "params": params})

    # --- status ---------------------------------------------------------------------------------------------------
    def test_status_is_session_mcp_with_claudes_words_and_the_address_fingerprint(self):
        self.assertEqual(self.host.mcp_status("a"), "sent")
        self.assertEqual(self.sent("mcpServerStatus/list")[-1]["params"], {"threadId": "t1"})
        facts = self.answer("mcpServerStatus/list", {"data": [
            row("docs", "authenticationRequired", "notLoggedIn", "https://mcp.example.test"),
            row("local", "connected"), row("gone", "failed"), row("off", "disabled"), row("slow", "starting"),
            row("odd", None)], "nextCursor": None})
        self.assertEqual(facts, [("session.mcp", "a", {
            "servers": {"docs": "needs-auth", "local": "connected", "gone": "failed", "off": "disabled",
                        "slow": "pending", "odd": "pending"},
            # the fingerprint of the address the host configured — the same as Claude's for the same server
            "addr": {"docs": mcp_addr({"url": URL})}})])

    def test_an_unchanged_status_is_not_a_fact_unless_something_failed(self):
        rows = {"data": [row("local", "connected")]}
        self.host.mcp_status("a")
        self.assertEqual(len(self.answer("mcpServerStatus/list", rows)), 1)
        self.host.mcp_status("a")
        self.assertEqual(self.answer("mcpServerStatus/list", rows), [])
        failed = {"data": [row("local", "failed")]}
        for _ in range(2):   # a failed one — every time: the core decides when to reconnect again
            self.host.mcp_status("a")
            self.assertEqual(len(self.answer("mcpServerStatus/list", failed)), 1)

    def test_more_servers_than_a_page_are_asked_for_by_the_cursor(self):
        self.host.mcp_status("a")
        self.assertEqual(self.answer("mcpServerStatus/list", {"data": [row("local", "connected")], "nextCursor": "c2"}),
                         [])
        self.assertEqual(self.sent("mcpServerStatus/list")[-1]["params"], {"threadId": "t1", "cursor": "c2"})
        (fact,) = self.answer("mcpServerStatus/list", {"data": [row("docs", "failed")], "nextCursor": None})
        self.assertEqual(fact[2]["servers"], {"local": "connected", "docs": "failed"})

    def test_the_servers_start_ends_with_a_status_on_its_own(self):
        self.note("mcpServer/startupStatus/updated", threadId="t1", name="local", status="starting")
        self.note("mcpServer/startupStatus/updated", threadId="t1", name="docs", status="starting")
        self.note("mcpServer/startupStatus/updated", threadId="t1", name="local", status="ready")
        self.assertEqual(self.sent("mcpServerStatus/list"), [])   # docs is still starting
        self.note("mcpServer/startupStatus/updated", threadId="t1", name="docs", status="failed",
                  error="The docs MCP server is not logged in.")
        self.assertEqual(len(self.sent("mcpServerStatus/list")), 1)
        (fact,) = self.answer("mcpServerStatus/list", {"data": [row("local", "connected"),
                                                                row("docs", "authenticationRequired")]})
        self.assertEqual(fact[2]["servers"], {"local": "connected", "docs": "needs-auth"})

    def test_servers_that_started_before_the_thread_was_known_are_asked_once_it_is(self):
        self.proc.thread = None
        self.note("mcpServer/startupStatus/updated", name="local", status="ready")
        self.assertEqual(self.sent("mcpServerStatus/list"), [])
        self.proc.thread = "t1"
        codex_mcp.start(self.proc)
        self.assertEqual(len(self.sent("mcpServerStatus/list")), 1)

    def test_info_names_the_threads_servers(self):
        self.assertEqual(codex_mcp.info(self.proc), {"mcp": ["docs", "local"], "mcp_login_pty": False})

    # --- reconnect ------------------------------------------------------------------------------------------------
    def test_a_failed_server_is_reloaded_once_and_each_waiting_reconnect_is_answered_by_its_start(self):
        self.host.mcp_status("a")
        self.answer("mcpServerStatus/list", {"data": [row("docs", "failed"), row("local", "failed")]})
        self.host.mcp_reconnect("a", server="docs")
        self.host.mcp_reconnect("a", server="local")
        self.assertEqual(len(self.sent("config/mcpServer/reload")), 1)   # one reload restarts them all
        self.assertEqual(self.answer("config/mcpServer/reload", {}), [])
        self.note("mcpServer/startupStatus/updated", name="docs", status="starting")
        self.note("mcpServer/startupStatus/updated", name="local", status="starting")
        got = self.note("mcpServer/startupStatus/updated", name="docs", status="ready")
        self.assertEqual(got, [("session.mcp_reconnected", "a", {"server": "docs", "ok": True, "error": None})])
        got = self.note("mcpServer/startupStatus/updated", name="local", status="failed", error="spawn: no such file")
        self.assertEqual(got[0], ("session.mcp_reconnected", "a", {"server": "local", "ok": False,
                                                                   "error": "spawn: no such file"}))

    def test_a_connected_server_is_not_reloaded(self):
        self.host.mcp_status("a")
        self.answer("mcpServerStatus/list", {"data": [row("local", "connected")]})
        self.host.mcp_reconnect("a", server="local")
        self.assertEqual(self.sent("config/mcpServer/reload"), [])
        self.assertEqual(self.inbox(), [("session.mcp_reconnected", "a", {"server": "local", "ok": True,
                                                                          "error": None})])

    def test_a_server_starting_after_a_sign_in_answers_without_a_reload(self):
        self.note("mcpServer/startupStatus/updated", name="docs", status="starting")
        self.host.mcp_reconnect("a", server="docs")
        self.assertEqual(self.sent("config/mcpServer/reload"), [])
        got = self.note("mcpServer/startupStatus/updated", name="docs", status="ready")
        self.assertEqual(got[0], ("session.mcp_reconnected", "a", {"server": "docs", "ok": True, "error": None}))

    def test_a_refused_reload_answers_every_waiting_reconnect_and_an_unknown_server_at_once(self):
        self.host.mcp_reconnect("a", server="docs")
        got = self.answer("config/mcpServer/reload", error={"code": -32603, "message": "config broken"})
        self.assertEqual(got, [("session.mcp_reconnected", "a", {"server": "docs", "ok": False,
                                                                 "error": "config broken"})])
        self.host.mcp_reconnect("a", server="nope")
        (fact,) = self.inbox()
        self.assertEqual((fact[2]["server"], fact[2]["ok"]), ("nope", False))

    def test_a_status_answer_from_before_the_reload_does_not_close_the_reconnect(self):
        from unittest import mock
        now = [1000.0]
        with mock.patch.object(codex_mcp, "clock", lambda: now[0]):
            self.host.mcp_status("a")   # on its way when the reconnect comes
            self.host.mcp_reconnect("a", server="local")
            self.assertEqual(self.answer("mcpServerStatus/list", {"data": [row("local", "failed")]}, last=False)[0][0],
                             "session.mcp")   # not session.mcp_reconnected: the reload has not restarted it yet
            now[0] += codex_mcp.STALE   # no start told at all: the next status answers the reconnect
            self.host.mcp_status("a")
            got = self.answer("mcpServerStatus/list", {"data": [row("local", "failed")]})
        self.assertIn(("session.mcp_reconnected", "a", {"server": "local", "ok": False, "error": "failed"}), got)

    def test_a_reload_that_started_nothing_does_not_hold_the_next_one_back(self):
        from unittest import mock
        now = [1000.0]
        with mock.patch.object(codex_mcp, "clock", lambda: now[0]):
            self.host.mcp_reconnect("a", server="local")
            self.answer("config/mcpServer/reload", {})
            self.host.mcp_reconnect("a", server="local")
            self.assertEqual(len(self.sent("config/mcpServer/reload")), 1)
            now[0] += codex_mcp.STALE
            self.host.mcp_reconnect("a", server="local")
        self.assertEqual(len(self.sent("config/mcpServer/reload")), 2)

    def test_mid_turn_the_reload_waits_for_the_turns_end_when_another_server_is_connected(self):
        self.host.mcp_status("a")
        self.answer("mcpServerStatus/list", {"data": [row("docs", "connected"), row("local", "failed")]})
        self.proc.turn = "turn-1"
        self.host.mcp_reconnect("a", server="local")
        self.assertEqual(self.sent("config/mcpServer/reload"), [])   # it would cut docs' calls in this turn
        self.proc.turn = None
        self.assertEqual(codex_mcp.turn_done(self.proc, "a", {}), [])
        self.assertEqual(len(self.sent("config/mcpServer/reload")), 1)

    def test_a_deferred_reload_goes_after_a_compaction_too_and_a_long_turn_does_not_close_it(self):
        from unittest import mock
        now = [1000.0]
        with mock.patch.object(codex_mcp, "clock", lambda: now[0]):
            self.host.mcp_status("a")
            self.answer("mcpServerStatus/list", {"data": [row("docs", "connected"), row("local", "failed")]})
            self.proc.starting = True   # turn/start sent, its id not known yet: a turn all the same
            self.host.mcp_reconnect("a", server="local")
            self.assertEqual(self.sent("config/mcpServer/reload"), [])
            self.proc.starting, self.proc.turn = False, "turn-1"
            now[0] += codex_mcp.STALE * 2   # a long turn: the periodic status does not close the deferred reconnect
            self.host.mcp_status("a")
            got = self.answer("mcpServerStatus/list", {"data": [row("docs", "connected"), row("local", "failed")]})
            self.assertNotIn("session.mcp_reconnected", [f[0] for f in got])
            self.proc.turn = None
            codex_mcp.compacted(self.proc, "a")
        self.assertEqual(len(self.sent("config/mcpServer/reload")), 1)

    def test_a_failed_sign_in_lets_the_next_reconnect_reload(self):
        self.note("mcpServer/oauthLogin/completed", name="docs", loginId="L9", success=True)
        self.host.mcp_reconnect("a", server="docs")
        self.assertEqual(self.sent("config/mcpServer/reload"), [])   # Codex restarts it after the sign-in itself
        self.note("mcpServer/startupStatus/updated", name="docs", status="failed", error="401")
        self.host.mcp_reconnect("a", server="docs")
        self.assertEqual(len(self.sent("config/mcpServer/reload")), 1)
        self.note("mcpServer/oauthLogin/completed", name="local", loginId="L8", success=False, error="denied")
        self.assertNotIn("local", codex_mcp._of(self.proc).signed_at)

    def test_mid_turn_the_only_server_is_reloaded_at_once(self):
        self.host.mcp_status("a")
        self.answer("mcpServerStatus/list", {"data": [row("local", "failed")]})
        self.proc.turn = "turn-1"
        self.host.mcp_reconnect("a", server="local")
        self.assertEqual(len(self.sent("config/mcpServer/reload")), 1)

    def test_after_the_address_went_through_the_reconnect_waits_for_codexs_own_restart(self):
        redirect, _ = self.listener(200)
        self.sign_in(redirect)
        self.host.mcp_callback("a", server="docs", attempt="l1", url=redirect + "?code=C1&state=S1")
        self.wait_inbox()
        self.host.mcp_reconnect("a", server="docs")   # the core's step after an accepted address (core/mcpwatch)
        self.assertEqual(self.sent("config/mcpServer/reload"), [])
        self.note("mcpServer/oauthLogin/completed", name="docs", loginId="L1", success=True)
        self.note("mcpServer/startupStatus/updated", name="docs", status="starting")
        got = self.note("mcpServer/startupStatus/updated", name="docs", status="ready")
        self.assertEqual(got[0], ("session.mcp_reconnected", "a", {"server": "docs", "ok": True, "error": None}))

    def test_an_answer_for_a_replaced_process_is_dropped(self):
        self.proc.thread = None
        self.host.mcp_login("a", server="docs", attempt="l1")
        old = self.proc
        new = codex.CodexProcess(types.SimpleNamespace(stdin=io.StringIO(), poll=lambda: None, pid=2), "sid", False)
        self.host.procs["a"] = new
        self.assertEqual(self.inbox(), [])
        self.assertEqual((old.next_id, new.next_id), (1, 1))   # the step touched no request ids

    def test_a_page_row_without_a_name_is_skipped(self):
        self.host.mcp_status("a")
        self.answer("mcpServerStatus/list", {"data": [{"runtimeStatus": "connected"}], "nextCursor": "c2"})
        (fact,) = self.answer("mcpServerStatus/list", {"data": [row("local", "connected")]})
        self.assertEqual(fact[2]["servers"], {"local": "connected"})

    def test_a_dead_process_raises_as_on_claude(self):
        self.proc.popen.poll = lambda: 1
        for call in (lambda: self.host.mcp_status("a"), lambda: self.host.mcp_reconnect("a", server="docs"),
                     lambda: self.host.mcp_login("a", server="docs", attempt="l1")):
            with self.assertRaises(RuntimeError):
                call()

    # --- sign-in --------------------------------------------------------------------------------------------------
    def test_a_sign_in_asks_the_status_first_then_puts_the_link_in_the_owners_file(self):
        self.host.mcp_login("a", server="docs", attempt="l7")
        self.assertEqual(self.sent("mcpServer/oauth/login"), [])
        self.assertEqual(self.answer("mcpServerStatus/list", {"data": [row("docs", "authenticationRequired")]}), [])
        self.assertEqual(self.sent("mcpServer/oauth/login")[-1]["params"], {"name": "docs", "threadId": "t1"})
        link = ("https://mcp.example.test/authorize?client_id=c&state=s1&redirect_uri="
                "http%3A%2F%2F127.0.0.1%3A40229%2Fcallback%2Fabc")
        got = self.answer("mcpServer/oauth/login", {"authorizationUrl": link, "loginId": "L1"})
        self.assertEqual(got, [("session.mcp_login", "a", {"attempt": "l7", "server": "docs", "step": "auth",
                                                           "ok": True})])
        self.assertEqual(mcplogin.link_of(self.state, "l7"), link)   # the link is not in the fact (nor the journal)

    def test_a_connected_or_starting_server_is_not_signed_in_again(self):
        for runtime, word in (("connected", "connected"), ("starting", "pending"), ("failed", "failed")):
            self.host.mcp_login("a", server="docs", attempt="l1")
            got = self.answer("mcpServerStatus/list", {"data": [row("docs", runtime)]})
            self.assertEqual(got[-1], ("session.mcp_login", "a", {"attempt": "l1", "server": "docs", "step": "auth",
                                                                  "ok": False, "status": word}))
        self.assertEqual(self.sent("mcpServer/oauth/login"), [])

    def test_a_refused_sign_in_and_a_failed_completion_fail_the_attempt_without_the_code(self):
        self.host.mcp_login("a", server="local", attempt="l1")
        self.answer("mcpServerStatus/list", {"data": [row("local", "authenticationRequired")]})
        got = self.answer("mcpServer/oauth/login",
                          error={"code": -32600, "message": "OAuth login is only supported for streamable HTTP servers."})
        self.assertEqual((got[0][2]["ok"], got[0][2]["step"]), (False, "auth"))
        self.host.mcp_login("a", server="docs", attempt="l2")
        self.answer("mcpServerStatus/list", {"data": [row("docs", "authenticationRequired")]})
        self.answer("mcpServer/oauth/login", {"authorizationUrl": "https://x.example.test/a", "loginId": "L2"})
        got = self.note("mcpServer/oauthLogin/completed", name="docs", loginId="L2", success=False,
                        error="token exchange failed: code=SECRET1&state=S")
        self.assertEqual(got[0][2]["attempt"], "l2")
        self.assertNotIn("SECRET1", json.dumps(got))
        self.assertEqual(self.note("mcpServer/oauthLogin/completed", name="docs", loginId="L2", success=True), [])

    def test_polling_tells_only_connected(self):
        self.host.mcp_login_poll("a", server="docs", attempt="l1")
        self.assertEqual(self.answer("mcpServerStatus/list", {"data": [row("docs", "authenticationRequired")]}), [])
        self.host.mcp_login_poll("a", server="docs", attempt="l1")
        self.assertEqual(self.answer("mcpServerStatus/list", {"data": [row("docs", "connected")]}),
                         [("session.mcp_login", "a", {"attempt": "l1", "server": "docs", "step": "status",
                                                      "status": "connected"})])

    def test_a_sign_in_while_the_thread_is_not_ready_is_pending_for_the_core_to_retry(self):
        self.proc.thread = None
        self.host.mcp_login("a", server="docs", attempt="l1")
        (fact,) = self.inbox()
        self.assertEqual(fact[2]["status"], "pending")

    # --- the address after a sign-in from another device ------------------------------------------------------------
    def test_the_callback_opens_only_this_attempts_listener_with_only_the_oauth_answer(self):
        redirect = "http://127.0.0.1:40229/callback/abc"
        target = codex_mcp.target
        self.assertEqual(target(redirect, "http://127.0.0.1:40229/callback/abc?code=C&state=S&x=1&redirect=evil"),
                         "http://127.0.0.1:40229/callback/abc?code=C&state=S")
        self.assertEqual(target(redirect, "http://localhost:40229/callback/abc?code=C"), None)    # another host
        self.assertEqual(target(redirect, "http://127.0.0.1:40230/callback/abc?code=C"), None)    # another port
        self.assertEqual(target(redirect, "http://127.0.0.1:40229/callback/zzz?code=C"), None)    # another attempt
        self.assertEqual(target(redirect, "http://127.0.0.1:40229/callback/abc"), None)           # no answer
        self.assertEqual(target(redirect, "http://127.0.0.1:99999/callback/abc?code=C"), None)    # a bad port
        self.assertEqual(target("https://example.test/callback/abc", "https://example.test/callback/abc?code=C"), None)

    def listener(self, status):
        got = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                got.append(self.path)
                self.send_response(status)
                self.end_headers()
                self.wfile.write(b"done")

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}/callback/abc", got

    def sign_in(self, redirect, attempt="l1"):
        self.host.mcp_login("a", server="docs", attempt=attempt)
        self.answer("mcpServerStatus/list", {"data": [row("docs", "authenticationRequired")]})
        self.answer("mcpServer/oauth/login", {"loginId": "L1", "authorizationUrl":
                    "https://mcp.example.test/authorize?redirect_uri=" + redirect.replace(":", "%3A").replace("/", "%2F")})

    def wait_inbox(self):
        end = time.time() + 10
        while self.host.inbox.empty() and time.time() < end:
            time.sleep(0.02)
        return self.inbox()

    def test_the_callback_reaches_codexs_listener_and_the_step_comes_through_the_inbox(self):
        redirect, got = self.listener(200)
        self.sign_in(redirect)
        self.assertEqual(self.host.mcp_callback("a", server="docs", attempt="l1",
                                                url=redirect + "?code=C1&state=S1&extra=no"), "sent")
        self.assertEqual(self.wait_inbox(), [("session.mcp_login", "a", {"attempt": "l1", "server": "docs",
                                                                          "step": "callback", "ok": True})])
        self.assertEqual(got, ["/callback/abc?code=C1&state=S1"])

    def test_a_refused_or_foreign_callback_is_a_failed_step_and_the_hub_fetches_nothing_else(self):
        redirect, got = self.listener(400)
        self.sign_in(redirect)
        self.host.mcp_callback("a", server="docs", attempt="l1", url=redirect + "?code=C1&state=S1")
        (fact,) = self.wait_inbox()
        self.assertEqual((fact[2]["step"], fact[2]["ok"]), ("callback", False))
        self.assertNotIn("C1", json.dumps(fact))
        self.host.mcp_callback("a", server="docs", attempt="l1", url="http://203.0.113.5/callback/abc?code=C2")
        (fact,) = self.wait_inbox()
        self.assertFalse(fact[2]["ok"])
        self.host.mcp_callback("a", server="docs", attempt="l9", url=redirect + "?code=C3")   # no such sign-in
        self.assertFalse(self.wait_inbox()[0][2]["ok"])
        self.assertEqual(got, ["/callback/abc?code=C1&state=S1"])

    # --- elicitation ------------------------------------------------------------------------------------------------
    def elicit(self, **params):
        return self.host._line("a", {"id": 5, "method": "mcpServer/elicitation/request",
                                     "params": {"threadId": "t1", "turnId": None, "serverName": "docs", **params}})

    def test_a_form_becomes_a_card_and_the_answers_go_back_typed(self):
        schema = {"type": "object", "required": ["color"], "properties": {
            "color": {"type": "string", "title": "Color", "enum": ["red", "blue"], "enumNames": ["Red", "Blue"]},
            "count": {"type": "integer", "title": "Count"}, "sure": {"type": "boolean", "title": "Sure"},
            "note": {"type": "string", "title": "Note", "description": "anything"}}}
        (fact,) = self.elicit(mode="form", message="Pick", requestedSchema=schema)
        self.assertEqual(fact[0], "permission.requested")
        qs = fact[2]["input"]["questions"]
        self.assertEqual([[o["label"] for o in q["options"]] for q in qs][:1], [["Red", "Blue"]])
        self.assertIn("MCP docs: Pick", qs[0]["question"])
        self.assertEqual(len(qs[3]["options"]), 0)   # text: only the card's own answer field
        yes = qs[2]["options"][0]["label"]
        answers = {qs[0]["question"]: "Blue", qs[1]["question"]: "3", qs[2]["question"]: yes,
                   qs[3]["question"]: "hello"}
        self.host.respond("a", fact[2]["request_id"], {"behavior": "allow", "updatedInput": {"answers": answers}})
        self.assertEqual(json.loads(self.popen.stdin.getvalue().splitlines()[-1]),
                         {"id": 5, "result": {"action": "accept", "content": {"color": "blue", "count": 3,
                                                                              "sure": True, "note": "hello"}}})

    def test_a_refusal_a_missing_required_answer_or_a_wrong_number_declines(self):
        schema = {"type": "object", "required": ["n"], "properties": {"n": {"type": "number", "title": "N"}}}
        schema["properties"]["n"].update(type="integer", minimum=0, maximum=10)
        q = "MCP docs: Pick\n\nN"
        for response in ({"behavior": "deny", "message": "no"},
                         {"behavior": "allow", "updatedInput": {"answers": {}}},
                         *({"behavior": "allow", "updatedInput": {"answers": {q: v}}} for v in ("12a", "inf", "nan", "11", "9" * 400))):
            (fact,) = self.elicit(mode="form", message="Pick", requestedSchema=schema)
            self.host.respond("a", fact[2]["request_id"], response)
            self.assertEqual(json.loads(self.popen.stdin.getvalue().splitlines()[-1])["result"], {"action": "decline"})

    def test_a_form_a_card_cannot_hold_is_declined_at_once(self):
        nested = {"type": "object", "properties": {"x": {"type": "object"}}}
        many = {"type": "object", "properties": {f"f{i}": {"type": "string"} for i in range(5)}}
        for params in ({"mode": "form", "message": "m", "requestedSchema": nested},
                       {"mode": "form", "message": "m", "requestedSchema": many},
                       {"mode": "openai/userVerification", "title": "t", "description": "d", "challenge": "c"}):
            self.assertEqual(self.elicit(**params), [])
            self.assertEqual(json.loads(self.popen.stdin.getvalue().splitlines()[-1]),
                             {"id": 5, "result": {"action": "decline"}})

    def test_a_link_is_a_card_with_the_link_and_accepts_only_on_done(self):
        (fact,) = self.elicit(mode="url", message="Confirm", url="https://example.test/ok", elicitationId="e1")
        q = fact[2]["input"]["questions"][0]
        self.assertIn("https://example.test/ok", q["question"])
        done = q["options"][0]["label"]
        self.host.respond("a", fact[2]["request_id"], {"behavior": "allow",
                                                       "updatedInput": {"answers": {q["question"]: done}}})
        self.assertEqual(json.loads(self.popen.stdin.getvalue().splitlines()[-1])["result"], {"action": "accept"})

    def test_another_request_is_not_this_slices(self):
        self.assertIsNone(codex_mcp.request(self.proc, "a", 1, "item/fileChange/requestApproval", {}))
        self.assertIsNone(codex_mcp.answer_request(self.proc, "item/fileChange/requestApproval", {}, {}))


class WholeHub(unittest.TestCase):
    """The core (daemon.Hub, core/mcpwatch) with a hub entirely on Codex: a server waiting for a sign-in shows in the
    snapshot like on Claude, the page's "Sign in" goes through the session, the end is seen by polling."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hubcxb-hub-")
        self.repo = os.path.join(self.state, "repo")
        os.makedirs(self.repo)
        with open(os.path.join(self.repo, ".mcp.json"), "w") as f:
            json.dump({"mcpServers": {"docs": {"type": "http", "url": URL}}}, f)
        self.calls = os.path.join(self.state, "calls.jsonl")
        env = {"FAKE_TURN_S": "0.2", "FAKE_CODEX_CALLS": self.calls}
        host = codex.CodexHost(self.state, codex=(sys.executable, FAKE_B), env=env, all_sessions=True,
                               default_model="gpt-def")
        self.hub = Hub(self.state, host, registry_dir=os.path.join(self.state, "registry"))
        self.hub.start()

    def tearDown(self):
        self.hub.host.kill_server()
        time.sleep(0.2)
        platforms.current.rmtree(self.state)

    def until(self, pred, timeout=15.0):
        end = time.time() + timeout
        while time.time() < end:
            self.hub.tick()
            if pred():
                return
            time.sleep(0.03)
        self.fail(f"did not wait; {json.dumps(self.hub.state.get('mcp_logins'))} {self.hub.state['sessions'].get('a')}")

    def methods(self):
        try:
            with open(self.calls, encoding="utf-8") as f:
                return [json.loads(x)["method"] for x in f]
        except OSError:
            return []

    def test_a_waiting_server_and_the_pages_sign_in_go_as_on_claude(self):
        spool.put(os.path.join(self.state, "spool"), "session.requested", "a", {"cwd": self.repo, "session_id": "sid-a"})
        self.until(lambda: "docs" in (self.hub.state.get("mcp_waits") or {}))
        sess = self.hub.state["sessions"]["a"]
        self.assertEqual(sess["info"]["mcp"], ["docs"])
        self.assertEqual(sess["info"]["mcp_auth"], ["docs"])
        self.assertEqual(sess["mcp_waits"], {"docs": mcp_addr({"url": URL})["fp"]})
        self.assertEqual(sess["mcp_addr"]["docs"]["host"], "mcp.example.test")
        # the page's "Sign in" (the owner): the session signs in, the link goes to the owner's file
        spool.put(os.path.join(self.state, "spool"), "mcp.login_requested", None,
                  {"server": "docs", "channel": "web", "owner": True})
        self.until(lambda: any(a.get("state") in ("open", "done") for a in self.hub.state.get("mcp_logins", {}).values()))
        (a,) = self.hub.state["mcp_logins"].values()
        self.assertEqual(a["session"], "a")
        self.assertNotIn("pty", a)   # through the live session, not `claude mcp login`
        self.assertTrue(mcplogin.link_of(self.state, a["id"]).startswith("https://mcp.example.test/authorize"))
        self.until(lambda: self.hub.state["mcp_logins"][a["id"]]["state"] == "done")
        self.assertNotIn("docs", self.hub.state.get("mcp_waits") or {})
        self.assertIn("mcpServer/oauth/login", self.methods())
        self.assertNotIn("config/mcpServer/reload", self.methods())   # Codex restarts the server after a sign-in itself
        logins = [e for e in journal.read(self.hub.journal.path) if e["type"] == "session.mcp_login"]
        self.assertNotIn("authorize", json.dumps(logins))   # the link never reaches the journal


class CodexSessionsDoNotFallBackToClaude(unittest.TestCase):
    """A sign-in through a Codex session cannot go the old way (`claude mcp login` in a pseudo-terminal): the hub has no
    claude, and the server is the thread's own — Codex's CLI does not know it."""

    def st(self, state, host="codex"):
        info = {"mcp": ["docs"], "mcp_auth": ["docs"], **({"host": host, "mcp_login_pty": False} if host else {})}
        return {"sessions": {"a": {"name": "a", "state": state, "info": info, "mcp_waits": {"docs": ""}}},
                "mcp_waits": {"docs": {"": {"since": 1}}}}

    def test_a_sleeping_codex_session_is_woken_not_pty(self):
        # it was a refusal ("wake the session and sign in again"): the owner pressed "Sign in" and nothing happened —
        # every holder sleeps after a restart of the hub. The core wakes the holder and signs in through it once it is up
        from core import mcpwatch
        st, actions = self.st("asleep"), []
        woke = mcpwatch.login_requested(st, {"seq": 9, "ts": 10}, {"server": "docs", "channel": "web", "owner": True},
                                        actions)
        self.assertEqual(woke["name"], "a")
        self.assertEqual(actions, [])   # the sign-in starts once the session is up (login_woke)
        (a,) = st["mcp_logins"].values()
        self.assertEqual((a["state"], a["session"], a.get("waking"), a.get("pty")), ("asked", "a", True, None))
        st, actions = self.st("asleep", host=None), []   # Claude — the old way, as before
        woke = mcpwatch.login_requested(st, {"seq": 9, "ts": 10}, {"server": "docs", "channel": "web", "owner": True},
                                        actions)
        self.assertIsNone(woke)
        self.assertEqual(actions[0]["do"], "mcp_login_pty")

    def test_an_info_from_before_the_flag_still_has_no_old_way(self):
        from core import mcpwatch
        st, actions = self.st("asleep"), []
        del st["sessions"]["a"]["info"]["mcp_login_pty"]   # host: codex only, as an earlier experiment wrote it
        woke = mcpwatch.login_requested(st, {"seq": 9, "ts": 10}, {"server": "docs", "channel": "web", "owner": True},
                                        actions)
        self.assertEqual((woke["name"], actions), ("a", []))

    def test_a_host_error_fails_the_attempt_instead_of_the_old_way(self):
        from core import mcpwatch
        st, actions = self.st("idle"), []
        mcpwatch.login_requested(st, {"seq": 9, "ts": 10}, {"server": "docs", "channel": "web", "owner": True}, actions)
        (a,) = st["mcp_logins"].values()
        mcpwatch.login_host(st, {"type": "host.error", "entity": "a", "ts": 11},
                            {"do": "mcp_login", "attempt": a["id"], "error": "RuntimeError('no process')"}, actions)
        self.assertEqual((a["state"], a.get("pty")), ("failed", None))
        self.assertEqual([x["do"] for x in actions], ["mcp_login"])


class SleepingCodexHolderIsWoken(unittest.TestCase):
    """"Sign in" on a hub on Codex while the session that holds the wait sleeps (every one does after a restart of the
    hub): the core wakes it and starts the sign-in through it once it is up — an event replay through the reducer."""

    def setUp(self):
        from core import reducer
        self.reducer, self.st, self.seq, self.actions = reducer, reducer.empty(), 0, []

    def ev(self, type, entity=None, src="cli", **data):
        self.seq += 1
        self.st, acts, why = self.reducer.reduce(self.st, {"seq": self.seq, "ts": 1000 + self.seq, "type": type,
                                                           "entity": entity, "data": data, "src": src})
        self.actions += acts
        return why

    def codex(self, name):
        self.ev("session.requested", name, cwd="/w", session_id="s-" + name, channel="cli", owner=True)
        self.ev("session.started", name, session_id="s-" + name, source="startup")
        self.ev("session.info", name, src="host", mcp=["docs"], mcp_login_pty=False)
        self.ev("session.mcp", name, src="host", servers={"docs": "needs-auth"})

    def login(self):
        self.actions = []
        why = self.ev("mcp.login_requested", None, server="docs", channel="web", owner=True)
        return why, (self.st.get("mcp_logins") or {}).get(f"l{self.seq}")

    def did(self, do):
        return [a for a in self.actions if a.get("do") == do]

    def snap(self):
        from core.views import Reader
        r = Reader()
        r.state = self.st
        return r.snapshot()

    def test_the_press_wakes_the_holder_and_the_sign_in_starts_when_it_is_up(self):
        self.codex("a")
        self.ev("session.ended", "a")   # asleep, as after a restart of the hub
        self.assertEqual(self.st["sessions"]["a"]["state"], "asleep")
        why, a = self.login()
        self.assertIsNone(why)
        self.assertEqual(self.st["sessions"]["a"]["state"], "starting")
        self.assertEqual([x["session"] for x in self.did("spawn")], ["a"])
        self.assertEqual(self.did("mcp_login") + self.did("mcp_login_pty"), [])
        (row,) = self.snap()["mcp_logins"]
        self.assertEqual((row["state"], row["session"], row.get("waking")), ("asked", "a", True))   # the page: "waking a…"
        self.actions = []
        self.ev("session.started", "a", session_id="s-a", source="resume")
        self.assertEqual(self.did("mcp_login"), [{"do": "mcp_login", "session": "a", "server": "docs", "attempt": a["id"]}])
        a = self.st["mcp_logins"][a["id"]]
        self.assertEqual((a["state"], a.get("waking")), ("asked", None))
        self.assertEqual(a["ts"], 1000 + self.seq)   # its life counts from the start, not from the press
        # its servers are still starting: the core asks again, as on any fresh start
        self.actions = []
        self.ev("session.mcp_login", "a", src="host", attempt=a["id"], server="docs", step="auth", ok=False,
                status="pending")
        self.assertEqual([x["do"] for x in self.did("mcp_login")], ["mcp_login"])
        self.ev("session.mcp_login", "a", src="host", attempt=a["id"], server="docs", step="auth", ok=True)
        self.assertEqual(self.st["mcp_logins"][a["id"]]["state"], "open")

    def test_a_second_press_while_it_wakes_does_not_wake_it_again(self):
        self.codex("a")
        self.ev("session.ended", "a")
        self.login()
        self.seq += 10   # past AGAIN: a new attempt replaces the first
        why, b = self.login()
        self.assertIsNone(why)
        self.assertEqual(self.did("spawn"), [])
        self.assertEqual((b["state"], b.get("waking")), ("asked", True))
        self.assertEqual(list(self.st["mcp_logins"]), [b["id"]])
        self.ev("session.started", "a", session_id="s-a", source="resume")
        self.assertEqual([x["attempt"] for x in self.did("mcp_login")], [b["id"]])

    def test_a_holder_that_did_not_come_up_fails_the_attempt_in_words(self):
        self.codex("a")
        self.ev("session.ended", "a")
        _, a = self.login()
        self.ev("session.ended", "a")   # went back to sleep before it started
        a = self.st["mcp_logins"][a["id"]]
        self.assertEqual(a["state"], "failed")
        self.assertIn("did not come up", a["error"])
        self.assertEqual(self.did("mcp_login"), [])

    def test_a_stopped_executor_is_not_woken_by_a_sign_in(self):
        # review: the sign-in raised a retired holder past the wake gates — its copy may be another executor's now, and
        # the owner's "stopped" mark was erased; a message does not wake it, a sign-in does not either
        self.codex("a")
        self.st["sessions"]["a"].update(role="executor", raised_by="h")
        self.assertIsNone(self.ev("session.stop_requested", "a", channel="web", owner=True))
        self.ev("session.exited", "a", why="released")
        self.ev("session.requested", "h", cwd="/h", session_id="s-h", channel="cli", owner=True)
        self.ev("session.started", "h", session_id="s-h", source="startup")
        self.actions = []
        why = self.ev("mcp.login_requested", None, server="docs", channel="session", owner=False, sender="h")
        self.assertIn("hub session wake a", str(why))
        a = self.st["sessions"]["a"]
        self.assertEqual((a["state"], (a.get("stopped") or {}).get("by")), ("retired", "owner"))
        self.assertEqual(self.did("spawn") + self.did("mcp_login") + self.did("mcp_login_pty"), [])
        self.assertFalse(self.st.get("mcp_logins"))
        self.assertEqual(self.st.get("_wake") or [], [])

    def test_a_sleeping_holder_goes_ahead_of_a_failed_one(self):
        # review: [a: failed, b: asleep] was refused "only through a live session (a)" although b could be woken
        for n in ("a", "b"):
            self.codex(n)
        self.ev("session.ended", "b")
        for _ in range(self.reducer.MAX_RESTARTS + 1):   # crashed past its restarts
            self.ev("session.exited", "a")
        self.assertEqual(self.st["sessions"]["a"]["state"], "failed")
        why, a = self.login()
        self.assertIsNone(why)
        self.assertEqual((a["session"], a.get("waking")), ("b", True))
        self.assertEqual([x["session"] for x in self.did("spawn")], ["b"])

    def test_the_named_sleeping_holder_is_the_one_woken(self):
        # review: with no live holder the pick ignored the session named (`hub mcp login b docs`) and woke "a"
        for n in ("a", "b"):
            self.codex(n)
            self.ev("session.ended", n)
        self.actions = []
        why = self.ev("mcp.login_requested", None, server="docs", session="b", channel="cli", owner=True)
        self.assertIsNone(why)
        a = self.st["mcp_logins"][f"l{self.seq}"]
        self.assertEqual((a["session"], a.get("waking")), ("b", True))
        self.assertEqual([x["session"] for x in self.did("spawn")], ["b"])
        self.assertEqual(self.st["sessions"]["a"]["state"], "asleep")

    def test_a_holder_already_starting_is_not_passed_over_for_the_named_one(self):
        # review: the name only breaks a tie — "a" woken by an earlier press is not joined by a second spawn of "b"
        for n in ("a", "b"):
            self.codex(n)
            self.ev("session.ended", n)
        self.login()
        self.assertEqual(self.st["sessions"]["a"]["state"], "starting")
        self.seq += 10   # past AGAIN
        self.actions = []
        self.assertIsNone(self.ev("mcp.login_requested", None, server="docs", session="b", channel="cli", owner=True))
        self.assertEqual(self.st["mcp_logins"][f"l{self.seq}"]["session"], "a")
        self.assertEqual(self.did("spawn"), [])
        self.assertEqual(self.st["sessions"]["b"]["state"], "asleep")

    def test_claude_holders_keep_the_old_way(self):
        self.ev("session.requested", "c", cwd="/w", session_id="s-c", channel="cli", owner=True)
        self.ev("session.started", "c", session_id="s-c", source="startup")
        self.ev("session.mcp", "c", src="host", servers={"docs": "needs-auth"})
        self.ev("session.ended", "c")
        why, a = self.login()
        self.assertIsNone(why)
        self.assertEqual((self.did("spawn"), [x["do"] for x in self.did("mcp_login_pty")]), ([], ["mcp_login_pty"]))
        self.assertEqual(self.st["sessions"]["c"]["state"], "asleep")


class HubMcpLoginOnCodex(unittest.TestCase):
    """`hub mcp login` on a hub on Codex goes the core's way (as the page's button) instead of a refusal; a server the core
    does not know as waiting has no old way on Codex — a refusal that says why."""

    def test_no_refusal_and_no_claude_pty(self):
        with tempfile.TemporaryDirectory() as tmp:
            state, cfg = os.path.join(tmp, "state"), os.path.join(tmp, "cfg")
            os.makedirs(state)
            os.makedirs(cfg)
            with open(os.path.join(cfg, "hub.json"), "w") as f:
                json.dump({"host": "codex"}, f)
            j = journal.Journal(os.path.join(state, "journal.jsonl"))
            j.append("session.requested", "a", {"cwd": tmp, "session_id": "s"}, src="cli")
            env = {**os.environ, "HUB_STATE_DIR": state, "HUB_CONFIG_DIR": cfg, "HUB_CONFIG": os.path.join(cfg, "hub.json"),
                   "PATH": os.path.dirname(sys.executable)}   # no claude on PATH
            env.pop("HUB_SESSION", None)
            out = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "hub"), "mcp", "login", "a", "docs"],
                                 capture_output=True, text=True, env=env, timeout=60)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("Codex", out.stderr)
        self.assertNotIn("claude", out.stderr.lower().replace("claude-", ""))


class HubMcpLoginWaitsForAWokenHolder(unittest.TestCase):
    """Review: `hub mcp login` waited 75 s for the link, while a woken holder first comes up and then its servers start
    (WOKE_PENDING): the command said "no link" while the sign-in was still on its way."""

    def test_the_link_after_a_long_wake_is_still_printed(self):
        import contextlib
        import importlib.machinery
        import importlib.util
        from unittest import mock
        from core import mcpwatch
        loader = importlib.machinery.SourceFileLoader("hub_cli_mcp", os.path.join(ROOT, "bin", "hub"))
        hub = importlib.util.module_from_spec(importlib.util.spec_from_loader("hub_cli_mcp", loader))
        loader.exec_module(hub)
        hub.ME = None
        # the core's timeline: the press is taken, the holder wakes for 100 s, its servers start, the link at 200 s
        rec = {"id": "l5", "server": "docs", "session": "a", "state": "asked"}
        timeline = [(0, {**rec, "waking": True}, None), (100, rec, None), (200, {**rec, "state": "open"}, "https://x/auth")]
        clock = [0]

        def wait(attempt, seconds, until, link=False):
            end = clock[0] + seconds
            for at, a, url in timeline:
                if at < clock[0] or at > end:
                    continue
                if (link and url) or until(a):
                    clock[0] = at
                    return (None, url) if link and url else (a, None)
            clock[0] = end
            return None, None

        st = {"mcp_waits": {"docs": {"": {"since": 1}}}, "sessions": {"a": {"mcp_waits": {"docs": ""}}}}
        out = io.StringIO()
        with mock.patch.object(hub, "mcp_decided", return_value=(5, None)), \
                mock.patch.object(hub, "mcp_attempt_wait", side_effect=wait), contextlib.redirect_stdout(out):
            self.assertTrue(hub.mcp_login_core(st, "a", "docs", None, True, False))
        self.assertIn("https://x/auth", out.getvalue())
        self.assertLess(clock[0], 100 + mcpwatch.WOKE_PENDING + 75 + 1)


class ClaudeHubAsMain(unittest.TestCase):
    """The claude host's MCP path does not touch this slice's code: a Claude session's info has no host mark."""

    def test_streamjson_does_not_use_codex_mcp(self):
        with open(os.path.join(ROOT, "hosts", "streamjson.py"), encoding="utf-8") as f:
            self.assertNotIn("codex_mcp", f.read())


if __name__ == "__main__":
    unittest.main()
