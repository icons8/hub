"""A hubd scenario without modules, with a constant clock — journal lines are compared byte for byte with the sample
journal_without_modules.jsonl, which the code before the Module API recorded with this same scenario (without modules
the recording stays as before). Only Hub.record, Hub.ingest and host actions — what exists in the code before modules.

The sample was recorded by main code before the Module API (aa9f9d4c): a copy of the scenario, run from the root of that
copy, `python3 scenario.py <empty folder> > journal_without_modules.jsonl`. Rewritten 10-17 (languages): a message in
the snapshot got a new field from — the rest is byte for byte as in that sample; again 10-17 (session essence): a
session in the snapshot got turn_seq — the number where the current turn starts; again 10-26 (inbox check): the fact
from the inbox in the scenario is a legitimate hook fact (session.compacted), not session.said, which only the core
writes (core/admit.py) — the sample was recorded by main code before the inbox check and matches it byte for byte;
again (turn boundary): a session in the snapshot got turn_head — the message the turn began with; again (one report):
turn_his — whether the turn is on the owner's own words; nothing else changed."""
import os
import sys


class Host:
    kind, screen_dialogs = "stream", False

    def __init__(self):
        self.tool_flags = lambda **kw: []

    def __getattr__(self, do):
        if do.startswith("_") or do in ("stamped", "shutdown"):
            raise AttributeError(do)
        return lambda name, **kw: "sent"

    def poll(self):
        return []


def run(state_dir):
    """The journal path after the scenario."""
    from core import daemon, spool
    t = [1000.0]

    def clock():
        t[0] += 0.25
        return t[0]

    owner = {"channel": "cli", "owner": True}
    hub = daemon.Hub(state_dir, Host(), registry_dir=state_dir, clock=clock)
    rec = hub.record
    rec("session.requested", "a", {"cwd": "/w", "session_id": "s-a", **owner})
    rec("session.started", "a", {})
    rec("message.received", None, {"to": "a", "text": "hello", "req": "r1", **owner})
    rec("turn.started", "a", {"marker": "m3"})
    rec("session.said", "a", {"text": "looking"})
    rec("turn.tool", "a", {"id": "t1", "name": "Bash", "input": {"command": "ls"}})
    rec("turn.tool_result", "a", {"id": "t1", "text": "ok"})
    rec("turn.result", "a", {"result": "done", "num_turns": 2, "cost_usd": 0.1})
    rec("message.received", None, {"to": "ghost", "text": "x", "channel": "session", "sender": "a", "req": "r2"})
    rec("session.requested", "b", {"cwd": "/w", "session_id": "s-b", **owner})
    rec("message.received", None, {"to": "b", "text": "wake up", **owner})
    rec("session.started", "b", {})
    rec("timer.fired", "m11", {"attempt": 1, "n": 1})
    spool.put(hub.spool_dir, "session.compacted", "b", {"trigger": "auto"}, src="hook")
    hub.ingest()
    rec("owner.away", None, {"until": None, **owner})
    rec("session.renamed", "b", {"to": "c", **owner})
    rec("unknown.kind", None, {})
    hub.snapshot("stop")
    return os.path.join(state_dir, "journal.jsonl")


if __name__ == "__main__":
    sys.path.insert(0, os.getcwd())
    with open(run(sys.argv[1]), "rb") as f:
        sys.stdout.write(f.read().decode("utf-8"))
