"""Session login — a facade (module extraction spec, §3; Module API 2.8, capability profile): which login Claude Code
sessions and the core's `claude -p` run on, and when to switch to another. All places of the core — the daemon (switch,
saving mode, gateway), the host (launch environment), the page (the "Accounts" snapshot, /api/accounts/use), the
watchdog, the critic, hubmod.model, pushes, the version check, `hub accounts use` — ask it, not modules.accounts.

The login state (st.account, st.limits, st.gateway, the account.* and gateway.changed events) is written and folded
only by the core, as before. The facade asks the provider WHAT to write and with which environment to start:
- builtin — the account pool in the core tree (modules/accounts): the same code as before the facade (goldens tests/test_profiles);
- module — a module process with the profile capability: the one named in hub.json `modules.use.profile` (at once) or the
  only ready one after the shadow beside the built-in (core/providers.py: builtin -> shadow -> warm -> module; who
  handles it is in st["providers"]).
  Each call carries mode: live — it handles it, shadow — only read and answer, warm — command tokens and conduit are
  allowed, the gateway is not. Points (core/modrun.py POINTS):
  profile.list {} -> {on, switch_at, profiles: [{name, email?, login?, renewable?, paused?}], paused?,
  route?: {on, ident, why?, code?}, ready?: {tokens, conduit, gateway: ok|fail|off|wait}} — route: the module's gateway
  (its place ident comes from the formula ident_of; the daemon writes gateway.changed); ready — whether it is ready to handle (warm);
  profile.decide {account, limits, now} -> {} | {switch_to, reason, restart?, refresh?, fresh?, free?, reset?,
  blocked?} | {exhausted: true, until, blocked} | {waiting: true, until, account}, and gens {name: token generation} and
  renewed {name: fingerprint} (a busy profile that is not the active one took a fresh token by command, another one than
  before: its block and measurement are lifted; optional - the core of an earlier version ignores it);
  profile.token {profile, session?, kind} -> {env, gateway?: {port, secret, first_party?}} — env: only login keys
  (modlaunch.login_name); gateway — the session goes through the module's gateway on 127.0.0.1:port (a switch without restart);
  the core builds the gateway environment (through) — the same as for the built-in;
  profile.use {name, force, account, now} -> {ok: true} | {error: unknown|paused|busy, why?, until?, forceable?}.
  The module did not answer (not ready, suspended, failure, timeout) — nothing changes: no switch, no return to the
  default login (unlike an earlier design: a module coming up after a hub restart would otherwise move the whole
  fleet to someone else's login). A session token with no answer — the default login with a log line, as for an account
  that is not in the pool; the launch first waits for the module (modrun.LAUNCH_CAPS).
"""
import hashlib
import json
import os
import re
import threading
import time

from core import catalog, cutout, i18n, modlaunch, modules
from core.coretext import T

BUILTIN = "builtin"
# who sent a fact the core does not take from it (admits): the word when the sender is not named
A_MODULE = T("core.profiles.a_module", "a module")
NOT_LISTED = T("core.profiles.not_listed", "account.limits from {sender}: profile {account} is not in its profile.list")
CAP = "profile"
KINDS = ("session", "model")   # the environment for a session process or for the core's `claude -p` (watchdog, critic, compat)
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@+-]{0,79}")   # like a pool account name (modules/accounts NAME)
LIST_TTL = 60          # the module's profile.list answer lives this long in the daemon (and until the next written account.*);
#                        after the first non-answer the previous answer is kept for as long — a momentary failure does not
#                        jerk the mode and the tab; silent for longer — "as without a pool" (extraction spec §3)
LIST_SILENT = 5        # the module did not answer profile.list — do not ask again for this long (each ask takes up to 2 s)
DECIDE_EVERY = 30      # the module's profile.decide — when the login changes and no less often than this many seconds
REASK_EVERY = 2        # a silent module is not asked again on a miss in the cached list for this long (relist)
ROUTE_EVERY = 5        # the daemon takes the module's route (its gateway) from a profile.list no older than this many seconds
MAX_PROFILES = 100
READY = ("tokens", "conduit", "gateway")   # ready in profile.list: whether the module is ready to handle (warm, core/providers.py)
READY_VALUES = ("ok", "fail", "off", "wait")
TOKEN_NOTICE = "account-token-"   # + the account name: the owner's notice that its command gave no token (Builtin.tell)
COMMAND_WAIT = 1.0     # the daemon tick waits this long for a token command it has just started (once per command); a longer
#                        one goes on in the background and a later tick takes its result (the command may run a minute)
JOB_FRESH = 300        # a command result no decision took within this long after it was ready is not about the question now
JOB_STUCK = 120        # a command thread with no result this long after its start (COMMAND_TIMEOUT and then some: a
#                        child left holding the pipe on Windows) counts as failed — the decisions do not wait for it for ever

# The account gateway is a local HTTP hop between the sessions' claude and the API (the built-in — modules/gateway, or a login module's gateway).
# The session environment for the gateway is the same for both (through): a session raised through one works through the
# other at the same place (port and secret); by the same marks a nested claude (hosts/nested) and modelrule recognize a session on the gateway.
UPSTREAM = "https://api.anthropic.com"
STUB = "hub-gateway"                 # a stub token in the session environment: the real one is only at the gateway
STUB_ENV = "HUB_GATEWAY_STUB"        # the same for a nested claude: Claude Code strips the token from the Bash environment
SECRET_HEADER = "x-hub-gateway"
UPSTREAM_ENV = "HUB_GATEWAY_UPSTREAM"  # where the gateway sends the session: the gateway address, not the person's own endpoint (core/modelrule)
SECRET = re.compile(r"[A-Za-z0-9_-]{16,256}")
IDENT = re.compile(r"[0-9a-f]{12}")   # ident_of


class BadAnswer(ValueError):
    pass


class NoProfile(OSError):
    """The core's `claude -p` is on a login module's profile and the module did not give its login: the call is skipped,
    like an unavailable model (OSError — like a missing claude), instead of going to the machine's login."""


# --- account gateway: the place and the session environment -------------------------------------------------------------

def ident_of(port, secret):
    """Fingerprint of the gateway place (port and secret): once it changes, sessions on the old one go nowhere and the core restarts them."""
    return hashlib.sha256(f"{port}:{secret}".encode()).hexdigest()[:12]


def upstream_of(env):
    """Where the gateway sends: the person's own ANTHROPIC_BASE_URL (before the gateway) or the API."""
    return (env.get("ANTHROPIC_BASE_URL") or UPSTREAM).rstrip("/")


def through(env, port, secret, stub, first_party=False):
    """The session's claude environment for the gateway on 127.0.0.1:port (modifies env). stub — the session is on a
    profile token: the real token is replaced by a stub; otherwise (this computer's login) its own login as is, the
    gateway substitutes the token if the active one is from the pool."""
    if stub:
        for k in modlaunch.AUTH_ENV:
            env.pop(k, None)
        env["CLAUDE_CODE_OAUTH_TOKEN"] = env[STUB_ENV] = STUB
    else:
        env.pop(STUB_ENV, None)
    env[UPSTREAM_ENV] = upstream_of(env)   # before the address is replaced: the person's own ANTHROPIC_BASE_URL or the API
    env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port}"
    env.setdefault("ENABLE_TOOL_SEARCH", "true")   # with a foreign address Claude Code would otherwise turn tool search off
    if first_party:
        # the gateway sends byte for byte to api.anthropic.com: "first party only" betas are needed (without the flag, 2 betas
        # fewer on 2.1.296); the flag is internal to Claude Code — check on every version
        env.setdefault("_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL", "1")
    own = env.get(modlaunch.HEADERS)
    env[modlaunch.HEADERS] = (own + "\n" if own else "") + f"{SECRET_HEADER}: {secret}"
    bypass = [x for x in (env.get("NO_PROXY") or env.get("no_proxy") or "").split(",") if x.strip()]
    bypass += [h for h in ("127.0.0.1", "localhost") if h not in bypass]
    env["NO_PROXY"] = env["no_proxy"] = ",".join(bypass)
    return env


