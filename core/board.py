"""The direction board follows the facts (dir-sync spec, owner decision 2026-10-26: the core keeps the facts, "Done" is the lead's).

The board is the direction's backlog files (modules/directions); the model writes them. The core knows something else
about the work: who was raised, who handed in. Here: the "delegation <-> card" link and the facts layer over it: who works
on a card, whether it was handed in and whether the lead has sorted it out. The core never sets Done: "merged" is an
external fact, the lead decides it.

An executor is linked to a card by three sources (spec K1):
  flag  - `hub task run <name> "..." --board TASK-N` (a direction session only; the card exists on the board);
  brief - the single TASK-N token in the brief's first line (the card exists) - the same delegation field as flag;
  edit  - the executor edited a card (`hub direction <slug> task create|edit|archive`, the direction.card fact), if per
          session it edited no more than three cards (more - a reconciler, not an executor of the subject).

State: the delegation and its executor (the session record) - the board field {slug, card, how}; edits - st["boards"][slug]
(the key appears with the first direction.card fact: journals without them fold as before):
  cards[TASK-N]   - {ts, seq, by, owner?: the last edit (by - a session; owner - the owner; seq - its place in the journal);
                    judged: when the lead or the owner changed the status or notes}
  touched[session] - [session_id, the cards the executor edited] (no more than EDIT_LINK_MAX + 1: beyond that it is
                    already a reconciler; a new session with the same name starts from a clean slate)
"""
import re

from core import askexpired, chain, owner
from core.coretext import T

CARD = re.compile(r"\bTASK-(\d+)\b")
EDIT_LINK_MAX = 3
# what clears "handed in - not sorted out" when the lead or the owner changes it (a title, label, priority - no)
JUDGING = ("status", "notes", "append-notes", "archive")
FIELDS = ("title", "description", "status", "ac", "check-ac", "uncheck-ac", "remove-ac", "notes", "append-notes", "plan",
          "priority", "labels", "add-label", "create", "archive")
UNKNOWN_SLACK = 5   # s: a card file is written before the edit fact - younger than the last fact by this much is not a "foreign edit"
UNLINKED_WINDOW = 24 * 3600
EDITING_FRESH = 15 * 60   # s: an edit in the running turn counts as "works on it" this long (the lead edits cards every few minutes)


def card_id(ref):
    """"TASK-9", "task-9", "9" -> "TASK-9"; not a card number - None."""
    m = re.fullmatch(r"(?:task-)?(\d+)", str(ref or "").strip(), re.I)
    return f"TASK-{int(m.group(1))}" if m else None


TASKS_MAX = 40         # cards the owner can name in one message (a column of a big board is 26)
TASK_TITLE_MAX = 120
TASK_STATUS_MAX = 40


def _line(text, n):
    """One line without brackets: a mark of a message is read up to "]" and goes on one line."""
    line = " ".join(str(text or "").split()).replace("]", ")").replace("[", "(")
    return line[:n] + ("…" if len(line) > n else "")


def clean_tasks(raw):
    """The cards the owner named in a message ([{id, title, status}] from the page): only "TASK-N" ids, no repeats, a
    title and a status cut to one line, at most TASKS_MAX. Not a list or nothing usable - [] (the message goes as it is)."""
    if not isinstance(raw, list):
        return []
    out, seen = [], set()
    for x in raw:
        m = re.fullmatch(r"task-(\d{1,9})", str(x.get("id") if isinstance(x, dict) else "").strip(), re.I)
        if not m or int(m.group(1)) in seen:
            continue
        seen.add(int(m.group(1)))
        out.append({"id": f"TASK-{int(m.group(1))}", "title": _line(x.get("title"), TASK_TITLE_MAX),
                    "status": _line(x.get("status"), TASK_STATUS_MAX)})
        if len(out) == TASKS_MAX:
            break
    return out


LABEL_TITLE_MAX = 70   # a title in the label is shorter than on the message: the label goes with every turn of the message


