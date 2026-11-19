"""The bus in the core state (spec hubbus §5.5–5.8, §5.13), `state["bus"]`: whether it is on, the hub's public keys, the
contact table, the envelopes seen (repeat protection), the counters of the limits and the quarantine; and since slice S1
the outgoing queue (`out`), the incoming the liaison decides on (`inbox`), the threads (`threads`: whose words opened
each), the sessions' requests to the liaison (`requests`) and the introductions waiting for the owner (`intros`).

Who writes what:
- the owner asks (`bus.on_requested`, `bus.off_requested`, `bus.contact_requested` — strict guards.from_owner: not a
  session, not the hub on the owner's word, not a module or plugin) and answers the core's cards (an ask on the
  liaison: `permission.answered`, strictly from him);
- the liaison asks (`bus.send_requested`, `bus.pass_requested`, `bus.decline_requested`, `bus.done_requested`,
  `bus.note`) — from any other
  session `not_liaison`; it decides nothing: the core checks and decides;
- a transport offers bytes and says it handed an envelope (`bus.offered`, `bus.handed`): the owner's terminal (the
  folder transport of the stand, `hub bus wire`), the built-in relay transport (src `bus-relay`, modules/bus_relay) or a
  module with the Module API right `bus.transport` (the daemon checks the right before writing); it reads what to hand
  from the transport view the core writes (`transport_view`, core/busd.py), never the state;
- the owner joins a relay and leaves it (`bus.relay_requested`): its address and key come only from the invitation —
  the core has no relay address of its own (spec hubbus §5.11);
- the core decides and writes `core.bus_*` facts (the spool refuses any `core.*` from outside, core/daemon.spooled).
The fold does not check signatures or cards again and does not seal — those are the daemon's decisions (core/bus.py).

A contact record carries `person` (the link to a person of the contacts book, spec §5.5.1): it never takes part in a
decision about trust — only in showing and in finding a contact by a person's name.
"""
import re
import unicodedata

from core import coretext, forms, langdata
from core.coretext import T

# version 2: the classes of a box (named in its signed head, known before decryption). A known contact's messages are
# not limited by rate: the counts per hour stay in the state (what was taken), nothing is refused by them
CLASSES = ("chat", "line", "liaison", "ctl")
PEER_FRESH = 600          # the relay's word about a contact's version is asked again after this long
V2_MISSES = 2             # unanswered v2 messages in a row: the contact gets no v2 until the relay says otherwise
UNANSWERED = 600          # a handed v2 message without a receipt this long counts as one miss
JOIN_GRACE = 3600         # B waits for A's answer this long past the link's term (a late ack is taken while the record lives)
LINKS_KEEP = 14 * 86400   # finished links and joins are kept this long (the page shows them, then they go)
MISUSE_AFTER = 600        # a permit used by a hub that did not come with the link in this long: the owner is told
BAD_BOX_DAY = 3           # boxes that do not open in a day → quarantine (no decryption until the owner decides)
SEEN_EXTRA = 86400        # a seen envelope is kept until its exp plus a day
AUTO_MAX = 8192           # bytes of the owner's own chat text (a key-like one asks again); a session's text: SEND_MAX
SEND_MAX = 60000          # bytes of a text the liaison sends (no attachments on the bus: a text or a link)
BASIS_AGE = 86400         # --text-from takes a message not older than a day
THREAD_TTL = 14 * 86400   # a thread gives its source the right to the answer this long
THREAD_MAX = 50           # messages in a thread
# what a thread keeps of its source's latest request, and an answer in it carries: the owner's chain the request was in
# (CHAIN: the answer's message carries it, _chain) and whether the source wrote it in a turn of the owner's
# (in_owner_turn, owner_turn: only the message given to that source, _deliver)
CHAIN = ("root", "via_schedule")
THREAD_KEYS = CHAIN + ("in_owner_turn",)
GONE_MAX = 20             # removed contacts whose local names are remembered
RULES_MAX = 30            # the liaison's rules about hubs
RULE_TEXT = 400           # characters of one rule
STALE = 30 * 60           # an incoming item or a request without an outcome this long — a mark and one notice
INBOX_KEEP = 14 * 86400
NOTES_KEEP = 200
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
STATUSES = ("pending", "known", "muted", "blocked")
OPS = ("add", "set", "remove", "link", "intro", "verify", "expose", "rename")
ONE_SHOT = ("receipt", "intro", "unlink", "card_update")   # envelopes handed once: nothing comes back for them
JOIN_STEPS = ("parsed", "joined", "contact", "intro_sent", "linked")
JOIN_OPEN = ("active", "expired", "cancelling")   # a join that still takes A's answer
OUTCOMES = ("passed", "answered", "declined", "asked_owner")
CARD_TAG = "bus-"          # the request_id of the core's cards on the liaison: bus-o<seq>, bus-b<seq>, bus-i<fp>
# the mark of another hub's words delivered by the core: no `from` kind of its own (the Plugin API names the kinds; the
# page shows the bus in slice S3) — the channel `hubbus` and the frame say what it is; never the owner's word
ORIGIN = forms.ORIGIN_CORE + ", hub bus"

# the core's refusals (core.rejected: the session reads the English, the owner his language — core/coretext.py); a
# machine code before ": " (bus_off, no_contact, not_verified, …) stays in every language
BUS_OFF = T("core.busstate.bus_off", "bus_off: the bus is off")
NO_SUCH_CONTACT = T("core.busstate.no_such_contact", "no such bus contact")
NO_CONTACT = T("core.busstate.no_contact", "no_contact: no bus contact {name}")
NOT_ACCEPTED = T("core.busstate.not_accepted", "contact {name} is not accepted (status {status})")
NOT_VERIFIED = T("core.busstate.not_verified", "not_verified: hub {name} is not checked — the owner checks the pair code "
                                               "on the page (the \"External hubs\" tab)")
EMPTY_TEXT = T("core.busstate.empty_text", "empty text")
HUB_NAME_BAD = T("core.busstate.hub_name_bad",
                 "the hub name is a line up to 100 characters without control and invisible characters")
CONTACT_NAME_BAD = T("core.busstate.contact_name_bad",
                     "a bus contact name is lowercase Latin letters, digits and hyphens, up to 32 characters")
PERSON_BAD = T("core.busstate.person_bad", "whose hub — a record of the contacts book: id (c + 8 hex) and name")
NO_LINK = T("core.busstate.no_link", "no such link")
LINK_ALREADY = T("core.busstate.link_already", "the link is already {state}")
JOIN_ALREADY = T("core.busstate.join_already", "the join is already {state}")
NO_OUT = T("core.busstate.no_out", "no such outgoing message")
NO_OUT_WAITING = T("core.busstate.no_out_waiting", "no such outgoing message waiting")
NO_INBOX = T("core.busstate.no_inbox", "no incoming item {id}")
BAD_FIELD = T("core.busstate.bad_field", "{kind}: field {field}")


def empty():
    return {"enabled": False, "crypto_ready": None, "me": {}, "contacts": {}, "seen": {}, "rate": {}, "bad": {},
            "last": None, "out": {}, "inbox": {}, "threads": {}, "requests": {}, "intros": {}, "notes": [],
            "relay": None, "knocks": {}, "links": {}, "joins": {}, "jobs": {}, "unlinked": {}}


def _bus(st):
    b = st.setdefault("bus", empty())
    for k, v in empty().items():   # a state of slice S0 has no S1 parts yet
        b.setdefault(k, v if not isinstance(v, (dict, list)) else type(v)())
    return b


def seen_key(fp, mid):
    return f"{fp} {mid}"


def by_fp(bus, fp):
    """(local name, record) of the contact with this fingerprint, or (None, None)."""
    for name, c in (bus.get("contacts") or {}).items():
        if c.get("fp") == fp:
            return name, c
    return None, None


def counts(bus, fp, now, cls=None):
    """(envelopes, messages) of the contact in the current hour — of a class of version 2, or of version 1."""
    r = (bus.get("rate") or {}).get(fp) or {}
    if r.get("h") != int(now) // 3600:
        return 0, 0
    if cls:
        c = (r.get("cls") or {}).get(cls) or {}
        return c.get("env", 0), c.get("msg", 0)
    return r.get("env", 0), r.get("msg", 0)


def effective_v(bus, contact):
    """The protocol this contact's hub reads now (spec hubbus/ux §4.0): a relay of version 1 carries no v2; the relay's
    word about the hub (it hears X-Hub-Bus-V with every request of that hub, a rolled back hub sets it to 1) over the
    card's; two v2 messages without an answer in a row — 1, until the relay says ≥ 2 again after them."""
    relay = bus.get("relay") or {}
    if relay and (relay.get("bus_v") or 1) < 2:
        return 1
    peer = contact.get("peer") or {}
    v = peer["v"] if type(peer.get("v")) is int else contact.get("bus_v") if type(contact.get("bus_v")) is int else 1
    miss = contact.get("v2_miss") or {}
    if miss.get("n", 0) >= V2_MISSES and peer.get("seq", -1) <= miss.get("seq", 0):
        return 1
    return v


def label_ok(label):
    """A hub label: a non-empty line up to 100 characters without control and format characters (the card's rule)."""
    return (isinstance(label, str) and 0 < len(label.strip()) and len(label) <= 100
            and not any(unicodedata.category(c) in ("Cc", "Cf") for c in label))


def owner_thread(bus, thread, contact, now):
    """Is this thread one the owner's own words opened with this contact, and alive: an answer in it is for the owner
    (the page and Telegram, no turn of the liaison), the contact being known and checked."""
    th = live_thread(bus, thread, contact, now)
    c = (bus.get("contacts") or {}).get(contact) or {}
    return bool(th and th.get("owner") and c.get("status") == "known" and c.get("verified"))


def folded_at_once(kind, data):
    """Owner requests the fold answers by itself (no fact of the daemon comes back for them): the caller does not wait."""
    return kind in ("bus.off_requested", "bus.relay_requested", "bus.reset_requested", "bus.name_requested",
                    "bus.mode_requested",
                    "bus.rule_requested", "bus.link_revoke_requested", "bus.join_cancel_requested") or (
        kind == "bus.contact_requested" and (data or {}).get("op") == "rename")


def shown(c):
    """What the owner of this hub calls a contact: his own name for it (`alias`, `hub bus contact rename`), else the
    label the other hub gave itself. Only for showing — the address of a contact is its local name, the trust is its keys."""
    return c.get("alias") or c.get("label")


def alias_ok(alias):
    """The owner's own name for a contact hub: label_ok's rule, and no line or paragraph separators either."""
    return label_ok(alias) and not any(unicodedata.category(c) in ("Zl", "Zp") for c in alias)


def live_thread(bus, thread, contact, now):
    """The thread with this contact while it is alive, else None (an answer in it is an ordinary incoming message);
    another contact that knows the id gets nothing from it. Its `source` is the session whose words opened it; its
    `root` (and `via_schedule`) is the chain (core/chain.py) of the source's latest request in it, if that was on the
    owner's word: an answer continues that chain, so the result on it goes where he asked. No root — the request was
    not on his word (a session's own, another hub's): the bus is not a chat with the owner. `in_owner_turn` — the source
    wrote that request in a turn of the owner's (owner_turn): its turn on the answer is an answer to him."""
    th = (bus.get("threads") or {}).get(thread or "")
    if not th or th.get("contact") != contact or now - th.get("ts", 0) > THREAD_TTL or th.get("n", 0) > THREAD_MAX:
        return None
    return th


def auto_thread_ok(bus, thread, contact, session, now):
    """May the core put a session's words to this contact in this thread by itself (spec d2423478): the thread is
    alive and takes one more message with room for the answer (under THREAD_MAX), it is not the owner's own, and its
    answers would come back to this session — its source is the session or nobody yet."""
    th = live_thread(bus, thread, contact, now)
    return bool(th and th.get("n", 0) < THREAD_MAX and not th.get("owner") and th.get("source") in (None, session))


def reply_thread(st, session, contact, now):
    """The thread a session's request to the liaison continues by itself (spec d2423478): the one of another hub's
    words its turn is on (words of that hub from one thread only — from two, which one it answers is unknown), else
    the latest thread that hub's words came to the session in; None — a new thread, as before."""
    from core import chain, reducer
    bus = st.get("bus") or {}
    sess = (st.get("sessions") or {}).get(session) or {}
    inbox = bus.get("inbox") or {}
    # the turn it runs now (the head of a finished turn stays on the session: not this request's)
    marks = reducer._took(sess) if sess.get("state") in chain.BUSY else []
    took = [((st.get("messages") or {}).get(m) or {}).get("hubbus") or {} for m in marks]
    turn = {inbox[h["inbox"]].get("thread") for h in took
            if h.get("inbox") in inbox and inbox[h["inbox"]].get("contact") == contact}
    turn.discard(None)
    if len(turn) == 1 and auto_thread_ok(bus, next(iter(turn)), contact, session, now):
        return next(iter(turn))
    # the latest by the time it was handed over, then by arrival (bN carries the seq): equal times keep the order
    got = [(x.get("done") or x.get("ts") or 0, int(k[1:]) if k[1:].isdigit() else 0, x["thread"])
           for k, x in inbox.items() if x.get("contact") == contact and x.get("thread")
           and session in (x.get("passed_to") or [])]
    last = max(got, default=None)
    return last[2] if last and auto_thread_ok(bus, last[2], contact, session, now) else None


def owner_turn(st, name):
    """The session `name` runs a turn of the owner's now: on his words to it, a later turn of such a turn without a
    marker (owner_chain: a turn on an answer marked so too), or one on his own task (no session set it: an executor of
    a mini-hub reports to the mini-hub); not one of a recurring schedule firing (not his live word, chain.scheduled).
    An answer in the thread of a request it sends now is an answer to him (owner decision 18.11: "Answered you", as an
    executor's answer to his task) — never his word: no from_owner, re or answers_question rides with it."""
    from core import chain
    sess = (st.get("sessions") or {}).get(name or "") or {}
    if sess.get("state") not in chain.BUSY or chain.turn_scheduled(st, name):
        return False
    d = (st.get("delegations") or {}).get(chain.turn_task(st, name) or "") or {}
    return bool(sess.get("turn_owner") or sess.get("owner_chain") or sess.get("turn_bus_owner")
                or (d.get("session") == name and d.get("for") and not d.get("for_session")))


def asked_in_owner_turn(st, data):
    """A session's words to another hub (to the liaison, or straight) written in a turn of the owner's: by the session
    itself, not by a schedule job's script on its behalf (not from its turn, core/chain.py); never the liaison's."""
    from core import liaison
    sender = data.get("sender")
    return (data.get("channel") == "session" and sender != liaison.NAME and not data.get("schedule_job")
            and owner_turn(st, sender))


def whose(o):
    """The session an outgoing message is for: the one whose words it carries, else the one whose request the liaison
    answered with its own words (`asker`, set by the core from the liaison's turn, not by the model). None — nobody."""
    return o["source"] if o.get("source") not in ("liaison", "owner") else o.get("asker")


def _turn_words(st):
    """Whose words the liaison's turn is on, as the core sees them: [(message, "owner" | a session's name)] for the
    messages the turn took; None when the turn has a message from the bus (another hub's words), one of someone
    else (a module, the core) or none at all. Read from the turn, never from the liaison's memory."""
    from core import forms, liaison, reducer
    sess = (st.get("sessions") or {}).get(liaison.NAME) or {}
    # the turn's head and what it took, and the marker of the prompt inserted mid-turn (the owner's `--now`, a watchdog's
    # letter) — unless it is the core's own notice: that is not words. What a hook refused the model did not read
    marks = reducer._took(sess)
    marker = sess.get("turn_marker")
    if marker and marker not in marks and not forms.is_core((st.get("messages") or {}).get(marker)):
        marks.append(marker)
    out = []
    for mark in marks:
        msg = (st.get("messages") or {}).get(mark)
        if msg and msg.get("state") == "failed":
            continue
        frm = forms.sender(msg) if msg else None
        if not msg or msg.get("channel") == "hubbus":
            return None
        if msg.get("from_owner"):
            out.append((msg, "owner"))
        elif frm and frm.get("kind") == "session" and frm.get("name") and frm["name"] != liaison.NAME:
            out.append((msg, frm["name"]))
        else:
            return None
    return out or None


def _asker(st, name):
    """The session on whose message the liaison's turn is now (its words are asked for by that session: «ask them…»):
    only when the turn's messages come from one session of this hub and none from the bus or the owner, and the message
    named no other contact."""
    words = _turn_words(st)
    if not words or any(who == "owner" or msg.get("bus_to") not in (None, name) for msg, who in words):
        return None
    asked = {who for _, who in words}
    return next(iter(asked)) if len(asked) == 1 else None


def _owner(data):
    """Strictly the owner: owner=true on the page, in the terminal outside sessions or in his Telegram. Not a module
    handler's mod:<name> channel (guards.from_owner lets it through for module requests): the bus is not a module's."""
    from core import guards
    return guards.from_owner_strict(None, data)


def _liaison(st, data):
    from core import liaison
    return liaison.from_liaison(st, data)


