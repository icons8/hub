"""Migration to executors (spec "tasks, specialists, executors", §8): what to convert, what to ask the owner.

A live worker (role worker, not a hub) with an open task is an executor (raised by the hub); with a direction backlog —
an executor with a backlog (does not leave on its own, closed by "finish?"); with neither — goes to the owner's list.
Agents stay specialists. The journal is not rewritten: the conversion is task.adopted events.
"""
import os
import re
import shutil
import time

import platforms

OPEN = ("working", "waiting-owner", "replied")
GONE = ("removed", "closed", "failed")


def raiser_of(st, name, backlog=None):
    """Who raised it: the session that asked the hub for the task (a task with for_session), otherwise the hub (None). A
    direction (backlog) is created by the hub on the owner's word — it is the hub's, whoever asked."""
    if backlog:
        return None
    from core import delegations
    for d in sorted(st["delegations"].values(), key=delegations.born, reverse=True):
        up = st["sessions"].get(d.get("for_session") or "")
        if d["session"] == name and up and up["state"] not in GONE:
            return up["name"]
    return None


def plan(st, backlogs=()):
    """{"adopt": [(session, backlog or None)], "unclear": [session]} — a repeated run adds nothing."""
    adopt, unclear, delivered = [], [], []
    for name, s in sorted(st["sessions"].items()):
        if s.get("role", "worker") != "worker" or s.get("kind") == "hub" or s["state"] in GONE:
            continue
        has_task = any(d["session"] == name and d["state"] in OPEN for d in st["delegations"].values())
        if name in backlogs:
            adopt.append((name, name))
        elif has_task:
            adopt.append((name, None))
            if not any(d["session"] == name and d["state"] in ("working", "waiting-owner") for d in st["delegations"].values()):
                delivered.append(name)   # handed in the task: after the conversion it will sleep (to wake — hub session wake)
        else:
            unclear.append(name)
    return {"adopt": adopt, "unclear": unclear, "delivered": delivered}


def records(state_dir, st):
    """Task records (core/taskrec) for open tasks that have none: the statement from the task, the reply in the progress
    log if there already was one. A repeated run leaves records alone. Returns the ids of those created."""
    from core import taskrec
    made = []
    for did, d in sorted(st["delegations"].items(), key=lambda x: x[1]["created"]):
        if d["state"] == "done" or os.path.exists(taskrec.path(state_dir, did)):
            continue
        taskrec.read(state_dir, st, did)
        if d.get("reply"):
            taskrec.note(state_dir, st, did, d["session"], "reply: " + str(d["reply"])[:500], now=d.get("since"))
        made.append(did)
    return made


DONE = "migrated"      # a file in the state folder: the automatic conversion (after an update) already happened — do not ask a second time
ROLLBACK = "rollback"  # file: the path of the backup; hub-up restores it between stopping the core and starting it
FILES = ("journal.jsonl", "state.json")


def latest_backup(state_dir):
    root = os.path.join(state_dir, "backup")
    # only <date>-<time> is the migration's copy: scrub-<time> and compact-<time> (archives) and ...-before-rollback sort
    # after or beside it but hold no journal.jsonl to restore
    dirs = sorted(d for d in os.listdir(root) if re.fullmatch(r"\d{8}-\d{6}", d)) if os.path.isdir(root) else []
    return os.path.join(root, dirs[-1]) if dirs else None


def apply_rollback(state_dir):
    """Restore the journal and state from a backup if a rollback was requested. The current ones go to backup/<time>-before-rollback.
    Called by hub-up when the core is stopped. True — restored."""
    flag = os.path.join(state_dir, ROLLBACK)
    try:
        with open(flag) as f:
            src = f.read().strip()
    except OSError:
        return False
    os.unlink(flag)
    if not os.path.exists(os.path.join(src, "journal.jsonl")):
        return False
    keep = os.path.join(state_dir, "backup", time.strftime("%Y%m%d-%H%M%S") + "-before-rollback")
    os.makedirs(keep, exist_ok=True)
    for f in FILES:
        cur = os.path.join(state_dir, f)
        if os.path.exists(cur):
            shutil.copy2(cur, keep)
        if os.path.exists(os.path.join(src, f)):
            shutil.copy2(os.path.join(src, f), cur + ".tmp")
            platforms.current.replace(cur + ".tmp", cur)
        elif os.path.exists(cur):
            os.unlink(cur)
    return True
