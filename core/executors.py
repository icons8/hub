"""Executors (spec "tasks, specialists, executors", release 2): a session for a single task.

Raised by a specialist, an executor or the hub: `hub task run <name> "<task>" [--repo PATH]` -> the fact task.run_requested
(limits are checked here) -> the daemon prepares its own working copy (branch task/<name>, without a copy of the
conversation) and writes session.requested (role executor, the profile of whoever raised it) and delegation.created (the
result goes to whoever raised it). If the copy failed — task.run_failed, with the reason, goes to whoever raised it. After
the report the executor leaves (retiring below); whoever raised it gets "add the lessons".
"""
import re
import uuid

from core import askexpired, coretext, forms, langdata, minihub, owner, specmode, tools
from core.coretext import T
from core.tools import Rejected
from core.machines import LIVE, SLEEP

LIMIT_SPECIALIST = 3   # journals before parallelism (core/minihub.py): live executors per specialist
LIMIT_OTHER = 6        # per direction, same there; and now — per executor and worker (subtasks); for a specialist and
                       # a direction it is parallelism; the hub has no ceiling (owner decision 2026-10-04: the hub
                       # must have no ceiling — directions after migration are its executors and took all the places)
BUDGET_OWNER_ONLY = 0.85   # weekly account limit: beyond it a new executor needs the owner's word, if
                           # the budget gate is on (hub.json budget.owner_gate, core/budget.gate)

# how a lesson is worded (2026-10-28, the stops audit: corrections became "ask before X" lines that lived forever)
LESSON_HINT = " (an owner's correction as \"do it like this\", not \"ask them before X\")"
# what goes into the block (owner decision 2026-11-08, "yes, stricter"): the profiles grew by a lesson rephrased again
LESSON_BAR = ("; only what will change the next decision of a future session and the profile does not say yet — one fact, "
              "one line, no PR or commit numbers and no dates unless the date is the point")
REPORT_RULE = ("You are the executor of this task, {raiser} raised you: the result of this turn goes to it by itself. The "
               "task record is `hub task show` (everyone reads and writes it: {raiser}, the owner, the hub): read it first; "
               "context, decisions and progress — `hub task note \"<what>\"`. In the result, as a separate \"Lessons\" "
               "block — what is worth writing into the profession's CLAUDE.md, if anything" + LESSON_HINT + LESSON_BAR + ". The owner's "
               "decisions that were voiced — `hub decided \"<what> · why\"`. Code goes in your own working copy; hand-in: "
               "rebase on a fresh main, tests, `git merge --ff-only` into main (in a project with MRs — an MR). Missing "
               "information — ask {raiser} (`hub send {raiser} \"…\"`). Once handed in, you fall asleep (the "
               "conversation and the copy are kept, you will be woken if needed); new work outside the task — tell "
               "{raiser}, do not take it yourself.")
# an executor's copy started from another branch merges into it (worktree.base_of)
MERGE_MAIN = "rebase on a fresh main, tests, `git merge --ff-only` into main"
MERGE_INTO = "rebase on a fresh branch {base}, tests, `git merge --ff-only` into {base}"
# for a direction that is a mini-hub from its first raise (core/minihub.born): the task is done by its executors, not
# by itself; the fold substitutes it for REPORT_RULE in a task not yet typed (born_brief)
BORN_RULE = ("{raiser} raised you with this task; you are a mini-hub from the first minute: the code, the tests and the "
             "hand-in are done by your executors — hand the work to them, check their result and answer; the task's "
             "result goes to {raiser} by itself. The task record is `hub task show` (everyone reads and writes it: "
             "{raiser}, the owner, the hub): read it first; context, decisions and progress — `hub task note \"<what>\"`. "
             "The owner's decisions that were voiced — `hub decided \"<what> · why\"`. Missing information — ask "
             "{raiser} (`hub send {raiser} \"…\"`).")
# REPORT_RULE as the versions before the English texts wrote it (a task queued at the update keeps it in the journal:
# born_brief still recognizes it); the raiser word was the Russian word for the hub (OLD_RAISER) where it is "hub" now.
# Their text is language data (core/langdata.py, `legacy`): the rule holds the hint inside it
LESSON_HINT_RU = langdata.legacy("lesson_hint")
REPORT_RULE_RU = langdata.legacy("report_rule")
OLD_RAISER = langdata.legacy("hub_word")
RAISER = "hub"   # the raiser in the texts when the hub or the owner raised it (`hub send hub` reaches the hub)


ONBOARD = ("\n\n(This is your executor {name} for the owner's task. Reporting to the owner, end with the line: \"If there "
           "are more questions — I can make this a direction; if not — {name} will fall asleep by itself in 30 minutes, "
           "raising it later is manual.\" If they say \"make it a direction\" — set it up yourself: hub direction new "
           "<slug> --repo <the task's repo> --session {name}.)")


TAKES = ("You are an executor of {agent}: it is busy, this message is for you; you are the executor for this task — do "
         "not raise another one, even if the task says \"raise an executor\". Need context — `hub show {agent}` (what it "
         "did last) and `hub show --grep \"<topic>\"`: read them before asking it. The task record is `hub task show`, "
         "append the progress with `hub task note`; the result — as usual, with a separate \"Lessons\" block, if any"
         + LESSON_HINT + LESSON_BAR + "; once you hand it in, you fall asleep.\n\n")
