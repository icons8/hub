"""Codex: MCP of a Codex session for the codex host (hosts/codex.py) — the
same facts the core gets from the claude host (hosts/streamjson.py, core/mcpwatch): the servers' status, a reconnect,
an OAuth sign-in from the page or `hub mcp login`, and an MCP server's question to the user (elicitation).

What app-server 0.162.1 does (probe 2026-11-05, no model, servers in the thread's config as the host gives them):
- mcpServerStatus/list {threadId} — every server of the thread with runtimeStatus (connected, failed,
  authenticationRequired, notStarted, starting, cancelled, disabled; null — not known) and authStatus; without a
  threadId the list is empty. mcpServer/startupStatus/updated tells starting -> ready | failed at the start, after a
  reload and after a sign-in;
- config/mcpServer/reload — restarts every server of the thread (one by name is not in the protocol); the thread's own
  servers stay;
- mcpServer/oauth/login {name, threadId} -> authorizationUrl, loginId; the browser comes back to Codex's own listener
  on 127.0.0.1:<port>/callback/<id> (redirect_uri of the link); then mcpServer/oauthLogin/completed and the server
  restarts by itself — no reconnect is needed. On a connected server it gives a new link anyway — hence the status
  first, as with Claude; on a stdio server — -32600;
- mcpServer/elicitation/request (form | url) comes only with an approval policy other than "never": with the hub's
  bypass Codex declines it itself.

Seams the host calls (the host's methods of the same name pass through here):
- status(host, name, proc) — CodexHost.mcp_status: mcpServerStatus/list; the answer is session.mcp {servers, addr} with
  Claude's words (connected, failed, needs-auth, pending, disabled) — only when something changed or failed, as there;
- reconnect(host, name, proc, server) — config/mcpServer/reload unless the server is connected or starting already
  (after a sign-in Codex restarts it by itself); session.mcp_reconnected comes once it has started (or failed to),
  several requests share one reload; while a turn runs and another server is connected — at the turn's end
  (turn_done): a reload restarts every server and would cut that one's calls;
- login(host, name, proc, server, attempt) — the status first: needs-auth — mcpServer/oauth/login, the link goes to the
  owner's file (core/mcplogin.put_link), session.mcp_login steps as with Claude; login_poll — the status until connected;
- callback(host, name, proc, server, attempt, url) — the address from the browser bar of another device: Codex has no
  request for it, so the host opens it itself — only on 127.0.0.1 at the port and path of this attempt's redirect_uri,
  with only its code/state/error (anything else in the address is dropped: not a way to make the hub fetch an
  arbitrary URL); the step fact comes from a thread through the host's inbox;
- request / answer_request — elicitation: a form of up to MAX_FIELDS plain fields (text, number, yes/no, a choice) or
  a link — a card like AskUserQuestion; anything else is declined at once;
- info(proc) -> {"mcp": the thread's servers, "mcp_login_pty": False — core/mcpwatch has no old way for them};
  turn_done(proc, name, turn) — a deferred reload; notice(proc, name, method, params); response(proc, name, method, params,
  msg) — the host's common seams (hosts/codex.py, "Seams").
"""
import math
import threading
import time
import urllib.parse
import urllib.request

from core import coretext
from core.coretext import T

UNSUPPORTED = "MCP sign-in and status are not supported on Codex"   # kept: hosts.codex.UNSUPPORTED_MCP
# runtimeStatus of mcpServerStatus/list -> the status words of Claude Code's mcp_status (core/mcpwatch reads them)
STATUS = {"connected": "connected", "authenticationRequired": "needs-auth", "failed": "failed", "cancelled": "failed",
          "disabled": "disabled", "notStarted": "pending", "starting": "pending"}
