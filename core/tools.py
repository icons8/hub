"""Session tools: requests, grants, the owner's "yes" on risky ones, revocation. The pure part of the reducer.

The catalog (`catalog.loaded`) arrives in the journal from hubd: {id: {"kind": "plugin"|"mcp", "risk": [...], ...}}.
Owner rule 2026-09-29: the hub attaches by itself from the catalog; a tool with a risk (money, prod, external
sends, the owner's mail) only after the owner's "yes". An attach is applied when the session is free: the core
releases it and brings it up again via --resume with the new set (see reducer._tools_apply).
"""
import re
import time

from core import coretext, forms, langdata
from core.coretext import T
from core.machines import SLEEP


def grant_note(state):
    """What to say after a tool is granted to a live session: it gets into the session when it is brought up with the
    new set, and the core brings up a free one only — the tool will not be there in a running turn. It cannot be
    granted before hub task run (there is no session yet), so for a task executor — --tools at bring-up. The CLI's
    output: in the language of whoever runs it."""
    from core import i18n
    if state in ASLEEP:   # 19.10: no process — no need to wake it for a grant, the bring-up takes the whole set
        return i18n.t("core.tools.grant_asleep")
    if state in ("busy", "dialog", "starting"):
        return i18n.t("core.tools.grant_busy")
    return i18n.t("core.tools.grant_free")


LIVE = ("starting", "idle", "busy", "dialog", "held")
# not up, but will come up with the same set (a message, an alarm, a core bring-up); a retired one is woken only by
# the owner or the hub
ASLEEP = (*SLEEP, "absent")


class Rejected(Exception):
    pass


def same_name(ids, tool):
    """Full ids with this name without @catalog ("frontend-design" -> "frontend-design@my-skills"), in order."""
    return sorted(t for t in set(ids) if "@" in t and t.rsplit("@", 1)[0] == tool)


def candidates(st, sess, tool):
    """Which tool is meant (05.10: the request was "frontend-design", "frontend-design@my-skills" was granted — the
    request hung for two weeks, grant by the short name answered "not in catalog"): an exact id of the catalog or of
    the session's set is itself; a name without @catalog gives the full ids with this name."""
    ids = set(st["catalog"]) | set((sess or {}).get("tools") or [])
    if not isinstance(tool, str) or "@" in tool or tool in ids:
        return [tool]
    return same_name(ids, tool)


def resolve(st, sess, tool):
    """The full id if there is exactly one; none or several — as is (guessing is not for us)."""
    full = candidates(st, sess, tool)
    return full[0] if len(full) == 1 else tool


def ambiguous(st, sess, tool):
    """A name that several tools have: a refusal with the full ids, or None."""
    full = candidates(st, sess, tool)
    return T("core.tools.ambiguous", "several have this name — a full id is needed: {ids}").format(ids=", ".join(full)) \
        if len(full) > 1 else None


def close_held(st, ev, sessions):
    """Requests for what the session already has (by the same id or the only one with this name) are closed without a
    grant. Called with every catalog and session bring-up: this also clears requests left hanging from before the rule
    (the catalog is written to the journal only when it changes). One waiting for the owner is not closed: it has a
    card, closed by his answer ("already had")."""
    for sess in sessions:
        for req in sess.get("requests") or []:
            if req["state"] == "requested" and _named(st, sess, req) in (sess.get("tools") or []):
                _close_had(req, ev)


