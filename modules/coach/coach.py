"""Fleet observer: coach (owner request 2026-10-03: track when a worker has outgrown its role and should become a
direction, or a worker should become an agent, and suggest retraining agents). Since 2026-10-05 it follows the model
"specialists, executors, directions": there are no workers anymore.

Counters over the hub journal (archive and current), no model. It only suggests and changes nothing; the owner decides:
  direction   executor that people keep coming back to: 3+ tasks or 2+ wake-ups a week, or it has outgrown its role
              (a day or more on one task, many turns, compaction or hundreds of steps): a direction candidate
  agent       the same work again: 3+ executors in one repo in a week and no specialist there
  bypass      the same, but the repo has a specialist: the executors are not raised by it (hub, owner), work goes around it
  retrain     a specialist accumulates watchdog alerts and error turns in a week: its own and its executors'
  idle_agent  a specialist with no work for a week: neither itself nor its executors

    hub coach                report: what it would suggest now
"""
import json
import os

from core import i18n, journal

WEEK = 7 * 86400
GROWN_SPAN = 24 * 3600      # an executor stays on a task longer: a direction candidate
GROWN_TURNS = 20
GROWN_TOOLS = 500           # ...or hundreds of steps instead of compaction
RECENT = 3 * 86400          # suggest only for those who worked in the last three days
RETURNS = 3                 # this many tasks for one executor in a week: people keep coming back to it
WAKES = 2                   # ...or this many wake-ups after handing in
REPEAT = 3                  # this many executors in one repo in a week: a repeat
RETRAIN = 3                 # watchdog alerts plus error turns for a specialist in a week
SKIP_KINDS = ("hub", "operator")


def _events(state_dir):
    """Events of all journals in order, without repeats (the archive tail is carried over into the new journal)."""
    paths = journal.archives(state_dir) + [os.path.join(state_dir, "journal.jsonl")]
    seen = 0
    for p in paths:
        try:
            f = open(p, "rb")
        except FileNotFoundError:
            continue
        with f:
            for line in f:
                if not line.startswith(b"{"):
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("seq", 0) > seen:
                    seen = e["seq"]
                    yield e


def repo(cwd):
    return (cwd or "").split("/.claude/worktrees/")[0].rstrip("/")


def _sessions(state_dir, now):
    s = {}
    counted = {}   # event seq -> (session, list): an event rejected by the core (core.rejected) did not happen

    def get(name):
        return s.setdefault(name, {"name": name, "cwd": None, "role": "worker", "kind": None, "raised_by": None,
                                   "backlog": None, "created": None, "first": None, "last": None,
                                   "turns": 0, "tools": 0, "compact": 0, "alerts": 0, "errors": 0,
                                   "tasks": [], "wakes": []})

    def count(e, name, key):
        if now - e["ts"] < WEEK:
            get(name)[key].append(e["seq"])
            counted[e["seq"]] = (name, key)
    for e in _events(state_dir):
        t, n, d, ts = e["type"], e.get("entity"), e.get("data") or {}, e["ts"]
        if t == "core.rejected":
            name, key = counted.pop(d.get("of"), (None, None))
            if name in s and d.get("of") in s[name][key]:
                s[name][key].remove(d["of"])
            continue
        if t == "delegation.created" and d.get("session"):
            count(e, d["session"], "tasks")
            continue
        if t in ("task.backlog", "task.backlog_closed", "task.backlog_dropped"):
            slug = d.get("slug")
            if t == "task.backlog_dropped":   # the hub or the owner took it off one session (anyone else was refused)
                by_hub = d.get("channel") != "session" or (s.get(d.get("sender")) or {}).get("kind") == "hub"
                names = {slug, *(d.get("slugs") or [])} - {None}
                if by_hub and d.get("session") in s and s[d["session"]]["backlog"] in names:
                    s[d["session"]]["backlog"] = None
            elif t == "task.backlog_closed":
                names = {slug, *(n for n in (d.get("slugs") if isinstance(d.get("slugs"), list) else []) if isinstance(n, str))} - {None}
                for x in s.values():
                    x["backlog"] = None if x["backlog"] in names else x["backlog"]
            elif slug and d.get("sender") in s:
                me = s[d["sender"]]
                (get(slug) if me["kind"] == "hub" else me)["backlog"] = slug
            continue
        if not n or t.startswith(("core.", "net.", "message.")):
            continue
        if t == "session.requested" and d.get("channel") == "session" and \
                (s.get(d.get("sender")) or {}).get("kind") != "hub":
            continue   # a working session only asks the hub for it (core/machines.py: from_session -> ask_hub)
        if t == "session.requested":
            x = get(n)
            if x["created"] is None or d.get("fork_of") or d.get("kind") or d.get("executor"):
                x.update(cwd=d.get("cwd") or x["cwd"], kind=d.get("kind") or x["kind"])
            if d.get("fork_of"):   # agent fork (old journals, before 04.10): not an executor
                x["role"] = "fork"
            elif d.get("executor"):
                x.update(role="executor", raised_by=d.get("raised_by"))
            elif d.get("role") == "agent":
                x["role"] = "agent"
            x["created"] = x["created"] or ts
        elif t == "session.role_set":
            get(n)["role"] = d.get("role") or get(n)["role"]
        elif t == "task.adopted":   # migration 04.10: a live worker is an executor
            get(n).update(role="executor", raised_by=d.get("raised_by"), backlog=d.get("backlog") or get(n)["backlog"])
        elif t == "session.renamed" and d.get("to"):
            s[d["to"]] = {**get(n), "name": d["to"]}
            s.pop(n, None)
        elif t == "session.wake_requested" and n in s:
            count(e, n, "wakes")
        elif n in s:
            x = s[n]
            if t == "turn.started":
                x["turns"] += 1
                x["first"] = x["first"] or ts
                x["last"] = ts
            elif t == "turn.tool":
                x["tools"] += 1
            elif t == "session.compacted":
                x["compact"] += 1
            elif t == "watch.alert" and now - ts < WEEK:
                x["alerts"] += 1
            elif t == "turn.result" and now - ts < WEEK:
                # an error is a result with error text; an empty error_during_execution is a stop or an aborted turn
                x["errors"] += 1 if d.get("is_error") and (d.get("text") or "").strip() else 0
    return s


