"""Core extension example (EXTENDING.md): a process next to the hub — reads the journal, writes its own facts.

Once every 10 minutes it counts how many turn results each session had in the last hour and writes the fact
`ext.hello.stats` — visible in the journal, in `hub show --grep` and in the "Hello" tab of the same extension.
hub-up starts it with HUB_STATE_DIR (the hub state folder) and PYTHONPATH (the hub code: core.journal, core.spool).
"""
import collections
import os
import time

from core import journal, spool

STATE = os.environ["HUB_STATE_DIR"]
NAME = os.environ.get("HUB_EXTENSION", "hello")


def stats(events, since):
    c = collections.Counter(e["entity"] for e in events if e["type"] == "turn.result" and e["ts"] >= since)
    return dict(c.most_common())


while True:
    events = journal.read_tail(os.path.join(STATE, "journal.jsonl"))
    spool.put(os.path.join(STATE, "spool"), f"ext.{NAME}.stats", None,
              {"turns_last_hour": stats(events, time.time() - 3600)}, src=f"ext:{NAME}")
    time.sleep(600)
