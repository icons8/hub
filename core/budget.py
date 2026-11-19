"""Limit economy mode (the "mini-hub" spec, section 4; owner decisions 2026-10-15) - only when the hub has at least one
mini-hub (core/minihub.on: parallelism N >= 1 and the session has already switched over); without them - the earlier rule
(core/executors.budget_waits: the active account's week from 85%).

An account's fill level is the largest of the 5 h, weekly and weekly-with-overage windows (a window whose reset has passed counts as 0; a
limit rejection before the reset - 100%). The pool's headroom is the lowest fill level among accounts not busy until their
reset; no pool - the single login.
An account with a token from a command (a gateway: several subscriptions shared with other people) is not headroom by its
own limits: the share belongs to the subscription of the current token, and at the limit the core takes a fresh one
(modules/accounts: refresh); the gateway gives no honest remainder - until it is busy until a reset it is free, with no
share and no forecast.
Modes, top to bottom: no unbusy account below the pool switch threshold (switch_at) - "owner" (an executor only on the
owner's explicit word); headroom from the economy threshold (0.85; if it was in economy - until it drops below the
threshold - 0.05) - "economy" (immediately - only tasks on the owner's live word and urgent ones, `--work normal` - on
the cheap pair); otherwise - "normal".

The core writes the mode as the budget.mode fact when it changes (the daemon, every tick): the journal fold does not depend
on the clock or hub.json.

The budget gate - whether to wait for the owner's word at the limit (hub.json budget.owner_gate, via the owner.settings
fact; on by default - everything above). Off: the weekly threshold does not hold, economy only moves `--work normal` to the
cheap pair (it does not hold the queue, no card), at the whole pool's threshold executors wait for the window reset and
rise on their own; the hub gets a line without "ask the owner". When it is switched off, the open economy card is
withdrawn (core/reducer.py, owner.settings).
"""
import os

from core import coretext, forms, i18n
from core.coretext import T

ECONOMY_AT = 0.85   # the default economy threshold (budget.economy_at in hub.json)
EXIT_GAP = 0.05     # leaving economy - this far below the threshold: the mode does not flicker at the boundary
FLOOR = 0.40        # the economy threshold is no lower than this, if it had to be lowered under the switch threshold
WINDOWS = {"five_hour": "core.budget.window_five_hour", "seven_day": "core.budget.window_seven_day",
           "seven_day_overage_included": "core.budget.window_seven_day_oi"}   # window -> catalog key
MODES = ("normal", "economy", "owner")


# the wait reasons per mode (the core's phrases, core/coretext.py): a request that waits keeps its code (why_code); a
# record without it is told by the start of the text - the English starts exactly as forms.PREFIXES has it
POOL_RESET = T("core.budget.wait_pool_reset", "pool limit ({why}): waits for the window to reset")
POOL_WORD = T("core.budget.wait_pool_word", "pool limit ({why}): an executor only on the owner's explicit word")
ECONOMY_WORD = T("core.budget.wait_economy", "limit economy ({why}): right away only tasks on the owner's live word and "
                 "urgent ones; waits for the window to reset or for the owner's word")
SWITCH_THRESHOLD = T("core.budget.why_switch", "the switch threshold")
FROM_85 = T("core.budget.why_from_85", "from 85%")
# the card to the hub (card) and the lines on leaving the mode (apply)
BY_OWNER = T("core.budget.by_owner", "the owner")
CARD_BUTTONS = T("core.budget.card_buttons",
                 "Ask the owner with one card with options: \"Wait for the reset\" — do nothing, the queue will come up "
                 "by itself; \"Raise the queue now\" — hub task go --queue --for <qN of their answer>; \"Raise only normal "
                 "on the cheap pair\" — hub task go --queue normal --for <qN>.")
CARD_POOL_RESET = T("core.budget.card_pool_reset",
                    "Pool limit: {why}{when}{eta}. New executors wait for the window to reset and will come up by "
                    "themselves; do not ask the owner about it; {queue}.")
