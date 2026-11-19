"""Delegations: the DELEGATION machine in core/machines.py.

The hub hands out a task with `delegation.created` (session, for=owner's message, context). The core builds the text
itself from a template: the owner's words verbatim, the context, the contract. The executor's turn result for this
task is the answer to the owner's message, and `REPLIED` for the hub; a turn with an error or a crashed session is
`STOPPED`; 90 minutes without a result is `TIMEOUT` (once). "Done" only by an explicit `delegation.done`.
"""
from core import askexpired, chain, coretext, forms, langdata, minihub, owner, specmode
from core.coretext import T
from core.guards import checker
from core.machines import DELEGATION, ENDED, decide
from core import tools
from core.tools import Rejected

HANDED_IN = ("replied", "done", "archived")   # task states of one whose executor reported (or the task is closed)
TIMEOUT_SECONDS = 90 * 60
# Ladder for a stuck task (delegations.stuck): the executor is free but there is no result: a reminder after 10 min
# of idleness, a second one 20 min later, and 30 min after that one fact to the hub (TIMEOUT; at 60 min of idleness,
# earlier than the 90-minute one). Counted from the end of the last turn or reminder: a busy session gets nothing.
# Journal replay 2026-09-30..10-07: 1 reminder per 204 tasks, insurance and not a stream
NUDGES = (10 * 60, 20 * 60, 30 * 60)
# Open after a reply (delegations.quiet; owner decision 2026-10-08: nudge open tasks without hub wait): the executor
# gave an interim reply, the task is not closed, and it is free and waiting for nothing: one reminder 30 min after
# the end of the turn, and 60 min later with no change one fact to the hub (once per task). Replay 09-30..10-07:
# 64 tasks with >=10 min of such silence
OPEN_NUDGE, OPEN_FACT = 30 * 60, 60 * 60
# A result within NUDGE_ANSWER after the reminder is taken as its answer. This is a heuristic on time alone: the result
# does not say what it answers, so a result that merely lands in the window counts too (then the countdown is not
# restarted and the fact to the hub comes on schedule - the safe side); 10 min is the usual time of a short check.
# The answer that goes to the hub (answered_reminder) is the result of the turn that read the reminder; by this
# window only for a reminder from an earlier core.
NUDGE_ANSWER = 10 * 60
# A ceiling on that count: OPEN_STALE after the reply (or the reminder) the step is due even if the executor kept
# working on other things, because its every turn moved "free since" forward and the reminder never came (a journal
# case: a task open for 21.8 h, 54 turns of other work, 5 parkings, 0 reminders). A sleeping executor (asleep,
# parked) is not woken: after OPEN_STALE the hub gets the one fact instead
OPEN_STALE = 2 * 60 * 60
# Reminders (step 0) of one session are spaced: tasks of one session that all reached OPEN_STALE in the same second gave
# it a pile of reminders in a row; the next one is due POKE_GAP after the latest reminder on its other tasks
POKE_GAP = 10 * 60
# A turn from a Claude Code monitor event (Monitor: "three more jobs are green, waiting…") is not forwarded at once:
# hold the last one and send it when events have been quiet for this many seconds, or replace it with a normal
# result (owner decision 2026-10-14: squeeze into one; in 16 min an executor forwarded 9 such lines to the
# assigning session and the hub, each waking both)
MONITOR_HOLD = 5 * 60
# Archive (core.archived, spec deleg-archive): a handed-in task of a removed executor after ARCHIVE_AFTER keeps only
# the beginnings of its texts in the record (full context in tasks/<id>.md, full reply in the journal and the
# transcript). 600 is the longest reader of the result (a departed executor's "last result" is last[:600]); 2 days
# is a x3 margin over the latest return of a removed one (hub session revive) over 25 days of journal
ARCHIVE_AFTER = 2 * 86400
CONTEXT_KEEP, REPLY_KEEP = 300, 600
TRIMMED = "the task text in the record is shortened — the full one: hub task show {did}"
NOTHING = "nothing to continue"   # answer to the core's "continue" (cut by a restart or limit) with nothing to continue
# the word earlier cores asked for: sessions of old conversations still answer with it
NOTHING_OLD = langdata.legacy("nothing")


def monitor_turn(head):
    """The turn was started by a monitor event (Claude Code inserted a <task-notification> with "Monitor event"),
    not by the end of a background command."""
    head = (head or "").lstrip()
    return head.startswith("<task-notification>") and "Monitor event" in head
# The second phrase is a live case of 2026-09-30: the session read "the turn result goes to the owner" as "end the
# turn with a report", wrote "now doing slice 1" and stopped. The interim one arrives anyway (continuation of the
# delegation).
# The second to last is "show milestones" (owner 2026-10-06): the session does not open the hub skill rule on an
# ordinary result (eval 0/2 -> 2/2).
# "You propose something visible" is 2026-10-06: a review of a PR that changes a page went to the owner as a question
# without a picture (the card has none).
# The last is "long turns without news" (owner 2026-10-06: "where did you vanish?", "that's awfully long"; eval
# 0/2 -> 2/2).
ASKS = "business decisions — to the owner as options (AskUserQuestion), wait for the answer"
CONTRACT = ("Do the technical steps yourself; business decisions — to the owner as options (AskUserQuestion), wait for the "
            "answer; report what was done. An interim report is not the end of the turn: keep working in this same turn, "
            "end it when the task is done or has hit an owner's decision. Made something visible (a page, a UI) — show "
            "it: a link and a screenshot or a video through `hub send-file`. Proposing something visible (a PR, a mockup, "
            "a UI option) — before the question form, send a \"this is how it will look\" snapshot (`hub shot` → `hub "
            "send-file`): a card has no picture. Something long (a full test run, Docker, a build — longer than ~5 min) — "
            "first a quick check of the main thing (one test, curl, a snapshot), the full run in the background "
            "(run_in_background), and end the turn with the line \"started X, will write the result\" (~N min only if it is "
            "known: the same run before, a measured pace; no measure — no number): the owner "
            "sees a long turn without news as a disappearance. A page answers 403 or a bot protection — do not get "
            "around it with search and caches: at once `hub tools request browser:<name>` and read it in the browser; the "
            "same with any missing tool (a service — its MCP). Do not ask the owner what you can find out yourself.")
# 2026-10-17: an executor began a turn with an English line between steps and ended it so ("I'm waiting for…"); in
# the first message of the turn the line is closer to the reply than the LANGUAGE rule in the system prompt. Turn
# replay (system prompt and messages from the transcript, Opus), first reply: Russian owner answered in English
# 5/12 -> 0/12 (with LANGUAGE below), English owner answered in Russian 0/12 -> 0/12; without the language name
# English slipped into Russian 4/12 — language unknown: no line
LANG_LINE = ("The language of everything you write is {lang}: the turn's result, \"waiting…\", every line between "
             "steps — also when command output, notifications and skills are in another language.")


def short_text(text, n=300):
    return text if len(text) <= n else text[:n] + "…"


RECORD = ("Task {did}: its record is `hub task show` (the owner, the hub and whoever set it read it); context, decisions "
          "and what you did along the way — append there with `hub task note \"<what>\"`.")


BAD_MODEL = T("core.delegations.bad_model", "model — one of {models} or a full claude-… id")
BAD_EFFORT = T("core.delegations.bad_effort", "effort — one of {efforts}")


def pinned(data, codex=False):
    """The executor's model and effort for the task (`--model`, `--effort`; owner decision 2026-10-06: model and effort
    go into the core): what is given is checked; an unknown value is refused at once, not an executor without the
    flag."""
    from core.catalog import EFFORTS, MODELS, known_model
    out = {k: str(data[k]) for k in ("model", "effort") if data.get(k)}
    if "model" in out and not known_model(out["model"], codex):
        raise Rejected(BAD_MODEL.format(models=", ".join(MODELS)))
    if "effort" in out and out["effort"] not in EFFORTS:
        raise Rejected(BAD_EFFORT.format(efforts=", ".join(EFFORTS)))
    return out


def lang_word(st):
    """The owner's language as a word, for the task's language line (LANG_LINE); None if unknown."""
    from core import i18n, reducer
    code = reducer.known_lang(st)
    return (forms.LANG_WORDS.get(code) or i18n.name(code)) if code else None


def task_text(*args, **kw):
    return task_parts(*args, **kw)[0]


def task_parts(words, mid, context, for_session=None, by=None, did=None, bus=None, lang=None, spec=None):
    """The task message text and where the context starts in it (characters; None without a context): the head and the
    tail around it are the core's frame, see brief_span."""
    # the task number and the recording rule go into every task, not only an executor's (owner 2026-10-04: we agreed
    # everything should be recorded and appended in the task, do sessions do it? no: the task came without a number
    # and without a word about recording)
    rec = RECORD.format(did=did) if did else ""
    line = " " + LANG_LINE.format(lang=lang) if lang else ""
    if for_session and words is None:
        via = "" if by == for_session else " (through the hub)"   # hub task new: the session sets the task itself
        head, tail = f"Task from session {for_session}{via}.", f"The result of this turn goes to {for_session}."
        # a mini-hub executor (bus): the owner's decisions go through whoever raised it, the form is closed to it
        # (core/minihub.py)
        rule = CONTRACT.replace(ASKS, minihub.BUS_ASKS.format(boss=bus)) if bus else CONTRACT
        # spec mode (core/specmode.py): the brief says it first — the task is the spec, not the work
        mode = specmode.EXECUTOR_LINE.format(did=did, path=spec, boss=for_session) if spec else ""
        return _join(head, mode, Brief(context or ""), rec, tail + " " + rule + line)
    if words is not None:
        head = f"Task from the owner (#{mid}). Their words:\n> " + words.replace("\n", "\n> ")
        tail = "The result of this turn goes to the owner as the reply to their message: write it as a reply to them."
    else:
        head, tail = "Task from the hub.", "The result of this turn goes to the hub."
    return _join(head, Brief(context or ""), rec, tail + " " + CONTRACT + line)


