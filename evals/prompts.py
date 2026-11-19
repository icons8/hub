#!/usr/bin/env python3
"""Evals of a single hub prompt: the same inputs, the prompt of two code versions, a real model, checks.

  evals/prompts.py <case.json>… [--before REV] [--after REV] [--trials K] [--only before|after] [--model M]

For prompts that are not a conversation with a session: the watcher's questions, a task for a plugin checker, whole rules
and skills (the prompt is the document itself). evals/run.py judges a session inside a running core by the journal; this one asks the model
directly — a turn takes seconds.

A case (evals/prompts/*.json, and evals/locale/ru/prompts/*.json — a Russian-speaking owner's, not published; a file name is
enough) names the prompt: a repo file and, for a constant in a Python file, its name (`before_file` /
`before_name`, if in the "before" version it lived elsewhere), `format` — for its {substitutions}, `schema` (right in the
case) or `schema_name` (a constant of the same file) — for a model that must answer JSON; `effort` — the reasoning
level, as for hub sessions (without it a JSON answer uses low, like the watcher); `call` — a function of the same
file that builds the text from the turn's `args` (it goes out as the message, the system prompt is the case's `system`). `append` —
constants ({file, name}; `optional` — skipped in a version that lacks it) after the prompt, as the core appends its addendum to a session's system prompt; with `append`
`format` fills only them, and the prompt stays as is. `tail` — a constant ({file, name}, with before_* as for the prompt) that the core
appends to the owner's word: it goes after each turn's input. `turns` — inputs with checks; an input may differ per
version ({"before": …, "after": …}) when the text the prompt reads is made by the code itself, or carry the core's words
as ⟦KEY⟧ with the turn's `consts` {KEY: {file, name, format?, optional?}} — that constant of each version (an `optional` one a
version lacks is an empty line, and the input may then leave the mark out); `format` {"lang": …} takes
a language code (ru, en) or any word a core used — each version gets the word its core puts into the rule. `executor` (of the case or of a turn: {agent, files: {CLAUDE.md,
CLAUDE.executor.md, CLAUDE.executor.src.md}, executor?: false for the agent itself, command?: {more arguments of host.command},
home?: a file of the version — hub-home/CLAUDE.md — for a session with no agent file, the hub itself (not an executor):
that file, then the addendum}) — the prompt is what the version's core gives that agent's executor (its whole addendum, built
by the version's hosts/streamjson from the files); the case's `prompt` then names only the schema. `files` are put into
the folder where the model works, `tools` allows it to read them. `hub_cli` ({"session": S}) — the model has the `hub`
of the version in its PATH (bin/hub from `git archive`, a state folder of its own, as session S): what it calls is
recorded; only commands that touch nothing but that state folder (RUN) are run, the rest are only recorded.

`trace` (the case): the model's own tool calls are recorded (stream-json) and the files it leaves in its folder are compared with
the case's `files` (a turn's `files` are laid over them): `tool` / `no_tool` (name — a regex over the tool name, `re` — over the
command or the file path, `except_re` — a path or command that does not count), `not_seen` (re: no tool result the model got matches it — a value it read, whatever the command), `changed` (every changed or new file matches
`only_re`; at least one matches `has_re`; `__pycache__`, `.git`, `.hub-eval` are not looked at), `file` (path + exists | min_len | re |
not_re over what the model left there; the whole folder is read, a seed file the model deleted is not there).

Answer checks: `field` (name + equals | min | max | re | not_re | max_words | item_re | item_not_re — the last two over
each item of a list field on its own, `^` is the start of a command; `with` — the body of a script another field
writes by a heredoc, read too when the field runs it) — over the JSON, `said` / `not_said` (re) —
over the text and the JSON strings, `any_of`; with `hub_cli` — `call` (re | not_re over the first `hub` call that is
not help, words joined as typed), `any_call` (re: some call that is not help matches), `no_call` (re: no call matches), `help_calls` (max). A missing field is a failure. A run without a claude login is n/a, not a pass.
A claude login is required (CLAUDE_CODE_OAUTH_TOKEN or ~/.claude); EVAL_CLAUDE — another executable.

Codex (experimental), `--host codex` — the default on a hub on Codex (hub.json "host": "codex"): the turn goes
to `codex app-server` (core/codexmodel.run: an ephemeral read-only thread). A case with `effort` is a session's rules:
the prompt plus the Codex addendum of the same revision (hosts/codex.py NOTE) are the thread's developerInstructions,
as a hub session on Codex gets them, on the sessions' model and effort (hub.json codex.model/effort or --codex-model,
--codex-effort; no effort set — the case's); a case without it
is a core-model prompt (the watcher): baseInstructions on codex.watch_model, low effort. The Codex folder is the run's
own: a copy of the evals' sign-in (<state>/codex-evals-home, `evals/run.py --codex-login`; under its lock, as evals/run.py
— the copy, refreshed by Codex, comes back) with a hub session's settings (hosts/codex_set: the isolation pins,
skill-installer and the user's own skills off), without the web search; the shell stays, its `hub` a stub answering
"ok" (the model may run `hub send` as a session does — nothing reaches a hub). Never the hub's sign-in (its refresh
token is one-time) and never the owner's own ~/.codex; --codex-home <folder> — another sign-in, taken the same way;
the thread's environment is a small one with a HOME of its own. The limits (account/rateLimits/read) before and after
are printed. A turn's `items` ([{role: user | developer, text}], or by version) go into the thread before it
(thread/inject_items) — what the host puts there itself (the context after a compaction); such a case is for Codex only.
No sign-in or a limit is n/a; the price is tokens.
"""
import argparse
import ast
import atexit
import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from common import LOGGED_OUT, WORK, find_case, guard_codex, on_codex  # noqa: E402 — shared by both eval runs
# how the hub asks its cheap model (the watcher): low effort, no prompt cache
QUICK = {"DISABLE_PROMPT_CACHING": "1"}
QUICK_ARGS = ["--effort", "low"]


