"""Codex: the steps and texts of a Codex session's turn for the codex host
(hosts/codex.py) — what the feed shows and what the turn's result carries; the work that outlives a turn (background
commands, subagents) and what stops it.

Seams the host calls:
- tool(item) / tool_result(item) — a Codex step -> (name, input) of turn.tool / (error, text) of turn.tool_result;
- result_fields(item) — more fields of turn.tool_result: mcp, the server of an MCP tool (as from the claude host —
  an MCP tool's error makes core/mcpwatch ask for the servers' status at once);
- said(item) — the data of session.said for an agentMessage;
- result_text(proc, status, error) — the text fields of turn.result (text, cut, lessons) on turn/completed;
- answer(proc, limit) — the turn's answer text (the core's model, core/codexmodel, takes it too);
- features(**session) — Codex features (`--enable`) this slice adds to a session's process: the subagents;
- child(proc, name, method, params) — everything of another thread in the session's process (a Codex subagent);
- item / turn_done / interrupt / response — the host's common seams (all of them: hosts/codex.py, "Seams").

Probes of codex app-server 0.162.1 with a fake Responses API (2026-11-05) this follows:
- a command keeps running after its turn ends — when the model left it (yield) and when the turn is interrupted; its
  item/completed comes when the process exits, after the turn. thread/backgroundTerminals/terminate stops it;
- a subagent is a thread of its own in the same process, its lines carry its threadId. multi_agent (v1) spawns it with a
  collabAgentToolCall step (the new thread in receiverThreadIds on item/completed), multi_agent_v2 with a
  subAgentActivity item (kind started, agentThreadId); in v2 a subagent may spawn its own;
- a spawned subagent runs on after the parent's turn ends; turn/interrupt of the parent does not stop it, and nothing
  wakes the parent when it is done.

So, as with Claude Code:
- a subagent's steps and texts go to the journal with `parent` (the step that spawned it): the feed shows them under
  that step, and /api/subagent reads them from sessions/<name>/codex-agents/<step>.json (core/subagents.py);
- its tokens go into the session's running turn, or, when the session is idle, into codex.spent of the session;
- what still runs when the session's turn ends (a subagent's turn, a command) is the session's background
  (session.background): the session is not free, a restart waits for it; when it ends the session gets a
  <task-notification> turn, the way Claude Code wakes it (the core already reads that form: hooks/hub_hook, reducer);
- an interrupt stops every running subagent's turn — those left running by earlier turns too (hub-dev, 2026-11-05:
  "Stop" stops the session's agents); a turn that ends interrupted takes its running commands with it (Claude Code's
  Esc stops the running Bash), while commands left by earlier turns stay, as Claude's run_in_background. A subagent
  stopped so wakes no one: the owner stopped it.
"""
import json
import os
import re
import sys
import time

from core import journal, lessons, redact

TOOL_NAMES = {"commandExecution": "Bash", "fileChange": "Edit", "webSearch": "WebSearch", "imageView": "Read",
              "collabAgentToolCall": "Agent", "dynamicToolCall": "Tool"}
# subagents: both generations of Codex's multi-agent feature, so that neither a project's .codex/config.toml nor a
# version that changes its default picks one for the hub (hosts/codex_set.args pins what is not enabled here)
FEATURES = ("multi_agent", "multi_agent_v2")
STEPS_MAX = 200       # a subagent's steps kept for /api/subagent
STEP_INPUT = 8000     # a step's input in that file (an edit's code; the journal keeps 1500)
AGENTS_KEPT = 50      # subagents whose steps a process keeps in memory (older ones stay in their files)
SAVE_S = 2            # a subagent's steps file is rewritten at most this often while it works
GONE = ("notLoaded", "systemError")   # a thread status that ends a subagent's turn without its turn/completed
NOTE_MAX = 4000       # a subagent's last message / a command's output in the notification
NOT_DONE = ("failed", "declined", "interrupted")   # a step's status (CommandExecutionStatus, PatchApplyStatus…)
TASK_ID = re.compile(r"\W")   # the core reads <task-id>(\w+)</task-id>
STOPPED = "interrupted"