CARD_POOL_WORD = T("core.budget.card_pool_word",
                   "Pool limit: {why}{when}. An executor now only on the owner's explicit word (hub task go <name> --for "
                   "<mN|qN>); mini-hubs answer by themselves, without digging; {queue}. {buttons}")
CARD_ECONOMY = T("core.budget.card_economy",
                 "Limit economy: {why}{when}{eta}. Executors now come up only for tasks on the owner's live word and "
                 "urgent ones, the others wait in the queue; --work normal — on the cheap pair, risky — as is; {queue}. "
                 "{buttons}")
WINDOW_RESET = T("core.budget.window_reset", "the window has reset")
# why the mode is what it is (decide): hubd writes the English and its marks (why_l10n) into budget.mode — it goes into
# the waits and cards above, which a session reads in English and the owner in his language (the page, hub task queue)
WHY_ALL_BUSY = T("core.budget.why_all_busy", "all pool accounts are busy until the reset")
WHY_OVER_POOL = T("core.budget.why_over_pool", "all pool accounts are at {pct}% and above (switch threshold {at}%)")
WHY_OVER_ACCOUNT = T("core.budget.why_over_account", "the account is at {pct}% and above (switch threshold {at}%)")
WHY_ECONOMY_POOL = T("core.budget.why_economy_pool", "pool reserve {pct}% (window {window} of account {account})")
WHY_ECONOMY_ACCOUNT = T("core.budget.why_economy_account", "window {window} of account {account} {pct}%")
WHY_ECONOMY_WINDOW = T("core.budget.why_economy_window", "window {window} {pct}%")
WHY_GATEWAY = T("core.budget.why_gateway", "{name}: the token is issued by the gateway, its reserve is not known")
WHY_RESERVE = T("core.budget.why_reserve", "pool reserve {pct}%")
WHY_UNKNOWN = T("core.budget.why_unknown", "limits are unknown")
WHY_NO_MINIHUBS = T("core.budget.why_no_minihubs", "no mini-hubs left")


def why_of(b):
    """The mode's reason (st["budget"] or a budget.mode fact) with the marks of its phrases; an earlier record — its text."""
    return coretext.got(b, "why")


def gate(st):
    """Whether executors wait for the owner's word at the limit (hub.json budget.owner_gate): without the setting - yes."""
    return (st.get("owner") or {}).get("budget_gate") is not False


def mode(st):
    """The mode in effect - or None: there are no mini-hubs (a journal from before parallelism, N = 0, all on "self",
    nobody has switched over yet; a departed session is not a mini-hub), the economy rule does not apply."""
    from core import minihub
    if not any(s["state"] not in minihub.GONE and minihub.on(st, s) for s in st["sessions"].values()):
        return None
    return (st.get("budget") or {}).get("mode") or "normal"


def label(why, code=None, word=True):
    """What a queued request waits for - a short line for the owner, if it waits for the economy mode, otherwise None.
    code - the request's why_code field, without it - the start of the text (core/forms.py); word - whether the budget
    gate (gate) is on."""
    code = forms.reason_code(code, why, forms.WAIT_CODES)
    if code == forms.POOL_LIMIT:
        return i18n.t("core.budget.label_pool_word") if word else i18n.t("core.budget.label_pool_reset")
    if code == forms.ECONOMY:
        return i18n.t("core.budget.label_economy")
    return None


def view(st):
    """The mode for the page (the limits line): {mode, why, until, queued, gate} - only when it is in effect and not normal;
    why - in the reader's language (the process language); gate - whether they wait for the owner's word (otherwise -
    for the window reset)."""
    m = mode(st)
    if m in (None, "normal"):
        return None
    b = st.get("budget") or {}
    # queued counts only what the mode holds (those waiting for a parallelism slot do not count)
    queued = sum(1 for r in (st.get("runs") or {}).values()
                 if r.get("state") == "queued" and label(r.get("why"), r.get("why_code")))
    return {"mode": m, "why": coretext.human(why_of(b)), "until": b.get("until"), "queued": queued, "gate": gate(st)}


