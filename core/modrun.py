"""A module process and calls to it (Module API, spec section 2.1): a module is its own process, the core calls it
over local HTTP with a secret and a time budget; if there is no answer, it is wrong or the process crashed, the core
does its own thing and the module is suspended.

Who does what:
- hubd (Supervisor) decides which modules must run: the graph (core/modules.py) plus their state in the journal; it
  puts a file `<state>/modrun/<name>.want.json` {cmd, cwd, version, order, budget, key}; removing it means the process
  is not needed (disabled, blocked, stopped after failures);
- hub-up keeps the process `mod:<name>` per these files (launch): it issues a port and a new secret for each launch
  and writes `<state>/modrun/<name>.run.json` {pid, port, secret, launch, started} (0600); if the process exits, a
  `module.error` fact (exited) and a relaunch after a pause;
- hubd calls `/hook/hello` on a new launch and writes `module.ready` {version, launch, points}; no answer within
  HELLO_WAIT or a wrong one gives `module.error` and blocked at once, dependents too;
- anyone (hubd, the host, the page, the hub command) calls a module with one function, Modules.call: the module must
  be ready in this launch, not suspended and declare the point; the answer is checked against the point's schema
  (POINTS); a failure gives None, `module.error` and a core decision.
Module state is a fold of module.* facts (apply) into state["modules"], one for all processes; the key appears only
with the first such fact, so a hub without modules keeps exactly the state it had before. The notification about
suspension, stop and blocked comes only from the fold, on a transition, one per transition (the owner_notice action
is executed by hubd).
One process crash is one failure: until hub-up notices the exit, each call to it is a connection failure (connect),
and then an exit too (exit); failures of these kinds in one launch (launch) within a minute count once toward the
pause, a repeat is not written (CRASH, CRASH_SPAN).
"""
import copy
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import uuid

import platforms
from core import i18n, modules, spool, version
from core.coretext import T
from core.tools import Rejected

STOPPED_REFUSAL = T("core.modrun.stopped", "module {name} is stopped after failures — hub modules reset {name}")

FAILS, WINDOW, PAUSE, MAX_SUSPENSIONS = 3, 300, 600, 5   # 3 failures in 5 min: a 10 min pause; the 5th pause in a row: stop
CALM = 3600           # a pause that starts later than this after the previous one ended resets the pause count: a slow
#                       minute at each hub start is not "pauses in a row"; a module failing again after each pause is
SLOW_AFTER = 120      # a point's miss counts as "slow" (no step to a pause) only this long after its good answer
START_WINDOW = 15     # the process exited before becoming ready: "did not start", blocked at once
HELLO_WAIT = 15       # how long to wait for a new process to start accepting connections
HELLO_EVERY = 0.5     # hello attempts while the process is not listening
HELLO_CONNECT = 0.5   # hello connection timeout: not listening means "still coming up", the core tick does not stall
BUDGET = 2.0          # default call budget, seconds
MAX_ANSWER = 1 << 20  # a bigger answer is a failure
RUN_DIR = "modrun"
PLAN = "plan.json"    # {core}: hubd checked the want files in this life of its own; hub-up raises modules only after that
CORE_ENV = "HUB_UP_CORE"   # the core life mark from hub-up: hubd writes it into PLAN
LOCAL_TTL = 60        # the process's own failure is stricter than the journal for this many seconds
RECORDS_EVERY = 20    # a process with no journal reader of its own rereads module state no more often (less than LOCAL_TTL)
STOPPED = ("error", "blocked")   # the process is not needed: hub-up stops it
# the process crashed: failures of these kinds in one launch within CRASH_SPAN s of the first are one crash, one
# failure counted toward pauses (hub-up notices the exit within a second, busy stopping another part, within a
# minute). closed (the request went out, no answer: crashed mid-call, or a live process cut it off) always counts, but
# CRASH failures of the same launch right after it do not.
# A live process that stopped accepting connections is a failure once per CRASH_SPAN: a pause within a few minutes
CRASH, CRASH_SPAN = ("connect", "exit"), 60
# session launch calls launch.env and prompt.parts: a module with these capabilities that has not yet answered hello
# is waited for (after a hub restart module processes come up later than sessions), no longer than LAUNCH_WAIT from
# the first deferred raise
LAUNCH_CAPS = ("launch.env", "prompt")   # and the login module named in modules.use.profile (core/profiles.py)
LAUNCH_WAIT = HELLO_WAIT + BUDGET + 5   # hello of a new process + a margin for hub-up to raise it
# developer mode (modules.dev, a module from a folder): a file edit restarts it, the call budget is long (debugger),
# failures do not accumulate toward a pause; fold and actions keep the core pass budget (core/corewrite.py), hello has its own
DEV_BUDGET = 60.0
DEV_SCAN = 1.0        # scan dev module folders no more often than this, seconds; an edit counts from the 2nd same scan
DEV_FILES = 5000      # no more files in the fingerprint: a huge folder must not stall the core tick
DEV_SKIP_DIRS = {"__pycache__", "node_modules", "venv", "env", "dist", "build", "target"}
DEV_SKIP_FILES = (".pyc", ".pyo", ".log", ".tmp", ".swp", ".swx", ".db", ".sqlite", ".sqlite3", "-journal", "-wal",
                  "-shm", "~")
LOG_TAIL = 4096       # tail of a dev module's log from which the crash reason is taken
LOG_HEAD = "--- hub: "   # a core line in a dev module's log (launch) is not a crash reason
LOG_MAX = 4 << 20     # a bigger dev module log is cut by hub-up (trim_logs): the old one goes to .1, writing restarts
_logs = {}            # hub-up: module -> the open log of its launch (a file object shared with the process, trim_logs)


class BadResult(ValueError):
    pass


def _number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _hello(res, ctx):
    """hello: {name, version, points}: name and version as in module.json; points from this API version, with capabilities."""
    if not (isinstance(res, dict) and isinstance(res.get("name"), str) and isinstance(res.get("version"), str)
            and isinstance(res.get("points", []), list) and all(isinstance(p, str) for p in res.get("points", []))):
        raise BadResult("hello: {name, version, points: [points]}")
    if res["name"] != ctx["name"] or res["version"] != ctx["version"]:
        raise BadResult(f"hello: {res['name']} {res['version']}, but module.json says {ctx['name']} {ctx['version']}")
    known = set(callable_points())
    bad = [p for p in res.get("points", []) if p not in known]
    if bad:
        raise BadResult(f"hello: this hub does not call the points {', '.join(bad)} (module API {version.MODULE_API})")
    if ctx.get("api"):   # points newer than the version the module was written for (the lower bound of api in module.json)
        own = set(offered(ctx["api"]))
        newer = [p for p in res.get("points", []) if p not in own]
        if newer:
            since = next(v for v, pts in version.MODULE_POINTS.items() if newer[0] in pts)
            raise BadResult(f"hello: the point {newer[0]} exists since module API {since}, but the module's api is \"{ctx['api']}\" — "
                            f"set ^{since} in module.json")
    caps = set(ctx.get("capabilities") or [])
    need = [p for p in res.get("points", []) if (POINTS.get(p) or {}).get("cap") and POINTS[p]["cap"] not in caps]
    if need:
        raise BadResult("hello: " + ", ".join(f"the point {p} needs the capability {POINTS[p]['cap']}" for p in need)
                        + " in capabilities of module.json")
    return {"version": res["version"], "points": sorted(set(res.get("points", [])))}


def _prompt_parts(res, ctx):
    """prompt.parts: {parts: [{name, text}]} is the whole new list of prompt-addendum parts (core/modprompt.py); names
    are non-empty and unique, the text length is not limited (only the whole answer is, MAX_ANSWER)."""
    got = res.get("parts") if isinstance(res, dict) else None
    if not isinstance(got, list) or not all(isinstance(p, dict) and isinstance(p.get("name"), str) and p["name"].strip()
                                            and len(p["name"]) <= 64 and isinstance(p.get("text"), str) for p in got):
        raise BadResult("prompt.parts: {parts: [{name: a name of up to 64 characters, text: a string}]}")
    names = [p["name"] for p in got]
    if len(set(names)) != len(names):
        raise BadResult("prompt.parts: part names repeat")
    return [{"name": p["name"], "text": p["text"]} for p in got]