JOB_OWN_KEY = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")   # a job with its own API login: not the machine's OAuth one the pool replaces


def job_env(env, state_dir, st):
    """A schedule job's command or check and a `hub run` on the account gateway like a session (modifies env): without it
    their `claude -p` goes to the machine's login and meets its limit while the pool stands idle. The active account is a
    pool token (job_stub): a stub, as a session on it has — the gateway puts the token in, and an expired login of the
    machine no longer fails claude before the gateway ("OAuth session expired"); otherwise the machine's login as is. The
    same conditions as a session's spawn: the core has the gateway on, gateway.json is the place the core knows, no cloud
    provider, no API key of its own (JOB_OWN_KEY: a script with the SDK would get the gateway without its secret header)
    and no module's own address in the environment. The place is the built-in one's (gateway.json); a login
    module's gateway whose place is not there (it is in the profile.token answer) — direct, as before. st is the core state."""
    gate = st.get("gateway") if isinstance(st, dict) else None
    if not isinstance(gate, dict) or gate.get("on") is not True or not gate.get("ident") or on_gateway(env) \
            or modlaunch.provider(env) or any(env.get(k) for k in JOB_OWN_KEY) or not cutout.present("gateway"):
        return env
    from modules.gateway import gateway as gw
    place = gw.identity(state_dir)
    if not place or place["ident"] != gate["ident"]:   # moved, and the core has not recorded it yet: direct, like a session
        return env
    probe = (gw.status_of(state_dir) or {}).get("probe") or {}
    return through(env, place["port"], place["secret"], stub=job_stub(state_dir, st),
                   first_party=probe.get("first_party") is True)


def job_stub(state_dir, st):
    """A job on the built-in gateway gets the stub: session login is the built-in pool's (no login module), the pool
    works and its active account has a token (not this computer's login row) — the gateway answers a stub with that
    token (Gateway.pick), as for a session on it (hosts/streamjson _stub). Settings unreadable — no stub, as before."""
    from core import providers
    from modules.accounts import accounts
    try:
        if sign_in(catalog.load_config(state_dir), st.get("providers") or providers.current(state_dir)) is not None:
            return False
    except (OSError, ValueError):
        return False
    acc = st.get("account")
    pool = accounts.load()
    return accounts.enabled(pool) and bool(token_of(pool, acc.get("active") if isinstance(acc, dict) else None))


def gateway_enabled(cfg):
    """hub.json gateway.enabled is on (as the gateway's own settings read it) — without importing the gateway, which the
    open flavor does not carry; a login module reads the same switch."""
    g = (cfg or {}).get("gateway")
    return isinstance(g, dict) and g.get("enabled") is True


def on_gateway(env):
    """A process with this environment goes through the account gateway (the gateway secret in the headers is set only by through)."""
    return SECRET_HEADER in (env.get(modlaunch.HEADERS) or "")


# --- providers-------------------------------------------------------------------------------------------------------

def token_of(pool, name):
    """The token of the pool's account name, or None."""
    return next((a.get("token") for a in pool["accounts"] if a["name"] == name), None)


