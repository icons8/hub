"""State machines are data, not code (brief: declarative machines; owner decision 2026-09-29: a state machine
should solve all the problems).

A table row: event -> {from state: variant | [variants]}; a variant is (to state, [actions]) or
(to state, [actions], condition). Of several variants the first whose condition holds wins; if none does, the
event is rejected with the reason from the condition. An event with no row for the current state is rejected too.
Conditions are names; the reducer (core/guards.py) checks them against the core state without guessing anything.
Actions are symbolic names. Events from FACTS are recorded and do not change state. Names starting with "_" are
internal transitions made by the reducer itself.

A session has two independent lines: SESSION (process life and turn) and CLOSING (handing over work before sleep).
"""
from core.coretext import T, join

LIVE = ("starting", "idle", "busy", "dialog")
# No process, the conversation exists (owner decision 2026-10-25: "asleep" is a state, not a flag): asleep (closed by
# the human, put to sleep, handed over its work) is woken by any message; parked is free without a process
# (core/parking: "free" for the owner), woken by a message, "Put to sleep" turns it into asleep; retired is an
# executor that handed over its task or was stopped: messages do not wake it (they go to whoever raised it), only
# the owner or the hub wakes it explicitly.
SLEEP = ("asleep", "parked", "retired")

SESSION = {
    "initial": "absent",
    "transitions": {
        # A new session; a sleeping one with the same name is woken with its previous conversation (--resume). The
        # hub or the owner opens it; a working session does not open one itself but asks the hub.
        "session.requested": {s: [("starting", ["spawn"], "revives"), (s, ["ask_hub"], "asks_hub")]
                              for s in ("absent", "closed", "failed", *SLEEP, "removed")},
        # Remove from the list, like closing a terminal tab (owner 2026-09-29): only a non-live one; a live one is put
        # to sleep first (it hands over its work; the reducer stops a free executor at once, like stop). The
        # conversation on disk is untouched: `claude --resume <id>` opens it. An executor can also be removed by
        # whoever raised it.
        "session.remove_requested": {s: ("removed", [], "from_hub_owner_or_raiser") for s in (*SLEEP, "failed", "closed")},
        # A handed-over executor is woken by the owner, the hub or whoever raised it (reducer, _wake_who: the raiser
        # through the ceilings and the queue of a new request, _raiser_wakes; it replaces the 2026-10-04 rule "raise
        # only by hand or ask the hub": a merge request review takes days, and the agent needs its own executor).
        # Any sleeping session likewise (guards.steers): not another working session - it writes to it (hub send)
        "session.wake_requested": {s: ("starting", ["spawn"], "steers") for s in SLEEP},
        # SessionStart: startup, resume, clear. The session is ready for input.
        "session.started": {s: ("idle", []) for s in LIVE},
        # UserPromptSubmit: a turn began (our message by its label, or human input).
        "turn.started": {**{s: ("busy", []) for s in ("starting", "idle", "busy")},
                         # fell asleep (HANDOFF-DONE) but Claude Code managed to take the next message: record the
                         # turn (label = delivery), do not wake the session: the process is already released
                         # (2026-10-03, a message hung "in the queue")
                         **{s: (s, []) for s in SLEEP}},
        # Stop: the turn is over. From dialog too: the window was closed together with the turn (Esc).
        "turn.stopped": {**{s: ("idle", []) for s in ("idle", "busy", "dialog")},
                         **{s: (s, []) for s in SLEEP}},  # Stop comes after the HANDOFF-DONE result, when asleep
        # Claude Code registry: status=waiting means the session waits for a human (AskUserQuestion, permission).
        "dialog.opened": {s: ("dialog", []) for s in ("idle", "busy")},
        "dialog.closed": {"dialog": ("busy", [])},
        # The process vanished without a human SessionEnd (OOM, reboot, kill): raise it.
        "session.exited": {**{s: ("starting", ["respawn"]) for s in LIVE},
                           "closed": ("closed", []), "failed": ("failed", []), "removed": ("removed", []),
                           **{s: (s, []) for s in SLEEP}},
        # The process is alive but no SessionStart for longer than start_stuck_minutes: kill and raise the same
        # conversation; counted among raises without SessionStart, like a crashed one (MAX_RESTARTS).
        "session.start_stuck": {"starting": ("starting", ["kill", "respawn"])},
        # SessionEnd with reason prompt_input_exit/logout: closed by a human, so asleep; do not raise it on its own.
        "session.ended": {**{s: ("asleep", []) for s in LIVE}, **{s: (s, []) for s in SLEEP}},
        # Sleep: the process goes away, the conversation stays. Decided by the CLOSING machine.
        "_sleep": {"idle": ("asleep", ["release"]), "held": ("asleep", ["release"])},
        # the executor did its job and reported (core/executors.retiring): sleep of a handed-over one; a parked
        # one has no process
        "_retire": {"idle": ("retired", ["release"]), "held": ("retired", ["release"]), "parked": ("retired", [])},
        # Parking (core/parking): free with no task has no process, the conversation stays; a message raises it
        "session.parked": {"idle": ("parked", ["release"])},
        # "Put to sleep" a parked one: there is no process anyway; for the owner it is no longer "free" but asleep
        "_unpark": {"parked": ("asleep", [])},
        # a message to a sleeping session (reducer.new_message): delivered once it is raised
        "_wake": {s: ("starting", []) for s in SLEEP},
        # a handed-over one was woken by the owner or the hub with a message, or is no longer an executor (backlog,
        # role): just asleep
        "_unretire": {"retired": ("asleep", [])},
        # stop an executor now (owner, hub or whoever raised it; owner decision 2026-10-04: stop all executors fast):
        # end the process at once (release_now: terminated, not stdin closed; its exit, then end_tree, then a spawn of the
        # same session — the host's background release) and put it to sleep without handing over work; messages no longer
        # wake it. Not interrupt + release: after an interrupt claude reads what was inserted mid-step and takes a new
        # turn on it before it exits (2026-10-27: six tool calls after "stop")
        "session.stop_requested": {s: ("retired", ["release_now"], "from_hub_owner_or_raiser")
                                   for s in ("starting", "idle", "busy", "dialog", "held")},
        # "Stop" is like Esc in Claude Code: the turn is interrupted, the session stays (owner, hub or whoever
        # raised it).
        "session.interrupt_requested": {s: (s, ["interrupt"], "from_hub_owner_or_raiser") for s in ("busy", "dialog")},
        # stream-json host: a permission or AskUserQuestion waits for a human's answer.
        # A question form does not hold the turn: a question card, the answer arrives as a message (owner decision
        # 2026-10-04); if the owner is away, the same card, shown on return (2026-10-21: a question deferred as text
        # had nothing to answer it with after the return). Another window waiting for a human while he is away: the
        # core answers "away, question saved".
        "permission.requested": {s: [(s, ["detach_question"], "hub_asks_owner"),
                                     (s, ["defer_question"], "owner_away_interactive"), ("dialog", [])]
                                 for s in ("idle", "busy", "dialog")},
        # Answered by whoever was asked: the owner, the hub by his rules, the core or whoever raised the executor;
        # not another session, not the session itself, not a plugin.
        "permission.answered": {"dialog": ("busy", ["respond"], "answers_permission")},
        # hub take: the owner takes a session into his terminal; the hub releases the process and types nothing into it.
        # The owner or the hub: not another working session (its process would be killed), not the held session itself
        # (its terminal is the owner's, a second process would start beside it).
        "session.take_requested": {s: ("held", ["release"], "from_hub_or_owner") for s in LIVE},
        "session.give_requested": {"held": ("starting", ["spawn"], "from_hub_or_owner")},
        # The turn result from the stream-json host: the turn is over even if the Stop hook did not arrive: on "Stop"
        # claude sends a result with an error and Stop does not fire (Mac 2026-09-30, a session hung "working").
        "_turn_ended": {"busy": ("idle", []), "dialog": ("idle", [])},
        # A free session's tool set was changed: release it and raise the same one via --resume.
        "_tools_apply": {"idle": ("starting", [])},
    },
}

