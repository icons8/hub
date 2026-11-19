"""Restarting the hub by a command (owner decision 2026-09-30): `hub restart` is a fact in the journal, not a script in
/tmp. The pure part.

The whole hub (the core and all modules, new code) is requested by the owner or a hub session; the core waits until
all sessions are free and have no background commands: a hubd restart cuts turns and kills the background (`--now` —
do not wait). It waits no longer than the ceiling (WAIT_MINUTES from the first request; friction digest 16.10: someone
else's turn held a restart for up to 188 min): after that the restart goes ahead, and after the bring-up the core tells
the cut sessions to continue — as after a limit, with the same task (reducer: cut_turn). Parts without the core (the
page, Telegram, files…) do not touch sessions — any session may request them, they run at once. A session for whose
sake the hub restarts the whole hub (it asks during a turn on that session's message) ends its turn itself — after the
bring-up the core tells it to continue too, if it is waiting (asker, wake).
It is done by hub-up: hubd puts a `restart` file into the state folder (see daemon.Hub.restart). After the bring-up the
core writes to the hub.

The page holds a restart while it has something the server lacks: a draft on the page or an unsent voice message must
not be cut by a restart (including an automatic one — after an update, a marketplace install, an exchange between
hubs). A voice message and a plugin's draft (data-hub-hold) hold. The tab reports it with the fact page.held every
HOLD_BEAT s while it holds, and with an empty one when it lets go; a closed tab releases at once (pagehide), a crashed
one by itself after HOLD_TTL. It holds the whole hub and the page parts (web, ext:*) for as long as the recording goes,
up to its own ceiling PAGE_MINUTES from the request (or restart_wait_minutes, if that is longer; 0 — none): 05.11 a
recording started 11 min after the request was cut by the 20-min ceiling of busy sessions and lost. `--now` does not
wait for it.
"""
import re

from core import chain, coretext, cutout, guards, langdata, modrun
from core.coretext import T
from core.tools import Rejected

# hubd is not restarted separately: only everything; a part whose built-in code is cut out of the tree is not there (core/cutout.py)
PARTS = tuple(cutout.parts(("web", "scheduler", "browser", "telegram", "files", "tailnet", "watch", "update", "conduit",
                            "stats", "gateway", "bus-relay", "cloudflare")))
FLAG = "restart"
# if the restart file lies longer (s), nobody is there to read it (the old service layout without hub-up, hub-up is
# down): hubd writes the request over it. If younger, hub-up has not picked it up yet (polled once a second, stopping a
# part takes up to 15 s): the new request waits in the state
UNREAD = 120
STOP = "stop"   # `hub stop`: hub-up stops its children and exits
# the hub was stopped by the owner (`hub stop`, `hub off`): while it is set, a scheduler-launched hub-up (--scheduled —
# a repeat every 5 min and a login on Windows) does not bring the hub up. `hub on` and a hand-started hub-up clear it;
# a crash does not leave it
HELD = "stopped"
CHILD = "HUB_UP_CHILD"   # on hub-up's descendants (core, parts, sessions, commands): their hub-up is no OS service
# who asked when it is the owner: an identifier in restart.by of every hub's state, the word of the Russian texts
# (core/langdata.py, legacy); the page and the bot show it as it is
OWNER_BY = langdata.legacy("restart_by")
WHO_OWNER = T("core.restart.who_owner", "the owner")   # OWNER_BY in a text: the owner reads it in his language
RESTARTED = "HUB_UP_RESTARTED"   # hub-up was started by a full restart: the `stop` file is fresh, not from a past life
UP = "hub-up.json"   # hub-up about itself for `hub stop`: {"pid", "service" — command that stops its service}
WAITING = ("busy", "dialog", "starting")


def running(state_dir):
    """hub-up of this state folder as it wrote about itself (UP), while it runs; {} when it does not: no record, a dead
    pid, or the pid is now a foreign process (hub-up crashed and the number was reused: its start time, when hub-up wrote
    one, and hub-up in its command line)."""
    import json
    import os
    import platforms
    try:
        with open(os.path.join(state_dir, UP)) as f:
            up = json.load(f)
    except (OSError, ValueError):
        return {}
    pid = up.get("pid") if isinstance(up, dict) else None
    if not (isinstance(pid, int) and platforms.current.alive(pid)):
        return {}
    born = platforms.current.process_start(pid) if up.get("started") is not None else None
    if born is not None and born != up["started"]:   # not readable now (None): the command line decides
        return {}
    return up if "hub-up" in platforms.current.command_line(pid) else {}


