#!/usr/bin/env python3
"""Measure the owner's communication with the hub from the journal: a baseline to compare changes in how they talk against.

    tools/comms_stats.py [--since DATE] [--until DATE] [--hub NAME] [--json] [journal …]

Journals are paths to JSONL files (the current one and the archives `journal-archive/*.jsonl`, in any order; repeats by
seq are dropped, broken lines skipped); without paths — `journal.jsonl` in the hub's state folder. The window is dates
YYYY-MM-DD in local time (`--until` inclusive) or `@<unix-time>`; the default is the last 7 days. Events of the window
are counted; replies that arrived after the window count if they are in the journal.

What the report holds:
- owner messages by channel (telegram / web / other), the share of voice messages (the text starts with «🎤»), and who
  they went to — the hub or a session directly;
- Telegram bot messages per owner message. Bot sends are not written to the journal, so there are two estimates:
  "by numbers" — Telegram message numbers in the chat are shared by the bot and the owner, the gap between the numbers
  of two neighbouring owner messages is how many the bot sent (overstates: button presses are not messages, but a text
  reply to a card and an untranscribed voice message do not get into the journal as messages and are counted as the
  bot's); "by events" — events that modules/telegram/bot.py turns into a send: the hub's turn result, an executor's
  result for a Telegram task, the turn result for a Telegram message, a question card (one message per question) and
  permissions, a card-closed mark, a monitor alert, a file from a session, a reminder (understates: a long text goes
  out as several messages, scheme pictures and voice messages are not counted);
- the time from an owner message to the first reply to it: the turn result (not empty, not "—", not an error) of the
  session they wrote to, in a turn marked with their message mN (or in a turn begun before the message — it was merged
  into the running turn); "picked up" — before the start of the turn marked mN; median and p90. Without a reply —
  separately the cases where their next message to the same addressee came before any reply (the turn was
  interrupted, the reply came on the next one);
- tasks created on their message (delegation.created with for=mN): up to the executor's first result and from the task
  to delegation.done; open ones;
- repeated questions ("where is the answer", "well?", "are you alive", "why no report"…) — the FOLLOWUP list below;
- question cards (permission.requested) and what became of them: answered (by channel), closed by the owner, withdrawn
  by the session, unanswered by the end of the journal (does not mean "hanging": a card without an event could be
  removed by a timeout or a session exit); by session.
"""
import argparse
import bisect
import collections
import datetime
import glob
import itertools
import json
import math
import os
import re
import statistics
import sys
import time

TYPES = ("message.received", "turn.started", "turn.result", "delegation.created", "delegation.done",
         "permission.requested", "permission.answered", "question.dismissed", "question.withdrawn",
         "session.requested", "core.snapshot", "owner.notify", "owner.file", "timer.fired")
NEEDLES = tuple(f'"type":"{t}"'.encode() for t in TYPES) + tuple(f'"type": "{t}"'.encode() for t in TYPES)
QUIET = ("", "—", "-")
VOICE = re.compile(r"^🎤[^:]*:\s*")


