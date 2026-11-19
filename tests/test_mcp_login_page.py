"""A sign-in to an MCP server from the page ("Sign in" on the "MCP waits for a login" band). Before, a stale login could be
fixed only with `hub mcp login` in a terminal: `claude mcp login` in a pseudo-terminal, a separate waiting process,
approval marks in `~/.claude.json`. Claude Code signs a server in inside a live session by control requests
(mcp_authenticate → a link; its own listener takes the browser's return; mcp_oauth_callback_url takes the address from
the browser bar of another device, then mcp_reconnect), so the sign-in lives in hubd: the core picks a live session
where the server waits, keeps the link and the address after the sign-in out of the journal, sees the end by polling
the status and reconnects the other sessions. No live session that can do it — the old way, in hubd's background.
Servers from hub.json mcp_login_confirm (money, production) — only with the owner's confirmation."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import os
import stat
import tempfile
import threading
import types
import unittest
from unittest import mock

from core import mcplogin, mcpwatch, reducer
from core.views import Reader
from hosts.streamjson import StreamHost, StreamProcess

OWNER = {"channel": "web", "owner": True}
A = {"fp": "aaaaaaaaaaaa", "host": "a.example"}
B = {"fp": "bbbbbbbbbbbb", "host": "b.example"}


class Fold(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.actions, self.why = reducer.empty(), 0, [], []

    def ev(self, type, entity=None, ts=None, src="cli", **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts or 1000 + self.seq, "type": type,
                                                      "entity": entity, "data": data, "src": src})
        self.actions += acts
        return why

    def start(self, *names):
        for n in names:
            self.ev("session.requested", n, cwd="/w", session_id="s-" + n, channel="cli", owner=True)
            self.ev("session.started", n, session_id="s-" + n)

    def waits(self, name, *servers, addr=None):
        self.ev("session.mcp", name, src="host", servers={s: "needs-auth" for s in servers} | {"other": "connected"},
                **({"addr": addr} if addr else {}))

    def did(self, do):
        return [a for a in self.actions if a.get("do") == do]

    def login(self, server="docs", ts=None, **data):
        self.actions = []
        why = self.ev("mcp.login_requested", None, ts=ts, server=server, **{**OWNER, **data})
        return why, (self.st.get("mcp_logins") or {}).get(f"l{self.seq}")

    def step(self, name, attempt, src="host", **data):
        return self.ev("session.mcp_login", name, src=src, attempt=attempt["id"], server=attempt["server"], **data)

    def snap(self):
        r = Reader()
        r.state = self.st
        return r.snapshot()

    def test_a_press_asks_a_live_session_where_the_server_waits_for_the_link(self):
        self.start("a", "b")
        self.waits("a", "docs")
        self.waits("b", "docs")
        self.ev("session.ended", "b")                  # asleep: no process to sign in through
        self.ev("turn.started", "a", marker=None)       # busy: control requests work mid-turn
        why, a = self.login()
        self.assertIsNone(why)
        self.assertEqual(self.did("mcp_login"), [{"do": "mcp_login", "session": "a", "server": "docs", "attempt": a["id"]}])
        self.assertEqual((a["state"], a["session"]), ("asked", "a"))
        (row,) = self.snap()["mcp_logins"]
        self.assertEqual((row["server"], row["state"]), ("docs", "asked"))
        self.assertNotIn("url", json.dumps(row))

    def test_an_idle_session_is_preferred_to_a_busy_one(self):
        self.start("a", "b")
        self.waits("a", "docs")
        self.waits("b", "docs")
        self.ev("turn.started", "a", marker=None)
        _, a = self.login()
        self.assertEqual(a["session"], "b")

    def test_a_plugin_or_module_process_does_not_start_a_sign_in(self):
        self.start("a")
        self.waits("a", "docs")
        why = self.ev("mcp.login_requested", None, server="docs", channel="plugin", owner=False, sender="mod:x")
        self.assertIn("owner", why)
        self.assertEqual(self.st.get("mcp_logins") or {}, {})

    def test_a_module_handler_is_not_the_owner_for_a_sign_in(self):
        # a module handler's request (mod:<name>, owner) is not the owner himself: it confirms no server of the confirm
        # list and brings no address after a sign-in
        self.start("a")
        self.waits("a", "pay", "docs")
        self.ev("owner.settings", None, src="core", timezone="UTC", mcp_confirm=["pay"])
        why, _ = self.login("pay", channel="mod:x", confirmed=True)
        self.assertIn("owner", why)
        self.assertEqual(self.st.get("mcp_logins") or {}, {})
        _, a = self.login()
        self.step("a", a, step="auth", ok=True)
        self.assertIn("owner", self.ev("mcp.callback_given", None, attempt=a["id"], channel="mod:x", owner=True))

    def test_a_server_that_does_not_wait_is_refused(self):
        self.start("a")
        self.waits("a", "docs")
        why, _ = self.login("pay")
        self.assertIn("does not wait for a sign-in", why)

    def test_a_listed_server_is_gated_under_the_name_the_session_knows_it_by(self):
        # `as` renames the server in the session: listing the entry's id must cover that name, or a sign-in to it
        # (money, production) would pass as an ordinary one
        self.assertEqual(mcpwatch.confirm_list(["pay", "docs"], {"pay": {"as": "plugin:x:pay"}, "docs": {"as": 5},
                                                                 "other": {"as": "plugin:x:other"}}),
                         ["docs", "pay", "plugin:x:pay"])
        self.assertEqual(mcpwatch.confirm_list(["pay"], {"pay": "not an entry"}), ["pay"])
        self.assertEqual(mcpwatch.confirm_list(["pay"]), ["pay"])

    def test_the_gate_takes_an_as_by_the_catalogs_rule(self):
        # an `as` the catalog does not take (spaces around it) is not the session's name: the gate does not add a
        # stripped name, which would be some other server
        self.assertEqual(mcpwatch.confirm_list(["pay"], {"pay": {"as": " stripe "}}), ["pay"])

    def test_the_core_records_the_gate_with_the_as_name_from_the_settings(self):
        from core.daemon import Hub
        h = Hub(tempfile.mkdtemp(prefix="confirm-as-"), type("H", (), {"alive": lambda s, n: True})(),
                registry_dir=tempfile.mkdtemp())
        cfg = {"mcp_login_confirm": ["pay"], "mcp": {"pay": {"server": {}, "as": "plugin:x:pay"}}}
        h.read_config = lambda: cfg
        h.load_catalog()
        self.assertEqual(h.state["owner"]["mcp_confirm"], ["pay", "plugin:x:pay"])

    def test_a_server_from_the_confirm_list_needs_the_owners_confirmation(self):
        self.start("a")
        self.waits("a", "pay", "docs")
        self.ev("owner.settings", None, src="core", timezone="UTC", mcp_confirm=["pay", 5, " pay ", ""])
        self.assertEqual(self.st["owner"]["mcp_confirm"], ["pay"])
        rows = {w["server"]: w for w in self.snap()["mcp_waits"]}
        self.assertTrue(rows["pay"]["confirm"])
        self.assertNotIn("confirm", rows["docs"])
        why, _ = self.login("pay")
        self.assertIn("confirmation", why)
        why, _ = self.login("pay", confirmed="yes")     # only true
        self.assertIn("confirmation", why)
        why, a = self.login("pay", confirmed=True)
        self.assertIsNone(why)
        self.assertEqual(a["state"], "asked")
        why, _ = self.login("docs")                     # not on the list — one press
        self.assertIsNone(why)

    def test_the_link_opens_the_attempt_polling_ends_it_and_the_others_reconnect_in_place(self):
        self.start("a", "b", "c", "d", "e")
        for n in "abcde":
            self.waits(n, "docs")
        self.ev("turn.started", "b", marker=None)                       # busy
        self.ev("host.done", "c", src="core", do="mcp_reconnect", server="other", result="unsupported")   # tmux-like
        self.ev("session.ended", "d")                                   # asleep
        _, a = self.login()
        self.assertEqual(a["session"], "a")
        self.step("a", a, step="auth", ok=True)
        self.assertEqual(self.st["mcp_logins"][a["id"]]["state"], "open")
        self.actions = []
        self.step("a", a, step="status", status="connected")
        self.assertEqual(self.st["mcp_logins"][a["id"]]["state"], "done")
        self.assertEqual(sorted(x["session"] for x in self.did("mcp_reconnect")), ["b", "e"])   # not a: it signed in
        self.assertTrue(self.st["sessions"]["c"].get("dirty"))          # cannot in place: restart when free
        for n in "abcde":
            self.assertNotIn("docs", self.st["sessions"][n]["info"].get("mcp_auth") or [], n)
        self.assertEqual(self.snap()["mcp_waits"], [])

    def test_a_step_from_anyone_but_the_host_or_the_core_is_ignored(self):
        self.start("a")
        self.waits("a", "docs")
        _, a = self.login()
        self.step("a", a, src="cli", step="auth", ok=True)
        self.step("a", a, src="spool", step="status", status="connected")
        self.assertEqual(self.st["mcp_logins"][a["id"]]["state"], "asked")

    def test_a_server_already_connected_at_the_start_is_signed_in(self):
        self.start("a")
        self.waits("a", "docs")
        _, a = self.login()
        self.step("a", a, step="auth", ok=False, status="connected")
        self.assertEqual(self.st["mcp_logins"][a["id"]]["state"], "done")

    def test_a_server_still_connecting_is_asked_again_for_a_while(self):
        self.start("a")
        self.waits("a", "docs")
        _, a = self.login()
        self.actions = []
        self.step("a", a, step="auth", ok=False, status="pending")
        self.assertEqual(self.did("mcp_login"), [{"do": "mcp_login", "session": "a", "server": "docs", "attempt": a["id"],
                                                  "delay": 1}])
        self.assertEqual(self.st["mcp_logins"][a["id"]]["state"], "asked")
        self.ev("session.mcp_login", "a", ts=a["ts"] + mcpwatch.PENDING, src="host", attempt=a["id"], server="docs",
                step="auth", ok=False, status="pending")
        self.assertEqual(self.st["mcp_logins"][a["id"]]["state"], "failed")

    def test_a_failed_authenticate_says_why(self):
        self.start("a")
        self.waits("a", "docs")
        _, a = self.login()
        self.step("a", a, step="auth", ok=False, error="SDK auth failed: ECONNREFUSED")
        got = self.st["mcp_logins"][a["id"]]
        self.assertEqual((got["state"], got["error"]), ("failed", "SDK auth failed: ECONNREFUSED"))

    def test_the_address_from_a_phone_goes_through_the_callback_and_a_reconnect(self):
        self.start("a")
        self.waits("a", "docs")
        _, a = self.login()
        self.step("a", a, step="auth", ok=True)
        self.actions = []
        self.assertIsNone(self.ev("mcp.callback_given", None, attempt=a["id"], **OWNER))
        self.assertEqual(self.did("mcp_callback"), [{"do": "mcp_callback", "session": "a", "server": "docs",
                                                     "attempt": a["id"]}])
        self.assertEqual(self.st["mcp_logins"][a["id"]]["state"], "sent")
        self.step("a", a, step="callback", ok=True)
        self.assertEqual(self.did("mcp_reconnect"), [{"do": "mcp_reconnect", "session": "a", "server": "docs"}])
        self.assertEqual(self.st["mcp_logins"][a["id"]]["state"], "sent")   # the code is exchanged, not yet connected
        self.ev("session.mcp_reconnected", "a", src="host", server="docs", ok=True)
        self.assertEqual(self.st["mcp_logins"][a["id"]]["state"], "done")

    def test_a_wrong_address_keeps_the_sign_in_waiting_with_the_reason(self):
        self.start("a")
        self.waits("a", "docs")
        _, a = self.login()
        self.step("a", a, step="auth", ok=True)
        self.ev("mcp.callback_given", None, attempt=a["id"], **OWNER)
        self.step("a", a, step="callback", ok=False, error="Callback URL not accepted")
        got = self.st["mcp_logins"][a["id"]]
        self.assertEqual((got["state"], got["error"]), ("open", "Callback URL not accepted"))
        self.assertIsNone(self.ev("mcp.callback_given", None, attempt=a["id"], **OWNER))   # another try

    def test_the_address_comes_only_from_the_owner_and_only_while_the_attempt_lives(self):
        self.start("a")
        self.waits("a", "docs")
        _, a = self.login()
        self.assertIn("no longer waits for an address", self.ev("mcp.callback_given", None, attempt=a["id"], **OWNER))   # no link yet
        self.step("a", a, step="auth", ok=True)
        self.assertIn("owner", self.ev("mcp.callback_given", None, attempt=a["id"], channel="plugin", owner=False,
                                          sender="mod:x"))
        self.assertIn("no such", self.ev("mcp.callback_given", None, attempt="l999", **OWNER))
        late = self.st["mcp_logins"][a["id"]]["opened"] + mcpwatch.LIFE
        self.assertIn("no longer waits for an address", self.ev("mcp.callback_given", None, ts=late, attempt=a["id"], **OWNER))

    def test_a_new_process_of_the_session_ends_the_attempt(self):
        self.start("a")
        self.waits("a", "docs")
        _, a = self.login()
        self.step("a", a, step="auth", ok=True)
        self.ev("session.started", "a", session_id="s-a", source="resume")
        got = self.st["mcp_logins"][a["id"]]
        self.assertEqual((got["state"], got["error"]), ("failed", mcpwatch.GONE))
        self.assertEqual(mcpwatch.polling(self.st, got["ts"] + 1), [])

    def test_why_an_attempt_failed_reads_in_the_owners_language(self):
        # the state keeps the English and the marks of the core's phrase (error_l10n): the page's MCP band (the snapshot,
        # in the page server's language) and `hub mcp login` in the owner's terminal show it in Russian
        from core import i18n
        self.start("a")
        self.waits("a", "docs")
        _, a = self.login()
        self.step("a", a, step="auth", ok=True)
        self.ev("session.started", "a", session_id="s-a", source="resume")
        self.st = json.loads(json.dumps(self.st))   # as a reader's fold and state.json keep it
        ru = i18n.tr("ru", "core.mcpwatch.gone")
        self.assertEqual(mcpwatch.error_text(self.st["mcp_logins"][a["id"]], "ru"), ru)   # a Russian owner
        self.assertEqual(mcpwatch.error_text(self.st["mcp_logins"][a["id"]], "en"), str(mcpwatch.GONE))
        (row,) = [r for r in self.snap()["mcp_logins"] if r["id"] == a["id"]]
        self.assertEqual(row["error"], str(mcpwatch.GONE))   # the page in the tests' language (English)
        self.assertNotIn("error_l10n", row)
        # an error the host gives with the core's phrase (no sign-in link): its marks ride in the fact
        from hosts import streamjson
        self.waits("a", "docs")
        _, b = self.login()
        fact = json.loads(json.dumps({"step": "auth", "ok": False, **streamjson.coretext.put({}, "error",
                                                                                            streamjson.NO_LINK)}))
        self.step("a", b, **fact)
        got = self.st["mcp_logins"][b["id"]]
        self.assertEqual((got["state"], got["error"]), ("failed", "no sign-in link"))
        self.assertEqual(mcpwatch.error_text(got, "ru"), i18n.tr("ru", "core.streamjson.no_login_link"))
        # a refused address keeps the sign-in waiting with the reason; the next link clears the reason with its marks
        self.waits("a", "docs")
        _, c = self.login()
        self.step("a", c, step="auth", ok=True)
        self.step("a", c, step="callback", ok=False)
        got = self.st["mcp_logins"][c["id"]]
        self.assertEqual((got["state"], got["error"]), ("open", "the address was not accepted"))
        self.assertEqual(mcpwatch.error_text(got, "ru"), i18n.tr("ru", "core.mcpwatch.address_refused"))
        self.step("a", c, step="auth", ok=True)
        self.assertFalse({"error", "error_l10n"} & set(self.st["mcp_logins"][c["id"]]))

    def test_a_clear_inside_the_same_process_does_not_end_the_attempt(self):
        self.start("a")
        self.waits("a", "docs")
        _, a = self.login()
        self.step("a", a, step="auth", ok=True)
        self.ev("session.started", "a", session_id="s-a2", source="clear")
        self.assertEqual(self.st["mcp_logins"][a["id"]]["state"], "open")

    def test_a_restarted_live_session_that_has_not_reported_yet_signs_in_itself(self):
        # info.mcp_auth is dropped at every start, the wait is not: the host asks for the status first anyway
        self.start("a")
        self.waits("a", "docs")
        self.ev("session.started", "a", session_id="s-a", source="resume")
        _, a = self.login()
        self.assertEqual((a["session"], a.get("pty")), ("a", None))

    def test_a_sign_in_at_an_unknown_address_leaves_the_known_ones_waiting(self):
        self.start("a", "b", "c")
        self.waits("a", "docs", addr={"docs": A})
        self.waits("c", "docs", addr={"docs": B})
        self.ev("session.info", "b", mcp=["docs"], mcp_failed=[], mcp_auth=["docs"], mcp_connected=[])
        self.assertEqual(len(self.snap()["mcp_waits"]), 3)
        _, x = self.login(fp="")
        self.assertEqual(x["session"], "b")
        self.step("b", x, step="auth", ok=True)
        self.actions = []
        self.step("b", x, step="status", status="connected")
        self.assertEqual(self.did("mcp_reconnect"), [])
        for n in "ac":
            self.assertEqual(self.st["sessions"][n]["info"]["mcp_auth"], ["docs"], n)
        self.assertEqual(sorted(w["host"] for w in self.snap()["mcp_waits"]), ["a.example", "b.example"])

    def test_an_address_learned_while_signing_in_is_the_one_signed_in(self):
        self.start("a")
        self.ev("session.info", "a", mcp=["docs"], mcp_failed=[], mcp_auth=["docs"], mcp_connected=[])
        _, x = self.login(fp="")
        self.step("a", x, step="auth", ok=True)
        self.waits("a", "docs", addr={"docs": A})        # its first poll tells the address
        self.step("a", x, step="status", status="connected")
        self.assertEqual(self.snap()["mcp_waits"], [])

    def test_signed_in_without_a_human_comes_up_on_a_reconnect(self):
        self.start("a")
        self.waits("a", "docs")
        _, x = self.login()
        self.actions = []
        self.step("a", x, step="auth", ok=True, silent=True)
        self.assertEqual(self.did("mcp_reconnect"), [{"do": "mcp_reconnect", "session": "a", "server": "docs"}])
        self.ev("session.mcp_reconnected", "a", src="host", server="docs", ok=True)
        self.assertEqual(self.st["mcp_logins"][x["id"]]["state"], "done")

    def test_a_new_old_way_sign_in_to_the_same_name_replaces_the_earlier(self):
        self.start("a", "b")
        self.waits("a", "docs", addr={"docs": A})
        self.waits("b", "docs", addr={"docs": B})
        self.ev("session.ended", "a")
        self.ev("session.ended", "b")
        _, x = self.login(fp=A["fp"])
        _, y = self.login(fp=B["fp"])
        self.assertEqual(self.st["mcp_logins"][x["id"]]["state"], "failed")
        self.assertEqual(self.st["mcp_logins"][y["id"]]["state"], "asked")

    def test_a_session_that_stops_ends_the_attempt(self):
        self.start("a")
        self.waits("a", "docs")
        _, a = self.login()
        self.ev("session.ended", "a")
        self.assertEqual(self.st["mcp_logins"][a["id"]]["state"], "failed")

    def test_no_live_session_signs_in_the_old_way(self):
        self.start("a")
        self.waits("a", "docs")
        self.ev("session.ended", "a")
        _, a = self.login()
        self.assertTrue(a["pty"])
        self.assertEqual(self.did("mcp_login_pty"), [{"do": "mcp_login_pty", "session": "a", "server": "docs",
                                                      "attempt": a["id"]}])
        self.step("a", a, src="core", step="auth", ok=True, pty=True)
        self.assertEqual(mcpwatch.polling(self.st, a["ts"] + 1), [])   # nothing to poll: no live process
        self.actions = []
        self.ev("mcp.callback_given", None, attempt=a["id"], **OWNER)
        self.assertEqual(self.did("mcp_callback"), [{"do": "mcp_callback", "session": "a", "server": "docs",
                                                     "attempt": a["id"], "pty": True}])
        self.step("a", a, src="core", step="callback", ok=True, pty=True)
        self.assertEqual(self.st["mcp_logins"][a["id"]]["state"], "done")
        self.assertEqual(self.snap()["mcp_waits"], [])

    def test_a_host_without_the_requests_signs_in_the_old_way(self):
        self.start("a")
        self.waits("a", "docs")
        _, a = self.login()
        self.actions = []
        self.ev("host.error", "a", src="core", do="mcp_login", attempt=a["id"], error="AttributeError")
        self.assertEqual(len(self.did("mcp_login_pty")), 1)
        _, b = self.login(ts=a["ts"] + mcpwatch.AGAIN)   # a Claude Code that does not know the request
        self.actions = []
        self.step("a", b, step="auth", ok=False, error="Unsupported control request subtype", unsupported=True)
        self.assertEqual(len(self.did("mcp_login_pty")), 1)
        self.assertTrue(self.st["mcp_logins"][b["id"]]["pty"])

    def test_a_second_press_within_seconds_is_refused_a_later_one_replaces_the_attempt(self):
        self.start("a")
        self.waits("a", "docs")
        _, a = self.login()
        why = self.ev("mcp.login_requested", None, ts=a["ts"] + 1, server="docs", **OWNER)
        self.assertIn("has already started", why)
        why = self.ev("mcp.login_requested", None, ts=a["ts"] + mcpwatch.AGAIN, server="docs", **OWNER)
        self.assertIsNone(why)
        self.assertEqual(list(self.st["mcp_logins"]), [f"l{self.seq}"])

    def test_two_servers_under_one_name_are_signed_in_by_address(self):
        self.start("a", "b")
        self.waits("a", "docs", addr={"docs": A})
        self.waits("b", "docs", addr={"docs": B})
        why, _ = self.login()
        self.assertIn("addresses", why)
        why, x = self.login(fp=B["fp"])
        self.assertEqual((x["session"], x["host"]), ("b", "b.example"))
        self.step("b", x, step="status", status="connected")   # a session.mcp_login before open is not the end…
        self.assertEqual(self.st["mcp_logins"][x["id"]]["state"], "asked")
        self.step("b", x, step="auth", ok=True)
        self.step("b", x, step="status", status="connected")
        self.assertEqual([(w["server"], w["host"]) for w in self.snap()["mcp_waits"]], [("docs", "a.example")])


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
    """The stream-json host: the link and the address after the sign-in never go into a fact."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="mlp-")
        self.host = StreamHost(self.state)
        self.popen = FakePopen()
        self.host.procs["a"] = StreamProcess(self.popen)

    def answer(self, response=None, error=None):
        rid = self.popen.lines[-1]["request_id"]
        resp = {"request_id": rid, "subtype": "error" if error else "success",
                **({"error": error} if error else {"response": response or {}})}
        self.host.inbox.put(("a", {"type": "control_response", "response": resp}))
        return [f for f in self.host.poll() if f[0] == "session.mcp_login"]

    def status(self, state):
        return {"mcpServers": [{"name": "docs", "status": state}, {"name": "other", "status": "connected"}]}

    def test_the_status_first_then_the_link_into_a_file_only(self):
        url = "https://auth.example/authorize?response_type=code&state=s3cr3t&redirect_uri=http%3A%2F%2Flocalhost%3A3118"
        self.host.mcp_login("a", server="docs", attempt="l7")
        self.assertEqual(self.popen.lines[-1]["request"], {"subtype": "mcp_status"})
        self.assertEqual(self.answer(self.status("needs-auth")), [])
        self.assertEqual(self.popen.lines[-1]["request"], {"subtype": "mcp_authenticate", "serverName": "docs"})
        (fact,) = self.answer({"authUrl": url, "requiresUserAction": True, "callbackPort": 3118, "state": "s3cr3t"})
        self.assertEqual(fact, ("session.mcp_login", "a", {"attempt": "l7", "server": "docs", "step": "auth", "ok": True}))
        self.assertEqual(mcplogin.link_of(self.state, "l7"), url)
        path = os.path.join(self.state, "mcp-login", "links", "l7")
        if os.name != "nt":   # file permissions are POSIX; on Windows the profile folders are closed by ACLs
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_a_connected_server_gets_no_new_sign_in(self):
        self.host.mcp_login("a", server="docs", attempt="l7")
        (fact,) = self.answer(self.status("connected"))
        self.assertEqual(fact[2], {"attempt": "l7", "server": "docs", "step": "auth", "ok": False, "status": "connected"})
        self.assertEqual(len(self.popen.lines), 1)       # no mcp_authenticate

    def test_an_unknown_request_says_so(self):
        self.host.mcp_login("a", server="docs", attempt="l7")
        self.answer(self.status("needs-auth"))
        (fact,) = self.answer(error="Unsupported control request subtype: mcp_authenticate")
        self.assertTrue(fact[2]["unsupported"])
        self.assertFalse(fact[2]["ok"])

    def test_signed_in_without_a_human_is_not_a_failure(self):
        self.host.mcp_login("a", server="docs", attempt="l7")
        self.answer(self.status("needs-auth"))
        (fact,) = self.answer({"requiresUserAction": False})
        self.assertEqual((fact[2]["ok"], fact[2]["silent"]), (True, True))
        self.assertIsNone(mcplogin.link_of(self.state, "l7"))

    def test_an_error_that_repeats_the_address_loses_its_one_time_values(self):
        self.host.mcp_callback("a", server="docs", attempt="l7", url="http://localhost:1/cb?code=0n3t1me&state=s3")
        (fact,) = self.answer(error="Invalid callback URL http://localhost:1/cb?code=0n3t1me&state=s3")
        self.assertNotIn("0n3t1me", json.dumps(fact))
        self.assertNotIn("s3", fact[2]["error"].split("state=")[1])

    def test_polling_tells_only_connected(self):
        self.host.mcp_login_poll("a", server="docs", attempt="l7")
        self.assertEqual(self.answer(self.status("needs-auth")), [])
        self.host.mcp_login_poll("a", server="docs", attempt="l7")
        self.assertEqual(self.answer(error="boom"), [])
        self.host.mcp_login_poll("a", server="docs", attempt="l7")
        (fact,) = self.answer(self.status("connected"))
        self.assertEqual(fact[2], {"attempt": "l7", "server": "docs", "step": "status", "status": "connected"})

    def test_the_address_goes_to_claude_not_into_the_fact(self):
        back = "http://localhost:3118/callback?code=0n3t1me&state=s3cr3t"
        self.host.mcp_callback("a", server="docs", attempt="l7", url=back)
        self.assertEqual(self.popen.lines[-1]["request"], {"subtype": "mcp_oauth_callback_url", "serverName": "docs",
                                                           "callbackUrl": back})
        (fact,) = self.answer(error="Callback URL not accepted: it belongs to a different sign-in attempt")
        self.assertEqual(fact[2]["step"], "callback")
        self.assertNotIn("0n3t1me", json.dumps(fact))