class NotLoggedIn(Exception):
    pass


def source_at(repo, rev, path):
    r = subprocess.run(["git", "-C", repo, "show", f"{rev}:{path}"], capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"{path} is not in {rev}")
    return r.stdout


SAFE = {n: __builtins__[n] if isinstance(__builtins__, dict) else getattr(__builtins__, n)
        for n in ("list", "dict", "tuple", "set", "len", "range", "str", "int", "float")}


def constants(text):
    """Top-level constants `name = <expression of literals>` of a Python file (string concatenation, list(OTHER) of an earlier
    constant) without importing the file: top-level assignments are executed in order in an empty namespace, those
    that need more than literals are skipped. A core phrase `T(key, "English", word=…)` (core/coretext.py) is its
    English, as the session gets it."""
    space = {"__builtins__": SAFE, "T": lambda key, text, **fixed: text.format(**fixed) if fixed else text}
    for node in ast.parse(text).body:
        if isinstance(node, ast.Assign) and all(isinstance(t, ast.Name) for t in node.targets):
            try:
                value = eval(compile(ast.Expression(node.value), "<case>", "eval"), space)  # noqa: S307
            except Exception:  # noqa: BLE001 — not a literal: the case does not need it
                continue
            for t in node.targets:
                space[t.id] = value
    return space


def constant(text, name):
    space = constants(text)
    if name not in space:
        raise SystemExit(f"{name} is not a constant of the file")
    return space[name]


def function_at(text, name):
    """A top-level function of a Python file, separate from it: it sees the file's constants (the critic: BRIEF) and only
    the builtins from SAFE."""
    for node in ast.parse(text).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            space = constants(text)
            exec(compile(ast.Module([node], []), "<case>", "exec"), space)  # noqa: S102
            return space[name]
    raise SystemExit(f"{name} is not a function of the file")


def prompt_at(repo, rev, spec, before=False, key="name"):
    """The prompt (a string) or, with key="schema_name", the answer schema of the code version."""
    path = (spec.get("before_file") if before else None) or spec["file"]
    text = source_at(repo, rev, path)
    name = (spec.get("before_" + key) if before else None) or spec.get(key)
    return constant(text, name) if name else text


def optional(repo, rev, spec, before=False):
    """An addendum constant; with `"optional": true`, None when the version has no such file or constant (a rule that moved
    to the system prompt: the "before" version has it as a tail instead). `suffix` — the case's own text the core puts right
    after the constant (the roster's lines after ROSTER), the same for both versions."""
    try:
        return prompt_at(repo, rev, spec, before) + spec.get("suffix", "")
    except SystemExit:
        if spec.get("optional"):
            return None
        raise


def runs_of(cmd, other):
    """The bodies of the scripts `other` writes by a heredoc (`cat > path <<'X'`, `cat <<X > path`, `tee path`) that the
    command cmd runs, else "". Codex often keeps a monitor in a script its first command writes; the rest of that
    command (a one-off notify) is not the monitor, nor a shell variable it sets — gone when the schedule runs."""
    out, lines, i = [], other.split("\n"), 0
    while i < len(lines):
        line = lines[i]
        tag = re.search(r"""<<-?\s*['"]?([\w.-]+)['"]?""", line)
        path = re.search(r"""(?:\bcat\b[^\n|;]*?>>?|\btee\s+(?:-a\s+)?)\s*['"]?([~\w./-]+)""", line)
        if not (tag and path):
            i += 1
            continue
        end = next((j for j in range(i + 1, len(lines)) if lines[j].strip() == tag.group(1)), len(lines))
        if re.search(r"(?<![\w./-])" + re.escape(path.group(1)) + r"(?![\w./-])", cmd):
            out.append("\n".join(lines[i + 1:end]))
        i = end + 1
    return "\n".join(out)


HELP_WORDS = ("-h", "--help")
# with hub_cli only these really run (on the eval's own state folder: they put facts into its mailbox or read it);
# everything else — a browser, a model, the network, a server, the hub's processes — is recorded, not run
RUN = ("schedule", "session", "send", "task", "send-file", "wait", "waiting", "gist", "decided", "notify", "status",
       "show", "tools", "question", "route", "close-ready", "slash", "away", "back", "answer", "allow", "deny", "post",
       "feed", "projects", "version", "layout", "help", "contacts", "profile")


def is_help(argv):
    return not argv or argv[0] == "help" or any(a in HELP_WORDS for a in argv)