# the raiser works in a folder git does not have (ignored or untracked): the copy lacks it, the executor starts in the
# root of the copy (hosts/worktree._finish, source_cwd); both its brief and a message handed over to it (TAKES) say so
SOURCE_FOLDER = ("The raiser's folder {path} is not in your working copy (it is outside git, e.g. in .gitignore): your "
                 "copy is the repo root. \"Your folder\" or \"here\" in the task is {path}: read and write its files by "
                 "their absolute path; do not put them into the copy and do not commit them.")


def source_note(worktree):
    """SOURCE_FOLDER for the executor's copy, or "" when the copy has the raiser's folder."""
    path = worktree.get("source_cwd") if isinstance(worktree, dict) else None
    return SOURCE_FOLDER.format(path=path) if path else ""


# a letter -> Latin. Of the earlier texts' language only, not the union: auto_name must give one name under the old and
# the new code (a letter another language's data adds would change names mid-update — a second executor for one task)
_LAT = langdata.get(langdata.OLD, "translit", "latin", {})


def _takeable(st, m, agent):
    """(Old journals: task.auto_run.) A message an auto-executor could take: yours, a hub task, a session request — not a
    service message of the core, the hub (handover) or a plugin, not a schedule job, not an addition to a task, not from its own executor."""
    if m["state"] != "queued" or m["session"] != agent or m.get("attempt") or m.get("about") or m.get("schedule"):
        return False
    f = forms.sender(m)
    service = f["kind"] in ("core", "hub", "plugin") if f else (m.get("origin") or "").startswith(
        forms.CORE_ORIGINS + forms.HUB_ORIGINS + forms.PLUGIN_ORIGINS)
    if service or m.get("handover"):
        return False
    sender = st["sessions"].get(m.get("sender") or "") or {}
    if sender.get("raised_by") == agent:
        return False
    # only a task (an assignment): the owner's direct words ("publish", "restart"), hub forwards and clarifications
    # go to the specialist itself (04.10, an avalanche: three executors for everything while the specialist ran tests)
    return any(d.get("message") == m["id"] and d["session"] == agent and d["state"] == "working"
               for d in st["delegations"].values())


def named(st, agent, slug):
    """Executor name from the hub: <specialist>-<gist in Latin letters>, a free one (taken — with a number); empty — None."""
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", "".join(_LAT.get(c, c) for c in str(slug or "").lower())).strip("-")[:24].strip("-")
    if not slug:
        return None
    base = f"{agent}-{slug}"[:36].strip("-.")
    name, n = base, 2
    while name in st["sessions"] or name in (st.get("runs") or {}):
        name, n = f"{base}-{n}", n + 1
    return name


_RECORD_HEAD = re.compile(r"(?m)^Task (d\d+): (?=its record is )")   # delegations.RECORD's start


def auto_name(st, agent, text):
    """A name by the gist of the task (owner decision 2026-10-04: do not duplicate naming): the specialist and the first
    words in Latin letters. Headers the core puts before the words are not the gist (core/forms.HEADS, both forms: names
    used to come out as "hub-dev-zadacha-ot" on 04.10, "hub-dev-otvet-vladelca" on 05.10). The name is built from the text,
    not from a separate words field: folding the journal tail with the old and the new code must give one name
    (otherwise the core would raise a second executor for the same task). So the task's record line
    (core/delegations.RECORD), English since the English texts, is read with the word the versions before wrote there."""
    t = forms.strip_heads(_RECORD_HEAD.sub(langdata.legacy("record_task") + r" \1: ", str(text)))
    words = []
    for w in langdata.fold(t.lower(), langdata.OLD).split():   # one language, as _LAT
        w = "".join(_LAT.get(c, c) for c in w)
        w = "".join(c for c in w if c.isascii() and (c.isalnum() or c == "-")).strip("-")
        if w:
            words.append(w)
        if len("-".join(words)) >= 16 or len(words) == 2:
            break
    # the name is at most 40 chars with the number (analysis 05.10: with a long specialist name the core kept refusing)
    base = (f"{agent}-" + ("-".join(words)[:24].strip("-") or "task"))[:36].strip("-.")
    name, n = base, 2
    while name in st["sessions"] or name in (st.get("runs") or {}):
        name, n = f"{base}-{n}", n + 1
    return name


# a constant line in the system instructions of a specialist's executor (owner decision 2026-10-05: specialists'
# executors must know they are executors, not the main specialists; they may not raise executors — the core rejects it)
EXECUTOR_NOTE = ("You are an executor of the specialist {agent} for one task — not {agent} itself. Its CLAUDE.md below is "
                 "its profile: you read it but do not edit it (lessons go into your result, as a \"Lessons\" block). You "
                 "do not raise executors (hub task run is closed to you): work bigger than the task, or new work — tell "
                 "{agent} (hub send {agent} \"…\"). Once you hand in the task, you fall asleep.")
# an addendum to it when the executor got a digest of the agent's profile (core/execprofile.py), not its whole CLAUDE.md
EXECUTOR_SHORT = ("Its profile in these instructions is a digest for executors; the full one is {path}: read it if what "
                  "you need is not in the digest.")


