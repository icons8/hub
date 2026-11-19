"""Spending and limits from the journal (owner request 2026-10-05: a plugin with limits and statistics) - for the
preinstalled "Spending" plugin (/api/usage).

Spending is the increments of cost_usd in turn.result. claude counts it cumulatively over the whole conversation: a
process restart with --resume does not reset it (05.10: one session went 614 -> 619 -> 626 across a dozen restarts), a
fork (old journals: there have been no forks since 04.10, but their turns still occur in archives) started from the
parent's accumulated total (474.99), sometimes from an earlier point of it (553.18 with the parent at 571.04). So a turn
costs the difference from this session's previous total; for a fork's first turn the previous one is the parent's nearest
total not above its own. A total below the previous one means the process came up from a saved point (02.10: 215.83 ->
207.98) or a new conversation began: the base is likewise the session's nearest previous total not above the new one,
and if there is none the turn costs the whole total. Missed events (rotation carries over only the tail) do not lose
money: their cost goes into the next turn.

Limits are account.limits (the window's share and its reset time). A window starts its own length before the reset or at
an early reset (the share dropped noticeably: 04.10 18:41 weekly 0.99 -> 0; a percent of jitter from parallel
sessions is not a reset). The forecast uses the pace over the last hours of the window, not minutes. Prices are those
claude gives (at API rates), not the subscription bill.

Journals are read lightly: only the needed lines, an archive once (its facts go to a file next to it, <archive>.usage.json),
the current journal - what was appended since last time.
"""
import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta
import platforms
from core import journal
from core.executors import reset_passed   # one "reset has passed" boundary shared with the executor queue
from core.zones import zone

TYPES = tuple(f'"type":"{t}"'.encode() for t in ("turn.result", "session.requested", "session.renamed", "account.limits",
                                                  "core.rejected", "module.model", "codex.spent"))
CODEX = "codex"   # the engine of Codex facts — account.limits and turns in tokens
MOD = "mod:"   # a module's spending (a model call through the core, module.model) - key mod:<name> next to session names
LENGTH = {"five_hour": 5 * 3600, "seven_day": 7 * 86400, "seven_day_opus": 7 * 86400, "seven_day_sonnet": 7 * 86400}
HORIZON = {"five_hour": 5 * 3600}      # the pace for the forecast is over the last hours of the window; weekly - over a day
MIN_SPAN = 15 * 60                     # a pace over less than a quarter of an hour is not a pace
RESET_DROP = 0.05                      # the share dropped by this much - an early reset; less is jitter
DAYS = 7
TOP = 12
HEAD = 200            # the type mark is at the start of the line: {"seq":...,"ts":...,"src":...,"type":...
CHUNK = 1 << 20       # an archive is read in chunks: a chunk in memory, not the file
# archive facts go to a file next to it (an archive does not change): a restarted page does not reread archives (25.10: the
# first /api/usage after a restart read 19 GB of archives for minutes); another file format or archive size - read the archive
SIDECAR = ".usage.json"
# what _fact takes changed - FORMAT + 1 (2: Codex turns in tokens, 3: the engine of limits, the Codex host);
# the set of types (TYPES) is checked by the file itself
FORMAT = 3
KINDS = [t.decode() for t in TYPES]   # the set of fact types - in the file next to the archive
# the file of the version before Codex turns: a Claude hub's archive holds no Codex facts, so
# its facts there are the same — kept, not reread (a reread of every archive took minutes, 25.10)
BEFORE_CODEX = (1, [k for k in KINDS if k != "codex.spent"])

_files = {}   # the current journal: path -> {"ino", "pos", "facts"} - read on from where we stopped
# archives: state folder -> {"read": archives read, "facts": {seq: fact}, "reader": the reading thread}. Each archive is
# read once; session tails that rotation repeats in every next journal are stored once (15.10: 70 thousand
# facts instead of 19 thousand, 58 MB in page memory - and growing with every archive)
_archives = {}
_lock = threading.Lock()   # the page asks from several threads; held for a moment: archives are read by a separate thread


class Busy(Exception):
    """The archives are still being read (the first time after an update - minutes): ask later."""


def reset():
    _files.clear()
    _archives.clear()


def _tokens(d):
    return (d.get("tokens_in") or 0) + (d.get("tokens_out") or 0)


