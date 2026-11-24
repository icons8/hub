"""`hub contacts deputy` and `hub contacts deputies` (spec d2679763, part A): the owner names a person of the book his
deputy for an area, in his words. The command finds the record (it gets an id, as for `hub contacts link`), the
person's channel — their hub on the bus, or the session `--via` names — and puts the fact; the core checks whose word
it is and that the area is quoted from it (core/deputies.py). The line it prints is what the hub shows the owner.

  hub contacts deputy "<person>" "<area>" [--not "<what they do not decide>"] [--via <hub|session>] [--all]
      [--for mN|qN]   (--all: they decide everything in the area, acting for him)
  hub contacts deputy "<person>" --off [--for mN|qN]
  hub contacts deputies
"""
import datetime

from core import deputies, deputyroute, i18n
from core.coretext import human


def _opt(words, flag):
    if flag not in words:
        return None, words
    i = words.index(flag)
    if i + 1 >= len(words):
        raise SystemExit(i18n.t("cli.hub.needs_value", flag=flag))
    return words[i + 1], words[:i] + words[i + 2:]


def _record(who):
    """(person {id, name}, names, addresses) of the only record with this name or alias; the record gets an id of its
    own under the book's lock if it has none (or shares one with a copy)."""
    from modules.contacts import contacts as book

    def change(rows):
        row = book._one(rows, who)
        if not row.get("id") or sum(1 for x in rows if isinstance(x, dict) and x.get("id") == row["id"]) > 1:
            row["id"] = book.new_id(rows)
        al = row.get("aliases") if isinstance(row.get("aliases"), list) else []
        return ({"id": row["id"], "name": row["name"]}, [a for a in al if isinstance(a, str)],
                {k: row.get(k) for k in deputies.ADDRESS_KEYS if row.get(k)})
    return book.update(book.default_path(), change)


def _channel(st, person, via):
    """The channel to put in the fact: the person's one usable hub, the one --via names, or a session --via names
    (its address — the record's addresses now); None — the core says what is missing."""
    hubs = {n: c for n, c in ((st.get("bus") or {}).get("contacts") or {}).items()
            if (c.get("person") or {}).get("id") == person["id"]}
    if via and via in hubs:
        return {"kind": "bus", "contact": via}
    if via:
        return {"kind": "via", "session": via}
    fit = [n for n, c in hubs.items() if c.get("status") == "known" and c.get("verified") and not c.get("quarantine")]
    if len(fit) == 1:
        return {"kind": "bus", "contact": fit[0]}
    if len(fit) > 1:
        raise SystemExit(i18n.t("cli.deputy.two_hubs", name=person["name"], hubs=", ".join(sorted(fit))))
    return None


def line(st, rec):
    """«Anna — deputy: «area»; does not decide: «…»; channel: hub anna-hub · since 20.11, m123» (+ why it is off)."""
    ch = rec.get("channel") or {}
    channel = i18n.t("cli.deputy.channel_hub", hub=ch.get("contact")) if ch.get("kind") == "bus" \
        else i18n.t("cli.deputy.channel_via", session=ch.get("session"))
    since = datetime.datetime.fromtimestamp(rec["since"]).strftime("%d.%m")
    by = i18n.t("cli.deputy.by_owner") if rec.get("by") == "owner" else rec.get("by")
    out = i18n.t("cli.deputy.line", name=rec["person"]["name"], zone=rec["zone"], channel=channel, since=since, by=by)
    if rec.get("not"):
        out = i18n.t("cli.deputy.line_not", line=out, nope=rec["not"])
    if rec.get("all"):
        out = i18n.t("cli.deputy.line_all", line=out)
    why = deputies.why_text(st, rec)
    return i18n.t("cli.deputy.inactive", line=out, why=human(why)) if why else out


