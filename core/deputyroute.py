"""A card to a deputy (spec d2679763, part A, A6–A7): while the owner is away with the flag, the session that asked (or
the hub) sends a card whose questions lie in a deputy's area to that deputy — `hub question route qN <person>`. The
card stays the owner's card: he may answer it himself; it is "at the deputy" (ask["deputy"]) until the session closes
it on the deputy's answer (`hub decided --card qN`), returns it to him (`--owner`), or something takes the ground away.

The way out is the deputy's channel (core/deputies.py): their hub on the bus — the core's own message in a thread of its
own per card (`route` = qN in the thread), the answer in that thread comes back to the session — or the session of
their channel (Teams), which asks the person with the route's tag [rN] and records their words verbatim (`hub answer
<session> --card qN --route rN --from … --msg <id>`). A hub with a rule of the liaison's about it is not asked: the
owner's rule about that hub is the liaison's to read, and a deputy's question does not go around it.

A deputy's answer is never the owner's word: no from_owner, answers_question, re or on_behalf; no gate of the core
passes with it. One card — one deputy — one thread: "not mine" returns it to the owner, never to someone else.

A deputy who decides everything in their area (deputies "all") takes the owner's own cards too (🚨, money, the hub's own
card) and their own request (--own, marked to them and in his summary); never a card of the liaison or the bus (their
answer is the bus's gate, his alone). What stays his whatever the mark — his personal data, his correspondence, a text
in his name, what needs his hands — the core cannot read in a card: the session that routes it judges, told so (ASK_ALL,
HUB_ALL), and the deputy and the session acting on the answer hear it again (OUT_ALL, REPLY_ALL)."""
from core import busstate, deputies, forms, liaison, tools
from core.coretext import T

ASKED, ANSWERED, RETURNED, DECIDED = "asked", "answered", "returned", "decided"
AT = (ASKED, ANSWERED)
WORDS_SHOWN = 4000   # the deputy's words in the owner's summary (all their answers on the card, in order)
MSGS_KEEP = 200  # Teams message ids a route took (a repeat --msg is refused); words kept per route

ROUTE_WHO = T("core.deputyroute.who", "a card goes to a deputy from the session that asked it or from the hub")
ROUTE_NOT_AWAY = T("core.deputyroute.not_away", "the owner is not away with the deputies (hub away --deputies): the "
                                                "card is theirs")
ROUTE_NOT_DEPUTY = T("core.deputyroute.not_deputy", "«{who}» is not a deputy of this away — they are: {names}")
ROUTE_NO_CARD = T("core.deputyroute.no_card", "no open card {qid} of {name}")
ROUTE_THEIRS = T("core.deputyroute.theirs", "card {qid} is the owner's own ({why}): it waits for them")
ROUTE_OWN = T("core.deputyroute.own", "card {qid} is about {name}'s own request: it waits for the owner — only a deputy "
                                      "who decides everything in their area takes their own request")
ROUTE_ALREADY = T("core.deputyroute.already", "card {qid} is at {name} already — their answer comes to {session}")
ROUTE_TRIED = T("core.deputyroute.tried", "card {qid} was at {name} and came back: it waits for the owner, not for "
                                          "the same deputy again")
ROUTE_RULES = T("core.deputyroute.rules", "the liaison has a rule about the hub {hub}: words to it go through the "
                                          "liaison, a deputy's card does not — it waits for the owner")
ROUTE_ADDRESS = T("core.deputyroute.address", "the channel of {name} is not the one the owner named: {why} — the card "
                                              "waits for the owner")
ROUTE_NOT_AT = T("core.deputyroute.not_at", "card {qid} is not at a deputy")
ROUTE_ANSWER_WHO = T("core.deputyroute.answer_who", "the deputy's words on route {route} are recorded by the session "
                                                    "of their channel ({session}) or the hub")
ROUTE_ANSWER_ROUTE = T("core.deputyroute.answer_route", "card {qid} has no route {route} waiting for an answer")
ROUTE_MSG_SEEN = T("core.deputyroute.msg_seen", "--msg {msg}: this message is recorded on {qid} already")
ROUTE_TOO_BIG = T("core.deputyroute.too_big", "the card's message is over {most} bytes: shorten --note")
ROUTE_FROM = T("core.deputyroute.from", "--from «{who}» is not {name}: only their own words answer route {route}")
ROUTE_MSG = T("core.deputyroute.msg", "--from \"<the person>\" --msg <the id of their message> and their words are "
                                      "needed")
DECIDED_NO_TEXT = T("core.deputyroute.decided_no_text", "hub decided --card qN \"<what> · <deputy>, deputy · how to "
                                                         "roll back\": the decision itself is needed")
DECIDED_NOT_ANSWERED = T("core.deputyroute.decided_not_answered",
                         "card {qid} has no deputy's answer to decide by: hub decided without --card, or wait for "
                         "the answer")

