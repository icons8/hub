"""Journal fold: (state, event) -> (state, actions, rejection).

A pure function: no clock, no I/O. Time comes from the event's ts. The same journal always yields the same
state, so replaying a recorded failure is a deterministic test.
"""
import copy
import json
import os
import re
import traceback

from core import (appwatch, askexpired, backbone, board, budget, busstate, catalog, chain, chats, codeedits, configwatch, coretext, corewrite, delegations, executors, gitaccess, guards, hubrun, identity, journal, layout,
                  langdata, i18n, lessons, liaison, forms, limitresume, mcpwatch, minihub, modelrule, modprompt, modrun, modstate, news, owner, parking, profcap, redact, refusals, restart, schedule, specmode, system,
                  tools)
from core.coretext import T
from core.guards import checker
from core.machines import (CLOSING, COMMAND_CONFIRM_SECONDS, CONFIRM_SECONDS, DELEGATION, ENDED, FACTS, GAVE_UP,
                           HUB_MAX_DELAY, LIVE, MAX_ATTEMPTS, MAX_RESTARTS, MESSAGE, OWNER, SESSION, SLEEP, decide,
                           refusal, step)
from core.tools import Rejected

ABS_PATH = re.compile(r"^(?:[/\\]|[A-Za-z]:[/\\])")


def is_abs_path(path):
    """An absolute path: starts with "/", a backslash, or "drive-letter colon slash". Parsed as text, not via
    os.path/ntpath: hubs on different OSes and Python versions read the journal (ntpath.isabs answers differently on
    3.12 and 3.13 for a path with a single backslash)."""
    return bool(ABS_PATH.match(str(path or "")))


# the word a session answers the core's "continue" with when there is nothing to continue (delegations.NOTHING; a literal:
# evals read the phrases below from the source, without imports — tests/test_coretext.py keeps the two equal)
NOTHING = "nothing to continue"
EDITS_SHOWN = 50   # the files of the hub's own edits that the state keeps (modules/update EDITS_SHOWN: the same number)
# the letters of core/codeedits.classify: git diff's (changed, added, deleted, type changed, unmerged) and "?" — new outside
# git; anything else is shown as "?"
EDIT_KINDS = ("M", "A", "D", "T", "U", "?")


def _whole(n):
    return isinstance(n, int) and not isinstance(n, bool) and n >= 0


def app_shell_shape(got):
    """update.checked's "shell" (modules/update/layer.py): {runtime, size, auto, deb, file} with these types only."""
    if not isinstance(got, dict) or not isinstance(got.get("runtime"), int) or isinstance(got["runtime"], bool):
        return None
    out = {"runtime": got["runtime"], "auto": got.get("auto") is True, **({"deb": True} if got.get("deb") is True else {})}
    if isinstance(got.get("size"), int) and not isinstance(got["size"], bool):
        out["size"] = got["size"]
    if isinstance(got.get("file"), str) and 0 < len(got["file"]) <= 300:
        out["file"] = got["file"]
    return out


def minimum_shape(low):
    """The minimum version a hub is below (modules/update/layer.py _below), as the fold keeps it, or None."""
    if not (isinstance(low, dict) and low.get("below") is True and isinstance(low.get("version"), str)):
        return None
    reason = low.get("reason") if isinstance(low.get("reason"), dict) else {}
    return {"version": low["version"][:64], "below": True,
            "reason": {str(k)[:16]: v[:2000] for k, v in list(reason.items())[:40] if isinstance(v, str)},
            "stops": [str(s)[:32] for s in low.get("stops") or [] if isinstance(s, str)][:16]
            if isinstance(low.get("stops"), list) else [],
            **({"since": low["since"][:10]} if isinstance(low.get("since"), str) else {}),
            **({"ahead": True} if low.get("ahead") is True else {})}


def local_edits_shape(v):
    """{"count", "files": [{"path", "st"}], "modules"?, "base"?, "backup"?} from the update.checked fact (the hub's own edits of the
    core), or None for anything else: the fact comes from a process and its shape is not trusted — a whole non-negative
    count, at most EDITS_SHOWN files of a one-line path and one letter of EDIT_KINDS, base — a full commit hash."""
    if not isinstance(v, dict) or not _whole(v.get("count")):
        return None
    # a path with a line break or another control character is shown escaped: it goes into a task for a session as one line
    files = [{"path": "".join(c if c.isprintable() else f"\\x{ord(c):02x}" if ord(c) < 256 else f"\\u{ord(c):04x}"
                              for c in f["path"][:200]), "st": f["st"][:1] if f["st"][:1] in EDIT_KINDS else "?",
              **({"zone": f["zone"]} if f.get("zone") in codeedits.ZONE_KEYS else {})}
             for f in (v.get("files") if isinstance(v.get("files"), list) else [])
             if isinstance(f, dict) and isinstance(f.get("path"), str) and f["path"] and isinstance(f.get("st"), str)
             and f["st"]][:EDITS_SHOWN]
    mods = [m[:60] for m in (v.get("modules") if isinstance(v.get("modules"), list) else []) if isinstance(m, str) and m.isprintable()]
    base = v.get("base")
    # files by zone (core/codeedits.ZONES), whole counts: only the known zones and positive whole numbers
    zones = {k: n for k, n in (v.get("zones") if isinstance(v.get("zones"), dict) else {}).items()
             if k in codeedits.ZONE_KEYS and isinstance(n, int) and not isinstance(n, bool) and n > 0}
    # the last take-off that is not put back yet (modules/update/takeoff.py): the page offers "Put back"
    b = v.get("backup")
    backup = {"name": b["name"], "count": b["count"], "at": b["at"]} if isinstance(b, dict) and isinstance(b.get("name"), str) \
        and codeedits.BACKUP_NAME.fullmatch(b["name"]) and _whole(b.get("count")) \
        and _whole(b.get("at")) else None
    # the release's tests that went red on the hub's code (modules/update/shadow.py): test names, at most 10, the whole
    # count; and the release's hunks a merge may have lost: a signal id and a one-line path, at most 10, the whole count
    s = v.get("shadow")
    red = [n for n in (s.get("failed") if isinstance(s, dict) and isinstance(s.get("failed"), list) else [])
           if isinstance(n, str) and len(n) <= 300 and re.fullmatch(r"tests\.[\w.]+", n)][:10]
    shadow = {"failed": red, "count": s["count"] if _whole(s.get("count")) and s["count"] >= len(red) else len(red),
              **({"version": s["version"][:20]} if isinstance(s.get("version"), str) and s["version"].isprintable()
                 and s["version"] else {})} if red else None
    lo = v.get("lost")
    items = [{"id": i["id"], "path": i["path"][:200]}
             for i in (lo.get("items") if isinstance(lo, dict) and isinstance(lo.get("items"), list) else [])
             if isinstance(i, dict) and isinstance(i.get("id"), str) and re.fullmatch(r"[0-9a-f]{8}", i["id"])
             and isinstance(i.get("path"), str) and i["path"] and i["path"].isprintable()][:10]
    lost = {"items": items, "count": lo["count"] if _whole(lo.get("count")) and lo["count"] >= len(items) else len(items)} \
        if items else None
    return {"count": v["count"], "files": files, **({"zones": zones} if zones else {}),
            **({"shadow": shadow} if shadow else {}), **({"lost": lost} if lost else {}),
            **({"backup": backup} if backup else {}), **({"modules": mods[:20]} if mods else {}),
            **({"base": base} if isinstance(base, str) and re.fullmatch(r"[0-9a-f]{40,64}", base) else {})}


MARKER = "[hub:{id}{origin}] "
# appended to every owner word to a session, except Claude Code commands (owner decision 2026-10-08: a session asked
# in text at the end of its reply, which is inconvenient; tasks have this in CONTRACT, direct messages did not; eval
# on a copy of the conversation 0 -> 1)
# a card is only for his decision and reads without the feed (16.10: sessions brought cards for a technical choice
# inside a task; eval before/after: cards on a tech choice 7 of 8 -> 9 of 18, all 9 real decisions; prod decisions
# via the form 3 of 3). 28.10 the card rule moved to the system prompt (hosts/streamjson.CARDS: it is not lost by distance
# or compaction); the tail keeps only the form-only line, which a comment thread replaces
ASK_BY_FORM = ("A question to the owner — only through the AskUserQuestion form, not as text at the end of the answer; "
               "the card's first line — what happened and why it is theirs to decide.")
# owner words from a file-comment thread: the turn's result goes to the same thread, so ask there (owner decision
# 2026-10-10: negotiations held in file comments must be accounted for; the form diverted the question to the feed)
ASK_IN_THREAD = ("A question to the owner — as text in this answer (it will show in the comment thread), not through the "
                 "AskUserQuestion form.")


def empty():
    return {"seq": 0, "sessions": {}, "messages": {}, "aliases": {}, "catalog": {}, "delegations": {},
            "owner": owner.initial(), "schedule": {},
            "browsers": {}, "limits": {}}


# `hub session model` with a model the Codex host does not take (core/modelrule.codex_name refuses it in the command)
MODEL_BAD_CODEX = T("core.reducer.model_bad_codex", "the model is codex/<id> — a model of the hub's Codex (its list: the "
                                                    "page's model menu) — or default; only where the hub runs Claude "
                                                    "sessions too (not entirely on Codex) — also one of {models} or a "
                                                    "full id claude-…")
# an error in the fold code (reduce): the start is forms.PREFIXES[CORE_FAILURE], which readers without the code field match
CORE_FAILURE_TEXT = T("core.reducer.core_failure", "core failure on event {type}: {error}: {detail} ({where})")


def reduce(state, event, known=None):
    """Apply an event. Returns (new state, actions, rejection reason or None).

    A rejected event does not change the state. known is what hubd has already recorded about the event (journal
    readers learn it from the next line, verdicts): "rejected" - leave the state alone; "accepted" - apply in place,
    without copying the state (a copy per event costs minutes on the feed, 01.10)."""
    if known == "rejected":
        state["seq"] = event.get("seq", state["seq"])
        return state, [], T("core.reducer.rejected_in_journal", "rejected in the journal")
    new = state if known == "accepted" else copy_state(state)
    new["seq"] = event.get("seq", new["seq"])
    actions = []
    try:
        modstate.dedup_check(new, event)   # a repeat module fact with the same dedup_key is refused, citing the accepted one
        f = event.get("fold")   # a module's core.write decision on the same line (core/corewrite.py)
        if f is not None and f.get("mode") == "reject":
            raise Rejected(T("core.reducer.module_refused", "module {mod}: {reason}").format(mod=f.get("mod"),
                                                                                              reason=f.get("reason")))
        if f is None or f.get("mode") != "replace":
            _apply(new, event, actions)
        if f is not None or "by" in event or event.get("type") == "core.rejected":
            corewrite.folded(new, event)
        for name in new.pop("_wake", []):
            actions.append(spawn_action(new["sessions"][name], st=new))
        for n in new.pop("_notes", []):   # a task trail goes into its record, written by the daemon (delegations.trail)
            actions.append({"do": "task_note", "session": None, **n})
        for fact in new.pop("_facts", []):   # a fact the daemon writes after this one (schedule._drop: a milestone the core removed)
            actions.append({"session": None, **fact})
        _closing_after_turn(new, event, actions)
        _pump(new, event, actions)
        hubrun.follow(new, event)   # the runs (`hub run`) of a session that went stop with it
        owner.settle(new)   # the "not delivered" notice holds until the message arrives
        modstate.dedup_take(new, event)
    except Rejected as e:
        why = e.args[0] if len(e.args) == 1 and isinstance(e.args[0], str) else str(e)   # forms.Why carries a code
        return (new if known else _unchanged(state, new)), [], why
    except Exception as e:  # noqa: BLE001
        # an error in the fold code rejects this event instead of crashing the core: incident 30.09 14:16, a KeyError in
        # _rename brought down hubd and all modules on every journal read. The cause is visible in the journal
        # (core.rejected)
        where = traceback.extract_tb(e.__traceback__)[-1]
        return (new if known else _unchanged(state, new)), [], forms.Why(CORE_FAILURE_TEXT.format(
            type=event.get("type"), error=type(e).__name__, detail=str(e),
            where=f"{os.path.basename(where.filename)}:{where.lineno}"), forms.CORE_FAILURE)
    return new, actions, None


DECISIONS_KEPT = 100   # `hub decided` records kept in the state for `hub decided list` and a withdrawal: the last ones
PRUNABLE = ("delivered", "failed")
# agent forks (before 04.10, replaced by executors): their events in old journals fold without consequences
OLD_FORKS = ("session.fork_requested", "session.fork_failed", "session.queue_moved")


def prunable(st, before):
    keep = {x for d in st["delegations"].values() if d["state"] not in ENDED for x in (d.get("for"), d.get("message"))}
    return any(m["state"] in PRUNABLE and m["created"] < before and mid not in keep for mid, m in st["messages"].items())


def copy_state(o):
    """A deep copy of the state (only dict, list and JSON values), 1.5 times faster than copy.deepcopy, which would run
    on every event (01.10: a feed of 43k events took minutes)."""
    t = type(o)
    if t is dict:
        return {k: copy_state(v) for k, v in o.items()}
    if t is list:
        return [copy_state(v) for v in o]
    if t in (str, int, float, bool) or o is None:
        return o
    return copy.deepcopy(o)


def _unchanged(state, new):
    state = copy_state(state)
    state["seq"] = new["seq"]
    return state


def verdicts(events):
    """(event, known): hubd writes core.rejected on the line after a rejected event, so the outcome is known for all
    but the last event; the last one gets None (fold with a copy)."""
    for i, e in enumerate(events):
        if i + 1 == len(events):
            yield e, None
        else:
            nxt = events[i + 1]
            rejected = nxt["type"] == "core.rejected" and (nxt.get("data") or {}).get("of") == e.get("seq")
            yield e, "rejected" if rejected else "accepted"


def fold(state, events):
    """Fold a batch of journal events (readers only: hubd folds its own events itself, via Hub.fold)."""
    for e, known in verdicts(events):
        if e["type"] == "core.snapshot" and (e.get("data") or {}).get("state"):
            state = restore(e)
        else:
            state, _, _ = reduce(state, e, known)
    return state


def replay(events):
    """State from the journal: from the last snapshot, then events by the current code.

    hubd writes a snapshot on stop (and every SNAPSHOT_EVERY events, no more often than the events since weigh one —
    daemon.snapshot_due): new code does not reread old events under new rules. Case 29.09: after the sleep machine changed, old "put to sleep" events were read as "hand over your work",
    and the core woke six sleeping sessions."""
    start = 0
    state = empty()
    for i in range(len(events) - 1, -1, -1):
        if events[i]["type"] == "core.snapshot" and (events[i].get("data") or {}).get("state"):
            state, start = restore(events[i]), i + 1
            break
    if start and "decisions" not in events[start - 1]["data"]["state"]:   # a snapshot of a core without `decided list`
        backfill_decisions(state, events[:start])
    return fold(state, events[start:])


def restore(snapshot_event):
    state = {**empty(), **copy.deepcopy(snapshot_event["data"]["state"])}
    state["seq"] = snapshot_event["seq"]
    return loaded(state)


def loaded(state):
    """State from a snapshot or a state file (including a previous hub's): without secrets, the watchdog's service
    entries under new keys, sleep flags turned into SESSION states (upgrade). All read paths go through here."""
    return upgrade(watch_keys(without_secrets(state)))


def decisions_pending(state, rotated):
    """The state has not had its `hub decided` records looked for in the journal and the archives (hubd does it at start,
    once: decisions_searched): it has no key (a core before `hub decided list`), or the journal was rotated, so a list
    built from its carried part may lack the older records (10.11 a hub listed none, 12 recorded)."""
    return not state.get("decisions_searched") and ("decisions" not in state or rotated)


def backfill_decisions(state, events, complete=False):
    """A snapshot written by a core without `hub decided list` has no state["decisions"]: they are built from the
    journal events given (owner.decided and the withdrawals among them, up to the snapshot), so that the decisions
    recorded before the update can be listed and withdrawn. complete: events come from the live journal and the
    archives (journal.decided_events), so an empty list is final - the key decisions_searched marks it, and hubd does not
    read the journal for it at every start; without it (a replay of a rotated journal) the list may lack the records.
    The records the state already has stay as they are (they carry what the events do not: slug, told, revoked); the
    events only add the ones it lacks."""
    found = []
    # what the core rejected counts for nothing: a withdrawal an older hubd refused as an unknown type withdrew nothing
    rejected = {(ev.get("data") or {}).get("of") for ev in events if ev["type"] == "core.rejected"}
    for ev in events:
        if ev["seq"] > state.get("seq", 0):
            break
        data = ev.get("data") or {}
        if ev["seq"] in rejected:
            continue
        if ev["type"] == "owner.decided":
            found.append({"seq": ev["seq"], "by": data.get("sender"), "text": data.get("text"), "ts": ev["ts"]})
        elif ev["type"] == "owner.decided_revoked":
            for d in found:
                if d["seq"] == data.get("seq") and not d.get("revoked"):
                    d["revoked"] = {"by": data.get("sender"), "ts": ev["ts"]}
    have = {d["seq"]: d for d in state.get("decisions") or []}
    for d in found:
        if d["seq"] in have:
            if d.get("revoked") and not have[d["seq"]].get("revoked"):
                have[d["seq"]]["revoked"] = d["revoked"]
        else:
            have[d["seq"]] = d
    state["decisions"] = [have[n] for n in sorted(have)][-DECISIONS_KEPT:]
    if complete:
        state["decisions_searched"] = True
    return state


def upgrade(state):
    """A previous core's snapshot (state.json, core.snapshot) brought to the current form; changes nothing on a current
    one. Before this change (25.10) parking and "handed in the task" were flags of a sleeping session (asleep + parked,
    asleep + retired on an executor); now they are SESSION states (core/machines.SLEEP); the flags are removed from all
    sessions (they also remained on deleted and on awakened ones). An agent without a profile gets its session_id as
    one (03.11): a new conversation (/clear) changes the id, the file key must not follow it."""
    for s in (state.get("sessions") or {}).values():
        if s.get("role") == "agent" and s.get("session_id") and not s.get("profile"):
            s["profile"] = s["session_id"]   # an agent from before 03.11: its file key pinned before the id changes
        parked, retired = s.pop("parked", None), s.pop("retired", None)
        if s.get("state") == "asleep" and retired and minihub.runner(s):
            s["state"] = "retired"
        elif s.get("state") == "asleep" and parked:
            s["state"] = "parked"
    _codex_rows(state)
    return state


def _codex_rows(state):
    """Codex (experimental): a core before the Codex limits row kept a Codex reading in st["limits"]
    (and in the default login's row): that reading becomes the Codex row. A hub not entirely on Codex gets a pool
    account's Claude row back, otherwise none until the next Claude reading (the default login's numbers may be Codex's
    — they carry no engine)."""
    lim = state.get("limits")
    if not isinstance(lim, dict) or lim.get("engine") != "codex" or not isinstance(state.get("codex"), dict) \
            or state["codex"].get("limits"):
        return
    state["codex"]["limits"] = dict(lim)
    if not state["codex"].get("whole"):
        acc = state.get("account") or {}
        seen = (acc.get("seen") or {}).get(acc.get("active") or "")
        state["limits"] = {**seen, "account": acc.get("active")} if seen and acc.get("active") else {}


def legacy(state):
    """The reverse of upgrade, for rolling back to the previous code (tools/sleep_states.py down): parked -> asleep +
    parked, retired -> asleep + retired (with the time since)."""
    for s in (state.get("sessions") or {}).values():
        if s.get("state") == "parked":
            s.update(state="asleep", parked=True)
        elif s.get("state") == "retired":
            s.update(state="asleep", retired=s.get("since"))
    return state


def without_secrets(state):
    """A previous hub's snapshot or state file kept the env and headers of MCP servers in the catalog; the state without them."""
    state["catalog"] = redact.public_catalog(state.get("catalog"))
    return state


def watch_keys(state):
    """The watchdog's service entries in st["watch"] used to sit next to session names ("surrender" is a list, "core"
    is a time): moved to keys with ":". A list under "surrender" is always service (a session holds a time there); a
    time under "core" is service if no session has that name (a previous hub's entry is otherwise indistinguishable,
    and we do not take it away from a session)."""
    box = state.get("watch")
    if not isinstance(box, dict):
        return state
    if isinstance(box.get("surrender"), list):
        box.setdefault(SURRENDER_KEY, box["surrender"])
        del box["surrender"]
    if "core" in box and "core" not in (state.get("sessions") or {}) and CORE_KEY not in box:
        box[CORE_KEY] = box.pop("core")
    return state


# --- events --------------------------------------------------------------------------------------------------

MCP_HELD_OUTSIDE = T("core.msg.mcp_held_outside",
                     "MCP {tool} did not come up for {name}: it is one for all, and nobody else in the hub holds it — so a "
                     "process outside the hub holds it (Claude in the owner's terminal, a second hub). To find it: ps -Ao "
                     "pid,ppid,command | grep '{cmd}'. Stopping someone else's process — only on the owner's word; once it "
                     "is free — hub session reload {name}.")
# who did it, as a word inside a phrase (the owner reads it in his language, and Russian needs the case of the place)
WHO_OWNER = T("core.msg.who_owner", "the owner")              # "cancelled (the owner)"
WHO_OWNER_OF = T("core.msg.who_owner_of", "the owner")        # "an executor from the owner"
WHO_OWNER_HEAD = T("core.msg.who_owner_head", "The owner")    # at the start of a sentence: "The owner connected …"
WHO_HUB_HEAD = T("core.msg.who_hub_head", "The hub")
RUN_QUEUED = T("core.msg.run_queued",
               "Executor {name} from {by} is in the queue, no. {n}: {wait}. Task: {task}. To raise it now — hub task go "
               "{name} (above the budget threshold — with --for <mN|qN> of the owner's word); to cancel — hub task cancel "
               "{name}; the whole queue — hub task queue.")
NO_OPEN_QUESTION = T("core.reducer.no_open_question", "no such open question")
REASON_UNKNOWN = T("core.msg.reason_unknown", "the reason is unknown")
NO_HUBNET = T("core.reducer.no_hubnet", "there is no Hubnet network in the hub")   # net.* of old journals
CWD_MOVED = T("core.msg.cwd_moved",
              "The folder is changed to {cwd}, you are restarted with the same conversation. Continue the task; nothing "
              "to continue — answer with one line «{nothing}».", nothing=NOTHING)
# the watchdog's concession detector (watch.surrender): recheck on the merits and answer the owner again
SURRENDER_NOTE = T("core.msg.surrender",
                   "Hub watchdog: to the owner's doubt ({id}: \"{text}\") you answered with a concession without checking "
                   "the disputed point in this turn. Only new data changes a position: check the disputed point on the "
                   "merits (code, data, a source) and answer them again, with the first line in the owner's language: "
                   "\"Rechecked: <what I checked> → hold / change / partly\". This answer goes to the owner.")
# the answer to a form question while the owner is away (after _away_since): the model reads it, the turn goes on
ASK_DEFERRED = (". The question is saved, they will see it when they are back. Decide what is reversible yourself and "
                "record it: hub decided \"<what> · why · how to roll back\"; put the owner's decision off and go on with "
                "other work.")
# the refusal of a permission when the owner gave no reason (hosts/codex_rules.DENIED: the same text)
DENIED_BY_OWNER = "Denied by the owner"
# a restart cut background work: the tail of the continuation (after RELOAD_ASKED, CUT_TURN, LIMIT_CUT)
BG_CUT = T("core.msg.bg_cut", " The background work that was running is cut off: {bg} — start it again if it is still "
                              "needed.")
BG_CUT_TOO = T("core.msg.bg_cut_turn", " Background work was running and is cut off: {bg} — start it again if it is "
                                       "still needed.")
BG_CUT_LIMIT = T("core.msg.bg_cut_limit", " The background work that was running is cut off too: {bg} — start it again "
                                          "if it is still needed.")
# one session was restarted with the same conversation, cutting its background work: why
RESPAWN_CUT = T("core.msg.respawn_cut",
                "The hub restarted you with the same conversation ({why}) — background work is cut off: {bg}. Start "
                "again what is still needed; nothing is needed — answer with one line «{nothing}».", nothing=NOTHING)
CUT_WHY = {"codex": T("core.msg.cut_why_codex", "the Codex account changed"),
           "claude": T("core.msg.cut_why_claude", "the Claude account changed"),
           "set": T("core.msg.cut_why_set", "a new tool, model or folder"),
           "minihub_on": T("core.msg.cut_why_minihub_on", "mini-hub mode is on: edits in the folder are closed"),
           "minihub_off": T("core.msg.cut_why_minihub_off", "mini-hub mode is off: edits in the folder are open")}


def _held_outside(st, ev, sess, data):
    """A "one for all" tool (a personal Telegram) failed to come up and nobody else in the hub holds it, so a process
    outside the hub does (08.10: Claude in the owner's Terminal held the Telegram session for a day; hub sessions got
    CONNECTION_CLOSED). Tell the hub once, until it connects again: what is taken and how to find the holder."""
    told = st.setdefault("outside_told", [])
    failed = set(data.get("mcp_failed") or [])
    for tool in [t for t in told if t not in failed and t in (data.get("mcp") or [])]:
        told.remove(tool)   # it connected, so the next failure is news again
    for tool in sorted(failed):
        tid = mcpwatch.server_tool(st["catalog"], tool) or tool   # the session's server name may be the entry's `as`
        spec = st["catalog"].get(tid) or {}
        if not spec.get("exclusive") or tool in told:
            continue
        inside = [x["name"] for x in st["sessions"].values() if x is not sess and tid in (x.get("tools") or [])
                  and x["state"] in LIVE]
        if inside:
            continue
        told.append(tool)
        server = spec.get("server") or {}
        cmd = str((server.get("args") or [None])[-1] or os.path.basename(str(server.get("command") or tool)))
        _notify_hub(st, {**ev, "seq": f"{ev['seq']}o"},
                    MCP_HELD_OUTSIDE.format(tool=tool, name=sess["name"], cmd=cmd[:60]))


def _apply(st, ev, actions):
    kind = ev["type"]
    data = ev.get("data") or {}
    # a process fact under the old name of an already renamed session (05.10: Stop came before the result, a rename in
    # between, the result was lost and the task stayed "in progress") goes to whoever is under that name now
    name = ev.get("entity")
    if kind.startswith(("turn.", "session.", "dialog.", "permission.", "host.")) and kind not in ("session.requested",
            "session.renamed") and name and name not in st["sessions"] and f"was:{name}" in st["aliases"] \
            and st["aliases"][f"was:{name}"] in st["sessions"]:
        ev = {**ev, "entity": st["aliases"][f"was:{name}"]}
    why = busstate.liaison_refusal(st, kind, data)   # the liaison: only its bus commands (core/liaison.py)
    if why:
        raise Rejected(why)
    ctx = {"new_message": new_message, "notify": lambda e, text, **kw: _notify_hub(st, e, text, **kw),
           "sleep": lambda name, e: _auto_sleep(st, name, e, actions)}
    if kind in ("turn.tool", "turn.tool_result", "turn.result", "session.said"):
        mcpwatch.touched(st, ev, data)   # did the session touch a server that failed to come up (after the fix: "retry")
    if kind == "session.identified":
        if ev.get("entity") not in st["sessions"]:
            raise Rejected("unknown session")
        identity.remember(st, ev["entity"], data)
        for field in ("pid", "pane", "socket"):
            if data.get(field) is not None:
                st["sessions"][ev["entity"]][field] = data[field]
        return
    if kind == "owner.settings":
        st["owner"]["tz"] = data.get("timezone") or "UTC"
        if "quiet_review" in data:   # non-working hours: cards pile up until 10:00 of a working day (core/owner.py, review_*)
            rv = owner.review_of(data["quiet_review"])
            if rv:
                st["owner"]["review"] = rv
            else:
                st["owner"].pop("review", None)
                owner.review_release(st, ev["ts"])
        # compaction on wake (owner decision 2026-10-11), the shared executor ceiling and parallelism (15.10) come from
        # hub.json; minihub_default in old journals affects nothing (core/minihub.py)
        before = _modes(st)
        for k in ("compact_wake", "compact_min", "executors"):
            if k in data:
                st["owner"][k] = data[k]
        if "parallel" in data:   # the first such event is the boundary of the new rules (core/minihub.py)
            st["owner"]["parallel"] = minihub.parallel_of(data["parallel"])
        if "drop_tools" in data:   # tools an agent's executors do not inherit (hub.json executors.agents.<agent>.drop_tools)
            st["owner"]["drop_tools"] = executors.clean_drops(data["drop_tools"])
        if "mcp_confirm" in data:   # MCP servers a sign-in to which waits for the owner's confirmation (core/mcpwatch)
            st["owner"]["mcp_confirm"] = mcpwatch.confirm_list(data["mcp_confirm"])
        _modes_follow(st, ev, before)
        if "budget_gate" in data and (data["budget_gate"] is not False) != budget.gate(st):
            _budget_gate(st, ev, data["budget_gate"] is not False)
        return
    if kind in ("owner.away", "owner.back") or (kind == "timer.fired" and ev.get("entity") == "owner"):
        return _owner_event(st, ev, data, "_away_expired" if kind == "timer.fired" else kind)
    if kind == "timer.fired" and str(ev.get("entity", "")).startswith("q:"):
        owner.remind(st, ev)
        return
    if kind == "timer.fired" and str(ev.get("entity", "")).startswith(schedule.TIMER):   # a job's term is near
        return schedule.warn(st, ev, {"new_message": new_message, "hub": _hub})
    if kind == "task.run_requested":
        # an executor for a task (spec "tasks, specialists, executors", release 2): checks are here, the daemon makes the
        # copy and the launch (core/executors.events_for). No room or budget is not a refusal but a queue: the request
        # is not lost, the hub sees it and can launch at once or cancel (owner decision 2026-10-04: queue in the core,
        # hub sees it)
        name, owner_word = ev.get("entity") or "", tools.from_owner(data)
        raiser = None if owner_word else data.get("sender")
        me = st["sessions"].get(raiser or "") or {}
        up = st["sessions"].get(me.get("raised_by") or "") or {}
        if minihub.runner(me) and up.get("role") == "agent":
            # a specialist's executor does not raise executors (owner decision 2026-10-05: this must be forbidden)
            raise Rejected(T("core.reducer.run_by_executor",
                             "you are an executor of {name}: you may not raise executors — do it yourself or tell "
                             "{name}: hub send {name} \"<what is needed>\"").format(name=up["name"]))
        tool_word = owner_word
        if data.get("for") and not owner_word:
            # the hub, on the owner's word (--for with a message or a form answer): above the limit threshold too
            # (04.10); a session for its own executor, on the owner's word to it, but not for risky --tools: the owner or
            # the hub decides those (friction digest 16.10)
            owner_word = _owner_word_for(st, data, ev, own=True)
            tool_word = me.get("kind") == "hub"
        if len(name) > NAME_MAX and NAME_CHARS.fullmatch(name):   # the "Latin, digits" refusal was repeated in
            raise Rejected(coretext.join(T("core.reducer.run_name_long",   # a loop for a long name
                                           "an executor's name is at most {max} characters, and this one has {n}: "
                                           "shorten the gist").format(max=NAME_MAX, n=len(name)),
                                         T("core.reducer.run_name_cut", "; e.g. {cut}")   # 13.11: and a fitting one
                                         .format(cut=executors.free_name(st, name[:NAME_MAX].rstrip("-"))
                                                 if executors.taken(st, name[:NAME_MAX].rstrip("-"))
                                                 else name[:NAME_MAX].rstrip("-"))))
        if not NAME.fullmatch(name) or not str(data.get("task") or "").strip():
            raise Rejected(T("core.reducer.run_name_task",
                             "an executor's name (Latin letters, digits, hyphen) and the task text are needed"))
        # --branch: on an existing branch; --here: right in a ready copy of --repo (mini-hubs, slice 3; the daemon
        # checks the copy and branch on disk, here only what is visible without the disk)
        here, branch = data.get("here") is True, data.get("branch")
        if branch is not None and (not isinstance(branch, str) or not branch.strip() or branch.startswith("-")):
            raise Rejected(T("core.reducer.run_branch", "--branch <the name of an existing branch>"))
        if here and not data.get("repo"):
            raise Rejected(T("core.reducer.run_here_repo",
                             "--here is work right in a ready copy: name it with --repo <the root of the copy>"))
        if here and branch:
            raise Rejected(T("core.reducer.run_here_branch",
                             "--here and --branch together are not allowed: a ready copy is already on its own branch — "
                             "keep one"))
        from_main = data.get("from_main") is True
        if from_main and (here or branch):
            raise Rejected(T("core.reducer.run_from_main",
                             "--from-main is a new branch off the main one: not together with --here and --branch (a "
                             "ready branch)"))
        busy = here and executors.here_refused(st, data["repo"], raiser, name)
        if busy:
            raise Rejected(busy)
        spec = _spec_flag(st, data, raiser)   # --spec [<folder>] (core/specmode.py): True, a folder name or None
        why = executors.check(st, name, raiser)   # also parallelism 0 or "auto" (owner decision 2026-10-15: with
        if why:                                   # parallelism 0 there are no executors at all)
            raise Rejected(why)
        # the direction board card the work is for (core/board.py): only a direction session gives it, its own board
        link = data.get("board")
        if link is not None:
            if not isinstance(link, dict) or not board.card_id(link.get("card")) or link.get("slug") != board.lead_slug(st, raiser):
                raise Rejected(T("core.reducer.run_board",
                                 "--board TASK-N is given by a direction session — with a card of its own board"))
            link = {"slug": link["slug"], "card": board.card_id(link["card"]),
                    "how": "brief" if link.get("how") == "brief" else "flag"}
        # tools at start (--tools): granted seconds later, an executor that had already begun its turn did not see them
        # until the end of the task (05.10). Rules as for hub tools grant: from the catalog; a risky one needs the
        # owner's word, otherwise the executor starts without it and the owner gets a card
        unknown = [t for t in data.get("tools") or [] if t not in st["catalog"]]
        if unknown:
            raise Rejected(T("core.reducer.run_not_in_catalog", "not in the catalog: {tools} (hub tools list)")
                           .format(tools=", ".join(unknown)))
        pin = delegations.pinned(data, catalog.codex_state(st))
        # base tools (every session has them) are already there: do not grant or ask the owner (07.10: Jira three
        # times in a night)
        data = {**data, "tools": [t for t in data.get("tools") or [] if not st["catalog"][t].get("base")]}
        risky = [t for t in data["tools"] if st["catalog"][t].get("risk") and not tool_word]
        # limit economy (mini-hubs only, core/budget.py): at once for a task on the owner's live word or an urgent one;
        # --work normal is raised on the economy pair, which hub task run computed from the settings
        eco = delegations.pinned(data["economy"], catalog.codex_state(st)) if isinstance(data.get("economy"), dict) else {}
        mode = budget.mode(st)
        live = bool(mode and (owner_word or _live(st, raiser)))
        wait = executors.waits(st, raiser, owner_word, now=ev["ts"], live=live, asked=True)
        run = st.setdefault("runs", {})[name] = {"by": raiser, "task": str(data["task"]).strip(), "repo": data.get("repo"),
                                                 "ts": ev["ts"], "owner_ok": owner_word,
                                                 "state": "queued" if wait else "ready", **forms.why_fields(wait),
                                                 # whose chain asked: the executor's result goes there (core/chain.py)
                                                 "root": chain.root(st, data.get("for")) or chain.turn_root(st, raiser),
                                                 # which task asked: its task is a subtask of that one, even if that
                                                 # turn ends before the daemon raises the executor
                                                 "parent": chain.turn_task(st, raiser),
                                                 # raised by a mini-hub: its task waits for this executor even if the
                                                 # mode is switched off later (core/minihub.waiting_on)
                                                 **({"minihub": True} if minihub.on(st, me) else {}),
                                                 **({"here": True} if here else {}),
                                                 **({"branch": branch.strip()} if branch else {}),
                                                 **({"from_main": True} if from_main else {}),
                                                 **({"spec": spec} if spec else {}),
                                                 **pin,
                                                 **({"work": data["work"]} if data.get("work") in ("normal", "risky")
                                                    else {}),
                                                 **({"economy": eco} if eco else {}),
                                                 **({"live": True} if live else {}),
                                                 # asked in a turn started by a schedule firing: its task is not the
                                                 # owner's live word either (core/chain.scheduled)
                                                 **({"via_schedule": True} if chain.turn_scheduled(st, raiser)
                                                    and not data.get("for") else {}),
                                                 **({"tools": list(data["tools"]), "ask": risky} if data.get("tools")
                                                    else {}),
                                                 **({"board": link} if link else {})}
        if not wait:
            budget.economize(st, run)
        held = executors.budget_waits(st, owner_word, ev["ts"], live)
        if wait and (minihub.on(st, me) and not held or held and mode):
            pass   # a mini-hub raises many: room frees up by itself, nothing for the hub to decide (the queue number is
            #        in the hub task run output); those waiting on limit economy go in a card to the owner
            #        (budget.mode), not one by one
        elif wait:
            _notify_hub(st, ev, RUN_QUEUED.format(name=name, by=raiser or WHO_OWNER_OF, n=executors.position(st, name),
                                                  wait=wait, task=run["task"][:200]))
        return
    if kind == "task.auto_run":
        # the core's former auto-raise for a busy specialist (04.10-05.10), for old journals only; now the hub decides
        # (hub route, _route)
        if data.get("channel") != "core":
            raise Rejected(T("core.reducer.auto_run_core", "the core raises an executor for a specialist"))
        name, agent, mid = ev.get("entity") or "", data.get("agent"), data.get("message")
        msg = st["messages"].get(mid or "")
        if not NAME.fullmatch(name) or executors.check(st, name, agent) or not msg \
                or not executors._takeable(st, msg, agent):
            raise Rejected(T("core.reducer.auto_run_nothing", "nothing to give an executor: the message is not in the "
                                                              "specialist's queue or the name is taken"))
        msg["handover"] = name   # no longer deliver to the specialist: the executor takes it
        st.setdefault("runs", {})[name] = {"by": agent, "task": msg["text"], "message": mid, "repo": None,
                                           "ts": ev["ts"], "state": "ready", "why": None, "auto": True}
        return
    if kind in ("task.queue_paused", "task.queue_resumed"):
        # an explicit pause of the executor queue (only the limit threshold held it: the limit dropped and the queue
        # raised everyone by itself, against the owner's "launch nothing"): the hub or the owner sets and lifts it. A core
        # module with the queue.pause right (checked by the daemon before the fact is written) sets its own pause and
        # lifts only that one; the queue stands while anyone's pause stands, and the hub's or owner's resume lifts all of them
        sender = data.get("sender") or ""
        who = st["sessions"].get(sender) or {}
        mod = sender if sender.startswith("mod:") and not tools.from_owner(data) else None
        if not (mod or tools.from_owner(data) or who.get("kind") == "hub"):
            raise Rejected(T("core.reducer.queue_pause_who", "the hub or the owner pauses the queue"))
        # who -> {by, why, ts}; "hub" is the hub's or the owner's. A state from before this: its one pause is the hub's
        pauses = st.get("queue_pauses")
        if pauses is None:
            pauses = st["queue_pauses"] = {"hub": dict(st["queue_paused"])} if st.get("queue_paused") else {}
        mine = mod or "hub"
        if kind == "task.queue_paused":
            # by: None is the owner (the display fills in the name in the page language)
            pauses[mine] = {"by": mod or (None if tools.from_owner(data) else sender),
                            "why": str(data.get("why") or "")[:200], "ts": ev["ts"]}
        elif not mod:
            pauses.clear()
        elif mine in pauses:
            del pauses[mine]
        elif pauses:
            raise Rejected(T("core.reducer.queue_pause_not_yours",
                             "the queue is paused by {who}: only they lift it (or the owner: hub task resume)").format(
                who=", ".join(sorted(p["by"] or "owner" for p in pauses.values()))))
        # the one every reader sees: the hub's or the owner's pause first, else the oldest module's
        shown = pauses.get("hub") or min(pauses.values(), key=lambda p: p["ts"], default=None)
        if shown:
            st["queue_paused"] = dict(shown)
        else:
            st.pop("queue_paused", None)
        if not pauses:
            st.pop("queue_pauses", None)
        executors.recount(st, ev["ts"])   # waiting runs get a new reason (and its code: the old one is stale)
        return
    if kind == "task.run_held":
        # a module's admission (the task.admit point, core/daemon.admit_pass): {module, reason} keeps the request in the
        # queue with the module's reason as its why, {release: true} lifts the hold (the request waits for room as any
        # other, or rises). The daemon writes it only on a change; a request gone meanwhile is nothing to hold
        run = (st.get("runs") or {}).get(ev.get("entity") or "")
        if not run or run.get("wake"):
            return
        if data.get("release") is True:
            if run.pop("held", None):
                executors.recount(st, ev["ts"])   # the request waits for room as any other: it gets its reason anew
            return
        reason = " ".join(str(data.get("reason") or "").split())[:200]
        if not reason:
            return
        module = str(data.get("module") or "")[:64]
        run.pop("why_code", None)
        run.pop("why_l10n", None)
        run.update(state="queued", held={"module": module, "reason": reason, "ts": ev["ts"]},
                   **forms.why_fields(executors.HELD.format(module=module, reason=reason)))
        return
    if kind in ("task.run_ready", "task.run_go", "task.run_cancelled"):
        run = (st.get("runs") or {}).get(ev.get("entity") or "")
        if not run or run.get("state") != "queued" and kind != "task.run_cancelled":
            up = st["sessions"].get(ev.get("entity") or "") or {}
            if not run and kind == "task.run_cancelled" and minihub.runner(up) and up["state"] in LIVE:
                # already raised, nothing to remove from the queue; it is stopped instead (friction digest 16.10)
                raise Rejected(T("core.reducer.run_cancel_live",
                                 "{name} already works, it is not in the queue — to stop it: hub session stop {name}, "
                                 "to take it off the list — then hub session remove {name}").format(name=up["name"]))
            raise Rejected(T("core.reducer.run_not_queued", "no such request in the queue"))
        name, who = ev["entity"], st["sessions"].get(data.get("sender") or "") or {}
        hub = who.get("kind") == "hub"
        if kind == "task.run_cancelled":
            # the requester names itself: the owner's own request has by None, and a fact that names nobody is not him
            mine = bool(data.get("sender")) and data["sender"] == run.get("by")
            if not (tools.from_owner(data) or hub or mine):
                raise Rejected(T("core.reducer.run_cancel_who", "the one who asked, the hub or the owner cancels"))
            del st["runs"][name]
            took = st["messages"].get(run.get("message") or "")
            if took and took.get("handover") == name:   # the task is back with the specialist (07.10: one hung for 3 h)
                took.pop("handover")
            by = run.get("by")
            if by and by != data.get("sender") and by in st["sessions"]:
                new_message(st, f"m{ev['seq']}", by, T("core.msg.run_cancelled",
                                                       "The executor request {name} is cancelled ({who}): {task}").format(
                                name=name, who=WHO_OWNER if tools.from_owner(data) else data.get("sender"),
                                task=run["task"][:200]), ev["ts"], origin=forms.ORIGIN_CORE, **_for_parent(st, run))
            return
        if kind == "task.run_go":   # raise out of turn: the owner or the hub; above the budget threshold it takes the owner's word
            if not (tools.from_owner(data) or hub):
                raise Rejected(T("core.reducer.run_go_who", "the hub or the owner raises out of turn"))
            if tools.from_owner(data) or data.get("for"):
                # and his word to the executor's lead: a mini-hub passed "raise it" to the hub (20.10)
                run["owner_ok"] = tools.from_owner(data) or _owner_word_for(st, data, ev, lead=run.get("by"))
            if data.get("queue"):
                # the economy card's "raise the queue" button (hub task go --queue): the owner's word for the request,
                # and room by queue and ceilings; with no room it is raised by itself once room frees up
                if not run.get("owner_ok"):
                    raise Rejected(T("core.reducer.run_go_queue",
                                     "the queue from the economy card is raised on the owner's word: --for <qN|mN>"))
            else:
                run["force"] = True
        wait = executors.waits(st, run.get("by"), run.get("owner_ok"), run.get("force"), now=ev["ts"],
                               live=run.get("live"))
        if wait and data.get("queue"):
            run.pop("why_code", None)
            run.pop("why_l10n", None)
            run.update(forms.why_fields(wait))
            return
        if wait:
            raise Rejected(T("core.reducer.run_still_waits", "{name} still waits: {wait}").format(name=name, wait=wait))
        if run.get("wake"):
            return _wake_ready(st, ev, name, actions)
        run.update(state="ready", why=None)
        run.pop("held", None)   # raised out of turn: the module is asked again before the launch
        run.pop("why_code", None)
        run.pop("why_l10n", None)
        budget.economize(st, run)
        return
    if kind == "task.adopted":
        # migration (spec section 8): a live worker becomes an executor (raised by the hub); with a backlog it does not leave by itself
        if not tools.from_owner(data):
            raise Rejected(T("core.reducer.adopt_who", "the owner moves it (hub migrate)"))
        sess = st["sessions"].get(ev.get("entity") or "")
        if not sess or sess["state"] in ("removed", "closed", "failed") or sess.get("role", "worker") != "worker":
            raise Rejected(T("core.reducer.adopt_live", "only live workers are moved"))
        before = _modes(st)
        sess.update(role="executor", raised_by=data.get("raised_by") or _hub(st), adopted_at=ev["ts"])
        if data.get("backlog"):
            sess["backlog"] = data["backlog"]
        _modes_follow(st, ev, before)
        return
    if kind == "question.dismissed":
        # the owner closed a question card without answering (Esc, x; 04.10): the question is removed from the page,
        # Telegram and reminders; the session gets a note that no answer will come, so it does not wait or keep the
        # assignment in "waits for the owner"
        if not tools.from_owner(data):
            raise Rejected(T("core.reducer.dismiss_who", "the owner closes a question"))
        sess = st["sessions"].get(ev.get("entity") or "")
        ask = next((a for a in (sess or {}).get("asks") or [] if a["request_id"] == data.get("request_id")), None)
        if not ask:
            raise Rejected(NO_OPEN_QUESTION)
        if busstate.card_answered(st, ev, data, sess, actions, "dismissed"):
            return   # a card of the bus closed without an answer: not a yes — nothing goes out
        sess["asks"].remove(ask)
        _answered(sess, ev, data, "dismissed", ask=ask)
        st["owner"]["reminders"].pop(f"q:{sess['name']}:{ask['request_id']}", None)
        delegations.trail(st, ask.get("delegation"), delegations.BY_OWNER,
                          T("core.msg.trail_dismissed", "closed question {id} without an answer").format(id=ask["id"]))
        new_message(st, f"m{ev['seq']}", sess["name"],
                    T("core.msg.question_dismissed",
                      "The owner closed your question {id} (\"{question}\") without answering: no answer will come, do "
                      "not wait for it. If you cannot go on without the owner's decision — say so in the result; if you "
                      "can — go on your own way.").format(id=ask["id"], question=owner.question_head(ask)),
                    ev["ts"], origin=forms.ORIGIN_OWNER, from_owner=True, answers_question=ask["id"],
                    channel=data.get("channel"), card_dismissed=True, on_behalf="owner",
                    **({"for_delegation": ask["delegation"]} if ask.get("delegation") else {}))
        return
    if kind == "question.shown":
        # the hub shows a session's question in its own chat as the same card instead of asking it anew (12.10: the
        # owner had two cards about one thing and closed the extra one himself): one card, an answer from any chat goes
        # to the session, and the card disappears everywhere
        sess = st["sessions"].get(ev.get("entity") or "")
        ask = next((a for a in owner.open_asks(sess or {}, ev["ts"]) if a["id"] == data.get("id")), None)
        if not ask:
            raise Rejected(NO_OPEN_QUESTION)
        hub = data.get("sender")   # the hub that showed it (there may be two), not just any
        if (st["sessions"].get(hub or "") or {}).get("kind") != "hub" or hub == sess["name"]:
            raise Rejected(T("core.reducer.shown_who", "the hub shows a session's question in its own chat"))
        ask["shown"] = hub
        return
    if kind == "ask.expired":
        return _ask_expired(st, ev, data)
    if kind == "owner.invited":
        return _owner_invited(st, ev, data)
    if kind == "owner.came":
        return _owner_came(st, ev, data)
    if kind == "question.withdrawn":
        # a session withdraws its own question to the owner, the hub any (07.10: two identical questions from two
        # sessions, no way to remove the duplicate): the card leaves the page, Telegram and reminders
        sess = st["sessions"].get(ev.get("entity") or "")
        ask = next((a for a in (sess or {}).get("asks") or [] if a["id"] == data.get("id")), None)
        if not ask:
            raise Rejected(NO_OPEN_QUESTION)
        who = data.get("sender")
        is_hub = (st["sessions"].get(who or "") or {}).get("kind") == "hub"
        if not (tools.from_owner(data) or who == sess["name"] or is_hub):
            raise Rejected(T("core.reducer.withdraw_who",
                             "the session that asked, the hub or the owner withdraws a question"))
        if busstate.card_answered(st, ev, data, sess, actions):
            return   # a card of the bus withdrawn: nothing goes out
        why = str(data.get("why") or "").strip()
        _drop_ask(st, sess, ask, who or delegations.BY_OWNER, T("core.msg.trail_withdrawn", "withdrew question {id}").format(
            id=ask["id"]) + (f": {why}" if why else ""))
        _answered(sess, ev, data, ask=ask, why=why)
        if who != sess["name"]:
            question = owner.question_head(ask)
            new_message(st, f"m{ev['seq']}", sess["name"], T("core.msg.question_withdrawn_why",
                        "Your question to the owner {id} (\"{question}\") is withdrawn: {why}. No answer to it will come — "
                        "do not wait.").format(id=ask["id"], question=question, why=why) if why else
                        T("core.msg.question_withdrawn",
                          "Your question to the owner {id} (\"{question}\") is withdrawn. No answer to it will come — do "
                          "not wait.").format(id=ask["id"], question=question), ev["ts"],
                        origin=forms.ORIGIN_HUB if is_hub else forms.ORIGIN_OWNER,
                        **({} if is_hub else {"on_behalf": "owner"}))
        return
    if kind == "task.noted":
        # a task record grew (core/taskrec.py): the journal gets who and where; the counter is for the page
        d = st["delegations"].get(ev.get("entity") or "")
        if not d:
            raise Rejected(T("core.reducer.no_task", "no such task"))
        d["notes"] = d.get("notes", 0) + 1
        return
    if kind == "task.backlog":
        # a session started a backlog (hub direction new): it is an executor with a backlog and does not leave by itself
        # (spec section 2). When the hub starts a direction for another session, the backlog is that session's, by
        # the direction name (if the session does not exist yet, it waits for it), not the hub's (04.10: the backlog
        # went to the hub, the direction became a "worker" and went to sleep when the task closed)
        # The hub or the owner gives a direction to a session with a different name via data.session (08.10: "make a
        # direction" about the executor fixer: the backlog waited for a session named like the direction, which did
        # not exist, and "Directions" stayed empty)
        sess, slug = st["sessions"].get(data.get("sender") or ""), str(data.get("slug") or "")
        if not slug or not (sess or tools.from_owner(data)):
            raise Rejected(T("core.reducer.backlog_who", "a session starts a backlog: hub direction new <slug>"))
        boss = tools.from_owner(data) or sess.get("kind") == "hub"
        name = str(data.get("session") or (slug if boss else sess["name"]))
        if not boss and name != sess["name"]:
            raise Rejected(T("core.reducer.backlog_other", "the hub or the owner gives a direction to another session"))
        if not boss and sess.get("backlog") and sess["backlog"] != slug:   # a second direction would take the first one's lead
            raise Rejected(T("core.reducer.backlog_second",
                             "you already lead the direction {lead}; a new one with its own lead is started by the hub "
                             "— ask it to run: hub direction new {slug} --session <name>").format(lead=sess["backlog"], slug=slug))
        if sess and sess.get("kind") == "hub":
            sess.pop("backlog", None)
        pending = st.get("pending_backlogs") or {}
        for key in [k for k, v in pending.items() if (v.get("slug") if isinstance(v, dict) else k) == slug]:
            del pending[key]   # the direction was waiting for another name; now it is clear whose it is
        target = st["sessions"].get(name)
        if target and target["state"] != "removed":
            before = _modes(st)
            target["backlog"] = slug
            _unretire(target)
            _modes_follow(st, ev, before)   # became a direction: a mini-hub after the current task (at 0 and "auto", by itself)
        else:   # the session does not exist yet; the backlog waits for it (session.requested)
            st.setdefault("pending_backlogs", {})[name] = {"slug": slug, "ts": ev["ts"]}
        return
    if kind == "task.backlog_dropped":
        # the hub or the owner takes a lead off a direction (hub direction lead <slug> --drop <session>): the session keeps
        # its work, only the backlog goes, and with it the direction's summary after compaction and the board's "lead". The
        # last lead may be dropped: the direction is then like one nobody has taken yet. 06.11: an agent ran `lead <slug>`
        # without a name and became a second lead next to the one the hub named; nothing could take it off
        from core import i18n
        lang = i18n.state_lang(st)
        if guards.from_owner_or_hub(st, data):   # the same hands as the hub's assignment (task.backlog)
            raise Rejected(i18n.tr(lang, "core.direction.drop_who"))
        slug, name = str(data.get("slug") or ""), str(data.get("session") or "")
        # a session keeps the name under which it took the direction: after `hub direction rename` the CLI sends the former ones too
        also = data.get("slugs") if isinstance(data.get("slugs"), list) else []
        slugs = {slug, *(x for x in also if isinstance(x, str))} - {""}
        target, pending = st["sessions"].get(name), st.get("pending_backlogs") or {}
        waiting = pending.get(name)
        if target and target["state"] != "removed" and target.get("backlog") in slugs:
            before = _modes(st)
            target.pop("backlog")
            _modes_follow(st, ev, before)
        elif waiting is not None and (waiting.get("slug") if isinstance(waiting, dict) else name) in slugs:
            del pending[name]   # the direction was waiting for a session that is not there yet
        else:
            raise Rejected(i18n.tr(lang, "core.direction.drop_not_lead", name=name, slug=slug))
        return
    if kind == "direction.card":
        # an edit of a direction board card (hub direction <slug> task create|edit|archive, core/board.py): who and when
        why = board.card_edited(st, ev, data, tools.from_owner(data))
        if why:
            raise Rejected(why)
        return
    if kind == "task.backlog_closed":
        # the direction is closed (hub direction archive); its executor leaves like all others, after the report
        # a session keeps the name under which it took the direction: after a rename the CLI sends the former ones too
        also = data.get("slugs") if isinstance(data.get("slugs"), list) else []
        names = {data.get("slug"), *(x for x in also if isinstance(x, str))} - {None, ""}
        before = _modes(st)
        for s in st["sessions"].values():
            if s.get("backlog") in names:
                s.pop("backlog", None)
        _modes_follow(st, ev, before)
        return
    if kind == "task.run_failed":
        run = (st.get("runs") or {}).pop(ev.get("entity") or "", None)
        took = st["messages"].get((run or {}).get("message") or "")
        if took and took.get("handover") == ev.get("entity"):   # the copy failed; the task is back in the specialist's queue
            took.pop("handover")
            _notify_hub(st, ev, T("core.msg.auto_run_failed",
                                  "Could not raise executor {name} for the busy {agent}: {reason}. Task {task} is in its "
                                  "queue again — it gets it after its turn.").format(
                name=ev.get("entity"), agent=run["by"], reason=coretext.got(data, "reason") or REASON_UNKNOWN,
                task=took.get("delegation") or took["id"]))
            return
        if run and run.get("by") in st["sessions"]:
            new_message(st, f"m{ev['seq']}", run["by"], T("core.msg.run_failed",
                                                          "Could not raise executor {name}: {reason}. Do it yourself or "
                                                          "try again.").format(
                name=ev.get("entity"), reason=coretext.got(data, "reason") or REASON_UNKNOWN), ev["ts"],
                origin=forms.ORIGIN_CORE, **_for_parent(st, run))
        return
    if kind == "owner.decided_revoked":
        return _decision_revoked(st, ev, data, actions)
    if kind == "owner.decided":
        st["owner"]["decided"].append({"session": data.get("sender"), "text": data.get("text"), "ts": ev["ts"]})
        decisions = st.setdefault("decisions", [])   # the last ones, for `hub decided list` and a withdrawal
        decisions.append({"seq": ev["seq"], "by": data.get("sender"), "text": data.get("text"), "ts": ev["ts"]})
        del decisions[:-DECISIONS_KEPT]
        # a decision spoken to an executor goes to whoever raised it, to record in its own CLAUDE.md (spec section 4)
        by = st["sessions"].get(data.get("sender") or "") or {}
        slug = board.direction_of(st, by) if data.get("channel") == "session" else None
        if slug and str(data.get("text") or "").strip():
            # the owner's word during direction work also goes to decisions/ of its backlog (spec dir-sync K3): after
            # compaction `hub direction resume` shows it; the daemon writes the file, a repeat of the same event
            # makes no second file
            decisions[-1]["slug"] = slug   # a withdrawal takes the file out of the backlog again
            actions.append({"do": "board_decision", "session": None, "slug": slug, "seq": ev["seq"], "ts": ev["ts"],
                            "by": by["name"], "text": str(data["text"]).strip()})
        up = st["sessions"].get(by.get("raised_by") or "") if minihub.runner(by) else None   # a direction is not an executor
        # not to the hub (it has no profession CLAUDE.md of its own) and not to one who heard the owner's word
        # himself, via his card or a message: recording it is the job of the same turn (owner decision 2026-10-16:
        # 14 of 16 wake-ups were wasted)
        if up and up["state"] not in ("removed", "closed", "failed") and up.get("kind") != "hub" \
                and not _heard_it(st, up["name"], data.get("text")):
            # the core has already put it in the direction's backlog decisions/ (board_decision above); no second record needed
            filed = T("core.msg.decision_filed", " The core puts it into the decisions of the direction's backlog itself "
                                                 "— do not duplicate it with a record of your own.") if slug else ""
            new_message(st, f"m{ev['seq']}", up["name"], T("core.msg.decision_relayed",
                        "An owner's decision (recorded by {by}): {text}\nIf it is about your area — add it to your "
                        "CLAUDE.md, section \"Owner's decisions\".").format(by=by["name"], text=data.get("text")) + filed,
                        ev["ts"], origin=forms.ORIGIN_CORE)
            decisions[-1]["told"] = up["name"]   # a withdrawal tells it too
        return
    if kind == "timer.fired" and str(ev.get("entity", "")).startswith("d"):
        return delegations.timeout(st, ev, ctx)
    if kind == "timer.fired" and str(ev.get("entity", "")).startswith("h:"):
        return _handoff_timer(st, ev, ctx)
    if kind == "timer.fired" and str(ev.get("entity", "")).startswith(NET_TIMER):
        return _net_resume(st, ev)
    if kind == "timer.fired":
        return _timer(st, ev, data, actions)
    if kind == "delegation.created":
        _may_rush(st, data)
        delegations.created(st, ev, data, ctx)
        d = st["delegations"][f"d{ev['seq']}"]
        if specmode.is_open(d):   # spec mode: the hook's projection before its first turn (core/specmode.py)
            actions.append({"do": "spec_mode", "session": d["session"], **specmode.projection(d)})
        if d.get("board"):   # the card also goes to the executor's record, held until the session ends (core/board.py)
            st["sessions"][d["session"]]["board"] = dict(d["board"])
        _born(st, st["sessions"][d["session"]], ev["seq"], mark=False)   # a mini-hub's task is in its own words from birth
        if data.get("now") and d["message"] in st["messages"]:
            _rush(st, st["messages"][d["message"]], ev["ts"], actions)
        # any session can set `--for mN`: his words become the answer to a form only if set by someone entitled to
        # answer it himself (the owner, the hub, whoever raised the executor); one rule, guards.answers_permission
        if guards.answers_permission(st, {**data, "_raised_by": st["sessions"][d["session"]].get("raised_by")}) is None:
            return _delegated_words_answer_form(st, ev, d, actions)
        return
    if kind == "delegation.done":
        return delegations.done(st, ev, ctx)
    if kind in ("delegation.spec_accepted", "delegation.spec_dropped"):
        delegations.spec_close(st, ev, ctx)
        d = st["delegations"][ev["entity"]]
        # the hook's projection (core/specmode.py): edits open at once; a dropped spec is put aside (spec.dropped.md)
        pending = data.get("pending") if specmode.pending_ok(d["spec"]["path"], data.get("pending")) else None
        actions.append({"do": "spec_mode", "session": d["session"], **specmode.projection(d),
                        **({"dropped": True} if kind == "delegation.spec_dropped" else {}),
                        **({"accepted_from": pending} if kind == "delegation.spec_accepted" and pending else {})})
        return
    if kind == "owner.dismissed":  # the owner dismissed the watchdog's "answer?" guess with a click (01.10)
        if not tools.from_owner(data):
            raise Rejected(T("core.reducer.dismissed_who", "the owner dismisses it"))
        if ev.get("entity") not in st["sessions"]:
            raise Rejected("unknown session")
        st["sessions"][ev["entity"]].pop("attention", None)
        return
    if kind == "owner.seen":
        # the owner looked at the session on the page: everything up to seq is read (30.09: he could not tell what is new)
        if not tools.from_owner(data):
            raise Rejected(T("core.reducer.seen_who", "\"read\" is the owner's word"))
        hub = ev.get("entity") if isinstance(ev.get("entity"), str) and ev["entity"].startswith(":hub:") else None
        if hub:
            # or a linked hub's page: its words up to ts are read (busflow.list_view `seen`) — "Now" keeps a hub that
            # answered him until then, on every device, as a session's "Answered you"
            ts = data.get("ts")
            if hub[5:] not in ((st.get("bus") or {}).get("contacts") or {}):
                raise Rejected("unknown hub")
            # a word's time: not past the event by more than a clock skew (a mark from the future would hide every later
            # answer of that hub)
            if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not 0 <= ts <= ev["ts"] + 86400:
                raise Rejected("ts: the time of a word")
            seen = st["owner"].setdefault("seen", {})
            seen[hub] = max(seen.get(hub, 0), ts)
            return
        if ev.get("entity") not in st["sessions"]:
            raise Rejected("unknown session")
        seen = st["owner"].setdefault("seen", {})
        seen[ev["entity"]] = max(seen.get(ev["entity"], 0), int(data.get("seq") or 0))
        return
    if kind == "turn.result":
        _handoff_result(st, ev, data, actions, ctx)
        if isinstance(data.get("context"), int) and ev.get("entity") in st["sessions"]:
            # conversation length is the context of the last model call: the core compacts a long conversation by it (compact_idle)
            st["sessions"][ev["entity"]]["context"] = data["context"]
            if data.get("turns") and liaison.clear_due(st, st["sessions"][ev["entity"]]):   # a model turn, not a command's
                # the liaison needs no memory (its rules come with each message): a new conversation, typed by the core
                # as the owner would type /clear, before anything else reaches it (spec d2505378, part B, /clear)
                new_message(st, f"m{ev['seq']}c", liaison.NAME, "/clear", ev["ts"], origin=forms.ORIGIN_CORE,
                            command=True, builtin=True)
        # a built-in Claude Code command (/compact, /cost...) gives neither a hook nor a marked turn, only a result
        # with no steps; commands are typed into a free session, so this result is its (07.10: "/compact" from the
        # page failed after 4 attempts)
        for m in _messages_of(st, ev.get("entity")):
            if m["state"] == "typing" and m.get("command"):
                # in the feed, where it was entered, not where it ended: otherwise it lands under its own reply
                # (07.10: "1466 min")
                m["delivered_at"] = m["since"]
                _msg_move(m, "_delivered", ev["ts"])
                if data.get("chosen") and data.get("turns") == 0:
                    _codex_chosen(st, st["sessions"].get(ev.get("entity")), m, data["chosen"])
        blocked = hook_refused(data)
        hook_sess = st["sessions"].get(ev.get("entity"))
        if blocked:
            # a refusal is not an answer (hook text and the prompt echo): STOPPED for the assignment, not an "answer" for the owner (owner_out)
            data = {**data, "is_error": True}
        if blocked and hook_sess:
            # a session hook deliberately refused the prompt (exit 2 / decision:block): the model did not read it,
            # although the core hook (hooks run in parallel) has already recorded the marked turn; cancel the delivery
            # and tell the sender the reason. No restart or retry: the same hook would refuse again. The marked turn's
            # messages, if no Stop followed it (the session is mid-turn); if the core hook was too late, whatever is
            # being typed; a turn without a mark (terminal): what was typed waits for its own turn, leave it alone
            if hook_sess["state"] == "busy":
                # this prompt's, not the turn's: the model read what an earlier prompt of the turn carried (turn_also
                # keeps the whole turn)
                took = [st["messages"].get(mid or "")
                        for mid in (hook_sess.get("turn_marker"), *(hook_sess.get("prompt_also") or ()))]
            else:
                took = [m for m in _messages_of(st, hook_sess["name"]) if m["state"] == "typing"]
            # the reason is without the script path ("[/path/hook.sh]: ", exit 2) and without the prompt echo ("Original prompt: ...")
            why = re.sub(r"^\s*\[[^\]\n]*\]:\s*", "", re.split(r"\n\s*Original prompt:", blocked.group(1))[0]).strip()
            failed = set()
            for m in took:
                if m and m["session"] == hook_sess["name"] and m["state"] in ("typing", "delivered"):
                    _msg_move(m, "_blocked", ev["ts"])
                    failed.add(m["id"])
                    m.pop("delivered_at", None)
                    # the eyes badge on one taken by the turn: it will get no result-answer (_settle is silent at turns 0)
                    _badge(st, m["id"], None)
                    if m["id"] in (hook_sess.get("eyes") or []):
                        hook_sess["eyes"].remove(m["id"])
                    m["reason"] = "blocked by hook"
                    if why:
                        m["hook"] = why[:300]
            # the model did not read them: they are no part of what the turn took (_took, _terminal_note)
            if failed and hook_sess.get("turn_also"):
                left = [m for m in hook_sess["turn_also"] if m not in failed]
                if left:
                    hook_sess["turn_also"] = left
                else:
                    hook_sess.pop("turn_also")
        replied = {d["id"] for d in st["delegations"].values() if d["state"] == "replied"}
        cut_task = (st["sessions"].get(ev.get("entity")) or {}).get("delegation")
        # who else tells the owner this result: before the reply, it moves the task and the askers
        merged = (_merged_into(st, st["sessions"].get(ev.get("entity")), cut_task, data, ev["ts"])
                  if tools.answered(data) else None)
        delegations.on_result(st, ev, data, ctx)
        sess = st["sessions"].get(ev.get("entity"))
        if minihub.runner(sess) and not data.get("is_error") and (data.get("text") or "").strip():
            _keep_lessons(sess, data)
        if sess and not blocked and _auth_refused(sess, data) and not _moved_during_turn(st, sess, ev["ts"]):
            _refuse_login(st, sess, ev)
        if sess and _off_pool(sess, data):
            # not the account's refusal (_auth_refused): the next message raises the process again on the active one's
            # login (_account_stale); without a cut — with no token yet it would fail again in a loop of restarts
            sess["account"] = None
            sess.pop("account_gen", None)
        if sess and not blocked and _account_cut(sess, data) and not sess.get("limit_cut"):
            # a second result of the same turn (hubd writes a result after the next one starts too) comes from the
            # previous process: the account of the cut stays the first, otherwise a session raised on the new one will
            # not continue (cleared on turn.started)
            # the turn was cut by an account limit: the core (Claude accounts) restarts the session on another one and
            # it continues (_account); the continuation is of the same matter: an assignment (its result is the reply
            # to the owner) or an owner message (into his thread)
            # a Codex cut takes its reset from its own account's row only, not Claude's
            codex_cut = _codex_limit(st, ev, data, data.get("account", sess.get("account")))
            sess["limit_cut"] = {"ts": ev["ts"], "account": sess.get("account"), "gen": sess.get("account_gen"),
                                 **({} if codex_cut else {"resets": _cut_resets(st, ev["ts"])}),
                                 **_continues(st, _head(sess), cut_task), **codex_cut}
            if _moved_during_turn(st, sess, ev["ts"]):
                # accounts gateway: the account changed already during this turn, the refusal came from the old one; continue at once
                _continue_cut(st, ev, sess)
        if sess and not blocked and net_cut(data) and not sess.get("limit_cut") and not sess.get("net_cut"):
            # the network cut the turn (Claude Code retried for minutes and gave up; the owner on the road, sessions stood
            # until the owner wrote again): hubd fires the n: timer when the term is up and the API answers (core/netprobe.py),
            # the core says continue in the same matter (_net_resume). A second result of the same turn is not a new cut
            tries = sess.get("net_tries", 0)
            if tries < NET_TRIES:
                sess["net_tries"] = tries + 1
                sess["net_cut"] = {"ts": ev["ts"], "due": ev["ts"] + NET_WAIT[min(tries, len(NET_WAIT) - 1)],
                                   **_continues(st, _head(sess), cut_task),
                                   # the API refused the network: hubd asks a request it would refuse (netprobe.allowed)
                                   **({"route": True} if NO_ROUTE_TEXT.search(data.get("text") or "") else {})}
        elif sess and sess.get("net_cut") and net_cut(data) and NO_ROUTE_TEXT.search(data.get("text") or ""):
            sess["net_cut"]["route"] = True   # a later result of the same turn names the refused network: its probe, not the generic
        elif sess and not data.get("is_error") and data.get("turns") != 0:   # (a built-in command is not a turn that went through)
            sess.pop("net_tries", None)   # a turn went through: the next network cut starts from the short pause again
        codex_acct = data.get("account", (sess or {}).get("account")) if data.get("engine") == "codex" else None
        if data.get("auth_lost") and codex_acct and _codex_pool_on(st):
            # the Codex account pool: the sign-in of this process's account is lost, not necessarily the hub's
            _codex_lost(st, ev, codex_acct, {"why": data.get("text")})
        elif data.get("auth_lost"):   # a Codex turn failed on the sign-in (401) — as codex.auth_lost
            _codex(st, {**ev, "type": "codex.auth_lost"}, {"why": data.get("text"), "channel": "core"}, ctx)
        elif data.get("engine") == "codex" and not data.get("is_error"):
            if (st.get("codex") or {}).get("lost"):
                st["codex"]["lost"] = None   # a Codex turn passed — there is a sign-in (the 401 was a one-off, or signed in from Codex's terminal)
            _codex_let_in_account(st, codex_acct)
        if data.get("engine") == "codex" and not data.get("is_error") and data.get("turns"):
            _codex_let_in(st, ev, data)   # a model turn, not a command's answer (turns 0)
        # closed an assignment or did a big piece of work (many steps): ask it to finish its own CLAUDE.md
        tools_in_turn = (sess or {}).get("turn_tools", 0)
        if sess and tools_in_turn >= RECAP_TOOLS and not data.get("is_error") and sess.get("kind") != "operator":
            # a big turn ended: ask for a Claude Code recap (goal, where it is now, next; owner decision 2026-10-03)
            actions.append({"do": "recap", "session": sess["name"]})
        if sess and (sess.pop("turn_tools", 0) >= REFLECT_TOOLS or any(
                d["state"] == "replied" and d["id"] not in replied and d["session"] == sess["name"]
                for d in st["delegations"].values())):
            _reflect(st, sess, ev)
        if sess:   # the turn result (usually after Stop): thumbs-up means it answered; interrupted, failed or asked the owner: no badge
            _settle(st, sess, data, ev)
        if sess and not blocked and profile_of(sess):
            # the size cap of its own CLAUDE.md: the daemon looks at the file after the agent's turn, not every tick
            actions.append({"do": "profile_check", "session": sess["name"], "profile": profile_of(sess)})
        if sess and tools.answered(data):
            # "new" on a session is a result the owner sees on it, except an answer to a task for the hub/another
            # session (REPLIED is not for him): a turn on his message (turn_owner), an answer to his task (d["for"]) or
            # an ordinary turn that did not close a task for another recipient (14.10: the client lit "new" on any
            # result)
            owner_turn = sess.pop("turn_owner", False)
            name = sess["name"]
            # an answer to his task — or the session's turn on another hub's answer to its request from his turn, or
            # its report soon after his task to it broke (delegations.after_break)
            bus_owner = bool(sess.pop("turn_bus_owner", False))
            answered_owner = any(d.get("for") and d["session"] == name and d.get("replied_seq") == ev["seq"]
                                 for d in st["delegations"].values()) or bus_owner \
                or delegations.after_break(st, sess, ev, shown_text(data["text"]), [st["messages"].get(m) or {}
                                                                                     for m in _took(sess)])
            answered_other = any(d.get("for_session") and d["session"] == name and d.get("replied_seq") == ev["seq"]
                                 for d in st["delegations"].values())
            # a mini-hub handed a task to executors, so the turn result is interim (core/minihub.py): for a session's task
            # it is not "new"; for the owner's task it is "new" (his "assigned" line). A turn busy with nothing else
            # (not an owner turn, not an answer to another task) gets the interim_out mark: not in "Done" (the page and
            # Telegram show it as an ordinary result)
            passed = [d for d in st["delegations"].values() if d.get("passed_seq") == ev["seq"] and d["session"] == name]
            # a mini-hub's executor: the result goes only to the mini-hub (the bus): not "new", not "Done"; a turn on the
            # owner's own word (woke it, a command) is his, as usual
            bus = minihub.boss(st, sess) and not (owner_turn or bus_owner)
            # a result that another session tells the owner (_merged_into) is not "new" and not for Telegram; a turn
            # with the owner's own word or an answer to his task is addressed to him as well
            # a turn only on the core's notes about an executor's hand-in (lessons, delegations._service) is the session's
            # own housekeeping: not output to anyone — "new", the list line and its gist (tools.gist_now compares owner_out
            # with last_out) and the return summary stay on its last real result ("MR opened", not "recorded the lessons")
            service = sess.get("turn_service") and not (owner_turn or answered_owner)
            if not service:
                sess["last_out"] = ev["seq"]  # a result with an answer: there was output (compatibility; the "new" badge goes by owner_out)
            merged = merged if not (owner_turn or answered_owner) else None
            if merged:
                sess["merged"] = {"seq": ev["seq"], "into": merged}
            if not merged and not bus and not service and (owner_turn or answered_owner
                                                           or not (answered_other or any(not d.get("for") for d in passed))):
                sess["owner_out"] = ev["seq"]
                # the start of this result is the second "New" line in the session list; short, one line (snapshot)
                sess["out_head"] = out_head(shown_text(data["text"]))
                sess["out_head_ts"] = ev["ts"]   # the time (event ts) of this result, next to out_head
                sess["out_turn"] = sess.get("turn_seq") or 0   # a gist from an earlier turn is no longer about this result
                # the gist is the first line of the result itself (a rule to sessions: start a long result with it): the
                # result is the last word of the turn, more important than a `hub gist` in the middle ("took on...", a question)
                lead = result_gist(shown_text(data["text"]))
                if lead:
                    sess["gist"] = {"text": lead, "ts": ev["ts"], "turn": sess["out_turn"], "result": True}   # about the result itself
            # strictly for the owner (for hub.addUnread plugins): a turn on his message or a continuation of such a turn
            # without a marker, an answer to his task; not core and session service output. Only among the "new"
            # (owner_out): an answer to another session's task and an executor's result to a mini-hub are not for him,
            # even in the continuation of his turn
            if sess.get("owner_out") == ev["seq"] and (sess.get("owner_chain") or answered_owner):
                sess["owner_reply"] = ev["seq"]
                # the start of the answer itself: a later result the session sends on its own replaces out_head, and the
                # "Answered you" line must still say what was answered (snapshot reply_head, reply_ts)
                sess["reply_head"] = sess["out_head"]
                sess["reply_ts"] = ev["ts"]
            if passed and not (owner_turn or bus_owner) and not any(
                    d["session"] == name and d.get("replied_seq") == ev["seq"] for d in st["delegations"].values()):
                sess["interim_out"] = ev["seq"]
            if st["owner"].get("state") == "away" and sess.get("kind") != "hub" and sess.get("interim_out") != ev["seq"] \
                    and not bus and not service and (not merged or (st["sessions"].get(merged) or {}).get("kind") == "hub"):
                # "Done" in the return summary is the last result of every working session, even without assignments
                # (03.10: v1 went to prod overnight and the summary said "nothing"); a specialist's executor is in the
                # specialist's result
                done = [d for d in st["owner"]["done"] if d["session"] != sess["name"]]
                item = {"session": sess["name"], "text": delegations.short_text(shown_text(data["text"]))}   # without HANDOFF-DONE
                did = next((d["id"] for d in st["delegations"].values() if d["state"] == "replied"
                            and d["id"] not in replied and d["session"] == sess["name"]), None)
                st["owner"]["done"] = done + [{**item, "delegation": did} if did else item]
        if sess and sess["state"] in ("busy", "dialog"):
            _session_event(st, {**ev, "type": "_turn_ended"}, {}, actions)
        return
    if kind in ("session.close_requested", "session.close_ready"):
        return _closing(st, ev, data, actions, ctx)
    if kind in OLD_FORKS:
        return
    if kind == "session.reload_requested":  # a new process, same conversation, when free (MCP failed to come up); "starting" is immediate
        sess = st["sessions"].get(ev.get("entity"))
        # who may comes first: the refusal by state names the next command, and that is advice only to those who may act
        if guards.from_owner_or_hub(st, data) and (not sess or data.get("sender") != sess["name"]):
            raise Rejected(T("core.reducer.reload_who", "a restart is for the owner, the hub or the session itself"))
        if not sess or sess["state"] not in LIVE:   # what to do instead depends on the state (machines: refusals)
            why = refusal(SESSION, sess["state"] if sess else "absent", kind, _real(ev.get("entity")))
            raise Rejected(_named(why, ev.get("entity")))
        refusals.reload(sess, data)   # a repeat of Opus's filter waits for this hand (core/refusals.py)
        if sess["state"] == "starting":
            # the launch hung (process alive, no SessionStart): such a session never gets a free minute, dirty would
            # wait forever, so restart now, same conversation. Not counted in MAX_RESTARTS: it is a request, not a crash
            sess["since"] = ev["ts"]
            actions += [{"do": "kill", "session": sess["name"]}, spawn_action(sess, st=st)]
            return
        sess["dirty"] = True  # the same path as a new tool set: release + --resume in a free minute
        sess["respawn_now"] = True   # asked for: a background run does not hold it (_relaunch_waits)
        if data.get("sender") == sess["name"] and not sess.get("reload_went"):
            # the session itself, mid-task: continue it after the launch (the same matter: an assignment or an owner
            # word). If it asks again before a new matter arrives (reload_went), there is no second continuation:
            # otherwise a loop while the tool never comes up
            sess["reload_asked"] = _continues(st, _head(sess), sess.get("delegation")) or True
        return
    if kind == "session.worktree_removed":  # hubd removed (or kept) a deleted session's working copy
        sess = st["sessions"].get(ev.get("entity"))
        if not sess or not sess.get("worktree"):
            raise Rejected(T("core.reducer.no_worktree", "no working copy"))
        sess["worktree"].update(cleaned=True, cleanup=data.get("why"))
        if not data.get("ok"):  # the work stayed on disk; the hub should know where (why_l10n: in the owner's language)
            ctx["notify"](ev, T("core.msg.worktree_kept", "The working copy of the removed session {name} is kept: {why}.")
                          .format(name=sess["name"], why=coretext.got(data, "why")))
        return
    if kind == "session.role_set":
        return _role_set(st, ev, data)
    if kind == "session.iconed":
        return _iconed(st, ev, data)
    if kind == "layout.set":   # the whole session-list layout (core/layout.py)
        why = layout.set_layout(st, ev, data)
        if why:
            raise Rejected(why)
        return
    if kind == "session.renamed":
        return _rename(st, ev, data, actions)
    if kind == "ext.post":
        return _ext_post(st, ev, data)
    if kind == "session.waiting":
        # what a session waits for is its own fact (owner decision 2026-10-03: if there is something to do, signal
        # that they wait for something; "free" says nothing useful); the hub and the owner can say it for it
        sess = st["sessions"].get(ev.get("entity"))
        if not sess or sess["state"] == "removed":
            raise Rejected("unknown session")
        from core.guards import _sender_is_hub
        if data.get("channel") == "session" and data.get("sender") != sess["name"] and not _sender_is_hub(st, data):
            raise Rejected(T("core.reducer.waiting_who", "what a session waits for is said by itself (or the hub)"))
        if data.get("clear"):
            sess.pop("waiting", None)
            return
        text = str(data.get("text") or "").strip()
        if not text:
            raise Rejected(T("core.reducer.waiting_text", "waiting for: a text is needed — whom or what"))
        sess["waiting"] = {"text": text[:200], "ts": ev["ts"]}
        return
    if kind == "session.gist":
        # a session's gist in one line is its own fact (`hub gist`: write the essence itself and fit in 160
        # characters): what it did, what it needs from the owner, what it is doing; the hub and the owner can say it
        # for it. The turn is the one it was said in: a later result for the owner without a gist line of its own
        # hides it, unless the session waits or runs a background job of its own (tools.gist_now)
        sess = st["sessions"].get(ev.get("entity"))
        if not sess or sess["state"] == "removed":
            raise Rejected("unknown session")
        from core.guards import _sender_is_hub
        own = data.get("channel") == "session" and (data.get("sender") == sess["name"] or _sender_is_hub(st, data))
        if not own and not tools.from_owner(data):   # a plugin and a module do not speak for it
            raise Rejected(T("core.reducer.gist_who", "a session's gist is said by itself (or the hub, the owner)"))
        if data.get("clear"):
            sess.pop("gist", None)
            return
        text = one_line(strip_markup(str(data.get("text") or "")[:MD_SCAN]), word=True)
        if not text:
            raise Rejected(T("core.reducer.gist_text", "gist: a text is needed — what it did, what it needs from the "
                                                       "owner or what it is doing"))
        sess["gist"] = {"text": text, "ts": ev["ts"], "turn": sess.get("turn_seq") or 0}
        return
    if kind == "session.recap":
        # what it is working on now comes from the watchdog (modules/watch); the Claude Code recap after a big turn from the host (by claude)
        sess = st["sessions"].get(ev.get("entity"))
        if not sess or data.get("channel") == "session":
            raise Rejected(T("core.reducer.recap_who", "the watchdog and the host write the recap"))
        field, limit = ("away", 600) if data.get("by") in HOST_RECAP else ("recap", 200)
        saw = data.get("turn")   # the watchdog names the turn it saw: the next could have started while the model thought
        if field == "recap" and isinstance(saw, int) and not isinstance(saw, bool) and 0 < saw < (sess.get("turn_seq") or 0):
            raise Rejected(T("core.reducer.recap_past", "the \"working on\" line is about a past turn"))
        sess[field] = {"text": str(data.get("text") or "")[:limit], "by": data.get("by"), "ts": ev["ts"]}
        return
    if kind == "session.model_refused":   # Opus's filter moved it to another model: back when free (core/refusals.py)
        sess = st["sessions"].get(ev.get("entity"))
        return refusals.fact(st, ev, data, _engine(st, sess) if sess else None)
    if kind == "session.kit":
        # which skills and MCP servers its process loaded (core/kit.py, spec d2547143): the host says it only when it
        # changed; not in the page's snapshot — /api/session/kit gives it
        sess = st["sessions"].get(ev.get("entity"))
        if sess and sess["state"] != "removed" and isinstance(data.get("skills"), list):
            sess["kit"] = {**data, "at": ev["ts"]}
        return
    if kind == "session.info":
        sess = st["sessions"].get(ev.get("entity"))
        if sess:
            refusals.info(sess, data, ev["ts"])   # after a return from Opus's fallback: is it on the asked model
            sess.pop("mcp_init", None)
            sess["info"] = dict(data)   # its own copy: mcpwatch.status edits info, while below we read data as it came
            # MCP failed to connect at start (a timeout while the whole fleet is coming up): reconnect in place,
            # without restarting the session; if that fails, retry no more often than mcpwatch.RETRY, tell the hub
            # once (core/mcpwatch)
            if data.get("mcp_failed") and sess["state"] in LIVE:
                failed = set(data["mcp_failed"])
                mcpwatch.status(st, ev, {"servers": {m: "failed" if m in failed else "connected"
                                                     for m in data.get("mcp") or []}}, actions, ctx)
                sess["info"] = {**data, "mcp_auth": data.get("mcp_auth") or []}
            else:   # connected at start: the next drop again gets an attempt and a word to the hub (tmux has no polling)
                fix = (sess.get("mcp_watch") or {}).get("fix") or {}
                for m in set(data.get("mcp") or []) - set(data.get("mcp_auth") or []):
                    fix.pop(m, None)
                    if m not in (data.get("mcp_failed") or []):
                        mcpwatch.came_up(st, m)
            _held_outside(st, ev, sess, data)
            if "mcp_auth" in data:   # since when it waits, through restarts (core/mcpwatch.report); old hosts do not say
                auth = [m for m in data["mcp_auth"] or [] if isinstance(m, str)]
                named = [m for m in data.get("mcp") or [] if isinstance(m, str)]
                failed = [m for m in data.get("mcp_failed") or [] if isinstance(m, str)]
                if isinstance(data.get("mcp_connected"), list):   # the host names connected and disabled ones
                    up, off = data["mcp_connected"], data.get("mcp_disabled") or []
                    unsure = [m for m in named if m not in auth and m not in up and m not in off]   # failed, pending
                else:   # a host from before: the rest counted as connected
                    up, unsure = [m for m in named if m not in auth and m not in failed], failed
                mcpwatch.report(st, ev, sess, auth, up, unsure)
            # MCP waits for login (OAuth): tell the hub how to log in without an interactive session, once per server,
            # not per session: Claude Code has one login per server (owner decision 2026-10-07: authorize a repo's mcp
            # right when the repo is connected; after a restart the hub logged in over and over for every session)
            mcpwatch.auth_notice(st, ev, sess, data.get("mcp_auth"), ctx["notify"])
        return
    if kind == "mcp.logged_in":
        # logged in to an MCP (hub mcp login): sessions where it waited for login restart with the same conversation when free
        server = data.get("server")
        who = st["sessions"].get(ev.get("entity")) or {}
        if not isinstance(server, str):
            return
        # the server at the address of the session that logged in (another one under the same name keeps waiting)
        fp = (who.get("mcp_waits") or {}).get(server, mcpwatch.fp_of(who, server))
        mcpwatch.after_login(st, server, fp, actions)
        return
    if kind == "mcp.reconnect_requested":
        # hub mcp reconnect: right now, past the automatic pause (core/mcpwatch); who may — as for hub mcp login
        try:
            return mcpwatch.reconnect_requested(st, ev, data, actions)
        except ValueError as e:
            raise Rejected(str(e)) from None
    if kind == "mcp.wait_dismissed":   # the owner closed a row of the "MCP waits for a login" band (core/mcpwatch)
        try:
            return mcpwatch.wait_dismissed(st, ev, data)
        except ValueError as e:
            raise Rejected(str(e)) from None
    if kind in ("mcp.login_requested", "mcp.callback_given"):
        # a sign-in from the page (core/mcpwatch): only the owner; the link and the address are not in the journal
        try:
            return (mcpwatch.login_requested if kind == "mcp.login_requested" else mcpwatch.callback_given)(
                st, ev, data, actions)
        except ValueError as e:
            raise Rejected(str(e)) from None
    if kind == "session.mcp_login":   # a step of a sign-in attempt from the host or hubd (core/mcpwatch)
        return mcpwatch.login_step(st, ev, data, actions)
    if kind == "catalog.loaded":
        # a previous hub's event may carry the env and headers of MCP servers; they do not go into the state
        old, st["catalog"] = st["catalog"], redact.public_catalog(data.get("tools"))
        tools.close_held(st, ev, st["sessions"].values())
        if data.get("restart_updated"):
            _plugins_updated(st, old)
        return
    if kind.startswith("tools."):
        name = identity.address(st, ev.get("entity")) or ev.get("entity")   # "hub" as in tools.apply
        sess = st["sessions"].get(name) or {}
        data = dict(data, tool=tools.resolve(st, sess, data.get("tool")))   # session notes use the full id as granted
        out = tools.apply(st, ev, data, ctx)
        if kind == "tools.denied" and sess and data.get("sender") != name and sess["state"] in tools.LIVE:
            # a refusal is told to the session in words: otherwise it waits for a tool that will never come (14.10:
            # "waiting for the browser" after the hub refused and advised another one, and the watchdog raised it to the
            # owner as his decision). Like "Connected": a core message, the turn is a continuation of its assignment
            # (the result goes to whoever set it). For a session not running, see below
            who = WHO_OWNER_HEAD if data.get("owner") else (data.get("sender") or WHO_HUB_HEAD)
            mid = f"m{ev['seq']}"
            new_message(st, mid, name, _denied_text(who, data), ev["ts"], origin=forms.ORIGIN_CORE)
            did, _ = delegations.open_task(st, name)
            if did:
                delegations.continued(st, name, mid, ev, did)
        req = next((r for r in sess.get("requests") or [] if r.get("asleep_decided") == ev["seq"]), None)
        if req:
            req.pop("asleep_decided")
        elif sess.get("asleep_decided") == ev["seq"]:   # a grant to a session not running, without a request (19.10)
            req = {"state": "granted"}
        sess.pop("asleep_decided", None)
        if req and not executors.dormant(sess):
            # the owner's decision on a session not running (17.10): no need to wake it for this, an "FYI" note rides
            # with its next message so that it does not wait for a tool it already has or will never get
            # "the owner" means his decision or a verified word (the core does not verify --for on a non-risky tool)
            who = WHO_OWNER_HEAD if tools.from_owner(data) or req.get("owner_words") else (data.get("sender")
                                                                                           or WHO_HUB_HEAD)
            text = (T("core.msg.granted_asleep",
                      "{who} connected {tool} while the process was not up: it is already in your set.").format(
                who=who, tool=data.get("tool")) if req["state"] == "granted" else _denied_text(WHO_OWNER_HEAD, data))
            new_message(st, f"m{ev['seq']}", name, text, ev["ts"], origin=forms.ORIGIN_CORE, fyi=True)
        return out
    if kind == "browser.reset":
        # the supervisor closed a hung tab: chrome-devtools-mcp keeps stale state in sessions, so a session with this
        # browser restarts (same conversation, MCP anew; 01.10) before its next message, not the whole batch of free
        # sessions at once: on 06.10 under load this went in circles, a launch with MCP, more load, reset again
        tool = f"browser:{ev.get('entity')}"
        for sess in st["sessions"].values():
            if tool in (sess.get("tools") or []) and sess["state"] in ("starting", "idle", "busy", "dialog"):
                sess["browser_stale"] = True
        return
    if kind == "browser.status":
        # the browser supervisor reports whether a browser responds (modules/browser); one removed from the settings is dropped
        browsers = st.setdefault("browsers", {})
        if data.get("removed"):
            browsers.pop(ev.get("entity"), None)
        else:
            old = browsers.get(ev.get("entity")) or {}
            same = old and old.get("up") == data.get("up")   # a first status without up starts the count (no KeyError)
            browsers[ev.get("entity")] = {**data, "since": old.get("since", ev["ts"]) if same else ev["ts"]}
        return
    if kind in ("secret.requested", "secret.filled", "secret.failed", "secret.used", "secret.copied", "secret.removed",
                "secret.denied"):
        # "Secrets" (modules/secrets): the last hand-out of each secret for its tab — names, never values. A hand-out is
        # recorded before the value leaves (requested) and after: one without an outcome stays "outcome unknown"
        item = data.get("item")
        if not (isinstance(item, str) and item):
            return
        last = st.setdefault("secrets_last", {})
        if kind == "secret.removed":
            last.pop(item, None)
            return
        if kind == "secret.denied":   # a refusal before any hand-out is no use; after one (the cursor moved) it ends it
            was = last.get(item)
            if was and was["outcome"] == "pending" and was["who"] == ev.get("entity"):
                was["outcome"] = "failed"
            return
        how = "copy" if kind == "secret.copied" else "exec" if data.get("env") else "fill"
        outcome = {"secret.requested": "pending", "secret.failed": "failed", "secret.copied": "ok"}.get(
            kind, "ok" if kind == "secret.filled" or data.get("code") == 0 else "exit")
        last[item] = {"ts": ev["ts"], "who": ev.get("entity"), "how": how, "outcome": outcome,
                      "where": data.get("origin") or data.get("program") or "",
                      **({"code": data["code"]} if isinstance(data.get("code"), int) else {})}
        return
    if kind == "session.background":
        # the host saw a background command or agent start: the session is free but waits for them (as in CC, 30.09)
        sess = st["sessions"].get(ev.get("entity"))
        if sess and sess["state"] not in ("idle", "busy", "dialog"):
            # background belongs only to a live process of its own. Restarting, asleep, with the owner: the list comes
            # from the departing process: a released one still sends them while it stops its tasks, and the cut is
            # already recorded (cut_turn). Otherwise dead tasks hung "in background" on the new process for up to an
            # hour and a half, and the next cut mentioned them a second time (journal, 16.10). Not a refusal: the
            # fact is true, just about a departed process
            return
        if sess and isinstance(data.get("tasks"), list):
            # the full background task list from Claude Code is the truth: what is not in it has finished
            old = sess.get("background") or {}
            # the output file and last output (watchdog, 03.10) are unknown to Claude Code's list: keep our own; a
            # helper agent's model too (the start of its call named it, Claude Code's list does not)
            keep = lambda tid: {k: v for k, v in (old.get(tid) or {}).items()  # noqa: E731
                                if k in ("output", "out", "out_at", "model")}
            sess["background"] = {t["id"]: {"id": t["id"], "kind": t.get("kind"), "title": t.get("title") or "",
                                            "since": (old.get(t["id"]) or {}).get("since", ev["ts"]), **keep(t["id"]),
                                            **_bg_model(t)}
                                  for t in data["tasks"] if t.get("id")}
            # not a set: the order decides which of the departed _bg_gone remembers, and it must not depend on PYTHONHASHSEED
            _bg_gone(sess, [tid for tid in old if tid not in sess["background"]], ev["ts"])
            if old and not sess["background"]:
                # background ended, the session is about to take its result: count the "free" period from here (07.10:
                # an executor was put to sleep the same second and never read the result of its own run)
                sess["background_done"] = ev["ts"]
            return
        if not sess or not data.get("id"):
            raise Rejected(T("core.reducer.bg_no_session", "no such session or id"))
        if data.get("done"):  # finished mid-turn (a task-notification in the same turn)
            if sess.setdefault("background", {}).pop(data["id"], None):
                _bg_gone(sess, [data["id"]], ev["ts"])
            return
        sess.setdefault("background", {})[data["id"]] = {"id": data["id"], "kind": data.get("kind"),
                                                         "title": data.get("title") or "", "since": ev["ts"],
                                                         **_bg_model(data),
                                                         **({"output": str(data["output"])[:400]} if data.get("output") else {})}
        return
    if kind == "session.background_output":
        # the watchdog saw a background command print something (owner decision 2026-10-03: he could not tell what a
        # session was doing): when and the last line; the page shows what runs in the background
        sess = st["sessions"].get(ev.get("entity"))
        task = ((sess or {}).get("background") or {}).get(data.get("id") or "")
        if data.get("channel") == "session":
            raise Rejected(T("core.reducer.bg_output_who", "the watchdog writes background output, not a session"))
        if not task:
            if sess and overtaken(data, (sess.get("bg_gone") or {}).get(data.get("id") or "")):
                return   # it ended while the watchdog read its output (the "[killed]" output and the end in the same second)
            raise Rejected(T("core.reducer.bg_no_task", "no such background task"))
        task["out_at"], task["out"] = data.get("at"), str(data.get("line") or "")[:200]
        return
    if kind == "owner.notify":
        # a message to the owner from a monitor (modules/monitors, `hub notify`); for a session, the word to the owner is the turn result
        # the command of a job the session wrote runs as that session (schedule.speaker_of) — a milestone writes to him
        if data.get("channel") == "session" and not schedule.from_own_job(st, data):
            raise Rejected(T("core.reducer.notify_session",
                             "a session writes to the owner with its turn result, not hub notify"))
        if not (data.get("text") or "").strip():
            raise Rejected(T("core.reducer.notify_empty", "an empty message"))
        owner.noticed(st, ev, data)   # the notice's occasion (push spec, section 2.1): id, sender, update
        schedule.milestone(st, ev, data, {"new_message": new_message, "hub": _hub})   # a stuck progress job is removed
        if data.get("channel") == "core" and data.get("id") == CODEX_NOTICE and (st.get("codex") or {}).get("lost"):
            st["codex"]["lost"]["told"] = True   # this loss was told — once, across restarts
        name = str(data.get("id") or "")[len(CODEX_NOTICE) + 1:] if str(data.get("id") or "").startswith(
            CODEX_NOTICE + ":") else None
        lost = ((_pool_of(st, "codex").get("lost") or {}).get(name) or None) if name else None
        if data.get("channel") == "core" and lost:
            lost["told"] = True   # the Codex account pool: this account's loss was told — once
        return
    if kind == "owner.notify_resolved":   # the notice's cause is gone: a module, a schedule, the hub or the owner clears it
        owner.resolved(st, ev, data)
        return
    if kind == "core.claude_missing":   # hubd: no Claude Code program — spawns wait for it (daemon.wait_claude); null — it is there
        path = data.get("path")
        if path is None:
            st.pop("claude_missing", None)
        elif isinstance(path, str) and path:
            st["claude_missing"] = {"path": path[:500], "since": (st.get("claude_missing") or {}).get("since", ev["ts"])}
        else:
            raise Rejected(T("core.reducer.claude_missing_path", "path is a string or null"))
        return
    if kind == "core.login_missing":   # hubd: Claude Code has no sign-in — spawns wait for it (daemon.wait_login); false — it has
        if not isinstance(data.get("missing"), bool):
            raise Rejected(T("core.reducer.login_missing_flag", "missing is true or false"))
        if data["missing"]:
            st["login_missing"] = {"since": (st.get("login_missing") or {}).get("since", ev["ts"])}
        else:
            st.pop("login_missing", None)
        return
    if kind == "update.checked":
        # the result of an update check (owner decision 2026-10-03: check updates in the About tab)
        # the check is over (result 'moved' has neither applied nor failed); update.checked is written by one cycle
        # of the update module, one pass at a time, so another pass's tests do not run meanwhile
        st.pop("update_testing", None)
        hint = gitaccess.kind(data.get("error"))   # git could not reach GitHub: the kind of the failure (core/gitaccess.py)
        st["update_check"] = {"result": str(data.get("result") or "")[:20], "behind": data.get("behind"),
                              "notes": list(data.get("notes") or [])[:12], "error": data.get("error"), "ts": ev["ts"],
                              # a newer Claude Code is needed: what is needed and what is installed
                              **{k: str(data[k])[:20] for k in ("need_claude", "have_claude") if data.get(k)},
                              **({"hint": hint} if hint else {}),   # the page turns it into "what to do"
                              # a release without the function's built-in code waits for the module (extraction spec section 4.4): why
                              **({"need_module": [str(w)[:200] for w in data["need_module"][:10]]}
                                 if isinstance(data.get("need_module"), list) else {}),
                              # the update channel and the release it installs (tag vX.Y.Z; 15.10)
                              # a code layer's nightly version is longer (1.9.3-nightly.<time>.<sha>, core/appcode.NAME)
                              **{k: str(data[k])[:64] for k in ("update_channel", "version", "have_version") if data.get(k)},
                              # release hold: when it installs by itself (Unix seconds); "Install" does it at once
                              **({"install_after": data["install_after"]} if isinstance(data.get("install_after"), (int, float))
                                 and not isinstance(data["install_after"], bool) else {}),
                              # the app (installer spec S3): updates by the button, "Return <previous>", a release that
                              # needs a newer app (environment numbers and where to download it)
                              **({"app": True} if data.get("app") is True else {}),
                              # the app on a code layer (modules/update/layer.py): versions without a count of changes
                              **({"layer": True} if data.get("layer") is True else {}),
                              # installed in the background, waiting for the person's restart (modules/update/layer.py
                              # _restart_or_ask): the page's "Restart"
                              **({"ask_restart": True} if data.get("ask_restart") is True else {}),
                              **{k: str(data[k])[:64] for k in ("previous",) if data.get(k)},
                              # a build of the person's edits runs (`hub app apply`): "Return the released"
                              **({"own": {"release": built["release"][:64], "edits": built["edits"]}}
                                 if isinstance(built := data.get("own"), dict) and isinstance(built.get("release"), str)
                                 and isinstance(built.get("edits"), int) and not isinstance(built["edits"], bool) else {}),
                              **{k: data[k] for k in ("need_runtime", "have_runtime")
                                 if isinstance(data.get(k), int) and not isinstance(data[k], bool)},
                              **({"download": data["download"]} if isinstance(data.get("download"), str)
                                 and data["download"].startswith("https://") and len(data["download"]) <= 300 else {}),
                              # the new app itself is on the distribution server (app spec §4.4): its environment and
                              # size; auto — "Update" puts it in place (Mac, Windows), file — the .deb to install (Linux)
                              **({"shell": shell} if (shell := app_shell_shape(data.get("shell"))) else {}),
                              # the hub's own edits of the core against the installed release (Plugin API 1.33)
                              **({"local_edits": edits} if (edits := local_edits_shape(data.get("local_edits"))) else {}),
                              # the app on a code layer: below the distribution server's minimum version (app spec
                              # §7.2) — the band and Telegram; reports of failed updates go to the developers (§7.1)
                              **({"min": low} if (low := minimum_shape(data.get("min"))) else {}),
                              **({"report": {"on": data["report"]["on"]}} if isinstance(data.get("report"), dict)
                                 and isinstance(data["report"].get("on"), bool) else {})}
        return
    if kind == "update.modules":
        # private modules installed, or why not (modules/update/private.py): "About" and the statistics report
        mods = data.get("modules") if isinstance(data.get("modules"), dict) else {}
        hint = None if data.get("access") is True else gitaccess.kind(data.get("why"))   # no access: why, by kind
        st["private_modules"] = {
            "ok": data.get("ok") is True, "access": data.get("access") is True, "ts": ev["ts"],
            "ref": str(data.get("ref") or "")[:40], "why": str(data.get("why") or "")[:600],
            **({"hint": hint} if hint else {}),
            **{k: [str(x)[:40] for x in data[k][:10]] for k in ("missing", "declined", "local_edits")
               if isinstance(data.get(k), list)},
            "modules": {str(n)[:40]: str(s)[:20] for n, s in list(mods.items())[:10]}}
        return
    if kind == "update.testing":
        # the new code is merged and tests run (~5 min): not yet "waiting for restart"; applied / failed will report the result (04.10)
        st["update_testing"] = {"ts": ev["ts"], "from": data.get("from"), "upstream": data.get("upstream")}
        return
    if kind == "agents.system":   # system agents (core/system.py): named by the core at every start
        if data.get("channel") != "core":
            raise Rejected(T("core.reducer.system_who", "the core names the system agents"))
        st["system"] = [str(n) for n in data.get("names") or []]
        st["trainer"] = data.get("trainer")   # its page has "Train executors"
        return
    if kind == "rules.reviewed":   # the owner's rules (hub.json rules) were handed to the dev agent for review (core/system.py)
        if data.get("channel") != "core":
            raise Rejected(T("core.reducer.rules_who", "the core orders the review of the rules"))
        st["rules_reviewed"] = str(data.get("digest") or "")
        if isinstance(data.get("rules"), list):   # the previous core did not write the set, so its journals add nothing to sessions
            news.rules(st, ev, data["rules"])
        return
    if kind == "news.loaded":   # at startup the core read CHANGES.md: the items under its "for sessions" heading (core/news.py)
        if data.get("channel") != "core":
            raise Rejected(T("core.reducer.news_who", "the core reads what is new for sessions"))
        return news.load(st, ev, data)
    if kind in ("update.applied", "update.failed"):
        st.pop("update_testing", None)
        # updates from the shared repository, for the "Updates" page (owner decision 2026-10-02: show that an update
        # exists and let him see what changed)
        ups = st.setdefault("updates", [])
        ups.append({"ts": ev["ts"], "seq": ev["seq"], "ok": kind == "update.applied", "notes": list(data.get("notes") or []),
                    "commits": list(data.get("commits") or [])[:30], "why": data.get("why"),
                    "files": list(data.get("files") or []), "from": data.get("from"), "to": data.get("to"),
                    **({"version": str(data["version"])[:64]} if data.get("version") else {}),
                    # who went back (a code layer): "timeout" — the release did not come up, "owner" — "Return"
                    **({"by": data["by"]} if data.get("by") in ("timeout", "owner") else {})})
        del ups[:-20]
        return
    if kind == "owner.file":
        # a file for the owner: video, document, picture (02.10: videos would not open); Telegram sends it to the chat
        path = str(data.get("path") or "")
        if not is_abs_path(path):
            raise Rejected(T("core.reducer.file_path", "a full path to the file is needed"))
        return
    if kind == "watch.needs_owner":
        # watchdog: the turn result waits for something from the owner but there is no form question: a "waits for you"
        # mark on the session until the owner writes to it or it starts working again (owner decision 2026-09-30: it
        # reported but asked nothing)
        sess = st["sessions"].get(ev.get("entity"))
        if data.get("channel") == "session":
            raise Rejected(T("core.reducer.needs_owner_who", "the watchdog sets the mark, not a session"))
        why = attention_refused(st, sess)
        if why:
            if why in (UNKNOWN, NOT_FREE) and sess and overtaken(data, sess.get("since")):
                return   # since the watchdog read the result, the session is mid-turn again, asked via a form, or removed
            raise Rejected(why)
        sess["attention"] = {"since": ev["ts"], "p": data.get("p"), "ask": data.get("ask"), "text": data.get("text")}
        return
    if kind == "session.attention_off":
        # the session itself says its result needs nothing from the owner (`hub attention off`): the "waits for you"
        # mark goes, and the result of the turn it was said in does not set it again (attention_refused) — the next
        # turn is judged as usual. The hub and the owner can say it for it, of the mark they saw: with no mark there is
        # nothing of theirs to hold back — a turn running, or a result in and not judged yet, is judged as usual
        # (review). Another session, a plugin, a module — no
        sess = st["sessions"].get(ev.get("entity"))
        if not sess or sess["state"] == "removed":
            raise Rejected("unknown session")
        from core.guards import _sender_is_hub
        itself = data.get("channel") == "session" and data.get("sender") == sess["name"]
        own = itself or data.get("channel") == "session" and _sender_is_hub(st, data)
        if not own and not tools.from_owner(data):
            raise Rejected(T("core.reducer.attention_off_who",
                             "the \"waits for you\" mark is cleared by the session itself, the hub or the owner"))
        if own and data.get("schedule_job"):   # a schedule script on behalf of a session is not its turn (core/chain.py)
            raise Rejected(T("core.reducer.attention_off_job",
                             "the \"waits for you\" mark is cleared by a turn of the session, not by a schedule job"))
        if sess.pop("attention", None) or itself:   # a mark is always of the latest turn: turn.started drops it
            sess["attention_off"] = sess.get("turn_seq")
        return
    if kind == "watch.alert":
        return _watch_alert(st, ev, data, ctx)
    if kind == "watch.surrender":
        # the watchdog's concession detector (optional; 06.10): the session gave in to the owner's doubt without
        # checking, so verify on the merits and answer him again; the answer goes to the same thread (msg["re"],
        # modules/telegram)
        sess, msg = st["sessions"].get(ev.get("entity") or ""), st["messages"].get(data.get("message") or "")
        if data.get("channel") == "session":
            raise Rejected(T("core.reducer.surrender_who", "the watchdog notices a concession, not a session"))
        if not sess or sess["state"] in ("removed", "closed", "failed") or not msg or not msg.get("from_owner") \
                or msg["session"] != sess["name"]:
            raise Rejected(T("core.reducer.surrender_no_message", "no such message of the owner to this session"))
        done = st.setdefault("watch", {}).setdefault(SURRENDER_KEY, [])
        if msg["id"] in done:
            raise Rejected(T("core.reducer.surrender_told", "this message was already spoken about"))
        done[:] = done[-50:] + [msg["id"]]
        new_message(st, f"m{ev['seq']}", sess["name"],
                    SURRENDER_NOTE.format(id=msg["id"], text=delegations.short_text(str(msg["text"]), 200)),
                    ev["ts"], origin=forms.ORIGIN_CORE, re=msg["id"])
        return
    if kind == "watch.promise":
        return _watch_promise(st, ev, data)
    if kind == "account.limits":
        if data.get("engine") == "codex":   # Codex's limits are a row of their own
            return _codex_limits(st, ev, data)
        # Claude account limits from the host (30.09: show limits as in Claude Code), the latest; with an account
        # pool each account has its own, and the limits line is the active one's
        acc = st.get("account") or {}
        name = data.get("account")
        if name and (data.get("gen") or 0) < ((acc.get("refreshed") or {}).get(name) or 0):
            return   # a process with the old token of an account that took a fresh one (accounts: refresh), not about the new one
        # the default login (no pool) goes under the key "" (an account name is never empty): when we return to it, its numbers apply
        was = ((acc.get("seen") or {}).get(name) or {}) if name else {}
        data = _held_windows((acc.get("seen") or {}).get(name or ""), data, ev["ts"], (acc.get("refreshed") or {}).get(name))
        seen = {**{k: data.get(k) for k in ("status", "type", "windows")}, "ts": ev["ts"]}
        held = ((was.get("windows") or {}).get("suspended") or {}).get("resets") or 0
        if was.get("type") == "suspended" and held > ev["ts"] and data.get("status") != "rejected":
            # a model suspension holds until its term whatever another model answers (a probe asks a small one): the
            # fresh windows are kept, the refusal stays (_refuse_login); a limit refusal is a refusal of its own
            seen = {**seen, "status": "rejected", "type": "suspended",
                    "windows": {**(data.get("windows") or {}), "suspended": was["windows"]["suspended"]}}
            data = {**data, "status": "rejected", "type": "suspended"}   # the limits line says the same
        st.setdefault("account", {}).setdefault("seen", {})[name or ""] = seen
        gen = (acc.get("refreshed") or {}).get(name) or 0
        if name and ((st["account"].get("first_seen") or {}).get(name) or {}).get("gen") != gen:
            # the first reading of the account's current token this core has: accounts.decide tells a token used up at its
            # limit (it had reserve then) from one handed out spent
            st["account"].setdefault("first_seen", {})[name] = {**seen, "gen": gen}
        _overage_notice(st, ev, ctx, name or "", data.get("overage") is True, data.get("windows"))
        if name and data.get("status") in ("allowed", "allowed_warning") and name in (acc.get("refused") or {}):
            # the token answered (a probe, "Refresh", a session's request): its login refusal was a one-off — the block
            # it set goes and a later refusal is a first one again (_refuse_login); the active one's block is lifted by
            # the pool's decision ("free"), which also continues the turns cut on it (_account)
            acc["refused"].pop(name)
            if name != acc.get("active"):
                (acc.get("blocked") or {}).pop(name, None)
        if name == acc.get("active") and not _codex_whole(st):
            st["limits"] = {**data, "ts": ev["ts"]}
        return
    if kind in ("account.switched", "account.exhausted", "account.waiting") + CODEX_POOL_KINDS:
        return _account(st, ev, data, ctx)   # the Claude pool, or the Codex pool (codex.*: the same rules, its record)
    if kind == "executor.unmerged":   # an executor's branch is not merged into its base
        return executors.on_unmerged(st, ev, data, ctx)
    if kind in ("codex.host", "codex.login", "codex.logout", "codex.auth_lost", "codex.limit_reset", "codex.spent") \
            + CODEX_ACCOUNT_KINDS:
        return _codex(st, ev, data, ctx)   # the hub's Codex sign-in and limit
    if kind == "account.token":   # a pool account's token fingerprint (daemon): a change means a new generation
        return _account_token(st, ev, data)
    if kind == "account.no_token":   # the active command account's command gave no token / a token came (daemon)
        return _account_no_token(st, ev, data)
    if kind == "account.unblocked":   # the "busy until" marks that no longer hold: under the threshold now (accounts._lifted)
        return _account_unblocked(st, data)
    if kind == "gateway.changed":   # the accounts gateway (modules/gateway), written by the daemon on change
        return _gateway(st, ev, data, ctx)
    if kind == "budget.mode":   # the limit economy mode (core/budget.py), written by the daemon on change
        return budget.apply(st, ev, data, ctx)
    if kind == "speech.said":   # bot: the voice module did or did not voice a reply (core/providers.py, consecutive failures)
        from core import providers
        return providers.said(st, ev, data)
    if kind.startswith("ext."):  # extension facts (EXTENDING.md): "ext.<name>.<what>" go to the journal and do not change the state
        if len(kind.split(".")) < 3:
            raise Rejected(T("core.reducer.ext_kind", "an extension event is ext.<name>.<what>"))
        return
    if kind == "mod.prompt":  # what modules changed in the session prompt (core/modprompt.py), for hub show
        return modprompt.apply(st, ev, data)
    if kind == "module.forget":   # the owner clears a warning about a module with core.write (core/corewrite.py)
        return corewrite.forget(st, data)
    if kind.startswith("module."):  # core modules (core/modrun.py): failure, readiness, clearing a stop
        return modrun.apply(st, ev, data, actions)
    if kind.startswith("mod."):  # a core module's own state (core/modstate.py): mods.<name>, only its own branch
        return modstate.apply(st, ev, data)
    if kind.startswith("net."):  # the Hubnet network was removed from the hub (02.10); its events in old journals are skipped
        raise Rejected(NO_HUBNET)
    if kind in ("run.requested", "run.stop_requested", "core.run_started", "core.run_ended"):   # `hub run` (core/hubrun.py)
        return hubrun.apply(st, ev, data, {"new_message": new_message, "chain": lambda sess: _run_chain(st, sess)})
    if kind.startswith("schedule."):
        return schedule.apply(st, ev, data, {"new_message": new_message, "hub": _hub})
    if kind.startswith("hub.restart_") or kind == "page.held":   # page.held: the page holds a restart (core/restart.py)
        return restart.apply(st, ev, data, ctx)
    if kind == "core.archived":   # handed-in tasks of deleted executors go to the archive (core/delegations.archive)
        return delegations.archive(st, ev, data, ctx)
    if kind.startswith(("core.profile_", "profile.")):   # the size cap of an agent's CLAUDE.md (core/profcap.py)
        return _profile(st, ev, data, actions)
    if kind == "core.pruned":
        # long-delivered messages leave the state (they are in the journal); those needed by open assignments stay
        keep = {x for d in st["delegations"].values() if d["state"] not in ENDED for x in (d.get("for"), d.get("message"))}
        for mid, m in list(st["messages"].items()):  # the same condition as in prunable
            if m["state"] in PRUNABLE and m["created"] < data.get("before", 0) and mid not in keep:
                del st["messages"][mid]
        return
    if kind == "core.slept":
        return _slept(st, data)   # the machine slept: terms that count a living process's time move by it
    if kind == "core.started":
        st["started"] = ev["ts"]  # since when the core runs, for "About" (03.10: uptime)
        st["started_seq"] = ev["seq"]   # and by which event: is the core on pre-update code? (core/restart.behind)
        if "ui_lang_default" not in st:
            # the interface language with no setting (core/i18n.py human): a hub whose history began before languages
            # stays Russian; a start without the i18n mark is a previous core, with the mark and no history a new hub
            # (once)
            history = st["sessions"] or st["messages"] or st["delegations"] or not data.get("i18n")
            st["ui_lang_default"] = "ru" if history else ""
        st["host"] = data.get("host") or "stream"   # tmux sends no turn results, so the eyes/thumbs badges have nothing to decide by
        modrun.started(st)   # modules that failed to come up last time try again
        for name in restart.wake(st):   # sessions that asked the hub to restart the hub mid-task
            sess = st["sessions"].get(name)
            queued = any(m["session"] == name and m["state"] in parking.PENDING for m in st["messages"].values())
            if sess and sess["state"] == "idle" and not sess.get("cut_turn") and not queued:   # a waiting message wakes it
                # the turn result released the assignment from the session (delegations.on_result); the same matter if
                # the turn was on it: last_task (on_turn sets it for the assignment's turn and clears it for a new
                # matter); a finished one is not a continuation (review)
                d = st["delegations"].get(sess.get("delegation") or sess.get("last_task") or "") or {}
                task = d["id"] if d.get("session") == name and d.get("state") in ("working", "replied", "stopped") else None
                new_message(st, f"m{ev['seq']}x6", name, RESTART_ASKED, ev["ts"], origin=forms.ORIGIN_CORE,
                            **({"root": sess["turn_root"]} if sess.get("turn_root") else {}),
                            **_continues(st, _head(sess), task))
        return restart.started(st, ev, ctx)
    if kind == "turn.tool" and ev.get("entity") in st["sessions"]:  # steps in a turn measure "big work"
        st["sessions"][ev["entity"]]["turn_tools"] = st["sessions"][ev["entity"]].get("turn_tools", 0) + 1
    if kind == "turn.tool_result":   # an MCP tool with an error: the MCP status right now (core/mcpwatch)
        mcpwatch.tool_error(st, ev, data, actions)
    if kind == "session.mcp":
        mcpwatch.status(st, ev, data, actions, ctx)
        if st["sessions"].get(ev.get("entity")):
            servers = data.get("servers") or {}
            mcpwatch.report(st, ev, st["sessions"][ev["entity"]], [n for n, s in servers.items() if s == "needs-auth"],
                            [n for n, s in servers.items() if s == "connected"],
                            [n for n, s in servers.items() if s in ("failed", "pending")], data.get("addr"))
            for n in [n for n, s in servers.items() if s == "connected"]:
                mcpwatch.connected(st, ev, ev["entity"], n, actions)   # a sign-in from the page went through it
        return
    if kind == "session.mcp_reconnected":
        mcpwatch.reconnected(st, ev, data, ctx)
        if data.get("ok") and isinstance(data.get("server"), str):
            mcpwatch.connected(st, ev, ev.get("entity"), data["server"], actions)
        return
    if kind in ("core.rejected", "cli.gave_up"):
        return _late_reject(st, ev, data)
    if kind == "spool.refused":   # a core.* fact from the spool (daemon.spooled)
        raise Rejected(T("core.reducer.spool_core_only",
                         "{type}: only the core writes core.* events and the spool check count").format(
            type=data.get("type")))
    if kind == "session.compacted" and ev.get("entity") in st["sessions"]:
        sess = st["sessions"][ev["entity"]]
        sess.pop("compacting", None)   # compaction is over, the queue types again (_pump)
        if data.get("ok") and isinstance(data.get("after"), int):
            sess["context"] = data["after"]
        elif "ok" not in data:   # Claude Code compacted by itself (hook): the new length is unknown until the next turn
            sess.pop("context", None)
    if kind == "settings.changed" and data.get("what") == "reactions.enabled" and not data.get("new"):
        for sess in st["sessions"].values():   # the module was switched off: pending reactions to lines are not sent to sessions
            for line in sess.pop("reacts", None) or {}:
                _badge(st, line, None, own=True)   # and they are not in the feed: the session did not receive them
    if kind == "host.done" and data.get("do") == "spawn" and "cut" in data and ev.get("entity") in st["sessions"]:
        refusals.spawned(st["sessions"][ev["entity"]], data)   # whether the host cut the stopped turn (core/refusals.py)
    if kind == "host.done" and data.get("do") == "spawn" and data.get("result") == GATEWAY_BYPASSED \
            and ev.get("entity") in st["sessions"]:
        st["sessions"][ev["entity"]].pop("gateway", None)   # a core module pointed claude at its own address: not on the gateway
    if kind == "host.done" and data.get("do") == "spawn" and data.get("result") in ("spawned", GATEWAY_BYPASSED) \
            and (st["sessions"].get(ev.get("entity")) or {}).get("state") == "starting":
        # the deadline until SessionStart (daemon.check_starting) counts from the real launch, not from the decision
        # to launch: spawn waits in a queue (gateway, modules, backoff), and a new process would otherwise be killed
        # at once. In its own field, not since: others read since (assignments, watchdog), and "already running"
        # launched no process, so it does not extend the deadline
        st["sessions"][ev["entity"]]["spawned"] = ev["ts"]
        if "account" in data:   # a spawn held for the sign-in took the pool's account of the moment (none — it went meanwhile)
            held = st["sessions"][ev["entity"]]
            if data["account"] and isinstance(data["account"], str):
                held["account"] = data["account"]
            else:
                held.pop("account", None)
            if data["account"] and data.get("gen"):
                held["account_gen"] = data["gen"]
            else:
                held.pop("account_gen", None)
    if kind == "host.done" and data.get("do") == "mcp_reconnect" and data.get("result") == "unsupported" \
            and ev.get("entity") in st["sessions"]:
        mcpwatch.unsupported(st["sessions"][ev["entity"]], data.get("server"))   # the host cannot do it in place (tmux)
    if kind in ("host.done", "host.error") and data.get("do") in ("mcp_login", "mcp_callback"):
        mcpwatch.login_host(st, ev, data, actions)   # a sign-in from the page: no request on this host — the old way
    if kind in ("host.done", "host.error") and data.get("do") == "compact" and data.get("result") != "sent" \
            and ev.get("entity") in st["sessions"]:
        st["sessions"][ev["entity"]].pop("compacting", None)   # /compact was not sent (tmux, failure), so there will be no compaction
    if kind in ("spool.checked", "spool.flagged", "spool.mode"):   # admit.OWN
        from core import admit   # not at the top: admit takes NAME from here
        return admit.fold(st, ev)   # the mailbox check: its window and mode (core/admit.py)
    if kind == "settings.changed":
        configwatch.page(st, ev, data)   # a watched key saved from the page: accepted at the next check (core/configwatch.py)
    if kind in appwatch.FACTS:   # the app's code layer: what is edited, the release put back (core/appwatch.py)
        try:
            return appwatch.fold(st, ev, data)
        except appwatch.Bad as e:
            raise Rejected(str(e)) from None
    if kind in configwatch.FACTS:   # the watched settings: the accepted state and rows "changed outside the page"
        try:
            return configwatch.fold(st, ev, data)
        except configwatch.Bad as e:
            raise Rejected(str(e)) from None
    if kind == "telegram.group":
        # the bot's word whether a Telegram group is bound (spec d2513429): its admins are the owner, so on a hub nobody
        # sits at (bus mode remote) a form goes there as a card instead of being refused (busstate.remote_form). Only the
        # bot says it; an older core rejects the type — and refuses the form, as before
        if ev.get("src") != "telegram" or not isinstance(data.get("on"), bool):
            raise Rejected(GROUP_FACT)
        if data["on"]:
            st["telegram_group"] = True
        else:
            st.pop("telegram_group", None)
        return
    if kind in FACTS:
        return
    if kind == "message.inject":
        return _inject(st, ev, data, actions)
    if kind == "message.routed":
        return _route(st, ev, data, ctx, actions)
    if kind == "message.cancel":
        return _cancel(st, ev, data)
    if kind == "message.weighed":  # the watchdog's (Haiku) "insert now?" estimate, shown to the owner on a queued message
        msg = st["messages"].get(ev.get("entity"))
        if not msg or data.get("channel") == "session":
            raise Rejected(T("core.reducer.no_message", "no such message"))
        if not by_watch_or_owner(data):   # a module or a plugin would show the watchdog's "urgent" on its own message
            raise Rejected(WEIGHED_BY_OUTSIDER)
        msg["urgency"] = data.get("p")
        return
    if kind == "message.received":
        return _received(st, ev, data, actions)
    if kind == "message.reacted":
        return _reacted(st, ev, data)
    if kind == "permission.answered" and data.get("channel") == "plugin" and str(data.get("sender")).startswith("mod:"):
        # a core module process (bin/hub: HUB_MODULE) does not answer for the owner
        raise Rejected(T("core.reducer.answer_module",
                         "the owner answers sessions' questions and permissions, not a module process"))
    if kind.startswith(("session.", "turn.", "dialog.", "permission.")):
        return _session_event(st, ev, data, actions)
    if kind.startswith("chat."):   # the owner's chats with people: the adapter's facts and the sessions' requests (core/chats.py)
        why = chats.fold(st, ev, data, actions)
        if why:
            raise Rejected(why)
        return
    if kind.startswith(("bus.", "core.bus_")):   # the hub bus: the owner's requests and the core's decisions (core/busstate.py)
        why = busstate.fold(st, ev, data, actions)
        if why:
            raise Rejected(why)
        return
    if kind == "journal.scrub_requested":   # hub journal scrub --apply: the live journal is scrubbed via rotation (core/scrub.py)
        if guards.from_owner_or_hub(st, data):
            raise Rejected(T("core.reducer.scrub_who", "the owner, or the hub on their word, starts a journal scrub"))
        actions.append({"do": "scrub_journal"})
        return
    raise Rejected("unknown event type")


# a command with no row for the session's state: the reason says who may first (a state hint names the next command,
# advice only to those who may act). A wake of a live session keeps its hint: "write to it" is for anyone
PERMISSION_FIRST = {"session.remove_requested": "from_hub_owner_or_raiser",
                    "session.stop_requested": "from_hub_owner_or_raiser",
                    "session.interrupt_requested": "from_hub_owner_or_raiser",
                    "session.take_requested": "from_hub_or_owner", "session.give_requested": "from_hub_or_owner"}


def _target(st, sess):
    """The session a command is about, for the guards of who may: who raised it, its backlog, its name and the mini-hub
    of its bus (guards.from_hub_owner_or_raiser, guards.steers)."""
    return {"_raised_by": sess.get("raised_by"), "_backlog": sess.get("backlog"), "_name": sess["name"],
            "_bus": minihub.hub_of(st, sess), "_state": sess["state"]}


# the core fast-forwards a free lead's folder (core/daemon.forward_folders): while git moves its files nothing is typed
# to the sessions of the folder; the hold ends with the result fact, or by itself after FORWARD_HOLD (a result lost in
# a core restart is closed by the daemon at once, this is the last resort)
FORWARD_HOLD = 600
FORWARD_BROKE = T("core.msg.folder_forward_broke", "The core tried to fast-forward your folder {path} ({branch}) to "
                  "{ref}, and git stopped: {why}. Check `git status` there before you rely on the files.")
FORWARDED = T("core.msg.folder_forwarded", "The core fast-forwarded your folder {path} ({branch}) to {ref}: {range}, "
              "commits: {count}. Nothing to do: the files on disk are the fresh ones.")


def forwarding(sess, ts):
    """The core is fast-forwarding the session's folder now: its typing waits (_pump), its spawn too (the daemon)."""
    mark = (sess or {}).get("forwarding")
    return isinstance(mark, dict) and ts - (mark.get("since") or 0) < FORWARD_HOLD


def _folder_forward(st, ev, data, kind):
    """session.folder_forwarding: the core starts git in the folder data.path of sessions data.sessions — each must be
    free (no turn, no background, nothing to type, no /compact running), each gets the hold mark. session.folder_forwarded: the result —
    the marks go; done (ok) — a quiet line ("FYI", it wakes nobody) to each session of the folder, one per folder
    until it is read: a second move before the next message widens the range of the first line; git stopped halfway
    (touched) — a quiet line to check git status."""
    if ev.get("src") != "core":
        raise Rejected(T("core.reducer.forward_who", "only the core fast-forwards a folder"))
    names = data.get("sessions")
    if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
        raise Rejected("no sessions")
    path, ts = data.get("path"), ev["ts"]
    if kind == "session.folder_forwarding":
        taken = minihub.busy(st)
        for name in names:
            sess = st["sessions"].get(name)
            if not sess or sess["state"] in minihub.GONE or not minihub.free(st, sess, taken) \
                    or parking.compacting(sess, ts):
                raise Rejected(T("core.reducer.forward_busy", "{name} is not free — its folder is not fast-forwarded "
                                 "now").format(name=name))
        for name in names:
            st["sessions"][name]["forwarding"] = {"since": ts, "path": path}
        return
    for name in names:
        sess = st["sessions"].get(name)
        if sess and (sess.get("forwarding") or {}).get("path") == path:
            sess.pop("forwarding")
    if data.get("ok") is not True:
        if data.get("touched") is True:   # git was killed mid-way: the files may be half moved
            for name in names:
                sess = st["sessions"].get(name)
                if not sess or sess["state"] in minihub.GONE or executors.dormant(sess) or any(
                        m["session"] == name and m["state"] == "aside" and m.get("forward_broke") == path
                        for m in st["messages"].values()):
                    continue   # one unread line per folder
                new_message(st, f"m{ev['seq']}", name, FORWARD_BROKE.format(
                    path=path, branch=data.get("branch"), ref=data.get("ref"), why=coretext.got(data, "why") or "?"),
                    ts, origin=forms.ORIGIN_CORE, fyi=True, forward_broke=path)
        return
    b = str(data.get("to") or "")
    for name in names:
        sess = st["sessions"].get(name)
        if not sess or sess["state"] in minihub.GONE or executors.dormant(sess):
            continue
        a, count = str(data.get("from") or ""), data.get("count")
        count = count if isinstance(count, int) and not isinstance(count, bool) else None
        line = next((m for m in st["messages"].values() if m["session"] == name and m["state"] == "aside"
                     and (m.get("forwarded") or {}).get("path") == path), None)
        if line:   # not read yet: one line from its first commit to the new one
            a, was = line["forwarded"]["from"], line["forwarded"].get("count")
            count = count + was if count is not None and isinstance(was, int) else None
        text = FORWARDED.format(path=path, branch=data.get("branch"), ref=data.get("ref"), range=f"{a[:8]}..{b[:8]}",
                                count="?" if count is None else count)
        if line:
            line["text"], line["forwarded"] = str(text), {"from": a, "path": path, "count": count}
            l10n = coretext.marks(text)
            line.pop("l10n", None)
            if l10n:
                line["l10n"] = l10n
            continue
        new_message(st, f"m{ev['seq']}", name, text, ts, origin=forms.ORIGIN_CORE, fyi=True,
                    forwarded={"from": a, "path": path, "count": count})


def _session_event(st, ev, data, actions):
    name, kind, ts = ev.get("entity"), ev["type"], ev["ts"]
    sess = st["sessions"].get(name)
    current = sess["state"] if sess else SESSION["initial"]
    guarded = kind in ("session.requested", "permission.requested", "permission.answered", "session.remove_requested",
                       "session.interrupt_requested", "session.stop_requested", "session.take_requested",
                       "session.give_requested", "session.wake_requested")
    if kind == "session.limit_reset":
        return _limit_reset(st, ev, data, sess, actions)
    if kind == "session.resume_requested":
        return _resume_requested(st, ev, data, sess)
    if kind == "session.effort_set":
        # the session's reasoning level (owner decision 2026-10-05: ask for higher effort when medium does not cope):
        # the owner, the hub or the session itself; a free session restarts with the same conversation and this level;
        # `default` is the level from the settings again (as with the model)
        from core.catalog import EFFORTS
        _setter(st, sess, data, name, T("core.reducer.effort_who",
                                        "the owner, the hub or the session itself sets the level"), itself=True)
        level = str(data.get("level") or "")
        if level not in (*EFFORTS, "default"):
            raise Rejected(T("core.reducer.effort_bad", "the level is one of {levels} or default").format(
                levels=", ".join(EFFORTS)))
        if level == "default":
            sess.pop("effort", None)
        else:
            sess["effort"] = level
        _respawn(sess)
        return
    if kind == "session.cwd_set":
        # the working folder moved (owner decision 2026-10-06: agent folders go under the hub state directory): the
        # owner or the hub; a free session restarts in the new folder with the same conversation (--resume looks for
        # it in the new cwd's project folder, and not every claude looks in other folders: before launching, the host
        # puts a copy there, hosts/common.carry_transcript)
        _setter(st, sess, data, name, T("core.reducer.cwd_who", "the owner or the hub changes a session's folder"))
        if sess.get("worktree"):   # it has its own working copy, whose folder the core manages (review)
            raise Rejected(T("core.reducer.cwd_worktree",
                             "the session has its own working copy: its folder is not moved"))
        cwd = str(data.get("cwd") or "")
        if not is_abs_path(cwd):
            raise Rejected(T("core.reducer.cwd_abs", "the folder is an absolute path"))
        if sess.get("cwd") and cwd.rstrip("/\\") == str(sess["cwd"]).rstrip("/\\"):
            return   # the same folder (path text without a trailing slash): nothing, no restart and no move
        before = _modes(st)
        sess["cwd"] = cwd
        if _typed_to(st, sess):   # the hub changes the folder right after the task, before the first turn: without --resume it is lost
            sess["has_history"] = True
            sess["hold_respawn"] = True   # and we leave the process alone until the turn passes (_pump)
        for k in ("minihub_since", "folder_dirty", "folder_error"):   # the mini-hub is re-evaluated for the new folder
            sess.pop(k, None)                                           # (core/minihub.py)
        _modes_follow(st, ev, before)
        _respawn(sess)
        did = _stalled_task(st, sess, ev["ts"]) if data.get("channel") == "session" else None
        if did:   # a session asked (via the hub) and waits: nobody can start a turn in the new folder, so continue the task
            # (the result goes to whoever set it)
            new_message(st, f"m{ev['seq']}", name, CWD_MOVED.format(cwd=cwd), ev["ts"],
                        origin=forms.ORIGIN_CORE, for_delegation=did)
        return
    if kind == "session.model_set":
        # the session's model (owner decision 2026-10-05: the ability to change the model for anyone): like the
        # reasoning level, the owner, the hub or the session itself; `default` is the default model again; a free
        # session restarts at once
        from core.catalog import MODELS, known_model
        _setter(st, sess, data, name, T("core.reducer.model_who",
                                        "the owner, the hub or the session itself sets the model"), itself=True)
        model = str(data.get("model") or "")
        if model != "default" and not known_model(model, catalog.codex_state(st)):
            if catalog.codex_state(st):   # the Codex host: its form (`hub session model` gives real ids itself)
                raise Rejected(MODEL_BAD_CODEX.format(models=", ".join(MODELS)))
            raise Rejected(T("core.reducer.model_bad", "the model is one of {models}, a full id claude-… or default")
                           .format(models=", ".join(MODELS)))
        if model == "default":
            sess.pop("model", None)
        else:
            sess["model"] = model
        _respawn(sess)
        return
    if kind == "session.minihub_set":
        return   # a per-session mini-hub flag exists only in old journals: now it is the hub's parallelism (core/minihub.py)
    if kind == "session.spec_set":
        return _spec_set(st, ev, data, name, sess)
    if kind == "session.solo_set":
        # the "solo" checkbox (15.10): a specialist or direction works by itself, without executors, at any parallelism,
        # an exception for fast work (a secretary, an observer). Set by the owner, the hub and the dev agent (when
        # creating an agent); a free session restarts at once, a busy one after its turn
        who = st["sessions"].get(data.get("sender") or "") or {}
        trainer = data.get("sender") and data.get("sender") == st.get("trainer")
        if not (tools.from_owner(data) or who.get("kind") == "hub" or trainer):
            raise Rejected(T("core.reducer.solo_who",
                             "the \"solo\" checkbox is set by the owner, the hub or agent-dev"))
        if not sess or sess["state"] == "removed":
            raise Rejected("unknown session")
        want = data.get("on") is True
        if want and minihub.why_not(sess):
            raise Rejected(T("core.reducer.solo_kind",
                             "{name}: \"solo\" is for a specialist or a direction — {why}").format(
                name=name, why=minihub.why_not(sess)))
        if not want and not sess.get("solo") and sess.get("solo_auto"):
            raise Rejected(T("core.reducer.solo_auto",
                             "{name} works solo not by the checkbox: {why} — executors would get in each other's way "
                             "there").format(name=name, why=minihub.auto(sess)))
        before = _modes(st)
        if want:
            sess["solo"] = True
            sess.pop("parallel", None)   # "solo" instead of its own number: a session has one of the three (core/minihub.own)
        else:
            sess.pop("solo", None)
        _modes_follow(st, ev, before)
        return
    if kind == "session.parallel_set":
        # a specialist's or direction's own number of executors (owner decision 2026-10-16: individual parallelism
        # settings in each session, in the status bar, configured by click): n >= 1 is the parallel field, "default" is
        # the hub's general parallelism; 0 is "solo", the same solo field as session.solo_set (the page and hub write 0
        # through it: an old core after a rollback knows it). Set by the owner, the hub and the dev agent; not by the
        # session itself (it does not raise its own spending)
        who = st["sessions"].get(data.get("sender") or "") or {}
        trainer = data.get("sender") and data.get("sender") == st.get("trainer")
        if not (tools.from_owner(data) or who.get("kind") == "hub" or trainer):
            raise Rejected(T("core.reducer.parallel_who",
                             "a session's number of executors is set by the owner, the hub or agent-dev"))
        if not sess or sess["state"] == "removed":
            raise Rejected("unknown session")
        if minihub.why_not(sess):
            raise Rejected(T("core.reducer.parallel_kind",
                             "{name}: an own number of executors is for a specialist or a direction — {why}").format(
                name=name, why=minihub.why_not(sess)))
        if minihub.parallel(st) is None:   # a journal from before parallelism: the number would affect nothing, not "accepted"
            raise Rejected(T("core.reducer.parallel_none",
                             "the hub has no parallelism yet (a core from before it): an own number has no effect"))
        n = data.get("n")
        if minihub.own_event(n) is None:   # one rule shared with the page and hub
            raise Rejected(T("core.reducer.parallel_bad", "the number of executors is a whole number from 0 to {max} or "
                                                          "default").format(max=minihub.OWN_MAX))
        before = _modes(st)
        sess.pop("solo", None)
        sess.pop("parallel", None)
        if n == 0:
            sess["solo"] = True
        elif n != "default":
            sess["parallel"] = int(n)
        _modes_follow(st, ev, before)
        return
    if kind in ("session.folder_forwarding", "session.folder_forwarded"):
        return _folder_forward(st, ev, data, kind)
    if kind == "session.folder":
        # a daemon fact about a specialist's or direction's folder (core/minihub.py): not git, or a repo with no
        # commits, means "solo" by itself (executors would get in each other's way); git with a commit and no task in
        # progress means a mini-hub: the switch comes after the current task (15.10). dirty is the number of git
        # status lines, or null (not counted): it does not hold the switch, the mini-hub sees it (16.10); clean is a
        # previous core's field: journals from before the change fold as they lived (a dirty one waits)
        if ev.get("src") != "core":
            raise Rejected(T("core.reducer.folder_who", "the core checks a specialist's folder"))
        if not sess or sess["state"] == "removed":
            raise Rejected("unknown session")
        if data.get("git") == "error":   # git did not answer (timeout, no git): the folder is unchecked, so change nothing
            sess["folder_error"] = str(data.get("why") or _state_text(st, "core.reducer.git_silent"))[:300]
            return
        sess.pop("folder_error", None)
        before = _modes(st)
        auto = minihub.AUTO.get(data.get("git"))
        if auto:
            sess["solo_auto"] = auto
        else:
            sess.pop("solo_auto", None)
        if "dirty" in data:   # null means not counted (awaiting the switch, a sleeping mini-hub): the previous number stays
            n = data["dirty"]
            if isinstance(n, int) and not isinstance(n, bool) and n > 0:
                sess["folder_dirty"] = n
            elif n is not None or sess.get("folder_dirty") is True:   # 0 is clean; True is a previous core's mark
                sess.pop("folder_dirty", None)
            if not sess.get("minihub_since") and minihub.want(st, sess) and minihub.free(st, sess):
                sess["minihub_since"] = ev["seq"]
        elif data.get("clean") is False:
            sess["folder_dirty"] = True
        elif data.get("clean") is True:
            sess.pop("folder_dirty", None)
            if minihub.want(st, sess) and minihub.free(st, sess):
                sess["minihub_since"] = ev["seq"]
        _modes_follow(st, ev, before)
        return
    if kind == "session.compact_requested":
        # compact the conversation via Claude Code's /compact (owner decision 2026-10-05): the owner, the hub or the
        # session itself; the core sends it when the session is free (_pump). text is instructions on what to keep
        # (/compact <text>)
        if not sess or sess["state"] in ("removed", "closed", "failed"):
            raise Rejected(T("core.reducer.compact_state", "only a live or sleeping session can be compacted") +
                           f" ({kind} in {sess['state'] if sess else 'absent'})")
        who = st["sessions"].get(data.get("sender") or "") or {}
        quiet = data.get("channel") == "core" and data.get("why") == "idle"   # the core: a long conversation, the cache still warm
        if not (quiet or tools.from_owner(data) or who.get("kind") == "hub" or data.get("sender") == name):
            raise Rejected(T("core.reducer.compact_who",
                             "the owner, the hub or the session itself asks to compact the conversation"))
        if sess["state"] in SLEEP:
            # asleep (15.10: the hub woke it with a message only for compaction): we do not raise a process for it, the
            # cache is cold anyway; compaction comes first on wake (_compact_on_wake), with the requester's instructions
            sess["compact_wake"] = str(data.get("text") or "").strip()[:2000] or True
            return
        sess["compact"] = str(data.get("text") or "").strip()[:2000] or True
        if quiet:
            sess["idle_compacted"] = True   # not again until the next turn
        return
    if kind == "session.remove_requested" and system.refused(st, name):
        raise Rejected(system.refused(st, name))
    if kind == "session.remove_requested" and current == "idle":
        # a free executor (friction digest 16.10: 16 "remove in idle" refusals to the hub, from executors that handed in
        # their result): no turn, nothing to interrupt, so stop it as stop does and delete. A live non-executor still
        # goes to sleep first; an executor with an open task, question or background is refused too: stop would cut them
        # silently, so that is decided explicitly (hub session stop)
        why = guards.from_hub_owner_or_raiser(st, {**data, **_target(st, sess)})
        if why:
            raise Rejected(why + f" ({kind} in idle)")
        if not minihub.runner(sess):
            why = guards.on_duty(st, sess) or T("core.reducer.remove_free",
                                                "the session is free — a sleeping one is removed: first hub session "
                                                "close {name} (it hands over its work and falls asleep), then "
                                                "remove").format(name=name)
            raise Rejected(why + f" ({kind} in idle)")
        why = guards.closable(st, sess, queued=False, now=ev["ts"])   # the same rule as for sleep: task, question, background
        if why:
            raise Rejected(T("core.reducer.remove_anyway",
                             "{why} — to remove it anyway: hub session stop {name} (cuts it off), then remove").format(
                why=why, name=name) + f" ({kind} in idle)")
        _session_event(st, {**ev, "type": "session.stop_requested", "removing": True}, data, actions)
        return _session_event(st, ev, data, actions)
    if kind == "session.stop_requested":
        if not minihub.runner(sess):
            # the hint names hub session close: only to whoever may put it to sleep
            why = sess and guards.steers(st, {**data, **_target(st, sess)})
            if why:
                raise Rejected(why)
            raise Rejected(T("core.reducer.stop_executor",
                             "only an executor can be stopped at once; the others — hub session close (they hand over "
                             "their work); to cut a turn — hub session interrupt"))
        sess["closing"] = "none"   # next comes retired (a table row): it sleeps like one that handed in, messages do not wake it
        owner_stop = tools.from_owner(data)
        # who stopped it and when, until it is woken: the page shows "stopped by you · 13:02", not "handed in" or "error"
        sess["stopped"] = {"ts": ts, "by": "owner" if owner_stop else str(data.get("sender") or "hub")}
        # what was inserted mid-turn with no turn of its own yet dies with the process: withdrawn, not delivered by the
        # next turn (2026-10-27: "STOP" inserted mid-step was read after the cut and run as a turn). Claude Code gives no
        # sign of reading an insertion inside the turn, so "not read" is "the turn that got it is still running". A line
        # typed into stdin of a free one goes back to its queue, as before (it comes when the executor is woken)
        unread = set(sess.pop("inserted_unread", None) or [])
        for m in st["messages"].values():
            if m["session"] == name and m["state"] == "delivered" and m["id"] in unread:
                _msg_move(m, "_stopped", ts)
                m["reason"] = "stopped by owner" if owner_stop else "session stopped"
        # its tasks become "cut off" (to whoever set them, and to the hub, as a cut), its queue and questions go to
        # whoever raised it (analysis 05.10: a stopped executor held its task "in progress" and its messages in the queue forever)
        open_tasks = [d["id"] for d in st["delegations"].values() if d["session"] == name
                      and d["state"] in ("working", "waiting-owner")]
        # stopped by the hub itself (a command from its session): its "STOPPED" is no news to it (no turn, rides with the
        # next message) — when the task is the hub's own: set by it for itself (for_session is the hub, hub task run).
        # A task of the owner (for) or of another session cut off, or one with no asker at all (raised by the owner in a
        # terminal) — somebody waits for it, the hub has to tell them: a message
        by_hub = data.get("channel") == "session" and (st["sessions"].get(data.get("sender") or "") or {}).get("kind") == "hub"

        def stopped_note(e, text, **kw):
            d = st["delegations"].get(kw.get("task") or "") or {}
            own = not d.get("for") and d.get("for_session") == data.get("sender")
            _notify_hub(st, e, text, fyi=by_hub and own, **kw)
        delegations.on_state(st, name, "failed", ev, {"new_message": new_message, "notify": stopped_note})
        up = sess.get("raised_by") if (st["sessions"].get(sess.get("raised_by") or "") or {}).get("state") \
            not in (None, "removed", "closed", "failed") else _hub(st)
        raiser = st["sessions"].get(up or "") or {}
        if owner_stop and not open_tasks and up == sess.get("raised_by") and raiser.get("kind") != "hub" \
                and raiser.get("state") in LIVE and not ev.get("removing"):
            # no task to cut (it had handed in its result, the turn was its own): whoever raised it, while it works,
            # still learns of the stop (a sleeping one is not woken for it, the hub does not need a turn on what the
            # owner did himself, nor does anyone on a removal)
            new_message(st, f"m{ev['seq']}x9", up, T("core.msg.stopped_idle",
                        "STOPPED: {name} — stopped by the owner; it had no task in progress. What it managed — hub show "
                        "{name}; to wake it — hub session wake {name} {wakers}.").format(
                name=name, wakers=executors.wakers(sess, up)), ts, origin=forms.ORIGIN_CORE)
        for m in st["messages"].values():
            if m["session"] == name and m["state"] == "queued" and up:
                m["session"] = up
                m.pop("handover", None)
                coretext.prepend(m, T("core.msg.was_for_stopped",
                                      "(This was for executor {name} — it was stopped.)").format(name=name) + "\n")
        for ask in sess.pop("asks", None) or []:
            st["owner"]["reminders"].pop(f"q:{name}:{ask['request_id']}", None)
    if kind == "session.wake_requested" and executors.dormant(sess):
        # an executor that handed in is woken by the owner, the hub or whoever raised it
        who = _wake_who(st, sess, data)
        _wake_gate(st, sess, direct=who == "direct")
        if who == "raiser":
            # the raiser is counted: its wake goes through the ceilings of a new request (_raiser_terms). A wake without a
            # job has nothing to wait in the queue with, so it is refused with the reason; with a job it takes the queue
            *_, wait = _raiser_terms(st, ev, data, sess)
            if wait:
                raise Rejected(T("core.reducer.wake_waits",
                                 "{name} cannot be woken now — {wait}. With a task (hub session wake {name} \"<what to "
                                 "do>\") it takes the queue and comes up by itself").format(name=name, wait=wait))
        delegations.trail(st, _last_task(st, name), delegations.BY_CORE,
                          T("core.msg.trail_wake_asked", "{name} woken").format(name=name))
    if kind == "session.parked":
        # only the core parks (hubd: after a restart and after idleness), and only a free session with nothing to do
        # (core/parking); after a restart (why restart) the process is gone, so a compaction in progress is no
        # obstacle, as in daemon.reconcile
        if data.get("channel") != "core" or not sess or not parking.parkable(st, sess, now=ev["ts"],
                                                                             gone=data.get("why") == "restart"):
            raise Rejected(T("core.reducer.park_free", "only a free session with nothing to do can be parked"))
    if kind == "permission.answered" and sess and busstate.card_answered(st, ev, data, sess, actions):
        return   # a card of the hub bus: the owner's decision is the core's (send, accept), not a form
    if kind == "permission.answered" and sess and _answer_ask(st, ev, data, sess):
        return   # an answer to a hub question handed over as a card: the turn ended long ago, so deliver by message
    if kind == "permission.requested" and sess and data.get("tool") == "AskUserQuestion" \
            and (why := busstate.remote_form(st, sess)):
        # nobody sits at this hub's page (`hub bus mode remote`): a form would wait for no one; refused at once with no
        # card, saying whom to ask in the thread instead (spec d2505378, part D, remote). The page and Telegram judge
        # by the same rule
        actions.append({"do": "respond", "session": name, "request_id": data.get("request_id"), "response": {
            "behavior": "deny", "message": why}})
        return
    if kind == "permission.requested" and sess and minihub.asks_blocked(st, name, data):
        # the bus (owner decision 2026-10-15): a mini-hub's executor asks the owner no questions, refused at once with no
        # card (the host also blocks the form; this covers a process raised without the ban). The page and Telegram
        # judge by the same rule
        actions.append({"do": "respond", "session": name, "request_id": data.get("request_id"), "response": {
            "behavior": "deny", "message": minihub.ASK_REFUSED.format(boss=minihub.boss(st, sess))}})
        return
    if kind == "permission.requested" and data.get("auto") == "allow" and ev.get("src") == "host" and sess:
        # the core allows a confirmation request by itself, as enabled in the settings (permissions.auto_allow; the
        # host marked the request, and does not mark form questions; a mark not from the host takes the usual path to
        # the owner). No state check: a live process sends the request, and the page, Telegram and the watchdog judge
        # by the same two signs (src and auto), so there is one decision
        # the input in the journal is masked (masked); Claude Code hands hubd the input as is (Hub.give_input)
        actions.append({"do": "respond", "session": name, "request_id": data.get("request_id"),
                        "response": {"behavior": "allow", "updatedInput": dict(data.get("input") or {})},
                        **({"masked": True} if data.get("masked") else {})})
        sess.pop("attention", None)   # it keeps working, so the watchdog's "waits for you" mark is not needed (as usual)
        return
    gdata = {**data, "_kind": sess.get("kind"), **_target(st, sess)} if guarded and sess else data
    to, todo, why = decide(SESSION, current, kind, checker(st, gdata) if guarded else None, name=_real(name))
    first = PERMISSION_FIRST.get(kind)
    if to is None and sess and first:
        # there is no row for this state; permissions still come first: a "hub session stop" or "wake it first" hint to
        # someone whose command would be refused is a wasted round (review)
        why = getattr(guards, first)(st, gdata) or why
    if to is None:
        raise Rejected(_named(why, name))
    if "ask_hub" in todo:
        return _ask_hub_for_session(st, ev, data)
    if kind == "session.requested" and guards.raiser_revives(st, gdata):
        data = _raiser_revives(st, ev, data, sess)   # the raiser brings its own executor back (guards.revives)

    if kind == "session.requested" and current in SLEEP:
        pass  # wake: the same sessionId, the same conversation, the same set
    elif kind == "session.requested":
        old = sess or {}
        sess = st["sessions"][name] = {
            "name": name, "state": current, "cwd": data.get("cwd"), "session_id": data.get("session_id"),
            "mode": data.get("mode"), "ever_started": False, "restarts": 0, "pid": None, "pane": None,
            "pending": [], "since": ts, "kind": data.get("kind") or "work", "project": data.get("project") or "all",
            "tools": list(data.get("tools") or []), "requests": [], "dirty": False, "closing": CLOSING["initial"],
            # an agent is a permanent professional (about is what it does), a worker is for a task (30.09)
            "role": "agent" if data.get("role") == "agent" and data.get("kind") != "hub" else "worker",
            "about": data.get("about"), "icon": data.get("icon"),
            # a new session lives by the current rules anyway; one that returned with its own conversation does not:
            # without --system-prompt-snapshot, claude on --resume takes the first request's prompt, with the hub's old rules
            "news_seen": old["news_seen"] if data.get("revive") and "news_seen" in old
            and old.get("session_id") == data.get("session_id") else news.latest(st),
        }
        if data.get("profile"):   # a system agent came back without a conversation, so its CLAUDE.md is the old one (core/system.py)
            sess["profile"] = str(data["profile"])
        if (data.get("liaison") or data.get("revive") and old.get("liaison")) and name == liaison.NAME \
                and (data.get("channel") == "core" or old.get("liaison")):
            # the bus's liaison (core/liaison.py): made by the core only; its profile is the core's, not its own file
            sess.update(liaison=True, model=liaison.MODEL)
        if name in (st.get("pending_backlogs") or {}):   # the hub already started a backlog for this direction (task.backlog)
            wait = st["pending_backlogs"].pop(name)
            sess["backlog"] = wait["slug"] if isinstance(wait, dict) else name   # old journals: the name is the direction
        takes = st["messages"].get(data.get("takes") or "") if data.get("executor") else None
        if takes and takes.get("handover") == name:
            # raised by the core for a busy specialist: its message (and the task, if it is a task) goes to this executor
            takes.pop("handover")
            takes["session"] = name
            sess["auto"] = True   # raised by the core: counts toward "one auto-executor at a time"
            coretext.prepend(takes, executors.TAKES.format(agent=data.get("raised_by")))
            d = next((x for x in st["delegations"].values() if x.get("message") == takes["id"]), None)
            if d:
                d["session"] = name
            new_message(st, f"m{ev['seq']}x6", data.get("raised_by"),
                        T("core.msg.taken_while_busy",
                          "While you were busy, {id} (\"{text}\") was taken by your executor {name}; the result is with "
                          "it (hub show {name}).").format(id=takes["id"], name=name,
                                                          text=str(takes["text"]).split("\n\n", 1)[-1][:120]),
                        ts, origin=forms.ORIGIN_CORE)
            note = executors.source_note(data.get("worktree"))   # its copy lacks the raiser's folder: where to find it
            if note:   # after the message and its quote above: the marks of its text keep their places
                takes["text"] += "\n\n" + note
        if data.get("executor"):  # an executor for a task: the profile and set of whoever raised it (spec, release 2)
            raiser = st["sessions"].get(data.get("raised_by") or "") or {}
            sess.update(role="executor", raised_by=data.get("raised_by"), worktree=data.get("worktree"),
                        about=data.get("about"), lessons={"text": "", "unclear": False})   # lessons from the first turn
            if data.get("here"):   # right in a ready copy (--here): the copy is not its own, so no cleanup and no second executor in it
                sess["here"] = True
            sess.update(delegations.pinned(data, catalog.codex_state(st)))   # hub task run --model/--effort: the first launch uses them too
            if raiser:
                # the agent may have a list it does not pass to its executors (drop_tools); what the task names with
                # --tools stays whole
                named = (st.get("runs") or {}).get(name, {}).get("tools") or []
                drop = (st["owner"].get("drop_tools") or {}).get(modelrule.agent_of(st, raiser["name"])) or []
                sess.update(mode=raiser.get("mode"), project=raiser.get("project") or "all",
                            tools=[t for t in raiser.get("tools") or [] if t not in drop or t in named],
                            icon=raiser.get("icon"))
                if raiser.get("own_icon"):   # its own icon is copied by value at birth: a later change on the raiser does not affect it
                    sess["own_icon"] = raiser["own_icon"]
                if raiser.get("role") == "agent":
                    sess["profile"] = raiser.get("profile") or raiser["session_id"]
            run = (st.get("runs") or {}).pop(name, None) or {}
            if run.get("root"):
                sess["task_root"] = run["root"]   # its task (delegation.created follows) is in the same chain
            if run.get("via_schedule"):
                sess["task_sched"] = True   # asked in a turn started by a schedule: its task is not a live word either (core/budget)
            if run.get("spec"):
                sess["task_spec"] = run["spec"]   # raised with --spec: its task carries the spec (delegations.created)
            if run.get("parent"):
                sess["task_parent"] = run["parent"]   # and a subtask of the one it was asked for
                if run.get("minihub"):
                    sess["task_minihub"] = True   # mini-hub: its task waits for this subtask (core/minihub.waiting_on)
            bus = minihub.hub_of(st, raiser) or (data.get("raised_by") if run.get("minihub") else None)
            if bus:   # a mini-hub's executor (and its subtask): the owner is reachable only via the mini-hub (core/minihub.boss)
                sess["bus"] = bus
            # what the daemon found in the executor's folder (.mcp.json): the launch passes the file, the session has it
            repo = data.get("repo_tools") if ev.get("src") == "core" and isinstance(data.get("repo_tools"), list) else []
            for tool in run.get("tools") or []:   # hub task run --tools: the first launch uses them too
                if tool in sess["tools"]:   # inherited from whoever raised it: do not ask the owner (17.10)
                    continue
                if tool in repo:   # from its repo: closed as "had", no card and no attach (tools._close_had)
                    sess["requests"].append({"tool": tool, "why": _state_text(st, "core.reducer.why_task_start"),
                                             "state": "granted", "known": True, "since": ts, "had": ev["seq"],
                                             "repo": True})
                    continue
                if tool in run.get("ask") or []:
                    hold = owner.review_hold(st, ts, [_state_text(st, "core.reducer.why_task_start")], [data.get("raised_by"), name])
                    sess["requests"].append({"tool": tool, "why": _state_text(st, "core.reducer.why_task_start"),
                                             "state": "needs-owner",
                                             "known": True, "since": ts, **({"held_until": hold} if hold else {})})
                elif tool in st["catalog"] and tools.allowed_for(st["catalog"][tool], name):
                    tools._grant(sess, None, tool, ts, st)
            sess["dirty"] = False
            sess.pop("new_tools", None)
        if data.get("revive") and old.get("session_id") == data.get("session_id"):
            # bring back a deleted session with its conversation (02.10): the same set and --resume, not a new session
            for k in ("has_history", "tools", "role", "about", "icon", "own_icon", "profile", "mode", "project",
                      "context", "solo", "parallel", "bus", "here", "owner_talks", "raised_by", "board",
                      "handed_in", "spec", "refused"):
                if old.get(k) is not None:
                    sess[k] = old[k]
            top = executors.copy_of({**sess, "worktree": data.get("worktree") or old.get("worktree")})
            who = executors.holder(st, top, name) if top else None
            if who:   # its copy (its own or a ready one, --here) was given to another, who works in it
                raise Rejected(T("core.reducer.revive_copy_taken",
                                 "{name} cannot be brought back — its copy {top} is taken by {who}").format(
                    name=name, top=top, who=who))
            _compact_on_wake(st, sess)
            if sess.get("role") == "fork":  # a fork of an agent from old versions, brought back deliberately: a worker
                sess["role"] = "worker"
            if isinstance(data.get("worktree"), dict):  # the working copy was restored (hub session revive)
                sess["worktree"] = {k: data["worktree"][k] for k in WORKTREE_KEYS if k in data["worktree"]}
        elif layout.keeps_place(st, sess):   # the hub and a system agent are the same session: the icon and place go with it
            if old.get("own_icon"):
                sess["own_icon"] = old["own_icon"]
            for k in ("solo", "parallel", "spec"):   # and "solo" or its own number of executors, specs: the owner's choice
                if old.get(k):
                    sess[k] = old[k]
        else:   # any other session under the old name (not revive) is new: a clean slate in the layout
            layout.forget(st, name, ev["seq"])
        if not data.get("revive"):   # a direction with a task in the core's copy right away: a mini-hub from the first launch
            _born(st, sess, ev["seq"])
        identity.remember(st, name, {"session_id": data.get("session_id")})
    if kind == "session.started":
        # the raise that cut a turn the filter stopped (core/refusals.py): the note before the session_id below changes
        refusals.started(st, sess, data, ts, new_message, f"m{ev['seq']}x4")
        if data.get("session_id") and data["session_id"] != sess.get("session_id"):
            for k in ("cut_eyes", "resume", "resume_due"):   # the conversation changed (/clear): nothing to continue in it
                sess.pop(k, None)
            if sess.get("role") == "agent" and sess.get("session_id") and not sess.get("profile"):
                # its CLAUDE.md is keyed by the first id (<state>/agents/<id>): a new conversation keeps the file (02.11)
                sess["profile"] = sess["session_id"]
        sess["ever_started"] = True
        sess["restarts"] = 0
        tools.close_held(st, ev, [sess])   # a dangling request for what is already in the set (the catalog is written only on change)
        sess.pop("compacting", None)   # release+spawn and reload write no session.exited: new process, compaction interrupted
        if data.get("source") in ("startup", "resume") and st.get("host") != "tmux":   # tmux sends no session.info
            sess["mcp_init"] = ts   # a new process: until system init it does not read its input (parking.mcp_init)
        else:   # /clear inside the same process: it already reads its input
            sess.pop("mcp_init", None)
        asked = sess.pop("compact_wake", None)   # compaction was requested while it slept, however it was woken
        if asked and data.get("session_id") in (None, sess.get("session_id")):   # the same conversation: there is something to compact
            sess["compact"] = asked
        for field in ("session_id", "pid", "pane"):
            if data.get(field) is not None:
                sess[field] = data[field]
        identity.remember(st, name, data)
        # "awaiting login" is from the previous start; a fresh MCP status arrives at the start of the turn (05.10: it hung after a restart)
        (sess.get("info") or {}).pop("mcp_auth", None)
        if data.get("source") not in ("clear", "compact"):   # those come inside the same process
            mcpwatch.login_gone(st, ev, name)   # a new process: a sign-in begun in the old one is gone with it
        if data.get("source") == "clear":   # a new conversation: the old one's length is not this one's (parking, /clear)
            sess.pop("context", None)
        cut = sess.pop("cut_turn", None)
        resumes = sess.pop("resume_due", None)   # a turn was cut, not just background work
        limit = sess.get("limit_cut")
        # a limit cut the turn and the session came up on another account (Claude accounts): continue
        limit = limit if limit and (limit.get("account"), limit.get("gen")) != (sess.get("account"),
                                                                               sess.get("account_gen")) else None
        if cut:
            # a restart cut the turn (02.10: the result of a background review arrived in the cut turn and was lost, work stood still)
            bg = cut.get("background") if isinstance(cut, dict) else None
            own = cut.get("asked") if isinstance(cut, dict) else None
            if own:   # it asked for a reload mid-task itself: the turn ended on its own, nobody is left to wake it
                text = RELOAD_ASKED + (BG_CUT.format(bg="; ".join(bg)) if bg else "")
            elif isinstance(cut, dict) and cut.get("respawn"):   # one session was restarted, not the hub
                plain = (CUT_WHY["codex"] if cut.get("account") == "codex" else CUT_WHY["claude"]) \
                    if cut.get("account") else CUT_WHY["set"]
                why = {True: CUT_WHY["minihub_on"], False: CUT_WHY["minihub_off"]}.get(cut.get("minihub"), plain)
                text = RESPAWN_CUT.format(why=why, bg="; ".join(bg))
            else:   # after a stop with no clean shutdown (the machine or hubd crashed) nobody restarted the hub: the text says so
                text = (CUT_CRASH if isinstance(cut, dict) and cut.get("crash") else CUT_TURN) \
                    + (BG_CUT_TOO.format(bg="; ".join(bg)) if bg else "")
            if limit and bg:   # the background was cut with the turn: one message, continue the turn and raise the needed background
                text = LIMIT_CUT + BG_CUT_LIMIT.format(bg="; ".join(bg))
            # the continuation is in the cut turn's chain (core/chain.py): a reply to its message goes where it was asked
            again = {"root": sess["turn_root"]} if sess.get("turn_root") else {}
            cut = new_message(st, f"m{ev['seq']}x3", name, text, ts, origin=forms.ORIGIN_CORE, **again)
            if isinstance(resumes, dict) and not limit:   # an assignment (its result is the reply) or an owner message (re)
                cut.update(resumes)
            elif isinstance(own, dict) and not limit:   # the same matter it asked for
                cut.update(own)
            if own:
                sess["reload_went"] = True
        if limit:
            sess.pop("limit_cut")
            go = cut or new_message(st, f"m{ev['seq']}x5", name,
                                    LIMIT_CUT_CODEX_POOL if limit.get("engine") == "codex" else LIMIT_CUT, ts,
                                    origin=forms.ORIGIN_CORE,
                                    **({"root": sess["turn_root"]} if sess.get("turn_root") else {}))
            go.update({k: limit[k] for k in ("re", "for_delegation") if limit.get(k)})
            cut, resumes = go, True
        if cut and resumes:   # a continuation of a cut turn: taking it returns to the turn what the cut turn had taken (_take)
            sess["resume"] = cut["id"]
        if cut:
            sess.pop("net_cut", None)   # the restart's or the limit's continuation carries on the same turn: not a second one
    if kind in ("turn.started", "permission.requested"):
        sess.pop("attention", None)  # it works again or asked via a form: the watchdog's "waits for you" mark is no longer needed
    if kind == "turn.started":
        sess.pop("mcp_init", None)
        sess.pop("limit_cut", None)  # a new turn began: do not continue one cut by the limit
        sess.pop("net_cut", None)    # nor one cut by the network
        # the chain and task of a `hub task run` request apply only to a task set together with the launch; if there
        # was none (a refusal, an executor for a busy specialist), they do not stick to someone else's task later
        sess.pop("task_root", None)
        sess.pop("task_parent", None)
        sess.pop("task_sched", None)
        sess.pop("task_spec", None)
        sess.pop("idle_compacted", None)   # the conversation grows again: it can be compacted again
        refusals.turn(sess, data)   # a turn on a later message: a cut would take its work too (core/refusals.py)
        old_marker = sess.get("turn_marker")
        sess["turn_marker"] = data.get("marker")   # which message the turn is on: the continuation after a limit (_continues)
        # a new turn: the session was free, or a letter starts it with no Stop between. A message inserted mid-turn
        # (`--now`, a watchdog) or a notice without a marker (a background helper ended) is the same turn, and it changes
        # nothing of what the turn began with: otherwise a continuation after a limit answers the notice, not the words
        marker_msg = st["messages"].get(data.get("marker") or "") or {}
        free = current not in chain.BUSY
        fresh = free or bool(data.get("marker") and not marker_msg.get("injected"))
        # what the turn began with: a continuation after a limit or a restart is of it, and so is the fold (_head,
        # _merged_into); the state of an older core has no turn_head, its turn_marker before this prompt is what the
        # turn began with
        head = sess.get("turn_head", old_marker)
        sess["turn_head"] = data.get("marker") if fresh or not head else head
        # and what else this turn took: a hook refused the prompt, so unread; an insertion mid-turn adds to it
        before = [] if fresh else list(sess.get("turn_also") or [])
        # of this prompt alone: what a refusal of it fails (not what an earlier prompt of the turn carried, the head
        # included: the model read it; a later prompt may repeat it)
        prompt_also = [m for m in (data.get("also") or []) if m != sess["turn_head"] and m not in before]
        if prompt_also:
            sess["prompt_also"] = prompt_also
        else:
            sess.pop("prompt_also", None)
        also = list(dict.fromkeys(before + prompt_also))
        if also:
            sess["turn_also"] = also
        else:
            sess.pop("turn_also", None)
        # a turn on an owner message (direct conversation, the hub) has a result addressed to the owner (owner_out);
        # the marker is still in messages now and may be pruned by the time of the result
        took = [st["messages"].get(m) or {} for m in [data.get("marker")] + list(data.get("also") or []) if m]
        if any(m and not forms.is_core(m) for m in took):   # a new matter: its reload will be continued again
            sess.pop("reload_went", None)
        # an insertion mid-turn (watchdog, --now) or a notice without a marker is the same turn: it does not remove
        # the owner mark; a message that was not inserted starts a new chain (as turn_root below)
        sess["turn_owner"] = any(m.get("from_owner") for m in took) or bool(not fresh and sess.get("turn_owner"))
        # a turn on another hub's answer to a request this session wrote in a turn of the owner's (busstate.owner_turn):
        # its result is an answer to him, as an executor's to his task (owner decision 18.11: "Answered you"); words of
        # another hub, not his — turn_owner, turn_his and the owner-word gates stay as they are
        if any((m.get("hubbus") or {}).get("in_owner_turn") for m in took) or (not fresh and sess.get("turn_bus_owner")):
            sess["turn_bus_owner"] = True
        else:
            sess.pop("turn_bus_owner", None)   # no key on ordinary turns: the state stays as it was
        # the turn is on the owner's own words: his message, his answer to a card, words passed on (`--for`); a notice
        # inserted mid-turn does not take it away (_merged_into)
        sess["turn_his"] = _his_words(took) or bool(not fresh and sess.get("turn_his"))
        # a turn only on the core's notes about another session's hand-in (lessons): its result is not one for the
        # owner (turn.result); anything else taken in, an insertion mid-turn too, makes it an ordinary turn. A new turn
        # without a marker (Claude Code woke it: a background job of some earlier turn ended) is ordinary: its result may
        # be the owner's
        if took and all(m and delegations._service(m) for m in took) and (fresh or sess.get("turn_service")):
            sess["turn_service"] = True
        elif took or fresh:
            sess.pop("turn_service", None)   # no key on ordinary turns: the state (and a snapshot of it) stays as it was
        if data.get("marker") or data.get("also"):   # a turn without a marker (background ended): the addressee of the previous turn
            sess["owner_chain"] = sess["turn_owner"] or bool(sess.get("turn_bus_owner"))
        if free:
            _turn_begins(st, sess)   # eyes badges count from the last start of a turn
            sess.pop("recap", None)   # the watchdog's "what it is working on" line is about the previous turn; no new one yet
            if sess.get("gateway"):   # turn start: did the account change during the turn (turn.result); Stop will change since
                sess["turn_at"] = ev["ts"]
        if fresh:
            # which turn is running: whether the gist (session.gist) and the result to the owner (out_turn) come from one
            sess["turn_seq"] = ev["seq"]
        if data.get("marker") or data.get("also"):
            # whose message started this turn's chain (core/chain.py): the result goes where the owner asked
            root = chain.of_turn(st, data.get("marker"), data.get("also"))
            # something inserted mid-turn (watchdog, --now) with no start of its own is the same turn: its start
            # stays (13.10: a message from one session in the middle of another's turn, on the owner's words, took the
            # result away from the owner); something read after the turn's end is already a new turn with its own chain
            if root or fresh:
                sess["turn_root"] = root
                # and whether the chain passed through a schedule firing (limit economy: not the owner's live word),
                # only with mini-hubs; without them there is no economy rule (core/budget.py)
                if budget.mode(st) and chain.of_turn_scheduled(st, data.get("marker"), data.get("also")):
                    sess["turn_sched"] = True
                else:
                    sess.pop("turn_sched", None)
        # a turn without a mark: Claude Code woke the session itself (background ended): the same chain as the previous turn
        # the conversation is already on disk: Claude Code (stream-json) writes it on input, before the model replies
        sess["has_history"] = True
        sess["turn_tools"] = 0
        # Claude Code wakes the session when its background command or agent finished (task-notification): no longer in background
        # Only such a turn counts: a task number quoted in a human's words removes nothing. The hook names the tasks whose
        # notice carries a status (notice.ended): a monitor's event has none and the monitor keeps running (11.11: an
        # executor watching its runs fell asleep); a turn from an older hook — every id in the head, as before
        head = data.get("prompt_head") or ""
        notice = data.get("notice")
        listed = notice.get("ended") if isinstance(notice, dict) else None
        for tid in ([t for t in listed if isinstance(t, str)] if isinstance(listed, list) else
                    re.findall(r"<task-id>(\w+)</task-id>", head) if head.lstrip().startswith("<task-notification>") else []):
            if sess.setdefault("background", {}).pop(tid, None):
                _bg_gone(sess, [tid], ts)
    if kind == "session.exited":
        sess.pop("compacting", None)
        sess.pop("mcp_init", None)
        refusals.exited(sess)   # the raise that carried the cut died before its start: no second cut (core/refusals.py)
        bg = [t.get("title") or t.get("id") for t in (sess.get("background") or {}).values()]
        if current in ("busy", "dialog") or bg:
            # the turn or background work was cut with the process: after the launch, tell it to continue (02.10,
            # restart --now: a free session with an agent in the background stayed silent)
            sess["cut_turn"] = {"ts": ts, "background": bg, **({"crash": True} if data.get("crash") else {})}
        _bg_gone(sess, sess.get("background") or {}, ts)
        sess["background"] = {}  # the process left, and so did its background commands and agents
        if current in chain.BUSY:   # the turn is cut: a continuation turn after the launch decides (a flag on the
            # session: the cut record is overwritten by both a second cut and a restart for tools); the continuation is
            # of the same matter, as after a limit
            sess["resume_due"] = _continues(st, _head(sess), sess.get("delegation")) or True
            _cut(st, sess)
    if kind == "turn.started" and data.get("marker"):
        # a turn with an mN mark is a fact of delivery of mN to this session, in whatever state the queue held it. Only
        # its own: another message's mark in the turn text (a background notification, an insertion) does not count as
        # delivery, otherwise the turn took the message away from its addressee (analysis 04.10)
        msg = st["messages"].get(data["marker"])
        late = msg and msg["state"] == "failed"
        if _awaits_turn(msg, name):
            _msg_move(msg, "_delivered", ts)
            msg["delivered_at"] = ts
            if late:
                msg.pop("reason", None)   # "not confirmed" no longer applies to it
    if kind == "turn.started" and sess.get("inserted_unread"):
        read = {data.get("marker"), *(data.get("also") or [])}
        sess["inserted_unread"] = [m for m in sess["inserted_unread"] if m not in read]
        if not sess["inserted_unread"]:
            del sess["inserted_unread"]
    if kind == "turn.started":
        # Claude Code took other lines from stdin in the same turn (05.10: for an interrupted turn, what was inserted
        # and typed afterward arrived with the first one's mark): those already typed to this session are delivered,
        # and the queue does not wait behind them
        for mid in data.get("also") or []:
            m = st["messages"].get(mid)
            if m and m["session"] == name and m["state"] == "typing":
                _msg_move(m, "_delivered", ts)
                m["delivered_at"] = ts
        # what this turn took (the mark, also; an insertion goes to the turn that read it): the eyes badge; a new turn
        # started by the owner's word means the session's lines before it are answered (folded into "Brief")
        took = [_take(st, sess, m) for m in map(st["messages"].get, (data.get("marker"), *(data.get("also") or [])))
                if m and m["session"] == name and m["state"] == "delivered"]
        if any(took) and fresh:
            sess["taken"] = ev["seq"]
        delegations.on_turn(st, name, data.get("marker"), data.get("prompt_head"), fresh, ev["ts"],
                            also=data.get("also"))

    if "defer_question" in todo:
        # the owner is away: the question is saved for him, the session continues (ask once and move on)
        text = owner.question_text({"tool": data.get("tool"), "input": data.get("input")})
        st["owner"]["deferred"].append({"session": name, "text": text, "ts": ts})
        sess.setdefault("turn_asks", []).append(DEFERRED)   # this turn's question stays open until he returns (_settle)
        msg = _away_since(st) + ASK_DEFERRED
        actions.append({"do": "respond", "session": name, "request_id": data.get("request_id"),
                        "response": {"behavior": "deny", "message": msg}})
    elif "detach_question" in todo:
        # the hub asked the owner via a form: the question is a card (page, Telegram, reminders) and we do not hold the
        # turn, otherwise the form holds the hub's queue for hours (03.10 23:43 to 04.10 05:56) and sessions cannot reach it
        qid = f"q{ev['seq']}"
        sess.setdefault("turn_asks", []).append(qid)   # this turn's question: open at the result, no thumbs-up (_settle)
        did = sess.get("delegation")   # a question on an assignment: while open, the assignment "waits for the owner"
        delegations.trail(st, did, name, T("core.msg.trail_question", "question to the owner {qid}: {question}").format(
            qid=qid, question=owner.question_text({"tool": data.get("tool"), "input": data.get("input")})))
        texts = owner.ask_texts({"tool": data.get("tool"), "input": data.get("input")})
        # away: the card waits for his return, with no push or Telegram, and does not expire; a 🚨 one reaches him at once
        # (owner 17.11, q2551190: "push at once")
        away = st["owner"]["state"] == "away" and not owner.urgent(texts)
        # non-working hours (hub.json quiet_review): the card is on the page, Telegram and push get it in the working morning
        hold = owner.review_hold(st, ts, texts, [name])
        sess.setdefault("asks", []).append({"id": qid, "request_id": data.get("request_id"), "tool": data.get("tool"),
                                            "input": data.get("input") or {}, "interactive": True, "since": ts,
                                            **({"delegation": did} if did else {}), **({"away": True} if away else {}),
                                            **({"held_until": hold} if hold else {}),
                                            # the economy card: removed when the budget gate is switched off
                                            **({"budget_card": True} if budget.card_of(st, sess) else {})})
        if liaison.is_liaison(st, name):   # its form on one incoming item: that item waits for the owner (no 30-min mark)
            busstate.liaison_asked(st, sess, data.get("request_id"), ts)
        hub_may = away and _owners_own(st, sess, sess["asks"][-1]) is None   # the hub may answer it for him
        if hub_may and st.get("host") == "tmux":
            _away_card(st, ev, sess, sess["asks"][-1])   # tmux sends no turn results (_settle): to the hub at once
        # the same question is already open on another session: tell the hub to remove the duplicate (07.10, two questions about one rule)
        text = owner.question_key({"tool": data.get("tool"), "input": data.get("input")})
        same = [(x["name"], q["id"]) for x in st["sessions"].values() if x["name"] != name for q in owner.open_asks(x, ts)
                if owner.question_key(q) == text]   # a stale card is no longer the one the owner answers (owner.ASK_TTL)
        if same:
            other, oq = same[0]
            _notify_hub(st, {**ev, "seq": f"{ev['seq']}d"},
                        T("core.msg.question_duplicate",
                          "Question {qid} from {name} repeats the open {oq} from {other}: \"{text}\". One is enough for "
                          "the owner — remove the duplicate: hub question withdraw {qid} \"same as {oq}\" (and tell "
                          "{name} that the answer comes through {other}).").format(qid=qid, name=name, oq=oq,
                                                                                    other=other, text=text[:120]))
        d = st["delegations"].get(did or "")
        if d and decide(DELEGATION, d["state"], "_asks_owner")[0] is not None:
            # "waits for the owner" applies only to this assignment, not all the session's assignments (04.10: a question
            # on one task made another "wait for you" too, though the owner had already answered that one's question)
            delegations.move(st, d, "_asks_owner", ev, {"new_message": new_message,
                                                        "notify": lambda e, text, **kw: _notify_hub(st, e, text, **kw)})
        actions.append({"do": "respond", "session": name, "request_id": data.get("request_id"), "response": {
            "behavior": "deny", "message": ASK_AWAY.format(away=_away_since(st), qid=qid)
            + (ASK_AWAY_HUB.format(qid=qid) if hub_may else "") if away
            else ASK_DETACHED.format(qid=qid, hours=owner.ASK_TTL // 3600)
            + (ASK_REVIEW.format(at=_review_at(st, hold), qid=qid) if hold else "")}})
    elif kind == "permission.requested":
        told = _tell_raiser_permit(st, ev, sess, data)
        sess["pending"].append({"request_id": data.get("request_id"), "tool": data.get("tool"),
                                "input": data.get("input") or {}, "interactive": bool(data.get("interactive")),
                                "since": ts, **({"why": data["why"]} if data.get("why") else {}),
                                **({"told": told} if told else {}),
                                **({"masked": True} if data.get("masked") else {})})
    if kind == "permission.answered":
        req = _take_request(sess, data.get("request_id"))
        _withdraw_permit_note(st, req)
        _answered(sess, ev, data)
        if data.get("behavior") == "allow":
            updated = dict(req["input"])
            if data.get("answers"):
                updated["answers"] = data["answers"]
            response = {"behavior": "allow", "updatedInput": updated}
        else:
            response = {"behavior": "deny", "message": data.get("message") or DENIED_BY_OWNER}
        actions.append({"do": "respond", "session": name, "request_id": req["request_id"], "response": response,
                        **({"masked": True} if req.get("masked") else {})})
        if sess["pending"]:
            to = "dialog"  # there are still questions: the session still waits for a human
    if to != "dialog" and to != "busy":
        for req in sess["pending"]:
            _withdraw_permit_note(st, req)
        sess["pending"] = []

    if "respawn" in todo:
        sess["restarts"] += 1
        if sess["restarts"] > MAX_RESTARTS and sess["kind"] != "hub":
            to, todo = "failed", [a for a in todo if a == "kill"]   # do not raise, but kill a hung process
    if kind in ("session.started", "session.exited") or (current in chain.BUSY and to not in chain.BUSY):
        # a new process or none: what the old one had in its stdin is gone with it; the turn ended (idle, or the process
        # let go: the owner took it into his terminal, it ended itself): Claude Code read the insertion inside it (an
        # insertion does not always get a turn with its mark), starts the next turn on it, or it went with the process
        sess.pop("inserted_unread", None)
    if to in LIVE:
        sess.pop("stopped", None)   # woken: the "stopped by you" mark is history now
    if sess["state"] != to:
        sess["since"] = ts
        delegations.on_state(st, name, to, ev, {"new_message": new_message,
                                                "notify": lambda e, text, **kw: _notify_hub(st, e, text, **kw)})
        if sess.get("closing", "none") != "none" and to in ("starting", "held", "failed", *SLEEP, "closed"):
            sess["closing"] = decide(CLOSING, sess["closing"], "_cancel")[0]  # it left by itself: the handover is cancelled
    if to == "removed" and current not in ("removed", "retired") and minihub.runner(sess):
        # the executor left: tell whoever raised it, lessons from its result go to CLAUDE.md (spec section 4). Removed
        # from asleep or parked (the term, or by hand), it already fell asleep after its work: the note rides with the
        # raiser's next message instead of waking it (a cleanup of many old executors woke it once per executor)
        _lessons_note(st, ev, sess, late=current in ("asleep", "parked"), gone=True)
    if to in ("closed", "failed", "removed") and current not in ("closed", "failed", "removed") and minihub.runner(sess):
        # and a crashed or closed one: its checks would otherwise fire at the hub as "the session is gone, decide who gets it"
        up = identity.address(st, sess["raised_by"]) if sess.get("raised_by") else None   # a renamed one by its new name
        schedule.hand_waits(st, name, up if up and st["sessions"][up]["state"] not in ("removed", "closed", "failed")
                            else None, ts)
    sess["state"] = to
    if to in ("closed", "failed", "removed") or (current in chain.BUSY and to in (*SLEEP, "held")):
        _gone(st, sess)
    if to not in LIVE:
        # no process (parked, asleep, crashed, with the owner): the next launch is with the new plugin version anyway;
        # the "will restart with them" mark in hub status is not needed for a sleeping session
        sess.pop("plugins_stale", None)
        mcpwatch.login_gone(st, ev, sess["name"])   # and a sign-in through its process
        if sess.get("background"):
            _bg_gone(sess, sess["background"], ts)
            sess["background"] = {}   # and the background left with the process (a stopped executor, hub take)
    if to == "removed" and current != "removed":
        schedule.drop_milestones(st, name, ev, {"new_message": new_message, "hub": _hub})   # its progress jobs report nobody's work
    if to == "removed":
        sess.pop("kit", None)   # a removed session is not shown: its set would only weigh on the state
        sess.pop("bg_gone", None)   # the watchdog will no longer send a removed session's background output
        mcpwatch.removed(st, sess)   # a removed session no longer holds a server "waiting for a login"

    for act in todo:
        if act in ("spawn", "respawn"):
            actions.append(spawn_action(sess, delay=act == "respawn", st=st))
        elif act in ("kill", "release", "interrupt"):
            actions.append({"do": act, "session": name})
        elif act == "release_now":   # a stop: the host ends the process at once and waits for its exit in the background
            actions.append({"do": "release", "session": name, "now": True})
    if _for_good(sess, current, to, kind):
        actions.append({"do": "end_tree", "session": name})   # after release: claude has exited, its commands follow

    # The session left: its messages being typed go back to the queue (or become undelivered if the session is gone).
    for msg in _messages_of(st, name):
        if to in ("starting", "held", *SLEEP):
            if msg["state"] == "typing":
                _msg_move(msg, "_requeue", ts)
        elif to in ("closed", "failed", "removed"):   # a deleted one too: otherwise "queued" forever (journal 04.10)
            if msg["state"] in ("queued", "typing", "aside"):
                _msg_move(msg, "_fail", ts)
                msg["reason"] = f"session {to}"


def _for_good(sess, current, to, kind):
    """The session left for good or handed in its work: its background commands (a dev server, a test database, a
    waiter) have no one left to serve, and the host stops them by the tree mark (hosts/common.Trees; 16.10: a sleeping
    executor's dev server held 1.7 GB): the executor fell asleep (handed in, stopped), or the session was deleted or
    closed. Not for parking: a free session will come back and continue with its stand; not for a specialist that
    sleeps until the next task either."""
    if current == to:
        return False
    if to in ("closed", "removed"):
        return True
    return to in ("asleep", "retired") and minihub.runner(sess)


HANDOFF_MARK, DONE_MARK = "HANDOFF-REQUEST", "HANDOFF-DONE"


def done_body(text):
    """The text after the leading HANDOFF-DONE (and one separator on the mark's own line: "HANDOFF-DONE: …",
    "HANDOFF-DONE \u2014 …"), or None — no mark. One rule for the sleep notice to the hub and for what the owner sees."""
    stripped = str(text or "").lstrip()
    if not stripped.startswith(DONE_MARK):
        return None
    rest = stripped[len(DONE_MARK):]
    return rest[DONE_SEP.match(rest).end():].lstrip()


DONE_SEP = re.compile(r"[ \t]*(?:[:\u2013\u2014]|-(?=[ \t]))?")   # a list item on the next line ("\n- …") is content


def shown_text(text):
    """The text of a result as the owner sees it: without the leading HANDOFF-DONE. The mark is for the core (it puts the
    session to sleep, `_handoff_result` reads it from the event itself); in the feed and the session list it is noise."""
    body = done_body(text)
    return str(text or "") if body is None else body


def handoff_text(name):
    """A request to hand over work before sleep; the session's reply is its turn result, not Telegram."""
    return HANDOFF_TEXT.format(name=name)


# the marks stay out of the catalog text (fixed): the core finds the request and the answer by them in any language
HANDOFF_TEXT = T("core.msg.handoff",
                 "{mark} for {name}. The hub puts this session to sleep (the process stops, the conversation is kept). "
                 "Before it is put to sleep, hand over your work:\n"
                 "1. What can be finished in 10 minutes — finish it.\n"
                 "2. Hand the rest that is open to a live session that leads this area (hub send <session> with the "
                 "context, paths, PR/task numbers). Nobody — write exactly that, the owner decides.\n"
                 "3. Background commands and the schedule (hub schedule list --session {name}): stop or remove what is "
                 "not needed (hub schedule rm <id>), hand what is needed over with the task (hub schedule retarget <id> "
                 "<session>).\n"
                 "4. If you wait for the owner's answer — list what for.\n"
                 "When you finish, start this turn's result exactly with the line {done}, then:\n"
                 "Done: …\nOpen: … (or \"nothing\")\nHanded over: <to whom> — <what> (or \"nobody\")\nBackground: … "
                 "(or \"none\")", mark=HANDOFF_MARK, done=DONE_MARK)


def _owner_word_for(st, data, ev, own=False, lead=None):
    tools.owner_word(st, data, ev["ts"], own=own, lead=lead, cards=True)
    return True


def _heard_it(st, name, text):
    """The decision refers (mN, qN) to an owner word said to the session name itself: a message to it or an answer to its card."""
    for ref in re.findall(r"\b[mq]\d+\b", str(text or "")):
        msg = st["messages"].get(ref) or {}
        if ref[0] == "m" and msg.get("session") == name and msg.get("from_owner"):
            return True
        if ref[0] == "q" and any(m["session"] == name and m.get("answers_question") == ref for m in st["messages"].values()):
            return True
    return False


def _last_task(st, name):
    mine = [d for d in st["delegations"].values() if d["session"] == name]
    return max(mine, key=delegations.born)["id"] if mine else None


def _head(sess):
    """The message the turn began with: a notice inserted mid-turn does not change it (turn_head), unlike turn_marker."""
    return sess.get("turn_head", sess.get("turn_marker"))


def _took(sess):
    """Ids of the messages the turn took: the one it began with and what it read together with it (turn_also)."""
    return [m for m in [_head(sess)] + list(sess.get("turn_also") or []) if m]


def _his_words(took):
    """Is a turn on the owner's own words — among the messages it took: his message, his answer to a card, words passed
    on (`--for`)?"""
    return any(m.get("from_owner") or m.get("re") or m.get("answers_question") for m in took)


def _from_terminal(d):
    """A task set from the owner's terminal (`hub task new`, a schedule job's command): no `--for`, no asking session and
    no sender. Records of older cores keep the sender in `by`, whoever the assigner is, so a task a session set (even
    with no `for_session`) is not from the terminal."""
    return not (d.get("for") or d.get("for_session") or d.get("by"))


def _set_by_him(d):
    """The owner set the task himself, not through a session: `--for` his message, or from his terminal."""
    return bool(d.get("for")) or _from_terminal(d)


def _terminal_note(st, sess):
    """The hub's note (REPLIED, STOPPED) about a task from the owner's terminal, among the messages the hub's turn took:
    its head, or what it took with it (the hub was busy and read the note together with another letter)."""
    for m in _took(sess):
        note = st["delegations"].get((st["messages"].get(m) or {}).get("task") or "")
        if note and note["session"] != sess["name"] and _from_terminal(note):
            return note
    return None


def _told(st, sess, who, data):
    """Did this turn's result reach `who` (so that it can tell the owner in its stead)? The result is the reply to a
    task (the reply goes to whoever set it), the session wrote the result to `who` itself in this turn
    (delegations.got_result: a short line is not it), `who` is a mini-hub's boss (the bus), or
    `who` waits for the answer (`hub waiting`) and the core relays the result to it (delegations._relay_result)."""
    return (delegations.got_result(sess, who, data.get("text")) or minihub.boss(st, sess) == who
            or any(w["to"] == who for w in delegations.relay_targets(st, sess, data)))


def _merged_into(st, sess, did, data, now):
    """Another session tells the owner this turn's result — its name, else None (one task — one message to the owner,
    from the one he gave it to). A turn on his words to this session or on his task for it is always his. An executor
    reports to whoever set the task or raised it. Sessions talking about the owner's task (the chain starts at his
    message, core/chain.py) do not tell him: the result comes from the one he gave the task to. The hub after the answer
    on a task would only retell it; a task that broke off or has no answer yet the hub speaks about itself, and so
    does what it learned from others on its own task. Only what reached that session is folded into it (_told), and
    only if it can speak (awake): otherwise nobody would say it to the owner. The hub's retelling is folded into the one
    who answered even if it has gone to sleep since, but only if that answer reached the owner (owner_out)."""
    if not sess:
        return None
    name, marker = sess["name"], st["messages"].get(_head(sess) or "") or {}
    d = st["delegations"].get(did or "")
    # a state raised by an older core (a turn was running at `hub restart`) has no turn_his: judge by the turn's message
    his = sess["turn_his"] if "turn_his" in sess else _his_words([st["messages"].get(m) or {} for m in _took(sess)])
    if his or (d and d["session"] == name and _set_by_him(d)):
        return None   # his own task: the assigner is the owner
    who, told, root, replied = None, False, sess.get("turn_root"), 0   # replied: the hub retells an answer given at that seq
    note = _terminal_note(st, sess) if sess.get("kind") == "hub" and not root else None
    if note:
        # a task from his terminal has no chain (no root), so the hub finds it by the note it is working on
        who = note["state"] in ("replied", "done") and note.get("replied_seq") and note["session"]
        told, replied = True, note.get("replied_seq") or 0   # the hub retells what was already answered
    elif d and d["session"] == name:
        # the reply to the task goes to the one who set it, unless the task cannot take it (closed meanwhile)
        who, told = d["for_session"], delegations.reply_lands(sess, d, data, now)
    elif minihub.runner(sess) and sess.get("raised_by"):
        who = sess["raised_by"]
    elif root:
        mine = [x for x in st["delegations"].values() if x.get("for") == root]
        if sess.get("kind") == "hub":
            # the hub retells only the task whose note (REPLIED) its turn took: not the chain's latest task, not a
            # session's letter in the chain (it may carry news: a request, another task's answer)
            noted = {(st["messages"].get(m) or {}).get("task") for m in _took(sess)} - {None}
            mine = [x for x in mine if x["id"] in noted]
        d = max(mine, key=delegations.born, default=None)
        if sess.get("kind") == "hub":
            who = d and d["state"] in ("replied", "done") and d.get("replied_seq") and d["session"]   # done may have no answer
            told, replied = True, (d or {}).get("replied_seq") or 0   # the hub retells what was already answered
        elif d:
            who = d["state"] != "stopped" and d["session"]
        else:
            who = (st["messages"].get(root) or {}).get("session")
            who = who if (st["sessions"].get(who or "") or {}).get("kind") != "hub" else None
        if sess.get("kind") != "hub" and marker.get("sender") != who:
            # a correspondence is one step: a turn on the letter of the very session that has the task. Further the
            # chain is no longer about it (an agent answering two sessions in one turn must not fold a result on one
            # task into the other's)
            who = None
    up = st["sessions"].get(who or "")
    # the one who gave the answer may have gone to sleep since (the hub closed the task on REPLIED): the hub retells what
    # that session has already said, so it only needs a feed; any other result needs a session that is awake to say it
    # (not any of SLEEP: a parked one is raised by a message as an asleep one is)
    if not who or who == name or not up or up["state"] in (
            ("removed", "closed", "failed") if replied else ("removed", "closed", "failed", *SLEEP)):
        return None
    if replied and (up.get("owner_out") or 0) < replied:
        return None   # its answer did not reach the owner (a mini-hub's executor answers on the bus, or it was folded): the hub says it
    return who if told or _told(st, sess, who, data) else None


def _keep_lessons(sess, data):
    """The "Lessons" block of an executor's result, before it hands in (core/lessons): lessons from any of its turns, not
    only the last; a mention without a block gets a "look yourself" mark. A result truncated by the journal carries the
    block from the full text (host, `lessons`)."""
    verdict, found = data.get("lessons") or lessons.scan(data.get("text"))
    if journal.is_cut(data) and not data.get("lessons") and verdict != "yes":
        verdict = "unclear"   # a truncated result with no block from the host (an older host): the end with lessons may be lost
    # raised by a previous core: the fold did not see its earlier turns, so we cannot stay silent, only say "look yourself";
    # woken after handing in (a hub restart, a message): its lessons were asked for then, this counts only new ones
    kept = sess.setdefault("lessons", {"text": "", "unclear": not sess.get("handed_in")})
    if verdict == "yes" and found not in kept["text"]:
        kept["text"] = (kept["text"] + "\n\n" + found).strip()[-lessons.MAX:]
    if verdict in ("unclear", "none"):   # a later "Lessons: none" clears the earlier "will add at the end"
        kept["unclear"] = verdict == "unclear"


# how to record them (2026-10-28, the stops audit): a correction of the owner becomes "do it this way", not a new "ask first"
LESSON_FORM = T("core.msg.lesson_form",
                "an owner's correction — as \"do it like this\", not \"ask them before X\"; a stop — only on their direct "
                "\"ask me\" (their words and the date) or about money, prod, clients, the irreversible, a repo's gate")
# where whoever raised an executor records its lessons: a specialist in its CLAUDE.md, a direction in its backlog
LESSONS_WHERE_AGENT = T("core.msg.lessons_where_agent", "into your CLAUDE.md")
LESSONS_WHERE_DIRECTION = T("core.msg.lessons_where_direction",
                            "into the direction's backlog (`hub direction {slug} doc create …` or a decision)")
# what to record of them (owner decision 2026-11-08, "yes, stricter": the profiles grew 0.5-0.7 KB per handed-in
# executor; on six real notes the text below wrote 92 bytes where "what will be useful" wrote 336 — the lessons were
# there already, rephrased)
LESSONS_TAKE = T("core.msg.lessons_take",
                 "The lessons from its result — record {where} only what will change the next decision of a future "
                 "session: first find the line on that topic — it is there: correct it, do not add one; one fact — one "
                 "line, no PR or commit numbers and no dates unless the date is the point ({form}{agent}; the whole "
                 "result — hub show {name}):")
# a late note rides aside, "FYI": it says this is a job of this turn
LESSONS_LATE = T("core.msg.lessons_late",
                 "This is not just FYI — record in this turn {where} the lessons from its result: only what will change "
                 "the next decision of a future session; first find the line on that topic — it is there: correct it, "
                 "do not add one; one fact — one line, no PR or commit numbers and no dates unless the date is the point "
                 "({form}{agent}; the whole result — hub show {name}); no need to answer about it:")
# for a specialist (its own CLAUDE.md, under the cap — core/profcap.py): what it pins and how big the file is
LESSONS_PIN = T("core.msg.lessons_pin",
                "; an owner's correction or decision, a prohibition, a gate — with \U0001F4CC at the start of its line: "
                "over the size cap the core never moves those out")
LESSONS_SIZE = T("core.msg.lessons_size", "; your CLAUDE.md is {kb} KB of the {cap} KB cap")
LESSONS_SIZE_FREE = T("core.msg.lessons_size_free", "; your CLAUDE.md is {kb} KB")
LESSONS_UNCLEAR = T("core.msg.lessons_unclear",
                    "It mentioned lessons, but not as a \"Lessons\" block in the result — look yourself (hub show {name}, "
                    "the task record) and record them {where} ({form}).")
LESSONS_WHY = {"none": T("core.msg.lessons_none", "no lessons"),
               "unseen": T("core.msg.lessons_unseen", "the core saw no result with lessons"),
               "unclear": T("core.msg.lessons_no_block", "lessons mentioned without a block")}


def _open_task(st, name):
    """(id, state) of the executor's last task if it has not reported (in work, cut off, waiting for the owner), else
    None: the one rule of what "handed in" may be said about (the note to its raiser, the line in the task record)."""
    did = _last_task(st, name)
    state = (st["delegations"].get(did) or {}).get("state")
    return None if state in (None, *delegations.HANDED_IN) else (did, state)


def _lessons_note(st, ev, sess, late=False, gone=False):
    """An executor handed in its task: tell whoever raised it the lessons themselves from its results; if there are none,
    do not wake it (owner decision 2026-10-16: the requester's turn on "record the lessons" was wasted in 74 of 132
    cases), and put a line in the task record. If there was no result with text (or the executor was raised before this
    version) or lessons were mentioned without a block, do it the old way: look yourself. A late note (the executor is
    removed long after it fell asleep) does not wake either: it rides with the next message, and only with lessons in
    hand: "look yourself" hours later is noise (one per executor on the first cleanup of a backlog), and for one woken
    after it handed in, its lessons already went out with the hand-in. gone: it is being removed — there is no wake to wait
    for, so its lessons go now, under a head that does not say "handed in" if its task is not done."""
    up = st["sessions"].get(sess.get("raised_by") or "") or {}
    # lessons go to a specialist and a direction (the hub has no profession): owner decision 2026-10-04
    told = (up.get("role") == "agent" or up.get("backlog")) and up["state"] not in ("removed", "closed", "failed")
    open_task = _open_task(st, sess["name"])
    if open_task and not gone:
        # it fell asleep with its task unfinished (a cut-off turn the core's continuation did not take up): this is not a
        # hand-in. The raiser is told so, nothing is popped and nothing marked handed in: its lessons wait for the real
        # result after a wake (_keep_lessons), which then counts them all
        if told and not late:
            did, state = open_task
            new_message(st, f"m{ev['seq']}x5", up["name"], board.NOT_HANDED_HEAD.format(
                name=sess["name"], id=did, state=state, wakers=executors.wakers(sess, up["name"])),
                        ev["ts"], origin=forms.ORIGIN_CORE)
        return
    kept = sess.pop("lessons", None)
    if not told:
        return
    # its raiser has been told now: answers after a wake count only new lessons (_keep_lessons)
    sess["handed_in"] = True
    # a direction has no CLAUDE.md of its own, so lessons go to its backlog (dev agent 04.10: a lesson without an addressee)
    where = (LESSONS_WHERE_AGENT if up.get("role") == "agent" else
             LESSONS_WHERE_DIRECTION.format(slug=up["backlog"]))
    # board cards it worked on (spec dir-sync K4): the daemon put the statuses from disk (retire_executors)
    cards = board.handed_lines(st, sess["name"], (ev.get("data") or {}).get("cards"), ev["ts"]) if up.get("backlog") else []
    tail = coretext.join(*("\n\n" + line for line in cards))
    # a late note rides in the "FYI, no reply needed" block, and there the raisers put the lessons off to a turn that
    # never comes (dev agent eval: 0 of 4 wrote them, 5 of 6 with "not just FYI — in this turn")
    head = (board.REMOVED_OPEN_HEAD.format(name=sess["name"], id=open_task[0], state=open_task[1]) if open_task else
            board.HANDED_HEAD.format(name=sess["name"]))
    if kept and kept["text"]:
        text = coretext.join(head, " ", (LESSONS_LATE if late else LESSONS_TAKE).format(
            where=where, form=LESSON_FORM, name=sess["name"], agent=_lessons_agent(up)), f"\n\n{kept['text']}")
    elif not late and (kept is None or kept["unclear"]):
        text = coretext.join(head, " ", LESSONS_UNCLEAR.format(where=where, form=LESSON_FORM, name=sess["name"]))
    else:
        # late: "mentioned without a block" or "no result seen" is not "none"
        none = not late or not (kept is None or kept["unclear"])
        why = LESSONS_WHY["none"] if none else LESSONS_WHY["unseen"] if kept is None else LESSONS_WHY["unclear"]
        delegations.trail(st, _last_task(st, sess["name"]), delegations.BY_CORE, T("core.msg.trail_lessons",
                                                                      "{why} — {name} is not woken").format(
            why=why, name=up["name"]) + (T("core.msg.trail_lessons_late", " (removed some time later; hub show {name})")
                                         .format(name=sess["name"]) if late and not none else ""))
        if cards:   # a card line does not wake: it rides with the next message (spec: not a single new wake-up)
            new_message(st, f"m{ev['seq']}x5", up["name"], head + tail,
                        ev["ts"], origin=forms.ORIGIN_CORE, fyi=True, lessons_of=sess["name"])
        return
    if late:
        _late_note(st, ev, up["name"], sess["name"], text + tail)
        return
    # lessons_of: a note about another session's hand-in, not about a task of the raiser (delegations._service)
    new_message(st, f"m{ev['seq']}x5", up["name"], text + tail, ev["ts"], origin=forms.ORIGIN_CORE, lessons_of=sess["name"])


def _lessons_agent(up):
    """The specialist's part of the lessons note: pin what must stay, and the size of its file against the cap (the last
    the core saw, core.profile_size); a direction records into its backlog — nothing."""
    if up.get("role") != "agent":
        return ""
    size = up.get("profile_size") or {}
    if not size:
        return LESSONS_PIN
    kb = _kb(size.get("bytes") or 0)
    return coretext.join(LESSONS_PIN, LESSONS_SIZE.format(kb=kb, cap=size["cap"]) if size.get("cap") else
                         LESSONS_SIZE_FREE.format(kb=kb))


LATE_NOTES = 5    # late lessons notes in one "FYI" block of a raiser; past them the names go in one line (_late_note)
LATE_NAMES = 20   # names that line spells out; the rest is a count


def _late_note(st, ev, up, name, text):
    """A late lessons note, aside at the raiser up. The first cleanup of a backlog removes hundreds of old executors at
    once, and every note rode in one block with no cap (~80 KB for one raiser on a hub with ~300 of them): one block
    holds LATE_NOTES of them, the rest of the removed are named in one more line of it. Counted are all its late notes
    still aside, also those already given to a carrier message (its redelivery gathers every aside one again, _ride),
    and grouped as _ride groups them (by session); only a line not yet given is extended (a formed block is not
    changed). A block delivered — the next starts anew."""
    aside = [m for m in st["messages"].values() if m.get("late_note") and m["state"] == "aside" and m["session"] == up]
    more = next((m for m in aside if "late_count" in m and not m.get("with")), None)
    if more is None and sum("late_count" not in m for m in aside) < LATE_NOTES:
        new_message(st, f"m{ev['seq']}x5", up, text, ev["ts"], origin=forms.ORIGIN_CORE, fyi=True, late_note=True)
        return
    names, n = ((more["late_more"], more["late_count"]) if more else ([], 0))
    names, n = (names + [name])[:LATE_NAMES], n + 1   # the names it spells out, the rest only counted
    listed = ", ".join(names)
    text = (T("core.msg.late_more_count", "Also removed ({n}): {names} and {rest} more — their lessons did not get here; "
                                          "if needed — hub show <name>.").format(n=n, names=listed, rest=n - len(names))
            if n > len(names) else
            T("core.msg.late_more", "Also removed ({n}): {names} — their lessons did not get here; if needed — hub show "
                                    "<name>.").format(n=n, names=listed))
    if more is None:
        new_message(st, f"m{ev['seq']}x5", up, text, ev["ts"], origin=forms.ORIGIN_CORE, fyi=True, late_note=True,
                    late_more=names, late_count=n)
    else:   # the line is rewritten whole: its marks with it
        more.update(text=str(text), l10n=coretext.marks(text), late_more=names, late_count=n)


# --- the size cap of an agent's own CLAUDE.md (core/profcap.py; owner decisions 2026-11-08) --------------------------
PROFILE_FOLD = T("core.msg.profile_fold",
                 "Core: your CLAUDE.md is {kb} KB, over the {cap} KB cap (hub settings: agents.profile_max_kb). Fold it "
                 "below the cap in this turn: merge repeats, fold closed rollouts and chronicle down to the fact; do not "
                 "touch prohibitions, gates and the owner's corrections. The core moves nothing out of it until its "
                 "pinning is confirmed (`hub profile pin`). End the turn with one line: its size now.")
PROFILE_EVICTED = T("core.msg.profile_evicted",
                    "Core: your CLAUDE.md was {before} KB, over the {cap} KB cap: it moved {n} of the oldest unpinned "
                    "items out of it, it is {after} KB now. They are kept whole: `hub profile evicted --grep <topic>`; "
                    "`hub profile restore <id>` puts one back. What must stay — pin it with \U0001F4CC at the start of "
                    "its line.")
PROFILE_FIXED = T("core.msg.profile_fixed",
                  "Core: the pinned part of your CLAUDE.md is {fixed} KB, over the {cap} KB cap — nothing is moved out "
                  "of it. Fold the pinned items, or unpin what may go.")
PROFILE_FIXED_DEV = T("core.msg.profile_fixed_dev",
                      "Core: the pinned part of {agent}'s CLAUDE.md is {fixed} KB, over the {cap} KB cap (hub settings: "
                      "agents.profile_max_kb): the core moves nothing out of it. Look at it with {agent}: what can be "
                      "unpinned or folded, or the cap.")
EVICT_ID = re.compile(r"e[0-9a-f]{12}(?:-\d+)?")
PROFILE_NO_AGENT = T("core.reducer.profile_no_agent", "no such agent")
PROFILE_RESTORE_WHO = T("core.reducer.profile_restore_who",
                        "an agent's evicted item is put back by the agent itself, the hub or the owner")
PROFILE_BAD_ID = T("core.reducer.profile_bad_id", "not an evicted item's id (e and 12 hex digits)")
PROFILE_PIN_WHO = T("core.reducer.profile_pin_who",
                    "an agent's pinning is confirmed by the owner, the hub or the agent developer")
PROFILE_PIN_PATHS = T("core.reducer.profile_pin_paths", "the pinning list and the cut items are absolute paths")
PROFILE_SIZE_BAD = T("core.reducer.profile_size_bad", "the size of a profile is whole numbers of bytes and KB")


def profile_of(sess):
    """The profile (the folder agents/<profile>) of an agent's own CLAUDE.md: an agent's, not its executor's (an
    executor carries its raiser's profile) nor the bus liaison's (the core's profile)."""
    if not sess or sess.get("role") != "agent" or sess.get("liaison"):
        return None
    return sess.get("profile") or sess.get("session_id")


def _kb(n):
    return (int(n) + 1023) // 1024


def _profile(st, ev, data, actions):
    """The cap's facts (core.profile_*: the daemon after an agent's turn) and the requests about an agent's evicted
    items (profile.*: `hub profile restore|pin`)."""
    kind, name = ev["type"], ev.get("entity")
    sess = st["sessions"].get(name or "")
    prof = profile_of(sess)
    if not prof or sess["state"] in ("removed",):
        raise Rejected(PROFILE_NO_AGENT)
    if kind == "profile.restore_requested":
        # the agent itself, the hub or the owner (an executor reads the evicted items, it does not change its raiser's file)
        if guards.from_owner(st, data) and not (data.get("channel") == "session" and data.get("sender") in (name, _hub(st))):
            raise Rejected(PROFILE_RESTORE_WHO)
        if not isinstance(data.get("id"), str) or not EVICT_ID.fullmatch(data["id"]):
            raise Rejected(PROFILE_BAD_ID)
        actions.append({"do": "profile_restore", "session": name, "profile": prof, "id": data["id"]})
        return
    if kind == "profile.pin_requested":
        trainer = (st.get("system") or [None, None])[1:2]
        if guards.from_owner(st, data) and not (data.get("channel") == "session"
                                                and data.get("sender") in (_hub(st), *trainer)):
            raise Rejected(PROFILE_PIN_WHO)
        spec, cut = data.get("spec"), data.get("cut")
        if not isinstance(spec, str) or not os.path.isabs(spec) or (cut is not None and not (isinstance(cut, str)
                                                                                           and os.path.isabs(cut))):
            raise Rejected(PROFILE_PIN_PATHS)
        actions.append({"do": "profile_pin", "session": name, "profile": prof, "spec": spec, "cut": cut})
        return
    if kind == "core.profile_marked":
        sess["profile_marked"] = True
        return
    if kind in ("core.profile_restored", "core.profile_pin_failed"):
        return   # the journal is the record; the file changed on disk
    if kind == "core.profile_evicted":
        n, cap = int(data.get("n") or 0), int(data.get("cap") or 0)
        if n:
            new_message(st, f"m{ev['seq']}", name, PROFILE_EVICTED.format(
                before=_kb(data.get("before") or 0), after=_kb(data.get("after") or 0), cap=cap, n=n), ev["ts"],
                        origin=forms.ORIGIN_CORE, fyi=True)
        return
    if kind != "core.profile_size":
        raise Rejected("unknown event type")
    size, fixed, cap = (data.get(k) for k in ("bytes", "fixed", "cap"))
    if not all(isinstance(x, int) and not isinstance(x, bool) and x >= 0 for x in (size, fixed, cap)):
        raise Rejected(PROFILE_SIZE_BAD)
    sess["profile_size"] = {"bytes": size, "fixed": fixed, "cap": cap, "pending": bool(data.get("pending"))}
    limit = cap * 1024
    if not cap or size <= limit:
        sess.pop("profile_told", None)   # under the cap again: the next time over it says so again
        return
    if not sess.get("profile_marked"):
        if sess.get("profile_told") != "fold":
            sess["profile_told"] = "fold"
            new_message(st, f"m{ev['seq']}", name, PROFILE_FOLD.format(kb=_kb(size), cap=cap), ev["ts"],
                        origin=forms.ORIGIN_CORE)
        return
    if fixed > limit and sess.get("profile_told") != "fixed":
        sess["profile_told"] = "fixed"
        dev = (st.get("system") or [None])[0]
        if dev == name or (st["sessions"].get(dev or "") or {}).get("state") in (None, "removed", "closed", "failed"):
            new_message(st, f"m{ev['seq']}", name, PROFILE_FIXED.format(fixed=_kb(fixed), cap=cap), ev["ts"],
                        origin=forms.ORIGIN_CORE)
            return
        new_message(st, f"m{ev['seq']}", name, PROFILE_FIXED.format(fixed=_kb(fixed), cap=cap), ev["ts"],
                    origin=forms.ORIGIN_CORE, fyi=True)
        new_message(st, f"m{ev['seq']}x1", dev, PROFILE_FIXED_DEV.format(agent=name, fixed=_kb(fixed), cap=cap),
                    ev["ts"], origin=forms.ORIGIN_CORE)


def _closing(st, ev, data, actions, ctx):
    """The CLOSING machine: the closable/held_by_owner/parked_free conditions decide whether to sleep at once or hand
    over work first."""
    name = ev.get("entity")
    sess = st["sessions"].get(name)
    if not sess:
        raise Rejected("no such session")
    if system.refused(st, name):
        raise Rejected(system.refused(st, name))
    why = guards.steers(st, {**data, **_target(st, sess)})
    if why:
        raise Rejected(why)
    to, todo, why = decide(CLOSING, sess.get("closing", "none"), ev["type"], checker(st, {**sess, "req": data, "_now": ev["ts"]}))
    if to is None:
        raise Rejected(why)
    # it handed in and falls asleep (core/executors.retiring, written by the daemon): from here it is woken only
    # explicitly; the lessons go to whoever raised it. The core ignores retire from the session itself: the core
    # drives it (executor_done), and the session's own "put to sleep" is ordinary sleep
    retire = ev["type"] == "session.close_requested" and data.get("retire") and minihub.runner(sess) \
        and data.get("channel") != "session"
    if retire:
        open_task = _open_task(st, name)
        delegations.trail(st, _last_task(st, name), delegations.BY_CORE, T(
            "core.msg.trail_retired_open", "{name} fell asleep without a result: the task is {state} (to wake it — hub "
            "session wake {name})").format(name=name, state=open_task[1]) if open_task else T(
            "core.msg.trail_retired", "{name} handed in and fell asleep (to wake it — hub session wake {name})"
        ).format(name=name))
        _lessons_note(st, ev, sess)
    sess["closing"] = to
    if ev["type"] == "session.close_ready":
        sess["close_note"] = data.get("summary")
    if ev["type"] == "session.close_requested" and "handoff" not in todo:
        sess["slept_on"] = ev["seq"]   # it fell asleep at once, without a handover, and the feed labels it so (core/views._items)
    _closing_actions(st, sess, todo, ev, actions, retire=retire)


def _closing_after_turn(st, ev, actions):
    """A request to put to sleep made mid-turn: the turn ended and the session is free, so start the handover (or keep waiting)."""
    for sess in st["sessions"].values():
        if sess.get("closing") == "ready" and sess["state"] == "idle":
            to, todo, _why = decide(CLOSING, "ready", "_turn_done", checker(st, {**sess, "_now": ev["ts"]}))
            if to is not None:
                sess["closing"] = to
                _closing_actions(st, sess, todo, ev, actions)


WATCH_COOLDOWN = 30 * 60
# st["watch"]: keys are session names ("name", "name:kind" for alerts to the hub) and service entries; service keys
# start with ":" (a session name does not, see NAME), otherwise a session called "surrender" or "core" would share an entry with a service one
SURRENDER_KEY, CORE_KEY, PROMISE_KEY = ":surrender", ":core", ":promise"
# what the watchdog saw, as words inside its alert (modules/watch reads them too, for an alert about the hub itself)
WATCH_KINDS = {"loop": T("core.msg.watch_loop", "is going in circles"),
               "errors": T("core.msg.watch_errors", "keeps hitting the same error"),
               "hung": T("core.msg.watch_hung", "hung: a turn is running, but there are no events"),
               "silent": T("core.msg.watch_silent", "went silent: it got a message and does not answer")}
WATCH_TROUBLE = T("core.msg.watch_trouble", "is in trouble")


WATCH_FACTS = {"undelivered": T("core.msg.watch_undelivered", "a message is not delivered to it"),
               "crashloop": T("core.msg.watch_crashloop", "the process falls and comes up again"),
               "stalled": T("core.msg.watch_stalled", "stands after a cut turn"),
               "background": T("core.msg.watch_background", "a command or an agent has run in the background for long")}


def _watch_alert(st, ev, data, ctx):
    """The journal watchdog (modules/watch) thinks a session is in trouble: tell the hub; about one session no more
    often than once per WATCH_COOLDOWN. The watchdog only reports: the hub decides what to do."""
    name, ts, kind = ev.get("entity"), ev["ts"], data.get("kind")
    if data.get("channel") == "session":
        raise Rejected(T("core.reducer.alert_who", "the watchdog sends an alert, not a session"))
    if data.get("message") is not None:   # "not delivered": the watchdog may have looked at the past, so check against the queue
        owner.waiting(st, data["message"])
    seen = st.setdefault("watch", {})
    # the watchdog's reason and advice: its phrases' marks ride in the fact (reason_l10n, advice_l10n)
    reason = coretext.join(" ", coretext.got(data, "reason")) if data.get("reason") else ""
    advice = T("core.msg.watch_advice", "\nWatchdog's advice: {advice}").format(advice=coretext.got(data, "advice")) \
        if data.get("advice") else ""
    if kind == "corebug":  # an error in core code on an event: the session is not to blame
        if ts - seen.get(CORE_KEY, -WATCH_COOLDOWN) < WATCH_COOLDOWN:
            raise Rejected(T("core.reducer.alert_core_told",
                             "too early: the hub was already told about the core failure"))
        seen[CORE_KEY] = ts
        return ctx["notify"](ev, T("core.msg.watch_corebug",
                                   "Journal watchdog: the core rejected an event because of an error in its own "
                                   "code.{reason}\nThis is work for the session that develops the hub: pass it the "
                                   "text whole.").format(reason=reason))
    sess = st["sessions"].get(name)
    if not sess or sess["state"] == "removed":
        raise Rejected("unknown session")
    # behavior (a loop, errors, hung, silent) shares one shelf, core facts (not delivered, crashing) each get their own
    key = f"{name}:{kind}" if kind in WATCH_FACTS else name
    if ts - seen.get(key, -WATCH_COOLDOWN) < WATCH_COOLDOWN:
        raise Rejected(T("core.reducer.alert_told", "too early: the hub was already told about this session"))
    seen[key] = ts
    tail = "\n".join(f"  {x}" for x in data.get("tail") or [])
    if kind in WATCH_FACTS:
        head = T("core.msg.watch_fact", "Journal watchdog: session {name} — {what}.{reason}").format(
            name=name, what=WATCH_FACTS[kind], reason=reason)
    else:
        # (an earlier detector had the confidence figure until 05.10)
        sure = T("core.msg.watch_sure", " (confidence {pct}%)").format(pct=round(data["trouble"] * 100)) \
            if data.get("trouble") else ""
        head = T("core.msg.watch_kind", "Journal watchdog: session {name}, it seems, {what}{sure}.{reason} {why}").format(
            name=name, what=WATCH_KINDS.get(kind, WATCH_TROUBLE), sure=sure, reason=reason, why=data.get("why") or "")
    ctx["notify"](ev, coretext.join(
        head, T("core.msg.watch_steps", "\nLast steps:\n{tail}").format(tail=tail) if tail else "", advice,
        T("core.msg.watch_decide", "\nLook at hub show {name} and decide: wait, write to it, stop or restart it. A false "
                                   "alarm — do nothing. Write to the owner only if their decision is needed.").format(
            name=name)))


NAME_MAX = 40
NAME_CHARS = re.compile(r"[a-z0-9][a-z0-9._-]*")
NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,%d}" % (NAME_MAX - 1))


def _rename(st, ev, data, actions):
    """A new session name (30.09). The name is a key: messages, assignments, the schedule and addresses move over;
    the old name stays an address. Only for a sleeping session: a live process has the name in its environment (HUB_SESSION)."""
    old, new = ev.get("entity"), str(data.get("to") or "").strip()
    sess = st["sessions"].get(old)
    if not sess or sess["state"] == "removed":
        raise Rejected("unknown session")
    if sess.get("kind") == "hub":
        raise Rejected(T("core.reducer.rename_hub", "the hub is not renamed: its name is in the settings (hub.json)"))
    if system.refused(st, old):
        raise Rejected(system.refused(st, old))
    if guards.from_owner_or_hub(st, data):
        raise Rejected(T("core.reducer.rename_who", "the hub or the owner renames"))
    if not NAME.fullmatch(new):
        raise Rejected(T("core.reducer.rename_bad", "a name is Latin letters, digits, hyphen, up to {max} characters")
                       .format(max=NAME_MAX))
    if new in st["sessions"] and st["sessions"][new]["state"] != "removed":
        raise Rejected(T("core.reducer.rename_taken", "the name {name} is taken").format(name=new))
    live = sess["state"] == "idle"
    if live:
        # a free live session is renamed by a restart under the new name (an agent cannot be put to sleep, and the
        # process has the name in its environment)
        if sess.get("pending") or sess.get("background") or \
                any(m["session"] == old and m["state"] in ("queued", "typing") for m in st["messages"].values()):
            raise Rejected(T("core.reducer.rename_busy",
                             "the session has a question, background or a queue — it is renamed once it is free"))
    elif sess["state"] in ("starting", "busy", "dialog", "held"):
        # busy: it renames itself when it frees up (01.10: nobody repeated a refusal made mid-turn)
        sess["rename_to"] = new
        return
    del st["sessions"][old]
    sess["name"], sess["was"] = new, (sess.get("was") or []) + [old]
    st["sessions"][new] = sess
    for m in st["messages"].values():
        for k in ("session", "to"):
            if m.get(k) == old:
                m[k] = new
    for d in st["delegations"].values():
        for k in ("session", "for_session", "by"):
            if d.get(k) == old:
                d[k] = new
    for run in (st.get("hub_runs") or {}).values():   # its `hub run` commands go on and report to the new name
        if run.get("session") == old:
            run["session"] = new
    for job in st.get("schedule", {}).values():  # a schedule entry is the job itself: for whom (session) and who set it (by)
        for k in ("session", "by"):
            if job.get(k) == old:
                job[k] = new
        if (job.get("command_by") or {}).get("session") == old:   # whose word its command is (schedule.speaker_of)
            job["command_by"] = {"session": new}
    for bucket in ("deferred", "done", "decided"):
        for item in st["owner"].get(bucket) or []:
            if isinstance(item, dict) and item.get("session") == old:
                item["session"] = new
    for d in st.get("decisions") or []:   # `hub decided list`: the author withdraws under the new name
        for k in ("by", "told"):
            if d.get(k) == old:
                d[k] = new
    for key, name in list(st["aliases"].items()):
        if name == old:
            st["aliases"][key] = new
    st["aliases"][f"was:{old}"] = new
    for s in st["sessions"].values():  # a renamed session's executors are still its; a mini-hub's executor also has the bus
        for k in ("raised_by", "bus"):
            if s.get(k) == old:
                s[k] = new
    for run in (st.get("runs") or {}).values():   # its queued executor requests are still its
        if run.get("by") == old:
            run["by"] = new
    reminders = st["owner"].get("reminders") or {}
    for key in [k for k in reminders if k.startswith(f"q:{old}:")]:   # its open cards, with their reminders
        reminders[f"q:{new}:" + key[len(f"q:{old}:"):]] = reminders.pop(key)
    seen = st["owner"].get("seen") or {}
    if old in seen:
        seen[new] = seen.pop(old)
    layout.rename(st, old, new, ev["seq"])
    if live:  # the same conversation, a new process under the new name
        sess["state"], sess["since"] = "starting", ev["ts"]
        actions.append({"do": "release", "session": old})
        actions.append(spawn_action(sess, st=st))


def _setter(st, sess, data, name, refusal, itself=False):
    """A live session's setting (level, model, folder, mini-hub) is set by the owner or the hub; with itself, by the session too."""
    if not sess or sess["state"] in ("removed", "closed", "failed"):
        raise Rejected("unknown session")
    who = st["sessions"].get(data.get("sender") or "") or {}
    if not (tools.from_owner(data) or who.get("kind") == "hub" or (itself and data.get("sender") == name)):
        raise Rejected(refusal)


def _state_text(st, key):
    """A text the fold keeps for a human to read later, in the hub's language (as st["messages"] keeps the owner's)."""
    from core import i18n
    return i18n.tr(i18n.state_lang(st) or i18n.SOURCE, key)


def _real(name):
    """The session's name for a refusal's {name} when it is a real one; otherwise None (the placeholder "<name>")."""
    return name if isinstance(name, str) and NAME.fullmatch(name) else None


def _named(why, name):
    """A refusal with the session's name in place of <name>, when the name is a real one. The machines' phrases get
    the name as their {name} (refusal, decide: their marks stay); a plain text has it replaced."""
    if getattr(why, "spans", ()) or not _real(name):
        return why
    return why.replace("<name>", name)


SPEC_ON = T("core.msg.spec_on", "Specs are on: {name} raises the executor of a large task with a spec first (hub task "
                               "run … --spec). Turn them off — /spec off.")
SPEC_OFF = T("core.msg.spec_off", "Specs are off for {name}.")
SPEC_OFF_OPEN = T("core.msg.spec_off_open", "Specs are off for {name}. Open specs: {list} — they wait for \"Accept\" or "
                                           "\"Drop\".")
SPEC_WHO = T("core.reducer.spec_who", "specs are turned on by a person with the /spec command in the session's composer")
SPEC_EXECUTOR = T("core.reducer.spec_executor", "{name} is an executor: an executor has no executors of its own")
SPEC_FLAG_OFF = T("core.reducer.spec_flag_off", "--spec: specs are off for {name} — a person turns them on with /spec on "
                                                "in its composer")
SPEC_FOLDER = T("core.reducer.spec_folder", "--spec-dir <name>: Latin lowercase letters, digits, hyphen, up to 40, "
                                            "not dN (that is a task's own folder)")
SPEC_FOLDER_QUEUED = T("core.reducer.spec_folder_queued", "--spec-dir {folder}: executor {name} raised with this folder "
                                                          "has not started yet — another folder")
SPEC_FOLDER_TAKEN = T("core.reducer.spec_folder_taken", "--spec-dir {folder}: the open spec of {id} lies there — another folder, "
                                                        "or accept or drop that one first")


def _spec_set(st, ev, data, name, sess):
    """/spec on|off in a session's composer (core/specmode.py): only a person on the page or in Telegram (owner
    decision 2026-11-08: "the mode is turned on only by a person with /"), for a session that raises executors. The
    raiser's rule is in its prompt: a new process with the same conversation. Open specs of its executors stay open."""
    if not (data.get("owner") is True and data.get("channel") in ("web", "telegram")):
        raise Rejected(SPEC_WHO)
    if not sess or sess["state"] == "removed":
        raise Rejected("unknown session")
    if minihub.runner(sess) or sess.get("liaison"):
        raise Rejected(SPEC_EXECUTOR.format(name=name))
    want = data.get("on") is True
    if want == bool(sess.get("spec")):
        return
    if want:
        sess["spec"] = True
    else:
        sess.pop("spec", None)
    _respawn(sess)
    opened = [d for d in st["delegations"].values() if d.get("for_session") == name and specmode.is_open(d)
              and d["state"] not in ENDED]
    text = (SPEC_ON.format(name=name) if want else
            SPEC_OFF_OPEN.format(name=name, list=", ".join(f"{d['id']} ({d['session']})" for d in opened)) if opened
            else SPEC_OFF.format(name=name))
    new_message(st, f"m{ev['seq']}", name, text, ev["ts"], origin=forms.ORIGIN_CORE, fyi=True)


def _spec_flag(st, data, raiser):
    """hub task run --spec [<folder>]: only a session with specs on; the folder is a name inside specs/, not taken by
    another open spec."""
    want = data.get("spec")
    if not want:
        return None
    if not (st["sessions"].get(raiser or "") or {}).get("spec"):
        raise Rejected(SPEC_FLAG_OFF.format(name=raiser or "the terminal"))
    if want is True:
        return True
    if not isinstance(want, str) or not specmode.FOLDER.fullmatch(want):
        raise Rejected(SPEC_FOLDER)
    other = specmode.taken(st, want)
    if other:
        raise Rejected(SPEC_FOLDER_TAKEN.format(folder=want, id=other) if other in st["delegations"] else
                       SPEC_FOLDER_QUEUED.format(folder=want, name=other))
    return want


def _respawn(sess):
    """A new process with the same conversation, as for a new tool set: at once if free, after the turn if busy; asked
    for by name, so a background run does not hold it (_relaunch_waits)."""
    if sess["state"] in ("idle", "busy", "dialog", "starting"):
        sess["dirty"] = True
        sess["respawn_now"] = True


def _modes(st):
    """A snapshot before the rules change: whether each session is a mini-hub (core/minihub.on) and whether it has no executors."""
    return {n: (minihub.on(st, s), bool(minihub.alone(st, s))) for n, s in st["sessions"].items()}


def _modes_follow(st, ev, before):
    """After a change of parallelism, "solo", folder, role or backlog: a session that stopped being a mini-hub does so
    at once (if it becomes one again, it is after the current task); one whose mode changed gets a new process at the
    end of the turn; one with no executors left has its queued requests dropped. Running executors are not stopped: they
    finish, and the results go to whoever raised them."""
    k = 0
    for name, sess in list(st["sessions"].items()):
        was, was_alone = before.get(name, (False, False))
        if not minihub.want(st, sess):
            sess.pop("minihub_since", None)
        elif not sess.get("minihub_since") and sess["state"] in ("starting", "idle"):
            _born(st, sess, ev["seq"])   # it became a direction or specialist before starting a task
        _minihub_follows(sess, was, minihub.on(st, sess))
        why = minihub.alone(st, sess)
        if why and not was_alone:
            k += _drop_runs(st, ev, name, why, k)


def _drop_runs(st, ev, name, why, k=0):
    """A session has no more executors (parallelism 0 or "solo"): its queued and ready-to-raise requests are dropped, and
    it gets a line for each task it asked for (_for_parent: a turn on it is that task's turn, as with a cancel); a
    task handed to an executor via hub route returns to it. Returns the number of lines (ids numbered within this event)."""
    runs, by_task = st.get("runs") or {}, {}
    for n in [n for n, r in runs.items() if r.get("by") == name]:
        run = runs.pop(n)
        took = st["messages"].get(run.get("message") or "")
        if took and took.get("handover") == n:
            took.pop("handover")
        by_task.setdefault(_for_parent(st, run).get("for_delegation"), []).append(n)
    if st["sessions"][name]["state"] in ("removed", "closed", "failed"):
        return 0
    for i, (did, gone) in enumerate(by_task.items()):
        new_message(st, f"m{ev['seq']}x8{k + i}", name, T("core.msg.runs_dropped",
                                                          "Executor requests are dropped ({names}): {why}.").format(
            names=", ".join(gone), why=why),
                    ev["ts"], origin=forms.ORIGIN_CORE, **({"for_delegation": did} if did else {}))
    return len(by_task)


def _born(st, sess, seq, mark=True):
    """A mini-hub from the first launch (core/minihub.born): one rule for every place: the minihub_since mark (mark
    means set it if absent; without mark only for one already born a mini-hub) and its not yet typed task in its own
    words (executors.born_brief)."""
    if not minihub.born(st, sess) or not (sess.get("minihub_since") or mark):
        return
    if not sess.get("minihub_since"):
        sess["minihub_since"] = seq
    executors.born_brief(st, sess["name"])


def _minihub_follows(sess, was, now):
    """A mini-hub (core/minihub.on) started or stopped being one: a new process, with the edit ban and the addendum or
    without them. Its own mark respawn_minihub (what the process was raised with), not dirty: dirty is for other
    causes and keeps the old cut text (_tools_apply); if it is restored to how it was launched, no restart is needed."""
    if now == was or sess["state"] not in ("idle", "busy", "dialog", "starting"):
        return
    spawned = sess.pop("respawn_minihub", was)
    if now != spawned:
        sess["respawn_minihub"] = spawned


def _role_set(st, ev, data):
    """Appoint as an agent (the hub or the owner), edit the description and icon; demote to a worker: the owner, or the
    hub on his word (--for with his message to the hub or an answer to the hub's card, as for risky tools)."""
    sess = st["sessions"].get(ev.get("entity"))
    if not sess or sess["state"] == "removed":
        raise Rejected("unknown session")
    if sess.get("kind") == "hub":
        raise Rejected(T("core.reducer.role_hub", "the hub is neither an agent nor a worker"))
    if guards.from_owner_or_hub(st, data):
        raise Rejected(T("core.reducer.role_who", "the hub or the owner assigns roles"))
    role = data.get("role") or sess.get("role") or "worker"
    if role not in ("agent", "worker"):
        raise Rejected(T("core.reducer.role_kind", "the role is agent or worker"))
    if role != "agent" and system.refused(st, sess["name"]):
        raise Rejected(system.refused(st, sess["name"]))
    if role == "worker" and sess.get("role") == "agent" and guards.from_owner(st, data):
        if not data.get("for"):
            raise Rejected(T("core.reducer.role_demote",
                             "the owner demotes an agent; the hub — on their word: --for mN (their message to you) or qN "
                             "(the answer to your card)"))
        tools.owner_word(st, data, ev["ts"], cards=True)   # an mN of his message to the hub or a qN, the answer to a card
    about = data.get("about") or sess.get("about")
    if role == "agent" and not about:
        raise Rejected(T("core.reducer.role_about", "an agent needs --about: what it does, in one line — by it the hub "
                                                    "chooses whom to give a task"))
    before = _modes(st)
    sess.update(role=role, about=about, icon=data.get("icon") or sess.get("icon"))
    _unretire(sess)
    _modes_follow(st, ev, before)   # became a specialist: a mini-hub after the current task; stopped: at once, without the rules


def _unretire(sess):
    """One that handed in stopped being an executor (role, a direction's backlog): it simply sleeps, and messages wake it again."""
    if sess["state"] == "retired" and not minihub.runner(sess):
        sess["state"] = step(SESSION, "retired", "_unretire")[0]


def _iconed(st, ev, data):
    """An own icon for any session, the hub too (the owner or the hub); empty removes it and the previous one returns
    (the agent's, the direction's)."""
    sess = st["sessions"].get(ev.get("entity"))
    if not sess or sess["state"] == "removed":
        raise Rejected("unknown session")
    why = layout.who_may(st, data)
    if why:
        raise Rejected(why)
    icon = data.get("icon")
    if icon in (None, ""):
        sess.pop("own_icon", None)
    elif layout.icon_ok(icon):
        sess["own_icon"] = icon
    else:
        raise Rejected(T("core.reducer.icon_one", "an icon is one emoji (not text, no spaces)"))


def _auto_sleep(st, name, ev, actions):
    """The assignment is closed: the executor hands over its work and falls asleep if it can (the closable condition);
    otherwise it stays."""
    sess = st["sessions"].get(name)
    if not sess or sess.get("backlog"):   # a direction: the task is closed but backlog work goes on, so it does not hand over
        return
    to, todo, _why = decide(CLOSING, sess.get("closing", "none"), "session.close_ready", checker(st, {**sess, "_now": ev["ts"]}))
    if to is None:
        return
    sess["closing"] = to
    sess["close_note"] = "task closed"
    _closing_actions(st, sess, todo, ev, actions)


def _closing_actions(st, sess, todo, ev, actions, retire=False):
    for act in todo:
        if act == "handoff":
            new_message(st, f"m{ev['seq']}", sess["name"], handoff_text(sess["name"]), ev["ts"], origin=forms.ORIGIN_HUB)
            sess["handoff_due"] = ev["ts"] + HANDOFF_WAIT   # without HANDOFF-DONE the handover ends on timeout (_handoff_timer)
            sess.pop("handoff_said", None)
        elif act == "sleep":
            _session_event(st, {"type": "_retire" if retire else "_sleep", "entity": sess["name"], "ts": ev["ts"],
                                "seq": ev["seq"]}, {}, actions)
        elif act == "unpark":
            # a parked session (no process): for the owner it is no longer "free" but asleep; a message still wakes
            # it. State only: there is no process, and the time since it has slept does not change
            sess["state"] = step(SESSION, "parked", "_retire" if retire else "_unpark")[0]


def _handoff_result(st, ev, data, actions, ctx):
    """The turn result starts with HANDOFF-DONE and the session was handing over: put it to sleep and tell the hub (a
    line for the day's summary)."""
    sess = st["sessions"].get(ev.get("entity"))
    text = (data.get("text") or "").lstrip()
    if not sess or sess.get("closing") != "handoff":
        return
    if not text.startswith(DONE_MARK):
        # a result without the mark: the handover is not cancelled at once, since the result could be from a turn
        # before the handover and arrive after it began; the timeout (_handoff_timer) tells the hub if the mark never
        # comes. Each result replaces the previous: an empty handover-turn result leaves the hub no text from a late
        # result of an earlier turn
        sess["handoff_said"] = delegations.short_text(text, 300) if text else None
        return
    waiting = [m["id"] for m in st["messages"].values() if m["session"] == sess["name"]
               and m["state"] in ("queued", "typing") and not m["text"].startswith(HANDOFF_MARK)]
    if waiting:
        # someone wrote to it while it was handing over (09.10: an owner message was typed into the handover turn and
        # the process released, so there was no reply for almost two hours): do not put to sleep, let it answer first
        sess["closing"] = decide(CLOSING, "handoff", "_cancel")[0]
        ctx["notify"](ev, HANDOFF_INTERRUPTED.format(name=sess["name"], ids=", ".join(waiting)))
        return
    to, todo, _ = decide(CLOSING, "handoff", "_handoff_done")
    sess["closing"] = to
    if sess["state"] == "busy":
        sess["state"] = "idle"  # the turn result came before Stop: the turn is finished
    _closing_actions(st, sess, todo, ev, actions)
    body = done_body(text).strip()
    note = f"\nCLOSE-READY: {sess['close_note']}" if sess.get("close_note") else ""
    ctx["notify"](ev, coretext.join(HANDOFF_SLEPT.format(mark=DONE_MARK, name=sess["name"],
                                                         wakes=_wakes(st, sess["name"], ev["ts"])), ":\n", body, note))


HANDOFF_INTERRUPTED = T("core.msg.handoff_interrupted",
                        "{name} is not put to sleep: a message came while it was handing over ({ids}) — it will answer "
                        "it first. Put it to sleep again once it is free.")
HANDOFF_SLEPT = T("core.msg.handoff_slept", "{mark} {name} is asleep{wakes}")
HANDOFF_UNMARKED = T("core.msg.handoff_unmarked",
                     "{name} ended the handover turn without {mark} — not put to sleep{said}. Look at hub show {name}; to "
                     "put it to sleep again, if still needed — hub session close {name}.")
WAKES_ON_SCHEDULE = T("core.msg.handoff_wakes", " (will wake by itself for scheduled checks: {n}, the next at {at})")
HANDOFF_WAIT = 10 * 60   # handover deadline: the turn ended without HANDOFF-DONE, so the handover is cancelled, the hub gets a line
HANDOFF_SETTLE = 60      # free for less than this: a marked result may still arrive (results and hooks take different paths)


def _handoff_timer(st, ev, ctx):
    """The handover deadline (timer.fired h:<name>): the handover turn ended without HANDOFF-DONE, so the handover is
    cancelled (otherwise every following "put to sleep" was refused until a restart), the session stays alive and the
    hub decides. If the turn is still running or HANDOFF still waits to be typed, the deadline is postponed."""
    sess = st["sessions"].get(str(ev.get("entity"))[2:])
    if not sess:
        return
    if sess.get("closing") != "handoff":
        sess.pop("handoff_due", None)
        return
    ts, since = ev["ts"], sess.get("since") or ev["ts"]
    waits = any(m["state"] in ("queued", "typing") and str(m["text"]).startswith(HANDOFF_MARK)
                for m in _messages_of(st, sess["name"]))
    if sess["state"] != "idle" or waits:
        sess["handoff_due"] = ts + HANDOFF_WAIT
        return
    if ts - since < HANDOFF_SETTLE:
        sess["handoff_due"] = since + HANDOFF_SETTLE
        return
    sess["closing"] = decide(CLOSING, "handoff", "_cancel")[0]
    sess.pop("handoff_due", None)
    said = sess.pop("handoff_said", None)
    name = sess["name"]
    ctx["notify"](ev, HANDOFF_UNMARKED.format(name=name, mark=DONE_MARK, said=f": «{said}»" if said else ""))


def handoff_timers(st):
    """(deadline, h:<name>, None) of handovers, for pending_timers. A handover from a previous core version (with no
    deadline) counts from the state change: one that hung before the update is cancelled too."""
    return [(s.get("handoff_due") or (s.get("since") or 0) + HANDOFF_WAIT, f"h:{name}", None)
            for name, s in st["sessions"].items() if s.get("closing") == "handoff"]


def _wakes(st, name, now):
    """Sleep is not forever: its own schedule jobs (`hub wait` checks) will wake it (the hub on 01.10: "put to sleep",
    and a minute later alive again, an answer check)."""
    from datetime import datetime
    from core.zones import zone
    from core import schedule as sched
    tz = (st.get("owner") or {}).get("tz") or "UTC"
    due = sorted(t for j in st.get("schedule", {}).values() if sched.wakes(j, name) and j.get("state") == "active"
                 for t in [sched.next_due(j, now, tz)] if t)
    if not due:
        return ""
    at = datetime.fromtimestamp(due[0], zone(tz)).strftime("%H:%M")
    return WAKES_ON_SCHEDULE.format(n=len(due), at=at)


REFLECT_EVERY = 3 * 3600
RECAP_TOOLS = 2     # steps per turn: after such a turn we ask for a Claude Code recap (owner decision 2026-10-03)
# session.recap by — the host's recap after such a turn (the session's "away" line): Claude Code's /recap, and on Codex
# the core's model over the thread's tail (hosts/codex_life.recap)
HOST_RECAP = ("claude", "codex")
REFLECT_TOOLS = 15  # this many steps per turn is a "big piece of work": agents are written to via hub send, few assignments
OUT_HEAD = 160      # characters of the start of a result to the owner in the snapshot (out_head): a list line, not text
MD_SCAN = 4000      # characters of a text strip_markup cleans for a one-line head: its regexes are quadratic on a long line of "**"


GIST = 160          # characters of a session's gist (`hub gist`): a line of the "Now" tab, 3-4 lines


def one_line(text, limit=GIST, word=False):
    """Text on one line: whitespace and newlines become one space, longer than limit is cut with "..." (word cuts at the
    last whole word if it is not shorter than half)."""
    t = " ".join(str(text or "").split())
    if len(t) <= limit:
        return t
    cut = t[:limit - 1] if word else t[:limit]
    if word and t[limit - 1] != " " and " " in cut[limit // 2:]:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:—–-" if word else None) + "…"


LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
NOT_GIST = re.compile(r"^(?:#|\||>|[-*+]\s|\d+[.)]\s)")   # a heading, table, quote, list item


def result_gist(text):
    """The first line of a long result is its gist if it is 20 to GIST characters and not a heading, table, quote, list
    item or an intro ("here is what I found:"); otherwise None. A link becomes its text, the Markdown marks are stripped (strip_markup),
    whitespace becomes one space. Long means without link addresses too: the page shows a short result itself."""
    t = LINK.sub(r"\1", str(text or "")[:MD_SCAN]).strip()   # longer is long anyway; LINK is quadratic on a run of "["
    first = t.partition("\n")[0].strip()
    if len(" ".join(t.split())) <= GIST or NOT_GIST.match(first):
        return None
    first = " ".join(strip_markup(first[:MD_SCAN]).split())
    if first.endswith(":") or NOT_GIST.match(first):
        return None
    return first if 20 <= len(first) <= GIST else None   # "Done." is not a gist: the first phrase of the result says more


MD_IMAGE_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
# a mark opens at a word edge and closes at one: "2**10", "src/**/*.py" and "__init__.py" are not bold (a dunder name is one
# word between "__": models write bold with "**")
MD_STRONG = re.compile(r"(?<![\w*/\\])\*\*(\S(?:.*?\S)?)\*\*(?![\w*])|(?<!\w)__(?!\w+__(?!\w))(\S(?:.*?\S)?)__(?!\w)|~~(\S(?:.*?\S)?)~~")
MD_EM_STAR = re.compile(r"(?<![^\s(\[\"'«“])\*(?=[^\W_]|[(«“—–])([^*]+?)(?<=[^\W_]|[.!?:;,)\"'»”])\*(?![\w*])")   # opens before and closes after a letter: "(*)", "/**/*.py", "clean-*" are not italic
MD_EM_UNDER = re.compile(r"(?<![^\s(\[\"'«“])_(?=[^\W_]|[(«“—–])([^_]+?)(?<=[^\W_]|[.!?:;,)\"'»”])_(?![\w_])")
MD_TABLE_RULE = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")   # the |---|---| row under a table header
MD_LINE_START = re.compile(r"^\s*(?:#{1,6}\s+|>(?!=)\s?|[-*+•]\s+|\d{1,3}[.)]\s+|```[\w+.-]*(?=\s*$)|\|)")
MD_CODE = re.compile(r"`([^`]*)`")
MD_STRAY_STRONG = re.compile(r"(?:(?<=\s)|^)\*\*(?=\S)|(?<=\S)\*\*(?=\s|$)|^\s*\*\*\s*$")   # a "**" that opens or closes at a space or a line edge: not "2**10", not "src/**/*.py"


def _unbold(m):
    """A bold match loses its marks, except one that opens right after "(" and holds a ")" with no "(" of its own before it:
    that is the call `f(**kwargs)` and the bold after it, not a bold in brackets ("(**note**)", "(**a (b)** c)")."""
    inner = next(g for g in m.groups() if g is not None)
    if (m.group(0).startswith("**") and m.start() and m.string[m.start() - 1] == "("
            and inner.count(")") > inner.count("(")):
        return m.group(0)
    return inner


def strip_markup(text):
    """Text for a list line without Markdown: a link is its text (an image is its alt), **bold**, __bold__, ~~struck~~,
    *italic*, _italic_ and `code` lose their marks; a heading, quote, list item and a table border lose the mark at the
    start of a line. Newlines stay (one_line folds them). Snake_case, "3 > 2" and "a * b" are left alone."""
    out = []
    for line in str(text or "").replace("\0", "").splitlines():
        line = MD_IMAGE_LINK.sub(r"\1", line)
        if MD_TABLE_RULE.match(line):
            continue
        table = line.lstrip().startswith("|")
        line = MD_LINE_START.sub("", line)
        code = []   # `code` keeps its text as it is ("src/**/*.py", "a_b_c"): only the ticks go
        line = MD_CODE.sub(lambda m: code.append(m.group(1)) or f"\0{len(code) - 1}\0", line)
        line = MD_STRONG.sub(_unbold, line)
        line = MD_STRAY_STRONG.sub("", line)   # "**" left unpaired: a mark too
        line = MD_EM_UNDER.sub(r"\1", MD_EM_STAR.sub(r"\1", line))
        out.append(re.sub("\0(\\d+)\0", lambda m: code[int(m.group(1))], line).rstrip(" |" if table else " "))
    return "\n".join(out)


HEAD_FENCE = re.compile(r"^\s*(?:```|~~~)[\w+.-]*\s*$")   # a bare fence line: "```x``` ok" is an inline span, not a fence
HEAD_HEADING = re.compile(r"^\s*#{1,6}\s+\S")
HEAD_CLOSING = re.compile(r"\s+#+\s*$")   # "## Title ##"
SENTENCE_MARKS = (".", "!", "?", "…", ":", ";", ",")


def head_text(text):
    """The text of a result for one line, without Markdown marks and without the seams that folding lines would glue:
    a heading that is not the last text ends with a full stop ("Report. The map is rebuilt." — not "Report The map…"), and
    the rows of a table that comes after some text are left out (they would trail as "a | b"); a result that opens with a
    table (or a heading and a table) keeps its header row. A "#" or "|" line inside a code fence is code, not a heading
    or a table."""
    pieces, fenced, prose, table = [], False, False, False
    for line in str(text or "").splitlines():
        heading = False
        if HEAD_FENCE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            if line.lstrip().startswith("|"):
                if prose or table:
                    continue
                table = True
            elif HEAD_HEADING.match(line):
                heading, line = True, HEAD_CLOSING.sub("", line)
        clean = " ".join(strip_markup(line).split())
        if clean:
            pieces.append((clean, heading))
            prose = prose or not (heading or table and line.lstrip().startswith("|"))
    return " ".join(f"{t}." if h and i < len(pieces) - 1 and not t.endswith(SENTENCE_MARKS) else t
                    for i, (t, h) in enumerate(pieces))


def out_head(text):
    """The start of a result on one line (OUT_HEAD characters, cut with "..."), without Markdown marks: clients put it in
    a list row as is (the web page used to strip them itself, the native ones showed "**bold**" literally)."""
    return one_line(head_text(str(text or "")[:MD_SCAN]), OUT_HEAD)


def _reflect(st, sess, ev):
    """An agent closed an assignment: ask it to add durable notes to its own CLAUDE.md (owner decision 2026-10-01:
    in a day no agent wrote a single line there, one instruction is not enough). No more often than once per
    REFLECT_EVERY; not for executors and workers (the file is not theirs or does not exist); not for a network operator
    (it has no tools, the owner maintains the file)."""
    if sess.get("role") != "agent" or ev["ts"] - sess.get("reflected", -REFLECT_EVERY) < REFLECT_EVERY:
        return
    sess["reflected"] = ev["ts"]
    # the path comes from the session environment: the hub's state folder is its own (02.10: a demo hub got someone
    # else's ~/.hub-core/...)
    new_message(st, f"m{ev['seq']}x7", sess["name"], REFLECT_ASK, ev["ts"], origin=forms.ORIGIN_CORE)


REFLECT_ASK = T("core.msg.reflect_ask",
                "Task closed. Reread this piece of work and add what lasts to your CLAUDE.md ($HUB_AGENT_FILE — the path "
                "is also at the start of your instructions): where things are and how they work, the owner's decisions "
                "about your work, what burned you. Only what is new, briefly, no tasks and no diary; nothing to add — "
                "write nothing. Answer with one line: what you added, or \"nothing\".")


LANG_NOISE = re.compile(r"https?://\S+|`[^`]*`|(?:~|\.{0,2})/[\w./~-]+|\S+@\S+|\[hub:[^\]]*\]")
LANG_KEEP = 5   # the owner's language is judged by this many of his latest messages with a recognizable language


# scripts that name a single language (code ranges); Latin is not a language: English, German and Turkish cannot be
# told apart by letters (languages spec section 4.2: "latin", then down the chain)
LANG_SCRIPTS = (("ru", ((0x400, 0x4FF),)),
                ("ar", ((0x600, 0x6FF), (0x750, 0x77F), (0xFB50, 0xFDFF), (0xFE70, 0xFEFF))),
                ("hi", ((0x900, 0x97F),)),
                ("ko", ((0x1100, 0x11FF), (0x3130, 0x318F), (0xAC00, 0xD7AF))),
                ("ja", ((0x3040, 0x30FF),)),
                ("zh-CN", ((0x3400, 0x4DBF), (0x4E00, 0x9FFF))))
LANG_WIDE = {"ko": 2, "ja": 2, "zh-CN": 2}   # a character of these scripts is a syllable or a word, not a letter
# the letters are language data (core/langdata.py, `letters`)
LANG_UK = set(langdata.get("uk", "letters", "own", ""))          # letters used only in Ukrainian
LANG_RU = set(langdata.get("ru", "letters", "own", ""))          # letters used only in Russian (of the two)
LANG_RU_OWN = set(langdata.get("ru", "letters", "own_not_south", ""))   # of those, the ones absent from South Slavic
LANG_SOUTH = set(langdata.get("ru", "letters", "south", ""))     # letters of Serbian and Macedonian
# Bulgarian: the hard sign before a consonant (in Russian it is only a separator)
LANG_SOUTH_HARD = re.compile(langdata.get("ru", "letters", "south_hard_sign") or langdata.NEVER)


def owner_lang(text):
    """The language of the owner's text by script: a code (ru, uk, ar, hi, ko, ja, zh-CN) or "latin" for Latin script,
    language unknown. Cyrillic is Russian or Ukrainian by letters that only one of them has (Bulgarian, Serbian,
    Macedonian give None, we do not guess); kana means Japanese, han characters without kana mean Chinese. Links, paths,
    code and email are not speech (09.10: a Russian message with an MR link was classed as "English"); short text (under
    12 letters) gives None. A tie is not Latin: a couple of English words do not make a message English."""
    words = LANG_NOISE.sub(" ", text or "")
    counts = {}
    for c in words:
        o = ord(c)
        code = "latin" if o < 0x250 and c.isalpha() else \
            next((k for k, spans in LANG_SCRIPTS if any(a <= o <= b for a, b in spans)), None)
        if code:
            counts[code] = counts.get(code, 0) + LANG_WIDE.get(code, 1)
    if "ja" in counts:   # Japanese is also written with han characters
        counts["ja"] += counts.pop("zh-CN", 0)
    if sum(counts.values()) < 12:
        return None
    code = max(counts, key=lambda k: (counts[k], k != "latin"))
    if code == "ru":
        low = words.lower()
        # Serbian, Macedonian (their own letters), Bulgarian (the hard sign is a vowel, before a consonant): the hub
        # has no such languages, so we do not count them as Russian; the Russian hard sign is only a separator
        # (as in "podyezd")
        if not any(c in LANG_RU_OWN for c in low) and (any(c in LANG_SOUTH for c in low) or LANG_SOUTH_HARD.search(low)):
            return None
        if sum(c in LANG_UK for c in low) > sum(c in LANG_RU for c in low):
            code = "uk"
    return code


def _lang_vote(value):
    """A history vote: old words become codes; the old "English" and the code en (the pre-language detector called any
    Latin text that) become "latin"."""
    code = forms.lang_code(value)
    return "latin" if code == "en" else code


def _note_lang(st, text):
    """The owner's language is the most frequent among the last LANG_KEEP messages (one message does not switch it)."""
    lang = owner_lang(text)
    if not lang:
        return
    # old words in the vote history become codes: the vote is not thrown off at the seam
    seen = [_lang_vote(x) for x in (st["owner"].get("langs") or [])[-(LANG_KEEP - 1):]] + [lang]
    st["owner"]["langs"] = seen
    st["owner"]["lang"] = max(dict.fromkeys(reversed(seen)), key=seen.count)   # a tie goes to the latest


def known_lang(st):
    """The owner's language: from his last message; after a restart, until he has written, from his messages in the
    state, newest first (09.10: the language rule with no language, and the secretary answered in English again)."""
    if not st:
        return None
    lang = (st.get("owner") or {}).get("lang")
    if lang:
        lang = forms.lang_code(lang)
        # Latin in the rule to sessions means "English", as before languages (spec section 4.2, "only the general
        # rule", with the prompt slice S3 and its evals)
        return "en" if lang == "latin" else lang
    mine = sorted((m for m in (st.get("messages") or {}).values() if m.get("from_owner")),
                  key=lambda m: m.get("created") or 0, reverse=True)
    lang = next((x for x in (owner_lang(m.get("text")) for m in mine) if x), None)
    return "en" if lang == "latin" else lang


def _compact_on_wake(st, sess):
    """A session is woken after sleep or revived, and the conversation is over the threshold: /compact first, then the
    message: the cache is cold after sleep, and an uncompacted conversation would be poured in whole (owner decision
    2026-10-11: compact a session on revive and on wake if it is over the limit, on by default; compact_idle.on_wake
    / min_tokens in hub.json)."""
    from core import parking
    own = st.get("owner") or {}
    asked = sess.pop("compact_wake", None)   # compaction was requested while it slept
    if asked:
        sess["compact"] = asked
    elif own.get("compact_wake", True) and (sess.get("context") or 0) >= (own.get("compact_min") or parking.COMPACT_MIN_TOKENS):
        sess["compact"] = True


def _typed_to(st, sess):
    """The process has already accepted a message: Claude Code writes the conversation to disk on input, before turn.started."""
    return bool(sess.get("ever_started")) and any(
        m["state"] in ("typing", "delivered") for m in _messages_of(st, sess["name"]))


def _stalled_task(st, sess, ts):
    """The id of an assignment nobody is left to continue after a move: the reply is fresh (not older than OPEN_NUDGE;
    beyond that the reminder ladder nudges it), the task is open after the reply, and the executor is free and waits for
    nothing: no `hub waiting`, no schedule checks, no owner, no own messages in the queue or being typed
    (delegations.quiet; a task in the queue or being typed will be typed into the new process and start a turn by
    itself). A sleeping or busy session, or one with no assignment, is not woken."""
    did, _ = delegations.open_task(st, sess["name"])
    d = st["delegations"].get(did or "")
    if (d and delegations.free(st, d, now=ts) is not None and delegations.quiet(st, d, now=ts) is not None
            and ts - (d.get("answered_at") or d["since"]) < delegations.OPEN_NUDGE):
        return did
    return None


def _awaits_turn(msg, name):
    """The message waits for its turn in session name: a turn with its mark delivers it. "Not delivered" after the
    attempts ran out, and the marked turn came later, too (typed right after the /compact of a big conversation, the
    compaction took minutes, and "Not delivered" hung on an answered message)."""
    return bool(msg) and msg["session"] == name and (msg["state"] in ("queued", "typing") or (
        msg["state"] == "failed" and str(msg.get("reason") or "").startswith("not confirmed")))


def hook_lost(st, kind, name, data):
    """A fact the stream-json host put for a core hook cancelled by its timeout (data.fallback, hosts/streamjson
    HookWatch): whether the core still lacks it. The hook writes before Claude Code goes on and hubd writes facts in the
    order they happened (Hub.collect): one the hook managed to write is already folded, and the host's is not written."""
    if name not in st["sessions"] and st["aliases"].get(f"was:{name}") in st["sessions"]:
        name = st["aliases"][f"was:{name}"]   # renamed: its process keeps the old name, as in _apply
    sess = st["sessions"].get(name)
    if not sess:
        return False
    if kind == "session.started" and data.get("source") == "clear":   # /clear in a live process: a new conversation
        return sess["state"] in LIVE and bool(data.get("session_id")) and data["session_id"] != sess.get("session_id")
    if kind == "session.started":
        return sess["state"] == "starting" and data.get("session_id") in (None, sess.get("session_id"))
    if kind == "turn.started":
        return _awaits_turn(st["messages"].get(data.get("marker") or ""), name)
    return False


def spawn_action(sess, delay=False, st=None):
    """Launch a session with its whole set: tools, the project folder policy, the kind. The hub after crashes gets a pause."""
    action = {"do": "spawn", "session": sess["name"], "session_id": sess["session_id"],
              # --resume only if the conversation is on disk (a turn happened); otherwise a fresh launch with the same
              # --session-id: a session with no turns fails on --resume with "No conversation found" (found on the VM 29.09, demo)
              "resume": bool(sess.get("has_history")), "cwd": sess["cwd"], "mode": sess.get("mode"),
              "tools": list(sess.get("tools") or []), "project": sess.get("project") or "all",
              "kind": sess.get("kind") or "work"}
    if minihub.runner(sess) and sess.get("raised_by") and \
            ((st or {}).get("sessions", {}).get(sess["raised_by"]) or {}).get("role") == "agent":
        action["executor_of"] = sess["raised_by"]   # a specialist's executor: knows it is not the specialist itself
    if minihub.on(st or {}, sess):
        action["minihub"] = True   # mini-hub: the addendum and the edit ban in its own folder (host)
    up = minihub.boss(st or {}, sess)
    if up:
        action["bus"] = up         # a mini-hub's executor: no form to the owner, questions go to whoever raised it (host)
    opened = specmode.open_of(st or {}, sess["name"])
    if opened:
        action["spec"] = specmode.projection(opened)   # spec mode: the hook's settings and the line in the prompt (host)
    elif sess.get("task_spec"):
        # raised with --spec, its task not recorded yet (delegation.created follows session.requested): the hook's
        # settings already; the projection comes with the task (the spec_mode action), the line — in its brief
        action["spec"] = {"task": None, "path": None, "boss": sess.get("raised_by"), "open": True}
    if sess.get("spec"):
        action["spec_boss"] = True   # specs on (/spec on): the raiser's rule in the prompt (host)
    if sess.get("effort"):   # its own reasoning level (hub session effort), otherwise from the settings
        action["effort"] = sess["effort"]
    if sess.get("model"):    # its own model (hub session model), otherwise from the settings or Claude Code
        action["model"] = sess["model"]
    lang = known_lang(st)
    if lang:                 # the owner's language goes into the language rule (hosts/streamjson.py: LANGUAGE)
        action["lang"] = lang
    from core import i18n
    ui = i18n.state_lang(st)
    if ui:                   # the human's language from the state goes into a new agent's CLAUDE.md template (hub.json is the host's)
        action["ui_lang"] = ui
    engine = _engine(st, sess)
    account = _pool_of(st, engine).get("active")
    if account:              # a pool account's token (modules/accounts), or a Codex account (core/codexslot.py)
        action["account"] = account
    if engine == "codex" and account:   # the Codex account's folder (CODEX_HOME) — the one the core took (codex.pool)
        home = next((p["home"] for p in (((st or {}).get("codex") or {}).get("pool") or {}).get("profiles") or []
                     if p["name"] == account), None)
        hub_row = next((p["name"] for p in (((st or {}).get("codex") or {}).get("pool") or {}).get("profiles") or []
                        if p.get("hub")), None)
        if hub_row:   # the host falls back to the hub folder when the account's is not fit now: its facts are this row's
            action["hub_account"] = hub_row
        if home:
            action["home"] = home
        else:   # its folder is no longer taken (the daemon moves the pool off it): the hub folder, not marked with it
            account = None
            action.pop("account", None)
    gen = (_pool_of(st, engine).get("refreshed") or {}).get(account)
    if gen:                  # a freshly taken token of the same account (accounts: refresh) marks the limits
        action["account_gen"] = gen
    gate = (st or {}).get("gateway") or {}
    if gate.get("on"):       # via the accounts gateway (modules/gateway): it substitutes the token, switching needs no restart
        action["gateway"] = True
        sess["gateway"] = gate["ident"]
    elif sess.get("gateway"):
        sess.pop("gateway")
    if account or sess.get("account"):
        sess["account"] = account   # which one it was raised with: a turn cut by a limit continues only on another
        if gen:
            sess["account_gen"] = gen   # or on a fresh token of the same one
        else:
            sess.pop("account_gen", None)
    # the agent's own CLAUDE.md (30.09: agents write their own CLAUDE.md); the key is the agent's sessionId; the bus's
    # liaison has the core's profile instead (core/liaison.py), the same on every hub
    profile = sess.get("profile") or (sess["session_id"] if sess.get("role") == "agent" else None)
    if sess.get("liaison"):
        action["liaison"], profile = True, None
        action["about"] = sess.get("about")
    if profile:
        action.update(profile=profile, about=sess.get("about"))
    if st is not None:  # which agents exist goes to every session at start (30.09): by their profile, to them
        action["agents"] = [[s["name"], s.get("about") or ""] for s in st["sessions"].values()
                            if s.get("role") == "agent" and s["name"] != sess["name"]
                            and s["state"] not in ("removed", "closed", "failed")]
    if delay and sess.get("kind") == "hub" and sess["restarts"] > 1:
        action["delay"] = min(HUB_MAX_DELAY, 2 ** (sess["restarts"] - 1))
    refusals.spawn(sess, action)   # Opus's filter moved it to another model: the conversation cut before that turn
    sess["dirty"] = False
    sess.pop("hold_respawn", None)
    sess.pop("reload_asked", None)
    sess.pop("respawn_now", None)
    sess.pop("respawn_minihub", None)
    sess.pop("browser_stale", None)
    sess.pop("plugins_stale", None)   # it launches with the new plugins already, no second restart needed
    return action


ANSWERED_KEEP = 16   # closed cards per session that the page recognizes on a repeated answer
ANSWERED_WHY = 300   # characters of a withdrawal's words kept in the record


def _answered(sess, ev, data, behavior=None, *, ask=None, why=None):
    """How a card was closed: the number of the accepted fact, the decision fingerprint (owner.answer_sig) and the
    client's client_id. For a repeat of the same answer (the client got no receipt and retried from its queue, a second
    client, a page restart in between) the page replies "already accepted" with this number, and tells a different
    answer apart from "no such card". The record also says how it was closed (how, by the fact: answered, dismissed - the
    owner's x, withdrawn - by the session that asked, the hub or the owner), the card's id (id: qN; none for a permission
    form), when (ts), by whom (by: "owner" or the name of the session or the hub) and, for a withdrawal, the words it came
    with (why). A withdrawn card has no reply to repeat: no sig and no client_id, a reader that matches a repeat by them
    never takes it for an answer. The last ANSWERED_KEEP are kept."""
    how = {"question.withdrawn": "withdrawn", "question.dismissed": "dismissed"}.get(ev["type"], "answered")
    by = "owner" if tools.from_owner(data) else data.get("sender") if isinstance(data.get("sender"), str) else None
    rec = {"request_id": data.get("request_id") or (ask or {}).get("request_id"), "seq": ev["seq"],
           "sig": None if how == "withdrawn" else owner.answer_sig(behavior or data.get("behavior"), data.get("answers")),
           "how": how, "id": (ask or {}).get("id"), "ts": ev["ts"], "by": by,
           "why": str(why).strip()[:ANSWERED_WHY] or None if why and how == "withdrawn" else None}
    if isinstance(data.get("client_id"), str) and how != "withdrawn":
        rec["client_id"] = data["client_id"]
    box = [r for r in sess.get("answered") or [] if r["request_id"] != rec["request_id"]]
    sess["answered"] = (box + [rec])[-ANSWERED_KEEP:]


def _take_request(sess, request_id):
    """Take a pending request: by id, or the oldest."""
    for i, req in enumerate(sess["pending"]):
        if request_id in (None, req["request_id"]):
            return sess["pending"].pop(i)
    raise Rejected(f"no pending request {request_id}")


FEED_MAX = 500   # posts in a plugin channel (API 1.2)
POST_MAX = 20000   # characters per post (API 1.22; it was 2000, and the owner's JSON reply did not fit)
POST_BAD_PLUGIN = T("core.reducer.post_bad_plugin", "plugin channel: the plugin name is Latin letters, digits, hyphen")
POST_BAD_TEXT = T("core.reducer.post_bad_text", "post: text of 1 to {max} characters")
POST_NO_SESSION = T("core.reducer.post_no_session", "no session {name}")
POST_WHO = T("core.reducer.post_who", "posts are written by sessions, the owner and plugins")
POST_BY = langdata.legacy("post_by")   # a module's or plugin's post author: a protocol value the page shows (by)
POST_NO_REPLY_TO = T("core.reducer.post_no_reply_to", "no post {id} in {plugin}")


def post_row(ev, data, sessions=None):
    """(plugin, post) of an ext.post event: the row that goes into the plugin channel. sessions — the state's sessions: when
    given, a session author must exist in them (the reducer); without it (an old post read back from the journal, whose
    session may be gone since) the author is not checked. Rejected — the post is not valid."""
    plugin = str(data.get("plugin") or "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", plugin):
        raise Rejected(POST_BAD_PLUGIN)
    text = str(data.get("text") or "").strip()
    if not text or len(text) > POST_MAX:
        raise Rejected(POST_BAD_TEXT.format(max=POST_MAX))
    if data.get("channel") == "session":
        by = data.get("sender") or ""
        if sessions is not None and (sessions.get(by) or {}).get("state") in (None, "removed"):
            raise Rejected(POST_NO_SESSION.format(name=by))
    elif data.get("channel") == "plugin" and data.get("sender"):
        sender = forms.outside_id(data["sender"])
        by = POST_BY["module" if sender.startswith("mod:") else "plugin"].format(name=sender[4:])
    elif tools.from_owner(data):
        by = "owner"
    else:
        raise Rejected(POST_WHO)
    return plugin, {"id": f"p{ev['seq']}", "by": by, "text": text, "reply_to": data.get("reply_to") or None, "ts": ev["ts"]}


def _ext_post(st, ev, data):
    """A post to a plugin channel (API 1.2, owner decision 2026-10-03: a feed for agents, to post by themselves, talk
    and comment on each other). The author is whoever sent it: a session, the owner, a plugin process; it cannot be forged."""
    plugin, post = post_row(ev, data, st["sessions"])
    feed = st.setdefault("feeds", {}).setdefault(plugin, [])
    if post["reply_to"] and not any(p["id"] == post["reply_to"] for p in feed):
        raise Rejected(POST_NO_REPLY_TO.format(id=post["reply_to"], plugin=plugin))
    feed.append(post)
    del feed[:-FEED_MAX]


def _join_note(*notes):
    """Core service notes to the recipient (routing hints), one per line, without wrapper brackets in the sender's text."""
    return "\n".join(n.strip() for n in notes if isinstance(n, str) and n.strip())


FYI_FOR = "for"   # the start of the one-line note of an "FYI" passed to the raiser (_to_raiser, _merge_fyi)
# the same start in notes written before the translation: state keeps them (_merge_fyi replaces them too)
FYI_FOR_RU = langdata.legacy("fyi_for")


def _to_raiser(st, name, fyi=False):
    """A message to an executor name that has handed in: (to whom: whoever raised it, or the hub; a note with its last result).
    An "FYI" gets one line instead: it asks nothing, and the raiser already has the result and the wake command."""
    sleeper = st["sessions"][name]
    last = next((d.get("reply") for d in sorted(st["delegations"].values(), key=delegations.born, reverse=True)
                 if d["session"] == name and d.get("reply")), None)
    up = identity.address(st, sleeper.get("raised_by") or "") if sleeper.get("raised_by") else None
    up = up if up and st["sessions"][up]["state"] not in ("removed", "closed", "failed") else _hub(st)
    if fyi:
        return up, f"{FYI_FOR} {name} — it is asleep"
    # a routing note: only the recipient's model reads it (the typed tail, _typed)
    return up, (f"\n\n(This was written to executor {name} — it has handed in its task and is asleep"
                + (f"; its last result: «{last[:600]}»" if last else "")
                + f". To wake it with its conversation — hub session wake {name} \"<what to do>\" (the owner, the hub or "
                  "whoever raised it).)")


def _bus_note(st, name, up):
    """The routing note to mini-hub up that got the owner's words meant for its executor name (the bus): they did not
    reach it, the mini-hub passes them on — a sleeping executor by a wake, a working one by a message. A mini-hub wakes
    its executor only to rework a task it gave it (`--about dN`, _raiser_terms): the note names the latest such task and
    the others; with none (it did not raise it, or the task went to the archive) the mini-hub asks the hub to wake it."""
    sess = st["sessions"].get(name)
    done = [d["id"] for d in sorted(delegations.done_for(st, name, up), key=delegations.born)] \
        if sess and sess.get("raised_by") == up else []
    if not executors.dormant(sess):
        how = f'to pass them on: hub send {name} "<what to do>"'
    elif done:
        how = (f"it has handed in its task and is asleep — to pass them on with its conversation: hub session wake {name} "
               f'--about {done[-1]} "<what to do>"' + (f" (its other tasks: {', '.join(done[:-1])})" if done[1:] else ""))
    else:
        # the hub wakes it itself: the words sent back to the hub as a message would come back here (the bus)
        hub = _hub(st) or "hub"
        how = (f"it has handed in its task and is asleep, and you cannot wake it: ask the hub to — hub send {hub} "
               f'"wake {name} with hub session wake: <what to do>"')
    return (f"(This was written to your executor {name}; the core did not deliver it to {name} — the owner's words to "
            f"your executors come to you. {how[0].upper() + how[1:]}.)")


def _merge_fyi(st, msg, ts):
    """The same "FYI" (sender, text, files) is already waiting at this session: a sender who writes to an executor and to
    whoever raised it (`hub send A --fyi T; hub send B --fyi T`) would give the raiser one text twice. The second is
    accepted but not added to the block: it is marked delivered with the first, whose note names every address."""
    first = next((m for m in st["messages"].values()
                  if m["state"] == "aside" and m["session"] == msg["session"] and m is not msg
                  and (m.get("sender"), m["text"], m.get("attachments")) == (msg.get("sender"), msg["text"], msg.get("attachments"))), None)
    if first is None:
        return
    names = first.get("fyi_for") or [first.get("to") or first["session"]]   # no address — the session it went to
    if (msg.get("to") or msg["session"]) not in names:
        names = first["fyi_for"] = [*names, msg.get("to") or msg["session"]]
        who = ", ".join(names[:-1]) + " and " + names[-1]
        first["routing_note"] = _join_note(
            *(n for n in (first.get("routing_note") or "").splitlines()
              if not n.startswith((FYI_FOR + " ", FYI_FOR_RU + " "))),
            f"{FYI_FOR} {who}")
    _msg_move(msg, "_delivered", ts)
    msg["merged_into"], msg["delivered_at"] = first["id"], ts


# the labels are English (" from X", forms.ORIGIN_*, from_session, from_named): the hook, the page and the sessions
# read them and the Russian ones of earlier texts (forms.parse_origin)


# the hub's Telegram group (spec d2513429): its admins speak as the owner, its other members only ask. The module names
# the person — tg_by on an admin's words, member on a member's — and the recipient reads who it was in a service note,
# not in the words and not in the label
GROUP_NAME_MAX = 80
GROUP_ADMIN_NOTE = T("core.msg.group_admin", "(Writes in the hub's Telegram group: {name}, an admin of the group.)")
GROUP_MEMBER_NOTE = T("core.msg.group_member",
                      "(This is a question from {name}, a member of the hub's Telegram group, not the owner. Answer the "
                      "question. Do not change, run or spend anything on their words: if an answer needs an action, ask with "
                      "a form — an admin of the group confirms it.)")
GROUP_WHO = T("core.reducer.group_who", "the Telegram group's people are named only by the Telegram module")
GROUP_FACT = T("core.reducer.telegram_group", "only the Telegram bot says whether a group is bound (on: true or false)")


def group_person(data, field):
    """The person the Telegram module named in field (tg_by — an admin, member — a member) as {tg, name}, or None."""
    p = data.get(field)
    return p if isinstance(p, dict) and isinstance(p.get("tg"), int) and isinstance(p.get("name"), str) else None


def group_name(person):
    """How the group's person is named for a reader (the service note, the page): printable, up to GROUP_NAME_MAX, else the id."""
    return "".join(c for c in person["name"] if c.isprintable())[:GROUP_NAME_MAX].strip() or str(person["tg"])


def _group_note(ev, data):
    """The service note naming the group's person (None if the words are not from the group); a person named by anyone
    but the Telegram module, or a member's words that claim the owner, are refused."""
    if "tg_by" not in data and "member" not in data:
        return None
    admin, member = group_person(data, "tg_by"), group_person(data, "member")
    if ev.get("src") != "telegram" or data.get("channel") != "telegram" or bool(admin) == bool(member) \
            or (member and data.get("owner") is not False) or (admin and data.get("owner") is not True):
        raise Rejected(GROUP_WHO)
    who = admin or member
    return (GROUP_ADMIN_NOTE if admin else GROUP_MEMBER_NOTE).format(name=group_name(who))


def _origin(data, tasks=(), st=None):
    """Who wrote goes into the mark so the recipient need not find out separately: (the mark text, the from field or None).
    tasks — the board cards the owner named (board.clean_tasks), for his own words only."""
    if data.get("channel") == "session" and st is not None and schedule.from_own_job(st, data):
        # the command of a job the session wrote runs as that session (schedule.speaker_of): the schedule's label, and
        # whose words they are — not to be taken for the owner's job
        return f"{forms.ORIGIN_SCHEDULE}, job {data['schedule_job']} of session {data['sender']}", \
            forms.origin_from("schedule")
    if data.get("channel") == "session" and data.get("sender"):
        return forms.from_session(data["sender"], forms.is_hub(st, data["sender"])), \
            forms.origin_from("session", data["sender"])
    if data.get("channel") == "plugin" and data.get("sender"):   # a plugin or core module process (hub-up)
        sender = forms.outside_id(data["sender"])   # the core writes mod:<name> / ext:<name>, old plugins a bare name
        if sender.startswith("mod:"):
            return forms.from_named("module", sender[4:]), forms.origin_from("module", sender[4:])
        return forms.from_named("plugin", sender[4:]), forms.origin_from("plugin", sender[4:])
    if data.get("schedule_job"):
        return f"{forms.ORIGIN_SCHEDULE}, job {data['schedule_job']}", forms.origin_from("schedule")
    if group_person(data, "member"):   # a member of the hub's Telegram group: asks, is not the owner (_group_note)
        return forms.ORIGIN_MEMBER + (f", in reply to: «{_quote(data['quote'])}»" if data.get("quote") else ""), \
            forms.origin_from("member")
    if data.get("owner"):
        # an owner reply to an executor's answer goes straight to the executor
        origin = forms.owner_origin(data) + (f", a reply to your answer on {data['reply_to']}" if data.get("reply_to") else "")
        origin += f", in reply to: «{_quote(data['quote'])}»" if data.get("quote") else ""
        return origin + (f", {board.tasks_label(tasks)}" if tasks else ""), forms.origin_from("owner")
    return "", None


QUOTE_MAX = 150


def _quote(text):
    """The start of the message the owner replied to (30.09): on one line, without "]", since the mark is read up to "] "."""
    line = " ".join(str(text).split()).replace("]", ")").replace("[", "(")
    return line[:QUOTE_MAX] + ("…" if len(line) > QUOTE_MAX else "")


SEND_NO_SESSION = T("core.reducer.send_no_session", "no session {name}: who is there — hub status; to the hub — hub send hub")
SEND_GONE = T("core.reducer.send_gone",
              "session {name} {what} on {when}: the message cannot be delivered. To bring it back with its conversation — "
              "hub session revive {name} (the hub/the owner); who is there — hub status")
SEND_OWN_SLEEPER = T("core.reducer.send_own_sleeper",
                     "executor {name} has handed in and is asleep: a message does not wake it, and from you it would only "
                     "come back to you. To give it work — hub session wake {name} --about {task} \"<what to do>\"")
GONE_WORDS = {"removed": T("core.reducer.gone_removed", "was removed"),
              "closed": T("core.reducer.gone_closed", "was closed"),
              "failed": T("core.reducer.gone_failed", "crashed")}
OWNER_TO_EXECUTOR = T("core.msg.owner_to_executor", "The owner to executor {name}:")
TRAIL_WOKEN = T("core.msg.trail_woken", "{name} woken: {text}")
CARD_NOT_OPEN = T("core.reducer.card_not_open",
                  "--card {card}: {name} has no open card {card} asked before the owner's words {word} — the open ones: "
                  "hub status")


SPEC_OWNER_COPY = T("core.msg.spec_owner_copy",
                    "The owner wrote to executor {name} directly (task {task}, its spec is open) — for your information, "
                    "no need to answer or pass it on: {text}")
SPEC_COPY_CHARS = 2000


def _received(st, ev, data, actions=None, queued=None):
    """A session message. queued is a wake (a rework dN, or True) that got its place in the executor queue (_wake_ready): the
    wake checks were already passed when it was queued."""
    mid = f"m{ev['seq']}"
    if mid in st["messages"]:
        raise Rejected("duplicate message id")
    if "attachments" in data:
        data = {**data, "attachments": clean_attachments(data["attachments"])}
    failed = clean_failed(data.get("attach_failed"))
    _may_rush(st, data, module=True)
    # "for your information" (`hub send --fyi`): only an ordinary message; an urgent one, the owner's words, an
    # addition to a task, a wake, a command to itself, an answer to a card all wait for a turn or action, and this does not
    fyi = data.get("fyi") is True
    if fyi and any(data.get(k) for k in ("now", "for", "about", "wake", "slash", "card", "action", "comment")):
        raise Rejected(FYI_PLAIN)
    # he writes from the page or Telegram himself, so he is present (09.10: /away with no end was forgotten for a
    # day and all sessions' forms piled up silently); from a client via a module handle too; CLI is not necessarily
    # him: modules write that way as well
    if st["owner"]["state"] == "away" and data.get("owner") is True and (
            data.get("channel") in ("web", "telegram") or guards.MODULE_CHANNEL.fullmatch(str(data.get("channel")))):
        _owner_event(st, ev, data, "owner.back")
    to = (data.get("to") or "").strip()
    # no address means the hub: "wrote without a reply, you are writing to the hub"; "hub" is the hub too (the skill
    # teaches `hub send hub`, while the hub session is named by the settings, hub-core: on 01.10 eight messages to
    # the hub silently failed to arrive)
    name = identity.address(st, to) if to else _hub(st)
    if name is None and to and data.get("channel") == "session":
        raise Rejected(SEND_NO_SESSION.format(name=to))
    why = busstate.message_refusal(st, data, name, ev["ts"])   # the liaison writes only through hub bus pass; --to a bus contact
    if why:
        raise Rejected(why)
    tasks = board.clean_tasks(data.get("tasks")) if tools.from_owner(data) else []   # the board cards he picked on the page
    origin, frm = _origin(data, tasks, st)
    # the client named a card (/api/send {card}): an answer to it is valid only for an open card of this session and
    # a message that can be an answer (as below for a form: no quote, attachments, action); otherwise card is dropped
    # and does not route words around the mini-hub straight to the executor. If the named card is gone (closed, someone
    # else's), the words are delivered but do not become an answer to another card or form: he answered the one he
    # saw (a stale client snapshot)
    gone_card = False
    if data.get("card") and not data.get("for") and (any(
            data.get(k) for k in ("quote", "reply_to", "attachments", "redirected", "slash", "action")) or not any(
            a["id"] == data["card"] for a in owner.open_asks(st["sessions"].get(name) or {}, ev["ts"]))):
        data = {k: v for k, v in data.items() if k != "card"}
        gone_card = True
    # the owner copied an open question's card and appended an answer ("<question> go"): the verbatim text of one card
    # is an answer to it with the rest of his words, to that session (06.10: he pasted the card into the hub chat, it
    # stayed open, and he answered again on the page). A paraphrase or an ambiguous case goes to the hub as is
    # (owner.quoted_card)
    if tools.from_owner(data) and name and not backbone.command(data.get("text")) and not any(
            data.get(k) for k in ("quote", "reply_to", "attachments", "for", "about", "redirected", "slash", "action",
                                  "card", "fyi")):
        card = owner.quoted_card(st, data.get("text"), ev["ts"], only=None if name == _hub(st) else name)
        if card:
            name, data = card[0], {**data, "text": card[2], "card": card[1]["id"]}
            gone_card = False   # he copied an open card verbatim: an answer to it
    # routing hints (who was written to, how to wake) are a service note to the recipient, not the sender's words: the
    # text (feed, quote, edit, a reply to it) holds only his words; the recipient gets the note as a block when typed (_typed)
    routing = []
    group_note = _group_note(ev, data)   # who in the hub's Telegram group wrote it
    if group_note:
        routing.append(group_note)
    addressed = None   # the owner wrote to a session that is gone or never was, the words went elsewhere: its name (the feed row)
    was = st["sessions"].get(to) if to else None
    if was and was.get("role") == "executor" and was["state"] == "removed" and name and name != to:
        # a message to a departed executor goes to whoever raised it, with its last result (spec, case 8)
        last = next((d.get("reply") for d in sorted(st["delegations"].values(), key=delegations.born, reverse=True)
                     if d["session"] == to and d.get("reply")), None)
        note = f"\n\n(This was written to executor {to} — it has already left" + (
            f"; its last result: «{last[:600]}»)" if last else ")")
        routing.append(note.strip())
        addressed = to if tools.from_owner(data) else None
    relayed = None
    up = minihub.hub_of(st, st["sessions"].get(name)) if name and not queued else None
    # while the executor's spec is open the owner talks to it himself, the mini-hub gets a copy (spec mode: owner decision
    # 2026-11-09); once the spec is accepted or dropped it is the bus again
    spec_task = specmode.direct(st["sessions"].get(name), specmode.open_of(st, name)) \
        if up and tools.from_owner(data) else None
    if up and (tools.from_owner(data) or data.get("for")) and not (data.get("wake") or data.get("card") or spec_task
                                                                     or tools.from_owner(data) and data.get("comment")
                                                                     or backbone.command(data.get("text"))):
        # the bus (owner decision 2026-10-15: the owner talks only to the mini-hub): his words to a mini-hub's executor
        # (page, Telegram, a reply in its feed, relayed by the hub) go to the mini-hub, which passes them on itself. A
        # wake, a Claude Code command and an answer to an open card are actions on the session itself and go to it; so
        # does a comment in "Files": the owner holds that thread with it, and the reply in the thread is its turn
        # result (22.10: spec edits went through the mini-hub's retelling)
        data = {**data, "text": coretext.join(OWNER_TO_EXECUTOR.format(name=name), " ", str(data.get("text") or "")),
                "redirected": True}
        data.pop("about", None)   # an addition to the executor's task would return it to the executor itself
        # the mini-hub reads in the note that the words did not reach the executor and how to pass them on (10.11: the
        # hub relayed them with --now to a sleeping executor, the mini-hub took "The owner to executor X" for a copy, as if
        # the hub had woken it itself, and ended its turn with "—"; the executor slept four more hours)
        routing.append(_bus_note(st, name, up))
        relayed, name = name, up   # the executor's feed shows the words too, "passed to <mini-hub>" (views._items)
    sleeper = st["sessions"].get(name) if name else None
    reopen = None
    if executors.dormant(sleeper):
        if data.get("wake"):
            # wake explicitly: the owner or the hub at once; whoever raised it through the ceilings and the queue of a new
            # request (a mini-hub - to rework its own task, core/minihub.py); one that waited in the queue is already checked
            if queued:
                reopen = queued if isinstance(queued, str) else None
            elif _wake_who(st, sleeper, data) == "raiser":
                _wake_gate(st, sleeper, direct=False)
                reopen = _raiser_wakes(st, ev, data, sleeper)
                if reopen is None:
                    return   # no room: the wake waits in the core's queue (case 3) and is raised by itself
            else:
                _wake_gate(st, sleeper, direct=True)
            sleeper["state"] = step(SESSION, "retired", "_unretire")[0]   # woken: now as for a sleeping session (new_message)
            delegations.trail(st, _last_task(st, name), delegations.BY_CORE,
                              TRAIL_WOKEN.format(name=name, text=str(data.get("text") or "")[:200]))
        else:
            # a message does not wake a sleeper: it goes to whoever raised it (or the hub), with its last result
            up, note = _to_raiser(st, name, fyi)
            if data.get("channel") == "session" and data.get("sender") == up:
                # the sender raised it: "to whoever raised it" is its own queue, the sleeper would get nothing
                raise Rejected(SEND_OWN_SLEEPER.format(name=name, task=_last_task(st, name) or "<dN>"))
            routing.append(note.strip())
            data = {**data, "redirected": True}
            name = up
    # a deleted or crashed session gets neither "accepted" nor silence (owner decision 2026-10-03: never lose or
    # freeze; on 30.09 the owner's decisions went to a deleted session and vanished): a session gets a refusal with a
    # reason; the owner's words go to the hub with a note so that it passes them to whoever needs them
    gone = st["sessions"].get(name) if name else None
    if gone and gone["state"] in ("removed", "closed", "failed") and gone.get("kind") != "hub":
        when = owner._local(gone["since"], st["owner"].get("tz") or "UTC").strftime("%d.%m %H:%M")
        what = GONE_WORDS[gone["state"]]
        if data.get("channel") == "session" or data.get("schedule_job"):
            raise Rejected(SEND_GONE.format(name=name, what=what, when=when))
        if tools.from_owner(data) and _hub(st):
            origin += f", addressed to {name}, but {name} is gone ({what} on {when}) — pass it on to whoever needs it"
            addressed, name = addressed or name, _hub(st)   # a removed executor's raiser gone too: whom he wrote to
            data = {**data, "redirected": True}   # not an answer to the hub's form (analysis 04.10)
    elif name is None and NAME.fullmatch(to) and tools.from_owner(data) and not data.get("schedule_job") and _hub(st):
        # no such session at all (a typo, a client's stale list): the owner's words are not failed as "unknown address"
        # — they go to the hub with the addressee named, as for a gone one above. Only a session-shaped name: it goes
        # into the hub's origin line as is
        origin += f", addressed to {to}, but there is no session {to} — pass it on to whoever needs it"
        addressed, name = to, _hub(st)
        data = {**data, "redirected": True}
    # the owner's words to the hub, relayed to a session (`hub send <session> --for mN`): we check that it is his
    # message to the hub, not older than a day, and only from the hub (07.10: a "Roll it out" from Telegram left the
    # session's card open and the result went elsewhere than to him)
    word = None
    if data.get("for") and data.get("channel") == "session":
        word = st["messages"][tools.owner_word(st, data, ev["ts"])["message"]]
        # `--card qN`: the hub names which card he answered (07.10: a session with two cards, the core does not guess)
        tgt = st["sessions"].get(name) or {}
        if data.get("card") and not any(a["id"] == data["card"] and owner.before(a, word["created"])
                                        for a in owner.open_asks(tgt, ev["ts"])):
            raise Rejected(CARD_NOT_OPEN.format(card=data["card"], name=name, word=word["id"]))
    # an addition to an assignment (`hub send <agent> --about <dN|mN>`, the hub on 03.10: an addition went to someone
    # other than who did the task) goes to the session that holds the assignment: the addressee's executor
    about = str(data.get("about") or "").strip()
    task = next((d for d in st["delegations"].values() if about and about in (d["id"], d.get("for"), d.get("message"))
                 and d["state"] != "done"), None) if name else None
    if task:
        data = {**data, "about_task": task["id"]}
        holder = st["sessions"].get(task["session"]) or {}
        # to whoever holds the task: the addressee's executor (analysis 05.10: an addition to an auto-executor's task went
        # to the specialist)
        mine = holder.get("raised_by") == name
        if task["session"] != name and mine and holder.get("state") not in ("removed", "closed", "failed") \
                and not executors.dormant(holder):
            name = task["session"]
        origin += f", an addition to task {task['id']}"
    if tools.from_owner(data) and name and name == _hub(st) and not (data.get("quote") or data.get("reply_to")) \
            and first_word(st):  # a reply to an answer: the conversation is already going
        # the owner's very first message to the hub is an introduction (owner decision 2026-10-02: onboard on the first greeting);
        # the hub that greeted them first (_owner_invited) has done it: what is left of it is the address book
        origin += FIRST_AFTER_INVITE if st["owner"].get("invited") else \
            ", the first message: first introduce the owner to the hub following ONBOARDING.md"
    typed = _command(st, data, name)
    if fyi and typed:
        raise Rejected(FYI_PLAIN)
    # the core's phrases in a text a module or process of the hub sent (the update notes to Hub Developer, a comment of
    # "Files", the watcher): their marks ride in the event (l10n), the owner reads them in his language (core/coretext.py)
    body = data.get("text", "")
    if data.get("l10n") and not typed:
        body = coretext.restore(body, data.get("l10n"))
    msg = new_message(st, mid, name, body.lstrip() if typed else body, ev["ts"],
                      origin=origin, to=data.get("to"), **typed, **({"frm": frm} if frm else {}),
                      attachments=list(data.get("attachments") or []), from_owner=tools.from_owner(data),
                      **({"attach_failed": failed} if failed else {}),
                      **({"routing_note": _join_note(*routing)} if routing else {}),
                      **({"relayed_for": relayed} if relayed else {}),   # the bus: whom the words were for
                      **({"addressed": addressed} if addressed else {}),   # gone or never was: the words went elsewhere
                      **({"tasks": tasks} if tasks else {}),   # the board cards he named (the feed shows them as chips)
                      # where the owner wrote from (telegram, web, cli); a session is in its own turn's chain (core/chain.py)
                      **({"channel": data.get("channel")} if tools.from_owner(data) else {}),
                      # a schedule script's message on behalf of a session (`schedule_job`) is not from its turn: it
                      # takes neither the chain nor the "schedule turn" mark from its turn
                      **({"root": chain.turn_root(st, data.get("sender"))}
                         if data.get("channel") == "session" and not data.get("schedule_job")
                         and chain.turn_root(st, data.get("sender")) else {}),
                      **({"via_schedule": True}
                         if data.get("channel") == "session" and not data.get("schedule_job")
                         and chain.turn_scheduled(st, data.get("sender")) else {}),
                      **({"sender": data["sender"]} if data.get("channel") == "session" and data.get("sender") else {}),
                      **({"about": data["about_task"]} if data.get("about_task") else {}),
                      # a page button writes on the owner's behalf ("Install" in "About", "Repair plugin"): an action,
                      # not an answer to a card or form (09.10: "Install the hub update" closed a "Send the PR?" card)
                      **({"action": True} if tools.from_owner(data) and data.get("action") is True else {}),
                      **({"comment": str(data["comment"])} if tools.from_owner(data) and data.get("comment") else {}),
                      **({"fyi": True} if fyi else {}))
    busstate.message_taken(st, ev, data, msg)   # a session's request to the liaison: its contact, and an outcome to wait for
    if spec_task and msg["session"] == name and not typed and not fyi:
        # the copy "for your information" to the mini-hub: no turn, it rides with its next message (_ride); a command
        # (/compact) is an action on the session, not words
        words = str(data.get("text") or "")
        new_message(st, mid, up, SPEC_OWNER_COPY.format(
            name=name, task=spec_task["id"],
            text=(words[:SPEC_COPY_CHARS] + "…" if len(words) > SPEC_COPY_CHARS else words)
            + attachments_note(data.get("attachments"))), ev["ts"], origin=forms.ORIGIN_CORE, fyi=True)
    if fyi and msg["state"] == "aside":
        _merge_fyi(st, msg, ev["ts"])
    if reopen:   # a rework of an executor's task: it is in progress again, and the result of the turn on this message answers it
        delegations.continued(st, name, msg["id"], ev, reopen)
    if tools.from_owner(data):
        _note_lang(st, data.get("text"))   # the owner's language goes to sessions at launch (09.10: after a compact they wrote English)
    if word and msg["session"] == name:
        _relayed_answer(st, msg, word, ev["ts"], data.get("card"))
    if data.get("now"):
        _rush(st, msg, ev["ts"], actions)
    elif data.get("channel") == "session" and minihub.boss(st, st["sessions"].get(name)) == data.get("sender") \
            and st["sessions"][name].get("bus") == data.get("sender"):
        # a mini-hub to its working executor: a correction between its steps, not after the turn (the same session)
        _rush(st, msg, ev["ts"], actions)
    me = st["sessions"].get(data.get("sender") or "") if data.get("channel") == "session" and not queued else None
    # it wrote itself (delegations.on_result); also after an empty result while the turn is still running (07.10)
    # an FYI the core delivered as an ordinary message (fyi_wakes) is written to it, as an ordinary one; it still answers
    # no card or form below: the sender said it is not an answer
    if me is not None and (not fyi or msg.get("fyi_woke")) and not data.get("schedule_job"):
        me.setdefault("sent_in_turn", []).extend(x for x in (data.get("to"), name) if x)
        # the longest one message to each: a short line is not the turn's result, nor are several (got_result)
        chars = me.setdefault("sent_chars", {})
        for x in {data.get("to"), name} - {None}:
            chars[x] = max(chars.get(x, 0), len((data.get("text") or "").strip()))
    if msg.get("from_owner"):
        st["owner"]["greeted"] = True
    if name is None:
        msg["state"], msg["reason"] = "failed", "unknown address" if to else "no hub session"
    elif msg.get("from_owner"):
        st["sessions"][name].pop("attention", None)  # the owner answered the session: the "waits for you" mark is cleared
        # an answer to an open form is only a direct message to this session: not a redirected one (another session's
        # words), not a reply quoting something else, with no attachments (they cannot go into a form answer), not a
        # command (analysis 04.10)
        if not (data.get("redirected") or data.get("quote") or data.get("reply_to") or data.get("attachments")
                or typed or msg.get("action") or gone_card or fyi):
            _owner_answers_form(st, ev, st["sessions"][name], msg, actions)
            # the hub's question is a card: the owner's words to the hub are the answer to the single open one (owner
            # decision 2026-10-04: a text reply to an ask should clear the question)
            sess = st["sessions"][name]
            # one asked without him and not yet shown counts only if named (copied) by him himself
            # the core's bus cards are answered only by their buttons (core/busstate.card_answered), never by words
            asks = [a for a in owner.open_asks(sess, ev["ts"]) if (owner.before(a, ev["ts"]) or a["id"] == data.get("card"))
                    and not busstate.is_card(a)]
            # with two or more open, the words answer none (answer with a card option or by quoting it)
            if len(asks) == 1 or any(a["id"] == data.get("card") for a in asks):
                ask = next((a for a in asks if a["id"] == data.get("card")), asks[-1])   # the copied one, if any
                sess["asks"].remove(ask)
                st["owner"]["reminders"].pop(f"q:{name}:{ask['request_id']}", None)
                msg["answers_question"] = ask["id"]
                # the raw answer: the feed shows it on one line, without the "answer to question ..." prefix
                msg["answer_body"] = msg["text"]
                if ask.get("delegation"):
                    msg["for_delegation"] = ask["delegation"]
                q = owner.question_text(ask)
                coretext.prepend(msg, ANSWER_HEAD.format(id=ask["id"], question=q[:120]) + " ")
        if minihub.runner(st["sessions"][name]) and not (data.get("redirected") or msg.get("action")):
            # the owner's words reached the executor itself (not redirected, not a page button): it is the owner's
            # interlocutor, not a handed-in task, so it does not leave on the remove_after_hours deadline
            # (core/executors.retiring; on 20.10 the deadline deleted a hub's executor the owner had talked to for two days)
            st["sessions"][name].setdefault("owner_talks", ev["ts"])
        seen = st["owner"].setdefault("seen", {})     # and he has seen everything it sent earlier
        seen[name] = max(seen.get(name, 0), ev["seq"])


# the heads of the owner's answers in a message text: forms.HEADS strips them from a name by substance (executors.auto_name)
ANSWER_HEAD = T("core.msg.answer_head", "Answer to question {id} («{question}»):")
YOUR_ANSWER_HEAD = T("core.msg.your_answer_head", "This is the user's answer to your question {id} («{question}»)")
PASSED_BY_HUB = T("core.msg.passed_by_hub", ", passed on by the hub")
TRAIL_ANSWER_VIA_HUB = T("core.msg.trail_answer_via_hub", "answer to {id} via the hub: {text}")
SLASH_NO_COMMAND = T("core.reducer.slash_no_command", "a command starts with \"/\": hub slash \"/<skill> <arguments>\"")
SLASH_OWN_ONLY = T("core.reducer.slash_own_only",
                   "hub slash types a command only into your own session; for another — ask the hub")
SLASH_ENDS = T("core.reducer.slash_ends", "/{cmd} ends the session — only the owner types it")
SLASH_SCAFFOLD = T("core.reducer.slash_scaffold",
                   "{name} is a mini-hub: an executor drives the scaffold — hub task run <{name}-gist> --work normal|risky "
                   "\"… via /{cmd}\"")
SLASH_SCAFFOLD_ASK = T("core.reducer.slash_scaffold_ask", "; or ask {name} to assign it")


def _command(st, data, name):
    """A Claude Code command (`/ship ...`) from the owner, the hub or the session itself (`hub slash`) is typed as a
    human would type it: the command first, the mark after ({"command": True}). A turn on its own command is a turn of
    the same task (for_delegation). From another session, or redirected, it is plain text: sessions do not type
    commands for each other (07.10: the hub's "/gp-work" arrived as text, the mark was in front, and the repo
    scaffold did not start)."""
    cmd = backbone.command(data.get("text"))
    sender = data.get("sender") if data.get("channel") == "session" else None
    if data.get("slash"):
        if not cmd:
            raise Rejected(SLASH_NO_COMMAND)
        if not sender or sender != name:
            raise Rejected(SLASH_OWN_ONLY)
    if not cmd or not name or data.get("redirected"):
        return {}
    by_hub = (st["sessions"].get(sender or "") or {}).get("kind") == "hub"
    if not (tools.from_owner(data) or by_hub or sender == name):
        return {}
    if cmd in backbone.ENDS_SESSION and not tools.from_owner(data):
        raise Rejected(SLASH_ENDS.format(cmd=cmd))
    out = {"command": True}
    sess = st["sessions"].get(name) or {}
    if minihub.on(st, sess) and cmd in ((sess.get("info") or {}).get("backbone") or ()):
        # the scaffold opens a working copy, and the core rejects the mini-hub's edits in it (16.10): its executor drives
        # it. Which skills are the scaffold, the host learns from disk at start (session.info); before the first start
        # there is no list, so no refusal
        raise Rejected(SLASH_SCAFFOLD.format(name=name, cmd=cmd)
                       + ("" if sender == name else SLASH_SCAFFOLD_ASK.format(name=name)))
    if cmd in ((sess.get("info") or {}).get("builtins") or backbone.BUILTINS):
        out["builtin"] = True   # without a mark: it has no hook, and the end of the input is its arguments (07.10: /effort)
    if cmd in ("model", "effort") and tools.from_owner(data) and sess:
        _remember_choice(st, sess, cmd, str(data.get("text") or "").split()[1:2])
    d = st["delegations"].get(sess.get("delegation") or sess.get("last_task") or "")
    if sender == name and d and d["session"] == name and d["state"] in ("working", "replied", "stopped"):
        out["for_delegation"] = d["id"]
    return out


def _remember_choice(st, sess, cmd, arg):
    """The owner's `/model X`, `/effort X`: his choice stays with the session (owner decision 2026-10-13: if the user
    chose another one, no need to change it): in a session with no screen Claude Code keeps it "for this session only"
    and loses it on a --resume launch, so the core raises the session with it, as after `hub session model|effort`. No
    need to restart: it has already switched."""
    from core.catalog import EFFORTS, known_model
    value = arg[0] if arg else ""
    if not value:
        return
    host = (sess.get("info") or {}).get("host")
    if host == "codex" or not host and (str(sess.get("model") or "").startswith("codex/") or _codex_whole(st)):
        # Codex (experimental): the Codex host decides what it takes — kept by its answer (_codex_chosen); a
        # Codex session before its first session.info by its model, or every one on a hub entirely on Codex
        return
    if (cmd == "model" and known_model(value, catalog.codex_state(st))) or (cmd == "effort" and value in EFFORTS):
        sess[cmd] = value
    elif cmd == "model":   # default clears it; one unknown to the core (opusplan, sonnet[1m]): Claude Code may have switched:
        sess.pop(cmd, None)   # do not restore the earlier choice over it at launch (review 13.10)
    # all effort values are known: Claude Code would not accept a typo, so the earlier choice stays


def _codex_chosen(st, sess, msg, chosen):
    """Codex (experimental): the Codex host took the owner's typed /model or /effort (its answer's chosen,
    hosts/codex_cmds): kept for the session as _remember_choice keeps Claude's — what the host refused (a model Codex
    lacks or hides, an effort the model lacks) is never kept. Only the owner's command and only the value he typed."""
    cmd = backbone.command(msg.get("text"))
    words = str(msg.get("text") or "").split()
    if not sess or not msg.get("from_owner") or cmd not in ("model", "effort") or len(words) < 2 \
            or not isinstance(chosen, dict) or chosen.get(cmd) != words[1]:
        return
    from core.catalog import EFFORTS, known_model
    value = words[1]
    if (cmd == "model" and value.startswith("codex/") and known_model(value, catalog.codex_state(st))) or \
            (cmd == "effort" and value in EFFORTS):
        sess[cmd] = value


def _delegated_words_answer_form(st, ev, d, actions):
    """The owner answered in text to the hub rather than to the session with an open form; the hub assigned his words
    to it (04.10: a text reply to an ask should clear the question; the assignment sat in the queue behind the form).
    The owner's words are the answer to the form, and the result of the resumed turn is the answer to the assignment."""
    src, msg, sess = st["messages"].get(d["for"] or ""), st["messages"].get(d["message"]), st["sessions"][d["session"]]
    if not (src and src.get("from_owner") and msg and msg["state"] == "queued") or src.get("attachments") \
            or src.get("action"):
        return
    # an answer is only words written after the question (05.10: a new request to the hub 2 s before the card closed
    # it), and only to a card the hub named (`hub task new --for mN --card qN`): without it this is a new task, not an
    # answer (10.10: the only open card was closed by the owner's new task)
    asks = [a for a in owner.open_asks(sess, ev["ts"])
            if owner.before(a, src["created"]) and d.get("card") and a["id"] == d["card"]]
    if asks:   # a question card: the task with his words is the answer; the question is closed and the task arrives with a note
        ask = asks[-1]
        sess["asks"].remove(ask)
        st["owner"]["reminders"].pop(f"q:{sess['name']}:{ask['request_id']}", None)
        msg["answers_question"] = ask["id"]
        coretext.prepend(msg, coretext.join(YOUR_ANSWER_HEAD.format(id=ask["id"], question=owner.question_head(ask)),
                                            ".\n"))
        return
    if owner.open_asks(sess, ev["ts"]):
        return   # the question was asked after his words: the task goes its own way
    _owner_answers_form(st, ev, sess, msg, actions, text=d["words"], after=src["created"])
    if msg.get("answered_form"):
        sess["delegation"] = sess["last_task"] = d["id"]


RELAYED_TAIL = T("core.msg.relayed_tail",
                 "(The result of this turn goes to the owner as a reply to their message — where they wrote from: write it "
                 "as a reply to them; do not duplicate the same result to the hub.)")


def _relayed_answer(st, msg, word, ts, card=None):
    """The hub relayed the owner's words (word): the turn result goes to the owner where he wrote from (re ->
    modules/telegram); his words answer the card named by the hub (card, `--card qN`); if none is named, this is not an
    answer to a card but a new request (10.10: the only open card was closed by any owner words relayed to the session)."""
    msg["re"] = word["id"]
    # where the result will go is told to the session, as in a task (delegations.task_text): otherwise it sends the
    # same result to the hub (`hub send hub`), and the hub retells the owner what he already got (analysis 13.10). A
    # result by re is delivered only by Telegram; from the page the answer goes through the hub; for a command the
    # tail would become its argument (07.10: /effort)
    if word.get("channel") == "telegram" and not msg.get("command"):
        coretext.append(msg, "\n\n" + RELAYED_TAIL)
    if word.get("action") or not card:
        return   # a page button retold by the hub is not an answer to a card; neither is anything without --card
    sess = st["sessions"][msg["session"]]
    asks = [a for a in owner.open_asks(sess, ts) if owner.before(a, word["created"]) and (not card or a["id"] == card)]
    if not asks:
        return
    ask = asks[-1]
    sess["asks"].remove(ask)
    st["owner"]["reminders"].pop(f"q:{sess['name']}:{ask['request_id']}", None)
    msg["answers_question"] = ask["id"]
    if ask.get("delegation"):
        msg["for_delegation"] = ask["delegation"]
    delegations.trail(st, ask.get("delegation"), delegations.BY_OWNER,
                      TRAIL_ANSWER_VIA_HUB.format(id=ask["id"], text=word["text"][:200]))
    coretext.prepend(msg, coretext.join(YOUR_ANSWER_HEAD.format(id=ask["id"], question=owner.question_head(ask)),
                                        PASSED_BY_HUB, ":\n"))


def _owner_answers_form(st, ev, sess, msg, actions, text=None, after=None):
    """The "form <-> queue" lock (03.10: an hour, 30.09: 10 h): a session waits for the owner's answer to its form
    (AskUserQuestion), while his message to it waits for the end of the turn, and the turn never ends. The owner's
    message to such a session is the answer: the core answers the form with it, like "own option" in Claude Code."""
    if actions is None or sess["state"] != "dialog":
        return
    req = next((r for r in sess["pending"] if r.get("tool") == "AskUserQuestion"), None)
    if not req or after is not None and (req.get("since") or 0) > after:
        return   # the form was opened after his words: they are not an answer to it
    qs = [q.get("question", "") for q in (req.get("input") or {}).get("questions") or []]
    _session_event(st, {"type": "permission.answered", "entity": sess["name"], "ts": ev["ts"], "seq": ev["seq"]},
                   {"request_id": req["request_id"], "behavior": "allow", "answers": {q: text or msg["text"] for q in qs},
                    "channel": "core", "owner": True}, actions)
    _msg_move(msg, "_delivered", ev["ts"])
    msg["delivered_at"], msg["answered_form"] = ev["ts"], req["request_id"]
    _take(st, sess, msg)   # the answer belongs to the same turn, which will decide the badge


def first_word(st):
    """The owner has never written: no mark, nothing read, none of his messages (a hub from before this version is not a
    first). What their page marked read after they came (owner.came: it opens on the hub's chat) is no word of theirs."""
    o = st["owner"]
    return not o.get("greeted") and (bool(o.get("came")) or not o.get("seen")) \
        and not any(m.get("from_owner") for m in st["messages"].values())


def _owner_event(st, ev, data, kind):
    o = st["owner"]
    to, todo, why = decide(OWNER, o["state"], kind, checker(st, data))
    if to is None:
        raise Rejected(why)
    if "away" in todo:
        owner.went_away(o, ev, data)
    if "extend" in todo:
        owner.new_period(o, ev, data)
    o["state"] = to
    if "away" in todo:
        _away_open(st, ev)
    if "back" in todo:
        _away_asks_back(st, ev)
        o["summary"] = {**owner.came_back(st, ev), "seq": ev["seq"]}


DROPPED_GONE = T("core.msg.ask_dropped_gone", "removed question {id}: the owner is back, and the session is gone")
DROPPED_GATE_OFF = T("core.msg.ask_dropped_gate_off", "removed question {id}: the budget gate is off")
GATE_OFF = T("core.msg.budget_gate_off",
             "The budget gate is off: executors at the limit do not wait for the owner's word. The core removed the "
             "economy card {ids} — there will be no answer to it, do not wait.")
STALE_BY_CORE = T("core.reducer.stale_by_core", "stale questions are raised by the core")
STALE_LINE = T("core.msg.ask_stale_line", "- {id} (asked {at}): «{question}»")


AWAY_CARD_BACK = "the owner is back"   # the reason of the withdrawn message: the page says it in words (REASONS in dom.js)


def _away_cards_back(st, ev):
    """The owner is back: a card the hub has not taken yet (still in its queue) is withdrawn — his answer is refused
    from now on (HUB_ANSWER_HERE), and the message would only cost the hub a turn. What the hub already has (typed,
    delivered) stays. The card asked to be sent again if he leaves once more (to_hub is cleared)."""
    gone = set()
    for msg in st["messages"].values():
        if msg.get("away_card") is not None and msg["state"] == "queued":
            _msg_move(msg, "_cancel", ev["ts"])
            msg["reason"] = AWAY_CARD_BACK
            gone.add(msg["away_card"])
    for sess in st["sessions"].values():
        for ask in sess.get("asks") or []:
            if ask.get("to_hub") in gone:
                del ask["to_hub"]


def _away_asks_back(st, ev):
    """The owner is back: cards asked in his absence are in front of him from this minute (deadline, reminders, push,
    Telegram count from his return; since remembers when it was asked). If the session has gone meanwhile (closed,
    crashed, deleted), the card is removed (nobody to answer), and the question is text in the summary, like a deferred one."""
    _away_cards_back(st, ev)
    for sess in st["sessions"].values():
        for ask in [a for a in sess.get("asks") or [] if owner.waits_return(a)]:
            if sess["state"] in ("closed", "failed", "removed"):
                _drop_ask(st, sess, ask, delegations.BY_CORE, DROPPED_GONE.format(id=ask["id"]))
                st["owner"]["deferred"].append({"session": sess["name"], "text": owner.question_text(ask),
                                                "ts": ask["since"]})
            else:
                ask["back_at"] = ev["ts"]


def _budget_gate(st, ev, on):
    """The budget gate was toggled (core/budget.gate): waiting runs get a fresh reason; if switched off, open economy
    cards are removed (nothing left to decide on them), and the hub gets a line."""
    st["owner"]["budget_gate"] = on
    executors.recount(st, ev["ts"])
    if on:
        return
    dropped = []
    for sess in st["sessions"].values():
        for ask in [a for a in sess.get("asks") or [] if a.get("budget_card")]:
            _drop_ask(st, sess, ask, delegations.BY_CORE, DROPPED_GATE_OFF.format(id=ask["id"]))
            dropped.append(ask["id"])
    if dropped:
        _notify_hub(st, ev, GATE_OFF.format(ids=", ".join(dropped)))


def _drop_ask(st, sess, ask, who, note):
    """A card was removed without the owner's answer: from the page, Telegram and reminders; an assignment that waited
    only for it is in progress again."""
    sess["asks"].remove(ask)
    st["owner"]["reminders"].pop(f"q:{sess['name']}:{ask['request_id']}", None)
    delegations.trail(st, ask.get("delegation"), who, note)
    d = st["delegations"].get(ask.get("delegation") or "")
    if d and d["state"] == "waiting-owner" and not sess.get("pending") \
            and not any(a.get("delegation") == d["id"] for a in sess["asks"]):
        # no questions left, and the session awaits no owner permission: back in progress
        d["state"] = decide(DELEGATION, d["state"], "_owner_answered")[0]


ASK_EXPIRED = T("core.msg.ask_expired",
                "Core: your questions to the owner have had no answer for longer than {hours} h — the cards have gone stale, "
                "and nobody came back to the question after them:\n{lines}\nThe owner's decision is still needed to move on — "
                "ask the question again through the AskUserQuestion form: the context in the question itself (they may not "
                "have opened the old card, \"see above\" will not work), one decision per question. The question is already "
                "settled (the owner answered otherwise, it settled by itself, the task is closed) or not needed — ask nothing "
                "and answer with a single \"—\": such an answer goes to no one. The core will not remind you of this question "
                "again.")


def _ask_expired(st, ev, data):
    """The daemon (core/askexpired.py) found cards of a session gone stale with no answer: the session gets one message
    with them and remembers that these questions were raised (revived). A fact that no longer holds - the card answered,
    withdrawn or already raised, the session asleep or gone - is a fact with no consequences, like a stale timer."""
    if data.get("channel") != "core":
        raise Rejected(STALE_BY_CORE)
    sess = st["sessions"].get(ev.get("entity") or "")
    ids = data.get("asks")
    if not sess or sess["state"] not in askexpired.WAKEABLE or not isinstance(ids, list):
        return
    cards = [(a, k) for a, k in askexpired.stale(sess, ev["ts"], st["delegations"]) if a["id"] in ids]
    if not cards:
        return
    tz = st["owner"]["tz"]
    rows = [STALE_LINE.format(id=a["id"], at=owner._local(a["since"], tz).strftime("%d.%m %H:%M"),
                              question=owner.question_text(a)[:askexpired.TEXT]) for a, _ in cards]
    lines = _lines(rows)
    new_message(st, f"m{ev['seq']}", sess["name"],
                ASK_EXPIRED.format(hours=owner.ASK_TTL // 3600, lines=lines),
                ev["ts"], origin=forms.ORIGIN_CORE)
    revived = sess.setdefault("revived", {})
    for _, k in cards:
        revived[k] = ev["ts"]
    # forgotten by age, not by count (evicting a still-stale question by count raised it again every PAUSE; review
    # 29.10): raised more than MAX_AGE ago and with no card of that question young enough to be raised
    young = {askexpired.key(a) for a in sess.get("asks") or [] if ev["ts"] - owner.ask_since(a) < askexpired.MAX_AGE}
    for k, at in list(revived.items()):
        if ev["ts"] - at >= askexpired.MAX_AGE and k not in young:
            del revived[k]


NEW_SESSION_NO_HUB = T("core.reducer.new_session_no_hub", "there is no hub — nobody to ask for a new session: ask the owner")
NEW_SESSION_BAD_NAME = T("core.reducer.new_session_bad_name", "the name is Latin letters, digits, hyphen, up to 40 characters")
NEW_SESSION_ASK = T("core.msg.new_session_ask",
                    "A new session is needed for {who}: {task}\nName: {name}. Folder: {folder}.\nIf you agree: hub session "
                    "new {name} --cwd {cwd}, then hub task new {name} --for-session {who} \"<task>\" — the executor's "
                    "answer will come to {who}.")
NEW_SESSION_ROLE = T("core.msg.new_session_role",
                     "\nThe session asked for a role: add {flags} to that command, or the session is raised without it.")
NO_TASK_GIVEN = T("core.msg.new_session_no_task", "(no task given)")
NO_FOLDER_GIVEN = T("core.msg.new_session_no_folder", "not given")
FOLDER_PLACEHOLDER = T("core.msg.new_session_folder", "<folder>")


def _ask_hub_for_session(st, ev, data):
    """A session asks for a new session: a request to the hub from a template. The folder is the session's word: in a
    command the hub may run, it goes in shell quotes."""
    import shlex
    who, name, hub = data.get("sender"), ev.get("entity"), _hub(st)
    if not hub:
        raise Rejected(NEW_SESSION_NO_HUB)
    if not NAME.fullmatch(str(name or "")):   # the name also goes into the hub's command: unchecked from the spool (bin/hub checks it)
        raise Rejected(NEW_SESSION_BAD_NAME)
    cwd = str(data.get("cwd") or "")
    text = NEW_SESSION_ASK.format(who=who, task=data.get("task") or NO_TASK_GIVEN, name=name, folder=cwd or NO_FOLDER_GIVEN,
                                  cwd=shlex.quote(cwd) if cwd else FOLDER_PLACEHOLDER)
    # the role the session asked for (--agent, --about, --icon): the hub raises the session with the same flags, not as a
    # worker (a word of the session, quoted like the folder)
    flags = [*(["--agent"] if data.get("role") == "agent" else []),
             *(f"{f} {shlex.quote(str(data[k]))}" for f, k in (("--about", "about"), ("--icon", "icon")) if data.get(k))]
    if flags:
        text = coretext.join(text, NEW_SESSION_ROLE.format(flags=" ".join(flags)))
    new_message(st, f"m{ev['seq']}", hub, text, ev["ts"], origin=forms.from_session(who),
                frm=forms.origin_from("session", who))


INVITE = T("core.msg.invite",
           "The hub has started for the first time and the owner has not written yet. Greet them first, now, without "
           "waiting for their word: introduce yourself and the hub following ONBOARDING.md (next to your CLAUDE.md), "
           "steps 1 to 4 only, the address book step waits for their first word. Your answer is that one greeting: short "
           "blocks, no long dashes (never the character —, use a comma, a colon or a full stop), no remarks on what you "
           "are doing. Write in {language}. If they write before you finish, answer them in the same turn.")


FIRST_AFTER_INVITE = (", the first message, you have greeted them already: no second introduction, only the address book "
                      "step of ONBOARDING.md (step 6), quietly, and answer their words")


def _owner_came(st, ev, data):
    """The owner came for the first time (owner 2026-11-19: a fresh hub greeted a person with a Russian browser in the
    machine's English): their page opened and said its language. Kept once, and only while the hub has not met them
    (first_word); the core greets them after it, in this language (core/system.invite_due)."""
    if not tools.from_owner(data):
        raise Rejected(T("core.reducer.came_who", "only the owner's page says they came"))
    if st["owner"].get("came") or st["owner"].get("invited") or not first_word(st):
        return   # met already: a hub in use, or a second page
    st["owner"]["came"] = {"ts": ev["ts"], "lang": i18n.known(data.get("lang")), "via": data.get("channel")}


def invite_open(st):
    """Nobody has greeted the owner and they have not written (first_word)."""
    return not st["owner"].get("invited") and first_word(st)


def _owner_invited(st, ev, data):
    """The first start of the hub (core/system.invite_due): the hub session gets one message to greet the owner by
    ONBOARDING.md, once for the whole journal. Their first word after that is no introduction again (FIRST_AFTER_INVITE)."""
    if data.get("channel") != "core":
        raise Rejected(T("core.reducer.invite_who", "the core invites the owner"))
    hub = _hub(st)
    if not hub or not invite_open(st):
        return   # a fact with no consequences: the invitation was made, or the owner has already written
    st["owner"]["invited"] = ev["ts"]
    new_message(st, f"m{ev['seq']}", hub, INVITE.format(language=i18n.name(data.get("lang"))), ev["ts"],
                origin=forms.ORIGIN_CORE)


def _hub(st):
    return next((n for n, x in st["sessions"].items() if x.get("kind") == "hub" and x["state"] not in ("closed", "failed")),
                None)


RELOAD_ASKED = T("core.msg.reload_asked",
                 "The hub restarted you at your request (hub session reload) with the same conversation. Continue the work "
                 "you asked for the restart for right from where you stopped. Nothing to continue — answer with one line "
                 "«{nothing}».", nothing=NOTHING)
RESTART_ASKED = T("core.msg.restart_asked",
                  "The hub was restarted on a request that you or your executor wrote to the hub about — with the same "
                  "conversation. Continue the work that waited for the restart right from where you stopped. Nothing to "
                  "continue — answer with one line «{nothing}».", nothing=NOTHING)
CUT_TURN = T("core.msg.cut_turn",
             "A hub restart cut off your turn or background work. Look at where you stopped: the last step and the result of "
             "a background task or agent that may have come in the cut turn — and continue the work. Nothing to continue — "
             "answer with one line «{nothing}».", nothing=NOTHING)
CUT_CRASH = T("core.msg.cut_crash",
              "The hub stopped without a clean shutdown (the machine or the hub process crashed) and that cut off your turn or "
              "background work. Look at where you stopped: the last step and the result of a background task or agent that may "
              "have come in the cut turn — and continue the work. Nothing to continue — answer with one line «{nothing}».",
              nothing=NOTHING)


ASK_DETACHED = T("core.msg.ask_detached",
                 "Question {qid} is shown to the owner as a card (the page, Telegram, reminders) — no need to hold the turn "
                 "on it: end the turn or do something else. The answer will come as an ordinary message, the owner's answer "
                 "to question {qid}; after {hours} h without an answer the question goes stale. Once you get the answer, "
                 "first check that the question is still relevant; the owner's decision — only after their answer.")


ASK_AWAY = T("core.msg.ask_away",
             "{away}. Question {qid} is saved as a card — they will see it when they are back; the answer will come as an "
             "ordinary message, the owner's answer to question {qid}. Do not hold the turn on it. Decide what is reversible "
             "yourself and record it: hub decided \"<what> · why · how to roll back\" — and withdraw the question: hub "
             "question withdraw {qid} \"decided\"; put the owner's decision off and go on with other work.")
# to a session, not the hub itself: what it leaves the hub may answer for the owner (spec d2549241)
ASK_AWAY_HUB = T("core.msg.ask_away_hub",
                 " What you leave for them, the hub looks at after your turn and may answer for them unless it is their "
                 "own decision; either answer comes as an ordinary message on {qid}.")
AWAY = T("core.msg.owner_away", "The owner is away")
AWAY_UNTIL = T("core.msg.owner_away_until", "The owner is away until {at}")
OWNER_ANSWER_HEAD = T("core.msg.owner_answer_head", "The user's answer to question {id}")   # forms.HEADS strips it
ANSWER_STALE = T("core.msg.owner_answer_stale", " (the question is stale — check whether it still matters)")
DECLINED = T("core.msg.answer_declined", "declined")
TRAIL_ANSWER = T("core.msg.trail_answer", "answer to {id}: {answers}")
OWNER_ANSWERS = T("core.reducer.owner_answers", "a question to the owner is answered by the owner")


def _lines(rows):
    """The rows one per line, the marks of the core's phrases in each kept (str.join loses them)."""
    out = []
    for i, row in enumerate(rows):
        out += ["\n", row] if i else [row]
    return coretext.join(*out)


ASK_REVIEW = T("core.msg.ask_review",
               " It is non-working time for the owner: the card reaches them in Telegram {at}, in one batch with the "
               "others, and its deadline counts from then. Anything urgent that cannot wait for the morning (prod, money, "
               "clients) — ask the question again, starting it with 🚨, and withdraw this one: hub question withdraw {qid} "
               "\"urgent\".")


def _review_at(st, at):
    t = owner._local(at, st["owner"]["tz"])
    return f"{t.strftime('%d.%m')} {t.strftime('%H:%M')}"


def _away_since(st):
    until = st["owner"].get("until")
    return AWAY_UNTIL.format(at=owner._local(until, st["owner"]["tz"]).strftime("%H:%M")) if until else AWAY


def _ask_answer_text(ask, answers, ts, words=None):
    qs = [q.get("question", "") for q in (ask.get("input") or {}).get("questions") or []]
    lines = [coretext.join("— ", q, ": ", answers.get(q, "—")) for q in qs] if answers else [words or ""]
    stale = not owner.waits_return(ask) and ts - owner.ask_since(ask) >= owner.ASK_TTL
    more = owner.asks_more(answers)
    return coretext.join(OWNER_ANSWER_HEAD.format(id=ask["id"]), ANSWER_STALE if stale else "", ":\n", _lines(lines),
                         "\n\n" + owner.MORE_HINT if more else "")


def _answer_ask(st, ev, data, sess):
    """permission.answered on a hub question handed over as a card: the answer is a message to the hub marked with qN."""
    rid = data.get("request_id")
    ask = next((a for a in sess.get("asks") or [] if a["request_id"] == rid), None)
    if not ask:
        return False
    if not tools.from_owner(data):
        return _hub_answers(st, ev, data, sess, ask)
    sess["asks"].remove(ask)
    _answered(sess, ev, data, ask=ask)
    st["owner"]["reminders"].pop(f"q:{sess['name']}:{rid}", None)
    if liaison.is_liaison(st, sess["name"]):   # the item its question held is the liaison's again
        busstate.liaison_answered(st, rid, ev["ts"])
    answers = data.get("answers") or {}
    if data.get("behavior") != "allow" and not answers:
        answers = {q.get("question", ""): DECLINED for q in (ask["input"].get("questions") or [])}
    delegations.trail(st, ask.get("delegation"), delegations.BY_OWNER,
                      TRAIL_ANSWER.format(id=ask["id"], answers="; ".join(f"{q}: {a}" for q, a in answers.items())))
    new_message(st, f"m{ev['seq']}", sess["name"], _ask_answer_text(ask, answers, ev["ts"]), ev["ts"],
                origin=forms.ORIGIN_OWNER, from_owner=True, answers_question=ask["id"], channel=data.get("channel"),
                on_behalf="owner",   # the core's note in his name, not his words: views.core_row reads the mark
                **({"card_declined": True} if data.get("behavior") != "allow" else {}),
                **({"for_delegation": ask["delegation"]} if ask.get("delegation") else {}))
    return True


# --- the hub answers sessions' cards while the owner is away (spec d2549241) -----------------------------------------
# A card a session left for the owner at the end of its turn goes to the hub (AWAY_CARD), the ones open when he leaves
# in one message (AWAY_OPEN). The hub answers what is not his own decision; the core refuses what is his whatever the hub
# thinks (OWNERS_OWN), and the hub's answer is never his word: no from_owner, answers_question, re or on_behalf.
AWAY_RULE = T("core.msg.away_rule",
              "Theirs: money and paid plans; prod, releases and publishing; clients and people (a message to a person, "
              "how people and agents work); anything irreversible (deleting data, force-push, sending outside); a gate "
              "of a repo or a skill (merge, MR approval, \"code only after ok\", spec acceptance); access, keys and "
              "secrets; a choice they kept for themselves (\"only on my word\"). Their own rules (your CLAUDE.md, the "
              "session's repo rules, hub decided list) say what is theirs: a step those rules let agents take without "
              "their yes (a stage rollout, a routine work message on their task, an agent's own local merge) is not "
              "theirs. Theirs is not yours in either direction — no \"no\", \"not yet\" or \"only look\" for them: "
              "leave the card, it already is their question (no form, no message to them). A card with one such "
              "question among others — leave it whole. Unsure — leave it. Otherwise choose as they would: the "
              "recommended option unless their past decisions say otherwise. Turn result «—».")
AWAY_CARD = T("core.msg.away_card",
              "The owner is away. Card {qid} from {name}{task}: {questions}. Answer it for the owner unless it is their "
              "own decision: hub answer {name} --card {qid} \"<answer>\" --why \"<why · how to roll back>\" (one answer "
              "per question, in order). {rule}")
AWAY_TASK = T("core.msg.away_card_task", " (task {id}: «{words}»)")
AWAY_OPEN = T("core.msg.away_open",
              "The owner has gone away. Cards open now: {lines}. Answer those that are not their own decision: hub "
              "answer <session> --card <qN> \"<answer>\" --why \"<why · how to roll back>\". {rule}")
AWAY_CARD_MAX = 2000   # the questions and options in the hub's message: enough to decide without reading
HUB_ANSWER = T("core.msg.hub_answer",
               "Answer to question {id} — the hub decided for the owner (they are away):\n{lines}\nWhy: {why}\nThis is "
               "not the owner's word: no gate passes with it. They will see it in their return summary and may overturn "
               "it — keep the step reversible; if it turns out to need the owner (money, prod, people) — do not do it, "
               "say so in your result.")
HUB_ANSWER_HERE = T("core.reducer.hub_answer_here", "the owner is here — their cards are theirs")
HUB_ANSWER_WHY = T("core.reducer.hub_answer_why", "the hub's answer for the owner needs --why \"<why · how to roll back>\"")
HUB_ANSWER_LEAVE = T("core.reducer.hub_answer_leave", "the hub answers a card or leaves it to the owner — no refusal")
HUB_ANSWER_GONE = T("core.reducer.hub_answer_gone", "the session that asked is gone: the question goes to their summary")
HUB_ANSWER_THEIRS = T("core.reducer.hub_answer_theirs", "card {id} is the owner's own ({why}): it waits for them")
OWNERS_OWN = {"hub": T("core.reducer.own_hub", "the hub's own question"),
              "liaison": T("core.reducer.own_liaison", "the liaison's: out of the hub only by the owner's card"),
              "bus": T("core.reducer.own_bus", "a card of the hub bus"),
              "budget": T("core.reducer.own_budget", "the budget card: money"),
              "urgent": T("core.reducer.own_urgent", "marked 🚨")}
TRAIL_HUB_ANSWER = T("core.msg.trail_hub_answer", "the hub answered {id} for the owner: {answers}")


def _owners_own(st, sess, ask):
    """Why card ask of sess is the owner's whatever the hub thinks (OWNERS_OWN), or None."""
    if sess.get("kind") == "hub":
        return OWNERS_OWN["hub"]
    if liaison.is_liaison(st, sess["name"]):
        return OWNERS_OWN["liaison"]
    if busstate.is_card(ask):
        return OWNERS_OWN["bus"]
    if ask.get("budget_card"):
        return OWNERS_OWN["budget"]
    if owner.urgent(owner.ask_texts(ask)):
        return OWNERS_OWN["urgent"]
    return None


def _card_text(ask, most=AWAY_CARD_MAX):
    """A card's questions with their options, for the hub to decide by; a longer one is cut with "…"."""
    parts = []
    for q in (ask.get("input") or {}).get("questions") or []:
        opts = "; ".join(f"{o.get('label', '')}" + (f" — {o['description']}" if o.get("description") else "")
                         for o in q.get("options") or [] if isinstance(o, dict))
        parts.append(f"«{q.get('question', '')}»" + (f" Options: {opts}" if opts else ""))
    text = " / ".join(parts) or f"«{owner.question_text(ask)}»"
    return text if len(text) <= most else text[:most] + "…"


def _away_card(st, ev, sess, ask):
    """The card a session left for the owner goes to the hub once (ask["to_hub"])."""
    if ask.get("to_hub"):
        return
    ask["to_hub"] = ev["seq"]
    d = st["delegations"].get(ask.get("delegation") or "") or {}
    task = AWAY_TASK.format(id=d["id"], words=delegations.short_text(" ".join(str(d.get("words") or "").split()), 120)) \
        if d else ""
    _notify_hub(st, ev, AWAY_CARD.format(qid=ask["id"], name=sess["name"], task=task, questions=_card_text(ask),
                                         rule=AWAY_RULE), away_card=ev["seq"])


def _away_open(st, ev):
    """The owner has gone away: the cards open now go to the hub in one message."""
    cards = [(s, a) for s in st["sessions"].values() if s.get("state") not in minihub.GONE
             for a in owner.open_asks(s, ev["ts"]) if _owners_own(st, s, a) is None]
    if not cards:
        return
    for _, a in cards:
        a["to_hub"] = ev["seq"]
    lines = "; ".join(owner.CARD_LINE.format(id=a["id"], name=s["name"], question=_card_text(a, 300)) for s, a in cards)
    _notify_hub(st, ev, AWAY_OPEN.format(lines=lines, rule=AWAY_RULE), away_card=ev["seq"])


def _hub_answers(st, ev, data, sess, ask):
    """permission.answered on a card not from the owner: only the hub, while he is away, on a card that is not his own,
    with a why. The session gets the hub's answer (not his word); the decision goes into his return summary and to
    `hub decided undo`."""
    who = data.get("sender")
    if data.get("channel") != "session" or (st["sessions"].get(who or "") or {}).get("kind") != "hub":
        raise Rejected(OWNER_ANSWERS)
    if st["owner"]["state"] != "away":
        raise Rejected(HUB_ANSWER_HERE)
    if sess["state"] in minihub.GONE:
        raise Rejected(HUB_ANSWER_GONE)
    own = _owners_own(st, sess, ask)
    if own:
        raise Rejected(HUB_ANSWER_THEIRS.format(id=ask["id"], why=own))
    why = " ".join(str(data.get("why") or "").split())
    if not why:
        raise Rejected(HUB_ANSWER_WHY)
    answers = data.get("answers") if isinstance(data.get("answers"), dict) else {}
    if data.get("behavior") != "allow" or not answers:
        raise Rejected(HUB_ANSWER_LEAVE)
    said = "; ".join(str(v) for v in answers.values())
    _drop_ask(st, sess, ask, who, TRAIL_HUB_ANSWER.format(id=ask["id"], answers=said))
    _answered(sess, ev, data, ask=ask)
    qs = [q.get("question", "") for q in (ask.get("input") or {}).get("questions") or []]
    new_message(st, f"m{ev['seq']}", sess["name"], HUB_ANSWER.format(
        id=ask["id"], lines=_lines([coretext.join("— ", q, ": ", str(answers.get(q, "—"))) for q in qs]), why=why),
        ev["ts"], origin=forms.ORIGIN_HUB, hub_answers=ask["id"],
        **({"for_delegation": ask["delegation"]} if ask.get("delegation") else {}))
    text = f"{ask['id']} «{owner.question_head(ask)}» → {said} · {why}"
    decisions = st.setdefault("decisions", [])   # `hub decided undo <seq>` withdraws it: the session is told (told)
    decisions.append({"seq": ev["seq"], "by": who, "text": text, "ts": ev["ts"], "told": sess["name"], "card": ask["id"]})
    del decisions[:-DECISIONS_KEPT]
    st["owner"]["decided"].append({"session": who, "text": text, "ts": ev["ts"], "hub": True, "asker": sess["name"],
                                   "card": ask["id"]})
    return True


LATE_REJECT = T("core.msg.late_reject",
                "The core rejected your command ({what}): {reason}. The CLI did not wait for the answer and wrote \"in the "
                "mailbox\" — this is the outcome, the command was not carried out.")


def _late_reject(st, ev, data):
    """A core refusal that the session's CLI did not wait for ("in the spool, the core has not recorded it yet") is
    sent to it as a message (06.10: `hub schedule rm` under load; the refusal was recorded, and the session decided the
    core had silently ignored it). The two facts may come in any order: the refusal and "CLI did not wait" are joined
    by the request number req."""
    req = data.get("req")
    if not req:
        return
    late = st.setdefault("late", {})
    entry = late.setdefault(req, {"ts": ev["ts"]})
    if ev["type"] == "core.rejected":
        entry.update(reason=data.get("reason"), type=data.get("type"), entity=ev.get("entity"), sender=data.get("sender"),
                     **({"l10n": data["l10n"]} if data.get("l10n") else {}))   # the core's phrases in the reason
    elif data.get("channel") == "session":
        entry.update(gave_up=True, what=data.get("what"), sender=data.get("sender"))
    if entry.get("reason") and entry.get("gave_up") and entry.get("sender") in st["sessions"]:
        what = entry.get("what") or f"{entry.get('type')} {entry.get('entity') or ''}".strip()
        new_message(st, f"m{ev['seq']}", entry["sender"], LATE_REJECT.format(
            what=what, reason=coretext.restore(str(entry["reason"]), entry.get("l10n"))), ev["ts"])
        late.pop(req)
    for k in [k for k, v in late.items() if ev["ts"] - v["ts"] > 3600]:   # the pair never matched: do not hoard
        late.pop(k)


REACT_SEEN, REACT_ANSWERED = "👀", "👍"   # the same badges and meaning as in Telegram (modules/telegram/bot.py)
BADGES_KEEP = 300  # the core remembers the last badges: enough for the feed, the snapshot does not grow
OWN_KEEP = 1000    # the owner's own reactions, kept separately: the fleet's stream of turns does not push them out
EYES_KEEP = 20     # taken messages awaiting a result, per session
REACTS_KEEP = 20   # pending owner reactions to lines, per session
QUOTES_KEEP = 100  # fragments on the owner's badges (a reaction to a selection, 21.10), the last ones: the snapshot does not grow
DEFERRED = "away"  # in turn_asks: this turn's question is deferred until the owner returns (defer_question)


def _badge(st, key, emoji, own=False):
    """A feed badge: mN on an owner message, an event number on a session line. Set by the core (eyes/thumbs,
    badges) or by the owner himself (owner_badges; an empty string means he removed it); in the snapshot they are
    merged, his on top (views). None removes."""
    badges = st.setdefault("owner_badges" if own else "badges", {})
    badges.pop(key, None)
    if emoji is not None:
        badges[key] = emoji
        while len(badges) > (OWN_KEEP if own else BADGES_KEEP):
            del badges[next(iter(badges))]


def _take(st, sess, msg):
    """A turn took a message: the eyes badge on the owner's word, the result of this turn will decide the badge
    (_settle). Each one once (the mark of an already taken message in the next turn is not a new take). The
    continuation of a cut turn (a core message, sess.resume) returns to the turn what the cut turn had taken
    (cut_eyes), by whatever path it was taken (its own turn, an insertion, in also). A badge set or removed by the owner
    himself is left alone by the core; tmux sends no results, so there is no badge. True means the owner's word was taken."""
    cut = []
    if msg["id"] == sess.get("resume"):
        del sess["resume"]
        cut = sess.pop("cut_eyes", None) or []
    word = bool(msg.get("from_owner")) and not msg.get("turn_took")
    if word:
        msg["turn_took"] = True
    if st.get("host") != "tmux":
        own = st.get("owner_badges") or {}
        eyes = sess.setdefault("eyes", [])
        for mid in cut + ([msg["id"]] if word else []):
            if mid not in own and not (st["messages"].get(mid) or {}).get("reacted"):   # the owner's badge is his
                _badge(st, mid, REACT_SEEN)
                eyes.append(mid)
        for old in eyes[:-EYES_KEEP]:   # the result never came
            _badge(st, old, None)
        del eyes[:-EYES_KEEP]
    return word


def _turn_begins(st, sess):
    """A new turn (the session was not mid-turn): eyes badges are for what was taken from this start; whatever the
    previous turn left unresolved (it had no result) gets no badge. What a cut turn had taken (cut_eyes) waits for the
    continuation while it is on its way: the cut is not yet picked up (cut_turn), or the continuation is not taken, not
    removed and has not gone to another session (resume); otherwise it will not come (a limit, but the session was
    raised on the same account), so forget it."""
    sess.pop("turn_asks", None)
    resume = st["messages"].get(sess.get("resume") or "")
    if not sess.get("cut_turn") and (not resume or resume["state"] == "failed" or resume["session"] != sess["name"]):
        sess.pop("cut_eyes", None)
        sess.pop("resume", None)
    for mid in sess.pop("eyes", None) or []:
        _badge(st, mid, None)


def _cut(st, sess):
    """A turn was cut (a limit, the process left mid-turn): no badge; what was taken waits for a continuation turn
    (cut_eyes), and taking the core's continuation message returns the eyes badge to it (_take)."""
    eyes = sess.pop("eyes", None) or []
    for mid in eyes:
        _badge(st, mid, None)
    if eyes:
        sess["cut_eyes"] = (sess.get("cut_eyes") or []) + eyes


def _settle(st, sess, data, ev):
    """The turn result settles the current turn's messages: thumbs-up means it answered; no badge means interrupted,
    failed, or this turn's question to the owner is still open (an answer read by the same turn means the turn
    continued and answered; one deferred while the owner was "away" is open). The journal writes a turn's result before
    the next turn starts (hubd: a tick by the event time, 13.10), so a result is always about the current turn. An
    account limit means the turn was cut, and what was taken waits for a continuation turn (_cut)."""
    if data.get("turns") == 0:   # a built-in command (/compact, /cost): a result with no turn
        return
    if _account_cut(sess, data) or net_cut(data):
        return _cut(st, sess)
    asks = sess.get("turn_asks") or []
    asked = DEFERRED in asks or set(asks) & {a.get("id") for a in sess.get("asks") or []}
    if asked and st["owner"]["state"] == "away":   # what the session left for him in this turn goes to the hub
        for ask in [a for a in sess.get("asks") or [] if a["id"] in asks and _owners_own(st, sess, a) is None]:
            _away_card(st, ev, sess, ask)
    ok = tools.answered(data) and not sess.get("pending") and not asked
    for mid in sess.pop("eyes", None) or []:
        _badge(st, mid, REACT_ANSWERED if ok else None)


def _gone(st, sess):
    """No result will come: the session is closed or crashed for good, or the process was released mid-turn (stop, the
    owner took it into a terminal): clear the waiting eyes badges. Parking and sleep after Stop are different: the
    result will come."""
    for k in ("cut_eyes", "resume", "resume_due"):   # a cut one already has no badge
        sess.pop(k, None)
    for mid in sess.pop("eyes", None) or []:
        _badge(st, mid, None)


REACT_BY_OWNER = T("core.reducer.react_by_owner", "a reaction is the owner's word")
REACT_TARGET = T("core.reducer.react_target", "a reaction is to a message mN or to a line of a session's feed")
REACT_NO_SESSION = T("core.reducer.react_no_session", "no such session")


def _reacted(st, ev, data):
    """An owner emoji reaction (12.10, as in Telegram). On his own message mN it is his badge instead of the core's.
    On a session line (an event number) it is a word without text: the session does not wake but learns of it along
    with the owner's next message (_pump)."""
    if not tools.from_owner(data):
        raise Rejected(REACT_BY_OWNER)
    target, emoji = str(data.get("target") or ""), str(data.get("emoji") or "").strip()[:16]
    msg = st["messages"].get(target)
    known = target in (st.get("badges") or {}) or target in (st.get("owner_badges") or {})
    if msg or (target.startswith("m") and known):   # removed by core.pruned (older than a day) while the badge is still in the feed
        if msg:
            msg["reacted"] = True   # from now on the badge is his, the core leaves it alone
        for s in st["sessions"].values():   # also for one removed by core.pruned: whose it was can no longer be found
            for k in ("eyes", "cut_eyes"):
                if target in (s.get(k) or []):
                    s[k].remove(target)
        _badge(st, target, None)
        return _badge(st, target, emoji, own=True)   # empty means he removed it himself: the "Reaction" on his message sets it again
    if not target.isdigit():
        raise Rejected(REACT_TARGET)
    sess = st["sessions"].get(str(data.get("session") or ""))
    if not sess:
        raise Rejected(REACT_NO_SESSION)
    reacts = sess.setdefault("reacts", {})   # in the session record: a rename carries them along, a new session starts without others'
    reacts.pop(target, None)
    _badge(st, target, emoji or None, own=True)
    # a reaction to a selected piece (part, 21.10): the same badge, the fragment as a hint on it (views reactions.quotes)
    (st.get("owner_quotes") or {}).pop(target, None)
    if emoji and data.get("part") and data.get("quote"):
        quotes = st.setdefault("owner_quotes", {})
        quotes[target] = _quote(data["quote"])
        while len(quotes) > QUOTES_KEEP:
            del quotes[next(iter(quotes))]
    if emoji:
        reacts[target] = {"emoji": emoji, "quote": _quote(data.get("quote") or "")}
        while len(reacts) > REACTS_KEEP:
            del reacts[next(iter(reacts))]


def _reacts_note(sess):
    """Pending owner reactions to a session's lines, for his message that it is about to receive; marks them seen."""
    reacts = sess.pop("reacts", None)
    if not reacts:
        return None
    # a tail of the typed input: only the session's model reads it (_typed)
    return ("\n\n(Before this the owner reacted to your messages: "
            + "; ".join(r["emoji"] + (f" to «{r['quote']}»" if r["quote"] else "") for r in reacts.values()) + ".)")


DECISION_UNKNOWN = T("core.reducer.decision_unknown", "decision #{seq} is not among the last recorded ones (hub decided "
                     "list)")
DECISION_GONE = T("core.reducer.decision_gone", "decision #{seq} is already withdrawn")
DECISION_WHO = T("core.reducer.decision_who", "a decision is withdrawn by the session that recorded it, the hub or the "
                 "owner")
DECISION_WITHDRAWN_CARD = T("core.msg.decision_withdrawn_card",
                            "The hub's answer to question {card} is withdrawn (decision #{seq}): {text}\nThe question is "
                            "the owner's again: do not ask it again while they are away. Their word on it comes — redo "
                            "the step by it; none by the time they are back and your task needs it — ask it once with "
                            "the form.")
DECISION_WITHDRAWN = T("core.msg.decision_withdrawn", "The owner's decision #{seq} (recorded by {by}) was withdrawn: "
                       "{text}\nIf you added it to your CLAUDE.md — take it out. What was already done on it stays — do not ask the "
                       "owner about it.")


def _decision_revoked(st, ev, data, actions):
    """`hub decided undo <seq>`: a decision recorded by mistake (`hub decided list` made the decision "list") is taken
    back by a new fact — the journal only grows. Its author, the hub or the owner withdraws it; it leaves the owner's
    summary and the direction's backlog, and whoever was told about it hears that it is withdrawn. What the decision
    already did otherwise stays."""
    rec = next((d for d in st.get("decisions") or [] if d["seq"] == data.get("seq")), None)
    if not rec:
        raise Rejected(DECISION_UNKNOWN.format(seq=data.get("seq")))
    if rec.get("revoked"):
        raise Rejected(DECISION_GONE.format(seq=rec["seq"]))
    who = data.get("sender")
    if guards.from_owner_or_hub(st, data) and not (who and who == rec.get("by")):
        raise Rejected(DECISION_WHO)
    rec["revoked"] = {"by": who, "ts": ev["ts"]}
    # the summary entry has no seq (a replay of an older journal must give the same summary): matched by author, text, time
    st["owner"]["decided"] = [d for d in st["owner"]["decided"]
                              if (d.get("session"), d.get("text"), d.get("ts")) != (rec["by"], rec["text"], rec["ts"])]
    # a record found in the journal at the update (backfill_decisions) has no slug: the author's direction, if any
    slug = rec.get("slug") or board.direction_of(st, st["sessions"].get(rec.get("by") or ""))
    if slug:
        actions.append({"do": "board_decision_revoke", "session": None, "slug": slug, "seq": rec["seq"]})
    up = st["sessions"].get(rec.get("told") or "")
    if up and up["state"] not in ("removed", "closed", "failed"):
        new_message(st, f"m{ev['seq']}", up["name"], (DECISION_WITHDRAWN_CARD if rec.get("card") else DECISION_WITHDRAWN)
                    .format(seq=rec["seq"], by=rec["by"], text=rec["text"], card=rec.get("card")),
                    ev["ts"], origin=forms.ORIGIN_CORE)


def new_message(st, mid, name, text, ts, origin="", frm=None, **extra):
    """A message to a session: into the queue; for a closed/crashed one, not delivered; a sleeping one is woken. origin
    is the mark for the session as text, frm the same as a field (from: core/forms.py); for a mark with no tails
    (core, hub, owner, schedule) it is derived itself."""
    base, n = mid, 1
    while mid in st["messages"]:   # two core messages in one event (REPLIED to the hub and HANDOFF): do not overwrite (04.10)
        mid, n = f"{base}x{n}", n + 1
    frm = frm or (forms.origin_from(forms.fixed_kind(origin)) if forms.fixed_kind(origin) else None)
    # the core's phrases in the text (core/coretext.py): the session reads the English, the owner the same in his language
    l10n = coretext.marks(text)
    msg = st["messages"][mid] = {
        "id": mid, "to": extra.pop("to", name), "session": name, "text": str(text) if isinstance(text, str) else text,
        "origin": origin, **({"l10n": l10n} if l10n else {}), **({"from": frm} if frm else {}),
        "state": "queued", "attempt": 0, "deadline": None, "created": ts, "since": ts, **extra,
    }
    sess = st["sessions"].get(name)
    if sess is None:
        return msg
    if executors.dormant(sess):
        # an executor that handed in sleeps: only the owner or the hub wakes it, explicitly (_received clears the mark
        # earlier); everything else (schedule, replies, core notes) goes to whoever raised it (analysis 05.10: it was
        # woken bypassing the rule)
        up = sess.get("raised_by")
        up = up if up and (st["sessions"].get(up) or {}).get("state") not in (None, "removed", "closed", "failed") \
            else _hub(st)
        if up and up != name:
            msg["session"] = up
            msg["routing_note"] = _join_note(msg.get("routing_note"), f"For executor {name} — it has handed in its "
                                             f"task and is asleep; to wake it — hub session wake {name}.")
            sess = st["sessions"][up]
    w = sess.get("waiting")   # the recipient's: a message to a sleeping executor did not reach it and went to whoever raised it
    # an "FYI" to a session that waits for the owner goes as an ordinary message (29.10: "the owner logged in, go on"
    # lay aside at a session whose result asked him to log in): the worst case is a plain `hub send`. Not the core's
    # own notes, and not one passed on to whoever raised an executor (routing_note): the news was for someone else
    if extra.get("fyi") and not forms.is_core(msg) and not msg.get("routing_note") and fyi_wakes(sess):
        extra.pop("fyi")
        msg.pop("fyi")
        msg["fyi_woke"] = True
    if w and "stale" not in w and not forms.is_core(msg) and not extra.get("schedule") and not extra.get("fyi"):
        # "what it waits for" is its word about why it is free; after it was written to (owner, session, hub, plugin,
        # a reply to a task) the word is stale (13.10: marks hung for days after the reply). Core service messages and
        # the schedule (the `hub wait` check) do not count. It is not erased: the snapshot returns it as waited
        w["stale"] = ts
    if sess["state"] in ("closed", "failed", "removed"):
        msg["state"], msg["reason"] = "failed", f"session {sess['state']}"
    elif extra.get("fyi"):
        _msg_move(msg, "_aside", ts)   # "for your information": neither wakes nor starts a turn, rides with the next message (_ride)
    elif sess["state"] in SLEEP:
        sess["state"], sess["since"] = step(SESSION, sess["state"], "_wake")[0], ts  # delivered once it comes up
        st.setdefault("_wake", []).append(sess["name"])   # the recipient: a message to a sleeping executor wakes whoever raised it
        sess.pop("stopped", None)   # woken: the "stopped by you" mark is history now (as in _session_event)
        _compact_on_wake(st, sess)
    return msg


# the result of a turn cut by a limit: "You've hit your weekly limit · resets ...", "Claude AI usage limit reached|..."
LIMIT_TEXT = re.compile(r"hit your .{0,40}limit|usage limit reached|limit reached", re.I)
# the result of a turn where the account login was refused: the token is revoked, expired or invalid ("Failed to
# authenticate: OAuth token revoked...", "Invalid API key · Please run /login", "API Error: 401 ... authentication_error").
# Not a tool's text ("HTTP 403 ... requires authentication" from WebFetch is not about the Claude login)
AUTH_TEXT = re.compile(r"failed to authenticate|oauth token (has been )?(revoked|expired)|token (has been )?revoked|"
                       r"invalid api key|authentication_error|api error: 401\b|please run /login", re.I)
# the API refused a request from a network outside the organization's allowed ones (the VPN is off): a 403 "Request not
# allowed" under the same "Failed to authenticate" prefix. The account and its token are fine: not a login refusal, the
# turn waits for the network like a cut one (net_cut). Narrow: another 403 (permission_error, a suspension) is the account's
NO_ROUTE_TEXT = re.compile(r"\b403\b.{0,60}request not allowed", re.I | re.S)
# the API suspended a model for the account's organization until a stated time (a 403 permission_error,
# model_access_suspended; Claude Code prints it under the same "Failed to authenticate" prefix): the account is busy
# until that time, the token is fine (11-06: taken for a revoked token, two accounts busy for an hour)
SUSPENDED_TEXT = re.compile(r"is suspended for this organization until (\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?Z", re.I)
# a login refusal of the token itself holds the account for AUTH_HOLD; refused again once that hold is over (refusals
# within it are the same episode: parallel requests and processes still on the token fail together) means the token is
# revoked, AUTH_REVOKED; a successful answer on the token in between forgets the episode (account.limits)
AUTH_HOLD = 300
AUTH_REVOKED = 3600
SUSPEND_MIN = 60   # a suspension whose stated end has already passed by our clock still holds the account this long
AUTH_FORGET = 86400   # an episode over this long ago is forgotten: an unused account's blip of yesterday is not a repeat
# the accounts gateway takes a move on the core's next tick: a refusal this soon after a seamless move may be a request
# on the previous token (_moved_during_turn)
SWITCH_GRACE = 2


def _auth_refused(sess, data):
    """Is the turn result a pool account login refusal (not a limit)? Without a pool (the computer's login) there is
    nothing to switch to, so no."""
    if "account" in data and (data["account"], data.get("gen")) != (sess.get("account"), sess.get("account_gen")):
        # the process's result is not on the session's current account (a previous process finished the turn after a
        # restart on another one; raised without a pool token, the computer's login): the refusal is not about the current one
        return False
    return _login_refused(sess, data)


def _login_refused(sess, data):
    """A failed turn whose text is a login refusal, not a limit, in a session marked with a pool account."""
    text = data.get("text") or ""
    return bool(data.get("is_error") and sess.get("account") and AUTH_TEXT.search(text) and not LIMIT_TEXT.search(text)
                and not NO_ROUTE_TEXT.search(text))


def _off_pool(sess, data):
    """The login was refused to a process that is on no pool login (the host writes account: null — it was started without
    the account's token: the account had none yet, 10-27 "Not logged in"), though the session is marked with an account.
    A late result of an earlier such process after a restart on a token costs one more restart: the result does not say
    which process it came from."""
    return "account" in data and data["account"] is None and _login_refused(sess, data)


def _moved_during_turn(st, sess, ts):
    """The accounts gateway moved this session to another account during the turn (seamless): what the turn's requests
    were refused with is about the previous token, not the current account."""
    moved = (st.get("account") or {}).get("seamless_at")
    return bool(moved and sess.get("gateway") and (moved >= (sess.get("turn_at") or ts) or ts - moved < SWITCH_GRACE))


def _account_cut(sess, data):
    """The turn was cut by the account: a limit or a pool account login refusal; it continues on another (limit_cut, _cut).
    A Codex turn: its host says the sign-in was lost (auth_lost) on a process marked with a pool account."""
    return bool(data.get("is_error") and LIMIT_TEXT.search(data.get("text") or "")) or _auth_refused(sess, data) \
        or bool(data.get("engine") == "codex" and data.get("auth_lost") and sess.get("account")
                and data.get("account", sess["account"]) == sess["account"])


def _refuse_login(st, sess, ev):
    """The session's account login was refused: the account is marked busy, like a limit refusal (accounts.limit_hit:
    a model suspension until the API's term, type suspended; a refusal of the token, type auth, for AUTH_HOLD, and for
    AUTH_REVOKED when it comes again after that hold without a success in between): the core switches to another from the pool
    (accounts.decide), and if all are busy the hub is told (account.exhausted). Otherwise a revoked token silently failed
    every turn of every session (19.10: 17 turns of 8 sessions in 75 minutes, not one account.*). A process on the
    previous token (the new one is already in the pool, account.token) is not about the current one. The pool of the
    session's engine (a Codex session: the Codex pool)."""
    acc = _pool(st, _engine(st, sess))
    name = sess["account"]
    if (sess.get("account_gen") or 0) < ((acc.get("refreshed") or {}).get(name) or 0):
        return
    ts = ev["ts"]
    until = _suspended_until(ev["data"].get("text") or "")
    if until:   # not the token (it got through): busy until the API's own term, a probe on a small model does not lift it
        (acc.get("refused") or {}).pop(name, None)
        acc.setdefault("seen", {})[name] = {"status": "rejected", "type": "suspended",
                                            "windows": {"suspended": {"resets": max(until, ts + SUSPEND_MIN)}}, "ts": ts}
        return
    if acc is not st.get("account"):   # the Codex pool: its limits do not go through account.limits, nothing forgets
        acc.setdefault("seen", {})[name] = {"status": "rejected", "type": "auth", "windows": {}, "ts": ts}
        return
    # the token itself: revoked only when refused again after the hold, with no success in between; a refusal within
    # the hold is the same episode, of the same kind
    was = (acc.get("refused") or {}).get(name)
    if not isinstance(was, dict) or ts - (was.get("until") or 0) > AUTH_FORGET:
        was = None
    revoked = bool(was) and (bool(was.get("revoked")) or ts >= (was.get("until") or 0))
    until = max(ts + (AUTH_REVOKED if revoked else AUTH_HOLD), (was or {}).get("until") or 0)
    acc.setdefault("refused", {})[name] = {"until": until, "revoked": revoked}
    acc.setdefault("seen", {})[name] = {"status": "rejected", "type": "auth", "revoked": revoked,
                                        "windows": {"auth": {"resets": until}}, "ts": ts}


def _suspended_until(text):
    """The end of a model suspension for the organization in a refusal's text (SUSPENDED_TEXT), epoch seconds, or None."""
    import datetime
    m = SUSPENDED_TEXT.search(text)
    if not m:
        return None
    try:
        return int(datetime.datetime.fromisoformat(m.group(1) + "+00:00").timestamp())
    except ValueError:
        return None


# a UserPromptSubmit hook deliberately refused the prompt (exit 2 / decision:block): a result with no steps (turns:0),
# the text starts with "UserPromptSubmit operation blocked by hook: <hook reason>". A crashed hook (exit 1, 127)
# does not block the prompt, so it is not this
HOOK_BLOCKED = re.compile(r"UserPromptSubmit operation blocked by hook\b:?(.*)", re.S)


def hook_refused(data):
    """Is the turn result a hook's refusal of the prompt (HOOK_BLOCKED, no steps)? A match with the reason, or None
    (reducer, modules/telegram)."""
    return HOOK_BLOCKED.match(data.get("text") or "") if data.get("turns") == 0 else None
# a turn result with is_error is a Claude Code or API failure, not an answer: how to name it to the owner (status line,
# modules/telegram) — the catalog key of the name; failure() gives it in the language of the human it is shown to
# a turn cut by the network: Claude Code retried for minutes and gave up — "API Error: Can't reach the API server … (ENOTFOUND)",
# "API Error: Connection error." The core continues it once the API answers again (net_cut, _net_resume, core/netprobe.py)
NET_TEXT = re.compile(r"^API Error\b.*(can't reach the api|\bE(NOTFOUND|AI_AGAIN|CONNREFUSED|CONNRESET|TIMEDOUT|NETUNREACH)\b"
                      r"|connection error|request timed out|fetch failed|socket hang up"
                      r"|connection (dropped|refused)|no internet route|unable to connect to api(?!:))", re.I | re.S)
NET_WAIT = (30, 60, 120, 300)   # seconds before the continuation after the 1st, 2nd… network cut in a row (then the last one)
NET_TRIES = 12   # network cuts in a row after which the core stops continuing by itself: the probe passes, the turns still fail
NET_TIMER = "n:"   # the timer of a network cut: n:<session>; hubd fires it only when the API answers (core/netprobe.py)
# a working or parked session gets the continuation; one handed in, asleep, closed or held in a terminal does not
NET_CONTINUABLE = ("idle", "busy", "dialog", "starting", "parked")
NET_CUT = T("core.msg.net_cut",
            "The network to the Claude API dropped in the middle of your turn; the API answers again. Look at where you "
            "stopped and continue the work from there. Nothing to continue — answer with one line «{nothing}».",
            nothing=NOTHING)
FAILURES = ((LIMIT_TEXT, "core.failure.limit"),
            (NET_TEXT, "core.failure.network"),
            (NO_ROUTE_TEXT, "core.failure.no_access"),
            (re.compile(r"\b529\b|overloaded", re.I), "core.failure.overloaded"),
            (re.compile(r"\b429\b|rate.?limit|too many requests", re.I), "core.failure.rate_limit"),
            (re.compile(r"prompt is too long|context (window|length)", re.I), "core.failure.context"),
            (re.compile(r"credit balance|billing", re.I), "core.failure.credits"),
            # a pool token was revoked (a new login, a password change): the /login from Claude Code's hint will not help it
            (re.compile(r"token (has been )?revoked", re.I),
             "core.failure.token_revoked"),
            (re.compile(r"\b40[13]\b|authenticat|invalid api key|oauth|/login|unauthori[sz]ed", re.I),
             "core.failure.auth"),
            (re.compile(r"\bAPI Error\b|\b5\d\d\b", re.I), "core.failure.api"))
# a host line that is really failure text (its result will come with is_error): not the turn's "first line" for the owner
FAILURE_LINE = re.compile(r"^(You've hit your|Claude AI usage limit|API Error\b|Prompt is too long|Credit balance is too "
                          r"low|Invalid API key|Please run /login|OAuth token)", re.I)
RESETS = re.compile(r"resets?\s+([^·\n]{1,60})", re.I)


def failure(data):
    """Is the turn result a failure (is_error with text)? (how to name it — in the language of this process's human,
    i18n.t; limit reset time or None), or None if it is an answer."""
    text = (data.get("text") or "").strip()
    if not data.get("is_error") or not text:
        return None
    from core import i18n
    key = next((key for pat, key in FAILURES if pat.search(text)), "core.failure.other")
    when = RESETS.search(text) if key == FAILURES[0][1] else None
    label = i18n.t(key)
    if data.get("engine") == "codex":   # a Codex turn — a Codex failure, not Claude's
        label = label.replace("Claude", "Codex")
    return label, when.group(1).strip().rstrip(".") if when else None


# Codex (experimental): a hub on Codex has one account; a turn cut by a limit
# is continued by the core itself after the window resets (codex.limit_reset from the daemon), exactly once per cut
LIMIT_CUT_CODEX = T("core.msg.limit_cut_codex",
                    "The Codex limit cut off your turn; the limit window has reset. Look at where you stopped and continue "
                    "the work from there. Nothing to continue — answer with one line «{nothing}».", nothing=NOTHING)


def _codex_limit(st, ev, data, account=None):
    """A cut by the Codex limit: when to continue — the reset from the result (the window is used up), otherwise the latest
    reset of a used-up window by the last limits of the cut's account (the active one's row, or its own seen numbers
    with the account pool); unknown — no automatic continuation."""
    if data.get("engine") != "codex":
        return {}
    mine = account == _pool_of(st, "codex").get("active")   # the active one's row (without a pool: both None)
    resets = data.get("limit_resets")
    if not _num(resets) or resets <= ev["ts"]:   # the reset has passed — the snapshot is stale: no continue every tick
        lim = _codex_row_of(st) if mine else (_pool_of(st, "codex").get("seen") or {}).get(account) or {}
        ahead = {n: w for n, w in (lim.get("windows") or {}).items() if isinstance(w, dict)
                 and (w.get("resets") or 0) > ev["ts"]}
        full = [w["resets"] for w in ahead.values() if (w.get("used") or 0) >= 1]
        # no used-up window by the numbers, but Codex refused (rejected) — the nearest reset: if it fails, a new cut, a new time
        resets = max(full) if full else min((w["resets"] for w in ahead.values()), default=None) \
            if lim.get("status") == "rejected" else None
    elif mine and resets > _codex_row_of(st).get("held", 0):
        # the turn names a later reset than the reading may show (the week): the economy holds until it too
        # (core/budget: held) — until it passes, a read says Codex lets in, or a turn begun after it goes through
        was = _codex_row_of(st) or {"engine": "codex", "status": "rejected", "windows": {}, "ts": ev["ts"]}
        _codex_row(st, {**was, "held": resets, "held_at": ev["ts"]})
    return {"engine": "codex", **({"resets": resets} if resets else {})}


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


# Codex (experimental): the pool's decisions are the Claude pool's
# events on the Codex pool's record (_account), its rows and per-account sign-ins are its own facts. Accounts of folders
# other than the hub's have their own types: a core without the pool refuses them instead of taking them for the hub's
CODEX_POOL_KINDS = ("codex.switched", "codex.exhausted", "codex.waiting")
CODEX_ACCOUNT_KINDS = ("codex.pool", "codex.account_login", "codex.account_logout", "codex.account_lost")
ACCOUNT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@+-]{0,79}")   # a pool account's name (core/profiles.NAME)
LIMIT_CUT_CODEX_POOL = T("core.msg.limit_cut_codex_pool",
                         "The Codex limit cut off your turn; the hub moved Codex to another account and restarted you with "
                         "the same conversation. Look at where you stopped and continue the work from there. Nothing to "
                         "continue — answer with one line «{nothing}».", nothing=NOTHING)


def _engine(st, sess):
    """Which account pool a session's process is on: codex — on a hub with the Codex host, its model is codex/… or the
    hub is entirely on Codex (the bus liaison stays on claude, hosts/codex.MuxHost); otherwise claude."""
    cx = (st or {}).get("codex") or {}
    if not cx.get("host") or (sess or {}).get("liaison"):
        return "claude"
    return "codex" if cx.get("whole") or str((sess or {}).get("model") or "").startswith("codex/") else "claude"


def claude_account(st, sess):
    """The pool account a Claude session's launch takes now: (name, token generation), as spawn_action decides it; None
    — the session is on Codex (its own pool and folder are decided at the launch). A spawn held for a while decides it
    anew with this (daemon.fresh_account)."""
    if _engine(st, sess) != "claude":
        return None
    acc = _pool_of(st, "claude")
    name = acc.get("active")
    return name, ((acc.get("refreshed") or {}).get(name) if name else None)


def _pool(st, engine):
    """The account pool's record (written): Claude's st["account"], Codex's st["codex"]["account"]."""
    return st.setdefault("codex", {}).setdefault("account", {}) if engine == "codex" else st.setdefault("account", {})


def _pool_of(st, engine):
    """The same, read only ({} — none)."""
    return (((st or {}).get("codex") or {}).get("account") if engine == "codex" else (st or {}).get("account")) or {}


def _codex_pool_on(st):
    return bool(((st.get("codex") or {}).get("pool") or {}).get("on"))


def _before_sign_in(pool, name, data):
    """A module's measurement (started) begun before the account's current sign-in: about the previous one."""
    since = ((pool.get("logins") or {}).get(name) or {}).get("since")
    return _num(data.get("started")) and _num(since) and data["started"] < since


def _codex_signed_in(st, name, data, now):
    """An account's new sign-in is a new generation: its earlier numbers and loss are about the previous one (another
    ChatGPT account may have signed into the same folder)."""
    pool = _pool(st, "codex")
    pool.setdefault("logins", {})[name] = {"email": data.get("email"), "plan": data.get("plan"), "since": now}
    # a new generation, as a Claude account's new token (_account_token): its processes are raised again at their next
    # message, a turn cut on the lost sign-in at once — and continues (session.started: another generation)
    pool.setdefault("refreshed", {})[name] = now
    for key in ("seen", "lost"):
        (pool.get(key) or {}).pop(name, None)
    if (st.get("codex") or {}).get("lost") and _codex_pool_on(st):
        st["codex"]["lost"] = None   # every row had lost its sign-in; now one lets in


def _codex_lost(st, ev, name, data):
    """An account's sign-in is lost (a 401, a refresh token used already): it is busy as a sign-in refusal (the pool's
    decide moves away, as _refuse_login for Claude), the daemon tells the owner once; the hub as a whole does not let in
    only when every row has lost its sign-in."""
    pool = _pool(st, "codex")
    if _before_sign_in(pool, name, data):
        return
    was = (pool.get("lost") or {}).get(name) or {}
    pool.setdefault("lost", {})[name] = {"ts": ev["ts"], "since": was.get("since") or ev["ts"], "why": data.get("why"),
                                         **({"told": True} if was.get("told") else {})}
    pool.setdefault("seen", {})[name] = {"status": "rejected", "type": "auth", "windows": {}, "ts": ev["ts"]}
    rows = [p["name"] for p in ((st.get("codex") or {}).get("pool") or {}).get("profiles") or []]
    if rows and all(n in pool["lost"] for n in rows):
        cx = st["codex"]
        hub = cx.get("lost") or {}
        cx["lost"] = {"ts": ev["ts"], "since": hub.get("since") or ev["ts"], "why": data.get("why"),
                      **({"told": True} if hub.get("told") else {})}


def _codex_let_in_account(st, name):
    """A Codex turn of a process on this account went through: its sign-in works."""
    pool = _pool_of(st, "codex")
    if name and name in (pool.get("lost") or {}):
        pool["lost"].pop(name)
        if ((pool.get("seen") or {}).get(name) or {}).get("type") == "auth":
            pool["seen"].pop(name)


def codex_active_home(st):
    """The folder of the Codex pool's active account (codex.pool), None — the hub folder (no pool, its row, not taken)."""
    cx = st.get("codex") or {}
    active = _pool_of(st, "codex").get("active")
    if not cx.get("host") or not _codex_pool_on(st) or not active:
        return None   # back on claude, the pool off: the hub folder
    row = next((p for p in (cx.get("pool") or {}).get("profiles") or [] if p["name"] == active), None)
    return None if not row or row.get("hub") else row["home"]


def codex_notice(name):
    """The id of the owner's notice "this Codex account's sign-in is lost" (core/daemon.codex_tick)."""
    return f"{CODEX_NOTICE}:{name}"


def codex_account_lost_text(st, name):
    lost = (_pool_of(st, "codex").get("lost") or {}).get(name) or {}
    who = ((_pool_of(st, "codex").get("logins") or {}).get(name) or {}).get("email")
    from core import i18n   # the owner's notice (core/daemon.codex_pool_tick): in his language
    return i18n.t("core.reducer.codex_account_lost", name=name, who=f" ({who})" if who else "",
                  why=f" ({delegations.short_text(str(lost.get('why') or ''), 160)})" if lost.get("why") else "")


def _codex_whole(st):
    """The hub is entirely on Codex (the daemon's mark codex.host whole): its limits row (st["limits"]) is Codex's."""
    return bool((st.get("codex") or {}).get("whole"))


def _codex_row_of(st):
    """The Codex limits row (_codex_limits), {} — none yet."""
    return (st.get("codex") or {}).get("limits") or {}


def _codex_row(st, row):
    """The Codex limits row is row; on a hub entirely on Codex it is the hub's row too (st["limits"], a copy)."""
    st.setdefault("codex", {})["limits"] = row
    if _codex_whole(st):
        st["limits"] = dict(row)


def _codex_limits(st, ev, data):
    """account.limits of Codex: with the account pool (an account in the fact) its numbers go to that account
    (st["codex"]["account"]["seen"]), and the row is the active one's; a measurement of a module begun before the
    account's current sign-in is about the previous one and is not taken."""
    name, pool = data.get("account"), _pool_of(st, "codex")
    if name and _before_sign_in(pool, name, data):
        return
    if name or pool:
        _pool(st, "codex").setdefault("seen", {})[name or ""] = {
            **{k: data.get(k) for k in ("status", "type", "windows")}, "ts": ev["ts"]}
    if name != pool.get("active"):
        return   # another account's numbers: not the row
    _codex_row_limits(st, ev, {k: v for k, v in data.items() if k != "started"})


def _codex_row_limits(st, ev, data):
    """account.limits of Codex (hosts/codex_limits): a row of its own, st["codex"]["limits"] — not the default Claude
    login's (account.seen[""]), not the hub's row on a hub with claude sessions (both write with no account name, the
    engine field tells them apart; before this, the limits line and the economy took whichever came last). A reset a cut
    turn named (held) stays while it is ahead, unless a read says ordinary usage is allowed (usage_allowed): a sparse
    update says "allowed" too once the 5 h window renews (hosts/codex_limits.limits_fact), while the week may still
    refuse."""
    was = _codex_row_of(st)
    keep = _num(was.get("held")) and was["held"] > ev["ts"] and data.get("usage_allowed") is not True
    _codex_row(st, {**data, "ts": ev["ts"], **({k: was[k] for k in HELD if k in was} if keep else {})})


HELD = ("held", "held_at")   # a reset a cut Codex turn named and when (_codex_limit)


def _codex_let_in(st, ev, data):
    """A Codex turn went through: Codex lets in — a reset a cut turn named no longer holds the economy. Only a turn
    begun after that cut (by its duration): one let in before it may finish after it."""
    row = _codex_row_of(st)
    begun = ev["ts"] - (data.get("duration_ms") or 0) / 1000 if _num(data.get("duration_ms")) else ev["ts"]
    if "held" in row and begun > (row.get("held_at") or 0):
        _codex_row(st, {k: v for k, v in row.items() if k not in HELD})


def _codex_cut_due(st, sess, now):
    cut = sess.get("limit_cut") or {}
    # one parked by the core too: the core's message raises it; an executor asleep after the cut is woken (_wake_cut, as a
    # Claude cut by core/limitresume); a stopped one — not. With the account pool only a cut on the active account
    # (without a pool both are None): a cut on another one continues by the switch (_pump, session.started) — not twice
    active = _pool_of(st, "codex").get("active")
    return cut.get("engine") == "codex" and bool(cut.get("resets")) and cut["resets"] <= now \
        and sess.get("state") in (*LIVE, "parked", "retired") and not limitresume.stopped(sess) \
        and (active is None or cut.get("account") == active)


CODEX_NOTICE = "codex-auth"   # the owner's notice "Codex does not let in" (core/daemon.codex_tick)


def codex_lost_text(st):
    """The owner's line while the hub's Codex does not let in (codex.lost), or None."""
    cx = st.get("codex") or {}
    lost = cx.get("lost")
    if not lost:
        return None
    from core import i18n   # the owner's notice (core/daemon.codex_tick): in his language
    return i18n.t("core.reducer.codex_no_login" if not cx.get("login") else "core.reducer.codex_login_lost",
                  why=delegations.short_text(str(lost.get("why") or ""), 160))


def codex_due(st, now):
    """For the daemon: is it time to write codex.limit_reset — there is a turn cut by the Codex limit whose reset has
    passed (the same rule as the continuation in the fold — otherwise the fact would be written every tick)."""
    return any(_codex_cut_due(st, s, now) for s in st["sessions"].values())


CODEX_HOST_BY_CORE = T("core.reducer.codex_host_by_core", "the Codex host is marked by the core")
CODEX_POOL_BY_CORE = T("core.reducer.codex_pool_by_core", "the Codex pool's rows are written by the core")
CODEX_POOL_ROWS = T("core.reducer.codex_pool_rows", "codex.pool — rows {shape}", shape="{name, home, hub?}")
CODEX_ACCOUNT_LOGIN_OWNER = T("core.reducer.codex_account_login_owner", "a Codex account sign-in is the owner's word")
CODEX_NEEDS_ACCOUNT = T("core.reducer.codex_needs_account", "{kind} — with an account name")
CODEX_ACCOUNT_LOST_BY = T("core.reducer.codex_account_lost_by",
                          "\"a Codex account's sign-in is lost\" is written by the host and the pool's module")
CODEX_LOGIN_OWNER = T("core.reducer.codex_login_owner", "the hub's Codex sign-in is the owner's word")
CODEX_BACK = T("core.msg.codex_back",
               "The hub's Codex lets in again (the owner had a \"does not let in\" notice): signed in as {email}. What "
               "failed without a sign-in can be repeated.")
CODEX_SPENT_BY_CORE = T("core.reducer.codex_spent_by_core", "the core's model usage is written by the core")
CODEX_AUTH_LOST_BY = T("core.reducer.codex_auth_lost_by", "\"the Codex sign-in is lost\" is written by the host")
CODEX_RESET_BY_CORE = T("core.reducer.codex_reset_by_core", "a Codex limit reset is marked by the core")


def _codex(st, ev, data, ctx):
    cx = st.setdefault("codex", {})
    kind, now = ev["type"], ev["ts"]
    if kind == "codex.host":   # the daemon: the hub has the Codex host (codex/… models are known) or no longer
        if data.get("channel") != "core":
            raise Rejected(CODEX_HOST_BY_CORE)
        cx["host"] = bool(data.get("on"))
        if not cx["host"]:   # back to claude: a Codex sign-in loss no longer concerns the hub (core/daemon.codex_tick)
            cx["lost"] = None
        whole = cx["host"] and data.get("whole") is True   # entirely on Codex ("host": "codex"), not codex/… next to claude
        if whole != _codex_whole(st):   # the hub's limits row follows its engine at once: Codex's, or Claude's last known
            if whole:
                cx["whole"] = True
                st["limits"] = dict(cx.get("limits") or {})
            else:
                cx.pop("whole", None)
                acc = st.get("account") or {}
                seen = (acc.get("seen") or {}).get(acc.get("active") or "")
                st["limits"] = {**seen, "account": acc.get("active")} if seen else {}   # as on a switch (_account)
        return
    if kind == "codex.pool":   # the daemon (core/codexslot.py): the pool's rows whose folders the core took
        rows = data.get("profiles")
        if data.get("channel") != "core":
            raise Rejected(CODEX_POOL_BY_CORE)
        if not (isinstance(rows, list) and all(isinstance(p, dict) and isinstance(p.get("name"), str)
                                               and isinstance(p.get("home"), str) for p in rows)):
            raise Rejected(CODEX_POOL_ROWS)
        cx["pool"] = {"on": bool(data.get("on")), "switch_at": data.get("switch_at"), "by": data.get("by"),
                      "profiles": [{k: p[k] for k in ("name", "home", "hub") if k in p} for p in rows]}
        return
    if kind in ("codex.account_login", "codex.account_logout"):   # an account folder other than the hub's
        if guards.from_owner(st, data):
            raise Rejected(CODEX_ACCOUNT_LOGIN_OWNER)
        name = data.get("account")
        if not (isinstance(name, str) and ACCOUNT_NAME.fullmatch(name)):
            raise Rejected(CODEX_NEEDS_ACCOUNT.format(kind=kind))
        if kind == "codex.account_login":
            _codex_signed_in(st, name, data, now)
        else:
            pool = _pool(st, "codex")
            for key in ("logins", "lost"):
                (pool.get(key) or {}).pop(name, None)
        return
    if kind == "codex.account_lost":
        # the host (a process of the account) or the pool's module (a measurement; core/daemon admits only its rows)
        if not (data.get("channel") in (None, "core")
                or data.get("channel") == "plugin" and str(data.get("sender") or "").startswith("mod:")):
            raise Rejected(CODEX_ACCOUNT_LOST_BY)
        name = data.get("account")
        if not (isinstance(name, str) and ACCOUNT_NAME.fullmatch(name)):
            raise Rejected(CODEX_NEEDS_ACCOUNT.format(kind="codex.account_lost"))
        return _codex_lost(st, ev, name, data)
    if kind in ("codex.login", "codex.logout"):
        if guards.from_owner(st, data):
            raise Rejected(CODEX_LOGIN_OWNER)
        name = data["account"] if isinstance(data.get("account"), str) and ACCOUNT_NAME.fullmatch(data["account"]) \
            else None
        if name and kind == "codex.login":   # the hub folder's row of the account pool: its new generation too
            _codex_signed_in(st, name, data, now)
        elif name:
            for key in ("logins", "lost"):
                (_pool(st, "codex").get(key) or {}).pop(name, None)
        if kind == "codex.login":   # the owner's notice "does not let in" is cleared by the daemon (codex_tick)
            was = cx.get("lost") or {}
            cx.update(login={k: data.get(k) for k in ("email", "plan")}, since=now, lost=None)
            if was.get("told"):   # a real outage (the owner was told): the hub, signed in again, may repeat what failed
                ctx["notify"](ev, CODEX_BACK.format(email=data.get("email") or "?"))
        else:
            cx.update(login=None, since=now, lost=None)
        return
    if kind == "codex.spent":   # tokens of the core's model (core/codexmodel.py) — a fact for "Usage", the state does not change
        if data.get("channel") not in (None, "core"):
            raise Rejected(CODEX_SPENT_BY_CORE)
        return
    if kind == "codex.auth_lost":
        if data.get("channel") not in (None, "core"):
            raise Rejected(CODEX_AUTH_LOST_BY)
        # not a message to the hub session: it has no sign-in either (a turn with a 401, stand 2026-10-29) — the daemon
        # tells the owner (codex_tick: owner.notify, the page and Telegram) once per loss: since — its beginning, told —
        # the notice went (owner.notify above). With the account pool the hub folder is one of its rows (account)
        if isinstance(data.get("account"), str) and ACCOUNT_NAME.fullmatch(data["account"]) and _codex_pool_on(st):
            return _codex_lost(st, ev, data["account"], data)
        was = cx.get("lost") or {}
        cx["lost"] = {"ts": now, "since": was.get("since") or now, "why": data.get("why"),
                      **({"told": True} if was.get("told") else {})}
        return
    # codex.limit_reset (the daemon): the window has reset — continue the turns it cut, each once
    if data.get("channel") != "core":
        raise Rejected(CODEX_RESET_BY_CORE)
    for sess in [s for s in st["sessions"].values() if _codex_cut_due(st, s, now)]:
        if executors.dormant(sess):
            _wake_cut(st, ev, sess, LIMIT_CUT_CODEX)
        else:
            _continue_cut(st, ev, sess, LIMIT_CUT_CODEX)


def _continues(st, marker, task):
    """What continues a turn cut by a limit or by the process leaving (a restart): an assignment (for_delegation) or
    an owner message (re)."""
    if task:
        return {"for_delegation": task}
    msg = st["messages"].get(marker or "") or {}
    re_ = msg["id"] if msg.get("from_owner") else msg.get("re")
    return {"re": re_} if re_ else {}


def _run_chain(st, sess):
    """What the outcome of a `hub run` continues: the chain of the turn that started it (core/chain.py) and its matter —
    an assignment or the owner's message, as a turn cut by a restart is continued (_continues)."""
    return {**({"root": sess["turn_root"]} if sess.get("turn_root") else {}),
            **_continues(st, _head(sess), sess.get("delegation"))}


def _cut_resets(st, ts):
    """When the window that refused the turn resets (the host writes the refusal's account.limits just before the result), or
    None: no refusal in the limits, or a reset that is not ahead (a stale measurement) - a cut with no known reset is not
    continued by the clock (core/limitresume.py)."""
    limits = st.get("limits") or {}
    at = ((limits.get("windows") or {}).get(limits.get("type")) or {}).get("resets")
    return at if limits.get("status") == "rejected" and isinstance(at, (int, float)) and not isinstance(at, bool) \
        and at > ts else None


# the Claude pool's texts that have a Codex pool's own (i18n core.codex_pool.*)
CODEX_TEXTS = ("core.account.exhausted", "core.account.free_again", "core.account.switched", "core.account.default")
LIMIT_CUT = T("core.msg.limit_cut",
              "The Claude account limit cut off your turn; the hub switched Claude to another account and restarted you with "
              "the same conversation. Look at where you stopped and continue the work from there. Nothing to continue — "
              "answer with one line «{nothing}».", nothing=NOTHING)


ACCOUNT_SWITCH_BY = T("core.reducer.account_switch_by", "the {engine} account is switched by the core, the owner or the hub")
ACCOUNT_WAIT_ACTIVE = T("core.reducer.account_wait_active",
                        "only the active account can wait for a reset, with the reset time")
ACCOUNT_TOKEN_BY_CORE = T("core.reducer.account_token_by_core",
                          "the core notices an account token change by the pool file (hub accounts token)")
ACCOUNT_TOKEN_FIELDS = T("core.reducer.account_token_fields", "account.token — with the account name and the token fingerprint")
GATEWAY_BY_CORE = T("core.reducer.gateway_by_core", "the accounts gateway is switched on and off by the core (by the setting)")
GATEWAY_NEEDS_IDENT = T("core.reducer.gateway_needs_ident", "a switched-on gateway needs a place (ident)")
LIMIT_RESET_BY_CORE = T("core.reducer.limit_reset_by_core", "the continuation after a limit reset is written by the core")


def _account(st, ev, data, ctx):
    """Claude accounts (modules/accounts): a switch to another Claude account, or "all busy". The core decides
    (accounts.decide), by hand the owner or the hub (hub accounts use). The fleet does not restart live sessions all
    at once: a process takes the new login at its next launch (_pump: before a message, after parking; with background
    work, after it), one cut by a limit at once; if we return to the login a process was raised with, it is already on
    it (_account_stale)."""
    engine = "codex" if ev["type"].startswith("codex.") else "claude"   # the Codex pool: the same rules (CODEX_POOL_KINDS)
    kind = "account." + ev["type"].split(".", 1)[1]
    if data.get("channel") != "core" and guards.from_owner_or_hub(st, data):
        raise Rejected(ACCOUNT_SWITCH_BY.format(engine="Codex" if engine == "codex" else "Claude"))
    from core import i18n
    lang = i18n.state_lang(st)   # the language of the state, not of the hubd process (it reads its own once, at start)
    key = (lambda k: k.replace("core.account.", "core.codex_pool.") if engine == "codex" and k in CODEX_TEXTS else k)
    # no language known yet (a fresh hub: the first line comes before the owner's first word): the English with the marks
    # of its phrases, the feed shows it in the language the owner turns out to speak (core/coretext.py)
    say = (lambda k, **p: i18n.tr(lang, key(k), **p)) if lang else (lambda k, **p: coretext.catalog(key(k), **p))  # noqa: E731
    # the reason made by a process of another language is read in the note's own
    why_of = lambda: coretext.human(coretext.got(data, "why"), lang or i18n.SOURCE)  # noqa: E731
    quiet = engine == "claude" and _codex_whole(st)   # no session of this hub runs on the Claude pool: nothing to tell
    notify = (lambda *a, **kw: None) if quiet else ctx["notify"]
    acc = _pool(st, engine)
    now = ev["ts"]
    blocked = {n: t for n, t in {**(acc.get("blocked") or {}), **(data.get("blocked") or {})}.items() if t > now}
    acc["blocked"] = blocked
    for n in data.get("blocked") or {}:
        (acc.get("fresh_fp") or {}).pop(n, None)   # blocked again: a fresh token of it may be reported anew (accounts: probe)
    tz = st["owner"].get("tz") or "UTC"
    at = lambda t: owner._local(t, tz).strftime("%d.%m %H:%M")  # noqa: E731
    if kind == "account.waiting":
        # the 5 h window reset is near (accounts.decide: wait_reset): do not switch, wait for it on the active one:
        # sessions cut by the limit stand and continue in the same process at the reset (free + reset below); the
        # conversation cache on this account is intact. We do not tell the hub: a limit failure is not news, and in
        # minutes everything continues by itself
        if data.get("account") != acc.get("active") or not isinstance(data.get("until"), (int, float)):
            raise Rejected(ACCOUNT_WAIT_ACTIVE)
        acc["waiting"] = {"account": data["account"], "until": data["until"], "why": data.get("why")}
        acc["exhausted"] = None   # one of two: waiting for the active one's reset, or all busy
        acc.pop("released", None)
        return
    if kind == "account.exhausted":
        # all busy, said until then; or the episode a fresh token nobody read was released from (unread below) and the
        # queue has read it at the limit since: the same episode
        said = (acc.get("exhausted") or acc.get("released") or {}).get("until") or 0
        until = data.get("until") or 0
        acc.pop("waiting", None)
        acc.pop("released", None)
        if said > now and until >= said:
            # the same episode, a later reset (a gateway's next token): the earliest one holds, said already
            acc["exhausted"] = {"until": said}
            return
        acc["exhausted"] = {"until": until}
        notify(ev, say(_exhausted_key(acc, until) if engine == "claude" else "core.account.exhausted",
                       why=why_of() or say("core.account.limit"), at=at(until or now)))
        return
    was, to = acc.get("active"), data.get("to")
    pinned = bool(data.get("manual") or (data.get("refresh") or data.get("free")) and acc.get("pinned"))   # the same one: it holds
    # force (hub accounts use --force): chosen forcibly, so the core does not move away from it by threshold, only on an API refusal
    forced = bool(data.get("force") or (data.get("refresh") or data.get("free")) and was == to and acc.get("forced"))
    # all are busy and the active one takes a fresh token on its limit: the episode holds until that token's limits
    # (accounts.decide: free, or exhausted with an earlier reset). Before, every fresh token of a gateway ended it: the
    # budget read "normal" ("its reserve is not known") and seconds later "none free" again (2026-11-13)
    said = (acc.get("exhausted") or {}).get("until", 0)
    episode = data.get("refresh") and was == to and said > now
    acc.update(active=to, since=now, exhausted=acc["exhausted"] if episode else None, pinned=pinned, forced=forced)
    if data.get("unread") and was == to and said > now:
        # the hold of a fresh token nobody read has ended: the queue comes up and reads it. A reading at the limit is
        # still this episode (accounts.decide: busy again until the account's own earliest reset, block, not said
        # again); the next fresh token no sooner than REFRESH_EXHAUSTED from here, and it is this episode too; any
        # other switch ends it
        acc["released"] = {"until": said, **({"block": blocked[to]} if blocked.get(to) else {})}
        acc.setdefault("refresh_at", dict(acc.get("refreshed") or {}))[to] = now
    elif not (data.get("refresh") and was == to):
        acc.pop("released", None)
    if data.get("force") and to:
        blocked.pop(to, None)   # the earlier "busy" mark was by a threshold the owner cancelled
    acc.pop("waiting", None)
    # refreshed is the account token generation (processes and their limits are marked with it), refresh_at is when a
    # fresh one was taken on a limit (accounts.decide: no more often than REFRESH_EVERY, while all are busy than
    # REFRESH_EXHAUSTED; the hold of a fresh token nobody has read ends REFRESH_EXHAUSTED after it, and that release
    # counts as one: the next fresh token REFRESH_EXHAUSTED later); the earlier state knew only refreshed, which served
    # as both
    acc.setdefault("refresh_at", dict(acc.get("refreshed") or {}))
    if (data.get("refresh") or data.get("fresh")) and to:
        # a fresh token on command, taken on a limit (refresh) or on switching to this account (fresh: the
        # organization gateway gives the account with the most headroom, and on return it is already another): limits
        # and processes of the previous token are not about it; busy only if it too hits the limit
        acc.setdefault("refreshed", {})[to] = now
        if data.get("refresh"):
            acc["refresh_at"][to] = now
        (acc.get("seen") or {}).pop(to, None)
        if not episode:
            blocked.pop(to, None)
    if data.get("manual") and data.get("force") and to and to == was:
        acc["fresh_asked"] = now   # `hub accounts use <active> --force`: accounts.decide takes a fresh token of it, once
    if to:
        # how many fresh tokens in a row were handed out spent (accounts.decide: the next one after REFRESH_SPENT, doubled per
        # repeat); a move to another account or any other change ends the run, a free or unread hold of the same does not
        run = data.get("spent")
        if data.get("refresh") and isinstance(run, int) and not isinstance(run, bool) and run > 0:
            acc.setdefault("refresh_spent", {})[to] = min(run, 20)
        elif not (data.get("free") or data.get("unread")):
            (acc.get("refresh_spent") or {}).pop(to, None)
            if not acc.get("refresh_spent"):
                acc.pop("refresh_spent", None)
    if to != was or data.get("refresh") or data.get("fresh"):
        # the limits line and the executors' budget threshold (executors.waits) belong to the new login: its last known
        # numbers, not the previous one's; while there are none, empty until the first account.limits
        seen = (acc.get("seen") or {}).get(to or "")
        if engine == "codex":   # the Codex row is the active Codex account's (on a hub entirely on Codex — the hub's)
            _codex_row(st, {**seen, "engine": "codex", "account": to} if seen else {})
        elif not _codex_whole(st):   # a hub on Codex: its row is Codex's (_codex_limits)
            st["limits"] = {**seen, "account": to} if seen else {}
    gen = (acc.get("refreshed") or {}).get(to)   # the new login's token generation (processes are marked with it)
    if data.get("free") and to:
        blocked.pop(to, None)   # the active one is below the threshold again (accounts.decide): not busy
        for sess in st["sessions"].values():
            limit = sess.get("limit_cut")
            if _engine(st, sess) != engine:
                continue   # the other pool's session
            # a live or parked one (core/parking); one that fell asleep otherwise (closed by a human, stopped, handed
            # in) and one taken by the owner into a terminal (held) do not count: it will be raised or returned by a
            # message, and it continues with that
            up = sess["state"] in ("idle", "busy", "dialog", "starting", "parked")
            if limit and up \
                    and (limit.get("account"), limit.get("gen")) == (to, gen) == (sess.get("account"), sess.get("account_gen")):
                # this account's limit cut it and its window has reset (we waited for the reset or all were busy), or
                # its busy mark was stale or a refusal its token has since answered past (free without a reset):
                # continue on it, in the same process: no restart, the prompt cache is intact
                _continue_cut(st, ev, sess, LIMIT_CUT_CODEX if engine == "codex" else LIMIT_RESET)
        # not after the reset (it continues by itself) and not when its fresh token went unread for long (unread: the
        # hold ends so that the queue reads it; its reserve is not known, it is not "free")
        if data.get("channel") == "core" and was == to and not data.get("reset") and not data.get("unread"):
            notify(ev, say("core.account.free_again", to=to), fyi=True)
    # the accounts gateway (modules/gateway): sessions on it are already on the new one, the gateway substitutes the
    # token, the process is the same; a turn cut by a limit continues at once (without the gateway, by a launch on the
    # new login, _pump)
    seamless = []
    ident = (st.get("gateway") or {}).get("ident") if data.get("seamless") and to and engine == "claude" else None
    if ident:
        for name, sess in st["sessions"].items():
            if sess["state"] not in ("idle", "busy", "dialog", "starting") or sess.get("gateway") != ident:
                continue
            seamless.append(name)
            sess["account"] = to
            sess.pop("account_gen", None) if not gen else sess.update(account_gen=gen)
            limit = sess.get("limit_cut")
            if limit and (limit.get("account"), limit.get("gen")) != (to, gen):
                _continue_cut(st, ev, sess)
        if seamless:
            acc["seamless_at"] = now
    # an executor that fell asleep after the cut (the core put it to sleep idle while the pool waited for the reset or all
    # were busy) continues when a live one cut the same way would: free on the account it was cut on, or another login
    # (_pump moves a live one there, by the same _account_stale). Woken as on a hub without a pool (_limit_reset): its
    # task, the raiser's places
    for sess in list(st["sessions"].values()):
        limit = sess.get("limit_cut")
        if limit and executors.dormant(sess) and not limitresume.stopped(sess) and _engine(st, sess) == engine \
                and (data.get("free") or _account_stale(st, sess)):
            _wake_cut(st, ev, sess, _cut_text(st, sess, limit))
    if data.get("channel") == "core" and data.get("restart"):
        why = coretext.join(" (", why_of(), ")") if data.get("why") else ""
        free = coretext.join(*(say("core.account.free_after", name=n, at=at(t)) for n, t in (data.get("blocked") or {}).items()))
        default = say("core.account.default")
        head = (say("core.account.new_token", to=to, why=why) if data.get("refresh") and to == was else
                say("core.account.switched", was=was or default, to=to or default, why=why, free=free))
        rest = [n for n, x in st["sessions"].items() if n not in seamless and x["state"] in ("idle", "busy", "dialog")]
        lazy = say("core.account.lazy")
        notify(ev, say("core.account.tail_plain", head=head, lazy=lazy) if not seamless else
               say("core.account.tail_gateway", head=head) +
               (say("core.account.tail_rest", lazy=lazy) if rest else "."), fyi=True)


def _held_windows(was, data, ts, refreshed):
    """account.limits with each usage window kept from falling inside its own window. Readings of one token come from the
    processes of many sessions, each with the headers of its own last request, so they arrive out of order (95, 94, 95 within
    one window) and a share at the threshold flips the budget mode and the pool's busy mark. Within the same token and the
    same reset a share never goes down and a window the reading lacks keeps its last value; another token (the previous
    reading is older than the account's current generation, `refreshed`) or another reset is a fresh measurement. A
    rejection's own 1 is the host's mark, not a measurement: it is not kept. A drop of usage.RESET_DROP or more under the
    same reset is an early reset (core/usage.py), not a late reading: it is taken."""
    from core import usage
    old = (was or {}).get("windows")
    if not isinstance(old, dict) or (was or {}).get("status") == "rejected" or ((was or {}).get("ts") or 0) < (refreshed or 0):
        return data
    windows = data.get("windows")
    windows = dict(windows) if isinstance(windows, dict) else {}
    before = dict(windows)
    for key, w in old.items():
        if not (isinstance(w, dict) and _num(w.get("used")) and _num(w.get("resets")) and w["resets"] > ts):
            continue
        now = windows.get(key)
        if now is None:
            windows[key] = dict(w)
        elif isinstance(now, dict) and now.get("resets") == w["resets"] and _num(now.get("used")) \
                and 0 < round(w["used"] - now["used"], 6) < usage.RESET_DROP:   # rounded: 0.95 - 0.90 is 0.05
            windows[key] = {**now, "used": w["used"]}
    return data if windows == before else {**data, "windows": windows}


OVERAGE_OFF = 600   # readings without the overage flag for this long (seconds) end an episode



def _overage_notice(st, ev, ctx, name, on, windows):
    """An account starts to be billed as overage (account.limits overage false -> true): the hub is told once and tells the
    owner. The episode is kept per account outside its reading (a fresh token drops the reading, not the episode). Readings
    come out of order (a late process writes its old headers), so a reading without the flag does not end it: it ends when
    such readings go on for OVERAGE_OFF with no flagged one between, or when the windows that were used up at its start have
    reset (none used up: when its windows reset). A fresh token or a process of an old token (gen) does not start one. A hub whose sessions all run on Codex has
    nobody to tell (as _account)."""
    episodes = st["account"].get("overage") or {}
    now = ev["ts"]
    episode = episodes.get(name)
    if episode and episode.get("until") and now >= episode["until"]:
        del episodes[name]
        episode = None
    if episode:
        if on:
            episode.pop("off", None)
        elif "off" not in episode:
            episode["off"] = now
        elif now - episode["off"] >= OVERAGE_OFF:
            del episodes[name]
    elif on:
        ahead = [w for w in (windows or {}).values() if isinstance(w, dict) and isinstance(w.get("resets"), (int, float))
                 and w["resets"] > now]
        full = [w["resets"] for w in ahead if (w.get("used") or 0) >= 1]
        # none used up: the latest reset of its windows, else a week — an account not read again (the pool moved off it)
        # would keep the episode for good and its next overage would go untold
        until = max(full) if full else max((w["resets"] for w in ahead), default=now + 7 * 86400)
        st["account"].setdefault("overage", episodes)[name] = {"at": now, "until": until}
        if not _codex_whole(st):
            from core import i18n
            lang = i18n.state_lang(st)
            say = (lambda k, **p: i18n.tr(lang, k, **p)) if lang else coretext.catalog   # as _account
            ctx["notify"](ev, say("core.account.overage", account=name or say("core.account.default")))
    if not episodes:
        st["account"].pop("overage", None)   # a hub that never goes into overage keeps the account row as it was


def _account_token(st, ev, data):
    """The fingerprint (hash) of a pool account's token, written by the daemon when it differs from the one in the
    state: both `hub accounts token` and remove + add of the same name with a new token (19.10: sessions on the old token
    got "OAuth token revoked" every turn). An account's first fingerprint (new in the pool, the first run with this
    rule) is only remembered. If it changed, it is a new generation: processes on the old token are no longer on the
    current one and take the new one at the next launch (_account_stale), and limits they still write do not count (as
    with a fresh token on command). The account is the same: its known limits, busy state and place in the pool stand,
    there is no switch. Sessions on the accounts gateway are already on the new one (the gateway takes the token from
    the pool on every request)."""
    name, fp = data.get("name"), data.get("fp")
    if data.get("channel") != "core":
        raise Rejected(ACCOUNT_TOKEN_BY_CORE)
    if not (isinstance(name, str) and name and isinstance(fp, str) and fp):
        raise Rejected(ACCOUNT_TOKEN_FIELDS)
    acc = st.setdefault("account", {})
    if data.get("fresh"):
        # a fresh token by command of an account that is busy until the reset and is not the active one (accounts: the service
        # may hand out another account now): a new generation that lifts the block and the old measurement; the fingerprints
        # stay (a command token has none: it is changed by the core itself). The active one and a repeat of the same
        # fingerprint (the module reports it once) are ignored
        fresh = acc.setdefault("fresh_fp", {})
        if name == acc.get("active") or fresh.get(name) == fp:
            return
        fresh[name] = fp
        acc.setdefault("refresh_at", dict(acc.get("refreshed") or {}))
        acc["refresh_at"][name] = ev["ts"]
        (acc.get("blocked") or {}).pop(name, None)
        (acc.get("seen") or {}).pop(name, None)
        _new_generation(st, acc, name, ev["ts"])
        return
    tokens = acc.setdefault("tokens", {})
    was, tokens[name] = tokens.get(name), fp
    if was is None or was == fp:
        return
    if ((acc.get("seen") or {}).get(name) or {}).get("type") == "auth":
        acc["seen"].pop(name)   # the login refusal was for the previous token: the new one is not busy (_refuse_login)
    (acc.get("refused") or {}).pop(name, None)
    acc.setdefault("refresh_at", dict(acc.get("refreshed") or {}))   # the earlier state: refreshed served as both
    _new_generation(st, acc, name, ev["ts"])


ACCOUNT_NO_TOKEN_FIELDS = T("core.reducer.account_no_token_fields",
                            "account.no_token — by the core, with the account name and either at and until or got")


def _exhausted_key(acc, until):
    """The text of "all busy" by what holds the active account: its token command giving nothing is the issuer not
    answering, a refused sign-in (until is its own recheck) is a retry, not a reset; a limit stays the nearest reset."""
    seen = (acc.get("seen") or {}).get(acc.get("active")) or {}
    kind = seen.get("type")
    own = ((seen.get("windows") or {}).get(kind) or {}).get("resets")   # the time its own cause gives; another's is a reset
    if kind == "no_token" and own == until:
        return "core.account.exhausted_issuer"
    if kind == "auth" and own == until:
        return "core.account.exhausted_auth"
    return "core.account.exhausted"


def _account_no_token(st, ev, data):
    """The active command account's command gave no token (core/profiles.py Builtin.no_token): sessions would start on this
    computer's login, refused or absent (11-10: account: null on every turn, not the account's refusal, _off_pool). The
    account is busy until `until` (accounts.limit_hit, type no_token): the pool's decision moves to the next free one or
    says "all busy". A repeat failure while the hub is all busy on it extends that state too, so the owner hears "all busy"
    once. got — a token came: the mark, its block and the "all busy" it held go."""
    name = data.get("name")
    if data.get("channel") != "core":
        raise Rejected(ACCOUNT_NO_TOKEN_FIELDS)
    got = data.get("got") is True
    if not (isinstance(name, str) and name and (got or _num(data.get("at")) and _num(data.get("until")))):
        raise Rejected(ACCOUNT_NO_TOKEN_FIELDS)
    acc = st.setdefault("account", {})
    seen = acc.get("seen") or {}
    if got:
        if (seen.get(name) or {}).get("type") != "no_token":
            return
        mark = (((seen.pop(name).get("windows") or {}).get("no_token") or {}).get("resets")) or 0
        if acc.get("active") == name and ((acc.get("exhausted") or {}).get("until") or 0) > ev["ts"] \
                and ((acc.get("blocked") or {}).get(name) or 0) > mark:
            # all busy by more than this mark (its earlier tokens at the limit): the token it gives now is a fresh one
            # with a reserve not known yet, the episode and the block hold until its limits, as a fresh one at the limit
            # does (accounts.decide; unread, the hold ends REFRESH_EXHAUSTED from here)
            acc.setdefault("refresh_at", dict(acc.get("refreshed") or {}))[name] = ev["ts"]
            return
        (acc.get("blocked") or {}).pop(name, None)
        if acc.get("active") == name:
            acc["exhausted"] = None
        return
    until = data["until"]
    acc.setdefault("seen", {})[name] = {"status": "rejected", "type": "no_token", "at": data["at"],
                                        "windows": {"no_token": {"resets": until}}, "ts": ev["ts"]}
    exhausted = acc.get("exhausted") or {}
    end = exhausted.get("until") or 0
    if acc.get("active") == name and end > data["at"] \
            and not any(n != name and data["at"] < t <= end for n, t in (acc.get("blocked") or {}).items()):
        # still all busy on it: said once. Not when another account frees by then (the episode ends at its reset): the
        # mark would pass for a later end, and that reset for news again (accounts.decide)
        exhausted["until"] = max(end, until)


def _account_unblocked(st, data):
    """The pool's "busy until" marks (accounts._lifted) of non-active accounts that the latest measurement or a raised threshold
    no longer holds: the marks go, nothing else changes (the account is not the active one, nobody moves, nobody is told)."""
    if data.get("channel") != "core":
        raise Rejected(ACCOUNT_TOKEN_BY_CORE)
    names = data.get("names")
    for name in names if isinstance(names, list) else ():
        if isinstance(name, str):
            ((st.get("account") or {}).get("blocked") or {}).pop(name, None)


def _new_generation(st, acc, name, ts):
    """The account's token is another one: processes and limits of the previous are not about it (refreshed); sessions on the
    accounts gateway are already on the new one (the gateway takes the token from the pool on every request)."""
    acc.setdefault("refreshed", {})[name] = ts
    ident = (st.get("gateway") or {}).get("ident")
    for sess in st["sessions"].values():
        if ident and sess.get("gateway") == ident and sess.get("account") == name \
                and sess["state"] in ("idle", "busy", "dialog", "starting"):
            sess["account_gen"] = ts


def _account_refuses(st, sess, ts):
    """The session process's account refuses on a limit (its latest limits are rejected) and is busy until the reset:
    a turn on it would fail."""
    acc = _pool_of(st, _engine(st, sess))
    name = sess.get("account")
    return bool(name) and (acc.get("blocked") or {}).get(name, 0) > ts and \
        ((acc.get("seen") or {}).get(name) or {}).get("status") == "rejected"


def _account_stale(st, sess):
    """The session process was raised not on the pool's current account or not on its current token (refreshed): a new
    one will be taken at the next launch. Without a pool everyone has None, so this never happens. The pool of the
    session's engine (a Codex session: the Codex pool)."""
    acc = _pool_of(st, _engine(st, sess))
    active = acc.get("active")
    return (sess.get("account"), sess.get("account_gen")) != (active, (acc.get("refreshed") or {}).get(active))


def _gateway(st, ev, data, ctx):
    """The accounts gateway came on, went off or moved (ident: port and secret). New launches go through it; sessions
    on the previous gateway restart with the same conversation (free ones at once, busy ones after the turn), until
    then the gateway keeps serving them (users in view.json); those working directly stay as they were until their
    own restart."""
    if data.get("channel") != "core":
        raise Rejected(GATEWAY_BY_CORE)
    from core import i18n
    lang = i18n.state_lang(st)
    say = lambda key, **p: i18n.tr(lang, key, **p)  # noqa: E731
    was = st.get("gateway") or {}
    on, ident = bool(data.get("on")), data.get("ident") if data.get("on") else None
    if on and not ident:
        raise Rejected(GATEWAY_NEEDS_IDENT)
    st["gateway"] = {"on": on, "ident": ident, "since": ev["ts"],
                     # why with the marks of its phrases (why_l10n): the page and the status show it in the reader's
                     # language (coretext.got)
                     **(coretext.put({}, "why", coretext.got(data, "why")) if data.get("why") else {}),
                     **({"code": data["code"]} if data.get("why") and isinstance(data.get("code"), str) else {})}
    moved = []
    for name, sess in st["sessions"].items():
        # a starting one too: its process may already have left with the old address, so restart, and the mark stays
        # until then (the gateway keeps serving)
        if sess.get("gateway") and sess["gateway"] != ident and sess["state"] in ("starting", "idle", "busy", "dialog"):
            sess["dirty"] = True
            sess["respawn_now"] = True   # the old address may be gone already: background work does not hold it
            moved.append(name)
    # the reason in the same language as the note (say: none known — English)
    why = f" ({coretext.human(coretext.got(data, 'why'), lang or i18n.SOURCE)})" if data.get("why") else ""
    if on and not was.get("on"):
        ctx["notify"](ev, say("core.gateway.on"))
    elif not on and was.get("on"):
        ctx["notify"](ev, say("core.gateway.off_failed" if data.get("failed") else "core.gateway.off", why=why) + (
            say("core.gateway.moved", names=", ".join(moved)) if moved else ""))
    elif not on and data.get("failed"):
        ctx["notify"](ev, say("core.gateway.failed", why=why))
    elif moved:
        ctx["notify"](ev, say("core.gateway.port", names=", ".join(moved)))


GATEWAY_BYPASSED = "spawned direct"   # the result of a gateway launch that a core module (launch.env) pointed at its own address

LIMIT_CUT_SEAMLESS = T("core.msg.limit_cut_seamless",
                       "The Claude account limit cut off your turn; the hub switched Claude to another account. Look at "
                       "where you stopped and continue the work from there. Nothing to continue — answer with one line "
                       "«{nothing}».", nothing=NOTHING)


LIMIT_RESET = T("core.msg.limit_reset",
                "The Claude account limit cut off your turn; its window has reset, the account is the same. Look at where "
                "you stopped and continue the work from there. Nothing to continue — answer with one line «{nothing}».", nothing=NOTHING)


def _continue_cut(st, ev, sess, text=LIMIT_CUT_SEAMLESS, mark="limit_cut"):
    """Continue a turn cut by a limit with a core message in its chain, without a restart: the accounts gateway is
    already on another account or the same account's window has reset. mark net_cut — a turn the network cut, the API
    answers again (_net_resume)."""
    limit = sess.pop(mark)
    go = new_message(st, f"m{ev['seq']}x5-{sess['name']}", sess["name"], text, ev["ts"], origin=forms.ORIGIN_CORE,
                     **({"root": sess["turn_root"]} if sess.get("turn_root") else {}))
    go.update({k: limit[k] for k in ("re", "for_delegation") if limit.get(k)})
    sess["resume"] = go["id"]


def net_cut(data):
    """The turn result is a network cut (NET_TEXT): Claude Code could not reach the API and gave up; or the API refused the
    network itself (NO_ROUTE_TEXT: the VPN is off)."""
    text = data.get("text") or ""
    return bool(data.get("is_error") and (NET_TEXT.search(text) or NO_ROUTE_TEXT.search(text)))


def _net_resume(st, ev):
    """The term after a network cut is up and the API answers (hubd fires the n: timer only then): the session continues
    the cut turn, in its chain and the same matter (an assignment or the owner's thread). Once: the mark goes with the
    timer. Nothing to continue: a working or parked session only (NET_CONTINUABLE), and not twice — a limit cut or a
    restart (cut_turn) continues it by its own way, a message waiting for the session starts a turn anyway."""
    sess = st["sessions"].get(str(ev.get("entity"))[len(NET_TIMER):])
    if not (sess or {}).get("net_cut"):
        return   # a stale timer: a turn began, the session went
    if any(limitresume._ends_cut(m) for m in _messages_of(st, sess["name"])):
        return   # a message on its way starts the turn and its start clears the mark; removed — the timer is back
    if sess["state"] not in NET_CONTINUABLE or sess.get("limit_cut") or sess.get("cut_turn"):
        sess.pop("net_cut")
        return
    _continue_cut(st, ev, sess, NET_CUT, mark="net_cut")


def _limit_reset(st, ev, data, sess, actions):
    """The daemon (core/limitresume.py) found the window of a cut turn over: the session continues, without a restart. A fact
    for a session that is no longer cut by this window (a turn began, another reset) is a fact with no consequences, like a stale
    timer. An executor that fell asleep after the cut is woken by the core, as `hub session wake`
    would, with its task continued; if the raiser's places are taken it waits in the queue like any wake."""
    if data.get("channel") != "core":
        raise Rejected(LIMIT_RESET_BY_CORE)
    cut = (sess or {}).get("limit_cut")
    if not isinstance(cut, dict) or cut.get("resets") != data.get("resets") or cut.get("account") is not None \
            or sess["state"] not in limitresume.CONTINUABLE or limitresume.stopped(sess):
        return
    sess["limit_reset_done"] = cut["resets"]
    if executors.dormant(sess):
        return _wake_cut(st, ev, sess)
    _continue_cut(st, ev, sess, LIMIT_RESET)


CUT_STAYS_ASLEEP = T("core.msg.cut_stays_asleep",
                     "The Claude limit window has reset, but executor {name}, cut by the limit on task {did}, stayed asleep: "
                     "{why}. To wake it when possible - hub session wake {name}.")
TRAIL_WOKEN_RESET = T("core.msg.trail_woken_reset", "{name} woken: the limit window has reset")
# continued on another account (_cut_text: LIMIT_CUT, LIMIT_CUT_CODEX_POOL): no window has reset
CUT_STAYS_ASLEEP_POOL = T("core.msg.cut_stays_asleep_pool",
                          "The account pool has room again, but executor {name}, cut by the limit on task {did}, stayed "
                          "asleep: {why}. To wake it when possible - hub session wake {name}.")
TRAIL_WOKEN_POOL = T("core.msg.trail_woken_pool", "{name} woken: the account pool has room again")
def _resume_requested(st, ev, data, sess):
    """The owner's (or the hub's) "Continue" on a session paused by a limit (POST /api/session/resume): the cut turn continues
    now, the same way the clock (_limit_reset) or the pool (account.switched free) would continue it at the reset - not
    before it (limitresume.held: the window still refuses, the reply names when it continues by itself). The new turn ends
    the pause as any turn does (turn.started drops limit_cut; modules that hold the session on a limit see it too)."""
    if guards.from_owner_or_hub(st, data):
        raise Rejected(T("core.reducer.resume_who", "only the owner or the hub continues a session after a limit"))
    if not sess:
        raise Rejected(refusal(SESSION, "absent", ev["type"]))
    now = ev["ts"]
    why = limitresume.held(st, sess, now)
    if why and why[0] == "not_paused":
        raise Rejected(forms.Why(T("core.reducer.resume_not_paused", "the session is not paused by a limit: there is nothing to continue"), "not_paused"))
    if sess["state"] not in limitresume.CONTINUABLE:   # asleep, held by the owner, departed: machines: refusals
        raise Rejected(_named(refusal(SESSION, sess["state"], ev["type"], _real(sess["name"])), sess["name"]))
    if why:
        # one a person stopped does not continue by itself (limitresume.stopped): it waits for this button again
        raise Rejected(forms.Why((T("core.reducer.resume_not_yet_stopped",
                                    "the limit has not reset yet — the session was stopped and does not continue by "
                                    "itself: continue it after {at}") if limitresume.stopped(sess) else
                                  T("core.reducer.resume_not_yet",
                                    "the limit has not reset yet — the session continues by itself at {at}")
                                  ).format(at=limitresume.at(st, why[1], now)), "not_yet"))
    cut = sess["limit_cut"]
    text = _cut_text(st, sess, cut)
    if limitresume.number(cut.get("resets")) and cut.get("account") is None and cut.get("engine") != "codex":
        sess["limit_reset_done"] = cut["resets"]   # the clock does not continue the same window a second time
    if executors.dormant(sess):
        return _wake_cut(st, ev, sess, text)
    _continue_cut(st, ev, sess, text)


def _cut_text(st, sess, cut):
    """The words a cut turn continues with. With a pool it continues on the active account: on the cut one in the same
    process, on another after a launch with the same conversation (_pump moves it, the message waits for the launch); a
    Codex cut - the words its own clock and pool continue it with (codex.limit_reset and session.started)."""
    moved = _account_stale(st, sess)
    return ((LIMIT_CUT_CODEX_POOL if moved else LIMIT_CUT_CODEX) if cut.get("engine") == "codex"
            else LIMIT_CUT if moved else LIMIT_RESET)


def _woken_elsewhere(text):
    """The cut turn continues on another account of the pool (the words _cut_text chose), not after its window's reset."""
    return text in (LIMIT_CUT, LIMIT_CUT_CODEX_POOL)


def _wake_cut(st, ev, sess, text=LIMIT_RESET):
    """A retired executor cut by the limit: its task (the cut one, else its last) continues. No task - nothing to continue
    (the mark is dropped). The raiser may not have executors, or the copy is taken: the raiser gets one line instead. Places
    or the budget are taken: the same queue as a raiser's wake (_raiser_wakes), raised by itself (_wake_ready)."""
    name, cut = sess["name"], sess.pop("limit_cut")
    did = cut.get("for_delegation") or _last_task(st, name)
    d = st["delegations"].get(did or "") or {}
    if d.get("session") != name or d.get("state") in ENDED:   # accepted or archived meanwhile: nothing to continue
        return
    raiser = sess.get("raised_by")
    by = raiser if (st["sessions"].get(raiser or "") or {}).get("state") not in (None, "removed", "closed", "failed") \
        else _hub(st)
    try:
        _wake_gate(st, sess, direct=by != raiser)   # the raiser is told by the note, the hub (its stand-in) is told who continues
    except Rejected as e:
        if by and by != name:
            why = e.args[0] if len(e.args) == 1 and isinstance(e.args[0], str) else str(e)   # with its phrases' marks
            new_message(st, f"m{ev['seq']}x5-{name}", by,
                        (CUT_STAYS_ASLEEP_POOL if _woken_elsewhere(text) else CUT_STAYS_ASLEEP).format(name=name, did=did, why=why),
                        ev["ts"], origin=forms.ORIGIN_CORE)
        return
    wait = executors.waits(st, by, now=ev["ts"]) if by else None
    runs = st.setdefault("runs", {})
    if wait and name not in runs:
        runs[name] = {"by": by, "task": str(text), "ts": ev["ts"], "state": "queued", **forms.why_fields(wait),
                      "minihub": bool(minihub.on(st, st["sessions"].get(by))),
                      "wake": {"about": did, "text": str(text), "l10n": coretext.marks(text), "attachments": [], "cut": cut}}
        return
    if not wait:
        _core_wake(st, ev, sess, did, cut, text)


def _core_wake(st, ev, sess, did, cut, text=LIMIT_RESET):
    """Wake a retired executor with the continuation of its cut task did: a core message in the cut turn's chain, the task is
    in progress again (the same as the wake by the hub with `--about`, _received)."""
    name = sess["name"]
    sess["state"] = step(SESSION, "retired", "_unretire")[0]
    delegations.trail(st, did, delegations.BY_CORE, (TRAIL_WOKEN_POOL if _woken_elsewhere(text) else TRAIL_WOKEN_RESET).format(name=name))
    go = new_message(st, f"m{ev['seq']}x5-{name}", name, text, ev["ts"], origin=forms.ORIGIN_CORE,
                     **({"root": sess["turn_root"]} if sess.get("turn_root") else {}))
    go.update({k: cut[k] for k in ("re", "for_delegation") if cut.get(k)})
    delegations.continued(st, name, go["id"], ev, did)
    sess["resume"] = go["id"]


def _notify_hub(st, ev, text, fyi=False, **extra):
    """A verdict on an assignment goes to the hub as a core message (task is which task: the chain, core/chain.py). A
    mini-hub executor's task bypasses the hub (the bus): whoever raised it already got REPLIED and STOPPED (the result,
    the STOPPED line), and the rest (TIMEOUT, silence) goes to it.
    fyi: the hub has nothing to do about it (an account switch, "hub restarted", a stop it made itself): an aside, it
    wakes nothing and rides with the hub's next message (_ride)."""
    d = st["delegations"].get(extra.get("task") or "") or {}
    up = minihub.boss(st, st["sessions"].get(d.get("session") or ""))
    if up and d.get("for_session") == up:
        if not text.startswith(("REPLIED", "STOPPED")):
            new_message(st, f"m{ev['seq']}", up, text, ev["ts"], origin=forms.ORIGIN_CORE, **extra)
        return
    hub = _hub(st)
    if hub:
        new_message(st, f"m{ev['seq']}", hub, text, ev["ts"], origin=forms.ORIGIN_CORE, **({"fyi": True} if fyi else {}), **extra)


def _slept(st, data):
    """The machine slept for data.seconds (the wall clock ran, the hub and its sessions did not: macOS DarkWake gives a
    window of 10-50 s every 15 minutes; 05.11: /compact took 1904 s instead of ~55 s). The terms that wait for a living
    process get that time back: the guard of a compaction in progress, the hold of a folder being fast-forwarded and the delivery deadline of a message being typed
    (its attempts are counted per term: four of them ran out across one sleep). The rest of the terms are wall-clock by
    meaning (the schedule, the owner's hours, the cache that lives an hour)."""
    n = data.get("seconds")
    if isinstance(n, bool) or not isinstance(n, (int, float)) or not 0 < n < 7 * 86400:   # nan fails the comparison too
        return
    for sess in st["sessions"].values():
        if sess.get("compacting"):
            sess["compacting"] += n
        if isinstance(sess.get("forwarding"), dict) and isinstance(sess["forwarding"].get("since"), (int, float)):
            sess["forwarding"]["since"] += n   # git fast-forwarding its folder slept too (_folder_forward)
    for m in st["messages"].values():
        if m["state"] == "typing" and m["deadline"] is not None:
            m["deadline"] += n


def _timer(st, ev, data, actions):
    msg = st["messages"].get(ev.get("entity"))
    if not msg or msg["state"] != "typing" or data.get("attempt") != msg["attempt"]:
        return  # a stale timer is a fact with no consequences
    ts = ev["ts"]
    sess = st["sessions"][msg["session"]]
    if parking.mcp_init(sess, ts) and msg["since"] >= sess["mcp_init"]:
        # the line is in the stdin of a process still connecting MCP: wait, spend no attempt (one typed into the
        # previous process is retried as usual)
        msg["deadline"] = ts + _term(msg)
    elif msg["attempt"] >= MAX_ATTEMPTS:
        _msg_move(msg, "_fail", ts)
        msg["reason"] = GAVE_UP
    elif sess["state"] == "idle":
        _msg_move(msg, "_retry", ts)
        _typed(msg, ts, actions)
    else:
        _msg_move(msg, "_requeue", ts)


# --- queue -----------------------------------------------------------------------------------------------------

def _pump_quiet(sess):
    """For a free session with no messages _pump does nothing: there are no marks the loop branches work on without
    messages (respawn_minihub and compact by key presence, the rest by value, as the branches read them). _pump runs on
    every event and the state holds hundreds of sessions (25.10: 389): a full pass over each on every event costs about
    a quarter of the CPU while the page reads the journal for the first time (a copy, 20k events: 6.8 -> 5.1 s)."""
    return not ("respawn_minihub" in sess or "compact" in sess or sess.get("rename_to") or sess.get("plugins_stale")
                or sess.get("dirty") or sess.get("limit_cut"))


def queue_key(m):
    """The typing order in a session's queue: urgent first, then older, and on equal `created` by number (m9 before m10;
    as strings it would be the other way round). The watchdog uses the same order: its "head of the queue" is what the core types first."""
    tail = re.match(r"m(\d+)", m["id"])
    return not m.get("urgent"), m["created"], int(tail.group(1)) if tail else -1, m["id"]


def _pump(st, ev, actions):
    """For a free session with no message being typed, type the oldest from the queue."""
    ts = ev["ts"]
    active = {}  # queue and typing per session: one pass over messages per event, not per session (01.10)
    for m in st["messages"].values():
        if m["state"] in ("queued", "typing"):
            active.setdefault(m["session"], []).append(m)
        elif m["state"] == "aside":   # an "FYI" is delivered together with its carrier (_ride)
            carrier = st["messages"].get(m.get("with") or "") or {}
            if carrier.get("state") == "delivered":
                _msg_move(m, "_delivered", ts)
                m["delivered_at"] = carrier.get("delivered_at") or ts
                m.pop("with")
            elif carrier.get("state") not in ("queued", "typing"):
                # the carrier did not arrive, so it rides with the next one (a turn with the undelivered one's mark may
                # still come and it would be read twice: better than losing it)
                m.pop("with", None)
                if executors.dormant(st["sessions"].get(m["session"])):
                    # the executor handed in or was stopped, so it will have no turn: it goes to whoever raised it, like messages to it
                    up, note = _to_raiser(st, m["session"], fyi=True)
                    if up and up == m.get("sender"):
                        # the sender raised it: the raiser's queue is its own, the FYI would only come back (as at send time)
                        _msg_move(m, "_fail", ts)
                        m["reason"] = "its sender raised the executor, which fell asleep"
                    elif up and up != m["session"]:
                        if m["session"] in m.get("fyi_for", ()):
                            note = None   # a merged FYI already names every address (_merge_fyi)
                        m["routing_note"], m["session"] = _join_note(m.get("routing_note"), note), up
    for name, sess in list(st["sessions"].items()):
        if sess.pop("mcp_live", None) and sess["state"] in ("busy", "dialog") and _engine(st, sess) == "claude":
            # an MCP granted mid-turn joins the running Claude Code now (owner decision 2026-11-11: in the same turn); the
            # restart after the turn (dirty) stays the fallback and the source of truth. Codex keeps the restart
            actions.append({"do": "mcp_set", "session": name, "tools": list(sess.get("tools") or []),
                            "live": tools.live(st, sess)})
        if sess["state"] == "busy":   # an urgent message waited for the session to answer "yes" or come up; it is mid-turn again: now
            for m in active.get(name, []):
                if m.get("urgent") and m["state"] == "queued":
                    _rush(st, m, ts, actions)
        if sess["state"] != "idle":
            continue
        if parking.compacting(sess, ts):
            # /compact is running: a relaunch (rename, tools, model, account, plugin, mini-hub) and typing wait for its
            # result: release+spawn would interrupt the compaction. What was typed waits for the end: a message's
            # confirmation counts attempts at 30 seconds, while /compact of a big conversation takes minutes; it waits
            # no longer than the core waits for the command's own result (COMMAND_CONFIRM_SECONDS)
            for m in active.get(name, []):
                if m.get("held"):   # deferred by the hub: goes to it as without the compaction, typed after it
                    _release(st, m, ts, FREED_GOT_IT.format(name=name))
            continue
        if "forwarding" in sess and forwarding(sess, ts):
            continue   # git is moving the files of its folder (_folder_forward): typing and a relaunch wait for it

        if name not in active and _pump_quiet(sess):
            continue   # free, nothing to write to it and no marks: none of the branches below would fire
        if sess.get("rename_to") and not sess.get("pending") and not sess.get("background") and not active.get(name):
            new = sess.pop("rename_to")
            try:
                _rename(st, {**ev, "entity": name}, {"to": new, "channel": "cli", "owner": True}, actions)
            except Rejected:
                pass  # the name was taken while the turn ran: the old one stays
            continue
        if "respawn_minihub" in sess and not active.get(name) and parking.parkable(st, sess, now=ts):
            # it became or stopped being a mini-hub and has no task: park it, the new rules come with the first message
            # (the first run of parallelism switches dozens of free specialists, so do not raise them all at once)
            sess.pop("respawn_minihub")
            _session_event(st, {"type": "session.parked", "entity": name, "ts": ts, "seq": ev["seq"]},
                           {"channel": "core", "why": "minihub"}, actions)
            continue
        if (sess.get("dirty") or "respawn_minihub" in sess or (sess.get("browser_stale") and active.get(name))) \
                and not (sess.get("hold_respawn") and any(m["state"] == "typing" for m in active.get(name, []))) \
                and not _relaunch_waits(sess, active.get(name), ts):
            # a new tool set (or the browser was reset and a message arrived): release the free session and raise it
            # again via --resume; the message arrives after the launch. A folder change (hold_respawn) waits for the
            # turn to begin if the message is typed: turn.started comes later in the journal (24.10: 20 s), and
            # release would interrupt the turn
            _tools_apply(st, sess, ev, actions)
            continue
        mine = active.get(name, [])
        if _account_stale(st, sess) and (sess.get("limit_cut") or not any(m["state"] == "typing" for m in mine)
                                         and any(m["state"] == "queued" and not m.get("handover") for m in mine)
                                         and (not restart.working(sess, ts) or _account_refuses(st, sess, ts))):
            # the account changed (Claude accounts): move to the new one before a message, if no background work runs or
            # the old account already refuses (a turn there would fail on the limit); a turn cut by a limit moves now:
            # there is nothing to continue on the old one
            _tools_apply(st, sess, ev, actions, account=_engine(st, sess))
            continue
        if sess.get("plugins_stale") and not any(m["state"] == "typing" for m in active.get(name, [])) \
                and not restart.working(sess, ts):
            # an updated plugin (12.10): when free and no background work runs (a service such as a dev server does not
            # hold it, as with a hub restart): with no task, park it and the new version comes with the first message;
            # with a task, restart now. They are not all raised at once, which is what parking is for (core/parking.py)
            sess.pop("plugins_stale")
            if parking.parkable(st, sess, now=ts):
                # the same parking as in reconcile and idleness (hubd writes session.parked): a SESSION machine
                # transition, the assignment board, cancelling the handover, release
                _session_event(st, {"type": "session.parked", "entity": name, "ts": ts, "seq": ev["seq"]},
                               {"channel": "core", "why": "plugins"}, actions)
            else:
                _tools_apply(st, sess, ev, actions)
            continue
        for m in mine:
            if m.get("held"):   # it freed up before the hub decided, so it gets it itself
                _release(st, m, ts, FREED_GOT_IT.format(name=name))
        if any(m["state"] == "typing" for m in mine):
            continue
        compact = sess.pop("compact", None)
        if compact:   # compact the conversation while free, before the next message
            actions.append({"do": "compact", "session": name, **({"text": compact} if isinstance(compact, str) else {})})
            sess["compacting"] = ts
            continue
        for m in mine:   # a "handed to an executor" mark with no executor queued or running: the task is back with the specialist
            h = m.get("handover")
            if h and h not in (st.get("runs") or {}) and h not in st["sessions"]:
                m.pop("handover")
        queued = [m for m in mine if m["state"] == "queued" and not m.get("handover")]
        if queued:
            msg = min(queued, key=queue_key)   # urgent first
            _msg_move(msg, "_pump", ts)
            # what changed for sessions and it has not seen yet goes with this message; for a command the end of the input is its arguments
            fresh = None if msg.get("command") else news.unseen(st, sess)
            if fresh:
                msg["news"] = fresh
            reacts = _reacts_note(sess) if msg.get("from_owner") and not msg.get("command") else None
            if reacts:
                msg["reacts"] = reacts
            _ride(st, msg)
            if sess.get("kind") == "hub" and msg.get("from_owner") and not msg.get("command") \
                    and not msg.get("answers_question"):
                # his words to the hub may answer a session's card: the hub sees the open ones with numbers (--card)
                cards = owner.cards_note(st, msg["id"], ts, hub=name)
                if cards:
                    msg["cards"] = cards
            _typed(msg, ts, actions)


def _plugins_updated(st, old):
    """A plugin was updated (`claude plugin update`, a git pull of the set; 12.10: a busy session stayed on the old
    version until a manual restart): live sessions with it get a mark, and _pump restarts them when they free up.
    Updated means a different folder (a copy from Claude Code with the version in the path) or a different hash of its
    folder in the set (tree): a commit to a neighboring plugin or a README does not touch it. Core plugins arrive with a
    hub update, and its restart picks them up."""
    changed = set()
    for tid, t in st["catalog"].items():
        was = old.get(tid)
        if t.get("kind") != "plugin" or not was or t.get("source") == "core":
            continue
        if was.get("path") != t["path"] or (was.get("tree") and t.get("tree") and was["tree"] != t["tree"]):
            changed.add(tid)
    if not changed:
        return
    for sess in st["sessions"].values():
        if sess["state"] not in LIVE:
            continue
        ids = [tid for tid, t in st["catalog"].items() if t.get("base")] + list(sess.get("tools") or [])
        ids += [w for tid in ids for w in (st["catalog"].get(tid) or {}).get("with") or []]
        hit = sorted(changed & set(ids))
        if hit:
            sess["plugins_stale"] = sorted(set(sess.get("plugins_stale") or []) | set(hit))


def _relaunch_waits(sess, mine, ts):
    """A relaunch for a new tool set, an MCP fix or the mini-hub mode waits while the session's background
    work runs and nobody writes to it — the same rule (restart.working) as an updated plugin and another account: release
    would cut the work (claude does not exit while its background runs; killed after 20 s, the tasks orphaned — 11.11).
    A message for the session (mine) goes with the new process, so it relaunches at once, as before; so does what was
    asked for by name, takes something away or cannot wait (respawn_now: `hub session reload`, the model, the effort,
    the folder, the spec mode, a revoked tool, a one-for-all tool passed on, a tool the session asked for, a gateway
    move). A message the hub holds back (held) or one for a handover does not count: it is not typed now."""
    typed = [m for m in mine or () if not m.get("held") and not m.get("handover")]   # what would be typed now
    return not typed and not sess.get("respawn_now") and restart.working(sess, ts)


def _tools_apply(st, sess, ev, actions, account=False):
    to, todo = step(SESSION, sess["state"], "_tools_apply")  # noqa: F841 — actions are collected below
    sess["state"], sess["since"] = to, ev["ts"]
    bg = [t.get("title") or t.get("id") for t in (sess.get("background") or {}).values()]
    # a relaunch only because of a mini-hub or account flag change: its reason goes in the cut text; if there are
    # other reasons, the old text
    other = sess.get("dirty") or sess.get("plugins_stale") or sess.get("browser_stale")
    switched = "respawn_minihub" in sess and not other
    sess.pop("respawn_minihub", None)
    asked = sess.pop("reload_asked", None)
    if bg or asked:
        # background agents and commands leave with the process, and there is no session.exited here (06.10: five
        # "In background" lines hung for an hour, the session waited for agents that no longer existed): clear them and say so after the launch
        sess["cut_turn"] = {"ts": ev["ts"], "background": bg, "respawn": True,
                            **({"minihub": minihub.on(st, sess)} if switched else {}),
                            **({"account": account} if account and not other else {}),
                            **({"asked": asked} if asked else {})}
        _bg_gone(sess, sess.get("background") or {}, ev["ts"])
        sess["background"] = {}
    actions.append({"do": "release", "session": sess["name"]})
    actions.append(spawn_action(sess, st=st))
    frees = sess.pop("frees", None)
    if frees:  # it released a "one for all" tool: the new holder, if already up, restarts once more, to the free one
        holder = st["sessions"].get(frees["to"])
        if holder and frees["tool"] in (holder.get("tools") or []) and holder["state"] in ("starting", "idle", "busy", "dialog") \
                and not holder.get("dirty"):
            holder["dirty"] = holder["respawn_now"] = True
    added = sess.pop("new_tools", [])
    busy_with = [d for d in st["delegations"].values() if d["session"] == sess["name"] and d["state"] not in ENDED]
    if added and (busy_with or any(a["asked"] for a in added)):
        added = [a["tool"] for a in added]
        # the session comes up with the same conversation but will not continue by itself (30.09: one stood idle)
        mid = f"m{ev['seq']}x2"  # m{seq} in this same event may be taken by a notice to the hub
        did, task = delegations.open_task(st, sess["name"])
        # repeat the task: sessions ended a turn for nothing on "continue the task" without it (the hub on 01.10, three times a day)
        text = CONNECTED.format(tools=", ".join(added))
        if task:
            text = coretext.join(text, "\n\n", CONNECTED_TASK.format(did=did), "\n", task)
        new_message(st, mid, sess["name"], text, ev["ts"], origin=forms.ORIGIN_CORE)
        if did:  # the same assignment as in the text: alive and from someone who still exists (03.10)
            delegations.continued(st, sess["name"], mid, ev, did)
        cut = sess.get("cut_turn") or {}
        if cut.get("asked"):   # "continue" has already been said, no second one is needed
            sess["reload_went"] = True
            if cut.get("background"):
                cut.pop("asked")
            else:
                sess.pop("cut_turn")


CONNECTED = T("core.msg.connected",
              "Connected: {tools}. The session was restarted with the same conversation — continue the task this was "
              "needed for.")
CONNECTED_TASK = T("core.msg.connected_task", "The task ({did}) it was asked for — finish it in this turn:")


def attachments_note(files):
    """Attachments as paths in the text (an "attachment: <path>" line); the host attaches images by itself too."""
    if not files:
        return ""
    # the mark of a pasted image is the same as in the owner's text: "[Image #1]" (as in Claude Code); files are already
    # clean_attachments (_typed)
    lines = [f"- {'[' + str(f['label']) + '] ' if f.get('label') else ''}{f['path']} ({f.get('name') or ''}, "
             f"{_size(f) // 1024} KB)" for f in files]
    return "\n\nAttachments:\n" + "\n".join(lines)   # a tail of the typed input: only the session's model reads it


def failed_note(failed):
    """Attachments that did not arrive (the Telegram bot did not download them): the reason goes to the session
    separately from the owner's words; the bot itself writes it to the owner."""
    if not failed:
        return ""
    lines = "\n".join(f"- {f['reason']}" for f in failed)
    return f"\n\nNot downloaded from Telegram (the bot tells the owner the reason there too):\n{lines}"


def clean_failed(failed):
    """An event's attach_failed: [{name, reason, size?}] as strings; anything else is dropped, long values are truncated."""
    if not isinstance(failed, list):
        return []
    return [{"name": f["name"][:200], **({"size": f["size"]} if _size(f) else {}), "reason": f["reason"][:400]}
            for f in failed[:10] if isinstance(f, dict) and isinstance(f.get("name"), str) and f["name"]
            and isinstance(f.get("reason"), str) and f["reason"]]


def _size(f):
    return f["size"] if isinstance(f.get("size"), int) and not isinstance(f["size"], bool) else 0


def clean_attachments(files):
    """A message's attachments as the core stores them: objects with a path and an integer size. Anything else (a
    plugin, a hand-written spool file) is dropped: a file without a path broke every following event."""
    if not isinstance(files, list):
        return []
    return [f if _size(f) == f.get("size", 0) else {**f, "size": 0} for f in files
            if isinstance(f, dict) and isinstance(f.get("path"), str) and f["path"]]


def injectable(msg):
    """Can it be inserted mid-turn: not something that rests on the turn boundary: an assignment (its result is the
    reply; an urgent one from the hub can be, since this turn's result becomes its reply too), "hand over your work", a
    schedule job, a core service message to an agent ("finish your CLAUDE.md")."""
    return not ((msg.get("delegation") and not msg.get("urgent")) or msg.get("schedule") or msg["text"].startswith(HANDOFF_MARK)
                or msg["id"].endswith("x7") or msg.get("command"))   # a command mid-turn is just text


# the reasons the fold and the watchdog compare (in one process, with these very constants)
NOT_QUEUED = T("core.reducer.not_queued", "the message is not in the queue")
NOT_IN_TURN = T("core.reducer.not_in_turn", "the session is not in a turn — the message will go the usual way")
UNKNOWN = T("core.reducer.unknown_session", "unknown session")
NOT_FREE = T("core.reducer.not_free", "the session is already working or asking with a form")
HELD_BY_HUB = T("core.reducer.held", "the message waits for the hub's decision")
QUEUE_ONLY = T("core.reducer.queue_only", "a task, a handover, a schedule job — only through the queue, with a new turn")
MINIHUB_RESULT = T("core.reducer.minihub_result", "a mini-hub executor: the result goes to {boss}, not to the owner")
ATTENTION_OFF = T("core.reducer.attention_off",
                  "the session said itself: the result of this turn does not wait for the owner (hub attention off)")
HANDING_OVER = T("core.reducer.handing_over", "the session is handing over")
EXECUTOR_RESULT = T("core.reducer.executor_result", "an executor: the result went to whoever set the task")


def inject_refused(st, msg):
    """Why it cannot be inserted mid-turn (None means it can). One rule for the fold and the watchdog: it checks it
    against the fresh journal before the fact (16.10: 211 refusals, the turn ended while the model weighed urgency)."""
    if not msg or msg["state"] != "queued":
        return NOT_QUEUED
    if msg.get("held"):
        return HELD_BY_HUB
    if (st["sessions"].get(msg["session"]) or {}).get("state") != "busy":
        return NOT_IN_TURN
    if not injectable(msg):
        return QUEUE_ONLY
    return None


def attention_refused(st, sess):
    """Why the "waits for you" mark must not be set (None means set it). One rule for the fold and the watchdog."""
    if not sess or sess["state"] == "removed":
        return UNKNOWN
    if sess["state"] != "idle" or sess.get("pending"):
        return NOT_FREE
    if minihub.boss(st, sess):   # the bus: a mini-hub executor's result goes to the mini-hub, the owner does not wait for it
        return MINIHUB_RESULT.format(boss=minihub.boss(st, sess))
    if "attention_off" in sess and sess["attention_off"] == sess.get("turn_seq"):
        return ATTENTION_OFF
    return None


# a "you promised" reminder to one session goes no more often than this (owner decision 2026-10-26). A week of the
# journal: 15 and 30 min give 42 and 41 reminders, of which 11 and 10 of the labeled ones are real; a second promise
# from the same session can come 10-20 minutes later
PROMISE_COOLDOWN = 15 * 60
PROMISE_NUDGE = T("core.msg.promise_nudge",
                  "Core: your last turn ended with a promise — \"{what}\" — but nothing was started in it, and nothing you "
                  "have in flight will do it. This is your job, and it can be done now — do it (a command, an edit, "
                  "`hub task run`). Already done, waiting for an event, a date or a person, or it was not a promise — "
                  "answer with a single \"—\", starting nothing: such an answer goes to no one.")


def promise_refused(st, sess):
    """Why not to remind a session of a promise from its result (None means we can): it is free, nothing waits in its
    queue, and it is not an executor (its result already went to whoever set the task; those that handed in are not woken).
    One rule for the fold and the watchdog."""
    why = attention_refused(st, sess)
    if why in (UNKNOWN, NOT_FREE):
        return why
    if sess.get("closing", "none") != "none":
        return HANDING_OVER
    if minihub.runner(sess):
        return EXECUTOR_RESULT
    if any(m["session"] == sess["name"] and m["state"] in ("queued", "typing") for m in st["messages"].values()):
        return PROMISE_QUEUED
    return None


PROMISE_QUEUED = T("core.reducer.promise_queued", "it is already being written to: a message is in the queue")
PROMISE_BY_WATCH = T("core.reducer.promise_by_watch", "a promise reminder is set by the watchdog, not by a session")
PROMISE_SAID = T("core.reducer.promise_said", "this result has already been told about")
PROMISE_EARLY = T("core.reducer.promise_early", "too early: it has already been reminded of a promise")
PROMISE_DEFAULT = "I will do it"   # the promise's words when the watchdog gave none (the session's model reads it)


def _watch_promise(st, ev, data):
    """Watchdog: a result ended with a promise of its own action and nothing in flight covers it (modules/watch, the
    `"promise": true` option): one reminder to the session per result, no more often than PROMISE_COOLDOWN; the fact in
    the journal also serves for counting accuracy. A stale one (the session has begun a turn since, or is being written
    to) is dropped quietly, with no refusal."""
    name = ev.get("entity")
    if data.get("channel") == "session":
        raise Rejected(PROMISE_BY_WATCH)
    sess = st["sessions"].get(name or "")
    why = promise_refused(st, sess)
    if why:
        queued = [m["created"] for m in st["messages"].values()
                  if sess and m["session"] == name and m["state"] in ("queued", "typing")]
        if why in (UNKNOWN, NOT_FREE, PROMISE_QUEUED) and sess and overtaken(data, sess.get("since"), *queued):
            return
        raise Rejected(why)
    if data.get("turn") != sess.get("turn_seq"):
        return   # a new turn has run since the result the watchdog read: the promise may have been kept, so do not remind
    box = st.setdefault("watch", {}).setdefault(PROMISE_KEY, {})
    last = box.get(name) or {}
    if last.get("of") == data.get("of"):
        raise Rejected(PROMISE_SAID)
    if ev["ts"] - last.get("at", -PROMISE_COOLDOWN) < PROMISE_COOLDOWN:
        raise Rejected(PROMISE_EARLY)
    for old in [n for n, x in box.items() if ev["ts"] - x["at"] >= PROMISE_COOLDOWN]:
        del box[old]   # the period has passed and the record holds nothing any more (deleted sessions do not pile up)
    box[name] = {"of": data.get("of"), "at": ev["ts"]}
    what = " ".join(str(data.get("what") or "").split())[:200] or PROMISE_DEFAULT
    new_message(st, f"m{ev['seq']}", name, PROMISE_NUDGE.format(what=what), ev["ts"], origin=forms.ORIGIN_CORE,
                **({"root": sess["turn_root"]} if sess.get("turn_root") else {}))


def _bg_model(task):
    """A background helper agent's model as the host named it (the page shows it in the session's row), else nothing."""
    model = task.get("model") if task.get("kind") == "agent" else None
    return {"model": model[:80]} if isinstance(model, str) and model.strip() else {}


def _bg_gone(sess, ids, ts):
    """When background tasks ended (the last BG_GONE): the fold uses them to tell a stale watchdog output (overtaken)."""
    if not ids:
        return
    gone = sess.setdefault("bg_gone", {})
    for tid in ids:
        gone.pop(tid, None)
        gone[tid] = ts
    for tid in list(gone)[:-BG_GONE]:
        del gone[tid]


BG_GONE = 20


def overtaken(data, *changed):
    """A watchdog fact is stale, not rejected: the watchdog decided on the journal up to seen_at (the time of the last
    line it read), and the state it needs changed later, so the core quietly does not apply it, with no core.rejected
    (16.10: 507 such "refusals" in 16 days, races between the watchdog and the session itself). Without seen_at, or if
    the change was not later, it is a real refusal."""
    seen = data.get("seen_at")
    if isinstance(seen, bool) or not isinstance(seen, (int, float)):
        return False
    return any(isinstance(t, (int, float)) and not isinstance(t, bool) and t > seen for t in changed)


def removable(msg):
    """Can the owner remove it from the queue: not an assignment (its result is the reply) and not core service output,
    which come back on their own; a copy of a schedule job can be, the job stays (09.10: "Remove" on a batch of
    reminders was silently rejected)."""
    return not (msg.get("delegation") or msg["text"].startswith(HANDOFF_MARK) or msg["id"].endswith("x7"))


FYI_PLAIN = "--fyi — only an ordinary message: without --now, --for, --about, --card and not a command"
FYI_HEAD = "For your information (came without a turn; no need to answer unless it changes your work):"


def fyi_wakes(sess):
    """Must an "FYI" to this session still wake it: its result waits for the owner (the watchdog's "waits for you",
    attention), so news about it is what it waits for — the owner logged in, gave access, decided. Its own
    `hub waiting` does not count: an "accepted, doing it" to the one who waits keeps that mark (the restart "continue"
    relies on it), and the answer itself is sent without --fyi."""
    return bool(sess.get("attention"))


EXEC_PERMIT = T("core.msg.exec_permit", "{name} waits for permission: {what}{why}. The owner has the card too; to "
                                        "answer yourself: hub allow {name} / hub deny {name}.")


def _tell_raiser_permit(st, ev, sess, data):
    """An executor asks permission for a tool (not a form): whoever raised it hears once, in an "FYI" that wakes no turn
    (before, the raiser found out only by polling `hub status`). Once per request: the same request again is not told."""
    up = st["sessions"].get(sess.get("raised_by") or "") or {}
    rid = data.get("request_id")
    if sess.get("role") != "executor" or up.get("state") in (None, "removed", "closed", "failed") \
            or up["name"] == sess["name"] or data.get("interactive") or data.get("tool") == "AskUserQuestion" \
            or (rid is not None and any(r.get("request_id") == rid for r in sess["pending"])):
        return None
    inp = data.get("input") or {}
    if isinstance(inp, dict) and isinstance(inp.get("command"), str):
        what = inp["command"]
    elif isinstance(inp, dict):   # the fields, each cut short: a Write carries a whole file
        what = json.dumps({str(k)[:40]: redact.mask_head(v, 100) if isinstance(v, str) else
                           v if isinstance(v, (bool, int, float)) else "…" for k, v in list(inp.items())[:8]},
                          ensure_ascii=False)
    else:
        what = ""
    what = redact.mask_head(f"{data.get('tool')} {what}".strip(), 400)
    why = f" ({redact.mask_head(str(data['why']), 120)})" if data.get("why") else ""
    return new_message(st, f"m{ev['seq']}x8", up["name"], EXEC_PERMIT.format(name=sess["name"], what=what, why=why),
                       ev["ts"], origin=forms.ORIGIN_CORE, fyi=True)["id"]   # the id new_message gave (it renames on a clash)


def _withdraw_permit_note(st, req):
    """The request is answered (or gone with the process): its note, if it still lies aside, undelivered, would say
    "waits" of what no longer waits — it is taken back. One already delivered stays."""
    m = st["messages"].get(req.get("told") or "")
    if m and m["state"] == "aside" and not m.get("with"):
        del st["messages"][m["id"]]


TOOL_DENIED = T("core.msg.tool_denied",
                "{who} refused {tool}{reason}. Do not wait for it: go on without it or as the reason says.")


def _denied_text(who, data):
    """A tool refusal told to the session in words (a message to a live one, an "FYI" to one not running)."""
    return TOOL_DENIED.format(who=who, tool=data.get("tool"), reason=f": {data['reason']}" if data.get("reason") else "")


def _ride(st, msg):
    """The same session's "FYI" messages (`hub send --fyi`, the core's service messages to the hub) ride with this block at the end of the input: no
    turn was spent on them (friction digest 16.10: 143 turns on statuses with no reaction). Delivered when this one
    arrives (_pump); if it does not, they ride with the next. A command does not carry them: the end of its input is
    its arguments."""
    msg.pop("fyi_note", None)
    for m in st["messages"].values():
        if m.get("with") == msg["id"]:
            m.pop("with")
    if msg.get("command"):
        return
    riders = sorted((m for m in st["messages"].values() if m["state"] == "aside" and m["session"] == msg["session"]
                     and not m.get("with")), key=lambda m: (m["created"], m["id"]))
    if not riders:
        return
    for m in riders:
        m["with"] = msg["id"]
    msg["fyi_note"] = "\n\n" + FYI_HEAD + "".join(
        f"\n— {m['id']}{m.get('origin') or ''}: {m['text']}"
        + (f" (the core's service note: {m['routing_note']})" if m.get("routing_note") else "")
        + attachments_note(m.get("attachments")) for m in riders)


INJECT_BY_WATCH = T("core.reducer.inject_by_watch", "an insertion mid-turn is decided by the watchdog, not by a session")
INJECT_BY_OUTSIDER = T("core.reducer.inject_by_outsider",
                       "an insertion mid-turn is decided by the watchdog or the owner, not by a module or a plugin: the "
                       "message waits in the queue")
WEIGHED_BY_OUTSIDER = T("core.reducer.weighed_by_outsider",
                        "the urgency of a message is estimated by the watchdog, not by a module or a plugin")
RUSH_RIGHT = "message.rush"   # the Module API right of a module that sends urgent messages (core/daemon checks it)
RUSH_BY_HUB = T("core.reducer.rush_by_hub",
                "urgent, bypassing the queue (--now), is decided by the hub: send without --now or ask the hub (hub send hub "
                "\"…\")")
CANCEL_BY_OWNER = T("core.reducer.cancel_by_owner", "only the owner removes from the queue")
CANCEL_RIDING = T("core.reducer.cancel_riding", "already being sent to the session together with {id}")
CANCEL_NOT_REMOVABLE = T("core.reducer.cancel_not_removable",
                         "a task, a handover — cannot be removed this way, it would come back by itself")


def _inject(st, ev, data, actions):
    """The watchdog (Haiku) decided that this message to a busy session is needed now, not after the turn (01.10), or the
    owner pushed it through himself (the page, a module handle on his request)."""
    if data.get("channel") == "session":
        raise Rejected(INJECT_BY_WATCH)
    msg = st["messages"].get(ev.get("entity"))
    why = inject_refused(st, msg)
    if not by_watch_or_owner(data):   # its own seen_at does not make it a stale watchdog fact
        raise Rejected(why or INJECT_BY_OUTSIDER)
    if why:
        sess = st["sessions"].get((msg or {}).get("session") or "") or {}
        if msg and overtaken(data, {NOT_QUEUED: msg.get("since"), NOT_IN_TURN: sess.get("since")}.get(why)):
            return   # the message was already delivered or the turn ended while the watchdog weighed urgency
        raise Rejected(why)
    _msg_move(msg, "_inject", ev["ts"])
    msg["injected"], msg["delivered_at"] = True, ev["ts"]
    _ride(st, msg)
    _unread(st, msg)
    _typed(msg, ev["ts"], actions, note="(came in the middle of your turn — take it into account now) ")


def by_watch_or_owner(data):
    """message.inject and message.weighed are the watchdog's (channel cli, as the owner's terminal), the owner's (the page,
    a module handle on his request) or the core's. A module or a plugin process (channel plugin) is none of them: an
    insertion of its own would push its message mid-turn past the queue, where urgent is the hub's or the owner's word
    (`--now`, `hub route … now`)."""
    return tools.from_owner(data) or data.get("channel") in (None, "core")


def _unread(st, msg):
    """An insertion mid-turn is "delivered" as soon as it is typed, but Claude Code reads it only between steps: until a
    turn with its mark starts it is unread (the session's inserted_unread). The snapshot says so on the message
    ("inserted, not read yet", views); a stop of an executor withdraws such lines (the session.stop_requested branch);
    the end of the turn and a new process (session.started, session.exited) clear the list, so it does not grow."""
    sess = st["sessions"].get(msg["session"])
    if sess is not None:
        # and the "FYI" lines that rode in the same input (_ride): delivered with it, unread with it
        riders = [m["id"] for m in st["messages"].values() if m.get("with") == msg["id"]]
        sess.setdefault("inserted_unread", []).extend([msg["id"], *riders])


def _may_rush(st, data, module=False):
    """Urgent, bypassing the queue (`--now`), is decided by the hub or the owner (owner decision 2026-10-09: the hub
    can judge urgency itself), and, for a message only (module=True, from _received), by a core module with the
    message.rush right (Module API 2.10: the daemon checks the right before the fact is written; the core names the
    sender mod:<name>, spool.outsider). An urgent task from a module is refused like any other session's."""
    who = st["sessions"].get(data.get("sender") or "") or {}
    mod = module and data.get("channel") == "plugin" and str(data.get("sender") or "").startswith("mod:")
    if data.get("now") and not (tools.from_owner(data) or mod
                                or (data.get("channel") == "session" and who.get("kind") == "hub")):
        raise Rejected(RUSH_BY_HUB)


def _rush(st, msg, ts, actions):
    """An urgent message from the hub or the owner (owner decision 2026-10-09: send it at once as urgent, do not just
    queue it): to a busy session, mid-turn now, without the watchdog's estimate; otherwise first in the queue once it frees up."""
    if msg["state"] != "queued" or msg.get("handover"):
        return False
    msg["urgent"] = True
    sess = st["sessions"].get(msg["session"]) or {}
    if msg.get("held") or sess.get("state") != "busy" or not injectable(msg):
        return False
    _msg_move(msg, "_inject", ts)
    msg["injected"], msg["delivered_at"] = True, ts
    _ride(st, msg)
    _unread(st, msg)
    did = msg.get("delegation")
    if did:   # this turn's result is also its reply: Claude Code does not always report an insertion as a marked turn (journal 09.10)
        if not sess.get("delegation"):
            sess["delegation"] = did
        elif sess["delegation"] != did and did not in sess.get("also_tasks", []):
            sess.setdefault("also_tasks", []).append(did)
    _typed(msg, ts, actions, note=(f"(urgent task {did} — came in the middle of your turn: take it up now; the result of "
                                   "this turn becomes its answer too) " if did
                                   else "(urgent — came in the middle of your turn: take it into account now) "))
    return True


def _cancel(st, ev, data):
    """The owner removes a message from the queue himself, before the turn (the page, the "Delete" button on "queued")."""
    if data.get("channel") == "session":
        raise Rejected(CANCEL_BY_OWNER)
    msg = st["messages"].get(ev.get("entity"))
    if not msg or msg["state"] not in ("queued", "aside"):
        raise Rejected(NOT_QUEUED)
    carrier = (st["messages"].get(msg.get("with") or "") or {})
    if carrier.get("state") == "typing":
        raise Rejected(CANCEL_RIDING.format(id=carrier["id"]))
    if not removable(msg):
        raise Rejected(CANCEL_NOT_REMOVABLE)
    _msg_move(msg, "_cancel", ev["ts"])
    msg["reason"] = "removed by owner"


WAKE_WHO = T("core.reducer.wake_who",
             "{name} is asleep after handing in its task: it is woken by the owner, the hub or whoever raised it{direction} "
             "— ask the hub: hub send hub \"…\"")
WAKE_DIRECTION = T("core.reducer.wake_direction", " (a direction — only the hub or the owner)")
WAKE_MINI_ABOUT = T("core.reducer.wake_mini_about",
                    "{name}: a mini-hub wakes an executor to rework its own task — --about dN is needed; ")
WAKE_REWORK = T("core.reducer.wake_rework",
                "{name} can be woken to rework its task: hub session wake {name} --about <{ids}> \"<what>\"; a new job — "
                "a new executor (hub task run)")
WAKE_QUEUED = T("core.reducer.wake_queued",
                "{name} is already in the queue, No. {n}, for {kind} — wait or cancel: hub task cancel {name}, then "
                "hub session wake {name} … again")
KIND_REWORK = T("core.reducer.kind_rework", "a rework")   # the kinds of a wake (minihub.wake_kind) in these sentences
KIND_WAKE = T("core.reducer.kind_wake", "a wake-up")
KIND_REWORK_CAP = T("core.msg.kind_rework_cap", "The rework")
KIND_WAKE_CAP = T("core.msg.kind_wake_cap", "The wake-up")
WAKE_GONE = T("core.msg.wake_gone",
              "{what} of {name} cannot be handed over — it is gone: {task}. If needed — a new executor (hub task run).")
WAKE_NOT_HANDED = T("core.msg.wake_not_handed", "{what} of {name} cannot be handed over — {why}: {task}")
WAKE_CONTINUES = T("core.reducer.wake_continues", "; {up} continues: hub send {up} \"…\"")
COPY_TAKEN = T("core.reducer.copy_taken",
               "its copy {top} is taken by {who}; wait until {who} hands in, or a new job — to a new executor")
WAKE_REFUSED = T("core.reducer.wake_refused", "{name} cannot be woken — {why}")


def _wake_who(st, sess, data):
    """Who wakes an executor that handed in: "direct" - the owner or the hub (they decide for themselves, no ceilings),
    "raiser" - the session that raised it (it is counted: its wake goes through the ceilings and the queue of a new
    request, _raiser_wakes); anyone else is refused. The raiser's gate is the same as for stop and remove
    (guards.from_hub_owner_or_raiser): a direction (an executor with a backlog) is woken only by the hub or the owner."""
    hub = st["sessions"].get(data.get("sender") or "") or {}
    if tools.from_owner(data) or hub.get("kind") == "hub":
        return "direct"
    if data.get("channel") == "session" and guards.from_hub_owner_or_raiser(
            st, {**data, **_target(st, sess)}) is None:
        return "raiser"
    raise Rejected(WAKE_WHO.format(name=sess["name"], direction=WAKE_DIRECTION if sess.get("backlog") else ""))


def _raiser_wakes(st, ev, data, sleeper):
    """The session that raised a sleeping executor wakes it with a job, through the same ceilings and budget as raising a
    new one: the owner and the hub decide for themselves, the raiser is counted, and a wake that skipped them would be a
    way round the ceilings. A mini-hub - only for its own task (`--about dN`): a new task means a new executor, one
    session, one task; another agent may name the task or not. The task (dN) is what to reopen, "" - nothing to reopen.
    If there is no room it is not a refusal but the core's queue (the "parallelism" spec, case 3): a request with wake,
    like a new hub task run; it is raised by itself (_wake_ready), and then the result is None."""
    name, me = sleeper["name"], data["sender"]
    about, mini, live, wait = _raiser_terms(st, ev, data, sleeper)
    if wait:
        d = st["delegations"].get(about) or {}
        runs = st.setdefault("runs", {})
        runs[name] = {"by": me, "task": str(data.get("text") or ""), "ts": ev["ts"], "state": "queued",
                      **forms.why_fields(wait),
                      **({"minihub": True} if mini else {}), "root": chain.turn_root(st, me),
                      **({"live": True} if live else {}),
                      # the mini-hub task it reworks for waits for the request too (core/minihub.waiting_on)
                      **({"parent": d["parent"]} if mini and d.get("parent") else {}),
                      "wake": {"about": about, "text": str(data.get("text") or ""),
                               "attachments": list(data.get("attachments") or [])}}
        return None
    return about


REVIVE_OTHER = T("core.reducer.revive_other",
                 "{name} can be brought back only as it was: the same conversation, folder and working copy")
REVIVE_WAITS = T("core.reducer.revive_waits",
                 "{name} cannot be brought back now: {why}. Repeat the command when there is a place")


WORKTREE_KEYS = ("path", "branch", "repo", "keep_branch", "from_main", "base", "base_sha", "source_cwd")   # what a revive carries


# what a raiser's revive request keeps (_raiser_revives): the same conversation and folder, the caller and the copy
REVIVE_FIELDS = ("session_id", "cwd", "revive", "channel", "owner", "sender", "req", "worktree")


def revive_worktree(old):
    """The working copy a revive of this removed session restores, as the request carries it (hub session revive), or None."""
    wt = old.get("worktree") or {}
    return {k: wt[k] for k in WORKTREE_KEYS if k in wt} if wt.get("path") else None


def revive_wait(st, old, sender, now):
    """Why the raiser cannot bring its executor back now (the places of a new request: without them a revive would be a way
    round the ceilings, as for a wake with a job), as a phrase, or None. The command asks it before it restores the copy."""
    wait = executors.waits(st, sender, now=now)
    return REVIVE_WAITS.format(name=old["name"], why=wait) if wait else None


def _raiser_revives(st, ev, data, old):
    """Whoever raised an executor brings it back (hub session revive, guards.revives): the same conversation, folder and
    working copy the record has and nothing else - the request's fields are the caller's words, so only these are taken
    from it (kind, mode, profile, role... come from the record, as for the hub's revive) - and the places of a new
    request. The hub and the owner decide for themselves, they are not checked here. Returns the request's data."""
    got = data.get("worktree")
    if data.get("session_id") != old.get("session_id") or data.get("cwd") != old.get("cwd") or (
            got is not None and got != revive_worktree(old)):
        raise Rejected(REVIVE_OTHER.format(name=old["name"]))
    wait = revive_wait(st, old, data["sender"], ev["ts"])
    if wait:
        raise Rejected(wait)
    return {k: data[k] for k in REVIVE_FIELDS if k in data}


def _raiser_terms(st, ev, data, sleeper):
    """What a raiser's wake meets, one place for a wake with a job (_raiser_wakes) and for `session.wake_requested`:
    the refusals (a mini-hub needs --about dN; --about is the executor's own handed-in task; already in the queue), then
    whether the ceilings and the budget leave room -> (about, mini, live, wait), wait is the reason or None."""
    name, me = sleeper["name"], data["sender"]
    mini = minihub.on(st, st["sessions"].get(me))
    about = str(data.get("about") or "")
    mine = [x["id"] for x in delegations.done_for(st, name, me)]
    if (mini or about) and about not in mine:
        raise Rejected((WAKE_MINI_ABOUT.format(name=name) if mini and not about else "")
                       + WAKE_REWORK.format(name=name, ids="|".join(mine) or "dN"))
    queued = (st.get("runs") or {}).get(name)
    if queued:
        raise Rejected(WAKE_QUEUED.format(name=name, n=executors.position(st, name),
                                          kind=KIND_REWORK if (queued.get("wake") or {}).get("about") else KIND_WAKE))
    d = st["delegations"].get(about) or {}
    # the budget is the limit economy mode (core/budget.py; those waiting on it go in one card to the owner, not as a line
    # to the hub): a rework on the owner's live word, or the raiser's turn on it, goes at once
    live = bool(budget.mode(st) and (_live(st, me) or chain.task_root(d) and not d.get("via_schedule")))
    return about, mini, live, executors.waits(st, me, now=ev["ts"], live=live)


def _live(st, name):
    """An executor request from the turn of name is on the owner's live word or urgent (limit economy, core/budget.py): a
    turn in the chain of an owner message, not through a schedule firing, or a turn on an urgent message (--now)."""
    sess = st["sessions"].get(name or "") or {}
    if sess.get("state") not in chain.BUSY:
        return False
    if chain.turn_root(st, name) and not chain.turn_scheduled(st, name):
        return True
    d = st["delegations"].get(chain.turn_task(st, name) or "") or {}
    return any((st["messages"].get(m or "") or {}).get("urgent") for m in (sess.get("turn_marker"), d.get("message")))


def _wake_ready(st, ev, name, actions):
    """A rework from the queue (case 3) got its room: the text goes to the executor exactly once, like a wake by a
    mini-hub (the task is in progress again); no copy or daemon launch is needed since the session is the same. If the
    executor is gone, the mini-hub gets a line about its task; if it woke by itself (woken by the owner or the hub), the
    text goes to it as an ordinary message on that task."""
    run = st["runs"].pop(name)
    sleeper, by = st["sessions"].get(name), run.get("by")
    if not sleeper or sleeper["state"] in ("removed", "closed", "failed"):
        if by in st["sessions"] and st["sessions"][by]["state"] not in ("removed", "closed", "failed"):
            what = KIND_REWORK_CAP if (run.get("wake") or {}).get("about") else KIND_WAKE_CAP
            new_message(st, f"m{ev['seq']}", by, WAKE_GONE.format(what=what, name=name, task=run["task"][:200]), ev["ts"],
                        origin=forms.ORIGIN_CORE, **_for_parent(st, run))
        return
    w = run["wake"]
    if w.get("cut"):   # a retired executor cut by the limit (_wake_cut): the core's continuation, not the raiser's message
        # the words by the pool as it is now: it may have moved to another account while this waited (the text kept with
        # the run is what the raiser's queue shows)
        text = _cut_text(st, sleeper, w["cut"])
        return _core_wake(st, ev, sleeper, w["about"], w["cut"], text) \
            if executors.dormant(sleeper) else None   # woken meanwhile: its turn is its own
    if executors.dormant(sleeper):
        # the gate was passed when it asked, but it waited: meanwhile the raiser stopped having executors, or put another
        # into the sleeper's copy (`hub task run … --here`), and two in one copy would interfere
        why = _wake_refusal(st, sleeper, direct=False)
        if why:
            if by in st["sessions"] and st["sessions"][by]["state"] not in ("removed", "closed", "failed"):
                new_message(st, f"m{ev['seq']}", by, WAKE_NOT_HANDED.format(
                    what=KIND_REWORK_CAP if w["about"] else KIND_WAKE_CAP, name=name, why=why, task=run["task"][:200]),
                    ev["ts"], origin=forms.ORIGIN_CORE, **_for_parent(st, run))
            return
    data = {"to": name, "text": w["text"], "wake": True, "about": w["about"], "channel": "session", "owner": False,
            "sender": by, **({"attachments": w["attachments"]} if w.get("attachments") else {})}
    _received(st, ev, data, actions, queued=w["about"] or True)


def _wake_refusal(st, sleeper, direct):
    """Why a sleeping executor is not woken now, as a phrase (not a sentence: no colon in it), or None. One source for the
    gate (_wake_gate: the refusal to whoever asks) and for a wake leaving the queue (_wake_ready: the note to the raiser).
    Whoever raised it has no executors (parallelism 0 or "solo"): nobody wakes it, not even the owner or the hub; whoever
    raised it continues by itself, and the owner and the hub (direct) are told so. Its copy (its own or a ready one,
    --here) is taken by another: two in one copy would interfere."""
    up = sleeper.get("raised_by") or ""
    why = executors.refused(st, up)
    if why:
        return why + (WAKE_CONTINUES.format(up=up) if direct else "")
    top = executors.copy_of(sleeper)
    who = executors.holder(st, top, sleeper["name"]) if top else None
    if who:
        return COPY_TAKEN.format(top=top, who=who)
    return None


def _wake_gate(st, sleeper, direct):
    """The refusal of a wake (_wake_refusal) as the answer to whoever asks; direct - the owner or the hub, not the raiser."""
    why = _wake_refusal(st, sleeper, direct)
    if why:
        raise Rejected(WAKE_REFUSED.format(name=sleeper["name"], why=why))


def _for_parent(st, run):
    """A message to a mini-hub about its executor request (cancelled, failed to start) is attached to the task it asked for:
    a turn on this message is that task's turn, and if no executors remain on it, its result is the reply to it (core/minihub.py)."""
    if run.get("parent") and run.get("minihub"):
        return {"for_delegation": run["parent"]}
    return {}


FREED_GOT_IT = T("core.msg.freed_got_it", "{name} got free and received it itself")
NO_DECISION = T("core.msg.no_decision", "no decision needed: {why}")


def _release(st, msg, ts, why):
    """The message no longer awaits the hub's decision; its hint to the hub, if not yet read, is withdrawn. There are no
    new ones (owner decision 2026-10-16: a message to a busy specialist goes to its queue without a hub turn; the hub
    answered "-" 15 of 15 times); only those recorded by a previous core persist."""
    note = st["messages"].get(msg.pop("held", None) or "")
    if note and note["state"] == "queued":
        _msg_move(note, "_cancel", ts)
        note["reason"] = str(NO_DECISION.format(why=why))


ROUTE_WHO = T("core.reducer.route_who", "decided by the hub or the owner")
ROUTE_HOW = T("core.reducer.route_how", "how: queue | executor | drop | now")
ROUTE_DONE = T("core.reducer.route_done", "{ref} no longer waits for a decision: delivered, handed to an executor or removed")
ROUTE_NOT_NOW = T("core.reducer.route_not_now",
                  "{ref} cannot be inserted mid-turn (a handover, a schedule job, a command) — it will arrive with a new turn")
WHO_SENDER_OWNER = T("core.reducer.who_owner", "the owner")   # who sent a message with no sender: the owner
ROUTE_NOT_TASK = T("core.reducer.route_not_task",
                   "an executor is given a task (dN), and {ref} is a message: ask {sender} to set a task (hub task new "
                   "{name} \"…\"), answer it yourself or to the queue — hub route {ref} queue")
ROUTE_NOT_AGENT = T("core.reducer.route_not_agent",
                    "{name} is not a specialist: an executor for a task — hub task run <name> \"…\" --work normal|risky")
ROUTE_TO_QUEUE = T("core.reducer.route_to_queue", "{why} — to the queue of {name}: hub route {ref} queue")
ROUTE_QUEUED = T("core.reducer.route_queued", "{ref} does not wait for the hub's decision: it is already in the queue of {name}")
ROUTE_NOT_SESSION = T("core.reducer.route_not_session",
                      "{ref} is not from a session: only a message or a task of another session can be not passed on")
ROUTE_WHY = T("core.reducer.route_why", "the reason goes to {sender}: hub route {ref} drop \"<why>\"")
HUB_DROPPED = T("core.msg.hub_dropped", "the hub did not pass it on: {why}")
TRAIL_NOT_PASSED = T("core.msg.trail_not_passed", "did not pass it on to {name}: {why}")
HUB_NOT_PASSED = T("core.msg.hub_not_passed", "The hub did not pass your {ref} on to {name} («{body}»): {why}")


def _route(st, ev, data, ctx, actions):
    """The hub (or the owner) decides about a message in a busy specialist's queue: `hub route <mN|dN> queue|executor|drop|now`.
    The message itself waits in the queue (the hub need not take a turn for it); executor also works for the hub's own task that it set."""
    who = st["sessions"].get(data.get("sender") or "") or {}
    if not (tools.from_owner(data) or who.get("kind") == "hub"):
        raise Rejected(ROUTE_WHO)
    ref, how = str(ev.get("entity") or ""), data.get("route")
    d = st["delegations"].get(ref)
    msg = st["messages"].get(d["message"] if d else ref)
    d = d or st["delegations"].get((msg or {}).get("delegation") or "")
    if how not in ("queue", "executor", "drop", "now"):
        raise Rejected(ROUTE_HOW)
    if not msg or msg["state"] != "queued" or msg.get("handover"):
        raise Rejected(ROUTE_DONE.format(ref=ref))
    name = msg["session"]
    if how == "now":   # urgent: now mid-turn, first in line for a free one (09.10)
        if not injectable({**msg, "urgent": True}):
            raise Rejected(ROUTE_NOT_NOW.format(ref=ref))
        msg.pop("held", None)
        _rush(st, msg, ev["ts"], actions)
        return
    sender = msg.get("sender") or (d or {}).get("by") or WHO_SENDER_OWNER
    if how == "executor":
        agent = st["sessions"].get(name) or {}
        if not d or d["state"] != "working" or d["session"] != name:
            raise Rejected(ROUTE_NOT_TASK.format(ref=ref, sender=sender, name=name))
        if agent.get("role") != "agent" or agent.get("kind") == "hub":
            raise Rejected(ROUTE_NOT_AGENT.format(name=name))
        why = executors.refused(st, name)
        if why:
            raise Rejected(ROUTE_TO_QUEUE.format(why=why, name=name, ref=ref))
        # the name by substance comes from the hub (owner decision 2026-10-05: it named sessions badly, like "by
        # analogy"); otherwise from the text
        pin = {**{k: d[k] for k in ("model", "effort") if d.get(k)}, **delegations.pinned(data, catalog.codex_state(st))}   # the hub's word outranks the task's
        # the kind of work and the economy pair too: the route's own (hub route ... --work), otherwise the task's (hub task new --work)
        own = any(data.get(k) for k in ("work", "model", "effort"))
        work = data.get("work") if own else d.get("work")
        eco = (delegations.pinned(data["economy"], catalog.codex_state(st)) if isinstance(data.get("economy"), dict) else {}) if own \
            else d.get("economy") or {}
        run = executors.named(st, name, data.get("name")) or executors.auto_name(st, name, d.get("context") or msg["text"])
        # limit economy (core/budget.py): a task on the owner's live word or an urgent one goes at once
        live = bool(budget.mode(st) and (chain.root(st, msg["id"]) and not chain.scheduled(st, msg["id"])
                                         or msg.get("urgent")))
        wait = executors.waits(st, name, now=ev["ts"], live=live)
        msg.pop("held", None)
        msg["handover"] = run   # no longer deliver to the specialist: the executor takes it
        st.setdefault("runs", {})[run] = {"by": name, "task": d.get("context") or msg["text"], "message": msg["id"],
                                          "repo": None, "ts": ev["ts"], "state": "queued" if wait else "ready",
                                          **forms.why_fields(wait), "auto": True, **pin,
                                          **({"work": work} if work in ("normal", "risky") else {}),
                                          **({"economy": dict(eco)} if eco else {}),
                                          **({"live": True} if live else {})}
        if not wait:
            budget.economize(st, st["runs"][run])
        return
    if how == "queue":
        if not msg.pop("held", None):
            raise Rejected(ROUTE_QUEUED.format(ref=ref, name=name))
        return
    # without a previous core's mark, only another session's message: the owner's words and tasks (and those relayed
    # by the hub, --for) cannot be removed
    if not msg.get("held") and (msg.get("from_owner") or (d or {}).get("for") or sender not in st["sessions"]
                                or st["sessions"][sender].get("kind") == "hub"):
        raise Rejected(ROUTE_NOT_SESSION.format(ref=ref))
    why = " ".join(str(data.get("why") or "").split())
    if not why:
        raise Rejected(ROUTE_WHY.format(sender=sender, ref=ref))
    msg.pop("held", None)
    _msg_move(msg, "_cancel", ev["ts"])
    msg["reason"] = str(HUB_DROPPED.format(why=why))
    if d:
        delegations.move(st, d, "delegation.done", ev, ctx)
        delegations.trail(st, d["id"], delegations.BY_HUB, TRAIL_NOT_PASSED.format(name=name, why=why))
    if sender in st["sessions"]:
        body = str(d.get("context") if d else msg["text"])
        new_message(st, f"m{ev['seq']}", sender, HUB_NOT_PASSED.format(name=name, ref=ref, body=delegations.short_text(body, 200),
                                                                      why=why), ev["ts"], origin=forms.ORIGIN_CORE)


def _term(msg):
    """How long a typed line waits for its turn: the reply to a built-in command is its result, and /compact of a big
    conversation takes minutes."""
    return COMMAND_CONFIRM_SECONDS if msg.get("command") else CONFIRM_SECONDS


def _typed(msg, ts, actions, note=""):
    msg["attempt"] += 1
    msg["deadline"] = ts + _term(msg)
    files = clean_attachments(msg.get("attachments"))
    mark = MARKER.format(id=msg["id"], origin=msg.get("origin", ""))
    # a mark at the start of a line is only our own: another's in words and addenda is quoted (forms.quote_labels)
    if msg.get("builtin"):
        text = msg["text"]   # a built-in command has no hook, and its arguments are its own
    elif msg.get("command"):   # a Claude Code command works only if the input starts with it; the hook finds the mark at the end too
        text = forms.quote_labels(msg["text"]) + "\n\n" + (mark + note).rstrip()
    else:
        text = mark + note + forms.quote_labels(msg["text"])
    tail = (("\n\nThe core's service note:\n" + msg["routing_note"] if msg.get("routing_note") else "")
            + attachments_note(files) + failed_note(msg.get("attach_failed"))
            + (msg.get("cards") or "") + (msg.get("news") or "")
            + (msg.get("reacts") or "") + (msg.get("fyi_note") or "")
            + ("\n\n" + (ASK_IN_THREAD if msg.get("comment") else ASK_BY_FORM)
               if msg.get("from_owner") and not msg.get("command") else ""))
    actions.append({"do": "type", "session": msg["session"], "message": msg["id"],
                    "attempt": msg["attempt"], "attachments": files, "text": text + forms.quote_labels(tail)})


def _messages_of(st, name):
    return [m for m in st["messages"].values() if m["session"] == name]


def _msg_move(msg, kind, ts):
    moved = step(MESSAGE, msg["state"], kind)
    if moved is None:
        raise AssertionError(f"message {msg['id']}: {kind} in {msg['state']}")
    if msg["state"] != moved[0]:
        msg["since"] = ts
    msg["state"] = moved[0]
    if msg["state"] != "typing":
        msg["deadline"] = None


def _awaited(state):
    """Names of sessions a message is on its way to (queued or being typed, not a handover): a network cut's timer waits
    while it does, as the limit's clock does (limitresume.due)."""
    return {m["session"] for m in state.get("messages", {}).values() if limitresume._ends_cut(m)}


def pending_timers(state, now=None):
    """(deadline, id, attempt): messages being typed, assignment deadlines, reminders, handovers, the terms of
    schedule jobs; hubd writes timer.fired by them. now - the clock: question cards stale by it do not hold a task."""
    return sorted([(m["deadline"], m["id"], m["attempt"]) for m in state["messages"].values()
                   if m["state"] == "typing" and m["deadline"] is not None]
                  + (delegations.pending(state, now) if "delegations" in state else [])
                  + (owner.pending(state) if "owner" in state else [])
                  + (handoff_timers(state) if "sessions" in state else [])
                  + (schedule.pending(state) if "schedule" in state else [])
                  + [(s["net_cut"]["due"], NET_TIMER + n, 0) for n, s in state.get("sessions", {}).items()
                     if s.get("net_cut") and n not in _awaited(state)],
                  key=lambda x: x[0])
