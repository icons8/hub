"""Rollback of "sleeping states": the hub state in the form of the previous core (before this change, 2026-10-25), for the
previous code.

    python3 tools/sleep_states.py down <state folder>

Run it with this version's code while the hub is stopped completely (the hub-up service — launchd or systemd — too:
otherwise it would start hubd with the new code, which would read the snapshot the new way again), before starting the
previous code. The previous code does not know the states parked and retired (core/machines.SLEEP): a parked session
would not wake on a message. Here parked → asleep + parked, retired → asleep + retired (core/reducer.legacy); the result
is put as a snapshot at the end of the journal and into state.json — the previous hubd takes state.json, without it the
last snapshot of the journal, and journal readers, once they reach it, take it too.
"""
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from core import journal, lock, reducer  # noqa: E402


def down(state_dir, now=None):
    held = lock.acquire(os.path.join(state_dir, "hubd.lock"))   # hubd writes the same journal and state.json
    if held is None:
        sys.exit("hubd is running — stop the whole hub first (and the hub-up service), then down")
    try:
        state = reducer.legacy(journal.load_state(state_dir))
        j = journal.Journal(os.path.join(state_dir, "journal.jsonl"))
        ev = j.append("core.snapshot", data={"why": "downgrade", "state": {k: v for k, v in state.items()
                                                                           if not k.startswith("_") and k != "seq"}},
                      ts=now or round(time.time(), 3))
        state["seq"] = ev["seq"]
        journal.save_state(state_dir, state)
    finally:
        held.close()
    return sum(s["state"] == "asleep" and bool(s.get("parked") or s.get("retired")) for s in state["sessions"].values())


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "down":
        sys.exit(__doc__)
    print(f"done: a snapshot for the earlier code, sleepers with the flag {down(sys.argv[2])}")
