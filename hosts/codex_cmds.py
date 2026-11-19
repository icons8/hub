"""Codex: commands typed into a Codex session for the codex host
(hosts/codex.py) — the counterpart of what Claude Code does with them itself, without a model turn:
- `/model codex/<id>` and `/effort <level>` — set on the process at once, the next turn/start
  carries them (turn/start.model and .effort, which Codex keeps for the thread; thread/settings/update is the
  experimental API) and the session.info gets them at once. A model is named as the core names a Codex model,
  codex/<id>: the core keeps the owner's /model only in that form (core/reducer._remember_choice), and a bare id would
  bring the session back on another model (on a hub with claude — on claude) after its next start; `default` is not kept
  either — for it the answer names `hub session model|effort <session> default`;
- the effort: Codex takes any effort string, and a turn/start effort reaches the API as is (probe 2026-11-05, a fake
  Responses API: max for gpt-5.5 went out as max) — so an effort the model does not have (model/list:
  supportedReasoningEfforts; 0.162.1: gpt-5.5 has no max and ultra) is refused by `/effort` in words, and whatever
  effort a turn/start would carry (a launch with `hub session effort`, the hub's or an executor's own, a model switch)
  is lowered to the strongest one the model has that is not stronger (fit, before_turn — every turn/start);
- `/clear` — a new thread (thread/start, sessionStartSource "clear") under a new core session id, like Claude Code's
  new conversation; the host answers it as any thread start, with session.started {source: clear}; input typed
  meanwhile waits for the new thread;
- `/compact <text>` — compaction as `hub session compact`; Codex's compaction takes no instructions, the answer says the
  text was not used;
- `/<skill> …` — a skill Codex knows (skills/list of the session folder, enabled; a Claude plugin skill
  `/<plugin>:<skill>` by its own name) goes as a skill input next to the text; any other name stays text, as before.
  Codex does not read a repo's .claude/skills (probe 2026-11-05: not in skills/list, a skill input with such a path is
  ignored), so a repo's human-only skills (/ship) stay text;
- the other Claude Code commands the core may type (core/backbone.BUILTINS, ENDS_SESSION) — an answer that Codex has no
  such command, not a turn of the model.
The answer to a command is a turn.result with no turn (turns 0), as Claude Code gives for its own commands: the core
marks the typed command delivered by it. An answer goes through the host's queue as a line of our own (ANSWER), so it
comes in a poll after the host.done of the typing, like any line of the process.

Seams the host calls:
- intercept(host, name, proc, text, message=None, attachments=()) — first in CodexHost.type, after the "no process" and
  "already sent" checks, with the text as typed: None — the host goes on (a turn with the text); a string — the input
  is handled here and that is type()'s answer (the slice marks the message itself: proc.sent, proc.message_of);
- input(proc, inp, text) — the user input list (UserInput items) the host is about to queue -> the list to queue;
- compact(host, name, proc, text) — `/compact <text>`: None — the host compacts as before (Codex's compaction takes no
  instructions; the text is dropped); a string — handled here, that is compact()'s answer;
- before_turn / notice / response / info / start / compacted — the host's common seams (hosts/codex.py, "Seams").
"""
import re
import sys
import uuid

from core import backbone, i18n, kit

HANDLED = ("model", "effort", "clear", "compact")
# Claude Code's own commands the core may type that Codex has no counterpart for: an answer, not a model turn
NO_CODEX = tuple(sorted((set(backbone.BUILTINS) | set(backbone.ENDS_SESSION)) - set(HANDLED)))
ANSWER = "hub/commandAnswer"   # our own line in the host's queue (never a Codex method: Codex has no "hub/" namespace)
ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")   # Codex's efforts, weaker to stronger
MARK = re.compile(r"(?m)^\[hub:m\d+")   # the core's label after a command's arguments (core/reducer._typed)
PREFIX = "codex/"
# model/list of the hub's Codex: model id -> {"efforts": [...], "default": effort, "is_default": bool, "hidden": bool};
# a hidden one (codex-auto-review and the like) is kept for fitting the effort of a session that runs on it, but /model
# does not take it: the page's menu does not show it (core/codexcli._model)
KNOWN = {}