# While the session is with the owner, facts from his terminal (hooks) are recorded and do not change state.
for _event in ("session.started", "turn.started", "turn.stopped", "dialog.opened", "dialog.closed",
               "session.exited", "session.ended"):
    SESSION["transitions"][_event]["held"] = ("held", [])

# Handing over work before sleep: closable → HANDOFF-REQUEST → HANDOFF-DONE → sleep.
# A session with the owner in manual mode falls asleep at once: the turn is his. The hub puts it to sleep itself
# (owner decision 2026-09-29). A parked one (no process) falls asleep without handing over work: unparking (unpark)
# is the last variant, so that for a sleeping non-parked session the refusal reason stays the closable one.
# Who asks is checked before the table (reducer._closing, guards.steers).
CLOSING = {
    "initial": "none",
    "transitions": {
        "session.close_requested": {"none": [("none", ["sleep"], "held_by_owner"), ("none", ["sleep"], "executor_done"),
                                             ("handoff", ["handoff"], "closable"),
                                             ("none", ["unpark"], "parked_free")]},
        # CLOSE-READY is written by a session in the middle of its turn: "I'll finish the turn, then put me to sleep"
        # (live check 2026-09-30): the core remembers the request (ready) and starts the handover when the turn is
        # over and the session is free.
        "session.close_ready": {"none": [("handoff", ["handoff"], "closable"), ("ready", [], "own_turn")]},
        "_turn_done": {"ready": ("handoff", ["handoff"], "closable")},
        "_handoff_done": {"handoff": ("none", ["sleep"])},
        # The session left by itself (crashed, fell asleep, went to the owner): the handover is cancelled.
        "_cancel": {"handoff": ("none", []), "ready": ("none", [])},
    },
}