# to the deputy (their hub reads it first): who asks, on what, why them, and the question with its options
OUT = T("core.deputyroute.out",
        "For {name} to decide, not for their hub: please show it to them. The owner of the hub «{hub}» is away and named "
        "{name} their deputy for «{zone}»{nope}. From the session {session} of that hub{task}. Question: {questions}"
        "{note} Answer in this thread with {name}'s own words; if it is not theirs to decide — say so.")
OUT_NOT = T("core.deputyroute.out_not", " (not for: «{nope}»)")
OUT_TASK = T("core.deputyroute.out_task", ", task {id} «{words}»")
OUT_NOTE = T("core.deputyroute.out_note", " The session adds: «{note}».")
OUT_ALL = T("core.deputyroute.out_all",
            " {name} acts for the owner in «{zone}»: everything in it is theirs to decide, prod, money and merges "
            "included — except, always the owner's: their personal data, their correspondence, a text in the owner's "
            "name, and what needs the owner's own hands (Touch ID, a code from their phone, a password). If the question "
            "is one of these — answer that it is not yours.")
OUT_OWN = T("core.deputyroute.out_own", " It is about your own request: you decide it as the owner's deputy, and the "
                                        "owner will see it marked so.")
# to the session of the person's channel
VIA = T("core.deputyroute.via",
        "Route {route}: ask {name} through your channel ({address}) — a work question on the owner's task (they are "
        "away and named {name} their deputy for «{zone}»). Start your message with [{route}] and send this text as it "
        "is:\n{text}\nTheir answer, word for word: hub answer {session} --card {qid} --route {route} --from \"{name}\" "
        "--msg <the id of their message> \"<their words>\". A question back from them — hub send {session} \"[{route}] "
        "…\". Cannot reach them — hub question route {qid} --owner.")
# to the session that asked
REPLY = T("core.deputyroute.reply",
          "Reply on {qid} from {name} ({via}), the owner's deputy for «{zone}»{nope}: «{words}». It is {name}'s word, "
          "not the owner's: it settles questions in their area; the core's gates (tools, --for, publishing) stay the "
          "owner's. It settles the question — act and record it: hub decided --card {qid} \"<what> · {name}, deputy · "
          "how to roll back\" — the card closes. They ask back — answer them{back}. Not theirs, or it is not settled — "
          "hub question route {qid} --owner: the card waits for the owner.")
# the same from a deputy who decides everything in their area
REPLY_ALL = T("core.deputyroute.reply_all",
              "Reply on {qid} from {name} ({via}), who acts for the owner in «{zone}»{nope}: «{words}». It is {name}'s "
              "word, not the owner's, and it is the decision on this card: your rules and your repo's gates take it as "
              "the owner's answer to that gate would be (prod, a merge, publishing, money included). It never covers a "
              "text in the owner's name, nor showing their correspondence or personal data — those wait for the owner. "
              "It settles the question — record it first: hub decided --card {qid} \"<what> · {name}, deputy · how to "
              "roll back\" (the card closes); then a gate of the core that needs the owner's word takes --for {qid} "
              "(hub tools grant, hub task run/go, hub task spec accept; a tool with the owner's own data still waits for "
              "them). They ask back — answer them{back}. Not theirs, or it is not settled — hub question route {qid} "
              "--owner: the card waits for the owner.")
# the same to the liaison, whose form it was: it has no route, no gates of the core — only the bus and this close
REPLY_LIAISON = T("core.deputyroute.reply_liaison",
                  "Reply on your form {qid} from {name} ({via}), who acts for the owner in «{zone}»{nope}: «{words}». "
                  "For a work message it is the owner's \"Send\" or \"Don't send\" — act on it as on theirs; never for "
                  "promises or deadlines in the owner's name, a text in their name, their personal data or the contents "
                  "of their correspondence: those wait for the owner. Close the form: hub decided --card {qid} \"<what> "
                  "· {name}, deputy · —\". They ask back — answer them{back}.")
REPLY_VIA_HUB = T("core.deputyroute.via_hub", "through their hub {hub}")
REPLY_VIA_SESSION = T("core.deputyroute.via_session", "through {session}")
BACK_BUS = T("core.deputyroute.back_bus", " in the thread: hub send liaison --to {hub} --thread {thread} \"…\"")
BACK_VIA = T("core.deputyroute.back_via", ": hub send {session} \"[{route}] …\"")
VOID = T("core.deputyroute.void", "{name}'s reply on {qid} is no longer a ground: {why}. Do not act on it; the card is "
                                  "the owner's.")
GONE = T("core.deputyroute.gone", "Card {qid} did not reach {name}: {why}. It waits for the owner — do not send it "
                                  "again.")