OWNER_KINDS = ("bus.on_requested", "bus.off_requested", "bus.contact_requested", "bus.relay_requested",
               "bus.link_requested", "bus.link_revoke_requested", "bus.join_requested", "bus.join_cancel_requested",
               "bus.chat_requested", "bus.setup_step", "bus.reset_requested", "bus.name_requested",
               "bus.mode_requested")
# the steps of «make the bus work» an owner's process reports (core/busflow.Flow.ensure): the page shows the progress,
# hub doctor the last failure
SETUP_STEPS = ("ssh", "lib", "selfcheck", "keys", "card", "relay")
SETUP_STATES = ("run", "ok", "fail", "skip")
# what a transport says: an envelope offered or handed (S1); a job of the relay done, the permits of our links on the
# relay, a contact's version the relay heard, the relay's own version (L0)
TRANSPORT_KINDS = ("bus.offered", "bus.handed", "bus.job_done", "bus.invites_seen", "bus.peer_seen", "bus.relay_seen")
TRANSPORT_SRC = "bus-relay"     # the built-in relay transport, a part of hub-up (modules/bus_relay)
TRANSPORT_RIGHT = "bus.transport"   # the Module API right of a transport module (core/daemon checks it before writing)
KNOCK_HUB = 3600                # «an unknown hub knocks»: one notice an hour for the whole hub
KNOCK_FP = 86400                # and one a day for a fingerprint; the others are counted into the next notice


def transport_refusal(ev, data):
    """Who hands and offers envelopes: the owner's terminal, the built-in relay transport, a module with the right
    (named by the core: channel plugin, sender mod:<name> — the daemon refused a module without the right)."""
    if _owner(data) is None:
        return None
    if ev.get("src") == TRANSPORT_SRC and not ({"channel", "sender", "owner"} & set(data)):
        return None
    sender = data.get("sender")
    if data.get("channel") == "plugin" and isinstance(sender, str) and sender.startswith("mod:"):
        return None
    return T("core.busstate.transport_who", "envelopes are handed to the bus by a transport: the owner's terminal, the "
                                            "built-in relay transport or a module with the right {right}"
             ).format(right=TRANSPORT_RIGHT)
OFFER_MAX = 3   # invitations waiting for the owner's press at once: a session cannot bury him in cards
LIAISON_KINDS = ("bus.send_requested", "bus.pass_requested", "bus.decline_requested", "bus.done_requested", "bus.note")


def rules_for(bus, contact):
    """The rules about this hub and about every hub («*»), as lines: the liaison reads them with each message of the hub."""
    return [r["text"] for r in (bus.get("rules") or []) if r.get("contact") in (contact, "*")]


def _turn_origin(st):
    """Whose words the liaison's turn is on: the owner and/or sessions of this hub — by the messages the turn took, as
    the core sees them; None when the turn has a message from the bus (another hub's words) or no message at all. A rule
    is written only from such a turn: another hub cannot teach the liaison how to treat it."""
    words = _turn_words(st)
    return ", ".join(sorted({who for _, who in words}))[:80] if words else None


def _rule(st, ev, data):
    """`hub bus rule`: the liaison (from a turn on the owner's or a session's words) or the owner writes down or removes a
    rule about a hub; the rules are the liaison's memory, kept in the core and read back with each message of that hub."""
    bus = _bus(st)
    if not bus.get("enabled"):
        return BUS_OFF
    by = None
    if _liaison(st, data):
        by = _turn_origin(st)
        if not by:   # also removing: another hub's words must not take the owner's rules away
            return T("core.busstate.rule_origin", "rule_origin: rules change on the word of the owner or a session of "
                                                  "the hub, not from another hub's incoming")
    elif _owner(data):
        return T("core.busstate.rules_who", "the hubs' rules are kept by the liaison or the owner")
    else:
        by = "owner"
    rules = bus.setdefault("rules", [])
    if data.get("op") == "remove":
        keep = [r for r in rules if r.get("id") != data.get("id")]
        if len(keep) == len(rules):
            return T("core.busstate.no_rule", "no rule {id}").format(id=data.get("id"))
        bus["rules"] = keep
        return _last(bus, {"of": ev["seq"]}, "ok")
    if data.get("op") != "add":
        return T("core.busstate.rule_op", "rule: add or remove")
    contact, text = data.get("contact"), " ".join(str(data.get("text") or "").split())
    if contact != "*" and contact not in bus["contacts"]:
        return NO_CONTACT.format(name=contact)
    if not text or len(text) > RULE_TEXT:
        return T("core.busstate.rule_len", "a rule is 1 to {most} characters").format(most=RULE_TEXT)
    if len(rules) >= RULES_MAX:
        return T("core.busstate.rules_full", "there are already {most} rules — remove some (hub bus rule rm)"
                 ).format(most=RULES_MAX)
    rules.append({"id": f"r{ev['seq']}", "contact": contact, "text": text, "by": by, "ts": ev["ts"]})
    return _last(bus, {"of": ev["seq"]}, "ok")


MODES = ("secretary", "remote")


def owners(st):
    """The hubs that run this one (spec d2505378, part D, remote): in the mode `remote` — nobody at this hub's page — every known hub
    with the pair code checked is its owner; their words go to the hub session as the owner's task. Otherwise none.
    The mode is the owner's (strict: the page, his terminal) and lives in the core's state, not in hub.json any
    session could write."""
    b = st.get("bus") or {}
    if b.get("mode") != "remote" or not b.get("enabled"):
        return []
    return sorted(n for n, c in (b.get("contacts") or {}).items()
                  if c.get("status") == "known" and c.get("verified") and not c.get("quarantine"))


def remote_form(st, sess):
    """The refusal of a form on a hub nobody sits at (mode `remote`, spec d2505378, part D, remote): None in any other mode; else
    whom to ask instead — the owner whose words this turn is on, in that thread, first. A Telegram group bound (the bot's
    fact telegram.group, spec d2513429) has the owner's people in it: the form is their card — except on a turn of a
    linked owner's words, whose frame (liaison.OWNED_DO) has the question go to him in his thread, and the group does
    not see his task."""
    from core import liaison, reducer
    b = st.get("bus") or {}
    if b.get("mode") != "remote" or not b.get("enabled"):
        return None
    group, who = bool(st.get("telegram_group")), owners(st)
    if not who:
        return None if group else liaison.FORM_NOBODY
    if liaison.is_liaison(st, sess.get("name")):
        return None if group else liaison.FORM_REMOTE_LIAISON.format(owners=", ".join(who))
    inbox, msgs = b.get("inbox") or {}, st.get("messages") or {}
    took = [inbox.get(((msgs.get(m) or {}).get("hubbus") or {}).get("inbox") or "") or {} for m in reducer._took(sess)]
    mine = next((i for i in took if i.get("contact") in who), {})
    if group and not mine:
        return None
    first = mine.get("contact") or who[0]
    return liaison.FORM_REMOTE.format(contact=first, thread=f" --thread {mine['thread']}" if mine.get("thread") else "",
                                      owners=", ".join(who))


def direct_refusal(st, name, owned=None):
    """Why a session's words to this contact go through the liaison, not straight from the core (spec d2505378, part A, outgoing):
    None — the core sends them itself. Straight only to the owner's own people: a known hub with the pair code checked,
    bound to a person of his contacts book (an unbound hub is «a new person» — the liaison's form decides its first
    message), and with no rule about it (a rule is the owner's word to the liaison, and the liaison reads it)."""
    b = st.get("bus") or {}
    c = (b.get("contacts") or {}).get(name or "")
    if not b.get("enabled"):
        return "bus_off"
    if name in (owners(st) if owned is None else owned):   # the hub's own owners: straight, whatever the rules
        return None
    if c is None or c.get("status") != "known":
        return "not_known"
    if not c.get("verified"):
        return "not_verified"
    if c.get("quarantine"):
        return "quarantine"
    if not (isinstance(c.get("person"), dict) and c["person"].get("id")):
        return "no_person"
    if rules_for(b, name):
        return "rules"
    return None


NOT_DIRECT = T("core.busstate.not_direct", "not_direct: {why} — words to {name} go through the liaison now: send them "
                                          "again, hub send liaison --to {name} \"…\"")
DIRECT_THREAD = T("core.busstate.direct_thread",
                  "thread_closed: thread {thread} does not take your words to {name} (another contact's, gone, full, "
                  "the owner's own or another session's) — send without --thread")


def _direct(st, ev, data, actions):
    """`hub send liaison --to <contact> "…"` to the owner's own hub: the core sends the session's words itself, the
    liaison is not woken (spec d2505378, part A, outgoing). Its own fact, not a message to the liaison: a core rolled back refuses the
    fact and does not hand the liaison requests the core has sent already. A refusal goes back to the session."""
    from core import chain, liaison
    sender = data.get("sender")
    if data.get("channel") != "session" or not sender or sender == liaison.NAME \
            or sender not in (st.get("sessions") or {}):
        return T("core.busstate.direct_who", "a session's words go straight only from a session of this hub")
    bus = _bus(st)
    ts = ev["ts"]
    name = data.get("contact")
    why = direct_refusal(st, name)
    if why == "bus_off":
        return BUS_OFF
    if why:
        return NOT_DIRECT.format(why=why, name=name)
    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        return EMPTY_TEXT
    if len(text.encode("utf-8")) > SEND_MAX:
        return T("core.busstate.send_too_big", "text over 60 KB — the bus has no attachments, only a text or a link")
    thread, auto = data.get("thread"), False
    if thread is not None:
        if not isinstance(thread, str) or not auto_thread_ok(bus, thread, name, sender, ts):
            return DIRECT_THREAD.format(thread=thread, name=name)
    else:
        thread = reply_thread(st, sender, name, ts)
        auto = thread is not None
    root, sched = chain.turn_root(st, sender), chain.turn_scheduled(st, sender)
    oid = f"o{ev['seq']}"
    bus["out"][oid] = {"kind": "message", "status": "requested", "contact": name, "fp": bus["contacts"][name]["fp"],
                       "source": sender, "basis": None, "via": data.get("via") if data.get("via") in ("local", "person")
                       else "local", "text": text, "agent_to": None, "thread": thread, "ts": ts, "of": ev["seq"],
                       "direct": True, **({"thread_auto": True} if auto else {}),
                       **({"root": root, **({"via_schedule": True} if sched else {})} if root else {}),
                       **({"in_owner_turn": True} if asked_in_owner_turn(st, data) else {})}
    actions.append({"do": "bus_send", "session": None, "out": oid, "of": ev["seq"]})
    return None


def fold(st, ev, data, actions):
    """Apply a bus fact: None, or the reason the core refuses it."""
    kind = ev["type"]
    if kind == "bus.rule_requested":
        return _rule(st, ev, data)
    if kind == "bus.direct_requested":
        return _direct(st, ev, data, actions)
    if kind == "bus.offer_requested":
        return _offer_request(st, ev, data, actions)
    if kind in OWNER_KINDS:
        why = _owner(data)
        if why:
            return T("core.busstate.owner_only", "only the owner changes the bus and the bus contacts ({why})"
                     ).format(why=why)
        return _request(st, ev, kind, data, actions)
    if kind in TRANSPORT_KINDS:
        why = transport_refusal(ev, data)
        if why:
            return why
        return _transport(st, ev, kind, data, actions)
    if kind in LIAISON_KINDS:
        if not _liaison(st, data):
            return T("core.busstate.not_liaison", "not_liaison: bus commands are for the liaison only (the system "
                                                  "session liaison); a session writes to it: hub send liaison --to "
                                                  "<contact> \"…\"")
        if not (st.get("bus") or {}).get("enabled"):
            return BUS_OFF
        return _from_liaison(st, ev, kind, data, actions)
    bus = _bus(st)
    ts = ev.get("ts") or 0
    if kind == "core.bus_enabled":
        bus.update(enabled=True, crypto_ready=True, failed=None,
                   me={**{k: data[k] for k in ("fp", "sign_pub", "enc_pub") if k in data},
                       "label": data.get("label") or (bus.get("me") or {}).get("label"), "lib": data.get("lib")})
        actions.append({"do": "bus_system", "session": None})   # the liaison comes (core/system.specs)
        return _last(bus, data, "ok")
    if kind == "core.bus_key_failed":
        bus.update(crypto_ready=False, failed={"code": data.get("code"), "reason": data.get("reason"), "ts": ts})
        return _last(bus, data, data.get("code"))
    if kind == "core.bus_request_failed":
        _offer_told(st, ev, next((k for k, r in (bus.get("offers") or {}).items()
                                  if r.get("state") == "taken" and r.get("take_of") == data.get("of")), None),
                    "failed", code=data.get("code"))   # a press that did not link
        return _last(bus, data, data.get("code") or "failed")
    if kind == "core.bus_contact_added":
        name = data.get("name")
        if not isinstance(name, str) or name in bus["contacts"]:
            return T("core.busstate.contact_exists", "a bus contact with this name already exists")
        bus["contacts"][name] = {
            **{k: data.get(k) for k in ("fp", "label", "sign_pub", "enc_pub", "relays", "agents", "bus_v")},
            "status": data.get("status") if data.get("status") in STATUSES else "pending",
            "verified": bool(data.get("verified")), "send": "ask", "person": None,
            "added": ts}
        intro = bus["intros"].pop(data.get("fp"), None)
        if intro:
            _drop_card(st, intro.get("card"))
        return _last(bus, data, "ok")
    if kind == "core.bus_contact_set":
        c = bus["contacts"].get(data.get("name"))
        if c is None or data.get("status") not in STATUSES:
            return NO_SUCH_CONTACT
        c["status"] = data["status"]
        if data.get("decided", True):   # the owner's decision on the status ends the quarantine; a change of rights does not
            c.pop("quarantine", None)
            bus["bad"].pop(c.get("fp"), None)
        first = data.get("verified") and not c.get("verified")
        if data.get("verified"):
            c["verified"] = True
        if first:
            _checked(st, ev, data["name"], c, data, actions)
        return _last(bus, data, "ok")
    if kind == "core.bus_contact_removed":
        c = bus["contacts"].pop(data.get("name"), None)
        if c is None:
            return NO_SUCH_CONTACT
        for part in ("rate", "bad"):
            bus[part].pop(c.get("fp"), None)
        bus["rules"] = [r for r in bus.get("rules") or [] if r.get("contact") != data.get("name")]   # a new hub under the name is new
        if c.get("fp"):   # the same hub met again (a new link after a removal) gets this local name back
            gone = bus.setdefault("gone", {})
            gone.pop(c["fp"], None)
            gone[c["fp"]] = data["name"]
            for old in list(gone)[:-GONE_MAX]:
                del gone[old]
        for t in [t for t, th in bus["threads"].items() if th.get("contact") == data.get("name")]:
            del bus["threads"][t]   # the key is forgotten, the thread is closed
        u = data.get("unlinked")
        if isinstance(u, dict):   # the other owner unlinked us: the page says «hub … unlinked you», the history stays
            bus["unlinked"][str(c.get("fp"))] = {"name": data["name"], "label": shown(c), "ts": ts}
        j = bus["joins"].get(data.get("join") or "")
        if j is not None:   # the contact a join made has gone with it: the join removes nothing more later
            j["made"] = False
        return _last(bus, data, "ok")
    if kind == "core.bus_contact_linked":
        c = bus["contacts"].get(data.get("name"))
        if c is None:
            return NO_SUCH_CONTACT
        p = data.get("person")
        c["person"] = {"id": str(p["id"]), "name": str(p.get("name") or "")} if isinstance(p, dict) and p.get("id") else None
        return _last(bus, data, "ok")
    if kind in LINK_FACTS:
        return _link_fact(st, ev, kind, data, actions)
    if kind in ("core.bus_received", "core.bus_chat_received", "core.bus_receipt_seen", "core.bus_rejected",
                "core.bus_delivered", "core.bus_card_updated"):
        if (kind == "core.bus_chat_received") != (data.get("for") == "owner") and kind in ("core.bus_received",
                                                                                          "core.bus_chat_received"):
            return T("core.busstate.chat_fact", "the owners' chat — only by its own fact core.bus_chat_received with "
                                                "for: owner")
        why = _inbound(bus, kind, data, ts)
        if why is None and kind == "core.bus_card_updated":
            return _card_updated(bus, data)
        if why is None and kind in ("core.bus_received", "core.bus_chat_received"):
            _received(st, ev, data)
        if why is None and kind in ("core.bus_receipt_seen", "core.bus_delivered"):
            _delivered(st, ev, data)
        return why
    if kind == "core.bus_send_held":
        return _held(st, ev, data)
    if kind == "core.bus_out":
        return _out(st, ev, data)
    if kind == "core.bus_send_refused":
        return _refused(st, ev, data.get("out"), data.get("code") or "refused", data)
    if kind == "core.bus_expired":
        return _expired(st, ev, data)
    if kind == "core.bus_passed":
        return _passed(st, ev, data)
    if kind == "core.bus_intro":
        return _intro(st, ev, data)
    if kind in ("core.bus_offer", "core.bus_offer_ended"):
        return _offer_fact(st, ev, kind, data, actions)
    if kind == "core.bus_stale":
        return _stale(st, ev, data)
    if kind == "core.bus_liaison_sleep":
        return _liaison_sleep(st, ev, actions)
    if kind == "core.bus_held_expired":   # the owner did not decide on a doubtful incoming item in a day: refused
        item = bus["inbox"].get(data.get("inbox") or "")
        if item is None or item.get("status") != "held" or item.get("outcome") is not None:
            return T("core.busstate.nothing_to_drop", "nothing to take off")
        _drop_card(st, item.pop("card", None))
        item.update(status="refused", outcome="declined", why="card_expired")
        actions.append({"do": "bus_receipt", "session": None, "inbox": data["inbox"], "status": "refused",
                        "code": "expired"})
        return None
    if kind == "core.bus_redeliver":   # the liaison is back: an item whose message did not reach it goes again
        item = bus["inbox"].get(data.get("inbox") or "")
        if item is None or item.get("outcome") is not None or item.get("status") != "ok":
            return T("core.busstate.nothing_to_redeliver", "nothing to pass on again")
        _to_liaison(st, ev, data["inbox"], item, bus["contacts"].get(item["contact"]) or {})
        return None
    return "unknown event type"


