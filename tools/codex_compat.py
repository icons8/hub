#!/usr/bin/env python3
"""Codex (experimental): compatibility of the core's Codex host with Codex CLI versions, like tools/compat.py for
Claude Code — without a model: the model provider is a fake Responses API inside this check, nothing goes to OpenAI.

    tools/codex_compat.py [--versions 0.155.1,0.166.0] [--codex <binary>]… [--keep] [--json] [--only pool]

Versions are installed with `npm i @openai/codex@<v>` into a temporary folder (the machine's codex is not touched); the
machine's codex (hosts/codex.codex_bin) is always in the list. For every version:
- schema: `app-server generate-json-schema` has what the host's code (hosts/codex*.py, core/codex*.py — read by
  host_calls, not listed by hand) sends and handles: every method it names, every literal parameter of its requests and
  the constant values it gives them (enums), its answers to Codex's requests (ANSWERS), the fields it reads (FIELDS);
  what is only in the experimental API (the host asks for it in initialize) is looked for in the `--experimental` schema;
- hooks: a thread's hooks given in its config get the key and trusted_hash the core computes (hosts/codex_set.hook_trust)
  — otherwise Codex skips them without a word;
- features: every feature the host pins (hosts/codex_set.PINNED, CORE_MODEL_OFF, hosts/codex.FEATURES) is known — an unknown one `-c` ignores silently,
  and the isolation would be gone without a word;
- a live hub: the core (daemon.Hub) with CodexHost in a temporary state folder raises a session in a folder whose
  .codex/config.toml turns hooks off and memories, multi_agent on, with a memory summary in the hub's Codex folder and a
  skill in the user's $HOME/.agents/skills, an MCP server from the project's .mcp.json; then three turns through the
  fake API: a plain answer (turn.result reaches the core), `pkill` (the core's hook must ban it) and a call of the MCP
  server (it must answer with HUB_STATE_DIR, which only the session's environment has). What reached the "model" is
  checked: the hub's note in the instructions, the MCP tools, the session's subagents, no memory, no user skill. No
  subagents where the core keeps them off: the bus's liaison (also no MCP, no web search) and the core's model, in the
  temp folder and in a trusted folder whose .codex/config.toml turns them on. The platform's parts: a turn interrupted
  while a command it left running goes on — the host terminates that background terminal; a session in a read-only
  mode (dontAsk) runs a command but cannot write. The form of apply_patch's input to a repo's hooks (tool_name
  apply_patch, the patch in tool_input.command — what hooks/edit_hooks.py reads to give an Edit|Write hook Claude's
  calls): a version that changes it is red here before every patch in such a repo is refused; and the repo's guard
  refusing a file refuses the patch. A failure of the core's hook (the session's rules file broken) refuses a command;
- the Codex account pool (core/codexslot.py): a thread goes hub folder → an account folder the core prepared (links,
  -c sqlite_home) → hub folder with its whole history, the account folder gets no databases of its own, a second
  writer of the thread is refused.
Every Codex process runs with CODEX_HOME and HOME inside the run folder: the personal ~/.codex is not read or written.

Exit code: 0 — all versions ok, 3 — an incompatible version, 2 — the check itself failed.
"""
import argparse
import ast
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# What the host sends and handles — its requests with their parameters, the notifications and server requests it
# reads — is taken from its code (host_calls), not listed here: a slice that adds a call is checked without touching
# this tool. What follows is what the code alone does not show: the fields it reads in answers, the parameters it
# builds in steps (thread/start of a session goes through hosts/codex_rules and hosts/codex_liaison).
HOST_FILES = (("hosts", "codex*.py"), ("core", "codex*.py"))
METHOD = re.compile(r"^[a-z][A-Za-z]*(?:/[A-Za-z_]+)+$")
OWN = ("hub/",)   # the host's posts to itself (CodexHost.post), not Codex's
FIELDS = {"ThreadStartParams": ("cwd", "approvalPolicy", "sandbox", "developerInstructions", "model", "config",
                                "ephemeral", "baseInstructions", "sessionStartSource"),
          "ThreadResumeParams": ("threadId", "cwd", "approvalPolicy", "sandbox", "developerInstructions", "model",
                                 "config"),
          "TurnStartParams": ("threadId", "input", "effort", "model", "outputSchema"),
          "InitializeCapabilities": ("experimentalApi",),
          # the commands of a session (hosts/codex_cmds.py): models and their efforts, skills
          "ModelListParams": ("includeHidden", "cursor"), "ModelListResponse": ("data", "nextCursor"),
          "Model": ("id", "isDefault", "defaultReasoningEffort", "supportedReasoningEfforts"),
          "ReasoningEffortOption": ("reasoningEffort",), "SkillsListParams": ("cwds",),
          "SkillMetadata": ("name", "path", "enabled"),
          "TurnSteerParams": ("threadId", "expectedTurnId", "input"),
          "TurnInterruptParams": ("threadId", "turnId")}
# the host's answers to Codex's server requests (codex.py, codex_rules.answer_request, codex_mcp): field -> its values
ANSWERS = {"CommandExecutionRequestApprovalResponse": {"decision": ("accept", "decline")},
           "FileChangeRequestApprovalResponse": {"decision": ("accept", "decline")},
           "PermissionsRequestApprovalResponse": {"permissions": (), "scope": ("turn",)},
           "McpServerElicitationRequestResponse": {"action": ("accept", "decline"), "content": ()},
           "ToolRequestUserInputResponse": {"answers": ()}}
USER_INPUT = ("text", "localImage", "skill")   # the kinds of a turn's input the host sends
MEMORY, USER_SKILL = "COMPAT_MEMORY_SUMMARY", "COMPAT_USER_SKILL"
MCP_SERVER = r'''import json, os, sys
for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m:
        continue
    if m["method"] == "initialize":
        r = {"protocolVersion": m["params"].get("protocolVersion"), "capabilities": {"tools": {}},
             "serverInfo": {"name": "compat", "version": "1"}}
    elif m["method"] == "tools/list":
        r = {"tools": [{"name": "echo", "description": "echo", "inputSchema": {"type": "object", "properties": {}}}]}
    elif m["method"] == "tools/call":
        r = {"content": [{"type": "text", "text": "ECHO state=%s arg=%s" % (os.environ.get("HUB_STATE_DIR"),
                                                                           sys.argv[1:])}]}
    else:
        r = {}
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}), flush=True)
'''