def _fact(e):
    t, d = e["type"], e.get("data") or {}
    f = {"seq": e["seq"], "ts": e["ts"], "type": t, "entity": e.get("entity")}
    # Codex (experimental): Codex gives no money — a turn and a call of the core's model (codex.spent: the watcher…)
    # in tokens (per turn or call, not cumulative)
    if t == "codex.spent":
        f.update(tokens=_tokens(d), text=str(d.get("model") or ""), turns=None, ms=None)
        return f
    if t == "turn.result":
        if d.get("cost_usd") is None and d.get("engine") == "codex" and _tokens(d):
            f.update(tokens=_tokens(d), text=(d.get("text") or "").strip()[:200], turns=d.get("turns"),
                     ms=d.get("duration_ms"))
            return f
        if d.get("cost_usd") is None:
            return None
        f.update(cost=d["cost_usd"], text=(d.get("text") or "").strip()[:200], turns=d.get("turns"),
                 ms=d.get("duration_ms"))
    elif t == "session.requested":
        parent = d.get("fork_of") or (d.get("raised_by") if d.get("executor") else None)
        if not parent:
            return None
        f.update(parent=parent, fork=bool(d.get("fork_of")))
    elif t == "session.renamed":
        f["to"] = d.get("to")
    elif t == "core.rejected":  # what the core rejected did not happen (01.10: a rename to a taken name)
        f["of"] = d.get("of")
    elif t == "module.model":
        # written by the page (core/modelcall.py); the same type from the module itself (hubmod.fact: the mailbox sets channel
        # plugin) is not spending
        if d.get("channel") == "plugin" or not isinstance(e.get("entity"), str):
            return None
        usd = d.get("cost_usd")
        f.update(cost=usd if isinstance(usd, (int, float)) and not isinstance(usd, bool) and usd >= 0 else None,
                 ms=d.get("ms"), model=d.get("model"), ok=bool(d.get("ok")))
    else:
        f.update(windows=d.get("windows") or {}, status=d.get("status"), account=d.get("account"), gen=d.get("gen"))
        if d.get("engine") == CODEX:   # experiment: Codex limits are another series (a Claude fact stays as it was)
            f["engine"] = CODEX
    return f


def _scan(f, out, types=TYPES, fact=None):
    """Facts from the lines of file f (from its current position) go to out; returns how many bytes were read as whole
    lines. The needed lines are found in file chunks by searching for type marks (bytes.find), not by looping over
    lines: an archive has 120 thousand lines, only a few percent are needed (25.10: line-by-line parsing of 19 GB of
    archives took minutes, /api/usage did not answer)."""
    pos, buf, skipped = 0, b"", None   # read as whole lines; a half-written line; the length of the line being skipped
    while True:
        block = f.read(CHUNK)
        if not block:
            return pos
        if skipped is not None:   # the end of a long unneeded line (a state snapshot) - do not hold it in memory
            nl = block.find(b"\n")
            if nl < 0:
                skipped += len(block)
                continue
            pos += skipped + nl + 1
            skipped, block = None, block[nl + 1:]
        buf += block
        end = buf.rfind(b"\n") + 1
        for line in _lines(buf, end, types):
            try:
                got = (fact or _fact)(json.loads(line))
            except (ValueError, KeyError) + ((TypeError, AttributeError) if fact else ()):
                continue   # usage's own facts: a broken one is a bug to see, not to skip
            if got:
                out(got)
        pos += end
        buf = buf[end:]
        if len(buf) > HEAD and not any(t in buf[:HEAD] for t in types):
            skipped, buf = len(buf), b""


def _lines(buf, end, types=TYPES):
    """Lines of buf[:end] with a needed type mark in the first HEAD bytes - in file order."""
    hits = []
    for t in types:
        i = buf.find(t, 0, end)
        while i >= 0:
            start = buf.rfind(b"\n", 0, i) + 1
            stop = buf.find(b"\n", i, end) + 1
            if i + len(t) - start <= HEAD:
                hits.append((start, stop))
            i = buf.find(t, stop, end)
    for start, stop in sorted(set(hits)):
        yield buf[start:stop]


def _read(path):
    try:
        st = os.stat(path)
    except FileNotFoundError:
        _files.pop(path, None)
        return []
    c = _files.get(path)
    if not c or c["ino"] != st.st_ino or st.st_size < c["pos"]:  # a new file, or the journal was replaced by rotation
        c = _files[path] = {"ino": st.st_ino, "pos": 0, "facts": []}
    if st.st_size > c["pos"]:
        with open(path, "rb") as f:
            f.seek(c["pos"])
            c["pos"] += _scan(f, c["facts"].append)
    return c["facts"]


