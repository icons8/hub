"""Codex: bans and permissions of the codex host's
sessions (hosts/codex.py).

Seams the host calls:
- prepare_home(home, codex, env, args) — a Codex folder (the hub's; an account folder of the Codex pool) is
  created, config.toml written: its own files (hooks.json, trust in the hooks, the Windows sandbox); codex — the codex
  command, env — its process environment (without Claude's sign-in, that folder's CODEX_HOME), args — the folder's own
  app-server arguments (core/codexcli.args) — an account folder's is prepared in the host's background, not in the tick;
- features(**session) — Codex features (`--enable`) for this session's process;
  session — name, kind, mode, minihub, bus, executor_of, cwd, state_dir (the state folder), agent_file (own CLAUDE.md),
  home (its Codex folder: the hook's trust and the sandbox's readiness are that folder's);
- thread(params, **session) — the parameters of thread/start | thread/resume (mode, sandbox) before sending;
- info(proc) — the session's mode for session.info (from the thread's parameters);
- notice(proc, name, method, params) — a notification the host does not parse itself: the core's facts as a list;
- request(proc, name, rid, method, params) — a Codex request to the client: a permission (commandExecution,
  fileChange, permissions requestApproval; the v1 execCommandApproval, applyPatchApproval) — a card,
  permission.requested; None — not this slice's;
- answer_request(proc, method, data, response) — the owner's answer to such a card: the result to write to Codex.

Modes (slice H, probe 2026-11-05 on codex 0.162.1 with a fake Responses API): Claude Code's permission mode is Codex's
approval policy and sandbox (MODES). bypassPermissions — never ask, no sandbox, as before; default — every command
but the ones Codex knows to be safe reads, and every edit, is a card (untrusted); acceptEdits — edits in the folder and
commands in the sandbox (writes only in the folder and /tmp, no network) go without asking, going beyond it is a card
(on-request); plan — the same, read-only; dontAsk — read-only, and nothing asks (never). An approved command runs
outside the sandbox. "auto" (Claude's model reviewer; Codex has one, auto_review, on its own model) is not taken: it
was not checked and spends the hub's quota on every approval.

The core's bans are held by Codex's PreToolUse hook (Claude Code's format, probe 2026-10-19) — `hooks/deny_hint.py` on
every call: pkill/killall/pgrep and a long sleep — as with Claude; for a mini-hub — apply_patch edits in its folder, git
commit and push (the rules are core/minihub.deny, the session's file codex-rules.json). Codex does not run a hook from
CODEX_HOME until config.toml has its trusted_hash: the core takes the hash from Codex itself (hooks/list) — Codex computes
it its own way. The hook did not become trusted — sessions do not start: better no session than no bans. Codex's own
subagents (multi_agent) are off in config.toml (the core's own model calls, codexmodel); a session's process turns them on
on its command line, as Claude Code has Agent — not the bus's liaison (hosts/codex_items.features).
"""
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time

from core import langdata
from core.coretext import T
from hosts import codex_sandbox, codex_set

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(REPO, "hooks", "deny_hint.py")
ASK = "default_mode_request_user_input"   # the tool of a question to the owner (hosts/codex.FEATURES)
RULES = "codex-rules.json"                # the session's mini-hub rules for the hook: <state folder>/sessions/<name>/
# Claude Code's permission mode -> Codex's (approvalPolicy, sandbox); None — the hub's default, full access
MODES = {None: ("never", "danger-full-access"), "bypassPermissions": ("never", "danger-full-access"),
         "acceptEdits": ("on-request", "workspace-write"), "default": ("untrusted", "workspace-write"),
         "manual": ("untrusted", "workspace-write"), "plan": ("on-request", "read-only"),
         "dontAsk": ("never", "read-only")}
MODE_UNKNOWN = T("core.codex_rules.mode_unknown", "mode {mode}: Codex does not have it (there are bypassPermissions, "
                 "acceptEdits, default, manual, plan, dontAsk) — the session does not start")