# --- the fake Responses API ---------------------------------------------------------------------------------------
class FakeApi:
    """POST …/responses: the last user message "CALL <ns.tool> <json>" is answered with that function call, "CUSTOM
    <tool> <text>" with a custom tool call (apply_patch: the text is the patch), its output with "done: <output>",
    anything else with "ok". Every request body is kept (what the "model" saw)."""

    def __init__(self):
        self.bodies = []
        api = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, data, kind="application/json"):
                self.send_response(code)
                self.send_header("content-type", kind)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._send(200, b'{"models": [], "data": []}')

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = {}
                if not self.path.rstrip("/").endswith("/responses"):
                    return self._send(200, b"{}")
                api.bodies.append(body)
                item = api.answer(body, len(api.bodies))
                n = len(api.bodies)
                events = [{"type": "response.created", "response": {"id": f"r{n}"}},
                          {"type": "response.output_item.done", "item": item},
                          {"type": "response.completed", "response": {"id": f"r{n}", "usage": {
                              "input_tokens": 10, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 2,
                              "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 12}}}]
                self._send(200, "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode(),
                           "text/event-stream")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    @staticmethod
    def answer(body, n):
        """The user text may chain steps with " ;; ": step k is done after k tool outputs, then "done: <output>"."""
        outs = []
        for item in reversed(body.get("input") or []):
            if item.get("type") in ("function_call_output", "custom_tool_call_output"):
                outs.append(item)
            if item.get("role") == "user":
                text = " ".join(p.get("text", "") for p in item.get("content") or [] if isinstance(p, dict))
                at = min((i for i in (text.find("CALL "), text.find("CUSTOM ")) if i >= 0), default=-1)
                steps = [x.strip() for x in text[at:].split(" ;; ")] if at >= 0 else []
                if len(outs) < len(steps) and steps[len(outs)].startswith("CUSTOM "):
                    _, tool, raw = (steps[len(outs)] + " ").split(" ", 2)
                    raw = raw.strip() + "\n"
                    if tool == "apply_patch" and any(t.get("name") == tool and t.get("type") == "function"
                                                     for t in body.get("tools") or []):
                        # the tool is a JSON function for this model: the same text as its "input" argument
                        return {"type": "function_call", "id": f"f{n}", "call_id": f"c{n}", "name": tool,
                                "arguments": json.dumps({"input": raw})}
                    return {"type": "custom_tool_call", "id": f"t{n}", "call_id": f"c{n}", "name": tool, "input": raw}
                if len(outs) < len(steps):
                    _, tool, args = (steps[len(outs)] + " {}").split(" ", 2)
                    ns, _, short = tool.rpartition(".")
                    return {"type": "function_call", "id": f"f{n}", "call_id": f"c{n}", "name": short,
                            **({"namespace": ns} if ns else {}), "arguments": args.strip().removesuffix(" {}")}
                break
        if outs:
            return {"type": "message", "role": "assistant", "id": f"m{n}",
                    "content": [{"type": "output_text", "text": "done: " + str(outs[0].get("output"))[:2000]}]}
        return {"type": "message", "role": "assistant", "id": f"m{n}",
                "content": [{"type": "output_text", "text": "ok"}]}

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def provider(url):
    """codex's own flags: the model provider is the fake API (no sign-in)."""
    return ["-c", 'model_provider="hubcompat"', "-c",
            'model_providers.hubcompat={name="hubcompat",base_url="%s",wire_api="responses",'
            'requires_openai_auth=false}' % url]


# `codex -c X app-server -c Y` drops X (probe 2026-11-05, 0.162.1): the provider goes after the subcommand, next to
# the host's own pins — through this launcher (a list for CodexHost.codex, the same on Windows)
LAUNCHER = r'''import subprocess, sys
real, flags, args = sys.argv[1], sys.argv[2], sys.argv[3:]
if args[:1] == ["app-server"] and args[1:2] != ["generate-json-schema"]:
    args = ["app-server", *flags.split("\x1f"), *args[1:]]
sys.exit(subprocess.call([real, *args]))
'''


# --- static checks ------------------------------------------------------------------------------------------------
def isolated_env(run):
    home = os.path.join(run, "home")
    os.makedirs(home, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "ANTHROPIC", "HUB_", "OPENAI", "CODEX"))}
    env.update(CODEX_HOME=os.path.join(run, "codex-static"), HOME=home, USERPROFILE=home)
    os.makedirs(env["CODEX_HOME"], exist_ok=True)
    return env


def schema_problems(binary, run):
    out, exp = os.path.join(run, "schema"), os.path.join(run, "schema-experimental")
    failed = {}
    for flags, where in (((), out), (("--experimental",), exp)):
        p = subprocess.run([binary, "app-server", "generate-json-schema", *flags, "--out", where],
                           env=isolated_env(run), capture_output=True, text=True, timeout=120)
        if p.returncode:
            label = " ".join(("generate-json-schema",) + flags)
            failed[flags] = f"{label}: exit code {p.returncode} {p.stderr.strip()[-300:]}"
    if () in failed:
        return list(failed.values())   # no stable schema: nothing to check
    # the experimental one failed alone: the stable problems are still named
    return list(failed.values()) + check_schema(out, experimental=None if failed else exp)


def _defs(out):
    defs = {}
    for path in glob.glob(os.path.join(out, "**", "*.json"), recursive=True):
        with open(path, encoding="utf-8") as f:
            s = json.load(f)
        for k, v in (s.get("definitions") or s.get("$defs") or {}).items():
            defs.setdefault(k, v)
    return defs


def _keys(node, local):
    """A literal parameters dict -> {key: {constant string values}}: conditional keys and spreads give both branches,
    a spread of a name — the dicts assigned to it in the same function (local)."""
    out = {}
    if isinstance(node, ast.IfExp):
        for branch in (node.body, node.orelse):
            for k, v in _keys(branch, local).items():
                out.setdefault(k, set()).update(v)
        return out
    if isinstance(node, ast.Name):
        for d in local.get(node.id, ()):
            for k, v in _keys(d, {}).items():
                out.setdefault(k, set()).update(v)
        return out
    if not isinstance(node, ast.Dict):
        return out
    for k, v in zip(node.keys, node.values):
        if k is None:   # **spread
            for kk, vv in _keys(v, local).items():
                out.setdefault(kk, set()).update(vv)
            continue
        names = [k] if not isinstance(k, ast.IfExp) else [k.body, k.orelse]
        values = {x.value for x in ([v.body, v.orelse] if isinstance(v, ast.IfExp) else [v])
                  if isinstance(x, ast.Constant) and isinstance(x.value, str)}
        for n in names:
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                out.setdefault(n.value, set()).update(values)
    return out


def _own(scope):
    """The nodes of a function, class or module without those of the functions and classes defined inside it (their
    own node is included: visit goes into it)."""
    todo = list(ast.iter_child_nodes(scope))
    while todo:
        n = todo.pop()
        yield n
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):   # a lambda is part of its scope
            todo.extend(ast.iter_child_nodes(n))


def host_calls(repo=REPO):
    """What the Codex host's code (HOST_FILES) names of the protocol -> (every method it sends or handles,
    {request: {parameter: {constant values}}}). A method is any string that looks like one ("thread/start"); a request
    is a call with the method and a dict after it, or a message dict {"method": …, "params": {…}}."""
    methods, params = set(), {}

    def add(method, keys):
        mine = params.setdefault(method, {})
        for k, v in keys.items():
            mine.setdefault(k, set()).update(v)

    def visit(scope, outer):
        """One scope (module, class, function): its own dict names over the enclosing ones (a closure sees them), its
        calls and message dicts; the scopes inside it after."""
        own = list(_own(scope))
        local = dict(outer)
        names = {}
        for n in own:
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Dict):
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        names.setdefault(t.id, []).append(n.value)
        local.update(names)   # a name assigned here hides the enclosing one
        for n in own:
            if isinstance(n, ast.Call):
                args = n.args
                for i, x in enumerate(args):
                    if isinstance(x, ast.Constant) and isinstance(x.value, str) and METHOD.match(x.value) \
                            and not x.value.startswith(OWN) and i + 1 < len(args):
                        add(x.value, _keys(args[i + 1], local))
                        break
            elif isinstance(n, ast.Dict):
                d = {k.value: v for k, v in zip(n.keys, n.values) if isinstance(k, ast.Constant)}
                m = d.get("method")
                if isinstance(m, ast.Constant) and isinstance(m.value, str) and "params" in d \
                        and not m.value.startswith(OWN):
                    add(m.value, _keys(d["params"], local))
        for n in own:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(n, local if not isinstance(n, ast.ClassDef) else outer)

    for folder, mask in HOST_FILES:
        for path in sorted(glob.glob(os.path.join(repo, folder, mask))):
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read(), path)
            visit(tree, {})
            for n in ast.walk(tree):
                if isinstance(n, ast.Constant) and isinstance(n.value, str) and METHOD.match(n.value) \
                        and not n.value.startswith(OWN):
                    methods.add(n.value)
    # what has no "/" in its name: the handshake (its parameters are checked as any request's) and the turn's error
    methods |= set(params) | {"error"}
    if repo == REPO:   # a session's thread: its mode is Codex's approval policy and sandbox, set in a step (MODES)
        from hosts import codex_rules
        for m in ("thread/start", "thread/resume"):
            add(m, {"approvalPolicy": {a for a, _ in codex_rules.MODES.values()},
                    "sandbox": {b for _, b in codex_rules.MODES.values()}})
    return methods, params


