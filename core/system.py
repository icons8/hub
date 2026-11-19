"""System agents — Hub Developer (core/dev.py) and Agent Developer (modules/agent_dev): every hub always has them
(owner decision 2026-10-08: system agents for everyone, always, so they can be neither put to sleep nor deleted; the
core checks on every start that they exist and adds them; agent-developer is mandatory in the core).

On every start the core names them with the fact `agents.system` and brings back the missing ones: absent — a new one;
removed, closed or failed — the same conversation if it is on disk, otherwise a new one with the same profile (the
agent's CLAUDE.md); demoted to worker — an agent again. The fold does not let them be put to sleep, deleted, dismissed
or renamed (no matter who asks). Parking an idle free session (core/parking) is not sleep: the process comes up with
the first message. hub.json `dev.enabled`/`agent_dev.enabled` no longer switch them off; `HUB_NO_SYSTEM_AGENTS` is
only for core tests and test stands."""
import os

from core import coretext
from core.coretext import T

GONE = ("removed", "closed", "failed")
# daily fleet review (owner decision 2026-10-08: once a day, from transcripts): the `hub coach` counters only show where
# to look, the review is done by agent-dev (the model) over session conversations; it brings things to the owner as a
# form, and changes nothing on its own without a "yes"
REVIEW = {"id": "agent-dev-review", "cron": "0 10 * * *",
          "prompt": "Daily fleet review by transcripts — skill agent-developer, section \"6. Daily review\". Nothing to "
                    "bring to the owner — end the turn with a single \"—\"."}
# the stops audit of the agents (owner 2026-10-28: "make sure this internal audit runs on other hubs too and they do not
# skip it"; "save tokens, it must not be heavy"): a stop is a rule "ask / show the owner before X" in an agent's CLAUDE.md.
# The precheck (modules/agent_dev/stops-audit.py, no model) wakes agent-dev only when an audit is due — weekly or when an
# agent's file grew — and repeats it on every tick until agent-dev marks it done; the page shows whether it runs (stops_view)
STOPS = {"id": "agent-dev-stops", "cron": "0 11 * * *",
         "prompt": "Stops audit of the agents — skill agent-developer, section \"6a. Stops audit\"; the commands are in "
                   "the precheck output below. It can't be put off: the reminder repeats every day until you mark it "
                   "--done. Nothing to bring to the owner — mark it --done and end the turn with a single \"—\"."}
STOPS_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "modules", "agent_dev",
                            "stops-audit.py")
STOPS_SILENT = 2 * 86400   # no tick for longer — the hub slept or the job is broken: the page shows it red
STOPS_SKIPPED = 3          # ticks in a row with "due" and no --done: agent-dev skips the audit
# "Train the executors" in the agent-dev menu (owner decision 2026-10-08: forced additional training): the same review
# right now, and the button is the owner's "yes" to retrain the agents
TRAIN = T("core.system.train", "Train the executors (the owner's button): a fleet review now — skill agent-developer, section \"6. Daily "
         "review\", \"by the button\": retrain the agents you find yourself, with cases and runs; the result — whom you "
         "retrained with what and how it was checked.")   # a phrase: the owner reads the button's task in their language
# the owner's rules for all sessions (hub.json rules) go to agent-dev for review when they appear or change
# (owner decision 2026-10-08: analyse the rules and say what they may get in the way of; the hub's current rules are
# tuned through evals, and personal ones can, and most likely do, break that)
RULES = T("core.system.rules", "The owner set rules for all the hub's sessions (hub.json `rules`; the core puts them into every session's system "
         "prompt) — review them: skill agent-developer, section \"7. The owner's rules\". Tell them what the rules may get "
         "in the way of: the rules of the hub and the agents are tuned and checked with evals, personal ones are not and "
         "most likely break them. The rules:\n")
RENEW = 7 * 86400   # a recurring job lives at most 30 days (core/schedule) — the core renews it in advance
REFUSED = T("core.system.refused",
            "{name} — a system agent of the hub: it is always there — it can't be put to sleep, deleted, dismissed or "
            "renamed")


def off():
    return bool(os.environ.get("HUB_NO_SYSTEM_AGENTS"))


def specs(cfg, state_dir, st=None):
    """[(name, session.requested data)] — who must exist: always Hub Developer and Agent Developer (in this order: the
    second is the trainer); the liaison only while the hub bus is on (core/liaison.py)."""
    from core import dev
    from modules.agent_dev import agent
    out = [dev.spec(cfg, state_dir), agent.spec(cfg, state_dir)]
    from core import liaison
    if liaison.needed(st):   # the hub bus is on or the owner's chats are connected (core/chats.py)
        out.append(liaison.spec(state_dir))
    return out


def jobs(cfg, state_dir):
    """System schedule jobs: [schedule.set specification]."""
    from modules.agent_dev import agent
    name = agent.spec(cfg, state_dir)[0]
    return [{**REVIEW, "session": name}, {**STOPS, "session": name, "check": stops_check(cfg)}]


def stops_check(cfg):
    """The precheck command of the stops audit: the hub's own Python (as hooks get it), paths quoted for the shell the
    schedule runs it in (hub.json `schedule.shell`; a change of it changes the check, and due_jobs sets the job anew)."""
    import platforms
    from core import schedule
    return platforms.current.job_quote([*platforms.current.python(), STOPS_SCRIPT], schedule.shell(cfg))