# A delegation: the executor's turn result = the answer to the owner; verdicts go to the hub.
# "Done" only by an explicit delegation.done (lesson of 2026-09-29: the board set "Done" on a reply).
DELEGATION = {
    "initial": "absent",
    "transitions": {
        "delegation.created": {"absent": ("working", ["task"])},
        "_asks_owner": {"working": ("waiting-owner", [])},
        "_owner_answered": {"waiting-owner": ("working", [])},
        # with an open spec (core/specmode.py) the result is an interim report and the task waits for the owner's
        # "Accept" in the spec state: no hand-in, no sleep, no reminders
        # accepted in the middle of a turn: that turn's result is still the spec's report, interim; the work by the
        # spec is the turn on the core's "accepted" message (spec_go_pending)
        "_answered": {**{s: [("spec", ["spec_ready"], "spec_open"), (s, ["spec_interim"], "spec_go_pending"),
                             ("replied", ["replied"])]
                         for s in ("working", "waiting-owner")}, "spec": ("spec", ["spec_ready"])},
        # a turn of the waiting executor broke off (an API error on a follow-up): the spec still waits, nothing is cut
        "_errored": {**{s: ("stopped", ["stopped"]) for s in ("working", "waiting-owner")}, "spec": ("spec", [])},
        # the person accepts the spec (or whoever raised it, with the owner's word): the same executor does the work by
        # it; dropped — the task is closed and the executor sleeps (who may — checked before the table,
        # delegations.spec_close)
        "delegation.spec_accepted": {**{s: (s, ["spec_accepted"], "spec_open") for s in ("working", "waiting-owner")},
                                     **{s: ("working", ["spec_accepted"], "spec_open") for s in ("spec", "stopped")}},
        "delegation.spec_dropped": {s: ("done", ["spec_dropped", "sleep_executor"], "spec_open")
                                    for s in ("working", "waiting-owner", "spec", "stopped")},
        # The executor answered "waiting for a tool", the tool was connected: the delegation is working again, the
        # next result is again the answer to the owner (live check 2026-09-30).
        "_continued": {s: ("working", []) for s in ("replied", "stopped")},
        # The executor began a turn itself (a background helper finished) after replying: the result goes to the
        # owner and the hub too, until the hub closes the delegation (owner decision 2026-09-30: "done, MR" was lost
        # after "waiting for the agent").
        # and from the archive: a removed executor was brought back (hub session revive), its result on the old
        # task is a continuation
        # a cut-off task with its spec still open goes back to waiting for "Accept", not to a hand-in
        "_followup": {"replied": ("replied", ["followup"]),
                      "stopped": [("spec", ["spec_ready"], "spec_open"), ("replied", ["replied"])],
                      "archived": ("replied", ["followup"])},
        "_session_failed": {s: ("stopped", ["stopped"]) for s in ("working", "waiting-owner")},
        "_timeout": {s: (s, ["timeout"], "not_timed_out") for s in ("working", "waiting-owner")},
        # "Done": the executor hands over its work and falls asleep if nothing else is open for it (owner decision
        # 2026-09-29)
        "delegation.done": {s: ("done", ["sleep_executor"], "spec_closed")
                            for s in ("working", "waiting-owner", "replied", "stopped", "archived", "spec")},
        # To the archive (core.archived, owner decision 2026-10-26): a handed-in task of a removed executor, cleaned
        # up by the core, not closed by the hub; texts in the record are the beginnings (full ones in tasks/<id>.md
        # and the journal), the delegation no longer holds messages.
        # Not "Done": only delegation.done still closes. A done task of a removed executor is trimmed in place and
        # stays done: "archived" can be continued by a revived executor (_followup), a closed task must not
        "_archive": {**{s: ("archived", ["trim"], "executor_gone") for s in ("replied", "stopped")},
                     "done": ("done", ["trim"], "executor_gone")},
    },
}
# Closed delegations: neither the board, nor "holds messages", nor "continue the task" sees them
ENDED = ("done", "archived")

