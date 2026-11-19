"""Codex (experimental): Codex limits and sign-in on a hub on Codex.
The host reads the account limits (account/rateLimits/read, …/updated) and writes the same account.limits as Claude; a
turn cut by the subscription limit is continued by the core itself after the window resets; no sign-in, or it was
lost — codex.auth_lost, one notice to the owner (not a turn of the hub session)."""
import tests  # noqa: F401 — do not see the machine's settings (~/.config/hub-core), see tests/__init__.py
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

import platforms

from core import i18n, journal, reducer, spool
from core.daemon import Hub
from hosts import codex_limits
from hosts.codex import CodexHost

HERE = os.path.dirname(__file__)
FAKE_CODEX = os.path.join(HERE, "fakes", "fake_codex.py")

# the reply of the real 0.162.1 (probe 2026-10-19, prolite): one window, a week
READ = {"ordinaryUsageAllowed": True, "rateLimits": {
    "limitId": "codex", "primary": {"usedPercent": 7, "windowDurationMins": 10080, "resetsAt": 1792905741},
    "secondary": None, "planType": "prolite", "rateLimitReachedType": None}}
NOAUTH = {"error": {"code": -32600, "message": "codex account authentication required to read rate limits"}}


class Facts(unittest.TestCase):
    def setUp(self):
        codex_limits.reset()

    def test_read_maps_windows_like_claude(self):
        facts = codex_limits.response(None, "maps", "account/rateLimits/read", {}, {"id": 5, "result": READ})
        self.assertEqual(facts, [("account.limits", None, {
            "status": "allowed", "type": None, "overage": False, "engine": "codex", "plan": "prolite",
            "windows": {"seven_day": {"used": 0.07, "resets": 1792905741}}, "usage_allowed": True})])
        # the same numbers — no fact; a sparse update is merged with the previous one (the week window did not vanish)
        self.assertEqual(codex_limits.response(None, "maps", "account/rateLimits/read", {}, {"id": 6, "result": READ}), [])
        five = {"rateLimits": {"primary": {"usedPercent": 40, "windowDurationMins": 300, "resetsAt": 1000}}}
        (fact,) = codex_limits.notice(None, "maps", "account/rateLimits/updated", five)
        self.assertEqual(set(fact[2]["windows"]), {"seven_day", "five_hour"})
        self.assertEqual(fact[2]["windows"]["five_hour"], {"used": 0.4, "resets": 1000})

    def test_reached_or_not_allowed_is_rejected(self):
        snap = {**READ["rateLimits"], "rateLimitReachedType": "rate_limit_reached"}
        (f,) = codex_limits.response(None, "m", "account/rateLimits/read", {}, {"result": {"rateLimits": snap}})
        self.assertEqual((f[2]["status"], f[2]["type"]), ("rejected", "rate_limit_reached"))
        codex_limits.reset()
        (f,) = codex_limits.response(None, "m", "account/rateLimits/read", {},
                                     {"result": {**READ, "ordinaryUsageAllowed": False}})
        self.assertEqual(f[2]["status"], "rejected")

    def test_update_does_not_keep_old_not_allowed_and_skips_other_buckets(self):
        codex_limits.response(None, "m", "account/rateLimits/read", {}, {"result": {**READ, "ordinaryUsageAllowed": False}})
        (f,) = codex_limits.notice(None, "m", "account/rateLimits/updated", {"rateLimits": {
            "limitId": "codex", "primary": {"usedPercent": 0, "windowDurationMins": 10080, "resetsAt": 2}}})
        self.assertEqual(f[2]["status"], "allowed")   # the reset time changed — a new window
        codex_limits.reset()
        codex_limits.response(None, "m", "account/rateLimits/read", {}, {"result": {**READ, "ordinaryUsageAllowed": False}})
        (f,) = codex_limits.notice(None, "m", "account/rateLimits/updated", {"rateLimits": {
            "primary": {"usedPercent": 9, "windowDurationMins": 10080, "resetsAt": 1792905741}}})
        self.assertEqual(f[2]["status"], "rejected")   # the same window — "not allowed" holds
        self.assertEqual(codex_limits.notice(None, "m", "account/rateLimits/updated", {"rateLimits": {
            "limitId": "other", "primary": {"usedPercent": 99, "windowDurationMins": 10080}}}), [])

    def test_other_window_length_kept_but_named_apart(self):
        snap = {"primary": {"usedPercent": 10, "windowDurationMins": 1440, "resetsAt": 5}}
        (f,) = codex_limits.response(None, "m", "account/rateLimits/read", {}, {"result": {"rateLimits": snap}})
        self.assertEqual(list(f[2]["windows"]), ["codex_1440m"])

    def test_no_login_on_read(self):
        self.assertEqual(codex_limits.response(None, "m", "account/rateLimits/read", {}, NOAUTH)[0][0], "codex.auth_lost")
        self.assertEqual(codex_limits.response(None, "m", "account/rateLimits/read", {},
                                               {"error": {"message": "network down"}}), [])
        self.assertIsNone(codex_limits.response(None, "m", "turn/start", {}, {"result": {}}))   # not ours — to the host

    def test_turn_errors(self):
        codex_limits.response(None, "m", "account/rateLimits/read", {}, {"result": {"rateLimits": {
            "primary": {"usedPercent": 100, "windowDurationMins": 10080, "resetsAt": 4102444800}}}})
        got = codex_limits.result(None, {"status": "failed", "error": {"message": "You've hit your usage limit.",
                                                                       "codexErrorInfo": "usageLimitExceeded"}})
        self.assertTrue(got["limit"])
        self.assertEqual(got["limit_resets"], 4102444800)
        self.assertRegex(got["text"], reducer.LIMIT_TEXT)
        # probe 2026-10-19 without sign-in: httpConnectionFailed 401
        got = codex_limits.result(None, {"status": "failed", "error": {
            "message": "unexpected status 401 Unauthorized", "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 401}}}})
        self.assertTrue(got["auth_lost"])
        # a short-lived rateLimitExceeded is an ordinary turn error, not a cut by the subscription limit
        got = codex_limits.result(None, {"status": "failed", "error": {"message": "slow down",
                                                                       "codexErrorInfo": "rateLimitExceeded"}})
        self.assertEqual(got, {"engine": "codex"})
        self.assertEqual(codex_limits.result(None, {"status": "completed"}), {"engine": "codex"})


class Fold(unittest.TestCase):
    """The fold: the Codex limit, continuing after the reset, sign-in."""

    def setUp(self):
        self.st, self.seq, self.acts = reducer.empty(), 0, []
        owner = {"channel": "cli", "owner": True}
        self.ev("session.requested", "hub", cwd="/h", session_id="s-h", kind="hub", **owner)
        self.ev("session.requested", "maps", cwd="/m", session_id="s-m", **owner)
        for n in ("hub", "maps"):
            self.ev("session.started", n, session_id=f"s-{n[0]}", source="startup")

    def ev(self, t, e=None, ts=None, **d):
        self.seq += 1
        self.st, self.acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts or 100 + self.seq, "type": t,
                                                           "entity": e, "data": d})
        return why

    def to_hub(self):
        return [m["text"] for m in self.st["messages"].values() if m["to"] == "hub"]

    def cut(self, **extra):
        self.ev("message.received", None, to="maps", text="make a report", channel="web", owner=True)
        mid = list(self.st["messages"])[-1]
        self.ev("turn.started", "maps", marker=mid)
        self.ev("turn.result", "maps", text="Codex: usage limit reached (usageLimitExceeded)", is_error=True,
                engine="codex", limit=True, **extra)

    def test_cut_continues_once_after_reset(self):
        self.ev("account.limits", None, status="rejected", windows={"seven_day": {"used": 1.0, "resets": 5000}},
                engine="codex")
        self.cut()
        self.assertEqual(self.st["sessions"]["maps"]["limit_cut"]["resets"], 5000)
        self.assertFalse(reducer.codex_due(self.st, 4999))
        self.assertTrue(reducer.codex_due(self.st, 5000))
        self.assertIsNotNone(self.ev("codex.limit_reset", None, ts=5001, channel="cli", owner=True))   # not the core
        before = set(self.st["messages"])
        self.assertIsNone(self.ev("codex.limit_reset", None, ts=5001, channel="core"))
        (new,) = set(self.st["messages"]) - before
        self.assertEqual(self.st["messages"][new]["to"], "maps")
        self.assertTrue(self.st["messages"][new]["text"].startswith("The Codex limit cut off your turn"))
        self.assertNotIn("limit_cut", self.st["sessions"]["maps"])
        self.assertFalse(reducer.codex_due(self.st, 6000))   # exactly once

    def test_the_clock_continuation_of_claude_cuts_leaves_a_codex_cut(self):
        # core/limitresume continues a cut of the one Claude login by the clock with a Claude text; a Codex cut has its
        # own (codex.limit_reset): one continuation, its own words — even when the daemon was down past both resets
        from core import limitresume
        self.cut(limit_resets=5000)
        self.assertEqual(limitresume.due(self.st, 10 ** 6), [])
        self.assertTrue(reducer.codex_due(self.st, 10 ** 6))

    def test_rejected_without_full_window_takes_nearest_reset(self):
        self.ev("account.limits", None, status="rejected", engine="codex", windows={
            "seven_day": {"used": 0.99, "resets": 9000}, "five_hour": {"used": 0.5, "resets": 3000}})
        self.cut()
        self.assertEqual(self.st["sessions"]["maps"]["limit_cut"]["resets"], 3000)

    def test_resets_from_result_wins_and_unknown_waits_owner(self):
        self.cut(limit_resets=7000)
        self.assertEqual(self.st["sessions"]["maps"]["limit_cut"]["resets"], 7000)
        self.st["sessions"]["maps"].pop("limit_cut")
        self.cut()   # the reset is unknown — no auto-continue
        self.assertIsNone(self.st["sessions"]["maps"]["limit_cut"].get("resets"))   # unknown: no continuation by the clock
        self.assertFalse(reducer.codex_due(self.st, 10 ** 10))

    def test_stale_reset_is_not_used(self):
        # the snapshot is stale: the exhausted window has already reset — no auto-continue (otherwise a continue every tick)
        self.ev("account.limits", None, status="allowed", windows={"seven_day": {"used": 1.0, "resets": 50},
                                                                    "five_hour": {"used": 0.99, "resets": 90000}},
                engine="codex")
        self.cut(limit_resets=60)
        self.assertIsNone(self.st["sessions"]["maps"]["limit_cut"].get("resets"))   # unknown: no continuation by the clock

    def test_parked_session_is_continued(self):
        self.cut(limit_resets=5000)
        self.st["sessions"]["maps"]["state"] = "asleep"   # stopped — do not wake
        self.assertFalse(reducer.codex_due(self.st, 5000))
        self.st["sessions"]["maps"]["state"] = "parked"    # parked by the core after 2 h idle (a state since 2026-10-25) — continue
        self.assertTrue(reducer.codex_due(self.st, 5000))
        self.ev("codex.limit_reset", None, ts=5001, channel="core")
        self.assertNotIn("limit_cut", self.st["sessions"]["maps"])

    def test_lost_cleared_by_a_good_turn(self):
        self.ev("codex.auth_lost", None, why="401")
        self.ev("turn.result", "maps", text="done", is_error=False, engine="codex")
        self.assertIsNone(self.st["codex"]["lost"])

    def test_claude_cut_untouched(self):
        self.ev("message.received", None, to="maps", text="x", channel="web", owner=True)
        self.ev("turn.result", "maps", text="You've hit your weekly limit · resets 5pm", is_error=True)
        self.assertNotIn("engine", self.st["sessions"]["maps"]["limit_cut"])
        self.assertFalse(reducer.codex_due(self.st, 10 ** 10))

    def test_failure_label_says_codex(self):
        self.assertEqual(reducer.failure({"is_error": True, "text": "Codex: usage limit reached", "engine": "codex"})[0],
                         i18n.t("core.failure.limit").replace("Claude", "Codex"))   # the owner's language (tests: en)
        self.assertEqual(reducer.failure({"is_error": True, "text": "unexpected status 401 Unauthorized",
                                          "engine": "codex"})[0], i18n.t("core.failure.auth").replace("Claude", "Codex"))
        self.assertEqual(reducer.failure({"is_error": True, "text": "usage limit reached"})[0], i18n.t("core.failure.limit"))

    def test_auth_lost_is_not_a_turn_of_the_hub(self):
        """The stand 2026-10-29: the "locked out" line went to the hub session as a message — a turn of a session that has no
        sign-in itself (a 401 at once). The loss is kept in the state with the moment it began; the owner is told by the
        daemon (owner.notify, the Hub test below), not the hub."""
        self.assertIsNone(self.ev("codex.auth_lost", None, why="authentication required", ts=500))
        self.ev("codex.auth_lost", None, why="authentication required", ts=510)
        self.ev("turn.result", "maps", text="unexpected status 401", is_error=True, engine="codex", auth_lost=True, ts=520)
        self.assertEqual(self.to_hub(), [])
        self.assertEqual((self.st["codex"]["lost"]["since"], self.st["codex"]["lost"]["ts"]), (500, 520))
        self.assertEqual(reducer.codex_lost_text(self.st), i18n.t("core.reducer.codex_no_login", why="unexpected status 401"))
        self.assertIsNotNone(self.ev("codex.auth_lost", None, why="x", channel="session", sender="maps"))
        # sign-in is the owner's only; after it a new loss is a new one (its own beginning)
        self.assertIsNotNone(self.ev("codex.login", None, email="a@example.com", plan="prolite",
                                     channel="session", owner=False, sender="maps"))
        self.assertIsNone(self.ev("codex.login", None, email="a@example.com", plan="prolite", channel="cli", owner=True))
        self.assertEqual(self.st["codex"]["login"], {"email": "a@example.com", "plan": "prolite"})
        self.assertIsNone(self.st["codex"]["lost"])
        self.assertIsNone(reducer.codex_lost_text(self.st))
        self.ev("codex.auth_lost", None, why="refresh token was already used", ts=900)
        self.assertEqual(self.st["codex"]["lost"]["since"], 900)
        self.assertEqual(reducer.codex_lost_text(self.st), i18n.t("core.reducer.codex_login_lost",
                                                             why="refresh token was already used"))
        self.assertFalse([t for t in self.to_hub() if "let in" in t or "lets in" in t])   # "lets in again" — the hub has a sign-in then
        self.assertIsNone(self.ev("codex.logout", None, channel="web", owner=True))
        self.assertIsNone(self.st["codex"]["login"])


    def test_login_after_lost_is_not_a_turn_of_the_hub_either(self):
        """Pilot 2026-10-19: the "locked out" line stayed in the hub's feed after sign-in. A loss the owner was not told of (a
        blip) gives the hub no line on the sign-in either: the daemon clears the owner's notice (the Hub test below)."""
        self.ev("codex.auth_lost", None, why="authentication required")
        self.ev("codex.login", None, email="a@example.com", plan="prolite", channel="web", owner=True)
        self.assertFalse([t for t in self.to_hub() if "lets in" in t])
        self.assertIsNone(self.st["codex"]["lost"])

    def test_the_told_mark_survives_further_refusals(self):
        """The core's owner.notify about the loss marks it told in the state: a restart of the daemon does not tell again."""
        self.ev("codex.auth_lost", None, why="401", ts=500)
        self.ev("owner.notify", None, id=reducer.CODEX_NOTICE, title="Codex", text="locked out", channel="core", owner=False)
        self.ev("codex.auth_lost", None, why="401", ts=600)
        self.assertEqual((self.st["codex"]["lost"]["told"], self.st["codex"]["lost"]["since"]), (True, 500))
        self.ev("owner.notify", None, id=reducer.CODEX_NOTICE, title="x", text="x", source="maps")   # not the core's
        self.ev("codex.login", None, email="a@example.com", plan="prolite", channel="web", owner=True)
        # an outage the owner was told of: the hub, signed in again, may repeat what failed — one line, with the context
        (back,) = [t for t in self.to_hub() if "lets in" in t]
        self.assertIn("lets in again (the owner had a", back)
        self.ev("codex.auth_lost", None, why="401", ts=700)
        self.assertNotIn("told", self.st["codex"]["lost"])                    # a new loss is told again

    def test_model_spend_is_a_fact_for_usage(self):
        """Tokens of the core's Codex model (watcher, push, voice) are a codex.spent fact in the journal; the core writes it, not a session."""
        self.assertIsNone(self.ev("codex.spent", "codex:watch", model="gpt-6-luna", tokens_in=9600, tokens_out=40))
        self.assertIsNotNone(self.ev("codex.spent", "codex:watch", model="x", tokens_in=1, tokens_out=1, channel="session",
                                     sender="maps"))


class HostWithFake(unittest.TestCase):
    """A hub entirely on Codex with a fake app-server: limits when a session starts, a limit cut in the turn result."""

    def setUp(self):
        codex_limits.reset()
        self.state = tempfile.mkdtemp(prefix="hubcxl-")
        self.replies = os.path.join(self.state, "replies.json")

    def tearDown(self):
        self.hub.host.kill_server()
        time.sleep(0.2)
        platforms.current.rmtree(self.state)

    def start(self, replies):
        with open(self.replies, "w") as f:
            json.dump(replies, f)
        host = CodexHost(self.state, codex=(sys.executable, FAKE_CODEX), all_sessions=True, default_model="gpt-test",
                         env={"FAKE_TURN_S": "0.2", "FAKE_CODEX_REPLIES": self.replies})
        self.hub = Hub(self.state, host, registry_dir=os.path.join(self.state, "registry"))
        self.hub.start()
        spool.put(os.path.join(self.state, "spool"), "session.requested", "a", {"cwd": self.state, "session_id": "sid-a"})
        self.until(lambda: self.hub.state["sessions"].get("a", {}).get("state") == "idle")

    def until(self, pred, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end:
            self.hub.tick()
            if pred():
                return
            time.sleep(0.03)
        self.fail(f"did not wait; {self.hub.state['sessions']}")

    def events(self, type):
        return [e for e in journal.read(self.hub.journal.path) if e["type"] == type]

    def test_limits_on_start(self):
        self.start({"account/rateLimits/read": READ})
        self.until(lambda: self.events("account.limits"))
        self.assertEqual(self.hub.state["limits"]["windows"], {"seven_day": {"used": 0.07, "resets": 1792905741}})

    def test_no_login_seen_at_start(self):
        self.start({"account/rateLimits/read": NOAUTH})
        self.until(lambda: self.events("codex.auth_lost"))
        self.assertTrue(self.hub.state["codex"]["lost"])

    def test_no_login_is_told_to_the_owner_once_and_cleared_by_a_login(self):
        """Not a message to the hub session (it has no sign-in either — a turn with a 401): owner.notify of the core —
        the page's notices and Telegram; after the loss has held CODEX_TELL_AFTER; one per loss, even when the owner
        closed it; the sign-in clears it."""
        self.start({"account/rateLimits/read": NOAUTH})
        self.until(lambda: self.events("codex.auth_lost"))
        for _ in range(5):
            self.hub.tick()
        self.assertEqual(self.events("owner.notify"), [])                       # not yet: a one-off 401 is not told
        self.hub.CODEX_TELL_AFTER = 0
        self.until(lambda: self.events("owner.notify"))
        spool.put(os.path.join(self.state, "spool"), "codex.auth_lost", None, {"why": "401"})   # the loss goes on
        spool.put(os.path.join(self.state, "spool"), "owner.notify_resolved", None,              # the owner closed it
                  {"id": "n:core:codex-auth", "channel": "web", "owner": True})
        for _ in range(5):
            self.hub.tick()
        self.assertNotIn("n:core:codex-auth", self.hub.state["notices"])
        (told,) = self.events("owner.notify")
        self.assertEqual((told["data"]["id"], told["data"]["channel"]), ("codex-auth", "core"))
        self.assertIn(i18n.t("core.reducer.codex_no_login", why="").split(" (")[0], told["data"]["text"])
        self.assertFalse([m for m in self.hub.state["messages"].values() if "let in" in m["text"]])
        # a new loss after a sign-in is told again, and the sign-in after it clears the open notice
        spool.put(os.path.join(self.state, "spool"), "codex.login", None,
                  {"email": "a@example.com", "plan": "prolite", "channel": "cli", "owner": True})
        spool.put(os.path.join(self.state, "spool"), "codex.auth_lost", None, {"why": "refresh token was already used"})
        self.until(lambda: len(self.events("owner.notify")) == 2)
        self.assertEqual(self.events("owner.notify")[-1]["data"]["text"],
                         i18n.t("core.reducer.codex_login_lost", why="refresh token was already used"))
        spool.put(os.path.join(self.state, "spool"), "codex.login", None,
                  {"email": "a@example.com", "plan": "prolite", "channel": "cli", "owner": True})
        self.until(lambda: self.events("owner.notify_resolved") and "n:core:codex-auth" not in self.hub.state["notices"]
                   and len(self.events("owner.notify_resolved")) == 2)

    def test_turn_cut_by_usage_limit(self):
        failed = {"method": "turn/completed", "params": {"turn": {
            "id": "t-lim", "status": "failed", "durationMs": 5,
            "error": {"message": "You've hit your usage limit.", "codexErrorInfo": "usageLimitExceeded"}}}}
        self.start({"account/rateLimits/read": {"rateLimits": {"primary": {
                        "usedPercent": 100, "windowDurationMins": 10080, "resetsAt": 4102444800}},
                        "ordinaryUsageAllowed": False},
                    "turn/start": {"result": {"turn": {"id": "t-lim", "status": "inProgress", "items": []}},
                                   "notes": [{"method": "turn/started", "params": {"turn": {"id": "t-lim"}}}, failed]}})
        self.until(lambda: self.events("account.limits"))
        spool.put(os.path.join(self.state, "spool"), "message.received", None,
                  {"to": "a", "text": "do it", "channel": "web", "owner": True})
        self.until(lambda: self.events("turn.result"))
        res = self.events("turn.result")[-1]["data"]
        self.assertTrue(res["limit"] and res["is_error"])
        self.assertEqual(res["engine"], "codex")
        cut = self.hub.state["sessions"]["a"]["limit_cut"]
        self.assertEqual((cut["engine"], cut["resets"]), ("codex", 4102444800))


class RolledBackToClaude(unittest.TestCase):
    """The hub went back from "host": "codex" to claude (stand 2026-11-05): a sign-in loss of the Codex time no longer
    concerns it — the daemon tells the owner nothing ("Codex does not let in… sign in" came 2 min after the rollback),
    an open notice goes, the fold drops the loss."""

    def setUp(self):
        import types
        from unittest import mock
        self.tmp = tempfile.mkdtemp(prefix="hubcxr-")
        self.addCleanup(platforms.current.rmtree, self.tmp)
        conf = os.path.join(self.tmp, "hub.json")
        with open(conf, "w") as f:
            f.write("{}")
        with mock.patch.dict(os.environ, {"HUB_CONFIG": conf}):
            os.environ.pop("HUB_CODEX", None)
            self.h = Hub(os.path.join(self.tmp, "state"), types.SimpleNamespace())
        self.h.CODEX_TELL_AFTER = 0
        # what the Codex time left: the host's mark and a loss of the sign-in
        self.h.record("codex.host", None, {"on": True, "channel": "core", "owner": False})
        self.h.record("codex.auth_lost", None, {"why": "401 Unauthorized", "channel": "core"})

    def events(self, type):
        return [e for e in journal.read(self.h.journal.path) if e["type"] == type]

    def test_a_loss_left_from_the_codex_time_is_not_told(self):
        self.h.codex_tick()
        self.h.codex_tick()
        self.assertEqual(self.events("owner.notify"), [])
        self.assertIsNone(self.h.state["codex"]["lost"])
        self.assertFalse(self.h.state["codex"]["host"])

    def test_an_open_notice_goes(self):
        self.h.record("owner.notify", None, {"id": reducer.CODEX_NOTICE, "title": "Codex", "text": "x",
                                             "channel": "core", "owner": False})
        self.assertIn(f"n:core:{reducer.CODEX_NOTICE}", self.h.state["notices"])
        self.h.codex_tick()
        self.h.codex_tick()
        self.assertNotIn(f"n:core:{reducer.CODEX_NOTICE}", self.h.state["notices"])
        self.assertEqual(len(self.events("owner.notify")), 1)
        self.assertEqual(len(self.events("owner.notify_resolved")), 1)

    def test_a_loss_left_after_the_host_mark_was_already_off(self):
        """State written by a core before this fix: codex.host off already, the loss still there."""
        self.h.record("codex.host", None, {"on": False, "channel": "core", "owner": False})
        self.h.state["codex"]["lost"] = {"ts": 1, "since": 1, "why": "401"}
        self.h.codex_tick()
        self.assertEqual(self.events("owner.notify"), [])
        self.assertIsNone(self.h.state["codex"]["lost"])


class CodexExecutorCut(unittest.TestCase):
    """An executor whose turn the Codex limit cut, asleep after handing in (retired): woken with its task after the reset,
    in Codex words — as a Claude cut is by core/limitresume (reducer._wake_cut)."""

    def setUp(self):
        from tests.test_limit_reset_resume import Folding
        self.f = Folding()
        self.f.setUp()
        owner = {"channel": "cli", "owner": True}
        self.f.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **owner)
        self.f.ev("session.started", "hub")
        self.f.ev("session.requested", "maps", cwd="/m", session_id="s-m", role="agent", about="Maps", **owner)
        self.f.ev("session.started", "maps")

    def test_a_retired_executor_cut_by_the_codex_limit_is_woken_with_its_task(self):
        from core import executors
        f = self.f
        self.assertIsNone(f.ev("task.run_requested", "maps-a", task="make a report", channel="session", owner=False,
                               sender="maps"))
        for t, e, d in executors.events_for(f.st, "maps-a", f.st["runs"]["maps-a"], cwd="/x", worktree=None):
            self.assertIsNone(f.ev(t, e, **d))
        f.ev("session.started", "maps-a")
        (d,) = [d for d in f.st["delegations"].values() if d["session"] == "maps-a"]
        f.ev("turn.started", "maps-a", marker=d["message"])
        f.ev("turn.result", "maps-a", text="Codex: usage limit reached", is_error=True, engine="codex", limit=True,
             limit_resets=5000)
        f.ev("turn.stopped", "maps-a")
        self.assertIsNone(f.ev("session.close_requested", "maps-a", retire=True))
        self.assertEqual(f.st["sessions"]["maps-a"]["state"], "retired")
        self.assertTrue(reducer.codex_due(f.st, 5000))
        self.assertIsNone(f.ev("codex.limit_reset", None, ts=5001, channel="core"))
        x = f.st["sessions"]["maps-a"]
        self.assertEqual(x["state"], "starting")
        self.assertNotIn("limit_cut", x)
        (go,) = [m for m in f.st["messages"].values() if m["session"] == "maps-a" and "window has reset" in m["text"]]
        self.assertTrue(go["text"].startswith("The Codex limit cut off your turn"), go["text"])
        self.assertEqual(go.get("for_delegation"), d["id"])
        self.assertFalse(reducer.codex_due(f.st, 6000))


if __name__ == "__main__":
    unittest.main()