DENIED = "Denied by the owner"            # the core's refusal text when the owner gave no reason (core/reducer.py)
# ...and its earlier form: a refusal an earlier core made is recognised too (not steered into the turn as a reason)
DEFAULT_DENIALS = (DENIED, langdata.legacy("owner_denied"))
TRUST_TIMEOUT = 20
RETRY = 5 * 60                            # not trusted — a new attempt at a session start at most once in RETRY s
                                          # (at once when codex is found where it was not: installed meanwhile),
RETRY_TIMEOUT = 5                         # and shorter: the start runs in hubd's tick
_TRUSTED = {}                             # the hub's Codex folder -> {"ok", "codex", "env", "at", "find"} (prepare_home)
_RETRYING = threading.Lock()              # trusted()'s checks again, one at a time
_FLAGS = {}                               # the hub's Codex folder -> the path of a thread config's hooks in its keys
# a hook of the trust check's own command line: hooks/list names the path a thread config's hooks have in this Codex
# (flags_path) — on Windows "C:\\<session-flags>\\config.toml", whatever the drive of the folder (live 2026-11-05)
FLAGS_PROBE = "hub-trust-key-probe"
MULTI_AGENT = re.compile(r"^multi_agent\s*=.*$", re.M)


def hooks_json():
    """The core's hook on every tool call: Bash and apply_patch come with tool_input.command. Matcher ".*", as in the
    probe of 2026-10-19: what Codex does with a regular expression in matcher was not checked live — an extra hook run
    is cheaper than a silently disabled ban."""
    import platforms
    hook = platforms.current.hook_command(sys.executable, HOOK)
    if hook.get("args"):   # Claude Code's exec form (Windows) — Codex does not know args
        line = _hook_line([hook["command"], *hook["args"]])
        if not line:
            print(f"codex: the core's hook cannot be written as a line without spaces ({sys.executable}, {HOOK}) — Codex "
                  "sessions will not start", file=sys.stderr, flush=True)
            return {"hooks": {"PreToolUse": []}}
        hook = {"type": "command", "command": line}
    return {"hooks": {"PreToolUse": [{"matcher": ".*", "hooks": [hook]}]}}


# a word of a hook line that both cmd /C "…" and PowerShell -Command "…" read as itself (Codex runs the line in the
# session's shell, PowerShell on Windows, or cmd /C when it has none; command_runner.rs); "~" — inside a word only (an 8.3
# name, PROGRA~1): at the start PowerShell reads it as the home folder
PLAIN = re.compile(r"[A-Za-z0-9_\-.:\\/][A-Za-z0-9_\-.:\\/~]*")


def _hook_line(words):
    """The hook as one command line for Codex on Windows, or None. Codex ignores Claude Code's `args` and ran the bare
    program — the hook "completed", the denied command ran (probe 2026-11-05). A quoted path does not parse in both
    shells (PowerShell needs `& "…"`, cmd does not), so every word is plain: a path with a space goes as its short 8.3
    name; if there is none — no line."""
    out = []
    for w in words:
        if not PLAIN.fullmatch(w):
            w = _short(w)
        if not w or not PLAIN.fullmatch(w):
            return None
        out.append(w)
    return " ".join(out)


def _short(path):
    """The 8.3 name of an existing Windows path (GetShortPathNameW), or None."""
    try:
        import ctypes
        get = ctypes.windll.kernel32.GetShortPathNameW
    except (ImportError, AttributeError):
        return None
    buf = ctypes.create_unicode_buffer(1024)
    n = get(path, buf, len(buf))
    return buf.value if 0 < n < len(buf) else None