# Owner: "I'm away [for N h]" / "I'm back": himself on the page/in the CLI, or the hub on his message; not sessions.
# When the period expires he is back automatically.
OWNER = {
    "initial": "present",
    "transitions": {
        "owner.away": {"present": ("away", ["away"], "from_owner_or_hub"),
                       "away": ("away", ["extend"], "from_owner_or_hub")},   # only a new period: away_since and the summary stay
        "owner.back": {"away": ("present", ["back"], "from_owner_or_hub")},
        "_away_expired": {"away": ("present", ["back"])},
    },
}

# A schedule job (owner decision 2026-09-30: a separate clock process, the hub cleans up declaratively).
# The clock (modules/scheduler) only reports "slot came" / "deadline passed"; what to do is decided by this table.
SCHEDULE = {
    "initial": "absent",
    "transitions": {
        # Set or change. A new one: anyone; an existing one: the owner, the hub, whoever set it
        # or the session it is addressed to.
        "schedule.set": {s: ("active", ["save"], "may_edit_job")
                         for s in ("absent", "active", "paused", "done", "expired", "removed")},
        # an expired one too: "not needed" in answer to the core's "the job ended" (schedule.ended below)
        "schedule.remove_requested": {s: ("removed", [], "may_edit_job") for s in ("active", "paused", "expired")},
        # Hand to another session (if the session is gone, the job is paused until the hub decides).
        "schedule.retarget": {s: ("active", ["retarget"], "may_edit_job") for s in ("active", "paused")},
        # The slot came. A recurring job that missed the slot by more than 6 h is skipped without a message; if the
        # session is gone, the hub gets "decide whom to give it to, or remove" (a recurring one is paused);
        # otherwise a message to the session (it wakes a sleeping one).
        "schedule.due": {"active": [("active", ["skip"], "missed_grace"),
                                    ("paused", ["orphan"], "recurring_target_gone"),
                                    ("done", ["orphan"], "target_gone"),
                                    ("done", ["fire"], "one_shot"),
                                    ("active", ["fire"])]},
        # The recurring job's deadline passed (until): whoever set it hears of it (30.10: watchers ended silently and
        # stood for days); two days before, a hubd timer has told them too (core/schedule.warn).
        "schedule.ended": {"active": ("expired", ["ended"], "past_until")},
        # The clock launches the job's command: a fact before the launch, so restarting the clock mid-command does
        # not run it twice.
        "schedule.started": {s: (s, ["started"]) for s in ("active", "paused", "done", "expired", "removed")},
        # The clock ran the job's command (monitors, owner decision 2026-09-30): the exit code goes into the job; the
        # state is the same. Code "unknown" means the launch is recorded but there is no result (the clock
        # restarted): no code in the job, the hub gets "outcome unknown" (no message for a removed job).
        "schedule.ran": {s: (s, ["ran"]) for s in ("active", "paused", "done", "expired", "removed")},
    },
}

