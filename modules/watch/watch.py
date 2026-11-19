"""Journal watcher: loops, hangs and silences of sessions — reported to the hub. It takes the whole journal as input; when something new
appears it has two actions: tell the hub about trouble or not. A daemon that watches every line of the journal and fires either by
the number of lines or when there were no lines for some time; the collection window is short; it is a system function, not a scheduled job.

A process under hub-up (the `watch` part): it reads the journal on the fly. For each session it accumulates new steps and asks the model
when: BATCH lines have accumulated; or WINDOW_SECONDS have passed since the first new line; or a turn is running but there have been no lines for SILENT seconds.
The model is Haiku via `claude -p` (since 2026-10-05, instead of the previous model; poll intervals increased): one
call decides "is it trouble?" and writes the reason. Trouble is the fact `watch.alert`; the core writes to the hub, no more than once per half hour about one
session (core/reducer.py). The watcher fixes nothing itself: the hub decides.

Switch the model off — `{"off": true}` in `watch.json` of the settings directory (the facts without a model work then too). The surrender detector
(`watch.surrender`) — `{"surrender": true}` there; the reminder about a promise without action (`watch.promise`) —
`{"promise": true}`. State is only in memory: after a restart the past is not rechecked.
"""
import collections
import json
import os
import re
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
from core import catalog, coretext, forms, journal, langdata, minihub, owner, parking, redact, reducer, spool  # noqa: E402
from core import machines  # noqa: E402
from core.coretext import T  # noqa: E402
from core.machines import COMMAND_CONFIRM_SECONDS  # noqa: E402
# the v1.4.0 page server imports this file during the update's self-check with its own core.machines in memory (no
# SLEEP): without the fallback every snapshot failed, "No connection" for the whole check (tests/test_old_page_server)
SLEEP = getattr(machines, "SLEEP", ("asleep", "parked", "retired"))
MCP_WAIT = getattr(parking, "MCP_WAIT", int(catalog.MCP_TIMEOUT) / 1000)

WINDOW = 60          # how many of a session's last steps the model sees
WINDOW_SECONDS = 180  # collection window: from the first new line to the question to the model (was 30 s)
BATCH = 100          # this many new lines — ask without waiting for the end of the window (was 20)
SILENT = 600         # a turn is running but no lines for longer than this — ask (was 300; the same as the Bash command limit)
MAX_CHARS = 12000    # the tail of steps for the model (60 steps are usually 7–9 thousand characters)
ASKS = 2             # questions about steps per pass: Haiku answers in seconds, the urgency of messages must not wait
STALL = 20 * 60      # after an interrupted turn the session is free but there is no new turn for this long — trouble (eval 2026-10-03, item 6)
UNDELIVERED = 150    # a message to a free session has been in the queue longer than this — a fact, without a model
CRASHES = 2          # restarts in a row
OWNER_COOLDOWN = 30 * 60
BG_LONG = 3 * 60 * 60  # a background command runs longer than this and is silent for BG_QUIET — an alarm to the hub (watch for
BG_QUIET = 30 * 60   # hung background processes; a real case — a local server in the background, 100 min without output).
                     # Not 1 h: builds, local CI and lock waits under load stay silent an hour or two and still work
BG_AGENT = 90 * 60   # a background agent does not write its output file — we judge by age
EVERY = 2.0          # how often to look into the journal
ACTIVITY = ("turn.started", "turn.tool", "turn.tool_result", "turn.result", "turn.stopped", "session.said",
            "permission.requested", "host.error", "session.exited", "session.start_stuck",
            "session.interrupt_requested")
KINDS = {
    "ok": "Normal work: different steps, progress, a finished turn, or waiting for the owner's answer.",
    "loop": "Stuck in a loop: the same or nearly the same step repeated many times with no progress.",
    "errors": "Hits the same error again and again, or the turn ended with an error and nothing follows.",
    "hung": "Hung: a turn is in progress but nothing has happened for a long time.",
    "silent": "Went silent: received a message or task and did not start or finish a turn on it.",
}


LANG_NAMES = {"ru": "Russian", "en": "English"}   # the owner's language code (core/forms.py) → the name for the model


def config_path():
    return os.environ.get("HUB_WATCH_CONFIG") or os.path.join(catalog.config_dir(), "watch.json")


def config():
    try:
        with open(config_path(), encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def ask(text):
    """One Haiku question about one session → {"trouble": bool, "kind": a KINDS key, "reason", "advice"}."""
    return _claude(text, WATCH_PROMPT, WATCH_SCHEMA)


def _line(e):
    d, t = e.get("data") or {}, time.strftime("%H:%M:%S", time.localtime(e["ts"]))
    kind = e["type"]
    if kind == "turn.started":
        if _has(d.get("prompt_head"), WAIT_MARKS):
            return f"{t} hub wait check: the session waits for a person's answer on a schedule (2/5/10/20 min…) — normal"
        if _has(d.get("prompt_head"), SCHEDULE_MARKS):
            return f"{t} turn of a schedule job (repeats by plan — normal): {(d.get('prompt_head') or '')[:160]}"
        return f"{t} turn started: {(d.get('prompt_head') or '')[:200]}"
    if kind == "turn.tool":
        inp = d.get("input") or {}
        arg = inp.get("command") or inp.get("file_path") or inp.get("description") or inp.get("prompt") or \
            json.dumps(inp, ensure_ascii=False)
        return f"{t} {d.get('name')}: {str(arg)[:200]}"
    if kind == "turn.tool_result":
        return f"{t}   → {'error' if d.get('error') else 'ok'}: {(d.get('text') or '')[:160]}"
    if kind == "session.said":
        return f"{t} session wrote: {(d.get('text') or '')[:200]}"
    if kind == "turn.result":
        return f"{t} turn ended{' with an error' if d.get('is_error') else ''}: {(d.get('text') or '')[:160]}"
    if kind == "turn.stopped":
        return f"{t} the model finished answering"
    if kind == "session.interrupt_requested":  # run of 2026-09-30: all the "turns with an error" of the day were stops by the owner
        return f"{t} the owner stopped the turn (the next \"turn ended with an error\" is that stop, not a failure)"
    if kind == "permission.requested" and auto_allowed(e):
        return f"{t} the core allowed it itself: {d.get('tool')}"
    if kind == "permission.requested":
        return f"{t} asks the owner: {d.get('tool')}"
    if kind == "host.error":
        return f"{t} host failure: {(d.get('error') or '')[:160]}"
    if kind == "session.exited":
        return f"{t} session process exited: {d.get('why') or ''}"
    if kind == "session.start_stuck":
        return f"{t} session start hung ({d.get('minutes') or '?'} min without SessionStart): the core killed the process and raised it again"
    return None


# what the watcher sees from the core state (Watcher.facts, background): the reason and the advice of watch.alert go to
# the hub as the core's phrases (core/coretext.py) — the session reads the English, the owner his language
STALLED = T("core.watch.stalled", "The turn or the background work broke off together with the process {minutes} min "
            "ago, and there has been no new turn since.")
STALLED_ADVICE = T("core.watch.stalled_advice", "Write to it to continue from where it stopped (hub send) or look at "
                   "hub show.")
UNDELIVERED_WHY = T("core.watch.undelivered", "Message {id} has waited {minutes} min {seconds} s (state {state}, attempt "
                    "{attempt}), and the session is free.")
UNDELIVERED_ADVICE = T("core.watch.undelivered_advice", "Check whether its process is alive; if it does not arrive — hub "
                       "restart or wake it again.")
CRASHLOOP = T("core.watch.crashloop", "Restarts in a row: {count}.")
CRASHLOOP_ADVICE = T("core.watch.crashloop_advice", "Look at ~/.hub-core/logs and the session's stderr.")
COREBUG = T("core.watch.corebug", "Event {type} #{of}: {reason}")
BG_AGENT_WORD = T("core.watch.bg_agent", "a background agent")
BG_COMMAND_WORD = T("core.watch.bg_command", "a background command")
BG_LAST_OUT = T("core.watch.bg_last_out", "; last output {minutes} min ago: \"{line}\"")
BG_NO_OUT = T("core.watch.bg_no_out", "; no output")
BG_WHY = T("core.watch.background", "In the background, {what} \"{title}\" has been running for {dur}{said}.")
BG_ADVICE = T("core.watch.background_advice", "Ask it (hub send) whether this is still needed: a server or a watch "
              "\"until the end\" will not finish by themselves and hold the session \"in the background\" and the hub's "
              "restart. Not needed — let it stop.")


def _dur(s):
    s = int(s)
    return T("core.watch.dur_hours", "{h} h {m} min").format(h=s // 3600, m=s % 3600 // 60) if s >= 3600 else \
        T("core.watch.dur_minutes", "{m} min").format(m=s // 60)


def _tasks_base():
    """Where Claude Code keeps the output of background commands: /tmp/claude-<uid> (macOS — /private/tmp), on Windows — %TEMP%/claude."""
    import tempfile
    if os.name == "nt":
        return os.path.join(tempfile.gettempdir(), "claude")
    return os.path.join("/private/tmp" if sys.platform == "darwin" else "/tmp", f"claude-{os.getuid()}")


def guess_output(sess, tid):
    """The output file of a background command as Claude Code names it: /tmp/claude-<uid>/<folder>/<sessionId>/tasks/<id>.output,
    on Windows — %TEMP%/claude/<folder>/<sessionId>/tasks/<id>.output (there is no os.getuid there: 1182 failures in hub-up.log)."""
    import glob
    slug = re.sub(r"[^A-Za-z0-9]", "-", sess.get("cwd") or "")
    hits = glob.glob(os.path.join(glob.escape(_tasks_base()), glob.escape(slug), sess.get("session_id") or "*", "tasks",
                                  f"{tid}.output"))
    return hits[0] if hits else None


def last_line(path, size=4096):
    """The last non-empty line of the output file (without terminal control characters), up to 200 characters; keys are masked
    before truncation (core/redact.py: a mask no longer recognizes a truncated tail of a key)."""
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - size))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)
    lines = [x.strip() for x in text.replace("\r", "\n").split("\n") if x.strip()]
    return redact.mask_head(lines[-1], 200) if lines else ""