class Builtin:
    """The pool in the core tree: decisions of modules.accounts.decide, tokens from accounts.json, the command token in
    its own thread (as the accounts module takes it): the daemon tick waits for it no longer than `wait`, then the
    decision that needs the token writes nothing and a later tick takes the result. cached — the pool file is re-read
    only when it changed (daemon: every tick); otherwise on every question, as the host, the page and the watchdog
    used to read it."""
    name = BUILTIN
    wait = COMMAND_WAIT

    def __init__(self, clock, cached=False, state_dir=None):
        self.clock, self.cached, self.state_dir = clock, cached, state_dir
        self.mtime, self.loaded, self.tried = "never", None, {}
        self.failed, self.got = {}, {}   # when the account's command last gave no token / gave one (the same one too)
        self.probed, self.counts = {}, {}   # when a busy command account was last probed; the command token counters (refresh.json)
        self.jobs, self.counting = {}, threading.Lock()   # token commands under way or not taken yet: name -> job (fetch)

    def pool(self):
        from modules.accounts import accounts
        if not self.cached:
            return accounts.load()
        try:
            mtime = os.stat(accounts.path()).st_mtime_ns
        except OSError:
            mtime = None
        if self.mtime != mtime:
            self.mtime, self.loaded = mtime, accounts.load()
        return self.loaded

    def listing(self):
        from modules.accounts import accounts
        pool = self.pool()
        return {"on": accounts.enabled(pool), "switch_at": pool["switch_at"],
                **({"paused": True} if pool.get("off") else {}),
                "profiles": [{"name": a["name"], **({"email": a["email"]} if a.get("email") else {}),
                              **({"login": True} if a.get("login") else {}),
                              **({"renewable": True} if a.get("command") else {})} for a in pool["accounts"]]}

    def token(self, name, session=None, kind="session"):
        """The account's login environment (an empty value removes the variable) or None — not in the pool or no token yet.
        The core's `claude -p` only while the pool works (accounts.model_env), a session process also on pause (like the host)."""
        from modules.accounts import accounts
        pool = self.pool()
        if kind == "model" and not accounts.enabled(pool):
            return None
        acc = next((a for a in pool["accounts"] if a["name"] == name), None)
        if acc and acc.get("login"):
            return {"env": {}}   # this computer's login: the environment as is, marked with this account
        if not acc or not acc.get("token"):
            return None
        return {"env": {**{k: "" for k in modlaunch.AUTH_ENV}, "CLAUDE_CODE_OAUTH_TOKEN": acc["token"]}}

    def decide(self, state, record, shadow=None):
        """First the token fingerprints (account.token — a new token of the same account: into the journal before the
        switch decision, the decision then uses the new generation), then the accounts.decide decision; the command token
        is fresh on a switch and at the limit. state() is the core state now, record(type, data) writes to the journal.
        shadow(intent) — the module's shadow: the built-in's intent (accounts.decide before the command token, None —
        nothing) — before side effects, with the same input."""
        from modules.accounts import accounts
        self.collect(state, record)
        pool = self.pool()
        if not accounts.enabled(pool) and not (state().get("account") or {}).get("active"):
            return
        # a new token of the same account (hub accounts token, remove + add) — by fingerprint; the command token is
        # changed by the core itself (switch, refresh) with its own generation
        known = (state().get("account") or {}).get("tokens") or {}
        for a in pool["accounts"]:
            fp = accounts.fingerprint(a["token"]) if a.get("token") and not a.get("command") else None
            if fp and known.get(a["name"]) != fp:
                record("account.token", {"name": a["name"], "fp": fp})
        self.probe_blocked(state, record)
        # the active one is a command account with no token yet (moved to by hand; its row came back into the pool
        # without one): take it now, before sessions start — otherwise they start on the default login (10-27)
        active = (state().get("account") or {}).get("active")
        pool = self.pool()   # the probe's collect may have taken a token into the file since
        if accounts.enabled(pool) and any(a["name"] == active and a.get("command") and not a.get("token")
                                          for a in pool["accounts"]):
            self.fetch(active, state=state, record=record, quiet=True)
        self.no_token(state, record, active)

        # a command that has just failed (fetch would not call it again before COMMAND_RETRY) is not a target: the move goes
        # to the next free one rather than staying on the busy account until the command recovers
        skip = {a["name"] for a in pool["accounts"] if a.get("command")
                and self.clock() - self.failed.get(a["name"], -accounts.COMMAND_RETRY) < accounts.COMMAND_RETRY}

        def decide(refresh=True):
            return accounts.decide(state().get("account"), self.pool(), accounts.enabled(self.pool()), self.clock(),
                                   refresh, skip)
        todo = decide()
        if todo and todo["type"] == "account.unblocked":
            # "busy until" marks that the latest reading or a raised threshold no longer holds: only the built-in judges them (the
            # shadow of a login module is not told), the decision follows by the marks that remain
            record(todo.pop("type"), todo)
            todo = decide()   # at once: a move away from a limit-hit account does not wait for the next tick
            if todo and todo["type"] == "account.unblocked":
                todo = None   # the marks did not go (the write was refused): not again this tick, the rest waits for the next
        if shadow is not None:
            shadow(dict(todo) if todo else None)
        fresh = True
        if todo and todo.get("refresh"):
            got = self.fetch(todo["to"], fresh=True, state=state, record=record)
            if got is None:
                return   # being fetched: a later tick decides by its result (the shadow is told the same intent again)
            if not got[0]:
                fresh = False
                todo = decide(refresh=False)   # no fresh one — busy until the reset, like an ordinary one
        while todo and todo["type"] == "account.switched" and todo.get("to") and not todo.get("refresh") \
                and not todo.get("free"):
            target = next((a for a in self.pool()["accounts"] if a["name"] == todo["to"]), {})
            if not target.get("command"):
                break
            if todo["to"] in skip:
                return   # the pool's first by default (no one else is free) is behind a failing command: later again
            if todo["to"] not in self.jobs and target.get("token") \
                    and self.clock() - self.got.get(todo["to"], -accounts.COMMAND_RETRY) < accounts.COMMAND_RETRY:
                break   # its command gave this token a moment ago (the probe of this tick): not a second call
            got = self.fetch(todo["to"], state=state, record=record)
            if got is None:
                return   # the command gives a fresh token on every switch; it is being fetched — a later tick moves
            if got[0]:
                if got[1]:
                    todo["fresh"] = True   # a different token is a new generation: the previous one's processes and limits are not about it
                break
            # the command's token is fresh on every switch; it gave none: the next free one, if there is one (later again)
            skip = {*skip, todo["to"]}
            todo = decide(refresh=fresh)
        if todo:
            record(todo.pop("type"), todo)

    def no_token(self, state, record, active):
        """The active command account whose command has just given no token is busy (account.no_token): sessions would
        start on this computer's login, which is refused or not there (11-10: three sessions refused for hours, account:
        null, no move). accounts.decide moves to the next free one or says "all busy"; the mark lasts until the retry after
        next (each failed retry extends it). Once a token of a marked account is in the pool, the mark goes (got)."""
        from modules.accounts import accounts
        pool, seen = self.pool(), (state().get("account") or {}).get("seen") or {}
        for name, s in list(seen.items()):
            if (s or {}).get("type") == "no_token" and token_of(pool, name):
                record("account.no_token", {"name": name, "got": True})
        at = self.failed.get(active)
        if not accounts.enabled(pool) or at is None or self.clock() - at >= accounts.COMMAND_RETRY or token_of(pool, active) \
                or not any(a["name"] == active and a.get("command") for a in pool["accounts"]) \
                or (seen.get(active) or {}).get("at") == at:
            return
        record("account.no_token", {"name": active, "at": at, "until": at + 2 * accounts.COMMAND_RETRY})

    def fetch(self, name, fresh=False, state=None, record=None, quiet=False):
        """The account's command token goes to the pool file, in its own thread (the command may run COMMAND_TIMEOUT):
        (True, whether the token differs from the one before the command) — it is there; (False, …) — the command gave
        none or (fresh — a different one than before is needed) the same one: not again before COMMAND_RETRY; None —
        being fetched, a later call takes the result. Only the call that starts the command waits for it (up to
        self.wait): every later tick only looks. quiet — nobody waits for the result (the token only goes to the
        pool): a finished job is dropped by collect. With state and record the owner hears why the command gave none
        (tell)."""
        from modules.accounts import accounts
        self.collect(state, record)
        job, wait = self.jobs.get(name), 0.0
        if job is None:
            if self.clock() - self.tried.get(name, -accounts.COMMAND_RETRY) < accounts.COMMAND_RETRY:
                return False, False
            job = self.jobs[name] = self.start(name, quiet)
            wait = self.wait
        job["quiet"] = job["quiet"] and quiet   # someone waits for it: it is not dropped when done
        if not job["done"].wait(wait):
            return (False, False) if job.get("stuck") else None
        self.collect(state, record)
        if job.get("stale") or not job["ok"]:
            return False, False   # failed (collect keeps no failed job: its tried says it) or no longer about this pool
        if self.jobs.get(name) is job:
            del self.jobs[name]
        if fresh and not job["changed"]:
            self.tried = {**self.tried, name: job["at"]}
            return False, False
        return True, job["changed"]

    def start(self, name, quiet):
        """A token command in its own thread: it touches only the pool file (accounts.fetch_token, under its lock), the
        counters (note) and its job; the journal and this object's state are the tick's (collect)."""
        from modules.accounts import accounts
        token = lambda: token_of(accounts.load(), name)  # noqa: E731 — the file itself: the cached pool is the tick's
        began = time.monotonic()
        job = {"done": threading.Event(), "ok": False, "changed": False, "why": [], "token": None, "taken": False,
               "quiet": quiet, "asked": self.clock(), "began": began}

        def work():
            was = None
            try:
                was = token()
                job["ok"] = accounts.fetch_token(name, why=job["why"],
                                                 note=lambda outcome, reason: self.note(name, outcome, reason))
            except Exception as e:  # noqa: BLE001 — the background does not fail silently: a failure with its reason
                print(f"hubd: token command of {name}: {type(e).__name__}"[:300], flush=True)
                job["why"].append(type(e).__name__)
            finally:
                try:
                    job["token"] = token()
                except Exception:  # noqa: BLE001
                    job["token"] = None
                job["changed"] = bool(job["ok"]) and job["token"] != was
                job["at"] = job["asked"] + (time.monotonic() - began)   # by the core's clock of the question
                job["done"].set()
        threading.Thread(target=work, name=f"token-{name}", daemon=True).start()
        return job

    def collect(self, state=None, record=None):
        """Finished token commands, in the tick: the owner's notice (tell), the retry times, the pool re-read. A result no
        decision took within JOB_FRESH, or one the pool's token has changed since, is dropped (the command again no
        sooner than COMMAND_RETRY after it: a second call right away would issue yet another token)."""
        from modules.accounts import accounts
        for name, job in list(self.jobs.items()):
            if not job["done"].is_set():
                if job.get("stuck") or time.monotonic() - job["began"] < JOB_STUCK:
                    continue
                # the thread has not come back (the command's child holds its pipe): a failure for the decisions, the
                # next free one is the target; the job stays — no second command while this one runs, and if it ever
                # finishes, its result is taken as any other (a token clears the notice)
                job["stuck"] = True
                why = f"no result in {JOB_STUCK} s"
                print(f"hubd: token command of {name}: {why}", flush=True)
                if record is not None:
                    self.tell(state(), record, name, why)
                at = job["asked"] + (time.monotonic() - job["began"])
                self.tried = {**self.tried, name: at}
                self.failed = {**self.failed, name: at}
                continue
            if not job["taken"]:
                job["taken"] = True
                self.mtime = "never"   # the pool file was rewritten — re-read it
                if record is not None and (job["ok"] or job["why"]):
                    self.tell(state(), record, name, None if job["ok"] else job["why"][0])
                if job["ok"]:
                    self.got = {**self.got, name: job["at"]}
                else:
                    self.tried = {**self.tried, name: job["at"]}
                    self.failed = {**self.failed, name: job["at"]}
                if job["quiet"] or not job["ok"]:
                    del self.jobs[name]   # a failure is in tried: a caller gets it from there
                    continue
            if self.clock() - job["at"] > JOB_FRESH or token_of(self.pool(), name) != job["token"]:
                del self.jobs[name]
                job["stale"] = True
                self.tried = {**self.tried, name: max(self.tried.get(name, job["at"]), job["at"])}

    @staticmethod
    def tell(st, record, name, why):
        """The owner hears that the account's command gave no token, and why: one notice per account with a constant id —
        a retry with the same reason writes nothing, a new reason updates it; once a token comes it is cleared
        (2026-11-05: every move to conduit failed for 2.5 hours and nobody heard of it)."""
        from modules.accounts import accounts
        nid = TOKEN_NOTICE + name
        notice = (st.get("notices") or {}).get(f"n:core:{nid}")
        if why is None:
            if notice:
                record("owner.notify_resolved", {"id": nid, "owner": False})
            return
        lang = i18n.state_lang(st)
        text = i18n.tr(lang, "core.account.token_failed", name=name, why=why, minutes=accounts.COMMAND_RETRY // 60)
        if (notice or {}).get("text") != text:
            record("owner.notify", {"id": nid, "title": i18n.tr(lang, "core.account.token_failed_title", name=name),
                                    "text": text, "source": "core", "owner": False})

    def note(self, name, outcome, why=""):
        """The command token result into the counters file <state>/gateway/refresh.json (ok / same / fail, the time of the
        latest of each); a failed write is not an error: the counters are for diagnostics."""
        with self.counting:   # commands of two accounts may finish at once
            self._note(name, outcome, why)

    def _note(self, name, outcome, why):
        from modules.accounts import accounts
        folder = os.path.join(self.state_dir or _state_dir(), "gateway")
        num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)  # noqa: E731
        if not self.counts:   # after a restart the counters go on from the file
            try:
                with open(os.path.join(folder, "refresh.json"), encoding="utf-8") as f:
                    self.counts = json.load(f)
            except (OSError, ValueError):
                pass
            if not (isinstance(self.counts, dict) and all(
                    isinstance(v, dict) and all(isinstance(v.get(k), int) for k in ("ok", "same", "fail"))
                    and isinstance(v.get("at"), dict) and all(num(t) for t in v["at"].values())
                    for v in self.counts.values())):
                self.counts = {}   # a damaged file: the count starts again, the tick does not stop
        accounts.tally(self.counts, name, outcome, self.clock(), why)
        try:
            os.makedirs(folder, exist_ok=True)
            catalog.write_private(os.path.join(folder, "refresh.json"), self.counts)
        except OSError:
            pass

    def probe_blocked(self, state, record):
        """A command account (the organization token service) busy until the reset and not the active one: the service may
        hand out another account now, so a fresh token is tried not less often than REFRESH_EVERY (the command is not called
        more often than COMMAND_RETRY, fetch). A different token (by the hash) lifts the block and the old measurement:
        account.token with fresh; the same one — the block stands. The active one is not probed here: accounts.decide
        takes its fresh token at the limit (REFRESH_EVERY; while all are busy REFRESH_EXHAUSTED, and a fresh token nobody
        has read ends that hold REFRESH_EXHAUSTED after it)."""
        from modules.accounts import accounts
        pool = self.pool()
        acc, now = state().get("account") or {}, self.clock()
        for a in pool["accounts"] if accounts.enabled(pool) else ():
            name = a["name"]
            if not a.get("command") or name == acc.get("active") or (acc.get("blocked") or {}).get(name, 0) <= now:
                continue
            last = (acc.get("refresh_at", acc.get("refreshed")) or {}).get(name) or 0
            if name not in self.jobs:   # a probe being fetched is come back to until its result is taken
                if now - max(last, self.probed.get(name, 0)) < accounts.REFRESH_EVERY:
                    continue
                if now - self.tried.get(name, -accounts.COMMAND_RETRY) < accounts.COMMAND_RETRY:
                    continue   # the command was called a moment ago: a probe that would not call it is not a probe
            got = self.fetch(name, fresh=True, state=state, record=record)
            if got is None:
                break   # being fetched: the decisions of the tick do not wait for it
            self.probed = {**self.probed, name: now}
            if got[0]:
                now_token = token_of(self.pool(), name)
                if now_token:
                    record("account.token", {"name": name, "fp": accounts.fingerprint(now_token), "fresh": True})
            break   # one probe per tick

    def use(self, acc, name, force, now):
        from modules.accounts import accounts
        pool = self.pool()
        return accounts.use_check(acc, pool, accounts.enabled(pool), name, force, now)


