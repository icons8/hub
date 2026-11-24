"""The owner's deputies (spec d2679763, part A): a person of the contacts book who decides questions of an area the owner
wrote for them, while he is away with the flag (`hub away --deputies`). The book is an address, not a permission — any
session writes `contacts.json` — so the deputy lives here, a fact of the core (`contact.deputy_set`, `_off`), in
`state["deputies"]` by the record's id: the owner's area verbatim, what they do not decide, the channel the core asks
them by, and the address of that channel at the time he named them.

Who sets it: the owner himself (his terminal, the page, Telegram — strict guards.from_owner_strict), or the hub on his
word (`--for mN|qN`, tools.owner_word): the core checks where the words come from and that the area is his text
verbatim — his message, or his answer to the card — and that the person is named in it. What the words mean (not a
"no", not a question) it does not read: the hub does, and the owner sees what was recorded at once.

The channel is a hub on the bus — known, the pair code checked, bound to this very person — or a session of this hub
that holds the person's channel (Teams: the session `--via` names). A deputy is active only while that channel is the
same as when he named them: another fingerprint, an unlinked or quarantined hub, another address in the book — and it is
not, until he names them again. A deputy's answer is never his word (core/deputyroute.py).

"all" — the deputy decides everything in their area (owner 22.11: "Deputies may do everything"; "they act for me, I am
not there in fact"): a card of it goes to them with the gates the core keeps for him otherwise. Set by the same word as
the record (the hub reads that his words say so, as it reads the area); set again without it, or --off, and it is gone."""
import re
import unicodedata

from core import guards, tools
from core.minihub import GONE   # a session that will not take a message
from core.coretext import T

ZONE_MAX = 500   # characters of the area and of "does not decide": the owner's words, in every question to the deputy
KEEP_REFS = 50   # the words that set or removed a deputy, remembered: the same mN does not set a removed one again

DEPUTY_WHO = T("core.deputies.who", "a deputy is set by the owner: in their terminal, or by the hub on their word — "
                                    "--for mN (their message to you) or qN (their answer to a card)")
DEPUTY_NO_PERSON = T("core.deputies.no_person", "a deputy is a person of the contacts book: its record id (c + 8 hex) "
                                                "and name")
DEPUTY_ZONE = T("core.deputies.zone", "the area is the owner's text, 1 to {most} characters", most=ZONE_MAX)
DEPUTY_NOT = T("core.deputies.not", "\"does not decide\" is the owner's text, up to {most} characters", most=ZONE_MAX)
DEPUTY_NO_CHANNEL = T("core.deputies.no_channel",
                      "{name} has no channel: no hub on the bus that is known, checked and linked to them, and no "
                      "session of their channel — link their hub (hub contacts link) or name the session that holds "
                      "their channel: --via <session>")
DEPUTY_CHANNEL_GONE = T("core.deputies.channel_gone", "the channel of {name} is not usable: {why}")
DEPUTY_NOT_NAMED = T("core.deputies.not_named", "--for {ref}: the owner's words do not name {name}")
DEPUTY_NOT_QUOTED = T("core.deputies.not_quoted",
                      "--for {ref}: «{text}» is not the owner's words — the area and \"does not decide\" go verbatim, "
                      "as a quote of their message or their answer to the card")
DEPUTY_NO_ANSWERS = T("core.deputies.no_answers", "--for {ref}: this answer to the card has no answers to quote")
DEPUTY_USED = T("core.deputies.used", "--for {ref}: these words already set {name}, who was removed since — set them "
                                      "again only on new words of the owner")
DEPUTY_NONE = T("core.deputies.none", "{name} is not a deputy")
DEPUTY_ALL_WORDS = T("core.deputies.all_words", "--for {ref}: these words already named {name} a deputy who does not "
                                                "decide everything — the mark (--all) takes new words of the owner")
WHY_GONE = {"no_contact": T("core.deputies.why_no_contact", "the hub {hub} is no longer among the bus contacts"),
            "fp": T("core.deputies.why_fp", "the hub {hub} has another key than when the owner named them"),
            "status": T("core.deputies.why_status", "the hub {hub} is not known (status {status})"),
            "verified": T("core.deputies.why_verified", "the pair code of the hub {hub} is not checked"),
            "quarantine": T("core.deputies.why_quarantine", "the hub {hub} is in quarantine"),
            "person": T("core.deputies.why_person", "the hub {hub} is linked to another record of the book now"),
            "session": T("core.deputies.why_session", "the session {hub} of their channel is gone"),
            "address": T("core.deputies.why_address", "their address in the book changed since the owner named them"),
            "off": T("core.deputies.why_off", "removed")}