def find_up(state_dir, gap=0):
    """running(), waiting up to `gap` s while hub-up.json still names a hub-up that has gone: right after a full
    restart the new hub-up (a new process on Windows, platforms restart_gap) has not written about itself yet."""
    import os
    import time
    up, until = running(state_dir), time.time() + gap
    while not up and os.path.exists(os.path.join(state_dir, UP)) and time.time() < until:
        time.sleep(0.2)
        up = running(state_dir)
    return up


def ask_stop(state_dir):
    """`hub stop`'s request to hub-up: the HELD mark first (a scheduler-launched hub-up will not bring the hub up
    again), then STOP, which hub-up reads within a second."""
    import os
    for name in (HELD, STOP):
        with open(os.path.join(state_dir, name), "w") as f:
            f.write("")

LABEL = {"busy": T("core.restart.busy", "a turn is running"),
         "dialog": T("core.restart.dialog", "waits for the owner's answer mid-turn"),
         "starting": T("core.restart.starting", "coming up")}
IN_BACKGROUND = T("core.restart.in_background", "{name}: in the background {work}")
# the core's report to the hub after the bring-up and the `hub status` lines (core/coretext.py: the hub reads the
# English, the owner his language)
RESTARTED_NOTE = T("core.restart.restarted", "The hub was restarted ({asked}){cut}. The core and the modules run on the "
                   "new code; sessions with work were brought up with their previous conversations, free ones will come "
                   "up with their first message (parked in hub status). Check hub status and tell the owner the outcome "
                   "if he is waiting for it.")
ASKED = T("core.restart.asked", "asked by: {by}")
ASKED_WHY = T("core.restart.asked_why", "asked by: {by}, why: {why}")
CUT = T("core.restart.cut", "waited longer than the ceiling and did not get there: {cut} — whose turn or background was "
        "cut, the core told them to continue")
PAGE_CUT = T("core.restart.page_cut", "a recording on the page did not wait out the ceiling ({page}) — it may have been cut")
NOT_DELIVERED = T("core.restart.not_delivered", "not delivered ({what})")
PAGE_LIMIT = T("core.restart.page_limit", "no longer than {minutes} min from the request")
NO_CEILING = T("core.restart.no_ceiling", "no ceiling (restart_wait_minutes 0)")
WAITS_NO_CEILING = T("core.restart.waits_no_ceiling", "waits without a ceiling (restart_wait_minutes 0)")
CEILING_PASSED = T("core.restart.ceiling_passed", "the ceiling has passed — a restart without them (they get \"continue\")")
WAITS_MORE = T("core.restart.waits_more", "waits for them up to {minutes} min more, then a restart without them (they get "
               "\"continue\")")
HUB_WAITS_PAGE = T("core.restart.hub_waits_page", "the hub restart waits for a recording on the page (asked by: {by}): {what}")
THEN = T("core.restart.then", "then also {held}")
HUB_WAITS = T("core.restart.hub_waits", "the hub restart waits (asked by: {by}): {held}")
NOW = T("core.restart.now", "now")
PARTS_WAIT_PAGE = T("core.restart.parts_wait_page", "the restart of {parts} waits for a recording on the page: {what}")
HUB_WAITS_PAGE_LOG = "the hub restart waits for a recording on the page: {what}"   # a hubd log line


def by_text(by):
    """Who asked, for a text: the owner's identifier (OWNER_BY) as a phrase, a session by its name."""
    return WHO_OWNER if by == OWNER_BY else by


def _commas(items):
    return [x for i, item in enumerate(items) for x in ([", "] if i else []) + [item]]


def _semis(items):
    return [x for i, item in enumerate(items) for x in (["; "] if i else []) + [item]]


