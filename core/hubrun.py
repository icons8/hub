"""`hub run`: a session's long command that the core runs outside the session's tree and outside the hub, up to 24 h
(owner decision 2026-11-11, spec attachments/hub-run/spec.md). It outlives the end of the turn, an executor's hand-in,
parking, `hub restart` (and `--now`), the restart of the hub by its OS service and a code update; when it ends the
session gets a core message with the exit code, the last lines of output and the path to the log.

This file is the fold: the RUN automaton's records in st["hub_runs"] (st["runs"] is the executors' rework queue),
the checks, the stop when the session goes, the outcome message. The process itself — core/runjobs.py (the daemon's
tick) and platforms/runwrap.py (the wrapper, which holds the deadline without the hub).

Events: run.requested (a session, entity = the session), run.stop_requested (entity = the run), core.run_started and
core.run_ended (the core only: a mailbox fact cannot forge the end of someone's run)."""
from core import coretext, forms, redact
from core.coretext import T
from core.machines import RUN, decide
from core.tools import Rejected

PER_SESSION = 4     # live runs of one session (a chosen limit, not a measure)
PER_HUB = 16        # live runs of the whole hub
MAX_LENGTH = 24 * 3600   # seconds: the longest run, and the default one
MIN_LENGTH = 60
SKEW = 300          # seconds the command's clock may be ahead of the core's when it computes until_at (and the
                    # command's 1 minute is checked by the command itself: the core takes half of it)
COMMAND_MAX = 64 * 1024   # characters of the command (more — a file and `hub run bash <file>`)
KEEP_ENDED = 20     # ended records of one session kept in the state (the events stay in the journal)
# the session will not read the outcome: its runs stop. Not "asleep" (a message wakes it, the outcome too) nor "parked";
# "retired" is an executor stopped (`hub session stop`) — handing in is held by closable while a run goes
GONE = ("retired", "closed", "removed", "failed")
LIVE = ("queued", "running", "stopping")
MODES = (None, "", "bypassPermissions")   # a session in another mode would get past its own limits outside itself

ONLY_SESSION = T("core.hubrun.only_session", "hub run is run from a session (HUB_SESSION): the owner has a terminal of "
                 "his own, a plugin or a module has none")
NOT_MINE = T("core.hubrun.not_mine", "a session starts only its own runs")
NO_SESSION = T("core.hubrun.no_session", "no session {name}")
SESSION_GONE = T("core.hubrun.session_gone", "session {name} is {state}: nobody would read the result")
MODE = T("core.hubrun.mode", "hub run runs outside your session's permissions (mode {mode}): only in bypassPermissions")
NO_COMMAND = T("core.hubrun.no_command", "no command: hub run '<command>' or hub run - (the command on stdin)")
LONG_COMMAND = T("core.hubrun.long_command", "the command is longer than {max} characters: put it into a file and run "
                 "hub run bash <file>")
NO_CWD = T("core.hubrun.no_cwd", "the folder of the run is an absolute path")
BAD_UNTIL = T("core.hubrun.bad_until", "the term of a run is from 1 minute to 24 hours (--until 90m, 6h, 24h or 18:30)")
TOO_MANY = T("core.hubrun.too_many", "{n} runs are already going: hub run list / hub run stop <id>")
TOO_MANY_HUB = T("core.hubrun.too_many_hub", "the hub already runs {n} commands with hub run — wait for one to end or "
                 "stop one (hub run list --all)")
STOP_WHO = T("core.hubrun.stop_who", "a run is stopped by its session, whoever raised it, the hub or the owner")

ENDED = T("core.hubrun.ended", "hub run {id} ended: {how} after {took} ({command}). Log: {log}")
LAST_LINES = T("core.hubrun.last_lines", "Last lines:")
HOW = {"exit": T("core.hubrun.how_exit", "exit {code}"),
       "timeout": T("core.hubrun.how_timeout", "stopped at its term"),
       "stopped": T("core.hubrun.how_stopped", "stopped by {by}"),
       "lost": T("core.hubrun.how_lost", "lost without a result (the machine restarted, or its process was killed)"),
       "not_started": T("core.hubrun.how_not_started", "not started: {error} — a run that must outlive a hub restart "
                        "is not possible here now; tell the hub")}