def tasks_label(tasks):
    """The line for the session's label: the cards as the owner saw them on the board."""
    short = lambda s: s if len(s) <= LABEL_TITLE_MAX else s[:LABEL_TITLE_MAX - 1] + "…"  # noqa: E731
    return "about the board's tasks (as the owner sees them on the page): " + "; ".join(
        x["id"] + (f" «{short(x['title'])}»" if x["title"] else "") + (f" ({x['status']})" if x["status"] else "")
        for x in tasks)


def brief_card(text):
    """The card from the brief's first line: the single TASK-N token (the lead writes "Task TASK-41 of the direction ...").
    Two different ones or none - None: the link is not guessed."""
    first = str(text or "").strip().splitlines()[:1]
    found = {f"TASK-{int(n)}" for n in CARD.findall(first[0])} if first else set()
    return found.pop() if len(found) == 1 else None


def lead_slug(st, name):
    """The slug of the direction that session name leads, or None."""
    return (st["sessions"].get(name or "") or {}).get("backlog") or None


def direction_of(st, sess):
    """A session's direction slug: its own (a lead) or that of whoever raised it (a direction executor)."""
    if not sess:
        return None
    if sess.get("backlog"):
        return sess["backlog"]
    if sess.get("role") == "executor":
        return lead_slug(st, sess.get("raised_by"))
    return None


# --- the direction.card fact (reducer) --------------------------------------------------------------------------------

def card_edited(st, ev, data, owner):
    """The direction.card fact {slug, card, fields}: who edited a card and when. owner - the owner's word (his channels).
    Not an edit - a refusal with a reason (reducer.Rejected is raised by the caller: here - None or the reason text)."""
    slug, card = str(data.get("slug") or ""), card_id(data.get("card"))
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", slug) or not card:
        return T("core.board.card_needs", "direction.card: needs the direction's slug and card TASK-N")
    fields = [f for f in data.get("fields") or [] if f in FIELDS]
    sess = st["sessions"].get(data.get("sender") or "") if data.get("channel") == "session" else None
    who = None if owner else (sess or {}).get("name") or str(data.get("sender") or "") or None
    if not owner and not who:
        return T("core.board.card_who", "direction.card: unknown who edited it")
    board = st.setdefault("boards", {}).setdefault(slug, {})
    rec = board.setdefault("cards", {}).setdefault(card, {})
    rec["ts"], rec["seq"], rec["by"] = ev["ts"], ev["seq"], who   # by None and owner - the owner edited it
    if owner:
        rec["owner"] = True
    else:
        rec.pop("owner", None)
    lead = bool(sess and sess.get("backlog") == slug)
    if (owner or lead) and any(f in JUDGING for f in fields):
        rec["judged"] = ev["ts"]
    if sess and sess.get("role") == "executor" and not sess.get("backlog"):
        rec = board.setdefault("touched", {}).get(sess["name"])
        if not rec or rec[0] != sess.get("session_id"):   # the same name for a new session - a clean slate
            rec = board["touched"][sess["name"]] = [sess.get("session_id"), []]
        if card not in rec[1] and len(rec[1]) <= EDIT_LINK_MAX:
            rec[1].append(card)
    return None


# --- links -----------------------------------------------------------------------------------------------------------

def explicit(st):
    """{executor: [(slug, card, how)]} - the link set at delegation time (flag, brief): it lives in the session record
    (board), a new session with the same name starts without it, and a delegation without --board does not remove it."""
    return {s["name"]: [(s["board"]["slug"], s["board"]["card"], s["board"].get("how") or "flag")]
            for s in st["sessions"].values() if isinstance(s.get("board"), dict)}


def _last(st):
    last = {}
    for d in st["delegations"].values():
        if d["session"] not in last or d["created"] >= last[d["session"]]["created"]:
            last[d["session"]] = d
    return last