def _st(proc):
    """This slice's state on the process: the thread's model as Codex reported it, our requests in flight, skills."""
    st = getattr(proc, "cmds", None)
    if st is None:
        st = proc.cmds = {"model": None, "mine": {}, "skills": None, "skill": None, "listed": False, "pages": {},
                          "compacts": []}
    return st


def _model(proc):
    """The model of the next turn: the one we send, otherwise the thread's (Codex's default when none was given)."""
    return proc.model or _st(proc)["model"]


def _efforts(model):
    """The model's efforts the core keeps for a session (reducer._remember_choice: catalog.EFFORTS, as the page's
    commands.model_efforts) — another would hold until the next start and be gone; a model with none of them — all its."""
    from core.catalog import EFFORTS
    entry = KNOWN.get(model or "")
    if not entry:
        return None
    return [e for e in entry["efforts"] if e in EFFORTS] or entry["efforts"]


def fit(model, effort):
    """The effort to send for the model: as is, if the model has it or its efforts are unknown; otherwise the strongest
    it has that is not stronger, or its weakest (an effort outside ORDER: the model's default)."""
    have = _efforts(model)
    if not effort or not have or effort in have:
        return effort
    if effort not in ORDER:
        return KNOWN[model]["default"] if KNOWN[model]["default"] in have else have[0]
    weaker = [e for e in have if e in ORDER and ORDER.index(e) <= ORDER.index(effort)]
    return weaker[-1] if weaker else have[0]


def _fit_proc(proc, name):
    """proc.effort fitted to the model, with a log line if it changed -> did it change."""
    effort = fit(_model(proc), proc.effort)
    if effort == proc.effort:
        return False
    print(f"codex: model {_model(proc)} has no effort {proc.effort} — {name or 'the session'} goes with {effort}",
          file=sys.stderr, flush=True)
    proc.effort = effort
    return True


def _info(proc, name, **changed):
    """A session.info with the changed fields: the core replaces info whole (hosts/codex.py, proc.session_info)."""
    if not proc.session_info:
        return []
    proc.session_info = {**proc.session_info, **changed}
    return [("session.info", name, proc.session_info)]


def _result(name, text, chosen=None):
    """The answer to a command: a result with no turn, as Claude Code gives for its own (/cost, /model…). chosen — the
    /model or /effort the host took ({"model": "codex/<id>"} | {"effort": level}): the core keeps the owner's choice
    for the session only by it (core/reducer._codex_chosen), what the host refused it does not keep."""
    return ("turn.result", name, {"text": text, "subtype": "success", "is_error": False, "duration_ms": 0, "turns": 0,
                                  "cost_usd": None, "tokens_in": 0, "tokens_out": 0, "engine": "codex",
                                  **({"chosen": chosen} if chosen else {})})


def _queue(host, name, text, facts=(), chosen=None):
    with host.lock:
        host.inbox.put((name, {"method": ANSWER, "params": {"text": text, "facts": list(facts),
                                                            **({"chosen": chosen} if chosen else {})}}, host.clock()))


def _say(host, name, proc, message, text, chosen=None, **changed):
    """An answer known at once, with the session.info of the changed fields (see the module's docstring)."""
    if message:
        proc.sent.add(message)
    _queue(host, name, text, _info(proc, name, **changed) if changed else [], chosen)
    return "sent"


def _shown():
    """The models /model offers and takes — those the page's menu shows (not hidden)."""
    return [m for m, entry in KNOWN.items() if not entry.get("hidden")]


def _call(proc, method, body, **record):
    rid = proc.call(method, body)
    _st(proc)["mine"][rid] = {"method": method, **record}
    return rid


