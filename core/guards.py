"""Transition conditions of the state machines (core/machines.py): None - allowed, otherwise the refusal reason (a core
phrase in English, core/coretext.py: the owner reads it in his language).

They check core state only - no indirect signs. `closable` - the session can be put to sleep:
no turn, no question to the owner, no open delegation, no message queue.
"""
import re

from core import minihub, specmode
from core.coretext import T
from core.machines import SLEEP
from core.modules import NAME as _MODULE_NAME

# one rule for opening, removing and stopping sessions - and one reason (06.10: removal got "the hub opens new sessions")
HUB_OR_OWNER = T("core.guards.hub_or_owner", "the hub or the owner does this — ask the hub: hub send hub \"…\"")
OWNER_WORD = T("core.guards.owner_word", "this is the owner's word")
OWNER_CHANNELS = ("web", "cli", "telegram")  # owner channels (the page, the CLI outside sessions, his Telegram); not sessions
# and a module handler serving an owner's request (Module API: the page passes the module X-Hub-Owner) - channel mod:<name>
MODULE_CHANNEL = re.compile("mod:" + _MODULE_NAME.pattern)


def owner_channel(channel):
    """A channel the owner's word arrives through."""
    return channel in OWNER_CHANNELS or (isinstance(channel, str) and MODULE_CHANNEL.fullmatch(channel) is not None)


def held_by_owner(st, sess):
    return None if sess["state"] == "held" else T("core.guards.not_held", "the session is not in manual mode")


def on_duty(st, sess):
    """Who cannot be put to sleep in any state: the hub, a system agent, an agent before dismissal. None - can sleep."""
    if sess.get("kind") == "hub":
        return T("core.guards.hub_awake", "the hub cannot be put to sleep: it always works")
    from core import system
    if system.refused(st, sess["name"]):
        return system.refused(st, sess["name"])
    if sess.get("role") == "agent" and sess.get("kind") != "operator":  # the network operator was a core role, it is gone
        # owner decision 2026-09-30: agents must not go anywhere until they are dismissed
        return T("core.guards.agent_on_duty", "this is an agent: it stays on duty until the owner dismisses it "
                 "(hub session role {name} worker)").format(name=sess["name"])
    return None


def closable(st, sess, queued=True, now=None):
    """Whether it can be put to sleep now (None) or why not. queued=False - queued messages do not hold it: whoever
    raised it will pick them up (remove of a free executor - same as stop, core/reducer). now - the event time (a guard
    gets it as sess["_now"]): a card older than owner.ASK_TTL no longer holds it."""
    from core import owner
    why = on_duty(st, sess)
    if why:
        return why
    if sess["state"] in SLEEP:   # 08.10: "not free (asleep): a turn is running" was wrong - it is asleep
        return T("core.guards.already_asleep", "the session is already asleep, there is no process — close is not "
                 "needed; to remove it from the list — hub session remove {name}").format(name=sess["name"])
    if sess["state"] != "idle":
        return T("core.guards.not_free", "the session is not free ({state}): a turn is running or it waits for an "
                 "answer").format(state=sess["state"])
    if sess.get("pending"):
        return T("core.guards.pending", "the session waits for the owner's answer to its question")
    asks = owner.open_asks(sess, sess.get("_now") if now is None else now)
    if asks:
        # a question card is open: if it slept, the hub would decide the question was abandoned and ask the owner a second
        # time (a personal hub, 04.10: both the hub and the session were waiting)
        return T("core.guards.open_ask", "the session has an open question to the owner {id} — it waits for the "
                 "answer").format(id=asks[-1]["id"])
    if sess.get("background"):
        # free between turns, but its commands or agents run in the background: sleep would kill them (30.09)
        return T("core.guards.background", "in the session's background: {work}").format(
            work=", ".join(b.get("title") or b["id"] for b in sess["background"].values()))
    from core import hubrun
    going = hubrun.live_of(st, sess["name"])
    if going:   # its `hub run` goes on outside it, and the result comes to it as a message (core/hubrun.py)
        return T("core.guards.hub_run", "a hub run is going: {ids} — its result comes as a message (to stop it: hub run "
                 "stop <id>)").format(ids=", ".join(r["id"] for r in going))
    for d in st.get("delegations", {}).values():
        if d["session"] == sess["name"] and specmode.holds(d):
            if specmode.is_open(d):
                return T("core.guards.spec_waits", "the spec of {id} waits for the owner's \"Accept\"").format(id=d["id"])
            return T("core.guards.task_open", "task {id} is not closed yet").format(id=d["id"])
    for m in st["messages"].values():
        if m["session"] == sess["name"] and m["state"] in (("queued", "typing") if queued else ("typing",)):
            return T("core.guards.queued", "message {id} is in the queue").format(id=m["id"])
    return None


