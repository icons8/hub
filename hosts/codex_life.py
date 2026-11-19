"""Codex: the life of a Codex session beyond its turns for the codex host
(hosts/codex.py) — the recap after a big turn, orphans after a hubd crash, the direction's context after a compaction,
whether a session's conversation can be continued.

- recap(host, name, proc) — CodexHost.recap: Codex has no /recap. The host asks the session's app-server for the tail
  of its thread (thread/turns/list, no model) and the core's model (core/codexmodel.ask) writes one or two sentences
  from it in a thread of its own (at most one per session and RECAP_AT_ONCE per host at a time: each is a model call
  of ~4.5 thousand tokens plus the tail); the answer comes back through the host's inbox as a line of its own (LIFE)
  and becomes session.recap by "codex" — what Claude Code's /recap gives (the fold keeps it as the "away" line).
- reap(host, pid, session_id) — CodexHost.reap: the app-server's command line carries no session id, so the process is
  told by its tree marker (platforms.TREE_ENV, "<name>@<hub folder>", the host sets it) and "app-server" in its command:
  the exact marker of the session whose thread file knows this session id, any session of this hub if none does.
- resumable(host, name, sess) — whether the session's next process on this host continues its conversation: the thread
  of its core session id (sessions/<name>/codex-threads.json) has its file in codex-home/sessions (core/system.returning
  brings a system agent back with its conversation only then). None — the session does not go to Codex here.
- compacted(proc, name) (the core's compaction) and compacted_inside(proc, name) (Codex compacted by itself inside a
  session's turn: 0.162.1 sends only a contextCompaction item, before the turn's input or after a step, and no
  thread/compacted; a Codex that sends thread/compacted inside a turn gets it once too): a direction's session gets
  `hub direction resume` as context, as the core's SessionStart hook gives it to claude
  (hooks/hub_hook.compacted_context, run as `hub_hook.py --compacted` in a thread of its own). It goes in by
  thread/inject_items — a developer message in the thread's history, which the next model call sees, idle or inside a
  running turn alike. After the core's compaction the session's input is held (proc.held) until the context is in (the
  answer to thread/inject_items, at most INJECT_WAIT) or known to be empty; inside a running turn nothing is held.
Probe of codex-cli 0.162.1 with a fake Responses API (05.11): thread/inject_items needs no capability and reaches the
model as a developer message, idle and mid-turn, and stays in history; turn/start.additionalContext (proc.turn_extra)
is refused (-32600) without capabilities.experimentalApi in initialize and is not in Codex before 0.140 — not used here.
"""
import glob
import json
import os
import subprocess
import sys
import threading
import uuid

import platforms

BY = "codex"             # session.recap by: the host's recap after a big turn (as "claude" for Claude Code's /recap)
LIFE = "hub/life"        # the method of the lines this module's threads put into the host's inbox (not a Codex method)
CONTEXT_WAIT = 10        # seconds for `hub_hook.py --compacted` (it keeps to its own 7 s budget inside)
INJECT_WAIT = 10         # seconds the held input waits for the answer to thread/inject_items, at most
RECAP_AT_ONCE = 2        # recaps (model calls) of one host at a time; more — skipped, as a failed /recap
RECAP_TURNS = 4          # the thread's last turns the recap model sees
RECAP_CHARS = 8000       # and no more characters of them (the latest stay)
PIECE = 1500             # one message of the tail, at most
RECAP_PROMPT = ("You write a recap of an AI coding agent's conversation for its owner, who was away: one or two "
                "sentences — the goal, where the work is now, the next step. Use the language the user writes in. "
                "Only what the conversation shows; no quotes, no generic words. Answer with JSON only.")
RECAP_SCHEMA = {"type": "object", "required": ["recap"], "properties": {"recap": {"type": "string"}}}
HOOK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks", "hub_hook.py")


def _recaps(host):
    """The host's sessions whose recap is being written by the model now (RECAP_AT_ONCE)."""
    return host.__dict__.setdefault("life_recaps", set())


def _life(proc):
    """This module's state on the process: recap requests in flight (request id -> host), the latest compaction's
    token, thread/inject_items in flight (request id -> the hold token it releases)."""
    return proc.__dict__.setdefault("life", {"recap": {}, "hold": None, "inject": {}, "inside": None})


