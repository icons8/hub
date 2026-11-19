"""MCP watchdog for live sessions: MCP access is restored promptly. Claude Code reports server status on the
`mcp_status` control request without a model turn; the core asks for it once per EVERY and right after an mcp__…
tool error. A failed server (failed) gets `mcp_reconnect` in place, without restarting the session and in the middle of
a turn, no more often than RETRY. If that fails — tell the hub: once from each session until the server comes up
there, and one message per (server, reason) per DAY for all sessions at once (a server unreachable from this machine
by construction — a desktop app's localhost — would otherwise produce a message for every new session). A transient
reason (a reset connection, a timeout, a 5xx of the service) is first retried quietly after LADDER seconds — a short
network drop heals itself and nobody hears of it; only a server still down after the ladder is told. A new reason
from the next session, or the server coming up (connected and failed in no live session; on a host without polling —
tmux — "failed" stays until restart, then we wait DAY) — tell again.
Sessions are told only if the session itself stumbled on the server:
a quiet repair wakes nobody. Waiting for sign-in (needs-auth) cannot be fixed by reconnecting: tell the hub, as at start."""

import re

from core import coretext, forms
from core.coretext import T
from core.machines import LIVE

EVERY = 5 * 60      # how often to ask a live session for status
RETRY = 10 * 60     # reconnect one server no more often than this
HIT = 30 * 60       # a session's tool error counts as "stumbled" for this long
DAY = 24 * 3600    # the same "failed" for a server goes to the hub no more often than this
ASK_GAP = 60        # after a tool error — status right away, but no more often than this
ASKS = ("idle", "busy", "dialog")
LADDER = (30, 60, 120, 300, 600)   # quiet retries after a transient failure, seconds; then the hub is told and RETRY applies
TRANSIENT = re.compile(r"ECONNRESET|ETIMEDOUT|ECONNREFUSED|EPIPE|EAI_AGAIN|ENOTFOUND|socket hang up|Connection closed|"
                       r"fetch failed|timed? ?out|\b5\d\d\b|Internal Server Error|Bad Gateway|Service Unavailable", re.I)


class Refused(ValueError):
    """Why the core does not do a request (the reducer turns it into a refusal): str() gives the reason itself, with the
    marks of the core's phrases (core/coretext.py: the owner reads it in his language)."""
    def __str__(self):
        return self.args[0] if len(self.args) == 1 and isinstance(self.args[0], str) else super().__str__()


# the core's words to sessions and the hub (core/coretext.py)
RECONNECTED = T("core.mcpwatch.reconnected", "MCP {server} dropped and is connected again. If something did not work "
                "because of it — repeat it now.")
DOWN_HUB = T("core.mcpwatch.down_hub", "MCP {server} dropped, reconnecting it in place did not work ({why}); it is not "
             "up for {names}{more}. The core tries again every {retry} min; about other sessions with the same reason — "
             "not sooner than in {hours} h, if it does not come up. The service is down or unreachable from this machine "
             "— tell the owner; the matter is in one session — hub session reload <name> (a restart with the same "
             "conversation when it is free).")
AND_MORE = T("core.mcpwatch.and_more", "and {n} more")
DOWN_SESSION = T("core.mcpwatch.down_session", "MCP {server} dropped, reconnecting it has not worked yet — the hub knows. "
                 "Work without it or wait; do not repeat the calls in a loop.")
AUTH_CONFIRM = T("core.mcpwatch.auth_confirm", "MCP {server} waits for a sign-in (first noticed by {name}). A service from "
                 "the mcp_login_confirm list (money or production): hub mcp login will refuse you — only the owner signs "
                 "in, with the \"Sign in\" button in the \"MCP waits for a sign-in\" band on the page. If it cannot be "
                 "done without it — tell him; do not sign in yourself.")
AUTH = T("core.mcpwatch.auth", "MCP {server} waits for a sign-in (first noticed by {name}). A sign-in is one per server, "
         "not per session: hub mcp login {name} {server}, once; after the sign-in the core itself reconnects the server in "
         "all sessions where it waited. Do not sign in in a loop yourself: every sign-in is a tab in the owner's browser; "
         "only at their request. A service about money or production (live Stripe) — only on their word.")
FP_FIELD = T("core.mcpwatch.fp_field", "fp is the fingerprint of the server's address from mcp_waits")
FP_WHICH = T("core.mcpwatch.fp_which", "MCP {server} waits for a sign-in at {count} addresses — which one: fp from mcp_waits")
NOT_WAITING = T("core.mcpwatch.not_waiting", "MCP {server} does not wait for a sign-in")


def server_tool(tools, server):
    """The catalog id of the MCP entry that a session knows as `server` (its `as`, else its id); None if it is not one."""
    for tid, t in tools.items():
        if isinstance(t, dict) and t.get("kind") == "mcp" and (t.get("as") or tid) == server:
            return tid
    return None