MAX_RESTARTS = 3  # raises in a row without SessionStart, then failed (except the hub: it is always raised)
HUB_MAX_DELAY = 60  # the pause before raising a crashed hub grows 2, 4, 8… up to a minute

# A long command of a session that the core runs outside the session and the hub (`hub run`, core/hubrun.py): it
# outlives a hub restart and the OS service's, the result comes to the session as a core message.
RUN = {
    "initial": "absent",
    "transitions": {
        # A session asks: the core checks who, the mode, the term and the limits, and starts it (core/runjobs.py).
        "run.requested": {"absent": ("queued", ["save"], "may_run")},
        # The wrapper has taken the run (platforms/runwrap.py). A stop asked before that waits for it.
        "core.run_started": {"queued": ("running", ["started"]), "stopping": ("stopping", ["started"])},
        # Stop: the session itself, whoever raised it, the hub, the owner. The core stops it too when the session
        # goes (asleep, closed, removed, crashed — hubrun.follow).
        "run.stop_requested": {s: ("stopping", ["stop"], "may_stop_run") for s in ("queued", "running")},
        # The outcome, written by the core last — once what the run started is gone.
        "core.run_ended": {s: ("ended", ["ended"]) for s in ("queued", "running", "stopping")},
    },
}

MESSAGE = {
    "initial": "absent",
    "transitions": {
        "message.received": {"absent": ("queued", [])},
        "_pump": {"queued": ("typing", ["type"])},
        # "for information" (`hub send --fyi`): not a queue item: it starts no turn and wakes nothing; it rides with
        # the next message typed to the session and is delivered together with it (reducer._ride)
        "_aside": {"queued": ("aside", [])},
        # to a busy session mid-turn (owner decision 2026-10-01): a line on stdin arrives after the current step;
        # delivered as soon as typed, there will be no turn with the label
        "_inject": {"queued": ("delivered", ["type"])},
        # and one that did not wait for confirmation: the turn with its label did begin after all, so it arrived
        # (/compact took longer than the attempts)
        "_delivered": {"typing": ("delivered", []), "queued": ("delivered", []), "failed": ("delivered", []),
                       "aside": ("delivered", [])},
        "_retry": {"typing": ("typing", ["type"])},
        "_requeue": {"typing": ("queued", [])},
        # the session hook rejected a prompt with a label (exit 2): the core already counted the turn, the model
        # did not read it
        "_blocked": {"typing": ("failed", []), "delivered": ("failed", [])},
        "_fail": {"queued": ("failed", []), "typing": ("failed", []), "aside": ("failed", [])},
        # the executor was stopped (session.stop_requested) before it read a line inserted mid-turn (no turn of its
        # own yet, the turn still running): the killed process will not read it, and the next turn does not get it
        "_stopped": {"delivered": ("failed", [])},
        # the owner removes it from the queue himself, before the turn (page, "Delete" button), not waiting for the
        # session to see it
        "_cancel": {"queued": ("failed", []), "aside": ("failed", [])},
    },
}

# A refusal "the event has no row for this state" is put in words with a next step (friction summary 2026-10-16: 126
# refusals "X in idle"; after "remove in idle" the hub repeated the same command 12 times). said is what the state
# means; refusals is the phrase for an event ("*" for any state); <name> is substituted by the reducer. The code in
# parentheses is for journal analysis.
# The phrases are the core's (core/coretext.py): the session reads the English, the owner the same in his language.
# {name} is the session the event is about (refusal's name; without it — NAME_HOLDER, "<name>").
SESSION["said"] = {
    "absent": T("core.machines.session_absent", "no such session — the names: hub status"),
    "starting": T("core.machines.session_starting", "the session is coming up"),
    "idle": T("core.machines.session_idle", "the session is free"),
    "busy": T("core.machines.session_busy", "the session is working — a turn is in progress"),
    "dialog": T("core.machines.session_dialog", "the session is waiting for an answer to its question"),
    "held": T("core.machines.session_held", "the session is with the owner in the terminal"),
    "asleep": T("core.machines.session_asleep", "the session is asleep"),
    "parked": T("core.machines.session_parked", "the session is asleep (free, the process released)"),
    "retired": T("core.machines.session_retired", "the executor handed in its task and is asleep"),
    "removed": T("core.machines.session_removed", "the session is removed from the list"),
    "closed": T("core.machines.session_closed", "the session is closed"),
    "failed": T("core.machines.session_failed", "the session crashed")}