def _last(bus, data, code):
    """The outcome of the request for the command that waits on it (`of` — the request's number)."""
    if data.get("of") is not None:
        bus["last"] = {"of": data["of"], "code": code, **({"reason": data["reason"]} if data.get("reason") else {})}
    return None


# --- links and joins (spec hubbus/ux §4.2–4.5) ---------------------------------------------------------------------

LINK_FACTS = ("core.bus_link_made", "core.bus_link_used", "core.bus_link_ack_wanted", "core.bus_link_refused",
              "core.bus_link_misused", "core.bus_join", "core.bus_join_step", "core.bus_v2_unanswered")
CONTACT_KEYS = ("fp", "label", "sign_pub", "enc_pub", "relays", "agents", "bus_v")


def pasted(old):
    """A link pasted before cannot be pasted again while that join is open or done — nor after any end once our
    introduction went: each new introduction is a new nonce of ours, so a hub in the middle that refuses until the
    code suits it would get a try per paste (spec hubbus/ux §5.2). Ended before the introduction — pasted anew."""
    return old is not None and (old.get("state") in JOIN_OPEN + ("linked",) or bool(old.get("intro_id")))


def waiting_code(bus, c):
    """True while the pair code of contact `c` is yet to come: its first link is ours to answer for — we pasted it
    (a join of its hub is active) and the other hub's answer, which carries the code, has not come back."""
    return c.get("linked_at") is None and any(j.get("state") == "active" and j.get("fp_a") == c.get("fp")
                                              for j in (bus.get("joins") or {}).values())


def settle_pair(c, lid, made):
    """The pair id after the link `lid` is done for an existing contact. Both hubs decide by what both know — when each
    link was made, by its maker's clock as the link says (`made`), and its id — never by the order the answers came in
    here: the link made last is the pair (a new link replaces an old one, a late unlink of the old removes nothing),
    crossed links (A→B and B→A at once) end with the same one on both hubs; the same second — the smaller id."""
    old = c.get("pair")
    if not old or old == lid:
        return lid
    return lid if (_made(made), _desc(lid)) > (_made(c.get("pair_at")), _desc(old)) else old


def _made(t):
    """A link's time of making as compared: unknown (a record without it) — before any."""
    return t if type(t) is int else -1


def _desc(lid):
    """An id compared backwards: of two links of the same second the smaller id is «later»."""
    return tuple(-ord(ch) for ch in str(lid))


def _settle(c, lid, made, ts, sas):
    """A link of this contact is done here: the pair (settle_pair), its time and its code — the code shown is always
    the code of the link that is the pair; a link that lost leaves the pair, its time and its code as they were."""
    # a contact no link has finished yet (made by a paste) takes the first link done here, whichever made it: the pair
    # is always the latest-made of the links done — the same set on both hubs, so the same pair
    if c.get("linked_at") is None or settle_pair(c, lid, made) == lid:
        c.update(pair=lid, pair_at=_made(made))
        code = _sas(lid, sas)
        if code:
            c["sas"] = code
        else:
            c.pop("sas", None)
    c.update(linked_at=ts, via="link")


def _sas(lid, code):
    """The pair code of the link `lid` as a contact keeps it (bus.contact_code shows it while `lid` is the pair)."""
    from core import bus as buslib
    return {"id": lid, "code": code} if isinstance(code, str) and buslib.SAS.fullmatch(code) else None


def _link_contact(bus, name, info, lid, made, ts, done, sas=None, person=None):
    """A contact by a link — the table «an existing contact» (§4.5): none — known, not checked, via the link; pending (the
    old way, the same hub) — known, the check and the rights stay; known, muted, quarantined — only the pair changes;
    the name of another hub — a suffix. (local name, made anew). A blocked hub never comes here: the core refuses first."""
    old, c = by_fp(bus, info.get("fp"))
    code = _sas(lid, sas) if done else None
    if c is None:
        if not isinstance(name, str) or not NAME.fullmatch(name) or name in bus["contacts"]:
            from core import bus as buslib
            name = buslib.local_name(info.get("label"), set(bus["contacts"]), info.get("fp"), bus.get("gone"))
        bus["contacts"][name] = {**{k: info.get(k) for k in CONTACT_KEYS}, "status": "known", "verified": False,
                                 "send": "ask", "person": person, "added": ts,
                                 "via": "link", "pair": lid, "pair_at": _made(made),
                                 "linked_at": ts if done else None,
                                 **({"sas": code} if code else {})}
        bus["unlinked"].pop(str(info.get("fp")), None)
        (bus.get("gone") or {}).pop(str(info.get("fp")), None)
        intro = bus["intros"].pop(info.get("fp"), None)
        return name, True, intro
    if c.get("status") == "pending":
        c["status"] = "known"
    if person and not c.get("person"):   # whose hub it is, said when linking: only where the owner has said nothing yet
        c["person"] = person
    if done:   # linked now (not merely pasted): the pair may change; both pages show its code, «Matches» needs no typing
        _settle(c, lid, made, ts, sas)
    if type(info.get("bus_v")) is int and info["bus_v"] > (c.get("bus_v") or 0):
        c["bus_v"] = info["bus_v"]
    return old, False, None


def _link_fact(st, ev, kind, data, actions):
    bus = _bus(st)
    ts = ev["ts"]
    if kind == "core.bus_v2_unanswered":
        c = bus["contacts"].get(data.get("name") or "")
        if c is None:
            return NO_SUCH_CONTACT
        o = bus["out"].get(data.get("out") or "")
        if o is not None:
            o["unanswered"] = True
        m = c.get("v2_miss") or {}
        c["v2_miss"] = {"n": m.get("n", 0) + 1, "seq": ev["seq"]}
        c.pop("peer", None)   # asked again (transport_view: peers_now)
        c["peer_ask"] = ev["seq"]
        return None
    if kind == "core.bus_link_made":
        lid = data.get("id")
        if not isinstance(lid, str) or lid in bus["links"]:
            return T("core.busstate.link_exists", "such a link already exists")
        bus["links"][lid] = {"pub": data.get("pub"), "exp": data.get("exp"), "invite": data.get("invite"),
                             "nonce": data.get("nonce"), "made_at": data.get("made_at"), "state": "open", "ts": ts,
                             "posted": not data.get("invite"), **({"label": data["label"]} if data.get("label") else {}),
                             **({"person": _person_of(data)} if _person_of(data) else {})}
        if data.get("invite"):   # the permit goes to the relay only now: the record is first, there is no orphan
            _job(bus, f"i{lid[:16]}", "invite", ts, link=lid)
        return _last(bus, data, "ok")
    if kind in ("core.bus_link_used", "core.bus_link_ack_wanted", "core.bus_link_refused", "core.bus_link_misused"):
        rec = bus["links"].get(data.get("id") or "")
        if rec is None:
            return NO_LINK
        if kind == "core.bus_link_misused":
            rec["misused"] = data.get("fp")
            return None
        if kind == "core.bus_link_refused":
            if data.get("code") == "expired" and rec.get("state") == "open":
                rec.update(state="expired", done=ts)
                actions.append({"do": "bus_link_files", "session": None})
            return None
        if kind == "core.bus_link_ack_wanted":
            if rec.get("state") != "used" or rec.get("used_fp") != data.get("fp"):
                return T("core.busstate.link_not_ours", "the link is not this hub's")
            rec["ack"] = {"fp": data["fp"], "intro": data.get("intro")}
            return None
        if rec.get("state") != "open":   # core.bus_link_used: open → used and the contact, one fact
            return LINK_ALREADY.format(state=rec.get("state"))
        info = data.get("card") or {}
        if info.get("fp") != data.get("fp"):
            return T("core.busstate.card_not_ours", "the card is not this hub's")
        _, other = by_fp(bus, data.get("fp"))
        if other is not None and other.get("status") == "blocked":
            return T("core.busstate.hub_blocked", "the hub is blocked by the owner")
        name, made, intro = _link_contact(bus, data.get("name"), info, data["id"], rec.get("made_at"), ts, True,
                                          data.get("sas"), person=rec.get("person"))
        if intro:
            _drop_card(st, intro.get("card"))
        rec.update(state="used", used_fp=data["fp"], used_at=ts, name=name, intro=data.get("intro"),
                   ack={"fp": data["fp"], "intro": data.get("intro")})
        actions.append({"do": "bus_link_files", "session": None})
        bus["jobs"].pop(f"i{data['id'][:16]}", None)
        if rec.get("invite") and rec.get("posted"):   # B came: the permit is not needed (used — the relay says so)
            _job(bus, f"r{data['id'][:16]}", "revoke", ts, link=data["id"], invite=rec["invite"])
        return None
    if kind == "core.bus_join":
        jid = data.get("id")
        if not isinstance(jid, str) or not isinstance(data.get("fp_a"), str):
            return T("core.busstate.join_no_id", "a join without an id")
        old = bus["joins"].get(jid)
        if pasted(old):
            return T("core.busstate.link_pasted", "this link is already pasted")
        bus["joins"][jid] = {"fp_a": data["fp_a"], "label": data.get("label"), "exp": data.get("exp"), "step": "parsed",
                             "state": "active", "ts": ts, "stepped": ts, "card": data.get("card") or {},
                             "relay": data.get("relay") if isinstance(data.get("relay"), dict) else None,
                             "commit": data.get("commit"), "made_at": data.get("made_at"),
                             **({"person": _person_of(data)} if _person_of(data) else {})}
        actions.append({"do": "bus_join_next", "session": None, "id": jid})
        return _last(bus, data, "ok")
    # core.bus_join_step
    j = bus["joins"].get(data.get("id") or "")
    if j is None:
        return T("core.busstate.no_join", "no such join")
    step = data.get("step")
    if step in ("linked", "refused"):
        if j.get("state") not in JOIN_OPEN:
            return JOIN_ALREADY.format(state=j.get("state"))
        if step == "refused":
            j.update(state="refused", code=str(data.get("code") or "refused")[:32], done=ts)
            actions.append({"do": "bus_join_drop", "session": None, "id": data["id"]})
            _offer_told(st, ev, data["id"], "failed", code=j["code"])
            return None
        _, c = by_fp(bus, j["fp_a"])
        if c is not None:
            _settle(c, data["id"], j.get("made_at"), ts, data.get("sas"))
        cancelling = j.get("state") == "cancelling"
        j.update(step="linked", state="linked", done=ts)
        if cancelling:   # the owner cancelled after the introduction went: now the ordinary unlink (§10)
            j["state"] = "cancelled"
            _offer_told(st, ev, data["id"], "cancel")
            actions.append({"do": "bus_join_drop", "session": None, "id": data["id"], "unlink": True})
        else:
            _offer_told(st, ev, data["id"], "linked", name=j.get("name"))
        return None
    if step == "expired":   # no answer in the term and an hour: the page says so; a late ack still links
        if j.get("state") == "cancelling":
            j.update(state="cancelled", done=ts)
            actions.append({"do": "bus_join_drop", "session": None, "id": data["id"], "unlink": True})
            _offer_told(st, ev, data["id"], "cancel")
            return None
        if j.get("state") != "active":
            return T("core.busstate.join_not_active", "the join is not in progress")
        j.update(state="expired", done=ts)
        _offer_told(st, ev, data["id"], "failed", code="expired", soft=True)   # a late answer of A still links
        bus["jobs"].pop(f"j{data['id'][:16]}", None)
        if not j.get("intro_id"):   # A never heard of us: nothing can come back — the contact and the secret go now
            actions.append({"do": "bus_join_drop", "session": None, "id": data["id"]})
        else:
            actions.append({"do": "bus_link_files", "session": None})
        return None
    if step == "forget":
        if j.get("state") != "expired":
            return T("core.busstate.join_not_expired", "the join has not expired")
        j.update(state="forgotten", done=ts)
        actions.append({"do": "bus_join_drop", "session": None, "id": data["id"]})
        return None
    if j.get("state") != "active":
        return JOIN_ALREADY.format(state=j.get("state"))
    if step == "joining":   # the registration on the link's relay goes to the transport (its secret is the daemon's file)
        if j.get("step") != "parsed" or not j.get("relay"):
            return T("core.busstate.join_wrong_step", "the registration is not at this step")
        _job(bus, f"j{data['id'][:16]}", "join", ts, join=data["id"])
        return None
    if step == "joined" and j.get("step") == "parsed":
        if j.get("relay") and data.get("relay"):   # registered on the link's relay: the hub's relay from now on
            bus["relay"] = {**{k: j["relay"].get(k) for k in ("url", "key", "ref", "invite", "by")}, "ts": ts}
    elif step == "contact" and j.get("step") == "joined":
        info = j.get("card") or {}
        _, other = by_fp(bus, j["fp_a"])
        if other is not None and other.get("status") == "blocked":
            j.update(state="refused", code="blocked", done=ts)
            _offer_told(st, ev, data["id"], "failed", code="blocked")
            return None
        name, made, intro = _link_contact(bus, data.get("name"), info, data["id"], j.get("made_at"), ts, False,
                                           person=j.get("person"))
        if intro:
            _drop_card(st, intro.get("card"))
        j.update(name=name, made=made)
    else:
        return T("core.busstate.join_step_order", "step {step} never follows {prev}").format(step=step,
                                                                                             prev=j.get("step"))
    j.update(step=step, stepped=ts)
    actions.append({"do": "bus_join_next", "session": None, "id": data["id"]})
    return None


def _card_updated(bus, data):
    """A contact's new name and agents catalog: applied once the owner checked its pair code; before that kept aside
    (shown nowhere) and applied at the check — the other hub sends it once."""
    c = bus["contacts"].get(data.get("contact") or "")
    if c is None:
        return NO_SUCH_CONTACT
    card = {"label": data.get("label"), "agents": data.get("agents")}
    if not c.get("verified"):
        c["card_pending"] = card
        return None
    _apply_card(c, card)
    return None


def _apply_card(c, card):
    if label_ok(card.get("label")):
        c["label"] = card["label"]
    if isinstance(card.get("agents"), dict):
        c["agents"] = {str(k): str(v) for k, v in card["agents"].items()}


def _checked(st, ev, name, c, data, actions):
    """The owner checked the pair code for the first time: what it sent for the agents while not checked goes on now —
    to the liaison (a doubtful one marked as such), with the receipt `ok`."""
    bus = _bus(st)
    if isinstance(c.get("card_pending"), dict):   # the catalog it sent before the check
        _apply_card(c, c.pop("card_pending"))
    for bid, item in sorted(bus["inbox"].items()):
        if item.get("contact") != name or item.get("status") != "unverified" or item.get("outcome") is not None:
            continue
        item["status"] = "ok"
        _to_liaison(st, ev, bid, item, c)
        actions.append({"do": "bus_receipt", "session": None, "inbox": bid, "status": "ok"})