def thresholds(economy_at, switch_at):
    """(economy threshold, pool switch threshold): economy is always below the switch - otherwise S - 0.10, not below 0.40."""
    e = economy_at if isinstance(economy_at, (int, float)) and not isinstance(economy_at, bool) \
        and 0 < economy_at <= 1 else ECONOMY_AT
    if e >= switch_at:
        e = max(round(switch_at - 0.10, 4), FLOOR)
    return e, switch_at


def _past(w, now):
    return bool(w.get("resets")) and w["resets"] <= now


def _keys(seen):
    """The windows of the fill level: 5 h, the week and the week with overage; Codex limits (experiment, hosts/codex_limits) - also its windows
    of another length (codex_<N>m, the same rule as "Usage")."""
    if (seen or {}).get("engine") != "codex":
        return list(WINDOWS)
    from core import usage
    return list(WINDOWS) + sorted(k for k in ((seen or {}).get("windows") or {}) if usage.codex_length(k))


def _codex_cut(seen, now):
    """The window of a Codex rejection (experiment): Codex does not name it (rateLimitReachedType, ordinaryUsageAllowed
    false). By the rule reducer._codex_limit applies to the windows when a cut turn brings no reset of its own, as of the
    reading (without its time - now): of the windows ahead, the used-up one with the latest reset, otherwise the nearest
    reset. None - not a Codex rejection or no window ahead."""
    seen = seen or {}
    if seen.get("engine") != "codex" or seen.get("status") != "rejected":
        return None
    at = seen.get("ts") or now
    windows = seen.get("windows") or {}
    ahead = [(w["resets"], key) for key in _keys(seen) for w in [windows.get(key)]
             if isinstance(w, dict) and isinstance(w.get("resets"), (int, float)) and w["resets"] > at]
    full = [x for x in ahead if (windows[x[1]].get("used") or 0) >= 1]
    return (max(full) if full else min(ahead, default=(None, None)))[1]


def fill(seen, now):
    """(share, window) of an account from its latest limits: the largest of the 5 h, weekly and weekly-with-overage windows; a rejection before the reset - 1.
    Codex (experiment) does not name the window of a rejection: it is _codex_cut's, full until that window resets."""
    windows = (seen or {}).get("windows") or {}
    out = (0, None)
    for key in _keys(seen):
        w = windows.get(key) if isinstance(windows.get(key), dict) else {}
        used = 0 if _past(w, now) else (w.get("used") or 0)
        if used > out[0]:
            out = (used, key)
    if (seen or {}).get("status") == "rejected":
        w = windows.get(seen.get("type")) or {}
        if not _past(w, now) and seen.get("type") in WINDOWS:
            out = (1, seen["type"])
        else:
            cut = _codex_cut(seen, now)
            if cut and not _past(windows[cut], now):
                out = (1, cut)
    held = _held(seen, now)
    if held:   # a cut Codex turn named a reset still ahead: full until it, whatever the reading shows
        out = (1, _held_window(seen, held))
    return out


def _free_at(seen, level, now):
    """When an account's fill level drops below level: the latest reset of the windows that hold it above."""
    windows = (seen or {}).get("windows") or {}
    cut = _codex_cut(seen, now)   # a Codex rejection: its window holds it full until its reset, whatever its share
    at = [w.get("resets") for key in _keys(seen) for w in [windows.get(key) or {}]
          if not _past(w, now) and ((w.get("used") or 0) >= level or key == cut) and w.get("resets")]
    at += [h for h in [_held(seen, now)] if h]   # the reset a cut Codex turn named holds it full until then
    return max(at) if at else None


def _held(seen, now):
    """The reset a Codex turn the limit cut named (experiment, reducer._codex_limit: held), if it is ahead: the
    reading may show an earlier one (the 5 h window while the week refused), and Codex still refuses until it."""
    held = (seen or {}).get("held")
    return held if (seen or {}).get("engine") == "codex" and isinstance(held, (int, float)) \
        and not isinstance(held, bool) and held > now else None


def _held_window(seen, held):
    """The window of the held reset: the one whose reset is nearest to it (None — the reading has no windows)."""
    windows = (seen or {}).get("windows") or {}
    near = [(abs(w["resets"] - held), key) for key in _keys(seen) for w in [windows.get(key)]
            if isinstance(w, dict) and isinstance(w.get("resets"), (int, float))]
    return min(near)[1] if near else None


