"""Who handles session login, the reply voice and the stats report — the built-in core code or a module (module
extraction spec §4.3, Module API 2.8). State lives in the core: st["providers"][<capability>] is the fold of the
module.provider event, written only by hubd (the Tick step), and of the bot fact speech.said (two failed voicings by
a module in a row — back to the built-in).

    profile:  builtin -> shadow -> warm -> module;  any -> builtin (module paused, stopped, removed, divergence);
              builtin -> warm again for the version that already led sign-in here (passed): a pause is not a new shadow
    speech:   builtin -> module;  module -> builtin (pause, two voicing failures in a row)
    stats:    builtin -> module;  module -> builtin (pause, module failure; three failed sends in a row — for STATS_HOLD)

- builtin — the code in the core tree handles it; a module, if there is one, stays in the shadow (mode "shadow"): it
  only reads and answers.
- shadow (profile only) — the built-in handles it; the module is asked the same questions, the answers are compared and
  not applied. After SHADOW_SECONDS and SHADOW_MIN comparisons without divergence, with a list and token check — warm.
  Divergence — back; the module of that version is no longer a candidate (hold) until another version arrives or the
  owner says `hub modules reset <name>`.
- warm — the module may do side effects (command tokens, conduit; mode "warm"), decisions are still the built-in's, the
  gateway is not handed over; the module says ready in profile.list — module, not ready within WARM_SECONDS — back (hold).
- module — the module handles it (mode "live"); the built-in gateway yields to it (core/profiles, modules/gateway).
The owner's knob is modules.use.<capability> in hub.json: "builtin" keeps the built-in, a module name picks that module
at once (no shadow; paused — the built-in, back — it again). Not named — a candidate: the only ready module with that
capability; on the app (core/packaged.py) there is no candidate: the built-in handles what the owner did not name. Local edits to the built-in code of this function (§5.9) keep it on the built-in: the edit would stop taking effect.

For processes without their own journal reader (watchdog, critic, command, stats report, gateway) hubd puts a copy of
st["providers"] in <state>/modrun/providers.json (like the want-files for hub-up) — current().
"""
import copy
import hashlib
import json
import os
import time

from core import cutout, embed, i18n, modules
from core.coretext import T
from core.tools import Rejected

CAPS = ("profile", "speech", "stats")
STATES = {"profile": ("builtin", "shadow", "warm", "module"), "speech": ("builtin", "module"),
          "stats": ("builtin", "module")}
NEEDS = {"profile": ("profile.list", "profile.decide", "profile.token", "profile.use"), "speech": ("speech.say",),
         "stats": ()}
FILE = "providers.json"   # in the modrun folder
EVERY = 5                 # hubd step no more often than, seconds
# The numbers are starting settings, not measurements (extraction spec §8.1: revisited from the C6 benches and R1 observation).
SHADOW_SECONDS = 86400    # shadow needs at least this many seconds of hubd observation (spec §4.3: 24 h); hubd downtime does not count
SHADOW_MIN = 60           # and at least this many decision comparisons (one per 30 s — half an hour of work), "empty" among them
SHADOW_CHECK = 600        # profile list and token check — on entering the shadow and no less often than this
WARM_SECONDS = 1800       # warm: the module must say ready no later than this
SEEN_EVERY = 3600         # the comparison count goes to the journal at the first sight of an answer kind and no more often
EDITS_EVERY = 3600        # recount local edits of the built-in no more often
DOCTOR_RETRY = 60         # the candidate's doctor did not answer — ask again no sooner
SPEECH_FAILS = 2          # failed voicings by a module in a row — back to the built-in voice
STATS_FAILS = 3           # failed report sends by a module in a row — back to the built-in report
# why a function went back to the built-in (the cause field of module.provider state=builtin; the stats report counts by it, §6.2).
# planned — the owner's word or the hub's own condition; trial — the module did not pass the shadow or the warm-up (it never handled
# the function); fault — the module failed while it handled it or could not be called
CAUSES = {"owner": "planned", "removed": "planned", "edits": "planned", "model": "planned", "app": "planned",
          "shadow": "trial", "paused": "fault", "stopped": "fault", "blocked": "fault", "fail": "fault"}
STATS_HOLD = 6 * 3600     # ...for this long: sends that did not arrive are mostly the network or the server, then the module again
KINDS = ("empty", "switch", "exhausted", "waiting")   # kinds of profile.decide answer — shadow coverage count
# the built-in implementation that moves into modules (§5.9): editing it on a hub stops taking effect once it switches to a module.
# Tests are not implementation: editing them does not hold
EDITS = {"profile": ("modules/accounts", "modules/gateway", "modules/conduit"),
         "speech": ("modules/voice/speak.py",),
         "stats": ("modules/stats",)}