def tool(item):
    """A Codex step -> (name, input) for the feed (as with Claude Code: the name, the main argument); not a step — None."""
    kind = item.get("type")
    if kind == "commandExecution":
        return "Bash", {"command": item.get("command"), "cwd": item.get("cwd")}
    if kind == "fileChange":
        return _edit(item.get("changes") or [])
    if kind == "mcpToolCall":
        return f"mcp__{item.get('server')}__{item.get('tool')}", item.get("arguments") or {}
    if kind == "webSearch":
        return "WebSearch", {"query": item.get("query")}
    if kind == "collabAgentToolCall":
        # only a spawn is an Agent step (the feed offers its subagent's steps); wait, sendInput, closeAgent… — a step of
        # their own name, with the agents they address
        prompt, call = str(item.get("prompt") or ""), str(item.get("tool") or "")
        if call != "spawnAgent":
            verb = re.sub(r"Agents?$", "", call) or "Call"   # closeAgent -> AgentClose, wait -> AgentWait
            return "Agent" + verb[:1].upper() + verb[1:], {
                "agents": item.get("receiverThreadIds") or [], **({"prompt": prompt} if prompt else {})}
        return "Agent", {"description": prompt.split("\n")[0][:120] or call, "prompt": prompt,
                         **({"model": item["model"]} if item.get("model") else {})}
    if kind == "subAgentActivity":   # multi_agent_v2: its start is the step; its end is not a step of its own
        if item.get("kind") != "started":
            return None
        return "Agent", {"description": str(item.get("agentPath") or ""),
                         **({"model": item["model"]} if item.get("model") else {})}
    if kind in TOOL_NAMES:
        return TOOL_NAMES[kind], {k: item.get(k) for k in ("tool", "prompt", "path", "arguments") if item.get(k)}
    return None


def _hunks(diff):
    """A unified diff of one file -> edits [{old_string, new_string}] (the form the feed draws for Claude's Edit): a hunk's
    context lines go into both."""
    edits, old, new = [], None, None
    for line in diff.split("\n") + ["@@"]:
        if line.startswith("@@"):
            if old is not None:
                edits.append({"old_string": "\n".join(old), "new_string": "\n".join(new)})
            old, new = [], []
        elif old is None or not line or line.startswith("\\"):
            continue   # before the first hunk, the end, "\ No newline at end of file"
        elif line[0] == "-":
            old.append(line[1:])
        elif line[0] == "+":
            new.append(line[1:])
        else:
            old.append(line[1:])
            new.append(line[1:])
    return edits


def _edit(changes):
    """A fileChange (FileUpdateChange: path, kind add|delete|update, diff) -> the feed's Write (one new file) or Edit with
    the file and its edits; several files — Edit of the first with its edits, every path in files (the feed draws one
    file's diff: another file's hunks under the first one's name would be a wrong diff)."""
    changes = [c for c in changes if isinstance(c, dict) and c.get("path")]
    if not changes:
        return "Edit", {"changes": []}
    first = changes[0]
    kind, diff = (first.get("kind") or {}).get("type"), str(first.get("diff") or "")
    edits = [{"old_string": "", "new_string": diff.rstrip("\n")}] if kind == "add" else \
        [{"old_string": diff.rstrip("\n"), "new_string": ""}] if kind == "delete" else _hunks(diff)
    if len(changes) == 1 and (first.get("kind") or {}).get("type") == "add":
        return "Write", {"file_path": first["path"], "content": str(first.get("diff") or "")}
    moved = (first.get("kind") or {}).get("move_path")
    return "Edit", {"file_path": first["path"], "edits": edits, **({"move_path": moved} if moved else {}),
                    **({"files": [c["path"] for c in changes]} if len(changes) > 1 else {})}


