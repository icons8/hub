"""A turn cut by the network ("API Error: Can't reach the API server … (ENOTFOUND)": Claude Code retried for minutes and
gave up) continues by itself once the Claude API answers again. Before this, such sessions stood until someone wrote to
them: with the owner on the road, for hours. The core marks the cut (net_cut, a growing pause, a cap on repeats), hubd fires
its timer only when a probe through the sessions' proxy gets an answer from the API (core/netprobe.py), and the session gets
one continuation in the cut turn's chain: a working or parked one only, never twice with the restart's or the limit's."""
import tests  # noqa: F401 - ignore the machine's settings
import socket
import tempfile
import threading
import types
import unittest

from core import executors, i18n, netprobe, reducer
from core.daemon import Hub

OWNER = {"channel": "cli", "owner": True}
NET = "API Error: Can't reach the API server — check your internet or DNS (ENOTFOUND)"
AGAIN = "the API answers again"
NO_ROUTE = "Failed to authenticate. API Error: 403 Request not allowed"


def by(name):
    return {"channel": "session", "owner": False, "sender": name}


class Folding:
    """A fold with helpers (not a TestCase: a subclass does not pull in others' tests)."""

    def setUp(self):
        self.st, self.seq = reducer.empty(), 0

    def ev(self, t, e=None, ts=None, **d):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts or 100 + self.seq, "type": t, "entity": e,
                                                   "data": d})
        return why

    def timers(self):
        return [t for t in reducer.pending_timers(self.st) if t[1].startswith(reducer.NET_TIMER)]

    def to(self, name):
        return [m for m in self.st["messages"].values() if m["session"] == name]

    def continuations(self, name):
        return [m for m in self.to(name) if AGAIN in m["text"]]

    def fire(self):
        (due, key, n), = self.timers()
        self.ev("timer.fired", key, ts=due, attempt=n)