def _args(text, cmd):
    """What follows the command, without the core's label and what it appends after it."""
    rest = str(text).lstrip()[len(cmd) + 1:]
    found = MARK.search(rest)
    return (rest[:found.start()] if found else rest).strip()


def _names(items):
    return ", ".join(items)


def _also(text, more):
    """An answer and its second sentence, if there is one."""
    return f"{text} {more}" if more else text


def _busy(proc):
    """A turn runs or is about to (turn/start sent, compaction asked): no new thread and no compaction now."""
    return bool(proc.turn or proc.starting or proc.compacts or proc.compacting)


# --- the seams ---------------------------------------------------------------------------------------------------------
def intercept(host, name, proc, text, message=None, attachments=()):
    st = _st(proc)
    st["skill"] = None
    cmd = backbone.command(text)
    if cmd in ("model", "effort"):
        return _setting(host, name, proc, cmd, _args(text, cmd), message)
    if cmd == "clear":
        return _clear(host, name, proc, message)
    if cmd == "compact":
        return _compact_typed(host, name, proc, _args(text, cmd), message)
    if cmd in NO_CODEX:
        return _say(host, name, proc, message, i18n.t("core.codex_cmds.not_on_codex", command=cmd,
                                                      handled=_names('/' + c for c in HANDLED)))
    skills = st["skills"] or {}
    own = cmd if cmd in skills else (cmd or "").rpartition(":")[2]   # a Claude plugin skill: /<plugin>:<skill>
    if cmd and own in skills:
        st["skill"] = (own, skills[own])
    return None


def input(proc, inp, text):
    st = _st(proc)
    skill, st["skill"] = st["skill"], None
    if not skill:
        return inp
    return [{"type": "skill", "name": skill[0], "path": skill[1]}] + inp


def compact(host, name, proc, text):
    return None


def _setting(host, name, proc, cmd, arg, message):
    """`/model`, `/effort`: the value goes on the process at once, the next turn/start carries it — the answer and
    session.info at once."""
    model, effort = _model(proc), proc.effort
    value = arg.split()[0] if arg else ""
    if cmd == "model":
        if not value:
            return _say(host, name, proc, message, _also(
                i18n.t("core.codex_cmds.model_is", model=PREFIX + (model or i18n.t("core.codex_cmds.codex_default"))),
                _shown() and i18n.t("core.codex_cmds.available", models=_names(PREFIX + m for m in _shown()))))
        if value == "default":   # the core does not keep it for the session (reducer): the next start would undo it
            return _say(host, name, proc, message, i18n.t("core.codex_cmds.model_default", name=name))
        if not value.startswith(PREFIX):
            return _say(host, name, proc, message, i18n.t("core.codex_cmds.model_prefix", model=PREFIX + value))
        value = value[len(PREFIX):]
        if KNOWN and value not in _shown():   # a hidden one too: the menu does not offer it (Codex's own reviewer…)
            return _say(host, name, proc, message, i18n.t("core.codex_cmds.no_model", model=value,
                                                          models=_names(PREFIX + m for m in _shown())))
        proc.model = value
        _fit_proc(proc, name)
        if proc.start_params is not None:
            proc.start_params["model"] = value   # a /clear's new thread and a fresh thread after a lost one take it too
        return _say(host, name, proc, message, _also(
            i18n.t("core.codex_cmds.model_is", model=PREFIX + value),
            proc.effort != effort and i18n.t("core.codex_cmds.effort_lowered", effort=effort, now=proc.effort)),
            chosen={"model": PREFIX + value}, model=value, effort=proc.effort)
    have = _efforts(model)
    if not value:
        return _say(host, name, proc, message, _also(
            i18n.t("core.codex_cmds.effort_is", effort=effort or i18n.t("core.codex_cmds.model_default_effort")),
            have and i18n.t("core.codex_cmds.model_has", model=model, efforts=_names(have))))
    if value == "default":   # as /model default: the core would bring the earlier choice back at the next start
        return _say(host, name, proc, message, i18n.t("core.codex_cmds.effort_default", name=name))
    from core.catalog import EFFORTS
    allowed = have or list(EFFORTS)   # what the core keeps (_efforts); an unknown model — the core's levels
    if value not in allowed:
        return _say(host, name, proc, message, i18n.t("core.codex_cmds.no_effort", model=model or "Codex", effort=value,
                                                      efforts=_names(allowed)))
    proc.effort = value
    return _say(host, name, proc, message, i18n.t("core.codex_cmds.effort_is", effort=value), chosen={"effort": value},
                effort=value)