def check(c, answer):
    """→ (passed, note). answer: {"text": the model's text, "fields": its JSON or {}, "calls": argv of `hub` calls}."""
    fields = answer.get("fields") or {}
    calls = answer.get("calls") or []
    if c["type"] == "call":
        first = next((shlex.join(a) for a in calls if not is_help(a)), None)
        if first is None:
            return False, "no hub call"
        ok = ("re" not in c or bool(re.search(c["re"], first, re.S))) and \
            ("not_re" not in c or not re.search(c["not_re"], first, re.S))
        return ok, first[:160]
    if c["type"] == "any_call":   # some call that is not help matches: the model may look at the state before it acts
        hit = next((shlex.join(a) for a in calls if not is_help(a) and re.search(c["re"], shlex.join(a), re.S)), None)
        return hit is not None, (hit or "no such call")[:160]
    if c["type"] == "no_call":
        hit = next((shlex.join(a) for a in calls if re.search(c["re"], shlex.join(a), re.S)), None)
        return hit is None, (hit or "none such")[:160]
    if c["type"] in ("tool", "no_tool"):   # the model's own tool calls (case: "trace"), not `hub` ones
        def text(t):
            i = t.get("input") or {}
            return str(i.get("command") or i.get("file_path") or i.get("notebook_path") or json.dumps(i, ensure_ascii=False))
        if "tools" not in answer:   # no trace: "none such" would pass for a guarantee nobody checked
            return False, 'no trace (the case needs "trace": true, Claude only)'
        hits = [f"{t['name']}: {text(t)}" for t in answer["tools"]
                if re.fullmatch(c["name"], str(t["name"])) and re.search(c.get("re", ""), text(t), re.S)
                and not ("except_re" in c and re.search(c["except_re"], text(t), re.S))]
        return (bool(hits) == (c["type"] == "tool")), (hits[0] if hits else "none such")[:160]
    if c["type"] == "not_seen":   # what the tools gave the model back (case: "trace"): a value must not be there
        if "seen" not in answer:
            return False, 'no trace (the case needs "trace": true, Claude only)'
        hit = next((m.group(0) for s in answer["seen"] for m in [re.search(c["re"], s, re.S)] if m), None)
        return hit is None, (hit or "none such")[:160]
    if c["type"] == "changed":   # the files the model changed or created in its folder, against the case's seed
        paths = answer.get("changed") or []
        bad = [p for p in paths if "only_re" in c and not re.search(c["only_re"], p)]
        some = "has_re" not in c or any(re.search(c["has_re"], p) for p in paths)
        return (not bad and some), (f"outside: {bad[0]}" if bad else ", ".join(paths) or "nothing changed")[:160]
    if c["type"] == "file":
        body = (answer.get("files") or {}).get(c["path"])
        if body is None:
            return (c.get("exists") is False), "no such file"
        ok = c.get("exists", True) and len(body) >= c.get("min_len", 0) \
            and ("re" not in c or bool(re.search(c["re"], body, re.I | re.S))) \
            and ("not_re" not in c or not re.search(c["not_re"], body, re.I | re.S))
        return ok, f"{c['path']}: {len(body)} chars"
    if c["type"] == "help_calls":
        n = sum(is_help(a) for a in calls)
        return n <= c["max"], f"help called {n} times"
    if c["type"] == "field":
        if c["name"] not in fields:
            return False, f"no field {c['name']}"
        v = fields[c["name"]]
        # a list (commands): each item on its own — a command is judged from its start, not by words inside its argument
        items = [str(x) for x in v] if isinstance(v, list) else [str(v)]
        if c.get("with"):   # the field plus what it runs of the other one (a script or a variable that one defines)
            ran = runs_of(str(v), str(fields.get(c['with']) or ''))
            v = f"{v}\n{ran}"
            items += [ran] if ran else []
        ok = ("equals" not in c or v == c["equals"]) and ("min" not in c or v >= c["min"]) \
            and ("max" not in c or v <= c["max"]) and ("re" not in c or bool(re.search(c["re"], str(v), re.I | re.S))) \
            and ("not_re" not in c or not re.search(c["not_re"], str(v), re.I | re.S)) \
            and ("max_words" not in c or len(str(v).split()) <= c["max_words"])
        hit = next((x for x in items if "item_not_re" in c and re.search(c["item_not_re"], x, re.I | re.S)), None)
        ok = ok and hit is None and ("item_re" not in c or any(re.search(c["item_re"], x, re.I | re.S) for x in items))
        return ok, f"{c['name']}={(hit or str(v))[:80]}"
    hay = "\n".join([answer.get("text") or "", *[v for v in fields.values() if isinstance(v, str)]])
    if c["type"] in ("said", "not_said"):
        m = re.search(c["re"], hay, re.I | re.S)
        return (bool(m) == (c["type"] == "said")), (m.group(0)[:80] if m else "no match")
    if c["type"] == "any_of":
        got = [check(x, answer) for x in c["checks"]]
        return any(ok for ok, _ in got), " | ".join(n for _, n in got)
    raise SystemExit(f"unknown check {c['type']}")


def claude_binary():
    return os.environ.get("EVAL_CLAUDE") or shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")


def tool_calls(events):
    """[{"name", "input"}] — every tool call of a stream-json run, in order."""
    return [{"name": b.get("name"), "input": b.get("input") or {}} for e in events if e.get("type") == "assistant"
            for b in (e.get("message") or {}).get("content") or [] if isinstance(b, dict) and b.get("type") == "tool_use"]


