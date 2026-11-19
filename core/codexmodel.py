"""Codex host (experimental second engine): the core's model on a hub on Codex (hub.json
`"host": "codex"`) — the watcher ("awaits you", urgency, alarms), the push text, the voice digest. Instead of
`claude -p --model haiku`: a `codex app-server` process per call in the hub's Codex folder (CODEX_HOME = <state
folder>/codex-home, the hub's sign-in, not the personal ~/.codex), an `ephemeral` thread (not written to disk),
`baseInstructions` — the call's prompt, `sandbox: read-only`, `approvalPolicy: never`, the answer by the turn's
`outputSchema` (probe 2026-10-19: ~2 s, 9.6 thousand input tokens). The input is other people's text (a session's result,
a module's text), so ask() leaves the model no tools: HUB_CORE_MODEL=1 makes the core's hook (hooks/deny_hint.py) refuse
every tool, the shell tool is off in the thread (hosts/codex_set.CORE_MODEL_OFF), and the call is made only after
Codex's own hooks/list shows the core's hook trusted in the folder the call runs in (the hub's, or the Codex pool's
active account's) — otherwise Untrusted, no thread, no turn.

The model is hub.json `codex.watch_model` (gpt-6-luna by default) and `codex.watch_effort` (low). No sign-in or a limit —
the next call not earlier than in PAUSE (one log line), not on every turn result; a new sign-in (the sign-in file of the
hub's Codex folder changed — `hub codex login` from another process) lifts the pause. The call's tokens are the fact
`codex.spent` in the journal (the caller is the entity), "Usage" counts them like the sessions' turns. Tests do not call
the live model (HUB_NO_MODEL) unless given their own codex.
"""
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time

from core import catalog

WATCH_MODEL = "gpt-6-luna"   # owner decision q621320: the watcher, summaries and the core's model — gpt-6-luna
WATCH_EFFORT = "low"
TIMEOUT = 90
PAUSE = 10 * 60
# signs of "no sign-in" and "a limit" in a Codex refusal (codexErrorInfo of a turn, the error text of thread/start)
DOWN = ("unauthorized", "usagelimitexceeded", "ratelimitexceeded", "not logged in", "login", "refresh token",
        "usage limit", "rate limit", "requiresopenaiauth")
CLAUDE_ENV = ("CLAUDE", "ANTHROPIC", "HUB_", "MCP_TIMEOUT")
_UNTRUSTED = {}   # the reason of a call not made (the core's hook not trusted) -> when it was last logged
_DOWN = {"until": 0.0, "auth": None}   # the pause: until when, and what the sign-in file was when it began
# the marks of the core's own model call for the hook (hooks/deny_hint.core_model): the call only answers — every tool is
# refused. Only ask() sets them: the critic (needs to read) and the other commands built from env() run without
CORE_MODEL_ENV = {"HUB_ON_CODEX": "1", "HUB_CORE_MODEL": "1"}
# bin/hub refusal on a hub on Codex: the catalog key (parameter name), translated where it is printed
NO_THREAD = "cli.codex.no_thread"


class Error(ValueError):
    spent = (0, 0)   # tokens (in, out) the failed call had already used


class BadAnswer(Error):
    """The model answered off the schema — sign-in and limit have nothing to do with it, no pause (the answer may contain "login")."""


class Off(Error):
    """No call was made: the model is off in tests (HUB_NO_MODEL) or paused after a refusal (no sign-in, a limit)."""


class Untrusted(Error):
    """No call was made: Codex does not run the core's hook in the folder (the file gone, changed, hooks/list failed) — the
    hook is what leaves the model no tools (hooks/deny_hint.py, HUB_CORE_MODEL)."""


class Timeout(Error):
    """No answer in time."""


def state_dir_of(state_dir=None):
    if state_dir:
        return state_dir
    from core.daemon import default_state_dir
    return default_state_dir()


def config(state_dir=None):
    try:
        return catalog.load_config(state_dir_of(state_dir))
    except (OSError, ValueError):
        return {}


def on_codex(state_dir=None, cfg=None):
    """The whole hub on Codex (hub.json "host": "codex")."""
    return (config(state_dir) if cfg is None else cfg).get("host") == "codex"