def takes_place(s):
    """Takes a place under whoever raised it: alive and not asleep (one that reported sleeps until explicitly woken —
    owner decision 2026-10-04) and an executor — a direction (with a backlog) is not an executor of whoever raised it
    (minihub.executor). The page snapshot follows the same rule."""
    return s["state"] in (*LIVE, "held") and minihub.runner(s)


def live_of(st, raiser):
    return [s for s in st["sessions"].values() if s.get("raised_by") == raiser and takes_place(s)]


def places(st):
    """Places for one queue pass (promotable, recount), in one walk of sessions and requests: live executors and ready
    requests per raiser and the totals under the hub ceiling (max_live). waits reads them instead of walking the whole
    state for every queued request (a big hub: thousands of sessions times the queue on every daemon tick). The pass
    itself changes no session or request state, so the counts hold to its end."""
    sessions, live, ready, running, ready_all = st["sessions"], {}, {}, 0, 0
    for s in sessions.values():
        if takes_place(s):
            live[s.get("raised_by")] = live.get(s.get("raised_by"), 0) + 1
            running += sessions_executor(st, s)
    for r in (st.get("runs") or {}).values():
        if r.get("state") == "ready":
            ready[r.get("by")] = ready.get(r.get("by"), 0) + 1
            ready_all += bool(r.get("by")) and (sessions.get(r["by"]) or {}).get("kind") != "hub"
    from core import budget
    return {"live": live, "ready": ready, "running": running, "ready_all": ready_all, "mode": budget.mode(st)}


def dormant(sess):
    """The executor handed in the task and went to sleep (state retired): messages do not wake it — they go to whoever
    raised it; the owner, the hub or whoever raised it wakes it (reducer, _wake_who)."""
    return bool(sess) and sess["state"] == "retired"


def _raiser(sess, to):
    """`to` raised this session and it is not a direction: the raiser's gate (guards.from_hub_owner_or_raiser)."""
    return bool(to) and to == sess.get("raised_by") and not sess.get("backlog")


def wakers(sess, to):
    """Who wakes it, as said to session `to` in a note: the raiser too - unless it is a direction (reducer._wake_who)."""
    return WAKERS_RAISER if _raiser(sess, to) else WAKERS_HUB


def revivers(sess, to):
    """Who brings a removed, closed or crashed executor back, as said to session `to`: the raiser too - unless it is a
    direction (guards.revives)."""
    return REVIVERS_RAISER if _raiser(sess, to) else WAKERS_HUB


REVIVERS_RAISER = T("core.executors.revivers_raiser", "(the hub, the owner or whoever raised it)")
WAKERS_RAISER = T("core.executors.wakers_raiser", "(the owner, the hub or whoever raised it)")
WAKERS_HUB = T("core.executors.wakers_hub", "(hub/owner)")
NAME_TAKEN = T("core.executors.name_taken", "the name {name} is taken")
NAME_FREE = T("core.executors.name_free", "; a free one: {free}")
NO_SESSION = T("core.mcpwatch.no_session", "no session {name}")
HERE_OWN = T("core.executors.here_own", "{path} is your folder: --here — only in a ready copy where nobody works")
HERE_TAKEN = T("core.executors.here_taken", "{path} is taken by {who}: two in one copy would get in each other's way")
# starts with forms.PREFIXES[WEEKLY_LIMIT] ("weekly limit"): the reason is told by its start without the code
WEEKLY = T("core.executors.weekly", "weekly limit of the account {pct}% (threshold {limit}%): waits for the owner's word")
PAUSE_KINDS = ("task.queue_paused", "task.queue_resumed")
PAUSE_RIGHT = "queue.pause"   # the Module API right to set and lift a pause of the module's own (the daemon checks it)
PAUSED = T("core.executors.paused", "the queue is paused ({by}: {why}) — hub task resume")
PAUSED_BY = T("core.executors.paused_by", "the owner")
PAUSED_WHY = T("core.executors.paused_why", "no reason")
HELD = T("core.executors.held", "held by the module {module}: {reason}")   # task.admit (core/daemon.admit_pass)
CAP_RAISER = T("core.executors.cap_raiser",
               "{raiser} already has {cap} working executors — waits until someone hands in and falls asleep")
CAP_HUB = T("core.executors.cap_hub",
            "{cap} working executors of sessions in all (the hub's ceiling) — waits until someone hands in and falls "
            "asleep")


def taken(st, name):
    return name in st["sessions"] and st["sessions"][name]["state"] != "removed" or name in (st.get("runs") or {})


def free_name(st, name, most=None):
    """name-2, name-3… (maps-2 is taken: maps-3, not maps-2-2): the first not taken, cut to fit the name's limit."""
    if most is None:
        from core import reducer   # reducer imports this module
        most = reducer.NAME_MAX
    base = re.sub(r"-\d{1,2}$", "", name) or name   # a counter, not a year or a ticket (release-2026)
    for n in range(2, 1000):
        tail = f"-{n}"
        free = base[:most - len(tail)].rstrip("-") + tail
        if not taken(st, free):
            return free
    return name


def check(st, name, raiser):
    """The refusal reason or None: only what the queue cannot fix (the name, no such raiser, the raiser has no
    executors)."""
    if taken(st, name):   # and a free one next to it (13.11: sessions guessed one more name, and it was taken too)
        return coretext.join(NAME_TAKEN.format(name=name), NAME_FREE.format(free=free_name(st, name)))
    if raiser:
        who = st["sessions"].get(raiser)
        if not who or who["state"] in ("removed", "closed", "failed"):
            return NO_SESSION.format(name=raiser)
    return refused(st, raiser)