def _clear(host, name, proc, message):
    """`/clear`: a new thread under a new core session id; input meanwhile waits in the queue (no thread — no turn)."""
    if not proc.thread:
        return _say(host, name, proc, message, i18n.t("core.codex_cmds.clear_no_thread"))
    if _busy(proc):
        return _say(host, name, proc, message, i18n.t("core.codex_cmds.clear_busy"))
    work = getattr(proc, "cx_items", None)   # slice C: a subagent's turn still running in this thread
    if work is not None and work.running:
        # the subagents' lines are routed by the session's thread (hosts/codex.py): a new thread now would take them
        # for the session's own turn. A command left in the background does not hold /clear (as with Claude Code)
        return _say(host, name, proc, message, i18n.t("core.codex_cmds.clear_subagents"))
    if message:
        proc.sent.add(message)
    old, proc.thread = proc.thread, None
    _call(proc, "thread/start", {**(proc.start_params or {}), "sessionStartSource": "clear"}, host=host, old=old)
    return "sent"


def _compact_typed(host, name, proc, text, message):
    """`/compact [text]` typed: compaction as the core's; the answer comes when it is over (compacted) or refused."""
    if _busy(proc) or not proc.thread:
        return _say(host, name, proc, message, i18n.t("core.codex_cmds.compact_busy" if proc.thread
                                                      else "core.codex_cmds.compact_no_thread"))
    host.compact(name)
    if message:
        proc.sent.add(message)
    rid = max((r for r, (m, _) in proc.calls.items() if m == "thread/compact/start"), default=None)
    _st(proc)["compacts"].append({"rid": rid, "text": text, "host": host})
    return "sent"


def _compact_answer(text):
    return _also(i18n.t("core.codex_cmds.compacted"), text and i18n.t("core.codex_cmds.compact_text_unused", text=text))


def before_turn(proc):
    if _fit_proc(proc, None):
        _st(proc)["info_due"] = True   # this seam gives no facts: the session.info goes at the end of the turn


def turn_done(proc, name, turn):
    if not _st(proc).pop("info_due", None):
        return []
    return _info(proc, name, effort=proc.effort)


def start(proc):
    st = _st(proc)
    if not st["listed"]:   # once per process: a new codex binary may know other models
        st["listed"] = True
        _call(proc, "model/list", {"includeHidden": True})
    if st["skills"] is None:
        _list_skills(proc)


def _list_skills(proc):
    cwd = (proc.start_params or {}).get("cwd")
    _call(proc, "skills/list", {"cwds": [cwd]} if cwd else {})


def kit_fact(proc, name):
    """session.kit (core/kit.py) when the set changed: the skills of skills/list and the tool counts codex_mcp saw."""
    rows = _st(proc).get("kit_rows")
    if rows is None:
        return []   # the skills are not listed yet: a set without them would say "no skills"
    got = kit.from_codex(rows, getattr(proc, "kit_mcp", None))
    if got == getattr(proc, "kit_sent", None):
        return []
    proc.kit_sent = got
    return [("session.kit", name, {"engine": "codex", **got})]


def info(proc):
    _fit_proc(proc, None)   # model/list already known (an earlier process): fitted before the first turn/start
    return {"builtins": sorted(set(HANDLED) | set(NO_CODEX)), "effort": proc.effort}