WHY_WITHDRAWN = T("core.deputyroute.why_withdrawn", "the card is withdrawn")
WHY_RETURNED = T("core.deputyroute.why_returned", "the card went back to the owner")
CLOSED = T("core.deputyroute.closed", "Route {route} (card {qid}) is closed: {why}. Do not ask further; a late answer "
                                      "is not needed.")
WHY_ANSWERED = T("core.deputyroute.why_answered", "the owner answered the card himself")
WHY_BACK = T("core.deputyroute.why_back", "the owner is back")
WHY_OFF = T("core.deputyroute.why_off", "the owner removed the deputy or changed their area")
WHY_NOT_SENT = T("core.deputyroute.why_not_sent", "the message was not delivered ({code})")
WHY_THREAD = T("core.deputyroute.why_thread", "the thread with their hub is over (14 days or 50 messages)")
# the deputies a session is told of with its card (ASK_AWAY), and the hub with the cards it gets (AWAY_CARD, AWAY_OPEN)
ASK_DEPUTIES = T("core.deputyroute.ask_deputies",
                 " The owner's deputies now: {who}. A card whose every question lies in one deputy's area (and in none "
                 "of what they do not decide) — send it to them: hub question route {qid} <name>; their answer comes to "
                 "you on {qid}. Never to a deputy: money and paid plans; prod; releases and publishing; a message in the "
                 "owner's name to a person outside a work question; anything irreversible; access, keys and secrets; how "
                 "people and agents work; a choice the owner kept \"only on my word\"; a question about the deputy's own "
                 "request or proposal. Even if the area seems to say so — leave it for the owner.")
# with a deputy who decides everything in their area among them (deputies "all")
ASK_ALL = T("core.deputyroute.ask_all",
            " The owner's deputies now: {who}. A card whose every question lies in one deputy's area (and in none of what "
            "they do not decide) — send it to them: hub question route {qid} <name>; their answer comes to you on {qid}. "
            "A deputy marked (everything in it) acts for the owner in their area: every question of it is theirs — "
            "money, prod, releases and publishing, access, how people and agents work, the irreversible, a choice the "
            "owner kept \"only on my word\", and their own request or proposal (route it with --own). To the others, "
            "never: money and paid plans; prod; releases and publishing; a message in the owner's name to a person outside "
            "a work question; anything irreversible; access, keys and secrets; how people and agents work; a choice the "
            "owner kept \"only on my word\"; a question about the deputy's own request or proposal. To no deputy, "
            "marked or not: the owner's personal data; the owner's correspondence (showing, forwarding or retelling it, in "
            "the question, its options or --note); any text in the owner's name (a letter, a post, an answer to a person "
            "as them); what needs the owner's own hands (Touch ID, a code from their phone, a password, a captcha). A card "
            "with one such question among others — leave it whole for the owner.")
HUB_DEPUTIES = T("core.deputyroute.hub_deputies",
                 " The owner's deputies now: {who} — a card in one deputy's area: hub question route <qN> <name> "
                 "(never the owner's own decisions above, never a deputy's own request).")
HUB_ALL = T("core.deputyroute.hub_all",
            " The owner's deputies now: {who} — a card in one deputy's area: hub question route <qN> <name>. One marked "
            "(everything in it) acts for the owner there: the owner's own decisions above are theirs too, the liaison's "
            "forms on words to other hubs, and their own request (--own); the others — never the owner's own decisions, "
            "never a deputy's own request. To no deputy: "
            "the owner's personal data, their correspondence, a text in the owner's name, what needs the owner's own hands "
            "(Touch ID, a code from their phone, a password).")
TRAIL = T("core.msg.trail_deputy", "closed {id} on the answer of the deputy {name}")
DEPUTY_LINE = T("core.deputyroute.deputy_line", "{name} — «{zone}»{nope}")
DEPUTY_NOT = T("core.deputyroute.deputy_not", " (not: «{nope}»)")
DEPUTY_ALL = T("core.deputyroute.deputy_all", " (everything in it)")


def _reducer():
    from core import reducer
    return reducer


def who_line(inc):
    """The deputies of this away (deputies.included) with their areas, for a text of the core; "" — none."""
    return "; ".join(DEPUTY_LINE.format(name=r["person"]["name"], zone=r["zone"],
                                        nope=DEPUTY_NOT.format(nope=r["not"]) if r.get("not") else "")
                     + (DEPUTY_ALL if r.get("all") else "") for r in inc.values())


def ask_text(st, qid, owners_own=False):
    """The deputies told to a session with its card; owners_own — the card is one the owner keeps for himself
    (reducer._owners_own): only a deputy who decides everything takes it, so without one there is nothing to tell."""
    inc = deputies.included(st)
    if owners_own:   # only those who decide everything take it: the others are not named
        inc = {pid: r for pid, r in inc.items() if r.get("all")}
    if not inc:
        return ""
    return (ASK_ALL if any(r.get("all") for r in inc.values()) else ASK_DEPUTIES).format(who=who_line(inc), qid=qid)