STARTED = {"ready": True, "failed": False, "cancelled": False}   # startupStatus: the server's start is over (ok or not)
ELICIT = "mcpServer/elicitation/request"
# why a step of a sign-in or a reconnect failed: the attempt's error (the page, `hub mcp login`), a reconnect's goes to
# the hub's notice
NO_SERVER = T("core.codex_mcp.no_server", "no MCP {server}")
FOREIGN = T("core.codex_mcp.foreign_address", "the address is not from this sign-in: the address from the browser bar "
            "after the sign-in is needed")
NOT_TAKEN = T("core.codex_mcp.address_refused", "Codex did not accept the address: HTTP {code}")
NOT_WAITING = T("core.codex_mcp.not_waiting", "the sign-in no longer waits for the address ({kind})")
LOGIN_FAILED = T("core.codex_mcp.login_failed", "the sign-in did not go through")
NO_LINK = T("core.streamjson.no_login_link", "no sign-in link")   # the same as hosts/streamjson's
MAX_FIELDS = 4          # AskUserQuestion's limit: a form with more fields is declined
CALLBACK_WAIT = 10      # s: opening the address after a sign-in on Codex's own listener
STALE = 60              # s: a reconnect whose server has not started (re)starting by then is answered by the status;
                        # a reload not followed by the servers' start no longer holds the next one back
clock = time.monotonic  # the seams' clock: windows of seconds, not dates (a clock step does not stretch them)
LATER = "hub/mcpLater"  # a notification Codex never sends: a step answered by the host itself, through its inbox
# the parameters of an address after a sign-in that go on to Codex's listener (OAuth 2.0: the answer of authorize)
RETURN_KEYS = ("code", "state", "error", "error_description", "error_uri", "iss")


class _Mcp:
    """The MCP state of one session's app-server (kept on the process: CodexProcess.mcp)."""

    def __init__(self):
        self.asks = {}        # our request id -> its tag (status, pre, poll, auth, reload, callback)
        self.started = {}     # server -> the last startupStatus (starting | ready | failed | cancelled)
        self.statuses = {}    # server -> the last status word from mcpServerStatus/list
        self.seen = None      # (servers, addr) of the last session.mcp: nothing new — no fact
        self.asking = False   # a status request after the servers started is on its way
        self.reload_at = 0    # when config/mcpServer/reload went (0 — none on its way)
        self.restarting = False  # a server began starting after that reload: the end of the starts frees reload_at
        self.deferred = False  # a reload waits for the running turn's end (it would cut the other servers' calls)
        self.waiting = {}     # server -> since when a reconnect waits for its start to end (session.mcp_reconnected)
        self.signed_at = {}   # server -> when the address after a sign-in went through: Codex restarts it by itself
        self.logins = {}      # Codex loginId -> (server, attempt)
        self.redirects = {}   # attempt -> redirect_uri of its sign-in link (the callback opens only that)
        self.state_dir = None  # the hub's state folder: the sign-in link goes to the owner's file there (login sets it)


def _of(proc):
    st = getattr(proc, "mcp", None)
    if not isinstance(st, _Mcp):
        st = _Mcp()
        proc.mcp = st
    return st


def _servers(proc):
    """The thread's MCP servers as the host gave them (hosts/codex_set.thread_config): {name: config}."""
    config = ((getattr(proc, "start_params", None) or {}).get("config") or {}).get("mcp_servers")
    return config if isinstance(config, dict) else {}


def _word(row):
    return STATUS.get(row.get("runtimeStatus"), "pending")


def _addr(proc, rows):
    """{name: {fp, host}} as the claude host gives it (hosts/streamjson.mcp_addr): from the address the host configured
    (the same fingerprint as on Claude for the same server), else from Codex's httpOrigin (a plugin's server)."""
    from hosts.streamjson import mcp_addr
    configured = _servers(proc)
    out = {}
    for row in rows:
        url = (configured.get(row.get("name")) or {}).get("url") or row.get("httpOrigin")
        got = mcp_addr({"url": url}) if isinstance(url, str) else None
        if got:
            out[row["name"]] = got
    return out


