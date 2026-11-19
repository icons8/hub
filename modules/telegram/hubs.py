"""Linking hubs from the owner's Telegram (spec hubbus/ux §3.4): /link on hub A — one message with the link, to forward
to the bot of the other hub; a forwarded or pasted link (or a relay invitation) on hub B — a preview card with «Link»;
«Linked» with the pair code and «Matches»; the other hub's words to the owner with «Reply» — a reply goes to that hub's liaison as the
owner's own words (no card, no turn of this hub's model). The core's other bus notices go as they are (🔔).

The owner's path is core/busflow.py, the same as the terminal's and the page's: this module only gives it buttons. The
slow part (an install, the core's answers, B's steps until A answers) runs in a worker thread; what it brings is shown
from the bot's loop, in order, and retried on a network failure. A link is a secret: between the preview and the press
it stays in this process's memory (not the bot's state file, not a log), it never goes to the hub as a message, and its
messages leave the chat once the hubs are linked (a bot may delete messages of its private chat for 48 hours)."""
import hashlib
import html
import os
import queue
import re
import secrets
import sys
import threading
import uuid

from core import bus, busflow, busrelay, i18n, journal, liaison, redact, spool

KEY_TTL = 3600       # «Link» of a preview, a held chat text: an hour at most (and never past the link's term)
CONSENT_TTL = 600    # «Enable and make a link»
KEEP_LINKS = 50      # links whose messages are still to leave the chat
KEEP_KEYS = 100      # buttons of the bus the state file keeps
TTLS = {"1h": 3600, "24h": 86400, "7d": 604800}
# a join's refusals after which the link is of no use here: its messages leave the chat (the rest — a network failure, an
# install — leave it for another press)
SPENT = ("used", "revoked", "expired", "stale", "bad_link", "bad_invite", "own_link", "own_card", "pasted", "blocked",
         "bad_signature", "cancelled", "forgotten", "refused", "other_relay", "relay_other")
TEXT_MAX = 4096      # a Telegram message
EXCLUSIVE = ("link", "join")   # one at a time: they set the bus up (an install, the keys, the relay)
INVITE = re.compile(re.escape(busrelay.INVITE_PREFIX) + r"\S+")


def mask(text):
    """The text with every bus link and relay invitation hidden: a quote of the owner's reply, a message the bot could
    not parse — whatever may go on to the hub."""
    text = redact.mask_links(str(text or ""))
    return INVITE.sub(busrelay.INVITE_PREFIX + "…", text) if busrelay.INVITE_PREFIX in text else text


def secret(text):
    """Does the text carry a link of the bus or a relay invitation (anywhere, after a card too)?"""
    return find(text)[0] is not None


def find(text):
    """(kind, token) of the first link or relay invitation in a text — a card before it does not hide it (busflow.find
    names only the first token of any kind); (None, None) if there is none."""
    text = str(text or "")
    starts = sorted(m.start() for m in re.finditer(re.escape(bus.LINK_PREFIX) + "|" + re.escape(busrelay.INVITE_PREFIX),
                                                   text))
    for i in starts:   # each place a prefix stands: the token there, if it is one (a bare mention of the prefix — on)
        kind, token = busflow.find(text[i:])
        if kind in ("link", "invite") and text.startswith(token[:len(bus.LINK_PREFIX)], i):
            return kind, token
    return None, None


def invite_id(token):
    return "inv:" + hashlib.sha256(token.encode()).hexdigest()[:16]


def esc(text):
    return html.escape(str(text or ""), quote=False)