def _release(proc, token):
    """The held input goes on (hosts/codex._flush through its host): main thread only, as every line's parsing."""
    inject = _life(proc)["inject"]
    for rid in [r for r, t in inject.items() if t == token]:
        del inject[rid]   # released by the deadline: a late answer is nobody's
    if token in proc.held:
        proc.held.discard(token)
        if proc.host is not None:
            proc.host._flush(proc)


def _post(host, name, params):
    """A line for the host's own poll (the hubd thread parses it, as any line of the session): from this module's threads."""
    with host.lock:
        host.inbox.put((name, {"method": LIFE, "params": params}, host.clock()))


# --- the recap after a big turn ---------------------------------------------------------------------------------------
def recap(host, name, proc):
    if proc is None or not host.alive(name):
        raise RuntimeError("no process")
    if not proc.thread:
        raise RuntimeError("thread not ready")
    running = _recaps(host)
    asked = sum(len(_life(p)["recap"]) for p in host.procs.values())   # thread tails on their way
    if _life(proc)["recap"] or name in running or len(running) + asked >= RECAP_AT_ONCE:
        return "skipped"   # one is on its way: the next big turn asks again
    rid = proc.call("thread/turns/list", {"threadId": proc.thread, "limit": RECAP_TURNS, "itemsView": "full",
                                          "sortDirection": "desc"})
    _life(proc)["recap"][rid] = host
    return "sent"


def tail(result):
    """The text of thread/turns/list turns (newest first, as asked) for the recap model: oldest first, the latest
    RECAP_CHARS characters."""
    from hosts import codex_items
    parts = []
    for turn in reversed([t for t in result.get("data") or [] if isinstance(t, dict)]):
        for item in turn.get("items") or []:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "userMessage":
                text = "\n".join(c.get("text", "") for c in item.get("content") or []
                                 if isinstance(c, dict) and c.get("type") == "text")
                parts.append("User: " + text.strip()[:PIECE])
            elif kind == "agentMessage" and (item.get("text") or "").strip():
                parts.append("Agent: " + item["text"].strip()[:PIECE])
            elif codex_items.tool(item):
                tname, inp = codex_items.tool(item)
                parts.append(f"Step {tname}: " + json.dumps(inp, ensure_ascii=False)[:200])
    text = "\n\n".join(p for p in parts if p.split(":", 1)[1].strip())
    return text[-RECAP_CHARS:]


def _recap(host, name, text):
    from core import codexmodel
    try:
        got = codexmodel.ask(text, RECAP_PROMPT, RECAP_SCHEMA, state_dir=host.state_dir, by="recap")
    except Exception as e:  # noqa: BLE001 — no recap this time (no sign-in, a limit, a bad answer): as a failed /recap
        print(f"codex: recap of {name} failed: {e!r}", file=sys.stderr, flush=True)
        return
    finally:
        _recaps(host).discard(name)
    text = " ".join(str((got or {}).get("recap") or "").split())
    if text:
        _post(host, name, {"recap": text})


def response(proc, name, method, params, msg):
    if method == "thread/inject_items" and msg.get("id") in _life(proc)["inject"]:
        _release(proc, _life(proc)["inject"].pop(msg["id"]))   # the context is in: the held input goes on
        if "error" in msg:
            print(f"codex: the context after compaction of {name} is not accepted: {msg['error']}", file=sys.stderr, flush=True)
        return []
    if method != "thread/turns/list":
        return None
    host = _life(proc)["recap"].pop(msg.get("id"), None)
    if host is None:
        return None   # not this module's request
    if "error" in msg:
        print(f"codex: recap {name}: thread/turns/list: {msg['error']}", file=sys.stderr, flush=True)
        return []
    text = tail(msg.get("result") or {})
    running = _recaps(host)
    if text and name not in running and len(running) < RECAP_AT_ONCE:
        running.add(name)
        try:
            threading.Thread(target=_recap, args=(host, name, text), daemon=True, name=f"codex-recap-{name}").start()
        except RuntimeError as e:   # no thread to start: no recap this time, and not "one is running" forever
            running.discard(name)
            print(f"codex: recap of {name} failed: {e!r}", file=sys.stderr, flush=True)
    return []