def apply(st, ev, data, ctx):
    kind, ts = ev["type"], ev["ts"]
    if kind == "hub.restart_requested":
        parts = list(data.get("parts") or [])
        if "hubd" in parts:
            raise Rejected(T("core.restart.hubd_alone", "hubd is not restarted separately: hub restart without parts is the "
                             "whole hub"))
        bad = [p for p in parts if p not in PARTS and not str(p).startswith(("ext:", "mod:"))]
        if bad:
            raise Rejected(T("core.restart.no_part", "no such part: {bad}; there are {parts}").format(bad=", ".join(bad),
                                                                                            parts=", ".join(PARTS)))
        for p in parts:
            why = modrun.restartable(st, p[4:]) if str(p).startswith("mod:") else None
            if why:
                raise Rejected(why)
        # a module that did not come up (blocked) gets a new attempt: no process, hub-up brings it up by the want file
        parts = [p for p in parts if not (str(p).startswith("mod:") and modrun.retry(st, p[4:]))]
        if data.get("parts") and not parts:
            return
        if "scheduler" in parts:
            outlives(st, data)
        if parts:
            if not st.get("restart_parts"):
                st["restart_parts"] = []
            st.setdefault("restart_parts_since", ts)   # the page's wait period counts from the first request
            st["restart_parts"] += [p for p in parts if p not in st["restart_parts"]]
            if data.get("now"):   # `--now` is for the named parts only: another's hurry does not release a waiting page
                now = hurried(st)
                st["restart_parts_now"] = now + [p for p in parts if touches_page(p) and p not in now]
            return
        if guards.from_owner_or_hub(st, data):
            raise Rejected(T("core.restart.whole_hub", "the whole hub is restarted by the owner or the hub session: ask the hub "
                             "(hub send) or name the parts"))
        if data.get("heal") and heal(st, ts) != data["heal"]:   # sat in the mailbox (core is new, restart waits or runs)
            raise Rejected(T("core.restart.heal_needless", "a restart after the update is not needed: the core is on its code, a "
                             "restart already waits or is under way"))
        outlives(st, data)
        by = data.get("sender") if data.get("channel") == "session" else OWNER_BY
        was = st.get("restart") or {}
        if was.get("state") == "waiting":   # asked again while waiting: new who and why, period from the first request
            ts, now = was["since"], was.get("now") or bool(data.get("now"))
        else:
            now = bool(data.get("now"))
        asked = (was.get("asked") or []) + asker(st, data, ev["ts"])   # and while the previous restart has not come up
        # quiet: nobody waits for the report (the hub acts on its own); the owner or a session that asked keeps the usual message
        quiet = by != OWNER_BY and not asked and not awaited(st, by) and (not was or bool(was.get("quiet")))
        st["restart"] = {"state": "waiting", "since": ts, "by": by, "why": data.get("why"), "now": now,
                         **({"asked": asked} if asked else {}), **({"quiet": True} if quiet else {})}
        if data.get("heal"):
            st["restart_heal"] = str(data["heal"])
        return
    if kind == "page.held":
        return held(st, ev, data)
    if kind == "hub.restart_cancelled":
        if (st.get("restart") or {}).get("state") != "waiting":
            raise Rejected(T("core.restart.not_requested", "no restart was requested"))
        del st["restart"]
        to = behind(st)
        if to:   # the owner cancelled the post-update restart: the page does not ask for it again (heal)
            st["restart_heal"] = to
        return
    if kind == "hub.restart_started":  # hubd put the file for hub-up
        if data.get("parts"):
            st["restart_parts"] = [p for p in st.get("restart_parts") or [] if p not in data["parts"]]
            st["restart_parts_now"] = [p for p in hurried(st) if p not in data["parts"]]
            if not st["restart_parts"]:
                for k in ("restart_parts", "restart_parts_since", "restart_parts_now"):
                    st.pop(k, None)
        elif st.get("restart"):
            st["restart"].update(state="started", since=ts,
                                 **(coretext.put({}, "cut", coretext.got(data, "cut")) if data.get("cut") else {}),
                                 **(coretext.put({}, "page", coretext.got(data, "page")) if data.get("page") else {}))
        return
    raise Rejected("unknown restart event")


def outlives(st, data):
    """The restart is asked by the command of a schedule job that is running: it takes the schedule clock down with it,
    and the code of the command is never reported (the next clock says schedule.ran "unknown"). Marked: the restart the
    command asked for is its success (core/schedule.py, ran)."""
    job = (st.get("schedule") or {}).get(data.get("schedule_job")) or {}
    if job.get("command") and job.get("started_slot") == job.get("run_slot") != job.get("ran_slot"):
        job["restart_slot"] = job["run_slot"]


HEAL_AFTER = 120   # the page compares the core with the code no earlier than (s) after its own start: an ordinary
                   # restart of the whole hub will finish by then