def language_data():
    """The language files (i18n/data/<language>.json), read once with the stdlib alone: tools/ runs without the core. A
    missing or broken file is a broken install and fails loudly."""
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "i18n", "data")
    out = []
    for name in sorted(os.listdir(root)):
        if name.endswith(".json"):
            with open(os.path.join(root, name), encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError(f"{name}: not an object")
            out.append(data)
    return out


def recognised(data, section, key):
    """The values of every language's file joined into one list of strings (a string value is one item)."""
    out = []
    for lang in data:
        v = (lang.get(section) or {}).get(key, [])
        out += [v] if isinstance(v, str) else v
    return out


LANGUAGES = language_data()
# a follow-up: the owner asks where the answer is or whether the session is alive (the phrases are language data)
FOLLOWUP = [re.compile(p, re.I) for p in recognised(LANGUAGES, "comms", "followup")]
HUB_WORDS = ("hub", *recognised(LANGUAGES, "words", "hub"))   # how a message's "to" names the hub
REPLY_CAP = 24 * 3600   # a reply later than a day is no longer a reply to this message


def read(paths):
    """Events of the needed types from the journals, by seq, without repeats; reads line by line."""
    seen, out = set(), []
    for path in paths:
        with open(path, "rb") as f:
            for line in f:
                if not any(n in line for n in NEEDLES):
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(e, dict) or e.get("type") not in TYPES or e.get("seq") in seen:
                    continue
                seen.add(e.get("seq"))
                if e["type"] == "core.snapshot":   # the snapshot is large: only who the hub is is needed from it
                    sessions = ((e.get("data") or {}).get("state") or {}).get("sessions") or {}
                    e["data"] = {"hubs": [n for n, s in sessions.items() if (s or {}).get("kind") == "hub"]}
                out.append(e)
    out.sort(key=lambda e: (e.get("ts") or 0, e["seq"] if isinstance(e["seq"], int) else 0))
    return out


def dist(values):
    a = sorted(values)
    if not a:
        return {"n": 0, "median_s": None, "p90_s": None}
    return {"n": len(a), "median_s": round(float(statistics.median(a)), 1),
            "p90_s": round(float(a[max(0, math.ceil(0.9 * len(a)) - 1)]), 1)}


def answered(values, total):
    return {"n": total, "answered": len(values), "unanswered": total - len(values),
            **{k: v for k, v in dist(values).items() if k != "n"}}


def strip_voice(text):
    return VOICE.sub("", text or "").strip()


def is_followup(text):
    t = strip_voice(text)
    return any(p.search(t) for p in FOLLOWUP)


def text_of(e):
    return ((e.get("data") or {}).get("text") or "").strip()


def is_reply(e):
    d = e.get("data") or {}
    return not d.get("is_error") and text_of(e) not in QUIET


def collect(paths, since, until, hubs=()):
    events = read(paths)
    hubs = set(hubs)
    for e in events:
        d = e.get("data") or {}
        if e["type"] == "session.requested" and d.get("kind") == "hub" and e.get("entity"):
            hubs.add(e["entity"])
        elif e["type"] == "core.snapshot":
            hubs.update(d.get("hubs") or [])
    hub = sorted(hubs)[0] if hubs else "hub"
    inwin = lambda e: since <= (e.get("ts") or 0) < until  # noqa: E731

    def target(d):
        to = d.get("to")
        return hub if not to or to in HUB_WORDS or to in hubs else to

    # session turns: (start, label); session results — by time
    turns, results = collections.defaultdict(list), collections.defaultdict(list)
    marker_start = {}
    for e in events:
        who = e.get("entity")
        if e["type"] == "turn.started" and who:
            m = (e.get("data") or {}).get("marker")
            turns[who].append((e["ts"], m))
            if m and (who, m) not in marker_start:
                marker_start[(who, m)] = e["ts"]
        elif e["type"] == "turn.result" and who and is_reply(e):
            results[who].append(e)

    turn_ts = {who: [t for t, _ in lst] for who, lst in turns.items()}
    result_ts = {who: [r["ts"] for r in lst] for who, lst in results.items()}

    def turn_of(who, ts):
        k = bisect.bisect_right(turn_ts.get(who) or [], ts)
        return turns[who][k - 1] if k else (None, None)

    def results_after(who, ts):
        lst = results.get(who) or []
        return itertools.islice(lst, bisect.bisect_right(result_ts.get(who) or [], ts), None)

    owner = [e for e in events if e["type"] == "message.received" and inwin(e)
             and (e.get("data") or {}).get("owner") and (e.get("data") or {}).get("channel") not in ("session", None)]
    owner_ids = {f"m{e['seq']}": e for e in events if e["type"] == "message.received"
                 and (e.get("data") or {}).get("owner")}

    # --- owner messages ----------------------------------------------------------------------------------------------
    by_channel = {}
    for e in owner:
        d = e["data"]
        c = by_channel.setdefault(d["channel"], {"messages": 0, "voice": 0, "to_hub": 0, "to_session": 0})
        c["messages"] += 1
        c["voice"] += (d.get("text") or "").startswith("🎤")
        c["to_hub" if target(d) == hub else "to_session"] += 1

    # --- first reply -------------------------------------------------------------------------------------------------
    lat, lat_ch, pickup, no_reply, superseded = [], collections.defaultdict(list), [], [], 0
    owner_to = collections.defaultdict(list)
    for e in owner:
        owner_to[target(e["data"])].append(e)
    for e in owner:
        d, mid, who = e["data"], f"m{e['seq']}", target(e["data"])
        start = marker_start.get((who, mid))
        if start is not None:
            pickup.append(start - e["ts"])
        reply = None
        for r in results_after(who, e["ts"]):
            if r["ts"] - e["ts"] > REPLY_CAP:
                break
            t0, m = turn_of(who, r["ts"])
            if m == mid or (start is None and t0 is not None and t0 <= e["ts"]):
                reply = r
                break
        if reply:
            lat.append(reply["ts"] - e["ts"])
            lat_ch[d["channel"]].append(reply["ts"] - e["ts"])
        else:
            no_reply.append(e["seq"])
            nxt = next((x for x in owner_to[who] if x["ts"] > e["ts"] or (x["ts"] == e["ts"] and x["seq"] > e["seq"])),
                       None)
            first = next(results_after(who, e["ts"]), None)
            if nxt and nxt["ts"] - e["ts"] <= REPLY_CAP and (first is None or first["ts"] >= nxt["ts"]):
                superseded += 1
    totals = collections.Counter(e["data"]["channel"] for e in owner)

    # --- delegations -------------------------------------------------------------------------------------------------
    done = {e.get("entity"): e for e in events if e["type"] == "delegation.done"}
    deleg_reply, deleg_n, created_done, deleg_open = [], 0, [], 0
    for e in events:
        if e["type"] != "delegation.created" or not inwin(e):
            continue
        d, did = e.get("data") or {}, f"d{e['seq']}"
        fin = done.get(did)
        if fin:
            created_done.append(fin["ts"] - e["ts"])
        else:
            deleg_open += 1
        o = owner_ids.get(d.get("for") or "")
        if not o:
            continue
        deleg_n += 1
        r = next(results_after(d.get("session"), e["ts"]), None)
        if r and r["ts"] - o["ts"] <= REPLY_CAP:
            deleg_reply.append(r["ts"] - o["ts"])

    # --- Telegram: by message numbers --------------------------------------------------------------------------------
    tg = [e for e in owner if e["data"]["channel"] == "telegram"]
    chats = collections.defaultdict(list)
    for e in tg:
        t = e["data"].get("tg") or {}
        if isinstance(t.get("id"), int):
            chats[t.get("chat")].append(t["id"])
    gaps = []
    for ids in chats.values():
        ids.sort()
        gaps += [b - a - 1 for a, b in zip(ids, ids[1:])]
    by_ids = {"bot_messages": sum(gaps), "gaps": len(gaps),
              "per_owner_message": round(sum(gaps) / len(gaps), 2) if gaps else None,
              "median_gap": float(statistics.median(gaps)) if gaps else None, "max_gap": max(gaps) if gaps else None}

    # --- Telegram: by events the bot turns into a send --------------------------------------------------------------
    tg_marks = {f"m{e['seq']}" for e in events if e["type"] == "message.received"
                and (e.get("data") or {}).get("channel") == "telegram" and (e.get("data") or {}).get("tg")}
    tg_marks |= {f"m{e['seq']}" for e in events if e["type"] == "permission.answered"
                 and (e.get("data") or {}).get("channel") == "telegram"}
    sends = collections.Counter()
    open_deleg = {}   # executor → delegation for a Telegram message, not done yet
    cards = {}        # (session, request_id) → card
    for e in events:
        t, d, who, w = e["type"], e.get("data") or {}, e.get("entity"), inwin(e)
        if t == "delegation.created" and d.get("for") in tg_marks:
            open_deleg[d.get("session")] = f"d{e['seq']}"
        elif t == "delegation.done":
            open_deleg = {k: v for k, v in open_deleg.items() if v != who}
        elif t == "turn.result" and who and is_reply(e):
            if who in hubs or who == hub:
                sends["hub_result"] += w
            elif who in open_deleg or turn_of(who, e["ts"])[1] in tg_marks:
                sends["session_result"] += w
        elif t == "permission.requested" and who:
            qs = len(((d.get("input") or {}).get("questions") or [])) if d.get("tool") == "AskUserQuestion" else 1
            cards[(who, d.get("request_id"))] = {"seq": e["seq"], "ts": e["ts"], "session": who, "msgs": max(qs, 1),
                                                 "tool": d.get("tool"), "win": w, "end": None, "how": None}
            sends["card"] += max(qs, 1) * w
        elif t in ("permission.answered", "question.dismissed", "question.withdrawn") and who:
            if t == "question.withdrawn":
                c = next((c for (s, _), c in cards.items() if s == who and f"q{c['seq']}" == d.get("id")), None)
            else:
                c = cards.get((who, d.get("request_id")))
            if c and c["end"] is None:
                c["end"], c["how"] = e["ts"], (t, d.get("channel") or e.get("src"))
                sends["card_closed"] += c["msgs"] * w
        elif t == "owner.notify":
            sends["notify"] += (1 + bool(d.get("file"))) * w
        elif t == "owner.file":
            sends["file"] += w
        elif t == "timer.fired" and str(who or "").startswith("q:"):
            sends["reminder"] += w
    n_sends = sum(sends.values())
    by_events = {"sends": n_sends, "per_owner_message": round(n_sends / len(tg), 2) if tg else None,
                 "by_kind": {k: v for k, v in sends.items() if v}}

    # --- cards -------------------------------------------------------------------------------------------------------
    win_cards = [c for c in cards.values() if c["win"]]
    ans = collections.Counter(c["how"][1] for c in win_cards if c["how"] and c["how"][0] == "permission.answered")
    ans_lat = [c["end"] - c["ts"] for c in win_cards if c["how"] and c["how"][0] == "permission.answered"]
    top = collections.Counter(c["session"] for c in win_cards)
    left = collections.Counter(c["session"] for c in win_cards if not c["how"])
    rank = lambda cnt: [[k, v] for k, v in sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))[:5]]  # noqa: E731

    fu = [e for e in owner if is_followup(e["data"].get("text"))]
    return {
        "window": {"since": since, "until": until, "hub": hub},
        "owner": {"total": len(owner), "by_channel": by_channel},
        "telegram": {"owner_messages": len(tg), "by_ids": by_ids, "by_events": by_events},
        "reply": {"all": answered(lat, len(owner)),
                  "by_channel": {c: answered(lat_ch[c], n) for c, n in sorted(totals.items())},
                  "pickup": dist(pickup), "superseded": superseded, "unanswered_seqs": no_reply[:20]},
        "delegation": {"for_owner_messages": deleg_n, "executor_reply": answered(deleg_reply, deleg_n),
                       "created_to_done": dist(created_done), "open": deleg_open},
        "followups": {"total": len(fu), "share": round(len(fu) / len(owner), 3) if owner else None,
                      "by_channel": dict(collections.Counter(e["data"]["channel"] for e in fu)),
                      "examples": [{"seq": e["seq"], "text": strip_voice(e["data"].get("text"))[:80]} for e in fu[:20]]},
        "cards": {"requested": len(win_cards),
                  "by_tool": dict(collections.Counter("AskUserQuestion" if c["tool"] == "AskUserQuestion" else "other"
                                                      for c in win_cards)),
                  "answered": dict(ans),
                  "dismissed": sum(1 for c in win_cards if c["how"] and c["how"][0] == "question.dismissed"),
                  "withdrawn": sum(1 for c in win_cards if c["how"] and c["how"][0] == "question.withdrawn"),
                  "unanswered": sum(left.values()), "answer_latency": dist(ans_lat),
                  "top_sessions": rank(top), "top_unanswered": rank(left)},
    }