def tool_results(events):
    """The text of every tool result of a stream-json run: what the model saw (a value it read lands here)."""
    out = []
    for e in events:
        if e.get("type") != "user":
            continue
        for b in (e.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                body = b.get("content")
                out.append(body if isinstance(body, str) else json.dumps(body, ensure_ascii=False))
    return out


def ask(system, schema, user, model, tools, cwd, env, effort=None, trace=False):
    cmd = [claude_binary(), "-p", "--model", model, "--tools", tools, "--setting-sources", "", "--strict-mcp-config",
           "--no-session-persistence", "--output-format", "stream-json" if trace else "json",
           *(["--verbose"] if trace else []), "--system-prompt", system]
    if tools:
        cmd += ["--allowedTools", tools]
    if schema:
        cmd += ["--json-schema", json.dumps(schema)]
    if effort:   # as for hub sessions (case: "effort"), otherwise with JSON — like the watcher
        cmd += ["--effort", effort]
    elif schema:
        cmd += QUICK_ARGS
    r = subprocess.run(cmd, input=user, capture_output=True, text=True, timeout=300, cwd=cwd, env={**os.environ, **env})
    events = []
    try:
        if trace:   # one JSON object a line: the turn's events, the last of them is the result
            for line in r.stdout.splitlines():
                try:
                    events.append(json.loads(line))
                except ValueError:
                    pass
            out = next((e for e in reversed(events) if e.get("type") == "result"), None)
            if out is None:
                raise ValueError("no result")
        else:
            out = json.loads(r.stdout)
    except ValueError:
        raise RuntimeError(f"claude exited with code {r.returncode}: {(r.stdout + r.stderr)[:200]}") from None
    text = str(out.get("result") or "")
    if LOGGED_OUT.search(text):
        raise NotLoggedIn(text[:100])
    if out.get("is_error"):
        raise RuntimeError(text[:200])
    fields = out.get("structured_output") if isinstance(out.get("structured_output"), dict) else {}
    return {"text": text, "fields": fields, "cost": out.get("total_cost_usd") or 0, **({"tools": tool_calls(events), "seen": tool_results(events)} if trace else {})}


CODEX_NOTE = {"file": "hosts/codex.py", "name": "NOTE", "optional": True}   # what a Codex session gets on top of the rules


def run_config():
    """config.toml of a run's own Codex folder — what a hub session's Codex has (hosts/codex_set: CONFIG with the
    isolation pins, skill-installer and the user's own skills off), except the web search and the login shell. The
    shell stays: without it Codex answers "no shell for `hub send`" instead of naming its commands (first run
    2026-11-05: 16 of 207 answers)."""
    import tomllib
    from hosts import codex_set
    config = tomllib.loads(codex_set.CONFIG)
    # the stub `hub` of the thread's PATH holds only without the login shell and its snapshot: they source the
    # user's profile, which puts the machine's own `hub` first again (probe 2026-11-05, codex-cli 0.162.1)
    features = {**config.pop("features", {}), **codex_set.PINNED, "shell_snapshot": False}
    config.update(web_search="disabled", allow_login_shell=False)
    skills = [e for e in codex_set.thread_config({}, {"HOME": os.path.expanduser("~")})["skills"]["config"]
              if all(_utf8(v) for v in e.values())]   # a path that is not UTF-8 cannot be written into the file
    lines = [f"{k} = {_toml(v)}" for k, v in config.items()] + ["", "[features]"]
    lines += [f"{k} = {_toml(v)}" for k, v in features.items()]
    for entry in skills:
        lines += ["", "[[skills.config]]", *(f"{k} = {_toml(v)}" for k, v in entry.items())]
    return "\n".join(lines) + "\n"


def _utf8(v):
    try:
        str(v).encode("utf-8")
        return True
    except UnicodeEncodeError:   # a surrogate-escaped byte of a file name
        return False


def _toml(v):
    """A TOML value: a boolean, a list of them or strings, a basic string (\\, " and control characters escaped)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, list):
        return "[" + ", ".join(_toml(x) for x in v) + "]"
    if isinstance(v, (int, float)):
        return str(v)
    out = "".join("\\\\" if c == "\\" else '\\"' if c == '"' else f"\\u{ord(c):04x}"
                  if ord(c) < 0x20 or ord(c) == 0x7f else c for c in str(v))
    return f'"{out}"'


# `hub` in the shell of an eval's thread: the model may run `hub send`/`hub waiting` as a session does; it goes nowhere
STUB_HUB = "#!/bin/sh\necho ok\n"
STUB_HUB_CMD = "@echo ok\r\n"   # Windows: a shell there finds hub.cmd (PATHEXT), the file above it does not run
# what of the machine's environment the thread gets: what Codex and a shell need (a proxy to reach the model), no keys
KEEP_ENV = ("USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TERM", "SHELL", "TZ", "SSL_CERT_FILE",
            "SSL_CERT_DIR", "http_proxy", "https_proxy", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
            # Windows: without these a process there cannot start its shell or open a socket (no keys in them; its
            # os.environ ignores the case of a name: one spelling each)
            "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "USERNAME")


@contextlib.contextmanager
def codex_setup(a):
    """--host codex, for the whole run: the codex command, its environment, the sessions' and the core's model; the
    limits before and after. The sign-in — the evals' own (<state>/codex-evals-home) or the given --codex-home, never
    the owner's own ~/.codex nor the hub's: under its lock a copy goes into a folder of the run with run_config() and
    comes back after it. The environment is a small one (KEEP_ENV), with a HOME of the run's own, `hub` in its PATH a
    stub and an empty hub for the machine's own `hub`. A missing piece — a refusal with what to give."""
    sys.path.append(REPO)
    from core import codexmodel
    import codexrun
    hub = on_codex()
    picks = codexmodel.picks(cfg=None if hub else {})
    model = a.codex_model or picks["session"][0]
    if not model:
        raise SystemExit("--codex-model <the model of Codex sessions>: hub.json has no codex.model")
    state_dir = codexmodel.state_dir_of(None)
    given = os.path.realpath(a.codex_home) if a.codex_home else None
    if given == os.path.realpath(os.path.join(os.path.expanduser("~"), ".codex")):
        raise SystemExit(f"{a.codex_home} is the owner's own Codex folder: evals run only with a CODEX_HOME of their own")
    from core import codexcli
    pool = os.path.realpath(os.path.join(state_dir, "moddata")) + os.sep
    if given and (given == os.path.realpath(codexcli.home_of(state_dir)) or given.startswith(pool)):   # the pool's too
        raise SystemExit(f"{a.codex_home} is the hub's sign-in: its token is one-time, parallel runs would knock the hub "
                         "out of it; evals use a sign-in of their own (evals/run.py --codex-login)")
    auth = codexrun.Auth(state_dir)
    if given:   # another sign-in folder of the evals: the same copy, lock and return
        auth.home, auth.src = a.codex_home, os.path.join(a.codex_home, "auth.json")
    with auth, contextlib.ExitStack() as stack:
        run = tempfile.mkdtemp(prefix="hubpeval-")
        stack.callback(shutil.rmtree, run, True)
        stack.callback(auth.take, run)
        auth.give(run)
        home = codexrun.run_home(run)
        with open(os.path.join(home, "config.toml"), "w", encoding="utf-8") as f:
            f.write(run_config())
        stub, nohub, own_home = (os.path.join(run, d) for d in ("bin", "no-hub", "home"))
        for d in (stub, nohub, own_home):
            os.makedirs(d)
        with open(os.path.join(stub, "hub"), "w") as f:
            f.write(STUB_HUB)
        os.chmod(os.path.join(stub, "hub"), 0o755)
        with open(os.path.join(stub, "hub.cmd"), "w") as f:
            f.write(STUB_HUB_CMD)
        path = os.environ.get("PATH")
        env = {**{k: os.environ[k] for k in KEEP_ENV if k in os.environ}, "CODEX_HOME": home, "HOME": own_home,
               "USERPROFILE": own_home,   # the home on Windows
               "PATH": stub + (os.pathsep + path if path else ""),   # an empty entry would be the current folder
               "HUB_STATE_DIR": nohub, "HUB_CONFIG": os.path.join(nohub, "hub.json"), "HUB_CONFIG_DIR": nohub}
        codex = {"cmd": [codexmodel.binary()], "env": env, "model": model,
                 # a hub on Codex runs every session at its own effort (hosts/codex.CodexHost.pick), not the case's
                 "effort": a.codex_effort or picks["session"][1], "core": picks["core"]}
        print(f"Codex quota before: {limits(state_dir, home)}", flush=True)
        yield codex
        print(f"Codex quota after: {limits(state_dir, home)}", flush=True)


def limits(state_dir, home):
    """The sign-in's limits now (account/rateLimits/read, no model) — a line, or why they did not read."""
    from core import codexcli
    try:
        rpc = codexcli.Rpc(state_dir, home)
        try:
            return codexcli.windows_line(rpc.call("account/rateLimits/read", {})) or "—"
        finally:
            rpc.close()
    except (OSError, RuntimeError) as e:
        return f"not read ({str(e)[:100]})"


def inject(items):
    """A turn's `items` ([{role, text}]) -> the thread's history items (thread/inject_items)."""
    return [{"type": "message", "role": i["role"], "content": [{"type": "input_text", "text": i["text"]}]}
            for i in items]


def ask_codex(codex, case, system, schema, user, cwd, items=None):
    """One turn on Codex: a case with effort — a session's turn (developerInstructions, the sessions' model), without —
    the core's model (baseInstructions, codex.watch_model and watch_effort, as codexmodel.ask). The case's model and env
    and tools are Claude's — not used: the thread is read-only in the case's folder, with the shell (`hub` a stub).
    items — history injected before the turn. No sign-in or a limit — NotLoggedIn (n/a)."""
    from core import codexmodel
    session = bool(case.get("effort"))
    model, effort = (codex["model"], codex["effort"] or case["effort"]) if session else codex["core"]
    try:
        got, spent = codexmodel.run(codex["cmd"], user, system, schema, model, effort, 300, codex["env"], cwd=cwd,
                                    developer=session, items=inject(items) if items else None)
    except codexmodel.Error as e:
        if codexmodel.down(str(e)):
            raise NotLoggedIn(str(e)[:100]) from None
        raise RuntimeError(str(e)[:200]) from None
    return {"text": got if schema is None else "", "fields": got if schema is not None else {}, "cost": 0,
            "tokens": list(spent)}


WRAPPER = r'''import json, os, subprocess, sys, time
argv = sys.argv[1:]
with open(os.path.join(os.environ["HUB_EVAL_CALLS"], "%d.json" % time.time_ns()), "w") as f:
    json.dump(argv, f)
words = [a for a in argv if not a.startswith("-")]
asks = argv[-1:] in (["-h"], ["--help"]) or any(a in ("-h", "--help") for a in argv[:2])   # what bin/hub takes as help
if words and words[0] not in RUN and (words[0] == "direction" or not asks):
    print("(eval: the command is recorded, not run)")
    sys.exit(0)
env = {k: v for k, v in os.environ.items() if not k.startswith("HUB_")}
state = os.environ["HUB_EVAL_STATE"]   # the state, hub.json and the settings folder (contacts, accounts) — all its own
env.update(HUB_STATE_DIR=state, HUB_CONFIG=os.path.join(state, "hub.json"), HUB_CONFIG_DIR=os.path.join(state, "config"),
           HUB_CLI_WAIT="0", HUB_SESSION=SESSION, PYTHONUTF8="1")
sys.exit(subprocess.run([sys.executable, os.path.join(TREE, "bin", "hub"), *argv], env=env).returncode)
'''


def cli_tree(repo, rev, into):
    """The code of a version for hub_cli: `git archive` into a folder of its own (once per sha)."""
    if os.path.isdir(into):
        return into
    os.makedirs(into)
    data = subprocess.run(["git", "-C", repo, "archive", "--format=tar", rev], capture_output=True, check=True).stdout
    import io
    import tarfile
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        t.extractall(into, filter="data")
    return into


EXECUTOR = r"""
import json, os, sys
spec = json.load(sys.stdin)
home = os.environ["HOME"]
folder = os.path.join(home, "agents", spec["agent"])
os.makedirs(folder)
for name, text in (spec.get("files") or {}).items():
    with open(os.path.join(folder, name), "w", encoding="utf-8", newline="") as f:
        f.write(text)
sys.path.insert(0, os.getcwd())
from hosts.streamjson import StreamHost
host = StreamHost(os.environ["HUB_STATE_DIR"], claude=("echo",))
host.fresh_prompt = False
# `executor: false` — the agent itself (a mini-hub boss), not its executor; `command` — more of host.command's own
# arguments (bus, minihub, spec, spec_boss…): a version that does not take one has the prompt without it
import inspect
# `home` (a file of the version, hub-home/CLAUDE.md) — a session without an agent file whose folder carries that CLAUDE.md
# (the hub itself): the file, then the addendum the core gives such a session
want = {**({} if spec.get("home") else {"agent_file": os.path.join(folder, "CLAUDE.md")}),
        **({"executor_of": spec["agent"]} if spec.get("executor", True) and not spec.get("home") else {}),
        **(spec.get("command") or {})}
known = inspect.signature(host.command).parameters
cmd = host.command("w", "s-1", **{k: v for k, v in want.items() if k in known})
with open(cmd[cmd.index("--append-system-prompt-file") + 1], encoding="utf-8") as f:
    text = f.read()
if spec.get("home"):
    with open(spec["home"], encoding="utf-8") as f:
        text = f.read().rstrip("\n") + "\n\n" + text
sys.stdout.write(json.dumps({"text": text, "profile": host.profile_used.get("w")}))
"""


def executor_prompt(tree, spec):
    """What the version's core gives an agent's executor on top of Claude Code's own prompt (hosts/streamjson.command):
    the agent's files from the case (`executor`: {agent, files: {CLAUDE.md, CLAUDE.executor.md, …}}) in a folder of
    their own, the version's code from `git archive`, a home and state of their own — nothing of the machine's hub."""
    home = tempfile.mkdtemp(prefix="hubeval-exec-")
    try:
        with open(os.path.join(home, "hub.json"), "w") as f:
            f.write("{}\n")
        os.makedirs(os.path.join(home, "state"))
        env = {k: v for k, v in os.environ.items() if not k.startswith("HUB_")}
        env.update(HOME=home, HUB_CONFIG=os.path.join(home, "hub.json"), HUB_STATE_DIR=os.path.join(home, "state"),
                   HUB_CONFIG_DIR=home, HUB_LANG="en")
        r = subprocess.run([sys.executable, "-c", EXECUTOR], input=json.dumps(spec), capture_output=True, text=True,
                           cwd=tree, env=env, timeout=60)
        if r.returncode:
            raise SystemExit(f"the executor's prompt of {tree}: {(r.stderr or r.stdout)[-400:]}")
        got = json.loads(r.stdout)
        if (spec.get("command") or {}).get("spec"):   # the spec's path as the run's folder sees it, not this throwaway home
            got["text"] = got["text"].replace(os.path.join(home, "state") + os.sep, "")
    finally:
        shutil.rmtree(home, True)
    print(f"executor prompt of {os.path.basename(tree)}: {got['profile'] and got['profile'].get('kind')}, "
          f"{len(got['text'].encode())} bytes", file=sys.stderr)
    return got["text"]


def hub_cli(cwd, tree, session):
    """A `hub` in the model's PATH: the version's bin/hub on a state folder of its own, every call recorded."""
    calls, state, bins = (os.path.join(cwd, ".hub-eval", d) for d in ("calls", "state", "bin"))
    for d in (calls, state, bins):
        os.makedirs(d, exist_ok=True)   # a case may seed the state (files: .hub-eval/state/config/contacts.json)
    with open(os.path.join(state, "hub.json"), "w") as f:
        f.write("{}\n")
    script = os.path.join(bins, "hub")
    with open(script, "w", encoding="utf-8") as f:
        f.write(f"#!{sys.executable}\nTREE = {tree!r}\nSESSION = {session!r}\nRUN = {RUN!r}\n" + WRAPPER)
    os.chmod(script, 0o755)
    env = {"PATH": bins + os.pathsep + os.environ.get("PATH", ""), "HUB_EVAL_CALLS": calls, "HUB_EVAL_STATE": state}
    return env, calls


def read_calls(folder):
    out = []
    for name in sorted(os.listdir(folder), key=lambda n: int(n.split(".")[0])):
        with open(os.path.join(folder, name), encoding="utf-8") as f:
            out.append(json.load(f))
    return out


OWN = (".hub-eval", "__pycache__", ".git", ".pytest_cache")   # what a run leaves of its own: not the model's doing


def own(path):
    return any(part in OWN for part in path.split("/"))


def snapshot(cwd):
    """{path: text} of the files of a folder, without what a run leaves of its own (the hub's stub, caches, git)."""
    out = {}
    for root, dirs, names in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in OWN]
        for n in names:
            path = os.path.join(root, n)
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    out[os.path.relpath(path, cwd).replace(os.sep, "/")] = f.read()
            except OSError:
                pass
    return out


