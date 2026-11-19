"""Compacting a session's conversation — Claude Code's /compact from the hub. The command
`hub session compact <session>` (owner, hub or the session itself): the core waits until the session is free and sends
/compact into its stream-json input. Claude Code replies without a turn: a "compacting" status, compact_boundary (how
many tokens there were and are now), the conversation summary as a replay (isReplay) and a result with num_turns 0 —
the host turns this into a session.compacted fact, not a turn result."""
import tests  # noqa: F401 — keep the machine's settings out
import tempfile
import unittest

from core import reducer, views

OWNER = {"channel": "web", "owner": True}


class Pipe:
    def __init__(self):
        self.lines = []

    def write(self, s):
        self.lines.append(s)

    def flush(self):
        pass


class Host(unittest.TestCase):
    def test_the_compact_reply_is_a_fact_not_a_turn(self):
        from hosts.streamjson import StreamHost, StreamProcess
        host = StreamHost(tempfile.mkdtemp(prefix="cmp-"))
        popen = type("P", (), {"stdin": Pipe(), "poll": lambda self: None, "pid": 1})()
        host.procs["a"] = StreamProcess(popen)
        self.assertEqual(host.compact("a"), "sent")
        self.assertIn('"/compact"', popen.stdin.lines[0])
        # how Claude Code 2.1 replies (checked on a live session)
        for msg in ({"type": "system", "subtype": "status", "status": "compacting"},
                    {"type": "system", "subtype": "status", "status": None, "compact_result": "success"},
                    {"type": "system", "subtype": "init", "model": "m", "cwd": "/w", "mcp_servers": []},
                    {"type": "system", "subtype": "compact_boundary",
                     "compact_metadata": {"trigger": "manual", "pre_tokens": 6604, "post_tokens": 1685}},
                    {"type": "user", "isReplay": True, "message": {"role": "user", "content":
                     "This session is being continued… <task-notification><task-id>b1</task-id></task-notification>"}},
                    {"type": "user", "isReplay": True, "message": {"role": "user", "content":
                     "<local-command-stdout>Compacted </local-command-stdout>"}},
                    {"type": "result", "subtype": "success", "num_turns": 0, "result": "", "total_cost_usd": 0.06}):
            host.inbox.put(("a", msg))
        facts = host.poll()
        self.assertEqual([f[0] for f in facts], ["session.info", "session.compacted"])
        self.assertEqual(facts[1][2], {"ok": True, "before": 6604, "after": 1685})
        # the next real turn — as usual
        host.inbox.put(("a", {"type": "result", "result": "done", "num_turns": 3}))
        self.assertEqual([f[0] for f in host.poll()], ["turn.result"])

    def test_what_to_keep_goes_with_the_command(self):
        # review: "/compact keep only the API decisions" — instructions for Claude Code's compaction; don't lose the tail
        from hosts.streamjson import StreamHost, StreamProcess
        host = StreamHost(tempfile.mkdtemp(prefix="cmp-"))
        popen = type("P", (), {"stdin": Pipe(), "poll": lambda self: None, "pid": 1})()
        host.procs["a"] = StreamProcess(popen)
        self.assertEqual(host.compact("a", text="keep only the API decisions"), "sent")
        self.assertIn('"/compact keep only the API decisions"', popen.stdin.lines[0])