def links(st, name, given=None):
    """The cards of executor name: [(slug, card, how)] - first those set at delegation time, then by edits (if there are no
    more than EDIT_LINK_MAX). given - explicit(st) computed in advance."""
    out = list((explicit(st) if given is None else given).get(name, []))
    sid = (st["sessions"].get(name) or {}).get("session_id")
    for slug, board in (st.get("boards") or {}).items():
        rec = (board.get("touched") or {}).get(name)
        mine = rec[1] if rec and rec[0] == sid else []
        if len(mine) <= EDIT_LINK_MAX:
            out += [(slug, c, "edit") for c in mine if not any(o[:2] == (slug, c) for o in out)]
    return out


def shared(st):
    """What is shared by all cards and directions of one computation: the links set at delegation time, each session's
    first and last delegation, who has mail queued - one pass over the state, not one per executor."""
    last = _last(st)
    # since when the executor has been working - by its last delegation (the same name again - anew)
    return {"given": explicit(st), "first": {n: d["created"] for n, d in last.items()}, "last": last,
            "queued": {m["session"] for m in st["messages"].values() if m["state"] in ("queued", "typing")}}


def worker(st, name, now, common=None):
    """What is up with executor name: {state: working | handed | stopped | removed, since, waiting, stuck, gist}."""
    from core import delegations
    common = common or shared(st)
    sess = st["sessions"].get(name) or {}
    d = common["last"].get(name)
    state = sess.get("state")
    # handed in: there was an answer to the task (a removed executor's task goes to the archive - core.archived - with a truncated answer)
    answered = bool(d) and (d["state"] in ("replied", "done") or d["state"] == "archived" and bool(d.get("reply")))
    # "handed in since" is the time of its result: a lead's edit right after the result, before it was put to sleep, is already sorting out
    if state == "retired":   # handed in and went to sleep (or was stopped: then there is no answer)
        return {"state": "handed", "since": d.get("answered_at") or d["since"]} if answered else \
            {"state": "stopped", "since": sess.get("since")}
    if not sess or state == "removed":   # removed (including by the executors.remove_after_hours deadline) - handed in, if it answered
        return {"state": "handed", "since": d.get("answered_at") or d["since"]} if answered else \
            {"state": "removed", "since": sess.get("since")}
    if state in ("starting", "idle", "busy", "dialog", "held"):
        # a stale card waits until askexpired raised it once (askexpired.holds), as it holds the reminders
        waiting = state == "held" or bool(d and d["state"] == "waiting-owner") or bool(owner.open_asks(sess, now)) \
            or askexpired.holds(sess, now, st["delegations"])
        stuck = bool(d) and not waiting and delegations.stuck(st, d, common["queued"], now=now) is not None
        gist = (sess.get("gist") or {}).get("text") or sess.get("about") or ""
        return {"state": "working", "since": sess.get("since"), "waiting": waiting, "stuck": stuck, "gist": gist}
    return {"state": "stopped", "since": sess.get("since")}


def executors_of(st, slug):
    """The executors of direction slug: raised by its lead (a session with this backlog)."""
    leads = {s["name"] for s in st["sessions"].values() if s.get("backlog") == slug}
    return [s for s in st["sessions"].values() if s.get("role") == "executor" and not s.get("backlog")
            and s.get("raised_by") in leads]


# --- the facts layer (spec K2) ---------------------------------------------------------------------------------------