def builtin(cap):
    """The built-in code of cap is in the tree. Without it (the open flavor, core/cutout.py) "builtin" means nobody: a ready
    module leads at once (there is nothing to compare it with, no shadow or warm-up), a pause after failures and failed
    voicings or sends keep it (there is no other one to hand the function to)."""
    return cutout.present(cap)


# --- reading: who handles it now ----------------------------------------------------------------------------------------

def named(cfg, cap):
    """What the owner said in hub.json modules.use.<cap>: "builtin", a module name or None (said nothing)."""
    v = (modules.settings(cfg).get("use") or {}).get(cap)
    return v if isinstance(v, str) and (v == "builtin" or modules.NAME.fullmatch(v)) else None


def active(cap, cfg, recs):
    """The module that handles cap now, or None — the built-in. recs is st["providers"] (or current())."""
    use = named(cfg, cap)
    if use == "builtin":
        return None
    rec = (recs or {}).get(cap) or {}
    mod = rec.get("module") if isinstance(rec.get("module"), str) else None
    if use:   # the owner's word: this module, until hubd records that it is paused (the built-in stands in for it)
        return None if mod == use and rec.get("state") == "builtin" else use
    return mod if rec.get("state") == "module" else None


def modes(name, caps, cfg, recs):
    """{capability: live | warm | shadow} of module name by its capabilities caps: live — it handles this function; warm —
    session login is in warm with it; shadow — otherwise: only read and answer, no side effects (token commands,
    keychain, gateway, report sending)."""
    out = {}
    for c in (c for c in CAPS if c in (caps or ())):
        rec = (recs or {}).get(c) or {}
        out[c] = ("live" if active(c, cfg, recs) == name else
                  "warm" if c == "profile" and rec.get("state") == "warm" and rec.get("module") == name else "shadow")
    return out


def mode(name, caps, cfg, recs, got=None):
    """One mode per module — the strictest among the functions it handles or is being tried on (a candidate in shadow,
    warm): a module with login in the shadow and voice in service is shadow (login side effects — token commands, the
    gateway — are forbidden while login is in the shadow). A function it has nothing to do with (the hub or another
    module handles it) does not spoil the mode."""
    got = modes(name, caps, cfg, recs) if got is None else got
    mine = [m for c, m in got.items() if m == "live" or ((recs or {}).get(c) or {}).get("module") == name
            and ((recs or {}).get(c) or {}).get("state") in ("shadow", "warm")]
    return min(mine, key=("shadow", "warm", "live").index) if mine else "shadow"


def path(state_dir):
    return os.path.join(state_dir, "modrun", FILE)


_read = {}   # path -> (mtime_ns, size, what was read): the page and gateway ask often — the file changes rarely


def current(state_dir):
    """st["providers"] from hubd's copy — for processes without their own journal reader. No file — {} (all built-in)."""
    p = path(state_dir)
    try:
        st = os.stat(p)
        mark = (st.st_mtime_ns, st.st_size, st.st_ino)   # the file is replaced whole: a new inode on every write
        if (_read.get(p) or (None,))[0] == mark:
            return copy.deepcopy(_read[p][1])
        with open(p, encoding="utf-8") as f:
            got = json.load(f)
    except (OSError, ValueError):
        return {}
    got = got if isinstance(got, dict) else {}
    _read[p] = (mark, got)
    return copy.deepcopy(got)


def write(state_dir, recs):
    from core import catalog
    os.makedirs(os.path.dirname(path(state_dir)), exist_ok=True)
    catalog.write_private(path(state_dir), recs or {})


LABEL = {"profile": "core.providers.label_profile", "speech": "core.providers.label_speech",   # catalog keys
         "stats": "core.providers.label_stats"}
NONE_LABEL = {"profile": "core.providers.none_profile", "speech": "core.providers.none_speech",   # catalog keys
              "stats": "core.providers.none_stats"}
STATE_LABEL = {"builtin": "core.providers.state_builtin", "shadow": "core.providers.state_shadow",   # catalog keys
               "warm": "core.providers.state_warm", "module": "core.providers.state_module"}


def lines(recs):
    """Lines of `hub modules`: who handles each function; where it is a module — with the reason and hold."""
    out = []
    for cap in CAPS:
        rec = (recs or {}).get(cap)
        if not rec:
            continue
        who = i18n.t(STATE_LABEL[rec["state"]]) if rec.get("state") in STATE_LABEL else rec.get("state")
        if rec.get("state") == "builtin" and not builtin(cap):
            who = i18n.t(NONE_LABEL[cap])   # the open flavor: no built-in code, nobody leads it
        if rec.get("state") != "builtin" and rec.get("module"):
            who += f" {rec['module']}"
        seen = rec.get("seen") or {}
        count = sum(v for k, v in seen.items() if k in KINDS)
        extra = [i18n.t("core.providers.compared", count=count)] if rec.get("state") == "shadow" and count else []
        if rec.get("why"):
            extra.append(rec["why"])
        hold = rec.get("hold") if builtin(cap) else None   # without the built-in code a hold is not in force
        if hold and hold.get("until"):
            if rec.get("state") == "builtin":   # the module is back — a past time is not a warning
                extra.append(i18n.t("core.providers.retry_after", module=hold["module"],
                                    time=time.strftime("%d.%m %H:%M", time.localtime(hold["until"]))))
        elif hold:
            extra.append(i18n.t("core.providers.held_until_new", module=hold["module"],
                                version=hold.get("version") or "").replace("  ", " "))
        out.append(f"{i18n.t(LABEL[cap])}: {who}" + (f" — {'; '.join(extra)}" if extra else ""))
    return out