def side_home(state_dir=None, cfg=None):
    """The Codex folder of this hub's critic and voice — the only ones that ever took the personal ~/.codex (the spec of
    several hubs, 2.12): a hub on Codex, or a Claude hub with its own Claude sign-in (hub.json claude_config_dir: it
    takes nothing of the OS user's personal sign-ins) — the hub's Codex folder ("" when that holds no sign-in: then no
    Codex at all, never the personal one); a Claude hub on the shared sign-in (the main hub, an instance with the shared
    one) — None: the personal ~/.codex, as before."""
    cfg = config(state_dir) if cfg is None else cfg
    if not on_codex(cfg=cfg) and not cfg.get("claude_config_dir"):
        return None
    from core import codexcli
    at = codexcli.home_of(state_dir_of(state_dir))   # the hub folder (a pool account's needs its own arguments)
    return at if on_codex(cfg=cfg) or os.path.isfile(os.path.join(at, "auth.json")) else ""


def hub_command(state_dir=None):
    """This hub's terminal command: hub, or hub-<name> of another hub of the machine (core/instance) — what a remedy
    names (`hub-<name> codex login` signs that hub in; `hub codex login` would sign in the main one)."""
    from core import instance
    return instance.command_name(instance.name(state_dir_of(state_dir)))


def home(state_dir=None):
    """The Codex folder the core's model runs in: the Codex pool's active account's (otherwise it would burn account 1
    and fail while it is at its limit), the hub's own without a pool."""
    from core import codexcli
    return codexcli.active_home(state_dir_of(state_dir))


def binary():
    """codex — as the host's (hosts/codex.codex_bin): HUB_CODEX_BIN, this hub's own copy, PATH, ~/.local/bin."""
    from hosts.codex import codex_bin
    return codex_bin()


def env(state_dir=None, base=None, at=None):
    """The environment of codex for the core's model and the critic: the hub's CODEX_HOME (at — another folder: the
    Codex pool's active account's, for ask), without Claude's sign-in and the hub's variables. A command a caller builds
    itself (the critic's codex exec, --version) runs in the hub folder: an account folder needs its arguments too."""
    base = os.environ if base is None else base
    return {**{k: v for k, v in base.items() if not k.startswith(CLAUDE_ENV)},
            "CODEX_HOME": at or os.path.join(state_dir_of(state_dir), "codex-home")}


def strict(schema):
    """The answer schema for outputSchema: objects have all fields required and no extra ones (strict mode demands it)."""
    if not isinstance(schema, dict):
        return schema
    out = {k: strict(v) if isinstance(v, dict) else v for k, v in schema.items()}
    if out.get("type") == "object" and isinstance(out.get("properties"), dict):
        out["properties"] = {k: strict(v) for k, v in out["properties"].items()}
        out["required"] = list(out["properties"])
        out["additionalProperties"] = False
    if isinstance(out.get("items"), dict):
        out["items"] = strict(out["items"])
    return out


def down(why):
    text = json.dumps(why, ensure_ascii=False).lower() if not isinstance(why, str) else why.lower()
    return any(w in text for w in DOWN)


def _auth_stamp(state_dir):
    """The sign-in file of the hub's Codex folder: (mtime, size) or None — whether the sign-in changed since the pause began."""
    try:
        st = os.stat(os.path.join(home(state_dir), "auth.json"))
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def picks(state_dir=None, cfg=None):
    """Which Codex model and effort, by hub.json codex: {"core": (watch_model, watch_effort) — the core's model, with the
    defaults; "session": (model, effort) — the sessions' (None — Codex's own default)} — one reading for the core's model,
    a module's call (core/modelcall.call_codex) and the evals."""
    own = (config(state_dir) if cfg is None else cfg).get("codex")
    own = own if isinstance(own, dict) else {}
    return {"core": (own.get("watch_model") or WATCH_MODEL, own.get("watch_effort") or WATCH_EFFORT),
            "session": (own.get("model"), own.get("effort"))}