def refused(st, raiser):
    """The raiser has no executors (parallelism 0 or "alone", core/minihub.alone) — the refusal text, otherwise None.
    One gate for all paths to a new executor: hub task run (check), hub route … executor, waking a sleeping executor;
    the core drops the queue and ready requests of such a session when the mode changes (reducer._drop_runs)."""
    return minihub.alone(st, st["sessions"].get(raiser or "") or {})


def _inside_text(path, top):
    """path is inside the working tree top by path text (a fold without disk): the folder itself or under it, except
    executor copies in <top>/.claude/worktrees (like hosts/worktree.in_tree on disk)."""
    p, t = (str(x).replace("\\", "/").rstrip("/") for x in (path, top))
    own = t + "/.claude/worktrees"
    return p == t or p.startswith(t + "/") and not (p == own or p.startswith(own + "/"))


def holder(st, top, me=None, inside=None):
    """Who works in the working tree top (the folder itself or under it), other than me: a session that is not removed
    and not a sleeping executor that reported (it is woken only through _wake_gate) — the name or None. inside is the
    comparison: the daemon uses the disk (hosts/worktree.in_tree: a link and a different letter case are the same
    folder), the fold uses path text (it has no disk; matching text means the same folder)."""
    inside = inside or _inside_text
    for s in st["sessions"].values():
        if s["name"] != me and s.get("cwd") and s["state"] not in minihub.GONE and not dormant(s) \
                and inside(s["cwd"], top):
            return s["name"]
    return None


def copy_of(sess):
    """The copy the executor considers its own: its working copy or a ready one (`--here`) — otherwise None (not git: the
    raiser's folder, shared)."""
    return (sess.get("worktree") or {}).get("path") or (sess.get("cwd") if sess.get("here") else None)


def here_refused(st, path, raiser, me=None, inside=None):
    """`hub task run … --repo W --here` (mini-hubs, slice 3): the executor works right in the ready copy W — not in the
    raiser's folder and not where a live session already works (two in one copy would get in each other's way). -> the
    refusal text or None."""
    inside = inside or _inside_text
    up = st["sessions"].get(raiser or "") or {}
    if up.get("cwd") and inside(up["cwd"], path):
        return HERE_OWN.format(path=path)
    who = holder(st, path, me, inside)
    return HERE_TAKEN.format(path=path, who=who) if who else None


def reset_passed(w, now):
    """The reset of window w ({used, resets}) has already passed by now — its share is stale (executor queue threshold, "Spending")."""
    return now is not None and bool(w.get("resets")) and w["resets"] <= now


CAPS = ("max_live",)   # the former max_per_agent and max_per_other exist only in old journals (parallelism replaced them)


def caps_from(cfg):
    """The shared ceiling from "Settings -> General -> Advanced -> Executors" (hub.json executors.max_live) — if set to an integer above zero;
    the core gets it as the owner.settings fact. How many each specialist and direction has is parallelism
    (core/minihub.parallel_of)."""
    ex = (cfg or {}).get("executors") if isinstance((cfg or {}).get("executors"), dict) else {}
    return {k: int(ex[k]) for k in CAPS if isinstance(ex.get(k), (int, float)) and not isinstance(ex[k], bool)
            and ex[k] >= 1}


def clean_drops(raw):
    """{agent: [tool, …]} from a raw value: names that are non-empty strings, once each; anything else is ignored."""
    out = {}
    for agent, names in (raw.items() if isinstance(raw, dict) else ()):
        if isinstance(agent, str) and isinstance(names, list):
            keep = list(dict.fromkeys(n for n in names if isinstance(n, str) and n.strip()))
            if keep:
                out[agent] = keep
    return out


def drop_tools_from(cfg):
    """hub.json executors.agents.<agent>.drop_tools: tools that agent's executors do not inherit from it (off by default;
    the core gets it as the owner.settings fact). A value that is not a list of names is ignored."""
    ex = (cfg or {}).get("executors") if isinstance((cfg or {}).get("executors"), dict) else {}
    agents = ex.get("agents") if isinstance(ex.get("agents"), dict) else {}
    return clean_drops({a: v.get("drop_tools") for a, v in agents.items() if isinstance(v, dict)})


def sessions_executor(st, sess):
    """An executor raised by a session (not by the hub or the owner) counts toward the shared max_live ceiling."""
    up = st["sessions"].get(sess.get("raised_by") or "") or {}
    return minihub.runner(sess) and bool(up) and up.get("kind") != "hub"


def budget_waits(st, owner_ok=False, now=None, live=False, mode=Ellipsis):
    """Whether the request waits because of the budget. With mini-hubs there is a saving mode (core/budget.py: live — a
    task from the owner's live word, or an urgent one); without — the old rule: the active account's weekly window
    near the threshold waits for the owner's word. With the budget gate off (core/budget.gate) the weekly threshold holds nothing."""
    from core import budget
    m = budget.mode(st) if mode is Ellipsis else mode   # mode — budget.mode(st) already taken for a queue pass
    if m:
        return budget.waits(st, owner_ok, live, m)
    if not budget.gate(st):
        return None
    window = ((st.get("limits") or {}).get("windows") or {}).get("seven_day") or {}
    week = 0 if reset_passed(window, now) else window.get("used") or 0
    if week >= BUDGET_OWNER_ONLY and not owner_ok:
        return forms.Why(WEEKLY.format(pct=round(week * 100), limit=round(BUDGET_OWNER_ONLY * 100)), forms.WEEKLY_LIMIT)
    return None