def hub_text(st):
    inc = deputies.included(st)
    return (HUB_ALL if any(r.get("all") for r in inc.values()) else HUB_DEPUTIES).format(who=who_line(inc)) \
        if inc else ""


def to_hub(st, sess, ask):
    """The liaison's form goes to the hub with the cards left for the owner while a deputy who decides everything can
    take it (the liaison cannot route it itself); any other card of the owner's own stays out, as before."""
    return liaison.is_liaison(st, sess["name"]) and not busstate.is_card(ask) \
        and any(r.get("all") for r in deputies.included(st).values())


def bus_gate(st, sess, ask):
    """A core card of the bus (whom to trust: a link offer, an introduction, an item of a hub not checked): its answer
    is the bus's own gate, the owner's alone — no deputy takes it, even one who decides everything (wherever it sits:
    the bus puts one on the hub when there is no liaison). The liaison's own forms on words to another hub are work
    cards: a deputy who decides everything takes them (REPLY_LIAISON)."""
    return busstate.is_card(ask)


def at(ask):
    """The card is at a deputy now."""
    return (ask.get("deputy") or {}).get("state") in AT


def _find(st, name, qid):
    sess = (st.get("sessions") or {}).get(name or "")
    ask = next((a for a in (sess or {}).get("asks") or [] if a["id"] == qid), None)
    return sess, ask


def _who(st, data, sess):
    sender = data.get("sender")
    is_hub = ((st.get("sessions") or {}).get(sender or "") or {}).get("kind") == "hub"
    if data.get("channel") != "session" or not (is_hub or sender == sess["name"]):
        raise tools.Rejected(ROUTE_WHO)
    return sender


def _frame(st, sess, ask, rec, note, own=False):
    r = _reducer()
    from core import coretext
    d = (st.get("delegations") or {}).get(ask.get("delegation") or "") or {}
    task = OUT_TASK.format(id=d["id"], words=" ".join(str(d.get("words") or "").split())[:120]) if d else ""
    out = OUT.format(name=rec["person"]["name"], hub=(st.get("bus") or {}).get("name") or "—", zone=rec["zone"],
                     nope=OUT_NOT.format(nope=rec["not"]) if rec.get("not") else "", session=sess["name"],
                     task=task, questions=r._card_text(ask), note=OUT_NOTE.format(note=note) if note else "")
    return coretext.join(out, *([OUT_ALL.format(name=rec["person"]["name"], zone=rec["zone"])] if rec.get("all")
                                else []), *([OUT_OWN] if own else []))