def _entries(folder, name):
    """{method: its entry} of one schema file of the folder ({} if the file is not there)."""
    path = os.path.join(folder, name)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        s = json.load(f)
    out = {}
    for v in s.get("oneOf") or s.get("anyOf") or []:
        m = (v.get("properties") or {}).get("method") or {}
        for x in m.get("enum") or ([m["const"]] if "const" in m else []):
            out[x] = v
    return out


def _resolve(node, defs, depth=0):
    while isinstance(node, dict) and "$ref" in node and depth < 20:
        node, depth = defs.get(node["$ref"].rsplit("/", 1)[-1]) or {}, depth + 1
    return node if isinstance(node, dict) else {}


def _props(node, defs):
    """The properties an object schema can have, its own and its variants' (a tagged union) -> {name: [schemas]}."""
    node = _resolve(node, defs)
    out = {k: [p] for k, p in (node.get("properties") or {}).items()}
    for v in (node.get("oneOf") or []) + (node.get("anyOf") or []) + (node.get("allOf") or []):
        for k, p in _props(v, defs).items():
            out.setdefault(k, []).extend(p)
    return out


def _allowed(node, defs, depth=0):
    """The string values a property takes -> a set, or None when any string goes (or it is not a string at all)."""
    node = _resolve(node, defs)
    if "enum" in node or "const" in node:
        return {x for x in node.get("enum") or [node.get("const")] if isinstance(x, str)}
    variants = (node.get("oneOf") or []) + (node.get("anyOf") or []) + (node.get("allOf") or [])
    if not variants or depth > 10:
        kind = node.get("type")
        return None if not kind or kind == "string" or (isinstance(kind, list) and "string" in kind) else set()
    out = set()
    for v in variants:
        got = _allowed(v, defs, depth + 1)
        if got is None:
            return None
        out |= got
    return out


def _values(label, values, props, defs):
    """The constant values the host sends for a property (its schemas over the variants) -> what the version lacks."""
    allowed = set()
    for p in props:
        got = _allowed(p, defs)
        if got is None:
            return []
        allowed |= got
    return [(label % v) + " — the version does not know the value" for v in sorted(values) if v not in allowed]


def check_schema(out, experimental=None, calls=None):
    """What the host sends and handles (host_calls) against the schema folder, its experimental part against the
    --experimental folder (if given), the fields it reads (FIELDS) and its input kinds -> what is missing."""
    methods, params = calls or host_calls()
    names = ("ClientRequest.json", "ServerNotification.json", "ServerRequest.json")
    stable = {}
    for name in names:
        stable.update(_entries(out, name))
    exp, exp_defs = {}, {}
    if experimental and not os.path.exists(os.path.join(experimental, "ClientRequest.json")):
        return ["no ClientRequest.json (--experimental)"] + check_schema(out, None, (methods, params))
    if experimental:
        for name in names:
            exp.update(_entries(experimental, name))
        exp_defs = _defs(experimental)
    defs = _defs(out)
    # without the experimental schema a method or parameter missing from the stable one may be only experimental
    problems = [f"no {m}" + ("" if experimental else " (--experimental)")
                for m in sorted(methods) if m not in stable and m not in exp]
    for m in sorted(params):
        entry, where = (stable.get(m), defs) if m in stable else (exp.get(m), {**defs, **exp_defs})
        if not entry:   # missing from both schemas: named above (every request is in methods)
            continue
        ref = (entry.get("properties") or {}).get("params") or {}
        have = _props(ref, where)
        exp_have = _props(ref, {**defs, **exp_defs}) if experimental else {}
        for k in sorted(params[m]):
            if k not in have and k not in exp_have:
                problems.append(f"no {m} {{{k}}}" + ("" if experimental else " (--experimental)"))
                continue
            # a stable request's values by the stable schema: an experimental definition of the same name may take more
            problems += _values(f"{m} {{{k}: %s}}", params[m][k], have[k] if k in have else exp_have[k],
                                where if k in have else {**defs, **exp_defs})
    for k, fields in ANSWERS.items():   # the host's answers to Codex's questions
        have = _props({"$ref": "#/definitions/" + k}, defs)
        for f, values in fields.items():
            if f not in have:
                problems.append(f"no {k}.{f}")
            else:
                problems += _values(f"{k} {{{f}: %s}}", values, have[f], defs)
    for k, fields in FIELDS.items():
        have = set((defs.get(k) or {}).get("properties") or {})
        problems += [f"no {k}.{f}" for f in fields if f not in have]
    kinds = set()
    for v in (defs.get("UserInput") or {}).get("oneOf") or (defs.get("UserInput") or {}).get("anyOf") or []:
        kind = ((v if isinstance(v, dict) else {}).get("properties") or {}).get("type") or {}
        kinds.update(kind.get("enum") or ([kind["const"]] if "const" in kind else []))
    problems += [f"no UserInput {{type: {k}}}" for k in USER_INPUT if k not in kinds]
    return problems


def feature_problems(binary, run):
    from hosts import codex, codex_set
    p = subprocess.run([binary, "features", "list"], env=isolated_env(run), capture_output=True, text=True, timeout=60)
    if p.returncode:   # not "every flag unknown": the list itself is not there
        return [f"features list: exit code {p.returncode} {p.stderr.strip()[-300:]}"]
    known = {line.split()[0] for line in p.stdout.splitlines() if line.strip()}
    want = list(codex_set.PINNED) + list(codex.FEATURES) + list(codex_set.CORE_MODEL_OFF)
    return [f"flag {f} is unknown to the version — pinning silently has no effect" for f in want if f not in known]


# hooks a thread gets in its config (a set's or a repo's, hosts/codex_set): Codex runs one only if its trusted_hash under
# [hooks.state."<key>"] is the one it computes itself — a key or hash the core computes otherwise and the hooks are
# silently skipped. Neutral commands, every field that goes into the hash.
HOOKS = {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 /x/a b.py", "timeout": 30},
                                                     {"type": "command", "command": "echo 2", "statusMessage": "checking"}]},
                        {"hooks": [{"type": "command", "command": "echo nomatch"}]}],
         "PostToolUse": [{"matcher": "Edit|Write", "hooks": [{"type": "command", "command": "echo post", "async": True}]}],
         "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "echo ups"}]}],
         "SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "echo ss", "timeout": 5}]}],
         "Stop": [{"hooks": [{"type": "command", "command": "echo stop"}]}]}


def _toml(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, list):
        return "[" + ",".join(_toml(x) for x in v) + "]"
    return "{" + ",".join(f"{k}={_toml(x)}" for k, x in v.items()) + "}"


def compare_hooks(got, mine):
    """hooks/list of the version {key: currentHash} against the core's {key: trusted_hash} -> what differs. Keys are
    matched by their tail (":event:group:hook"): a different prefix (the folder of the flags, "C:\\…" on Windows) is
    named with both keys, and the hashes are still compared."""
    def tail(k):
        return tuple(k.rsplit(":", 3)[-3:])
    theirs, ours = {tail(k): k for k in got}, {tail(k): k for k in mine}
    problems = []
    for t in sorted(set(theirs) | set(ours)):
        a, b = theirs.get(t), ours.get(t)
        if a is None:
            problems.append(f"hook {b}: the core expects it, Codex has no such hook")
        elif b is None:
            problems.append(f"hook {a}: Codex has it, the core does not know it")
        else:
            if a != b:
                problems.append(f"hook key: Codex {a}, core {b} — the trust will not be found, the set's hooks silently will not run")
            if got[a] != mine[b]:
                problems.append(f"hook {a}: Codex computes {got[a]}, core {mine[b]} — the set's hooks silently will not run")
    return problems