def _features_off(text):
    """The host's config.toml with Codex's own subagents off (multi_agent): into the [features] table."""
    if MULTI_AGENT.search(text):
        return MULTI_AGENT.sub("multi_agent = false", text, count=1)
    if "[features]\n" in text:
        return text.replace("[features]\n", "[features]\nmulti_agent = false\n", 1)
    return text.rstrip("\n") + "\n\n[features]\nmulti_agent = false\n"


def _rpc(codex, env, home, calls, timeout, until=None, args=()):
    """`codex app-server` (no model) in home: initialize, then calls [(method, params)] -> (initialize's result, [the
    answer of each call — its message, None without one], [the notifications]); until(notification) — read on after
    the answers until it is true (a call that ends with a notification); args — more of its command line. No process —
    None."""
    try:
        p = subprocess.Popen(list(codex) + ["app-server", *codex_set.args(), *args], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1, cwd=home, env=env)
    except OSError as e:
        print(f"codex: app-server cannot be started: {e}", file=sys.stderr, flush=True)
        return None
    lines = queue.Queue()

    def read():
        try:
            for line in p.stdout:
                lines.put(line)
        except (OSError, ValueError):   # the stream was closed in finally while it was being read
            pass
        lines.put(None)
    threading.Thread(target=read, daemon=True).start()
    init, answers, notes = None, {}, []
    try:
        msgs = [{"id": 0, "method": "initialize", "params": {"clientInfo": {"name": "agents-hub", "version": "exp"}}},
                {"method": "initialized"}] + [{"id": i, "method": m, "params": a} for i, (m, a) in enumerate(calls, 1)]
        for msg in msgs:
            p.stdin.write(json.dumps(msg) + "\n")
        p.stdin.flush()
        end, done = time.time() + timeout, until is None
        # until the answers and the notification; a refused call ends the wait (its notification will not come)
        while len(answers) < len(calls) or not (done or any("error" in a for a in answers.values())):
            try:
                line = lines.get(timeout=max(0.0, end - time.time()))
            except queue.Empty:
                break
            if line is None:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if "method" in msg:
                notes.append(msg)
                done = done or until(msg)
            elif msg.get("id") == 0:
                init = msg.get("result")
            elif msg.get("id") in range(1, len(calls) + 1):
                answers[msg["id"]] = msg
    except OSError:
        pass
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
    return init, [answers.get(i) for i in range(1, len(calls) + 1)], notes


def _hooks_list(codex, env, home, timeout=TRUST_TIMEOUT, info=None, folder=()):
    """hooks/list of `codex app-server` (no model) -> the hook records; a failure — None. info — a dict that gets the
    platform of that Codex (initialize's platformFamily), its windowsSandbox/readiness (codex_sandbox) and the path
    of a thread config's hooks in its keys (FLAGS_PROBE); folder — the folder's own arguments (an account folder of the
    Codex pool: core/codexcli.args)."""
    calls = [("hooks/list", {"cwds": [home]})] + ([("windowsSandbox/readiness", {})] if info is not None else [])
    probe = ["-c", f'hooks.Stop=[{{hooks=[{{type="command",command="{FLAGS_PROBE}"}}]}}]'] if info is not None else []
    got = _rpc(codex, env, home, calls, timeout, args=[*folder, *probe])
    if got is None:
        return None
    init, answers, _ = got
    msg = answers[0]
    hooks = [h for d in ((msg or {}).get("result") or {}).get("data") or [] for h in d.get("hooks") or []]
    if info is not None:
        info["platform"] = (init or {}).get("platformFamily")
        info["readiness"] = ((answers[1] or {}).get("result") or {}).get("status")
        info["flags"] = next((str(h["key"]).rsplit(":", 3)[0] for h in hooks if h.get("source") == "sessionFlags"
                              and str(h.get("key", "")).count(":") >= 3), None)
    if msg is None:
        return None
    if "error" in msg:
        print(f"codex: hooks/list: {msg['error']}", file=sys.stderr, flush=True)
        return None
    return hooks


