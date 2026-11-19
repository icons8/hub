"""A fresh limits measurement by the login module that leads session login — the "Refresh" button of Settings → Accounts
and `hub accounts probe` while a module leads (its pool is its own): the core asks its cli point (`accounts probe
--json`), the module measures each account with its own token and puts account.limits into the mailbox itself, each with
a req label; the core waits for its own decision on each reading. In the core, not in the built-in pool (modules/accounts):
the open flavor is cut without it (core/cutout.py) and a login module still measures there."""
import json

from core import i18n

# window -> catalog key of its label; windows for the whole account, weekly Opus/Sonnet are for models only
WINDOWS = {"five_hour": "core.budget.window_five_hour", "seven_day": "core.budget.window_seven_day"}


def _say(key, **params):
    """A catalog text in the process's language (a session reads English, the owner's terminal his)."""
    return i18n.tr(i18n.current(), key, **params)


MODULE_PROBE = "accounts probe --json"   # the login module's measurement: its cli point (the accounts module 1.1.0)


def module_probe(mods, module, names, caller, version=None):
    """A fresh limits measurement by the login module that leads session login (its pool is its own): its cli point,
    `accounts probe --json [<name>…]` -> (results, None) in probe()'s form — [{name, code, limits?, status?, error?,
    req?}], the module writes account.limits itself, each with the req label to wait for — or (None, the refusal to
    show after "the login module <name>: "). A module without the measurement (the accounts module before 1.1.0 prints its help) is told to be updated,
    a silent one — to hub doctor. caller — who asks, as modcli.caller; version — the module's, for the text."""
    got = mods.call(module, "cli", {"command": "accounts", "args": MODULE_PROBE.split()[1:] + list(names),
                                    "caller": caller})
    if got is None:
        return None, _say("core.accounts.probe_silent")
    try:
        answer = json.loads(got.get("out") or "")
    except ValueError:   # its help (no measurement) or {stdin: true}
        answer = None
    if isinstance(answer, dict) and isinstance(answer.get("error"), str) and answer["error"].strip():
        return None, answer["error"].strip()[:300]
    rows = answer.get("results") if isinstance(answer, dict) else None
    if not (isinstance(rows, list) and all(isinstance(r, dict) and isinstance(r.get("name"), str)
                                           and isinstance(r.get("code"), str) for r in rows)):
        return None, (_say("core.accounts.probe_old_version", version=version) if version
                      else _say("core.accounts.probe_old"))
    keep = {"status": int, "error": str, "req": str}
    return [{"name": r["name"][:80], "code": r["code"][:40],
             **{k: r[k] for k, kind in keep.items() if isinstance(r.get(k), kind) and not isinstance(r.get(k), bool)},
             **({"limits": _shown(r.get("limits"))} if r["code"] == "ok" else {})}
            for r in rows], None


def _shown(lim):
    """The limits of a module's reading for the lines: its windows of the right form only (a wrong one is not shown —
    the core's fold takes the reading itself from the module's account.limits)."""
    def number(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v < 1e11   # a reset: before 5138
    lim = lim if isinstance(lim, dict) else {}
    wins = lim.get("windows") if isinstance(lim.get("windows"), dict) else {}
    return {"status": lim.get("status") if isinstance(lim.get("status"), str) else None,
            "windows": {k: {f: w[f] for f in ("used", "resets") if number(w.get(f))}
                        for k, w in wins.items() if k in WINDOWS and isinstance(w, dict)}}


def recorded(path, reqs, wait, start):
    """{account: (seq, None) accepted | (seq, reason) refused | (None, None) not recorded} for the readings put into the
    core's mailbox with the req labels {account: req} after the journal mark start — one wait for all of them."""
    from core import journal
    got = journal.decided_all(path, list(reqs.values()), wait, 1 << 20, start=start)
    return {a: got[req] for a, req in reqs.items()}