HEAL_EVERY = 60
HEAL_WHY = T("core.restart.heal_why", "the core stayed on the previous code after the update — the page asked by itself")


def heal(st, now):
    """The page asks for a restart of the whole hub (the heal field of the request) -> the update commit or None: the
    core is on the code before it (behind), this version was not asked for and the restart was not cancelled
    (restart_heal), and a restart of the whole hub is neither waiting nor under way (one started more recently than
    HEAL_AFTER — hub-up is still bringing the core up; older — lost: the previous hubd overwrote the request)."""
    to, r = behind(st), st.get("restart") or {}
    if not to or st.get("restart_heal") == to or r.get("state") == "waiting":
        return None
    if r.get("state") == "started" and now - r["since"] < HEAL_AFTER:   # the clock went back: wait, do not ask
        return None
    return to


def behind(st, head=None):
    """The core is on the old code after an update: the last applied update was recorded later than the core came up
    (started), and its code is on disk (head, if known). -> the update commit or None.
    This happens on a move to a version that fixed the race of restart requests (26.10): the update is still driven by
    the previous hubd, and on some hubs only the page restarted. The sign is derived from facts the previous core writes
    too; a hub without updates (from a development folder) and a rolled-back update (its code is no longer on disk)
    are not "behind"."""
    ups = [u for u in st.get("updates") or [] if u.get("ok") and u.get("to")]
    if not ups or st.get("started") is None:
        return None
    # it came up after the update — by time or by event number: the clock may step back, and the bring-up number
    # folded by the previous core may lag behind (the previous reducer does not set it)
    seq = ups[-1].get("seq")
    if st["started"] >= ups[-1]["ts"] or (seq and (st.get("started_seq") or 0) >= seq):
        return None
    return ups[-1]["to"] if head is None or head == ups[-1]["to"] else None


def asker(st, data, now):
    """Who asked the hub to restart the whole hub: the hub session asks during a turn on another session's message —
    that one waits for the bring-up, its turn ended by itself, and it will get no CUT_TURN. [{"session", "turn",
    "waits"}]: turn is the turn in which it wrote to the hub; waits is whether it is already waiting now (the hub's
    reply "restarting" will clear its mark, while the restart waits up to the ceiling)."""
    hub = st["sessions"].get(data.get("sender") or "") if data.get("channel") == "session" else None
    if not hub or hub.get("kind") != "hub" or hub["state"] not in ("busy", "dialog"):
        return []
    # only the message that started the hub's turn: those taken in the same turn (turn_also) did not ask for a restart,
    # nor did a notice inserted mid-turn (turn_head stays the turn's start)
    mid = hub.get("turn_head", hub.get("turn_marker"))
    frm = ((st["messages"].get(mid or "") or {}).get("from")) or {}
    who = st["sessions"].get(frm.get("name") or "") if frm.get("kind") == "session" else None
    if who and who["name"] != hub["name"] and (who.get("turn_seq") or 0) < _seq(mid):
        from core import executors
        waits = {"waits": True} if executors.awaits(st, who["name"], who, now) else {}
        return [{"session": who["name"], "turn": who.get("turn_seq"), **waits}]
    return []


def awaited(st, name):
    """Someone may wait for the report of a restart that session `name` asks for: its turn is in the owner's chain (turn_root:
    his message, or a later turn of it after `hub wait`, a background command — not only a turn on his own message) or took a
    message of another session (its head, marker or turn_also: asker() looks at the head only). The marks of the last turn stay on a
    session after it: a free one waits for nobody — unless a background command of its turn is still running (its shell may
    call `hub restart` itself: the owner still waits for the report), so the marks are read from the session, not by
    chain.turn_root (busy only)."""
    sess = st["sessions"].get(name) or {}
    if sess.get("state") not in chain.BUSY and not sess.get("background"):
        return False
    if sess.get("turn_root") or sess.get("owner_chain") or sess.get("turn_owner"):
        return True
    # what the turn began with (turn_head: a notice inserted mid-turn replaces turn_marker, not it) and what came since
    for mid in [sess.get("turn_head"), sess.get("turn_marker"), *(sess.get("turn_also") or [])]:
        frm = (st["messages"].get(mid or "") or {}).get("from") or {}
        if frm.get("kind") == "session" and frm.get("name") != name:
            return True
    return False