def tool_result(item):
    """A step's end -> (error, text). Refused by the owner (a permission card answered "no": status declined) — an error,
    as Claude's denied tool; so is a failed or interrupted one."""
    kind, status = item.get("type"), item.get("status")
    if kind == "commandExecution":
        return (item.get("exitCode") not in (0, None) or status in NOT_DONE,
                item.get("aggregatedOutput") or (status if status in NOT_DONE else ""))
    if kind == "mcpToolCall":
        got = item.get("error") or item.get("result")
        return (bool(item.get("error")) or status in NOT_DONE,
                json.dumps(got, ensure_ascii=False) if got is not None else status or "")
    return status in NOT_DONE, status or ""


def result_fields(item):
    return {"mcp": item["server"]} if item.get("type") == "mcpToolCall" and item.get("server") else {}


def answer(proc, limit=journal.TEXT_MAX):
    """The result of a turn: the answer's messages (final_answer, partial_answer) in order; no phases (not every model
    sends them) — the last message, as before. Longer than limit — the last one whole (the question and the conclusion are
    usually in it), the beginning as much as fits."""
    parts = [t for phase, t in proc.texts if phase in ("final_answer", "partial_answer")] or [proc.last_text]
    text = "\n\n".join(parts)
    if len(text) <= limit or len(parts) == 1 or len(parts[-1]) > limit - 100:
        return text[:limit]
    room = limit - len(parts[-1]) - 3
    return text[:room] + "…\n\n" + parts[-1]


def said(item):
    """session.said of an agentMessage: the text as the journal keeps it, the length when it was cut."""
    text = item["text"]
    return {"text": text[:journal.TEXT_MAX], **({"cut": len(text)} if len(text) > journal.TEXT_MAX else {})}


def result_text(proc, status, error):
    """The text fields of turn.result: the whole answer, as from claude — a runaway one goes into the journal cut, its
    "Lessons" block scanned first."""
    text = answer(proc, limit=10 ** 9) if status == "completed" else (error or proc.last_text or f"Codex turn: {status}")
    found = lessons.scan(text) if len(text) > journal.TEXT_MAX else None
    return {"text": answer(proc) if status == "completed" else text[:journal.TEXT_MAX],
            **({"cut": len(text)} if len(text) > journal.TEXT_MAX else {}), **({"lessons": found} if found else {})}


def features(**session):
    """Codex features this slice turns on for a session's process: its subagents (both generations, pinned on). Not
    for the bus's liaison (slice H): it reads another hub's words, and Claude's liaison has Agent and Task denied
    (core/liaison.DENY) — so its subagents stay pinned off (hosts/codex_set.args)."""
    return () if session.get("liaison") else FEATURES


# --- what outlives a turn: the session's commands and subagents --------------------------------------------------
class Work:
    """A session process's state of this slice (proc.cx_items)."""

    def __init__(self):
        self.parent_of = {}   # subagent thread -> the step id that spawned it (the feed's parent of its steps)
        self.title = {}       # subagent thread -> what it was given (prompt or task path)
        self.model = {}       # subagent thread -> its model
        self.running = {}     # subagent thread -> its running turn id
        self.cmds = {}        # (thread, item id) -> {"pid", "title", "turn"}: a command that has not ended
        self.bg = set()       # background task ids: what was still running when the session became free
        self.shown = []       # the last background list given to the core
        self.stopped = set()  # commands this slice terminated: their end is not news for the session
        self.stopping = set() # subagent threads asked to stop: not the session's background while their turn ends
        self.quiet = set()    # commands whose stop was refused: the session's background again, their end still no news
        self.pending = {}     # subagent thread -> [in, out] tokens spent while the session had no turn
        self.last = {}        # subagent thread -> its last message
        self.steps = {}       # spawning step id -> [steps of its subagent] (/api/subagent), the latest active last
        self.files = {}       # step id -> {tool id -> step} of the same list: a result finds its step
        self.dirty = {}       # step id -> when its steps changed after the last save
        self.saved = {}       # step id -> when its file was written