def _prompt_input(res, ctx):
    """prompt.input: {text} is the text the session will receive; an empty one is a failure (the message would go
    empty and the core would count it delivered)."""
    if not (isinstance(res, dict) and isinstance(res.get("text"), str) and res["text"].strip()):
        raise BadResult("prompt.input: {text: a non-empty string}")
    return res["text"]


# --- launch.env, cli, doctor, migrate (core/modlaunch.py, core/modcli.py) ------------------------------------------

LAUNCH_PREFIXES = ("ANTHROPIC_", "CLAUDE_")   # names a module sets for a session without an env list in module.json
# claude flags that change what it loads (settings and their hooks, MCP, plugins, agents, the system prompt), what it
# may do (permissions, tools, folders), which conversation it is and how it talks to the core; compared without "-"
# and "_", in lower case
ARGS_DENIED = {"settings", "settingsources", "mcpconfig", "strictmcpconfig", "plugindir", "dangerouslyskippermissions",
               "allowdangerouslyskippermissions", "permissionmode", "permissionprompttool", "allowedtools",
               "disallowedtools", "tools", "adddir", "appendsystemprompt", "appendsystempromptfile", "systemprompt",
               "systempromptfile", "systempromptsnapshot", "agents", "agent", "resume", "continue", "forksession",
               "sessionid", "print", "inputformat", "outputformat", "includepartialmessages", "replayusermessages",
               "verbose", "init", "initonly", "maintenance", "ide", "chrome"}
MAX_ARGS, MAX_ARG = 32, 500


def _strmap(v, what):
    if not (isinstance(v, dict) and all(isinstance(k, str) and isinstance(x, str) for k, x in v.items())):
        raise BadResult(f"{what}: an object of strings")
    return v


def launch_keys(env, ctx):
    """What a module may set for a session: ANTHROPIC_*, CLAUDE_*, its own HUB_MOD_<NAME>_* and names from the env of
    module.json, except those that change which code runs (modules.env_denied). The rest is dropped (a filter, not a
    failure)."""
    allowed, name = set(ctx.get("env") or []), ctx.get("name")
    return {k: v for k, v in _strmap(env, "launch.env: env").items()
            if modules.ENV_NAME.fullmatch(k) and not modules.env_denied(k, name)
            and (k.startswith(LAUNCH_PREFIXES) or k in allowed or k.startswith(modules.env_prefix(name or "")))}


def _launch_args(args):
    """Extra claude arguments: few, short, only long flags and their values, none from ARGS_DENIED. A short flag can
    glue several together (-pc), and "--" ends the flags, so neither is allowed."""
    if not (isinstance(args, list) and all(isinstance(a, str) for a in args)):
        raise BadResult("launch.env: args — a list of strings")
    if len(args) > MAX_ARGS or any(len(a) > MAX_ARG for a in args):
        raise BadResult(f"launch.env: up to {MAX_ARGS} arguments of {MAX_ARG} characters each")
    if args and not args[0].startswith("--"):   # it would stick as a value to the core's last flag (--mcp-config is a list)
        raise BadResult("launch.env: args start with a --name flag")
    for a in args:
        if a.startswith("-") and (not a.startswith("--") or a == "--" or
                                  a[2:].split("=", 1)[0].replace("-", "").replace("_", "").lower() in ARGS_DENIED):
            raise BadResult(f"launch.env: {a.split('=', 1)[0]} is not an argument a module may add to claude")
    return args


def _launch_env(res, ctx):
    """launch.env: {env?, args?}: the environment (an empty string removes a variable) and claude arguments."""
    if not isinstance(res, dict) or set(res) - {"env", "args"}:
        raise BadResult("launch.env: {env, args}")
    out = {}
    if "env" in res:
        out["env"] = launch_keys(res["env"], ctx)
    if "args" in res:
        out["args"] = _launch_args(res["args"])
    return out


def _cli(res, ctx):
    """cli: {out?, err?, code?}: what to print and which code to exit with; {stdin: true} (2.8) means the command
    needs stdin (a secret, not as an argument): the core reads it and calls again with the stdin field."""
    if res == {"stdin": True}:
        return {"stdin": True}
    if not (isinstance(res, dict) and not set(res) - {"out", "err", "code"}
            and all(isinstance(res.get(k, ""), str) for k in ("out", "err"))
            and isinstance(res.get("code", 0), int) and not isinstance(res.get("code", 0), bool)
            and 0 <= res.get("code", 0) <= 255):
        raise BadResult("cli: {out, err, code 0–255}")
    return {"out": res.get("out", ""), "err": res.get("err", ""), "code": res.get("code", 0)}


MAX_CHECKS = 50


def _doctor(res, ctx):
    """doctor: [{name, ok, detail?}]: the module's checks for hub doctor."""
    if not (isinstance(res, list) and len(res) <= MAX_CHECKS and all(
            isinstance(c, dict) and isinstance(c.get("name"), str) and c["name"] and isinstance(c.get("ok"), bool)
            and isinstance(c.get("detail", ""), str) for c in res)):
        raise BadResult(f"doctor: [{{name, ok: true|false, detail}}], up to {MAX_CHECKS}")
    return [{"name": c["name"][:80], "ok": c["ok"], "detail": c.get("detail", "")[:500]} for c in res]


def _migrate(res, ctx):
    """migrate: {} means done; {error: "…"} means it failed (the module is not enabled, the version stays)."""
    if not isinstance(res, dict):
        raise BadResult("migrate: {} or {error}")
    if res.get("error"):
        raise BadResult(f"migrate: {str(res['error'])[:300]}")
    return res


# Points the core calls on a module: path /hook/<point>, budget is the default seconds (the manifest may lower it),
# cap is the capability without which a module cannot declare the point, check(answer, ctx) validates the answer
# against the schema or raises BadResult. Module API slices add their own points here (and to version.MODULE_POINTS).
def _corewrite(check):
    def run(res, ctx):
        from core import corewrite
        return getattr(corewrite, check)(res, ctx)
    return run


def _profiles(check):
    def run(res, ctx):
        from core import profiles
        try:
            return getattr(profiles, check)(res, ctx)
        except profiles.BadAnswer as e:
            raise BadResult(str(e)) from None
    return run


def _codexpool(check):
    def run(res, ctx):
        from core import codexslot
        pool = codexslot.code()
        if pool is None:   # a tree without the pool's code never offers its points (callable_points)
            raise BadResult("codex.*: this hub has no Codex pool")
        try:
            return getattr(pool, check)(res, ctx)
        except pool.BadAnswer as e:
            raise BadResult(str(e)) from None
    return run


SAY_SECONDS = 600   # a voice message cannot be longer (Telegram: sendVoice)


def _speech_say(res, ctx):
    """speech.say: {path, seconds, said} is a ready voice message (OGG/Opus); {error} means it failed, the reason as
    text; {} means nothing (no voice message). The module names the path: what the file is, the facade checks
    (modules/voice/speech.py)."""
    if res == {}:
        return {}
    if isinstance(res, dict) and set(res) == {"error"} and isinstance(res["error"], str) and res["error"].strip():
        return {"error": res["error"][:300]}
    if not (isinstance(res, dict) and set(res) == {"path", "seconds", "said"} and isinstance(res["path"], str)
            and os.path.isabs(res["path"]) and len(res["path"]) <= 4096 and _number(res["seconds"])
            and 0 < res["seconds"] <= SAY_SECONDS and isinstance(res["said"], str)):
        raise BadResult("speech.say: {path: an absolute path, seconds: 0–600, said: a string}, {error: a reason} or {}")
    return {"path": res["path"], "seconds": res["seconds"], "said": res["said"][:20000]}


HOLD_REASON = 200   # the longest reason of a hold, characters: it is a line in the queue, not a report


def _task_admit(res, ctx):
    """task.admit: {} lets the executor's launch through, {hold: a reason} keeps the request in the queue with the reason
    (core/daemon.admit_pass). The reason is one non-empty line of text; a longer one is cut."""
    if res == {}:
        return {}
    if not (isinstance(res, dict) and set(res) == {"hold"} and isinstance(res["hold"], str) and res["hold"].strip()):
        raise BadResult("task.admit: {hold: a reason as a non-empty string} or {}")
    return {"hold": " ".join(res["hold"].split())[:HOLD_REASON]}


