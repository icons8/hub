"""Client push notifications: only requests to the owner, each one once; old ones are not sent out at startup."""
import tests  # noqa: F401 — do not see the machine's settings (~/.config/hub-core), see tests/__init__.py
import os
import tempfile
import unittest

from core import owner, reducer, views
from modules.web import push

TOKEN = "ab" * 32
PHONE = "cd" * 32   # can clear a banner (caps clear)
CFG = {"push": {"enabled": True, "key_file": "/k.p8", "key_id": "K", "team_id": "T", "topic": "app.x"}}
T0 = 1_000_000.0


def ask(r, since=T0, card=None):
    return {"request_id": r, "tool": "AskUserQuestion", "input": {"questions": [{"question": f"Question {r}?"}]},
            "since": since, **({"id": card} if card else {})}


def state(*pending, requests=(), notices=(), reminders=None):
    st = reducer.empty()
    st["sessions"]["maps"] = {"name": "maps", "state": "idle", "since": 1,
                              "pending": [p if isinstance(p, dict) else ask(p) for p in pending],
                              "requests": list(requests)}
    st["owner"]["reminders"] = dict(reminders or {})
    for n in notices:
        st.setdefault("notices", {})[n["id"]] = {"sender": "mod:shop", "title": "", "text": "disk", "urgent": False,
                                                 "since": T0, "at": T0, "v": 5, **n}
    return st


def snap(*pending, now=T0, **kw):
    """What the push module takes from the page (Web.push_source): core triggers (owner.alerts) and snapshot sessions.
    """
    r = views.Reader()
    r.state = state(*pending, **kw)
    return {"alerts": owner.alerts(r.state, now), "sessions": r.snapshot(now=now)["sessions"]}


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.snap = snap()
        self.cfg = CFG
        self.calls = []
        self.code = 200
        self.logged = []
        self.p = self.push()

    def push(self):
        return push.Push(self.d, lambda: self.snap, lambda: self.cfg, self.sender,
                         log=lambda *a, **k: self.logged.append(a))

    def sender(self, cfg, token, payload, host, head):
        self.calls.append((token, payload, head))
        return self.code, "BadDeviceToken" if self.code == 400 else ""

    def step(self, now, p=None):
        """Pass and send whatever is due by the time now → how many APNs accepted."""
        p = p or self.p
        p.tick(now)
        return p.flush(now)