# --- the context after a compaction ---------------------------------------------------------------------------------
def _context(host, name, token):
    """`hub direction resume` for a direction's session, as the core's hook gives it to claude; always posts back (an
    empty context releases the hold)."""
    text = ""
    try:
        env = {**platforms.current.owner_env(os.environ), **(getattr(host, "env", None) or {}), "HUB_SESSION": name,
               "HUB_STATE_DIR": host.state_dir, "PYTHONIOENCODING": "utf-8"}
        r = platforms.current.run([*platforms.current.python(), HOOK, "--compacted"], CONTEXT_WAIT,
                                  capture_output=True, stdin=subprocess.DEVNULL, env=env, **platforms.current.apart())
        text = r.stdout.decode("utf-8", "replace").strip() if r.returncode == 0 else ""
    except Exception as e:  # noqa: BLE001 — no context this time; the hold must be released anyway
        print(f"codex: the context after compaction of {name}: {e!r}", file=sys.stderr, flush=True)
    finally:
        _post(host, name, {"context": text, "token": token})


def _ask_context(proc, name, hold):
    host = getattr(proc, "host", None)
    if host is None:
        return
    token = uuid.uuid4().hex
    _life(proc)["hold"] = token
    if hold:
        proc.held.add(token)   # the next turn/start waits for the context (hosts/codex._flush)
    threading.Thread(target=_context, args=(host, name, token), daemon=True, name=f"codex-context-{name}").start()


def compacted(proc, name):
    _ask_context(proc, name, hold=True)
    return []


def compacted_inside(proc, name):
    _life(proc)["inside"] = proc.turn
    _ask_context(proc, name, hold=False)   # the turn goes on: the context goes into it, nothing waits
    return []


def notice(proc, name, method, params):
    if method == "thread/compacted":
        # a Codex that signals its own compaction inside a running turn by thread/compacted (0.162.1 does not): once a
        # turn, and not after the core's compaction (no running turn then, or it is the compaction turn)
        if proc.turn and not proc.compacting and not proc.compacts and _life(proc)["inside"] != proc.turn:
            compacted_inside(proc, name)
        return []
    if method != LIFE:
        return []
    if "release" in params:   # the answer to thread/inject_items did not come in INJECT_WAIT: the input goes on anyway
        _release(proc, params["release"])
        return []
    if "recap" in params:
        return [("session.recap", name, {"text": str(params["recap"])[:600], "by": BY})]
    if "token" in params:
        token, life = params["token"], _life(proc)
        if token == life["hold"] and params.get("context") and proc.thread:   # the latest compaction's, not a stale one
            rid = proc.call("thread/inject_items", {"threadId": proc.thread, "items": [
                {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": params["context"]}]}]})
            if token in proc.held:
                life["inject"][rid] = token   # held until the answer: the next turn's first model call sees it
                timer = threading.Timer(INJECT_WAIT, _post, (proc.host, name, {"release": token}))
                timer.daemon = True
                timer.start()
            life["hold"] = None
            return []
        if token == life["hold"]:
            life["hold"] = None
        _release(proc, token)
    return []


# --- orphans and conversations ------------------------------------------------------------------------------------
def _name_of(host, session_id):
    """The session whose thread file (sessions/<name>/codex-threads.json) knows this core session id, or None."""
    for path in glob.glob(os.path.join(glob.escape(host.state_dir), "sessions", "*", "codex-threads.json")):
        try:
            with open(path, encoding="utf-8") as f:
                if session_id in (json.load(f) or {}):
                    return os.path.basename(os.path.dirname(path))
        except (OSError, ValueError, TypeError):
            continue
    return None


def reap(host, pid, session_id):
    if not pid or not session_id or any(p.popen.pid == pid for p in host.procs.values()):
        return False
    name, suffix = _name_of(host, session_id), "@" + host.state_dir
    return platforms.current.reap_tagged(pid, lambda tag, cmd: "app-server" in cmd and (
        tag == host.tree_tag(name) if name else tag.endswith(suffix)))


def has_thread(host, name, session_id):
    """The Codex conversation of this core session id is on disk: thread/resume continues it (otherwise a new thread)."""
    thread = host._thread_of(name, session_id)
    if not isinstance(thread, str) or not thread or any(c in thread for c in "/\\*?["):
        return False
    pattern = os.path.join(glob.escape(host.home), "sessions", "*", "*", "*", f"rollout-*-{thread}.jsonl")
    return bool(glob.glob(pattern))


def resumable(host, name, sess):
    """None — this session's next process is not a Codex one here; otherwise whether it continues the conversation."""
    from hosts.codex import is_codex
    if not (host.all_sessions or is_codex(sess.get("model"))):
        return None
    return has_thread(host, name, sess.get("session_id"))


def turn_done(proc, name, turn):
    return []