def ask(text, prompt, schema, state_dir=None, codex=None, model=None, effort=None, timeout=TIMEOUT, now=None, by="model"):
    """One question to the core's Codex model -> the answer object by the schema; schema None — the answer text (a str).
    A failure is Error (the watcher moves the question, as on a claude -p failure); Off — no call was made. by — who asks
    (watch, push, voice, mod:<module>): the entity of codex.spent is "codex:<by>", not a session name."""
    now = time.time() if now is None else now
    if codex is None and os.environ.get("HUB_NO_MODEL"):   # tests: do not call the live model
        raise Off("codex: the model is off (HUB_NO_MODEL)")
    # a new sign-in lifts the pause on a limit too (another account); a token refresh by a session too: one extra call
    if now < _DOWN["until"] and _auth_stamp(state_dir) != _DOWN["auth"]:
        _DOWN["until"] = 0.0
        print("codex: the core's model — the sign-in changed, the pause is lifted", file=sys.stderr, flush=True)
    if now < _DOWN["until"]:
        raise Off(f"codex: no sign-in or a limit — the next call after {time.strftime('%H:%M', time.localtime(_DOWN['until']))}")
    core = picks(state_dir)["core"]
    model, effort = model or core[0], effort or core[1]
    try:
        from core import codexcli
        from hosts import codex_set
        at = home(state_dir)   # once: hubd may rewrite the active folder between two reads
        answer, used = run(list(codex or [binary()]), text, prompt, schema, model, effort, timeout,
                           {**env(state_dir, at=at), **CORE_MODEL_ENV},
                           extra=[*codexcli.args(state_dir_of(state_dir), at), *codex_set.core_model_args()], hook_home=at)
    except Error as e:
        if isinstance(e, Untrusted):
            _untrusted_line(e, now)
        _spent(state_dir, by, model, e.spent)
        if not isinstance(e, (BadAnswer, Untrusted)) and down(str(e)):
            _DOWN.update(until=now + PAUSE, auth=_auth_stamp(state_dir))
            print(f"codex: the core's model — no sign-in or a limit, calls paused for {PAUSE // 60} min: {e}",
                  file=sys.stderr, flush=True)
        raise
    _spent(state_dir, by, model, used)
    return answer


def _untrusted_line(e, now):
    """The hub log line for a call that was not made because Codex does not run the core's hook: one per PAUSE for the
    same reason (every watcher result asks again, the hook may be trusted by then — no pause — but the log is not flooded)."""
    why = str(e)
    if now - _UNTRUSTED.get(why, -PAUSE) >= PAUSE:
        _UNTRUSTED[why] = now
        print(f"codex: the core's model was not called: {why}", file=sys.stderr, flush=True)


def _spent(state_dir, by, model, spent):
    """The call's tokens — the fact codex.spent into the core's mailbox (no channel: the core's model process, admit.UNNAMED)."""
    if not (spent[0] or spent[1]):
        return
    from core import spool
    try:
        spool.put(os.path.join(state_dir_of(state_dir), "spool"), "codex.spent", f"codex:{by}",
                  {"model": model, "tokens_in": spent[0], "tokens_out": spent[1]}, src="model")
    except OSError as e:
        print(f"codex: the core's model spending was not recorded: {e}", file=sys.stderr, flush=True)


def _failed(cls, text, spent):
    e = cls(text)
    e.spent = tuple(spent)
    return e