def _hooks_list(binary, env, args, timeout):
    """hooks/list of `codex app-server <args>` in env's CODEX_HOME -> ({key: hook}, None) or (None, the failure)."""
    import queue
    p = subprocess.Popen([binary, "app-server", *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, encoding="utf-8", env=env, cwd=env["CODEX_HOME"])
    lines = queue.Queue()
    threading.Thread(target=lambda: [lines.put(x) for x in p.stdout] + [lines.put(None)], daemon=True).start()
    try:
        for m in ({"id": 0, "method": "initialize", "params": {"clientInfo": {"name": "compat", "version": "0"}}},
                  {"method": "initialized"}, {"id": 1, "method": "hooks/list", "params": {"cwds": [env["CODEX_HOME"]]}}):
            p.stdin.write(json.dumps(m) + "\n")
            p.stdin.flush()
        end = time.time() + timeout
        while True:
            if time.time() > end:   # a stream of notifications without the answer
                raise queue.Empty
            line = lines.get(timeout=max(0.1, end - time.time()))
            if line is None:
                return None, "hooks/list: app-server closed"
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("id") == 1:
                break
    except queue.Empty:
        return None, f"hooks/list: no answer within {timeout} s"
    finally:
        p.kill()
        p.wait()
    if "error" in m:
        return None, f"hooks/list: {m['error']}"
    return {h.get("key"): h for d in (m.get("result") or {}).get("data") or [] for h in d.get("hooks") or []}, None


def hook_problems(binary, run, timeout=60):
    """The core's trust of a thread's hooks (hosts/codex_set.hook_trust) against what the version computes, then the
    proof: the core's keys and hashes written as [hooks.state] in the version's config.toml — Codex must call every
    hook trusted (it runs only those)."""
    from hosts import codex_set
    trust = getattr(codex_set, "hook_trust", None)
    if trust is None:   # a core without hooks in a thread's config: nothing to compare
        return []
    args = [x for ev, groups in HOOKS.items() for x in ("-c", f"hooks.{ev}={_toml(groups)}")]
    env = isolated_env(run)
    got, err = _hooks_list(binary, env, args, timeout)
    if err:
        return [err]
    # the keys' path the core learns from its Codex at the trust check (hosts/codex_rules.flags_path: "C:\\…" on
    # Windows); a core before it had none
    from hosts import codex_rules
    info = {}
    if getattr(codex_rules, "FLAGS_PROBE", None):
        codex_rules._hooks_list([binary], env, env["CODEX_HOME"], timeout, info=info)
    if getattr(codex_rules, "FLAGS_PROBE", None) and not info.get("flags"):
        return [f"the core did not learn the trust-key path from Codex (the core's hooks/list got no answer or has no sessionFlags entry; "
                f"Codex has: {[k for k in got if k][:1]}) — the set's hooks silently will not run"]
    mine = trust(HOOKS, info["flags"]) if info.get("flags") else trust(HOOKS)
    problems = compare_hooks({k: h.get("currentHash") for k, h in got.items()}, mine)
    with open(os.path.join(env["CODEX_HOME"], "config.toml"), "a", encoding="utf-8") as f:
        f.writelines(f"\n[hooks.state.{json.dumps(k)}]\ntrusted_hash = {json.dumps(v)}\n" for k, v in mine.items())
    again, err = _hooks_list(binary, env, args, timeout)
    if err:
        return problems + [err]
    left = sorted(k for k, h in again.items() if h.get("trustStatus") != "trusted")
    if left:
        problems.append(f"Codex did not accept the trust written by the core ({len(left)} of {len(again)}): {left[0]} … — "
                        "these hooks will not run")
    return problems


def version_of(binary, run):
    p = subprocess.run([binary, "--version"], env=isolated_env(run), capture_output=True, text=True, timeout=60)
    return (p.stdout.strip().split() or ["?"])[-1]


# --- the live hub (a child process: its own HOME, state and settings) ----------------------------------------------
def state_seen(said, state):
    """The MCP server's answer names the session's state folder: as is, or as repr shows a Windows path ("\\" doubled)."""
    return any(f"state={s}" in said for s in (state, state.replace("\\", "\\\\")))


def live(binary, run, timeout):
    """The child's report: {"problems": […], "seen": …}."""
    os.makedirs(run, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "ANTHROPIC", "HUB_", "OPENAI", "CODEX"))}
    p = subprocess.run([sys.executable, os.path.abspath(__file__), "--child", binary, run, str(timeout)], env=env,
                       capture_output=True, text=True, timeout=timeout + 60)
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"problems": [f"the check crashed:{(p.stderr or p.stdout).strip()[-600:]}"]}


def child(binary, run, timeout):
    state, home, work = (os.path.join(run, d) for d in ("state", "home", "work"))
    for d in (state, home, os.path.join(home, ".agents", "skills", "compat-user"), os.path.join(work, ".codex"),
              os.path.join(state, "codex-home", "memories")):
        os.makedirs(d, exist_ok=True)
    with open(os.path.join(home, ".agents", "skills", "compat-user", "SKILL.md"), "w") as f:
        f.write(f"---\nname: compat-user\ndescription: {USER_SKILL}\n---\nx\n")
    with open(os.path.join(state, "codex-home", "memories", "memory_summary.md"), "w") as f:
        f.write(MEMORY + "\n")
    # a hub on Codex holds spawns until its Codex has a sign-in file (daemon.codex_signed_in); the model here is the
    # fake API, which asks for none — a placeholder file, no key
    with open(os.path.join(state, "codex-home", "auth.json"), "w") as f:
        json.dump({"auth_mode": "apikey", "OPENAI_API_KEY": "compat-placeholder"}, f)
    # the project turns off what the hub's isolation stands on (Codex trusts the folder itself)
    with open(os.path.join(work, ".codex", "config.toml"), "w") as f:
        f.write("[features]\nhooks = false\nmemories = true\nmulti_agent = true\nmulti_agent_v2 = true\n")
    with open(os.path.join(run, "mcp_server.py"), "w") as f:
        f.write(MCP_SERVER)
    with open(os.path.join(work, ".mcp.json"), "w") as f:
        json.dump({"mcpServers": {"compat": {"command": sys.executable,
                                             "args": [os.path.join(run, "mcp_server.py"), "${COMPAT_ARG:-dflt}"]}}}, f)
    with open(os.path.join(run, "hub.json"), "w") as f:
        json.dump({"host": "codex", "codex": {"model": "fake-model"}, "sets": [], "base": [], "mcp": {},
                   "connectors": False}, f)
    subprocess.run(["git", "init", "-q", work], capture_output=True)
    # `hub` of the sessions here is a stand-in that only prints a mark: the liaison's command is seen to run, and
    # whatever `hub` the machine has (a live hub) is never reached
    bindir = os.path.join(run, "bin")
    os.makedirs(bindir, exist_ok=True)
    if os.name == "nt":
        with open(os.path.join(bindir, "hub.cmd"), "w") as f:
            f.write(f"@echo {HUB_MARK} %*\r\n")
    else:
        with open(os.path.join(bindir, "hub"), "w") as f:
            f.write(f'#!/bin/sh\necho {HUB_MARK} "$@"\n')
        os.chmod(os.path.join(bindir, "hub"), 0o755)
    os.environ["PATH"] = bindir + os.pathsep + os.environ.get("PATH", "")
    os.environ.update(HOME=home, USERPROFILE=home, HUB_STATE_DIR=state, HUB_CONFIG=os.path.join(run, "hub.json"),
                      HUB_NO_MODEL="1", COMPAT_ARG="from-env")
    from core import journal, spool
    from core.daemon import Hub
    from hosts.codex import CodexHost

    class CompatHub(Hub):   # no side effects outside the run (as evals/driver.EvalHub)
        def ensure_dev(self):
            pass

        def fork(self):
            pass

        def switch_accounts(self):
            pass

    api = FakeApi()
    problems = []
    with open(os.path.join(run, "launcher.py"), "w") as f:
        f.write(LAUNCHER)
    codex = [sys.executable, os.path.join(run, "launcher.py"), binary, "\x1f".join(provider(api.url))]
    host = CodexHost(state, codex=codex, all_sessions=True, default_model="fake-model")
    hub = CompatHub(state, host, registry_dir=os.path.join(run, "registry"))
    hub.start()
    end = time.time() + timeout

    def events(type):
        return [e for e in journal.read(hub.journal.path) if e["type"] == type and e.get("entity") == "s"]

    def until(pred, within=None):   # within — a step's own limit: one failed step leaves time to the others
        stop = min(end, time.time() + within) if within else end
        while time.time() < stop:
            hub.tick()
            if pred():
                return True
            time.sleep(0.1)
        return False

    def put(text):
        spool.put(os.path.join(state, "spool"), "message.received", None, {"to": "s", "text": text})

    def turn(text):
        before = len(events("turn.result"))
        put(text)
        if not until(lambda: len(events("turn.result")) > before):
            return None
        return events("turn.result")[-1]["data"].get("text") or ""

    try:
        spool.put(os.path.join(state, "spool"), "session.requested", "s", {"cwd": work, "session_id": "sid-s"})
        if not until(lambda: (hub.state["sessions"].get("s") or {}).get("state") == "idle"):
            errs = [e["data"] for e in journal.read(hub.journal.path) if e["type"] == "host.error"]
            raise RuntimeError(f"the session did not come up within {timeout} s:{errs[-1:] or hub.state['sessions'].get('s')}")
        said = turn("compat probe: answer")
        if said is None or "ok" not in said:
            problems.append(f"the turn did not reach turn.result ({said!r})")
        said = turn('CALL exec_command {"cmd":"pkill -f hub-codex-compat-nothing"}')
        if not said or "PreToolUse" not in said:
            problems.append(f"the core's ban (hook) did not fire with hooks = false in the project: {said!r}")
        said = turn("CALL mcp__compat.echo {}")
        if not said or not state_seen(said, state):
            problems.append(f"the set's MCP server runs without the session environment: {said!r}")
        elif "from-env" not in said:
            problems.append(f"${{NAME:-default}} in .mcp.json is not expanded from the environment: {said!r}")
        seen = json.dumps(api.bodies, ensure_ascii=False)
        tools = {t.get("name") for b in api.bodies for t in b.get("tools") or []}
        from hosts.codex import NOTE
        if json.dumps(NOTE.split(".")[0], ensure_ascii=False)[1:-1] not in seen:   # its first sentence, in any language
            problems.append("the core's note (hosts/codex.NOTE) is missing from the instructions")
        if "mcp__compat" not in tools:
            problems.append(f"the set's MCP tools are missing:{sorted(t for t in tools if t)}")
        nested = {n.get("name") for b in api.bodies for t in b.get("tools") or [] for n in [t, *(t.get("tools") or [])]}
        if "spawn_agent" not in nested:   # subagents are the host's own (hosts/codex_items.FEATURES), a session has them
            problems.append("Codex subagents did not turn on (hosts/codex_items.FEATURES)")
        for mark, what in ((MEMORY, "Codex memory"), (USER_SKILL, "a skill from $HOME/.agents/skills"),
                           ("skill-installer", "Codex's system skill skill-installer")):
            if mark in seen:
                problems.append(f"reached the model: {what}")
        problems += stop_and_sandbox(host, work, run, until, put, lambda: len(events("turn.result")), api.url)
        problems += no_subagents(api, host, hub, work, until)
        problems += apply_patch_form(api, host, run, until)
        problems += hook_failure_refuses(host, work, run, state, until)
    except Exception as e:  # noqa: BLE001 — the report says what broke, the table goes on
        problems.append(f"{type(e).__name__}: {e}")
    finally:
        try:
            host.kill_server()
        except Exception:  # noqa: BLE001
            pass
        api.stop()
    tools = sorted({t.get("name") or t.get("type") or "?" for b in api.bodies for t in b.get("tools") or []})
    print(json.dumps({"problems": problems, "requests": len(api.bodies), "tools": tools}, ensure_ascii=False))