class Brief(str):
    """The context among the pieces of a task text: _join tells where it starts."""


def _join(*pieces):
    """The pieces on lines (empty ones dropped) and the offset of the Brief one (None if it is empty)."""
    out, at = [], None
    for x in pieces:
        if x:
            if isinstance(x, Brief):
                at = sum(len(y) + 1 for y in out)
            out.append(str(x))
    return "\n".join(out), at


def lead_ok(lead, context):
    """`lead` of a task (delegation.created): how many leading characters of its context are the setter's own words, the
    rest is the core's frame (the executor's rules, the direction's note). A whole number inside the context."""
    return isinstance(lead, int) and not isinstance(lead, bool) and isinstance(context, str) and 0 < lead <= len(context)


def brief_of(msg):
    """Where the setter's words lie in the task message text — [start, end) in UTF-16 units, as the page counts string
    positions; None when the core did not mark them (a task of an earlier version) or the text no longer holds them.
    The message keeps the span in characters (`brief`, set when the task is typed: the text moves only at its start,
    coretext.prepend moves the span with it)."""
    span, text = msg.get("brief"), msg.get("text")
    if not (isinstance(span, list) and len(span) == 2 and isinstance(text, str)):
        return None
    a, b = span
    if not all(isinstance(x, int) and not isinstance(x, bool) for x in span) or not 0 <= a < b <= len(text):
        return None
    units = lambda n: n + sum(ord(c) > 0xFFFF for c in text[:n])
    return [units(a), units(b)]