def _ready(host, name, proc):
    if not host.alive(name):
        raise RuntimeError("no process")
    return bool(proc.thread)


def _call(proc, method, params, tag):
    rid = proc.call(method, params)
    _of(proc).asks[rid] = tag
    return rid


def _later(host, name, proc, tag, msg):
    """A step whose answer does not come from Codex (the callback, a session still starting, a reconnect with nothing to
    do): a notification of our own into the host's inbox — the fact goes the same way as Codex's lines, in the next poll.
    It touches nothing of the process (the callback runs in a thread of its own) and names it: an answer meant for a
    process that has since been replaced is dropped (notice)."""
    with host.lock:
        host.inbox.put((name, {"method": LATER, "params": {"proc": _of(proc), "tag": tag, "msg": msg}}, host.clock()))


def _scrub(text):
    from core import mcplogin
    return mcplogin.scrub(str(text or ""))[:300]


# --- the host's methods ----------------------------------------------------------------------------------------------
def status(host, name, proc):
    if not _ready(host, name, proc):
        return "starting"   # no thread yet: the list would be empty; the servers' start ends with a status anyway
    _call(proc, "mcpServerStatus/list", {"threadId": proc.thread}, ("status", None, None, []))
    return "sent"


def reconnect(host, name, proc, server):
    if not _ready(host, name, proc):
        raise RuntimeError("thread not ready")
    st, now = _of(proc), clock()
    if server not in st.statuses and server not in st.started and server not in _servers(proc):
        _later(host, name, proc, ("reconnected", server),
               {"result": {"ok": False, "error": NO_SERVER.format(server=server)}})
        return "sent"
    if st.statuses.get(server) == "connected" and st.started.get(server) != "starting":
        _later(host, name, proc, ("reconnected", server), {"result": {"ok": True}})
        return "sent"
    st.waiting[server] = now
    if st.started.get(server) == "starting" or now - st.signed_at.get(server, -STALE) < STALE:
        return "sent"   # it is starting, or will once the sign-in completes: its start answers, no reload
    _reload(proc, now)
    return "sent"


def _reload(proc, now):
    """config/mcpServer/reload — it restarts every server of the thread: one for several reconnects; while a turn runs
    and another server is connected, at the turn's end (turn_done) — a restart would cut that server's calls."""
    st = _of(proc)
    if st.reload_at and now - st.reload_at < STALE:
        return
    if (proc.turn or proc.starting) and any(w == "connected" for s, w in st.statuses.items() if s not in st.waiting):
        st.deferred = True
        return
    st.deferred, st.reload_at = False, now
    _call(proc, "config/mcpServer/reload", {}, ("reload",))


def turn_done(proc, name, turn):
    _deferred(proc)
    return []


def compacted(proc, name):
    _deferred(proc)   # a compaction turn ends here, not in turn_done
    return []


def _deferred(proc):
    st = _of(proc)
    if st.deferred and st.waiting and proc.thread and not proc.turn and not proc.starting:
        _reload(proc, clock())


def login(host, name, proc, server, attempt):
    _of(proc).state_dir = host.state_dir
    if not _ready(host, name, proc):   # still starting: as Claude's "pending" — the core asks again in a second
        _later(host, name, proc, ("pre", server, attempt, []),
                {"result": {"data": [{"name": server, "runtimeStatus": "starting"}]}})
        return "sent"
    _call(proc, "mcpServerStatus/list", {"threadId": proc.thread}, ("pre", server, attempt, []))
    return "sent"


def login_poll(host, name, proc, server, attempt):
    if not _ready(host, name, proc):
        return "starting"
    _call(proc, "mcpServerStatus/list", {"threadId": proc.thread}, ("poll", server, attempt, []))
    return "sent"