def facts(st, slug, cards, now, common=None):
    """Facts of work on the cards of board slug. cards - [{id, status, mtime?, updated?}] from the files. Writes nothing.

    {"cards": {id: {kind, ...}}, "unlinked": [{name, about, state, since}], "counts": {working, unsorted, unlinked},
     "lag"?: {since, age, delegations, owner} - the board lags behind the work (lag below)}
    kind: working - a linked executor works on it (who, gist, waiting, stuck, handed, total);
          unsorted - all linked ones handed in, and the lead or the owner has not touched status and notes since (since, who);
          unknown - the card file changed after the hand-in, and there is no edit fact (since);
          idle - In Progress, no live linked ones and no one editing it now (last: {ts, by} - the last known edit).
    A session that is not linked to the card but edited it (the last edit of the card) in the turn it is running now, no
    longer than EDITING_FRESH ago (a lead, another agent), also works on it: kind working, who - it. The turn is the core's
    fact (turn_seq), the edit is the direction.card fact (seq). Only for an In Progress card with no live executor on it
    that is not "handed in, not sorted out": a lead's non-judging edit of a handed-in card does not hide it.
    Done and cards without facts - no entry. The keys of cards are card ids as in the files; links are matched by TASK-N."""
    edits = (((st.get("boards") or {}).get(slug) or {}).get("cards") or {})
    common = common or shared(st)
    by_card, unlinked = {}, []
    team = executors_of(st, slug)
    for sess in team:
        mine = [c for s, c, _how in links(st, sess["name"], common["given"]) if s == slug]
        if not mine:
            first = common["first"].get(sess["name"])
            if first is not None and now - first <= UNLINKED_WINDOW:
                w = worker(st, sess["name"], now, common)
                if w["state"] != "removed":
                    unlinked.append({"name": sess["name"], "about": sess.get("about") or "", "state": w["state"],
                                     "since": w.get("since") if w["state"] != "working" else first})
            continue
        w = worker(st, sess["name"], now, common)
        for c in mine:
            by_card.setdefault(c, []).append((sess["name"], w))
    out = {}
    for c in cards:
        cid, status = c.get("id"), c.get("status")
        if not cid or status == "Done":
            continue
        key = card_id(cid) or cid
        linked = [(n, w) for n, w in by_card.get(key, []) if w["state"] != "removed"]
        live = [(n, w) for n, w in linked if w["state"] == "working"]
        handed = [(n, w) for n, w in linked if w["state"] == "handed"]
        edit = edits.get(key) or {}
        if live:
            out[cid] = {"kind": "working", "who": [n for n, _ in live], "gist": live[0][1].get("gist") or "",
                        "waiting": any(w.get("waiting") for _, w in live), "stuck": any(w.get("stuck") for _, w in live),
                        "handed": len(handed), "total": len(linked)}
            continue
        if handed and len(handed) == len(linked):
            since = max(w["since"] or 0 for _, w in handed)
            if (edit.get("judged") or 0) < since:
                mtime = c.get("mtime") or 0
                if mtime > since + UNKNOWN_SLACK and mtime > (edit.get("ts") or 0) + UNKNOWN_SLACK:
                    out[cid] = {"kind": "unknown", "since": since}
                else:
                    out[cid] = {"kind": "unsorted", "since": since, "who": [n for n, _ in handed]}
                continue
        if status == "In Progress":
            holder = editing_now(st, edit, now)
            if holder:
                out[cid] = holder
                continue
            last = {"ts": edit["ts"], "by": edit.get("by"), **({"owner": True} if edit.get("owner") else {})} \
                if edit.get("ts") else \
                {"ts": None, "by": None, "updated": c.get("updated")}
            out[cid] = {"kind": "idle", "last": last}
    unlinked.sort(key=lambda u: -(u.get("since") or 0))
    counts = {"working": sum(1 for f in out.values() if f["kind"] == "working"),
              "unsorted": sum(1 for f in out.values() if f["kind"] in ("unsorted", "unknown")),
              "unlinked": len(unlinked)}
    behind = lag(st, slug, cards, edits, now, common, {s["name"] for s in team})
    return {"cards": out, "unlinked": unlinked, "counts": counts, **({"lag": behind} if behind else {})}