def wake(st):
    """Whom to tell to continue after the bring-up (core.started): the asker, if nothing has started since its turn, it
    is free and waiting — a `hub waiting` mark at the time of the request or an open assignment without a result now
    (one that handed in a report and waits for nothing is not woken: "continue" would cost a turn of "nothing to
    continue")."""
    out = []
    for a in (st.get("restart") or {}).get("asked") or []:
        sess = st["sessions"].get(a["session"])
        if (sess and sess.get("turn_seq") == a["turn"] and sess["state"] == "idle" and sess.get("kind") != "hub"
                and not sess.get("background") and sess["name"] not in out
                and (a.get("waits") or any(d["session"] == sess["name"] and d["state"] in ("working", "waiting-owner")
                                           for d in st["delegations"].values()))):
            out.append(sess["name"])
    return out


def _seq(mid):
    """The number of the event with which the core created the message (m<seq>, m<seq>x<n>); anything else — 0."""
    m = re.match(r"m(\d+)", mid or "")
    return int(m.group(1)) if m else 0


def ext_parts(parts, exts, live=()):
    """(parts, unknown): a plugin is named case-insensitively (`ext:Alice:x` — the settings key is `alice:x`, the
    account in lowercase) and is brought to the key from the settings; a plugin absent from the settings is unknown,
    unless its process is alive (live: the `ext:<key>` names hub-up runs): hub-up stops it and does not start it.
    exts is the extensions section of hub.json; None (settings cannot be read) — the parts as is."""
    if exts is None:
        return list(parts), []
    known = {n.lower(): n for n in live if n.startswith("ext:")}
    known.update({f"ext:{k}".lower(): f"ext:{k}" for k in exts})   # the settings win over a stale process
    out = [known.get(p.lower(), p) if p.startswith("ext:") and p[4:] not in exts and p not in live else p
           for p in parts]
    return out, [p for p in out if p.startswith("ext:") and p not in known.values() and p[4:] not in exts]


def started(st, ev, ctx):
    """core.started: the core came up. If it was a requested restart, tell the hub; it reports to the owner."""
    r = st.pop("restart", None)
    for k in ("restart_parts", "restart_parts_since", "restart_parts_now"):
        st.pop(k, None)
    if r and r["state"] == "started":
        # the cut sessions and the page's holds: the English with the marks of its phrases (hub.restart_started)
        cut = "; " + CUT.format(cut=coretext.join(*_semis(coretext.got(r, "cut")))) if r.get("cut") else ""
        if r.get("page"):   # nobody can continue a draft on the page: say it as is
            cut += "; " + PAGE_CUT.format(page=coretext.got(r, "page"))
        asked = (ASKED_WHY.format(by=by_text(r["by"]), why=r["why"]) if r.get("why") else
                 ASKED.format(by=by_text(r["by"])))
        ctx["notify"](ev, RESTARTED_NOTE.format(asked=asked, cut=cut), fyi=bool(r.get("quiet")) and not cut)


WAIT_MINUTES = 20   # the whole hub waits for busy sessions at most this long (hub.json restart_wait_minutes, 0 — none)
WAIT_MAX = 7 * 24 * 60
# a background command that runs longer than this and is silent longer than SERVICE_QUIET is a service (a server, a
# console), not work: restart skips it. Claude Code's own longest background term (`timeout` of run_in_background,
# 2 h in 2.1.309): a quiet wait or run under it is work (11.11: at 30 min `until …; sleep` waits were cut as services)
SERVICE_AFTER = 2 * 60 * 60
SERVICE_QUIET = 10 * 60


def service(b, now):
    """A background service command (the hub 03.10: "an eternal background — a console on :7070 — holds hub restart
    forever"): it has run longer than SERVICE_AFTER and printed nothing for SERVICE_QUIET. An agent is always work. After the
    bring-up the core tells the session to continue the cut background (cut_turn) — it will start the service again."""
    if now is None or b.get("kind") == "agent" or b.get("since") is None:
        return False
    return now - b["since"] >= SERVICE_AFTER and now - (b.get("out_at") or b["since"]) >= SERVICE_QUIET


def working(sess, now):
    """It runs work in the background: a command or an agent; a service such as a dev server is not work (a restart
    does not wait for it, a gist about the present does not rest on it)."""
    return any(not service(b, now) for b in (sess.get("background") or {}).values())


