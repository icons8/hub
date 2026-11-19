"""`hub session kit`: a session's skills and MCP as the engine loaded them against what it really used, and the same for
the whole hub (`--fleet`) — the data of the page's "Skills and MCP", without the page (spec d2547143, core/uses.py).

The numbers are not computed here: `uses.report` (one session) and `uses.fleet` (the hub) give them, the page and this
command read the same answer. This file asks for it (the journal's digests, built here when an archive has none) and
writes it down as a compact text; `--json` is the answer as it is.

stdlib only, no hub imports at the top (bin/hub loads it on this command only)."""
import datetime
import sys
import time

LIST_NAMES = 12   # names of what was never opened in a line: the most expensive first, the rest by count
STATUS = {"connected": "web.kit.mcp_connected", "failed": "web.kit.mcp_failed", "needs-auth": "web.kit.mcp_auth",
          "disabled": "web.kit.mcp_disabled", "pending": "web.kit.mcp_pending"}   # uses.mcp_status's words, the page's texts


def answer(state_dir, sessions, name, days, fleet=False, now=None):
    """The data: uses.fleet of the hub or uses.report of session `name`, with the journal's digests made first when an
    archive has none (the page does it in the background, a command waits). None — no such session."""
    from core import uses
    slim = {"sessions": {n: {k: s.get(k) for k in uses.STATE_KEYS} for n, s in sessions.items()}}
    if not fleet and (name not in sessions or sessions[name].get("state") == "removed"):
        return None
    reader = uses.Reader(state_dir)
    if not reader.digests_now(background=False)[1]:
        print(_t("cli.kit.digesting"), file=sys.stderr, flush=True)
        try:
            reader.build_here()
        except OSError as e:   # a folder the command cannot write to: what is digested counts, the answer says so
            print(f"uses: {e!r}", file=sys.stderr)
    now = time.time() if now is None else now
    if fleet:
        return uses.fleet(reader, slim, days, now, background=False)
    return uses.report(reader, slim, name, days, now, background=False)


def _t(key, **kw):
    from core import i18n
    return i18n.t(key, **kw)


def _when(ts, tz):
    if not ts:
        return _t("web.kit.never")
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(tz or "UTC")
    except Exception:   # noqa: BLE001 — an unknown zone: UTC
        zone = datetime.timezone.utc
    return datetime.datetime.fromtimestamp(ts, zone).strftime("%m-%d %H:%M")


def _period(days):
    return _t({7: "web.kit.days_7", 30: "web.kit.days_30"}.get(days, "web.kit.days_all"))


def _table(rows):
    """Left-aligned columns, two spaces apart; the first row is the header."""
    width = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    return ["  " + "  ".join(str(c).ljust(w) for c, w in zip(r, width)).rstrip() for r in rows]


def _names(rows, tokens=False):
    """`a (84), b (88)` — the heaviest first, up to LIST_NAMES, then how many more."""
    rows = sorted(rows, key=lambda r: (-(r.get("tokens") or 0), r["name"]))
    out = ", ".join(f"{r['name']} ({r['tokens']})" if tokens and r.get("tokens") else r["name"] for r in rows[:LIST_NAMES])
    return out + (", " + _t("cli.kit.more", count=len(rows) - LIST_NAMES) if len(rows) > LIST_NAMES else "")


def _calls(u):
    return (u.get("tool") or 0) + (u.get("command") or 0) + (u.get("file") or 0) if u else 0


def _volume(own, ex=None):
    """The period's denominator, in the page's words: the session's conversations and turns, its executors' too."""
    return _t("web.kit.volume", **own) + (f"; {_t('web.kit.volume_executors', **ex)}" if ex else "")


def session(d, tz=None):
    """The lines of one session's answer (uses.report)."""
    kit = d.get("kit")
    head = _t("cli.kit.head", name=d["session"], period=_period(d["days"]),
              when=_when(kit["at"], tz)) if kit else _t("cli.kit.no_set", name=d["session"])
    out = [head]
    if kit:
        listed = kit.get("listed") or {}
        parts = [_t("cli.kit.in_set", count=len(kit.get("skills") or []))]
        if listed:
            parts.append(_t("cli.kit.in_list", count=listed.get("in_prompt")))
            if isinstance(listed.get("tokens"), int):
                parts.append(_t("web.kit.list_tokens", count=listed["tokens"]))
        out.append("; ".join(parts))
    own, ex, vol = d["used"]["own"], d["used"]["executors"], d["volume"]
    out.append(_volume(vol["own"], vol["executors"] if d["used"]["executors_count"] else None))
    if not d["complete"]:
        out.append(_t("cli.kit.reading", when=_when(d.get("read_from"), tz)))
    skills = {r["name"]: r for r in (kit or {}).get("skills") or []}
    keys = {k[6:] for k in (*own, *ex) if k.startswith("skill:")}
    opened = sorted((n for n in skills.keys() | keys if _calls(own.get("skill:" + n)) + _calls(ex.get("skill:" + n))),
                    key=lambda n: (-(_calls(own.get("skill:" + n)) + _calls(ex.get("skill:" + n))), n))
    if opened:
        out += ["", _t("cli.kit.opened", opened=len(opened), loaded=len(skills))]
        show_ex = d["used"]["executors_count"]
        rows = [(_t("cli.kit.col_skill"), _t("cli.kit.col_calls"), *([_t("cli.kit.col_executors")] if show_ex else []),
                 _t("cli.kit.col_convs"), _t("cli.kit.col_last"), _t("cli.kit.col_tokens"))]
        for n in opened:
            o, e = own.get("skill:" + n) or {}, ex.get("skill:" + n) or {}
            rows.append((n, _calls(o), *([_calls(e)] if show_ex else []), f"{o.get('convs', 0)}/{vol['own']['conversations']}",
                         _when(max(o.get("last") or 0, e.get("last") or 0), tz), (skills.get(n) or {}).get("tokens") or "—"))
        out += _table(rows)
    rest = [r for n, r in skills.items() if n not in opened]
    if rest:
        weight = sum(r.get("tokens") or 0 for r in rest)
        out += ["", _t("cli.kit.not_opened", count=len(rest)) + (", " + _t("web.kit.list_tokens", count=weight) if weight else "")
                + ": " + _names(rest, tokens=True)]
    servers = _servers(d)
    if servers:
        out += ["", _t("cli.kit.mcp", count=len(servers))]
        out += _table([(_t("cli.kit.col_server"), _t("cli.kit.col_status"), _t("cli.kit.col_tools"),
                        _t("cli.kit.col_calls"), _t("cli.kit.col_last"))] + [
            (n, r["status"], r["tools"], r["calls"], _when(r["last"], tz)) for n, r in servers])
    return out