def route(st, ev, data, actions):
    """question.routed: to a deputy of this away, or back to the owner (`back`)."""
    r = _reducer()
    sess, ask = _find(st, ev.get("entity"), data.get("id"))
    if not ask:
        raise tools.Rejected(ROUTE_NO_CARD.format(qid=data.get("id"), name=ev.get("entity")))
    dep = ask.get("deputy") or {}
    if data.get("back"):
        return _back(st, ev, data, sess, ask, dep)
    who = _who(st, data, sess)
    inc = deputies.included(st)
    ids, _ = deputies._named(st, [str(data.get("person") or "")])
    if not inc and not deputies.held(st):
        raise tools.Rejected(ROUTE_NOT_AWAY)
    flag = (st.get("owner") or {}).get("deputies") or {}
    down = (st.get("deputies") or {}).get(ids[0]) if ids and ids[0] not in inc and deputies.held(st) \
        and (flag.get("all") or ids[0] in (flag.get("ids") or [])) else None
    if down and down.get("on"):   # appointed, on this away's flag, but their channel does not work now
        raise tools.Rejected(ROUTE_ADDRESS.format(name=down["person"]["name"], why=deputies.why_text(st, down)))
    pid = ids[0] if ids and ids[0] in inc else None
    if pid is None:
        raise tools.Rejected(ROUTE_NOT_DEPUTY.format(who=str(data.get("person") or "")[:100],
                                                     names=", ".join(x["person"]["name"] for x in inc.values())))
    rec = inc[pid]
    own = r._owners_own(st, sess, ask)
    # a deputy who decides everything takes his own cards too, but never the liaison's or the bus's (bus_gate: a
    # deputy's "decided" would only close it)
    if own and (not rec.get("all") or bus_gate(st, sess, ask)):
        raise tools.Rejected(ROUTE_THEIRS.format(qid=ask["id"], why=r.OWNERS_OWN["bus"] if busstate.is_card(ask)
                                                 else own))
    theirs = data.get("own") is True   # the card is about the deputy's own request (the session says so: --own)
    if theirs and not rec.get("all"):
        raise tools.Rejected(ROUTE_OWN.format(qid=ask["id"], name=rec["person"]["name"]))
    if dep.get("state") in AT:
        raise tools.Rejected(ROUTE_ALREADY.format(qid=ask["id"], name=dep.get("name"), session=sess["name"]))
    if pid in (ask.get("deputy_tried") or []):
        raise tools.Rejected(ROUTE_TRIED.format(qid=ask["id"], name=rec["person"]["name"]))
    ch = rec["channel"]
    note = " ".join(str(data.get("note") or "").split())[:500]
    frame = _frame(st, sess, ask, rec, note, theirs)
    if len(frame.encode("utf-8")) > busstate.SEND_MAX:   # the bus's own limit, as for any session's words
        raise tools.Rejected(ROUTE_TOO_BIG.format(most=busstate.SEND_MAX))
    out = {"pid": pid, "name": rec["person"]["name"], "zone": rec["zone"], **({"not": rec["not"]} if rec.get("not")
                                                                               else {}),
           "appt": rec["seq"], "state": ASKED, "at": ev["ts"], "by": who, "kind": ch["kind"],
           **({"all": True} if rec.get("all") else {}), **({"own": True} if theirs else {})}
    if ch["kind"] == "bus":
        why = busstate.direct_refusal(st, ch["contact"], owned=[])
        if why == "rules":
            raise tools.Rejected(ROUTE_RULES.format(hub=ch["contact"]))
        if why:
            raise tools.Rejected(ROUTE_ADDRESS.format(name=rec["person"]["name"], why=why))
        bus = busstate._bus(st)
        oid = f"o{ev['seq']}"
        bus["out"][oid] = {"kind": "message", "status": "requested", "contact": ch["contact"], "fp": ch["fp"],
                           "source": sess["name"], "basis": None, "via": "local", "text": frame, "agent_to": None,
                           "thread": None, "ts": ev["ts"], "of": ev["seq"], "direct": True, "route": ask["id"]}
        actions.append({"do": "bus_send", "session": None, "out": oid, "of": ev["seq"]})
        out.update(contact=ch["contact"], out=oid)
    else:
        why = deputies.why_text(st, rec, data.get("address"))
        if why:
            raise tools.Rejected(ROUTE_ADDRESS.format(name=rec["person"]["name"], why=why))
        rid = f"r{ev['seq']}"
        address = ", ".join(f"{k} {v}" for k, v in sorted(ch["address"].items()))
        r.new_message(st, f"m{ev['seq']}", ch["session"], VIA.format(
            route=rid, name=rec["person"]["name"], address=address, zone=rec["zone"], text=frame,
            session=sess["name"], qid=ask["id"]), ev["ts"], origin=forms.ORIGIN_CORE)
        out.update(session=ch["session"], route=rid)
    ask["deputy"] = out
    ask.setdefault("deputy_tried", []).append(pid)


def _back(st, ev, data, sess, ask, dep):
    """`hub question route qN --owner`: from the session that asked, the hub, or the session of the person's channel
    that cannot reach them (VIA tells it so). Whoever of the other two holds a part of the route hears it is over."""
    sender = data.get("sender")
    chan = dep.get("kind") == "via" and data.get("channel") == "session" and sender and sender == dep.get("session")
    who = sender if chan else _who(st, data, sess)
    if dep.get("state") not in AT:
        raise tools.Rejected(ROUTE_NOT_AT.format(qid=ask["id"]))
    was = dep["state"]
    dep.update(state=RETURNED, done=ev["ts"], why="back", by=who)
    r = _reducer()
    ch = (st.get("sessions") or {}).get(dep.get("session") or "") or {}
    if dep.get("kind") == "via" and ch and who != ch["name"] and ch.get("state") not in deputies.GONE:
        r.new_message(st, f"m{ev['seq']}vc", ch["name"], CLOSED.format(route=dep.get("route"), qid=ask["id"],
                                                                        why=WHY_RETURNED), ev["ts"],
                      origin=forms.ORIGIN_CORE)
    if who != sess["name"] and sess.get("state") not in deputies.GONE:
        text = VOID if was == ANSWERED else GONE
        r.new_message(st, f"m{ev['seq']}v", sess["name"], text.format(name=dep["name"], qid=ask["id"],
                                                                      why=WHY_RETURNED), ev["ts"],
                      origin=forms.ORIGIN_CORE)
    _to_hub(st, ev, sess, ask)


def _sub(ev, tag):
    """The event under another number for the messages a check makes on the way: the event's own message (mN of a
    message.received, the hub's of a bus fact) keeps its id."""
    return {**ev, "seq": f"{ev['seq']}{tag}"}


def _to_hub(st, ev, sess, ask):
    """A card back from a deputy while the owner is still away: the hub gets it as any card left for him (AWAY_CARD) —
    it skipped the hub while at the deputy."""
    r = _reducer()
    if (st.get("owner") or {}).get("state") == "away" and sess.get("state") not in deputies.GONE \
            and r._hub_sees(st, sess, ask):
        r._away_card(st, ev, sess, ask)