def _take(state_dir, paths):
    """The thread that reads the archives paths: each one's facts go into the shared ones by seq, without repeats."""
    a = _archives[state_dir]
    current = os.path.join(state_dir, "journal.jsonl")
    for p in paths:
        try:
            st = os.stat(p)
            with _lock:   # the archive is the former current journal (rotation): take what was read from it, read the rest
                c = _files.get(current)
                same = c and c["ino"] and c["ino"] == st.st_ino   # without a file number (0) it cannot be matched - read whole
                known = (list(c["facts"]), c["pos"]) if same and c["pos"] <= st.st_size else None
            got = _archive(p, known) or []
        except FileNotFoundError:   # the archive was removed while we were looking
            got = []
        except Exception as e:   # noqa: BLE001 - an unreadable archive must not hold up the others nor repeat endlessly
            print(f"usage: archive {os.path.basename(p)} not read: {e!r}", file=sys.stderr, flush=True)
            got = []
        with _lock:
            a["read"].add(p)   # counted as read even if it failed below: otherwise facts() would keep starting a thread on it forever
            for fact in got:
                a["facts"].setdefault(fact["seq"], fact)


def _archive(path, known=None):
    """The facts of one archive: from the file next to it (<archive>.usage.json), if there is none - by reading the archive,
    and the file next to it is written for later. known - (facts, up to which byte) already read from this same file while
    it was the current journal: only the rest is read. No archive - None."""
    try:
        size = os.path.getsize(path)
    except FileNotFoundError:
        return None
    try:
        with open(path + SIDECAR, encoding="utf-8") as f:
            saved = json.load(f)
        got = saved.get("facts")
        if (saved.get("format"), saved.get("types")) in ((FORMAT, KINDS), BEFORE_CODEX) and saved.get("size") == size \
                and isinstance(got, list) and all(isinstance(x, dict) and isinstance(x.get("seq"), int)
                                                and isinstance(x.get("ts"), (int, float)) and isinstance(x.get("type"), str)
                                                for x in got):
            return got
    except (OSError, ValueError, KeyError, AttributeError):
        pass
    got, pos = known or ([], 0)
    try:
        with open(path, "rb") as f:
            f.seek(pos)
            _scan(f, got.append)
    except FileNotFoundError:
        return None
    tmp = f"{path}{SIDECAR}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"format": FORMAT, "types": KINDS, "size": size, "facts": got}, f, ensure_ascii=False,
                      separators=(",", ":"))
        platforms.current.replace(tmp, path + SIDECAR)
    except (OSError, ValueError, TypeError):   # a read-only folder, text that cannot be written - read again next time
        try:
            os.remove(tmp)
        except OSError:
            pass
    return got


def facts(state_dir, wait=None):
    """The needed events of all journals by seq, without repeats (rotation carries session tails into the new journal).
    Unread archives are read by a separate thread; wait - how many seconds to wait for it (None - as long as needed), if
    it did not finish - Busy."""
    end = None if wait is None else time.monotonic() + wait
    while True:
        with _lock:
            a = _archives.setdefault(state_dir, {"read": set(), "facts": {}, "reader": None})
            pending = [p for p in journal.archives(state_dir) if p not in a["read"]]
            if not pending:
                by = dict(a["facts"])
                for f in _read(os.path.join(state_dir, "journal.jsonl")):
                    by.setdefault(f["seq"], f)
                break
            reader = a["reader"]
            if not reader or not reader.is_alive():
                reader = a["reader"] = threading.Thread(target=_take, args=(state_dir, pending), daemon=True,
                                                        name="usage-archives")
                reader.start()
        reader.join(None if end is None else max(0.0, end - time.monotonic()))
        if reader.is_alive():
            raise Busy()
    rejected = {f["of"] for f in by.values() if f["type"] == "core.rejected"}
    return [by[s] for s in sorted(by) if s not in rejected and by[s]["type"] != "core.rejected"]