# --- fold ---------------------------------------------------------------------------------------------------------------

def _allowed(cap, was, to, data):
    if to == "builtin" or to == was:
        return True
    if to == "shadow":
        return was == "builtin"
    if to == "warm":   # from the shadow; or again — a module of the version that already led sign-in on this hub (passed)
        return was == "shadow" or was == "builtin" and data.get("again") is True
    # module: from warm (session login), from builtin (voice, report), by the owner's word (modules.use.<cap> — a name) or
    # from any state when the tree has no built-in code of it (bare: the core's fact — the fold reads no disk)
    return was == "warm" or cap != "profile" and was == "builtin" or bool(data.get("named")) or data.get("bare") is True


def apply(st, ev, data):
    """module.provider {cap, state, module?, why?, version?, seen?, hold?, named?, bare?} — only from the core (channel core)."""
    if data.get("channel") != "core":
        raise Rejected(T("core.providers.core_writes", "the core writes the provider of a function"))
    cap, to = data.get("cap"), data.get("state")
    if cap not in CAPS or to not in STATES[cap]:
        raise Rejected(T("core.providers.bad_cap", "module.provider: cap — {caps}, state — one of {states}").format(
            caps=", ".join(CAPS), states=str(STATES.get(cap, ()))))
    mod = data.get("module")
    if mod is not None and not (isinstance(mod, str) and modules.NAME.fullmatch(mod)):
        raise Rejected(T("core.providers.bad_module", "module.provider: module — a module name"))
    if to != "builtin" and mod is None:
        raise Rejected(T("core.providers.needs_module", "module.provider: not the built-in one — with a module name"))
    recs = st.setdefault("providers", {})
    was = recs.get(cap) or {"state": "builtin"}
    if not _allowed(cap, was.get("state"), to, data):
        raise Rejected(T("core.providers.no_transition", "module.provider {cap}: {was} → {to} — no such transition")
                       .format(cap=cap, was=str(was.get("state")), to=to))
    rec = {"state": to, "since": was.get("since") if (to, mod) == (was.get("state"), was.get("module")) else ev["ts"]}
    for k in ("module", "why", "version"):
        if isinstance(data.get(k), str) and data[k]:
            rec[k] = data[k][:300]
    if to == "shadow" and isinstance(data.get("seen"), dict):
        rec["seen"] = {k: int(v) for k, v in data["seen"].items() if k in KINDS + ("checks", "seconds")
                       and isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0}
    if to == "module" and was.get("state") == "module" and was.get("module") == mod and was.get("proved"):
        rec["proved"] = was["proved"]
    if cap == "profile":   # the version that led sign-in here: after a pause it returns through warm, not a new shadow
        passed = {"module": mod, "version": rec.get("version") or ""} if to == "module" and was.get("state") == "warm" \
            else was.get("passed")   # only one that came through warm: the owner's word skips the shadow, it does not pass it
        if passed is None and to == "builtin" and was.get("state") == "module" and isinstance(was.get("module"), str):
            # led sign-in before the core kept `passed` (an older release): it came through the shadow here — a pause or
            # a failure returns it through warm; a divergence (a hold below) and a new version still go to the shadow
            passed = {"module": was["module"], "version": was.get("version") or ""}
        if isinstance(data.get("hold"), dict) and data["hold"].get("kind", "shadow") != "fail":
            passed = None   # rejected in warm or on a divergence: a reset of the hold does not skip the shadow
        if isinstance(passed, dict) and isinstance(passed.get("module"), str):
            rec["passed"] = {"module": passed["module"][:40], "version": str(passed.get("version") or "")[:40]}
    hold = data.get("hold") if isinstance(data.get("hold"), dict) else was.get("hold")
    # bare: True — set, False — cleared (the built-in code is back in the tree), absent — kept for the same module
    bare = to == "module" and (data.get("bare") is True or data.get("bare") is None and was.get("bare") is True
                               and (was.get("state"), was.get("module")) == ("module", mod))
    if bare:   # no built-in code in the tree: nothing to hand back to, so no hold either (said keeps the module)
        rec["bare"], hold = True, None
    if isinstance(hold, dict) and isinstance(hold.get("module"), str):
        rec["hold"] = {"module": hold["module"][:40], "version": str(hold.get("version") or "")[:40],
                       "why": str(hold.get("why") or "")[:300],
                       # fail — the module failed in service (voice, report): holds even one named by the owner; shadow — the shadow
                       "kind": "fail" if hold.get("kind") == "fail" else "shadow"}
        if isinstance(hold.get("until"), (int, float)) and not isinstance(hold["until"], bool):
            rec["hold"]["until"] = hold["until"]   # a hold for a while (failed report sends), not until a new version
    recs[cap] = rec