def parked_free(st, sess):
    """A parked session (core/parking) counts as "free" for the owner: the owner's or the hub's "Put to sleep" removes the
    parking, and it really sleeps (12.10: four silent refusals in a row). There is no process - nobody to hand work over
    to; the rest is as in closable: with an open question or delegation it cannot sleep, parking does not look at them.
    Not parked - the reason from closable."""
    if sess["state"] != "parked" or from_owner_or_hub(st, sess.get("req") or {}):
        return closable(st, sess)
    return closable(st, {**sess, "state": "idle"})


def executor_done(st, sess):
    """The core retires an executor that has done its job (core/executors.retiring): sleep at once, no handover - the report has already gone."""
    req = sess.get("req") or {}
    if not req.get("retire") or not minihub.runner(sess):
        return T("core.guards.not_retire", "not an executor's leaving")
    if req.get("channel") == "session":
        return T("core.guards.core_retires", "the core retires the executor")
    return closable(st, sess)


def own_turn(st, sess):
    """A session itself asks to be put to sleep in the middle of its own turn (entity - the session, req - who sent it)."""
    req = sess.get("req") or {}
    if req.get("channel") == "session" and req.get("sender") == sess["name"] and sess["state"] == "busy":
        return None
    return closable(st, sess)


def not_timed_out(st, d):
    return None if not d.get("timed_out") else T("core.guards.timed_out", "the term has already fired")


def spec_open(st, d):
    """The task's spec is open (core/specmode.py): waiting for "Accept" or "Drop"."""
    if specmode.is_open(d):
        return None
    if isinstance(d.get("spec"), dict):
        return (T("core.guards.spec_accepted_already", "the spec of {id} is already accepted")
                if d["spec"].get("state") == "accepted" else
                T("core.guards.spec_dropped_already", "the spec of {id} is already dropped")).format(id=d["id"])
    return T("core.guards.spec_none", "task {id} has no spec").format(id=d["id"])


def spec_go_pending(st, d):
    """The spec was accepted, and the core's "accepted — do the work" message has not started a turn yet: a result now is
    still the spec's report (the owner accepted mid-turn), not the hand-in of the work."""
    go = (d.get("spec") or {}).get("go")
    if go and (st["messages"].get(go) or {}).get("state") in ("queued", "typing"):
        return None
    return T("core.guards.spec_go_started", "the work by the spec has started")


def spec_closed(st, d):
    """delegation.done waits for the spec: it is closed by "Accept" or "Drop", not by a hand-in."""
    return T("core.guards.spec_still_open", "the spec of {id} is open: \"Accept\" or \"Drop\" it").format(
        id=d["id"]) if specmode.is_open(d) else None


def executor_gone(st, d):
    """A delegation goes to the archive only if an executor did it and was removed (core.archived)."""
    sess = st["sessions"].get(d.get("session") or "") or {}
    return None if sess.get("state") == "removed" and sess.get("role") == "executor" else \
        T("core.guards.executor_kept", "the executor is not removed")


def _sender_is_hub(st, request):
    sender = st["sessions"].get(request.get("sender") or "")
    return bool(sender) and sender.get("kind") == "hub"


def from_hub_or_owner(st, request):
    """The hub session, the owner (page, Telegram, CLI outside sessions) or the core itself: daemon events - without a
    channel or with channel core (the hub raises itself, executors, system agents). Others - no: a working session asks
    the hub, a plugin process (channel plugin) is not promised this - it has its own facts, a notification to the owner
    and a message to a session."""
    if request.get("channel") in (None, "core") or from_owner(st, request) is None or (
            request.get("channel") == "session" and _sender_is_hub(st, request)):
        return None
    return HUB_OR_OWNER   # one reason for opening, removing and stopping sessions