class Hubs:
    """The bus part of one bot. put/current/note/idle/config_dir — the Flow's (tests give a stand core's)."""

    def __init__(self, bot):
        self.bot = bot
        self.secrets = {}           # key → (text, until): pasted links, held chat texts — this process only
        self.out, self.hold = [], 0  # what to show, in order: a network failure — again at the next tick
        self.done = queue.Queue()   # what the worker brought: callables for the bot's loop
        self.sync = False           # tests: a job runs in the caller
        self.busy = None            # the running set-up job (link, join)
        self.rejected = {}
        self.put = self._put
        self.current = lambda: journal.load_state(bot.state_dir)
        self.note = lambda type_, data: bot.put(type_, None, data)
        self.idle, self.config_dir = None, None
        bot.st.setdefault("bus_msgs", {})
        bot.st.setdefault("bus_cards", {})   # link id → the card of a join that waits for A's answer

    # --- the core -----------------------------------------------------------------------------------------------------

    def _put(self, type_, entity, data, say=False):
        """The owner's request from Telegram and the core's record of it (as `hub` in the terminal): its number."""
        req = uuid.uuid4().hex[:12]
        path = os.path.join(self.bot.state_dir, "journal.jsonl")
        start = journal.mark(path)
        spool.put(self.bot.spool, type_, entity, {**data, "channel": "telegram", "owner": True, "req": req},
                  src="telegram")
        seq, why = journal.decided(path, req, busflow.WAIT, start=start)
        if why:
            self.rejected[seq] = why   # as the core gave it: busflow.why says its phrases in the bot's language
        return seq

    def flow(self, say=None):
        return busflow.Flow(self.put, self.current, self.bot.state_dir, rejected=self.rejected, note=self.note,
                            say=say, idle=self.idle, config_dir=self.config_dir)

    def bus(self):
        return self.bot.reader.state.get("bus") or {}

    def run(self, what, work, then, say=None):
        """work(flow) → Done in the worker, then(done) in the bot's loop. A set-up job while another goes — False."""
        if what in EXCLUSIVE:
            if self.busy:
                return False
            self.busy = what

        def go():
            try:
                got = work(self.flow(say))
            except Exception as e:  # noqa: BLE001 — a failure of the path is a line to the owner, never a dead bot
                sys.stderr.write(f"telegram: hub link, {what}: {type(e).__name__}\n")   # no text: it may be a link
                got = busflow.Done("error", busflow.why("error", type(e).__name__))
            finally:
                if what in EXCLUSIVE:
                    self.busy = None
            self.done.put(lambda: then(got))
        if self.sync:
            go()
            self.tick()
        else:
            threading.Thread(target=go, daemon=True).start()
        return True

    # --- the bot's loop -----------------------------------------------------------------------------------------------

    def tick(self):
        while True:
            try:
                then = self.done.get_nowait()
            except queue.Empty:
                break
            try:
                then()
            except Exception as e:  # noqa: BLE001
                sys.stderr.write(f"telegram: hub link: {type(e).__name__}\n")
        self.flush()
        now = self.bot.clock()
        self.secrets = {k: v for k, v in self.secrets.items() if v[1] > now}

    def flush(self):
        """What to show, in order; a network failure or 429 — the same item at the next tick, a permanent error — a line
        in the log (Telegram's words, never ours: they may hold a link) and the next one."""
        while self.out and self.bot.clock() >= self.hold:
            try:
                self.out[0]()
            except Exception as e:  # noqa: BLE001
                if getattr(e, "transient", False):
                    self.hold = self.bot.clock() + (getattr(e, "retry_after", None) or 0)
                    return
                sys.stderr.write(f"telegram: hub link: {e if hasattr(e, 'transient') else type(e).__name__}\n")
            self.out.pop(0)

    def key(self, payload):
        return {"callback_data": self.bot.key({**payload, "ts": self.bot.clock()})}

    def button(self, text, payload):
        return {"text": text, **self.key(payload)}

    def send(self, text, buttons=None, reply_to=None, route=None, then=None, force=None):
        def item():
            if self.bot.chat() is None:
                return
            params = {**self.bot.where(reply_to), "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
            if reply_to:
                params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
            if buttons:
                params["reply_markup"] = {"inline_keyboard": buttons}
            elif force:
                params["reply_markup"] = {"force_reply": True, "input_field_placeholder": force[:64]}
            mid = self.bot.call("sendMessage", **params)["message_id"]
            self.sent(mid, route, then)
        self.out.append(item)

    def sent(self, mid, route, then):
        if route:
            self.bot.st["routes"][str(mid)] = route
        if then:
            then(mid)

    def edit(self, where, text, buttons=None, route=None, then=None):
        """A message of the bot made anew (the buttons go unless given); none to edit or it is gone — a new one."""
        def item():
            mid = where() if callable(where) else where
            if self.bot.chat() is None:
                return
            if mid:
                try:
                    self.bot.api.call("editMessageText", chat_id=self.bot.chat(), message_id=mid, text=text,
                                      parse_mode="HTML", disable_web_page_preview=True,
                                      reply_markup={"inline_keyboard": buttons or []})
                    return self.sent(mid, route, then)
                except Exception as e:  # noqa: BLE001
                    if getattr(e, "transient", False):
                        raise
                    if "not modified" in str(e):
                        return self.sent(mid, route, then)
            params = {**self.bot.where(), "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
            if buttons:
                params["reply_markup"] = {"inline_keyboard": buttons}
            self.sent(self.bot.call("sendMessage", **params)["message_id"], route, then)
        self.out.append(item)

    def remember(self, lid, mid, own):
        """A message that carries the link `lid` (own — the bot's; else the owner's paste): it leaves the chat later."""
        msgs = self.bot.st["bus_msgs"]
        got = msgs.pop(lid, [])   # to the end: the oldest are forgotten first
        msgs[lid] = got + [[mid, own]]
        for old in list(msgs)[:-KEEP_LINKS]:
            msgs.pop(old)

    def forget(self, lid):
        """The link's messages leave the chat: deleted (a bot may for 48 hours in its private chat), the bot's own that
        cannot be — its text replaced."""
        for mid, own in self.bot.st["bus_msgs"].pop(lid or "", []):
            self.out.append(lambda mid=mid, own=own: self.drop(mid, own))

    def drop(self, mid, own):
        try:
            self.bot.api.call("deleteMessage", chat_id=self.bot.chat(), message_id=mid)
            return
        except Exception as e:  # noqa: BLE001
            if getattr(e, "transient", False):
                raise
        if own:
            try:
                self.bot.api.call("editMessageText", chat_id=self.bot.chat(), message_id=mid,
                                  text=i18n.t("telegram.bus.removed"), reply_markup={"inline_keyboard": []})
            except Exception as e:  # noqa: BLE001
                if getattr(e, "transient", False):
                    raise

    def prune(self):
        """Buttons of the bus — the last KEEP_KEYS of each kind (a press on an older one: «out of date»): a long chat's
        «Reply» buttons never push out «Matches». Joins whose card waits for A's answer — the last KEEP_LINKS."""
        keys = self.bot.st["keys"]
        kinds = {}
        for k, v in keys.items():
            if str(v.get("do", "")).startswith("bus_"):
                kinds.setdefault(v["do"], []).append(k)
        for got in kinds.values():
            for k in got[:-KEEP_KEYS]:
                keys.pop(k)
        cards = self.bot.st["bus_cards"]
        for old in list(cards)[:-KEEP_LINKS]:
            cards.pop(old)

    # --- A: a link ----------------------------------------------------------------------------------------------------

    def link_cmd(self, m, arg):
        """/link [1h|24h|7d]: the bus off — what will happen and «Enable and make a link» (the consent is the press);
        on — the link at once."""
        mid = m["message_id"]
        ttl = TTLS.get(arg.strip() or "24h")
        if ttl is None:
            return self.send(esc(i18n.t("telegram.bus.usage")), reply_to=mid)
        if not self.bus().get("enabled"):
            rows = [[self.button(i18n.t("telegram.bus.enable_link"), {"do": "bus_link", "ttl": ttl})]]
            return self.send(esc(i18n.t("cli.bus.consent")), buttons=rows, reply_to=mid)
        self.make(ttl, mid)

    def make(self, ttl, reply_to):
        if self.busy:
            self.send(esc(i18n.t("telegram.bus.busy")), reply_to=reply_to)
            return False
        holder = {}
        self.send(esc(i18n.t("telegram.bus.making")), reply_to=reply_to, then=lambda mid: holder.update(id=mid))

        def say(step, state, text):
            if step == "lib" and state == "run":   # minutes: say what takes them
                self.done.put(lambda: self.edit(lambda: holder.get("id"), esc(i18n.t("telegram.bus.installing"))))

        def then(done):
            if not done.ok:
                return self.edit(lambda: holder.get("id"), failed(done))
            lid = done.data["id"]
            rows = [[self.button(i18n.t("telegram.bus.revoke"), {"do": "bus_revoke", "id": lid})]]
            self.edit(lambda: holder.get("id"), link_text(done), rows, then=lambda mid: self.remember(lid, mid, True))
        return self.run("link", lambda flow: flow.link(ttl=ttl), then, say)

    def revoke(self, payload, cq):
        lid = payload["id"]
        payload["used"] = True

        def then(done):
            if done.ok:
                self.forget(lid)
                self.send(esc(i18n.t("telegram.bus.revoked")))
            else:
                payload["used"] = False   # the relay or the core did not answer: the same button again
                self.send(failed(done))
        self.run("revoke", lambda flow: flow.revoke(lid), then)
        return i18n.t("telegram.bus.revoking")

    # --- B: a pasted link or invitation -------------------------------------------------------------------------------

    def pasted(self, m, text):
        """The owner's message carries a link or a relay invitation (forwarded, pasted with words around, broken over
        lines): a preview card instead of a message to the hub. True — taken."""
        kind, token = find(text)
        if kind is None:
            return False
        mid, now = m["message_id"], self.bot.clock()
        if kind == "invite":
            try:
                inv = busrelay.read_invite(token)[0]
            except busrelay.Error as e:
                self.send(failed_code("bad_invite", str(e)), reply_to=mid)
                return True
            lid = invite_id(token)
            key = self.keep(token, now + KEY_TTL)
            self.remember(lid, mid, False)
            body = i18n.t("telegram.bus.invite_preview", url=inv["relay"], by=inv["by"])
            if not self.bus().get("enabled"):
                body += "\n\n" + i18n.t("cli.bus.consent")
            rows = [[self.button(i18n.t("telegram.bus.invite_btn"), {"do": "bus_join", "secret": key, "id": lid,
                                                                     "until": now + KEY_TTL, "invite": True}),
                     self.button(i18n.t("telegram.bus.no"), {"do": "bus_no", "secret": key, "id": lid})]]
            self.send(esc(body), buttons=rows, reply_to=mid)
            return True
        try:
            info = busflow.preview(token, self.bus(), now)
        except bus.Error as e:
            if e.code == "bad_link":   # not a link after all (the prefix mentioned, a cut one): the words go on as words
                return False
            self.send(failed_code(e.code, str(e) if e.code == "other_relay" else ""), reply_to=mid)
            return True
        except Exception as e:  # noqa: BLE001 — no ssh-keygen and the like: the owner's words, never the link
            sys.stderr.write(f"telegram: hub link, preview: {type(e).__name__}\n")
            self.send(failed_code("error", type(e).__name__), reply_to=mid)
            return True
        until = min(info["exp"], now + KEY_TTL)
        key = self.keep(token, until)
        self.remember(info["id"], mid, False)
        label = liaison.safe_label(info["label"])
        body = (f"<b>{esc(i18n.t('telegram.bus.preview_q', label=label))}</b>\n"
                + esc(i18n.t("telegram.bus.preview", code=info["code"], relay=info["relay"] or "-",
                             when=busflow._when(info["exp"]))))
        if info["needs"]:
            body += "\n" + esc(i18n.t("telegram.bus.preview_needs"))
        rows = [[self.button(i18n.t("telegram.bus.link_btn"), {"do": "bus_join", "secret": key, "id": info["id"],
                                                               "until": until, "label": label}),
                 self.button(i18n.t("telegram.bus.no"), {"do": "bus_no", "secret": key, "id": info["id"]})]]
        self.send(body, buttons=rows, reply_to=mid)
        return True

    def keep(self, text, until):
        return self.keep_as(secrets.token_hex(8), text, until)

    def keep_as(self, key, text, until):
        self.secrets[key] = (text, until or self.bot.clock() + KEY_TTL)
        return key

    def join(self, payload, cq):
        card = (cq.get("message") or {}).get("message_id")
        token = (self.secrets.get(payload.get("secret")) or (None,))[0] or self.from_reply(cq, payload)
        if not token:
            return i18n.t("telegram.bus.old")
        if self.busy:
            return i18n.t("telegram.bus.busy")
        payload["used"] = True
        if payload.get("secret") not in self.secrets:   # taken from the message again: keep it for a press after a failure
            self.keep_as(payload.get("secret"), token, payload.get("until"))
        invite = payload.get("invite")
        label = payload.get("label") or ""
        self.edit(card, esc(i18n.t("telegram.bus.relay_joining" if invite else "telegram.bus.joining", label=label)))
        if not invite:   # the core's notice of the end goes to this card too (A may answer after the bot stops watching)
            self.bot.st["bus_cards"][payload["id"]] = {"card": card, "label": label}

        def then(done):
            self.secrets.pop(payload.get("secret"), None)
            if invite and done.ok:   # the first hub of a relay: the next step is one press away
                self.forget(payload["id"])
                return self.edit(card, esc(i18n.t("telegram.bus.invite_done", url=done.data.get("url") or "-")),
                                 [[self.button(i18n.t("telegram.bus.make_btn"), {"do": "bus_link", "ttl": TTLS["24h"]})]])
            elif done.ok:
                text = esc(i18n.t("telegram.bus.joined", label=label))
            elif done.code in ("waiting", "queued"):   # A answers later; or the core is busy and takes the request by itself: the core's notice («Linked» or «not linked») ends the card
                text = "⏳ " + esc(done.text)
            elif done.code in SPENT:   # the link will not be used: it leaves the chat
                self.forget(payload["id"])
                text = failed(done)
            else:   # the network, an install, the core busy: the link stays, the same card may be pressed again
                self.keep_as(payload.get("secret"), token, payload.get("until"))
                payload["used"] = False
                return self.edit(card, failed(done), [[self.button(
                    i18n.t("telegram.bus.invite_btn" if invite else "telegram.bus.link_btn"), payload)]])
            self.edit(card, text)
        self.run("join", lambda flow: flow.join(token), then)
        return i18n.t("telegram.bus.joining_note")

    def from_reply(self, cq, payload):
        """The link of a card whose memory is gone (the bot restarted): the owner's message the card answers, if Telegram
        gives it — and only the link of this card."""
        text = (((cq.get("message") or {}).get("reply_to_message")) or {}).get("text") or ""
        kind, token = find(text)
        if kind == "invite" and payload.get("invite"):
            return token if invite_id(token) == payload.get("id") else None
        if kind != "link" or payload.get("invite"):
            return None
        try:
            return token if bus.read_link(token, None)["id"] == payload.get("id") else None
        except Exception:  # noqa: BLE001
            return None

    def no(self, payload, cq):
        payload["used"] = True
        self.secrets.pop(payload.get("secret"), None)
        self.forget(payload.get("id"))
        self.edit((cq.get("message") or {}).get("message_id"), esc(i18n.t("telegram.bus.declined")))
        return i18n.t("telegram.bus.declined_note")

    # --- after: the pair code, the other hub's words -----------------------------------------------------------------------

    def notice(self, data):
        """A notice of the core about the bus (owner.notify with `bus`): «Linked» with the code and «Matches», the other
        owner's words with «Reply»; the rest — as any notice. True — shown here. Runs inside the bot's event sending
        (bot.send repeats nothing that went)."""
        about = data.get("bus") if isinstance(data.get("bus"), dict) else {}
        kind, contact = about.get("kind"), about.get("contact")
        label = liaison.safe_label(about.get("label") or contact)
        route = {"bus": contact, "label": label}
        card = self.bot.st["bus_cards"].pop(about.get("id") or "", None) if kind in ("linked", "join_failed") else None
        if kind == "join_failed":   # B's link did not come about: its card says why, the link leaves the chat
            self.forget(about.get("id"))
            if card is None:
                return False   # the join went from the page or the terminal: the notice as it is
            self.edit(card["card"], "✗ " + esc(i18n.t("telegram.bus.failed", why=busflow.why(about.get("code") or ""))))
            return True
        if card:
            self.edit(card["card"], esc(i18n.t("telegram.bus.joined", label=card.get("label") or label)))
        if kind == "linked" and contact:
            rows = [[self.button(i18n.t("telegram.bus.matches"), {"do": "bus_verify", "contact": contact,
                                                                  "label": label}),
                     self.button(i18n.t("telegram.bus.write"), {"do": "bus_write", "contact": contact,
                                                                "label": label})]]
            self.bot.send(esc(i18n.t("telegram.bus.linked", code=about.get("code") or "-")),
                          head=f"🔗 <b>{esc(i18n.t('telegram.bus.linked_head', label=label))}</b>", buttons=rows,
                          route=route)
            self.forget(about.get("id"))
            return True
        if kind == "chat" and contact:
            rows = [[self.button(i18n.t("telegram.bus.reply"), {"do": "bus_write", "contact": contact, "label": label})]]
            self.bot.send(data.get("text") or "", head=f"💬 <b>{esc(label)}</b>", buttons=rows, route=route)
            return True
        if kind == "misused":
            self.bot.send(data.get("text") or "", head=f"⛔ <b>{esc(data.get('title'))}</b>")
            return True
        return False

    def verify(self, payload, cq):
        contact, label = payload["contact"], payload.get("label") or ""   # pressed again: the core answers the same

        def then(done):
            self.send(esc(i18n.t("telegram.bus.verified", label=label)) if done.ok else failed(done))
        self.run("verify", lambda flow: flow.verify(contact), then)
        return i18n.t("telegram.bus.verifying")

    def write(self, payload, cq):
        """«Reply» / «Write»: a message to answer — a reply to it goes to that owner."""
        label = payload.get("label") or ""
        self.send(esc(i18n.t("telegram.bus.write_prompt", label=label)), force=label,
                  route={"bus": payload["contact"], "label": label})
        return i18n.t("telegram.bus.write_note")

    def reply(self, m, route, text):
        """The owner's reply to a message of the other hub: his words go to that hub's liaison — no card, no turn here."""
        mid = m["message_id"]
        if not text or m.get("photo") or m.get("document"):
            return self.send(esc(i18n.t("telegram.bus.text_only")), reply_to=mid)
        self.chat(route["bus"], route.get("label") or route["bus"], text, mid)

    def chat(self, contact, label, text, mid, confirm=False):
        def then(done):
            if done.ok:
                self.out.append(lambda: self.bot.react(mid, "👌"))
            elif done.code == "secret" and not confirm:   # a key-like text: he sees it and says yes
                until = self.bot.clock() + KEY_TTL
                key = self.keep(text, until)
                rows = [[self.button(i18n.t("telegram.bus.send_anyway"), {"do": "bus_chat", "secret": key,
                                                                          "contact": contact, "label": label,
                                                                          "msg": mid, "until": until}),
                         self.button(i18n.t("telegram.bus.dont_send"), {"do": "bus_chat_no", "secret": key})]]
                self.send(esc(i18n.t("telegram.bus.secret_q", label=label)), buttons=rows, reply_to=mid)
            else:
                self.send(esc(i18n.t("telegram.bus.not_sent", why=done.text or done.code)), reply_to=mid)
        self.run("chat", lambda flow: flow.chat(contact, text, confirm=confirm), then)

    def chat_yes(self, payload, cq):
        text = (self.secrets.pop(payload.get("secret"), None) or (None,))[0]
        if not text:
            return i18n.t("telegram.bus.old")
        payload["used"] = True
        self.edit((cq.get("message") or {}).get("message_id"), esc(i18n.t("telegram.bus.sending")))
        self.chat(payload["contact"], payload.get("label") or "", text, payload.get("msg"), confirm=True)
        return i18n.t("telegram.bus.sending")

    def chat_no(self, payload, cq):
        payload["used"] = True
        self.secrets.pop(payload.get("secret"), None)
        self.edit((cq.get("message") or {}).get("message_id"), esc(i18n.t("telegram.bus.not_sent_kept")))
        return i18n.t("telegram.bus.declined_note")

    # --- a press ------------------------------------------------------------------------------------------------------

    def press(self, payload, cq):
        """A press on a button of the bus (the owner's — the bot checked): one press each; a preview, a held text and
        the consent — with a term. → the note on the button."""
        if payload.get("used"):
            return i18n.t("telegram.bus.pressed")
        do, now = payload["do"], self.bot.clock()
        if now > payload.get("until", float("inf")) or do == "bus_link" and now - payload.get("ts", 0) > CONSENT_TTL:
            return i18n.t("telegram.bus.old")
        if do == "bus_link":
            if not self.make(payload["ttl"], (cq.get("message") or {}).get("message_id")):
                return i18n.t("telegram.bus.busy")
            payload["used"] = True
            return i18n.t("telegram.bus.making_note")
        return {"bus_revoke": self.revoke, "bus_join": self.join, "bus_no": self.no, "bus_verify": self.verify,
                "bus_write": self.write, "bus_chat": self.chat_yes, "bus_chat_no": self.chat_no}[do](payload, cq)


def link_text(done):
    """The one message to forward: the link in monospace (a tap copies it), the term and what to do with it."""
    link = done.value
    hint = i18n.t("telegram.bus.link_hint", when=busflow._when(done.data.get("exp")))
    if not done.data.get("posted"):
        hint += "\n" + i18n.t("cli.bus.link_unposted")
    head = i18n.t("telegram.bus.link_head")
    if len(f"🔗 {head}\n{link}\n{hint}") > TEXT_MAX:   # the link alone always fits (core/bus.LINK_MAX for a hub's)
        return f"<code>{esc(link)}</code>"
    return f"🔗 <b>{esc(head)}</b>\n<code>{esc(link)}</code>\n{esc(hint)}"


def failed(done):
    text = "✗ " + esc(i18n.t("telegram.bus.failed", why=done.text or done.code))
    if done.command:
        text += "\n" + esc(i18n.t("cli.bus.command", command="")) + f"<code>{esc(done.command)}</code>"
    return text


def failed_code(code, reason=""):
    return "✗ " + esc(i18n.t("telegram.bus.failed", why=busflow.why(code, reason)))