def _tool_name(server):
    """Server name as in its tool names (mcp__<name>__…): plugin:a:b → plugin_a_b."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(server))


def due(st, asked, now):
    """Which live sessions with MCP are due for a status request; asked — {session: when asked} (kept by the daemon).
    A quiet retry of the ladder (fix[server]["next"]) makes the session due at that time, once."""
    def retry(s, at):
        return any(at < f["next"] <= now for f in ((s.get("mcp_watch") or {}).get("fix") or {}).values()
                   if f.get("next") is not None)
    return [n for n, s in st["sessions"].items()
            if s["state"] in ASKS and (s.get("info") or {}).get("mcp")
            and (now - asked.get(n, 0) >= EVERY or retry(s, asked.get(n, 0)))]


def tool_error(st, ev, data, actions):
    """An mcp__<server>__… tool returned an error — ask for status now, do not wait for EVERY."""
    sess = st["sessions"].get(ev.get("entity"))
    if not sess or not data.get("error") or not data.get("mcp"):
        return
    watch = sess.setdefault("mcp_watch", {})
    watch.setdefault("hit", {})[data["mcp"]] = ev["ts"]
    if ev["ts"] - watch.get("asked", 0) >= ASK_GAP:
        watch["asked"] = ev["ts"]
        actions.append({"do": "mcp_status", "session": sess["name"]})


def touched(st, ev, data):
    """The session touched a failed server — an mcp__<server>__… call, a tool search (ToolSearch), the server name in a
    message, error or result: after the repair it will be told "repeat" (reconnected). A tool's own error is tool_error."""
    sess = st["sessions"].get(ev.get("entity"))
    failed = ((sess or {}).get("info") or {}).get("mcp_failed")
    if not failed:
        return
    if ev["type"] == "turn.tool":
        name = str(data.get("name") or "")
        text = str(data.get("input") or "") if name == "ToolSearch" else ""
    else:
        name, text = "", str(data.get("text") or "")
    hit = sess.setdefault("mcp_watch", {}).setdefault("hit", {})
    for server in failed:
        tool = f"mcp__{_tool_name(server)}__"
        if name.startswith(tool) or tool in text or re.search(rf"(?<![\w-]){re.escape(server)}(?![\w-])", text, re.I):
            hit[_tool_name(server)] = ev["ts"]


def status(st, ev, data, actions, ctx):
    sess = st["sessions"].get(ev.get("entity"))
    if not sess:
        return
    servers = data.get("servers") or {}
    info = sess.setdefault("info", {})
    info["mcp_failed"] = sorted(n for n, s in servers.items() if s == "failed")
    info["mcp_auth"] = sorted(n for n, s in servers.items() if s == "needs-auth")
    watch = sess.setdefault("mcp_watch", {})
    fix = watch.setdefault("fix", {})
    for server, state in servers.items():
        if state == "connected":
            fix.pop(server, None)          # it came up — the next drop will be reported to the hub again
            came_up(st, server)
        elif state == "failed":
            f = fix.setdefault(server, {})
            if f.get("unsupported"):
                continue                   # cannot reconnect in place: a restart already happened — no loop, until it connects
            if f.get("next") is not None:  # a quiet retry of the ladder: at its time, not at RETRY
                if ev["ts"] >= f["next"]:
                    f.pop("next")
                    f["tried"] = ev["ts"]
                    actions.append({"do": "mcp_reconnect", "session": sess["name"], "server": server})
            elif ev["ts"] - f.get("tried", -RETRY) >= RETRY:
                f["tried"] = ev["ts"]
                actions.append({"do": "mcp_reconnect", "session": sess["name"], "server": server})
    auth_notice(st, ev, sess, info["mcp_auth"], ctx["notify"])


def unsupported(sess, server=None):
    """Cannot reconnect in place (a host without mcp_reconnect — tmux, or a Claude Code that does not know the request):
    restart the process with the same conversation when idle — as before the watchdog: once, until the server connects (status).
    in_place: False — for the session, not the server, until a reconnect goes through: hub mcp reconnect does not
    restart it by presses (the per-server mark goes once the server connects)."""
    sess.setdefault("mcp_watch", {})["in_place"] = False
    if server:
        sess["mcp_watch"].setdefault("fix", {}).setdefault(server, {})["unsupported"] = True
    if sess["state"] in LIVE:
        sess["dirty"] = True


