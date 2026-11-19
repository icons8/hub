"""Compact a long conversation while the cache is warm (owner decision 2026-10-08: compaction before sleep). Analysis of
2026-10-05..08: 38% of the spend was the whole conversation written into the cache again (258 times, ~335k tokens each),
175 of them a session woken after an hour of silence, when the cache (1 h) had expired. The core remembers how long the
conversation is (the context of the last model call in the turn); idle for 30 minutes and the conversation longer than
the threshold — /compact while the cache is still warm: the wake-up then writes the compacted conversation.
Do not compact after an hour — the cache has gone cold, and compaction itself would write the whole conversation.
Tests do not call the model."""
import tests  # noqa: F401 — do not see the machine's settings
import tempfile
import unittest

from core import reducer
from core.machines import COMMAND_CONFIRM_SECONDS
from core.daemon import Hub

OWNER = {"channel": "web", "owner": True}
MIN = 60


class Pipe:
    def __init__(self):
        self.lines = []

    def write(self, s):
        self.lines.append(s)

    def flush(self):
        pass


class HostContext(unittest.TestCase):
    def test_turn_result_carries_the_context_of_the_last_call(self):
        from hosts.streamjson import StreamHost, StreamProcess
        host = StreamHost(tempfile.mkdtemp(prefix="cidle-"))
        popen = type("P", (), {"stdin": Pipe(), "poll": lambda self: None, "pid": 1})()
        host.procs["a"] = StreamProcess(popen)
        for usage in ({"input_tokens": 3, "cache_read_input_tokens": 200000, "cache_creation_input_tokens": 1000},
                      {"input_tokens": 5, "cache_read_input_tokens": 201000, "cache_creation_input_tokens": 2500}):
            host.inbox.put(("a", {"type": "assistant", "message": {"id": "x", "model": "m", "usage": usage,
                                                                    "content": [{"type": "text", "text": "ok"}]}}))
        host.inbox.put(("a", {"type": "result", "result": "done", "num_turns": 2,
                              "usage": {"input_tokens": 8, "cache_read_input_tokens": 401000}}))
        res = [f for f in host.poll() if f[0] == "turn.result"][0][2]
        self.assertEqual(res["context"], 203505)                      # the last call, not the sum over the turn
        self.assertEqual(res["tokens_in"], 401008)