def _relay_fact(st, ev, kind, data, actions):
    """What the transport learned from the relay: a job done (or refused for good), the permits of our links, the
    version the relay heard from a contact, the relay's own version. Shown and used, never a decision about trust."""
    bus = _bus(st)
    ts = ev["ts"]
    if kind == "bus.relay_seen":
        v = data.get("bus_v")
        if not (type(v) is int and 1 <= v <= 99) or not bus.get("relay"):
            return T("core.busstate.relay_seen", "bus.relay_seen: relay version")
        bus["relay"]["bus_v"] = v
        return None
    if kind == "bus.peer_seen":
        v = data.get("bus_v")
        _, c = by_fp(bus, data.get("fp"))
        if c is None or not (v is None or type(v) is int and 1 <= v <= 99):
            return T("core.busstate.peer_seen", "bus.peer_seen: no such contact")
        c["peer"] = {"v": v or 1, "seq": ev["seq"], "ts": ts}
        c.pop("peer_ask", None)
        if (v or 1) >= 2:   # the relay heard v2 from that hub after the misses: it was away, not rolled back
            c.pop("v2_miss", None)
        return None
    if kind == "bus.invites_seen":
        items = data.get("items")
        if not isinstance(items, list) or len(items) > 100:
            return T("core.busstate.invites_seen", "bus.invites_seen: list")
        by_invite = {r.get("invite"): r for r in bus["links"].values() if r.get("invite")}
        for x in items:
            rec = by_invite.get(x.get("id")) if isinstance(x, dict) else None
            if rec is not None and isinstance(x.get("used_by"), str) and not rec.get("invite_used"):
                rec["invite_used"] = {"fp": x["used_by"], "ts": ts}
        return None
    job = bus["jobs"].pop(data.get("job") or "", None)   # bus.job_done
    if job is None:
        return T("core.busstate.no_job", "no such relay job")
    code = data.get("code")
    code = str(code)[:32] if code else None
    if job["do"] == "invite":
        rec = bus["links"].get(job.get("link") or "")
        if rec is not None:
            if code is None:
                rec["posted"] = True
            elif rec.get("state") == "open":   # the relay refused the permit for good (a quota): the link is useless
                rec.update(state="failed", code=code, done=ts)
                actions.append({"do": "bus_link_files", "session": None})
    elif job["do"] == "revoke":
        rec = bus["links"].get(job.get("link") or "")
        if rec is not None:
            rec["revoked_relay"] = code or "ok"
    elif job["do"] == "join":
        j = bus["joins"].get(job.get("join") or "")
        if j is not None and j.get("state") == "active" and j.get("step") == "parsed":
            if code is None:
                bus["relay"] = {**{k: (j.get("relay") or {}).get(k) for k in ("url", "key", "ref", "invite", "by")},
                                "ts": ts}
                j.update(step="joined", stepped=ts)
                actions.append({"do": "bus_join_next", "session": None, "id": job["join"]})
            else:
                j.update(state="refused", code=code, done=ts)
                actions.append({"do": "bus_join_drop", "session": None, "id": job["join"]})
    return None


# --- the owner's requests ------------------------------------------------------------------------------------------

def _request(st, ev, kind, data, actions):
    bus = st.get("bus") or {}
    if kind == "bus.on_requested":
        label = data.get("label")
        if label is not None and not label_ok(label):
            return T("core.busstate.label_bad",
                     "the hub label is a line up to 100 characters without control and invisible characters")
        from core import liaison
        other = (st.get("sessions") or {}).get(liaison.NAME)
        if other is not None and not other.get("liaison"):
            # the core does not adopt someone's session of the reserved name (system.returning would take it with its tools)
            return T("core.busstate.liaison_name_taken", "the name {name} is taken by an ordinary session — rename it "
                                                         "(hub session rename), then hub bus on"
                     ).format(name=liaison.NAME)
        actions.append({"do": "bus_on", "session": None, "of": ev["seq"], **({"label": label.strip()} if label else {})})
        return None
    if kind == "bus.name_requested":   # the hub's own name for the others: one setting, the owner's, not guessed
        name = data.get("name")
        if not isinstance(name, str) or not alias_ok(name.strip()):
            return HUB_NAME_BAD
        bus = _bus(st)
        bus["name"] = name.strip()
        if bus.get("me"):   # the card the bus makes from now on carries it
            bus["me"]["label"] = bus["name"]
        return _last(bus, {"of": ev["seq"]}, "ok")
    if kind == "bus.mode_requested":   # nobody at this hub's page: the linked hubs are its owners (spec d2505378, part D, remote)
        if data.get("mode") not in MODES:
            return T("core.busstate.mode_bad", "the liaison's mode is {modes}").format(modes=" or ".join(MODES))
        bus = _bus(st)
        if data["mode"] == "remote":
            bus["mode"] = "remote"
        else:
            bus.pop("mode", None)
        return _last(bus, {"of": ev["seq"]}, "ok")
    if kind == "bus.off_requested":
        _off(st, ev, actions)
        return _last(_bus(st), {"of": ev["seq"]}, "ok")
    if kind == "bus.setup_step":
        return _setup(_bus(st), ev, data)
    if kind == "bus.reset_requested":
        return _reset(st, ev, actions)
    if not bus.get("enabled"):
        return T("core.busstate.bus_off_first", "the bus is off: first hub bus on")
    if kind == "bus.relay_requested":
        return _relay(st, ev, data)
    if kind in ("bus.link_requested", "bus.link_revoke_requested", "bus.join_requested", "bus.join_cancel_requested",
                "bus.chat_requested"):
        return _link_request(st, ev, kind, data, actions)
    op, name = data.get("op"), data.get("name")
    if op not in OPS or not isinstance(name, str):
        return T("core.busstate.unknown_contact_op", "an unknown request about a bus contact")
    if op == "add":
        if not NAME.fullmatch(name):
            return CONTACT_NAME_BAD
        if name in bus.get("contacts", {}):
            return T("core.busstate.contact_named_exists", "bus contact {name} already exists").format(name=name)
        if not isinstance(data.get("card"), str):
            return T("core.busstate.no_card", "no hub card")
    elif op == "intro":   # the owner accepts (or blocks) a hub that introduced itself through a transport
        if not NAME.fullmatch(name):
            return CONTACT_NAME_BAD
        if data.get("fp") not in (bus.get("intros") or {}):
            return T("core.busstate.no_intro", "no such introduction")
        if data.get("status") not in ("known", "blocked"):
            return T("core.busstate.intro_status", "an introduction is accepted (known) or blocked (blocked)")
    elif name not in bus.get("contacts", {}):
        return T("core.busstate.no_contact_named", "no bus contact {name}").format(name=name)
    if op == "rename":   # the owner's own name for the contact; empty — back to what the other hub calls itself
        alias = data.get("alias")
        if not isinstance(alias, str) or (alias.strip() and not alias_ok(alias.strip())):
            return HUB_NAME_BAD
        if alias.strip():
            bus["contacts"][name]["alias"] = alias.strip()
        else:
            bus["contacts"][name].pop("alias", None)
        return _last(bus, {"of": ev["seq"]}, "ok")
    if op == "set" and data.get("status") is not None and data.get("status") not in STATUSES:
        return T("core.busstate.status_one_of", "status — one of: {statuses}").format(statuses=", ".join(STATUSES))
    code = data.get("code")
    if code is not None and not isinstance(code, str):
        return T("core.busstate.code_str", "the check code is a string")
    p = data.get("person")
    if op == "link" and p is not None and not (isinstance(p, dict) and isinstance(p.get("id"), str)
                                               and re.fullmatch(r"c[0-9a-f]{8}", p["id"])):
        return T("core.busstate.link_person", "a link is the id of a book record (c + 8 hex) and a name")
    if op == "expose":   # the owner no longer chooses what a hub sees (the liaison decides): an old request is taken, does nothing
        return _last(bus, {"of": ev["seq"]}, "ok")
    actions.append({"do": "bus_contact", "session": None, "of": ev["seq"], "op": op, "name": name,
                    **{k: data[k] for k in ("card", "status", "code", "fp") if isinstance(data.get(k), str)},
                    **({"person": p} if op == "link" else {})})
    return None


REFUSED_HINT = {"secret": T("core.busstate.hint_secret", "The text has something like a key or a password — remove it "
                                                         "(the hub does not send secret values) and send again."),
                "too_big": T("core.busstate.hint_too_big", "Text over {most} bytes — send it shorter or as a link."
                             ).format(most=SEND_MAX)}
NO_NAME = T("core.busstate.no_name", "no_name: this hub has no name — set it (Settings → External hubs, hub bus name); "
                                     "other hubs tell you apart by it")


def person_ok(p):
    """A record of the contacts book as a bus contact carries it: its id and name (the book is the owner's, the id is
    the book's own — `hub contacts link`, the page's «Link to a contact»)."""
    return isinstance(p, dict) and isinstance(p.get("id"), str) and re.fullmatch(r"c[0-9a-f]{8}", p["id"]) is not None \
        and isinstance(p.get("name"), str) and 0 < len(p["name"]) <= 100


def _person_of(data):
    p = data.get("person")
    return {"id": p["id"], "name": p["name"]} if person_ok(p) else None


def _link_request(st, ev, kind, data, actions):
    """The owner links hubs (spec hubbus/ux §4.2): makes a link, revokes it, pastes another hub's link (as a file in the
    core's folder — the link is a secret and never goes into the journal), cancels a join; writes to another owner
    (the owners' chat: his press is his word, no card, no liaison)."""
    from core import bus as buslib
    bus = _bus(st)
    if kind == "bus.link_requested":
        ttl = data.get("ttl", buslib.LINK_TTL)
        if ttl not in buslib.LINK_TTLS:
            return T("core.busstate.link_ttl", "the link term is 1 hour, 24 hours or 7 days (3600, 86400, 604800)")
        label = data.get("label")   # the name for this invitation alone; else the hub's own
        if label is not None and not (isinstance(label, str) and alias_ok(label.strip())):
            return T("core.busstate.invite_name_bad", "the hub name in the invitation is a line up to 100 characters "
                                                      "without control and invisible characters")
        if not bus.get("name"):   # the hub's own name is required; the field of the invitation only changes it
            return NO_NAME
        if data.get("person") is not None and not person_ok(data["person"]):
            return PERSON_BAD
        actions.append({"do": "bus_link", "session": None, "of": ev["seq"], "ttl": ttl,
                        **({"person": _person_of(data)} if _person_of(data) else {}),
                        **({"label": label.strip()} if label and label.strip() != bus.get("name") else {})})
        return None
    if kind == "bus.link_revoke_requested":
        rec = bus["links"].get(data.get("id") or "")
        if rec is None:
            return NO_LINK
        if rec.get("state") != "open":
            return LINK_ALREADY.format(state=rec.get("state"))
        rec.update(state="revoked", done=ev["ts"])
        actions.append({"do": "bus_link_files", "session": None})
        bus["jobs"].pop(f"i{data['id'][:16]}", None)   # a permit not recorded yet never goes to the relay now
        if rec.get("invite") and rec.get("posted"):
            _job(bus, f"r{data['id'][:16]}", "revoke", ev["ts"], link=data["id"], invite=rec["invite"])
        return _last(bus, {"of": ev["seq"]}, "ok")
    if kind == "bus.join_requested":
        if not bus.get("name"):
            return NO_NAME
        for k, ok in (("path", lambda v: isinstance(v, str) and v), ("sha256", lambda v: isinstance(v, str)
                                                                     and re.fullmatch(r"[0-9a-f]{64}", v))):
            if not ok(data.get(k)):
                return BAD_FIELD.format(kind="bus.join_requested", field=k)
        if data.get("person") is not None and not person_ok(data["person"]):
            return PERSON_BAD
        actions.append({"do": "bus_join", "session": None, "of": ev["seq"], "path": data["path"],
                        "sha256": data["sha256"], **({"person": _person_of(data)} if _person_of(data) else {})})
        return None
    if kind == "bus.join_cancel_requested":
        j = bus["joins"].get(data.get("id") or "")
        if j is None or j.get("state") not in ("active", "expired"):
            return T("core.busstate.no_active_join", "no such join in progress")
        if j.get("intro_id"):   # A may have taken it: wait for its answer to unlink, or for the term
            j.update(state="cancelling", done=ev["ts"])
        else:
            j.update(state="cancelled", done=ev["ts"])
            actions.append({"do": "bus_join_drop", "session": None, "id": data["id"]})
            _offer_told(st, ev, data.get("id"), "cancel")   # a cancel that waits for A's answer is told when it ends
        return _last(bus, {"of": ev["seq"]}, "ok")
    # bus.chat_requested
    name, text = data.get("contact"), data.get("text")
    contact = bus["contacts"].get(name or "")
    if contact is None:
        return NO_CONTACT.format(name=name)
    if contact.get("status") not in ("known", "muted"):
        return NOT_ACCEPTED.format(name=name, status=contact.get("status"))
    if not isinstance(text, str) or not text.strip():
        return EMPTY_TEXT
    if len(text.encode("utf-8")) > AUTO_MAX:
        return T("core.busstate.chat_too_big", "text over {most} bytes").format(most=AUTO_MAX)
    oid = f"o{ev['seq']}"
    if data.get("to_hub") is True:   # «Tell the hub»: an ordinary message to the other hub's liaison, the owner's own words
        if contact.get("status") != "known":
            return T("core.busstate.not_accepted_hub", "contact {name} is not accepted for messages to the hub "
                                                       "(status {status})"
                     ).format(name=name, status=contact.get("status"))
        if not contact.get("verified"):
            return NOT_VERIFIED.format(name=name)
        bus["out"][oid] = {"kind": "message", "status": "requested", "contact": name, "fp": contact["fp"],
                           "source": "owner", "press": True, "basis": None, "via": "local", "text": text,
                           "agent_to": None, "thread": None, "ts": ts_of(ev), "of": ev["seq"],
                           **({"confirm": True} if data.get("confirm") is True else {})}
        actions.append({"do": "bus_send", "session": None, "out": oid, "of": ev["seq"]})
        return None
    # the owners' chat of the first releases (a person to a person, class `chat`): the page, the terminal and Telegram
    # no longer ask for it, a journal of those days is folded as it was
    bus["out"][oid] = {"kind": "message", "status": "requested", "contact": name, "fp": contact["fp"], "source": "owner",
                       "for": "owner", "basis": None, "via": "local", "text": text, "agent_to": None, "thread": None,
                       "ts": ts_of(ev), "of": ev["seq"], **({"confirm": True} if data.get("confirm") is True else {})}
    actions.append({"do": "bus_send", "session": None, "out": oid, "of": ev["seq"]})
    return None


def ts_of(ev):
    return ev.get("ts") or 0


def _job(bus, jid, do, ts, **params):
    """A job of the relay for the transport (core/busd.py writes the transport view): a permit to record or drop, a
    registration by a link's permit. Its secret part, if any, is a file the daemon wrote; the state holds none."""
    bus["jobs"][jid] = {"do": do, "ts": ts, **params}


def _relay(st, ev, data):
    """The owner joined a relay (the terminal made the registration with the invitation, `hub bus relay join`) or left
    it: the relay's address and key — only from the invitation; the hub's card names it for the contacts."""
    from core import busrelay
    bus = _bus(st)
    op = data.get("op")
    if op == "join":
        try:
            inv = busrelay.read_invite(data.get("invite"), signed=False)[0]   # its signature is masked in the journal
        except busrelay.Error as e:
            return T("core.busstate.relay_invite", "relay invitation: {error}").format(error=str(e))
        bus["relay"] = {"url": inv["relay"], "key": inv["key"], "ref": busrelay.ref(inv["relay"], inv["key"]),
                        "invite": inv["id"], "by": inv["by"], "ts": ev["ts"]}
        return _last(bus, {"of": ev["seq"]}, "ok")
    if op == "leave":
        if not bus.get("relay"):
            return T("core.busstate.relay_none", "no relay is connected")
        bus["relay"] = None
        return _last(bus, {"of": ev["seq"]}, "ok")
    return T("core.busstate.relay_op", "a relay request: join or leave")


def _setup(bus, ev, data):
    """A step of «make the bus work» (core/busflow.Flow.ensure) as the owner's process saw it: the last run, step by
    step — a run starts again with the first step."""
    step, state = data.get("step"), data.get("state")
    if step not in SETUP_STEPS or state not in SETUP_STATES:
        return T("core.busstate.setup_step", "a bus setup step: step — one of {steps}, state — one of {states}"
                 ).format(steps=", ".join(SETUP_STEPS), states=", ".join(SETUP_STATES))
    for key, most in (("code", 32), ("reason", 300), ("command", 500)):
        v = data.get(key)
        if v is not None and not (isinstance(v, str) and len(v) <= most and v.isprintable()):
            return T("core.busstate.setup_field", "{key} — a line up to {most} characters without control characters"
                     ).format(key=key, most=most)
    ts = ts_of(ev)
    setup = bus.get("setup") if isinstance(bus.get("setup"), dict) else None
    if setup is None or step == SETUP_STEPS[0]:
        setup = {"started": ts, "steps": {}}
    setup["steps"][step] = {"step": step, "state": state, "ts": ts,
                            **{k: data[k] for k in ("code", "reason", "command") if data.get(k)}}
    setup["ts"] = ts
    bus["setup"] = setup
    return None


def _reset(st, ev, actions):
    """`hub bus reset` (spec hubbus/ux §10): the bus goes off as with `hub bus off`, then the identity goes — the
    contacts, links, joins, the relay and the keys (the daemon removes the key files; the fingerprint is remembered as
    retired, so keys a cut left on disk are never taken back by the next `hub bus on`)."""
    b = _bus(st)
    old = (b.get("me") or {}).get("fp")
    _off(st, ev, actions)
    fresh = empty()
    fresh["retired"] = ([*(b.get("retired") or []), old] if old else list(b.get("retired") or []))[-20:]
    # the signing key of a retired identity: a reset cut after the exchange key went leaves only this one on disk
    sign = (b.get("me") or {}).get("sign_pub")
    fresh["retired_sign"] = ([*(b.get("retired_sign") or []), sign] if sign else list(b.get("retired_sign") or []))[-20:]
    fresh["reset"] = ts_of(ev)
    st["bus"] = fresh
    actions.append({"do": "bus_reset", "session": None, "fp": old})
    return _last(fresh, {"of": ev["seq"]}, "ok")