def spends(items):
    """Turns with a price: [{session, ts, usd, text, turns, ms}] and session parents {fork or executor: parent}. A model
    call by a module is a separate item with key mod:<name> (the price is per call, not cumulative); without a price - usd None."""
    last, seen, forks, parents, out = {}, {}, {}, {}, []
    for f in items:
        n, t = f["entity"], f["type"]
        if t == "module.model" and f["cost"] != 0:
            out.append({"session": MOD + n, "ts": f["ts"], "usd": f["cost"], "text": f.get("model") or "",
                        "turns": None, "ms": f["ms"]})
        elif t == "session.requested":
            parents[n] = f["parent"]
            if f["fork"]:
                forks[n] = list(seen.get(f["parent"]) or [])
        elif t == "session.renamed" and f.get("to"):
            for box in (last, seen, forks, parents):  # a copy: events under the old name also occur afterwards (05.10)
                if n in box:
                    box[f["to"]] = list(box[n]) if isinstance(box[n], list) else box[n]
        elif t in ("turn.result", "codex.spent") and "tokens" in f:   # a Codex turn or core model call: "usd" holds tokens
            if f["tokens"] > 0:
                out.append({"session": n, "ts": f["ts"], "usd": f["tokens"], "text": f["text"], "turns": f["turns"],
                            "ms": f["ms"], "tokens": True})
        elif t == "turn.result":
            if n in forks:  # a fork's first turn: the base is the parent's nearest total not above its own
                prev = max((v for v in forks.pop(n) if v <= f["cost"]), default=0)
            else:
                prev = last.get(n, 0)
                if f["cost"] < prev:  # came up from an earlier point, or a new conversation
                    prev = max((v for v in seen[n] if v <= f["cost"]), default=0)
            seen.setdefault(n, []).append(f["cost"])
            usd = f["cost"] - prev
            last[n] = f["cost"]
            if usd > 0:
                out.append({"session": n, "ts": f["ts"], "usd": usd, "text": f["text"], "turns": f["turns"], "ms": f["ms"]})
    return out, parents


def _sum(items):
    by = {}
    for x in items:
        by[x["session"]] = by.get(x["session"], 0) + x["usd"]
    return {k: round(v, 4) for k, v in sorted(by.items(), key=lambda kv: -kv[1])}


def codex_length(name):
    """A Codex window of another length (experiment, hosts/codex_limits: codex_<N>m) - its length, seconds; otherwise None.
    One rule for "Usage" and the economy (core/budget)."""
    m = re.fullmatch(r"codex_([1-9][0-9]{0,6})m", name) if isinstance(name, str) else None
    return int(m.group(1)) * 60 if m else None


def _length(name):
    """A window's length, seconds: Claude's by name; Codex's unknown one by its minutes; otherwise None."""
    return LENGTH.get(name) or codex_length(name)


def windows(items, paid, now, account=None, engine=None):
    """Limit windows from the active account's latest account.limits: share, reset, start, spending in the window, the
    price of a percent, pace and forecast. account - the core's st["account"]: an account pool - the active one's series
    (different accounts' shares of the same window are different series, even with a reset at the same hour; an old
    process of another account writes after the switch too), without the limits of an old token if the active account
    took a fresh one (as reducer: account.limits). engine "codex" (experiment) - the series of Codex limits: one sign-in
    of the hub, no account name, never the Claude pool's; otherwise Claude's facts only."""
    account = account or {}
    active = account.get("active")
    fresh = (account.get("refreshed") or {}).get(active) or 0
    if engine == CODEX:
        points = [f for f in items if f["type"] == "account.limits" and f.get("engine") == CODEX]
    else:
        points = [f for f in items if f["type"] == "account.limits" and f.get("engine") != CODEX
                  and f.get("account") == active and (f.get("gen") or 0) >= fresh]
    if not points:
        return [], None
    latest = points[-1]
    out = []
    for name, w in latest["windows"].items():
        length = _length(name) if isinstance(w, dict) else None
        if not length or not isinstance(w.get("used"), (int, float)):
            continue
        resets = w.get("resets") or 0
        start = resets - length if resets else latest["ts"] - length
        series = [(p["ts"], p["windows"][name]["used"]) for p in points
                  if isinstance((p["windows"].get(name) or {}).get("used"), (int, float))
                  and (p["windows"][name].get("resets") or 0) == resets]
        for (_, a), (ts, b) in zip(series, series[1:]):   # early reset: the share dropped, the deadline is the same
            if b <= a - RESET_DROP and ts > start:
                start = ts
        series = [(ts, u) for ts, u in series if ts >= start]
        gone = reset_passed(w, now)
        used = 0 if gone else w["used"]
        inside = [x for x in paid if x["ts"] >= start] if not gone else []
        total = round(sum(x["usd"] for x in inside), 4)
        rate = eta = at_reset = None
        if not gone and series:
            ts1, u1 = series[-1]
            ref = next(((ts, u) for ts, u in series if ts >= max(start, now - HORIZON.get(name, 86400))), None)
            if not ref or ref[0] >= ts1:
                ref = (start, 0)
            if ts1 - ref[0] >= MIN_SPAN and u1 > ref[1]:
                rate = (u1 - ref[1]) / (ts1 - ref[0])      # share per second
                eta = ts1 + (1 - u1) / rate
                at_reset = min(u1 + rate * (resets - ts1), 9.99) if resets else None
        out.append({"name": name, "used": used, "resets": resets or None, "start": start, "total": total,
                    "by": _sum(inside), "per_pct": round(total / (used * 100), 4) if used >= 0.01 else None,
                    "rate_h": rate * 100 * 3600 if rate else None, "eta": eta, "at_reset": at_reset,
                    "seen": series[-1][0] if series else None,
                    "gone": gone})   # the reset has passed: `used` is 0 not by a measurement - the page says "limit reset, no fresh reading"
    order = list(LENGTH)
    out.sort(key=lambda w: (order.index(w["name"]) if w["name"] in order else len(order), _length(w["name"])))
    return out, latest.get("status")