def reconnected(st, ev, data, ctx):
    sess = st["sessions"].get(ev.get("entity"))
    if not sess:
        return
    server, name = data.get("server"), sess["name"]
    if data.get("unsupported"):   # this Claude Code cannot reconnect in place — the old path, no notices and no repeats
        return unsupported(sess, server)
    watch = sess.setdefault("mcp_watch", {})
    hit = ev["ts"] - (watch.get("hit") or {}).get(_tool_name(server), -HIT) < HIT
    info = sess.setdefault("info", {})
    if data.get("ok"):
        info["mcp_failed"] = [m for m in info.get("mcp_failed") or [] if m != server]
        info["mcp_auth"] = [m for m in info.get("mcp_auth") or [] if m != server]   # after sign-in — connected
        report(st, ev, sess, (), [server])
        watch.pop("in_place", None)
        (watch.get("fix") or {}).pop(server, None)
        came_up(st, server)
        if hit:
            (watch.get("hit") or {}).pop(_tool_name(server), None)
            ctx["new_message"](st, f"m{ev['seq']}", name, RECONNECTED.format(server=server), ev["ts"],
                               origin=forms.ORIGIN_CORE)
        return
    f = watch.setdefault("fix", {}).setdefault(server, {})
    if f.get("told") or server in (info.get("mcp_auth") or []):
        return   # still waits for a login (a reconnect on request): it did not drop, the hub was told how to log in
    step = f.get("step", 0)
    if step < len(LADDER) and TRANSIENT.search(data.get("error") or ""):
        f["step"], f["next"] = step + 1, ev["ts"] + LADDER[step]   # a network blip: retry quietly, tell nobody yet
        return
    f["told"] = ev["ts"]
    why = (data.get("error") or "")[:200]
    down = st.setdefault("mcp_down_told", {})
    key = re.sub(r"\d+", "#", why)        # the reason without numbers: a port or request number does not make it a new one
    if ev["ts"] - (down.get(server) or {}).get(key, -DAY) >= DAY:
        for m in list(down):               # a day has passed — forget (the server may have left the settings for good)
            down[m] = {k: t for k, t in down[m].items() if ev["ts"] - t < DAY}
            if not down[m]:
                del down[m]
        down.setdefault(server, {})[key] = ev["ts"]
        names = sorted({name, *failed_at(st, server)})
        more = " " + AND_MORE.format(n=len(names) - 10) if len(names) > 10 else ""
        ctx["notify"](ev, DOWN_HUB.format(server=server, why=why, names=", ".join(names[:10]), more=more,
                                          retry=RETRY // 60, hours=DAY // 3600))
    if hit:
        ctx["new_message"](st, f"m{ev['seq']}s", name, DOWN_SESSION.format(server=server), ev["ts"],
                           origin=forms.ORIGIN_CORE)


def came_up(st, server):
    """The server is connected in a session: if it is failed in no live one — it came up, the next drop goes to the hub
    again, even with the same reason and within the same day. Connected in some and failing in others — the problem is
    in the sessions, not the server: stay quiet until DAY."""
    down = st.get("mcp_down_told") or {}
    if server in down and not failed_at(st, server):
        del down[server]


def failed_at(st, server):
    """Live sessions where the server is currently failed (by the last status)."""
    return [n for n, s in st["sessions"].items()
            if s["state"] in LIVE and server in ((s.get("info") or {}).get("mcp_failed") or [])]


def auth_notice(st, ev, sess, servers, notify):
    """MCP is waiting for sign-in (OAuth): tell the hub how to sign in without an interactive session — once per server,
    not per session: Claude Code has one sign-in per server, not per session (otherwise after a hub restart the hub
    would sign in over and over for each)."""
    told = st.setdefault("mcp_auth_told", [])
    for server in servers or []:
        if server in told:
            continue
        told.append(server)
        del told[:-200]
        if server in ((st.get("owner") or {}).get("mcp_confirm") or []):   # money, production: not for a session's command
            notify({**ev, "seq": f"{ev['seq']}a{len(told)}"}, AUTH_CONFIRM.format(server=server, name=sess["name"]))
            continue
        notify({**ev, "seq": f"{ev['seq']}a{len(told)}"}, AUTH.format(server=server, name=sess["name"]))


# --- waiting for a login, per server (the snapshot's mcp_waits) --------------------------------------------------------
# A session keeps the servers it waits for in mcp_waits {name: fp} through restarts (info.mcp_auth is dropped at every
# start); the state keeps mcp_waits {name: {fp: {since, host?}}} — since when the server waits: the first report of it,
# until a login, until a session where it is connected, or until no session (not removed) names it. fp — the
# fingerprint of the server's address (the host, from mcp_status config.url): one name in `.mcp.json` of two projects is
# two servers, a login into one is not a login into the other; "" — the address is not known (init does not say it; the
# first poll does, and the session remembers it in mcp_addr {name: {fp, host}}). dismissed — the owner closed the row
# (wait_dismissed): the fp of that row, True — closed before its address was known; the wait goes on, only the page
# does not show it; the mark lives as long as the entry, so a restart
# or a repeated report of the same wait keeps it closed, while a login, a connected session or a name no session holds
# any more delete the entry, and the next wait is a new one.

def fp_of(sess, server):
    return ((sess.get("mcp_addr") or {}).get(server) or {}).get("fp") or ""


def report(st, ev, sess, waiting, connected=(), unsure=None, addr=None):
    """The session's MCP report: waiting — every server it waits a login for now, connected — the ones connected in
    it, unsure — the ones in a status that says nothing about a login (failed: a timeout while the fleet comes up,
    pending): a server it waited for keeps its wait and since; None — the report is about some servers only, the rest
    stay as they were. A server it no longer names, or disabled, no longer waits. addr — {name: {fp, host}} from the
    host (session.mcp)."""
    if isinstance(addr, dict):
        known = sess.setdefault("mcp_addr", {})
        for n, a in addr.items():
            if isinstance(n, str) and isinstance(a, dict) and isinstance(a.get("fp"), str) and a["fp"]:
                known[n] = {"fp": a["fp"][:40], "host": str(a.get("host") or "")[:200]}
    connected = [n for n in connected or () if isinstance(n, str)]
    for server in connected:
        logged_in(st, server, fp_of(sess, server))
    old = sess.get("mcp_waits") or {}
    new = {n: fp_of(sess, n) for n in waiting or () if isinstance(n, str)}
    for n in old:
        if n not in new and n not in connected and (unsure is None or n in unsure):
            new[n] = fp_of(sess, n)
    for n, fp in new.items():
        per = st.setdefault("mcp_waits", {}).setdefault(n, {})
        # the address became known: the wait does not start anew (init named it, the first poll told the address)
        was = per.get("") if old.get(n) == "" and fp else None
        if fp in per:
            if was:
                per[fp]["since"] = min(per[fp].get("since") or ev["ts"], was.get("since") or ev["ts"])
            continue
        host = (sess.get("mcp_addr") or {}).get(n, {}).get("host") if fp else None
        per[fp] = {"since": (was or {}).get("since") or ev["ts"], **({"host": host} if host else {}),
                   **({"dismissed": fp} if _closed_for(was, fp) else {})}
    if new:
        sess["mcp_waits"] = new
    else:
        sess.pop("mcp_waits", None)
    if any(new.get(n) != fp for n, fp in old.items()):
        prune(st)


def _closed_for(was, fp):
    """The owner's mark of an unknown-address wait goes over to the address fp its session learned (a new entry): when
    it was closed as a row of its own (True) or as the row of that very address — not when it joined the row of another
    one (a second server under the same name is a new wait)."""
    d = (was or {}).get("dismissed")
    return d is True or (bool(fp) and d == fp)


def prune(st):
    """A server no session (not removed) waits for any more is no longer waiting."""
    w = st.get("mcp_waits")
    if not w:
        return
    held = {(n, fp) for x in st["sessions"].values() if x["state"] != "removed"
            for n, fp in (x.get("mcp_waits") or {}).items()}
    for n in list(w):
        for fp in [fp for fp in w[n] if (n, fp) not in held]:
            del w[n][fp]
        if not w[n]:
            del w[n]


def logged_in(st, server, fp=""):
    """The server at this address is signed in (a login, or it is connected in some session): the login is one per
    server, so the sessions that wait for it — sleeping ones too — no longer do. A known address also clears the waits
    whose address is not known: most likely the same server (a sleeping session that named it before its first poll,
    every wait right after an update) — otherwise it would "wait" for days; another server under the same name, if it
    is one, is named again when its session wakes. Another known address stays, and an unknown one does not clear a
    known: the session that signed in or reported it connected is live, its first poll brings the address
    (session.mcp) and clears that one."""
    w = st.get("mcp_waits") or {}
    gone = {fp, ""} & set(w.get(server) or {})
    if not gone:
        return
    for x in st["sessions"].values():
        mine = x.get("mcp_waits")
        if mine and mine.get(server) in gone:
            del mine[server]
            if not mine:
                del x["mcp_waits"]
    for g in gone:
        del w[server][g]
    if not w[server]:
        del w[server]


def removed(st, sess):
    sess.pop("mcp_addr", None)
    if sess.pop("mcp_waits", None):
        prune(st)


def view(st):
    """The snapshot: waiting servers, the oldest first, with the sessions (not removed) that wait for each; host — where
    the server is, when the address is known (two servers under one name differ by it). Waits with an unknown address
    join the only known address of the name (as a login does, logged_in): one server is one row while the sessions
    learn its address; with two known addresses they are a row of their own. fp — the address fingerprint the page
    names in a sign-in (mcp.login_requested); confirm — a sign-in needs the owner's confirmation (hub.json
    mcp_login_confirm)."""
    confirm = set((st.get("owner") or {}).get("mcp_confirm") or [])
    holders = {}
    for x in st["sessions"].values():
        if x["state"] != "removed":
            for n, fp in (x.get("mcp_waits") or {}).items():
                holders.setdefault((n, fp), []).append(x["name"])
    out = []
    for n, per in (st.get("mcp_waits") or {}).items():
        rows = {fp: {"since": e.get("since"), "sessions": holders[(n, fp)], "host": e.get("host"),
                     "closed": bool(e.get("dismissed"))} for fp, e in per.items() if (n, fp) in holders}
        known = [fp for fp in rows if fp]
        if "" in rows and len(known) == 1:
            blind, row = rows.pop(""), rows[known[0]]
            row["since"] = min(row["since"], blind["since"])
            row["sessions"] = row["sessions"] + blind["sessions"]
        for fp, r in rows.items():
            if r["closed"]:     # the owner closed this wait (wait_dismissed): it is shown again when it starts anew
                continue
            out.append({"server": n, "since": r["since"], "sessions": sorted(set(r["sessions"])), "fp": fp,
                        **({"host": r["host"]} if r["host"] else {}), **({"confirm": True} if n in confirm else {})})
    return sorted(out, key=lambda x: (x["since"] or 0, x["server"], x.get("host") or ""))


def wait_dismissed(st, ev, data):
    """The owner closed a row of the band (mcp.wait_dismissed {server, fp?}): only the owner himself — the page, his
    terminal; not a session or a plugin. Without fp — the only address the server waits at. The unknown-address wait
    that joins the row (view) is closed with it. ValueError — why not."""
    from core import guards
    if guards.from_owner_strict(None, data) is not None:
        raise Refused(T("core.mcpwatch.dismiss_owner", "only the owner may close the \"MCP waits for a sign-in\" band"))
    server = _server(data)
    fp = data.get("fp")
    if fp is not None and (not isinstance(fp, str) or len(fp) > 40):
        raise Refused(FP_FIELD)
    per = (st.get("mcp_waits") or {}).get(server) or {}
    known = [k for k in per if k]
    if fp is None:
        if len(known) > 1:
            raise Refused(FP_WHICH.format(server=server, count=len(known)))
        fp = known[0] if known else ""
    elif fp == "" and "" not in per and len(known) == 1:   # the page showed the row before the first poll told the address
        fp = known[0]
    group = {fp} | ({""} if fp and known == [fp] else set())
    if not group & set(per):
        raise Refused(NOT_WAITING.format(server=server))
    for k in group & set(per):
        per[k]["dismissed"] = fp or True   # the address of the closed row; True — closed before its address was known


# --- reconnect on request (hub mcp reconnect) ---------------------------------------------------------------------------
GAP = 30   # one server in one session by request no more often than this: a second press does not stack requests


def _server(data):
    server = data.get("server")
    if not isinstance(server, str) or not server or len(server) > 200 or not server.isprintable():
        raise Refused(T("core.mcpwatch.server_name", "an MCP server is its name, as in hub show (one line, up to 200 "
                        "characters)"))
    return server


def _stuck(s, server):
    """Cannot reconnect in place: the session (unsupported — tmux, an old Claude Code) or this server (until it connects)."""
    w = s.get("mcp_watch") or {}
    return w.get("in_place") is False or bool(((w.get("fix") or {}).get(server) or {}).get("unsupported"))


def after_login(st, server, fp, actions, skip=None, idle_in_place=False, blind=False):
    """The server at this address is signed in (one login per server): every session that waits for it no longer does.
    A busy one reconnects it now, mid-turn (if that fails, mcpwatch.reconnected tells the hub); an idle live one restarts
    with the same conversation when free — or, idle_in_place, reconnects it in place too when it can (a login from the
    page: its Claude Code took the token, the others only need mcp_reconnect — S0 probe). skip — the session the login
    went through (already connected). Sleeping sessions only stop waiting: they read the login at their next start.
    blind — the login was at an unknown address while other addresses of the name are known: only the sessions that do
    not know the address either."""
    for x in st["sessions"].values():
        waiting = (x.get("info") or {}).get("mcp_auth") or []
        if (fp and fp_of(x, server) not in ("", fp)) or (blind and fp_of(x, server)):
            continue   # another server under the same name: this login is not its
        if server in waiting:
            if x["name"] == skip:
                pass
            elif x["state"] in ("busy", "dialog") or (idle_in_place and x["state"] == "idle" and not _stuck(x, server)):
                actions.append({"do": "mcp_reconnect", "session": x["name"], "server": server})
            elif x["state"] in LIVE:
                x["dirty"] = True
            # the login is shared per server, so "awaiting login" is cleared on sleeping sessions too (07.10)
            x["info"]["mcp_auth"] = [m for m in waiting if m != server]
    if server in (st.get("mcp_auth_told") or []):
        st["mcp_auth_told"].remove(server)   # the login went stale later: tell again
    logged_in(st, server, fp)


def reconnect_requested(st, ev, data, actions):
    """mcp_reconnect right now, past the RETRY pause: to the named session (the server is among its MCP), or to every
    live session where the server is failed or waits for a login. Not to a session asked within GAP, nor to one that
    cannot reconnect in place (unsupported: tmux, an old Claude Code — there it would be a restart of the session on
    every press). ValueError — why not. mcp_reconnect_asked {server: {seq, sessions}} — the last request, for the
    command's answer (kept for GAP)."""
    server, name = _server(data), ev.get("entity")

    def stuck(s):
        return _stuck(s, server)

    def fresh(s):
        t = ((s.get("mcp_watch") or {}).get("pressed") or {}).get(server)
        return isinstance(t, (int, float)) and 0 <= ev["ts"] - t < GAP
    if name:
        sess = st["sessions"].get(name)
        if not sess or sess["state"] == "removed":
            raise Refused(T("core.mcpwatch.no_session", "no session {name}").format(name=name))
        if sess["state"] not in ASKS:
            raise Refused(T("core.mcpwatch.not_running", "{name} is not running ({state}): nothing to reconnect — its "
                            "MCP servers connect at its start").format(name=name, state=sess["state"]))
        if server not in ((sess.get("info") or {}).get("mcp") or []):
            raise Refused(T("core.mcpwatch.no_server", "{name} has no MCP {server}").format(name=name, server=server))
        who = [sess]
    else:
        who = [s for s in st["sessions"].values() if s["state"] in ASKS and server in
               ((s.get("info") or {}).get("mcp_failed") or []) + ((s.get("info") or {}).get("mcp_auth") or [])]
        if not who:
            raise Refused(T("core.mcpwatch.nothing_failed", "MCP {server} has not failed and does not wait for a sign-in "
                            "in any running session — nothing to reconnect; for one session: hub mcp reconnect {server} "
                            "--session <name>").format(server=server))
    if all(stuck(s) for s in who):
        names = sorted(s["name"] for s in who)
        raise Refused(T("core.mcpwatch.stuck", "{names}: MCP cannot be reconnected in place (the host or the Claude Code "
                        "version); a restart with the same conversation — hub session reload <name>").format(
                            names=", ".join(names[:10])))
    who = [s for s in who if not stuck(s)]
    if all(fresh(s) for s in who):
        raise Refused(T("core.mcpwatch.pressed", "MCP {server} is already reconnecting: a repeat — not sooner than {gap} s "
                        "after the previous one").format(server=server, gap=GAP))
    who = [s for s in who if not fresh(s)]
    asked = st.setdefault("mcp_reconnect_asked", {})
    for k in [k for k, v in asked.items() if not isinstance(v.get("ts"), (int, float)) or ev["ts"] - v["ts"] >= GAP]:
        del asked[k]
    asked[server] = {"ts": ev["ts"], "seq": ev["seq"], "sessions": sorted(s["name"] for s in who)}
    for s in who:
        w = s.setdefault("mcp_watch", {})
        pressed = w.setdefault("pressed", {})
        for k in [k for k, t in pressed.items() if not isinstance(t, (int, float)) or ev["ts"] - t >= GAP]:
            del pressed[k]
        pressed[server] = ev["ts"]
        if server in ((s.get("info") or {}).get("mcp_failed") or []):
            # the automatic retry counts from this attempt: it does not repeat it a minute later
            w.setdefault("fix", {}).setdefault(server, {})["tried"] = ev["ts"]
        actions.append({"do": "mcp_reconnect", "session": s["name"], "server": server})


# --- a sign-in from the page (POST /api/mcp/login): in hubd, through a live session's control requests ------------------
# Claude Code (2.1.218+, S0 probe) signs a server in inside a live session: mcp_authenticate gives the link (its own
# listener on localhost takes the browser's return), mcp_oauth_callback_url takes the address from the browser bar when
# the browser is elsewhere (a phone) — then mcp_reconnect is needed. The end of a sign-in is announced by nothing: the
# core polls mcp_status. An attempt is a record st["mcp_logins"][id] (id l<seq of the request>): server, fp, the session
# it goes through, state asked → open → sent → done | failed. The link and the address after the sign-in never reach the
# journal (core/mcplogin: files for the owner only). No live session that can do it (all asleep, tmux, a Claude Code
# without the requests) — the old way: `claude mcp login` in a pseudo-terminal (core/mcplogin.start/finish), in hubd's
# background (pty).
LIFE = 290     # s: Claude Code keeps an attempt about 5 minutes (alive at 285 s, gone at 330 s)
POLL = 1       # s: how often hubd asks the attempt's session for the status while the attempt is open
AGAIN = 5      # s: a press within this after the previous one does not start a second attempt
KEEP = 600     # s: a finished attempt stays in the state (the page shows "signed in" / "did not work")
PENDING = 30   # s: a server still connecting at the start of an attempt is asked again for this long
ACTIVE = ("asked", "open", "sent")
# why an attempt failed: kept on the attempt (error), the page and `hub mcp login` show it
GONE = T("core.mcpwatch.gone", "the session the sign-in went through restarted or stopped — the sign-in is broken off, "
         "start again")


def confirm_list(raw, mcp=None):
    """hub.json mcp_login_confirm: the servers (by name) a sign-in to which needs the owner's confirmation (money,
    production). Anything that is not a list of names counts as empty — one name per line, as in hub show. An entry of
    the settings' `mcp` section named `as` is the server `as` to a session: listing its id covers that name too (mcp —
    that section), or the gate would not hold for the name the session reports."""
    if not isinstance(raw, list):
        return []
    from core.catalog import as_name_ok   # the catalog's own rule for `as`: the gate holds the name it connects by
    names = {s.strip() for s in raw if isinstance(s, str) and s.strip() and len(s) <= 200 and s.isprintable()}
    for n in list(names):
        entry = (mcp or {}).get(n)
        alias = entry.get("as") if isinstance(entry, dict) else None
        if as_name_ok(alias):
            names.add(alias)
    return sorted(names)


def _tidy(logins, now):
    for k in [k for k, a in logins.items() if not isinstance(a, dict) or now - (a.get("at") or 0) >= KEEP
              or (a.get("state") in ACTIVE and now - (a.get("ts") or 0) >= LIFE + KEEP)]:
        del logins[k]


def _caller(data, what):
    """Who asks for a sign-in or its address: the owner (the page, his terminal) -> True, a session (`hub mcp login`,
    anyone, as before the core did it) -> False; a plugin or module process, a module handler's mod:<name> request
    included — never (ValueError): the owner's confirmation of a server is his own word only."""
    from core import guards
    if guards.from_owner_strict(None, data) is None:
        return True
    if data.get("channel") == "session":
        return False
    raise Refused(what)


def login_requested(st, ev, data, actions):
    """The owner pressed "Sign in" or a session ran `hub mcp login` (mcp.login_requested {server, fp?, session?,
    confirmed?}): pick the session (the named one first), start an attempt. A server from the confirm list — only with
    the owner's confirmation (a session never has it). ValueError — why not."""
    owner = _caller(data, T("core.mcpwatch.caller_login", "an MCP sign-in is started by the owner or a hub session (hub "
                            "mcp login), not by a plugin or module process"))
    server = _server(data)
    hint = data.get("session")
    if hint is not None and (not isinstance(hint, str) or len(hint) > 200):
        raise Refused(T("core.mcpwatch.hint_field", "session is the name of the session to sign in through first"))
    fp = data.get("fp")
    if fp is not None and (not isinstance(fp, str) or len(fp) > 40):
        raise Refused(FP_FIELD)
    per = (st.get("mcp_waits") or {}).get(server) or {}
    known = [k for k in per if k]
    if fp is None:
        if len(known) > 1:
            raise Refused(FP_WHICH.format(server=server, count=len(known)))
        fp = known[0] if known else ""
    elif fp == "" and "" not in per and len(known) == 1:   # the page showed the row before the first poll told the address
        fp = known[0]
    group = {fp} | ({""} if fp and known == [fp] else set())   # an unknown address joins the only known (view)
    if not group & set(per):
        raise Refused(NOT_WAITING.format(server=server))
    if server in ((st.get("owner") or {}).get("mcp_confirm") or []) and not (owner and data.get("confirmed") is True):
        raise Refused(T("core.mcpwatch.needs_confirm", "a sign-in to {server} — only after the owner's confirmation "
                        "(the mcp_login_confirm list in hub.json): the \"Sign in\" button on the page").format(server=server))
    logins = st.setdefault("mcp_logins", {})
    _tidy(logins, ev["ts"])
    for k, a in list(logins.items()):
        if a.get("server") == server and a.get("fp") == fp:
            if a.get("state") in ACTIVE and 0 <= ev["ts"] - (a.get("ts") or 0) < AGAIN:
                raise Refused(T("core.mcpwatch.started", "a sign-in to {server} has already started — the link will be "
                                "there in a few seconds").format(server=server))
            del logins[k]   # one attempt per server: the new one replaces the earlier (its link is no longer valid)
    held = [x for x in st["sessions"].values() if x["state"] != "removed" and server in (x.get("mcp_waits") or {})
            and x["mcp_waits"][server] in group]
    # a live one that reports the server waiting, or restarted and has not reported yet (info.mcp_auth is dropped at
    # every start; the host asks for the status first anyway)
    ready = sorted((x for x in held if x["state"] in ASKS and not _stuck(x, server)
                    and (server in ((x.get("info") or {}).get("mcp_auth") or []) or "mcp_auth" not in (x.get("info") or {}))),
                   key=lambda x: (x["name"] != hint, server not in ((x.get("info") or {}).get("mcp_auth") or []),
                                  x["state"] != "idle", x["name"]))
    if not ready and not held:
        raise Refused(T("core.mcpwatch.no_waiter", "MCP {server} does not wait for a sign-in in any session").format(
            server=server))
    who = ready[0] if ready else sorted(held, key=lambda x: (x["state"] not in LIVE, x["name"]))[0]
    if not ready and not _pty_ok(who):
        raise Refused(T("core.mcpwatch.live_only", "MCP {server} signs in only through a live session ({name}): its "
                        "host knows this server itself, `claude mcp login` will not sign it in — wake the session and sign "
                        "in again").format(server=server, name=who["name"]))
    a = {"id": f"l{ev['seq']}", "server": server, "fp": fp, "session": who["name"], "state": "asked", "ts": ev["ts"],
         "at": ev["ts"], "by": "owner" if owner else "session", **({"host": per[fp]["host"]} if (per.get(fp) or {}).get("host") else {})}
    if not ready:
        a["pty"] = True
        # the old way keeps one waiting login per server name (core/mcplogin.work_dir): an earlier one is replaced
        for b in logins.values():
            if b.get("server") == server and b.get("pty") and b.get("state") in ACTIVE:
                _fail(b, ev, T("core.mcpwatch.replaced", "replaced by a new sign-in to the same server"))
    logins[a["id"]] = a
    _start(a, actions)


def _pty_ok(sess):
    """The old way (`claude mcp login` in a pseudo-terminal) can sign in this session's servers — unless its host said
    otherwise (session.info mcp_login_pty: False — Codex: the servers are its thread's own)."""
    info = sess.get("info") or {}
    return info.get("mcp_login_pty") is not False and info.get("host") != "codex"   # host: an info from before the flag


def _start(a, actions):
    actions.append({"do": "mcp_login_pty" if a.get("pty") else "mcp_login", "session": a["session"],
                    "server": a["server"], "attempt": a["id"]})


def _fail(a, ev, why):
    a.update(state="failed", at=ev["ts"])
    # the English and the marks of its phrases (error_l10n): the page and `hub mcp login` show it in the reader's language
    coretext.put(a, "error", why or T("core.mcpwatch.failed", "the sign-in did not go through"), limit=300)


def _done(st, ev, a, actions):
    a.update(state="done", at=ev["ts"])
    coretext.drop(a, "error")
    # the address the attempt's session learned while it went (its first poll) — that server is signed in; still unknown
    # beside two known ones (its own row): the servers at the known ones are not this login's
    fp = a["fp"] or fp_of(st["sessions"].get(a["session"]) or {}, a["server"])
    blind = not fp and len([k for k in (st.get("mcp_waits") or {}).get(a["server"]) or {} if k]) > 1
    # pty: the login went through another process — the attempt's session reconnects too
    after_login(st, a["server"], fp, actions, skip=None if a.get("pty") else a["session"], idle_in_place=True,
                blind=blind)


def _attempt(st, data):
    a = (st.get("mcp_logins") or {}).get(data.get("attempt"))
    return a if isinstance(a, dict) and a.get("state") in ACTIVE else None


def login_step(st, ev, data, actions):
    """A fact of the attempt (session.mcp_login {attempt, step, ok, error?, status?, unsupported?}) — from the host
    (control requests) or hubd itself (pty): auth — the link is ready (or why not; status — the server was not waiting:
    connected means signed in already), callback — the address after the sign-in was taken, status — polling saw it
    connected."""
    _tidy(st.get("mcp_logins") or {}, ev["ts"])
    a = _attempt(st, data)
    if not a or ev.get("src") not in ("host", "core"):
        return
    step, ok, why = data.get("step"), data.get("ok") is True, coretext.got(data, "error")
    if step == "auth":
        if ok and data.get("silent") and not a.get("pty"):   # signed in without a human: the server comes up on a reconnect
            a.update(state="sent", at=ev["ts"], opened=ev["ts"])
            actions.append({"do": "mcp_reconnect", "session": a["session"], "server": a["server"]})
        elif ok:
            a.update(state="open", at=ev["ts"], opened=ev["ts"])
            coretext.drop(a, "error")
        elif data.get("status") == "connected":
            _done(st, ev, a, actions)
        elif data.get("status") == "pending" and ev["ts"] - a["ts"] < PENDING:
            # the server is still connecting (a session just started): ask again in a second
            actions.append({"do": "mcp_login", "session": a["session"], "server": a["server"], "attempt": a["id"],
                            "delay": 1})
        elif data.get("unsupported") and not a.get("pty"):
            a.update(pty=True, at=ev["ts"])   # this Claude Code does not know the request: the old way
            _start(a, actions)
        else:
            _fail(a, ev, why or (T("core.mcpwatch.server_not_waiting", "the server does not wait for a sign-in: "
                                   "{status}").format(status=data["status"]) if data.get("status") else None))
    elif step == "callback":
        if ok and a.get("pty"):
            _done(st, ev, a, actions)
        elif ok:   # the code is exchanged, but the server stays needs-auth until reconnected (S0 probe)
            a.update(state="sent", at=ev["ts"])
            actions.append({"do": "mcp_reconnect", "session": a["session"], "server": a["server"]})
        elif a.get("pty"):
            _fail(a, ev, why)
        else:   # a wrong address (another attempt's state, no code): the sign-in itself still waits
            a.update(state="open", at=ev["ts"])
            coretext.put(a, "error", why or T("core.mcpwatch.address_refused", "the address was not accepted"), limit=300)
    elif step == "status" and data.get("status") == "connected" and a["state"] != "asked":
        _done(st, ev, a, actions)   # polling starts once the link is out: a status before it is not this sign-in


def connected(st, ev, name, server, actions):
    """The attempt's session has the server connected (a reconnect after the address went through, a status report):
    the sign-in is done, not only by polling."""
    for a in list((st.get("mcp_logins") or {}).values()):
        if a.get("state") in ACTIVE and not a.get("pty") and a.get("session") == name and a.get("server") == server \
                and a.get("state") != "asked":
            _done(st, ev, a, actions)


def login_host(st, ev, data, actions):
    """host.done / host.error of mcp_login and mcp_callback: a host without the request (tmux) or a process that left —
    the old way for the start; for the address — the attempt did not work."""
    a = _attempt(st, data)
    if not a or a.get("pty") or a.get("session") != ev.get("entity"):
        return
    if ev["type"] == "host.done" and data.get("result") == "sent":
        return
    if data.get("do") == "mcp_login" and not _pty_ok(st["sessions"].get(a["session"]) or {}):
        _fail(a, ev, GONE if data.get("error") else T("core.mcpwatch.not_started", "the session did not start the "
                                                      "sign-in: {result}").format(result=data.get("result")))
    elif data.get("do") == "mcp_login":
        a.update(pty=True, at=ev["ts"])
        _start(a, actions)
    elif data.get("do") == "mcp_callback":
        _fail(a, ev, T("core.mcpwatch.no_callback", "the address after the sign-in did not reach the session")
              if data.get("result") == "no-callback" else GONE)


def callback_given(st, ev, data, actions):
    """The owner (or a session he gave the address to: `hub mcp login --redirect`) sent the address from the browser bar
    (mcp.callback_given {attempt}); the address itself is in a file for the owner only (core/mcplogin.put_callback), hubd takes it when it acts. ValueError — why not."""
    owner = _caller(data, T("core.mcpwatch.caller_callback", "the address after a sign-in is sent by the owner or a "
                            "hub session (hub mcp login), not by a plugin or module process"))
    a = (st.get("mcp_logins") or {}).get(data.get("attempt"))
    if not isinstance(a, dict):
        raise Refused(T("core.mcpwatch.no_attempt", "no such sign-in attempt — start the sign-in again"))
    if not owner and a.get("by") != "session":   # a session does not finish the owner's sign-in (the page's, or a confirmed one)
        raise Refused(T("core.mcpwatch.owners_attempt", "the address for a sign-in the owner started is sent by the "
                        "owner"))
    if a.get("state") not in ("open", "sent") or ev["ts"] - (a.get("opened") or a.get("ts") or 0) >= LIFE:
        raise Refused(T("core.mcpwatch.attempt_over", "the sign-in to {server} no longer waits for an address — start "
                        "the sign-in again").format(server=a.get("server")))
    a.update(state="sent", at=ev["ts"])
    coretext.drop(a, "error")
    actions.append({"do": "mcp_callback", "session": a["session"], "server": a["server"], "attempt": a["id"],
                    **({"pty": True} if a.get("pty") else {})})


def login_gone(st, ev, name):
    """The session's process changed or left: a control-request attempt through it is gone with it."""
    for a in (st.get("mcp_logins") or {}).values():
        if a.get("session") == name and a.get("state") in ACTIVE and not a.get("pty"):
            _fail(a, ev, GONE)


def polling(st, now):
    """Attempts whose end hubd watches by polling the session (core/daemon.check_mcp)."""
    return [a for a in (st.get("mcp_logins") or {}).values()
            if isinstance(a, dict) and a.get("state") in ("open", "sent") and not a.get("pty")
            and 0 <= now - (a.get("ts") or 0) < LIFE]


def logins_view(st):
    """The snapshot: sign-in attempts, the latest first (the page shows the state of each server's sign-in); why one
    failed — in the reader's language (the process language: the page server's is the hub's)."""
    keys = ("id", "server", "host", "fp", "session", "state", "ts", "at", "error", "pty")
    return sorted(({k: (error_text(a) if k == "error" else a[k]) for k in keys if k in a}
                   for a in (st.get("mcp_logins") or {}).values() if isinstance(a, dict)),
                  key=lambda a: -(a.get("ts") or 0))


def error_text(a, lang=None):
    """Why an attempt failed or waits, as a human reads it: the core's phrases in lang (the process language by
    default); an earlier core's text as it is."""
    return coretext.human(coretext.got(a, "error"), lang)