def waits(st, raiser, owner_ok=False, force=False, extra=0, now=None, extra_all=0, live=False, asked=False, index=None):
    """Why the request waits in the queue, or None — it can be raised (owner decision 2026-10-04: the queue lives in the core and the hub sees it):
    the budget waits for the owner's word; places under the raiser wait until someone sleeps (or the hub/owner raises it at once).
    now is the event time (fold) or the daemon clock: a window share whose reset has passed is stale. Ceilings come from settings
    (owner.settings): for a specialist and a direction it is parallelism (0 or "alone" — no requests: refused in check, the queue
    is dropped when the mode changes), for an executor and a worker — 6; the shared one (max_live) — for session executors, not the hub or the owner; extra/extra_all — already
    raised in this queue pass; live — a task from the owner's live word or an urgent one (limit saving); asked —
    the request itself (hub task run), not a queue pass: the pause lets through only it, by the owner's word, and hub task go;
    index — places(st) of this queue pass (the same counts without a walk of the state per request)."""
    held = budget_waits(st, owner_ok, now, live, index["mode"] if index else Ellipsis)
    if held:   # before the pause: the budget reason code is counted by the saving card and the mini-hub (budget.view)
        return held
    paused = st.get("queue_paused")
    if paused and not (force or (owner_ok and asked)):   # explicit pause: the queue itself raises nobody, not even a request
        # by the owner's word that waited for a place; the hub/owner use hub task go
        return PAUSED.format(by=paused.get("by") or PAUSED_BY, why=paused.get("why") or PAUSED_WHY)
    who = st["sessions"].get(raiser or "") or {}
    if raiser and not force and who.get("kind") != "hub":
        caps = (st.get("owner") or {}).get("executors") or {}
        n = minihub.parallel(st)
        if n is None:   # a journal before parallelism: the old ceilings
            cap = (caps.get("max_per_agent") or LIMIT_SPECIALIST) if who.get("role") == "agent" else \
                (caps.get("max_per_other") or LIMIT_OTHER)
        else:
            cap = minihub.limit(st, who) if minihub.lead(who) else LIMIT_OTHER   # its own number or the common N
        index = index or places(st)
        # ready-to-raise requests of the same raiser also take places (analysis 05.10: three in a row with two alive gave five)
        if index["live"].get(raiser, 0) + index["ready"].get(raiser, 0) + extra >= cap:
            return CAP_RAISER.format(raiser=raiser, cap=cap)
        top = caps.get("max_live")
        if top:
            running, ready = index["running"], index["ready_all"]
            if running + ready + extra_all >= top:
                return CAP_HUB.format(cap=top)
    return None


def promotable(st, now=None):
    """Queued requests that may rise now, in request order (the daemon writes task.run_ready)."""
    out, runs, taken, index = [], st.get("runs") or {}, {}, None
    for name, run in sorted(runs.items(), key=lambda kv: kv[1].get("ts") or 0):
        if run.get("state") != "queued" or run.get("held"):   # a held one is released by its module (admit_pass), not by room
            continue
        by, index = run.get("by"), index or places(st)   # one walk of the state for the whole pass
        if not waits(st, by, run.get("owner_ok"), run.get("force"), taken.get(by, 0), now,
                     sum(n for k, n in taken.items() if k and (st["sessions"].get(k) or {}).get("kind") != "hub"),
                     run.get("live"), index=index):
            out.append(name)
            taken[by] = taken.get(by, 0) + 1   # one place, one executor: count the next with the already raised one
    return out


def recount(st, now):
    """Queued requests get their reason anew, in request order and counting those already being raised in this pass (as
    promotable does): otherwise with one free place "will rise by itself" would be written to everyone. No reason — it waits only for the daemon."""
    taken, index = {}, None
    for _, run in sorted((st.get("runs") or {}).items(), key=lambda kv: kv[1].get("ts") or 0):
        if run.get("state") != "queued" or run.get("held"):   # a held one keeps the module's reason
            continue
        by = run.get("by")
        index = index or places(st)   # one walk of the state for the whole pass
        wait = waits(st, by, run.get("owner_ok"), run.get("force"), taken.get(by, 0), now,
                     sum(n for k, n in taken.items() if k and (st["sessions"].get(k) or {}).get("kind") != "hub"),
                     run.get("live"), index=index)
        if not wait:
            taken[by] = taken.get(by, 0) + 1
        run.pop("why_code", None)
        run.pop("why_l10n", None)
        run.update(forms.why_fields(wait or forms.READY_TO_RISE))


def pause_view(st):
    """The queue pause for the page: {by, why, ts, queued} — who set it, why (may be empty), since when and how many
    requests wait; without a pause — None. From the core state, not from the waiting requests' reasons."""
    p = st.get("queue_paused")
    if not p:
        return None
    return {"by": p.get("by"), "why": p.get("why") or "", "ts": p.get("ts"),
            "queued": sum(1 for r in (st.get("runs") or {}).values() if r.get("state") == "queued")}