def flags_path(home):
    """The path a thread config's hooks have in the trust keys of the hub's Codex (from its own hooks/list at the
    trust check), None — not known (no check yet, a version without it): codex_set.SESSION_FLAGS is taken."""
    return _FLAGS.get(os.path.abspath(home))


def _ours(hooks, path):
    """The core's hook records in hooks/list's answer: Codex names the file by its real path (a CODEX_HOME through a
    link — macOS's TMPDIR /var → /private/var — is listed as the link's target), the core may know it by the link."""
    paths = (path, os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path)))
    return [h for h in hooks or [] if h.get("sourcePath") in paths
            or str(h.get("key", "")).startswith(tuple(p + ":" for p in paths))]


def prepare_home(home, codex=None, env=None, args=(), find=None):
    """The core's hooks.json, no multi_agent in config.toml and trust in its own hook (trusted_hash from hooks/list).
    find() — the codex command now (the host's: codex_bin at every use), for trusted()'s retries."""
    _TRUSTED[os.path.abspath(home)] = {"ok": False, "codex": codex, "env": env, "at": time.time(), "args": list(args),
                                       "find": find}
    path = os.path.join(home, "hooks.json")
    want = hooks_json()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(want, f, ensure_ascii=False)
    config = os.path.join(home, "config.toml")
    try:
        with open(config, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        text = ""
    new = _features_off(text)
    if new != text:   # CONFIG has it already (hosts/codex.write_config): an old file only
        with open(config, "w", encoding="utf-8") as f:
            f.write(new)
    if codex:
        info = {}
        _trust(home, codex, env, want, info=info, args=args)
        codex_sandbox.prepare(home, codex, env, info, args)   # Windows: Codex's own sandbox set up, or the sandboxed refused
    return None


def _trust(home, codex, env, want, timeout=TRUST_TIMEOUT, info=None, args=()):
    path, config = os.path.join(home, "hooks.json"), os.path.join(home, "config.toml")
    hooks = _ours(_hooks_list(codex, env, home, timeout, info=info, folder=args), path)
    if info is not None and info.get("flags"):   # every check, the retry too; an unanswered one keeps the known path
        _FLAGS[os.path.abspath(home)] = info["flags"]
    # the key is the file path (with "\\" on Windows): a TOML string in JSON form, where "\\" is escaped
    add = [f'\n[hooks.state.{json.dumps(h["key"])}]\ntrusted_hash = {json.dumps(h["currentHash"])}\n' for h in hooks
           if h.get("trustStatus") != "trusted" and h.get("key") and h.get("currentHash")]
    if add:
        with open(config, encoding="utf-8") as f:
            text = f.read()
        for h in hooks:   # the previous record of the same hook (a retry) goes: TOML does not accept two tables with one name
            text = text.replace(f'\n[hooks.state.{json.dumps(h.get("key"))}]\n', "\n[hooks.state.stale]\n")
            text = re.sub(r"\n\[hooks\.state\.stale\]\ntrusted_hash = [^\n]*\n", "", text)
        with open(config, "w", encoding="utf-8") as f:
            f.write(text + "".join(add))
        hooks = _ours(_hooks_list(codex, env, home, timeout, folder=args), path)
    ok = listed_trusted(hooks, home)   # hooks.json was written from `want` above
    find = (_TRUSTED.get(os.path.abspath(home)) or {}).get("find")
    _TRUSTED[os.path.abspath(home)] = {"ok": ok, "codex": codex, "env": env, "at": time.time(), "args": list(args),
                                       "find": find, "there": _there(codex)}
    if not ok:
        print(f"codex: Codex does not trust the core's hook ({[h.get('trustStatus') for h in hooks]}) — sessions will not start",
              file=sys.stderr, flush=True)
    return ok


def listed_trusted(hooks, home):
    """The core's hook of the Codex folder home is run by Codex: hooks/list's records (all the Codex lists) hold every hook of
    the folder's hooks.json, each trusted — for a process that did not prepare the folder itself (prepare_home keeps this
    in memory, trusted()): the core's own model call asks Codex itself. No file, an empty one, a hook Codex does not list or
    does not trust (changed, a new Codex hashes differently) — False."""
    try:
        with open(os.path.join(home, "hooks.json"), encoding="utf-8") as f:
            want = json.load(f)
        expected = sum(len(g.get("hooks") or []) for groups in want["hooks"].values() for g in groups)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False
    ours = _ours(hooks, os.path.join(home, "hooks.json"))
    return expected > 0 and len(ours) == expected and all(h.get("trustStatus") == "trusted" for h in ours)


def trusted(state_dir, now=None, home=None):
    """The core's hook is trusted in home (the hub's Codex folder by default; an account folder of the Codex pool has its
    own trust — the key is its hooks.json path). Not — one more attempt (hooks/list is sometimes slow on the first start
    after an update), but at most once in RETRY: a session start must not wait for Codex on every round — and at once
    when the codex command is another now or its program is there now and was not (find: Codex CLI installed after
    the host came up). A trusted folder whose codex command is another now is checked again with it, at once."""
    if not state_dir:
        return False
    home = os.path.abspath(home or os.path.join(state_dir, "codex-home"))
    t = _TRUSTED.get(home)
    if not t:
        return False
    now = time.time() if now is None else now
    if not t["codex"]:
        return t["ok"]
    codex = _found(t)
    changed = codex != list(t["codex"])
    if t["ok"] and not changed:
        return True
    # another codex now (installed after the start; the hub's own copy put in place): its trust is checked before a
    # session runs it — a trusted hook is trusted by that Codex's hash, not by any
    new = changed or not t.get("there", True) and _there(codex)
    if new or now - t["at"] >= RETRY:
        with _RETRYING:   # two spawns at once: one check, the other reads its outcome (config.toml is written once)
            done = _TRUSTED.get(home)
            if done is not t and done and list(done["codex"]) == codex:   # checked meanwhile with this codex
                return done["ok"]
            info = {}
            # a codex just put in place gets the start's wait once (its first app-server is slower): a miss is RETRY away
            ok = _trust(home, codex, t["env"], hooks_json(), TRUST_TIMEOUT if new else RETRY_TIMEOUT, info=info,
                        args=t.get("args") or ())
            codex_sandbox.retried(home, info, codex)   # the same run's platform and readiness: no second app-server for them
        return ok
    return t["ok"]


def _there(codex):
    """The program of a codex command is on disk (a path) or in PATH (a name)."""
    exe = (list(codex or []) or [""])[0]
    return bool(exe) and (os.path.isfile(exe) if os.path.dirname(exe) else bool(shutil.which(exe)))


def _found(t):
    """The codex command of a trust record now: its find() (the host's search at every use), else the one it had."""
    try:
        found = t["find"]() if callable(t.get("find")) else None
    except Exception as e:  # noqa: BLE001 — a search that failed keeps the command the record had
        print(f"codex: the codex command cannot be found anew: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        found = None
    return list(found) if found else list(t["codex"])


def features(default, **session):
    """Codex features of a session. The core's hook is not trusted — the start is refused (the bans would not hold); a
    mode Codex cannot hold (MODES) — refused too, not silently full access. A mini-hub's executor does not ask the
    owner (the bus, core/minihub.py) — no question tool."""
    if session.get("mode") not in MODES:
        raise RuntimeError(MODE_UNKNOWN.format(mode=session["mode"]))
    if not trusted(session.get("state_dir"), home=session.get("home")):
        raise RuntimeError("the bans do not hold: Codex does not trust the core's hook — the session does not start "
                           "(hubd log)")
    # the liaison's thread is workspace-write (hosts/codex_liaison.thread), a mode's — its pair in MODES
    liaison = session.get("liaison")
    why = codex_sandbox.refusal(session.get("state_dir"), "workspace-write" if liaison else MODES[session.get("mode")][1],
                                "the liaison" if liaison else f"mode {session.get('mode')}", home=session.get("home"))
    if why:
        raise RuntimeError(why)
    return tuple(f for f in default if not (f == ASK and session.get("bus")))


def thread(params, **session):
    """The session's mode as Codex's approval policy and sandbox (MODES); the mini-hub rules go to the hook as a file,
    and so does the mark of the bus's liaison (its guard, hooks/liaison_guard.py). The file is written for every session
    (empty for a non-mini-hub): without it the hook refuses everything (hooks/deny_hint.py)."""
    state_dir, name = session.get("state_dir"), session.get("name")
    if state_dir and name:
        path = os.path.join(state_dir, "sessions", name, RULES)
        deny = []
        if session.get("minihub"):
            from core import minihub
            deny = minihub.deny(session.get("cwd"), keep=(session.get("agent_file"), state_dir))
        # the repo's Claude Code permission rules: the hook reads these files on every call (an edit holds at once,
        # as Claude Code watches them); the liaison has no repo
        from hooks import repo_rules   # not at import: the hosts package imports without hooks/ (smoke step)
        cwd = session.get("cwd")
        sources = session.get("sources", repo_rules.SOURCES)   # the hub's --setting-sources, as claude's
        repo = {} if session.get("liaison") or not cwd else {"settings": repo_rules.files(cwd, sources),
                                                              "root": os.path.abspath(cwd)}
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"deny": deny, **({"liaison": True} if session.get("liaison") else {}), **repo}, f,
                      ensure_ascii=False)
        os.replace(path + ".tmp", path)
    policy, sandbox = MODES[session.get("mode")]
    out = {**params, "approvalPolicy": policy, "sandbox": sandbox}
    if (policy, sandbox) == ("on-request", "workspace-write") and state_dir:
        # acceptEdits runs commands in the sandbox without asking: `hub …` puts its facts into the core's spool, outside
        # the folder — writable, else every hub command fails. Not for untrusted (default): it asks for every such
        # command anyway, and an approved one runs outside the sandbox
        out["config"] = {**(params.get("config") or {}),
                         "sandbox_workspace_write": {"writable_roots": [spool_root(state_dir)]}}
    return out