class Daemon(unittest.TestCase):
    def setUp(self):
        from core.daemon import Hub
        self.state = tempfile.mkdtemp(prefix="mld-")
        self.calls = []
        self.host = types.SimpleNamespace(
            mcp_callback=lambda name, **kw: self.calls.append(("callback", name, kw)) or "sent",
            mcp_login_poll=lambda name, **kw: self.calls.append(("poll", name, kw)) or "sent")
        self.now = [1000.0]
        self.hub = Hub(self.state, self.host, clock=lambda: self.now[0])

    def facts(self, type):
        with open(os.path.join(self.state, "journal.jsonl"), encoding="utf-8") as f:
            return [e for e in map(json.loads, f) if e["type"] == type]

    def test_the_address_is_read_once_from_the_owners_file_and_is_not_in_the_journal(self):
        back = "http://localhost:3118/callback?code=0n3t1me&state=s3cr3t"
        mcplogin.put_callback(self.state, "l5", back)
        self.hub.execute({"do": "mcp_callback", "session": "a", "server": "docs", "attempt": "l5"})
        self.assertEqual(self.calls, [("callback", "a", {"server": "docs", "attempt": "l5", "url": back})])
        self.assertIsNone(mcplogin.take_callback(self.state, "l5"))      # taken: no second use
        self.hub.execute({"do": "mcp_callback", "session": "a", "server": "docs", "attempt": "l5"})
        self.assertEqual(len(self.calls), 1)
        (done,) = [e for e in self.facts("host.done") if e["data"].get("result") == "no-callback"]
        self.assertEqual(done["data"]["attempt"], "l5")
        with open(os.path.join(self.state, "journal.jsonl"), encoding="utf-8") as f:
            self.assertNotIn("0n3t1me", f.read())

    def test_an_open_attempt_is_polled_once_a_second_while_it_lives(self):
        self.hub.state["mcp_logins"] = {"l5": {"id": "l5", "server": "docs", "fp": "", "session": "a", "state": "open",
                                               "ts": 1000.0, "at": 1000.0}}
        self.hub.mcp_login_tick()
        self.hub.mcp_login_tick()
        self.now[0] += mcpwatch.POLL
        self.hub.mcp_login_tick()
        self.assertEqual([c[0] for c in self.calls], ["poll", "poll"])
        self.now[0] = 1000.0 + mcpwatch.LIFE
        self.hub.mcp_login_tick()
        self.assertEqual(len(self.calls), 2)

    def test_the_old_way_runs_in_the_background_and_its_step_comes_in_the_tick(self):
        self.hub.record("session.requested", "a", {"cwd": self.state, "session_id": "s-a", "channel": "cli", "owner": True})
        go = threading.Event()

        def start(server, cwd, work, say=None):
            go.wait(5)
            return "https://auth.example/authorize?response_type=code&redirect_uri=x", None
        with mock.patch("core.mcplogin.start", side_effect=start), \
                mock.patch("core.mcplogin.finish", return_value=(0, "ok code=0n3t1me")) as finish:
            self.hub.execute({"do": "mcp_login_pty", "session": "a", "server": "docs", "attempt": "l5"})
            self.hub.mcp_login_tick()
            self.assertEqual(self.facts("session.mcp_login"), [])        # the tick does not wait for the link
            go.set()
            for j in list(self.hub.mcp_jobs):
                j["done"].wait(5)
            self.hub.mcp_login_tick()
            (fact,) = self.facts("session.mcp_login")
            self.assertEqual(fact["data"], {"attempt": "l5", "server": "docs", "pty": True, "step": "auth", "ok": True})
            self.assertTrue(mcplogin.link_of(self.state, "l5").startswith("https://auth.example/"))
            mcplogin.put_callback(self.state, "l5", "http://localhost:1/cb?code=0n3t1me")
            self.hub.execute({"do": "mcp_callback", "session": "a", "server": "docs", "attempt": "l5", "pty": True})
            for j in list(self.hub.mcp_jobs):
                j["done"].wait(5)
            self.hub.mcp_login_tick()
        self.assertEqual(finish.call_args[0][1], "http://localhost:1/cb?code=0n3t1me")
        last = self.facts("session.mcp_login")[-1]["data"]
        self.assertEqual((last["step"], last["ok"]), ("callback", True))

    def test_an_address_pasted_just_after_the_listener_took_the_login_is_not_a_failure(self):
        from core import i18n
        mcplogin.put_callback(self.state, "l6", "http://localhost:1/cb?code=x")
        with mock.patch("core.mcplogin.finish", return_value=(1, i18n.t("cli.mcp.not_waiting_address"))), \
                mock.patch("core.mcplogin.ended", return_value=(0, "")):
            self.hub.execute({"do": "mcp_callback", "session": "a", "server": "docs", "attempt": "l6", "pty": True})
            for j in list(self.hub.mcp_jobs):
                j["done"].wait(5)
            self.hub.mcp_login_tick()
        last = self.facts("session.mcp_login")[-1]["data"]
        self.assertEqual((last["step"], last["ok"]), ("callback", True))