def _ground(st, ask):
    """Why the deputy's answer on this card is no longer a ground (VOID's why), or None."""
    dep = ask.get("deputy") or {}
    rec = (st.get("deputies") or {}).get(dep.get("pid") or "") or {}
    if not rec.get("on") or rec.get("seq") != dep.get("appt") or deputies.channel_why(st, rec) is not None:
        return WHY_OFF
    if (st.get("owner") or {}).get("state") != "away":
        return WHY_BACK
    return None


def _reply_text(st, sess, ask, words, via, back):
    dep = ask["deputy"]
    text = REPLY_LIAISON if liaison.is_liaison(st, sess["name"]) else REPLY_ALL if dep.get("all") else REPLY
    return text.format(qid=ask["id"], name=dep["name"], via=via, zone=dep["zone"],
                        nope=OUT_NOT.format(nope=dep["not"]) if dep.get("not") else "", words=words, back=back)


def reply(st, ev, item, to):
    """The deputy's words in the thread of a card's route, given to the session that asked: the frame for it, or None
    — the card is no longer at them (an ordinary answer in the thread)."""
    sess, ask = _find(st, to, item.get("route"))
    if not ask or not at(ask) or (ask["deputy"].get("contact") != item.get("contact")):
        return None
    why = _ground(st, ask)
    if why:   # an answer under a changed appointment: the card is the owner's, the words an ordinary answer
        void(st, _sub(ev, "g"), sess, ask, why)
        return None
    dep = ask["deputy"]
    dep.update(state=ANSWERED, answered=ev["ts"])
    dep.setdefault("words", []).append(" ".join(str(item.get("text") or "").split())[:2000])
    del dep["words"][:-MSGS_KEEP]
    return _reply_text(st, sess, ask, item.get("text") or "", REPLY_VIA_HUB.format(hub=item["contact"]),
                       BACK_BUS.format(hub=item["contact"], thread=item.get("thread")))


def answered(st, ev, data):
    """question.deputy_answered: the session of the person's channel (or the hub) records their words on route rN."""
    r = _reducer()
    sess, ask = _find(st, ev.get("entity"), data.get("id"))
    if not ask:
        raise tools.Rejected(ROUTE_NO_CARD.format(qid=data.get("id"), name=ev.get("entity")))
    dep = ask.get("deputy") or {}
    route_id = str(data.get("route") or "")
    if dep.get("route") != route_id or dep.get("state") not in AT:
        raise tools.Rejected(ROUTE_ANSWER_ROUTE.format(qid=ask["id"], route=route_id))
    sender = data.get("sender")
    is_hub = ((st.get("sessions") or {}).get(sender or "") or {}).get("kind") == "hub"
    if data.get("channel") != "session" or not (is_hub or sender == dep.get("session")):
        raise tools.Rejected(ROUTE_ANSWER_WHO.format(route=route_id, session=dep.get("session")))
    msg, words = str(data.get("msg") or "").strip(), str(data.get("text") or "").strip()
    if not msg or not words or not str(data.get("from") or "").strip():
        raise tools.Rejected(ROUTE_MSG)
    rec = (st.get("deputies") or {}).get(dep.get("pid") or "") or {}
    theirs = {w for n in [dep["name"], *(rec.get("names") or [])] for w in deputies.norm(n).split() if len(w) > 1}
    if not theirs & set(deputies.norm(data["from"]).split()):   # someone else in the chat: their own name, whole
        raise tools.Rejected(ROUTE_FROM.format(who=str(data["from"])[:100], name=dep["name"], route=route_id))
    if msg[:200] in (dep.get("msgs") or []):
        raise tools.Rejected(ROUTE_MSG_SEEN.format(msg=msg[:80], qid=ask["id"]))
    why = deputies.why_text(st, rec, data.get("address")) if rec.get("on") else None
    if why:   # their address in the book changed since he named them: not their channel any more
        void(st, _sub(ev, "a"), sess, ask, why)
        return
    why = _ground(st, ask)
    if why:
        raise tools.Rejected(VOID.format(name=dep["name"], qid=ask["id"], why=why))
    dep.setdefault("msgs", []).append(msg[:200])
    del dep["msgs"][:-MSGS_KEEP]
    dep.update(state=ANSWERED, answered=ev["ts"])
    dep.setdefault("words", []).append(" ".join(words.split())[:2000])
    del dep["words"][:-MSGS_KEEP]
    r.new_message(st, f"m{ev['seq']}", sess["name"], _reply_text(
        st, sess, ask, words, REPLY_VIA_SESSION.format(session=dep["session"]),
        BACK_VIA.format(session=dep["session"], route=route_id)), ev["ts"], origin=forms.ORIGIN_CORE,
        deputy_reply=ask["id"])