def spool_root(state_dir):
    """The core's spool as a writable root of Codex's sandbox: the real path (a sandbox compares real paths), made
    beforehand — Codex cannot create a writable root inside the read-only rest (probe 2026-11-05: `hub gist` — EROFS)."""
    spool = os.path.join(os.path.realpath(state_dir), "spool")
    os.makedirs(spool, exist_ok=True)
    return spool


def info(proc):
    """session.info's mode: what the thread was started with (the host writes bypassPermissions itself). manual is
    default's other name; the liaison's sandbox is no mode of Claude Code's: bypassPermissions, as its claude reports."""
    params = getattr(proc, "start_params", None) or {}
    pair = (params.get("approvalPolicy"), params.get("sandbox"))
    return {"mode": next((m for m, p in MODES.items() if m and p == pair), "bypassPermissions")}


def notice(proc, name, method, params):
    """The plan of a Codex turn (update_plan) is the "working on" line, like TodoWrite with Claude: the inProgress step; the same one — silence."""
    if method != "turn/plan/updated":
        return []
    now = next((s.get("step") for s in params.get("plan") or [] if isinstance(s, dict) and s.get("status") == "inProgress"
                and s.get("step")), None)
    if not now or getattr(proc, "plan_now", None) == (params.get("turnId"), now):   # a new turn — the line anew
        return []
    proc.plan_now = (params.get("turnId"), now)
    return [("session.recap", name, {"text": " ".join(str(now).split())[:160], "by": "plan"})]