class Notify(Base):
    def test_only_new_requests_once_and_old_ones_are_not_sent_at_start(self):
        self.snap = snap("r0")
        self.p.register(TOKEN)
        self.assertEqual(self.step(T0), 0)            # it was there before startup — stay silent
        self.snap = snap("r0", "r1")
        self.assertEqual(self.step(T0 + 3), 1)
        self.assertEqual(self.step(T0 + 6), 0)        # the same question — not a second time
        t, pl, _ = self.calls[0]
        # topic as the title, session name as the second line (spec decision R9), a tap leads to the session
        self.assertEqual(pl["aps"]["alert"], {"title": "Question r1?", "subtitle": "maps", "body": "Question r1?"})
        self.assertEqual(pl["session"], "maps")

    def test_a_banner_has_a_collapse_id_from_the_hash_and_the_hub_part(self):
        self.p.register(TOKEN)
        self.step(T0)
        self.snap = snap(ask("r1", card="q7"))
        self.step(T0 + 3)
        (_, pl, head), = self.calls
        key = pl["hub"]["key"]
        self.assertRegex(key, r"^h[0-9a-f]{16}$")
        self.assertEqual(head, {"apns-push-type": "alert", "apns-priority": "10", "apns-collapse-id": key,
                                "apns-expiration": str(int(T0 + 3 + push.EXPIRE))})
        self.assertNotIn("maps", key)
        self.assertEqual((pl["aps"]["mutable-content"], pl["aps"]["thread-id"], pl["aps"]["sound"]), (1, "maps", "default"))
        self.assertEqual({k: pl["hub"][k] for k in ("v", "hub", "kind", "card", "open", "closed")},
                         {"v": 1, "hub": self.p.data["hub"], "kind": "question", "card": "q7", "open": [key],
                          "closed": []})
        # another hub — another key for the same trigger: two hubs in one app do not remove each other's banners
        other = push.Push(tempfile.mkdtemp(), lambda: self.snap, lambda: self.cfg, self.sender)
        self.assertNotEqual(other._key("q:maps:r1"), key)

    def test_a_reminder_replaces_the_banner_with_the_same_key(self):
        self.p.register(TOKEN)
        self.step(T0)
        self.snap = snap("r1")
        self.step(T0 + 3)
        self.snap = snap("r1", reminders={"q:maps:r1": 1})
        self.assertEqual(self.step(T0 + 1800), 1)
        first, again = self.calls
        self.assertEqual(first[2]["apns-collapse-id"], again[2]["apns-collapse-id"])
        self.assertEqual(again[1]["hub"]["n"], 1)
        self.assertGreater(again[1]["hub"]["rev"], first[1]["hub"]["rev"])

    def test_closed_goes_by_a_silent_push_only_to_devices_that_can_clear_and_at_most_every_20_minutes(self):
        self.p.register(TOKEN)
        self.p.register(PHONE)
        self.p.data["tokens"][PHONE]["caps"] = ["clear"]
        self.snap = snap("r1", "r2", "r3")
        self.step(T0)
        keys = {i: self.p._key(f"q:maps:{i}") for i in ("r1", "r2", "r3")}
        self.snap = snap("r2", "r3")                 # r1 was answered on the computer
        self.assertEqual(self.step(T0 + 3), 1)
        (token, pl, head), = self.calls
        self.assertEqual(token, PHONE)
        self.assertEqual(head, {"apns-push-type": "background", "apns-priority": "5", "apns-expiration": "0"})
        self.assertEqual(pl["aps"], {"content-available": 1})
        self.assertEqual((pl["hub"]["closed"], sorted(pl["hub"]["open"])), ([keys["r1"]], sorted([keys["r2"], keys["r3"]])))
        self.snap = snap("r3")                       # a minute later another one is closed — waits 20 min
        self.assertEqual(self.step(T0 + 60), 0)
        self.assertEqual(self.step(T0 + 3 + push.SILENT_EVERY), 1)
        self.assertEqual(self.calls[-1][1]["hub"]["closed"], [keys["r2"]])   # what was sent silently — do not repeat

    def test_a_banner_also_carries_what_was_closed(self):
        self.p.register(TOKEN)
        self.snap = snap("r1")
        self.step(T0)
        self.snap = snap("r2")
        self.step(T0 + 3)
        (_, pl, _), = self.calls
        self.assertEqual(pl["hub"]["closed"], [self.p._key("q:maps:r1")])

    def test_tool_connection_request_is_a_request_too(self):
        self.p.register(TOKEN)
        self.step(T0)
        self.snap = snap(requests=[{"state": "needs-owner", "tool": "browser:lobo", "why": "check the login",
                                    "since": T0}])
        self.assertEqual(self.step(T0 + 3), 1)
        self.assertEqual(self.calls[0][1]["aps"]["alert"]["title"], "Connect browser:lobo?")

    def test_nothing_is_sent_without_key_or_token_and_the_backlog_is_not_replayed_later(self):
        self.step(T0)
        self.snap = snap("r1")
        self.assertEqual(self.step(T0 + 3), 0)        # no token
        self.p.register(TOKEN)
        self.assertEqual(self.step(T0 + 6), 0)        # r1 has already "passed" — do not send retroactively
        self.assertEqual(self.calls, [])

    def test_a_dead_token_is_dropped_and_tokens_survive_restart(self):
        self.p.register(TOKEN)
        self.step(T0)
        self.assertTrue(os.path.exists(os.path.join(self.d, "push.json")))
        if os.name != "nt":   # POSIX permissions; on Windows — the user folder's permissions
            self.assertEqual(os.stat(os.path.join(self.d, "push.json")).st_mode & 0o777, 0o600)   # tokens — owner only
        again = self.push()
        self.assertIn(TOKEN, again.data["tokens"])
        self.assertEqual(again.data["hub"], self.p.data["hub"])
        self.step(T0 + 3, again)
        self.code = 400
        self.snap = snap("r2", now=T0 + 6)
        self.assertEqual(self.step(T0 + 6, again), 0)
        self.assertNotIn(TOKEN, again.data["tokens"])

    def test_off_by_default_even_with_a_key_and_a_token_and_nothing_is_read(self):
        off = {"push": {**CFG["push"], "enabled": False}}
        for cfg in ({}, off, {"push": {k: v for k, v in CFG["push"].items() if k != "enabled"}}):
            self.cfg = cfg
            self.p.register(TOKEN)
            self.snap = None                          # the snapshot must not even be read
            self.assertEqual(self.step(T0), 0)
        self.assertEqual(self.calls, [])

    def test_a_question_asked_while_off_is_not_replayed_when_switched_on(self):
        self.p.register(TOKEN)
        self.step(T0)                                 # it was on: the "last pass" is recorded
        self.cfg = {}
        self.snap = snap("r1", now=T0 + 5)
        self.step(T0 + 5)
        self.cfg = CFG
        self.assertEqual(self.step(T0 + 9), 0)        # switched on: the first pass remembers what is already open
        self.assertEqual(self.calls, [])

    def test_the_number_of_devices_is_capped_and_the_oldest_go_first(self):
        for i in range(push.MAX_TOKENS + 5):
            self.p.register(f"{i:02x}" * 16)
        self.assertEqual(len(self.p.data["tokens"]), push.MAX_TOKENS)
        self.assertNotIn("00" * 16, self.p.data["tokens"])
        self.assertIn(f"{push.MAX_TOKENS + 4:02x}" * 16, self.p.data["tokens"])
        self.assertEqual(push.device_count(self.d), push.MAX_TOKENS)

    def test_mode_none_silences_the_device_and_other_levels_get_the_questions(self):
        quiet, loud = "cd" * 32, "ef" * 32
        self.p.register(TOKEN, mode="none")
        self.p.register(quiet, mode="asks")
        self.p.register(loud, mode="all")
        self.step(T0)
        self.snap = snap("r1")
        self.assertEqual(self.step(T0 + 3), 2)
        self.assertEqual(sorted(t for t, _, _ in self.calls), sorted([quiet, loud]))
        again = self.push()   # the level survived a restart
        self.assertEqual(again.data["tokens"][TOKEN]["mode"], "none")
        self.p.register(TOKEN, mode="results")   # changed the level — same token, new level
        self.calls = []
        self.snap = snap("r1", "r2")
        self.assertEqual(self.step(T0 + 6), 3)
        self.assertIn(TOKEN, [t for t, _, _ in self.calls])

    def test_notices_go_to_all_and_urgent_ones_to_asks_too(self):
        asks, every = "cd" * 32, "ef" * 32
        self.p.register(asks, mode="asks")
        self.p.register(every, mode="all")
        self.step(T0)
        self.snap = snap(notices=[{"id": "n:mod:shop:a", "text": "disk full"},
                                  {"id": "n:mod:shop:b", "text": "gate", "urgent": True}])
        self.step(T0 + 3)
        got = sorted((t, pl["aps"]["alert"]["body"]) for t, pl, _ in self.calls)
        self.assertEqual(got, sorted([(every, "disk full"), (every, "gate"), (asks, "gate")]))
        self.assertTrue(all(pl["hub"]["kind"] == "notice" and "session" not in pl for _, pl, _ in self.calls))

    def test_unknown_or_missing_mode_means_questions_and_a_bare_reregister_keeps_the_level(self):
        self.p.register(TOKEN)
        self.assertEqual(self.p.data["tokens"][TOKEN]["mode"], "asks")
        for mode in ("everything", 5):
            self.p.register(TOKEN, mode=mode)
            self.assertEqual(self.p.data["tokens"][TOKEN]["mode"], "asks", mode)
        self.p.register(TOKEN, mode="none")
        self.p.register(TOKEN, env="sandbox")
        self.assertEqual(self.p.data["tokens"][TOKEN], {**self.p.data["tokens"][TOKEN], "mode": "none", "env": "sandbox"})

    def test_bad_token_is_refused(self):
        self.assertFalse(self.p.register("not-a-token"))

    def test_es256_signature_is_raw_64_bytes(self):
        der = bytes([0x30, 0x44, 0x02, 0x20]) + b"\x01" * 32 + bytes([0x02, 0x20]) + b"\x02" * 32
        self.assertEqual(push._der_to_raw(der), b"\x01" * 32 + b"\x02" * 32)
        long = bytes([0x30, 0x45, 0x02, 0x21, 0x00]) + b"\x81" * 32 + bytes([0x02, 0x20]) + b"\x02" * 32
        self.assertEqual(push._der_to_raw(long), b"\x81" * 32 + b"\x02" * 32)