TICKER = r'''import os, sys, time
pid = os.path.join(os.path.dirname(os.path.abspath(__file__)), sys.argv[1] + ".pid")
with open(pid + ".tmp", "w") as f:
    f.write(str(os.getpid()))
os.replace(pid + ".tmp", pid)
stop = os.path.join(os.path.dirname(pid), "stop")   # the probe's end: it leaves by itself, no signal by a number
end = time.time() + 120
while time.time() < end and not os.path.exists(stop):
    time.sleep(0.2)
'''
WRITER = r'''import os
print("WRITER RAN", flush=True)
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "work", "ro-written.txt"), "w") as f:
    f.write("x")
print("WRITER WROTE", flush=True)
'''


def shell_cmd(*args):
    """A command line for Codex's shell tool: bash here, PowerShell on Windows (a quoted path is called with &)."""
    if os.name == "nt":
        return ("& " if any(" " in a for a in args) else "") + " ".join(f'"{a}"' if " " in a else a for a in args)
    import shlex
    return " ".join(shlex.quote(a) for a in args)


def running(pid):
    """Whether the process runs: a zombie (ended, its parent has not collected it yet) does not."""
    from platforms import current
    if not current.alive(pid):
        return False
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        pass
    if sys.platform == "darwin":
        r = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
        return bool(r.stdout.strip()) and not r.stdout.strip().startswith("Z")
    return True   # Windows: alive as the platform says (no zombies there)


def stop_and_sandbox(host, work, run, until, put, results, url=None):
    """A turn's command still running when the turn is interrupted is stopped with it (hosts/codex_items: Codex leaves it
    as a background terminal, the host terminates it); a session in a read-only mode (dontAsk: never ask, read-only
    sandbox) runs a command but cannot write. Both are the platform's: Windows has its own process tree and sandbox."""
    problems = []
    with open(os.path.join(run, "ticker.py"), "w") as f:
        f.write(TICKER)
    # the first command goes to the background (its wait is short), the second holds the turn: the interrupt leaves the
    # first as Codex's background terminal — only the host's terminate stops it (the second Codex stops itself)
    bg, fg = (os.path.join(run, f"{x}.pid") for x in ("bg", "fg"))
    steps = [{"cmd": shell_cmd(sys.executable, os.path.join(run, "ticker.py"), x), "yield_time_ms": ms}
             for x, ms in (("bg", 1000), ("fg", 30000))]
    before = results()
    put(" ;; ".join("CALL exec_command " + json.dumps(st) for st in steps))
    started = until(lambda: os.path.exists(bg) and os.path.exists(fg), within=30)
    if not started:
        problems.append(f"the turn's commands did not start (turns: {results() - before})")
    pids = []
    for path in (bg, fg):
        try:
            with open(path) as f:
                pids.append(int(f.read()))
        except (OSError, ValueError):
            pids.append(None)
    if started or results() == before:   # also when only one started: no turn of s may go on into the next probes
        try:
            host.interrupt("s")
        except RuntimeError:   # its process is gone: nothing goes on
            pass
        if not until(lambda: results() > before, within=30) and started:
            problems.append("the turn interrupt did not reach turn.result")
    for pid, what in zip(pids, ("the background command of the interrupted turn is alive: terminate did not work",
                                "the command the interrupted turn was waiting for is alive")):
        if pid and not until(lambda: not running(pid), within=15):
            if started:
                problems.append(f"{what} (pid {pid})")
    with open(os.path.join(run, "stop"), "w"):   # whatever of the ticker still runs leaves within 0.2 s
        pass
    with open(os.path.join(run, "writer.py"), "w") as f:
        f.write(WRITER)
    host.spawn("ro", "sid-ro", cwd=work, mode="dontAsk", kind="work")
    if not until(lambda: getattr(host.procs.get("ro"), "thread", None)):
        return problems + ["the session in dontAsk (read-only) mode did not come up"]
    done = {}
    proc = host.procs["ro"]
    before = proc.last_text
    host.type("ro", "CALL exec_command " + json.dumps({"cmd": shell_cmd(sys.executable, os.path.join(run, "writer.py"))}))
    until(lambda: proc.last_text != before and not proc.turn)
    said = proc.last_text or ""
    if os.path.exists(os.path.join(work, "ro-written.txt")):
        problems.append("the read-only (dontAsk) sandbox did not stop a write into the session folder")
    elif "blocked by policy" in said and os.name == "nt":   # 0.162.1: no sandbox to run it in — safe, but no command
        problems.append(NOTE + "read-only modes (plan, dontAsk): Codex lets no command run (blocked by policy)")
    elif "WRITER RAN" not in said:
        problems.append(f"in the read-only sandbox the command did not run:{said[-300:]!r}")
    if url and "WRITER RAN" in said:   # the sandbox has no network (Linux, macOS); Windows' unelevated one does not hold it
        with open(os.path.join(run, "netter.py"), "w") as f:
            f.write(NETTER)
        before = proc.last_text
        host.type("ro", "CALL exec_command " + json.dumps(
            {"cmd": shell_cmd(sys.executable, os.path.join(run, "netter.py"), url.rsplit("/v1", 1)[0] + "/ping")}))
        until(lambda: proc.last_text != before and not proc.turn)
        said = proc.last_text or ""
        if "NET OPEN" in said:
            problems.append((NOTE if os.name == "nt" else "") + "the read-only sandbox let network through (127.0.0.1)")
        elif "NET SHUT" not in said:
            problems.append(f"the network check in the read-only sandbox did not run:{said[-300:]!r}")
    return problems


