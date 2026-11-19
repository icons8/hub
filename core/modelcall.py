"""A short model call for a core module (Module API 2.7, the model right; EXTENDING "Model through the core"): the same
`claude -p` as the watchdog line (modules/watch/watch.py:_claude) - without tools, settings and MCP, without saving the
conversation, on the active pool account. Called by the page process (modules/web/server.py: /api/module-model): it
reads the pool token, the module gets only the answer. The limits live here too: what can be asked and how many times.
"""
import json
import os
import subprocess
import tempfile
import threading
import time

from core import catalog, ownclaude

CAP = "model"                  # a module.json right (Module API 2.7)
MODELS = ("haiku", "sonnet")   # Claude Code aliases - the latest model of the family; costlier ones are not for modules
MAX_INPUT = 100_000            # prompt, input and schema together, characters
# the prompt goes to claude as a file (--system-prompt-file), the input via stdin, and the schema as a command-line
# argument: its length is as Windows would write it (subprocess.list2cmdline); 6 000 leaves headroom under cmd.exe
# (8 191 characters per line), through which claude.cmd from npm is launched
MAX_SCHEMA = 6_000
MAX_TOKENS, DEFAULT_TOKENS = 4096, 1024
MAX_TIMEOUT, DEFAULT_TIMEOUT = 120, 60
PER_MODULE, TOTAL = 2, 4       # at once: for one module / for all
PER_HOUR = 60                  # calls of a module in the last hour
HOUR = 3600
CALLS = "model-calls.json"     # call times for the hour per module - in the state folder: a page restart does not reset them
STATUS = {"bad": 400, "denied": 403, "off": 403, "busy": 429, "limit": 429, "failed": 502, "unavailable": 503,
          "timeout": 504}


class Error(Exception):
    """A refusal with a code (STATUS): the text is for the log, not for parsing."""

    def __init__(self, code, text="", usd=None, ran=True, model=None):
        super().__init__(text or code)
        self.code, self.usd = code, usd   # usd - the price, if claude managed to give it (an error after the API request)
        self.ran = ran                    # claude was started: the call is a spend (possibly without a price)
        self.model = model                # the model that was called, when it is not the asked alias (a hub on Codex)


def checked(body):
    """A module's request body -> (prompt, text, model, schema, max_tokens, timeout); anything else - Error bad."""
    if not isinstance(body, dict):
        raise Error("bad", "the body is a JSON object")
    prompt, text = body.get("prompt"), body.get("text", "")
    model, schema = body.get("model", "haiku"), body.get("schema")
    tokens, timeout = body.get("max_tokens", DEFAULT_TOKENS), body.get("timeout", DEFAULT_TIMEOUT)
    if not isinstance(prompt, str) or not prompt.strip():
        raise Error("bad", "prompt is a non-empty string")
    if not isinstance(text, str):
        raise Error("bad", "text is a string")
    if model not in MODELS:
        raise Error("bad", f"model {model!r} is not allowed: {', '.join(MODELS)}")
    if schema is not None and not isinstance(schema, dict):
        raise Error("bad", "schema is a JSON Schema object")
    if isinstance(tokens, bool) or not isinstance(tokens, int) or not 1 <= tokens <= MAX_TOKENS:
        raise Error("bad", f"max_tokens is an integer from 1 to {MAX_TOKENS}")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= MAX_TIMEOUT:
        raise Error("bad", f"timeout is from 1 to {MAX_TIMEOUT} seconds")
    try:
        prompt.encode("utf-8")
    except UnicodeEncodeError:
        raise Error("bad", "prompt is not valid text (a lone surrogate)") from None
    if "\0" in prompt:
        raise Error("bad", "prompt has a NUL character")
    argv = len(subprocess.list2cmdline([json.dumps(schema)])) if schema is not None else 0   # as it will go into command()
    if argv > MAX_SCHEMA:
        raise Error("bad", f"schema is {argv} characters on the command line, over {MAX_SCHEMA}")
    size = len(prompt) + len(text) + (len(json.dumps(schema, ensure_ascii=False)) if schema is not None else 0)
    if size > MAX_INPUT:
        raise Error("bad", f"prompt, text and schema are {size} characters, over {MAX_INPUT}")
    return prompt, text, model, schema, tokens, timeout


class Limits:
    """How many calls are running now (in memory) and how many there were in the hour (the CALLS file in the state folder)."""

    def __init__(self, state_dir, clock=time.time):
        self.path, self.clock = os.path.join(state_dir, CALLS), clock
        self.lock, self.live = threading.Lock(), {}

    def _hour(self, now):
        try:
            with open(self.path, encoding="utf-8") as f:
                got = json.load(f)
        except (OSError, ValueError):
            got = {}
        got = got if isinstance(got, dict) else {}
        return {m: [t for t in ts if isinstance(t, (int, float)) and now - HOUR < t <= now]
                for m, ts in got.items() if isinstance(ts, list)}

    def take(self, module):
        """A slot for a module's call, or Error busy/limit. The time is written at once: a call without an answer also counts toward the hour."""
        with self.lock:
            now = self.clock()
            if self.live.get(module, 0) >= PER_MODULE or sum(self.live.values()) >= TOTAL:
                raise Error("busy", f"{self.live.get(module, 0)} calls of the module and {sum(self.live.values())} in all are running "
                                    f"(at most {PER_MODULE} per module and {TOTAL} in all)")
            hour = self._hour(now)
            if len(hour.get(module, [])) >= PER_HOUR:
                raise Error("limit", f"{PER_HOUR} calls in an hour — the next one after "
                                     f"{time.strftime('%H:%M', time.localtime(min(hour[module]) + HOUR))}")
            hour[module] = hour.get(module, []) + [now]
            catalog.write_private(self.path, {m: ts for m, ts in hour.items() if ts})
            self.live[module] = self.live.get(module, 0) + 1

    def give(self, module):
        with self.lock:
            self.live[module] = self.live.get(module, 1) - 1
            if self.live[module] <= 0:
                del self.live[module]