def one(job):
    case, turn, variant, system, schema, user, n, model, codex, items, tree = job
    cwd = tempfile.mkdtemp(prefix="hubeval-")
    try:
        seed = {**(case.get("files") or {}), **(turn.get("files") or {})}
        for rel, body in seed.items():
            os.makedirs(os.path.dirname(os.path.join(cwd, rel)), exist_ok=True)
            with open(os.path.join(cwd, rel), "w", encoding="utf-8") as f:
                f.write(body)
        env = QUICK if schema and not case.get("effort") else {}
        if codex:   # a hub_cli case is Claude's only (main leaves it out on Codex)
            answer = ask_codex(codex, case, system, schema, user, cwd, items)
        else:
            cli, calls = hub_cli(cwd, tree, case["hub_cli"].get("session", "maps")) if tree else ({}, None)
            answer = ask(system, schema, user, case.get("model", model), case.get("tools", ""), cwd,
                         {**env, **(case.get("env") or {}), **cli}, case.get("effort"), case.get("trace", False))
            if calls:
                answer["calls"] = read_calls(calls)
            if case.get("trace"):   # what the model left in its folder: changed or new files, against the seed
                now, was = snapshot(cwd), {p: b for p, b in seed.items() if not own(p)}
                answer["changed"] = sorted(p for p in {*now, *was} if now.get(p) != was.get(p))
                answer["files"] = {p: b[:20000] for p, b in now.items()}   # the folder as it is: a deleted file is not there
                for t in answer.get("tools") or []:   # the model's paths are absolute: as the folder sees them
                    for k in ("file_path", "notebook_path"):
                        if isinstance(t["input"].get(k), str):
                            for root in (cwd, os.path.realpath(cwd)):   # a symlinked temp folder (macOS) shows as its target
                                t["input"][k] = t["input"][k].removeprefix(root + os.sep)
    except NotLoggedIn as e:
        return job, None, [("n/a", False, str(e))], {}
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as e:
        return job, False, [("error", False, str(e)[:160])], {}
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
    results = [(c.get("id") or c["type"], *check(c, answer)) for c in turn["checks"]]
    return job, all(ok for _, ok, _ in results), results, answer


