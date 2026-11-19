"""The daemon's side of the hub bus (spec hubbus §5.6–5.8, §5.13): the decisions the fold does not make — seal and sign,
the policy for an outgoing message (a card or straight out), the check of an offered envelope, the receipts, the timers,
the liaison's sleep. Every decision becomes a `core.bus_*` fact; the fold reads the facts and checks nothing again.
Nothing here runs while the bus is off; the library is imported only by core/bus.py, on demand.

Called by core/daemon.Hub: execute(hub, action) for the fold's `bus_*` actions, tick(hub) on every tick.
"""
import base64
import hashlib
import importlib
import itertools
import json
import os
import platforms
import secrets
import stat
import threading

from core import busstate, liaison

ACTIONS = ("bus_send", "bus_seal", "bus_offered", "bus_pass", "bus_receipt", "bus_system", "bus_liaison_sleep",
           "bus_link", "bus_link_files", "bus_join", "bus_join_next", "bus_join_drop", "bus_reset",
           "bus_offer", "bus_offer_take")


def in_dir(state_dir):
    """Where a transport puts an envelope as a file before `bus.offered`: the core's own folder."""
    return os.path.join(state_dir, "bus", "in")


# the secrets of linking (spec hubbus/ux §4.1): files 0600 in the core's folder, never the journal or the state — the
# boundary is the OS user, as for the rest of the bus
def link_path(state_dir, lid):
    """A's link as text (the page and the CLI read it; it goes when the link is used, revoked, expired)."""
    return os.path.join(state_dir, "bus", "links", f"{lid}.link")


def join_dir(state_dir):
    """Where the page, the CLI or the bot put a pasted link before `bus.join_requested` (like `bus/in/`)."""
    return os.path.join(state_dir, "bus", "join")


def offer_dir(state_dir):
    """Where a session puts the invitation of another hub it was given before `bus.offer_requested` (like `join_dir`)."""
    return os.path.join(state_dir, "bus", "offer")


def offered_path(state_dir, lid):
    """The invitation the owner has not decided on yet: the core's own copy, checked; it goes with the offer."""
    return os.path.join(state_dir, "bus", "offered", f"{lid}.link")


def joined_path(state_dir, lid):
    """B's copy of the link while its join needs it (the permit for the registration, the one-time key for the
    introduction); it goes once the introduction is made or the join ends."""
    return os.path.join(state_dir, "bus", "joins", f"{lid}.link")


def jobs_dir(state_dir):
    """The secret parts of the relay's jobs (a permit's text, the binding of the transport key), one file each."""
    return os.path.join(state_dir, "bus", "jobs")


def queued_digests(state_dir, types=("bus.join_requested", "bus.offer_requested")):
    """The digests of the links whose request (of these types) still lies in the core's mailbox: unread, without being
    taken away (spool.take would set a broken file aside). The file of such a request is not swept, however long the
    core takes to get to the mailbox, and a repeat of the same paste makes no second request."""
    folder = os.path.join(state_dir, "spool")
    out = set()
    try:
        names = [n for n in os.listdir(folder) if n.endswith(".json")]
    except OSError:
        return out
    for n in names:
        try:
            with open(os.path.join(folder, n), encoding="utf-8") as f:
                item = json.load(f)
        except (OSError, ValueError):
            continue
        data = item.get("data") if isinstance(item, dict) else None
        if isinstance(data, dict) and item.get("type") in types and isinstance(data.get("sha256"), str):
            out.add((item["type"], data["sha256"]))
    return out