def said(st, ev, data):
    """speech.said {module, key, ok, why?} from the bot: the voice module that handles the voice did or did not voice a reply.
    SPEECH_FAILS failures in a row — back to the built-in, the module of that version goes to hold. A fact; only the count changes state."""
    if ev.get("src") != "telegram":
        raise Rejected(T("core.providers.said_by_bot", "voicing is marked by the Telegram bot"))
    rec = (st.get("providers") or {}).get("speech")
    if not rec or rec.get("state") != "module" or rec.get("module") != data.get("module") or data.get("ok") is None:
        return   # ok None — the module produced a voice message but Telegram did not accept it: not the module's fault
    if data.get("ok") is True:
        rec.pop("fails", None)
        rec.setdefault("proved", ev["ts"])   # the module's first voice message reached the owner — a sign of readiness (§6.2)
        return
    rec["fails"] = int(rec.get("fails") or 0) + 1
    if rec["fails"] >= SPEECH_FAILS and not rec.get("bare"):   # bare: no built-in voice to hand it to — it stays
        why = i18n.tr(i18n.state_lang(st), "core.providers.speech_failed", count=SPEECH_FAILS,
                      why=str(data.get("why") or "")[:200])
        version = ((st.get("modules") or {}).get(rec["module"]) or {}).get("version") or ""
        st["providers"]["speech"] = {"state": "builtin", "since": ev["ts"], "module": rec["module"], "why": why,
                                     "hold": {"module": rec["module"], "version": version, "why": why, "kind": "fail"}}


def unhold(st, name):
    """hub modules reset <name> — the module can be a candidate again."""
    for rec in (st.get("providers") or {}).values():
        if (rec.get("hold") or {}).get("module") == name:
            rec.pop("hold")


# --- local edits to the built-in (§5.9) ---------------------------------------------------------------------------------

def local_edits(repo, cfg):
    """{capability: [paths]} — the hub's own changes in the built-in implementation of this function on top of the release
    it stands on (the base is the common ancestor with the update branch, as in the stats report; if absent — the nearest
    release tag v*; if absent too — its own HEAD: only uncommitted changes). Not git (a package) — {}: there is no own
    code there. An edit to the built-in takes effect only after the hub processes restart, and hubd counts edits at start —
    there is no window when an edit acts unchecked."""
    import platforms
    import subprocess

    def git(*args):
        try:
            out = platforms.current.run(["git", "-C", repo, *args], 10, capture_output=True, text=True)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return out.stdout if out.returncode == 0 else None

    if not git("rev-parse", "HEAD"):
        return {}
    up = (cfg or {}).get("update") if isinstance((cfg or {}).get("update"), dict) else {}
    ref = f"{up.get('remote') or 'origin'}/{up.get('branch') or 'main'}"
    base = (git("merge-base", "HEAD", ref) or git("describe", "--tags", "--abbrev=0", "--match", "v*") or "HEAD").strip()
    every = [p for ps in EDITS.values() for p in ps]
    every += [f":(glob)embed/*/{p}" + ("/**" if "." not in os.path.basename(p) else "") for p in every]   # an embed's
    changed = set((git("diff", "--name-only", "--no-renames", "-z", base, "--", *every) or "").split("\0"))
    changed |= set((git("ls-files", "--others", "--exclude-standard", "-z", "--", *every) or "").split("\0"))
    out = {}
    for cap, roots in EDITS.items():
        hit = sorted(p for p in changed if p and any(q == r or q.startswith(r + "/") for r in roots
                                                     for q in (p, embed.core_path(p)))
                     and "/tests/" not in p and not os.path.basename(p).startswith("test_"))
        if hit:
            out[cap] = hit
    return out


# --- shadow comparison (session login) ----------------------------------------------------------------------------------

def _secs(t):
    return round(t) if isinstance(t, (int, float)) and not isinstance(t, bool) else t


def norm(event):
    """A decision for comparison: (kind, …) without reason texts, fingerprints and core marks (seamless, fresh, reset)."""
    if not event:
        return ("empty",)
    kind, d = event
    blocked = tuple(sorted((n, _secs(t)) for n, t in (d.get("blocked") or {}).items()))
    if kind == "account.switched":
        return ("switch", d.get("to"), bool(d.get("restart")), bool(d.get("free")), bool(d.get("refresh")), blocked)
    if kind == "account.exhausted":
        return ("exhausted", _secs(d.get("until")), blocked)
    if kind == "account.waiting":
        return ("waiting", d.get("account"), _secs(d.get("until")))
    return ("other", kind)