class Absent:
    """No built-in pool in the tree (the open flavor, core/cutout.py): no pool, no decisions, no profile tokens — sessions
    on this machine's Claude Code login, unless a login module takes it over."""
    name = BUILTIN

    def pool(self):
        return {"accounts": [], "switch_at": 0.95}

    def listing(self, ttl=0):
        return None

    def token(self, name, session=None, kind="session"):
        return None

    def decide(self, state, record, shadow=None):
        return None

    def use(self, acc, name, force, now):
        return {"code": "none"}


class Module:
    """A module process with the profile capability (Module API 2.8): answers follow the point schemas (POINTS), no answer — None."""

    def __init__(self, name, mods, clock, mode="live", prefix="profile", view=None, kinds=None, ask=None):
        self.name, self.mods, self.clock, self.mode = name, mods, clock, mode
        # the same machinery for another pool (core/codexslot.py: the Codex accounts): the points' prefix, the pool's
        # record in the state (view(st) -> (record, limits row, the cuts of its sessions)) and the event types
        self.prefix, self.view, self.kinds = prefix, view or _claude_view, kinds or {}
        self.ask_more = ask   # ask(st) -> more fields of the decide question (the Codex pool: the rows the core accepts)
        self.list_at, self.listed = None, None
        self.reasked = None         # when relist() last got no answer
        self.good, self.silent_since = None, None   # the last profile.list answer; since when it has been silent
        self.asked = (None, None)   # (fingerprint of the profile.decide question, when asked)
        self.intent = None          # shadow: the built-in's intent that was used the last time we asked

    def call(self, point, payload):
        """mode is in every call (core/providers.mode): in the shadow the module only reads and answers."""
        return self.mods.call(self.name, point, {**payload, "mode": self.mode}) if self.mods is not None else None

    def listing(self, ttl=0):
        now = self.clock()
        if ttl and self.list_at is not None and now - self.list_at < (ttl if self.listed is not None
                                                                      else min(ttl, LIST_SILENT)):
            return self.listed
        got = self.call(f"{self.prefix}.list", {})
        if got is not None:
            self.good, self.silent_since = got, None
        else:
            self.silent_since = self.silent_since if self.silent_since is not None else now
            if ttl and self.good is not None and now - self.silent_since < LIST_TTL:
                got = self.good   # just went silent — the previous answer
        self.list_at, self.listed = now, got
        return got

    def relist(self):
        """A fresh answer for a miss in the cached list (account.limits of a profile that is not there). A silent module is
        not asked again for REASK_EVERY (each ask takes up to 2 s) and leaves the cache as it was; an answering one is asked
        on every miss. None — not asked, or silent."""
        now = self.clock()
        if self.reasked is not None and now - self.reasked < REASK_EVERY:
            return None
        got = self.call(f"{self.prefix}.list", {})
        self.reasked = now if got is None else None
        if got is not None:
            self.good, self.silent_since, self.list_at, self.listed = got, None, now, got
        return got

    def token(self, name, session=None, kind="session"):
        """The profile's environment: its login keys instead of the inherited AUTH_ENV, as in the built-in pool (an API key
        outranks a login token — otherwise claude would go to the owner's key while the limits were counted for the profile)."""
        got = self.call("profile.token", {"profile": name, **({"session": session} if session else {}), "kind": kind})
        if got == {}:
            return got   # the profile has no login — an answer, not silence (the shadow compares it with the built-in)
        if got and got["env"]:
            got = {**got, "env": {**{k: "" for k in modlaunch.AUTH_ENV}, **got["env"]}}
        return got

    def question(self, st, now):
        """(the profile.decide question, its fingerprint): the login without token fingerprints, limits, turns cut off by a limit."""
        acc, limits, cuts = self.view(st)
        acc = {k: v for k, v in acc.items() if k != "tokens"}
        more = self.ask_more(st) if self.ask_more else {}
        mark = hashlib.sha256(json.dumps([acc, limits, cuts, more], sort_keys=True, default=str).encode()).hexdigest()
        return {"account": acc, "limits": limits or {}, "now": now, **more}, mark

    def due(self, mark, now):
        """Ask again: the login changed or the module answered long ago."""
        return not (mark == self.asked[0] and now - self.asked[1] < DECIDE_EVERY)

    def ask(self, question, mark, now):
        got = self.call(f"{self.prefix}.decide", question)
        if got is not None:
            self.asked = (mark, now)
        return got

    def decide(self, state, record):
        st, now = state(), self.clock()
        question, mark = self.question(st, now)
        if not self.due(mark, now):
            return   # the login did not change — the module answered recently
        got = self.ask(question, mark, now)
        if got is None:
            return   # no answer (starting up, failure) — ask on the next tick
        acc = self.view(st)[0]
        evs = events(got, acc, now, self.kinds)
        # a new token of a profile whose fingerprint existed already is a new generation that changes the login (refreshed,
        # the login refusal of the old token is lifted): as in the built-in, it goes to the journal first, the decision
        # follows the new login: the same question against the state after the write, once; the second answer is written
        # whole (a new fingerprint in every answer does not hold the decision). If the module does not answer the second —
        # no decision until the next question
        # (renewed - a busy profile took a fresh token by command - is the same: the block is lifted, the decision follows the new state)
        if any(k == "account.token" and (d.get("fresh") or (acc.get("tokens") or {}).get(d["name"]) is not None)
               for k, d in evs):
            for kind, data in evs:
                if kind == "account.token":
                    record(kind, data)
            st, now = state(), self.clock()
            question, mark = self.question(st, now)
            got = self.ask(question, mark, now)
            if got is None:
                return
            evs = events(got, self.view(st)[0], now, self.kinds)
        for kind, data in evs:
            record(kind, data)

    def use(self, acc, name, force, now):
        acc = {k: v for k, v in (acc or {}).items() if k != "tokens"}
        got = self.call(f"{self.prefix}.use", {"name": name, "force": bool(force), "account": acc, "now": now})
        if got is None:
            return {"code": "none"}
        if got.get("ok"):
            return None
        return {"code": got["error"], **{k: got[k] for k in ("why", "until", "forceable") if k in got}}


