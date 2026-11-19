"""A turn cut by an account limit continues by itself when the limit window resets - on a hub without an account pool.

With a pool the continuation is the accounts module's (modules/accounts: account.switched, with `free` and `reset` once the
window is over). With one login nobody watched the clock: a cut session stood until someone wrote to it, and a hub restart does
not continue it. The cut turn now remembers when its window resets (reducer: limit_cut["resets"], from the account.limits that
the host writes just before the refusal); the daemon step resume_cut asks `due` once per EVERY seconds and writes
session.limit_reset for each session whose window is over; the fold continues it (reducer._limit_reset).

A pure function of the state and the time: no clock, no I/O, so a replay of the journal decides the same."""

import datetime

from core.zones import zone

RESET_GRACE = 15   # seconds after the reset: the API and machine clocks differ slightly (as in modules/accounts)
STEP = 4           # sessions per pass: a hub of dozens of cut sessions is not raised in one blow
EVERY = 30         # seconds between passes (the tick is a fraction of a second: "per tick" would be all at once)

# live and parked ones are continued in place or woken by the message; an executor that fell asleep after the cut (retired) is
# woken by the core, as `hub session wake` would. Asleep (closed by a person), held by the owner and the departed are not
# woken: they continue when someone raises them
CONTINUABLE = ("idle", "busy", "dialog", "starting", "parked", "retired")


def stopped(sess):
    """An executor a person stopped after the cut (the owner, the hub or whoever raised it: `stopped`, cleared when it is
    woken): it sleeps on purpose, so neither the clock nor the pool wakes it at the reset; "Continue" or `hub session wake`
    does. One the core put to sleep idle after the cut (retired with no mark) is woken."""
    return sess.get("state") == "retired" and bool(sess.get("stopped"))


def view(cut):
    """What a client and a module see of a cut turn: {ts, resets} or None (not cut, or the core has continued it). `resets` -
    when the window resets; None when the core does not know it (the limits before the refusal did not name the window, or
    its reset was not ahead: nothing to wait for by the clock). Not the account, the thread or the task the core continues
    it with."""
    if not isinstance(cut, dict):
        return None
    at = cut.get("resets")
    return {"ts": cut.get("ts"), "resets": at if isinstance(at, (int, float)) and not isinstance(at, bool) else None}


def _rank(cut):
    """The owner's thread first, then an assignment, then the rest."""
    return 0 if cut.get("re") else 1 if cut.get("for_delegation") else 2


def _ends_cut(m):
    """A message on its way to its session: its turn ends the cut (handed to an executor: never typed to it, _pump)."""
    return m["state"] in ("queued", "typing") and not m.get("handover")


def due(st, now, step=STEP):
    """Names of sessions to continue now, at most `step`: the turn was cut with no pool account, its reset is known and
    passed (plus the grace) and was not continued yet (limit_reset_done is the reset already used: a session cut again by the
    same window is not continued a second time). A hub with an account pool (an active pool account) - none: there the
    continuation is the pool's. A session with a message queued for it - not yet: that message's turn ends the cut."""
    if (st.get("account") or {}).get("active"):
        return []
    ready, queued = [], None
    for name, sess in st["sessions"].items():
        cut = sess.get("limit_cut")
        if not isinstance(cut, dict) or cut.get("account") is not None or sess["state"] not in CONTINUABLE or stopped(sess) \
                or cut.get("engine") == "codex":   # experiment: a Codex cut is continued by codex.limit_reset (reducer._codex)
            continue
        at = cut.get("resets")
        if not isinstance(at, (int, float)) or isinstance(at, bool) or now < at + RESET_GRACE \
                or at <= (sess.get("limit_reset_done") or 0):
            continue
        if queued is None:   # built only when someone is due
            queued = {m["session"] for m in st["messages"].values() if _ends_cut(m)}
        if name in queued:
            continue
        ready.append((_rank(cut), cut.get("ts") or 0, name))
    return [name for _, _, name in sorted(ready)[:step]]


def number(t):
    return isinstance(t, (int, float)) and not isinstance(t, bool)


def held(st, sess, now):
    """Why the owner's "Continue" (POST /api/session/resume, reducer._resume_requested) cannot continue the cut turn now:
    ("not_paused", None) - no turn cut by a limit; ("not_yet", until) - a window that refuses is still closed and its end
    is known. None - continue now: the windows are over, the pool's active account has room, or the end is unknown (a try
    costs one turn); a message on its way is "not paused" too. The same RESET_GRACE as the Claude clock: a turn sent while
    the API still refuses by the window it names is cut again with no reset ahead, and such a cut is never continued by
    the clock. The windows, the pool and the grace are those of the cut's engine (a Codex cut: the Codex row and pool, no
    grace, as its clock). The page asks this before writing the fact and the fold
    asks it again, so a request in a race is decided the same."""
    cut = (sess or {}).get("limit_cut")
    if not isinstance(cut, dict) or any(m["session"] == sess["name"] and _ends_cut(m) for m in st["messages"].values()):
        return "not_paused", None   # a message on its way: its turn ends the cut (as the clock, due)
    codex = (st.get("codex") or {}) if cut.get("engine") == "codex" else None
    limits = (codex.get("limits") if codex is not None else st.get("limits")) or {}
    acc = (codex.get("account") if codex is not None else st.get("account")) or {}
    ends = [((limits.get("windows") or {}).get(limits.get("type")) or {}).get("resets")
            if limits.get("status") == "rejected" else None]   # the latest refusal (with a pool - the active account's)
    active = acc.get("active")
    if active:   # it continues on the active account (reducer._pump moves it there if it was cut on another)
        waiting, exhausted = acc.get("waiting") or {}, acc.get("exhausted") or {}
        # a fresh token by command (refresh_at) not read yet: taken while all are busy, the pool holds the busy mark and
        # "all busy" until its limits (accounts.decide), but its end is not known, and the turn is what reads it
        read = not (acc.get("refresh_at") or {}).get(active) or bool((acc.get("seen") or {}).get(active))
        ends += [(acc.get("blocked") or {}).get(active) if read else None, exhausted.get("until") if read else None,
                 waiting.get("until") if waiting.get("account") == active else None]
    else:
        ends.append(cut.get("resets"))
    grace = 0 if codex is not None else RESET_GRACE   # the Codex clock has none (reducer._codex_cut_due)
    ends = [t + grace for t in ends if number(t) and t + grace > now]
    return ("not_yet", max(ends)) if ends else None


def at(st, until, now):
    """The time the owner reads in "continues at ...": HH:MM in his zone, with the date when it is not today."""
    tz = zone((st.get("owner") or {}).get("tz") or "UTC")
    then, today = datetime.datetime.fromtimestamp(until, tz), datetime.datetime.fromtimestamp(now, tz)
    return then.strftime("%H:%M" if then.date() == today.date() else "%d.%m %H:%M")