LEFTOVER = T("core.hubrun.leftover", " What it started may still run: its process group could not be checked.")
STOPPED_BY_CORE = T("core.hubrun.by_core", "the core")


def runs(st):
    return st.get("hub_runs") or {}


def live_of(st, name):
    """The session's runs that are not over, oldest first."""
    return sorted((r for r in runs(st).values() if r["session"] == name and r["state"] in LIVE),
                  key=lambda r: r["ts"])


def checker(st, run, data):
    def check(guard):
        if guard == "may_run":
            return _may_run(st, run, data)
        if guard == "may_stop_run":
            return _may_stop(st, run, data)
        return None
    return check


def _may_run(st, run, data):
    if data.get("channel") != "session":
        return ONLY_SESSION
    name = run["session"]
    if data.get("sender") != name:
        return NOT_MINE
    sess = st["sessions"].get(name)
    if sess is None:
        return NO_SESSION.format(name=name)
    if sess["state"] in GONE:
        return SESSION_GONE.format(name=name, state=sess["state"])
    if sess.get("mode") not in MODES:
        return MODE.format(mode=sess.get("mode"))
    if len(live_of(st, name)) >= PER_SESSION:
        return TOO_MANY.format(n=PER_SESSION)
    if sum(r["state"] in LIVE for r in runs(st).values()) >= PER_HUB:
        return TOO_MANY_HUB.format(n=PER_HUB)
    return None


def _may_stop(st, run, data):
    from core import guards
    if guards.from_owner_or_hub(st, data) is None:
        return None
    sender = data.get("sender") if data.get("channel") == "session" else None
    sess = st["sessions"].get(run["session"]) or {}
    if sender and sender in (run["session"], sess.get("raised_by")):
        return None
    return STOP_WHO


def _valid(data, ts):
    command = data.get("command")
    if not isinstance(command, str) or not command.strip():
        raise Rejected(NO_COMMAND)
    if len(command) > COMMAND_MAX:
        raise Rejected(LONG_COMMAND.format(max=COMMAND_MAX))
    cwd = data.get("cwd")
    if not isinstance(cwd, str) or not (cwd.startswith("/") or (len(cwd) > 2 and cwd[1] == ":")):
        raise Rejected(NO_CWD)
    until = data.get("until_at")
    # until != until: NaN; the range check takes inf and a huge int too (math.isfinite(10**400) raises)
    if isinstance(until, bool) or not isinstance(until, (int, float)) or until != until \
            or not ts + MIN_LENGTH / 2 < until <= ts + MAX_LENGTH + SKEW:
        raise Rejected(BAD_UNTIL)
    return command, cwd, min(float(until), ts + MAX_LENGTH)


def apply(st, ev, data, ctx):
    """run.requested / run.stop_requested / core.run_started / core.run_ended. ctx: new_message, chain(sess) — the
    turn's chain fields (root, re or for_delegation) the outcome message continues."""
    kind = ev["type"]
    if kind == "run.requested":
        name = ev.get("entity")
        if not isinstance(name, str):
            raise Rejected(NO_SESSION.format(name=name))
        run = {"id": f"run{ev['seq']}", "session": name, "state": RUN["initial"]}
    else:
        run = runs(st).get(ev.get("entity") or "")
        if run is None:
            raise Rejected(RUN["said"]["absent"])
    to, todo, why = decide(RUN, run["state"], kind, checker(st, run, data))
    if to is None:
        raise Rejected(why)
    if "save" in todo:
        command, cwd, until = _valid(data, ev["ts"])
        sess = st["sessions"][run["session"]]
        run.update(command=command, cwd=cwd, until_at=until, ts=ev["ts"], **ctx["chain"](sess))
        st.setdefault("hub_runs", {})[run["id"]] = run
    run["state"], run["since"] = to, ev["ts"]
    if "started" in todo:
        for k in ("how", "unit", "pgid", "born", "boot"):
            if data.get(k) is not None:
                run[k] = data[k]
        run["started_at"] = ev["ts"]
    if "stop" in todo:
        run["stop_by"] = _who(st, data)
    if "ended" in todo:
        _ended(st, ev, run, data, ctx)