def _claude_view(st):
    """The Claude pool in the state: its record, the limits row, the sessions cut by a limit (name, account, generation)."""
    cuts = sorted([n, (s.get("limit_cut") or {}).get("account"), (s.get("limit_cut") or {}).get("gen")]
                  for n, s in (st.get("sessions") or {}).items() if s.get("limit_cut"))
    return st.get("account") or {}, st.get("limits"), cuts


def events(dec, acc, now, kinds=None):
    """The module's profile.decide answer -> [(type, data)] for the journal. A new token generation (gens) is account.token,
    like the fingerprint in the built-in; what is already in the state (the same waiting, "all busy" until the same
    time, a switch to the active one without a new token) is not written: otherwise the same fact every tick."""
    out = [("account.token", {"name": n, "fp": g}) for n, g in sorted((dec.get("gens") or {}).items())
           if (acc.get("tokens") or {}).get(n) != g]
    # renewed: a busy non-active profile took a fresh token by command (another one by the hash) — the same account.token with fresh
    # lifts its block and old measurement (reducer); the module reports it once, in the answer after the new token
    # (the active profile and a fingerprint already taken are not written: the reducer would ignore them, and a module
    # repeating its answer would add a journal line and a second question every tick)
    out += [("account.token", {"name": n, "fp": g, "fresh": True}) for n, g in sorted((dec.get("renewed") or {}).items())
            if n != acc.get("active") and (acc.get("fresh_fp") or {}).get(n) != g]
    active = acc.get("active")
    if dec.get("waiting"):
        if not ((acc.get("waiting") or {}).get("account") == dec["account"] and acc["waiting"].get("until") == dec["until"]):
            out.append(("account.waiting", {"account": dec["account"], "until": dec["until"],
                                            "why": dec.get("reason") or ""}))
    elif dec.get("exhausted"):
        if not (acc.get("exhausted") or {}).get("until", 0) > now:
            out.append(("account.exhausted", {"blocked": dec.get("blocked") or {}, "until": dec["until"],
                                              "why": dec.get("reason") or ""}))
    elif "switch_to" in dec:
        to = dec["switch_to"]
        flags = {k: True for k in ("restart", "refresh", "fresh", "free", "reset") if dec.get(k)}
        if to != active or flags.keys() & {"refresh", "fresh", "free"}:
            out.append(("account.switched", {"to": to, "from": active, **flags, "why": dec.get("reason") or "",
                                             **({"blocked": dec["blocked"]} if dec.get("blocked") else {})}))
    return [((kinds or {}).get(k, k), d) for k, d in out]


# --- facade -----------------------------------------------------------------------------------------------------------

def sign_in(cfg, recs):
    """The module that handles session login now (named, or after the shadow, core/providers.py), or None — the
    built-in pool. recs is the process's st["providers"] (or providers.current)."""
    from core import providers
    return providers.active(CAP, cfg, recs)