def window_name(window, lang=None):
    """The window for a person: "5 h", "week"; a Codex window of another length (codex_<N>m) by its length. lang - the
    language of the state when the caller is a fold (the process language of hubd is not the fold's); none - the process's."""
    def say(key, **params):
        return i18n.tr(lang, key, **params) if lang else i18n.t(key, **params)
    if window in WINDOWS:
        return say(WINDOWS[window])
    from core import usage
    mins = usage.codex_length(window or "")
    if not mins:
        return window
    mins //= 60
    return say("core.budget.window_days", n=mins // 1440) if mins % 1440 == 0 else \
        say("core.budget.window_hours", n=mins // 60) if mins % 60 == 0 else say("core.budget.window_minutes", n=mins)


def _resets(seen, window):
    return (((seen or {}).get("windows") or {}).get(window) or {}).get("resets") if window else None


def decide(st, names, switch_at, now, economy_at=None, renewable=()):
    """The mode now: {mode, why, until, window, account, used, resets} (resets - the reset of window). names - the pool's
    accounts in order (the pool is on), None - the single login (the active one's limits, st["limits"]); renewable -
    accounts with a token from a command (a gateway): their share does not count."""
    e, s = thresholds(economy_at, switch_at)
    acc = st.get("account") or {}
    if names:
        rows = [(n, (acc.get("seen") or {}).get(n), (acc.get("blocked") or {}).get(n) or 0) for n in names]
    else:
        rows = [(None, st.get("limits") or None, 0)]
    free = []
    for name, seen, blocked in rows:
        if blocked > now:
            continue
        if name in renewable:   # gateway: the share is of one of its subscriptions, at the limit a fresh token is taken
            free.append((0, name, None, None))
            continue
        used, window = fill(seen, now)
        free.append((used, name, window, seen))
    was = (st.get("budget") or {}).get("mode") or "normal"
    lowest = min(free, key=lambda x: x[0]) if free else None
    if lowest is None or lowest[0] >= s:
        # when at least one frees up: a busy one - by its deadline, the others - by the reset of windows above the switch threshold
        when = [max(b, _free_at(seen, s, now) or 0) for _, seen, b in rows]
        until = min([w for w in when if w > now], default=None)
        # the reason is the core's phrase: hubd writes it with its marks (the page and `hub task queue` show it in the
        # reader's language)
        pct, at = (round(lowest[0] * 100), round(s * 100)) if lowest else (None, None)
        why = (WHY_ALL_BUSY if lowest is None else
               WHY_OVER_POOL.format(pct=pct, at=at) if names and len(rows) > 1 else
               WHY_OVER_ACCOUNT.format(pct=pct, at=at))
        return {"mode": "owner", "why": why, "until": until, "window": lowest[2] if lowest else None,
                "account": lowest[1] if lowest else None, "used": lowest[0] if lowest else None,
                "resets": _resets(lowest[3], lowest[2]) if lowest else None}
    used, name, window, seen = lowest
    level = e - EXIT_GAP if was in ("economy", "owner") else e
    if used >= level:
        until = min([u for u in (_free_at(sn, e - EXIT_GAP, now) for _, _, _, sn in free) if u], default=None)
        pct, win = round(used * 100), _window_text(window)
        why = (WHY_ECONOMY_POOL.format(pct=pct, window=win, account=name) if names and len(rows) > 1 else
               WHY_ECONOMY_ACCOUNT.format(pct=pct, window=win, account=name) if name else
               WHY_ECONOMY_WINDOW.format(pct=pct, window=win))
        return {"mode": "economy", "why": why, "until": until, "window": window, "account": name, "used": used,
                "resets": _resets(seen, window)}
    why = (WHY_GATEWAY.format(name=name) if name in renewable else
           WHY_RESERVE.format(pct=round(used * 100)) if seen else WHY_UNKNOWN)
    return {"mode": "normal", "why": why,
            "until": None, "window": window, "account": name, "used": used, "resets": _resets(seen, window)}


def forecast(state_dir, account_state, name, window, now, engine=None):
    """When, at the current pace, the window of account name runs out - from the current journal's account.limits
    (core/usage: the pace over the last hours of the window; reads from the previous position); cannot be computed - None.
    This account only: different accounts have different series. engine - the series of the hub's limits row
    (st["limits"]["engine"]): "codex" on a hub entirely on Codex (experiment), otherwise Claude's."""
    from core import usage
    try:
        points = [f for f in usage._read(os.path.join(state_dir, "journal.jsonl")) if f["type"] == "account.limits"]
    except OSError:
        return None
    acc = {"active": name, "refreshed": (account_state or {}).get("refreshed") or {}}
    engine = usage.CODEX if engine == usage.CODEX and not name else None
    for w in usage.windows(points, [], now, acc, engine)[0]:
        if w["name"] == window:
            return w.get("eta")
    return None


def waits(st, owner_ok, live, m=None):
    """Why an executor's request waits under the economy mode - or None. m - mode(st) already taken by the caller (a
    queue pass takes it once, not for every request)."""
    m = m or mode(st)
    b = st.get("budget") or {}
    if m == "owner" and not owner_ok:
        why = why_of(b) or SWITCH_THRESHOLD
        return forms.Why((POOL_WORD if gate(st) else POOL_RESET).format(why=why), forms.POOL_LIMIT)
    if m == "economy" and gate(st) and not (owner_ok or live):
        return forms.Why(ECONOMY_WORD.format(why=why_of(b) or FROM_85), forms.ECONOMY)
    return None


def card_of(st, sess):
    """The turn of session sess is exactly for the message to the hub with the economy card (apply): its question to the
    owner is this card. A turn with something else glued on (turn_also) is not it: the question there could be about
    something else, we do not withdraw it."""
    mid = (st.get("budget") or {}).get("card")
    return bool(mid) and (sess or {}).get("kind") == "hub" and sess.get("turn_marker") == mid and \
        not sess.get("turn_also")


def economize(st, run):
    """A request is raised under economy: `--work normal` - on the economy pair (computed by hub task run from the settings)."""
    pair = run.get("economy")
    if isinstance(pair, dict) and pair and mode(st) in ("economy", "owner"):
        run.update({k: v for k, v in pair.items() if k in ("model", "effort") and v})


def card(st, data, tz):
    """The text for the hub: one card to the owner on entering economy and at the whole pool's switch threshold."""
    from core import owner
    at = lambda t: owner._local(t, tz).strftime("%d.%m %H:%M")   # noqa: E731
    queued = sorted(((n, r) for n, r in (st.get("runs") or {}).items()
                     if r.get("state") == "queued" and label(r.get("why"), r.get("why_code"))),
                    key=lambda kv: kv[1].get("ts") or 0)
    # the card is the core's words to the hub (core/coretext.py): the session reads the English, the owner his language
    items = [T("core.budget.queued_item", "{name} (from {by})").format(name=n, by=r.get("by") or BY_OWNER)
             for n, r in queued[:8]]
    ql = T("core.budget.queued", "in the queue now {count}: {names}").format(count=len(queued), names=coretext.join(
        *[x for i, item in enumerate(items) for x in ([", "] if i else []) + [item]], " …" if len(queued) > 8 else "")) \
        if queued else T("core.budget.queue_empty", "the queue is empty for now")
    when = T("core.budget.reset_at", "; reset — {at}").format(at=at(data["until"])) if data.get("until") else ""
    # a forecast later than the window reset is not a forecast: it will last until the reset
    late = data.get("eta") and data.get("resets") and data["eta"] >= data["resets"]
    of = T("core.budget.eta_account", " of account {account}").format(account=data["account"]) \
        if data.get("account") else ""
    eta = T("core.budget.eta", "; forecast: at this pace window {window}{account} runs out at {at}").format(
        window=_window_text(data.get("window")), account=of, at=at(data["eta"])) if data.get("eta") and not late else ""
    if not gate(st):   # the owner is not asked: the queue will rise by itself after the reset
        return CARD_POOL_RESET.format(why=why_of(data), when=when, eta=eta, queue=ql)
    if data["mode"] == "owner":
        return CARD_POOL_WORD.format(why=why_of(data), when=when, queue=ql, buttons=CARD_BUTTONS)
    return CARD_ECONOMY.format(why=why_of(data), when=when, eta=eta, queue=ql, buttons=CARD_BUTTONS)


def _window_text(window):
    """The window as the core's phrase ("5 h", "week", a Codex window by its length): the hub reads the English, the
    owner his language (window_name gives it in one language)."""
    if window == "five_hour":
        return T("core.budget.window_five_hour", "5 h")
    if window == "seven_day":
        return T("core.budget.window_seven_day", "week")
    if window == "seven_day_overage_included":
        return T("core.budget.window_seven_day_oi", "week incl. overage")
    from core import usage
    mins = usage.codex_length(window or "")
    if not mins:
        return window
    mins //= 60   # as window_name
    return T("core.budget.window_days", "{n} d").format(n=mins // 1440) if mins % 1440 == 0 else \
        T("core.budget.window_hours", "{n} h").format(n=mins // 60) if mins % 60 == 0 else \
        T("core.budget.window_minutes", "{n} min").format(n=mins)


def apply(st, ev, data, ctx):
    """budget.mode - written only by the core; the hub gets a card on entering economy and at the whole pool's threshold,
    a line on exit. card - the message to the hub with the card: a question the hub asks in the turn for it is the economy
    card (withdrawn when the gate is switched off)."""
    from core.tools import Rejected
    if data.get("channel") != "core" or data.get("mode") not in MODES:
        raise Rejected(T("core.budget.core_writes", "the budget mode is written by the core: normal | economy | owner"))
    was = (st.get("budget") or {}).get("mode") or "normal"
    card_msg = (st.get("budget") or {}).get("card")
    st["budget"] = {k: data.get(k) for k in ("mode", "why", "until", "window", "account", "used")}
    if isinstance(data.get("why"), str):   # the reason's marks: the owner reads it in his language (why_of)
        coretext.put(st["budget"], "why", why_of(data))
    if card_msg and data["mode"] != "normal":
        st["budget"]["card"] = card_msg
    to = data["mode"]
    if to == was:
        return
    if to != "normal" and mode(st) is None:   # without mini-hubs there is no economy rule - and no card either
        return
    if to == "owner" or (to == "economy" and was == "normal" and gate(st)):   # without the gate economy holds nothing
        ctx["notify"](ev, card(st, data, st["owner"].get("tz") or "UTC"))
        if gate(st):
            st["budget"]["card"] = f"m{ev['seq']}"
    elif to == "normal" and not gate(st):   # there were no cards (or they were withdrawn when the gate was switched off) - just a line
        if was == "owner":
            ctx["notify"](ev, (T("core.budget.no_minihubs", "No mini-hubs left — the limit economy mode is off")
                               if mode(st) is None else T("core.budget.pool_reset", "The pool limit has reset ({why})")
                               .format(why=why_of(data) or WINDOW_RESET))
                          + T("core.budget.queue_rises", ": the executors' queue comes up by itself."))
    elif to == "normal" and mode(st) is None:   # the last mini-hub was switched off: from here - the earlier weekly rule
        from core.executors import BUDGET_OWNER_ONLY
        ctx["notify"](ev, T("core.budget.back_to_weekly",
                            "No mini-hubs left — the limit economy mode is off; from here the earlier rule: from {pct}% "
                            "of the active account's weekly limit an executor waits for the owner's word (hub task queue "
                            "— who waits). The economy card, if the owner has not answered yet — withdraw it (hub "
                            "question withdraw <qN>).").format(pct=round(BUDGET_OWNER_ONLY * 100)))
    elif to == "normal":   # a card not yet answered - withdraw it
        ctx["notify"](ev, T("core.budget.economy_over",
                            "Limit economy is over ({why}): the executors' queue comes up by itself, in order and "
                            "within the ceilings. The economy card, if the owner has not answered yet — withdraw it (hub "
                            "question withdraw <qN>).").format(why=why_of(data) or WINDOW_RESET))