class Daemon(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.h = Hub(tempfile.mkdtemp(prefix="cidle-d-"), type("H", (), {"alive": lambda s, n: True})(),
                     registry_dir=tempfile.mkdtemp(), clock=lambda: self.now[0])
        self.done = []
        self.h.execute = lambda a: self.done.append(a)
        for name, kind in (("hub", "hub"), ("a", "work"), ("b", "work")):
            self.h.record("session.requested", name, {"cwd": "/w", "session_id": "s-" + name, "kind": kind, **OWNER})
            self.h.record("session.started", name, {})

    def turn(self, name, context):
        self.h.record("turn.started", name, {"marker": None, "prompt_head": "x"})
        self.h.record("turn.result", name, {"text": "done", "turns": 3, "context": context})
        self.h.record("turn.stopped", name, {})

    def compacted(self):
        out = [a["session"] for a in self.done if a.get("do") == "compact"]
        self.done = []
        return out

    def test_a_long_conversation_free_for_half_an_hour_is_compacted_once(self):
        self.turn("a", 250000)
        self.turn("b", 90000)                                         # short — leave it alone
        self.now[0] += 10 * MIN
        self.h.compact_idle()
        self.assertEqual(self.compacted(), [])                        # too early: the owner may answer right now
        self.now[0] += 21 * MIN
        self.h.compact_idle()
        self.assertEqual(self.compacted(), ["a"])
        self.h.record("session.compacted", "a", {"ok": True, "before": 250000, "after": 30000})
        self.assertEqual(self.h.state["sessions"]["a"]["context"], 30000)
        self.now[0] += 5 * MIN
        self.h.compact_idle()
        self.assertEqual(self.compacted(), [])                        # once

    def test_not_when_the_cache_is_already_cold(self):
        self.turn("a", 250000)
        self.now[0] += 70 * MIN
        self.h.compact_idle()
        self.assertEqual(self.compacted(), [])

    def test_the_hub_too_but_not_a_session_with_something_to_do(self):
        self.turn("hub", 300000)
        self.turn("a", 300000)
        self.h.record("message.received", None, {"to": "a", "text": "a task", **OWNER})
        self.done = []
        self.h.record("turn.started", "a", {"marker": None, "prompt_head": "a task"})   # busy
        self.now[0] += 35 * MIN
        self.h.compact_idle()
        self.assertEqual(self.compacted(), ["hub"])

    def test_after_a_new_turn_it_may_be_compacted_again(self):
        self.turn("a", 250000)
        self.now[0] += 31 * MIN
        self.h.compact_idle()
        self.h.record("session.compacted", "a", {"ok": True, "before": 250000, "after": 30000})
        self.assertEqual(self.compacted(), ["a"])
        self.turn("a", 260000)
        self.now[0] += 31 * MIN
        self.h.compact_idle()
        self.assertEqual(self.compacted(), ["a"])

    def test_a_pending_browser_reload_does_not_block_it(self):
        # 2026-10-09: an agent at 363k, idle for an hour, was not compacted: it waited for a browser reload (browser_stale), while compaction
        # does not kill the process and is not blocked by it
        self.turn("a", 300000)
        self.h.state["sessions"]["a"]["browser_stale"] = True
        self.now[0] += 31 * MIN
        self.h.compact_idle()
        self.assertEqual(self.compacted(), ["a"])

    def test_an_open_task_does_not_block_it(self):
        # 2026-10-15: an agent at 846k (423% of the threshold) was not compacted for a day and a half — it had a delegation "in progress", while compaction,
        # like a browser reload, does not kill the process; the delegation lives in the core, the compaction summary keeps the current task
        self.turn("a", 450000)
        self.h.record("delegation.created", None, {"session": "a", "for_session": "b", "context": "a layout"})
        mid = next(k for k, m in self.h.state["messages"].items() if m["session"] == "a")
        self.h.record("turn.started", "a", {"marker": mid, "prompt_head": "a layout"})
        self.h.record("turn.stopped", "a", {})
        self.assertTrue(any(d["session"] == "a" and d["state"] == "working"
                            for d in self.h.state["delegations"].values()))
        self.done = []
        self.now[0] += 6 * MIN
        self.h.compact_idle()
        self.assertEqual(self.compacted(), ["a"])

    def test_a_very_long_one_is_compacted_after_a_short_pause(self):
        # 2026-10-09: an agent doing a round every 30 minutes was never idle for 30 minutes in a row — 450k and growing; twice as long as
        # the threshold — compact after 5 minutes of silence, the cache is still warm
        self.turn("a", 450000)
        self.turn("b", 250000)
        self.now[0] += 4 * MIN
        self.h.compact_idle()
        self.assertEqual(self.compacted(), [])
        self.now[0] += 2 * MIN
        self.h.compact_idle()
        self.assertEqual(self.compacted(), ["a"])                     # b — as before, after half an hour

    def test_switched_off_in_settings(self):
        self.h.config["compact_idle"] = {"after_minutes": 0}
        self.turn("a", 400000)
        self.now[0] += 31 * MIN
        self.h.compact_idle()
        self.assertEqual(self.compacted(), [])

    def test_parking_waits_until_the_compaction_ends(self):
        # parking and idle compaction fire at the same time: /compact is sent, and in the same tick the process is killed
        self.h.config["park_after_hours"] = 0.5
        self.turn("a", 250000)
        self.now[0] += 31 * MIN
        self.h.compact_idle()
        self.h.park_idle()
        self.assertEqual(self.h.state["sessions"]["a"]["state"], "idle")
        self.assertEqual(self.compacted(), ["a"])
        self.h.park_idle()
        self.assertEqual(self.h.state["sessions"]["a"]["state"], "idle")        # compaction is still running
        self.h.record("session.compacted", "a", {"ok": True, "before": 250000, "after": 30000})
        self.h.park_idle()
        self.assertEqual(self.h.state["sessions"]["a"]["state"], "parked")      # compacted — now it is allowed

    def test_parking_does_not_wait_forever_for_a_compaction_that_never_answers(self):
        # /compact was sent, no result came (Claude Code hung) — a hung process is exactly the one parking must kill
        self.h.config["park_after_hours"] = 0.5
        self.turn("a", 250000)
        self.now[0] += 31 * MIN
        self.h.compact_idle()
        self.assertEqual(self.compacted(), ["a"])
        self.h.park_idle()
        self.assertEqual(self.h.state["sessions"]["a"]["state"], "idle")
        self.now[0] += COMMAND_CONFIRM_SECONDS + 1
        self.h.park_idle()
        self.assertEqual(self.h.state["sessions"]["a"]["state"], "parked")

    def test_a_hub_restart_in_the_middle_of_a_compaction_still_parks_instead_of_respawning(self):
        # the process is gone (a hub restart during /compact): nothing to compact, no need to bring everyone up at once
        self.turn("a", 250000)
        self.now[0] += 31 * MIN
        self.h.compact_idle()
        self.assertEqual(self.compacted(), ["a"])
        self.assertTrue(self.h.state["sessions"]["a"].get("compacting"))
        self.h.host = type("H", (), {"alive": lambda s, n: False})()
        self.h.reconcile()
        self.assertEqual(self.h.state["sessions"]["a"]["state"], "parked")

    def test_a_plugin_update_waits_until_the_compaction_ends(self):
        # an updated plugin on an idle session: not parked (compaction is running) — and not restarted in the middle of /compact
        self.turn("a", 250000)
        self.now[0] += 31 * MIN
        self.h.compact_idle()
        self.assertEqual(self.compacted(), ["a"])
        self.h.state["sessions"]["a"]["plugins_stale"] = True
        self.h.record("message.received", None, {"to": "b", "text": "x", **OWNER})   # any fact — a queue pass
        self.assertFalse([a for a in self.done if a.get("session") == "a" and a.get("do") in ("release", "spawn")])
        self.h.record("session.compacted", "a", {"ok": True, "before": 250000, "after": 30000})
        self.assertEqual(self.h.state["sessions"]["a"]["state"], "parked")   # compacted — the new version with the first message

    def test_a_hub_restart_with_a_compaction_not_yet_sent_parks(self):
        self.h.state["sessions"]["a"]["compact"] = True   # assigned but not sent: there is no process — not a reason to bring it up
        self.h.host = type("H", (), {"alive": lambda s, n: False})()
        self.h.reconcile()
        self.assertEqual(self.h.state["sessions"]["a"]["state"], "parked")

    def test_a_stuck_compaction_does_not_stop_the_next_idle_compaction(self):
        self.turn("a", 250000)
        self.now[0] += 31 * MIN
        self.h.compact_idle()
        self.assertEqual(self.compacted(), ["a"])
        self.now[0] += COMMAND_CONFIRM_SECONDS + 1
        self.h.state["sessions"]["a"].pop("idle_compacted", None)   # no result came — the compaction counts as failed
        from core import parking
        self.assertTrue(parking.compactable(self.h.state, self.h.state["sessions"]["a"], self.now[0], {}))

    def test_a_threshold_from_settings(self):
        self.h.config["compact_idle"] = {"min_tokens": 100000}
        self.turn("b", 120000)
        self.now[0] += 31 * MIN
        self.h.compact_idle()
        self.assertEqual(self.compacted(), ["b"])


class Core(unittest.TestCase):
    def test_only_the_core_asks_for_it_quietly_and_the_hooks_compaction_forgets_the_length(self):
        st, seq = reducer.empty(), [0]

        def ev(type, entity=None, **data):
            nonlocal st
            seq[0] += 1
            st, acts, why = reducer.reduce(st, {"seq": seq[0], "ts": 100 + seq[0], "type": type, "entity": entity,
                                                "data": data})
            return why

        ev("session.requested", "a", cwd="/w", session_id="s-a", **OWNER)
        ev("session.started", "a")
        ev("turn.result", "a", text="x", turns=1, context=220000)
        self.assertEqual(st["sessions"]["a"]["context"], 220000)
        self.assertIsNotNone(ev("session.compact_requested", "a", channel="session", sender="b", why="idle"))
        self.assertIsNone(ev("session.compact_requested", "a", channel="core", why="idle"))
        ev("session.compacted", "a", session_id="s-a")                # the SessionStart(compact) hook — no numbers
        self.assertNotIn("context", st["sessions"]["a"])


if __name__ == "__main__":
    unittest.main()


class OnWake(Daemon):
    """Owner decision 2026-10-11: a setting on by default — compact a session on revive and on wake-up if it is over the
    limit. The cache is cold after sleep — an uncompacted conversation would be written in full; compaction goes before the first message."""
    def wake(self, name):
        self.h.record("session.parked", name, {"channel": "core", "why": "idle"})
        self.done = []
        self.h.record("message.received", None, {"to": name, "text": "wake up", **OWNER})
        self.h.record("session.started", name, {})

    def first(self):
        acts = [a["do"] for a in self.done if a.get("do") in ("compact", "send", "type")]
        return acts[0] if acts else None

    def test_a_long_session_is_compacted_on_wake_before_the_message(self):
        self.turn("a", 250000)
        self.wake("a")
        self.assertEqual(self.first(), "compact")

    def test_a_short_one_just_gets_the_message(self):
        self.turn("a", 90000)
        self.wake("a")
        self.assertNotEqual(self.first(), "compact")

    def test_switched_off_in_settings(self):
        self.h.config = {**self.h.config, "compact_idle": {"on_wake": False}}
        self.h.load_catalog = lambda: None
        self.h.record("owner.settings", None, {"timezone": "UTC", "compact_wake": False, "compact_min": 200000})
        self.turn("a", 250000)
        self.wake("a")
        self.assertNotEqual(self.first(), "compact")

    def test_revive_compacts_a_long_one_too(self):
        self.turn("a", 250000)
        self.h.record("session.ended", "a", {"reason": "prompt_input_exit"})
        self.h.record("session.remove_requested", "a", OWNER)
        self.done = []
        self.h.record("session.requested", "a", {"cwd": "/w", "session_id": "s-a", "kind": "work", "revive": True, **OWNER})
        self.h.record("session.started", "a", {})
        self.assertIn("compact", [a["do"] for a in self.done])

    def test_asked_while_asleep_it_is_compacted_first_on_wake(self):
        # 2026-10-15: a session slept (the hub was restarted) with a conversation at one and a half thresholds; the hub asked to compact — "a live
        # session can be compacted", and it woke it with a message only for compaction. A request to a sleeping session is accepted and brings nothing up:
        # compaction goes first thing on wake-up, with the requester's instructions
        self.turn("a", 90000)                                         # shorter than the threshold: it would not compact by itself on wake-up
        self.h.record("session.parked", "a", {"channel": "core", "why": "idle"})
        self.done = []
        self.h.record("session.compact_requested", "a", {"channel": "session", "sender": "hub", "text": "keep the plan"})
        self.assertFalse(any(e["type"] == "core.rejected" for e in self.journal_tail()))
        self.assertEqual(self.done, [])                               # keeps sleeping
        self.h.record("message.received", None, {"to": "a", "text": "wake up", **OWNER})
        self.h.record("session.started", "a", {})
        self.assertEqual(self.first(), "compact")
        self.assertEqual(next(a for a in self.done if a["do"] == "compact").get("text"), "keep the plan")

    def test_asked_while_asleep_it_is_compacted_on_any_wake(self):
        # review: the request waited only for a wake-up by a message — hub session wake without text did not carry it out
        self.turn("a", 90000)
        self.h.record("session.parked", "a", {"channel": "core", "why": "idle"})
        self.h.record("session.compact_requested", "a", {"channel": "session", "sender": "hub", "text": "keep the plan"})
        self.done = []
        self.h.record("session.wake_requested", "a", {"channel": "session", "sender": "hub"})
        self.h.record("session.started", "a", {})
        self.assertEqual(next(a for a in self.done if a["do"] == "compact").get("text"), "keep the plan")

    def test_a_removed_one_still_cannot_be_compacted(self):
        self.h.record("session.ended", "a", {"reason": "prompt_input_exit"})
        self.h.record("session.remove_requested", "a", OWNER)
        self.h.record("session.compact_requested", "a", {"channel": "session", "sender": "hub"})
        self.assertTrue(any(e["type"] == "core.rejected" for e in self.journal_tail()))

    def journal_tail(self):
        from core import journal
        return list(journal.read(self.h.journal.path))[-3:]


class MidTurn(unittest.TestCase):
    """A single long turn grows past the threshold several times (54k → 291k), and the core compacts only between turns.
    compact_idle.mid_turn lets Claude Code compact mid-turn at the hub threshold (CLAUDE_CODE_AUTO_COMPACT_WINDOW);
    off by default."""
    def test_the_window_reaches_the_session_env(self):
        import json, os, shutil, types
        from unittest import mock
        d = tempfile.mkdtemp(prefix="midturn-")
        self.addCleanup(shutil.rmtree, d, True)
        cfg = os.path.join(d, "hub.json")
        host = types.SimpleNamespace(env={})
        with mock.patch.dict(os.environ, {"HUB_CONFIG": cfg}):
            hub = Hub(d, host, registry_dir=os.path.join(d, "registry"))
            hub.load_catalog()
            self.assertNotIn("CLAUDE_CODE_AUTO_COMPACT_WINDOW", host.env)          # default: Claude Code decides
            for ci, want in (({"mid_turn": True}, "200000"), ({"mid_turn": True, "min_tokens": 300000}, "300000"),
                             ({"mid_turn": False}, None)):
                with open(cfg, "w", encoding="utf-8") as f:
                    json.dump({"compact_idle": ci}, f)
                hub.load_catalog()
                self.assertEqual(host.env.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW"), want)