def position(st, name):
    runs = st.get("runs") or {}
    mine = runs[name].get("ts") or 0
    return sum(1 for r in runs.values() if r.get("state") == "queued" and (r.get("ts") or 0) <= mine)


def events_for(st, name, run, cwd, worktree, here=False, repo_tools=()):
    """The events with which the daemon raises an executor: the session and its task. here — it works right in the ready
    copy cwd (`--here`): the copy is not its own, cleanup does not touch it. repo_tools — those of the task's --tools
    that its folder's .mcp.json brings anyway (catalog.repo_tools): no request to the owner for them, the brief says so."""
    raiser = run.get("by")
    if run.get("message"):   # raised by the core for a busy specialist: its waiting message passes to the executor
        session = {"session_id": str(uuid.uuid4()), "executor": True, "raised_by": raiser, "cwd": cwd,
                   "about": run["task"].strip().splitlines()[0][:120], "takes": run["message"]}
        session.update({k: run[k] for k in ("model", "effort") if run.get(k)})
        if worktree:
            session["worktree"] = worktree
        return [("session.requested", name, session)]
    rule = REPORT_RULE.format(raiser=raiser or RAISER)
    base = (worktree or {}).get("base")
    if base and base != "main":   # experiment, slice C: it merges into the base of its copy, not into main (on_unmerged)
        rule = rule.replace(MERGE_MAIN, MERGE_INTO.format(base=base), 1)
    words = run["task"] + "\n\n" + rule   # for a mini-hub from birth it is BORN_RULE (born_brief)
    if here:   # slice 3: whose copy and which branch — otherwise "your own copy, merge to main" from the rule above misleads
        words += (f"\n\nYou work right in the ready copy {cwd} — it is not yours: do not change its branch, do not remove "
                  "the copy, hand in the way it is done there (an MR of its branch).")
    elif run.get("branch"):
        words += (f"\n\nYour copy is on the existing branch {run['branch']}: continue it (and its MR), do not start a new "
                  "branch.")
    note = source_note(worktree)
    if note:
        words += "\n\n" + note
    if repo_tools:
        words += ("\n\nAlready available in your repo (its .mcp.json), nothing to request: "
                  + ", ".join(repo_tools) + ".")
    backlog = (st["sessions"].get(raiser or "") or {}).get("backlog")
    if backlog:   # a direction's executor: its backlog is the shared plan (owner decision 2026-10-04)
        words += (f"\n\nYou work for the direction {raiser}: its backlog is `hub direction {backlog} task list` "
                  f"(tasks, decisions, documents); what you did on a backlog task — mark it there "
                  f"(`hub direction {backlog} task edit …`) and in the task record.")
    session = {"session_id": str(uuid.uuid4()), "executor": True, "raised_by": raiser, "cwd": cwd,
               "about": run["task"].strip().splitlines()[0][:120],
               **{k: run[k] for k in ("model", "effort") if run.get(k)}}
    if repo_tools:
        session["repo_tools"] = list(repo_tools)   # a fact of the daemon about the disk (reducer: only from the core)
    if worktree:
        session["worktree"] = worktree
    if here:
        session["here"] = True
    task = {"session": name, "for_session": raiser, "context": words,
            "lead": len(run["task"]),   # the setter's words are the start of the context, the rest is the core's frame
            **({"board": run["board"]} if run.get("board") else {}),   # a card of the direction's board (core/board.py)
            **({"channel": "session", "owner": False, "sender": raiser} if raiser else {"channel": "cli", "owner": True})}
    return [("session.requested", name, session), ("delegation.created", None, task)]


def born_brief(st, name):
    """A mini-hub from birth (core/minihub.born): its task from hub task run, not yet typed, gets BORN_RULE instead of
    "you are an executor" (REPORT_RULE): in the task record and in its message. Other texts stay as they are."""
    for d in st["delegations"].values():
        msg = st["messages"].get(d.get("message") or "")
        if d["session"] != name or not msg or msg["state"] != "queued":
            continue
        raiser = d.get("for_session")
        new = BORN_RULE.format(raiser=raiser or RAISER)   # as in events_for
        # the rule as this version writes it, and as the versions before did (a task queued at the update): before the
        # lesson bar, before the lesson hint, and in Russian (the raiser word OLD_RAISER)
        olds = [rule.format(raiser=raiser or who) for rule, who in (
            (REPORT_RULE, RAISER), (REPORT_RULE.replace(LESSON_BAR, "", 1), RAISER),
            (REPORT_RULE.replace(LESSON_BAR, "", 1).replace(LESSON_HINT, "", 1), RAISER),
            (REPORT_RULE_RU, OLD_RAISER), (REPORT_RULE_RU.replace(LESSON_HINT_RU, "", 1), OLD_RAISER))]
        for old in olds:
            for item, key in ((d, "context"), (msg, "text")):
                if old in str(item.get(key) or ""):
                    item[key] = item[key].replace(old, new, 1)