def down(st, sess, tool, grant=False):
    """A tool the session has, but whose MCP server is not up in it (info: mcp_failed, mcp_auth, from the last
    session.info or status): "already has it" would mislead, so say why and what to do. None: nothing is wrong with
    it (or it is not a server: a skill, a plugin without MCP). grant: the answer to `hub tools grant` (the hub or the
    owner reads it), otherwise to the session's own request. A plugin's server is named plugin:<plugin.json name>:<server>
    (catalog.plugin_servers); the catalog keeps that name as plugin_name, the id's part before @ if it has none.
    Waiting for a login is never a command for the session: the core tells the hub once per server
    (mcpwatch.auth_notice), and each login is a browser tab for the owner."""
    if sess.get("mcp_init"):   # a new process has not said its servers yet: mcp_failed is the previous process's
        return None
    info = sess.get("info") or {}
    base = tool.split("@")[0]
    entry = st["catalog"].get(tool) or {}
    plugin = entry.get("plugin_name") or base
    # by what the tool is: a catalog server is its own name (or its `as`, the name the session knows it by), a plugin's
    # servers are plugin:<name>:… (an unrelated server that shares the plugin's id is not its); a tool gone from the
    # catalog — either way
    mine = lambda names: [n for n in names or [] if (n in (tool, entry.get("as")) if entry.get("kind") == "mcp" else
                                                      str(n).startswith(f"plugin:{plugin}:") if entry else
                                                      n in (tool, base) or str(n).startswith(f"plugin:{plugin}:"))]
    failed, auth, me = mine(info.get("mcp_failed")), mine(info.get("mcp_auth")), sess["name"]
    if failed and grant:
        return (f"{tool} is already granted to {me}, but its MCP server {failed[0]} did not come up there: granting "
                f"again will not help. The core reconnects it by itself; right now: hub mcp reconnect {failed[0]} "
                f"--session {me}, or restart the session with the same conversation: hub session reload {me}.")
    if failed:
        return (f"{tool} is already granted, but its MCP server did not come up in {me}: asking again will not help. "
                f"The core reconnects it by itself; right now: hub mcp reconnect {failed[0]} --session {me}, or "
                f"restart the session with the same conversation: hub session reload {me}. If it fails again, the "
                f"cause is the server or its settings: tell the hub, do not keep asking for the grant.")
    if auth and grant:
        return (f"{tool} is already granted to {me}, but its MCP server {auth[0]} is waiting for a login: granting "
                f"again will not help. The core has told the hub about it (one login per server).")
    if auth:
        return (f"{tool} is already granted, but its MCP server is waiting for a login; the hub has been told. "
                f"Asking again will not help: go on without {tool}, or tell the hub if you cannot.")
    return None


def _named(st, sess, req):
    """The full id of a request; if found by a short name, write it into the request: CLI, feed and bot look a request
    up by id."""
    req["tool"] = resolve(st, sess, req["tool"])
    return req["tool"]


def allowed_for(info, name):
    """A tool with `only` in the catalog (hub.json: mcp.<id>.only) is granted only to the named sessions; to others —
    only_note."""
    return not info or not info.get("only") or name in info["only"]


def answered(data):
    """A turn result with an answer: there is text, it is not "—" (the silent result of a `hub wait` check), not a
    failure."""
    return (data.get("text") or "").strip() not in ("", "—") and not data.get("is_error")


def waiting_now(sess):
    """What the session is waiting for now: its `hub waiting`, until someone writes to it after that (stale)."""
    w = sess.get("waiting")
    return w if w and "stale" not in w else None


def waited(sess):
    """Its stale `hub waiting` (it was written to afterwards and did not repeat it) — "was waiting", or None."""
    w = sess.get("waiting")
    return w if w and "stale" in w else None


def gist_now(sess, now=None):
    """Its gist — {text, ts} (and lead: true for the start of a result): what the session did last and in what state, the one source of the list-row line for
    every client. While it is about the present it is the first line of the latest result for the owner (written at the
    moment of the result) or a `hub gist` said in that turn or later. A result for the owner from a turn without a
    new gist makes an earlier gist stale. So does a result for the owner without a gist line of its own that came
    later in the same turn as the `hub gist` ("searching for the order..." said before the answer), unless the session
    still waits for something (hub waiting) or runs a background job that is not a service one (restart.service): then
    "what it is doing now" is still true. A stale or missing gist is replaced by the start of the latest result
    (result_lead, marked lead: true), so the line still says what was done; None when there is no result at all ("no
    status": a client shows nothing rather than a goal or the words of someone else) and when its latest output went
    to someone else (owner_out < last_out: the result to the owner is an old one). A result ends its turn, so in a
    running turn the start of a result is older than the turn (ts < since: a client shows the recap). A turn with no result for the owner ("—", a built-in command, a
    check) changes nothing."""
    g = sess.get("gist")
    stale = not g or (sess.get("out_turn") or 0) > (g.get("turn") or 0)
    if not stale and not g.get("result") and sess.get("out_turn") == g.get("turn") and g["ts"] < (sess.get("out_head_ts") or 0):
        from core import restart   # restart imports this module
        stale = not (waiting_now(sess) or restart.working(sess, time.time() if now is None else now))
    if not stale:
        return {"text": g["text"], "ts": g["ts"]}
    if (sess.get("owner_out") or 0) < (sess.get("last_out") or 0):
        return None
    text = result_lead(sess.get("out_head"))
    return {"text": text, "ts": sess.get("out_head_ts") or 0, "lead": True} if text else None


SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def result_lead(head):
    """What a result says, for a list row without a gist (out_head: one line, no Markdown marks, up to 160 characters,
    a cut one ends with an ellipsis). A short result is shown whole: its second sentence is often the state ("Waiting
    for his answer"). A cut one — its first sentence, and a bare "Done." takes the next sentence too. No sentence end —
    the start cut at a word, within the gist's 160 characters."""
    from core import reducer   # reducer imports this module
    head = str(head or "").strip()
    out = ""
    if len(head) < reducer.OUT_HEAD or not head.endswith("…"):
        out = head
    else:
        for part in SENTENCE_END.split(head):
            out = f"{out} {part}".strip()
            if len(out) >= 20:
                break
    out = reducer.one_line(out, reducer.GIST, word=True)
    return out if any(c.isalnum() for c in out) else ""


def from_owner(data):
    """The owner's word comes only from his channels (the page, the CLI outside sessions, a module handle), not from a
    session."""
    from core.guards import owner_channel
    return data.get("owner") is True and owner_channel(data.get("channel"))


OWNER_WORD_TTL = 24 * 3600


# the start of the core's note on the × close (reducer), for messages before the card_dismissed mark; earlier cores
# wrote it in Russian
DISMISSED = ("The owner closed your question", langdata.legacy("dismissed"))


def card_answer(st, qid):
    """The owner's answer to card qid is his message marked answers_question; a card closed without an answer (×) and
    "Deny" are not an answer."""
    return next((m for m in reversed(st["messages"].values()) if m.get("answers_question") == qid
                 and m.get("from_owner") and not dismissal(m) and not m.get("card_declined")), None)


def dismissal(m):
    """The core's note to a session, "the owner closed your question without an answer" (×): from_owner, but not his
    words."""
    return bool(m.get("card_dismissed")) or str(m.get("text") or "").startswith(DISMISSED)