def _tools(bodies):
    """Every tool name the "model" was given, namespaces opened (MCP servers, the subagents' group)."""
    return {n.get("name") for b in bodies for t in b.get("tools") or [] for n in [t, *(t.get("tools") or [])]
            if n.get("name")} | {t.get("type") for b in bodies for t in b.get("tools") or [] if t.get("type")}


NETTER = r'''import sys, urllib.request
try:
    urllib.request.urlopen(sys.argv[1], timeout=5)
    print("NET OPEN", flush=True)
except Exception as e:  # noqa: BLE001 — any refusal is "shut"
    print("NET SHUT", type(e).__name__, flush=True)
'''
HUB_MARK = "HUB-COMPAT-RAN"
NOTE = "~ "   # a problem line with it is a limit of the platform or version, not an incompatibility: the row stays ok
SUBAGENT_TOOLS = ("spawn_agent", "send_input", "wait_agent", "close_agent", "spawn_agents_on_csv")


def no_subagents(api, host, hub, work, until):
    """Who must have no Codex subagents even where the project's .codex/config.toml turns them on: the bus's liaison
    (it reads another hub's words; Claude's has Agent and Task denied) and the core's model (one answer, read-only)."""
    from core import codexmodel
    problems = []
    mark = "compat probe: liaison"
    host.spawn("liaison", "sid-liaison", cwd=work, mode="bypassPermissions", kind="work", liaison=True)
    if not until(lambda: getattr(host.procs.get("liaison"), "thread", None)):
        return ["the liaison did not come up on Codex"]
    host.type("liaison", mark + " ;; CALL exec_command " + json.dumps({"cmd": "hub gist compat"}))

    def mine():   # only the liaison's requests: another session's turn may still be calling the fake API
        return [b for b in api.bodies if mark in json.dumps(b.get("input"), ensure_ascii=False)]
    if not until(lambda: mine() and not getattr(host.procs.get("liaison"), "turn", None), within=60):
        problems.append("the liaison's turn did not reach the model")
    got = _tools(mine())
    said = getattr(host.procs.get("liaison"), "last_text", "") or ""
    if "blocked by policy" in said or "rejected:" in said:   # its guard lets `hub gist` through; its sandbox must too
        problems.append(f"the liaison's sandbox does not let even hub … run:{said[-300:]!r}")
    elif HUB_MARK not in said:
        problems.append(f"the liaison's hub … command did not run:{said[-300:]!r}")
    # anything but `hub …` is its guard's to refuse (hooks/liaison_guard.py through the core's hook), on every platform
    proc = host.procs.get("liaison")
    before = getattr(proc, "last_text", "")
    host.type("liaison", "CALL exec_command " + json.dumps({"cmd": shell_cmd(sys.executable, "--version")}))
    until(lambda: getattr(proc, "last_text", "") != before and not getattr(proc, "turn", None), within=60)
    said = getattr(proc, "last_text", "") or ""
    if "PreToolUse" not in said:
        problems.append(f"the liaison's guard did not forbid a command other than hub …:{said[-300:]!r}")
    problems += [f"the liaison has {t} (Codex subagents)" for t in SUBAGENT_TOOLS if t in got]
    problems += [f"the liaison has {what}" for t, what in (("mcp__compat", "the set's MCP"),
                                                           ("web_search", "web search")) if t in got]
    # the core's model works in the temp folder (any user of the machine can write a .codex/config.toml there, and
    # Codex reads a project's config only where it trusts it) and, for the evals of a session's rules, in a folder of
    # its own (evals/prompts.py) — here the session's, which the host made trusted: the worst case
    tmp = os.path.join(os.path.dirname(work), "tmp")
    os.makedirs(os.path.join(tmp, ".codex"), exist_ok=True)
    with open(os.path.join(tmp, ".codex", "config.toml"), "w") as f:
        f.write("[features]\nmulti_agent = true\nmulti_agent_v2 = true\n")
    for where, cwd in (("in the temp folder", None), ("in the trusted folder", work)):
        before = len(api.bodies)
        tempfile.tempdir = tmp   # codexmodel.run: cwd None is tempfile.gettempdir()
        try:
            said, _ = codexmodel.run(host.codex, "compat probe: core model", "Answer.", None, "fake-model", "low",
                                     60, codexmodel.env(host.state_dir), cwd=cwd)
        except Exception as e:  # noqa: BLE001
            problems.append(f"the core's model {where}: {type(e).__name__}: {e}")
            continue
        finally:
            tempfile.tempdir = None
        got = _tools(api.bodies[before:])
        if "ok" not in str(said):
            problems.append(f"the core's model {where} did not answer:{said!r}")
        problems += [f"the core's model {where} has {t} (Codex subagents)" for t in SUBAGENT_TOOLS if t in got]
    # the core's model has no tools (core/codexmodel.ask): the hub folder's hook refuses a command it asks for, the pins
    # alone (CORE_MODEL_OFF) leave the hook the only layer here — the hook is what a version must keep holding
    try:
        said, _ = codexmodel.run(host.codex, "CALL exec_command " + json.dumps({"cmd": shell_cmd(sys.executable, "--version")}),
                                 "Answer.", None, "fake-model", "low", 60,
                                 {**codexmodel.env(host.state_dir), **codexmodel.CORE_MODEL_ENV}, cwd=work,
                                 hook_home=os.path.join(host.state_dir, "codex-home"))
    except Exception as e:  # noqa: BLE001
        problems.append(f"the core's model with no tools: {type(e).__name__}: {e}")
    else:
        if "PreToolUse" not in str(said) or "core's model" not in str(said):
            problems.append(f"the core's hook did not refuse the core's model a command:{said!r}"[:400])
    return problems


RECORDER = r'''import json, os, sys
d = json.loads(sys.stdin.buffer.read().decode("utf-8"))
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ap-" + sys.argv[1] + ".jsonl"), "a",
          encoding="utf-8") as f:
    f.write(json.dumps(d) + "\n")
if sys.argv[1] == "claude" and "denied" in ((d.get("tool_input") or {}).get("file_path") or ""):
    sys.stderr.write("compat guard: no writes here")
    sys.exit(2)
'''
PATCH_MODEL = "gpt-5.5"   # a model Codex gives apply_patch (a custom tool); fake-model gets none


def patch_form(raw, claude, offered=True):
    """What the hooks of a repo got on one patch adding allowed.txt -> problems. raw — the inputs of a hook with the
    matcher apply_patch, which Codex runs as is; claude — those of an Edit|Write hook, which the core puts behind
    hooks/edit_hooks.py. The script reads Codex's form (tool_name apply_patch, the patch in tool_input.command): a
    version that gives another one leaves every patch in a repo with such hooks refused."""
    if not offered:
        # red: without apply_patch nothing shows the repo's edit hooks work (a renamed tool, a model without it)
        return [f"apply_patch form: Codex gave the model {PATCH_MODEL} no apply_patch — the repo's edit hooks are not "
                "checked (did the tool name or the model change?)"]
    if not raw:
        return ["apply_patch form: the hook with matcher apply_patch was not called — Codex does not call hooks on "
                "file edits or renamed the tool; the repo's edit hooks (Edit|Write) silently will not run"]
    got = raw[0]
    inp = got.get("tool_input")
    if got.get("tool_name") != "apply_patch" or not isinstance(inp, dict) or "Add File: allowed.txt" not in str(
            inp.get("command")):
        keys = sorted(inp) if isinstance(inp, dict) else type(inp).__name__
        return [f"apply_patch form: the hook got tool_name={got.get('tool_name')!r}, tool_input {keys} (the core expects "
                "apply_patch and the patch in tool_input.command) — the repo's edit hooks (hooks/edit_hooks) will refuse every patch"]
    writes = [c for c in claude if c.get("tool_name") == "Write"
              and os.path.basename(str((c.get("tool_input") or {}).get("file_path"))) == "allowed.txt"]
    if not writes or writes[0]["tool_input"].get("content") != "ok\n":
        return [f"apply_patch form: the repo's Edit|Write hook did not get Write in Claude Code's form:{claude[:1]!r}"[:400]]
    return []