def _servers(d):
    """[(server, {status, tools, calls, last})] of one session's answer, the most used first."""
    own, ex = d["used"]["own"], d["used"]["executors"]
    names = {*(d.get("mcp_status") or {}), *(m["name"] for m in (d.get("kit") or {}).get("mcp") or [])}
    names |= {k[4:].partition("/")[0] for k in (*own, *ex) if k.startswith("mcp:")}
    rows = {}
    for n in names:
        mine = [u for box in (own, ex) for k, u in box.items() if k.startswith(f"mcp:{n}/")]
        status = (d.get("mcp_status") or {}).get(n)
        rows[n] = {"status": _t(STATUS.get(status, "web.kit.mcp_not_now")),
                   "tools": next((m["tools"] for m in (d.get("kit") or {}).get("mcp") or [] if m["name"] == n), "—"),
                   "calls": sum(_calls(u) for u in mine), "last": max((u.get("last") or 0 for u in mine), default=0)}
    return sorted(rows.items(), key=lambda kv: (-kv[1]["calls"], kv[0]))


def fleet(d, tz=None):
    """The lines of the hub's answer (uses.fleet)."""
    s, listed = d["sessions"], d["listed"]
    out = [_t("cli.kit.fleet_head", period=_period(d["days"]), sessions=s["with_set"], active=s["active"]),
           _volume(d["volume"])]
    if listed["known"]:
        out.append(_t("cli.kit.fleet_tokens", tokens=round(listed["tokens"] / listed["known"]), count=listed["known"]))
    if not d["complete"]:
        out.append(_t("cli.kit.reading", when=_when(d.get("read_from"), tz)))
    used = [r for r in d["skills"] if r["calls"]]
    if used:
        out += ["", _t("cli.kit.fleet_skills", opened=len(used), total=len(d["skills"]))]
        out += _table([(_t("cli.kit.col_skill"), _t("cli.kit.col_loaded"), _t("cli.kit.col_calls"),
                        _t("cli.kit.col_used_by"), _t("cli.kit.col_convs"), _t("cli.kit.col_last"),
                        _t("cli.kit.col_tokens"))] + [
            (r["name"], r["loaded"], r["calls"], r["used_by"], r["convs"], _when(r["last"], tz), r.get("tokens") or "—")
            for r in used])
    rest = [r for r in d["skills"] if not r["calls"] and r["loaded"]]
    if rest:
        # what is loaded in most sessions and never opened weighs the most: by the tokens times the sessions
        rest.sort(key=lambda r: (-(r.get("tokens") or 0) * r["loaded"], r["name"]))
        weight = sum((r.get("tokens") or 0) * r["loaded"] for r in rest)
        out += ["", _t("cli.kit.fleet_not_opened", count=len(rest))
                + (", " + _t("cli.kit.summed", weight=_t("web.kit.list_tokens", count=weight)) if weight else "") + ": " + ", ".join(
            f"{r['name']} ×{r['loaded']}" for r in rest[:LIST_NAMES]) + (
            ", " + _t("cli.kit.more", count=len(rest) - LIST_NAMES) if len(rest) > LIST_NAMES else "")]
    if d["mcp"]:
        out += ["", _t("cli.kit.mcp", count=len(d["mcp"]))]
        out += _table([(_t("cli.kit.col_server"), _t("cli.kit.col_loaded"), _t("cli.kit.col_tools"),
                        _t("cli.kit.col_calls"), _t("cli.kit.col_used_by"), _t("cli.kit.col_last"))] + [
            (r["name"], r["loaded"], r["tools"] or "—", r["calls"], r["used_by"], _when(r["last"], tz)) for r in d["mcp"]])
    return out