def _secret(path, text):
    platforms.current.makedirs_private(os.path.dirname(path))
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _gone(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _read_secret(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def view_path(state_dir):
    return os.path.join(state_dir, "bus", "view.json")


def transport_path(state_dir):
    """The transport interface (busstate.transport_view): what to hand and where, what to take."""
    return os.path.join(state_dir, "bus", "transport.json")


def out_dir(state_dir):
    """The envelopes to hand, one file each (`<out>.env`), next to the transport view: a transport reads these files,
    never the state."""
    return os.path.join(state_dir, "bus", "out")


INTRO_CHECKS = 20       # signature checks of introductions (keys from the envelope itself) a minute, the whole hub
REJECTED_HOUR = 100     # refusals written an hour; the rest — one record with their number


def _keys(hub):
    """The hub's keys, loaded once (and again after a change of the identity)."""
    from core import bus
    me = ((hub.state.get("bus") or {}).get("me") or {}).get("fp")
    cached = getattr(hub, "bus_keys", None)
    if cached is not None and cached.fp == me:
        return cached
    keys, why = bus.ready_keys(hub.state_dir)
    if why:
        raise bus.Error(why[0], why[1])
    if keys.fp != me:
        raise bus.Error("key_changed", f"the keys on disk are {keys.fp}, the core knows {me}")
    hub.bus_keys = keys
    return keys


def _lang(hub):
    from core import i18n
    return i18n.human(hub.config, hub.state)


def execute(hub, action):
    do = action["do"]
    try:
        if do == "bus_system":
            return hub.ensure_dev()
        if do == "bus_liaison_sleep":
            return liaison_sleep(hub)
        if do == "bus_reset":   # the bus is off by now: the identity's files go
            return reset(hub, action)
        bus_state = hub.state.get("bus") or {}
        if do == "bus_link_files":   # the files no longer needed go, with the bus off too (`hub bus off` leaves offers behind)
            return link_files(hub, action)
        if not bus_state.get("enabled"):
            return None
        return {"bus_send": send, "bus_seal": seal, "bus_offered": offered, "bus_pass": pass_on,
                "bus_receipt": receipt, "bus_link": link, "bus_link_files": link_files, "bus_join": join,
                "bus_join_next": join_next, "bus_join_drop": join_drop, "bus_offer": offer,
                "bus_offer_take": offer_take}[do](hub, action)
    except Exception as e:  # noqa: BLE001 — a failure of the bus is a refused step, not a core crash
        print(f"hubd: bus, {do}: {type(e).__name__}: {e}", flush=True)
        if do in ("bus_send", "bus_seal") and action.get("out"):
            hub.record("core.bus_send_refused", None, {"out": action["out"], "code": "error",
                                                       "reason": f"{type(e).__name__}: {e}"[:300],
                                                       **({"of": action["of"]} if action.get("of") else {})})
        if do in ("bus_link", "bus_join", "bus_offer", "bus_offer_take") and action.get("of"):
            hub.record("core.bus_request_failed", None, {"of": action["of"], "code": "error",
                                                         "reason": f"{type(e).__name__}: {e}"[:300]})
        return None


def send(hub, action):
    """The policy of an outgoing message: the liaison decides what to send, the core puts no card of its own — it keeps
    the secrets check and the size, and a refusal goes back to the liaison and the asking session as
    text (busstate._refused), never to the owner."""
    from core import redact
    b = hub.state["bus"]
    oid = action["out"]
    o = b["out"].get(oid) or {}
    if o.get("status") != "requested":
        return None
    finding = "secret-like" if redact.mask(o.get("text") or "") != (o.get("text") or "") else None
    if o.get("press"):   # «Tell the hub»: the owner's press is his word — no card; a key-like text he confirms
        if finding and not o.get("confirm"):
            hub.record("core.bus_send_refused", None, {"out": oid, "code": "secret", "of": action.get("of")})
            return None
        return _seal(hub, oid, "owner", of=action.get("of"))
    if o.get("for") == "owner":   # the owners' chat: the owner's press is his word — no card; a key-like text he confirms
        contact = b["contacts"].get(o["contact"]) or {}
        code = ("old_hub" if busstate.effective_v(b, contact) < 2 else "secret" if finding and not o.get("confirm")
                else None)
        if code:
            hub.record("core.bus_send_refused", None, {"out": oid, "code": code, "of": action.get("of")})
            return None
        return _seal(hub, oid, "owner", of=action.get("of"))
    code = "secret" if finding else busstate.send_refusal(b, oid, hub.clock())
    if code:
        hub.record("core.bus_send_refused", None, {"out": oid, "code": code, "of": action.get("of")})
        return None
    return _seal(hub, oid, "auto", of=action.get("of"))


def seal(hub, action):
    o = hub.state["bus"]["out"].get(action["out"]) or {}
    if o.get("status") not in ("requested", "held"):
        return None
    return _seal(hub, action["out"], o.get("approved") or "owner", of=action.get("of"))


def _seal(hub, oid, approved, of=None):
    from core import bus
    b = hub.state["bus"]
    o = b["out"][oid]
    contact = b["contacts"].get(o["contact"])
    owner = o.get("for") == "owner"
    if contact is None or contact.get("status") not in (("known", "muted") if owner else ("known",)):
        hub.record("core.bus_send_refused", None, {"out": oid, "code": "no_contact", **({"of": of} if of else {})})
        return None
    if not owner and not contact.get("verified"):   # the gate of the check, again at sealing (an old card answered)
        hub.record("core.bus_send_refused", None, {"out": oid, "code": "not_verified", **({"of": of} if of else {})})
        return None
    keys = _keys(hub)
    # the thread the core chose by itself must still take the answer for this session: another session may have
    # become its source while the message waited — then a new thread, decided before the envelope is built
    new = bool(o.get("thread_auto") and not busstate.auto_thread_ok(b, o.get("thread"), o["contact"], busstate.whose(o),
                                                                     hub.clock()))
    inner = bus.message_inner(b, {**o, "thread": None} if new else o)
    v2 = busstate.effective_v(b, contact) >= 2
    if owner:
        inner = {**inner, "for": "owner", "agent_from": "owner"}
    cls = bus.class_of(inner) if v2 else None
    mid, raw = bus.seal(keys, contact, inner, hub.clock(), cls=cls)
    head = json.loads(json.loads(raw)["head"])
    hub.record("core.bus_out", None, {"out": oid, "kind": "message", "id": mid, "contact": o["contact"],
                                      "fp": contact["fp"], "exp": head["exp"], "approved": approved,
                                      "seq": inner["seq"], "size": len(raw), **({"cls": cls} if cls else {}),
                                      "envelope": base64.b64encode(raw).decode("ascii"), **({"of": of} if of else {}),
                                      **({"thread_new": True} if new else {})})
    return None


def _mid(raw):
    return json.loads(json.loads(raw)["head"])["id"]


def _put_out(hub, kind, name, fp, mid, raw, **extra):
    """A one-shot envelope out (bus.out_fact: one builder of the fact for the daemon and the owner's requests)."""
    from core import bus
    hub.record("core.bus_out", None, bus.out_fact(kind, name, fp, mid, raw, **extra))


def receipt(hub, action):
    """The receipt after the owner decided on a held incoming message: `ok` (passed to the liaison) or refused."""
    from core import bus
    b = hub.state["bus"]
    item = b["inbox"].get(action["inbox"]) or {}
    contact = b["contacts"].get(item.get("contact") or "")
    if not contact:
        return None
    mid, raw = bus.receipt(_keys(hub), contact, item["id"], action["status"], hub.clock(), code=action.get("code"),
                           cls=item.get("cls"))
    _put_out(hub, "receipt", item["contact"], contact["fp"], mid, raw, inbox=action["inbox"], status=action["status"])
    return None


def intro_to(hub, name):
    """Our card to a contact the owner has just accepted: its hub shows the owner "this hub asks to be a contact"."""
    from core import bus
    b = hub.state["bus"]
    contact = b["contacts"].get(name) or {}
    me = b.get("me") or {}
    keys = _keys(hub)
    raw = bus.intro_envelope(keys, bus.hub_label(me, hub.config), hub.clock(), agents=exposed(hub.config),
                             relays=relays_of(b))
    _put_out(hub, "intro", name, contact.get("fp"), _mid(raw), raw)


def exposed(config):
    """The agents the hub names in its card for the advanced way (`bus.exposed` of hub.json: {session: what it does});
    one place for `hub bus card` and the introduction. A link's card names none: the catalog goes to a checked contact
    by `card_update` (spec hubbus/ux §9.2)."""
    from core import bus
    b = (config or {}).get("bus") if isinstance(config, dict) else None
    agents = b.get("exposed") if isinstance(b, dict) else None
    if not isinstance(agents, dict):
        return None
    return {k: v for k, v in agents.items() if isinstance(k, str) and bus.AGENT.fullmatch(k) and bus._text(v, 200)} \
        or None


def relays_of(bus_state):
    """The relays named in the hub's card: the one the owner joined (its address and key came in the invitation)."""
    relay = bus_state.get("relay") or {}
    return [relay["ref"]] if relay.get("ref") else []


def offered(hub, action):
    """An envelope a transport put into the core's folder (§5.8): an ordinary file inside `<state>/bus/in/`, not bigger
    than an envelope, the digest the transport named; read without blocking and copied, then checked by
    bus.receive; the verdict is written first, the receipt goes out after it; the file goes after the verdict."""
    from core import bus, buscards
    folder = os.path.realpath(in_dir(hub.state_dir))
    path = os.path.realpath(str(action.get("path") or ""))
    code = None
    raw = b""
    if os.path.dirname(path) != folder:
        code = "outside"
    else:
        try:
            st = os.lstat(path)
            if not stat.S_ISREG(st.st_mode) or st.st_size > bus.ENVELOPE_MAX:
                code = "not_a_file"
            else:
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_BINARY", 0))
                with os.fdopen(fd, "rb") as f:
                    raw = f.read(bus.ENVELOPE_MAX + 1)
                if len(raw) > bus.ENVELOPE_MAX or hashlib.sha256(raw).hexdigest() != action.get("sha256"):
                    code = "bad_offer"
        except OSError:
            code = "not_a_file"
    if code:
        hub.record("core.bus_rejected", None, {"code": code, "size": action.get("size"), "sha256": action.get("sha256"),
                                               "offer": action.get("of")})
        return None
    b = hub.state["bus"]
    now = hub.clock()
    v = None
    if _intro_over(hub, raw, now):   # a stranger's key is checked at most INTRO_CHECKS times a minute
        v = {"code": "rate", "fact": ("core.bus_rejected", {"code": "rate", "limit": "intro", "size": len(raw),
                                                            "sha256": hashlib.sha256(raw).hexdigest()}), "reply": None}
    v = v or bus.receive(b, _keys(hub), raw, now)
    if v["fact"] and v["fact"][0] == "core.bus_rejected":
        data = v["fact"][1]
        knock = data.get("code") == "unknown_hub"
        if not data.get("contact") and _rejected_over(hub, now, knock=knock):
            v = {**v, "fact": None}   # counted: one record with the number at the next refusal after the hour
        elif knock:   # after _rejected_over: the count of a past hour is already in the state
            notice = busstate.knock_notice(b, data.get("hub"), now)
            quiet = (b.get("knocks") or {}).get("quiet", 0) if notice else 0
            v = {**v, "fact": ("core.bus_rejected", {**data, "notice": notice, **({"quiet": quiet} if quiet else {})})}
    if v["fact"]:
        type_, data = v["fact"]
        if type_ == "core.bus_intro":
            taken = set(b["contacts"]) | {r.get("name") for f, r in (b.get("intros") or {}).items()
                                          if r.get("card") and f != data.get("fp")}
            name = bus.local_name(data.get("label"), taken, data.get("fp"), b.get("gone"))
            data = {**data, "name": name, "suggest": _suggest(hub, data.get("label"), name)}
            data["card"] = buscards.intro(_lang(hub), data, name)
        hub.record(type_, None, {**data, "offer": action.get("of")})   # `of` of a receipt is the envelope it confirms
        if type_ == "core.bus_rejected" and data.get("notice"):
            _knock(hub, data)
        _after(hub, type_, data)
    if v["reply"]:
        contact = b["contacts"].get((v["fact"] or (None, {}))[1].get("contact") or "") or {}
        _put_out(hub, "receipt", (v["fact"] or (None, {}))[1].get("contact"), contact.get("fp"), _mid(v["reply"]),
                 v["reply"])
    try:
        os.unlink(path)
    except OSError:
        pass
    return None


def _intro_over(hub, raw, now):
    """An introduction (its key comes in the envelope) over the minute's checks: refused before ssh-keygen runs."""
    try:
        t = json.loads(json.loads(raw)["head"]).get("t")
    except (ValueError, TypeError, KeyError, AttributeError):
        return False
    if t != "intro":
        return False
    minute = int(now) // 60
    m, n = getattr(hub, "bus_intro_checks", (None, 0))
    n = n + 1 if m == minute else 1
    hub.bus_intro_checks = (minute, n)
    return n > INTRO_CHECKS


def _rejected_over(hub, now, knock=False):
    """A refusal of a stranger's envelope (no contact): REJECTED_HOUR are written an hour, the rest are counted; the
    count goes out as one record at the first refusal of a later hour (the knocks among them — into the next notice)."""
    hour = int(now) // 3600
    h, n, quiet, knocks = getattr(hub, "bus_rejected_hour", (hour, 0, 0, 0))
    if h != hour:
        if quiet:
            hub.record("core.bus_rejected", None, {"code": "many", "n": quiet, "hour": h,
                                                   **({"knocks": knocks} if knocks else {})})
        h, n, quiet, knocks = hour, 0, 0, 0
    over = n >= REJECTED_HOUR
    hub.bus_rejected_hour = (h, n + (not over), quiet + over, knocks + (over and knock))
    return over


def _knock(hub, data):
    """«An unknown hub knocks» to the owner (spec §5.5): its fingerprint and the size, never the content; the knocks
    kept quiet since the last notice are counted in it."""
    from core import i18n
    quiet = data.get("quiet") or 0
    lang = _lang(hub)
    hub.record("owner.notify", None, {"title": i18n.tr(lang, "core.bus.knock_title"),
                                      "text": i18n.tr(lang, "core.bus.knock_text", fp=data.get("hub"),
                                                      size=data.get("size") or 0, more=quiet),
                                      "source": "bus", "channel": "core", "owner": False})


def _suggest(hub, label, name):
    """The "looks like" people of the contacts book for an introduction card (§5.5.1 p. 4d): a whole word of the label
    or the local name equal to a name or an alias, case aside, up to three; only a hint — nothing is linked by itself."""
    try:
        from modules.contacts import contacts
        rows = contacts.people(contacts.default_path())
    except Exception:  # noqa: BLE001 — a broken book gives no hints, the card goes without them
        return []
    words = {w.casefold() for w in str(label or "").replace("«", " ").replace("»", " ").split() + [name] if len(w) > 1}
    out = []
    for r in rows:
        keys = {k.casefold() for k in [r["name"], *r["aliases"]] for k in [k, *k.split()]}
        if words & keys and r["name"] not in out:
            out.append(r["name"])
    return out[:3]


# --- linking two hubs (spec hubbus/ux §4.1–4.5) ----------------------------------------------------------------------

def _notify(hub, title, text, about=None, **kw):
    """A notice to the owner; `about` — what it is about for a channel that shows it its own way (Telegram: the pair
    code with «Matches», a reply to the other owner): {kind, contact?, id?, code?, label?}."""
    from core import i18n
    lang = _lang(hub)
    hub.record("owner.notify", None, {"title": i18n.tr(lang, title, **kw), "text": i18n.tr(lang, text, **kw),
                                      "source": "bus", "channel": "core", "owner": False,
                                      **({"bus": about} if about else {})})


def _after(hub, type_, data):
    """What a verdict on an incoming envelope brings next: A's answer to B, the owner's notices."""
    from core import bus
    b = hub.state["bus"]
    if type_ in ("core.bus_link_used", "core.bus_link_ack_wanted"):
        link_acks(hub)
    if type_ == "core.bus_link_used":
        name, c = busstate.by_fp(b, data.get("fp"))
        code = bus.contact_code(c)
        if code and c.get("pair") == data.get("id"):   # a link that lost to the pair (crossed, late): no second notice
            label = (data.get("card") or {}).get("label") or ""
            _notify(hub, "core.bus.linked_title", "core.bus.linked_text", label=label, code=code,
                    about={"kind": "linked", "contact": name, "id": data.get("id"), "code": code, "label": label})
    elif type_ == "core.bus_link_refused":
        _link_answer(hub, data["fp"], "link_refused", {"id": data["id"], "fpB": data["fp"], "code": data["code"]},
                     label=(b["links"].get(data["id"]) or {}).get("label"))
    elif type_ == "core.bus_join_step" and data.get("step") == "linked":
        j = b["joins"].get(data.get("id")) or {}
        name, c = busstate.by_fp(b, j.get("fp_a"))
        code = bus.contact_code(c)
        if j.get("state") == "linked" and code and c.get("pair") == data.get("id"):
            label = j.get("label") or ""
            _notify(hub, "core.bus.linked_title", "core.bus.linked_text", label=label, code=code,
                    about={"kind": "linked", "contact": name, "id": data.get("id"), "code": code, "label": label})
    elif type_ == "core.bus_join_step" and data.get("step") in ("refused", "expired", "forget"):
        # B's link did not come about after its owner stopped watching (A refused later, the term is over): say so —
        # once, when nothing can come back (an expired join whose introduction went still takes A's late answer)
        from core import busflow
        j = b["joins"].get(data.get("id")) or {}
        if j.get("state") == "refused" or j.get("state") == "expired" and not j.get("intro_id") \
                or j.get("state") == "forgotten":
            code, label = j.get("code") or "expired", j.get("label") or ""
            _notify(hub, "core.bus.join_failed_title", "core.bus.join_failed_text", label=label,
                    why=busflow.why(code), about={"kind": "join_failed", "id": data.get("id"), "code": code,
                                                  "label": label})
    elif type_ == "core.bus_contact_removed" and isinstance(data.get("unlinked"), dict):
        label = data["unlinked"].get("label") or ""
        _notify(hub, "core.bus.unlinked_title", "core.bus.unlinked_text", label=label,
                about={"kind": "unlinked", "contact": data.get("name"), "label": label})
    elif (type_ == "core.bus_chat_received" or type_ == "core.bus_received" and _is_answer(b, data)) \
            and data.get("status") == "ok":
        # the other hub's words to this owner (an old release's owners' chat, or the answer in a thread of his own
        # words): the page and Telegram show them (a text of another hub, not an order)
        from core import i18n
        c = b["contacts"].get(data.get("contact")) or {}
        hub.record("owner.notify", None, {"title": i18n.tr(_lang(hub), "core.bus.chat_title",
                                                           label=busstate.shown(c) or data.get("contact")),
                                          "text": str(data.get("text") or "")[:3000], "source": "bus",
                                          "channel": "core", "owner": False,
                                          "bus": {"kind": "chat", "contact": data.get("contact"),
                                                  "label": busstate.shown(c) or data.get("contact")}})


def _is_answer(b, data):
    """The message the fold has just taken is an answer in a thread of the owner's own words (it is among the last items
    of the inbox: the fold has just put it there)."""
    mid = data.get("id")
    return bool(mid) and any(x.get("reply") and x.get("id") == mid for x in itertools.islice(reversed(b["inbox"].values()), 20))


def _link_answer(hub, fp, field, value, id=None, label=None):
    """A's introduction with a link field for the hub `fp`: our card (no catalog) and the answer — one-shot, open by
    design (the relay takes an introduction from anyone registered; B pinned our key from the link). An ack goes with
    the id `id` — the nonce the link committed to; a refusal reveals nothing (a random id)."""
    from core import bus
    b = hub.state["bus"]
    keys = _keys(hub)
    mid, raw = bus.link_intro(keys, label or bus.hub_label(b.get("me"), hub.config), hub.clock(), field, value,
                              relays=relays_of(b), id=id)
    name, _ = busstate.by_fp(b, fp)
    _put_out(hub, "intro", name, fp, mid, raw, **({"link_ack": value["id"]} if field == "link_ack" else {}))


def link_acks(hub):
    """Every used link whose ack is not out yet gets it (after the fact, and again at the next tick after a cut)."""
    for lid, rec in sorted((hub.state["bus"].get("links") or {}).items()):
        ack = rec.get("ack")
        if ack and rec.get("state") == "used" and rec.get("nonce"):
            _link_answer(hub, ack["fp"], "link_ack", {"id": lid, "fpB": ack["fp"], "intro_id": ack["intro"]},
                         id=rec["nonce"], label=rec.get("label"))


def link(hub, action):
    """«Link with a hub» on A: the one-time key, the permit for the relay (if the hub is on one), the link — its file
    and the permit's job file first, the fact next (a cut leaves files the tick removes, never a record without them;
    the permit goes to the relay only from the record)."""
    from core import bus
    b = hub.state["bus"]
    of = action.get("of")
    relay = b.get("relay") or None
    if relay and relay.get("bus_v") is not None and relay["bus_v"] < 2:
        hub.record("core.bus_request_failed", None, {"of": of, "code": "old_relay",
                                                     "reason": "the relay does not take envelopes of version 2"})
        return None
    keys = _keys(hub)
    now = hub.clock()
    label = action.get("label") or bus.hub_label(b.get("me"), hub.config)   # the name for this invitation, else the hub's
    text, rec, invite = bus.make_link(keys, label, now, relay=relay,
                                      ttl=action.get("ttl") or bus.LINK_TTL)
    _secret(link_path(hub.state_dir, rec["id"]), text)
    if invite:
        _secret(os.path.join(jobs_dir(hub.state_dir), f"i{rec['id'][:16]}.json"), json.dumps({"invite": invite}))
    hub.record("core.bus_link_made", None, {"id": rec["id"], "pub": rec["pub"], "exp": rec["exp"],
                                            "invite": rec["invite"], "nonce": rec["nonce"],
                                            "made_at": rec["made_at"], "of": of,
                                            **({"label": action["label"]} if action.get("label") else {}),
                                            **({"person": action["person"]} if action.get("person") else {})})
    return None


def _link_text(folder, action):
    """The link in the file a caller put in the core's folder: (raw bytes, None), or (None, the refusal code). An ordinary
    file inside `folder` of the size a link has, read without blocking, the digest the caller named."""
    from core import bus
    path = os.path.realpath(str(action.get("path") or ""))
    if os.path.dirname(path) != os.path.realpath(folder):
        return None, "outside"
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_size > bus.LINK_MAX:
            return None, "not_a_file"
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_BINARY", 0))
        with os.fdopen(fd, "rb") as f:
            raw = f.read(bus.LINK_MAX + 1)
    except OSError:
        return None, "not_a_file"
    if hashlib.sha256(raw).hexdigest() != action.get("sha256"):
        return None, "bad_offer"
    return raw, None


def _checked_link(hub, raw):
    """What B does before it takes a link, for a pasted one and for one a session brought: (the link as bus.read_link
    gives it, the relay it names or None, None) or (None, None, (code, reason)) — own link, a blocked hub, a link pasted
    before, another relay than this hub's."""
    from core import bus
    b = hub.state["bus"]
    try:
        got = bus.read_link(raw.decode("ascii"), hub.clock())
    except UnicodeDecodeError:
        return None, None, ("bad_link", "")
    except bus.Error as e:
        return None, None, (e.code, e)
    card = got["card"]
    if card["fp"] == (b.get("me") or {}).get("fp"):
        return None, None, ("own_link", "")
    _, known = busstate.by_fp(b, card["fp"])
    if known is not None and known.get("status") == "blocked":
        return None, None, ("blocked", "")
    if busstate.pasted((b.get("joins") or {}).get(got["id"])):
        return None, None, ("pasted", "")
    relay = None
    if got["invite"]:
        inv = got["invite"]
        from core import busrelay
        relay = {"url": inv["relay"], "key": inv["key"], "ref": busrelay.ref(inv["relay"], inv["key"]),
                 "invite": inv["id"], "by": inv["by"]}
        mine = b.get("relay") or {}
        if mine and mine.get("key") != relay["key"]:
            return None, None, ("other_relay", f"the link leads to {relay['url']}, this hub is on {mine.get('url')}")
    return got, relay, None


def join(hub, action):
    """B's owner pasted a link (a file 0600 in the core's folder, like an offered envelope): read and checked; refused
    — the reason for the owner and the file goes; taken — the record of the join, the file moves to the join's own."""
    of = action.get("of")
    folder = join_dir(hub.state_dir)
    path = os.path.realpath(str(action.get("path") or ""))

    def failed(code, reason=""):
        hub.record("core.bus_request_failed", None, {"of": of, "code": code, "reason": str(reason)[:300]})
        if os.path.dirname(path) == os.path.realpath(folder):
            _gone(path)

    raw, code = _link_text(folder, action)
    if code:
        return failed(code)
    got, relay, refusal = _checked_link(hub, raw)
    if refusal:
        return failed(*refusal)
    card = got["card"]
    platforms.current.makedirs_private(os.path.dirname(joined_path(hub.state_dir, got["id"])))
    os.replace(path, joined_path(hub.state_dir, got["id"]))
    hub.record("core.bus_join", None, {"id": got["id"], "fp_a": card["fp"], "label": card["label"], "exp": got["exp"],
                                       "card": {k: card[k] for k in busstate.CONTACT_KEYS}, "relay": relay,
                                       "commit": got["commit"], "made_at": got["made_at"], "of": of,
                                       **({"person": action["person"]} if action.get("person") else {})})
    return None


def offer(hub, action):
    """A session brought the invitation of another hub (a file 0600 in the core's `bus/offer/`): read and checked as a
    pasted one is; the core keeps its own copy and puts a card to the owner (busstate, kind «offer») — nothing else
    happens until he presses «Link». Refused: the reason goes to the session; the same invitation already waiting is
    `offered` (no second card)."""
    from core import buscards
    b = hub.state["bus"]
    of = action.get("of")
    folder = offer_dir(hub.state_dir)
    path = os.path.realpath(str(action.get("path") or ""))

    def failed(code, reason=""):
        hub.record("core.bus_request_failed", None, {"of": of, "code": code, "reason": str(reason)[:300]})
        if os.path.dirname(path) == os.path.realpath(folder):
            _gone(path)

    raw, code = _link_text(folder, action)
    if code:
        return failed(code)
    got, relay, refusal = _checked_link(hub, raw)
    if refusal:
        return failed(*refusal)
    old = (b.get("offers") or {}).get(got["id"]) or {}
    if old.get("state") == "open":
        return failed("offered")
    if old.get("state") == "declined" and hub.clock() - (old.get("done") or 0) < 86400:   # «No» a day ago at most:
        return failed("owner_said_no")   # a session does not ask again (the record goes with a later offer)
    from core import reducer
    if liaison.NAME not in hub.state["sessions"] and not reducer._hub(hub.state):
        return failed("no_card")
    if len([o for o in (b.get("offers") or {}).values() if o.get("state") == "open"]) >= busstate.OFFER_MAX:
        return failed("offers_full")
    card = got["card"]
    platforms.current.makedirs_private(os.path.dirname(offered_path(hub.state_dir, got["id"])))
    os.replace(path, offered_path(hub.state_dir, got["id"]))
    info = {"label": card["label"], "fp": card["fp"], "code": card["code"], "exp": got["exp"],
            "relay": relay["url"] if relay and not b.get("relay") else None,   # «Link» gives a hub without a relay this one
            "by": action.get("by")}
    hub.record("core.bus_offer", None, {"id": got["id"], "fp": card["fp"], "label": card["label"], "code": card["code"],
                                        "exp": got["exp"], "by": action.get("by"), "of": of,
                                        "card": buscards.offer(_lang(hub), info)})
    return None


def offer_take(hub, action):
    """The owner pressed «Link» on the card: the core's copy goes through the join of a pasted invitation — the same
    file in the same folder, the same checks (a term that ran out meanwhile is a refusal), the same steps after.
    A refusal on the spot is told to the owner, not lost with the card."""
    lid = action["id"]
    src = offered_path(hub.state_dir, lid)
    rec = (hub.state["bus"].get("offers") or {}).get(lid) or {}

    def lost(code, recorded=False):
        from core import busflow
        _notify(hub, "core.bus.offer_failed_title", "core.bus.offer_failed_text", label=rec.get("label") or "",
                why=busflow.why(code))
        if not recorded:   # the session that brought it hears it too (busstate): join wrote nothing of its own
            hub.record("core.bus_request_failed", None, {"of": action.get("of"), "code": code, "reason": ""})

    if rec.get("state") != "taken":
        return lost("lost")
    text = _read_secret(src)

    if not text:
        return lost("lost")
    raw = text.encode("ascii", "replace")
    digest = hashlib.sha256(raw).hexdigest()
    folder = join_dir(hub.state_dir)
    platforms.current.makedirs_private(folder)
    path = os.path.join(folder, digest + ".link")
    _secret(path, text)
    join(hub, {"of": action.get("of"), "path": path, "sha256": digest})
    _gone(src)
    last = hub.state["bus"].get("last") or {}   # join wrote its outcome: core.bus_join «ok» or core.bus_request_failed
    if last.get("of") != action.get("of") or last.get("code") != "ok":
        lost(last.get("code") if last.get("of") == action.get("of") else "error", last.get("of") == action.get("of"))
    return None


def join_next(hub, action):
    """The next step of B's join from the one it recorded (spec §4.3: a cut between two steps repeats from the step
    written): registration on the link's relay (a job of the transport) → the contact → one introduction."""
    from core import bus
    b = hub.state["bus"]
    jid = action["id"]
    j = (b.get("joins") or {}).get(jid)
    if j is None or j.get("state") != "active":
        return None
    step = j.get("step")
    if step == "parsed":
        relay = j.get("relay")
        mine = b.get("relay") or {}
        if relay and mine.get("ref") != relay.get("ref"):
            if mine:
                hub.record("core.bus_join_step", None, {"id": jid, "step": "refused", "code": "other_relay"})
            else:
                join_job(hub, jid)
            return None
        hub.record("core.bus_join_step", None, {"id": jid, "step": "joined"})
    elif step == "joined":
        taken = set(b["contacts"]) | {r.get("name") for r in (b.get("intros") or {}).values() if r.get("card")}
        hub.record("core.bus_join_step", None, {"id": jid, "step": "contact",
                                                "name": bus.local_name(j.get("label"), taken, j.get("fp_a"),
                                                                       b.get("gone"))})
    elif step == "contact":
        join_intro(hub, jid)
    return None


def join_job(hub, jid):
    """The registration of B on the link's relay: the permit and the binding of the transport key by the hub's key go
    to the transport as a job file; the job itself is the fold's (busstate: core.bus_join_step joining)."""
    from core import bus, busrelay
    b = hub.state["bus"]
    j = b["joins"][jid]
    if f"j{jid[:16]}" in (b.get("jobs") or {}):
        return
    text = _read_secret(joined_path(hub.state_dir, jid))
    if not text:
        hub.record("core.bus_join_step", None, {"id": jid, "step": "refused", "code": "lost"})
        return
    got = bus.read_link(text, None)
    keys = _keys(hub)
    key_file, tpub = busrelay.transport_key(make=True)
    bind = bus.ssh_sign(keys.sign_key, busrelay.bind_line(j["relay"]["key"], keys.fp, tpub), busrelay.NS)
    _secret(os.path.join(jobs_dir(hub.state_dir), f"j{jid[:16]}.json"), json.dumps({
        "invite": got["invite_text"], "bind": base64.b64encode(bind.encode("ascii")).decode("ascii"),
        "sign_pub": bus._b64(keys.sign_pub), "enc_pub": bus._b64(keys.enc_pub), "transport_pub": tpub,
        "label": str((b.get("me") or {}).get("label") or "")[:100], "url": j["relay"]["url"], "key": j["relay"]["key"],
        "fp": keys.fp}))
    hub.record("core.bus_join_step", None, {"id": jid, "step": "joining"})


def join_intro(hub, jid):
    """B's one introduction by the link: our card (no catalog) and the proof of the one-time key over A's and our
    fingerprints and this envelope's id; the envelope and the step are one fact (core.bus_out with `join`)."""
    from core import bus
    b = hub.state["bus"]
    j = b["joins"][jid]
    relay = b.get("relay") or {}
    if relay and relay.get("bus_v") is None:
        return None   # the transport has not asked the relay's version yet: the tick tries again
    if relay and relay["bus_v"] < 2:
        hub.record("core.bus_join_step", None, {"id": jid, "step": "refused", "code": "old_relay"})
        return None
    text = _read_secret(joined_path(hub.state_dir, jid))
    if not text:
        hub.record("core.bus_join_step", None, {"id": jid, "step": "refused", "code": "lost"})
        return None
    seed = bus.read_link(text, None)["seed"]
    keys = _keys(hub)
    eid = secrets.token_hex(16)
    sig = bus.link_proof(seed, j["fp_a"], keys.fp, eid, keys.lib)
    mid, raw = bus.link_intro(keys, bus.hub_label(b.get("me"), hub.config), hub.clock(), "link",
                              {"id": jid, "sig": sig}, relays=relays_of(b), id=eid)
    name, _ = busstate.by_fp(b, j["fp_a"])
    _put_out(hub, "intro", name, j["fp_a"], mid, raw, join=jid)
    link_files(hub)
    return None


def join_drop(hub, action):
    """A join ended without a link (refused, cancelled, lost): the contact it made goes — by the ordinary fact, so an
    older core removes it too; after an ack of a cancelled join, A gets the unlink first."""
    from core import bus
    b = hub.state["bus"]
    j = (b.get("joins") or {}).get(action["id"]) or {}
    name, c = busstate.by_fp(b, j.get("fp_a"))
    # only the contact this very join made and nothing re-linked since (another link changes the pair): a later link
    # of the same two hubs keeps its contact
    if c is not None and j.get("made") and c.get("pair") == action["id"]:
        if action.get("unlink") and busstate.effective_v(b, c) >= 2:
            keys = _keys(hub)
            mid, raw = bus.seal(keys, c, {"kind": "unlink", "pair_id": c["pair"]}, hub.clock(), cls="ctl")
            _put_out(hub, "unlink", name, c["fp"], mid, raw)
        hub.record("core.bus_contact_removed", None, {"name": name, "join": action["id"]})
    link_files(hub)
    return None


def reset(hub, action):
    """After `hub bus reset` (the fold dropped the identity): the hub's key files and every secret file of the bus —
    links, pasted links, joins, the relay's jobs, envelopes in and out. The transport key stays: it is bound anew at the
    next registration."""
    from core import bus
    import platforms
    hub.bus_keys = None
    bus.retire_keys(bus.key_dir())
    for folder in (os.path.dirname(link_path(hub.state_dir, "x")), os.path.dirname(joined_path(hub.state_dir, "x")),
                   join_dir(hub.state_dir), offer_dir(hub.state_dir), os.path.dirname(offered_path(hub.state_dir, "x")),
                   jobs_dir(hub.state_dir), in_dir(hub.state_dir), out_dir(hub.state_dir)):
        if os.path.isdir(folder):
            platforms.current.rmtree(folder)
    for path in (transport_path(hub.state_dir), relay_status_path(hub.state_dir)):
        _gone(path)
    hub.bus_view = hub.bus_transport = None
    return None


def link_files(hub, action=None):
    """The secret files of linking that are no longer needed go: A's link once it is not open (used, revoked, expired,
    failed) or past its term; B's copy once the introduction is made or the join ended; a job's file once the job is
    done; files without a record (a cut before the fact) and a pasted link nobody took within an hour."""
    b = hub.state.get("bus") or {}
    now = hub.clock()
    links, joins, jobs = b.get("links") or {}, b.get("joins") or {}, b.get("jobs") or {}
    offers = b.get("offers") or {}
    for folder, keep in ((os.path.dirname(link_path(hub.state_dir, "x")),
                          lambda k: (links.get(k) or {}).get("state") == "open" and (links[k].get("exp") or 0) > now),
                         (os.path.dirname(joined_path(hub.state_dir, "x")),
                          lambda k: (joins.get(k) or {}).get("state") == "active"
                          and joins[k].get("step") in ("parsed", "joined", "contact")),
                         (os.path.dirname(offered_path(hub.state_dir, "x")),
                          lambda k: (offers.get(k) or {}).get("state") in ("open", "taken") and (offers[k].get("exp") or 0) > now),
                         (jobs_dir(hub.state_dir), lambda k: k in jobs)):
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for n in names:
            key = n.rsplit(".", 1)[0]
            if n.endswith(".tmp") or not keep(key):
                _gone(os.path.join(folder, n))
    queued = queued_digests(hub.state_dir)
    for folder, type_ in ((join_dir(hub.state_dir), "bus.join_requested"), (offer_dir(hub.state_dir), "bus.offer_requested")):
        try:   # pasted or brought, and nobody took it in an hour — unless its request still waits in the mailbox
            for n in os.listdir(folder):
                p = os.path.join(folder, n)
                if now - os.lstat(p).st_mtime > 3600 and (type_, n.rsplit(".", 1)[0]) not in queued:
                    _gone(p)
        except OSError:
            pass
    return None


RESUME_EVERY = 30   # a step that failed (a cut, an error) is tried again by the tick at most this often


def linking(hub, now):
    """The tick's part of linking: a join stuck between two steps goes on from the step it wrote, an ack not out yet
    goes, the secret files no longer needed go (once a minute)."""
    b = hub.state["bus"]
    tried = hub.__dict__.setdefault("bus_link_tried", {})
    for jid, j in sorted((b.get("joins") or {}).items()):
        if j.get("state") == "active" and j.get("step") in ("parsed", "joined", "contact") \
                and f"j{jid[:16]}" not in (b.get("jobs") or {}) and now - tried.get(jid, -RESUME_EVERY) >= RESUME_EVERY:
            tried[jid] = now
            try:
                join_next(hub, {"id": jid})
            except Exception as e:  # noqa: BLE001 — the next tick tries again
                print(f"hubd: bus, joining {jid[:8]}: {type(e).__name__}: {e}", flush=True)
    if any(r.get("ack") and r.get("state") == "used" for r in (b.get("links") or {}).values()) \
            and now - tried.get(":ack", -RESUME_EVERY) >= RESUME_EVERY:
        tried[":ack"] = now
        link_acks(hub)
    if now - tried.get(":files", -60) >= 60:
        tried[":files"] = now
        link_files(hub)


def pass_on(hub, action):
    hub.record("core.bus_passed", None, {"inbox": action["inbox"], "to": action["to"], "of": action.get("of"),
                                         **({"note": action["note"]} if action.get("note") else {})})


def liaison_sleep(hub):
    sess = hub.state["sessions"].get(liaison.NAME)
    if liaison.needed(hub.state) or not sess or not sess.get("liaison"):
        return None
    if liaison.NAME in (hub.state.get("system") or []):
        hub.ensure_dev()
    if sess["state"] in ("idle", "held", "parked") and liaison.NAME not in (hub.state.get("system") or []):
        hub.record("core.bus_liaison_sleep", None, {})
    return None


def tick(hub):
    """Every tick: the timers of the bus (expiry, the held card's day, an item without an outcome for 30 minutes) and,
    with the bus off, the liaison's sleep once it is free; the view for commands outside the core."""
    b = hub.state.get("bus")
    if b:
        write_view(hub)
    if not b or not b.get("enabled"):   # also no bus at all and the owner's chats went off (core/chats.py)
        sess = hub.state["sessions"].get(liaison.NAME)
        if sess and sess.get("liaison") and sess["state"] in ("idle", "held", "parked"):
            liaison_sleep(hub)
        return
    heal_lib(hub)
    now = hub.clock()
    told = hub.__dict__.setdefault("bus_told", {})
    linking(hub, now)
    for type_, data in busstate.due(hub.state, now):
        hub.record(type_, None, data)
        _after(hub, type_, data)   # what a timer's fact brings: a notice (a join that did not come about)
        if type_ == "core.bus_link_misused":
            _notify(hub, "core.bus.misused_title", "core.bus.misused_text", fp=data["fp"],
                    about={"kind": "misused", "id": data.get("id")})
        if type_ == "core.bus_stale" and data["item"].startswith("b"):
            # one notice a day per contact to the owner, without the other hub's words
            day = (data.get("contact"), int(now) // 86400)
            if day not in told:
                told[day] = True
                from core import i18n
                hub.record("owner.notify", None, {"title": i18n.tr(_lang(hub), "core.bus.stale_title"),
                                                  "text": i18n.tr(_lang(hub), "core.bus.stale_text",
                                                                  contact=data.get("contact"), item=data["item"]),
                                                  "source": "bus", "channel": "core", "owner": False})


# the library is missing or too old: what the core may put in itself (keys, the self-check — the owner's decision)
HEAL = ("no_lib", "old_lib")


def heal_lib(hub):
    """The environment of the one-line install and of the app (PACKAGED "format": "app") came without cryptography
    before its runtime 2, and a hub moved onto it keeps its bus on: the check at start says no_lib. The core puts in the
    very wheels the release pins for the environment's platform (tools/package/versions.json, core/wheels.pinned) —
    downloaded by their sha256 and size (core/fetch), unpacked into <state>/pylib — once per start, in a thread; then
    the check at start again (core.bus_enabled), or core.bus_key_failed with the reason. No pip, no index: nothing but
    the pinned bytes. A hub from git is not touched (its Python is the owner's: `hub bus on --install`), nor a platform
    with no pinned wheel (Windows on ARM)."""
    job = hub.__dict__.get("bus_heal")
    if job is None:
        if not _broken(hub.state.get("bus") or {}):
            return
        parts = _pinned()
        if not parts:
            hub.bus_heal = {"skip": True}   # nothing to put in on this hub: not asked again in this process
            return
        job = hub.bus_heal = {"done": None}

        def work():
            try:
                job["done"] = _put_wheels(hub.state_dir, parts)
            except Exception as e:  # noqa: BLE001 — the background is not silent: the reason goes into the fact
                job["done"] = ("install_failed", f"{type(e).__name__}: {e}")
        try:
            threading.Thread(target=work, name="bus-heal", daemon=True).start()
        except RuntimeError as e:   # no thread to be had: said, not waited for forever
            job["done"] = ("install_failed", f"{type(e).__name__}: {e}")
        return
    if job.get("skip") or job["done"] is None or job.get("told"):
        return
    job["told"] = True
    if not _broken(hub.state.get("bus") or {}):
        return   # fixed or switched off meanwhile (`hub bus on --install`, `hub bus off`): an old outcome says nothing
    from core import bus
    if job["done"] != "ok":
        code, reason = job["done"]
        hub.record("core.bus_key_failed", None, {"code": code, "reason": str(reason)[:300]})
        return
    bus._LIB = None
    importlib.invalidate_caches()   # pylib may be new to the import system of this process
    try:
        facts = bus.start_check(hub.state["bus"], hub.state_dir)
    except Exception as e:  # noqa: BLE001 — as at start: the check must not stop the tick
        facts = [("core.bus_key_failed", {"code": "error", "reason": f"{type(e).__name__}: {e}"[:300]})]
    if not facts and hub.state["bus"].get("crypto_ready") is False:
        try:   # put in, and still the same refusal (a wheel this machine cannot load): its words, not the old ones
            bus.crypto(hub.state_dir)
        except bus.Unavailable as e:
            facts = [("core.bus_key_failed", {"code": e.code, "reason": f"put into {bus.pylib(hub.state_dir)}, "
                                                                        f"and still: {e}"[:300]})]
    for type_, data in facts:
        hub.record(type_, None, data)


def _broken(b):
    return bool(b.get("enabled")) and b.get("crypto_ready") is False and (b.get("failed") or {}).get("code") in HEAL


def _pinned():
    """The wheels this hub's environment should have carried: its platform (PACKAGED), its Python's minor."""
    import sys
    from core import packaged, wheels
    mark = packaged.info()
    if not packaged.is_app(mark) or not isinstance(mark.get("platform"), str):
        return []
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return wheels.pinned(os.path.join(root, "tools", "package", "versions.json"), mark["platform"],
                         "%d.%d" % sys.version_info[:2])


def _put_wheels(state_dir, parts):
    """The pinned wheels into <state>/pylib, whole or not at all: each downloaded (or already there) by its sha256 and
    size, all unpacked into pylib.new, which then replaces pylib. -> "ok" | (code, reason)."""
    from core import bus, fetch, wheels
    cache = os.path.join(state_dir, "bus", "wheels")
    os.makedirs(cache, exist_ok=True)
    files = []
    for p in parts:
        dest = os.path.join(cache, p["name"])
        if not (os.path.isfile(dest) and os.path.getsize(dest) == p["size"] and fetch.sha256(dest) == p["sha256"]):
            try:
                fetch.download(p["url"], dest, p["sha256"], p["size"], p["name"], "hub restart")
            except fetch.Failed as e:
                return ("no_network" if e.why == "broken" else "install_failed", str(e))
        files.append((dest, p))
    target = bus.pylib(state_dir)
    new = target + ".new"
    if os.path.lexists(new):
        platforms.current.rmtree(new)
    os.makedirs(new)
    try:
        for dest, p in files:
            wheels.install(dest, new, p["version"])
    except wheels.Bad as e:
        return ("install_failed", str(e))
    else:
        wheels.replace_tree(new, target, platforms.current.rmtree)
        platforms.current.rmtree(cache)   # in place: the downloads are not kept
    finally:
        if os.path.lexists(new):   # a refusal, a full disk: no half-written folder is left
            platforms.current.rmtree(new)
    return "ok"


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def write_view(hub):
    """state["bus"] for commands outside the core (busstate.view) and the transport interface (busstate.transport_view
    with the envelope files), written when they change."""
    text = json.dumps(busstate.view(hub.state), ensure_ascii=False, sort_keys=True)
    if getattr(hub, "bus_view", None) != text:
        _write(view_path(hub.state_dir), text)
        hub.bus_view = text
    view = busstate.transport_view(hub.state)
    text = json.dumps(view, ensure_ascii=False, sort_keys=True)
    if getattr(hub, "bus_transport", None) == text:
        return
    write_envelopes(hub.state_dir, hub.state, view)   # a job's file is written before its fact (link, join_job)
    _write(transport_path(hub.state_dir), text)
    hub.bus_transport = text


def write_envelopes(state_dir, st, view):
    """`<out>.env` for every envelope the view names, written before the view (a transport never sees a name without
    its file); files of envelopes no longer to hand go."""
    folder = out_dir(state_dir)
    os.makedirs(folder, exist_ok=True)
    want = {o["out"] for o in view["out"]}
    out = (st.get("bus") or {}).get("out") or {}
    for oid in want:
        path = os.path.join(folder, f"{oid}.env")
        if not os.path.exists(path):
            raw = base64.b64decode(out[oid]["envelope"])
            with open(path + ".tmp", "wb") as f:
                f.write(raw)
            os.replace(path + ".tmp", path)
    for name in os.listdir(folder):
        if not (name.endswith(".env") and name[:-4] in want):
            try:
                os.unlink(os.path.join(folder, name))
            except OSError:
                pass


def relay_status_path(state_dir):
    """What the relay transport says about itself (modules/bus_relay): the last success, the last error, the waiting."""
    return os.path.join(state_dir, "bus", "relay-status.json")


def read_relay_status(state_dir):
    try:
        with open(relay_status_path(state_dir), encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, dict) else None
    except (OSError, ValueError):
        return None


def read_transport(state_dir):
    """The transport view written by the core, or None."""
    try:
        with open(transport_path(state_dir), encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, dict) else None
    except (OSError, ValueError):
        return None


def read_view(state_dir):
    """The view written by the core, or None (the bus was never on): shown, never trusted."""
    try:
        with open(view_path(state_dir), encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, dict) else None
    except (OSError, ValueError):
        return None