def _work(proc):
    work = getattr(proc, "cx_items", None)
    if work is None:
        work = proc.cx_items = Work()
    return work


def _idle(proc):
    return not proc.turn and not proc.starting


def _agent_id(thread):
    return "agent_" + TASK_ID.sub("", str(thread))


def _cmd_id(thread, item_id, proc):
    own = TASK_ID.sub("_", str(item_id))
    return own if thread == proc.thread else f"{own}_{TASK_ID.sub('', str(thread))[-8:]}"


def _cmd_title(item):
    actions = [a.get("command") for a in item.get("commandActions") or [] if isinstance(a, dict) and a.get("command")]
    return redact.mask_head(" ; ".join(actions) or str(item.get("command") or ""), 120)


def _tasks(proc, work):
    """The session's background as the core keeps it (session.background tasks): running subagents, then commands."""
    agents = [{"id": _agent_id(t), "kind": "agent", "title": redact.mask_head(work.title.get(t) or str(t), 120),
               **({"model": str(work.model[t])[:80]} if work.model.get(t) else {})}
              for t in work.running if _agent_id(t) in work.bg]
    cmds = [{"id": _cmd_id(t, i, proc), "kind": "bash", "title": c["title"]}
            for (t, i), c in work.cmds.items() if _cmd_id(t, i, proc) in work.bg]
    return agents + cmds


def _background(proc, name, work):
    """A session.background fact when the list changed."""
    tasks = _tasks(proc, work)
    work.bg &= {t["id"] for t in tasks}   # what ended leaves for good
    if tasks == work.shown:
        return []
    work.shown = tasks
    return [("session.background", name, {"tasks": tasks})]


def _short(work, thread):
    return redact.mask_head((work.title.get(thread) or str(thread)).split("\n")[0], 120)


def _notify(proc, tid, status, summary, body):
    """A <task-notification> turn for the session, the form Claude Code wakes a session with when its background work
    ends; the host sends it (a turn when idle, an insertion into a running turn)."""
    text = (f"<task-notification>\n<task-id>{tid}</task-id>\n<status>{status}</status>\n<summary>{summary}</summary>\n"
            + (body.strip()[-NOTE_MAX:] + "\n" if body.strip() else "") + "</task-notification>")
    proc.backlog.append([{"type": "text", "text": text}])


def _add_tokens(proc, work, thread, last):
    spent = [last.get("inputTokens") or 0, last.get("outputTokens") or 0]
    if proc.turn:   # into the session's running turn (its turn.result)
        proc.spent = [proc.spent[0] + spent[0], proc.spent[1] + spent[1]]
    else:
        was = work.pending.get(thread, [0, 0])
        work.pending[thread] = [was[0] + spent[0], was[1] + spent[1]]


def _settle_tokens(proc, name, work, thread):
    """A subagent's turn ended: what it spent while the session had no turn — into the session's turn if one runs now,
    otherwise a codex.spent of the session ("Usage")."""
    spent = work.pending.pop(thread, None)
    if not spent or not (spent[0] or spent[1]):
        return []
    if proc.turn:
        proc.spent = [proc.spent[0] + spent[0], proc.spent[1] + spent[1]]
        return []
    return [("codex.spent", name, {"model": work.model.get(thread), "tokens_in": spent[0], "tokens_out": spent[1]})]