def from_hub_owner_or_raiser(st, request):
    """Stop, interrupt, remove: as from_hub_or_owner - plus whoever raised this executor (friction summary 16.10:
    the raiser asked the hub 17 times to remove its own executors). A direction (an executor with a backlog) - only the
    hub or the owner. request["_raised_by"], request["_backlog"] - who raised it and the backlog (reducer)."""
    if from_hub_or_owner(st, request) is None or (request.get("channel") == "session" and request.get("_raised_by")
                                                  and request.get("sender") == request["_raised_by"]
                                                  and not request.get("_backlog")):
        return None
    return T("core.guards.hub_owner_or_raiser", "the hub or the owner does this (for one's own executor — also whoever "
             "raised it) — ask the hub: hub send hub \"…\"")


REVIVABLE = ("removed", "closed", "failed")   # the states `hub session revive` brings back (bin/hub)


def revives(st, request):
    """session.requested: opens a session or wakes a sleeping one (from_hub_or_owner) - and brings an executor back
    (revive, from a removed, closed or crashed state) for whoever raised it too, as for stop and remove: a direction
    (an executor with a backlog), a specialist, a session of the hub and an executor of someone else stay with the hub and
    the owner. request["_state"] - the session's state (reducer); the same conversation and folder, and the ceilings of a
    new request, are the reducer's (_raiser_revives)."""
    why = from_hub_or_owner(st, request)
    if why is None:
        return None
    return from_hub_owner_or_raiser(st, request) if request.get("revive") and request.get("_state") in REVIVABLE else why


def raiser_revives(st, request):
    """True: this revive is granted through the raiser (not the hub, the owner or the core): the reducer then holds it to
    the same conversation and folder and to the places of a new request (reducer._raiser_revives)."""
    return bool(request.get("revive")) and request.get("_state") in REVIVABLE and from_hub_or_owner(st, request) is not None \
        and from_hub_owner_or_raiser(st, request) is None


def asks_hub(st, request):
    """A working session asks the hub for a new session (from_session); a revive is not that request - it is refused with
    the reason (revives) instead of a message to the hub about a "new session"."""
    return from_session(st, request) if not request.get("revive") else (
        from_hub_owner_or_raiser(st, request) or HUB_OR_OWNER)


def steers(st, request):
    """Put a session to sleep (close, close-ready) or wake it: as from_hub_owner_or_raiser (stop, remove) - plus the
    session itself and the mini-hub whose bus the executor is on (minihub.hub_of: the owner speaks of the bus's
    executors to it). Another working session or a plugin process in its own name may not: the core would send a
    handover in the hub's name or raise a process nobody asked for; it writes to the session instead (hub send).
    request["_name"], request["_bus"] - the session it is about and the mini-hub of its bus (reducer)."""
    why = from_hub_owner_or_raiser(st, request)
    if why and request.get("channel") == "session" and request.get("sender") \
            and request.get("sender") in (request.get("_name"), request.get("_bus")):
        return None
    return why


def from_session(st, request):
    """A working session (not the hub) - its request goes to the hub. The refusal has the same reason as from_hub_or_owner:
    this variant comes after it, and the refused one (a plugin process) needs that reason, not "not from a session"."""
    if request.get("channel") == "session" and not _sender_is_hub(st, request):
        return None
    return HUB_OR_OWNER


def answers_permission(st, data):
    """A session's permission request is answered by whoever was asked: the owner, the hub by his rules, the core - and
    whoever raised this executor (it manages it anyway; the raiser answered its executor one step ahead, raised by the
    hook before asking a human). Another session, the asker itself, a plugin process - no. data["_raised_by"] - who
    raised it (reducer)."""
    if from_hub_or_owner(st, data) is None:
        return None
    if data.get("channel") == "session" and data.get("_raised_by") and data.get("sender") == data["_raised_by"]:
        return None
    return T("core.guards.permission_answer", "a session's permission is answered by the owner, the hub or whoever "
             "raised this executor — ask the hub: hub send hub \"…\"")


def from_owner(st, data):
    return None if data.get("owner") is True and owner_channel(data.get("channel")) else OWNER_WORD


def from_owner_strict(st, data):
    """Only the owner himself: the page, the terminal outside sessions, his Telegram — not a module handler's
    mod:<name> request that from_owner lets through (the hub bus: contacts and keys, core/busstate.py)."""
    if data.get("owner") is True and data.get("channel") in OWNER_CHANNELS:
        return None
    return OWNER_WORD


