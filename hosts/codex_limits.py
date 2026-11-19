"""Codex: sign-in and account limits of Codex at the
codex host (hosts/codex.py).

Seams the host calls:
- start(proc) — the thread is ready (thread/start | thread/resume answered): ask for the account limits
  (account/rateLimits/read);
- notice(proc, name, method, params) — a notification the host does not parse itself: the core's facts as a list;
- result(proc, turn) — extra fields of the turn result (turn.result) on turn/completed: the limit, "sign-in lost";
- response(proc, name, method, params, msg) — app-server's answer to the host's request (its own — account/…, or a
  refusal of thread/start|resume, turn/start): facts as a list — the answer is handled here, the host does not touch it
  further; None — to the host.

Limits are the same fact `account.limits` as with Claude (the page, the executors' economy and "Usage" read it): windows
by duration — 300 min -> five_hour, 10080 -> seven_day (others — codex_<N>m: the economy and the limits line do not know
them); used — a share (usedPercent / 100), resets — seconds. A hub on Codex has one account — no name (like the default
sign-in). No sign-in or it was lost — the fact `codex.auth_lost` (probe 2026-10-19: without a sign-in rateLimits/read
refuses with "authentication required", a turn fails after ~16 s with httpStatusCode 401).
"""
import os
import re
import time

WINDOWS = {300: "five_hour", 10080: "seven_day"}
# a turn cut off by the Codex subscription limit (codexErrorInfo) — the result text the core recognizes (reducer.LIMIT_TEXT)
LIMIT_ERRORS = ("usageLimitExceeded",)
AUTH_TEXT = re.compile(r"authentication required|\b401\b|unauthori[sz]ed|refresh token|sign in again|log ?in again",
                       re.I)

# the last snapshot of limits per account (the Codex account pool: a process is on one account's folder, proc.account;
# without a pool — one, None) — a fact only when the numbers changed, as with Claude; data — with sparse updates and the
# ordinaryUsageAllowed sign
_last = {}


def reset():
    _last.clear()


def _account(proc):
    return getattr(proc, "account", None)


def _mark(proc):
    """The account field of the process's facts (the Codex pool), {} — without one."""
    acct = _account(proc)
    return {"account": acct} if acct else {}


def _windows(snap):
    out = {}
    for key in ("primary", "secondary"):
        w = snap.get(key)
        if not isinstance(w, dict) or not isinstance(w.get("usedPercent"), (int, float)):
            continue
        mins = w.get("windowDurationMins")
        out[WINDOWS.get(mins) or f"codex_{mins}m"] = {"used": w["usedPercent"] / 100, "resets": w.get("resetsAt")}
    return out


def limits_fact(snap, allowed=None, prev=None):
    """A Codex snapshot (RateLimitSnapshot) -> account.limits data; a sparse update is merged with the previous one."""
    prev = prev or {}
    # a read that says ordinary usage is allowed — Codex lets in now (the fold drops a reset a cut turn named,
    # reducer._codex_limits); a sparse update never says it
    said_open = allowed is True
    windows = {**(prev.get("windows") or {}), **_windows(snap or {})}
    reached = (snap or {}).get("rateLimitReachedType")
    # ordinaryUsageAllowed comes only in a read (rateLimits/read); a sparse update does not carry it — the previous
    # "not allowed" holds until the window has reset (the reset time changed): then "allowed" until the next read
    if allowed is None:
        renewed = any((prev.get("windows") or {}).get(n, {}).get("resets") not in (None, w.get("resets"))
                      for n, w in _windows(snap or {}).items())
        allowed = None if renewed else prev.get("_allowed")
    status = "rejected" if reached or allowed is False else "allowed"
    return {"status": status, "type": reached or prev.get("type"), "overage": False, "windows": windows,
            "engine": "codex", "plan": (snap or {}).get("planType") or prev.get("plan"), "_allowed": allowed,
            **({"usage_allowed": True} if said_open and status == "allowed" else {})}


def _emit(proc, data):
    public = {**{k: v for k, v in data.items() if not k.startswith("_")}, **_mark(proc)}
    last = _last.setdefault(_account(proc), {})
    same = last.get("public") == public
    last.update(public=public, data=data)
    return [] if same else [("account.limits", None, public)]


def _prev(proc):
    return (_last.get(_account(proc)) or {}).get("data")


def start(proc):
    proc.call("account/rateLimits/read", {})


def notice(proc, name, method, params):
    if method == "account/rateLimits/updated":
        snap = params.get("rateLimits") or {}
        if snap.get("limitId") not in (None, "codex"):   # another limit bucket — not the Codex subscription windows
            return []
        return _emit(proc, limits_fact(snap, prev=_prev(proc)))
    return []


def _auth_lost(proc, why):
    """The sign-in is lost: the hub folder's (codex.auth_lost, with its row's name in the pool) or an account folder's of
    the Codex pool (codex.account_lost — a core without the pool refuses it instead of taking it for the hub's)."""
    acct, home = _account(proc), getattr(proc, "home", None)
    hub = getattr(getattr(proc, "host", None), "home", None)
    if acct and home and hub and os.path.abspath(home) != os.path.abspath(hub):
        return [("codex.account_lost", None, {"account": acct, "why": str(why)[:300]})]
    return [("codex.auth_lost", None, {"why": str(why)[:300], **_mark(proc)})]


def response(proc, name, method, params, msg):
    if method == "account/rateLimits/read":
        if "error" in msg:
            err = (msg.get("error") or {}).get("message") or ""
            return _auth_lost(proc, err) if AUTH_TEXT.search(err) else []
        res = msg.get("result") or {}
        snap = (res.get("rateLimitsByLimitId") or {}).get("codex") or res.get("rateLimits") or {}
        return _emit(proc, limits_fact(snap, allowed=res.get("ordinaryUsageAllowed"), prev=_prev(proc)))
    return None


def _http_status(info):
    if isinstance(info, dict):
        for v in info.values():
            if isinstance(v, dict) and v.get("httpStatusCode"):
                return v["httpStatusCode"]
    return None


def result(proc, turn):
    """A Codex turn failed: the subscription limit — a text the core recognizes as a cut by a limit; no sign-in — a separate fact."""
    err = turn.get("error") if isinstance(turn.get("error"), dict) else None
    extra = {"engine": "codex"}
    if not err:
        return extra
    info = err.get("codexErrorInfo")
    message = str(err.get("message") or "")
    if info in LIMIT_ERRORS:
        now = time.time()
        resets = [w.get("resets") for w in ((_prev(proc) or {}).get("windows") or {}).values()
                  if isinstance(w, dict) and (w.get("resets") or 0) > now and (w.get("used") or 0) >= 1]
        extra.update(limit=True, text=f"Codex: usage limit reached ({info})" + (f" · {message}" if message else ""),
                     **({"limit_resets": max(resets)} if resets else {}))
    elif info == "unauthorized" or _http_status(info) == 401 or AUTH_TEXT.search(message):
        extra["auth_lost"] = True
    # a failure is about the process's account (reducer: _account_cut, _codex_limit); a folder not fit runs as the hub
    # row's (hosts/codex.folder), so on the pool a process always has one
    return {**extra, **_mark(proc)}