_HELD_WAKE = T("core.machines.wake_held", "the session is with the owner in the terminal — no need to wake it")
SESSION["refusals"] = {
    "session.remove_requested": {
        "removed": T("core.machines.remove_removed", "the session is already removed — no need to repeat"),
        **{s: T("core.machines.remove_working",
                "the session is working — a free or sleeping one is removed: stop an executor at once (hub session stop "
                "{name}), put another to sleep after its turn (hub session close {name}), then remove")
           for s in ("starting", "busy", "dialog")},
        "held": T("core.machines.remove_held",
                  "the session is with the owner in the terminal — remove it when they give it back")},
    "session.interrupt_requested": {"*": T("core.machines.interrupt_none", "no turn — nothing to interrupt")},
    "session.stop_requested": {**{s: T("core.machines.stop_asleep",
                                       "the session is already asleep — nothing to stop; to take it off the list — hub "
                                       "session remove {name}") for s in SLEEP},
                               "*": T("core.machines.stop_idle", "the session is not working — nothing to stop")},
    "session.wake_requested": {**{s: T("core.machines.wake_live",
                                       "the session is not asleep — no need to wake it; to write to it — hub send {name}")
                                  for s in LIVE},
                               "held": _HELD_WAKE,
                               "removed": T("core.machines.wake_removed",
                                            "the session is removed — bring it back with its conversation: hub session "
                                            "revive {name} (the hub or the owner; an executor — also whoever raised it)"),
                               "*": T("core.machines.wake_gone",
                                      "the session is closed or crashed — the hub or the owner can open it anew")},
    "session.requested": {**{s: T("core.machines.requested_live",
                                  "the session is already working — to write to it: hub send {name}") for s in LIVE},
                          "held": T("core.machines.requested_held",
                                    "the session is with the owner in the terminal — it comes back to the hub when they "
                                    "give it back")},
    "permission.answered": {"*": T("core.machines.answered_already",
                                   "this question is already answered or withdrawn — no need to answer")},
    "session.take_requested": {**{s: T("core.machines.take_asleep",
                                       "the session is asleep — wake it first: hub session wake {name}") for s in SLEEP},
                               "held": T("core.machines.take_held", "the session is already with the owner in the terminal"),
                               "*": T("core.machines.take_dead", "only a live session can be taken into the terminal")},
    "session.give_requested": {"*": T("core.machines.give_none",
                                      "the session is not with the owner in the terminal — nothing to give back")},
    # reload neither returns nor wakes a session: revive is for the hub, the owner and whoever raised an executor (guards.revives),
    # reload is wider (the session too)
    "session.reload_requested": {
        "failed": T("core.machines.reload_failed",
                    "the session crashed — bring it back with its conversation: hub session revive {name} (the hub or the "
                    "owner; an executor — also whoever raised it)"),
        "closed": T("core.machines.reload_closed",
                    "the session is closed — bring it back with its conversation: hub session revive {name} (the hub or the "
                    "owner; an executor — also whoever raised it)"),
        "removed": T("core.machines.reload_removed",
                     "the session is removed — bring it back with its conversation: hub session revive {name} (the hub or the "
                     "owner; an executor — also whoever raised it)"),
        **{s: T("core.machines.reload_asleep",
                "the session is asleep — wake it: hub session wake {name}, it comes up with the same conversation")
           for s in SLEEP},
        "held": T("core.machines.reload_held",
                  "the session is with the owner in the terminal — restart it when they give it back")},
    # the owner's "Continue" after a limit: a sleeping one continues with the next message to it, it is not raised for this
    "session.resume_requested": {
        "failed": T("core.machines.resume_failed",
                    "the session crashed — bring it back with its conversation: hub session revive {name} (the hub or the "
                    "owner; an executor — also whoever raised it)"),
        "closed": T("core.machines.resume_closed",
                    "the session is closed — bring it back with its conversation: hub session revive {name} (the hub or the "
                    "owner; an executor — also whoever raised it)"),
        "removed": T("core.machines.resume_removed",
                     "the session is removed — bring it back with its conversation: hub session revive {name} (the hub or the "
                     "owner; an executor — also whoever raised it)"),
        "asleep": T("core.machines.resume_asleep",
                    "the session is asleep — it continues with the next message to it: hub send {name}"),
        "held": T("core.machines.resume_held",
                  "the session is with the owner in the terminal — it continues when they give it back")},
}
CLOSING["said"] = {"none": T("core.machines.closing_none", "the session is not handing over"),
                   "handoff": T("core.machines.closing_handoff", "the session is handing over before sleep"),
                   "ready": T("core.machines.closing_ready", "the session falls asleep after its turn")}