def _hours(sec):
    return i18n.t("cli.coach.hours", n=f"{sec / 3600:.0f}") if sec < 72 * 3600 else \
        i18n.t("cli.coach.days", n=f"{sec / 86400:.0f}")


def analyze(state_dir, now, directions=None):
    """{suggestions: [{kind, subject, why, sessions}]}."""
    if directions is None:
        try:
            from modules.directions import direction
            directions = set(direction.active())
        except Exception:  # noqa: BLE001 — no module or folder means no directions
            directions = set()
    ss = {n: x for n, x in _sessions(state_dir, now).items() if x["kind"] not in SKIP_KINDS}
    agents = {n: x for n, x in ss.items() if x["role"] == "agent"}
    # executors (and sessions without a role, created by hand): not directions
    execs = {n: x for n, x in ss.items() if x["role"] in ("executor", "worker") and not x["backlog"]
             and n not in directions and x["cwd"]}
    out = []
    for n, x in sorted(execs.items()):
        if not (x["last"] and now - x["last"] < RECENT):
            continue
        span = x["last"] - (x["first"] or x["last"])
        why, back = [], len(x["tasks"]) >= RETURNS or len(x["wakes"]) >= WAKES
        if len(x["tasks"]) >= RETURNS:
            why.append(i18n.t("cli.coach.why_tasks", n=len(x["tasks"])))
        if len(x["wakes"]) >= WAKES:
            why.append(i18n.t("cli.coach.why_wakes", n=len(x["wakes"])))
        if span >= GROWN_SPAN and x["turns"] >= GROWN_TURNS and (x["compact"] or x["tools"] >= GROWN_TOOLS):
            why.append(i18n.t("cli.coach.why_span", span=_hours(span), turns=x["turns"], tools=x["tools"])
                       + (i18n.t("cli.coach.why_compact", n=x["compact"]) if x["compact"] else ""))
        if why:
            out.append({"kind": "direction", "subject": n, "sessions": [n],
                        "why": i18n.t("cli.coach.back", why="; ".join(why)) if back
                        else i18n.t("cli.coach.outgrown", why="; ".join(why))})
    agent_repo = {repo(x["cwd"]): n for n, x in agents.items() if x["cwd"]}

    def zone(r):
        """The specialist's repo or a folder inside it (repos in <agent folder>/repos/*, 07.10): its zone."""
        hit = max((a for a in agent_repo if r == a or r.startswith(a + "/")), key=len, default=None)
        return hit or r
    by_repo, sandbox = {}, os.path.join(state_dir, "")
    for n, x in execs.items():
        # the hub sandbox is a shared tasks folder without a repo, not anyone's repo
        if x["created"] and now - x["created"] < WEEK and x["turns"] and x["raised_by"] not in agents \
                and not (x["cwd"] + "/").startswith(sandbox):
            by_repo.setdefault(zone(repo(x["cwd"])), []).append(n)   # raised by the specialist: its work, not around it
    for r, names in sorted(by_repo.items()):
        if len(names) < REPEAT:
            continue
        names = sorted(names)
        if r in agent_repo:
            out.append({"kind": "bypass", "subject": agent_repo[r], "sessions": names,
                        "why": i18n.t("cli.coach.why_bypass", agent=agent_repo[r], n=len(names),
                                      names=", ".join(names))})
        else:
            out.append({"kind": "agent", "subject": r, "sessions": names,
                        "why": i18n.t("cli.coach.why_agent", n=len(names), repo=r, names=", ".join(names))})
    for n, x in sorted(agents.items()):
        team = [y for y in ss.values() if y is not x and y["raised_by"] == n and not y["backlog"]]   # a direction is not its own
        own, theirs = x["alerts"] + x["errors"], sum(y["alerts"] + y["errors"] for y in team)
        if own + theirs >= RETRAIN:
            out.append({"kind": "retrain", "subject": n, "sessions": [n] + sorted(y["name"] for y in team if
                                                                               y["alerts"] + y["errors"]),
                        "why": i18n.t("cli.coach.why_retrain", alerts=x["alerts"], errors=x["errors"])
                               + (i18n.t("cli.coach.why_theirs", n=theirs) if theirs else "")})
        last = max([x["last"] or x["created"] or now] + [y["last"] for y in team if y["last"]])
        if now - last >= WEEK and now - (x["created"] or now) >= WEEK:
            out.append({"kind": "idle_agent", "subject": n, "sessions": [n],
                        "why": i18n.t("cli.coach.why_idle", span=_hours(now - last))})
    return {"suggestions": out}


SAY = {"direction": "cli.coach.kind_direction", "agent": "cli.coach.kind_agent", "bypass": "cli.coach.kind_bypass",
       "retrain": "cli.coach.kind_retrain", "idle_agent": "cli.coach.kind_idle_agent"}   # catalog keys


def report(state_dir, now):
    rep = analyze(state_dir, now)
    lines = [i18n.t("cli.coach.header", n=len(rep["suggestions"]))] if rep["suggestions"] else [i18n.t("cli.coach.none")]
    lines += [f"  {i18n.t(SAY[s['kind']]):<20} {s['subject']:<28} {s['why']}" for s in rep["suggestions"]]
    return "\n".join(lines)