PID = re.compile(r"c[0-9a-f]{8}")
ADDRESS_KEYS = ("telegram", "teams_chat", "teams", "email")


def _text_ok(text, most, empty=False):
    if text is None:
        return empty
    if not isinstance(text, str) or len(text.strip()) > most or not (text.strip() or empty):
        return False
    return not any(unicodedata.category(c) in ("Cc", "Cf") and c not in "\n\r\t" for c in text)


def norm(text):
    """The form two texts compare in: one space, no case."""
    return " ".join(str(text or "").split()).casefold()


def _stems(names):
    """(start, length, slack) of the words naming the person: a name is declined in many languages (a Greek Γιώργος —
    Γιώργο), so a word of 4+ letters is matched by its start (all but its last one or two letters) and a length within
    two of it; a 3-letter one outside Latin — by its first two letters and the same length; a short Latin one (Al, Ann)
    — only whole."""
    out = set()
    for n in names:
        for w in re.findall(r"\w+", str(n).casefold()):
            if len(w) > 3:   # all but the last letter: Marina is not Maria
                out.add((w[:len(w) - 1], len(w), 2))
            elif len(w) == 3 and not w.isascii():
                out.add((w[:2], 3, 0))
            else:
                out.add((w, len(w), 0))
    return out


def names_in(text, names):
    """The text names the person: a word of it is their name or alias, maybe declined."""
    words = re.findall(r"\w+", str(text or "").casefold())
    return any(w.startswith(s) and abs(len(w) - n) <= slack for w in words for s, n, slack in _stems(names))


def channel_why(st, rec, address=None):
    """Why the deputy's channel is not the one the owner named (WHY_GONE key, with what it is about), or None. address —
    the person's addresses in the book now (the CLI reads them): for a channel through a session, another one is not
    the address he named."""
    ch = rec.get("channel") or {}
    if not rec.get("on"):
        return "off", {}
    if ch.get("kind") == "bus":
        c = ((st.get("bus") or {}).get("contacts") or {}).get(ch.get("contact") or "")
        hub = {"hub": ch.get("contact")}
        if c is None:
            return "no_contact", hub
        if c.get("fp") != ch.get("fp"):
            return "fp", hub
        if c.get("status") != "known":
            return "status", {**hub, "status": c.get("status")}
        if not c.get("verified"):
            return "verified", hub
        if c.get("quarantine"):
            return "quarantine", hub
        if (c.get("person") or {}).get("id") != rec["person"]["id"]:
            return "person", hub
        return None
    sess = (st.get("sessions") or {}).get(ch.get("session") or "")
    if not sess or sess.get("state") in GONE:
        return "session", {"hub": ch.get("session")}
    if address is not None and _address(address) != ch.get("address"):
        return "address", {}
    return None


def why_text(st, rec, address=None):
    got = channel_why(st, rec, address)
    if got is None:
        return None
    code, args = got
    return WHY_GONE[code].format(**args)


def _address(given):
    """The addresses of a record a channel through a session is bound to, in one comparable form."""
    if not isinstance(given, dict):
        return {}
    out = {}
    for k in ADDRESS_KEYS:
        v = given.get(k)
        if k == "telegram" and isinstance(v, dict):
            v = v.get("id")
        if v not in (None, ""):
            out[k] = str(v) if k in ("telegram", "teams_chat") else str(v).casefold()
    return out


def _channel(st, data, person):
    """The channel the request names, checked against the state: {"kind": "bus", "contact", "fp"} or {"kind": "via",
    "session", "address"}; Rejected with the reason."""
    ch = data.get("path")   # not "channel": that one names who speaks
    if not isinstance(ch, dict):
        raise tools.Rejected(DEPUTY_NO_CHANNEL.format(name=person["name"]))
    if ch.get("kind") == "bus":
        c = ((st.get("bus") or {}).get("contacts") or {}).get(str(ch.get("contact") or ""))
        rec = {"on": True, "person": person, "channel": {"kind": "bus", "contact": ch.get("contact"),
                                                          "fp": (c or {}).get("fp")}}
        why = why_text(st, rec)
        if why:
            raise tools.Rejected(DEPUTY_CHANNEL_GONE.format(name=person["name"], why=why))
        return rec["channel"]
    if ch.get("kind") == "via":
        address = _address(ch.get("address"))
        rec = {"on": True, "person": person, "channel": {"kind": "via", "session": ch.get("session"),
                                                          "address": address}}
        if not address:
            raise tools.Rejected(DEPUTY_NO_CHANNEL.format(name=person["name"]))
        why = why_text(st, rec)
        if why:
            raise tools.Rejected(DEPUTY_CHANNEL_GONE.format(name=person["name"], why=why))
        return rec["channel"]
    raise tools.Rejected(DEPUTY_NO_CHANNEL.format(name=person["name"]))