def editing_now(st, edit, now):
    """The fact "working" for the session that made the card's last edit in the turn it is running now (and not longer
    than EDITING_FRESH ago), else None: a lead or an agent that no `hub task run --board` link names. Two core facts, no
    guess from the text: the session is busy, and the edit came after that turn started. The turn is over - it works on
    nothing, the card is nobody's again. The edit is a sign, not a promise: a session that edited a card once and went
    on to other things shows for at most EDITING_FRESH."""
    sess = st["sessions"].get(edit.get("by") or "") if not edit.get("owner") else None
    if not sess or sess.get("state") not in chain.BUSY or not edit.get("seq") or edit["seq"] < (sess.get("turn_seq") or 0) \
            or now - (edit.get("ts") or 0) > EDITING_FRESH:
        return None
    gist = sess.get("gist") or {}
    return {"kind": "working", "who": [sess["name"]],
            "waiting": sess["state"] == "dialog" or bool(owner.open_asks(sess, now)),
            "stuck": False, "gist": (gist.get("text") or "") if gist.get("turn") == sess.get("turn_seq") else ""}


# --- the board lags behind the work (boards-sync A) -------------------------------------------------------------------
# Only the cards are the board, and only a session edits them. After the last edit of any card the direction's lead got
# the owner's word or raised an executor without a card, and nobody touched the cards for LAG_GRACE: a fact for the owner,
# not a verdict (a question needs no edit; a fresh edit of one card does not cure a stale note in another). An executor
# with a card is not counted: its card already says "working" / "handed in". Replay of the owner's journal 24-27.10, two
# directions: 34 times lit, 2 false (a one-off delegation, a question); hand-ins counted too - 55, 22 false.
LAG_GRACE = 5 * 60   # s: the lead edits cards in the same turn (a lead's turn: p95 under a minute)


def lag(st, slug, cards, edits, now, common, team):
    """{since: the oldest such event, age: s, delegations, owner} or None. team - the names of the direction's executors.
    The last edit is the newest of the card files and the direction.card facts (a file written while hubd refused the fact
    still counts); no cards - None. The owner's words: delivered to the lead (a queued one it has not seen yet), his own -
    not × on a card, "Deny", a slash command or a page button; they leave the state with the message (core.pruned, a day)."""
    from core.tools import dismissal
    last = max([c.get("mtime") or 0 for c in cards] + [e.get("ts") or 0 for e in edits.values()], default=0)
    if not cards or not last:
        return None
    leads = {s["name"] for s in st["sessions"].values() if s.get("backlog") == slug}
    raised = [d["created"] for d in st["delegations"].values() if d.get("for_session") in leads and d["session"] in team
              and d["created"] > last and not d.get("board")
              and not any(s == slug for s, _c, _h in links(st, d["session"], common["given"]))]
    # the session that got it (session), not the address typed (to): a word to a sleeping executor goes to the lead
    said = [m["created"] for m in st["messages"].values() if m["session"] in leads and m["created"] > last
            and m.get("from_owner") and m["state"] == "delivered"
            and not (dismissal(m) or m.get("card_declined") or m.get("command") or m.get("action"))]
    first = min(raised + said, default=None)
    if first is None or now - first < LAG_GRACE:
        return None
    return {"since": first, "age": int(now - first), "delegations": len(raised), "owner": len(said)}