def main(argv, put, current, rejected):
    if argv[:1] == ["deputies"] and len(argv) == 1:
        st = current()
        recs = [r for r in (st.get("deputies") or {}).values() if r.get("on")]
        print("\n".join(line(st, r) for r in recs) if recs else i18n.t("cli.deputy.none"))
        return 0
    words = list(argv[1:])
    ref, words = _opt(words, "--for")
    nope, words = _opt(words, "--not")
    via, words = _opt(words, "--via")
    off, every = "--off" in words, "--all" in words
    words = [w for w in words if w not in ("--off", "--all")]
    if not words or (off and (len(words) != 1 or nope or via or every)) or (not off and len(words) != 2):
        raise SystemExit(i18n.t("cli.deputy.usage"))
    try:
        person, aliases, address = _record(words[0])
    except ValueError as e:
        print(e)
        return 1
    data = {"person": person, "names": aliases, **({"for": ref} if ref else {})}
    if off:
        seq = put("contact.deputy_off", None, data)
    else:
        st = current()
        was = (st.get("deputies") or {}).get(person["id"]) or {}
        had = bool(was.get("on") and was.get("all"))   # set again without --all: the mark goes, say so
        channel = _channel(st, person, via)
        if channel and channel["kind"] == "via":
            channel["address"] = address
        seq = put("contact.deputy_set", None, {**data, "zone": words[1], **({"not": nope} if nope else {}),
                                                **({"all": True} if every else {}), "path": channel})
    why = rejected.get(seq)
    if why == "unknown event type":   # a hubd from before this command: the same command after a restart
        print(i18n.t("cli.deputy.old_core"))
        return 1
    if seq is None or why:
        return 1
    st = current()
    rec = (st.get("deputies") or {}).get(person["id"])
    print(i18n.t("cli.deputy.removed", name=person["name"]) if off or not rec else line(st, rec))
    if not off and rec and had and not rec.get("all"):
        print(i18n.t("cli.deputy.all_off", name=person["name"]))
    return 0


def away_lines(st, status=False, lang=None):
    """What this away hands the deputies: who and their areas, the names on the flag no deputy fits, and — for
    --status — how many cards are at the deputies now; last, that it is held until /back or the term. lang — the
    language of a page request (else the process's)."""
    t = (lambda key, **kw: i18n.tr(lang, key, **kw)) if lang else i18n.t
    o = st.get("owner") or {}
    flag = o.get("deputies")
    if not flag:
        return []
    inc = deputies.included(st)
    out = [t("cli.away.deputies", who="; ".join(t("cli.away.deputy_all" if r.get("all") else "cli.away.deputy",
                                                   name=r["person"]["name"], zone=r["zone"]) for r in inc.values()))
           if inc else t("cli.away.deputies_none")]
    if flag.get("unknown"):
        out.append(t("cli.away.deputies_unknown", names=", ".join(flag["unknown"])))
    if status:
        out.append(t("cli.away.at_deputies", count=len(deputyroute.at_deputies(st))))
    if deputies.held(st):   # his words from the page or Telegram do not end this away (reducer._received)
        out.append(t("cli.away.deputies_held" if o.get("until") else "cli.away.deputies_held_open"))
    return out


def route(asker, qid, words, note, put, current, rejected):
    """`hub question route qN <person> [--note "…"] [--own]` | `hub question route qN --owner`: the fact, and for a
    deputy whose channel is a session — their addresses in the book now (the core compares them with the ones he named).
    --own — the card is about the deputy's own request (only one who decides everything in their area takes it)."""
    own = "--own" in words
    words = [w for w in words if w != "--own"]
    back = words == ["--owner"]
    if (not back and (len(words) != 1 or words[0].startswith("--"))) or (back and own):
        raise SystemExit(i18n.t("cli.question.route_usage"))
    data = {"id": qid, **({"back": True} if back else {"person": words[0]}), **({"note": note} if note else {}),
            **({"own": True} if own else {})}
    if not back:
        st = current()
        ids, _ = deputies._named(st, words)
        data.update(address_of(st, ids[0] if ids else None))
    seq = put("question.routed", asker, data)
    why = rejected.get(seq)
    if why == "unknown event type":
        print(i18n.t("cli.deputy.old_core"))
        return 1
    if seq is None or why:
        return 1
    ask = next((a for a in (current().get("sessions") or {}).get(asker, {}).get("asks") or [] if a["id"] == qid), {})
    dep = ask.get("deputy") or {}
    if back:
        print(i18n.t("cli.question.route_back", qid=qid))
    elif dep.get("kind") == "bus":
        print(i18n.t("cli.question.routed_hub", qid=qid, name=dep.get("name"), hub=dep.get("contact")))
    else:
        print(i18n.t("cli.question.routed_via", qid=qid, name=dep.get("name"), session=dep.get("session"),
                     route=dep.get("route")))
    return 0


def address_of(st, pid):
    """{"address": …} — the person's addresses in the book now, for a deputy whose channel is a session (the core
    compares them with the ones the owner named); {} for any other."""
    rec = (st.get("deputies") or {}).get(pid or "") or {}
    if (rec.get("channel") or {}).get("kind") != "via":
        return {}
    from modules.contacts import contacts as book
    row = next((r for r in book.people(book.default_path()) if r.get("id") == rec["person"]["id"]), {})
    return {"address": {k: row.get(k) for k in deputies.ADDRESS_KEYS if row.get(k)}}


def card_address(st, qid):
    """address_of for the deputy a card is at."""
    ask = next((a for s in (st.get("sessions") or {}).values() for a in s.get("asks") or [] if a["id"] == qid), {})
    return address_of(st, (ask.get("deputy") or {}).get("pid"))