def _person(data):
    p = data.get("person")
    if not (isinstance(p, dict) and isinstance(p.get("id"), str) and PID.fullmatch(p["id"])
            and isinstance(p.get("name"), str) and 0 < len(p["name"]) <= 100):
        raise tools.Rejected(DEPUTY_NO_PERSON)
    names = [n for n in data.get("names") or [] if isinstance(n, str) and n.strip()] \
        if isinstance(data.get("names"), list) else []
    return {"id": p["id"], "name": p["name"]}, [p["name"], *names]


def _words(st, ev, data, names, quoted):
    """Whose word sets it: "owner" (his own channel), or the mN/qN of his words the hub cites — checked: his, fresh,
    naming the person, with the quoted texts in it verbatim. Rejected otherwise."""
    if guards.from_owner_strict(st, data) is None:
        return "owner", None
    sender = (st.get("sessions") or {}).get(data.get("sender") or "") or {}
    if data.get("channel") != "session" or sender.get("kind") != "hub" or not data.get("for"):
        raise tools.Rejected(DEPUTY_WHO)
    word = tools.owner_word(st, data, ev["ts"], cards=True)
    msg = st["messages"].get(word["message"]) or {}
    ref = str(data["for"])
    if not names_in(msg.get("text"), names):
        raise tools.Rejected(DEPUTY_NOT_NAMED.format(ref=ref, name=names[0]))
    if ref.startswith("q"):
        answers = msg.get("answers")
        if not isinstance(answers, dict) or not answers:
            raise tools.Rejected(DEPUTY_NO_ANSWERS.format(ref=ref))
        # a whole answer of his (the option he chose, without its "(Recommended)" mark, or his own text), not a piece
        # of one: "production" is not quoted from "everything except production"
        theirs = {norm(v) for v in answers.values()} | {norm(re.sub(r"\s*\([^()]*\)\s*$", "", str(v)))
                                                       for v in answers.values()}
        for text in quoted:
            if norm(text) not in theirs:
                raise tools.Rejected(DEPUTY_NOT_QUOTED.format(ref=ref, text=text[:80]))
    else:
        said = norm(msg.get("text"))
        for text in quoted:
            if norm(text) not in said:
                raise tools.Rejected(DEPUTY_NOT_QUOTED.format(ref=ref, text=text[:80]))
    return ref, msg["id"]


def fold(st, ev, data):
    """contact.deputy_set / contact.deputy_off: the owner's word on a deputy. Returns the record; Rejected otherwise."""
    person, names = _person(data)
    deps = st.setdefault("deputies", {})
    rec = deps.get(person["id"])
    if ev["type"] == "contact.deputy_off":
        if not rec or not rec.get("on"):
            raise tools.Rejected(DEPUTY_NONE.format(name=person["name"]))
        ref, _ = _words(st, ev, data, names, [])
        rec.update(on=False, off={"ts": ev["ts"], "by": ref, "seq": ev["seq"]})
        # the words that set this appointment never set it again, whatever comes later (his words live a day)
        # (each of them: an earlier re-set's words too)
        spent = [r for r in (*(rec.get("refs") or []), rec.get("by"), ref) if r and r != "owner"]
        rec["gone"] = ([r for r in rec.get("gone") or [] if r not in spent] + list(dict.fromkeys(spent)))[-KEEP_REFS:]
        return rec
    zone, nope = data.get("zone"), data.get("not")
    if not _text_ok(zone, ZONE_MAX):
        raise tools.Rejected(DEPUTY_ZONE)
    if not _text_ok(nope, ZONE_MAX, empty=True):
        raise tools.Rejected(DEPUTY_NOT)
    zone, nope = " ".join(zone.split()), " ".join(nope.split()) if nope and nope.strip() else None
    ref, word = _words(st, ev, data, names, [zone] + ([nope] if nope else []))
    if rec and ref != "owner" and ref in (rec.get("gone") or []):
        raise tools.Rejected(DEPUTY_USED.format(ref=ref, name=person["name"]))
    # the same words do not set the mark afterwards: what named someone a plain deputy did not say "everything" (his
    # words live a day: KEEP_REFS of them is more than a day of re-sets)
    every = data.get("all") is True
    # a record from before the mark has no "plain": every word that set it set a plain deputy — except the answer to a
    # card that the record itself was made from, whole, as its area: an older core took the mark with the same command
    # and dropped it from the record, so those words are the ones that asked for it
    legacy = bool(rec) and "plain" not in rec
    asked = legacy and ref.startswith("q") and rec.get("by") == ref and rec.get("zone") == zone \
        and rec.get("not") == nope
    plain = ((rec.get("plain") if not legacy else [r for r in rec.get("refs") or [] if not (asked and r == ref)])
             if rec else None) or []
    if every and ref in plain:
        raise tools.Rejected(DEPUTY_ALL_WORDS.format(ref=ref, name=person["name"]))
    channel = _channel(st, data, person)
    refs = ((rec or {}).get("refs") or []) + ([ref] if ref != "owner" else [])
    plain = plain + ([ref] if ref != "owner" and not every else [])
    deps[person["id"]] = {"person": person, "names": [n for n in names[1:] if n != person["name"]][:20],
                          "zone": zone, **({"not": nope} if nope else {}), **({"all": True} if every else {}),
                          "channel": channel,
                          "since": ev["ts"], "seq": ev["seq"], "by": ref, **({"word": word} if word else {}),
                          "on": True, "refs": refs[-KEEP_REFS:], "plain": plain[-KEEP_REFS:],
                          **({"gone": rec["gone"]} if rec and rec.get("gone") else {})}
    return deps[person["id"]]