class Core(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("session.requested", "hub", cwd="/h", session_id="s-h", kind="hub", **OWNER)
        self.ev("session.requested", "a", cwd="/w", session_id="s-a", **OWNER)
        self.ev("session.requested", "b", cwd="/w", session_id="s-b", **OWNER)
        for n in ("hub", "a", "b"):
            self.ev("session.started", n)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, self.acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 100 + self.seq, "type": type,
                                                           "entity": entity, "data": data})
        return why

    def compacts(self):
        return [a for a in self.acts if a.get("do") == "compact"]

    def steps(self, *steps):
        acts = []
        for s in steps:
            self.ev(*s[:2], **s[2])
            acts += [a for a in self.acts if a.get("do") == "compact"]
        return acts

    def test_compacts_when_free_once(self):
        self.ev("turn.started", "a", marker=None, prompt_head="x")
        self.assertIsNone(self.ev("session.compact_requested", "a", **OWNER))
        self.assertEqual(self.compacts(), [])                                    # busy — waits for the turn to end
        self.assertEqual(self.steps(("turn.result", "a", {"text": "done"}), ("turn.stopped", "a", {})),
                         [{"do": "compact", "session": "a"}])
        self.ev("session.waiting", "a", text="x", channel="session", sender="a")
        self.assertEqual(self.compacts(), [])                                    # only once
        self.assertIsNone(self.ev("session.compacted", "a", ok=True, before=6604, after=1685))

    def types(self):
        return [a.get("message") for a in self.acts if a.get("do") == "type"]

    def test_a_message_waits_while_the_conversation_is_compacted(self):
        # a message woke the session, the core sent /compact for a large conversation and typed the message right
        # after; the compaction ran for minutes, the confirmation took 4 × 30 seconds: "not delivered" on a message
        # that had already been answered
        self.ev("session.compact_requested", "a", **OWNER)
        self.assertEqual(self.compacts(), [{"do": "compact", "session": "a"}])
        self.ev("host.done", "a", do="compact", result="sent")
        mid = f"m{self.seq + 1}"
        self.ev("message.received", None, to="a", text="give me the text for the PR", **OWNER)
        self.assertEqual(self.types(), [])                                       # being compacted — waits in the queue
        self.assertEqual(self.st["messages"][mid]["state"], "queued")
        self.ev("session.compacted", "a", session_id="s-a")                      # hook: the compaction is over
        self.assertEqual(self.types(), [mid])

    def test_a_new_tool_set_waits_for_the_compact(self):
        # a model change (like granting a tool) sets dirty: release+spawn in the middle of /compact cut the compaction
        # short while the compact instruction was already cleared — the conversation stayed long
        self.ev("session.compact_requested", "a", **OWNER)
        self.ev("host.done", "a", do="compact", result="sent")
        self.ev("session.model_set", "a", model="default", **OWNER)
        self.assertEqual([a["do"] for a in self.acts if a.get("session") == "a"], [])   # being compacted — waits
        self.ev("session.compacted", "a", session_id="s-a")
        self.assertEqual([a["do"] for a in self.acts if a.get("session") == "a"], ["release", "spawn"])

    def test_a_message_held_by_the_old_core_is_still_hers_during_the_compact(self):
        # "waiting on the hub's decision" (the earlier core) was also lifted from a free session in the middle of a
        # compaction — the barrier does not change that
        self.ev("session.compact_requested", "a", **OWNER)
        self.ev("turn.started", "a", marker=None, prompt_head="x")
        mid = f"m{self.seq + 1}"
        self.ev("message.received", None, to="a", text="after compaction", **OWNER)
        self.ev("turn.stopped", "a")
        self.st["messages"][mid]["held"] = "m1"
        self.ev("session.waiting", "a", text="x", channel="session", sender="a")
        self.assertNotIn("held", self.st["messages"][mid])
        self.assertEqual(self.st["messages"][mid]["state"], "queued")

    def test_a_respawn_during_compact_does_not_hold_the_queue(self):
        # release+spawn and reload replace the process without session.exited; session.compacted will not come
        self.ev("session.compact_requested", "a", **OWNER)
        self.ev("host.done", "a", do="compact", result="sent")
        self.ev("session.started", "a", pid=2)
        mid = f"m{self.seq + 1}"
        self.ev("message.received", None, to="a", text="after the respawn", **OWNER)
        self.assertEqual(self.types(), [mid])

    def test_a_failed_compact_does_not_hold_the_queue(self):
        self.ev("session.compact_requested", "a", **OWNER)
        self.ev("host.done", "a", do="compact", result="unsupported")            # tmux cannot /compact
        mid = f"m{self.seq + 1}"
        self.ev("message.received", None, to="a", text="x", **OWNER)
        self.assertEqual(self.types(), [mid])

    def test_a_late_turn_delivers_a_message_marked_failed(self):
        # the confirmation was not awaited, yet the turn with its marker did start — it arrived, no "Not delivered" stays
        mid = f"m{self.seq + 1}"
        self.ev("message.received", None, to="a", text="x", **OWNER)
        for n in range(1, 5):
            self.ev("timer.fired", mid, attempt=n, n=n)
        self.assertEqual(self.st["messages"][mid]["state"], "failed")
        self.ev("turn.started", "a", marker=mid, prompt_head="x")
        self.assertEqual(self.st["messages"][mid]["state"], "delivered")
        self.assertIsNone(self.st["messages"][mid].get("reason"))

    def test_a_late_turn_does_not_revive_what_the_owner_removed(self):
        self.ev("turn.started", "a", marker=None, prompt_head="x")
        mid = f"m{self.seq + 1}"
        self.ev("message.received", None, to="a", text="x", **OWNER)
        self.assertIsNone(self.ev("message.cancel", mid, **OWNER))
        self.ev("turn.started", "a", marker=mid, prompt_head="x")
        self.assertEqual((self.st["messages"][mid]["state"], self.st["messages"][mid]["reason"]),
                         ("failed", "removed by owner"))

    def test_what_to_keep_reaches_the_host(self):
        self.ev("turn.started", "a", marker=None, prompt_head="x")
        self.assertIsNone(self.ev("session.compact_requested", "a", text="  keep the API decisions ", **OWNER))
        self.assertEqual(self.steps(("turn.result", "a", {"text": "done"}), ("turn.stopped", "a", {})),
                         [{"do": "compact", "session": "a", "text": "keep the API decisions"}])

    def test_who_may_ask(self):
        self.assertIsNotNone(self.ev("session.compact_requested", "a", channel="session", sender="b"))
        self.assertIsNone(self.ev("session.compact_requested", "a", channel="session", sender="a"))   # the session itself
        self.assertIsNone(self.ev("session.compact_requested", "b", channel="session", sender="hub"))

    def test_feed_says_it(self):
        r = views.Reader()
        evs = [{"seq": 1, "ts": 1, "type": "session.requested", "entity": "a", "data": {"cwd": "/w", "session_id": "s"}},
               {"seq": 2, "ts": 2, "type": "session.started", "entity": "a", "data": {}},
               {"seq": 3, "ts": 3, "type": "session.compact_requested", "entity": "a", "data": OWNER},
               {"seq": 4, "ts": 4, "type": "host.done", "entity": "a", "data": {"do": "compact", "result": "sent"}},
               {"seq": 6, "ts": 6, "type": "session.compacted", "entity": "a",
                "data": {"ok": True, "before": 160400, "after": 21500}},
               {"seq": 7, "ts": 7, "type": "session.compacted", "entity": "a", "data": {"session_id": "s"}}]   # hook
        for e in evs:
            r.feed(e)
        lines = [it["text"] for it in r.timelines["a"] if it["kind"] == "sys"]
        self.assertTrue(any("compacting the conversation" in t for t in lines), lines)
        self.assertEqual(len([t for t in lines if "conversation compacted" in t]), 1, lines)
        self.assertTrue(any("conversation compacted: 160k → 22k" in t for t in lines), lines)

    def test_feed_does_not_guess_when_the_core_will_compact(self):
        # when to compact is decided by reducer._pump, not the feed — the feed does not predict; "compacting" appears
        # only once the core has really sent the command to the session
        r = views.Reader()
        evs = [{"seq": 1, "ts": 1, "type": "session.requested", "entity": "a", "data": {"cwd": "/w", "session_id": "s"}},
               {"seq": 2, "ts": 2, "type": "session.started", "entity": "a", "data": {}},
               {"seq": 3, "ts": 3, "type": "turn.started", "entity": "a", "data": {"marker": None, "prompt_head": "x"}},
               {"seq": 4, "ts": 4, "type": "session.compact_requested", "entity": "a", "data": OWNER}]
        for e in evs:
            r.feed(e)
        sys_lines = lambda: [it["text"] for it in r.timelines["a"] if it["kind"] == "sys" and "conversation" in it["text"]]
        self.assertEqual(sys_lines(), [])                                    # busy — neither "will compact" nor "compacting"
        r.feed({"seq": 5, "ts": 5, "type": "host.done", "entity": "a", "data": {"do": "compact", "result": "sent"}})
        self.assertEqual(sys_lines(), ["compacting the conversation (/compact)"])        # the core sent it — only then it writes

    def test_a_tmux_session_says_it_cannot_compact(self):
        # review: the tmux host cannot /compact (host.done unsupported) — the feed stayed silent
        r = views.Reader()
        for e in ({"seq": 1, "ts": 1, "type": "session.requested", "entity": "a", "data": {"cwd": "/w", "session_id": "s"}},
                  {"seq": 2, "ts": 2, "type": "session.started", "entity": "a", "data": {}},
                  {"seq": 3, "ts": 3, "type": "session.compact_requested", "entity": "a", "data": OWNER},
                  {"seq": 4, "ts": 4, "type": "host.done", "entity": "a", "data": {"do": "compact", "result": "unsupported"}}):
            r.feed(e)
        (line,) = [it for it in r.timelines["a"] if it["kind"] == "sys" and "conversation" in it["text"]]
        self.assertTrue(line["text"].startswith("could not compact the conversation"), line)
        self.assertTrue(line["command"])                                     # the outcome of his command — visible in "Brief"

    def test_owner_command_stays_in_the_dialog_history(self):
        # /compact from the page goes to the core, not to the session as text — the feed still shows that he sent it,
        # as his own line; from the hub, a session or the core (idle) — only service lines
        r = views.Reader()
        evs = [{"seq": 1, "ts": 1, "type": "session.requested", "entity": "a", "data": {"cwd": "/w", "session_id": "s"}},
               {"seq": 2, "ts": 2, "type": "session.started", "entity": "a", "data": {}},
               {"seq": 3, "ts": 3, "type": "session.compact_requested", "entity": "a", "data": OWNER},
               {"seq": 4, "ts": 4, "type": "session.compact_requested", "entity": "a",
                "data": {"channel": "session", "sender": "a"}},
               {"seq": 5, "ts": 5, "type": "session.compact_requested", "entity": "a", "data": {"channel": "core", "why": "idle"}},
               {"seq": 6, "ts": 6, "type": "session.effort_set", "entity": "a", "data": {**OWNER, "level": "high"}},
               {"seq": 7, "ts": 7, "type": "session.reload_requested", "entity": "a", "data": OWNER}]
        for e in evs:
            r.feed(e)
        mine = [(it["seq"], it["text"], it["command"]) for it in r.timelines["a"] if it["kind"] == "in" and not it.get("sender")]
        self.assertEqual(mine, [(3, "/compact", True), (6, "/effort high", True), (7, "/reload", True)])
        self.assertFalse(any("restart" in (it.get("text") or "") for it in r.timelines["a"]))   # no predictions

    def test_only_what_became_of_his_command_is_marked_for_the_short_view(self):
        # review: "Brief" looked up lines by text and showed service lines too — "raised again" on every raise,
        # "compacting" on an idle compaction. The core sets the command mark: only the outcome of the owner's command
        r = views.Reader()
        ev = lambda seq, type, data: r.feed({"seq": seq, "ts": seq, "type": type, "entity": "a", "data": data})
        ev(1, "session.requested", {"cwd": "/w", "session_id": "s"})
        ev(2, "session.started", {})
        ev(3, "session.compact_requested", {"channel": "core", "why": "idle"})            # on idle — service
        ev(4, "host.done", {"do": "compact", "result": "sent"})
        ev(5, "session.compacted", {"ok": True, "before": 160400, "after": 21500})
        ev(6, "session.started", {"source": "resume"})                                    # a raise by itself
        ev(7, "turn.started", {"marker": None, "prompt_head": "x"})
        ev(8, "session.compact_requested", OWNER)                                         # his — in the middle of a turn
        ev(9, "turn.result", {"text": "done"})
        ev(10, "turn.stopped", {})
        ev(11, "host.done", {"do": "compact", "result": "sent"})
        ev(12, "session.compacted", {"ok": True, "before": 90000, "after": 20000})
        ev(13, "session.reload_requested", OWNER)
        ev(14, "session.started", {"source": "resume"})
        marked = [(it["seq"], it["kind"], bool(it.get("midturn"))) for it in r.timelines["a"] if it.get("command")]
        self.assertEqual(marked, [(8, "in", True), (11, "sys", False), (12, "sys", False), (13, "in", False),
                                  (14, "sys", False)])


if __name__ == "__main__":
    unittest.main()