APPROVALS = ("item/commandExecution/requestApproval", "item/fileChange/requestApproval",
             "item/permissions/requestApproval", "execCommandApproval", "applyPatchApproval")
LEGACY = ("execCommandApproval", "applyPatchApproval")   # the v1 protocol: decisions approved | denied


def _command(params):
    """The command as the model wrote it: Codex wraps it (`/bin/bash -lc '…'`), the parsed action has it bare."""
    cmd = params.get("command")
    if isinstance(cmd, list):   # v1: argv — quoted as a shell would read it
        import shlex
        return shlex.join(str(c) for c in cmd)
    acts = [a.get("command") for a in params.get("commandActions") or [] if isinstance(a, dict) and a.get("command")]
    return acts[0] if len(acts) == 1 else cmd


TOOLS = {"item/commandExecution/requestApproval": "Bash", "execCommandApproval": "Bash",
         "item/permissions/requestApproval": "Permissions"}   # the card's tool; the rest are Edit


def _card(proc, method, params):
    """(tool, input) of the card: what the page and Telegram show, as Claude Code's Bash, Edit."""
    if TOOLS.get(method) == "Bash":
        inp = {"command": _command(params), "cwd": params.get("cwd")}
        for key in ("networkApprovalContext", "additionalPermissions"):
            if params.get(key):
                inp[key] = params[key]
        return "Bash", inp
    if method == "applyPatchApproval":
        changes = params.get("fileChanges") if isinstance(params.get("fileChanges"), dict) else {}
        return "Edit", {"file_path": next(iter(changes), None),
                        "changes": [{"path": p, "kind": (c or {}).get("type")} for p, c in changes.items()]}
    if method == "item/permissions/requestApproval":
        asked = params.get("permissions") if isinstance(params.get("permissions"), dict) else {}
        return "Permissions", {"permissions": {k: v for k, v in asked.items() if v}, "cwd": params.get("cwd")}
    # a fileChange approval carries only its step's id; the paths and the diff came in that step's item/started
    item = (getattr(proc, "items", None) or {}).get(params.get("itemId")) or {}
    changes = [c for c in item.get("changes") or [] if isinstance(c, dict)]
    if not changes:
        return "Edit", {"item": params.get("itemId")}
    inp = {"file_path": changes[0].get("path"),
           "changes": [{"path": c.get("path"), "kind": (c.get("kind") or {}).get("type")} for c in changes],
           "diff": "\n".join(str(c.get("diff") or "") for c in changes)}
    if params.get("grantRoot"):
        inp["grantRoot"] = params["grantRoot"]
    return "Edit", inp