def owner_word(st, data, ts, own=False, lead=None, cards=False, lift=False, trainer=False, spec=None, deputy=None):
    """"--for mN": the owner's word to the hub (owner decision 2026-09-30) — his "yes" in the chat counts as permission.
    own — for the session's own executor (hub task run): then the owner's word to the session itself counts too
    (friction digest 16.10: the mini-hub passes the executor what was said to it — 15 task.run refusals, followed by
    the same request without --for).
    lead — the lead session of the executor being brought up (hub task go): the owner's word to it is fine too (20.10:
    the owner wrote to the mini-hub "bring up the executor", and the hub did not count his message).
    cards — a command with the gate "the owner's word for this action" (module add, tools grant, task run/go):
    "--for qN" is his answer to a card; for the hub — to a card of any session (20.10: the owner answered "Attach" on
    a specialist's card, the hub got a refusal and asked him again with a second card).
    lift — a command that only takes something off (hub marketplace uninstall|remove, module remove, market remove):
    for the hub his message to any session counts (17.11: he wrote "remove the plugin" to the session of its area, it
    passed this to the hub, and the hub's --for was refused as "written to {to}"). What is taken off comes back with
    an install on his word, but its approvals (module consents) start from scratch. Installing, granting and
    launching keep the rule.
    trainer — agent-dev may cite too (a skill for an agent, owner 2026-11-17): only the owner's message to it or his
    answer to its own card, never one written to someone else.
    spec — the task whose open spec is accepted or dropped (hub task spec accept|drop dN): his reply to that spec's
    card, written to its executor (the message's spec_card, reducer._spec_card), counts too.
    deputy — the gate (its event type) takes "--for qN" closed on the decision of a deputy who decides everything in
    their area (core/deputyroute.py word: still a ground — the same appointment, channel and away), as his answer to
    that card would be: their word, never his — the result names them ("deputy"), and the owner's summary has the gate.
    Only the gates that pass it (tools, executors, specs, roles); naming a deputy never does."""
    sender = st["sessions"].get(data.get("sender") or "") or {}
    hub = sender.get("kind") == "hub"
    own = own or (trainer and bool(sender) and sender.get("name") == st.get("trainer")
                  and data.get("channel") == "session")
    ref = str(data.get("for") or "")
    card = cards and re.fullmatch(r"q\d+", ref) and card_answer(st, ref)
    if deputy and cards and not card and re.fullmatch(r"q\d+", ref):
        from core import deputyroute   # deputyroute imports this module
        dep = deputyroute.word(st, ref)
    else:
        dep = None
    msg = card or dep or st["messages"].get(ref)
    trusted = hub or (own and sender and data.get("channel") == "session")
    # action gate: "Deny" and × are his messages to the hub, but not a "yes" (forwarding his words, hub send --for,
    # is fine)
    no = cards and not dep and (dismissal(msg or {}) or (msg or {}).get("card_declined"))
    if trusted and no:
        raise Rejected(FOR_DENIED.format(ref=ref))
    # which condition failed, first (13.11: one phrase for every reason, and a session repeated the same --for)
    why = None if not trusted or not ref else (
        (FOR_NO_ANSWER if cards and re.fullmatch(r"q\d+", ref) else FOR_NO_MESSAGE) if not msg else
        FOR_NOT_OWNER if not (msg.get("from_owner") or dep) else
        FOR_OLD.format(hours=int((ts - msg["created"]) // 3600)) if ts - msg["created"] > OWNER_WORD_TTL else
        FOR_ELSEWHERE.format(to=msg["session"]) if not (msg["session"] in (sender["name"], lead) or hub and (card or dep or lift)
                                                        or spec and msg.get("spec_card") == spec)
        else None)
    if not trusted or why or not msg:
        raise Rejected(coretext.join(*([FOR_WHY.format(ref=ref), why, ". "] if why else []),
                                     FOR_NEEDS.format(label=f"[hub:mN{forms.ORIGIN_OWNER}]"),
                                     *([FOR_CARD] if cards else []),
                                     *([FOR_OWN] if own and not hub else
                                       [FOR_ASK_HUB] if cards and sender and not hub else   # not trusted: nothing else helps
                                       [FOR_LEAD.format(lead=lead)] if lead and lead != sender.get("name") else [])))
    if dep:   # the deputy's decision: what it opened goes into the owner's summary (a refused event drops it)
        deputyroute.opened(st, ref, deputy)
        return {"message": ref, "text": msg["text"][:2000], "deputy": dep["deputy"]}
    return {"message": msg["id"], "text": msg["text"][:300]}


FOR_DEPUTY_DATA = T("core.tools.for_deputy_data",
                    "{tool} holds the owner's own data (risk owner-data): it is granted on the owner's word only, not on "
                    "the decision of the deputy {name} — the request waits for the owner")


def deputy_may(words, info, tool):
    """A tool on a deputy's decision (owner_word deputy): never one the catalog marks as holding the owner's own data
    (risk owner-data: his mail, chats, browser sign-ins): the owner keeps his personal data and correspondence to
    himself."""
    if words.get("deputy") and "owner-data" in (info.get("risk") or []):
        raise Rejected(FOR_DEPUTY_DATA.format(tool=tool, name=words["deputy"]))


def names_tool(st, words, tool):
    """The owner's message (words of owner_word) names this tool: its id, its entry name or the plugin's name in the
    catalog (3+ characters, as a word). An answer to a card is his message with the card's question and his choice:
    a card that names the tool is a yes to it."""
    info = (st.get("catalog") or {}).get(tool) or {}
    names = {tool.lower(), tool.split("@")[0].lower(), str(info.get("plugin_name") or "").lower()}
    return names_any(st, words, {n for n in names if len(n) >= 3})


def names_any(st, words, names):
    """The owner's message (words of owner_word) has one of these names as a word (lowercase)."""
    msg = st["messages"].get(words.get("message")) or {}
    text = str(msg.get("text") or words.get("text") or "").lower()
    return any(re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", text) for n in names)


def name_forms(name, bare=True):
    """The names the owner's words may call a module or a plugin by (3+ characters, lowercase). A source names itself
    in full — the whole as given, without .git, without the scheme, and for GitHub its owner/repo — and a word without
    a separator names nothing: the requester chooses the short name ("status" of evil/status, a folder `status`, is
    in "show me the status"), so does a host (alice/notes elsewhere than GitHub). bare — the short name counts too
    (the last part without hub-module- / hub-plugin-, an entry without @catalog): only where the hub cannot choose it —
    an installed module, an entry of an official catalog."""
    n = str(name or "").strip().lower().rstrip("/\\")
    plain = re.sub(r"\.git$", "", n)
    out = {n, plain, re.sub(r"^[a-z][\w+.-]*://", "", plain)} if bare or re.search(r"[/\\:@]", n) else set()
    hub = re.match(r"(?:[a-z][\w+.-]*://)?(?:[^@/]+@)?github\.com[/:]([\w.-]+)/([\w.-]+)", plain)
    if hub:
        out.add(f"{hub[1]}/{re.sub(r'[.]git$', '', hub[2])}")
    parts = [p for p in re.split(r"[/\\:]", plain) if p]
    if bare and parts:
        out |= {parts[-1], re.sub(r"^hub-(module|plugin)-", "", parts[-1])}
        if not re.search(r"[/\\:]", n):
            out.add(n.split("@")[0])   # an entry id <entry>@<catalog>; not git@host: of an address
    return {x for x in out if len(x) >= 3}


FOR_NOT_NAMED = T("core.tools.for_not_named",
                  "--for {ref}: the owner's words do not name {tool} — a risky tool is the owner's decision by its "
                  "name, in their message or in the question of the card they answered: grant it without --for, the "
                  "core sends them the card with what it can do")
FOR_DENIED = T("core.tools.for_denied", "--for {ref}: this is \"Deny\" or the owner closing the card — not their \"yes\"")
FOR_NEEDS = T("core.tools.for_needs",
              "--for: needs the owner's message to the hub no older than a day (mN of the label {label})")
FOR_WHY = T("core.tools.for_why", "--for {ref}: ")
FOR_NO_MESSAGE = T("core.tools.for_no_message", "no such message")
FOR_NO_ANSWER = T("core.tools.for_no_answer", "no answer of the owner to this card")
FOR_NOT_OWNER = T("core.tools.for_not_owner", "these are not the owner's words")
FOR_OLD = T("core.tools.for_old", "{hours} h old")
FOR_ELSEWHERE = T("core.tools.for_elsewhere", "the owner wrote it to {to}, not to you")
FOR_CARD = T("core.tools.for_card", "; or qN — their answer to a card")
FOR_OWN = T("core.tools.for_own", "; for your own executor — their message to you too")
FOR_LEAD = T("core.tools.for_lead", "; or their message to {lead} — the lead of this executor")
FOR_ASK_HUB = T("core.tools.for_ask_hub",
                "; this gate takes the word from the hub only — do not repeat it from here: hub send hub \"<what to "
                "connect>, his answer is qN\"")
NOT_OWNERS = T("core.tools.not_owners",
               "--for {id}: this is not the owner's message — a \"task from the owner\" only from their words")


def cited_task(msg):
    """`hub task new <session> --for mN`: the words become a "task from the owner", so it must be the owner's
    message. None means yes, otherwise the reason."""
    if not msg.get("from_owner"):
        return NOT_OWNERS.format(id=msg["id"])
    return None


def apply(st, ev, data, ctx=None):
    from core import identity, owner
    kind, ts = ev["type"], ev["ts"]
    name = identity.address(st, ev.get("entity")) or ev.get("entity")   # "hub" is the hub session, as in hub send
    sess = st["sessions"].get(name)
    tool = data.get("tool")   # the full id if the short name is unambiguous — resolved by the reducer (tools.resolve)
    if kind == "tools.revoked" and sess:   # revoke a sleeping and a parked session too: nothing to attach, can remove
        if (st["catalog"].get(tool) or {}).get("base"):   # otherwise the grant is gone but the session keeps the tool
            raise Rejected(T("core.tools.base_revoke",
                             "a base one — every session has it (hub.json base): remove it from base (live sessions lose "
                             "it with the hub's restart), then revoke the one granted separately"))
        if tool not in sess["tools"]:
            raise Rejected("not attached")
        sess["tools"].remove(tool)
        sess["dirty"] = True
        sess["respawn_now"] = True   # it takes a tool away: background work does not hold it (reducer._relaunch_waits)
        return
    if not sess or sess["state"] == "removed" or (kind == "tools.requested" and sess["state"] not in LIVE):
        raise Rejected("no live session")
    # grant and "yes": checks without a process work in any state; "no live session" only where a live one is needed
    # (17.10: grant to a sleeping session with an already granted tool refused before closing the hanging request)
    live = sess["state"] in LIVE
    info = st["catalog"].get(tool)
    many = kind in ("tools.requested", "tools.granted") and info is None and ambiguous(st, sess, tool)
    if many:
        raise Rejected(many)
    if kind in ("tools.requested", "tools.granted") and not allowed_for(info, name):
        raise Rejected(info.get("only_note") or T("core.tools.only", "{tool} is granted only to: {names}").format(
            tool=tool, names=", ".join(info["only"])))
    req = _open_request(st, sess, tool)
    # the owner's decision on his card for a session that is not up (asleep, parked, not started yet) — record it at
    # once, do not bring up a process for it (17.10: "yes" and a refusal got "no live session", the "yes" was lost).
    # Not for a closed or crashed one
    says = from_owner(data) or (kind == "tools.granted" and bool(data.get("for")))   # his word: himself or --for mN
    asleep = not live and sess["state"] in ASLEEP and says and bool(req) and req["state"] == "needs-owner"
    if kind == "tools.denied" and not live and not asleep:
        raise Rejected("waits for the owner" if req and req["state"] == "needs-owner" and not from_owner(data)
                       and sess["state"] in ASLEEP else "no live session")

    if kind in ("tools.requested", "tools.granted") and tool not in sess["tools"] and from_repo(sess, tool):
        raise Rejected("already has it: from its repo")   # the executor's start found it in the repo's .mcp.json
    if kind == "tools.requested":
        if tool in sess["tools"]:
            raise Rejected((live and down(st, sess, tool)) or "already has it")
        if req:
            raise Rejected("already requested")
        sess["requests"].append({"tool": tool, "why": data.get("why"), "state": "requested",
                                 "known": info is not None, "since": ts, "asked": True})
        # the hub attaches, so tell it (30.09: the request sat in the journal, the hub did not learn about it)
        if ctx and sess.get("kind") != "hub":
            how = ASK_GRANT.format(name=name, tool=tool) if info is not None else ASK_UNKNOWN
            ctx["notify"](ev, ASKS_TOOL.format(name=name, tool=tool, why=data.get("why") or ASK_NO_WHY, how=how))
        return
    if kind == "tools.granted":
        if info is None and tool not in sess["tools"]:   # granted before a catalog change: only close the request (below)
            raise Rejected("not in catalog")
        if tool in sess["tools"]:
            if req:   # the request outlived the grant (journals before 17.10): just close it (sleeping too)
                _close_had(req, ev)
                return
            raise Rejected((live and down(st, sess, tool, grant=True)) or "already has it")
        if info.get("base"):   # every session has it anyway — no card for the owner (07.10)
            raise Rejected("already has it: base")
        if asleep:
            if not from_owner(data):
                words = owner_word(st, data, ts, cards=True, trainer=True, deputy=kind)
                # checked before the request changes: a journal replay folds a refusal in place
                deputy_may(words, info, tool)
                if info.get("risk") and not names_tool(st, words, tool):
                    raise Rejected(FOR_NOT_NAMED.format(ref=data.get("for"), tool=tool))
                req["owner_words"] = words
            _grant(sess, req, tool, ts, st)
            _granted_asleep(sess, req, tool, ev, name, ctx)
            return
        if not live and (sess["state"] not in ASLEEP or sess["state"] == "retired"):   # only the owner or hub wakes a retired one
            raise Rejected("no live session")
        words = None
        if info.get("risk") and not from_owner(data) and data.get("for"):
            words = owner_word(st, data, ts, cards=True, trainer=True, deputy=kind)
            deputy_may(words, info, tool)
            if not names_tool(st, words, tool):
                # "find skills for maps" or "show me the status" is not a yes to a hook or a server with his data — the
                # owner's card is (it names what runs); the same stops a page, a mail or a skill's text making the hub
                # or agent-dev cite his last message (owner 2026-11-17; the hub too since 2026-11-18)
                raise Rejected(FOR_NOT_NAMED.format(ref=data.get("for"), tool=tool))
        elif info.get("risk") and not from_owner(data):
            if not req:
                req = {"tool": tool, "why": data.get("why"), "state": "requested", "known": True, "since": ts}
                sess["requests"].append(req)
            req["state"], req["since"] = "needs-owner", ts  # the hub is for it, the owner decides
            # non-working hours (hub.json quiet_review): the card waits for the working morning (core/owner.py)
            hold = owner.review_hold(st, ts, [req.get("why")], [data.get("sender"), name])
            if hold:
                req["held_until"] = hold
            else:
                req.pop("held_until", None)
            return
        if not live and not from_owner(data) and info.get("exclusive") and any(
                o is not sess and tool in (o.get("tools") or []) and o["state"] in LIVE for o in st["sessions"].values()):
            # one for all: taking it from a working session for a sleeping one, which may not come up for days, is
            # for the owner only
            raise Rejected(T("core.tools.exclusive_up",
                             "one for all: a session that is up has it now — grant it to the sleeping one when it comes up"))
        # a grant by a schedule script (in a batch) is not for the session's task: restart with the tool, no
        # "continue" turn
        _grant(sess, req, tool, ts, st, wake=bool(req) or not data.get("schedule_job"))
        if words:
            req = {"tool": tool, "why": data.get("why"), "state": "granted", "known": True, "since": ts,
                   "owner_words": words}
            sess["requests"].append(req)
        if not live:   # asleep or parked (19.10): no need to wake it — the tool is in the set from its bring-up
            _granted_asleep(sess, req or sess, tool, ev, name, None)
        return
    if kind == "tools.approved":
        if not from_owner(data):
            raise Rejected("only the owner approves")
        if not req or req["state"] != "needs-owner":
            raise Rejected("nothing waits for the owner")
        if tool in sess["tools"]:   # already has it: close the request, no second copy or re-bring-up (sleeping too)
            _close_had(req, ev)
            if ctx:
                ctx["notify"](ev, APPROVED_HAD.format(tool=tool, name=name))
            return
        if not live and not asleep:
            raise Rejected("no live session")
        _grant(sess, req, tool, ts, st)
        if not live:
            _granted_asleep(sess, req, tool, ev, name, ctx)
            return
        if ctx:
            ctx["notify"](ev, APPROVED.format(tool=tool, name=name))
        return
    if kind == "tools.denied":
        if not req:
            raise Rejected("no open request")
        if req["state"] == "needs-owner" and not from_owner(data):
            raise Rejected("waits for the owner")
        req["state"], req["since"], req["reason"] = "denied", ts, data.get("reason")
        if asleep:
            req["asleep_decided"] = ev["seq"]   # reducer: an "FYI" note for a session that is not up
        return
    raise Rejected("unknown tools event")


def live(st, sess):
    """The session's tools that join a running Claude Code mid-turn (hosts/streamjson.mcp_set): catalog MCP only. A
    plugin needs the restart for its skills (its own servers come with it then); a "one for all" one too — its previous
    holder may still be connected."""
    return [t for t in sess.get("tools") or [] if live_one(st, t)]


def live_one(st, tool):
    info = (st.get("catalog") or {}).get(tool) or {}
    return info.get("kind") == "mcp" and not info.get("exclusive")


def from_repo(sess, tool):
    """The session gets the tool from its repo's .mcp.json (the request closed at the start, reducer): no grant, no card."""
    return any(r["tool"] == tool and r.get("repo") for r in sess.get("requests") or [])


def _grant(sess, req, tool, ts, st=None, wake=True):
    if st and (st["catalog"].get(tool) or {}).get("exclusive"):
        # one for all (owner decision 2026-10-01: it should pass easily and automatically on request): take it from the
        # previous holder — it restarts without it when free; remind the new one to restart later
        for other in st["sessions"].values():
            if other is not sess and tool in (other.get("tools") or []):
                other["tools"].remove(tool)
                other["dirty"] = other["respawn_now"] = True   # at once: one for all, two must not hold it
                other["frees"] = {"tool": tool, "to": sess["name"]}
    sess["tools"].append(tool)
    if st and live_one(st, tool) and sess["state"] in ("busy", "dialog"):
        # mid-turn: the core connects it to the running process at once (reducer._pump: mcp_set)
        sess["mcp_live"] = True
    # after the bring-up the core will say "attached, continue" — if the session asked itself or has an open assignment
    if wake:
        sess.setdefault("new_tools", []).append({"tool": tool, "asked": bool(req and req.get("asked"))})
    sess["dirty"] = True  # applied when the session is free (or with the next spawn, if it is in manual mode)
    if req and req.get("asked") or st and (st["catalog"].get(tool) or {}).get("exclusive"):
        # it asked and waits for it, or the one-for-all tool was just taken from its holder (that one goes at once):
        # its background work does not hold the relaunch
        sess["respawn_now"] = True
    if req:
        req["state"], req["since"] = "granted", ts


def _granted_asleep(sess, req, tool, ev, name, ctx):
    """Granted to a session that is not up: the spawn at bring-up takes the whole set, so a re-bring-up and "attached,
    continue" are not needed (the session is told about the grant by an "FYI" note, reducer). new_tools is dropped
    whole: the bring-up (spawn_action) does not read it, and the leftover would show up at someone else's re-bring-up
    days later. req is the request or the session itself (a grant without a request); ctx None — do not tell the hub:
    it granted it itself."""
    sess.pop("new_tools", None)
    req["asleep_decided"] = ev["seq"]
    if ctx:
        ctx["notify"](ev, APPROVED_ASLEEP.format(tool=tool, name=name))


# the core's lines to the hub about tools (core/coretext.py)
ASK_GRANT = T("core.tools.ask_grant", "connect it: hub tools grant {name} {tool}")
ASK_UNKNOWN = T("core.tools.ask_unknown", "not in the hub's catalog \u2014 if a connected catalog has it (hub marketplace search <words>), hub tools grant installs it on the way")
ASK_NO_WHY = T("core.tools.ask_no_why", "no explanation")
ASKS_TOOL = T("core.tools.asks",
              "{name} asks for the tool {tool}: {why}. The decision is yours — {how}; to refuse: hub tools deny {name} "
              "{tool} \"<why>\".")
APPROVED_HAD = T("core.tools.approved_had",
                 "The owner allowed {tool} for {name}: the session already had it, there will be no restart.")
APPROVED = T("core.tools.approved", "The owner connected {tool} for {name}: the session comes up with it and continues by "
             "itself.")
APPROVED_ASLEEP = T("core.tools.approved_asleep",
                    "The owner connected {tool} for {name}: the session's process is not up now — it will have the tool "
                    "from its next start.")


def _close_had(req, ev):
    """A request for a tool the session already has: closed without a grant. had is set by this event (CLI and feed:
    "already")."""
    req.update(state="granted", since=ev["ts"], had=ev["seq"])


def _open_request(st, sess, tool):
    for req in sess["requests"]:
        if req["state"] in ("requested", "needs-owner") and _named(st, sess, req) == tool:
            return req
    return None