def blockers(st, now=None):
    """Who blocks a restart of the whole hub: sessions in a turn and with a background (except services — now is
    needed). `--now` — nobody."""
    if (st.get("restart") or {}).get("now"):
        return []
    out = []
    for s in st["sessions"].values():
        if s["state"] == "starting" and s.get("restarts") and s.get("kind") == "hub":
            # a hub session crashes and comes up again without the MAX_RESTARTS limit: no turn, nothing to wait for
            # (14.10: an eternal "starting" of the hub session held the restart after a self-update)
            continue
        if s["state"] == "starting" and (st.get("claude_missing") or st.get("login_missing")) \
                and (s.get("spawned") or 0) < s["since"]:
            continue   # its spawn waits for the Claude Code program or its sign-in (daemon.wait_claude, wait_login): no
            # process, nothing to wait for
            # (one spawned in this «starting» — host.done spawn sets spawned — is really starting and holds it, as before)
        if s["state"] in WAITING:
            out.append(coretext.join(s["name"], ": ", LABEL[s["state"]]))
        elif s["state"] in ("idle", "held"):
            work = [b for b in (s.get("background") or {}).values() if not service(b, now)]
            if work:
                out.append(IN_BACKGROUND.format(name=s["name"], work=", ".join(b.get("title") or b["id"] for b in work)))
    return out


def ceiling(cfg):
    """The wait ceiling from hub.json (restart_wait_minutes), in minutes; not a number or below zero — the default,
    above a week — a week (the core computes with it on every tick — it must not fail on overflow)."""
    v = (cfg or {}).get("restart_wait_minutes", WAIT_MINUTES)
    ok = isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and v >= 0   # v == v: not NaN
    return min(v, WAIT_MAX) if ok else WAIT_MINUTES


def left(st, now, minutes=WAIT_MINUTES):
    """How many seconds a waiting restart will still wait for busy sessions (ceiling minutes from the first request);
    None — no ceiling."""
    r = st.get("restart") or {}
    if r.get("state") != "waiting" or not minutes or now is None:
        return None
    return max(0, round(r["since"] + minutes * 60 - now))


HOLD_BEAT = 30       # while it holds, a tab repeats page.held this often (js/api.js)
HOLD_TTL = 150       # a hold lives this many seconds after the tab's last word: if it crashed, it releases (a closed
                     # one releases itself, pagehide); with a margin: Chrome wakes a hidden tab's timers once a minute
HOLD_MAX = 8         # holds of one tab — the excess is dropped (also by the page server, /api/hold)
HOLD_NAME = 40       # length of a hold name
HOLD_TAB = 64        # length of a tab id
HOLD_LABEL = {"voice": T("core.restart.hold_voice", "a voice message"), "plugin": T("core.restart.hold_plugin", "a plugin's draft")}
PAGE_MINUTES = 120   # the page holds a restart at most this long from the request: a recording lasts as long as the owner
                     # talks, the 20 min of busy sessions cut it (05.11); a tab that forgot its mark does not stop updates


def page_limit(minutes):
    """Minutes the page holds a restart: its own ceiling or the sessions' one, whichever is longer; 0 — no ceiling."""
    return max(minutes, PAGE_MINUTES) if minutes else 0


def held(st, ev, data):
    """page.held {tab, holds}: what the tab holds (empty — released). Only the page writes it."""
    if data.get("channel") != "web":
        raise Rejected(T("core.restart.page_only", "only the page reports what the page holds"))
    tab, ts = str(data.get("tab") or "")[:HOLD_TAB], ev["ts"]
    if not tab:
        raise Rejected(T("core.restart.no_tab", "no tab"))
    tabs = {k: v for k, v in (st.get("page_holds") or {}).items() if v["until"] > ts}   # expired ones go
    holds = list(dict.fromkeys(str(h)[:HOLD_NAME] for h in data.get("holds") or [] if h))[:HOLD_MAX]
    if holds:
        tabs[tab] = {"holds": holds, "until": ts + HOLD_TTL}
    else:
        tabs.pop(tab, None)
    if tabs:
        st["page_holds"] = tabs
    else:
        st.pop("page_holds", None)


def page(st, now):
    """What the page holds now (tab holds no older than HOLD_TTL); without now — nothing."""
    if now is None:
        return []
    out = []
    for h in (st.get("page_holds") or {}).values():
        if h["until"] > now:
            out += [x for x in h["holds"] if x not in out]
    return out