class Retry(Base):
    def setUp(self):
        super().setUp()
        self.p.register(TOKEN)
        self.step(T0)
        self.snap = snap("r1")

    def test_network_failure_is_retried_after_3_30_300_seconds_without_sound_then_given_up(self):
        self.code = 0
        self.step(T0 + 1)
        for wait in push.RETRY:
            at = self.calls[-1]
            self.assertEqual(self.p.flush(T0 + 1 + sum(push.RETRY[:push.RETRY.index(wait)]) + wait - 0.5), 0)
            self.assertIs(self.calls[-1], at)       # before the deadline — do not send
            self.p.flush(T0 + 1 + sum(push.RETRY[:push.RETRY.index(wait) + 1]))
        self.assertEqual(len(self.calls), 1 + len(push.RETRY))
        self.assertIn("sound", self.calls[0][1]["aps"])
        self.assertTrue(all("sound" not in c[1]["aps"] for c in self.calls[1:]))   # the repeat makes no sound again
        self.assertEqual(self.p.queue, [])

    def test_a_retry_is_dropped_once_the_question_is_closed(self):
        self.code = 0
        self.step(T0 + 1)
        self.snap = snap()
        self.p.tick(T0 + 2)
        self.code = 200
        self.assertEqual(self.p.flush(T0 + 10), 0)
        self.assertEqual(len(self.calls), 1)

    def test_429_pauses_the_device_for_an_hour_and_the_banners_come_after_it(self):
        self.code = 429
        self.step(T0 + 1)
        self.code = 200
        self.assertEqual(self.p.flush(T0 + 10), 0)             # on pause
        self.snap = snap("r1", "r2")
        self.assertEqual(self.step(T0 + 20), 0)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.step(T0 + 1 + push.PAUSE_429 + 1), 2)   # r1 and r2 were not lost
        self.assertEqual(sorted(c[1]["aps"]["alert"]["body"] for c in self.calls[1:]), ["Question r1?", "Question r2?"])
        self.code = 503
        self.snap = snap("r1", "r2", "r4")
        self.step(T0 + 2 * push.PAUSE_429)
        self.assertEqual(len([j for j in self.p.queue if j["attempt"] == 1]), 1)

    def test_403_drops_the_cached_provider_token(self):
        push._jwt[("k",)] = ("old", T0)
        self.code = 403
        self.step(T0 + 1)
        self.assertEqual(push._jwt, {})

    def test_403_is_logged_at_most_once_an_hour(self):
        self.code = 403
        self.step(T0 + 1)
        self.snap = snap("r1", "r2")
        self.step(T0 + 5)
        self.assertEqual(len(self.logged), 1)
        self.snap = snap("r1", "r2", "r3")
        self.step(T0 + 1 + push.LOG_403)
        self.assertEqual(len(self.logged), 2)

    def test_a_hanging_apns_does_not_hold_up_the_pass(self):
        # curl hangs (network) — the pass still sees the closing and queues the new one
        import threading
        hold, entered = threading.Event(), threading.Event()

        def slow(*a):
            entered.set()
            hold.wait(10)
            return 200, ""
        self.p.sender = slow
        self.p.tick(T0 + 1)
        th = threading.Thread(target=self.p.flush, args=(T0 + 1,), daemon=True)
        th.start()
        self.assertTrue(entered.wait(5))
        self.snap = snap("r2")
        self.assertEqual(self.p.tick(T0 + 4), 1)                 # does not wait for the send
        self.assertNotIn("q:maps:r1", self.p.data["sent"])
        hold.set()
        th.join(5)