def active(st):
    """{person id: record} of the deputies on, whose channel is the one the owner named (the bus part — from the state;
    the book's address of a channel through a session is checked by the command that routes)."""
    return {pid: r for pid, r in (st.get("deputies") or {}).items() if channel_why(st, r) is None}


def channel_label(rec):
    ch = rec.get("channel") or {}
    return f"bus:{ch.get('contact')}" if ch.get("kind") == "bus" else f"via:{ch.get('session')}"


# --- away with the flag (spec d2679763, A5): `hub away --deputies [<person> …]`, `/away deputies` ------------------

AWAY_FLAG_WORD = T("core.deputies.away_word", "--deputies: the hub turns it on only on the owner's word — --for mN "
                                              "(their message to you)")


def _named(st, words):
    """The deputies on the owner's list of names (a word naming one, as names_in): (ids, names nobody fits)."""
    deps = st.get("deputies") or {}
    ids, unknown = [], []
    for w in words:
        fit = [pid for pid, r in deps.items() if r.get("on") and norm(w) in {norm(n) for n in [r["person"]["name"],
                                                                                               *(r.get("names") or [])]}]
        fit = fit or [pid for pid, r in deps.items() if r.get("on") and names_in(w, [r["person"]["name"],
                                                                                    *(r.get("names") or [])])]
        if len(fit) == 1:
            ids.append(fit[0])
        else:
            unknown.append(str(w)[:100])
    return list(dict.fromkeys(ids)), unknown


def away_flag(st, ev, data, extend):
    """The flag of this away (owner["deputies"]): {"all": True} or {"ids": [...]}, plus the names that fit no deputy;
    a repeat while away adds to the period's flag. Checked before the owner's state changes: the hub turns it on only
    on his word. None — no flag in this request (an extend keeps the period's)."""
    flag = data.get("deputies")
    if flag in (None, False):
        return None
    if guards.from_owner_strict(st, data) is not None:   # not a module's request in his name: his own channel
        sender = (st.get("sessions") or {}).get(data.get("sender") or "") or {}
        if data.get("channel") != "session" or sender.get("kind") != "hub" or not data.get("for"):
            raise tools.Rejected(AWAY_FLAG_WORD)
        tools.owner_word(st, data, ev["ts"])
    o = st["owner"]
    had = (o.get("deputies") or {}) if extend else {}
    if flag is True or had.get("all"):
        return {"all": True, "since": had.get("since", ev["ts"])}
    words = [w for w in flag if isinstance(w, str) and w.strip()] if isinstance(flag, list) else []
    ids, unknown = _named(st, words)
    out = {"ids": list(dict.fromkeys((had.get("ids") or []) + ids)), "since": had.get("since", ev["ts"])}
    return {**out, **({"unknown": unknown} if unknown else {})}


def held(st):
    """This away is held until his /back or its term (reducer._received: his words do not end it): it is away with the
    flag, and the flag names a deputy he has appointed — whether or not their channel works now (a card to one whose
    channel is down waits for him; the away stays)."""
    o = st.get("owner") or {}
    flag = o.get("deputies") if o.get("state") == "away" else None
    if not flag:
        return False
    on = [pid for pid, r in (st.get("deputies") or {}).items() if r.get("on")]
    return bool(on) if flag.get("all") else any(pid in on for pid in flag.get("ids") or [])


def included(st):
    """{person id: record} of the deputies this away hands questions to: on, with the channel he named, in the flag.
    Empty — not away, no flag, or none fits."""
    o = st.get("owner") or {}
    flag = o.get("deputies") if o.get("state") == "away" else None
    if not flag:
        return {}
    on = active(st)
    return on if flag.get("all") else {pid: r for pid, r in on.items() if pid in (flag.get("ids") or [])}

