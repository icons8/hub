"""A card to the owner that went stale with no answer (owner.ASK_TTL) wakes the session that asked it, once.

A card does not hold the turn, so by the time it goes stale the session has ended its turn and sleeps: the question is on no
screen and nobody will ask it again. The daemon step revive_asks asks `due` every tick and writes the fact ask.expired for each
session found; the fold (reducer._ask_expired) writes the session one message asking it to put the question again with a form
if it is still needed. The card itself stays in the session's asks, as before.

A pure function of the state and the time: no clock, no I/O, so a replay of the journal decides the same."""
import hashlib

from core import budget, busstate, owner

STEP = 2      # sessions at a time, then PAUSE: a hub with a pile of old cards is not woken in one blow (a tick is ~0.3 s)
PAUSE = 60    # seconds after the last raise (the newest revived mark of any session)
TEXT = 200    # the beginning of a question in the message
MAX_AGE = 7 * 24 * 3600   # an older card is never raised: asks are never pruned, and the first tick after an update
                          # would otherwise wake sessions with questions weeks old
WAKEABLE = ("idle", "parked")   # the others: busy ones are waited for, asleep, retired and held are woken by a person only
OVER = ("replied", "stopped", "closed", "archived")   # the card's task is over: nobody waits for the answer


def key(ask):
    """The question as the session put it, without case and spacing: asked again, it is the same question."""
    return hashlib.sha1(owner.question_key(ask).encode()).hexdigest()[:12]


def stale(sess, now, delegations):
    """The session's cards that went stale (but are not older than MAX_AGE), whose task is not over and which were not
    revived yet, one per question: [(card, key)]. delegations - st["delegations"]."""
    revived, seen, out = sess.get("revived") or {}, set(), []
    for ask in sess.get("asks") or []:
        # a hub-bus card (busstate.is_card) is the core's question, not the session's own:
        # asked again with a form it would lose the way the owner's "Send" goes. The task is checked before the
        # question: a closed task's card does not hide the same question still open on another task
        if owner.waits_return(ask) or busstate.is_card(ask) \
                or not owner.ASK_TTL <= now - owner.ask_since(ask) < MAX_AGE \
                or (delegations.get(ask.get("delegation") or "") or {}).get("state") in OVER:
            continue
        k = key(ask)
        if k not in revived and k not in seen:
            seen.add(k)
            out.append((ask, k))
    return out


def holds(sess, now, delegations):
    """A stale card nobody raised yet still holds a session askexpired can wake (WAKEABLE): no sleep, removal or
    reminder before the question is asked again. Asleep or retired, nothing would ever raise it, so it holds nothing."""
    return sess.get("state") in WAKEABLE and bool(stale(sess, now, delegations))


def held(st, now):
    """Why nothing is woken now (a fact of the owner or the limit), or None."""
    o = st.get("owner") or owner.initial()
    if o["state"] == "away":
        return "away"
    if o.get("back_at") and now < o["back_at"] + owner.BACK_GRACE:
        return "back"
    if budget.mode(st) not in (None, "normal"):
        return "budget"
    return None


def due(st, now, step=STEP):
    """[(session, [card ids])], at most `step` sessions and not sooner than PAUSE after the last raise, the stalest
    first: a free session (idle or parked, not handing in) with a stale card nobody answered, which has asked nothing
    since (a card, a tool request), runs nothing, waits for no permission and no message. A question deferred while
    the owner was away (owner.deferred) is not a reason to wait: it went to him in the return summary, and the list is
    cleared only by his next absence."""
    if held(st, now):
        return []
    last = max((t for s in st["sessions"].values() for t in (s.get("revived") or {}).values()), default=0)
    if now - last < PAUSE:
        return []
    ready, queued = [], None
    for name, sess in st["sessions"].items():
        if sess["state"] not in WAKEABLE or sess.get("pending") or sess.get("background") or sess.get("limit_cut") \
                or sess.get("net_cut") or sess.get("closing", "none") != "none" or owner.open_asks(sess, now) \
                or any(r.get("state") == "needs-owner" for r in sess.get("requests") or []):
            continue
        cards = [a for a, _ in stale(sess, now, st["delegations"])]
        if not cards:
            continue
        if queued is None:   # built only when someone is due: the step runs every tick
            queued = {m["session"] for m in st["messages"].values() if m["state"] in ("queued", "typing")}
        if name not in queued:
            ready.append((min(owner.ask_since(a) for a in cards), name, [a["id"] for a in cards]))
    return [(name, ids) for _, name, ids in sorted(ready)[:step]]