def due_jobs(st, specs, now):
    """schedule.set events for system jobs: missing, cancelled, changed or about to expire."""
    from core.schedule import MAX_DAYS
    out = []
    for spec in specs:
        job = (st.get("schedule") or {}).get(spec["id"]) or {}
        same = job.get("state") == "active" and job.get("system") and \
            all(job.get(k) == spec.get(k) for k in ("session", "prompt", "cron", "check"))
        if not same or (job.get("until") or 0) - now < RENEW:
            out.append(("schedule.set", spec["id"], {**spec, "until": now + MAX_DAYS * 86400, "system": True,
                                                     "channel": "core"}))
    return out


def stops_view(st, now):
    """The stops audit for the page (the agent-dev menu): when it was last marked, since when it is requested and not
    marked, the last precheck code and the alarm — "off" (no active job), "silent" (no tick for 2 days), "failed" (the
    check failed), "skipped" (due three ticks in a row without --done), or None. The core knows the ticks only: "done" is
    the first quiet tick after due ones (the script marks --done in its own folder)."""
    from core.schedule import CHECK_NEW
    job = (st.get("schedule") or {}).get(STOPS["id"]) or {}
    news = job.get("news") or 0
    alarm = None
    if job.get("state") != "active":
        alarm = "off"
    elif now - (job.get("last_run") or job.get("created") or now) > STOPS_SILENT:
        alarm = "silent"
    elif job.get("last_check") not in (None, 0, CHECK_NEW):
        alarm = "failed"
    elif news >= STOPS_SKIPPED:
        alarm = "skipped"
    return {"ran": job.get("last_run") or job.get("created"), "check": job.get("last_check"), "done": job.get("news_cleared"),
            "asked": job.get("news_since") if news else None, "streak": news, "alarm": alarm}


def refused(st, name):
    """Refusal reason if the session is a system one; otherwise None."""
    return REFUSED.format(name=name) if name in (st.get("system") or []) else None


def transcript_path(cwd, session_id, claude_dir=None):
    """Where Claude Code keeps the conversation of a session in this folder: <config>/projects/<slug>/<id>.jsonl."""
    from hosts import worktree
    root = claude_dir or os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return os.path.join(root, "projects", worktree.slug(cwd), f"{session_id}.jsonl")


def has_transcript(cwd, session_id, claude_dir=None):
    """Conversation on disk: without it `--resume` fails with "No conversation found"."""
    return bool(cwd and session_id) and os.path.exists(transcript_path(cwd, session_id, claude_dir))


def returning(st, name, data, new_id, transcript=has_transcript, resumable=None):
    """Events that bring a system agent back: [(type, session, data)]; if it is in place — []. resumable(name, sess) —
    the host's own answer whether the session's next process continues its conversation (Codex: its thread on disk,
    hosts/codex_life.resumable); None from it or no such host — Claude Code's transcript."""
    sess = st["sessions"].get(name)
    if sess is None:
        return [("session.requested", name, {**data, "session_id": new_id})]
    if sess.get("kind") == "hub":   # the name is taken by a hub — do not create a second one
        return []
    out = []
    if sess["state"] in GONE:
        found = resumable(name, sess) if resumable else None
        if not sess.get("has_history") or (transcript(sess.get("cwd"), sess.get("session_id")) if found is None
                                           else found):
            # bring it back with its conversation and tools (like hub session revive) — in its folder, if it exists
            cwd = sess["cwd"] if sess.get("cwd") and os.path.isdir(sess["cwd"]) else data["cwd"]
            out.append(("session.requested", name, {"cwd": cwd, "session_id": sess["session_id"], "revive": True}))
        else:
            out.append(("session.requested", name, {**data, "session_id": new_id,
                                                    "profile": sess.get("profile") or sess.get("session_id")}))
    if sess.get("role") != "agent":   # dismissed (and maybe deleted after that) — an agent again
        out.append(("session.role_set", name, {"role": "agent", "about": sess.get("about") or data["about"],
                                               "icon": sess.get("icon") or data["icon"]}))
    return out


def invite_off():
    """No greeting of a new owner: core tests (a hub session there is not a new hub) and stands, HUB_NO_INVITE."""
    return bool(os.environ.get("HUB_NO_INVITE"))


def invite_due(st):
    """The hub's first start (owner 2026-11-09: a new hub wrote nothing until the owner did): the hub session has come up
    once and has not had a turn yet, the owner has come (owner.came: their page opened — the greeting is in their
    language, not the machine's; owner 2026-11-19) and has neither written nor been invited. The fact owner.invited
    makes the fold give it one message. The session is the one the fold would write to (reducer._hub), not one by a
    name."""
    from core import reducer
    name = reducer._hub(st)
    sess = st["sessions"].get(name or "") or {}
    return bool(name and sess.get("ever_started") and not sess.get("turn_seq") and st["owner"].get("came")
                and reducer.invite_open(st))


def rules_review(st, rules, trainer):
    """Events: the owner's rules are new or different — agent-dev reviews them (a message to it) and the core remembers the set."""
    import hashlib
    lines = [str(r).strip() for r in rules or [] if str(r).strip()]
    digest = hashlib.sha1("\n".join(lines).encode()).hexdigest()[:12] if lines else ""
    if digest == (st.get("rules_reviewed") or ""):
        return []
    text = RULES + "\n".join(f"- {r}" for r in lines)   # the core's words keep their marks: the owner reads them in their language
    out = [("message.received", None, {"to": trainer, "text": str(text), "l10n": coretext.marks(text),
                                       "channel": "core"})] if lines and trainer else []
    # the set also goes to running sessions: they read the rules at process start, the core will attach the new ones to the next message
    return out + [("rules.reviewed", None, {"digest": digest, "rules": lines, "channel": "core"})]