def apply_patch_form(api, host, run, until):
    """A repo's hooks on file edits on Codex (hooks/edit_hooks.py): Codex runs them on apply_patch with its own input;
    the form is checked (patch_form), then that the repo's guard refusing a file refuses the patch."""
    work = os.path.join(run, "work-ap")
    os.makedirs(os.path.join(work, ".claude"), exist_ok=True)
    subprocess.run(["git", "init", "-q", work], capture_output=True)
    rec = os.path.join(run, "recorder.py")
    with open(rec, "w") as f:
        f.write(RECORDER)

    def hook(tag):
        return {"type": "command", "command": shell_cmd(sys.executable, rec, tag)}
    with open(os.path.join(work, ".claude", "settings.json"), "w") as f:
        json.dump({"hooks": {"PreToolUse": [{"matcher": "apply_patch", "hooks": [hook("raw")]},
                                            {"matcher": "Edit|Write", "hooks": [hook("claude")]}]}}, f)
    before = len(api.bodies)
    try:
        said = host.spawn("ap", "sid-ap", cwd=work, mode="bypassPermissions", kind="work", model="codex/" + PATCH_MODEL)
        refused = None if said in ("spawned", "already running") else said
    except Exception as e:  # noqa: BLE001 — the host refuses a session it cannot hold (hosts/codex_rules, the sandbox)
        refused = f"{type(e).__name__}: {e}"
    if refused or not until(lambda: getattr(host.procs.get("ap"), "thread", None)):
        return [f"apply_patch form: the session with the repo's hooks did not come up{': ' + refused if refused else ''}"[:400]]
    proc = host.procs["ap"]

    def patch(name):
        was = proc.last_text
        host.type("ap", f"CUSTOM apply_patch *** Begin Patch\n*** Add File: {name}\n+ok\n*** End Patch")
        until(lambda: proc.last_text != was and not proc.turn, within=60)
        return proc.last_text or ""

    def read(tag):
        try:
            with open(os.path.join(run, f"ap-{tag}.jsonl"), encoding="utf-8") as f:
                return [json.loads(line) for line in f if line.strip()]
        except (OSError, ValueError):
            return []
    said = patch("allowed.txt")
    problems = patch_form(read("raw"), read("claude"), "apply_patch" in _tools(api.bodies[before:]))
    if not problems and not os.path.exists(os.path.join(work, "allowed.txt")):
        problems.append(f"apply_patch form: the patch the repo's guard let through was not applied:{said[-300:]!r}")
    if not problems:
        said = patch("denied.txt")
        if os.path.exists(os.path.join(work, "denied.txt")):
            problems.append("apply_patch form: the repo's guard (Edit|Write) refused, yet the patch went through")
        elif "no writes here" not in said:
            problems.append(f"apply_patch form: the guard's refusal did not reach the model:{said[-300:]!r}")
    return problems


RAN = r'''import os
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "hf-ran.txt"), "w") as f:
    f.write("x")
print("HF RAN", flush=True)
'''


def hook_failure_refuses(host, work, run, state, until):
    """A failure of the core's hook (hooks/deny_hint.py: here the session's rules file broken) refuses the call — a
    deny, not an exit code: Codex on Windows let a hook's exit 2 through (winstand, 2026-11-05), and the core's bans
    would be open while the hook fails."""
    from hosts import codex_rules
    script = os.path.join(run, "hf.py")
    with open(script, "w") as f:
        f.write(RAN)
    try:
        said = host.spawn("hf", "sid-hf", cwd=work, mode="bypassPermissions", kind="work")
        refused = None if said in ("spawned", "already running") else said
    except Exception as e:  # noqa: BLE001
        refused = f"{type(e).__name__}: {e}"
    if refused or not until(lambda: getattr(host.procs.get("hf"), "thread", None)):
        return [f"core hook failure: the session did not come up{': ' + refused if refused else ''}"[:400]]
    with open(os.path.join(state, "sessions", "hf", codex_rules.RULES), "w") as f:
        f.write("{broken")
    proc = host.procs["hf"]
    was = proc.last_text
    host.type("hf", "CALL exec_command " + json.dumps({"cmd": shell_cmd(sys.executable, script)}))
    ended = until(lambda: proc.last_text != was and not proc.turn, within=60)
    said = proc.last_text or ""
    try:
        host.kill("hf")   # its broken rules refuse every call: no later probe meets it
    except Exception:  # noqa: BLE001
        pass
    if os.path.exists(os.path.join(run, "hf-ran.txt")):
        return ["a core hook failure let the command through: the core's bans are open while the hook fails"]
    if not ended:
        return ["core hook failure: the turn with the command did not end within 60 s"]
    # the refusal carries the hook's exception and the broken rules file: the hook's own reason reached the model
    if codex_rules.RULES not in said or "RuntimeError" not in said:
        return [f"core hook failure: the reason for the refusal did not reach the model:{said[-300:]!r}"]
    return []