def target(redirect, url):
    """The address to open on Codex's listener: the redirect_uri of the attempt's link with only the OAuth answer's
    parameters from the address the person sent; None — the address is not this attempt's (another host, port or path)."""
    try:
        want, got = urllib.parse.urlsplit(redirect), urllib.parse.urlsplit(str(url).strip())
        same = (got.scheme.lower(), (got.hostname or "").lower(), got.port, got.path.rstrip("/")) == \
            (want.scheme.lower(), (want.hostname or "").lower(), want.port, want.path.rstrip("/"))
    except ValueError:   # a bad port
        return None
    if not same or want.scheme.lower() != "http" or (want.hostname or "").lower() not in ("127.0.0.1", "localhost"):
        return None
    query = [(k, v) for k, v in urllib.parse.parse_qsl(got.query, keep_blank_values=True) if k in RETURN_KEYS]
    if not query:
        return None
    return urllib.parse.urlunsplit(("http", want.netloc, want.path, urllib.parse.urlencode(query), ""))


def callback(host, name, proc, server, attempt, url):
    if not _ready(host, name, proc):
        raise RuntimeError("thread not ready")
    redirect = _of(proc).redirects.get(attempt)
    where = target(redirect, url) if redirect else None
    if where is None:
        _later(host, name, proc, ("callback", server, attempt),
               {"result": {"ok": False, "error": FOREIGN}})
        return "sent"

    def go():
        try:   # straight to the loopback: no proxy (a proxy would not reach Codex's listener and must not see the code)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(where, timeout=CALLBACK_WAIT) as r:
                res = {"ok": 200 <= r.status < 300}
        except urllib.error.HTTPError as e:   # Codex's listener refused the code (another attempt's state, a used code)
            res = {"ok": False, "error": NOT_TAKEN.format(code=e.code)}
        except (OSError, ValueError) as e:    # the listener is gone: the attempt timed out or ended
            res = {"ok": False, "error": NOT_WAITING.format(kind=type(e).__name__)}
        _later(host, name, proc, ("callback", server, attempt), {"result": res})

    threading.Thread(target=go, daemon=True).start()
    return "sent"


# --- the host's common seams -------------------------------------------------------------------------------------------
def info(proc):
    """The thread's servers for session.info: their state is not known yet at the start — session.mcp brings it once
    they have started (Claude's init says it at once; until then the core counts them as not reported)."""
    # mcp_login_pty: False — there is no old way (`claude mcp login`) for its servers: they are its thread's own
    return {"mcp": sorted(_servers(proc)), "mcp_login_pty": False}


def _settled(proc):
    """Every server has started: their status once (needs-auth is not in startupStatus) — session.mcp follows."""
    st = _of(proc)
    if proc.thread and not st.asking and st.started:
        st.asking = True
        _call(proc, "mcpServerStatus/list", {"threadId": proc.thread}, ("status", None, None, []))


def start(proc):
    """The thread is ready: the servers may have started before the host knew the thread."""
    if not any(s == "starting" for s in _of(proc).started.values()):
        _settled(proc)