def _who(st, data):
    if data.get("channel") == "session":
        return data.get("sender") or "?"
    return "owner" if data.get("owner") is True else str(data.get("sender") or "?")


def _ended(st, ev, run, data, ctx):
    for k in ("code", "reason", "by", "took", "tail", "log", "error", "leftover"):
        if data.get(k) is not None:
            run[k] = data[k]
    run["ended_at"] = ev["ts"]
    sess = st["sessions"].get(run["session"])
    if sess is not None and sess["state"] not in GONE and not run.get("quiet"):
        ctx["new_message"](st, f"m{ev['seq']}", run["session"], outcome(run, data, ev["ts"]), ev["ts"],
                           origin=forms.ORIGIN_CORE, **_chain_now(st, run))
    _prune(st, run["session"])


def _chain_now(st, run):
    """The chain of the turn that started the run; a task already closed is not reopened by the outcome."""
    out = {k: run[k] for k in ("root", "re") if run.get(k)}
    d = (st.get("delegations") or {}).get(run.get("for_delegation") or "")
    if d and d.get("session") == run["session"] and d.get("state") == "working":
        out["for_delegation"] = d["id"]
    return out


def took_text(seconds):
    seconds = max(0, int(seconds or 0))
    if seconds < 60:
        return f"{seconds} s"
    h, m = divmod(seconds // 60, 60)
    return f"{h} h {m} min" if h else f"{m} min"


def outcome(run, data, ts):
    reason = data.get("reason") or "lost"
    how = HOW.get(reason, HOW["lost"])
    by = data.get("by") or run.get("stop_by") or STOPPED_BY_CORE
    how = how.format(code=data.get("code"), by=by, error=str(data.get("error") or "")[:300])
    head = run["command"].strip().splitlines()[0][:120]
    took = data.get("took")
    took = took if isinstance(took, (int, float)) and not isinstance(took, bool) and 0 <= took <= 2 * MAX_LENGTH \
        else ts - (run.get("started_at") or run["ts"])
    text = ENDED.format(id=run["id"], how=how, took=took_text(took),
                        command=head, log=data.get("log") or "-")
    if data.get("leftover") == "unknown":
        text = coretext.join(text, LEFTOVER)
    tail = data.get("tail")
    if isinstance(tail, str) and tail.strip():
        text = coretext.join(text, "\n", LAST_LINES, "\n```\n", tail.rstrip("\n").replace("```", "'''"), "\n```")
    return text


def _prune(st, name):
    done = sorted((r for r in runs(st).values() if r["session"] == name and r["state"] == "ended"),
                  key=lambda r: r.get("ended_at") or r["ts"])
    for r in done[:-KEEP_ENDED]:
        del st["hub_runs"][r["id"]]


def follow(st, ev):
    """After every event: the runs of a session that went (GONE: an executor stopped, closed, removed, failed for good)
    or is no longer listed stop — nobody would read their outcome, and no message comes. An asleep or parked session
    keeps them: the outcome wakes it. Nothing to do without live runs."""
    if not st.get("hub_runs"):
        return
    for run in st["hub_runs"].values():
        if run["state"] not in ("queued", "running"):
            continue
        sess = st["sessions"].get(run["session"])
        if sess is None or sess["state"] in GONE:
            run.update(state="stopping", since=ev["ts"], stop_by="core", quiet=True)


def rows(st, name):
    """The session's live runs as rows of its background (core/views.py, the page and `hub show`)."""
    return [{"id": r["id"], "kind": "run", "title": "hub run: " + redact.mask_head(r["command"].strip().splitlines()[0], 80),
             "since": r.get("started_at") or r["ts"], "until_at": r["until_at"]} for r in live_of(st, name)]