POINTS = {
    "hello": {"budget": BUDGET, "cap": None, "check": _hello},
    # session launch waits for a module longer than usual; a human waits for a command; data migration is not a hot path
    "launch.env": {"budget": 5.0, "cap": "launch.env", "check": _launch_env},
    "cli": {"budget": 10.0, "cap": "cli", "check": _cli},
    "doctor": {"budget": BUDGET, "cap": "doctor", "check": _doctor},
    "migrate": {"budget": 10.0, "cap": None, "check": _migrate},
    "prompt.parts": {"budget": 1.0, "cap": "prompt", "check": _prompt_parts},   # hot path: every session launch
    "prompt.input": {"budget": 1.0, "cap": "prompt", "check": _prompt_input},   # and every message
    # core.write (core/corewrite.py): a decision on an event before it is written and an edit of that write's actions: a hot path
    "fold": {"budget": 1.0, "cap": "core.write", "check": _corewrite("fold_answer")},
    "actions": {"budget": 1.0, "cap": "core.write", "check": _corewrite("actions_answer")},
    # session login (core/profiles.py): list and decision in the daemon tick, token before claude launches, use is a human
    "profile.list": {"budget": 2.0, "cap": "profile", "check": _profiles("check_list")},
    "profile.decide": {"budget": 2.0, "cap": "profile", "check": _profiles("check_decide")},
    "profile.token": {"budget": 5.0, "cap": "profile", "check": _profiles("check_token")},
    "profile.use": {"budget": 2.0, "cap": "profile", "check": _profiles("check_use")},
    # the Codex account pool (an embed's, core/codexslot.py): answers from the module's memory — a measurement never runs inside a
    # question (the module's own thread); add makes a folder
    "codex.list": {"budget": 2.0, "cap": "profile", "check": _codexpool("check_list")},
    "codex.decide": {"budget": 2.0, "cap": "profile", "check": _codexpool("check_decide")},
    "codex.use": {"budget": 2.0, "cap": "profile", "check": _codexpool("check_use")},
    "codex.add": {"budget": 5.0, "cap": "profile", "check": _codexpool("check_add")},
    "codex.remove": {"budget": 2.0, "cap": "profile", "check": _codexpool("check_remove")},
    # reply voice (modules/voice/speech.py): text compression and voice take up to a minute, the bot waits in the background
    "speech.say": {"budget": 150.0, "cap": "speech", "check": _speech_say},
    # admission of an executor's launch (core/daemon.admit_pass): asked in the daemon tick, so a short budget; a miss never holds
    "task.admit": {"budget": 2.0, "cap": "task.admit", "check": _task_admit},
}


def callable_points():
    """Points this hub calls on modules (except hello): declared by the API version and implemented (POINTS); the Codex
    pool's only where the tree has its code (core/codexslot.py — the open flavor has none)."""
    from core import codexslot
    pool = codexslot.present()
    return [p for p in version.module_points() if p in POINTS and p != "hello" and (pool or p not in version.CODEX_POINTS)]


def offered(api=None):
    """Points the hub calls on a module with an api range: implemented by the hub and not newer than the lower bound of
    api, and the optional ones (version.OPTIONAL_POINTS: a module of a lower floor may declare them — the hello request's
    points tell it they are there)."""
    if not api:
        return callable_points()
    own = set(version.module_points(version.module_floor(api))) | set(version.OPTIONAL_POINTS)
    return [p for p in callable_points() if p in own]


def manifest_of(state_dir, name):
    """module.json of a module hubd orders to keep (the folder comes from its want), or {}."""
    w = wants(state_dir).get(name) or {}
    try:
        with open(os.path.join(w["cwd"], "module.json"), encoding="utf-8-sig") as f:
            man = json.load(f)
    except (KeyError, TypeError, OSError, ValueError):
        return {}
    return man if isinstance(man, dict) else {}


def budget_of(point):
    return (POINTS.get(point) or {}).get("budget", BUDGET)


# --- module state: a fold of module.* facts (reducer) ----------------------------------------------------------------

def fold(rec, data, ts, lang=None):
    """The module's record after one failure at time ts; lang is the language of the reason text kept in the record
    (the state's: i18n.state_lang)."""
    rec = {"state": "ok", "errors": [], "suspensions": 0, "until": None, **copy.deepcopy(rec or {})}
    kind = str(data.get("kind") or "")[:20]
    last = {"hook": str(data.get("hook") or "")[:60], "kind": kind, "error": str(data.get("error") or "")[:300],
            "ts": ts}
    if kind == "slow":
        # a point missed its budget right after its good answer (Modules.fail): a loaded machine, not a stuck module.
        # It counts toward no pause and does not hide the real failure in last
        rec["slow"] = last
        rec.setdefault("last", last)
        return rec
    rec["last"] = last
    if kind == "exit":
        if data.get("launch") and rec.get("ready") == data.get("launch"):
            rec.pop("ready")   # a ready process crashed: do not call until a new hello
        elif _number(data.get("uptime")) and data["uptime"] < START_WINDOW:
            kind = "start"     # exited without becoming ready: did not start
    if kind in ("late", "shadow"):
        # late means not ready for a session raise (core/daemon.wait_modules): raise order, not a failure; shadow is a
        # divergence from the built-in one in shadow mode (core/providers.py): the core already returned to the
        # built-in one, so it does not count toward pauses
        return rec
    if kind in ("start", "hello", "migrate"):
        if rec["state"] != "error":
            rec["state"], rec["until"] = "blocked", None
            rec["reason"] = i18n.tr(lang, {"start": "core.modrun.reason_start", "hello": "core.modrun.reason_hello",
                                           "migrate": "core.modrun.reason_migrate"}[kind], error=rec["last"]["error"])
            rec.pop("ready", None)
        return rec
    if rec["state"] in STOPPED or (rec["state"] == "suspended" and ts < (rec["until"] or 0)):
        return rec   # already stopped, or the call was in flight when the module was suspended
    launch = str(data.get("launch") or "")[:40]
    if kind in CRASH + ("closed",) and launch:
        if kind in CRASH and same_crash(rec, launch, ts):
            return rec   # the same crash: calls before hub-up noticed the exit, and the exit itself, are one failure
        rec["crash"] = {"launch": launch, "ts": ts}
    if data.get("dev"):
        rec["errors"] = []   # developer mode: the failure is visible in hub modules, does not accumulate toward a pause
        return rec
    errors = [t for t in rec["errors"] if t > ts - WINDOW] + [ts]
    if len(errors) >= FAILS:
        if rec["until"] is not None and ts - rec["until"] > CALM:
            rec["suspensions"] = 0   # a calm spell after the previous pause: bad minutes over hours do not accumulate
        rec["suspensions"] += 1
        errors = []
        rec["state"], rec["until"] = (("error", None) if rec["suspensions"] >= MAX_SUSPENSIONS
                                      else ("suspended", ts + PAUSE))
    rec["errors"] = errors
    return rec


def same_crash(rec, launch, now):
    """A failure of launch at time now is the same crash already counted in the record rec."""
    crash = (rec or {}).get("crash")
    return (isinstance(crash, dict) and crash.get("launch") == str(launch or "")[:40] and launch
            and now - (crash.get("ts") or 0) < CRASH_SPAN)


def status(rec, now):
    """ok | suspended | error | blocked: whether the module may be called at time now."""
    rec = rec or {}
    if rec.get("state") in STOPPED:
        return rec["state"]
    if rec.get("state") == "suspended" and now < (rec.get("until") or 0):
        return "suspended"
    return "ok"


STRICT = ("ok", "suspended", "blocked", "error")