class Owner(Folding, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.ev("session.requested", "maps", cwd="/m", session_id="s-m", **OWNER)
        self.ev("session.started", "maps")

    def cut(self, text=NET):
        self.ev("message.received", None, to="maps", text="do it", channel="telegram", owner=True)
        mid = f"m{self.seq}"
        self.ev("turn.started", "maps", marker=mid)
        self.ev("turn.result", "maps", text=text, is_error=True)
        return mid

    def test_the_cut_turn_continues_once_in_the_owners_thread(self):
        mid = self.cut()
        self.assertEqual(len(self.timers()), 1)
        self.fire()
        (go,) = self.continuations("maps")
        self.assertEqual(go.get("re"), mid)              # its result is the reply to his message
        self.assertEqual(go["origin"], reducer.forms.ORIGIN_CORE)
        self.assertEqual(self.timers(), [])              # one continuation, not in a loop
        self.ev("timer.fired", reducer.NET_TIMER + "maps", attempt=0)   # a stale timer is a fact with no consequences
        self.assertEqual(len(self.continuations("maps")), 1)

    def test_a_second_result_of_the_same_turn_is_not_a_second_cut(self):
        self.cut()
        self.ev("turn.result", "maps", text=NET, is_error=True)
        self.assertEqual(self.st["sessions"]["maps"]["net_tries"], 1)
        self.assertEqual(len(self.timers()), 1)

    def test_the_owner_wrote_first_nothing_to_continue(self):
        self.cut()
        self.ev("message.received", None, to="maps", text="so?", channel="telegram", owner=True)
        self.ev("turn.started", "maps", marker=f"m{self.seq}")
        self.assertEqual(self.timers(), [])

    def test_a_message_waiting_for_the_session_starts_the_turn_instead(self):
        self.cut()
        self.ev("message.received", None, to="maps", text="so?", channel="telegram", owner=True)   # not taken yet
        self.assertEqual(self.timers(), [])              # no timer while it is on its way: its turn is the continuation
        self.ev("timer.fired", reducer.NET_TIMER + "maps", attempt=0)   # (a timer fired before it came) — nothing
        self.assertEqual(self.continuations("maps"), [])
        self.ev("turn.started", "maps", marker=f"m{self.seq - 1}")
        self.assertNotIn("net_cut", self.st["sessions"]["maps"])

    def test_a_message_queued_for_a_parked_session_starts_the_turn_instead(self):
        self.cut()
        self.ev("session.parked", "maps", channel="core")
        self.ev("message.received", None, to="maps", text="so?", channel="telegram", owner=True)   # raises it, queued
        (queued,) = [m for m in self.to("maps") if m["state"] == "queued"]
        self.assertEqual(self.timers(), [])
        self.assertEqual(self.continuations("maps"), [])

    def test_a_waiting_message_removed_gives_the_continuation_back(self):
        self.cut()
        self.ev("session.parked", "maps", channel="core")
        self.ev("message.received", None, to="maps", text="so?", channel="telegram", owner=True)
        (queued,) = [m for m in self.to("maps") if m["state"] == "queued"]
        self.assertIsNone(self.ev("message.cancel", queued["id"], **OWNER))
        self.fire()
        self.assertEqual(len(self.continuations("maps")), 1)

    def test_failing_again_waits_longer_and_gives_up(self):
        self.cut()
        waits = []
        for _ in range(reducer.NET_TRIES + 2):
            if not self.timers():
                break
            (due, _, _), = self.timers()
            waits.append(due - self.st["sessions"]["maps"]["net_cut"]["ts"])
            self.fire()
            self.ev("turn.started", "maps", marker=self.continuations("maps")[-1]["id"])
            self.ev("turn.result", "maps", text="API Error: Connection error.", is_error=True)
        self.assertEqual(len(waits), reducer.NET_TRIES)
        self.assertEqual(waits, sorted(waits))
        self.assertLess(waits[0], waits[-1])

    def test_a_good_turn_resets_the_count(self):
        self.cut()
        self.fire()
        self.ev("turn.started", "maps", marker=self.continuations("maps")[-1]["id"])
        self.ev("turn.result", "maps", text="done")
        self.assertNotIn("net_tries", self.st["sessions"]["maps"])

    def test_a_built_in_command_does_not_reset_the_count(self):
        self.cut()
        self.fire()
        self.ev("turn.started", "maps", marker=self.continuations("maps")[-1]["id"])
        self.ev("turn.result", "maps", text="API Error: Connection error.", is_error=True)
        self.ev("turn.result", "maps", text="Compacted", turns=0)   # /compact: not a turn that went through
        self.assertEqual(self.st["sessions"]["maps"]["net_tries"], 2)

    def test_the_other_forms_claude_code_names_a_network_failure_by(self):
        for text in ("API Error: Connection dropped (EPIPE)",
                     "API Error: Connection refused \u2014 a firewall or proxy may be blocking it (ConnectionRefused)",
                     "API Error: No internet route \u2014 check your connection or VPN (EHOSTUNREACH)",
                     "API Error: Unable to connect to API (FailedToOpenSocket)",
                     "API Error: Unable to connect to API. Check your internet connection"):
            self.assertTrue(reducer.net_cut({"is_error": True, "text": text}), text)
        for text in ("API Error: Unable to connect to API: SSL certificate has expired",   # not a thing a wait fixes
                     "API Error: Couldn't connect through your proxy (ERR_PROXY_TUNNEL) \u2014 the proxy refused the tunnel"):
            self.assertFalse(reducer.net_cut({"is_error": True, "text": text}), text)

    def test_other_api_errors_are_not_continued(self):
        for text in ("API Error: 400 invalid_request_error", "API Error: 529 Overloaded",
                     "You've hit your session limit · resets 5pm"):
            self.cut(text)
            self.assertFalse(self.st["sessions"]["maps"].get("net_cut"), text)
            self.assertEqual(self.timers(), [], text)

    def test_a_closed_session_gets_nothing(self):
        self.cut()
        self.ev("session.close_requested", "maps", **OWNER)
        self.assertEqual(self.timers(), [])   # the handoff request is on its way: its turn ends the cut
        self.ev("turn.started", "maps", marker=next(m["id"] for m in self.to("maps") if m["state"] == "typing"))
        self.assertEqual(self.timers(), [])
        self.assertEqual(self.continuations("maps"), [])
        self.assertNotIn("net_cut", self.st["sessions"]["maps"])

    def test_a_restart_that_continues_the_turn_is_the_only_continuation(self):
        self.cut()
        self.st["sessions"]["maps"]["cut_turn"] = {"ts": self.seq, "background": []}   # the process left with the turn
        self.ev("session.started", "maps")
        self.assertEqual(self.timers(), [])
        self.assertEqual(self.continuations("maps"), [])
        self.assertEqual(len([m for m in self.to("maps") if m["origin"] == reducer.forms.ORIGIN_CORE]), 1)

    def test_a_limit_cut_waiting_on_the_session_is_not_doubled(self):
        self.cut()
        self.st["sessions"]["maps"]["limit_cut"] = {"ts": self.seq, "account": None}
        self.fire()
        self.assertEqual(self.continuations("maps"), [])
        self.assertIn("limit_cut", self.st["sessions"]["maps"])

    def test_the_owner_sees_it_named(self):
        self.assertEqual(reducer.failure({"is_error": True, "text": NET})[0], i18n.t("core.failure.network"))
        self.assertEqual(reducer.failure({"is_error": True, "text": "API Error: Connection error."})[0],
                         i18n.t("core.failure.network"))


class Executor(Folding, unittest.TestCase):
    def test_the_continuation_carries_the_task(self):
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.started", "hub")
        self.ev("session.requested", "frontend", cwd="/fe", session_id="s-fe", role="agent", about="Frontend", **OWNER)
        self.ev("session.started", "frontend")
        self.assertIsNone(self.ev("task.run_requested", "frontend-ci", task="fix CI", **by("frontend")))
        for t, e, d in executors.events_for(self.st, "frontend-ci", self.st["runs"]["frontend-ci"], cwd="/x",
                                            worktree=None):
            self.assertIsNone(self.ev(t, e, **d))
        self.ev("session.started", "frontend-ci")
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == "frontend-ci"]
        self.ev("turn.started", "frontend-ci", marker=d["message"])
        self.ev("turn.result", "frontend-ci", text=NET, is_error=True)
        self.fire()
        (go,) = self.continuations("frontend-ci")
        self.assertEqual(go.get("for_delegation"), d["id"])