def from_owner_or_hub(st, data):
    if from_owner(st, data) is None or (data.get("channel") == "session" and _sender_is_hub(st, data)):
        return None
    return T("core.guards.away_mode", "the \"away\" mode is turned on by the owner or by the hub on their word")


def hub_asks_owner(st, data):
    """A session asks the owner with a form - the question goes as a card and does not hold the turn (for the hub since
    04.10; for everyone - the "tasks, specialists, executors" spec, release 1)."""
    ok = data.get("tool") == "AskUserQuestion" and data.get("interactive")
    return None if ok else T("core.guards.not_form", "not a question with a form")


def owner_away_interactive(st, data):
    away = (st.get("owner") or {}).get("state") == "away"
    return None if away and data.get("interactive") else T("core.guards.owner_here", "the owner is here")


# --- schedule: entity = {"job", "req", "now", "slot", "target"} -------------------------------------------------

def may_edit_job(st, e):
    job, req = e["job"], e["req"]
    if req.get("channel") == "core":   # a system job (core/system.py) is set and renewed by the core
        return None
    if job.get("system"):
        return T("core.guards.system_job", "{id} is a system job of the hub: it cannot be removed, handed over or "
                 "changed").format(id=job["id"])
    if from_owner(st, req) is None or (req.get("channel") == "session" and _sender_is_hub(st, req)):
        return None
    if job["state"] == "absent" or req.get("sender") in (job.get("by"), job.get("session")):
        return None
    if req.get("channel") == "session" and _raised_by(st, job.get("session") or job.get("by"), req.get("sender")):
        return None   # whoever raised the executor the job is of (its target, a command's setter): a removed one's is not stuck
    return T("core.guards.foreign_job", "the job is someone else's: the hub, the owner, the session that set it or the "
             "one it comes to may change it")


def _raised_by(st, name, sender):
    """Is session name an executor that sender raised (also a removed one: its record stays)."""
    sess = (st.get("sessions") or {}).get(name or "") or {}
    return bool(sender) and sess.get("role") == "executor" and sess.get("raised_by") == sender


def target_gone(st, e):
    if e["job"].get("command"):
        return T("core.guards.command_job", "a command job: it has no session")
    sess = st["sessions"].get(e["job"]["session"])
    return None if sess is None or sess["state"] in ("closed", "failed", "removed") else \
        T("core.guards.session_exists", "the session exists")


def recurring_target_gone(st, e):
    return target_gone(st, e) if e["job"]["kind"] != "at" else T("core.guards.one_off", "one-off")


def one_shot(st, e):
    return None if e["job"]["kind"] == "at" else T("core.guards.recurring", "recurring")


def missed_grace(st, e):
    from core.schedule import GRACE
    late = e["now"] - e["slot"]
    return None if e["job"]["kind"] != "at" and late > GRACE else \
        T("core.guards.not_late", "the slot is not more than 6 h late")


def past_until(st, e):
    from core.schedule import end
    return None if e["now"] > end(e["job"]) else T("core.guards.term_not_over", "the job's term is not over yet")


GUARDS = {"from_owner": from_owner, "from_owner_or_hub": from_owner_or_hub, "owner_away_interactive": owner_away_interactive,
          "hub_asks_owner": hub_asks_owner,
          "held_by_owner": held_by_owner, "closable": closable, "parked_free": parked_free, "own_turn": own_turn, "not_timed_out": not_timed_out,
          "from_hub_or_owner": from_hub_or_owner, "from_hub_owner_or_raiser": from_hub_owner_or_raiser,
          "steers": steers, "revives": revives, "asks_hub": asks_hub,
          "answers_permission": answers_permission, "executor_done": executor_done, "from_session": from_session,
          "executor_gone": executor_gone, "spec_open": spec_open, "spec_closed": spec_closed,
          "spec_go_pending": spec_go_pending,
          "may_edit_job": may_edit_job, "target_gone": target_gone, "recurring_target_gone": recurring_target_gone,
          "one_shot": one_shot, "missed_grace": missed_grace, "past_until": past_until}


def checker(st, entity):
    return lambda name: GUARDS[name](st, entity)