def compare(ours, theirs, renewable):
    """(kind, None) — matched; ("skip", None) — not comparable; ("diverged", why). ours is the built-in's intent
    (accounts.decide before the command token), theirs is the module's answer to the same input. The module fetches
    command tokens itself and answers with a switch only when the token already exists: its "nothing" against the
    built-in's switch to a profile with a command or to a fresh token (refresh) is not a divergence."""
    if ours == theirs:
        return ours[0], None
    involves = ours[0] == "switch" and (ours[4] or ours[1] in renewable)
    if theirs == ("empty",) and involves:
        return "skip", None
    return "diverged", i18n.t("core.providers.compare_diverged", ours=ours, theirs=theirs)


def env_print(env):
    """Fingerprint of the login environment: only non-empty login keys (the gateway address and secret are not here)."""
    from core import modlaunch
    keys = {k: v for k, v in sorted((env or {}).items()) if v and modlaunch.login_name(k)}
    return hashlib.sha256(json.dumps(keys, sort_keys=True).encode()).hexdigest()[:16]


def list_print(listing):
    """The profile list for comparison: whether the pool works, the threshold, the pause, names with machine-login and command flags."""
    listing = listing or {}
    return (bool(listing.get("on")), listing.get("switch_at"), bool(listing.get("paused")),
            tuple((p["name"], bool(p.get("login")), bool(p.get("renewable"))) for p in listing.get("profiles") or []))


# --- hubd step -----------------------------------------------------------------------------------------------------------