def _spawned(proc, name, work, item):
    """The subagent threads a step of any thread spawned -> the step's id (their steps go under it). A subagent may
    start before its v1 spawn step completes (receiverThreadIds come only then): what it did meanwhile was kept under
    its own id — it moves under the step now (the journal keeps that id: those steps are hidden from the feed, as all
    a subagent's are)."""
    if item.get("type") == "collabAgentToolCall" and item.get("tool") == "spawnAgent":
        found = [(t, str(item.get("prompt") or "")) for t in item.get("receiverThreadIds") or []]
    elif item.get("type") == "subAgentActivity" and item.get("kind") == "started" and item.get("agentThreadId"):
        found = [(item["agentThreadId"], str(item.get("agentPath") or ""))]
    else:
        return
    for t, title in found:
        if t in work.parent_of:
            continue
        work.parent_of[t] = step = item.get("id")
        work.title.setdefault(t, title)
        if item.get("model"):
            work.model[t] = item["model"]
        early = work.steps.pop(_agent_id(t), None)
        if early is None:   # dropped from memory already: its file
            early = _load(proc, name, _agent_id(t))
        if early:
            _steps(proc, name, work, step)[:0] = early
            work.files.setdefault(step, {}).update(work.files.pop(_agent_id(t), {}))
            work.dirty.pop(_agent_id(t), None)
            work.saved.pop(_agent_id(t), None)
            _forget(proc, name, _agent_id(t))
            _save(proc, name, step, work)


def _command(proc, work, thread, method, item):
    """A command's start and end; an end of a background one that nobody stopped — news for the session."""
    key = (thread, item.get("id"))
    if method == "item/started" and item.get("status", "inProgress") == "inProgress":
        work.cmds[key] = {"pid": item.get("processId"), "title": _cmd_title(item),
                          "turn": proc.turn if thread == proc.thread else work.running.get(thread)}
        return
    if method != "item/completed" or work.cmds.pop(key, None) is None:
        return
    tid = _cmd_id(thread, item.get("id"), proc)
    if key in work.stopped or key in work.quiet:
        work.stopped.discard(key)
        work.quiet.discard(key)
        return
    if tid in work.bg and thread == proc.thread:
        code = item.get("exitCode")
        _notify(proc, tid, "completed" if code == 0 else "failed",
                f"Background command \"{_cmd_title(item)}\" finished (exit code {code})",
                str(item.get("aggregatedOutput") or ""))


def _stop_commands(proc, work, thread, turn):
    """A turn ended interrupted: its commands still running stop with it (Codex leaves them as background terminals)."""
    for (t, i), c in list(work.cmds.items()):
        if t == thread and c["turn"] == turn and c.get("pid") and (t, i) not in work.stopped:
            work.stopped.add((t, i))
            proc.call("thread/backgroundTerminals/terminate", {"threadId": t, "processId": str(c["pid"])})


def item(proc, name, method, it):
    """An item of the session's own thread: commands that may outlive the turn, steps that spawn subagents."""
    work = _work(proc)
    _spawned(proc, name, work, it)
    if it.get("type") == "commandExecution":
        _command(proc, work, proc.thread, method, it)
    _save_due(proc, name, work)
    return _background(proc, name, work) if work.bg or work.shown else []


def turn_done(proc, name, turn):
    """The session's turn ended: what still runs is its background; an interrupted turn takes its commands with it."""
    work = _work(proc)
    if turn.get("status") == STOPPED:
        _stop_commands(proc, work, proc.thread, turn.get("id"))
    work.bg |= {_agent_id(t) for t in work.running if t not in work.stopping}
    left = _left(proc, work) - work.bg
    work.bg |= left
    _save_due(proc, name, work, now=True)
    # its step's row says where it went, as Claude's run_in_background answer does (otherwise the feed marks a step
    # without a result "stopped" at the turn's end — while it runs on); its real end comes later on the same row
    rows = [("turn.tool_result", name, {"id": i, "error": False, "text": f"Command running in background with ID: {tid}"})
            for (t, i) in work.cmds for tid in [_cmd_id(t, i, proc)] if t == proc.thread and tid in left]
    return rows + _background(proc, name, work)


def _left(proc, work):
    """Commands left running by a turn that ended: the session's own, and a subagent's once its turn is over (while it
    works, the subagent is the background task, not its every command)."""
    return {_cmd_id(t, i, proc) for (t, i) in work.cmds
            if (t, i) not in work.stopped and (t == proc.thread or t not in work.running)}