def request(proc, name, rid, method, params):
    """A permission request (a mode other than full access, MODES): a card; the turn waits for the owner. With the
    setting "allow confirmation requests by itself" (permissions.auto_allow) the core answers yes itself, as with Claude:
    the host marks the request (proc.host — hosts/codex.py). Not in a read-only mode (plan), but for its `hub …`
    commands."""
    if method not in APPROVALS:
        return None
    from core import redact
    from hosts.streamjson import _clip
    proc.asks[f"cx-{rid}"] = (rid, method, params)
    tool, inp = _card(proc, method, params)
    host = getattr(proc, "host", None)
    # read-only (plan): a request is to go beyond the mode at all — the owner's, like Claude's ExitPlanMode; only a
    # plain `hub …` command (the spool is outside the read-only sandbox) is still the core's to allow by the setting
    from hooks.liaison_guard import plain_hub
    read_only = (getattr(proc, "start_params", None) or {}).get("sandbox") == "read-only" \
        and not (tool == "Bash" and plain_hub(inp.get("command"), commands=None))
    data = {"request_id": f"cx-{rid}", "tool": tool, "interactive": False, "input": _clip(inp),
            "why": redact.mask_head(str(params.get("reason") or ""), 300) or None}
    if host is not None and not read_only and (host.auto_allow_on() or _repo_allows(proc, method, params)):
        data["auto"] = "allow"
    return [("permission.requested", name, data)]