# the lines below are the direction commands' output (modules/directions): in the language of the process that prints
# them (a session's — English)
def _age(s):
    from core import i18n
    if s < 3600:
        return i18n.t("core.board.age_min", min=max(1, s // 60))
    if s < 86400:
        return i18n.t("core.board.age_hours", hours=s // 3600, min=s // 60 % 60)
    return i18n.t("core.board.age_days", days=s // 86400)


def lag_line(behind):
    """"The board lags: after the last card edit 2 tasks without a card, the owner's word, 8 min"; no lag - empty."""
    if not behind:
        return ""
    from core import i18n
    parts = [i18n.t(word, count=behind[key]) for key, word in (("delegations", "core.board.lag_delegations"),
                                                                ("owner", "core.board.lag_owner")) if behind.get(key)]
    return i18n.t("core.board.lag", parts=", ".join(parts), age=_age(int(behind["age"])))


# --- the line to the lead on hand-in (spec K4)--------------------------------------------------------------------------------
# The texts are for direction sessions (evalevals/prompts/direction-board.json)
HANDED_ALONE = T("core.msg.handed_alone",
                 "Card {card} ({status}): {name} handed in; nobody else works on it. Closed in full — "
                 "`hub direction {slug} task edit {card} -s Done`; something is left — a note on what is left and whom we "
                 "wait for (`hub direction {slug} task edit {card} --append-notes \"…\"`): it removes the \"not sorted out\" "
                 "mark on the board.")
HANDED_OTHERS = T("core.msg.handed_others", "Card {card} ({status}): {name} handed in; still working on it: {others}.")
HANDED_HEAD = T("core.msg.handed_head", "Executor {name} handed in its task and fell asleep.")
REMOVED_OPEN_HEAD = T("core.msg.removed_open_head",
                      "Executor {name} was removed with its task not done: task {id} is {state}.")
NOT_HANDED_HEAD = T("core.msg.not_handed_head",
                    "Executor {name} fell asleep without a result: task {id} is {state}. What it got done — hub show "
                    "{name}; to continue — hub session wake {name} {wakers}.")
# hub task run from a direction session without a link to a card (spec K1): not a refusal - one line
NO_CARD = ("This task has no board card — the owner will not see it on the {slug} board. Work on a card — next time give "
           "it: --board TASK-N; one-off work without a card — leave it as is.")


def handed_lines(st, name, cards, now):
    """K4 lines for the lead: executor name handed in. cards - [{slug, card, status}], the statuses were read by the daemon
    from disk (session.close_requested retire); Done and vanished cards - no line."""
    common = None
    lines = []
    for c in cards or []:
        slug, card, status = c.get("slug"), card_id(c.get("card")), c.get("status")
        if not slug or not card or not status or status == "Done":
            continue
        common = common or shared(st)
        others = []
        for sess in executors_of(st, slug):
            if sess["name"] == name or not any(s == slug and k == card
                                               for s, k, _ in links(st, sess["name"], common["given"])):
                continue
            if worker(st, sess["name"], now, common)["state"] == "working":
                others.append(sess["name"])
        text = HANDED_OTHERS if others else HANDED_ALONE
        lines.append(text.format(card=card, status=status, name=name, slug=slug, others=", ".join(others)))
    return lines


# --- output for sessions (hub direction <slug> task list, resume, list) ----------------------------------------------

def _when(ts):
    import time
    return time.strftime("%d.%m %H:%M", time.localtime(ts)) if ts else "?"


def verdict(f):
    """A card's fact in one line (no final period) - for sessions in the direction commands' output."""
    from core import i18n
    if f["kind"] == "working":
        out = i18n.t("core.board.working", who=", ".join(f["who"]))
        if f.get("waiting"):
            out += i18n.t("core.board.waits_owner")
        elif f.get("stuck"):
            out += i18n.t("core.board.stuck")
        if f.get("handed"):
            out += i18n.t("core.board.handed", handed=f["handed"], total=f["total"])
        return out
    if f["kind"] == "unsorted":
        return i18n.t("core.board.unsorted", when=_when(f["since"]), who=", ".join(f["who"]))
    if f["kind"] == "unknown":
        return i18n.t("core.board.unknown", when=_when(f["since"]))
    last = f.get("last") or {}
    if last.get("ts"):
        who = i18n.t("core.board.owner") if last.get("owner") else last.get("by") or "?"
        return i18n.t("core.board.no_executor_by", when=_when(last["ts"]), who=who)
    return i18n.t("core.board.no_executor") + (i18n.t("core.board.last_edit_utc", when=last["updated"])
                                               if last.get("updated") else "")


def unlinked_line(out):
    """"No card in a day: ..." - direction executors without a link; none - empty."""
    names = [u["name"] for u in out.get("unlinked") or []]
    if not names:
        return ""
    from core import i18n
    more = i18n.t("core.board.unlinked_more", count=len(names) - 8) if len(names) > 8 else ""
    return i18n.t("core.board.unlinked", names=", ".join(names[:8]), more=more)