def interrupt(proc, name):
    """Stop: the subagents' running turns too (the host interrupts only the session's own); their commands stop when the
    turns end interrupted (child below)."""
    work = _work(proc)
    for thread, turn in work.running.items():
        if turn:
            work.stopping.add(thread)
            proc.call("turn/interrupt", {"threadId": thread, "turnId": turn})


def response(proc, name, method, params, msg):
    """Answers to this slice's requests. A stop of a command that did not happen goes to the log, and the command is
    the session's again: background while it runs, news when it ends (not a free session with a command alive)."""
    if method not in ("thread/backgroundTerminals/terminate",) and not (
            method == "turn/interrupt" and (params or {}).get("threadId") != proc.thread):
        return None
    if "error" not in msg and (not method.endswith("terminate") or (msg.get("result") or {}).get("terminated")):
        return []
    print(f"codex: {method} {name} {params}: {msg.get('error') or msg.get('result')}", file=sys.stderr, flush=True)
    if not method.endswith("terminate"):
        return []
    work = _work(proc)
    for key, c in work.cmds.items():
        if key in work.stopped and key[0] == params.get("threadId") and str(c.get("pid")) == params.get("processId"):
            work.stopped.discard(key)
            work.quiet.add(key)   # often it is exiting already: the owner stopped it, its end wakes no one
            if _idle(proc):
                work.bg |= _left(proc, work)
    return _background(proc, name, work)


def child(proc, name, method, params):
    """A line of another thread in the session's process — a subagent (and its own, in v2): its steps and texts go to
    the journal under the step that spawned it, its tokens to the session, its turn is the session's background once
    the session is free."""
    from hosts.streamjson import _clip, _result_text
    work, thread = _work(proc), params.get("threadId")
    parent = work.parent_of.get(thread) or _agent_id(thread)
    facts = []
    if method == "turn/started":
        work.running[thread] = (params.get("turn") or {}).get("id")
        if _idle(proc):
            work.bg.add(_agent_id(thread))
    elif method == "turn/completed":
        turn = params.get("turn") or {}
        work.running.pop(thread, None)
        work.stopping.discard(thread)
        if turn.get("status") == STOPPED:
            _stop_commands(proc, work, thread, turn.get("id"))
        facts += _settle_tokens(proc, name, work, thread)
        if _idle(proc):   # the session is free: what the subagent left running is its background now
            work.bg |= _left(proc, work)
        if _agent_id(thread) in work.bg and turn.get("status") != STOPPED:
            _notify(proc, _agent_id(thread), "completed" if turn.get("status") == "completed" else "failed",
                    f"Agent \"{_short(work, thread)}\" finished", work.last.get(thread) or "")
        work.bg.discard(_agent_id(thread))
    elif method == "thread/tokenUsage/updated":
        _add_tokens(proc, work, thread, (params.get("tokenUsage") or {}).get("last") or {})
    elif thread in work.running and (method == "thread/closed" or (
            method == "thread/status/changed" and (params.get("status") or {}).get("type") in GONE)):
        # gone without the end of its turn (closeAgent, an error): not running — or the session would never be free.
        # News only of an error: a close is the session's own doing
        work.running.pop(thread, None)
        facts += _settle_tokens(proc, name, work, thread)
        if _idle(proc):
            work.bg |= _left(proc, work)
        if _agent_id(thread) in work.bg and (params.get("status") or {}).get("type") == "systemError":
            _notify(proc, _agent_id(thread), "failed", f"Agent \"{_short(work, thread)}\" stopped",
                    work.last.get(thread) or "")
        work.bg.discard(_agent_id(thread))
        _save_due(proc, name, work, now=True)
    elif method in ("item/started", "item/completed"):
        it = params.get("item") or {}
        _spawned(proc, name, work, it)
        if it.get("type") == "commandExecution":
            _command(proc, work, thread, method, it)
        steps, before = _steps(proc, name, work, parent), len(facts)
        if it.get("type") == "agentMessage" and method == "item/completed" and (it.get("text") or "").strip():
            work.last[thread] = it["text"]
            facts.append(("session.said", name, {**said(it), "parent": parent}))
            steps.append({"kind": "said", "text": it["text"][:journal.TEXT_MAX]})
        elif method == "item/started" and tool(it):
            tname, inp = tool(it)
            facts.append(("turn.tool", name, {"id": it.get("id"), "name": tname, "input": _clip(inp), "parent": parent}))
            step = {"kind": "tool", "id": it.get("id"), "name": tname, "input": _clip(inp, STEP_INPUT), "status": "running"}
            steps.append(step)
            work.files.setdefault(parent, {})[it.get("id")] = step
        elif method == "item/completed" and tool(it):
            error, text = tool_result(it)
            facts.append(("turn.tool_result", name, {"id": it.get("id"), "error": bool(error),
                                                     "text": _result_text(text), "parent": parent,
                                                     **result_fields(it)}))
            step = work.files.get(parent, {}).get(it.get("id"))
            if step:
                step.update(result=_result_text(text, 4000), status="error" if error else "done")
        del steps[:-STEPS_MAX]
        if len(facts) > before:
            work.dirty.setdefault(parent, time.monotonic())
            if method == "item/started":   # a running step shows at once (it may run for minutes with no line)
                _save(proc, name, parent, work)
    _save_due(proc, name, work, method == "turn/completed")
    return facts + _background(proc, name, work)