def report(state_dir, now=None, tz="UTC", account=None, wait=None):
    """The "Spending" report; wait - how long to wait for the archives to be read (facts), if not in time - Busy.
    Codex (experimental): Codex gives tokens, not dollars - the two never go into one sum. The main part is in
    dollars or in tokens (unit) - of the engine heard from last in the shown days (turns, limits); a hub with both gets
    the other one's part next to it, under "other", of the same form."""
    now = time.time() if now is None else now
    tzone = zone(tz)   # an unknown zone or no zone database - core/zones decides itself
    items = facts(state_dir, wait)
    paid, parents = spends(items)
    unpriced = [x for x in paid if x["usd"] is None]   # a module's model call without a price (timed out) is not zero
    paid = [x for x in paid if x["usd"] is not None]
    today = datetime.fromtimestamp(now, tzone).date()
    first = today - timedelta(days=DAYS - 1)
    shown = datetime(first.year, first.month, first.day, tzinfo=tzone).timestamp()
    usd = [x for x in paid if not x.get("tokens")]
    tokens = [x for x in paid if x.get("tokens")]
    # an engine is on the page when the shown days have its turns or its limits (a Codex trial a month ago - not). The
    # main part: dollars while Claude has turns in the shown days (the page does not jump on a hub with both); without
    # them - the engine heard from last (a hub moved to Codex: Codex on top, Claude's last reading under it)
    limits = [f for f in items if f["type"] == "account.limits" and f["ts"] >= shown]
    last = {"tokens": max([x["ts"] for x in tokens if x["ts"] >= shown] +
                          [f["ts"] for f in limits if f.get("engine") == CODEX], default=None),
            "usd": max([x["ts"] for x in usd if x["ts"] >= shown] +
                       [f["ts"] for f in limits if f.get("engine") != CODEX], default=None)}
    turns = any(x["ts"] >= shown for x in usd)
    main = "tokens" if last["tokens"] is not None and not turns and (last["usd"] or 0) < last["tokens"] else "usd"
    other = "usd" if main == "tokens" else "tokens"

    def part(paid, engine):
        days = []
        for i in range(DAYS - 1, -1, -1):
            day = today - timedelta(days=i)
            a = datetime(day.year, day.month, day.day, tzinfo=tzone).timestamp()
            b = (datetime(day.year, day.month, day.day, tzinfo=tzone) + timedelta(days=1)).timestamp()
            inside = [x for x in paid if a <= x["ts"] < b]
            days.append({"day": day.isoformat(), "start": a, "total": round(sum(x["usd"] for x in inside), 4),
                         "by": _sum(inside)})
        wins, status = windows(items, paid, now, account, engine)
        since = days[0]["start"]
        # the costliest turns are the sessions': short model calls by modules (hundreds at a cent) would crowd them out on
        # a quiet hub; codex:<who> - the core's Codex model (experiment), like a module
        top = sorted((x for x in paid if x["ts"] >= since and not x["session"].startswith((MOD, "codex:"))),
                     key=lambda x: -x["usd"])[:TOP]
        return {"days": days, "windows": wins, "status": status,
                "top": [{**x, "usd": round(x["usd"], 4)} for x in top]}

    of = {"tokens": lambda: part(tokens, CODEX), "usd": lambda: part(usd, None)}
    out = {"now": now, "tz": str(tzone), "unit": main, **of[main](), "parents": parents,
           "unpriced": _count(x for x in unpriced if x["ts"] >= shown)}
    if last[other] is not None:
        out["other"] = {"unit": other, **of[other]()}
    return out


def _count(items):
    by = {}
    for x in items:
        by[x["session"]] = by.get(x["session"], 0) + 1
    return by