def fmt_s(x):
    if x is None:
        return "—"
    return f"{x:.0f} s" if x < 120 else (f"{x / 60:.1f} min" if x < 7200 else f"{x / 3600:.1f} h")


def pct(a, b):
    return f"{100 * a / b:.0f}%" if b else "—"


def report(s):
    w, o, tg, r, dl, fu, c = (s[k] for k in ("window", "owner", "telegram", "reply", "delegation", "followups", "cards"))
    day = lambda ts: datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")  # noqa: E731
    lat = lambda d: f"median {fmt_s(d['median_s'])}, p90 {fmt_s(d['p90_s'])}"  # noqa: E731
    out = [f"Window {day(w['since'])} — {day(w['until'])}, hub {w['hub']}",
           f"Owner messages: {o['total']} — " + ", ".join(
               f"{ch} {x['messages']} (voice {x['voice']}, {pct(x['voice'], x['messages'])}; to the hub {x['to_hub']}, "
               f"to sessions {x['to_session']})" for ch, x in sorted(o["by_channel"].items())),
           f"Bot messages in Telegram per owner message ({tg['owner_messages']} total): by ids "
           f"{tg['by_ids']['per_owner_message']} (all {tg['by_ids']['bot_messages']}, median "
           f"{tg['by_ids']['median_gap']}, max {tg['by_ids']['max_gap']}); by events "
           f"{tg['by_events']['per_owner_message']} (all {tg['by_events']['sends']}: "
           + ", ".join(f"{k} {v}" for k, v in sorted(tg["by_events"]["by_kind"].items())) + ")",
           f"First reply: {lat(r['all'])}; unanswered {r['all']['unanswered']} of {r['all']['n']} ({r['superseded']} of "
           f"them: his next message to the same place came before any reply)"]
    out += [f"  {ch}: {lat(x)}; unanswered {x['unanswered']} of {x['n']}" for ch, x in r["by_channel"].items()]
    out += [f"  picked up: {lat(r['pickup'])}",
            f"Delegations on his messages: {dl['for_owner_messages']}; the executor's first result: "
            f"{lat(dl['executor_reply'])}, no result {dl['executor_reply']['unanswered']}",
            f"  delegation → done (all delegations of the window): {lat(dl['created_to_done'])} (n={dl['created_to_done']['n']}); "
            f"not done {dl['open']}",
            f"Repeated asks: {fu['total']} ({pct(fu['total'], o['total'])}) — "
            + ", ".join(f"{k} {v}" for k, v in sorted(fu["by_channel"].items())),
            f"Question cards: {c['requested']} (" + ", ".join(f"{k} {v}" for k, v in sorted(c["by_tool"].items()))
            + f"); answered {sum(c['answered'].values())} ("
            + ", ".join(f"{k} {v}" for k, v in sorted(c["answered"].items())) + f"), dismissed {c['dismissed']}, "
            f"withdrawn {c['withdrawn']}, unanswered {c['unanswered']}; answer within {lat(c['answer_latency'])}",
            "  asked the most: " + ", ".join(f"{k} {v}" for k, v in c["top_sessions"]),
            "  unanswered: " + (", ".join(f"{k} {v}" for k, v in c["top_unanswered"]) or "—")]
    return "\n".join(out)


def parse_when(s, end=False):
    if s.startswith("@"):
        return float(s[1:])
    d = datetime.datetime.strptime(s, "%Y-%m-%d")
    return (d + datetime.timedelta(days=1 if end else 0)).timestamp()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("journals", nargs="*", help="JSONL journals (default: the hub's journal.jsonl)")
    p.add_argument("--since", help="from the date YYYY-MM-DD (or @unix)")
    p.add_argument("--until", help="up to the date YYYY-MM-DD inclusive (or @unix)")
    p.add_argument("--hub", action="append", default=[], help="name of the hub session (usually found in the journal)")
    p.add_argument("--json", action="store_true", help="the numbers as JSON")
    a = p.parse_args(argv)
    until = parse_when(a.until, end=True) if a.until else time.time()
    since = parse_when(a.since) if a.since else until - 7 * 86400
    paths = []
    for j in a.journals or []:
        paths += sorted(glob.glob(j)) or [j]
    if not paths:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from core.daemon import default_state_dir
        paths = [os.path.join(default_state_dir(), "journal.jsonl")]
    s = collect(paths, since, until, a.hub)
    print(json.dumps(s, ensure_ascii=False, indent=1) if a.json else report(s))


if __name__ == "__main__":
    main()