def main():
    guard_codex()   # no codex of the run without a CODEX_HOME of its own (tools/codex_guard.py)
    ap = argparse.ArgumentParser()
    ap.add_argument("cases", nargs="+")
    ap.add_argument("--before")
    ap.add_argument("--after", default="HEAD")
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--trials", type=int, default=2)
    ap.add_argument("--only", choices=("before", "after"))
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--host", choices=("claude", "codex"), help="by default — the hub's (hub.json host)")
    ap.add_argument("--codex-home", help="--host codex: a Codex folder with a sign-in, copied into the run (by default "
                                         "— the evals' sign-in, <state>/codex-evals-home)")
    ap.add_argument("--codex-model", help="--host codex: the sessions' model (hub.json codex.model by default)")
    ap.add_argument("--codex-effort", help="--host codex: the sessions' effort (hub.json codex.effort, else the case's)")
    a = ap.parse_args()
    cases = []
    for path in (find_case(c) or ap.error(f"no such case: {c}") for c in a.cases):   # a path, or a name in any case folder
        with open(path, encoding="utf-8") as f:
            cases.append(json.load(f))
    on = (a.host or ("codex" if on_codex() else "claude")) == "codex"
    if not on and any(t.get("items") for c in cases for t in c["turns"]):
        raise SystemExit("a turn with items is a Codex-only case (--host codex): what the Codex host puts into the thread")
    if on and any(c.get("hub_cli") for c in cases):   # its `hub` records the calls; on Codex `hub` is a stub
        print("without the hub_cli cases (--host claude only): " + ", ".join(c["id"] for c in cases if c.get("hub_cli")))
        cases = [c for c in cases if not c.get("hub_cli")]
        if not cases:
            raise SystemExit("nothing to run on Codex: every case is a hub_cli one (--host claude)")
    with codex_setup(a) if on else contextlib.nullcontext() as codex:
        run_cases(a, cases, codex)