def digest(name, sess, steps, queued, now):
    """The text for the model: the session state and its last steps."""
    quiet = int(now - steps[-1]["ts"]) if steps else 0
    head = f"Session {name}. State: {sess['state']}"
    if sess["state"] == "busy":
        head += f", turn running {int(now - sess['since']) // 60} min"
    if sess.get("pending") or sess["state"] == "dialog":
        head += f"; waits {int(now - sess['since']) // 60} min for the owner's answer to its question — normal"
    if queued:
        head += f"; messages in queue: {queued}"
    if sess.get("background"):
        head += "; waits for its background tasks: " + ", ".join(
            (b.get("title") or b["id"])[:60] for b in sess["background"].values())
    head += f". Last event {quiet // 60} min {quiet % 60} s ago"
    if sess["state"] == "busy" and quiet >= SILENT:
        head += " — silent mid-turn"
    lines = [" ".join(x.split()) for x in (_line(e) for e in steps) if x]
    return head + ".\n" + "\n".join(lines)[-MAX_CHARS:]


WATCH_PROMPT = (
    "You watch a fleet of autonomous agent sessions (Claude Code under a hub). The input is the state and the last steps "
    "of one session. Decide whether the session needs a supervisor's help right now. Almost always — no. Trouble: "
    "going in circles (the same steps without progress), hitting the same error again and again, hung mid-turn, gone "
    "silent. Not trouble: different steps with progress; one or two errors after which it changes approach; a long "
    "command (tests, a build, waiting for a background task) — even if it is silent for minutes; a finished turn; a "
    "turn stopped by the owner; waiting for the owner's answer. trouble=true — only when the steps hold clear facts of "
    "trouble. kind — ok if there is no trouble. reason — one or two sentences with facts from the steps, in the owner's "
    "language (the input line \"Owner's language\"); advice — what the supervisor should do, one sentence (empty "
    "strings if no trouble). Answer with JSON only.")
WATCH_SCHEMA = {"type": "object", "required": ["trouble", "kind", "reason", "advice"], "properties": {
    "trouble": {"type": "boolean"}, "kind": {"type": "string", "enum": list(KINDS)},
    "reason": {"type": "string"}, "advice": {"type": "string"}}}


# The "what it is working on" line: at the start of a turn — at once, from the task; later it is reconciled with the steps (as soon as
# the task is understood, write what it is doing; if it moves on — reconcile).
RECAP_EVERY = 90    # reconcile during the turn — no more often than this (seconds)
RECAP_STEPS = 4     # and not before this many new steps have accumulated
RECAP_STEPS_SEEN = 12  # the model sees this many last steps and RECAP_TASK characters of the task (was 30 and 1500): thinking
RECAP_TASK = 500       # is removed — the same line for $0.003 instead of $0.01 (a trial on 6 turns)
# the Russian examples of the form (the third person, "taking on") — from the language data, as the prompt had them
_RECAP_RU = langdata.get("ru", "words", "recap_examples") or {}
_RECAP_DOING = (f"; in Russian «{_RECAP_RU['doing']}», not «{_RECAP_RU['not']}»" if {"doing", "not"} <= set(_RECAP_RU)
                else "; in a language where the verb has persons — its third person singular, not the first")
_RECAP_TAKING = f", «{_RECAP_RU['taking']}»" if "taking" in _RECAP_RU else ""
RECAP_PROMPT = ("The input is the task of an AI agent session's current turn and its last steps. One short line in "
                "the owner's language (the input line \"Owner's language\"), at most 12 words — drop details: what it is "
                "working on right now, starting with a present-tense verb about it in the third person (\"Fixing the "
                f"feed loading on the page\"{_RECAP_DOING}). Only what the steps show. If the input says \"No steps "
                "yet\" — the turn has just begun and what it does is unknown: write what it is taking on, not what it does "
                f"(\"Taking on the feed loading on the page\"{_RECAP_TAKING}), with no verbs of editing, running or "
                "deleting. No quotes, no generic words, no \"session\", no \"working on a task\". Answer with JSON only.")
RECAP_SCHEMA = {"type": "object", "required": ["now"], "properties": {"now": {"type": "string"}}}
# the line model did not answer for this session (claude -p limit, network) — the next call is not on the next pass (once per EVERY),
# but after a pause: RECAP_PAUSE, then doubled, no more than RECAP_PAUSE_MAX; while the core sees an account refusal until the window reset
# (limits) — until the reset, also across turns. A successful answer, the pool switching to another account, the end of a turn (except one waiting for a reset)
# lift the pause.
RECAP_PAUSE = 60
RECAP_PAUSE_MAX = 1800


def recap_model(text):
    """recap — Haiku via `claude -p`, like the watcher's questions."""
    return clean_recap(_claude(text, RECAP_PROMPT, RECAP_SCHEMA)["now"])


def clean_recap(text):
    """Haiku sometimes puts another JSON into the line ({"activity": "…"}, seen live) — take the text from it. The JSON can be
    broken ({"status": "…"}} with an extra brace) — then the first string value, not the whole line for the page."""
    t = re.sub(r"^```\w*|```$", "", str(text or "").strip()).strip()
    if t.startswith("{"):
        try:
            obj = json.JSONDecoder().raw_decode(t)[0]   # the tail after the object ({…}}) does not matter
            t = next((v for v in obj.values() if isinstance(v, str)), "") if isinstance(obj, dict) else ""
        except ValueError:
            m = re.search(r'"\w+"\s*:\s*"((?:[^"\\]|\\.)*)', t)
            t = m.group(1) if m else t.strip("{}[] \n")
    return t.strip().strip("«»\"'")


# By default Claude Code lets Haiku think (1.4–2.6 thousand tokens out of a 1.5–3.2 thousand answer, 15–30 s) and
# writes the cache for an hour at double price — a yes/no question needs neither. MAX_THINKING_TOKENS=0 turned off
# thinking in Haiku 4.5; for Haiku 5.5 the CLI no longer accepts such disabling (rejects_disabled_thinking) and the flag is
# silently ignored — it was replaced by --effort low on the call itself (below): less thinking, not switched off
# completely, but the output is shorter by ~20–30%. recap too: Haiku writes a status JSON into the line even with thinking, it is
# unwrapped by clean_recap
QUICK = {"DISABLE_PROMPT_CACHING": "1"}


POOL = {"active": None}   # the pool account the sessions are on now (Watcher.feed from the journal)


def model_env(base=None, account=None):
    """The environment of the core's own claude -p (the watcher, hubmod.model through core/modelcall, the critic, the
    stops audit): the owner's login and network, without the hub variables, on the current pool account. POOL is filled
    only by the watcher's feed: in another process (web — the push summary, Telegram — the voice reply) the account is given by the caller from
    its own core state (without it the call went on the machine's login, whose week had run out). The login goes through the facade
    core/profiles.py (the pool in the core tree or a login module). No model remap of the sessions (catalog.service_env):
    the call asks for haiku and gets haiku — unless the account's login turns on a cloud provider of its own (a login
    module's profile with Bedrock, Vertex), where the names are how the provider's models are named."""
    from core import modlaunch, profiles
    base = os.environ if base is None else base
    # a process of the previous release (the page server, Telegram) keeps its catalog in memory and may load this file
    # from disk first: without service_env there — the environment as that release built it
    service = hasattr(catalog, "service_env")
    env = catalog.service_env(base) if service else {**base, **catalog.user_env()}
    env = {k: v for k, v in env.items() if not k.startswith("HUB_")}
    cloud = modlaunch.provider(env)
    env = profiles.model_env(env, account or POOL["active"])
    if service and modlaunch.provider(env) and not cloud:   # the profile's own provider: the remap as its sessions have it
        full = {**base, **catalog.user_env()}
        env = {**{k: v for k, v in full.items() if catalog.model_remap(k)}, **env}
    return env


def _claude(text, prompt, schema, quick=True, account=None, by="watch"):
    from core import codexmodel
    if codexmodel.on_codex():   # Codex (experimental): a hub on Codex — the core's Codex model, no claude
        return codexmodel.ask(text, prompt, schema, by=by)
    if os.environ.get("HUB_NO_MODEL"):   # tests: do not call the live model
        raise ValueError("claude -p: the model is off (HUB_NO_MODEL)")
    import subprocess
    import tempfile
    from core import realclaude
    claude = realclaude.find()   # from a session, "claude" in PATH is its wrapper
    try:
        r = subprocess.run([claude, "-p", "--model", "haiku", *(["--effort", "low"] if quick else []),
                            "--tools", "", "--setting-sources", "",
                            "--strict-mcp-config", "--no-session-persistence", "--output-format", "json",
                            "--system-prompt", prompt, "--json-schema", json.dumps(schema)],
                           input=text, capture_output=True, text=True, timeout=90, cwd=tempfile.gettempdir(),
                           env={**model_env(account=account), **(QUICK if quick else {})})
    except subprocess.TimeoutExpired as e:
        raise OSError(f"claude -p: no answer within {e.timeout} s") from e
    out = json.loads(r.stdout)
    if out.get("is_error") or not isinstance(out.get("structured_output"), dict):
        raise ValueError(f"claude -p: {str(out.get('result'))[:200]}")
    return out["structured_output"]


