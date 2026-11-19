"""Start of a chain: which owner message everything began with and where he wrote it from (owner decision 2026-10-08:
answers go where he asked — a result for his Telegram message goes to Telegram, one from the page stays on the page).

Only explicit core links, no guessing from text: an owner message is itself the start (its channel is `channel`); the
owner's words passed on are `re`; a task and everything about it (text to the executor, the core's verdict to the hub,
a "reply to the task", an answer to a question about it, an addition) have its `root`, old tasks have `for`; a schedule
firing has the job's `root`; a session message has the `root` of the turn in which it wrote it. A session turn takes its
start from its marker (or from the lines inserted into it); a turn without a marker (Claude Code woke the session
itself: background work finished) continues the start of the previous turn.
The start is remembered when the link arises (the core removes messages older than a day from the state, while tasks
and jobs live longer)."""

BUSY = ("busy", "dialog")


def root(st, mid):
    """The owner message with which the chain of message mid began — or None."""
    m = (st.get("messages") or {}).get(mid or "")
    if not m:
        return None
    if m.get("from_owner"):
        return mid
    if m.get("re"):
        return m["re"]   # the owner's words passed on (`hub send --for`, a recheck, a continuation after a limit)
    did = m.get("task") or m.get("for_delegation") or m.get("delegation") or m.get("about")
    d = (st.get("delegations") or {}).get(did or "")
    if d:
        return task_root(d)
    job = (st.get("schedule") or {}).get(m.get("schedule") or "")
    if job:
        return job.get("root")
    return m.get("root")


def task_root(d):
    return d["root"] if "root" in d else d.get("for")


def channel(st, mid):
    """Channel of the owner message: telegram, web, cli — or None (unknown: old, removed from the state)."""
    return ((st.get("messages") or {}).get(mid or "") or {}).get("channel")


def of_turn(st, marker, also=()):
    """Start of the turn with marker (and inserted lines also): the first one found."""
    for mid in [marker, *(also or [])]:
        r = root(st, mid)
        if r:
            return r
    return None


def turn_root(st, name):
    """Start of the turn session `name` is running now (its messages, tasks, jobs — in the same chain)."""
    sess = (st.get("sessions") or {}).get(name or "") or {}
    return sess.get("turn_root") if sess.get("state") in BUSY else None


def turn_task(st, name):
    """The task for which session `name` is running a turn now — or None."""
    sess = (st.get("sessions") or {}).get(name or "") or {}
    return sess.get("delegation") if sess.get("state") in BUSY else None


def scheduled(st, mid):
    """The chain of message mid passed through a firing of a recurring schedule job (spec "mini-hub", §4: the task is not
    on the owner's live word, even though `root` is his — the job was set at his request). `hub wait` checks and one-off
    jobs continue the turn where they were set: their mark is that turn's. The owner's words are live."""
    from core import schedule
    m = (st.get("messages") or {}).get(mid or "")
    if not m or m.get("from_owner"):
        return False
    if m.get("re"):   # the owner's words passed on are live, if not passed on from a scheduled turn
        return bool(m.get("via_schedule"))
    did = m.get("task") or m.get("for_delegation") or m.get("delegation") or m.get("about")
    d = (st.get("delegations") or {}).get(did or "")
    if d:   # and a message about a live task written in a scheduled turn (hub send --about) is not live
        return bool(d.get("via_schedule") or m.get("via_schedule"))
    job = (st.get("schedule") or {}).get(m.get("schedule") or "")
    if job:
        return bool(job.get("via_schedule")) or (job.get("kind") in ("every", "cron")
                                                  and not schedule.WAIT_ID.match(job.get("id") or ""))
    return bool(m.get("via_schedule"))


def of_turn_scheduled(st, marker, also=()):
    """The turn with marker is from a schedule firing: judged by the message that gave the turn its start (as in of_turn)."""
    mids = [m for m in [marker, *(also or [])] if m]
    first = next((m for m in mids if root(st, m)), None)
    return scheduled(st, first) if first else any(scheduled(st, m) for m in mids)


def turn_scheduled(st, name):
    """The turn session `name` is running now is from a schedule firing (the mark is set only with mini-hubs)."""
    sess = (st.get("sessions") or {}).get(name or "") or {}
    return bool(sess.get("turn_sched")) if sess.get("state") in BUSY else False