class Profiles:
    """One per process (daemon, host, page) or per call (watchdog, critic, command). config() is this process's hub.json
    (None — read from disk on each question); mods are the module calls (core/modrun.Modules; None — create, only if the
    provider is a module); cached — the pool file is re-read by time (daemon); ttl — how long the module's profile.list answer lives."""

    def __init__(self, state_dir=None, mods=None, config=None, clock=time.time, cached=False, ttl=0, providers=None):
        self.state_dir, self._mods, self._config, self.clock, self.ttl = state_dir, mods, config, clock, ttl
        self._providers = providers   # the process's st["providers"]; None — hubd's copy from disk (core/providers.current)
        # the built-in pool, or none: the open flavor is cut without it (core/cutout.py)
        self.builtin = (Builtin(lambda: self.clock(), cached=cached, state_dir=state_dir) if cutout.present("profile")
                        else Absent())
        self.modules = {}   # (name, mode) -> Module (its own answer cache)
        # the login module's shadow (hubd, core/providers.Tick): shadow() is (module, mode) or None, observe(kind, …) is the
        # result of a decision comparison
        self.shadow, self.observe = None, None

    def config(self):
        if self._config is not None:
            return self._config() or {}
        from core import catalog
        try:
            return catalog.load_config(self.state_dir or _state_dir())
        except (OSError, ValueError):
            return {}

    def mods(self):
        if self._mods is None:
            from core import modrun
            self._mods = modrun.Modules(self.state_dir or _state_dir())
        return self._mods

    def recs(self):
        if self._providers is not None:
            return self._providers() or {}
        from core import providers
        return providers.current(self.state_dir or _state_dir())

    def sign_in(self, cfg=None):
        """The module that handles session login now, or None — the built-in pool."""
        return sign_in(self.config() if cfg is None else cfg, self.recs())

    def module(self, name, mode="live"):
        if (name, mode) not in self.modules:
            self.modules[(name, mode)] = Module(name, self.mods(), lambda: self.clock(), mode)
        return self.modules[(name, mode)]

    def provider(self):
        name = self.sign_in()
        return self.builtin if name is None else self.module(name)

    def listing(self, ttl=None):
        """{on, switch_at, profiles, paused?, route?} — or None: the module did not answer (no "Accounts" tab). ttl — the
        module's answer is no older than this many seconds (no longer than the process ttl)."""
        p = self.provider()
        return p.listing(self.ttl if ttl is None else min(self.ttl, ttl)) if isinstance(p, Module) else p.listing()

    def route(self, fresh=True):
        """The login module's gateway (the route of its profile.list): {on, ident?, why?, code?}, {} — the module has no
        gateway; None — the module did not answer or session login is on the built-in pool (its gateway is modules/gateway,
        status.json). fresh — no older than ROUTE_EVERY (gateway on: notice at once when it is turned off or changes
        place), otherwise like the list (LIST_TTL)."""
        if self.sign_in() is None:
            return None
        got = self.listing(ROUTE_EVERY if fresh else None)
        return None if got is None else got.get("route") or {}

    def serves(self, gate):
        """The gateway of this place (gateway from profile.token) is on at the login module — its route."""
        r = self.route() or {}
        return bool(r.get("on")) and r.get("ident") == ident_of(gate["port"], gate["secret"])

    def names(self):
        """(pool names if it works, otherwise None; the switch threshold; names with a command token) — for the saving mode;
        the login module did not answer — as without a pool (extraction spec §3)."""
        got = self.listing() or {"on": False, "switch_at": 0.95, "profiles": []}
        return ([p["name"] for p in got["profiles"]] if got["on"] else None, got["switch_at"],
                {p["name"] for p in got["profiles"] if p.get("renewable")})

    def starting(self):
        """The login module has not answered profile.list since this process started, and has been silent for less than
        LIST_TTL: it is still starting, "no pool" is not known yet (the saving mode waits instead of judging by one login).
        After names() / listing(): the silence is counted from the first ask."""
        p = self.provider()
        return isinstance(p, Module) and p.good is None and p.silent_since is not None and \
            self.clock() - p.silent_since < LIST_TTL

    def gateway(self):
        """The built-in account gateway (modules/gateway) is the one in charge: it substitutes the built-in pool's tokens,
        and sessions on it work only with it (launch waiting, environment from its gateway.json). A login module has its
        own gateway — gateway in the profile.token answer and route in profile.list. No built-in gateway in the tree (the open
        flavor) — never."""
        return self.sign_in() is None and cutout.present("gateway")

    def token(self, name, session=None, kind="session"):
        """{env, gateway?} of profile name — or None (no such profile, no token, the module did not answer)."""
        return (self.provider().token(name, session, kind) or None) if name else None   # {} — no login

    def model_env(self, env, account):
        """The environment of the core's `claude -p` on the session login (modifies env). No pool, it is paused, no account
        set — this computer's login; the login module's pool works but the module did not give the profile's login —
        NoProfile (the call is skipped). If the module gave a gateway — through it, with the same environment as the
        sessions (otherwise, with a proxy, the request to 127.0.0.1 would go into the proxy)."""
        if not account:
            return env
        p = self.provider()
        got = p.token(account, None, "model")
        if got:   # {} — the profile has no login: as without an answer
            modlaunch.apply(env, got.get("env"))
            gate = got.get("gateway")
            # the module's gateway is not working (route) or a cloud provider — directly, like sessions
            if gate and not modlaunch.provider(env) and self.serves(gate):
                through(env, gate["port"], gate["secret"], stub=bool((got.get("env") or {}).get("CLAUDE_CODE_OAUTH_TOKEN")),
                        first_party=bool(gate.get("first_party")))
        elif isinstance(p, Module) and (p.listing() or {}).get("on") is not False:
            raise NoProfile(f"the sign-in module {p.name} gave no sign-in for the profile {account}")
        return env

    def gateway_summary(self, core, now, cfg=None):
        """The gateway for "Settings -> Accounts", hub doctor and hub accounts: (level ok|warn|bad|off, line). core is the
        core's st["gateway"]. The built-in — by its status.json; a login module — by its answer and the core state: silent
        means "not responding" (the last "on" is not "working")."""
        cfg = self.config() if cfg is None else cfg
        name = self.sign_in(cfg)
        if name is None and not cutout.present("gateway"):   # the open flavor: a gateway only with a login module
            return ("bad", i18n.t("core.profiles.gw_no_module")) if (core or {}).get("on") or gateway_enabled(cfg) \
                else ("off", i18n.t("core.profiles.gw_off"))
        if name is None:
            from modules.gateway import gateway as gw
            return gw.summary(cfg, gw.status_of(self.state_dir or _state_dir()), core, now)
        core = core or {}
        if self.listing() is None:
            return "bad", i18n.t("core.profiles.gw_silent_waiting" if core.get("on") else "core.profiles.gw_silent", name=name)
        if core.get("on"):
            return "ok", i18n.t("core.profiles.gw_ok", name=name)
        if core.get("why"):
            from core import coretext   # the core's phrases of the reason: in the reader's language
            return "bad", i18n.t("core.profiles.gw_down", why=coretext.human(coretext.got(core, "why")))
        return "off", i18n.t("core.profiles.gw_off")

    def decide(self, state, record):
        """What to write now: record(type, data) — the daemon writes with channel core; state() is the core state after
        each write. A switch with restart while the account gateway (built-in or module's) works, to a profile that is not
        the machine's login, is seamless: sessions on the gateway switch without restart (reducer)."""
        p = self.provider()
        shadow = self.shadow() if p is self.builtin and self.shadow else None

        def put(kind, data):
            gate = state().get("gateway") or {}
            if kind == "account.switched" and data.get("restart") and data.get("to") and gate.get("on") \
                    and (p is self.builtin or (self.route() or {}).get("ident") == gate.get("ident")):
                # the module's gateway counts only if it holds the place known to the core (not the built-in one still finishing its service)
                listed = (self.listing() or {}).get("profiles") or []
                if not next((x for x in listed if x["name"] == data["to"]), {}).get("login"):
                    data["seamless"] = True   # the gateway substitutes the pool token for sessions on it: no restart (reducer)
            record(kind, data)
            if isinstance(p, Module):
                p.list_at = None   # account.* was written — re-read the list
        if shadow:
            p.decide(state, put, lambda intent: self.safe_compare(state, shadow, intent))
        else:
            p.decide(state, put)

    def safe_compare(self, state, shadow, intent):
        """The shadow does not touch the built-in's decision: any comparison failure is a log line, the built-in writes its own."""
        try:
            self.compare(state, shadow, intent)
        except Exception as e:  # noqa: BLE001
            print(f"hubd: shadow of the login module {shadow[0]}: {type(e).__name__}: {e}"[:300], flush=True)

    def compare(self, state, shadow, intent):
        """Shadow: module shadow[0] gets the same question the built-in just decided (before its command token and the
        write); the comparison result goes to observe. It is asked when the built-in decided something, the login changed
        or the module answered long ago (DECIDE_EVERY). If the module does not answer, it does not count."""
        from core import providers
        from modules.accounts import accounts
        mod, now = self.module(*shadow), self.clock()
        st = state()
        question, mark = mod.question(st, now)
        ours = (intent["type"], {k: v for k, v in intent.items() if k != "type"}) if intent else None
        # the same intent on the same login (a switch waits for the command token) — no more often than DECIDE_EVERY: the tick does not
        # wait on the module, a repeat is not a new comparison
        if not mod.due(mark, now) and (intent is None or providers.norm(ours) == mod.intent):
            return
        got = mod.ask(question, mark, now)
        if got is None or not self.observe:
            return   # no answer — ask again with the same intent on the next tick
        mod.intent = providers.norm(ours)
        pool = self.builtin.pool()
        renewable = {a["name"] for a in pool["accounts"] if a.get("command")}
        # token fingerprints (gens) of profiles with a permanent token must be the same as the built-in's (accounts.fingerprint)
        fps = {a["name"]: accounts.fingerprint(a["token"]) for a in pool["accounts"]
               if a.get("token") and not a.get("command")}
        bad = sorted(n for n, g in (got.get("gens") or {}).items() if n in fps and fps[n] != g)
        if bad:
            self.observe("diverged", i18n.t("core.profiles.gens_differ", names=", ".join(bad)))
            return
        theirs = next(((k, d) for k, d in events(got, st.get("account") or {}, now) if k != "account.token"), None)
        kind, why = providers.compare(providers.norm(ours), providers.norm(theirs), renewable)
        self.observe(kind, why, list(providers.norm(ours)), list(providers.norm(theirs)))

    def use(self, acc, name, force=False):
        """Switch to name now (/api/accounts/use, hub accounts use): None — allowed; {code: unknown | paused | busy |
        none (the module did not answer), why?, until?, forceable?}."""
        return self.provider().use(acc, name, force, self.clock())

    def admits(self, sender, data):
        """account.limits from a module process (sender mod:<name>): written only by the login module, for its own profile.
        None — accept; otherwise the refusal reason."""
        name = self.sign_in()
        if not (isinstance(sender, str) and sender == f"mod:{name}" and name):
            return T("core.profiles.limits_by_login", "account.limits from {sender}: sign-in limits are written by the "
                     "host or the sign-in module (modules.use.profile)").format(sender=sender or A_MODULE)
        acc = (data or {}).get("account")
        if not isinstance(acc, str):
            return NOT_LISTED.format(sender=sender, account=repr(acc))
        # the cached list is no proof of absence: it may be the silence of a module that was still coming up (hubd restart)
        # or a list from before the profile was added — a miss is checked against a fresh answer, a hit stays cheap
        got = self.listing()
        if acc not in [x["name"] for x in (got or {}).get("profiles") or []]:
            mod = self.provider()
            got = mod.relist() if isinstance(mod, Module) else None
            if acc not in [x["name"] for x in (got or {}).get("profiles") or []]:
                return NOT_LISTED.format(sender=sender, account=repr(acc))
        return None