NEEDS_MIN = 80       # shorter — we do not ask ("done", "accepted")
NEEDS_PROMPT = (
    "The input is the end of an agent session's turn result that it sent to the owner. Decide whether the result "
    "waits for something from the owner: a decision, an answer, a \"yes\", a choice, an action only they can do. The "
    "session works with other sessions and agents, and most of what it waits for comes from them. needs — true only "
    "if the work can't go on right now without the owner: the text asks the owner directly (a question, an "
    "imperative, \"waiting for your answer\", the owner named as the one to act or answer — even next to other "
    "waits) or needs what only a person can give (a decision, an approval, a login, a payment, a physical action). "
    "False: a report of what was done, a promise to do it itself, an obstacle reported with the session's own retry "
    "or scheduled step and no ask of the owner (\"locked now; sends at 9:00\"), \"nothing is needed from you\", a future step "
    "(\"prod — only after your approval\", \"your yes, on the card X brings\") when nothing is needed now, an action the owner can take only after "
    "what still runs (\"the job ends at 16:30, then you take the file\") even if listed as needed, and a wait "
    "on what other sessions, agents or "
    "processes produce, where the owner is not named as the one to give it — \"waiting for X\" with no addressee "
    "is such a wait, never turn it into a request to the owner. ask — what exactly is needed from the owner, one "
    "short line in the owner's language (the input line \"Owner's language\"), starting with a verb (\"Say yes to "
    "merging the MR\"); if needs=false — an empty string. Answer with JSON only.")
NEEDS_SCHEMA = {"type": "object", "required": ["needs", "ask"], "properties": {
    "needs": {"type": "boolean"}, "ask": {"type": "string"}}}


NEEDS_TAIL = 900     # how many characters from the end of the result we judge: there the session says what it waits for


def ending(text):
    """The end of a result — the last paragraphs up to NEEDS_TAIL characters (so that a session is not asked "an answer?" by a phrase from the middle of
    the result when it ended with advice — "leave it, but more precisely")."""
    paras = [p for p in text.strip().split("\n\n") if p.strip()]
    out = ""
    for p in reversed(paras):
        if out and len(p) + len(out) + 2 > NEEDS_TAIL:
            break
        out = p + ("\n\n" + out if out else "")
    return out[-NEEDS_TAIL:]


TAIL = 300            # characters of a result the core keeps with a mark (the owner reads it as the detail of the item)
SENTENCE = re.compile(r"(?<=[.!?…])\s+")