OFF_REQUEST = T("core.busstate.off_request", "The hub bus is turned off: your request to the liaison {id} (contact "
                                             "{contact}) was not sent and will not be.")


def _off(st, ev, actions):
    """`hub bus off` — one action of the core: (1) the bus stops taking and sending; (2) every incoming item and every
    request of a session without an outcome gets `declined: bus_off`, its source — a note; held outgoing is refused and
    the cards go; (3) the liaison leaves the system list and (4) falls asleep (core.bus_liaison_sleep, when free)."""
    from core import liaison, reducer
    b = _bus(st)
    b["enabled"] = False
    ts = ev["ts"]
    for bid, item in b["inbox"].items():
        if item.get("outcome") is None:
            item["outcome"] = "declined"
            item["why"] = "bus_off"
    told = set()
    for oid, o in b["out"].items():   # held or not yet decided: refused, its source told (and its request closed)
        if o.get("kind") == "message" and o.get("status") in ("requested", "held"):
            _refused(st, ev, oid, "bus_off")
            req = b["requests"].get(o.get("basis") or "")
            if req is not None:
                req["why"] = "bus_off"
    for mid, req in b["requests"].items():
        if req.get("outcome") is None:
            req["outcome"] = "declined"
            req["why"] = "bus_off"
            if req.get("session") not in told:
                told.add(req.get("session"))
                reducer.new_message(st, f"m{ev['seq']}r{len(told)}", req["session"],
                                    OFF_REQUEST.format(id=mid, contact=req.get("contact")), ts,
                                    origin=reducer.forms.ORIGIN_CORE)
    for item in b["inbox"].values():
        _drop_card(st, item.pop("card", None))
    for intro in b["intros"].values():
        _drop_card(st, intro.get("card"))
    b["intros"] = {}
    for lid, offer in (b.get("offers") or {}).items():
        _drop_card(st, offer.pop("card", None))
        if offer.get("state") == "open":
            offer.update(state="off", done=ts)   # not the owner's «No»: after `bus on` the invitation may come again
            _offer_told(st, ev, lid, "off")
    actions.append({"do": "bus_link_files", "session": None})   # the core's copies of the invitations go (runs with the bus off)
    actions.append({"do": "bus_system", "session": None})
    if liaison.NAME in (st.get("sessions") or {}) and not liaison.needed(st):   # the owner's chats keep it (core/chats.py)
        actions.append({"do": "bus_liaison_sleep", "session": None})


def _liaison_sleep(st, ev, actions):
    """The bus is off: the liaison falls asleep like `hub session close` — a running turn ends, the process goes, the
    conversation stays. The daemon asks for it while the liaison is free (idle, held, parked) and the bus is off."""
    from core import liaison, machines, reducer
    sess = (st.get("sessions") or {}).get(liaison.NAME)
    if liaison.needed(st) or not sess or not sess.get("liaison"):
        return T("core.busstate.liaison_no_sleep",
                 "the liaison is not put to sleep: the bus is on or there is no liaison")
    if liaison.NAME in (st.get("system") or []):
        return T("core.busstate.liaison_system", "the liaison is still in the list of system agents")
    if sess["state"] in ("idle", "held"):
        reducer._session_event(st, {"type": "_sleep", "entity": sess["name"], "ts": ev["ts"], "seq": ev["seq"]}, {},
                               actions)
    elif sess["state"] == "parked":
        sess["state"] = machines.step(machines.SESSION, "parked", "_unpark")[0]
    else:
        return T("core.busstate.liaison_busy", "the liaison is not free now")
    return None


# --- the transport -------------------------------------------------------------------------------------------------

def _transport(st, ev, kind, data, actions):
    bus = st.get("bus") or {}
    if not bus.get("enabled"):
        return BUS_OFF
    if kind == "bus.offered":
        for k, ok in (("path", lambda v: isinstance(v, str) and v), ("sha256", lambda v: isinstance(v, str)
                                                                     and re.fullmatch(r"[0-9a-f]{64}", v)),
                      ("size", lambda v: type(v) is int and v >= 0)):
            if not ok(data.get(k)):
                return BAD_FIELD.format(kind="bus.offered", field=k)
        actions.append({"do": "bus_offered", "session": None, "of": ev["seq"],
                        **{k: data[k] for k in ("path", "sha256", "size")}})
        return None
    if kind != "bus.handed":
        return _relay_fact(st, ev, kind, data, actions)
    o = bus["out"].get(data.get("out") or "")
    if o is None or o.get("status") not in ("signed", "handed"):
        return T("core.busstate.no_envelope", "no such envelope in the queue")
    if o.get("kind") in ONE_SHOT:
        del bus["out"][data["out"]]   # nothing is expected for them: handed — done
        return None
    o.update(status="handed", handed=ev["ts"], via=str(data.get("via") or "")[:40] or None)
    return None


# --- the liaison's requests ----------------------------------------------------------------------------------------

def _from_liaison(st, ev, kind, data, actions):
    bus = _bus(st)
    ts = ev["ts"]
    if kind == "bus.note":
        text = " ".join(str(data.get("text") or "").split())[:500]
        if not text:
            return T("core.busstate.empty_note", "an empty note")
        on = data.get("on")
        if on is not None and on not in bus["inbox"]:
            return NO_INBOX.format(id=on)
        bus["notes"] = (bus["notes"] + [{"seq": ev["seq"], "ts": ts, "contact": data.get("contact"), "text": text,
                                        **({"on": on} if on else {})}])[-NOTES_KEEP:]
        if on:
            bus["inbox"][on]["note"] = text
        return None
    if kind in ("bus.decline_requested", "bus.done_requested"):   # done: read, nothing to do — closed, nothing goes out
        decline = kind == "bus.decline_requested"
        key = data.get("inbox") or ""
        item = bus["inbox"].get(key) or (bus["requests"].get(key) if decline else None)   # a request is only declined
        if item is None:   # a session's request is closed by send or decline (decline tells the session), not done
            if key in bus["requests"]:
                return f"{key} is a session's request: done closes only an incoming bN, a request — decline"
            return T("core.busstate.no_inbox_or_request", "no incoming item or request {id}").format(id=data.get("inbox"))
        if item.get("outcome") is not None:
            return T("core.busstate.has_outcome", "{id} already has an outcome: {outcome}").format(
                id=data.get("inbox"), outcome=item["outcome"])
        if item.get("status", "ok") != "ok":
            return T("core.busstate.waits_owner", "{id} waits for the owner's decision ({status}) — not the liaison's"
                     ).format(id=data.get("inbox"), status=item.get("status"))
        why = " ".join(str(data.get("why") or "").split())[:300] or None
        item.update(outcome="declined" if decline else "read", why=why, done=ts)
        if decline:
            _decline_notice(bus, item, ev, actions)
        return None
    if kind == "bus.pass_requested":
        why = pass_refusal(st, data.get("inbox"), data.get("session"), ts)
        if why:
            return why
        actions.append({"do": "bus_pass", "session": None, "of": ev["seq"], "inbox": data["inbox"],
                        "to": data["session"], **({"note": str(data["note"])[:500]} if data.get("note") else {})})
        return None
    return _send_request(st, ev, data, actions)


# the words go to another hub (no marks travel there): plain English; a notice of a hub of an earlier release, and the
# items journals already hold, carry the earlier form — both are recognised
DECLINE_PREFIX = "Hub "
DECLINE_MIDDLE = " declined the request: "
DECLINE_NO_WHY = "no explanation"
DECLINE_OLD = langdata.legacy("decline_notice")   # {"prefix", "middle"}


def _decline_notice(bus, item, ev, actions):
    """The sender of a declined incoming request is told in the same thread: "Hub X declined the request: <why>" — by the
    core, so that it does not depend on the liaison's model (a hub of the previous release takes it as an ordinary answer in
    the thread). Nothing goes for an item with no thread or with another contact's thread id (the item does not take
    that thread over in _received), from a contact that is not known and checked, or for such a
    notice itself (two hubs must not decline each other's refusals for ever)."""
    contact = bus["contacts"].get(item.get("contact") or "") or {}
    thread = item.get("thread")
    if (not thread or contact.get("status") != "known" or not contact.get("verified") or is_decline_notice(item.get("text"))
            or (bus["threads"].get(thread) or {}).get("contact") != item.get("contact")):
        return
    from core import redact
    me = bus.get("me") or {}
    head = f"{DECLINE_PREFIX}{me.get('label') or bus.get('name') or ''}{DECLINE_MIDDLE}"
    text = head + (item.get("why") or DECLINE_NO_WHY)
    if redact.mask(text) != text:   # a key-like reason would be held back by the secrets check, and the sender would get nothing
        text = head + DECLINE_NO_WHY
    oid = f"o{ev['seq']}"
    bus["out"][oid] = {"kind": "message", "status": "requested", "contact": item["contact"], "fp": contact["fp"],
                       "source": "liaison", "basis": None, "via": None, "text": text, "agent_to": None,
                       "thread": thread, "ts": ev["ts"], "of": ev["seq"], "decline": True}
    actions.append({"do": "bus_send", "session": None, "out": oid, "of": ev["seq"]})


def is_decline_notice(text):
    if not isinstance(text, str):
        return False
    first = text.split("\n", 1)[0]
    return any(text.startswith(prefix) and middle in first
               for prefix, middle in ((DECLINE_PREFIX, DECLINE_MIDDLE), (DECLINE_OLD["prefix"], DECLINE_OLD["middle"])))


# --- the liaison's echo: "sent to the hub X", "passed to session Y" (reducer._merged_into) ---------------------------

ECHO_SENT = ("signed", "handed", "delivered")   # the core sealed it: gone out, whatever the other hub does after


ECHO_QUIET = ("session.gist", "bus.note")   # its line about itself, its own notes: nothing anybody is told


def echo_mark(st, ev):
    """What the liaison's turn asked of the core and the core took: an outgoing message and an item passed to a session
    (each decided by its own later fact) — or anything else, a decision of its own nobody else tells (`off`): closing or
    declining an item, a rule, a request in the owner's chats with people. Cleared at the start of its next turn."""
    from core import liaison
    sess = st["sessions"].get(liaison.NAME)
    kind, data = ev["type"], ev["data"]
    if sess is None or kind in ECHO_QUIET:
        return
    mark = sess.setdefault("turn_echo", {})
    if kind == "bus.send_requested":
        mark.setdefault("out", []).append(f"o{ev['seq']}")
    elif kind == "bus.pass_requested":
        # the item may have gone to that session before: this pass counts once it is there one more time
        had = ((st["bus"]["inbox"].get(data["inbox"]) or {}).get("passed_to") or []).count(data["session"])
        mark.setdefault("pass", []).append([data["inbox"], data["session"], had])
    else:
        mark["off"] = True


def echo_refused(st, data):
    """core.rejected of a request of the liaison: its turn had a refusal, and the result tells of it."""
    from core import liaison
    sess = st["sessions"].get(liaison.NAME)
    if sess is not None and data.get("sender") == liaison.NAME:
        sess.setdefault("turn_echo", {})["off"] = True


def echo_of(st, sess):
    """The sessions the liaison's turn only echoes — whose words it sent out, to which it passed an item — when the core
    sealed every message of the turn with no card to the owner, delivered every item, refused nothing, and the liaison
    neither asked the owner nor decided anything of its own; else []. Such a session tells the owner itself."""
    mark = sess.get("turn_echo") or {}
    if mark.get("off") or sess.get("turn_asks"):
        return []
    bus = st.get("bus") or {}
    out = [(bus.get("out") or {}).get(oid) or {} for oid in mark.get("out") or ()]
    if any(o.get("status") not in ECHO_SENT or o.get("approved") != "auto" or "finding" in o or not whose(o)
           for o in out):
        return []   # not gone yet, refused, the owner had a card on it, or the liaison's own words to nobody's request
    passed = mark.get("pass") or ()
    if any((((bus.get("inbox") or {}).get(bid) or {}).get("passed_to") or []).count(to) <= had for bid, to, had in passed):
        return []   # the core has not delivered it (yet), or refused
    return list(dict.fromkeys([w for w in map(whose, out) if w] + [to for _, to, _ in passed]))


def pass_refusal(st, bid, to, now):
    """Why the core does not pass an incoming item to this session (None — it may): to any live session of the hub but
    the liaison — the liaison chooses; nobody gets an item held for the owner or a muted one."""
    from core import liaison
    bus = st.get("bus") or {}
    item = (bus.get("inbox") or {}).get(bid or "")
    if item is None:
        return NO_INBOX.format(id=bid)
    if item.get("status") != "ok":
        return T("core.busstate.not_for_pass", "{id} is not for passing on: {status}").format(id=bid,
                                                                                             status=item.get("status"))
    sess = (st.get("sessions") or {}).get(to or "")
    if not sess or sess["state"] in ("removed", "closed", "failed") or to == liaison.NAME:
        return T("core.busstate.no_session", "no_session: no session {name}").format(name=to)
    contact = (bus.get("contacts") or {}).get(item["contact"]) or {}
    if contact.get("status") != "known":
        return T("core.busstate.contact_not_accepted", "contact {name} is not accepted").format(name=item["contact"])
    if not contact.get("verified"):   # the gate of the check: an answer in a thread too
        return T("core.busstate.not_verified_pass", "not_verified: hub {name} is not checked — nothing from it is "
                                                    "passed to agents").format(name=item["contact"])
    if item.get("for") == "owner":
        return T("core.busstate.for_owner_item", "{id} is an answer in the thread of your words; it is for you, not "
                                                 "for sessions").format(id=bid)
    return None