def notice(proc, name, method, params):
    st = _of(proc)
    if method == LATER:
        return _answer(proc, name, params["tag"], params["msg"]) if params.get("proc") is st else []
    if method == "mcpServer/startupStatus/updated" and isinstance(params.get("name"), str):
        server, state = params["name"], params.get("status")
        st.started[server] = state
        if state == "ready":   # started and connected: a reconnect right after needs no reload
            st.statuses[server] = "connected"
        if state == "starting" and server in st.waiting:
            st.waiting[server] = clock()   # its restart is under way: its end answers, not an older status
        if state == "starting" and st.reload_at:
            st.restarting = True
        if state in STARTED:
            st.signed_at.pop(server, None)   # the restart after a sign-in is over: a next reconnect reloads again
        facts = []
        if state in STARTED and st.waiting.pop(server, None):
            error = None if STARTED[state] else str(params.get("error") or state)[:300]
            facts.append(("session.mcp_reconnected", name, {"server": server, "ok": STARTED[state], "error": error}))
        if state == "starting":
            st.asking = False
        elif not any(s == "starting" for s in st.started.values()):
            if st.restarting:   # a start left over from before the reload does not free it
                st.reload_at, st.restarting = 0, False
            _settled(proc)
        return facts
    if method == "mcpServer/oauthLogin/completed":
        server, attempt = st.logins.pop(params.get("loginId"), (params.get("name"), None))
        if attempt is None:
            attempt = next((a for (s, a) in st.logins.values() if s == server), None)
        if isinstance(server, str):   # Codex restarts it with the token: a reconnect waits for that (STALE), no reload
            st.signed_at[server] = clock()
            if not params.get("success"):
                st.signed_at.pop(server)
        if attempt is None or params.get("success"):
            return []   # a success ends by the status: Codex restarts the server, polling and session.mcp see it connected
        st.redirects.pop(attempt, None)
        # the error with the marks of the core's phrase (error_l10n): the page shows it in the owner's language
        return [("session.mcp_login", name, {"attempt": attempt, "server": server, "step": "auth", "ok": False,
                                             **coretext.put({}, "error", _scrub(params.get("error")) or LOGIN_FAILED)})]
    return []


def _rows(msg):
    return [r for r in (msg.get("result") or {}).get("data") or [] if isinstance(r, dict) and isinstance(r.get("name"), str)]


def response(proc, name, method, params, msg):
    st = _of(proc)
    rid = msg.get("id")
    if rid not in st.asks:
        return None
    return _answer(proc, name, st.asks.pop(rid), msg)


def _answer(proc, name, tag, msg):
    st = _of(proc)
    kind = tag[0]
    if kind in ("status", "pre", "poll"):
        if "error" not in msg and (msg.get("result") or {}).get("nextCursor"):   # more servers: the next page
            rows = tag[3] + _rows(msg)
            _call(proc, "mcpServerStatus/list", {"threadId": proc.thread, "cursor": msg["result"]["nextCursor"]},
                  (*tag[:3], rows))
            return []
        return _status_answer(proc, name, tag, msg)
    if kind == "auth":
        return [_auth_answer(proc, name, tag, msg)]
    if kind == "callback":
        _, server, attempt = tag
        res = msg.get("result") or {}
        if res.get("ok"):
            st.redirects.pop(attempt, None)
            st.signed_at[server] = clock()
        return [("session.mcp_login", name, {"attempt": attempt, "server": server, "step": "callback",
                                             "ok": bool(res.get("ok")),
                                             **(coretext.put({}, "error", res["error"]) if res.get("error") else {})})]
    if kind == "reconnected":
        res = msg.get("result") or {}
        return [("session.mcp_reconnected", name, {"server": tag[1], "ok": bool(res.get("ok")),
                                                   "error": res.get("error")})]
    if kind == "reload":
        if "error" not in msg:
            return []   # the servers restart: each one's start answers its reconnect (notice)
        st.reload_at, st.restarting = 0, False
        error = str((msg.get("error") or {}).get("message") or "reload")[:300]
        return [("session.mcp_reconnected", name, {"server": s, "ok": False, "error": error})
                for s in list(st.waiting) if st.waiting.pop(s, None)]
    return []