def void(st, ev, sess, ask, why, k=""):
    """The ground is gone (the owner answered, the deputy removed, away over, not delivered, the thread over): the card
    is the owner's again; a session that has the deputy's answer hears it is no ground; one still waiting — that the
    card did not reach them."""
    dep = ask.get("deputy") or {}
    if dep.get("state") not in AT:
        return
    was = dep["state"]
    dep.update(state=RETURNED, done=ev["ts"], why=str(why))
    r = _reducer()
    ch = (st.get("sessions") or {}).get(dep.get("session") or "") or {}
    if dep.get("kind") == "via" and ch and ch.get("state") not in deputies.GONE:   # the session of their channel stops
        r.new_message(st, f"m{ev['seq']}{k or 'v'}c", ch["name"], CLOSED.format(route=dep.get("route"), qid=ask["id"],
                                                                                 why=why), ev["ts"],
                      origin=forms.ORIGIN_CORE)
    closed = why is WHY_ANSWERED or why is WHY_WITHDRAWN   # the card is closed: nothing for the hub
    if not closed:
        _to_hub(st, ev, sess, ask)
    if sess.get("state") in deputies.GONE or why is WHY_WITHDRAWN:
        return
    # the session that has the deputy's answer hears it is no ground; one still waiting — that it did not reach them
    text = VOID.format(name=dep["name"], qid=ask["id"], why=why) if was == ANSWERED else \
        None if closed else GONE.format(name=dep["name"], qid=ask["id"], why=why)
    if text:
        r.new_message(st, f"m{ev['seq']}{k or 'v'}", sess["name"], text, ev["ts"], origin=forms.ORIGIN_CORE)


def withdrawn(st, ev, sess, ask):
    """The card is withdrawn (by the session, the hub or the owner) while at a deputy: their channel's session stops."""
    void(st, ev, sess, ask, WHY_WITHDRAWN)


def at_deputies(st):
    """[(session, card)] of the cards at a deputy now."""
    return [(s["name"], a) for s, a in each_at(st)]


def each_at(st, keep=lambda ask: True):
    return [(s, a) for s in (st.get("sessions") or {}).values() for a in s.get("asks") or [] if at(a) and keep(a)]


def deputy_changed(st, ev, pid):
    """contact.deputy_set/_off: the cards at this deputy under an earlier appointment go back to the owner."""
    rec = (st.get("deputies") or {}).get(pid) or {}
    for n, (s, a) in enumerate(each_at(st, lambda a: a["deputy"].get("pid") == pid
                                       and (not rec.get("on") or rec.get("seq") != a["deputy"].get("appt")))):
        void(st, ev, s, a, WHY_OFF, k=f"d{n}")
    if rec.get("on"):   # named again (the mark, the channel, the area): a new appointment may take their cards again
        for s in (st.get("sessions") or {}).values():
            for a in s.get("asks") or []:
                if pid in (a.get("deputy_tried") or []):
                    a["deputy_tried"] = [p for p in a["deputy_tried"] if p != pid]


def came_back(st, ev):
    """The owner is back: every card at a deputy is his card again (with back_at, as any card of the away). The
    summary keeps the ones still waiting for an answer (summary_waiting, read before this)."""
    for n, (s, a) in enumerate(each_at(st)):
        void(st, ev, s, a, WHY_BACK, k=f"b{n}")


def owner_answered(st, ev, sess, ask):
    void(st, ev, sess, ask, WHY_ANSWERED)


def not_sent(st, ev, o, code):
    """The bus did not take or deliver the card's message: back to the owner."""
    sess, ask = _find(st, o.get("source"), o.get("route"))
    if ask and at(ask) and ask["deputy"].get("out") is not None and \
            ((st.get("bus") or {}).get("out") or {}).get(ask["deputy"]["out"]) is o:
        void(st, _sub(ev, "n"), sess, ask, WHY_NOT_SENT.format(code=code))


QUIET = ("turn.", "session.said", "session.gist", "session.waiting", "account.", "browser.", "spool.", "ext.", "mod.")


def check(st, ev):
    """The cards at a deputy whose ground went with no fact about it — the deputy's channel changed (another key, an
    unlinked or quarantined hub, a gone session) or the thread with their hub is over (THREAD_TTL, THREAD_MAX): back to
    the owner. A function of the state and the event's time, no timer; not on the stream of a turn's own events."""
    if str(ev.get("type") or "").startswith(QUIET):
        return
    bus = st.get("bus") or {}
    for n, (s, a) in enumerate(each_at(st)):
        why = _ground(st, a)
        o = (bus.get("out") or {}).get(a["deputy"].get("out") or "") or {}
        if why is None and a["deputy"].get("kind") == "bus" and o.get("thread") \
                and o.get("status") not in ("requested", "held") \
                and busstate.live_thread(bus, o["thread"], a["deputy"].get("contact"), ev["ts"]) is None:
            why = WHY_THREAD
        if why is not None and why is not WHY_BACK:   # coming back voids them itself (came_back)
            void(st, _sub(ev, f"t{n}"), s, a, why)