def _notice(name, before, rec, lang=None):
    """The text for the owner on a transition (in the language lang), or None."""
    if (rec["state"], rec["suspensions"]) == (before.get("state", "ok"), before.get("suspensions", 0)):
        return None
    last = rec["last"]
    if rec["state"] == "error":
        return i18n.tr(lang, "core.modrun.notice_error", name=name, hook=last["hook"], error=last["error"])
    if rec["state"] == "suspended" and rec["suspensions"] == 1:
        return i18n.tr(lang, "core.modrun.notice_suspended", name=name, count=FAILS, window=WINDOW // 60,
                       hook=last["hook"], error=last["error"], pause=PAUSE // 60)
    if rec["state"] == "blocked":
        return i18n.tr(lang, "core.modrun.notice_blocked", name=name, reason=rec["reason"])
    return None


def apply(st, ev, data, actions):
    """Part of the fold: module.error (a failure, fold), module.ready (hubd: the process answered hello), module.claims,
    module.model (the page: a model call spend, only a fact), module.reset (the owner or the hub clears a stop).
    The modules key appears only with the first such fact."""
    from core import guards
    kind = ev["type"]
    if kind == "module.provider":   # about a function, not one module (module is who provides it, may be absent)
        from core import providers
        return providers.apply(st, ev, data)
    name = str(data.get("module") or "")
    if not modules.NAME.fullmatch(name) or ev.get("entity") not in (None, name):
        raise Rejected("module event: data.module is the module name")
    if kind == "module.error":
        recs = st.setdefault("modules", {})
        before = recs.get(name) or {}
        lang = i18n.state_lang(st)   # the language of the state, not of the hubd process
        recs[name] = fold(before, data, ev["ts"], lang)
        text = _notice(name, before, recs[name], lang)
        if text and not data.get("dev"):   # dev module: failures go to hub modules and its log, no notifications
            actions.append({"do": "owner_notice", "session": None,
                            "title": i18n.tr(lang, "core.modrun.notice_title", name=name), "text": text,
                            "id": f"module:{name}"})
    elif kind == "module.ready":
        if data.get("channel") != "core":
            raise Rejected("the core writes a module's readiness")
        rec = (st.get("modules") or {}).get(name) or {}
        if rec.get("state") == "error":
            raise Rejected(STOPPED_REFUSAL.format(name=name))
        rec = st.setdefault("modules", {}).setdefault(name, {})
        if rec.get("state") == "blocked":
            rec["state"] = "ok"
            rec.pop("reason", None)
        (st.get("notices") or {}).pop(f"n:core:module:{name}", None)   # it came up: "did not come up" is cleared (owner_notice)
        rec.update(ready=str(data.get("launch") or "")[:40], version=str(data.get("version") or "")[:40],
                   points=[str(p)[:40] for p in data.get("points") or []][:64])
    elif kind == "module.claims":   # hubd: what the module did not get from the paths and keys of the snapshot: a history line
        if data.get("channel") != "core":
            raise Rejected("the core writes what a module did not get")
    elif kind == "module.model":   # the page: a model call spent by a module, a line in "Spending" (core/usage.py)
        if ev.get("src") != "web" or data.get("channel") not in (None, "web"):   # modmodel.answer has no channel
            raise Rejected("the hub page writes the cost of a model call made by a module")
    elif kind == "module.reset":
        # the owner from his channels or a hub session; not a plugin or module process (channel plugin): the generic
        # from_hub_or_owner lets through any channel except a session
        if guards.from_owner_or_hub(st, data):
            raise Rejected(T("core.modrun.reset_by_owner",
                             "a module's stop is lifted by the owner or by the hub on their word"))
        if name not in (st.get("modules") or {}):
            raise Rejected(T("core.modrun.reset_nothing", "module {name} did not fail — nothing to lift")
                           .format(name=name))
        rec = st["modules"][name]
        rec.update(state="ok", errors=[], suspensions=0, until=None)
        rec.pop("reason", None)
        from core import providers
        providers.unhold(st, name)   # and a provider candidate again after a shadow divergence
    else:
        raise Rejected("module events: module.error, module.ready, module.claims, module.model, module.reset, "
                       "module.provider")


def restartable(st, name):
    """Why `hub restart mod:<name>` cannot be done, or None. A module that never answered and never failed is not
    recorded in the state: its process is still coming up, or there is no such module."""
    rec = (st.get("modules") or {}).get(name)
    if rec is None:
        return T("core.modrun.not_up_yet", "module {name} has not come up yet — what is with it: hub modules") \
            .format(name=name)
    if rec.get("state") == "error":
        return T("core.modrun.stopped_restart", "module {name} is stopped after failures — to turn it on again: "
                 "hub modules reset {name}").format(name=name)
    return None


def retry(st, name):
    """A new load (core.started, hub restart mod:<name>): blocked means try again. True if it was blocked."""
    rec = (st.get("modules") or {}).get(name)
    if not rec or rec.get("state") != "blocked":
        return False
    rec["state"] = "ok"
    rec.pop("reason", None)
    return True


def started(st):
    """core.started: the hub came up again; modules that did not come up last time try again."""
    for name in list(st.get("modules") or {}):
        retry(st, name)


# --- launch files ---------------------------------------------------------------------------------------------------

def run_dir(state_dir):
    return os.path.join(state_dir, RUN_DIR)


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            got = json.load(f)
        return got if isinstance(got, dict) else None
    except (OSError, ValueError):
        return None


def _write(path, data, private=False):
    """Atomically; private means 0600 (the launch secret). The replace goes through platforms (Windows: retry while
    the file is being read)."""
    from platforms import current
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        current.replace(tmp, path)
    except BaseException:   # a full disk, or a reader holds the file past the retries: the content (a secret, if
        _drop(tmp)          # private) does not stay in .tmp
        raise


def _drop(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def wants(state_dir):
    """{name: want}: which modules hubd orders to keep (hub-up, Modules)."""
    try:
        names = sorted(os.listdir(run_dir(state_dir)))
    except OSError:
        return {}
    out = {}
    for n in names:
        if n.endswith(".want.json"):
            w = _read(os.path.join(run_dir(state_dir), n))
            if w and modules.NAME.fullmatch(n[:-len(".want.json")]) and isinstance(w.get("cmd"), list):
                out[n[:-len(".want.json")]] = w
    return out


def planned_by(state_dir):
    """The life mark of the core that last checked the want files (PLAN), or None."""
    return (_read(os.path.join(run_dir(state_dir), PLAN)) or {}).get("core")


def halted(state_dir, name):
    """hub-up stopped the module process itself (disabled, blocked): its launch is gone, so the launch file goes."""
    _drop(os.path.join(run_dir(state_dir), f"{name}.run.json"))
    _close_log(name)


def run_of(state_dir, name):
    """The module process's current launch {pid, port, secret, launch, started}, or None."""
    return _read(os.path.join(run_dir(state_dir), f"{name}.run.json"))


def slice_path(state_dir, name):
    """A slice of hub state for a module process (modstate.slice): written by hubd, read by hubmod."""
    return os.path.join(run_dir(state_dir), f"{name}.state.json")


def slice_of(state_dir, name):
    """The slice for module name written by hubd, or None if there is no file (hubd has not written yet) or it is unreadable."""
    got = _read(slice_path(state_dir, name))
    ok = got is not None and isinstance(got.get("mods"), dict) and isinstance(got.get("others"), int) \
        and isinstance(got.get("ready"), (str, type(None)))
    return got if ok else None


def log_of(state_dir, name):
    """A dev module's log: its process output (the last launch, at most LOG_MAX; the earlier piece in .1)."""
    return os.path.join(state_dir, "logs", f"mod-{name}.log")


def trim_logs():
    """hub-up, once per tick: if the launch log is bigger than LOG_MAX, its tail (up to LOG_MAX bytes, to the current
    end) goes to <log>.1 (the previous .1 is dropped), and the log starts over. The process writes to the same file
    object (inherited): on POSIX with O_APPEND it writes to the end, that is, to the start of the truncated file; on
    Windows its position is moved by seek right here (the object's position is shared), and the truncation follows
    it (truncate without a number): a line that slips in between seek and truncation stays before the core's line,
    with no hole of zeros. A line that slips in between the copy and the truncation is lost: it is a log, not a
    journal. A module writing an error in a loop used to bloat the log without limit per launch (~17 MB/day)."""
    for name, (path, log) in list(_logs.items()):
        try:
            if os.fstat(log.fileno()).st_size <= LOG_MAX:
                continue
            _tail_copy(path, path + ".1")
            log.seek(0)
            log.truncate()
            log.write(f"{LOG_HEAD}{time.strftime('%Y-%m-%d %H:%M:%S')} log cut at {LOG_MAX >> 20} MB, "
                      f"the previous one is {os.path.basename(path)}.1\n".encode())
            log.flush()
        except (OSError, ValueError):   # the file was removed or closed: on the next tick or launch
            pass


def _tail_copy(src, dst):
    """The last LOG_MAX bytes of src (to its current end) into dst in chunks, not all into memory."""
    with open(src, "rb") as f, open(dst + ".tmp", "wb") as out:
        size = os.fstat(f.fileno()).st_size
        f.seek(max(0, size - LOG_MAX))
        left = min(size, LOG_MAX)
        while left > 0:
            chunk = f.read(min(left, 1 << 20))
            if not chunk:
                break
            out.write(chunk)
            left -= len(chunk)
    platforms.current.replace(dst + ".tmp", dst)


def _close_log(name):
    """The process exited or was stopped: hub-up no longer holds its log."""
    got = _logs.pop(name, None)
    if got:
        try:
            got[1].close()
        except OSError:
            pass


def _log_tail(state_dir, name):
    """The last lines of a dev module's log: the crash reason for hub modules; "" if there is no log."""
    try:
        with open(log_of(state_dir, name), "rb") as f:
            f.seek(max(0, os.fstat(f.fileno()).st_size - LOG_TAIL))
            lines = [x.strip() for x in f.read().decode("utf-8", "replace").splitlines()
                     if x.strip() and not x.startswith(LOG_HEAD)]
    except OSError:
        return ""
    return " | ".join(lines[-2:])[-240:]


def files_of(folder):
    """Files of a dev module's folder {path: (size, mtime)}, without hidden ones, caches, data and logs (the module
    writes them itself: otherwise it would restart itself) and at most DEV_FILES; the second value says the list was cut."""
    out = {}
    for d, dirs, names in os.walk(folder):
        dirs[:] = sorted(x for x in dirs if x not in DEV_SKIP_DIRS and not x.startswith("."))
        for f in sorted(names):
            if f.startswith(".") or f.endswith(DEV_SKIP_FILES):
                continue
            try:
                st = os.stat(os.path.join(d, f))
            except OSError:
                continue
            out[os.path.relpath(os.path.join(d, f), folder).replace(os.sep, "/")] = (st.st_size, st.st_mtime_ns)
            if len(out) >= DEV_FILES:
                return out, True
    return out, False


def key_of(mark):
    """The key of a want: it changes with the command, the folder, the version and the manifest (hub-up restarts a
    module by it; the page reads a page plugin's file once per key)."""
    return hashlib.sha1(json.dumps(mark, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def stamp_of(files):
    return hashlib.sha1(json.dumps(sorted(files.items())).encode()).hexdigest()[:16]


def git_ignored(folder, rels):
    """Which of the paths a git repository folder ignores (.gitignore; tracked ones never): module data in a working
    copy is not an edit. Not a repository, no git, or the folder itself ignored (it lies in an ignored directory of an
    outer repo) gives nothing."""
    if not rels:
        return set()
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    try:
        r = subprocess.run(["git", "-C", folder, "check-ignore", "--stdin", "-z"], input="\0".join([".", *rels]) + "\0",
                           capture_output=True, text=True, timeout=2, env=env)
    except (OSError, subprocess.SubprocessError):
        return set()
    got = {x for x in r.stdout.split("\0") if x} if r.returncode == 0 else set()
    return set() if "." in got else got


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PYTHON_WORDS = ("python3", "python")


def command(cmd):
    """What hub-up runs for a module's run: a first word `python3` or `python` means the hub's own Python, in the same
    UTF-8 mode as the hub parts (on Windows `python3` is often the store stub, and a server may not have it); the
    rest is as in module.json. want and its key stay as in the manifest."""
    cmd = list(cmd)
    if cmd and cmd[0] in PYTHON_WORDS:
        return [sys.executable, *(["-X", "utf8"] if sys.flags.utf8_mode else []), *cmd[1:]]
    return cmd


def launch(state_dir, name, want, spawn, env=None):
    """hub-up: start a module process per want. spawn(cmd, cwd, env) -> process. The port and secret are new for each
    launch; the launch file comes before the process (the module cannot answer before the core knows the secret)."""
    port, secret, launch_id = _free_port(), uuid.uuid4().hex + uuid.uuid4().hex, uuid.uuid4().hex[:12]
    os.makedirs(run_dir(state_dir), exist_ok=True)
    from core import modlaunch
    # hub-up from a session: the module is not that session (bin/hub: OUTSIDER); claude login keys do not go to the
    # module, the core gives it a model
    env = {k: v for k, v in (env if env is not None else os.environ).items()
           if k not in ("HUB_SESSION", "HUB_EXTENSION") and not modlaunch.login_name(k)}
    env = {**env, "HUB_STATE_DIR": state_dir, "HUB_MODULE": name,
           "HUB_MODULE_PORT": str(port), "HUB_MODULE_SECRET": secret, "HUB_MODULE_LAUNCH": launch_id}
    path = os.path.join(run_dir(state_dir), f"{name}.run.json")
    run = {"pid": None, "port": port, "secret": secret, "launch": launch_id, "started": time.time()}
    _write(path, run, private=True)
    if want.get("dev"):   # developer mode: the process output goes to its own log, the crash reason to hub modules
        logged = log_of(state_dir, name)
        os.makedirs(os.path.dirname(logged), exist_ok=True)
        _close_log(name)
        with open(logged, "wb") as log:
            log.write(f"{LOG_HEAD}{time.strftime('%Y-%m-%d %H:%M:%S')} launch {launch_id}\n".encode())
        # in append mode (O_APPEND for the process too) and kept open by hub-up: trim_logs cuts the log without
        # restarting the process
        log = open(logged, "ab")
        try:
            proc = spawn(command(want["cmd"]), want.get("cwd"), env, stdout=log, stderr=subprocess.STDOUT)
        except BaseException:
            log.close()
            raise
        _logs[name] = (logged, log)
    else:
        proc = spawn(command(want["cmd"]), want.get("cwd"), env)
    _write(path, {**run, "pid": proc.pid}, private=True)
    return proc


def exited(state_dir, name, code, uptime, error=None):
    """hub-up: the module process exited by itself (or did not start: error); the launch file goes (nobody to call)
    and a module.error fact with no channel: the core channel on a mailbox file is a forgery of the core (core/admit.py)."""
    run = run_of(state_dir, name) or {}
    _drop(os.path.join(run_dir(state_dir), f"{name}.run.json"))
    _close_log(name)
    dev = bool((wants(state_dir).get(name) or {}).get("dev"))
    error = error or f"the process exited with code {code}"
    tail = _log_tail(state_dir, name) if dev else ""
    spool.put(os.path.join(state_dir, "spool"), "module.error", name,
              {"module": name, "hook": "process", "kind": "exit", "error": error + (f": {tail}" if tail else ""),
               "launch": run.get("launch"), "uptime": round(uptime, 1), **_dev({"dev": dev})},
              src="hub-up")


# --- call -----------------------------------------------------------------------------------------------------------

def post(port, secret, path, payload, budget, connect=None):
    """POST http://127.0.0.1:<port><path> with X-Hub-Secret, wait no longer than budget. -> (None, JSON answer) or
    (kind of failure, text): connect | closed | timeout | status | size | schema. connect means the connection was not
    accepted (and has its own connection timeout: hello of a new process, since on Windows a refusal on a port
    without a listener arrives only after ~2 s; that is "not listening yet", not silence); closed means the request
    went out but the connection was cut or closed without an answer."""
    deadline = time.monotonic() + budget
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    head = (f"POST {path} HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\nX-Hub-Secret: {secret}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode()
    chunks, size = [], 0
    try:
        try:
            s = socket.create_connection(("127.0.0.1", int(port)), timeout=min(budget, connect or budget))
        except socket.timeout:
            if connect:
                return "connect", "the process is not accepting connections yet"
            raise
        except OSError as e:
            return "connect", f"the process does not accept connections ({type(e).__name__})"
        with s:
            s.settimeout(max(0.001, deadline - time.monotonic()))
            s.sendall(head + body)
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    return "timeout", f"no answer within {budget:g} s"
                s.settimeout(left)
                chunk = s.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_ANSWER + 65536:
                    return "size", f"the answer is larger than {MAX_ANSWER >> 20} MB"
    except socket.timeout:
        return "timeout", f"no answer within {budget:g} s"
    except OSError as e:
        return "closed", f"the connection was cut ({type(e).__name__})"
    raw = b"".join(chunks)
    if not raw:
        return "closed", "the process closed the connection without an answer"
    top, sep, rest = raw.partition(b"\r\n\r\n")
    lines = top.split(b"\r\n")
    parts = lines[0].split() if lines and lines[0] else []
    if not sep or len(parts) < 2 or not parts[0].startswith(b"HTTP/"):
        return "schema", "the answer is not HTTP"
    if parts[1] != b"200":
        return "status", f"answer {parts[1].decode('latin-1')[:3]}"
    for line in lines[1:]:
        k, _, v = line.partition(b":")
        if k.strip().lower() == b"content-length" and v.strip().isdigit():
            if len(rest) < int(v.strip()):
                return "schema", "the answer is cut off"
            rest = rest[:int(v.strip())]
    if len(rest) > MAX_ANSWER:
        return "size", f"the answer is larger than {MAX_ANSWER >> 20} MB"
    try:
        return None, json.loads(rest.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return "schema", "the answer is not JSON"


def _journal_records(state_dir):
    """Module records now (state.json + the journal tail), for processes without a journal reader of their own (the hub command)."""
    from core import journal
    try:
        return journal.load_state(state_dir).get("modules") or {}
    except (OSError, ValueError):
        return {}


class Modules:
    """Module calls from one process. records() is this process's state["modules"] (hubd its own state, the page and
    the bot their own journal reader, the command state.json); emit(type, entity, data) writes a fact to the journal
    (by default the core's mailbox). The process folds its own failure itself too (local): it stops calling the module
    at once without waiting for the journal, and takes the stricter of the two states while the journal has not caught
    up (LOCAL_TTL after its own last failure); hubd writes to the journal itself (local=False)."""

    def __init__(self, state_dir, records=None, emit=None, clock=time.time, local=True):
        self.state_dir, self.clock, self.fold_local = state_dir, clock, local
        if records is None:
            cache = {"at": None, "recs": {}}

            def records():   # state.json + journal tail, no more often than RECORDS_EVERY: the fold is not free
                if cache["at"] is None or self.clock() - cache["at"] > RECORDS_EVERY:
                    cache["at"], cache["recs"] = self.clock(), _journal_records(state_dir)
                return cache["recs"]
        self.records = records
        # written by the core (the page, hub <module command>), not by a module: the mailbox treats src module as a
        # module's fact (spool.outsider)
        self.emit = emit or (lambda t, e=None, d=None: spool.put(os.path.join(state_dir, "spool"), t, e, d,
                                                                   src="modrun"))
        self.local = {}
        self.good = {}   # (module, point) -> (launch, when) of the point's last answer in this process; gone at a failure

    def status(self, name):
        now = self.clock()
        own = self.local.get(name)
        if own and now - own["last"]["ts"] > LOCAL_TTL:
            own = None   # the journal already knows these failures (and the owner's reset after them)
        a, b = status((self.records() or {}).get(name), now), status(own, now)
        return max(a, b, key=STRICT.index)

    def ready(self, name, wanted=None):
        """The module's current launch if it is ready (answered hello in this launch) and may be called; otherwise None.
        wanted is the already read wants()."""
        rec = (self.records() or {}).get(name) or {}
        run = run_of(self.state_dir, name)
        if name not in (wanted if wanted is not None else wants(self.state_dir)):
            return None   # disabled or blocked: hub-up stops it
        if not run or not rec.get("ready") or rec["ready"] != run.get("launch") or self.status(name) != "ok":
            return None
        return run

    def answering(self, point):
        """Names of ready modules that declared the point, in load order."""
        recs, wanted = self.records() or {}, wants(self.state_dir)
        order = sorted(wanted.items(), key=lambda x: (x[1].get("order", 0), x[0]))
        return [n for n, _ in order if point in ((recs.get(n) or {}).get("points") or []) and self.ready(n, wanted)]

    def declared(self, point, cap=None):
        """Names of modules whose last hello declared the point: those ordered kept, in load order — ready or not yet (a
        module coming up after a hub restart or paused still declares it), then those the core stopped after failures
        (STOPPED: not ordered kept, yet nobody removed them); one that was removed, switched off or whose new version
        dropped the point does not. cap: also one ordered kept with that right that has not answered hello yet (its
        first start)."""
        recs, wanted = self.records() or {}, wants(self.state_dir)
        order = [n for n, _ in sorted(wanted.items(), key=lambda x: (x[1].get("order", 0), x[0]))]
        order += sorted(n for n, r in recs.items() if n not in wanted and isinstance(r, dict) and r.get("state") in STOPPED)
        return [n for n in order if point in ((recs.get(n) or {}).get("points") or [])
                or cap and "points" not in (recs.get(n) or {}) and cap in ((wanted.get(n) or {}).get("capabilities") or [])]

    def call(self, name, point, payload, ctx=None):
        """Call a module's point: an answer per the point's schema, or None if the module is not ready, did not declare
        the point, is suspended or failed (then module.error). ctx is what the answer check needs (name, version,
        module capabilities)."""
        spec, wanted = POINTS[point], wants(self.state_dir)
        run = self.ready(name, wanted)
        if run is None or point not in ((self.records() or {}).get(name) or {}).get("points", []):
            return None
        return self._call(name, point, payload, run, ctx, spec, wanted[name])

    def _call(self, name, point, payload, run, ctx, spec, want):
        budget = DEV_BUDGET if want.get("dev") else min(spec.get("budget", BUDGET),
                                                         (want.get("budget") or {}).get(point) or BUDGET * 1000)
        kind, value = post(run["port"], run["secret"], f"/hook/{point}", payload, budget)
        if not kind:
            try:
                got = spec["check"](value, {"name": name, **(ctx or {})})
                self.good[(name, point)] = (run.get("launch"), self.clock())
                return got
            except BadResult as e:
                kind, value = "schema", str(e)
        self.fail(name, point, kind, value, run.get("launch"))
        return None

    def crashed(self, name, kind, launch):
        """This launch's crash is already counted (by its own fold or the journal): a repeat is not written."""
        if kind not in CRASH or not launch:
            return False
        now = self.clock()
        return any(same_crash(rec, launch, now) for rec in (self.local.get(name), (self.records() or {}).get(name)))

    def fail(self, name, point, kind, error, launch=None):
        """A call failure is a module.error fact. False means not written: this launch's crash is already counted.
        The first missed budget of a point that was answered by the same launch of the module within SLOW_AFTER seconds
        is written as "slow": it is in the module's record (slow) and counts toward no pause. A point that misses again
        with no answer between (a stuck module), a new launch or an old answer count as before: a poll every few
        seconds on a loaded machine must not add isolated slow answers up to a ten minute pause and a stop.
        Per-call processes (the hub command, a watchdog) have no memory of answers: their misses count as before."""
        answered = self.good.pop((name, point), None)
        if self.crashed(name, kind, launch):
            return False
        if kind == "timeout" and answered and launch and answered[0] == launch and self.clock() - answered[1] < SLOW_AFTER:
            kind = "slow"
        data = {"module": name, "hook": point, "kind": kind, "error": str(error)[:500], "launch": launch}
        data.update(_dev(wants(self.state_dir).get(name)))
        if self.fold_local:
            self.local[name] = fold(self.local.get(name), data, self.clock())
        self.emit("module.error", name, data)
        return True


# --- hubd: which modules to keep and their readiness ----------------------------------------------------------------

def _dev(want):
    """A mark of a dev module's failure (the fold does not accumulate it toward a pause and sends no notification)."""
    return {"dev": True} if (want or {}).get("dev") else {}


class Supervisor:
    """Part of hubd. reload(cfg, catalog) on a settings reread: the module graph; tick() on every tick: want files per
    the graph and module state, hello for new launches. Without modules it writes nothing and creates no folder."""

    def __init__(self, state_dir, state, record, clock=time.time):
        self.state_dir, self.state, self.clock = state_dir, state, clock   # state() is hubd's state right now
        self.mods = Modules(state_dir, records=lambda: self.state().get("modules") or {},
                            emit=lambda t, e=None, d=None: record(t, e, d), clock=clock, local=False)
        self.record = record
        self.found, self.cfg, self.catalog = None, {}, None   # found None means modules were never read yet
        self.written = None       # the last thing written to want (None: not yet compared with the disk)
        self.written_static = None   # the same for the modules without a process (.static.json)
        self.lost, self.told_lost = {}, {}   # what modules did not get from the snapshot paths and keys; what is in the journal
        self.hellos = {}          # name -> {launch, first, tried, done}: hello of the current launch
        # dev modules: the fingerprint in want, the files behind it, new files (waiting for a second scan), when to
        # scan again, who was already told that the folder exceeds DEV_FILES
        self.stamps, self.files, self.dev_pending, self.next_scan, self.told = {}, {}, {}, None, set()
        self.published = None     # name -> slice written as a file in this hubd life (publish); None: not written yet

    def reload(self, cfg, catalog):
        self.cfg, self.catalog = cfg, set(catalog or {})
        try:
            self.found = modules.discover(cfg, self.state_dir)
        except Exception as e:  # noqa: BLE001 — foreign module files do not bring the core down
            print(f"hubd: modules not read — {type(e).__name__}: {e}", flush=True)

    def plan(self):
        recs = self.state().get("modules") or {}
        blocked = {n: r.get("reason") or i18n.t("core.modrun.stopped_default", name=n) for n, r in recs.items()
                   if r.get("state") in STOPPED}
        return modules.resolve(self.found, self.cfg, catalog=self.catalog, blocked=blocked)

    def want(self, plan):
        from core import modclaims
        out = {}
        devs = self.devs()
        claims, self.lost = modclaims.resolve(plan, self.cfg)   # own snapshot paths and keys (Module API 2.4)
        for r in plan:
            if r["state"] != "ok" or modules.processless(r["manifest"]):
                continue
            man = r["manifest"]
            w = {"name": r["name"], "cmd": list(man["run"]), "cwd": r["dir"], "version": r["version"],
                 "order": r["order"], "budget": man.get("budget") or {},
                 "capabilities": list(man.get("capabilities") or []), "api": man.get("api")}
            mark = [w["cmd"], w["cwd"], w["version"], man]
            if devs.get(r["name"]) == r["dir"]:   # an edit changes the fingerprint, the fingerprint is the key: hub-up restarts it
                if r["name"] not in self.stamps:
                    self.files[r["name"]] = files_of(r["dir"])[0]
                    self.stamps[r["name"]] = stamp_of(self.files[r["name"]])
                w["dev"] = True
                mark.append(self.stamps[r["name"]])
            w["key"] = key_of(mark)
            if r["name"] in claims:   # not in the key: a path dispute got resolved, the process is not restarted
                w["claims"] = claims[r["name"]]
            out[r["name"]] = w
        return out

    def static_want(self, plan):
        """Modules without a process: nothing to start, hub-up does not see them; the core serves their page plugin. The key
        changes with the version and the manifest (the page plugin's file is read once per key)."""
        out = {}
        for r in plan:
            if r["state"] == "ok" and modules.processless(r["manifest"]):
                mark = [r["dir"], r["version"], r["manifest"]]
                out[r["name"]] = {"name": r["name"], "cwd": r["dir"], "version": r["version"], "order": r["order"],
                                  "api": r["manifest"].get("api"),
                                  "key": key_of(mark)}
        return out

    def devs(self):
        return modules.dev_of(self.cfg, self.found or [])

    def watch(self):
        """Edits in dev module folders: new files that are the same on two scans in a row (the editor finished writing)
        go into want (a restart); module.json is reread; a module that did not come up (blocked) tries again. If only
        files changed that the folder's git ignores, it is not an edit. A scan takes at most ~5% of a tick: a long one
        is run less often."""
        now = self.clock()
        if self.found is None or (self.next_scan is not None and now < self.next_scan):
            return
        began, devs, changed = time.monotonic(), self.devs(), []
        for name in set(self.stamps) - set(devs):
            for d in (self.stamps, self.files, self.dev_pending):
                d.pop(name, None)
        for name, folder in devs.items():
            if name not in self.stamps:   # first scan (want removes them itself, but not for a module that is already blocked)
                self.files[name] = files_of(folder)[0]
                self.stamps[name] = stamp_of(self.files[name])
                continue
            files, cut = files_of(folder)
            if cut and name not in self.told:
                self.told.add(name)
                print(f"hubd: dev module {name}: the folder holds more than {DEV_FILES} files — later edits are not seen", flush=True)
            if files == self.files[name]:
                self.dev_pending.pop(name, None)
            elif self.dev_pending.get(name) != files:
                self.dev_pending[name] = files
            else:
                old = self.files[name]
                self.files[name] = self.dev_pending.pop(name)
                diff = sorted(k for k in set(old) | set(files) if old.get(k) != files.get(k))
                if set(diff) - git_ignored(folder, diff):
                    self.stamps[name] = stamp_of(files)
                    changed.append(name)
        self.next_scan = now + max(DEV_SCAN, 20 * (time.monotonic() - began))
        if not changed:
            return
        print(f"hubd: modules in developer mode changed — {', '.join(changed)}", flush=True)
        self.reload(self.cfg, self.catalog)
        recs = self.state().get("modules") or {}
        retry = [f"mod:{n}" for n in changed if (recs.get(n) or {}).get("state") == "blocked"]
        if retry:   # did not start: again after an edit (like hub restart mod:<name>)
            self.record("hub.restart_requested", None, {"parts": retry, "channel": "core", "owner": False})

    def sync(self):
        """The want files follow the plan; extra ones go. Only the first module creates the folder."""
        from core import modpages
        if self.found is None:
            return {}   # not read (the reread failed): do not touch the want files by an empty plan
        self.lost = {}
        plan = self.plan() if self.found else []
        want, static = self.want(plan), self.static_want(plan)
        self.tell_lost()
        if want == self.written and static == self.written_static:
            return want
        on_disk, static_disk = wants(self.state_dir), modpages.statics(self.state_dir)
        if want or on_disk or static or static_disk:
            os.makedirs(run_dir(self.state_dir), exist_ok=True)
            # the new file first, the old one after: a module that gains or loses its process is never in neither (the
            # page asks for its plugin meanwhile)
            for name, w in want.items():
                if on_disk.get(name) != w:
                    _write(os.path.join(run_dir(self.state_dir), f"{name}.want.json"), w)
            for name, w in static.items():
                if static_disk.get(name) != w:
                    _write(os.path.join(run_dir(self.state_dir), f"{name}{modpages.STATIC}"), w)
            for name in set(on_disk) - set(want):
                _drop(os.path.join(run_dir(self.state_dir), f"{name}.want.json"))
            for name in set(static_disk) - set(static):
                _drop(os.path.join(run_dir(self.state_dir), f"{name}{modpages.STATIC}"))
            _write(os.path.join(run_dir(self.state_dir), PLAN), {"core": os.environ.get(CORE_ENV)})
        self.written, self.written_static = want, static
        return want

    def tell_lost(self):
        """What a module did not get from its snapshot paths and keys (core/modclaims.py) goes as a module.claims line
        into the journal, once per change in a hubd life; when a dispute is resolved, a line with an empty lost."""
        for name in sorted(set(self.lost) | set(self.told_lost)):
            if self.lost.get(name, []) != self.told_lost.get(name, []):
                self.record("module.claims", name, {"module": name, "lost": self.lost.get(name, []),
                                                    "channel": "core", "owner": False})
        self.told_lost = {n: list(v) for n, v in self.lost.items()}

    def mode_of(self, name, w):
        """{mode, modes} for a module with the profile, speech or stats capability (core/providers): mode is the
        strictest, modes is per function (live leads, warm, shadow runs next to the built-in one, without side
        effects). As of launch time: who leads now is in modrun/providers.json and in mode on every profile.* call.
        Nothing for the others."""
        from core import providers
        caps = w.get("capabilities") or []
        if not set(caps) & set(providers.CAPS):
            return {}
        recs = self.state().get("providers") or {}
        got = providers.modes(name, caps, self.cfg, recs)
        return {"mode": providers.mode(name, caps, self.cfg, recs, got), "modes": got}

    def pending(self):
        """Modules a session launch waits for: hubd orders them kept, they have the launch.env or prompt capability (or
        are the module that leads session login, named in modules.use.profile or after shadow, core/providers.py),
        they are not stopped or suspended, but are not ready in this launch or their launch file belongs to a process
        that no longer exists (the hub was restarted and the "ready" record is from the previous process)."""
        from core import profiles
        from platforms import current
        wanted, out = wants(self.state_dir), []
        sign_in = profiles.sign_in(self.cfg, self.state().get("providers") or {})
        for name, w in sorted(wanted.items(), key=lambda x: (x[1].get("order", 0), x[0])):
            caps = set(w.get("capabilities") or [])
            if not (caps & set(LAUNCH_CAPS) or name == sign_in and profiles.CAP in caps) \
                    or self.mods.status(name) != "ok":
                continue
            run = self.mods.ready(name, wanted)
            if not (run and isinstance(run.get("pid"), int) and current.alive(run["pid"])):
                out.append(name)
        return out

    def watchers(self):
        """{name: capabilities from modstate.VIEW_RIGHTS} of modules ordered kept, those whose slice carries parts of
        hub state (sessions, account limits, owner messages)."""
        from core import modstate
        out = {}
        for n, w in (self.written or {}).items():
            caps = set(w.get("capabilities") or []) & set(modstate.VIEW_RIGHTS)
            if caps:
                out[n] = caps
        return out

    def publish_views(self):
        """After an event that did not touch module state itself: new parts go into the slice of modules with part
        capabilities (modstate.views), if they changed. hubd calls it on every event: each part is built once for all
        (and only if someone has its capability), module records are not rebuilt; those whose slice was not yet
        written in this life get it whole (publish)."""
        from core import modstate
        watch = self.watchers()
        if not watch:
            return
        if self.published is None:
            return self.publish(only=set(watch))
        built = modstate.views(self.state(), watch.values())
        fresh = set(watch) - set(self.published)
        if fresh:   # the slice was not yet written for it (or the write failed): whole, only to it
            self.publish(only=fresh, built=built)
        for name in sorted(set(watch) - fresh):
            old = self.published[name]
            new = modstate.part(built, watch[name])
            if any(old.get(k) != v for k, v in new.items()):
                # parts of a revoked capability do not stay in the slice, even if the tick has not rewritten it yet
                self._put_slice(name, {**{k: v for k, v in old.items() if k not in modstate.PART_KEYS}, **new})

    def publish(self, only_new=False, only=None, built=None):
        """Hub state slices (modstate.slices) for modules hubd orders kept, as files in modrun, only the changed ones:
        a module process reads its own, not the whole state.json. The first call in a hubd life erases the previous
        life's slices (they could be left by a hubd before a rollback or one that crashed between a journal line and
        a slice); the slice of a module no longer kept goes away. only_new means write only to those not yet written in
        this life or whose part capabilities changed (the tick: cheap); only means only these; built is the parts
        already built for this event (publish_views)."""
        from core import modstate
        if self.published is None:
            self.published = {}
            try:
                names = os.listdir(run_dir(self.state_dir))
            except OSError:
                names = []
            for n in names:
                if n.endswith(".state.json"):
                    _drop(os.path.join(run_dir(self.state_dir), n))
        want = set(self.written or {})
        for name in set(self.published) - want:
            _drop(slice_path(self.state_dir, name))
            del self.published[name]
        watch = self.watchers()
        if only_new:   # and those whose part capabilities changed: the set of part keys in the slice is wrong
            names = sorted(n for n in want if n not in self.published
                           or {k for k in modstate.PART_KEYS if k in self.published[n]}
                           != set(modstate.part_keys(watch.get(n) or ())))
        else:
            names = sorted(want if only is None else want & set(only))
        for name, got in modstate.slices(self.state(), names, watch, built).items() if names else ():
            if self.published.get(name) != got:
                self._put_slice(name, got)

    def _put_slice(self, name, got):
        try:
            _write(slice_path(self.state_dir, name), got)
            self.published[name] = got
        except OSError as e:   # not written: the module process will read the hub state, the tick will try again
            _drop(slice_path(self.state_dir, name))
            self.published.pop(name, None)
            print(f"hubd: state slice of module {name} not written — {e}", flush=True)

    def tick(self):
        try:
            self.watch()
            want = self.sync()
            self.publish(only_new=True)
        except Exception as e:  # noqa: BLE001 — a failure around modules does not bring the core down: the next tick tries again
            print(f"hubd: modules — {type(e).__name__}: {e}", flush=True)
            return
        for name, w in want.items():
            try:
                self.hello(name, w)
            except Exception as e:  # noqa: BLE001 — one module does not hinder the others
                print(f"hubd: module {name} — {type(e).__name__}: {e}", flush=True)

    def hello(self, name, w):
        """A new module process launch: hello -> module.ready; not listening longer than HELLO_WAIT or answering wrongly
        gives module.error (blocked)."""
        run = run_of(self.state_dir, name)
        rec = (self.state().get("modules") or {}).get(name) or {}
        launch_id = (run or {}).get("launch")
        if not launch_id or launch_id == rec.get("ready") or self.mods.status(name) != "ok":
            return
        now = self.clock()
        h = self.hellos.get(name)
        if not h or h["launch"] != launch_id:
            h = self.hellos[name] = {"launch": launch_id, "first": now, "tried": float("-inf"), "done": False}
        if h["done"] or now - h["tried"] < HELLO_EVERY:
            return
        h["tried"] = now
        kind, value = post(run["port"], run["secret"], "/hook/hello",
                           {"module": name, "api": version.MODULE_API, "points": offered(w.get("api")),
                            **self.mode_of(name, w)},
                           min(budget_of("hello"), (w.get("budget") or {}).get("hello") or BUDGET), connect=HELLO_CONNECT)
        if kind in ("connect", "closed") and now - h["first"] < HELLO_WAIT:
            return   # the process is still coming up
        h["done"] = True
        if not kind:
            try:
                got = _hello(value, {"name": name, "version": w["version"], "capabilities": w.get("capabilities"),
                                       "api": w.get("api")})
            except BadResult as e:
                if self.newer_on_disk(name, w, value, h):
                    return
                kind, value = "schema", str(e)
        if kind:
            self.record("module.error", name, {"module": name, "hook": "hello", "kind": "hello",
                                               "error": f"{kind}: {value}"[:500], "launch": launch_id,
                                               "channel": "core", "owner": False, **_dev(w)})
            return
        if not self.migrate(name, w, run, got, rec):
            return
        self.record("module.ready", name, {"module": name, "version": got["version"], "points": got["points"],
                                           "launch": launch_id, "channel": "core", "owner": False})

    def newer_on_disk(self, name, w, res, h):
        """hello with another version, and module.json on disk already has it: the module folder changed (git checkout),
        hubd rereads the manifests once per CATALOG_EVERY, and the process brought up by the previous want read the new
        one. Not blocked: the modules are reread now, this launch's hello goes again on the next tick, by the new want
        (hub-up brings the process up again by it). Once per launch: if the reread did not help, it is a failure as
        before. True means wait for the tick."""
        if h.get("reread") or not (isinstance(res, dict) and res.get("name") == name
                                   and isinstance(res.get("version"), str) and res["version"] != w.get("version")):
            return False
        try:
            with open(os.path.join(w["cwd"], "module.json"), encoding="utf-8-sig") as f:
                disk = json.load(f)
        except (KeyError, TypeError, OSError, ValueError):
            return False
        if not (isinstance(disk, dict) and disk.get("name") == name and disk.get("version") == res["version"]):
            return False
        h["reread"], h["done"] = True, False
        print(f"hubd: module {name} — {res['version']} is already on disk (was {w.get('version')}): modules reread",
              flush=True)
        self.reload(self.cfg, self.catalog)
        return True

    def migrate(self, name, w, run, got, rec):
        """The version changed since the last readiness and the module declared migrate: /hook/migrate {from, to}
        before module.ready. A failure is module.error (kind migrate: blocked), the version stays: the next load tries
        again. True means go on."""
        was = rec.get("version")
        if not was or was == got["version"] or "migrate" not in got["points"]:
            return True
        budget = DEV_BUDGET if w.get("dev") else min(budget_of("migrate"),
                                                      (w.get("budget") or {}).get("migrate") or BUDGET * 1000)
        kind, value = post(run["port"], run["secret"], "/hook/migrate", {"from": was, "to": got["version"]}, budget)
        if not kind:
            try:
                POINTS["migrate"]["check"](value, {"name": name})
                return True
            except BadResult as e:
                kind, value = "schema", str(e)
        self.record("module.error", name, {"module": name, "hook": "migrate", "kind": "migrate",
                                           "error": f"{kind}: {value}"[:500], "launch": run.get("launch"),
                                           "channel": "core", "owner": False, **_dev(w)})
        return False