# --- the Codex account pool (core/codexslot.py): an account folder shares the hub folder's threads ------------------
class _App:
    """One `codex app-server` with its own CODEX_HOME: requests and notifications, no model but the fake API."""

    def __init__(self, binary, home, env, args, work, timeout=30):
        import queue
        self.timeout = timeout
        self.p = subprocess.Popen([binary, "app-server", *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, encoding="utf-8", cwd=work,
                                  env={**env, "CODEX_HOME": home})
        self.q, self.n = queue.Queue(), 0
        threading.Thread(target=self._read, daemon=True).start()
        self.call("initialize", {"clientInfo": {"name": "hub-compat", "version": "0"},
                                 "capabilities": {"experimentalApi": True}})
        self.p.stdin.write(json.dumps({"method": "initialized"}) + "\n")
        self.p.stdin.flush()

    def _read(self):
        """stdout lines into the queue; a line that is not JSON is skipped, the end of the process is None."""
        try:
            for line in self.p.stdout:
                try:
                    self.q.put(json.loads(line))
                except ValueError:
                    continue
        except (OSError, ValueError):
            pass
        self.q.put(None)

    def _next(self, end, what):
        import queue
        while time.time() < end:
            try:
                x = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            if x is None:
                self.q.put(None)
                raise RuntimeError(f"codex app-server closed on {what}")
            return x
        raise subprocess.TimeoutExpired([what], self.timeout)

    def call(self, method, params):
        self.n += 1
        rid = self.n
        self.p.stdin.write(json.dumps({"id": rid, "method": method, "params": params}) + "\n")
        self.p.stdin.flush()
        end = time.time() + self.timeout
        while True:
            x = self._next(end, method)
            if x.get("id") == rid and "method" not in x:
                return x

    def turn(self, thread, text):
        """A turn through the fake API: its turn/completed; a refused turn/start or a failed turn — RuntimeError."""
        got = self.call("turn/start", {"threadId": thread, "input": [{"type": "text", "text": text}]})
        if "error" in got:
            raise RuntimeError(f"turn/start: {(got['error'] or {}).get('message')}")
        end = time.time() + 2 * self.timeout
        while True:
            x = self._next(end, "turn/start")
            if x.get("method") == "turn/completed":
                turn = (x.get("params") or {}).get("turn") or {}
                if turn.get("status") == "failed":
                    raise RuntimeError(f"the turn failed:{(turn.get('error') or {}).get('message')}")
                return x

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            self.p.kill()


def pool_problems(binary, run, timeout=60):
    """The Codex pool's scheme on this version (spec, probes 2026-11-05): a thread begun in the hub folder H continues
    in an account folder V prepared by the core (hosts/codex.prepare_account: links to H's sessions, writer locks, MCP
    sign-ins; the index arguments codexcli.args) with its whole history, and back in H; V gets no databases of its
    own; a second writer of the same thread from V is refused."""
    from core import codexcli
    from hosts.codex import prepare_account, write_config
    state = os.path.join(run, "state")
    hub, acct, work = codexcli.home_of(state), os.path.join(state, "moddata", "accounts", "codex", "codex-2"), \
        os.path.join(run, "work")
    os.makedirs(work)
    os.makedirs(acct, mode=0o700)
    write_config(hub)
    why = prepare_account(state, acct)
    if why:
        return [f"pool: the account folder is not ready — {why}"]
    api = FakeApi()
    env = isolated_env(run)
    flags = provider(api.url)
    problems, apps = [], []

    def app(home):
        a = _App(binary, home, env, [*codexcli.args(state, home), *flags], work, timeout)
        apps.append(a)
        return a

    def said(marker):   # what the model got in the turn whose input carried marker (a later extra request is not it)
        mine = [json.dumps(b.get("input") or [], ensure_ascii=False) for b in api.bodies]
        return next((x for x in reversed(mine) if marker in x), "")
    try:
        h = app(hub)
        thread = h.call("thread/start", {"cwd": work, "approvalPolicy": "never", "sandbox": "danger-full-access",
                                         "model": "fake-model"})["result"]["thread"]["id"]
        h.turn(thread, "pool-first")
        h.close()
        v = app(acct)
        got = v.call("thread/resume", {"threadId": thread, "cwd": work})
        if "error" in got:
            return problems + [f"pool: a thread from the hub folder did not come up in the account folder —{got['error'].get('message')}"]
        v.turn(thread, "pool-second")
        if "pool-first" not in said("pool-second"):
            problems.append("pool: in the account folder the thread lacks the hub folder's history")
        own = [f for f in os.listdir(acct) if f.endswith(".sqlite")]
        if own:
            problems.append(f"pool: the account folder has databases of its own (sqlite_home not taken):{', '.join(sorted(own))}")
        # a second writer: V holds the thread, H resumes it too — refused
        h2 = app(hub)
        got = h2.call("thread/resume", {"threadId": thread, "cwd": work})
        if "error" not in got:
            problems.append("pool: a second writer of the same thread was not refused (shared thread-writer-locks/)")
        elif "writer" not in str(got["error"].get("message")):   # refused, in other words: the host's retry keys on it
            problems.append(f"{NOTE}pool: the refusal of the second writer is worded differently —{got['error'].get('message')}")
        v.close()
        h2.close()
        h3 = app(hub)
        got = h3.call("thread/resume", {"threadId": thread, "cwd": work})
        if "error" in got:
            return problems + [f"pool: the thread did not return to the hub folder —{got['error'].get('message')}"]
        h3.turn(thread, "pool-third")
        if not ("pool-first" in said("pool-third") and "pool-second" in said("pool-third")):
            problems.append("pool: in the hub folder after the account the thread lacks part of its history")
    finally:
        for a in apps:
            a.close()
        api.stop()
    return problems


# --- versions -----------------------------------------------------------------------------------------------------
def native(prefix):
    """The native codex of an npm install (the .bin launcher is a node script, a .cmd on Windows)."""
    for path in sorted(glob.glob(os.path.join(prefix, "node_modules", "@openai", "codex-*", "vendor", "*", "bin",
                                              "codex*"))):
        if os.path.basename(path) in ("codex", "codex.exe"):
            return path
    launcher = os.path.join(prefix, "node_modules", ".bin", "codex")
    return launcher if os.path.exists(launcher) else None


def install(version, root):
    prefix = os.path.join(root, f"npm-{version}")
    npm = shutil.which("npm")
    if not npm:
        return None, "no npm"
    try:
        p = subprocess.run([npm, "i", "--prefix", prefix, "--no-audit", "--no-fund", f"@openai/codex@{version}"],
                           capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return None, "npm i: did not finish within 600 s"
    if p.returncode:
        return None, f"npm i: {p.stderr.strip()[-300:]}"
    found = native(prefix)
    return (found, None) if found else (None, "no codex in the install")


def check(binary, root, timeout, only=None):
    """One version's row; a version that hangs is its own row ("did not answer"), the table goes on. only="pool" — the
    Codex pool's scheme alone."""
    run = readable_folder("run-", root)
    row, problems, rep = {"binary": binary, "version": "?"}, [], {}
    try:
        row["version"] = version_of(binary, run)
        if only != "pool":
            problems += schema_problems(binary, run)
            problems += feature_problems(binary, run)
            problems += hook_problems(binary, run)
            rep = live(binary, os.path.join(run, "live"), timeout)
            problems += rep.get("problems") or []
        problems += pool_problems(binary, os.path.join(run, "pool"), timeout)   # last: the rows above stand without it
    except subprocess.TimeoutExpired as e:   # what was found before the hang stays in the row
        problems.append(f"did not answer within {e.timeout} s:{' '.join(map(str, e.cmd))[-200:]}")
    except Exception as e:  # noqa: BLE001 — a binary that does not run, a schema folder without a file: this row only
        problems.append(f"{type(e).__name__}: {e}")
    row.update(problems=problems, ok=not [p for p in problems if not p.startswith(NOTE)], requests=rep.get("requests"),
               tools=rep.get("tools"))
    return row


def readable_folder(prefix, parent=None):
    """A new folder Codex's sandbox can read: Python 3.13+ on Windows makes mkdtemp's (mode 0o700) for its owner alone,
    and the unelevated sandbox's restricted token then reads nothing in it — the run's scripts, its `hub` (winstand,
    2026-11-05); a folder the hub makes for a session inherits its parent's access."""
    parent = parent or tempfile.gettempdir()
    for _ in range(100):
        path = os.path.join(parent, prefix + os.urandom(4).hex())
        try:
            os.mkdir(path, 0o755 if os.name == "nt" else 0o700)   # 0o700 is mkdtemp's ACL there
            return path
        except FileExistsError:
            continue
    raise FileExistsError(parent)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--versions", default=os.environ.get("HUB_CODEX_COMPAT_VERSIONS", ""))
    ap.add_argument("--codex", action="append", default=[], help="a codex binary to check as well")
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--keep", action="store_true", help="keep the run folder (what the fake model saw)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--only", choices=("pool",), help="only the Codex account pool's scheme")
    ap.add_argument("--child", nargs=3, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.child:
        return child(a.child[0], a.child[1], int(a.child[2]))
    from hosts.codex import codex_bin
    root = readable_folder("hub-codex-compat-")
    rows, binaries = [], list(a.codex)
    own = codex_bin()
    if shutil.which(own) or os.path.isfile(own):
        binaries.insert(0, shutil.which(own) or own)
    try:
        for v in [v.strip() for v in a.versions.split(",") if v.strip()]:
            found, err = install(v, root)
            if err:
                rows.append({"binary": f"@openai/codex@{v}", "version": v, "ok": False, "problems": [err]})
            else:
                binaries.append(found)
        for b in binaries:
            rows.append(check(b, root, a.timeout, a.only))
    except Exception as e:  # noqa: BLE001
        print(f"codex_compat: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    finally:
        if not a.keep:
            shutil.rmtree(root, ignore_errors=True)
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    else:
        for r in rows:
            print(f"{r['version']:>10}  {'ok' if r['ok'] else 'INCOMPATIBLE'}  {r['binary']}")
            for p in r["problems"]:
                print(f"            - {p}")
        if a.keep:
            print(f"run folder: {root}")
    return 0 if all(r["ok"] for r in rows) else 3


if __name__ == "__main__":
    sys.exit(main())