def run(cmd, text, prompt, schema, model, effort, timeout, run_env, cwd=None, developer=False, items=None, extra=(),
        hook_home=None):
    """One ephemeral read-only thread of `codex app-server` -> (the answer: an object by the schema, or the text when the
    schema is None; tokens (in, out)). prompt is the thread's baseInstructions (the call's whole prompt, as the core's
    model); developer=True — its developerInstructions on top of Codex's own, as a session of a hub on Codex gets the
    core's rules (evals of session rules). cwd — where the thread works (an eval's files), by default the temp folder.
    items — history put into the thread before the turn (thread/inject_items, as the host gives a session the context
    after a compaction: hosts/codex_life), the turn starts once it is in. hook_home — the Codex folder whose core hook must
    be trusted (hooks/list of this same process, before the thread): not — Untrusted, no thread, no turn. No pause, no
    journal: ask() and the evals add them."""
    from hosts import codex_set   # the isolation pins: a trusted folder's .codex/config.toml does not turn subagents on
    cwd = cwd or tempfile.gettempdir()
    try:
        p = subprocess.Popen(cmd + ["app-server", *extra, *codex_set.args()], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1, cwd=cwd, env=run_env)
    except OSError as e:
        raise Off(f"codex: {e}") from e   # not started — nothing spent
    lines = queue.Queue()

    def read():
        try:
            for line in p.stdout:
                lines.put(line)
        except (OSError, ValueError):   # the stream was closed in finally while it was being read
            pass
        lines.put(None)
    threading.Thread(target=read, daemon=True).start()

    last_text, texts, spent, thread = "", [], [0, 0], None

    def write(obj):
        try:
            p.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
            p.stdin.flush()
        except (OSError, ValueError) as e:   # app-server exited (at once: no such subcommand, a broken folder)
            raise _failed(Error, f"codex app-server does not take input: {e}", spent) from None

    end = time.time() + timeout
    try:
        write({"id": 0, "method": "initialize", "params": {"clientInfo": {"name": "agents-hub-model", "version": "exp"}}})
        write({"method": "initialized"})
        def start_thread():
            write({"id": 1, "method": "thread/start", "params": {
                "cwd": cwd, "approvalPolicy": "never", "sandbox": "read-only", "model": model, "ephemeral": True,
                "developerInstructions" if developer else "baseInstructions": prompt}})
        if hook_home:   # the thread only after Codex says it runs the core's hook here: no hook, a model with a shell
            write({"id": 4, "method": "hooks/list", "params": {"cwds": [cwd]}})
        else:
            start_thread()
        while True:
            try:
                line = lines.get(timeout=max(0.0, end - time.time()))
            except queue.Empty:
                raise _failed(Timeout, f"codex: no answer within {timeout} s", spent)
            if line is None:
                raise _failed(Error, "codex app-server exited", spent)
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("id") == 4:
                from hosts import codex_rules
                if "error" in msg or "result" not in msg:
                    raise _failed(Untrusted, f"codex: hooks/list: {(msg.get('error') or {}).get('message') or msg.get('error')}"
                                             " — the core's hook is not confirmed, no call", spent)
                hooks = [h for d in (msg["result"] or {}).get("data") or [] for h in d.get("hooks") or []]
                if not codex_rules.listed_trusted(hooks, hook_home):
                    raise _failed(Untrusted, f"codex: Codex does not run the core's hook in {hook_home} (no hooks.json, "
                                             "changed or not trusted) — no call, the model would have tools", spent)
                start_thread()
                continue
            if msg.get("id") in (1, 2, 3) and "error" in msg:
                raise _failed(Error, f"codex: {(msg['error'] or {}).get('message') or msg['error']}", spent)
            if msg.get("id") == 1 and "result" in msg:
                thread = ((msg["result"] or {}).get("thread") or {}).get("id")
                if items:   # the history first, the turn once it is in
                    write({"id": 3, "method": "thread/inject_items", "params": {"threadId": thread, "items": list(items)}})
                    continue
            if msg.get("id") in (1, 3) and "result" in msg:
                write({"id": 2, "method": "turn/start", "params": {
                    "threadId": thread, "effort": effort, **({"outputSchema": strict(schema)} if schema is not None else {}),
                    "input": [{"type": "text", "text": text}]}})
                continue
            method, params = msg.get("method"), msg.get("params") or {}
            if "id" in msg and method:   # a request to the client (a permission, a question) — not for the core's model
                write({"id": msg["id"], "error": {"code": -32601, "message": "the core's model asks nothing"}})
            elif method == "item/completed" and (params.get("item") or {}).get("type") == "agentMessage":
                item = params["item"]
                last_text = item.get("text") or last_text
                texts.append((item.get("phase"), item.get("text") or ""))
            elif method == "thread/tokenUsage/updated":   # on every model call: last — its tokens (as at the host)
                last = (params.get("tokenUsage") or {}).get("last") or {}
                spent = [spent[0] + (last.get("inputTokens") or 0), spent[1] + (last.get("outputTokens") or 0)]
            elif method == "error" and not params.get("willRetry"):
                raise _failed(Error, f"codex: {json.dumps(params.get('error'), ensure_ascii=False)[:300]}", spent)
            elif method == "turn/completed":
                turn = params.get("turn") or {}
                if turn.get("status") != "completed":
                    raise _failed(Error, f"codex: turn {turn.get('status')}: "
                                         f"{json.dumps(turn.get('error'), ensure_ascii=False)[:300]}", spent)
                if schema is None:   # the text — the answer's messages, as a session's turn result
                    from types import SimpleNamespace
                    from hosts.codex import _answer
                    return _answer(SimpleNamespace(texts=texts, last_text=last_text), limit=10 ** 9), tuple(spent)
                try:
                    out = json.loads(last_text)
                except ValueError:
                    raise _failed(BadAnswer, f"codex: the answer is not JSON: {last_text[:200]}", spent) from None
                if not isinstance(out, dict):
                    raise _failed(BadAnswer, f"codex: the answer is not an object: {last_text[:200]}", spent)
                return out, tuple(spent)
    finally:
        try:
            p.stdin.close()
        except OSError:
            pass
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()


def take_command(state_dir, name, session_id, base=None, codex=None):
    """`hub take` on a hub on Codex: (command, environment) — `codex resume <thread>` in the hub's Codex folder, without
    questions and a sandbox, as the session runs at the core; this session has no thread — None."""
    try:
        with open(os.path.join(state_dir, "sessions", name, "codex-threads.json"), encoding="utf-8") as f:
            thread = (json.load(f) or {}).get(session_id)
    except (OSError, ValueError, AttributeError):
        thread = None
    if not thread:
        return None
    run_env = {**env(state_dir, base), "HUB_SESSION": name, "HUB_STATE_DIR": state_dir}
    return [codex or binary(), "resume", "--dangerously-bypass-approvals-and-sandbox", thread], run_env