def command(prompt_file, model, schema):
    from core import realclaude   # the real claude, bypassing the session wrapper
    return [realclaude.find(), "-p", "--model", model, *(["--effort", "low"] if model == "haiku" else []),
            "--tools", "", "--setting-sources", "", "--strict-mcp-config", "--no-session-persistence",
            "--output-format", "json", "--system-prompt-file", prompt_file,
            *(["--json-schema", json.dumps(schema)] if schema is not None else [])]


def call(prompt, text, model, schema, tokens, timeout, account=None, run=None):
    """One call -> {"text"|"json", "usd", "model"} or Error (timeout, failed, unavailable). usd - the price at API rates,
    as claude gave it, or None."""
    if os.environ.get("HUB_NO_MODEL"):   # tests: do not call the live model
        raise Error("unavailable", "the model is off (HUB_NO_MODEL)", ran=False)
    from modules.watch.watch import QUICK, model_env
    try:
        env = {**model_env(account=account), **QUICK, "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(tokens)}
    except OSError as e:   # the login module gave no profile login (core/profiles.NoProfile) - same as an unavailable model
        raise Error("unavailable", str(e), ran=False) from None
    fd, prompt_file = tempfile.mkstemp(prefix="hub-model-", suffix=".txt")   # 0600: the module's prompt is for us only
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(prompt)
        cmd = command(prompt_file, model, schema)   # the release's own copy turns its updates off here (core/ownclaude)
        r = (run or subprocess.run)(cmd, input=text, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    timeout=timeout, cwd=tempfile.gettempdir(),
                                    env={**env, **{k: os.environ[k] for k in ownclaude.ENV if k in os.environ}})
    except subprocess.TimeoutExpired:
        raise Error("timeout", f"no answer in {timeout} s") from None
    except (OSError, ValueError) as e:   # no claude, or a line longer than the OS allows
        raise Error("failed", f"claude did not start: {e}", ran=False) from None
    finally:
        try:
            os.unlink(prompt_file)
        except OSError:
            pass
    try:
        out = json.loads(r.stdout)
    except ValueError:
        raise Error("failed", ((r.stderr or "") + (r.stdout or "")).strip()[-300:] or f"exit code {r.returncode}") from None
    if not isinstance(out, dict):
        raise Error("failed", str(out)[:300])
    usd = out.get("total_cost_usd")
    usd = usd if isinstance(usd, (int, float)) and not isinstance(usd, bool) else None
    if out.get("is_error"):
        raise Error("failed", str(out.get("result"))[:300], usd)
    # modelUsage may also hold an auxiliary model next to the called one: the one named from the family, otherwise the costlier
    usage = out.get("modelUsage") if isinstance(out.get("modelUsage"), dict) else {}
    used = max(usage, key=lambda k: (model in k, _cost(usage[k])), default=None)
    if schema is not None:
        if not isinstance(out.get("structured_output"), dict):
            raise Error("failed", f"the answer does not match the schema: {str(out.get('result'))[:200]}", usd)
        return {"json": out["structured_output"], "usd": usd, "model": used or model}
    return {"text": str(out.get("result") or ""), "usd": usd, "model": used or model}


def call_codex(prompt, text, model, schema, tokens, timeout, account=None, state_dir=None, by="model"):
    """Codex (experimental): the same call on a hub on Codex (hub.json "host": "codex") — through the hub's Codex
    (core/codexmodel.ask: its sign-in and CODEX_HOME, an ephemeral read-only thread), there is no claude. haiku is the
    core's model (codex.watch_model and watch_effort, low by default), sonnet — the sessions' model (codex.model and
    effort, medium by default). max_tokens and the Claude pool account do not apply (app-server has no output cap); usd
    is None — Codex spends tokens of a plan, they go to the journal as codex.spent ("Usage"). by — the entity of
    codex.spent after "codex:"."""
    from core import codexmodel
    picks = codexmodel.picks(state_dir)
    used, effort = picks["core"]
    if model == "sonnet":
        used, effort = picks["session"][0] or used, picks["session"][1] or "medium"
    try:
        got = codexmodel.ask(text, prompt, schema, state_dir=state_dir, model=used, effort=effort, timeout=timeout, by=by)
    except codexmodel.Off as e:   # tests, or paused after a refusal (no sign-in, a limit): nothing was started
        raise Error("unavailable", str(e), ran=False, model=used) from None
    except codexmodel.Timeout as e:
        raise Error("timeout", str(e), model=used) from None
    except codexmodel.Error as e:
        raise Error("failed", str(e), model=used) from None
    return {"json": got, "usd": None, "model": used} if schema is not None else {"text": got, "usd": None, "model": used}


def _cost(rec):
    got = rec.get("costUSD") if isinstance(rec, dict) else None
    return got if isinstance(got, (int, float)) and not isinstance(got, bool) else 0


def has_right(cfg, state_dir, name):
    """The module name found by the hub (no manifest errors) asks for the model right - one rule for the call, the button and the CLI."""
    from core import modules
    return any(CAP in (f["manifest"].get("capabilities") or []) for f in modules.discover(cfg, state_dir)
               if f["name"] == name and not f.get("errors"))