def page_label(holds):
    return NOT_DELIVERED.format(what=coretext.join(*_commas([HOLD_LABEL.get(h, h) for h in holds])))


def page_waits(st, now, since, hurry, minutes=WAIT_MINUTES):
    """Wait for the page: it holds, it is not `--now` and its ceiling from the request has not passed (page_limit)."""
    limit = page_limit(minutes)
    return (not hurry and since is not None and bool(page(st, now))   # no period — a request of an old core: no wait
            and (not limit or now - since < limit * 60))


def page_ceiling(minutes):
    limit = page_limit(minutes)
    return PAGE_LIMIT.format(minutes=limit) if limit else NO_CEILING


def hurried(st):
    """Page parts named with `--now`: they do not wait for it (a list; anything else — none)."""
    v = st.get("restart_parts_now")
    return list(v) if isinstance(v, list) else []


def touches_page(part):
    """A part whose restart makes a tab lose what it has not delivered: the page itself and plugin processes."""
    return part == "web" or str(part).startswith("ext:")


def due(st, now=None, minutes=WAIT_MINUTES):
    """What hubd must ask hub-up for now: {"parts": [...]} (empty — the whole hub) or None. The whole hub waits for the
    page (page_waits), then for busy sessions up to the ceiling (left), then goes ahead without them: "cut" is whom it
    will cut (told to the hub after the bring-up). Page parts wait for it, the others go at once."""
    r = st.get("restart")
    if r and r["state"] == "waiting" and not page_waits(st, now, r["since"], r.get("now"), minutes):
        held, holds = blockers(st, now), [] if r.get("now") else page(st, now)
        # the ceiling passed, but the page holds; the texts keep the marks of their phrases (page_l10n, cut_l10n): the
        # note after the bring-up reads in the owner's language
        gone = coretext.put({}, "page", page_label(holds)) if holds else {}
        if not held:
            return {"parts": [], **gone}
        if left(st, now, minutes) == 0:
            return {"parts": [], **coretext.put({}, "cut", held), **gone}
    parts = st.get("restart_parts") or []
    if parts and page_waits(st, now, st.get("restart_parts_since"), False, minutes):
        parts = [p for p in parts if not touches_page(p) or p in hurried(st)]
    return {"parts": list(parts)} if parts else None


def note(st, now, minutes=WAIT_MINUTES):
    """A `hub status` line about a waiting restart: who holds it and how long it will still wait up to the ceiling — in
    the language of the process that prints it (a session reads English, the owner's terminal his language)."""
    r, held, wait = st["restart"], blockers(st, now), left(st, now, minutes)
    rest = "" if not held else "; " + (
        WAITS_NO_CEILING if wait is None else CEILING_PASSED if wait == 0 else WAITS_MORE.format(minutes=(wait + 59) // 60))
    by = by_text(r["by"])
    if page_waits(st, now, r["since"], r.get("now"), minutes):   # the sessions' ceiling may pass while the page holds
        line = coretext.join(HUB_WAITS_PAGE.format(by=by, what=page_label(page(st, now))), "; ", page_ceiling(minutes),
                             *(["; ", THEN.format(held=coretext.join(*_semis(held))), rest] if held else []))
    else:
        line = coretext.join(HUB_WAITS.format(by=by, held=coretext.join(*_semis(held)) if held else NOW), rest)
    return coretext.human(line)


def parts_note(st, now, minutes=WAIT_MINUTES):
    """A `hub status` line: page parts wait while it holds; None — they do not wait. In the process language (note)."""
    line = _parts_note(st, now, minutes)
    return None if line is None else coretext.human(line)


def _parts_note(st, now, minutes):
    parts = [p for p in st.get("restart_parts") or [] if touches_page(p) and p not in hurried(st)]
    if not parts or not page_waits(st, now, st.get("restart_parts_since"), False, minutes):
        return None
    return coretext.join(PARTS_WAIT_PAGE.format(parts=", ".join(parts), what=page_label(page(st, now))), "; ",
                         page_ceiling(minutes))


def page_note(st, now, minutes=WAIT_MINUTES):
    """A hubd log line (English): a restart (whole or page parts) waits for the page; None — it does not wait."""
    r = st.get("restart") or {}
    if r.get("state") == "waiting" and page_waits(st, now, r["since"], r.get("now"), minutes):
        return HUB_WAITS_PAGE_LOG.format(what=page_label(page(st, now)))
    return _parts_note(st, now, minutes)
