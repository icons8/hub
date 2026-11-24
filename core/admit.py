"""Who may say what through the mailbox (core/spool.py): checking a mailbox fact before it is written to the journal.

hubd writes process facts itself (src core, host, observer, registry); the others — CLI, hooks, the page, the bot, the
clock, modules — put a file in the mailbox, and the file itself names who is speaking (channel, sender). Any process of
the hub user can put a file in the mailbox, so this is not a security boundary (a session is the same OS user). The
check keeps in the mailbox what only the core knows (process facts: pid, turn result, permission question), does not
let a file speak with a core channel, and makes a session sender an existing session, not any name.

Two modes (spec V6, §3.3). Flag: the fact is written as before, with a spool.flagged {of, type, src, channel, code,
reason} next to it; hub statistics count flags by (type, src, code). Refuse: the same flag, and the fact is written with
a core.rejected — the fold does not apply it, the CLI prints the reason. Which one is the hub.json setting admit.mode:
auto (the default) refuses once this hub's own window is clean — at least CLEAN_DAYS of the last WINDOW_DAYS UTC days
with checked facts and not one flag — and then stays refusing; flag is the owner's way out and holds always; refuse
refuses at once. The window is core state (st["admit"], folded from the core's spool.checked and spool.flagged), the
switch is the core fact spool.mode: a restart or a journal reader sees the same mode.
"""
import re

from core import forms
from core.coretext import T
from core.reducer import NAME   # session name: not a path and not an option

INTERNAL = ("core", "host", "observer", "registry")   # srcs written by hubd itself, and core channels: not from the mailbox
# written only by hubd or the host: such a fact from the mailbox would give the writer more than the user has (a signal
# to any pid, a turn result and a permission question on behalf of a session, system agents, a token fingerprint) or
# would forge what a session did. The list follows the code: a type that at least one writer puts in the mailbox is not
# included (a census test in tests/test_admit.py)
CORE_ONLY = frozenset({
    "account.exhausted", "account.no_token", "account.token", "account.unblocked", "agents.system", "ask.expired", "budget.mode", "catalog.loaded", "config.changed_outside", "config.seen", "codex.auth_lost", "codex.host", "codex.limit_reset", "dialog.closed",
    "dialog.opened", "executor.unmerged", "gateway.changed", "host.done", "host.error", "hub.restart_started", "mod.prompt",
    "module.claims", "module.provider", "module.ready", "news.loaded", "owner.invited", "owner.settings", "permission.requested",
    "rules.reviewed", "session.background", "session.compacting", "session.exited", "session.folder", "session.folder_forwarded",
    "session.folder_forwarding", "session.fork_failed",
    "session.identified", "session.info", "session.kit", "session.limit_reset", "session.mcp", "session.mcp_login",
    "session.mcp_reconnected", "session.mcp_set", "session.model_refused", "session.parked",
    "session.queue_moved", "session.said", "session.start_stuck", "session.worktree_removed", "spool.checked",
    "spool.flagged", "spool.mode", "spool.refused", "task.auto_run", "task.run_failed", "task.run_held", "task.run_ready",
    "timer.fired",
    "turn.result", "turn.tool", "turn.tool_result",
})
# written without a channel by: a session hook (its process), the schedule clock, the agents' browser, the account
# gateway (limits), the module launcher itself about modules (failure, model spend — the page), plugins and modules about
# themselves, the hub bus's relay transport (src bus-relay: bus.offered, bus.handed, bus.job_done, bus.invites_seen,
# bus.peer_seen, bus.relay_seen). The others name who is speaking: a fact without a channel would pass the fold's "not from a session"
# checks as the owner or the core
UNNAMED = frozenset({"session.started", "session.compacted", "session.ended", "session.end_other", "turn.started",
                     "turn.stopped", "schedule.due", "schedule.started", "schedule.ran", "schedule.ended", "browser.reset",
                     "browser.status", "account.limits", "codex.spent", "module.error", "module.model", "bus.offered", "bus.handed",
                     "bus.job_done", "bus.invites_seen", "bus.peer_seen", "bus.relay_seen"})
UNNAMED_PREFIXES = ("ext.", "mod.")
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")   # session_id, profile — paths and values of claude options
MODE = "flag"       # the mode until the core has written spool.mode (and of reports from before the modes)
MODES = ("flag", "refuse")
SETTINGS = ("auto", *MODES)
OWN = ("spool.checked", "spool.flagged", "spool.mode")   # the check's own facts: the fold keeps them (window and mode)
WINDOW_DAYS = 7     # the window of the auto mode, UTC days, today included
CLEAN_DAYS = 5      # days of it with checked mailbox facts (the hub was running); not one flag in the whole window
DAY = 86400
FIELD = 40          # length of strings from the file in spool.flagged

# reason codes (the code field of spool.flagged; statistics count by them — the text does not go into the report)
CORE_ONLY_CODE = "core_only"
CORE_SRC = "core_src"
CORE_CHANNEL = "core_channel"
NO_CHANNEL = "no_channel"
NO_SESSION = "no_session"
BAD_NAME = "bad_name"
BAD_ID = "bad_id"
PID = "pid"


def known_session(st, name):
    return isinstance(name, str) and bool(NAME.fullmatch(name)) and \
        (name in st["sessions"] or f"was:{name}" in st["aliases"])