class Tick:
    """The providers machine in hubd. hub is core.daemon.Hub: state, config, record, clock, modules (Supervisor), profiles,
    state_dir. Decisions rest on module state (ready, pause, version), the candidate's doctor (once per launch), shadow
    comparisons (Profiles.decide calls observe) and local edits to the built-in.

    An explicit module name (modules.use.<capability>) is the owner's word: it bypasses the shadow, its hold and local edits; paused,
    removed, stopped — the built-in, back — it again; failed in service (hold "fail": voice, report) — waits for a new version
    (failed report sends — only STATS_HOLD). Not named — the candidate passes everything: shadow (session login), hold, edits."""

    def __init__(self, hub, repo=None):
        from core import catalog
        self.hub, self.repo = hub, repo or catalog.REPO
        self.at = None
        self.doctors = {}     # name -> (launch, whether green, when asked)
        self.edits, self.edits_at = {}, None
        self.seen, self.seen_of, self.seen_at, self.checked_at = {}, None, None, None   # shadow count (with the journal)
        self.written = None
        from core import packaged
        # the app (app spec §3.3): the four functions are its built-in code — no candidate module, no shadow; only the
        # owner's explicit modules.use.<capability> hands one to a module of their own
        self.app = packaged.is_app(packaged.info())

    # what is visible about modules
    def recs(self):
        return self.hub.state.get("providers") or {}

    def mods(self):
        return self.hub.state.get("modules") or {}

    def ready(self, name, wanted):
        return self.hub.modules.mods.ready(name, wanted) is not None

    def gone(self, name, wanted, cap=None):
        """Why module name can no longer handle it (disabled, removed, paused, stopped, did not start) — (cause, text) or None.
        Not up yet after a hub restart is not a reason: wait for it (session start waits for it, modrun.pending)."""
        if name not in wanted:
            return "removed", i18n.t("core.providers.gone_removed", name=name)
        st = self.hub.modules.mods.status(name)
        if st == "suspended" and cap and not builtin(cap):
            return None   # paused after failures: its process keeps serving, and there is nobody else (session starts wait)
        if st != "ok":
            cause, key = {"suspended": ("paused", "core.providers.gone_suspended"),
                          "error": ("stopped", "core.providers.gone_stopped"),
                          "blocked": ("blocked", "core.providers.gone_blocked")}.get(st, ("stopped", None))
            return cause, i18n.t(key, name=name) if key else st
        return None

    def lacks(self, cap, name, wanted):
        """Voice: the module asks for a model (capability model) and the owner has not allowed it (modules.model) — instead
        of condensing it would give raw text, and that would not count as a failure: it cannot handle it. (cause, text) or None."""
        if cap == "speech" and "model" in ((wanted.get(name) or {}).get("capabilities") or []) \
                and name not in (modules.settings(self.hub.config).get("model") or []):
            return "model", i18n.t("core.providers.no_model", name=name)
        return None

    def candidate(self, cap, wanted):
        """The only ready module with capability cap that declared the needed points, or None (none or two — choose in
        modules.use)."""
        recs = self.mods()
        have = sorted(n for n, w in wanted.items() if cap in (w.get("capabilities") or [])
                      and set(NEEDS[cap]) <= set((recs.get(n) or {}).get("points") or []) and self.ready(n, wanted))
        return have[0] if len(have) == 1 else None

    def doctor(self, name):
        """The candidate's doctor is green (all checks ok; no such point — nothing to check): True / False / None — no answer.
        Once per process launch; no answer — again after DOCTOR_RETRY."""
        rec = self.mods().get(name) or {}
        launch, now = rec.get("ready"), self.hub.clock()
        got = self.doctors.get(name)
        if got and got[0] == launch and (got[1] is not None or now - got[2] < DOCTOR_RETRY):
            return got[1]
        if "doctor" not in (rec.get("points") or []):
            ok = True
        else:
            from core import version
            res = self.hub.modules.mods.call(name, "doctor", {"api": version.MODULE_API})
            ok = None if res is None else all(c["ok"] for c in res)
        self.doctors[name] = (launch, ok, now)
        return ok

    def held(self, cap, name, kind=None):
        hold = (self.recs().get(cap) or {}).get("hold") or {}
        return hold.get("module") == name and hold.get("version") == ((self.mods().get(name) or {}).get("version") or "") \
            and (kind is None or hold.get("kind") == kind) and not (hold.get("until") and self.hub.clock() >= hold["until"])

    def put(self, cap, state, module=None, why="", **extra):
        data = {"cap": cap, "state": state, **({"module": module} if module else {}), "why": why, "channel": "core",
                **extra}
        if module and state != "builtin":
            data.setdefault("version", (self.mods().get(module) or {}).get("version") or "")
        if state == "shadow" and cap == "profile":
            data["seen"] = {k: round(v) if k == "seconds" else v for k, v in self.seen.items()}
            self.seen_at = self.hub.clock()
        self.hub.record("module.provider", None, data)

    def back(self, cap, module, why, hold=False, cause=None, until=None):
        """Back to the built-in; hold — the module of that version is no longer a candidate: "shadow" (divergence, not ready in warm) or
        "fail" (failed in service — holds even one named by the owner); until — only until then, not until a new version. cause —
        one of CAUSES (the reason as a code, no text)."""
        extra = {"hold": {"module": module, "version": (self.mods().get(module) or {}).get("version") or "",
                          "why": why, "kind": hold if hold in ("shadow", "fail") else "shadow",
                          **({"until": until} if until else {})}} if hold and module else {}
        if cap == "profile":
            self.seen, self.seen_of, self.checked_at = {}, None, None
        self.put(cap, "builtin", module, why[:300], **({"cause": cause} if cause in CAUSES else {}), **extra)

    def diverged(self, module, hook, why, ours=None, theirs=None):
        """Shadow divergence: module.error kind=shadow (does not count toward pauses) and back to the built-in with hold."""
        self.hub.record("module.error", module, {"module": module, "hook": hook, "kind": "shadow", "error": why[:500],
                                                 **({"ours": ours} if ours is not None else {}),
                                                 **({"theirs": theirs} if theirs is not None else {}),
                                                 "channel": "core", "owner": False})
        self.back("profile", module, i18n.t("core.providers.diverged", hook=hook, why=why), hold=True, cause="shadow")

    # shadow comparisons (Profiles.decide)
    def shadowing(self):
        """(module, mode) — session login is in the shadow or warm with a module: it is asked the same questions; None — no shadow."""
        rec = self.recs().get("profile") or {}
        if rec.get("state") in ("shadow", "warm") and named(self.hub.config, "profile") is None:
            return rec.get("module"), "warm" if rec["state"] == "warm" else "shadow"
        return None

    def restore(self):
        """The shadow count comes from the journal (field seen) if this hubd life has not counted yet: a restart loses only
        what was unsaved (at most SEEN_EVERY), hubd downtime does not count toward observation."""
        rec = self.recs().get("profile") or {}
        key = (rec.get("module"), rec.get("since"))
        if self.seen_of != key:
            self.seen_of = key
            self.seen = dict(rec.get("seen") or {}) if rec.get("state") == "shadow" else {}
            self.seen_at = self.hub.clock()

    def observe(self, kind, why=None, ours=None, theirs=None):
        """The result of one decision comparison: a kind from KINDS, "skip" (not comparable) or "diverged" (why)."""
        got = self.shadowing()
        if not got or kind == "skip":
            return
        module = got[0]
        if kind == "diverged":
            self.diverged(module, "profile.decide", why, ours, theirs)
            return
        self.restore()
        first = kind not in self.seen
        self.seen[kind] = self.seen.get(kind, 0) + 1
        if got[1] == "shadow" and (first or self.hub.clock() - (self.seen_at or 0) >= SEEN_EVERY):
            self.put("profile", "shadow", module, (self.recs().get("profile") or {}).get("why") or "")

    def check(self, module, warm=False):
        """Check of the profile list and the tokens of profiles with a permanent token (in warm — also that profiles with a
        command have a token): True — matched, None — the module did not answer or the command token is not there yet
        (does not count), False — divergence."""
        prof = self.hub.profiles
        mod = prof.module(module, "warm" if warm else "shadow")
        theirs = mod.listing(0)
        if theirs is None:
            return None
        ours = prof.builtin.listing()
        if list_print(ours) != list_print(theirs):
            self.diverged(module, "profile.list",
                          i18n.t("core.providers.list_differs", ours=list_print(ours), theirs=list_print(theirs)))
            return False
        for p in ours["profiles"]:
            if p.get("login") or p.get("renewable") and not warm:
                continue
            got = mod.token(p["name"])
            if got is None:
                return None
            if p.get("renewable"):   # warm: the module fetches the command token itself — values cannot be compared, but it must exist
                from core import modlaunch
                if not any(v for k, v in (got.get("env") or {}).items() if modlaunch.login_name(k)):
                    return None
                continue
            mine = prof.builtin.token(p["name"])
            if env_print((mine or {}).get("env")) != env_print(got.get("env")):
                self.diverged(module, "profile.token", i18n.t("core.providers.token_differs", name=p["name"]),
                              env_print((mine or {}).get("env")), env_print(got.get("env")))
                return False
        return True

    # step
    def tick(self):
        now = self.hub.clock()
        if self.at is not None and now - self.at < EVERY:
            return
        last, self.at = self.at, now
        if self.edits_at is None or now - self.edits_at >= EDITS_EVERY:
            self.edits_at = now
            try:
                self.edits = local_edits(self.repo, self.hub.config)
            except Exception as e:  # noqa: BLE001 — git in a foreign folder does not crash the tick
                print(f"hubd: local edits not checked — {type(e).__name__}: {e}", flush=True)
        from core import modrun
        wanted = modrun.wants(self.hub.state_dir)
        if (self.recs().get("profile") or {}).get("state") == "shadow" and last is not None:
            self.restore()   # observed time: the steps of this hubd life, a gap counts at most two steps
            self.seen["seconds"] = self.seen.get("seconds", 0) + min(now - last, 2 * EVERY)
        for cap in CAPS:
            self.step(cap, wanted, now)
        self.publish()

    def publish(self):
        recs = self.recs()
        if recs != self.written and (recs or self.written is not None or os.path.exists(path(self.hub.state_dir))):
            try:
                write(self.hub.state_dir, recs)
                self.written = copy.deepcopy(recs)
            except OSError as e:
                print(f"hubd: {FILE} not written — {e!r}", flush=True)

    def step(self, cap, wanted, now):
        cfg = self.hub.config
        rec = self.recs().get(cap) or {}
        state, mod = rec.get("state") or "builtin", rec.get("module")
        own = builtin(cap)   # the built-in code is in the tree; without it "builtin" is nobody, holds and edits are moot
        use = named(cfg, cap)
        if use == "builtin":
            if state != "builtin":
                self.back(cap, mod, f"modules.use.{cap} = builtin", cause="owner")
            return
        if use:   # the owner's word: this module at once; if it cannot — the built-in stands in for it
            why = self.gone(use, wanted, cap) or self.lacks(cap, use, wanted)
            if why and (state != "builtin" or mod != use):
                self.back(cap, use, why[1], cause=why[0])
            elif not why and state == "module" and mod == use:
                if own and rec.get("bare"):   # the built-in code is back: its failures hand the function back again
                    self.put(cap, "module", use, f"modules.use.{cap} = {use}", named=True, bare=False)
                elif not own and not rec.get("bare"):   # led on a tree with the built-in code: mark it
                    self.put(cap, "module", use, f"modules.use.{cap} = {use}", named=True, bare=True)
                elif cap == "stats" and own:
                    self.stats(rec, use)
            # returned after voicing or report failures (hold of this version) — a named one also waits for a new version or reset
            elif not why and self.ready(use, wanted) and not (own and state == "builtin" and mod == use
                                                               and self.held(cap, use, "fail")):
                self.put(cap, "module", use, f"modules.use.{cap} = {use}", named=True, **({} if own else {"bare": True}))
            return
        if self.app:
            if state != "builtin":
                self.back(cap, mod, i18n.t("core.providers.app_builtin"), cause="app")
            return
        if state != "builtin":
            why = self.gone(mod, wanted, cap) or self.lacks(cap, mod, wanted) \
                or (("edits", i18n.t("core.providers.local_edits", files=", ".join(self.edits[cap][:5]))) if own and cap in self.edits else None)
            if why:
                self.back(cap, mod, why[1], cause=why[0])
            elif not own:   # shadow or warm from a tree with the built-in code: nothing to compare with — the module leads
                if state != "module" or not rec.get("bare"):
                    self.put(cap, "module", mod, i18n.t("core.providers.no_builtin"), bare=True)
            elif state == "module" and rec.get("bare"):   # the built-in code is back in the tree: the mark goes
                self.put(cap, "module", mod, rec.get("why") or "", bare=False)
            elif cap == "profile":
                self.profile(rec, mod, now, cfg)
            elif cap == "stats" and state == "module":
                self.stats(rec, mod)
            return
        name = self.candidate(cap, wanted)
        if name is None or own and (cap in self.edits or self.held(cap, name)) or self.lacks(cap, name, wanted) \
                or self.doctor(name) is not True:
            return
        if not own:   # nobody leads it: the only ready module, at once
            self.put(cap, "module", name, i18n.t("core.providers.ready_module"), bare=True)
        elif cap == "profile":
            self.seen, self.seen_of, self.checked_at = {}, None, None
            passed = rec.get("passed") or {}
            if passed.get("module") == name and passed.get("version") == ((self.mods().get(name) or {}).get("version") or "")[:40]:
                # it went back on a pause or by the owner's word, not on a divergence: the shadow of this version is passed
                self.put(cap, "warm", name, i18n.t("core.providers.again_warm"), again=True)
            else:
                self.put(cap, "shadow", name, i18n.t("core.providers.ready_shadow"))
        else:
            self.put(cap, "module", name, i18n.t("core.providers.ready_module"))

    def profile(self, rec, mod, now, cfg):
        state = rec["state"]
        self.restore()
        if state == "shadow" and (self.checked_at is None or now - self.checked_at >= SHADOW_CHECK):
            got = self.check(mod)
            if got is False:
                return
            if got:
                self.checked_at = now
                self.seen["checks"] = self.seen.get("checks", 0) + 1
        if state == "shadow":
            total = sum(v for k, v in self.seen.items() if k in KINDS)
            if self.seen.get("seconds", 0) >= SHADOW_SECONDS and total >= SHADOW_MIN and self.seen.get("empty") \
                    and self.seen.get("checks"):
                self.put("profile", "warm", mod, i18n.t("core.providers.shadow_passed", hours=SHADOW_SECONDS // 3600, total=total,
                                                        kinds=", ".join(f"{k} {self.seen[k]}" for k in KINDS if self.seen.get(k))))
        elif state == "warm":
            listing = self.hub.profiles.module(mod, "warm").listing(0) or {}
            ready = listing.get("ready") or {}
            from modules.gateway import gateway as gw
            need = {"tokens": ("ok",), "conduit": ("ok", "off"),
                    "gateway": ("ok",) if gw.settings(cfg)["enabled"] else ("ok", "off")}
            bad = [k for k in need if ready.get(k) == "fail"]
            if bad:
                self.fail_warm(mod, i18n.t("core.providers.warm_not_ready", bad=", ".join(bad)))
                return
            if all(ready.get(k) in ok for k, ok in need.items()):
                got = self.check(mod, warm=True)   # and it really has the command tokens, not just "ok"
                if got is False:
                    return
                if got:
                    self.put("profile", "module", mod, i18n.t("core.providers.ready_warm"))
                    return
            if now - (rec.get("since") or now) >= WARM_SECONDS:
                self.fail_warm(mod, i18n.t("core.providers.warm_timeout", minutes=WARM_SECONDS // 60,
                                           ready=ready or i18n.t("core.providers.no_ready")))

    def fail_warm(self, mod, why):
        self.hub.record("module.error", mod, {"module": mod, "hook": "profile.list", "kind": "shadow",
                                              "error": why[:500], "channel": "core", "owner": False})
        self.back("profile", mod, why, hold=True, cause="shadow")

    def stats(self, rec, mod):
        """The module's report: a module failure after the switch (hold until a new version) or STATS_FAILS failed sends by it in a row
        (hold for STATS_HOLD: the network or the server fails the built-in the same way) — back to the built-in."""
        last = (self.mods().get(mod) or {}).get("last") or {}
        if (last.get("ts") or 0) >= (rec.get("since") or float("inf")) and last.get("kind") not in ("shadow", "late"):
            self.back("stats", mod, i18n.t("core.providers.module_failed", hook=last.get("hook"), error=last.get("error")),
                      hold="fail", cause="fail")
            return
        try:
            with open(os.path.join(self.hub.state_dir, "stats", "state.json"), encoding="utf-8") as f:
                st = json.load(f)
        except (OSError, ValueError):
            return
        # failures belong to this switch: earlier ones (before hold and reset) were recorded before since
        fresh = isinstance(st, dict) and isinstance(st.get("tried"), (int, float)) and st["tried"] >= (rec.get("since") or 0)
        if fresh and st.get("by") == mod and isinstance(st.get("fails"), int) \
                and not isinstance(st["fails"], bool) and st["fails"] >= STATS_FAILS:
            self.back("stats", mod, i18n.t("core.providers.report_failed", count=st["fails"], error=st.get("error") or ""),
                      hold="fail", cause="fail", until=round(self.hub.clock() + STATS_HOLD))