def by_version(value, before):
    """A case value that may differ per version: {"before": …, "after": …} — the one of this version, else as is."""
    return value["before" if before else "after"] if isinstance(value, dict) and set(value) == {"before", "after"} else value


def with_consts(text, turn, repo, rev, was):
    """The core's own words inside a turn's input, of the version under test: `consts` {KEY: {file, name, format?}} — each
    ⟦KEY⟧ in the input becomes that constant of the version (formatted with `format`, which may differ per version).
    A message the core writes into a turn (a fold constant, a hook's hint) changes with the version — the case keeps the
    scenario, the version its words."""
    for key, spec in (turn.get("consts") or {}).items():
        mark = "⟦%s⟧" % key
        if mark not in text:
            if spec.get("optional"):   # the other version's input is its own words
                continue
            raise SystemExit(f"{turn['id']}: the input has no {mark}")
        value = optional(repo, rev, spec, was)
        value = "" if value is None else value   # a constant the version does not have yet: the line is not there
        fmt = by_version(spec.get("format"), was)
        text = text.replace(mark, value.format(**fmt) if fmt and value else value)
    if "⟦" in text:
        raise SystemExit(f"{turn['id']}: {text[text.index('⟦'):][:40]} is not in its consts")
    return text


def lang_words(repo, rev):
    """A language in a case's `lang` (a code, or a word some version of the core used) → the word the core of this version
    puts into the rule for sessions (core/forms.py LANG_CODES → LANG_WORDS); a version without them — as is."""
    try:
        space = constants(source_at(repo, rev, "core/forms.py"))
    except SystemExit:
        return {}
    codes, words = space.get("LANG_CODES") or {}, space.get("LANG_WORDS") or {}
    codes = {**{"Russian": "ru", "English": "en"}, **codes}   # a word a later core uses — an older one has its own
    return {w: words[c] for w, c in codes.items() if c in words}