def _status_answer(proc, name, tag, msg):
    kind, server, attempt, rows = tag
    st = _of(proc)
    if kind == "status":
        st.asking = False
    if "error" in msg:
        if kind == "pre":
            return [("session.mcp_login", name, {"attempt": attempt, "server": server, "step": "auth", "ok": False,
                                                 "error": _scrub((msg["error"] or {}).get("message"))})]
        return []
    rows = rows + _rows(msg)
    words = {r["name"]: _word(r) for r in rows}
    st.statuses.update(words)
    facts = []
    # a reconnect whose server never told its start (STALE after the request or its last "starting") — by the status;
    # a fresher one waits for its start: this answer may predate the reload
    now = clock()
    _deferred(proc)   # a turn that ended without turn_done (a refused steer cleared it)
    for s in [s for s, at in st.waiting.items() if not st.deferred and now - at >= STALE
              and words.get(s) not in (None, "pending")]:
        st.waiting.pop(s)
        facts.append(("session.mcp_reconnected", name, {"server": s, "ok": words[s] == "connected",
                                                        "error": None if words[s] == "connected" else words[s]}))
    if kind == "pre":
        word = words.get(server)
        if word != "needs-auth":   # connected — signed in already; pending — the core asks again; else — why not
            return facts + [("session.mcp_login", name, {"attempt": attempt, "server": server, "step": "auth",
                                                         "ok": False, "status": str(word or "none")})]
        _call(proc, "mcpServer/oauth/login", {"name": server, "threadId": proc.thread}, ("auth", server, attempt))
        return facts
    if kind == "poll":
        return facts + ([("session.mcp_login", name, {"attempt": attempt, "server": server, "step": "status",
                                                      "status": "connected"})] if words.get(server) == "connected" else [])
    # the tool count of each server for the session's set (core/kit.py, hosts/codex_cmds.kit_fact)
    proc.kit_mcp = {r["name"]: len(r["tools"]) if isinstance(r.get("tools"), dict) else 0 for r in rows}
    from hosts import codex_cmds
    facts += codex_cmds.kit_fact(proc, name)
    addr = _addr(proc, rows)
    key = (words, addr)
    # nothing changed and nothing failed — not a fact (a server waiting for a sign-in waits for days); a failed one —
    # every time: the core decides when to reconnect again (hosts/streamjson._mcp_answer, the same rule)
    if st.seen == key and "failed" not in words.values():
        return facts
    st.seen = key
    return facts + [("session.mcp", name, {"servers": words, **({"addr": addr} if addr else {})})]


def _auth_answer(proc, name, tag, msg):
    from core import mcplogin
    _, server, attempt = tag
    fact = {"attempt": attempt, "server": server, "step": "auth"}
    got = msg.get("result") or {}
    url = got.get("authorizationUrl")
    if "error" in msg or not isinstance(url, str) or not url.startswith(("https://", "http://")):
        error = _scrub((msg.get("error") or {}).get("message")) if "error" in msg else NO_LINK
        return ("session.mcp_login", name, {**fact, "ok": False, "error": error or NO_LINK})
    st = _of(proc)
    redirect = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query)).get("redirect_uri")
    if redirect:
        st.redirects[attempt] = redirect
    if got.get("loginId"):
        st.logins[got["loginId"]] = (server, attempt)
    try:
        mcplogin.put_link(st.state_dir, attempt, url)
    except (OSError, ValueError, TypeError) as e:
        return ("session.mcp_login", name, {**fact, "ok": False, "error": repr(e)[:200]})
    return ("session.mcp_login", name, {**fact, "ok": True})


# --- elicitation: an MCP server asks the user ----------------------------------------------------------------------------
def _choices(schema):
    """[(label, value)] of a choice field (enum with enumNames, oneOf const/title), or None."""
    if isinstance(schema.get("oneOf"), list):
        return [(str(o.get("title") or o.get("const")), o.get("const")) for o in schema["oneOf"] if isinstance(o, dict)]
    if isinstance(schema.get("enum"), list):
        names = schema.get("enumNames") if isinstance(schema.get("enumNames"), list) else []
        return [(str(names[i]) if i < len(names) else str(v), v) for i, v in enumerate(schema["enum"])]
    return None


def _fields(form):
    """[(key, schema, kind)] of a form, kind — text | number | integer | boolean | choice; None — not a form a card holds."""
    props = (form or {}).get("properties") if isinstance(form, dict) else None
    if not isinstance(props, dict) or not props or len(props) > MAX_FIELDS:
        return None
    out = []
    for key, schema in props.items():
        if not isinstance(schema, dict):
            return None
        kind = "choice" if _choices(schema) else schema.get("type")
        if kind == "string":
            kind = "text"
        if kind not in ("text", "number", "integer", "boolean", "choice"):
            return None
        out.append((key, schema, kind))
    return out