class Restart(Base):
    def test_closed_while_down_is_cleared_and_new_after_the_last_pass_comes_as_a_banner(self):
        self.p.register(TOKEN)
        self.p.register(PHONE, caps=["clear"])
        self.snap = snap("r1")
        self.step(T0)
        self.step(T0 + 3)
        # the page was down for 2 minutes: r1 answered, r2 asked a minute before the respawn, r0 long ago (before the
        # previous pass)
        self.snap = snap(ask("r0", since=T0 - 3600), ask("r2", since=T0 + 60))
        again = self.push()
        self.assertEqual(self.step(T0 + 120, again), 3)   # r2 banner to two devices and a silent PHONE
        bodies = sorted((t, pl["aps"].get("alert", {}).get("body", ""), tuple(pl["hub"]["closed"])) for t, pl, _ in self.calls)
        k1 = again._key("q:maps:r1")
        self.assertEqual(bodies, sorted([(TOKEN, "Question r2?", (k1,)), (PHONE, "Question r2?", (k1,)), (PHONE, "", (k1,))]))

    def test_older_than_ten_minutes_after_restart_is_only_remembered(self):
        self.p.register(TOKEN)
        self.step(T0)
        self.snap = snap(ask("r2", since=T0 + 60))
        self.assertEqual(self.step(T0 + 60 + push.FRESH + 1, self.push()), 0)

    def test_the_old_sent_format_is_read(self):
        import json
        with open(os.path.join(self.d, "push.json"), "w") as f:
            json.dump({"tokens": {TOKEN: {"env": "production", "at": 1, "mode": "asks"}}, "sent": {"q:maps:r1": T0}}, f)
        p = self.push()
        self.assertEqual(p.data["sent"]["q:maps:r1"], {"key": p._key("q:maps:r1"), "at": T0, "v": 0})
        self.snap = snap("r1")
        self.assertEqual(self.step(T0 + 3, p), 0)

    def test_the_previous_release_reads_the_new_file(self):
        # rollback: the earlier push.py (tag v1.1.0) reads the new version's push.json and works (extra fields do not
        # bother it)
        import importlib.util
        import subprocess
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        r = subprocess.run(["git", "-C", repo, "show", "v1.1.0:modules/web/push.py"], capture_output=True, text=True)
        if r.returncode:
            self.skipTest("no tag v1.1.0 (not git)")
        path = os.path.join(tempfile.mkdtemp(), "push_old.py")
        with open(path, "w") as f:
            f.write(r.stdout)
        spec = importlib.util.spec_from_file_location("push_old", path)
        old = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(old)
        self.p.register(TOKEN, caps=["clear"])
        self.snap = snap("r1")
        self.step(T0)
        calls = []
        o = old.Push(self.d, lambda: {"sessions": [{"name": "maps", "pending": [ask("r1"), ask("r2")]}]},
                     lambda: self.cfg, lambda c, t, pl, h: calls.append(pl) or (200, ""))
        self.assertIn(TOKEN, o.data["tokens"])
        self.assertEqual(o.tick(), 0)
        self.assertEqual(o.tick(), 0)            # r1 was sent by the new version, r2 is "old" to the earlier one
        o.data["sent"].pop("q:maps:r2")
        self.assertEqual(o.tick(), 1)
        self.assertEqual(self.push().data["tokens"][TOKEN]["mode"], "asks")   # and the new one reads the earlier file