def _state_dir():
    from core import daemon
    return daemon.default_state_dir()


def model_env(env, account, state_dir=None):
    """The core's `claude -p` (watchdog, critic, version check) on the session login — through this call's facade."""
    return Profiles(state_dir).model_env(env, account)


# --- hub accounts use: the core writes the owner's or hub's word------------------------------------------------------------

def all_busy_note(st, at, now):
    """The line `hub accounts use` adds when the core has found the whole pool busy (account.exhausted, still in force):
    until when, and that an account can be added. None otherwise. at — time in the owner's zone."""
    until = ((st.get("account") or {}).get("exhausted") or {}).get("until")
    if isinstance(until, (int, float)) and not isinstance(until, bool) and until > now:
        return i18n.t("cli.accounts.use_all_busy", time=at(until))
    return None


def cli_use(args, st, put, me, cfg_path, now=None, state_dir=None, out=None):
    """`hub accounts use <name> [--force]` -> exit code; None — not this command (the module/built-in CLI parses it).
    Texts and the order of checks are as in the built-in (modules/accounts main)."""
    from core import catalog
    if args[:1] != ["use"]:
        return None
    say = out or (lambda s: print(s))
    rest = [a for a in args[1:] if a != "--token-stdin"]   # the old spelling of add — the built-in CLI strips it
    if me and (st["sessions"].get(me) or {}).get("kind") != "hub":
        say(i18n.t("core.profiles.owner_or_hub"))
        return 1
    if not (len(rest) in (1, 2) and (len(rest) == 1 or rest[1] == "--force")):
        return None
    force = len(rest) == 2
    now = now or time.time()
    prof = Profiles(state_dir, clock=lambda: now)
    listed = prof.listing()
    if listed is None:
        say(i18n.t("core.profiles.use_none"))
        return 1
    names = [p["name"] for p in listed["profiles"]]
    if listed.get("paused"):
        say(i18n.t("core.profiles.use_paused"))
        return 1
    if rest[0] not in names:
        say(i18n.t("core.profiles.use_not_in_pool", name=rest[0], names=", ".join(names) or i18n.t("core.profiles.pool_empty")))
        return 1
    bad = prof.use(st.get("account"), rest[0], force)
    from core.zones import zone
    from datetime import datetime
    cfg = catalog.read_settings(cfg_path)
    tz = (st.get("owner") or {}).get("tz") or cfg.get("timezone") or "UTC"
    at = (lambda t: datetime.fromtimestamp(t, zone(tz)).strftime("%d.%m %H:%M"))
    busy = all_busy_note(st, at, now)
    if bad and bad["code"] == "busy":
        parts = [bad.get("why") or i18n.t("core.profiles.busy")]
        if isinstance(bad.get("until"), (int, float)):
            parts.append(i18n.t("core.profiles.busy_until", time=at(bad["until"])))
        say(", ".join(parts) + (" — " + i18n.t("core.profiles.force_hint") if bad.get("forceable") else ""))
        if busy:
            say(busy)
        return 1
    if bad:
        key = {"paused": "core.profiles.use_paused", "unknown": "core.profiles.use_unknown",
               "none": "core.profiles.use_none"}.get(bad["code"])
        say(i18n.t(key) if key else str(bad["code"]))
        return 1
    put("account.switched", None, {"to": rest[0], "from": (st.get("account") or {}).get("active"), "restart": True,
                                   "manual": True,
                                   "why": i18n.tr(i18n.owner(), "core.profiles.picked_manually_forced" if force
                                                  else "core.profiles.picked_manually"),
                                   **({"force": True} if force else {})})
    say(i18n.t("cli.accounts.use_done", name=rest[0]))
    if busy:
        say(busy)
    return 0


# --- point answer schemas (core/modrun.py POINTS) ----------------------------------------------------------------------

def _number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _names(v, what):
    if not (isinstance(v, dict) and all(isinstance(k, str) and NAME.fullmatch(k) for k in v)):
        raise BadAnswer(f"{what}: an object {{profile: ...}}")
    return v