def _send_request(st, ev, data, actions):
    """hub bus send: the liaison asks to send the words of a session by reference (--text-from mN), or its own (--text);
    the core checks what the liaison cannot know or must not choose, and the daemon decides: a card to the owner or,
    for an allowed pair «source session × contact», straight out."""
    from core import chain, forms, liaison
    bus = _bus(st)
    ts = ev["ts"]
    name = data.get("contact")
    contact = bus["contacts"].get(name or "")
    if contact is None:
        known = ", ".join(sorted(n for n, c in bus["contacts"].items() if c.get("status") == "known"))
        return NO_CONTACT.format(name=name) + (T("core.busstate.known_contacts", " (there are: {names})"
                                                  ).format(names=known) if known else "")
    if contact.get("status") != "known":
        return NOT_ACCEPTED.format(name=name, status=contact.get("status"))
    if not contact.get("verified"):
        return NOT_VERIFIED.format(name=name)
    if "for" in data:
        return T("core.busstate.for_owner_field", "for: owner is the owner's word; only the owner's channel (the chat on "
                                                  "the page) sets it, not a session")
    thread = data.get("thread")
    if thread is not None:
        th = bus["threads"].get(thread)
        if th is None:
            return T("core.busstate.unknown_thread", "unknown_thread: no thread {thread}").format(thread=thread)
        if th.get("contact") != name:
            return T("core.busstate.wrong_thread", "wrong_contact: thread {thread} is open with {other}, not with "
                                                   "{name}").format(thread=thread, other=th.get("contact"), name=name)
    agent = data.get("agent")
    if agent is not None and not (isinstance(agent, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", agent)):
        return T("core.busstate.agent_hint", "the addressee hint is a session name")
    basis, via, source, asker, auto = data.get("text_from"), None, liaison.NAME, None, False
    if (basis is None) == (data.get("text") is None):
        return T("core.busstate.text_one_of", "one of these is needed: --text-from mN (a session's words) or --text "
                                              "(your own)")
    if basis is not None:
        msg = (st.get("messages") or {}).get(basis)
        if msg is None:
            return T("core.busstate.no_message", "no message {id}").format(id=basis)
        if msg.get("to") != liaison.NAME:
            return T("core.busstate.basis_not_liaison", "{id} is not addressed to the liaison").format(id=basis)
        if msg.get("channel") == "hubbus":
            return T("core.busstate.basis_from_bus", "{id} is from the bus; these are not the words of a session of "
                                                     "this hub").format(id=basis)
        frm = forms.sender(msg) or {}
        if msg.get("from_owner"):
            source = "owner"
        elif frm.get("kind") == "session" and frm.get("name") and frm["name"] != liaison.NAME:
            source = frm["name"]
        else:
            return T("core.busstate.basis_not_words", "{id} is not the words of a session of the hub or of the owner"
                     ).format(id=basis)
        if ts - (msg.get("created") or 0) > BASIS_AGE:
            return T("core.busstate.basis_old", "{id} is older than a day").format(id=basis)
        if msg.get("bus_to") is not None and msg["bus_to"] != name:
            return T("core.busstate.wrong_basis", "wrong_contact: {id} was written for {other}, not for {name}"
                     ).format(id=basis, other=msg["bus_to"], name=name)
        if any(o.get("basis") == basis and o.get("status") not in ("refused",) for o in bus["out"].values()):
            return T("core.busstate.basis_sent", "{id} is already sent (or waits for a card)").format(id=basis)
        via = msg.get("bus_to_via")
        text = msg.get("text")
        # the liaison named no thread: the one the core kept on the session's request, if it still takes the answer
        kept = (bus["requests"].get(basis) or {}).get("thread")
        if thread is None and kept and auto_thread_ok(bus, kept, name, source, ts):
            thread, auto = kept, True
    else:
        text = data.get("text")
        asker = _asker(st, name)
    if not isinstance(text, str) or not text.strip():
        return EMPTY_TEXT
    if len(text.encode("utf-8")) > SEND_MAX:
        return T("core.busstate.send_too_big", "text over 60 KB — the bus has no attachments, only a text or a link")
    # the owner message the request of a session is in the chain of (core/chain.py), and whether that chain passed a
    # schedule firing: of its words, else of the liaison's turn on them (the hub passed his "ask them" with --for; the
    # asker: one session's words, none of the owner's or another hub's). An answer in the thread continues it
    # (live_thread); the liaison's own words carry none
    # and whether the session wrote the request in a turn of the owner's (owner_turn, marked on its message to the
    # liaison): an answer in the thread is an answer to him; the owner's own words and the liaison's carry no mark
    if basis is not None:
        root, sched = chain.root(st, basis), chain.scheduled(st, basis)
        mine = bool(msg.get("in_owner_turn"))
    elif asker:
        lia = st["sessions"].get(liaison.NAME) or {}
        root = chain.turn_root(st, liaison.NAME)
        sched = chain.of_turn_scheduled(st, lia.get("turn_marker"), lia.get("turn_also") or ())
        # every one of its messages the turn took: one from a turn without him makes the liaison's words not his answer
        words = [m for m, who in _turn_words(st) or () if who == asker]
        mine = bool(words) and all(m.get("in_owner_turn") for m in words)
    else:
        root, sched, mine = None, False, False
    oid = f"o{ev['seq']}"
    bus["out"][oid] = {"kind": "message", "status": "requested", "contact": name, "fp": contact["fp"],
                       "source": source, "basis": basis, "via": via, "text": text, "agent_to": agent, "thread": thread,
                       "ts": ts, "of": ev["seq"], **({"asker": asker} if asker else {}),
                       **({"thread_auto": True} if auto else {}),
                       **({"root": root, **({"via_schedule": True} if sched else {})} if root else {}),
                       **({"in_owner_turn": True} if mine else {})}
    if basis in bus["requests"]:
        bus["requests"][basis]["out"] = oid
    actions.append({"do": "bus_send", "session": None, "out": oid, "of": ev["seq"]})
    return None


def send_refusal(bus, oid, now):
    """Why the core does not send this outgoing message (None — it goes): the contact is not checked, the text is over
    the size. The liaison decides everything else; the secrets check is the daemon's (core/redact). A refusal is told to
    the liaison and the asking session (_refused)."""
    o = bus["out"][oid]
    c = bus["contacts"].get(o["contact"]) or {}
    if not c.get("verified"):
        return "not_verified"
    if len(o["text"].encode("utf-8")) > SEND_MAX:
        return "too_big"
    return None


def _card(st, ev, request_id, card, kind):
    """A ready card of the core: an ask on the liaison (or the hub, if there is no liaison) — the page and Telegram show
    it like any form; the answer comes back to busstate.card_answered, not to the session."""
    from core import liaison, reducer
    who = liaison.NAME if liaison.NAME in st["sessions"] else reducer._hub(st)
    sess = st["sessions"].get(who or "")
    if sess is None:
        return None
    qid = f"q{ev['seq']}"
    sess.setdefault("asks", []).append({"id": qid, "request_id": request_id, "tool": "AskUserQuestion",
                                        "input": card.get("input") or {}, "interactive": True, "since": ev["ts"],
                                        "hubbus": {"kind": kind, "labels": card.get("labels") or {}}})
    return {"session": who, "id": qid, "request_id": request_id}


def _drop_card(st, card):
    if not card:
        return
    sess = (st.get("sessions") or {}).get(card.get("session") or "") or {}
    for a in list(sess.get("asks") or []):
        if a.get("request_id") == card.get("request_id"):
            sess["asks"].remove(a)
            (st.get("owner") or {}).get("reminders", {}).pop(f"q:{sess['name']}:{a['request_id']}", None)


def _held(st, ev, data):
    bus = _bus(st)
    o = bus["out"].get(data.get("out") or "")
    if o is None or o.get("status") != "requested":
        return NO_OUT_WAITING
    o["status"] = "held"
    o["finding"] = data.get("finding")
    o["card"] = _card(st, ev, CARD_TAG + data["out"], data.get("card") or {}, "out")
    _request_outcome(bus, o, "asked_owner")
    return _last(bus, data, "held")


def _request_outcome(bus, o, outcome):
    req = bus["requests"].get(o.get("basis") or "")
    if req is not None and req.get("outcome") in (None, "asked_owner"):
        req["outcome"] = outcome


def _out(st, ev, data):
    """The core sealed and signed an envelope: a message (its thread and source are recorded), a receipt or an
    introduction — all go out through a transport."""
    bus = _bus(st)
    ts = ev["ts"]
    kind = data.get("kind")
    oid = data.get("out")
    if kind in ONE_SHOT:
        j = bus["joins"].get(data.get("join") or "")
        if data.get("join") is not None and (j is None or j.get("state") != "active" or j.get("intro_id")):
            return T("core.busstate.join_no_hello", "this join no longer needs a hello")
        bus["out"][oid] = {"kind": kind, "status": "signed", "id": data.get("id"), "contact": data.get("contact"),
                           "fp": data.get("fp"), "envelope": data.get("envelope"), "ts": ts, "exp": data.get("exp")}
        if kind == "receipt" and data.get("inbox") in bus["inbox"]:
            bus["inbox"][data["inbox"]]["receipted"] = data.get("status")
        if j is not None:   # B's introduction by the link and its step are one fact: a cut cannot send a second one
            j.update(step="intro_sent", intro_id=data.get("id"), stepped=ts)
        rec = bus["links"].get(data.get("link_ack") or "")
        if rec is not None:
            rec.pop("ack", None)
        return None
    o = bus["out"].get(oid or "")
    if o is None or o.get("status") not in ("requested", "held"):
        return NO_OUT_WAITING
    o.update(status="signed", id=data.get("id"), envelope=data.get("envelope"), exp=data.get("exp"),
             signed=ts, approved=data.get("approved"), seq_in_thread=data.get("seq"),
             **({"cls": data["cls"]} if data.get("cls") else {}))
    _drop_card(st, o.pop("card", None))
    if o.get("for") == "owner":   # the owners' chat: no thread of the liaison's
        return _last(bus, data, "signed")
    # the daemon left the thread the core chose by itself (another session took it meanwhile): a new one, as sealed
    thread = (None if data.get("thread_new") else o.get("thread")) or data.get("id")
    o["thread"] = thread
    th = bus["threads"].get(thread)
    if th is None:
        th = bus["threads"][thread] = {"contact": o["contact"], "source": whose(o), "ts": ts, "n": 1,
                                       **({"owner": True} if o.get("press") else {})}
    else:
        th["n"] = th.get("n", 0) + 1
        if th.get("source") is None and whose(o):
            th["source"] = whose(o)
    if whose(o) is not None and not o.get("decline"):
        # a session's request: the answers that follow are to the latest one, and go to the source — its chain, if
        # the request is the source's; another session's request, or one not on the owner's word, leaves none. The
        # liaison's own words (a clarification in the thread) change nothing; a thread with no source has no chain
        for k in THREAD_KEYS:
            th.pop(k, None)
        if whose(o) == th["source"]:
            th.update({k: o[k] for k in THREAD_KEYS if o.get(k)})
    # a decline notice answers only the declined item, which has its outcome already; a thread the core chose by itself
    # for a session's words answers nothing the liaison still has to decide in it
    if not o.get("decline") and not o.get("thread_auto"):
        for item in bus["inbox"].values():   # the liaison answered in the thread: those items have an outcome
            if item.get("thread") == thread and item.get("contact") == o["contact"] and item.get("outcome") is None:
                item.update(outcome="answered", done=ts)
    _request_outcome(bus, o, "sent")
    return _last(bus, data, "signed")


def _say(st, ev, to, text, k):
    from core import reducer
    if to and to in st["sessions"]:
        reducer.new_message(st, f"m{ev['seq']}{k}", to, text, ev["ts"], origin=reducer.forms.ORIGIN_CORE)


# what the liaison and the asking session hear about an outgoing message that did not go or did not arrive
NOT_SENT = T("core.busstate.not_sent", "Not sent to the hub {contact} ({out}{basis}): {code}.")
NOT_SENT_BASIS = T("core.busstate.not_sent_basis", ", words {id}")
OWNER_ANSWERED = T("core.busstate.owner_answered", " The owner answered: \"{words}\".")
REMOTE_REFUSED = T("core.busstate.remote_refused", "The hub {contact} refused {out}: {code}.")
NOT_DELIVERED = T("core.busstate.not_delivered", "Not delivered to the hub {contact} ({out}): the hub did not answer in "
                                                 "7 days.")


def _refused(st, ev, oid, code, data=None):
    from core import liaison
    bus = _bus(st)
    o = bus["out"].get(oid or "")
    if o is None or o.get("status") not in ("requested", "held", "signed", "handed"):
        return NO_OUT
    o.update(status="refused", code=code, done=ev["ts"])
    o.pop("envelope", None)
    _drop_card(st, o.pop("card", None))
    _request_outcome(bus, o, "declined")
    if o.get("for") == "owner" or o.get("press"):   # the owner's own words: the liaison has no part in it (no turn), nothing went — no body kept
        o.pop("text", None)
        return _last(bus, data or {}, code)
    words = (data or {}).get("words")
    text = coretext.join(
        NOT_SENT.format(contact=o["contact"], out=oid, code=code,
                        basis=NOT_SENT_BASIS.format(id=o["basis"]) if o.get("basis") else ""),
        *((" ", REFUSED_HINT[code]) if code in REFUSED_HINT else ()),
        *((OWNER_ANSWERED.format(words=" ".join(str(words).split())[:300]),) if words else ()))
    if whose(o):
        _say(st, ev, whose(o), text, "s")
    if code != "bus_off" and not o.get("direct"):   # the core sent it without the liaison: the session alone is told
        _say(st, ev, liaison.NAME if liaison.NAME in st["sessions"] else None, text, "l")
    return _last(bus, data or {}, code)


def _delivered(st, ev, data):
    """A signed receipt of the receiver for our envelope: `ok` — its core took it and gave it to its liaison; `held` —
    its owner checks it; `refused` — with a code. Only from the contact the envelope went to."""
    from core import liaison
    bus = _bus(st)
    o = next(((k, x) for k, x in bus["out"].items() if x.get("kind") == "message" and x.get("id") == data.get("of")
              and x.get("fp") == data.get("hub")), None)
    if o is None:
        return None
    oid, o = o
    status = data.get("status")
    if status == "held":
        o["remote"] = "held"
        return None
    if status == "refused":
        o.update(status="refused", code=f"remote:{data.get('code') or '-'}", done=ev["ts"])
        o.pop("envelope", None)
        text = REMOTE_REFUSED.format(contact=o["contact"], out=oid, code=data.get("code") or "—")
        if o.get("press"):   # the owner's own words: the page shows the refusal under them, no session is told
            return None
        _say(st, ev, liaison.NAME if liaison.NAME in st["sessions"] else None, text, "l")
        if whose(o):
            _say(st, ev, whose(o), text, "s")
        return None
    o.update(status="delivered", done=ev["ts"])
    o.pop("envelope", None)
    o.pop("text", None)   # the text is in the journal (the basis message); the state keeps no body after delivery
    c = bus["contacts"].get(o.get("contact") or "")
    if c is not None:
        c["delivered"] = True
        if data.get("cls"):   # the contact read a v2 box: the misses are over
            c.pop("v2_miss", None)
    return None


def _expired(st, ev, data):
    from core import liaison
    bus = _bus(st)
    oid = data.get("out")
    o = bus["out"].get(oid or "")
    if o is None:
        return NO_OUT
    if o.get("kind") != "message":
        del bus["out"][oid]
        return None
    if o.get("status") == "held":
        return _refused(st, ev, oid, "card_expired")
    if o.get("status") not in ("signed", "handed"):
        return T("core.busstate.out_decided", "this outgoing message is already decided")
    o.update(status="expired", done=ev["ts"])
    o.pop("envelope", None)
    text = NOT_DELIVERED.format(contact=o["contact"], out=oid)
    if o.get("press"):
        return None
    _say(st, ev, liaison.NAME if liaison.NAME in st["sessions"] else None, text, "l")
    if whose(o):
        _say(st, ev, whose(o), text, "s")
    return None


# --- incoming -------------------------------------------------------------------------------------------------------

def _inbound(bus, kind, data, ts):
    """An envelope decided by the core: the seen set, the counters, the quarantine. A refusal without a contact (format,
    unknown hub, wrong signature) counts nothing: a stranger cannot spend a contact's limit."""
    fp, name = data.get("hub"), data.get("contact")
    hour = int(ts) // 3600
    if bus.get("pruned") != hour:   # once an hour, not on every envelope: the seen set may hold thousands
        bus["pruned"] = hour
        for k in [k for k, e in bus["seen"].items() if e.get("exp", 0) + SEEN_EXTRA < ts]:
            del bus["seen"][k]
        for f in [f for f, r in bus["rate"].items() if r.get("h") != hour]:
            del bus["rate"][f]
        for f in [f for f, times in bus["bad"].items() if not any(t > ts - 86400 for t in times)]:
            del bus["bad"][f]
        for t in [t for t, th in (bus.get("threads") or {}).items() if th.get("ts", 0) + THREAD_TTL < ts]:
            del bus["threads"][t]
        for b in [b for b, item in (bus.get("inbox") or {}).items() if item.get("ts", 0) + INBOX_KEEP < ts]:
            del bus["inbox"][b]
        for m in [m for m, r in (bus.get("requests") or {}).items() if r.get("ts", 0) + INBOX_KEEP < ts]:
            del bus["requests"][m]
        for f in [f for f, r in (bus.get("intros") or {}).items() if not r.get("card") and r.get("ts", 0) + 86400 < ts]:
            del bus["intros"][f]
        for o in [o for o, x in (bus.get("out") or {}).items()
                  if x.get("status") in ("delivered", "refused", "expired") and x.get("done", ts) + INBOX_KEEP < ts]:
            del bus["out"][o]
        knocked = (bus.get("knocks") or {}).get("fps") or {}
        for f in [f for f, t in knocked.items() if t + KNOCK_FP < ts]:
            del knocked[f]
        for part in ("links", "joins"):   # finished ones are kept two weeks for the page; a join waiting for A stays
            recs = bus.get(part) or {}
            for k in [k for k, r in recs.items() if max(r.get("done") or 0, r.get("exp") or 0) + LINKS_KEEP < ts
                      and r.get("state") not in ("active", "cancelling")]:
                del recs[k]
    if kind == "core.bus_rejected" and data.get("code") == "many" and type(data.get("knocks")) is int:
        k = bus.setdefault("knocks", {})
        k["quiet"] = k.get("quiet", 0) + max(0, data["knocks"])   # knocks past the hour's records: in the next notice
    if kind == "core.bus_rejected" and data.get("code") == "unknown_hub" and isinstance(fp, str):
        k = bus.setdefault("knocks", {})
        if data.get("notice"):   # the daemon told the owner (knock_notice): the next one an hour later, this hub a day later
            k.update(last=ts, quiet=0)
            k.setdefault("fps", {})[fp] = ts
        else:
            k["quiet"] = k.get("quiet", 0) + 1
    if not (isinstance(fp, str) and isinstance(name, str)):
        return None
    r = bus["rate"].get(fp)
    if r is None or r.get("h") != hour:
        r = bus["rate"][fp] = {"h": hour, "env": 0, "msg": 0}
    cls = data.get("cls") if data.get("cls") in CLASSES else None
    count = r.setdefault("cls", {}).setdefault(cls, {"env": 0, "msg": 0}) if cls else r   # v2: per class
    count["env"] += 1
    key = seen_key(fp, data.get("id"))
    if kind in ("core.bus_received", "core.bus_chat_received"):
        count["msg"] += 1
        result = {"ok": "delivered", "held": "held", "muted": "muted", "unverified": "unverified"}.get(
            data.get("status"), "delivered")
        bus["seen"][key] = {"kind": "message", "result": result, "exp": data.get("exp", ts), "receipted": ts,
                            **({"cls": cls} if cls else {})}
    elif kind in ("core.bus_receipt_seen", "core.bus_delivered"):
        bus["seen"][key] = {"kind": "receipt", "result": "receipt", "exp": data.get("exp", ts)}
    elif kind == "core.bus_card_updated":
        bus["seen"][key] = {"kind": "ctl", "result": "ctl", "exp": data.get("exp", ts)}
    elif data.get("code") == "dup" and data.get("receipted") and key in bus["seen"]:
        bus["seen"][key]["receipted"] = ts
    elif data.get("code") == "bad_box":
        bad = [t for t in bus["bad"].get(fp, []) if t > ts - 86400] + [ts]
        bus["bad"][fp] = bad
        c = bus["contacts"].get(name)
        if c is not None and len(bad) >= BAD_BOX_DAY:
            c["quarantine"] = ts
    return None


def _received(st, ev, data):
    """An accepted message: the item the liaison decides on, and — for an ordinary one — the message to the liaison in
    the core's frame, made by this same fold (accepted without a message is impossible). Held — a card to the owner
    first; muted — kept, not delivered."""
    from core import forms, liaison, reducer
    bus = _bus(st)
    ts = ev["ts"]
    name, thread = data["contact"], data.get("thread")
    th = bus["threads"].get(thread or "")
    live = live_thread(bus, thread, name, ts) or {}
    source = live.get("source")
    answer = data.get("for") != "owner" and data.get("status") == "ok" and owner_thread(bus, thread, name, ts)
    if th is None and thread:   # opened by the other side: our answer in it needs no card (auto_allowed)
        bus["threads"][thread] = {"contact": name, "source": None, "ts": ts, "n": 1, "theirs": True}
    elif th is not None and th.get("contact") == name:
        th["n"] = th.get("n", 0) + 1
    elif th is not None:   # another contact's thread id: the item is not in that thread, an answer opens a new one
        thread = None
    bid = f"b{ev['seq']}"
    marker = data.get("marker") if isinstance(data.get("marker"), str) and liaison.MARKER.fullmatch(data["marker"]) \
        else f"bus-{str(data.get('id') or '')[:8]}"
    remote = data.get("status") == "ok" and name in owners(st)   # nobody at the page: for the hub session (spec d2505378, part D, remote)
    owner = answer and not remote   # the answer in a thread of the owner's words is for him, not the liaison
    item = bus["inbox"][bid] = {
        "contact": name, "fp": data.get("hub"), "id": data.get("id"), "agent_from": data.get("agent_from"),
        "agent_to": data.get("agent_to"), "thread": thread, "text": data.get("text") or "", "ts": ts, "source": source,
        "status": data.get("status"), "outcome": "muted" if data.get("status") == "muted" else "owner" if owner else None,
        "marker": marker, **({"for": "owner"} if owner else {}), **({"reply": True} if answer else {}),
        **({"chat": True} if data.get("for") == "owner" else {}),   # a hub of the previous release wrote it as a chat
        **({"finding": data["finding"]} if data.get("finding") else {}),
        **({"cls": data["cls"]} if data.get("cls") in CLASSES else {}),
        **{k: live[k] for k in THREAD_KEYS if live.get(k)}}
    contact = bus["contacts"].get(name) or {}
    if remote and _owned(st, ev, bid, item, source):   # no session to give it to: the liaison, nobody reads the page
        return
    if owner:   # the other owner's words: for the page and Telegram, never the liaison or a session — no turn
        return
    if data.get("status") == "ok":
        if source and not item.get("finding") and not pass_refusal(st, bid, source, ts):   # the answer to a session's request: to it itself
            _deliver(st, ev, bid, item, source, None, direct=True)
        else:
            _to_liaison(st, ev, bid, item, contact)
    elif data.get("status") == "held":
        item["card"] = _card(st, ev, CARD_TAG + bid, data.get("card") or {}, "held")


def _owned(st, ev, bid, item, source):
    """An owner's words on a hub nobody sits at (spec d2505378, part D, remote): straight to the hub session as the owner's task,
    no turn of the liaison; the answer in the thread of a live session's request goes to that session, as for any hub.
    False — no session to give it to: the liaison, as before."""
    from core import reducer
    ts = ev["ts"]
    hub = reducer._hub(st)
    to = source if source and not pass_refusal(st, bid, source, ts) else None
    if to is None and hub and not pass_refusal(st, bid, hub, ts):
        to = hub
    if to is None:
        return False
    _deliver(st, ev, bid, item, to, None, direct=to == source, owned=source if source and to != source else True)
    return True


def _to_liaison(st, ev, bid, item, contact):
    from core import forms, liaison, reducer
    msg = reducer.new_message(st, f"m{ev['seq']}", liaison.NAME,
                              liaison.inbound(bid, item, contact, item["marker"], rules_for(_bus(st), item["contact"])),
                              ev["ts"],
                              origin=ORIGIN, channel="hubbus",
                              hubbus={"inbox": bid, "contact": item["contact"]}, **_chain(item))
    item["message"] = msg["id"]


def _chain(item):
    """The chain of the owner's request the item answers (live_thread): the message carries it, so the turn on it
    continues his chain and its result goes where he asked, and a chain that passed a schedule firing stays not his
    live word (chain.scheduled). Words, not his: from_owner and `re` stay unset."""
    return {k: item[k] for k in CHAIN if item.get(k)}


def _passed(st, ev, data):
    from core import liaison, reducer
    bus = _bus(st)
    bid, to = data.get("inbox"), data.get("to")
    why = pass_refusal(st, bid, to, ev["ts"])
    if why:
        return why
    msg = _deliver(st, ev, bid, bus["inbox"][bid], to, data.get("note"))
    return _last(bus, {**data, "message": msg["id"]}, "passed")


def _deliver(st, ev, bid, item, to, note, direct=False, owned=False):
    """The words of another hub to a session, in the core's frame: passed by the liaison, or — an answer in a thread
    of that session's own request — by the core at once (`direct`: no turn of the liaison). `owned`: an owner's words
    on a hub nobody sits at (spec d2505378, part D, remote) — the frame of the owner's task; a session name — the thread was that session's."""
    from core import liaison, reducer
    frame = liaison.owned(item, item["marker"], rules_for(_bus(st), item["contact"]), direct,
                          of=owned if isinstance(owned, str) else None) if owned \
        else liaison.passed(item, item["marker"], note, direct)
    # the answer to the request its source wrote in a turn of the owner's, given to that source: its turn on it is
    # an answer to him (reducer: turn_bus_owner) — to another session it is only words of another hub
    asked = item.get("in_owner_turn") and to == item.get("source")
    if asked:
        item["answered_owner"] = True   # the hub answered him (busflow.list_view: "Now" keeps it until he opens it)
    msg = reducer.new_message(st, f"m{ev['seq']}", to, frame, ev["ts"],
                              origin=ORIGIN, channel="hubbus",
                              hubbus={"inbox": bid, "contact": item["contact"], "passed": True,
                                      **({"in_owner_turn": True} if asked else {})}, **_chain(item))
    item.setdefault("passed_to", []).append(to)
    item.update(outcome="passed", done=ev["ts"], **({"direct": True} if direct else {}))
    return msg


def _intro(st, ev, data):
    """A hub introduced itself through a transport (after its owner accepted us): a card to the owner with its label,
    fingerprint and check code; once per fingerprint a day (the daemon), never to the sessions."""
    bus = _bus(st)
    fp = data.get("fp")
    if not isinstance(fp, str) or by_fp(bus, fp)[1] is not None:
        return T("core.busstate.hub_known", "this hub is already among the contacts")
    old = bus["intros"].get(fp)
    if old:
        _drop_card(st, old.get("card"))
    for f in [f for f, r in bus["intros"].items() if not r.get("card") and r.get("ts", 0) + 86400 < ev["ts"]]:
        del bus["intros"][f]   # answered "not now" over a day ago: gone, its name is free
    rec = {k: data.get(k) for k in ("fp", "label", "sign_pub", "enc_pub", "relays", "agents", "bus_v", "name", "suggest")}
    rec["ts"] = ev["ts"]
    rec["card"] = _card(st, ev, CARD_TAG + "i" + fp[4:16], data.get("card") or {}, "intro")
    bus["intros"][fp] = rec
    return None


def _offer_request(st, ev, data, actions):
    """A session (or the owner's terminal) brings the invitation of another hub it was given: the link lies as a file in
    the core's folder (like a pasted one), the core reads and checks it (busd.offer) and puts a card to the owner — his
    press «Link» is the same join as a pasted invitation. Never a module's or the liaison's, never the other hub's."""
    from core import liaison
    bus = _bus(st)
    if data.get("channel") != "session" or not data.get("sender") or liaison.from_liaison(st, data):
        return T("core.busstate.offer_who", "another hub's invitation is brought to the owner by a session of the hub "
                                            "(not the liaison); the owner pastes it themselves: hub bus join -")
    if not bus.get("enabled"):
        return T("core.busstate.offer_bus_off", "bus_off: the bus is off — the owner turns it on (Settings → External "
                                                "hubs); a session does not take the invitation")
    if not bus.get("name"):
        return NO_NAME
    for k, ok in (("path", lambda v: isinstance(v, str) and v), ("sha256", lambda v: isinstance(v, str)
                                                                 and re.fullmatch(r"[0-9a-f]{64}", v))):
        if not ok(data.get(k)):
            return BAD_FIELD.format(kind="bus.offer_requested", field=k)
    if len([o for o in (bus.get("offers") or {}).values() if o.get("state") == "open"]) >= OFFER_MAX:
        return T("core.busstate.offers_full", "offers_full: {most} invitations already wait for the owner's decision "
                                              "— do not add new ones").format(most=OFFER_MAX)
    actions.append({"do": "bus_offer", "session": None, "of": ev["seq"], "path": data["path"], "sha256": data["sha256"],
                    "by": str(data["sender"])[:100]})
    return None


def _offer_fact(st, ev, kind, data, actions):
    """core.bus_offer: the daemon read and checked the invitation — the card to the owner (kind «offer»); and
    core.bus_offer_ended: the offer is over (the term, the owner's «no» is the answer itself) — its card goes."""
    bus = _bus(st)
    offers = bus.setdefault("offers", {})
    lid = data.get("id")
    if not isinstance(lid, str):
        return T("core.busstate.offer_no_id", "an invitation without an id")
    if kind == "core.bus_offer":
        old = offers.get(lid)
        if old is not None and old.get("state") == "open":
            return T("core.busstate.offer_waits", "this invitation already waits for the owner")
        card = _card(st, ev, CARD_TAG + "f" + lid[:16], data.get("card") or {}, "offer")
        if card is None:
            return T("core.busstate.offer_no_card", "nobody to show the card to: no liaison and no hub session")
        for k in [k for k, r in offers.items()
                  if r.get("state") not in ("open", "taken") and ev["ts"] - (r.get("done") or 0) > 86400]:
            del offers[k]   # ended a day ago: gone, the invitation may be brought again
        offers[lid] = {"fp": data.get("fp"), "label": data.get("label"), "code": data.get("code"), "exp": data.get("exp"),
                       "by": data.get("by"), "ts": ev["ts"], "state": "open", "card": card}
        return _last(bus, data, "ok")
    rec = offers.get(lid)
    if rec is None or rec.get("state") != "open":
        return T("core.busstate.no_offer", "no such invitation waiting")
    _drop_card(st, rec.pop("card", None))
    rec.update(state=data.get("why") or "expired", done=ev["ts"])
    actions.append({"do": "bus_link_files", "session": None})
    _offer_told(st, ev, lid, "expired")
    return None


def _offer_told(st, ev, lid, how, soft=False, **params):
    """The session that brought an invitation hears how it ended, once: linked, the owner's «No», a press that did not
    link (`why` in words; never the raw reason of a crash), cancelled, the term, the bus turned off. The words are the
    state's language (not the process's). `soft`: a note that a later outcome may follow (it does not close the offer)."""
    from core import busflow, i18n, liaison
    rec = (_bus(st).get("offers") or {}).get(lid or "")
    if rec is None or rec.get("told") or rec.get("by") not in st["sessions"] \
            or (how in ("linked", "failed", "cancel") and rec.get("state") != "taken"):
        return   # a join of the same link the owner pasted by hand is not the outcome of this offer
    lang = i18n.state_lang(st)
    if how == "failed":
        params = {"why": busflow.why(params.get("code") or "error", lang=lang)}
    elif how == "linked":
        params = {"name": liaison.safe_label(params.get("name"))}
    if not soft:
        rec["told"] = ev["ts"]
    key = {"linked": "core.bus.offer_told_linked", "no": "core.bus.offer_told_no", "failed": "core.bus.offer_told_failed",
           "expired": "core.bus.offer_told_expired", "off": "core.bus.offer_told_off",
           "cancel": "core.bus.offer_told_cancel"}[how]
    _say(st, ev, rec.get("by"), i18n.tr(lang, key, label=liaison.safe_label(rec.get("label")), **params), "o" + lid[:8])


def liaison_asked(st, sess, rid, ts):
    """The liaison asked the owner by a form in its turn on exactly one incoming item: that item waits for the owner,
    not for the liaison — no 30-minute mark while the question is open (spec d2505378, part C, asked). Its outcome stays None: the
    liaison still closes it (pass, done, an answer) once the owner has answered. A turn on two items marks neither:
    which one the question is about is unknown."""
    from core import reducer
    inbox = (st.get("bus") or {}).get("inbox") or {}
    msgs = st.get("messages") or {}
    items = {(msgs.get(m) or {}).get("hubbus", {}).get("inbox") for m in reducer._took(sess)} - {None}
    open_ = [b for b in items if b in inbox and inbox[b].get("outcome") is None and inbox[b].get("status", "ok") == "ok"]
    if len(items) == 1 and open_ and rid:
        qid = next((a.get("id") for a in sess.get("asks") or [] if a.get("request_id") == rid), None)
        inbox[open_[0]]["asked"] = {"rid": rid, "ts": ts, **({"qid": qid} if qid else {})}
        inbox[open_[0]].pop("since", None)   # a new question: an earlier answer's 30 minutes are not this one's


def _asked_since(st, item):
    """When the 30 minutes of an item the liaison asked about start: while its question is open — not at all (None);
    once closed — from the owner's answer to it (any path: a card, typed words, --for), else from the question."""
    from core import liaison
    asked = item.get("asked") or {}
    got = [m.get("created") or 0 for m in (st.get("messages") or {}).values()
           if m.get("session") == liaison.NAME and asked.get("qid") and m.get("answers_question") == asked["qid"]]
    return max(got, default=asked.get("ts") or item.get("ts") or 0)


def liaison_answered(st, rid, ts):
    """The owner answered the liaison's question: the item it held is the liaison's again, 30 minutes from now."""
    for item in ((st.get("bus") or {}).get("inbox") or {}).values():
        if (item.get("asked") or {}).get("rid") == rid:
            item.pop("asked")
            item.pop("stale", None)
            item["since"] = ts


def _waits_owner(st, item):
    """Is this item held by an open question of the liaison to the owner (liaison_asked)?"""
    from core import liaison
    rid = (item.get("asked") or {}).get("rid")
    if not rid:
        return False
    sess = (st.get("sessions") or {}).get(liaison.NAME) or {}
    return any(a.get("request_id") == rid for a in sess.get("asks") or [])


def _clock(st, item, now):
    """From when an item's 30 minutes count: None — not now (the liaison's question to the owner about it is open)."""
    if _waits_owner(st, item):
        return None
    return item.get("since") or (_asked_since(st, item) if item.get("asked") else None) or item.get("ts") or now


def _stale(st, ev, data):
    """An incoming item or a session's request without an outcome for 30 minutes: the mark, and once — a note to the
    request's source (the owner's notice for an item is written by the daemon, without the other hub's words)."""
    bus = _bus(st)
    key = data.get("item")
    item = bus["inbox"].get(key or "") or bus["requests"].get(key or "")
    if item is None or item.get("outcome") is not None or item.get("stale"):
        return T("core.busstate.nothing_to_mark", "nothing to mark")
    item["stale"] = ev["ts"]
    if key in bus["requests"]:
        _say(st, ev, item.get("session"), T("core.busstate.stale_request", "The liaison has not handled your request "
                                                                           "{id} (contact {contact}) in 30 minutes. It "
                                                                           "waits for it; if urgent, tell the owner in "
                                                                           "your result.").format(
            id=key, contact=item.get("contact")), "s")
    return None


# --- the owner's answer to a card of the core ----------------------------------------------------------------------

def card_answered(st, ev, data, sess, actions, behavior=None):
    """permission.answered / question.dismissed / question.withdrawn on a card of the bus: True if it was one (handled
    here, not as a session's form). Only the owner's "Send" sends; "Send and from now on…" also allows the pair
    «source session × contact»; anything else — closing, another answer, words — is not a yes."""
    from core import guards, reducer
    rid = data.get("request_id")
    ask = next((a for a in sess.get("asks") or [] if a.get("request_id") == rid and a.get("hubbus")), None) \
        if rid else next((a for a in sess.get("asks") or [] if a.get("id") == data.get("id") and a.get("hubbus")), None)
    if ask is None:
        return False
    kind = ev["type"]
    if kind == "permission.answered" and guards.from_owner_strict(st, data):
        raise reducer.Rejected(T("core.busstate.card_owner_only", "only the owner answers a card of the bus"))
    sess["asks"].remove(ask)
    reducer._answered(sess, ev, data, behavior, ask=ask, why=data.get("why"))
    st["owner"]["reminders"].pop(f"q:{sess['name']}:{ask['request_id']}", None)
    answers = data.get("answers") or {} if kind == "permission.answered" and data.get("behavior") == "allow" else {}
    said = next(iter(answers.values()), None) if answers else None
    labels = ask["hubbus"].get("labels") or {}
    pick = next((k for k, v in labels.items() if said is not None and v == said), None)
    bus = _bus(st)
    tag = ask["request_id"][len(CARD_TAG):]
    what = ask["hubbus"].get("kind")
    if what == "out":
        o = bus["out"].get(tag)
        if o is None or o.get("status") != "held":
            return True
        o.pop("card", None)
        if pick in ("send", "always") and bus.get("enabled"):
            o["approved"] = "owner"
            actions.append({"do": "bus_seal", "session": None, "out": tag})
        else:
            _refused(st, ev, tag, "declined", {"words": said if pick is None else None})
        return True
    if what == "held":
        item = bus["inbox"].get(tag)
        if item is None or item.get("status") != "held" or item.get("outcome") is not None:
            return True
        item.pop("card", None)
        if pick == "pass" and bus.get("enabled"):
            item["status"] = "ok"
            _to_liaison(st, ev, tag, item, bus["contacts"].get(item["contact"]) or {})
            actions.append({"do": "bus_receipt", "session": None, "inbox": tag, "status": "ok"})
        else:
            item.update(status="refused", outcome="declined", why="owner")
            actions.append({"do": "bus_receipt", "session": None, "inbox": tag, "status": "refused", "code": "declined"})
        return True
    if what == "offer":
        lid = next((k for k, r in (bus.get("offers") or {}).items()
                    if (r.get("card") or {}).get("request_id") == ask["request_id"]), None)
        if lid is None:
            return True
        rec = bus["offers"][lid]
        rec.pop("card", None)
        if pick == "link" and bus.get("enabled") and rec.get("state") == "open":
            rec.update(state="taken", done=ev["ts"], take_of=ev["seq"])   # the outcome comes with this number
            actions.append({"do": "bus_offer_take", "session": None, "of": ev["seq"], "id": lid})
        else:   # «No», the close button, words: nothing is done and the file goes
            rec.update(state="declined", done=ev["ts"])
            actions.append({"do": "bus_link_files", "session": None})
            if pick != "link" or not bus.get("enabled"):   # a press the offer could not take is not the owner's «No»
                _offer_told(st, ev, lid, "no" if pick != "link" else "off")
        return True
    if what == "intro":
        fp = next((f for f, r in bus["intros"].items() if (r.get("card") or {}).get("request_id") == ask["request_id"]),
                  None)
        if fp is None:
            return True
        rec = bus["intros"][fp]
        rec.pop("card", None)
        if pick in ("accept", "block") and bus.get("enabled"):
            actions.append({"do": "bus_contact", "session": None, "of": ev["seq"], "op": "intro", "fp": fp,
                            "name": rec.get("name"), "status": "known" if pick == "accept" else "blocked"})
        else:
            rec["card"] = None   # "not now": the record keeps the day's repeat protection; it may come again tomorrow
        return True
    return True


def is_card(ask):
    return bool((ask or {}).get("hubbus"))


# --- the liaison's boundaries and the sessions' requests to it ----------------------------------------------------

LIAISON_MAY = ("bus.send_requested", "bus.pass_requested", "bus.decline_requested", "bus.done_requested", "bus.note",
               "bus.rule_requested", "chat.send_requested", "chat.pass_requested", "chat.done_requested",
               "message.received",
               "session.gist", "question.withdrawn", "cli.gave_up")


def liaison_refusal(st, kind, data):
    """What the liaison may not do (§5.13): only its bus commands, a line about itself and its own question; no tasks,
    no executors, no sessions, no schedule, no tools, no owner's decisions. The same boundary as the rest of the spool:
    by the session name the process states."""
    from core import liaison
    if kind in LIAISON_MAY or not liaison.from_liaison(st, data):
        return None
    return T("core.busstate.liaison_closed", "this is closed to the liaison ({kind}): only hub bus "
                                             "send/pass/done/note/decline and a form to the owner").format(kind=kind)


def message_refusal(st, data, name, now=0):
    """A message the core refuses because of the bus: the liaison writes to sessions only through `hub bus pass` (the
    words of another hub would reach a session without the core's frame); to the liaison while the bus is off; a
    request `--to <contact>` that names no accepted contact."""
    from core import liaison
    if liaison.from_liaison(st, data) and name != liaison.NAME:
        return T("core.busstate.liaison_pass_only", "the liaison writes to sessions only through hub bus pass (the core "
                                                    "puts the \"from another hub\" frame)")
    if name != liaison.NAME or not liaison.is_liaison(st, name):
        if data.get("bus_to") is not None:
            return T("core.busstate.to_liaison_only", "--to <contact> — only in a message to the liaison: hub send "
                                                      "liaison --to <contact> \"…\"")
        return None
    b = st.get("bus") or {}
    if not liaison.needed(st):
        return T("core.busstate.liaison_asleep", "the hub bus is off — the liaison is asleep; the owner turns it on: "
                                                 "hub bus on")
    if data.get("channel") == "hubbus":
        return T("core.busstate.bus_past_core", "a bus message does not go to the liaison past the core")
    if data.get("channel") == "chats":
        return T("core.busstate.chat_past_core", "a person's message does not go to the liaison past the core")
    to = data.get("bus_to")
    if to is not None:
        c = (b.get("contacts") or {}).get(to) if isinstance(to, str) else None
        if c is None or c.get("status") != "known":
            return T("core.busstate.no_hub_path", "no_hub_path: bus contact {name} is not among the accepted ones — who "
                                                  "is there: hub contacts find <who>").format(name=to)
        if data.get("bus_to_via") not in ("local", "person"):
            return T("core.busstate.bus_to_via", "bus_to_via — local or person")
        t = data.get("bus_thread")
        if t is not None and not (isinstance(t, str) and auto_thread_ok(b, t, to, data.get("sender"), now)):
            return DIRECT_THREAD.format(thread=t, name=to)
    return None


def message_taken(st, ev, data, msg):
    """A message to the liaison that names a contact (`hub send liaison --to anna "…"`): the canonical local name and
    how it was named are written on the message by the core (`bus_to`, `bus_to_via`: local — the contact's own name,
    person — through a person of the book; an allowed pair works only for local), and the request waits for an
    outcome (sent, declined, asked_owner)."""
    from core import liaison
    if msg.get("session") == liaison.NAME and asked_in_owner_turn(st, data):
        msg["in_owner_turn"] = True   # a request written in a turn of the owner's: its answer is to him (_send_request)
    if msg.get("session") != liaison.NAME or data.get("bus_to") is None:
        return
    msg["bus_to"], msg["bus_to_via"] = data["bus_to"], data["bus_to_via"]
    if data.get("channel") == "session" and data.get("sender"):
        c = (st.get("bus") or {}).get("contacts", {}).get(data["bus_to"]) or {}
        # the core's service note to the liaison (model-facing, as the other routing notes of the core)
        msg["routing_note"] = (f"A request of {data['sender']} to pass to the hub {data['bus_to']} "
                               f"({liaison.safe_label(shown(c))}; {liaison.whose(c)}): this is not a question to you — "
                               f"send its words by reference: hub bus send {data['bus_to']} --text-from {msg['id']}")
        rules = rules_for(_bus(st), data["bus_to"])
        if rules:   # the rules about that hub come with its incoming; a request to it needs them too (spec d2505378, part A, outgoing)
            msg["routing_note"] += " " + liaison.IN_RULES + " " + "; ".join(rules)
        # the thread the session named (`--thread`, checked in message_refusal), else the one the core keeps for it
        thread = data.get("bus_thread") or reply_thread(st, data["sender"], data["bus_to"], ev["ts"])
        _bus(st)["requests"][msg["id"]] = {"session": data["sender"], "contact": data["bus_to"], "via": data["bus_to_via"],
                                           "ts": ev["ts"], "outcome": None, **({"thread": thread} if thread else {})}


# --- views ---------------------------------------------------------------------------------------------------------

def view(st):
    """What commands outside the core read without the big state (hub contacts find, hub send --to): whether the bus is
    on and the contacts — local name, status, label, person and the visible sessions; no keys, no texts. A file any
    process may write: shown, never trusted (the fold checks every name against the state)."""
    b = st.get("bus") or {}
    me = b.get("me") or {}
    owned = owners(st)
    return {"enabled": bool(b.get("enabled")),
            "contacts": {n: {"status": c.get("status"), "label": shown(c), "stated": c.get("label"), "person": c.get("person"),
                             "agents": sorted((c.get("agents") or {}).keys()),
                             # a session's words go straight from the core (`bus.direct_requested`): written by a core
                             # that folds that fact, so the CLI of a newer release never sends it to an older daemon
                             **({"direct": True} if direct_refusal(st, n, owned) is None else {})}
                         for n, c in sorted((b.get("contacts") or {}).items())},
            # the sessions the liaison may choose from when it passes a message on (it is not given the state): the name
            # and what the session does, never the liaison itself
            "sessions": liaison_sessions(st),
            # the owner's path (core/busflow): its progress without the big state — links, joins, the setup steps
            "me": {"fp": me.get("fp"), "label": me.get("label")} if me.get("fp") else None,
            "rules": [{"id": r.get("id"), "contact": r.get("contact"), "text": r.get("text"), "by": r.get("by")}
                      for r in b.get("rules") or []],
            "relay": {"url": b["relay"].get("url"), "bus_v": b["relay"].get("bus_v")} if b.get("relay") else None,
            "links": {k: {"state": r.get("state"), "exp": r.get("exp"), "posted": bool(r.get("posted")),
                          "invite": bool(r.get("invite")), "code": r.get("code")}
                      for k, r in sorted((b.get("links") or {}).items())},
            "joins": {k: {"state": j.get("state"), "step": j.get("step"), "code": j.get("code"), "label": j.get("label"),
                          "name": j.get("name"), "intro": bool(j.get("intro_id"))}
                      for k, j in sorted((b.get("joins") or {}).items())},
            "setup": b.get("setup")}


def liaison_sessions(st):
    """{name: what it does} of the live sessions the liaison may pass a message to (the core's state is not given to it
    whole): never the liaison itself and not an executor of someone's task."""
    return {n: ("hub session · " if s.get("kind") == "hub" else "") + (s.get("about") or "")[:200]
            for n, s in sorted((st.get("sessions") or {}).items())
            if s.get("state") not in ("removed", "closed", "failed") and not s.get("liaison")
            and s.get("role") != "executor"}


def knock_notice(bus, fp, now):
    """Does an envelope of an unknown hub get a notice to the owner (spec §5.5): not more than one an hour for the whole
    hub and one a day for the fingerprint; the others are counted (`quiet`) and named in the next notice."""
    k = bus.get("knocks") or {}
    return now - k.get("last", -KNOCK_HUB) >= KNOCK_HUB and now - (k.get("fps") or {}).get(fp, -KNOCK_FP) >= KNOCK_FP


def transport_view(st):
    """What a transport reads (core/busd.py writes it as a file, with the envelopes next to it): whether the bus works,
    our fingerprint, the relay the owner joined, the fingerprints the hub takes envelopes from (accepted and muted
    contacts — a relay drops the rest before they come) and the envelopes to hand. No keys, no texts."""
    b = st.get("bus") or {}
    relay = b.get("relay") or None
    contacts = (b.get("contacts") or {}).values()
    links = b.get("links") or {}
    return {"enabled": bool(b.get("enabled")) and b.get("crypto_ready") is not False,
            "me": (b.get("me") or {}).get("fp"),
            "relay": {"url": relay["url"], "key": relay["key"], "ts": relay.get("ts"), "bus_v": relay.get("bus_v")}
            if relay else None,
            "allow": sorted({c["fp"] for c in contacts
                             if c.get("status") in ("known", "muted") and isinstance(c.get("fp"), str)}),
            # the jobs of the relay (the secret part of a job is the daemon's file next to the view, never here)
            "jobs": [{"job": k, "do": j["do"], **{p: j[p] for p in ("invite", "join") if j.get(p)}}
                     for k, j in sorted((b.get("jobs") or {}).items())],
            # the permits of our links to watch on the relay (used by a hub that did not come — the owner is told)
            "watch": sorted(r["invite"] for r in links.values() if r.get("invite") and r.get("posted")
                            and not r.get("invite_used") and r.get("state") in ("open", "used")),
            # whose version to ask the relay: the contacts we write to; `now` — asked again after a miss
            "peers": sorted({c["fp"] for c in contacts if c.get("status") in ("known", "muted")
                             and isinstance(c.get("fp"), str)}),
            "peers_now": sorted({c["fp"] for c in contacts if c.get("peer_ask") and isinstance(c.get("fp"), str)}),
            "out": [{"out": oid, "fp": o["fp"], "id": o.get("id"), "exp": o.get("exp"), "kind": o.get("kind") or "message",
                     "status": o["status"], **({"handed": o["handed"]} if o.get("handed") else {}),
                     **({"remote": o["remote"]} if o.get("remote") else {})}
                    for oid, o in sorted((b.get("out") or {}).items())
                    if o.get("status") in ("signed", "handed") and o.get("envelope") and isinstance(o.get("fp"), str)]}


def undelivered(st):
    """Items without an outcome whose message did not reach the liaison (it was removed, closed or failed when they
    came): once it is back, they go to it again — the item is in the journal, nothing is lost or doubled."""
    from core import liaison
    sess = (st.get("sessions") or {}).get(liaison.NAME)
    if not sess or not sess.get("liaison") or sess["state"] in ("removed", "closed", "failed"):
        return []
    msgs = st.get("messages") or {}
    return [b for b, item in ((st.get("bus") or {}).get("inbox") or {}).items()
            if item.get("outcome") is None and item.get("status") == "ok"
            and (item.get("message") is None or (msgs.get(item["message"]) or {}).get("state") == "failed")]


def due(st, now):
    """The daemon's timers: [(type, data)] — an outgoing envelope past its exp, a held card past a day, an item or a
    request without an outcome past 30 minutes, an item to give the returned liaison again."""
    from core import owner
    b = st.get("bus") or {}
    out = [("core.bus_redeliver", {"inbox": k}) for k in undelivered(st)]
    for oid, o in (b.get("out") or {}).items():
        if o.get("status") in ("signed", "handed") and (o.get("exp") or now + 1) <= now:
            out.append(("core.bus_expired", {"out": oid}))
        elif o.get("status") == "held" and now - (o.get("ts") or now) >= owner.ASK_TTL:
            out.append(("core.bus_expired", {"out": oid}))
    for k, o in (b.get("offers") or {}).items():   # an invitation the owner did not press in a day or past its term
        if o.get("state") == "open" and (now - (o.get("ts") or now) >= owner.ASK_TTL or (o.get("exp") or now + 1) <= now):
            out.append(("core.bus_offer_ended", {"id": k, "why": "expired"}))
    for k, item in (b.get("inbox") or {}).items():
        if item.get("status") == "held" and item.get("outcome") is None and now - (item.get("ts") or now) >= owner.ASK_TTL:
            out.append(("core.bus_held_expired", {"inbox": k}))
    for key in ("inbox", "requests"):
        for k, item in (b.get(key) or {}).items():
            if item.get("outcome") is None and not item.get("stale") and item.get("status", "ok") == "ok" \
                    and (start := _clock(st, item, now)) is not None and now - start >= STALE:
                out.append(("core.bus_stale", {"item": k, "contact": item.get("contact")}))
    for k, j in (b.get("joins") or {}).items():
        # B: no answer of A (or no introduction made at all) in the link's term and an hour
        if j.get("state") in ("active", "cancelling") and now > (j.get("exp") or now) + JOIN_GRACE:
            out.append(("core.bus_join_step", {"id": k, "step": "expired"}))
        # a contact the join made and A never confirmed: it goes with the join's record (a late ack is taken until then)
        elif j.get("state") == "expired" and j.get("made") and j.get("intro_id") \
                and now > (j.get("done") or now) + LINKS_KEEP - 3600:
            out.append(("core.bus_join_step", {"id": k, "step": "forget"}))
    for k, r in (b.get("links") or {}).items():   # A: the permit used by a hub that did not come with the link
        u = r.get("invite_used") or {}
        if u.get("fp") and not r.get("misused") and u["fp"] != r.get("used_fp") \
                and (r.get("state") != "open" or now - u.get("ts", now) >= MISUSE_AFTER):
            out.append(("core.bus_link_misused", {"id": k, "fp": u["fp"]}))
    contacts = b.get("contacts") or {}
    for oid, o in (b.get("out") or {}).items():   # a v2 message without an answer: one miss for the contact's version
        if o.get("cls") and o.get("status") == "handed" and not o.get("remote") and not o.get("unanswered") \
                and o.get("contact") in contacts and now - (o.get("handed") or now) >= UNANSWERED:
            out.append(("core.bus_v2_unanswered", {"name": o["contact"], "out": oid}))
    return out