def decided(st, ev, data):
    """`hub decided --card qN "…"`: the session acts on the deputy's answer and closes the card with that ground.
    Refused when the card has no deputy's answer, or the ground is gone. Returns the record for the owner's summary."""
    r = _reducer()
    sender = data.get("sender")
    qid = str(data.get("card") or "")
    sess = next((s for s in (st.get("sessions") or {}).values() for a in s.get("asks") or [] if a["id"] == qid), None)
    ask = next((a for a in (sess or {}).get("asks") or [] if a["id"] == qid), None)
    if not ask:
        raise tools.Rejected(r.NO_OPEN_QUESTION)
    _who(st, data, sess)
    dep = ask.get("deputy") or {}
    if dep.get("state") != ANSWERED:
        raise tools.Rejected(DECIDED_NOT_ANSWERED.format(qid=qid))
    if not str(data.get("text") or "").strip():
        raise tools.Rejected(DECIDED_NO_TEXT)
    rec = (st.get("deputies") or {}).get(dep.get("pid") or "") or {}
    why = _ground(st, ask) or (deputies.why_text(st, rec, data.get("address")) if data.get("address") is not None
                               else None)
    if why:
        raise tools.Rejected(VOID.format(name=dep["name"], qid=qid, why=why))
    dep.update(state=DECIDED, done=ev["ts"])
    form = liaison.is_liaison(st, sess["name"])   # the liaison's form quotes another hub: its decision is the bus's
    if dep.get("all") and not form:   # may open a gate of the core (word)
        _keep_word(st, ev, sess, ask, dep)
    r._drop_ask(st, sess, ask, sender, TRAIL.format(id=qid, name=dep["name"]))
    if form:   # the item its form held is the liaison's again, 30 minutes from now
        busstate.liaison_answered(st, ask.get("request_id"), ev["ts"])
    r._answered(sess, {**ev, "type": "question.withdrawn"}, data, ask=ask, why=f"deputy {dep['name']}")
    return {"card": qid, "asker": sess["name"], "deputy": dep["name"], "via": dep.get("contact") or dep.get("session"),
            "words": " / ".join(dep.get("words") or [])[:WORDS_SHOWN], **({"own": True} if dep.get("own") else {})}


def _keep_word(st, ev, sess, ask, dep):
    """The decision of a deputy who decides everything on card ask, for `--for qN` of a gate of the core: the card's
    questions and their words — what names a tool, as the owner's card and his answer do (not the session's decision:
    the session would name the tool itself); kept a day."""
    words = st.setdefault("deputy_words", {})
    for k in [k for k, w in words.items() if ev["ts"] - w["ts"] > tools.OWNER_WORD_TTL]:
        del words[k]
    words[ask["id"]] = {"pid": dep["pid"], "name": dep["name"], "appt": dep["appt"], "session": sess["name"],
                        "away": (st.get("owner") or {}).get("away_since"), "ts": dep.get("answered") or ev["ts"],
                        "text": " ".join(f"{_reducer()._card_text(ask, 1500)} — {dep['name']}: "
                                         f"«{' / '.join(dep.get('words') or [])[:1000]}»".split())}


def word(st, qid):
    """The deputy's decision on card qid as tools.owner_word reads a word, or None — gone: the same appointment (on,
    everything, its seq), channel and away (deputies.included, the away of the decision); the day is owner_word's."""
    w = (st.get("deputy_words") or {}).get(qid)
    if not w:
        return None
    rec = (st.get("deputies") or {}).get(w["pid"]) or {}
    if not (rec.get("all") and rec.get("seq") == w["appt"] and w["pid"] in deputies.included(st)
            and (st.get("owner") or {}).get("away_since") == w["away"]):
        return None
    return {"id": qid, "text": w["text"], "session": w["session"], "created": w["ts"], "deputy": w["name"]}


def opened(st, qid, gate):
    """A gate of the core passed on the deputy's decision on qid: it goes into the owner's summary with the card."""
    for d in (st.get("owner") or {}).get("decided") or []:
        dep = d.get("deputy")
        if isinstance(dep, dict) and dep.get("card") == qid and gate not in (dep.get("opened") or []):
            dep.setdefault("opened", []).append(gate)


def summary_waiting(st):
    """The cards at a deputy without an answer when the owner comes back: [{session, card, deputy, since}]."""
    return [{"session": s["name"], "card": a["id"], "deputy": a["deputy"]["name"], "since": a["deputy"]["at"]}
            for s, a in each_at(st) if a["deputy"]["state"] == ASKED]