def check(st, kind, entity, data, src):
    """Why a mailbox fact would not be accepted (forms.Why with a code) or None. data — as the core will write it (for a
    module and a plugin the channel and sender were already set by spool.outsider); core.* does not get here — it is
    replaced by the spool.refused stub. A non-string type is rejected by the fold — not our concern here."""
    if not isinstance(kind, str):
        return None
    data = data if isinstance(data, dict) else {}
    if kind in CORE_ONLY:
        return forms.Why(T("core.admit.core_only", "{kind} is written only by the core").format(kind=kind), CORE_ONLY_CODE)
    if src in INTERNAL:
        return forms.Why(T("core.admit.core_src", "src {src} is hubd itself, not the mailbox").format(src=src), CORE_SRC)
    channel = data.get("channel")
    if not isinstance(channel, str) or not channel:
        if kind not in UNNAMED and not kind.startswith(UNNAMED_PREFIXES):
            return forms.Why(T("core.admit.no_channel", "{kind} from the mailbox without a channel: who is speaking "
                               "(EXTENDING.md)").format(kind=kind), NO_CHANNEL)
    elif channel in INTERNAL:
        return forms.Why(T("core.admit.core_channel", "channel {channel} is a core channel").format(channel=channel), CORE_CHANNEL)
    if channel == "session" and not known_session(st, data.get("sender")):
        return forms.Why(T("core.admit.no_session", "no session {name}: the sender of a session fact is an existing "
                           "session").format(name=repr(str(data.get("sender"))[:FIELD])), NO_SESSION)
    if kind == "session.requested" and not NAME.fullmatch(str(entity or "")):
        return forms.Why(T("core.admit.bad_name", "a session name: lowercase Latin letters, digits, dot, hyphen, "
                           "underscore; up to 40, not starting with a dot or a hyphen"), BAD_NAME)
    if kind in ("session.requested", "session.started"):
        bad = [f for f in ("session_id", "profile") if data.get(f) is not None and not SAFE_ID.fullmatch(str(data[f]))]
        if bad:
            return forms.Why(T("core.admit.bad_id", "{field}: Latin letters, digits, dot, hyphen, underscore — not a path "
                               "and not an option").format(field=bad[0]), BAD_ID)
        if kind == "session.started" and any(data.get(f) is not None for f in ("pid", "pane", "socket")):
            return forms.Why(T("core.admit.pid", "the core knows the session's pid, not the mailbox"), PID)
    return None


def flagged(seq, kind, src, data, why):
    """spool.flagged data: which fact and why it would be refused — strings from the file are shortened."""
    channel = data.get("channel")
    return {"of": seq, "type": str(kind)[:FIELD], "src": str(src)[:FIELD],
            "channel": None if channel is None else str(channel)[:FIELD], "code": forms.code_of(why), "reason": str(why)}


def setting(config):
    """hub.json admit.mode: auto when absent; anything but auto, flag or refuse (a typo, a wrong type) — flag: a broken
    setting never starts refusing."""
    if not isinstance(config, dict) or "admit" not in config:
        return "auto"
    sec = config["admit"]
    if not isinstance(sec, dict):
        return "flag"
    value = sec.get("mode", "auto")
    return value if isinstance(value, str) and value in SETTINGS else "flag"


def mode(st):
    """The mode the core checks in now (the last spool.mode)."""
    m = (st.get("admit") or {}).get("mode")
    return m if m in MODES else MODE


def window(st, now):
    """(days with checked facts, flags) over the last WINDOW_DAYS UTC days up to now, today included."""
    today = int(now // DAY)
    seen = flags = 0
    for day, (n, f) in ((st.get("admit") or {}).get("days") or {}).items():
        if today - WINDOW_DAYS < int(day) <= today:
            seen, flags = seen + (n > 0), flags + f
    return seen, flags


def want(config_setting, st, now):
    """The mode the core should be in: flag and refuse as set; auto — refuse once the window is clean, and a refusal it
    switched on itself does not relax by itself (refused facts are flags too: otherwise one forged file would take the
    hub back to flag mode). A refusal the owner set and then took off goes by the window again."""
    if config_setting in MODES:
        return config_setting
    if mode(st) == "refuse" and (st.get("admit") or {}).get("set") == "auto":
        return "refuse"
    seen, flags = window(st, now)
    return "refuse" if seen >= CLEAN_DAYS and not flags else "flag"


def fold(st, ev):
    """spool.checked, spool.flagged, spool.mode — written by the core only: from the mailbox such a file is replaced by
    the spool.refused stub (daemon.spooled); a mailbox fact of an older core (src not core) neither counts in the window
    nor switches the mode."""
    if ev.get("src", "core") != "core":
        return
    kind, data = ev["type"], ev.get("data") or {}
    a = st.setdefault("admit", {})
    if kind == "spool.mode":
        if data.get("mode") in MODES:
            a["mode"], a["set"], a["since"] = data["mode"], data.get("set"), ev.get("ts")
        return
    checked = data.get("n") if kind == "spool.checked" else 0
    if isinstance(checked, bool) or not isinstance(checked, int) or not isinstance(ev.get("ts"), (int, float)):
        return
    day = int(ev["ts"] // DAY)
    days = a.setdefault("days", {})
    c = days.setdefault(str(day), [0, 0])
    c[0] += checked
    c[1] += kind == "spool.flagged"
    for old in [d for d in days if int(d) <= day - WINDOW_DAYS]:
        del days[old]   # out of any window that can still come