def tail(text, limit=TAIL):
    """The end of a result up to limit characters, starting where a line or a sentence starts: a cut from the middle of a
    word ("oogle asks…") reads as garbage. The last sentence alone is longer — its beginning, cut at the end with "…"."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    parts = [s for line in text.splitlines() for s in SENTENCE.split(" ".join(line.split())) if s]
    out = ""
    for s in reversed(parts):
        if len(s) + len(out) + (1 if out else 0) > limit:
            break
        out = s + (" " + out if out else "")
    return out or parts[-1][:limit - 1].rstrip() + "…"


CARD_ID = re.compile(r"\bq\d+\b")
CARDS_MAX = 8
# the rule rides with the block, not in NEEDS_PROMPT: a result with no open cards gets the same input as before (eval: the
# longer prompt made the ownerless "waiting for frames" a false mark in 4 of 20)
CARDS_HEAD = ("OPEN CARDS: question cards to the owner that are open now; he sees them on the page. Waiting for the "
              "answer to one of them, or a reminder of it, is not a new need: needs=false, unless the text also needs "
              "something else from the owner that no listed card covers.\n")
# A result is held back from the model by core facts, and judged once they pass if no newer result replaced it: the
# session has an open question card of its own — the page already shows it as waiting for him, with the card's
# question; the model had the card in OPEN CARDS and still marked (a week of 1385 marks: 231 with an open card of their
# own, 162 of them only a reminder of it; 21 asked for something else too — the answer to the card usually starts a
# new turn, and that turn's result is judged as usual, without the card); or the
# owner dismissed its mark less than DISMISS_HOLD ago and has not written to it or answered its card since — a session
# woken by others ends every few minutes with the same list, and the mark he dismissed came back (198 marks of that
# week within an hour of a click, none of the 80 labeled ones a new need; a three-hour hold would have held 3)
DISMISS_HOLD = 3600


def open_cards(state, name, text, now):
    """The OPEN CARDS block for the needs question: another session's open question card the result names by id (a
    session with an open card of its own is not asked about, Watcher.owner_needed). The owner sees them on the page, so a
    result that only restates one is not a new wait. No cards - an empty string."""
    named = set(CARD_ID.findall(text))
    rows = [f"- {ask['id']} (session {sess['name']}): {' '.join(owner.question_text(ask).split())[:160]}"
            for sess in state["sessions"].values() if sess["name"] != name
            for ask in owner.open_asks(sess, now) if ask["id"] in named]
    return CARDS_HEAD + "\n".join(rows[:CARDS_MAX]) + "\n\n" if rows else ""


def needs_owner(text):
    """Haiku: whether the turn result waits for something from the owner → {"needs": bool, "ask": what is needed in one line}."""
    return _claude(text, NEEDS_PROMPT, NEEDS_SCHEMA)


URGENT = 0.6  # from this score a message is inserted in the middle of a turn
URGENT_PROMPT = (
    "An autonomous agent is in the middle of work (its last steps below). A new message came to it (at the end). Deliver "
    "it right now, mid-work, if it changes or stops what the agent is doing, corrects it, answers what it is waiting "
    "for, or is urgent. Wait for the end of the work if it is a new separate task, info for later, thanks. p — the "
    "estimate from 0 to 1 that it must be delivered now. Answer with JSON only.")
URGENT_SCHEMA = {"type": "object", "required": ["p"], "properties": {"p": {"type": "number"}}}


def urgent(text):
    """Haiku: insert the message into a busy session now or let it wait for the end of the turn → 0..1."""
    return max(0.0, min(1.0, float(_claude(text, URGENT_PROMPT, URGENT_SCHEMA)["p"])))


# the phrases of the rules below are language data (core/langdata.py, `watch`): every language with data, English after it
STATUS_Q = re.compile(langdata.alt("watch", "status_q"), re.I)


# a service note of the hub to a session: a card waits for "yes", connected / requested for you, clones removed, sessions cleaned up
HUB_SERVICE = re.compile(langdata.alt("watch", "hub_service"), re.I)
# ...but not an instruction: about the owner, a stop, urgency — the model decides that
HUB_ORDER = re.compile(langdata.alt("watch", "hub_order"), re.I)


def urgency_rule(m, hub=None):
    """Urgency by a rule, before the model (eval 2026-10-03, item 8: 8 of 12 false insertions were core service messages; the owner's "what are you doing
    now?" was missed; later a decision: "0.6 + hub service"): core service and a hub service note — 0,
    a short owner question about status — 1, the rest is decided by the model (None)."""
    origin, text = m.get("origin") or "", (m.get("text") or "").strip()
    f = forms.sender(m)   # the from field, without it — the label (both forms of core/forms.py)
    if f["kind"] == "core" if f else origin.startswith(forms.CORE_ORIGINS):
        return 0.0
    by_hub = f.get("name") == hub if f else any(origin.startswith(f"{b}{hub}") for b in forms.MARKER_BY)
    if hub and by_hub and HUB_SERVICE.search(text) and not HUB_ORDER.search(text):
        return 0.0
    owner = f["kind"] == "owner" if f else origin.startswith(forms.OWNER_ORIGINS)
    if owner and len(text) <= 80 and STATUS_Q.search(text):
        return 1.0
    return None


# Surrender detector (optional, `"surrender": true` in watch.json): the owner doubted, and the session in the same turn
# gave in without checking the disputed point. By rules, without a model: the doubt is in his message, the surrender is at the start of the result,
# the check is at least one step other than edits, the to-do list and hub commands.
DOUBT = re.compile(langdata.alt("watch", "doubt") + r"|are you sure|\bwrong\b", re.I)
SURRENDER = re.compile(langdata.alt("watch", "surrender") + r"|you'?re right|good catch|my mistake", re.I)
RECHECK = re.compile(langdata.alt("watch", "recheck") + r"|re-?checked", re.I)
SURRENDER_HEAD = 400   # the surrender is at the start of the result
NOT_A_CHECK = ("Edit", "Write", "MultiEdit", "NotebookEdit", "TodoWrite")


def doubt(text):
    return bool(DOUBT.search(text or ""))


def surrender(text):
    text = text or ""
    return bool(SURRENDER.search(text[:SURRENDER_HEAD])) and not RECHECK.search(text)


def checked(tools):
    """The steps of the turn checked something: reading, search, a command, an external source — not an edit and not a hub command."""
    for t in tools:
        cmd = str((t.get("input") or {}).get("command") or "").strip()
        if t.get("name") in NOT_A_CHECK or (t.get("name") == "Bash" and re.match(r"(\S*/)?hub\s", cmd)):
            continue
        return True
    return False


# A promise without action (optional, `"promise": true` in watch.json): a turn
# ended with "I take X next", but nothing is launched in the turn and nothing in flight will do it — the core reminds the session
# (core/reducer.py, watch.promise). From a review of a week of the journal 2026-10-18..25 (11,608 results, 34 real promises): a gate
# by rule — an own action verb in the last five sentences of the end of the result, without a condition; forwarding verbs
# ("I will send, pass on, forward") do not open the gate — their precision is about a third (the ritual "I will forward the report"); then
# Haiku PROMISE_VOTES times in a row over the core facts — it fires only if all votes are "a promise, not covered"
# (3 of 3: as many known real ones as with 2 of 3, and 49 firings instead of 62 per week).
PROMISE_PROMPT = (
    "The input is the end of an AI agent session's turn result. The turn is over: the session now sleeps and does "
    "nothing until something wakes it. Decide whether the text ends with a PROMISE — a statement in the session's OWN "
    "voice (I / we) that IT will do a concrete action of its own next, with no condition and no event it waits for. "
    "promise=true: \"taking X next\", \"launching it now\", \"I'll look into it\", \"I'll file a separate task\", \"next I fix Y\", "
    "\"I'll bring the cards\". promise=false: (1) a report of what was done or what is running now; (2) a wait for an "
    "event or a person: CI or a command in the background, an executor's or another session's reply, a schedule or a "
    "clock time, the owner's answer, an approval, \"after you say yes\", \"if you agree\", \"when X finishes\", \"waiting for "
    "the window\"; (3) steps that someone else will take — an executor, another session, an agent, \"the team\" — or a "
    "plan of the whole project that the session only narrates; (4) a question, options or a proposal for the owner; "
    "(5) lessons, notes, rules the session wrote down; (6) a vague \"later\" or \"if needed\" without a commitment. "
    "what — the promised action in a few words, in the text's language; empty if promise=false. Answer with JSON only. Examples. "
    "promise=true: \"Taking the page freeze next.\" / \"Now I look at what fills the disk.\" / \"I'll file a separate "
    "task for it.\" promise=false: \"Next the executor opens the MR, then I forward the link.\" (others act) / \"If you agree, I'll "
    "prepare the spec.\" (condition) / \"Next step — devops stops the sync.\" (third party) / \"Order: A, then B, then C.\" (a plan "
    "the session only narrates) / \"Lesson: check X first.\" (a note). When in doubt, promise=false: the check wakes an "
    "idle session, a false alarm costs more than a miss. The input also has a block FACTS: what the core knows is in "
    "flight for this session right now (a background command, an executor it waits for, a scheduled job, its own wait). "
    "covered=true only if the promised action is clearly the same thing that is in flight or is brought by it (the "
    "promised check is that scheduled job, the promised result is what that executor returns, the promised wait is that "
    "background command). Something in flight that has nothing to do with the promised action — a recurring job about "
    "something else, an executor on another task — does not cover it: covered=false. covered=false if promise=false. "
    "Answer with JSON only.")
PROMISE_SCHEMA = {"type": "object", "required": ["promise", "covered", "what"], "properties": {
    "promise": {"type": "boolean"}, "covered": {"type": "boolean"}, "what": {"type": "string"}}}
PROMISE_VOTES = 3     # this many answers "a promise, not covered" in a row — a firing; the first "no" — we do not ask further
PROMISE_DELAY = 10    # since the result — wait: the message that wakes it arrives within seconds (slash commands, replies)
PROMISE_TRIES = 3     # the model did not answer — repeat after WINDOW_SECONDS, no more than this many times per result
# a sample of rejected ones goes to the journal (watch.promise_skipped), to count the misses in a week: the share by stage
PROMISE_SAMPLE = {"gate": 0.02, "model": 0.25}
PROMISE_VERBS = re.compile(r"\b(" + langdata.alt("watch", "promise_verbs") + r")\w*", re.I)
PROMISE_NEXT = re.compile("(" + langdata.alt("watch", "promise_next") + ")", re.I)
PROMISE_RELAY = re.compile("(" + langdata.alt("watch", "promise_relay") + ")", re.I)
PROMISE_IF = re.compile(r"\b(" + langdata.alt("watch", "promise_if") + r")\b", re.I)


def promise_gate(text):
    """The gate before the model: in the last five sentences of the end of the result — an own action verb (not only forwarding) or
    "next I take / then I ...", and the same sentence has no condition or waiting."""
    t = " ".join(ending(text or "").split())
    for s in re.split(r"(?<=[.!?…])\s+", t)[-5:]:
        work = PROMISE_NEXT.search(s) or any(not PROMISE_RELAY.match(m.group(0)) for m in PROMISE_VERBS.finditer(s))
        if work and not PROMISE_IF.search(s):
            return True
    return False


def promise_facts(st, name, now):
    """What is in flight for a session — a list for the model: background, executors, whose answer it waits for, its schedule jobs,
    its `hub wait`, an open question to the owner. Whether this covers what was promised is decided by the model (the core rule hid 32 of 34
    real ones: almost everyone has something hanging, and it is not about the promised)."""
    from core import schedule, tools

    def short(s, n=110):
        return redact.mask_head(" ".join(str(s or "").split()), n)
    sess, out = st["sessions"].get(name) or {}, []
    for b in (sess.get("background") or {}).values():
        out.append(f"- background {b.get('kind') or 'command'} running {max(0, int((now - b.get('since', now)) / 60))} "
                   f"min: «{short(b.get('title') or b.get('id'))}»")
    for d in st["delegations"].values():
        if d.get("for_session") == name and d["state"] in ("working", "waiting-owner"):
            out.append(f"- it is waiting for the answer of executor {d['session']} (task {d['id']}): "
                       f"«{short(d.get('words') or d.get('context'))}»")
    for j in (st.get("schedule") or {}).values():
        if j.get("state") == "active" and schedule.wakes(j, name):
            when = "one-shot" if j.get("kind") == "at" else f"recurring {j.get('kind')}"
            out.append(f"- scheduled job «{j['id']}» ({when}) wakes it: «{short(j.get('prompt') or j.get('command'), 90)}»")
    w = tools.waiting_now(sess)
    if w:
        out.append(f"- its own `hub wait`: «{short(w.get('text'))}»")
    if owner.open_asks(sess, now) or any(r.get("state") == "needs-owner" for r in sess.get("requests") or []):
        out.append("- an open question card to the owner is waiting for an answer")
    return "\n".join(out) or "- nothing: no background command, no executor it waits for, no scheduled job, no wait"


def promise_model(text):
    """Haiku: whether the result ends with a promise of its own action and whether what is in flight covers it."""
    return _claude(text, PROMISE_PROMPT, PROMISE_SCHEMA)


def sampled(seq, share):
    """Whether the result seq goes into the sample with share — by number, without randomness: the same result when repeated."""
    return isinstance(seq, int) and (seq * 2654435761) % 1000 < share * 1000


WAIT_MARKS = forms.WAIT_MARKS  # the start of the `hub wait` check message (bin/hub), both forms
SCHEDULE_MARKS = forms.SCHEDULE_MARKS  # the label of a schedule job message, both forms


def _has(text, marks):
    return any(m in (text or "") for m in marks)
HUNG_MIN = 120       # "hung" — not earlier than this many seconds without events
HUNG_BROWSER = 200   # after a browser call — longer: its commands have their own timeout of about 3 minutes
HUNG_SCHEDULE = 600  # a scheduled turn: nobody waits for it, turns of 5–6 minutes without steps happen
HUNG_AGENT = 30 * 60  # a nested agent without background: its steps do not go to the journal, it works for tens of minutes
ERRORS_MIN = 2       # "stuck on errors" — at least this many errors among the last steps


EDITS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
LOOP_SAME = 3        # a loop — this many identical calls (name + input) in one turn without a file edit between them
LOOP_LIKE = 4        # ...or this many commands with one start ("corepack pnpm install" with different flags)


def _heads(cmd):
    """Command starts in a Bash line: three words of each part, without assignments (X=… cmd, export X=…)."""
    out = set()
    cmd = re.sub(r"\$\([^)]*\)|\"[^\"]*\"|'[^']*'", "''", cmd or "")
    for part in re.split(r"[;&|\n]+", cmd):
        words = [w for w in part.split() if not re.match(r"^[A-Za-z_][A-Za-z_0-9]*=", w)]
        if words and words[0] not in ("export", "cd", "mkdir", "echo", "set", "head", "tail", "grep", "cat"):
            out.add(" ".join(words[:3]))
    return out


def _work_edit(d):
    """An edit of a work file (not a temporary one): a command repeated between such edits is an "edit → check" cycle."""
    path = str((d.get("input") or {}).get("file_path") or (d.get("input") or {}).get("notebook_path") or "")
    return d.get("name") in EDITS and not path.startswith(("/tmp/", "/private/tmp/", "/var/folders/"))


def _current_turn(steps):
    """The start of the current turn and its steps (steps — a list or a deque of the watcher)."""
    steps = list(steps)
    i = max((k for k, e in enumerate(steps) if e["type"] == "turn.started"), default=-1)
    return (steps[i] if i >= 0 else None), steps[i + 1:]


def running(last):
    """How many seconds of silence are legitimate: the last step is a call without an answer, it is still within its timeout (2026-10-03..05: all three
    "hung" — a full test run longer than 5 minutes; for Bash the limit is its timeout, 2 minutes by default)."""
    if not last or last["type"] != "turn.tool":
        return 0
    d = last.get("data") or {}
    if d.get("name") == "Bash":
        try:
            limit = int((d.get("input") or {}).get("timeout") or 120000) / 1000
        except (TypeError, ValueError):
            limit = 120
        return min(limit, 600) + 60
    if d.get("name") in ("Agent", "Task"):
        return HUNG_AGENT
    return 0


def auto_allowed(e):
    """A request the core answered by itself (permissions.auto_allow): the host mark, as in core/reducer."""
    return e.get("type") == "permission.requested" and e.get("src") == "host" \
        and (e.get("data") or {}).get("auto") == "allow"


def plausible(kind, steps, now, sess=None):
    """Checking by facts what the models said (2026-09-30: five false alarms in an evening — "hung" on pauses of 4–7 s,
    deliberate waits in the browser, one command error; "hung" 73% on a session that waited 26 minutes for the owner's answer
    to a form question). Eval of 2026-10-03 over the journal 09-30..10-03: of 23 alarms 5 were right — the rules below."""
    sess = sess or {}
    if sess.get("kind") == "hub" and kind != "hung":
        return False  # the hub retells others' troubles — by its steps we judge only "hung" (eval 2026-10-03, item 1)
    if kind in ("hung", "silent") and (sess.get("pending") or sess.get("state") == "dialog"):
        return False  # waiting for the owner's answer — this is neither a hang nor a silence
    if kind == "hung":
        if sess.get("background"):
            return False  # waiting for its own background agent or command (eval 2026-10-03, item 5)
        head, cur = _current_turn(steps)
        asked = [e for e in cur if e["type"] in ("permission.requested", "permission.answered")
                 and not auto_allowed(e)]   # allowed by the core itself — does not wait for the owner
        if asked and asked[-1]["type"] == "permission.requested":
            return False  # a form question in the middle of a turn is still unanswered (eval 2026-10-03, case a)
        if any(e["type"] == "turn.tool_result" and "Async agent launched" in str((e.get("data") or {}).get("text"))
               for e in cur):
            return False  # its own background agent in this turn: Claude Code will wake it when it finishes (same place, case a)
        last = steps[-1] if steps else None
        if last and last["type"] == "permission.requested" and not auto_allowed(last):
            return False  # waiting for the owner's permission for a command (eval 2026-10-03, item 4)
        quiet = now - last["ts"] if last else 0
        browser = last and last["type"] == "turn.tool" and "browser" in str((last.get("data") or {}).get("name"))
        scheduled = _has(((head or {}).get("data") or {}).get("prompt_head"), SCHEDULE_MARKS)
        # a scheduled turn is waited for by nobody: a long one is not yet trouble (eval 2026-10-03, case b)
        return quiet >= max(HUNG_SCHEDULE if scheduled else HUNG_BROWSER if browser else HUNG_MIN, running(last))
    if kind == "loop":
        # a loop — its own steps going in circles inside one turn (eval 2026-10-03, items 2–3): scheduled turns and hub wait checks
        # are never a loop; edit → test → edit is work; the last one — the process exit (a restart) — is not a loop
        if steps and steps[-1]["type"] in ("session.exited", "session.start_stuck"):
            return False
        head, cur = _current_turn(steps)
        prompt = ((head or {}).get("data") or {}).get("prompt_head") or ""
        if _has(prompt, SCHEDULE_MARKS + WAIT_MARKS):
            return False
        seen, like = {}, {}
        for e in cur:
            d = e.get("data") or {}
            if e["type"] != "turn.tool":
                continue
            if _work_edit(d):
                seen.clear()
                like.clear()
                continue
            sig = (d.get("name"), json.dumps(d.get("input"), sort_keys=True, ensure_ascii=False))
            seen[sig] = seen.get(sig, 0) + 1
            if seen[sig] >= LOOP_SAME:
                return True
            if d.get("name") == "Bash":  # 2026-09-30: nine pnpm install with different flags, a hook bypass by a script in /tmp
                for h in _heads((d.get("input") or {}).get("command")):
                    like[h] = like.get(h, 0) + 1
                    if like[h] >= LOOP_LIKE:
                        return True
        return False
    if kind == "errors":
        recent = steps[-12:]
        return sum(1 for e in recent if e["type"] == "turn.tool_result" and (e.get("data") or {}).get("error")) \
            + sum(1 for e in recent if e["type"] == "turn.result" and (e.get("data") or {}).get("is_error")) >= ERRORS_MIN
    return True


def calm(sess, steps):
    """The session is not in a turn, and the last thing is an ordinary end of a turn without errors: there is no loop, hang or silence here, and
    undelivered and interrupted are facts without a model. Such windows were the most frequent questions."""
    if sess.get("state") == "busy" or not steps:
        return False
    last = steps[-1]
    d = last.get("data") or {}
    if last["type"] == "turn.result":
        return not d.get("is_error")
    return last["type"] in ("turn.stopped", "session.said") and not any(
        e["type"] == "turn.result" and (e.get("data") or {}).get("is_error") for e in steps[-3:])


class Watcher:
    def __init__(self, state_dir, cfg=None, ask=ask, needs=needs_owner, urgent=urgent, recap=None, promise=promise_model):
        self.state_dir, self.cfg, self.ask = state_dir, cfg, ask
        self.promise = promise
        self.promises = {}  # session -> the last turn result not yet checked for a promise without action
        self.own = {}       # session -> whether the running turn is its own (own_turn at its start)
        self.doubted = {}   # session -> the owner's message with a doubt that this turn started from (or none)
        self.verified = {}  # session -> whether it checked anything in this turn since then (the steps window does not hold on a long turn)
        self.recap = recap or recap_model
        self.recapped = {}  # session -> (when, how many steps there were) of the last recap
        self.recap_pause = {}   # session -> {until, n, account, reset} — failures of the line model (RECAP_PAUSE)
        self._lang = (None, "")  # (event number, the owner's language line) — lang_line
        self.urgent, self.weighed = urgent, set()  # messages in the queue that the model has already been asked about
        self.needs = needs
        self.results = {}   # session -> the last turn result not yet checked for "waits for the owner"
        self.dismissed = {}  # session -> when the owner dismissed its "answer?" mark (until he writes to it; DISMISS_HOLD)
        self.told = {}      # what has already been said: (session, what) -> when / number
        self.bugs = []      # core failure deviations not yet reported
        self.path = os.path.join(state_dir, "journal.jsonl")
        self.offset = 0
        self.state = reducer.empty()
        self.steps = {}     # session -> the last WINDOW steps
        self.counted = {}   # session -> how many steps in total (the steps window does not grow on a long turn)
        self.turn_recap = {}  # session -> seq of the turn for which the task line already exists
        self.fresh = {}     # session -> {"n": new lines, "since": when the first one came}
        self.checked = {}   # session -> when it was last asked about silence (or a line came)
        self.started = False
        self.cut = {}       # session -> when its turn or background work was cut off together with the process
        self.yielded = []   # (session, owner's message): a surrender without a check, not yet sent
        self.seen_at = None  # the time of the last journal line read: the core uses it to tell a stale fact

    def read(self):
        from core import journal
        ino = journal.identity(self.path)
        out = []
        if ino != getattr(self, "ino", ino):  # the journal was started anew (journal.rotate)
            # rotation of the file we were reading: read the rest of the archive from our place and stand on the "rotate" snapshot — what was carried over
            # is not fed again (step windows and counters doubled); if that fails — from the start of the new one, the repeated part is the past
            got = journal.go_on(self.path, self.ino, self.offset, ino)
            if got is not None:
                out, self.offset = got
            else:
                self.offset, self.state, self.started = 0, reducer.empty(), False
        self.ino = ino
        try:
            with open(self.path, "rb") as f:
                if os.fstat(f.fileno()).st_ino != ino:   # while deciding there was another rotation: this file — next time
                    return out
                f.seek(self.offset)
                chunk = f.read()
        except FileNotFoundError:
            return out
        end = chunk.rfind(b"\n")
        if end < 0:
            return out
        self.offset += end + 1
        for line in chunk[:end].split(b"\n"):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out

    def feed(self, e, now, known=None):
        self.seen_at = e.get("ts", self.seen_at)
        if e["type"] == "core.snapshot" and (e.get("data") or {}).get("state"):
            self.state = reducer.restore(e)
            POOL["active"] = (self.state.get("account") or {}).get("active")
            return
        name = e.get("entity")
        before = self.state["sessions"].get(name or "") or {}
        if e["type"] == "session.exited" and (before.get("state") in ("busy", "dialog") or before.get("background")):
            self.cut[name] = e["ts"]  # the turn or background was cut off — if the session then stops, it is trouble (eval 2026-10-03, item 6)
        elif e["type"] == "turn.started":
            self.cut.pop(name, None)
        marked = bool(before.get("attention"))   # read before the fold: an "accepted" event is folded in place
        self.state, _, why = reducer.reduce(self.state, e, known)
        POOL["active"] = (self.state.get("account") or {}).get("active")
        if not why and e["type"] == "owner.dismissed" and marked:   # a stale page's second click holds nothing
            self.dismissed[name] = e["ts"]
        elif not why and e["type"] == "session.requested":
            self.dismissed.pop(name, None)   # a new session under a removed one's name starts clean
        elif not why and e["type"] == "message.received":
            msg = self.state["messages"].get(f"m{e.get('seq')}") or {}
            if msg.get("from_owner"):
                self.undismiss(msg.get("session"), e["ts"])   # he wrote to it: its next result is judged as usual
        elif not why and e["type"] == "permission.answered" and name in self.dismissed:
            from core import tools
            if tools.from_owner(e.get("data") or {}):
                self.undismiss(name, e["ts"])                 # or answered its card
        if self.started and e["type"] == "core.rejected" and forms.is_reason(
                forms.CORE_FAILURE, (e.get("data") or {}).get("code"), (e.get("data") or {}).get("reason")):
            self.bugs.append(e)
        if e["type"] == "session.renamed":
            new = (e.get("data") or {}).get("to")
            for box in (self.steps, self.fresh, self.checked, self.counted, self.turn_recap, self.recap_pause,
                        self.promises, self.own, self.doubted, self.verified, self.dismissed):
                if name in box:
                    box[new] = box.pop(name)
            return
        if e["type"] not in ACTIVITY or name not in self.state["sessions"]:
            return
        if e["type"] == "session.exited" and (e.get("data") or {}).get("why") == "reconcile":
            return  # the process left with a hub restart — this is not the session's behaviour
        if e["type"] == "turn.started":
            self.results.pop(name, None)
            self.promises.pop(name, None)
            # the start of a turn, not an insertion in the middle of it: the fold's own decision (turn_seq), so a letter
            # read with no Stop before it is a new turn here too
            if self.state["sessions"][name].get("turn_seq") == e.get("seq"):
                self.own[name] = self.own_turn(e)
            self.doubted[name] = self.doubt_of(e)
            self.verified.pop(name, None)
        elif e["type"] == "turn.tool" and checked([e.get("data") or {}]):
            self.verified[name] = True
        text = (e.get("data") or {}).get("text") or ""
        if self.started and e["type"] == "turn.result" and not e["data"].get("is_error") and len(text) >= NEEDS_MIN \
                and not minihub.boss(self.state, self.state["sessions"][name]):   # bus: the result goes to the mini-hub, not to the owner
            self.results[name] = {"text": text, "after": e["ts"]}
        if self.started and e["type"] == "turn.result" and not e["data"].get("is_error"):
            self.surrendered(name, text)
            if len(text) >= NEEDS_MIN and self.own.get(name):   # the watcher did not see the start of the turn — do not check
                self.promises[name] = {"of": e.get("seq"), "turn": self.state["sessions"][name].get("turn_seq"),
                                       "text": text, "after": e["ts"] + PROMISE_DELAY, "tries": 0}
        self.steps.setdefault(name, collections.deque(maxlen=WINDOW)).append(e)
        if e["type"] in ("turn.tool", "turn.started", "session.said"):
            self.counted[name] = self.counted.get(name, 0) + 1
        self.checked[name] = e["ts"]
        if self.started:  # we do not check the past at startup
            f = self.fresh.setdefault(name, {"n": 0, "since": e["ts"]})
            f["n"] += 1

    def undismiss(self, name, ts):
        """The owner's word to the session ends the hold of his click: a result held by it is judged at once."""
        if self.dismissed.pop(name, None) is not None and name in self.results:
            self.results[name]["after"] = min(self.results[name]["after"], ts)

    def own_turn(self, head):
        """The turn (its turn.started) was started not by the schedule, not by a `hub wait` check and not by a core message (including its own
        reminder): we do not check a promise in such a result — one reminder per promise. Decided at the
        start of the turn: by the result the start leaves the steps window, and the message leaves the state (core.pruned)."""
        d = head.get("data") or {}
        if _has(d.get("prompt_head"), SCHEDULE_MARKS + WAIT_MARKS):
            return False
        msg = self.state["messages"].get(d.get("marker") or "") or {}
        return not (forms.is_core(msg) or msg.get("schedule"))

    def doubt_of(self, head):
        """The number of the owner's message with a doubt that the turn started from (or in the middle of) — otherwise None. Decided at the
        start of the turn: by the result the start of a long turn leaves the steps window, and the message leaves the state (core.pruned)."""
        msg = self.state["messages"].get(((head.get("data") or {}).get("marker")) or "") or {}
        return msg["id"] if msg.get("from_owner") and doubt(msg.get("text")) else None

    def surrendered(self, name, text):
        """A turn result to the owner's doubt — a surrender without a check in this turn: remember it (decide sends it if enabled)."""
        if self.doubted.get(name) and surrender(text) and not self.verified.get(name):
            self.yielded.append((name, self.doubted[name]))

    def step(self, now=None):
        """One look into the journal: read to the end, decide whom to ask about, ask."""
        now = time.time() if now is None else now
        self.refresh(now)
        self.started = True
        self.decide(now)

    def refresh(self, now):
        """Read the journal to the end. Before a model call and before a fact: a pass with model questions takes seconds and minutes, and
        in that time a session starts a turn, a message arrives by itself (420 core refusals on such facts)."""
        started = self.started
        for e, known in reducer.verdicts(self.read()):
            self.feed(e, now, known)
        if started:   # rotation in the middle of a pass: the repeated tail is the past, and what is read further is already new
            self.started = True

    def recaps(self, now):
        """What the session works on now (like recap in Claude Code). A new turn — at once, from its task;
        later — no more than once per RECAP_EVERY and only with RECAP_STEPS new steps. The current to-do list item
        (TodoWrite) — without a model, otherwise Haiku from the task and steps. A hub on Codex
        — the line from the Codex plan (the host, session.recap by plan); the core's Codex model only with watch.json
        "codex_recap": true (the spec of 19.10 counted a call at ~9.6 thousand tokens; measured 05.11 with a fake
        Responses API — ~16 KB a request, about 4.5 thousand tokens, ~10 KB of it Codex's tool schemas)."""
        from core import codexmodel
        on_codex = codex_recap = None
        for name, sess in list(self.state["sessions"].items()):
            if sess.get("state") != "busy":
                if sess.get("state") != "dialog" and not (self.recap_pause.get(name) or {}).get("reset"):
                    self.recap_pause.pop(name, None)   # the turn ended; one waiting for a window reset — across turns too
                continue
            steps = list(self.steps.get(name) or [])
            n = self.counted.get(name, 0)
            head, cur = _current_turn(steps)
            fresh = head is not None and self.turn_recap.get(name) != head["seq"]
            last_at, last_n = self.recapped.get(name, (-RECAP_EVERY, 0))
            if not fresh and (now - last_at < RECAP_EVERY or n - last_n < RECAP_STEPS):
                continue
            todo = next((e for e in reversed(cur) if e["type"] == "turn.tool"
                         and (e.get("data") or {}).get("name") == "TodoWrite"), None)
            now_item = next((t for t in ((todo or {}).get("data") or {}).get("input", {}).get("todos") or []
                             if t.get("status") == "in_progress"), None)
            if now_item:
                text, by = now_item.get("activeForm") or now_item.get("content") or "", "todo"
            else:
                on_codex = codexmodel.on_codex(self.state_dir) if on_codex is None else on_codex
                if on_codex and codex_recap is None:
                    codex_recap = (self.cfg if self.cfg is not None else config()).get("codex_recap") is True
                if on_codex and not codex_recap:
                    continue
                task = self.task_text(head)
                if not task and not cur:
                    continue
                if self.recap_paused(name, now):
                    continue
                shown = steps[-self.recap_limits()[0]:]
                ask = self.lang_line() + (f"Turn task: {task}\n\n" if task else "") \
                    + ("No steps yet: the turn has just begun.\n" if not cur else "") + digest(name, sess, shown, 0, now)
                try:
                    text, by = self.recap(ask), "model"
                except Exception as e:  # noqa: BLE001 — the model did not answer: after a pause
                    wait = self.recap_failed(name, now)
                    print(f"watch: recap {name} failed: {e!r} — next one in {int(wait)} s", flush=True)
                    continue
                self.recap_pause.pop(name, None)
            text = " ".join(str(text or "").split())[:160]
            self.recapped[name] = (now, n)
            if head is not None:
                self.turn_recap[name] = head["seq"]
            if text:   # turn — which turn the line is about: while the model was thinking, the next one could start
                spool.put(os.path.join(self.state_dir, "spool"), "session.recap", name,
                          {"text": text, "by": by, **({"turn": head["seq"]} if head is not None else {}),
                           "channel": "cli", "owner": True}, src="watch")

    def recap_paused(self, name, now):
        """The line model of this session is paused after a failure (RECAP_PAUSE). The pause is lifted if the pool switched to another
        account, and for one waiting for a window reset — also if the core no longer sees an account refusal."""
        p = self.recap_pause.get(name)
        rejected = (self.state.get("limits") or {}).get("status") == "rejected"
        if p and (p["account"] != POOL["active"] or p["reset"] and not rejected):
            del self.recap_pause[name]
            return False
        return bool(p) and now < p["until"]

    def recap_failed(self, name, now):
        """A line model failure: a pause twice as long as the last one (RECAP_PAUSE…RECAP_PAUSE_MAX), and if the account refused until
        the window reset — until the reset. How long to wait (seconds)."""
        n = (self.recap_pause.get(name) or {}).get("n", 0)
        wait, reset = min(RECAP_PAUSE * 2 ** n, RECAP_PAUSE_MAX), False
        lim = self.state.get("limits") or {}
        at = ((lim.get("windows") or {}).get(lim.get("type")) or {}).get("resets")
        if lim.get("status") == "rejected" and isinstance(at, (int, float)) and at > now:
            wait, reset = at - now + 1, True
        self.recap_pause[name] = {"until": now + wait, "n": n + 1, "account": POOL["active"], "reset": reset}
        return wait

    def lang_line(self):
        """The first line of the model input: in which language to write what the owner will read (reason, ask, recap). The language is
        by his messages (the core, reducer); unknown — Russian, as the page was before language catalogs. Once
        per event number: without owner.lang the core goes through all the messages of the state."""
        seq = self.state.get("seq")
        if self._lang[0] != seq:
            code = reducer.known_lang(self.state)
            self._lang = (seq, f"Owner's language: {LANG_NAMES.get(code, code or LANG_NAMES['ru'])}.\n")
        return self._lang[1]

    def recap_limits(self):
        """How many steps and task characters the "what it works on" line model sees: watch.json (recap_steps, recap_task),
        otherwise RECAP_STEPS_SEEN and RECAP_TASK. Garbage in the file — default values."""
        cfg = self.cfg if self.cfg is not None else config()

        def num(key, default):
            v = cfg.get(key)
            return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 1 else default
        return num("recap_steps", RECAP_STEPS_SEEN), num("recap_task", RECAP_TASK)

    def task_text(self, head):
        """The task of a turn: the message it started from (by the label), otherwise the start of the prompt."""
        if head is None:
            return ""
        d = head.get("data") or {}
        msg = (self.state.get("messages") or {}).get(d.get("marker") or "") or {}
        return " ".join(str(msg.get("text") or d.get("prompt_head") or "").split())[:self.recap_limits()[1]]

    def decide(self, now):
        """Whom it is time to ask the model about — ask; trouble is the fact watch.alert."""
        self.facts(now)
        self.background(now)
        self.recaps(now)
        cfg = self.cfg if self.cfg is not None else config()
        box = os.path.join(self.state_dir, "spool")
        for name, mid in self.yielded if cfg.get("surrender") else ():
            print(f"{time.strftime('%H:%M:%S')} watch: {name} — a concession without a check on {mid}", flush=True)
            spool.put(box, "watch.surrender", name, {"message": mid, "channel": "cli", "owner": True}, src="watch")
        self.yielded = []
        if cfg.get("off"):
            self.fresh.clear()
            self.results.clear()
            self.promises.clear()
            return
        self.inject(now)
        self.owner_needed(now)
        if cfg.get("promise") is True:
            self.promised(now)
        else:
            self.promises.clear()
        asked = 0
        for name, sess in list(self.state["sessions"].items()):
            if asked >= ASKS:
                break  # the rest — in the next pass, in EVERY seconds
            if sess["state"] in ("removed", "closed"):
                continue
            f = self.fresh.get(name)
            due = f and (f["n"] >= BATCH or now - f["since"] >= WINDOW_SECONDS)
            silent = not f and sess["state"] == "busy" and now - self.checked.get(name, now) >= SILENT
            if not due and not silent:
                continue
            steps = list(self.steps.get(name) or [])
            if calm(sess, steps):
                self.fresh.pop(name, None)  # the turn ended as usual — the model has nothing to look for here (fewer questions)
                continue
            queued = sum(1 for m in self.state["messages"].values()
                         if m["session"] == name and m["state"] in ("queued", "typing"))
            text = digest(name, sess, steps, queued, now)
            asked += 1
            try:
                got = self.ask(self.lang_line() + text)
            except (OSError, ValueError, KeyError, TypeError) as e:
                print(f"watch: the model did not answer about {name} ({e!r}) — will retry", flush=True)
                if f:
                    f["since"] = now  # the same lines, the next attempt in a window
                else:
                    self.checked[name] = now - SILENT + WINDOW_SECONDS
                continue
            self.fresh.pop(name, None)
            if silent:
                self.checked[name] = now
            sure = bool(got.get("trouble"))
            kind = got.get("kind") if got.get("kind") in KINDS and got.get("kind") != "ok" else "errors"
            if sure and not plausible(kind, steps, now, sess):
                sure = False  # the models' "hung" on pauses of seconds and "errors" from a single error are false (the hub, 2026-09-30)
            if sure:
                self.emit(name, now, {"kind": kind, "why": text.split("\n")[0], "tail": text.split("\n")[1:][-3:],
                                      "reason": got.get("reason"), "advice": got.get("advice")})

    def inject(self, now):
        """A message waits in the queue of a busy session — ask the model whether to insert it now (all messages go
        through the model, the owner's too; Haiku since 2026-10-05). About each — once; the core does not insert assignments and
        service messages into a turn."""
        if not self.urgent:
            return
        def due(m):   # a core rule: one waiting for a hub decision — after its decision, otherwise the urgent one is never weighed
            return bool(m) and m["id"] not in self.weighed and not reducer.inject_refused(self.state, m)

        for mid in [m["id"] for m in self.state["messages"].values() if due(m)]:
            m = self.state["messages"].get(mid)
            if not due(m):
                continue
            hub = next((n for n, x in self.state["sessions"].items() if x.get("kind") == "hub"), None)
            rule = urgency_rule(m, hub)
            if rule is None:
                self.refresh(now)   # the pass is long: the message may have arrived already — do not call the model in vain
                m = self.state["messages"].get(mid)
                if not due(m):
                    continue
            sess = self.state["sessions"][m["session"]]
            self.weighed.add(m["id"])
            if rule is not None:  # a core service message — never, a short owner question about status — always (item 8)
                if rule >= URGENT:  # a service "urgency 0%" does not need to be in the queue — it is not a score
                    spool.put(os.path.join(self.state_dir, "spool"), "message.weighed", m["id"],
                              {"p": rule, "channel": "cli", "owner": True}, src="watch")
                    self.insert(m["id"], rule, now)
                continue
            steps = list(self.steps.get(sess["name"]) or [])
            text = digest(sess["name"], sess, steps, 0, now) + \
                f"\n\nNEW MESSAGE{m.get('origin') or ''}:\n{m['text'][:3000]}"
            try:
                p = self.urgent(text[-MAX_CHARS:])
            except (OSError, ValueError, KeyError, TypeError) as e:
                print(f"watch: the model did not answer about message {m['id']} ({e!r}) — it waits for the end of the turn", flush=True)
                continue
            spool.put(os.path.join(self.state_dir, "spool"), "message.weighed", m["id"],
                      {"p": round(p, 2), "channel": "cli", "owner": True}, src="watch")  # visible in the queue on the page
            if p >= URGENT and self.insert(m["id"], round(p, 2), now):
                print(f"{time.strftime('%H:%M:%S')} watch: {m['id']} → {sess['name']} now ({p:.2f})", flush=True)

    def insert(self, mid, p, now):
        """Insert in the middle of a turn if the core lets it by the fresh journal (its own rule): otherwise a fact — an extra refusal."""
        self.refresh(now)
        if reducer.inject_refused(self.state, self.state["messages"].get(mid)):
            return False
        spool.put(os.path.join(self.state_dir, "spool"), "message.inject", mid,
                  {"p": p, "seen_at": self.seen_at, "channel": "cli", "owner": True}, src="watch")
        return True

    def owner_needed(self, now):
        """A turn result waits for something from the owner, but there is no form question — mark the session "waits for you" (a result that reports but
        asks nothing leaves the owner to check by hand whether anything is needed there)."""
        for name, res in list(self.results.items()):
            if not self.waiting(name, res) or now < res["after"]:
                continue
            self.refresh(now)   # the pass is long: the session may have started a new turn already — do not call the model in vain
            if not self.waiting(name, res):
                continue
            hold = self.dismissed[name] + DISMISS_HOLD if name in self.dismissed else 0
            if hold and now >= hold:
                del self.dismissed[name]
            if now < hold or owner.open_asks(self.state["sessions"][name], now):
                res["after"] = max(hold, now + WINDOW_SECONDS)   # held by core facts, no model; judged once they pass
                continue                                          # (DISMISS_HOLD), unless a new turn replaced it
            try:
                end = ending(res["text"])
                op = self.needs(self.lang_line() + open_cards(self.state, name, end, now) + end)
            except (OSError, ValueError, KeyError, TypeError) as e:
                print(f"watch: the model did not answer about the result of {name} ({e!r}) — will retry", flush=True)
                res["after"] = now + WINDOW_SECONDS
                continue
            self.refresh(now)   # while the model read the result, the session could have started working again
            if not self.waiting(name, res):
                continue
            del self.results[name]
            if op.get("needs"):
                ask_line = (op.get("ask") or "").strip()[:200] or None
                print(f"{time.strftime('%H:%M:%S')} watch: {name} — the result waits for the owner: {ask_line or ''}", flush=True)
                spool.put(os.path.join(self.state_dir, "spool"), "watch.needs_owner", name, {
                    "p": 1.0, "ask": ask_line, "text": tail(res["text"]),
                    "seen_at": self.seen_at, "channel": "cli", "owner": True}, src="watch")

    def promised(self, now):
        """A result ended with a promise of its own action, and nothing in flight covers it — the fact watch.promise (the core
        will remind the session). One result per pass: up to PROMISE_VOTES model calls in a row."""
        for name, p in list(self.promises.items()):
            if now < p["after"]:
                continue
            self.refresh(now)   # the pass is long: the session may have started a turn already, someone may have written to it
            if not self.promise_due(name, p):
                continue
            if not promise_gate(p["text"]):
                del self.promises[name]
                self.promise_skipped(name, p, "gate")
                continue
            facts = promise_facts(self.state, name, now)
            text, votes, what = f"FACTS:\n{facts}\n\nEND OF THE RESULT:\n{ending(p['text'])}", [], ""
            try:
                while len(votes) < PROMISE_VOTES and (not votes or votes[-1]):
                    got = self.promise(text)
                    votes.append(bool(got.get("promise")) and not got.get("covered"))
                    what = what or (str(got.get("what") or "").strip() if votes[-1] else "")
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
                p["tries"] += 1
                p["after"] = now + WINDOW_SECONDS
                if p["tries"] >= PROMISE_TRIES:
                    del self.promises[name]
                print(f"watch: the model did not answer about the promise of {name} ({e!r})", flush=True)
                return
            self.refresh(now)   # while the model read, the session could have started working again
            if not self.promise_due(name, p):
                return
            del self.promises[name]
            if len(votes) < PROMISE_VOTES or not all(votes):
                self.promise_skipped(name, p, "model", votes=votes, what=what[:200])
                return
            print(f"{time.strftime('%H:%M:%S')} watch: {name} — a promise without an action: {what[:120]}", flush=True)
            spool.put(os.path.join(self.state_dir, "spool"), "watch.promise", name, redact.mask_data({
                "of": p["of"], "turn": p["turn"], "what": " ".join(what.split())[:200], "votes": len(votes),
                "text": tail(p["text"]), "facts": facts[:600], "seen_at": self.seen_at,
                "channel": "cli", "owner": True}), src="watch")
            return

    def promise_due(self, name, p):
        """The result p still waits for a promise check: the last one of the session, it is free by the core rule and did not start a turn."""
        if self.promises.get(name) is not p:
            return False
        sess = self.state["sessions"].get(name)
        if reducer.promise_refused(self.state, sess) or sess.get("turn_seq") != p["turn"]:
            del self.promises[name]
            return False
        return True

    def promise_skipped(self, name, p, stage, **extra):
        """A rejected result goes into the journal as a sample (PROMISE_SAMPLE): in a week it shows what was missed."""
        if sampled(p["of"], PROMISE_SAMPLE[stage]):
            spool.put(os.path.join(self.state_dir, "spool"), "watch.promise_skipped", name, redact.mask_data({
                "of": p["of"], "turn": p["turn"], "stage": stage, **extra,
                "text": tail(p["text"]), "channel": "cli", "owner": True}), src="watch")

    def waiting(self, name, res):
        """The result res still waits for a check: for a free session the mark can be set by the core rule. In a turn — wait; left,
        asked by a form, a result to the mini-hub — forget (the mark is not needed)."""
        if self.results.get(name) is not res:
            return False   # a new turn or a new result — its turn
        sess = self.state["sessions"].get(name)
        if sess and self.needs and sess["state"] == "busy":
            return False
        if not sess or not self.needs or reducer.attention_refused(self.state, sess):
            del self.results[name]
            return False
        return True

    def core_waits(self, mid):
        """The core (state.json) confirms that the message waits for delivery. A snapshot older than the message — we do not know: yes."""
        try:
            snap = journal.read_state_file(self.state_dir)
        except (OSError, ValueError, AttributeError):   # AttributeError: the v1.4.0 page server's own core.journal in
            return True                                 # memory has no read_state_file (as SLEEP above) — we do not know
        m = re.match(r"m(\d+)", mid)
        msg = ((snap.get("state") or {}).get("messages") or {}).get(mid)
        if msg is None:
            return not m or int(m.group(1)) > (snap.get("seq") or 0)
        return msg.get("state") in ("queued", "typing")

    def facts(self, now):
        """What is visible from the core state without a model: an undelivered message, a crashing process, a failure in the core code."""
        st = self.state
        for name, ts in list(self.cut.items()):
            sess = st["sessions"].get(name) or {}
            if sess.get("state") == "idle" and now - ts >= STALL and ("stall", name, ts) not in self.told:
                self.told[("stall", name, ts)] = now
                self.emit(name, now, {"kind": "stalled", "reason": STALLED.format(minutes=int(now - ts) // 60),
                                      "advice": STALLED_ADVICE})
            elif sess.get("state") in (None, "removed", "closed", *SLEEP):
                self.cut.pop(name, None)
        heads = {}   # the head of a session's queue: in print, otherwise the first in the queue (as _pump) — others wait their turn behind it
        for m in st["messages"].values():
            if m["state"] == "typing" or m["state"] == "queued" and not m.get("handover"):
                cur = heads.get(m.get("session"))
                key = (m["state"] != "typing", *reducer.queue_key(m))
                if cur is None or key < cur[0]:
                    heads[m.get("session")] = (key, m)
        for _, m in heads.values():   # only heads are judged: one more pass over all messages is not needed
            sess = st["sessions"].get(m.get("session") or "")
            # a message behind a long turn is not trouble: counted from the moment the session became free; behind another in the queue
            # (a 37th in the hub queue while the host spent minutes typing the first) — too: the alarm is about the queue head;
            # a new process still connecting MCP has not read its input: counted from the end of that wait (parking.mcp_init)
            if sess and sess["state"] == "idle" and \
                    not (sess.get("compacting") and now - sess["compacting"] < COMMAND_CONFIRM_SECONDS) and \
                    now - max(m["created"], sess["since"], sess.get("mcp_init", -MCP_WAIT) + MCP_WAIT) >= UNDELIVERED and \
                    ("msg", m["id"]) not in self.told:
                self.told[("msg", m["id"])] = now
                if not self.core_waits(m["id"]):
                    continue  # it is not in the core or it has already arrived — the journal reader sees a phantom (eval 2026-10-03, item 7)
                waited = int(now - m["created"])
                self.emit(sess["name"], now, {"kind": "undelivered", "message": m["id"], "reason": UNDELIVERED_WHY.format(
                    id=m["id"], minutes=waited // 60, seconds=waited % 60, state=m["state"], attempt=m.get("attempt", 0)),
                    "advice": UNDELIVERED_ADVICE})
        for name, sess in st["sessions"].items():
            if sess["state"] not in ("removed", "closed") and sess.get("restarts", 0) >= CRASHES and \
                    self.told.get(("crash", name)) != sess["restarts"]:
                self.told[("crash", name)] = sess["restarts"]
                self.emit(name, now, {"kind": "crashloop", "reason": CRASHLOOP.format(count=sess["restarts"]),
                                      "advice": CRASHLOOP_ADVICE})
        for e in self.bugs:
            self.emit(e.get("entity"), now, {"kind": "corebug", "reason": COREBUG.format(
                type=str(e["data"].get("type")), of=str(e["data"].get("of")), reason=str(e["data"].get("reason")))})
        self.bugs = []

    def background(self, now):
        """Session background: the last output of a background command — to the core (the page shows what runs in the background), a long and silent background — an alarm to the hub."""
        box = os.path.join(self.state_dir, "spool")
        for name, sess in self.state["sessions"].items():
            if sess.get("state") in ("removed", "closed", *SLEEP, None):
                continue
            for b in list((sess.get("background") or {}).values()):
                out_at, line = b.get("out_at"), b.get("out")
                if not b.get("output") and b.get("kind") == "bash":
                    b = {**b, "output": guess_output(sess, b["id"]) or ""}   # launched before the host wrote the file
                if b.get("output"):
                    try:
                        mtime = os.path.getmtime(b["output"])
                    except OSError:
                        mtime = None
                    seen = self.told.get(("bgout", name, b["id"]))   # (file time, last line)
                    if seen and seen[0] == mtime:
                        out_at, line = seen
                    elif mtime:
                        out_at, line = mtime, last_line(b["output"])
                        self.told[("bgout", name, b["id"])] = (mtime, line)
                        spool.put(box, "session.background_output", name, {"id": b["id"], "at": mtime, "line": line,
                                                                           "seen_at": self.seen_at, "channel": "cli",
                                                                           "owner": True}, src="watch")
                age = now - b.get("since", now)
                quiet = now - (out_at or b.get("since", now))
                agent = b.get("kind") == "agent" and not b.get("output")
                stuck = age >= BG_AGENT if agent else age >= BG_LONG and quiet >= BG_QUIET
                if not stuck or ("bg", name, b["id"]) in self.told:
                    continue
                self.told[("bg", name, b["id"])] = now
                what = BG_AGENT_WORD if b.get("kind") == "agent" else BG_COMMAND_WORD
                said = (BG_LAST_OUT.format(minutes=int(quiet // 60), line=line) if line and out_at
                        else "" if agent else BG_NO_OUT)
                self.emit(name, now, {"kind": "background", "reason": BG_WHY.format(
                    dur=_dur(age), what=what, title=b.get("title") or b["id"], said=said), "advice": BG_ADVICE})

    def emit(self, name, now, data):
        """Trouble in a session — to the hub (the fact watch.alert, the core itself limits the rate); trouble in the hub itself — to the owner. Through a mask:
        the alarm quotes what the session printed (the last line of a background command)."""
        reason = data.get("reason")   # with the marks of its phrases (the mask gives plain text): for the owner below
        marked = {k: data[k] for k in ("reason", "advice") if getattr(data.get(k), "spans", ())}
        data = redact.mask_data(data)
        sess = self.state["sessions"].get(name) or {}
        box = os.path.join(self.state_dir, "spool")
        print(f"{time.strftime('%H:%M:%S')} watch: {name} — {data['kind']} {data.get('reason') or ''}"[:300], flush=True)
        if sess.get("kind") == "hub" and data["kind"] != "corebug":
            if now - self.told.get(("owner", name), -OWNER_COOLDOWN) < OWNER_COOLDOWN:
                return
            self.told[("owner", name)] = now
            # the owner reads it: in his language (the watcher's process language); the core's phrases in it too
            from core import i18n
            what = reducer.WATCH_KINDS.get(data["kind"]) or reducer.WATCH_FACTS.get(data["kind"])
            what = coretext.human(what) if what else i18n.t("watch.in_trouble")
            detail = redact.mask(str(coretext.human(reason))) if reason else data.get("why") or ""
            # about a message — a notification with its id: the core rejects a stale one and removes an open one when it arrives
            about = {"id": f"{data['kind']}:{data['message']}", "message": data["message"]} if data.get("message") else {}
            spool.put(box, "owner.notify", None, {
                "title": i18n.t("watch.hub_title"), "urgent": True, "source": "watch", "channel": "cli", "owner": True,
                **about, "text": i18n.t("watch.hub_trouble", name=name, what=what, tail="\n".join(data.get("tail") or []),
                                        detail=detail)}, src="watch")
            return
        # the core's phrases keep their marks (<field>_l10n: the hub's note reads in the owner's language) — only when the
        # mask left the text as it was: a masked value stays out of the marks too
        for k, v in marked.items():
            if data.get(k) == str(v):
                coretext.put(data, k, v)
        spool.put(box, "watch.alert", name, {**data, "channel": "cli", "owner": True}, src="watch")


def main():
    from core.daemon import default_state_dir
    w = Watcher(default_state_dir())
    from core import codexmodel
    model = "Codex" if codexmodel.on_codex() else "Haiku"
    print("watch: journal watcher " + ("is off in watch.json" if config().get("off") else f"is running ({model})"), flush=True)
    while True:
        try:
            w.step()
        except Exception as e:  # noqa: BLE001 — the watcher must not crash on a single journal line
            print(f"watch: pass failed: {e!r}", flush=True)
        time.sleep(EVERY)


if __name__ == "__main__":
    main()