def compacted(proc, name):
    waiting = _st(proc)["compacts"]
    return [_result(name, _compact_answer(waiting.pop(0)["text"]))] if waiting else []


def notice(proc, name, method, params):
    if method == "skills/changed":   # Codex's skill files changed: list them again (the set, /skill)
        _list_skills(proc)
        return []
    if method == ANSWER:
        return [tuple(f) for f in params.get("facts") or []] + [_result(name, params.get("text") or "",
                                                                         params.get("chosen"))]
    return []


def response(proc, name, method, params, msg):
    st = _st(proc)
    res = msg.get("result") if isinstance(msg.get("result"), dict) else {}
    if method in ("thread/start", "thread/resume") and res.get("model"):
        st["model"] = res["model"]   # the thread's model: Codex's default if none was given
    if method == "thread/compact/start" and "error" in msg:
        # a refused compaction never ends: the host would count it as running for good (no /clear, a later turn's own
        # compaction taken for it)
        proc.compacts = max(0, proc.compacts - 1)
        mine = next((c for c in st["compacts"] if c["rid"] == msg.get("id")), None)
        if mine:   # a typed /compact Codex refused: its answer now, not with some later compaction
            st["compacts"].remove(mine)
            why = (msg.get("error") or {}).get("message") or i18n.t("core.codex_cmds.refused")
            _queue(mine["host"], name, i18n.t("core.codex_cmds.compact_refused", why=why))
        return None
    mine = st["mine"].pop(msg.get("id"), None)
    if mine is None:
        return None
    if mine["method"] == "model/list":
        if "error" in msg:   # a listing cut short: the known models stay as they were
            st["pages"] = {}
            return []
        for m in res.get("data") or []:
            # keyed as the page's list is (core/codexcli._model): the model's name, its id when it has none
            if isinstance(m, dict) and (m.get("model") or m.get("id")):
                st["pages"][m.get("model") or m["id"]] = {
                    "efforts": [e.get("reasoningEffort") for e in m.get("supportedReasoningEfforts") or []
                                if isinstance(e, dict) and e.get("reasoningEffort")],
                    "default": m.get("defaultReasoningEffort"), "is_default": bool(m.get("isDefault")),
                    "hidden": m.get("hidden") is True}
        if res.get("nextCursor"):
            _call(proc, "model/list", {"includeHidden": True, "cursor": res["nextCursor"]})
            return []
        if st["pages"]:   # a whole listing: what an older binary had and this one dropped goes too
            KNOWN.clear()
            KNOWN.update(st["pages"])
        st["pages"] = {}
        return _info(proc, name, effort=proc.effort) if _fit_proc(proc, name) else []
    if mine["method"] == "skills/list":
        if "error" in msg:
            st["skills"] = st["skills"] or {}   # as before: a refused listing is not asked again by start
            return []
        rows = [s for entry in res.get("data") or [] if isinstance(entry, dict) for s in entry.get("skills") or []
                if isinstance(s, dict) and s.get("enabled") and s.get("name") and s.get("path")]
        st["skills"] = {s["name"]: s["path"] for s in rows}
        st["kit_rows"] = rows
        return kit_fact(proc, name)
    if mine["method"] == "thread/start":   # /clear: the host answers it as any thread start (source "clear")
        host = mine["host"]
        if "error" in msg:   # the conversation stays: no new thread, the old one goes on
            proc.thread = mine["old"]
            host._flush(proc)
            why = (msg.get("error") or {}).get("message") or i18n.t("core.codex_cmds.refused")
            return [_result(name, i18n.t("core.codex_cmds.clear_failed", why=why))]
        proc.session_id = str(uuid.uuid4())     # the host remembers the thread under it and reports it (session.started)
        _queue(host, name, i18n.t("core.codex_cmds.cleared"))   # after the host's session.started: in the next poll
        return None
    return []