class OldWayListener(unittest.TestCase):
    def test_the_old_way_ends_by_itself_when_the_browser_came_back_on_the_hubs_machine(self):
        from core.daemon import Hub
        state = tempfile.mkdtemp(prefix="mlo-")
        hub = Hub(state, types.SimpleNamespace(), clock=lambda: 1000.0)
        hub.state["mcp_logins"] = {"l5": {"id": "l5", "server": "docs", "fp": "", "session": "a", "state": "open",
                                          "ts": 990.0, "at": 990.0, "pty": True}}
        with mock.patch("core.mcplogin.ended", return_value=None):
            hub.mcp_login_tick()
        with mock.patch("core.mcplogin.ended", return_value=(0, "ok code=0n3t1me")):
            hub.mcp_login_tick()       # within the pause: not asked again
            hub.clock = lambda: 1000.0 + 2 * mcpwatch.POLL
            hub.mcp_login_tick()
        with open(os.path.join(state, "journal.jsonl"), encoding="utf-8") as f:
            facts = [e["data"] for e in map(json.loads, f) if e["type"] == "session.mcp_login"]
        self.assertEqual(facts, [{"attempt": "l5", "server": "docs", "pty": True, "step": "callback", "ok": True}])


class Files(unittest.TestCase):
    def test_the_attempt_id_is_the_only_name_and_old_files_go(self):
        d = tempfile.mkdtemp(prefix="mlf-")
        with self.assertRaises(ValueError):
            mcplogin.put_link(d, "../x", "https://a")
        mcplogin.put_link(d, "l1", "https://a")
        old = os.path.join(d, "mcp-login", "links", "l1")
        os.utime(old, (1, 1))
        mcplogin.put_link(d, "l2", "https://b")
        self.assertFalse(os.path.exists(old))
        self.assertIsNone(mcplogin.link_of(d, "../../etc"))

    def test_the_one_time_values_go_in_any_spelling(self):
        for text in ("cb?code=0n3&state=s3", "cb%3Fcode%3D0n3%26state%3Ds3", '{"code": "0n3", "state":"s3"}'):
            self.assertNotIn("0n3", mcplogin.scrub(text), text)
            self.assertNotIn("s3", mcplogin.scrub(text), text)
        self.assertEqual(mcplogin.scrub("error_code=5"), "error_code=5")


if __name__ == "__main__":
    unittest.main()