class Daemon(unittest.TestCase):
    def test_the_timer_waits_for_the_api(self):
        h = Hub(tempfile.mkdtemp(prefix="net-d-"), types.SimpleNamespace(poll=lambda: []))
        h.execute = lambda a: None
        h.record("session.requested", "maps", {"cwd": "/m", "session_id": "s-m", **OWNER})
        h.record("session.started", "maps", {})
        h.record("turn.started", "maps", {})
        h.record("turn.result", "maps", {"text": NET, "is_error": True})
        h.clock = lambda: 10 ** 11
        h.net_up = lambda: False
        h.fire_timers()
        self.assertIn("net_cut", h.state["sessions"]["maps"])      # no answer from the API: it waits
        h.net_up = lambda: True
        h.fire_timers()
        self.assertNotIn("net_cut", h.state["sessions"]["maps"])
        self.assertIn(AGAIN, [m for m in h.state["messages"].values() if m["session"] == "maps"][-1]["text"])

    def cut_by(self, text):
        h = Hub(tempfile.mkdtemp(prefix="net-d-"), types.SimpleNamespace(poll=lambda: []))
        h.execute = lambda a: None
        h.record("session.requested", "maps", {"cwd": "/m", "session_id": "s-m", **OWNER})
        h.record("session.started", "maps", {})
        h.record("turn.started", "maps", {})
        h.record("turn.result", "maps", {"text": text, "is_error": True})
        h.clock = lambda: 10 ** 11
        return h

    def test_a_network_the_api_refuses_waits_for_its_own_probe_and_spends_no_repeat(self):
        h = self.cut_by(NO_ROUTE)
        asked = []
        h.net_up = lambda route=False: asked.append(route) or False
        for _ in range(reducer.NET_TRIES * 2):
            h.fire_timers()
        self.assertEqual(set(asked), {True}, "this cut is asked with the probe for the refused network")
        self.assertEqual(h.state["sessions"]["maps"]["net_tries"], 1)
        self.assertIn("net_cut", h.state["sessions"]["maps"])
        h.net_up = lambda route=False: route
        h.fire_timers()
        self.assertNotIn("net_cut", h.state["sessions"]["maps"])
        self.assertIn(AGAIN, [m for m in h.state["messages"].values() if m["session"] == "maps"][-1]["text"])

    def test_a_later_result_of_the_same_turn_that_names_the_refused_network_marks_the_cut(self):
        h = self.cut_by(NET)
        h.record("turn.result", "maps", {"text": NO_ROUTE, "is_error": True})
        self.assertTrue(h.state["sessions"]["maps"]["net_cut"].get("route"))
        self.assertEqual(h.state["sessions"]["maps"]["net_tries"], 1)

    def test_the_two_probes_are_two_probers_with_their_own_question(self):
        from unittest import mock
        h = Hub(tempfile.mkdtemp(prefix="net-d-"), types.SimpleNamespace(poll=lambda: []))
        with mock.patch.object(netprobe.Prober, "up", lambda self: False):
            h.net_up()
            h.net_up(True)
        self.assertIsNot(h.netprobes[False], h.netprobes[True])
        self.assertIs(h.netprobes[False].probe, netprobe.reachable)
        self.assertIs(h.netprobes[True].probe, netprobe.allowed)

    def test_the_other_cuts_keep_their_probe(self):
        h = self.cut_by(NET)
        h.net_up = lambda: False      # no argument: the probe as before
        h.fire_timers()
        self.assertIn("net_cut", h.state["sessions"]["maps"])

    def test_the_cut_by_the_refused_network_is_marked(self):
        h = self.cut_by(NO_ROUTE)
        self.assertTrue(h.state["sessions"]["maps"]["net_cut"].get("route"))
        self.assertFalse(self.cut_by(NET).state["sessions"]["maps"]["net_cut"].get("route"))

    def test_no_probe_without_a_cut(self):
        h = Hub(tempfile.mkdtemp(prefix="net-d-"), types.SimpleNamespace(poll=lambda: []))
        h.net_up = lambda: self.fail("probed with nothing to continue")
        h.fire_timers()