class Payload(Base):
    def test_sixty_open_and_forty_closed_fit_and_more_open_drop_the_list(self):
        import json
        self.p.register(TOKEN)
        meta = self.p.data["tokens"][TOKEN]
        meta["closed"] = [self.p._key(f"q:maps:c{i}") for i in range(push.CLOSED_MAX)]
        long = {"id": "q:" + "x" * 300, "kind": "question", "session": "s" * 64, "card": "q123456", "title": "t" * 40,
                "subtitle": "s" * 64, "body": "é" * push.BODY, "since": T0, "v": 3}
        self.p.open = {f"q:maps:r{i}": {"key": self.p._key(f"q:maps:r{i}")} for i in range(push.OPEN_MAX - 1)}
        self.p.open[long["id"]] = {**long, "key": self.p._key(long["id"])}
        pl = self.p.payload({"kind": "alert", "alert": self.p.open[long["id"]], "attempt": 0}, meta)
        self.assertLessEqual(len(json.dumps(pl, ensure_ascii=False).encode()), push.PAYLOAD_MAX)
        self.assertEqual((len(pl["hub"]["open"]), len(pl["hub"]["closed"])), (push.OPEN_MAX, push.CLOSED_MAX))
        self.p.open["q:maps:extra"] = {"key": self.p._key("q:maps:extra")}
        pl = self.p.payload({"kind": "background", "attempt": 0}, meta)
        self.assertNotIn("open", pl["hub"])        # 61 open — the list is incomplete, do not send
        self.assertEqual(len(pl["hub"]["closed"]), push.CLOSED_MAX)

    def test_an_oversized_payload_drops_open_then_shortens_closed(self):
        import json
        self.p.register(TOKEN)
        meta = self.p.data["tokens"][TOKEN]
        meta["closed"] = ["h" + "0" * 200 + str(i) for i in range(push.CLOSED_MAX)]   # deliberately long
        self.p.open = {f"q:{i}": {"key": "h" + "1" * 60 + str(i)} for i in range(push.OPEN_MAX)}
        pl = self.p.payload({"kind": "background", "attempt": 0}, meta)
        self.assertLessEqual(len(json.dumps(pl, ensure_ascii=False).encode()), push.PAYLOAD_MAX)
        self.assertNotIn("open", pl["hub"])
        self.assertLess(len(pl["hub"]["closed"]), push.CLOSED_MAX)
        self.assertEqual(pl["hub"]["closed"][-1], meta["closed"][-1])   # the last ones remain

    def test_a_notice_carries_its_sender_and_a_question_does_not(self):
        self.p.register(TOKEN, mode="all")
        self.step(T0)
        self.snap = snap("r1", notices=[{"id": "n:core:a", "sender": "core", "text": "update"},
                                        {"id": "n:mod:shop:a", "text": "disk"}])
        self.step(T0 + 3)
        got = {pl["hub"]["kind"] + ":" + pl["aps"]["alert"]["body"]: pl["hub"].get("sender") for _, pl, _ in self.calls}
        self.assertEqual(got["notice:update"], "core")    # the client shows core/watch notices as a quiet banner
        self.assertEqual(got["notice:disk"], "mod:shop")
        self.assertEqual([v for k, v in got.items() if not k.startswith("notice:")], [None])   # a question — as before