CLOSING["refusals"] = {e: {"handoff": T("core.machines.close_handoff",
                                        "already handing over before sleep — it falls asleep by itself, no need to repeat"),
                           "ready": T("core.machines.close_ready",
                                      "already falls asleep after this turn — no need to repeat")}
                       for e in ("session.close_requested", "session.close_ready")}
DELEGATION["said"] = {"absent": T("core.machines.task_absent", "no such task"),
                      "working": T("core.machines.task_working", "the task is in progress"),
                      "waiting-owner": T("core.machines.task_waiting_owner", "the task is waiting for the owner"),
                      "replied": T("core.machines.task_replied", "the task has a reply"),
                      "stopped": T("core.machines.task_stopped", "the task broke off"),
                      "done": T("core.machines.task_done", "the task is closed"),
                      "archived": T("core.machines.task_archived",
                                    "the task is in the archive: handed in long ago, the executor removed")}
DELEGATION["refusals"] = {"delegation.done": {"done": T("core.machines.task_done_again",
                                                        "the task is already closed — no need to repeat")}}
OWNER["said"] = {"present": T("core.machines.owner_present", "the owner is here"),
                 "away": T("core.machines.owner_away", "the owner is away")}
SCHEDULE["said"] = {"absent": T("core.machines.job_absent", "no such job — the list: hub schedule list"),
                    "active": T("core.machines.job_active", "the job is active"),
                    "paused": T("core.machines.job_paused", "the job is paused"),
                    "done": T("core.machines.job_done", "the one-off job has already fired"),
                    "expired": T("core.machines.job_expired", "the job's term is over"),
                    "removed": T("core.machines.job_removed", "the job is removed")}
SCHEDULE["refusals"] = {e: {"removed": T("core.machines.job_removed_again",
                                         "the job is already removed — no need to repeat"),
                            "done": T("core.machines.job_done_again",
                                      "the one-off job has already fired — nothing to remove or hand over"),
                            # removing an expired one is allowed ("not needed"), so this is the refusal to retarget it
                            "expired": T("core.machines.job_expired_again",
                                         "the job's term is over — nothing to hand over; needed further — hub schedule "
                                         "extend <id>")}
                        for e in ("schedule.remove_requested", "schedule.retarget")}
MESSAGE["said"] = {"absent": T("core.machines.msg_absent", "no such message"),
                   "queued": T("core.machines.msg_queued", "the message is in the queue"),
                   "typing": T("core.machines.msg_typing", "the message is being typed"),
                   "delivered": T("core.machines.msg_delivered", "the message is already delivered"),
                   "failed": T("core.machines.msg_failed", "the message is withdrawn or not delivered"),
                   "aside": T("core.machines.msg_aside", "the message is for information — it comes with the next one")}
RUN["said"] = {"absent": T("core.machines.run_absent", "no such run — the list: hub run list"),
               "queued": T("core.machines.run_queued", "the run is starting"),
               "running": T("core.machines.run_running", "the run is going"),
               "stopping": T("core.machines.run_stopping", "the run is being stopped"),
               "ended": T("core.machines.run_ended", "the run has ended")}
RUN["refusals"] = {"run.stop_requested": {"stopping": T("core.machines.run_stopping_again",
                                                        "the run is already being stopped — no need to repeat"),
                                          "ended": T("core.machines.run_ended_again",
                                                     "the run has already ended — nothing to stop")},
                   "core.run_ended": {"ended": T("core.machines.run_ended_twice",
                                                 "the run has already ended — its outcome is recorded once")}}