def run_cases(a, cases, codex):
    sha = lambda rev: subprocess.run(["git", "-C", a.repo, "rev-parse", "--short", rev], capture_output=True,  # noqa: E731
                                     text=True, check=True).stdout.strip()
    jobs, rows = [], {}
    trees = tempfile.mkdtemp(prefix="hubeval-trees-")   # hub_cli: the code of each version
    atexit.register(shutil.rmtree, trees, True)   # also after a failure or Ctrl-C
    for case in cases:
        spec = case["prompt"]
        for variant, rev in (("before", a.before or case.get("before", "main")), ("after", a.after)):
            if a.only and a.only != variant:
                continue
            was = variant == "before"
            if rev is None:   # a prompt new in this version — has no "before"
                continue
            tree = cli_tree(a.repo, rev, os.path.join(trees, sha(rev))) if case.get("hub_cli") else None
            if tree:   # what differs between the versions is the hub command itself; the prompt is the case's
                text = by_version(case["system"], was)
            else:
                text = prompt_at(a.repo, rev, spec, was)
            task = function_at(text, spec["call"]) if spec.get("call") else None   # the prompt is what the function builds
            schema = spec.get("schema") or (prompt_at(a.repo, rev, spec, was, "schema_name") if spec.get("schema_name") else None)
            notes = [n for n in (optional(a.repo, rev, part, was) for part in spec.get("append") or []) if n is not None]
            tail = prompt_at(a.repo, rev, spec["tail"], was) if spec.get("tail") else ""
            # a session on Codex gets the Codex addendum after the core's rules (hosts/codex.CodexHost.instructions)
            codex_note = optional(a.repo, rev, CODEX_NOTE) if codex and case.get("effort") else None
            words = lang_words(a.repo, rev)
            for turn in case["turns"]:
                ex = turn.get("executor") or case.get("executor")
                fmt = {**(spec.get("format") or {}), **(turn.get("format") or {})}
                # a value may differ per version ({"before": …, "after": …}): the word the core of that version passes
                fmt = {k: by_version(v, was) for k, v in fmt.items()}
                if "lang" in fmt:   # the owner's language: the word the core of that version puts into the rule
                    fmt["lang"] = words.get(fmt["lang"], fmt["lang"])
                if ex:   # what the version's core gives the agent's executor, as is (the profile's own braces stay), and the notes
                    system = "\n\n".join([executor_prompt(cli_tree(a.repo, rev, os.path.join(trees, sha(rev))), ex),
                                            *(n.format(**fmt) for n in notes)])
                elif notes:
                    system = "\n\n".join([text, *(n.format(**fmt) for n in notes)])
                else:
                    system = text.format(**fmt) if fmt else text
                if task:   # the built task goes out as the message, the system prompt is the case's own
                    system, turn = by_version(case.get("system", ""), was), {**turn, "input": task(*by_version(turn["args"], was))}
                if codex_note:
                    system = system.rstrip() + "\n\n" + codex_note
                user = turn["input"]["before" if was else "after"] if isinstance(turn["input"], dict) else turn["input"]
                user = with_consts(user, turn, a.repo, rev, was)
                if tail:   # what the core appends to the owner's word (a constant of that version)
                    user += "\n\n" + tail
                items = turn.get("items")
                items = items["before" if was else "after"] if isinstance(items, dict) else items
                rows[(f"{case['id']}/{turn['id']}", variant)] = {"sha": sha(rev), "pass": 0, "na": 0, "fail": set()}
                jobs += [(case, turn, variant, system, schema, user, n, a.model, codex, items, tree)
                         for n in range(1, a.trials + 1)]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    os.makedirs(os.path.join(WORK, "prompt-runs"), exist_ok=True)
    log = os.path.join(WORK, "prompt-runs", f"{stamp}.jsonl")
    with ThreadPoolExecutor(a.jobs) as pool, open(log, "w", encoding="utf-8") as out:
        for job, ok, results, answer in pool.map(one, jobs):
            case, turn, variant = job[0], job[1], job[2]
            row = rows[(f"{case['id']}/{turn['id']}", variant)]
            if ok is None:
                row["na"] += 1
            elif ok:
                row["pass"] += 1
            else:
                row["fail"] |= {cid for cid, cok, _ in results if not cok}
            out.write(json.dumps({"case": case["id"], "turn": turn["id"], "variant": variant, "trial": job[6], "ok": ok,
                                  "checks": [[cid, cok, note] for cid, cok, note in results], "answer": answer},
                                 ensure_ascii=False) + "\n")
    print("| Case/turn | Variant | Passed | Failures |\n|---|---|---|---|")
    for (name, variant), r in rows.items():
        total = a.trials - r["na"]
        print(f"| {name} | {variant} `{r['sha']}` | {r['pass']}/{total}" + (f" (+{r['na']} n/a)" if r["na"] else "")
              + f" | {', '.join(sorted(r['fail'])) or '—'} |")
    with open(log, encoding="utf-8") as f:
        answers = [json.loads(line).get("answer") or {} for line in f]
    if codex:   # Codex spends a plan's tokens, there is no price
        print(f"\nCodex tokens: input {sum((x.get('tokens') or [0, 0])[0] for x in answers)}, "
              f"output {sum((x.get('tokens') or [0, 0])[1] for x in answers)}\nanswers: {log}")
    else:
        print(f"\nprice: ${sum(x.get('cost') or 0 for x in answers):.2f}\nanswers: {log}")


if __name__ == "__main__":
    main()