class Batches(Base):
    """Batches and the rate limit (§2.3): more than 3 new triggers per pass — 2 banners and a third "N more"; notices
    from one sender — at most 6 banners an hour, then one replaceable "<sender>: N more" (at most once per 10 min);
    per device — at most 30 banners an hour from all senders together."""

    def setUp(self):
        super().setUp()
        self.p.register(TOKEN)
        self.step(T0)

    def banners(self):
        return [pl for _, pl, h in self.calls if h["apns-push-type"] == "alert"]

    def test_more_than_three_new_at_once_is_two_banners_and_a_batch(self):
        self.snap = snap("r1", "r2", "r3", "r4", "r5")
        self.assertEqual(self.step(T0 + 3), 3)
        a, b, batch = self.banners()
        self.assertEqual([a["aps"]["alert"]["body"], b["aps"]["alert"]["body"]], ["Question r1?", "Question r2?"])
        self.assertEqual(batch["hub"]["kind"], "batch")
        self.assertIn("3", batch["aps"]["alert"]["title"])
        self.assertIn(batch["hub"]["key"], batch["hub"]["open"])
        view = self.p.view()
        self.assertIn("batch", [x["kind"] for x in view["alerts"]])
        # another batch while the old one is open — the same key (replaces the banner, at most once per 10 min)
        self.snap = snap("r1", "r2", "r3", "r4", "r5", "r6", "r7", "r8", "r9")
        self.step(T0 + 6)
        self.assertEqual(self.banners()[-1]["hub"]["key"], self.p._key("q:maps:r7"))   # new batch: 2 banners
        self.step(T0 + 3 + push.BATCH_EVERY)
        again = self.banners()[-1]
        self.assertEqual((again["hub"]["kind"], again["hub"]["key"]), ("batch", batch["hub"]["key"]))
        self.assertIn("5", again["aps"]["alert"]["title"])   # three earlier ones and two from the new batch
        # a batch is closed when all its triggers are closed
        self.snap = snap("r1", "r2", "r6", "r7", "r9")
        self.p.tick(T0 + 9 + push.BATCH_EVERY)
        self.assertIn(batch["hub"]["key"], [x["key"] for x in self.p.view()["alerts"]])
        self.snap = snap("r1", "r2")
        self.p.tick(T0 + 12 + push.BATCH_EVERY)
        self.assertEqual({x["kind"] for x in self.p.view()["alerts"]}, {"question"})
        self.assertIn(batch["hub"]["key"], self.p.data["tokens"][TOKEN]["closed"])

    def test_what_piled_up_after_hours_is_one_banner_in_the_morning(self):
        # hub.json quiet_review: cards held for the working morning come out together — one push for the batch
        held = [{**ask(r), "held_until": T0 + 2} for r in ("r1", "r2", "r3")]
        self.snap = snap(*held, now=T0 + 1)
        self.assertEqual(self.step(T0 + 1), 0)                           # still non-working time
        self.snap = snap(*held, now=T0 + 3)
        self.assertEqual(self.step(T0 + 3), 1)
        (batch,) = self.banners()
        self.assertEqual(batch["hub"]["kind"], "batch")
        self.assertIn("3", batch["aps"]["alert"]["title"])
        self.d, self.snap = tempfile.mkdtemp(), snap()   # one alone is an ordinary banner
        self.p, self.calls = self.push(), []
        self.p.register(TOKEN)
        self.step(T0)
        self.snap = snap({**ask("r1"), "held_until": T0 + 2}, now=T0 + 3)
        self.step(T0 + 3)
        self.assertEqual([pl["hub"]["kind"] for pl in self.banners()], ["question"])

    def test_a_noisy_sender_gets_six_banners_an_hour_then_one_replaced_batch(self):
        self.p.register(TOKEN, mode="all")
        notices = []
        for i in range(9):
            notices.append({"id": f"n:mod:shop:{i}", "text": f"event {i}", "since": T0 + 60 * i})
            self.snap = snap(notices=notices)
            self.step(T0 + 60 * i + 1)
        kinds = [pl["hub"]["kind"] for pl in self.banners()]
        self.assertEqual(kinds, ["notice"] * 6 + ["batch"])          # 7th — a batch, 8th and 9th — at most every 10 min
        self.assertTrue(self.banners()[-1]["aps"]["alert"]["title"].startswith("shop: "))
        notices.append({"id": "n:mod:shop:9", "text": "more", "since": T0 + 60 * 20})
        self.snap = snap(notices=notices)
        self.step(T0 + 6 * 60 + 1 + push.BATCH_EVERY)
        last = self.banners()[-1]
        self.assertEqual(last["hub"]["kind"], "batch")
        self.assertIn("4", last["aps"]["alert"]["title"])
        self.assertEqual(len(self.banners()), 8)

    def test_a_device_gets_at_most_thirty_banners_an_hour(self):
        for i in range(push.DEVICE_HOUR + 3):
            self.snap = snap(*[f"r{j}" for j in range(i + 1)])
            self.step(T0 + 10 + i)
        kinds = [pl["hub"]["kind"] for pl in self.banners()]
        self.assertEqual(kinds.count("question"), push.DEVICE_HOUR)
        self.assertEqual(kinds[push.DEVICE_HOUR], "batch")