class FakeProxy:
    """An http proxy on this machine: CONNECT gets `connect`; after a 200 it answers the request itself with `status`."""

    def __init__(self, connect=200, status=404, body=b""):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.connect, self.status, self.body, self.seen = connect, status, body, []
        threading.Thread(target=self.serve, daemon=True).start()

    def head(self, f):
        lines = []
        while True:
            line = f.readline()
            if line in (b"", b"\r\n", b"\n"):
                return lines
            lines.append(line.decode().strip())

    def serve(self):
        try:
            c, _ = self.sock.accept()
        except OSError:
            return
        with c, c.makefile("rb") as f:
            self.seen += self.head(f)[:1]
            c.sendall(f"HTTP/1.1 {self.connect} X\r\n\r\n".encode())
            if self.connect == 200:
                head = self.head(f)
                self.seen += head[:1]
                size = next((int(h.split(":")[1]) for h in head if h.lower().startswith("content-length:")), 0)
                f.read(size)
                c.sendall(f"HTTP/1.1 {self.status} X\r\nContent-Length: {len(self.body)}\r\n\r\n".encode() + self.body)

    def close(self):
        self.sock.close()


class Probe(unittest.TestCase):
    def test_the_probe_asks_the_api_not_a_local_gateway(self):
        self.assertEqual(netprobe.target({}), ("https", "api.anthropic.com", 443))
        self.assertEqual(netprobe.target({"ANTHROPIC_BASE_URL": "http://127.0.0.1:8317"}),
                         ("https", "api.anthropic.com", 443))
        self.assertEqual(netprobe.target({"ANTHROPIC_BASE_URL": "https://llm.example.test/v1"}),
                         ("https", "llm.example.test", 443))

    def probe(self, **kw):
        p = FakeProxy(**kw)
        self.addCleanup(p.close)
        env = {"ANTHROPIC_BASE_URL": "http://api.example.test", "http_proxy": f"http://127.0.0.1:{p.port}"}
        return netprobe.reachable(env, timeout=5), p.seen

    def test_an_answer_of_the_api_through_the_proxy_means_it_is_back(self):
        ok, seen = self.probe(status=404)
        self.assertTrue(ok)
        self.assertEqual(seen, ["CONNECT api.example.test:80 HTTP/1.1", "HEAD / HTTP/1.1"])

    def test_a_proxy_that_is_up_proves_nothing(self):
        self.assertFalse(self.probe(connect=502)[0])     # it refused to reach the API
        self.assertFalse(self.probe(status=503)[0])      # it let us in and failed itself

    ALLOWED = b'{"error":{"type":"forbidden","message":"Request not allowed"}}'

    def allowed(self, **kw):
        p = FakeProxy(**kw)
        self.addCleanup(p.close)
        env = {"ANTHROPIC_BASE_URL": "http://api.example.test", "http_proxy": f"http://127.0.0.1:{p.port}"}
        return netprobe.allowed(env, timeout=5), p.seen

    def test_the_network_the_api_refuses_is_not_back_until_it_answers_otherwise(self):
        # a request with no key to the messages path: the network refuses it before the key is looked at (403), else the
        # key is (401); no model is called either way
        ok, seen = self.allowed(status=403, body=self.ALLOWED)
        self.assertFalse(ok)
        self.assertEqual(seen, ["CONNECT api.example.test:80 HTTP/1.1", "POST /v1/messages HTTP/1.1"])
        for status, body in ((401, b'{"error":{"type":"authentication_error"}}'), (400, b""), (404, b""),
                             (429, b"")):
            self.assertTrue(self.allowed(status=status, body=body)[0], status)
        for body in (b"", b"<html>Access denied</html>"):   # any 403: something in front still refuses the network
            self.assertFalse(self.allowed(status=403, body=body)[0], body)

    def test_a_proxy_failure_is_not_the_network_back_for_this_probe_either(self):
        self.assertFalse(self.allowed(connect=502)[0])
        self.assertFalse(self.allowed(status=503)[0])

    def test_a_stale_answer_is_not_trusted(self):
        now = [0.0]
        p = netprobe.Prober(lambda: {}, probe=lambda env: True, clock=lambda: now[0])
        p.up()
        p.thread.join()
        self.assertTrue(p.up())
        p.probe = lambda env: False
        now[0] = 10 ** 4                                  # an hour later: a new probe, the old "yes" does not count
        self.assertFalse(p.up())
        p.thread.join()
        self.assertFalse(p.up())


if __name__ == "__main__":
    unittest.main()