def steps_path(state_dir, name, step):
    """The file /api/subagent reads a subagent's steps from (core/subagents.py)."""
    return os.path.join(state_dir, "sessions", name, "codex-agents", TASK_ID.sub("_", str(step)) + ".json")


def _save_due(proc, name, work, now=False):
    """A busy subagent rewrites its steps file at most every SAVE_S (it is on the host's line path); what waits goes
    with the next line of the process — the subagent's or the session's — and its turn's end writes at once."""
    for step in list(work.dirty):
        if now or time.monotonic() - work.saved.get(step, 0) >= SAVE_S:
            _save(proc, name, step, work)


def _steps(proc, name, work, step):
    """The steps of a spawning step, made the latest active; one dropped from memory comes back from its file (it is
    rewritten whole: without that it would keep only what came after)."""
    steps = work.steps.pop(step, None)
    if steps is None:
        steps = _load(proc, name, step)
        work.files[step] = {x.get("id"): x for x in steps if isinstance(x, dict) and x.get("kind") == "tool"}
    work.steps[step] = steps
    for old in list(work.steps)[:-AGENTS_KEPT]:   # a long session spawns many: the quiet ones live on disk
        if old in work.dirty:
            _save(proc, name, old, work)
        work.steps.pop(old, None)
        work.files.pop(old, None)
        work.saved.pop(old, None)
    return steps


def _load(proc, name, step):
    state_dir = getattr(proc, "state_dir", None)
    if not state_dir:
        return []
    try:
        with open(steps_path(state_dir, name, step), encoding="utf-8") as f:
            items = json.load(f).get("items")
        return items if isinstance(items, list) else []
    except (OSError, ValueError, AttributeError):
        return []


def _forget(proc, name, step):
    state_dir = getattr(proc, "state_dir", None)
    if state_dir:
        try:
            os.remove(steps_path(state_dir, name, step))
        except OSError:
            pass


def _save(proc, name, parent, work):
    work.dirty.pop(parent, None)
    work.saved[parent] = time.monotonic()
    state_dir = getattr(proc, "state_dir", None)
    if not state_dir:
        return
    path = steps_path(state_dir, name, parent)
    thread = next((t for t, p in work.parent_of.items() if p == parent), None)
    data = {"agent": {"type": "codex", "description": (work.title.get(thread) or "").split("\n")[0][:200],
                      "thread": thread}, "items": work.steps.get(parent) or []}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(path + ".tmp", path)
    except OSError as e:   # the feed only loses the steps view; the journal has them
        print(f"codex: the steps of subagent {name} are not recorded: {e}", file=sys.stderr, flush=True)


def notice(proc, name, method, params):
    return []