def _repo_allows(proc, method, params):
    """The repo's allow rules cover the request (hooks/repo_rules.py), and no deny or ask rule meets it: Claude Code
    would not ask, so the core answers yes itself. Judged on what Codex will run — its command, every path and a move's
    target — not on the card's summary; a grant beyond the command (grantRoot, additionalPermissions, network) is
    always the owner's. The thread's folder is the project root."""
    from hooks import repo_rules
    root = (getattr(proc, "start_params", None) or {}).get("cwd")
    if not root or any(params.get(k) for k in ("grantRoot", "additionalPermissions", "networkApprovalContext")):
        return False
    rules = repo_rules.load(repo_rules.files(root, getattr(proc, "sources", repo_rules.SOURCES)))
    if not rules["allow"]:
        return False
    if TOOLS.get(method) == "Bash":
        script = _script(params.get("command"))
        hit = script and repo_rules.bash(script, rules, root, params.get("cwd") or root)
    elif method == "applyPatchApproval":
        changes = params.get("fileChanges") if isinstance(params.get("fileChanges"), dict) else {}
        hit = repo_rules.paths("edit", [*changes, *[(c or {}).get("move_path") for c in changes.values()
                                                    if isinstance(c, dict)]], rules, root)
    elif method == "item/fileChange/requestApproval":
        item = (getattr(proc, "items", None) or {}).get(params.get("itemId")) or {}
        changes = [c for c in item.get("changes") or [] if isinstance(c, dict)]
        hit = repo_rules.paths("edit", [*(c.get("path") for c in changes),
                                        *((c.get("kind") or {}).get("move_path") for c in changes
                                          if isinstance(c.get("kind"), dict))], rules, root)
    else:
        return False
    return bool(hit) and hit[0] == "allow"


SHELLS = ("bash", "sh", "zsh")


def _script(command):
    """The shell line Codex will run: a v1 argv quoted; `/bin/bash -lc '<line>'` — the line; otherwise as it is."""
    import shlex
    if isinstance(command, list):
        words = [str(c) for c in command]
    elif isinstance(command, str):
        try:
            words = shlex.split(command)
        except ValueError:
            return command
    else:
        return None
    if len(words) == 3 and os.path.basename(words[0]) in SHELLS and words[1] in ("-c", "-lc"):
        return words[2]
    return shlex.join(words) if isinstance(command, list) else command


def answer_request(proc, method, data, response):
    """The owner's answer as Codex's decision. A refusal goes on with the turn (decline, not cancel), as with Claude; his
    reason — Codex's decision has no text for it — is steered into the running turn (the core's default text is not:
    Codex already tells the model it was rejected)."""
    if method not in APPROVALS:
        return None
    allow = response.get("behavior") == "allow"
    why = str(response.get("message") or "").strip()
    # a subagent's request (its own thread): Codex tells it of the refusal; the reason steered into the session's
    # thread would start a turn there that the subagent never sees
    asker, thread = (data or {}).get("threadId"), getattr(proc, "thread", None)
    own = not asker or not thread or asker == thread
    if not allow and why and why not in DEFAULT_DENIALS and own:
        tool = TOOLS.get(method, "Edit")
        proc.backlog.append([{"type": "text", "text": f"Permission request denied ({tool}): {why}"}])
        host = getattr(proc, "host", None)
        if host is not None:
            host._flush(proc)
    if method in LEGACY:
        return {"decision": "approved" if allow else "denied"}
    # "decline" even where availableDecisions lists only accept and cancel (untrusted): codex 0.162.1 takes it and goes
    # on with the turn (probe 2026-11-05); cancel would interrupt the turn
    if method == "item/permissions/requestApproval":
        return {"permissions": ((data or {}).get("permissions") or {}) if allow else {}, "scope": "turn"}
    return {"decision": "accept" if allow else "decline"}