# --- retirement of an executor that reported (hubd, retire_executors) --------------------------------------------------
RETIRE_AFTER = 120  # seconds idle after the report — then the executor falls asleep (owner decision 2026-10-03)
RETIRE_AFTER_HUB = 30 * 60  # a hub's executor (the owner's task): half an hour for questions and "make it a direction" (04.10)
WAITING_HOLD = 30 * 60  # s; this long after the result an executor with its own `hub waiting` does not fall asleep — it awaits an answer and will answer at once (22.10)
EXECUTOR_KEEP = 0  # s; a reported executor sleeps this long and is removed from the list (hub.json executors.remove_after_hours; 0 — do not remove, the default)


# --- an executor's branch not merged into the base of its copy --------------------
UNMERGED = T("core.msg.unmerged",
             "Core: your branch {branch} is not merged into {base}{what} — the task is not handed in. Merge it (rebase on a "
             "fresh branch {base}, tests, `git merge --ff-only` into {base}) or answer in one line why you can't.")
UNMERGED_UP = T("core.msg.unmerged_up",
                "Executor {name}: branch {branch} is not merged into {base}{what}{again} — the result above is without the merge.")
AGAIN = T("core.msg.unmerged_again", " even after the reminder")   # {again} of UNMERGED_UP: the executor was reminded and still did not merge


AHEAD = T("core.msg.unmerged_ahead", {"one": "{count} commit ahead", "other": "{count} commits ahead"})
DIRTY_COPY = T("core.msg.unmerged_dirty", "uncommitted changes in the copy")
UNMERGED_TRAIL = T("core.executors.unmerged_trail", "reminder: the branch is not merged into {base}{what}")


def _what(data):
    n = data.get("ahead") or 0
    parts = ([AHEAD.format(count=n)] if n else []) + ([DIRTY_COPY] if data.get("dirty") else [])
    return coretext.join(" (", parts[0], *("; " + part for part in parts[1:]), ")") if parts else ""


def on_unmerged(st, ev, data, ctx):
    """The daemon saw after an executor's result (data.seq — the result event) that the branch of its copy is not merged
    into the base (hosts/worktree.merged). The first time for the task — a reminder to the executor (its answer continues
    the same task); again, or the executor is no longer live (gone, asleep after handing in) — a line to whoever raised it
    (raised by the hub or the owner — to the hub). The counter is in the session state per task (survives a core restart,
    a new task starts over); the same result a second time — no effect."""
    if ev.get("src") != "core":
        raise Rejected(T("core.executors.merge_core", "the core checks the merge"))
    sess = st["sessions"].get(ev.get("entity") or "")
    if not sess or not minihub.runner(sess) or not (sess.get("worktree") or {}).get("base"):
        raise Rejected(T("core.executors.no_base", "no executor with a copy base"))
    mark = sess.get("unmerged") or {}
    if mark.get("seq") == data.get("seq"):
        return
    name, did = sess["name"], data.get("task")
    n = (mark.get("n") or 0) + 1 if mark.get("task") == did else 1
    sess["unmerged"] = {"seq": data.get("seq"), "n": n, "task": did}
    words = {"name": name, "branch": data.get("branch") or "", "base": data.get("base") or "", "what": _what(data)}
    from core import delegations
    if n == 1 and sess["state"] in (*LIVE, "parked"):   # a parked one is raised by the message; retired (handed in) — not
        ctx["new_message"](st, f"m{ev['seq']}", name, UNMERGED.format(**words), ev["ts"], origin=forms.ORIGIN_CORE,
                           **({"for_delegation": did} if did else {}))
        delegations.trail(st, did, delegations.BY_CORE, UNMERGED_TRAIL.format(base=words["base"], what=words["what"]))
        return
    line = UNMERGED_UP.format(**words, again=AGAIN if n > 1 else "")
    delegations.trail(st, did, delegations.BY_CORE, line)
    up = st["sessions"].get(sess.get("raised_by") or "") or {}
    if up and up.get("kind") != "hub" and up["state"] not in minihub.GONE:
        ctx["new_message"](st, f"m{ev['seq']}", up["name"], line, ev["ts"], origin=forms.ORIGIN_CORE,
                           **({"task": did} if did else {}))
    else:
        ctx["notify"](ev, line, **({"task": did} if did else {}))


def unmerged_due(st):
    """Executor results whose merge to check (hubd, check_merged): an executor with a copy base (not `--here`, not
    `--branch`), its turn is over (idle or parked, not asleep after handing in), its task has answered, and the fold has
    not looked at this result yet -> [(name, task, seq of the result, copy)]."""
    out, last = [], None
    for name, s in st["sessions"].items():
        wt = s.get("worktree") or {}
        if not minihub.runner(s) or not wt.get("base") or wt.get("keep_branch") or s.get("here") \
                or s["state"] not in ("idle", "asleep", "parked") or dormant(s):
            continue
        if last is None:   # one pass over the tasks per call, only when there is an executor to look at
            last = {}
            for d in st["delegations"].values():
                if d.get("replied_seq") and d["replied_seq"] > (last.get(d["session"]) or {}).get("replied_seq", -1):
                    last[d["session"]] = d
        d = last.get(name)
        if d and (s.get("unmerged") or {}).get("seq") != d["replied_seq"]:
            out.append((name, d["id"], d["replied_seq"], wt))
    return out


