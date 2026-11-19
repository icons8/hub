"""Codex: Codex's own sandbox on Windows for the hub's sessions that run in one — the
modes of hosts/codex_rules.MODES other than full access and the bus's liaison (hosts/codex_liaison.thread).

Live codex.exe 0.162.1 on Windows, an ordinary user (probes 2026-11-05): windowsSandbox/readiness is "notConfigured"
in a fresh CODEX_HOME, and then every command of a workspace-write or read-only thread is "rejected: blocked by
policy" — the liaison could not run even `hub gist`, plan and dontAsk ran nothing. Codex's own windowsSandbox/setupStart
{mode: "unelevated"} works without an administrator: it writes `[windows] sandbox = "unelevated"` into that CODEX_HOME's
config.toml (the core keeps the table across its rewrites, codex_set.KEEP_TABLES), and a new app-server says "ready"
(the one that ran the setup still says "notConfigured"). "elevated" needs an administrator's approval — the core does
not ask for it, and a mode the folder already has is not changed. On POSIX readiness says "notConfigured" too (Codex
needs no setup there) — Windows is told by initialize's platformFamily.

Not ready — a session that needs the sandbox is not started, with the reason (codex_rules.features), rather than
started with full access.

Seams codex_rules calls:
- prepare(home, codex, env, info, args) — after the hook's trust check; info — {"platform", "readiness"} of that same
  run; args — the folder's own app-server arguments (an account folder of the Codex pool: core/codexcli.args);
- refusal(state_dir, sandbox, who, now, home) — the reason a session with this sandbox cannot start, or None; home —
  the session's Codex folder (an account folder of the pool has its own mode and readiness), the hub's by default;
- retried(home, info) — the trust check's retry answered (its platform and readiness).
"""
import os
import re
import sys
import time

SETUP = "unelevated"   # the mode an ordinary user's setup gives (elevated asks an administrator)
SETUP_TIMEOUT = 60     # setupStart … setupCompleted at the host's start, s (live: about a second)
RETRY = 5 * 60         # not ready — a new attempt at a session start at most once in RETRY s,
RETRY_SETUP = 10       # and shorter: the start runs in hubd's tick
MODE = re.compile(r'^\[windows\][^\[]*?^sandbox\s*=\s*"([^"]*)"', re.M | re.S)   # the folder's mode, config.toml
FULL = (None, "danger-full-access")
_STATE = {}            # a Codex folder (the hub's, a pool account's) -> {"ok", "why", "codex", "env", "args", "at"}


def prepare(home, codex, env, info, args=()):
    info = info or {}
    return _check(os.path.abspath(home), codex, env, info.get("platform"), info.get("readiness"), args=args)


def _mode(home):
    try:
        with open(os.path.join(home, "config.toml"), encoding="utf-8") as f:
            m = MODE.search(f.read())
    except OSError:
        return None
    return m.group(1) if m else None


def retried(home, info, codex=None):
    """The trust check's retry (codex_rules.trusted) answered: a sandbox not ready yet is checked with its platform and
    readiness, in the tick's short time; codex — the command that retry ran (found anew: installed meanwhile)."""
    s = _STATE.get(os.path.abspath(home))
    if s and not s["ok"] and s["codex"] and (info or {}).get("platform"):
        _check(os.path.abspath(home), list(codex or s["codex"]), s["env"], info["platform"], info.get("readiness"),
               RETRY_SETUP, s["args"])


def _check(home, codex, env, platform=None, readiness=None, timeout=None, args=()):
    """The folder's sandbox -> _STATE; platform and readiness — of the trust check's run (None — asked here).
    timeout — the setup's wait (SETUP_TIMEOUT at the host's start, RETRY_SETUP in the tick); a question's — at most the
    trust check's own (TRUST_TIMEOUT, RETRY_TIMEOUT in the tick); args — the folder's own app-server arguments."""
    from hosts import codex_rules
    timeout = timeout or SETUP_TIMEOUT
    ask = codex_rules.TRUST_TIMEOUT if timeout >= SETUP_TIMEOUT else codex_rules.RETRY_TIMEOUT
    if platform is None:   # the trust check had no answer (or a retry): Codex itself
        platform, readiness = _ask(codex, env, home, ask, args)
    why = None
    if platform is None:
        why = "Codex did not answer, the platform is unknown"
    elif platform != "windows":   # Linux, macOS: Codex's sandbox needs no setup
        readiness = "ready"
    elif readiness is None:
        readiness = _ask(codex, env, home, ask, args)[1]
    # Codex's own setup, as an ordinary user may: no mode in the folder, or the core's own one to be brought up to date
    # (updateRequired); elevated is an administrator's — the core does not lower it to unelevated
    if why is None and (readiness == "notConfigured" or (readiness == "updateRequired" and _mode(home) in (None, SETUP))):
        got = codex_rules._rpc(codex, env, home, [("windowsSandbox/setupStart", {"mode": SETUP, "cwd": home})],
                               timeout, until=lambda m: m.get("method") == "windowsSandbox/setupCompleted", args=args)
        init, answers, notes = got or (None, [None], [])
        done = next((m.get("params") or {} for m in notes if m.get("method") == "windowsSandbox/setupCompleted"), None)
        refused = (answers[0] or {}).get("error")
        if refused:
            why = f"the sandbox setup was refused: {(refused.get('message') if isinstance(refused, dict) else 0) or refused}"
        elif done is None:
            why = "the sandbox setup did not answer" if got else "codex did not start"
        elif not done.get("success"):
            why = f"the sandbox setup failed: {done.get('error') or '?'}"
        else:   # a new process: the setup's own one keeps its old answer
            readiness = _ask(codex, env, home, max(ask, min(timeout, codex_rules.TRUST_TIMEOUT)), args)[1]
    if why is None and readiness != "ready":
        why = f"windowsSandbox/readiness: {readiness or 'no answer'}"
    _STATE[home] = {"ok": why is None, "why": why, "codex": codex, "env": env, "args": list(args), "at": time.time()}
    if why:
        print(f"codex: the sandbox is not ready ({why}) — sandboxed sessions and the liaison will not start",
              file=sys.stderr, flush=True)
    return why is None


def _ask(codex, env, home, timeout, args=()):
    """(platformFamily, windowsSandbox/readiness) of a new app-server; None for what did not answer."""
    from hosts import codex_rules
    got = codex_rules._rpc(codex, env, home, [("windowsSandbox/readiness", {})], timeout, args=args)
    if got is None:
        return None, None
    init, answers, _ = got
    return (init or {}).get("platformFamily"), ((answers[0] or {}).get("result") or {}).get("status")


def refusal(state_dir, sandbox, who, now=None, home=None):
    """Why a session with this sandbox cannot start (the folder's sandbox is not ready), or None. No check yet (no
    codex given, prepare not run) — None: the hook's trust refuses such a start anyway (codex_rules.trusted)."""
    if sandbox in FULL or not state_dir:
        return None
    home = os.path.abspath(home or os.path.join(state_dir, "codex-home"))
    s = _STATE.get(home)
    if not s:
        return None
    now = time.time() if now is None else now
    if not s["ok"] and s["codex"] and now - s["at"] >= RETRY:
        _check(home, s["codex"], s["env"], timeout=RETRY_SETUP, args=s["args"])
        s = _STATE[home]
    if s["ok"]:
        return None
    return (f"the Codex sandbox is not ready ({s['why']}): {who} does not start without it — the core does not give it "
            "full access (hubd log)")