class Summary(Base):
    """Summarizer (R8): off by default; when on — the topic and the decision come from the model, the push waits up to
    4 s, a reply with a number that is not in the question is a fallback path; a late reply becomes the reminder's
    topic. Tests never call a live model."""
    Q = ("Shop A — 830 999 and delivery 47 228, shop B — 818 885, arrives 28.10. Which basket to place?")

    def setUp(self):
        super().setUp()
        self.asked, self.later, self.accounts = [], [], []
        self.answer = {"topic": "Equipment — where to buy", "decision": "Shop B is cheaper, arrives 28.10"}
        self.p = push.Push(self.d, lambda: self.snap, lambda: self.cfg, self.sender, summarize=self.model,
                           spawn=lambda f: f())
        self.p.register(TOKEN)
        self.step(T0)

    def model(self, text, lang, account=None):
        self.asked.append(text)
        self.accounts.append(account)
        return self.answer

    def question(self, **kw):
        return {"request_id": "r1", "tool": "AskUserQuestion", "since": T0,
                "input": {"questions": [{"question": self.Q, "header": "Summary"}]}, **kw}

    def test_off_by_default(self):
        self.snap = snap(self.question())
        self.step(T0 + 3)
        self.assertEqual(self.asked, [])
        self.assertEqual(self.calls[0][1]["aps"]["alert"]["title"], "Shop A — 830 999 and delivery 47 228…")

    def test_on_the_topic_and_decision_come_from_the_model(self):
        self.cfg = {"push": {**CFG["push"], "summarize": True}}
        self.snap = snap(self.question())
        self.step(T0 + 3)
        self.assertEqual(self.asked, [self.Q])
        self.assertEqual(self.calls[0][1]["aps"]["alert"],
                         {"title": "Equipment — where to buy", "subtitle": "maps",
                          "body": "Shop B is cheaper, arrives 28.10"})

    def test_the_model_runs_on_the_pool_account_of_the_snapshot(self):
        """2026-10-20: the page process is not a watcher, the pool account comes from the core snapshot (otherwise the
        machine login)."""
        self.cfg = {"push": {**CFG["push"], "summarize": True}}
        self.snap = {**snap(self.question()), "account": {"active": "work"}}
        self.step(T0 + 3)
        self.assertEqual(self.accounts, ["work"])

    def test_the_page_gives_the_pool_account_to_push(self):
        from modules.web.server import Web
        web = Web(self.d)
        web.tail.snapshot = {**web.tail.snapshot, "account": {"active": "work", "exhausted": None}}
        self.assertEqual(web.push_source()["account"], {"active": "work", "exhausted": None})

    def test_a_made_up_number_falls_back(self):
        self.cfg = {"push": {**CFG["push"], "summarize": True}}
        self.answer = {"topic": "Basket", "decision": "Place it by 20.10, or the price will rise"}
        self.snap = snap(self.question())
        self.step(T0 + 3)
        self.assertNotEqual(self.calls[0][1]["aps"]["alert"]["title"], "Basket")

    def test_a_slow_model_waits_four_seconds_then_the_fallback_and_the_topic_stays_for_the_reminder(self):
        self.cfg = {"push": {**CFG["push"], "summarize": True}}
        self.p.spawn = self.later.append                     # the model is still thinking
        self.snap = snap(self.question())
        self.p.tick(T0 + 3)
        self.assertEqual(self.p.flush(T0 + 5), 0)            # waiting for the model's reply
        self.assertEqual(self.p.flush(T0 + 3 + push.SUMMARY_WAIT), 1)
        self.assertNotEqual(self.calls[0][1]["aps"]["alert"]["title"], "Equipment — where to buy")
        self.later[0]()                                      # the reply came late
        self.snap = snap(self.question(), reminders={"q:maps:r1": 1})
        self.step(T0 + 1800)
        self.assertEqual(self.calls[-1][1]["aps"]["alert"]["title"], "Equipment — where to buy")
        self.assertTrue(self.calls[-1][1]["aps"]["alert"]["body"].startswith("Waiting"))

    def test_without_a_live_model_it_is_the_fallback(self):
        import os
        self.assertTrue(os.environ.get("HUB_NO_MODEL"))
        with self.assertRaises(ValueError):
            push.summarize_model("x", "ru")