STATE_WORD = T("core.machines.state_word", "state \"{state}\"")
NOT_NOW = T("core.machines.not_now", "{said} — not possible now; what is possible is shown in hub status")
NAME_HOLDER = T("core.machines.name_holder", "<name>")


def refusal(machine, current, event_type, name=None):
    """Why the event has no row for this state: a phrase (refusals, else said) and a code in parentheses. name — the
    session the next command is about (its {name}); none — the placeholder "<name>"."""
    row = machine.get("refusals", {}).get(event_type, {})
    text = row.get(current) or (row.get("*") if current != "absent" else None)   # no such thing: say so
    if not text:
        said = machine.get("said", {}).get(current) or STATE_WORD.format(state=current)
        text = said if " — " in said else NOT_NOW.format(said=said)
    if "{name}" in str(getattr(text, "_template", "")):
        text = text.format(name=name or NAME_HOLDER)
    return join(text, f" ({event_type} in {current})")


MAX_ATTEMPTS = 4
GAVE_UP = f"not confirmed after {MAX_ATTEMPTS} attempts"   # reason for failed: the core no longer types
CONFIRM_SECONDS = 30  # how long to wait for the turn with the label after typing
COMMAND_CONFIRM_SECONDS = 600  # Claude Code commands: the result of /compact on a big conversation takes minutes

# Facts: record them, do not change session/message state (except aliases, see identity).
FACTS = {
    "session.identified", "session.end_other", "session.compacted", "session.compacting",
    "host.done", "host.error", "core.rejected", "core.started", "session.said", "core.snapshot",
    "turn.tool", "turn.tool_result",
    "session.mcp_set",  # the host added a granted MCP to a running Claude Code (or it refused): the restart applies it anyway
    "plugins.changed",
    "plugins.consented",  # the owner (or the page code, marketplaces spec 3.6) allowed a plugin new permissions
    "release.watch",  # the former tag watcher of a publishing hub: earlier journals (and an old update part during the update) have it
    "secret.requested", "secret.denied", "secret.filled", "secret.failed",  # "Secrets" (modules/secrets): who, what, where; no values
    "secret.used",  # `hub secret exec`: a secret went into a command's environment (name, variable, program, exit code)
    "secret.saved", "secret.removed", "secret.copied",  # the owner on the Secrets tab: names and which parts changed; no values
    "watch.promise_skipped",  # watcher: a result rejected by the promise check, sampled into the journal (skip count)
    # settings that change how the core works (core/configwatch.py): the accepted state and an edit outside the page;
    # the fold keeps them in config_seen
    "config.seen", "config.changed_outside",
    # the app's code layer (core/appwatch.py): an edit of the layer, the release put back; the fold keeps them in app_code
    "app.code_modified", "app.code_restored", "app.moved",   # app.moved: the move from a git clone, in app_move
    "schedule.milestone_removed",  # the core removed a progress job (core/schedule._drop): which, why, after how many, the notice
    "settings.changed",  # the owner changed a setting on the page (hub name…): the page gets a snapshot   # the owner installed, enabled or removed a plugin: the page gets a snapshot and reloads
}


def step(machine, current, event_type, check=None):
    """(new state, actions) or None if there is no transition.

    check(condition) -> None if the condition holds, otherwise a reason. All variants with unmet conditions give
    None; the reason comes from decide()."""
    moved = decide(machine, current, event_type, check)
    return moved[:2] if moved[0] is not None else None


def decide(machine, current, event_type, check=None, name=None):
    """(to state, actions, None) or (None, None, the refusal reason); name — for the refusal's {name} (refusal)."""
    row = machine["transitions"].get(event_type, {})
    options = row.get(current)
    if options is None:
        return None, None, refusal(machine, current, event_type, name)
    reason = None
    for opt in options if isinstance(options, list) else [options]:
        guard = opt[2] if len(opt) > 2 else None
        why = check(guard) if guard and check else None
        if why is None:
            return opt[0], list(opt[1]), None
        reason = why
    return None, None, reason