def check_list(res, ctx):
    if not (isinstance(res, dict) and isinstance(res.get("on"), bool) and _number(res.get("switch_at"))
            and 0.5 <= res["switch_at"] <= 1 and isinstance(res.get("profiles"), list)
            and len(res["profiles"]) <= MAX_PROFILES):
        raise BadAnswer(f"profile.list: {{on, switch_at 0.5-1, profiles: [up to {MAX_PROFILES}]}}")
    out, seen = [], set()
    for p in res["profiles"]:
        if not (isinstance(p, dict) and isinstance(p.get("name"), str) and NAME.fullmatch(p["name"])
                and p["name"] not in seen and isinstance(p.get("email", ""), str)
                and all(isinstance(p.get(k, False), bool) for k in ("login", "renewable", "paused"))):
            raise BadAnswer("profile.list: profiles — [{name, email?, login?, renewable?, paused?}], names are unique")
        seen.add(p["name"])
        out.append({"name": p["name"], **({"email": p["email"][:254]} if p.get("email") else {}),
                    **{k: True for k in ("login", "renewable", "paused") if p.get(k)}})
    if not isinstance(res.get("paused", False), bool):
        raise BadAnswer("profile.list: paused — true|false")
    ready = res.get("ready")
    if ready is not None and not (isinstance(ready, dict) and set(ready) <= set(READY)
                                  and all(v in READY_VALUES for v in ready.values())):
        raise BadAnswer(f"profile.list: ready — {{{', '.join(READY)}: {'|'.join(READY_VALUES)}}}")
    return {"on": res["on"], "switch_at": res["switch_at"], "profiles": out,
            **({"paused": True} if res.get("paused") else {}),
            **({"route": _route(res["route"])} if "route" in res else {}),
            **({"ready": dict(ready)} if ready is not None else {})}


def _route(r):
    """The module's gateway: {on, ident (when on), why?, code?}; ident is the place, ident_of the port and secret (the
    secret is not in the list): anything else would match no profile.token answer."""
    if not (isinstance(r, dict) and isinstance(r.get("on"), bool)
            and (r.get("ident") is None and not r["on"] or isinstance(r.get("ident"), str) and IDENT.fullmatch(r["ident"]))
            and isinstance(r.get("why", ""), str) and isinstance(r.get("code", ""), str)):
        raise BadAnswer("profile.list: route — {on: true|false, ident (ident_of the port and secret, when on), why?, code?}")
    return {"on": r["on"], **({"ident": r["ident"]} if r["on"] else {}),
            **({"why": r["why"][:300]} if r.get("why") else {}), **({"code": r["code"][:40]} if r.get("code") else {})}


def check_decide(res, ctx):
    """{} — nothing; one of: a switch (switch_to), "all busy" (exhausted), "wait for reset" (waiting); gens — generations."""
    if not isinstance(res, dict):
        raise BadAnswer("profile.decide: an object")
    out = {}
    if "gens" in res:
        out["gens"] = {k: v for k, v in _names(res["gens"], "profile.decide: gens").items()}
        if not all(isinstance(v, str) and 0 < len(v) <= 64 for v in out["gens"].values()):
            raise BadAnswer("profile.decide: gens - {profile: a string of up to 64 characters}")
    if "renewed" in res:
        out["renewed"] = {k: v for k, v in _names(res["renewed"], "profile.decide: renewed").items()}
        if not all(isinstance(v, str) and 0 < len(v) <= 64 for v in out["renewed"].values()):
            raise BadAnswer("profile.decide: renewed - {profile: a string of up to 64 characters}")
    kinds = [k for k in ("switch_to", "exhausted", "waiting") if k in res and (k == "switch_to" or res[k])]
    if len(kinds) > 1:
        raise BadAnswer("profile.decide: one of switch_to, exhausted, waiting")
    reason = res.get("reason", "")
    if not isinstance(reason, str):
        raise BadAnswer("profile.decide: reason - a string")
    reason = reason[:300]
    blocked = _names(res.get("blocked") or {}, "profile.decide: blocked")
    if not all(_number(t) for t in blocked.values()):
        raise BadAnswer("profile.decide: blocked - {profile: time}")
    if not kinds:
        return out
    if kinds == ["switch_to"]:
        to = res["switch_to"]
        if not (to is None or isinstance(to, str) and NAME.fullmatch(to)):
            raise BadAnswer("profile.decide: switch_to - a profile name or null (the default sign-in)")
        if not all(isinstance(res.get(k, False), bool) for k in ("restart", "refresh", "fresh", "free", "reset")):
            raise BadAnswer("profile.decide: restart, refresh, fresh, free, reset — true|false")
        return {**out, "switch_to": to, "reason": reason, "blocked": blocked,
                **{k: True for k in ("restart", "refresh", "fresh", "free", "reset") if res.get(k)}}
    if not _number(res.get("until")):
        raise BadAnswer(f"profile.decide: {kinds[0]} - with until (a time)")
    if kinds == ["exhausted"]:
        return {**out, "exhausted": True, "until": res["until"], "blocked": blocked, "reason": reason}
    if not (isinstance(res.get("account"), str) and NAME.fullmatch(res["account"])):
        raise BadAnswer("profile.decide: waiting - with account (a profile)")
    return {**out, "waiting": True, "until": res["until"], "account": res["account"], "reason": reason}


def check_token(res, ctx):
    """{env, gateway?}: env holds only claude login keys (an empty value removes the variable) — the address, network and
    the rest are not given to a profile; gateway is the module's gateway on 127.0.0.1 {port, secret, first_party?}: the
    core sets the address and the environment for it (through). {} — the profile has no login (no such profile, no token
    yet): not the same as {"env": {}} — this machine's login marked with the profile."""
    if res == {}:
        return {}
    if not (isinstance(res, dict) and isinstance(res.get("env"), dict) and not set(res) - {"env", "gateway"}):
        raise BadAnswer("profile.token: {env: {name: string}, gateway?} or {} - the profile has no sign-in")
    gate = res.get("gateway")
    if gate is not None and not (
            isinstance(gate, dict) and not set(gate) - {"port", "secret", "first_party"}
            and isinstance(gate.get("port"), int) and not isinstance(gate["port"], bool) and 0 < gate["port"] < 65536
            and isinstance(gate.get("secret"), str) and SECRET.fullmatch(gate["secret"])
            and isinstance(gate.get("first_party", False), bool)):
        raise BadAnswer("profile.token: gateway - {port 1-65535, secret [A-Za-z0-9_-]{16,256}, first_party?}")
    env = res["env"]
    bad = [k for k, v in env.items() if not (isinstance(k, str) and modules.ENV_NAME.fullmatch(k)
                                             and modlaunch.login_name(k) and not modules.provider_name(k)
                                             and isinstance(v, str))]
    if bad:
        raise BadAnswer(f"profile.token: {bad[0]!s:.60} - not a claude sign-in key ({', '.join(modlaunch.AUTH_ENV)})")
    return {"env": dict(env), **({"gateway": {"port": gate["port"], "secret": gate["secret"],
                                              **({"first_party": True} if gate.get("first_party") else {})}}
                                 if gate is not None else {})}


def check_use(res, ctx):
    if isinstance(res, dict) and res.get("ok") is True:
        return {"ok": True}
    if not (isinstance(res, dict) and res.get("error") in ("unknown", "paused", "busy")
            and isinstance(res.get("why", ""), str) and (res.get("until") is None or _number(res["until"]))
            and isinstance(res.get("forceable", False), bool)):
        raise BadAnswer("profile.use: {ok: true} or {error: unknown|paused|busy, why?, until?, forceable?}")
    return {"error": res["error"], **({"why": res["why"][:300]} if res.get("why") else {}),
            **({"until": res["until"]} if res.get("until") is not None else {}),
            **({"forceable": True} if res.get("forceable") else {})}