def request(proc, name, rid, method, params):
    if method != ELICIT:
        return None
    from core import i18n
    server = str(params.get("serverName") or "MCP")
    head = f"MCP {server}: {params.get('message') or ''}".strip()
    key = f"cx-{rid}"
    if params.get("mode") == "url" and isinstance(params.get("url"), str):
        q = {"question": f"{head}\n\n{params['url']}", "header": server[:12], "multiSelect": False,
             "options": [{"label": i18n.t("cx_b.url_done"), "description": ""},
                         {"label": i18n.t("cx_b.url_refuse"), "description": ""}]}
        proc.asks[key] = (rid, method, {"url": q["question"], "done": i18n.t("cx_b.url_done")})
        return [_card(name, key, [q])]
    fields = _fields(params.get("requestedSchema")) if params.get("mode", "form") == "form" else None
    if not fields:   # a form a card does not hold (nested, too many fields) or a mode of OpenAI's own: declined
        proc.write({"id": rid, "result": {"action": "decline"}})
        return []
    yes, no = i18n.t("cx_b.yes"), i18n.t("cx_b.no")
    questions, order = [], []
    for i, (field, schema, kind) in enumerate(fields):
        title = str(schema.get("title") or field)
        text = (head + "\n\n" if i == 0 else "") + title + (f" — {schema['description']}" if schema.get("description")
                                                           else "")
        options = [lbl for lbl, _ in _choices(schema)] if kind == "choice" else [yes, no] if kind == "boolean" else []
        questions.append({"question": text, "header": title[:12], "multiSelect": False,
                          "options": [{"label": o, "description": ""} for o in options]})
        order.append((text, field, kind, schema))
    required = (params.get("requestedSchema") or {}).get("required") or []
    proc.asks[key] = (rid, method, {"fields": order, "required": list(required), "yes": yes, "no": no})
    return [_card(name, key, questions)]


def _card(name, key, questions):
    return ("permission.requested", name, {"request_id": key, "tool": "AskUserQuestion", "interactive": True,
                                           "why": None, "input": {"questions": questions}})


def _value(raw, kind, schema, yes, no):
    """A card's answer text as the form's value; ValueError — it does not fit the field."""
    raw = str(raw).strip()
    if kind == "boolean":
        if raw in (yes, no):
            return raw == yes
        raise ValueError(raw)
    if kind in ("number", "integer"):
        value = int(raw) if kind == "integer" else float(raw)
        lo, hi = (x if isinstance(x, (int, float)) else None for x in (schema.get("minimum"), schema.get("maximum")))
        if not math.isfinite(value) or (lo is not None and value < lo) or (hi is not None and value > hi):
            raise ValueError(raw)   # "inf" would be written as Infinity — not JSON; out of range — the server refuses
        return value
    if kind == "choice":
        for label, value in _choices(schema):
            if raw in (label, str(value)):
                return value
        raise ValueError(raw)
    return raw


def answer_request(proc, method, data, response):
    if method != ELICIT:
        return None
    given = (response.get("updatedInput") or {}).get("answers") or {}
    if response.get("behavior") != "allow" or not isinstance(given, dict):
        return {"action": "decline"}
    if "url" in data:
        return {"action": "accept" if given.get(data["url"]) == data["done"] else "decline"}
    content = {}
    for text, field, kind, schema in data["fields"]:
        raw = given.get(text)
        if raw is None or str(raw).strip() == "":
            if field in data["required"]:
                return {"action": "decline"}
            continue
        try:
            content[field] = _value(raw, kind, schema, data["yes"], data["no"])
        except (ValueError, OverflowError):   # "12a", 10**400: the server would refuse it — declined rather than sent wrong
            return {"action": "decline"}
    return {"action": "accept", "content": content}
