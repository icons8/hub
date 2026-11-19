"""The Telegram adapter of the owner's chats (spec d2108458 slice C0, core/chats.py): the hub's bot connected to the
owner's own account (Telegram → Settings → Chat automation; the bot needs Business Mode in @BotFather) sees the private
messages people write to him and answers them in his name — what a session asked to send (core/chats.py: the session
decides by the owner's rules, the core checks the conversation, one answer to a message and the secrets).

In: `business_connection` of the paired owner → chat.connected / chat.disconnected; `business_message` from a person
in a private chat → chat.received (text only). A connection made by anyone else — ignored: a stranger who connects the
hub's bot to his own account must not make his chats look like the owner's. The owner's own messages in those chats,
bots' messages, edits and media without text are not incoming.
Out: a session's request the core took (the chat.send_requested event the journal holds) → one
`sendMessage` with the connection id, as an answer to the person's message, with the agent's signature. Exactly once:
the out id is written to the bot's state file before the call; a call that may or may not have reached Telegram (a
network failure, a bot restart in the middle) is `chat.send_unknown`, never a second try.

The facts carry the bot's channel and `owner: false` (not the owner's put()): a person's words are not his.
"""
import secrets
import sys

from core import catalog, chats, i18n, spool

SERVICE = "telegram"
SENT_KEEP = 500   # outgoing ids remembered (a repeat of an old request event after a journal reread sends nothing)


def _name(user):
    user = user or {}
    return " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x) or user.get("username") or "—"


class Business:
    def __init__(self, bot):
        self.bot = bot
        st = bot.st
        st.setdefault("business", {})   # {"id", "user", "reply", "on"}: the owner's connection
        st.setdefault("chat_sent", {})  # out id -> "sending" | "unknown" | "failed" | Telegram message id

    # --- facts ---------------------------------------------------------------------------------------------------
    def put(self, type_, data):
        spool.put(self.bot.spool, type_, None, {**data, "service": SERVICE, "channel": "telegram", "owner": False},
                  src="telegram")

    def book(self, uid):
        """The owner's name for the person in his contacts book, by the Telegram id only (namesakes)."""
        try:
            from modules.contacts import contacts
            rows = contacts.people(contacts.default_path())
        except (OSError, ValueError):
            return None
        for r in rows:
            tg = r.get("telegram")
            if isinstance(tg, dict) and tg.get("id") is not None and str(tg["id"]) == str(uid):
                return r["name"]
        return None

    # --- in --------------------------------------------------------------------------------------------------------
    def on_connection(self, bc):
        user = bc.get("user") or {}
        if user.get("id") not in self.bot.owners():
            print(f"telegram: a business connection of another account ({user.get('id')}) — ignored", file=sys.stderr,
                  flush=True)
            return
        rights = bc.get("rights") or {}
        reply = bool(rights.get("can_reply", bc.get("can_reply")))
        mine = self.bot.st["business"]
        if bc.get("is_enabled") is False:
            if mine.get("on") and mine.get("id") == bc.get("id"):
                mine["on"] = False
                self.put("chat.disconnected", {})
                self.bot.send(i18n.t("telegram.chats.disconnected"))
            return
        was = dict(mine)
        mine.update(id=bc.get("id"), user=user.get("id"), reply=reply, on=True)
        if was.get("on") and (was.get("id"), was.get("reply")) == (mine["id"], reply):
            return
        self.put("chat.connected", {"reply": reply, "user": {"id": str(user["id"]), "name": _name(user)}})
        self.bot.send(i18n.t("telegram.chats.connected" if reply else "telegram.chats.connected_read"))

    def on_message(self, m):
        mine = self.bot.st["business"]
        if not mine.get("on") or m.get("business_connection_id") != mine.get("id"):
            return
        frm, chat = m.get("from") or {}, m.get("chat") or {}
        if chat.get("type") != "private" or frm.get("is_bot") or frm.get("id") in self.bot.owners():
            return   # C0: a person's private message; the owner's own and bots' — not incoming
        text = (m.get("text") or m.get("caption") or "").strip()
        if not text:
            return
        book = self.book(frm.get("id"))
        self.put("chat.received", {"conv": str(chat["id"]), "msg": str(m["message_id"]), "text": text,
                                   "marker": self.marker(text),   # the fence: not guessable by the sender
                                   "from": {"id": str(frm["id"]), "name": _name(frm),
                                            **({"username": frm["username"]} if frm.get("username") else {})},
                                   **({"book": book} if book else {})})

    @staticmethod
    def marker(text):
        while True:
            m = "chat-" + secrets.token_hex(4)
            if m not in text:
                return m

    # --- out -------------------------------------------------------------------------------------------------------
    def recover(self):
        """At start: an answer marked "sending" when the bot stopped may or may not have gone — unknown, not repeated."""
        for oid, v in list(self.bot.st["chat_sent"].items()):
            if v == "sending":
                self.bot.st["chat_sent"][oid] = "unknown"
                self.put("chat.send_unknown", {"out": oid})

    def signature(self):
        cfg = catalog.config_or_last(self.bot.state_dir) or {}
        sec = cfg.get("chats") if isinstance(cfg.get("chats"), dict) else {}
        return "" if sec.get("signature") is False else "\n\n— 🤖 " + i18n.t("telegram.chats.signature")

    def outgoing(self, ev, st):
        """The journal event ev is folded in st: send the answer it put on its way."""
        if ev.get("type") != "chat.send_requested" or ev.get("rejected"):
            return
        out = chats.to_send(st, ev["seq"], SERVICE)
        if out:
            self.send(*out)

    def send(self, oid, o):
        from modules.telegram.bot import TgError
        done = self.bot.st["chat_sent"]
        if oid in done:
            return
        mine = self.bot.st["business"]
        group = self.bot.group() is not None   # a group: chats with people are off
        if not mine.get("on") or not mine.get("id") or group:
            done[oid] = "failed"
            self.put("chat.send_failed", {"out": oid, "why": "the bot works in a group: chats with people are off"
                                          if group else "no connection"})
            return
        done[oid] = "sending"
        while len(done) > SENT_KEEP:
            done.pop(next(iter(done)))
        self.bot.save()   # before the call: a restart in the middle is "unknown", not a second message
        try:
            r = self.bot.api.call("sendMessage", _once=True, business_connection_id=mine["id"], chat_id=int(o["conv"]),
                                  text=o["text"] + self.signature(),
                                  reply_parameters={"message_id": int(o["reply_to"]),
                                                    "allow_sending_without_reply": True})
        except TgError as e:
            if e.transient and e.retry_after is None and not e.unsent:   # a network failure or 5xx after the request
                # left: it may have reached Telegram — never repeated
                done[oid] = "unknown"
                self.put("chat.send_unknown", {"out": oid})
            else:
                done[oid] = "failed"
                self.put("chat.send_failed", {"out": oid, "why": str(e)[:300]})
            return
        except Exception as e:  # noqa: BLE001 — the bot's own failure: unknown whether it went, never repeated
            done[oid] = "unknown"
            self.put("chat.send_unknown", {"out": oid})
            print(f"telegram: answer {oid}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            return
        done[oid] = str((r or {}).get("message_id") or "")
        self.put("chat.sent", {"out": oid, **({"msg": done[oid]} if done[oid] else {})})