def retiring(st, now, keep=EXECUTOR_KEEP):
    """[("sleep", name) | ("remove", name)]: an executor that did its job and reported goes to sleep without a handover
    (the report was already given); one that reported sleeps keep seconds and is removed — bring it back with hub session
    revive (off by default — owner decision 2026-10-04: let them pile up, nothing will be lost). Directions (with a
    backlog) leave only on "finish?"."""
    from core.guards import closable
    out = []
    for name, s in st["sessions"].items():
        if not minihub.runner(s):
            continue
        if s.get("adopted_at") and now - s["adopted_at"] < RETIRE_AFTER:
            continue   # just migrated (hub migrate) — do not retire it in the same minute
        if s["state"] in (*SLEEP, "closed", "failed"):
            # asleep with its conversation and copy, woken explicitly; removed only on an enabled timeout — and not one the
            # owner wrote to directly (owner_talks: their interlocutor, they remove it), nor one with its task still open
            # or awaiting an answer. Any sleep counts: handed in (retired), parked (hub restart, idle, plugins: parking
            # takes a free executor right after its report, before RETIRE_AFTER) or put to sleep by its raiser or on a
            # closed task with a plain sleep (asleep) — otherwise those pile up under the raiser with their working copies
            if keep and s["state"] in SLEEP and now - s["since"] >= keep \
                    and not s.get("owner_talks") and not _open_work(st, name, now) and not awaits(st, name, s, now) \
                    and not _woken_once(st, name) and not _scheduled(st, name, now):
                out.append(("remove", name))
        elif s["state"] == "idle" and s.get("closing", "none") == "none" \
                and now - max(s["since"], s.get("background_done") or 0) >= _after(st, s) and closable(st, s, now=now) is None \
                and not _asks(st, s, now):
            # a stale card not raised yet holds it: asleep, askexpired would never wake it (it wakes idle and parked)
            out.append(("sleep", name))
    return out


def _after(st, s):
    up = st["sessions"].get(s.get("raised_by") or "") or {}
    after = RETIRE_AFTER_HUB if not s.get("raised_by") or up.get("kind") == "hub" else RETIRE_AFTER
    # its `hub waiting` means at least WAITING_HOLD: it awaits an answer and will answer at once (22.10, it was answering the
    # owner's comments on a spec — the core put it to sleep 2 minutes after the result). Cleared (--clear) or stale (it was written to) — as before;
    # the ceiling: a forgotten mark does not hold a place under the raiser. Putting it to sleep by hand works as before (closable ignores it)
    return max(after, WAITING_HOLD) if tools.waiting_now(s) else after


def awaits(st, name, s, now):
    """One that reported still awaits an answer — the remove_after_hours timeout does not remove it (22.10: it awaited the
    owner's comments on a spec, a one-hour timeout deleted it): its own `hub wait` check is ahead (ceiling — its timeout,
    --for) or a fresh `hub waiting` mark (not older than WAITING_HOLD: a forgotten one does not hold)."""
    from core import schedule
    w = tools.waiting_now(s)
    return bool(w and now - (w.get("ts") or 0) < WAITING_HOLD) or any(
        j["state"] == "active" and schedule.wakes(j, name) and schedule.WAIT_ID.match(j["id"])
        for j in st.get("schedule", {}).values())


def _scheduled(st, name, now):
    """What the removal term must not cut short besides a `hub wait` check: its place in the queue for a rework (a removed
    executor cannot be raised by `hub task go`) and a recurring job that wakes it with a finite term ahead (`until`); one
    without a term does not hold — it would hold forever, and on removal it goes to the hub to reassign, as before."""
    from core import schedule
    return name in (st.get("runs") or {}) or any(
        j["state"] == "active" and j.get("kind") != "at" and schedule.wakes(j, name) and now < schedule.end(j) < float("inf")
        for j in st.get("schedule", {}).values())


def _woken_once(st, name):
    """A one-shot job ahead will wake it (a recheck its raiser scheduled: a session job or `hub session wake` as a
    command) — removed before it, the job would wake a session that is gone (a recurring one: _scheduled). A command
    job is done on due, but the clock runs it a little later: it holds until the run is recorded (the clock's own
    condition to launch it)."""
    from core import schedule
    return any(j.get("kind") == "at" and schedule.wakes(j, name)
               and (j["state"] == "active"
                    or (j.get("command") and j.get("run_slot") and j.get("ran_slot") != j["run_slot"]))
               for j in st.get("schedule", {}).values())


def _open_work(st, name, now):
    return any(d["session"] == name and specmode.holds(d) for d in st["delegations"].values()) \
        or any(m["session"] == name and m["state"] in ("queued", "typing") for m in st["messages"].values()) \
        or _asks(st, st["sessions"].get(name) or {}, now)


def _asks(st, s, now):
    """A card holds the executor while it is open (owner.open_asks) and, once stale, until askexpired raised it once
    (revived): removed or asleep first, the question would never be asked again. A retired one is never raised
    (askexpired.holds), so its stale card does not keep it from removal."""
    return bool(owner.open_asks(s, now) or askexpired.holds(s, now, st["delegations"]))


def worktrees_to_clean(st):
    """Removed sessions whose working copy is still on disk — to clean up (hubd, hosts/worktree.remove)."""
    return [s for s in st["sessions"].values() if s["state"] == "removed" and (s.get("worktree") or {}).get("path")
            and not s["worktree"].get("cleaned")]