class Secrets(Base):
    def test_tokens_and_the_key_stay_out_of_the_snapshot(self):
        import json
        from modules.web.server import Web
        web = Web(self.d)
        web.config = lambda: {"push": {**CFG["push"], "key_file": "/secret/AuthKey_K.p8"}}
        web.push.register(TOKEN, caps=["clear"])
        r = views.Reader()
        r.state = state("r1")
        base = r.snapshot(now=T0)
        web.push.snapshot = lambda: snap("r1")
        web.push.tick(T0)
        out = json.dumps(web.with_clock(base), ensure_ascii=False)
        self.assertNotIn(TOKEN, out)
        self.assertNotIn("AuthKey", out)
        snap_out = web.with_clock(base)
        self.assertEqual([a["key"] for a in snap_out["alerts"]], [web.push._key("q:maps:r1")])
        self.assertIsInstance(snap_out["alerts_rev"], int)


class ProviderToken(unittest.TestCase):
    def test_the_jwt_is_signed_once_per_half_hour(self):
        # APNs answers 429 TooManyProviderTokenUpdates if the provider key changes more often than once per 20 min
        from unittest import mock
        push._jwt.clear()
        with mock.patch.object(push, "jwt", side_effect=lambda *a, now=None: f"jwt-{now}") as signed:
            first = push.provider_token(CFG["push"], now=T0)
            self.assertEqual(push.provider_token(CFG["push"], now=T0 + 600), first)
            self.assertNotEqual(push.provider_token(CFG["push"], now=T0 + push.JWT_EVERY), first)
        self.assertEqual(signed.call_count, 2)
        push._jwt.clear()


class Curl(unittest.TestCase):
    """The APNs key (JWT) is not visible in the process list: it used to go into curl's argv (`-H authorization:
    bearer …`), and argv is read by any process on the machine (ps). Now headers, body and address go as a curl config
    on stdin. Checked with a real curl against a local server."""

    def test_the_bearer_token_is_not_in_curls_argv_and_still_reaches_the_server(self):
        import json
        import shutil
        import subprocess
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from unittest import mock
        if not shutil.which("curl"):
            self.skipTest("no curl")
        features = subprocess.run(["curl", "-V"], capture_output=True, text=True, errors="replace").stdout
        if "HTTP2" not in features:   # curl on Windows and Git for Windows have no HTTP/2, and APNs works only over it
            self.skipTest("curl without HTTP/2")
        got = {}

        class Apns(BaseHTTPRequestHandler):
            def do_POST(self):
                got.update(path=self.path, auth=self.headers.get("authorization"), topic=self.headers.get("apns-topic"),
                           body=self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode())
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass
        srv = HTTPServer(("127.0.0.1", 0), Apns)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        argv, real = [], subprocess.run

        def run(cmd, **kw):
            argv.append(cmd)
            return real(cmd, **kw)
        payload = {"aps": {"alert": {"title": "maps", "body": 'Question "with quotes" \\ and a slash?'}}}
        # a machine proxy that does not exist, and NO_PROXY without its own addresses: curl still goes straight to
        # 127.0.0.1 (core/net)
        dead = {k: "http://127.0.0.1:9" for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy",
                                                   "all_proxy")}
        with mock.patch.object(push, "jwt", return_value="eyJ.SECRET-JWT.sig"), \
                mock.patch.object(push.subprocess, "run", side_effect=run), \
                mock.patch.dict(os.environ, {**dead, "NO_PROXY": "example.org", "no_proxy": "example.org"}):
            code, _ = push.send_apns(CFG["push"], TOKEN, payload, host=f"http://127.0.0.1:{srv.server_address[1]}")
        self.assertEqual(code, 200)
        self.assertFalse(any("SECRET-JWT" in a for cmd in argv for a in cmd), argv)
        self.assertEqual(got["auth"], "bearer eyJ.SECRET-JWT.sig")
        self.assertEqual(got["topic"], "app.x")
        self.assertEqual(got["path"], f"/3/device/{TOKEN}")
        self.assertEqual(json.loads(got["body"]), payload)


if __name__ == "__main__":
    unittest.main()