def move(st, d, event, ev, ctx, first=False):
    """A transition by the DELEGATION table and its actions. ctx: new_message, notify. first: the first real result
    after a silent "—", or the answer to the core's "open" reminder (it goes to the hub as a result, not as a
    continuation)."""
    to, todo, why = decide(DELEGATION, d["state"], event, checker(st, d))
    if to is None:
        raise Rejected(why)
    if d["state"] != to:
        d["since"] = ev["ts"]
    d["state"] = to
    name = d["session"]
    for act in todo:
        if act == "task":
            text, at = task_parts(d["words"], d["for"], d["context"], d.get("for_session"), d.get("by"), d["id"],
                                  minihub.boss(st, st["sessions"].get(name)), lang_word(st), spec=specmode.in_brief(d))
            # the setter's words inside the text (`lead` of the task): the page folds the rest, see brief_of
            brief = {"brief": [at, at + d["lead"]]} if at is not None and d.get("lead") else {}
            ctx["new_message"](st, d["message"], name, text,
                               ev["ts"], **brief,
                               origin=forms.from_session(d["by"], forms.is_hub(st, d["by"])) if d.get("by") else "", delegation=d["id"],
                               attachments=d.get("attachments") or [],
                               **({"frm": forms.origin_from("session", d["by"])} if d.get("by") else {}))
        elif act in ("replied", "followup"):
            text = d["reply"] or ""
            up, me = st["sessions"].get(d.get("for_session") or "") or {}, st["sessions"].get(name) or {}
            sent = False   # whether the full result was sent to the recipient in this turn
            # it wrote to it itself in this turn (`hub send`): the same result is not needed a second time (analysis
            # 2026-10-07: 22 of 101)
            # «—» (passed it on, a check with nothing new) is not a result: it only woke the assigning session with an
            # empty line; the state still moves, and the first real result after it goes out (owner 02.11). The raiser
            # of a mini-hub executor (minihub.boss: the mini-hub, or an executor that ran a subtask) gets no REPLIED for
            # it (_notify_hub): the "—" is how it learns the executor is done
            if d.get("for_session") and not got_result(me, d["for_session"], text) \
                    and (text.strip() != "—" or minihub.boss(st, me) == d["for_session"]):
                if up.get("kind") == "hub" and minihub.runner(me) and not d.get("onboarded"):
                    from core.executors import ONBOARD   # hub executor: onboarding of the direction (2026-10-04), once
                    text += ONBOARD.format(name=name)
                    d["onboarded"] = True
                ctx["new_message"](st, f"m{ev['seq']}x1", d["for_session"], text, ev["ts"],
                                   origin=forms.from_session(name, forms.is_hub(st, name)) + f", reply to task {d['id']}", task=d["id"],
                                   frm=forms.origin_from("session", name))
                sent = True
            # the task's recipient is the hub itself, and we already gave it the full result ("reply to task dN") in
            # this turn: a separate REPLIED "from the core" would duplicate it (2026-10-14). If the result was
            # suppressed (the session wrote to the hub itself), REPLIED stays: it is a signal on the task. For
            # another recipient REPLIED stays for the hub too.
            if up.get("kind") == "hub" and sent:
                continue
            # a continuation goes to whoever waits by itself (to sessions as the result above, to the owner into his
            # thread): the hub does not need it (owner 2026-10-16: to "REPLIED (continuation)" the hub answered "—"
            # 24 times out of 24); the hub gets it only when it is the recipient (here the result is suppressed:
            # the session wrote to it itself in this turn)
            if act == "followup" and not first and (d["for"] or up.get("state") not in (None, "removed", "closed", "failed")) \
                    and up.get("kind") != "hub":
                continue
            short = text if len(text) <= 300 else text[:300] + "…"
            target = (TO_OWNER.format(mid=d["for"]) if d["for"] else
                      SESSION_WORD.format(name=d["for_session"]) if d.get("for_session") else TO_HUB)
            more = act == "followup" and not first
            ctx["notify"](ev, (REPLIED_MORE if more else REPLIED).format(id=d["id"], name=name, target=target, short=short),
                          task=d["id"])
        elif act == "stopped":
            cut = cut_words(ev)
            ctx["notify"](ev, STOPPED_HUB.format(id=d["id"], name=name, cut=cut), task=d["id"])
            up = st["sessions"].get(d.get("for_session") or "") or {}
            if up and up.get("kind") != "hub":   # whoever waits for the result should know there will be none
                from core import executors
                # the session is gone (_session_failed): a message cannot be delivered to a crashed one (like reducer:
                # _received), and does not wake a stopped one: do not suggest "hub send" to it
                how = (HOW_SEND.format(name=name) if event != "_session_failed" else
                       HOW_WAKE.format(name=name, wakers=executors.wakers(st["sessions"].get(name) or {}, up["name"]))
                       if ev.get("type") == "session.stop_requested"
                       or (st["sessions"].get(name) or {}).get("state") == "retired" else
                       HOW_CRASHED.format(name=name, revivers=executors.revivers(
                           st["sessions"].get(name) or {}, up["name"])))
                # task: its turn on this message continues its task (like the reply to the task above)
                ctx["new_message"](st, f"m{ev['seq']}x1", up["name"],
                                   STOPPED_SESSION.format(id=d["id"], name=name, cut=cut, how=how), ev["ts"],
                                   origin=forms.ORIGIN_CORE, task=d["id"])
        elif act == "trim":
            if isinstance(d.get("context"), str) and len(d["context"]) > CONTEXT_KEEP:
                # the "Waiting for the owner's yes" line is the yes_note field: nobody needs its stub (open_task
                # removes it whole)
                d["context"] = without_yes_note(d, d["context"])
            for key, n in (("context", CONTEXT_KEEP), ("reply", REPLY_KEEP)):
                if isinstance(d.get(key), str):
                    d[key] = short_text(d[key], n)
            d.pop("held", None)
            d["trimmed"] = True
        elif act == "spec_ready":
            _spec_ready(st, d, ev, ctx)
        elif act == "spec_interim":
            _spec_ready(st, d, ev, ctx, interim=True)
        elif act == "spec_accepted":
            _spec_accepted(st, d, ev, ctx)
        elif act == "spec_dropped":
            _spec_dropped(st, d, ev, ctx)
        elif act == "sleep_executor":
            ctx["sleep"](name, ev)
        elif act == "timeout":
            d["timed_out"] = True
            ctx["notify"](ev, TIMEOUT_HUB.format(id=d["id"], name=name, minutes=TIMEOUT_SECONDS // 60), task=d["id"])


# the core's lines on a task to the hub and to whoever set it (core/coretext.py: the owner reads them in his language)
TO_OWNER = T("core.delegations.to_owner", "the owner on {mid}")
TO_HUB = T("core.delegations.to_hub", "the hub")
SESSION_WORD = T("core.delegations.session_word", "session {name}")
REPLIED = T("core.delegations.replied", "REPLIED {id}: {name} replied to {target}: \"{short}\"")
REPLIED_MORE = T("core.delegations.replied_more",
                 "REPLIED {id} (continuation): {name} replied to {target} again: \"{short}\"")
STOPPED_HUB = T("core.delegations.stopped_hub", "STOPPED {id}: {name} — {cut}, no reply to the owner")
STOPPED_SESSION = T("core.delegations.stopped_session",
                    "STOPPED {id}: {name} — {cut}, no result on the task. What it got done — hub show {name}; {how}")
HOW_SEND = T("core.delegations.how_send", "continue — hub send {name} \"…\"")
HOW_WAKE = T("core.delegations.how_wake",
             "it fell asleep (stopped, or handed over): wake it — hub session wake {name} {wakers}")
HOW_CRASHED = T("core.delegations.how_crashed",
                "it crashed: the task anew — hub task run <name> … --work normal|risky (bring it back with its "
                "conversation — hub session revive {name} {revivers})")
TIMEOUT_HUB = T("core.delegations.timeout", "TIMEOUT {id}: {name} has worked on the task longer than {minutes} minutes")
CUT_ERROR = T("core.delegations.cut_error", "the turn broke off with an error")
CUT_OWNER = T("core.delegations.cut_owner", "stopped by the owner")
CUT_SESSION = T("core.delegations.cut_session", "stopped by session {name}")
CUT_STOPPED = T("core.delegations.cut_stopped", "stopped")


def cut_words(ev):
    """Why the task broke off, in the words of the STOPPED line: a stop (session.stop_requested) says who stopped it —
    the owner stopped it on purpose, that is not an error (2026-10-27: "the turn broke off with an error" after the
    owner's stop)."""
    if ev.get("type") != "session.stop_requested":
        return CUT_ERROR
    data = ev.get("data") or {}
    if tools.from_owner(data):
        return CUT_OWNER
    return CUT_SESSION.format(name=data["sender"]) if data.get("sender") else CUT_STOPPED


# who wrote a line of the task record: the core's phrases — the record is written in the hub's language (daemon task_note)
BY_OWNER = T("core.delegations.by_owner", "owner")
BY_CORE = T("core.delegations.by_core", "core")
BY_HUB = T("core.delegations.by_hub", "hub")
SET = T("core.delegations.set", "task set: {who} → {name}")
SET_WORDS = T("core.delegations.set_words", "task set: {who} → {name} (the owner's words {src})")
CLOSED = T("core.delegations.closed", "task closed ({who})")
RESULT = T("core.delegations.result", "result: ")
INTERIM = T("core.delegations.interim", "interim result (executors are working): ")
GONE_SESSION = T("core.delegations.gone",
                 "session {name} is gone (it left) — give the task to a live session or a new executor: hub task run "
                 "<name> … --work normal|risky")
DORMANT = T("core.delegations.dormant",
            "{name} is asleep after handing in its task — wake it with the task: hub session wake {name} \"<what to do>\" "
            "(the owner, the hub or whoever raised it), or give the task to a new executor: hub task run <name> … "
            "--work normal|risky")
SELF_TASK = T("core.delegations.self_task", "you do not set a task for yourself — do it yourself")
BUSY_FOR = T("core.delegations.busy_for",
             "{src} already has task {id} at {session} — the clarification goes there: hub send {session} --about {id}")
# its start is forms.YES_NOTE_HEADS (open_task finds the line by it)
YES_NOTE = T("core.delegations.yes_note",
             "Waiting for the owner's yes: {tools}. Do not ask them yourself; when it is connected, the core will write.")
PASSED = T("core.delegations.passed",
           "Task {id}: {name} handed it to {names} (executors); the answer will come after their result and the check.")


def trail(st, did, who, text):
    """A line in the task's record (tasks/<id>.md), written by the daemon (action task_note); the core keeps the task's
    trail, the model only adds to it (owner decision 2026-10-04: everything that is raised must write to task.note,
    so nothing is lost)."""
    if did:
        # a phrase of the core keeps its marks (the record is written in the hub's language); other text on one line
        line = text if getattr(text, "spans", ()) and len(text) <= 700 else " ".join(str(text).split())[:700]
        st.setdefault("_notes", []).append({"delegation": did, "who": who, "text": line})


def created(st, ev, data, ctx):
    from core import identity
    asked = str(data.get("session") or "").strip()
    gone = st["sessions"].get(asked)
    if gone and gone["state"] == "removed":
        # the address of a departed executor leads to whoever raised it, for messages; a task is not needed there
        # (2026-10-04: it went to the hub itself and became "the owner's answer" to its question)
        raise Rejected(GONE_SESSION.format(name=asked))
    from core import executors
    if executors.dormant(gone):  # handed in its task and asleep: a delegation does not wake it, the owner or hub does
        raise Rejected(DORMANT.format(name=asked))
    name = identity.resolve(st, asked)
    if not name or st["sessions"][name]["state"] in ("closed", "failed"):
        raise Rejected("no such session")
    if name == data.get("sender"):
        raise Rejected(SELF_TASK)
    words, src, files, yes_note = None, data.get("for"), [], None
    if src:
        msg = st["messages"].get(src)
        if not msg:
            raise Rejected("no such owner message")
        why = tools.cited_task(msg)
        if why:
            raise Rejected(why)
        # one owner message is one task (spec section 4): a second path of the same request does not breed parallel
        # work; "working" and "waiting for the owner" count as busy; a replied one does not block setting it again
        # (after "can't do it" and the like)
        busy = next((d for d in st["delegations"].values()
                     if d.get("for") == src and d["state"] in specmode.HELD), None)
        if busy:
            raise Rejected(BUSY_FOR.format(src=src, id=busy["id"], session=busy["session"]))
        words, files = msg["text"], list(msg.get("attachments") or [])
    waiting = [r["tool"] for r in st["sessions"][name].get("requests") or [] if r["state"] == "needs-owner"]
    if waiting:  # the question is already with the owner: the session does not ask him itself (2026-09-30)
        note = YES_NOTE.format(tools=", ".join(waiting))
        data = {**data, "context": "\n".join(x for x in (data.get("context"), note) if x)}
        yes_note = note   # its own line as a field (core/forms.py): open_task removes it once the tool is connected
    from core.catalog import codex_state
    codex = codex_state(st)
    pin = pinned(data, codex)   # model and effort if the hub gives the task to an executor (hub task new --model/--effort)
    # the kind of work and the limit-saving pair (hub task new --work normal, core/budget.py) go there too
    if data.get("work") in ("normal", "risky"):
        pin["work"] = data["work"]
    if isinstance(data.get("economy"), dict) and pinned(data["economy"], codex):
        pin["economy"] = pinned(data["economy"], codex)
    # the start of the chain (core/chain.py): the owner's words; for an executor `hub task run`, the turn in which it
    # was asked; otherwise the turn of whoever sets it. An executor's subtask (parent) continues its task: the reply
    # to it is the turn of that task; an executor's `hub task run` task is a subtask of the one it was asked for (the
    # asker's turn may already be over)
    sender = data.get("sender") if data.get("channel") == "session" else None
    asked_root, asked_parent = st["sessions"][name].pop("task_root", None), st["sessions"][name].pop("task_parent", None)
    asked_minihub = st["sessions"][name].pop("task_minihub", None) and asked_parent   # a mini-hub subtask
    root = (chain.root(st, src) if src else None) or asked_root or chain.turn_root(st, sender)
    parent = asked_parent or chain.turn_task(st, sender)
    # set in a turn started by a schedule firing (limit saving, core/budget.py), not by a live word of the owner;
    # with the owner's words (--for <message id>) it is live
    sched = st["sessions"][name].pop("task_sched", None) or chain.turn_scheduled(st, sender)
    did = f"d{ev['seq']}"
    d = st["delegations"][did] = {
        "id": did, "session": name, "for": src, "words": words, "context": data.get("context"),
        **({"lead": data["lead"]} if lead_ok(data.get("lead"), data.get("context")) else {}),   # see brief_span
        **({"yes_note": yes_note} if yes_note else {}),   # the "Waiting for yes" line in context
        "for_session": data.get("for_session"),
        **({"card": str(data["card"])} if data.get("card") else {}),   # the card his words answer
        **({"board": dict(data["board"])} if isinstance(data.get("board"), dict) else {}),   # board card (core/board.py)
        "by": data.get("sender"), "message": f"m{ev['seq']}", "state": DELEGATION["initial"], "created": ev["ts"],
        "since": ev["ts"], "deadline": ev["ts"] + TIMEOUT_SECONDS, "timed_out": False, "reply": None,
        "attachments": files,  # the owner's attachments go to the executor along with his words
        "root": root, **({"parent": parent} if parent else {}),
        **({"minihub": True} if asked_minihub else {}),   # raised by a mini-hub: its task waits for it (minihub.waiting_on)
        **({"via_schedule": True} if sched and not src else {}),
        **pin,
    }
    folder = st["sessions"][name].pop("task_spec", None)   # raised with --spec (core/specmode.py)
    if folder:
        d["spec"] = {"path": specmode.rel_path(did if folder is True else folder), "state": "open"}
    move(st, d, "delegation.created", ev, ctx)
    who = d.get("for_session") or (data.get("sender") if data.get("channel") == "session" else BY_OWNER)
    trail(st, did, BY_CORE, (SET_WORDS if src else SET).format(who=who, name=name, src=src))


def continued(st, name, mid, ev, did):
    """A tool was connected for the executor: delegation did (open_task) continues with the core's message mid. Earlier
    ones are already replaced by the new one (2026-09-30: an old one came back "to work" next to a newer one and
    just hung)."""
    d = st["delegations"].get(did)
    if d and decide(DELEGATION, d["state"], "_continued")[0] is not None:
        d["state"], d["since"], d["message"] = "working", ev["ts"], mid
        d["deadline"], d["timed_out"] = ev["ts"] + TIMEOUT_SECONDS, False
        d.pop("nudges", None), d.pop("nudged_at", None)   # a continuation gets a new ladder


def on_turn(st, name, marker, head=None, fresh=True, now=None, also=None):
    """The executor began a turn with a task: it is the delegation's turn, and its result will be the reply. A turn
    without a label (Claude Code woke the session itself: a background helper finished) continues the delegation,
    only if the session worked on nothing else since (2026-10-01: the result of a background turn on one session's
    message went to another, by an old delegation)."""
    sess = st["sessions"][name]
    _read_reminder(st, sess, marker)
    msg = st["messages"].get(marker or "") or {}
    plain = marker and not forms.is_core(msg) and not any(msg.get(k) for k in ("for_delegation", "task", "delegation"))
    if (fresh or plain) and not sess.get("delegation") and sess.get("service_held"):
        # the tasks a service turn set aside (below) are the session's again: a new turn, or a plain message inserted
        # mid-turn (one naming a task makes the turn that task's, below). Kept aside until then, a late second result
        # of the service turn does not answer them either; an urgent task inserted mid-turn holds the turn, so they
        # wait for the next one, not dropped. One closed or handed in meanwhile is not taken back
        held = sess.pop("service_held")
        held = [did for did in ([held] if isinstance(held, str) else held)   # a single id: the PR's first version
                if (st["delegations"].get(did) or {}).get("session") == name
                and st["delegations"][did]["state"] in ("working", "waiting-owner", "spec")]
        if held:
            sess["delegation"] = held[0]
            rest = [did for did in held[1:] if did not in sess.get("also_tasks", [])]
            if rest:
                sess.setdefault("also_tasks", []).extend(rest)
    if fresh:   # a new turn: askers of an earlier one that ended without a result are not waiting on this one
        sess["sent_in_turn"] = []   # whom the session wrote to in this turn: the core does not forward their result (on_result)
        sess.pop("sent_chars", None)   # and its longest message to each (got_result)
        sess.pop("answer_to", None)   # (a notice inserted mid-turn is the same turn: it forgets neither)
    if marker or fresh:   # a notification inserted mid-turn does not make it a monitor turn (review 2026-10-14)
        sess["monitor_turn"] = not marker and monitor_turn(head)
    if not marker:
        d = st["delegations"].get(sess.get("last_task") or "")
        if d and d["session"] == name and d["state"] in ("replied", "stopped", "spec"):
            sess["delegation"] = d["id"]
        return
    _expect_answer(st, sess, msg, fresh)
    if fresh and (sess.get("delegation") or sess.get("also_tasks")) and _service(msg) \
            and all(_service(st["messages"].get(m) or {}) for m in also or () if isinstance(m, str)):
        # a task carried from an earlier turn (it ended with no text) and a turn started by a core note about another
        # session's hand-in ("an executor handed in, record the lessons"): its reply is about the note, not the task's
        # result (journal 13.11: "Already recorded. —" closed the owner's task and went to the hub as REPLIED). The
        # task waits for the next turn, and so does an urgent one carried with it. Not if the turn took other lines
        # too (also): they may be about the task
        sess["service_held"] = [did for did in (sess.pop("delegation", None), *sess.pop("also_tasks", [])) if did]
        return
    cur = st["delegations"].get(sess.get("delegation") or "")
    if msg.get("injected") and msg.get("delegation") and cur and cur["id"] != msg["delegation"] \
            and cur["session"] == name and cur["state"] in ("working", "waiting-owner", "replied", "stopped", "spec"):
        # an urgent task inserted mid-turn of another (`--now`, owner 2026-10-09): the turn stays that one's, the
        # result goes to both
        also = sess.setdefault("also_tasks", [])
        if msg["delegation"] not in also:
            also.append(msg["delegation"])
        return
    if not forms.is_core(msg):  # a core service message ("finish CLAUDE.md") does not change what the session works on
        sess["last_task"] = None
    did = msg.get("for_delegation")   # the owner's answer to a question on a delegation: the turn of that delegation
    if did and cur and cur["id"] != did and cur["state"] in ("working", "waiting-owner", "spec"):
        # the answer was inserted mid-turn of another delegation: the turn stays its (2026-10-04: the analysis
        # result and the next question went to the owner under the message about a migration, since the answer to
        # its question arrived mid-turn); that delegation no longer "waits for the owner": he answered
        other = st["delegations"].get(did) or {}
        if other.get("session") == name and other.get("state") == "waiting-owner":
            other["state"] = decide(DELEGATION, other["state"], "_owner_answered")[0]
        did = None
    if did and (st["delegations"].get(did) or {}).get("session") == name:
        d = st["delegations"][did]
        if d["state"] == "waiting-owner" and forms.is_core(msg) and _asked(sess, d, now, after=msg.get("created")):
            # the core's "continue" after a cut carries the cut turn's task, but the session asked the owner about it
            # after the "continue" was queued (it went on in another turn): no answer of the owner, the turn is not
            # the task's (journal 12.11: the late "continue" took the task back to work and its "nothing to continue"
            # became the task's result). A card the cut turn itself asked does not count: its continuation is the task's
            did = None
        elif d["state"] == "waiting-owner":
            d["state"] = decide(DELEGATION, d["state"], "_owner_answered")[0]
        if did and d["state"] in ("working", "replied", "stopped", "spec"):
            sess["delegation"] = sess["last_task"] = did
    for d in st["delegations"].values():
        # "waits for you" too: the mark could remain from a question on another matter (2026-10-03: a fork's turn did
        # not attach, the result did not become a reply, the delegation stayed "working" forever); a turn by the
        # delegation's label is its turn
        # and the replied one too: the late result of an interrupted turn closed the continuation after "Connected"
        # before its turn began (journal analysis 2026-10-07: the continuation's result was lost)
        if d["message"] == marker and d["session"] == name and d["state"] in ("working", "waiting-owner", "replied",
                                                                               "stopped", "spec"):
            if d["state"] == "waiting-owner":
                d["state"] = decide(DELEGATION, d["state"], "_owner_answered")[0]
            sess["delegation"] = sess["last_task"] = d["id"]
    sub = st["delegations"].get(msg.get("task") or "")
    up = st["delegations"].get((sub or {}).get("parent") or "")
    if not sess.get("delegation") and up and up["session"] == name and sub.get("for_session") == name \
            and up["state"] in ("working", "replied", "stopped"):
        # the reply to a subtask the session set during its own task continues that task (2026-10-08: the executor's
        # result after a subtask reply reached neither the owner nor the hub). Only this turn (delegation): the
        # parent cannot be pinned in last_task, otherwise the next label-less turn about something else would go to
        # its owner (2026-10-14: the result of an unrelated turn went to that task's owner)
        sess["delegation"] = up["id"]
    if not sess.get("delegation"):
        _followup(st, sess, msg)


def _service(msg):
    """A core note about another session's hand-in (an executor's lessons, reducer._lessons_note and _late_note): about
    no task of this one. Named by a field, not guessed from what the message lacks: a core message about the task does
    not always carry it (a second "Connected" while the task is already in work, roll-up of a week: the executor's
    report would not have reached whoever set the task)."""
    return forms.is_core(msg) and bool(msg.get("lessons_of") or msg.get("late_note"))


def born(d):
    """The order of tasks by when they were made: the event time is rounded to the millisecond, so two tasks of one session
    can tie, and the later one (the greater number) is the newer. A key for max/sorted over delegations."""
    return d["created"], int(d["id"][1:]) if d["id"][1:].isdigit() else 0


def done_for(st, name, sender, archived=False):
    """Tasks of executor name from sender that it handed in (reply or break); its next turn continues them;
    archived also includes those gone to the archive (a removed one was brought back: _followup)."""
    states = ("replied", "stopped", "spec", "archived") if archived else ("replied", "stopped", "spec")
    return [d for d in st["delegations"].values() if d["session"] == name and d.get("for_session") == sender
            and d["state"] in states]


def _followup(st, sess, msg):
    """A repeat turn of an executor on a message from the session that raised it (`hub send`, `hub session wake "…"`)
    continues that session's task: the result goes to it (hang analysis 2026-10-06: 13 of 17 such results were lost).
    Only the executor of this session: for a specialist and a neighbour every letter is not a continuation of an old
    task (analysis of the whole journal 2026-10-07: otherwise 110 extra forwards, 41 of them duplicates; 2026-09-30: a
    turn on someone else's message is not a continuation). `--about <id>` names one.
    A direction (role executor with a backlog) here is still treated as an executor: we do not drop its result to
    the raiser without analysing delivery (2026-10-18, journal roll-up: dropping it changed the forwards of
    directions' results)."""
    sender = msg.get("sender")
    if not sender or forms.is_core(msg) or sess.get("role") != "executor" or sess.get("raised_by") != sender:
        return
    mine = [d for d in done_for(st, sess["name"], sender, archived=True) if d["created"] <= msg["created"]]
    named = [d for d in mine if d["id"] == msg.get("about")]
    d = named[0] if named else max(mine, key=born, default=None)
    if d:
        sess["delegation"] = sess["last_task"] = d["id"]


def nothing(text):
    """The result "nothing to continue" (in quotes, emphasized, with a period too) answers the core's "continue"."""
    return (text or "").strip().strip("«»\"'*`_ ").rstrip(".!").lower() in (NOTHING, NOTHING_OLD)


RELAY_MIN = 20   # a shorter result is an acknowledgement, not an answer ("—", "ok")
BREAK_WINDOW = 3 * 3600   # a result this long after the owner's task to the session broke is still its answer


def after_break(st, sess, ev, text, took):
    """The turn result answers the owner's own task to this session that broke on an error (stopped) before it (owner
    decision 18.11, q2575460): the turn after the break is often not the task's (the hub asks "where is the plan?", the
    core says "continue"), and the report went to the folded "Sent on their own". Only the first such report (the task
    remembers it, break_answered), while that task is the session's latest and his (a session that set a task gets the
    report itself), within BREAK_WINDOW of the break, on words of whoever gave it the task (the hub) or the core's own
    (not a note about another session's hand-in), and a real line: RELAY_MIN characters, not a "—" pass-on line. A turn
    without a marker after the break was his already (on_turn: last_task). text: as the owner sees it
    (reducer.shown_text); took: the messages the turn took."""
    d = max((x for x in st["delegations"].values() if x["session"] == sess["name"]), key=born, default=None)
    if not d or d["state"] != "stopped" or not d.get("for") or d.get("for_session") or d.get("break_answered"):
        return False
    def theirs(m):   # written after the break by the core (not a note about a hand-in) or by whoever gave the task: the hub
        who = (forms.sender(m) or {}).get("name")
        return (m.get("created") or 0) >= d["since"] and (
            (forms.is_core(m) and not _service(m)) or (who and who == d.get("by"))
            or (st["sessions"].get(who or "") or {}).get("kind") == "hub")
    if not took or not all(theirs(m) for m in took):
        return False
    text = (text or "").strip()
    if ev["ts"] - d["since"] > BREAK_WINDOW or len(text) < RELAY_MIN or text.startswith("—"):
        return False
    d["break_answered"] = ev["seq"]
    return True


def _expect_answer(st, sess, msg, fresh):
    """A session wrote to this one with a plain `hub send` (no task, not `--fyi`, not a core relay of a result): its
    answer is `hub send` back. A session that answers in the turn's text is seen only by the owner's page; the asker
    waits for nothing (the asker waited and re-asked later). The turn result then goes to the asker by the core
    (on_result), as for a task, but only if the asker signalled that it waits (`hub waiting`, see _relay_result)."""
    f = forms.sender(msg)
    if not f or f["kind"] != "session" or f["name"] == sess["name"] or msg.get("fyi") or msg.get("relay") \
            or msg.get("delegation") or msg.get("task") or msg.get("schedule"):
        return
    up = st["sessions"].get(f["name"]) or {}
    if not up or up.get("kind") == "hub" or up.get("state") in ("removed", "closed", "failed"):
        return
    waits = sess.setdefault("answer_to", []) if fresh else sess.get("answer_to")
    if waits is not None and not any(w["to"] == f["name"] for w in waits):
        waits.append({"to": f["name"], "msg": msg["id"]})


def got_result(sess, who, text):
    """Did `who` get this turn's result already: the session wrote to it in this turn itself (`hub send`), one message
    not shorter than the result. A short line ("started, ten minutes"), or several of them, is not the result: the
    result still goes (review of PR #429: such a line, then a notice inserted mid-turn, and the raiser never got the
    long report). A message an older core recorded (in sent_in_turn, not in sent_chars: this core writes both together)
    keeps the old rule: any message is it."""
    if who not in (sess.get("sent_in_turn") or []):
        return False
    chars = sess.get("sent_chars") or {}
    return who not in chars or chars[who] >= len((text or "").strip())


def relay_targets(st, sess, data):
    """Whom the core relays this turn's text result to (pure; _relay_result sends it): those who wrote to the session
    in this turn, were not answered by `hub send` and said they wait (`hub waiting`, still fresh: not written to
    since). Not for a task's turn: its result is the reply. A report sent with a plain `hub send` is not a question."""
    if sess.get("delegation") or sess.get("also_tasks") or data.get("is_error") \
            or len((data.get("text") or "").strip()) < RELAY_MIN:
        return []
    return [w for w in sess.get("answer_to") or []
            if not got_result(sess, w["to"], data.get("text")) and w["to"] != sess["name"]   # it wrote to it itself
            and (wait := (st["sessions"].get(w["to"]) or {}).get("waiting")) and "stale" not in wait]


def _relay_result(st, sess, ev, data, ctx):
    """The turn ended: the result goes to those relay_targets names."""
    waits = relay_targets(st, sess, data)
    sess.pop("answer_to", None)
    text = (data.get("text") or "").strip()
    for w in waits:
        ctx["new_message"](st, f"m{ev['seq']}x2", w["to"], text, ev["ts"],
                           origin=forms.from_session(sess["name"], sess.get("kind") == "hub") + f", turn result on your {w['msg']}", relay=True,
                           frm=forms.origin_from("session", sess["name"]))


def on_result(st, ev, data, ctx):
    sess = st["sessions"].get(ev.get("entity"))
    if not sess:
        return
    mine = sess.get("delegation") or sess.get("also_tasks")
    if not mine:
        _relay_result(st, sess, ev, data, ctx)
    else:
        sess.pop("answer_to", None)   # the result is the reply to the task: it reaches the assigner (and the asker, if the same)
    # the turn result is the reply to the turn's task and to urgent ones inserted mid-turn (`--now`)
    for did in [sess.pop("delegation", None)] + sess.pop("also_tasks", []):
        _answered(st, sess, did, ev, data, ctx)


def _asked(sess, d, now, after=None):
    """The task has an open card to the owner (its turn ended on a question); `after`: one asked later than that."""
    return any(a.get("delegation") == d["id"] and (after is None or a["since"] > after)
               for a in owner.open_asks(sess, now))


def _takes_reply(sess, d, now):
    """Can the task take a turn result as its reply? Not when it is closed (done) or the turn ended on a question
    card: the delegation then waits for the owner, and his answer will attach the turn again."""
    if not d or d["state"] not in ("working", "waiting-owner", "replied", "stopped", "archived", "spec"):
        return False
    return not (d["state"] == "waiting-owner" and _asked(sess, d, now))


def reply_lands(sess, d, data, now):
    """Will the turn result be recorded as the reply to task d, i.e. reach whoever set it (_answered)? The reducer asks
    this before the result is applied, to see whether the assigner can tell the owner in the executor's stead."""
    later = bool(d) and d["state"] in ("replied", "stopped", "archived")
    return _takes_reply(sess, d, now) and not (later and nothing(data.get("text")))


def _answered(st, sess, did, ev, data, ctx):
    d = st["delegations"].get(did) if did else None
    if not _takes_reply(sess, d, ev["ts"]):
        return
    later = d["state"] in ("replied", "stopped", "archived")  # a continuation after a reply: an error is not a new STOPPED
    # for an already handed-in task, "nothing to continue" in answer to the core's "continue" is not a second reply
    # (roll-up 2026-10-16: the continuation came after the handover and went to the assigner)
    text = "" if later and nothing(data.get("text")) else (data.get("text") or "").strip()
    if data.get("is_error") or (later and not text):
        return None if later else move(st, d, "_errored", ev, ctx)
    if not text:
        # an empty result is not a reply (hub, 2026-10-01: after "Connected" the turn ended idle and counted as
        # REPLIED); the delegation stays in work, the session's next turn is again on it
        if sess.get("delegation"):
            sess.setdefault("also_tasks", []).append(d["id"])
        else:
            sess["delegation"] = d["id"]
        return
    if later and sess.get("monitor_turn"):
        # a monitor event: hold the last result until quiet (MONITOR_HOLD); if it wrote to the assigner itself, it
        # already arrived
        if d.get("for_session") and got_result(sess, d["for_session"], data.get("text")):
            d.pop("held", None)
        else:
            d["held"] = {"reply": data.get("text") or "", "seq": ev["seq"], "ts": ev["ts"]}
        return
    if not later and d["session"] == sess["name"] and _passed_on(st, sess, d, ev, data, ctx):
        return
    d.pop("held", None)   # an ordinary result is newer than the held one: it goes out
    # the first turn said only "—" (the task went to replied silently): the real result that comes later is the
    # task's result for the hub, not a continuation, otherwise nobody gets it to close the task. Only for the owner's
    # task: a session that set the task gets the result itself, the hub is not told twice
    first = later and d["state"] == "replied" and d["for"] and not d.get("answered_at") \
        and (d.get("reply") or "").strip() == "—" and text != "—"
    answer = later and text != "—" and answered_reminder(d, ev["ts"])
    # the answer to the core's "open" reminder on the owner's task goes to the hub as the task's result, unless a
    # session waits for it (`--for`): that one gets it itself
    up = st["sessions"].get(d.get("for_session") or "") or {}
    to_hub = bool(answer and d["for"]) and up.get("state") in (None, "removed", "closed", "failed")
    d["reply"], d["replied_seq"] = data.get("text") or "", ev["seq"]
    if d["reply"].strip() != "—":   # "—" is a quiet check result, not a new reply: it does not reset quiet
        d["answered_at"] = d["quiet_mark"] = ev["ts"]
        d["replies"] = d.get("replies", 0) + 1   # results the owner sees: the quote of his message goes on the first
        if answer:
            d["open_answered"] = True   # no second reminder: only the fact to the hub, if it falls silent (quiet)
        else:   # a new reply: the countdown, reminder included, starts again
            d.pop("open_answered", None), d.pop("open_read", None)
    trail(st, d["id"], d["session"], RESULT + d["reply"])
    move(st, d, "_followup" if later else "_answered", ev, ctx, first=first or to_hub)


def _passed_on(st, sess, d, ev, data, ctx):
    """A mini-hub delegated the task (its executors still work on it or wait in the queue): the turn result is interim,
    into the task's record; the task stays in work, the hub gets no REPLIED. The assigning session gets one line
    "delegated to X" (once per each new one); for the owner's task the result itself is shown to him on the page
    (his "delegated"), nothing to the hub. When the list is empty, the next turn's result goes out as a reply, as
    usual (spec "mini-hub", section 2.2)."""
    busy = minihub.waiting_on(st, d)
    if not busy:
        return False
    name = d["session"]
    d["passed_seq"] = ev["seq"]   # the turn result is interim: not in "Done" and not in Telegram (reducer, telegram)
    trail(st, d["id"], name, INTERIM + (data.get("text") or ""))
    new = [n for n in busy if n not in (d.get("passed") or [])]
    if not new:
        return True
    d["passed"] = sorted(set(d.get("passed") or []) | set(new))
    line = PASSED.format(id=d["id"], name=name, names=", ".join(new))
    up = st["sessions"].get(d.get("for_session") or "") or {}
    if up and up.get("kind") != "hub":
        if up["name"] not in (sess.get("sent_in_turn") or []):   # it wrote to it in this turn itself: it already knows
            ctx["new_message"](st, f"m{ev['seq']}x1", up["name"], line, ev["ts"], origin=forms.ORIGIN_CORE)
    elif not d.get("for"):
        ctx["notify"](ev, line, task=d["id"])
    return True


SPEC_READY = T("core.delegations.spec_ready",
               "SPEC {id}: {name} wrote the spec {path} — it waits for the owner's \"Accept\" in Files. Its report: "
               "{text}")
SPEC_INTERIM = T("core.delegations.spec_interim",
                 "SPEC {id}: {name} reported on the spec {path} (already accepted, the work is next): {text}")
SPEC_ACCEPTED = T("core.delegations.spec_accepted", "Spec {id} of {name} is accepted ({who}): it does the work by it now.")
SPEC_DROPPED = T("core.delegations.spec_dropped", "Spec {id} of {name} is dropped ({who}): the task is closed.")
SPEC_GO = ("The spec of {id} is accepted by {who} (copy: `$HUB_STATE_DIR/{copy}`). Spec mode is off: do the work by the "
           "spec. Change the spec now only with a note in it and a line in your result.")
SPEC_FOR = T("core.delegations.spec_for",
             "the spec is accepted or dropped by the owner — pass on their word: --for mN (their message to you)")
SPEC_WHO = T("core.delegations.spec_who",
             "the spec of {id} is accepted or dropped by the owner (Files, the page, Telegram), or by {boss} with their "
             "word (--for mN)")
SPEC_SHA = T("core.delegations.spec_sha", "accept needs the sha256 of the text that was shown and its copy")
TRAIL_ACCEPTED = T("core.delegations.trail_spec_accepted", "spec accepted ({who}): {copy}")
TRAIL_DROPPED = T("core.delegations.trail_spec_dropped", "spec dropped ({who})")


def _spec_ready(st, d, ev, ctx, interim=False):
    """A turn result with the spec open: an interim report to whoever raised the executor (no REPLIED to the hub, the
    task stays open); the first one marks the spec ready — the card "Spec dN is ready" (the page, Telegram). interim —
    the spec was accepted mid-turn: the report goes as is, the work comes with the next turn."""
    text, name, up = d["reply"] or "", d["session"], d.get("for_session")
    if not interim:
        d["spec"].setdefault("ready", ev["seq"])
    me = st["sessions"].get(name) or {}
    if up and up in st["sessions"] and not got_result(me, up, text) and text.strip() != "—":
        ctx["new_message"](st, f"m{ev['seq']}x1", up, (SPEC_INTERIM if interim else SPEC_READY).format(
                               id=d["id"], name=name, path=d["spec"]["path"], text=text),
                           ev["ts"], origin=forms.from_session(name, forms.is_hub(st, name)) + f", spec of task {d['id']}",
                           task=d["id"], frm=forms.origin_from("session", name))


def spec_who(st, d, ev):
    """Who closes the spec: the owner himself (the page, Files, Telegram, his terminal) -> "owner"; whoever raised the
    executor (or the hub) with the owner's word to it (--for mN, his answer to a card) -> its name; anyone else —
    refused (owner decision 2026-11-08: otherwise a mini-hub would accept for him and the mode would lose its point)."""
    from core import guards
    data = ev.get("data") or {}
    if guards.from_owner_strict(st, data) is None:
        return BY_OWNER
    sender = data.get("sender") if data.get("channel") == "session" else None
    me = st["sessions"].get(sender or "") or {}
    if sender and (sender == d.get("for_session") or me.get("kind") == "hub"):
        if not data.get("for"):
            raise Rejected(SPEC_FOR)
        tools.owner_word(st, data, ev["ts"], own=True, cards=True)
        return sender
    raise Rejected(SPEC_WHO.format(id=d["id"], boss=d.get("for_session") or TO_HUB))


def spec_close(st, ev, ctx):
    """delegation.spec_accepted / delegation.spec_dropped: who may, then the DELEGATION table."""
    d = st["delegations"].get(ev.get("entity"))
    if not d:
        raise Rejected("no such delegation")
    who = spec_who(st, d, ev)
    data = ev.get("data") or {}
    # what was shown is accepted: its sum and its copy, made before the fact (specmode.accept_copy) — the executor works by
    # spec.accepted.md, so an accept without the copy would point it at a missing or an older file
    if ev["type"] == "delegation.spec_accepted" and specmode.is_open(d) and not (
            isinstance(data.get("sha256"), str) and len(data["sha256"]) == 64
            and specmode.pending_ok(d["spec"]["path"], data.get("pending"))):
        raise Rejected(SPEC_SHA)
    d["_spec_who"] = who
    try:
        move(st, d, ev["type"], ev, ctx)
    finally:
        d.pop("_spec_who", None)


def _spec_accepted(st, d, ev, ctx):
    data, who = ev.get("data") or {}, d.get("_spec_who") or BY_OWNER
    copy = specmode.accepted_path(d["spec"]["path"])
    d["spec"].update(state="accepted", at=ev["ts"], by=str(who), sha256=data["sha256"], copy=copy)
    d["deadline"], d["timed_out"] = ev["ts"] + TIMEOUT_SECONDS, False   # the work by the spec gets a new term and ladder
    d.pop("nudges", None), d.pop("nudged_at", None)
    name = d["session"]
    if name in st["sessions"]:
        ctx["new_message"](st, f"m{ev['seq']}", name, SPEC_GO.format(id=d["id"], who=who, copy=copy), ev["ts"],
                           origin=forms.ORIGIN_CORE, for_delegation=d["id"])
        d["spec"]["go"] = f"m{ev['seq']}"   # until its turn starts, a result is still the spec's (guards.spec_go_pending)
    up = d.get("for_session")
    if up and up in st["sessions"] and up != (ev.get("data") or {}).get("sender"):
        ctx["new_message"](st, f"m{ev['seq']}x1", up, SPEC_ACCEPTED.format(id=d["id"], name=name, who=who), ev["ts"],
                           origin=forms.ORIGIN_CORE, task=d["id"])
    trail(st, d["id"], BY_CORE, TRAIL_ACCEPTED.format(who=who, copy=copy))


def _spec_dropped(st, d, ev, ctx):
    who = d.get("_spec_who") or BY_OWNER
    d["spec"].update(state="dropped", at=ev["ts"], by=str(who))
    up = d.get("for_session")
    if up and up in st["sessions"] and up != (ev.get("data") or {}).get("sender"):
        ctx["new_message"](st, f"m{ev['seq']}x1", up, SPEC_DROPPED.format(id=d["id"], name=d["session"], who=who),
                           ev["ts"], origin=forms.ORIGIN_CORE)
    trail(st, d["id"], BY_CORE, TRAIL_DROPPED.format(who=who))


def on_state(st, name, to, ev, ctx):
    """The executor's state moves the board: it asks the owner, "waits for you"; it crashed, "broken off"."""
    event = {"dialog": "_asks_owner", "busy": "_owner_answered", "failed": "_session_failed"}.get(to)
    if not event:
        return
    sess = st["sessions"].get(name) or {}
    for d in list(st["delegations"].values()):
        if d["session"] == name and decide(DELEGATION, d["state"], event)[0] is not None:
            if event == "_owner_answered" and _asked(sess, d, ev["ts"]):
                continue   # a turn on something else (the core's late "continue"): its card still waits for the owner
            move(st, d, event, ev, ctx)


def stuck(st, d, queued=None, now=None, kids=None):
    """Since when the task has stood without an executor, or None if it does not stand (the owner complained that
    sessions do not say what they are doing, "are you alive?"). It stands when the task is in work, and the executor
    is free and waits for nothing: the turn ended without a result or never took the task. It does not stand when:
    busy (a turn is running, nothing until its end), waiting for the owner (a card, a "yes" for a tool, a question
    deferred while he is away), background work is running, a message is queued (it will arrive by itself;
    undelivered ones are the watcher's concern), asleep or crashed (a reminder would wake a sleeping one bypassing
    the owner's or hub's "stop"; a crashed one is already STOPPED). queued is the names of sessions with undelivered
    messages (pending counts them at once). now - the clock: a card stale by it (owner.ASK_TTL) is not waiting. kids -
    minihub.children of the same state (pending builds it once for all tasks)."""
    sess = free(st, d, queued, now=now, kids=kids) if d["state"] == "working" else None
    return None if sess is None else max(d["since"], sess.get("since") or 0, d.get("nudged_at") or 0, returned(st, d))


def deferred(st, d):
    """The task's session has a question deferred during the owner's last absence (owner.deferred: the list lives
    until his next absence)."""
    return any(q.get("session") == d["session"] for q in st["owner"].get("deferred") or [])


def returned(st, d):
    """When the owner came back, if the task's session has a question deferred while he was away, otherwise 0: until
    then it waited for him, so the reminders count from his return, not from the turn's end (otherwise the first one
    comes the minute he opens the return summary)."""
    return (st["owner"].get("back_at") or 0) if deferred(st, d) else 0


def free(st, d, queued=None, states=("idle",), now=None, kids=None):
    """The task's executor is free and waits for nothing: its record, otherwise None (the rules for "does not
    stand" are in stuck). states: the session states that count (quiet also asks about sleeping ones). now - the clock:
    a card stale by it (owner.ASK_TTL) is not waiting once askexpired raised it (askexpired.holds): before that a
    reminder, timed from long before the card went stale, would come first and push the raise back."""
    sess = st["sessions"].get(d["session"])
    if not sess or sess.get("kind") == "hub" or sess["state"] not in states:
        return None
    if sess.get("background") or sess.get("pending") or owner.open_asks(sess, now) or sess.get("limit_cut") \
            or sess.get("net_cut"):   # a cut turn: the core continues it itself
        return None
    from core import hubrun
    if hubrun.live_of(st, sess["name"]):   # its `hub run` goes on outside it: the result wakes it (core/hubrun.py)
        return None
    if now is not None and askexpired.holds(sess, now, st["delegations"]):
        return None
    if any(r.get("state") == "needs-owner" for r in sess.get("requests") or []):
        return None
    # a question deferred while the owner is away holds only until he returns: then it is text in the return summary,
    # nothing answers it in the session, and the record stays until his next absence
    if st["owner"].get("state") == "away" and deferred(st, d):
        return None
    if minihub.waiting_on(st, d, kids):   # a mini-hub waits for its executors: reminders go on their tasks
        return None
    if queued is None:
        queued = {m["session"] for m in st["messages"].values() if m["state"] in ("queued", "typing")}
    return None if d["session"] in queued else sess


def _mark_answer(d, ts):
    """A released monitor result (timeout): the time of the latest result is always kept (answered_at: the board's
    "handed in since", the fresh-reply check after a move); the quiet countdown starts from quiet_mark, which a result
    soon after the reminder leaves where it was."""
    before = d.get("quiet_mark") or d.get("answered_at") or d["since"]
    d["answered_at"] = ts
    d["quiet_mark"] = before if _answers_nudge(d, ts) else ts


def _answers_nudge(d, ts):
    """A result soon after the core's "open" reminder counts as the answer to it by time: for a reminder from an
    earlier core (answered_reminder) and for a released monitor result, which then does not restart the quiet
    countdown (_mark_answer), otherwise every such result brought a new reminder 30 min later."""
    return bool(d.get("open_nudged")) and 0 <= ts - d["open_nudged"] < NUDGE_ANSWER


def answered_reminder(d, ts):
    """This result answers the core's "open" reminder (done, waiting for X, stuck: what the reminder asks for): the
    first one after a turn read the reminder (open_read; a turn cut by a limit and continued is the same answer), and
    a reminder from an earlier core has no id: by time (_answers_nudge). On the owner's task such a result goes to the
    hub as the task's result, not as a continuation (a session that set the task gets it anyway); the task gets no
    second reminder (open_answered), only the fact if it falls silent again. A continuation of an owner's task does
    not reach the hub, so a "done" in answer to the reminder reached nobody; a later `hub wait` on another topic moved
    the countdown past the reminder, and the same reminder came back with no fact (a colleague's hub: two tasks
    answered "done" twice, closed 9 h later; a week of the journal: one task got 7 reminders)."""
    if d["state"] != "replied" or d.get("open_fact") or d.get("open_answered") or not d.get("open_nudged"):
        return False
    return bool(d.get("open_read")) if d.get("open_nudge_mid") else _answers_nudge(d, ts)


def _read_reminder(st, sess, marker):
    """The turn read the core's "open" reminder (its mark, or read together with it): its result is the answer."""
    for mid in (marker, *(sess.get("turn_also") or ())):
        d = st["delegations"].get((st["messages"].get(mid or "") or {}).get("for_delegation") or "")
        if d and d.get("open_nudge_mid") == mid and d["session"] == sess["name"]:
            d["open_read"] = True


def last_nudged(st):
    """{session: the time of the latest reminder on any of its tasks} — for POKE_GAP; counted once per tick (pending)."""
    out = {}
    for d in st["delegations"].values():
        if d.get("open_nudged"):
            out[d["session"]] = max(out.get(d["session"], 0), d["open_nudged"])
    return out


def quiet(st, d, queued=None, now=None, kids=None, nudged_at=None):
    """Open after a reply, and quiet (owner decision 2026-10-08: nudge open tasks without hub wait): the deadline of the
    next step (when, step, idle_since, mark), where step 0 is a reminder to the executor and 1 a fact to the hub, or
    None. Quiet means: the task is `replied`, not closed, the executor is free (free) and waits for nothing: no
    `hub waiting` and no check of its own in the schedule (`hub wait` ones are one-shot; recurring ones count if set
    after the task). mark is the latest thing that resets the countdown: a reply on the task or a set wait.
    Each step is due OPEN_STALE after mark (or the reminder) at the latest, however often the executor's turns on
    other work moved its idle time; a sleeping executor (asleep, parked; not retired) gets no reminder, only the fact
    to the hub OPEN_STALE after mark. A reminder is due no earlier than POKE_GAP after the latest reminder on another
    task of the same session (nudged_at: last_nudged, passed in by pending)."""
    if d["state"] != "replied" or d.get("open_fact"):
        return None
    sess = free(st, d, queued, now=now, kids=kids)
    sleeping = sess is None
    if sleeping:
        sess = free(st, d, queued, states=("asleep", "parked"), now=now, kids=kids)
    if sess is None or tools.waiting_now(sess):
        return None
    from core import schedule
    jobs = [j for j in (st.get("schedule") or {}).values() if schedule.wakes(j, d["session"])]
    if any(j["state"] == "active" and (j.get("kind") == "at" or j.get("created", 0) >= d["created"]) for j in jobs):
        return None
    # the countdown mark (a stale one is possible, hence the fallbacks), a set wait, the one-shot checks of the executor
    mark = max([d.get("quiet_mark") or d.get("answered_at") or d["since"], (sess.get("waiting") or {}).get("ts") or 0]
               + [j.get("created", 0) for j in jobs if j["id"].startswith(f"{d['session']}-wait-")])
    back = returned(st, d)   # moves the deadline, not the step: a reminder already sent before the absence is not repeated
    if sleeping:
        return max(mark, back) + OPEN_STALE, 1, mark, mark
    idle = max(mark, sess.get("since") or 0, back)
    nudged = d.get("open_nudged") or 0
    if nudged <= mark and not d.get("open_answered"):
        due = min(idle + OPEN_NUDGE, max(mark, back) + OPEN_STALE)
        last = (last_nudged(st) if nudged_at is None else nudged_at).get(d["session"], 0)
        if last > nudged:   # a reminder on another task of the session (its own is older than mark)
            due = max(due, last + POKE_GAP)
        return due, 0, idle, mark
    # after the reminder, or after its answer and what came later (a wait): only the fact is left
    top = max(nudged, mark)
    idle = max(top, sess.get("since") or 0, back)
    return min(idle + OPEN_FACT, max(top, back) + OPEN_STALE), 1, idle, mark


OPEN_NUDGE_TEXT = T("core.delegations.open_nudge",
                    "Task {id} is open, and you have been free for {mins} min. Done — say so in a result (the hub will "
                    "close it); waiting for something — set `hub wait \"<what, where to look>\"`, the check will wake you "
                    "itself; stuck — say what is in the way; nothing to add — \"—\".")
OPEN_NUDGE_TRAIL = T("core.delegations.open_nudge_trail", "reminder: open, free for {mins} min without a wait")
OPEN_FACT_TRAIL = T("core.delegations.open_fact_trail",
                    "interim reply and {mins} min of silence — the hub decides the task")
NUDGED = T("core.delegations.nudged", "the core reminded it")
NOT_WOKEN = T("core.delegations.not_woken", "asleep, the core did not wake it")
OPEN_FACT_TEXT = T("core.delegations.open_fact",
                   "TIMEOUT {id}: task {id} has been silent for {mins} min after an interim reply ({name}, {how}): check "
                   "— done → close it (hub task done {id}), still relevant → nudge the executor (hub send {name} --about "
                   "{id} '<what is needed>'; asleep — hub session wake {name} --about {id} '<what is needed>'), not "
                   "needed → close it; do not write to the owner, the executor will report")
NUDGES_TRAIL = T("core.delegations.nudges_trail", "reminders without an answer ({n}) — the hub decides the task")
NUDGES_FACT = T("core.delegations.nudges_fact",
                "TIMEOUT {id}: {name} is free, but there is no result — {mins} min in work, {n} reminders without an "
                "answer. Decide: hand it over again or ask the owner")
FROM_OWNER = T("core.delegations.from_owner", "the owner")
FROM_HUB = T("core.delegations.from_hub", "the hub")
NUDGE_TEXT = T("core.delegations.nudge",
               "Task {id} from {who} has waited {mins} min for an answer, and you are free. Answer with a result or a "
               "progress line: what is done and what next; stuck — say what is in the way.")
NUDGE_LAST = T("core.delegations.nudge_last",
               "Task {id} from {who} has waited {mins} min for an answer, and you are free. Answer with a result or a "
               "progress line: what is done and what next; stuck — say what is in the way. This is the last reminder: "
               "after it the hub decides the task.")
NUDGE_TRAIL = T("core.delegations.nudge_trail", "reminder {n}: free without a result")


def poke(st, d, ev, ctx, step, idle, mark):
    """The "open after reply" step: a reminder to the executor (a turn on it continues the task) or one fact to the hub.
    Nothing new for the owner."""
    name = d["session"]
    if step == 0:
        d["open_nudged"], d["open_nudge_mid"] = ev["ts"], f"m{ev['seq']}"
        d.pop("open_read", None)
        mins = round((ev["ts"] - idle) / 60)
        text = OPEN_NUDGE_TEXT.format(id=d["id"], mins=mins)
        ctx["new_message"](st, f"m{ev['seq']}", name, text, ev["ts"], origin=forms.ORIGIN_CORE, for_delegation=d["id"])
        trail(st, d["id"], BY_CORE, OPEN_NUDGE_TRAIL.format(mins=mins))
        return
    d["open_fact"] = True
    mins = round((ev["ts"] - mark) / 60)
    trail(st, d["id"], BY_CORE, OPEN_FACT_TRAIL.format(mins=mins))
    # the fact leads the hub to an action: check, then close or nudge; the owner hears from the executor, not the hub
    did = d["id"]
    how = NUDGED if d.get("open_nudged") else NOT_WOKEN   # a sleeping executor gets no reminder
    ctx["notify"](ev, OPEN_FACT_TEXT.format(id=did, mins=mins, name=name, how=how), task=did)


def nudge_at(st, d, queued=None, now=None, kids=None):
    """The deadline of the next ladder step (NUDGES: reminders to the executor, the last step is a fact to the hub)
    or None."""
    n = d.get("nudges") or 0
    if n >= len(NUDGES):
        return None
    since = stuck(st, d, queued, now=now, kids=kids)
    return None if since is None else since + NUDGES[n]


def nudge(st, d, ev, ctx):
    """A ladder step: a reminder to the executor (a turn on it is the task's turn, the result is a reply, as for the
    task itself) or, on the last step, one fact to the hub, which decides (hand over again, ask the owner). Nothing
    new for the owner."""
    n = d["nudges"] = (d.get("nudges") or 0) + 1
    d["nudged_at"] = ev["ts"]
    name, mins = d["session"], round((ev["ts"] - d["since"]) / 60)
    if n >= len(NUDGES):
        trail(st, d["id"], BY_CORE, NUDGES_TRAIL.format(n=n - 1))
        if not d["timed_out"]:
            d["timed_out"] = True   # one fact to the hub: the 90-minute TIMEOUT is not needed after it
            ctx["notify"](ev, NUDGES_FACT.format(id=d["id"], name=name, mins=mins, n=n - 1), task=d["id"])
        return
    who = (FROM_OWNER if d["for"] else SESSION_WORD.format(name=d["for_session"]) if d.get("for_session") else FROM_HUB)
    text = (NUDGE_LAST if n == len(NUDGES) - 1 else NUDGE_TEXT).format(id=d["id"], who=who, mins=mins)
    ctx["new_message"](st, f"m{ev['seq']}", name, text, ev["ts"], origin=forms.ORIGIN_CORE, for_delegation=d["id"])
    trail(st, d["id"], BY_CORE, NUDGE_TRAIL.format(n=n))


def timeout(st, ev, ctx):
    """A delegation's deadline (timer.fired for a task): a ladder step if it is due, otherwise 90 minutes without a
    result, TIMEOUT to the hub."""
    d = st["delegations"].get(ev.get("entity"))
    if not d:
        return
    held = d.get("held")
    if held and held["ts"] + MONITOR_HOLD <= ev["ts"]:
        d.pop("held")
        if d["state"] in ("replied", "stopped"):   # if closed, the held one is stale
            d["reply"], d["replied_seq"] = held["reply"], held["seq"]
            if d["reply"].strip() != "—":
                _mark_answer(d, held["ts"])   # a fresh result restarts the quiet countdown, like an ordinary reply
            trail(st, d["id"], d["session"], RESULT + d["reply"])
            move(st, d, "_followup", ev, ctx)
        return
    at = nudge_at(st, d, now=ev["ts"])
    if at is not None and at <= ev["ts"]:
        return nudge(st, d, ev, ctx)
    q = quiet(st, d, now=ev["ts"])
    if q and q[0] <= ev["ts"]:
        return poke(st, d, ev, ctx, *q[1:])
    if d["deadline"] <= ev["ts"] and d["state"] in ("working", "waiting-owner") and minihub.waiting_on(st, d):
        d["deadline"] = ev["ts"] + TIMEOUT_SECONDS   # a mini-hub waits for its executors: restart the deadline
        return
    if d["deadline"] <= ev["ts"] and decide(DELEGATION, d["state"], "_timeout", checker(st, d))[0] is not None:
        move(st, d, "_timeout", ev, ctx)


def done(st, ev, ctx):
    d = st["delegations"].get(ev.get("entity"))
    if not d:
        raise Rejected("no such delegation")
    move(st, d, "delegation.done", ev, ctx)
    who = ev["data"].get("sender") if ev["data"].get("channel") == "session" else BY_OWNER
    trail(st, d["id"], BY_CORE, CLOSED.format(who=who))


def pending(st, now=None):
    """Delegation deadlines for hubd: the nearest of a ladder step and 90 minutes without a result; for replied
    ones, the quiet step. Called on every tick of the core: the queue, the children of tasks and the latest reminder
    of each session are counted once for all tasks, not per task."""
    queued = {m["session"] for m in st["messages"].values() if m["state"] in ("queued", "typing")}
    kids = minihub.children(st)
    nudged_at = last_nudged(st)
    out = []
    for d in st["delegations"].values():
        if d.get("held") and d["state"] == "stopped":
            out.append((d["held"]["ts"] + MONITOR_HOLD, d["id"], None))
            continue
        if d["state"] == "replied":
            q = quiet(st, d, queued, now=now, kids=kids, nudged_at=nudged_at)
            at = [x for x in (q[0] if q else None, d["held"]["ts"] + MONITOR_HOLD if d.get("held") else None)
                  if x is not None]
            if at:
                out.append((min(at), d["id"], None))
            continue
        if d["state"] not in ("working", "waiting-owner"):
            continue
        at = [x for x in (None if d["timed_out"] else d["deadline"], nudge_at(st, d, queued, now=now, kids=kids)) if x is not None]
        if at:
            out.append((min(at), d["id"], None))
    return out


def without_yes_note(d, context):
    """context without its own "Waiting for the owner's yes" line (the yes_note field; for old records, by the
    line's beginning)."""
    return "\n".join(x for x in (context or "").split("\n")
                     if not (x in d["yes_note"].split("\n") if isinstance(d.get("yes_note"), str)
                             else x.startswith(forms.YES_NOTE_HEADS)))


def open_task(st, name):
    """The session's latest unclosed delegation: (id, task text), to remind it after a restart."""
    def asker_alive(d):  # the asking session still exists, else nobody to answer (2026-10-03: a removed operator's task)
        asker = st["sessions"].get(d.get("for_session") or "")
        return not d.get("for_session") or (asker and asker["state"] not in ("removed", "closed", "failed"))
    mine = [d for d in st["delegations"].values() if d["session"] == name
            and d["state"] not in ENDED and asker_alive(d)]
    d = max(mine, key=born, default=None)
    if not d:
        return None, None
    # the "waiting for the owner's yes" note is stale: the tool was just connected
    context = without_yes_note(d, d["context"])
    if d.get("trimmed"):   # brought back from the archive: the core's record holds the beginning of the task text
        context = "\n".join(x for x in (context, TRIMMED.format(did=d["id"])) if x)
    return d["id"], task_text(d["words"], d["for"], context or None, d.get("for_session"), d.get("by"), d["id"],
                              minihub.boss(st, st["sessions"].get(name)), lang_word(st))


def archivable(st, before):
    """Delegations for the archive (hubd, the prune pass): handed in (reply or break) by executors removed before
    `before`; and done ones of such executors not trimmed yet (they stay done, only their texts are cut)."""
    out = []
    for d in st["delegations"].values():
        sess = st["sessions"].get(d["session"]) or {}
        if (d["state"] in ("replied", "stopped") or d["state"] == "done" and not d.get("trimmed")) \
                and sess.get("since", before) < before \
                and decide(DELEGATION, d["state"], "_archive", checker(st, d))[0] is not None:
            out.append(d["id"])
    return out


def archive(st, ev, data, ctx):
    """core.archived {ids}: delegations recorded by hubd go to the archive. The age is not recomputed (like `before`
    in core.pruned): journal readers get the same; one that no longer fits (back in work, executor alive) is
    skipped silently."""
    ids = data.get("ids")
    if not isinstance(ids, list):
        raise Rejected(T("core.delegations.archived_ids", "core.archived: ids — a list of task numbers"))
    for did in ids:
        d = st["delegations"].get(did) if isinstance(did, str) else None
        if d and decide(DELEGATION, d["state"], "_archive", checker(st, d))[0] is not None:
            move(st, d, "_archive", ev, ctx)
